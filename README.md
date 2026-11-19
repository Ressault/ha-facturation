# Facturation — add-on Home Assistant

Application de devis et factures pour micro-entrepreneur assujetti à la TVA, hébergée
dans Home Assistant (Ingress) et connectée à la plateforme agréée SUPER PDP
(facturation électronique, e-reporting des encaissements).

## Installation

Paramètres → Modules complémentaires → Boutique → ⋮ → Dépôts, ajoutez l'URL de ce dépôt,
puis installez **Facturation**.

## Développement

```
pip install -r facturation/requirements.txt pytest
pytest tests
```
