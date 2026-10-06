import os
import re
import io
import json
import hmac
import time
import base64
import hashlib
import secrets
import binascii
import datetime
import threading
import traceback
from functools import wraps
from html import escape as _e

from flask import Flask, request, jsonify, render_template, redirect, url_for, send_from_directory
from flask_cors import CORS
from flask_login import LoginManager, UserMixin, login_user, logout_user, \
                        login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

from database import init_db, get_db, chiudi_connessioni_richiesta
from notifiche import NotificaManager
from aifa import lookup_aic, e_placeholder, cartella_immagini
from scheduler import avvia_scheduler, ripianifica_oggi

app = Flask(__name__)

# ── Chiave di sessione ──────────────────────────────────────────────────────
# Chi conosce la secret_key può falsificare il cookie di sessione e accedere come
# qualsiasi utente: i valori di esempio pubblicati nel repository non vanno mai usati.
_CHIAVI_NOTE = {
    "",
    "farmaci-secret-cambia-questo-valore",
    "mabalu-pillolapp-secret-2026-bart",
    "farmaci-manager-secret-2026-mabalu",
}
_secret = os.environ.get("SECRET_KEY", "")
if _secret in _CHIAVI_NOTE:
    _secret = secrets.token_hex(32)
    print("[AUTH] ⚠️ SECRET_KEY assente o di esempio: uso una chiave casuale "
          "(le sessioni si azzerano ad ogni riavvio). Imposta 'secret_key' nella configurazione.")
app.secret_key = _secret
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024   # foto/CSV: max 16 MB
app.config["REMEMBER_COOKIE_HTTPONLY"] = True

# Segreto per autenticare le chiamate del webhook Telegram (derivato dalla secret_key)
WEBHOOK_SECRET = hmac.new(_secret.encode(), b"pillolapp-telegram-webhook", hashlib.sha256).hexdigest()[:48]
WEBHOOK_SEGRETO_ATTIVO = False   # diventa True solo dopo una registrazione riuscita con secret_token

CORS(app, resources={r"/api/*": {"origins": "*"}})
app.teardown_request(chiudi_connessioni_richiesta)

# ── Flask-Login ─────────────────────────────────────────────────────────────
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "pagina_login"
login_manager.login_message = "Accedi per continuare"

class Utente(UserMixin):
    def __init__(self, id, username, ruolo, paziente_id=None):
        self.id          = str(id)
        self.username    = username
        self.ruolo       = ruolo
        self.paziente_id = paziente_id

    @property
    def is_caregiver(self):
        return self.ruolo == "caregiver"

    @property
    def is_paziente(self):
        return self.ruolo == "paziente"

@login_manager.user_loader
def load_user(user_id):
    db = get_db()
    row = db.execute("SELECT * FROM utenti WHERE id=? AND attivo=1", (user_id,)).fetchone()
    db.close()
    if row:
        return Utente(row["id"], row["username"], row["ruolo"], row["paziente_id"])
    return None

# ── Password ────────────────────────────────────────────────────────────────
# Le password nuove usano PBKDF2 con salt. Gli hash SHA-256 "nudi" delle versioni
# precedenti restano validi e vengono aggiornati in automatico al primo login.
def hash_password(pw: str) -> str:
    return generate_password_hash(pw, method="pbkdf2:sha256:310000")

def _hash_legacy(h: str) -> bool:
    return bool(h) and re.fullmatch(r"[0-9a-f]{64}", h) is not None

def verifica_password(hash_salvato: str, pw: str) -> bool:
    if not hash_salvato:
        return False
    if _hash_legacy(hash_salvato):
        return hmac.compare_digest(hash_salvato, hashlib.sha256(pw.encode()).hexdigest())
    try:
        return check_password_hash(hash_salvato, pw)
    except Exception:
        return False

def crea_utente_se_non_esiste(username, password, ruolo, paziente_id=None) -> bool:
    """Crea utente al primo avvio se non esiste. Ritorna True se l'ha creato."""
    db = get_db()
    ex = db.execute("SELECT id FROM utenti WHERE username=?", (username,)).fetchone()
    creato = False
    if not ex:
        db.execute("""
            INSERT INTO utenti (username, password, ruolo, paziente_id)
            VALUES (?, ?, ?, ?)
        """, (username, hash_password(password), ruolo, paziente_id))
        db.commit()
        print(f"[AUTH] Utente creato: {username} ({ruolo})")
        creato = True
    db.close()
    return creato

def _avvisa_credenziali_deboli():
    """Segnala nel log se il caregiver usa ancora una password di esempio pubblicata online."""
    db = get_db()
    row = db.execute("SELECT password FROM utenti WHERE username='caregiver'").fetchone()
    db.close()
    if row and any(verifica_password(row["password"], pw) for pw in ("admin1234", "PillolApp2026!")):
        print("[AUTH] ⚠️ ATTENZIONE: l'utente 'caregiver' usa ancora una password di esempio "
              "pubblicata nel repository. Cambiala subito dalla dashboard (🔑) o dalla configurazione.")

# ── Limite tentativi di login (anti brute-force) ────────────────────────────
_LOGIN_MAX_TENTATIVI = 5
_LOGIN_FINESTRA_S    = 900
_LOGIN_BLOCCO_S      = 300
_login_falliti = {}
_login_lock = threading.Lock()

def _chiave_login(username):
    return f"{request.remote_addr}|{username.lower()}"

def _login_secondi_blocco(chiave):
    with _login_lock:
        stato = _login_falliti.get(chiave)
        if not stato:
            return 0
        adesso = time.time()
        if stato["blocco_fino"] > adesso:
            return int(stato["blocco_fino"] - adesso) + 1
        if adesso - stato["inizio"] > _LOGIN_FINESTRA_S:
            _login_falliti.pop(chiave, None)
        return 0

def _login_registra_fallimento(chiave):
    with _login_lock:
        adesso = time.time()
        stato = _login_falliti.get(chiave)
        if not stato or adesso - stato["inizio"] > _LOGIN_FINESTRA_S:
            stato = {"conteggio": 0, "inizio": adesso, "blocco_fino": 0}
        stato["conteggio"] += 1
        if stato["conteggio"] >= _LOGIN_MAX_TENTATIVI:
            stato["blocco_fino"] = adesso + _LOGIN_BLOCCO_S
            stato["conteggio"] = 0
            stato["inizio"] = adesso
        _login_falliti[chiave] = stato

def _login_azzera(chiave):
    with _login_lock:
        _login_falliti.pop(chiave, None)

# ── Config da env ────────────────────────────────────────────────────────────
HA_URL              = os.environ.get("HA_URL", "http://192.168.1.83:8123").rstrip("/")
HA_TOKEN            = os.environ.get("HA_TOKEN", "")
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS   = [x.strip() for x in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if x.strip()]
ALEXA_ABILITATA     = os.environ.get("ALEXA_ABILITATA", "true").lower() == "true"
ALEXA_ENTITY_ID     = os.environ.get("ALEXA_ENTITY_ID", "media_player.alexa")
RITARDO_MIN         = int(os.environ.get("NOTIFICA_RITARDO_MIN", 15))
MAX_TENTATIVI       = int(os.environ.get("NOTIFICA_MAX_TENTATIVI", 3))
CAREGIVER_PASSWORD  = os.environ.get("CAREGIVER_PASSWORD", "")
MODALITA            = os.environ.get("MODALITA_UTILIZZO", "famiglia")  # "solo" | "famiglia"
PUBLIC_URL          = os.environ.get("PUBLIC_URL", "")   # es. https://farmaci.mabalu.it

notifica_mgr = NotificaManager(
    telegram_token=TELEGRAM_BOT_TOKEN,
    chat_ids=TELEGRAM_CHAT_IDS,
    ha_url=HA_URL,
    ha_token=HA_TOKEN,
    alexa_entity=ALEXA_ENTITY_ID,
    alexa_abilitata=ALEXA_ABILITATA,
)

# ═══════════════════════════════════════════════════════════════════════════
# UTILITÀ — risposte, permessi, validazione
# ═══════════════════════════════════════════════════════════════════════════

def _json_body():
    return request.get_json(silent=True) or {}

def _nega():
    return jsonify({"error": "Non autorizzato"}), 403

def _in_background(fn, *args):
    """Esegue fn(*args) in un thread: per chiamate di rete che non devono bloccare la richiesta."""
    def _run():
        try:
            fn(*args)
        except Exception:
            print(f"[BG] Errore in {getattr(fn, '__name__', fn)}:\n{traceback.format_exc()}")
    threading.Thread(target=_run, daemon=True).start()

def solo_caregiver(f):
    """Route riservata al caregiver."""
    @wraps(f)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_caregiver:
            return _nega()
        return f(*args, **kwargs)
    return wrapper

def _accesso_paziente(pid) -> bool:
    """Il caregiver vede tutti i pazienti; un paziente solo i propri dati."""
    if not current_user.is_authenticated:
        return False
    if current_user.is_caregiver:
        return True
    try:
        return current_user.paziente_id is not None and int(current_user.paziente_id) == int(pid)
    except (TypeError, ValueError):
        return False

def _paziente_di_terapia(tid):
    db = get_db()
    r = db.execute("SELECT paziente_id FROM terapie WHERE id=?", (tid,)).fetchone()
    db.close()
    return r["paziente_id"] if r else None

def _normalizza_aic(valore):
    """AIC a 9 cifre → prime 6 (codice medicinale); 6 cifre → invariato; altro → None."""
    cifre = re.sub(r"[^0-9]", "", str(valore or ""))
    if len(cifre) == 9:
        return cifre[:6]
    if len(cifre) == 6:
        return cifre
    return None

def _data_valida(valore):
    try:
        return str(datetime.date.fromisoformat(str(valore)[:10]))
    except (TypeError, ValueError):
        return None

def _normalizza_orari(valore):
    """Accetta lista o stringa JSON di 'H:MM'/'HH:MM'. Ritorna (lista ordinata senza doppioni, errore)."""
    if isinstance(valore, str):
        try:
            valore = json.loads(valore)
        except ValueError:
            return None, "orari non validi"
    if not isinstance(valore, list) or not valore:
        return None, "Campo obbligatorio: orari"
    risultato = set()
    for o in valore:
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(o))
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            return None, f"Orario non valido: {o}"
        risultato.add(f"{int(m.group(1)):02d}:{m.group(2)}")
    return sorted(risultato), None

