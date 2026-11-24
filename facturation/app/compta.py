"""Exports comptables : écritures (journaux ventes, achats, banque), livre des recettes, registre des achats.

Deux formats : « excel » (CSV ; séparateur point-virgule, virgule décimale, dates JJ/MM/AAAA)
et « sheets » (CSV universel : virgule, point décimal, dates AAAA-MM-JJ, que Google Sheets lit dans toutes les langues).
"""
import csv
import io
import re
from decimal import Decimal

import db
from calc import cash_rows, money

ACCOUNTS = {
    "client": ("411000", "Clients"), "fournisseur": ("401000", "Fournisseurs"),
    "ventes": ("706000", "Prestations de services"), "debours": ("467000", "Débours refacturés"),
    "tva_collectee": ("445710", "TVA collectée"), "tva_deductible": ("445660", "TVA déductible sur ABS"),
    "tva_immo": ("445620", "TVA déductible sur immobilisations"),
    "charges": ("606000", "Achats et charges externes"), "immobilisation": ("218000", "Immobilisations"),
    "banque": ("512000", "Banque"),
}
METHODS = {"virement": "Virement", "cheque": "Chèque", "especes": "Espèces", "carte": "Carte",
           "prelevement": "Prélèvement", "paypal": "PayPal"}


def _name(snapshot, conn=None, client_id=None):
    s = db.loads(snapshot) or {}
    if s.get("name"):
        return s["name"]
    if conn and client_id:
        r = conn.execute("SELECT name FROM clients WHERE id=?", (client_id,)).fetchone()
        if r:
            return r["name"]
    return ""


def _aux(name, prefix):
    return prefix + re.sub(r"[^A-Z0-9]", "", (name or "").upper().translate(
        str.maketrans("ÀÂÄÉÈÊËÎÏÔÖÙÛÜÇ", "AAAEEEEIIOOUUUC")))[:10]


def _bounds(year):
    if year:
        return f"{year}-01-01", f"{int(year) + 1}-01-01"
    return "0000-01-01", "9999-12-31"


