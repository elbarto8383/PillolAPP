import os
import json
import datetime
import hashlib
from flask import Flask, request, jsonify, render_template, redirect, url_for, session
from flask_cors import CORS
from flask_login import LoginManager, UserMixin, login_user, logout_user, \
                        login_required, current_user
from apscheduler.schedulers.background import BackgroundScheduler

from database import init_db, get_db
from notifiche import NotificaManager
from aifa import lookup_aic
from scheduler import avvia_scheduler

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "farmaci-manager-secret-2026-mabalu")
CORS(app, resources={r"/api/*": {"origins": "*"}})

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

def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def crea_utente_se_non_esiste(username, password, ruolo, paziente_id=None):
    """Crea utente al primo avvio se non esiste."""
    db = get_db()
    ex = db.execute("SELECT id FROM utenti WHERE username=?", (username,)).fetchone()
    if not ex:
        db.execute("""
            INSERT INTO utenti (username, password, ruolo, paziente_id)
            VALUES (?, ?, ?, ?)
        """, (username, hash_password(password), ruolo, paziente_id))
        db.commit()
        print(f"[AUTH] Utente creato: {username} ({ruolo})")
    db.close()

# ── Config da env ────────────────────────────────────────────────────────────
HA_URL              = os.environ.get("HA_URL", "http://192.168.1.83:8123")
HA_TOKEN            = os.environ.get("HA_TOKEN", "")
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_IDS   = [x for x in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if x]
ALEXA_ABILITATA     = os.environ.get("ALEXA_ABILITATA", "true").lower() == "true"
ALEXA_ENTITY_ID     = os.environ.get("ALEXA_ENTITY_ID", "media_player.alexa")
RITARDO_MIN         = int(os.environ.get("NOTIFICA_RITARDO_MIN", 15))
MAX_TENTATIVI       = int(os.environ.get("NOTIFICA_MAX_TENTATIVI", 3))
CAREGIVER_PASSWORD  = os.environ.get("CAREGIVER_PASSWORD", "admin1234")
MODALITA            = os.environ.get("MODALITA_UTILIZZO", "famiglia")  # "solo" | "famiglia"

notifica_mgr = NotificaManager(
    telegram_token=TELEGRAM_BOT_TOKEN,
    chat_ids=TELEGRAM_CHAT_IDS,
    ha_url=HA_URL,
    ha_token=HA_TOKEN,
    alexa_entity=ALEXA_ENTITY_ID,
    alexa_abilitata=ALEXA_ABILITATA,
)

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

        db = get_db()
        row = db.execute(
            "SELECT * FROM utenti WHERE username=? AND attivo=1", (username,)
        ).fetchone()
        db.close()

        if row and row["password"] == hash_password(password):
            utente = Utente(row["id"], row["username"], row["ruolo"], row["paziente_id"])
            login_user(utente, remember=True)
            return _redirect_dopo_login()
        else:
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

@app.route("/static/<path:path>")
def static_files(path):
    return send_from_directory("static", path)

# ═══════════════════════════════════════════════════════════════════════════
# API — PAZIENTI
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/utenti", methods=["GET"])
@login_required
def get_utenti():
    if not current_user.is_caregiver:
        return jsonify({"error": "Non autorizzato"}), 403
    db = get_db()
    rows = db.execute("""
        SELECT u.id, u.username, u.ruolo, u.attivo, u.creato_il,
               p.nome, p.cognome
        FROM utenti u
        LEFT JOIN pazienti p ON u.paziente_id = p.id
        ORDER BY u.ruolo, u.username
    """).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/utenti", methods=["POST"])
@login_required
def crea_utente():
    if not current_user.is_caregiver:
        return jsonify({"error": "Non autorizzato"}), 403
    data = request.get_json()
    username  = data.get("username", "").strip()
    password  = data.get("password", "").strip()
    ruolo     = data.get("ruolo", "paziente")
    paziente_id = data.get("paziente_id")

    if not username or not password:
        return jsonify({"error": "Username e password obbligatori"}), 400

    db = get_db()
    try:
        db.execute("""
            INSERT INTO utenti (username, password, ruolo, paziente_id)
            VALUES (?, ?, ?, ?)
        """, (username, hash_password(password), ruolo, paziente_id))
        db.commit()
        uid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.close()
        return jsonify({"id": uid, "message": f"Utente '{username}' creato"}), 201
    except Exception as e:
        db.close()
        return jsonify({"error": str(e)}), 400

@app.route("/api/utenti/<int:uid>/password", methods=["PUT"])
@login_required
def cambia_password(uid):
    if not current_user.is_caregiver:
        return jsonify({"error": "Non autorizzato"}), 403
    data = request.get_json()
    nuova = data.get("password", "").strip()
    if not nuova:
        return jsonify({"error": "Password vuota"}), 400
    db = get_db()
    db.execute("UPDATE utenti SET password=? WHERE id=?", (hash_password(nuova), uid))
    db.commit()
    db.close()
    return jsonify({"message": "Password aggiornata"})

