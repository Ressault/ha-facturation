"""Import de devis (ou factures) à partir de PDF MonAE / facturation.pro.

Le texte est extrait avec pypdf, puis analysé : numéro, date, client, objet, lignes, totaux.
Le résultat est toujours présenté pour vérification avant d'être enregistré.
"""
import io
import os
import secrets
import shutil
import re
from decimal import Decimal, InvalidOperation

from pypdf import PdfReader

MONTHS = {"janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
          "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12}

AMOUNT = r"-?(?:[1-9]\d{0,2}(?:[ \u00a0\u202f]\d{3})+|[1-9]\d*|0)(?:,\d{1,4})?"
ITEM_RE = re.compile(
    rf"^(?:(?P<label>.*?)\s+)?(?:(?P<rate>\d{{1,2}}(?:[.,]\d+)?)\s*%\s+)?(?P<pu>{AMOUNT})\s*€\s+"
    rf"(?P<qty>-?\d+(?:[.,]\d+)?)(?:\s*(?P<unit>[A-Za-zéèû²³.]{{1,6}}))?\s+(?P<total>{AMOUNT})\s*€\s*$")
FOOTER_RE = re.compile(r"^(Page \d+/\d+|.*SIRET \d{9}.*N° TVA.*|Site web :.*|.*–\s*Entreprise individuelle.*)$")


def pdf_text(data, mode="plain"):
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for p in reader.pages:
        try:
            pages.append(p.extract_text(extraction_mode="layout") if mode == "layout" else p.extract_text())
        except Exception:  # noqa: BLE001
            pages.append(p.extract_text() or "")
    return "\n".join(pages)


def amount(s):
    try:
        return Decimal(re.sub(r"[   €]", "", s or "").replace(",", "."))
    except InvalidOperation:
        return None


def fr_date(text):
    m = re.search(r"(\d{1,2})(?:er)?\s+([a-zéû]+)\s+(\d{4})", text or "", re.I)
    if m and m.group(2).lower() in MONTHS:
        return f"{int(m.group(3)):04d}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", text or "")
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


