"""Règles métier : documents, numérotation, paiements, plateforme agréée."""
import os
import re
from datetime import date, timedelta

import db
import superpdp
from decimal import Decimal

from calc import d, money, totals, vat_breakdown
from pdf import build_pdf

KIND_PATTERN_KEYS = {"facture": "invoice", "avoir": "avoir", "devis": "devis"}

SELLER_KEYS = ["company_name", "company_trade_name", "address", "post_code", "city", "siren", "siret",
               "vat_number", "ape", "email", "phone", "website", "iban", "bic", "rcs_rm", "insurance",
               "late_penalty", "recovery_fee", "discount_terms", "footer_note", "logo_path",
               "default_penalty_rate", "default_discount_rate",
               "pdp_address_scheme", "pdp_address_value"]
CLIENT_KEYS = ["kind", "name", "siren", "vat_number", "address", "post_code", "city", "country",
               "email", "phone", "pdp_address_scheme", "pdp_address_value"]


class BusinessError(Exception):
    pass


# -- numérotation -------------------------------------------------------------

def format_number(pattern: str, year: int, n: int, month: int = 1) -> str:
    def repl(m):
        return str(n).zfill(len(m.group(1)))
    out = (pattern.replace("{AAAA}", f"{year:04d}").replace("{AA}", f"{year % 100:02d}")
           .replace("{MM}", f"{month:02d}"))
    out = re.sub(r"\{(N+)\}", repl, out)
    return out


DEFAULT_PATTERNS = {"invoice": "F-{AAAA}-{NNNN}", "avoir": "AV-{AAAA}-{NNNN}", "devis": "D-{AAAA}-{NNN}"}


def counter_key(settings, key, year):
    pattern = settings.get(f"{key}_pattern") or DEFAULT_PATTERNS[key]
    yearly = "{AA" in pattern and settings.get(f"{key}_reset_yearly", "1") != "0"
    return pattern, (f"{key}_next_{year}" if yearly else f"{key}_next")


def next_number(conn, kind: str, issue_date: str) -> str:
    s = db.get_settings(conn)
    key = KIND_PATTERN_KEYS[kind]
    year, month = int(issue_date[:4]), int(issue_date[5:7])
    pattern, ck = counter_key(s, key, year)
    n = int(s.get(ck) or 1)
    while True:
        number = format_number(pattern, year, n, month)
        if not conn.execute("SELECT 1 FROM documents WHERE number = ?", (number,)).fetchone():
            break
        n += 1
    db.set_settings({ck: n + 1}, conn)
    return number


def preview_number(conn, kind, issue_date):
    s = db.get_settings(conn)
    year, month = int(issue_date[:4]), int(issue_date[5:7])
    pattern, ck = counter_key(s, KIND_PATTERN_KEYS[kind], year)
    return format_number(pattern, year, int(s.get(ck) or 1), month)


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


from calc import PAYMENT_TERMS  # noqa: E402
PAYMENT_TERMS_LABEL = dict(PAYMENT_TERMS)
LINE_KINDS = ("item", "title", "subtotal", "comment", "page", "blank")


