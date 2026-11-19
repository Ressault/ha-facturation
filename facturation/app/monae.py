"""Import de l'historique depuis MonAE / facturation.pro via leur API (clé API).

Documentation : https://facturation.dev — authentification HTTP Basic (identifiant API + clé API).
Récupère les clients, toutes les factures et avoirs (lignes, règlements) et les PDF originaux.
"""
import json
import os
import re
import threading
import time

import requests

import db
from calc import d, money

BASE = "https://www.facturation.pro"
UA = "HA-Facturation (Ressault@users.noreply.github.com)"
_lock = threading.Lock()


class MonAEError(Exception):
    pass


class Api:
    def __init__(self, login, key):
        self.s = requests.Session()
        self.s.auth = (login.strip(), key.strip())
        self.s.headers.update({"User-Agent": UA, "Accept": "application/json"})
        self.last = 0.0

    def get(self, path, params=None, raw=False):
        for _ in range(5):
            wait = 0.55 - (time.time() - self.last)  # 600 requêtes / 5 min maximum
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            r = self.s.get(BASE + path, params=params, timeout=60)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "30")) + 1)
                continue
            if r.status_code == 401:
                raise MonAEError("Identifiant ou clé API MonAE refusés.")
            if r.status_code >= 400:
                raise MonAEError(f"MonAE {path} : HTTP {r.status_code} {r.text[:200]}")
            return r if raw else r.json()
        raise MonAEError("MonAE limite les requêtes : réessayez dans quelques minutes.")

    def pages(self, path, params=None):
        page = 1
        while True:
            r = self.get(path, dict(params or {}, page=page), raw=True)
            yield r.json()
            info = json.loads(r.headers.get("X-Pagination") or "{}")
            if page >= int(info.get("total_pages") or 1):
                break
            page += 1


# -- état de la tâche (stocké en base pour survivre aux rechargements de page) --------

def set_job(**kw):
    conn = db.connect()
    try:
        job = db.loads(db.get_settings(conn).get("monae_job"), {}) or {}
        job.update(kw)
        db.set_settings({"monae_job": db.dumps(job)}, conn)
        conn.commit()
    finally:
        conn.close()


def get_job():
    return db.loads(db.get_settings().get("monae_job"), {}) or {}


def start(login, key, firm_id=None, with_pdfs=True):
    if not _lock.acquire(blocking=False):
        raise MonAEError("Un import est déjà en cours.")
    set_job(status="running", step="Connexion à MonAE…", done=0, total=0, report=None, error=None, firms=None,
            started=time.time())

    def run():
        try:
            _run(Api(login, key), firm_id, with_pdfs)
        except Exception as e:  # noqa: BLE001
            set_job(status="error", error=str(e))
        finally:
            _lock.release()
    threading.Thread(target=run, daemon=True).start()


# -- conversion ---------------------------------------------------------------------

def client_name(c):
    name = (c.get("company_name") or "").strip()
    person = " ".join(x for x in [(c.get("first_name") or "").strip(), (c.get("last_name") or "").strip()] if x)
    return name or person or (c.get("short_name") or "").strip() or f"Client {c.get('id')}"


def parse_pa_address(v):
    v = (v or "").strip()
    if not v:
        return "", ""
    if ":" in v:
        scheme, value = v.split(":", 1)
        return scheme, value
    return "0225", v


def import_customer(conn, c):
    siren = re.sub(r"\D", "", c.get("siret") or "")[:9]
    name = client_name(c)
    row = None
    if siren:
        row = conn.execute("SELECT id FROM clients WHERE siren = ?", (siren,)).fetchone()
    if not row:
        row = conn.execute("SELECT id FROM clients WHERE lower(name) = lower(?)", (name,)).fetchone()
    if row:
        return row["id"], False
    scheme, value = parse_pa_address(c.get("electronic_address"))
    kind = "particulier" if c.get("individual") else "pro"
    phone = c.get("phone") or c.get("mobile") or ""
    cid = conn.execute(
        "INSERT INTO clients(kind, name, siren, vat_number, address, post_code, city, country, email, phone, "
        "pdp_address_scheme, pdp_address_value, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, name, siren, (c.get("vat_number") or "").replace(" ", ""), c.get("street") or "",
         c.get("zip_code") or "", c.get("city") or "", (c.get("country") or "FR")[:2].upper(), c.get("email") or "",
         phone, scheme, value, "Importé de MonAE")).lastrowid
    return cid, True


