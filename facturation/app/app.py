"""Facturation — application web (Flask) servie dans Home Assistant via Ingress."""
import csv
import io
import os
import threading
import time
import zipfile
from datetime import date, timedelta

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request, send_file,
                   url_for)

import backup
import calc
import db
import debug_routes
import importer
import logic
import monae
import superpdp
from calc import NATURES, PAYMENT_TERMS, d, money
from pdf import fmt_date, fmt_money, fmt_qty, fmt_rate

app = Flask(__name__)
app.secret_key = os.environ.get("FACTU_SECRET", "") or os.urandom(24)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
def fmt_int(v):
    try:
        return f"{int(round(float(v))):,}".replace(",", "\u202f")
    except (TypeError, ValueError):
        return v


app.jinja_env.filters.update(money=fmt_money, date=fmt_date, rate=fmt_rate, qty=fmt_qty, int_fr=fmt_int)

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
        "octobre", "novembre", "décembre"]
MOIS_COURT = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
STATUS = {
    "brouillon": ("Brouillon", "green"), "emise": ("À encaisser", "blue"), "partielle": ("Payée en partie", "amber"),
    "payee": ("Payée", "grey"), "annulee": ("Annulée", "grey"), "rembourse": ("Remboursé", "grey"),
    "en_attente": ("En attente", "blue"), "accepte": ("Accepté", "green"), "refuse": ("Refusé", "grey"),
    "facture": ("Facturé", "grey"),
}
KIND_LABEL = {"facture": "Facture", "devis": "Devis", "avoir": "Avoir"}
TERMS = dict(PAYMENT_TERMS)
FIRST_YEAR = 2016


def period_groups(today=None):
    """Choix du filtre Période, groupés comme dans MonAE : mois de l'année, trimestres, années."""
    today = today or date.today()
    y = today.year
    return [
        ("", [("tout", "Indifférent"), ("12m", "12 derniers mois")]),
        (f"Mois {y}", [(f"m-{y}-{m:02d}", MOIS[m - 1].capitalize()) for m in range(1, 13)]),
        (f"Trimestres {y}", [(f"t-{y}-{q}", f"T{q} {y}") for q in range(1, 5)]),
        ("Années", [(f"a-{a}", str(a)) for a in range(y, min(FIRST_YEAR, y) - 1, -1)]),
        ("", [("perso", "Personnalisée…")]),
    ]


def period_label(period):
    if "-T" in period:
        return f"{period[-2:]} {period[:4]}"
    y, m = period.split("-")[:2]
    return f"{MOIS[int(m) - 1]} {y}"


def period_range(code, start=None, end=None, today=None):
    """(début inclus, fin exclue) au format ISO pour un code de période."""
    today = today or date.today()
    y, m = today.year, today.month
    code = code or "tout"
    if code == "12m":
        try:
            s_ = today.replace(year=y - 1)
        except ValueError:  # 29 février
            s_ = today.replace(year=y - 1, day=28)
        return (s_ + timedelta(days=1)).isoformat(), (today + timedelta(days=1)).isoformat()
    if code.startswith("m-") and len(code) == 9:
        return calc.month_bounds(code[2:])
    if code.startswith("t-") and len(code) == 8:
        return calc.quarter_bounds(f"{code[2:6]}-T{code[7]}")
    if code.startswith("a-") and code[2:].isdigit():
        a = int(code[2:])
        return f"{a:04d}-01-01", f"{a + 1:04d}-01-01"
    if code == "mois":
        return calc.month_bounds(f"{y:04d}-{m:02d}")
    if code == "mois-1":
        return calc.month_bounds(calc.previous_period(today))
    if code == "trimestre":
        return calc.quarter_bounds(calc.quarter_of(f"{y:04d}-{m:02d}"))
    if code == "trimestre-1":
        return calc.quarter_bounds(calc.previous_quarter(today))
    if code == "annee":
        return f"{y:04d}-01-01", f"{y + 1:04d}-01-01"
    if code == "annee-1":
        return f"{y - 1:04d}-01-01", f"{y:04d}-01-01"
    if code == "perso" and (start or end):
        e = (date.fromisoformat(end) + timedelta(days=1)).isoformat() if end else "9999-12-31"
        return start or "0000-01-01", e
    return "0000-01-01", "9999-12-31"


def period_choice(code, today=None):
    """Ramène les anciens codes (mois, trimestre…) sur une entrée de la liste."""
    today = today or date.today()
    y, m = today.year, today.month
    if code == "mois":
        return f"m-{y}-{m:02d}"
    if code == "trimestre":
        return f"t-{y}-{(m - 1) // 3 + 1}"
    if code == "annee":
        return f"a-{y}"
    if code == "annee-1":
        return f"a-{y - 1}"
    if code == "mois-1":
        return "m-" + calc.previous_period(today)
    if code == "trimestre-1":
        q = calc.previous_quarter(today)
        return f"t-{q[:4]}-{q[-1]}"
    return code


URSSAF_URL = "https://www.autoentrepreneur.urssaf.fr/portail/accueil.html"
IMPOTS_URL = "https://cfspro.impots.gouv.fr/"  # page de connexion de l'espace professionnel
PLAFOND_MICRO_SERVICES = 83600  # plafond micro BIC services / BNC 2026-2028

app.jinja_env.globals.update(URSSAF_URL=URSSAF_URL, IMPOTS_URL=IMPOTS_URL, STATUS=STATUS, KIND_LABEL=KIND_LABEL, period_label=period_label, NATURES=NATURES,
                             TERMS=TERMS, PAYMENT_TERMS=PAYMENT_TERMS, period_groups=period_groups, MOIS_COURT=MOIS_COURT)


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
def inject_globals():
    c = conn()
    try:
        s = db.get_settings(c)
        try:
            tasks = logic.tasks(c)
        except Exception:  # noqa: BLE001
            tasks = []
        setup_missing = not (s.get("company_name") and s.get("siret") and s.get("vat_number"))
        return {"tasks": tasks, "setup_missing": setup_missing, "pdp_on": logic.pdp_configured(c), "settings": s,
                "today": date.today().isoformat()}
    finally:
        c.close()


@app.errorhandler(logic.BusinessError)
def business_error(e):
    flash(str(e), "error")
    return redirect(request.referrer or url_for("index"))


# -- tableau de bord ----------------------------------------------------------

def sums(c, start, end):
    """Encaissé, facturé, achats (HT et TTC) entre start et end."""
    enc = {"ht": 0.0, "ttc": 0.0}
    for r in calc.cash_rows(c, start, end):
        sign = -1 if r["kind"] == "avoir" else 1
        ratio = (r["total_ht"] / r["total_ttc"]) if r["total_ttc"] else 1
        enc["ttc"] += sign * r["amount"]
        enc["ht"] += sign * r["amount"] * ratio
    fac = c.execute(
        "SELECT COALESCE(SUM(CASE WHEN kind='avoir' THEN -total_ttc ELSE total_ttc END),0) ttc, "
        "COALESCE(SUM(CASE WHEN kind='avoir' THEN -total_ht ELSE total_ht END),0) ht FROM documents "
        "WHERE kind IN ('facture','avoir') AND status != 'brouillon' AND issue_date >= ? AND issue_date < ?",
        (start, end)).fetchone()
    ach = c.execute("SELECT COALESCE(SUM(total_ttc),0) ttc, COALESCE(SUM(total_ht),0) ht FROM purchases "
                    "WHERE COALESCE(draft, 0) = 0 AND invoice_date >= ? AND invoice_date < ?", (start, end)).fetchone()
    return {"enc": enc, "fac": {"ht": fac["ht"], "ttc": fac["ttc"]},
            "ach": {"ht": ach["ht"], "ttc": ach["ttc"]}}


def open_invoices(c):
    rows = c.execute(
        "SELECT d.*, cl.name AS client_name, "
        "(SELECT COALESCE(SUM(amount),0) FROM payments WHERE document_id=d.id) AS paid, "
        "(SELECT COALESCE(SUM(total_ttc),0) FROM documents a WHERE a.avoir_of_id=d.id AND a.kind='avoir' "
        " AND a.status!='brouillon') AS credited "
        "FROM documents d LEFT JOIN clients cl ON cl.id=d.client_id WHERE d.kind='facture' "
        "AND d.status IN ('emise','partielle') ORDER BY d.due_date, d.id").fetchall()
    out = []
    for r in rows:
        rest = money(r["total_ttc"]) - money(r["paid"]) - money(r["credited"])
        if rest <= 0:
            continue
        ratio = d(r["total_ht"]) / d(r["total_ttc"]) if r["total_ttc"] else d(1)
        out.append(dict(r, rest_ttc=float(rest), rest_ht=float(money(rest * ratio))))
    return out


