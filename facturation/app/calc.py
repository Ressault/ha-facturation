"""Calculs : totaux de documents, ventilation TVA, encaissements, déclaration CA3.

Règles retenues (prestations de services, régime réel normal mensuel) :
- La TVA collectée est exigible à l'encaissement : on ventile chaque paiement reçu
  au prorata des taux de TVA de la facture concernée.
- Un remboursement lié à un avoir diminue la TVA collectée du mois du remboursement.
- La TVA déductible est prise sur le mois de la « date de déduction » de chaque achat
  (date de paiement pour les services achetés, date de facture pour les biens).
- Les montants de la CA3 se saisissent en euros entiers (arrondi au plus proche).
"""
from collections import OrderedDict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
EURO = Decimal("1")

# Lignes de la 3310-CA3-SD (cadre B, opérations en France métropolitaine)
CA3_RATE_LINES = OrderedDict([
    (Decimal("20"), ("08", "Taux normal 20 %")),
    (Decimal("10"), ("9B", "Taux réduit 10 %")),
    (Decimal("5.5"), ("09", "Taux réduit 5,5 %")),
    (Decimal("2.1"), ("10", "Taux particulier 2,1 %")),
])


def d(value) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value).replace(",", ".").replace(" ", ""))


def money(value) -> Decimal:
    return d(value).quantize(CENT, rounding=ROUND_HALF_UP)


def euros(value) -> int:
    return int(d(value).quantize(EURO, rounding=ROUND_HALF_UP))


def rate_key(rate) -> Decimal:
    r = d(rate).normalize()
    return r if r != 0 else Decimal("0")


PAYMENT_TERMS = [
    ("0", "À réception"),
    ("15-inv", "15 jours net"),
    ("30-inv", "30 jours net"),
    ("45-inv", "45 jours net"),
    ("60-inv", "60 jours net"),
    ("00fm10-inv", "Fin de mois le 10"),
    ("00fm20-inv", "Fin de mois le 20"),
    ("07fm-inv", "7 jours fin de mois"),
    ("30fm-inv", "30 jours fin de mois"),
    ("30fm10-inv", "30 jours fin de mois le 10"),
    ("30fm20-inv", "30 jours fin de mois le 20"),
    ("45fm-inv", "45 jours fin de mois"),
]

NATURES = OrderedDict([
    ("bnc", "Prestation non commerciale (BNC)"),
    ("bic", "Prestation de services commerciale (BIC)"),
    ("vente", "Vente de marchandises (BIC)"),
    ("debours", "Débours"),
])


def _get(line, key, default=None):
    try:
        v = line[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def is_item(line) -> bool:
    return _get(line, "kind", "item") in ("item", "", None)


def items(lines):
    return [ln for ln in lines if is_item(ln)]


def line_net(line) -> Decimal:
    return money(d(line["quantity"]) * d(line["unit_price"]))


def vat_breakdown(lines):
    """[(taux, base_ht, tva)] — TVA calculée par taux sur la base cumulée (règle EN 16931)."""
    bases = OrderedDict()
    for ln in items(lines):
        k = rate_key(ln["vat_rate"])
        bases[k] = bases.get(k, Decimal("0")) + line_net(ln)
    out = []
    for rate, base in bases.items():
        out.append((rate, money(base), money(base * rate / 100)))
    return out


def totals(lines):
    bd = vat_breakdown(lines)
    ht = sum((b for _, b, _ in bd), Decimal("0"))
    tva = sum((t for _, _, t in bd), Decimal("0"))
    return {"ht": money(ht), "tva": money(tva), "ttc": money(ht + tva), "breakdown": bd}


def nature_breakdown(lines, default_nature="bnc"):
    """[(nature, taux, base_ht, tva)] : ventilation par nature d'activité et par taux.

    La TVA de chaque taux (calculée globalement) est répartie au prorata des bases.
    """
    per_rate = {r: (b, t) for r, b, t in vat_breakdown(lines)}
    groups = OrderedDict()
    for ln in items(lines):
        key = (_get(ln, "nature") or default_nature, rate_key(ln["vat_rate"]))
        groups[key] = groups.get(key, Decimal("0")) + line_net(ln)
    out = []
    allocated = {}
    keys = list(groups)
    for i, (nature, rate) in enumerate(keys):
        base = money(groups[(nature, rate)])
        rate_base, rate_tva = per_rate.get(rate, (Decimal("0"), Decimal("0")))
        last_of_rate = all(k[1] != rate for k in keys[i + 1:])
        if last_of_rate:
            tva = rate_tva - allocated.get(rate, Decimal("0"))
        else:
            tva = money(rate_tva * base / rate_base) if rate_base else Decimal("0")
            allocated[rate] = allocated.get(rate, Decimal("0")) + tva
        out.append((nature, rate, base, tva))
    return out


def split_payment_by_nature(amount, nb):
    """Ventile un encaissement TTC sur [(nature, taux, base, tva)] -> [(nature, taux, base, tva)]."""
    amount = money(amount)
    total = sum((b + t for _, _, b, t in nb), Decimal("0"))
    if not nb or total == 0:
        return [("bnc", Decimal("0"), amount, Decimal("0"))]
    out, allocated = [], Decimal("0")
    for i, (nature, rate, base, tva) in enumerate(nb):
        share = amount - allocated if i == len(nb) - 1 else money(amount * (base + tva) / total)
        allocated += share if i < len(nb) - 1 else 0
        b = money(share * base / (base + tva)) if (base + tva) else share
        out.append((nature, rate, b, share - b))
    return out


def split_payment(amount, breakdown):
    """Ventile un encaissement TTC sur les taux de la facture.

    Renvoie [(taux, base_ht, tva)] ; la somme des parts égale exactement le montant.
    """
    amount = money(amount)
    total_ttc = sum((b + t for _, b, t in breakdown), Decimal("0"))
    if total_ttc == 0 or not breakdown:
        return [(Decimal("0"), amount, Decimal("0"))]
    parts = []
    allocated = Decimal("0")
    for i, (rate, base, tva) in enumerate(breakdown):
        if i == len(breakdown) - 1:
            share = amount - allocated
        else:
            share = money(amount * (base + tva) / total_ttc)
            allocated += share
        # dans la part TTC de ce taux : base = part / (1 + taux)
        part_base = money(share * 100 / (100 + rate)) if rate else share
        parts.append((rate, part_base, share - part_base))
    return parts


def month_bounds(period: str):
    y, m = (int(x) for x in period.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1)
    return start.isoformat(), end.isoformat()


def previous_period(today: date | None = None) -> str:
    today = today or date.today()
    y, m = today.year, today.month - 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y:04d}-{m:02d}"


