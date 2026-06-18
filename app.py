"""
Quizzer — Classroom Quiz Web Application
Flask + Flask-SocketIO backend.

SECURITY NOTE: Change FACULTY_PASSWORD before deploying to production!
"""
import json
import os
import re
import random
import sqlite3
import subprocess
import threading
import uuid
from contextlib import contextmanager

import gevent.monkey
gevent.monkey.patch_all()

from flask import (
    Flask,
    redirect,
    render_template,
    request,
    session as flask_session,
    url_for,
    jsonify,
    make_response,
)
from flask_socketio import SocketIO, emit, join_room

import ai_generator

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

# SECURITY: Change this password before deploying!
FACULTY_PASSWORD = "teacher123"

DB_PATH      = os.path.join(os.path.dirname(__file__), "quiz.db")
UPLOAD_DIR   = os.path.join(os.path.dirname(__file__), "static", "uploads")
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
os.makedirs(UPLOAD_DIR, exist_ok=True)

SERVER_IP   = "10.42.0.1"
SERVER_PORT = int(os.environ.get("PORT", 80))
QUIZ_URL    = f"http://{SERVER_IP}:{SERVER_PORT}/"

socketio = SocketIO(app, async_mode="gevent", cors_allowed_origins="*",
                    ping_timeout=60, ping_interval=25)