def _end_of_month(dt):
    nxt = date(dt.year + (dt.month == 12), dt.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def due_date_for(issue: str, terms: str) -> str:
    """Date d'échéance selon les conditions de règlement (codes MonAE : 30-inv, 30fm10-inv…)."""
    dt = date.fromisoformat(issue[:10])
    code = (terms or "30-inv").replace("-inv", "")
    m = re.fullmatch(r"(\d+)(fm)?(\d+)?", code)
    if not m:
        return (dt + timedelta(days=30)).isoformat()
    days, fm, day = int(m.group(1)), m.group(2), m.group(3)
    dt = dt + timedelta(days=days)
    if fm:
        dt = _end_of_month(dt)
        if day:
            nxt = dt + timedelta(days=1)
            dt = date(nxt.year, nxt.month, min(int(day), 28))
    return dt.isoformat()


def clean_lines(lines, default_nature="bnc"):
    clean = []
    for i, ln in enumerate(lines):
        kind = (ln.get("kind") or "item").strip()
        if kind not in LINE_KINDS:
            kind = "item"
        title = (ln.get("designation") or "").strip()
        if kind in ("title", "comment") and not title:
            continue
        if kind == "item" and not title:
            continue
        item = kind == "item"
        clean.append({
            "position": len(clean), "kind": kind,
            "optional": 1 if item and str(ln.get("optional") or "0") in ("1", "on", "true") else 0,
            "designation": title or ("Sous-total" if kind == "subtotal" else ""),
            "description": (ln.get("description") or "").strip() if item else "",
            "quantity": float(d(ln.get("quantity") or 1)) if item else 0.0,
            "unit": (ln.get("unit") or "u").strip() if item else "",
            "unit_price": float(d(ln.get("unit_price") or 0)) if item else 0.0,
            "vat_rate": float(d(ln.get("vat_rate") or 0)) if item else 0.0,
            "nature": (ln.get("nature") or default_nature) if item else None,
        })
    return clean


def insert_lines(conn, doc_id, lines):
    for ln in lines:
        conn.execute(
            "INSERT INTO lines(document_id, position, kind, designation, description, quantity, unit, unit_price, "
            "vat_rate, nature, optional) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, ln["position"], ln.get("kind") or "item", ln["designation"], ln.get("description") or "",
             ln["quantity"], ln.get("unit") or "", ln["unit_price"], ln["vat_rate"], ln.get("nature"),
             1 if ln.get("optional") else 0))


def _rate(v):
    v = (str(v) if v is not None else "").replace(",", ".").replace("%", "").strip()
    return float(v) if v else None


def save_document(conn, doc_id, form, lines):
    """Crée ou met à jour un document modifiable (brouillon ou devis)."""
    kind = form["kind"]
    if doc_id:
        doc, _ = get_doc(conn, doc_id)
        if doc["kind"] in ("facture", "avoir") and doc["status"] != "brouillon":
            raise BusinessError("Une facture émise ne peut plus être modifiée : supprimez-la (un avoir sera créé).")
    s = db.get_settings(conn)
    clean = clean_lines(lines, s.get("default_nature") or "bnc")
    if not any(ln["kind"] == "item" for ln in clean):
        raise BusinessError("Ajoutez au moins une ligne de prestation.")
    if not form.get("client_id"):
        raise BusinessError("Choisissez un client.")
    issue = form.get("issue_date") or date.today().isoformat()
    t = totals(clean, issue)
    terms = form.get("payment_terms") or s.get("default_payment_terms") or "30-inv"
    values = {
        "kind": kind, "client_id": int(form["client_id"]), "issue_date": issue,
        "payment_terms": terms,
        "due_date": (form.get("due_date") or due_date_for(issue, terms)) if kind == "facture" else None,
        "validity_date": form.get("validity_date") or None,
        "penalty_rate": _rate(form.get("penalty_rate")), "discount_rate": _rate(form.get("discount_rate")),
        "service_date": form.get("service_date") or None, "subject": form.get("subject") or None,
        "notes": form.get("notes") or None, "total_ht": float(t["ht"]), "total_tva": float(t["tva"]),
        "total_ttc": float(t["ttc"]),
        "avoir_of_id": int(form["avoir_of_id"]) if form.get("avoir_of_id") else None,
        "source_devis_id": int(form["source_devis_id"]) if form.get("source_devis_id") else None,
    }
    if form.get("auto_due") == "1" and kind == "facture":
        values["due_date"] = due_date_for(issue, terms)
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
    insert_lines(conn, doc_id, clean)
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
        "SELECT MAX(issue_date) AS m FROM documents WHERE kind IN ('facture', 'avoir') AND status != 'brouillon'"
    ).fetchone()["m"]
    if last and issue < last:
        raise BusinessError(f"La date ({issue}) est antérieure à la dernière facture émise ({last}) : "
                            "la numérotation doit rester chronologique.")
    number = next_number(conn, doc["kind"], issue)
    due = doc["due_date"]
    if doc["kind"] == "facture" and (not due or due < issue):
        s = db.get_settings(conn)
        due = due_date_for(issue, doc.get("payment_terms") or s.get("default_payment_terms") or "30-inv")
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
            "total_ht": src["total_ht"], "total_tva": src["total_tva"], "total_ttc": src["total_ttc"],
            "payment_terms": src.get("payment_terms"), "penalty_rate": src.get("penalty_rate"),
            "discount_rate": src.get("discount_rate")}
    if avoir:
        vals["avoir_of_id"] = src_id
        vals["subject"] = f"Avoir sur facture {src['number']}"
    if src["kind"] == "devis" and kind == "facture":
        vals["source_devis_id"] = src_id
        # les lignes en option ne passent pas sur la facture
        lines = [dict(ln, position=i) for i, ln in enumerate(x for x in lines if not x.get("optional"))]
    if kind != "devis":
        lines = [dict(ln, optional=0) for ln in lines]
    t = totals(lines, today)  # recalcul à la date du nouveau document (méthode d'arrondi en vigueur)
    vals.update(total_ht=float(t["ht"]), total_tva=float(t["tva"]), total_ttc=float(t["ttc"]))
    cols = ", ".join(vals)
    new_id = conn.execute(f"INSERT INTO documents ({cols}) VALUES ({', '.join('?' for _ in vals)})",
                          tuple(vals.values())).lastrowid
    insert_lines(conn, new_id, lines)
    return new_id