PAY_MODES = {1: "paypal", 2: "carte", 3: "virement", 4: "chèque", 5: "espèces", 8: "prélèvement"}


NATURE_MAP = {1: "vente", 2: "bic", 3: "bnc", 8: "debours"}
STYLE_MAP = {"title": "title", "comment": "comment", "subtotal": "subtotal", "new_page": "page"}


def build_lines(inv, default_nature="bnc"):
    """Lignes MonAE -> nos lignes : titres, commentaires, sous-totaux, sauts de page, nature, remise globale."""
    sign = -1 if d(inv.get("total")) < 0 else 1
    rebate = d(inv.get("rebate_percentage") or 0)
    out = []
    for it in sorted(inv.get("items") or [], key=lambda x: x.get("position") or 0):
        if it.get("optional"):
            continue
        style = STYLE_MAP.get(it.get("style") or "")
        title = (it.get("title") or "").strip()
        if style:
            if style in ("title", "comment") and not title:
                continue
            out.append({"kind": style, "designation": title[:2000] or ("Sous-total" if style == "subtotal" else ""),
                        "description": "", "quantity": 0.0, "unit": "", "unit_price": 0.0, "vat_rate": 0.0,
                        "nature": None})
            continue
        qty, price = d(it.get("quantity") or 0), d(it.get("unit_price") or 0)
        if qty == 0 and price == 0:
            if title:
                out.append({"kind": "comment", "designation": title[:2000], "description": "", "quantity": 0.0,
                            "unit": "", "unit_price": 0.0, "vat_rate": 0.0, "nature": None})
            continue
        if rebate:
            price = price * (100 - rebate) / 100
        if sign < 0:  # avoir : montants positifs
            qty, price = abs(qty), abs(price)
        rate = d(it.get("vat") or 0)
        rate = rate * 100 if rate < 1 else rate
        first, _, rest = title.partition("\n")
        out.append({"kind": "item", "designation": (first or "Prestation").strip()[:500],
                    "description": rest.strip(), "quantity": float(qty), "unit": (it.get("measure") or "u")[:12],
                    "unit_price": float(price), "vat_rate": float(rate.quantize(d("0.1")).normalize()),
                    "nature": NATURE_MAP.get(it.get("nature")) or default_nature})
    for i, ln in enumerate(out):
        ln["position"] = i
    return out


def check_lines(lines, inv):
    from calc import totals
    if not any(ln["kind"] == "item" for ln in lines):
        return False
    t = totals(lines)
    ht = abs(money(inv.get("total")))
    ttc = abs(money(inv.get("total_with_vat"))) if inv.get("total_with_vat") is not None else None
    return abs(t["ht"] - ht) <= d("0.02") and (ttc is None or abs(t["ttc"] - ttc) <= d("0.02"))


def fallback_lines(inv):
    ht = abs(money(inv.get("total")))
    ttc = abs(money(inv.get("total_with_vat") or inv.get("total")))
    rate = ((ttc - ht) * 100 / ht) if ht else d(0)
    rate = min([d(20), d(10), d("5.5"), d("2.1"), d(0)], key=lambda r: abs(r - rate))
    return [{"position": 0, "kind": "item", "designation": inv.get("title") or f"Facture {inv.get('invoice_ref')}",
             "description": "", "quantity": 1.0, "unit": "u", "unit_price": float(ht), "vat_rate": float(rate),
             "nature": None}]


