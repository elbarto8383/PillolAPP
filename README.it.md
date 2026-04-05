# 💊 PillolApp — Home Assistant Add-on

<div align="center">

**Gestione intelligente delle terapie farmacologiche per pazienti cronici**

[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-Add--on-41BDF5?logo=home-assistant&logoColor=white)](https://www.home-assistant.io/)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-3.0-000000?logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-2CA5E0?logo=telegram&logoColor=white)](https://core.telegram.org/bots)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
🇬🇧 [Read in English](README.md)


---

### ❤️ Se PillolApp ti è utile, considera una donazione per supportare lo sviluppo

[![Donate PayPal](https://img.shields.io/badge/Donate-PayPal-00457C?logo=paypal&logoColor=white&style=for-the-badge)](https://paypal.me/elbarto83)

*Ogni contributo aiuta a mantenere il progetto attivo e a sviluppare nuove funzionalità.*

---

</div>

## 📋 Descrizione

**PillolApp** è un add-on per Home Assistant OS che trasforma il tuo smart home in un assistente medico personale. Progettato per chi gestisce terapie farmacologiche croniche — proprie o di un familiare — PillolApp invia promemoria puntuali via Telegram e Alexa, monitora le scorte e allerta il caregiver quando necessario.

### 🎯 Modalità di utilizzo

| Modalità | Descrizione | Ideale per |
|---|---|---|
| **`solo`** | Utente unico — sei tu stesso paziente e caregiver | Chi gestisce autonomamente la propria terapia |
| **`famiglia`** | Caregiver separato da uno o più pazienti | Chi assiste un familiare non autosufficiente |

---

## ✨ Funzionalità

### 💊 Gestione Farmaci
- Ricerca farmaci per **codice AIC** con database AIFA locale (2.400+ farmaci)
- **Scanner OCR** integrato — inquadra la scritta A.I.C. sulla scatola con la fotocamera
- Lookup online AIFA per farmaci non presenti nel database locale
- Aggiornamento automatico del database AIFA ogni mese

### 📅 Terapie
- Pianificazione terapie con orari multipli e giorni della settimana selezionabili
- Supporto durata terapia (giorni) o terapia continuativa
- Astuccio settimanale con visualizzazione fasce giornaliere (Mattina/Pomeriggio/Sera/Notte)

### 🔔 Notifiche Intelligenti
- **Telegram Bot** — promemoria con bottoni ✅ SÌ / ❌ NO
- **Alexa TTS** — annuncio vocale al momento dell'assunzione
- Sistema a **tentativi** — dopo N tentativi senza risposta, alert al caregiver
- **Modalità famiglia**: notifiche solo al paziente, alert caregiver solo se necessario
- **Modalità solo**: tutto all'utente unico

### 📦 Scorte
- Monitoraggio quantità rimanenti per ogni farmaco
- Alert automatico quando la scorta scende sotto la soglia
- Notifica Telegram al caregiver

### 🔐 Autenticazione
- Login separato per caregiver e pazienti
- Il caregiver vede la dashboard completa
- Ogni paziente vede solo la propria pagina con le terapie del giorno
- Accesso protetto con Flask-Login e sessioni

### 🏠 Integrazione Home Assistant
- Sensori HA per terapie attive, farmaci in scadenza, scorte basse
- Compatibile con automazioni e dashboard HA

---

## 🚀 Installazione

### Prerequisiti
- Home Assistant OS o Supervised
- Telegram Bot (crealo con [@BotFather](https://t.me/BotFather))
- URL pubblico per il webhook Telegram (es. con Nginx + Certbot)

### 1. Copia i file

```bash
mkdir -p /addons/pillolapp
cp -r pillolapp/* /addons/pillolapp/
```

### 2. Installa da Home Assistant

1. Vai in **Impostazioni → Add-on → Store**
2. Clicca **⋮ → Controlla aggiornamenti**
3. Cerca **PillolApp** tra gli add-on locali
4. Clicca **Installa** → **Ricostruisci**

### 3. Configura

| Campo | Descrizione | Esempio |
|---|---|---|
| `ha_url` | URL interno di Home Assistant | `http://192.168.1.83:8123` |
| `ha_token` | Long-lived access token HA | `eyJ0...` |
| `public_url` | URL pubblico per il webhook Telegram | `https://farmaci.tuodominio.it` |
| `telegram_bot_token` | Token del bot Telegram | `123456:ABC...` |
| `telegram_chat_ids` | Chat ID del caregiver (per alert) | `44413116` |
| `alexa_abilitata` | Abilita annunci Alexa | `true` |
| `alexa_entity_id` | Entity ID dell'Echo | `media_player.echo_cucina` |
| `notifica_ritardo_minuti` | Minuti tra un tentativo e l'altro | `15` |
| `notifica_max_tentativi` | Numero massimo di tentativi | `3` |
| `modalita_utilizzo` | `solo` o `famiglia` | `famiglia` |
| `caregiver_password` | Password accesso caregiver | `LatuaPassword!` |
| `secret_key` | Chiave segreta sessioni Flask | `stringa-casuale-lunga` |

### 4. Configura Nginx

```nginx
server {
    listen 443 ssl;
    server_name farmaci.tuodominio.it;
    ssl_certificate     /etc/letsencrypt/live/farmaci.tuodominio.it/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/farmaci.tuodominio.it/privkey.pem;

    add_header Content-Security-Policy "
        default-src 'self';
        script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdnjs.cloudflare.com;
        style-src 'self' 'unsafe-inline';
        img-src 'self' data: https: blob:;
        connect-src 'self' https:;
        font-src 'self' data:;
        worker-src 'self' blob:;
        frame-ancestors 'none';
    " always;

    location / {
        proxy_pass         http://192.168.1.83:5001;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

---

## 🤖 Configurazione Bot Telegram

1. Apri [@BotFather](https://t.me/BotFather) su Telegram
2. Crea un nuovo bot con `/newbot`
3. Copia il token e inseriscilo nella configurazione
4. Ogni paziente manda `/start` al bot per ricevere il proprio **Chat ID**
5. Il caregiver inserisce il Chat ID nella scheda anagrafica del paziente

**Comandi bot:**
- `/start` — registrazione e ricezione Chat ID
- `/stato` — terapie di oggi

---

## 🏗️ Architettura

```
PillolApp
├── app.py              # Flask backend — API REST + autenticazione
├── database.py         # Schema SQLite + inizializzazione
├── notifiche.py        # Telegram bot + Alexa TTS
├── scheduler.py        # APScheduler — notifiche + update AIFA mensile
├── aifa.py             # Lookup AIC — DB locale → AIFA online → fallback
├── aifa_import.py      # Import CSV AIFA (anti-deadlock)
├── run.sh              # Entrypoint add-on HA
├── Dockerfile          # Alpine + Python + Tesseract OCR ITA
├── config.yaml         # Schema configurazione add-on HA
├── requirements.txt    # Dipendenze Python
├── static/
│   ├── css/style.css
│   └── js/
│       ├── farmaco-avatar.js
│       └── farmaco-scanner.js
└── templates/
    ├── index.html          # Dashboard caregiver
    ├── login.html          # Pagina login
    ├── home_paziente.html  # Home paziente
    ├── gestione.html       # PWA paziente autosufficiente
    └── conferma.html       # Schermata paziente assistito
```

### Database SQLite

| Tabella | Descrizione |
|---|---|
| `pazienti` | Anagrafica pazienti |
| `farmaci` | Catalogo farmaci |
| `terapie` | Terapie attive con orari e giorni |
| `scorte` | Quantità rimanenti per paziente/farmaco |
| `assunzioni` | Log SI/NO/PENDENTE |
| `astuccio_slot` | Piano settimanale 7×4 fasce |
| `aifa_lookup` | Database locale farmaci AIFA |
| `utenti` | Credenziali caregiver e pazienti |

---

## 🔄 Aggiornamento database AIFA

Il database si aggiorna automaticamente ogni **1° del mese alle 03:00**.
Se AIFA blocca il download (403), ricevi notifica Telegram con istruzioni per upload manuale:

```bash
curl -X POST https://farmaci.tuodominio.it/api/aifa/upload-csv \
  -F "file=@classe_a.csv" -F "tipo=classe_a"
```

---

## 🛠️ Stack tecnico

- **Backend**: Flask 3.0, SQLite WAL, APScheduler
- **Frontend**: HTML5, CSS3, JS vanilla — PWA mobile-first
- **OCR**: Tesseract ITA lato server (pytesseract + Pillow)
- **Notifiche**: python-telegram-bot, Alexa Media Player HA
- **Auth**: Flask-Login
- **Container**: Alpine Linux, Python 3.11

---

## 🗺️ Roadmap

- [ ] PWA installabile su mobile
- [ ] Supporto multi-lingua
- [ ] Export PDF report assunzioni
- [ ] Notifiche push browser
- [ ] Dashboard statistiche avanzate

---

## 🤝 Contribuire

Pull request benvenute! Per modifiche importanti apri prima una issue.

1. Fork del repository
2. Crea un branch (`git checkout -b feature/nuova-funzionalita`)
3. Commit (`git commit -m 'Aggiunge nuova funzionalità'`)
4. Push (`git push origin feature/nuova-funzionalita`)
5. Apri una Pull Request

---

## 📄 Licenza

Distribuito sotto licenza MIT. Vedi [LICENSE](LICENSE) per maggiori informazioni.

---

## ❤️ Supporta il progetto

PillolApp è sviluppato e mantenuto nel tempo libero. Se lo trovi utile:

<div align="center">

[![Donate PayPal](https://img.shields.io/badge/Dona%20con-PayPal-00457C?logo=paypal&logoColor=white&style=for-the-badge)](https://paypal.me/elbarto83)

**Grazie di cuore per il supporto! 🙏**

</div>

---

<div align="center">
Fatto con ❤️ per semplificare la vita a chi gestisce terapie croniche
</div>