def cancel_invoice(conn, doc_id):
    """« Supprimer » une facture émise : crée et émet automatiquement l'avoir total correspondant."""
    doc, lines = get_doc(conn, doc_id)
    if doc["kind"] != "facture" or doc["status"] == "brouillon":
        raise BusinessError("Seule une facture émise s'annule par avoir.")
    if doc.get("cancelled_by_id"):
        raise BusinessError("Cette facture est déjà annulée.")
    remaining = money(doc["total_ttc"]) - credited_amount(conn, doc_id)
    if remaining <= 0:
        raise BusinessError("Cette facture est déjà entièrement couverte par des avoirs.")
    avoir_id = duplicate_as(conn, doc_id, "avoir", avoir=True)
    if remaining < money(doc["total_ttc"]):
        # avoirs partiels déjà émis : un seul avoir du solde, au taux principal
        conn.execute("DELETE FROM lines WHERE document_id = ?", (avoir_id,))
        bd = vat_breakdown(lines)
        rate = max(bd, key=lambda x: x[1])[0] if bd else 20
        base = money(remaining * 100 / (100 + d(rate)))
        insert_lines(conn, avoir_id, [{"position": 0, "kind": "item", "designation": f"Solde de la facture {doc['number']}",
                                       "description": "", "quantity": 1.0, "unit": "u", "unit_price": float(base),
                                       "vat_rate": float(rate), "nature": None}])
        t = totals(get_doc(conn, avoir_id)[1])
        conn.execute("UPDATE documents SET total_ht=?, total_tva=?, total_ttc=? WHERE id=?",
                     (float(t["ht"]), float(t["tva"]), float(t["ttc"]), avoir_id))
    conn.execute("UPDATE documents SET subject = ?, issue_date = ? WHERE id = ?",
                 (f"Annulation de la facture {doc['number']}", max(date.today().isoformat(), doc["issue_date"]),
                  avoir_id))
    validate_document(conn, avoir_id)
    conn.execute("UPDATE documents SET cancelled_by_id = ? WHERE id = ?", (avoir_id, doc_id))
    # si la facture avait été encaissée, l'avoir est à rembourser ; sinon il solde la facture
    refresh_status(conn, doc_id)
    db.log(conn, "info", f"Facture {doc['number']} annulée par avoir.")
    return avoir_id


