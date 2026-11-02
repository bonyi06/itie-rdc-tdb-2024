# Tableau de bord ITIE-RDC — Suivi des télédéclarations 2024 (version live)

Refonte du tableau de bord de suivi des télédéclarations de l'Initiative pour la
Transparence des Industries Extractives en République démocratique du Congo
(ITIE-RDC). Cette version récupère les données en temps réel depuis la
plateforme T/SL de l'ITIE-RDC.

## Architecture

- **Flask** (Python 3.11) sert la page.
- À chaque requête, le serveur consulte son cache ; si celui-ci a plus de 5
  minutes, il scrape les deux pages sources :
  - `https://www.tsl-itierdc.org/tableau_de_bord/index.php`
  - `https://www.tsl-itierdc.org/tableau_de_bord_2024.php`
- Les données parsées sont agrégées et injectées dans le template Jinja.
- Le taux de change CDF/USD retenu pour la campagne 2024 est **2 793,46**
  (constante `FX_RATE_CDF` dans `app.py`).

## Déploiement — Render

Service de type **Web Service**, runtime **Python**, plan **free**. Les
commandes de build et de démarrage sont dans `render.yaml`.

## Développement local

```bash
pip install -r requirements.txt
python app.py
# http://localhost:5000
```

## Fichiers

- `app.py` — serveur Flask + scraper
- `templates/index.html` — template de la page
- `requirements.txt` — dépendances Python
- `render.yaml` — configuration Render

## Licence

Produit institutionnel du Secrétariat technique de l'ITIE-RDC.
