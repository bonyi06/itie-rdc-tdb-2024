# Tableau de bord ITIE-RDC — Suivi des télédéclarations 2024

Refonte du tableau de bord de suivi des télédéclarations de l'Initiative pour la
Transparence des Industries Extractives en République démocratique du Congo
(ITIE-RDC). Cette page présente l'avancement des déclarations des 66 entreprises
extractives du périmètre de réconciliation et des 179 structures (régies
financières et entités perceptrices) qui perçoivent les flux issus des mines et
hydrocarbures sur la plateforme T/SL de l'ITIE-RDC.

## Contenu

- `index.html` — tableau de bord autonome (HTML, CSS, JavaScript). Aucun
  back-end requis : la page tourne entièrement côté navigateur.

## Hébergement

Prévu pour un hébergement statique (Render, Netlify, Vercel, Cloudflare Pages,
GitHub Pages). Aucun processus serveur, aucune base de données.

## Mise à jour des données

Les chiffres de la campagne sont saisis dans le bloc `<script>` au bas de
`index.html`, dans les tableaux `REGIES` (régies perceptrices) et `ENT`
(entreprises déclarantes), ainsi que dans les constantes de synthèse
(`enrolled`, `periReconT`, `periReconD`, etc.).

Taux de change CDF/USD retenu pour la campagne 2024 : **2 793,46**
(constante `FX` dans le script).

## Charte graphique

Alignée sur `itierdc.net` :

- Bleu institutionnel : `#0068B4`
- Bleu foncé : `#004F89`
- Jaune or : `#FBB800`
- Polices : Montserrat (titres) + Open Sans (corps)
- Chart.js (CDN cdnjs) pour les graphiques

## Licence

Produit institutionnel du Secrétariat technique de l'ITIE-RDC.
