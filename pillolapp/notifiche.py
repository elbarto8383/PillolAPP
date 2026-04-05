import requests
import json


class NotificaManager:
    def __init__(self, telegram_token, chat_ids, ha_url, ha_token,
                 alexa_entity, alexa_abilitata=True):
        self.telegram_token  = telegram_token
        self.chat_ids        = chat_ids
        self.ha_url          = ha_url
        self.ha_token        = ha_token
        self.alexa_entity    = alexa_entity
        self.alexa_abilitata = alexa_abilitata

    # ── Telegram ────────────────────────────────────────────────────────────

    def invia_telegram(self, chat_id, testo, inline_keyboard=None):
        if not self.telegram_token:
            print("[TELEGRAM] Token non configurato.")
            return
        payload = {"chat_id": chat_id, "text": testo, "parse_mode": "HTML"}
        if inline_keyboard:
            payload["reply_markup"] = json.dumps({"inline_keyboard": inline_keyboard})
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                json=payload, timeout=10
            )
            r.raise_for_status()
        except Exception as e:
            print(f"[TELEGRAM] Errore invio a {chat_id}: {e}")

    def notifica_assunzione(self, paziente, farmaco_nome, dose, orario, assunzione_id):
        """Notifica SOLO al paziente (suo chat_id personale)."""
        testo = (
            f"💊 <b>Promemoria farmaco</b>\n\n"
            f"👤 {paziente['nome']} {paziente['cognome']}\n"
            f"🔹 <b>{farmaco_nome}</b> — {dose}\n"
            f"🕐 Orario: <b>{orario}</b>\n\n"
            f"Hai preso il farmaco?"
        )
        keyboard = [[
            {"text": "✅ SÌ, ho preso", "callback_data": f"SI_{assunzione_id}"},
            {"text": "❌ Non ancora",   "callback_data": f"NO_{assunzione_id}"},
        ]]
        chat_id = paziente.get("telegram_chat_id")
        if chat_id:
            # Notifica SOLO al paziente — il caregiver non vede questo messaggio
            self.invia_telegram(chat_id, testo, keyboard)
        else:
            # Paziente senza chat_id — notifica al caregiver come fallback
            print(f"[TELEGRAM] Paziente {paziente['nome']} senza chat_id, fallback caregiver")
            for cid in self.chat_ids:
                self.invia_telegram(cid, testo, keyboard)

    def alert_caregiver(self, paziente, farmaco_nome, orario, tentativi):
        """Alert SOLO al caregiver dopo max_tentativi senza risposta."""
        testo = (
            f"⚠️ <b>ATTENZIONE — Farmaco non confermato</b>\n\n"
            f"👤 {paziente['nome']} {paziente['cognome']}\n"
            f"💊 <b>{farmaco_nome}</b>\n"
            f"🕐 Orario previsto: {orario}\n"
            f"🔁 Tentativi effettuati: {tentativi}\n\n"
            f"Il paziente non ha risposto. Verificare di persona."
        )
        # Alert SOLO ai chat_id del caregiver — mai al paziente
        for cid in self.chat_ids:
            self.invia_telegram(cid, testo)

    def alert_rifiuto_caregiver(self, paziente, farmaco_nome, orario):
        """Alert al caregiver quando il paziente preme NO."""
        testo = (
            f"❌ <b>Farmaco rifiutato</b>\n\n"
            f"👤 {paziente['nome']} {paziente['cognome']}\n"
            f"💊 <b>{farmaco_nome}</b>\n"
            f"🕐 Orario: {orario}\n\n"
            f"Il paziente ha indicato di non aver preso il farmaco."
        )
        for cid in self.chat_ids:
            self.invia_telegram(cid, testo)

    def notifica_scorta_bassa(self, paziente, farmaco_nome, quantita_rimasta):
        testo = (
            f"📦 <b>Scorta in esaurimento</b>\n\n"
            f"👤 {paziente['nome']} {paziente['cognome']}\n"
            f"💊 <b>{farmaco_nome}</b>\n"
            f"📉 Rimanenti: <b>{quantita_rimasta}</b> unità\n\n"
            f"Ricordarsi di rinnovare la prescrizione."
        )
        for cid in self.chat_ids:
            self.invia_telegram(cid, testo)

    # ── Alexa TTS ───────────────────────────────────────────────────────────

    def parla_alexa(self, testo):
        if not self.alexa_abilitata:
            print("[ALEXA] Disabilitata dalla configurazione.")
            return
        if not self.ha_token or not self.alexa_entity:
            return
        headers = {
            "Authorization": f"Bearer {self.ha_token}",
            "Content-Type": "application/json"
        }
        try:
            requests.post(
                f"{self.ha_url}/api/services/notify/alexa_media",
                headers=headers,
                json={
                    "message": testo,
                    "data": {"type": "announce"},
                    "target": [self.alexa_entity]
                },
                timeout=5
            )
        except Exception as e:
            print(f"[ALEXA] Errore TTS: {e}")

    def notifica_completa(self, paziente, farmaco_nome, dose, orario, assunzione_id, modalita="famiglia"):
        """
        Notifica Telegram + Alexa.
        Modalità solo: notifica a se stesso (chat_ids globali).
        Modalità famiglia: notifica solo al paziente.
        """
        self.notifica_assunzione(paziente, farmaco_nome, dose, orario, assunzione_id, modalita)
        testo_alexa = (
            f"{paziente['nome']}, è ora di prendere {farmaco_nome}, {dose}. "
            f"Conferma sull'app o su Telegram."
        )
        self.parla_alexa(testo_alexa)

    def notifica_assunzione(self, paziente, farmaco_nome, dose, orario, assunzione_id, modalita="famiglia"):
        """
        Modalità solo: notifica ai chat_ids globali (l'utente è sia paziente che caregiver).
        Modalità famiglia: notifica SOLO al chat_id personale del paziente.
        """
        testo = (
            f"💊 <b>Promemoria farmaco</b>\n\n"
            f"👤 {paziente['nome']} {paziente['cognome']}\n"
            f"🔹 <b>{farmaco_nome}</b> — {dose}\n"
            f"🕐 Orario: <b>{orario}</b>\n\n"
            f"Hai preso il farmaco?"
        )
        keyboard = [[
            {"text": "✅ SÌ, ho preso", "callback_data": f"SI_{assunzione_id}"},
            {"text": "❌ Non ancora",   "callback_data": f"NO_{assunzione_id}"},
        ]]

        if modalita == "solo":
            # Utente unico — notifica ai chat_ids globali (è lui stesso)
            for cid in self.chat_ids:
                self.invia_telegram(cid, testo, keyboard)
        else:
            # Modalità famiglia — solo al chat_id del paziente
            chat_id = paziente.get("telegram_chat_id")
            if chat_id:
                self.invia_telegram(chat_id, testo, keyboard)
            else:
                print(f"[TELEGRAM] Paziente {paziente['nome']} senza chat_id, fallback caregiver")
                for cid in self.chat_ids:
                    self.invia_telegram(cid, testo, keyboard)

    def alert_caregiver_completo(self, paziente, farmaco_nome, orario, tentativi, modalita="famiglia"):
        """
        Alert quando il paziente non risponde dopo max tentativi.
        Modalità solo: Telegram + Alexa allo stesso utente.
        Modalità famiglia: Telegram + Alexa al caregiver.
        """
        if modalita == "solo":
            testo = (
                f"⚠️ <b>Promemoria mancato</b>\n\n"
                f"💊 <b>{farmaco_nome}</b>\n"
                f"🕐 Orario: {orario}\n"
                f"🔁 Tentativi: {tentativi}\n\n"
                f"Non hai ancora confermato l'assunzione."
            )
        else:
            testo = (
                f"⚠️ <b>ATTENZIONE — Farmaco non confermato</b>\n\n"
                f"👤 {paziente['nome']} {paziente['cognome']}\n"
                f"💊 <b>{farmaco_nome}</b>\n"
                f"🕐 Orario: {orario}\n"
                f"🔁 Tentativi: {tentativi}\n\n"
                f"Il paziente non ha risposto. Verificare di persona."
            )
        for cid in self.chat_ids:
            self.invia_telegram(cid, testo)

        # Alexa — avvisa vocalmente
        if modalita == "solo":
            testo_alexa = (
                f"Attenzione! Non hai ancora confermato "
                f"l'assunzione di {farmaco_nome} prevista alle {orario}."
            )
        else:
            testo_alexa = (
                f"Attenzione! {paziente['nome']} non ha confermato "
                f"l'assunzione di {farmaco_nome} prevista alle {orario}. "
                f"Verificare di persona."
            )
        self.parla_alexa(testo_alexa)
