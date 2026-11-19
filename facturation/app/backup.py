"""Sauvegarde et restauration complètes : base de données + PDF archivés + logo + factures reçues.

- snapshot() : copie cohérente de la base (API de sauvegarde SQLite) dans /data/sauvegardes, 14 dernières gardées.
  Appelée avant chaque sauvegarde Home Assistant (backup_pre) et chaque nuit.
- full_zip() : un seul fichier ZIP avec tout ce qu'il faut pour tout récupérer.
- restore_zip() : remet en place un ZIP produit par full_zip() (une copie de l'état actuel est gardée avant).
"""
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime

import db

SKIP_DIRS = {"sauvegardes", "tmp"}
KEEP = 14


def backup_dir():
    d = os.path.join(db.DATA_DIR, "sauvegardes")
    os.makedirs(d, exist_ok=True)
    return d


def copy_db(dest):
    """Copie cohérente de la base, même si l'app écrit au même moment."""
    src = sqlite3.connect(db.DB_PATH)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dest


def snapshot(label=None):
    if not os.path.exists(db.DB_PATH):
        return None
    d = backup_dir()
    name = f"facturation-{label or datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    path = copy_db(os.path.join(d, name))
    shutil.copyfile(path, os.path.join(d, "facturation-derniere.db"))
    dated = sorted(f for f in os.listdir(d) if f.startswith("facturation-2") and f.endswith(".db"))
    for old in dated[:-KEEP]:
        try:
            os.remove(os.path.join(d, old))
        except OSError:
            pass
    return path


def list_snapshots():
    d = backup_dir()
    out = []
    for f in sorted(os.listdir(d), reverse=True):
        if f.startswith("facturation-2") and f.endswith(".db"):
            p = os.path.join(d, f)
            out.append({"name": f, "size": os.path.getsize(p), "date": datetime.fromtimestamp(os.path.getmtime(p))})
    return out


def full_zip():
    """Chemin d'un ZIP temporaire contenant toute l'app : base cohérente + fichiers du dossier de données."""
    tmp = tempfile.NamedTemporaryFile(prefix="facturation-", suffix=".zip", delete=False)
    tmp.close()
    with tempfile.TemporaryDirectory() as work:
        dbcopy = copy_db(os.path.join(work, "facturation.db"))
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(dbcopy, "facturation.db")
            for root, dirs, files in os.walk(db.DATA_DIR):
                rel_root = os.path.relpath(root, db.DATA_DIR)
                if rel_root == ".":
                    dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
                for f in files:
                    rel = os.path.normpath(os.path.join(rel_root, f))
                    if rel.startswith("facturation.db") or rel.endswith((".db-wal", ".db-shm", ".db-journal")):
                        continue
                    z.write(os.path.join(root, f), rel)
            z.writestr("LISEZMOI.txt", (
                "Sauvegarde complète de l'app Facturation (Home Assistant).\n"
                f"Créée le {datetime.now():%d/%m/%Y à %H:%M}.\n\n"
                "Contenu : facturation.db (clients, factures, devis, avoirs, règlements, dépenses, réglages),\n"
                "archives/ (PDF des factures, avoirs, devis et factures fournisseurs), logo.\n\n"
                "Pour tout récupérer : installez l'app Facturation, puis Export → Restaurer une sauvegarde.\n"
                "Les PDF des factures s'ouvrent aussi directement, sans l'app, dans le dossier archives/.\n"))
    return tmp.name


def restore_zip(fileobj):
    """Restaure un ZIP de full_zip(). Renvoie le chemin de la copie de sécurité de l'état précédent."""
    data = fileobj.read()
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("Ce fichier n'est pas une sauvegarde ZIP.")
    names = z.namelist()
    if "facturation.db" not in names:
        raise ValueError("Ce ZIP ne contient pas de base facturation.db : ce n'est pas une sauvegarde de l'app.")
    for n in names:
        if n.startswith("/") or ".." in n.split("/"):
            raise ValueError("Sauvegarde invalide (chemin de fichier suspect).")
    with tempfile.TemporaryDirectory() as work:
        z.extractall(work)
        test = sqlite3.connect(os.path.join(work, "facturation.db"))
        try:
            ok = test.execute("PRAGMA integrity_check").fetchone()[0]
            test.execute("SELECT COUNT(*) FROM documents").fetchone()
        except sqlite3.DatabaseError:
            ok = "illisible"
        finally:
            test.close()
        if ok != "ok":
            raise ValueError(f"La base contenue dans la sauvegarde est abîmée ({ok}).")
        safety = snapshot("avant-restauration-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        # base : copie page à page dans la base en service
        src = sqlite3.connect(os.path.join(work, "facturation.db"))
        dst = sqlite3.connect(db.DB_PATH)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        # fichiers (PDF, logo…)
        for root, _, files in os.walk(work):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), work)
                if rel in ("facturation.db", "LISEZMOI.txt"):
                    continue
                dest = os.path.join(db.DATA_DIR, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copyfile(os.path.join(root, f), dest)
    db.init_db()
    return safety


if __name__ == "__main__":
    # appelé par Home Assistant juste avant une sauvegarde (backup_pre dans config.yaml)
    if len(sys.argv) > 1 and sys.argv[1] == "snapshot":
        print(snapshot() or "aucune base")
