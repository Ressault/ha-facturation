"""Import de devis (ou factures) à partir de PDF MonAE / facturation.pro.

Le texte est extrait avec pypdf, puis analysé : numéro, date, client, objet, lignes, totaux.
Le résultat est toujours présenté pour vérification avant d'être enregistré.
"""
import io
import re

from pypdf import PdfReader


def pdf_text(data, mode="layout"):
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for p in reader.pages:
        try:
            pages.append(p.extract_text(extraction_mode=mode) if mode == "layout" else p.extract_text())
        except Exception:  # noqa: BLE001
            pages.append(p.extract_text() or "")
    return "\n\f\n".join(pages)


def parse_quote(data):
    return {"text": pdf_text(data, "plain")}
