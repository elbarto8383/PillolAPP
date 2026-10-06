# Changelog

## 2.1.2
- Fix: il login funziona anche con la pagina incorporata (iframe) in una dashboard di Home Assistant su https.
- Aggiunti icona, README, documentazione e changelog dell'add-on.

## 2.1.1
- Fix: l'add-on si installa di nuovo con le versioni recenti del Supervisor (`BUILD_FROM` di default nel Dockerfile).
- Architetture supportate: amd64 e aarch64.

## 2.1.0
- Sicurezza: API protette da login e permessi, webhook Telegram autenticato, password con PBKDF2, limite ai tentativi di login, nessuna password o chiave di default nel codice.
- Promemoria più affidabili: pianificazione che non perde i follow-up, dati sempre aggiornati dal database, recupero dopo il riavvio, fallback al caregiver.
- Scorte: avviso al superamento della soglia, scarico una sola volta per assunzione.
- Correzioni a database ("database is locked"), ricerca AIC, validazione dati, astuccio (anno ISO), aderenza su Home Assistant, import AIFA e foto confezioni persistenti.
