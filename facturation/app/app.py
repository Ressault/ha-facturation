"""Facturation — application web (Flask) servie dans Home Assistant via Ingress."""
import os
import threading
import time
from datetime import date, timedelta

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request, send_file,
                   url_for)

import calc
import db
import importer
import logic
import superpdp
from calc import money
from pdf import fmt_date, fmt_money, fmt_rate

app = Flask(__name__)
app.secret_key = os.environ.get("FACTU_SECRET", "") or os.urandom(24)
app.jinja_env.filters.update(money=fmt_money, date=fmt_date, rate=fmt_rate)

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
        "octobre", "novembre", "décembre"]
STATUS = {
    "brouillon": ("Brouillon", "grey"), "emise": ("À encaisser", "blue"), "partielle": ("Payée en partie", "amber"),
    "payee": ("Payée", "green"), "annulee": ("Annulée par avoir", "grey"), "rembourse": ("Remboursé", "green"),
    "en_attente": ("En attente", "blue"), "accepte": ("Accepté", "green"), "refuse": ("Refusé", "grey"),
    "facture": ("Facturé", "green"),
}
KIND_LABEL = {"facture": "Facture", "devis": "Devis", "avoir": "Avoir"}


def period_label(period):
    y, m = period.split("-")
    return f"{MOIS[int(m) - 1]} {y}"


app.jinja_env.globals.update(STATUS=STATUS, KIND_LABEL=KIND_LABEL, period_label=period_label)


class IngressMiddleware:
    """Préfixe les URL avec le chemin Ingress de Home Assistant et refuse les accès directs."""

    def __init__(self, wsgi):
        self.wsgi = wsgi
        self.ingress_only = os.environ.get("FACTU_INGRESS_ONLY") == "1"

    def __call__(self, environ, start_response):
        if self.ingress_only and environ.get("REMOTE_ADDR") not in ("172.30.32.2", "127.0.0.1"):
            start_response("403 Forbidden", [("Content-Type", "text/plain; charset=utf-8")])
            return ["Accès uniquement via Home Assistant.".encode()]
        prefix = environ.get("HTTP_X_INGRESS_PATH", "")
        if prefix:
            environ["SCRIPT_NAME"] = prefix.rstrip("/")
        return self.wsgi(environ, start_response)


app.wsgi_app = IngressMiddleware(app.wsgi_app)


def conn():
    return db.connect()


@app.context_processor
def inject_banner():
    c = conn()
    try:
        s = db.get_settings(c)
        today = date.today()
        period = calc.previous_period(today)
        declared = c.execute("SELECT 1 FROM declarations WHERE period = ? AND declared_at IS NOT NULL",
                             (period,)).fetchone()
        banner = None
        first_doc = c.execute("SELECT MIN(issue_date) AS m FROM documents WHERE kind = 'facture' "
                              "AND status != 'brouillon'").fetchone()["m"]
        if not declared and first_doc and first_doc[:7] <= period:
            due_day = int(s.get("ca3_due_day") or 19)
            due = date(today.year, today.month, min(due_day, 28))
            days = (due - today).days
            banner = {"period": period, "label": period_label(period), "due": due.isoformat(),
                      "days": days, "late": days < 0}
        setup_missing = not (s.get("company_name") and s.get("siret") and s.get("vat_number"))
        return {"tva_banner": banner, "setup_missing": setup_missing,
                "pdp_on": logic.pdp_configured(c), "settings": s}
    finally:
        c.close()


@app.errorhandler(logic.BusinessError)
def business_error(e):
    flash(str(e), "error")
    return redirect(request.referrer or url_for("index"))


# -- tableau de bord ----------------------------------------------------------