def next_period(period: str) -> str:
    y, m = (int(x) for x in period.split("-"))
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y:04d}-{m:02d}"


def compute_period(conn, period: str):
    """Calcule la TVA du mois `period` (YYYY-MM) à partir de la base."""
    start, end = month_bounds(period)

    # --- TVA collectée sur encaissements
    by_rate = OrderedDict()
    details = []
    rows = conn.execute(
        """SELECT p.id, p.date, p.amount, d.id AS doc_id, d.kind, d.number, d.client_snapshot
           FROM payments p JOIN documents d ON d.id = p.document_id
           WHERE p.date >= ? AND p.date < ? AND d.kind IN ('facture', 'avoir')
           ORDER BY p.date, p.id""",
        (start, end),
    ).fetchall()
    debours = Decimal("0")
    for r in rows:
        lines = conn.execute(
            "SELECT quantity, unit_price, vat_rate, kind, nature FROM lines WHERE document_id = ?", (r["doc_id"],)
        ).fetchall()
        nb = nature_breakdown(lines)
        sign = Decimal("-1") if r["kind"] == "avoir" else Decimal("1")
        for nature, rate, base, tva in split_payment_by_nature(r["amount"], nb):
            if nature == "debours":
                debours += sign * (base + tva)
                continue
            acc = by_rate.setdefault(rate, [Decimal("0"), Decimal("0")])
            acc[0] += sign * base
            acc[1] += sign * tva
        details.append({
            "date": r["date"], "number": r["number"], "kind": r["kind"],
            "amount": float(sign * money(r["amount"])),
        })

    collected_lines = []
    base_total = Decimal("0")
    tva_brute = Decimal("0")
    other_rates = []
    for rate, (base, tva) in by_rate.items():
        base_total += base
        if rate in CA3_RATE_LINES:
            code, label = CA3_RATE_LINES[rate]
            collected_lines.append({"code": code, "label": label, "rate": rate,
                                    "base": money(base), "tva": money(tva)})
            tva_brute += tva
        elif rate == 0:
            other_rates.append({"rate": rate, "base": money(base)})
        else:
            other_rates.append({"rate": rate, "base": money(base), "tva": money(tva)})
            tva_brute += tva
    collected_lines.sort(key=lambda x: list(CA3_RATE_LINES).index(x["rate"]))

    taxable_base = sum((c["base"] for c in collected_lines), Decimal("0"))

    # --- TVA déductible
    ded = conn.execute(
        """SELECT category, COALESCE(SUM(total_tva), 0) AS tva, COUNT(*) AS n
           FROM purchases WHERE deduction_date >= ? AND deduction_date < ?
           GROUP BY category""",
        (start, end),
    ).fetchall()
    ded_immo = Decimal("0")
    ded_other = Decimal("0")
    n_purchases = 0
    for r in ded:
        n_purchases += r["n"]
        if r["category"] == "immobilisation":
            ded_immo += d(r["tva"])
        else:
            ded_other += d(r["tva"])

    prev = conn.execute(
        "SELECT credit_reporte FROM declarations WHERE period = ?", (previous_of(period),)
    ).fetchone()
    report_credit = d(prev["credit_reporte"]) if prev and prev["credit_reporte"] else Decimal("0")

    # --- Lignes CA3 (en euros entiers, comme sur impots.gouv)
    l08 = [dict(c, base_e=euros(c["base"]), tva_e=euros(c["tva"])) for c in collected_lines]
    l16 = sum(c["tva_e"] for c in l08) + sum(euros(o.get("tva", 0)) for o in other_rates)
    l19 = euros(ded_immo)
    l20 = euros(ded_other)
    l22 = euros(report_credit)
    l23 = l19 + l20 + l22
    net = l16 - l23

    return {
        "period": period,
        "payments": details,
        "n_purchases": n_purchases,
        "a1_base": euros(taxable_base),
        "a1_exact": money(taxable_base),
        "lines_brute": l08,
        "other_rates": other_rates,
        "l16": l16,
        "tva_brute_exact": money(tva_brute),
        "l19": l19,
        "l20": l20,
        "l22": l22,
        "l23": l23,
        "td": max(net, 0),
        "l28": max(net, 0),
        "l25": max(-net, 0),
        "l27": max(-net, 0),
        "l32": max(net, 0),
        "ca_urssaf": money(base_total),
        "debours": money(debours),
        "empty": not rows and n_purchases == 0 and l22 == 0,
    }


