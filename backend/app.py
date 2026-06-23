import os, cv2, json, base64, hashlib, hmac, secrets, threading, queue, time, re, csv, io
import urllib.request, urllib.error
import numpy as np
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, request, jsonify, Response, g
from flask_cors import CORS
from dotenv import load_dotenv
import psycopg2, psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool
from pgvector.psycopg2 import register_vector

load_dotenv()
app = Flask(__name__)
CORS(app, supports_credentials=True)

# ── Config ────────────────────────────────────────────────────────────────
PG_DSN    = os.getenv("DATABASE_URL", "postgresql://frs_user:frs123@localhost:5432/frs")
THRESHOLD = float(os.getenv("RECOGNITION_THRESHOLD", "0.80"))
SKIP      = int(os.getenv("FRAME_SKIP", "2"))
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))  # QR HMAC signing key

# Email config — uses BREVO API (more reliable than raw SMTP)
# Sign up free at brevo.com, verify a sender address, generate an API key.
# Free tier: 100 emails/day indefinitely.
BREVO_API_KEY  = os.getenv("BREVO_API_KEY", "")
BREVO_FROM     = os.getenv("BREVO_FROM", "")
EMAIL_ENABLED  = bool(BREVO_API_KEY and BREVO_FROM)

BREVO_HEADERS = {
    "accept": "application/json",
    "api-key": BREVO_API_KEY,
    "content-type": "application/json"
}

# ── DB ────────────────────────────────────────────────────────────────────
_pool: ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()

def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadedConnectionPool(minconn=2, maxconn=20, dsn=PG_DSN)
    return _pool

@contextmanager
def get_db(register_pgvector=True):
    pool = _get_pool()
    conn = pool.getconn()
    if register_pgvector:
        register_vector(conn)
    try:
        yield conn
        conn.commit()
    except:
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

def total_attendance_days(conn, dept=None):
    if dept:
        row = qone(conn, """
            SELECT COUNT(DISTINCT a.date) AS total_days
            FROM attendance a
            JOIN students s ON s.student_id = a.student_id
            WHERE s.department = %s
        """, (dept,))
    else:
        row = qone(conn, "SELECT COUNT(DISTINCT date) AS total_days FROM attendance")
    return int(row["total_days"] or 0) if row else 0

def init_db():
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        with conn.cursor() as c:
            c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            SERIAL PRIMARY KEY,
                username      TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'admin'
            );

            CREATE TABLE IF NOT EXISTS students (
                id           SERIAL  PRIMARY KEY,
                student_id   TEXT    NOT NULL UNIQUE,
                full_name    TEXT    NOT NULL,
                department   TEXT,
                email        TEXT,
                phone        TEXT,
                semester     TEXT,
                status       TEXT    NOT NULL DEFAULT 'active',
                face_image   BYTEA,
                embedding    vector(512),
                sample_count INTEGER NOT NULL DEFAULT 0,
                enrolled_at  TIMESTAMPTZ DEFAULT NOW()
            );

            -- Non-destructive migrations for existing DBs
            ALTER TABLE students ADD COLUMN IF NOT EXISTS semester     TEXT;
            ALTER TABLE students ADD COLUMN IF NOT EXISTS status       TEXT NOT NULL DEFAULT 'active';
            ALTER TABLE students ADD COLUMN IF NOT EXISTS face_image   BYTEA;
            ALTER TABLE students ADD COLUMN IF NOT EXISTS embedding    vector(512);
            ALTER TABLE students ADD COLUMN IF NOT EXISTS sample_count INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE students ADD COLUMN IF NOT EXISTS faculty_id   INTEGER REFERENCES faculties(id);

            -- Normalise department to faculty short code (e.g. "CSIT", "BCA", "BBM")
            UPDATE students SET department = f.code
            FROM   faculties f
            WHERE  students.faculty_id = f.id
              AND  students.department IS DISTINCT FROM f.code;

            CREATE TABLE IF NOT EXISTS attendance (
                id         SERIAL PRIMARY KEY,
                student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
                date       DATE NOT NULL DEFAULT CURRENT_DATE,
                time       TIME NOT NULL DEFAULT CURRENT_TIME,
                status     TEXT NOT NULL DEFAULT 'Present',
                note       TEXT
            );

            ALTER TABLE attendance ADD COLUMN IF NOT EXISTS note       TEXT;
            ALTER TABLE attendance ADD COLUMN IF NOT EXISTS subject_id INTEGER REFERENCES subjects(id);
            ALTER TABLE attendance ADD COLUMN IF NOT EXISTS teacher_id INTEGER REFERENCES teachers(id);
            ALTER TABLE attendance ADD COLUMN IF NOT EXISTS session_id INTEGER;

            CREATE TABLE IF NOT EXISTS recognition_logs (
                id         SERIAL PRIMARY KEY,
                student_id TEXT,
                full_name  TEXT,
                confidence REAL,
                recognized BOOLEAN NOT NULL,
                logged_at  TIMESTAMPTZ DEFAULT NOW()
            );

            -- Admin activity log: tracks every create/update/delete
            CREATE TABLE IF NOT EXISTS activity_logs (
                id          SERIAL PRIMARY KEY,
                admin_user  TEXT NOT NULL,
                action      TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id   TEXT,
                detail      TEXT,
                logged_at   TIMESTAMPTZ DEFAULT NOW()
            );

            -- Email send log: prevents duplicate emails
            CREATE TABLE IF NOT EXISTS email_log (
                id          SERIAL PRIMARY KEY,
                student_id  TEXT NOT NULL,
                email_to    TEXT NOT NULL,
                subject     TEXT,
                sent_at     TIMESTAMPTZ DEFAULT NOW(),
                success     BOOLEAN NOT NULL DEFAULT true,
                error_msg   TEXT
            );

            -- Subjects table
            CREATE TABLE IF NOT EXISTS subjects (
                id         SERIAL PRIMARY KEY,
                name       TEXT NOT NULL,
                code       TEXT NOT NULL UNIQUE,
                faculty    TEXT,
                semester   INTEGER,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            -- Settings persisted to DB (survives restarts)
            CREATE TABLE IF NOT EXISTS system_settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );

            -- Academic calendar
            CREATE TABLE IF NOT EXISTS academic_years (
                id         SERIAL PRIMARY KEY,
                name       TEXT NOT NULL,
                start_date DATE NOT NULL,
                end_date   DATE NOT NULL,
                is_current BOOLEAN NOT NULL DEFAULT false
            );

            CREATE TABLE IF NOT EXISTS holidays (
                id               SERIAL PRIMARY KEY,
                date             DATE NOT NULL,
                name             TEXT NOT NULL,
                academic_year_id INTEGER REFERENCES academic_years(id) ON DELETE CASCADE,
                UNIQUE (date, academic_year_id)
            );

            -- Attendance audit trail (before/after for every manual change)
            CREATE TABLE IF NOT EXISTS attendance_history (
                id            SERIAL PRIMARY KEY,
                attendance_id INTEGER,
                student_id    TEXT,
                subject_id    INTEGER REFERENCES subjects(id),
                date          DATE,
                old_status    TEXT,
                new_status    TEXT,
                changed_by    TEXT NOT NULL,
                reason        TEXT,
                changed_at    TIMESTAMPTZ DEFAULT NOW()
            );

            -- Attendance correction requests from students
            CREATE TABLE IF NOT EXISTS correction_requests (
                id           SERIAL PRIMARY KEY,
                student_id   TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
                subject_id   INTEGER REFERENCES subjects(id),
                date         DATE NOT NULL,
                reason       TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending',
                reviewed_by  INTEGER REFERENCES teachers(id),
                review_note  TEXT,
                reviewed_at  TIMESTAMPTZ,
                created_at   TIMESTAMPTZ DEFAULT NOW()
            );

            -- Automated attendance warning log
            CREATE TABLE IF NOT EXISTS attendance_warnings (
                id         SERIAL PRIMARY KEY,
                student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
                subject_id INTEGER REFERENCES subjects(id),
                percentage NUMERIC(5,2) NOT NULL,
                threshold  NUMERIC(5,2) NOT NULL DEFAULT 75.00,
                sent_via   TEXT NOT NULL DEFAULT 'email',
                sent_at    TIMESTAMPTZ DEFAULT NOW()
            );

            INSERT INTO users (username, password_hash, role)
            VALUES ('admin',
                    '240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9',
                    'admin')
            ON CONFLICT DO NOTHING;

            INSERT INTO system_settings (key, value) VALUES
                ('recognition_threshold', '0.80'),
                ('frame_skip', '2'),
                ('min_attendance_pct', '75'),
                ('qr_expiry_seconds', '90')
            ON CONFLICT (key) DO NOTHING;
            """)
        # Non-destructive migrations for subjects table (may pre-exist without these columns)
        with conn.cursor() as c:
            for col_sql in [
                "ALTER TABLE subjects ADD COLUMN IF NOT EXISTS faculty    TEXT",
                "ALTER TABLE subjects ADD COLUMN IF NOT EXISTS semester   INTEGER",
                "ALTER TABLE subjects ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW()",
            ]:
                try:
                    c.execute(col_sql)
                except Exception:
                    pass
        # Teacher profile columns — non-destructive migrations
        with conn.cursor() as c:
            for col_sql in [
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name  TEXT",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS email      TEXT",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone      TEXT",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS teacher_id TEXT",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS faculty    TEXT",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS semester   INTEGER",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS subject    TEXT",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS time_slot  TEXT",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS status     TEXT NOT NULL DEFAULT 'active'",
            ]:
                try:
                    c.execute(col_sql)
                except Exception:
                    pass
        # ── Fix per-subject attendance unique constraint ────────────────────
        # Drop the old (student_id, date) constraint that prevented a student
        # from being marked in more than one subject per day.
        with conn.cursor() as c:
            c.execute("""
                ALTER TABLE attendance DROP CONSTRAINT IF EXISTS attendance_student_id_date_key;

                -- Records WITH a subject: unique per student+subject+date
                CREATE UNIQUE INDEX IF NOT EXISTS attendance_student_subject_date_uniq
                    ON attendance(student_id, subject_id, date)
                    WHERE subject_id IS NOT NULL;

                -- Legacy records WITHOUT a subject (daily attendance): unique per student+date
                CREATE UNIQUE INDEX IF NOT EXISTS attendance_student_date_null_uniq
                    ON attendance(student_id, date)
                    WHERE subject_id IS NULL;

                -- Performance indexes
                CREATE INDEX IF NOT EXISTS idx_attendance_date           ON attendance(date);
                CREATE INDEX IF NOT EXISTS idx_attendance_subject        ON attendance(subject_id);
                CREATE INDEX IF NOT EXISTS idx_attendance_session        ON attendance(session_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_teacher_date     ON attendance_sessions(teacher_id, session_date);
                CREATE INDEX IF NOT EXISTS idx_recognition_logged_at     ON recognition_logs(logged_at);
                CREATE INDEX IF NOT EXISTS idx_warnings_student          ON attendance_warnings(student_id);
                CREATE INDEX IF NOT EXISTS idx_corrections_student       ON correction_requests(student_id, status);
            """)

        with conn.cursor() as c:
            c.execute("""
                CREATE INDEX IF NOT EXISTS students_embedding_hnsw
                ON students USING hnsw (embedding vector_cosine_ops)
                WITH (m=16, ef_construction=64);
            """)

        # Leave requests table (student portal)
        with conn.cursor() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS leave_requests (
                    id          SERIAL PRIMARY KEY,
                    student_id  TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
                    from_date   DATE NOT NULL,
                    to_date     DATE NOT NULL,
                    reason      TEXT NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'pending',
                    reviewed_by INTEGER REFERENCES teachers(id),
                    review_note TEXT,
                    reviewed_at TIMESTAMPTZ,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_leave_student ON leave_requests(student_id, status);
            """)
        # Notification read-tracking (adds column if not present)
        with conn.cursor() as c:
            try:
                c.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS notifications_read_at TIMESTAMPTZ")
            except Exception:
                pass
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

def find_best_match(conn, query_emb):
    row = qone(conn, """
        SELECT student_id, full_name,
               1 - (embedding <=> %s::vector) AS similarity
        FROM   students
        WHERE  embedding IS NOT NULL
        ORDER  BY embedding <=> %s::vector
        LIMIT  1
    """, (query_emb.tolist(), query_emb.tolist()))
    if not row: return None, None, 0.0
    return row["student_id"], row["full_name"], float(row["similarity"])

# ── Frame quality scoring ─────────────────────────────────────────────────
def score_frame_quality(frame_bgr, face_bbox=None):
    """
    Returns a quality dict:
      blur_score     : 0-100 (higher = sharper)
      brightness     : 0-100 (50 is ideal)
      face_size_ok   : bool  (face takes up enough of the frame)
      overall        : 0-100 composite score
      passed         : bool  (overall >= 60)
    """
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape

    # Blur: Laplacian variance — low = blurry.
    # Divisor 3 (not 5): real webcam frames have lap_var 50-300.
    # Tilted-down or up frames have lower variance due to forehead/chin
    # dominating the frame — 500/5=100 was too strict for angled poses.
    lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()
    blur_score = min(100, int(lap_var / 3))

    # Brightness: mean pixel value mapped to 0-100
    mean_bright = float(np.mean(gray))
    # 50-200 is good; penalty outside that range
    if 50 <= mean_bright <= 200:
        brightness = int(70 + 30 * (1 - abs(mean_bright - 125) / 75))
    else:
        brightness = max(0, int(40 - abs(mean_bright - 125) / 5))

    # Face size: face area / frame area.
    # 3% threshold (was 5%) — angled poses (up/down) show less face area.
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
        # Lower threshold to 50 (was 60) — angled poses are inherently
        # lower sharpness but are still valid for face embedding generation.
        "passed":       overall >= 50,
    }

# ── Email queue (background thread) ──────────────────────────────────────
# Emails are placed in a queue and sent by a background daemon thread.
# Recognition is never blocked waiting for SMTP.
_email_queue: queue.Queue = queue.Queue(maxsize=200)

def _email_worker():
    """Daemon thread — runs forever, draining the email queue."""
    while True:
        try:
            job = _email_queue.get(timeout=5)
            if job is None: break   # shutdown signal
            _send_email_now(**job)
        except queue.Empty:
            continue
        except Exception as e:
            print(f"[email_worker] Unhandled error: {e}")