@app.route("/")
def index():
    c = conn()
    try:
        today = date.today()
        y = today.year
        month = today.strftime("%Y-%m")
        q = calc.quarter_of(month)
        m_s, m_e = calc.month_bounds(month)
        q_s, q_e = calc.quarter_bounds(q)
        stats = {"mois": sums(c, m_s, m_e), "trimestre": sums(c, q_s, q_e),
                 "annee": sums(c, f"{y}-01-01", f"{y + 1}-01-01")}
        unpaid = open_invoices(c)
        late = [u for u in unpaid if u["due_date"] and u["due_date"] < today.isoformat()]
        rest = {"ht": sum(u["rest_ht"] for u in unpaid), "ttc": sum(u["rest_ttc"] for u in unpaid)}
        late_sum = {"ht": sum(u["rest_ht"] for u in late), "ttc": sum(u["rest_ttc"] for u in late)}
        drafts = c.execute("SELECT d.*, cl.name AS client_name FROM documents d LEFT JOIN clients cl ON "
                           "cl.id=d.client_id WHERE d.status='brouillon' ORDER BY d.issue_date DESC, d.id DESC"
                           ).fetchall()
        devis = c.execute("SELECT d.*, cl.name AS client_name FROM documents d LEFT JOIN clients cl ON "
                          "cl.id=d.client_id WHERE d.kind='devis' AND d.status='en_attente' "
                          "ORDER BY d.issue_date DESC LIMIT 100").fetchall()
        chart = []
        yy, mm = today.year, today.month
        for i in range(11, -1, -1):
            k = yy * 12 + (mm - 1) - i
            per = f"{k // 12:04d}-{k % 12 + 1:02d}"
            s_, e_ = calc.month_bounds(per)
            sm = sums(c, s_, e_)
            chart.append({"label": MOIS_COURT[k % 12], "year": k // 12, "enc": sm["enc"], "fac": sm["fac"]})
        errors = c.execute("SELECT COUNT(*) n FROM documents WHERE pdp_error IS NOT NULL AND pdp_id IS NULL "
                           "AND status!='brouillon'").fetchone()["n"]
        try:
            same_day = today.replace(year=y - 1)
        except ValueError:
            same_day = today.replace(year=y - 1, day=28)
        last_year = sums(c, f"{y - 1}-01-01", (same_day + timedelta(days=1)).isoformat())
        last_year_full = sums(c, f"{y - 1}-01-01", f"{y}-01-01")
        # encaissements cumulés mois par mois, année en cours et 3 précédentes
        series, years_cmp = [], []
        for yy_ in range(y, y - 4, -1):
            acc, pts = {"ht": 0.0, "ttc": 0.0}, []
            for m in range(1, 13):
                if yy_ == y and m > today.month:
                    break
                ms, me = calc.month_bounds(f"{yy_}-{m:02d}")
                e_ = sums(c, ms, me)["enc"]
                acc = {"ht": acc["ht"] + e_["ht"], "ttc": acc["ttc"] + e_["ttc"]}
                pts.append(dict(acc))
            series.append({"year": yy_, "pts": pts})
            years_cmp.append({"year": yy_, "total": pts[-1] if pts else {"ht": 0, "ttc": 0}})
        ca_year = stats["annee"]["enc"]["ht"]
        plafond = {"limit": PLAFOND_MICRO_SERVICES, "ca": ca_year,
                   "pct": min(100, round(100 * ca_year / PLAFOND_MICRO_SERVICES, 1)) if ca_year > 0 else 0}
        transfer = None
        if request.args.get("virement") or request.args.get("montant"):
            transfer = logic.match_transfer(c, request.args.get("virement"), request.args.get("montant"))
            transfer["label"] = request.args.get("virement") or ""
        return render_template("index.html", stats=stats, unpaid=unpaid, late=late, rest=rest, late_sum=late_sum,
                               drafts=drafts, devis=devis, chart=chart, month_label=period_label(month),
                               quarter_label=f"T{q[-1]} {q[:4]}", year=y, pdp_errors=errors, last_year=last_year,
                               last_year_full=last_year_full, series=series, years_cmp=years_cmp,
                               layout=dash_layout(c), hello_name=hello_name(db.get_settings(c)),
                               plafond=plafond, arrears=logic.urssaf_arrears(c), transfer=transfer)
    finally:
        c.close()


def hello_name(s):
    """Prénom affiché sur l'accueil : réglage, sinon le nom de l'utilisateur Home Assistant."""
    if (s.get("first_name") or "").strip():
        return s["first_name"].strip()
    ha = (request.headers.get("X-Remote-User-Display-Name") or request.headers.get("X-Remote-User-Name") or "").strip()
    if ha:
        return ha.split()[0].capitalize()
    return ""


DASH_DEFAULT = [("enc_mois", 3), ("enc_annee", 6), ("reste", 3), ("facture", 3), ("raccourcis", 3),
                ("graphique", 8), ("trimestre", 4), ("virement", 12), ("listes", 12)]


def dash_layout(c):
    """Disposition des tuiles : ordre, largeur (colonnes sur 12) et hauteur (rangées, 0 = automatique)."""
    import json
    known = dict(DASH_DEFAULT)
    try:
        saved = json.loads(db.get_settings(c).get("dash_layout") or "[]")
    except ValueError:
        saved = []
    out, seen = [], set()
    for t in saved:
        if isinstance(t, dict) and t.get("id") in known and t["id"] not in seen:
            try:
                w, h = int(t.get("w") or known[t["id"]]), int(t.get("h") or 0)
            except (TypeError, ValueError):
                w, h = known[t["id"]], 0
            out.append({"id": t["id"], "w": min(12, max(2, w)), "h": min(30, max(0, h))})
            seen.add(t["id"])
    out += [{"id": k, "w": w, "h": 0} for k, w in DASH_DEFAULT if k not in seen]
    return out


@app.route("/preferences", methods=["POST"])
def save_prefs():
    data = request.get_json(silent=True) or {}
    vals = {}
    if data.get("ui_theme") in ("light", "dark"):
        vals["ui_theme"] = data["ui_theme"]
    if data.get("ui_mode") in ("ht", "ttc"):
        vals["ui_mode"] = data["ui_mode"]
    if vals:
        with db.transaction() as c:
            db.set_settings(vals, c)
    return jsonify({"ok": True})


@app.route("/accueil/disposition", methods=["POST"])
def dash_save():
    import json
    data = request.get_json(silent=True) or {}
    items = data.get("tiles")
    with db.transaction() as c:
        if items is None or data.get("reset"):
            db.set_settings({"dash_layout": ""}, c)
        else:
            db.set_settings({"dash_layout": json.dumps([{"id": str(t.get("id")), "w": int(t.get("w") or 3),
                                                         "h": int(t.get("h") or 0)}
                                                        for t in items if isinstance(t, dict)])}, c)
    return jsonify({"ok": True})


@app.route("/taches/<path:task_id>", methods=["POST"])
def task_toggle(task_id):
    with db.transaction() as c:
        logic.mark_task(c, task_id, request.form.get("undo") != "1")
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True})
    return redirect(request.referrer or url_for("index"))


# -- clients ------------------------------------------------------------------

@app.route("/clients")
def clients():
    q = (request.args.get("q") or "").strip()
    c = conn()
    try:
        sql = ("SELECT cl.*, (SELECT COUNT(*) FROM documents d WHERE d.client_id=cl.id AND d.kind='facture' "
               "AND d.status!='brouillon') AS n_fact, (SELECT COALESCE(SUM(CASE WHEN kind='avoir' THEN -total_ht "
               "ELSE total_ht END),0) FROM documents d WHERE d.client_id=cl.id AND d.kind IN ('facture','avoir') "
               "AND d.status!='brouillon') AS ca, (SELECT MAX(issue_date) FROM documents d WHERE d.client_id=cl.id) "
               "AS last_doc FROM clients cl")
        show_archived = request.args.get("masques") == "1"
        sql += " WHERE COALESCE(cl.archived, 0) = ?"
        args = [1 if show_archived else 0]
        if q:
            sql += " AND (cl.name LIKE ? OR cl.siren LIKE ? OR cl.city LIKE ? OR cl.email LIKE ?)"
            args += [f"%{q}%"] * 4
        rows = c.execute(sql + " ORDER BY cl.name COLLATE NOCASE", args).fetchall()
        orphans = sum(1 for r in rows if not r["last_doc"])
        n_archived = c.execute("SELECT COUNT(*) n FROM clients WHERE archived=1").fetchone()["n"]
        return render_template("clients.html", clients=rows, q=q, orphans=orphans, show_archived=show_archived,
                               n_archived=n_archived)
    finally:
        c.close()


CLIENT_FIELDS = ["kind", "name", "siren", "vat_number", "address", "post_code", "city", "country", "email",
                 "phone", "pdp_address_scheme", "pdp_address_value", "notes"]