# Timer for global quiz end: {session_id: {'cancelled': bool}}
_timers: dict = {}
_timers_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT NOT NULL,
                status TEXT DEFAULT 'setup',
                questions_per_student INTEGER DEFAULT 10,
                time_limit_seconds INTEGER DEFAULT 1800,
                started_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                type TEXT DEFAULT 'mcq',
                options TEXT,
                correct_answer TEXT,
                suggested_time INTEGER DEFAULT 30,
                order_index INTEGER NOT NULL,
                image_url TEXT DEFAULT NULL,
                marks INTEGER DEFAULT 1,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );

            CREATE TABLE IF NOT EXISTS students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                roll_no TEXT NOT NULL,
                device_id TEXT,
                option_seed INTEGER,
                status TEXT DEFAULT 'pending',
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );

            -- Per-student question assignment (random subset, random order)
            CREATE TABLE IF NOT EXISTS student_questions (
                student_id INTEGER NOT NULL,
                question_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                PRIMARY KEY (student_id, question_id),
                FOREIGN KEY (student_id) REFERENCES students(id),
                FOREIGN KEY (question_id) REFERENCES questions(id)
            );

            CREATE TABLE IF NOT EXISTS answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                question_id INTEGER NOT NULL,
                answer TEXT,
                is_correct INTEGER DEFAULT 0,
                time_taken INTEGER,
                submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                marks_awarded REAL DEFAULT NULL,
                ai_feedback TEXT DEFAULT NULL,
                UNIQUE(student_id, question_id),
                FOREIGN KEY (student_id) REFERENCES students(id),
                FOREIGN KEY (question_id) REFERENCES questions(id)
            );
        """)


def _migrate_db():
    """Add columns introduced in newer versions — safe to run on existing DBs."""
    with get_db() as conn:
        for ddl in [
            "ALTER TABLE questions ADD COLUMN marks INTEGER DEFAULT 1",
            "ALTER TABLE answers ADD COLUMN marks_awarded REAL DEFAULT NULL",
            "ALTER TABLE answers ADD COLUMN ai_feedback TEXT DEFAULT NULL",
            "ALTER TABLE students ADD COLUMN status TEXT DEFAULT 'pending'",
        ]:
            try:
                conn.execute(ddl)
            except Exception:
                pass  # Column already exists


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def faculty_required(f):
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        if not flask_session.get("is_faculty"):
            if request.is_json:
                return jsonify({"error": "Unauthorized"}), 401
            return redirect(url_for("faculty_page"))
        return f(*args, **kwargs)

    return decorated


def _get_active_session():
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE status != 'ended' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Captive portal detection routes
# ---------------------------------------------------------------------------
# Android probes /generate_204 and expects HTTP 204.
# iOS probes /hotspot-detect.html and expects "Success" HTML.
# Windows probes /connecttest.txt etc.
# All get a redirect to QUIZ_URL instead of the expected response,
# which signals the OS that a captive portal is present.
# The redirect uses the absolute server IP (not the requested Host header)
# so the CNA/browser lands on the quiz page, not in an infinite loop.

def _to_quiz():
    return redirect(QUIZ_URL, code=302)

# iOS / macOS: return a page that meta-redirects; CNA opens it immediately.
def _ios_portal():
    html = (
        f'<html><head>'
        f'<meta http-equiv="refresh" content="0;url={QUIZ_URL}">'
        f'</head><body>'
        f'<a href="{QUIZ_URL}">Tap to open quiz</a>'
        f'</body></html>'
    )
    # Return 200 with redirect meta — iOS CNA opens this page directly.
    return make_response(html, 200)

_REDIRECT_PATHS = [
    "/generate_204",                          # Android / Chrome OS
    "/library/test/success.html",            # Apple (older)
    "/connecttest.txt",                       # Windows
    "/ncsi.txt",                              # Windows
    "/success.txt",
    "/canonical.html",
    "/redirect",
    "/kindle-wifi/wifistub.html",             # Amazon Kindle
    "/chat",                                  # Samsung / misc
    "/mobile/status.php",                     # misc Android
]
_IOS_PATHS = [
    "/hotspot-detect.html",                   # iOS / macOS primary probe
]

for _path in _REDIRECT_PATHS:
    app.add_url_rule(
        _path,
        endpoint="captive_" + _path.replace("/", "_").replace(".", "_"),
        view_func=_to_quiz,
    )
for _path in _IOS_PATHS:
    app.add_url_rule(
        _path,
        endpoint="captive_ios" + _path.replace("/", "_").replace(".", "_"),
        view_func=_ios_portal,
    )


@app.route("/<path:dummy>")
def catch_all(dummy):
    """Redirect any unrecognised path to the quiz page (catches unknown OS probes)."""
    # Let Flask handle its own static files and faculty routes normally.
    if dummy.startswith(("static/", "faculty", "socket.io", "api/")):
        return make_response("Not found", 404)
    return redirect(QUIZ_URL, code=302)


# ---------------------------------------------------------------------------
# Student routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    active = _get_active_session()
    session_id = active["id"] if active and active["status"] in ("lobby", "active") else None
    return render_template("student.html", session_id=session_id)


@app.route("/favicon.ico")
def favicon():
    return make_response("", 204)


@app.route("/api/session/active")
def api_session_active():
    """Student page polls this to find out when the lobby opens."""
    active = _get_active_session()
    if active and active["status"] in ("lobby", "active"):
        return jsonify({"session_id": active["id"], "status": active["status"], "topic": active["topic"]})
    return jsonify({"session_id": None})


# ---------------------------------------------------------------------------
# Faculty routes
# ---------------------------------------------------------------------------
@app.route("/faculty")
def faculty_page():
    if not flask_session.get("is_faculty"):
        return render_template("faculty_login.html", error=None)
    active = _get_active_session()
    return render_template("faculty.html", active_session=active)


@app.route("/faculty/login", methods=["POST"])
def faculty_login():
    if request.form.get("password", "") == FACULTY_PASSWORD:
        flask_session["is_faculty"] = True
        flask_session.permanent = False
        return redirect(url_for("faculty_page"))
    return render_template("faculty_login.html", error="Incorrect password.")


@app.route("/faculty/logout")
def faculty_logout():
    flask_session.clear()
    return redirect(url_for("faculty_page"))


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------
@app.route("/api/session/create", methods=["POST"])
@faculty_required
def api_session_create():
    data = request.get_json(force=True)
    topic = (data.get("topic") or "").strip()
    if not topic:
        return jsonify({"error": "Topic is required"}), 400

    with get_db() as conn:
        conn.execute("UPDATE sessions SET status='ended' WHERE status != 'ended'")
        cur = conn.execute(
            "INSERT INTO sessions (topic, status) VALUES (?, 'setup')", (topic,)
        )
        session_id = cur.lastrowid

    return jsonify({"session_id": session_id, "topic": topic})


@app.route("/api/questions/generate", methods=["POST"])
@faculty_required
def api_questions_generate():
    data = request.get_json(force=True)
    session_id  = int(data.get("session_id", 0))
    topic       = (data.get("topic") or "").strip()
    count       = max(1, min(50, int(data.get("count", 20))))
    difficulty  = data.get("difficulty", "Medium")
    bloom_level = data.get("bloom_level", "Mixed")
    model       = data.get("model", "llama2")

    questions = ai_generator.generate_questions(topic, count, difficulty, bloom_level, model)
    if questions is None:
        return jsonify({"error": "Failed to generate questions. Is Ollama running?"}), 502

    with get_db() as conn:
        # Find next order_index so new questions are appended after existing ones
        last = conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) FROM questions WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        for idx, q in enumerate(questions):
            conn.execute(
                """INSERT INTO questions
                   (session_id, text, type, options, correct_answer, suggested_time, order_index)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session_id, q["text"], q["type"], json.dumps(q["options"]),
                 q["correct_answer"], q["suggested_time"], last + 1 + idx),
            )
        rows = conn.execute(
            "SELECT * FROM questions WHERE session_id=? ORDER BY order_index", (session_id,)
        ).fetchall()

    result = []
    for row in rows:
        r = dict(row)
        r["options"] = json.loads(r["options"]) if r["options"] else []
        result.append(r)

    return jsonify({"questions": result})


@app.route("/api/questions/<int:session_id>", methods=["GET"])
@faculty_required
def api_questions_list(session_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM questions WHERE session_id=? ORDER BY order_index", (session_id,)
        ).fetchall()
    result = []
    for row in rows:
        r = dict(row)
        r["options"] = json.loads(r["options"]) if r["options"] else []
        result.append(r)
    return jsonify(result)