def map_terms(code):
    from calc import PAYMENT_TERMS
    codes = {c for c, _ in PAYMENT_TERMS}
    code = (code or "").strip()
    if not code:
        return None
    if code in codes:
        return code
    if f"{code}-inv" in codes:
        return f"{code}-inv"
    return code if code.replace("-inv", "").replace("fm", "").isdigit() else None


def _num_or_none(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def resolve_client(conn, api, inv, client_map, report):
    cid = client_map.get(inv.get("customer_id"))
    if not cid:
        try:
            cid, _ = import_customer(conn, api.get(f"/firms/{report['firm_id']}/customers/{inv['customer_id']}.json"))
        except MonAEError:
            cid = conn.execute("INSERT INTO clients(kind, name) VALUES ('pro', 'Client MonAE')").lastrowid
        client_map[inv.get("customer_id")] = cid
    return cid


def import_invoice(conn, api, inv, client_map, with_pdfs, report, kind_hint=None):
    import logic
    from calc import totals
    s = db.get_settings(conn)
    is_quote = kind_hint == "devis"
    draft = bool(inv.get("draft"))
    ref = (inv.get("full_invoice_ref") or inv.get("invoice_ref") or "") if not is_quote else \
        str(inv.get("full_quote_ref") or inv.get("quote_ref") or "")
    kind = "devis" if is_quote else ("avoir" if d(inv.get("total")) < 0 else "facture")
    lines = build_lines(inv, s.get("default_nature") or "bnc")
    if not check_lines(lines, inv):
        report["approx"] += 1
        lines = fallback_lines(inv)
    t = totals(lines)
    terms = map_terms(inv.get("pay_before"))
    extra_cols = {"payment_terms": terms, "penalty_rate": _num_or_none(inv.get("penalty")),
                  "discount_rate": _num_or_none(inv.get("discount")), "monae_id": inv.get("id")}

    existing = conn.execute("SELECT id, imported, status FROM documents WHERE monae_id = ? AND kind = ?",
                            (inv.get("id"), kind)).fetchone()
    if not existing and ref and not draft:
        existing = conn.execute("SELECT id, imported, status FROM documents WHERE number = ? AND kind = ?",
                                (ref if not is_quote else f"MONAE-D{ref}", kind)).fetchone() or \
            conn.execute("SELECT id, imported, status FROM documents WHERE number = ? AND kind IN ('facture','avoir')",
                         (ref,)).fetchone()
    if existing:
        if not existing["imported"] and existing["status"] != "brouillon":
            report["skipped"] += 1
            return
        # mise à jour d'un document déjà importé : lignes typées, conditions de règlement
        conn.execute("DELETE FROM lines WHERE document_id = ?", (existing["id"],))
        logic.insert_lines(conn, existing["id"], lines)
        sets = ", ".join(f"{k} = ?" for k in extra_cols)
        conn.execute(f"UPDATE documents SET {sets}, total_ht = ?, total_tva = ?, total_ttc = ? WHERE id = ?",
                     (*extra_cols.values(), float(t["ht"]), float(t["tva"]), float(t["ttc"]), existing["id"]))
        report["refreshed"] += 1
        return

    cid = resolve_client(conn, api, inv, client_map, report)
    if draft and not is_quote:
        doc_id = conn.execute(
            "INSERT INTO documents(kind, number, status, client_id, issue_date, due_date, subject, notes, total_ht, "
            "total_tva, total_ttc, imported, payment_terms, penalty_rate, discount_rate, monae_id) "
            "VALUES (?, NULL, 'brouillon', ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
            (kind, cid, inv.get("invoiced_on"), inv.get("term_on"), inv.get("title"), inv.get("information"),
             float(t["ht"]), float(t["tva"]), float(t["ttc"]), *extra_cols.values())).lastrowid
        logic.insert_lines(conn, doc_id, lines)
        report["drafts"] += 1
        return
    if is_quote:
        status = {0: "en_attente", 1: "accepte", 9: "refuse"}.get(inv.get("quote_status"), "en_attente")
        if inv.get("fully_invoiced"):
            status = "facture"
        number = f"MONAE-D{ref}" if ref else f"MONAE-D{inv.get('id')}"
        doc_id = conn.execute(
            "INSERT INTO documents(kind, number, status, client_id, issue_date, validity_date, subject, notes, "
            "total_ht, total_tva, total_ttc, imported, client_snapshot, seller_snapshot, payment_terms, penalty_rate, "
            "discount_rate, monae_id) VALUES ('devis', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
            (number, status, cid, inv.get("invoiced_on"), inv.get("term_on"), inv.get("title"),
             inv.get("information"), float(t["ht"]), float(t["tva"]), float(t["ttc"]),
             db.dumps(logic.snapshot_client(conn, cid)), db.dumps(logic.snapshot_seller(conn)),
             *extra_cols.values())).lastrowid
        logic.insert_lines(conn, doc_id, lines)
        if with_pdfs:
            _download(api, conn, report, f"/firms/{report['firm_id']}/quotes/{inv['id']}.pdf", doc_id,
                      inv.get("invoiced_on"), f"devis-{number}", {})
        report["quotes"] += 1
        return

    full_ref = ref or f"MONAE-{inv['id']}"
    paid_on = inv.get("paid_on")
    settlements = inv.get("settlements") or []
    status = "emise"
    if kind == "facture":
        if paid_on:
            status = "payee"
        elif settlements:
            status = "partielle"
    elif paid_on:
        status = "rembourse"
    doc_id = conn.execute(
        "INSERT INTO documents(kind, number, status, client_id, issue_date, due_date, subject, notes, total_ht, "
        "total_tva, total_ttc, imported, client_snapshot, seller_snapshot, validated_at, payment_terms, penalty_rate, "
        "discount_rate, monae_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?)",
        (kind, full_ref, status, cid, inv.get("invoiced_on"), inv.get("term_on"), inv.get("title"),
         inv.get("information"), float(t["ht"]), float(t["tva"]), float(t["ttc"]),
         db.dumps(logic.snapshot_client(conn, cid)), db.dumps(logic.snapshot_seller(conn)),
         *extra_cols.values())).lastrowid
    logic.insert_lines(conn, doc_id, lines)
    # encaissements (historique : déjà déclarés, jamais retransmis)
    if kind == "facture":
        pays = [(st.get("paid_on"), abs(money(st.get("total"))), PAY_MODES.get(st.get("payment_mode"), "autre"))
                for st in settlements if st.get("paid_on")]
        paid_sum = sum((p_[1] for p_ in pays), d(0))
        if paid_on and paid_sum < t["ttc"]:
            pays.append((paid_on, t["ttc"] - paid_sum, PAY_MODES.get(inv.get("payment_mode"), "autre")))
        for date_, amount, mode in pays:
            if amount > 0:
                conn.execute("INSERT INTO payments(document_id, date, amount, method, pdp_reported) "
                             "VALUES (?, ?, ?, ?, 1)", (doc_id, date_, float(amount), f"{mode} (MonAE)"))
    safe = re.sub(r"[^\w.-]", "-", full_ref)
    if with_pdfs:
        _download(api, conn, report, f"/firms/{report['firm_id']}/invoices/{inv['id']}.pdf", doc_id,
                  inv.get("invoiced_on"), f"{kind}-{safe}", {"original": 1}, raw_json=inv)
    report["invoices" if kind == "facture" else "avoirs"] += 1
    report["refs"].append((inv.get("invoiced_on") or "", full_ref))


def _download(api, conn, report, path, doc_id, issued, name, params, raw_json=None):
    year = (issued or "0000")[:4]
    folder = os.path.join(db.ARCHIVE_DIR, year)
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r"[^\w.-]", "-", name)
    if raw_json is not None:
        with open(os.path.join(folder, f"monae-{safe}.json"), "w", encoding="utf-8") as f:
            json.dump(raw_json, f, ensure_ascii=False, indent=1)
    try:
        r = api.get(path, params, raw=True)
        out = os.path.join(folder, f"{safe}.pdf")
        with open(out, "wb") as f:
            f.write(r.content)
        conn.execute("UPDATE documents SET pdf_path = ? WHERE id = ?", (out, doc_id))
    except MonAEError as e:
        report["pdf_errors"] += 1
        report.setdefault("errors", []).append(f"PDF {name} : {e}")