@app.route("/clients/nouveau", methods=["GET", "POST"])
@app.route("/clients/<int:client_id>", methods=["GET", "POST"])
def client_form(client_id=None):
    c = conn()
    try:
        if request.method == "POST":
            f = request.form
            vals = {k: (f.get(k) or "").strip() for k in CLIENT_FIELDS}
            vals["siren"] = "".join(ch for ch in vals["siren"] if ch.isdigit())[:9]
            vals["country"] = (vals["country"] or "FR").upper()[:2]
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
            if nxt in ("facture", "devis"):
                return redirect(url_for("doc_new", kind=nxt, client_id=client_id))
            return redirect(url_for("client_form", client_id=client_id))
        client = c.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone() if client_id else None
        docs = c.execute("SELECT * FROM documents WHERE client_id=? ORDER BY issue_date DESC, id DESC",
                         (client_id,)).fetchall() if client_id else []
        ca = sum((-1 if x["kind"] == "avoir" else 1) * (x["total_ht"] or 0) for x in docs
                 if x["kind"] in ("facture", "avoir") and x["status"] != "brouillon")
        return render_template("client_form.html", client=client, docs=docs, ca=ca)
    finally:
        c.close()


@app.route("/clients/vers-fournisseurs", methods=["POST"])
def clients_to_suppliers():
    ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]
    with db.transaction() as c:
        n = logic.move_clients_to_suppliers(c, ids)
    flash(f"{n} contact(s) déplacé(s) dans les fournisseurs." if n else
          "Aucun contact déplacé (un contact déjà facturé reste un client).", "ok" if n else "error")
    return redirect(request.referrer or url_for("suppliers"))


@app.route("/clients/<int:client_id>/supprimer", methods=["POST"])
def client_delete(client_id):
    with db.transaction() as c:
        n = logic.delete_clients(c, [client_id])
    flash("Client supprimé." if n["deleted"] else
          "Client retiré de vos listes. Ses factures sont conservées (obligation légale de 10 ans) et le restent "
          "à son nom.", "ok")
    return redirect(url_for("clients"))


@app.route("/clients/supprimer", methods=["POST"])
def clients_delete():
    ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]
    with db.transaction() as c:
        n = logic.delete_clients(c, ids)
    parts = []
    if n["deleted"]:
        parts.append(f"{n['deleted']} client(s) supprimé(s)")
    if n["archived"]:
        parts.append(f"{n['archived']} retiré(s) des listes (leurs factures sont conservées)")
    flash(" · ".join(parts) or "Aucun client sélectionné.", "ok")
    return redirect(url_for("clients"))


@app.route("/clients/<int:client_id>/restaurer", methods=["POST"])
def client_restore(client_id):
    with db.transaction() as c:
        c.execute("UPDATE clients SET archived=0 WHERE id=?", (client_id,))
    flash("Client remis dans vos listes.", "ok")
    return redirect(url_for("client_form", client_id=client_id))


@app.route("/api/siren/<siren>")
def api_siren(siren):
    c = conn()
    try:
        siren = "".join(ch for ch in siren if ch.isdigit())[:9]
        try:
            info = logic.lookup_siren(c, siren)
        except Exception as e:  # noqa: BLE001 — réseau ou annuaire
            return jsonify({"error": f"Recherche impossible : {e}"}), 502
        info["vat_number"] = logic.vat_number_from_siren(siren)
        return jsonify(info)
    finally:
        c.close()


# -- documents ----------------------------------------------------------------

def doc_filters(args, kind):
    return {
        "statut": args.get("statut", ""), "type": args.get("type", "facture_le"),
        "periode": period_choice(args.get("periode", "tout")),
        "du": args.get("du", ""), "au": args.get("au", ""), "q": (args.get("q") or "").strip(),
        "client": args.get("client", ""),
    }


def query_documents(c, kind, f):
    start, end = period_range(f["periode"], f["du"] or None, f["au"] or None)
    kinds = ("facture", "avoir") if kind == "facture" else (kind,)
    sql = ("SELECT d.*, cl.name AS client_name, "
           "(SELECT COALESCE(SUM(amount),0) FROM payments WHERE document_id=d.id) AS paid, "
           "(SELECT MAX(date) FROM payments WHERE document_id=d.id) AS paid_on, "
           "(SELECT COALESCE(SUM(total_ttc),0) FROM documents a WHERE a.avoir_of_id=d.id AND a.kind='avoir' "
           " AND a.status!='brouillon') AS credited "
           f"FROM documents d LEFT JOIN clients cl ON cl.id=d.client_id WHERE d.kind IN ({','.join('?' * len(kinds))})")
    args = list(kinds)
    if f["periode"] != "tout":
        if f["type"] == "encaisse_le" and kind == "facture":
            ids = sorted({r["doc_id"] for r in calc.cash_rows(c, start, end)}) or [0]
            sql += f" AND d.id IN ({','.join(str(int(i)) for i in ids)})"
        else:
            sql += " AND COALESCE(d.issue_date, '') >= ? AND COALESCE(d.issue_date, '') < ?"
            args += [start, end]
    st = f["statut"]
    today = date.today().isoformat()
    if st == "brouillons":
        sql += " AND d.status='brouillon'"
    elif st == "payees":
        sql += " AND d.kind='facture' AND d.status='payee'"
    elif st == "non_payees":
        sql += " AND d.kind='facture' AND d.status IN ('emise','partielle')"
    elif st == "echues":
        sql += " AND d.kind='facture' AND d.status IN ('emise','partielle') AND d.due_date < ?"
        args.append(today)
    elif st == "avoirs":
        sql += " AND d.kind='avoir'"
    elif st == "annulees":
        sql += " AND d.status='annulee'"
    elif st in ("en_attente", "accepte", "refuse", "facture"):
        sql += " AND d.status=?"
        args.append(st)
    if f["client"].isdigit():
        sql += " AND d.client_id = ?"
        args.append(int(f["client"]))
    if f["q"]:
        sql += " AND (cl.name LIKE ? OR d.subject LIKE ? OR d.number LIKE ?)"
        args += [f"%{f['q']}%"] * 3
    sql += " ORDER BY (d.status='brouillon') DESC, COALESCE(d.issue_date,'') DESC, d.id DESC"
    rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    tot = {"n": 0, "ht": d(0), "ttc": d(0), "rest": d(0), "enc": d(0), "rest_ht": d(0), "drafts_ht": d(0),
           "drafts_ttc": d(0), "n_drafts": 0, "enc_ht": d(0)}
    for r in rows:
        sign = -1 if r["kind"] == "avoir" else 1
        r["sign"] = sign
        r["late"] = bool(r["kind"] == "facture" and r["status"] in ("emise", "partielle") and r["due_date"]
                         and r["due_date"] < today)
        r["rest"] = float(money(r["total_ttc"]) - money(r["paid"]) - money(r["credited"])) \
            if r["kind"] == "facture" and r["status"] in ("emise", "partielle") else 0.0
        ratio = d(r["total_ht"]) / d(r["total_ttc"]) if r["total_ttc"] else d(1)
        if r["status"] == "brouillon":
            if r["kind"] == "facture":
                tot["n_drafts"] += 1
                tot["drafts_ht"] += d(r["total_ht"])
                tot["drafts_ttc"] += d(r["total_ttc"])
            continue
        tot["n"] += 1
        tot["ht"] += sign * d(r["total_ht"])
        tot["ttc"] += sign * d(r["total_ttc"])
        tot["rest"] += d(r["rest"])
        tot["rest_ht"] += money(d(r["rest"]) * ratio)
    tot["rest_drafts"] = tot["rest"] + tot["drafts_ttc"]
    tot["rest_drafts_ht"] = tot["rest_ht"] + tot["drafts_ht"]
    if f["type"] == "encaisse_le" and f["periode"] != "tout" and kind == "facture":
        for r in calc.cash_rows(c, start, end):
            sg = -1 if r["kind"] == "avoir" else 1
            tot["enc"] += sg * d(r["amount"])
            tot["enc_ht"] += sg * money(d(r["amount"]) * (d(r["total_ht"]) / d(r["total_ttc"]) if r["total_ttc"] else 1))
    return rows, tot


@app.route("/documents")
def documents():
    kind = request.args.get("kind", "facture")
    if kind == "avoir":
        return redirect(url_for("documents", kind="facture", statut="avoirs"))
    f = doc_filters(request.args, kind)
    c = conn()
    try:
        rows, tot = query_documents(c, kind, f)
        kinds = ("facture", "avoir") if kind == "facture" else (kind,)
        companies = c.execute(
            f"SELECT DISTINCT cl.id, cl.name FROM clients cl JOIN documents d ON d.client_id = cl.id "
            f"WHERE d.kind IN ({','.join('?' * len(kinds))}) ORDER BY cl.name COLLATE NOCASE", kinds).fetchall()
        return render_template("documents.html", docs=rows, kind=kind, f=f, tot=tot, companies=companies)
    finally:
        c.close()