def previous_of(period: str) -> str:
    y, m = (int(x) for x in period.split("-"))
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{y:04d}-{m:02d}"


# -- URSSAF ---------------------------------------------------------------------

def quarter_of(period_or_date: str) -> str:
    y, m = int(period_or_date[:4]), int(period_or_date[5:7])
    return f"{y:04d}-T{(m - 1) // 3 + 1}"


def quarter_bounds(q: str):
    y, t = int(q[:4]), int(q[-1])
    start = date(y, 3 * (t - 1) + 1, 1)
    end = date(y + (t == 4), (3 * t) % 12 + 1, 1)
    return start.isoformat(), end.isoformat()


def previous_quarter(today: date | None = None) -> str:
    today = today or date.today()
    t = (today.month - 1) // 3 + 1
    y = today.year
    if t == 1:
        return f"{y - 1:04d}-T4"
    return f"{y:04d}-T{t - 1}"


def shift_quarter(q: str, delta: int) -> str:
    y, t = int(q[:4]), int(q[-1])
    idx = y * 4 + (t - 1) + delta
    return f"{idx // 4:04d}-T{idx % 4 + 1}"


def compute_urssaf(conn, start: str, end: str, settings: dict):
    """Chiffre d'affaires encaissé HT par nature entre start (inclus) et end (exclu), et cotisations estimées."""
    ca = OrderedDict((k, Decimal("0")) for k in NATURES)
    rows = conn.execute(
        """SELECT p.amount, d.id AS doc_id, d.kind FROM payments p JOIN documents d ON d.id = p.document_id
           WHERE p.date >= ? AND p.date < ? AND d.kind IN ('facture', 'avoir')""", (start, end)).fetchall()
    for r in rows:
        lines = conn.execute("SELECT quantity, unit_price, vat_rate, kind, nature FROM lines WHERE document_id = ?",
                             (r["doc_id"],)).fetchall()
        sign = Decimal("-1") if r["kind"] == "avoir" else Decimal("1")
        for nature, rate, base, tva in split_payment_by_nature(r["amount"], nature_breakdown(
                lines, settings.get("default_nature") or "bnc")):
            ca[nature if nature in ca else "bnc"] += sign * (base if nature != "debours" else base + tva)
    vl = settings.get("versement_liberatoire") == "1"
    lines_out, total = [], Decimal("0")
    for key, label in (("vente", "Ventes de marchandises"), ("bic", "Prestations commerciales ou artisanales"),
                       ("bnc", "Prestations non commerciales")):
        base = euros(ca[key])
        rate = d(settings.get(f"urssaf_rate_{key}") or 0)
        amount = euros(d(base) * rate / 100)
        lines_out.append({"label": f"Cotisations - {label}", "base": base, "rate": rate, "amount": amount})
        total += amount
        if vl:
            r = d(settings.get(f"ir_rate_{key}") or 0)
            a = euros(d(base) * r / 100)
            lines_out.append({"label": f"Impôt sur le revenu - {label}", "base": base, "rate": r, "amount": a})
            total += a
    cfp_base = euros(ca["vente"] + ca["bic"] + ca["bnc"])
    cfp_rate = d(settings.get("cfp_rate") or 0)
    cfp = euros(d(cfp_base) * cfp_rate / 100)
    lines_out.append({"label": "CFP (contribution à la formation professionnelle)", "base": cfp_base,
                      "rate": cfp_rate, "amount": cfp})
    total += cfp
    return {"ca": {k: euros(v) for k, v in ca.items()}, "ca_exact": ca, "lines": lines_out, "total": int(total),
            "n_payments": len(rows)}
