import json
import datetime
import threading
import traceback

from apscheduler.schedulers.background import BackgroundScheduler

from database import get_db

_scheduler = None
_lock = threading.RLock()

# Configurazione corrente: salvata qui così chi ripianifica (es. dopo la modifica di
# una terapia) non deve ripassare tutti i parametri e non rischia di perderne alcuni.
_cfg = {
    "notifica_mgr": None,
    "ritardo_min": 15,
    "max_tentativi": 3,
    "chat_ids": [],
    "modalita": "famiglia",
}

# Un promemoria "in ritardo" (es. riavvio dell'add-on a pochi minuti dall'orario)
# viene comunque inviato entro questo margine, invece di essere scartato.
MISFIRE_GRACE_S = 600


# ═══════════════════════════════════════════════════════════════════════════
# AVVIO
# ═══════════════════════════════════════════════════════════════════════════

def avvia_scheduler(notifica_mgr, ritardo_min=15, max_tentativi=3, chat_ids=None, modalita="famiglia"):
    """Avvia (o riavvia) lo scheduler. Da chiamare UNA volta all'avvio dell'app."""
    global _scheduler
    with _lock:
        if _scheduler and _scheduler.running:
            _scheduler.shutdown(wait=False)

        _cfg.update({
            "notifica_mgr": notifica_mgr,
            "ritardo_min": ritardo_min,
            "max_tentativi": max_tentativi,
            "chat_ids": list(chat_ids or []),
            "modalita": modalita,
        })

        _scheduler = BackgroundScheduler(
            timezone="Europe/Rome",
            job_defaults={"misfire_grace_time": MISFIRE_GRACE_S, "coalesce": True},
        )

        _scheduler.add_job(
            func=ripianifica_oggi,
            trigger="cron",
            hour=0, minute=1,
            id="pianifica_giornata",
            replace_existing=True
        )

        _scheduler.add_job(
            func=_aggiorna_aifa_safe,
            trigger="cron",
            day=1, hour=3, minute=0,
            id="aggiorna_aifa_mensile",
            replace_existing=True
        )

        _scheduler.start()
        ripianifica_oggi()
        _ripristina_followup()
        print(f"[SCHEDULER] Avviato. Modalità: {modalita}")


def _aggiorna_aifa_safe():
    """
    Wrapper per l'aggiornamento AIFA — gira in thread APScheduler separato.
    Delega tutto ad aifa_import.aggiorna_aifa_scheduler() che usa il pattern
    sicuro: download in memoria → INSERT batch veloce → connessione DB chiusa subito.
    Mai tiene il DB aperto durante il download (che può durare minuti).
    """
    notifica_mgr = _cfg["notifica_mgr"]
    chat_ids = _cfg["chat_ids"]
    print(f"[SCHEDULER] Avvio aggiornamento mensile AIFA — "
          f"{datetime.datetime.now().strftime('%d/%m/%Y %H:%M')}")
    try:
        from aifa_import import aggiorna_aifa_scheduler
        aggiorna_aifa_scheduler(notifica_mgr=notifica_mgr, chat_ids=chat_ids)
    except Exception as e:
        print(f"[SCHEDULER] Errore aggiornamento AIFA: {e}")
        # Notifica caregiver anche in caso di errore imprevisto
        if notifica_mgr and chat_ids:
            for cid in chat_ids:
                notifica_mgr.invia_telegram(
                    cid,
                    f"❌ <b>Aggiornamento AIFA fallito</b>\n\n"
                    f"Errore: {str(e)[:200]}\n\n"
                    f"Controlla i log dell'add-on per i dettagli."
                )


# ═══════════════════════════════════════════════════════════════════════════
# PIANIFICAZIONE
# ═══════════════════════════════════════════════════════════════════════════

def _parse_orario(orario):
    """'08:30' → (8, 30). Ritorna None se il formato non è valido."""
    try:
        ora, minuto = str(orario).split(":")[:2]
        ora, minuto = int(ora), int(minuto)
        if 0 <= ora <= 23 and 0 <= minuto <= 59:
            return ora, minuto
    except (ValueError, TypeError):
        pass
    return None


def _json_list(valore, default):
    """Legge una colonna JSON (orari / giorni_settimana) senza mai sollevare eccezioni."""
    if valore in (None, ""):
        return default
    try:
        dati = json.loads(valore) if isinstance(valore, str) else valore
        return dati if isinstance(dati, list) else default
    except (ValueError, TypeError):
        return default


def _terapia_attiva_oggi(row, oggi):
    """True se la terapia è in corso oggi (periodo valido e giorno della settimana previsto)."""
    if row["data_inizio"] and str(row["data_inizio"]) > str(oggi):
        return False
    if row["data_fine"] and str(row["data_fine"]) < str(oggi):
        return False
    giorni = _json_list(row["giorni_settimana"], list(range(7)))
    return oggi.weekday() in giorni


