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
    for m in ["db", "calc", "logic", "superpdp", "pdf", "importer", "monae", "app"]:
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
        {"id": 4, "customer_id": 10, "invoice_ref": "BROUILLON", "draft": True, "total": "1", "items": []},
    ]
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
        if path.endswith(".pdf"):
            return Resp(content=b"%PDF-1.4 original")
        raise AssertionError(path)

    monkeypatch.setattr(monae.Api, "get", fake_get)
    monae._run(monae.Api("u", "k"), None, True)
    job = monae.get_job()
    assert job["status"] == "done", job
    r = job["report"]

    assert (r["clients"], r["invoices"], r["avoirs"], r["approx"]) == (2, 2, 1, 0)
    assert r["numbering"]["pattern"] == "{AAAA}{MM}-{N}" and r["numbering"]["next"] == 44
    conn = db.connect()
    docs = {d["number"]: d for d in conn.execute("SELECT * FROM documents").fetchall()}
    assert docs["202609-41"]["status"] == "payee" and docs["202609-41"]["total_ttc"] == 1200
    assert docs["202610-42"]["status"] == "partielle" and docs["202610-42"]["total_ttc"] == 600
    assert docs["202610-43"]["kind"] == "avoir" and docs["202610-43"]["total_ttc"] == 120
    assert os.path.exists(docs["202609-41"]["pdf_path"])
    cl = conn.execute("SELECT * FROM clients WHERE name='Agence Dupont'").fetchone()
    assert cl["siren"] == "732829320" and cl["pdp_address_value"] == "732829320"
    # numérotation continue
    import logic
    assert logic.preview_number(conn, "facture", "2026-11-20") == "202611-44"
    conn.close()
