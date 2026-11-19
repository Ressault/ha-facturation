"""Génération des PDF (devis, factures, avoirs) avec fpdf2 — mise en page sobre, logo, lignes typées."""
import os
from datetime import date

from fpdf import FPDF

from calc import PAYMENT_TERMS, d, line_net, money, totals

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
TERMS_LABEL = dict(PAYMENT_TERMS)

TITLES = {"devis": "Devis", "facture": "Facture", "avoir": "Avoir"}
INK = (29, 29, 31)
GREY = (110, 110, 115)
LIGHT = (245, 245, 247)
HAIR = (220, 220, 225)
ACCENT = (0, 113, 227)


def fmt_money(v) -> str:
    q = money(v)
    neg = q < 0
    s = f"{abs(q):,.2f}".replace(",", " ").replace(".", ",")
    return f"{'-' if neg else ''}{s} €"


def fmt_qty(v) -> str:
    q = d(v).normalize()
    return f"{q:f}".replace(".", ",")


def fmt_rate(v) -> str:
    return fmt_qty(v) + " %"


def fmt_date(iso) -> str:
    if not iso:
        return ""
    try:
        y, m, dd = str(iso)[:10].split("-")
        return f"{dd}/{m}/{y}"
    except ValueError:
        return str(iso)


def penalty_text(doc, seller):
    rate = doc.get("penalty_rate")
    if rate in (None, ""):
        rate = seller.get("default_penalty_rate")
    if rate not in (None, ""):
        return f"Pénalités de retard : {fmt_qty(rate)} % par an, exigibles dès le lendemain de l'échéance."
    return seller.get("late_penalty") or "Pénalités de retard : 3 fois le taux d'intérêt légal."


def discount_text(doc, seller):
    rate = doc.get("discount_rate")
    if rate in (None, ""):
        rate = seller.get("default_discount_rate")
    if rate not in (None, "") and d(rate) > 0:
        return f"Escompte pour paiement anticipé : {fmt_qty(rate)} %."
    return seller.get("discount_terms") or "Pas d'escompte pour paiement anticipé."


