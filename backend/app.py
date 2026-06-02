import os, cv2, json, base64, hashlib, threading, queue, time, re, secrets, logging
import urllib.request, urllib.error
import numpy as np
from contextlib import contextmanager
from datetime import datetime, date
from flask import Flask, request, jsonify, Response, g
from flask_cors import CORS
from dotenv import load_dotenv
import psycopg2, psycopg2.extras, psycopg2.pool

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("frs")

app = Flask(__name__)
CORS(app, supports_credentials=True)

_recent_marks = {}
_RECENT_MARK_TTL = 60

# ── Config ────────────────────────────────────────────────────────────────
PG_DSN    = os.getenv("DATABASE_URL", "postgresql://frs_user:frs123@localhost:5432/frs")
THRESHOLD = float(os.getenv("RECOGNITION_THRESHOLD", "0.80"))
SKIP      = int(os.getenv("FRAME_SKIP", "2"))

BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
BREVO_FROM    = os.getenv("BREVO_FROM", "")
EMAIL_ENABLED = bool(BREVO_API_KEY and BREVO_FROM)

def _brevo_headers():
    return {
        "accept":       "application/json",
        "api-key":      os.getenv("BREVO_API_KEY", ""),
        "content-type": "application/json",
    }

# ── Connection pool ───────────────────────────────────────────────────────
_pool: "psycopg2.pool.ThreadedConnectionPool | None" = None

def _get_pool():
    global _pool
    if _pool is None or _pool.closed:
        log.info("Creating DB connection pool (min=2, max=20) …")
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2, maxconn=20, dsn=PG_DSN, connect_timeout=10,
        )
        log.info("Connection pool ready.")
    return _pool

@contextmanager
def get_db(register_pgvector=True):
    pool = _get_pool()
    conn = pool.getconn()
    if register_pgvector:
        try:
            from pgvector.psycopg2 import register_vector
            register_vector(conn)
        except Exception:
            pass
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)

def qone(conn, sql, p=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(sql, p); row = c.fetchone()
        return dict(row) if row else None

def qall(conn, sql, p=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(sql, p); return [dict(r) for r in c.fetchall()]

def qexec(conn, sql, p=()):
    with conn.cursor() as c:
        c.execute(sql, p); return c.rowcount

def total_attendance_days(conn, dept=None, subject_id=None, teacher_id=None):
    if teacher_id:
        row = qone(conn, "SELECT COUNT(DISTINCT a.date) AS total_days FROM attendance a WHERE a.teacher_id = %s", (teacher_id,))
    elif subject_id:
        row = qone(conn, "SELECT COUNT(DISTINCT a.date) AS total_days FROM attendance a WHERE a.subject_id = %s", (subject_id,))
    elif dept:
        row = qone(conn, """
            SELECT COUNT(DISTINCT a.date) AS total_days FROM attendance a
            JOIN students s ON s.student_id = a.student_id WHERE s.department = %s
        """, (dept,))
    else:
        row = qone(conn, "SELECT COUNT(DISTINCT date) AS total_days FROM attendance")
    return int(row["total_days"] or 0) if row else 0

def _has_pgvector(conn):
    row = qone(conn, "SELECT 1 FROM pg_type WHERE typname = 'vector'")
    return bool(row)

def _ddl(conn, sql, label=""):
    try:
        with conn.cursor() as c:
            c.execute(sql)
        if label:
            log.debug("  DDL ok: %s", label)
    except Exception as exc:
        log.warning("  DDL warn [%s]: %s", label, exc)

PGVECTOR_AVAILABLE = False

def init_db():
    global PGVECTOR_AVAILABLE
    log.info("Initialising database schema …")
    conn = psycopg2.connect(PG_DSN, connect_timeout=10)
    conn.autocommit = True
    try:
        try:
            with conn.cursor() as c:
                c.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            log.info("pgvector extension ready.")
        except Exception as exc:
            log.warning("pgvector extension could not be created (%s). Falling back to TEXT embeddings.", exc)

        PGVECTOR_AVAILABLE = _has_pgvector(conn)
        embedding_col = "vector(512)" if PGVECTOR_AVAILABLE else "TEXT"
        log.info("Embedding column type: %s", embedding_col)

        _ddl(conn, """
            CREATE TABLE IF NOT EXISTS users (
                id            SERIAL PRIMARY KEY,
                username      TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'admin'
            )
        """, "users")

        _ddl(conn, f"""
            CREATE TABLE IF NOT EXISTS students (
                id           SERIAL  PRIMARY KEY,
                student_id   TEXT    NOT NULL UNIQUE,
                full_name    TEXT    NOT NULL,
                department   TEXT,
                email        TEXT    UNIQUE,
                phone        TEXT,
                semester     TEXT,
                status       TEXT    NOT NULL DEFAULT 'active',
                face_image   BYTEA,
                embedding    {embedding_col},
                sample_count INTEGER NOT NULL DEFAULT 0,
                enrolled_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """, "students")

        for stmt in [
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS semester     TEXT",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS status       TEXT NOT NULL DEFAULT 'active'",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS face_image   BYTEA",
            f"ALTER TABLE students ADD COLUMN IF NOT EXISTS embedding   {embedding_col}",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS sample_count INTEGER NOT NULL DEFAULT 0",
        ]:
            _ddl(conn, stmt, stmt[:55])

        _ddl(conn, """
            CREATE TABLE IF NOT EXISTS faculties (
                id         SERIAL PRIMARY KEY,
                name       TEXT NOT NULL UNIQUE,
                code       TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """, "faculties")

        _ddl(conn, """
            CREATE TABLE IF NOT EXISTS subjects (
                id         SERIAL PRIMARY KEY,
                name       TEXT NOT NULL,
                code       TEXT,
                faculty_id INTEGER REFERENCES faculties(id) ON DELETE CASCADE,
                semester   INTEGER NOT NULL CHECK (semester BETWEEN 1 AND 8),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(name, faculty_id, semester)
            )
        """, "subjects")

        _ddl(conn, """
            CREATE TABLE IF NOT EXISTS time_slots (
                id         SERIAL PRIMARY KEY,
                label      TEXT NOT NULL,
                start_time TIME NOT NULL,
                end_time   TIME NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """, "time_slots")

        _ddl(conn, """
            CREATE TABLE IF NOT EXISTS teachers (
                id            SERIAL PRIMARY KEY,
                teacher_id    TEXT NOT NULL UNIQUE,
                full_name     TEXT NOT NULL,
                email         TEXT UNIQUE,
                phone         TEXT,
                password_hash TEXT NOT NULL,
                faculty_id    INTEGER REFERENCES faculties(id),
                semester      INTEGER CHECK (semester BETWEEN 1 AND 8),
                subject_id    INTEGER REFERENCES subjects(id),
                time_slot_id  INTEGER REFERENCES time_slots(id),
                status        TEXT NOT NULL DEFAULT 'active',
                created_at    TIMESTAMPTZ DEFAULT NOW()
            )
        """, "teachers")

        # ── ISSUE 4: Teacher multiple assignments ─────────────────────────
        # A teacher can teach multiple subjects in different semesters/faculties
        _ddl(conn, """
            CREATE TABLE IF NOT EXISTS teacher_assignments (
                id           SERIAL PRIMARY KEY,
                teacher_id   INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
                faculty_id   INTEGER NOT NULL REFERENCES faculties(id) ON DELETE CASCADE,
                semester     INTEGER NOT NULL CHECK (semester BETWEEN 1 AND 8),
                subject_id   INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
                time_slot_id INTEGER REFERENCES time_slots(id),
                is_primary   BOOLEAN NOT NULL DEFAULT false,
                created_at   TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(teacher_id, faculty_id, semester, subject_id)
            )
        """, "teacher_assignments")

        # ── ISSUE 5: Time slot scheduling uniqueness ──────────────────────
        # Same faculty + semester + time slot cannot be assigned to two teachers
        _ddl(conn, """
            CREATE TABLE IF NOT EXISTS class_schedules (
                id           SERIAL PRIMARY KEY,
                faculty_id   INTEGER NOT NULL REFERENCES faculties(id) ON DELETE CASCADE,
                semester     INTEGER NOT NULL CHECK (semester BETWEEN 1 AND 8),
                time_slot_id INTEGER NOT NULL REFERENCES time_slots(id) ON DELETE CASCADE,
                teacher_id   INTEGER REFERENCES teachers(id) ON DELETE SET NULL,
                subject_id   INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
                created_at   TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(faculty_id, semester, time_slot_id)
            )
        """, "class_schedules")

        _ddl(conn, """
            CREATE TABLE IF NOT EXISTS attendance (
                id         SERIAL PRIMARY KEY,
                student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
                teacher_id INTEGER REFERENCES teachers(id),
                subject_id INTEGER REFERENCES subjects(id),
                date       DATE NOT NULL DEFAULT CURRENT_DATE,
                time       TIME NOT NULL DEFAULT CURRENT_TIME,
                status     TEXT NOT NULL DEFAULT 'Present',
                note       TEXT,
                UNIQUE (student_id, date, subject_id)
            )
        """, "attendance")

        for stmt in [
            "ALTER TABLE attendance ADD COLUMN IF NOT EXISTS teacher_id INTEGER REFERENCES teachers(id)",
            "ALTER TABLE attendance ADD COLUMN IF NOT EXISTS subject_id INTEGER REFERENCES subjects(id)",
            "ALTER TABLE attendance ADD COLUMN IF NOT EXISTS note TEXT",
        ]:
            _ddl(conn, stmt, stmt[:55])

        _ddl(conn, """
            CREATE TABLE IF NOT EXISTS recognition_logs (
                id         SERIAL PRIMARY KEY,
                student_id TEXT,
                full_name  TEXT,
                confidence REAL,
                recognized BOOLEAN NOT NULL,
                teacher_id INTEGER REFERENCES teachers(id),
                subject_id INTEGER REFERENCES subjects(id),
                logged_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """, "recognition_logs")

        for stmt in [
            "ALTER TABLE recognition_logs ADD COLUMN IF NOT EXISTS teacher_id INTEGER REFERENCES teachers(id)",
            "ALTER TABLE recognition_logs ADD COLUMN IF NOT EXISTS subject_id INTEGER REFERENCES subjects(id)",
        ]:
            _ddl(conn, stmt, stmt[:55])

        _ddl(conn, """
            CREATE TABLE IF NOT EXISTS activity_logs (
                id          SERIAL PRIMARY KEY,
                admin_user  TEXT NOT NULL,
                action      TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id   TEXT,
                detail      TEXT,
                logged_at   TIMESTAMPTZ DEFAULT NOW()
            )
        """, "activity_logs")

        _ddl(conn, """
            CREATE TABLE IF NOT EXISTS email_log (
                id          SERIAL PRIMARY KEY,
                student_id  TEXT NOT NULL,
                email_to    TEXT NOT NULL,
                subject     TEXT,
                sent_at     TIMESTAMPTZ DEFAULT NOW(),
                success     BOOLEAN NOT NULL DEFAULT true,
                error_msg   TEXT
            )
        """, "email_log")

        _ddl(conn, """
            CREATE TABLE IF NOT EXISTS sessions (
                id         SERIAL PRIMARY KEY,
                token      TEXT NOT NULL UNIQUE,
                user_type  TEXT NOT NULL,
                user_id    INTEGER,
                student_id TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                expires_at TIMESTAMPTZ DEFAULT NOW() + INTERVAL '24 hours'
            )
        """, "sessions")

        _ddl(conn,
             "CREATE INDEX IF NOT EXISTS sessions_token_idx ON sessions (token)",
             "sessions_token_idx")

        _ddl(conn, """
            INSERT INTO users (username, password_hash, role)
            VALUES ('admin',
                    '240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9',
                    'admin')
            ON CONFLICT DO NOTHING
        """, "seed admin")

        _ddl(conn, """
            INSERT INTO time_slots (label, start_time, end_time) VALUES
                ('Morning   (06:00 - 08:00)', '06:00', '08:00'),
                ('Period 1  (08:00 - 09:00)', '08:00', '09:00'),
                ('Period 2  (09:00 - 10:00)', '09:00', '10:00'),
                ('Period 3  (10:00 - 11:00)', '10:00', '11:00'),
                ('Period 4  (11:00 - 12:00)', '11:00', '12:00'),
                ('Lunch     (12:00 - 13:00)', '12:00', '13:00'),
                ('Period 5  (13:00 - 14:00)', '13:00', '14:00'),
                ('Period 6  (14:00 - 15:00)', '14:00', '15:00'),
                ('Period 7  (15:00 - 16:00)', '15:00', '16:00'),
                ('Evening   (16:00 - 18:00)', '16:00', '18:00')
            ON CONFLICT DO NOTHING
        """, "seed time_slots")

        if PGVECTOR_AVAILABLE:
            _ddl(conn, """
                CREATE INDEX IF NOT EXISTS students_embedding_hnsw
                ON students USING hnsw (embedding vector_cosine_ops)
                WITH (m=16, ef_construction=64)
            """, "hnsw index")

        log.info("Schema initialisation complete. pgvector=%s", PGVECTOR_AVAILABLE)

    finally:
        conn.close()

# ── Face model ────────────────────────────────────────────────────────────
_face_app = None

def get_face_app():
    global _face_app
    if _face_app is None:
        from insightface.app import FaceAnalysis
        _face_app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        _face_app.prepare(ctx_id=0, det_size=(640, 640))
    return _face_app

def decode_image(b64_or_bytes):
    if isinstance(b64_or_bytes, str):
        b64_or_bytes = base64.b64decode(b64_or_bytes)
    arr = np.frombuffer(b64_or_bytes, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)

def extract_embeddings(frames_b64):
    fa = get_face_app()
    embeddings, thumbnail = [], None
    for b64 in frames_b64:
        frame = decode_image(b64)
        if frame is None: continue
        rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        faces = fa.get(rgb)
        if not faces: continue
        face  = max(faces, key=lambda f:(f.bbox[2]-f.bbox[0])*(f.bbox[3]-f.bbox[1]))
        embeddings.append(face.normed_embedding)
        if thumbnail is None:
            x1,y1,x2,y2 = [max(0,int(v)) for v in face.bbox]
            crop = frame[y1:y2, x1:x2]
            if crop.size > 0:
                crop = cv2.resize(crop, (128,128))
                _, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
                thumbnail = buf.tobytes()
    if not embeddings: return None, None
    mean_emb = np.mean(embeddings, axis=0)
    mean_emb /= np.linalg.norm(mean_emb)
    return mean_emb, thumbnail

def _cosine_similarity(a, b):
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))

