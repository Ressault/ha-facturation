import io
import os
import sys
import tempfile
from datetime import date
from decimal import Decimal

import pytest

APP = os.path.join(os.path.dirname(__file__), "..", "facturation", "app")
sys.path.insert(0, os.path.abspath(APP))


@pytest.fixture()
def env(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("FACTU_DATA_DIR", tmp)
    for m in ["db", "calc", "logic", "superpdp", "pdf", "importer", "monae", "backup", "pdfimport", "app"]:
        sys.modules.pop(m, None)
    import app as appmod
    import db
    db.DATA_DIR = tmp
    db.DB_PATH = os.path.join(tmp, "facturation.db")
    db.ARCHIVE_DIR = os.path.join(tmp, "archives")
    db.init_db()
    db.set_settings({
        "company_name": "Antonin Ressault", "address": "1 rue de la Paix", "post_code": "37600",
        "city": "Loches", "siret": "12345678900012", "siren": "123456789", "vat_number": "FR32123456789",
        "iban": "FR7630006000011234567890189", "bic": "AGRIFRPPXXX", "email": "a@example.fr",
    })
    appmod.app.config["TESTING"] = True
    return appmod, db, appmod.app.test_client()


def make_client(c, **kw):
    data = {"kind": "pro", "name": "Agence Dupont", "siren": "732829320", "address": "2 avenue X",
            "post_code": "75001", "city": "Paris", "country": "FR", "email": "c@dupont.fr"}
    data.update(kw)
    r = c.post("/clients/nouveau", data=data)
    assert r.status_code == 302
    return int(r.headers["Location"].rstrip("/").split("/")[-1])


def new_doc(c, kind, client_id, lines, action="enregistrer", issue=None):
    data = {"kind": kind, "client_id": client_id, "issue_date": issue or date.today().isoformat(),
            "action": action, "subject": "Prestation test"}
    form = []
    for k, v in data.items():
        form.append((k, v))
    for ln in lines:
        for k in ["designation", "description", "quantity", "unit", "unit_price", "vat_rate"]:
            form.append((k, str(ln.get(k, ""))))
    from werkzeug.datastructures import MultiDict
    r = c.post("/documents/enregistrer", data=MultiDict(form))
    assert r.status_code == 302, r.data
    return int(r.headers["Location"].rstrip("/").split("/")[-1])


def test_full_flow(env):
    appmod, db, c = env
    cid = make_client(c)
    conn = db.connect()
    cl = conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
    assert cl["vat_number"] == "FR44732829320"  # clé TVA calculée

    # devis
    did = new_doc(c, "devis", cid, [{"designation": "Modélisation 3D", "quantity": "2", "unit": "jour",
                                     "unit_price": "450", "vat_rate": "20"}])
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
    assert doc["number"].startswith("D-") and doc["total_ttc"] == 1080
    assert os.path.exists(doc["pdf_path"])

    # devis -> facture
    r = c.post(f"/documents/{did}/facturer")
    fid = int(r.headers["Location"].split("/")[-2])
    r = c.post(f"/documents/{fid}/emettre")
    assert r.status_code == 302
    f = conn.execute("SELECT * FROM documents WHERE id=?", (fid,)).fetchone()
    assert f["number"] == f"F-{date.today().year}-0001" and f["status"] == "emise"
    assert f["due_date"] and os.path.exists(f["pdf_path"])
    assert conn.execute("SELECT status FROM documents WHERE id=?", (did,)).fetchone()["status"] == "facture"

    # facture non modifiable
    r = c.get(f"/documents/{fid}/modifier")
    assert r.status_code == 302

    # paiement partiel puis solde
    c.post(f"/documents/{fid}/paiement", data={"date": "2026-10-05", "amount": "500", "method": "virement"})
    assert conn.execute("SELECT status FROM documents WHERE id=?", (fid,)).fetchone()["status"] == "partielle"
    c.post(f"/documents/{fid}/paiement", data={"date": "2026-10-20", "amount": "580", "method": "virement"})
    assert conn.execute("SELECT status FROM documents WHERE id=?", (fid,)).fetchone()["status"] == "payee"

    # achat avec TVA
    c.post("/achats/nouveau", data={"supplier": "LDLC", "invoice_date": "2026-10-03", "total_ht": "1000",
                                    "total_tva": "200", "category": "immobilisation", "paid_date": "2026-10-03"})
    c.post("/achats/nouveau", data={"supplier": "Free", "invoice_date": "2026-10-10", "total_ht": "25",
                                    "total_tva": "5", "paid_date": "2026-10-12"})

    import calc
    res = calc.compute_period(conn, "2026-10")
    assert res["a1_base"] == 900
    assert res["lines_brute"][0]["code"] == "08"
    assert res["lines_brute"][0]["base_e"] == 900 and res["lines_brute"][0]["tva_e"] == 180
    assert res["l16"] == 180 and res["l19"] == 200 and res["l20"] == 5 and res["l23"] == 205
    assert res["l25"] == 25 and res["l28"] == 0
    assert float(res["ca_urssaf"]) == 900.0

    # déclaration -> crédit reporté le mois suivant
    c.post("/tva/declarer", data={"period": "2026-10"})
    res11 = calc.compute_period(conn, "2026-11")
    assert res11["l22"] == 25

    # pages
    for url in ["/", "/clients", f"/clients/{cid}", "/documents?kind=facture", "/documents?kind=devis",
                f"/documents/{fid}", f"/documents/{did}", "/achats", "/achats/nouveau", "/tva?mois=2026-10",
                "/reglages", "/import", f"/documents/{fid}/pdf", "/documents/nouveau?kind=facture"]:
        r = c.get(url)
        assert r.status_code == 200, url

    # avoir partiel + remboursement : diminue la TVA du mois
    r = c.post(f"/documents/{fid}/avoir")
    aid = int(r.headers["Location"].split("/")[-2])
    new = new_doc  # noqa
    from werkzeug.datastructures import MultiDict
    form = MultiDict([("id", aid), ("kind", "avoir"), ("client_id", cid), ("issue_date", date.today().isoformat()),
                      ("avoir_of_id", fid), ("action", "valider"), ("designation", "Remise geste commercial"),
                      ("description", ""), ("quantity", "1"), ("unit", "u"), ("unit_price", "100"), ("vat_rate", "20")])
    c.post("/documents/enregistrer", data=form)
    a = conn.execute("SELECT * FROM documents WHERE id=?", (aid,)).fetchone()
    assert a["number"].startswith("AV-") and a["total_ttc"] == 120
    c.post(f"/documents/{aid}/paiement", data={"date": "2026-11-04", "amount": "120"})
    res11 = calc.compute_period(conn, "2026-11")
    assert res11["lines_brute"][0]["base_e"] == -100 and res11["lines_brute"][0]["tva_e"] == -20
    conn.close()


def test_chronology_and_siren(env):
    appmod, db, c = env
    cid = make_client(c, siren="")
    fid = new_doc(c, "facture", cid, [{"designation": "X", "quantity": "1", "unit_price": "10", "vat_rate": "20"}])
    r = c.post(f"/documents/{fid}/emettre", follow_redirects=True)
    assert "SIREN du client est obligatoire" in r.get_data(as_text=True)


def test_split_payment_and_breakdown(env):
    import calc
    lines = [{"quantity": 1, "unit_price": 100, "vat_rate": 20}, {"quantity": 1, "unit_price": 50, "vat_rate": 5.5}]
    bd = calc.vat_breakdown(lines)
    assert bd == [(Decimal("20"), Decimal("100.00"), Decimal("20.00")), (Decimal("5.5"), Decimal("50.00"), Decimal("2.75"))]
    parts = calc.split_payment("172.75", bd)
    assert sum(b + t for _, b, t in parts) == Decimal("172.75")
    assert parts[0][1] == Decimal("100.00") and parts[1][2] == Decimal("2.75")


def test_en16931_and_send(env, monkeypatch):
    appmod, db, c = env
    import logic
    import superpdp
    calls = {}

    def fake_token(self):
        return "tok"

    def fake_convert(self, en_json, pdf_bytes):
        import json
        calls["en"] = json.loads(en_json)
        assert pdf_bytes[:4] == b"%PDF"
        return b"%PDF-facturx"

    def fake_send(self, fx, ext):
        calls["ext"] = ext
        return {"id": 4242}

    def fake_event(self, payload):
        calls["event"] = payload
        return {"id": 1}

    monkeypatch.setattr(superpdp.Client, "token", fake_token)
    monkeypatch.setattr(superpdp.Client, "to_facturx", fake_convert)
    monkeypatch.setattr(superpdp.Client, "send", fake_send)
    monkeypatch.setattr(superpdp.Client, "post_event", fake_event)
    db.set_settings({"pdp_client_id": "id", "pdp_client_secret": "secret"})

    cid = make_client(c)
    fid = new_doc(c, "facture", cid, [
        {"designation": "Rendu 3D", "quantity": "3", "unit": "h", "unit_price": "60", "vat_rate": "20"},
        {"designation": "Impression", "quantity": "1", "unit_price": "40", "vat_rate": "10"}], action="valider")
    conn = db.connect()
    f = conn.execute("SELECT * FROM documents WHERE id=?", (fid,)).fetchone()
    assert f["pdp_id"] == 4242, f["pdp_error"]
    en = calls["en"]
    assert en["type_code"] == 380 and en["number"] == f["number"]
    assert en["seller"]["legal_registration_identifier"] == {"scheme": "0002", "value": "123456789"}
    assert en["buyer"]["legal_registration_identifier"]["value"] == "732829320"
    assert en["totals"]["total_without_vat"] == "220.00"
    assert en["totals"]["total_vat_amount"]["value"] == "40.00"
    assert en["totals"]["total_with_vat"] == "260.00"
    assert {v["vat_category_rate"] for v in en["vat_break_down"]} == {"20", "10"}
    assert en["lines"][0]["invoiced_quantity_code"] == "HUR"
    assert en["payment_instructions"]["credit_transfers"][0]["payment_account_identifier"]["value"].startswith("FR76")
    assert any(n.get("subject_code") == "PMT" for n in en["notes"])

    c.post(f"/documents/{fid}/paiement", data={"date": "2026-11-10", "amount": "260"})
    ev = calls["event"]
    assert ev["status_code"] == "fr:212" and ev["invoice_id"] == 4242
    amounts = {r["value_percent"]: r["amount"] for r in ev["details"][0]["reported_data"]}
    assert amounts == {"20.0": "216.00", "10.0": "44.00"}
    assert conn.execute("SELECT pdp_reported FROM payments WHERE document_id=?", (fid,)).fetchone()[0] == 1
    conn.close()


def test_import_csv(env):
    appmod, db, c = env
    clients_csv = "Raison sociale;SIRET;Adresse;Code postal;Ville;Email\nAgence Dupont;73282932000074;2 av X;75001;Paris;c@d.fr\n"
    inv_csv = ("Numéro;Date;Client;Total HT;TVA;Total TTC;Statut;Date de paiement\n"
               "F2026-0041;15/09/2026;Agence Dupont;1 000,00;200,00;1 200,00;Payée;30/09/2026\n"
               "F2026-0042;02/10/2026;Nouveau Client;500,00;100,00;600,00;En attente;\n")
    r = c.post("/import", data={"files": [(io.BytesIO(clients_csv.encode("cp1252")), "clients.csv"),
                                          (io.BytesIO(inv_csv.encode("utf-8")), "factures.csv")]},
               content_type="multipart/form-data")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "<strong>1</strong> client(s) et <strong>2</strong> facture(s)" in body, body
    conn = db.connect()
    rows = conn.execute("SELECT number, status, total_ttc FROM documents ORDER BY number").fetchall()
    assert [(r["number"], r["status"], r["total_ttc"]) for r in rows] == [
        ("F2026-0041", "payee", 1200.0), ("F2026-0042", "emise", 600.0)]
    assert conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 2
    conn.close()


def test_numbering_patterns(env):
    import logic
    assert logic.format_number("F-{AAAA}-{NNNN}", 2026, 7) == "F-2026-0007"
    assert logic.format_number("FA{AA}{NNN}", 2027, 12) == "FA27012"
    assert logic.vat_number_from_siren("732829320") == "FR44732829320"


def test_pick_address(env):
    import logic
    res = {"data": [{"identifier": "0225:552081317", "is_active": False},
                    {"identifier": "0225:552081317_A", "is_active": True},
                    {"identifier": "0225:552081317_B", "is_active": True}]}
    assert logic.pick_address(res, "552081317") is None
    res["data"][0]["is_active"] = True
    assert logic.pick_address(res, "552081317") == ("0225", "552081317")
    assert logic.pick_address({"data": [{"identifier": "0225:123456789_X", "is_active": True}]}, "123456789") == ("0225", "123456789_X")


def test_monae_import(env, monkeypatch):
    appmod, db, c = env
    import monae

    class Resp:
        def __init__(self, data=None, content=b"", pages=1):
            self._d, self.content = data, content
            self.headers = {"X-Pagination": '{"total_pages": %d}' % pages}

        def json(self):
            return self._d

    invoices = [
        {"id": 1, "customer_id": 10, "invoice_ref": "202609-41", "invoiced_on": "2026-09-15", "term_on": "2026-10-15",
         "title": "Rendus", "total": "1000.0", "total_with_vat": "1200.0", "paid_on": "2026-09-30", "payment_mode": 3,
         "draft": False, "rebate_percentage": "0.0", "settlements": [],
         "items": [{"position": 1, "title": "Rendu", "quantity": "2.0", "unit_price": "500.0", "vat": "0.200"},
                   {"position": 2, "title": "Section", "quantity": "0.0", "unit_price": "0.0", "vat": "0.200"}]},
        {"id": 2, "customer_id": 10, "invoice_ref": "202610-42", "invoiced_on": "2026-10-02", "title": "Visite",
         "total": "500.0", "total_with_vat": "600.0", "paid_on": None, "draft": False, "rebate_percentage": "10.0",
         "settlements": [{"paid_on": "2026-10-20", "total": "200.0", "payment_mode": 2}],
         "items": [{"position": 1, "title": "Visite 360", "quantity": "1.0", "unit_price": "555.555", "vat": "0.200"}]},
        {"id": 3, "customer_id": 11, "invoice_ref": "202610-43", "invoiced_on": "2026-10-05", "title": "Avoir",
         "total": "-100.0", "total_with_vat": "-120.0", "paid_on": "2026-10-05", "draft": False,
         "items": [{"position": 1, "title": "Geste", "quantity": "1.0", "unit_price": "-100.0", "vat": "0.200"}]},
        {"id": 4, "customer_id": 10, "invoice_ref": None, "draft": True, "invoiced_on": "2026-11-14", "title": "Chalets",
         "total": "1875.0", "total_with_vat": "2250.0", "pay_before": "45fm-inv",
         "items": [{"position": 1, "title": "Visite virtuelle", "style": "title", "quantity": "0", "unit_price": "0", "vat": "0"},
                   {"position": 2, "title": "Rendu nuit\nVue piscine", "quantity": "1.0", "unit_price": "1875.0", "vat": "0.200", "nature": 3},
                   {"position": 3, "title": "Livré en 4K", "style": "comment", "quantity": "0", "unit_price": "0", "vat": "0"}]},
    ]
    quotes = [{"id": 50, "customer_id": 10, "quote_ref": 12, "invoiced_on": "2026-10-01", "term_on": "2026-11-01",
               "total": "300.0", "total_with_vat": "360.0", "quote_status": 0, "draft": False,
               "items": [{"position": 1, "title": "Devis rendu", "quantity": "1", "unit_price": "300", "vat": "0.200"}]},
              # même numéro qu'une facture : ne doit jamais l'écraser
              {"id": 1, "customer_id": 11, "quote_ref": "202609-41", "invoiced_on": "2026-08-01", "total": "5000.0",
               "total_with_vat": "6000.0", "quote_status": 1, "draft": False,
               "items": [{"position": 1, "title": "Musée", "quantity": "1", "unit_price": "5000", "vat": "0.200"}]}]
    suppliers = [{"id": 70, "company_name": "Spotify", "city": "Stockholm"}, {"id": 71, "company_name": "Autodesk"}]
    purchases = [{"id": 80, "supplier_id": 70, "title": "abonnement spotify", "invoiced_on": "2026-10-23",
                  "paid_on": "2026-10-23", "payment_mode": 1, "total": "9.27", "total_with_vat": "11.12", "vat_amount": "1.85"},
                 {"id": 81, "supplier_id": 71, "title": "license 3ds max", "invoiced_on": "2026-07-06", "paid_on": "2026-07-06",
                  "payment_mode": 3, "total": "285.0", "total_with_vat": "342.0", "vat_amount": "57.0"}]
    recurring = [{"id": 90, "supplier_id": 70, "title": "abonnement spotify", "total": "9.27", "total_with_vat": "11.12",
                  "vat_amount": "1.85", "frequency": 30, "next_on": "2026-11-23", "occurrences": 22}]
    customers = [{"id": 10, "company_name": "Agence Dupont", "siret": "73282932000074", "street": "2 av", "zip_code": "75001",
                  "city": "Paris", "country": "FR", "individual": False, "electronic_address": "0225:732829320"},
                 {"id": 11, "company_name": "", "first_name": "Jean", "last_name": "Martin", "individual": True}]

    def fake_get(self, path, params=None, raw=False):
        if path == "/account.json":
            return {"firms": [{"id": 99, "name": "Moi"}]}
        if path.endswith("/customers.json"):
            return Resp(customers)
        if path.endswith("/invoices.json"):
            return Resp(invoices)
        if path.endswith("/quotes.json"):
            return Resp(quotes)
        if path.endswith("/suppliers.json"):
            return Resp(suppliers)
        if path.endswith("/purchases.json"):
            return Resp(purchases)
        if path.endswith("/recurring_purchases.json"):
            return Resp(recurring)
        if path.endswith(".pdf"):
            return Resp(content=b"%PDF-1.4 original")
        raise AssertionError(path)

    monkeypatch.setattr(monae.Api, "get", fake_get)
    # un contact fournisseur importé par erreur dans les clients
    conn0 = db.connect()
    conn0.execute("INSERT INTO clients(kind, name) VALUES ('pro', 'Autodesk')")
    conn0.commit()
    conn0.close()
    monae._run(monae.Api("u", "k"), None, True)
    job = monae.get_job()
    assert job["status"] == "done", job
    r = job["report"]

    assert (r["clients"], r["invoices"], r["avoirs"], r["approx"]) == (2, 2, 1, 0), r
    assert (r["drafts"], r["quotes"], r["suppliers"], r["purchases"], r["recurring"], r["moved"]) == (1, 2, 2, 2, 1, 1), r
    assert r["numbering"]["pattern"] == "{AAAA}{MM}-{N}" and r["numbering"]["next"] == 44
    conn = db.connect()
    docs = {d["number"]: d for d in conn.execute("SELECT * FROM documents").fetchall()}
    assert docs["202609-41"]["status"] == "payee" and docs["202609-41"]["total_ttc"] == 1200
    assert docs["202610-42"]["status"] == "partielle" and docs["202610-42"]["total_ttc"] == 600
    assert docs["202610-43"]["kind"] == "avoir" and docs["202610-43"]["total_ttc"] == 120
    assert os.path.exists(docs["202609-41"]["pdf_path"])
    cl = conn.execute("SELECT * FROM clients WHERE name='Agence Dupont'").fetchone()
    assert cl["siren"] == "732829320" and cl["pdp_address_value"] == "732829320"
    draft = conn.execute("SELECT * FROM documents WHERE status='brouillon'").fetchone()
    assert draft["payment_terms"] == "45fm-inv" and draft["number"] is None and draft["total_ttc"] == 2250
    kinds = [(l["kind"], l["nature"]) for l in conn.execute("SELECT kind, nature FROM lines WHERE document_id=? ORDER BY position", (draft["id"],))]
    assert kinds == [("title", None), ("item", "bnc"), ("comment", None)]
    item = conn.execute("SELECT * FROM lines WHERE document_id=? AND kind='item'", (draft["id"],)).fetchone()
    assert item["designation"] == "Rendu nuit" and item["description"] == "Vue piscine"
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE kind='devis'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM clients WHERE name='Autodesk'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0] == 2
    # relancer l'import : aucun doublon, documents complétés
    monae._run(monae.Api("u", "k"), None, True)
    r2 = monae.get_job()["report"]
    assert r2["invoices"] == 0 and r2["purchases"] == 0 and r2["drafts"] == 0 and r2["refreshed"] >= 4, r2
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 6
    inv = conn.execute("SELECT * FROM documents WHERE number='202609-41'").fetchone()
    assert inv["total_ttc"] == 1200 and inv["kind"] == "facture"
    assert conn.execute("SELECT total_ttc FROM documents WHERE number='MONAE-D202609-41'").fetchone()[0] == 6000
    # numérotation continue
    import logic
    assert logic.preview_number(conn, "facture", "2026-11-20") == "202611-44"
    conn.close()



