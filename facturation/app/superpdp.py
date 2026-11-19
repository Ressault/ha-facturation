"""Client minimal pour l'API SUPER PDP (plateforme agréée).

Flux utilisé :
1. OAuth2 client_credentials  -> POST /oauth2/token
2. Modèle EN 16931 (JSON) + PDF -> POST /v1.beta/invoices/convert?from=en16931&to=factur-x
3. Dépôt de la Factur-X      -> POST /v1.beta/invoices (application/pdf)
4. Suivi                      -> GET /v1.beta/invoice_events?invoice_id=…
5. Encaissement (e-reporting des prestations de services) -> POST /v1.beta/invoice_events fr:212
6. Réception des factures fournisseurs -> GET /v1.beta/invoices?direction=in
"""
import time

import requests

from calc import d, money, totals, vat_breakdown

TIMEOUT = 30

STATUS_LABELS = {
    "fr:200": "Déposée",
    "fr:201": "Émise",
    "fr:202": "Reçue par la plateforme du client",
    "fr:203": "Mise à disposition du client",
    "fr:204": "Prise en charge",
    "fr:205": "Approuvée",
    "fr:206": "Approuvée partiellement",
    "fr:207": "En litige",
    "fr:208": "Suspendue",
    "fr:209": "Complétée",
    "fr:210": "Refusée",
    "fr:211": "Paiement transmis",
    "fr:212": "Encaissée",
    "fr:213": "Rejetée",
    "fr:501": "Irrecevable",
}
ERROR_STATUSES = {"fr:210", "fr:213", "fr:501"}


class PDPError(Exception):
    pass


class Client:
    def __init__(self, client_id, client_secret, endpoint="https://api.superpdp.tech"):
        if not client_id or not client_secret:
            raise PDPError("Identifiants SUPER PDP non renseignés (Réglages).")
        self.client_id = client_id
        self.client_secret = client_secret
        self.endpoint = endpoint.rstrip("/")
        self._token = None
        self._token_exp = 0

    # -- bas niveau ---------------------------------------------------------
    def token(self):
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = requests.post(
            f"{self.endpoint}/oauth2/token",
            data={"grant_type": "client_credentials", "client_id": self.client_id,
                  "client_secret": self.client_secret},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            raise PDPError(f"Authentification SUPER PDP refusée (HTTP {r.status_code}) : {r.text[:300]}")
        body = r.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 3600))
        return self._token

    def _req(self, method, path, auth=True, expect_json=True, **kw):
        headers = kw.pop("headers", {})
        if auth:
            headers["Authorization"] = f"Bearer {self.token()}"
        r = requests.request(method, f"{self.endpoint}{path}", headers=headers, timeout=TIMEOUT, **kw)
        if r.status_code >= 400:
            msg = r.text[:800]
            try:
                j = r.json()
                msg = j.get("message") or msg
                if j.get("errors"):
                    msg += " — " + "; ".join(str(e) for e in j["errors"])[:800]
            except ValueError:
                pass
            raise PDPError(f"SUPER PDP {method} {path} : HTTP {r.status_code} — {msg}")
        if not expect_json:
            return r.content
        if r.status_code == 204 or not r.content:
            return {}
        return r.json()

    # -- API ----------------------------------------------------------------
    def me(self):
        return self._req("GET", "/v1.beta/companies/me")

    def session(self):
        return self._req("GET", "/v1.beta/oauth2_sessions/me")

    def lookup_company(self, siren):
        return self._req("GET", "/v1.beta/french_directory/companies", auth=False,
                         params={"number": siren, "limit": 1})

    def directory_entries(self, siren):
        return self._req("GET", "/v1.beta/french_directory/entries", auth=False,
                         params={"number": siren})

    def to_facturx(self, en_invoice_json: str, pdf_bytes: bytes) -> bytes:
        files = {
            "invoice": ("invoice.json", en_invoice_json, "application/json"),
            "pdf": ("invoice.pdf", pdf_bytes, "application/pdf"),
        }
        return self._req("POST", "/v1.beta/invoices/convert", expect_json=False,
                         params={"from": "en16931", "to": "factur-x"}, files=files)

    def validate(self, facturx: bytes):
        files = {"file_name": ("invoice.pdf", facturx, "application/pdf")}
        return self._req("POST", "/v1.beta/validation_reports", files=files)

    def send(self, facturx: bytes, external_id: str):
        return self._req("POST", "/v1.beta/invoices", params={"external_id": external_id[:36]},
                         data=facturx, headers={"Content-Type": "application/pdf"})

    def events(self, invoice_id, starting_after_id=None):
        params = {"invoice_id": invoice_id, "limit": 100}
        if starting_after_id:
            params["starting_after_id"] = starting_after_id
        return self._req("GET", "/v1.beta/invoice_events", params=params)

    def post_event(self, payload):
        return self._req("POST", "/v1.beta/invoice_events", json=payload)

    def received(self, starting_after_id=0):
        params = [("direction", "in"), ("order", "asc"), ("limit", "100"),
                  ("expand[]", "en_invoice")]
        if starting_after_id:
            params.append(("starting_after_id", str(starting_after_id)))
        return self._req("GET", "/v1.beta/invoices", params=params)

    def download(self, invoice_id, fmt="original"):
        return self._req("GET", f"/v1.beta/invoices/{invoice_id}", expect_json=False,
                         params={"format": fmt})

    def generate_test_invoice(self, fmt="cii"):
        return self._req("GET", "/v1.beta/invoices/generate_test_invoice", expect_json=False,
                         params={"format": fmt})

    def convert_to_en(self, xml: bytes, fmt="cii"):
        return self._req("POST", "/v1.beta/invoices/convert", params={"from": fmt, "to": "en16931"},
                         data=xml, headers={"Content-Type": "application/xml"})

    def get_invoice(self, invoice_id):
        return self._req("GET", f"/v1.beta/invoices/{invoice_id}")

    def b2c_transactions(self, rows):
        return self._req("POST", "/v1.beta/b2c_transactions", json={"data": rows})

    def b2c_payments(self, rows):
        return self._req("POST", "/v1.beta/b2c_payments", json={"data": rows})