@app.route("/api/questions/<int:question_id>", methods=["PUT"])
@faculty_required
def api_question_update(question_id):
    data = request.get_json(force=True)
    with get_db() as conn:
        conn.execute(
            """UPDATE questions
               SET text=?, type=?, options=?, correct_answer=?, suggested_time=?, marks=?
               WHERE id=?""",
            (data.get("text","").strip(), data.get("type","mcq"),
             json.dumps(data.get("options",[])), data.get("correct_answer","").strip(),
             int(data.get("suggested_time",30)), max(1, int(data.get("marks",1))), question_id),
        )
        row = conn.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
    if not row:
        return jsonify({"error": "Not found"}), 404
    r = dict(row)
    r["options"] = json.loads(r["options"]) if r["options"] else []
    return jsonify(r)


@app.route("/api/questions/<int:question_id>/upload-image", methods=["POST"])
@faculty_required
def api_upload_image(question_id):
    """Attach an image to a question. Accepts multipart/form-data with field 'image'."""
    if "image" not in request.files:
        return jsonify({"error": "No image file provided"}), 400

    f = request.files["image"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXTS:
        return jsonify({"error": f"Unsupported file type. Allowed: {', '.join(ALLOWED_EXTS)}"}), 400

    filename = f"{uuid.uuid4().hex}{ext}"
    save_path = os.path.join(UPLOAD_DIR, filename)
    f.save(save_path)
    image_url = f"/static/uploads/{filename}"

    with get_db() as conn:
        conn.execute("UPDATE questions SET image_url=? WHERE id=?", (image_url, question_id))
        row = conn.execute("SELECT image_url FROM questions WHERE id=?", (question_id,)).fetchone()

    if not row:
        return jsonify({"error": "Question not found"}), 404

    return jsonify({"image_url": row["image_url"]})


@app.route("/api/questions/<int:question_id>/remove-image", methods=["DELETE"])
@faculty_required
def api_remove_image(question_id):
    """Remove image from a question and delete the uploaded file."""
    with get_db() as conn:
        row = conn.execute("SELECT image_url FROM questions WHERE id=?", (question_id,)).fetchone()
        if row and row["image_url"]:
            file_path = os.path.join(os.path.dirname(__file__), row["image_url"].lstrip("/"))
            if os.path.exists(file_path):
                os.remove(file_path)
        conn.execute("UPDATE questions SET image_url=NULL WHERE id=?", (question_id,))
    return jsonify({"ok": True})


@app.route("/api/questions/<int:question_id>", methods=["DELETE"])
@faculty_required
def api_question_delete(question_id):
    """Delete a single question (and its image if any)."""
    with get_db() as conn:
        row = conn.execute("SELECT image_url FROM questions WHERE id=?", (question_id,)).fetchone()
        if row and row["image_url"]:
            file_path = os.path.join(os.path.dirname(__file__), row["image_url"].lstrip("/"))
            if os.path.exists(file_path):
                os.remove(file_path)
        conn.execute("DELETE FROM student_questions WHERE question_id=?", (question_id,))
        conn.execute("DELETE FROM answers WHERE question_id=?", (question_id,))
        conn.execute("DELETE FROM questions WHERE id=?", (question_id,))
    return jsonify({"ok": True})


@app.route("/api/questions/delete-bulk", methods=["POST"])
@faculty_required
def api_questions_delete_bulk():
    """Delete multiple questions by ID list."""
    ids = request.get_json(force=True).get("ids", [])
    if not ids:
        return jsonify({"ok": True, "deleted": 0})
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT image_url FROM questions WHERE id IN ({','.join('?'*len(ids))})", ids
        ).fetchall()
        for row in rows:
            if row["image_url"]:
                path = os.path.join(os.path.dirname(__file__), row["image_url"].lstrip("/"))
                if os.path.exists(path):
                    os.remove(path)
        placeholders = ','.join('?' * len(ids))
        conn.execute(f"DELETE FROM student_questions WHERE question_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM answers WHERE question_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM questions WHERE id IN ({placeholders})", ids)
    return jsonify({"ok": True, "deleted": len(ids)})


