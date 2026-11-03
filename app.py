"""
Tableau de bord ITIE-RDC — Suivi des télédéclarations 2024 (version live).

Scrape les sources T/SL de l'ITIE-RDC toutes les 5 minutes et affiche
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
CACHE_TTL_SUIVI = 900       # 15 minutes pour la page de suivi (3 scrapings combinés)
USER_AGENT = "Mozilla/5.0 (compatible; ITIE-RDC-TDB/1.0; +https://www.itierdc.net)"
TIMEOUT = 120         # Le serveur T/SL met 40-60 s à répondre, on laisse large
MAX_RETRIES = 1       # 1 tentative + 1 retry

_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_scrape_lock = threading.Lock()

# Cache séparé pour les données détaillées, clé = (year, reportselect, perim)
_details_cache: dict[tuple, dict] = {}
_details_lock = threading.Lock()

# Cache séparé pour la page Suivi (matrice + par-ese + flux)
_suivi_cache: dict[str, Any] = {"data": None, "ts": 0.0}
_suivi_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_number(s: str) -> float:
    """Convertit « 1 018 813 842,45 » → 1018813842.45."""
    s = re.sub(r"[\s ]", "", s or "").replace(",", ".")
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
    """Parse la matrice entreprises × régies de /tableau_de_bord_2024.php.
    Compte les déclarations sans détail (version utilisée par /)."""
    html = fetch(SRC_2024)
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if not table:
        return []
    entities: list[dict[str, Any]] = []
    tbody = table.find("tbody") or table
    date_re = re.compile(r"\d{1,2}/\d{1,2}")
    for tr in tbody.find_all("tr"):
        # Le site utilise <th> pour les 3 premières colonnes (No, NIF, Entreprise)
        # et <td> pour les 155 colonnes de régies.
        cells = tr.find_all(["th", "td"])
        if len(cells) < 4:
            continue
        no = cells[0].get_text(strip=True)
        nif = cells[1].get_text(strip=True)
        name = cells[2].get_text(strip=True)
        if not name or name.lower() == "entreprises":
            continue  # saute la ligne d'en-tête
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


def scrape_2024_matrix() -> dict[str, Any]:
    """Parse la matrice complète entreprises × régies de /tableau_de_bord_2024.php.

    Retourne :
      - regies   : liste des noms de régies (ordre colonnes)
      - entreprises : liste d'entreprises avec, pour chaque, la liste des
                      déclarations (régie, date, validé).

    Le site encode le statut par couleur de fond :
      - #00FF00 (vert) = validé
      - #FF0000 (rouge) = non validé
    """
    html = fetch(SRC_2024)
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if not table:
        return {"regies": [], "entreprises": [], "declarations": [],
                "nb_valid": 0, "nb_invalid": 0}

    rows = table.find_all("tr")
    if not rows:
        return {"regies": [], "entreprises": [], "declarations": [],
                "nb_valid": 0, "nb_invalid": 0}

    # Ligne entête : No | NIF | Entreprises | <régies...>
    header_cells = rows[0].find_all(["th", "td"])
    headers = [c.get_text(strip=True) for c in header_cells]
    regies = headers[3:] if len(headers) > 3 else []

    entreprises: list[dict[str, Any]] = []
    declarations: list[dict[str, Any]] = []
    date_re = re.compile(r"\d{1,2}/\d{1,2}")
    nb_valid = 0
    nb_invalid = 0

    for tr in rows[1:]:
        # Le site utilise <th> pour les 3 premières colonnes (No, NIF, Entreprise)
        # et <td> pour les 155 colonnes de régies. On lit les deux.
        cells = tr.find_all(["th", "td"])
        if len(cells) < 4:
            continue
        no = cells[0].get_text(strip=True)
        nif = cells[1].get_text(strip=True)
        name = cells[2].get_text(strip=True)
        if not name:
            continue
        ent_decls: list[dict[str, Any]] = []
        for i, c in enumerate(cells[3:]):
            txt = c.get_text(strip=True)
            if not date_re.match(txt):
                continue
            style = (c.get("style") or "").lower().replace(" ", "")
            validated = "#00ff00" in style
            invalidated = "#ff0000" in style
            regie_name = regies[i] if i < len(regies) else f"col{i}"
            d = {
                "regie": regie_name,
                "date": txt,
                "validated": validated,
                "status": "valid" if validated else ("invalid" if invalidated else "unknown"),
            }
            ent_decls.append(d)
            declarations.append({"entreprise": name, "nif": nif, **d})
            if validated:
                nb_valid += 1
            elif invalidated:
                nb_invalid += 1

        last_date = ent_decls[-1]["date"] if ent_decls else "—"
        nb_touched = len(ent_decls)
        nb_v = sum(1 for d in ent_decls if d["validated"])
        nb_nv = nb_touched - nb_v
        entreprises.append({
            "no": no, "nif": nif, "name": name,
            "nb_regies": nb_touched,
            "nb_valid": nb_v,
            "nb_invalid": nb_nv,
            "last_date": last_date,
            "declarations": ent_decls,
            "status": "crit" if nb_touched == 0 else (
                "ok" if (nb_touched > 0 and nb_nv == 0) else "warn"
            ),
        })

    return {
        "regies": regies,
        "entreprises": entreprises,
        "declarations": declarations,
        "nb_valid": nb_valid,
        "nb_invalid": nb_invalid,
    }


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
        first = texts[0]
        is_num_row = first.isdigit()
        if not is_num_row and len(cells) >= 10:
            # Ligne de régie (groupement)
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


def parse_etats_par_ese(html: str) -> dict[str, Any]:
    """Parse le tableau 'Reporting par entreprise' retourné par /etats/.

    Structure :
      - lignes `.entete.header_line` : secteur (SECTEUR PÉTROLIER, SECTEUR MINIER, …)
      - lignes `.regieline` : type d'entreprise (ENTREPRISE DE L'ETAT, PRIVÉE, …)
      - lignes vides ou `.odd` : entreprises individuelles
      - lignes `.totalLine` : totaux
    """
    soup = BeautifulSoup(html, "lxml")
    entreprises: list[dict[str, Any]] = []
    secteurs: dict[str, dict[str, Any]] = {}

    current_secteur: str = "—"
    current_type: str = "—"

    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if not cells:
                continue
            classes = tr.get("class") or []
            classes_str = " ".join(classes).lower()
            texts = [c.get_text(strip=True) for c in cells]
            first = texts[0]

            if "entete" in classes_str or "header_line" in classes_str:
                # Il y a deux lignes 'entete header_line' par secteur :
                # (1) le nom du secteur + "Montants initalements déclarés" (~4 cellules)
                # (2) les sous-entêtes "Sociétés / Etat / Différence" répétés (9 cellules)
                # On ne garde que la première.
                sub_header_tokens = ("sociétés", "societes", "etat", "état",
                                     "différence", "difference")
                first_low = first.lower()
                is_sub_header = first_low in sub_header_tokens or len(cells) >= 9
                if first and not is_sub_header and "montant" not in first_low:
                    current_secteur = first
                    if current_secteur not in secteurs:
                        secteurs[current_secteur] = {"name": current_secteur,
                                                     "soc_fin": 0, "etat_fin": 0,
                                                     "diff_fin": 0, "nb_ent": 0}
                continue

            if "regieline" in classes_str:
                # Type d'entreprise (ENTREPRISE DE L'ETAT, PRIVÉE, etc.)
                if first:
                    current_type = first
                continue

            if "totalline" in classes_str:
                continue

            # Ligne d'entreprise individuelle
            if len(cells) < 10:
                continue
            name = first
            if not name:
                continue
            try:
                ent = {
                    "name": name,
                    "secteur": current_secteur,
                    "type": current_type,
                    "soc_decl":  parse_number(texts[1]),
                    "etat_decl": parse_number(texts[2]),
                    "diff_decl": parse_number(texts[3]),
                    "soc_adj":   parse_number(texts[4]),
                    "etat_adj":  parse_number(texts[5]),
                    "diff_adj":  parse_number(texts[6]),
                    "soc_fin":   parse_number(texts[7]),
                    "etat_fin":  parse_number(texts[8]),
                    "diff_fin":  parse_number(texts[9]),
                }
                entreprises.append(ent)
                s = secteurs.setdefault(current_secteur, {"name": current_secteur,
                                                          "soc_fin": 0, "etat_fin": 0,
                                                          "diff_fin": 0, "nb_ent": 0})
                s["soc_fin"]  += ent["soc_fin"]
                s["etat_fin"] += ent["etat_fin"]
                s["diff_fin"] += ent["diff_fin"]
                s["nb_ent"]   += 1
            except (IndexError, ValueError):
                pass

    totals = {
        "nb_ent": len(entreprises),
        "nb_secteurs": len(secteurs),
        "sum_soc_fin":  sum(e["soc_fin"]  for e in entreprises),
        "sum_etat_fin": sum(e["etat_fin"] for e in entreprises),
        "sum_diff_fin": sum(e["diff_fin"] for e in entreprises),
    }
    return {"entreprises": entreprises,
            "secteurs": list(secteurs.values()),
            "totals": totals}


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
# Page Suivi — matrice + stats par entreprise/régie + rapprochement montants
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    """Normalise un nom pour matcher entreprises et régies entre sources."""
    s = (s or "").upper().strip()
    s = re.sub(r"\s+", " ", s)
    return s


def build_suivi() -> dict[str, Any]:
    """Agrège : matrice (dates/statut) + flux (montants par régie) +
    par-entreprise (totaux par entreprise). Fait 3 appels web."""
    log.info("Build suivi (3 scrapings) …")
    try:
        mat = scrape_2024_matrix()
    except Exception as e:
        log.error("Scrape matrix 2024 : %s", e)
        mat = {"regies": [], "entreprises": [], "declarations": [], "nb_valid": 0, "nb_invalid": 0}

    try:
        flux_data = get_details(2024, "decla-flux-global", "")
    except Exception as e:
        log.warning("Flux global KO : %s", e)
        flux_data = {"regies": [], "flux": [], "totals": {}}

    try:
        html = fetch_etats(2024, "decla-par-ese", "", "")
        parese = parse_etats_par_ese(html)
    except Exception as e:
        log.warning("Par-ese KO : %s", e)
        parese = {"entreprises": [], "secteurs": [], "totals": {}}

    # Montants par régie (depuis flux-global)
    montants_par_regie = {}
    for r in flux_data.get("regies", []):
        montants_par_regie[_norm(r["name"])] = r

    # Montants par entreprise (depuis par-ese)
    montants_par_ent = {}
    for e in parese["entreprises"]:
        montants_par_ent[_norm(e["name"])] = e

    # Enrichit chaque entreprise de la matrice avec son montant total
    ents = mat["entreprises"]
    for e in ents:
        m = montants_par_ent.get(_norm(e["name"]))
        if m:
            e["soc_fin"]  = m["soc_fin"]
            e["etat_fin"] = m["etat_fin"]
            e["diff_fin"] = m["diff_fin"]
            e["secteur"]  = m.get("secteur", "—")
            e["type"]     = m.get("type", "—")
        else:
            e["soc_fin"] = 0
            e["etat_fin"] = 0
            e["diff_fin"] = 0
            e["secteur"] = "—"
            e["type"] = "—"

    # Compte par régie : combien d'entreprises déclarantes, combien validées
    regies_stats = {}
    for name in mat["regies"]:
        regies_stats[name] = {
            "name": name,
            "nb_ent": 0, "nb_valid": 0, "nb_invalid": 0,
            "last_date": "—",
            "entreprises": [],  # liste des entreprises déclarantes
            "soc_fin":  0, "etat_fin": 0, "diff_fin": 0,
        }
    for d in mat["declarations"]:
        rs = regies_stats.get(d["regie"])
        if not rs:
            continue
        rs["nb_ent"] += 1
        if d["validated"]:
            rs["nb_valid"] += 1
        elif d["status"] == "invalid":
            rs["nb_invalid"] += 1
        rs["entreprises"].append({"name": d["entreprise"], "nif": d["nif"],
                                  "date": d["date"], "validated": d["validated"]})
        if d["date"] > rs["last_date"]:
            rs["last_date"] = d["date"]

    # Jointure montants par régie (sur name normalisé)
    for name, rs in regies_stats.items():
        m = montants_par_regie.get(_norm(name))
        if m:
            rs["soc_fin"]  = m["soc_fin"]
            rs["etat_fin"] = m["etat_fin"]
            rs["diff_fin"] = m["diff_fin"]

    regies_list = sorted(regies_stats.values(), key=lambda r: (-r["nb_ent"], r["name"]))

    # KPIs globaux
    nb_declarations = len(mat["declarations"])
    nb_valid = mat["nb_valid"]
    nb_invalid = mat["nb_invalid"]
    nb_ent_actifs = sum(1 for e in ents if e["nb_regies"] > 0)
    nb_ent_total = len(ents)
    nb_regies_actives = sum(1 for r in regies_list if r["nb_ent"] > 0)
    nb_regies_total = len(regies_list)
    pct_valid = (nb_valid / nb_declarations * 100) if nb_declarations else 0

    totals_flux = flux_data.get("totals", {})
    totals_ese = parese.get("totals", {})

    return {
        "entreprises": ents,
        "regies": regies_list,
        "regies_names": mat["regies"],
        "declarations": mat["declarations"],
        "flux": flux_data.get("flux", []),
        "secteurs": parese.get("secteurs", []),
        "kpi": {
            "nb_declarations": nb_declarations,
            "nb_valid": nb_valid,
            "nb_invalid": nb_invalid,
            "pct_valid": pct_valid,
            "nb_ent_actifs": nb_ent_actifs,
            "nb_ent_total": nb_ent_total,
            "nb_regies_actives": nb_regies_actives,
            "nb_regies_total": nb_regies_total,
            "sum_soc_fin":  totals_ese.get("sum_soc_fin",  0),
            "sum_etat_fin": totals_ese.get("sum_etat_fin", 0),
            "sum_diff_fin": totals_ese.get("sum_diff_fin", 0),
        },
        "scraped_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }


def get_suivi() -> dict[str, Any]:
    now = time.time()
    if _suivi_cache["data"] is not None and (now - _suivi_cache["ts"]) < CACHE_TTL_SUIVI:
        return _suivi_cache["data"]
    with _suivi_lock:
        now = time.time()
        if _suivi_cache["data"] is not None and (now - _suivi_cache["ts"]) < CACHE_TTL_SUIVI:
            return _suivi_cache["data"]
        try:
            fresh = build_suivi()
            _suivi_cache["data"] = fresh
            _suivi_cache["ts"] = now
            log.info("Cache Suivi rafraîchi.")
            return fresh
        except Exception as e:
            log.error("Échec build_suivi : %s", e)
            if _suivi_cache["data"] is not None:
                return _suivi_cache["data"]
            return {
                "entreprises": [], "regies": [], "regies_names": [],
                "declarations": [], "flux": [], "secteurs": [],
                "kpi": {"nb_declarations": 0, "nb_valid": 0, "nb_invalid": 0,
                        "pct_valid": 0, "nb_ent_actifs": 0, "nb_ent_total": 0,
                        "nb_regies_actives": 0, "nb_regies_total": 0,
                        "sum_soc_fin": 0, "sum_etat_fin": 0, "sum_diff_fin": 0},
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
    _suivi_cache["data"] = None
    _suivi_cache["ts"] = 0
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


@app.route("/suivi")
def suivi():
    """Page Suivi — matrice entreprise × régie + statuts + montants."""
    data = get_suivi()
    return render_template("suivi.html", data=data)


@app.route("/api/suivi")
def api_suivi():
    """JSON du Suivi — pour une éventuelle app mobile ou tableau Google Sheets."""
    from flask import jsonify
    data = get_suivi()
    # Allège le payload : on retire les sous-listes « declarations » déjà
    # redondantes avec le top-level.
    payload = {
        "scraped_at": data.get("scraped_at"),
        "kpi": data.get("kpi"),
        "entreprises": [
            {k: v for k, v in e.items() if k != "declarations"}
            for e in data.get("entreprises", [])
        ],
        "regies": [
            {k: v for k, v in r.items() if k != "entreprises"}
            for r in data.get("regies", [])
        ],
        "declarations": data.get("declarations"),
    }
    return jsonify(payload)


@app.route("/diag-matrix")
def diag_matrix():
    """Diagnostic — ce que le parser voit depuis Render sur tableau_de_bord_2024.php."""
    out = ["<pre style='font-family:monospace;padding:20px;font-size:12px'>"]
    out.append("DIAGNOSTIC MATRICE 2024 — depuis Render\n")
    out.append("=" * 70 + "\n\n")
    try:
        t0 = time.time()
        html = fetch(SRC_2024)
        out.append(f"[1] GET {SRC_2024}\n")
        out.append(f"    HTTP OK, {len(html)} octets, {time.time()-t0:.1f} s\n\n")

        soup = BeautifulSoup(html, "lxml")
        tables = soup.find_all("table")
        out.append(f"[2] Nombre de tables trouvées : {len(tables)}\n\n")

        for idx, table in enumerate(tables[:3]):
            rows = table.find_all("tr")
            tbody = table.find("tbody")
            thead = table.find("thead")
            out.append(f"[3.{idx}] Table n°{idx} : {len(rows)} lignes, "
                       f"thead={'oui' if thead else 'non'}, tbody={'oui' if tbody else 'non'}\n")
            # Première ligne
            if rows:
                r0 = rows[0]
                cells_td = r0.find_all("td")
                cells_th = r0.find_all("th")
                cells_any = r0.find_all(["th", "td"])
                out.append(f"    Row 0 : {len(cells_td)} td, {len(cells_th)} th, "
                           f"{len(cells_any)} au total — classe='{r0.get('class', [])}'\n")
                if cells_any:
                    sample = [c.get_text(strip=True)[:20] for c in cells_any[:6]]
                    out.append(f"    Row 0 contenu (6 premières cellules) : {sample}\n")
            # Deuxième ligne
            if len(rows) > 1:
                r1 = rows[1]
                cells_td = r1.find_all("td")
                cells_th = r1.find_all("th")
                cells_any = r1.find_all(["th", "td"])
                out.append(f"    Row 1 : {len(cells_td)} td, {len(cells_th)} th, "
                           f"{len(cells_any)} au total — classe='{r1.get('class', [])}'\n")
                if cells_any:
                    sample = [c.get_text(strip=True)[:20] for c in cells_any[:6]]
                    out.append(f"    Row 1 contenu (6 premières cellules) : {sample}\n")
            # Troisième ligne
            if len(rows) > 2:
                r2 = rows[2]
                cells_any = r2.find_all(["th", "td"])
                if cells_any:
                    sample = [c.get_text(strip=True)[:20] for c in cells_any[:6]]
                    out.append(f"    Row 2 (6 premières cellules) : {sample}\n")
            out.append("\n")

        # Essai du parser actuel
        out.append("[4] Résultat du parser actuel (scrape_2024_matrix) :\n")
        mat = scrape_2024_matrix()
        out.append(f"    regies      : {len(mat['regies'])} items\n")
        out.append(f"    entreprises : {len(mat['entreprises'])} items\n")
        out.append(f"    declarations: {len(mat['declarations'])} items\n")
        out.append(f"    nb_valid    : {mat['nb_valid']}\n")
        out.append(f"    nb_invalid  : {mat['nb_invalid']}\n")

        # Snippet de l'HTML brut autour de la ligne 1 (si existe)
        out.append("\n[5] Extrait HTML brut (après la 1ère balise <tr>, 500 car.) :\n")
        import re as _re
        m = _re.search(r"<tr[^>]*>", html)
        if m:
            start = m.end()
            end = html.find("</tr>", start)
            snippet = html[start:min(end, start+500)] if end > 0 else html[start:start+500]
            # échappe les < > pour affichage
            snippet = snippet.replace("<", "[").replace(">", "]")
            out.append(f"    {snippet}\n")

    except Exception as e:
        import traceback
        out.append(f"\nERREUR : {type(e).__name__}: {e}\n")
        out.append(traceback.format_exc())

    out.append("</pre>")
    return "".join(out)


@app.route("/diag")
def diag():
    """
    Diagnostic réseau — teste la joignabilité du serveur T/SL depuis Render.
    """
    import socket
    import ssl

    out = ["<pre style='font-family: monospace; padding: 20px;'>"]
    out.append("DIAGNOSTIC — depuis les serveurs Render\n")
    out.append(f"Horodatage : {datetime.now().isoformat()}\n")
    out.append("=" * 70 + "\n")

    out.append("\n[1] Résolution DNS de www.tsl-itierdc.org\n")
    try:
        ip = socket.gethostbyname("www.tsl-itierdc.org")
        out.append(f"    OK → IP résolue : {ip}\n")
    except Exception as e:
        out.append(f"    ÉCHEC → {type(e).__name__}: {e}\n")

    out.append("\n[2] Connexion TCP sur port 443 (handshake HTTPS)\n")
    try:
        t0 = time.time()
        with socket.create_connection(("www.tsl-itierdc.org", 443), timeout=15):
            dt = time.time() - t0
            out.append(f"    OK → connecté en {dt:.2f} s\n")
    except Exception as e:
        out.append(f"    ÉCHEC → {type(e).__name__}: {e}\n")

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

    out.append("\n[4] HEAD https://www.tsl-itierdc.org/\n")
    try:
        t0 = time.time()
        r = requests.head("https://www.tsl-itierdc.org/",
                          timeout=30,
                          headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
                          allow_redirects=True)
        dt = time.time() - t0
        out.append(f"    OK → HTTP {r.status_code} en {dt:.2f} s\n")
    except Exception as e:
        out.append(f"    ÉCHEC → {type(e).__name__}: {str(e)[:200]}\n")

    out.append("\n[5] GET https://www.tsl-itierdc.org/tableau_de_bord/index.php\n")
    try:
        t0 = time.time()
        r = requests.get("https://www.tsl-itierdc.org/tableau_de_bord/index.php",
                         timeout=90,
                         headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
        dt = time.time() - t0
        out.append(f"    OK → HTTP {r.status_code} en {dt:.2f} s, {len(r.content)} octets\n")
    except Exception as e:
        out.append(f"    ÉCHEC → {type(e).__name__}: {str(e)[:200]}\n")

    out.append("\n[6] GET https://www.google.com/\n")
    try:
        t0 = time.time()
        r = requests.get("https://www.google.com/", timeout=15)
        dt = time.time() - t0
        out.append(f"    OK → HTTP {r.status_code} en {dt:.2f} s\n")
    except Exception as e:
        out.append(f"    ÉCHEC → {type(e).__name__}: {e}\n")

    out.append("</pre>")
    return "".join(out)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
