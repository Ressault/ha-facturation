# Facturation

Devis, factures, avoirs et TVA mensuelle pour micro-entrepreneur assujetti à la TVA
(prestations de services, TVA sur les encaissements), avec facturation électronique
via la plateforme agréée **SUPER PDP**.

## Premiers pas

1. Ouvrez **Facturation** dans la barre latérale.
2. **Réglages** : renseignez votre entreprise (nom, adresse, SIRET, n° de TVA, IBAN).
3. **Réglages → Importer l'historique** : déposez les exports CSV de vos clients et
   factures MonAE. Indiquez ensuite le prochain numéro de facture.
4. **Plateforme agréée** : créez un compte sur superpdp.tech, puis une *application*
   (menu Applications) ; recopiez son *client ID* et son *client secret* dans les réglages,
   puis cliquez sur **Tester la connexion**.

## Au quotidien

- **Devis → facture** en un clic. À l'émission, la facture reçoit son numéro définitif,
  est archivée (PDF) et transmise au client professionnel via la plateforme agréée.
- **Marquer payée** : la date d'encaissement détermine le mois de TVA, et le statut
  « Encaissée » est transmis automatiquement à la plateforme.
- **Achats** : les factures de vos fournisseurs arrivent toutes seules ; marquez-les
  payées pour que leur TVA soit déduite.
- **TVA** : un bandeau apparaît chaque mois tant que la TVA du mois précédent n'est pas
  déclarée. La page TVA donne les montants ligne par ligne (A1, 08, 16, 19, 20, 23, 28…)
  à recopier sur impots.gouv.fr, ainsi que le chiffre d'affaires à déclarer à l'URSSAF.

## Données et sauvegardes

Tout est stocké dans le dossier `/data` de l'add-on (base SQLite et PDF archivés),
inclus dans les sauvegardes Home Assistant. Les factures doivent être conservées
10 ans : gardez des sauvegardes régulières hors du Raspberry Pi.

L'accès se fait uniquement via Home Assistant (Ingress), donc avec votre compte HA.
