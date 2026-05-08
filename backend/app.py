"""
FRS Backend — Database-only architecture
- No local image folders
- Embeddings stored as pgvector(512) in PostgreSQL
- Similarity search done inside the DB via HNSW index
- Server-Sent Events for real-time attendance updates to frontend
"""
import os, cv2, json, base64, hashlib, threading, queue, time
import numpy as np
from contextlib import contextmanager
from datetime import datetime, date
from flask import Flask, request, jsonify, Response, g
from flask_cors import CORS
from dotenv import load_dotenv
import psycopg2, psycopg2.extras
from pgvector.psycopg2 import register_vector   # pgvector adapter

load_dotenv()
app = Flask(__name__)
CORS(app, supports_credentials=True)

# ── Config ────────────────────────────────────────────────────────────────
# Prefer DATABASE_URL when provided; otherwise fall back to the local dev URL.
PG_DSN    = os.getenv("DATABASE_URL", "postgresql://frs_user:frs123@localhost:5432/frs")
THRESHOLD = float(os.getenv("RECOGNITION_THRESHOLD", "0.80"))
SKIP      = int(os.getenv("FRAME_SKIP", "2"))

# ── DB ────────────────────────────────────────────────────────────────────
@contextmanager
def get_db(register_pgvector=True):
    conn = psycopg2.connect(PG_DSN)
    # autocommit=False is psycopg2 default — never set it after connecting,
    # that raises: set_session cannot be used inside a transaction
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

def init_db():
    """
    CREATE EXTENSION and CREATE INDEX CONCURRENTLY cannot run inside a
    transaction block. We use autocommit=True for the setup connection,
    then run the remaining DDL in separate autocommit statements.
    """
    conn = psycopg2.connect(PG_DSN)
    conn.autocommit = True          # safe here — connection is brand new, no txn yet
    try:
        with conn.cursor() as c:
            # Must be outside a transaction
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
                id           SERIAL PRIMARY KEY,
                student_id   TEXT NOT NULL UNIQUE,
                full_name    TEXT NOT NULL,
                department   TEXT,
                email        TEXT,
                phone        TEXT,
                face_image   BYTEA,
                embedding    vector(512),
                sample_count INTEGER NOT NULL DEFAULT 0,
                enrolled_at  TIMESTAMPTZ DEFAULT NOW()
            );

            ALTER TABLE students ADD COLUMN IF NOT EXISTS face_image   BYTEA;
            ALTER TABLE students ADD COLUMN IF NOT EXISTS embedding    vector(512);
            ALTER TABLE students ADD COLUMN IF NOT EXISTS sample_count INTEGER NOT NULL DEFAULT 0;

            CREATE TABLE IF NOT EXISTS attendance (
                id         SERIAL PRIMARY KEY,
                student_id TEXT NOT NULL REFERENCES students(student_id) ON DELETE CASCADE,
                date       DATE NOT NULL DEFAULT CURRENT_DATE,
                time       TIME NOT NULL DEFAULT CURRENT_TIME,
                status     TEXT NOT NULL DEFAULT 'Present',
                UNIQUE (student_id, date)
            );

            CREATE TABLE IF NOT EXISTS recognition_logs (
                id         SERIAL PRIMARY KEY,
                student_id TEXT,
                full_name  TEXT,
                confidence REAL,
                recognized BOOLEAN NOT NULL,
                logged_at  TIMESTAMPTZ DEFAULT NOW()
            );

            INSERT INTO users (username, password_hash, role)
            VALUES ('admin',
                    '240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9',
                    'admin')
            ON CONFLICT DO NOTHING;
            """)

        # HNSW index also cannot run inside a transaction
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
    """Decode base64 string or raw bytes into a BGR OpenCV frame."""
    if isinstance(b64_or_bytes, str):
        b64_or_bytes = base64.b64decode(b64_or_bytes)
    arr = np.frombuffer(b64_or_bytes, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)

def extract_embeddings(frames_b64: list) -> tuple[np.ndarray | None, bytes | None]:
    """
    Given a list of base64-encoded images:
    1. Run insightface on each
    2. Collect all face embeddings
    3. Return (averaged_unit_vector, thumbnail_bytes)
    """
    fa = get_face_app()
    embeddings = []
    thumbnail  = None

    for b64 in frames_b64:
        frame = decode_image(b64)
        if frame is None: continue
        rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        faces = fa.get(rgb)
        if not faces: continue
        # pick largest face
        face  = max(faces, key=lambda f:(f.bbox[2]-f.bbox[0])*(f.bbox[3]-f.bbox[1]))
        embeddings.append(face.normed_embedding)
        if thumbnail is None:
            x1,y1,x2,y2 = [max(0,int(v)) for v in face.bbox]
            crop = frame[y1:y2, x1:x2]
            if crop.size > 0:
                crop = cv2.resize(crop, (128, 128))
                _, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
                thumbnail = buf.tobytes()

    if not embeddings:
        return None, None

    mean_emb = np.mean(embeddings, axis=0)
    mean_emb /= np.linalg.norm(mean_emb)     # re-normalize after averaging
    return mean_emb, thumbnail

# ── pgvector similarity search ────────────────────────────────────────────
def find_best_match(conn, query_emb: np.ndarray):
    """
    Use pgvector's cosine distance operator (<=>)
    to find the closest student embedding in the DB.
    Returns (student_id, full_name, similarity) or (None, None, 0.0)
    """
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

# ── SSE broadcast ─────────────────────────────────────────────────────────
# Each connected SSE client gets a queue. When attendance is marked,
# we push an event to every queue so all browser tabs update instantly.
_sse_clients: list[queue.Queue] = []
_sse_lock = threading.Lock()

def sse_broadcast(data: dict):
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
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO attendance (student_id, date, time, status)
                VALUES (%s, %s, %s, 'Present')
                ON CONFLICT (student_id, date) DO NOTHING
            """, (student_id, today, now))
            marked = c.rowcount == 1
            c.execute("""
                INSERT INTO recognition_logs (student_id, full_name, confidence, recognized)
                VALUES (%s,%s,%s,true)
            """, (student_id, name, round(confidence*100, 1)))

    if marked:
        # Push real-time event to all connected SSE clients
        sse_broadcast({
            "type":       "attendance",
            "student_id": student_id,
            "name":       name,
            "confidence": round(confidence*100, 1),
            "time":       now,
            "date":       today
        })