def _send_email_now(to_addr, subject, html_body, student_id, retry=0):
    """
    Sends email using Brevo API
    """
    if not EMAIL_ENABLED:
        return

    today = date.today().isoformat()

    # Prevent duplicate emails
    try:
        with get_db() as conn:
            already = qone(conn, """
                SELECT id FROM email_log
                WHERE student_id=%s
                  AND subject=%s
                  AND sent_at::date=%s
                  AND success=true
            """, (student_id, subject, today))

        if already:
            return
    except:
        pass

    payload = json.dumps({
        "sender": {
            "name": "Vedanetram",
            "email": BREVO_FROM
        },
        "to": [
            {
                "email": to_addr
            }
        ],
        "subject": subject,
        "htmlContent": html_body
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=payload,
            method="POST",
            headers=BREVO_HEADERS
        )

        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status

        with get_db() as conn:
            qexec(conn, """
                INSERT INTO email_log
                (student_id, email_to, subject, success)
                VALUES (%s,%s,%s,true)
            """, (student_id, to_addr, subject))

    except Exception as e:
        err = str(e)

        try:
            with get_db() as conn:
                qexec(conn, """
                    INSERT INTO email_log
                    (student_id, email_to, subject, success, error_msg)
                    VALUES (%s,%s,%s,false,%s)
                """, (student_id, to_addr, subject, err))
        except:
            pass

        if retry < 2:
            time.sleep(8 * (retry + 1))
            _send_email_now(
                to_addr,
                subject,
                html_body,
                student_id,
                retry + 1
            )

def queue_attendance_email(student_id, name, dept, att_date, att_time, email_to):
    """
    Build the HTML email and add it to the queue.
    Returns immediately — SMTP happens in background.
    """
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

    subject = f"Attendance Confirmed — {att_date}"
    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"/></head>
<body style="margin:0;padding:0;background:#f4f6f9;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f9;padding:32px 0;">
    <tr><td align="center">
        <table width="560" cellpadding="0" cellspacing="0"
                     style="background:#ffffff;border-radius:10px;overflow:hidden;
                                    box-shadow:0 2px 12px rgba(0,0,0,0.08);">

            <!-- Header -->
            <tr><td style="background:#1B2A4A;padding:28px 36px;">
                <p style="margin:0;font-size:22px;font-weight:700;color:#ffffff;
                                letter-spacing:0.04em;">वेदनेत्रम्</p>
                <p style="margin:4px 0 0;font-size:13px;color:#93C5FD;">
                    Automated Attendance Notification</p>
            </td></tr>

            <!-- Green status bar -->
            <tr><td style="background:#166534;padding:14px 36px;">
                <p style="margin:0;font-size:15px;font-weight:600;color:#DCFCE7;">
                    ✓ &nbsp;Attendance Marked Successfully</p>
            </td></tr>

            <!-- Body -->
            <tr><td style="padding:32px 36px;">
                <p style="margin:0 0 20px;font-size:15px;color:#374151;">
                    Dear <strong>{name}</strong>,</p>
                <p style="margin:0 0 24px;font-size:14px;color:#6B7280;line-height:1.7;">
                    Your attendance has been recorded for today's session.
                    Below are the details of your attendance entry.</p>

                <!-- Details table -->
                <table width="100%" cellpadding="0" cellspacing="0"
                             style="border:1px solid #E5E7EB;border-radius:8px;overflow:hidden;
                                            margin-bottom:24px;">
                    <tr style="background:#F9FAFB;">
                        <td style="padding:12px 16px;font-size:13px;color:#6B7280;
                                             border-bottom:1px solid #E5E7EB;width:40%;">Student ID</td>
                        <td style="padding:12px 16px;font-size:13px;font-weight:600;
                                             color:#111827;border-bottom:1px solid #E5E7EB;">{student_id}</td>
                    </tr>
                    <tr>
                        <td style="padding:12px 16px;font-size:13px;color:#6B7280;
                                             border-bottom:1px solid #E5E7EB;">Department</td>
                        <td style="padding:12px 16px;font-size:13px;font-weight:600;
                                             color:#111827;border-bottom:1px solid #E5E7EB;">{dept or '—'}</td>
                    </tr>
                    <tr style="background:#F9FAFB;">
                        <td style="padding:12px 16px;font-size:13px;color:#6B7280;
                                             border-bottom:1px solid #E5E7EB;">Date</td>
                        <td style="padding:12px 16px;font-size:13px;font-weight:600;
                                             color:#111827;border-bottom:1px solid #E5E7EB;">{att_date}</td>
                    </tr>
                    <tr>
                        <td style="padding:12px 16px;font-size:13px;color:#6B7280;
                                             border-bottom:1px solid #E5E7EB;">Time</td>
                        <td style="padding:12px 16px;font-size:13px;font-weight:600;
                                             color:#111827;border-bottom:1px solid #E5E7EB;">{att_time}</td>
                    </tr>
                    <tr style="background:#F9FAFB;">
                        <td style="padding:12px 16px;font-size:13px;color:#6B7280;">
                            Attendance Rate</td>
                        <td style="padding:12px 16px;font-size:13px;font-weight:700;
                                             color:#166534;">{pct}</td>
                    </tr>
                </table>

                <p style="margin:0 0 8px;font-size:13px;color:#6B7280;line-height:1.6;">
                    This is an automated message from the Face Recognition Attendance System.
                    Please do not reply to this email.</p>
            </td></tr>

            <!-- Footer -->
            <tr><td style="background:#F9FAFB;padding:20px 36px;
                                         border-top:1px solid #E5E7EB;">
                <p style="margin:0;font-size:12px;color:#9CA3AF;">
                    © 2026 Face Recognition Attendance System · Automated Notification</p>
            </td></tr>
        </table>
    </td></tr>
</table>
</body>
</html>"""
    try:
        _email_queue.put_nowait({
            "to_addr":    email_to,
            "subject":    subject,
            "html_body":  html,
            "student_id": student_id,
        })
    except queue.Full:
        pass  # queue full — skip silently, don't slow recognition

def queue_attendance_summary_email(student_id, name, dept, att_date, att_time, status, email_to):
    """Queue a personalized attendance summary email for one student."""
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

    status_label = status if status else "Absent"
    status_color = "#166534" if status_label == "Present" else "#991b1b"
    status_bg = "#DCFCE7" if status_label == "Present" else "#FEE2E2"
    status_text = (
        f"Marked present at {att_time}" if status_label == "Present"
        else "Not marked present today"
    )
    subject = f"Daily Attendance Summary — {att_date}"
    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"/></head>
<body style="margin:0;padding:0;background:#f4f6f9;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f9;padding:32px 0;">
  <tr><td align="center">
    <table width="560" cellpadding="0" cellspacing="0"
           style="background:#ffffff;border-radius:10px;overflow:hidden;
                  box-shadow:0 2px 12px rgba(0,0,0,0.08);">

      <!-- Header -->
      <tr><td style="background:#1B2A4A;padding:28px 36px;">
        <p style="margin:0;font-size:22px;font-weight:700;color:#ffffff;
              letter-spacing:0.04em;">वेदनेत्रम्</p>
        <p style="margin:4px 0 0;font-size:13px;color:#93C5FD;">
          Automated Attendance Notification</p>
      </td></tr>

            <!-- Status bar -->
            <tr><td style="background:{status_color};padding:14px 36px;">
                <p style="margin:0;font-size:15px;font-weight:600;color:{status_bg};">
                    {'✓' if status_label == 'Present' else '○'} &nbsp;Attendance {status_label}</p>
      </td></tr>

      <!-- Body -->
      <tr><td style="padding:32px 36px;">
        <p style="margin:0 0 20px;font-size:15px;color:#374151;">
          Dear <strong>{name}</strong>,</p>
        <p style="margin:0 0 24px;font-size:14px;color:#6B7280;line-height:1.7;">
                    Here is your attendance summary for today's session.</p>

        <!-- Details table -->
        <table width="100%" cellpadding="0" cellspacing="0"
               style="border:1px solid #E5E7EB;border-radius:8px;overflow:hidden;
                      margin-bottom:24px;">
          <tr style="background:#F9FAFB;">
            <td style="padding:12px 16px;font-size:13px;color:#6B7280;
                       border-bottom:1px solid #E5E7EB;width:40%;">Student ID</td>
            <td style="padding:12px 16px;font-size:13px;font-weight:600;
                       color:#111827;border-bottom:1px solid #E5E7EB;">{student_id}</td>
          </tr>
          <tr>
            <td style="padding:12px 16px;font-size:13px;color:#6B7280;
                       border-bottom:1px solid #E5E7EB;">Department</td>
            <td style="padding:12px 16px;font-size:13px;font-weight:600;
                       color:#111827;border-bottom:1px solid #E5E7EB;">{dept or '—'}</td>
          </tr>
          <tr style="background:#F9FAFB;">
            <td style="padding:12px 16px;font-size:13px;color:#6B7280;
                       border-bottom:1px solid #E5E7EB;">Date</td>
            <td style="padding:12px 16px;font-size:13px;font-weight:600;
                       color:#111827;border-bottom:1px solid #E5E7EB;">{att_date}</td>
          </tr>
          <tr>
            <td style="padding:12px 16px;font-size:13px;color:#6B7280;
                       border-bottom:1px solid #E5E7EB;">Time</td>
                        <td style="padding:12px 16px;font-size:13px;font-weight:600;
                                             color:#111827;border-bottom:1px solid #E5E7EB;">{att_time or '—'}</td>
                    </tr>
                    <tr style="background:#F9FAFB;">
                        <td style="padding:12px 16px;font-size:13px;color:#6B7280;
                                             border-bottom:1px solid #E5E7EB;">Status</td>
                        <td style="padding:12px 16px;font-size:13px;font-weight:600;
                                             color:#111827;border-bottom:1px solid #E5E7EB;">{status_label}</td>
                    </tr>
                    <tr>
                        <td style="padding:12px 16px;font-size:13px;color:#6B7280;
                                             border-bottom:1px solid #E5E7EB;">Summary</td>
                        <td style="padding:12px 16px;font-size:13px;font-weight:600;
                                             color:#111827;border-bottom:1px solid #E5E7EB;">{status_text}</td>
          </tr>
          <tr style="background:#F9FAFB;">
            <td style="padding:12px 16px;font-size:13px;color:#6B7280;">
              Attendance Rate</td>
            <td style="padding:12px 16px;font-size:13px;font-weight:700;
                       color:#166534;">{pct}</td>
          </tr>
        </table>

        <p style="margin:0 0 8px;font-size:13px;color:#6B7280;line-height:1.6;">
          This is an automated message from the Face Recognition Attendance System.
          Please do not reply to this email.</p>
      </td></tr>

      <!-- Footer -->
      <tr><td style="background:#F9FAFB;padding:20px 36px;
                     border-top:1px solid #E5E7EB;">
        <p style="margin:0;font-size:12px;color:#9CA3AF;">
          © 2026 Face Recognition Attendance System · Automated Notification</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""
    try:
        _email_queue.put_nowait({
            "to_addr":    email_to,
            "subject":    subject,
            "html_body":  html,
            "student_id": student_id,
        })
    except queue.Full:
        pass  # queue full — skip silently, don't slow recognition

def _send_bulk_attendance_emails(att_date):
    with get_db() as conn:
        total_days = total_attendance_days(conn)
        rows = qall(conn, """
            SELECT s.student_id, s.full_name, s.department, s.email,
                   COALESCE(a.time::text, '') AS time,
                   COALESCE(a.status, 'Absent') AS status
            FROM students s
            LEFT JOIN attendance a
                   ON a.student_id = s.student_id AND a.date = %s
            WHERE s.email IS NOT NULL AND s.email <> ''
            ORDER BY s.full_name
        """, (att_date,))

    queued = 0
    for r in rows:
        queue_attendance_summary_email(
            r["student_id"],
            r["full_name"],
            r["department"],
            att_date,
            r["time"] or "—",
            r["status"],
            r["email"],
        )
        queued += 1
    return {"total_days": total_days, "queued": queued}

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
camera_state = {"active": False, "cap": None}

def _gen_frames():
    fa  = get_face_app()
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    camera_state["cap"] = cap
    frame_n = 0
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
                    sid, name, sim = find_best_match(conn, emb)
                label, color = "Unknown", (0, 60, 220)
                if sim >= THRESHOLD and sid:
                    label, color = name, (0, 200, 80)
                    _mark_attendance_and_broadcast(sid, name, sim)
                else:
                    _log_recognition(None, "Unknown", sim, False)
                cv2.rectangle(frame, (x1,y1), (x2,y2), color, 2)
                cv2.putText(frame, f"{label} ({sim*100:.0f}%)",
                            (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
    cap.release()
    camera_state["cap"] = None

# ── Attendance helpers ────────────────────────────────────────────────────
def _mark_attendance_and_broadcast(student_id, name, confidence):
    today = date.today().isoformat()
    now   = datetime.now().strftime("%H:%M:%S")
    dept  = None
    email = None
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO attendance (student_id, date, time, status)
                VALUES (%s,%s,%s,'Present')
                ON CONFLICT (student_id, date) WHERE subject_id IS NULL DO NOTHING
            """, (student_id, today, now))
            marked = c.rowcount == 1
            c.execute("""
                INSERT INTO recognition_logs (student_id, full_name, confidence, recognized)
                VALUES (%s,%s,%s,true)
            """, (student_id, name, round(confidence*100,1)))
        if marked:
            row = qone(conn,
                "SELECT department, email FROM students WHERE student_id=%s",
                (student_id,))
            if row:
                dept  = row.get("department")
                email = row.get("email")
    if marked:
        sse_broadcast({
            "type":"attendance","student_id":student_id,
            "name":name,"confidence":round(confidence*100,1),
            "time":now,"date":today
        })
        # Queue email asynchronously — does not block recognition
        if email:
            queue_attendance_email(student_id, name, dept, today, now, email)

def _log_recognition(sid, name, confidence, recognized):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO recognition_logs (student_id, full_name, confidence, recognized)
                VALUES (%s,%s,%s,%s)
            """, (sid, name, round(confidence*100,1), recognized))

def _log_activity(admin_user, action, target_type, target_id=None, detail=None):
    """Write an admin action to the activity log table."""
    try:
        with get_db() as conn:
            qexec(conn, """
                INSERT INTO activity_logs (admin_user, action, target_type, target_id, detail)
                VALUES (%s,%s,%s,%s,%s)
            """, (admin_user, action, target_type, target_id, detail))
    except: pass

# ── Auth ──────────────────────────────────────────────────────────────────
def _hash(pw): return hashlib.sha256(pw.encode()).hexdigest()

def require_auth(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = request.headers.get("Authorization","").replace("Bearer ","")
        if not token: return jsonify({"error":"Unauthorized"}), 401
        with get_db() as conn:
            user = qone(conn,
                "SELECT id,username,role FROM users WHERE password_hash=%s",(token,))
            if not user:
                # Authenticated directly from teachers table — id is already teachers.id
                user = qone(conn,
                    "SELECT id, full_name AS username, 'teacher' AS role FROM teachers "
                    "WHERE password_hash=%s AND status='active'",(token,))
            elif user.get("role") == "teacher":
                # Authenticated from users table — resolve the real teachers.id
                t = qone(conn,
                    "SELECT id FROM teachers WHERE password_hash=%s AND status='active'", (token,))
                if t:
                    user["_tid"] = t["id"]
            if not user:
                # Student session token — check sessions table
                sess = qone(conn,
                    """SELECT s.student_id, st.full_name, st.faculty_id, st.semester
                       FROM sessions s
                       JOIN students st ON st.student_id = s.student_id
                       WHERE s.token=%s AND s.user_type='student'
                         AND (s.expires_at IS NULL OR s.expires_at > NOW())""",
                    (token,))
                if sess:
                    user = {
                        "id": 0,
                        "username": sess["student_id"],
                        "role": "student",
                        "student_id": sess["student_id"],
                        "full_name": sess["full_name"],
                        "faculty_id": sess["faculty_id"],
                        "semester": sess["semester"],
                    }
        if not user: return jsonify({"error":"Unauthorized"}), 401
        g.user = user
        return fn(*args, **kwargs)
    return wrapper

def _tid():
    """Return teachers.id for the currently authenticated teacher."""
    return g.user.get("_tid", g.user["id"])

# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════════════════

# ── Health ────────────────────────────────────────────────────────────────
@app.route("/api/health")
def health():
    try:
        with get_db() as conn:
            row = qone(conn,
                "SELECT installed_version FROM pg_available_extensions WHERE name='vector'")
            vec_ok = bool(row and row.get("installed_version"))
        return jsonify({
            "status":"ok","db":"ok",
            "pgvector":"ok" if vec_ok else "missing",
            "email": "enabled" if EMAIL_ENABLED else "disabled",
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({"status":"ok","db":"error","detail":str(e)})

# ── Auth ──────────────────────────────────────────────────────────────────
@app.route("/api/auth/login", methods=["POST"])
def login():
    d = request.json or {}
    password  = d.get("password", "")
    email     = d.get("email", "").strip()
    username  = d.get("username", "").strip()

    # Teacher login — checked against the teachers table by email
    if email:
        with get_db() as conn:
            t = qone(conn,
                "SELECT id, full_name, password_hash FROM teachers "
                "WHERE email=%s AND password_hash=%s AND status='active'",
                (email, _hash(password)))
        if not t: return jsonify({"error": "Invalid email or password"}), 401
        return jsonify({"token": t["password_hash"], "role": "teacher", "username": t["full_name"]})

    # Admin / user login — checked against the users table by username
    with get_db() as conn:
        user = qone(conn,
            "SELECT id,username,role,password_hash FROM users WHERE username=%s AND password_hash=%s",
            (username, _hash(password)))
    if not user: return jsonify({"error": "Invalid credentials"}), 401
    return jsonify({"token": user["password_hash"], "role": user["role"], "username": user["username"]})

@app.route("/api/auth/me")
@require_auth
def me(): return jsonify(g.user)

@app.route("/api/auth/change-password", methods=["POST"])
@require_auth
def change_password():
    d = request.json or {}
    old_pw  = d.get("old_password","")
    new_pw  = d.get("new_password","")
    if not new_pw or len(new_pw) < 6:
        return jsonify({"error":"New password must be at least 6 characters"}), 400
    with get_db() as conn:
        if g.user.get("role") == "teacher":
            tid = _tid()
            teacher = qone(conn,
                "SELECT id FROM teachers WHERE id=%s AND password_hash=%s",
                (tid, _hash(old_pw)))
            if not teacher: return jsonify({"error":"Current password is incorrect"}), 401
            qexec(conn, "UPDATE teachers SET password_hash=%s WHERE id=%s",
                  (_hash(new_pw), tid))
        else:
            user = qone(conn,
                "SELECT id FROM users WHERE id=%s AND password_hash=%s",
                (g.user["id"], _hash(old_pw)))
            if not user: return jsonify({"error":"Current password is incorrect"}), 401
            qexec(conn, "UPDATE users SET password_hash=%s WHERE id=%s",
                  (_hash(new_pw), g.user["id"]))
    _log_activity(g.user.get("username","?"), "change_password", "user", str(g.user["id"]))
    return jsonify({"updated": True})

# ── Student portal login (email-only, no password) ────────────────────────
@app.route("/api/student/login", methods=["POST"])
def student_login():
    import secrets
    d = request.json or {}
    email = (d.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Email is required"}), 400
    with get_db(register_pgvector=False) as conn:
        student = qone(conn,
            """SELECT student_id, full_name, email, department, semester, status, faculty_id
               FROM students WHERE LOWER(email)=%s""",
            (email,))
        if not student:
            return jsonify({"error": "No student found with that email. Please use your registered email."}), 404
        if student.get("status") == "inactive":
            return jsonify({"error": "Your account is inactive. Contact the admin."}), 403
        # Issue a session token stored in the sessions table
        token = secrets.token_hex(32)
        expires = datetime.now() + timedelta(days=7)
        qexec(conn, """
            INSERT INTO sessions (token, user_type, student_id, created_at, expires_at)
            VALUES (%s, 'student', %s, NOW(), %s)
        """, (token, student["student_id"], expires))
    return jsonify({
        "ok": True,
        "token": token,
        "student_id": student["student_id"],
        "full_name": student["full_name"],
        "faculty_id": student.get("faculty_id"),
        "semester": student.get("semester"),
    })

# ── Students ──────────────────────────────────────────────────────────────
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
            SELECT COUNT(*) FILTER(WHERE status='Present') AS total_present,
                   %s::int AS total_days,
                   ROUND(100.0*COUNT(*) FILTER(WHERE status='Present')
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
            FROM recognition_logs
            WHERE student_id=%s ORDER BY logged_at DESC LIMIT 20
        """, (sid,))

        att_records = qall(conn, """
            SELECT a.date::text, a.time::text, a.status, a.note,
                   sb.name AS subject_name, sb.code AS subject_code
            FROM attendance a
            LEFT JOIN subjects sb ON sb.id = a.subject_id
            WHERE a.student_id=%s
            ORDER BY a.date DESC, a.time DESC LIMIT 90
        """, (sid,))

        by_subject = qall(conn, """
            SELECT sb.id AS subject_id, sb.name AS subject_name, sb.code AS subject_code,
                   COUNT(a.id) FILTER(WHERE a.status='Present') AS present,
                   COUNT(a.id) AS total,
                   ROUND(100.0*COUNT(a.id) FILTER(WHERE a.status='Present')
                         /NULLIF(COUNT(a.id),0),1) AS pct
            FROM attendance a
            JOIN subjects sb ON sb.id=a.subject_id
            WHERE a.student_id=%s AND a.date >= NOW()-INTERVAL '6 months'
            GROUP BY sb.id, sb.name, sb.code
            ORDER BY pct ASC NULLS LAST
        """, (sid,))

    return jsonify({
        **s,
        "stats":       stats,
        "monthly":     monthly,
        "logs":        logs,
        "attendance":  att_records,
        "by_subject":  by_subject,
    })

@app.route("/api/students/<sid>/photo")
def student_photo(sid):
    with get_db() as conn:
        row = qone(conn,"SELECT face_image FROM students WHERE student_id=%s",(sid,))
    if not row or not row["face_image"]: return "",404
    return Response(bytes(row["face_image"]),mimetype="image/jpeg")

@app.route("/api/students/<sid>", methods=["PUT"])
@require_auth
def update_student(sid):
    if g.user.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    d = request.json or {}
    ALLOWED = {"full_name","department","email","phone","semester","status"}
    fields  = {k:d[k] for k in ALLOWED if k in d}
    if not fields: return jsonify({"error":"Nothing to update"}), 400

    # Validate email format if provided
    if "email" in fields and fields["email"]:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", fields["email"]):
            return jsonify({"error":"Invalid email format"}), 400

    # Validate status
    if "status" in fields and fields["status"] not in ("active","inactive","graduated","suspended"):
        return jsonify({"error":"Invalid status value"}), 400

    sql = "UPDATE students SET " + ", ".join(f"{k}=%s" for k in fields) + " WHERE student_id=%s"
    with get_db() as conn:
        rows = qexec(conn, sql, list(fields.values()) + [sid])
    if rows == 0: return jsonify({"error":"Student not found"}), 404

    _log_activity(
        g.user["username"], "update_student", "student", sid,
        f"Updated fields: {', '.join(fields.keys())}"
    )
    return jsonify({"updated":True, "fields": list(fields.keys())})

@app.route("/api/students/<sid>", methods=["DELETE"])
@require_auth
def delete_student(sid):
    if g.user.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    with get_db() as conn:
        rows = qexec(conn,"DELETE FROM students WHERE student_id=%s",(sid,))
    _log_activity(g.user["username"],"delete_student","student",sid)
    return jsonify({"deleted": rows > 0})