def import_supplier(conn, sup):
    name = client_name(sup)
    siren = re.sub(r"\D", "", sup.get("siret") or "")[:9] or None
    row = conn.execute("SELECT id FROM suppliers WHERE monae_id = ?", (sup.get("id"),)).fetchone()
    row = row or conn.execute("SELECT id FROM suppliers WHERE lower(name) = lower(?)", (name,)).fetchone()
    if row:
        conn.execute("UPDATE suppliers SET monae_id = ? WHERE id = ?", (sup.get("id"), row["id"]))
        return row["id"], False
    sid = conn.execute(
        "INSERT INTO suppliers(name, siren, vat_number, address, post_code, city, country, email, phone, website, "
        "notes, monae_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, siren, (sup.get("vat_number") or "").replace(" ", ""), sup.get("street") or "", sup.get("zip_code") or "",
         sup.get("city") or "", (sup.get("country") or "FR")[:2].upper(), sup.get("email") or "",
         sup.get("phone") or sup.get("mobile") or "", sup.get("website") or "", sup.get("notes") or "",
         sup.get("id"))).lastrowid
    return sid, True


PURCHASE_MODES = {1: "paypal", 2: "carte", 3: "virement", 4: "chèque", 5: "espèces", 8: "prélèvement",
                  9: "TIP", 20: "crédit fournisseur", 90: "avoir", 99: "autre"}