def _log_recognition(sid, name, confidence, recognized):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO recognition_logs (student_id, full_name, confidence, recognized)
                VALUES (%s,%s,%s,%s)
            """, (sid, name, round(confidence*100, 1), recognized))

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

# ═════════════════════════════════════════════════════════════════════════
#  ROUTES
# ═════════════════════════════════════════════════════════════════════════

@app.route("/api/health")
def health():
    try:
        with get_db() as conn:
            # Check pgvector is installed
            row = qone(conn, "SELECT installed_version FROM pg_available_extensions WHERE name='vector'")
            vec_ok = bool(row and row.get("installed_version"))
        return jsonify({"status":"ok","db":"ok","pgvector": "ok" if vec_ok else "missing"})
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
    return jsonify({"token": user["password_hash"], "role": user["role"], "username": user["username"]})

@app.route("/api/auth/me")
@require_auth
def me(): return jsonify(g.user)

# ── Students ──────────────────────────────────────────────────────────────
@app.route("/api/students")
def list_students():
    q    = request.args.get("q","").strip()
    dept = request.args.get("department","").strip()
    sql  = "SELECT student_id,full_name,department,email,phone,sample_count,enrolled_at::text FROM students"
    params, where = [], []
    if q:
        where.append("(full_name ILIKE %s OR student_id ILIKE %s)")
        params += [f"%{q}%",f"%{q}%"]
    if dept:
        where.append("department=%s"); params.append(dept)
    if where: sql += " WHERE "+(" AND ".join(where))
    sql += " ORDER BY full_name"
    with get_db() as conn:
        students = qall(conn, sql, params)
    return jsonify({"students": students, "count": len(students)})

@app.route("/api/students/<sid>")
def get_student(sid):
    with get_db() as conn:
        s = qone(conn,
            "SELECT student_id,full_name,department,email,phone,sample_count,enrolled_at::text FROM students WHERE student_id=%s",
            (sid,))
        if not s: return jsonify({"error":"Not found"}), 404

        stats = qone(conn,"""
            SELECT
              COUNT(*) FILTER(WHERE status='Present') AS total_present,
              COUNT(DISTINCT date) AS total_days,
              ROUND(100.0*COUNT(*) FILTER(WHERE status='Present')/NULLIF(COUNT(DISTINCT date),0),1) AS percentage
            FROM attendance WHERE student_id=%s
        """,(sid,))

        monthly = qall(conn,"""
            SELECT TO_CHAR(date,'Mon YYYY') AS month,
                   DATE_TRUNC('month',date) AS month_sort,
                   COUNT(*) FILTER(WHERE status='Present') AS present
            FROM attendance WHERE student_id=%s AND date >= NOW()-INTERVAL '6 months'
            GROUP BY month, month_sort ORDER BY month_sort
        """,(sid,))

        logs = qall(conn,"""
            SELECT logged_at::text,confidence,recognized FROM recognition_logs
            WHERE student_id=%s ORDER BY logged_at DESC LIMIT 20
        """,(sid,))

    return jsonify({**s, "stats": stats, "monthly": monthly, "logs": logs})

@app.route("/api/students/<sid>/photo")
def student_photo(sid):
    with get_db() as conn:
        row = qone(conn,"SELECT face_image FROM students WHERE student_id=%s",(sid,))
    if not row or not row["face_image"]:
        return "", 404
    return Response(bytes(row["face_image"]), mimetype="image/jpeg")

@app.route("/api/students/<sid>", methods=["PUT"])
@require_auth
def update_student(sid):
    d = request.json or {}
    fields = {k:d[k] for k in ("full_name","department","email","phone") if k in d}
    if not fields: return jsonify({"error":"Nothing to update"}), 400
    sql = "UPDATE students SET "+", ".join(f"{k}=%s" for k in fields)+" WHERE student_id=%s"
    with get_db() as conn:
        qexec(conn, sql, list(fields.values())+[sid])
    return jsonify({"updated": True})

@app.route("/api/students/<sid>", methods=["DELETE"])
@require_auth
def delete_student(sid):
    with get_db() as conn:
        rows = qexec(conn,"DELETE FROM students WHERE student_id=%s",(sid,))
    return jsonify({"deleted": rows > 0})

@app.route("/api/departments")
def departments():
    with get_db() as conn:
        rows = qall(conn,"SELECT DISTINCT department FROM students WHERE department IS NOT NULL ORDER BY department")
    return jsonify({"departments":[r["department"] for r in rows]})

# ── Enroll ────────────────────────────────────────────────────────────────
@app.route("/api/enroll", methods=["POST"])
def enroll():
    """
    Accept multipart/form-data with:
      - student_id, full_name, department, email, phone (fields)
      - images[]  (one or more image files)
    OR JSON with:
      - student_id, full_name, ...
      - frames: ["base64...", "base64...", ...]   (from webcam capture)

    Flow:
      1. Decode every image/frame
      2. insightface extracts embedding from each
      3. Average all embeddings → one unit vector
      4. Store vector in PostgreSQL (pgvector column)
      5. Optionally store thumbnail as BYTEA
      6. Duplicate check: if student_id already exists, UPDATE embeddings
    """
    # Support both multipart (file upload) and JSON (webcam frames)
    if request.content_type and "application/json" in request.content_type:
        data       = request.json or {}
        student_id = data.get("student_id","").strip()
        full_name  = data.get("full_name","").strip()
        department = data.get("department") or None
        email      = data.get("email")      or None
        phone      = data.get("phone")      or None
        frames_b64 = data.get("frames", [])   # list of base64 strings
    else:
        student_id = request.form.get("student_id","").strip()
        full_name  = request.form.get("full_name","").strip()
        department = request.form.get("department") or None
        email      = request.form.get("email")      or None
        phone      = request.form.get("phone")      or None
        files      = request.files.getlist("images")
        frames_b64 = []
        for f in files:
            raw  = f.read()
            frames_b64.append(base64.b64encode(raw).decode())

    if not student_id or not full_name:
        return jsonify({"error": "student_id and full_name required"}), 400
    if not frames_b64:
        return jsonify({"error": "No images provided"}), 400

    # ── Duplicate face check ──────────────────────────────────────────────
    # Before enrolling, check if this face already exists for a DIFFERENT student
    # (prevents someone enrolling under two IDs)
    test_emb, _ = extract_embeddings(frames_b64[:3])   # quick check with first 3 frames
    if test_emb is not None:
        with get_db() as conn:
            existing_sid, existing_name, sim = find_best_match(conn, test_emb)
        if sim > 0.92 and existing_sid and existing_sid != student_id:
            return jsonify({
                "error": f"This face already belongs to {existing_name} ({existing_sid}). "
                         f"Similarity: {sim*100:.1f}%"
            }), 409   # 409 Conflict

    # ── Extract embeddings from all frames ────────────────────────────────
    mean_emb, thumbnail = extract_embeddings(frames_b64)
    if mean_emb is None:
        return jsonify({"error": "No faces detected in any image"}), 422

    # ── Upsert into PostgreSQL ────────────────────────────────────────────
    # ON CONFLICT updates embeddings if student re-enrolls (e.g. after haircut)
    with get_db() as conn:
        qexec(conn,"""
            INSERT INTO students
                (student_id, full_name, department, email, phone,
                 embedding, face_image, sample_count)
            VALUES (%s,%s,%s,%s,%s, %s::vector, %s, %s)
            ON CONFLICT (student_id) DO UPDATE SET
                full_name    = EXCLUDED.full_name,
                department   = EXCLUDED.department,
                email        = EXCLUDED.email,
                phone        = EXCLUDED.phone,
                embedding    = EXCLUDED.embedding,
                face_image   = EXCLUDED.face_image,
                sample_count = EXCLUDED.sample_count
        """, (student_id, full_name, department, email, phone,
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
    """
    Accept JSON { image: "base64..." }
    1. Decode image
    2. insightface extracts embedding
    3. pgvector cosine search in DB → best match
    4. If above threshold → mark attendance
    5. Return result
    """
    data    = request.json or {}
    img_b64 = data.get("image")
    if not img_b64: return jsonify({"error":"No image"}), 400

    frame = decode_image(img_b64)
    if frame is None: return jsonify({"error":"Cannot decode image"}), 400

    fa    = get_face_app()
    rgb   = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    faces = fa.get(rgb)
    if not faces:
        return jsonify({"recognized": False, "message": "No face detected"})

    face = max(faces, key=lambda f:(f.bbox[2]-f.bbox[0])*(f.bbox[3]-f.bbox[1]))
    emb  = face.normed_embedding
    bbox = [int(x) for x in face.bbox]

    with get_db() as conn:
        sid, name, sim = find_best_match(conn, emb)

    if sim >= THRESHOLD and sid:
        marked = False
        today  = date.today().isoformat()
        now    = datetime.now().strftime("%H:%M:%S")
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
            sse_broadcast({"type":"attendance","student_id":sid,"name":name,
                           "confidence":round(sim*100,1),"time":now,"date":today})
        return jsonify({"recognized":True,"student_id":sid,"name":name,
                        "confidence":round(sim*100,1),"bbox":bbox,
                        "attendance_marked":marked})

    _log_recognition(None,"Unknown",sim,False)
    return jsonify({"recognized":False,"name":"Unknown",
                    "confidence":round(sim*100,1),"bbox":bbox})

# ── Camera stream ─────────────────────────────────────────────────────────
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

# ── Server-Sent Events ────────────────────────────────────────────────────
@app.route("/api/events")
def sse_events():
    """
    Browser connects once: new EventSource('/api/events')
    Backend pushes JSON whenever attendance is marked.
    No polling needed.
    """
    q = queue.Queue(maxsize=50)
    with _sse_lock: _sse_clients.append(q)

    def generate():
        try:
            # Send a heartbeat every 20s to keep connection alive
            while True:
                try:
                    msg = q.get(timeout=20)
                    yield msg
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            with _sse_lock:
                if q in _sse_clients: _sse_clients.remove(q)

    return Response(generate(),
                    mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

# ── Attendance ────────────────────────────────────────────────────────────

@app.route("/api/attendance")
def get_attendance():
    """
    Returns attendance for a specific date.
    Optional filters: ?date=YYYY-MM-DD  &department=BCA
    """
    target = request.args.get("date", date.today().isoformat())
    dept   = request.args.get("department", "").strip()

    sql = """
        SELECT s.student_id, s.full_name, s.department,
               a.date::text, a.time::text, a.status
        FROM   students s
        LEFT   JOIN attendance a
               ON  a.student_id = s.student_id
               AND a.date       = %s
    """
    params = [target]
    if dept:
        sql += " WHERE s.department = %s"
        params.append(dept)
    sql += " ORDER BY s.full_name"

    with get_db() as conn:
        rows = qall(conn, sql, params)

    records = [
        {
            "student_id": r["student_id"],
            "name":       r["full_name"],
            "department": r["department"],
            "date":       r["date"] or target,
            "time":       r["time"] or "—",
            "status":     r["status"] or "Absent",
        }
        for r in rows
    ]
    return jsonify({
        "date":    target,
        "records": records,
        "present": sum(1 for r in records if r["status"] == "Present"),
        "absent":  sum(1 for r in records if r["status"] == "Absent"),
    })


@app.route("/api/attendance/faculty-summary")
def faculty_summary():
    """
    Returns a per-faculty breakdown for a given date.
    Each faculty entry includes: name, total enrolled, present, absent, rate,
    and the full student list so the frontend can render per-faculty tables.

    Response shape:
    {
      "date": "2026-05-20",
      "faculties": [
        {
          "name": "BCA",
          "total": 40, "present": 32, "absent": 8, "rate": 80.0,
          "students": [ {student_id, name, time, status}, ... ]
        },
        ...
      ],
      "overall": { "total": 120, "present": 95, "absent": 25, "rate": 79.2 }
    }
    """
    target = request.args.get("date", date.today().isoformat())

    with get_db() as conn:
        # All students with their attendance for the target date
        rows = qall(conn, """
            SELECT s.student_id,
                   s.full_name,
                   COALESCE(s.department, 'Unassigned') AS department,
                   a.time::text  AS time,
                   COALESCE(a.status, 'Absent') AS status
            FROM   students s
            LEFT   JOIN attendance a
                   ON  a.student_id = s.student_id
                   AND a.date       = %s
            ORDER  BY s.department, s.full_name
        """, (target,))

    # Group by department in Python — preserves ordering
    faculty_map = {}
    for r in rows:
        dept = r["department"]
        if dept not in faculty_map:
            faculty_map[dept] = []
        faculty_map[dept].append({
            "student_id": r["student_id"],
            "name":       r["full_name"],
            "time":       r["time"] or "—",
            "status":     r["status"],
        })

    faculties = []
    for dept_name in sorted(faculty_map.keys()):
        students = faculty_map[dept_name]
        present  = sum(1 for s in students if s["status"] == "Present")
        total    = len(students)
        faculties.append({
            "name":     dept_name,
            "total":    total,
            "present":  present,
            "absent":   total - present,
            "rate":     round(present / total * 100, 1) if total else 0,
            "students": students,
        })

    overall_total   = sum(f["total"]   for f in faculties)
    overall_present = sum(f["present"] for f in faculties)
    return jsonify({
        "date":      target,
        "faculties": faculties,
        "overall": {
            "total":   overall_total,
            "present": overall_present,
            "absent":  overall_total - overall_present,
            "rate":    round(overall_present / overall_total * 100, 1) if overall_total else 0,
        }
    })


@app.route("/api/attendance/history")
def attendance_history():
    dept = request.args.get("department", "").strip()
    with get_db() as conn:
        if dept:
            rows = qall(conn, """
                SELECT a.date::text,
                       COUNT(*) FILTER(WHERE a.status='Present') AS present
                FROM   attendance a
                JOIN   students s ON s.student_id = a.student_id
                WHERE  s.department = %s
                GROUP  BY a.date
                ORDER  BY a.date DESC
                LIMIT  30
            """, (dept,))
            total = qone(conn,
                "SELECT COUNT(*) AS n FROM students WHERE department=%s",
                (dept,))["n"]
        else:
            rows = qall(conn, """
                SELECT date::text,
                       COUNT(*) FILTER(WHERE status='Present') AS present
                FROM   attendance
                GROUP  BY date
                ORDER  BY date DESC
                LIMIT  30
            """)
            total = qone(conn, "SELECT COUNT(*) AS n FROM students")["n"]

    return jsonify({"history": [
        {
            "date":    r["date"],
            "present": r["present"],
            "absent":  max(0, total - r["present"]),
        }
        for r in rows
    ]})


@app.route("/api/attendance/stats")
def attendance_stats():
    dept = request.args.get("department", "").strip()
    with get_db() as conn:
        sql = """
            SELECT s.student_id, s.full_name, s.department,
                   COUNT(a.id) FILTER(WHERE a.status='Present') AS present_days,
                   COUNT(DISTINCT a.date)                         AS total_days,
                   ROUND(
                       100.0 * COUNT(a.id) FILTER(WHERE a.status='Present')
                       / NULLIF(COUNT(DISTINCT a.date), 0),
                   1) AS pct
            FROM   students s
            LEFT   JOIN attendance a ON a.student_id = s.student_id
        """
        params = []
        if dept:
            sql += " WHERE s.department = %s"
            params.append(dept)
        sql += " GROUP BY s.student_id, s.full_name, s.department ORDER BY pct DESC NULLS LAST"
        rows = qall(conn, sql, params)
    return jsonify({"stats": rows})


@app.route("/api/attendance/export")
def export_csv():
    """
    Faculty-formatted CSV export.
    If ?department= is given, exports only that faculty with a header block.
    Without it, exports all students (flat format).
    Supports date range: ?from=YYYY-MM-DD&to=YYYY-MM-DD
    """
    from_d = request.args.get("from", date.today().isoformat())
    to_d   = request.args.get("to",   date.today().isoformat())
    dept   = request.args.get("department", "").strip()

    with get_db() as conn:
        sql = """
            SELECT s.student_id, s.full_name, s.department,
                   a.date::text, a.time::text, a.status
            FROM   students s
            LEFT   JOIN attendance a
                   ON  a.student_id = s.student_id
                   AND a.date BETWEEN %s AND %s
        """
        params = [from_d, to_d]
        if dept:
            sql += " WHERE s.department = %s"
            params.append(dept)
        sql += " ORDER BY s.department, a.date, s.full_name"
        rows = qall(conn, sql, params)

    lines = []

    if dept:
        # ── Faculty-specific export: header block + student rows ──────────
        total   = len(set(r["student_id"] for r in rows))
        present = sum(1 for r in rows if r["status"] == "Present")
        absent  = total - present
        rate    = round(present / total * 100, 1) if total else 0

        lines += [
            f"Faculty / Department: {dept}",
            f"Date Range: {from_d} to {to_d}",
            f"Total Enrolled: {total}",
            f"Total Present: {present}",
            f"Total Absent: {absent}",
            f"Attendance Rate: {rate}%",
            "",   # blank line before data
            "Student ID,Name,Date,Time,Status",
        ]
        lines += [
            f'{r["student_id"]},{r["full_name"]},{r["date"] or ""},'
            f'{r["time"] or "—"},{r["status"] or "Absent"}'
            for r in rows
        ]
        filename = f"attendance_{dept}_{from_d}_{to_d}.csv"
    else:
        # ── All-faculties flat export ──────────────────────────────────────
        lines.append("Student ID,Name,Department,Date,Time,Status")
        lines += [
            f'{r["student_id"]},{r["full_name"]},{r["department"] or ""},'
            f'{r["date"] or ""},{r["time"] or "—"},{r["status"] or "Absent"}'
            for r in rows
        ]
        filename = f"attendance_all_{from_d}_{to_d}.csv"

    return Response(
        "\n".join(lines),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# ── Logs ──────────────────────────────────────────────────────────────────
@app.route("/api/logs")
def get_logs():
    limit = int(request.args.get("limit", 50))
    with get_db() as conn:
        rows = qall(conn,"""
            SELECT student_id,full_name,confidence,recognized,logged_at::text
            FROM recognition_logs ORDER BY logged_at DESC LIMIT %s
        """,(limit,))
    return jsonify({"logs": rows})

# ── Settings ──────────────────────────────────────────────────────────────
@app.route("/api/settings")
def get_settings():
    return jsonify({"recognition_threshold": THRESHOLD, "frame_skip": SKIP})

@app.route("/api/settings", methods=["PUT"])
@require_auth
def update_settings():
    global THRESHOLD, SKIP
    d = request.json or {}
    if "recognition_threshold" in d: THRESHOLD = float(d["recognition_threshold"])
    if "frame_skip" in d:            SKIP      = int(d["frame_skip"])
    return jsonify({"recognition_threshold": THRESHOLD, "frame_skip": SKIP})

# ── Boot ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5050, threaded=True)
