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
import threading
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
SRC_ETATS = "https://www.tsl-itierdc.org/etats/"
FX_RATE_CDF = 2793.46
CACHE_TTL = 300             # 5 minutes pour les tableaux de bord
CACHE_TTL_DETAILS = 1800    # 30 minutes pour les flux détaillés (coûteux à scraper)
USER_AGENT = "Mozilla/5.0 (compatible; ITIE-RDC-TDB/1.0; +https://www.itierdc.net)"
TIMEOUT = 120         # Le serveur T/SL met 40-60 s à répondre, on laisse large
MAX_RETRIES = 1       # 1 tentative + 1 retry

_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_scrape_lock = threading.Lock()

# Cache séparé pour les données détaillées, clé = (year, reportselect, perim)
_details_cache: dict[tuple, dict] = {}
_details_lock = threading.Lock()


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
    """GET avec retries exponentiels. Les pages T/SL sont lentes à générer."""
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = requests.get(
                url,
                timeout=(10, TIMEOUT),  # (connect, read)
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                },
            )
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:
            last_err = e
            log.warning("Tentative %d/%d a échoué pour %s : %s",
                        attempt + 1, MAX_RETRIES + 1, url, e)
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)  # 1 s, 2 s
    assert last_err is not None
    raise last_err


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


def _fallback_data() -> dict[str, Any]:
    """Données de secours affichées quand les sources ITIE sont injoignables.
    On montre que le site est bien en ligne mais que la source est muette."""
    return {
        "perimetres": {"recon": {"pct": 0, "num": 0, "den": 0},
                       "unilat": {"pct": 0, "num": 0, "den": 0},
                       "total": {"pct": 0, "num": 0, "den": 0}},
        "regies_top": [],
        "regies_all": [],
        "entreprises": [],
        "matrix": [],
        "sum_usd": 0, "sum_cdf": 0, "sum_usd_eq": 0, "sum_lignes": 0,
        "nb_regies": 0, "nb_entreprises": 0,
        "nb_enrolled": 0, "nb_declar": 0,
        "fx": FX_RATE_CDF,
        "scraped_at": datetime.now().strftime("%d/%m/%Y %H:%M")
                      + " — données ITIE indisponibles, nouvelle tentative dans 5 min",
    }


def get_data() -> dict[str, Any]:
    now = time.time()
    # Cache chaud → on sert immédiatement
    if _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]

    # Cache à rafraîchir — un seul thread fait le scraping à la fois
    with _scrape_lock:
        now = time.time()
        # Un autre thread a pu rafraîchir pendant qu'on attendait le lock
        if _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL:
            return _cache["data"]
        try:
            fresh = aggregate()
            _cache["data"] = fresh
            _cache["ts"] = now
            log.info("Cache rafraîchi.")
            return fresh
        except Exception as e:
            log.error("Rafraîchissement échoué : %s", e)
            if _cache["data"] is None:
                _cache["data"] = _fallback_data()
                _cache["ts"] = now - CACHE_TTL + 10  # réessaie dans 10 s
            return _cache["data"]


def _warmup_in_background() -> None:
    """Lance un premier scraping dès le démarrage du worker, pour que
    le premier visiteur n'attende pas 50 s. Tourne dans un thread."""
    def _run():
        time.sleep(2)  # laisse Gunicorn finir de démarrer
        log.info("Pré-chargement en arrière-plan…")
        try:
            get_data()
            log.info("Pré-chargement terminé.")
        except Exception as e:
            log.warning("Pré-chargement échoué : %s", e)
    threading.Thread(target=_run, daemon=True, name="warmup").start()


# Démarre le pré-chargement dès l'import du module (= au boot Gunicorn)
_warmup_in_background()


# ---------------------------------------------------------------------------
# Scraping /etats/ — données détaillées (flux, par entreprise, dépenses sociales)
# ---------------------------------------------------------------------------
REPORT_LABELS = {
    "decla-flux-global":  "Reporting par flux",
    "decla-par-ese":      "Reporting par entreprise",
    "decla-pso":          "Dépenses sociales par secteur et entreprise",
}
PERIMETRE_LABELS = {"": "Tous", "R": "Réconciliation", "U": "Unilatéral", "A": "Non classé"}


def fetch_etats(year: int, reportselect: str, perimetre: str = "", company: str = "") -> str:
    """POST sur /etats/ — endpoint AJAX qui renvoie l'HTML du tableau détaillé."""
    data = {
        "origine": "publique",
        "reportselect": reportselect,
        "year": str(year),
        "perimetreList": perimetre,
    }
    if company:
        data["company"] = company
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = requests.post(
                SRC_ETATS,
                data=data,
                timeout=(10, TIMEOUT),
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": "https://www.tsl-itierdc.org/donnees-itie/",
                },
            )
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:
            last_err = e
            log.warning("Tentative %d/%d /etats/ (%s %s) a échoué : %s",
                        attempt + 1, MAX_RETRIES + 1, reportselect, year, e)
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
    assert last_err is not None
    raise last_err