def _carica_terapia(terapia_id, oggi=None):
    """
    Rilegge la terapia dal DB nel momento in cui serve (non una copia vecchia):
    così chat_id, dose, nome farmaco e stato attivo sono sempre aggiornati.
    Ritorna None se la terapia (o il paziente) non è più attiva oggi.
    """
    oggi = oggi or datetime.date.today()
    db = get_db()
    try:
        row = db.execute("""
            SELECT t.*, p.nome, p.cognome, p.telegram_chat_id, p.profilo,
                   f.nome AS farmaco_nome
            FROM terapie t
            JOIN pazienti p ON t.paziente_id = p.id
            JOIN farmaci  f ON t.farmaco_id  = f.id
            WHERE t.id = ? AND t.attiva = 1 AND p.attivo = 1
        """, (terapia_id,)).fetchone()
    finally:
        db.close()
    if not row or not _terapia_attiva_oggi(row, oggi):
        return None
    return dict(row)


def _slot_confermato(terapia_id, orario_previsto):
    """True se QUALSIASI tentativo di questo slot (terapia + orario) è già stato confermato."""
    db = get_db()
    try:
        r = db.execute("""
            SELECT 1 FROM assunzioni
            WHERE terapia_id = ? AND orario_previsto = ? AND esito IN ('SI', 'SKIP')
            LIMIT 1
        """, (terapia_id, orario_previsto)).fetchone()
    finally:
        db.close()
    return r is not None


def _slot_gia_notificato(terapia_id, orario_previsto):
    db = get_db()
    try:
        r = db.execute("""
            SELECT 1 FROM assunzioni WHERE terapia_id = ? AND orario_previsto = ? LIMIT 1
        """, (terapia_id, orario_previsto)).fetchone()
    finally:
        db.close()
    return r is not None


def ripianifica_oggi():
    """
    Allinea i job di oggi alle terapie presenti nel DB. È IDEMPOTENTE e non tocca
    i follow-up già in corso, quindi si può chiamare in sicurezza dopo ogni modifica:
      • aggiunge i job per orari nuovi o non ancora pianificati
      • rimuove i job di terapie disattivate / orari tolti / pazienti disattivati
      • non rinvia promemoria già inviati (controllo sul log assunzioni)
    """
    with _lock:
        if not _scheduler or not _scheduler.running:
            return
        oggi = datetime.date.today()
        adesso = datetime.datetime.now()
        suffisso = f"_{oggi}"

        db = get_db()
        try:
            terapie = db.execute("""
                SELECT t.id, t.orari, t.giorni_settimana, t.data_inizio, t.data_fine
                FROM terapie t
                JOIN pazienti p ON t.paziente_id = p.id
                JOIN farmaci  f ON t.farmaco_id  = f.id
                WHERE t.attiva = 1 AND p.attivo = 1
            """).fetchall()
        finally:
            db.close()

        desiderati = {}
        for t in terapie:
            if not _terapia_attiva_oggi(t, oggi):
                continue
            for orario in _json_list(t["orari"], []):
                hm = _parse_orario(orario)
                if not hm:
                    print(f"[SCHEDULER] Orario non valido ignorato (terapia {t['id']}): {orario!r}")
                    continue
                quando = datetime.datetime.combine(oggi, datetime.time(*hm))
                desiderati[f"terapia_{t['id']}_{orario}{suffisso}"] = (t["id"], orario, quando)

        # Rimuove i job di oggi che non servono più
        for job in _scheduler.get_jobs():
            if job.id.startswith("terapia_") and job.id.endswith(suffisso) and job.id not in desiderati:
                job.remove()

        # Aggiunge quelli mancanti
        nuovi = 0
        for job_id, (tid, orario, quando) in desiderati.items():
            if quando < adesso - datetime.timedelta(seconds=MISFIRE_GRACE_S):
                continue  # orario già passato oggi
            if _scheduler.get_job(job_id):
                continue  # già pianificato
            if _slot_gia_notificato(tid, f"{oggi} {orario}"):
                continue  # già inviato
            _scheduler.add_job(
                func=_esegui_notifica,
                trigger="date",
                run_date=quando,
                args=[tid, orario],
                id=job_id,
                replace_existing=True
            )
            nuovi += 1

        print(f"[SCHEDULER] Pianificazione per {oggi} aggiornata ({nuovi} nuovi promemoria).")


def _ripristina_followup():
    """
    Dopo un riavvio dell'add-on i follow-up (rinotifica / alert al caregiver) vivevano solo
    in memoria e andavano persi: qui li ricostruiamo dagli slot di oggi ancora senza conferma.
    """
    oggi = datetime.date.today()
    db = get_db()
    try:
        righe = db.execute("""
            SELECT a.id, a.terapia_id, a.orario_previsto, a.tentativo, a.creato_il
            FROM assunzioni a
            WHERE a.orario_previsto LIKE ?
              AND a.note IS NOT 'alert_caregiver'
              AND a.id = (SELECT MAX(b.id) FROM assunzioni b
                          WHERE b.terapia_id = a.terapia_id AND b.orario_previsto = a.orario_previsto)
              AND NOT EXISTS (SELECT 1 FROM assunzioni c
                              WHERE c.terapia_id = a.terapia_id AND c.orario_previsto = a.orario_previsto
                                AND c.esito IN ('SI', 'SKIP'))
        """, (f"{oggi} %",)).fetchall()
    finally:
        db.close()

    ritardo = _cfg["ritardo_min"]
    ripristinati = 0
    for r in righe:
        if not _carica_terapia(r["terapia_id"], oggi):
            continue
        try:
            creato = datetime.datetime.strptime(r["creato_il"], "%Y-%m-%d %H:%M:%S")
            trascorsi = (datetime.datetime.utcnow() - creato).total_seconds()
        except (ValueError, TypeError):
            trascorsi = 0
        attesa = max(30, ritardo * 60 - trascorsi)
        orario = r["orario_previsto"].split(" ")[-1]
        _pianifica_followup(r["id"], r["terapia_id"], orario, r["tentativo"], attesa_s=attesa)
        ripristinati += 1
    if ripristinati:
        print(f"[SCHEDULER] Ripristinati {ripristinati} follow-up dopo il riavvio.")


