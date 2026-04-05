"""
aifa_import.py — Importa il database farmaci AIFA nel DB locale

Scarica i CSV pubblici AIFA (classe A, H, lista trasparenza) e li combina
con un dizionario hardcoded di farmaci OTC/classe C comuni.
Popola la tabella `aifa_lookup` nel DB SQLite per lookup offline istantaneo.

Uso:
  python3 aifa_import.py            # scarica e importa tutto
  python3 aifa_import.py --skip-download  # usa solo il dizionario hardcoded
"""

import sqlite3
import os
import sys
import re
import io
import requests

DB_PATH = os.environ.get("DB_PATH", "/data/farmaci.db")

# ── URL CSV AIFA pubblici ────────────────────────────────────────────────
CSV_SOURCES = [
    {
        "url": "https://www.aifa.gov.it/documents/20142/847339/elenco_medicinali_carenti.csv",
        "nome": "Farmaci carenti",
        "separatore": ";",
        "col_aic": "Codice AIC",
        "col_nome": "Nome medicinale",
        "col_pa": "Principio attivo",
        "col_atc": "Codice ATC",
        "skip_righe": 2,
    },
    {
        "url": "https://www.aifa.gov.it/documents/20142/3442801/Classe_H_per_nome_commerciale_30-09-2025.csv",
        "nome": "Classe H",
        "separatore": ";",
        "col_aic": "Codice AIC",
        "col_nome": "Denominazione e Confezione",
        "col_pa": "Principio Attivo",
        "col_atc": None,
    },
    {
        "url": "https://www.aifa.gov.it/documents/20142/1648033/Classe_A_per_nome_commerciale.csv",
        "nome": "Classe A",
        "separatore": ";",
        "col_aic": "Codice AIC",
        "col_nome": "Denominazione e Confezione",
        "col_pa": "Principio Attivo",
        "col_atc": None,
    },
    {
        "url": "https://www.aifa.gov.it/documents/20142/1541885/lista_trasparenza.csv",
        "nome": "Lista Trasparenza",
        "separatore": ";",
        "col_aic": "Codice AIC",
        "col_nome": "Denominazione",
        "col_pa": "Principio attivo",
        "col_atc": "ATC",
    },
]