def _normalizza_giorni(valore):
    """Ritorna (lista 0-6 ordinata | None se tutti i giorni, errore)."""
    if valore in (None, "", []):
        return None, None
    if isinstance(valore, str):
        try:
            valore = json.loads(valore)
        except ValueError:
            return None, "giorni_settimana non validi"
    if not isinstance(valore, list):
        return None, "giorni_settimana non validi"
    try:
        giorni = sorted({int(g) for g in valore})
    except (TypeError, ValueError):
        return None, "giorni_settimana non validi"
    if any(g < 0 or g > 6 for g in giorni):
        return None, "giorni_settimana deve contenere valori da 0 (lun) a 6 (dom)"
    return (None if len(giorni) == 7 else giorni), None

def _intero_positivo(valore):
    try:
        n = int(valore)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None

# ═══════════════════════════════════════════════════════════════════════════
# PROTEZIONE API
# ═══════════════════════════════════════════════════════════════════════════

# Gli unici endpoint raggiungibili senza sessione: login, file statici e il webhook
# di Telegram (che si autentica da solo con il secret_token).
ENDPOINT_PUBBLICI = {"pagina_login", "telegram_webhook", "static"}

@app.before_request
def _richiedi_login_api():
    if request.endpoint is None or request.endpoint in ENDPOINT_PUBBLICI:
        return None
    if request.path.startswith("/api/") and not current_user.is_authenticated:
        return jsonify({"error": "Autenticazione richiesta"}), 401
    return None

# ═══════════════════════════════════════════════════════════════════════════
# AUTENTICAZIONE
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/login", methods=["GET", "POST"])
def pagina_login():
    if current_user.is_authenticated:
        return _redirect_dopo_login()

    errore = None
    username_prev = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        username_prev = username
        chiave = _chiave_login(username)

        attesa = _login_secondi_blocco(chiave)
        if attesa:
            minuti = max(1, (attesa + 59) // 60)
            errore = f"Troppi tentativi falliti. Riprova tra {minuti} minut{'o' if minuti == 1 else 'i'}."
            return render_template("login.html", errore=errore, username_prev=username_prev), 429

        db = get_db()
        row = db.execute(
            "SELECT * FROM utenti WHERE username=? AND attivo=1", (username,)
        ).fetchone()

        if row and verifica_password(row["password"], password):
            _login_azzera(chiave)
            if _hash_legacy(row["password"]):
                db.execute("UPDATE utenti SET password=? WHERE id=?", (hash_password(password), row["id"]))
                db.commit()
            db.close()
            utente = Utente(row["id"], row["username"], row["ruolo"], row["paziente_id"])
            login_user(utente, remember=True)
            return _redirect_dopo_login()

        db.close()
        _login_registra_fallimento(chiave)
        errore = "Username o password non corretti"

    return render_template("login.html", errore=errore, username_prev=username_prev)

def _redirect_dopo_login():
    if MODALITA == "solo":
        # In modalità solo → tutti vanno alla dashboard unica
        return redirect(url_for("index"))
    # Modalità famiglia → caregiver alla dashboard, paziente alla sua home
    if current_user.is_caregiver:
        return redirect(url_for("index"))
    else:
        return redirect(url_for("home_paziente_view"))

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("pagina_login"))

# ═══════════════════════════════════════════════════════════════════════════
# ROUTES FRONTEND
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/")
@login_required
def index():
    # Modalità famiglia: solo il caregiver vede la dashboard completa
    if MODALITA == "famiglia" and not current_user.is_caregiver:
        return redirect(url_for("home_paziente_view"))
    return render_template("index.html", modalita=MODALITA)

@app.route("/home")
@login_required
def home_paziente_view():
    if current_user.is_caregiver:
        return redirect(url_for("index"))
    db = get_db()
    paziente = db.execute(
        "SELECT * FROM pazienti WHERE id=? AND attivo=1",
        (current_user.paziente_id,)
    ).fetchone()
    db.close()
    if not paziente:
        logout_user()
        return redirect(url_for("pagina_login"))
    return render_template("home_paziente.html", paziente=paziente)

@app.route("/paziente/<int:paziente_id>")
@login_required
def schermata_paziente(paziente_id):
    """Schermata adattiva: mostra UI completa o solo conferma in base al profilo."""
    if not _accesso_paziente(paziente_id):
        return "Non autorizzato", 403
    db = get_db()
    paziente = db.execute(
        "SELECT * FROM pazienti WHERE id = ? AND attivo = 1", (paziente_id,)
    ).fetchone()
    db.close()
    if not paziente:
        return "Paziente non trovato", 404
    if dict(paziente)["profilo"] == "assistito":
        return render_template("conferma.html", paziente=dict(paziente))
    return render_template("gestione.html", paziente=dict(paziente))

@app.route("/media/farmaci/<path:filename>")
@login_required
def media_farmaci(filename):
    """Foto confezioni caricate dagli utenti (salvate in /data, quindi persistenti)."""
    return send_from_directory(cartella_immagini(), filename)

# ═══════════════════════════════════════════════════════════════════════════
# API — UTENTI
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/utenti/cambia-pwd", methods=["POST"])
@login_required
def cambia_pwd_autonomo():
    """L'utente cambia la propria password verificando quella attuale."""
    data = _json_body()
    pwd_attuale = data.get("password_attuale", "")
    pwd_nuova   = data.get("password_nuova", "")

    if not pwd_attuale or not pwd_nuova:
        return jsonify({"error": "Compila entrambi i campi"}), 400
    if len(pwd_nuova) < 6:
        return jsonify({"error": "La nuova password deve avere almeno 6 caratteri"}), 400

    db = get_db()
    utente = db.execute(
        "SELECT * FROM utenti WHERE id=?", (current_user.id,)
    ).fetchone()

    if not utente or not verifica_password(utente["password"], pwd_attuale):
        db.close()
        return jsonify({"error": "Password attuale non corretta"}), 400

    db.execute(
        "UPDATE utenti SET password=? WHERE id=?",
        (hash_password(pwd_nuova), current_user.id)
    )
    db.commit()
    db.close()
    return jsonify({"message": "Password aggiornata con successo"})


@app.route("/api/utenti", methods=["GET"])
@solo_caregiver
def get_utenti():
    db = get_db()
    rows = db.execute("""
        SELECT u.id, u.username, u.ruolo, u.attivo, u.creato_il, u.paziente_id,
               p.nome, p.cognome
        FROM utenti u
        LEFT JOIN pazienti p ON u.paziente_id = p.id
        ORDER BY u.ruolo, u.username
    """).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/utenti", methods=["POST"])
@solo_caregiver
def crea_utente():
    data = _json_body()
    username  = str(data.get("username", "")).strip()
    password  = str(data.get("password", "")).strip()
    ruolo     = data.get("ruolo", "paziente")
    paziente_id = data.get("paziente_id")

    if not username or not password:
        return jsonify({"error": "Username e password obbligatori"}), 400
    if ruolo not in ("caregiver", "paziente"):
        return jsonify({"error": "ruolo deve essere 'caregiver' o 'paziente'"}), 400

    db = get_db()
    try:
        if ruolo == "paziente":
            paz = db.execute(
                "SELECT id FROM pazienti WHERE id=? AND attivo=1", (paziente_id,)
            ).fetchone() if paziente_id is not None else None
            if not paz:
                return jsonify({"error": "Un utente paziente richiede un paziente_id valido"}), 400
        else:
            paziente_id = None

        if db.execute("SELECT 1 FROM utenti WHERE username=?", (username,)).fetchone():
            return jsonify({"error": f"Username '{username}' già in uso"}), 400

        c = db.execute("""
            INSERT INTO utenti (username, password, ruolo, paziente_id)
            VALUES (?, ?, ?, ?)
        """, (username, hash_password(password), ruolo, paziente_id))
        db.commit()
        return jsonify({"id": c.lastrowid, "message": f"Utente '{username}' creato"}), 201
    finally:
        db.close()

@app.route("/api/utenti/<int:uid>/password", methods=["PUT"])
@solo_caregiver
def cambia_password(uid):
    data = _json_body()
    nuova = str(data.get("password", "")).strip()
    if not nuova:
        return jsonify({"error": "Password vuota"}), 400
    db = get_db()
    try:
        if not db.execute("SELECT 1 FROM utenti WHERE id=?", (uid,)).fetchone():
            return jsonify({"error": "Utente non trovato"}), 404
        db.execute("UPDATE utenti SET password=? WHERE id=?", (hash_password(nuova), uid))
        db.commit()
    finally:
        db.close()
    return jsonify({"message": "Password aggiornata"})

@app.route("/api/utenti/<int:uid>", methods=["DELETE"])
@solo_caregiver
def elimina_utente(uid):
    if str(uid) == str(current_user.id):
        return jsonify({"error": "Non puoi disattivare il tuo stesso account"}), 400
    db = get_db()
    try:
        utente = db.execute("SELECT ruolo FROM utenti WHERE id=? AND attivo=1", (uid,)).fetchone()
        if not utente:
            return jsonify({"error": "Utente non trovato"}), 404
        if utente["ruolo"] == "caregiver":
            altri = db.execute(
                "SELECT COUNT(*) AS n FROM utenti WHERE ruolo='caregiver' AND attivo=1 AND id<>?", (uid,)
            ).fetchone()["n"]
            if altri == 0:
                return jsonify({"error": "Deve restare almeno un caregiver attivo"}), 400
        db.execute("UPDATE utenti SET attivo=0 WHERE id=?", (uid,))
        db.commit()
    finally:
        db.close()
    return jsonify({"message": "Utente disattivato"})

# ═══════════════════════════════════════════════════════════════════════════
# API — PAZIENTI
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/pazienti", methods=["GET"])
@solo_caregiver
def get_pazienti():
    db = get_db()
    rows = db.execute("SELECT * FROM pazienti WHERE attivo = 1 ORDER BY cognome").fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

def _chat_id_pulito(valore):
    """Il Chat ID Telegram è numerico: toglie spazi e invisibili copiati per sbaglio."""
    if valore is None:
        return None
    pulito = re.sub(r"\s+", "", str(valore))
    return pulito or None

