#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run.sh — Entrypoint add-on Home Assistant
# /data  → cartella privata persistente dell'add-on (sempre scrivibile)
# /config → cartella config HA (richiede map: config:rw in config.yaml)
# ─────────────────────────────────────────────────────────────────────────────
set -e

CONFIG_PATH=/data/options.json

if [ ! -f "$CONFIG_PATH" ]; then
    echo "[run.sh] ERRORE: $CONFIG_PATH non trovato."
    exit 1
fi

echo "[run.sh] Lettura configurazione..."

export HA_URL=$(jq --raw-output '.ha_url // "http://homeassistant.local:8123"' $CONFIG_PATH)
export HA_TOKEN=$(jq --raw-output '.ha_token // ""' $CONFIG_PATH)
export PUBLIC_URL=$(jq --raw-output '.public_url // ""' $CONFIG_PATH)
export TELEGRAM_BOT_TOKEN=$(jq --raw-output '.telegram_bot_token // ""' $CONFIG_PATH)
export TELEGRAM_CHAT_IDS=$(jq --raw-output '.telegram_chat_ids // [] | join(",")' $CONFIG_PATH)
export ALEXA_ABILITATA=$(jq --raw-output '.alexa_abilitata // true' $CONFIG_PATH)
export ALEXA_ENTITY_ID=$(jq --raw-output '.alexa_entity_id // "media_player.alexa"' $CONFIG_PATH)
export NOTIFICA_RITARDO_MIN=$(jq --raw-output '.notifica_ritardo_minuti // 15' $CONFIG_PATH)
export NOTIFICA_MAX_TENTATIVI=$(jq --raw-output '.notifica_max_tentativi // 3' $CONFIG_PATH)

# /data è la cartella privata persistente dell'add-on — sempre scrivibile
export CAREGIVER_PASSWORD=$(jq --raw-output '.caregiver_password // "PillolApp2026!"' $CONFIG_PATH)
export SECRET_KEY=$(jq --raw-output '.secret_key // "mabalu-pillolapp-secret-2026-bart"' $CONFIG_PATH)
export MODALITA_UTILIZZO=$(jq --raw-output '.modalita_utilizzo // "famiglia"' $CONFIG_PATH)
export DB_PATH="/data/farmaci.db"

echo "[run.sh] Configurazione:"
echo "  HA_URL             = $HA_URL"
echo "  PUBLIC_URL         = $PUBLIC_URL"
echo "  MODALITA           = $MODALITA_UTILIZZO"
echo "  ALEXA_ABILITATA    = $ALEXA_ABILITATA"
echo "  ALEXA_ENTITY_ID    = $ALEXA_ENTITY_ID"
echo "  DB_PATH            = $DB_PATH"
echo "  RITARDO NOTIFICA   = ${NOTIFICA_RITARDO_MIN} min"
echo "  MAX TENTATIVI      = $NOTIFICA_MAX_TENTATIVI"

# Crea directory immagini farmaci
mkdir -p /app/static/img/farmaci

echo "[run.sh] Inizializzazione database farmaci locali..."
DB_PATH=/data/farmaci.db python3 /app/aifa_import.py --skip-download || echo "[run.sh] Import OTC completato (o già presente)"

echo "[run.sh] Avvio PillolApp sulla porta 5001..."
exec python3 /app/app.py
