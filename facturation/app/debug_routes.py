"""Points de diagnostic (accessibles uniquement via Home Assistant)."""
import os
import time
from datetime import date, timedelta

from flask import jsonify

import db
import logic
import superpdp


def register(app, conn):
    @app.route("/api/debug/pdftext/<int:doc_id>")
    def api_debug_pdftext(doc_id):
        """Texte extrait d'un PDF archivé, analyse, et lignes enregistrées (réglage de l'import PDF)."""
        from flask import request
        import pdfimport
        c = conn()
        try:
            row = c.execute("SELECT pdf_path, number, total_ht, total_ttc, issue_date FROM documents WHERE id=?",
                            (doc_id,)).fetchone()
            if not row or not row["pdf_path"]:
                return jsonify({"error": "pas de PDF"}), 404
            with open(row["pdf_path"], "rb") as fh:
                data = fh.read()
            text = pdfimport.pdf_text(data, request.args.get("mode") or "plain")
            stored = [dict(r) for r in c.execute(
                "SELECT kind, designation, description, quantity, unit, unit_price, vat_rate FROM lines "
                "WHERE document_id=? ORDER BY position", (doc_id,))]
            return jsonify({"doc": dict(row), "text": text, "stored": stored,
                            "parsed": pdfimport.jsonable(pdfimport.parse_text(text))})
        finally:
            c.close()

    @app.route("/api/debug/pdfcheck")
    def api_debug_pdfcheck():
        """Analyse tous les PDF archivés et compare aux montants enregistrés."""
        import pdfimport
        c = conn()
        try:
            rows = c.execute("SELECT id, number, total_ht, total_ttc, issue_date, pdf_path FROM documents "
                             "WHERE pdf_path IS NOT NULL AND imported=1 ORDER BY id").fetchall()
            bad, ok, warn = [], 0, 0
            for r in rows:
                try:
                    with open(r["pdf_path"], "rb") as fh:
                        p = pdfimport.parse_quote(fh.read())
                except Exception as exc:  # noqa: BLE001
                    bad.append({"id": r["id"], "err": str(exc)[:120]})
                    continue
                n_items = c.execute("SELECT COUNT(*) FROM lines WHERE document_id=? AND COALESCE(kind,'item')='item'",
                                    (r["id"],)).fetchone()[0]
                pi = sum(1 for ln in p["lines"] if ln["kind"] == "item")
                probs = []
                if p["number"] != r["number"]:
                    probs.append(f"num {p['number']}")
                if p["issue_date"] != r["issue_date"]:
                    probs.append(f"date {p['issue_date']}")
                if p["total_ht"] is None or abs(float(p["total_ht"]) - r["total_ht"]) > 0.01:
                    probs.append(f"ht {p['total_ht']}")
                if p["total_ttc"] is None or abs(float(p["total_ttc"]) - r["total_ttc"]) > 0.01:
                    probs.append(f"ttc {p['total_ttc']}")
                if pi != n_items:
                    probs.append(f"items {pi}/{n_items}")
                if p["warnings"]:
                    warn += 1
                if probs or p["warnings"]:
                    bad.append({"id": r["id"], "n": r["number"], "p": probs, "w": p["warnings"][:2]})
                else:
                    ok += 1
            return jsonify({"total": len(rows), "ok": ok, "with_warnings": warn, "bad": bad})
        finally:
            c.close()

    @app.route("/api/debug/annuaire/<siren>")
    def api_debug_directory(siren):
        c = conn()
        try:
            api = superpdp.Client("x", "x", db.get_settings(c).get("pdp_endpoint"))
            out = {}
            for name, fn in (("companies", api.lookup_company), ("entries", api.directory_entries)):
                try:
                    out[name] = fn(siren)
                except Exception as e:  # noqa: BLE001
                    out[name] = {"error": str(e)}
            return jsonify(out)
        finally:
            c.close()


    @app.route("/api/debug/pdp")
    def api_debug_pdp():
        c = conn()
        try:
            s = db.get_settings(c)
            out = {"client_id_set": bool(s.get("pdp_client_id")), "secret_set": bool(s.get("pdp_client_secret")),
                   "endpoint": s.get("pdp_endpoint"), "siren": s.get("siren"),
                   "company_ok": bool(s.get("company_name") and s.get("siret"))}
            if out["client_id_set"] and out["secret_set"]:
                try:
                    res = logic.test_connection(c)
                    c.commit()
                    out["company"] = res.get("company")
                    out["address"] = res.get("address")
                    out["directory_error"] = res.get("directory_error")
                    try:
                        out["session"] = logic.pdp_client(c).session()
                    except Exception as e:  # noqa: BLE001
                        out["session_error"] = str(e)
                except Exception as e:  # noqa: BLE001
                    out["error"] = str(e)
            return jsonify(out)
        finally:
            c.close()


    @app.route("/api/debug/validation")
    def api_debug_validation():
        """Construit une facture fictive (non enregistrée), la convertit en Factur-X et la fait valider."""
        import tempfile
        from pdf import build_pdf
        c = conn()
        try:
            seller = logic.snapshot_seller(c)
            client = {"kind": "pro", "name": "Client Test SARL", "siren": "552081317", "vat_number": "FR03552081317",
                      "address": "22-30 avenue de Wagram", "post_code": "75008", "city": "Paris", "country": "FR"}
            doc = {"id": 0, "kind": "facture", "number": "TEST-0001", "issue_date": date.today().isoformat(),
                   "due_date": (date.today() + timedelta(days=30)).isoformat(), "subject": "Facture de test",
                   "notes": None, "service_date": None}
            lines = [{"designation": "Prestation de test", "description": "", "quantity": 2, "unit": "h",
                      "unit_price": 60, "vat_rate": 20},
                     {"designation": "Frais", "description": "", "quantity": 1, "unit": "u", "unit_price": 15.5,
                      "vat_rate": 10}]
            path = os.path.join(tempfile.gettempdir(), "test-facture.pdf")
            build_pdf(doc, lines, seller, client, path)
            en = superpdp.build_en_invoice(doc, lines, seller, client, {})
            api = logic.pdp_client(c)
            out = {"en_invoice": en}
            try:
                with open(path, "rb") as f:
                    fx = api.to_facturx(db.dumps(en), f.read())
                out["facturx_bytes"] = len(fx)
                out["validation"] = api.validate(fx)
            except Exception as e:  # noqa: BLE001
                out["error"] = str(e)
            return jsonify(out)
        finally:
            c.close()


    @app.route("/api/debug/sandbox-e2e")
    def api_debug_sandbox_e2e():
        """Envoi réel en bac à sable : facture fictive (non enregistrée) -> client de test -> statut Encaissée."""
        import tempfile
        from pdf import build_pdf
        c = conn()
        steps = []
        try:
            api = logic.pdp_client(c)
            me = api.me()
            if me.get("env") != "sandbox":
                return jsonify({"error": "Refusé : identifiants de production. Ce test ne tourne qu'en bac à sable."}), 400
            steps.append(f"Entreprise de test : {me.get('formal_name')} ({me.get('number')})")
            tpl = api.convert_to_en(api.generate_test_invoice("cii"))
            steps.append("Facture modèle du bac à sable récupérée")
            seller = logic.snapshot_seller(c)
            seller.update({"company_name": me.get("formal_name") or seller["company_name"],
                           "address": me.get("address") or seller["address"], "post_code": me.get("postcode") or "",
                           "city": me.get("city") or "", "siren": me.get("number"), "siret": "",
                           "vat_number": (tpl.get("seller") or {}).get("vat_identifier") or ""})
            b = tpl.get("buyer") or {}
            pa = b.get("postal_address") or {}
            client = {"kind": "pro", "name": b.get("name"), "address": pa.get("address_line1", ""),
                      "post_code": pa.get("post_code", ""), "city": pa.get("city", ""),
                      "country": pa.get("country_code", "FR"),
                      "siren": (b.get("legal_registration_identifier") or {}).get("value", ""),
                      "vat_number": b.get("vat_identifier") or ""}
            num = f"TEST-{int(time.time())}"
            doc = {"id": 0, "kind": "facture", "number": num, "issue_date": date.today().isoformat(),
                   "due_date": (date.today() + timedelta(days=30)).isoformat(), "subject": "Test bout en bout",
                   "notes": None, "service_date": None}
            lines = [{"designation": "Prestation de test", "description": "", "quantity": 2, "unit": "h",
                      "unit_price": 60, "vat_rate": 20},
                     {"designation": "Frais", "description": "", "quantity": 1, "unit": "u", "unit_price": 15.5,
                      "vat_rate": 10}]
            en = superpdp.build_en_invoice(doc, lines, seller, client, {})
            # identifiants du bac à sable (schémas spécifiques)
            for k in ("electronic_address", "legal_registration_identifier", "identifiers"):
                if (tpl.get("seller") or {}).get(k):
                    en["seller"][k] = tpl["seller"][k]
                if b.get(k):
                    en["buyer"][k] = b[k]
                elif k in en["buyer"]:
                    en["buyer"].pop(k)
            path = os.path.join(tempfile.gettempdir(), "e2e.pdf")
            build_pdf(doc, lines, seller, client, path)
            with open(path, "rb") as f:
                fx = api.to_facturx(db.dumps(en), f.read())
            val = api.validate(fx)
            ok = all(r.get("is_valid") for r in val.get("data", []))
            steps.append(f"Factur-X générée et validée : {'valide' if ok else 'INVALIDE'}")
            if not ok:
                return jsonify({"steps": steps, "validation": val})
            sent = api.send(fx, num)
            steps.append(f"Facture {num} déposée (id {sent.get('id')}) pour {client['name']}")
            events = []
            for _ in range(15):
                time.sleep(2)
                events = (api.events(sent["id"]).get("data") or [])
                if any(e.get("status_code") in ("fr:202", "fr:203", "fr:213", "fr:501") for e in events):
                    break
            steps += [f"Statut : {superpdp.STATUS_LABELS.get(e['status_code'], e.get('status_text'))} ({e['status_code']})"
                      for e in events]
            from calc import vat_breakdown, totals
            ttc = totals(lines)["ttc"]
            api.post_event(superpdp.payment_event(sent["id"], date.today().isoformat(), ttc, vat_breakdown(lines)))
            time.sleep(2)
            last = (api.events(sent["id"]).get("data") or [])
            if last:
                e = last[-1]
                steps.append(f"Après encaissement : {superpdp.STATUS_LABELS.get(e['status_code'], e.get('status_text'))} "
                             f"({e['status_code']})")
            return jsonify({"steps": steps, "events": last})
        except Exception as e:  # noqa: BLE001
            steps.append(f"ERREUR : {e}")
            return jsonify({"steps": steps}), 500
        finally:
            c.close()