# ═══════════════════════════════════════════════════════════════════════════
# ESECUZIONE
# ═══════════════════════════════════════════════════════════════════════════

def _registra_tentativo(terapia_id, orario, tentativo):
    db = get_db()
    try:
        c = db.execute("""
            INSERT INTO assunzioni (terapia_id, orario_previsto, esito, tentativo)
            VALUES (?, ?, 'PENDENTE', ?)
        """, (terapia_id, f"{datetime.date.today()} {orario}", tentativo))
        db.commit()
        return c.lastrowid
    finally:
        db.close()


def _paziente_da_terapia(t):
    return {
        "nome": t["nome"],
        "cognome": t["cognome"],
        "telegram_chat_id": t["telegram_chat_id"],
        "profilo": t["profilo"],
    }


def _invia(t, orario, assunzione_id):
    try:
        _cfg["notifica_mgr"].notifica_completa(
            _paziente_da_terapia(t), t["farmaco_nome"], t["dose"], orario,
            assunzione_id, _cfg["modalita"]
        )
    except Exception:
        # Un invio fallito non deve impedire i follow-up successivi
        print(f"[SCHEDULER] Errore invio promemoria:\n{traceback.format_exc()}")


def _esegui_notifica(terapia_id, orario):
    oggi = datetime.date.today()
    t = _carica_terapia(terapia_id, oggi)
    if not t:
        return  # terapia disattivata o modificata dopo la pianificazione
    if orario not in _json_list(t["orari"], []):
        return  # orario rimosso dalla terapia
    if _slot_gia_notificato(terapia_id, f"{oggi} {orario}"):
        return  # evita doppioni (es. dopo un riavvio)

    assunzione_id = _registra_tentativo(terapia_id, orario, 1)
    _invia(t, orario, assunzione_id)
    _pianifica_followup(assunzione_id, terapia_id, orario, 1)


def _pianifica_followup(assunzione_id, terapia_id, orario, tentativo, attesa_s=None):
    if not _scheduler or not _scheduler.running:
        return
    if attesa_s is None:
        attesa_s = _cfg["ritardo_min"] * 60
    quando = datetime.datetime.now() + datetime.timedelta(seconds=attesa_s)

    if tentativo >= _cfg["max_tentativi"]:
        _scheduler.add_job(
            func=_alert_caregiver_finale,
            trigger="date",
            run_date=quando,
            args=[assunzione_id, terapia_id, orario, tentativo],
            id=f"alert_{assunzione_id}_{tentativo}",
            replace_existing=True
        )
        return

    _scheduler.add_job(
        func=_rinotifica,
        trigger="date",
        run_date=quando,
        args=[assunzione_id, terapia_id, orario, tentativo],
        id=f"followup_{assunzione_id}_{tentativo}",
        replace_existing=True
    )


def _rinotifica(assunzione_id, terapia_id, orario, tentativo):
    oggi = datetime.date.today()
    if _slot_confermato(terapia_id, f"{oggi} {orario}"):
        return  # confermato (anche da un tentativo precedente): basta promemoria
    t = _carica_terapia(terapia_id, oggi)
    if not t:
        return

    nuovo_tentativo = tentativo + 1
    nuovo_id = _registra_tentativo(terapia_id, orario, nuovo_tentativo)
    _invia(t, orario, nuovo_id)
    _pianifica_followup(nuovo_id, terapia_id, orario, nuovo_tentativo)


def _alert_caregiver_finale(assunzione_id, terapia_id, orario, tentativo):
    oggi = datetime.date.today()
    if _slot_confermato(terapia_id, f"{oggi} {orario}"):
        return
    t = _carica_terapia(terapia_id, oggi)
    if not t:
        return

    # Segna l'alert come inviato (evita di ripeterlo se l'add-on si riavvia)
    db = get_db()
    try:
        db.execute("UPDATE assunzioni SET note = 'alert_caregiver' WHERE id = ?", (assunzione_id,))
        db.commit()
    finally:
        db.close()

    try:
        _cfg["notifica_mgr"].alert_caregiver_completo(
            _paziente_da_terapia(t), t["farmaco_nome"], orario, tentativo, _cfg["modalita"]
        )
    except Exception:
        print(f"[SCHEDULER] Errore invio alert caregiver:\n{traceback.format_exc()}")