@app.route("/")
def index():
    c = conn()
    try:
        today = date.today()
        month = today.strftime("%Y-%m")
        start, end = calc.month_bounds(month)
        encaisse = c.execute("SELECT COALESCE(SUM(p.amount),0) s FROM payments p JOIN documents d ON d.id=p.document_id "
                             "WHERE d.kind='facture' AND p.date>=? AND p.date<?", (start, end)).fetchone()["s"]
        factures_mois = c.execute("SELECT COALESCE(SUM(total_ttc),0) s FROM documents WHERE kind='facture' "
                                  "AND status!='brouillon' AND issue_date>=? AND issue_date<?",
                                  (start, end)).fetchone()["s"]
        unpaid = c.execute(
            "SELECT d.*, c.name AS client_name, (SELECT COALESCE(SUM(amount),0) FROM payments WHERE document_id=d.id) AS paid "
            "FROM documents d LEFT JOIN clients c ON c.id=d.client_id WHERE d.kind='facture' "
            "AND d.status IN ('emise','partielle') ORDER BY d.due_date").fetchall()
        reste = sum(money(u["total_ttc"]) - money(u["paid"]) for u in unpaid)
        late = [u for u in unpaid if u["due_date"] and u["due_date"] < today.isoformat()]
        recent = c.execute("SELECT d.*, c.name AS client_name FROM documents d LEFT JOIN clients c ON c.id=d.client_id "
                           "ORDER BY d.id DESC LIMIT 8").fetchall()
        devis_open = c.execute("SELECT COUNT(*) n FROM documents WHERE kind='devis' AND status='en_attente'").fetchone()["n"]
        errors = c.execute("SELECT COUNT(*) n FROM documents WHERE pdp_error IS NOT NULL AND pdp_id IS NULL "
                           "AND status!='brouillon'").fetchone()["n"]
        return render_template("index.html", encaisse=encaisse, factures_mois=factures_mois, unpaid=unpaid,
                               reste=reste, late=late, recent=recent, devis_open=devis_open,
                               month_label=period_label(month), pdp_errors=errors)
    finally:
        c.close()


# -- clients ------------------------------------------------------------------

@app.route("/clients")
def clients():
    c = conn()
    try:
        rows = c.execute(
            "SELECT cl.*, (SELECT COUNT(*) FROM documents d WHERE d.client_id=cl.id AND d.kind='facture' "
            "AND d.status!='brouillon') AS n_fact, (SELECT COALESCE(SUM(total_ht),0) FROM documents d WHERE "
            "d.client_id=cl.id AND d.kind='facture' AND d.status!='brouillon') AS ca FROM clients cl ORDER BY cl.name"
        ).fetchall()
        return render_template("clients.html", clients=rows)
    finally:
        c.close()


@app.route("/clients/nouveau", methods=["GET", "POST"])
@app.route("/clients/<int:client_id>", methods=["GET", "POST"])
def client_form(client_id=None):
    c = conn()
    try:
        if request.method == "POST":
            f = request.form
            vals = {k: (f.get(k) or "").strip() for k in
                    ["kind", "name", "siren", "vat_number", "address", "post_code", "city", "country", "email",
                     "phone", "pdp_address_scheme", "pdp_address_value", "notes"]}
            vals["siren"] = vals["siren"].replace(" ", "")
            vals["country"] = (vals["country"] or "FR").upper()
            if not vals["name"]:
                raise logic.BusinessError("Le nom du client est obligatoire.")
            if vals["kind"] == "pro" and vals["siren"] and not vals["vat_number"] and vals["country"] == "FR":
                vals["vat_number"] = logic.vat_number_from_siren(vals["siren"])
            if client_id:
                c.execute(f"UPDATE clients SET {', '.join(k + '=?' for k in vals)} WHERE id=?",
                          (*vals.values(), client_id))
            else:
                client_id = c.execute(f"INSERT INTO clients ({', '.join(vals)}) VALUES ({', '.join('?' for _ in vals)})",
                                      tuple(vals.values())).lastrowid
            c.commit()
            flash("Client enregistré.", "ok")
            nxt = request.args.get("next")
            if nxt == "facture":
                return redirect(url_for("doc_new", kind="facture", client_id=client_id))
            if nxt == "devis":
                return redirect(url_for("doc_new", kind="devis", client_id=client_id))
            return redirect(url_for("client_form", client_id=client_id))
        client = c.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone() if client_id else None
        docs = c.execute("SELECT * FROM documents WHERE client_id=? ORDER BY issue_date DESC, id DESC",
                         (client_id,)).fetchall() if client_id else []
        return render_template("client_form.html", client=client, docs=docs)
    finally:
        c.close()