@app.route("/api/questions/add", methods=["POST"])
@faculty_required
def api_question_add():
    data = request.get_json(force=True)
    session_id = int(data.get("session_id", 0))
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Question text is required"}), 400

    with get_db() as conn:
        max_order = conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) FROM questions WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        cur = conn.execute(
            """INSERT INTO questions
               (session_id, text, type, options, correct_answer, suggested_time, order_index, marks)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, text, data.get("type","mcq"), json.dumps(data.get("options",[])),
             data.get("correct_answer","").strip(), int(data.get("suggested_time",30)), max_order+1,
             max(1, int(data.get("marks", 1)))),
        )
        row = conn.execute("SELECT * FROM questions WHERE id=?", (cur.lastrowid,)).fetchone()

    r = dict(row)
    r["options"] = json.loads(r["options"]) if r["options"] else []
    return jsonify(r), 201


@app.route("/api/session/<int:session_id>/open-lobby", methods=["POST"])
@faculty_required
def api_open_lobby(session_id):
    with get_db() as conn:
        conn.execute("UPDATE sessions SET status='lobby' WHERE id=?", (session_id,))
    socketio.emit("session_opened", {"session_id": session_id}, room=f"session_{session_id}")
    return jsonify({"status": "lobby"})


@app.route("/api/students/<int:session_id>", methods=["GET"])
@faculty_required
def api_students(session_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, roll_no, joined_at FROM students WHERE session_id=? ORDER BY joined_at",
            (session_id,),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/session/<int:session_id>/progress", methods=["GET"])
@faculty_required
def api_progress(session_id):
    """Live progress: per-student answered count vs assigned count."""
    with get_db() as conn:
        students = conn.execute(
            "SELECT id, name, roll_no, status FROM students WHERE session_id=? AND status IN ('approved','submitted')",
            (session_id,),
        ).fetchall()
        progress = []
        for s in students:
            assigned = conn.execute(
                "SELECT COUNT(*) FROM student_questions WHERE student_id=?", (s["id"],)
            ).fetchone()[0]
            answered = conn.execute(
                "SELECT COUNT(*) FROM answers WHERE student_id=?", (s["id"],)
            ).fetchone()[0]
            correct = conn.execute(
                "SELECT COUNT(*) FROM answers WHERE student_id=? AND is_correct=1", (s["id"],)
            ).fetchone()[0]
            progress.append({
                "student_id": s["id"],
                "name": s["name"],
                "roll_no": s["roll_no"],
                "assigned": assigned,
                "answered": answered,
                "correct": correct,
                "done": assigned > 0 and answered >= assigned,
                "status": s["status"] or "approved",
            })
    return jsonify(progress)


@app.route("/api/session/<int:session_id>/results", methods=["GET"])
@faculty_required
def api_results(session_id):
    with get_db() as conn:
        students = conn.execute(
            "SELECT id, name, roll_no FROM students WHERE session_id=?", (session_id,)
        ).fetchall()

        results = []
        for student in students:
            assigned = conn.execute(
                "SELECT COUNT(*) FROM student_questions WHERE student_id=?", (student["id"],)
            ).fetchone()[0]
            answers = conn.execute(
                """SELECT a.question_id, a.answer, a.is_correct, a.time_taken,
                          a.marks_awarded, a.ai_feedback,
                          q.text as question_text, q.correct_answer, q.type, q.marks
                   FROM answers a
                   JOIN questions q ON q.id = a.question_id
                   WHERE a.student_id=?""",
                (student["id"],),
            ).fetchall()

            mcq_score = sum(1 for a in answers if a["is_correct"] and a["type"] != "short_answer")
            sa_score = sum(a["marks_awarded"] for a in answers
                           if a["type"] == "short_answer" and a["marks_awarded"] is not None)
            sa_max = sum(a["marks"] or 1 for a in answers if a["type"] == "short_answer")
            total_pts = mcq_score + sa_score
            max_pts = (assigned - sum(1 for a in answers if a["type"] == "short_answer")) + sa_max
            pct = round(total_pts / max_pts * 100, 1) if max_pts > 0 else 0

            results.append({
                "student_id": student["id"],
                "name": student["name"],
                "roll_no": student["roll_no"],
                "score": mcq_score,
                "sa_score": round(sa_score, 2),
                "sa_max": sa_max,
                "total_points": round(total_pts, 2),
                "max_points": max_pts,
                "total": assigned,
                "percentage": pct,
                "answers": [dict(a) for a in answers],
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        for rank, r in enumerate(results, 1):
            r["rank"] = rank

    return jsonify(results)


@app.route("/api/session/<int:session_id>/my-answers/<int:student_id>", methods=["GET"])
def api_my_answers(session_id, student_id):
    with get_db() as conn:
        answers = conn.execute(
            """SELECT a.question_id, a.answer AS your_answer, a.is_correct,
                      a.marks_awarded, a.ai_feedback,
                      q.text AS question_text, q.correct_answer, q.type, q.marks
               FROM answers a
               JOIN questions q ON q.id = a.question_id
               JOIN student_questions sq ON sq.question_id = q.id AND sq.student_id = a.student_id
               WHERE a.student_id=?
               ORDER BY sq.position""",
            (student_id,),
        ).fetchall()

    mcq_answers = [a for a in answers if a["type"] != "short_answer"]
    return jsonify({
        "answers": [dict(a) for a in answers],
        "mcq_correct": sum(1 for a in mcq_answers if a["is_correct"]),
        "mcq_total": len(mcq_answers),
    })


@app.route("/api/models", methods=["GET"])
@faculty_required
def api_models():
    models = ai_generator.get_available_models()
    return jsonify({"models": models or []})


# ---------------------------------------------------------------------------
# Hotspot management
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _wifi_interfaces():
    """Return list of wireless interface names found on this machine."""
    try:
        out = subprocess.run(["iw", "dev"], capture_output=True, text=True, timeout=5).stdout
        ifaces = re.findall(r"Interface (\S+)", out)
        if ifaces:
            return ifaces
    except Exception:
        pass
    # fallback: /sys/class/net
    try:
        return [
            iface for iface in os.listdir("/sys/class/net")
            if os.path.exists(f"/sys/class/net/{iface}/wireless")
        ]
    except Exception:
        return ["wlan0"]


def _hotspot_status():
    result = subprocess.run(["pgrep", "-f", "quizzer_hostapd.conf"],
                            capture_output=True)
    if result.returncode != 0:
        return {"running": False, "ssid": "", "interface": "", "port": ""}
    ssid, iface, port = "", "", ""
    try:
        cfg = open("/tmp/quizzer_hostapd.conf").read()
        m = re.search(r"^ssid=(.+)$", cfg, re.M)
        if m:
            ssid = m.group(1)
        m = re.search(r"^interface=(.+)$", cfg, re.M)
        if m:
            iface = m.group(1)
    except Exception:
        pass
    try:
        port = open("/tmp/quizzer_port").read().strip()
    except Exception:
        port = "80"
    return {"running": True, "ssid": ssid, "interface": iface, "port": port}


@app.route("/api/hotspot/status", methods=["GET"])
@faculty_required
def api_hotspot_status():
    status = _hotspot_status()
    status["interfaces"] = _wifi_interfaces()
    return jsonify(status)


@app.route("/api/hotspot/start", methods=["POST"])
@faculty_required
def api_hotspot_start():
    data = request.get_json(force=True)
    ssid      = (data.get("ssid") or "ClassroomQuiz").strip()
    password  = (data.get("password") or "quiz12345").strip()
    interface = (data.get("interface") or "wlan0").strip()
    port      = str(data.get("port") or os.environ.get("PORT", "80"))

    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    script = os.path.join(SCRIPT_DIR, "setup_hotspot.sh")
    try:
        result = subprocess.run(
            ["sudo", "bash", script, ssid, password, interface, port],
            capture_output=True, text=True, timeout=45,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Hotspot setup timed out"}), 500

    if result.returncode != 0:
        return jsonify({"error": result.stderr or result.stdout or "Setup failed"}), 500

    return jsonify({"ok": True, "ssid": ssid, "interface": interface, "port": port})


@app.route("/api/hotspot/stop", methods=["POST"])
@faculty_required
def api_hotspot_stop():
    interface = (request.get_json(force=True) or {}).get("interface", "")
    script = os.path.join(SCRIPT_DIR, "teardown_hotspot.sh")
    args = ["sudo", "bash", script]
    if interface:
        args.append(interface)
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Teardown timed out"}), 500

    if result.returncode != 0:
        return jsonify({"error": result.stderr or "Stop failed"}), 500

    return jsonify({"ok": True})


@app.route("/api/questions/generate-from-document", methods=["POST"])
@faculty_required
def api_questions_generate_from_document():
    """Generate questions from an uploaded PDF document."""
    if "document" not in request.files:
        return jsonify({"error": "No document file provided"}), 400
    f = request.files["document"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400

    session_id  = int(request.form.get("session_id", 0))
    count       = max(1, min(50, int(request.form.get("count", 10))))
    difficulty  = request.form.get("difficulty", "Medium")
    bloom_level = request.form.get("bloom_level", "Mixed")
    model       = request.form.get("model", "")
    if not model:
        return jsonify({"error": "Model is required"}), 400

    try:
        import pdfplumber
        with pdfplumber.open(f) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except ImportError:
        return jsonify({"error": "pdfplumber not installed. Run: pip install pdfplumber"}), 500
    except Exception as e:
        return jsonify({"error": f"Failed to read PDF: {e}"}), 400

    if not text.strip():
        return jsonify({"error": "Could not extract text from PDF. Use a text-based (non-scanned) PDF."}), 400

    questions = ai_generator.generate_from_document(text, count, difficulty, bloom_level, model)
    if questions is None:
        return jsonify({"error": "Failed to generate questions. Is Ollama running?"}), 502

    with get_db() as conn:
        last = conn.execute(
            "SELECT COALESCE(MAX(order_index), -1) FROM questions WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        for idx, q in enumerate(questions):
            conn.execute(
                """INSERT INTO questions
                   (session_id, text, type, options, correct_answer, suggested_time, order_index)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session_id, q["text"], q["type"], json.dumps(q["options"]),
                 q["correct_answer"], q["suggested_time"], last + 1 + idx),
            )
        rows = conn.execute(
            "SELECT * FROM questions WHERE session_id=? ORDER BY order_index", (session_id,)
        ).fetchall()

    result = []
    for row in rows:
        r = dict(row)
        r["options"] = json.loads(r["options"]) if r["options"] else []
        result.append(r)
    return jsonify({"questions": result})


