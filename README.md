# Vedanetram · AI-Powered Face Recognition Attendance System

Vedanetram (वेदनेत्रम्) is a web-based attendance system for schools and colleges. It uses a webcam to recognise students' faces automatically, marks attendance in real time, and gives admins and teachers instant reports — no paper registers, no manual data entry.

---

## Table of Contents

1. [How It Works](#how-it-works)
2. [Who Can Use It (Roles)](#who-can-use-it-roles)
3. [Project Structure](#project-structure)
4. [Prerequisites](#prerequisites)
5. [Setup (Step by Step)](#setup-step-by-step)
6. [Running the App](#running-the-app)
7. [Using the System](#using-the-system)
8. [Environment Variables](#environment-variables)
9. [Email Notifications](#email-notifications)
10. [API Reference](#api-reference)
11. [Database Tables](#database-tables)
12. [Troubleshooting](#troubleshooting)
13. [Accuracy Tips](#accuracy-tips)
14. [Production Deployment](#production-deployment)

---

## How It Works

```
Webcam / Photo
      │
      ▼
YOLOv8  ──► detects faces in the frame
      │
      ▼
ArcFace (InsightFace)  ──► converts each face into a 512-number vector (embedding)
      │
      ▼
PostgreSQL + pgvector  ──► compares the vector against all enrolled students
      │                    (sub-millisecond search, scales to 100 000+ students)
      ▼
Match found?  ──YES──► mark attendance in DB + send email notification
                        + push live update to every open browser tab (SSE)
              ──NO───► log as unknown face, do nothing
```

**Key design choices:**

- No image files are saved. Only the 512-number embedding vector is stored.
- One embedding per student, averaged over all their enrolment photos — robust against lighting changes.
- `pgvector`'s HNSW index does similarity search inside PostgreSQL — no slow Python loops.
- Server-Sent Events push live recognition events to the browser; no page refresh needed.

---

## Who Can Use It (Roles)

The system has three roles. Each role sees a different panel after login.

| Role | What they can do |
|---|---|
| **Admin** | Full control — manage faculties, subjects, teachers, students, timetable, settings, view all reports |
| **Teacher** | Start attendance sessions (camera / manual / photo upload), view their own class reports |
| **Student** | *(future)* View their own attendance records |

---

## Project Structure

```
frs/
├── backend/
│   ├── app.py              ← All backend logic: Flask API, AI inference, camera, email
│   ├── face_detector.py    ← YOLOv8 face detection wrapper
│   ├── requirements.txt    ← Python dependencies
│   └── .env                ← Your secrets and settings (create this file)
├── frontend/
│   ├── index.html          ← Single-page app (all pages in one file)
│   └── static/
│       ├── css/style.css   ← Dark-mode design system
│       └── js/app.js       ← All UI logic, API calls, webcam handling
└── start.sh                ← One-command startup script
```

---

## Prerequisites

You need these installed before running the project:

| Tool | Version | Notes |
|---|---|---|
| Python | 3.11 or 3.12 | 3.13 works but InsightFace may need a manual build |
| PostgreSQL | 16+ | The database |
| pgvector | 0.7+ | A PostgreSQL extension for vector search |

> **No TensorFlow, no dlib, no MongoDB.** Face detection uses YOLOv8 and recognition uses ArcFace via ONNX Runtime — lightweight and fast.

---

## Setup (Step by Step)

### Step 1 — Install PostgreSQL and pgvector

**macOS**

```bash
brew install postgresql@16 pgvector
brew services start postgresql@16
echo 'export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

**Ubuntu / Debian**

```bash
sudo apt update
sudo apt install postgresql postgresql-contrib postgresql-16-pgvector
sudo systemctl start postgresql
sudo systemctl enable postgresql
```

**Windows** — download PostgreSQL from [postgresql.org](https://www.postgresql.org/download/windows/), then install pgvector from [github.com/pgvector/pgvector](https://github.com/pgvector/pgvector).

---

### Step 2 — Create the database

```bash
psql -U postgres
```

```sql
CREATE USER frs_user WITH PASSWORD 'frs_password';
CREATE DATABASE frs OWNER frs_user;
GRANT ALL PRIVILEGES ON DATABASE frs TO frs_user;
\q
```

Then enable the vector extension:

```bash
psql -U frs_user -d frs -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

---

### Step 3 — Create your `.env` file

Create a file called `.env` inside the `backend/` folder:

```dotenv
# Required
DATABASE_URL=postgresql://frs_user:frs_password@localhost:5432/frs

# Recognition settings
RECOGNITION_THRESHOLD=0.80
FRAME_SKIP=2

# Email notifications (optional — see Email Notifications section)
BREVO_API_KEY=
BREVO_FROM=
```

---

### Step 4 — Install Python dependencies

```bash
cd backend
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

> On first run, InsightFace downloads the ArcFace model (~300 MB). This happens only once.

---

## Running the App

### Option A — One command (recommended)

```bash
./start.sh
```

This creates the virtual environment (if missing), installs packages, starts the Flask backend on port **5050**, and starts the frontend server on port **8080**.

### Option B — Run each part separately

**Terminal 1 — Backend**

```bash
cd backend
source venv/bin/activate
python app.py
```

**Terminal 2 — Frontend**

```bash
cd frontend
python3 -m http.server 8080
```

### Open the app

Go to **http://localhost:8080** in your browser.

**Default admin login:**
- Username: `admin`
- Password: `admin123`

**Health check** (confirm backend is working):

```bash
curl http://localhost:5050/api/health
# {"status":"ok","db":"ok","pgvector":"ok"}
```

If `"pgvector": "missing"` appears, run the `CREATE EXTENSION` command from Step 2 again.

---

## Using the System

### Admin Panel

After logging in as admin you get a sidebar with these pages:

#### Dashboard
- 4 summary cards: total students, present today, absent today, attendance rate
- 30-day attendance bar chart
- Live recognition event feed (updates instantly when the camera is running)
- Today's full attendance table

#### Students
- Card grid of all enrolled students with search and faculty filter
- Click any card to open the student's full profile: attendance history, monthly chart, recognition log

#### Teachers
- Add, edit, and delete teachers
- Assign each teacher to one or more faculty / semester / subject / day / time slot combinations
- All assignments are shown as tags on the teacher row

#### Enroll a Student
1. Go to **Enroll**
2. Fill in Student ID, Full Name, Faculty, Semester, Email, Phone
3. Choose how to collect face images:
   - **Webcam** — guided 5-pose capture (Front, Left, Right, Up, Down) or continuous auto-capture
   - **Upload** — drag-drop 10–150 photos
4. Click **Enroll** — the system extracts embeddings from every photo, averages them, and saves one vector per student

> If the same face already exists under a different student ID (similarity > 92%), enrolment is rejected to prevent duplicates.

#### Attendance Page
- Faculty tabs across the top (one per faculty, auto-generated)
- **All** tab shows a summary card per faculty with present/absent counts and rate
- Individual faculty tab shows a student-by-student table
- Search bar filters the active tab
- **Export CSV** — exports attendance data for the selected faculty or all faculties

#### Timetable
- Visual grid: rows = time slots, columns = days (Mon–Sat)
- Click any cell to assign a teacher to that slot for a given faculty and semester
- Collision detection prevents double-booking the same teacher

#### Reports
- Date range selector for historical analysis
- Monthly attendance trend (line chart)
- Per-faculty attendance rate (bar chart)
- Per-student percentage table with risk labels: **Good** (≥75%), **At Risk** (<75%), **Critical** (<60%)

#### Manage
- **Faculties** — add/edit/delete faculties (e.g. BCA, BBM, CSIT)
- **Subjects** — add/edit/delete subjects linked to a faculty and semester
- **Time Slots** — add/edit/delete class time slots (search box included)

#### Settings
- Change recognition threshold (0.0–1.0)
- Change frame skip value
- Test email configuration

---

### Teacher Panel

Teachers log in with their own credentials and see a separate panel:

#### Teacher Dashboard
- Today's classes (pulled from their assignments)
- Weekly schedule
- My Assigned Classes — card view and timetable view
- Stats: total classes, sessions marked today

#### Start Attendance (Camera)
1. Go to **Take Attendance**
2. Select the subject and time slot for the class
3. Click **Start Camera** — live webcam feed opens, students are recognised automatically
4. Attendance is marked the moment a face is matched (once per student per subject per day)
5. Click **Close Session** when done

#### Manual Attendance
- Mark students present or absent one by one from a list
- Useful as a backup when the camera is unavailable

#### Recognition Logs
- History of all face recognition events for the teacher's classes

#### Reports (Teacher)
- Per-student attendance percentages for the teacher's assigned subjects
- Filter by subject, semester, date range, or student name
- Risk summary cards (Critical / At Risk / Good counts)
- Export CSV

---

## Environment Variables

All variables go in `backend/.env`.

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | Yes | — | PostgreSQL connection string |
| `RECOGNITION_THRESHOLD` | No | `0.80` | Cosine similarity cutoff (0.0–1.0). Higher = stricter matching |
| `FRAME_SKIP` | No | `2` | Process every Nth camera frame. `1` = every frame (more CPU), `3` = every 3rd frame (less CPU) |
| `BREVO_API_KEY` | No | — | Brevo (formerly Sendinblue) API key for email notifications |
| `BREVO_FROM` | No | — | Verified sender email address in Brevo |
| `SECRET_KEY` | No | auto-generated | HMAC key for QR code signing — set a fixed value in production |

---

## Email Notifications

When a student's attendance is marked, the system can automatically send them an email confirmation.

**Setup:**

1. Create a free account at [brevo.com](https://www.brevo.com) (100 emails/day free forever)
2. Verify a sender email address in Brevo
3. Generate an API key from your Brevo dashboard
4. Add both values to `backend/.env`:

```dotenv
BREVO_API_KEY=your-api-key-here
BREVO_FROM=yourname@yourdomain.com
```

5. Restart the backend — you should see `EMAIL_ENABLED: True` in the startup logs
6. Test it from **Admin → Settings → Test Email**

If `BREVO_API_KEY` or `BREVO_FROM` is not set, email is silently disabled and attendance marking still works normally.

---

## API Reference

All endpoints are on port `5050`. Endpoints marked ✓ in the Auth column require an `Authorization: Bearer <token>` header (you get the token from `/api/auth/login`).

### Auth

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/auth/login` | — | Login with username + password, returns token |
| GET | `/api/auth/me` | ✓ | Current user info |
| POST | `/api/auth/change-password` | ✓ | Change own password |

### Students

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/students` | — | List students; `?q=` search, `?department=` filter |
| GET | `/api/students/<id>` | — | Full profile: metadata, attendance %, monthly chart, recognition log |
| GET | `/api/students/<id>/photo` | — | Enrolled face thumbnail (JPEG) |
| PUT | `/api/students/<id>` | ✓ | Update student fields |
| DELETE | `/api/students/<id>` | ✓ | Delete student (cascades attendance records) |
| GET | `/api/departments` | — | List of distinct faculty codes (for dropdowns) |

### Enrolment & Recognition

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/enroll` | — | Enrol a student with face images (multipart or JSON) |
| POST | `/api/capture/validate-frame` | — | Check a single frame for face quality before capture |
| POST | `/api/recognize` | — | Identify a face in a base64 image; marks attendance |
| POST | `/api/recognize/batch` | — | Identify all faces in a classroom photo (no auto-mark) |

### Camera & Live Stream

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/camera/start` | — | Open the webcam and start recognition loop |
| POST | `/api/camera/stop` | — | Stop the camera and release the resource |
| GET | `/api/stream` | — | MJPEG live stream with bounding boxes drawn |
| GET | `/api/events` | — | Server-Sent Events — pushes `{"type":"attendance",...}` on each mark |

### Attendance

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/attendance` | — | Attendance records for `?date=YYYY-MM-DD`; `?department=` filter |
| GET | `/api/attendance/faculty-summary` | — | Per-faculty breakdown with student list for a date |
| GET | `/api/attendance/history` | — | 30-day present/absent counts for charts |
| GET | `/api/attendance/stats` | — | Per-student attendance percentages |
| GET | `/api/attendance/export` | — | Download CSV; `?department=` adds a faculty header block |
| PUT | `/api/attendance/<sid>/<date>` | ✓ | Manually update one attendance record |
| DELETE | `/api/attendance/<sid>/<date>` | ✓ | Remove one attendance record |

### Attendance Sessions (Teacher Workflow)

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/attendance/sessions` | ✓ | Start a session (resumes if one is already open for same subject/day) |
| GET | `/api/attendance/sessions` | ✓ | List sessions (scoped to the logged-in teacher) |
| GET | `/api/attendance/sessions/<id>` | ✓ | Session details + student list |
| PUT | `/api/attendance/sessions/<id>/close` | ✓ | Close a session |
| POST | `/api/attendance/sessions/<id>/mark` | ✓ | Mark one student in a session |
| POST | `/api/attendance/sessions/<id>/bulk` | ✓ | Bulk mark multiple students + auto-close session |

### Teachers

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/teachers` | ✓ | List teachers with their assignments |
| POST | `/api/teachers` | ✓ | Create a new teacher |
| GET | `/api/teachers/<id>` | ✓ | Single teacher with all assignments |
| PUT | `/api/teachers/<id>` | ✓ | Update teacher basic info |
| DELETE | `/api/teachers/<id>` | ✓ | Delete teacher |
| POST | `/api/teachers/<id>/assignments` | ✓ | Add a faculty/semester/subject/day/timeslot assignment |
| PUT | `/api/teacher-assignments/<id>` | ✓ | Edit an existing assignment (collision-checked) |
| DELETE | `/api/teacher-assignments/<id>` | ✓ | Remove an assignment |

### Teacher Self-Service

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/teacher/me` | ✓ | Logged-in teacher's profile and all assignments |
| GET | `/api/teacher/me/today` | ✓ | Today's classes |
| GET | `/api/teacher/me/schedule` | ✓ | Full weekly schedule keyed by Mon/Tue/… |
| GET | `/api/teacher/me/stats` | ✓ | class_count, today_marked, total_sessions |

### Faculties, Subjects, Time Slots

| Method | Path | Auth | Description |
|---|---|---|---|
| GET/POST | `/api/faculties` | ✓ | List or create faculties |
| PUT/DELETE | `/api/faculties/<id>` | ✓ | Edit or delete a faculty |
| GET/POST | `/api/subjects` | ✓ | List or create subjects; `?faculty_id=&semester=` filters |
| PUT/DELETE | `/api/subjects/<id>` | ✓ | Edit or delete a subject |
| GET/POST | `/api/timeslots` | ✓ | List or create time slots; `?search=&limit=` for search |
| PUT/DELETE | `/api/timeslots/<id>` | ✓ | Edit or delete a time slot |

### Timetable

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/timetable` | ✓ | Timetable grid for `?faculty_id=&semester=` |
| POST | `/api/timetable/check` | ✓ | Check for scheduling collision |
| POST | `/api/timetable/<id>` | ✓ | Create a timetable entry |
| DELETE | `/api/timetable/<id>` | ✓ | Remove a timetable entry |

### Reports

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/reports/my-attendance` | ✓ | Role-scoped attendance report |
| GET | `/api/reports/session-summary` | ✓ | Per-session summary |
| GET | `/api/reports/teacher-performance` | ✓ | Per-student metrics with risk labels (Critical/At Risk/Good) |

### System

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/health` | — | Database and pgvector status |
| GET/PUT | `/api/settings` | ✓ | Read or update threshold and frame-skip |
| GET | `/api/logs` | — | Recent recognition events; `?limit=N` |
| GET | `/api/activity-logs` | ✓ | Admin audit trail |
| POST | `/api/email/test` | ✓ | Send a test email |
| GET | `/api/email/logs` | ✓ | History of sent emails |
| POST | `/api/email/send-attendance-summary` | ✓ | Manually trigger attendance summary emails |

---

## Database Tables

All data lives in PostgreSQL. No external stores, no file system.

```
users               → login accounts (admin + teachers share this table)
students            → enrolled students + face embeddings (vector 512)
teachers            → teacher profiles (name, email, phone, status)
teacher_assignments → each teacher's faculty/semester/subject/day/timeslot combos
faculties           → BCA, BBM, BSc CSIT (or whatever you add)
subjects            → 36+ subjects linked to a faculty and semester
time_slots          → class periods with start/end times
class_schedules     → timetable: which teacher teaches which subject at which slot
attendance_sessions → one open session per teacher/subject/day; tracks open/closed
attendance          → one row per student per subject per day (no duplicates)
recognition_logs    → every face recognition attempt (success or failure)
activity_logs       → admin action audit trail
email_log           → sent email history (prevents duplicate emails)
sessions            → auth session tokens
```

**The face embedding** is stored as a 512-dimension vector in `students.embedding`. PostgreSQL with pgvector searches this column using an HNSW index — the similarity lookup takes under 1 millisecond even with thousands of students.

---

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `"pgvector": "missing"` in health check | Extension not enabled | `psql -d frs -c "CREATE EXTENSION vector;"` |
| InsightFace install error | Missing C++ build tools | macOS: `xcode-select --install` · Ubuntu: `sudo apt install build-essential` |
| Camera not opening | Permission denied | macOS: System Settings → Privacy & Security → Camera → allow Terminal |
| `No face detected` on enrolment | Poor lighting or angle | Use a frontal face in good light; lower threshold to `0.75` for testing |
| Login fails with correct password | Database not initialised | Make sure `python app.py` ran successfully at least once (it creates the admin account) |
| CORS error in browser console | API port mismatch | Confirm backend is on port `5050` and `flask-cors` is in requirements.txt |
| Teacher dashboard shows empty | Auth ID mismatch | Use the latest `app.py` — it fixes the `_tid()` teacher ID resolution |
| Email not sending | Missing env vars | Set both `BREVO_API_KEY` and `BREVO_FROM` in `backend/.env` |

---

## Accuracy Tips

- Enrol **30–100 images** per student across varied lighting and slight angle changes
- The default threshold `0.80` works well for typical classroom lighting
- Raise to `0.85` in bright, controlled environments to reduce false positives
- Lower to `0.75` if students wearing glasses or masks are frequently missed
- Set `FRAME_SKIP=1` to process every camera frame (more accurate, higher CPU usage)
- Set `FRAME_SKIP=3` on slow machines to reduce CPU load

---

## Production Deployment

```bash
# Replace Flask dev server with Gunicorn
pip install gunicorn
cd backend && source venv/bin/activate
gunicorn -w 2 -b 0.0.0.0:5050 app:app

# Serve frontend with Nginx
# Point the root to frs/frontend/ and proxy /api/ to localhost:5050
```

**Checklist before going live:**

- [ ] Change the default admin password from the Settings page
- [ ] Set a fixed `SECRET_KEY` in `.env` (otherwise it regenerates on every restart)
- [ ] Use a managed PostgreSQL service (Supabase, Neon, AWS RDS) for reliability
- [ ] Set up daily database backups: `pg_dump frs > backup_$(date +%Y%m%d).sql`
- [ ] Never commit `.env` to version control — add it to `.gitignore`

---

## IP Camera / CCTV

Change one line in `backend/app.py` inside `_gen_frames()`:

```python
# USB webcam (default)
cap = cv2.VideoCapture(0)

# IP camera via RTSP
cap = cv2.VideoCapture("rtsp://admin:password@192.168.1.100:554/stream")
```

Everything else — recognition, attendance marking, live updates — works identically.