def parse_text(text):
    raw = [ln.strip() for ln in text.splitlines()]
    rows = [ln for ln in raw if ln and not FOOTER_RE.match(ln)]
    out = {"kind": "devis", "number": "", "issue_date": None, "validity_date": None, "subject": "",
           "client": {"name": "", "address": "", "post_code": "", "city": "", "siren": ""},
           "lines": [], "total_ht": None, "total_tva": None, "total_ttc": None, "warnings": []}
    full = "\n".join(rows)
    m = re.search(r"\b(DEVIS|FACTURE|AVOIR)\s*N°\s*:?\s*(\S+)", full, re.I)
    if m:
        out["kind"] = {"DEVIS": "devis", "FACTURE": "facture", "AVOIR": "avoir"}[m.group(1).upper()]
        out["number"] = m.group(2)
    m = re.search(r"^Date(?: d'émission| du devis)?\s*:\s*(.+)$", full, re.M | re.I)
    out["issue_date"] = fr_date(m.group(1)) if m else None
    m = re.search(r"(?:validité|Valable jusqu'au|Date de fin de validité)\s*:?\s*(.+)$", full, re.M | re.I)
    if m:
        out["validity_date"] = fr_date(m.group(1))
    m = re.search(r"^Objet\s*:\s*(.+)$", full, re.M)
    if m:
        out["subject"] = m.group(1).strip()

    # Client : bloc en tête, jusqu'à « Client – », « Objet » ou l'en-tête du tableau.
    head = []
    for ln in rows:
        if re.match(r"^(Client\b|Objet\s*:|Désignation\b)", ln):
            sm = re.search(r"SIRE[NT]\s*:\s*([\d ]+)", ln)
            if sm:
                out["client"]["siren"] = sm.group(1).replace(" ", "")
            break
        head.append(ln)
    if head:
        out["client"]["name"] = head[0]
        addr = []
        for ln in head[1:]:
            pm = re.match(r"^(\d{5})\s+(.+)$", ln)
            if pm:
                out["client"]["post_code"], out["client"]["city"] = pm.group(1), pm.group(2).title()
                break
            addr.append(ln)
        out["client"]["address"] = "\n".join(addr)

    # Tableau des lignes (peut se répéter sur plusieurs pages).
    in_table, lines = False, []
    for ln in rows:
        if ln.startswith("Désignation"):
            in_table = True
            continue
        if not in_table:
            continue
        if re.match(r"^((sous-)?total\s*\(?HT\)?|Report|À reporter|A reporter)\b", ln, re.I):
            in_table = False
            continue
        im = ITEM_RE.match(ln)
        if im:
            label = (im.group("label") or "").strip()
            if not label:  # désignation sur plusieurs lignes : on reprend les lignes en attente
                pending = []
                while lines and lines[-1]["kind"] == "title":
                    pending.insert(0, lines.pop()["designation"])
                label = " ".join(pending)
            label = label.rstrip(":").strip()
            lines.append({"kind": "item", "designation": label, "description": "",
                          "quantity": amount(im.group("qty")), "unit": im.group("unit") or "",
                          "unit_price": amount(im.group("pu")),
                          "vat_rate": Decimal(im.group("rate").replace(",", ".")) if im.group("rate") else Decimal("0"),
                          "_total": amount(im.group("total"))})
            continue
        prev = lines[-1] if lines else None
        if prev and prev["kind"] == "item" and (ln.startswith(("-", "•", "–")) or ln[:1].islower()):
            prev["description"] = (prev["description"] + "\n" + ln).strip()
        else:
            lines.append({"kind": "title", "designation": ln})
    # Remise globale (« Remise (10,00%) -22,00 € ») : ajoutée comme ligne négative.
    rate0 = next((ln["vat_rate"] for ln in lines if ln["kind"] == "item"), Decimal("0"))
    for m in re.finditer(rf"^Remise(?:\s*\(([\d.,]+)\s*%\))?\s+({AMOUNT})\s*€\s*$", full, re.M):
        val = amount(m.group(2))
        if val:
            val = -abs(val)
            lab = f"Remise {m.group(1).replace(',00', '')} %" if m.group(1) else "Remise"
            lines.append({"kind": "item", "designation": lab, "description": "", "quantity": Decimal("1"),
                          "unit": "", "unit_price": val, "vat_rate": rate0, "_total": val})
    out["lines"] = lines
    hts = re.findall(rf"^Total HT\s+({AMOUNT})\s*€", full, re.M)
    if hts:
        out["total_ht"] = amount(hts[-1])

    for m in re.finditer(rf"^TVA\s+(\d{{1,2}}(?:[.,]\d+)?)\s*%\s+({AMOUNT})\s*€", full, re.M):
        out["total_tva"] = (out["total_tva"] or Decimal("0")) + amount(m.group(2))
    m = re.search(rf"^(?:TOTAL TTC|Total TTC|Net à payer)\s+({AMOUNT})\s*€", full, re.M)
    if m:
        out["total_ttc"] = amount(m.group(1))
    if out["total_tva"] is None and out["total_ht"] is not None:
        out["total_tva"] = Decimal("0")
    if out["total_ttc"] is None and out["total_ht"] is not None:
        out["total_ttc"] = out["total_ht"] + (out["total_tva"] or 0)

    check(out)
    return out


def check(out):
    w = out["warnings"]
    items = [ln for ln in out["lines"] if ln["kind"] == "item"]
    if not out["number"]:
        w.append("Numéro introuvable.")
    if not out["issue_date"]:
        w.append("Date introuvable.")
    if not out["client"]["name"]:
        w.append("Client introuvable.")
    if not items:
        w.append("Aucune ligne chiffrée reconnue.")
    for ln in items:
        if ln["quantity"] is not None and ln["unit_price"] is not None and ln["_total"] is not None:
            if abs(ln["quantity"] * ln["unit_price"] - ln["_total"]) > Decimal("0.02"):
                w.append(f"Ligne « {ln['designation'][:40]} » : quantité × prix ≠ total.")
    ht = sum((ln["_total"] or 0) for ln in items)
    if out["total_ht"] is not None and abs(ht - out["total_ht"]) > Decimal("0.02"):
        w.append(f"Somme des lignes ({ht} €) ≠ total HT du PDF ({out['total_ht']} €).")


def parse_quote(data):
    return parse_text(pdf_text(data))


def jsonable(out):
    """Copie sérialisable (Decimal → float)."""
    def conv(v):
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        if isinstance(v, list):
            return [conv(x) for x in v]
        return v
    return conv(out)


# -- importation -----------------------------------------------------------------

