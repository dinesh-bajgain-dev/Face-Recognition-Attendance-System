# वेदनेत्रम् · Face Recognition Attendance System

An AI-powered attendance management system for academic institutions. Students are identified automatically from a webcam or CCTV feed using facial recognition, attendance is marked in real time, and faculty-wise reports are available instantly from a web dashboard.

---

## What's Inside

```
frs/
├── backend/
│   ├── app.py              ← Flask REST API — all routes, AI inference, camera stream
│   ├── requirements.txt    ← Python dependencies
│   └── .env                ← DATABASE_URL, RECOGNITION_THRESHOLD, FRAME_SKIP
├── frontend/
│   ├── index.html          ← Single-page application (8 pages)
│   └── static/
│       ├── css/style.css   ← Dark-mode design system
│       └── js/app.js       ← All API calls, SSE handler, webcam logic
└── start.sh                ← Convenience script to start both servers
```

---

## How It Works

```
Browser (app.js)
      │
      │  fetch() + EventSource (SSE)
      ▼
Flask API  ──────────────────────────────────────────────────────┐
  │                                                              │
  │  InsightFace ArcFace          pgvector cosine search        │
  │  (512-d face embedding)  ──►  inside PostgreSQL             │
  │                               (HNSW index, sub-ms)          │
  │                                                             │
  │  OpenCV VideoCapture                                        │
  └─ Webcam / RTSP  ──►  MJPEG stream  ──►  <img> in browser  ─┘
```

**Key design decisions:**

- **No local files.** Images are processed in Python memory. Only the 512-float embedding vector is stored in PostgreSQL — no image folders, no JSON files, no CSV logs.
- **One averaged embedding per student.** 50 images produce 50 embeddings, averaged and re-normalized into one robust vector per student.
- **pgvector HNSW index** does the similarity search inside PostgreSQL — no Python loop over all students. Scales to 100,000+ students with sub-millisecond query time.
- **Server-Sent Events** push attendance updates to all open browser tabs the moment a student is recognized. No polling.

---

## Prerequisites

| Tool       | Version      | Notes                                            |
| ---------- | ------------ | ------------------------------------------------ |
| Python     | 3.11 or 3.12 | 3.13 works but insightface may need manual build |
| PostgreSQL | 16+          | With `pgvector` extension                        |
| pgvector   | 0.7+         | Installed as a PostgreSQL extension              |

> **No TensorFlow, no dlib, no MongoDB.** The stack was completely redesigned. InsightFace with ONNX Runtime handles all face detection and recognition without TensorFlow.

---

## Setup (Step by Step)

### 1. Install PostgreSQL and pgvector

**Mac**

```bash
brew install postgresql@16 pgvector
brew services start postgresql@16
export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"   # add to ~/.zshrc
```

**Ubuntu / Debian**

```bash
sudo apt update
sudo apt install postgresql postgresql-contrib postgresql-16-pgvector
sudo systemctl start postgresql
sudo systemctl enable postgresql
```