def find_best_match(conn, query_emb, faculty_id=None, semester=None):
    if PGVECTOR_AVAILABLE:
        if faculty_id and semester:
            row = qone(conn, """
                SELECT student_id, full_name,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM   students
                WHERE  embedding IS NOT NULL
                  AND  department = (SELECT name FROM faculties WHERE id=%s)
                  AND  semester = %s
                ORDER  BY embedding <=> %s::vector
                LIMIT  1
            """, (query_emb.tolist(), faculty_id, str(semester), query_emb.tolist()))
        else:
            row = qone(conn, """
                SELECT student_id, full_name,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM   students
                WHERE  embedding IS NOT NULL
                ORDER  BY embedding <=> %s::vector
                LIMIT  1
            """, (query_emb.tolist(), query_emb.tolist()))
        if not row:
            return None, None, 0.0
        return row["student_id"], row["full_name"], float(row["similarity"])
    else:
        if faculty_id and semester:
            rows = qall(conn, """
                SELECT student_id, full_name, embedding
                FROM   students
                WHERE  embedding IS NOT NULL
                  AND  department = (SELECT name FROM faculties WHERE id=%s)
                  AND  semester = %s
            """, (faculty_id, str(semester)))
        else:
            rows = qall(conn,
                "SELECT student_id, full_name, embedding FROM students WHERE embedding IS NOT NULL")

        best_sid, best_name, best_sim = None, None, 0.0
        for row in rows:
            raw = row["embedding"]
            if raw is None:
                continue
            try:
                stored = np.asarray(
                    json.loads(raw) if isinstance(raw, str) else list(raw),
                    dtype=np.float32,
                )
            except Exception:
                continue
            sim = _cosine_similarity(query_emb, stored)
            if sim > best_sim:
                best_sim, best_sid, best_name = sim, row["student_id"], row["full_name"]

        return best_sid, best_name, best_sim

def score_frame_quality(frame_bgr, face_bbox=None):
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()
    blur_score = min(100, int(lap_var / 3))
    mean_bright = float(np.mean(gray))
    if 50 <= mean_bright <= 200:
        brightness = int(70 + 30 * (1 - abs(mean_bright - 125) / 75))
    else:
        brightness = max(0, int(40 - abs(mean_bright - 125) / 5))
    face_size_ok = False
    if face_bbox:
        x1, y1, x2, y2 = face_bbox
        face_area  = max(0, (x2-x1)) * max(0, (y2-y1))
        frame_area = w * h
        ratio      = face_area / frame_area if frame_area else 0
        face_size_ok = ratio >= 0.03
    overall = int(blur_score * 0.5 + brightness * 0.3 + (20 if face_size_ok else 0))
    overall = min(100, overall)
    return {
        "blur_score":   blur_score,
        "brightness":   brightness,
        "face_size_ok": face_size_ok,
        "overall":      overall,
        "passed":       overall >= 50,
    }

# ── Email queue ──────────────────────────────────────────────────────────
_email_queue: queue.Queue = queue.Queue(maxsize=200)

def _email_worker():
    while True:
        try:
            job = _email_queue.get(timeout=5)
            if job is None:
                break
            _send_email_now(**job)
        except queue.Empty:
            continue
        except Exception as exc:
            log.error("[email_worker] %s", exc)