# -- Construction du modèle EN 16931 ------------------------------------------

UNIT_CODES = {"u": "C62", "h": "HUR", "heure": "HUR", "j": "DAY", "jour": "DAY",
              "forfait": "C62", "mois": "MON", "km": "KMT", "m2": "MTK", "m": "MTR"}


def _num(v):
    q = d(v).normalize()
    return f"{q:f}"


def _money(v):
    return f"{money(v):.2f}"


def _addr(p):
    a = {"country_code": (p.get("country") or "FR").upper()[:2]}
    if p.get("address"):
        parts = [x.strip() for x in p["address"].splitlines() if x.strip()]
        for i, part in enumerate(parts[:3]):
            a[f"address_line{i + 1}"] = part
    if p.get("post_code"):
        a["post_code"] = p["post_code"]
    if p.get("city"):
        a["city"] = p["city"]
    return a


def build_en_invoice(doc, lines, seller, client, extra=None):
    extra = extra or {}
    t = totals(lines)
    siren = (seller.get("siren") or (seller.get("siret") or "")[:9]).replace(" ", "")
    seller_json = {
        "name": seller.get("company_name", ""),
        "postal_address": _addr(seller),
        "electronic_address": {"scheme": seller.get("pdp_address_scheme") or "0225",
                               "value": seller.get("pdp_address_value") or siren},
        "legal_registration_identifier": {"scheme": "0002", "value": siren},
        "additional_legal_information": "Entrepreneur individuel (EI)",
    }
    if seller.get("company_trade_name"):
        seller_json["trading_name"] = seller["company_trade_name"]
    if seller.get("vat_number"):
        seller_json["vat_identifier"] = seller["vat_number"].replace(" ", "")
    if seller.get("email") or seller.get("phone"):
        seller_json["contact"] = {k: v for k, v in {
            "email_address": seller.get("email"), "phone_number": seller.get("phone")}.items() if v}

    buyer_json = {"name": client.get("name", ""), "postal_address": _addr(client)}
    c_siren = (client.get("siren") or "").replace(" ", "")[:9]
    if c_siren:
        buyer_json["legal_registration_identifier"] = {"scheme": "0002", "value": c_siren}
    if client.get("pdp_address_value"):
        buyer_json["electronic_address"] = {"scheme": client.get("pdp_address_scheme") or "0225",
                                            "value": client["pdp_address_value"]}
    elif c_siren:
        buyer_json["electronic_address"] = {"scheme": "0225", "value": c_siren}
    if client.get("vat_number"):
        buyer_json["vat_identifier"] = client["vat_number"].replace(" ", "")
    if client.get("email"):
        buyer_json["contact"] = {"email_address": client["email"]}

    en_lines = []
    for i, ln in enumerate(lines, 1):
        rate = d(ln["vat_rate"])
        item = {"name": ln["designation"][:200]}
        if ln.get("description"):
            item["description"] = ln["description"]
        en_lines.append({
            "identifier": str(i),
            "invoiced_quantity": _num(ln["quantity"]),
            "invoiced_quantity_code": UNIT_CODES.get((ln.get("unit") or "u").lower(), "C62"),
            "net_amount": _money(d(ln["quantity"]) * d(ln["unit_price"])),
            "item_information": item,
            "price_details": {"item_net_price": _num(ln["unit_price"])},
            "vat_information": {"invoiced_item_vat_category_code": "S" if rate else "E",
                                "invoiced_item_vat_rate": _num(rate)},
        })

    vat = []
    for rate, base, tva in vat_breakdown(lines):
        entry = {"vat_category_code": "S" if rate else "E",
                 "vat_category_rate": _num(rate),
                 "vat_category_taxable_amount": _money(base),
                 "vat_category_tax_amount": _money(tva)}
        if not rate:
            entry["vat_exemption_reason"] = "Exonération de TVA"
        vat.append(entry)

    paid = money(extra.get("paid", 0))
    totals_json = {
        "sum_invoice_lines_amount": _money(t["ht"]),
        "total_without_vat": _money(t["ht"]),
        "total_vat_amount": {"value": _money(t["tva"]), "currency_code": "EUR"},
        "total_with_vat": _money(t["ttc"]),
        "amount_due_for_payment": _money(t["ttc"] - paid),
    }
    if paid:
        totals_json["paid_amount"] = _money(paid)

    notes = [
        {"note": seller.get("late_penalty") or "Pénalités de retard : 3 fois le taux d'intérêt légal",
         "subject_code": "PMD"},
        {"note": seller.get("recovery_fee") or "Indemnité forfaitaire pour frais de recouvrement : 40 €",
         "subject_code": "PMT"},
        {"note": seller.get("discount_terms") or "Pas d'escompte pour paiement anticipé",
         "subject_code": "AAB"},
    ]
    if doc.get("notes"):
        notes.append({"note": doc["notes"][:1000]})

    is_avoir = doc["kind"] == "avoir"
    already_paid = paid and paid >= t["ttc"]
    en = {
        "number": doc["number"],
        "issue_date": doc["issue_date"],
        "type_code": 381 if is_avoir else 380,
        "currency_code": "EUR",
        "process_control": {"specification_identifier": "urn:cen.eu:en16931:2017",
                            "business_process_type": "S2" if already_paid else "S1"},
        "seller": seller_json,
        "buyer": buyer_json,
        "lines": en_lines,
        "vat_break_down": vat,
        "totals": totals_json,
        "notes": notes,
    }
    if doc.get("due_date") and not is_avoir:
        en["payment_due_date"] = doc["due_date"]
    if seller.get("iban") and not is_avoir:
        ct = {"payment_account_identifier": {"value": seller["iban"].replace(" ", ""), "scheme": ""}}
        if seller.get("bic"):
            ct["payment_service_provider_identifier"] = seller["bic"].replace(" ", "")
        ct["payment_account_name"] = seller.get("company_name", "")
        en["payment_instructions"] = {"payment_means_type_code": "58",
                                      "remittance_information": doc["number"],
                                      "credit_transfers": [ct]}
    if is_avoir and extra.get("avoir_of_number"):
        ref = {"reference": extra["avoir_of_number"], "preceding_invoice_type_code": 380}
        if extra.get("avoir_of_date"):
            ref["issue_date"] = extra["avoir_of_date"]
        en["preceding_invoice_references"] = [ref]
    return en