@app.route("/api/utenti/<int:uid>", methods=["DELETE"])
@login_required
def elimina_utente(uid):
    if not current_user.is_caregiver:
        return jsonify({"error": "Non autorizzato"}), 403
    db = get_db()
    db.execute("UPDATE utenti SET attivo=0 WHERE id=?", (uid,))
    db.commit()
    db.close()
    return jsonify({"message": "Utente disattivato"})

@app.route("/api/pazienti", methods=["GET"])
@login_required
def get_pazienti():
    if not current_user.is_caregiver:
        return jsonify({"error": "Non autorizzato"}), 403
    db = get_db()
    rows = db.execute("SELECT * FROM pazienti WHERE attivo = 1 ORDER BY cognome").fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/pazienti", methods=["POST"])
def crea_paziente():
    data = request.get_json()
    required = ["nome", "cognome"]
    for f in required:
        if not data.get(f):
            return jsonify({"error": f"Campo obbligatorio mancante: {f}"}), 400

    profilo = data.get("profilo", "assistito")
    if profilo not in ("autosufficiente", "assistito"):
        return jsonify({"error": "profilo deve essere 'autosufficiente' o 'assistito'"}), 400

    db = get_db()
    c = db.execute("""
        INSERT INTO pazienti (nome, cognome, data_nascita, profilo, telegram_chat_id, note_medico)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        data["nome"], data["cognome"],
        data.get("data_nascita"), profilo,
        data.get("telegram_chat_id"), data.get("note_medico")
    ))
    db.commit()
    nuovo_id = c.lastrowid
    db.close()
    return jsonify({"id": nuovo_id, "message": "Paziente creato"}), 201

@app.route("/api/pazienti/<int:pid>", methods=["PUT"])
def aggiorna_paziente(pid):
    data = request.get_json()
    db = get_db()
    db.execute("""
        UPDATE pazienti SET nome=?, cognome=?, data_nascita=?, profilo=?,
        telegram_chat_id=?, note_medico=?, aggiornato_il=datetime('now')
        WHERE id=?
    """, (
        data.get("nome"), data.get("cognome"), data.get("data_nascita"),
        data.get("profilo"), data.get("telegram_chat_id"), data.get("note_medico"), pid
    ))
    db.commit()
    db.close()
    return jsonify({"message": "Paziente aggiornato"})

@app.route("/api/pazienti/<int:pid>", methods=["DELETE"])
def elimina_paziente(pid):
    db = get_db()
    db.execute("UPDATE pazienti SET attivo=0 WHERE id=?", (pid,))
    db.commit()
    db.close()
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

@app.route("/api/farmaci/ocr-foto-b64", methods=["POST"])
def ocr_foto_b64():
    """Riceve immagine come base64 dataURL e usa pytesseract per leggere l'AIC."""
    try:
        import re, base64
        from PIL import Image
        import io

        data = request.get_json()
        if not data or "immagine" not in data:
            return jsonify({"error": "Campo 'immagine' mancante", "aic": None}), 400

        data_url = data["immagine"]
        # Rimuovi prefisso "data:image/jpeg;base64,"
        if "," in data_url:
            data_url = data_url.split(",", 1)[1]

        img_bytes = base64.b64decode(data_url)
        print(f"[OCR-B64] Immagine: {len(img_bytes)} bytes")

        if len(img_bytes) < 100:
            return jsonify({"error": f"Immagine troppo piccola ({len(img_bytes)} bytes)", "aic": None}), 400

        img = Image.open(io.BytesIO(img_bytes))
        img.load()
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        print(f"[OCR-B64] Dimensione: {img.size} {img.mode}")

        try:
            import pytesseract
            testo = pytesseract.image_to_string(img, lang="ita+eng")
            if not testo.strip():
                testo = pytesseract.image_to_string(img, lang="eng")
        except ImportError:
            return jsonify({"error": "pytesseract non installato", "aic": None})
        except Exception as e:
            import traceback
            print(f"[OCR-B64] Errore pytesseract: {traceback.format_exc()}")
            return jsonify({"error": str(e), "aic": None}), 500

        print(f"[OCR-B64] Testo: {testo[:300]}")

        patterns = [
            r'A\.?I\.?C\.?\s*[:\-]?\s*0?(\d{6,9})',
            r'\bAIC\s*[:\-]?\s*0?(\d{6,9})',
            r'\bA0?(\d{8})\b',
            r'\b(0\d{8})\b',
            r'\b(0\d{5})\b',
        ]
        for pat in patterns:
            m = re.search(pat, testo, re.IGNORECASE)
            if m:
                aic = re.sub(r'[^0-9]', '', m.group(1))
                if len(aic) >= 6:
                    print(f"[OCR-B64] AIC trovato: {aic}")
                    return jsonify({"aic": aic, "testo": testo[:200]})

        return jsonify({"aic": None, "testo": testo[:200]})

    except Exception as e:
        import traceback
        print(f"[OCR-B64] Errore: {traceback.format_exc()}")
        return jsonify({"error": str(e), "aic": None}), 500


@app.route("/api/farmaci/ocr-foto", methods=["POST"])
def ocr_foto():
    """Riceve una foto e usa pytesseract lato server per leggere l'AIC."""
    if "foto" not in request.files:
        return jsonify({"error": "Nessuna foto ricevuta", "aic": None}), 400
    try:
        import re
        from PIL import Image
        import io

        foto = request.files["foto"]
        img_bytes = foto.read()

        print(f"[OCR] Foto ricevuta: {len(img_bytes)} bytes, type: {foto.content_type}")

        if len(img_bytes) < 100:
            return jsonify({"error": f"Immagine troppo piccola ({len(img_bytes)} bytes)", "aic": None}), 400

        try:
            img = Image.open(io.BytesIO(img_bytes))
            img.load()  # forza caricamento completo
            # Converti in RGB se necessario
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
        except Exception as e:
            import traceback
            print(f"[OCR] Errore apertura immagine: {traceback.format_exc()}")
            return jsonify({"error": f"Immagine non valida: {str(e)}", "aic": None}), 400

        print(f"[OCR] Immagine: {img.size} {img.mode}")

        try:
            import pytesseract
            testo = pytesseract.image_to_string(img, lang="ita+eng")
            if not testo.strip():
                testo = pytesseract.image_to_string(img, lang="eng")
        except ImportError:
            return jsonify({"error": "pytesseract non installato", "aic": None})
        except Exception as e:
            import traceback
            print(f"[OCR] Errore pytesseract: {traceback.format_exc()}")
            return jsonify({"error": f"OCR fallito: {str(e)}", "aic": None}), 500

        print(f"[OCR] Testo ({len(testo)} chars): {testo[:300]}")

        patterns = [
            r'A\.?I\.?C\.?\s*[:\-]?\s*0?(\d{6,9})',
            r'\bAIC\s*[:\-]?\s*0?(\d{6,9})',
            r'\bA0?(\d{8})\b',
            r'\b(0\d{8})\b',
            r'\b(0\d{5})\b',
        ]
        for pat in patterns:
            m = re.search(pat, testo, re.IGNORECASE)
            if m:
                aic = re.sub(r'[^0-9]', '', m.group(1))
                if len(aic) >= 6:
                    print(f"[OCR] AIC trovato: {aic}")
                    return jsonify({"aic": aic, "testo": testo[:200]})

        return jsonify({"aic": None, "testo": testo[:200]})

    except Exception as e:
        import traceback
        print(f"[OCR] Errore generico: {traceback.format_exc()}")
        return jsonify({"error": str(e), "aic": None}), 500


@app.route("/api/farmaci/test-aic/<aic>", methods=["GET"])
def test_aic(aic):
    """Endpoint GET per testare il lookup AIC direttamente dal browser."""
    from aifa import lookup_aic
    risultato = lookup_aic(aic)
    return jsonify({
        "aic_input": aic,
        "risultato": risultato,
        "trovato": risultato is not None and risultato.get("nome") != f"Farmaco AIC {aic[:6]}"
    })


@app.route("/api/farmaci/lookup", methods=["POST"])
def lookup_farmaco():
    """Cerca farmaco per AIC (da OCR o input manuale) via AIFA, con cache locale."""
    data = request.get_json()
    aic = data.get("aic", "").strip()
    if not aic:
        return jsonify({"error": "AIC obbligatorio"}), 400

    db = get_db()
    # 1. Cache locale
    cached = db.execute("SELECT payload_json FROM cache_aic WHERE aic=?", (aic,)).fetchone()
    if cached:
        db.close()
        return jsonify({"source": "cache", "farmaco": json.loads(cached["payload_json"])})

    # 2. Lookup AIFA
    farmaco_data = lookup_aic(aic)
    if not farmaco_data:
        db.close()
        return jsonify({"error": "Farmaco non trovato per questo AIC"}), 404

    # 3. Salva in cache e in anagrafica
    db.execute(
        "INSERT OR IGNORE INTO cache_aic (aic, payload_json) VALUES (?, ?)",
        (aic, json.dumps(farmaco_data))
    )
    db.execute("""
        INSERT OR IGNORE INTO farmaci (aic, nome, nome_commerciale, principio_attivo,
            forma_farmaceutica, dosaggio, atc, produttore, foglietto_url,
            immagine_url, immagine_locale, colore_avatar)
        VALUES (:aic, :nome, :nome_commerciale, :principio_attivo,
            :forma_farmaceutica, :dosaggio, :atc, :produttore, :foglietto_url,
            :immagine_url, :immagine_locale, :colore_avatar)
    """, farmaco_data)
    db.commit()

    farmaco_row = db.execute("SELECT * FROM farmaci WHERE aic=?", (aic,)).fetchone()
    db.close()
    return jsonify({"source": "aifa", "farmaco": dict(farmaco_row)})

@app.route("/api/farmaci/<int:fid>/immagine", methods=["POST"])
def upload_immagine_farmaco(fid):
    """
    Upload foto confezione scattata dall'utente (livello 1).
    Accetta multipart/form-data con campo 'foto'.
    """
    from aifa import salva_immagine_utente
    if "foto" not in request.files:
        return jsonify({"error": "Campo 'foto' mancante"}), 400

    file = request.files["foto"]
    ext  = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "jpg"
    if ext not in ("jpg", "jpeg", "png", "webp"):
        return jsonify({"error": "Formato non supportato (jpg/png/webp)"}), 400

    path = salva_immagine_utente(fid, file.read(), ext)
    db = get_db()
    db.execute(
        "UPDATE farmaci SET immagine_locale=?, immagine_url=NULL WHERE id=?",
        (path, fid)
    )
    db.commit()
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
    data = request.get_json()
    if not data.get("nome"):
        return jsonify({"error": "nome obbligatorio"}), 400
    db = get_db()
    c = db.execute("""
        INSERT INTO farmaci (aic, nome, nome_commerciale, principio_attivo,
            forma_farmaceutica, dosaggio, atc)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        data.get("aic"), data["nome"], data.get("nome_commerciale"),
        data.get("principio_attivo"), data.get("forma_farmaceutica"),
        data.get("dosaggio"), data.get("atc")
    ))
    db.commit()
    nuovo_id = c.lastrowid
    db.close()
    return jsonify({"id": nuovo_id, "message": "Farmaco aggiunto"}), 201

# ═══════════════════════════════════════════════════════════════════════════
# API — TERAPIE
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/terapie/<int:paziente_id>", methods=["GET"])
def get_terapie(paziente_id):
    db = get_db()
    rows = db.execute("""
        SELECT t.*, f.nome as farmaco_nome, f.principio_attivo, f.forma_farmaceutica
        FROM terapie t JOIN farmaci f ON t.farmaco_id = f.id
        WHERE t.paziente_id = ? AND t.attiva = 1
        ORDER BY t.orari
    """, (paziente_id,)).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/terapie", methods=["POST"])
def crea_terapia():
    data = request.get_json()
    required = ["paziente_id", "dose", "orari"]
    for f in required:
        if not data.get(f):
            return jsonify({"error": f"Campo obbligatorio: {f}"}), 400

    orari = data["orari"] if isinstance(data["orari"], str) else json.dumps(data["orari"])
    giorni = data.get("giorni_settimana")
    giorni = giorni if isinstance(giorni, str) else json.dumps(giorni) if giorni else None

    db = get_db()

    # Se farmaco_id non è presente, proviamo a salvare/trovare il farmaco dall'AIC o dal nome
    farmaco_id = data.get("farmaco_id")
    if not farmaco_id:
        aic  = data.get("aic") or data.get("farmaco_aic")
        nome = data.get("nome") or data.get("farmaco_nome", "Farmaco sconosciuto")
        pa   = data.get("principio_attivo")
        atc  = data.get("atc")
        colore = data.get("colore_avatar", "#2563eb")

        if aic:
            # Cerca se già esiste nel DB farmaci
            ex = db.execute("SELECT id FROM farmaci WHERE aic=?", (aic[:6],)).fetchone()
            if ex:
                farmaco_id = ex["id"]
            else:
                # Inserisci il farmaco
                c2 = db.execute("""
                    INSERT INTO farmaci (aic, nome, nome_commerciale, principio_attivo, atc, colore_avatar)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (aic[:6], nome, nome, pa, atc, colore))
                db.commit()
                farmaco_id = c2.lastrowid
        elif nome:
            # Solo nome, senza AIC
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
        db.close()
        return jsonify({"error": "farmaco_id mancante e impossibile determinare il farmaco"}), 400

    c = db.execute("""
        INSERT INTO terapie (paziente_id, farmaco_id, dose, orari, giorni_settimana,
            durata_giorni, data_inizio, data_fine, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["paziente_id"], farmaco_id, data["dose"],
        orari, giorni, data.get("durata_giorni"),
        data.get("data_inizio", str(datetime.date.today())),
        data.get("data_fine"), data.get("note")
    ))
    db.commit()
    nuovo_id = c.lastrowid
    db.close()
    avvia_scheduler(notifica_mgr, RITARDO_MIN, MAX_TENTATIVI)
    return jsonify({"id": nuovo_id, "message": "Terapia creata"}), 201

@app.route("/api/terapie/<int:tid>", methods=["PUT"])
def aggiorna_terapia(tid):
    data = request.get_json()
    orari = data.get("orari")
    if orari and not isinstance(orari, str):
        orari = json.dumps(orari)
    db = get_db()
    db.execute("""
        UPDATE terapie SET dose=?, orari=?, giorni_settimana=?, durata_giorni=?,
        data_fine=?, note=?, aggiornato_il=datetime('now')
        WHERE id=?
    """, (
        data.get("dose"), orari, data.get("giorni_settimana"),
        data.get("durata_giorni"), data.get("data_fine"), data.get("note"), tid
    ))
    db.commit()
    db.close()
    avvia_scheduler(notifica_mgr, RITARDO_MIN, MAX_TENTATIVI)
    return jsonify({"message": "Terapia aggiornata"})

@app.route("/api/terapie/<int:tid>", methods=["DELETE"])
def elimina_terapia(tid):
    db = get_db()
    db.execute("UPDATE terapie SET attiva=0 WHERE id=?", (tid,))
    db.commit()
    db.close()
    return jsonify({"message": "Terapia disattivata"})

# ═══════════════════════════════════════════════════════════════════════════
# API — SCORTE
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/scorte/<int:paziente_id>", methods=["GET"])
def get_scorte(paziente_id):
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
    data = request.get_json()
    db = get_db()
    db.execute("""
        INSERT INTO scorte (paziente_id, farmaco_id, quantita, unita, soglia_minima, scadenza, lotto)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(paziente_id, farmaco_id) DO UPDATE SET
            quantita=excluded.quantita, unita=excluded.unita,
            soglia_minima=excluded.soglia_minima, scadenza=excluded.scadenza,
            lotto=excluded.lotto, aggiornato_il=datetime('now')
    """, (
        data["paziente_id"], data["farmaco_id"], data["quantita"],
        data.get("unita", "compresse"), data.get("soglia_minima", 7),
        data.get("scadenza"), data.get("lotto")
    ))
    db.commit()
    db.close()
    aggiorna_sensori_ha()
    return jsonify({"message": "Scorta aggiornata"})

# ═══════════════════════════════════════════════════════════════════════════
# API — ASSUNZIONI (conferma dal paziente)
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/assunzioni/conferma", methods=["POST"])
def conferma_assunzione():
    """Endpoint chiamato da Telegram callback, Alexa o PWA del paziente."""
    data = request.get_json()
    assunzione_id = data.get("assunzione_id")
    esito = data.get("esito")  # 'SI' | 'NO'

    if not assunzione_id or esito not in ("SI", "NO"):
        return jsonify({"error": "assunzione_id e esito (SI/NO) obbligatori"}), 400

    db = get_db()
    db.execute("""
        UPDATE assunzioni SET esito=?, orario_risposta=datetime('now'), canale=?
        WHERE id=?
    """, (esito, data.get("canale", "pwa"), assunzione_id))

    if esito == "SI":
        # Scala la scorta di 1 unità
        ass = db.execute("""
            SELECT t.paziente_id, t.farmaco_id FROM assunzioni a
            JOIN terapie t ON a.terapia_id = t.id WHERE a.id=?
        """, (assunzione_id,)).fetchone()
        if ass:
            db.execute("""
                UPDATE scorte SET quantita = MAX(0, quantita - 1),
                aggiornato_il = datetime('now')
                WHERE paziente_id=? AND farmaco_id=?
            """, (ass["paziente_id"], ass["farmaco_id"]))

    db.commit()
    db.close()
    aggiorna_sensori_ha()
    return jsonify({"message": f"Assunzione registrata: {esito}"})

@app.route("/api/assunzioni/<int:paziente_id>", methods=["GET"])
def get_assunzioni(paziente_id):
    giorni = request.args.get("giorni", 7)
    db = get_db()
    rows = db.execute("""
        SELECT a.*, t.dose, f.nome as farmaco_nome
        FROM assunzioni a
        JOIN terapie t ON a.terapia_id = t.id
        JOIN farmaci f ON t.farmaco_id = f.id
        WHERE t.paziente_id = ?
        AND a.creato_il >= datetime('now', ? || ' days')
        ORDER BY a.orario_previsto DESC
    """, (paziente_id, f"-{giorni}")).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])

# ═══════════════════════════════════════════════════════════════════════════
# API — ASTUCCIO
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/astuccio/<int:paziente_id>", methods=["GET"])
def get_astuccio(paziente_id):
    settimana = request.args.get("settimana", datetime.date.today().isocalendar()[1])
    anno = request.args.get("anno", datetime.date.today().year)
    settimana_iso = f"{anno}-W{str(settimana).zfill(2)}"
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
    data = request.get_json()
    paziente_id = data["paziente_id"]
    settimana_iso = data.get("settimana_iso",
        f"{datetime.date.today().year}-W{str(datetime.date.today().isocalendar()[1]).zfill(2)}")

    db = get_db()
    terapie = db.execute("""
        SELECT * FROM terapie WHERE paziente_id=? AND attiva=1
    """, (paziente_id,)).fetchall()

    FASCIA_MAP = {"mattina": "M", "pranzo": "P", "sera": "S", "notte": "N"}
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
                fascia = ora_a_fascia(ora)
                key = (giorno, fascia)
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

    db.commit()
    db.close()
    return jsonify({"message": f"Astuccio generato per {settimana_iso}"})

@app.route("/api/astuccio/slot/<int:slot_id>/carica", methods=["POST"])
def marca_slot_caricato(slot_id):
    data = request.get_json()
    caricato = 1 if data.get("caricato", True) else 0
    db = get_db()
    db.execute("""
        UPDATE astuccio_slot SET caricato=?, caricato_il=datetime('now')
        WHERE id=?
    """, (caricato, slot_id))
    db.commit()
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
    oggi = str(datetime.date.today())
    tra_7 = str(datetime.date.today() + datetime.timedelta(days=7))

    totale = db.execute("SELECT COUNT(*) as n FROM pazienti WHERE attivo=1").fetchone()["n"]
    in_scadenza = db.execute(
        "SELECT COUNT(*) as n FROM scorte WHERE scadenza IS NOT NULL AND scadenza <= ?", (tra_7,)
    ).fetchone()["n"]
    terapie_attive = db.execute(
        "SELECT COUNT(*) as n FROM terapie WHERE attiva=1"
    ).fetchone()["n"]
    aderenza_oggi = db.execute("""
        SELECT
            ROUND(100.0 * SUM(CASE WHEN esito='SI' THEN 1 ELSE 0 END) /
                  MAX(COUNT(*), 1), 1) as pct
        FROM assunzioni WHERE date(creato_il)=?
    """, (oggi,)).fetchone()["pct"] or 0
    db.close()

    sensori = [
        ("sensor.farmaci_pazienti_attivi", totale, "pazienti", "mdi:account-multiple"),
        ("sensor.farmaci_scorte_in_scadenza", in_scadenza, "farmaci", "mdi:pill"),
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
def seed_test():
    """Popola il DB con dati di test. Usare solo in fase di sviluppo."""
    import datetime, json
    db = get_db()
    oggi = str(datetime.date.today())
    adesso = datetime.datetime.now()
    test_ora = (adesso + datetime.timedelta(minutes=3)).strftime("%H:%M")

    try:
        # Paziente
        db.execute("""
            INSERT OR IGNORE INTO pazienti
                (nome, cognome, data_nascita, profilo, telegram_chat_id, note_medico)
            VALUES (?,?,?,?,?,?)
        """, ("Luigi","Rossi","1950-03-15","assistito","44413116",
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
        db.close()
        aggiorna_sensori_ha()
        avvia_scheduler(notifica_mgr, RITARDO_MIN, MAX_TENTATIVI)
        return jsonify({
            "ok": True,
            "paziente_id": pid,
            "message": f"Dati di test creati! Notifica test Cardioaspirina alle {test_ora}",
            "schermata_paziente": f"/paziente/{pid}"
        })
    except Exception as e:
        db.close()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/aifa/upload-csv", methods=["POST"])
def upload_csv_aifa():
    """
    Carica manualmente un CSV AIFA scaricato dal tuo PC.
    Usare quando il container non riesce a scaricare da aifa.gov.it.

    curl -X POST https://farmaci.mabalu.it/api/aifa/upload-csv \
      -F "file=@/percorso/al/file.csv" \
      -F "tipo=classe_a"

    tipo: classe_a | classe_h | carenti | trasparenza
    """
    if "file" not in request.files:
        return jsonify({"error": "Campo 'file' mancante"}), 400

    file = request.files["file"]
    tipo = request.form.get("tipo", "generico")

    # Configurazione per tipo
    config_map = {
        "classe_a": {"col_aic": "Codice AIC", "col_nome": "Denominazione e Confezione",
                     "col_pa": "Principio Attivo", "col_atc": None, "sep": ";"},
        "classe_h": {"col_aic": "Codice AIC", "col_nome": "Denominazione e Confezione",
                     "col_pa": "Principio Attivo", "col_atc": None, "sep": ";"},
        "carenti":  {"col_aic": "Codice AIC", "col_nome": "Nome medicinale",
                     "col_pa": "Principio attivo", "col_atc": "Codice ATC",
                     "sep": ";", "skip": 2},
        "trasparenza": {"col_aic": "Codice AIC", "col_nome": "Denominazione",
                        "col_pa": "Principio attivo", "col_atc": "ATC", "sep": ";"},
    }
    cfg = config_map.get(tipo, {"col_aic": "Codice AIC", "col_nome": "Denominazione",
                                 "col_pa": "Principio attivo", "col_atc": None, "sep": ";"})

    try:
        contenuto = file.read()
        try:
            testo = contenuto.decode("utf-8")
        except UnicodeDecodeError:
            testo = contenuto.decode("latin-1")

        righe = testo.splitlines()
        skip = cfg.get("skip", 0)
        righe = righe[skip:]
        if not righe:
            return jsonify({"error": "File vuoto"}), 400

        # Strip più aggressivo delle virgolette dall'header
        header = [h.strip().strip('"').strip("'").strip() for h in righe[0].split(cfg["sep"])]

        def col_idx(nome_col):
            if not nome_col: return None
            nome_lower = nome_col.lower().strip().strip('"')
            for i, h in enumerate(header):
                h_clean = h.lower().strip().strip('"').strip("'")
                if nome_lower in h_clean or h_clean in nome_lower:
                    return i
            return None

        idx_aic  = col_idx(cfg["col_aic"])
        idx_nome = col_idx(cfg["col_nome"])
        idx_pa   = col_idx(cfg.get("col_pa"))
        idx_atc  = col_idx(cfg.get("col_atc"))

        if idx_aic is None or idx_nome is None:
            return jsonify({
                "error": f"Colonne non trovate",
                "header_trovato": header,
                "col_aic_cercata": cfg["col_aic"],
                "col_nome_cercata": cfg["col_nome"],
                "idx_aic": idx_aic,
                "idx_nome": idx_nome
            }), 400

        import re
        db = get_db()
        # Crea tabella se non esiste
        db.execute("""
            CREATE TABLE IF NOT EXISTS aifa_lookup (
                aic TEXT PRIMARY KEY, nome TEXT NOT NULL,
                principio_attivo TEXT, atc TEXT, fonte TEXT,
                aggiornato_il TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        count = 0
        for riga in righe[1:]:
            if not riga.strip(): continue
            campi = [c.strip().strip('"') for c in riga.split(cfg["sep"])]
            if len(campi) <= max(idx_aic, idx_nome): continue

            aic_raw = campi[idx_aic].strip()
            nome    = campi[idx_nome].strip()
            pa      = campi[idx_pa].strip() if idx_pa and idx_pa < len(campi) else None
            atc_val = campi[idx_atc].strip() if idx_atc and idx_atc < len(campi) else None

            aic_clean = re.sub(r"[^0-9]", "", aic_raw)
            if len(aic_clean) < 6 or not nome: continue
            aic_6 = aic_clean[:6]

            # Non sovrascrivere hardcoded
            ex = db.execute("SELECT fonte FROM aifa_lookup WHERE aic=?", (aic_6,)).fetchone()
            if ex and ex["fonte"] == "hardcoded": continue

            db.execute("""
                INSERT OR REPLACE INTO aifa_lookup (aic, nome, principio_attivo, atc, fonte)
                VALUES (?, ?, ?, ?, ?)
            """, (aic_6, nome, pa, atc_val, tipo))
            count += 1

        db.commit()
        tot = db.execute("SELECT COUNT(*) as n FROM aifa_lookup").fetchone()["n"]
        db.close()
        return jsonify({"ok": True, "importati": count, "totale_db": tot,
                        "message": f"{count} farmaci importati da {file.filename}"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/aifa/test-connessione", methods=["GET"])
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


@app.route("/api/aifa/import", methods=["POST"])
def import_aifa():
    """Scarica i CSV AIFA e popola la tabella aifa_lookup."""
    import threading
    def _run():
        try:
            from aifa_import import main as aifa_main
            import sys
            # Rimuovi --skip-download se presente
            old_argv = sys.argv[:]
            sys.argv = [sys.argv[0]]
            aifa_main()
            sys.argv = old_argv
            print("[AIFA-IMPORT] Completato via API.")
        except Exception as e:
            print(f"[AIFA-IMPORT] Errore: {e}")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"message": "Import AIFA avviato in background. Controlla i log."})


@app.route("/api/aifa/import-otc", methods=["POST"])
def import_aifa_otc():
    """Importa solo il dizionario OTC hardcoded (veloce, no download)."""
    try:
        from aifa_import import init_tabella, importa_dizionario_otc
        db = get_db()
        init_tabella(db)
        count = importa_dizionario_otc(db)
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
def sync_ha():
    aggiorna_sensori_ha()
    return jsonify({"message": "Sensori HA aggiornati"})

# ═══════════════════════════════════════════════════════════════════════════
# TELEGRAM WEBHOOK
# ═══════════════════════════════════════════════════════════════════════════

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
    update = request.get_json(silent=True)
    if not update:
        return jsonify({"ok": True})

    # ── Callback query (bottoni inline SI/NO) ──────────────────────────────
    cq = update.get("callback_query")
    if cq:
        callback_id   = cq["id"]
        chat_id       = cq["message"]["chat"]["id"]
        message_id    = cq["message"]["message_id"]
        data          = cq.get("data", "")
        utente        = cq["from"].get("first_name", "Utente")

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
    if msg:
        chat_id = msg["chat"]["id"]
        testo   = msg.get("text", "").strip()
        nome    = msg["from"].get("first_name", "")

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
                    f"👋 Bentornato/a <b>{paz['nome']}</b>!\n\n"
                    f"💊 <b>PillolApp</b> è pronto.\n"
                    f"Riceverai qui i promemoria per i tuoi farmaci.\n\n"
                    f"📋 Usa /stato per vedere le terapie di oggi.\n"
                    f"ℹ️ Rispondi ai messaggi con ✅ <b>SÌ</b> o ❌ <b>No</b> "
                    f"per confermare l'assunzione."
                )
            else:
                # Nuovo utente — mostra il Chat ID da comunicare al caregiver
                _invia_messaggio(chat_id,
                    f"👋 Ciao <b>{nome}</b>!\n\n"
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
                f"💊 <b>PillolApp</b>\n\n"
                f"Usa /start per registrarti\n"
                f"Usa /stato per vedere le terapie di oggi."
            )

    return jsonify({"ok": True})


def _processa_risposta(ass_id: int, esito: str, chat_id: int,
                       message_id: int, utente: str):
    """Registra la risposta SI/NO e aggiorna il messaggio Telegram."""
    db = get_db()
    ass = db.execute("""
        SELECT a.*, t.paziente_id, t.farmaco_id, t.dose,
               f.nome as farmaco_nome, p.nome as paz_nome, p.cognome as paz_cognome
        FROM assunzioni a
        JOIN terapie t ON a.terapia_id = t.id
        JOIN farmaci  f ON t.farmaco_id  = f.id
        JOIN pazienti p ON t.paziente_id = p.id
        WHERE a.id = ?
    """, (ass_id,)).fetchone()

    if not ass:
        db.close()
        _invia_messaggio(chat_id, "⚠️ Assunzione non trovata.")
        return

    if ass["esito"] == "SI":
        db.close()
        _modifica_messaggio(chat_id, message_id,
            f"✅ <b>{ass['farmaco_nome']}</b> già confermato!")
        return

    # Registra la risposta
    db.execute("""
        UPDATE assunzioni
        SET esito=?, orario_risposta=datetime('now'), canale='telegram'
        WHERE id=?
    """, (esito, ass_id))

    if esito == "SI":
        # Scala la scorta
        db.execute("""
            UPDATE scorte SET quantita = MAX(0, quantita - 1),
            aggiornato_il = datetime('now')
            WHERE paziente_id=? AND farmaco_id=?
        """, (ass["paziente_id"], ass["farmaco_id"]))

    db.commit()
    db.close()

    if esito == "SI":
        testo_edit = (
            f"✅ <b>{ass['farmaco_nome']}</b> — {ass['dose']}\n"
            f"👤 {ass['paz_nome']} {ass['paz_cognome']}\n"
            f"🕐 {ass['orario_previsto']}\n\n"
            f"<i>Confermato da {utente}</i> ✓"
        )
        _modifica_messaggio(chat_id, message_id, testo_edit)
        aggiorna_sensori_ha()

    else:  # NO
        _modifica_messaggio(chat_id, message_id,
            f"⏰ <b>{ass['farmaco_nome']}</b> — non ancora preso.\n"
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

    if not paziente:
        db.close()
        _invia_messaggio(chat_id,
            "⚠️ Chat ID non associato a nessun paziente.\n"
            "Configuralo nelle impostazioni dell'app."
        )
        return

    oggi = str(datetime.date.today())
    giorno_sett = datetime.date.today().weekday()

    terapie = db.execute("""
        SELECT t.*, f.nome as farmaco_nome
        FROM terapie t JOIN farmaci f ON t.farmaco_id = f.id
        WHERE t.paziente_id=? AND t.attiva=1
          AND (t.data_fine IS NULL OR t.data_fine >= ?)
    """, (paziente["id"], oggi)).fetchall()

    if not terapie:
        db.close()
        _invia_messaggio(chat_id, "📋 Nessuna terapia attiva per oggi.")
        return

    log_oggi = db.execute("""
        SELECT a.*, t.farmaco_id FROM assunzioni a
        JOIN terapie t ON a.terapia_id = t.id
        WHERE t.paziente_id=? AND date(a.creato_il)=?
    """, (paziente["id"], oggi)).fetchall()
    db.close()

    prese = {(a["farmaco_id"], a["orario_previsto"].split(" ")[-1])
             for a in log_oggi if a["esito"] == "SI"}

    righe = [f"📋 <b>Terapie di oggi — {paziente['nome']}</b>\n"]
    for t in terapie:
        orari = json.loads(t["orari"] or "[]")
        giorni = json.loads(t["giorni_settimana"] or "null") or list(range(7))
        if giorno_sett not in giorni:
            continue
        for o in orari:
            icona = "✅" if (t["farmaco_id"], o) in prese else "⏳"
            righe.append(f"{icona} <b>{t['farmaco_nome']}</b> — {t['dose']} ore {o}")

    _invia_messaggio(chat_id, "\n".join(righe))


# ── Setup webhook (chiamato all'avvio se URL configurato) ──────────────────

def registra_webhook(public_url: str):
    """
    Registra il webhook su Telegram.
    Chiamare con l'URL pubblico del server, es. https://farmaci.mabalu.it
    """
    if not TELEGRAM_BOT_TOKEN:
        print("[TG] Token non configurato — webhook non registrato.")
        return
    webhook_url = f"{public_url.rstrip('/')}/api/telegram/webhook"
    try:
        import requests as req
        r = req.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
            json={"url": webhook_url, "allowed_updates": ["message", "callback_query"]},
            timeout=10
        )
        data = r.json()
        if data.get("ok"):
            print(f"[TG] Webhook registrato: {webhook_url}")
        else:
            print(f"[TG] Errore registrazione webhook: {data}")
    except Exception as e:
        print(f"[TG] Registrazione webhook fallita: {e}")


@app.route("/api/telegram/setup-webhook", methods=["POST"])
def setup_webhook_endpoint():
    """Endpoint per registrare il webhook manualmente via POST."""
    data = request.get_json()
    url  = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "url obbligatorio"}), 400
    registra_webhook(url)
    return jsonify({"message": f"Webhook registrato per {url}"})


@app.route("/api/telegram/info", methods=["GET"])
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

PUBLIC_URL = os.environ.get("PUBLIC_URL", "")   # es. https://farmaci.mabalu.it

if __name__ == "__main__":
    init_db()
    # Crea utente caregiver di default se non esiste
    crea_utente_se_non_esiste("caregiver", CAREGIVER_PASSWORD, "caregiver")
    print(f"[AUTH] Modalità: {MODALITA}")
    print(f"[AUTH] Caregiver username: caregiver | password: {CAREGIVER_PASSWORD}")

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
    app.run(host="0.0.0.0", port=5001, debug=False)
