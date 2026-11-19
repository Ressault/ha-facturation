"""Règles métier : documents, numérotation, paiements, plateforme agréée."""
import os
import re
from datetime import date, timedelta

import db
import superpdp
from calc import d, money, totals, vat_breakdown
from pdf import build_pdf

KIND_PATTERN_KEYS = {"facture": "invoice", "avoir": "avoir", "devis": "devis"}

SELLER_KEYS = ["company_name", "company_trade_name", "address", "post_code", "city", "siren", "siret",
               "vat_number", "ape", "email", "phone", "iban", "bic", "rcs_rm", "insurance",
               "late_penalty", "recovery_fee", "discount_terms", "footer_note",
               "pdp_address_scheme", "pdp_address_value"]
CLIENT_KEYS = ["kind", "name", "siren", "vat_number", "address", "post_code", "city", "country",
               "email", "phone", "pdp_address_scheme", "pdp_address_value"]


class BusinessError(Exception):
    pass


# -- numérotation -------------------------------------------------------------

def format_number(pattern: str, year: int, n: int) -> str:
    def repl(m):
        return str(n).zfill(len(m.group(1)))
    out = pattern.replace("{AAAA}", f"{year:04d}").replace("{AA}", f"{year % 100:02d}")
    out = re.sub(r"\{(N+)\}", repl, out)
    return out


DEFAULT_PATTERNS = {"invoice": "F-{AAAA}-{NNNN}", "avoir": "AV-{AAAA}-{NNNN}", "devis": "D-{AAAA}-{NNN}"}


def counter_key(settings, key, year):
    pattern = settings.get(f"{key}_pattern") or DEFAULT_PATTERNS[key]
    return pattern, (f"{key}_next_{year}" if "{AA" in pattern else f"{key}_next")


def next_number(conn, kind: str, issue_date: str) -> str:
    s = db.get_settings(conn)
    key = KIND_PATTERN_KEYS[kind]
    year = int(issue_date[:4])
    pattern, ck = counter_key(s, key, year)
    n = int(s.get(ck) or 1)
    while True:
        number = format_number(pattern, year, n)
        if not conn.execute("SELECT 1 FROM documents WHERE number = ?", (number,)).fetchone():
            break
        n += 1
    db.set_settings({ck: n + 1}, conn)
    return number


def preview_number(conn, kind, issue_date):
    s = db.get_settings(conn)
    year = int(issue_date[:4])
    pattern, ck = counter_key(s, KIND_PATTERN_KEYS[kind], year)
    return format_number(pattern, year, int(s.get(ck) or 1))


# -- documents ----------------------------------------------------------------

def get_doc(conn, doc_id):
    doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not doc:
        raise BusinessError("Document introuvable.")
    lines = conn.execute("SELECT * FROM lines WHERE document_id = ? ORDER BY position", (doc_id,)).fetchall()
    return dict(doc), [dict(x) for x in lines]


def paid_amount(conn, doc_id):
    r = conn.execute("SELECT COALESCE(SUM(amount), 0) AS s FROM payments WHERE document_id = ?",
                     (doc_id,)).fetchone()
    return money(r["s"])


def credited_amount(conn, doc_id):
    r = conn.execute(
        "SELECT COALESCE(SUM(total_ttc), 0) AS s FROM documents WHERE avoir_of_id = ? AND kind = 'avoir' "
        "AND status != 'brouillon'", (doc_id,)).fetchone()
    return money(r["s"])


