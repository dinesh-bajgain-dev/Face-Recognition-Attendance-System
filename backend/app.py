import os, cv2, json, base64, hashlib, threading, queue, time, re
import urllib.request, urllib.error
import numpy as np
from contextlib import contextmanager
from datetime import datetime, date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, request, jsonify, Response, g
from flask_cors import CORS
from dotenv import load_dotenv
import psycopg2, psycopg2.extras
from pgvector.psycopg2 import register_vector

load_dotenv()
app = Flask(__name__)
CORS(app, supports_credentials=True)

# ── Config ────────────────────────────────────────────────────────────────
PG_DSN    = os.getenv("DATABASE_URL", "postgresql://frs_user:frs123@localhost:5432/frs")
THRESHOLD = float(os.getenv("RECOGNITION_THRESHOLD", "0.80"))
SKIP      = int(os.getenv("FRAME_SKIP", "2"))

# Email config — uses SendGrid API (more reliable than raw SMTP)
# Sign up free at sendgrid.com, verify a sender address, generate an API key.
# Free tier: 100 emails/day indefinitely.
SENDGRID_API_KEY  = os.getenv("SENDGRID_API_KEY", "")
SENDGRID_FROM     = os.getenv("SENDGRID_FROM", "")     # verified sender email
EMAIL_ENABLED     = bool(SENDGRID_API_KEY and SENDGRID_FROM)