def entries(conn, year=None):
    """Écritures en partie double : VT (factures/avoirs émis), AC (achats), BQ (encaissements, règlements)."""
    start, end = _bounds(year)
    out = []

    def add(journal, num, day, account, label, ref, debit=Decimal("0"), credit=Decimal("0"), aux="", aux_lib=""):
        debit, credit = money(debit), money(credit)
        if debit < 0:
            debit, credit = Decimal("0"), credit - debit
        if credit < 0:
            credit, debit = Decimal("0"), debit - credit
        if debit or credit:
            out.append({"journal": journal, "num": num, "date": day, "compte": account[0], "compte_lib": account[1],
                        "aux": aux, "aux_lib": aux_lib, "piece": ref, "libelle": label,
                        "debit": debit, "credit": credit})

    n = 0
    for d in conn.execute(
            "SELECT * FROM documents WHERE kind IN ('facture','avoir') AND status <> 'brouillon' AND number IS NOT NULL "
            "AND issue_date >= ? AND issue_date < ? ORDER BY issue_date, id", (start, end)).fetchall():
        n += 1
        sign = Decimal("-1") if d["kind"] == "avoir" else Decimal("1")
        client = _name(d["client_snapshot"], conn, d["client_id"])
        num = f"VT{n:05d}"
        lib = f"{'Avoir' if d['kind'] == 'avoir' else 'Facture'} {d['number']} {client}".strip()
        add("VT", num, d["issue_date"], ACCOUNTS["client"], lib, d["number"],
            debit=sign * Decimal(str(d["total_ttc"])), aux=_aux(client, "C"), aux_lib=client)
        lines = conn.execute("SELECT quantity, unit_price, vat_rate, kind, nature FROM lines WHERE document_id=?",
                             (d["id"],)).fetchall()
        base_ventes, base_debours = Decimal("0"), Decimal("0")
        for ln in lines:
            if (ln["kind"] or "item") != "item":
                continue
            net = Decimal(str(ln["quantity"] or 0)) * Decimal(str(ln["unit_price"] or 0))
            if ln["nature"] == "debours":
                base_debours += net
            else:
                base_ventes += net
        tva = Decimal(str(d["total_tva"] or 0))
        ht = Decimal(str(d["total_ht"] or 0))
        base_ventes = ht - money(base_debours)  # le HT enregistré fait foi
        add("VT", num, d["issue_date"], ACCOUNTS["ventes"], lib, d["number"], credit=sign * base_ventes)
        add("VT", num, d["issue_date"], ACCOUNTS["debours"], lib, d["number"], credit=sign * base_debours)
        add("VT", num, d["issue_date"], ACCOUNTS["tva_collectee"], lib, d["number"], credit=sign * tva)

    n = 0
    for p in conn.execute(
            "SELECT * FROM purchases WHERE COALESCE(draft,0)=0 AND COALESCE(invoice_date, paid_date) >= ? "
            "AND COALESCE(invoice_date, paid_date) < ? ORDER BY COALESCE(invoice_date, paid_date), id",
            (start, end)).fetchall():
        n += 1
        day = p["invoice_date"] or p["paid_date"]
        num, ref = f"AC{n:05d}", p["invoice_number"] or f"ACH{p['id']}"
        lib = f"{p['supplier']} {p['description'] or ''}".strip()[:80]
        immo = p["category"] == "immobilisation"
        add("AC", num, day, ACCOUNTS["immobilisation" if immo else "charges"], lib, ref, debit=Decimal(str(p["total_ht"] or 0)))
        add("AC", num, day, ACCOUNTS["tva_immo" if immo else "tva_deductible"], lib, ref,
            debit=Decimal(str(p["total_tva"] or 0)))
        add("AC", num, day, ACCOUNTS["fournisseur"], lib, ref, credit=Decimal(str(p["total_ttc"] or 0)),
            aux=_aux(p["supplier"], "F"), aux_lib=p["supplier"])

    n = 0
    for r in receipts(conn, year):
        n += 1
        num = f"BQ{n:05d}"
        amt = Decimal(str(r["montant"]))
        lib = f"Règlement {r['piece']} {r['client']}".strip()
        add("BQ", num, r["date"], ACCOUNTS["banque"], lib, r["piece"], debit=amt)
        add("BQ", num, r["date"], ACCOUNTS["client"], lib, r["piece"], credit=amt,
            aux=_aux(r["client"], "C"), aux_lib=r["client"])
    for p in conn.execute(
            "SELECT * FROM purchases WHERE COALESCE(draft,0)=0 AND paid_date >= ? AND paid_date < ? "
            "ORDER BY paid_date, id", (start, end)).fetchall():
        n += 1
        num, ref = f"BQ{n:05d}", p["invoice_number"] or f"ACH{p['id']}"
        amt = Decimal(str(p["total_ttc"] or 0))
        lib = f"Paiement {p['supplier']}"
        add("BQ", num, p["paid_date"], ACCOUNTS["fournisseur"], lib, ref, debit=amt,
            aux=_aux(p["supplier"], "F"), aux_lib=p["supplier"])
        add("BQ", num, p["paid_date"], ACCOUNTS["banque"], lib, ref, credit=amt)
    out.sort(key=lambda e: ({"VT": 0, "AC": 1, "BQ": 2}[e["journal"]], e["date"] or "", e["num"]))
    return out