def test_v2_features(env):
    appmod, db, c = env
    import calc
    import logic
    from datetime import date as _d
    cid = make_client(c)
    # conditions de règlement
    assert logic.due_date_for("2026-11-19", "30-inv") == "2026-12-19"
    assert logic.due_date_for("2026-11-19", "30fm-inv") == "2026-12-31"
    assert logic.due_date_for("2026-11-19", "00fm10-inv") == "2026-12-10"
    assert logic.due_date_for("2026-11-19", "30fm10-inv") == "2027-01-10"
    assert logic.due_date_for("2026-11-19", "07fm-inv") == "2026-11-30"
    assert logic.due_date_for("2026-11-19", "45fm-inv") == "2027-01-31"
    # lignes typées
    from werkzeug.datastructures import MultiDict
    rows = [("title", "Rendus", "", "", "", "", "", ""),
            ("item", "Rendu jour", "", "2", "u", "500", "20", "bnc"),
            ("subtotal", "Sous-total", "", "", "", "", "", ""),
            ("comment", "Livraison en 4K", "", "", "", "", "", ""),
            ("page", "—", "", "", "", "", "", ""),
            ("item", "Frais de déplacement", "", "1", "u", "80", "0", "debours")]
    form = [("kind", "facture"), ("client_id", cid), ("issue_date", _d.today().isoformat()), ("payment_terms", "30fm-inv"),
            ("penalty_rate", "10"), ("discount_rate", "2"), ("action", "valider"), ("subject", "Test")]
    for r in rows:
        for k, v in zip(["kind_line", "designation", "description", "quantity", "unit", "unit_price", "vat_rate", "nature"], r):
            form.append((k, v))
    resp = c.post("/documents/enregistrer", data=MultiDict(form))
    fid = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    conn = db.connect()
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (fid,)).fetchone()
    assert doc["total_ht"] == 1080 and doc["total_ttc"] == 1280 and doc["penalty_rate"] == 10
    assert doc["due_date"] == logic.due_date_for(_d.today().isoformat(), "30fm-inv")
    assert [l["kind"] for l in conn.execute("SELECT kind FROM lines WHERE document_id=? ORDER BY position", (fid,))] == \
        ["title", "item", "subtotal", "comment", "page", "item"]
    assert c.get(f"/documents/{fid}").status_code == 200
    assert c.get(f"/documents/{fid}/pdf").status_code == 200
    # paiement total : TVA hors débours, URSSAF BNC hors débours
    c.post(f"/documents/{fid}/paiement", data={"date": "2026-10-10", "amount": "1280"})
    res = calc.compute_period(conn, "2026-10")
    assert res["a1_base"] == 1000 and res["l16"] == 200 and float(res["debours"]) == 80.0
    u = calc.compute_urssaf(conn, "2026-10-01", "2027-01-01", db.get_settings(conn))
    assert u["ca"]["bnc"] == 1000 and u["ca"]["debours"] == 80
    # « Supprimer » une facture finalisée => avoir automatique
    resp = c.post(f"/documents/{fid}/supprimer")
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (fid,)).fetchone()
    assert doc["status"] == "annulee" and doc["cancelled_by_id"]
    av = conn.execute("SELECT * FROM documents WHERE id=?", (doc["cancelled_by_id"],)).fetchone()
    assert av["kind"] == "avoir" and av["status"] != "brouillon" and av["number"].startswith("AV-") and av["total_ttc"] == 1280
    # navigation précédent/suivant
    fid2 = new_doc(c, "facture", cid, [{"designation": "Y", "quantity": "1", "unit_price": "10", "vat_rate": "20"}], action="valider")
    d2 = conn.execute("SELECT * FROM documents WHERE id=?", (fid2,)).fetchone()
    prev, nxt = logic.neighbours(conn, dict(d2))
    assert prev["id"] == fid and nxt is None
    # brouillon : vraie suppression
    did = new_doc(c, "facture", cid, [{"designation": "Z", "quantity": "1", "unit_price": "5", "vat_rate": "20"}])
    c.post(f"/documents/{did}/supprimer")
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE id=?", (did,)).fetchone()[0] == 0
    # achats récurrents
    c.post("/achats-recurrents/nouveau", data={"supplier": "Spotify", "title": "Abonnement", "total_ht": "9,27",
                                               "total_tva": "1,85", "every_months": "1", "next_date": "2026-09-23",
                                               "auto_paid": "1", "active": "1"})
    n = conn.execute("SELECT COUNT(*) FROM purchases WHERE recurring_id IS NOT NULL").fetchone()[0]
    rec = conn.execute("SELECT * FROM recurring_purchases").fetchone()
    expected = sum(1 for m in ["2026-09-23", "2026-10-23", "2026-11-23", "2026-12-23", "2027-01-23"] if m <= _d.today().isoformat())
    assert n == expected and rec["occurrences"] == expected and rec["next_date"] > _d.today().isoformat()
    # tâches cochables
    conn.execute("INSERT INTO documents(kind, number, status, client_id, issue_date, imported) VALUES "
                 "('facture', 'OLD-1', 'payee', ?, '2026-01-05', 1)", (cid,))
    conn.commit()
    tasks = logic.tasks(conn, _d(2026, 11, 19))
    assert any(t["id"] == "tva:2026-10" for t in tasks)
    c.post("/taches/tva:2026-10")
    tasks = logic.tasks(conn, _d(2026, 11, 19))
    assert not any(t["id"] == "tva:2026-10" for t in tasks)
    assert conn.execute("SELECT declared_at FROM declarations WHERE period='2026-10'").fetchone()[0]
    # fournisseurs / reclassement
    oid = make_client(c, name="Free Mobile", siren="")
    c.post("/clients/vers-fournisseurs", data={"ids": [str(oid), str(cid)]})
    assert conn.execute("SELECT COUNT(*) FROM clients WHERE id=?", (oid,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM clients WHERE id=?", (cid,)).fetchone()[0] == 1  # facturé : reste client
    assert conn.execute("SELECT COUNT(*) FROM suppliers WHERE name='Free Mobile'").fetchone()[0] == 1
    # toutes les pages
    for url in ["/", "/clients", "/fournisseurs", "/fournisseurs/nouveau", "/achats", "/achats-recurrents",
                "/achats-recurrents/nouveau", "/tva", "/urssaf", "/export", "/reglages", "/import",
                "/documents?kind=facture&periode=annee&type=encaisse_le", "/documents?kind=facture&statut=echues",
                "/documents?kind=devis", "/documents/export.csv?kind=facture", "/achats/export.csv",
                f"/export/archives-{_d.today().year}.zip", "/documents/nouveau?kind=devis"]:
        r = c.get(url)
        assert r.status_code == 200, url
    conn.close()


def test_v3_periods_avoirs_recurring(env):
    appmod, db, client = env
    import calc
    import logic
    today = date(2026, 11, 19)
    # filtre période
    assert appmod.period_range("12m", today=today) == ("2025-11-20", "2026-11-20")
    assert appmod.period_range("m-2026-10", today=today) == ("2026-10-01", "2026-11-01")
    assert appmod.period_range("t-2026-3", today=today) == ("2026-07-01", "2026-10-01")
    assert appmod.period_range("a-2016", today=today) == ("2016-01-01", "2017-01-01")
    groups = appmod.period_groups(today)
    flat = [v for _, opts in groups for v, _ in opts]
    assert flat[:3] == ["tout", "12m", "m-2026-01"] and flat[13] == "m-2026-12" and "t-2026-4" in flat
    assert "a-2026" in flat and "a-2016" in flat and flat[-1] == "perso"
    assert appmod.period_choice("trimestre", today) == "t-2026-4"

    conn = db.connect()
    cid = conn.execute("INSERT INTO clients(kind, name) VALUES ('pro', 'Client T3')").lastrowid

    def doc(kind, num, day, ht, status="emise", avoir_of=None):
        i = conn.execute("INSERT INTO documents(kind, number, status, client_id, issue_date, total_ht, total_tva, total_ttc,"
                         " imported, avoir_of_id, client_snapshot) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                         (kind, num, status, cid, day, ht, ht * .2, ht * 1.2, avoir_of, '{"name": "Client T3"}')).lastrowid
        conn.execute("INSERT INTO lines(document_id, position, designation, quantity, unit_price, vat_rate) "
                     "VALUES (?, 1, 'x', 1, ?, 20)", (i, ht))
        return i
    f1 = doc("facture", "T3-1", "2025-07-02", 1000, "payee")
    conn.execute("INSERT INTO payments(document_id, date, amount, pdp_reported) VALUES (?, '2025-07-10', 1200, 1)", (f1,))
    doc("avoir", "T3-A1", "2025-08-03", 600)          # avoir importé, non rattaché : compte à sa date
    f2 = doc("facture", "T3-2", "2025-08-05", 500, "annulee")
    doc("avoir", "T3-A2", "2025-08-06", 500, avoir_of=f2)  # annulation d'une facture impayée : ne compte pas
    conn.commit()
    r = calc.compute_urssaf(conn, "2025-07-01", "2025-10-01", db.get_settings(conn))
    assert r["ca"]["bnc"] == 400 and r["n_docs"] == 2, r
    p = calc.compute_period(conn, "2025-08")
    assert p["a1_base"] == -600, p["a1_base"]

    # abonnement retrouvé dans les dépenses
    for d_ in ("2026-08-23", "2026-09-23", "2026-10-23"):
        conn.execute("INSERT INTO purchases(supplier, invoice_date, description, total_ht, total_tva, total_ttc, category,"
                     " paid_date, method) VALUES ('Spotify ', ?, 'abonnement spotify', 9.27, 1.85, 11.12, 'biens_services', ?, 'paypal')",
                     (d_, d_))
    conn.commit()
    assert logic.detect_recurring(conn, today) == 1
    assert logic.detect_recurring(conn, today) == 0
    rec = conn.execute("SELECT * FROM recurring_purchases ORDER BY id DESC").fetchone()
    assert rec["next_date"] == "2026-11-23" and rec["every_months"] == 1 and rec["method"] == "paypal"
    conn.close()

    av = db.connect().execute("SELECT id FROM documents WHERE number='T3-A1'").fetchone()["id"]
    for url in (f"/documents/{av}", "/documents?periode=t-2025-3&type=encaisse_le", "/documents?periode=12m",
                "/achats?periode=a-2016", "/achats-recurrents", "/urssaf?p=2025-T3", "/"):
        res = client.get(url)
        assert res.status_code == 200, (url, res.status_code)
    html = client.get("/documents?periode=t-2025-3&type=encaisse_le").get_data(as_text=True)
    assert "T3-A1" in html and "T3-1" in html and "T3-A2" not in html


def test_v4_purchase_vat_urssaf_filters(env):
    appmod, db, client = env
    import logic
    from datetime import date
    # dépense saisie en TTC avec TVA récupérable à 20 %
    r = client.post("/achats/nouveau", data={"supplier_new": "Adobe", "description": "Photoshop", "invoice_date": "2026-11-02",
                                             "total_ttc": "24,00", "vat_rate": "20", "total_tva": "", "method": "carte",
                                             "payment_ref": "CB ADOBE *PHOTOSHOP", "next": "new"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/achats/nouveau")
    conn = db.connect()
    p = conn.execute("SELECT * FROM purchases WHERE supplier='Adobe'").fetchone()
    assert (p["total_tva"], p["total_ht"], p["paid_date"], p["payment_ref"], p["vat_rate"]) == (4.0, 20.0, "2026-11-02", "CB ADOBE *PHOTOSHOP", 20.0)
    # montant libre et brouillon non compté
    client.post("/achats/nouveau", data={"supplier_pick": "Adobe", "total_ttc": "10", "vat_rate": "", "total_tva": "1,5",
                                         "invoice_date": "2026-11-03", "draft": "1"})
    d2 = conn.execute("SELECT * FROM purchases WHERE total_ttc=10").fetchone()
    assert d2["total_ht"] == 8.5 and d2["draft"] == 1 and d2["supplier_id"] == p["supplier_id"]
    html = client.get(f"/achats?periode=tout&fournisseur={p['supplier_id']}").get_data(as_text=True)
    assert "Photoshop" in html and "brouillon" in html
    # filtre société sur les factures
    cid = make_client(client)
    other = make_client(client, name="Autre client", siren="552100554")
    new_doc(client, "facture", cid, [{"designation": "Rendu", "quantity": 1, "unit_price": 100, "vat_rate": 20}], action="valider")
    new_doc(client, "facture", other, [{"designation": "Plan", "quantity": 1, "unit_price": 50, "vat_rate": 20}], action="valider")
    html = client.get(f"/documents?periode=tout&client={cid}").get_data(as_text=True)
    assert "Agence Dupont" in html and "Autre client" in html  # la liste déroulante contient les deux sociétés
    assert html.count('data-href="') == 1
    doc_id = conn.execute("SELECT id FROM documents WHERE client_id=? AND kind='facture'", (cid,)).fetchone()["id"]
    # règlement avec référence de paiement
    client.post(f"/documents/{doc_id}/paiement", data={"date": "2026-11-05", "amount": "120", "method": "virement",
                                                       "reference": "VIR SEPA AGENCE DUPONT FACT"})
    assert conn.execute("SELECT reference FROM payments WHERE document_id=?", (doc_id,)).fetchone()["reference"] == "VIR SEPA AGENCE DUPONT FACT"
    assert client.get(f"/documents/{doc_id}").status_code == 200
    # rapprochement d'un virement
    d3 = conn.execute("SELECT * FROM documents WHERE client_id=? AND kind='facture'", (other,)).fetchone()
    m = logic.match_transfer(conn, f"VIR SEPA AUTRE CLIENT FACTURE {d3['number']}", "60,00")
    assert m["matches"] and m["matches"][0]["doc"]["id"] == d3["id"] and m["amount"] == 60.0
    assert client.get("/?virement=VIR+AUTRE+CLIENT&montant=60").status_code == 200
    # cotisations URSSAF payées en partie → retard
    client.post("/urssaf/2026-T2/cotisations", data={"amount": "500"})
    client.post("/urssaf/2026-T2/paiement", data={"date": "2026-07-25", "amount": "300", "reference": "PRLV URSSAF"})
    a = logic.urssaf_arrears(conn, date(2026, 11, 19))
    assert a["total"] == 200 and a["periods"][0]["period"] == "2026-T2"
    r = new_doc(client, "facture", cid, [{"designation": "Brouillon", "quantity": 2, "unit_price": 10, "vat_rate": 20}])
    draft_id = conn.execute("SELECT id FROM documents WHERE status='brouillon'").fetchone()["id"]
    for url in ("/urssaf?p=2026-T2", "/", "/achats/nouveau", f"/documents/{doc_id}", f"/documents/{draft_id}/modifier",
                f"/documents/{draft_id}", "/documents/nouveau?kind=devis", "/documents/nouveau?kind=facture",
                f"/achats/{p['id']}"):
        assert client.get(url).status_code == 200, url
    assert "Retard URSSAF" in client.get("/").get_data(as_text=True)
    conn.close()


def test_v5_clients_options_preview(env):
    appmod, db, client = env
    import logic
    cid = make_client(client)
    lone = make_client(client, name="Sans document", siren="552100554")
    # devis avec une ligne en option, un saut de ligne et un titre vide (ignoré)
    form = [("kind", "devis"), ("client_id", cid), ("issue_date", "2026-11-19"), ("action", "enregistrer"), ("subject", "Devis")]
    rows = [("title", "", "", "", "", "", "", "", "0"),
            ("item", "Perspective", "détail\nsur deux lignes", "2", "u", "450", "20", "bnc", "0"),
            ("blank", "", "", "", "", "", "", "", "0"),
            ("item", "Vue aérienne", "", "1", "u", "300", "20", "bnc", "1")]
    for r in rows:
        for k, v in zip(["kind_line", "designation", "description", "quantity", "unit", "unit_price", "vat_rate", "nature", "optional"], r):
            form.append((k, v))
    from werkzeug.datastructures import MultiDict
    form = MultiDict(form)
    r = client.post("/documents/enregistrer", data=form)
    assert r.status_code == 302
    conn = db.connect()
    dv = conn.execute("SELECT * FROM documents WHERE kind='devis'").fetchone()
    assert dv["total_ht"] == 900 and dv["total_ttc"] == 1080
    kinds = [(x["kind"], x["optional"]) for x in conn.execute("SELECT * FROM lines WHERE document_id=? ORDER BY position", (dv["id"],))]
    assert kinds == [("item", 0), ("blank", 0), ("item", 1)]
    html = client.get(f"/documents/{dv['id']}").get_data(as_text=True)
    assert "Option" in html and "Options proposées" in html
    assert client.get(f"/documents/{dv['id']}/pdf").status_code == 200
    ed = client.get(f"/documents/{dv['id']}/modifier").get_data(as_text=True)
    assert 'class="grip"' in ed and "En option" in ed and 'data-kind="title"' in ed and "<option>mois</option>" not in ed and "<option>km</option>" not in ed
    # devis → facture : l'option ne passe pas
    r = client.post(f"/documents/{dv['id']}/facturer")
    inv_id = int(r.headers["Location"].rstrip("/").split("/")[-2]) if "/modifier" in r.headers["Location"] else \
        int(r.headers["Location"].rstrip("/").split("/")[-1])
    inv = conn.execute("SELECT * FROM documents WHERE id=?", (inv_id,)).fetchone()
    assert inv["kind"] == "facture" and inv["total_ht"] == 900
    assert conn.execute("SELECT COUNT(*) FROM lines WHERE document_id=? AND optional=1", (inv_id,)).fetchone()[0] == 0
    # aperçu PDF sans enregistrer
    r = client.post("/documents/apercu", data=form)
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE kind='devis'").fetchone()[0] == 1
    # nouvelle facture : ligne titre vide en tête
    assert 'data-kind="title"' in client.get("/documents/nouveau?kind=facture").get_data(as_text=True)
    # suppression des clients : sans document → supprimé ; avec documents → retiré des listes
    r = client.post("/clients/supprimer", data={"ids": [str(cid), str(lone)]})
    assert r.status_code == 302
    assert conn.execute("SELECT COUNT(*) FROM clients WHERE id=?", (lone,)).fetchone()[0] == 0
    assert conn.execute("SELECT archived FROM clients WHERE id=?", (cid,)).fetchone()[0] == 1
    assert "Agence Dupont" not in client.get("/clients").get_data(as_text=True).split("<table")[-1]
    new = client.get("/documents/nouveau?kind=facture").get_data(as_text=True)
    assert "Agence Dupont" not in new
    assert "Agence Dupont" in client.get("/clients?masques=1").get_data(as_text=True)
    client.post(f"/clients/{cid}/restaurer")
    assert conn.execute("SELECT archived FROM clients WHERE id=?", (cid,)).fetchone()[0] == 0
    assert client.get("/").status_code == 200
    conn.close()


def test_v6_services_dash_delete(env):
    appmod, db, client = env
    conn = db.connect()
    names = [r["designation"] for r in conn.execute("SELECT designation FROM services ORDER BY position")]
    assert names[:3] == ["Perspectives extérieures", "Perspectives intérieures", "Plan de masse"] and len(names) == 5
    assert conn.execute("SELECT unit_price FROM services WHERE designation='Cyclorama'").fetchone()[0] == 500
    ed = client.get("/documents/nouveau?kind=devis").get_data(as_text=True)
    assert "Plan de masse" in ed and 'class="pick"' in ed
    assert client.get("/prestations").status_code == 200
    client.post("/prestations", data={"designation": "Animation", "unit_price": "900", "description": "- 30 s", "unit": "u", "vat_rate": "20"})
    sid = conn.execute("SELECT id FROM services WHERE designation='Animation'").fetchone()[0]
    client.post("/prestations", data={"id": sid, "delete": "1"})
    assert not conn.execute("SELECT 1 FROM services WHERE id=?", (sid,)).fetchone()
    # disposition de l'accueil
    r = client.post("/accueil/disposition", json={"tiles": [{"id": "listes", "w": 12}, {"id": "enc_annee", "w": 8}]})
    assert r.status_code == 200
    lay = appmod.dash_layout(conn)
    assert [t["id"] for t in lay][:2] == ["listes", "enc_annee"] and lay[1]["w"] == 8 and len(lay) == 9
    html = client.get("/").get_data(as_text=True)
    assert html.index('data-id="listes"') < html.index('data-id="enc_mois"') and "Réorganiser" in html
    client.post("/accueil/disposition", json={"reset": True})
    assert appmod.dash_layout(conn)[0]["id"] == "enc_mois"
    # suppression d'un devis déjà transformé en facture
    cid = make_client(client)
    new_doc(client, "devis", cid, [{"designation": "Rendu", "quantity": 1, "unit_price": 100, "vat_rate": 20}])
    dv = conn.execute("SELECT id FROM documents WHERE kind='devis'").fetchone()["id"]
    client.post(f"/documents/{dv}/facturer")
    r = client.post(f"/documents/{dv}/supprimer")
    assert r.status_code == 302
    assert not conn.execute("SELECT 1 FROM documents WHERE id=?", (dv,)).fetchone()
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE kind='facture'").fetchone()[0] == 1
    # site internet en pied de page, formulaire d'achat récurrent
    import pdf
    assert "www.exemple.fr" in pdf.legal_footer({"company_name": "A", "website": "www.exemple.fr"})
    assert pdf.group_iban("fr7630006000011234567890189") == "FR76 3000 6000 0112 3456 7890 189"
    r = client.post("/achats-recurrents/nouveau", data={"supplier_new": "Spotify", "title": "abonnement", "total_ttc": "11,12",
                                                        "vat_rate": "20", "every_months": "1", "next_date": "2099-01-23", "active": "1"})
    rec = conn.execute("SELECT * FROM recurring_purchases WHERE title='abonnement'").fetchone()
    assert rec["total_tva"] == 1.85 and rec["total_ht"] == 9.27
    assert client.get(f"/achats-recurrents/{rec['id']}").status_code == 200
    assert client.get("/documents?periode=tout&client=%d" % cid).status_code == 200
    conn.close()


def test_v7_backup_restore(env):
    appmod, db, client = env
    import zipfile, io, os
    cid = make_client(client)
    new_doc(client, "facture", cid, [{"designation": "Rendu", "quantity": 1, "unit_price": 100, "vat_rate": 20}], action="valider")
    r = client.get("/export/sauvegarde-complete.zip")
    assert r.status_code == 200
    data = r.get_data()
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    assert "facturation.db" in names and any(n.startswith("archives/") and n.endswith(".pdf") for n in names)
    # on casse les données puis on restaure
    conn = db.connect()
    conn.execute("DELETE FROM lines"); conn.execute("DELETE FROM payments"); conn.execute("DELETE FROM documents"); conn.commit()
    conn.close()
    r = client.post("/export/restaurer", data={"confirm": "RESTAURER", "fichier": (io.BytesIO(data), "s.zip")},
                    content_type="multipart/form-data")
    assert r.status_code == 302
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE kind='facture'").fetchone()[0] == 1
    conn.close()
    import backup
    assert backup.list_snapshots() or os.listdir(os.path.join(db.DATA_DIR, "sauvegardes"))
    r = client.post("/export/restaurer", data={"confirm": "non", "fichier": (io.BytesIO(data), "s.zip")},
                    content_type="multipart/form-data")
    assert r.status_code == 302
    assert client.get("/export").status_code == 200
    import pdf
    f = pdf.legal_footer({"company_name": "A", "siret": "1", "vat_number": "FR1"})
    assert f.split("\n")[1].startswith("TVA FR1")


def test_v9_vat_rounding_duplicate_summary(env):
    appmod, db, c = env
    import calc
    lines = [{"designation": "A", "quantity": 1, "unit_price": "0.13", "vat_rate": 20},
             {"designation": "B", "quantity": 1, "unit_price": "0.13", "vat_rate": 20}]
    # 0,026 + 0,026 : ligne par ligne 0,03 + 0,03 = 0,06 ; arrondi de la somme 0,052 -> 0,05
    assert calc.totals(lines, "2026-03-29")["tva"] == Decimal("0.06")
    assert calc.totals(lines, "2026-03-30")["tva"] == Decimal("0.05")
    cid = make_client(c)
    fid = new_doc(c, "facture", cid, [{"designation": "Perspective", "quantity": "1", "unit_price": "500",
                                       "vat_rate": "20"}])
    r = c.post(f"/documents/{fid}/dupliquer", data={"en": "devis"})
    assert r.status_code == 302
    conn = db.connect()
    dv = conn.execute("SELECT * FROM documents WHERE kind='devis' ORDER BY id DESC").fetchone()
    assert dv["number"].startswith("D-") and dv["status"] == "en_attente" and dv["total_ttc"] == 600
    r = c.post(f"/documents/{dv['id']}/dupliquer", data={"en": "facture"})
    f2 = conn.execute("SELECT * FROM documents WHERE kind='facture' ORDER BY id DESC").fetchone()
    assert f2["id"] != fid and f2["status"] == "brouillon"
    html = c.get("/documents?kind=facture").get_data(as_text=True)
    assert "Reste à encaisser avec brouillons" in html and 'data-ttc=' in html


def _monae_like_pdf():
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.add_font("dejavu", "", os.path.join(APP, "fonts", "DejaVuSans.ttf"))
    pdf.set_font("dejavu", size=10)
    for t in ["SARL DRIPMOON", "13 Rue Descartes", "37000 TOURS", "Client – SIRET : 51418612100067",
              "Objet : Deux perspectives", "Désignation % TVA PU HT Qté Total HT", "Projet Espacil",
              "Perspective : 20.0 % 500,00 € 2 1 000,00 €", "- Mise en texture", "- Post-production",
              "Total HT 1 000,00 €", "TVA 20.0 % 200,00 €", "TOTAL TTC 1 200,00 €",
              "DEVIS N° D-2025-007", "Date : 4 octobre 2025"]:
        pdf.cell(0, 6, t, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def test_v9_pdf_quote_import(env):
    appmod, db, c = env
    import json
    import pdfimport
    p = pdfimport.parse_quote(_monae_like_pdf())
    assert p["kind"] == "devis" and p["number"] == "D-2025-007" and p["issue_date"] == "2025-10-04"
    assert p["client"]["name"] == "SARL DRIPMOON" and p["client"]["post_code"] == "37000"
    items = [ln for ln in p["lines"] if ln["kind"] == "item"]
    assert len(items) == 1 and items[0]["unit_price"] == Decimal("500.00") and items[0]["quantity"] == 2
    assert "Mise en texture" in items[0]["description"] and not p["warnings"]
    r = c.post("/devis/import", data={"files": (io.BytesIO(_monae_like_pdf()), "devis.pdf")},
               content_type="multipart/form-data")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "D-2025-007" in html
    conn = db.connect()
    assert not conn.execute("SELECT 1 FROM documents WHERE number='D-2025-007'").fetchone()  # rien avant validation
    docs = json.loads(html.split("const DOCS = ")[1].split(";\n")[0])
    for d in docs:
        d.update(include=True, status="accepte")
    r = c.post("/devis/import", data={"payload": json.dumps(docs)})
    assert r.status_code == 302
    dv = conn.execute("SELECT * FROM documents WHERE number='D-2025-007'").fetchone()
    assert dv["kind"] == "devis" and dv["status"] == "accepte" and dv["total_ttc"] == 1200 and dv["imported"] == 1
    assert os.path.exists(dv["pdf_path"])
    assert conn.execute("SELECT name FROM clients WHERE id=?", (dv["client_id"],)).fetchone()["name"] == "SARL DRIPMOON"
    # un second import du même numéro est refusé
    r = c.post("/devis/import", data={"payload": json.dumps(docs)}, follow_redirects=True)
    assert "existe déjà" in r.get_data(as_text=True)


def test_v9_compta_exports(env):
    appmod, db, c = env
    cid = make_client(c)
    fid = new_doc(c, "facture", cid, [{"designation": "Perspective", "quantity": "1", "unit_price": "500",
                                       "vat_rate": "20"}], issue="2026-10-01")
    c.post(f"/documents/{fid}/emettre")
    c.post(f"/documents/{fid}/paiement", data={"date": "2026-10-20", "amount": "600", "method": "virement"})
    conn = db.connect()
    conn.execute("INSERT INTO purchases(supplier, invoice_number, invoice_date, total_ht, total_tva, total_ttc, "
                 "paid_date, method) VALUES ('Spotify', 'S1', '2026-10-05', 10, 2, 12, '2026-10-05', 'carte')")
    conn.commit()
    import compta
    ent = compta.entries(conn, "2026")
    assert sum(e["debit"] for e in ent) == sum(e["credit"] for e in ent)
    for j in ("VT", "AC", "BQ"):
        assert sum(e["debit"] for e in ent if e["journal"] == j) == sum(e["credit"] for e in ent if e["journal"] == j)
    r = c.get("/export/compta/recettes?annee=2026")
    txt = r.get_data(as_text=True)
    assert r.status_code == 200 and "20/10/2026" in txt and "600,00" in txt and "Virement" in txt
    r = c.get("/export/compta/ecritures?annee=2026&format=sheets")
    txt = r.get_data(as_text=True)
    assert "2026-10-01" in txt and "706000" in txt and "500.00" in txt and "Journal," in txt
    assert "Spotify" in c.get("/export/compta/achats?annee=2026").get_data(as_text=True)
    assert c.get("/export").status_code == 200


def test_v9_stats_pages(env):
    appmod, db, c = env
    cid = make_client(c)
    fid = new_doc(c, "facture", cid, [{"designation": "Perspective extérieure", "quantity": "2", "unit_price": "450",
                                       "vat_rate": "20"}, {"designation": "Visite virtuelle 360°", "quantity": "1",
                                       "unit_price": "500", "vat_rate": "20"}])
    c.post(f"/documents/{fid}/emettre")
    new_doc(c, "devis", cid, [{"designation": "Plan de masse", "quantity": "1", "unit_price": "200", "vat_rate": "20"}])
    conn = db.connect()
    conn.execute("INSERT INTO purchases(supplier, invoice_date, total_ht, total_tva, total_ttc) "
                 "VALUES ('Spotify', date('now'), 10, 2, 12)")
    conn.commit()
    import stats
    y = date.today().year
    st = stats.statistics(conn, y)
    assert round(st["total"]["ca"], 2) == 1400 and st["total"]["achats"] == 10 and round(st["total"]["benefice"], 2) == 1390
    recs = stats.sales(conn, "facture", f"{y}-01-01", f"{y + 1}-01-01")
    acts = stats.breakdown(recs, "activite")
    assert {r["name"]: round(r["value"]) for r in acts["rows"]} == {"Perspectives extérieures": 900,
                                                                     "Visites virtuelles 360°": 500}
    for url in ["/statistiques", "/statistiques?affichage=trimestre&ventilation=encaisse",
                "/statistiques?affichage=annee", "/statistiques?categorie=Cyclorama", "/previsionnel",
                "/ventilation-ca", "/ventilation-ca?ventilation=encaisse&periode=tout", "/ventilation-achats"]:
        r = c.get(url)
        assert r.status_code == 200, url
    assert "Spotify" in c.get("/ventilation-achats").get_data(as_text=True)
    html = c.get("/previsionnel").get_data(as_text=True)
    assert "400,00" in html and "Prestation test" in html


def test_v9_duplicate_draft_from_editor(env):
    appmod, db, c = env
    cid = make_client(c)
    did = new_doc(c, "facture", cid, [{"designation": "Insertion", "quantity": "2", "unit_price": "75", "vat_rate": "20"}])
    assert "Dupliquer en devis" in c.get(f"/documents/{did}/modifier").get_data(as_text=True)
    from werkzeug.datastructures import MultiDict
    form = MultiDict([("id", str(did)), ("kind", "facture"), ("client_id", str(cid)), ("issue_date", date.today().isoformat()),
                      ("subject", "Modifié"), ("designation", "Insertion"), ("description", ""), ("quantity", "3"),
                      ("unit", ""), ("unit_price", "75"), ("vat_rate", "20"), ("action", "dupliquer-devis")])
    r = c.post("/documents/enregistrer", data=form)
    assert r.status_code == 302 and "/modifier" in r.headers["Location"]
    conn = db.connect()
    src = conn.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
    dv = conn.execute("SELECT * FROM documents WHERE kind='devis'").fetchone()
    assert src["status"] == "brouillon" and src["total_ht"] == 225  # brouillon enregistré tel qu'affiché
    assert dv["number"].startswith("D-") and dv["total_ht"] == 225 and dv["subject"] == "Modifié"
    form["action"] = "dupliquer-facture"
    c.post("/documents/enregistrer", data=form)
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE kind='facture' AND status='brouillon'").fetchone()[0] == 2


def test_v9_quote_pdf_mentions(env):
    appmod, db, c = env
    cid = make_client(c)
    did = new_doc(c, "devis", cid, [{"designation": "Perspective", "quantity": "1", "unit_price": "500", "vat_rate": "20"}])
    conn = db.connect()
    dv = conn.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
    import pdfimport
    text = pdfimport.pdf_text(open(dv["pdf_path"], "rb").read())
    assert "Devis valide jusqu'au" in text and "Conditions de règlement appliquées sur les factures" in text
    assert "Date limite de règlement" in text and "40 €" in text
    assert f"BON DE COMMANDE – DEVIS N° {dv['number']}" in text and "Nom et qualité" in text and "Bon pour accord" in text
