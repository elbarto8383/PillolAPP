import json
from html import escape as _e

import requests


class NotificaManager:
    def __init__(self, telegram_token, chat_ids, ha_url, ha_token,
                 alexa_entity, alexa_abilitata=True):
        self.telegram_token  = telegram_token
        self.chat_ids        = chat_ids
        self.ha_url          = (ha_url or "").rstrip("/")
        self.ha_token        = ha_token
        self.alexa_entity    = alexa_entity
        self.alexa_abilitata = alexa_abilitata

    # ── Telegram ────────────────────────────────────────────────────────────

    def invia_telegram(self, chat_id, testo, inline_keyboard=None):
        """Invia un messaggio Telegram. Ritorna True se consegnato, False altrimenti."""
        if not self.telegram_token:
            print("[TELEGRAM] Token non configurato.")
            return False
        if chat_id in (None, ""):
            return False
        payload = {"chat_id": str(chat_id).strip(), "text": testo, "parse_mode": "HTML"}
        if inline_keyboard:
            payload["reply_markup"] = json.dumps({"inline_keyboard": inline_keyboard})
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                json=payload, timeout=10
            )
            if r.status_code >= 400:
                # Il corpo della risposta spiega il motivo (es. "chat not found",
                # "bot was blocked by the user", "can't parse entities")
                print(f"[TELEGRAM] Errore invio a {chat_id}: HTTP {r.status_code} — {r.text[:200]}")
                return False
            return True
        except Exception as e:
            print(f"[TELEGRAM] Errore invio a {chat_id}: {e}")
            return False

    def alert_rifiuto_caregiver(self, paziente, farmaco_nome, orario):
        """Alert al caregiver quando il paziente preme NO."""
        testo = (
            f"❌ <b>Farmaco rifiutato</b>\n\n"
            f"👤 {_e(paziente['nome'])} {_e(paziente['cognome'])}\n"
            f"💊 <b>{_e(farmaco_nome)}</b>\n"
            f"🕐 Orario: {_e(str(orario))}\n\n"
            f"Il paziente ha indicato di non aver preso il farmaco."
        )
        for cid in self.chat_ids:
            self.invia_telegram(cid, testo)

    def notifica_scorta_bassa(self, paziente, farmaco_nome, quantita_rimasta, unita="unità"):
        """Avviso ai caregiver quando la scorta scende sotto la soglia minima."""
        if isinstance(quantita_rimasta, float) and quantita_rimasta.is_integer():
            quantita_rimasta = int(quantita_rimasta)
        testo = (
            f"📦 <b>Scorta in esaurimento</b>\n\n"
            f"👤 {_e(paziente['nome'])} {_e(paziente['cognome'])}\n"
            f"💊 <b>{_e(farmaco_nome)}</b>\n"
            f"📉 Rimanenti: <b>{_e(str(quantita_rimasta))}</b> {_e(str(unita))}\n\n"
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
            r = requests.post(
                f"{self.ha_url}/api/services/notify/alexa_media",
                headers=headers,
                json={
                    "message": testo,
                    "data": {"type": "announce"},
                    "target": [self.alexa_entity]
                },
                timeout=5
            )
            if r.status_code >= 400:
                print(f"[ALEXA] Errore TTS: HTTP {r.status_code} — {r.text[:150]}")
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
        Modalità famiglia: notifica SOLO al chat_id personale del paziente. Se il paziente
        non ha un chat_id, o Telegram non riesce a consegnare (es. non ha mai premuto /start
        sul bot), il promemoria viene recapitato al caregiver così non va perso.
        """
        testo = (
            f"💊 <b>Promemoria farmaco</b>\n\n"
            f"👤 {_e(paziente['nome'])} {_e(paziente['cognome'])}\n"
            f"🔹 <b>{_e(farmaco_nome)}</b> — {_e(dose)}\n"
            f"🕐 Orario: <b>{_e(str(orario))}</b>\n\n"
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
            return

        # Modalità famiglia — solo al chat_id del paziente
        chat_id = paziente.get("telegram_chat_id")
        consegnato = bool(chat_id) and self.invia_telegram(chat_id, testo, keyboard)
        if consegnato:
            return

        if chat_id:
            motivo = "il messaggio non è stato consegnato (il paziente deve aprire il bot e premere /start)"
        else:
            motivo = "il paziente non ha un Chat ID Telegram configurato"
        print(f"[TELEGRAM] Paziente {paziente['nome']}: {motivo} — fallback caregiver")
        testo_fb = f"⚠️ <i>Promemoria inoltrato a te perché {_e(motivo)}.</i>\n\n" + testo
        for cid in self.chat_ids:
            self.invia_telegram(cid, testo_fb, keyboard)

    def alert_caregiver_completo(self, paziente, farmaco_nome, orario, tentativi, modalita="famiglia"):
        """
        Alert quando il paziente non risponde dopo max tentativi.
        Modalità solo: Telegram + Alexa allo stesso utente.
        Modalità famiglia: Telegram + Alexa al caregiver.
        """
        if modalita == "solo":
            testo = (
                f"⚠️ <b>Promemoria mancato</b>\n\n"
                f"💊 <b>{_e(farmaco_nome)}</b>\n"
                f"🕐 Orario: {_e(str(orario))}\n"
                f"🔁 Tentativi: {tentativi}\n\n"
                f"Non hai ancora confermato l'assunzione."
            )
        else:
            testo = (
                f"⚠️ <b>ATTENZIONE — Farmaco non confermato</b>\n\n"
                f"👤 {_e(paziente['nome'])} {_e(paziente['cognome'])}\n"
                f"💊 <b>{_e(farmaco_nome)}</b>\n"
                f"🕐 Orario: {_e(str(orario))}\n"
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