# ── Dizionario hardcoded farmaci OTC / Classe C comuni ───────────────────
# Formato: "AIC_6_cifre": ("Nome commerciale", "Principio attivo", "ATC")
FARMACI_OTC = {
    "033656": ("Enantyum 25mg", "Dexketoprofene trometamolo", "M01AE17"),
    "020102": ("Tachipirina 1000mg", "Paracetamolo", "N02BE01"),
    "020101": ("Tachipirina 500mg", "Paracetamolo", "N02BE01"),
    "020100": ("Tachipirina 250mg", "Paracetamolo", "N02BE01"),
    "023209": ("Nurofen 400mg", "Ibuprofene", "M01AE01"),
    "025166": ("Moment 400mg", "Ibuprofene", "M01AE01"),
    "034748": ("Brufen 600mg", "Ibuprofene", "M01AE01"),
    "027104": ("Aspirina 500mg", "Acido acetilsalicilico", "N02BA01"),
    "026443": ("Cardioaspirina 100mg", "Acido acetilsalicilico", "B01AC06"),
    "024402": ("Eutirox 75mcg", "Levotiroxina sodica", "H03AA01"),
    "024403": ("Eutirox 100mcg", "Levotiroxina sodica", "H03AA01"),
    "024404": ("Eutirox 125mcg", "Levotiroxina sodica", "H03AA01"),
    "024405": ("Eutirox 150mcg", "Levotiroxina sodica", "H03AA01"),
    "024401": ("Eutirox 50mcg", "Levotiroxina sodica", "H03AA01"),
    "035246": ("Triatec 5mg", "Ramipril", "C09AA05"),
    "035247": ("Triatec 10mg", "Ramipril", "C09AA05"),
    "029836": ("Glucophage 500mg", "Metformina cloridrato", "A10BA02"),
    "029837": ("Glucophage 850mg", "Metformina cloridrato", "A10BA02"),
    "029838": ("Glucophage 1000mg", "Metformina cloridrato", "A10BA02"),
    "033871": ("Lansox 30mg", "Lansoprazolo", "A02BC03"),
    "030482": ("Nexium 20mg", "Esomeprazolo", "A02BC05"),
    "030483": ("Nexium 40mg", "Esomeprazolo", "A02BC05"),
    "028773": ("Omeprazen 20mg", "Omeprazolo", "A02BC01"),
    "034539": ("Pantoprazolo EG 40mg", "Pantoprazolo", "A02BC02"),
    "038271": ("Atorvastatina 20mg", "Atorvastatina calcica", "C10AA05"),
    "038272": ("Atorvastatina 40mg", "Atorvastatina calcica", "C10AA05"),
    "025354": ("Zocor 20mg", "Simvastatina", "C10AA01"),
    "025355": ("Zocor 40mg", "Simvastatina", "C10AA01"),
    "034285": ("Amlodipina 5mg", "Amlodipina besilato", "C08CA01"),
    "034286": ("Amlodipina 10mg", "Amlodipina besilato", "C08CA01"),
    "027826": ("Norvasc 5mg", "Amlodipina besilato", "C08CA01"),
    "031395": ("Concor 5mg", "Bisoprololo fumarato", "C07AB07"),
    "031396": ("Concor 10mg", "Bisoprololo fumarato", "C07AB07"),
    "036363": ("Metoprololo 100mg", "Metoprololo tartrato", "C07AB02"),
    "026444": ("Coumadin 5mg", "Warfarin sodico", "B01AA03"),
    "035519": ("Pradaxa 110mg", "Dabigatran etexilato", "B01AE07"),
    "036228": ("Xarelto 20mg", "Rivaroxaban", "B01AF01"),
    "034277": ("Eliquis 5mg", "Apixaban", "B01AF02"),
    "024718": ("Metformina 850mg", "Metformina cloridrato", "A10BA02"),
    "034980": ("Januvia 100mg", "Sitagliptin fosfato", "A10BH01"),
    "029073": ("Lantus 100UI/ml", "Insulina glargine", "A10AE04"),
    "036542": ("Toujeo 300UI/ml", "Insulina glargine", "A10AE04"),
    "031218": ("Levitra 10mg", "Vardenafil cloridrato", "G04BE09"),
    "028072": ("Viagra 50mg", "Sildenafil citrato", "G04BE03"),
    "033025": ("Cialis 20mg", "Tadalafil", "G04BE08"),
    "035738": ("Zoloft 50mg", "Sertralina cloridrato", "N06AB06"),
    "031671": ("Efexor 75mg", "Venlafaxina cloridrato", "N06AX16"),
    "026942": ("Tavor 1mg", "Lorazepam", "N05BA06"),
    "026943": ("Tavor 2.5mg", "Lorazepam", "N05BA06"),
    "026030": ("Valium 5mg", "Diazepam", "N05BA01"),
    "023682": ("En 0.5mg", "Alprazolam", "N05BA12"),
    "023683": ("En 1mg", "Alprazolam", "N05BA12"),
    "029284": ("Lyrica 75mg", "Pregabalin", "N03AX16"),
    "029285": ("Lyrica 150mg", "Pregabalin", "N03AX16"),
    "029286": ("Lyrica 300mg", "Pregabalin", "N03AX16"),
    "031630": ("Gabapentin 300mg", "Gabapentin", "N03AX12"),
    "027038": ("Voltaren 75mg", "Diclofenac sodico", "M01AB05"),
    "027039": ("Voltaren 100mg", "Diclofenac sodico", "M01AB05"),
    "033060": ("Arcoxia 90mg", "Etoricoxib", "M01AH05"),
    "033061": ("Arcoxia 120mg", "Etoricoxib", "M01AH05"),
    "026948": ("Aulin 100mg", "Nimesulide", "M01AX17"),
    "027537": ("Ketoprofene 100mg", "Ketoprofene", "M01AE03"),
    "024680": ("Augmentin 875mg", "Amoxicillina + Acido clavulanico", "J01CR02"),
    "024679": ("Augmentin 500mg", "Amoxicillina + Acido clavulanico", "J01CR02"),
    "032074": ("Zimox 1g", "Amoxicillina triidrato", "J01CA04"),
    "027069": ("Zitromax 500mg", "Azitromicina diidrato", "J01FA10"),
    "023490": ("Klacid 500mg", "Claritromicina", "J01FA09"),
    "031753": ("Levoxacin 500mg", "Levofloxacina emiidrato", "J01MA12"),
    "028613": ("Ciproxin 500mg", "Ciprofloxacina cloridrato", "J01MA02"),
    "027624": ("Fluimucil 600mg", "Acetilcisteina", "R05CB01"),
    "024916": ("Bisolvon 8mg", "Bromexina cloridrato", "R05CB02"),
    "028061": ("Ambroxolo 30mg", "Ambroxolo cloridrato", "R05CB06"),
    "033390": ("Mucosolvan 30mg", "Ambroxolo cloridrato", "R05CB06"),
    "027404": ("Xyzal 5mg", "Levocetirizina dicloridrato", "R06AE09"),
    "026857": ("Aerius 5mg", "Desloratadina", "R06AX27"),
    "023406": ("Clarityn 10mg", "Loratadina", "R06AX13"),
    "024531": ("Bentelan 0.5mg", "Betametasone", "H02AB01"),
    "022657": ("Deltacortene 25mg", "Prednisone", "H02AB07"),
    "022658": ("Deltacortene 5mg", "Prednisone", "H02AB07"),
    "030978": ("Singulair 10mg", "Montelukast sodico", "R03DC03"),
    "026906": ("Ventolin 100mcg", "Salbutamolo solfato", "R03AC02"),
    "031564": ("Symbicort 160/4.5mcg", "Budesonide + Formoterolo", "R03AK07"),
    "034381": ("Seretide 50/25mcg", "Salmeterolo + Fluticasone", "R03AK06"),
    "028597": ("Omacor 1000mg", "Omega-3", "C10AX06"),
    "029391": ("Maalox 400mg", "Idrossido di alluminio + magnesio", "A02AD01"),
    "023107": ("Plasil 10mg", "Metoclopramide cloridrato", "A03FA01"),
    "022783": ("Buscopan 10mg", "Butilscopolamina bromuro", "A03BB01"),
    "023185": ("Imodium 2mg", "Loperamide cloridrato", "A07DA03"),
    "027351": ("Normix 200mg", "Rifaximina", "A07AA11"),
    "025672": ("Lactulosio 66.7%", "Lattulosio", "A06AD11"),
    "034916": ("Flomax 0.4mg", "Tamsulosina cloridrato", "G04CA02"),
    "033988": ("Avodart 0.5mg", "Dutasteride", "G04CB02"),
    "031888": ("Xatral 10mg", "Alfuzosina cloridrato", "G04CA01"),
    "033514": ("Ezetimibe 10mg", "Ezetimibe", "C10AX09"),
    "038506": ("Rosuvastatina 10mg", "Rosuvastatina calcica", "C10AA07"),
    "038507": ("Rosuvastatina 20mg", "Rosuvastatina calcica", "C10AA07"),
    "030217": ("Crestor 20mg", "Rosuvastatina calcica", "C10AA07"),
    "036621": ("Jardiance 10mg", "Empagliflozin", "A10BK03"),
    "036622": ("Jardiance 25mg", "Empagliflozin", "A10BK03"),
    "036485": ("Forxiga 10mg", "Dapagliflozin propanediolo", "A10BK01"),
    "035979": ("Victoza 6mg/ml", "Liraglutide", "A10BJ02"),
    "038069": ("Ozempic 0.5mg", "Semaglutide", "A10BJ06"),
    "038070": ("Ozempic 1mg", "Semaglutide", "A10BJ06"),
    "034671": ("Eliquis 2.5mg", "Apixaban", "B01AF02"),
    "037441": ("Entresto 100mg", "Sacubitril + Valsartan", "C09DX04"),
    "037442": ("Entresto 200mg", "Sacubitril + Valsartan", "C09DX04"),
    "033060": ("Arcoxia 60mg", "Etoricoxib", "M01AH05"),
    "028476": ("Ceclor 500mg", "Cefacloro monoidrato", "J01DA06"),
    "024528": ("Rocefin 1g", "Ceftriaxone disodico", "J01DD04"),
    "022881": ("Ronaxan 100mg", "Doxiciclina cloridrato", "J01AA02"),
    "025040": ("Bactrim Forte", "Cotrimossazolo", "J01EE01"),
    "033416": ("Diflucan 150mg", "Fluconazolo", "J02AC01"),
    "031432": ("Sporanox 100mg", "Itraconazolo", "J02AC02"),
    "028344": ("Zovirax 200mg", "Aciclovir", "J05AB01"),
    "032611": ("Valtrex 500mg", "Valaciclovir cloridrato", "J05AB11"),
}