@app.route("/documents/export.csv")
def documents_csv():
    kind = request.args.get("kind", "facture")
    f = doc_filters(request.args, kind)
    c = conn()
    try:
        rows, _ = query_documents(c, kind, f)
        out = io.StringIO()
        w = csv.writer(out, delimiter=";")
        w.writerow(["Type", "Numéro", "Date", "Client", "Objet", "Total HT", "TVA", "Total TTC", "Échéance",
                    "Statut", "Payé le", "Reste dû"])
        for r in rows:
            s = r["sign"]
            w.writerow([KIND_LABEL[r["kind"]], r["number"] or "brouillon", fmt_date(r["issue_date"]), r["client_name"],
                        r["subject"] or "", _n(s * r["total_ht"]), _n(s * r["total_tva"]), _n(s * r["total_ttc"]),
                        fmt_date(r["due_date"]), STATUS[r["status"]][0], fmt_date(r["paid_on"]), _n(r["rest"])])
        return _csv_response(out.getvalue(), f"{'devis' if kind == 'devis' else 'ventes'}.csv")
    finally:
        c.close()


def _n(v):
    return f"{money(v):.2f}".replace(".", ",")


def _csv_response(text, name):
    return send_file(io.BytesIO(("﻿" + text).encode("utf-8")), mimetype="text/csv", as_attachment=True,
                     download_name=name)


def default_doc(c, kind, client_id=None):
    s = db.get_settings(c)
    today = date.today()
    terms = s.get("default_payment_terms") or "30-inv"
    return {"id": None, "kind": kind, "client_id": client_id, "issue_date": today.isoformat(), "status": "brouillon",
            "validity_date": (today + timedelta(days=int(s.get("devis_validity_days") or 30))).isoformat()
            if kind == "devis" else None,
            "due_date": logic.due_date_for(today.isoformat(), terms), "payment_terms": terms,
            "penalty_rate": s.get("default_penalty_rate") or None, "discount_rate": s.get("default_discount_rate") or None,
            "subject": "", "notes": "", "service_date": "", "avoir_of_id": None, "source_devis_id": None}


@app.route("/documents/nouveau")
def doc_new():
    kind = request.args.get("kind", "facture")
    if kind not in ("facture", "devis"):
        abort(400)
    c = conn()
    try:
        s = db.get_settings(c)
        doc = default_doc(c, kind, request.args.get("client_id", type=int))
        lines = [{"kind": "title", "designation": ""},
                 {"kind": "item", "designation": "", "description": "", "quantity": 1, "unit": "u", "unit_price": "",
                  "vat_rate": float(s.get("default_vat_rate") or 20), "nature": s.get("default_nature") or "bnc"}]
        return render_template("doc_form.html", doc=doc, lines=lines, clients=all_clients(c, doc.get("client_id")),
                               services=services(c),
                               preview=logic.preview_number(c, kind, date.today().isoformat()))
    finally:
        c.close()


@app.route("/documents/<int:doc_id>/modifier")
def doc_edit(doc_id):
    c = conn()
    try:
        doc, lines = logic.get_doc(c, doc_id)
        if doc["kind"] != "devis" and doc["status"] != "brouillon":
            flash("Une facture finalisée ne se modifie plus. Pour la corriger : Supprimer (un avoir est créé), "
                  "puis Dupliquer.", "error")
            return redirect(url_for("doc_view", doc_id=doc_id))
        return render_template("doc_form.html", doc=doc, lines=with_title(lines), clients=all_clients(c, doc["client_id"]),
                               services=services(c),
                               preview=logic.preview_number(c, doc["kind"], doc["issue_date"] or date.today().isoformat())
                               if doc["status"] == "brouillon" else None)
    finally:
        c.close()


def form_lines(f):
    keys = ["kind_line", "designation", "description", "quantity", "unit", "unit_price", "vat_rate", "nature",
            "optional"]
    cols = {k: f.getlist(k) for k in keys}
    lines = []
    for i in range(len(cols["designation"])):
        ln = {k: (cols[k][i] if i < len(cols[k]) else "") for k in keys}
        ln["kind"] = ln.pop("kind_line") or "item"
        lines.append(ln)
    return lines


@app.route("/documents/apercu", methods=["POST"])
def doc_preview():
    """Aperçu PDF du document tel qu'il est dans l'éditeur, sans l'enregistrer."""
    f = request.form
    c = conn()
    try:
        s = db.get_settings(c)
        kind = f.get("kind") or "facture"
        doc = {r[1]: None for r in c.execute("PRAGMA table_info(documents)").fetchall()}
        doc.update({k: (f.get(k) or None) for k in ("issue_date", "due_date", "validity_date", "subject", "service_date",
                                                   "notes", "payment_terms")})
        doc.update(kind=kind, status="brouillon", id=f.get("id", type=int) or 0,
                   penalty_rate=logic._rate(f.get("penalty_rate")), discount_rate=logic._rate(f.get("discount_rate")),
                   number=logic.preview_number(c, kind, f.get("issue_date") or date.today().isoformat()) if kind == "devis"
                   else None)
        lines = logic.clean_lines(form_lines(f), s.get("default_nature") or "bnc")
        if not f.get("client_id", type=int):
            return "<p style='font-family:sans-serif;padding:30px'>Choisissez d'abord un client pour voir l'aperçu.</p>"
        seller = logic.snapshot_seller(c)
        client = logic.snapshot_client(c, f.get("client_id", type=int))
        path = os.path.join(db.DATA_DIR, "tmp", f"apercu-editeur-{doc['id']}.pdf")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        extra = {}
        if kind == "avoir" and f.get("avoir_of_id", type=int):
            ref = c.execute("SELECT number, issue_date FROM documents WHERE id=?", (f.get("avoir_of_id", type=int),)).fetchone()
            if ref:
                extra = {"avoir_of_number": ref["number"], "avoir_of_date": ref["issue_date"]}
        from pdf import build_pdf
        build_pdf(doc, lines, seller, client, path, extra)
        return send_file(path, mimetype="application/pdf", download_name=f"Aperçu {KIND_LABEL.get(kind, '')}.pdf")
    finally:
        c.close()


@app.route("/documents/enregistrer", methods=["POST"])
def doc_save():
    f = request.form
    doc_id = f.get("id", type=int)
    lines = form_lines(f)
    with db.transaction() as c:
        doc_id = logic.save_document(c, doc_id, f.to_dict(), lines)
        if f.get("action") == "valider":
            logic.validate_document(c, doc_id)
    if f.get("action") == "valider":
        return after_validation(doc_id)
    flash("Brouillon enregistré." if f.get("kind") != "devis" else "Devis enregistré.", "ok")
    return redirect(url_for("doc_view", doc_id=doc_id))


def after_validation(doc_id):
    c = conn()
    try:
        s = db.get_settings(c)
        doc, _ = logic.get_doc(c, doc_id)
        flash(f"{KIND_LABEL[doc['kind']]} {doc['number']} finalisée.", "ok")
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
        t = calc.totals(lines, doc["issue_date"])
        payments = c.execute("SELECT * FROM payments WHERE document_id=? ORDER BY date", (doc_id,)).fetchall()
        paid = logic.paid_amount(c, doc_id)
        credited = logic.credited_amount(c, doc_id) if doc["kind"] == "facture" else 0
        avoirs = c.execute("SELECT * FROM documents WHERE avoir_of_id=?", (doc_id,)).fetchall()
        origin = c.execute("SELECT id, number, kind FROM documents WHERE id IN (?, ?)",
                           (doc["avoir_of_id"] or 0, doc["source_devis_id"] or 0)).fetchall()
        invoices_from = c.execute("SELECT id, number, status FROM documents WHERE source_devis_id=?",
                                  (doc_id,)).fetchall()
        remaining = money(doc["total_ttc"]) - paid - money(credited)
        avoir_rest = money(doc["total_ttc"]) - paid
        prev, nxt = logic.neighbours(c, doc)
        crow = c.execute("SELECT email FROM clients WHERE id=?", (doc["client_id"] or 0,)).fetchone()
        client_email = (client.get("email") if isinstance(client, dict) else None) or (crow["email"] if crow else None)
        mail_link = None
        if client_email:
            s = db.get_settings(c)
            late = doc["kind"] == "facture" and doc["status"] in ("emise", "partielle") and doc["due_date"] \
                and doc["due_date"] < date.today().isoformat()
            what = {"facture": "facture", "avoir": "avoir", "devis": "devis"}[doc["kind"]]
            if late:
                subj = f"Relance — facture {doc['number']}"
                body = (f"Bonjour,\n\nSauf erreur de ma part, la facture {doc['number']} du {fmt_date(doc['issue_date'])}"
                        f" d'un montant de {fmt_money(float(remaining))} TTC, arrivée à échéance le "
                        f"{fmt_date(doc['due_date'])}, reste à régler.\n\nPourriez-vous procéder au règlement "
                        f"par virement ?\n{('IBAN : ' + s['iban'] + chr(10)) if s.get('iban') else ''}"
                        f"\nSi le paiement a été effectué entre-temps, merci de ne pas tenir compte de ce message."
                        f"\n\nBien cordialement,\n{s.get('company_name') or ''}")
            else:
                subj = f"{what.capitalize()} {doc['number'] or ''}".strip()
                body = (f"Bonjour,\n\nVeuillez trouver ci-joint {'le' if what == 'devis' else 'la' if what == 'facture' else "l'"}"
                        f"{'' if what == 'avoir' else ' '}{what} {doc['number'] or ''}"
                        f" d'un montant de {fmt_money(float(doc['total_ttc']))} TTC.\n\nBien cordialement,\n"
                        f"{s.get('company_name') or ''}")
            from urllib.parse import quote
            mail_link = f"mailto:{client_email}?subject={quote(subj)}&body={quote(body)}"
        running = d(0)
        shown = []
        for ln in lines:
            ln = dict(ln)
            if (ln.get("kind") or "item") == "item" and ln.get("optional"):
                ln["opt_ht"] = float(calc.line_net(ln))
            elif (ln.get("kind") or "item") == "item":
                running += calc.line_net(ln)
            elif ln["kind"] == "subtotal":
                ln["subtotal"] = float(running)
                running = d(0)
            shown.append(ln)
        return render_template("doc_view.html", doc=doc, lines=shown, client=client, t=t, payments=payments,
                               paid=paid, credited=credited, remaining=remaining, avoir_rest=avoir_rest, avoirs=avoirs,
                               client_email=client_email, mail_link=mail_link, origin=origin,
                               invoices_from=invoices_from, prev=prev, nxt=nxt)
    finally:
        c.close()


