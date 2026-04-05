"""
aifa.py — Lookup farmaci italiani

Strategia lookup AIC (in ordine di priorità):
  1. Tabella aifa_lookup nel DB locale (da CSV AIFA + dizionario OTC)
  2. Endpoint AIFA online (vari tentativi)
  3. Fallback con nome generico

Immagini: Open Products Facts → avatar SVG
"""

import re
import os
import hashlib
import requests
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "/data/farmaci.db")
IMG_DIR = os.path.join(os.path.dirname(__file__), "static", "img")

AVATAR_COLORS = [
    "#2563eb", "#16a34a", "#d97706", "#dc2626", "#7c3aed",
    "#0891b2", "#c2410c", "#1d4ed8", "#15803d", "#b45309",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "it-IT,it;q=0.9",
}


def lookup_aic(aic: str) -> dict | None:
    aic_clean = re.sub(r"[^0-9]", "", aic)
    if len(aic_clean) == 9:
        aic_farmaco = aic_clean[:6]
    elif len(aic_clean) == 6:
        aic_farmaco = aic_clean
    else:
        print(f"[AIFA] AIC non valido: {aic}")
        return None

    # Livello 1: lookup locale (velocissimo, offline)
    farmaco = _lookup_locale(aic_farmaco)

    # Livello 2: prova endpoint AIFA online
    if not farmaco:
        farmaco = _cerca_aifa_online(aic_farmaco)

    # Livello 3: fallback generico
    if not farmaco:
        farmaco = {
            "aic": aic_farmaco,
            "nome": f"Farmaco AIC {aic_farmaco}",
            "nome_commerciale": f"Farmaco AIC {aic_farmaco}",
            "principio_attivo": None, "forma_farmaceutica": None,
            "dosaggio": None, "atc": None, "produttore": None,
            "foglietto_url": f"https://medicinali.aifa.gov.it/?aic={aic_farmaco}",
        }

    gtin = _aic_to_gtin(aic_clean)
    farmaco["immagine_url"]    = cerca_immagine_prodotto(farmaco["nome"], gtin)
    farmaco["immagine_locale"] = None
    farmaco["colore_avatar"]   = colore_avatar(farmaco["nome"])
    return farmaco