def init_tabella(conn):
    """Crea la tabella aifa_lookup se non esiste."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS aifa_lookup (
            aic               TEXT PRIMARY KEY,
            nome              TEXT NOT NULL,
            principio_attivo  TEXT,
            atc               TEXT,
            fonte             TEXT,
            aggiornato_il     TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    print("[AIFA-IMPORT] Tabella aifa_lookup pronta.")


def importa_dizionario_otc(conn):
    """Importa il dizionario hardcoded dei farmaci OTC/classe C."""
    count = 0
    for aic, (nome, pa, atc) in FARMACI_OTC.items():
        conn.execute("""
            INSERT OR REPLACE INTO aifa_lookup (aic, nome, principio_attivo, atc, fonte)
            VALUES (?, ?, ?, ?, 'hardcoded')
        """, (aic, nome, pa, atc))
        count += 1
    conn.commit()
    print(f"[AIFA-IMPORT] Dizionario OTC: {count} farmaci importati.")
    return count


def importa_csv_aifa(conn, source: dict) -> int:
    """Scarica e importa un CSV AIFA nel DB."""
    print(f"[AIFA-IMPORT] Scarico {source['nome']}...")
    try:
        resp = requests.get(
            source["url"],
            headers={"User-Agent": "FarmaciManager/1.0"},
            timeout=30
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[AIFA-IMPORT] Errore download {source['nome']}: {e}")
        return 0

    # Decodifica con gestione encoding italiano
    try:
        testo = resp.content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            testo = resp.content.decode("latin-1")
        except Exception:
            testo = resp.content.decode("utf-8", errors="replace")

    righe = testo.splitlines()
    skip = source.get("skip_righe", 0)
    righe = righe[skip:]

    if not righe:
        print(f"[AIFA-IMPORT] {source['nome']}: nessuna riga trovata.")
        return 0

    # Header
    header = [h.strip().strip('"') for h in righe[0].split(source["separatore"])]

    def col_idx(nome_col):
        if not nome_col:
            return None
        for i, h in enumerate(header):
            if nome_col.lower() in h.lower():
                return i
        return None

    idx_aic  = col_idx(source["col_aic"])
    idx_nome = col_idx(source["col_nome"])
    idx_pa   = col_idx(source.get("col_pa"))
    idx_atc  = col_idx(source.get("col_atc"))

    if idx_aic is None or idx_nome is None:
        print(f"[AIFA-IMPORT] {source['nome']}: colonne AIC/nome non trovate. Header: {header[:5]}")
        return 0

    count = 0
    for riga in righe[1:]:
        if not riga.strip():
            continue
        campi = [c.strip().strip('"') for c in riga.split(source["separatore"])]
        if len(campi) <= max(idx_aic, idx_nome):
            continue

        aic_raw = campi[idx_aic].strip()
        nome    = campi[idx_nome].strip()
        pa      = campi[idx_pa].strip() if idx_pa and idx_pa < len(campi) else None
        atc     = campi[idx_atc].strip() if idx_atc and idx_atc < len(campi) else None

        # Normalizza AIC: rimuovi tutto tranne cifre, prendi prime 6
        aic_clean = re.sub(r"[^0-9]", "", aic_raw)
        if len(aic_clean) >= 6:
            aic_6 = aic_clean[:6]
        else:
            continue

        if not nome or len(nome) < 2:
            continue

        # Non sovrascrivere farmaci OTC hardcoded (hanno fonte='hardcoded')
        existing = conn.execute(
            "SELECT fonte FROM aifa_lookup WHERE aic=?", (aic_6,)
        ).fetchone()
        if existing and existing[0] == "hardcoded":
            continue

        conn.execute("""
            INSERT OR REPLACE INTO aifa_lookup (aic, nome, principio_attivo, atc, fonte)
            VALUES (?, ?, ?, ?, ?)
        """, (aic_6, nome, pa, atc, source["nome"]))
        count += 1

    conn.commit()
    print(f"[AIFA-IMPORT] {source['nome']}: {count} farmaci importati.")
    return count


def _scarica_csv_in_memoria(source: dict):
    """
    Scarica il CSV AIFA e prepara i dati in memoria (lista di tuple).
    NON tocca il DB — così il download lento non blocca SQLite.
    Ritorna: (lista_record, messaggio_errore_o_None)
    """
    print(f"[AIFA-IMPORT] Scarico {source['nome']} in memoria...")
    try:
        resp = requests.get(
            source["url"],
            headers={"User-Agent": "FarmaciManager/1.0"},
            timeout=45
        )
        resp.raise_for_status()
    except Exception as e:
        return [], f"Errore download {source['nome']}: {e}"

    try:
        testo = resp.content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            testo = resp.content.decode("latin-1")
        except Exception:
            testo = resp.content.decode("utf-8", errors="replace")

    righe = testo.splitlines()
    skip  = source.get("skip_righe", 0)
    righe = righe[skip:]

    import csv as _csv
    reader = _csv.reader(righe, delimiter=source["separatore"])
    header = None
    records = []

    for i, row in enumerate(reader):
        if i == 0:
            header = [c.strip() for c in row]
            continue
        if not row or not any(row):
            continue
        try:
            idx_aic  = header.index(source["col_aic"])
            idx_nome = header.index(source["col_nome"])
            idx_pa   = header.index(source["col_pa"]) if source.get("col_pa") and source["col_pa"] in header else -1
            idx_atc  = header.index(source["col_atc"]) if source.get("col_atc") and source["col_atc"] in header else -1

            aic  = re.sub(r"[^0-9]", "", row[idx_aic].strip())
            nome = row[idx_nome].strip()[:200] if len(row) > idx_nome else ""
            pa   = row[idx_pa].strip()[:200]   if idx_pa >= 0 and len(row) > idx_pa else None
            atc  = row[idx_atc].strip()[:20]   if idx_atc >= 0 and len(row) > idx_atc else None

            if len(aic) >= 6 and nome:
                aic_6 = aic[:6] if len(aic) >= 6 else aic
                records.append((aic_6, nome, pa, atc, source["nome"]))
        except (ValueError, IndexError):
            continue

    print(f"[AIFA-IMPORT] {source['nome']}: {len(records)} record pronti in memoria")
    return records, None


def _inserisci_batch(records: list, fonte: str) -> int:
    """
    Apre il DB, fa INSERT batch velocissimo, chiude subito.
    Connessione aperta < 1 secondo anche per 10.000 record.
    """
    if not records:
        return 0
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executemany("""
            INSERT OR REPLACE INTO aifa_lookup
                (aic, nome, principio_attivo, atc, fonte, aggiornato_il)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
        """, records)
        conn.commit()
        n = conn.execute(
            "SELECT COUNT(*) FROM aifa_lookup WHERE fonte=?", (fonte,)
        ).fetchone()[0]
        conn.close()
        print(f"[AIFA-IMPORT] {fonte}: {len(records)} inseriti, totale fonte: {n}")
        return len(records)
    except Exception as e:
        print(f"[AIFA-IMPORT] Errore INSERT {fonte}: {e}")
        try:
            conn.close()
        except Exception:
            pass
        return 0


def aggiorna_aifa_scheduler(notifica_mgr=None, chat_ids=None):
    """
    Funzione chiamata dallo scheduler mensile.
    Pattern sicuro anti-deadlock:
      1. Scarica TUTTI i CSV in memoria (download lento, DB mai toccato)
      2. Apre DB → INSERT batch → chiude subito (< 1 secondo per sorgente)
    Se AIFA risponde 403 manda notifica Telegram al caregiver.
    """
    import threading
    print(f"[AIFA-IMPORT] ── Avvio aggiornamento mensile ──")

    errori = []
    totale = 0

    for source in CSV_SOURCES:
        # FASE 1: download in memoria — DB mai aperto
        records, errore = _scarica_csv_in_memoria(source)

        if errore:
            errori.append(f"• {source['nome']}: {errore}")
            print(f"[AIFA-IMPORT] SKIP {source['nome']}: {errore}")
            continue

        # FASE 2: INSERT batch veloce — connessione aperta < 1s
        n = _inserisci_batch(records, source["nome"])
        totale += n

    # Statistiche finali (connessione rapida, solo lettura)
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        tot_db = conn.execute("SELECT COUNT(*) FROM aifa_lookup").fetchone()[0]
        conn.close()
        print(f"[AIFA-IMPORT] ✅ Aggiornamento completato — {tot_db} farmaci in DB")
    except Exception:
        tot_db = totale

    # Notifica Telegram al caregiver
    if notifica_mgr and chat_ids:
        if errori:
            msg = (
                f"⚠️ <b>Aggiornamento AIFA parziale</b>\n\n"
                f"✅ Farmaci aggiornati: {totale}\n"
                f"❌ Sorgenti non scaricate:\n" + "\n".join(errori) + "\n\n"
                f"Puoi aggiornarle manualmente con:\n"
                f"<code>curl -X POST https://farmaci.mabalu.it/api/aifa/upload-csv "
                f"-F file=@classe_a.csv -F tipo=classe_a</code>"
            )
        else:
            msg = (
                f"✅ <b>DB Farmaci aggiornato</b>\n\n"
                f"📦 Farmaci nel database: <b>{tot_db}</b>\n"
                f"🗓️ Prossimo aggiornamento: tra 30 giorni"
            )
        for cid in chat_ids:
            notifica_mgr.invia_telegram(cid, msg)

    return totale


def main():
    skip_download = "--skip-download" in sys.argv

    # Init tabella con connessione rapida
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    init_tabella(conn)

    # Importa dizionario OTC hardcoded (velocissimo, in memoria)
    totale = importa_dizionario_otc(conn)
    conn.close()

    if not skip_download:
        # Usa il pattern sicuro: download in memoria → INSERT batch
        for source in CSV_SOURCES:
            records, errore = _scarica_csv_in_memoria(source)
            if not errore:
                totale += _inserisci_batch(records, source["nome"])
            else:
                print(f"[AIFA-IMPORT] SKIP: {errore}")
    else:
        print("[AIFA-IMPORT] Skip download CSV (--skip-download).")

    # Statistiche finali
    conn = sqlite3.connect(DB_PATH, timeout=5)
    tot_db = conn.execute("SELECT COUNT(*) as n FROM aifa_lookup").fetchone()[0]
    esempi = conn.execute(
        "SELECT aic, nome, principio_attivo FROM aifa_lookup LIMIT 5"
    ).fetchall()
    conn.close()

    print(f"\n[AIFA-IMPORT] ✅ Completato! Totale in DB: {tot_db} farmaci")
    for e in esempi:
        print(f"  AIC {e[0]}: {e[1]} ({e[2]})")


if __name__ == "__main__":
    main()