PURCHASE_NATURE = {1: "biens", 2: "services", 5: "mixte", 9: "autre"}


def import_purchase(conn, p, supplier_map, report):
    if p.get("draft") or p.get("soft_deleted"):
        return
    if conn.execute("SELECT 1 FROM purchases WHERE monae_id = ?", (p.get("id"),)).fetchone():
        report["skipped"] += 1
        return
    ttc = money(p.get("total_with_vat") or 0)
    tva = money(p.get("vat_amount") or 0)
    ht = money(p.get("total")) if p.get("total") is not None else ttc - tva
    twin = conn.execute("SELECT id FROM purchases WHERE monae_id IS NULL AND source = 'recurrent' AND invoice_date = ?"
                        " AND abs(total_ttc - ?) < 0.01", (p.get("invoiced_on"), float(ttc))).fetchone()
    if twin:
        conn.execute("UPDATE purchases SET monae_id = ? WHERE id = ?", (p.get("id"), twin["id"]))
        report["skipped"] += 1
        return
    sid = supplier_map.get(p.get("supplier_id"))
    sname = (p.get("supplier_identity") or "").strip()
    if sid:
        sname = conn.execute("SELECT name FROM suppliers WHERE id = ?", (sid,)).fetchone()["name"]
    elif sname:
        import logic
        sid = logic.supplier_id_for(conn, sname)
    category = "immobilisation" if p.get("accounting_asset") else "biens_services"
    paid = p.get("paid_on")
    conn.execute(
        "INSERT INTO purchases(supplier, supplier_id, invoice_number, invoice_date, description, total_ht, total_tva,"
        " total_ttc, category, nature, method, paid_date, deduction_date, due_date, source, monae_id, notes)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'monae', ?, ?)",
        (sname or "Fournisseur", sid, p.get("ref"), p.get("invoiced_on"), p.get("title"), float(ht), float(tva),
         float(ttc), category, PURCHASE_NATURE.get(p.get("nature"), "services"),
         PURCHASE_MODES.get(p.get("payment_mode")) if paid else None, paid, paid or None, p.get("term_on"),
         p.get("id"), p.get("notes")))
    report["purchases"] += 1