@app.route("/api/siren/<siren>")
def api_siren(siren):
    c = conn()
    try:
        siren = "".join(ch for ch in siren if ch.isdigit())[:9]
        try:
            info = logic.lookup_siren(c, siren)
        except superpdp.PDPError as e:
            return jsonify({"error": str(e)}), 502
        except Exception as e:  # réseau
            return jsonify({"error": f"Recherche impossible : {e}"}), 502
        info["vat_number"] = logic.vat_number_from_siren(siren)
        return jsonify(info)
    finally:
        c.close()


# -- documents ----------------------------------------------------------------

@app.route("/documents")
def documents():
    kind = request.args.get("kind", "facture")
    year = request.args.get("annee", "")
    c = conn()
    try:
        q = ("SELECT d.*, c.name AS client_name, (SELECT COALESCE(SUM(amount),0) FROM payments WHERE document_id=d.id) "
             "AS paid FROM documents d LEFT JOIN clients c ON c.id=d.client_id WHERE d.kind=?")
        args = [kind]
        if year:
            q += " AND substr(d.issue_date,1,4)=?"
            args.append(year)
        q += " ORDER BY (d.status='brouillon') DESC, d.issue_date DESC, d.id DESC"
        rows = c.execute(q, args).fetchall()
        years = [r["y"] for r in c.execute("SELECT DISTINCT substr(issue_date,1,4) y FROM documents WHERE kind=? "
                                           "ORDER BY y DESC", (kind,)).fetchall() if r["y"]]
        return render_template("documents.html", docs=rows, kind=kind, years=years, year=year)
    finally:
        c.close()


@app.route("/documents/nouveau")
def doc_new():
    kind = request.args.get("kind", "facture")
    if kind not in ("facture", "devis"):
        abort(400)
    c = conn()
    try:
        s = db.get_settings(c)
        today = date.today()
        doc = {"id": None, "kind": kind, "client_id": request.args.get("client_id", type=int),
               "issue_date": today.isoformat(), "status": "brouillon",
               "validity_date": (today + timedelta(days=int(s.get("devis_validity_days") or 30))).isoformat()
               if kind == "devis" else None, "due_date": None, "subject": "", "notes": "", "service_date": ""}
        lines = [{"designation": "", "description": "", "quantity": 1, "unit": "u", "unit_price": "",
                  "vat_rate": float(s.get("default_vat_rate") or 20)}]
        return render_template("doc_form.html", doc=doc, lines=lines, clients=all_clients(c),
                               catalog=catalog(c), preview=logic.preview_number(c, kind, today.isoformat()))
    finally:
        c.close()


@app.route("/documents/<int:doc_id>/modifier")
def doc_edit(doc_id):
    c = conn()
    try:
        doc, lines = logic.get_doc(c, doc_id)
        if doc["kind"] != "devis" and doc["status"] != "brouillon":
            flash("Une facture émise ne se modifie plus : créez un avoir.", "error")
            return redirect(url_for("doc_view", doc_id=doc_id))
        return render_template("doc_form.html", doc=doc, lines=lines, clients=all_clients(c), catalog=catalog(c),
                               preview=None)
    finally:
        c.close()


@app.route("/documents/enregistrer", methods=["POST"])
def doc_save():
    f = request.form
    doc_id = f.get("id", type=int)
    lines = []
    for i in range(len(f.getlist("designation"))):
        lines.append({k: f.getlist(k)[i] for k in
                      ["designation", "description", "quantity", "unit", "unit_price", "vat_rate"]})
    with db.transaction() as c:
        doc_id = logic.save_document(c, doc_id, f.to_dict(), lines)
        if f.get("action") == "valider":
            logic.validate_document(c, doc_id)
    if f.get("action") == "valider":
        return after_validation(doc_id)
    flash("Enregistré.", "ok")
    return redirect(url_for("doc_view", doc_id=doc_id))


