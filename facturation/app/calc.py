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


def line_net(line) -> Decimal:
    return money(d(line["quantity"]) * d(line["unit_price"]))


def vat_breakdown(lines):
    """[(taux, base_ht, tva)] — TVA calculée par taux sur la base cumulée (règle EN 16931)."""
    bases = OrderedDict()
    for ln in lines:
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
    for r in rows:
        lines = conn.execute(
            "SELECT quantity, unit_price, vat_rate FROM lines WHERE document_id = ?", (r["doc_id"],)
        ).fetchall()
        bd = vat_breakdown(lines)
        sign = Decimal("-1") if r["kind"] == "avoir" else Decimal("1")
        for rate, base, tva in split_payment(r["amount"], bd):
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
        "empty": not rows and n_purchases == 0 and l22 == 0,
    }


def previous_of(period: str) -> str:
    y, m = (int(x) for x in period.split("-"))
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{y:04d}-{m:02d}"