@app.route("/api/pazienti", methods=["POST"])
@solo_caregiver
def crea_paziente():
    data = _json_body()
    required = ["nome", "cognome"]
    for f in required:
        if not str(data.get(f) or "").strip():
            return jsonify({"error": f"Campo obbligatorio mancante: {f}"}), 400

    profilo = data.get("profilo", "assistito")
    if profilo not in ("autosufficiente", "assistito"):
        return jsonify({"error": "profilo deve essere 'autosufficiente' o 'assistito'"}), 400

    db = get_db()
    c = db.execute("""
        INSERT INTO pazienti (nome, cognome, data_nascita, profilo, telegram_chat_id, note_medico)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        data["nome"].strip(), data["cognome"].strip(),
        data.get("data_nascita"), profilo,
        _chat_id_pulito(data.get("telegram_chat_id")), data.get("note_medico")
    ))
    db.commit()
    nuovo_id = c.lastrowid
    db.close()
    return jsonify({"id": nuovo_id, "message": "Paziente creato"}), 201

@app.route("/api/pazienti/<int:pid>", methods=["PUT"])
@solo_caregiver
def aggiorna_paziente(pid):
    data = _json_body()
    db = get_db()
    try:
        attuale = db.execute("SELECT * FROM pazienti WHERE id=?", (pid,)).fetchone()
        if not attuale:
            return jsonify({"error": "Paziente non trovato"}), 404

        # Aggiornamento parziale: i campi non inviati restano invariati
        nome     = str(data.get("nome", attuale["nome"]) or "").strip()
        cognome  = str(data.get("cognome", attuale["cognome"]) or "").strip()
        profilo  = data.get("profilo", attuale["profilo"])
        if not nome or not cognome:
            return jsonify({"error": "nome e cognome non possono essere vuoti"}), 400
        if profilo not in ("autosufficiente", "assistito"):
            return jsonify({"error": "profilo deve essere 'autosufficiente' o 'assistito'"}), 400
        chat_id = _chat_id_pulito(data["telegram_chat_id"]) if "telegram_chat_id" in data else attuale["telegram_chat_id"]

        db.execute("""
            UPDATE pazienti SET nome=?, cognome=?, data_nascita=?, profilo=?,
            telegram_chat_id=?, note_medico=?, aggiornato_il=datetime('now')
            WHERE id=?
        """, (
            nome, cognome,
            data.get("data_nascita", attuale["data_nascita"]), profilo, chat_id,
            data.get("note_medico", attuale["note_medico"]), pid
        ))
        db.commit()
    finally:
        db.close()
    return jsonify({"message": "Paziente aggiornato"})

@app.route("/api/pazienti/<int:pid>", methods=["DELETE"])
@solo_caregiver
def elimina_paziente(pid):
    db = get_db()
    db.execute("UPDATE pazienti SET attivo=0 WHERE id=?", (pid,))
    # Un paziente disattivato non deve più poter accedere
    db.execute("UPDATE utenti SET attivo=0 WHERE paziente_id=? AND ruolo='paziente'", (pid,))
    db.commit()
    db.close()
    ripianifica_oggi()
    return jsonify({"message": "Paziente disattivato"})

# ═══════════════════════════════════════════════════════════════════════════
# API — FARMACI + LOOKUP AIC
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/farmaci", methods=["GET"])
def get_farmaci():
    db = get_db()
    rows = db.execute("SELECT * FROM farmaci ORDER BY nome").fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

_AIC_PATTERNS = [
    r'A\.?I\.?C\.?\s*[:\-]?\s*0?(\d{6,9})',
    r'\bAIC\s*[:\-]?\s*0?(\d{6,9})',
    r'\bA0?(\d{8})\b',
    r'\b(0\d{8})\b',
    r'\b(0\d{5})\b',
]

def _estrai_aic_da_testo(testo):
    for pat in _AIC_PATTERNS:
        m = re.search(pat, testo, re.IGNORECASE)
        if m:
            aic = re.sub(r'[^0-9]', '', m.group(1))
            if len(aic) >= 6:
                return aic
    return None

def _ocr_immagine(img_bytes, tag):
    """OCR sull'immagine. Ritorna (risposta_json, status_http)."""
    from PIL import Image

    if len(img_bytes) < 100:
        return {"error": f"Immagine troppo piccola ({len(img_bytes)} bytes)", "aic": None}, 400
    try:
        img = Image.open(io.BytesIO(img_bytes))
        img.load()  # forza caricamento completo
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
    except Exception as e:
        print(f"[{tag}] Errore apertura immagine: {traceback.format_exc()}")
        return {"error": f"Immagine non valida: {str(e)}", "aic": None}, 400

    print(f"[{tag}] Immagine: {img.size} {img.mode}, {len(img_bytes)} bytes")

    try:
        import pytesseract
        testo = pytesseract.image_to_string(img, lang="ita+eng")
        if not testo.strip():
            testo = pytesseract.image_to_string(img, lang="eng")
    except ImportError:
        return {"error": "pytesseract non installato", "aic": None}, 200
    except Exception as e:
        print(f"[{tag}] Errore pytesseract: {traceback.format_exc()}")
        return {"error": f"OCR fallito: {str(e)}", "aic": None}, 500

    print(f"[{tag}] Testo ({len(testo)} chars): {testo[:300]}")
    aic = _estrai_aic_da_testo(testo)
    if aic:
        print(f"[{tag}] AIC trovato: {aic}")
    return {"aic": aic, "testo": testo[:200]}, 200

@app.route("/api/farmaci/ocr-foto-b64", methods=["POST"])
def ocr_foto_b64():
    """Riceve immagine come base64 dataURL e usa pytesseract per leggere l'AIC."""
    data = _json_body()
    if "immagine" not in data:
        return jsonify({"error": "Campo 'immagine' mancante", "aic": None}), 400

    data_url = str(data["immagine"])
    if "," in data_url:   # rimuove il prefisso "data:image/jpeg;base64,"
        data_url = data_url.split(",", 1)[1]
    try:
        img_bytes = base64.b64decode(data_url)
    except (binascii.Error, ValueError):
        return jsonify({"error": "Immagine base64 non valida", "aic": None}), 400

    risposta, status = _ocr_immagine(img_bytes, "OCR-B64")
    return jsonify(risposta), status

@app.route("/api/farmaci/ocr-foto", methods=["POST"])
def ocr_foto():
    """Riceve una foto e usa pytesseract lato server per leggere l'AIC."""
    if "foto" not in request.files:
        return jsonify({"error": "Nessuna foto ricevuta", "aic": None}), 400
    risposta, status = _ocr_immagine(request.files["foto"].read(), "OCR")
    return jsonify(risposta), status

@app.route("/api/farmaci/test-aic/<aic>", methods=["GET"])
def test_aic(aic):
    """Endpoint GET per testare il lookup AIC direttamente dal browser."""
    risultato = lookup_aic(aic)
    return jsonify({
        "aic_input": aic,
        "risultato": risultato,
        "trovato": risultato is not None and not e_placeholder(risultato.get("nome"))
    })

_COLONNE_FARMACO = ("aic", "nome", "nome_commerciale", "principio_attivo", "forma_farmaceutica",
                    "dosaggio", "atc", "produttore", "foglietto_url", "immagine_url",
                    "immagine_locale", "colore_avatar")

def _salva_farmaco_da_lookup(db, aic6, farmaco_data):
    """
    Inserisce il farmaco in anagrafica oppure, se esiste solo con il nome segnaposto
    ("Farmaco AIC xxxxxx"), lo aggiorna con i dati reali. Ritorna la riga di `farmaci`.
    """
    esistente = db.execute("SELECT * FROM farmaci WHERE aic=?", (aic6,)).fetchone()
    dati = {c: farmaco_data.get(c) for c in _COLONNE_FARMACO}
    dati["aic"] = aic6
    if not esistente:
        db.execute(f"""
            INSERT OR IGNORE INTO farmaci ({", ".join(_COLONNE_FARMACO)})
            VALUES ({", ".join(":" + c for c in _COLONNE_FARMACO)})
        """, dati)
    elif e_placeholder(esistente["nome"]) and not e_placeholder(dati["nome"]):
        db.execute("""
            UPDATE farmaci SET nome=:nome, nome_commerciale=:nome_commerciale,
                principio_attivo=:principio_attivo, atc=:atc,
                forma_farmaceutica=COALESCE(:forma_farmaceutica, forma_farmaceutica),
                dosaggio=COALESCE(:dosaggio, dosaggio), produttore=COALESCE(:produttore, produttore),
                foglietto_url=COALESCE(:foglietto_url, foglietto_url),
                immagine_url=COALESCE(:immagine_url, immagine_url),
                colore_avatar=COALESCE(:colore_avatar, colore_avatar)
            WHERE id=:id
        """, {**dati, "id": esistente["id"]})
    db.commit()
    return db.execute("SELECT * FROM farmaci WHERE aic=?", (aic6,)).fetchone()

@app.route("/api/farmaci/lookup", methods=["POST"])
def lookup_farmaco():
    """Cerca farmaco per AIC (da OCR o input manuale): anagrafica → cache → AIFA, sempre con id."""
    data = _json_body()
    aic_raw = str(data.get("aic", "")).strip()
    if not aic_raw:
        return jsonify({"error": "AIC obbligatorio"}), 400
    aic6 = _normalizza_aic(aic_raw)
    if not aic6:
        return jsonify({"error": "AIC non valido: servono 6 o 9 cifre"}), 400

    db = get_db()
    try:
        # 1. Anagrafica locale (già nota e con nome reale)
        riga = db.execute("SELECT * FROM farmaci WHERE aic=?", (aic6,)).fetchone()
        if riga and not e_placeholder(riga["nome"]):
            return jsonify({"source": "cache", "farmaco": dict(riga)})

        # 2. Cache di un precedente lookup riuscito
        cached = db.execute("SELECT payload_json FROM cache_aic WHERE aic=?", (aic6,)).fetchone()
        if cached:
            try:
                payload = json.loads(cached["payload_json"])
            except ValueError:
                payload = None
            if payload and not e_placeholder(payload.get("nome")):
                riga = _salva_farmaco_da_lookup(db, aic6, payload)
                return jsonify({"source": "cache", "farmaco": dict(riga)})

        # 3. Lookup (DB AIFA locale → AIFA online → segnaposto)
        farmaco_data = lookup_aic(aic6)
        if not farmaco_data:
            return jsonify({"error": "Farmaco non trovato per questo AIC"}), 404

        # Il segnaposto NON va in cache: se in seguito importi i CSV AIFA, il nome reale
        # deve poter comparire al lookup successivo.
        if not e_placeholder(farmaco_data.get("nome")):
            db.execute(
                "INSERT OR REPLACE INTO cache_aic (aic, payload_json) VALUES (?, ?)",
                (aic6, json.dumps(farmaco_data))
            )
        riga = _salva_farmaco_da_lookup(db, aic6, farmaco_data)
        return jsonify({"source": "aifa", "farmaco": dict(riga)})
    finally:
        db.close()

@app.route("/api/farmaci/<int:fid>/immagine", methods=["POST"])
def upload_immagine_farmaco(fid):
    """
    Upload foto confezione scattata dall'utente (livello 1).
    Accetta multipart/form-data con campo 'foto'.
    """
    from aifa import salva_immagine_utente
    from PIL import Image
    if "foto" not in request.files:
        return jsonify({"error": "Campo 'foto' mancante"}), 400

    file = request.files["foto"]
    nome_file = file.filename or ""
    ext = nome_file.rsplit(".", 1)[-1].lower() if "." in nome_file else "jpg"
    if ext not in ("jpg", "jpeg", "png", "webp"):
        return jsonify({"error": "Formato non supportato (jpg/png/webp)"}), 400

    contenuto = file.read()
    try:
        Image.open(io.BytesIO(contenuto)).verify()   # deve essere davvero un'immagine
    except Exception:
        return jsonify({"error": "Il file non è un'immagine valida"}), 400

    db = get_db()
    try:
        if not db.execute("SELECT 1 FROM farmaci WHERE id=?", (fid,)).fetchone():
            return jsonify({"error": "Farmaco non trovato"}), 404
        path = salva_immagine_utente(fid, contenuto, ext)
        db.execute(
            "UPDATE farmaci SET immagine_locale=?, immagine_url=NULL WHERE id=?",
            (path, fid)
        )
        db.commit()
    finally:
        db.close()
    return jsonify({"immagine_locale": path, "message": "Foto salvata"})


@app.route("/api/farmaci/<int:fid>/avatar", methods=["GET"])
def get_avatar_info(fid):
    """Ritorna colore e iniziali per generare l'avatar SVG lato client."""
    from aifa import colore_avatar, iniziali_avatar
    db = get_db()
    f = db.execute("SELECT nome, colore_avatar FROM farmaci WHERE id=?", (fid,)).fetchone()
    db.close()
    if not f:
        return jsonify({"error": "Farmaco non trovato"}), 404
    nome  = f["nome"]
    color = f["colore_avatar"] or colore_avatar(nome)
    return jsonify({
        "colore":   color,
        "iniziali": iniziali_avatar(nome),
        "nome":     nome,
    })


@app.route("/api/farmaci", methods=["POST"])
def aggiungi_farmaco_manuale():
    """Inserimento manuale quando l'OCR/AIFA non trova il farmaco."""
    data = _json_body()
    nome = str(data.get("nome") or "").strip()
    if not nome:
        return jsonify({"error": "nome obbligatorio"}), 400
    aic6 = _normalizza_aic(data.get("aic"))

    db = get_db()
    try:
        if aic6:
            # L'AIC è univoco: se il farmaco esiste già lo riutilizziamo (e, se aveva solo il
            # nome segnaposto, gli diamo il nome inserito dall'utente) invece di fallire.
            esistente = db.execute("SELECT id, nome FROM farmaci WHERE aic=?", (aic6,)).fetchone()
            if esistente:
                if e_placeholder(esistente["nome"]) and not e_placeholder(nome):
                    db.execute("UPDATE farmaci SET nome=?, nome_commerciale=? WHERE id=?",
                               (nome, nome, esistente["id"]))
                    db.commit()
                return jsonify({"id": esistente["id"], "message": "Farmaco già presente"})
        c = db.execute("""
            INSERT INTO farmaci (aic, nome, nome_commerciale, principio_attivo,
                forma_farmaceutica, dosaggio, atc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            aic6, nome, data.get("nome_commerciale"),
            data.get("principio_attivo"), data.get("forma_farmaceutica"),
            data.get("dosaggio"), data.get("atc")
        ))
        db.commit()
        return jsonify({"id": c.lastrowid, "message": "Farmaco aggiunto"}), 201
    finally:
        db.close()

# ═══════════════════════════════════════════════════════════════════════════
# API — TERAPIE
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/terapie/<int:paziente_id>", methods=["GET"])
def get_terapie(paziente_id):
    if not _accesso_paziente(paziente_id):
        return _nega()
    db = get_db()
    rows = db.execute("""
        SELECT t.*, f.nome as farmaco_nome, f.principio_attivo, f.forma_farmaceutica,
               f.aic as farmaco_aic, f.immagine_url, f.immagine_locale, f.colore_avatar
        FROM terapie t JOIN farmaci f ON t.farmaco_id = f.id
        WHERE t.paziente_id = ? AND t.attiva = 1
        ORDER BY t.orari
    """, (paziente_id,)).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

def _data_fine_da_durata(data_inizio, durata_giorni):
    """data_inizio + durata (il primo giorno conta): una terapia di 5 giorni dal 1° finisce il 5."""
    if not durata_giorni:
        return None
    inizio = datetime.date.fromisoformat(data_inizio)
    return str(inizio + datetime.timedelta(days=durata_giorni - 1))

@app.route("/api/terapie", methods=["POST"])
def crea_terapia():
    data = _json_body()
    try:
        paziente_id = int(data.get("paziente_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Campo obbligatorio: paziente_id"}), 400
    if not _accesso_paziente(paziente_id):
        return _nega()

    dose = str(data.get("dose") or "").strip()
    if not dose:
        return jsonify({"error": "Campo obbligatorio: dose"}), 400
    orari, err = _normalizza_orari(data.get("orari"))
    if err:
        return jsonify({"error": err}), 400
    giorni, err = _normalizza_giorni(data.get("giorni_settimana"))
    if err:
        return jsonify({"error": err}), 400

    data_inizio = _data_valida(data.get("data_inizio")) if data.get("data_inizio") else str(datetime.date.today())
    if not data_inizio:
        return jsonify({"error": "data_inizio non valida"}), 400
    durata = _intero_positivo(data.get("durata_giorni"))
    data_fine = None
    if data.get("data_fine"):
        data_fine = _data_valida(data["data_fine"])
        if not data_fine:
            return jsonify({"error": "data_fine non valida"}), 400
    else:
        data_fine = _data_fine_da_durata(data_inizio, durata)

    db = get_db()
    try:
        if not db.execute("SELECT 1 FROM pazienti WHERE id=? AND attivo=1", (paziente_id,)).fetchone():
            return jsonify({"error": "Paziente non trovato"}), 404

        # Se farmaco_id non è presente, proviamo a salvare/trovare il farmaco dall'AIC o dal nome
        farmaco_id = data.get("farmaco_id")
        if farmaco_id:
            if not db.execute("SELECT 1 FROM farmaci WHERE id=?", (farmaco_id,)).fetchone():
                return jsonify({"error": "farmaco_id inesistente"}), 400
        else:
            aic6   = _normalizza_aic(data.get("aic") or data.get("farmaco_aic"))
            nome   = str(data.get("nome") or data.get("farmaco_nome") or "").strip()
            pa     = data.get("principio_attivo")
            atc    = data.get("atc")
            colore = data.get("colore_avatar", "#2563eb")

            if aic6:
                ex = db.execute("SELECT id FROM farmaci WHERE aic=?", (aic6,)).fetchone()
                if ex:
                    farmaco_id = ex["id"]
                else:
                    c2 = db.execute("""
                        INSERT INTO farmaci (aic, nome, nome_commerciale, principio_attivo, atc, colore_avatar)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (aic6, nome or "Farmaco sconosciuto", nome or "Farmaco sconosciuto", pa, atc, colore))
                    db.commit()
                    farmaco_id = c2.lastrowid
            elif nome:
                ex = db.execute("SELECT id FROM farmaci WHERE nome=?", (nome,)).fetchone()
                if ex:
                    farmaco_id = ex["id"]
                else:
                    c2 = db.execute("""
                        INSERT INTO farmaci (nome, nome_commerciale, principio_attivo, atc, colore_avatar)
                        VALUES (?, ?, ?, ?, ?)
                    """, (nome, nome, pa, atc, colore))
                    db.commit()
                    farmaco_id = c2.lastrowid

        if not farmaco_id:
            return jsonify({"error": "farmaco_id mancante e impossibile determinare il farmaco"}), 400

        c = db.execute("""
            INSERT INTO terapie (paziente_id, farmaco_id, dose, orari, giorni_settimana,
                durata_giorni, data_inizio, data_fine, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            paziente_id, farmaco_id, dose,
            json.dumps(orari), json.dumps(giorni) if giorni else None,
            durata, data_inizio, data_fine, data.get("note")
        ))
        db.commit()
        nuovo_id = c.lastrowid
    finally:
        db.close()
    ripianifica_oggi()
    return jsonify({"id": nuovo_id, "message": "Terapia creata"}), 201

@app.route("/api/terapie/<int:tid>", methods=["PUT"])
def aggiorna_terapia(tid):
    data = _json_body()
    db = get_db()
    try:
        t = db.execute("SELECT * FROM terapie WHERE id=?", (tid,)).fetchone()
        if not t:
            return jsonify({"error": "Terapia non trovata"}), 404
        if not _accesso_paziente(t["paziente_id"]):
            return _nega()

        # Aggiornamento parziale: i campi non inviati restano invariati
        dose = str(data.get("dose", t["dose"]) or "").strip()
        if not dose:
            return jsonify({"error": "dose non può essere vuota"}), 400

        if "orari" in data:
            orari, err = _normalizza_orari(data["orari"])
            if err:
                return jsonify({"error": err}), 400
            orari_json = json.dumps(orari)
        else:
            orari_json = t["orari"]

        if "giorni_settimana" in data:
            giorni, err = _normalizza_giorni(data["giorni_settimana"])
            if err:
                return jsonify({"error": err}), 400
            giorni_json = json.dumps(giorni) if giorni else None
        else:
            giorni_json = t["giorni_settimana"]

        durata = _intero_positivo(data["durata_giorni"]) if "durata_giorni" in data else t["durata_giorni"]
        if "data_fine" in data:
            data_fine = _data_valida(data["data_fine"]) if data["data_fine"] else None
            if data["data_fine"] and not data_fine:
                return jsonify({"error": "data_fine non valida"}), 400
        elif "durata_giorni" in data:
            data_fine = _data_fine_da_durata(t["data_inizio"], durata)
        else:
            data_fine = t["data_fine"]

        db.execute("""
            UPDATE terapie SET dose=?, orari=?, giorni_settimana=?, durata_giorni=?,
            data_fine=?, note=?, aggiornato_il=datetime('now')
            WHERE id=?
        """, (dose, orari_json, giorni_json, durata, data_fine, data.get("note", t["note"]), tid))
        db.commit()
    finally:
        db.close()
    ripianifica_oggi()
    return jsonify({"message": "Terapia aggiornata"})

@app.route("/api/terapie/<int:tid>", methods=["DELETE"])
def elimina_terapia(tid):
    pid = _paziente_di_terapia(tid)
    if pid is None:
        return jsonify({"error": "Terapia non trovata"}), 404
    if not _accesso_paziente(pid):
        return _nega()
    db = get_db()
    db.execute("UPDATE terapie SET attiva=0 WHERE id=?", (tid,))
    db.commit()
    db.close()
    ripianifica_oggi()   # toglie subito i promemoria di oggi di questa terapia
    return jsonify({"message": "Terapia disattivata"})

# ═══════════════════════════════════════════════════════════════════════════
# API — SCORTE
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/scorte/<int:paziente_id>", methods=["GET"])
def get_scorte(paziente_id):
    if not _accesso_paziente(paziente_id):
        return _nega()
    db = get_db()
    rows = db.execute("""
        SELECT s.*, f.nome as farmaco_nome, f.forma_farmaceutica
        FROM scorte s JOIN farmaci f ON s.farmaco_id = f.id
        WHERE s.paziente_id = ?
        ORDER BY f.nome
    """, (paziente_id,)).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/scorte", methods=["POST"])
def aggiorna_scorta():
    data = _json_body()
    try:
        paziente_id = int(data["paziente_id"])
        farmaco_id  = int(data["farmaco_id"])
        quantita    = float(data["quantita"])
        soglia      = float(data.get("soglia_minima", 7))
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "paziente_id, farmaco_id e quantita sono obbligatori e numerici"}), 400
    if quantita < 0 or soglia < 0:
        return jsonify({"error": "quantita e soglia_minima non possono essere negative"}), 400
    if not _accesso_paziente(paziente_id):
        return _nega()
    scadenza = _data_valida(data.get("scadenza")) if data.get("scadenza") else None
    if data.get("scadenza") and not scadenza:
        return jsonify({"error": "scadenza non valida"}), 400

    db = get_db()
    try:
        if not db.execute("SELECT 1 FROM pazienti WHERE id=? AND attivo=1", (paziente_id,)).fetchone() or \
           not db.execute("SELECT 1 FROM farmaci WHERE id=?", (farmaco_id,)).fetchone():
            return jsonify({"error": "Paziente o farmaco inesistente"}), 404
        db.execute("""
            INSERT INTO scorte (paziente_id, farmaco_id, quantita, unita, soglia_minima, scadenza, lotto)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paziente_id, farmaco_id) DO UPDATE SET
                quantita=excluded.quantita, unita=excluded.unita,
                soglia_minima=excluded.soglia_minima, scadenza=excluded.scadenza,
                lotto=excluded.lotto, aggiornato_il=datetime('now')
        """, (
            paziente_id, farmaco_id, quantita,
            data.get("unita", "compresse"), soglia, scadenza, data.get("lotto")
        ))
        db.commit()
    finally:
        db.close()
    _in_background(aggiorna_sensori_ha)
    return jsonify({"message": "Scorta aggiornata"})

# ═══════════════════════════════════════════════════════════════════════════
# API — ASSUNZIONI (conferma dal paziente)
# ═══════════════════════════════════════════════════════════════════════════

def _registra_esito(ass_id, esito, canale):
    """
    Registra SI/NO su un'assunzione. Logica unica per app, PWA e Telegram.
      • 'SI' è definitivo e non viene mai declassato
      • la scorta scende di 1 una sola volta per slot (anche con tentativi multipli o doppi click)
      • se la scorta attraversa la soglia minima parte l'avviso al caregiver
    Ritorna un dict con 'stato' = 'ok' | 'gia_confermato' | 'non_trovata' + i dati utili.
    """
    avviso = None
    db = get_db()
    try:
        ass = db.execute("""
            SELECT a.*, t.paziente_id, t.farmaco_id, t.dose,
                   f.nome AS farmaco_nome, p.nome AS paz_nome, p.cognome AS paz_cognome
            FROM assunzioni a
            JOIN terapie t ON a.terapia_id = t.id
            JOIN farmaci f ON t.farmaco_id = f.id
            JOIN pazienti p ON t.paziente_id = p.id
            WHERE a.id = ?
        """, (ass_id,)).fetchone()
        if not ass:
            return {"stato": "non_trovata"}
        info = {"paziente_id": ass["paziente_id"], "farmaco_nome": ass["farmaco_nome"],
                "dose": ass["dose"], "paz_nome": ass["paz_nome"], "paz_cognome": ass["paz_cognome"],
                "orario_previsto": ass["orario_previsto"]}

        slot_gia_si = db.execute("""
            SELECT 1 FROM assunzioni
            WHERE terapia_id=? AND orario_previsto=? AND esito='SI' LIMIT 1
        """, (ass["terapia_id"], ass["orario_previsto"])).fetchone()

        if ass["esito"] == "SI" or (esito == "NO" and slot_gia_si):
            return {"stato": "gia_confermato", **info}

        db.execute("""
            UPDATE assunzioni SET esito=?, orario_risposta=datetime('now'), canale=?
            WHERE id=?
        """, (esito, canale, ass_id))

        if esito == "SI" and not slot_gia_si:
            sc = db.execute("""
                SELECT quantita, soglia_minima, unita FROM scorte
                WHERE paziente_id=? AND farmaco_id=?
            """, (ass["paziente_id"], ass["farmaco_id"])).fetchone()
            if sc:
                nuova = max(0, sc["quantita"] - 1)
                db.execute("""
                    UPDATE scorte SET quantita=?, aggiornato_il=datetime('now')
                    WHERE paziente_id=? AND farmaco_id=?
                """, (nuova, ass["paziente_id"], ass["farmaco_id"]))
                if (sc["quantita"] > sc["soglia_minima"] >= nuova) or (nuova == 0 and sc["quantita"] > 0):
                    avviso = (nuova, sc["unita"])
        db.commit()
    finally:
        db.close()

    if avviso:
        _in_background(notifica_mgr.notifica_scorta_bassa,
                       {"nome": info["paz_nome"], "cognome": info["paz_cognome"]},
                       info["farmaco_nome"], avviso[0], avviso[1])
    _in_background(aggiorna_sensori_ha)
    return {"stato": "ok", **info}

def _paziente_di_assunzione(ass_id):
    db = get_db()
    r = db.execute("""
        SELECT t.paziente_id FROM assunzioni a JOIN terapie t ON a.terapia_id = t.id WHERE a.id=?
    """, (ass_id,)).fetchone()
    db.close()
    return r["paziente_id"] if r else None

@app.route("/api/assunzioni/<int:ass_id>/risposta", methods=["POST"])
def risposta_assunzione(ass_id):
    """Il paziente autosufficiente conferma manualmente un'assunzione."""
    data  = _json_body()
    esito = data.get("esito", "SI")
    if esito not in ("SI", "NO"):
        return jsonify({"error": "esito deve essere SI o NO"}), 400
    pid = _paziente_di_assunzione(ass_id)
    if pid is None:
        return jsonify({"error": "Assunzione non trovata"}), 404
    if not _accesso_paziente(pid):
        return _nega()
    _registra_esito(ass_id, esito, "app")
    return jsonify({"message": f"Assunzione {esito} registrata"})


@app.route("/api/assunzioni/conferma", methods=["POST"])
def conferma_assunzione():
    """Endpoint chiamato dalla PWA del paziente."""
    data = _json_body()
    assunzione_id = data.get("assunzione_id")
    esito = data.get("esito")  # 'SI' | 'NO'

    if not assunzione_id or esito not in ("SI", "NO"):
        return jsonify({"error": "assunzione_id e esito (SI/NO) obbligatori"}), 400
    try:
        assunzione_id = int(assunzione_id)
    except (TypeError, ValueError):
        return jsonify({"error": "assunzione_id non valido"}), 400

    pid = _paziente_di_assunzione(assunzione_id)
    if pid is None:
        return jsonify({"error": "Assunzione non trovata"}), 404
    if not _accesso_paziente(pid):
        return _nega()

    canale = data.get("canale", "pwa")
    canale = canale if canale in ("pwa", "app", "alexa", "telegram") else "pwa"
    _registra_esito(assunzione_id, esito, canale)
    return jsonify({"message": f"Assunzione registrata: {esito}"})

@app.route("/api/assunzioni/<int:paziente_id>", methods=["GET"])
def get_assunzioni(paziente_id):
    """
    Log assunzioni degli ultimi N giorni (giorni=1 → solo oggi). Per ogni slot
    (terapia + orario) ritorna UNA riga: quella confermata se c'è, altrimenti l'ultimo
    tentativo; 'tentativi' dice quanti promemoria sono partiti.
    """
    if not _accesso_paziente(paziente_id):
        return _nega()
    try:
        giorni = max(1, min(int(request.args.get("giorni", 7)), 365))
    except (TypeError, ValueError):
        giorni = 7
    dal = str(datetime.date.today() - datetime.timedelta(days=giorni - 1))

    db = get_db()
    rows = db.execute("""
        SELECT a.*, t.dose, f.nome as farmaco_nome
        FROM assunzioni a
        JOIN terapie t ON a.terapia_id = t.id
        JOIN farmaci f ON t.farmaco_id = f.id
        WHERE t.paziente_id = ?
          AND substr(a.orario_previsto, 1, 10) >= ?
        ORDER BY a.orario_previsto DESC, a.id DESC
    """, (paziente_id, dal)).fetchall()
    db.close()

    slot = {}
    for r in rows:
        chiave = (r["terapia_id"], r["orario_previsto"])
        d = dict(r)
        if chiave not in slot:
            d["tentativi"] = 1
            slot[chiave] = d
        else:
            slot[chiave]["tentativi"] += 1
            if d["esito"] == "SI" and slot[chiave]["esito"] != "SI":
                d["tentativi"] = slot[chiave]["tentativi"]
                slot[chiave] = d
    return jsonify(list(slot.values()))

# ═══════════════════════════════════════════════════════════════════════════
# API — ASTUCCIO
# ═══════════════════════════════════════════════════════════════════════════

def _settimana_iso_corrente():
    iso = datetime.date.today().isocalendar()
    # Si usa l'ANNO ISO, non quello solare: il 31/12 o il 1/1 possono appartenere
    # alla settimana 52/53 dell'anno precedente o alla 1 del successivo.
    return f"{iso[0]}-W{iso[1]:02d}"

@app.route("/api/astuccio/<int:paziente_id>", methods=["GET"])
def get_astuccio(paziente_id):
    if not _accesso_paziente(paziente_id):
        return _nega()
    settimana_iso = _settimana_iso_corrente()
    if request.args.get("settimana") or request.args.get("anno"):
        iso = datetime.date.today().isocalendar()
        anno = request.args.get("anno", iso[0])
        sett = request.args.get("settimana", iso[1])
        settimana_iso = f"{anno}-W{str(sett).zfill(2)}"
    db = get_db()
    rows = db.execute("""
        SELECT * FROM astuccio_slot
        WHERE paziente_id=? AND settimana_iso=?
        ORDER BY giorno, fascia
    """, (paziente_id, settimana_iso)).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/astuccio/genera", methods=["POST"])
def genera_astuccio():
    """Genera automaticamente gli slot dell'astuccio dalla terapia della settimana."""
    data = _json_body()
    try:
        paziente_id = int(data["paziente_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "paziente_id obbligatorio"}), 400
    if not _accesso_paziente(paziente_id):
        return _nega()
    settimana_iso = data.get("settimana_iso") or _settimana_iso_corrente()
    m = re.fullmatch(r"(\d{4})-W(\d{2})", str(settimana_iso))
    if not m:
        return jsonify({"error": "settimana_iso non valida (formato AAAA-Www)"}), 400
    try:
        lunedi = datetime.date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    except ValueError:
        return jsonify({"error": "settimana_iso non valida"}), 400
    domenica = lunedi + datetime.timedelta(days=6)

    db = get_db()
    try:
        terapie = db.execute("""
            SELECT * FROM terapie WHERE paziente_id=? AND attiva=1
              AND (data_inizio IS NULL OR data_inizio <= ?)
              AND (data_fine   IS NULL OR data_fine   >= ?)
        """, (paziente_id, str(domenica), str(lunedi))).fetchall()

        ORA_FASCIA = {
            "M": range(5, 12),
            "P": range(12, 15),
            "S": range(15, 22),
            "N": list(range(22, 24)) + list(range(0, 5))
        }

        def ora_a_fascia(ora_str):
            ora = int(ora_str.split(":")[0])
            for fascia, ore in ORA_FASCIA.items():
                if ora in ore:
                    return fascia
            return "M"

        slot_map = {}
        for t in terapie:
            orari = json.loads(t["orari"])
            giorni = json.loads(t["giorni_settimana"]) if t["giorni_settimana"] else list(range(7))
            for giorno in giorni:
                for ora in orari:
                    key = (giorno, ora_a_fascia(ora))
                    if key not in slot_map:
                        slot_map[key] = []
                    if t["farmaco_id"] not in slot_map[key]:
                        slot_map[key].append(t["farmaco_id"])

        for (giorno, fascia), farmaci_ids in slot_map.items():
            db.execute("""
                INSERT INTO astuccio_slot (paziente_id, settimana_iso, giorno, fascia, farmaci_ids)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(paziente_id, settimana_iso, giorno, fascia) DO UPDATE SET
                    farmaci_ids=excluded.farmaci_ids
            """, (paziente_id, settimana_iso, giorno, fascia, json.dumps(farmaci_ids)))

        # Rimuove gli slot di terapie non più presenti (altrimenti restano per sempre)
        esistenti = db.execute(
            "SELECT id, giorno, fascia FROM astuccio_slot WHERE paziente_id=? AND settimana_iso=?",
            (paziente_id, settimana_iso)
        ).fetchall()
        for s in esistenti:
            if (s["giorno"], s["fascia"]) not in slot_map:
                db.execute("DELETE FROM astuccio_slot WHERE id=?", (s["id"],))
        db.commit()
    finally:
        db.close()
    return jsonify({"message": f"Astuccio generato per {settimana_iso}"})

@app.route("/api/astuccio/slot/<int:slot_id>/carica", methods=["POST"])
def marca_slot_caricato(slot_id):
    data = _json_body()
    db = get_db()
    try:
        slot = db.execute("SELECT paziente_id FROM astuccio_slot WHERE id=?", (slot_id,)).fetchone()
        if not slot:
            return jsonify({"error": "Slot non trovato"}), 404
        if not _accesso_paziente(slot["paziente_id"]):
            return _nega()
        caricato = 1 if data.get("caricato", True) else 0
        db.execute("""
            UPDATE astuccio_slot
            SET caricato=?, caricato_il=CASE WHEN ?=1 THEN datetime('now') ELSE NULL END
            WHERE id=?
        """, (caricato, caricato, slot_id))
        db.commit()
    finally:
        db.close()
    return jsonify({"message": "Slot aggiornato"})

# ═══════════════════════════════════════════════════════════════════════════
# HOME ASSISTANT — sensori
# ═══════════════════════════════════════════════════════════════════════════

def aggiorna_sensori_ha():
    """Aggiorna i sensori su Home Assistant via REST API."""
    if not HA_TOKEN:
        return
    import requests as req
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json"
    }
    db = get_db()
    try:
        oggi = str(datetime.date.today())
        tra_7 = str(datetime.date.today() + datetime.timedelta(days=7))

        totale = db.execute("SELECT COUNT(*) as n FROM pazienti WHERE attivo=1").fetchone()["n"]
        in_scadenza = db.execute("""
            SELECT COUNT(*) as n FROM scorte s JOIN pazienti p ON s.paziente_id = p.id
            WHERE p.attivo=1 AND s.scadenza IS NOT NULL AND s.scadenza <= ?
        """, (tra_7,)).fetchone()["n"]
        scorte_basse = db.execute("""
            SELECT COUNT(*) as n FROM scorte s JOIN pazienti p ON s.paziente_id = p.id
            WHERE p.attivo=1 AND s.quantita <= s.soglia_minima
        """).fetchone()["n"]
        terapie_attive = db.execute(
            "SELECT COUNT(*) as n FROM terapie t JOIN pazienti p ON t.paziente_id = p.id "
            "WHERE t.attiva=1 AND p.attivo=1"
        ).fetchone()["n"]

        # Aderenza: una riga per SLOT (terapia + orario), "presa" se un qualsiasi tentativo è SI.
        # Prima ogni promemoria ripetuto contava come una mancata assunzione in più.
        slot = db.execute("""
            SELECT MAX(CASE WHEN esito='SI' THEN 1 ELSE 0 END) AS preso
            FROM assunzioni
            WHERE orario_previsto LIKE ?
            GROUP BY terapia_id, orario_previsto
        """, (f"{oggi} %",)).fetchall()
    finally:
        db.close()
    aderenza_oggi = round(100.0 * sum(s["preso"] for s in slot) / len(slot), 1) if slot else 0

    sensori = [
        ("sensor.farmaci_pazienti_attivi", totale, "pazienti", "mdi:account-multiple"),
        ("sensor.farmaci_scorte_in_scadenza", in_scadenza, "farmaci", "mdi:pill"),
        ("sensor.farmaci_scorte_basse", scorte_basse, "farmaci", "mdi:package-down"),
        ("sensor.farmaci_terapie_attive", terapie_attive, "terapie", "mdi:clipboard-text"),
        ("sensor.farmaci_aderenza_oggi", aderenza_oggi, "%", "mdi:check-circle"),
    ]
    for entity_id, stato, unita, icona in sensori:
        try:
            req.post(
                f"{HA_URL}/api/states/{entity_id}",
                headers=headers,
                json={"state": stato, "attributes": {"unit_of_measurement": unita, "icon": icona}},
                timeout=5
            )
        except Exception as e:
            print(f"[HA] Errore aggiornamento {entity_id}: {e}")

@app.route("/api/seed-test", methods=["GET", "POST"])
@solo_caregiver
def seed_test():
    """Popola il DB con dati di test. Usare solo in fase di sviluppo."""
    db = get_db()
    oggi = str(datetime.date.today())
    adesso = datetime.datetime.now()
    test_ora = (adesso + datetime.timedelta(minutes=3)).strftime("%H:%M")
    chat_test = TELEGRAM_CHAT_IDS[0] if TELEGRAM_CHAT_IDS else None

    try:
        # Paziente
        paz = db.execute(
            "SELECT id FROM pazienti WHERE nome='Luigi' AND cognome='Rossi'"
        ).fetchone()
        if not paz:
            db.execute("""
                INSERT INTO pazienti
                    (nome, cognome, data_nascita, profilo, telegram_chat_id, note_medico)
                VALUES (?,?,?,?,?,?)
            """, ("Luigi","Rossi","1950-03-15","assistito",chat_test,
                  "Ipertensione, diabete tipo 2. Allergia: penicillina."))
            db.commit()
            paz = db.execute(
                "SELECT id FROM pazienti WHERE nome='Luigi' AND cognome='Rossi'"
            ).fetchone()
        pid = paz["id"]

        farmaci = [
            ("020102","Tachipirina 1000mg","Paracetamolo","Compresse","1000mg","N02BE01","Angelini","#d97706"),
            ("035246","Ramipril 5mg","Ramipril","Compresse","5mg","C09AA05","Sanofi","#2563eb"),
            ("029836","Metformina 500mg","Metformina cloridrato","Compresse rivestite","500mg","A10BA02","Merck","#16a34a"),
            ("033871","Lansoprazolo 30mg","Lansoprazolo","Capsule","30mg","A02BC03","Takeda","#7c3aed"),
            ("026443","Cardioaspirina 100mg","Acido acetilsalicilico","Compresse","100mg","B01AC06","Bayer","#dc2626"),
        ]
        fids = {}
        for aic,nome,pa,forma,dos,atc,prod,col in farmaci:
            db.execute("""
                INSERT OR IGNORE INTO farmaci
                    (aic,nome,principio_attivo,forma_farmaceutica,dosaggio,atc,produttore,colore_avatar)
                VALUES (?,?,?,?,?,?,?,?)
            """, (aic,nome,pa,forma,dos,atc,prod,col))
            db.commit()
            fids[nome] = db.execute("SELECT id FROM farmaci WHERE aic=?", (aic,)).fetchone()["id"]

        terapie = [
            ("Lansoprazolo 30mg","1 capsula",["07:30"],"Stomaco vuoto prima di colazione"),
            ("Metformina 500mg","1 compressa",["08:00","13:00","20:00"],"Durante i pasti"),
            ("Ramipril 5mg","1 compressa",["08:00"],"Non interrompere senza medico"),
            ("Cardioaspirina 100mg","1 compressa",[test_ora],"Durante il pasto principale"),
            ("Tachipirina 1000mg","1 compressa",["21:00"],"Solo se necessario"),
        ]
        giorni = list(range(7))
        for nome,dose,orari,note in terapie:
            # Evita di duplicare le terapie se il seed viene lanciato più volte
            if db.execute("SELECT 1 FROM terapie WHERE paziente_id=? AND farmaco_id=? AND attiva=1",
                          (pid, fids[nome])).fetchone():
                continue
            db.execute("""
                INSERT INTO terapie
                    (paziente_id,farmaco_id,dose,orari,giorni_settimana,data_inizio,note,attiva)
                VALUES (?,?,?,?,?,?,?,1)
            """, (pid, fids[nome], dose, json.dumps(orari), json.dumps(giorni), oggi, note))

        scorte = [
            ("Lansoprazolo 30mg",28,"capsule",7),
            ("Metformina 500mg",90,"compresse",14),
            ("Ramipril 5mg",30,"compresse",7),
            ("Cardioaspirina 100mg",5,"compresse",7),
            ("Tachipirina 1000mg",20,"compresse",5),
        ]
        scad = str(datetime.date.today() + datetime.timedelta(days=180))
        for nome,qta,unita,soglia in scorte:
            db.execute("""
                INSERT OR REPLACE INTO scorte
                    (paziente_id,farmaco_id,quantita,unita,soglia_minima,scadenza,aggiornato_il)
                VALUES (?,?,?,?,?,?,datetime('now'))
            """, (pid, fids[nome], qta, unita, soglia, scad))

        db.commit()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        db.close()
    _in_background(aggiorna_sensori_ha)
    ripianifica_oggi()
    return jsonify({
        "ok": True,
        "paziente_id": pid,
        "message": f"Dati di test creati! Notifica test Cardioaspirina alle {test_ora}",
        "schermata_paziente": f"/paziente/{pid}"
    })


@app.route("/api/aifa/upload-csv", methods=["POST"])
@solo_caregiver
def upload_csv_aifa():
    """
    Carica manualmente un CSV AIFA scaricato dal tuo PC.
    Usare quando il container non riesce a scaricare da aifa.gov.it.
    Richiede di essere loggati come caregiver (cookie di sessione).

    tipo: classe_a | classe_h | carenti | trasparenza
    """
    from aifa_import import parse_csv_bytes, sorgente_per_tipo, init_tabella, _inserisci_batch

    if "file" not in request.files:
        return jsonify({"error": "Campo 'file' mancante"}), 400

    file = request.files["file"]
    tipo = request.form.get("tipo", "generico")
    source = sorgente_per_tipo(tipo)

    records, errore = parse_csv_bytes(file.read(), source)
    if errore:
        return jsonify({"error": f"CSV non leggibile: {errore}"}), 400
    if not records:
        return jsonify({"error": "Nessun farmaco valido trovato nel file"}), 400

    db = get_db()
    init_tabella(db)
    db.close()
    count = _inserisci_batch(records, source["nome"])

    db = get_db()
    tot = db.execute("SELECT COUNT(*) as n FROM aifa_lookup").fetchone()["n"]
    db.close()
    return jsonify({"ok": True, "importati": count, "totale_db": tot,
                    "message": f"{count} farmaci importati da {file.filename}"})


@app.route("/api/aifa/test-connessione", methods=["GET"])
@solo_caregiver
def test_connessione_aifa():
    """Testa se il container riesce a raggiungere i server AIFA."""
    import requests as req
    risultati = {}
    urls = [
        "https://www.aifa.gov.it/robots.txt",
        "https://www.aifa.gov.it/documents/20142/847339/elenco_medicinali_carenti.csv",
        "https://google.com",
    ]
    for url in urls:
        try:
            r = req.head(url, timeout=8, allow_redirects=True)
            risultati[url] = {"status": r.status_code, "ok": r.status_code < 400}
        except Exception as e:
            risultati[url] = {"status": None, "ok": False, "errore": str(e)[:100]}
    return jsonify(risultati)


_import_aifa_lock = threading.Lock()

@app.route("/api/aifa/import", methods=["POST"])
@solo_caregiver
def import_aifa():
    """Scarica i CSV AIFA e popola la tabella aifa_lookup."""
    if not _import_aifa_lock.acquire(blocking=False):
        return jsonify({"message": "Un import AIFA è già in corso. Controlla i log."}), 409

    def _run():
        try:
            from aifa_import import main as aifa_main
            aifa_main(skip_download=False)
            print("[AIFA-IMPORT] Completato via API.")
        except Exception as e:
            print(f"[AIFA-IMPORT] Errore: {e}")
        finally:
            _import_aifa_lock.release()
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"message": "Import AIFA avviato in background. Controlla i log."})


