"""
app.py — वेदनेत्रम् Smart Attendance v3.2
YOLOv8 Face Detection Edition

CHANGES FROM v3.1:
──────────────────
DETECTION ENGINE:   InsightFace FaceAnalysis (buffalo_l) → YOLOv8
EMBEDDING ENGINE:   InsightFace ArcFace (buffalo_l) → PRESERVED (unchanged)
CAMERA:             OpenCV VideoCapture → PRESERVED (unchanged)
DATABASE:           PostgreSQL + pgvector → UNCHANGED
ATTENDANCE LOGIC:   UNCHANGED
AUTH / RBAC:        UNCHANGED
ALL API ENDPOINTS:  UNCHANGED (same URLs, same request/response shapes)

Detection pipeline change:
  BEFORE: fa = FaceAnalysis(); faces = fa.get(rgb_frame)
          face.bbox, face.normed_embedding, face.kps all from InsightFace

  AFTER:  detector = YOLOFaceDetector.instance()   (face_detector.py)
          yolo_faces = detector.detect(bgr_frame)   → bboxes + crops
          arcface    = get_arcface_model()           → embedding only
          emb = arcface.get_embedding(crop)          → 512-d vector

  The ArcFace recognizer is loaded separately via a lightweight wrapper
  (get_arcface_model) that loads only the recognition part of buffalo_l,
  NOT the full FaceAnalysis pipeline that bundles detection+recognition.
"""

import os, cv2, json, base64, hashlib, threading, queue, time, re, secrets, logging
import urllib.request, urllib.error
import numpy as np
from contextlib import contextmanager
from datetime import datetime, date
from flask import Flask, request, jsonify, Response, g
from flask_cors import CORS
from dotenv import load_dotenv
import psycopg2, psycopg2.extras, psycopg2.pool

# ── NEW: YOLOv8 detector import ───────────────────────────────────────────────
from face_detector import (
    get_yolo_detector,
    detect_faces_in_frame,
    detect_largest_face,
    score_frame_quality_yolo,
    FaceDetectionResult,
)

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

# ── Config ────────────────────────────────────────────────────────────────────
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

# ── DB connection pool ────────────────────────────────────────────────────────
_pool = None

def _get_pool():
    global _pool
    if _pool is None or _pool.closed:
        log.info("Creating DB connection pool …")
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2, maxconn=20, dsn=PG_DSN, connect_timeout=10,
        )
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
        c.execute(sql, p)
        row = c.fetchone()
        return dict(row) if row else None

