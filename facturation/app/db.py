"""Base SQLite : schéma, connexion, réglages."""
import json
import os
import sqlite3
from contextlib import contextmanager

DATA_DIR = os.environ.get("FACTU_DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "facturation.db")
ARCHIVE_DIR = os.path.join(DATA_DIR, "archives")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'pro',          -- 'pro' ou 'particulier'
    name TEXT NOT NULL,
    siren TEXT,
    vat_number TEXT,
    address TEXT,
    post_code TEXT,
    city TEXT,
    country TEXT NOT NULL DEFAULT 'FR',
    email TEXT,
    phone TEXT,
    pdp_address_scheme TEXT,                    -- adresse de facturation électronique (annuaire)
    pdp_address_value TEXT,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                         -- 'devis', 'facture', 'avoir'
    number TEXT UNIQUE,                         -- NULL tant que brouillon (factures/avoirs)
    status TEXT NOT NULL DEFAULT 'brouillon',
    client_id INTEGER REFERENCES clients(id),
    issue_date TEXT,
    due_date TEXT,
    validity_date TEXT,
    service_date TEXT,                          -- date / période de la prestation
    subject TEXT,
    notes TEXT,
    total_ht REAL DEFAULT 0,
    total_tva REAL DEFAULT 0,
    total_ttc REAL DEFAULT 0,
    source_devis_id INTEGER REFERENCES documents(id),
    avoir_of_id INTEGER REFERENCES documents(id),
    client_snapshot TEXT,                       -- JSON figé à l'émission
    seller_snapshot TEXT,                       -- JSON figé à l'émission
    pdf_path TEXT,
    pdp_id INTEGER,
    pdp_status TEXT,
    pdp_status_text TEXT,
    pdp_error TEXT,
    pdp_last_event_id INTEGER,
    ereport_tx_sent INTEGER DEFAULT 0,
    imported INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    validated_at TEXT
);

CREATE TABLE IF NOT EXISTS lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    designation TEXT NOT NULL,
    description TEXT,
    quantity REAL NOT NULL DEFAULT 1,
    unit TEXT DEFAULT 'u',
    unit_price REAL NOT NULL DEFAULT 0,
    vat_rate REAL NOT NULL DEFAULT 20
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    amount REAL NOT NULL,                       -- TTC encaissé (positif) ; remboursement d'avoir : positif sur l'avoir
    method TEXT,
    pdp_reported INTEGER DEFAULT 0,
    pdp_error TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier TEXT NOT NULL,
    supplier_siren TEXT,
    invoice_number TEXT,
    invoice_date TEXT,
    description TEXT,
    total_ht REAL DEFAULT 0,
    total_tva REAL DEFAULT 0,
    total_ttc REAL DEFAULT 0,
    category TEXT NOT NULL DEFAULT 'biens_services',  -- ou 'immobilisation'
    deduction_date TEXT,                         -- mois où la TVA est déduite
    paid_date TEXT,
    source TEXT DEFAULT 'manuel',                -- 'manuel' ou 'pdp'
    pdp_id INTEGER UNIQUE,
    file_path TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS declarations (
    period TEXT PRIMARY KEY,                     -- 'YYYY-MM'
    declared_at TEXT,
    tva_nette REAL,
    credit_reporte REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT DEFAULT CURRENT_TIMESTAMP,
    level TEXT,
    message TEXT
);
"""

DEFAULT_SETTINGS = {
    "company_name": "",
    "company_trade_name": "",
    "address": "",
    "post_code": "",
    "city": "",
    "siren": "",
    "siret": "",
    "vat_number": "",
    "ape": "",
    "email": "",
    "phone": "",
    "iban": "",
    "bic": "",
    "rcs_rm": "",                     # ex. « Dispensé d'immatriculation au RCS et au RM »
    "insurance": "",                  # assurance pro si obligatoire
    "invoice_pattern": "F-{AAAA}-{NNNN}",
    "avoir_pattern": "AV-{AAAA}-{NNNN}",
    "devis_pattern": "D-{AAAA}-{NNN}",
    "payment_days": "30",
    "devis_validity_days": "30",
    "default_vat_rate": "20",
    "late_penalty": "Pénalités de retard : 3 fois le taux d'intérêt légal",
    "recovery_fee": "Indemnité forfaitaire pour frais de recouvrement en cas de retard de paiement : 40 €",
    "discount_terms": "Pas d'escompte pour paiement anticipé",
    "footer_note": "",
    "ca3_due_day": "19",
    "pdp_client_id": "",
    "pdp_client_secret": "",
    "pdp_endpoint": "https://api.superpdp.tech",
    "pdp_auto_send": "1",
    "pdp_address_scheme": "",
    "pdp_address_value": "",
    "ereporting_start": "2027-09-01",
    "last_inbox_id": "0",
}


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with transaction() as conn:
        conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))


def get_settings(conn=None):
    own = conn is None
    conn = conn or connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        s = dict(DEFAULT_SETTINGS)
        s.update({r["key"]: r["value"] for r in rows})
        return s
    finally:
        if own:
            conn.close()


def set_settings(values: dict, conn=None):
    own = conn is None
    conn = conn or connect()
    try:
        for k, v in values.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, "" if v is None else str(v)),
            )
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def log(conn, level, message):
    conn.execute("INSERT INTO journal(level, message) VALUES (?, ?)", (level, message[:2000]))


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False)


def loads(text, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default