@app.route("/api/session/<int:session_id>/grade-short-answers", methods=["POST"])
@faculty_required
def api_grade_short_answers(session_id):
    """Batch-grade all short-answer responses using Ollama after quiz ends."""
    model = (request.get_json(force=True) or {}).get("model", "")
    if not model:
        return jsonify({"error": "Model is required"}), 400

    with get_db() as conn:
        questions = conn.execute(
            "SELECT * FROM questions WHERE session_id=? AND type='short_answer'", (session_id,)
        ).fetchall()
        if not questions:
            return jsonify({"ok": True, "graded": 0, "message": "No short-answer questions."})

        grading_tasks = []
        for q in questions:
            responses = conn.execute(
                """SELECT a.student_id, a.answer FROM answers a
                   WHERE a.question_id=? AND a.answer IS NOT NULL AND a.answer != ''""",
                (q["id"],),
            ).fetchall()
            if not responses:
                continue
            grading_tasks.append({
                "question_id": q["id"],
                "question_text": q["text"],
                "expected_answer": q["correct_answer"] or "",
                "marks": q["marks"] if q["marks"] else 1,
                "image_url": q["image_url"],
                "responses": [{"student_id": r["student_id"], "answer": r["answer"]} for r in responses],
            })

    if not grading_tasks:
        return jsonify({"ok": True, "graded": 0, "message": "No answers to grade."})

    results = ai_generator.grade_short_answers(grading_tasks, model)

    graded_count = 0
    with get_db() as conn:
        for r in results:
            conn.execute(
                """UPDATE answers SET marks_awarded=?, ai_feedback=?
                   WHERE student_id=? AND question_id=?""",
                (r["marks_awarded"], r["feedback"], r["student_id"], r["question_id"]),
            )
            graded_count += 1

    return jsonify({"ok": True, "graded": graded_count})


