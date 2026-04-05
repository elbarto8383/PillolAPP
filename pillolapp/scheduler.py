import json
import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from database import get_db

_scheduler = None


def avvia_scheduler(notifica_mgr, ritardo_min=15, max_tentativi=3, chat_ids=None, modalita="famiglia"):
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)

    _scheduler = BackgroundScheduler(timezone="Europe/Rome")

    _scheduler.add_job(
        func=lambda: _pianifica_oggi(notifica_mgr, ritardo_min, max_tentativi, modalita),
        trigger="cron",
        hour=0, minute=1,
        id="pianifica_giornata",
        replace_existing=True
    )

    _scheduler.add_job(
        func=lambda: _aggiorna_aifa_safe(notifica_mgr, chat_ids),
        trigger="cron",
        day=1, hour=3, minute=0,
        id="aggiorna_aifa_mensile",
        replace_existing=True
    )

    _pianifica_oggi(notifica_mgr, ritardo_min, max_tentativi, modalita)
    _scheduler.start()
    print(f"[SCHEDULER] Avviato. Modalità: {modalita}")


def _aggiorna_aifa_safe(notifica_mgr, chat_ids):
    """
    Wrapper per l'aggiornamento AIFA — gira in thread APScheduler separato.
    Delega tutto ad aifa_import.aggiorna_aifa_scheduler() che usa il pattern
    sicuro: download in memoria → INSERT batch veloce → connessione DB chiusa subito.
    Mai tiene il DB aperto durante il download (che può durare minuti).
    """
    import threading
    print(f"[SCHEDULER] Avvio aggiornamento mensile AIFA — "
          f"{datetime.datetime.now().strftime('%d/%m/%Y %H:%M')}")
    try:
        from aifa_import import aggiorna_aifa_scheduler
        # chat_ids può essere None se il caregiver non ha configurato Telegram
        ids = chat_ids or []
        aggiorna_aifa_scheduler(notifica_mgr=notifica_mgr, chat_ids=ids)
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


def _pianifica_oggi(notifica_mgr, ritardo_min, max_tentativi, modalita="famiglia"):
    """Legge tutte le terapie attive e pianifica i job di oggi."""
    global _scheduler
    oggi = datetime.date.today()
    giorno_settimana = oggi.weekday()  # 0=lun … 6=dom

    db = get_db()
    terapie = db.execute("""
        SELECT t.*, p.nome, p.cognome, p.telegram_chat_id, p.profilo,
               f.nome as farmaco_nome
        FROM terapie t
        JOIN pazienti p ON t.paziente_id = p.id
        JOIN farmaci  f ON t.farmaco_id  = f.id
        WHERE t.attiva=1 AND p.attivo=1
          AND (t.data_fine IS NULL OR t.data_fine >= ?)
    """, (str(oggi),)).fetchall()
    db.close()

    for t in terapie:
        t = dict(t)
        orari = json.loads(t["orari"])
        giorni = json.loads(t["giorni_settimana"]) if t["giorni_settimana"] else list(range(7))

        if giorno_settimana not in giorni:
            continue

        for orario in orari:
            ora, minuto = map(int, orario.split(":"))
            scheduled_time = datetime.datetime.combine(oggi, datetime.time(ora, minuto))

            if scheduled_time < datetime.datetime.now():
                continue  # orario già passato oggi

            job_id = f"terapia_{t['id']}_{orario}_{oggi}"
            _scheduler.add_job(
                func=_esegui_notifica,
                trigger="date",
                run_date=scheduled_time,
                args=[t, orario, notifica_mgr, ritardo_min, max_tentativi, modalita],
                id=job_id,
                replace_existing=True
            )

    print(f"[SCHEDULER] Pianificazione per {oggi} completata.")


def _esegui_notifica(terapia, orario, notifica_mgr, ritardo_min, max_tentativi, modalita="famiglia"):
    db = get_db()
    c = db.execute("""
        INSERT INTO assunzioni (terapia_id, orario_previsto, esito, tentativo)
        VALUES (?, ?, 'PENDENTE', 1)
    """, (terapia["id"], f"{datetime.date.today()} {orario}"))
    db.commit()
    assunzione_id = c.lastrowid
    db.close()

    paziente = {
        "nome": terapia["nome"],
        "cognome": terapia["cognome"],
        "telegram_chat_id": terapia["telegram_chat_id"],
        "profilo": terapia["profilo"],
    }
    notifica_mgr.notifica_completa(
        paziente, terapia["farmaco_nome"], terapia["dose"], orario, assunzione_id, modalita
    )
    _pianifica_followup(assunzione_id, terapia, orario, notifica_mgr, ritardo_min, max_tentativi, 1, modalita)


def _pianifica_followup(assunzione_id, terapia, orario, notifica_mgr, ritardo_min, max_tentativi, tentativo, modalita="famiglia"):
    global _scheduler
    if tentativo >= max_tentativi:
        _scheduler.add_job(
            func=_alert_caregiver_finale,
            trigger="date",
            run_date=datetime.datetime.now() + datetime.timedelta(minutes=ritardo_min),
            args=[assunzione_id, terapia, orario, notifica_mgr, tentativo, modalita],
            id=f"alert_{assunzione_id}_{tentativo}",
            replace_existing=True
        )
        return

    _scheduler.add_job(
        func=_rinotifica,
        trigger="date",
        run_date=datetime.datetime.now() + datetime.timedelta(minutes=ritardo_min),
        args=[assunzione_id, terapia, orario, notifica_mgr, ritardo_min, max_tentativi, tentativo, modalita],
        id=f"followup_{assunzione_id}_{tentativo}",
        replace_existing=True
    )


def _rinotifica(assunzione_id, terapia, orario, notifica_mgr, ritardo_min, max_tentativi, tentativo, modalita="famiglia"):
    db = get_db()
    ass = db.execute("SELECT esito FROM assunzioni WHERE id=?", (assunzione_id,)).fetchone()
    db.close()
    if not ass or ass["esito"] == "SI":
        return

    nuovo_tentativo = tentativo + 1
    db = get_db()
    db.execute("""
        INSERT INTO assunzioni (terapia_id, orario_previsto, esito, tentativo)
        VALUES (?, ?, 'PENDENTE', ?)
    """, (terapia["id"], f"{datetime.date.today()} {orario}", nuovo_tentativo))
    db.commit()
    nuovo_id = db.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
    db.close()

    paziente = {
        "nome": terapia["nome"],
        "cognome": terapia["cognome"],
        "telegram_chat_id": terapia["telegram_chat_id"],
        "profilo": terapia["profilo"],
    }
    notifica_mgr.notifica_completa(
        paziente, terapia["farmaco_nome"], terapia["dose"], orario, nuovo_id, modalita
    )
    _pianifica_followup(nuovo_id, terapia, orario, notifica_mgr, ritardo_min, max_tentativi, nuovo_tentativo, modalita)


def _alert_caregiver_finale(assunzione_id, terapia, orario, notifica_mgr, tentativo, modalita="famiglia"):
    db = get_db()
    ass = db.execute("SELECT esito FROM assunzioni WHERE id=?", (assunzione_id,)).fetchone()
    db.close()
    if ass and ass["esito"] == "SI":
        return
    paziente = {
        "nome": terapia["nome"],
        "cognome": terapia["cognome"],
        "telegram_chat_id": terapia["telegram_chat_id"],
    }
    notifica_mgr.alert_caregiver_completo(paziente, terapia["farmaco_nome"], orario, tentativo, modalita)