def _send_email_now(to_addr, subject, html_body, student_id, retry=0):
    api_key  = os.getenv("BREVO_API_KEY", "")
    from_addr = os.getenv("BREVO_FROM", "")
    if not (api_key and from_addr):
        log.warning("Brevo not configured — skipping email to %s", to_addr)
        return

    today = date.today().isoformat()
    try:
        with get_db() as conn:
            already = qone(conn, """
                SELECT id FROM email_log
                WHERE student_id=%s AND subject=%s
                  AND sent_at::date=%s AND success=true
            """, (student_id, subject, today))
        if already:
            return
    except Exception:
        pass

    payload = json.dumps({
        "sender":      {"name": "वेदनेत्रम् Attendance", "email": from_addr},
        "to":          [{"email": to_addr}],
        "subject":     subject,
        "htmlContent": html_body,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data    = payload,
            method  = "POST",
            headers = _brevo_headers(),
        )
        with urllib.request.urlopen(req, timeout=15):
            pass
        with get_db() as conn:
            qexec(conn, """
                INSERT INTO email_log (student_id, email_to, subject, success)
                VALUES (%s,%s,%s,true)
            """, (student_id, to_addr, subject))
        log.info("Email sent to %s (%s)", to_addr, subject)
    except Exception as exc:
        err = str(exc)
        log.warning("Email failed to %s: %s", to_addr, err)
        try:
            with get_db() as conn:
                qexec(conn, """
                    INSERT INTO email_log
                        (student_id, email_to, subject, success, error_msg)
                    VALUES (%s,%s,%s,false,%s)
                """, (student_id, to_addr, subject, err))
        except Exception:
            pass
        if retry < 2:
            time.sleep(8 * (retry + 1))
            _send_email_now(to_addr, subject, html_body, student_id, retry + 1)

def queue_attendance_email(student_id, name, dept, att_date, att_time, email_to, subject_name=None, teacher_name=None):
    if not EMAIL_ENABLED or not email_to:
        return
    pct = "—"
    try:
        with get_db() as conn:
            total_days = total_attendance_days(conn)
            row = qone(conn, """
                SELECT ROUND(100.0*COUNT(*) FILTER(WHERE status='Present')
                       /NULLIF(%s,0),1) AS pct
                FROM attendance WHERE student_id=%s
            """, (total_days, student_id))
            if row and row["pct"] is not None:
                pct = f"{row['pct']}%"
    except: pass

    subject_line = f"Attendance Confirmed — {att_date}"
    html = f"""<!DOCTYPE html><html><body style="font-family:Arial;">
<h2>Attendance Confirmed</h2>
<p>Dear {name},</p>
<p>Your attendance has been marked for {att_date} at {att_time}.</p>
<p>Subject: {subject_name or '—'} | Teacher: {teacher_name or '—'}</p>
<p>Overall Rate: {pct}</p>
</body></html>"""
    try:
        _email_queue.put_nowait({
            "to_addr": email_to, "subject": subject_line,
            "html_body": html, "student_id": student_id,
        })
    except queue.Full:
        pass

# ── SSE ───────────────────────────────────────────────────────────────────
_sse_clients = []
_sse_lock    = threading.Lock()

def sse_broadcast(data):
    msg = f"data: {json.dumps(data)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:  q.put_nowait(msg)
            except queue.Full: dead.append(q)
        for q in dead: _sse_clients.remove(q)

# ── Camera ────────────────────────────────────────────────────────────────
camera_state = {"active": False, "cap": None, "teacher_id": None, "subject_id": None}

def _gen_frames():
    fa  = get_face_app()
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    camera_state["cap"] = cap
    frame_n = 0
    teacher_id = camera_state.get("teacher_id")
    subject_id = camera_state.get("subject_id")
    faculty_id = None
    semester   = None
    if teacher_id:
        with get_db() as conn:
            t = qone(conn, "SELECT faculty_id, semester FROM teachers WHERE id=%s", (teacher_id,))
            if t:
                faculty_id = t["faculty_id"]
                semester   = t["semester"]
    while camera_state["active"]:
        ok, frame = cap.read()
        if not ok: break
        frame_n += 1
        if frame_n % SKIP == 0:
            rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            faces = fa.get(rgb)
            for face in faces:
                x1,y1,x2,y2 = [int(v) for v in face.bbox]
                emb = face.normed_embedding
                with get_db() as conn:
                    sid, name, sim = find_best_match(conn, emb, faculty_id, semester)
                label, color = "Unknown", (0, 60, 220)
                if sim >= THRESHOLD and sid:
                    label, color = name, (0, 200, 80)
                    _mark_attendance_and_broadcast(sid, name, sim, teacher_id, subject_id)
                else:
                    _log_recognition(None, "Unknown", sim, False, teacher_id, subject_id)
                cv2.rectangle(frame, (x1,y1), (x2,y2), color, 2)
                cv2.putText(frame, f"{label} ({sim*100:.0f}%)",
                            (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
    cap.release()
    camera_state["cap"] = None

# ── Attendance helpers ────────────────────────────────────────────────────
def _mark_attendance_and_broadcast(student_id, name, confidence, teacher_id=None, subject_id=None):
    today = date.today().isoformat()
    now   = datetime.now().strftime("%H:%M:%S")
    dept  = None
    email = None
    subject_name = None
    teacher_name = None
    key = (student_id, today, subject_id)
    ts = time.time()
    for k, v in list(_recent_marks.items()):
        if ts - v > _RECENT_MARK_TTL:
            _recent_marks.pop(k, None)

    recently = key in _recent_marks
    with get_db() as conn:
        with conn.cursor() as c:
            marked = False
            if not recently:
                c.execute("""
                    INSERT INTO attendance (student_id, teacher_id, subject_id, date, time, status)
                    VALUES (%s,%s,%s,%s,%s,'Present')
                    ON CONFLICT (student_id, date, subject_id) DO NOTHING
                """, (student_id, teacher_id, subject_id, today, now))
                marked = c.rowcount == 1
                if marked:
                    _recent_marks[key] = ts
            c.execute("""
                INSERT INTO recognition_logs (student_id, full_name, confidence, recognized, teacher_id, subject_id)
                VALUES (%s,%s,%s,true,%s,%s)
            """, (student_id, name, round(confidence*100,1), teacher_id, subject_id))
        if marked:
            row = qone(conn, "SELECT department, email FROM students WHERE student_id=%s", (student_id,))
            if row:
                dept  = row.get("department")
                email = row.get("email")
            if subject_id:
                s = qone(conn, "SELECT name FROM subjects WHERE id=%s", (subject_id,))
                if s: subject_name = s["name"]
            if teacher_id:
                t = qone(conn, "SELECT full_name FROM teachers WHERE id=%s", (teacher_id,))
                if t: teacher_name = t["full_name"]
    if marked:
        sse_broadcast({
            "type":"attendance","student_id":student_id,
            "name":name,"confidence":round(confidence*100,1),
            "time":now,"date":today,
            "subject": subject_name,
            "teacher": teacher_name
        })
        if email:
            queue_attendance_email(student_id, name, dept, today, now, email, subject_name, teacher_name)

def _log_recognition(sid, name, confidence, recognized, teacher_id=None, subject_id=None):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO recognition_logs (student_id, full_name, confidence, recognized, teacher_id, subject_id)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, (sid, name, round(confidence*100,1), recognized, teacher_id, subject_id))

def _log_activity(admin_user, action, target_type, target_id=None, detail=None):
    try:
        with get_db() as conn:
            qexec(conn, """
                INSERT INTO activity_logs (admin_user, action, target_type, target_id, detail)
                VALUES (%s,%s,%s,%s,%s)
            """, (admin_user, action, target_type, target_id, detail))
    except: pass

# ── Auth helpers ──────────────────────────────────────────────────────────
def _hash(pw): return hashlib.sha256(pw.encode()).hexdigest()

def _get_admin_username():
    if g.user and isinstance(g.user, dict) and "username" in g.user:
        return g.user["username"]
    return "unknown"

def _create_session(user_type, user_id=None, student_id=None):
    token = secrets.token_hex(32)
    with get_db() as conn:
        qexec(conn, """
            INSERT INTO sessions (token, user_type, user_id, student_id)
            VALUES (%s, %s, %s, %s)
        """, (token, user_type, user_id, student_id))
    return token

def _get_session(token):
    if not token:
        return None
    with get_db() as conn:
        sess = qone(conn, """
            SELECT * FROM sessions
            WHERE token=%s AND expires_at > NOW()
        """, (token,))
    return sess

def require_auth(roles=None):
    from functools import wraps
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            token = request.headers.get("Authorization","").replace("Bearer ","")
            sess  = _get_session(token)
            if not sess:
                # ── ISSUE 6 FIX: Return structured error so frontend can detect expired session ──
                return jsonify({"error":"Unauthorized", "code":"SESSION_EXPIRED"}), 401
            allowed = roles or ["admin", "teacher", "student"]
            if sess["user_type"] not in allowed:
                return jsonify({"error":"Forbidden"}), 403
            g.session  = sess
            g.role     = sess["user_type"]
            g.user_id  = sess.get("user_id")
            g.student_id = sess.get("student_id")
            if sess["user_type"] == "admin" and sess.get("user_id"):
                with get_db() as conn:
                    g.user = qone(conn, "SELECT id, username, role FROM users WHERE id=%s", (sess["user_id"],))
            elif sess["user_type"] == "teacher" and sess.get("user_id"):
                with get_db() as conn:
                    g.user = qone(conn, """
                        SELECT t.*, f.name AS faculty_name, sub.name AS subject_name,
                               ts.label AS time_slot_label, ts.start_time::text, ts.end_time::text
                        FROM teachers t
                        LEFT JOIN faculties f ON f.id = t.faculty_id
                        LEFT JOIN subjects sub ON sub.id = t.subject_id
                        LEFT JOIN time_slots ts ON ts.id = t.time_slot_id
                        WHERE t.id=%s
                    """, (sess["user_id"],))
            elif sess["user_type"] == "student":
                with get_db() as conn:
                    g.user = qone(conn, "SELECT * FROM students WHERE student_id=%s", (sess.get("student_id"),))
            else:
                g.user = None
            return fn(*args, **kwargs)
        return wrapper
    return decorator

def require_admin(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = request.headers.get("Authorization","").replace("Bearer ","")
        # New session-based auth
        sess = _get_session(token)
        if sess and sess["user_type"] == "admin":
            with get_db() as conn:
                g.user = qone(conn, "SELECT id, username, role FROM users WHERE id=%s", (sess["user_id"],))
            g.session = sess
            g.role = "admin"
            return fn(*args, **kwargs)
        # Legacy fallback: password_hash as token
        with get_db() as conn:
            user = qone(conn,
                "SELECT id,username,role FROM users WHERE password_hash=%s AND role='admin'",(token,))
        if not user:
            # ── ISSUE 6 FIX: Structured error for expired/invalid session ──
            return jsonify({"error":"Unauthorized", "code":"SESSION_EXPIRED"}), 401
        g.user = user
        g.role = "admin"
        g.session = None
        return fn(*args, **kwargs)
    return wrapper

# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES — HEALTH
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/health")
def health():
    try:
        with get_db() as conn:
            qone(conn, "SELECT 1")
        return jsonify({
            "status":    "ok",
            "db":        "ok",
            "email":     "enabled" if EMAIL_ENABLED else "disabled",
            "timestamp": datetime.now().isoformat(),
            "version":   "3.1",
        })
    except Exception as e:
        return jsonify({"status": "error", "db": "error", "detail": str(e)}), 503

# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES — AUTHENTICATION
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/auth/login", methods=["POST"])
def login():
    d = request.json or {}
    role_hint = d.get("role", "admin")

    if role_hint == "student":
        email = (d.get("email") or "").strip().lower()
        if not email:
            return jsonify({"error": "Email required for student login"}), 400
        with get_db() as conn:
            student = qone(conn,
                "SELECT * FROM students WHERE LOWER(email)=%s AND status='active'",
                (email,))
        if not student:
            return jsonify({"error": "Student not found or inactive"}), 401
        token = _create_session("student", student_id=student["student_id"])
        return jsonify({
            "token":      token,
            "role":       "student",
            "student_id": student["student_id"],
            "full_name":  student["full_name"],
            "department": student["department"],
            "semester":   student["semester"],
        })

    elif role_hint == "teacher":
        email = (d.get("email") or "").strip().lower()
        password = d.get("password", "")
        if not email:
            return jsonify({"error": "Email required for teacher login"}), 400
        with get_db() as conn:
            teacher = qone(conn,
                "SELECT * FROM teachers WHERE LOWER(email)=%s AND password_hash=%s AND status='active'",
                (email, _hash(password)))
        if not teacher:
            return jsonify({"error": "Invalid credentials"}), 401
        token = _create_session("teacher", user_id=teacher["id"])
        with get_db() as conn:
            full = qone(conn, """
                SELECT t.*, f.name AS faculty_name, sub.name AS subject_name,
                       ts.label AS time_slot_label
                FROM teachers t
                LEFT JOIN faculties f ON f.id = t.faculty_id
                LEFT JOIN subjects sub ON sub.id = t.subject_id
                LEFT JOIN time_slots ts ON ts.id = t.time_slot_id
                WHERE t.id=%s
            """, (teacher["id"],))
            # Load all assignments
            assignments = qall(conn, """
                SELECT ta.*, f.name AS faculty_name, sub.name AS subject_name,
                       ts.label AS time_slot_label
                FROM teacher_assignments ta
                LEFT JOIN faculties f ON f.id = ta.faculty_id
                LEFT JOIN subjects sub ON sub.id = ta.subject_id
                LEFT JOIN time_slots ts ON ts.id = ta.time_slot_id
                WHERE ta.teacher_id = %s
                ORDER BY ta.faculty_id, ta.semester, ta.subject_id
            """, (teacher["id"],))
        return jsonify({
            "token":        token,
            "role":         "teacher",
            "teacher_id":   teacher["teacher_id"],
            "full_name":    teacher["full_name"],
            "faculty":      full.get("faculty_name") if full else None,
            "semester":     teacher["semester"],
            "subject":      full.get("subject_name") if full else None,
            "time_slot":    full.get("time_slot_label") if full else None,
            "faculty_id":   teacher["faculty_id"],
            "subject_id":   teacher["subject_id"],
            "time_slot_id": teacher["time_slot_id"],
            "db_id":        teacher["id"],
            "assignments":  assignments,
        })

    else:  # admin
        username = (d.get("username") or "").strip()
        password = d.get("password", "")
        with get_db() as conn:
            user = qone(conn,
                "SELECT id,username,role,password_hash FROM users WHERE username=%s AND password_hash=%s",
                (username, _hash(password)))
        if not user:
            return jsonify({"error": "Invalid credentials"}), 401
        token = _create_session("admin", user_id=user["id"])
        return jsonify({
            "token":        token,
            "legacy_token": user["password_hash"],
            "role":         user["role"],
            "username":     user["username"]
        })

# ── ISSUE 6 FIX: Token validation endpoint ─────────────────────────────
@app.route("/api/auth/validate")
def validate_token():
    """
    Check if the current token is still valid without requiring auth decorator.
    Frontend calls this on page load to detect stale localStorage tokens.
    Returns 200 {valid:true} or 200 {valid:false} — never 401.
    """
    token = request.headers.get("Authorization","").replace("Bearer ","")
    if not token:
        return jsonify({"valid": False, "reason": "no_token"})
    sess = _get_session(token)
    if not sess:
        return jsonify({"valid": False, "reason": "session_expired"})
    return jsonify({"valid": True, "user_type": sess["user_type"]})

@app.route("/api/auth/me")
def me():
    token = request.headers.get("Authorization","").replace("Bearer ","")
    sess = _get_session(token)
    if sess:
        return jsonify({"user_type": sess["user_type"], "user_id": sess.get("user_id"),
                        "student_id": sess.get("student_id")})
    with get_db() as conn:
        user = qone(conn, "SELECT id,username,role FROM users WHERE password_hash=%s",(token,))
    if not user: return jsonify({"error":"Unauthorized", "code":"SESSION_EXPIRED"}), 401
    return jsonify(user)

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    token = request.headers.get("Authorization","").replace("Bearer ","")
    if token:
        try:
            with get_db() as conn:
                qexec(conn, "DELETE FROM sessions WHERE token=%s", (token,))
        except: pass
    return jsonify({"ok": True})

@app.route("/api/auth/change-password", methods=["POST"])
@require_admin
def change_password():
    d = request.json or {}
    old_pw  = d.get("old_password","")
    new_pw  = d.get("new_password","")
    if not new_pw or len(new_pw) < 6:
        return jsonify({"error":"New password must be at least 6 characters"}), 400
    with get_db() as conn:
        user = qone(conn,
            "SELECT id FROM users WHERE id=%s AND password_hash=%s",
            (g.user["id"], _hash(old_pw)))
        if not user: return jsonify({"error":"Current password is incorrect"}), 401
        qexec(conn,
            "UPDATE users SET password_hash=%s WHERE id=%s",
            (_hash(new_pw), g.user["id"]))
    _log_activity(g.user["username"], "change_password", "user", str(g.user["id"]))
    return jsonify({"updated": True})

# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES — FACULTIES
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/faculties")
def list_faculties():
    with get_db() as conn:
        rows = qall(conn, "SELECT * FROM faculties ORDER BY name")
    return jsonify({"faculties": rows})

@app.route("/api/faculties", methods=["POST"])
@require_admin
def create_faculty():
    d = request.json or {}
    name = (d.get("name") or "").strip()
    code = (d.get("code") or "").strip() or None
    if not name:
        return jsonify({"error": "Faculty name required"}), 400
    with get_db() as conn:
        existing = qone(conn, "SELECT id FROM faculties WHERE LOWER(name)=LOWER(%s)", (name,))
        if existing:
            return jsonify({"error": "Faculty already exists"}), 409
        with conn.cursor() as c:
            c.execute("INSERT INTO faculties (name, code) VALUES (%s,%s) RETURNING id", (name, code))
            new_id = c.fetchone()[0]
    _log_activity(g.user["username"], "create_faculty", "faculty", str(new_id), name)
    return jsonify({"id": new_id, "name": name}), 201

@app.route("/api/faculties/<int:fid>", methods=["PUT"])
@require_admin
def update_faculty(fid):
    d = request.json or {}
    name = (d.get("name") or "").strip()
    code = (d.get("code") or "").strip() or None
    if not name:
        return jsonify({"error": "Faculty name required"}), 400
    with get_db() as conn:
        rows = qexec(conn, "UPDATE faculties SET name=%s, code=%s WHERE id=%s", (name, code, fid))
    if rows == 0:
        return jsonify({"error": "Faculty not found"}), 404
    _log_activity(g.user["username"], "update_faculty", "faculty", str(fid), name)
    return jsonify({"updated": True})

@app.route("/api/faculties/<int:fid>", methods=["DELETE"])
@require_admin
def delete_faculty(fid):
    with get_db() as conn:
        rows = qexec(conn, "DELETE FROM faculties WHERE id=%s", (fid,))
    if rows == 0:
        return jsonify({"error": "Faculty not found"}), 404
    _log_activity(g.user["username"], "delete_faculty", "faculty", str(fid))
    return jsonify({"deleted": True})

# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES — SUBJECTS
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/subjects")
def list_subjects():
    faculty_id = request.args.get("faculty_id")
    semester   = request.args.get("semester")
    sql = """
        SELECT s.*, f.name AS faculty_name
        FROM subjects s LEFT JOIN faculties f ON f.id = s.faculty_id
        WHERE 1=1
    """
    params = []
    if faculty_id:
        sql += " AND s.faculty_id=%s"; params.append(faculty_id)
    if semester:
        sql += " AND s.semester=%s"; params.append(semester)
    sql += " ORDER BY f.name, s.semester, s.name"
    with get_db() as conn:
        rows = qall(conn, sql, params)
    return jsonify({"subjects": rows})

@app.route("/api/semesters")
def list_semesters():
    faculty_id = request.args.get("faculty_id")
    if not faculty_id:
        return jsonify({"semesters": []})
    with get_db() as conn:
        rows = qall(conn, """
            SELECT gs.semester,
                   COUNT(s.id) AS subject_count
            FROM generate_series(1, 8) AS gs(semester)
            LEFT JOIN subjects s
                   ON s.semester = gs.semester
                  AND s.faculty_id = %s
            GROUP BY gs.semester
            ORDER BY gs.semester
        """, (faculty_id,))
    return jsonify({
        "semesters": [
            {
                "value": int(r["semester"]),
                "name": f"Semester {int(r['semester'])}",
                "subject_count": int(r["subject_count"] or 0),
            }
            for r in rows
        ]
    })

@app.route("/api/subjects", methods=["POST"])
@require_admin
def create_subject():
    d = request.json or {}
    name       = (d.get("name") or "").strip()
    code       = (d.get("code") or "").strip() or None
    faculty_id = d.get("faculty_id")
    semester   = d.get("semester")
    if not name or not faculty_id or not semester:
        return jsonify({"error": "name, faculty_id, semester required"}), 400
    semester = int(semester)
    if not (1 <= semester <= 8):
        return jsonify({"error": "Semester must be between 1 and 8"}), 400
    with get_db() as conn:
        faculty = qone(conn, "SELECT id FROM faculties WHERE id=%s", (faculty_id,))
        if not faculty:
            return jsonify({"error": "Faculty not found"}), 404
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO subjects (name, code, faculty_id, semester)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (name, faculty_id, semester) DO UPDATE SET code=EXCLUDED.code
                RETURNING id
            """, (name, code, faculty_id, semester))
            new_id = c.fetchone()[0]
    _log_activity(g.user["username"], "create_subject", "subject", str(new_id), name)
    return jsonify({"id": new_id, "name": name}), 201

@app.route("/api/subjects/<int:sid>", methods=["PUT"])
@require_admin
def update_subject(sid):
    d = request.json or {}
    name       = (d.get("name") or "").strip()
    code       = (d.get("code") or "").strip()
    faculty_id = d.get("faculty_id")
    semester   = d.get("semester")
    if faculty_id:
        with get_db() as conn:
            faculty = qone(conn, "SELECT id FROM faculties WHERE id=%s", (faculty_id,))
        if not faculty:
            return jsonify({"error": "Faculty not found"}), 404
    if semester:
        semester = int(semester)
        if not (1 <= semester <= 8):
            return jsonify({"error": "Semester must be between 1 and 8"}), 400
    sets, params = [], []
    if name:       sets.append("name=%s");       params.append(name)
    if "code" in d: sets.append("code=%s");      params.append(code or None)
    if faculty_id: sets.append("faculty_id=%s"); params.append(faculty_id)
    if semester: sets.append("semester=%s"); params.append(semester)
    if not sets: return jsonify({"error": "Nothing to update"}), 400
    params.append(sid)
    with get_db() as conn:
        rows = qexec(conn, f"UPDATE subjects SET {','.join(sets)} WHERE id=%s", params)
    if rows == 0:
        return jsonify({"error": "Subject not found"}), 404
    _log_activity(g.user["username"], "update_subject", "subject", str(sid))
    return jsonify({"updated": True})

@app.route("/api/subjects/<int:sid>", methods=["DELETE"])
@require_admin
def delete_subject(sid):
    with get_db() as conn:
        rows = qexec(conn, "DELETE FROM subjects WHERE id=%s", (sid,))
    if rows == 0:
        return jsonify({"error": "Subject not found"}), 404
    _log_activity(g.user["username"], "delete_subject", "subject", str(sid))
    return jsonify({"deleted": True})

# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES — TIME SLOTS
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/time-slots")
def list_time_slots():
    with get_db() as conn:
        rows = qall(conn, """
            SELECT ts.id, ts.label,
                   ts.start_time::text AS start_time,
                   ts.end_time::text   AS end_time,
                   ts.created_at::text AS created_at,
                   -- Scheduling info (ISSUE 2: time slots now have real meaning)
                   cs.faculty_id, cs.semester,
                   f.name AS assigned_faculty,
                   t.full_name AS assigned_teacher,
                   sub.name AS assigned_subject
            FROM time_slots ts
            LEFT JOIN class_schedules cs ON cs.time_slot_id = ts.id
            LEFT JOIN faculties f ON f.id = cs.faculty_id
            LEFT JOIN teachers t ON t.id = cs.teacher_id
            LEFT JOIN subjects sub ON sub.id = cs.subject_id
            ORDER BY ts.start_time
        """)
    return jsonify({"time_slots": rows})

@app.route("/api/time-slots", methods=["POST"])
@require_admin
def create_time_slot():
    d = request.json or {}
    label      = (d.get("label") or "").strip()
    start_time = (d.get("start_time") or "").strip()
    end_time   = (d.get("end_time") or "").strip()
    if not label or not start_time or not end_time:
        return jsonify({"error": "label, start_time, end_time required"}), 400
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("INSERT INTO time_slots (label, start_time, end_time) VALUES (%s,%s,%s) RETURNING id",
                      (label, start_time, end_time))
            new_id = c.fetchone()[0]
    _log_activity(g.user["username"], "create_time_slot", "time_slot", str(new_id), label)
    return jsonify({"id": new_id, "label": label}), 201

@app.route("/api/time-slots/<int:tsid>", methods=["DELETE"])
@require_admin
def delete_time_slot(tsid):
    with get_db() as conn:
        rows = qexec(conn, "DELETE FROM time_slots WHERE id=%s", (tsid,))
    if rows == 0:
        return jsonify({"error": "Not found"}), 404
    _log_activity(g.user["username"], "delete_time_slot", "time_slot", str(tsid))
    return jsonify({"deleted": True})

# ── ISSUE 2: Class schedule endpoints (time slots now own a schedule slot) ──
@app.route("/api/schedule")
def get_schedule():
    """Return weekly class schedule for a faculty + semester."""
    faculty_id = request.args.get("faculty_id")
    semester   = request.args.get("semester")
    if not faculty_id or not semester:
        return jsonify({"error": "faculty_id and semester required"}), 400
    with get_db() as conn:
        rows = qall(conn, """
            SELECT cs.id, cs.time_slot_id,
                   ts.label AS time_slot_label,
                   ts.start_time::text, ts.end_time::text,
                   t.full_name AS teacher_name, t.teacher_id,
                   sub.name AS subject_name, sub.code AS subject_code
            FROM class_schedules cs
            JOIN time_slots ts ON ts.id = cs.time_slot_id
            LEFT JOIN teachers t ON t.id = cs.teacher_id
            LEFT JOIN subjects sub ON sub.id = cs.subject_id
            WHERE cs.faculty_id = %s AND cs.semester = %s
            ORDER BY ts.start_time
        """, (faculty_id, semester))
    return jsonify({"schedule": rows})

@app.route("/api/schedule", methods=["POST"])
@require_admin
def assign_schedule():
    """
    ISSUE 5: Assign a teacher to a time slot for a given faculty+semester.
    Enforces UNIQUE(faculty_id, semester, time_slot_id).
    """
    d = request.json or {}
    faculty_id   = d.get("faculty_id")
    semester     = d.get("semester")
    time_slot_id = d.get("time_slot_id")
    teacher_id   = d.get("teacher_id")
    subject_id   = d.get("subject_id")

    if not all([faculty_id, semester, time_slot_id]):
        return jsonify({"error": "faculty_id, semester, time_slot_id required"}), 400

    semester = int(semester)
    if not (1 <= semester <= 8):
        return jsonify({"error": "Semester must be between 1 and 8"}), 400

    with get_db() as conn:
        # Validate faculty exists
        fac = qone(conn, "SELECT id, name FROM faculties WHERE id=%s", (faculty_id,))
        if not fac:
            return jsonify({"error": "Faculty not found"}), 404

        # Check for conflict (ISSUE 5)
        existing = qone(conn, """
            SELECT cs.id, t.full_name AS teacher_name
            FROM class_schedules cs
            LEFT JOIN teachers t ON t.id = cs.teacher_id
            WHERE cs.faculty_id=%s AND cs.semester=%s AND cs.time_slot_id=%s
        """, (faculty_id, semester, time_slot_id))

        if existing:
            ts = qone(conn, "SELECT label FROM time_slots WHERE id=%s", (time_slot_id,))
            return jsonify({
                "error": f"Schedule conflict: {fac['name']} Semester {semester} at "
                         f"'{ts['label'] if ts else time_slot_id}' is already assigned to "
                         f"{existing['teacher_name'] or 'another teacher'}.",
                "code":  "SCHEDULE_CONFLICT",
                "conflict_id": existing["id"]
            }), 409

        with conn.cursor() as c:
            c.execute("""
                INSERT INTO class_schedules (faculty_id, semester, time_slot_id, teacher_id, subject_id)
                VALUES (%s,%s,%s,%s,%s)
                RETURNING id
            """, (faculty_id, semester, time_slot_id, teacher_id, subject_id))
            new_id = c.fetchone()[0]

    _log_activity(g.user["username"], "assign_schedule", "schedule", str(new_id))
    return jsonify({"id": new_id, "assigned": True}), 201

@app.route("/api/schedule/<int:sid>", methods=["DELETE"])
@require_admin
def remove_schedule(sid):
    with get_db() as conn:
        rows = qexec(conn, "DELETE FROM class_schedules WHERE id=%s", (sid,))
    if rows == 0:
        return jsonify({"error": "Schedule not found"}), 404
    return jsonify({"deleted": True})

# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES — TEACHERS
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/teachers")
@require_admin
def list_teachers():
    with get_db() as conn:
        rows = qall(conn, """
            SELECT t.id, t.teacher_id, t.full_name, t.email, t.phone, t.status,
                   t.semester, t.faculty_id, t.subject_id, t.time_slot_id,
                   f.name AS faculty_name, sub.name AS subject_name,
                   ts.label AS time_slot_label,
                   ts.start_time::text AS start_time,
                   ts.end_time::text   AS end_time,
                   t.created_at::text
            FROM teachers t
            LEFT JOIN faculties f ON f.id = t.faculty_id
            LEFT JOIN subjects sub ON sub.id = t.subject_id
            LEFT JOIN time_slots ts ON ts.id = t.time_slot_id
            ORDER BY t.full_name
        """)
        # Attach assignments to each teacher
        for row in rows:
            row["assignments"] = qall(conn, """
                SELECT ta.id, ta.faculty_id, ta.semester, ta.subject_id, ta.time_slot_id,
                       ta.is_primary,
                       f.name AS faculty_name, sub.name AS subject_name,
                       ts.label AS time_slot_label
                FROM teacher_assignments ta
                LEFT JOIN faculties f ON f.id = ta.faculty_id
                LEFT JOIN subjects sub ON sub.id = ta.subject_id
                LEFT JOIN time_slots ts ON ts.id = ta.time_slot_id
                WHERE ta.teacher_id = %s
                ORDER BY ta.is_primary DESC, ta.faculty_id, ta.semester
            """, (row["id"],))
    return jsonify({"teachers": rows})

@app.route("/api/teachers/<int:tid>")
@require_admin
def get_teacher(tid):
    with get_db() as conn:
        row = qone(conn, """
            SELECT t.id, t.teacher_id, t.full_name, t.email, t.phone, t.status,
                   t.semester, t.faculty_id, t.subject_id, t.time_slot_id,
                   f.name AS faculty_name, sub.name AS subject_name,
                   ts.label AS time_slot_label, ts.start_time::text, ts.end_time::text,
                   t.created_at::text
            FROM teachers t
            LEFT JOIN faculties f ON f.id = t.faculty_id
            LEFT JOIN subjects sub ON sub.id = t.subject_id
            LEFT JOIN time_slots ts ON ts.id = t.time_slot_id
            WHERE t.id=%s
        """, (tid,))
        if not row: return jsonify({"error": "Not found"}), 404
        row["assignments"] = qall(conn, """
            SELECT ta.id, ta.faculty_id, ta.semester, ta.subject_id, ta.time_slot_id,
                   ta.is_primary,
                   f.name AS faculty_name, sub.name AS subject_name,
                   ts.label AS time_slot_label
            FROM teacher_assignments ta
            LEFT JOIN faculties f ON f.id = ta.faculty_id
            LEFT JOIN subjects sub ON sub.id = ta.subject_id
            LEFT JOIN time_slots ts ON ts.id = ta.time_slot_id
            WHERE ta.teacher_id = %s
            ORDER BY ta.is_primary DESC, ta.faculty_id, ta.semester
        """, (tid,))
    return jsonify(row)

@app.route("/api/teachers", methods=["POST"])
@require_admin
def create_teacher():
    d = request.json or {}
    teacher_id   = (d.get("teacher_id") or "").strip()
    full_name    = (d.get("full_name") or "").strip()
    password     = (d.get("password") or "").strip()
    email        = (d.get("email") or "").strip() or None
    phone        = (d.get("phone") or "").strip() or None
    faculty_id   = d.get("faculty_id") or None
    semester     = d.get("semester") or None
    subject_id   = d.get("subject_id") or None
    time_slot_id = d.get("time_slot_id") or None

    if not teacher_id or not full_name or not password:
        return jsonify({"error": "teacher_id, full_name, password required"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify({"error": "Invalid email format"}), 400
    if semester:
        semester = int(semester)
        if not (1 <= semester <= 8):
            return jsonify({"error": "Semester must be between 1 and 8"}), 400

    # ── ISSUE 3 FIX: Validate subject belongs to the declared faculty+semester ──
    if subject_id and faculty_id and semester:
        with get_db() as conn:
            subj = qone(conn,
                "SELECT id FROM subjects WHERE id=%s AND faculty_id=%s AND semester=%s",
                (subject_id, faculty_id, semester))
        if not subj:
            return jsonify({
                "error": "Selected subject does not belong to the selected faculty and semester. "
                         "Please select a subject from the correct semester."
            }), 400

    with get_db() as conn:
        existing = qone(conn, "SELECT id FROM teachers WHERE teacher_id=%s", (teacher_id,))
        if existing:
            return jsonify({"error": "Teacher ID already exists"}), 409
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO teachers
                    (teacher_id, full_name, password_hash, email, phone,
                     faculty_id, semester, subject_id, time_slot_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (teacher_id, full_name, _hash(password), email, phone,
                  faculty_id, semester, subject_id, time_slot_id))
            new_id = c.fetchone()[0]

        # ── ISSUE 4: Create primary assignment record ──────────────────────
        if faculty_id and semester and subject_id:
            try:
                qexec(conn, """
                    INSERT INTO teacher_assignments
                        (teacher_id, faculty_id, semester, subject_id, time_slot_id, is_primary)
                    VALUES (%s,%s,%s,%s,%s,true)
                    ON CONFLICT (teacher_id, faculty_id, semester, subject_id) DO NOTHING
                """, (new_id, faculty_id, semester, subject_id, time_slot_id))
            except Exception as e:
                log.warning("Could not create primary assignment: %s", e)

    _log_activity(g.user["username"], "create_teacher", "teacher", str(new_id), full_name)
    return jsonify({"id": new_id, "teacher_id": teacher_id}), 201

@app.route("/api/teachers/<int:tid>", methods=["PUT"])
@require_admin
def update_teacher(tid):
    d = request.json or {}
    ALLOWED = {"full_name","email","phone","faculty_id","semester","subject_id","time_slot_id","status"}
    fields  = {k: d[k] for k in ALLOWED if k in d}

    if d.get("password"):
        pw = d["password"].strip()
        if len(pw) < 6:
            return jsonify({"error": "Password must be at least 6 characters"}), 400
        fields["password_hash"] = _hash(pw)

    if not fields:
        return jsonify({"error": "Nothing to update"}), 400

    if "email" in fields and fields["email"]:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", fields["email"]):
            return jsonify({"error": "Invalid email format"}), 400
    if "semester" in fields and fields["semester"]:
        fields["semester"] = int(fields["semester"])
        if not (1 <= fields["semester"] <= 8):
            return jsonify({"error": "Semester must be between 1 and 8"}), 400

    # ── ISSUE 3 FIX: Validate subject/faculty/semester consistency on update ──
    subject_id = fields.get("subject_id") or d.get("subject_id")
    faculty_id = fields.get("faculty_id") or d.get("faculty_id")
    semester   = fields.get("semester") or d.get("semester")
    if subject_id and faculty_id and semester:
        with get_db() as conn:
            subj = qone(conn,
                "SELECT id FROM subjects WHERE id=%s AND faculty_id=%s AND semester=%s",
                (subject_id, faculty_id, int(semester)))
        if not subj:
            return jsonify({
                "error": "Selected subject does not belong to the selected faculty and semester."
            }), 400

    sql = "UPDATE teachers SET " + ", ".join(f"{k}=%s" for k in fields) + " WHERE id=%s"
    with get_db() as conn:
        rows = qexec(conn, sql, list(fields.values()) + [tid])
    if rows == 0:
        return jsonify({"error": "Teacher not found"}), 404
    _log_activity(g.user["username"], "update_teacher", "teacher", str(tid),
                  f"Updated fields: {', '.join(fields.keys())}")
    return jsonify({"updated": True})

@app.route("/api/teachers/<int:tid>", methods=["DELETE"])
@require_admin
def delete_teacher(tid):
    try:
        with get_db() as conn:
            att_ref = qone(conn, "SELECT COUNT(*) as cnt FROM attendance WHERE teacher_id=%s", (tid,))
            att_count = att_ref.get("cnt", 0) if att_ref else 0
            rec_ref = qone(conn, "SELECT COUNT(*) as cnt FROM recognition_logs WHERE teacher_id=%s", (tid,))
            rec_count = rec_ref.get("cnt", 0) if rec_ref else 0

            if att_count > 0 or rec_count > 0:
                return jsonify({
                    "error": f"Cannot delete teacher: {att_count} attendance and {rec_count} recognition records reference this teacher.",
                    "attendance_count": att_count,
                    "recognition_count": rec_count,
                }), 409

            rows = qexec(conn, "DELETE FROM teachers WHERE id=%s", (tid,))
        if rows == 0:
            return jsonify({"error": "Teacher not found"}), 404
        _log_activity(_get_admin_username(), "delete_teacher", "teacher", str(tid))
        return jsonify({"deleted": True})
    except Exception as e:
        log.error("delete_teacher error: %s", e)
        return jsonify({"error": f"Server error: {str(e)}"}), 500

# ── ISSUE 4: Teacher multiple assignment endpoints ────────────────────────
@app.route("/api/teachers/<int:tid>/assignments")
@require_admin
def list_teacher_assignments(tid):
    with get_db() as conn:
        rows = qall(conn, """
            SELECT ta.id, ta.faculty_id, ta.semester, ta.subject_id, ta.time_slot_id,
                   ta.is_primary, ta.created_at::text,
                   f.name AS faculty_name, sub.name AS subject_name, sub.code AS subject_code,
                   ts.label AS time_slot_label, ts.start_time::text, ts.end_time::text
            FROM teacher_assignments ta
            LEFT JOIN faculties f ON f.id = ta.faculty_id
            LEFT JOIN subjects sub ON sub.id = ta.subject_id
            LEFT JOIN time_slots ts ON ts.id = ta.time_slot_id
            WHERE ta.teacher_id = %s
            ORDER BY ta.is_primary DESC, ta.faculty_id, ta.semester
        """, (tid,))
    return jsonify({"assignments": rows})

@app.route("/api/teachers/<int:tid>/assignments", methods=["POST"])
@require_admin
def add_teacher_assignment(tid):
    d = request.json or {}
    faculty_id   = d.get("faculty_id")
    semester     = d.get("semester")
    subject_id   = d.get("subject_id")
    time_slot_id = d.get("time_slot_id") or None
    is_primary   = bool(d.get("is_primary", False))

    if not all([faculty_id, semester, subject_id]):
        return jsonify({"error": "faculty_id, semester, subject_id required"}), 400

    semester = int(semester)
    if not (1 <= semester <= 8):
        return jsonify({"error": "Semester must be between 1 and 8"}), 400

    with get_db() as conn:
        # Verify teacher exists
        teacher = qone(conn, "SELECT id FROM teachers WHERE id=%s", (tid,))
        if not teacher:
            return jsonify({"error": "Teacher not found"}), 404

        # ISSUE 3 validation: subject must belong to faculty+semester
        subj = qone(conn,
            "SELECT id FROM subjects WHERE id=%s AND faculty_id=%s AND semester=%s",
            (subject_id, faculty_id, semester))
        if not subj:
            return jsonify({
                "error": "Subject does not belong to the selected faculty and semester."
            }), 400

        # Check for schedule conflict (ISSUE 5) if time_slot_id given
        if time_slot_id:
            conflict = qone(conn, """
                SELECT cs.id, t.full_name AS teacher_name
                FROM class_schedules cs
                LEFT JOIN teachers t ON t.id = cs.teacher_id
                WHERE cs.faculty_id=%s AND cs.semester=%s AND cs.time_slot_id=%s
                  AND cs.teacher_id != %s
            """, (faculty_id, semester, time_slot_id, tid))
            if conflict:
                ts = qone(conn, "SELECT label FROM time_slots WHERE id=%s", (time_slot_id,))
                fac = qone(conn, "SELECT name FROM faculties WHERE id=%s", (faculty_id,))
                return jsonify({
                    "error": f"Schedule conflict: {fac['name'] if fac else faculty_id} "
                             f"Semester {semester} at '{ts['label'] if ts else time_slot_id}' "
                             f"is already assigned to {conflict['teacher_name'] or 'another teacher'}.",
                    "code": "SCHEDULE_CONFLICT"
                }), 409

        try:
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO teacher_assignments
                        (teacher_id, faculty_id, semester, subject_id, time_slot_id, is_primary)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    RETURNING id
                """, (tid, faculty_id, semester, subject_id, time_slot_id, is_primary))
                new_id = c.fetchone()[0]

            # Also create/update class schedule entry
            if time_slot_id:
                qexec(conn, """
                    INSERT INTO class_schedules (faculty_id, semester, time_slot_id, teacher_id, subject_id)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (faculty_id, semester, time_slot_id) DO NOTHING
                """, (faculty_id, semester, time_slot_id, tid, subject_id))

        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                return jsonify({"error": "This assignment already exists for this teacher."}), 409
            raise

    _log_activity(g.user["username"], "add_teacher_assignment", "teacher_assignment", str(new_id))
    return jsonify({"id": new_id, "assigned": True}), 201

@app.route("/api/teachers/<int:tid>/assignments/<int:aid>", methods=["DELETE"])
@require_admin
def remove_teacher_assignment(tid, aid):
    with get_db() as conn:
        # Check if primary — warn if removing primary
        assignment = qone(conn,
            "SELECT is_primary FROM teacher_assignments WHERE id=%s AND teacher_id=%s",
            (aid, tid))
        if not assignment:
            return jsonify({"error": "Assignment not found"}), 404

        qexec(conn, "DELETE FROM teacher_assignments WHERE id=%s AND teacher_id=%s", (aid, tid))
    _log_activity(g.user["username"], "remove_teacher_assignment", "teacher_assignment", str(aid))
    return jsonify({"deleted": True, "was_primary": assignment.get("is_primary", False)})

# ── Teacher reference management (unchanged from original) ────────────────
@app.route("/api/teachers/<int:tid>/references")
@require_admin
def teacher_references(tid):
    with get_db() as conn:
        rows = qall(conn, """
            SELECT a.id, a.student_id, s.full_name, a.date, a.time, a.status, a.note
            FROM attendance a
            JOIN students s ON s.student_id = a.student_id
            WHERE a.teacher_id=%s ORDER BY a.date DESC, a.time DESC LIMIT 500
        """, (tid,))
    return jsonify({"rows": rows, "count": len(rows)})

@app.route("/api/teachers/<int:tid>/clear-attendance", methods=["POST"])
@require_admin
def clear_teacher_attendance(tid):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE attendance SET teacher_id=NULL WHERE teacher_id=%s", (tid,))
            cleared = c.rowcount
    return jsonify({"cleared": cleared})

@app.route("/api/teachers/<int:tid>/reassign-attendance", methods=["POST"])
@require_admin
def reassign_teacher_attendance(tid):
    d = request.json or {}
    target_tid = d.get("to")
    if not target_tid:
        return jsonify({"error": "Target teacher ID required"}), 400
    with get_db() as conn:
        target = qone(conn, "SELECT id FROM teachers WHERE id=%s", (target_tid,))
        if not target:
            return jsonify({"error": "Target teacher not found"}), 404
        with conn.cursor() as c:
            c.execute("UPDATE attendance SET teacher_id=%s WHERE teacher_id=%s", (target_tid, tid))
            reassigned = c.rowcount
    return jsonify({"reassigned": reassigned})

@app.route("/api/teachers/<int:tid>/recognition-logs")
@require_admin
def teacher_recognition_logs(tid):
    with get_db() as conn:
        rows = qall(conn, """
            SELECT id, student_id, full_name, confidence, recognized, logged_at
            FROM recognition_logs WHERE teacher_id=%s ORDER BY logged_at DESC LIMIT 500
        """, (tid,))
    return jsonify({"rows": rows, "count": len(rows)})

@app.route("/api/teachers/<int:tid>/clear-recognition-logs", methods=["POST"])
@require_admin
def clear_teacher_recognition_logs(tid):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE recognition_logs SET teacher_id=NULL WHERE teacher_id=%s", (tid,))
            cleared = c.rowcount
    return jsonify({"cleared": cleared})

@app.route("/api/teachers/<int:tid>/reassign-recognition-logs", methods=["POST"])
@require_admin
def reassign_teacher_recognition_logs(tid):
    d = request.json or {}
    target_tid = d.get("to")
    if not target_tid:
        return jsonify({"error": "Target teacher ID required"}), 400
    with get_db() as conn:
        target = qone(conn, "SELECT id FROM teachers WHERE id=%s", (target_tid,))
        if not target:
            return jsonify({"error": "Target teacher not found"}), 404
        with conn.cursor() as c:
            c.execute("UPDATE recognition_logs SET teacher_id=%s WHERE teacher_id=%s", (target_tid, tid))
            reassigned = c.rowcount
    return jsonify({"reassigned": reassigned})

# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES — TEACHER PANEL
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/teacher/profile")
@require_auth(roles=["teacher"])
def teacher_profile():
    return jsonify(g.user)

@app.route("/api/teacher/students")
@require_auth(roles=["teacher"])
def teacher_students():
    teacher = g.user
    with get_db() as conn:
        students = qall(conn, """
            SELECT student_id, full_name, department, email, phone,
                   semester, status, sample_count, enrolled_at::text
            FROM students
            WHERE department = (SELECT name FROM faculties WHERE id=%s)
              AND semester = %s AND status = 'active'
            ORDER BY full_name
        """, (teacher["faculty_id"], str(teacher["semester"])))
    return jsonify({"students": students, "count": len(students)})

@app.route("/api/teacher/attendance")
@require_auth(roles=["teacher"])
def teacher_attendance():
    target     = request.args.get("date", date.today().isoformat())
    teacher    = g.user
    with get_db() as conn:
        records = qall(conn, """
            SELECT s.student_id, s.full_name,
                   a.date::text, a.time::text,
                   COALESCE(a.status, 'Absent') AS status, a.note
            FROM students s
            LEFT JOIN attendance a
                   ON  a.student_id = s.student_id
                   AND a.date = %s
                   AND a.subject_id = %s
            WHERE s.department = (SELECT name FROM faculties WHERE id=%s)
              AND s.semester = %s AND s.status = 'active'
            ORDER BY s.full_name
        """, (target, teacher["subject_id"], teacher["faculty_id"], str(teacher["semester"])))
    return jsonify({
        "date":    target,
        "records": records,
        "present": sum(1 for r in records if r["status"] == "Present"),
        "absent":  sum(1 for r in records if r["status"] == "Absent"),
        "subject_id":   teacher["subject_id"],
        "faculty_id":   teacher["faculty_id"],
        "semester":     teacher["semester"],
    })

@app.route("/api/teacher/attendance/mark", methods=["POST"])
@require_auth(roles=["teacher"])
def teacher_mark_attendance():
    d          = request.json or {}
    student_id = d.get("student_id")
    att_date   = d.get("date", date.today().isoformat())
    att_time   = d.get("time", datetime.now().strftime("%H:%M:%S"))
    status     = d.get("status", "Present")
    note       = d.get("note", "")
    teacher    = g.user

    if not student_id:
        return jsonify({"error": "student_id required"}), 400
    if status not in ("Present", "Absent"):
        return jsonify({"error": "status must be Present or Absent"}), 400

    with get_db() as conn:
        stu = qone(conn, """
            SELECT student_id FROM students
            WHERE student_id=%s
              AND department=(SELECT name FROM faculties WHERE id=%s)
              AND semester=%s
        """, (student_id, teacher["faculty_id"], str(teacher["semester"])))
        if not stu:
            return jsonify({"error": "Student not in your class"}), 403

        key = (student_id, att_date, teacher["subject_id"])
        ts = time.time()
        for k, v in list(_recent_marks.items()):
            if ts - v > _RECENT_MARK_TTL:
                _recent_marks.pop(k, None)
        recently = key in _recent_marks
        if not recently:
            qexec(conn, """
                INSERT INTO attendance (student_id, teacher_id, subject_id, date, time, status, note)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (student_id, date, subject_id) DO UPDATE
                    SET status=EXCLUDED.status, time=EXCLUDED.time, note=EXCLUDED.note, teacher_id=EXCLUDED.teacher_id
            """, (student_id, teacher["id"], teacher["subject_id"], att_date, att_time, status, note))
            _recent_marks[key] = ts

    return jsonify({"marked": True, "student_id": student_id, "status": status})

@app.route("/api/teacher/recognize", methods=["POST"])
@require_auth(roles=["teacher"])
def teacher_recognize():
    data    = request.json or {}
    img_b64 = data.get("image")
    if not img_b64: return jsonify({"error": "No image"}), 400

    teacher = g.user
    frame = decode_image(img_b64)
    if frame is None: return jsonify({"error": "Cannot decode image"}), 400

    fa    = get_face_app()
    rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    faces = fa.get(rgb)
    if not faces:
        return jsonify({"recognized": False, "message": "No face detected"})

    face = max(faces, key=lambda f:(f.bbox[2]-f.bbox[0])*(f.bbox[3]-f.bbox[1]))
    emb  = face.normed_embedding
    bbox = [int(x) for x in face.bbox]

    with get_db() as conn:
        sid, name, sim = find_best_match(conn, emb, teacher["faculty_id"], teacher["semester"])

    if sim >= THRESHOLD and sid:
        today = date.today().isoformat()
        now   = datetime.now().strftime("%H:%M:%S")
        key = (sid, today, teacher["subject_id"])
        ts = time.time()
        for k, v in list(_recent_marks.items()):
            if ts - v > _RECENT_MARK_TTL:
                _recent_marks.pop(k, None)
        recently = key in _recent_marks
        with get_db() as conn:
            with conn.cursor() as c:
                marked = False
                if not recently:
                    c.execute("""
                        INSERT INTO attendance (student_id, teacher_id, subject_id, date, time, status)
                        VALUES (%s,%s,%s,%s,%s,'Present')
                        ON CONFLICT (student_id, date, subject_id) DO NOTHING
                    """, (sid, teacher["id"], teacher["subject_id"], today, now))
                    marked = c.rowcount == 1
                    if marked:
                        _recent_marks[key] = ts
                c.execute("""
                    INSERT INTO recognition_logs
                        (student_id, full_name, confidence, recognized, teacher_id, subject_id)
                    VALUES (%s,%s,%s,true,%s,%s)
                """, (sid, name, round(sim*100,1), teacher["id"], teacher["subject_id"]))
        if marked:
            sse_broadcast({
                "type":"attendance","student_id":sid,"name":name,
                "confidence":round(sim*100,1),"time":now,"date":today,
                "subject": teacher.get("subject_name"),
                "teacher": teacher.get("full_name")
            })
        return jsonify({
            "recognized": True,
            "student_id": sid,
            "name": name,
            "confidence": round(sim*100,1),
            "bbox": bbox,
            "attendance_marked": marked,
        })

    _log_recognition(None,"Unknown",sim,False, teacher["id"], teacher["subject_id"])
    return jsonify({"recognized":False,"name":"Unknown","confidence":round(sim*100,1),"bbox":bbox})

@app.route("/api/teacher/attendance/logs")
@require_auth(roles=["teacher"])
def teacher_attendance_logs():
    teacher = g.user
    days    = int(request.args.get("days", 30))
    with get_db() as conn:
        rows = qall(conn, """
            SELECT a.student_id, s.full_name, a.date::text, a.time::text, a.status, a.note
            FROM attendance a
            JOIN students s ON s.student_id = a.student_id
            WHERE a.teacher_id=%s AND a.subject_id=%s
              AND a.date >= CURRENT_DATE - (%s * INTERVAL '1 day')
            ORDER BY a.date DESC, s.full_name
        """, (teacher["id"], teacher["subject_id"], days))
    return jsonify({"logs": rows})

@app.route("/api/teacher/change-password", methods=["POST"])
@require_auth(roles=["teacher"])
def teacher_change_password():
    d = request.json or {}
    old_pw = d.get("old_password","")
    new_pw = d.get("new_password","")
    if not new_pw or len(new_pw) < 6:
        return jsonify({"error": "New password must be at least 6 characters"}), 400
    with get_db() as conn:
        t = qone(conn, "SELECT id FROM teachers WHERE id=%s AND password_hash=%s",
                 (g.user_id, _hash(old_pw)))
        if not t:
            return jsonify({"error": "Current password is incorrect"}), 401
        qexec(conn, "UPDATE teachers SET password_hash=%s WHERE id=%s",
              (_hash(new_pw), g.user_id))
    return jsonify({"updated": True})

# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES — STUDENT PANEL
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/student/profile")
@require_auth(roles=["student"])
def student_profile():
    with get_db() as conn:
        s = qone(conn, """
            SELECT student_id, full_name, department, email, phone,
                   semester, status, enrolled_at::text
            FROM students WHERE student_id=%s
        """, (g.student_id,))
    if not s: return jsonify({"error": "Not found"}), 404
    return jsonify(s)

@app.route("/api/student/attendance")
@require_auth(roles=["student"])
def student_attendance():
    student_id = g.student_id
    with get_db() as conn:
        total_days = total_attendance_days(conn)
        stats = qone(conn, """
            SELECT COUNT(DISTINCT date) FILTER(WHERE status='Present') AS total_present,
                   %s::int AS total_days,
                   ROUND(100.0*COUNT(DISTINCT date) FILTER(WHERE status='Present')
                         /NULLIF(%s::int,0),1) AS percentage
            FROM attendance WHERE student_id=%s
        """, (total_days, total_days, student_id))

        records = qall(conn, """
            SELECT a.date::text, a.time::text, a.status, a.note,
                   sub.name AS subject_name, t.full_name AS teacher_name,
                   ts.label AS time_slot_label
            FROM attendance a
            LEFT JOIN subjects sub ON sub.id = a.subject_id
            LEFT JOIN teachers t ON t.id = a.teacher_id
            LEFT JOIN time_slots ts ON ts.id = t.time_slot_id
            WHERE a.student_id=%s
            ORDER BY a.date DESC, a.time DESC LIMIT 90
        """, (student_id,))

        monthly = qall(conn, """
            SELECT TO_CHAR(date,'Mon YYYY') AS month,
                   DATE_TRUNC('month',date) AS month_sort,
                   COUNT(*) FILTER(WHERE status='Present') AS present
            FROM attendance
            WHERE student_id=%s AND date >= NOW()-INTERVAL '6 months'
            GROUP BY month, month_sort ORDER BY month_sort
        """, (student_id,))

    return jsonify({
        "student_id": student_id,
        "stats":      stats,
        "records":    records,
        "monthly":    monthly,
    })

# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES — STUDENTS (admin)
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/students")
def list_students():
    q    = request.args.get("q","").strip()
    dept = request.args.get("department","").strip()
    sem  = request.args.get("semester","").strip()
    stat = request.args.get("status","").strip()
    sql  = """SELECT student_id, full_name, department, email, phone,
                     semester, status, sample_count, enrolled_at::text
              FROM students"""
    params, where = [], []
    if q:
        where.append("(full_name ILIKE %s OR student_id ILIKE %s)")
        params += [f"%{q}%", f"%{q}%"]
    if dept: where.append("department=%s"); params.append(dept)
    if sem:  where.append("semester=%s");   params.append(sem)
    if stat: where.append("status=%s");     params.append(stat)
    if where: sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY full_name"
    with get_db() as conn:
        students = qall(conn, sql, params)
    return jsonify({"students":students,"count":len(students)})

@app.route("/api/students/<sid>")
def get_student(sid):
    with get_db() as conn:
        s = qone(conn, """
            SELECT student_id, full_name, department, email, phone,
                   semester, status, sample_count, enrolled_at::text
            FROM students WHERE student_id=%s
        """, (sid,))
        if not s: return jsonify({"error":"Not found"}), 404

        total_days = total_attendance_days(conn)
        stats = qone(conn, """
            SELECT COUNT(DISTINCT date) FILTER(WHERE status='Present') AS total_present,
                   %s::int AS total_days,
                   ROUND(100.0*COUNT(DISTINCT date) FILTER(WHERE status='Present')
                         /NULLIF(%s::int,0),1) AS percentage
            FROM attendance WHERE student_id=%s
        """, (total_days, total_days, sid))

        monthly = qall(conn, """
            SELECT TO_CHAR(date,'Mon YYYY') AS month,
                   DATE_TRUNC('month',date) AS month_sort,
                   COUNT(*) FILTER(WHERE status='Present') AS present
            FROM attendance
            WHERE student_id=%s AND date >= NOW()-INTERVAL '6 months'
            GROUP BY month, month_sort ORDER BY month_sort
        """, (sid,))

        logs = qall(conn, """
            SELECT logged_at::text, confidence, recognized
            FROM recognition_logs WHERE student_id=%s ORDER BY logged_at DESC LIMIT 20
        """, (sid,))

        att_records = qall(conn, """
            SELECT a.date::text, a.time::text, a.status, a.note,
                   sub.name AS subject_name, t.full_name AS teacher_name
            FROM attendance a
            LEFT JOIN subjects sub ON sub.id = a.subject_id
            LEFT JOIN teachers t ON t.id = a.teacher_id
            WHERE a.student_id=%s ORDER BY a.date DESC LIMIT 60
        """, (sid,))

    return jsonify({
        **s,
        "stats":       stats,
        "monthly":     monthly,
        "logs":        logs,
        "attendance":  att_records,
    })

@app.route("/api/students/<sid>/photo")
def student_photo(sid):
    with get_db() as conn:
        row = qone(conn,"SELECT face_image FROM students WHERE student_id=%s",(sid,))
    if not row or not row["face_image"]: return "",404
    return Response(bytes(row["face_image"]),mimetype="image/jpeg")

@app.route("/api/students/<sid>", methods=["PUT"])
@require_admin
def update_student(sid):
    d = request.json or {}
    ALLOWED = {"full_name","department","email","phone","semester","status"}
    fields  = {k:d[k] for k in ALLOWED if k in d}
    if not fields: return jsonify({"error":"Nothing to update"}), 400
    if "email" in fields and fields["email"]:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", fields["email"]):
            return jsonify({"error":"Invalid email format"}), 400
    if "status" in fields and fields["status"] not in ("active","inactive","graduated","suspended"):
        return jsonify({"error":"Invalid status value"}), 400
    sql = "UPDATE students SET " + ", ".join(f"{k}=%s" for k in fields) + " WHERE student_id=%s"
    with get_db() as conn:
        rows = qexec(conn, sql, list(fields.values()) + [sid])
    if rows == 0: return jsonify({"error":"Student not found"}), 404
    _log_activity(g.user["username"], "update_student", "student", sid,
        f"Updated fields: {', '.join(fields.keys())}")
    return jsonify({"updated":True, "fields": list(fields.keys())})

@app.route("/api/students/<sid>", methods=["DELETE"])
@require_admin
def delete_student(sid):
    with get_db() as conn:
        rows = qexec(conn,"DELETE FROM students WHERE student_id=%s",(sid,))
    _log_activity(g.user["username"],"delete_student","student",sid)
    return jsonify({"deleted": rows > 0})

@app.route("/api/departments")
def departments():
    with get_db() as conn:
        rows = qall(conn,
            "SELECT DISTINCT department FROM students WHERE department IS NOT NULL ORDER BY department")
    return jsonify({"departments":[r["department"] for r in rows]})

# ── Attendance admin edit ─────────────────────────────────────────────────
@app.route("/api/attendance/<sid>/<att_date>", methods=["PUT"])
@require_admin
def update_attendance(sid, att_date):
    d      = request.json or {}
    status = d.get("status","")
    note   = d.get("note","")
    if status not in ("Present","Absent"):
        return jsonify({"error":"status must be Present or Absent"}), 400
    with get_db() as conn:
        qexec(conn, """
            INSERT INTO attendance (student_id, date, time, status, note)
            VALUES (%s, %s, CURRENT_TIME, %s, %s)
            ON CONFLICT (student_id, date, subject_id) DO UPDATE
                SET status=%s, note=%s
        """, (sid, att_date, status, note, status, note))
    _log_activity(g.user["username"], "edit_attendance", "attendance", sid,
                  f"Set {att_date} to {status}" + (f" — {note}" if note else ""))
    return jsonify({"updated":True})

@app.route("/api/attendance/<sid>/<att_date>", methods=["DELETE"])
@require_admin
def delete_attendance(sid, att_date):
    with get_db() as conn:
        rows = qexec(conn,
            "DELETE FROM attendance WHERE student_id=%s AND date=%s",
            (sid, att_date))
    _log_activity(g.user["username"],"delete_attendance","attendance",sid,
                  f"Deleted {att_date}")
    return jsonify({"deleted": rows > 0})

# ── Activity log ──────────────────────────────────────────────────────────
@app.route("/api/activity-logs")
@require_admin
def get_activity_logs():
    limit  = int(request.args.get("limit", 50))
    target = request.args.get("target_id","").strip()
    with get_db() as conn:
        if target:
            rows = qall(conn, """
                SELECT admin_user,action,target_type,target_id,detail,logged_at::text
                FROM activity_logs WHERE target_id=%s ORDER BY logged_at DESC LIMIT %s
            """, (target, limit))
        else:
            rows = qall(conn, """
                SELECT admin_user,action,target_type,target_id,detail,logged_at::text
                FROM activity_logs ORDER BY logged_at DESC LIMIT %s
            """, (limit,))
    return jsonify({"logs": rows})

# ── Frame quality validation ──────────────────────────────────────────────
@app.route("/api/capture/validate-frame", methods=["POST"])
def validate_frame():
    d       = request.json or {}
    img_b64 = d.get("image")
    if not img_b64: return jsonify({"error":"No image"}), 400

    frame = decode_image(img_b64)
    if frame is None: return jsonify({"error":"Cannot decode"}), 400

    fa    = get_face_app()
    rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    faces = fa.get(rgb)

    if not faces:
        return jsonify({"face_detected":False, "quality":None})

    face = max(faces, key=lambda f:(f.bbox[2]-f.bbox[0])*(f.bbox[3]-f.bbox[1]))
    bbox = [int(x) for x in face.bbox]
    quality = score_frame_quality(frame, bbox)

    pose_hint = "front"
    try:
        kps = face.kps
        if kps is not None:
            le, re = kps[0], kps[1]
            nose   = kps[2]
            eye_center_x = (le[0] + re[0]) / 2
            face_w = bbox[2] - bbox[0]
            nose_offset = (nose[0] - eye_center_x) / (face_w + 1e-5)
            eye_y   = (le[1] + re[1]) / 2
            face_h  = bbox[3] - bbox[1]
            nose_dy = (nose[1] - eye_y) / (face_h + 1e-5)
            mouth_pts = kps[3], kps[4]
            mouth_y   = (mouth_pts[0][1] + mouth_pts[1][1]) / 2
            mouth_dy  = (mouth_y - eye_y) / (face_h + 1e-5)
            if nose_offset > 0.12:   pose_hint = "left"
            elif nose_offset < -0.12: pose_hint = "right"
            elif nose_dy < 0.18 and mouth_dy < 0.38: pose_hint = "up"
            elif nose_dy > 0.25: pose_hint = "down"
            else: pose_hint = "front"
    except: pass

    return jsonify({
        "face_detected": True,
        "bbox":          bbox,
        "quality":       quality,
        "pose":          pose_hint,
    })

# ── Enroll ────────────────────────────────────────────────────────────────
@app.route("/api/enroll", methods=["POST"])
def enroll():
    if request.content_type and "application/json" in request.content_type:
        data       = request.json or {}
        student_id = data.get("student_id","").strip()
        full_name  = data.get("full_name","").strip()
        department = data.get("department") or None
        email      = data.get("email")      or None
        phone      = data.get("phone")      or None
        semester   = data.get("semester")   or None
        frames_b64 = data.get("frames",[])
    else:
        student_id = request.form.get("student_id","").strip()
        full_name  = request.form.get("full_name","").strip()
        department = request.form.get("department") or None
        email      = request.form.get("email")      or None
        phone      = request.form.get("phone")      or None
        semester   = request.form.get("semester")   or None
        frames_b64 = [base64.b64encode(f.read()).decode()
                      for f in request.files.getlist("images")]

    if not student_id or not full_name:
        return jsonify({"error":"student_id and full_name required"}), 400
    if not frames_b64:
        return jsonify({"error":"No images provided"}), 400

    existing_sid = None
    test_emb, _ = extract_embeddings(frames_b64[:3])
    if test_emb is not None:
        with get_db() as conn:
            existing_sid, existing_name, sim = find_best_match(conn, test_emb)
        if sim > 0.92 and existing_sid and existing_sid != student_id:
            return jsonify({
                "error": f"This face already belongs to {existing_name} ({existing_sid}). "
                         f"Similarity: {sim*100:.1f}%"
            }), 409

    mean_emb, thumbnail = extract_embeddings(frames_b64)
    if mean_emb is None:
        return jsonify({"error":"No faces detected in any image"}), 422

    if PGVECTOR_AVAILABLE:
        emb_value = mean_emb.tolist()
        emb_sql   = "%s::vector"
    else:
        emb_value = json.dumps(mean_emb.tolist())
        emb_sql   = "%s"

    with get_db() as conn:
        qexec(conn, f"""
            INSERT INTO students
                (student_id, full_name, department, email, phone, semester,
                 embedding, face_image, sample_count)
            VALUES (%s,%s,%s,%s,%s,%s, {emb_sql}, %s, %s)
            ON CONFLICT (student_id) DO UPDATE SET
                full_name    = EXCLUDED.full_name,
                department   = EXCLUDED.department,
                email        = EXCLUDED.email,
                phone        = EXCLUDED.phone,
                semester     = EXCLUDED.semester,
                embedding    = EXCLUDED.embedding,
                face_image   = EXCLUDED.face_image,
                sample_count = EXCLUDED.sample_count
        """, (student_id, full_name, department, email, phone, semester,
              emb_value, thumbnail, len(frames_b64)))

    return jsonify({
        "enrolled":   True,
        "student_id": student_id,
        "samples":    len(frames_b64),
        "is_update":  bool(existing_sid == student_id if test_emb is not None else False)
    })

# ── Camera ────────────────────────────────────────────────────────────────
@app.route("/api/camera/start", methods=["POST"])
def start_camera():
    if camera_state["active"]: return jsonify({"status":"already_running"})
    d = request.get_json(silent=True) or {}
    camera_state["teacher_id"] = d.get("teacher_id")
    camera_state["subject_id"] = d.get("subject_id")
    camera_state["active"] = True
    return jsonify({"status":"started"})

@app.route("/api/camera/stop", methods=["POST"])
def stop_camera():
    camera_state["active"] = False
    if camera_state["cap"]: camera_state["cap"].release()
    camera_state["teacher_id"] = None
    camera_state["subject_id"] = None
    return jsonify({"status":"stopped"})

@app.route("/api/stream")
def stream():
    if not camera_state["active"]: return jsonify({"error":"Camera not started"}), 400
    return Response(_gen_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")

# ── SSE ───────────────────────────────────────────────────────────────────
@app.route("/api/events")
def sse_events():
    q = queue.Queue(maxsize=50)
    with _sse_lock: _sse_clients.append(q)
    def generate():
        try:
            while True:
                try:    yield q.get(timeout=20)
                except queue.Empty: yield ": heartbeat\n\n"
        finally:
            with _sse_lock:
                if q in _sse_clients: _sse_clients.remove(q)
    return Response(generate(),
                    mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

# ── Attendance (admin) ────────────────────────────────────────────────────
@app.route("/api/attendance")
def get_attendance():
    target = request.args.get("date", date.today().isoformat())
    dept   = request.args.get("department","").strip()
    sql = """
        SELECT s.student_id, s.full_name, s.department,
               a.date::text, a.time::text, a.status, a.note
        FROM   students s
        LEFT   JOIN attendance a ON a.student_id = s.student_id AND a.date = %s
    """
    params = [target]
    if dept: sql += " WHERE s.department=%s"; params.append(dept)
    sql += " ORDER BY s.full_name"
    with get_db() as conn:
        rows = qall(conn, sql, params)
    records = [{"student_id":r["student_id"],"name":r["full_name"],
                "department":r["department"],"date":r["date"] or target,
                "time":r["time"] or "—","status":r["status"] or "Absent",
                "note":r["note"] or ""} for r in rows]
    return jsonify({"date":target,"records":records,
                    "present":sum(1 for r in records if r["status"]=="Present"),
                    "absent": sum(1 for r in records if r["status"]=="Absent")})

@app.route("/api/attendance/faculty-summary")
def faculty_summary():
    target = request.args.get("date", date.today().isoformat())
    with get_db() as conn:
        rows = qall(conn, """
            SELECT s.student_id, s.full_name,
                   COALESCE(s.department,'Unassigned') AS department,
                   a.time::text AS time,
                   COALESCE(a.status,'Absent') AS status
            FROM students s
            LEFT JOIN LATERAL (
                SELECT status, time FROM attendance a2
                WHERE a2.student_id = s.student_id AND a2.date = %s
                ORDER BY a2.time DESC LIMIT 1
            ) a ON true
            ORDER BY s.department, s.full_name
        """, (target,))

    faculty_map = {}
    for r in rows:
        dept = r["department"]
        faculty_map.setdefault(dept, []).append({
            "student_id": r["student_id"],
            "name": r["full_name"],
            "time": r["time"] or "—",
            "status": r["status"],
        })

    faculties = []
    for dept_name in sorted(faculty_map):
        students = faculty_map[dept_name]
        present = sum(1 for s in students if s["status"] == "Present")
        total = len(students)
        faculties.append({
            "name": dept_name,
            "total": total,
            "present": present,
            "absent": total - present,
            "rate": round(present / total * 100, 1) if total else 0,
            "students": students,
        })

    ot = sum(f["total"] for f in faculties)
    op = sum(f["present"] for f in faculties)
    return jsonify({"date":target,"faculties":faculties,
                    "overall":{"total":ot,"present":op,"absent":ot-op,
                               "rate":round(op/ot*100,1) if ot else 0}})

@app.route("/api/attendance/history")
def attendance_history():
    dept = request.args.get("department","").strip()
    with get_db() as conn:
        if dept:
            rows  = qall(conn, """
                SELECT a.date::text,
                       COUNT(*) FILTER(WHERE a.status='Present') AS present
                FROM   attendance a JOIN students s ON s.student_id=a.student_id
                WHERE  s.department=%s GROUP BY a.date ORDER BY a.date DESC LIMIT 30
            """, (dept,))
            total = qone(conn,"SELECT COUNT(*) AS n FROM students WHERE department=%s",(dept,))["n"]
        else:
            rows  = qall(conn,"""
                SELECT date::text, COUNT(*) FILTER(WHERE status='Present') AS present
                FROM attendance GROUP BY date ORDER BY date DESC LIMIT 30
            """)
            total = qone(conn,"SELECT COUNT(*) AS n FROM students")["n"]
    return jsonify({"history":[{"date":r["date"],"present":r["present"],
                                "absent":max(0,total-r["present"])} for r in rows]})

@app.route("/api/attendance/stats")
def attendance_stats():
    dept = request.args.get("department","").strip()
    with get_db() as conn:
        total_days = total_attendance_days(conn, dept or None)
        sql = """
            SELECT s.student_id, s.full_name, s.department,
                   COUNT(DISTINCT a.date) FILTER(WHERE a.status='Present') AS present_days,
                   %s::int AS total_days,
                   ROUND(100.0*COUNT(DISTINCT a.date) FILTER(WHERE a.status='Present')
                         /NULLIF(%s::int,0),1) AS pct
            FROM students s LEFT JOIN attendance a ON a.student_id=s.student_id
        """
        params = [total_days, total_days]
        if dept: sql += " WHERE s.department=%s"; params.append(dept)
        sql += " GROUP BY s.student_id,s.full_name,s.department ORDER BY pct DESC NULLS LAST"
        rows = qall(conn, sql, params)
    return jsonify({"stats": rows})

@app.route("/api/attendance/export")
def export_csv():
    from_d = request.args.get("from", date.today().isoformat())
    to_d   = request.args.get("to",   date.today().isoformat())
    dept   = request.args.get("department","").strip()
    with get_db() as conn:
        sql = """
            SELECT s.student_id,s.full_name,s.department,
                   a.date::text,a.time::text,a.status,a.note
            FROM students s
            LEFT JOIN attendance a ON a.student_id=s.student_id
                AND a.date BETWEEN %s AND %s
        """
        params = [from_d, to_d]
        if dept: sql += " WHERE s.department=%s"; params.append(dept)
        sql += " ORDER BY s.department, a.date, s.full_name"
        rows = qall(conn, sql, params)
    if dept:
        total  = len(set(r["student_id"] for r in rows))
        present= sum(1 for r in rows if r["status"]=="Present")
        lines  = [f"Faculty: {dept}",f"Date: {from_d} to {to_d}",
                  f"Enrolled: {total}",f"Present: {present}",
                  f"Absent: {total-present}",
                  f"Rate: {round(present/total*100,1) if total else 0}%","",
                  "Student ID,Name,Date,Time,Status,Note"]
        lines += [f'{r["student_id"]},{r["full_name"]},{r["date"] or ""},'
                  f'{r["time"] or "—"},{r["status"] or "Absent"},{r["note"] or ""}' for r in rows]
        fname = f"attendance_{dept}_{from_d}_{to_d}.csv"
    else:
        lines  = ["Student ID,Name,Department,Date,Time,Status,Note"]
        lines += [f'{r["student_id"]},{r["full_name"]},{r["department"] or ""},'
                  f'{r["date"] or ""},{r["time"] or "—"},{r["status"] or "Absent"},{r["note"] or ""}' for r in rows]
        fname = f"attendance_all_{from_d}_{to_d}.csv"
    return Response("\n".join(lines), mimetype="text/csv",
                    headers={"Content-Disposition":f"attachment; filename={fname}"})

# ── Logs ──────────────────────────────────────────────────────────────────
@app.route("/api/logs")
def get_logs():
    limit = int(request.args.get("limit", 50))
    with get_db() as conn:
        rows = qall(conn, """
            SELECT student_id,full_name,confidence,recognized,logged_at::text
            FROM recognition_logs ORDER BY logged_at DESC LIMIT %s
        """, (limit,))
    return jsonify({"logs": rows})

# ── Email ─────────────────────────────────────────────────────────────────
@app.route("/api/email/test", methods=["POST"])
@require_admin
def test_email():
    d  = request.json or {}
    to = d.get("email","").strip()
    if not to:
        return jsonify({"error": "email required"}), 400
    if not (os.getenv("BREVO_API_KEY") and os.getenv("BREVO_FROM")):
        return jsonify({"error": "Email not configured. Set BREVO_API_KEY and BREVO_FROM in .env"}), 503
    queue_attendance_email(
        "TEST", "Test Student", "Test Department",
        date.today().isoformat(), datetime.now().strftime("%H:%M:%S"), to
    )
    return jsonify({"queued": True})

@app.route("/api/email/logs")
@require_admin
def email_logs():
    with get_db() as conn:
        rows = qall(conn, """
            SELECT student_id, email_to, subject,
                   sent_at::text, success, error_msg
            FROM email_log ORDER BY sent_at DESC LIMIT 50
        """)
    return jsonify({"logs": rows})

# ── Settings ──────────────────────────────────────────────────────────────
@app.route("/api/settings")
def get_settings():
    return jsonify({
        "recognition_threshold": THRESHOLD,
        "frame_skip":            SKIP,
        "email_enabled":         bool(os.getenv("BREVO_API_KEY") and os.getenv("BREVO_FROM")),
        "email_service":         "Brevo",
        "brevo_from":            os.getenv("BREVO_FROM", ""),
    })

@app.route("/api/settings", methods=["PUT"])
@require_admin
def update_settings():
    global THRESHOLD, SKIP
    d = request.json or {}
    if "recognition_threshold" in d: THRESHOLD = float(d["recognition_threshold"])
    if "frame_skip" in d:            SKIP      = int(d["frame_skip"])
    _log_activity(g.user["username"],"update_settings","system",detail=str(d))
    return jsonify({"recognition_threshold":THRESHOLD,"frame_skip":SKIP})

# ── Boot ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("=== वेदनेत्रम् Smart Attendance v3.1 starting ===")
    init_db()
    try:
        with get_db() as conn:
            qone(conn, "SELECT 1")
        log.info("DB connection pool warmed up.")
    except Exception as exc:
        log.error("DB pool warm-up failed: %s", exc)
    _email_thread = threading.Thread(target=_email_worker, daemon=True)
    _email_thread.start()
    log.info("Email worker started. Listening on 0.0.0.0:5050 …")
    app.run(debug=True, host="0.0.0.0", port=5050, threaded=True)
