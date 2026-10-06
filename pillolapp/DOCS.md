# PillolAPP — Documentazione

## Primo avvio

1. Compila la configurazione (vedi sotto) e avvia l'add-on.
2. Apri l'interfaccia su `http://<IP-di-HA>:5001`.
3. Accedi con utente **`caregiver`** e la password impostata in `caregiver_password`.
   - Se hai lasciato `caregiver_password` vuota, la password iniziale viene **generata e scritta nel log dell'add-on** (riga `[AUTH]`): cambiala subito dalla dashboard (🔑).
4. Crea i pazienti e, in modalità `famiglia`, gli accessi dei pazienti.

La password viene applicata solo alla **creazione** dell'utente `caregiver`: se la cambi in configurazione dopo il primo avvio, usa il pulsante 🔑 della dashboard.

## Configurazione

| Opzione | Descrizione |
|---|---|
| `modalita_utilizzo` | `solo` = sei paziente e caregiver · `famiglia` = caregiver separato |
| `ha_url` | URL di Home Assistant (es. `http://192.168.1.83:8123`) |
| `ha_token` | Token di accesso a lunga durata (serve per Alexa e per i sensori) |
| `public_url` | URL pubblico **https** per il webhook Telegram (vuoto = webhook non registrato) |
| `telegram_bot_token` | Token del bot, da [@BotFather](https://t.me/BotFather) |
| `telegram_chat_ids` | Chat ID dei caregiver: ricevono alert, scorte basse e promemoria di riserva |
| `alexa_abilitata` / `alexa_entity_id` | Annunci vocali tramite Alexa Media Player |
| `notifica_ritardo_minuti` | Minuti tra un promemoria e il successivo (default 15) |
| `notifica_max_tentativi` | Promemoria prima dell'alert al caregiver (default 3) |
| `caregiver_password` | Password iniziale dell'utente `caregiver` (vuota = generata e scritta nel log) |
| `secret_key` | Chiave per firmare i cookie di sessione. **Lascia vuoto**: viene generata e conservata automaticamente. Se la imposti usa almeno 32 caratteri casuali; cambiarla disconnette tutti |

## Telegram

1. Crea il bot con `/newbot` su @BotFather e inserisci il token.
2. Ogni paziente deve aprire il bot e premere **/start**: il bot risponde con il suo **Chat ID**, da inserire nella scheda del paziente.
3. Per i pulsanti ✅/❌ serve il webhook: imposta `public_url` con un indirizzo **https** raggiungibile da Internet.
4. `telegram_chat_ids` = Chat ID dei caregiver. In modalità `solo` ricevono anche i promemoria.

Se un paziente non ha un Chat ID (o non ha mai premuto /start), il promemoria viene inoltrato al caregiver, con una nota che spiega il motivo.

## Reverse proxy (nginx)

```nginx
location / {
    proxy_pass         http://IP-DI-HA:5001;
    proxy_set_header   Host              $host;
    proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;   # necessario per i cookie in https
    proxy_read_timeout 60s;
}
```

### Pagina dentro una dashboard di Home Assistant (iframe)

- Su https l'app emette i cookie come `SameSite=None; Secure; Partitioned`, così il login funziona anche dentro un iframe su un altro sito: serve l'header `X-Forwarded-Proto` qui sopra.
- Nel Content-Security-Policy di nginx **non** usare `frame-ancestors 'none'`: indica l'origine di Home Assistant, es. `frame-ancestors 'self' http://192.168.1.83:8123;`. Non impostare nemmeno `X-Frame-Options: DENY`.

## Dati e aggiornamenti

- Database e foto delle confezioni sono in `/data` dell'add-on: **sono inclusi nei backup di Home Assistant** e restano agli aggiornamenti. Disinstallando l'add-on con l'opzione di eliminare i dati vengono cancellati.
- Il dizionario dei farmaci locale si arricchisce con i CSV AIFA: dalla dashboard puoi avviare l'import o caricare a mano un CSV.

## Sensori creati in Home Assistant

`sensor.farmaci_pazienti_attivi`, `sensor.farmaci_scorte_basse`, `sensor.farmaci_scorte_in_scadenza`, `sensor.farmaci_terapie_attive`, `sensor.farmaci_aderenza_oggi`.

## Problemi comuni

| Sintomo | Causa probabile |
|---|---|
| Il login non regge dentro la dashboard di HA | Manca `X-Forwarded-Proto` in nginx, oppure la pagina è incorporata via `http` da un altro sito |
| Nessun promemoria su Telegram al paziente | Il paziente non ha premuto /start o il Chat ID non è nella sua scheda |
| I pulsanti ✅/❌ non rispondono | Webhook non registrato: controlla `public_url` (https) e il log |
| "Troppi tentativi falliti" al login | Blocco temporaneo di 5 minuti dopo 5 password errate |
