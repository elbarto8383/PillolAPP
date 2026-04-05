# 💊 PillolApp — Home Assistant Add-on

<div align="center">

**Smart medication management for chronic patients**

[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-Add--on-41BDF5?logo=home-assistant&logoColor=white)](https://www.home-assistant.io/)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-3.0-000000?logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-2CA5E0?logo=telegram&logoColor=white)](https://core.telegram.org/bots)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

🇮🇹 [Leggi in italiano](README.it.md)

---

### ❤️ If PillolApp is useful to you, consider supporting its development

[![Donate PayPal](https://img.shields.io/badge/Donate-PayPal-00457C?logo=paypal&logoColor=white&style=for-the-badge)](https://paypal.me/elbarto83)

*Every contribution helps keep the project alive and fund new features.*

---

</div>

## 📋 Description

**PillolApp** is a Home Assistant OS add-on that turns your smart home into a personal medical assistant. Designed for people managing chronic medication therapies — for themselves or a family member — PillolApp sends timely reminders via Telegram and Alexa, monitors stock levels, and alerts the caregiver when needed.

### 🎯 Usage modes

| Mode | Description | Best for |
|---|---|---|
| **`solo`** | Single user — you are both patient and caregiver | People managing their own therapy independently |
| **`famiglia`** | Caregiver separate from one or more patients | People assisting a family member |

---

## ✨ Features

### 💊 Medication Management
- Search medications by **AIC code** with local AIFA database (2,400+ drugs)
- **Integrated OCR scanner** — point the camera at the A.I.C. label on the box
- Online AIFA lookup for medications not in the local database
- Automatic monthly database update

### 📅 Therapies
- Schedule therapies with multiple daily times and day-of-week selection
- Support for fixed-duration or ongoing therapies
- Weekly pill organizer with daily time slots (Morning / Afternoon / Evening / Night)

### 🔔 Smart Notifications
- **Telegram Bot** — reminders with inline ✅ YES / ❌ NO buttons
- **Alexa TTS** — voice announcement at medication time
- **Retry system** — after N unanswered attempts, caregiver is alerted
- **Family mode**: notifications only to the patient, caregiver alerted only when needed
- **Solo mode**: everything goes to the single user

### 📦 Stock Management
- Track remaining quantities per medication/patient
- Automatic low-stock alert
- Telegram notification to caregiver

### 🔐 Authentication
- Separate login for caregiver and patients
- Caregiver sees the full dashboard
- Each patient sees only their own page with today's therapies
- Protected access with Flask-Login and server-side sessions

### 🏠 Home Assistant Integration
- HA sensors for active therapies, expiring medications, low stock
- Compatible with HA automations and dashboards

---

## 🚀 Installation

### Prerequisites
- Home Assistant OS or Supervised
- Telegram Bot (create one with [@BotFather](https://t.me/BotFather))
- Public URL for the Telegram webhook (e.g. Nginx + Certbot)

### 1. Add the repository to Home Assistant

1. Go to **Settings → Add-ons → Store**
2. Click **⋮ → Repositories**
3. Add this URL:
   ```
   https://github.com/elbarto8383/PillolAPP
   ```
4. Click **Add** → **Close**

### 2. Install PillolApp

1. Find **PillolApp** in the add-on store (scroll down or search)
2. Click **Install** → **Rebuild**

> **Alternative — manual installation:**
> ```bash
> mkdir -p /addons/pillolapp
> cp -r pillolapp/* /addons/pillolapp/
> ```
> Then go to **Settings → Add-ons → Store → ⋮ → Check for updates**

### 3. Configure

| Field | Description | Example |
|---|---|---|
| `ha_url` | Internal Home Assistant URL | `http://192.168.1.83:8123` |
| `ha_token` | Long-lived access token | `eyJ0...` |
| `public_url` | Public URL for Telegram webhook | `https://pillolapp.yourdomain.com` |
| `telegram_bot_token` | Telegram bot token | `123456:ABC...` |
| `telegram_chat_ids` | Caregiver chat ID (for alerts) | `44413116` |
| `alexa_abilitata` | Enable Alexa announcements | `true` |
| `alexa_entity_id` | Echo device entity ID | `media_player.echo_kitchen` |
| `notifica_ritardo_minuti` | Minutes between retry attempts | `15` |
| `notifica_max_tentativi` | Maximum number of attempts | `3` |
| `modalita_utilizzo` | `solo` or `famiglia` | `famiglia` |
| `caregiver_password` | Caregiver login password | `YourPassword!` |
| `secret_key` | Flask session secret key | `long-random-string` |

### 4. Nginx Configuration

```nginx
server {
    listen 443 ssl;
    server_name pillolapp.yourdomain.com;
    ssl_certificate     /etc/letsencrypt/live/pillolapp.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/pillolapp.yourdomain.com/privkey.pem;

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

## 🤖 Telegram Bot Setup

1. Open [@BotFather](https://t.me/BotFather) on Telegram
2. Create a new bot with `/newbot`
3. Copy the token and add it to the configuration
4. Each patient sends `/start` to the bot to receive their personal **Chat ID**
5. The caregiver enters the Chat ID in the patient profile

**Available commands:**
- `/start` — registration and Chat ID display
- `/stato` — today's therapies

---

## 📊 How notifications work

```
Therapy scheduled
    │
    ├─► Telegram → patient only ("Time to take Eutirox...")
    └─► Alexa TTS → voice announcement

Patient replies ✅ YES
    └─► Recorded silently — caregiver not disturbed

Patient does not reply × 3
    ├─► Telegram → caregiver alert
    └─► Alexa TTS → caregiver voice alert
```

---

## 🏗️ Architecture

```
PillolApp
├── app.py              # Flask backend — REST API + authentication
├── database.py         # SQLite schema + initialization
├── notifiche.py        # Telegram bot + Alexa TTS
├── scheduler.py        # APScheduler — daily notifications + monthly AIFA update
├── aifa.py             # AIC lookup — local DB → AIFA online → fallback
├── aifa_import.py      # AIFA CSV import (deadlock-safe: download in memory)
├── run.sh              # HA add-on entrypoint
├── Dockerfile          # Alpine + Python + Tesseract OCR ITA
├── config.yaml         # HA add-on configuration schema
├── requirements.txt    # Python dependencies
├── static/
│   ├── css/style.css   # Mobile-first design system
│   └── js/
│       ├── farmaco-avatar.js   # Drug SVG avatars
│       └── farmaco-scanner.js  # OCR scanner
└── templates/
    ├── index.html          # Caregiver dashboard
    ├── login.html          # Login page
    ├── home_paziente.html  # Patient home with today's therapies
    ├── gestione.html       # Self-sufficient patient PWA
    └── conferma.html       # Assisted patient confirmation screen
```

### SQLite Database

| Table | Description |
|---|---|
| `pazienti` | Patient profiles with Telegram chat_id |
| `farmaci` | Medication catalog with AIC, active ingredient, ATC |
| `terapie` | Active therapies with times, days, dosage |
| `scorte` | Remaining stock per patient/medication |
| `assunzioni` | YES/NO/PENDING log per notification |
| `astuccio_slot` | Weekly plan — 7 days × 4 time slots |
| `aifa_lookup` | Local AIFA medication database (2,400+ entries) |
| `utenti` | Caregiver and patient login credentials |

---

## 🔄 AIFA Database Updates

The database updates automatically on the **1st of every month at 03:00**.
If AIFA blocks the download (403), you receive a Telegram notification with manual upload instructions:

```bash
curl -X POST https://pillolapp.yourdomain.com/api/aifa/upload-csv \
  -F "file=@classe_a.csv" -F "tipo=classe_a"

curl -X POST https://pillolapp.yourdomain.com/api/aifa/upload-csv \
  -F "file=@classe_h.csv" -F "tipo=classe_h"
```

---

## 🛠️ Tech Stack

- **Backend**: Flask 3.0, SQLite (WAL mode), APScheduler
- **Frontend**: HTML5, CSS3, Vanilla JS — mobile-first PWA
- **OCR**: Server-side Tesseract ITA (pytesseract + Pillow)
- **Notifications**: python-telegram-bot, Alexa Media Player (HA)
- **Auth**: Flask-Login with server-side sessions
- **Container**: Alpine Linux, Python 3.11

---

## 🗺️ Roadmap

- [ ] Installable mobile PWA
- [ ] Multi-language support
- [ ] PDF export of medication reports
- [ ] Browser push notifications
- [ ] Advanced statistics dashboard
- [ ] HACS integration

---

## 🤝 Contributing

Pull requests are welcome! For major changes, please open an issue first to discuss what you would like to change.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## 📄 License

Distributed under the MIT License. See [LICENSE](LICENSE) for more information.

---

## ❤️ Support the project

PillolApp is developed and maintained in my free time. If you find it useful:

<div align="center">

[![Donate PayPal](https://img.shields.io/badge/Donate%20with-PayPal-00457C?logo=paypal&logoColor=white&style=for-the-badge)](https://paypal.me/elbarto83)

**Thank you for your support! 🙏**

</div>

---

<div align="center">
Made with ❤️ to simplify life for people managing chronic therapies
</div>