@app.route("/api/departments")
def departments():
    with get_db() as conn:
        rows = qall(conn, """
            SELECT DISTINCT COALESCE(f.code, s.department) AS department
            FROM   students s
            LEFT   JOIN faculties f ON f.id = s.faculty_id
            WHERE  COALESCE(f.code, s.department) IS NOT NULL
            ORDER  BY department
        """)
    return jsonify({"departments":[r["department"] for r in rows]})

# ── Attendance admin edit ─────────────────────────────────────────────────
@app.route("/api/attendance/<sid>/<att_date>", methods=["PUT"])
@require_auth
def update_attendance(sid, att_date):
    if g.user.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    d          = request.json or {}
    status     = d.get("status","")
    note       = d.get("note","")
    subject_id = d.get("subject_id")
    if status not in ("Present","Absent"):
        return jsonify({"error":"status must be Present or Absent"}), 400
    try:
        from datetime import date as _date; _date.fromisoformat(att_date)
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400
    with get_db() as conn:
        if subject_id:
            qexec(conn, """
                INSERT INTO attendance (student_id, date, time, status, note, subject_id)
                VALUES (%s, %s, CURRENT_TIME, %s, %s, %s)
                ON CONFLICT (student_id, subject_id, date) WHERE subject_id IS NOT NULL
                DO UPDATE SET status=EXCLUDED.status, note=EXCLUDED.note
            """, (sid, att_date, status, note, int(subject_id)))
        else:
            qexec(conn, """
                INSERT INTO attendance (student_id, date, time, status, note)
                VALUES (%s, %s, CURRENT_TIME, %s, %s)
                ON CONFLICT (student_id, date) WHERE subject_id IS NULL
                DO UPDATE SET status=EXCLUDED.status, note=EXCLUDED.note
            """, (sid, att_date, status, note))
    _log_activity(
        g.user["username"], "edit_attendance", "attendance", sid,
        f"Set {att_date} to {status}" + (f" — {note}" if note else "")
    )
    return jsonify({"updated":True})

@app.route("/api/attendance/<sid>/<att_date>", methods=["DELETE"])
@require_auth
def delete_attendance(sid, att_date):
    """Remove a specific attendance record (admin only)."""
    if g.user.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    subject_id = request.args.get("subject_id")
    with get_db() as conn:
        if subject_id:
            rows = qexec(conn,
                "DELETE FROM attendance WHERE student_id=%s AND date=%s AND subject_id=%s",
                (sid, att_date, int(subject_id)))
        else:
            rows = qexec(conn,
                "DELETE FROM attendance WHERE student_id=%s AND date=%s AND subject_id IS NULL",
                (sid, att_date))
    _log_activity(g.user["username"],"delete_attendance","attendance",sid,
                  f"Deleted {att_date}")
    return jsonify({"deleted": rows > 0})

# ── Activity log ──────────────────────────────────────────────────────────
@app.route("/api/activity-logs")
@require_auth
def get_activity_logs():
    try:    limit = min(max(1, int(request.args.get("limit", 50))), 2000)
    except: limit = 50
    target = request.args.get("target_id","").strip()
    with get_db() as conn:
        if target:
            rows = qall(conn, """
                SELECT admin_user,action,target_type,target_id,detail,logged_at::text
                FROM activity_logs WHERE target_id=%s
                ORDER BY logged_at DESC LIMIT %s
            """, (target, limit))
        else:
            rows = qall(conn, """
                SELECT admin_user,action,target_type,target_id,detail,logged_at::text
                FROM activity_logs ORDER BY logged_at DESC LIMIT %s
            """, (limit,))
    return jsonify({"logs": rows})

# ── Pose smoothing state (module-level, reset per session) ────────────────
# Stores the last N raw pose readings to smooth out flicker.
# A pose must appear in the majority of recent frames before being confirmed.
_pose_history = []           # list of raw pose strings, capped at _POSE_HISTORY_LEN
_POSE_HISTORY_LEN = 3        # smooth over 3 consecutive frames
_pose_debug_enabled = os.getenv("POSE_DEBUG", "0") == "1"   # set POSE_DEBUG=1 to enable


def _smooth_pose(raw_pose: str) -> str:
    """
    Append raw_pose to the history buffer and return the majority vote.
    With _POSE_HISTORY_LEN=3 a pose must appear ≥2 times to be confirmed.
    This eliminates single-frame flicker between states.
    """
    global _pose_history
    _pose_history.append(raw_pose)
    if len(_pose_history) > _POSE_HISTORY_LEN:
        _pose_history = _pose_history[-_POSE_HISTORY_LEN:]
    # Majority vote
    from collections import Counter
    counts = Counter(_pose_history)
    return counts.most_common(1)[0][0]


# ── Frame quality validation ──────────────────────────────────────────────
@app.route("/api/capture/validate-frame", methods=["POST"])
@require_auth
def validate_frame():
    """
    Called by the auto-capture UI for each candidate frame.
    Returns quality scores and detected pose so the frontend can decide
    whether to accept the frame for the current pose step.

    Body:  { "image": "base64...", "reset_pose": bool (optional) }
    Response: {
        "face_detected": bool,
        "bbox":          [x1,y1,x2,y2] | null,
        "quality":       { blur_score, brightness, face_size_ok, overall, passed } | null,
        "pose":          "front"|"left"|"right"|"up"|"down",
        "debug":         { ... } (only when POSE_DEBUG=1)
    }

    ── Pose estimation geometry ────────────────────────────────────────────
    InsightFace buffalo_l returns 5 keypoints (kps) in ORIGINAL (pre-mirror)
    pixel coordinates:
        kps[0] = left_eye   kps[1] = right_eye   kps[2] = nose_tip
        kps[3] = left_mouth kps[4] = right_mouth

    The browser CSS-mirrors the video feed (transform: scaleX(-1)), so the
    canvas pixels are mirrored. A nose shift toward canvas-right = user's LEFT.

    Metrics (all normalised by face bbox dimensions so they are scale-invariant):
        nose_offset = (nose.x − eye_midpoint.x) / face_width
            > +0.12  → user turned LEFT   (nose moves canvas-right)
            < −0.12  → user turned RIGHT  (nose moves canvas-left)

        nose_dy = (nose.y − eye_midpoint.y) / face_height
            < 0.18  AND mouth_dy < 0.38  → UP   (chin dropped, nose rises)
            > 0.25                        → DOWN (chin raised, nose falls)
            0.18–0.25                     → FRONT

    Smoothing: raw pose is passed through a 3-frame majority-vote buffer to
    prevent flickering between states when the user holds a borderline pose.
    """
    global _pose_history

    d       = request.json or {}
    img_b64 = d.get("image")
    if not img_b64:
        return jsonify({"error": "No image"}), 400

    # Allow the frontend to reset the smoothing buffer (e.g. on pose step change)
    if d.get("reset_pose"):
        _pose_history = []

    frame = decode_image(img_b64)
    if frame is None:
        return jsonify({"error": "Cannot decode"}), 400

    # ── InsightFace detection (unchanged from working original) ─────────
    fa    = get_face_app()
    rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    faces = fa.get(rgb)

    if not faces:
        _pose_history = []   # reset smoothing when face is lost
        return jsonify({"face_detected": False, "quality": None, "pose": None})

    # Pick the largest face (closest to camera)
    face = max(faces, key=lambda f: (f.bbox[2]-f.bbox[0]) * (f.bbox[3]-f.bbox[1]))
    bbox = [int(x) for x in face.bbox]
    quality = score_frame_quality(frame, bbox)

    # ── Pose estimation — verbatim from working original ────────────────
    # All variable names, threshold values, and decision logic are preserved
    # exactly as they were in the working InsightFace version.
    raw_pose  = "front"
    debug_info = {}

    try:
        kps = face.kps   # shape (5,2) — left_eye, right_eye, nose, left_mouth, right_mouth
        if kps is None:
            # buffalo_l always returns kps; None means the model isn't loaded correctly
            if _pose_debug_enabled:
                print("[POSE] kps is None — check InsightFace model load")
            raw_pose = "front"
        else:
            le, re = kps[0], kps[1]   # left eye, right eye
            nose   = kps[2]           # nose tip

            # ── Horizontal: nose vs eye midpoint ─────────────────────────
            eye_center_x = (le[0] + re[0]) / 2.0
            face_w       = float(bbox[2] - bbox[0])
            nose_offset  = (nose[0] - eye_center_x) / (face_w + 1e-5)

            # ── Vertical: nose position relative to eye line ──────────────
            eye_y    = (le[1] + re[1]) / 2.0
            face_h   = float(bbox[3] - bbox[1])
            nose_dy  = (nose[1] - eye_y) / (face_h + 1e-5)

            # ── Mouth position (needed for up/down disambiguation) ─────────
            mouth_pts = kps[3], kps[4]   # left_mouth, right_mouth
            mouth_y   = (mouth_pts[0][1] + mouth_pts[1][1]) / 2.0
            mouth_dy  = (mouth_y - eye_y) / (face_h + 1e-5)

            # ── Decision tree (exact working original thresholds) ──────────
            if nose_offset > 0.12:
                raw_pose = "left"         # user's left (mirrored canvas)
            elif nose_offset < -0.12:
                raw_pose = "right"        # user's right (mirrored canvas)
            elif nose_dy < 0.18 and mouth_dy < 0.38:
                raw_pose = "up"           # chin dropped → nose near eyes
            elif nose_dy > 0.25:
                raw_pose = "down"         # chin raised  → nose far below eyes
            else:
                raw_pose = "front"

            # ── Debug logging (enabled via POSE_DEBUG=1 env var) ──────────
            if _pose_debug_enabled:
                print(
                    f"[POSE] raw={raw_pose:6s} | "
                    f"nose_offset={nose_offset:+.3f} (±0.12) | "
                    f"nose_dy={nose_dy:.3f} (up<0.18,down>0.25) | "
                    f"mouth_dy={mouth_dy:.3f} (up_guard<0.38) | "
                    f"face_w={face_w:.0f}px face_h={face_h:.0f}px | "
                    f"le=({le[0]:.0f},{le[1]:.0f}) "
                    f"re=({re[0]:.0f},{re[1]:.0f}) "
                    f"nose=({nose[0]:.0f},{nose[1]:.0f})"
                )

            debug_info = {
                "nose_offset": round(float(nose_offset), 4),
                "nose_dy":     round(float(nose_dy),     4),
                "mouth_dy":    round(float(mouth_dy),    4),
                "face_w":      round(face_w, 1),
                "face_h":      round(face_h, 1),
                "left_eye":    [round(float(le[0]),1), round(float(le[1]),1)],
                "right_eye":   [round(float(re[0]),1), round(float(re[1]),1)],
                "nose":        [round(float(nose[0]),1), round(float(nose[1]),1)],
                "thresholds": {
                    "horiz": 0.12,
                    "nose_dy_up":   0.18,
                    "mouth_dy_up":  0.38,
                    "nose_dy_down": 0.25,
                },
            }

    except Exception as e:
        if _pose_debug_enabled:
            print(f"[POSE] Exception in pose estimation: {e}")
        raw_pose = "front"

    # ── Smoothing: majority vote over last 3 frames ──────────────────────
    # Prevents flickering when the user holds a borderline pose angle.
    # The raw pose is what the geometry says; smoothed is what we report.
    smoothed_pose = _smooth_pose(raw_pose)

    if _pose_debug_enabled and raw_pose != smoothed_pose:
        print(f"[POSE] smoothed {raw_pose!r} → {smoothed_pose!r} (history={_pose_history})")

    response = {
        "face_detected": True,
        "bbox":          bbox,
        "quality":       quality,
        "pose":          smoothed_pose,
    }

    # Include debug info only when explicitly enabled (don't bloat normal responses)
    if _pose_debug_enabled:
        response["debug"] = {**debug_info, "raw_pose": raw_pose, "history": list(_pose_history)}

    return jsonify(response)

# ── Enroll ────────────────────────────────────────────────────────────────
@app.route("/api/enroll", methods=["POST"])
@require_auth
def enroll():
    if request.content_type and "application/json" in request.content_type:
        data       = request.json or {}
        student_id = data.get("student_id","").strip()
        full_name  = data.get("full_name","").strip()
        faculty_id = data.get("faculty_id") or None
        department = data.get("department") or None
        email      = data.get("email")      or None
        phone      = data.get("phone")      or None
        semester   = data.get("semester")   or None
        frames_b64 = data.get("frames",[])
    else:
        student_id = request.form.get("student_id","").strip()
        full_name  = request.form.get("full_name","").strip()
        faculty_id = request.form.get("faculty_id") or None
        department = request.form.get("department") or None
        email      = request.form.get("email")      or None
        phone      = request.form.get("phone")      or None
        semester   = request.form.get("semester")   or None
        frames_b64 = [base64.b64encode(f.read()).decode()
                      for f in request.files.getlist("images")]

    # Resolve department from faculty_id if provided (ensures department = faculty.code)
    if faculty_id:
        try:
            faculty_id = int(faculty_id)
        except (ValueError, TypeError):
            faculty_id = None

    if not student_id or not full_name:
        return jsonify({"error":"student_id and full_name required"}), 400
    if not frames_b64:
        return jsonify({"error":"No images provided"}), 400

    # Duplicate face check
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

    with get_db() as conn:
        # Resolve department code from faculty_id (short form, e.g. "CSIT" not "BSc CSIT")
        if faculty_id:
            fac = qone(conn, "SELECT code FROM faculties WHERE id=%s", (faculty_id,))
            if fac:
                department = fac["code"]
        qexec(conn, """
            INSERT INTO students
                (student_id, full_name, department, faculty_id, email, phone, semester,
                 embedding, face_image, sample_count)
            VALUES (%s,%s,%s,%s,%s,%s,%s, %s::vector, %s, %s)
            ON CONFLICT (student_id) DO UPDATE SET
                full_name    = EXCLUDED.full_name,
                department   = EXCLUDED.department,
                faculty_id   = EXCLUDED.faculty_id,
                email        = EXCLUDED.email,
                phone        = EXCLUDED.phone,
                semester     = EXCLUDED.semester,
                embedding    = EXCLUDED.embedding,
                face_image   = EXCLUDED.face_image,
                sample_count = EXCLUDED.sample_count
        """, (student_id, full_name, department, faculty_id, email, phone, semester,
              mean_emb.tolist(), thumbnail, len(frames_b64)))

    return jsonify({
        "enrolled":   True,
        "student_id": student_id,
        "samples":    len(frames_b64),
        "is_update":  bool(existing_sid == student_id if test_emb is not None else False)
    })

# ── Recognize ─────────────────────────────────────────────────────────────
@app.route("/api/recognize", methods=["POST"])
@require_auth
def recognize():
    data    = request.json or {}
    img_b64 = data.get("image")
    if not img_b64: return jsonify({"error":"No image"}), 400

    frame = decode_image(img_b64)
    if frame is None: return jsonify({"error":"Cannot decode image"}), 400

    fa    = get_face_app()
    rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    faces = fa.get(rgb)
    if not faces:
        return jsonify({"recognized":False,"message":"No face detected"})

    face = max(faces, key=lambda f:(f.bbox[2]-f.bbox[0])*(f.bbox[3]-f.bbox[1]))
    emb  = face.normed_embedding
    bbox = [int(x) for x in face.bbox]

    with get_db() as conn:
        sid, name, sim = find_best_match(conn, emb)

    if sim >= THRESHOLD and sid:
        today = date.today().isoformat()
        now   = datetime.now().strftime("%H:%M:%S")
        dept  = email = None
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO attendance (student_id, date, time, status)
                    VALUES (%s,%s,%s,'Present')
                    ON CONFLICT DO NOTHING
                """, (sid, today, now))
                marked = c.rowcount == 1
                c.execute("""
                    INSERT INTO recognition_logs (student_id, full_name, confidence, recognized)
                    VALUES (%s,%s,%s,true)
                """, (sid, name, round(sim*100,1)))
            if marked:
                row = qone(conn,"SELECT department, email FROM students WHERE student_id=%s",(sid,))
                if row: dept=row.get("department"); email=row.get("email")
        if marked:
            sse_broadcast({"type":"attendance","student_id":sid,"name":name,
                           "confidence":round(sim*100,1),"time":now,"date":today})
            if email:
                queue_attendance_email(sid, name, dept, today, now, email)
        return jsonify({"recognized":True,"student_id":sid,"name":name,
                        "confidence":round(sim*100,1),"bbox":bbox,
                        "attendance_marked":marked})

    _log_recognition(None,"Unknown",sim,False)
    return jsonify({"recognized":False,"name":"Unknown",
                    "confidence":round(sim*100,1),"bbox":bbox})

# ── Camera ────────────────────────────────────────────────────────────────
@app.route("/api/camera/start", methods=["POST"])
@require_auth
def start_camera():
    if camera_state["active"]: return jsonify({"status":"already_running"})
    camera_state["active"] = True
    return jsonify({"status":"started"})

@app.route("/api/camera/stop", methods=["POST"])
@require_auth
def stop_camera():
    camera_state["active"] = False
    if camera_state["cap"]: camera_state["cap"].release()
    return jsonify({"status":"stopped"})

@app.route("/api/stream")
@require_auth
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

# ── Attendance ────────────────────────────────────────────────────────────
@app.route("/api/attendance")
@require_auth
def get_attendance():
    target = request.args.get("date", date.today().isoformat())
    dept   = request.args.get("department","").strip()
    sql = """
        SELECT s.student_id, s.full_name, s.department,
               a.date::text, a.time::text, a.status, a.note
        FROM   students s
        LEFT   JOIN attendance a
               ON  a.student_id = s.student_id AND a.date = %s
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
@require_auth
def faculty_summary():
    target = request.args.get("date", date.today().isoformat())
    with get_db() as conn:
        rows = qall(conn, """
            SELECT s.student_id, s.full_name,
                   COALESCE(f.code, s.department, 'Unassigned') AS department,
                   a.time::text AS time,
                   COALESCE(a.status,'Absent') AS status
            FROM   students s
            LEFT   JOIN faculties f ON f.id = s.faculty_id
            LEFT   JOIN attendance a
                   ON  a.student_id=s.student_id AND a.date=%s
            ORDER  BY department, s.full_name
        """, (target,))
    faculty_map = {}
    for r in rows:
        faculty_map.setdefault(r["department"],[]).append({
            "student_id":r["student_id"],"name":r["full_name"],
            "time":r["time"] or "—","status":r["status"]})
    faculties = []
    for dept_name in sorted(faculty_map):
        students = faculty_map[dept_name]
        present  = sum(1 for s in students if s["status"]=="Present")
        total    = len(students)
        faculties.append({"name":dept_name,"total":total,"present":present,
                          "absent":total-present,
                          "rate":round(present/total*100,1) if total else 0,
                          "students":students})
    ot = sum(f["total"] for f in faculties)
    op = sum(f["present"] for f in faculties)
    return jsonify({"date":target,"faculties":faculties,
                    "overall":{"total":ot,"present":op,"absent":ot-op,
                               "rate":round(op/ot*100,1) if ot else 0}})