@app.route("/api/aifa/import-otc", methods=["POST"])
@solo_caregiver
def import_aifa_otc():
    """Importa solo il dizionario OTC hardcoded (veloce, no download)."""
    try:
        from aifa_import import init_tabella, importa_dizionario_otc
        db = get_db()
        try:
            init_tabella(db)
            count = importa_dizionario_otc(db)
        finally:
            db.close()
        return jsonify({"ok": True, "farmaci_importati": count,
                        "message": f"{count} farmaci OTC importati nel DB locale."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/aifa/stats", methods=["GET"])
def aifa_stats():
    """Statistiche della tabella aifa_lookup."""
    try:
        db = get_db()
        tot = db.execute("SELECT COUNT(*) as n FROM aifa_lookup").fetchone()
        per_fonte = db.execute(
            "SELECT fonte, COUNT(*) as n FROM aifa_lookup GROUP BY fonte"
        ).fetchall()
        db.close()
        return jsonify({
            "totale": tot["n"] if tot else 0,
            "per_fonte": {r["fonte"]: r["n"] for r in per_fonte}
        })
    except Exception:
        return jsonify({"totale": 0, "per_fonte": {}, "nota": "Tabella non ancora creata"})


@app.route("/api/sync-ha", methods=["POST"])
@solo_caregiver
def sync_ha():
    _in_background(aggiorna_sensori_ha)
    return jsonify({"message": "Sensori HA aggiornati"})

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM WEBHOOK
# ═══════════════════════════════════════════════════════════════════════════

def _chat_autorizzata(chat_id, paziente_id) -> bool:
    """Chi preme i bottoni deve essere il paziente stesso o un caregiver configurato."""
    cid = str(chat_id).strip()
    if cid in TELEGRAM_CHAT_IDS:
        return True
    db = get_db()
    r = db.execute("SELECT telegram_chat_id FROM pazienti WHERE id=?", (paziente_id,)).fetchone()
    db.close()
    return bool(r) and str(r["telegram_chat_id"] or "").strip() == cid

@app.route("/api/telegram/webhook", methods=["POST"])
def telegram_webhook():
    """
    Riceve tutti gli aggiornamenti da Telegram (messaggi + callback_query).
    Telegram manda un JSON a questo URL ad ogni interazione dell'utente.

    Gestisce:
    - callback_data "SI_<assunzione_id>"  → conferma assunzione
    - callback_data "NO_<assunzione_id>"  → rifiuto, schedula rinotifica
    - Qualsiasi messaggio testo → risposta di cortesia
    """
    # Una volta registrato il webhook con secret_token, accetta solo chiamate che lo presentano
    # (altrimenti chiunque conosca l'URL potrebbe falsificare conferme di assunzione).
    if WEBHOOK_SEGRETO_ATTIVO:
        ricevuto = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(ricevuto, WEBHOOK_SECRET):
            return jsonify({"ok": False}), 403

    update = request.get_json(silent=True)
    if not update:
        return jsonify({"ok": True})

    # ── Callback query (bottoni inline SI/NO) ──────────────────────────────
    cq = update.get("callback_query")
    if cq:
        try:
            callback_id = cq["id"]
            chat_id     = cq["message"]["chat"]["id"]
            message_id  = cq["message"]["message_id"]
        except (KeyError, TypeError):
            return jsonify({"ok": True})
        data   = cq.get("data", "")
        utente = (cq.get("from") or {}).get("first_name", "Utente")

        _risposta_callback(callback_id)   # ACK obbligatorio entro 10s

        if "_" not in data:
            return jsonify({"ok": True})

        esito, ass_id_str = data.split("_", 1)
        if esito not in ("SI", "NO") or not ass_id_str.isdigit():
            return jsonify({"ok": True})

        ass_id = int(ass_id_str)
        _processa_risposta(ass_id, esito, chat_id, message_id, utente)
        return jsonify({"ok": True})

    # ── Messaggio testo (es. /start o messaggio libero) ───────────────────
    msg = update.get("message")
    if msg and msg.get("chat"):
        chat_id = msg["chat"]["id"]
        testo   = (msg.get("text") or "").strip()
        nome    = (msg.get("from") or {}).get("first_name", "")

        if testo.startswith("/start"):
            # Controlla se il paziente è già registrato nel DB
            db = get_db()
            paz = db.execute(
                "SELECT nome, cognome FROM pazienti WHERE telegram_chat_id=? AND attivo=1",
                (str(chat_id),)
            ).fetchone()
            db.close()

            if paz:
                # Paziente già registrato — benvenuto personalizzato
                _invia_messaggio(chat_id,
                    f"👋 Bentornato/a <b>{_e(paz['nome'])}</b>!\n\n"
                    f"💊 <b>PillolApp</b> è pronto.\n"
                    f"Riceverai qui i promemoria per i tuoi farmaci.\n\n"
                    f"📋 Usa /stato per vedere le terapie di oggi.\n"
                    f"ℹ️ Rispondi ai messaggi con ✅ <b>SÌ</b> o ❌ <b>No</b> "
                    f"per confermare l'assunzione."
                )
            else:
                # Nuovo utente — mostra il Chat ID da comunicare al caregiver
                _invia_messaggio(chat_id,
                    f"👋 Ciao <b>{_e(nome)}</b>!\n\n"
                    f"💊 Benvenuto in <b>PillolApp</b> — il tuo assistente per la gestione "
                    f"delle terapie farmacologiche.\n\n"
                    f"─────────────────────\n"
                    f"🔢 <b>Il tuo Chat ID è:</b>\n\n"
                    f"<code>{chat_id}</code>\n\n"
                    f"─────────────────────\n"
                    f"📲 <b>Come procedere:</b>\n"
                    f"Copia questo codice e comunicalo al tuo caregiver. "
                    f"Lo inserirà nella tua scheda paziente per attivarti le notifiche.\n\n"
                    f"Una volta configurato riceverai qui i promemoria per i tuoi farmaci "
                    f"e potrai confermare le assunzioni con un semplice tocco. 💪"
                )
        elif testo.startswith("/stato"):
            _invia_stato(chat_id)
        else:
            _invia_messaggio(chat_id,
                "💊 <b>PillolApp</b>\n\n"
                "Usa /start per registrarti\n"
                "Usa /stato per vedere le terapie di oggi."
            )

    return jsonify({"ok": True})


def _processa_risposta(ass_id: int, esito: str, chat_id: int,
                       message_id: int, utente: str):
    """Registra la risposta SI/NO e aggiorna il messaggio Telegram."""
    pid = _paziente_di_assunzione(ass_id)
    if pid is None:
        _invia_messaggio(chat_id, "⚠️ Assunzione non trovata.")
        return
    if not _chat_autorizzata(chat_id, pid):
        print(f"[TG] Risposta ignorata: chat {chat_id} non autorizzata per l'assunzione {ass_id}")
        return

    res = _registra_esito(ass_id, esito, "telegram")
    if res["stato"] == "non_trovata":
        _invia_messaggio(chat_id, "⚠️ Assunzione non trovata.")
        return
    if res["stato"] == "gia_confermato":
        _modifica_messaggio(chat_id, message_id,
            f"✅ <b>{_e(res['farmaco_nome'])}</b> già confermato!")
        return

    if esito == "SI":
        testo_edit = (
            f"✅ <b>{_e(res['farmaco_nome'])}</b> — {_e(res['dose'])}\n"
            f"👤 {_e(res['paz_nome'])} {_e(res['paz_cognome'])}\n"
            f"🕐 {_e(res['orario_previsto'])}\n\n"
            f"<i>Confermato da {_e(utente)}</i> ✓"
        )
        _modifica_messaggio(chat_id, message_id, testo_edit)
    else:  # NO
        _modifica_messaggio(chat_id, message_id,
            f"⏰ <b>{_e(res['farmaco_nome'])}</b> — non ancora preso.\n"
            f"Riceverai un nuovo promemoria tra {RITARDO_MIN} minuti."
        )


def _risposta_callback(callback_query_id: str):
    """ACK obbligatorio per la callback_query — Telegram lo richiede entro 10s."""
    try:
        import requests as req
        req.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id},
            timeout=5
        )
    except Exception as e:
        print(f"[TG] ACK callback fallito: {e}")