def qall(conn, sql, p=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(sql, p)
        return [dict(r) for r in c.fetchall()]

def qexec(conn, sql, p=()):
    with conn.cursor() as c:
        c.execute(sql, p)
        return c.rowcount

def total_attendance_days(conn, dept=None, subject_id=None, teacher_id=None):
    if teacher_id:
        row = qone(conn, "SELECT COUNT(DISTINCT a.date) AS d FROM attendance a WHERE a.teacher_id=%s", (teacher_id,))
    elif subject_id:
        row = qone(conn, "SELECT COUNT(DISTINCT a.date) AS d FROM attendance a WHERE a.subject_id=%s", (subject_id,))
    elif dept:
        row = qone(conn, """
            SELECT COUNT(DISTINCT a.date) AS d FROM attendance a
            JOIN students s ON s.student_id=a.student_id WHERE s.department=%s
        """, (dept,))
    else:
        row = qone(conn, "SELECT COUNT(DISTINCT date) AS d FROM attendance")
    return int(row["d"] or 0) if row else 0

def _has_pgvector(conn):
    row = qone(conn, "SELECT 1 FROM pg_type WHERE typname='vector'")
    return bool(row)

def _ddl(conn, sql, label=""):
    try:
        with conn.cursor() as c:
            c.execute(sql)
        if label:
            log.debug("DDL ok: %s", label)
    except Exception as exc:
        log.warning("DDL warn [%s]: %s", label, exc)

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
        except Exception as exc:
            log.warning("pgvector extension error: %s", exc)

        PGVECTOR_AVAILABLE = _has_pgvector(conn)
        embedding_col = "vector(512)" if PGVECTOR_AVAILABLE else "TEXT"

        _ddl(conn, """CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY, username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'admin')""", "users")

        _ddl(conn, f"""CREATE TABLE IF NOT EXISTS students (
            id SERIAL PRIMARY KEY, student_id TEXT NOT NULL UNIQUE,
            full_name TEXT NOT NULL, department TEXT, email TEXT UNIQUE,
            phone TEXT, semester TEXT, status TEXT NOT NULL DEFAULT 'active',
            face_image BYTEA, embedding {embedding_col},
            sample_count INTEGER NOT NULL DEFAULT 0,
            enrolled_at TIMESTAMPTZ DEFAULT NOW())""", "students")

        for stmt in [
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS semester TEXT",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS face_image BYTEA",
            f"ALTER TABLE students ADD COLUMN IF NOT EXISTS embedding {embedding_col}",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS sample_count INTEGER NOT NULL DEFAULT 0",
        ]:
            _ddl(conn, stmt)

        _ddl(conn, """CREATE TABLE IF NOT EXISTS faculties (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            code TEXT, created_at TIMESTAMPTZ DEFAULT NOW())""", "faculties")

        _ddl(conn, """CREATE TABLE IF NOT EXISTS subjects (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, code TEXT,
            faculty_id INTEGER REFERENCES faculties(id) ON DELETE CASCADE,
            semester INTEGER NOT NULL CHECK (semester BETWEEN 1 AND 8),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(name, faculty_id, semester))""", "subjects")

        _ddl(conn, """CREATE TABLE IF NOT EXISTS time_slots (
            id SERIAL PRIMARY KEY, label TEXT NOT NULL,
            start_time TIME NOT NULL, end_time TIME NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW())""", "time_slots")

        _ddl(conn, """CREATE TABLE IF NOT EXISTS teachers (
            id SERIAL PRIMARY KEY, teacher_id TEXT NOT NULL UNIQUE,
            full_name TEXT NOT NULL, email TEXT UNIQUE, phone TEXT,
            password_hash TEXT NOT NULL,
            faculty_id INTEGER REFERENCES faculties(id),
            semester INTEGER CHECK (semester BETWEEN 1 AND 8),
            subject_id INTEGER REFERENCES subjects(id),
            time_slot_id INTEGER REFERENCES time_slots(id),
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMPTZ DEFAULT NOW())""", "teachers")

        _ddl(conn, """CREATE TABLE IF NOT EXISTS teacher_assignments (
            id SERIAL PRIMARY KEY,
            teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
            faculty_id INTEGER NOT NULL REFERENCES faculties(id) ON DELETE CASCADE,
            semester INTEGER NOT NULL CHECK (semester BETWEEN 1 AND 8),
            subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
            time_slot_id INTEGER REFERENCES time_slots(id),
            is_primary BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(teacher_id, faculty_id, semester, subject_id))""", "teacher_assignments")

        _ddl(conn, """CREATE TABLE IF NOT EXISTS class_schedules (
            id SERIAL PRIMARY KEY,
            faculty_id INTEGER NOT NULL REFERENCES faculties(id) ON DELETE CASCADE,
            semester INTEGER NOT NULL CHECK (semester BETWEEN 1 AND 8),
            time_slot_id INTEGER NOT NULL REFERENCES time_slots(id) ON DELETE CASCADE,
            teacher_id INTEGER REFERENCES teachers(id) ON DELETE SET NULL,
            subject_id INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE(faculty_id, semester, time_slot_id))""", "class_schedules")

        _ddl(conn, """CREATE TABLE IF NOT EXISTS attendance (
            id SERIAL PRIMARY KEY,
            student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
            teacher_id INTEGER REFERENCES teachers(id),
            subject_id INTEGER REFERENCES subjects(id),
            date DATE NOT NULL DEFAULT CURRENT_DATE,
            time TIME NOT NULL DEFAULT CURRENT_TIME,
            status TEXT NOT NULL DEFAULT 'Present', note TEXT,
            UNIQUE(student_id, date, subject_id))""", "attendance")

        for stmt in [
            "ALTER TABLE attendance ADD COLUMN IF NOT EXISTS teacher_id INTEGER REFERENCES teachers(id)",
            "ALTER TABLE attendance ADD COLUMN IF NOT EXISTS subject_id INTEGER REFERENCES subjects(id)",
            "ALTER TABLE attendance ADD COLUMN IF NOT EXISTS note TEXT",
        ]:
            _ddl(conn, stmt)

        _ddl(conn, """CREATE TABLE IF NOT EXISTS recognition_logs (
            id SERIAL PRIMARY KEY, student_id TEXT, full_name TEXT,
            confidence REAL, recognized BOOLEAN NOT NULL,
            teacher_id INTEGER REFERENCES teachers(id),
            subject_id INTEGER REFERENCES subjects(id),
            detection_engine TEXT DEFAULT 'yolov8',
            logged_at TIMESTAMPTZ DEFAULT NOW())""", "recognition_logs")

        # Add detection_engine column to existing tables (migration-safe)
        _ddl(conn, "ALTER TABLE recognition_logs ADD COLUMN IF NOT EXISTS detection_engine TEXT DEFAULT 'yolov8'")

        _ddl(conn, """CREATE TABLE IF NOT EXISTS activity_logs (
            id SERIAL PRIMARY KEY, admin_user TEXT NOT NULL,
            action TEXT NOT NULL, target_type TEXT NOT NULL,
            target_id TEXT, detail TEXT,
            logged_at TIMESTAMPTZ DEFAULT NOW())""", "activity_logs")

        _ddl(conn, """CREATE TABLE IF NOT EXISTS email_log (
            id SERIAL PRIMARY KEY, student_id TEXT NOT NULL,
            email_to TEXT NOT NULL, subject TEXT,
            sent_at TIMESTAMPTZ DEFAULT NOW(),
            success BOOLEAN NOT NULL DEFAULT true, error_msg TEXT)""", "email_log")

        _ddl(conn, """CREATE TABLE IF NOT EXISTS sessions (
            id SERIAL PRIMARY KEY, token TEXT NOT NULL UNIQUE,
            user_type TEXT NOT NULL, user_id INTEGER, student_id TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            expires_at TIMESTAMPTZ DEFAULT NOW() + INTERVAL '24 hours')""", "sessions")

        _ddl(conn, "CREATE INDEX IF NOT EXISTS sessions_token_idx ON sessions (token)")

        # Seed data
        _ddl(conn, """INSERT INTO users (username, password_hash, role)
            VALUES ('admin','240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9','admin')
            ON CONFLICT DO NOTHING""", "seed admin")

        _ddl(conn, """INSERT INTO time_slots (label, start_time, end_time) VALUES
            ('Morning   (06:00-08:00)','06:00','08:00'),
            ('Period 1  (08:00-09:00)','08:00','09:00'),
            ('Period 2  (09:00-10:00)','09:00','10:00'),
            ('Period 3  (10:00-11:00)','10:00','11:00'),
            ('Period 4  (11:00-12:00)','11:00','12:00'),
            ('Lunch     (12:00-13:00)','12:00','13:00'),
            ('Period 5  (13:00-14:00)','13:00','14:00'),
            ('Period 6  (14:00-15:00)','14:00','15:00'),
            ('Period 7  (15:00-16:00)','15:00','16:00'),
            ('Evening   (16:00-18:00)','16:00','18:00')
            ON CONFLICT DO NOTHING""", "seed time_slots")

        if PGVECTOR_AVAILABLE:
            _ddl(conn, """CREATE INDEX IF NOT EXISTS students_embedding_hnsw
                ON students USING hnsw (embedding vector_cosine_ops)
                WITH (m=16, ef_construction=64)""", "hnsw index")

        log.info("Schema init complete. pgvector=%s", PGVECTOR_AVAILABLE)
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════════════
#  FACE MODEL — YOLOv8 DETECTION + ArcFace EMBEDDING
# ═════════════════════════════════════════════════════════════════════════════

# ── ArcFace embedding model (InsightFace recognition ONLY, no detection) ─────
_arcface_model = None
_arcface_lock  = threading.Lock()

def get_arcface_model():
    """
    Load ONLY the ArcFace recognition model from InsightFace buffalo_l.

    This is the key architectural change:
      BEFORE: FaceAnalysis (detection + embedding bundled)
      AFTER:  YOLOv8 for detection + ArcFace alone for embedding

    We use insightface's model_zoo directly to load w600k_r50 (the ArcFace
    backbone inside buffalo_l) without the detection head.
    Falls back to loading the full FaceAnalysis if the direct load fails —
    in that case we just won't use its detector.
    """
    global _arcface_model
    if _arcface_model is not None:
        return _arcface_model
    with _arcface_lock:
        if _arcface_model is not None:
            return _arcface_model
        log.info("Loading ArcFace recognition model …")
        t0 = time.time()
        try:
            import insightface
            # Load recognition model directly from the model zoo
            # This skips the RetinaFace/SCRFD detector that buffalo_l bundles
            model = insightface.model_zoo.get_model(
                "w600k_r50",
                download=True,
                download_zip=True,
            )
            model.prepare(ctx_id=0)   # ctx_id=0 → GPU if available, else CPU
            _arcface_model = model
            log.info("ArcFace loaded in %.2fs (recognition-only, no detector)", time.time()-t0)
        except Exception as exc:
            log.warning("Direct ArcFace load failed (%s) — falling back to FaceAnalysis", exc)
            try:
                from insightface.app import FaceAnalysis
                fa = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
                fa.prepare(ctx_id=0, det_size=(640, 640))
                _arcface_model = fa
                log.info("FaceAnalysis fallback loaded in %.2fs", time.time()-t0)
            except Exception as exc2:
                log.error("ArcFace fallback also failed: %s", exc2)
                _arcface_model = None
    return _arcface_model


def _get_embedding_from_crop(crop_bgr: np.ndarray) -> np.ndarray | None:
    """
    Extract a 512-d ArcFace embedding from a face crop (BGR, any size).

    The crop is the output of YOLOv8 detection + margin padding.
    ArcFace internally resizes to 112×112.

    Returns:
        Normalized 512-d float32 vector, or None on failure.
    """
    model = get_arcface_model()
    if model is None:
        return None

    try:
        # Resize to ArcFace input size
        face_112 = cv2.resize(crop_bgr, (112, 112))
        rgb       = cv2.cvtColor(face_112, cv2.COLOR_BGR2RGB)

        # Direct ArcFace model (insightface.model_zoo)
        if hasattr(model, "get_feat"):
            emb = model.get_feat(rgb[np.newaxis])   # shape (1, 512)
            emb = emb[0].astype(np.float32)
            norm = np.linalg.norm(emb)
            return emb / norm if norm > 0 else None

        # FaceAnalysis fallback — run full pipeline on the crop
        # (detection will find the face again in the crop; small overhead)
        if hasattr(model, "get"):
            faces = model.get(rgb)
            if faces:
                face = max(faces, key=lambda f:(f.bbox[2]-f.bbox[0])*(f.bbox[3]-f.bbox[1]))
                return face.normed_embedding.astype(np.float32)
            return None

    except Exception as exc:
        log.warning("ArcFace embedding error: %s", exc)
        return None

    return None


def decode_image(b64_or_bytes) -> np.ndarray | None:
    """Decode a base64 string or raw bytes to a BGR numpy array."""
    if isinstance(b64_or_bytes, str):
        b64_or_bytes = base64.b64decode(b64_or_bytes)
    arr = np.frombuffer(b64_or_bytes, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def extract_embeddings(frames_b64: list) -> tuple[np.ndarray | None, bytes | None]:
    """
    Process a list of base64-encoded frames and return the mean ArcFace
    embedding and a JPEG thumbnail of the best face found.

    CHANGED from v3.1:
      Detection:  InsightFace FaceAnalysis.get() → YOLOv8 detect_largest_face()
      Embedding:  face.normed_embedding          → _get_embedding_from_crop()
      Thumbnail:  same logic (largest face crop)

    All other behaviour (averaging, normalization, sample count) unchanged.
    """
    detector  = get_yolo_detector()
    embeddings: list[np.ndarray] = []
    thumbnail: bytes | None = None

    for b64 in frames_b64:
        frame = decode_image(b64)
        if frame is None:
            continue

        # ── YOLO detection ────────────────────────────────────────────────
        face = detector.detect_largest(frame)
        if face is None:
            log.debug("extract_embeddings: no face detected in frame")
            continue

        # ── ArcFace embedding ─────────────────────────────────────────────
        emb = _get_embedding_from_crop(face.crop_bgr)
        if emb is None:
            continue
        embeddings.append(emb)

        # Thumbnail: first successful face crop resized to 128×128
        if thumbnail is None:
            crop128 = cv2.resize(face.crop_bgr, (128, 128))
            _, buf  = cv2.imencode(".jpg", crop128, [cv2.IMWRITE_JPEG_QUALITY, 85])
            thumbnail = buf.tobytes()

    if not embeddings:
        log.warning("extract_embeddings: no embeddings from %d frames", len(frames_b64))
        return None, None

    mean_emb  = np.mean(embeddings, axis=0)
    norm       = np.linalg.norm(mean_emb)
    mean_emb  = mean_emb / norm if norm > 0 else mean_emb
    log.info("extract_embeddings: %d/%d frames → embedding", len(embeddings), len(frames_b64))
    return mean_emb, thumbnail


def _cosine_similarity(a, b) -> float:
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def find_best_match(conn, query_emb, faculty_id=None, semester=None):
    """Cosine-similarity search unchanged from v3.1."""
    if PGVECTOR_AVAILABLE:
        if faculty_id and semester:
            row = qone(conn, """
                SELECT student_id, full_name,
                       1-(embedding <=> %s::vector) AS similarity
                FROM students WHERE embedding IS NOT NULL
                  AND department=(SELECT name FROM faculties WHERE id=%s)
                  AND semester=%s
                ORDER BY embedding <=> %s::vector LIMIT 1
            """, (query_emb.tolist(), faculty_id, str(semester), query_emb.tolist()))
        else:
            row = qone(conn, """
                SELECT student_id, full_name,
                       1-(embedding <=> %s::vector) AS similarity
                FROM students WHERE embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector LIMIT 1
            """, (query_emb.tolist(), query_emb.tolist()))
        if not row:
            return None, None, 0.0
        return row["student_id"], row["full_name"], float(row["similarity"])
    else:
        if faculty_id and semester:
            rows = qall(conn, """
                SELECT student_id, full_name, embedding FROM students
                WHERE embedding IS NOT NULL
                  AND department=(SELECT name FROM faculties WHERE id=%s)
                  AND semester=%s
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
                stored = np.asarray(json.loads(raw) if isinstance(raw, str) else list(raw), dtype=np.float32)
            except Exception:
                continue
            sim = _cosine_similarity(query_emb, stored)
            if sim > best_sim:
                best_sim, best_sid, best_name = sim, row["student_id"], row["full_name"]
        return best_sid, best_name, best_sim


# ═════════════════════════════════════════════════════════════════════════════
#  FRAME QUALITY SCORING (delegates to face_detector.py)
# ═════════════════════════════════════════════════════════════════════════════
def score_frame_quality(frame_bgr, face_bbox=None):
    """
    Backward-compatible wrapper.
    app.py callers pass (frame, bbox_list); we adapt to the YOLO version.
    """
    # Reconstruct a minimal FaceDetectionResult if bbox was passed
    face = None
    if face_bbox is not None:
        from face_detector import FaceDetectionResult
        face = FaceDetectionResult(bbox=list(face_bbox), confidence=0.9, crop_bgr=np.zeros((1,1,3), np.uint8))
    return score_frame_quality_yolo(frame_bgr, face)


# ═════════════════════════════════════════════════════════════════════════════
#  EMAIL
# ═════════════════════════════════════════════════════════════════════════════
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
    api_key   = os.getenv("BREVO_API_KEY", "")
    from_addr = os.getenv("BREVO_FROM", "")
    if not (api_key and from_addr):
        return
    today = date.today().isoformat()
    try:
        with get_db() as conn:
            already = qone(conn, """
                SELECT id FROM email_log
                WHERE student_id=%s AND subject=%s AND sent_at::date=%s AND success=true
            """, (student_id, subject, today))
        if already:
            return
    except Exception:
        pass
    payload = json.dumps({
        "sender": {"name": "वेदनेत्रम् Attendance", "email": from_addr},
        "to": [{"email": to_addr}],
        "subject": subject, "htmlContent": html_body,
    }).encode("utf-8")
    try:
        req = urllib.request.Request("https://api.brevo.com/v3/smtp/email",
            data=payload, method="POST", headers=_brevo_headers())
        with urllib.request.urlopen(req, timeout=15):
            pass
        with get_db() as conn:
            qexec(conn, "INSERT INTO email_log (student_id,email_to,subject,success) VALUES (%s,%s,%s,true)",
                  (student_id, to_addr, subject))
    except Exception as exc:
        err = str(exc)
        try:
            with get_db() as conn:
                qexec(conn, "INSERT INTO email_log (student_id,email_to,subject,success,error_msg) VALUES (%s,%s,%s,false,%s)",
                      (student_id, to_addr, subject, err))
        except Exception:
            pass
        if retry < 2:
            time.sleep(8*(retry+1))
            _send_email_now(to_addr, subject, html_body, student_id, retry+1)

def queue_attendance_email(student_id, name, dept, att_date, att_time, email_to, subject_name=None, teacher_name=None):
    if not EMAIL_ENABLED or not email_to:
        return
    subject_line = f"Attendance Confirmed — {att_date}"
    html = f"""<!DOCTYPE html><html><body style="font-family:Arial;">
<h2>Attendance Confirmed</h2><p>Dear {name},</p>
<p>Your attendance has been marked for {att_date} at {att_time}.</p>
<p>Subject: {subject_name or '—'} | Teacher: {teacher_name or '—'}</p>
</body></html>"""
    try:
        _email_queue.put_nowait({"to_addr":email_to,"subject":subject_line,"html_body":html,"student_id":student_id})
    except queue.Full:
        pass


# ═════════════════════════════════════════════════════════════════════════════
#  SSE
# ═════════════════════════════════════════════════════════════════════════════
_sse_clients = []
_sse_lock    = threading.Lock()

def sse_broadcast(data):
    msg = f"data: {json.dumps(data)}\n\n"
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)


# ═════════════════════════════════════════════════════════════════════════════
#  CAMERA STREAM — YOLOv8 detection in the live loop
# ═════════════════════════════════════════════════════════════════════════════
camera_state = {
    "active":     False,
    "cap":        None,
    "teacher_id": None,
    "subject_id": None,
}

def _gen_frames():
    """
    MJPEG frame generator for the /api/stream endpoint.

    CHANGED from v3.1:
      Detection:  fa.get(rgb)                → detect_faces_in_frame(frame_bgr)
      Embedding:  face.normed_embedding       → _get_embedding_from_crop(face.crop_bgr)
      Boxes:      InsightFace bbox format     → YOLOv8 [x1,y1,x2,y2]

    Frame skipping (SKIP env var) and all attendance marking are unchanged.
    """
    detector   = get_yolo_detector()
    cap        = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    camera_state["cap"] = cap

    frame_n    = 0
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

    log.info("Camera stream started (YOLOv8 detection, teacher_id=%s)", teacher_id)

    while camera_state["active"]:
        ok, frame = cap.read()
        if not ok:
            log.warning("Camera read failed — stopping stream")
            break

        frame_n += 1
        if frame_n % SKIP == 0:
            # ── YOLO detection ─────────────────────────────────────────────
            yolo_faces = detect_faces_in_frame(frame)

            for face in yolo_faces:
                x1, y1, x2, y2 = face.bbox

                # ── ArcFace embedding ──────────────────────────────────────
                emb = _get_embedding_from_crop(face.crop_bgr)
                if emb is None:
                    # Draw grey box for detected-but-unembeddable face
                    cv2.rectangle(frame, (x1,y1), (x2,y2), (80,80,80), 2)
                    cv2.putText(frame, "Embedding error", (x1, y1-8),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (80,80,80), 2)
                    continue

                # ── Recognition ────────────────────────────────────────────
                with get_db() as conn:
                    sid, name, sim = find_best_match(conn, emb, faculty_id, semester)

                label = "Unknown"
                color = (0, 60, 220)   # red-ish for unknown

                if sim >= THRESHOLD and sid:
                    label = name
                    color = (0, 200, 80)   # green for recognized
                    _mark_attendance_and_broadcast(sid, name, sim, teacher_id, subject_id)
                else:
                    _log_recognition(None, "Unknown", sim, False, teacher_id, subject_id)

                # Draw bounding box and label
                conf_pct = int(face.confidence * 100)
                cv2.rectangle(frame, (x1,y1), (x2,y2), color, 2)
                cv2.putText(
                    frame,
                    f"{label} ({sim*100:.0f}% | YOLO:{conf_pct}%)",
                    (x1, y1 - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.62, color, 2,
                )

        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"

    cap.release()
    camera_state["cap"] = None
    log.info("Camera stream ended")


# ═════════════════════════════════════════════════════════════════════════════
#  ATTENDANCE HELPERS (unchanged logic)
# ═════════════════════════════════════════════════════════════════════════════
def _mark_attendance_and_broadcast(student_id, name, confidence, teacher_id=None, subject_id=None):
    today = date.today().isoformat()
    now   = datetime.now().strftime("%H:%M:%S")
    key   = (student_id, today, subject_id)
    ts    = time.time()

    for k, v in list(_recent_marks.items()):
        if ts - v > _RECENT_MARK_TTL:
            _recent_marks.pop(k, None)

    recently = key in _recent_marks
    dept = email = subject_name = teacher_name = None

    with get_db() as conn:
        with conn.cursor() as c:
            marked = False
            if not recently:
                c.execute("""
                    INSERT INTO attendance (student_id,teacher_id,subject_id,date,time,status)
                    VALUES (%s,%s,%s,%s,%s,'Present')
                    ON CONFLICT (student_id,date,subject_id) DO NOTHING
                """, (student_id, teacher_id, subject_id, today, now))
                marked = c.rowcount == 1
                if marked:
                    _recent_marks[key] = ts
            c.execute("""
                INSERT INTO recognition_logs
                    (student_id,full_name,confidence,recognized,teacher_id,subject_id,detection_engine)
                VALUES (%s,%s,%s,true,%s,%s,'yolov8')
            """, (student_id, name, round(confidence*100, 1), teacher_id, subject_id))

        if marked:
            row = qone(conn, "SELECT department,email FROM students WHERE student_id=%s", (student_id,))
            if row:
                dept  = row.get("department")
                email = row.get("email")
            if subject_id:
                s = qone(conn, "SELECT name FROM subjects WHERE id=%s", (subject_id,))
                if s:
                    subject_name = s["name"]
            if teacher_id:
                t = qone(conn, "SELECT full_name FROM teachers WHERE id=%s", (teacher_id,))
                if t:
                    teacher_name = t["full_name"]

    if marked:
        sse_broadcast({
            "type": "attendance", "student_id": student_id, "name": name,
            "confidence": round(confidence*100, 1), "time": now, "date": today,
            "subject": subject_name, "teacher": teacher_name,
            "detection_engine": "yolov8",
        })
        if email:
            queue_attendance_email(student_id, name, dept, today, now, email, subject_name, teacher_name)


def _log_recognition(sid, name, confidence, recognized, teacher_id=None, subject_id=None):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO recognition_logs
                    (student_id,full_name,confidence,recognized,teacher_id,subject_id,detection_engine)
                VALUES (%s,%s,%s,%s,%s,%s,'yolov8')
            """, (sid, name, round(confidence*100, 1), recognized, teacher_id, subject_id))


def _log_activity(admin_user, action, target_type, target_id=None, detail=None):
    try:
        with get_db() as conn:
            qexec(conn, """
                INSERT INTO activity_logs (admin_user,action,target_type,target_id,detail)
                VALUES (%s,%s,%s,%s,%s)
            """, (admin_user, action, target_type, target_id, detail))
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════════
#  AUTH
# ═════════════════════════════════════════════════════════════════════════════
def _hash(pw): return hashlib.sha256(pw.encode()).hexdigest()

def _get_admin_username():
    if g.user and isinstance(g.user, dict) and "username" in g.user:
        return g.user["username"]
    return "unknown"

def _create_session(user_type, user_id=None, student_id=None):
    tok = secrets.token_hex(32)
    with get_db() as conn:
        qexec(conn, "INSERT INTO sessions (token,user_type,user_id,student_id) VALUES (%s,%s,%s,%s)",
              (tok, user_type, user_id, student_id))
    return tok

def _get_session(token):
    if not token:
        return None
    with get_db() as conn:
        return qone(conn, "SELECT * FROM sessions WHERE token=%s AND expires_at>NOW()", (token,))

def require_auth(roles=None):
    from functools import wraps
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            tok  = request.headers.get("Authorization","").replace("Bearer ","")
            sess = _get_session(tok)
            if not sess:
                return jsonify({"error":"Unauthorized","code":"SESSION_EXPIRED"}), 401
            allowed = roles or ["admin","teacher","student"]
            if sess["user_type"] not in allowed:
                return jsonify({"error":"Forbidden"}), 403
            g.session    = sess
            g.role       = sess["user_type"]
            g.user_id    = sess.get("user_id")
            g.student_id = sess.get("student_id")
            if sess["user_type"] == "admin" and sess.get("user_id"):
                with get_db() as conn:
                    g.user = qone(conn, "SELECT id,username,role FROM users WHERE id=%s", (sess["user_id"],))
            elif sess["user_type"] == "teacher" and sess.get("user_id"):
                with get_db() as conn:
                    g.user = qone(conn, """
                        SELECT t.*, f.name AS faculty_name, sub.name AS subject_name,
                               ts.label AS time_slot_label, ts.start_time::text, ts.end_time::text
                        FROM teachers t
                        LEFT JOIN faculties f ON f.id=t.faculty_id
                        LEFT JOIN subjects sub ON sub.id=t.subject_id
                        LEFT JOIN time_slots ts ON ts.id=t.time_slot_id
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
        tok  = request.headers.get("Authorization","").replace("Bearer ","")
        sess = _get_session(tok)
        if sess and sess["user_type"] == "admin":
            with get_db() as conn:
                g.user = qone(conn, "SELECT id,username,role FROM users WHERE id=%s", (sess["user_id"],))
            g.session = sess
            g.role    = "admin"
            return fn(*args, **kwargs)
        with get_db() as conn:
            user = qone(conn, "SELECT id,username,role FROM users WHERE password_hash=%s AND role='admin'", (tok,))
        if not user:
            return jsonify({"error":"Unauthorized","code":"SESSION_EXPIRED"}), 401
        g.user    = user
        g.role    = "admin"
        g.session = None
        return fn(*args, **kwargs)
    return wrapper


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — HEALTH  (now includes YOLOv8 status)
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/api/health")
def health():
    try:
        with get_db() as conn:
            qone(conn, "SELECT 1")
        db_ok = True
    except Exception as e:
        return jsonify({"status":"error","db":"error","detail":str(e)}), 503

    detector_info = get_yolo_detector().model_info
    arcface_ok    = get_arcface_model() is not None

    return jsonify({
        "status":           "ok",
        "db":               "ok",
        "pgvector":         "ok" if PGVECTOR_AVAILABLE else "missing",
        "email":            "enabled" if EMAIL_ENABLED else "disabled",
        "detection_engine": "yolov8",
        "yolo":             detector_info,
        "arcface":          "ok" if arcface_ok else "error",
        "timestamp":        datetime.now().isoformat(),
        "version":          "3.2-yolov8",
    })


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — AUTH
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/api/auth/login", methods=["POST"])
def login():
    d         = request.json or {}
    role_hint = d.get("role","admin")

    if role_hint == "student":
        email = (d.get("email") or "").strip().lower()
        if not email:
            return jsonify({"error":"Email required"}), 400
        with get_db() as conn:
            student = qone(conn, "SELECT * FROM students WHERE LOWER(email)=%s AND status='active'", (email,))
        if not student:
            return jsonify({"error":"Student not found or inactive"}), 401
        token = _create_session("student", student_id=student["student_id"])
        return jsonify({
            "token":token,"role":"student",
            "student_id":student["student_id"],"full_name":student["full_name"],
            "department":student["department"],"semester":student["semester"],
        })

    elif role_hint == "teacher":
        email    = (d.get("email") or "").strip().lower()
        password = d.get("password","")
        if not email:
            return jsonify({"error":"Email required"}), 400
        with get_db() as conn:
            teacher = qone(conn,
                "SELECT * FROM teachers WHERE LOWER(email)=%s AND password_hash=%s AND status='active'",
                (email, _hash(password)))
        if not teacher:
            return jsonify({"error":"Invalid credentials"}), 401
        token = _create_session("teacher", user_id=teacher["id"])
        with get_db() as conn:
            full = qone(conn, """
                SELECT t.*, f.name AS faculty_name, sub.name AS subject_name, ts.label AS time_slot_label
                FROM teachers t
                LEFT JOIN faculties f ON f.id=t.faculty_id
                LEFT JOIN subjects sub ON sub.id=t.subject_id
                LEFT JOIN time_slots ts ON ts.id=t.time_slot_id
                WHERE t.id=%s
            """, (teacher["id"],))
        return jsonify({
            "token":token,"role":"teacher",
            "teacher_id":teacher["teacher_id"],"full_name":teacher["full_name"],
            "faculty":full.get("faculty_name") if full else None,
            "semester":teacher["semester"],"subject":full.get("subject_name") if full else None,
            "time_slot":full.get("time_slot_label") if full else None,
            "faculty_id":teacher["faculty_id"],"subject_id":teacher["subject_id"],
            "time_slot_id":teacher["time_slot_id"],"db_id":teacher["id"],
        })

    else:  # admin
        username = (d.get("username") or "").strip()
        password = d.get("password","")
        with get_db() as conn:
            user = qone(conn,
                "SELECT id,username,role,password_hash FROM users WHERE username=%s AND password_hash=%s",
                (username, _hash(password)))
        if not user:
            return jsonify({"error":"Invalid credentials"}), 401
        token = _create_session("admin", user_id=user["id"])
        return jsonify({
            "token":token,"legacy_token":user["password_hash"],
            "role":user["role"],"username":user["username"],
        })

@app.route("/api/auth/validate")
def validate_token():
    tok  = request.headers.get("Authorization","").replace("Bearer ","")
    if not tok:
        return jsonify({"valid":False,"reason":"no_token"})
    sess = _get_session(tok)
    if not sess:
        return jsonify({"valid":False,"reason":"session_expired"})
    return jsonify({"valid":True,"user_type":sess["user_type"]})

@app.route("/api/auth/me")
def me():
    tok  = request.headers.get("Authorization","").replace("Bearer ","")
    sess = _get_session(tok)
    if sess:
        return jsonify({"user_type":sess["user_type"],"user_id":sess.get("user_id"),"student_id":sess.get("student_id")})
    with get_db() as conn:
        user = qone(conn, "SELECT id,username,role FROM users WHERE password_hash=%s", (tok,))
    if not user:
        return jsonify({"error":"Unauthorized","code":"SESSION_EXPIRED"}), 401
    return jsonify(user)

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    tok = request.headers.get("Authorization","").replace("Bearer ","")
    if tok:
        try:
            with get_db() as conn:
                qexec(conn, "DELETE FROM sessions WHERE token=%s", (tok,))
        except Exception:
            pass
    return jsonify({"ok":True})

@app.route("/api/auth/change-password", methods=["POST"])
@require_admin
def change_password():
    d      = request.json or {}
    old_pw = d.get("old_password","")
    new_pw = d.get("new_password","")
    if not new_pw or len(new_pw) < 6:
        return jsonify({"error":"New password must be at least 6 characters"}), 400
    with get_db() as conn:
        user = qone(conn, "SELECT id FROM users WHERE id=%s AND password_hash=%s", (g.user["id"], _hash(old_pw)))
        if not user:
            return jsonify({"error":"Current password is incorrect"}), 401
        qexec(conn, "UPDATE users SET password_hash=%s WHERE id=%s", (_hash(new_pw), g.user["id"]))
    return jsonify({"updated":True})


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — FACULTIES
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/api/faculties")
def list_faculties():
    with get_db() as conn:
        rows = qall(conn, "SELECT * FROM faculties ORDER BY name")
    return jsonify({"faculties":rows})

@app.route("/api/faculties", methods=["POST"])
@require_admin
def create_faculty():
    d    = request.json or {}
    name = (d.get("name") or "").strip()
    code = (d.get("code") or "").strip() or None
    if not name:
        return jsonify({"error":"Faculty name required"}), 400
    with get_db() as conn:
        if qone(conn, "SELECT id FROM faculties WHERE LOWER(name)=LOWER(%s)", (name,)):
            return jsonify({"error":"Faculty already exists"}), 409
        with conn.cursor() as c:
            c.execute("INSERT INTO faculties (name,code) VALUES (%s,%s) RETURNING id", (name,code))
            new_id = c.fetchone()[0]
    _log_activity(g.user["username"],"create_faculty","faculty",str(new_id),name)
    return jsonify({"id":new_id,"name":name}), 201

@app.route("/api/faculties/<int:fid>", methods=["PUT"])
@require_admin
def update_faculty(fid):
    d    = request.json or {}
    name = (d.get("name") or "").strip()
    code = (d.get("code") or "").strip() or None
    if not name:
        return jsonify({"error":"Faculty name required"}), 400
    with get_db() as conn:
        rows = qexec(conn, "UPDATE faculties SET name=%s,code=%s WHERE id=%s", (name,code,fid))
    if rows == 0:
        return jsonify({"error":"Not found"}), 404
    return jsonify({"updated":True})

@app.route("/api/faculties/<int:fid>", methods=["DELETE"])
@require_admin
def delete_faculty(fid):
    with get_db() as conn:
        rows = qexec(conn, "DELETE FROM faculties WHERE id=%s", (fid,))
    if rows == 0:
        return jsonify({"error":"Not found"}), 404
    return jsonify({"deleted":True})


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — SUBJECTS
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/api/subjects")
def list_subjects():
    fid = request.args.get("faculty_id")
    sem = request.args.get("semester")
    sql = "SELECT s.*, f.name AS faculty_name FROM subjects s LEFT JOIN faculties f ON f.id=s.faculty_id WHERE 1=1"
    params = []
    if fid:
        sql += " AND s.faculty_id=%s"; params.append(fid)
    if sem:
        sql += " AND s.semester=%s";   params.append(sem)
    sql += " ORDER BY f.name,s.semester,s.name"
    with get_db() as conn:
        rows = qall(conn, sql, params)
    return jsonify({"subjects":rows})

@app.route("/api/semesters")
def list_semesters():
    fid = request.args.get("faculty_id")
    if not fid:
        return jsonify({"semesters":[]})
    with get_db() as conn:
        rows = qall(conn, """
            SELECT gs.semester, COUNT(s.id) AS subject_count
            FROM generate_series(1,8) AS gs(semester)
            LEFT JOIN subjects s ON s.semester=gs.semester AND s.faculty_id=%s
            GROUP BY gs.semester ORDER BY gs.semester
        """, (fid,))
    return jsonify({"semesters":[{"value":int(r["semester"]),"name":f"Semester {int(r['semester'])}","subject_count":int(r["subject_count"] or 0)} for r in rows]})

@app.route("/api/subjects", methods=["POST"])
@require_admin
def create_subject():
    d        = request.json or {}
    name     = (d.get("name") or "").strip()
    code     = (d.get("code") or "").strip() or None
    fid      = d.get("faculty_id")
    semester = d.get("semester")
    if not name or not fid or not semester:
        return jsonify({"error":"name, faculty_id, semester required"}), 400
    semester = int(semester)
    if not (1 <= semester <= 8):
        return jsonify({"error":"Semester must be 1-8"}), 400
    with get_db() as conn:
        if not qone(conn, "SELECT id FROM faculties WHERE id=%s", (fid,)):
            return jsonify({"error":"Faculty not found"}), 404
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO subjects (name,code,faculty_id,semester)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (name,faculty_id,semester) DO UPDATE SET code=EXCLUDED.code
                RETURNING id
            """, (name,code,fid,semester))
            new_id = c.fetchone()[0]
    _log_activity(g.user["username"],"create_subject","subject",str(new_id),name)
    return jsonify({"id":new_id,"name":name}), 201

@app.route("/api/subjects/<int:sid>", methods=["PUT"])
@require_admin
def update_subject(sid):
    d    = request.json or {}
    sets, params = [], []
    if d.get("name"):   sets.append("name=%s");       params.append(d["name"].strip())
    if "code" in d:     sets.append("code=%s");        params.append(d["code"] or None)
    if d.get("faculty_id"): sets.append("faculty_id=%s"); params.append(d["faculty_id"])
    if d.get("semester"):
        sem = int(d["semester"])
        if not (1 <= sem <= 8): return jsonify({"error":"Semester must be 1-8"}), 400
        sets.append("semester=%s"); params.append(sem)
    if not sets: return jsonify({"error":"Nothing to update"}), 400
    params.append(sid)
    with get_db() as conn:
        rows = qexec(conn, f"UPDATE subjects SET {','.join(sets)} WHERE id=%s", params)
    if rows == 0: return jsonify({"error":"Not found"}), 404
    return jsonify({"updated":True})

@app.route("/api/subjects/<int:sid>", methods=["DELETE"])
@require_admin
def delete_subject(sid):
    with get_db() as conn:
        rows = qexec(conn, "DELETE FROM subjects WHERE id=%s", (sid,))
    if rows == 0: return jsonify({"error":"Not found"}), 404
    return jsonify({"deleted":True})


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — TIME SLOTS
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/api/time-slots")
def list_time_slots():
    with get_db() as conn:
        rows = qall(conn, """
            SELECT ts.id, ts.label,
                   ts.start_time::text AS start_time, ts.end_time::text AS end_time,
                   ts.created_at::text AS created_at,
                   cs.faculty_id, cs.semester,
                   f.name AS assigned_faculty, t.full_name AS assigned_teacher,
                   sub.name AS assigned_subject
            FROM time_slots ts
            LEFT JOIN class_schedules cs ON cs.time_slot_id=ts.id
            LEFT JOIN faculties f ON f.id=cs.faculty_id
            LEFT JOIN teachers t ON t.id=cs.teacher_id
            LEFT JOIN subjects sub ON sub.id=cs.subject_id
            ORDER BY ts.start_time
        """)
    return jsonify({"time_slots":rows})

@app.route("/api/time-slots", methods=["POST"])
@require_admin
def create_time_slot():
    d  = request.json or {}
    lb = (d.get("label") or "").strip()
    st = (d.get("start_time") or "").strip()
    et = (d.get("end_time") or "").strip()
    if not lb or not st or not et:
        return jsonify({"error":"label, start_time, end_time required"}), 400
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("INSERT INTO time_slots (label,start_time,end_time) VALUES (%s,%s,%s) RETURNING id", (lb,st,et))
            new_id = c.fetchone()[0]
    return jsonify({"id":new_id,"label":lb}), 201

@app.route("/api/time-slots/<int:tsid>", methods=["DELETE"])
@require_admin
def delete_time_slot(tsid):
    with get_db() as conn:
        rows = qexec(conn, "DELETE FROM time_slots WHERE id=%s", (tsid,))
    if rows == 0: return jsonify({"error":"Not found"}), 404
    return jsonify({"deleted":True})

@app.route("/api/schedule")
def get_schedule():
    fid = request.args.get("faculty_id")
    sem = request.args.get("semester")
    if not fid or not sem:
        return jsonify({"error":"faculty_id and semester required"}), 400
    with get_db() as conn:
        rows = qall(conn, """
            SELECT cs.id, cs.time_slot_id, ts.label AS time_slot_label,
                   ts.start_time::text, ts.end_time::text,
                   t.full_name AS teacher_name, t.teacher_id,
                   sub.name AS subject_name, sub.code AS subject_code
            FROM class_schedules cs
            JOIN time_slots ts ON ts.id=cs.time_slot_id
            LEFT JOIN teachers t ON t.id=cs.teacher_id
            LEFT JOIN subjects sub ON sub.id=cs.subject_id
            WHERE cs.faculty_id=%s AND cs.semester=%s
            ORDER BY ts.start_time
        """, (fid,sem))
    return jsonify({"schedule":rows})

@app.route("/api/schedule", methods=["POST"])
@require_admin
def assign_schedule():
    d   = request.json or {}
    fid = d.get("faculty_id"); sem = d.get("semester"); tsid = d.get("time_slot_id")
    tid = d.get("teacher_id"); sid = d.get("subject_id")
    if not all([fid,sem,tsid]):
        return jsonify({"error":"faculty_id, semester, time_slot_id required"}), 400
    sem = int(sem)
    if not (1 <= sem <= 8): return jsonify({"error":"Semester must be 1-8"}), 400
    with get_db() as conn:
        fac = qone(conn, "SELECT id,name FROM faculties WHERE id=%s", (fid,))
        if not fac: return jsonify({"error":"Faculty not found"}), 404
        existing = qone(conn, """
            SELECT cs.id, t.full_name AS teacher_name
            FROM class_schedules cs LEFT JOIN teachers t ON t.id=cs.teacher_id
            WHERE cs.faculty_id=%s AND cs.semester=%s AND cs.time_slot_id=%s
        """, (fid,sem,tsid))
        if existing:
            ts = qone(conn, "SELECT label FROM time_slots WHERE id=%s", (tsid,))
            return jsonify({"error":f"Schedule conflict: {fac['name']} Semester {sem} at '{ts['label']}' is already assigned to {existing['teacher_name'] or 'another teacher'}.","code":"SCHEDULE_CONFLICT","conflict_id":existing["id"]}), 409
        with conn.cursor() as c:
            c.execute("INSERT INTO class_schedules (faculty_id,semester,time_slot_id,teacher_id,subject_id) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                      (fid,sem,tsid,tid,sid))
            new_id = c.fetchone()[0]
    return jsonify({"id":new_id,"assigned":True}), 201

@app.route("/api/schedule/<int:sid>", methods=["DELETE"])
@require_admin
def remove_schedule(sid):
    with get_db() as conn:
        rows = qexec(conn, "DELETE FROM class_schedules WHERE id=%s", (sid,))
    if rows == 0: return jsonify({"error":"Not found"}), 404
    return jsonify({"deleted":True})


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — TEACHERS
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/api/teachers")
@require_admin
def list_teachers():
    with get_db() as conn:
        rows = qall(conn, """
            SELECT t.id,t.teacher_id,t.full_name,t.email,t.phone,t.status,
                   t.semester,t.faculty_id,t.subject_id,t.time_slot_id,
                   f.name AS faculty_name, sub.name AS subject_name,
                   ts.label AS time_slot_label,
                   ts.start_time::text, ts.end_time::text, t.created_at::text
            FROM teachers t
            LEFT JOIN faculties f ON f.id=t.faculty_id
            LEFT JOIN subjects sub ON sub.id=t.subject_id
            LEFT JOIN time_slots ts ON ts.id=t.time_slot_id
            ORDER BY t.full_name
        """)
        for row in rows:
            row["assignments"] = qall(conn, """
                SELECT ta.id,ta.faculty_id,ta.semester,ta.subject_id,ta.time_slot_id,ta.is_primary,
                       f.name AS faculty_name, sub.name AS subject_name, ts.label AS time_slot_label
                FROM teacher_assignments ta
                LEFT JOIN faculties f ON f.id=ta.faculty_id
                LEFT JOIN subjects sub ON sub.id=ta.subject_id
                LEFT JOIN time_slots ts ON ts.id=ta.time_slot_id
                WHERE ta.teacher_id=%s ORDER BY ta.is_primary DESC
            """, (row["id"],))
    return jsonify({"teachers":rows})

@app.route("/api/teachers/<int:tid>")
@require_admin
def get_teacher(tid):
    with get_db() as conn:
        row = qone(conn, """
            SELECT t.id,t.teacher_id,t.full_name,t.email,t.phone,t.status,
                   t.semester,t.faculty_id,t.subject_id,t.time_slot_id,
                   f.name AS faculty_name, sub.name AS subject_name,
                   ts.label AS time_slot_label, ts.start_time::text, ts.end_time::text, t.created_at::text
            FROM teachers t
            LEFT JOIN faculties f ON f.id=t.faculty_id
            LEFT JOIN subjects sub ON sub.id=t.subject_id
            LEFT JOIN time_slots ts ON ts.id=t.time_slot_id
            WHERE t.id=%s
        """, (tid,))
        if not row: return jsonify({"error":"Not found"}), 404
        row["assignments"] = qall(conn, """
            SELECT ta.id,ta.faculty_id,ta.semester,ta.subject_id,ta.time_slot_id,ta.is_primary,
                   f.name AS faculty_name, sub.name AS subject_name, ts.label AS time_slot_label
            FROM teacher_assignments ta
            LEFT JOIN faculties f ON f.id=ta.faculty_id
            LEFT JOIN subjects sub ON sub.id=ta.subject_id
            LEFT JOIN time_slots ts ON ts.id=ta.time_slot_id
            WHERE ta.teacher_id=%s ORDER BY ta.is_primary DESC
        """, (tid,))
    return jsonify(row)

@app.route("/api/teachers", methods=["POST"])
@require_admin
def create_teacher():
    d          = request.json or {}
    teacher_id = (d.get("teacher_id") or "").strip()
    full_name  = (d.get("full_name") or "").strip()
    password   = (d.get("password") or "").strip()
    email      = (d.get("email") or "").strip() or None
    phone      = (d.get("phone") or "").strip() or None
    fid        = d.get("faculty_id") or None
    semester   = d.get("semester") or None
    subject_id = d.get("subject_id") or None
    tsid       = d.get("time_slot_id") or None

    if not teacher_id or not full_name or not password:
        return jsonify({"error":"teacher_id, full_name, password required"}), 400
    if len(password) < 6:
        return jsonify({"error":"Password must be at least 6 characters"}), 400
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify({"error":"Invalid email format"}), 400
    if semester:
        semester = int(semester)
        if not (1 <= semester <= 8): return jsonify({"error":"Semester must be 1-8"}), 400

    if subject_id and fid and semester:
        with get_db() as conn:
            if not qone(conn, "SELECT id FROM subjects WHERE id=%s AND faculty_id=%s AND semester=%s", (subject_id,fid,semester)):
                return jsonify({"error":"Subject does not belong to the selected faculty and semester."}), 400

    with get_db() as conn:
        if qone(conn, "SELECT id FROM teachers WHERE teacher_id=%s", (teacher_id,)):
            return jsonify({"error":"Teacher ID already exists"}), 409
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO teachers (teacher_id,full_name,password_hash,email,phone,faculty_id,semester,subject_id,time_slot_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
            """, (teacher_id,full_name,_hash(password),email,phone,fid,semester,subject_id,tsid))
            new_id = c.fetchone()[0]
        if fid and semester and subject_id:
            try:
                qexec(conn, """
                    INSERT INTO teacher_assignments (teacher_id,faculty_id,semester,subject_id,time_slot_id,is_primary)
                    VALUES (%s,%s,%s,%s,%s,true) ON CONFLICT DO NOTHING
                """, (new_id,fid,semester,subject_id,tsid))
            except Exception:
                pass

    _log_activity(g.user["username"],"create_teacher","teacher",str(new_id),full_name)
    return jsonify({"id":new_id,"teacher_id":teacher_id}), 201

@app.route("/api/teachers/<int:tid>", methods=["PUT"])
@require_admin
def update_teacher(tid):
    d       = request.json or {}
    ALLOWED = {"full_name","email","phone","faculty_id","semester","subject_id","time_slot_id","status"}
    fields  = {k:d[k] for k in ALLOWED if k in d}
    if d.get("password"):
        pw = d["password"].strip()
        if len(pw) < 6: return jsonify({"error":"Password must be at least 6 characters"}), 400
        fields["password_hash"] = _hash(pw)
    if not fields: return jsonify({"error":"Nothing to update"}), 400
    if "email" in fields and fields["email"]:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", fields["email"]):
            return jsonify({"error":"Invalid email format"}), 400
    if "semester" in fields and fields["semester"]:
        fields["semester"] = int(fields["semester"])
        if not (1 <= fields["semester"] <= 8): return jsonify({"error":"Semester must be 1-8"}), 400
    subject_id = fields.get("subject_id") or d.get("subject_id")
    faculty_id = fields.get("faculty_id") or d.get("faculty_id")
    semester   = fields.get("semester") or d.get("semester")
    if subject_id and faculty_id and semester:
        with get_db() as conn:
            if not qone(conn, "SELECT id FROM subjects WHERE id=%s AND faculty_id=%s AND semester=%s", (subject_id,faculty_id,int(semester))):
                return jsonify({"error":"Subject does not belong to selected faculty and semester."}), 400
    sql = "UPDATE teachers SET " + ", ".join(f"{k}=%s" for k in fields) + " WHERE id=%s"
    with get_db() as conn:
        rows = qexec(conn, sql, list(fields.values())+[tid])
    if rows == 0: return jsonify({"error":"Not found"}), 404
    return jsonify({"updated":True})

@app.route("/api/teachers/<int:tid>", methods=["DELETE"])
@require_admin
def delete_teacher(tid):
    try:
        with get_db() as conn:
            att = qone(conn, "SELECT COUNT(*) AS c FROM attendance WHERE teacher_id=%s", (tid,))
            rec = qone(conn, "SELECT COUNT(*) AS c FROM recognition_logs WHERE teacher_id=%s", (tid,))
            if (att["c"] or 0) > 0 or (rec["c"] or 0) > 0:
                return jsonify({"error":f"Cannot delete: {att['c']} attendance and {rec['c']} recognition records reference this teacher.","attendance_count":att["c"],"recognition_count":rec["c"]}), 409
            rows = qexec(conn, "DELETE FROM teachers WHERE id=%s", (tid,))
        if rows == 0: return jsonify({"error":"Not found"}), 404
        _log_activity(_get_admin_username(),"delete_teacher","teacher",str(tid))
        return jsonify({"deleted":True})
    except Exception as e:
        return jsonify({"error":str(e)}), 500

@app.route("/api/teachers/<int:tid>/references")
@require_admin
def teacher_references(tid):
    with get_db() as conn:
        rows = qall(conn, """
            SELECT a.id,a.student_id,s.full_name,a.date,a.time,a.status,a.note
            FROM attendance a JOIN students s ON s.student_id=a.student_id
            WHERE a.teacher_id=%s ORDER BY a.date DESC LIMIT 500
        """, (tid,))
    return jsonify({"rows":rows,"count":len(rows)})

@app.route("/api/teachers/<int:tid>/clear-attendance", methods=["POST"])
@require_admin
def clear_teacher_attendance(tid):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE attendance SET teacher_id=NULL WHERE teacher_id=%s", (tid,))
            cleared = c.rowcount
    return jsonify({"cleared":cleared})

@app.route("/api/teachers/<int:tid>/reassign-attendance", methods=["POST"])
@require_admin
def reassign_teacher_attendance(tid):
    d   = request.json or {}
    to  = d.get("to")
    if not to: return jsonify({"error":"Target teacher ID required"}), 400
    with get_db() as conn:
        if not qone(conn, "SELECT id FROM teachers WHERE id=%s", (to,)):
            return jsonify({"error":"Target teacher not found"}), 404
        with conn.cursor() as c:
            c.execute("UPDATE attendance SET teacher_id=%s WHERE teacher_id=%s", (to,tid))
            moved = c.rowcount
    return jsonify({"reassigned":moved})

@app.route("/api/teachers/<int:tid>/recognition-logs")
@require_admin
def teacher_recognition_logs(tid):
    with get_db() as conn:
        rows = qall(conn, """
            SELECT id,student_id,full_name,confidence,recognized,detection_engine,logged_at
            FROM recognition_logs WHERE teacher_id=%s ORDER BY logged_at DESC LIMIT 500
        """, (tid,))
    return jsonify({"rows":rows,"count":len(rows)})

@app.route("/api/teachers/<int:tid>/clear-recognition-logs", methods=["POST"])
@require_admin
def clear_teacher_recognition_logs(tid):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("UPDATE recognition_logs SET teacher_id=NULL WHERE teacher_id=%s", (tid,))
            cleared = c.rowcount
    return jsonify({"cleared":cleared})

@app.route("/api/teachers/<int:tid>/reassign-recognition-logs", methods=["POST"])
@require_admin
def reassign_teacher_recognition_logs(tid):
    d  = request.json or {}
    to = d.get("to")
    if not to: return jsonify({"error":"Target teacher ID required"}), 400
    with get_db() as conn:
        if not qone(conn, "SELECT id FROM teachers WHERE id=%s", (to,)):
            return jsonify({"error":"Target not found"}), 404
        with conn.cursor() as c:
            c.execute("UPDATE recognition_logs SET teacher_id=%s WHERE teacher_id=%s", (to,tid))
            moved = c.rowcount
    return jsonify({"reassigned":moved})

@app.route("/api/teachers/<int:tid>/assignments")
@require_admin
def list_teacher_assignments(tid):
    with get_db() as conn:
        rows = qall(conn, """
            SELECT ta.id,ta.faculty_id,ta.semester,ta.subject_id,ta.time_slot_id,ta.is_primary,ta.created_at::text,
                   f.name AS faculty_name, sub.name AS subject_name, sub.code AS subject_code,
                   ts.label AS time_slot_label, ts.start_time::text, ts.end_time::text
            FROM teacher_assignments ta
            LEFT JOIN faculties f ON f.id=ta.faculty_id
            LEFT JOIN subjects sub ON sub.id=ta.subject_id
            LEFT JOIN time_slots ts ON ts.id=ta.time_slot_id
            WHERE ta.teacher_id=%s ORDER BY ta.is_primary DESC,ta.faculty_id,ta.semester
        """, (tid,))
    return jsonify({"assignments":rows})

@app.route("/api/teachers/<int:tid>/assignments", methods=["POST"])
@require_admin
def add_teacher_assignment(tid):
    d    = request.json or {}
    fid  = d.get("faculty_id"); sem = d.get("semester"); sid = d.get("subject_id"); tsid = d.get("time_slot_id") or None
    if not all([fid,sem,sid]): return jsonify({"error":"faculty_id, semester, subject_id required"}), 400
    sem = int(sem)
    if not (1 <= sem <= 8): return jsonify({"error":"Semester must be 1-8"}), 400
    with get_db() as conn:
        if not qone(conn, "SELECT id FROM teachers WHERE id=%s", (tid,)):
            return jsonify({"error":"Teacher not found"}), 404
        if not qone(conn, "SELECT id FROM subjects WHERE id=%s AND faculty_id=%s AND semester=%s", (sid,fid,sem)):
            return jsonify({"error":"Subject does not belong to selected faculty and semester."}), 400
        if tsid:
            conflict = qone(conn, """
                SELECT cs.id,t.full_name AS teacher_name FROM class_schedules cs
                LEFT JOIN teachers t ON t.id=cs.teacher_id
                WHERE cs.faculty_id=%s AND cs.semester=%s AND cs.time_slot_id=%s AND cs.teacher_id!=%s
            """, (fid,sem,tsid,tid))
            if conflict:
                ts  = qone(conn, "SELECT label FROM time_slots WHERE id=%s", (tsid,))
                fac = qone(conn, "SELECT name FROM faculties WHERE id=%s", (fid,))
                return jsonify({"error":f"Schedule conflict: {fac['name'] if fac else fid} Semester {sem} at '{ts['label'] if ts else tsid}' is already assigned to {conflict['teacher_name'] or 'another teacher'}.","code":"SCHEDULE_CONFLICT"}), 409
        try:
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO teacher_assignments (teacher_id,faculty_id,semester,subject_id,time_slot_id,is_primary)
                    VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
                """, (tid,fid,sem,sid,tsid,False))
                new_id = c.fetchone()[0]
            if tsid:
                qexec(conn, """
                    INSERT INTO class_schedules (faculty_id,semester,time_slot_id,teacher_id,subject_id)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                """, (fid,sem,tsid,tid,sid))
        except Exception as e:
            if "unique" in str(e).lower():
                return jsonify({"error":"This assignment already exists."}), 409
            raise
    return jsonify({"id":new_id,"assigned":True}), 201

@app.route("/api/teachers/<int:tid>/assignments/<int:aid>", methods=["DELETE"])
@require_admin
def remove_teacher_assignment(tid, aid):
    with get_db() as conn:
        asg = qone(conn, "SELECT is_primary FROM teacher_assignments WHERE id=%s AND teacher_id=%s", (aid,tid))
        if not asg: return jsonify({"error":"Not found"}), 404
        qexec(conn, "DELETE FROM teacher_assignments WHERE id=%s AND teacher_id=%s", (aid,tid))
    return jsonify({"deleted":True,"was_primary":asg.get("is_primary",False)})


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — TEACHER PANEL
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/api/teacher/profile")
@require_auth(roles=["teacher"])
def teacher_profile():
    return jsonify(g.user)

@app.route("/api/teacher/students")
@require_auth(roles=["teacher"])
def teacher_students():
    t = g.user
    with get_db() as conn:
        students = qall(conn, """
            SELECT student_id,full_name,department,email,phone,semester,status,sample_count,enrolled_at::text
            FROM students
            WHERE department=(SELECT name FROM faculties WHERE id=%s) AND semester=%s AND status='active'
            ORDER BY full_name
        """, (t["faculty_id"], str(t["semester"])))
    return jsonify({"students":students,"count":len(students)})

@app.route("/api/teacher/attendance")
@require_auth(roles=["teacher"])
def teacher_attendance():
    target = request.args.get("date", date.today().isoformat())
    t      = g.user
    with get_db() as conn:
        records = qall(conn, """
            SELECT s.student_id,s.full_name,a.date::text,a.time::text,
                   COALESCE(a.status,'Absent') AS status, a.note
            FROM students s
            LEFT JOIN attendance a ON a.student_id=s.student_id AND a.date=%s AND a.subject_id=%s
            WHERE s.department=(SELECT name FROM faculties WHERE id=%s) AND s.semester=%s AND s.status='active'
            ORDER BY s.full_name
        """, (target,t["subject_id"],t["faculty_id"],str(t["semester"])))
    return jsonify({"date":target,"records":records,
                    "present":sum(1 for r in records if r["status"]=="Present"),
                    "absent":sum(1 for r in records if r["status"]=="Absent"),
                    "subject_id":t["subject_id"],"faculty_id":t["faculty_id"],"semester":t["semester"]})

@app.route("/api/teacher/attendance/mark", methods=["POST"])
@require_auth(roles=["teacher"])
def teacher_mark_attendance():
    d          = request.json or {}
    student_id = d.get("student_id")
    att_date   = d.get("date", date.today().isoformat())
    att_time   = d.get("time", datetime.now().strftime("%H:%M:%S"))
    status     = d.get("status","Present")
    note       = d.get("note","")
    t          = g.user
    if not student_id: return jsonify({"error":"student_id required"}), 400
    if status not in ("Present","Absent"): return jsonify({"error":"status must be Present or Absent"}), 400
    with get_db() as conn:
        stu = qone(conn, "SELECT student_id FROM students WHERE student_id=%s AND department=(SELECT name FROM faculties WHERE id=%s) AND semester=%s",
                   (student_id,t["faculty_id"],str(t["semester"])))
        if not stu: return jsonify({"error":"Student not in your class"}), 403
        key = (student_id,att_date,t["subject_id"])
        ts  = time.time()
        for k,v in list(_recent_marks.items()):
            if ts-v > _RECENT_MARK_TTL: _recent_marks.pop(k,None)
        if key not in _recent_marks:
            qexec(conn, """
                INSERT INTO attendance (student_id,teacher_id,subject_id,date,time,status,note)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (student_id,date,subject_id) DO UPDATE
                    SET status=EXCLUDED.status,time=EXCLUDED.time,note=EXCLUDED.note,teacher_id=EXCLUDED.teacher_id
            """, (student_id,t["id"],t["subject_id"],att_date,att_time,status,note))
            _recent_marks[key] = ts
    return jsonify({"marked":True,"student_id":student_id,"status":status})

@app.route("/api/teacher/recognize", methods=["POST"])
@require_auth(roles=["teacher"])
def teacher_recognize():
    """
    Teacher face recognition endpoint.

    CHANGED from v3.1:
      Detection:  fa.get(rgb)                → detect_largest_face(frame_bgr)
      Embedding:  face.normed_embedding       → _get_embedding_from_crop(face.crop_bgr)
      Confidence: InsightFace score           → YOLO conf + ArcFace sim both logged
    """
    data    = request.json or {}
    img_b64 = data.get("image")
    if not img_b64:
        return jsonify({"error":"No image"}), 400

    teacher = g.user
    frame   = decode_image(img_b64)
    if frame is None:
        return jsonify({"error":"Cannot decode image"}), 400

    # ── YOLO detection ────────────────────────────────────────────────────
    yolo_face = detect_largest_face(frame)
    if yolo_face is None:
        log.debug("teacher_recognize: no face detected by YOLOv8")
        return jsonify({"recognized":False,"message":"No face detected"})

    yolo_conf = yolo_face.confidence
    bbox      = yolo_face.bbox

    # ── ArcFace embedding ─────────────────────────────────────────────────
    emb = _get_embedding_from_crop(yolo_face.crop_bgr)
    if emb is None:
        log.warning("teacher_recognize: embedding extraction failed")
        return jsonify({"recognized":False,"message":"Embedding extraction failed"})

    # ── Similarity search ─────────────────────────────────────────────────
    with get_db() as conn:
        sid, name, sim = find_best_match(conn, emb, teacher["faculty_id"], teacher["semester"])

    if sim >= THRESHOLD and sid:
        today = date.today().isoformat()
        now   = datetime.now().strftime("%H:%M:%S")
        key   = (sid, today, teacher["subject_id"])
        ts    = time.time()
        for k,v in list(_recent_marks.items()):
            if ts-v > _RECENT_MARK_TTL: _recent_marks.pop(k,None)
        recently = key in _recent_marks

        with get_db() as conn:
            with conn.cursor() as c:
                marked = False
                if not recently:
                    c.execute("""
                        INSERT INTO attendance (student_id,teacher_id,subject_id,date,time,status)
                        VALUES (%s,%s,%s,%s,%s,'Present')
                        ON CONFLICT (student_id,date,subject_id) DO NOTHING
                    """, (sid,teacher["id"],teacher["subject_id"],today,now))
                    marked = c.rowcount == 1
                    if marked:
                        _recent_marks[key] = ts
                c.execute("""
                    INSERT INTO recognition_logs
                        (student_id,full_name,confidence,recognized,teacher_id,subject_id,detection_engine)
                    VALUES (%s,%s,%s,true,%s,%s,'yolov8')
                """, (sid,name,round(sim*100,1),teacher["id"],teacher["subject_id"]))

        if marked:
            sse_broadcast({
                "type":"attendance","student_id":sid,"name":name,
                "confidence":round(sim*100,1),"time":now,"date":today,
                "subject":teacher.get("subject_name"),"teacher":teacher.get("full_name"),
                "detection_engine":"yolov8",
            })

        return jsonify({
            "recognized":        True,
            "student_id":        sid,
            "name":              name,
            "confidence":        round(sim*100, 1),     # ArcFace similarity
            "yolo_confidence":   round(yolo_conf*100, 1), # YOLO detection confidence
            "bbox":              bbox,
            "attendance_marked": marked,
            "detection_engine":  "yolov8",
        })

    _log_recognition(None,"Unknown",sim,False,teacher["id"],teacher["subject_id"])
    return jsonify({
        "recognized":      False,
        "name":            "Unknown",
        "confidence":      round(sim*100, 1),
        "yolo_confidence": round(yolo_conf*100, 1),
        "bbox":            bbox,
        "detection_engine":"yolov8",
    })

@app.route("/api/teacher/attendance/logs")
@require_auth(roles=["teacher"])
def teacher_attendance_logs():
    t    = g.user
    days = int(request.args.get("days",30))
    with get_db() as conn:
        rows = qall(conn, """
            SELECT a.student_id,s.full_name,a.date::text,a.time::text,a.status,a.note
            FROM attendance a JOIN students s ON s.student_id=a.student_id
            WHERE a.teacher_id=%s AND a.subject_id=%s
              AND a.date >= CURRENT_DATE - (%s * INTERVAL '1 day')
            ORDER BY a.date DESC,s.full_name
        """, (t["id"],t["subject_id"],days))
    return jsonify({"logs":rows})

@app.route("/api/teacher/change-password", methods=["POST"])
@require_auth(roles=["teacher"])
def teacher_change_password():
    d      = request.json or {}
    old_pw = d.get("old_password","")
    new_pw = d.get("new_password","")
    if not new_pw or len(new_pw) < 6:
        return jsonify({"error":"New password must be at least 6 characters"}), 400
    with get_db() as conn:
        if not qone(conn, "SELECT id FROM teachers WHERE id=%s AND password_hash=%s", (g.user_id,_hash(old_pw))):
            return jsonify({"error":"Current password incorrect"}), 401
        qexec(conn, "UPDATE teachers SET password_hash=%s WHERE id=%s", (_hash(new_pw),g.user_id))
    return jsonify({"updated":True})


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — STUDENT PANEL
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/api/student/profile")
@require_auth(roles=["student"])
def student_profile():
    with get_db() as conn:
        s = qone(conn, "SELECT student_id,full_name,department,email,phone,semester,status,enrolled_at::text FROM students WHERE student_id=%s", (g.student_id,))
    if not s: return jsonify({"error":"Not found"}), 404
    return jsonify(s)

@app.route("/api/student/attendance")
@require_auth(roles=["student"])
def student_attendance():
    sid = g.student_id
    with get_db() as conn:
        td      = total_attendance_days(conn)
        stats   = qone(conn, """
            SELECT COUNT(DISTINCT date) FILTER(WHERE status='Present') AS total_present,
                   %s::int AS total_days,
                   ROUND(100.0*COUNT(DISTINCT date) FILTER(WHERE status='Present')/NULLIF(%s::int,0),1) AS percentage
            FROM attendance WHERE student_id=%s
        """, (td,td,sid))
        records = qall(conn, """
            SELECT a.date::text,a.time::text,a.status,a.note,
                   sub.name AS subject_name,t.full_name AS teacher_name,ts.label AS time_slot_label
            FROM attendance a
            LEFT JOIN subjects sub ON sub.id=a.subject_id
            LEFT JOIN teachers t ON t.id=a.teacher_id
            LEFT JOIN time_slots ts ON ts.id=t.time_slot_id
            WHERE a.student_id=%s ORDER BY a.date DESC,a.time DESC LIMIT 90
        """, (sid,))
        monthly = qall(conn, """
            SELECT TO_CHAR(date,'Mon YYYY') AS month, DATE_TRUNC('month',date) AS month_sort,
                   COUNT(*) FILTER(WHERE status='Present') AS present
            FROM attendance WHERE student_id=%s AND date>=NOW()-INTERVAL '6 months'
            GROUP BY month,month_sort ORDER BY month_sort
        """, (sid,))
    return jsonify({"student_id":sid,"stats":stats,"records":records,"monthly":monthly})


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — STUDENTS (admin)
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/api/students")
def list_students():
    q    = request.args.get("q","").strip()
    dept = request.args.get("department","").strip()
    sem  = request.args.get("semester","").strip()
    stat = request.args.get("status","").strip()
    sql  = "SELECT student_id,full_name,department,email,phone,semester,status,sample_count,enrolled_at::text FROM students"
    params, where = [], []
    if q:
        where.append("(full_name ILIKE %s OR student_id ILIKE %s)"); params += [f"%{q}%",f"%{q}%"]
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
        s = qone(conn, "SELECT student_id,full_name,department,email,phone,semester,status,sample_count,enrolled_at::text FROM students WHERE student_id=%s", (sid,))
        if not s: return jsonify({"error":"Not found"}), 404
        td    = total_attendance_days(conn)
        stats = qone(conn, """
            SELECT COUNT(DISTINCT date) FILTER(WHERE status='Present') AS total_present,
                   %s::int AS total_days,
                   ROUND(100.0*COUNT(DISTINCT date) FILTER(WHERE status='Present')/NULLIF(%s::int,0),1) AS percentage
            FROM attendance WHERE student_id=%s
        """, (td,td,sid))
        monthly = qall(conn, """
            SELECT TO_CHAR(date,'Mon YYYY') AS month, DATE_TRUNC('month',date) AS month_sort,
                   COUNT(*) FILTER(WHERE status='Present') AS present
            FROM attendance WHERE student_id=%s AND date>=NOW()-INTERVAL '6 months'
            GROUP BY month,month_sort ORDER BY month_sort
        """, (sid,))
        logs = qall(conn, "SELECT logged_at::text,confidence,recognized,detection_engine FROM recognition_logs WHERE student_id=%s ORDER BY logged_at DESC LIMIT 20", (sid,))
        att  = qall(conn, """
            SELECT a.date::text,a.time::text,a.status,a.note,sub.name AS subject_name,t.full_name AS teacher_name
            FROM attendance a
            LEFT JOIN subjects sub ON sub.id=a.subject_id
            LEFT JOIN teachers t ON t.id=a.teacher_id
            WHERE a.student_id=%s ORDER BY a.date DESC LIMIT 60
        """, (sid,))
    return jsonify({**s,"stats":stats,"monthly":monthly,"logs":logs,"attendance":att})

@app.route("/api/students/<sid>/photo")
def student_photo(sid):
    with get_db() as conn:
        row = qone(conn, "SELECT face_image FROM students WHERE student_id=%s", (sid,))
    if not row or not row["face_image"]: return "",404
    return Response(bytes(row["face_image"]), mimetype="image/jpeg")

@app.route("/api/students/<sid>", methods=["PUT"])
@require_admin
def update_student(sid):
    d      = request.json or {}
    ALLOWED = {"full_name","department","email","phone","semester","status"}
    fields  = {k:d[k] for k in ALLOWED if k in d}
    if not fields: return jsonify({"error":"Nothing to update"}), 400
    if "email" in fields and fields["email"]:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", fields["email"]):
            return jsonify({"error":"Invalid email format"}), 400
    if "status" in fields and fields["status"] not in ("active","inactive","graduated","suspended"):
        return jsonify({"error":"Invalid status"}), 400
    sql = "UPDATE students SET " + ", ".join(f"{k}=%s" for k in fields) + " WHERE student_id=%s"
    with get_db() as conn:
        rows = qexec(conn, sql, list(fields.values())+[sid])
    if rows == 0: return jsonify({"error":"Not found"}), 404
    return jsonify({"updated":True,"fields":list(fields.keys())})

@app.route("/api/students/<sid>", methods=["DELETE"])
@require_admin
def delete_student(sid):
    with get_db() as conn:
        rows = qexec(conn, "DELETE FROM students WHERE student_id=%s", (sid,))
    return jsonify({"deleted":rows>0})

@app.route("/api/departments")
def departments():
    with get_db() as conn:
        rows = qall(conn, "SELECT DISTINCT department FROM students WHERE department IS NOT NULL ORDER BY department")
    return jsonify({"departments":[r["department"] for r in rows]})

@app.route("/api/attendance/<sid>/<att_date>", methods=["PUT"])
@require_admin
def update_attendance(sid, att_date):
    d      = request.json or {}
    status = d.get("status","")
    note   = d.get("note","")
    if status not in ("Present","Absent"): return jsonify({"error":"status must be Present or Absent"}), 400
    with get_db() as conn:
        qexec(conn, """
            INSERT INTO attendance (student_id,date,time,status,note)
            VALUES (%s,%s,CURRENT_TIME,%s,%s)
            ON CONFLICT (student_id,date,subject_id) DO UPDATE SET status=%s,note=%s
        """, (sid,att_date,status,note,status,note))
    return jsonify({"updated":True})

@app.route("/api/attendance/<sid>/<att_date>", methods=["DELETE"])
@require_admin
def delete_attendance(sid, att_date):
    with get_db() as conn:
        rows = qexec(conn, "DELETE FROM attendance WHERE student_id=%s AND date=%s", (sid,att_date))
    return jsonify({"deleted":rows>0})

@app.route("/api/activity-logs")
@require_admin
def get_activity_logs():
    limit  = int(request.args.get("limit",50))
    target = request.args.get("target_id","").strip()
    with get_db() as conn:
        if target:
            rows = qall(conn, "SELECT admin_user,action,target_type,target_id,detail,logged_at::text FROM activity_logs WHERE target_id=%s ORDER BY logged_at DESC LIMIT %s", (target,limit))
        else:
            rows = qall(conn, "SELECT admin_user,action,target_type,target_id,detail,logged_at::text FROM activity_logs ORDER BY logged_at DESC LIMIT %s", (limit,))
    return jsonify({"logs":rows})


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — FRAME VALIDATION (used by guided enrollment capture)
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/api/capture/validate-frame", methods=["POST"])
def validate_frame():
    """
    Real-time frame quality check for guided enrollment.

    CHANGED from v3.1:
      Detection:   fa.get(rgb)                → detect_largest_face(frame_bgr)
      Pose:        InsightFace kps landmarks   → YOLOFaceDetector.estimate_pose()
      Quality:     score_frame_quality(frame)  → score_frame_quality_yolo(frame, face)

    Response shape: UNCHANGED (face_detected, bbox, quality, pose)
    """
    d       = request.json or {}
    img_b64 = d.get("image")
    if not img_b64:
        return jsonify({"error":"No image"}), 400

    frame = decode_image(img_b64)
    if frame is None:
        return jsonify({"error":"Cannot decode"}), 400

    # ── YOLO detection ────────────────────────────────────────────────────
    yolo_face = detect_largest_face(frame)

    if yolo_face is None:
        return jsonify({"face_detected":False,"quality":None,"pose":None})

    quality   = score_frame_quality_yolo(frame, yolo_face)
    detector  = get_yolo_detector()
    pose_hint = detector.estimate_pose(yolo_face, frame.shape)

    return jsonify({
        "face_detected":    True,
        "bbox":             yolo_face.bbox,
        "quality":          quality,
        "pose":             pose_hint,
        "yolo_confidence":  round(yolo_face.confidence*100, 1),
        "detection_engine": "yolov8",
    })


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — ENROLLMENT
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/api/enroll", methods=["POST"])
def enroll():
    """
    Student enrollment.

    CHANGED from v3.1:
      extract_embeddings() now uses YOLOv8 for detection and ArcFace for embedding.
      All DB writes, duplicate detection, and response shape are unchanged.
    """
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
        frames_b64 = [base64.b64encode(f.read()).decode() for f in request.files.getlist("images")]

    if not student_id or not full_name:
        return jsonify({"error":"student_id and full_name required"}), 400
    if not frames_b64:
        return jsonify({"error":"No images provided"}), 400

    # Duplicate face check (test first 3 frames)
    existing_sid = None
    test_emb, _ = extract_embeddings(frames_b64[:3])
    if test_emb is not None:
        with get_db() as conn:
            existing_sid, existing_name, sim = find_best_match(conn, test_emb)
        if sim > 0.92 and existing_sid and existing_sid != student_id:
            return jsonify({"error":f"This face already belongs to {existing_name} ({existing_sid}). Similarity: {sim*100:.1f}%"}), 409

    # Full embedding extraction (all frames)
    mean_emb, thumbnail = extract_embeddings(frames_b64)
    if mean_emb is None:
        return jsonify({"error":"No faces detected in any image. Ensure good lighting and face visibility."}), 422

    if PGVECTOR_AVAILABLE:
        emb_value = mean_emb.tolist(); emb_sql = "%s::vector"
    else:
        emb_value = json.dumps(mean_emb.tolist()); emb_sql = "%s"

    with get_db() as conn:
        qexec(conn, f"""
            INSERT INTO students (student_id,full_name,department,email,phone,semester,embedding,face_image,sample_count)
            VALUES (%s,%s,%s,%s,%s,%s,{emb_sql},%s,%s)
            ON CONFLICT (student_id) DO UPDATE SET
                full_name=EXCLUDED.full_name, department=EXCLUDED.department,
                email=EXCLUDED.email, phone=EXCLUDED.phone, semester=EXCLUDED.semester,
                embedding=EXCLUDED.embedding, face_image=EXCLUDED.face_image,
                sample_count=EXCLUDED.sample_count
        """, (student_id,full_name,department,email,phone,semester,emb_value,thumbnail,len(frames_b64)))

    return jsonify({
        "enrolled":          True,
        "student_id":        student_id,
        "samples":           len(frames_b64),
        "is_update":         bool(existing_sid==student_id if test_emb is not None else False),
        "detection_engine":  "yolov8",
    })


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — CAMERA
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/api/camera/start", methods=["POST"])
def start_camera():
    if camera_state["active"]:
        return jsonify({"status":"already_running"})
    d = request.get_json(silent=True) or {}
    camera_state["teacher_id"] = d.get("teacher_id")
    camera_state["subject_id"] = d.get("subject_id")
    camera_state["active"]     = True
    return jsonify({"status":"started","detection_engine":"yolov8"})

@app.route("/api/camera/stop", methods=["POST"])
def stop_camera():
    camera_state["active"] = False
    if camera_state["cap"]:
        camera_state["cap"].release()
    camera_state["teacher_id"] = None
    camera_state["subject_id"] = None
    return jsonify({"status":"stopped"})

@app.route("/api/stream")
def stream():
    if not camera_state["active"]:
        return jsonify({"error":"Camera not started"}), 400
    return Response(_gen_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/api/events")
def sse_events():
    q = queue.Queue(maxsize=50)
    with _sse_lock:
        _sse_clients.append(q)
    def generate():
        try:
            while True:
                try:
                    yield q.get(timeout=20)
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            with _sse_lock:
                if q in _sse_clients:
                    _sse_clients.remove(q)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — ATTENDANCE (admin read/export)
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/api/attendance")
def get_attendance():
    target = request.args.get("date", date.today().isoformat())
    dept   = request.args.get("department","").strip()
    sql    = "SELECT s.student_id,s.full_name,s.department,a.date::text,a.time::text,a.status,a.note FROM students s LEFT JOIN attendance a ON a.student_id=s.student_id AND a.date=%s"
    params = [target]
    if dept: sql += " WHERE s.department=%s"; params.append(dept)
    sql += " ORDER BY s.full_name"
    with get_db() as conn:
        rows = qall(conn, sql, params)
    records = [{"student_id":r["student_id"],"name":r["full_name"],"department":r["department"],"date":r["date"] or target,"time":r["time"] or "—","status":r["status"] or "Absent","note":r["note"] or ""} for r in rows]
    return jsonify({"date":target,"records":records,"present":sum(1 for r in records if r["status"]=="Present"),"absent":sum(1 for r in records if r["status"]=="Absent")})

@app.route("/api/attendance/faculty-summary")
def faculty_summary():
    target = request.args.get("date", date.today().isoformat())
    with get_db() as conn:
        rows = qall(conn, """
            SELECT s.student_id,s.full_name,COALESCE(s.department,'Unassigned') AS department,a.time::text AS time,COALESCE(a.status,'Absent') AS status
            FROM students s
            LEFT JOIN LATERAL (SELECT status,time FROM attendance a2 WHERE a2.student_id=s.student_id AND a2.date=%s ORDER BY a2.time DESC LIMIT 1) a ON true
            ORDER BY s.department,s.full_name
        """, (target,))
    faculty_map = {}
    for r in rows:
        dept = r["department"]
        faculty_map.setdefault(dept,[]).append({"student_id":r["student_id"],"name":r["full_name"],"time":r["time"] or "—","status":r["status"]})
    faculties = []
    for dept_name in sorted(faculty_map):
        students = faculty_map[dept_name]
        present  = sum(1 for s in students if s["status"]=="Present")
        total    = len(students)
        faculties.append({"name":dept_name,"total":total,"present":present,"absent":total-present,"rate":round(present/total*100,1) if total else 0,"students":students})
    ot = sum(f["total"] for f in faculties)
    op = sum(f["present"] for f in faculties)
    return jsonify({"date":target,"faculties":faculties,"overall":{"total":ot,"present":op,"absent":ot-op,"rate":round(op/ot*100,1) if ot else 0}})

@app.route("/api/attendance/history")
def attendance_history():
    dept = request.args.get("department","").strip()
    with get_db() as conn:
        if dept:
            rows  = qall(conn, "SELECT a.date::text,COUNT(*) FILTER(WHERE a.status='Present') AS present FROM attendance a JOIN students s ON s.student_id=a.student_id WHERE s.department=%s GROUP BY a.date ORDER BY a.date DESC LIMIT 30", (dept,))
            total = qone(conn, "SELECT COUNT(*) AS n FROM students WHERE department=%s", (dept,))["n"]
        else:
            rows  = qall(conn, "SELECT date::text,COUNT(*) FILTER(WHERE status='Present') AS present FROM attendance GROUP BY date ORDER BY date DESC LIMIT 30")
            total = qone(conn, "SELECT COUNT(*) AS n FROM students")["n"]
    return jsonify({"history":[{"date":r["date"],"present":r["present"],"absent":max(0,total-r["present"])} for r in rows]})

@app.route("/api/attendance/stats")
def attendance_stats():
    dept = request.args.get("department","").strip()
    with get_db() as conn:
        td  = total_attendance_days(conn, dept or None)
        sql = "SELECT s.student_id,s.full_name,s.department,COUNT(DISTINCT a.date) FILTER(WHERE a.status='Present') AS present_days,%s::int AS total_days,ROUND(100.0*COUNT(DISTINCT a.date) FILTER(WHERE a.status='Present')/NULLIF(%s::int,0),1) AS pct FROM students s LEFT JOIN attendance a ON a.student_id=s.student_id"
        params = [td,td]
        if dept: sql += " WHERE s.department=%s"; params.append(dept)
        sql += " GROUP BY s.student_id,s.full_name,s.department ORDER BY pct DESC NULLS LAST"
        rows = qall(conn, sql, params)
    return jsonify({"stats":rows})

@app.route("/api/attendance/export")
def export_csv():
    from_d = request.args.get("from", date.today().isoformat())
    to_d   = request.args.get("to",   date.today().isoformat())
    dept   = request.args.get("department","").strip()
    with get_db() as conn:
        sql    = "SELECT s.student_id,s.full_name,s.department,a.date::text,a.time::text,a.status,a.note FROM students s LEFT JOIN attendance a ON a.student_id=s.student_id AND a.date BETWEEN %s AND %s"
        params = [from_d,to_d]
        if dept: sql += " WHERE s.department=%s"; params.append(dept)
        sql += " ORDER BY s.department,a.date,s.full_name"
        rows = qall(conn, sql, params)
    lines = ["Student ID,Name,Department,Date,Time,Status,Note"]
    lines += [f'{r["student_id"]},{r["full_name"]},{r["department"] or ""},{r["date"] or ""},{r["time"] or "—"},{r["status"] or "Absent"},{r["note"] or ""}' for r in rows]
    fname = f"attendance_all_{from_d}_{to_d}.csv"
    return Response("\n".join(lines), mimetype="text/csv", headers={"Content-Disposition":f"attachment; filename={fname}"})

@app.route("/api/logs")
def get_logs():
    limit = int(request.args.get("limit",50))
    with get_db() as conn:
        rows = qall(conn, "SELECT student_id,full_name,confidence,recognized,detection_engine,logged_at::text FROM recognition_logs ORDER BY logged_at DESC LIMIT %s", (limit,))
    return jsonify({"logs":rows})


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — SETTINGS
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/api/settings")
def get_settings():
    detector_info = get_yolo_detector().model_info
    return jsonify({
        "recognition_threshold": THRESHOLD,
        "frame_skip":            SKIP,
        "email_enabled":         bool(os.getenv("BREVO_API_KEY") and os.getenv("BREVO_FROM")),
        "detection_engine":      "yolov8",
        "yolo_model":            detector_info.get("model","—"),
        "yolo_device":           detector_info.get("device","cpu"),
        "yolo_conf_threshold":   detector_info.get("conf_thresh",0.45),
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

@app.route("/api/email/test", methods=["POST"])
@require_admin
def test_email():
    d  = request.json or {}
    to = d.get("email","").strip()
    if not to: return jsonify({"error":"email required"}), 400
    if not (os.getenv("BREVO_API_KEY") and os.getenv("BREVO_FROM")):
        return jsonify({"error":"Email not configured"}), 503
    queue_attendance_email("TEST","Test Student","Test Dept",date.today().isoformat(),datetime.now().strftime("%H:%M:%S"),to)
    return jsonify({"queued":True})

@app.route("/api/email/logs")
@require_admin
def email_logs():
    with get_db() as conn:
        rows = qall(conn, "SELECT student_id,email_to,subject,sent_at::text,success,error_msg FROM email_log ORDER BY sent_at DESC LIMIT 50")
    return jsonify({"logs":rows})


# ═════════════════════════════════════════════════════════════════════════════
#  BOOT
# ═════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    log.info("=== वेदनेत्रम् Smart Attendance v3.2-YOLOv8 starting ===")
    init_db()

    # Warm up DB pool
    try:
        with get_db() as conn:
            qone(conn, "SELECT 1")
        log.info("DB pool warmed up")
    except Exception as exc:
        log.error("DB pool warm-up failed: %s", exc)

    # Pre-load models in background so first request is fast
    def _preload():
        log.info("Pre-loading YOLOv8 detector …")
        det = get_yolo_detector()
        if det.is_available:
            log.info("YOLOv8 ready: %s", det.model_info)
        else:
            log.warning("YOLOv8 not available — check ultralytics install and weights")
        log.info("Pre-loading ArcFace recognizer …")
        arc = get_arcface_model()
        if arc:
            log.info("ArcFace ready")
        else:
            log.warning("ArcFace not available — recognition will fail")

    threading.Thread(target=_preload, daemon=True).start()

    # Email worker
    _email_thread = threading.Thread(target=_email_worker, daemon=True)
    _email_thread.start()
    log.info("Email worker started. Listening on 0.0.0.0:5050 …")
    app.run(debug=True, host="0.0.0.0", port=5050, threaded=True)
