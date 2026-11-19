"""Import de l'historique (exports CSV de MonAE / facturation.pro ou autre logiciel).

Le format exact des exports variant selon les logiciels, les colonnes sont reconnues
par leur intitulé (sans tenir compte des accents ni de la casse).
"""
import csv
import io
import re
import unicodedata
from datetime import datetime

from calc import d, money

CLIENT_COLS = {
    "name": ["raison sociale", "societe", "nom de l'entreprise", "entreprise", "client", "nom", "denomination",
             "organisation", "company"],
    "first_name": ["prenom"],
    "siren": ["siren", "siret", "numero siren", "numero siret", "n siret", "n siren"],
    "vat_number": ["tva intracommunautaire", "numero de tva", "n tva", "tva intra", "vat"],
    "address": ["adresse", "adresse 1", "adresse ligne 1", "rue", "address"],
    "address2": ["adresse 2", "complement d'adresse", "adresse ligne 2", "complement"],
    "post_code": ["code postal", "cp", "postal code", "zip"],
    "city": ["ville", "commune", "city"],
    "country": ["pays", "country", "code pays"],
    "email": ["email", "e-mail", "courriel", "mail"],
    "phone": ["telephone", "tel", "portable", "mobile", "phone"],
    "kind": ["type", "categorie", "particulier", "type de client"],
}
INVOICE_COLS = {
    "number": ["numero", "n facture", "numero de facture", "reference", "ref", "facture", "n"],
    "date": ["date", "date de facture", "date facture", "date d'emission", "emise le", "date de creation"],
    "client": ["client", "raison sociale", "nom du client", "societe", "destinataire"],
    "siren": ["siren", "siret", "siren client", "siret client"],
    "ht": ["total ht", "montant ht", "ht", "total hors taxes", "montant hors taxe"],
    "tva": ["tva", "montant tva", "total tva", "taxes"],
    "ttc": ["total ttc", "montant ttc", "ttc", "net a payer", "total"],
    "paid_date": ["date de paiement", "date paiement", "date de reglement", "payee le", "reglee le",
                  "date d'encaissement"],
    "status": ["statut", "etat", "status", "paiement"],
    "due_date": ["echeance", "date d'echeance", "date echeance"],
    "subject": ["objet", "titre", "libelle", "description"],
    "kind": ["type", "type de document", "nature"],
}


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[°º#.:]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def read_csv(raw: bytes):
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";" if sample.count(";") > sample.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if not rows:
        return [], []
    return rows[0], rows[1:]


def map_columns(header, spec):
    h = [norm(x) for x in header]
    mapping = {}
    for field, names in spec.items():
        for name in names:  # correspondance exacte d'abord
            if name in h and h.index(name) not in mapping.values():
                mapping[field] = h.index(name)
                break
        if field not in mapping:
            for i, col in enumerate(h):
                if i in mapping.values():
                    continue
                if any(col.startswith(name) for name in names if len(name) > 3):
                    mapping[field] = i
                    break
    return mapping


def parse_date(v):
    v = (v or "").strip()
    if not v:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y", "%d.%m.%Y", "%Y/%m/%d", "%d/%m/%Y %H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(v[:19], fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_amount(v):
    v = (v or "").replace("€", "").replace(" ", "").replace(" ", "").strip()
    if not v:
        return None
    if "," in v and "." in v:
        v = v.replace(".", "").replace(",", ".") if v.rfind(",") > v.rfind(".") else v.replace(",", "")
    else:
        v = v.replace(",", ".")
    try:
        return d(v)
    except Exception:  # noqa: BLE001
        return None


def kind_of(header):
    h = " ".join(norm(x) for x in header)
    inv_score = sum(k in h for k in ["ht", "ttc", "numero", "facture", "montant"])
    cli_score = sum(k in h for k in ["adresse", "code postal", "ville", "email", "telephone", "siret"])
    return "factures" if inv_score > cli_score else "clients"


def get(row, mapping, key):
    i = mapping.get(key)
    return row[i].strip() if i is not None and i < len(row) else ""


def import_clients(conn, header, rows, report):
    m = map_columns(header, CLIENT_COLS)
    if "name" not in m:
        report["errors"].append("Fichier clients : colonne du nom introuvable.")
        return
    n = 0
    for row in rows:
        name = get(row, m, "name")
        first = get(row, m, "first_name")
        if first and name and first.lower() not in name.lower():
            name = f"{first} {name}"
        if not name:
            continue
        siren = re.sub(r"\D", "", get(row, m, "siren"))[:9]
        kind_raw = norm(get(row, m, "kind"))
        kind = "particulier" if ("particulier" in kind_raw or kind_raw in ("oui", "1", "true")) or (
            not siren and first) else "pro"
        if conn.execute("SELECT 1 FROM clients WHERE lower(name) = lower(?) OR (siren != '' AND siren = ?)",
                        (name, siren or "-")).fetchone():
            report["skipped"] += 1
            continue
        address = get(row, m, "address")
        if get(row, m, "address2"):
            address += "\n" + get(row, m, "address2")
        country = (get(row, m, "country") or "FR").upper()
        if country in ("FRANCE", ""):
            country = "FR"
        conn.execute(
            "INSERT INTO clients(kind, name, siren, vat_number, address, post_code, city, country, email, phone) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, name, siren, get(row, m, "vat_number").replace(" ", ""), address, get(row, m, "post_code"),
             get(row, m, "city"), country[:2], get(row, m, "email"), get(row, m, "phone")))
        n += 1
    report["clients"] += n


def find_or_create_client(conn, name, siren):
    row = None
    if siren:
        row = conn.execute("SELECT id FROM clients WHERE siren = ?", (siren,)).fetchone()
    if not row and name:
        row = conn.execute("SELECT id FROM clients WHERE lower(name) = lower(?)", (name,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO clients(kind, name, siren) VALUES (?, ?, ?)",
                        ("pro" if siren else "pro", name or "Client importé", siren)).lastrowid


def import_invoices(conn, header, rows, report):
    m = map_columns(header, INVOICE_COLS)
    missing = [k for k in ("number", "date") if k not in m]
    if missing or not ({"ht", "ttc"} & set(m)):
        report["errors"].append("Fichier factures : colonnes numéro, date et montant (HT ou TTC) nécessaires. "
                                f"Colonnes trouvées : {', '.join(header)}")
        return
    n = 0
    for row in rows:
        number = get(row, m, "number")
        issue = parse_date(get(row, m, "date"))
        if not number or not issue:
            continue
        if conn.execute("SELECT 1 FROM documents WHERE number = ?", (number,)).fetchone():
            report["skipped"] += 1
            continue
        kind_raw = norm(get(row, m, "kind"))
        if "devis" in kind_raw:
            report["skipped"] += 1
            continue
        kind = "avoir" if "avoir" in kind_raw else "facture"
        ht, tva, ttc = (parse_amount(get(row, m, k)) for k in ("ht", "tva", "ttc"))
        if ht is None and ttc is not None and tva is not None:
            ht = ttc - tva
        if ht is None:
            continue
        if tva is None:
            tva = (ttc - ht) if ttc is not None else ht * d("0.2")
        ht, tva = money(abs(ht)), money(abs(tva))
        if kind == "facture" and (parse_amount(get(row, m, "ht")) or 0) < 0:
            kind = "avoir"
        rate = (tva * 100 / ht).quantize(d("0.1")) if ht else d(0)
        rate = min([d(20), d(10), d("5.5"), d("2.1"), d(0)], key=lambda r: abs(r - rate))
        client_id = find_or_create_client(conn, get(row, m, "client"), re.sub(r"\D", "", get(row, m, "siren"))[:9])
        status_raw = norm(get(row, m, "status"))
        paid_date = parse_date(get(row, m, "paid_date"))
        is_paid = bool(paid_date) or any(w in status_raw for w in ("paye", "regle", "encaisse", "solde"))
        doc_id = conn.execute(
            "INSERT INTO documents(kind, number, status, client_id, issue_date, due_date, subject, total_ht, "
            "total_tva, total_ttc, imported, client_snapshot, seller_snapshot) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (kind, number, "payee" if is_paid and kind == "facture" else "emise", client_id, issue,
             parse_date(get(row, m, "due_date")), get(row, m, "subject") or "Facture importée",
             float(ht), float(tva), float(ht + tva), client_json(conn, client_id), seller_json(conn))).lastrowid
        conn.execute("INSERT INTO lines(document_id, position, designation, quantity, unit, unit_price, vat_rate) "
                     "VALUES (?, 0, ?, 1, 'u', ?, ?)",
                     (doc_id, get(row, m, "subject") or f"Facture {number} (historique)", float(ht), float(rate)))
        if is_paid and paid_date:
            conn.execute("INSERT INTO payments(document_id, date, amount, method, pdp_reported) "
                         "VALUES (?, ?, ?, 'historique', 1)", (doc_id, paid_date, float(ht + tva)))
        elif is_paid:
            report["paid_without_date"] += 1
            conn.execute("INSERT INTO payments(document_id, date, amount, method, pdp_reported) "
                         "VALUES (?, ?, ?, 'historique', 1)", (doc_id, issue, float(ht + tva)))
        n += 1
        report["last_number"] = max(report.get("last_number") or "", number)
    report["invoices"] += n


def client_json(conn, client_id):
    import db
    import logic
    return db.dumps(logic.snapshot_client(conn, client_id))


def seller_json(conn):
    import db
    import logic
    return db.dumps(logic.snapshot_seller(conn))


def import_files(conn, files):
    report = {"clients": 0, "invoices": 0, "skipped": 0, "errors": [], "paid_without_date": 0,
              "last_number": None, "files": []}
    # clients d'abord, pour rattacher les factures
    parsed = []
    for f in files:
        if not f or not f.filename:
            continue
        raw = f.read()
        if not f.filename.lower().endswith((".csv", ".txt")):
            report["errors"].append(f"{f.filename} : seuls les fichiers CSV sont pris en charge "
                                    "(exportez vos listes en CSV depuis MonAE).")
            continue
        header, rows = read_csv(raw)
        if not header:
            report["errors"].append(f"{f.filename} : fichier vide.")
            continue
        parsed.append((kind_of(header), f.filename, header, rows))
    for kind, name, header, rows in sorted(parsed, key=lambda x: x[0] != "clients"):
        report["files"].append(f"{name} ({'clients' if kind == 'clients' else 'factures'}, {len(rows)} lignes)")
        if kind == "clients":
            import_clients(conn, header, rows, report)
        else:
            import_invoices(conn, header, rows, report)
    return report