def after_validation(doc_id):
    c = conn()
    try:
        s = db.get_settings(c)
        doc, _ = logic.get_doc(c, doc_id)
        flash(f"{KIND_LABEL[doc['kind']]} {doc['number']} émise.", "ok")
        if logic.pdp_configured(c) and s.get("pdp_auto_send") == "1":
            try:
                pid = logic.send_to_pdp(c, doc_id)
                c.commit()
                if pid:
                    flash("Transmise au client via la plateforme agréée.", "ok")
            except logic.BusinessError as e:
                c.commit()
                flash(f"Envoi à la plateforme agréée impossible : {e}", "error")
    finally:
        c.close()
    return redirect(url_for("doc_view", doc_id=doc_id))


@app.route("/documents/<int:doc_id>")
def doc_view(doc_id):
    c = conn()
    try:
        doc, lines = logic.get_doc(c, doc_id)
        client = db.loads(doc["client_snapshot"]) or dict(
            c.execute("SELECT * FROM clients WHERE id=?", (doc["client_id"],)).fetchone() or {})
        t = calc.totals(lines)
        payments = c.execute("SELECT * FROM payments WHERE document_id=? ORDER BY date", (doc_id,)).fetchall()
        paid = logic.paid_amount(c, doc_id)
        credited = logic.credited_amount(c, doc_id) if doc["kind"] == "facture" else 0
        avoirs = c.execute("SELECT * FROM documents WHERE avoir_of_id=?", (doc_id,)).fetchall()
        origin = c.execute("SELECT id, number, kind FROM documents WHERE id IN (?, ?)",
                           (doc["avoir_of_id"] or 0, doc["source_devis_id"] or 0)).fetchall()
        invoices_from = c.execute("SELECT id, number, status FROM documents WHERE source_devis_id=?",
                                  (doc_id,)).fetchall()
        remaining = money(doc["total_ttc"]) - paid - money(credited)
        return render_template("doc_view.html", doc=doc, lines=lines, client=client, t=t, payments=payments,
                               paid=paid, credited=credited, remaining=remaining, avoirs=avoirs, origin=origin,
                               invoices_from=invoices_from, today=date.today().isoformat())
    finally:
        c.close()


@app.route("/documents/<int:doc_id>/pdf")
def doc_pdf(doc_id):
    c = conn()
    try:
        doc, lines = logic.get_doc(c, doc_id)
        path = doc["pdf_path"]
        if doc["status"] == "brouillon" or not path or not os.path.exists(path):
            path, *_ = logic.render_pdf(c, doc, lines, final=doc["status"] != "brouillon")
            c.commit()
        name = f"{KIND_LABEL[doc['kind']]} {doc['number'] or 'brouillon'}.pdf"
        return send_file(path, mimetype="application/pdf", download_name=name,
                         as_attachment=request.args.get("dl") == "1")
    finally:
        c.close()


@app.route("/documents/<int:doc_id>/emettre", methods=["POST"])
def doc_validate(doc_id):
    with db.transaction() as c:
        logic.validate_document(c, doc_id)
    return after_validation(doc_id)


@app.route("/documents/<int:doc_id>/envoyer", methods=["POST"])
def doc_send(doc_id):
    c = conn()
    try:
        try:
            pid = logic.send_to_pdp(c, doc_id)
            flash("Transmise via la plateforme agréée." if pid else "Transmission effectuée.", "ok")
        finally:
            c.commit()
    finally:
        c.close()
    return redirect(url_for("doc_view", doc_id=doc_id))


@app.route("/documents/<int:doc_id>/facturer", methods=["POST"])
def doc_to_invoice(doc_id):
    with db.transaction() as c:
        new_id = logic.duplicate_as(c, doc_id, "facture")
        c.execute("UPDATE documents SET status='accepte' WHERE id=? AND status='en_attente'", (doc_id,))
    flash("Facture préparée à partir du devis : vérifiez puis émettez-la.", "ok")
    return redirect(url_for("doc_edit", doc_id=new_id))


@app.route("/documents/<int:doc_id>/avoir", methods=["POST"])
def doc_to_avoir(doc_id):
    with db.transaction() as c:
        doc, _ = logic.get_doc(c, doc_id)
        if doc["kind"] != "facture" or doc["status"] == "brouillon":
            raise logic.BusinessError("Un avoir se fait sur une facture émise.")
        new_id = logic.duplicate_as(c, doc_id, "avoir", avoir=True)
    flash("Avoir préparé : ajustez les lignes (montants positifs) puis émettez-le.", "ok")
    return redirect(url_for("doc_edit", doc_id=new_id))


