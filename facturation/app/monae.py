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


def build_lines(inv):
    """Lignes de la facture MonAE -> nos lignes (montants positifs, remise globale répercutée)."""
    sign = -1 if d(inv.get("total")) < 0 else 1
    rebate = d(inv.get("rebate_percentage") or 0)
    out = []
    for it in sorted(inv.get("items") or [], key=lambda x: x.get("position") or 0):
        if it.get("optional"):
            continue
        qty, price = d(it.get("quantity") or 0), d(it.get("unit_price") or 0)
        if qty == 0 and price == 0:
            continue  # ligne de titre / commentaire
        if rebate:
            price = price * (100 - rebate) / 100
        if sign < 0:  # avoir : montants positifs
            qty, price = abs(qty), abs(price)
        rate = d(it.get("vat") or 0)
        rate = rate * 100 if rate < 1 else rate
        out.append({"designation": (it.get("title") or "Prestation").strip()[:500], "description": "",
                    "quantity": float(qty), "unit": "u", "unit_price": float(price),
                    "vat_rate": float(rate.quantize(d("0.1")).normalize())})
    return out


def check_lines(lines, inv):
    from calc import totals
    if not lines:
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
    return [{"designation": inv.get("title") or f"Facture {inv.get('invoice_ref')}", "description": "",
             "quantity": 1.0, "unit": "u", "unit_price": float(ht), "vat_rate": float(rate)}]


def import_invoice(conn, api, inv, client_map, with_pdfs, report):
    ref = inv.get("invoice_ref") or inv.get("full_invoice_ref") or f"MONAE-{inv['id']}"
    full_ref = inv.get("full_invoice_ref") or ref
    if conn.execute("SELECT 1 FROM documents WHERE number = ?", (full_ref,)).fetchone():
        report["skipped"] += 1
        return
    import logic
    is_avoir = d(inv.get("total")) < 0
    kind = "avoir" if is_avoir else "facture"
    cid = client_map.get(inv.get("customer_id"))
    if not cid:
        try:
            cid, _ = import_customer(conn, api.get(f"/firms/{report['firm_id']}/customers/{inv['customer_id']}.json"))
        except MonAEError:
            cid = conn.execute("INSERT INTO clients(kind, name) VALUES ('pro', 'Client MonAE')").lastrowid
        client_map[inv.get("customer_id")] = cid
    lines = build_lines(inv)
    if not check_lines(lines, inv):
        report["approx"] += 1
        lines = fallback_lines(inv)
    from calc import totals
    t = totals(lines)
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
        "INSERT INTO documents(kind, number, status, client_id, issue_date, due_date, subject, total_ht, total_tva, "
        "total_ttc, imported, client_snapshot, seller_snapshot, validated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, CURRENT_TIMESTAMP)",
        (kind, full_ref, status, cid, inv.get("invoiced_on"), inv.get("term_on"), inv.get("title"),
         float(t["ht"]), float(t["tva"]), float(t["ttc"]), db.dumps(logic.snapshot_client(conn, cid)),
         db.dumps(logic.snapshot_seller(conn)))).lastrowid
    for i, ln in enumerate(lines):
        conn.execute("INSERT INTO lines(document_id, position, designation, description, quantity, unit, "
                     "unit_price, vat_rate) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (doc_id, i, ln["designation"], ln["description"], ln["quantity"], ln["unit"],
                      ln["unit_price"], ln["vat_rate"]))
    # encaissements (historique : déjà déclarés, jamais retransmis)
    if kind == "facture":
        pays = [(s.get("paid_on"), abs(money(s.get("total"))), PAY_MODES.get(s.get("payment_mode"), "autre"))
                for s in settlements if s.get("paid_on")]
        paid_sum = sum((p[1] for p in pays), d(0))
        if paid_on and paid_sum < t["ttc"]:
            pays.append((paid_on, t["ttc"] - paid_sum, PAY_MODES.get(inv.get("payment_mode"), "autre")))
        for date_, amount, mode in pays:
            if amount > 0:
                conn.execute("INSERT INTO payments(document_id, date, amount, method, pdp_reported) "
                             "VALUES (?, ?, ?, ?, 1)", (doc_id, date_, float(amount), f"{mode} (MonAE)"))
    # archives : JSON brut + PDF original
    year = (inv.get("invoiced_on") or "0000")[:4]
    folder = os.path.join(db.ARCHIVE_DIR, year)
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r"[^\w.-]", "-", full_ref)
    with open(os.path.join(folder, f"monae-{safe}.json"), "w", encoding="utf-8") as f:
        json.dump(inv, f, ensure_ascii=False, indent=1)
    if with_pdfs:
        try:
            r = api.get(f"/firms/{report['firm_id']}/invoices/{inv['id']}.pdf", {"original": 1}, raw=True)
            path = os.path.join(folder, f"{kind}-{safe}.pdf")
            with open(path, "wb") as f:
                f.write(r.content)
            conn.execute("UPDATE documents SET pdf_path = ? WHERE id = ?", (path, doc_id))
        except MonAEError as e:
            report["pdf_errors"] += 1
            report.setdefault("errors", []).append(f"PDF {full_ref} : {e}")
    report["invoices" if kind == "facture" else "avoirs"] += 1
    report["refs"].append((inv.get("invoiced_on") or "", full_ref))


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
              "pdf_errors": 0, "refs": []}
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
        set_job(step="Liste des factures…")
        invoices = []
        for page in api.pages(f"/firms/{firm_id}/invoices.json",
                              {"with_details": 1, "with_settlements": 1, "order": "billed", "sort": "asc"}):
            invoices += [i for i in page if not i.get("draft")]
        set_job(total=len(invoices), step=f"{len(invoices)} factures et avoirs à importer…")
        for n, inv in enumerate(invoices, 1):
            import_invoice(conn, api, inv, client_map, with_pdfs, report)
            conn.commit()
            if n % 5 == 0 or n == len(invoices):
                set_job(done=n, step=f"Facture {inv.get('full_invoice_ref') or inv.get('invoice_ref')} ({n}/{len(invoices)})")
        numbering = detect_numbering(report["refs"])
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
                             f"{report['avoirs']} avoirs.")
        conn.commit()
        set_job(status="done", step="Terminé", report=report, done=len(invoices))
    finally:
        conn.close()