def save_document(conn, doc_id, form, lines):
    """Crée ou met à jour un document modifiable (brouillon ou devis)."""
    kind = form["kind"]
    if doc_id:
        doc, _ = get_doc(conn, doc_id)
        if doc["kind"] in ("facture", "avoir") and doc["status"] != "brouillon":
            raise BusinessError("Une facture émise ne peut plus être modifiée : faites un avoir.")
    clean = []
    for i, ln in enumerate(lines):
        if not (ln.get("designation") or "").strip():
            continue
        clean.append({
            "position": i, "designation": ln["designation"].strip(),
            "description": (ln.get("description") or "").strip(),
            "quantity": float(d(ln.get("quantity") or 1)), "unit": (ln.get("unit") or "u").strip(),
            "unit_price": float(d(ln.get("unit_price") or 0)), "vat_rate": float(d(ln.get("vat_rate") or 0)),
        })
    if not clean:
        raise BusinessError("Ajoutez au moins une ligne.")
    if not form.get("client_id"):
        raise BusinessError("Choisissez un client.")
    t = totals(clean)
    values = {
        "kind": kind, "client_id": int(form["client_id"]),
        "issue_date": form.get("issue_date") or date.today().isoformat(),
        "due_date": form.get("due_date") or None, "validity_date": form.get("validity_date") or None,
        "service_date": form.get("service_date") or None, "subject": form.get("subject") or None,
        "notes": form.get("notes") or None, "total_ht": float(t["ht"]), "total_tva": float(t["tva"]),
        "total_ttc": float(t["ttc"]),
        "avoir_of_id": int(form["avoir_of_id"]) if form.get("avoir_of_id") else None,
        "source_devis_id": int(form["source_devis_id"]) if form.get("source_devis_id") else None,
    }
    if doc_id:
        sets = ", ".join(f"{k} = ?" for k in values)
        conn.execute(f"UPDATE documents SET {sets} WHERE id = ?", (*values.values(), doc_id))
        conn.execute("DELETE FROM lines WHERE document_id = ?", (doc_id,))
    else:
        if kind == "devis":
            values["number"] = next_number(conn, "devis", values["issue_date"])
            values["status"] = "en_attente"
        cols = ", ".join(values)
        qs = ", ".join("?" for _ in values)
        doc_id = conn.execute(f"INSERT INTO documents ({cols}) VALUES ({qs})", tuple(values.values())).lastrowid
    for ln in clean:
        conn.execute(
            "INSERT INTO lines(document_id, position, designation, description, quantity, unit, unit_price, vat_rate)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, ln["position"], ln["designation"], ln["description"], ln["quantity"], ln["unit"],
             ln["unit_price"], ln["vat_rate"]))
    doc, lines_ = get_doc(conn, doc_id)
    if kind == "devis":
        render_pdf(conn, doc, lines_)
    return doc_id


def snapshot_seller(conn):
    s = db.get_settings(conn)
    return {k: s.get(k, "") for k in SELLER_KEYS}


def snapshot_client(conn, client_id):
    c = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not c:
        raise BusinessError("Client introuvable.")
    return {k: c[k] for k in CLIENT_KEYS}


def check_seller(seller):
    missing = [label for key, label in [
        ("company_name", "nom"), ("address", "adresse"), ("post_code", "code postal"), ("city", "ville"),
        ("siret", "SIRET"), ("vat_number", "n° de TVA intracommunautaire")] if not seller.get(key)]
    if missing:
        raise BusinessError("Complétez vos informations dans Réglages : " + ", ".join(missing) + ".")


def pdf_path_for(doc):
    year = (doc.get("issue_date") or date.today().isoformat())[:4]
    folder = os.path.join(db.ARCHIVE_DIR, year)
    os.makedirs(folder, exist_ok=True)
    name = (doc.get("number") or f"brouillon-{doc['id']}").replace("/", "-")
    return os.path.join(folder, f"{doc['kind']}-{name}.pdf")


def render_pdf(conn, doc, lines, final=False):
    seller = db.loads(doc.get("seller_snapshot")) or snapshot_seller(conn)
    client = db.loads(doc.get("client_snapshot")) or snapshot_client(conn, doc["client_id"])
    extra = {}
    if doc["kind"] == "avoir" and doc.get("avoir_of_id"):
        ref = conn.execute("SELECT number, issue_date FROM documents WHERE id = ?", (doc["avoir_of_id"],)).fetchone()
        if ref:
            extra = {"avoir_of_number": ref["number"], "avoir_of_date": ref["issue_date"]}
    if final or doc["kind"] == "devis":
        path = pdf_path_for(doc)
    else:
        path = os.path.join(db.DATA_DIR, "tmp", f"apercu-{doc['id']}.pdf")
        os.makedirs(os.path.dirname(path), exist_ok=True)
    build_pdf(doc, lines, seller, client, path, extra)
    if final or doc["kind"] == "devis":
        conn.execute("UPDATE documents SET pdf_path = ? WHERE id = ?", (path, doc["id"]))
    return path, seller, client, extra


