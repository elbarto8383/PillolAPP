import sqlite3
import os

DB_PATH = os.environ.get("DB_PATH", "/config/farmaci.db")


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")   # permette letture/scritture concorrenti
    conn.execute("PRAGMA busy_timeout = 10000") # aspetta fino a 10s se DB occupato
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    # ------------------------------------------------------------------
    # PAZIENTI
    # profilo: "autosufficiente" | "assistito"
    #   autosufficiente → vede la PWA completa (gestione + conferma)
    #   assistito       → vede solo la schermata conferma assunzione
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS pazienti (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            nome              TEXT NOT NULL,
            cognome           TEXT NOT NULL,
            data_nascita      TEXT,
            profilo           TEXT NOT NULL DEFAULT 'assistito',
            telegram_chat_id  TEXT,
            note_medico       TEXT,
            attivo            INTEGER NOT NULL DEFAULT 1,
            creato_il         TEXT NOT NULL DEFAULT (datetime('now')),
            aggiornato_il     TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # ------------------------------------------------------------------
    # FARMACI  (anagrafica, popolata via OCR AIC + AIFA)
    # immagine_url    → URL remoto (Open Products Facts o scattata da utente)
    # immagine_locale → path locale sul server dopo download/upload
    # colore_avatar   → colore hex generato deterministicamente (fallback)
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS farmaci (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            aic               TEXT UNIQUE,
            nome              TEXT NOT NULL,
            nome_commerciale  TEXT,
            principio_attivo  TEXT,
            forma_farmaceutica TEXT,
            dosaggio          TEXT,
            atc               TEXT,
            produttore        TEXT,
            foglietto_url     TEXT,
            immagine_url      TEXT,
            immagine_locale   TEXT,
            colore_avatar     TEXT,
            creato_il         TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # Migrazione su DB esistente: aggiunge colonne se mancanti
    for col, tipo in [
        ("immagine_url", "TEXT"),
        ("immagine_locale", "TEXT"),
        ("colore_avatar", "TEXT"),
    ]:
        try:
            c.execute(f"ALTER TABLE farmaci ADD COLUMN {col} {tipo}")
        except Exception:
            pass  # colonna già presente

    # ------------------------------------------------------------------
    # SCORTE  (per paziente: quantità confezione + soglia minima)
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS scorte (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            paziente_id       INTEGER NOT NULL REFERENCES pazienti(id),
            farmaco_id        INTEGER NOT NULL REFERENCES farmaci(id),
            quantita          REAL NOT NULL DEFAULT 0,
            unita             TEXT NOT NULL DEFAULT 'compresse',
            soglia_minima     REAL NOT NULL DEFAULT 7,
            scadenza          TEXT,
            lotto             TEXT,
            aggiornato_il     TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(paziente_id, farmaco_id)
        )
    """)

    # ------------------------------------------------------------------
    # TERAPIE  (piano terapeutico per paziente+farmaco)
    # orari: JSON array  es. ["08:00","12:00","20:00"]
    # giorni_settimana: JSON array 0-6 (0=lun) o null = tutti i giorni
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS terapie (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            paziente_id       INTEGER NOT NULL REFERENCES pazienti(id),
            farmaco_id        INTEGER NOT NULL REFERENCES farmaci(id),
            dose              TEXT NOT NULL,
            orari             TEXT NOT NULL,
            giorni_settimana  TEXT,
            durata_giorni     INTEGER,
            data_inizio       TEXT NOT NULL DEFAULT (date('now')),
            data_fine         TEXT,
            note              TEXT,
            attiva            INTEGER NOT NULL DEFAULT 1,
            creato_il         TEXT NOT NULL DEFAULT (datetime('now')),
            aggiornato_il     TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # ------------------------------------------------------------------
    # ASSUNZIONI  (log di ogni evento notifica → risposta)
    # esito: 'SI' | 'NO' | 'SKIP' | 'PENDENTE'
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS assunzioni (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            terapia_id        INTEGER NOT NULL REFERENCES terapie(id),
            orario_previsto   TEXT NOT NULL,
            orario_risposta   TEXT,
            esito             TEXT NOT NULL DEFAULT 'PENDENTE',
            tentativo         INTEGER NOT NULL DEFAULT 1,
            canale            TEXT,
            note              TEXT,
            creato_il         TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # ------------------------------------------------------------------
    # ASTUCCIO_SLOT  (stato fisico settimanale del porta-pillole)
    # giorno: 0-6 (0=lun), fascia: 'M'|'P'|'S'|'N'
    # farmaci_ids: JSON array di farmaco_id
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS astuccio_slot (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            paziente_id       INTEGER NOT NULL REFERENCES pazienti(id),
            settimana_iso     TEXT NOT NULL,
            giorno            INTEGER NOT NULL,
            fascia            TEXT NOT NULL,
            farmaci_ids       TEXT NOT NULL DEFAULT '[]',
            caricato          INTEGER NOT NULL DEFAULT 0,
            caricato_il       TEXT,
            UNIQUE(paziente_id, settimana_iso, giorno, fascia)
        )
    """)

    # ------------------------------------------------------------------
    # CACHE_AIC  (evita di ricontattare AIFA ogni volta)
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS cache_aic (
            aic               TEXT PRIMARY KEY,
            payload_json      TEXT NOT NULL,
            creato_il         TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # ------------------------------------------------------------------
    # UTENTI  (autenticazione)
    # ruolo: "caregiver" | "paziente"
    # paziente_id: solo per ruolo paziente, collega all'anagrafica
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS utenti (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT NOT NULL UNIQUE,
            password    TEXT NOT NULL,
            ruolo       TEXT NOT NULL DEFAULT 'paziente',
            paziente_id INTEGER REFERENCES pazienti(id),
            attivo      INTEGER NOT NULL DEFAULT 1,
            creato_il   TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    conn.commit()
    conn.close()
    print("[DB] Database inizializzato correttamente.")


if __name__ == "__main__":
    init_db()
