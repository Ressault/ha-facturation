"""Génération des PDF (devis, factures, avoirs) avec fpdf2."""
import os
from datetime import date

from fpdf import FPDF

from calc import d, money, totals

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")

TITLES = {"devis": "DEVIS", "facture": "FACTURE", "avoir": "AVOIR"}
ACCENT = (37, 99, 235)
GREY = (90, 90, 90)


def fmt_money(v) -> str:
    q = money(v)
    neg = q < 0
    s = f"{abs(q):,.2f}".replace(",", " ").replace(".", ",")
    return f"{'-' if neg else ''}{s} €"


def fmt_qty(v) -> str:
    q = d(v).normalize()
    s = f"{q:f}"
    return s.replace(".", ",")


def fmt_rate(v) -> str:
    return fmt_qty(v) + " %"


def fmt_date(iso) -> str:
    if not iso:
        return ""
    try:
        y, m, dd = iso[:10].split("-")
        return f"{dd}/{m}/{y}"
    except ValueError:
        return iso


class DocPDF(FPDF):
    def __init__(self, footer_text):
        super().__init__(format="A4")
        self.footer_text = footer_text
        self.add_font("dejavu", "", os.path.join(FONT_DIR, "DejaVuSans.ttf"))
        self.add_font("dejavu", "B", os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf"))
        self.set_auto_page_break(True, margin=28)
        self.set_margins(15, 15, 15)

    def footer(self):
        self.set_y(-22)
        self.set_font("dejavu", "", 7)
        self.set_text_color(*GREY)
        self.multi_cell(0, 3.4, self.footer_text, align="C")
        self.set_y(-8)
        self.cell(0, 4, f"Page {self.page_no()}/{{nb}}", align="R")


def seller_lines(s):
    name = s.get("company_name", "")
    if name and " EI" not in name and not name.endswith("EI"):
        name = f"{name} EI"
    out = [name]
    if s.get("company_trade_name"):
        out.append(s["company_trade_name"])
    out += [s.get("address", ""), f"{s.get('post_code', '')} {s.get('city', '')}".strip()]
    if s.get("email"):
        out.append(s["email"])
    if s.get("phone"):
        out.append(s["phone"])
    return [x for x in out if x]


def legal_footer(s):
    parts = []
    name = s.get("company_name", "")
    parts.append(f"{name} - Entrepreneur individuel (EI)")
    if s.get("siret"):
        parts.append(f"SIRET {s['siret']}")
    elif s.get("siren"):
        parts.append(f"SIREN {s['siren']}")
    if s.get("vat_number"):
        parts.append(f"TVA intracommunautaire {s['vat_number']}")
    if s.get("ape"):
        parts.append(f"APE {s['ape']}")
    if s.get("rcs_rm"):
        parts.append(s["rcs_rm"])
    text = " · ".join(parts)
    if s.get("insurance"):
        text += "\n" + s["insurance"]
    if s.get("footer_note"):
        text += "\n" + s["footer_note"]
    return text


def build_pdf(doc, lines, seller, client, path, extra=None):
    """doc : dict du document ; seller/client : instantanés ; renvoie le chemin."""
    extra = extra or {}
    kind = doc["kind"]
    t = totals(lines)
    pdf = DocPDF(legal_footer(seller))
    pdf.alias_nb_pages()
    pdf.add_page()

    # En-tête vendeur
    pdf.set_font("dejavu", "B", 13)
    pdf.set_text_color(20, 20, 20)
    sl = seller_lines(seller)
    pdf.cell(110, 7, sl[0] if sl else "", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("dejavu", "", 9)
    pdf.set_text_color(*GREY)
    for ln in sl[1:]:
        pdf.cell(110, 4.6, ln, new_x="LMARGIN", new_y="NEXT")

    # Titre et références
    pdf.set_xy(120, 15)
    pdf.set_font("dejavu", "B", 20)
    pdf.set_text_color(*ACCENT)
    pdf.cell(75, 10, TITLES[kind], align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("dejavu", "", 9)
    pdf.set_text_color(20, 20, 20)
    refs = [("N°", doc.get("number") or "BROUILLON"), ("Date", fmt_date(doc.get("issue_date")))]
    if kind == "devis" and doc.get("validity_date"):
        refs.append(("Valable jusqu'au", fmt_date(doc["validity_date"])))
    if kind == "facture" and doc.get("due_date"):
        refs.append(("Échéance", fmt_date(doc["due_date"])))
    if doc.get("service_date"):
        refs.append(("Prestation", doc["service_date"]))
    if kind == "avoir" and extra.get("avoir_of_number"):
        refs.append(("Sur facture", extra["avoir_of_number"]))
    for label, value in refs:
        pdf.set_x(120)
        pdf.cell(35, 5, label, align="R")
        pdf.set_font("dejavu", "B", 9)
        pdf.cell(40, 5, str(value), align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("dejavu", "", 9)

    # Bloc client
    y = max(pdf.get_y(), 52) + 6
    pdf.set_xy(110, y)
    pdf.set_fill_color(243, 245, 249)
    cl = [client.get("name", "")]
    if client.get("address"):
        cl.append(client["address"])
    cl.append(f"{client.get('post_code', '')} {client.get('city', '')}".strip())
    if client.get("country") and client["country"] != "FR":
        cl.append(client["country"])
    if client.get("siren"):
        cl.append(f"SIREN {client['siren']}")
    if client.get("vat_number"):
        cl.append(f"TVA {client['vat_number']}")
    pdf.set_font("dejavu", "", 7.5)
    pdf.set_text_color(*GREY)
    pdf.cell(85, 5, "Client", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(20, 20, 20)
    for i, ln in enumerate(cl):
        pdf.set_x(110)
        pdf.set_font("dejavu", "B" if i == 0 else "", 9.5 if i == 0 else 9)
        pdf.cell(85, 5, ln, fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(110)
    pdf.cell(85, 2, "", fill=True, new_x="LMARGIN", new_y="NEXT")

    y = pdf.get_y() + 6
    pdf.set_y(y)
    if doc.get("subject"):
        pdf.set_font("dejavu", "B", 10)
        pdf.multi_cell(0, 5.5, doc["subject"], new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)

    # Tableau des lignes
    widths = [86, 18, 14, 26, 14, 22]
    heads = ["Désignation", "Qté", "Unité", "PU HT", "TVA", "Total HT"]
    pdf.set_font("dejavu", "B", 8.5)
    pdf.set_fill_color(*ACCENT)
    pdf.set_text_color(255, 255, 255)
    for w, h in zip(widths, heads):
        pdf.cell(w, 7, h, fill=True, align="L" if h == "Désignation" else "R")
    pdf.ln()
    pdf.set_text_color(20, 20, 20)
    for i, ln in enumerate(lines):
        fill = i % 2 == 1
        pdf.set_fill_color(248, 249, 251)
        text = ln["designation"]
        desc = ln.get("description") or ""
        # hauteurs (simulation) avant de dessiner
        pdf.set_font("dejavu", "B", 9)
        h_title = 5 * len(pdf.multi_cell(widths[0], 5, text, dry_run=True, output="LINES"))
        h_desc = 0
        if desc:
            pdf.set_font("dejavu", "", 8)
            h_desc = 4 * len(pdf.multi_cell(widths[0], 4, desc, dry_run=True, output="LINES"))
        h_total = h_title + h_desc + 1.5
        if pdf.get_y() + h_total > 265:
            pdf.add_page()
        x0, y0 = pdf.get_x(), pdf.get_y()
        if fill:
            pdf.rect(x0, y0, sum(widths), h_total, style="F")
        pdf.set_font("dejavu", "B", 9)
        pdf.multi_cell(widths[0], 5, text, align="L", new_x="LMARGIN", new_y="NEXT")
        if desc:
            pdf.set_x(x0)
            pdf.set_font("dejavu", "", 8)
            pdf.set_text_color(*GREY)
            pdf.multi_cell(widths[0], 4, desc, align="L", new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(20, 20, 20)
        pdf.set_xy(x0 + widths[0], y0)
        pdf.set_font("dejavu", "", 9)
        vals = [fmt_qty(ln["quantity"]), ln.get("unit") or "", fmt_money(ln["unit_price"]),
                fmt_rate(ln["vat_rate"]), fmt_money(d(ln["quantity"]) * d(ln["unit_price"]))]
        for w, v in zip(widths[1:], vals):
            pdf.cell(w, 5, v, align="R")
        pdf.set_xy(x0, y0 + h_total)

    # Totaux
    pdf.ln(4)
    if pdf.get_y() > 220:
        pdf.add_page()
    x = 120
    pdf.set_font("dejavu", "", 9)
    pdf.set_x(x)
    pdf.cell(45, 6, "Total HT", align="R")
    pdf.cell(30, 6, fmt_money(t["ht"]), align="R", new_x="LMARGIN", new_y="NEXT")
    for rate, base, tva in t["breakdown"]:
        pdf.set_x(x)
        pdf.cell(45, 6, f"TVA {fmt_rate(rate)} sur {fmt_money(base)}", align="R")
        pdf.cell(30, 6, fmt_money(tva), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(x)
    pdf.set_font("dejavu", "B", 11)
    pdf.set_fill_color(*ACCENT)
    pdf.set_text_color(255, 255, 255)
    label = "Total TTC" if kind != "avoir" else "Total TTC de l'avoir"
    pdf.cell(45, 8, label, align="R", fill=True)
    pdf.cell(30, 8, fmt_money(t["ttc"]), align="R", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(20, 20, 20)
    if kind == "facture" and extra.get("paid", 0) > 0:
        pdf.set_font("dejavu", "", 9)
        pdf.set_x(x)
        pdf.cell(45, 6, "Déjà réglé", align="R")
        pdf.cell(30, 6, fmt_money(extra["paid"]), align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(x)
        pdf.set_font("dejavu", "B", 9)
        pdf.cell(45, 6, "Reste à payer", align="R")
        pdf.cell(30, 6, fmt_money(t["ttc"] - money(extra["paid"])), align="R",
                 new_x="LMARGIN", new_y="NEXT")

    # Mentions
    pdf.ln(6)
    pdf.set_font("dejavu", "", 8.5)
    mentions = []
    if doc.get("notes"):
        mentions.append(doc["notes"])
    if kind == "facture":
        mentions.append("Nature de l'opération : prestations de services.")
        if seller.get("iban"):
            iban = seller["iban"]
            bic = f" - BIC {seller['bic']}" if seller.get("bic") else ""
            mentions.append(f"Paiement par virement : IBAN {iban}{bic} - Référence : {doc.get('number') or ''}")
        mentions.append(seller.get("late_penalty", ""))
        if client.get("kind", "pro") == "pro":
            mentions.append(seller.get("recovery_fee", ""))
        mentions.append(seller.get("discount_terms", ""))
    if kind == "avoir":
        mentions.append("Avoir à déduire de la facture référencée ci-dessus.")
    for m in [m for m in mentions if m]:
        pdf.multi_cell(0, 4.4, m, align="L", new_x="LMARGIN", new_y="NEXT")

    if kind == "devis":
        pdf.ln(6)
        if pdf.get_y() > 230:
            pdf.add_page()
        pdf.set_font("dejavu", "B", 9)
        pdf.set_x(110)
        pdf.cell(85, 6, "Bon pour accord - date et signature du client", new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(110)
        pdf.set_draw_color(180, 180, 180)
        pdf.cell(85, 28, "", border=1, new_x="LMARGIN", new_y="NEXT")

    pdf.set_title(f"{TITLES[kind].title()} {doc.get('number') or ''}")
    pdf.set_author(seller.get("company_name", ""))
    pdf.set_creation_date(_creation_dt(doc.get("issue_date")))
    pdf.output(path)
    return path


def _creation_dt(iso):
    from datetime import datetime, timezone
    try:
        dt = date.fromisoformat(iso[:10])
        return datetime(dt.year, dt.month, dt.day, 12, tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