class DocPDF(FPDF):
    def __init__(self, footer_text):
        super().__init__(format="A4")
        self.footer_text = footer_text
        self.add_font("dejavu", "", os.path.join(FONT_DIR, "DejaVuSans.ttf"))
        self.add_font("dejavu", "B", os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf"))
        self.set_auto_page_break(True, margin=26)
        self.set_margins(16, 16, 16)
        self.table_header = None

    def header(self):
        if self.page_no() > 1 and self.table_header:
            self.set_y(16)
            self.table_header()

    def footer(self):
        self.set_y(-20)
        self.set_draw_color(*HAIR)
        self.line(16, self.get_y() - 2, 194, self.get_y() - 2)
        self.set_font("dejavu", "", 6.8)
        self.set_text_color(*GREY)
        self.multi_cell(150, 3.2, self.footer_text, align="L")
        self.set_xy(166, -20)
        self.cell(28, 3.2, f"{self.page_no()} / {{nb}}", align="R")


def seller_name(s):
    name = s.get("company_name", "")
    if name and not name.rstrip().endswith("EI"):
        name = f"{name} EI"
    return name


def legal_footer(s):
    parts = [f"{s.get('company_name', '')} — Entrepreneur individuel (EI)"]
    if s.get("website"):
        parts.append(s["website"].strip())
    if s.get("siret"):
        parts.append(f"SIRET {s['siret']}")
    elif s.get("siren"):
        parts.append(f"SIREN {s['siren']}")
    second = []
    if s.get("vat_number"):
        second.append(f"TVA {s['vat_number']}")
    if s.get("ape"):
        second.append(f"APE {s['ape']}")
    if s.get("rcs_rm"):
        second.append(s["rcs_rm"])
    text = " · ".join(parts) + ("\n" + " · ".join(second) if second else "")
    if s.get("insurance"):
        text += "\n" + s["insurance"]
    if s.get("footer_note"):
        text += "\n" + s["footer_note"]
    return text


def group_iban(iban):
    raw = "".join((iban or "").split()).upper()
    return " ".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def payment_box(pdf, doc, seller, t, extra):
    """Encadré « Règlement par virement » : titulaire, IBAN en gros, BIC, référence, échéance, montant."""
    rest = t["ttc"] - money(extra.get("paid", 0) or 0)
    num = doc.get("number")
    rows = [("Titulaire", seller.get("company_name", "")), ("IBAN", group_iban(seller["iban"]))]
    if seller.get("bic"):
        rows.append(("BIC", seller["bic"].upper()))
    rows.append(("Référence à indiquer", f"Facture {num}" if num else "N° de la facture"))
    if doc.get("due_date"):
        rows.append(("À régler avant le", fmt_date(doc["due_date"])))
    rows.append(("Montant", fmt_money(rest)))
    # largeurs mesurées : rien ne déborde du cadre
    pdf.set_font("dejavu", "", 8.2)
    label_w = max(pdf.get_string_width(lbl) for lbl, _ in rows) + 6
    val_w = 0
    for lbl, val in rows:
        pdf.set_font("dejavu", "B", 10.5 if lbl == "IBAN" else 9)
        val_w = max(val_w, pdf.get_string_width(val))
    pad = 6
    w = min(178, pad * 2 + label_w + val_w + 4)
    pdf.set_font("dejavu", "B", 9.5)
    w = max(w, pdf.get_string_width("Règlement par virement bancaire") + pad * 2)
    line_h = 6.2
    h = pad + 6 + 1.5 + line_h * len(rows) + pad - 1.5
    if pdf.get_y() + h > 262:
        pdf.add_page()
    x, y = 16, pdf.get_y()
    pdf.set_fill_color(*LIGHT)
    pdf.set_draw_color(*HAIR)
    pdf.rect(x, y, w, h, style="DF", round_corners=True, corner_radius=2.5)
    pdf.set_xy(x + pad, y + pad - 1)
    pdf.set_text_color(*INK)
    pdf.cell(w - pad * 2, 6, "Règlement par virement bancaire", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1.5)
    for label, val in rows:
        pdf.set_x(x + pad)
        pdf.set_font("dejavu", "", 8.2)
        pdf.set_text_color(*GREY)
        pdf.cell(label_w, line_h, label)
        pdf.set_text_color(*INK)
        pdf.set_font("dejavu", "B", 10.5 if label == "IBAN" else 9)
        pdf.cell(w - pad * 2 - label_w, line_h, val, new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(y + h + 4)


def build_pdf(doc, lines, seller, client, path, extra=None):
    """doc : dict du document ; seller/client : instantanés ; renvoie le chemin."""
    extra = extra or {}
    kind = doc["kind"]
    t = totals(lines)
    pdf = DocPDF(legal_footer(seller))
    pdf.alias_nb_pages()
    pdf.add_page()

    # --- En-tête : logo + vendeur à gauche, titre à droite
    y_logo_end = 16
    logo = seller.get("logo_path")
    if logo and os.path.exists(logo):
        try:
            img = pdf.image(logo, x=16, y=14, h=18, keep_aspect_ratio=True)
            y_logo_end = 14 + 18 + 3
            _ = img
        except Exception:  # noqa: BLE001 — logo illisible : on l'ignore
            y_logo_end = 16
    pdf.set_xy(16, y_logo_end)
    pdf.set_font("dejavu", "B", 11.5)
    pdf.set_text_color(*INK)
    pdf.cell(100, 6, seller_name(seller), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("dejavu", "", 8.5)
    pdf.set_text_color(*GREY)
    info = []
    if seller.get("company_trade_name"):
        info.append(seller["company_trade_name"])
    info += [x for x in (seller.get("address") or "").splitlines() if x.strip()]
    info.append(f"{seller.get('post_code', '')} {seller.get('city', '')}".strip())
    for k in ("email", "phone"):
        if seller.get(k):
            info.append(seller[k])
    y_phone = None
    for ln in [x for x in info if x]:
        if seller.get("phone") and ln == seller["phone"]:
            y_phone = pdf.get_y()
        pdf.cell(100, 4.3, ln, new_x="LMARGIN", new_y="NEXT")
    y_seller_end = pdf.get_y()
    # le haut du bloc client s'aligne sur la ligne du téléphone (à défaut, sur la dernière ligne du bloc vendeur)
    y_client = y_phone if y_phone is not None else max(16, y_seller_end - 4.3)

    refs = [("Date", fmt_date(doc.get("issue_date")))]
    if kind == "devis" and doc.get("validity_date"):
        refs.append(("Valable jusqu'au", fmt_date(doc["validity_date"])))
    if kind == "facture" and doc.get("due_date"):
        refs.append(("Échéance", fmt_date(doc["due_date"])))
    if kind == "facture" and doc.get("payment_terms"):
        refs.append(("Règlement", TERMS_LABEL.get(doc["payment_terms"], doc["payment_terms"])))
    if doc.get("service_date"):
        refs.append(("Réalisé le", doc["service_date"]))
    if kind == "avoir" and extra.get("avoir_of_number"):
        refs.append(("Sur facture", extra["avoir_of_number"]))

    # --- Bloc client, en haut à droite
    cl = [client.get("name", "")]
    cl += [x for x in (client.get("address") or "").splitlines() if x.strip()]
    cl.append(f"{client.get('post_code', '')} {client.get('city', '')}".strip())
    if client.get("country") and client["country"] != "FR":
        cl.append(client["country"])
    small = []
    if client.get("siren"):
        small.append(f"SIREN {client['siren']}")
    if client.get("vat_number"):
        small.append(f"TVA {client['vat_number']}")
    y = y_client
    box_h = 9 + 5 * len([c for c in cl if c]) + (4.5 if small else 0)
    pdf.set_fill_color(*LIGHT)
    pdf.rect(112, y, 82, box_h, style="F", round_corners=True, corner_radius=2.5)
    pdf.set_xy(117, y + 3.5)
    pdf.set_font("dejavu", "", 7)
    pdf.set_text_color(*GREY)
    pdf.cell(72, 4, "FACTURÉ À" if kind != "devis" else "DESTINATAIRE", new_x="LMARGIN", new_y="NEXT")
    for i, ln in enumerate([c for c in cl if c]):
        pdf.set_x(117)
        pdf.set_font("dejavu", "B" if i == 0 else "", 9.5 if i == 0 else 8.8)
        pdf.set_text_color(*INK)
        pdf.cell(72, 5, ln, new_x="LMARGIN", new_y="NEXT")
    if small:
        pdf.set_x(117)
        pdf.set_font("dejavu", "", 7.5)
        pdf.set_text_color(*GREY)
        pdf.cell(72, 4.5, "  ·  ".join(small), new_x="LMARGIN", new_y="NEXT")

    # --- Bandeau du document, juste au-dessus des lignes : type, numéro bien visible, dates
    y = max(y + box_h, y_seller_end) + 9
    pdf.set_draw_color(*HAIR)
    pdf.set_line_width(0.2)
    pdf.line(16, y, 194, y)
    y += 4
    pdf.set_xy(16, y)
    word = TITLES[kind]
    num = f"N° {doc['number']}" if doc.get("number") else "N° (brouillon)"
    pdf.set_font("dejavu", "B", 13)
    pdf.set_text_color(*INK)
    ww = pdf.get_string_width(word) + 2.2
    pdf.cell(ww, 7, word)
    pdf.set_text_color(70, 70, 76)
    pdf.cell(120, 7, num, new_x="LMARGIN", new_y="NEXT")
    # dates juste en dessous : libellé gris, valeur en normal
    y = pdf.get_y() + 1.5
    x = 16
    for label, value in refs:
        pdf.set_font("dejavu", "", 8.5)
        lw = pdf.get_string_width(label + " ") + 0.5
        vw = pdf.get_string_width(str(value))
        if x + lw + vw > 194:
            x, y = 16, y + 5
        pdf.set_xy(x, y)
        pdf.set_text_color(*GREY)
        pdf.cell(lw, 5, label)
        pdf.set_text_color(*INK)
        pdf.cell(vw, 5, str(value))
        x += lw + vw + 7
    y += 6
    pdf.set_y(y + 2)
    if doc.get("subject"):
        pdf.set_font("dejavu", "B", 10.5)
        pdf.set_text_color(*INK)
        pdf.multi_cell(0, 5.5, doc["subject"], align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
    else:
        pdf.ln(1)

    # --- Lignes
    widths = [88, 16, 14, 24, 14, 22]
    heads = ["Désignation", "Qté", "Unité", "PU HT", "TVA", "Total HT"]

    def table_header():
        pdf.set_font("dejavu", "B", 7.5)
        pdf.set_text_color(*GREY)
        for w, h in zip(widths, heads):
            pdf.cell(w, 6, h.upper(), align="L" if h == "Désignation" else "R")
        pdf.ln()
        pdf.set_draw_color(*INK)
        pdf.set_line_width(0.3)
        pdf.line(16, pdf.get_y(), 194, pdf.get_y())
        pdf.set_line_width(0.2)
        pdf.ln(1.5)

    table_header()
    pdf.table_header = table_header
    running = d(0)
    options_total = d(0)
    for ln in lines:
        lk = ln.get("kind") or "item"
        if lk == "page":
            pdf.add_page()
            continue
        if lk == "blank":
            pdf.ln(5)
            continue
        if lk == "title":
            if pdf.get_y() > 250:
                pdf.add_page()
            pdf.ln(1.5)
            pdf.set_font("dejavu", "B", 9.5)
            pdf.set_text_color(*INK)
            pdf.multi_cell(0, 6, ln["designation"], align="L", new_x="LMARGIN", new_y="NEXT")
            continue
        if lk == "comment":
            pdf.set_font("dejavu", "", 8.2)
            pdf.set_text_color(*GREY)
            pdf.multi_cell(widths[0] + widths[1] + widths[2], 4.4, ln["designation"], align="L",
                           new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1)
            continue
        if lk == "subtotal":
            pdf.set_fill_color(*LIGHT)
            pdf.set_font("dejavu", "B", 8.8)
            pdf.set_text_color(*INK)
            pdf.cell(sum(widths[:-1]), 6.5, (ln.get("designation") or "Sous-total"), align="R", fill=True)
            pdf.cell(widths[-1], 6.5, fmt_money(running), align="R", fill=True, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1.5)
            running = d(0)
            continue
        optional = bool(ln.get("optional"))
        text = ln["designation"] + ("  — en option" if optional else "")
        desc = ln.get("description") or ""
        pdf.set_font("dejavu", "", 9)
        h_title = 5 * len(pdf.multi_cell(widths[0], 5, text, dry_run=True, output="LINES"))
        h_desc = 0
        if desc:
            pdf.set_font("dejavu", "", 7.8)
            h_desc = 4 * len(pdf.multi_cell(widths[0], 4, desc, dry_run=True, output="LINES"))
        h_total = h_title + h_desc + 2.5
        if pdf.get_y() + h_total > 268:
            pdf.add_page()
        x0, y0 = pdf.get_x(), pdf.get_y()
        pdf.set_font("dejavu", "", 9)
        pdf.set_text_color(*INK)
        pdf.multi_cell(widths[0], 5, text, align="L", new_x="LMARGIN", new_y="NEXT")
        if desc:
            pdf.set_x(x0)
            pdf.set_font("dejavu", "", 7.8)
            pdf.set_text_color(*GREY)
            pdf.multi_cell(widths[0], 4, desc, align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.set_xy(x0 + widths[0], y0)
        pdf.set_font("dejavu", "", 9)
        pdf.set_text_color(*INK)
        net = line_net(ln)
        if optional:
            pdf.set_text_color(*GREY)
            options_total += net
        else:
            running += net
        vals = [fmt_qty(ln["quantity"]), ln.get("unit") or "", fmt_money(ln["unit_price"]),
                fmt_rate(ln["vat_rate"]), fmt_money(net)]
        for w, v in zip(widths[1:], vals):
            pdf.cell(w, 5, v, align="R")
        pdf.set_xy(x0, y0 + h_total)
        pdf.set_draw_color(*HAIR)
        pdf.line(16, y0 + h_total - 1, 194, y0 + h_total - 1)
    pdf.table_header = None

    if options_total:
        pdf.ln(2)
        pdf.set_font("dejavu", "", 8.2)
        pdf.set_text_color(*GREY)
        pdf.multi_cell(0, 4.4, f"Options proposées, non comprises dans le total : {fmt_money(options_total)} HT.",
                       new_x="LMARGIN", new_y="NEXT")

    # --- Totaux
    pdf.ln(4)
    if pdf.get_y() > 222:
        pdf.add_page()
    x = 118
    pdf.set_font("dejavu", "", 9)
    pdf.set_text_color(*GREY)
    pdf.set_x(x)
    pdf.cell(46, 6, "Total HT", align="R")
    pdf.set_text_color(*INK)
    pdf.cell(30, 6, fmt_money(t["ht"]), align="R", new_x="LMARGIN", new_y="NEXT")
    for rate, base, tva in t["breakdown"]:
        pdf.set_x(x)
        pdf.set_text_color(*GREY)
        pdf.cell(46, 6, f"TVA {fmt_rate(rate)}", align="R")
        pdf.set_text_color(*INK)
        pdf.cell(30, 6, fmt_money(tva), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.set_x(x)
    pdf.set_fill_color(*INK)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("dejavu", "B", 11)
    label = "Total TTC" if kind != "avoir" else "Total de l'avoir TTC"
    pdf.cell(46, 9, label, align="R", fill=True)
    pdf.cell(30, 9, fmt_money(t["ttc"]) + " ", align="R", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK)
    if kind == "facture" and extra.get("paid", 0) > 0:
        pdf.set_font("dejavu", "", 9)
        pdf.set_x(x)
        pdf.cell(46, 6, "Déjà réglé", align="R")
        pdf.cell(30, 6, fmt_money(extra["paid"]), align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_x(x)
        pdf.set_font("dejavu", "B", 9)
        pdf.cell(46, 6, "Reste à payer", align="R")
        pdf.cell(30, 6, fmt_money(t["ttc"] - money(extra["paid"])), align="R", new_x="LMARGIN", new_y="NEXT")

    # --- Mentions
    pdf.ln(7)
    mentions = []
    if doc.get("notes"):
        mentions.append(("", doc["notes"]))
    if kind == "facture" and seller.get("iban"):
        payment_box(pdf, doc, seller, t, extra)
    if kind == "facture":
        mentions.append(("", "Nature de l'opération : prestations de services."))
        mentions.append(("", penalty_text(doc, seller)))
        if client.get("kind", "pro") == "pro":
            mentions.append(("", seller.get("recovery_fee", "")))
        mentions.append(("", discount_text(doc, seller)))
    if kind == "avoir":
        mentions.append(("", "Avoir à déduire de la facture référencée ci-dessus."))
    for label, m in [x for x in mentions if x[1]]:
        if pdf.get_y() > 262:
            pdf.add_page()
        if label:
            pdf.set_font("dejavu", "B", 8.2)
            pdf.set_text_color(*INK)
            pdf.multi_cell(0, 4.4, label, align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("dejavu", "", 8.2)
        pdf.set_text_color(*GREY if not label else INK)
        pdf.multi_cell(0, 4.4, m, align="L", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(0.6)

    if kind == "devis":
        pdf.ln(6)
        if pdf.get_y() > 228:
            pdf.add_page()
        pdf.set_font("dejavu", "B", 8.5)
        pdf.set_text_color(*INK)
        pdf.set_x(112)
        pdf.cell(82, 6, "Bon pour accord — date et signature", new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*HAIR)
        pdf.rect(112, pdf.get_y(), 82, 28, round_corners=True, corner_radius=2.5)

    pdf.set_title(f"{TITLES[kind]} {doc.get('number') or ''}")
    pdf.set_author(seller.get("company_name", ""))
    pdf.set_creation_date(_creation_dt(doc.get("issue_date")))
    pdf.output(path)
    return path


def _creation_dt(iso):
    from datetime import datetime, timezone
    try:
        dt = date.fromisoformat(str(iso)[:10])
        return datetime(dt.year, dt.month, dt.day, 12, tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