def neighbours(conn, doc):
    """Documents précédent / suivant du même type (ordre chronologique)."""
    key = (doc["issue_date"] or "", doc["id"])
    prev = conn.execute(
        "SELECT id, number FROM documents WHERE kind = ? AND (COALESCE(issue_date,'') < ? OR "
        "(COALESCE(issue_date,'') = ? AND id < ?)) ORDER BY COALESCE(issue_date,'') DESC, id DESC LIMIT 1",
        (doc["kind"], key[0], key[0], key[1])).fetchone()
    nxt = conn.execute(
        "SELECT id, number FROM documents WHERE kind = ? AND (COALESCE(issue_date,'') > ? OR "
        "(COALESCE(issue_date,'') = ? AND id > ?)) ORDER BY COALESCE(issue_date,'') ASC, id ASC LIMIT 1",
        (doc["kind"], key[0], key[0], key[1])).fetchone()
    return prev, nxt


# -- fournisseurs et achats récurrents ---------------------------------------------

def add_months(iso, months):
    dt = date.fromisoformat(iso[:10])
    m = dt.month - 1 + months
    y = dt.year + m // 12
    m = m % 12 + 1
    last = _end_of_month(date(y, m, 1)).day
    return date(y, m, min(dt.day, last)).isoformat()


def generate_recurring(conn, today=None):
    """Crée les dépenses des achats récurrents arrivés à échéance."""
    today = today or date.today().isoformat()
    n = 0
    for r in conn.execute("SELECT * FROM recurring_purchases WHERE active = 1 AND next_date <= ?", (today,)).fetchall():
        nxt, occ = r["next_date"], r["occurrences"]
        sup = conn.execute("SELECT name FROM suppliers WHERE id = ?", (r["supplier_id"],)).fetchone()
        while nxt <= today and (not r["max_occurrences"] or occ < r["max_occurrences"]):
            conn.execute(
                "INSERT INTO purchases(supplier, supplier_id, invoice_date, description, total_ht, total_tva, total_ttc,"
                " category, nature, method, paid_date, deduction_date, source, recurring_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'recurrent', ?)",
                (sup["name"] if sup else "Fournisseur", r["supplier_id"], nxt, r["title"], r["total_ht"], r["total_tva"],
                 r["total_ttc"], r["category"], r["nature"], r["method"], nxt if r["auto_paid"] else None,
                 nxt if r["auto_paid"] else None, r["id"]))
            occ += 1
            n += 1
            nxt = add_months(nxt, r["every_months"] or 1)
        active = 0 if r["max_occurrences"] and occ >= r["max_occurrences"] else 1
        conn.execute("UPDATE recurring_purchases SET next_date = ?, occurrences = ?, active = ? WHERE id = ?",
                     (nxt, occ, active, r["id"]))
    if n:
        db.log(conn, "info", f"{n} dépense(s) récurrente(s) créée(s).")
    return n


def delete_clients(conn, client_ids):
    """Supprime les clients sans document ; ceux qui en ont sont retirés des listes (les factures restent)."""
    out = {"deleted": 0, "archived": 0}
    for cid in client_ids:
        if conn.execute("SELECT 1 FROM documents WHERE client_id = ?", (cid,)).fetchone():
            conn.execute("UPDATE clients SET archived = 1 WHERE id = ?", (cid,))
            out["archived"] += 1
        elif conn.execute("DELETE FROM clients WHERE id = ?", (cid,)).rowcount:
            out["deleted"] += 1
    return out