def _lookup_locale(aic: str) -> dict | None:
    """Cerca nella tabella aifa_lookup del DB locale."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM aifa_lookup WHERE aic=?", (aic,)
        ).fetchone()
        conn.close()
        if row:
            print(f"[AIFA] Trovato in DB locale: {row['nome']}")
            return {
                "aic":               aic,
                "nome":              row["nome"],
                "nome_commerciale":  row["nome"],
                "principio_attivo":  row["principio_attivo"],
                "forma_farmaceutica": None,
                "dosaggio":          None,
                "atc":               row["atc"],
                "produttore":        None,
                "foglietto_url":     f"https://medicinali.aifa.gov.it/?aic={aic}",
            }
    except Exception as e:
        print(f"[AIFA] Errore lookup locale: {e}")
    return None


def _cerca_aifa_online(aic: str) -> dict | None:
    """Prova endpoint REST del portale AIFA medicinali."""
    endpoints = [
        f"https://medicinali.aifa.gov.it/api/medicinali?aic={aic}",
        f"https://medicinali.aifa.gov.it/api/v1/medicinali?aic={aic}&size=1",
        f"https://medicinali.aifa.gov.it/rest/medicinali/{aic}",
    ]
    for url in endpoints:
        try:
            r = requests.get(url, headers=HEADERS, timeout=8)
            if r.status_code == 200 and "application/json" in r.headers.get("content-type", ""):
                data = r.json()
                farmaco = _parse_json(data, aic)
                if farmaco:
                    print(f"[AIFA] Online trovato su: {url}")
                    return farmaco
        except Exception:
            continue
    return None


def _parse_json(data, aic: str) -> dict | None:
    item = None
    if isinstance(data, dict):
        item = (data.get("content") or data.get("data") or
                data.get("results") or data.get("medicinale") or data.get("farmaco"))
        if isinstance(item, list) and item:
            item = item[0]
        elif not isinstance(item, dict):
            item = data
    elif isinstance(data, list) and data:
        item = data[0]
    if not item or not isinstance(item, dict):
        return None
    nome = (item.get("denominazione") or item.get("nome") or
            item.get("nomeMedicinale") or "")
    if not nome or len(nome) < 2:
        return None
    return {
        "aic": aic, "nome": nome, "nome_commerciale": nome,
        "principio_attivo": item.get("principioAttivo") or item.get("principio_attivo"),
        "forma_farmaceutica": item.get("formaFarmaceutica"),
        "dosaggio": item.get("dosaggio"),
        "atc": item.get("codiceAtc") or item.get("atc"),
        "produttore": item.get("titolareAic"),
        "foglietto_url": f"https://medicinali.aifa.gov.it/?aic={aic}",
    }


# ═══════════════════════════════════════════════════════════════════════════
# IMMAGINI
# ═══════════════════════════════════════════════════════════════════════════

def cerca_immagine_prodotto(nome: str, gtin: str | None = None) -> str | None:
    if gtin:
        url = _opf_per_gtin(gtin)
        if url: return url
    url = _opf_per_nome(nome)
    if url: return url
    if gtin:
        url = _off_per_gtin(gtin)
        if url: return url
    return None


def _opf_per_gtin(gtin):
    try:
        r = requests.get(f"https://world.openproductsfacts.org/api/v2/product/{gtin}",
                         headers=HEADERS, timeout=8)
        return _estrai_immagine(r.json()) if r.status_code == 200 else None
    except Exception: return None


def _off_per_gtin(gtin):
    try:
        r = requests.get(f"https://world.openfoodfacts.org/api/v2/product/{gtin}",
                         headers=HEADERS, timeout=8)
        return _estrai_immagine(r.json()) if r.status_code == 200 else None
    except Exception: return None


def _opf_per_nome(nome):
    nome_breve = re.split(r"\d", nome)[0].strip()
    if len(nome_breve) < 3: return None
    try:
        r = requests.get(
            "https://world.openproductsfacts.org/cgi/search.pl",
            params={"search_terms": nome_breve, "search_simple": 1,
                    "action": "process", "json": 1, "page_size": 5,
                    "fields": "product_name,image_front_url,image_front_small_url"},
            headers=HEADERS, timeout=8)
        for p in r.json().get("products", []):
            img = p.get("image_front_url") or p.get("image_front_small_url")
            if img: return img
    except Exception: pass
    return None


def _estrai_immagine(data):
    if data.get("status") != 1: return None
    p = data.get("product", {})
    imgs = p.get("selected_images", {}).get("front", {}).get("display", {})
    return imgs.get("it") or imgs.get("en") or p.get("image_front_url") or p.get("image_url")


def salva_immagine_utente(farmaco_id: int, image_bytes: bytes, ext: str = "jpg") -> str:
    cartella = os.path.join(IMG_DIR, "farmaci")
    os.makedirs(cartella, exist_ok=True)
    filename = f"farmaco_{farmaco_id}.{ext}"
    with open(os.path.join(cartella, filename), "wb") as f:
        f.write(image_bytes)
    return f"/static/img/farmaci/{filename}"


# ═══════════════════════════════════════════════════════════════════════════
# AVATAR + UTILS
# ═══════════════════════════════════════════════════════════════════════════

def colore_avatar(nome: str) -> str:
    idx = int(hashlib.md5(nome.lower().encode()).hexdigest(), 16) % len(AVATAR_COLORS)
    return AVATAR_COLORS[idx]


def iniziali_avatar(nome: str) -> str:
    nome_pulito = re.sub(r"\d.*", "", nome).strip()
    parole = nome_pulito.split()
    if len(parole) >= 2:
        return "".join(p[0].upper() for p in parole[:3])
    return nome_pulito[:3].upper() or "?"


def _aic_to_gtin(aic: str) -> str | None:
    aic_9 = aic.zfill(9)[:9]
    base  = "80" + aic_9   # 11 cifre
    if len(base) != 11: return None
    pesi  = [1, 3] * 6
    tot   = sum(int(base[i]) * pesi[i] for i in range(11))
    check = (10 - (tot % 10)) % 10
    gtin  = base + str(check)
    return gtin if len(gtin) == 13 else None