def import_recurring(conn, r, supplier_map, report):
    if conn.execute("SELECT 1 FROM recurring_purchases WHERE monae_id = ?", (r.get("id"),)).fetchone():
        return
    sid = supplier_map.get(r.get("supplier_id"))
    if not sid and r.get("supplier_identity"):
        import logic
        sid = logic.supplier_id_for(conn, r["supplier_identity"])
    ttc = money(r.get("total_with_vat") or 0)
    tva = money(r.get("vat_amount") or 0)
    ht = money(r.get("total")) if r.get("total") is not None else ttc - tva
    freq = r.get("frequency") or r.get("every") or 30
    try:
        months = max(1, round(int(freq) / 30))
    except (TypeError, ValueError):
        months = 1
    nxt = r.get("next_on") or r.get("next_date") or r.get("start_on")
    if not nxt:
        return
    conn.execute(
        "INSERT INTO recurring_purchases(supplier_id, title, total_ht, total_tva, total_ttc, every_months, next_date,"
        " max_occurrences, occurrences, active, monae_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, r.get("title") or "Abonnement", float(ht), float(tva), float(ttc), months, nxt[:10],
         r.get("occurrences_max") or None, int(r.get("occurrences") or r.get("occurrences_count") or 0),
         0 if r.get("ended") or str(r.get("status")) == "1" else 1, r.get("id")))
    report["recurring"] += 1


def detect_numbering(refs):
    """Déduit le format de numérotation MonAE et le prochain numéro à utiliser."""
    if not refs:
        return None
    refs = sorted(refs)
    last_date, last_ref = refs[-1]
    m = re.fullmatch(r"(\d{4})(\d{2})-(\d+)", last_ref)
    if m:
        nums = [int(re.fullmatch(r"\d{6}-(\d+)", r).group(1)) for _, r in refs if re.fullmatch(r"\d{6}-(\d+)", r)]
        return {"pattern": "{AAAA}{MM}-{N}", "next": max(nums) + 1, "reset_yearly": "0", "last": last_ref}
    m = re.fullmatch(r"(.*?)(\d+)", last_ref)
    if m:
        prefix, digits = m.groups()
        pattern = prefix.replace(last_date[:4], "{AAAA}") + "{" + "N" * len(digits) + "}"
        same = [int(re.fullmatch(r".*?(\d+)", r).group(1)) for _, r in refs if r.startswith(prefix)]
        return {"pattern": pattern, "next": (max(same) if same else int(digits)) + 1,
                "reset_yearly": "1" if "{AAAA}" in pattern else "0", "last": last_ref}
    return {"last": last_ref}