def move_clients_to_suppliers(conn, client_ids):
    moved = 0
    for cid in client_ids:
        c = conn.execute("SELECT * FROM clients WHERE id = ?", (cid,)).fetchone()
        if not c:
            continue
        if conn.execute("SELECT 1 FROM documents WHERE client_id = ?", (cid,)).fetchone():
            continue  # déjà facturé : c'est bien un client
        sup = conn.execute("SELECT id FROM suppliers WHERE lower(name) = lower(?)", (c["name"],)).fetchone()
        if not sup:
            conn.execute("INSERT INTO suppliers(name, siren, vat_number, address, post_code, city, country, email, phone,"
                         " notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (c["name"], c["siren"], c["vat_number"], c["address"], c["post_code"], c["city"],
                          c["country"] or "FR", c["email"], c["phone"], c["notes"]))
        conn.execute("DELETE FROM clients WHERE id = ?", (cid,))
        moved += 1
    return moved


def detect_recurring(conn, today=None):
    """Repère les abonnements dans l'historique des dépenses (même fournisseur, même montant, à intervalle
    régulier : mensuel, trimestriel, annuel) et crée les achats récurrents correspondants s'ils n'existent pas."""
    today = today or date.today()
    rows = conn.execute(
        "SELECT p.*, COALESCE(s.name, p.supplier) AS sname FROM purchases p LEFT JOIN suppliers s ON s.id = p.supplier_id "
        "WHERE p.invoice_date IS NOT NULL AND p.total_ttc > 0 ORDER BY p.invoice_date, p.id").fetchall()
    groups = {}
    for r in rows:
        who = r["supplier_id"] or (r["sname"] or "").strip().lower()
        if not who:
            continue
        groups.setdefault((who, round(r["total_ttc"], 2)), []).append(r)
    created = 0
    for (who, ttc), items in groups.items():
        dates = [date.fromisoformat(i["invoice_date"][:10]) for i in items]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        months = None
        for m, lo, hi, need in ((1, 26, 35, 2), (3, 84, 97, 2), (12, 355, 376, 1)):
            if len(gaps) >= need and all(lo <= g <= hi for g in gaps[-need:]):
                months = m
                break
        if not months:
            continue
        last = items[-1]
        if (today - dates[-1]).days > months * 31 + 25:
            continue  # abonnement arrêté
        sid = last["supplier_id"] or supplier_id_for(conn, last["sname"])
        if conn.execute("SELECT 1 FROM recurring_purchases WHERE supplier_id = ? AND abs(total_ttc - ?) < 0.01",
                        (sid, ttc)).fetchone():
            continue
        nxt = add_months(last["invoice_date"], months)
        rid = conn.execute(
            "INSERT INTO recurring_purchases(supplier_id, title, total_ht, total_tva, total_ttc, category, nature, method,"
            " every_months, next_date, occurrences, auto_paid, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (sid, (last["description"] or last["sname"] or "Abonnement").strip(), last["total_ht"], last["total_tva"],
             last["total_ttc"], last["category"] or "biens_services", last["nature"] or "services", last["method"],
             months, nxt, len(items), 1 if all(i["paid_date"] for i in items) else 0)).lastrowid
        conn.execute(f"UPDATE purchases SET recurring_id = ? WHERE id IN ({','.join(str(i['id']) for i in items)})",
                     (rid,))
        created += 1
    if created:
        db.log(conn, "info", f"{created} abonnement(s) détecté(s) dans les dépenses.")
    return created