def _norm(s):
    s = re.sub(r"\b(sarl|sas|sasu|eurl|sa|sci|eirl|ei|ste|société)\b", " ", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", "", s.translate(str.maketrans("àâäéèêëîïôöùûüç", "aaaeeeeiioouuuc")))


def match_client(conn, client):
    """Retrouve un client existant par SIREN/SIRET, puis par nom."""
    siren = (client.get("siren") or "")[:9]
    if len(siren) == 9:
        row = conn.execute("SELECT id FROM clients WHERE REPLACE(COALESCE(siren,''),' ','') LIKE ?",
                           (siren + "%",)).fetchone()
        if row:
            return row["id"]
    key = _norm(client.get("name"))
    if not key:
        return None
    for row in conn.execute("SELECT id, name FROM clients"):
        if _norm(row["name"]) == key:
            return row["id"]
    return None


def stage_dir():
    import db
    path = os.path.join(db.DATA_DIR, "tmp", "import-pdf")
    os.makedirs(path, exist_ok=True)
    return path


def stage(conn, files):
    """Analyse des PDF envoyés : renvoie la liste à vérifier (fichiers gardés en attente)."""
    out = []
    for f in files:
        data = f.read()
        if not data:
            continue
        token = secrets.token_hex(8)
        with open(os.path.join(stage_dir(), token + ".pdf"), "wb") as fh:
            fh.write(data)
        try:
            p = parse_quote(data)
        except Exception as exc:  # noqa: BLE001
            p = {"kind": "devis", "number": "", "issue_date": None, "validity_date": None, "subject": "",
                 "client": {"name": ""}, "lines": [], "total_ht": None, "total_tva": None, "total_ttc": None,
                 "warnings": [f"PDF illisible : {exc}"]}
        p["token"], p["filename"] = token, f.filename or "document.pdf"
        p["client_id"] = match_client(conn, p["client"])
        p["duplicate"] = bool(p["number"] and conn.execute(
            "SELECT 1 FROM documents WHERE number=?", (p["number"],)).fetchone())
        if p["duplicate"]:
            p["warnings"].append(f"Le numéro {p['number']} existe déjà : il sera ignoré sauf si vous le changez.")
        for ln in p["lines"]:
            ln.pop("_total", None)
        out.append(jsonable(p))
    return out


def save(conn, docs):
    """Crée les devis vérifiés. Renvoie (créés, erreurs)."""
    import db
    import logic
    from calc import totals
    created, errors = [], []
    s = db.get_settings(conn)
    for p in docs:
        if not p.get("include"):
            continue
        number = (p.get("number") or "").strip()
        label = number or p.get("filename") or "?"
        if not number:
            errors.append(f"{label} : numéro manquant.")
            continue
        if conn.execute("SELECT 1 FROM documents WHERE number=?", (number,)).fetchone():
            errors.append(f"{label} : ce numéro existe déjà.")
            continue
        issue = p.get("issue_date") or ""
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", issue):
            errors.append(f"{label} : date invalide.")
            continue
        lines = logic.clean_lines(p.get("lines") or [], s.get("default_nature") or "bnc")
        if not any(ln["kind"] == "item" for ln in lines):
            errors.append(f"{label} : aucune ligne chiffrée.")
            continue
        cid = int(p["client_id"]) if str(p.get("client_id") or "").isdigit() else None
        if not cid:
            cl = p.get("client") or {}
            name = (cl.get("name") or "").strip()
            if not name:
                errors.append(f"{label} : client manquant.")
                continue
            cid = match_client(conn, cl) or conn.execute(
                "INSERT INTO clients(kind, name, siren, address, post_code, city, country) "
                "VALUES ('pro', ?, ?, ?, ?, ?, 'FR')",
                (name, (cl.get("siren") or "")[:14] or None, cl.get("address") or None,
                 cl.get("post_code") or None, cl.get("city") or None)).lastrowid
        t = totals(lines, issue)
        status = p.get("status") if p.get("status") in ("en_attente", "accepte", "refuse") else "en_attente"
        doc_id = conn.execute(
            "INSERT INTO documents(kind, number, status, client_id, issue_date, validity_date, subject, "
            "total_ht, total_tva, total_ttc, imported) VALUES ('devis', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (number, status, cid, issue, p.get("validity_date") or None, (p.get("subject") or "").strip() or None,
             float(t["ht"]), float(t["tva"]), float(t["ttc"]))).lastrowid
        logic.insert_lines(conn, doc_id, lines)
        src = os.path.join(stage_dir(), re.sub(r"[^0-9a-f]", "", p.get("token") or "") + ".pdf")
        if p.get("token") and os.path.exists(src):
            dest = logic.pdf_path_for({"kind": "devis", "number": number, "issue_date": issue, "id": doc_id})
            shutil.move(src, dest)
            conn.execute("UPDATE documents SET pdf_path=? WHERE id=?", (dest, doc_id))
        created.append(doc_id)
    if created:
        db.log(conn, "info", f"{len(created)} devis importé(s) depuis des PDF.")
    return created, errors
