"""Statistiques, prévisionnel et ventilations (pages du menu Accueil)."""
import re
from collections import OrderedDict
from datetime import date

import db
from calc import cash_rows

ACTIVITIES = [
    ("Visites virtuelles 360°", r"visite|360|panoram"),
    ("Cyclorama", r"cyclorama"),
    ("Plans de masse", r"plan(s)? (de )?masse"),
    ("Insertions", r"insertion"),
    ("Vidéos et animations", r"vid[ée]o|animati|film|motion"),
    ("Perspectives intérieures", r"int[ée]rieur"),
    ("Perspectives extérieures", r"perspective|image|rendu|vue a[ée]rienne|ext[ée]rieur"),
    ("Graphisme et mise en page", r"logo|plaquette|mise en page|pao|graphi|affiche|flyer"),
]
OTHER = "Autres"
MOIS = ["Janvier", "Février", "Mars", "Avril", "Mai", "Juin", "Juillet", "Août", "Septembre", "Octobre",
        "Novembre", "Décembre"]
MOIS_COURT = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def activity(*texts):
    for t in texts:
        low = (t or "").lower()
        for name, pat in ACTIVITIES:
            if re.search(pat, low):
                return name
    return OTHER


def _client_name(conn, doc, cache):
    s = db.loads(doc["client_snapshot"]) or {}
    if s.get("name"):
        return s["name"]
    cid = doc["client_id"]
    if cid not in cache:
        r = conn.execute("SELECT name FROM clients WHERE id=?", (cid,)).fetchone()
        cache[cid] = r["name"] if r else "Client inconnu"
    return cache[cid]


def _doc_parts(conn, doc):
    """Répartition du HT d'un document par activité : [(activité, part du HT)] dont la somme vaut 1."""
    parts, total = OrderedDict(), 0.0
    for ln in conn.execute("SELECT designation, description, quantity, unit_price, kind, nature FROM lines "
                           "WHERE document_id=? ORDER BY position", (doc["id"],)):
        if (ln["kind"] or "item") != "item" or ln["nature"] == "debours":
            continue
        net = (ln["quantity"] or 0) * (ln["unit_price"] or 0)
        a = activity(ln["designation"], ln["description"], doc["subject"])
        parts[a] = parts.get(a, 0.0) + net
        total += net
    if not parts or abs(total) < 0.005:
        return [(activity(doc["subject"]), 1.0)]
    return [(a, v / total) for a, v in parts.items()]


def sales(conn, mode="facture", start="0000-01-01", end="9999-12-31"):
    """Enregistrements de chiffre d'affaires : date, client, activité, HT, TTC (avoirs en négatif)."""
    out, ccache, pcache = [], {}, {}

    def parts(doc):
        if doc["id"] not in pcache:
            pcache[doc["id"]] = _doc_parts(conn, doc)
        return pcache[doc["id"]]

    if mode == "encaisse":
        for r in cash_rows(conn, start, end):
            doc = conn.execute("SELECT * FROM documents WHERE id=?", (r["doc_id"],)).fetchone()
            sign = -1 if r["kind"] == "avoir" else 1
            ratio = (r["total_ht"] / r["total_ttc"]) if r["total_ttc"] else 1
            ttc = sign * r["amount"]
            for a, share in parts(doc):
                out.append({"date": r["date"], "client": _client_name(conn, doc, ccache), "activite": a,
                            "ht": ttc * ratio * share, "ttc": ttc * share, "doc": doc["id"]})
        return out
    for doc in conn.execute("SELECT * FROM documents WHERE kind IN ('facture','avoir') AND status <> 'brouillon' "
                            "AND issue_date >= ? AND issue_date < ?", (start, end)).fetchall():
        sign = -1 if doc["kind"] == "avoir" else 1
        for a, share in parts(doc):
            out.append({"date": doc["issue_date"], "client": _client_name(conn, doc, ccache), "activite": a,
                        "ht": sign * doc["total_ht"] * share, "ttc": sign * doc["total_ttc"] * share, "doc": doc["id"]})
    return out