@app.route("/api/attendance/history")
@require_auth
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
@require_auth
def attendance_stats():
    dept = request.args.get("department","").strip()
    with get_db() as conn:
        total_days = total_attendance_days(conn, dept or None)
        sql = """
            SELECT s.student_id, s.full_name, s.department,
                   COUNT(a.id) FILTER(WHERE a.status='Present') AS present_days,
                   %s::int AS total_days,
                   ROUND(100.0*COUNT(a.id) FILTER(WHERE a.status='Present')
                         /NULLIF(%s::int,0),1) AS pct
            FROM students s LEFT JOIN attendance a ON a.student_id=s.student_id
        """
        params = [total_days, total_days]
        if dept: sql += " WHERE s.department=%s"; params.append(dept)
        sql += " GROUP BY s.student_id,s.full_name,s.department ORDER BY pct DESC NULLS LAST"
        rows = qall(conn, sql, params)
    return jsonify({"stats": rows})

@app.route("/api/attendance/export")
@require_auth
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
@require_auth
def get_logs():
    try:    limit = min(max(1, int(request.args.get("limit", 50))), 2000)
    except: limit = 50
    with get_db() as conn:
        rows = qall(conn, """
            SELECT DISTINCT ON (student_id, logged_at::date)
                   student_id, full_name, confidence, recognized, logged_at::text
            FROM recognition_logs
            WHERE student_id IS NOT NULL
            ORDER BY student_id, logged_at::date, logged_at ASC
        """)
    rows = sorted(rows, key=lambda r: r["logged_at"], reverse=True)[:limit]
    return jsonify({"logs": rows})

# ── Email ─────────────────────────────────────────────────────────────────
@app.route("/api/email/test", methods=["POST"])
@require_auth
def test_email():
    """Send a test email to the address in the request body."""
    d = request.json or {}
    to = d.get("email","").strip()
    if not to: return jsonify({"error":"email required"}), 400
    if not EMAIL_ENABLED:
        return jsonify({"error":"Email not configured. Set BREVO_API_KEY and BREVO_FROM in .env"}), 503
    queue_attendance_email(
        "TEST", "Test Student", "Test Department",
        date.today().isoformat(), datetime.now().strftime("%H:%M:%S"), to
    )
    return jsonify({"queued":True,"note":"Email will arrive within 10 seconds if SMTP is correct"})

@app.route("/api/email/logs")
@require_auth
def email_logs():
    with get_db() as conn:
        rows = qall(conn, """
            SELECT student_id,email_to,subject,sent_at::text,success,error_msg
            FROM email_log ORDER BY sent_at DESC LIMIT 50
        """)
    return jsonify({"logs": rows})

@app.route("/api/email/send-attendance-summary", methods=["POST"])
@require_auth
def send_attendance_summary():
    if not EMAIL_ENABLED:
        return jsonify({"error": "Email not configured. Set BREVO_API_KEY and BREVO_FROM in .env"}), 503

    att_date = (request.json or {}).get("date", date.today().isoformat())

    def _worker():
        try:
            _send_bulk_attendance_emails(att_date)
        except Exception as e:
            print(f"[bulk_email] Unhandled error: {e}")

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"queued": True, "date": att_date})

# ── Settings ──────────────────────────────────────────────────────────────
def _load_settings_from_db():
    """Load persisted settings from system_settings table into globals."""
    global THRESHOLD, SKIP
    try:
        with get_db(register_pgvector=False) as conn:
            rows = qall(conn, "SELECT key, value FROM system_settings")
            for row in rows:
                if row["key"] == "recognition_threshold":
                    THRESHOLD = float(row["value"])
                elif row["key"] == "frame_skip":
                    SKIP = int(row["value"])
    except Exception as e:
        print(f"[settings] Could not load from DB: {e}")

@app.route("/api/settings")
@require_auth
def get_settings():
    with get_db(register_pgvector=False) as conn:
        rows = qall(conn, "SELECT key, value FROM system_settings ORDER BY key")
    extra = {r["key"]: r["value"] for r in rows}
    return jsonify({
        "recognition_threshold": THRESHOLD,
        "frame_skip":            SKIP,
        "email_enabled":         EMAIL_ENABLED,
        "brevo_from":            BREVO_FROM if BREVO_FROM else "",
        "min_attendance_pct":    float(extra.get("min_attendance_pct", 75)),
        "qr_expiry_seconds":     int(extra.get("qr_expiry_seconds", 90)),
    })

@app.route("/api/settings", methods=["PUT"])
@require_auth
def update_settings():
    if g.user.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    global THRESHOLD, SKIP
    d = request.json or {}
    updates = {}
    if "recognition_threshold" in d:
        THRESHOLD = float(d["recognition_threshold"])
        updates["recognition_threshold"] = str(THRESHOLD)
    if "frame_skip" in d:
        SKIP = int(d["frame_skip"])
        updates["frame_skip"] = str(SKIP)
    for k in ("min_attendance_pct", "qr_expiry_seconds"):
        if k in d:
            updates[k] = str(d[k])
    if updates:
        with get_db(register_pgvector=False) as conn:
            for k, v in updates.items():
                qexec(conn,
                    "INSERT INTO system_settings (key, value, updated_at) VALUES (%s,%s,NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET value=%s, updated_at=NOW()",
                    (k, v, v))
    _log_activity(g.user["username"], "update_settings", "system", detail=str(d))
    return jsonify({"recognition_threshold": THRESHOLD, "frame_skip": SKIP,
                    "updated": list(updates.keys())})

# ── Faculties ─────────────────────────────────────────────────────────────
@app.route("/api/faculties")
@require_auth
def list_faculties():
    with get_db() as conn:
        rows = qall(conn, "SELECT id, name, code FROM faculties ORDER BY name")
    return jsonify({"faculties": rows})

@app.route("/api/faculties", methods=["POST"])
@require_auth
def create_faculty():
    d = request.json or {}
    if not d.get("name"): return jsonify({"error": "name is required"}), 400
    try:
        with get_db() as conn:
            row = qone(conn,
                "INSERT INTO faculties (name, code) VALUES (%s,%s) RETURNING id, name, code",
                (d["name"], d.get("code", "")))
    except Exception as e:
        if "unique" in str(e).lower():
            return jsonify({"error": "Faculty already exists"}), 409
        raise
    _log_activity(g.user["username"], "create_faculty", "faculty", target_id=d["name"])
    return jsonify({"faculty": row}), 201

@app.route("/api/faculties/<int:fid>", methods=["PUT"])
@require_auth
def update_faculty(fid):
    d = request.json or {}
    fields, vals = [], []
    for col in ["name", "code"]:
        if col in d:
            fields.append(f"{col}=%s"); vals.append(d[col])
    if not fields: return jsonify({"error": "Nothing to update"}), 400
    vals.append(fid)
    with get_db() as conn:
        row = qone(conn, f"UPDATE faculties SET {','.join(fields)} WHERE id=%s RETURNING id,name,code", vals)
    if not row: return jsonify({"error": "Faculty not found"}), 404
    _log_activity(g.user["username"], "update_faculty", "faculty", target_id=str(fid))
    return jsonify({"faculty": row})

@app.route("/api/faculties/<int:fid>", methods=["DELETE"])
@require_auth
def delete_faculty_db(fid):
    import psycopg2
    try:
        with get_db() as conn:
            # Check what's still referencing this faculty before attempting delete
            refs = {}
            refs["subjects"]     = (qone(conn, "SELECT COUNT(*) AS n FROM subjects WHERE faculty_id=%s", (fid,)) or {}).get("n", 0)
            refs["assignments"]  = (qone(conn, "SELECT COUNT(*) AS n FROM teacher_assignments WHERE faculty_id=%s", (fid,)) or {}).get("n", 0)
            refs["students"]     = (qone(conn, "SELECT COUNT(*) AS n FROM students WHERE faculty_id=%s", (fid,)) or {}).get("n", 0)
            refs["schedules"]    = (qone(conn, "SELECT COUNT(*) AS n FROM class_schedules WHERE faculty_id=%s", (fid,)) or {}).get("n", 0)
            refs["sessions"]     = (qone(conn, "SELECT COUNT(*) AS n FROM attendance_sessions WHERE faculty_id=%s", (fid,)) or {}).get("n", 0)

            blocking = {k: v for k, v in refs.items() if v > 0}
            if blocking:
                parts = [f"{v} {k}" for k, v in blocking.items()]
                return jsonify({"error": f"Cannot delete: faculty is used by {', '.join(parts)}. Remove them first."}), 409

            row = qone(conn, "DELETE FROM faculties WHERE id=%s RETURNING id", (fid,))
    except psycopg2.errors.ForeignKeyViolation as e:
        return jsonify({"error": "Cannot delete: faculty is still referenced by other records."}), 409
    if not row: return jsonify({"error": "Faculty not found"}), 404
    _log_activity(g.user["username"], "delete_faculty", "faculty", target_id=str(fid))
    return jsonify({"deleted": True})

# ── Time Slots ────────────────────────────────────────────────────────────
@app.route("/api/timeslots")
@require_auth
def list_timeslots():
    search = request.args.get("search", "").strip()
    try:    limit = min(max(0, int(request.args.get("limit", 0))), 2000)
    except: limit = 0   # 0 = no limit (for dropdowns)
    where  = "WHERE label ILIKE %s" if search else ""
    vals   = ([f"%{search}%"] if search else [])
    lim    = f"LIMIT {limit}" if limit else ""
    with get_db() as conn:
        rows = qall(conn,
            f"SELECT id, label, start_time::text AS start_time, end_time::text AS end_time "
            f"FROM time_slots {where} ORDER BY start_time {lim}", vals)
    return jsonify({"time_slots": rows})

@app.route("/api/timeslots", methods=["POST"])
@require_auth
def create_timeslot():
    d = request.json or {}
    if not d.get("label"): return jsonify({"error": "label is required"}), 400
    with get_db() as conn:
        row = qone(conn,
            "INSERT INTO time_slots (label, start_time, end_time) VALUES (%s,%s,%s) "
            "RETURNING id, label, start_time::text, end_time::text",
            (d["label"], d.get("start_time"), d.get("end_time")))
    _log_activity(g.user["username"], "create_timeslot", "timeslot", target_id=d["label"])
    return jsonify({"time_slot": row}), 201

@app.route("/api/timeslots/<int:tid>", methods=["PUT"])
@require_auth
def update_timeslot(tid):
    d = request.json or {}
    fields, vals = [], []
    for col in ["label", "start_time", "end_time"]:
        if col in d:
            fields.append(f"{col}=%s"); vals.append(d[col])
    if not fields: return jsonify({"error": "Nothing to update"}), 400
    vals.append(tid)
    with get_db() as conn:
        row = qone(conn,
            f"UPDATE time_slots SET {','.join(fields)} WHERE id=%s "
            "RETURNING id, label, start_time::text, end_time::text", vals)
    if not row: return jsonify({"error": "Time slot not found"}), 404
    return jsonify({"time_slot": row})

@app.route("/api/timeslots/<int:tid>", methods=["DELETE"])
@require_auth
def delete_timeslot(tid):
    with get_db() as conn:
        qexec(conn, "UPDATE teacher_assignments SET time_slot_id=NULL WHERE time_slot_id=%s", (tid,))
        row = qone(conn, "DELETE FROM time_slots WHERE id=%s RETURNING id", (tid,))
    if not row: return jsonify({"error": "Time slot not found"}), 404
    _log_activity(g.user["username"], "delete_timeslot", "timeslot", target_id=str(tid))
    return jsonify({"deleted": True})

@app.route("/api/timeslots/all", methods=["DELETE"])
@require_auth
def delete_all_timeslots():
    with get_db() as conn:
        qexec(conn, "UPDATE teacher_assignments SET time_slot_id=NULL")
        qexec(conn, "DELETE FROM time_slots")
    _log_activity(g.user["username"], "delete_all_timeslots", "timeslot")
    return jsonify({"deleted": True})

# ── Teachers CRUD ─────────────────────────────────────────────────────────
@app.route("/api/teachers", methods=["GET"])
@require_auth
def list_teachers():
    with get_db() as conn:
        rows = qall(conn, """
            SELECT t.id, t.teacher_id, t.full_name, t.email, t.phone, t.status,
                   COALESCE(
                       json_agg(
                           json_build_object(
                               'id',              ta.id,
                               'faculty_id',      ta.faculty_id,
                               'faculty_name',    f.name,
                               'faculty_code',    f.code,
                               'semester',        ta.semester,
                               'subject_id',      ta.subject_id,
                               'subject_name',    s.name,
                               'subject_code',    s.code,
                               'time_slot_id',    ta.time_slot_id,
                               'time_slot_label', ts.label,
                               'day_of_week',     ta.day_of_week
                           ) ORDER BY ta.id
                       ) FILTER (WHERE ta.id IS NOT NULL),
                       '[]'::json
                   ) AS assignments
            FROM teachers t
            LEFT JOIN teacher_assignments ta ON ta.teacher_id = t.id
            LEFT JOIN faculties  f  ON f.id  = ta.faculty_id
            LEFT JOIN subjects   s  ON s.id  = ta.subject_id
            LEFT JOIN time_slots ts ON ts.id = ta.time_slot_id
            GROUP BY t.id
            ORDER BY t.full_name
        """)
    return jsonify({"teachers": rows})

@app.route("/api/teachers/<int:tid>", methods=["GET"])
@require_auth
def get_teacher(tid):
    with get_db() as conn:
        t = qone(conn,
            "SELECT id, teacher_id, full_name, email, phone, status FROM teachers WHERE id=%s",
            (tid,))
        if not t: return jsonify({"error": "Teacher not found"}), 404
        assignments = qall(conn, """
            SELECT ta.id, ta.faculty_id, f.name AS faculty_name, f.code AS faculty_code,
                   ta.semester, ta.subject_id, s.name AS subject_name, s.code AS subject_code,
                   ta.time_slot_id, ts.label AS time_slot_label
            FROM teacher_assignments ta
            LEFT JOIN faculties  f  ON f.id  = ta.faculty_id
            LEFT JOIN subjects   s  ON s.id  = ta.subject_id
            LEFT JOIN time_slots ts ON ts.id = ta.time_slot_id
            WHERE ta.teacher_id = %s ORDER BY ta.id
        """, (tid,))
    t["assignments"] = assignments
    return jsonify({"teacher": t})

@app.route("/api/teachers", methods=["POST"])
@require_auth
def create_teacher():
    if g.user.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    d = request.json or {}
    if not d.get("teacher_id"): return jsonify({"error": "teacher_id is required"}), 400
    if not d.get("full_name"):  return jsonify({"error": "full_name is required"}), 400
    if not d.get("password"):   return jsonify({"error": "password is required"}), 400
    pw_hash = hashlib.sha256(d["password"].encode()).hexdigest()
    try:
        with get_db() as conn:
            row = qone(conn, """
                INSERT INTO teachers (teacher_id, full_name, email, phone, password_hash, status)
                VALUES (%s,%s,%s,%s,%s,%s)
                RETURNING id, teacher_id, full_name, email, phone, status
            """, (d["teacher_id"], d["full_name"],
                  d.get("email"), d.get("phone"), pw_hash,
                  d.get("status", "active")))
    except Exception as e:
        if "unique" in str(e).lower():
            return jsonify({"error": "Teacher ID already exists"}), 409
        raise
    _log_activity(g.user["username"], "create_teacher", "teacher", target_id=d["teacher_id"])
    return jsonify({"teacher": row}), 201

@app.route("/api/teachers/<int:tid>", methods=["PUT"])
@require_auth
def update_teacher(tid):
    if g.user.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    d = request.json or {}
    fields, values = [], []
    for col in ["teacher_id", "full_name", "email", "phone", "status"]:
        if col in d:
            fields.append(f"{col}=%s")
            values.append(d[col] or None)
    if "password" in d and d["password"]:
        fields.append("password_hash=%s")
        values.append(hashlib.sha256(d["password"].encode()).hexdigest())
    if not fields: return jsonify({"error": "Nothing to update"}), 400
    values.append(tid)
    with get_db() as conn:
        row = qone(conn,
            f"UPDATE teachers SET {', '.join(fields)} WHERE id=%s "
            "RETURNING id, teacher_id, full_name, email, phone, status",
            values)
    if not row: return jsonify({"error": "Teacher not found"}), 404
    _log_activity(g.user["username"], "update_teacher", "teacher", target_id=str(tid))
    return jsonify({"teacher": row})

@app.route("/api/teachers/<int:tid>", methods=["DELETE"])
@require_auth
def delete_teacher(tid):
    if g.user.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    with get_db() as conn:
        # Nullify FK references that are NO ACTION (not cascaded)
        qexec(conn, "UPDATE recognition_logs SET teacher_id = NULL WHERE teacher_id = %s", (tid,))
        qexec(conn, "UPDATE attendance SET teacher_id = NULL WHERE teacher_id = %s", (tid,))
        row = qone(conn, "DELETE FROM teachers WHERE id=%s RETURNING id", (tid,))
    if not row: return jsonify({"error": "Teacher not found"}), 404
    _log_activity(g.user["username"], "delete_teacher", "teacher", target_id=str(tid))
    return jsonify({"deleted": True})

# ── Teacher Assignments ────────────────────────────────────────────────────
@app.route("/api/teachers/<int:tid>/assignments", methods=["GET"])
@require_auth
def get_teacher_assignments(tid):
    with get_db() as conn:
        rows = qall(conn, """
            SELECT ta.id, ta.faculty_id, f.name AS faculty_name, f.code AS faculty_code,
                   ta.semester, ta.subject_id, s.name AS subject_name, s.code AS subject_code,
                   ta.time_slot_id, ts.label AS time_slot_label,
                   ts.start_time::text, ts.end_time::text, ta.day_of_week
            FROM teacher_assignments ta
            LEFT JOIN faculties  f  ON f.id  = ta.faculty_id
            LEFT JOIN subjects   s  ON s.id  = ta.subject_id
            LEFT JOIN time_slots ts ON ts.id = ta.time_slot_id
            WHERE ta.teacher_id = %s
            ORDER BY CASE ta.day_of_week
                WHEN 'Mon' THEN 0 WHEN 'Tue' THEN 1 WHEN 'Wed' THEN 2
                WHEN 'Thu' THEN 3 WHEN 'Fri' THEN 4 WHEN 'Sat' THEN 5
                ELSE 6 END, ts.start_time
        """, (tid,))
    return jsonify({"assignments": rows})