@app.route("/api/questions/ai-chat", methods=["POST"])
@faculty_required
def api_ai_chat():
    """Natural-language AI assistant: faculty requests new questions in plain text."""
    data = request.get_json(force=True)
    session_id = int(data.get("session_id", 0))
    message    = (data.get("message") or "").strip()
    model      = data.get("model", "")

    if not message:
        return jsonify({"error": "Message is required"}), 400
    if not model:
        return jsonify({"error": "Model is required"}), 400

    with get_db() as conn:
        sess = conn.execute("SELECT topic FROM sessions WHERE id=?", (session_id,)).fetchone()
        topic = sess["topic"] if sess else ""
        rows = conn.execute(
            "SELECT text, correct_answer FROM questions WHERE session_id=?", (session_id,)
        ).fetchall()
        existing = [dict(r) for r in rows]

    suggestions = ai_generator.ai_chat_questions(message, existing, topic, model)
    if suggestions is None:
        return jsonify({"error": "AI could not generate suggestions. Is Ollama running?"}), 502

    return jsonify({"suggestions": suggestions})


# ---------------------------------------------------------------------------
# Socket.IO events
# ---------------------------------------------------------------------------
@socketio.on("faculty_join")
def handle_faculty_join(data):
    session_id = int(data.get("session_id", 0))
    join_room(f"faculty_{session_id}")
    join_room(f"session_{session_id}")
    emit("faculty_joined", {"session_id": session_id})


@socketio.on("reveal_answers")
def handle_reveal_answers(data):
    session_id = int(data.get("session_id", 0))
    socketio.emit("answers_revealed", {}, room=f"session_{session_id}")


@socketio.on("student_join")
def handle_student_join(data):
    session_id = int(data.get("session_id", 0))
    name = (data.get("name") or "").strip()
    roll_no = (data.get("roll_no") or "").strip()

    if not name or not roll_no:
        emit("join_error", {"message": "Name and roll number are required."})
        return

    with get_db() as conn:
        session_row = conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        ).fetchone()

        if not session_row or session_row["status"] not in ("lobby", "active"):
            emit("join_error", {"message": "Session is not accepting students right now."})
            return

        existing = conn.execute(
            "SELECT * FROM students WHERE session_id=? AND roll_no=?", (session_id, roll_no)
        ).fetchone()

        if existing:
            conn.execute("UPDATE students SET name=? WHERE id=?", (name, existing["id"]))
            student_id = existing["id"]
            option_seed = existing["option_seed"]
            student_status = existing["status"] or "pending"
            # Allow rejected students to re-request
            if student_status == "rejected":
                conn.execute("UPDATE students SET status='pending' WHERE id=?", (student_id,))
                student_status = "pending"
        else:
            option_seed = random.randint(1, 2**31 - 1)
            cur = conn.execute(
                "INSERT INTO students (session_id, name, roll_no, option_seed, status) VALUES (?,?,?,?,?)",
                (session_id, name, roll_no, option_seed, "pending"),
            )
            student_id = cur.lastrowid
            student_status = "pending"

        join_room(f"session_{session_id}")
        join_room(f"student_{student_id}")

        # Reconnect: already approved or submitted — skip approval
        if student_status in ("approved", "submitted"):
            if session_row["status"] == "active":
                questions, already_answered = _get_or_assign_questions(
                    conn, student_id, session_id
                )
                emit("join_success", {
                    "student_id": student_id, "name": name,
                    "option_seed": option_seed, "session_id": session_id,
                })
                emit("quiz_started", {
                    "questions": questions,
                    "time_limit_seconds": session_row["time_limit_seconds"] or 1800,
                    "answered_ids": already_answered,
                })
            else:
                emit("join_success", {
                    "student_id": student_id, "name": name,
                    "option_seed": option_seed, "session_id": session_id,
                })
            return

        # New or pending — needs faculty approval
        emit("pending_approval", {"student_id": student_id, "name": name})
        socketio.emit("student_pending", {
            "student_id": student_id,
            "name": name,
            "roll_no": roll_no,
        }, room=f"faculty_{session_id}")