**Windows** — download from [postgresql.org/download/windows](https://postgresql.org/download/windows), then install pgvector from [github.com/pgvector/pgvector](https://github.com/pgvector/pgvector).

### 2. Create the database and user

```bash
psql -U postgres
```

```sql
CREATE USER frs_user WITH PASSWORD 'frs_password';
CREATE DATABASE frs OWNER frs_user;
GRANT ALL PRIVILEGES ON DATABASE frs TO frs_user;
\q
```

Then enable the pgvector extension inside the `frs` database:

```bash
psql -U frs_user -d frs -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

### 3. Configure the backend

Edit `backend/.env`:

```dotenv
DATABASE_URL=postgresql://frs_user:frs_password@localhost:5432/frs
RECOGNITION_THRESHOLD=0.80
FRAME_SKIP=2
```

`RECOGNITION_THRESHOLD` — cosine similarity cutoff (0.0–1.0). Raise to `0.85` to reduce false positives in controlled environments.

`FRAME_SKIP` — process every Nth camera frame for recognition. `2` = process every other frame (halves CPU load with negligible accuracy loss).

### 4. Set up the Python environment

```bash
cd frs/backend

python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

> On first run, InsightFace will automatically download the `buffalo_l` ArcFace model (~300 MB). This only happens once.

### 5. Start the backend

```bash
# Inside backend/ with venv active:
python app.py
```

The server starts on `http://0.0.0.0:5050`. On first start, `init_db()` creates all four database tables and the HNSW index automatically. You should see:

```
 * Running on http://0.0.0.0:5050
```

Verify everything is healthy:

```bash
curl http://localhost:5050/api/health
# {"status":"ok","db":"ok","pgvector":"ok"}
```

If `"pgvector": "missing"` appears, run step 2's `CREATE EXTENSION` command again.

### 6. Start the frontend

Open a second terminal:

```bash
cd frs/frontend
python3 -m http.server 8080
```

### 7. Open the app

Visit **http://localhost:8080** and sign in with:

- **Username:** `admin`
- **Password:** `admin123`

---

## Using the System

### Enroll a Student

1. Go to the **Enroll** page
2. Fill in Student ID, Full Name, Department, Email, Phone
3. Either:
   - **Upload images** — drag-drop or browse 30–150 face photos
   - **Use Webcam** — click "Use Webcam", then "Capture Frame" to collect live photos
4. Click **Enroll & Generate Embeddings**

The backend detects faces in every image, averages all embeddings into one vector, and stores it in PostgreSQL. The student is immediately searchable by the recognition system.

> If the same face already exists under a different student ID (similarity > 92%), enrollment is rejected to prevent duplicate registrations.

### Live Recognition

1. Go to the **Recognize** page
2. Click **▶ Start Camera** — the backend opens the webcam and streams MJPEG to the browser
3. Students in frame are identified automatically. Bounding boxes are drawn — green for known, red for unknown
4. Attendance is marked in the database the first time each student is recognized each day
5. The **Live Event Log** panel updates in real time via Server-Sent Events
6. Alternatively, click **Upload Image** to recognize a single photo

### Attendance Page

- **Faculty tabs** across the top — one tab per department (auto-generated from enrolled students)
- **All tab** shows a summary card per faculty with present/absent counts and an attendance rate bar
- **Individual faculty tab** shows a header card with stats and a student-by-student table
- **Search** filters within the active tab
- **Export CSV** — on the All tab exports all students; on a faculty tab exports a formatted CSV with a faculty header block (name, date, enrolled count, present, absent, rate)

### Dashboard

- 4 stat cards: registered students, present today, absent today, attendance rate
- 30-day stacked bar chart
- Live recognition event feed (updates via SSE when camera is active)
- Today's searchable attendance table

### Reports & Analytics

- Date-range selector for historical analysis
- Monthly attendance trend (line chart)
- Per-department attendance rate (bar chart)
- Per-student percentage table with color-coded status: **On Track** (≥75%), **At Risk** (≥50%), **Critical** (<50%)

### Students Page

- Card grid with search and department filter
- Click any card to open the full student profile
- Profile shows: metadata, attendance percentage with progress bar, monthly chart, recent recognition log

---

## API Reference

All routes are served on port `5050`. Write routes require `Authorization: Bearer <token>` header.

| Method | Endpoint                          | Auth | Description                                                                |
| ------ | --------------------------------- | ---- | -------------------------------------------------------------------------- |
| GET    | `/api/health`                     | —    | DB + pgvector status check                                                 |
| POST   | `/api/auth/login`                 | —    | Login, returns auth token                                                  |
| GET    | `/api/auth/me`                    | ✓    | Current user info                                                          |
| GET    | `/api/students`                   | —    | List students; `?q=` search, `?department=` filter                         |
| GET    | `/api/students/<id>`              | —    | Full profile with stats, monthly data, recognition logs                    |
| GET    | `/api/students/<id>/photo`        | —    | Enrolled face thumbnail (JPEG)                                             |
| PUT    | `/api/students/<id>`              | ✓    | Update student profile fields                                              |
| DELETE | `/api/students/<id>`              | ✓    | Remove student (cascades attendance)                                       |
| GET    | `/api/departments`                | —    | Distinct department list for dropdowns                                     |
| POST   | `/api/enroll`                     | —    | Enroll student — accepts images or JSON webcam frames, generates embedding |
| POST   | `/api/recognize`                  | —    | Identify face in base64 image, mark attendance                             |
| POST   | `/api/camera/start`               | —    | Activate live camera stream                                                |
| POST   | `/api/camera/stop`                | —    | Stop camera and release resource                                           |
| GET    | `/api/stream`                     | —    | MJPEG live stream (annotated frames)                                       |
| GET    | `/api/events`                     | —    | Server-Sent Events — pushes `{"type":"attendance",...}` on each mark       |
| GET    | `/api/attendance`                 | —    | Attendance for `?date=YYYY-MM-DD`; `?department=` filter                   |
| GET    | `/api/attendance/faculty-summary` | —    | Per-faculty breakdown with student lists for a date                        |
| GET    | `/api/attendance/history`         | —    | 30-day present/absent counts for charts                                    |
| GET    | `/api/attendance/stats`           | —    | Per-student attendance percentages                                         |
| GET    | `/api/attendance/export`          | —    | CSV download; `?department=` adds faculty header block                     |
| GET    | `/api/logs`                       | —    | Recent recognition events; `?limit=N`                                      |
| GET    | `/api/settings`                   | —    | Current threshold and frame-skip values                                    |
| PUT    | `/api/settings`                   | ✓    | Update threshold/frame-skip at runtime                                     |

---

## Database Schema

Four tables, all in PostgreSQL. No external stores.

```sql
-- Admin accounts
users (id, username, password_hash, role)

-- Student profiles + face embeddings
students (id, student_id, full_name, department, email, phone,
          face_image BYTEA,         -- JPEG thumbnail
          embedding vector(512),    -- ArcFace embedding (pgvector)
          sample_count, enrolled_at)

-- One row per student per day
attendance (id, student_id, date, time, status,
            UNIQUE(student_id, date))   -- prevents double-marking

-- Every recognition event (success or failure)
recognition_logs (id, student_id, full_name, confidence, recognized, logged_at)
```

The HNSW index on `students.embedding` enables sub-millisecond cosine similarity search:

```sql
CREATE INDEX students_embedding_hnsw
ON students USING hnsw (embedding vector_cosine_ops)
WITH (m=16, ef_construction=64);
```

---

## IoT / CCTV Integration

One line change in `app.py` inside `_gen_frames()`:

```python
# Current (USB webcam)
cap = cv2.VideoCapture(0)

# Replace with (IP camera / CCTV via RTSP)
cap = cv2.VideoCapture("rtsp://admin:password@192.168.1.100:554/stream")
```

Everything else — recognition, attendance marking, SSE broadcast — is identical. For multiple cameras, maintain a `dict[camera_id → thread]` and expose `/api/stream/<camera_id>`.

---

## Troubleshooting

| Error                                                | Cause                           | Fix                                                                                      |
| ---------------------------------------------------- | ------------------------------- | ---------------------------------------------------------------------------------------- |
| `set_session cannot be used inside a transaction`    | Old psycopg2 misuse             | Ensure `get_db()` does **not** set `conn.autocommit` after connecting — it's the default |
| `pgvector: "missing"` in health check                | Extension not enabled           | Run `psql -d frs -c "CREATE EXTENSION vector;"`                                          |
| `could not find a version that satisfies tensorflow` | Python 3.13 not supported by TF | Not needed — project uses InsightFace + ONNX Runtime instead                             |
| `insightface` install error                          | Missing C++ build tools         | Mac: `xcode-select --install` · Ubuntu: `sudo apt install build-essential`               |
| Camera not opening                                   | Permission denied               | macOS: System Settings → Privacy → Camera → allow Terminal/Python                        |
| `No face detected`                                   | Poor lighting or angle          | Use frontal face, adequate light. Threshold can be lowered to `0.75` for testing         |
| CORS error in browser                                | Wrong API port                  | Confirm backend runs on port `5050` and `flask-cors` is installed                        |
| Login fails with correct password                    | DB not seeded                   | Backend `init_db()` inserts default admin on first start — ensure it ran successfully    |

---

## Accuracy Tips

- Enroll **50–100 images** per student in varied lighting and slight angle changes
- Default threshold `0.80` works well for typical classroom lighting
- Raise to `0.85` in bright, controlled environments to eliminate false positives
- Lower to `0.75` if students with glasses or masks are frequently unrecognized
- Adjust `FRAME_SKIP` in `.env` — lower values (e.g. `1`) process every frame for higher accuracy at the cost of more CPU

---

## Production Notes

```bash
# Replace Flask dev server with Gunicorn (install: pip install gunicorn)
cd backend && source venv/bin/activate
gunicorn -w 2 -b 0.0.0.0:5050 app:app

# Serve frontend with Nginx (point root to frs/frontend/)
# Use Nginx as reverse proxy to Flask on localhost:5050
```

- Set `DEBUG=False` in production (remove `debug=True` from `app.run()`)
- Use a managed PostgreSQL service (Supabase, AWS RDS, Neon) for high availability
- Run daily `pg_dump frs > backup_$(date +%Y%m%d).sql` for database backups
- Store `DATABASE_URL` and other secrets in environment variables, never in source code