@app.route("/api/teachers/<int:tid>/assignments", methods=["POST"])
@require_auth
def add_teacher_assignment(tid):
    d            = request.json or {}
    faculty_id   = d.get("faculty_id")   or None
    semester     = d.get("semester")     or None
    subject_id   = d.get("subject_id")   or None
    time_slot_id = d.get("time_slot_id") or None
    day_of_week  = d.get("day_of_week")  or None

    with get_db() as conn:
        if not qone(conn, "SELECT id FROM teachers WHERE id=%s", (tid,)):
            return jsonify({"error": "Teacher not found"}), 404

        # Collision checks: (1) same teacher can't be in two slots at once;
        # (2) same faculty/semester/slot can only have one teacher
        if day_of_week and time_slot_id:
            self_conflict = qone(conn, """
                SELECT ta.id FROM teacher_assignments ta
                WHERE ta.teacher_id=%s AND ta.day_of_week=%s AND ta.time_slot_id=%s
            """, (tid, day_of_week, time_slot_id))
            if self_conflict:
                return jsonify({"error": "This teacher already has a class in this time slot"}), 409

        if faculty_id and semester and day_of_week and time_slot_id:
            conflict = qone(conn, """
                SELECT ta.id, t.full_name AS teacher_name
                FROM teacher_assignments ta
                JOIN teachers t ON t.id = ta.teacher_id
                WHERE ta.faculty_id=%s AND ta.semester=%s
                  AND ta.day_of_week=%s AND ta.time_slot_id=%s
                  AND ta.teacher_id != %s
            """, (faculty_id, semester, day_of_week, time_slot_id, tid))
            if conflict:
                return jsonify({
                    "error": f"Timetable collision — {conflict['teacher_name']} already assigned to this slot"
                }), 409

        try:
            row = qone(conn, """
                INSERT INTO teacher_assignments
                    (teacher_id, faculty_id, semester, subject_id, time_slot_id, day_of_week, is_primary)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                RETURNING id, faculty_id, semester, subject_id, time_slot_id, day_of_week
            """, (tid, faculty_id, semester, subject_id, time_slot_id, day_of_week,
                  d.get("is_primary", False)))
        except Exception as e:
            if "unique" in str(e).lower():
                return jsonify({"error": "This assignment already exists"}), 409
            raise

    _log_activity(g.user["username"], "add_assignment", "teacher", target_id=str(tid))
    return jsonify({"assignment": row}), 201

@app.route("/api/teacher-assignments/<int:aid>", methods=["PUT"])
@require_auth
def update_teacher_assignment(aid):
    d            = request.json or {}
    faculty_id   = d.get("faculty_id")   or None
    semester     = d.get("semester")     or None
    subject_id   = d.get("subject_id")   or None
    time_slot_id = d.get("time_slot_id") or None
    day_of_week  = d.get("day_of_week")  or None

    with get_db() as conn:
        existing = qone(conn, "SELECT id, teacher_id FROM teacher_assignments WHERE id=%s", (aid,))
        if not existing: return jsonify({"error": "Assignment not found"}), 404

        if faculty_id and semester and day_of_week and time_slot_id:
            conflict = qone(conn, """
                SELECT ta.id, t.full_name AS teacher_name
                FROM teacher_assignments ta
                JOIN teachers t ON t.id = ta.teacher_id
                WHERE ta.faculty_id=%s AND ta.semester=%s
                  AND ta.day_of_week=%s AND ta.time_slot_id=%s
                  AND ta.id != %s
            """, (faculty_id, semester, day_of_week, time_slot_id, aid))
            if conflict:
                return jsonify({
                    "error": f"Timetable collision — {conflict['teacher_name']} already has this slot"
                }), 409

        row = qone(conn, """
            UPDATE teacher_assignments
            SET faculty_id=%s, semester=%s, subject_id=%s, time_slot_id=%s, day_of_week=%s
            WHERE id=%s
            RETURNING id, teacher_id, faculty_id, semester, subject_id, time_slot_id, day_of_week
        """, (faculty_id, semester, subject_id, time_slot_id, day_of_week, aid))

    _log_activity(g.user["username"], "update_assignment", "teacher", target_id=str(aid))
    return jsonify({"assignment": row})

@app.route("/api/teacher-assignments/<int:aid>", methods=["DELETE"])
@require_auth
def delete_teacher_assignment(aid):
    with get_db() as conn:
        row = qone(conn,
            "DELETE FROM teacher_assignments WHERE id=%s RETURNING id", (aid,))
    if not row: return jsonify({"error": "Assignment not found"}), 404
    _log_activity(g.user["username"], "delete_assignment", "teacher", target_id=str(aid))
    return jsonify({"deleted": True})

# ── Subjects CRUD ──────────────────────────────────────────────────────────
@app.route("/api/subjects", methods=["GET"])
@require_auth
def list_subjects():
    faculty_id = request.args.get("faculty_id", "")
    semester   = request.args.get("semester", "")
    where, vals = [], []
    if faculty_id:
        where.append("s.faculty_id = %s"); vals.append(int(faculty_id))
    if semester:
        where.append("s.semester = %s"); vals.append(int(semester))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        rows = qall(conn, f"""
            SELECT s.id, s.name, s.code, s.semester, s.faculty_id,
                   f.name AS faculty_name, f.code AS faculty_code
            FROM subjects s
            LEFT JOIN faculties f ON f.id = s.faculty_id
            {clause}
            ORDER BY s.name
        """, vals)
    return jsonify({"subjects": rows})

@app.route("/api/subjects", methods=["POST"])
@require_auth
def create_subject():
    d = request.json or {}
    if not d.get("name"): return jsonify({"error": "name is required"}), 400
    if not d.get("code"): return jsonify({"error": "code is required"}), 400
    try:
        with get_db() as conn:
            row = qone(conn, """
                INSERT INTO subjects (name, code, faculty_id, semester)
                VALUES (%s,%s,%s,%s)
                RETURNING id, name, code, faculty_id, semester
            """, (d["name"], d["code"],
                  d.get("faculty_id") or None,
                  d.get("semester") or None))
    except Exception as e:
        if "unique" in str(e).lower():
            return jsonify({"error": "Subject code already exists"}), 409
        raise
    _log_activity(g.user["username"], "create_subject", "subject", target_id=d["code"])
    return jsonify({"subject": row}), 201

@app.route("/api/subjects/<int:sid>", methods=["PUT"])
@require_auth
def update_subject(sid):
    d = request.json or {}
    fields, values = [], []
    for col in ["name", "code", "faculty_id", "semester"]:
        if col in d:
            fields.append(f"{col}=%s")
            values.append(d[col] or None)
    if not fields: return jsonify({"error": "Nothing to update"}), 400
    values.append(sid)
    try:
        with get_db() as conn:
            row = qone(conn,
                f"UPDATE subjects SET {', '.join(fields)} WHERE id=%s "
                "RETURNING id, name, code, faculty_id, semester", values)
    except Exception as e:
        if "unique" in str(e).lower():
            return jsonify({"error": "Subject code already exists"}), 409
        raise
    if not row: return jsonify({"error": "Subject not found"}), 404
    _log_activity(g.user["username"], "update_subject", "subject", target_id=str(sid))
    return jsonify({"subject": row})

@app.route("/api/subjects/<int:sid>", methods=["DELETE"])
@require_auth
def delete_subject(sid):
    if g.user.get("role") != "admin":
        return jsonify({"error": "Admin access required"}), 403
    with get_db() as conn:
        att_n  = (qone(conn, "SELECT COUNT(*) AS n FROM attendance WHERE subject_id=%s", (sid,)) or {}).get("n", 0)
        sess_n = (qone(conn, "SELECT COUNT(*) AS n FROM attendance_sessions WHERE subject_id=%s", (sid,)) or {}).get("n", 0)
        if att_n or sess_n:
            return jsonify({"error": f"Cannot delete: {att_n} attendance records and {sess_n} sessions reference this subject. Remove them first."}), 409
        row = qone(conn, "DELETE FROM subjects WHERE id=%s RETURNING id", (sid,))
    if not row: return jsonify({"error": "Subject not found"}), 404
    _log_activity(g.user["username"], "delete_subject", "subject", target_id=str(sid))
    return jsonify({"deleted": True})

# ══════════════════════════════════════════════════════════════════════════
#  TIMETABLE  (collision-safe scheduling)
# ══════════════════════════════════════════════════════════════════════════

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

@app.route("/api/timetable")
@require_auth
def get_timetable():
    faculty_id = request.args.get("faculty_id", "")
    semester   = request.args.get("semester", "")
    where, vals = [], []
    if faculty_id: where.append("cs.faculty_id=%s"); vals.append(int(faculty_id))
    if semester:   where.append("cs.semester=%s");   vals.append(int(semester))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        rows = qall(conn, f"""
            SELECT cs.id, cs.faculty_id, f.name AS faculty_name, f.code AS faculty_code,
                   cs.semester, cs.day_of_week, cs.time_slot_id,
                   ts.label AS time_slot_label,
                   ts.start_time::text AS start_time, ts.end_time::text AS end_time,
                   cs.teacher_id, t.full_name AS teacher_name, t.teacher_id AS teacher_code,
                   cs.subject_id, s.name AS subject_name, s.code AS subject_code
            FROM class_schedules cs
            LEFT JOIN faculties  f  ON f.id  = cs.faculty_id
            LEFT JOIN time_slots ts ON ts.id = cs.time_slot_id
            LEFT JOIN teachers   t  ON t.id  = cs.teacher_id
            LEFT JOIN subjects   s  ON s.id  = cs.subject_id
            {clause}
            ORDER BY cs.day_of_week, ts.start_time
        """, vals)
        slots = qall(conn, "SELECT id, label, start_time::text, end_time::text FROM time_slots ORDER BY start_time")
        faculties = qall(conn, "SELECT id, name, code FROM faculties ORDER BY name")
    return jsonify({"timetable": rows, "slots": slots, "faculties": faculties})

@app.route("/api/timetable/check", methods=["POST"])
@require_auth
def check_timetable_slot():
    d = request.json or {}
    faculty_id   = d.get("faculty_id")
    semester     = d.get("semester")
    day_of_week  = d.get("day_of_week")
    time_slot_id = d.get("time_slot_id")
    teacher_id   = d.get("teacher_id")
    exclude_id   = d.get("exclude_id")   # for edit: skip own entry
    if not all([faculty_id, semester, day_of_week, time_slot_id]):
        return jsonify({"error": "faculty_id, semester, day_of_week, time_slot_id required"}), 400
    with get_db() as conn:
        # 1. Check if the slot (this specific class) is already taken
        sql = """
            SELECT cs.id, t.full_name AS teacher_name, s.name AS subject_name,
                   f.name AS faculty_name
            FROM class_schedules cs
            LEFT JOIN teachers t ON t.id = cs.teacher_id
            LEFT JOIN subjects s ON s.id = cs.subject_id
            LEFT JOIN faculties f ON f.id = cs.faculty_id
            WHERE cs.faculty_id=%s AND cs.semester=%s
              AND cs.day_of_week=%s AND cs.time_slot_id=%s
        """
        params = [faculty_id, semester, day_of_week, time_slot_id]
        if exclude_id:
            sql += " AND cs.id != %s"; params.append(exclude_id)
        slot_conflict = qone(conn, sql, params)
        if slot_conflict:
            return jsonify({"available": False, "conflict": slot_conflict,
                            "message": f"Slot taken by {slot_conflict.get('teacher_name','?')} — {slot_conflict.get('subject_name','?')}"})

        # 2. Check if the chosen teacher is already teaching elsewhere at the same time
        if teacher_id:
            t_sql = """
                SELECT cs.id, f.name AS faculty_name, cs.semester,
                       s.name AS subject_name
                FROM class_schedules cs
                LEFT JOIN faculties f ON f.id = cs.faculty_id
                LEFT JOIN subjects  s ON s.id = cs.subject_id
                WHERE cs.teacher_id=%s
                  AND cs.day_of_week=%s AND cs.time_slot_id=%s
            """
            t_params = [teacher_id, day_of_week, time_slot_id]
            if exclude_id:
                t_sql += " AND cs.id != %s"; t_params.append(exclude_id)
            t_conflict = qone(conn, t_sql, t_params)
            if t_conflict:
                return jsonify({"available": False, "conflict": t_conflict,
                                "message": f"Teacher already assigned to {t_conflict.get('faculty_name','?')} Sem {t_conflict.get('semester','?')} — {t_conflict.get('subject_name','?')} at this time"})

    return jsonify({"available": True})

@app.route("/api/timetable", methods=["POST"])
@require_auth
def create_timetable_entry():
    d = request.json or {}
    faculty_id   = d.get("faculty_id")
    semester     = d.get("semester")
    day_of_week  = d.get("day_of_week")
    time_slot_id = d.get("time_slot_id")
    teacher_id   = d.get("teacher_id")
    subject_id   = d.get("subject_id")
    if not all([faculty_id, semester, day_of_week, time_slot_id]):
        return jsonify({"error": "faculty_id, semester, day_of_week, time_slot_id required"}), 400
    with get_db() as conn:
        # Block if teacher is already teaching at this day+time in any other class
        if teacher_id:
            clash = qone(conn, """
                SELECT cs.id, f.name AS faculty_name, cs.semester, s.name AS subject_name
                FROM class_schedules cs
                LEFT JOIN faculties f ON f.id = cs.faculty_id
                LEFT JOIN subjects  s ON s.id = cs.subject_id
                WHERE cs.teacher_id=%s AND cs.day_of_week=%s AND cs.time_slot_id=%s
            """, (teacher_id, day_of_week, time_slot_id))
            if clash:
                return jsonify({
                    "error": f"Teacher conflict — already assigned to {clash.get('faculty_name','?')} Sem {clash.get('semester','?')} ({clash.get('subject_name','?')}) at this time"
                }), 409
        try:
            row = qone(conn, """
                INSERT INTO class_schedules
                    (faculty_id, semester, day_of_week, time_slot_id, teacher_id, subject_id)
                VALUES (%s,%s,%s,%s,%s,%s)
                RETURNING id, faculty_id, semester, day_of_week, time_slot_id, teacher_id, subject_id
            """, (faculty_id, semester, day_of_week, time_slot_id, teacher_id, subject_id))
        except Exception as e:
            if "unique" in str(e).lower():
                return jsonify({"error": "Timetable collision — slot already occupied"}), 409
            raise
    _log_activity(g.user["username"], "create_timetable", "schedule", target_id=str(row["id"]))
    return jsonify({"entry": row}), 201

@app.route("/api/timetable/<int:entry_id>", methods=["DELETE"])
@require_auth
def delete_timetable_entry(entry_id):
    with get_db() as conn:
        row = qone(conn, "DELETE FROM class_schedules WHERE id=%s RETURNING id", (entry_id,))
    if not row: return jsonify({"error": "Entry not found"}), 404
    _log_activity(g.user["username"], "delete_timetable", "schedule", target_id=str(entry_id))
    return jsonify({"deleted": True})

# ══════════════════════════════════════════════════════════════════════════
#  TEACHER — own profile, schedule, today's classes
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/teacher/me")
@require_auth
def teacher_me():
    if g.user["role"] != "teacher":
        return jsonify({"error": "Teacher access only"}), 403
    tid = _tid()
    with get_db() as conn:
        t = qone(conn,
            "SELECT id, teacher_id, full_name, email, phone, status FROM teachers WHERE id=%s", (tid,))
        if not t: return jsonify({"error": "Not found"}), 404
        assignments = qall(conn, """
            SELECT ta.id, ta.faculty_id, f.name AS faculty_name, f.code AS faculty_code,
                   ta.semester, ta.subject_id, s.name AS subject_name, s.code AS subject_code,
                   ta.time_slot_id, ts.label AS time_slot_label,
                   ts.start_time::text, ts.end_time::text, ta.day_of_week,
                   (SELECT COUNT(*) FROM students st
                    WHERE st.faculty_id = ta.faculty_id
                      AND st.semester::text = ta.semester::text
                      AND st.status = 'active') AS student_count
            FROM teacher_assignments ta
            LEFT JOIN faculties  f  ON f.id  = ta.faculty_id
            LEFT JOIN subjects   s  ON s.id  = ta.subject_id
            LEFT JOIN time_slots ts ON ts.id = ta.time_slot_id
            WHERE ta.teacher_id = %s
            ORDER BY CASE ta.day_of_week
                WHEN 'Mon' THEN 0 WHEN 'Tue' THEN 1 WHEN 'Wed' THEN 2
                WHEN 'Thu' THEN 3 WHEN 'Fri' THEN 4 WHEN 'Sat' THEN 5
                ELSE 6 END, ts.start_time
        """, (tid,))
    t["assignments"] = assignments
    return jsonify({"teacher": t})

@app.route("/api/teacher/me/today")
@require_auth
def teacher_today():
    if g.user["role"] != "teacher":
        return jsonify({"error": "Teacher access only"}), 403
    today_name = datetime.now().strftime("%a")   # Mon, Tue …
    today_str  = date.today().isoformat()
    tid        = _tid()
    with get_db() as conn:
        classes = qall(conn, """
            SELECT ta.id AS assignment_id,
                   ta.faculty_id, f.name AS faculty_name, f.code AS faculty_code,
                   ta.semester, ta.subject_id, s.name AS subject_name, s.code AS subject_code,
                   ta.time_slot_id, ts.label AS time_slot_label,
                   ts.start_time::text, ts.end_time::text, ta.day_of_week,
                   (SELECT COUNT(*) FROM students st
                    WHERE st.faculty_id = ta.faculty_id
                      AND st.semester::text = ta.semester::text
                      AND st.status = 'active') AS student_count
            FROM teacher_assignments ta
            LEFT JOIN faculties  f  ON f.id  = ta.faculty_id
            LEFT JOIN subjects   s  ON s.id  = ta.subject_id
            LEFT JOIN time_slots ts ON ts.id = ta.time_slot_id
            WHERE ta.teacher_id = %s AND ta.day_of_week = %s
            ORDER BY ts.start_time
        """, (tid, today_name))
        for cls in classes:
            sess = qone(conn, """
                SELECT id, status, method,
                       (SELECT COUNT(*) FROM attendance a
                        WHERE a.session_id = s2.id AND a.status='Present') AS marked_count
                FROM attendance_sessions s2
                WHERE teacher_id=%s AND subject_id=%s AND session_date=%s
                ORDER BY created_at DESC LIMIT 1
            """, (tid, cls.get("subject_id"), today_str))
            cls["session"] = sess
    return jsonify({"classes": classes, "day": today_name, "date": today_str})

@app.route("/api/teacher/me/schedule")
@require_auth
def teacher_schedule():
    if g.user["role"] != "teacher":
        return jsonify({"error": "Teacher access only"}), 403
    tid = _tid()
    with get_db() as conn:
        assignments = qall(conn, """
            SELECT ta.id AS assignment_id,
                   ta.faculty_id, f.name AS faculty_name, f.code AS faculty_code,
                   ta.semester, ta.subject_id, s.name AS subject_name, s.code AS subject_code,
                   ta.time_slot_id, ts.label AS time_slot_label,
                   ts.start_time::text, ts.end_time::text, ta.day_of_week
            FROM teacher_assignments ta
            LEFT JOIN faculties  f  ON f.id  = ta.faculty_id
            LEFT JOIN subjects   s  ON s.id  = ta.subject_id
            LEFT JOIN time_slots ts ON ts.id = ta.time_slot_id
            WHERE ta.teacher_id = %s
            ORDER BY CASE ta.day_of_week
                WHEN 'Mon' THEN 0 WHEN 'Tue' THEN 1 WHEN 'Wed' THEN 2
                WHEN 'Thu' THEN 3 WHEN 'Fri' THEN 4 WHEN 'Sat' THEN 5
                ELSE 6 END, ts.start_time
        """, (tid,))
    schedule = {d: [] for d in DAYS}
    for a in assignments:
        day = a.get("day_of_week")
        if day in schedule:
            schedule[day].append(a)
        else:
            schedule.setdefault("Other", []).append(a)
    return jsonify({"schedule": schedule, "days": DAYS, "all": assignments})