@app.route("/documents/<int:doc_id>/pdf")
def doc_pdf(doc_id):
    c = conn()
    try:
        doc, lines = logic.get_doc(c, doc_id)
        path = doc["pdf_path"]
        if doc["status"] == "brouillon" or (doc["kind"] == "devis" and not doc["imported"]) \
                or not path or not os.path.exists(path):
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
        s = db.get_settings(c)
        terms = s.get("default_payment_terms") or "30-inv"
        c.execute("UPDATE documents SET payment_terms=COALESCE(payment_terms, ?), due_date=? WHERE id=?",
                  (terms, logic.due_date_for(date.today().isoformat(), terms), new_id))
        c.execute("UPDATE documents SET status='accepte' WHERE id=? AND status='en_attente'", (doc_id,))
    flash("Brouillon de facture créé à partir du devis : vérifiez puis finalisez-le.", "ok")
    return redirect(url_for("doc_edit", doc_id=new_id))


@app.route("/documents/<int:doc_id>/avoir", methods=["POST"])
def doc_to_avoir(doc_id):
    with db.transaction() as c:
        doc, _ = logic.get_doc(c, doc_id)
        if doc["kind"] != "facture" or doc["status"] == "brouillon":
            raise logic.BusinessError("Un avoir se fait sur une facture finalisée.")
        new_id = logic.duplicate_as(c, doc_id, "avoir", avoir=True)
    flash("Avoir partiel en brouillon : ajustez les lignes puis finalisez-le.", "ok")
    return redirect(url_for("doc_edit", doc_id=new_id))


@app.route("/documents/<int:doc_id>/dupliquer", methods=["POST"])
def doc_duplicate(doc_id):
    with db.transaction() as c:
        doc, _ = logic.get_doc(c, doc_id)
        target = request.form.get("en")
        kind = target if target in ("facture", "devis") else ("devis" if doc["kind"] == "devis" else "facture")
        new_id = logic.duplicate_as(c, doc_id, kind)
        s = db.get_settings(c)
        terms = doc.get("payment_terms") or s.get("default_payment_terms") or "30-inv"
        c.execute("UPDATE documents SET due_date=?, avoir_of_id=NULL WHERE id=?",
                  (logic.due_date_for(date.today().isoformat(), terms), new_id))
        if kind == "devis":
            n = logic.next_number(c, "devis", date.today().isoformat())
            c.execute("UPDATE documents SET number=?, status='en_attente', source_devis_id=NULL WHERE id=?", (n, new_id))
    flash("Copie créée en facture brouillon." if kind == "facture" else "Copie créée en devis.", "ok")
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
    """Brouillon ou devis : suppression réelle. Facture finalisée : annulation automatique par avoir."""
    avoir = None
    with db.transaction() as c:
        doc, _ = logic.get_doc(c, doc_id)
        if doc["kind"] == "facture" and doc["status"] != "brouillon":
            avoir_id = logic.cancel_invoice(c, doc_id)
            avoir, _ = logic.get_doc(c, avoir_id)
        else:
            if doc["kind"] == "avoir" and doc["status"] != "brouillon":
                raise logic.BusinessError("Un avoir finalisé ne peut pas être supprimé.")
            # les factures issues de ce devis (ou un avoir brouillon) gardent leur existence propre
            c.execute("UPDATE documents SET source_devis_id=NULL WHERE source_devis_id=?", (doc_id,))
            c.execute("UPDATE documents SET avoir_of_id=NULL WHERE avoir_of_id=? AND status='brouillon'", (doc_id,))
            c.execute("DELETE FROM lines WHERE document_id=?", (doc_id,))
            c.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    if avoir:
        flash(f"Facture {doc['number']} supprimée : l'avoir {avoir['number']} a été créé automatiquement.", "ok")
        c = conn()
        try:
            if doc.get("pdp_id") and logic.pdp_configured(c):
                try:
                    logic.send_to_pdp(c, avoir["id"])
                    flash("Avoir transmis au client via la plateforme agréée.", "ok")
                except logic.BusinessError as e:
                    flash(f"Avoir non transmis à la plateforme : {e}", "error")
                c.commit()
        finally:
            c.close()
        return redirect(url_for("doc_view", doc_id=avoir["id"]))
    flash("Supprimé.", "ok")
    return redirect(url_for("documents", kind="devis" if doc["kind"] == "devis" else "facture"))


@app.route("/documents/<int:doc_id>/paiement", methods=["POST"])
def doc_payment(doc_id):
    f = request.form
    with db.transaction() as c:
        logic.add_payment(c, doc_id, f.get("date") or date.today().isoformat(), f.get("amount"), f.get("method"),
                          f.get("reference"))
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


# -- achats : dépenses, fournisseurs, récurrents --------------------------------

def purchase_filters(args):
    return {"periode": period_choice(args.get("periode", "annee")), "du": args.get("du", ""), "au": args.get("au", ""),
            "type": args.get("type", "facture_le"), "statut": args.get("statut", ""),
            "q": (args.get("q") or "").strip(), "fournisseur": args.get("fournisseur", "")}


def query_purchases(c, f):
    start, end = period_range(f["periode"], f["du"] or None, f["au"] or None)
    col = "paid_date" if f["type"] == "paye_le" else "invoice_date"
    sql = "SELECT p.*, s.name AS supplier_name FROM purchases p LEFT JOIN suppliers s ON s.id = p.supplier_id WHERE 1=1"
    args = []
    if f["periode"] != "tout":
        sql += f" AND COALESCE(p.{col}, '') >= ? AND COALESCE(p.{col}, '') < ?"
        args += [start, end]
    if f["statut"] == "non_payees":
        sql += " AND p.paid_date IS NULL"
    elif f["statut"] == "payees":
        sql += " AND p.paid_date IS NOT NULL"
    elif f["statut"] == "sans_deduction":
        sql += " AND p.deduction_date IS NULL"
    if f["fournisseur"].isdigit():
        sql += " AND p.supplier_id = ?"
        args.append(int(f["fournisseur"]))
    if f["q"]:
        sql += " AND (p.supplier LIKE ? OR s.name LIKE ? OR p.description LIKE ? OR p.invoice_number LIKE ? " \
               "OR p.payment_ref LIKE ?)"
        args += [f"%{f['q']}%"] * 5
    sql += " ORDER BY (p.deduction_date IS NULL AND p.source='pdp') DESC, COALESCE(p.invoice_date,'') DESC, p.id DESC"
    rows = c.execute(sql, args).fetchall()
    live = [r for r in rows if not r["draft"]]
    tot = {"n": len(live), "ht": sum(r["total_ht"] or 0 for r in live), "ttc": sum(r["total_ttc"] or 0 for r in live),
           "tva": sum(r["total_tva"] or 0 for r in live)}
    return rows, tot


@app.route("/achats")
def purchases():
    f = purchase_filters(request.args)
    c = conn()
    try:
        logic.generate_recurring(c)
        c.commit()
        rows, tot = query_purchases(c, f)
        companies = c.execute("SELECT DISTINCT s.id, s.name FROM suppliers s JOIN purchases p ON p.supplier_id = s.id "
                              "ORDER BY s.name COLLATE NOCASE").fetchall()
        return render_template("purchases.html", purchases=rows, f=f, tot=tot, companies=companies)
    finally:
        c.close()


@app.route("/achats/export.csv")
def purchases_csv():
    f = purchase_filters(request.args)
    c = conn()
    try:
        rows, _ = query_purchases(c, f)
        out = io.StringIO()
        w = csv.writer(out, delimiter=";")
        w.writerow(["Date", "Réf.", "Fournisseur", "Libellé", "HT", "TVA", "TTC", "Réglé le", "Mode", "TVA déduite en"])
        for r in rows:
            w.writerow([fmt_date(r["invoice_date"]), r["invoice_number"] or "", r["supplier_name"] or r["supplier"],
                        r["description"] or "", _n(r["total_ht"]), _n(r["total_tva"]), _n(r["total_ttc"]),
                        fmt_date(r["paid_date"]), r["method"] or "", (r["deduction_date"] or "")[:7]])
        return _csv_response(out.getvalue(), "achats.csv")
    finally:
        c.close()


