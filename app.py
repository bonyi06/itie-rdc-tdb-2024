"""
Tableau de bord ITIE-RDC — Suivi des télédéclarations 2024 (version live).

Scrape les deux pages du T/SL de l'ITIE-RDC toutes les 5 minutes et affiche
le tableau de bord avec les chiffres frais.

Déploiement : Render Web Service (plan free, Python 3.11).
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from typing import Any

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tdb-itie")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SRC_INDEX = "https://www.tsl-itierdc.org/tableau_de_bord/index.php"
SRC_2024  = "https://www.tsl-itierdc.org/tableau_de_bord_2024.php"
FX_RATE_CDF = 2793.46
CACHE_TTL = 300  # 5 minutes
USER_AGENT = "ITIE-RDC-TDB/1.0 (+https://www.itierdc.net)"
TIMEOUT = 20

_cache: dict[str, Any] = {"data": None, "ts": 0.0}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_number(s: str) -> float:
    """Convertit « 1 018 813 842,45 » → 1018813842.45."""
    s = re.sub(r"[\s ]", "", s or "").replace(",", ".")
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def parse_int(s: str) -> int:
    return int(parse_number(s))


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------
def fetch(url: str) -> str:
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def scrape_index() -> dict[str, list[dict[str, Any]]]:
    """Parse les 3 tables de /tableau_de_bord/index.php."""
    html = fetch(SRC_INDEX)
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    out: dict[str, list[dict[str, Any]]] = {"perimetres": [], "entreprises": [], "regies": []}

    # Table 1 : périmètres (3 cols)
    if len(tables) >= 1:
        for row in tables[0].find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            peri  = cells[0].get_text(strip=True)
            pct   = cells[1].get_text(strip=True)
            ratio = cells[2].get_text(strip=True)
            if not peri and not pct and not ratio:
                continue
            pct_val = 0
            m = re.search(r"(\d+)", pct)
            if m:
                pct_val = int(m.group(1))
            out["perimetres"].append({"name": peri or "Non classé", "pct": pct_val, "ratio": ratio})

    # Table 2 : entreprises (4 cols : No, NIF, Structure, Lignes)
    if len(tables) >= 2:
        for row in tables[1].find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            out["entreprises"].append({
                "no": parse_int(cells[0].get_text(strip=True)),
                "nif": cells[1].get_text(strip=True),
                "structure": cells[2].get_text(strip=True),
                "lignes": parse_int(cells[3].get_text(strip=True)),
            })

    # Table 3 : régies / EP (6 cols : No, NIF, Structure, Lignes, Total USD, Total CDF)
    if len(tables) >= 3:
        for row in tables[2].find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) < 6:
                continue
            out["regies"].append({
                "no": parse_int(cells[0].get_text(strip=True)),
                "nif": cells[1].get_text(strip=True),
                "structure": cells[2].get_text(strip=True),
                "lignes": parse_int(cells[3].get_text(strip=True)),
                "usd": parse_number(cells[4].get_text(strip=True)),
                "cdf": parse_number(cells[5].get_text(strip=True)),
            })

    return out


def scrape_2024() -> list[dict[str, Any]]:
    """Parse la matrice entreprises × régies de /tableau_de_bord_2024.php."""
    html = fetch(SRC_2024)
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if not table:
        return []
    entities: list[dict[str, Any]] = []
    tbody = table.find("tbody") or table
    date_re = re.compile(r"\d{1,2}/\d{1,2}")
    for tr in tbody.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue
        no = cells[0].get_text(strip=True)
        nif = cells[1].get_text(strip=True)
        name = cells[2].get_text(strip=True)
        if not name:
            continue
        touched = 0
        last_date = "—"
        for c in cells[3:]:
            txt = c.get_text(strip=True)
            if date_re.match(txt):
                touched += 1
                last_date = txt
        entities.append({
            "no": no, "nif": nif, "n": name,
            "r": touched, "l": 0, "last": last_date,
            "st": "crit" if touched == 0 else ("ok" if touched >= 7 else "warn"),
        })
    return entities


# ---------------------------------------------------------------------------
# Agrégation + cache
# ---------------------------------------------------------------------------
def aggregate() -> dict[str, Any]:
    log.info("Scraping sources ITIE-RDC…")
    try:
        idx = scrape_index()
    except Exception as e:
        log.error("Échec scraping index.php : %s", e)
        if _cache["data"] is not None:
            return _cache["data"]
        raise

    try:
        matrix = scrape_2024()
    except Exception as e:
        log.warning("Matrice 2024 indisponible, on continue sans : %s", e)
        matrix = []

    # Enrichit chaque entreprise du périmètre réconciliation avec le nb lignes
    # pris dans la table « entreprises » de index.php (clé : NIF+nom).
    lines_by_name = {}
    for e in idx["entreprises"]:
        key = e["structure"].strip().upper()
        lines_by_name[key] = e["lignes"]
    for e in matrix:
        key = e["n"].strip().upper()
        e["l"] = lines_by_name.get(key, 0)

    # Agrégats périmètres
    peri = {"recon": {"pct": 0, "num": 0, "den": 0},
            "unilat": {"pct": 0, "num": 0, "den": 0},
            "total": {"pct": 0, "num": 0, "den": 0}}
    for p in idx["perimetres"]:
        key = p["name"].lower()
        parts = re.sub(r"\s", "", p["ratio"]).split("/")
        num = int(parts[0]) if len(parts) > 0 and parts[0].isdigit() else 0
        den = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if "concil" in key:
            peri["recon"] = {"pct": p["pct"], "num": num, "den": den}
        elif "unilat" in key:
            peri["unilat"] = {"pct": p["pct"], "num": num, "den": den}
        elif key == "total":
            peri["total"] = {"pct": p["pct"], "num": num, "den": den}

    # Régies : équivalent USD + tri décroissant
    for r in idx["regies"]:
        r["usd_eq"] = r["usd"] + r["cdf"] / FX_RATE_CDF
    regies_sorted = sorted(idx["regies"], key=lambda x: -x["usd_eq"])

    sum_usd = sum(r["usd"] for r in idx["regies"])
    sum_cdf = sum(r["cdf"] for r in idx["regies"])
    sum_usd_eq = sum_usd + sum_cdf / FX_RATE_CDF
    sum_lignes = sum(r["lignes"] for r in idx["regies"])

    return {
        "perimetres": peri,
        "regies_top": regies_sorted[:15],
        "regies_all": regies_sorted,
        "entreprises": idx["entreprises"],
        "matrix": matrix,
        "sum_usd": sum_usd,
        "sum_cdf": sum_cdf,
        "sum_usd_eq": sum_usd_eq,
        "sum_lignes": sum_lignes,
        "nb_regies": len(idx["regies"]),
        "nb_entreprises": len(idx["entreprises"]),
        "nb_enrolled": max(peri["total"]["den"], len(idx["entreprises"])),
        "nb_declar": peri["total"]["num"],
        "fx": FX_RATE_CDF,
        "scraped_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }


def get_data() -> dict[str, Any]:
    now = time.time()
    if _cache["data"] is None or (now - _cache["ts"]) > CACHE_TTL:
        try:
            _cache["data"] = aggregate()
            _cache["ts"] = now
            log.info("Cache rafraîchi.")
        except Exception as e:
            log.error("Rafraîchissement échoué : %s", e)
            if _cache["data"] is None:
                raise
    return _cache["data"]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    data = get_data()
    return render_template("index.html", data=data)


@app.route("/refresh")
def refresh():
    """Force la mise à jour du cache (passer ?token=XXX en prod si besoin)."""
    _cache["data"] = None
    _cache["ts"] = 0
    get_data()
    return "Cache rafraîchi — <a href='/'>retour</a>"


@app.route("/healthz")
def healthz():
    return "OK", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