def supplier_id_for(conn, name, siren=None):
    name = (name or "").strip() or "Fournisseur"
    row = None
    if siren:
        row = conn.execute("SELECT id FROM suppliers WHERE siren = ?", (siren,)).fetchone()
    row = row or conn.execute("SELECT id FROM suppliers WHERE lower(name) = lower(?)", (name,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO suppliers(name, siren) VALUES (?, ?)", (name, siren)).lastrowid


# -- tâches -------------------------------------------------------------------

def tasks(conn, today=None):
    """Tâches à cocher : TVA du mois précédent, URSSAF du trimestre précédent."""
    from calc import compute_period, compute_urssaf, previous_period, previous_quarter, quarter_bounds
    today = today or date.today()
    s = db.get_settings(conn)
    out = []
    done = {r["task"] for r in conn.execute("SELECT task FROM tasks_done").fetchall()}
    first = conn.execute("SELECT MIN(issue_date) m FROM documents WHERE kind='facture' AND status!='brouillon'"
                         ).fetchone()["m"]
    if not first:
        return out
    period = previous_period(today)
    declared = conn.execute("SELECT 1 FROM declarations WHERE period = ? AND declared_at IS NOT NULL",
                            (period,)).fetchone()
    if f"tva:{period}" not in done and not declared and first[:7] <= period:
        due_day = int(s.get("ca3_due_day") or 19)
        due = date(today.year, today.month, min(due_day, 28))
        r = compute_period(conn, period)
        out.append({"id": f"tva:{period}", "kind": "tva", "period": period, "due": due.isoformat(),
                    "days": (due - today).days, "amount": r["l28"], "credit": r["l27"]})
    if s.get("urssaf_period") == "trimestre":
        q = previous_quarter(today)
        start, end = quarter_bounds(q)
        due = date.fromisoformat(end) + timedelta(days=30)
        if f"urssaf:{q}" not in done and first < end and today.isoformat() < (due + timedelta(days=45)).isoformat():
            u = compute_urssaf(conn, start, end, s)
            out.append({"id": f"urssaf:{q}", "kind": "urssaf", "period": q, "due": due.isoformat(),
                        "days": (due - today).days, "amount": u["total"], "ca": sum(u["ca"][k] for k in ("vente", "bic", "bnc"))})
    else:
        if f"urssaf:{period}" not in done and first[:7] <= period:
            y, m = int(period[:4]), int(period[5:7])
            start = f"{period}-01"
            end = f"{y + (m == 12):04d}-{m % 12 + 1:02d}-01"
            due = _end_of_month(date.fromisoformat(end))
            u = compute_urssaf(conn, start, end, s)
            out.append({"id": f"urssaf:{period}", "kind": "urssaf", "period": period, "due": due.isoformat(),
                        "days": (due - today).days, "amount": u["total"],
                        "ca": sum(u["ca"][k] for k in ("vente", "bic", "bnc"))})
    return out


# -- cotisations URSSAF : montant demandé, règlements, retard ---------------------

def urssaf_bounds(period):
    """(début, fin exclue, date limite) d'une période URSSAF 'YYYY-Tn' ou 'YYYY-MM'."""
    from calc import month_bounds, quarter_bounds
    start, end = quarter_bounds(period) if "-T" in period else month_bounds(period)
    e = date.fromisoformat(end)
    deadline = (date(e.year + (e.month == 12), e.month % 12 + 1, 1) - timedelta(days=1)).isoformat()
    return start, end, deadline


def urssaf_estimate(conn, period):
    from calc import compute_urssaf
    start, end, _ = urssaf_bounds(period)
    return compute_urssaf(conn, start, end, db.get_settings(conn))


def urssaf_set_due(conn, period, amount, declared_ca=None):
    conn.execute("INSERT INTO urssaf_dues(period, amount, declared_ca) VALUES (?, ?, ?) ON CONFLICT(period) DO UPDATE "
                 "SET amount = excluded.amount, declared_ca = COALESCE(excluded.declared_ca, urssaf_dues.declared_ca)",
                 (period, float(money(amount)), declared_ca))


def urssaf_add_payment(conn, period, pay_date, amount, reference=None):
    amount = money(amount)
    if amount <= 0:
        raise BusinessError("Montant invalide.")
    conn.execute("INSERT INTO urssaf_payments(period, date, amount, reference) VALUES (?, ?, ?, ?)",
                 (period, pay_date, float(amount), (reference or "").strip() or None))


def urssaf_ledger(conn, today=None):
    """Une ligne par période déclarée : cotisations demandées, réglées, reste, retard."""
    today = (today or date.today()).isoformat()
    periods = {r["period"] for r in conn.execute("SELECT period FROM urssaf_dues").fetchall()}
    periods |= {r["period"] for r in conn.execute("SELECT DISTINCT period FROM urssaf_payments").fetchall()}
    out = []
    for p in sorted(periods, reverse=True):
        due = conn.execute("SELECT * FROM urssaf_dues WHERE period = ?", (p,)).fetchone()
        paid = money(conn.execute("SELECT COALESCE(SUM(amount), 0) s FROM urssaf_payments WHERE period = ?",
                                  (p,)).fetchone()["s"])
        amount = money(due["amount"]) if due else Decimal("0")
        _, _, deadline = urssaf_bounds(p)
        rest = max(Decimal("0"), amount - paid)
        out.append({"period": p, "due": float(amount), "paid": float(paid), "rest": float(rest),
                    "deadline": deadline, "late": rest > 0 and deadline < today,
                    "declared_ca": due["declared_ca"] if due else None})
    return out


def urssaf_arrears(conn, today=None):
    rows = [r for r in urssaf_ledger(conn, today) if r["late"]]
    return {"total": sum(r["rest"] for r in rows), "periods": rows}


def match_transfer(conn, label, amount=None):
    """Factures non soldées qui correspondent à un virement collé depuis la banque (libellé, montant)."""
    label = (label or "").strip()
    amount = money(amount) if amount not in (None, "") else None
    if not amount:
        m = re.findall(r"[+]?\s?(\d{1,3}(?:[ \u202f.]\d{3})*(?:,\d{2})|\d+\.\d{2})\s?(?:€|EUR)?", label)
        if m:
            amount = money(m[-1].replace(" ", "").replace("\u202f", "").replace(".", "").replace(",", ".")
                           if "," in m[-1] else m[-1])
    words = {w for w in re.findall(r"[A-Za-zÀ-ÿ0-9]{3,}", label.lower())}
    nums = set(re.findall(r"\d+", label))
    out = []
    for r in conn.execute(
            "SELECT d.*, cl.name AS client_name, (SELECT COALESCE(SUM(amount),0) FROM payments WHERE document_id=d.id) "
            "AS paid FROM documents d LEFT JOIN clients cl ON cl.id = d.client_id WHERE d.kind = 'facture' "
            "AND d.status IN ('emise', 'partielle')").fetchall():
        rest = money(r["total_ttc"]) - money(r["paid"]) - credited_amount(conn, r["id"])
        if rest <= 0:
            continue
        score, why = 0, []
        num = str(r["number"] or "")
        if num and (num in label or num.split("-")[-1].lstrip("0") in nums):
            score += 5
            why.append("n° de facture")
        name_words = {w for w in re.findall(r"[A-Za-zÀ-ÿ0-9]{3,}", (r["client_name"] or "").lower())} - {"sarl", "sas", "eurl", "sasu"}
        if name_words and name_words & words:
            score += 3
            why.append("client")
        if amount and abs(rest - amount) < Decimal("0.01"):
            score += 4
            why.append("montant exact")
        elif amount and abs(money(r["total_ttc"]) - amount) < Decimal("0.01"):
            score += 3
            why.append("montant de la facture")
        if score:
            out.append({"doc": dict(r), "rest": float(rest), "score": score, "why": why})
    out.sort(key=lambda x: (-x["score"], x["doc"]["issue_date"] or ""))
    return {"amount": float(amount) if amount else None, "matches": out[:6]}


def mark_task(conn, task_id, done=True):
    if done:
        conn.execute("INSERT OR IGNORE INTO tasks_done(task) VALUES (?)", (task_id,))
        if task_id.startswith("urssaf:"):
            period = task_id[7:]
            if not conn.execute("SELECT 1 FROM urssaf_dues WHERE period = ?", (period,)).fetchone():
                r = urssaf_estimate(conn, period)
                urssaf_set_due(conn, period, r["total"], float(sum(r["ca"][k] for k in ("vente", "bic", "bnc"))))
        if task_id.startswith("tva:"):
            from calc import compute_period
            period = task_id[4:]
            r = compute_period(conn, period)
            conn.execute("INSERT INTO declarations(period, declared_at, tva_nette, credit_reporte) VALUES (?, "
                         "CURRENT_TIMESTAMP, ?, ?) ON CONFLICT(period) DO UPDATE SET declared_at=CURRENT_TIMESTAMP",
                         (period, r["l28"], r["l27"]))
    else:
        conn.execute("DELETE FROM tasks_done WHERE task = ?", (task_id,))
        if task_id.startswith("tva:"):
            conn.execute("DELETE FROM declarations WHERE period = ?", (task_id[4:],))


def refresh_status(conn, doc_id):
    doc, _ = get_doc(conn, doc_id)
    if doc["kind"] == "devis" or doc["status"] == "brouillon":
        return
    paid = paid_amount(conn, doc_id)
    if doc["kind"] == "avoir":
        status = "rembourse" if paid >= money(doc["total_ttc"]) and paid > 0 else "emise"
    elif doc.get("cancelled_by_id"):
        status = "annulee"
    else:
        due = money(doc["total_ttc"]) - credited_amount(conn, doc_id)
        if paid >= due and due >= 0:
            status = "payee" if paid > 0 else "annulee"
        elif paid > 0:
            status = "partielle"
        else:
            status = "emise"
    conn.execute("UPDATE documents SET status = ? WHERE id = ?", (status, doc_id))


def add_payment(conn, doc_id, pay_date, amount, method, reference=None):
    doc, lines = get_doc(conn, doc_id)
    if doc["kind"] not in ("facture", "avoir") or doc["status"] == "brouillon":
        raise BusinessError("Émettez d'abord le document.")
    amount = money(amount)
    if amount <= 0:
        raise BusinessError("Montant invalide.")
    pid = conn.execute("INSERT INTO payments(document_id, date, amount, method, reference) VALUES (?, ?, ?, ?, ?)",
                       (doc_id, pay_date, float(amount), method, (reference or "").strip() or None)).lastrowid
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
            addr = pick_address(out["directory"], siren)
            if addr:
                db.set_settings({"pdp_address_scheme": addr[0], "pdp_address_value": addr[1]}, conn)
                out["address"] = addr
        except superpdp.PDPError as e:
            out["directory_error"] = str(e)
    return out


def pick_address(res, siren=None):
    """Choisit l'adresse de facturation électronique (scheme, value) dans l'annuaire.

    Réponse de l'annuaire : {"data": [{"identifier": "0225:SIREN[_suffixe]", "is_active": bool, ...}]}.
    Priorité : l'adresse SIREN « nue » si active, sinon l'unique adresse active.
    """
    items = res.get("data") if isinstance(res, dict) else res
    active = []
    for it in items or []:
        ident = it.get("identifier") if isinstance(it, dict) else None
        if not ident or not it.get("is_active", True) or ":" not in ident:
            continue
        scheme, value = ident.split(":", 1)
        active.append((scheme, value))
    if siren:
        for scheme, value in active:
            if value == siren:
                return scheme, value
    if len(active) == 1:
        return active[0]
    return None


def lookup_siren(conn, siren):
    api = superpdp.Client("x", "x", db.get_settings(conn).get("pdp_endpoint"))
    out = {}
    res = api.lookup_company(siren)
    items = res.get("data") if isinstance(res, dict) else res
    if items:
        c = items[0]
        out = {
            "name": c.get("formal_name") or c.get("name") or "",
            "address": c.get("address") if isinstance(c.get("address"), str) else "",
            "post_code": c.get("postcode") or c.get("post_code") or "",
            "city": c.get("city") or "",
        }
    try:
        a = pick_address(api.directory_entries(siren), siren)
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