@app.route("/achats/nouveau", methods=["GET", "POST"])
@app.route("/achats/<int:pid>", methods=["GET", "POST"])
def purchase_form(pid=None):
    c = conn()
    try:
        if request.method == "POST":
            f = request.form
            ttc, tva = money(f.get("total_ttc") or 0), money(f.get("total_tva") or 0)
            rate = f.get("vat_rate")
            if not ttc and f.get("total_ht"):  # saisie HT + TVA (ancien formulaire, import)
                ttc = money(f.get("total_ht")) + tva
                rate = None
            if rate not in (None, "") and ttc:
                r = d(rate)
                tva = money(ttc - ttc / (1 + r / 100))
            ht = ttc - tva
            name = (f.get("supplier_new") or "").strip() or (f.get("supplier_pick") or "").strip() \
                or (f.get("supplier") or "").strip()
            if not name:
                raise logic.BusinessError("Choisissez un fournisseur ou saisissez-en un nouveau.")
            sid = logic.supplier_id_for(c, name)
            paid = f.get("paid_date") or None
            method = f.get("method") or None
            if method and not paid:
                paid = f.get("invoice_date") or date.today().isoformat()
            vals = {"supplier": name, "supplier_id": sid, "invoice_number": f.get("invoice_number") or None,
                    "invoice_date": f.get("invoice_date") or None, "description": f.get("description"),
                    "total_ht": float(ht), "total_tva": float(tva), "total_ttc": float(ttc),
                    "vat_rate": float(rate) if rate not in (None, "") else None,
                    "category": f.get("category") or "biens_services", "nature": f.get("nature") or "services",
                    "method": method, "paid_date": paid, "payment_ref": (f.get("payment_ref") or "").strip() or None,
                    "due_date": f.get("due_date") or None, "notes": f.get("notes") or None,
                    "draft": 1 if f.get("draft") else 0,
                    "deduction_date": f.get("deduction_date") or paid}
            if vals["nature"] == "biens" and not f.get("deduction_date") and vals["invoice_date"]:
                vals["deduction_date"] = vals["invoice_date"]
            if pid:
                c.execute(f"UPDATE purchases SET {', '.join(k + '=?' for k in vals)} WHERE id=?", (*vals.values(), pid))
            else:
                c.execute(f"INSERT INTO purchases ({', '.join(vals)}) VALUES ({', '.join('?' for _ in vals)})",
                          tuple(vals.values()))
            c.commit()
            flash(f"Dépense {name} · {fmt_money(float(ttc))} TTC enregistrée.", "ok")
            if f.get("next") == "new":
                return redirect(url_for("purchase_form"))
            return redirect(url_for("purchases"))
        p = c.execute("SELECT * FROM purchases WHERE id=?", (pid,)).fetchone() if pid else None
        sups = c.execute("SELECT name FROM suppliers ORDER BY name COLLATE NOCASE").fetchall()
        return render_template("purchase_form.html", p=p, suppliers=sups,
                               preset=request.args.get("fournisseur", ""))
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
        c.execute("DELETE FROM purchases WHERE id=? AND source != 'pdp'", (pid,))
    flash("Dépense supprimée.", "ok")
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


@app.route("/fournisseurs")
def suppliers():
    q = (request.args.get("q") or "").strip()
    c = conn()
    try:
        sql = ("SELECT s.*, (SELECT COUNT(*) FROM purchases p WHERE p.supplier_id=s.id) n, "
               "(SELECT COALESCE(SUM(total_ht),0) FROM purchases p WHERE p.supplier_id=s.id) ht, "
               "(SELECT MAX(invoice_date) FROM purchases p WHERE p.supplier_id=s.id) last FROM suppliers s")
        args = []
        if q:
            sql += " WHERE s.name LIKE ? OR s.siren LIKE ? OR s.city LIKE ?"
            args = [f"%{q}%"] * 3
        rows = c.execute(sql + " ORDER BY s.name COLLATE NOCASE", args).fetchall()
        orphans = c.execute("SELECT c.* FROM clients c WHERE NOT EXISTS (SELECT 1 FROM documents d WHERE "
                            "d.client_id=c.id) ORDER BY c.name COLLATE NOCASE").fetchall()
        return render_template("suppliers.html", suppliers=rows, q=q, orphans=orphans)
    finally:
        c.close()


SUPPLIER_FIELDS = ["name", "siren", "vat_number", "address", "post_code", "city", "country", "email", "phone",
                   "website", "notes"]


@app.route("/fournisseurs/nouveau", methods=["GET", "POST"])
@app.route("/fournisseurs/<int:sid>", methods=["GET", "POST"])
def supplier_form(sid=None):
    c = conn()
    try:
        if request.method == "POST":
            vals = {k: (request.form.get(k) or "").strip() for k in SUPPLIER_FIELDS}
            vals["country"] = (vals["country"] or "FR").upper()[:2]
            if not vals["name"]:
                raise logic.BusinessError("Le nom du fournisseur est obligatoire.")
            if sid:
                c.execute(f"UPDATE suppliers SET {', '.join(k + '=?' for k in vals)} WHERE id=?", (*vals.values(), sid))
                c.execute("UPDATE purchases SET supplier=? WHERE supplier_id=?", (vals["name"], sid))
            else:
                sid = c.execute(f"INSERT INTO suppliers ({', '.join(vals)}) VALUES ({', '.join('?' for _ in vals)})",
                                tuple(vals.values())).lastrowid
            c.commit()
            flash("Fournisseur enregistré.", "ok")
            return redirect(url_for("supplier_form", sid=sid))
        s = c.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone() if sid else None
        rows = c.execute("SELECT * FROM purchases WHERE supplier_id=? ORDER BY invoice_date DESC", (sid,)).fetchall() \
            if sid else []
        return render_template("supplier_form.html", s=s, purchases=rows)
    finally:
        c.close()


@app.route("/fournisseurs/<int:sid>/supprimer", methods=["POST"])
def supplier_delete(sid):
    with db.transaction() as c:
        if c.execute("SELECT 1 FROM purchases WHERE supplier_id=?", (sid,)).fetchone():
            raise logic.BusinessError("Ce fournisseur a des dépenses : il ne peut pas être supprimé.")
        c.execute("DELETE FROM recurring_purchases WHERE supplier_id=?", (sid,))
        c.execute("DELETE FROM suppliers WHERE id=?", (sid,))
    flash("Fournisseur supprimé.", "ok")
    return redirect(url_for("suppliers"))


@app.route("/achats-recurrents")
def recurring():
    c = conn()
    try:
        rows = c.execute("SELECT r.*, s.name AS supplier_name FROM recurring_purchases r LEFT JOIN suppliers s ON "
                         "s.id=r.supplier_id ORDER BY r.active DESC, r.next_date").fetchall()
        tot = {"ht": sum(r["total_ht"] / (r["every_months"] or 1) for r in rows if r["active"]),
               "ttc": sum(r["total_ttc"] / (r["every_months"] or 1) for r in rows if r["active"])}
        return render_template("recurring.html", rows=rows, tot=tot)
    finally:
        c.close()


@app.route("/achats-recurrents/detecter", methods=["POST"])
def recurring_detect():
    c = conn()
    try:
        n = logic.detect_recurring(c)
        c.commit()
        flash(f"{n} abonnement(s) trouvé(s) dans vos dépenses." if n else
              "Aucun nouvel abonnement trouvé dans vos dépenses.")
        return redirect(url_for("recurring"))
    finally:
        c.close()


@app.route("/achats-recurrents/nouveau", methods=["GET", "POST"])
@app.route("/achats-recurrents/<int:rid>", methods=["GET", "POST"])
def recurring_form(rid=None):
    c = conn()
    try:
        if request.method == "POST":
            f = request.form
            name = (f.get("supplier_new") or "").strip() or (f.get("supplier_pick") or "").strip() \
                or (f.get("supplier") or "").strip()
            if not name or not (f.get("title") or "").strip():
                raise logic.BusinessError("Fournisseur et libellé obligatoires.")
            ttc, tva = money(f.get("total_ttc") or 0), money(f.get("total_tva") or 0)
            if not ttc and f.get("total_ht"):
                ttc = money(f.get("total_ht")) + tva
            elif f.get("vat_rate") not in (None, "") and ttc:
                tva = money(ttc - ttc / (1 + d(f["vat_rate"]) / 100))
            ht = ttc - tva
            vals = {"supplier_id": logic.supplier_id_for(c, name), "title": f["title"].strip(), "total_ht": float(ht),
                    "total_tva": float(tva), "total_ttc": float(ttc), "category": f.get("category") or "biens_services",
                    "nature": f.get("nature") or "services", "method": f.get("method") or None,
                    "every_months": int(f.get("every_months") or 1),
                    "next_date": f.get("next_date") or date.today().isoformat(),
                    "max_occurrences": int(f["max_occurrences"]) if (f.get("max_occurrences") or "").isdigit() else None,
                    "auto_paid": 1 if f.get("auto_paid") else 0, "active": 1 if f.get("active") else 0}
            if rid:
                c.execute(f"UPDATE recurring_purchases SET {', '.join(k + '=?' for k in vals)} WHERE id=?",
                          (*vals.values(), rid))
            else:
                c.execute(f"INSERT INTO recurring_purchases ({', '.join(vals)}) VALUES ({', '.join('?' for _ in vals)})",
                          tuple(vals.values()))
            n = logic.generate_recurring(c)
            c.commit()
            flash("Achat récurrent enregistré." + (f" {n} dépense(s) créée(s)." if n else ""), "ok")
            return redirect(url_for("recurring"))
        r = c.execute("SELECT r.*, s.name AS supplier_name FROM recurring_purchases r LEFT JOIN suppliers s ON "
                      "s.id=r.supplier_id WHERE r.id=?", (rid,)).fetchone() if rid else None
        sups = c.execute("SELECT name FROM suppliers ORDER BY name COLLATE NOCASE").fetchall()
        return render_template("recurring_form.html", r=r, suppliers=sups)
    finally:
        c.close()