@socketio.on("approve_student")
def handle_approve_student(data):
    student_id = int(data.get("student_id", 0))
    session_id = int(data.get("session_id", 0))

    with get_db() as conn:
        conn.execute("UPDATE students SET status='approved' WHERE id=?", (student_id,))
        student = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
        session_row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not student or not session_row:
            return
        count = conn.execute(
            "SELECT COUNT(*) FROM students WHERE session_id=? AND status='approved'",
            (session_id,),
        ).fetchone()[0]

        if session_row["status"] == "active":
            questions, already_answered = _get_or_assign_questions(conn, student_id, session_id)
            socketio.emit("join_success", {
                "student_id": student_id, "name": student["name"],
                "option_seed": student["option_seed"], "session_id": session_id,
            }, room=f"student_{student_id}")
            socketio.emit("quiz_started", {
                "questions": questions,
                "time_limit_seconds": session_row["time_limit_seconds"] or 1800,
                "answered_ids": already_answered,
            }, room=f"student_{student_id}")
        else:
            socketio.emit("join_success", {
                "student_id": student_id, "name": student["name"],
                "option_seed": student["option_seed"], "session_id": session_id,
            }, room=f"student_{student_id}")

    socketio.emit("student_approved", {
        "student_id": student_id,
        "name": student["name"],
        "roll_no": student["roll_no"],
        "count": count,
    }, room=f"faculty_{session_id}")


@socketio.on("reject_student")
def handle_reject_student(data):
    student_id = int(data.get("student_id", 0))
    session_id = int(data.get("session_id", 0))
    with get_db() as conn:
        conn.execute("UPDATE students SET status='rejected' WHERE id=?", (student_id,))
    socketio.emit("join_rejected", {
        "message": "Your join request was declined by the teacher."
    }, room=f"student_{student_id}")


@socketio.on("allow_retake")
def handle_allow_retake(data):
    student_id = int(data.get("student_id", 0))
    session_id = int(data.get("session_id", 0))
    retake_type = data.get("retake_type", "same")

    with get_db() as conn:
        conn.execute("DELETE FROM answers WHERE student_id=?", (student_id,))
        if retake_type == "new":
            conn.execute("DELETE FROM student_questions WHERE student_id=?", (student_id,))
        new_seed = random.randint(1, 2**31 - 1)
        conn.execute(
            "UPDATE students SET option_seed=?, status='approved' WHERE id=?",
            (new_seed, student_id),
        )
        student = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
        session_row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        questions, _ = _get_or_assign_questions(conn, student_id, session_id)

    import datetime
    time_remaining = session_row["time_limit_seconds"] or 1800
    if session_row["started_at"] and session_row["status"] == "active":
        try:
            started_at = datetime.datetime.fromisoformat(session_row["started_at"])
            elapsed = (datetime.datetime.utcnow() - started_at).total_seconds()
            time_remaining = max(60, int(time_remaining - elapsed))
        except Exception:
            pass

    socketio.emit("retake_granted", {
        "questions": questions,
        "time_limit_seconds": time_remaining,
        "option_seed": new_seed,
    }, room=f"student_{student_id}")


@socketio.on("final_submit")
def handle_final_submit(data):
    student_id = int(data.get("student_id", 0))
    session_id = int(data.get("session_id", 0))
    with get_db() as conn:
        conn.execute("UPDATE students SET status='submitted' WHERE id=?", (student_id,))
        assigned = conn.execute(
            "SELECT COUNT(*) FROM student_questions WHERE student_id=?", (student_id,)
        ).fetchone()[0]
        answered = conn.execute(
            "SELECT COUNT(*) FROM answers WHERE student_id=?", (student_id,)
        ).fetchone()[0]
        correct = conn.execute(
            "SELECT COUNT(*) FROM answers WHERE student_id=? AND is_correct=1", (student_id,)
        ).fetchone()[0]
    socketio.emit("student_submitted", {"student_id": student_id}, room=f"faculty_{session_id}")
    socketio.emit("student_progress", {
        "student_id": student_id,
        "answered": answered,
        "assigned": assigned,
        "correct": correct,
        "done": True,
        "submitted": True,
    }, room=f"faculty_{session_id}")


@socketio.on("start_quiz")
def handle_start_quiz(data):
    """Faculty starts the quiz: all students get all questions in a unique random order."""
    session_id = int(data.get("session_id", 0))
    time_limit = int(data.get("time_limit_seconds", 1800))

    with get_db() as conn:
        all_q_rows = conn.execute(
            "SELECT * FROM questions WHERE session_id=? ORDER BY order_index", (session_id,)
        ).fetchall()

        students = conn.execute(
            "SELECT * FROM students WHERE session_id=? AND status='approved'", (session_id,)
        ).fetchall()

        conn.execute(
            """UPDATE sessions SET status='active',
               time_limit_seconds=?,
               started_at=CURRENT_TIMESTAMP WHERE id=?""",
            (time_limit, session_id),
        )

        for student in students:
            questions, _ = _get_or_assign_questions(
                conn, student["id"], session_id, all_q_rows=all_q_rows
            )
            socketio.emit("quiz_started", {
                "questions": questions,
                "time_limit_seconds": time_limit,
                "answered_ids": [],
            }, room=f"student_{student['id']}")

    # Start global countdown timer
    with _timers_lock:
        if session_id in _timers:
            _timers[session_id]["cancelled"] = True
        token = {"cancelled": False}
        _timers[session_id] = token

    def _quiz_timer():
        import gevent
        gevent.sleep(time_limit)
        with _timers_lock:
            if token.get("cancelled"):
                return
        _force_end_quiz(session_id)

    import gevent
    gevent.spawn(_quiz_timer)