def validate_document(conn, doc_id):
    """Émet une facture/un avoir : numéro définitif, figeage, PDF archivé."""
    doc, lines = get_doc(conn, doc_id)
    if doc["kind"] not in ("facture", "avoir") or doc["status"] != "brouillon":
        raise BusinessError("Ce document est déjà émis.")
    seller = snapshot_seller(conn)
    check_seller(seller)
    client = snapshot_client(conn, doc["client_id"])
    if client["kind"] == "pro" and client["country"] == "FR" and not client.get("siren"):
        raise BusinessError("Le SIREN du client est obligatoire sur les factures entre professionnels.")
    issue = doc["issue_date"] or date.today().isoformat()
    # chronologie : pas de facture antérieure à la dernière émise
    last = conn.execute(
        "SELECT MAX(issue_date) AS m FROM documents WHERE kind IN ('facture', 'avoir') AND status != 'brouillon' "
        "AND imported = 0").fetchone()["m"]
    if last and issue < last:
        raise BusinessError(f"La date ({issue}) est antérieure à la dernière facture émise ({last}) : "
                            "la numérotation doit rester chronologique.")
    number = next_number(conn, doc["kind"], issue)
    due = doc["due_date"]
    if doc["kind"] == "facture" and not due:
        s = db.get_settings(conn)
        due = (date.fromisoformat(issue) + timedelta(days=int(s.get("payment_days") or 30))).isoformat()
    conn.execute(
        "UPDATE documents SET number = ?, status = 'emise', issue_date = ?, due_date = ?, seller_snapshot = ?, "
        "client_snapshot = ?, validated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (number, issue, due, db.dumps(seller), db.dumps(client), doc_id))
    doc, lines = get_doc(conn, doc_id)
    render_pdf(conn, doc, lines, final=True)
    if doc.get("source_devis_id"):
        conn.execute("UPDATE documents SET status = 'facture' WHERE id = ?", (doc["source_devis_id"],))
    db.log(conn, "info", f"{doc['kind'].title()} {number} émise ({doc['total_ttc']:.2f} € TTC).")
    return doc_id


def duplicate_as(conn, src_id, kind, avoir=False):
    """Devis -> facture, ou facture -> avoir (copie des lignes)."""
    src, lines = get_doc(conn, src_id)
    today = date.today().isoformat()
    vals = {"kind": kind, "status": "brouillon", "client_id": src["client_id"], "issue_date": today,
            "subject": src["subject"], "service_date": src["service_date"], "notes": None,
            "total_ht": src["total_ht"], "total_tva": src["total_tva"], "total_ttc": src["total_ttc"]}
    if avoir:
        vals["avoir_of_id"] = src_id
        vals["subject"] = f"Avoir sur facture {src['number']}"
    if src["kind"] == "devis":
        vals["source_devis_id"] = src_id
    cols = ", ".join(vals)
    new_id = conn.execute(f"INSERT INTO documents ({cols}) VALUES ({', '.join('?' for _ in vals)})",
                          tuple(vals.values())).lastrowid
    for ln in lines:
        conn.execute(
            "INSERT INTO lines(document_id, position, designation, description, quantity, unit, unit_price, vat_rate)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (new_id, ln["position"], ln["designation"], ln["description"], ln["quantity"], ln["unit"],
             ln["unit_price"], ln["vat_rate"]))
    return new_id


def refresh_status(conn, doc_id):
    doc, _ = get_doc(conn, doc_id)
    if doc["kind"] == "devis" or doc["status"] == "brouillon":
        return
    paid = paid_amount(conn, doc_id)
    if doc["kind"] == "avoir":
        status = "rembourse" if paid >= money(doc["total_ttc"]) and paid > 0 else "emise"
    else:
        due = money(doc["total_ttc"]) - credited_amount(conn, doc_id)
        if paid >= due and due >= 0:
            status = "payee" if paid > 0 else "annulee"
        elif paid > 0:
            status = "partielle"
        else:
            status = "emise"
    conn.execute("UPDATE documents SET status = ? WHERE id = ?", (status, doc_id))


def add_payment(conn, doc_id, pay_date, amount, method):
    doc, lines = get_doc(conn, doc_id)
    if doc["kind"] not in ("facture", "avoir") or doc["status"] == "brouillon":
        raise BusinessError("Émettez d'abord le document.")
    amount = money(amount)
    if amount <= 0:
        raise BusinessError("Montant invalide.")
    pid = conn.execute("INSERT INTO payments(document_id, date, amount, method) VALUES (?, ?, ?, ?)",
                       (doc_id, pay_date, float(amount), method)).lastrowid
    refresh_status(conn, doc_id)
    return pid


def delete_payment(conn, payment_id):
    p = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if not p:
        return
    if p["pdp_reported"]:
        raise BusinessError("Cet encaissement a déjà été transmis à la plateforme agréée.")
    conn.execute("DELETE FROM payments WHERE id = ?", (payment_id,))
    refresh_status(conn, p["document_id"])


# -- plateforme agréée --------------------------------------------------------

def pdp_client(conn):
    s = db.get_settings(conn)
    return superpdp.Client(s.get("pdp_client_id"), s.get("pdp_client_secret"), s.get("pdp_endpoint"))


def pdp_configured(conn):
    s = db.get_settings(conn)
    return bool(s.get("pdp_client_id") and s.get("pdp_client_secret"))