@app.route("/achats-recurrents/<int:rid>/supprimer", methods=["POST"])
def recurring_delete(rid):
    with db.transaction() as c:
        c.execute("DELETE FROM recurring_purchases WHERE id=?", (rid,))
    flash("Achat récurrent supprimé (les dépenses déjà créées sont conservées).", "ok")
    return redirect(url_for("recurring"))


# -- déclarations ---------------------------------------------------------------

@app.route("/tva")
def tva():
    period = request.args.get("mois") or calc.previous_period()
    c = conn()
    try:
        res = calc.compute_period(c, period)
        decl = c.execute("SELECT * FROM declarations WHERE period=?", (period,)).fetchone()
        undated = c.execute("SELECT * FROM purchases WHERE deduction_date IS NULL ORDER BY invoice_date").fetchall()
        unpaid_reported = c.execute(
            "SELECT COUNT(*) n FROM payments WHERE pdp_reported=0 AND pdp_error IS NOT NULL").fetchone()["n"]
        history = c.execute("SELECT * FROM declarations ORDER BY period DESC LIMIT 12").fetchall()
        return render_template("tva.html", r=res, period=period, decl=decl, undated=undated,
                               prev=calc.previous_of(period), next=calc.next_period(period), history=history,
                               unpaid_reported=unpaid_reported)
    finally:
        c.close()


@app.route("/tva/declarer", methods=["POST"])
def tva_declare():
    period = request.form["period"]
    with db.transaction() as c:
        logic.mark_task(c, f"tva:{period}", not request.form.get("undo"))
    flash(f"TVA de {period_label(period)} marquée comme déclarée." if not request.form.get("undo")
          else "Déclaration annulée.", "ok")
    return redirect(url_for("tva", mois=period))


@app.route("/urssaf")
def urssaf():
    c = conn()
    try:
        s = db.get_settings(c)
        monthly = s.get("urssaf_period") == "mois"
        if monthly:
            period = request.args.get("p") or calc.previous_period()
            start, end = calc.month_bounds(period)
            prev, nxt = calc.previous_of(period), calc.next_period(period)
            label = period_label(period)
            e = date.fromisoformat(end)
            due = (date(e.year + (e.month == 12), e.month % 12 + 1, 1) - timedelta(days=1)).isoformat()
        else:
            period = request.args.get("p") or calc.previous_quarter()
            start, end = calc.quarter_bounds(period)
            prev, nxt = calc.shift_quarter(period, -1), calc.shift_quarter(period, 1)
            label = f"{period[-2:]} {period[:4]}"
            due = (date.fromisoformat(end) + timedelta(days=30)).isoformat()
        res = calc.compute_urssaf(c, start, end, s)
        done = c.execute("SELECT done_at FROM tasks_done WHERE task=?", (f"urssaf:{period}",)).fetchone()
        ledger = logic.urssaf_ledger(c)
        cur = next((x for x in ledger if x["period"] == period), None)
        pays = c.execute("SELECT * FROM urssaf_payments WHERE period=? ORDER BY date", (period,)).fetchall()
        return render_template("urssaf.html", r=res, period=period, label=label, prev=prev, nxt=nxt, done=done,
                               start=start, end=(date.fromisoformat(end) - timedelta(days=1)).isoformat(), due=due,
                               ledger=ledger, cur=cur, pays=pays, arrears=logic.urssaf_arrears(c))
    finally:
        c.close()


@app.route("/urssaf/<period>/cotisations", methods=["POST"])
def urssaf_due(period):
    with db.transaction() as c:
        logic.urssaf_set_due(c, period, request.form.get("amount") or 0)
    flash("Montant des cotisations enregistré.", "ok")
    return redirect(url_for("urssaf", p=period))


@app.route("/urssaf/<period>/paiement", methods=["POST"])
def urssaf_pay(period):
    f = request.form
    with db.transaction() as c:
        if not c.execute("SELECT 1 FROM urssaf_dues WHERE period=?", (period,)).fetchone():
            r = logic.urssaf_estimate(c, period)
            logic.urssaf_set_due(c, period, r["total"])
        logic.urssaf_add_payment(c, period, f.get("date") or date.today().isoformat(), f.get("amount"), f.get("reference"))
    flash("Règlement URSSAF enregistré.", "ok")
    return redirect(url_for("urssaf", p=period))


@app.route("/urssaf/paiement/<int:pid>/supprimer", methods=["POST"])
def urssaf_pay_delete(pid):
    with db.transaction() as c:
        row = c.execute("SELECT period FROM urssaf_payments WHERE id=?", (pid,)).fetchone()
        c.execute("DELETE FROM urssaf_payments WHERE id=?", (pid,))
    return redirect(url_for("urssaf", p=row["period"]) if row else url_for("urssaf"))


# -- export -------------------------------------------------------------------

@app.route("/export")
def export_page():
    c = conn()
    try:
        years = [r["y"] for r in c.execute("SELECT DISTINCT substr(issue_date,1,4) y FROM documents WHERE "
                                           "issue_date IS NOT NULL ORDER BY y DESC").fetchall() if r["y"]]
        return render_template("export.html", years=years, snapshots=backup.list_snapshots()[:7])
    finally:
        c.close()


@app.route("/export/sauvegarde-complete.zip")
def export_full():
    path = backup.full_zip()
    resp = send_file(path, mimetype="application/zip", as_attachment=True,
                     download_name=f"facturation-sauvegarde-{date.today().isoformat()}.zip")
    resp.call_on_close(lambda: os.path.exists(path) and os.remove(path))
    return resp


@app.route("/export/restaurer", methods=["POST"])
def export_restore():
    f = request.files.get("fichier")
    if not f or not f.filename:
        raise logic.BusinessError("Choisissez le fichier ZIP de sauvegarde.")
    if request.form.get("confirm") != "RESTAURER":
        raise logic.BusinessError("Tapez RESTAURER pour confirmer : les données actuelles seront remplacées.")
    try:
        safety = backup.restore_zip(f.stream)
    except ValueError as e:
        raise logic.BusinessError(str(e))
    flash("Sauvegarde restaurée." + (f" L'état précédent est gardé dans {os.path.basename(safety)}." if safety else ""), "ok")
    return redirect(url_for("index"))


@app.route("/export/instantane", methods=["POST"])
def export_snapshot():
    p = backup.snapshot()
    flash(f"Copie de la base enregistrée ({os.path.basename(p)})." if p else "Aucune base à copier.", "ok")
    return redirect(url_for("export_page"))


@app.route("/export/archives-<year>.zip")
def export_zip(year):
    c = conn()
    try:
        rows = c.execute("SELECT kind, number, pdf_path FROM documents WHERE substr(issue_date,1,4)=? AND "
                         "status!='brouillon' AND pdf_path IS NOT NULL", (year,)).fetchall()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for r in rows:
                if r["pdf_path"] and os.path.exists(r["pdf_path"]):
                    z.write(r["pdf_path"], f"{KIND_LABEL[r['kind']]}s/{os.path.basename(r['pdf_path'])}")
            for p in c.execute("SELECT file_path FROM purchases WHERE substr(invoice_date,1,4)=? AND file_path IS NOT "
                               "NULL", (year,)).fetchall():
                if os.path.exists(p["file_path"]):
                    z.write(p["file_path"], f"Achats/{os.path.basename(p['file_path'])}")
        buf.seek(0)
        return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=f"facturation-{year}.zip")
    finally:
        c.close()


# -- réglages -----------------------------------------------------------------