def _invia_messaggio(chat_id: int, testo: str, keyboard=None):
    """Invia un messaggio Telegram."""
    try:
        import requests as req
        payload = {
            "chat_id":    chat_id,
            "text":       testo,
            "parse_mode": "HTML"
        }
        if keyboard:
            payload["reply_markup"] = json.dumps({"inline_keyboard": keyboard})
        req.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload, timeout=8
        )
    except Exception as e:
        print(f"[TG] Invio messaggio fallito: {e}")


def _modifica_messaggio(chat_id: int, message_id: int, testo: str):
    """Modifica un messaggio esistente (rimuove i bottoni e aggiorna il testo)."""
    try:
        import requests as req
        req.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
            json={
                "chat_id":      chat_id,
                "message_id":   message_id,
                "text":         testo,
                "parse_mode":   "HTML",
                "reply_markup": json.dumps({"inline_keyboard": []})
            },
            timeout=8
        )
    except Exception as e:
        print(f"[TG] Modifica messaggio fallita: {e}")


def _invia_stato(chat_id: int):
    """Risponde a /stato con le terapie di oggi del paziente collegato al chat_id."""
    db = get_db()
    paziente = db.execute(
        "SELECT * FROM pazienti WHERE telegram_chat_id=? AND attivo=1",
        (str(chat_id),)
    ).fetchone()
    # Modalità solo: l'unico paziente è l'utente stesso, riconosciuto dal suo chat_id configurato
    if not paziente and MODALITA == "solo" and str(chat_id) in TELEGRAM_CHAT_IDS:
        paziente = db.execute("SELECT * FROM pazienti WHERE attivo=1 ORDER BY id LIMIT 1").fetchone()

    if not paziente:
        db.close()
        _invia_messaggio(chat_id,
            "⚠️ Chat ID non associato a nessun paziente.\n"
            "Configuralo nelle impostazioni dell'app."
        )
        return

    oggi_d = datetime.date.today()
    oggi = str(oggi_d)
    giorno_sett = oggi_d.weekday()

    terapie = db.execute("""
        SELECT t.*, f.nome as farmaco_nome
        FROM terapie t JOIN farmaci f ON t.farmaco_id = f.id
        WHERE t.paziente_id=? AND t.attiva=1
          AND (t.data_inizio IS NULL OR t.data_inizio <= ?)
          AND (t.data_fine IS NULL OR t.data_fine >= ?)
    """, (paziente["id"], oggi, oggi)).fetchall()

    log_oggi = db.execute("""
        SELECT a.*, t.farmaco_id FROM assunzioni a
        JOIN terapie t ON a.terapia_id = t.id
        WHERE t.paziente_id=? AND a.orario_previsto LIKE ?
    """, (paziente["id"], f"{oggi} %")).fetchall()
    db.close()

    prese = {(a["farmaco_id"], a["orario_previsto"].split(" ")[-1])
             for a in log_oggi if a["esito"] == "SI"}

    righe = [f"📋 <b>Terapie di oggi — {_e(paziente['nome'])}</b>\n"]
    for t in terapie:
        orari = json.loads(t["orari"] or "[]")
        giorni = json.loads(t["giorni_settimana"] or "null") or list(range(7))
        if giorno_sett not in giorni:
            continue
        for o in orari:
            icona = "✅" if (t["farmaco_id"], o) in prese else "⏳"
            righe.append(f"{icona} <b>{_e(t['farmaco_nome'])}</b> — {_e(t['dose'])} ore {o}")

    if len(righe) == 1:
        _invia_messaggio(chat_id, "📋 Nessuna terapia in programma per oggi.")
        return
    _invia_messaggio(chat_id, "\n".join(righe))