def parse_etats_flux(html: str) -> dict[str, Any]:
    """Parse le tableau 'Reporting par flux' retourné par /etats/."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if not table:
        return {"regies": [], "flux": [], "totals": {}}

    regies: list[dict[str, Any]] = []
    flux: list[dict[str, Any]] = []
    current_regie: str | None = None

    tbody = table.find("tbody") or table
    for tr in tbody.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        texts = [c.get_text(strip=True) for c in cells]
        # Ligne entête de régie : premier td sans numéro, structure "NOM (CODE)"
        is_header = tr.get("class") and any("header" in c.lower() or "regie" in c.lower() or "group" in c.lower() for c in tr.get("class"))
        first = texts[0]
        is_num_row = first.isdigit()
        if not is_num_row and len(cells) >= 10:
            # C'est probablement une ligne de régie (groupement)
            current_regie = first
            try:
                regies.append({
                    "name": current_regie,
                    "soc_decl":  parse_number(texts[1]),
                    "etat_decl": parse_number(texts[2]),
                    "diff_decl": parse_number(texts[3]),
                    "soc_adj":   parse_number(texts[4]),
                    "etat_adj":  parse_number(texts[5]),
                    "diff_adj":  parse_number(texts[6]),
                    "soc_fin":   parse_number(texts[7]),
                    "etat_fin":  parse_number(texts[8]),
                    "diff_fin":  parse_number(texts[9]),
                })
            except (IndexError, ValueError):
                pass
        elif is_num_row and len(cells) >= 11:
            # Ligne de flux individuel
            try:
                flux.append({
                    "no": int(first),
                    "regie": current_regie or "—",
                    "flux": texts[1],
                    "soc_decl":  parse_number(texts[2]),
                    "etat_decl": parse_number(texts[3]),
                    "diff_decl": parse_number(texts[4]),
                    "soc_adj":   parse_number(texts[5]),
                    "etat_adj":  parse_number(texts[6]),
                    "diff_adj":  parse_number(texts[7]),
                    "soc_fin":   parse_number(texts[8]),
                    "etat_fin":  parse_number(texts[9]),
                    "diff_fin":  parse_number(texts[10]),
                })
            except (IndexError, ValueError):
                pass

    totals = {
        "nb_regies": len(regies),
        "nb_flux": len(flux),
        "sum_soc_decl":  sum(f["soc_decl"] for f in flux),
        "sum_etat_decl": sum(f["etat_decl"] for f in flux),
        "sum_soc_fin":   sum(f["soc_fin"] for f in flux),
        "sum_etat_fin":  sum(f["etat_fin"] for f in flux),
        "sum_diff_fin":  sum(f["diff_fin"] for f in flux),
    }
    return {"regies": regies, "flux": flux, "totals": totals}


def get_details(year: int, reportselect: str, perimetre: str = "") -> dict[str, Any]:
    """Récupère les données détaillées avec cache long + verrou."""
    key = (year, reportselect, perimetre)
    now = time.time()
    cached = _details_cache.get(key)
    if cached and (now - cached["ts"]) < CACHE_TTL_DETAILS:
        return cached["data"]
    with _details_lock:
        cached = _details_cache.get(key)
        if cached and (now - cached["ts"]) < CACHE_TTL_DETAILS:
            return cached["data"]
        try:
            html = fetch_etats(year, reportselect, perimetre)
            parsed = parse_etats_flux(html)
            parsed["year"] = year
            parsed["reportselect"] = reportselect
            parsed["report_label"] = REPORT_LABELS.get(reportselect, reportselect)
            parsed["perimetre"] = perimetre
            parsed["perimetre_label"] = PERIMETRE_LABELS.get(perimetre, perimetre)
            parsed["scraped_at"] = datetime.now().strftime("%d/%m/%Y %H:%M")
            _details_cache[key] = {"data": parsed, "ts": now}
            return parsed
        except Exception as e:
            log.error("Échec /etats/ (%s %s %s) : %s", year, reportselect, perimetre, e)
            if cached:
                return cached["data"]
            return {
                "regies": [], "flux": [], "totals": {},
                "year": year, "reportselect": reportselect,
                "report_label": REPORT_LABELS.get(reportselect, reportselect),
                "perimetre": perimetre,
                "perimetre_label": PERIMETRE_LABELS.get(perimetre, perimetre),
                "scraped_at": datetime.now().strftime("%d/%m/%Y %H:%M") + " — données ITIE indisponibles",
                "error": str(e),
            }


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


@app.route("/details")
def details():
    """Page détails des déclarations — filtres + table complète + export."""
    from flask import request
    year = request.args.get("year", "2023")
    try:
        year_int = int(year)
    except ValueError:
        year_int = 2023
    reportselect = request.args.get("report", "decla-flux-global")
    perimetre = request.args.get("perim", "")
    if reportselect not in REPORT_LABELS:
        reportselect = "decla-flux-global"
    if perimetre not in PERIMETRE_LABELS:
        perimetre = ""
    data = get_details(year_int, reportselect, perimetre)
    return render_template("details.html", data=data,
                           years=list(range(2015, 2025)),
                           report_labels=REPORT_LABELS,
                           perim_labels=PERIMETRE_LABELS)


@app.route("/api/details")
def api_details():
    """Endpoint JSON pour recharger la table en AJAX sans toute la page."""
    from flask import request, jsonify
    year = request.args.get("year", "2023")
    try:
        year_int = int(year)
    except ValueError:
        year_int = 2023
    reportselect = request.args.get("report", "decla-flux-global")
    perimetre = request.args.get("perim", "")
    data = get_details(year_int, reportselect, perimetre)
    return jsonify(data)


@app.route("/diag")
def diag():
    """
    Diagnostic réseau — teste la joignabilité du serveur T/SL depuis Render.
    Résultats attendus pour comprendre où ça bloque.
    """
    import socket
    import ssl
    from urllib.parse import urlparse

    out = ["<pre style='font-family: monospace; padding: 20px;'>"]
    out.append("DIAGNOSTIC — depuis les serveurs Render\n")
    out.append(f"Horodatage : {datetime.now().isoformat()}\n")
    out.append("=" * 70 + "\n")

    # Test 1 : résolution DNS
    out.append("\n[1] Résolution DNS de www.tsl-itierdc.org\n")
    try:
        ip = socket.gethostbyname("www.tsl-itierdc.org")
        out.append(f"    OK → IP résolue : {ip}\n")
    except Exception as e:
        out.append(f"    ÉCHEC → {type(e).__name__}: {e}\n")

    # Test 2 : connexion TCP port 443
    out.append("\n[2] Connexion TCP sur port 443 (handshake HTTPS)\n")
    try:
        t0 = time.time()
        with socket.create_connection(("www.tsl-itierdc.org", 443), timeout=15):
            dt = time.time() - t0
            out.append(f"    OK → connecté en {dt:.2f} s\n")
    except Exception as e:
        out.append(f"    ÉCHEC → {type(e).__name__}: {e}\n")

    # Test 3 : handshake TLS
    out.append("\n[3] Handshake TLS\n")
    try:
        t0 = time.time()
        ctx = ssl.create_default_context()
        with socket.create_connection(("www.tsl-itierdc.org", 443), timeout=15) as sock:
            with ctx.wrap_socket(sock, server_hostname="www.tsl-itierdc.org") as ssock:
                dt = time.time() - t0
                out.append(f"    OK → TLS établi en {dt:.2f} s, cipher={ssock.cipher()[0]}\n")
    except Exception as e:
        out.append(f"    ÉCHEC → {type(e).__name__}: {e}\n")

    # Test 4 : HEAD sur la racine du site (page légère)
    out.append("\n[4] HEAD https://www.tsl-itierdc.org/  (page d'accueil, légère)\n")
    try:
        t0 = time.time()
        r = requests.head(
            "https://www.tsl-itierdc.org/",
            timeout=30,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            allow_redirects=True,
        )
        dt = time.time() - t0
        out.append(f"    OK → HTTP {r.status_code} en {dt:.2f} s\n")
        out.append(f"    Headers reçus : Server={r.headers.get('Server', '?')}, "
                   f"Content-Length={r.headers.get('Content-Length', '?')}\n")
    except Exception as e:
        out.append(f"    ÉCHEC → {type(e).__name__}: {str(e)[:200]}\n")

    # Test 5 : GET sur la page du tableau de bord (page lourde)
    out.append("\n[5] GET https://www.tsl-itierdc.org/tableau_de_bord/index.php  (lourde)\n")
    try:
        t0 = time.time()
        r = requests.get(
            "https://www.tsl-itierdc.org/tableau_de_bord/index.php",
            timeout=90,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
        )
        dt = time.time() - t0
        out.append(f"    OK → HTTP {r.status_code} en {dt:.2f} s, "
                   f"{len(r.content)} octets\n")
    except Exception as e:
        out.append(f"    ÉCHEC → {type(e).__name__}: {str(e)[:200]}\n")

    # Test 6 : GET sur un site connu (contrôle que le réseau sortant marche)
    out.append("\n[6] GET https://www.google.com/  (contrôle : réseau sortant Render)\n")
    try:
        t0 = time.time()
        r = requests.get("https://www.google.com/", timeout=15)
        dt = time.time() - t0
        out.append(f"    OK → HTTP {r.status_code} en {dt:.2f} s\n")
    except Exception as e:
        out.append(f"    ÉCHEC → {type(e).__name__}: {e}\n")

    out.append("\n" + "=" * 70 + "\n")
    out.append("Interprétation :\n")
    out.append("- Si [1]-[3] passent mais [4]-[5] timeout → serveur T/SL filtre ou ignore\n")
    out.append("  les connexions depuis Render (IP, UA, cookies de session).\n")
    out.append("- Si [1]-[3] échouent → problème réseau fondamental.\n")
    out.append("- Si [6] passe et [4]-[5] non → confirmation que c'est spécifique T/SL.\n")
    out.append("</pre>")
    return "".join(out)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