@app.route("/documents/<int:doc_id>/dupliquer", methods=["POST"])
def doc_duplicate(doc_id):
    with db.transaction() as c:
        doc, _ = logic.get_doc(c, doc_id)
        kind = "devis" if doc["kind"] == "devis" else "facture"
        new_id = logic.duplicate_as(c, doc_id, kind)
        if kind == "devis":
            n = logic.next_number(c, "devis", date.today().isoformat())
            c.execute("UPDATE documents SET number=?, status='en_attente', source_devis_id=NULL WHERE id=?", (n, new_id))
    return redirect(url_for("doc_edit", doc_id=new_id))


@app.route("/documents/<int:doc_id>/statut-devis", methods=["POST"])
def devis_status(doc_id):
    status = request.form.get("status")
    if status not in ("en_attente", "accepte", "refuse"):
        abort(400)
    with db.transaction() as c:
        c.execute("UPDATE documents SET status=? WHERE id=? AND kind='devis'", (status, doc_id))
    return redirect(url_for("doc_view", doc_id=doc_id))


@app.route("/documents/<int:doc_id>/supprimer", methods=["POST"])
def doc_delete(doc_id):
    with db.transaction() as c:
        doc, _ = logic.get_doc(c, doc_id)
        if doc["kind"] != "devis" and doc["status"] != "brouillon":
            raise logic.BusinessError("Une facture émise ne peut pas être supprimée : faites un avoir.")
        c.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    flash("Supprimé.", "ok")
    return redirect(url_for("documents", kind=doc["kind"]))


@app.route("/documents/<int:doc_id>/paiement", methods=["POST"])
def doc_payment(doc_id):
    f = request.form
    with db.transaction() as c:
        logic.add_payment(c, doc_id, f.get("date") or date.today().isoformat(), f.get("amount"), f.get("method"))
    c = conn()
    try:
        if logic.pdp_configured(c):
            try:
                logic.report_payments(c, doc_id)
            except superpdp.PDPError as e:
                flash(f"Encaissement enregistré, mais non transmis : {e}", "error")
            c.commit()
    finally:
        c.close()
    flash("Encaissement enregistré.", "ok")
    return redirect(url_for("doc_view", doc_id=doc_id))


@app.route("/paiements/<int:payment_id>/supprimer", methods=["POST"])
def payment_delete(payment_id):
    with db.transaction() as c:
        p = c.execute("SELECT document_id FROM payments WHERE id=?", (payment_id,)).fetchone()
        logic.delete_payment(c, payment_id)
    return redirect(url_for("doc_view", doc_id=p["document_id"]) if p else url_for("index"))


# -- achats -------------------------------------------------------------------

@app.route("/achats")
def purchases():
    c = conn()
    try:
        rows = c.execute("SELECT * FROM purchases ORDER BY (deduction_date IS NULL) DESC, "
                         "COALESCE(deduction_date, invoice_date) DESC, id DESC").fetchall()
        return render_template("purchases.html", purchases=rows, today=date.today().isoformat())
    finally:
        c.close()


@app.route("/achats/nouveau", methods=["GET", "POST"])
@app.route("/achats/<int:pid>", methods=["GET", "POST"])
def purchase_form(pid=None):
    c = conn()
    try:
        if request.method == "POST":
            f = request.form
            ht, tva = money(f.get("total_ht") or 0), money(f.get("total_tva") or 0)
            ttc = money(f.get("total_ttc") or 0) or ht + tva
            vals = {"supplier": f.get("supplier", "").strip(), "invoice_number": f.get("invoice_number"),
                    "invoice_date": f.get("invoice_date") or None, "description": f.get("description"),
                    "total_ht": float(ht), "total_tva": float(tva), "total_ttc": float(ttc),
                    "category": f.get("category") or "biens_services",
                    "paid_date": f.get("paid_date") or None,
                    "deduction_date": f.get("deduction_date") or f.get("paid_date") or None}
            if not vals["supplier"]:
                raise logic.BusinessError("Indiquez le fournisseur.")
            if pid:
                c.execute(f"UPDATE purchases SET {', '.join(k + '=?' for k in vals)} WHERE id=?", (*vals.values(), pid))
            else:
                c.execute(f"INSERT INTO purchases ({', '.join(vals)}) VALUES ({', '.join('?' for _ in vals)})",
                          tuple(vals.values()))
            c.commit()
            flash("Achat enregistré.", "ok")
            return redirect(url_for("purchases"))
        p = c.execute("SELECT * FROM purchases WHERE id=?", (pid,)).fetchone() if pid else None
        return render_template("purchase_form.html", p=p, today=date.today().isoformat())
    finally:
        c.close()