def purchases(conn, start="0000-01-01", end="9999-12-31"):
    return [{"date": p["invoice_date"], "fournisseur": p["supplier"] or "Sans fournisseur", "ht": p["total_ht"] or 0,
             "ttc": p["total_ttc"] or 0}
            for p in conn.execute("SELECT * FROM purchases WHERE COALESCE(draft,0)=0 AND invoice_date >= ? "
                                  "AND invoice_date < ?", (start, end)).fetchall()]


# -- Statistiques ----------------------------------------------------------------

def _bucket(day, step):
    y, m = int(day[:4]), int(day[5:7])
    if step == "trimestre":
        return y, (m - 1) // 3
    if step == "annee":
        return y, 0
    return y, m - 1


def _bucket_label(i, step, year):
    if step == "trimestre":
        return f"T{i + 1}"
    if step == "annee":
        return str(year)
    return MOIS[i]


def statistics(conn, year, mode="facture", step="mois", compare=True, cat="", first_year=None):
    """Tableau de bord : CA, achats, bénéfice par période de l'année `year` (ou par année si step = 'annee')."""
    if step == "annee":
        y0 = first_year or year
        years = list(range(y0, year + 1))
        keys = [(y, 0) for y in years]
        labels = [str(y) for y in years]
        start, end = f"{y0 - 1}-01-01", f"{year + 1}-01-01"
    else:
        n = 4 if step == "trimestre" else 12
        keys = [(year, i) for i in range(n)]
        labels = [_bucket_label(i, step, year) for i in range(n)]
        start, end = f"{year - 1}-01-01", f"{year + 1}-01-01"
    ca, ca_ttc, ach = {}, {}, {}
    for r in sales(conn, mode, start, end):
        if cat and r["activite"] != cat:
            continue
        k = _bucket(r["date"], step)
        ca[k] = ca.get(k, 0) + r["ht"]
        ca_ttc[k] = ca_ttc.get(k, 0) + r["ttc"]
    for p in ([] if cat else purchases(conn, start, end)):
        k = _bucket(p["date"], step)
        ach[k] = ach.get(k, 0) + p["ht"]
    today = date.today()
    rows = []
    for k, label in zip(keys, labels):
        prev = (k[0] - 1, k[1])
        cur, last = ca.get(k, 0.0), ca.get(prev, 0.0)
        future = step != "annee" and (k[0] > today.year or (k[0] == today.year and k[1] > _bucket(
            today.isoformat(), step)[1]))
        rows.append({"label": label, "short": MOIS_COURT[k[1]] if step == "mois" else label, "ca": cur, "ca_ttc": ca_ttc.get(k, 0.0), "ca_prev": last if compare else None,
                     "growth": ((cur - last) / abs(last) * 100) if compare and last and not future else None,
                     "achats": ach.get(k, 0.0), "benefice": cur - ach.get(k, 0.0), "future": future})
    tot = {key: sum((r[key] or 0) for r in rows) for key in ("ca", "ca_ttc", "achats", "benefice")}
    # comparaison à période égale : les mois à venir ne comptent pas
    tot["ca_prev"] = sum((r["ca_prev"] or 0) for r in rows if not r["future"]) if compare else None
    tot["growth"] = ((tot["ca"] - tot["ca_prev"]) / abs(tot["ca_prev"]) * 100) if compare and tot["ca_prev"] else None
    return {"rows": rows, "total": tot}


# -- Ventilations ------------------------------------------------------------------

def breakdown(records, key, value="ht", top=7):
    agg, count = {}, {}
    for r in records:
        agg[r[key]] = agg.get(r[key], 0) + r[value]
        count.setdefault(r[key], set()).add(r.get("doc") or id(r))
    items = sorted(agg.items(), key=lambda kv: -kv[1])
    total = sum(v for _, v in items)
    rows = [{"name": k, "value": v, "n": len(count[k]), "pct": (v / total * 100) if total else 0} for k, v in items
            if abs(v) >= 0.005]
    slices = [r for r in rows if r["value"] > 0][:top]
    rest = sum(r["value"] for r in rows if r["value"] > 0) - sum(s["value"] for s in slices)
    if rest > 0.005:
        slices.append({"name": "Autres", "value": rest, "pct": (rest / total * 100) if total else 0, "other": True})
    return {"rows": rows, "slices": slices, "total": total}