@app.route("/api/teacher/me/stats")
@require_auth
def teacher_stats():
    if g.user["role"] != "teacher":
        return jsonify({"error": "Teacher access only"}), 403
    tid = _tid()
    with get_db() as conn:
        total_sessions = qone(conn,
            "SELECT COUNT(*) AS n FROM attendance_sessions WHERE teacher_id=%s", (tid,))
        today_marked = qone(conn, """
            SELECT COUNT(*) AS n FROM attendance a
            WHERE a.teacher_id=%s AND a.date=CURRENT_DATE AND a.status='Present'
        """, (tid,))
        class_count = qone(conn,
            "SELECT COUNT(DISTINCT (faculty_id, semester, subject_id)) AS n FROM teacher_assignments WHERE teacher_id=%s",
            (tid,))
    return jsonify({
        "total_sessions": int(total_sessions["n"]) if total_sessions else 0,
        "today_marked":   int(today_marked["n"])   if today_marked   else 0,
        "class_count":    int(class_count["n"])     if class_count    else 0,
    })

# ══════════════════════════════════════════════════════════════════════════
#  ATTENDANCE SESSIONS
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/attendance/sessions", methods=["POST"])
@require_auth
def create_attendance_session():
    if g.user["role"] not in ("teacher", "admin"):
        return jsonify({"error": "Access denied"}), 403
    d          = request.json or {}
    teacher_id = _tid() if g.user["role"] == "teacher" else (d.get("teacher_id") or g.user["id"])
    faculty_id = d.get("faculty_id")
    semester   = d.get("semester")
    subject_id = d.get("subject_id")
    if not all([faculty_id, semester, subject_id]):
        return jsonify({"error": "faculty_id, semester, subject_id required"}), 400
    sess_date  = d.get("session_date", date.today().isoformat())
    method     = d.get("method", "manual")
    with get_db() as conn:
        existing = qone(conn, """
            SELECT id, status FROM attendance_sessions
            WHERE teacher_id=%s AND subject_id=%s AND session_date=%s AND status='open'
        """, (teacher_id, subject_id, sess_date))
        if existing:
            return jsonify({"session": existing, "resumed": True})
        row = qone(conn, """
            INSERT INTO attendance_sessions
                (teacher_id, faculty_id, semester, subject_id, time_slot_id,
                 day_of_week, session_date, method)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id, teacher_id, faculty_id, semester, subject_id,
                      session_date::text, method, status
        """, (teacher_id, faculty_id, semester, subject_id,
              d.get("time_slot_id"), d.get("day_of_week"), sess_date, method))
    return jsonify({"session": row}), 201

@app.route("/api/attendance/sessions")
@require_auth
def list_attendance_sessions():
    where, vals = [], []
    if g.user["role"] == "teacher":
        where.append("s.teacher_id=%s"); vals.append(_tid())
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        rows = qall(conn, f"""
            SELECT s.id, s.session_date::text, s.method, s.status,
                   s.semester, s.day_of_week, s.created_at::text,
                   t.full_name AS teacher_name,
                   f.name AS faculty_name, f.code AS faculty_code,
                   sb.name AS subject_name, sb.code AS subject_code,
                   ts.label AS time_slot_label,
                   (SELECT COUNT(*) FROM attendance a
                    WHERE a.session_id=s.id AND a.status='Present') AS present_count,
                   (SELECT COUNT(*) FROM attendance a WHERE a.session_id=s.id) AS total_count
            FROM attendance_sessions s
            LEFT JOIN teachers   t  ON t.id  = s.teacher_id
            LEFT JOIN faculties  f  ON f.id  = s.faculty_id
            LEFT JOIN subjects   sb ON sb.id = s.subject_id
            LEFT JOIN time_slots ts ON ts.id = s.time_slot_id
            {clause}
            ORDER BY s.session_date DESC, s.created_at DESC LIMIT 100
        """, vals)
    return jsonify({"sessions": rows})

@app.route("/api/attendance/sessions/<int:session_id>")
@require_auth
def get_attendance_session(session_id):
    with get_db() as conn:
        session = qone(conn, """
            SELECT s.id, s.teacher_id, t.full_name AS teacher_name,
                   s.faculty_id, f.name AS faculty_name, f.code AS faculty_code,
                   s.semester, s.subject_id, sb.name AS subject_name,
                   s.session_date::text, s.method, s.status,
                   s.created_at::text, s.closed_at::text
            FROM attendance_sessions s
            LEFT JOIN teachers  t  ON t.id  = s.teacher_id
            LEFT JOIN faculties f  ON f.id  = s.faculty_id
            LEFT JOIN subjects  sb ON sb.id = s.subject_id
            WHERE s.id = %s
        """, (session_id,))
        if not session: return jsonify({"error": "Session not found"}), 404
        students = qall(conn, """
            SELECT st.student_id, st.full_name, st.department, st.semester,
                   COALESCE(a.status,'Absent') AS status,
                   a.time::text AS att_time, a.note
            FROM students st
            LEFT JOIN attendance a ON a.student_id=st.student_id
                AND a.date=%s AND a.session_id=%s
            WHERE st.faculty_id=%s
              AND st.status='active'
              AND (%s::text IS NULL OR st.semester=%s::text)
            ORDER BY st.full_name
        """, (session["session_date"], session_id, session["faculty_id"],
              str(session["semester"]), str(session["semester"])))
    session["students"] = students
    return jsonify({"session": session})

@app.route("/api/attendance/sessions/<int:session_id>/close", methods=["PUT"])
@require_auth
def close_attendance_session(session_id):
    with get_db() as conn:
        row = qone(conn, """
            UPDATE attendance_sessions SET status='closed', closed_at=NOW()
            WHERE id=%s RETURNING id, status, closed_at::text
        """, (session_id,))
    if not row: return jsonify({"error": "Session not found"}), 404
    return jsonify({"session": row})

@app.route("/api/attendance/sessions/<int:session_id>/mark", methods=["POST"])
@require_auth
def mark_session_attendance(session_id):
    d          = request.json or {}
    student_id = d.get("student_id")
    status     = d.get("status", "Present")
    note       = d.get("note", "")
    if not student_id:
        return jsonify({"error": "student_id required"}), 400
    with get_db() as conn:
        sess = qone(conn,
            "SELECT session_date, subject_id, teacher_id, status FROM attendance_sessions WHERE id=%s",
            (session_id,))
        if not sess: return jsonify({"error": "Session not found"}), 404
        if sess["status"] == "closed": return jsonify({"error": "Session closed"}), 400
        with conn.cursor() as c:
            # Use per-subject unique index when subject_id is known
            if sess.get("subject_id"):
                c.execute("""
                    INSERT INTO attendance (student_id, date, time, status, note, subject_id, teacher_id, session_id)
                    VALUES (%s,%s,CURRENT_TIME,%s,%s,%s,%s,%s)
                    ON CONFLICT (student_id, subject_id, date) WHERE subject_id IS NOT NULL
                    DO UPDATE SET status=EXCLUDED.status, note=EXCLUDED.note, time=EXCLUDED.time
                """, (student_id, sess["session_date"], status, note,
                      sess["subject_id"], sess["teacher_id"], session_id))
            else:
                c.execute("""
                    INSERT INTO attendance (student_id, date, time, status, note, teacher_id, session_id)
                    VALUES (%s,%s,CURRENT_TIME,%s,%s,%s,%s)
                    ON CONFLICT (student_id, date) WHERE subject_id IS NULL
                    DO UPDATE SET status=EXCLUDED.status, note=EXCLUDED.note, time=EXCLUDED.time
                """, (student_id, sess["session_date"], status, note,
                      sess["teacher_id"], session_id))
    return jsonify({"marked": True, "student_id": student_id, "status": status})

@app.route("/api/attendance/sessions/<int:session_id>/bulk", methods=["POST"])
@require_auth
def bulk_mark_session(session_id):
    d       = request.json or {}
    records = d.get("records", [])
    with get_db() as conn:
        sess = qone(conn,
            "SELECT session_date, subject_id, teacher_id, status FROM attendance_sessions WHERE id=%s",
            (session_id,))
        if not sess: return jsonify({"error": "Session not found"}), 404
        if sess["status"] == "closed": return jsonify({"error": "Session closed"}), 400
        newly = 0
        for rec in records:
            sid    = rec.get("student_id")
            status = rec.get("status", "Absent")
            note   = rec.get("note", "")
            if not sid: continue
            with conn.cursor() as c:
                if sess.get("subject_id"):
                    c.execute("""
                        INSERT INTO attendance (student_id, date, time, status, note, subject_id, teacher_id, session_id)
                        VALUES (%s,%s,CURRENT_TIME,%s,%s,%s,%s,%s)
                        ON CONFLICT (student_id, subject_id, date) WHERE subject_id IS NOT NULL
                        DO UPDATE SET status=EXCLUDED.status, note=EXCLUDED.note, time=EXCLUDED.time
                        RETURNING (xmax = 0) AS inserted
                    """, (sid, sess["session_date"], status, note,
                          sess["subject_id"], sess["teacher_id"], session_id))
                else:
                    c.execute("""
                        INSERT INTO attendance (student_id, date, time, status, note, teacher_id, session_id)
                        VALUES (%s,%s,CURRENT_TIME,%s,%s,%s,%s)
                        ON CONFLICT (student_id, date) WHERE subject_id IS NULL
                        DO UPDATE SET status=EXCLUDED.status, note=EXCLUDED.note, time=EXCLUDED.time
                        RETURNING (xmax = 0) AS inserted
                    """, (sid, sess["session_date"], status, note,
                          sess["teacher_id"], session_id))
                row = c.fetchone()
                if row and row[0]: newly += 1
        qexec(conn, "UPDATE attendance_sessions SET status='closed', closed_at=NOW() WHERE id=%s", (session_id,))
    return jsonify({"submitted": True, "newly_marked": newly})

# ══════════════════════════════════════════════════════════════════════════
#  BATCH RECOGNITION  (classroom photo → multiple faces)
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/recognize/batch", methods=["POST"])
@require_auth
def batch_recognize():
    data    = request.json or {}
    img_b64 = data.get("image")
    if not img_b64: return jsonify({"error": "No image"}), 400
    frame = decode_image(img_b64)
    if frame is None: return jsonify({"error": "Cannot decode image"}), 400
    fa    = get_face_app()
    rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    faces = fa.get(rgb)
    if not faces:
        return jsonify({"recognized": [], "unknown": 0, "message": "No faces detected"})
    results, unknown = [], 0
    with get_db() as conn:
        for face in faces:
            emb  = face.normed_embedding
            bbox = [int(x) for x in face.bbox]
            sid, name, sim = find_best_match(conn, emb)
            if sim >= THRESHOLD and sid:
                results.append({"student_id": sid, "name": name,
                                "confidence": round(sim*100, 1), "bbox": bbox,
                                "status": "Present"})
            else:
                unknown += 1
    return jsonify({"recognized": results, "unknown": unknown, "total_faces": len(faces)})

# ══════════════════════════════════════════════════════════════════════════
#  ROLE-SCOPED REPORTS
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/reports/my-attendance")
@require_auth
def my_attendance_report():
    from_d     = request.args.get("from", date.today().isoformat())
    to_d       = request.args.get("to",   date.today().isoformat())
    subject_id = request.args.get("subject_id", "")
    dept       = request.args.get("department", "")

    sql    = """
        SELECT s.student_id, s.full_name, s.department, s.semester,
               COUNT(a.id) FILTER(WHERE a.status='Present') AS present,
               COUNT(a.id) AS total,
               ROUND(100.0 * COUNT(a.id) FILTER(WHERE a.status='Present') /
                     NULLIF(COUNT(a.id),0), 1) AS pct
        FROM students s
        LEFT JOIN attendance a ON a.student_id=s.student_id
            AND a.date BETWEEN %s AND %s
    """
    params = [from_d, to_d]
    where  = []

    if g.user["role"] == "teacher":
        tid = _tid()
        where.append("""s.faculty_id IN (
            SELECT DISTINCT ta.faculty_id FROM teacher_assignments ta
            WHERE ta.teacher_id=%s
        )""")
        params.append(tid)
        if subject_id:
            where.append("a.subject_id=%s"); params.append(int(subject_id))
    else:
        if dept:
            where.append("s.department=%s"); params.append(dept)

    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY s.student_id,s.full_name,s.department,s.semester ORDER BY pct DESC NULLS LAST"

    with get_db() as conn:
        rows = qall(conn, sql, params)
    return jsonify({"report": rows, "from": from_d, "to": to_d})

@app.route("/api/reports/session-summary")
@require_auth
def session_summary_report():
    """Per-session attendance summary, scoped to teacher."""
    where, vals = [], []
    if g.user["role"] == "teacher":
        where.append("s.teacher_id=%s"); vals.append(_tid())
    from_d = request.args.get("from", "")
    to_d   = request.args.get("to", "")
    if from_d: where.append("s.session_date>=%s"); vals.append(from_d)
    if to_d:   where.append("s.session_date<=%s"); vals.append(to_d)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with get_db() as conn:
        rows = qall(conn, f"""
            SELECT s.id, s.session_date::text, s.status, s.method,
                   t.full_name AS teacher_name,
                   f.name AS faculty_name, s.semester,
                   sb.name AS subject_name,
                   COUNT(a.id) FILTER(WHERE a.status='Present') AS present_count,
                   COUNT(a.id) AS total_count
            FROM attendance_sessions s
            LEFT JOIN teachers   t  ON t.id  = s.teacher_id
            LEFT JOIN faculties  f  ON f.id  = s.faculty_id
            LEFT JOIN subjects   sb ON sb.id = s.subject_id
            LEFT JOIN attendance a  ON a.session_id = s.id
            {clause}
            GROUP BY s.id,s.session_date,s.status,s.method,
                     t.full_name,f.name,s.semester,sb.name
            ORDER BY s.session_date DESC LIMIT 100
        """, vals)
    return jsonify({"sessions": rows})

@app.route("/api/reports/teacher-performance")
@require_auth
def teacher_performance_report():
    """Per-student attendance with risk detection, scoped to teacher's assigned faculty."""
    if g.user["role"] not in ("teacher", "admin"):
        return jsonify({"error": "Access denied"}), 403

    from_d     = request.args.get("from",
                    (date.today() - __import__("datetime").timedelta(days=30)).isoformat())
    to_d       = request.args.get("to",   date.today().isoformat())
    subject_id = request.args.get("subject_id", "")
    semester   = request.args.get("semester", "")
    faculty_id = request.args.get("faculty_id", "")

    student_where = ["s.status='active'"]
    att_where     = ["a.date BETWEEN %s AND %s"]
    # Keep JOIN-clause params and WHERE-clause params separate so they bind
    # in the correct positional order when the query is assembled.
    att_params = [from_d, to_d]   # extra params for att_where (JOIN clause)
    sw_params  = []                # params for student_where (WHERE clause)

    if g.user["role"] == "teacher":
        tid = _tid()
        if faculty_id:
            student_where.append("s.faculty_id=%s")
            sw_params.append(int(faculty_id))
        else:
            student_where.append("""s.faculty_id IN (
                SELECT DISTINCT ta.faculty_id FROM teacher_assignments ta
                WHERE ta.teacher_id=%s
            )""")
            sw_params.append(tid)
    else:
        if faculty_id:
            student_where.append("s.faculty_id=%s")
            sw_params.append(int(faculty_id))

    if semester:
        student_where.append("s.semester::text=%s")
        sw_params.append(str(semester))
    if subject_id:
        att_where.append("a.subject_id=%s")
        att_params.append(int(subject_id))

    # Final params must follow SQL order: JOIN conditions first, WHERE conditions second
    params = att_params + sw_params

    sw = " AND ".join(student_where)
    aw = " AND ".join(att_where)

    with get_db() as conn:
        rows = qall(conn, f"""
            SELECT s.student_id, s.full_name, s.department, s.semester,
                   COALESCE(SUM(CASE WHEN a.status='Present' THEN 1 ELSE 0 END), 0) AS present,
                   COALESCE(SUM(CASE WHEN a.status='Absent'  THEN 1 ELSE 0 END), 0) AS absent,
                   COALESCE(COUNT(a.id), 0) AS total,
                   CASE WHEN COUNT(a.id) = 0 THEN NULL
                        ELSE ROUND(100.0 * SUM(CASE WHEN a.status='Present' THEN 1 ELSE 0 END)
                             / COUNT(a.id), 1)
                   END AS pct,
                   MAX(CASE WHEN a.status='Absent' THEN a.date::text END) AS last_absent
            FROM students s
            LEFT JOIN attendance a
                ON a.student_id = s.student_id AND {aw}
            WHERE {sw}
            GROUP BY s.student_id, s.full_name, s.department, s.semester
            ORDER BY pct ASC NULLS LAST, s.full_name
        """, params)

        # Faculties and subjects for this teacher (for filter dropdowns)
        faculties = []
        subjects  = []
        if g.user["role"] == "teacher":
            faculties = qall(conn, """
                SELECT DISTINCT f.id, f.name, f.code
                FROM teacher_assignments ta
                JOIN faculties f ON f.id = ta.faculty_id
                WHERE ta.teacher_id = %s
                ORDER BY f.name
            """, (_tid(),))
            subjects = qall(conn, """
                SELECT DISTINCT sb.id, sb.name, sb.code
                FROM teacher_assignments ta
                JOIN subjects sb ON sb.id = ta.subject_id
                WHERE ta.teacher_id = %s
                ORDER BY sb.name
            """, (_tid(),))

    return jsonify({
        "students":  rows,
        "from":      from_d,
        "to":        to_d,
        "faculties": faculties,
        "subjects":  subjects,
    })