# ── Setup webhook (chiamato all'avvio se URL configurato) ──────────────────

def registra_webhook(public_url: str) -> bool:
    """
    Registra il webhook su Telegram, con secret_token per autenticare le chiamate.
    Chiamare con l'URL pubblico del server, es. https://farmaci.mabalu.it
    """
    global WEBHOOK_SEGRETO_ATTIVO
    if not TELEGRAM_BOT_TOKEN:
        print("[TG] Token non configurato — webhook non registrato.")
        return False
    webhook_url = f"{public_url.rstrip('/')}/api/telegram/webhook"
    try:
        import requests as req
        r = req.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
            json={"url": webhook_url, "allowed_updates": ["message", "callback_query"],
                  "secret_token": WEBHOOK_SECRET},
            timeout=10
        )
        data = r.json()
        if data.get("ok"):
            WEBHOOK_SEGRETO_ATTIVO = True
            print(f"[TG] Webhook registrato: {webhook_url}")
            return True
        print(f"[TG] Errore registrazione webhook: {data}")
    except Exception as e:
        print(f"[TG] Registrazione webhook fallita: {e}")
    return False


@app.route("/api/telegram/setup-webhook", methods=["POST"])
@solo_caregiver
def setup_webhook_endpoint():
    """Endpoint per registrare il webhook manualmente via POST."""
    data = _json_body()
    url  = str(data.get("url", "")).strip()
    if not url:
        return jsonify({"error": "url obbligatorio"}), 400
    if not url.lower().startswith("https://"):
        return jsonify({"error": "Telegram richiede un URL https"}), 400
    if not registra_webhook(url):
        return jsonify({"error": "Registrazione del webhook non riuscita (vedi log)"}), 502
    return jsonify({"message": f"Webhook registrato per {url}"})