def _run(api, firm_id, with_pdfs):
    acct = api.get("/account.json")
    firms = acct.get("firms") or []
    if not firm_id:
        if len(firms) == 1:
            firm_id = firms[0]["id"]
        elif not firms:
            raise MonAEError("Aucune entreprise trouvée sur ce compte MonAE.")
        else:
            set_job(status="choose_firm", firms=firms, step="Plusieurs entreprises : choisissez la vôtre.")
            return
    report = {"firm_id": firm_id, "clients": 0, "invoices": 0, "avoirs": 0, "skipped": 0, "approx": 0,
              "pdf_errors": 0, "refs": [], "refreshed": 0, "drafts": 0, "quotes": 0, "suppliers": 0,
              "purchases": 0, "recurring": 0, "moved": 0}
    conn = db.connect()
    try:
        set_job(step="Clients…")
        client_map = {}
        for page in api.pages(f"/firms/{firm_id}/customers.json"):
            for c in page:
                cid, created = import_customer(conn, c)
                client_map[c["id"]] = cid
                report["clients"] += int(created)
        conn.commit()
        set_job(step="Fournisseurs…")
        supplier_map = {}
        for page in api.pages(f"/firms/{firm_id}/suppliers.json"):
            for sup in page:
                sid, created = import_supplier(conn, sup)
                supplier_map[sup["id"]] = sid
                report["suppliers"] += int(created)
        # contacts fournisseurs importés par erreur dans les clients (CSV) : déplacés s'ils n'ont aucun document
        import logic
        names = {r["name"].strip().lower() for r in conn.execute("SELECT name FROM suppliers").fetchall()}
        ids = [r["id"] for r in conn.execute(
            "SELECT id, name FROM clients c WHERE NOT EXISTS (SELECT 1 FROM documents d WHERE d.client_id = c.id)"
        ).fetchall() if (r["name"] or "").strip().lower() in names]
        report["moved"] = logic.move_clients_to_suppliers(conn, ids)
        conn.commit()
        set_job(step="Liste des factures…")
        invoices = []
        for page in api.pages(f"/firms/{firm_id}/invoices.json",
                              {"with_details": 1, "with_settlements": 1, "order": "billed", "sort": "asc"}):
            invoices += page
        set_job(step="Liste des devis…")
        quotes = []
        for page in api.pages(f"/firms/{firm_id}/quotes.json", {"with_details": 1, "order": "billed", "sort": "asc"}):
            quotes += page
        set_job(step="Liste des achats…")
        purchases = []
        for page in api.pages(f"/firms/{firm_id}/purchases.json"):
            purchases += page
        recurring = []
        for path in (f"/firms/{firm_id}/recurring_purchases.json", f"/firms/{firm_id}/purchases/recurring.json"):
            try:
                for page in api.pages(path):
                    recurring += [x for x in (page if isinstance(page, list) else []) if isinstance(x, dict)]
                if recurring:
                    break
            except MonAEError:
                continue
        work = [("facture", i) for i in invoices] + [("devis", q) for q in quotes]
        total = len(work) + len(purchases)
        set_job(total=total, step=f"{len(invoices)} factures, {len(quotes)} devis, {len(purchases)} achats…")
        n = 0
        for hint, inv in work:
            import_invoice(conn, api, inv, client_map, with_pdfs, report, "devis" if hint == "devis" else None)
            conn.commit()
            n += 1
            if n % 5 == 0:
                set_job(done=n, step=f"{'Devis' if hint == 'devis' else 'Facture'} "
                                     f"{inv.get('full_invoice_ref') or inv.get('invoice_ref') or inv.get('quote_ref') or ''}"
                                     f" ({n}/{total})")
        for p in purchases:
            import_purchase(conn, p, supplier_map, report)
            n += 1
        for r in recurring:
            try:
                import_recurring(conn, r, supplier_map, report)
            except Exception as e:  # format inconnu : la détection ci-dessous prend le relais
                db.log(conn, "warning", f"Achat récurrent MonAE ignoré : {e}")
        import logic
        report["recurring"] += logic.detect_recurring(conn)
        conn.commit()
        set_job(done=total, step="Finalisation…")
        numbering = detect_numbering(report["refs"]) if report["refs"] else None
        if numbering and numbering.get("pattern"):
            year = int(sorted(report["refs"])[-1][0][:4] or 0)
            from logic import counter_key
            db.set_settings({"invoice_pattern": numbering["pattern"],
                             "invoice_reset_yearly": numbering["reset_yearly"]}, conn)
            _, ck = counter_key(db.get_settings(conn), "invoice", year)
            db.set_settings({ck: numbering["next"]}, conn)
            conn.commit()
        report["numbering"] = numbering
        report.pop("refs")
        db.log(conn, "info", f"Import MonAE : {report['clients']} clients, {report['invoices']} factures, "
                             f"{report['avoirs']} avoirs, {report['quotes']} devis, {report['drafts']} brouillons, "
                             f"{report['suppliers']} fournisseurs, {report['purchases']} achats, "
                             f"{report['refreshed']} documents mis à jour.")
        conn.commit()
        set_job(status="done", step="Terminé", report=report, done=len(invoices))
    finally:
        conn.close()