# ══════════════════════════════════════════════════════════════════════════
#  BULK STUDENT IMPORT  (CSV upload)
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/students/import", methods=["POST"])
@require_auth
def import_students():
    """
    Accept a CSV file or JSON array of student records.
    CSV columns: student_id, full_name, email, phone, department, faculty_id, semester, status
    Returns: { created, updated, failed: [{row, reason}] }
    """
    if g.user.get("role") != "admin":
        return jsonify({"error": "Admin access only"}), 403

    # Accept both multipart file and raw JSON
    if request.content_type and "multipart" in request.content_type:
        f = request.files.get("file")
        if not f:
            return jsonify({"error": "No file uploaded"}), 400
        raw = f.read().decode("utf-8-sig")   # strip BOM if present
        reader = csv.DictReader(io.StringIO(raw))
        rows = list(reader)
    else:
        rows = request.json or []

    created, updated, failed = 0, 0, []
    ALLOWED_STATUS = {"active", "inactive", "graduated", "suspended"}

    with get_db(register_pgvector=False) as conn:
        for i, row in enumerate(rows, start=2):   # row 1 = header
            sid = (row.get("student_id") or "").strip()
            name = (row.get("full_name") or "").strip()
            if not sid or not name:
                failed.append({"row": i, "reason": "student_id and full_name are required"})
                continue

            email  = (row.get("email") or "").strip() or None
            phone  = (row.get("phone") or "").strip() or None
            dept   = (row.get("department") or "").strip() or None
            sem    = (row.get("semester") or "").strip() or None
            status = (row.get("status") or "active").strip().lower()
            fid    = row.get("faculty_id") or None

            if status not in ALLOWED_STATUS:
                status = "active"
            if fid:
                try: fid = int(fid)
                except ValueError: fid = None

            if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
                failed.append({"row": i, "sid": sid, "reason": "Invalid email format"})
                continue

            try:
                r = qone(conn, """
                    INSERT INTO students (student_id, full_name, email, phone, department, faculty_id, semester, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (student_id) DO UPDATE SET
                        full_name  = EXCLUDED.full_name,
                        email      = COALESCE(EXCLUDED.email, students.email),
                        phone      = COALESCE(EXCLUDED.phone, students.phone),
                        department = COALESCE(EXCLUDED.department, students.department),
                        faculty_id = COALESCE(EXCLUDED.faculty_id, students.faculty_id),
                        semester   = COALESCE(EXCLUDED.semester, students.semester),
                        status     = EXCLUDED.status
                    RETURNING (xmax = 0) AS inserted
                """, (sid, name, email, phone, dept, fid, sem, status))
                if r and r["inserted"]: created += 1
                else: updated += 1
            except Exception as e:
                failed.append({"row": i, "sid": sid, "reason": str(e)})

    _log_activity(g.user["username"], "bulk_import_students", "student",
                  detail=f"created={created} updated={updated} failed={len(failed)}")
    return jsonify({"created": created, "updated": updated,
                    "failed": failed, "total": len(rows)})


# ══════════════════════════════════════════════════════════════════════════
#  ACADEMIC CALENDAR — Holidays + Academic Years
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/calendar/academic-years")
@require_auth
def list_academic_years():
    with get_db(register_pgvector=False) as conn:
        rows = qall(conn,
            "SELECT id, name, start_date::text, end_date::text, is_current "
            "FROM academic_years ORDER BY start_date DESC")
    return jsonify({"academic_years": rows})

@app.route("/api/calendar/academic-years", methods=["POST"])
@require_auth
def create_academic_year():
    if g.user.get("role") != "admin":
        return jsonify({"error": "Admin access only"}), 403
    d = request.json or {}
    if not all([d.get("name"), d.get("start_date"), d.get("end_date")]):
        return jsonify({"error": "name, start_date, end_date required"}), 400
    with get_db(register_pgvector=False) as conn:
        if d.get("is_current"):
            qexec(conn, "UPDATE academic_years SET is_current=false")
        row = qone(conn, """
            INSERT INTO academic_years (name, start_date, end_date, is_current)
            VALUES (%s,%s,%s,%s)
            RETURNING id, name, start_date::text, end_date::text, is_current
        """, (d["name"], d["start_date"], d["end_date"], bool(d.get("is_current"))))
    _log_activity(g.user["username"], "create_academic_year", "calendar", target_id=d["name"])
    return jsonify({"academic_year": row}), 201

@app.route("/api/calendar/academic-years/<int:yid>", methods=["PUT"])
@require_auth
def update_academic_year(yid):
    if g.user.get("role") != "admin":
        return jsonify({"error": "Admin access only"}), 403
    d = request.json or {}
    with get_db(register_pgvector=False) as conn:
        if d.get("is_current"):
            qexec(conn, "UPDATE academic_years SET is_current=false")
        fields, vals = [], []
        for col in ["name", "start_date", "end_date", "is_current"]:
            if col in d:
                fields.append(f"{col}=%s"); vals.append(d[col])
        if not fields: return jsonify({"error": "Nothing to update"}), 400
        vals.append(yid)
        row = qone(conn,
            f"UPDATE academic_years SET {','.join(fields)} WHERE id=%s "
            "RETURNING id, name, start_date::text, end_date::text, is_current", vals)
    if not row: return jsonify({"error": "Not found"}), 404
    return jsonify({"academic_year": row})

@app.route("/api/calendar/academic-years/<int:yid>", methods=["DELETE"])
@require_auth
def delete_academic_year(yid):
    if g.user.get("role") != "admin":
        return jsonify({"error": "Admin access only"}), 403
    with get_db(register_pgvector=False) as conn:
        row = qone(conn, "DELETE FROM academic_years WHERE id=%s RETURNING id", (yid,))
    if not row: return jsonify({"error": "Not found"}), 404
    return jsonify({"deleted": True})

@app.route("/api/calendar/holidays")
@require_auth
def list_holidays():
    year_id = request.args.get("academic_year_id", "")
    with get_db(register_pgvector=False) as conn:
        if year_id:
            rows = qall(conn,
                "SELECT id, date::text, name, academic_year_id FROM holidays "
                "WHERE academic_year_id=%s ORDER BY date", (int(year_id),))
        else:
            rows = qall(conn,
                "SELECT id, date::text, name, academic_year_id FROM holidays ORDER BY date")
    return jsonify({"holidays": rows})

@app.route("/api/calendar/holidays", methods=["POST"])
@require_auth
def create_holiday():
    if g.user.get("role") != "admin":
        return jsonify({"error": "Admin access only"}), 403
    d = request.json or {}
    name = d.get("name", "").strip()
    # Support both legacy "date" field and new "from_date"/"to_date"
    from_date = d.get("from_date") or d.get("date")
    to_date = d.get("to_date") or from_date
    if not from_date or not name:
        return jsonify({"error": "name and from_date are required"}), 400
    if to_date < from_date:
        return jsonify({"error": "to_date must be on or after from_date"}), 400
    year_id = d.get("academic_year_id") or None

    from datetime import date as _date, timedelta
    try:
        start = _date.fromisoformat(from_date)
        end = _date.fromisoformat(to_date)
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400

    added = []
    with get_db(register_pgvector=False) as conn:
        current = start
        while current <= end:
            date_str = current.isoformat()
            # Check for existing holiday on this date (handle NULL academic_year_id explicitly)
            if year_id:
                existing = qone(conn,
                    "SELECT id FROM holidays WHERE date=%s AND academic_year_id=%s",
                    (date_str, year_id))
            else:
                existing = qone(conn,
                    "SELECT id FROM holidays WHERE date=%s AND academic_year_id IS NULL",
                    (date_str,))
            if not existing:
                row = qone(conn, """
                    INSERT INTO holidays (date, name, academic_year_id)
                    VALUES (%s,%s,%s)
                    RETURNING id, date::text, name, academic_year_id
                """, (date_str, name, year_id))
                if row:
                    added.append(row)
            current += timedelta(days=1)
    if not added:
        return jsonify({"error": "All dates in the range already have a holiday"}), 409
    _log_activity(g.user["username"], "create_holiday", "calendar", target_id=f"{from_date}..{to_date}")
    return jsonify({"holidays": added, "added": len(added)}), 201

@app.route("/api/calendar/holidays/<int:hid>", methods=["DELETE"])
@require_auth
def delete_holiday(hid):
    if g.user.get("role") != "admin":
        return jsonify({"error": "Admin access only"}), 403
    with get_db(register_pgvector=False) as conn:
        row = qone(conn, "DELETE FROM holidays WHERE id=%s RETURNING id", (hid,))
    if not row: return jsonify({"error": "Not found"}), 404
    return jsonify({"deleted": True})


# ══════════════════════════════════════════════════════════════════════════
#  DEFAULTER LIST
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/reports/defaulters")
@require_auth
def defaulter_list():
    """
    Students below the minimum attendance threshold, per subject.
    Query params: threshold (default 75), faculty_id, semester, subject_id, academic_year_id
    """
    threshold  = float(request.args.get("threshold", 75))
    faculty_id = request.args.get("faculty_id", "")
    semester   = request.args.get("semester", "")
    subject_id = request.args.get("subject_id", "")
    year_id    = request.args.get("academic_year_id", "")

    # Build working-day count (total class days minus holidays)
    holiday_clause = ""
    holiday_params: list = []
    if year_id:
        holiday_clause = "AND a.date NOT IN (SELECT date FROM holidays WHERE academic_year_id=%s)"
        holiday_params = [int(year_id)]

    student_where = ["s.status='active'"]
    sw_params: list = []
    if faculty_id:
        student_where.append("s.faculty_id=%s"); sw_params.append(int(faculty_id))
    if semester:
        student_where.append("s.semester::text=%s"); sw_params.append(str(semester))

    att_where = ["1=1"]
    att_params: list = []
    if subject_id:
        att_where.append("a.subject_id=%s"); att_params.append(int(subject_id))

    sw = " AND ".join(student_where)
    aw = " AND ".join(att_where)

    params = att_params + holiday_params + sw_params + [threshold]

    with get_db(register_pgvector=False) as conn:
        rows = qall(conn, f"""
            SELECT s.student_id, s.full_name, s.department, s.semester,
                   sb.id AS subject_id, sb.name AS subject_name, sb.code AS subject_code,
                   COUNT(a.id) FILTER(WHERE a.status='Present') AS present,
                   COUNT(a.id) AS total,
                   ROUND(100.0 * COUNT(a.id) FILTER(WHERE a.status='Present')
                         / NULLIF(COUNT(a.id),0), 1) AS pct
            FROM students s
            JOIN teacher_assignments ta ON ta.faculty_id=s.faculty_id
                AND ta.semester::text=s.semester::text
            JOIN subjects sb ON sb.id=ta.subject_id
            LEFT JOIN attendance a ON a.student_id=s.student_id
                AND a.subject_id=sb.id
                AND {aw} {holiday_clause}
            WHERE {sw}
            GROUP BY s.student_id, s.full_name, s.department, s.semester, sb.id, sb.name, sb.code
            HAVING COUNT(a.id) > 0
               AND ROUND(100.0 * COUNT(a.id) FILTER(WHERE a.status='Present')
                   / NULLIF(COUNT(a.id),0), 1) < %s
            ORDER BY pct ASC, s.full_name
        """, params)
    return jsonify({"defaulters": rows, "threshold": threshold, "count": len(rows)})


# ══════════════════════════════════════════════════════════════════════════
#  QR CODE ATTENDANCE
# ══════════════════════════════════════════════════════════════════════════

def _qr_sign(payload: str) -> str:
    """HMAC-SHA256 signature over the payload string."""
    return hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()

def _qr_verify(payload: str, sig: str) -> bool:
    expected = _qr_sign(payload)
    return hmac.compare_digest(expected, sig)

@app.route("/api/attendance/sessions/<int:session_id>/qr", methods=["POST"])
@require_auth
def generate_session_qr(session_id):
    """
    Generate a time-limited QR code token for a session.
    Returns: { token, expires_at, qr_data }  where qr_data is what the student scans.
    The QR payload is:  "SID:<session_id>:EXP:<unix_ts>"  + HMAC signature.
    """
    with get_db(register_pgvector=False) as conn:
        sess = qone(conn,
            "SELECT id, status FROM attendance_sessions WHERE id=%s", (session_id,))
    if not sess: return jsonify({"error": "Session not found"}), 404
    if sess["status"] == "closed": return jsonify({"error": "Session is closed"}), 400

    expiry_s = int(request.json.get("expiry_seconds", 90) if request.json else 90)
    expires_at = int(time.time()) + expiry_s
    payload = f"SID:{session_id}:EXP:{expires_at}"
    sig     = _qr_sign(payload)
    token   = base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()

    return jsonify({
        "token":      token,
        "expires_at": expires_at,
        "session_id": session_id,
        "qr_data":    f"vedanetram://checkin/{token}",
    })

@app.route("/api/attendance/qr-checkin", methods=["POST"])
@require_auth
def qr_checkin():
    """
    Student scans QR → sends token + their auth.
    Marks Present for the session's subject on the session's date.
    """
    if g.user.get("role") != "student":
        return jsonify({"error": "Student access only"}), 403

    d     = request.json or {}
    token = d.get("token", "").strip()
    if not token: return jsonify({"error": "token required"}), 400

    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        # Format: "SID:<session_id>:EXP:<unix_ts>:<hmac_sig>"
        payload, sig = decoded.rsplit(":", 1)
        segs    = payload.split(":")   # ["SID", id, "EXP", ts]
        sid_val = segs[1]
        exp_val = int(segs[3])
    except Exception:
        return jsonify({"error": "Invalid QR token"}), 400

    if not _qr_verify(payload, sig):
        return jsonify({"error": "Invalid QR signature"}), 400
    if int(time.time()) > exp_val:
        return jsonify({"error": "QR code has expired"}), 400

    session_id = int(sid_val)
    student_id = g.user.get("student_id") or g.user.get("username")

    with get_db() as conn:
        sess = qone(conn,
            "SELECT session_date, subject_id, teacher_id, status, faculty_id FROM attendance_sessions WHERE id=%s",
            (session_id,))
        if not sess: return jsonify({"error": "Session not found"}), 404
        if sess["status"] == "closed": return jsonify({"error": "Session is closed"}), 400

        # Verify student belongs to this faculty
        stu = qone(conn, "SELECT faculty_id, semester FROM students WHERE student_id=%s", (student_id,))
        if not stu: return jsonify({"error": "Student not found"}), 404
        if stu["faculty_id"] != sess["faculty_id"]:
            return jsonify({"error": "You are not enrolled in this faculty"}), 403

        with conn.cursor() as c:
            if sess.get("subject_id"):
                c.execute("""
                    INSERT INTO attendance (student_id, date, time, status, subject_id, teacher_id, session_id)
                    VALUES (%s,%s,CURRENT_TIME,'Present',%s,%s,%s)
                    ON CONFLICT (student_id, subject_id, date) WHERE subject_id IS NOT NULL
                    DO NOTHING
                """, (student_id, sess["session_date"],
                      sess["subject_id"], sess["teacher_id"], session_id))
                marked = c.rowcount == 1
            else:
                c.execute("""
                    INSERT INTO attendance (student_id, date, time, status, teacher_id, session_id)
                    VALUES (%s,%s,CURRENT_TIME,'Present',%s,%s)
                    ON CONFLICT (student_id, date) WHERE subject_id IS NULL
                    DO NOTHING
                """, (student_id, sess["session_date"], sess["teacher_id"], session_id))
                marked = c.rowcount == 1

    return jsonify({
        "marked":     marked,
        "already":    not marked,
        "session_id": session_id,
        "date":       str(sess["session_date"]),
    })


# ══════════════════════════════════════════════════════════════════════════
#  ATTENDANCE CORRECTION REQUESTS  (student → teacher approval)
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/corrections", methods=["GET"])
@require_auth
def list_corrections():
    role = g.user.get("role")
    with get_db(register_pgvector=False) as conn:
        if role == "student":
            student_id = g.user.get("student_id") or g.user.get("username")
            rows = qall(conn, """
                SELECT cr.id, cr.student_id, cr.date::text, cr.reason, cr.status,
                       cr.review_note, cr.reviewed_at::text, cr.created_at::text,
                       s.name AS subject_name, s.code AS subject_code
                FROM correction_requests cr
                LEFT JOIN subjects s ON s.id=cr.subject_id
                WHERE cr.student_id=%s ORDER BY cr.created_at DESC
            """, (student_id,))
        elif role == "teacher":
            tid = _tid()
            rows = qall(conn, """
                SELECT cr.id, cr.student_id, st.full_name AS student_name,
                       cr.date::text, cr.reason, cr.status,
                       cr.review_note, cr.reviewed_at::text, cr.created_at::text,
                       s.name AS subject_name, s.code AS subject_code
                FROM correction_requests cr
                JOIN students st ON st.student_id=cr.student_id
                LEFT JOIN subjects s ON s.id=cr.subject_id
                WHERE cr.subject_id IN (
                    SELECT subject_id FROM teacher_assignments WHERE teacher_id=%s
                )
                ORDER BY cr.status='pending' DESC, cr.created_at DESC
            """, (tid,))
        else:  # admin
            rows = qall(conn, """
                SELECT cr.id, cr.student_id, st.full_name AS student_name,
                       cr.date::text, cr.reason, cr.status,
                       cr.review_note, cr.reviewed_at::text, cr.created_at::text,
                       s.name AS subject_name, s.code AS subject_code
                FROM correction_requests cr
                JOIN students st ON st.student_id=cr.student_id
                LEFT JOIN subjects s ON s.id=cr.subject_id
                ORDER BY cr.status='pending' DESC, cr.created_at DESC LIMIT 200
            """)
    return jsonify({"corrections": rows})

@app.route("/api/corrections", methods=["POST"])
@require_auth
def create_correction():
    """Student submits a correction request."""
    if g.user.get("role") != "student":
        return jsonify({"error": "Student access only"}), 403
    d          = request.json or {}
    date_val   = d.get("date", "").strip()
    reason     = d.get("reason", "").strip()
    subject_id = d.get("subject_id")
    student_id = g.user.get("student_id") or g.user.get("username")
    if not date_val or not reason:
        return jsonify({"error": "date and reason required"}), 400
    with get_db(register_pgvector=False) as conn:
        existing = qone(conn, """
            SELECT id FROM correction_requests
            WHERE student_id=%s AND date=%s AND (subject_id=%s OR (%s IS NULL AND subject_id IS NULL))
              AND status='pending'
        """, (student_id, date_val, subject_id, subject_id))
        if existing:
            return jsonify({"error": "A pending correction request already exists for this date/subject"}), 409
        row = qone(conn, """
            INSERT INTO correction_requests (student_id, subject_id, date, reason)
            VALUES (%s,%s,%s,%s)
            RETURNING id, student_id, subject_id, date::text, reason, status, created_at::text
        """, (student_id, subject_id or None, date_val, reason))
    return jsonify({"correction": row}), 201

@app.route("/api/corrections/<int:cid>", methods=["PUT"])
@require_auth
def review_correction(cid):
    """Teacher or admin approves/rejects a correction request."""
    if g.user.get("role") not in ("teacher", "admin"):
        return jsonify({"error": "Teacher or admin access only"}), 403
    d      = request.json or {}
    status = d.get("status", "").strip()
    note   = d.get("review_note", "").strip()
    if status not in ("approved", "rejected"):
        return jsonify({"error": "status must be approved or rejected"}), 400

    # reviewed_by references teachers(id) — NULL for admin (admin is not a teacher)
    reviewer_id = _tid() if g.user.get("role") == "teacher" else None

    with get_db() as conn:
        cr = qone(conn, """
            SELECT id, student_id, subject_id, date FROM correction_requests
            WHERE id=%s AND status='pending'
        """, (cid,))
        if not cr: return jsonify({"error": "Request not found or already reviewed"}), 404

        qexec(conn, """
            UPDATE correction_requests
            SET status=%s, review_note=%s, reviewed_by=%s, reviewed_at=NOW()
            WHERE id=%s
        """, (status, note, reviewer_id, cid))

        if status == "approved":
            # Apply the correction: mark Present for that date/subject
            if cr.get("subject_id"):
                qexec(conn, """
                    INSERT INTO attendance (student_id, date, time, status, note, subject_id)
                    VALUES (%s,%s,CURRENT_TIME,'Present',%s,%s)
                    ON CONFLICT (student_id, subject_id, date) WHERE subject_id IS NOT NULL
                    DO UPDATE SET status='Present', note=EXCLUDED.note
                """, (cr["student_id"], cr["date"],
                      f"Correction approved (ID:{cid})", cr["subject_id"]))
            else:
                qexec(conn, """
                    INSERT INTO attendance (student_id, date, time, status, note)
                    VALUES (%s,%s,CURRENT_TIME,'Present',%s)
                    ON CONFLICT (student_id, date) WHERE subject_id IS NULL
                    DO UPDATE SET status='Present', note=EXCLUDED.note
                """, (cr["student_id"], cr["date"], f"Correction approved (ID:{cid})"))

            # Audit trail
            qexec(conn, """
                INSERT INTO attendance_history
                    (student_id, subject_id, date, old_status, new_status, changed_by, reason)
                VALUES (%s,%s,%s,'Absent','Present',%s,%s)
            """, (cr["student_id"], cr.get("subject_id"), cr["date"],
                  g.user["username"], f"Correction request #{cid} approved"))

    _log_activity(g.user["username"], f"correction_{status}", "attendance", str(cid))
    return jsonify({"updated": True, "status": status})