# -- Prévisionnel ------------------------------------------------------------------

def _month_key(day):
    return day[:7]


def _add_months(y, m, n):
    k = y * 12 + (m - 1) + n
    return k // 12, k % 12 + 1


def forecast(conn, open_invoices, months=12):
    today = date.today()
    cur = today.strftime("%Y-%m")
    keys = []
    for i in range(months):
        y, m = _add_months(today.year, today.month, i)
        keys.append(f"{y:04d}-{m:02d}")
    labels = [f"{MOIS_COURT[int(k[5:]) - 1]} {k[2:4]}" for k in keys]

    fac = {k: 0.0 for k in keys}
    late, beyond = 0.0, 0.0
    for u in open_invoices:
        due = (u["due_date"] or u["issue_date"] or cur)
        if due < today.isoformat():
            late += u["rest_ht"]
        elif _month_key(due) in fac:
            fac[_month_key(due)] += u["rest_ht"]
        else:
            beyond += u["rest_ht"]

    to_pay = {k: 0.0 for k in keys}
    pay_late = 0.0
    unpaid = conn.execute("SELECT * FROM purchases WHERE COALESCE(draft,0)=0 AND paid_date IS NULL").fetchall()
    for p in unpaid:
        k = _month_key(p["invoice_date"] or cur)
        if k < cur:
            pay_late += p["total_ht"] or 0
        elif k in to_pay:
            to_pay[k] += p["total_ht"] or 0
    rec = {k: 0.0 for k in keys}
    for r in conn.execute("SELECT * FROM recurring_purchases WHERE active=1").fetchall():
        y, m = int(r["next_date"][:4]), int(r["next_date"][5:7])
        n_done, step = r["occurrences"] or 0, max(1, r["every_months"] or 1)
        for _ in range(months * 2):
            k = f"{y:04d}-{m:02d}"
            if k > keys[-1] or (r["max_occurrences"] and n_done >= r["max_occurrences"]):
                break
            if k in rec:
                rec[k] += r["total_ht"] or 0
            n_done += 1
            y, m = _add_months(y, m, step)

    devis = [dict(r) for r in conn.execute(
        "SELECT d.*, cl.name AS client_name FROM documents d LEFT JOIN clients cl ON cl.id=d.client_id "
        "WHERE d.kind='devis' AND d.status='en_attente' ORDER BY d.issue_date DESC").fetchall()]
    accepted = [dict(r) for r in conn.execute(
        "SELECT d.*, cl.name AS client_name FROM documents d LEFT JOIN clients cl ON cl.id=d.client_id "
        "WHERE d.kind='devis' AND d.status='accepte' AND NOT EXISTS (SELECT 1 FROM documents f WHERE "
        "f.source_devis_id=d.id AND f.kind='facture') ORDER BY d.issue_date DESC").fetchall()]
    drafts = [dict(r) for r in conn.execute(
        "SELECT d.*, cl.name AS client_name FROM documents d LEFT JOIN clients cl ON cl.id=d.client_id "
        "WHERE d.kind='facture' AND d.status='brouillon' ORDER BY d.issue_date DESC").fetchall()]
    rest_fac = sum(u["rest_ht"] for u in open_invoices)
    return {
        "labels": labels, "keys": keys,
        "fac": [fac[k] for k in keys], "fac_late": late, "fac_beyond": beyond,
        "pay": [to_pay[k] for k in keys], "pay_late": pay_late, "rec": [rec[k] for k in keys],
        "devis": devis, "accepted": accepted, "drafts": drafts,
        "summary": {"factures": rest_fac,
                    "devis": sum(d["total_ht"] for d in devis) + sum(d["total_ht"] for d in accepted)
                    + sum(d["total_ht"] for d in drafts),
                    "achats": sum(p["total_ht"] or 0 for p in unpaid) + sum(rec[k] for k in keys)},
    }