@app.route("/achats/<int:pid>/payer", methods=["POST"])
def purchase_paid(pid):
    d_ = request.form.get("date") or date.today().isoformat()
    with db.transaction() as c:
        c.execute("UPDATE purchases SET paid_date=?, deduction_date=COALESCE(deduction_date, ?) WHERE id=?",
                  (d_, d_, pid))
    return redirect(request.referrer or url_for("purchases"))


@app.route("/achats/<int:pid>/supprimer", methods=["POST"])
def purchase_delete(pid):
    with db.transaction() as c:
        c.execute("DELETE FROM purchases WHERE id=? AND source='manuel'", (pid,))
    return redirect(url_for("purchases"))


@app.route("/achats/<int:pid>/fichier")
def purchase_file(pid):
    c = conn()
    try:
        p = c.execute("SELECT file_path FROM purchases WHERE id=?", (pid,)).fetchone()
        if not p or not p["file_path"] or not os.path.exists(p["file_path"]):
            abort(404)
        return send_file(p["file_path"], mimetype="application/pdf")
    finally:
        c.close()


# -- TVA ----------------------------------------------------------------------

@app.route("/tva")
def tva():
    period = request.args.get("mois") or calc.previous_period()
    c = conn()
    try:
        res = calc.compute_period(c, period)
        decl = c.execute("SELECT * FROM declarations WHERE period=?", (period,)).fetchone()
        start, end = calc.month_bounds(period)
        undated = c.execute("SELECT * FROM purchases WHERE deduction_date IS NULL ORDER BY invoice_date").fetchall()
        unpaid_reported = c.execute(
            "SELECT COUNT(*) n FROM payments WHERE pdp_reported=0 AND pdp_error IS NOT NULL").fetchone()["n"]
        history = c.execute("SELECT * FROM declarations ORDER BY period DESC LIMIT 12").fetchall()
        return render_template("tva.html", r=res, period=period, decl=decl, undated=undated,
                               prev=calc.previous_of(period), next=calc.next_period(period),
                               is_current=period == calc.previous_period(), history=history,
                               unpaid_reported=unpaid_reported)
    finally:
        c.close()


@app.route("/tva/declarer", methods=["POST"])
def tva_declare():
    period = request.form["period"]
    with db.transaction() as c:
        res = calc.compute_period(c, period)
        if request.form.get("undo"):
            c.execute("DELETE FROM declarations WHERE period=?", (period,))
        else:
            c.execute("INSERT INTO declarations(period, declared_at, tva_nette, credit_reporte) VALUES (?, "
                      "CURRENT_TIMESTAMP, ?, ?) ON CONFLICT(period) DO UPDATE SET declared_at=CURRENT_TIMESTAMP, "
                      "tva_nette=excluded.tva_nette, credit_reporte=excluded.credit_reporte",
                      (period, res["l28"], res["l27"]))
    flash(f"TVA de {period_label(period)} marquée comme déclarée." if not request.form.get("undo")
          else "Déclaration annulée.", "ok")
    return redirect(url_for("tva", mois=period))


# -- réglages -----------------------------------------------------------------

SETTINGS_FIELDS = ["company_name", "company_trade_name", "address", "post_code", "city", "siren", "siret",
                   "vat_number", "ape", "email", "phone", "iban", "bic", "rcs_rm", "insurance", "invoice_pattern",
                   "avoir_pattern", "devis_pattern", "payment_days", "devis_validity_days", "default_vat_rate",
                   "late_penalty", "recovery_fee", "discount_terms", "footer_note", "ca3_due_day", "pdp_client_id",
                   "pdp_endpoint", "pdp_auto_send", "ereporting_start"]