# ══════════════════════════════════════════════════════════════════════════
#  STUDENT SELF-SERVICE (per-subject attendance view)
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/student/me/attendance")
@require_auth
def student_my_attendance():
    """Student's own per-subject attendance breakdown."""
    if g.user.get("role") != "student":
        return jsonify({"error": "Student access only"}), 403
    student_id = g.user.get("student_id") or g.user.get("username")
    from_d = request.args.get("from", (date.today() - timedelta(days=90)).isoformat())
    to_d   = request.args.get("to", date.today().isoformat())

    with get_db(register_pgvector=False) as conn:
        by_subject = qall(conn, """
            SELECT sb.id AS subject_id, sb.name AS subject_name, sb.code AS subject_code,
                   COUNT(a.id) FILTER(WHERE a.status='Present') AS present,
                   COUNT(a.id) AS total,
                   ROUND(100.0 * COUNT(a.id) FILTER(WHERE a.status='Present')
                         / NULLIF(COUNT(a.id),0), 1) AS pct
            FROM subjects sb
            JOIN attendance a ON a.subject_id=sb.id
                AND a.student_id=%s AND a.date BETWEEN %s AND %s
            GROUP BY sb.id, sb.name, sb.code
            ORDER BY pct ASC NULLS LAST
        """, (student_id, from_d, to_d))

        recent = qall(conn, """
            SELECT a.date::text, a.time::text, a.status, a.note,
                   sb.name AS subject_name, sb.code AS subject_code
            FROM attendance a
            LEFT JOIN subjects sb ON sb.id=a.subject_id
            WHERE a.student_id=%s AND a.date BETWEEN %s AND %s
            ORDER BY a.date DESC, a.time DESC LIMIT 60
        """, (student_id, from_d, to_d))

        overall = qone(conn, """
            SELECT COUNT(*) FILTER(WHERE status='Present') AS present,
                   COUNT(*) AS total,
                   ROUND(100.0 * COUNT(*) FILTER(WHERE status='Present')
                         / NULLIF(COUNT(*),0), 1) AS pct
            FROM attendance
            WHERE student_id=%s AND date BETWEEN %s AND %s
        """, (student_id, from_d, to_d))

    return jsonify({
        "by_subject": by_subject,
        "recent":     recent,
        "overall":    overall,
        "from":       from_d,
        "to":         to_d,
    })


# ══════════════════════════════════════════════════════════════════════════
#  ADMIN — Attendance history (audit trail for a student)
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/attendance/history/<sid>")
@require_auth
def attendance_change_history(sid):
    """Full audit trail of manual attendance changes for a student."""
    with get_db(register_pgvector=False) as conn:
        rows = qall(conn, """
            SELECT ah.id, ah.date::text, ah.old_status, ah.new_status,
                   ah.changed_by, ah.reason, ah.changed_at::text,
                   s.name AS subject_name, s.code AS subject_code
            FROM attendance_history ah
            LEFT JOIN subjects s ON s.id=ah.subject_id
            WHERE ah.student_id=%s ORDER BY ah.changed_at DESC LIMIT 100
        """, (sid,))
    return jsonify({"history": rows})


# ══════════════════════════════════════════════════════════════════════════
#  STUDENT PORTAL — Dashboard, Timetable, Notifications, Leave Requests
# ══════════════════════════════════════════════════════════════════════════

@app.route("/api/student/me/dashboard")
@require_auth
def student_dashboard():
    if g.user.get("role") != "student":
        return jsonify({"error": "Student access only"}), 403
    sid = g.user["student_id"]
    fac_id = g.user.get("faculty_id")
    sem = g.user.get("semester")
    today = date.today()
    day_abbr = today.strftime("%a")[:3]  # Mon, Tue…

    with get_db(register_pgvector=False) as conn:
        # Overall stats (all time)
        stats = qone(conn, """
            SELECT COUNT(*) FILTER(WHERE status='Present') AS present,
                   COUNT(*) AS total,
                   ROUND(100.0 * COUNT(*) FILTER(WHERE status='Present')
                         / NULLIF(COUNT(*),0), 1) AS pct
            FROM attendance WHERE student_id=%s
        """, (sid,))

        # Per-subject stats
        by_sub = qall(conn, """
            SELECT sb.id, sb.name AS subject_name, sb.code AS subject_code,
                   COUNT(a.id) FILTER(WHERE a.status='Present') AS present,
                   COUNT(a.id) AS total,
                   ROUND(100.0 * COUNT(a.id) FILTER(WHERE a.status='Present')
                         / NULLIF(COUNT(a.id),0), 1) AS pct
            FROM subjects sb
            JOIN attendance a ON a.subject_id=sb.id AND a.student_id=%s
            GROUP BY sb.id, sb.name, sb.code
            ORDER BY pct ASC NULLS LAST
        """, (sid,))

        # Today's classes from class_schedules
        today_classes = []
        if fac_id and sem:
            today_classes = qall(conn, """
                SELECT cs.id, ts.label, ts.start_time::text, ts.end_time::text,
                       s.name AS subject_name, s.code AS subject_code,
                       t.full_name AS teacher_name
                FROM class_schedules cs
                JOIN time_slots ts ON ts.id = cs.time_slot_id
                LEFT JOIN subjects s ON s.id = cs.subject_id
                LEFT JOIN teachers t ON t.id = cs.teacher_id
                WHERE cs.faculty_id=%s AND cs.semester=%s AND cs.day_of_week=%s
                ORDER BY ts.start_time
            """, (fac_id, sem, day_abbr))

        # Upcoming holidays (next 30 days)
        holidays = qall(conn, """
            SELECT name, date::text FROM holidays
            WHERE date >= %s AND date <= %s
            ORDER BY date LIMIT 5
        """, (today.isoformat(), (today + timedelta(days=30)).isoformat()))

        # Low attendance subjects (alerts)
        alerts = [s for s in by_sub if s["pct"] is not None and float(s["pct"]) < 75]

        # Monthly trend (last 6 months)
        monthly = qall(conn, """
            SELECT TO_CHAR(date, 'Mon') AS month,
                   DATE_TRUNC('month', date) AS month_start,
                   COUNT(*) FILTER(WHERE status='Present') AS present,
                   COUNT(*) AS total
            FROM attendance WHERE student_id=%s
              AND date >= %s
            GROUP BY month, month_start ORDER BY month_start
        """, (sid, (today - timedelta(days=180)).isoformat()))

    return jsonify({
        "stats": stats,
        "by_subject": by_sub,
        "today_classes": today_classes,
        "holidays": holidays,
        "alerts": alerts,
        "monthly_trend": monthly,
        "today": today.isoformat(),
        "day": day_abbr,
    })


@app.route("/api/student/me/timetable")
@require_auth
def student_timetable():
    if g.user.get("role") != "student":
        return jsonify({"error": "Student access only"}), 403
    sid = g.user["student_id"]
    fac_id = g.user.get("faculty_id")
    sem = g.user.get("semester")

    with get_db(register_pgvector=False) as conn:
        # Always re-fetch from DB — session data may be stale or semester may be NULL
        stu = qone(conn, "SELECT faculty_id, semester FROM students WHERE student_id=%s", (sid,))
        if stu:
            fac_id = fac_id or stu.get("faculty_id")
            sem = sem or stu.get("semester")

        if not fac_id:
            return jsonify({"timetable": [], "slots": [], "missing": "faculty"})

        # Build query — semester is optional; show all semesters if not set
        sem_clause = "AND cs.semester=%s" if sem else ""
        params_entries = [fac_id, sem] if sem else [fac_id]
        entries = qall(conn, f"""
            SELECT cs.id, cs.day_of_week, cs.semester,
                   ts.id AS time_slot_id,
                   ts.label, ts.start_time::text, ts.end_time::text,
                   s.name AS subject_name, s.code AS subject_code,
                   t.full_name AS teacher_name
            FROM class_schedules cs
            JOIN time_slots ts ON ts.id = cs.time_slot_id
            LEFT JOIN subjects s ON s.id = cs.subject_id
            LEFT JOIN teachers t ON t.id = cs.teacher_id
            WHERE cs.faculty_id=%s {sem_clause}
            ORDER BY ts.start_time, cs.day_of_week
        """, params_entries)
        # GROUP BY instead of DISTINCT — avoids Postgres "ORDER BY must appear in SELECT list"
        # error that occurs when ORDER BY ts.start_time isn't identical to ts.start_time::text
        slots = qall(conn, f"""
            SELECT ts.id, ts.label, ts.start_time::text, ts.end_time::text
            FROM class_schedules cs
            JOIN time_slots ts ON ts.id = cs.time_slot_id
            WHERE cs.faculty_id=%s {sem_clause}
            GROUP BY ts.id, ts.label, ts.start_time, ts.end_time
            ORDER BY ts.start_time
        """, params_entries)
    return jsonify({"timetable": entries, "slots": slots, "semester": sem, "faculty_id": fac_id})


@app.route("/api/student/me/notifications")
@require_auth
def student_notifications():
    if g.user.get("role") != "student":
        return jsonify({"error": "Student access only"}), 403
    sid = g.user["student_id"]
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    notes = []

    with get_db(register_pgvector=False) as conn:
        # Fetch the last time this student read their notifications
        sess_row = qone(conn,
            "SELECT notifications_read_at FROM sessions WHERE token=%s AND user_type='student'",
            (token,))
        read_at = sess_row["notifications_read_at"] if sess_row else None

        # Low attendance warnings — always present, never individually "new"
        low_subs = qall(conn, """
            SELECT sb.name AS subject_name, sb.code AS subject_code,
                   ROUND(100.0 * COUNT(a.id) FILTER(WHERE a.status='Present')
                         / NULLIF(COUNT(a.id),0), 1) AS pct
            FROM subjects sb
            JOIN attendance a ON a.subject_id=sb.id AND a.student_id=%s
            GROUP BY sb.id, sb.name, sb.code
            HAVING ROUND(100.0 * COUNT(a.id) FILTER(WHERE a.status='Present')
                         / NULLIF(COUNT(a.id),0), 1) < 75
        """, (sid,))
        for s in low_subs:
            pct = float(s["pct"] or 0)
            level = "critical" if pct < 65 else "warning"
            notes.append({"type": level, "title": f"Low attendance: {s['subject_code']}",
                           "body": f"{s['subject_name']} — {pct}% (below 75%)",
                           "time": None, "is_new": False})

        # Correction request statuses — "new" if reviewed after last read
        corrections = qall(conn, """
            SELECT id, date::text, status, reason, reviewed_at
            FROM correction_requests
            WHERE student_id=%s ORDER BY created_at DESC LIMIT 10
        """, (sid,))
        for c in corrections:
            reviewed_at = c.get("reviewed_at")
            is_new = bool(reviewed_at and (read_at is None or reviewed_at > read_at))
            if c["status"] == "pending":
                notes.append({"type": "info", "title": "Correction request pending",
                               "body": f"Date: {c['date']} — {c['reason'][:60]}",
                               "time": None, "is_new": False})
            elif c["status"] == "approved":
                notes.append({"type": "success", "title": "Correction approved ✓",
                               "body": f"Your attendance on {c['date']} was corrected.",
                               "time": reviewed_at.isoformat() if reviewed_at else None,
                               "is_new": is_new})
            elif c["status"] == "rejected":
                notes.append({"type": "warning", "title": "Correction rejected",
                               "body": f"Request for {c['date']} was rejected.",
                               "time": reviewed_at.isoformat() if reviewed_at else None,
                               "is_new": is_new})

        # Leave request statuses — "new" if reviewed after last read
        leaves = qall(conn, """
            SELECT id, from_date::text, to_date::text, status, reviewed_at
            FROM leave_requests
            WHERE student_id=%s ORDER BY created_at DESC LIMIT 10
        """, (sid,))
        for lv in leaves:
            reviewed_at = lv.get("reviewed_at")
            is_new = bool(reviewed_at and (read_at is None or reviewed_at > read_at))
            if lv["status"] == "approved":
                notes.append({"type": "success", "title": "Leave approved ✓",
                               "body": f"{lv['from_date']} to {lv['to_date']}",
                               "time": reviewed_at.isoformat() if reviewed_at else None,
                               "is_new": is_new})
            elif lv["status"] == "rejected":
                notes.append({"type": "warning", "title": "Leave rejected",
                               "body": f"{lv['from_date']} to {lv['to_date']}",
                               "time": reviewed_at.isoformat() if reviewed_at else None,
                               "is_new": is_new})

    unread_count = sum(1 for n in notes if n.get("is_new"))
    return jsonify({"notifications": notes, "unread_count": unread_count})


@app.route("/api/student/me/notifications/read", methods=["POST"])
@require_auth
def mark_notifications_read():
    """Mark all notifications as read for the current student session."""
    if g.user.get("role") != "student":
        return jsonify({"error": "Student access only"}), 403
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    with get_db(register_pgvector=False) as conn:
        qexec(conn,
            "UPDATE sessions SET notifications_read_at=NOW() WHERE token=%s AND user_type='student'",
            (token,))
    return jsonify({"ok": True})


@app.route("/api/student/me/profile")
@require_auth
def student_profile():
    """Student views their own profile — scoped to the authenticated student."""
    if g.user.get("role") != "student":
        return jsonify({"error": "Student access only"}), 403
    sid = g.user["student_id"]
    with get_db(register_pgvector=False) as conn:
        s = qone(conn, """
            SELECT st.student_id, st.full_name, st.department, st.email, st.phone,
                   st.semester, st.status, st.sample_count, st.enrolled_at::text,
                   f.name AS faculty_name, f.code AS faculty_code
            FROM students st
            LEFT JOIN faculties f ON f.id = st.faculty_id
            WHERE st.student_id=%s
        """, (sid,))
        if not s:
            return jsonify({"error": "Student not found"}), 404

        stats = qone(conn, """
            SELECT COUNT(*) FILTER(WHERE status='Present') AS present,
                   COUNT(*) AS total,
                   ROUND(100.0 * COUNT(*) FILTER(WHERE status='Present')
                         / NULLIF(COUNT(*),0), 1) AS pct
            FROM attendance WHERE student_id=%s
        """, (sid,))

        by_subject = qall(conn, """
            SELECT sb.name AS subject_name, sb.code AS subject_code,
                   COUNT(a.id) FILTER(WHERE a.status='Present') AS present,
                   COUNT(a.id) AS total,
                   ROUND(100.0 * COUNT(a.id) FILTER(WHERE a.status='Present')
                         / NULLIF(COUNT(a.id),0), 1) AS pct
            FROM subjects sb
            JOIN attendance a ON a.subject_id=sb.id AND a.student_id=%s
            GROUP BY sb.id, sb.name, sb.code
            ORDER BY pct ASC NULLS LAST
        """, (sid,))

    return jsonify({
        "student": s,
        "stats": stats or {},
        "by_subject": by_subject,
    })


@app.route("/api/leave-requests", methods=["GET"])
@require_auth
def list_leave_requests():
    role = g.user.get("role")
    with get_db(register_pgvector=False) as conn:
        if role == "student":
            rows = qall(conn, """
                SELECT lr.id, lr.from_date::text, lr.to_date::text, lr.reason,
                       lr.status, lr.review_note, lr.reviewed_at::text, lr.created_at::text,
                       t.full_name AS reviewer_name
                FROM leave_requests lr
                LEFT JOIN teachers t ON t.id = lr.reviewed_by
                WHERE lr.student_id=%s ORDER BY lr.created_at DESC
            """, (g.user["student_id"],))
        elif role in ("admin", "teacher"):
            rows = qall(conn, """
                SELECT lr.id, lr.student_id, st.full_name AS student_name,
                       lr.from_date::text, lr.to_date::text, lr.reason,
                       lr.status, lr.review_note, lr.reviewed_at::text, lr.created_at::text,
                       t.full_name AS reviewer_name
                FROM leave_requests lr
                JOIN students st ON st.student_id = lr.student_id
                LEFT JOIN teachers t ON t.id = lr.reviewed_by
                ORDER BY lr.created_at DESC LIMIT 100
            """)
        else:
            return jsonify({"error": "Unauthorized"}), 403
    return jsonify({"leave_requests": rows})


@app.route("/api/leave-requests", methods=["POST"])
@require_auth
def create_leave_request():
    if g.user.get("role") != "student":
        return jsonify({"error": "Student access only"}), 403
    sid = g.user["student_id"]
    d = request.json or {}
    from_date = d.get("from_date")
    to_date = d.get("to_date") or from_date
    reason = (d.get("reason") or "").strip()
    if not from_date or not reason:
        return jsonify({"error": "from_date and reason are required"}), 400
    with get_db(register_pgvector=False) as conn:
        row = qone(conn, """
            INSERT INTO leave_requests (student_id, from_date, to_date, reason)
            VALUES (%s,%s,%s,%s)
            RETURNING id, from_date::text, to_date::text, reason, status, created_at::text
        """, (sid, from_date, to_date, reason))
    return jsonify({"leave_request": row}), 201


@app.route("/api/leave-requests/<int:lid>", methods=["PUT"])
@require_auth
def review_leave_request(lid):
    if g.user.get("role") not in ("admin", "teacher"):
        return jsonify({"error": "Admin or teacher access only"}), 403
    d = request.json or {}
    status = d.get("status")
    if status not in ("approved", "rejected"):
        return jsonify({"error": "status must be 'approved' or 'rejected'"}), 400
    reviewer_id = _tid() if g.user.get("role") == "teacher" else None
    with get_db(register_pgvector=False) as conn:
        note = d.get("review_note") or d.get("note") or None
        row = qone(conn, """
            UPDATE leave_requests SET status=%s, reviewed_by=%s,
                review_note=%s, reviewed_at=NOW()
            WHERE id=%s
            RETURNING id, status
        """, (status, reviewer_id, note, lid))
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"updated": True, "status": status})


# ── Boot ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    _load_settings_from_db()   # restore persisted threshold / frame_skip
    # Start background email worker thread
    _email_thread = threading.Thread(target=_email_worker, daemon=True)
    _email_thread.start()
    app.run(debug=True, host="0.0.0.0", port=5050, threaded=True)