def ereporting_active(conn, on_date=None):
    s = db.get_settings(conn)
    start = s.get("ereporting_start") or "2027-09-01"
    return (on_date or date.today().isoformat()) >= start


def send_to_pdp(conn, doc_id):
    doc, lines = get_doc(conn, doc_id)
    if doc["status"] == "brouillon":
        raise BusinessError("Émettez d'abord la facture.")
    if doc.get("pdp_id"):
        raise BusinessError("Déjà transmise à la plateforme agréée.")
    client = db.loads(doc["client_snapshot"])
    seller = db.loads(doc["seller_snapshot"])
    api = pdp_client(conn)
    if client.get("kind") != "pro" or client.get("country", "FR") != "FR":
        # B2C / international : pas de facture électronique, e-reporting de la transaction
        if ereporting_active(conn, doc["issue_date"]) and not doc["ereport_tx_sent"] and doc["kind"] == "facture":
            api.b2c_transactions([superpdp.b2c_transaction(doc, lines)])
            conn.execute("UPDATE documents SET ereport_tx_sent = 1 WHERE id = ?", (doc_id,))
            db.log(conn, "info", f"E-reporting de la facture {doc['number']} transmis.")
        return None
    extra = {"paid": paid_amount(conn, doc_id)}
    if doc["kind"] == "avoir" and doc.get("avoir_of_id"):
        ref = conn.execute("SELECT number, issue_date FROM documents WHERE id = ?", (doc["avoir_of_id"],)).fetchone()
        extra.update({"avoir_of_number": ref["number"], "avoir_of_date": ref["issue_date"]})
    en = superpdp.build_en_invoice(doc, lines, seller, client, extra)
    with open(doc["pdf_path"], "rb") as f:
        base_pdf = f.read()
    try:
        facturx = api.to_facturx(db.dumps(en), base_pdf)
        res = api.send(facturx, doc["number"])
    except superpdp.PDPError as e:
        conn.execute("UPDATE documents SET pdp_error = ? WHERE id = ?", (str(e), doc_id))
        db.log(conn, "error", f"Envoi {doc['number']} : {e}")
        raise BusinessError(str(e))
    fx_path = doc["pdf_path"].replace(".pdf", "-facturx.pdf")
    with open(fx_path, "wb") as f:
        f.write(facturx)
    conn.execute("UPDATE documents SET pdp_id = ?, pdp_status = 'fr:200', pdp_status_text = ?, pdp_error = NULL, "
                 "pdf_path = ? WHERE id = ?", (res.get("id"), superpdp.STATUS_LABELS["fr:200"], fx_path, doc_id))
    db.log(conn, "info", f"{doc['number']} déposée sur la plateforme agréée (id {res.get('id')}).")
    # encaissements déjà saisis avant l'envoi
    report_payments(conn, doc_id, api)
    return res.get("id")


def report_payments(conn, doc_id, api=None):
    doc, lines = get_doc(conn, doc_id)
    if doc["kind"] != "facture":
        return
    pending = conn.execute("SELECT * FROM payments WHERE document_id = ? AND pdp_reported = 0", (doc_id,)).fetchall()
    if not pending:
        return
    client = db.loads(doc["client_snapshot"]) or {}
    api = api or pdp_client(conn)
    bd = vat_breakdown(lines)
    for p in pending:
        try:
            if doc.get("pdp_id"):
                api.post_event(superpdp.payment_event(doc["pdp_id"], p["date"], p["amount"], bd))
            elif client.get("kind") != "pro" and ereporting_active(conn, p["date"]):
                api.b2c_payments([superpdp.b2c_payment(p["date"], p["amount"], bd)])
            else:
                continue
            conn.execute("UPDATE payments SET pdp_reported = 1, pdp_error = NULL WHERE id = ?", (p["id"],))
        except superpdp.PDPError as e:
            conn.execute("UPDATE payments SET pdp_error = ? WHERE id = ?", (str(e), p["id"]))
            db.log(conn, "error", f"Encaissement {doc['number']} : {e}")


def sync_statuses(conn):
    """Met à jour les statuts des factures déposées et transmet les encaissements en attente."""
    api = pdp_client(conn)
    docs = conn.execute(
        "SELECT id, pdp_id, pdp_last_event_id FROM documents WHERE pdp_id IS NOT NULL "
        "AND COALESCE(pdp_status, '') NOT IN ('fr:212', 'fr:210', 'fr:213', 'fr:501')").fetchall()
    for d_ in docs:
        try:
            res = api.events(d_["pdp_id"], d_["pdp_last_event_id"])
        except superpdp.PDPError as e:
            db.log(conn, "error", f"Suivi facture {d_['id']} : {e}")
            continue
        events = res.get("data") or []
        if events:
            last = events[-1]
            code = last.get("status_code")
            conn.execute("UPDATE documents SET pdp_status = ?, pdp_status_text = ?, pdp_last_event_id = ? WHERE id = ?",
                         (code, superpdp.STATUS_LABELS.get(code, last.get("status_text") or code),
                          last.get("id"), d_["id"]))
    for row in conn.execute("SELECT DISTINCT document_id FROM payments WHERE pdp_reported = 0").fetchall():
        report_payments(conn, row["document_id"], api)