def receipts(conn, year=None):
    """Livre des recettes : encaissements dans l'ordre chronologique (avoirs remboursés en négatif)."""
    start, end = _bounds(year)
    out = []
    for r in cash_rows(conn, start, end):
        doc = conn.execute("SELECT client_id, subject FROM documents WHERE id=?", (r["doc_id"],)).fetchone()
        method = ""
        if r.get("id"):
            m = conn.execute("SELECT method FROM payments WHERE id=?", (r["id"],)).fetchone()
            method = METHODS.get((m["method"] or "") if m else "", ((m["method"] or "") if m else "").capitalize())
        sign = -1 if r["kind"] == "avoir" else 1
        ttc = Decimal(str(r["amount"])) * sign
        ratio = (Decimal(str(r["total_ht"])) / Decimal(str(r["total_ttc"]))) if r["total_ttc"] else Decimal("1")
        out.append({"date": r["date"], "piece": r["number"] or "", "client": _name(r["client_snapshot"], conn,
                    doc["client_id"] if doc else None), "nature": "Prestation de services",
                    "objet": (doc["subject"] if doc else "") or "", "montant_ht": money(ttc * ratio),
                    "montant": money(ttc), "mode": method or ("Imputation d'avoir" if r["kind"] == "avoir" else "")})
    return out


def purchases_register(conn, year=None):
    start, end = _bounds(year)
    return [{"date": p["invoice_date"] or p["paid_date"], "piece": p["invoice_number"] or "", "fournisseur": p["supplier"],
             "objet": p["description"] or "", "categorie": "Immobilisation" if p["category"] == "immobilisation"
             else "Biens et services", "ht": money(Decimal(str(p["total_ht"] or 0))),
             "tva": money(Decimal(str(p["total_tva"] or 0))), "ttc": money(Decimal(str(p["total_ttc"] or 0))),
             "paye": p["paid_date"] or "", "mode": METHODS.get(p["method"] or "", (p["method"] or "").capitalize())}
            for p in conn.execute(
                "SELECT * FROM purchases WHERE COALESCE(draft,0)=0 AND COALESCE(invoice_date, paid_date) >= ? "
                "AND COALESCE(invoice_date, paid_date) < ? ORDER BY COALESCE(invoice_date, paid_date), id",
                (start, end)).fetchall()]


# -- mise en forme -----------------------------------------------------------------

def _fmt(v, fmt):
    if isinstance(v, Decimal):
        return f"{v:.2f}".replace(".", ",") if fmt == "excel" else f"{v:.2f}"
    if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", v) and fmt == "excel":
        return f"{v[8:10]}/{v[5:7]}/{v[:4]}"
    return "" if v is None else v


def to_csv(header, keys, rows, fmt="excel", total_keys=()):
    out = io.StringIO()
    w = csv.writer(out, delimiter=";" if fmt == "excel" else ",")
    w.writerow(header)
    for r in rows:
        w.writerow([_fmt(r.get(k), fmt) for k in keys])
    if total_keys and rows:
        tot = {k: sum((r[k] for r in rows), Decimal("0")) for k in total_keys}
        w.writerow([_fmt(tot.get(k), fmt) if k in tot else ("Total" if i == 0 else "") for i, k in enumerate(keys)])
    return out.getvalue()


EXPORTS = {
    "ecritures": ("Écritures comptables",
                  ["Journal", "N° écriture", "Date", "Compte", "Libellé du compte", "Compte auxiliaire",
                   "Libellé auxiliaire", "Pièce", "Libellé", "Débit", "Crédit"],
                  ["journal", "num", "date", "compte", "compte_lib", "aux", "aux_lib", "piece", "libelle", "debit",
                   "credit"], ("debit", "credit"), entries),
    "recettes": ("Livre des recettes",
                 ["Date d'encaissement", "Facture", "Client", "Nature", "Objet", "Montant HT", "Montant encaissé",
                  "Mode de règlement"],
                 ["date", "piece", "client", "nature", "objet", "montant_ht", "montant", "mode"],
                 ("montant_ht", "montant"), receipts),
    "achats": ("Registre des achats",
               ["Date", "Facture", "Fournisseur", "Objet", "Catégorie", "HT", "TVA", "TTC", "Payé le", "Mode"],
               ["date", "piece", "fournisseur", "objet", "categorie", "ht", "tva", "ttc", "paye", "mode"],
               ("ht", "tva", "ttc"), purchases_register),
}


def export(conn, what, year=None, fmt="excel"):
    title, header, keys, total_keys, fn = EXPORTS[what]
    return to_csv(header, keys, fn(conn, year), fmt, total_keys)