# ── DB ────────────────────────────────────────────────────────────────────
@contextmanager
def get_db(register_pgvector=True):
    conn = psycopg2.connect(PG_DSN)
    if register_pgvector:
        register_vector(conn)
    try:
        yield conn
        conn.commit()
    except:
        conn.rollback()
        raise
    finally:
        conn.close()

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

            CREATE TABLE IF NOT EXISTS attendance (
                id         SERIAL PRIMARY KEY,
                student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
                date       DATE NOT NULL DEFAULT CURRENT_DATE,
                time       TIME NOT NULL DEFAULT CURRENT_TIME,
                status     TEXT NOT NULL DEFAULT 'Present',
                note       TEXT,
                UNIQUE (student_id, date)
            );

            ALTER TABLE attendance ADD COLUMN IF NOT EXISTS note TEXT;

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

            INSERT INTO users (username, password_hash, role)
            VALUES ('admin',
                    '240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9',
                    'admin')
            ON CONFLICT DO NOTHING;
            """)
        with conn.cursor() as c:
            c.execute("""
                CREATE INDEX IF NOT EXISTS students_embedding_hnsw
                ON students USING hnsw (embedding vector_cosine_ops)
                WITH (m=16, ef_construction=64);
            """)
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
    Sends via SendGrid Web API (HTTPS POST).
    No raw SMTP, no TLS port issues, works from any network.
    Fallback: if sendgrid package not installed, uses urllib.
    """
    if not EMAIL_ENABLED:
        return

    # Deduplication check
    today = date.today().isoformat()
    try:
        with get_db() as conn:
            already = qone(conn, """
                SELECT id FROM email_log
                WHERE student_id=%s AND subject=%s AND sent_at::date=%s AND success=true
            """, (student_id, subject, today))
        if already:
            return
    except: pass

    payload = json.dumps({
        "personalizations": [{"to": [{"email": to_addr}]}],
        "from":    {"email": SENDGRID_FROM},
        "subject": subject,
        "content": [{"type": "text/html", "value": html_body}]
    }).encode("utf-8")

    try:
        import urllib.request, urllib.error
        req = urllib.request.Request(
            "https://api.sendgrid.com/v3/mail/send",
            data    = payload,
            method  = "POST",
            headers = {
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
            }
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status   # 202 = accepted

        with get_db() as conn:
            qexec(conn, """
                INSERT INTO email_log (student_id, email_to, subject, success)
                VALUES (%s,%s,%s,true)
            """, (student_id, to_addr, subject))

    except Exception as e:
        err = str(e)
        try:
            with get_db() as conn:
                qexec(conn, """
                    INSERT INTO email_log (student_id, email_to, subject, success, error_msg)
                    VALUES (%s,%s,%s,false,%s)
                """, (student_id, to_addr, subject, err))
        except: pass
        if retry < 2:
            time.sleep(8 * (retry + 1))
            _send_email_now(to_addr, subject, html_body, student_id, retry+1)

def queue_attendance_email(student_id, name, dept, att_date, att_time, email_to):
    """
    Build the HTML email and add it to the queue.
    Returns immediately — SMTP happens in background.
    """
    if not EMAIL_ENABLED or not email_to:
        return
    # Fetch attendance percentage for the email body
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
                  letter-spacing:0.04em;">FRS Attendance System</p>
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
                ON CONFLICT (student_id, date) DO NOTHING
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
        if not user: return jsonify({"error":"Unauthorized"}), 401
        g.user = user
        return fn(*args, **kwargs)
    return wrapper

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
    with get_db() as conn:
        user = qone(conn,
            "SELECT id,username,role,password_hash FROM users WHERE username=%s AND password_hash=%s",
            (d.get("username",""), _hash(d.get("password",""))))
    if not user: return jsonify({"error":"Invalid credentials"}), 401
    return jsonify({"token":user["password_hash"],"role":user["role"],"username":user["username"]})

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
        user = qone(conn,
            "SELECT id FROM users WHERE id=%s AND password_hash=%s",
            (g.user["id"], _hash(old_pw)))
        if not user: return jsonify({"error":"Current password is incorrect"}), 401
        qexec(conn,
            "UPDATE users SET password_hash=%s WHERE id=%s",
            (_hash(new_pw), g.user["id"]))
    _log_activity(g.user["username"], "change_password", "user", str(g.user["id"]))
    return jsonify({"updated": True})

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
            SELECT date::text, time::text, status, note
            FROM attendance WHERE student_id=%s
            ORDER BY date DESC LIMIT 60
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
@require_auth
def update_student(sid):
    """
    Full profile update. Accepts all editable fields.
    Allowed: full_name, department, email, phone, semester, status
    Also supports changing student_id (with duplicate check).
    """
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
@require_auth
def update_attendance(sid, att_date):
    """
    Admin can manually set attendance status for a student on a specific date.
    Body: { "status": "Present"|"Absent", "note": "optional reason" }
    """
    d      = request.json or {}
    status = d.get("status","")
    note   = d.get("note","")
    if status not in ("Present","Absent"):
        return jsonify({"error":"status must be Present or Absent"}), 400
    with get_db() as conn:
        # Upsert: works whether the row exists or not
        qexec(conn, """
            INSERT INTO attendance (student_id, date, time, status, note)
            VALUES (%s, %s, CURRENT_TIME, %s, %s)
            ON CONFLICT (student_id, date) DO UPDATE
                SET status=%s, note=%s
        """, (sid, att_date, status, note, status, note))
    _log_activity(
        g.user["username"], "edit_attendance", "attendance", sid,
        f"Set {att_date} to {status}" + (f" — {note}" if note else "")
    )
    return jsonify({"updated":True})

@app.route("/api/attendance/<sid>/<att_date>", methods=["DELETE"])
@require_auth
def delete_attendance(sid, att_date):
    """Remove a specific attendance record (admin only)."""
    with get_db() as conn:
        rows = qexec(conn,
            "DELETE FROM attendance WHERE student_id=%s AND date=%s",
            (sid, att_date))
    _log_activity(g.user["username"],"delete_attendance","attendance",sid,
                  f"Deleted {att_date}")
    return jsonify({"deleted": rows > 0})

# ── Activity log ──────────────────────────────────────────────────────────
@app.route("/api/activity-logs")
@require_auth
def get_activity_logs():
    limit  = int(request.args.get("limit", 50))
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

# ── Frame quality validation ──────────────────────────────────────────────
@app.route("/api/capture/validate-frame", methods=["POST"])
def validate_frame():
    """
    Called by the auto-capture UI for each candidate frame.
    Returns quality scores so the frontend can decide whether to keep the frame.
    Body: { "image": "base64...", "pose": "front"|"left"|"right"|"up"|"down" }
    """
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

    # Pose estimation from landmark positions
    # InsightFace returns kps (5 keypoints): left eye, right eye, nose, left mouth, right mouth
    pose_hint = "front"
    try:
        kps = face.kps  # shape (5,2)
        if kps is not None:
            le, re = kps[0], kps[1]  # left eye, right eye
            nose   = kps[2]
            eye_center_x = (le[0] + re[0]) / 2
            face_w = bbox[2] - bbox[0]
            # Horizontal offset of nose from eye midpoint
            nose_offset = (nose[0] - eye_center_x) / (face_w + 1e-5)
            # Vertical: how far nose is below eyes
            eye_y   = (le[1] + re[1]) / 2
            face_h  = bbox[3] - bbox[1]
            nose_dy = (nose[1] - eye_y) / (face_h + 1e-5)

            # ── Pose estimation — user perspective ──────────────────────
            # Canvas captures MIRRORED pixels (browser mirrors video naturally).
            # Horizontal: positive nose_offset = user's LEFT (canvas right).
            # Vertical geometry (Y increases downward in pixel space):
            #   nose_dy = (nose_y - eye_midpoint_y) / face_height
            #
            # Empirical ranges (verified against actual webcam geometry):
            #   Front:   nose_dy  0.22 – 0.34  (nose normally below eyes)
            #   DOWN:    nose_dy  < 0.22        (chin dropped → nose rises toward eyes)
            #   UP:      nose_dy  > 0.35        (chin raised → nose drops further down)
            #
            # DOWN confirmation: also check that mouth is close to nose.
            # When looking down, mouth_dy (mouth-to-eye / face_h) < nose_dy + 0.12
            mouth_pts = kps[3], kps[4]   # left_mouth, right_mouth
            mouth_y   = (mouth_pts[0][1] + mouth_pts[1][1]) / 2
            mouth_dy  = (mouth_y - eye_y) / (face_h + 1e-5)

            if nose_offset > 0.12:
                pose_hint = "left"      # user's left (mirrored canvas)
            elif nose_offset < -0.12:
                pose_hint = "right"     # user's right (mirrored canvas)
            elif nose_dy < 0.22 and mouth_dy < 0.45:
                # Nose near eye level AND mouth not extremely low → looking down
                pose_hint = "up"
            elif nose_dy > 0.28:
                # Nose far below eye midpoint → head tilted back → looking up
                pose_hint = "down"
            else:
                pose_hint = "front"
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
        qexec(conn, """
            INSERT INTO students
                (student_id, full_name, department, email, phone, semester,
                 embedding, face_image, sample_count)
            VALUES (%s,%s,%s,%s,%s,%s, %s::vector, %s, %s)
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
              mean_emb.tolist(), thumbnail, len(frames_b64)))

    return jsonify({
        "enrolled":   True,
        "student_id": student_id,
        "samples":    len(frames_b64),
        "is_update":  bool(existing_sid == student_id if test_emb is not None else False)
    })

# ── Recognize ─────────────────────────────────────────────────────────────
@app.route("/api/recognize", methods=["POST"])
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
                    ON CONFLICT (student_id, date) DO NOTHING
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
def start_camera():
    if camera_state["active"]: return jsonify({"status":"already_running"})
    camera_state["active"] = True
    return jsonify({"status":"started"})

@app.route("/api/camera/stop", methods=["POST"])
def stop_camera():
    camera_state["active"] = False
    if camera_state["cap"]: camera_state["cap"].release()
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

# ── Attendance ────────────────────────────────────────────────────────────
@app.route("/api/attendance")
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
def faculty_summary():
    target = request.args.get("date", date.today().isoformat())
    with get_db() as conn:
        rows = qall(conn, """
            SELECT s.student_id, s.full_name,
                   COALESCE(s.department,'Unassigned') AS department,
                   a.time::text AS time,
                   COALESCE(a.status,'Absent') AS status
            FROM   students s
            LEFT   JOIN attendance a
                   ON  a.student_id=s.student_id AND a.date=%s
            ORDER  BY s.department, s.full_name
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
@require_auth
def test_email():
    """Send a test email to the address in the request body."""
    d = request.json or {}
    to = d.get("email","").strip()
    if not to: return jsonify({"error":"email required"}), 400
    if not EMAIL_ENABLED:
        return jsonify({"error":"Email not configured. Set SENDGRID_API_KEY and SENDGRID_FROM in .env"}), 503
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

# ── Settings ──────────────────────────────────────────────────────────────
@app.route("/api/settings")
def get_settings():
    return jsonify({
        "recognition_threshold": THRESHOLD,
        "frame_skip":            SKIP,
        "email_enabled":         EMAIL_ENABLED,
        "sendgrid_from":         SENDGRID_FROM if SENDGRID_FROM else "",
    })

@app.route("/api/settings", methods=["PUT"])
@require_auth
def update_settings():
    global THRESHOLD, SKIP
    d = request.json or {}
    if "recognition_threshold" in d: THRESHOLD = float(d["recognition_threshold"])
    if "frame_skip" in d:            SKIP      = int(d["frame_skip"])
    _log_activity(g.user["username"],"update_settings","system",detail=str(d))
    return jsonify({"recognition_threshold":THRESHOLD,"frame_skip":SKIP})

# ── Boot ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    # Start background email worker thread
    _email_thread = threading.Thread(target=_email_worker, daemon=True)
    _email_thread.start()
    app.run(debug=True, host="0.0.0.0", port=5050, threaded=True)