def fetch_inbox(conn):
    """Importe les factures fournisseurs reçues sur la plateforme agréée."""
    api = pdp_client(conn)
    s = db.get_settings(conn)
    after = int(s.get("last_inbox_id") or 0)
    n = 0
    while True:
        res = api.received(after)
        items = res.get("data") or []
        for it in items:
            after = max(after, int(it["id"]))
            if conn.execute("SELECT 1 FROM purchases WHERE pdp_id = ?", (it["id"],)).fetchone():
                continue
            p = superpdp.parse_received(it)
            sign = -1 if p.pop("type_code", 380) in (381, "381") else 1
            folder = os.path.join(db.ARCHIVE_DIR, "achats")
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, f"pdp-{it['id']}.pdf")
            try:
                content = api.download(it["id"], "factur-x")
                with open(path, "wb") as f:
                    f.write(content)
            except superpdp.PDPError:
                path = None
            conn.execute(
                "INSERT INTO purchases(supplier, supplier_siren, invoice_number, invoice_date, description, total_ht,"
                " total_tva, total_ttc, category, deduction_date, source, pdp_id, file_path)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'biens_services', NULL, 'pdp', ?, ?)",
                (p["supplier"], p["supplier_siren"], p["invoice_number"], p["invoice_date"], p["description"],
                 sign * p["total_ht"], sign * p["total_tva"], sign * p["total_ttc"], it["id"], path))
            n += 1
        if not res.get("has_after") or not items:
            break
    db.set_settings({"last_inbox_id": after}, conn)
    if n:
        db.log(conn, "info", f"{n} facture(s) fournisseur reçue(s).")
    return n


def test_connection(conn):
    api = pdp_client(conn)
    me = api.me()
    out = {"company": me}
    s = db.get_settings(conn)
    siren = (s.get("siren") or (s.get("siret") or "")[:9]).replace(" ", "")
    if siren:
        try:
            out["directory"] = api.directory_entries(siren)
            addr = pick_address(out["directory"])
            if addr:
                db.set_settings({"pdp_address_scheme": addr[0], "pdp_address_value": addr[1]}, conn)
                out["address"] = addr
        except superpdp.PDPError as e:
            out["directory_error"] = str(e)
    return out


def pick_address(res):
    """Extrait (scheme, value) d'une réponse d'annuaire, quel que soit son format exact."""
    items = res.get("data") if isinstance(res, dict) else res
    if isinstance(items, dict):
        items = [items]
    for it in items or []:
        if not isinstance(it, dict):
            continue
        for key in ("electronic_address", "address", "routing_address"):
            v = it.get(key)
            if isinstance(v, dict) and v.get("value"):
                return v.get("scheme") or "0225", v["value"]
            if isinstance(v, str) and v:
                return it.get("scheme") or "0225", v
        if it.get("identifier") and (it.get("scheme") or it.get("identifier_scheme")):
            return it.get("scheme") or it.get("identifier_scheme"), it["identifier"]
    return None


def lookup_siren(conn, siren):
    api = superpdp.Client("x", "x", db.get_settings(conn).get("pdp_endpoint"))
    out = {}
    res = api.lookup_company(siren)
    items = res.get("data") if isinstance(res, dict) else res
    if items:
        c = items[0]
        addr = c.get("address") or c.get("postal_address") or {}
        out = {
            "name": c.get("formal_name") or c.get("name") or "",
            "address": addr.get("address_line1") or addr.get("street") or "",
            "post_code": addr.get("post_code") or addr.get("postal_code") or "",
            "city": addr.get("city") or "",
        }
    try:
        a = pick_address(api.directory_entries(siren))
        if a:
            out["pdp_address_scheme"], out["pdp_address_value"] = a
    except superpdp.PDPError:
        pass
    return out


def vat_number_from_siren(siren: str) -> str:
    siren = re.sub(r"\D", "", siren or "")
    if len(siren) != 9:
        return ""
    key = (12 + 3 * (int(siren) % 97)) % 97
    return f"FR{key:02d}{siren}"