@socketio.on("extend_timer")
def handle_extend_timer(data):
    """Faculty extends the quiz countdown by N seconds."""
    session_id = int(data.get("session_id", 0))
    extend_by  = max(60, min(3600, int(data.get("extend_by_seconds", 300))))

    with get_db() as conn:
        conn.execute(
            "UPDATE sessions SET time_limit_seconds = time_limit_seconds + ? WHERE id=?",
            (extend_by, session_id),
        )

    # Broadcast to everyone in the session (students + faculty)
    socketio.emit("timer_extended", {
        "extend_by_seconds": extend_by,
    }, room=f"session_{session_id}")


@socketio.on("end_quiz")
def handle_end_quiz(data):
    session_id = int(data.get("session_id", 0))
    with _timers_lock:
        if session_id in _timers:
            _timers[session_id]["cancelled"] = True
    _force_end_quiz(session_id)


@socketio.on("submit_answer")
def handle_submit_answer(data):
    student_id = int(data.get("student_id", 0))
    question_id = int(data.get("question_id", 0))
    answer = (data.get("answer") or "").strip()
    time_taken = int(data.get("time_taken", 0))

    with get_db() as conn:
        question = conn.execute(
            "SELECT correct_answer, session_id, type FROM questions WHERE id=?", (question_id,)
        ).fetchone()
        if not question:
            return

        student_row = conn.execute(
            "SELECT status FROM students WHERE id=?", (student_id,)
        ).fetchone()
        if student_row and student_row["status"] == "submitted":
            return

        session_id = question["session_id"]
        if question["type"] == "short_answer":
            is_correct = 0  # pending AI grading
        else:
            is_correct = int(answer.lower() == (question["correct_answer"] or "").lower())

        conn.execute(
            """INSERT INTO answers (student_id, question_id, answer, is_correct, time_taken)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(student_id, question_id) DO UPDATE SET
                 answer=excluded.answer, is_correct=excluded.is_correct,
                 time_taken=excluded.time_taken, submitted_at=CURRENT_TIMESTAMP""",
            (student_id, question_id, answer, is_correct, time_taken),
        )

        assigned = conn.execute(
            "SELECT COUNT(*) FROM student_questions WHERE student_id=?", (student_id,)
        ).fetchone()[0]
        answered = conn.execute(
            "SELECT COUNT(*) FROM answers WHERE student_id=?", (student_id,)
        ).fetchone()[0]
        correct_total = conn.execute(
            "SELECT COUNT(*) FROM answers WHERE student_id=? AND is_correct=1", (student_id,)
        ).fetchone()[0]

    emit("answer_confirmed", {"question_id": question_id})

    # Notify faculty live progress
    socketio.emit("student_progress", {
        "student_id": student_id,
        "answered": answered,
        "assigned": assigned,
        "correct": correct_total,
        "done": answered >= assigned,
    }, room=f"faculty_{session_id}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _get_or_assign_questions(conn, student_id, session_id, all_q_rows=None):
    """
    Return (questions_list, already_answered_ids) for a student.
    Every student receives ALL questions but in a unique random order.
    """
    existing = conn.execute(
        """SELECT q.*, sq.position FROM questions q
           JOIN student_questions sq ON q.id = sq.question_id
           WHERE sq.student_id=? ORDER BY sq.position""",
        (student_id,),
    ).fetchall()

    if existing:
        questions = []
        for row in existing:
            q = dict(row)
            q["options"] = json.loads(q["options"]) if q["options"] else []
            questions.append(q)
    else:
        # Shuffle ALL questions uniquely for this student
        if all_q_rows is None:
            all_q_rows = conn.execute(
                "SELECT * FROM questions WHERE session_id=? ORDER BY order_index", (session_id,)
            ).fetchall()
        shuffled = list(all_q_rows)
        random.shuffle(shuffled)
        for pos, q_row in enumerate(shuffled):
            conn.execute(
                "INSERT OR IGNORE INTO student_questions (student_id, question_id, position) VALUES (?,?,?)",
                (student_id, q_row["id"], pos),
            )
        questions = []
        for pos, q_row in enumerate(shuffled):
            q = dict(q_row)
            q["options"] = json.loads(q["options"]) if q["options"] else []
            q["position"] = pos
            questions.append(q)

    already_answered = [
        row["question_id"]
        for row in conn.execute(
            "SELECT question_id FROM answers WHERE student_id=?", (student_id,)
        ).fetchall()
    ]
    return questions, already_answered


def _force_end_quiz(session_id: int):
    with get_db() as conn:
        conn.execute("UPDATE sessions SET status='ended' WHERE id=?", (session_id,))
    socketio.emit("quiz_ended", {}, room=f"session_{session_id}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    _migrate_db()
    print("=" * 60)
    print("  Quizzer — Classroom Quiz Server")
    print("=" * 60)
    print("  Faculty dashboard : http://localhost/faculty")
    print("  Student join page : http://10.42.0.1/")
    print(f"  Faculty password  : {FACULTY_PASSWORD}")
    print("=" * 60)
    port = int(os.environ.get("PORT", 80))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