def payment_event(pdp_invoice_id, pay_date, amount, breakdown):
    """Statut « Encaissée » (fr:212) avec montants encaissés par taux (TTC)."""
    from calc import split_payment
    reported = []
    for rate, base, tva in split_payment(amount, breakdown):
        reported.append({"amount": _money(base + tva), "currency_code": "EUR",
                         "type_code": "MEN", "value_percent": f"{d(rate):.1f}", "date": pay_date})
    return {"invoice_id": int(pdp_invoice_id), "status_code": "fr:212",
            "details": [{"reported_data": reported}]}


def b2c_transaction(doc, lines):
    t = totals(lines)
    return {
        "category_code": "TPS1", "role_code": "SE", "date": doc["issue_date"], "currency": "EUR",
        "tax_exclusive_amount": _money(t["ht"]), "tax_total": _money(t["tva"]),
        "tax_subtotals": [{"tax_percent": _num(r), "taxable_amount": _money(b), "tax_total": _money(v)}
                          for r, b, v in t["breakdown"]],
    }


def b2c_payment(pay_date, amount, breakdown):
    from calc import split_payment
    return {"date": pay_date, "subtotals": [
        {"amount": _money(b + v), "tax_percent": _num(r)} for r, b, v in split_payment(amount, breakdown)]}


def parse_received(item):
    """Transforme une facture reçue (expand en_invoice) en achat."""
    en = item.get("en_invoice") or {}
    seller = en.get("seller") or {}
    tot = en.get("totals") or {}
    lri = seller.get("legal_registration_identifier") or {}
    desc = ", ".join((ln.get("item_information") or {}).get("name", "") for ln in (en.get("lines") or [])[:3])
    ht = d(tot.get("total_without_vat"))
    tva_raw = tot.get("total_vat_amount")
    tva = d(tva_raw.get("value") if isinstance(tva_raw, dict) else tva_raw)
    ttc = d(tot.get("total_with_vat")) or ht + tva
    return {
        "supplier": seller.get("name") or "Fournisseur",
        "supplier_siren": lri.get("value"),
        "invoice_number": en.get("number"),
        "invoice_date": en.get("issue_date"),
        "description": desc[:300],
        "total_ht": float(ht),
        "total_tva": float(tva),
        "total_ttc": float(ttc),
        "type_code": en.get("type_code"),
    }