@app.route("/reglages", methods=["GET", "POST"])
def settings_page():
    c = conn()
    try:
        if request.method == "POST":
            f = request.form
            vals = {k: (f.get(k) or "").strip() for k in SETTINGS_FIELDS}
            vals["pdp_auto_send"] = "1" if f.get("pdp_auto_send") else "0"
            vals["siret"] = vals["siret"].replace(" ", "")
            vals["siren"] = (vals["siren"] or vals["siret"][:9]).replace(" ", "")
            if vals["siren"] and not vals["vat_number"]:
                vals["vat_number"] = logic.vat_number_from_siren(vals["siren"])
            if f.get("pdp_client_secret"):
                vals["pdp_client_secret"] = f["pdp_client_secret"].strip()
            counters = {k: f.get(k) for k in f if k.startswith(("invoice_next", "avoir_next", "devis_next")) and f.get(k)}
            db.set_settings({**vals, **counters}, c)
            c.commit()
            flash("Réglages enregistrés.", "ok")
            return redirect(url_for("settings_page"))
        s = db.get_settings(c)
        year = date.today().year
        counters = {}
        for k in ("invoice", "avoir", "devis"):
            _, ck = logic.counter_key(s, k, year)
            counters[k] = (ck, s.get(ck) or "1")
        journal = c.execute("SELECT * FROM journal ORDER BY id DESC LIMIT 30").fetchall()
        return render_template("settings.html", s=s, counters=counters, year=year, journal=journal)
    finally:
        c.close()


@app.route("/reglages/tester", methods=["POST"])
def settings_test():
    c = conn()
    try:
        try:
            out = logic.test_connection(c)
            c.commit()
            comp = out["company"]
            name = comp.get("formal_name") or comp.get("name") or "?"
            msg = f"Connexion réussie : {name}."
            if out.get("address"):
                msg += f" Adresse de facturation électronique : {out['address'][1]} ({out['address'][0]})."
            flash(msg, "ok")
        except (superpdp.PDPError, Exception) as e:  # noqa: BLE001
            flash(f"Échec de la connexion : {e}", "error")
    finally:
        c.close()
    return redirect(url_for("settings_page"))


@app.route("/synchroniser", methods=["POST"])
def sync_now():
    msgs = run_sync()
    for level, m in msgs:
        flash(m, level)
    return redirect(request.referrer or url_for("index"))


@app.route("/import", methods=["GET", "POST"])
def import_page():
    if request.method == "POST":
        files = request.files.getlist("files")
        c = conn()
        try:
            report = importer.import_files(c, files)
            c.commit()
        finally:
            c.close()
        return render_template("import.html", report=report)
    return render_template("import.html", report=None)


@app.route("/sante")
def health():
    return jsonify({"ok": True})


# -- utilitaires --------------------------------------------------------------

def all_clients(c):
    return c.execute("SELECT id, name, kind, email FROM clients ORDER BY name").fetchall()


def catalog(c):
    """Lignes déjà facturées, pour l'autocomplétion."""
    return c.execute("SELECT designation, unit, unit_price, vat_rate, MAX(id) FROM lines GROUP BY designation "
                     "ORDER BY MAX(id) DESC LIMIT 200").fetchall()


def run_sync():
    msgs = []
    c = conn()
    try:
        if not logic.pdp_configured(c):
            return [("error", "Plateforme agréée non configurée (Réglages).")]
        try:
            logic.sync_statuses(c)
            n = logic.fetch_inbox(c)
            c.commit()
            msgs.append(("ok", f"Synchronisé. {n} nouvelle(s) facture(s) fournisseur." if n else "Synchronisé."))
        except (superpdp.PDPError, Exception) as e:  # noqa: BLE001
            c.commit()
            msgs.append(("error", f"Synchronisation impossible : {e}"))
    finally:
        c.close()
    return msgs


def background_sync():
    while True:
        time.sleep(1800)
        try:
            run_sync()
        except Exception:  # noqa: BLE001
            pass


def main():
    db.init_db()
    threading.Thread(target=background_sync, daemon=True).start()
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("FACTU_PORT", "8099")), threads=4)


if __name__ == "__main__":
    main()