SETTINGS_FIELDS = ["company_name", "company_trade_name", "address", "post_code", "city", "siren", "siret",
                   "vat_number", "ape", "email", "phone", "website", "first_name", "iban", "bic", "rcs_rm", "insurance", "invoice_pattern",
                   "avoir_pattern", "devis_pattern", "devis_validity_days", "default_vat_rate",
                   "late_penalty", "recovery_fee", "discount_terms", "footer_note", "ca3_due_day", "pdp_client_id",
                   "pdp_endpoint", "ereporting_start", "default_nature", "default_payment_terms",
                   "default_penalty_rate", "default_discount_rate", "urssaf_period", "urssaf_rate_vente",
                   "urssaf_rate_bic", "urssaf_rate_bnc", "ir_rate_vente", "ir_rate_bic", "ir_rate_bnc", "cfp_rate"]
CHECKBOXES = ["pdp_auto_send", "invoice_reset_yearly", "versement_liberatoire"]


@app.route("/reglages", methods=["GET", "POST"])
def settings_page():
    c = conn()
    try:
        if request.method == "POST":
            f = request.form
            section = f.get("section", "")
            vals = {k: (f.get(k) or "").strip() for k in SETTINGS_FIELDS if k in f}
            for k in CHECKBOXES:
                if f.get(f"has_{k}"):
                    vals[k] = "1" if f.get(k) else "0"
            if "siret" in vals:
                vals["siret"] = vals["siret"].replace(" ", "")
                vals["siren"] = (vals.get("siren") or vals["siret"][:9]).replace(" ", "")
                if vals["siren"] and not vals.get("vat_number"):
                    vals["vat_number"] = logic.vat_number_from_siren(vals["siren"])
            if f.get("pdp_client_secret"):
                vals["pdp_client_secret"] = f["pdp_client_secret"].strip()
            counters = {k: f.get(k) for k in f if k.startswith(("invoice_next", "avoir_next", "devis_next")) and f.get(k)}
            logo = request.files.get("logo")
            if logo and logo.filename:
                ext = os.path.splitext(logo.filename)[1].lower()
                if ext not in (".png", ".jpg", ".jpeg"):
                    raise logic.BusinessError("Logo : format PNG ou JPG uniquement.")
                for old in ("logo.png", "logo.jpg", "logo.jpeg"):
                    try:
                        os.remove(os.path.join(db.DATA_DIR, old))
                    except OSError:
                        pass
                path = os.path.join(db.DATA_DIR, f"logo{ext}")
                logo.save(path)
                vals["logo_path"] = path
            if f.get("remove_logo"):
                vals["logo_path"] = ""
            db.set_settings({**vals, **counters}, c)
            c.commit()
            flash("Réglages enregistrés.", "ok")
            return redirect(url_for("settings_page", _anchor=section) if section else url_for("settings_page"))
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


@app.route("/reglages/logo")
def settings_logo():
    path = db.get_settings().get("logo_path")
    if not path or not os.path.exists(path):
        abort(404)
    return send_file(path)


@app.route("/reglages/tester", methods=["POST"])
def settings_test():
    c = conn()
    try:
        try:
            out = logic.test_connection(c)
            c.commit()
            comp = out["company"]
            name = comp.get("formal_name") or comp.get("name") or "?"
            env = " (bac à sable)" if comp.get("env") == "sandbox" else ""
            msg = f"Connexion réussie : {name}{env}."
            if out.get("address"):
                msg += f" Adresse de facturation électronique : {out['address'][1]}."
            flash(msg, "ok")
        except Exception as e:  # noqa: BLE001
            flash(f"Échec de la connexion : {e}", "error")
    finally:
        c.close()
    return redirect(url_for("settings_page", _anchor="pdp"))


@app.route("/synchroniser", methods=["POST"])
def sync_now():
    for level, m in run_sync():
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
        return render_template("import.html", report=report, job=monae.get_job())
    return render_template("import.html", report=None, job=monae.get_job())


@app.route("/import/monae", methods=["POST"])
def import_monae():
    f = request.form
    try:
        monae.start(f.get("login", ""), f.get("key", ""), f.get("firm_id") or None, with_pdfs=bool(f.get("pdfs")))
    except monae.MonAEError as e:
        flash(str(e), "error")
    return redirect(url_for("import_page"))


@app.route("/api/import/monae")
def import_monae_status():
    return jsonify(monae.get_job())


debug_routes.register(app, conn)


@app.route("/sante")
def health():
    return jsonify({"ok": True})


# -- utilitaires --------------------------------------------------------------

def with_title(lines):
    """Une ligne titre vide en tête de l'éditeur si le document n'en a pas (ignorée si elle reste vide)."""
    if lines and (lines[0].get("kind") or "item") == "title":
        return lines
    return [{"kind": "title", "designation": ""}] + list(lines)


def all_clients(c, keep_id=None):
    """Clients proposés dans la liste déroulante (sans ceux retirés, sauf celui déjà choisi)."""
    return [dict(r) for r in c.execute(
        "SELECT id, name, kind, email, city, siren FROM clients WHERE COALESCE(archived, 0) = 0 OR id = ? "
        "ORDER BY name COLLATE NOCASE", (keep_id or 0,)).fetchall()]


def services(c):
    return [dict(r) for r in c.execute("SELECT * FROM services ORDER BY position, id").fetchall()]


@app.route("/prestations", methods=["GET", "POST"])
def services_page():
    c = conn()
    try:
        if request.method == "POST":
            f = request.form
            sid = f.get("id", type=int)
            if f.get("delete") and sid:
                c.execute("DELETE FROM services WHERE id=?", (sid,))
                flash("Prestation supprimée.", "ok")
            else:
                name = (f.get("designation") or "").strip()
                if not name:
                    raise logic.BusinessError("Indiquez le nom de la prestation.")
                vals = (name, (f.get("description") or "").strip(), f.get("unit") or "u",
                        float(d(f.get("unit_price") or 0)), float(d(f.get("vat_rate") or 20)),
                        f.get("nature") or db.get_settings(c).get("default_nature") or "bnc")
                if sid:
                    c.execute("UPDATE services SET designation=?, description=?, unit=?, unit_price=?, vat_rate=?, nature=? "
                              "WHERE id=?", (*vals, sid))
                else:
                    pos = c.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM services").fetchone()[0]
                    c.execute("INSERT INTO services(designation, description, unit, unit_price, vat_rate, nature, position) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?)", (*vals, pos))
                flash(f"Prestation « {name} » enregistrée.", "ok")
            c.commit()
            return redirect(url_for("services_page"))
        return render_template("services.html", services=services(c), edit=request.args.get("id", type=int))
    finally:
        c.close()


@app.route("/prestations/ordre", methods=["POST"])
def services_order():
    ids = (request.get_json(silent=True) or {}).get("ids") or []
    with db.transaction() as c:
        for i, sid in enumerate(ids):
            c.execute("UPDATE services SET position=? WHERE id=?", (i, int(sid)))
    return jsonify({"ok": True})


def catalog(c):
    """Lignes déjà facturées, pour l'autocomplétion."""
    return c.execute("SELECT designation, unit, unit_price, vat_rate, nature, MAX(id) FROM lines "
                     "WHERE COALESCE(kind,'item')='item' GROUP BY designation ORDER BY MAX(id) DESC LIMIT 300").fetchall()


def run_sync():
    msgs = []
    c = conn()
    try:
        logic.generate_recurring(c)
        c.commit()
        if not logic.pdp_configured(c):
            return [("error", "Plateforme agréée non configurée (Réglages).")]
        try:
            logic.sync_statuses(c)
            n = logic.fetch_inbox(c)
            c.commit()
            msgs.append(("ok", f"Synchronisé. {n} nouvelle(s) facture(s) fournisseur." if n else "Synchronisé."))
        except Exception as e:  # noqa: BLE001
            c.commit()
            msgs.append(("error", f"Synchronisation impossible : {e}"))
    finally:
        c.close()
    return msgs


def nightly_snapshot():
    """Copie de la base chaque nuit vers 3 h 30, avant la sauvegarde automatique de Home Assistant (4 h)."""
    from datetime import datetime
    last = None
    while True:
        time.sleep(300)
        now = datetime.now()
        if now.hour == 3 and now.minute >= 30 and last != now.date():
            try:
                backup.snapshot()
                last = now.date()
            except Exception:  # noqa: BLE001
                pass


def background_sync():
    while True:
        time.sleep(1800)
        try:
            run_sync()
        except Exception:  # noqa: BLE001
            pass


def main():
    db.init_db()
    c = conn()
    try:  # une seule fois : retrouver les abonnements déjà présents dans les dépenses importées
        if db.get_settings(c).get("recurring_scan") != "1":
            logic.detect_recurring(c)
            db.set_settings({"recurring_scan": "1"}, c)
            c.commit()
    except Exception:  # noqa: BLE001
        pass
    finally:
        c.close()
    threading.Thread(target=background_sync, daemon=True).start()
    threading.Thread(target=nightly_snapshot, daemon=True).start()
    try:
        backup.snapshot()  # une copie à chaque démarrage (mise à jour, redémarrage du Raspberry)
    except Exception:  # noqa: BLE001
        pass
    from waitress import serve
    serve(app, host="0.0.0.0", port=int(os.environ.get("FACTU_PORT", "8099")), threads=6)


if __name__ == "__main__":
    main()