@app.route("/api/telegram/info", methods=["GET"])
@solo_caregiver
def telegram_info():
    """Mostra info sul bot e stato del webhook attuale."""
    if not TELEGRAM_BOT_TOKEN:
        return jsonify({"error": "Token non configurato"})
    try:
        import requests as req
        bot  = req.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe",
                       timeout=5).json()
        hook = req.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo",
                       timeout=5).json()
        return jsonify({
            "bot":     bot.get("result", {}),
            "webhook": hook.get("result", {}),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════
# AVVIO
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()

    # Utente caregiver di default: la password arriva dalla configurazione. Se manca ne
    # viene generata una casuale, mostrata UNA sola volta (mai una password fissa nel codice).
    _pwd_iniziale = CAREGIVER_PASSWORD or secrets.token_urlsafe(9)
    if crea_utente_se_non_esiste("caregiver", _pwd_iniziale, "caregiver") and not CAREGIVER_PASSWORD:
        print(f"[AUTH] 'caregiver_password' non configurata: password iniziale generata = {_pwd_iniziale}  "
              f"(cambiala subito dalla dashboard)")
    _avvisa_credenziali_deboli()
    print(f"[AUTH] Modalità: {MODALITA} | caregiver username: caregiver")

    if MODALITA == "solo":
        # In modalità solo: crea automaticamente paziente "principale"
        # collegato all'utente caregiver stesso
        db = get_db()
        paz = db.execute(
            "SELECT id FROM pazienti WHERE attivo=1 LIMIT 1"
        ).fetchone()
        if not paz:
            db.execute("""
                INSERT INTO pazienti (nome, cognome, profilo, attivo)
                VALUES ('Utente', 'Principale', 'autosufficiente', 1)
            """)
            db.commit()
            paz_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            # Collega utente caregiver al paziente
            db.execute(
                "UPDATE utenti SET paziente_id=? WHERE username='caregiver'",
                (paz_id,)
            )
            db.commit()
            print(f"[AUTH] Modalità SOLO: paziente principale creato (id={paz_id})")
        db.close()

    avvia_scheduler(notifica_mgr, RITARDO_MIN, MAX_TENTATIVI, chat_ids=TELEGRAM_CHAT_IDS, modalita=MODALITA)
    if PUBLIC_URL:
        registra_webhook(PUBLIC_URL)
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)
