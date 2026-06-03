/* ═══════════════════════════════════════════════════════════════════════════
   वेदनेत्रम् · Smart Attendance System v3.1
   Role-Based: Admin | Teacher | Student

   CHANGES FROM v3.0:
   ──────────────────
   ISSUE 1  — Dashboard graph fixed max-height; SimpleChart scales dynamically
   ISSUE 3  — Teacher modal: semester change triggers subject reload (filtered)
   ISSUE 4  — Teacher assignments panel: add/remove multiple subjects
   ISSUE 5  — Schedule conflict shown as clear error in teacher modal
   ISSUE 6  — api() intercepts 401 SESSION_EXPIRED, clears stale token, shows
               session-expired message instead of silent failure
   ═══════════════════════════════════════════════════════════════════════════ */

const API = "http://localhost:5050/api";

/* ── Global state ─────────────────────────────────────────────────────────── */
let token = localStorage.getItem("frs_token") || "";
let userRole = localStorage.getItem("frs_role") || "";
let userInfo = JSON.parse(localStorage.getItem("frs_user") || "null");
let currentPage = "";

/* ── Enroll state ─────────────────────────────────────────────────────────── */
let enrollStream = null;
let enrollFrames = [];
const POSE_SEQUENCE = [
  {
    pose: "front",
    target: 10,
    instruction: "Look straight at the camera",
    voiceInstruction: "Look straight at the camera",
  },
  {
    pose: "left",
    target: 10,
    instruction: "Turn your head to your LEFT",
    voiceInstruction: "Turn your head to your left",
  },
  {
    pose: "right",
    target: 10,
    instruction: "Turn your head to your RIGHT",
    voiceInstruction: "Turn your head to your right",
  },
  {
    pose: "up",
    target: 10,
    instruction: "Raise your chin slightly upward",
    voiceInstruction: "Raise your chin slightly upward",
  },
  {
    pose: "down",
    target: 10,
    instruction: "Lower your chin toward your chest",
    voiceInstruction: "Lower your chin toward your chest",
  },
];
const TOTAL_FRAMES = 50;
let enrollStep = 1;
let isAutoCaptureActive = false;
let captureAF = null;
let poseCaptureCounts = { front: 0, left: 0, right: 0, up: 0, down: 0 };
let _captureComplete = false;
let _validateInFlight = false;
let lastValidateTime = 0;
let currentQuality = null;
let currentPoseHint = "front";

/* ── Webcam / recognition state ───────────────────────────────────────────── */
let recogStream = null;
let teacherRecogStream = null;
let teacherAutoLoop = null;
let teacherAutoRunning = false;
let sessionActive = false;

/* ── Manual attendance ────────────────────────────────────────────────────── */
let manualAttMap = {};

/* ── Charts ───────────────────────────────────────────────────────────────── */
let dashboardChart = null,
  monthlyChart = null,
  deptChart = null;

/* ── Health polling ───────────────────────────────────────────────────────── */
let _healthInterval = null;
let _sseSource = null;

/* ═══════════════════════════════════════════════════════════════════════════
   UTILITIES
   ═══════════════════════════════════════════════════════════════════════════ */

/**
 * ISSUE 6 FIX: api() now intercepts SESSION_EXPIRED errors.
 * When the backend returns {error, code:"SESSION_EXPIRED"} the frontend
 * clears localStorage and redirects to login instead of leaving the user
 * on a broken page with silent 401 errors in the console.
 */
function api(path, opts = {}) {
  return fetch(API + path, {
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...(opts.headers || {}),
    },
    ...opts,
  }).then(async (r) => {
    const data = await r.json().catch(() => ({}));

    // ── ISSUE 6: Handle expired/invalid session gracefully ──
    if (r.status === 401) {
      const isSessionExpired = data.code === "SESSION_EXPIRED";
      if (isSessionExpired) {
        // Clear stale token and redirect to login with user-friendly message
        ["frs_token", "frs_role", "frs_user"].forEach((k) =>
          localStorage.removeItem(k),
        );
        token = "";
        userRole = "";
        userInfo = null;
        // Show login overlay with message
        const overlay = document.getElementById("loginOverlay");
        if (overlay) overlay.classList.remove("hidden");
        const errEl = document.getElementById("loginErr");
        if (errEl) {
          errEl.textContent = "Your session has expired. Please sign in again.";
          errEl.className = "msg err";
        }
        // Hide app content
        const student = document.getElementById("student-panel");
        if (student) student.classList.remove("active");
        selectLoginRole("admin");
        throw new Error("Session expired — please sign in again.");
      }
    }

    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  });
}

function toast(msg, type = "ok") {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.className = `toast ${type} show`;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 3500);
}

function $(id) {
  return document.getElementById(id);
}

function hide(el) {
  if (el) el.classList.add("hidden");
}
function show(el, displayType = "block") {
  if (!el) return;
  el.classList.remove("hidden");
}

function badge(text, type = "blue") {
  return `<span class="pill pill-${type}">${text}</span>`;
}
function statusBadge(status) {
  const map = {
    Present: "green",
    Absent: "red",
    active: "green",
    inactive: "amber",
    graduated: "blue",
    suspended: "red",
  };
  return badge(status, map[status] || "blue");
}
function fmtDate(d) {
  if (!d) return "—";
  return new Date(d).toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}
function setErr(id, msg) {
  const el = $(id);
  if (!el) return;
  el.textContent = msg;
  el.className = msg ? "msg err" : "msg";
}
function closeModal(id) {
  $(id).classList.remove("open");
}
function openModal(id) {
  $(id).classList.add("open");
}

/* ═══════════════════════════════════════════════════════════════════════════
   HEALTH CHECK
   ═══════════════════════════════════════════════════════════════════════════ */
function setApiStatus(online) {
  const dot = $("apiDot"),
    label = $("apiStatus");
  if (!dot || !label) return;
  if (online) {
    dot.className = "status-dot ok";
    label.textContent = "Online";
  } else {
    dot.className = "status-dot err";
    label.textContent = "Offline";
  }
}
async function checkHealth() {
  try {
    await api("/health");
    setApiStatus(true);
  } catch {
    setApiStatus(false);
  }
}
function startHealthPolling() {
  clearInterval(_healthInterval);
  checkHealth();
  _healthInterval = setInterval(checkHealth, 15000);
}

/* ═══════════════════════════════════════════════════════════════════════════
   SSE
   ═══════════════════════════════════════════════════════════════════════════ */
function startSSE() {
  if (_sseSource) {
    try {
      _sseSource.close();
    } catch {}
  }
  _sseSource = new EventSource(`${API}/events`);
  _sseSource.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      if (d.type === "attendance") {
        addLiveLog(d);
        if (currentPage === "dashboard") loadDashboard();
        if (currentPage === "attendance") loadAttendancePage();
        if (currentPage === "t-dashboard") loadTeacherDashboard();
        if (currentPage === "t-recognize") refreshSessionLog();
      }
    } catch {}
  };
}

/* ═══════════════════════════════════════════════════════════════════════════
   CLOCK
   ═══════════════════════════════════════════════════════════════════════════ */
function startClock() {
  const tick = () => {
    const el = $("clockDisplay");
    if (el) el.textContent = new Date().toLocaleTimeString();
  };
  tick();
  setInterval(tick, 1000);
}

/* ═══════════════════════════════════════════════════════════════════════════
   AUTH
   ═══════════════════════════════════════════════════════════════════════════ */
let loginRole = "admin";

function selectLoginRole(role) {
  loginRole = role;
  ["admin", "teacher", "student"].forEach((r) =>
    $(`roleBtn-${r}`)?.classList.remove("active"),
  );
  $(`roleBtn-${role}`)?.classList.add("active");
  $("field-username").classList.toggle("hidden", role === "student");
  $("field-email").classList.toggle("hidden", role !== "student");
  const userLabel = $("usernameLabel"),
    userInput = $("loginUser");
  if (userLabel && userInput) {
    if (role === "teacher") {
      userLabel.textContent = "Teacher Email";
      userInput.type = "email";
      userInput.placeholder = "teacher@college.edu";
    } else {
      userLabel.textContent = "Username";
      userInput.type = "text";
      userInput.placeholder = "admin";
    }
  }
  const hints = {
    admin: "Default: admin / admin123",
    teacher: "Use your registered email and password",
    student: "Enter your registered email address",
  };
  $("loginHint").textContent = hints[role];
  $("loginErr").textContent = "";
}

async function doLogin() {
  $("loginErr").textContent = "";
  let body;
  if (loginRole === "student") {
    const email = $("loginEmail").value.trim();
    if (!email) {
      $("loginErr").textContent = "Email required";
      return;
    }
    body = { role: "student", email };
  } else if (loginRole === "teacher") {
    const email = $("loginUser").value.trim(),
      password = $("loginPass").value;
    if (!email || !password) {
      $("loginErr").textContent = "Email and password required";
      return;
    }
    body = { role: "teacher", email, password };
  } else {
    const username = $("loginUser").value.trim(),
      password = $("loginPass").value;
    if (!username || !password) {
      $("loginErr").textContent = "Username and password required";
      return;
    }
    body = { role: loginRole, username, password };
  }
  try {
    const data = await api("/auth/login", {
      method: "POST",
      body: JSON.stringify(body),
    });
    token = data.token;
    userRole = data.role;
    userInfo = data;
    localStorage.setItem("frs_token", token);
    localStorage.setItem("frs_role", userRole);
    localStorage.setItem("frs_user", JSON.stringify(data));
    afterLogin();
  } catch (e) {
    $("loginErr").textContent = e.message;
  }
}

function afterLogin() {
  $("loginOverlay").classList.add("hidden");
  if (userRole === "student") {
    showStudentPanel();
    return;
  }
  if (userRole === "admin") {
    show($("adminNav"));
    hide($("teacherNav"));
  } else if (userRole === "teacher") {
    show($("teacherNav"));
    hide($("adminNav"));
  }
  const label =
    userRole === "teacher"
      ? userInfo.full_name || userInfo.teacher_id
      : userInfo.username || "admin";
  const roleLabel = userRole === "teacher" ? "Teacher" : "Admin";
  const ui = $("sidebarUserInfo");
  if (ui)
    ui.innerHTML = `
    <div style="display:flex;flex-direction:column;gap:1px;padding:0 0.5rem 0.25rem;">
      <span style="font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.08em;">${roleLabel}</span>
      <span style="font-size:12px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${label}</span>
    </div>`;
  if (userRole === "admin") navigate("dashboard");
  else navigate("t-dashboard");
  startClock();
  startSSE();
  startHealthPolling();
}

function logout() {
  api("/auth/logout", { method: "POST" }).catch(() => {});
  ["frs_token", "frs_role", "frs_user"].forEach((k) =>
    localStorage.removeItem(k),
  );
  token = "";
  userRole = "";
  userInfo = null;
  clearInterval(_healthInterval);
  if (_sseSource) {
    try {
      _sseSource.close();
    } catch {}
    _sseSource = null;
  }
  try {
    if (typeof stopTeacherWebcam === "function") stopTeacherWebcam();
  } catch {}
  try {
    if (teacherRecogStream) {
      teacherRecogStream.getTracks().forEach((t) => t.stop());
      teacherRecogStream = null;
    }
  } catch {}
  sessionActive = false;
  $("student-panel").classList.remove("active");
  $("loginOverlay").classList.remove("hidden");
  setApiStatus(false);
  $("loginErr").textContent = "";
  ["loginUser", "loginPass", "loginEmail"].forEach((id) => {
    if ($(id)) $(id).value = "";
  });
  selectLoginRole("admin");
}

/* ═══════════════════════════════════════════════════════════════════════════
   NAVIGATION
   ═══════════════════════════════════════════════════════════════════════════ */
function navigate(page) {
  if (page !== "enroll") {
    try {
      stopAutoCapture();
      if (enrollStream) {
        enrollStream.getTracks().forEach((t) => t.stop());
        enrollStream = null;
      }
    } catch {}
  }
  if (page !== "t-recognize" && page !== "t-dashboard") {
    try {
      if (typeof stopTeacherWebcam === "function") stopTeacherWebcam();
    } catch {}
  }
  currentPage = page;
  document
    .querySelectorAll(".page")
    .forEach((p) => p.classList.remove("active"));
  document
    .querySelectorAll(".nav-item")
    .forEach((n) => n.classList.remove("active"));
  const pageEl = $(`page-${page}`);
  if (pageEl) pageEl.classList.add("active");
  const navEl = document.querySelector(`.nav-item[data-page="${page}"]`);
  if (navEl) navEl.classList.add("active");
  $("mainContent")?.scrollTo(0, 0);
  const loaders = {
    dashboard: loadDashboard,
    students: loadStudents,
    teachers: loadTeachers,
    attendance: loadAttendancePage,
    reports: loadReports,
    settings: loadSettings,
    manage: loadManage,
    enroll: populateEnrollFaculty,
    "t-dashboard": loadTeacherDashboard,
    "t-recognize": initTeacherRecognize,
    "t-manual": loadManualAttendance,
    "t-logs": loadTeacherLogs,
  };
  if (loaders[page]) loaders[page]();
}

/* ═══════════════════════════════════════════════════════════════════════════
   LIVE LOG HELPER
   ═══════════════════════════════════════════════════════════════════════════ */
function addLiveLog(d) {
  const container = $("liveLog") || $("tSessionLog");
  if (!container) return;
  const div = document.createElement("div");
  div.className = "log-entry";
  div.innerHTML = `
    <div style="display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap;">
      <span class="pill pill-green">✓ Present</span>
      <span style="font-weight:600;">${d.name}</span>
      <span style="color:var(--text3);font-size:11px;font-family:var(--mono);">${d.student_id}</span>
    </div>
    <div style="font-size:11px;color:var(--text3);margin-top:2px;font-family:var(--mono);">
      ${d.time}${d.subject ? " · " + d.subject : ""}
    </div>`;
  container.prepend(div);
  if (container.children.length > 60) container.lastElementChild.remove();
}

/* ═══════════════════════════════════════════════════════════════════════════
   STUDENT PANEL
   ═══════════════════════════════════════════════════════════════════════════ */
async function showStudentPanel() {
  $("student-panel").classList.add("active");
  const name = userInfo.full_name || "Student";
  $("studentHeaderName").textContent = userInfo.student_id || "";
  $("studentWelcomeName").textContent = `Welcome, ${name}`;
  $("studentWelcomeSub").textContent =
    `${userInfo.department || ""}${userInfo.semester ? " · Semester " + userInfo.semester : ""}`;
  try {
    const data = await api("/student/attendance");
    const st = data.stats || {};
    $("sStat-present").textContent = st.total_present ?? "—";
    $("sStat-total").textContent = st.total_days ?? "—";
    const pct = st.percentage ?? null;
    $("sStat-pct").textContent = pct != null ? `${pct}%` : "—";
    $("sStat-pct").className =
      `student-stat-val ${pct != null && pct < 75 ? "amber" : "green"}`;
    const subjectMap = {};
    (data.records || []).forEach((r) => {
      const k = r.subject_name || "General";
      subjectMap[k] = subjectMap[k] || { present: 0, total: 0 };
      subjectMap[k].total++;
      if (r.status === "Present") subjectMap[k].present++;
    });
    const subjHtml =
      Object.entries(subjectMap)
        .map(([sub, v]) => {
          const p = Math.round((v.present / v.total) * 100);
          return `<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border);font-size:13px;">
        <span>${sub}</span>
        <div style="display:flex;align-items:center;gap:0.75rem;">
          <span style="color:var(--text2);font-size:12px;">${v.present}/${v.total}</span>
          <span style="font-weight:600;font-family:var(--mono);color:${p >= 75 ? "var(--green)" : "var(--red)"};">${p}%</span>
        </div>
      </div>`;
        })
        .join("") ||
      `<div style="color:var(--text3);padding:1rem 0;font-size:13px;">No records yet</div>`;
    const subjEl = $("studentSubjectSummary");
    if (subjEl) subjEl.innerHTML = subjHtml;
    const tbody = $("studentAttBody");
    if (!data.records || !data.records.length) {
      tbody.innerHTML = `<tr><td colspan="4" style="text-align:center;color:var(--text3);padding:2rem;">No attendance records yet</td></tr>`;
      return;
    }
    tbody.innerHTML = data.records
      .map(
        (r) => `
      <tr>
        <td>${fmtDate(r.date)}</td>
        <td>${r.subject_name || "—"}</td>
        <td>${r.teacher_name || "—"}</td>
        <td>${statusBadge(r.status)}</td>
      </tr>`,
      )
      .join("");
  } catch (e) {
    const b = $("studentAttBody");
    if (b)
      b.innerHTML = `<tr><td colspan="4" style="color:var(--red);text-align:center;padding:1rem;">${e.message}</td></tr>`;
  }
}

/* ═══════════════════════════════════════════════════════════════════════════
   ADMIN — DASHBOARD
   ═══════════════════════════════════════════════════════════════════════════ */
async function loadDashboard() {
  const today = new Date().toISOString().slice(0, 10);
  const dept = $("dashDeptFilter")?.value || "";
  $("dashDate").textContent = new Date().toLocaleDateString("en-GB", {
    weekday: "long",
    day: "2-digit",
    month: "long",
    year: "numeric",
  });
  try {
    const [summ, students, hist] = await Promise.all([
      api(`/attendance/faculty-summary?date=${today}`),
      api("/students"),
      api(
        `/attendance/history${dept ? "?department=" + encodeURIComponent(dept) : ""}`,
      ),
    ]);
    const df = $("dashDeptFilter");
    if (df && df.options.length <= 1) {
      summ.faculties.forEach((f) => {
        const o = document.createElement("option");
        o.value = o.textContent = f.name;
        df.appendChild(o);
      });
    }
    const facs = dept
      ? summ.faculties.filter((f) => f.name === dept)
      : summ.faculties;
    const overall = dept
      ? facs[0] || { total: 0, present: 0, absent: 0, rate: 0 }
      : summ.overall;
    $("dashMetrics").innerHTML = `
      <div class="metric-card"><div class="metric-label">Total Students</div><div class="metric-val">${students.count}</div></div>
      <div class="metric-card"><div class="metric-label">Present Today</div><div class="metric-val text-green">${overall.present}</div></div>
      <div class="metric-card"><div class="metric-label">Absent Today</div><div class="metric-val text-red">${overall.absent}</div></div>
      <div class="metric-card"><div class="metric-label">Attendance Rate</div><div class="metric-val text-blue">${overall.rate ?? 0}%</div></div>`;
    drawDashboardChart((hist.history || []).slice().reverse());
    let html = "";
    for (const f of facs) {
      html += `<div class="card mb-1rem">
        <div class="split-row mb-0-75rem">
          <span class="section-title">${f.name}</span>
          <div class="inline-kpis">
            <span class="text-green">${f.present} present</span><span class="text-muted">·</span>
            <span class="text-red">${f.absent} absent</span><span class="text-muted">·</span>
            <span class="text-blue">${f.rate}%</span>
          </div>
        </div>
        <div class="overflow-x-auto">
          <table class="data-table"><thead><tr><th>Student</th><th>ID</th><th>Time</th><th>Status</th></tr></thead>
          <tbody>${f.students.map((s) => `<tr><td>${s.name}</td><td class="mono-muted">${s.student_id}</td><td class="mono-cell">${s.time}</td><td>${statusBadge(s.status)}</td></tr>`).join("")}</tbody></table>
        </div>
      </div>`;
    }
    $("dashAttTable").innerHTML =
      html || `<div class="card empty-state">No student data yet.</div>`;
  } catch (e) {
    $("dashMetrics").innerHTML = `<div class="text-red">${e.message}</div>`;
  }
}

/* ─────────────────────────────────────────────────────────────────────────────
   ISSUE 1 FIX: SimpleChart — fixed container height, dynamic Y scaling.
   The canvas is sized to its container width but capped at a fixed height.
   Y-axis gridlines are computed from the actual data range, not from a
   hardcoded step, so the chart always fills the container cleanly regardless
   of how many students (or how large the numbers) are in the dataset.
   ───────────────────────────────────────────────────────────────────────── */
function drawDashboardChart(history) {
  const canvas = $("dashboardChart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (dashboardChart) dashboardChart.destroy();
  dashboardChart = new SimpleChart(ctx, {
    type: "bar",
    labels: history.map((h) => (h.date || "").slice(5)),
    datasets: [
      {
        label: "Present",
        data: history.map((h) => h.present),
        color: "#22c55e",
      },
      { label: "Absent", data: history.map((h) => h.absent), color: "#ef4444" },
    ],
  });
}

/* ═══════════════════════════════════════════════════════════════════════════
   ADMIN — STUDENTS
   ═══════════════════════════════════════════════════════════════════════════ */
let allStudents = [],
  studentDept = "";

async function loadStudents() {
  try {
    const [data, depts] = await Promise.all([
      api("/students"),
      api("/departments"),
    ]);
    allStudents = data.students;
    const tabs = $("studentFacultyTabs");
    tabs.innerHTML = `<button class="filter-btn active" onclick="filterStudentDept('',this)">All (${allStudents.length})</button>`;
    depts.departments.forEach((d) => {
      const cnt = allStudents.filter((s) => s.department === d).length;
      tabs.innerHTML += `<button class="filter-btn" onclick="filterStudentDept('${d}',this)">${d} (${cnt})</button>`;
    });
    const dl = $("editDeptList");
    if (dl) {
      dl.innerHTML = "";
      depts.departments.forEach((d) => {
        const o = document.createElement("option");
        o.value = d;
        dl.appendChild(o);
      });
    }
    renderStudents(allStudents);
  } catch (e) {
    $("studentFacultyBar").innerHTML =
      `<div class="card" style="color:var(--red);">${e.message}</div>`;
  }
}

function filterStudentDept(dept, btn) {
  studentDept = dept;
  document
    .querySelectorAll("#studentFacultyTabs .filter-btn")
    .forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  const q = $("studentSearch").value.toLowerCase();
  renderStudents(
    allStudents
      .filter((s) => !dept || s.department === dept)
      .filter(
        (s) =>
          !q ||
          s.full_name.toLowerCase().includes(q) ||
          s.student_id.toLowerCase().includes(q),
      ),
  );
}
function searchStudents() {
  const q = $("studentSearch").value.toLowerCase();
  renderStudents(
    allStudents
      .filter((s) => !studentDept || s.department === studentDept)
      .filter(
        (s) =>
          !q ||
          s.full_name.toLowerCase().includes(q) ||
          s.student_id.toLowerCase().includes(q),
      ),
  );
}
function renderStudents(list) {
  if (!list.length) {
    $("studentFacultyBar").innerHTML =
      `<div class="card empty-state">No students found</div>`;
    return;
  }
  $("studentFacultyBar").innerHTML = `
    <div class="card full-width-card">
      <div class="teacher-table-wrap">
        <table class="data-table students-table">
          <thead><tr><th>Student</th><th>ID</th><th>Faculty</th><th>Semester</th><th>Samples</th><th>Status</th></tr></thead>
          <tbody>
            ${list
              .map(
                (
                  s,
                ) => `<tr onclick="navigate('profile');loadProfile('${s.student_id}')" class="click-row">
              <td><div class="student-table-person">
                <div class="student-avatar small">
                  <img src="${API}/students/${s.student_id}/photo" onerror="this.classList.add('hidden');this.nextElementSibling.classList.remove('hidden')"/>
                  <div class="avatar-placeholder hidden">${s.full_name.charAt(0).toUpperCase()}</div>
                </div>
                <span>${s.full_name}</span>
              </div></td>
              <td class="mono-muted">${s.student_id}</td>
              <td>${s.department || "Unassigned"}</td>
              <td class="mono-cell">${s.semester || "—"}</td>
              <td class="mono-cell">${s.sample_count}</td>
              <td>${statusBadge(s.status)}</td>
            </tr>`,
              )
              .join("")}
          </tbody>
        </table>
      </div>
    </div>`;
}

async function loadProfile(sid) {
  $("profileContent").innerHTML =
    `<div class="card" style="padding:2rem;text-align:center;color:var(--text3);">Loading…</div>`;
  try {
    const s = await api(`/students/${sid}`);
    const pct = s.stats?.percentage ?? null;
    $("profileContent").innerHTML = `
      <div class="profile-header card" style="display:flex;gap:1.5rem;align-items:flex-start;flex-wrap:wrap;margin-bottom:1rem;">
        <div style="width:90px;height:90px;border-radius:50%;overflow:hidden;background:var(--bg4);flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:28px;font-weight:700;color:var(--text2);">
          <img src="${API}/students/${sid}/photo" onerror="this.style.display='none';this.parentElement.textContent='${s.full_name.charAt(0)}'" style="width:100%;height:100%;object-fit:cover;"/>
        </div>
        <div style="flex:1;min-width:200px;">
          <h2 style="font-size:20px;margin-bottom:4px;">${s.full_name}</h2>
          <div style="font-family:var(--mono);color:var(--text3);font-size:12px;margin-bottom:0.5rem;">${s.student_id}</div>
          <div style="display:flex;gap:0.5rem;flex-wrap:wrap;align-items:center;">${statusBadge(s.status)}${s.department ? badge(s.department, "blue") : ""}${s.semester ? badge("Sem " + s.semester, "amber") : ""}</div>
          <div style="margin-top:0.75rem;display:grid;grid-template-columns:1fr 1fr;gap:0.5rem;font-size:12px;color:var(--text2);">
            <div>📧 ${s.email || "—"}</div><div>📱 ${s.phone || "—"}</div>
            <div>👁 ${s.sample_count} face samples</div><div>📅 Enrolled ${fmtDate(s.enrolled_at)}</div>
          </div>
        </div>
        <div style="display:flex;gap:0.5rem;flex-wrap:wrap;">
          ${
            userRole === "admin"
              ? `
            <button class="btn-secondary" onclick="openEditModal('${s.student_id}','${s.full_name}','${s.department || ""}','${s.semester || ""}','${s.email || ""}','${s.phone || ""}','${s.status}')">Edit</button>
            <button class="btn-secondary" style="color:var(--red);border-color:var(--red-dim);" onclick="deleteStudent('${s.student_id}','${s.full_name}')">Delete</button>`
              : ""
          }
        </div>
      </div>
      <div class="metrics-grid" style="margin-bottom:1rem;">
        <div class="metric-card"><div class="metric-label">Present</div><div class="metric-val" style="color:var(--green)">${s.stats?.total_present ?? 0}</div></div>
        <div class="metric-card"><div class="metric-label">Total Days</div><div class="metric-val">${s.stats?.total_days ?? 0}</div></div>
        <div class="metric-card"><div class="metric-label">Rate</div><div class="metric-val" style="color:${pct != null && pct < 75 ? "var(--red)" : "var(--green)"}">${pct != null ? pct + "%" : "—"}</div></div>
      </div>
      <div class="card">
        <div class="section-title" style="margin-bottom:0.75rem;">Attendance History</div>
        <div style="overflow-x:auto;">
          <table class="student-att-table">
            <thead><tr><th>Date</th><th>Subject</th><th>Teacher</th><th>Status</th>${userRole === "admin" ? "<th>Action</th>" : ""}</tr></thead>
            <tbody>
              ${
                (s.attendance || []).length
                  ? (s.attendance || [])
                      .map(
                        (r) => `<tr>
                <td>${fmtDate(r.date)}</td>
                <td>${r.subject_name || "—"}</td>
                <td>${r.teacher_name || "—"}</td>
                <td>${statusBadge(r.status)}</td>
                ${userRole === "admin" ? `<td><button class="btn-secondary" style="font-size:11px;padding:2px 8px;" onclick="toggleAttendance('${sid}','${r.date}','${r.status}')">Toggle</button></td>` : ""}
              </tr>`,
                      )
                      .join("")
                  : `<tr><td colspan="5" style="text-align:center;color:var(--text3);padding:1.5rem;">No records</td></tr>`
              }
            </tbody>
          </table>
        </div>
      </div>`;
  } catch (e) {
    $("profileContent").innerHTML =
      `<div class="card" style="color:var(--red);">${e.message}</div>`;
  }
}

async function toggleAttendance(sid, date, currentStatus) {
  const newStatus = currentStatus === "Present" ? "Absent" : "Present";
  try {
    await api(`/attendance/${sid}/${date}`, {
      method: "PUT",
      body: JSON.stringify({ status: newStatus }),
    });
    toast(`Marked ${newStatus}`);
    loadProfile(sid);
  } catch (e) {
    toast(e.message, "err");
  }
}
function openEditModal(sid, name, dept, sem, email, phone, status) {
  $("editSid").value = sid;
  $("editName").value = name;
  $("editDept").value = dept;
  $("editSem").value = sem;
  $("editEmail").value = email;
  $("editPhone").value = phone;
  $("editStatus").value = status;
  setErr("editModalErr", "");
  openModal("editModal");
}
async function saveStudentEdit() {
  const sid = $("editSid").value;
  const body = {
    full_name: $("editName").value.trim(),
    department: $("editDept").value.trim(),
    semester: $("editSem").value.trim(),
    email: $("editEmail").value.trim(),
    phone: $("editPhone").value.trim(),
    status: $("editStatus").value,
  };
  try {
    await api(`/students/${sid}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
    toast("Student updated");
    closeModal("editModal");
    loadProfile(sid);
  } catch (e) {
    setErr("editModalErr", e.message);
  }
}
async function deleteStudent(sid, name) {
  if (!confirm(`Delete ${name} (${sid})?`)) return;
  try {
    await api(`/students/${sid}`, { method: "DELETE" });
    toast("Student deleted");
    navigate("students");
  } catch (e) {
    toast(e.message, "err");
  }
}

/* ═══════════════════════════════════════════════════════════════════════════
   ADMIN — TEACHERS  (Issues 3, 4, 5, 6)
   ═══════════════════════════════════════════════════════════════════════════ */
let faculties = [],
  allSubjects = [],
  timeSlots = [];

async function loadTeachers() {
  try {
    const [tData, fData, tsData] = await Promise.all([
      api("/teachers"),
      api("/faculties"),
      api("/time-slots"),
    ]);
    faculties = fData.faculties;
    timeSlots = tsData.time_slots;
    renderTeacherTable(tData.teachers);
  } catch (e) {
    $("teacherTableBody").innerHTML =
      `<tr><td colspan="9" style="color:var(--red);padding:1rem;">${e.message}</td></tr>`;
  }
}

function renderTeacherTable(teachers) {
  if (!teachers.length) {
    $("teacherTableBody").innerHTML =
      `<tr><td colspan="9" style="text-align:center;color:var(--text3);padding:2rem;">No teachers yet. Click "+ Add Teacher" to create one.</td></tr>`;
    return;
  }
  $("teacherTableBody").innerHTML = teachers
    .map((t) => {
      // Show all assignments as pills if multiple exist
      const assignmentPills =
        (t.assignments || []).length > 1
          ? (t.assignments || [])
              .map(
                (a) =>
                  `<span class="pill pill-blue" style="font-size:10px;margin:1px;">${a.subject_name} (Sem ${a.semester})</span>`,
              )
              .join("")
          : t.subject_name || "—";
      return `<tr>
      <td style="font-family:var(--mono);font-size:11px;">${t.teacher_id}</td>
      <td style="font-weight:500;">${t.full_name}</td>
      <td>${t.faculty_name || "—"}</td>
      <td style="font-family:var(--mono);">${t.semester || "—"}</td>
      <td style="font-size:12px;">${assignmentPills}</td>
      <td style="font-size:12px;color:var(--text2);">${t.time_slot_label || "—"}</td>
      <td style="font-size:12px;color:var(--text2);">${t.email || "—"}</td>
      <td>${statusBadge(t.status)}</td>
      <td>
        <div class="action-btns">
          <button class="btn-secondary" style="font-size:11px;padding:3px 8px;" onclick="openTeacherModal(${t.id})">Edit</button>
          <button class="btn-secondary" style="font-size:11px;padding:3px 8px;" onclick="openAssignmentsModal(${t.id},'${t.full_name}')">Assign</button>
          <button class="btn-secondary" style="font-size:11px;padding:3px 8px;color:var(--red);" onclick="deleteTeacher(${t.id},'${t.full_name}')">Del</button>
        </div>
      </td>
    </tr>`;
    })
    .join("");
}

async function openTeacherModal(tId) {
  try {
    const [fData, tsData] = await Promise.all([
      api("/faculties"),
      api("/time-slots"),
    ]);
    faculties = fData.faculties;
    timeSlots = tsData.time_slots;
  } catch {}

  $("tmFaculty").innerHTML =
    `<option value="">Select faculty</option>` +
    faculties.map((f) => `<option value="${f.id}">${f.name}</option>`).join("");
  $("tmTimeSlot").innerHTML =
    `<option value="">Select time slot</option>` +
    timeSlots
      .map((ts) => `<option value="${ts.id}">${ts.label}</option>`)
      .join("");
  $("tmSubject").innerHTML = `<option value="">Select subject</option>`;
  setErr("teacherModalErr", "");

  if (tId) {
    let t = null;
    try {
      t = await api(`/teachers/${tId}`);
    } catch (e) {
      setErr("teacherModalErr", e.message);
      return;
    }
    if (t) {
      $("teacherModalTitle").textContent = "Edit Teacher";
      $("tmId").value = t.id;
      $("tmTeacherId").value = t.teacher_id;
      $("tmFullName").value = t.full_name;
      $("tmPassword").value = "";
      $("tmPassword").placeholder = "Leave blank to keep current";
      $("tmEmail").value = t.email || "";
      $("tmPhone").value = t.phone || "";
      $("tmStatus").value = t.status;
      $("tmFaculty").value = t.faculty_id || "";
      $("tmSemester").value = t.semester || "";
      $("tmTimeSlot").value = t.time_slot_id || "";
      // ISSUE 3: load subjects filtered by both faculty AND semester
      if (t.faculty_id && t.semester) {
        await loadSubjectsForModal();
        $("tmSubject").value = t.subject_id || "";
      }
    }
  } else {
    $("teacherModalTitle").textContent = "Add Teacher";
    $("tmId").value = "";
    [
      "tmTeacherId",
      "tmFullName",
      "tmPassword",
      "tmEmail",
      "tmPhone",
      "tmSemester",
    ].forEach((id) => ($(id).value = ""));
    $("tmStatus").value = "active";
    $("tmFaculty").value = "";
    $("tmPassword").placeholder = "Min 6 characters";
  }
  openModal("teacherModal");
}

/**
 * ISSUE 3 FIX: loadSubjectsForModal now requires BOTH faculty AND semester
 * before fetching. Subject dropdown stays empty (with clear message) until
 * semester is also selected.
 */
async function loadSubjectsForModal() {
  const fid = $("tmFaculty").value;
  const sem = $("tmSemester").value;

  // Clear subject dropdown with a helpful message
  if (!fid) {
    $("tmSubject").innerHTML = `<option value="">Select faculty first</option>`;
    return;
  }
  if (!sem) {
    $("tmSubject").innerHTML =
      `<option value="">Select semester first</option>`;
    return;
  }

  $("tmSubject").innerHTML = `<option value="">Loading subjects…</option>`;
  try {
    // ISSUE 3: Filter by BOTH faculty_id AND semester
    const data = await api(`/subjects?faculty_id=${fid}&semester=${sem}`);
    if (!data.subjects || data.subjects.length === 0) {
      $("tmSubject").innerHTML =
        `<option value="">No subjects in Semester ${sem} for this faculty</option>`;
      return;
    }
    $("tmSubject").innerHTML =
      `<option value="">Select subject</option>` +
      data.subjects
        .map(
          (s) =>
            `<option value="${s.id}">${s.name}${s.code ? " · " + s.code : ""}</option>`,
        )
        .join("");
  } catch (e) {
    $("tmSubject").innerHTML =
      `<option value="">Error loading subjects</option>`;
  }
}

async function saveTeacher() {
  const id = $("tmId").value;
  const body = {
    teacher_id: $("tmTeacherId").value.trim(),
    full_name: $("tmFullName").value.trim(),
    email: $("tmEmail").value.trim() || null,
    phone: $("tmPhone").value.trim() || null,
    faculty_id: $("tmFaculty").value || null,
    semester: $("tmSemester").value || null,
    subject_id: $("tmSubject").value || null,
    time_slot_id: $("tmTimeSlot").value || null,
    status: $("tmStatus").value,
  };
  const pw = $("tmPassword").value.trim();
  if (pw) body.password = pw;
  if (!body.teacher_id || !body.full_name) {
    setErr("teacherModalErr", "Teacher ID and Full Name are required");
    return;
  }
  if (!id && !pw) {
    setErr("teacherModalErr", "Password is required for new teachers");
    return;
  }

  // ISSUE 3: Frontend guard — if faculty and semester set, subject must also be set
  if (body.faculty_id && body.semester && !body.subject_id) {
    setErr(
      "teacherModalErr",
      "Please select a subject for the chosen faculty and semester.",
    );
    return;
  }

  try {
    if (id)
      await api(`/teachers/${id}`, {
        method: "PUT",
        body: JSON.stringify(body),
      });
    else await api("/teachers", { method: "POST", body: JSON.stringify(body) });
    toast(id ? "Teacher updated" : "Teacher created");
    closeModal("teacherModal");
    loadTeachers();
  } catch (e) {
    // ISSUE 5: Show schedule conflict clearly
    if (e.message && e.message.includes("conflict")) {
      setErr("teacherModalErr", `⚠ Schedule conflict: ${e.message}`);
    } else {
      setErr("teacherModalErr", e.message);
    }
  }
}

/* ── ISSUE 4: Teacher Assignments Modal ──────────────────────────────────
   Admin can add/remove multiple subject assignments for one teacher.
   Each assignment = faculty + semester + subject + optional time slot.
   ──────────────────────────────────────────────────────────────────────── */
let _assignTeacherId = null;
let _assignTeacherName = "";

async function openAssignmentsModal(tid, name) {
  _assignTeacherId = tid;
  _assignTeacherName = name;

  // Dynamically inject the assignments modal if it doesn't exist
  if (!$("assignmentsModal")) {
    const div = document.createElement("div");
    div.className = "modal-overlay";
    div.id = "assignmentsModal";
    div.innerHTML = `
      <div class="modal-box" style="width:680px;max-width:95%;">
        <div class="modal-title" id="assignmentsModalTitle">Assignments</div>
        <div id="assignmentsList" style="margin:0.75rem 0;max-height:260px;overflow-y:auto;"></div>
        <div style="border-top:1px solid var(--border);padding-top:0.75rem;margin-top:0.5rem;">
          <div style="font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);margin-bottom:0.5rem;">Add New Assignment</div>
          <div class="modal-form-grid">
            <div class="form-group">
              <label>Faculty *</label>
              <select id="asgFaculty" onchange="loadAsgSemesters()"><option value="">Select faculty</option></select>
            </div>
            <div class="form-group">
              <label>Semester *</label>
              <select id="asgSemester" onchange="loadAsgSubjects()"><option value="">Select semester</option></select>
            </div>
            <div class="form-group">
              <label>Subject *</label>
              <select id="asgSubject"><option value="">Select subject</option></select>
            </div>
            <div class="form-group">
              <label>Time Slot</label>
              <select id="asgTimeSlot"><option value="">No time slot</option></select>
            </div>
          </div>
          <div id="asgErr" class="msg err"></div>
        </div>
        <div class="modal-footer">
          <button class="btn-secondary" onclick="closeModal('assignmentsModal')">Close</button>
          <button class="btn-primary" onclick="addAssignment()">+ Add Assignment</button>
        </div>
      </div>`;
    div.addEventListener("click", (e) => {
      if (e.target === div) div.classList.remove("open");
    });
    document.body.appendChild(div);
  }

  $("assignmentsModalTitle").textContent = `Assignments — ${name}`;
  setErr("asgErr", "");

  // Populate faculty and time slot dropdowns
  try {
    const [fData, tsData] = await Promise.all([
      api("/faculties"),
      api("/time-slots"),
    ]);
    $("asgFaculty").innerHTML =
      `<option value="">Select faculty</option>` +
      fData.faculties
        .map((f) => `<option value="${f.id}">${f.name}</option>`)
        .join("");
    $("asgTimeSlot").innerHTML =
      `<option value="">No time slot</option>` +
      tsData.time_slots
        .map((ts) => `<option value="${ts.id}">${ts.label}</option>`)
        .join("");
    $("asgSemester").innerHTML =
      `<option value="">Select faculty first</option>`;
    $("asgSubject").innerHTML =
      `<option value="">Select semester first</option>`;
  } catch {}

  await refreshAssignmentsList();
  openModal("assignmentsModal");
}

async function refreshAssignmentsList() {
  const container = $("assignmentsList");
  if (!container || !_assignTeacherId) return;
  try {
    const data = await api(`/teachers/${_assignTeacherId}/assignments`);
    const assignments = data.assignments || [];
    if (!assignments.length) {
      container.innerHTML = `<div style="color:var(--text3);font-size:13px;padding:1rem 0;">No assignments yet. Add one below.</div>`;
      return;
    }
    container.innerHTML = assignments
      .map(
        (a) => `
      <div style="display:flex;align-items:center;gap:0.75rem;padding:0.6rem 0.75rem;border-bottom:1px solid var(--border);flex-wrap:wrap;">
        ${a.is_primary ? `<span class="pill pill-blue" style="font-size:10px;">Primary</span>` : ""}
        <div style="flex:1;min-width:200px;">
          <div style="font-weight:600;font-size:13px;">${a.subject_name || "—"}</div>
          <div style="font-size:11px;color:var(--text3);font-family:var(--mono);">
            ${a.faculty_name || "—"} · Semester ${a.semester}${a.time_slot_label ? " · " + a.time_slot_label : ""}
          </div>
        </div>
        <button class="btn-secondary" style="font-size:11px;padding:2px 8px;color:var(--red);"
          onclick="removeAssignment(${a.id})">Remove</button>
      </div>`,
      )
      .join("");
  } catch (e) {
    container.innerHTML = `<div style="color:var(--red);font-size:13px;">${e.message}</div>`;
  }
}

async function loadAsgSemesters() {
  const fid = $("asgFaculty").value;
  $("asgSemester").innerHTML = `<option value="">Select semester</option>`;
  $("asgSubject").innerHTML = `<option value="">Select semester first</option>`;
  if (!fid) return;
  // All 8 semesters
  $("asgSemester").innerHTML =
    `<option value="">Select semester</option>` +
    Array.from(
      { length: 8 },
      (_, i) => `<option value="${i + 1}">Semester ${i + 1}</option>`,
    ).join("");
}

// ISSUE 3: loads subjects filtered by faculty + semester
async function loadAsgSubjects() {
  const fid = $("asgFaculty").value;
  const sem = $("asgSemester").value;
  $("asgSubject").innerHTML = `<option value="">Loading…</option>`;
  if (!fid || !sem) {
    $("asgSubject").innerHTML =
      `<option value="">Select faculty and semester first</option>`;
    return;
  }
  try {
    const data = await api(`/subjects?faculty_id=${fid}&semester=${sem}`);
    $("asgSubject").innerHTML = data.subjects.length
      ? `<option value="">Select subject</option>` +
        data.subjects
          .map((s) => `<option value="${s.id}">${s.name}</option>`)
          .join("")
      : `<option value="">No subjects in Semester ${sem}</option>`;
  } catch {
    $("asgSubject").innerHTML = `<option value="">Error loading</option>`;
  }
}

async function addAssignment() {
  const fid = $("asgFaculty").value;
  const sem = $("asgSemester").value;
  const sid = $("asgSubject").value;
  const tsid = $("asgTimeSlot").value || null;
  setErr("asgErr", "");
  if (!fid || !sem || !sid) {
    setErr("asgErr", "Faculty, semester, and subject are required.");
    return;
  }
  try {
    await api(`/teachers/${_assignTeacherId}/assignments`, {
      method: "POST",
      body: JSON.stringify({
        faculty_id: fid,
        semester: parseInt(sem),
        subject_id: sid,
        time_slot_id: tsid,
      }),
    });
    toast("Assignment added");
    // Reset selects
    $("asgFaculty").value = "";
    $("asgSemester").value = "";
    $("asgSubject").value = "";
    await refreshAssignmentsList();
    loadTeachers(); // refresh main table
  } catch (e) {
    // ISSUE 5: Surface schedule conflict clearly
    if (
      e.message &&
      (e.message.includes("conflict") ||
        e.message.includes("SCHEDULE_CONFLICT"))
    ) {
      setErr("asgErr", `⚠ Schedule conflict: ${e.message}`);
    } else {
      setErr("asgErr", e.message);
    }
  }
}

async function removeAssignment(aid) {
  if (!confirm("Remove this assignment?")) return;
  try {
    await api(`/teachers/${_assignTeacherId}/assignments/${aid}`, {
      method: "DELETE",
    });
    toast("Assignment removed");
    await refreshAssignmentsList();
    loadTeachers();
  } catch (e) {
    toast(e.message, "err");
  }
}

/* ── Teacher delete (unchanged logic, improved UX) ───────────────────────── */
let _currentTeacherRefsId = null;

async function deleteTeacher(id, name) {
  openTeacherRefsModal(id, name);
}

async function openTeacherRefsModal(id, name, errMsg) {
  _currentTeacherRefsId = id;
  $("teacherRefsTitle").textContent = `References for ${name}`;
  $("teacherRefsMsg").textContent = errMsg || "";
  $("teacherRefsBody").innerHTML =
    `<div style="padding:1rem;color:var(--text3);">Loading…</div>`;
  openModal("teacherRefsModal");
  try {
    const [attData, recData, tdata] = await Promise.all([
      api(`/teachers/${id}/references`),
      api(`/teachers/${id}/recognition-logs`),
      api("/teachers"),
    ]);
    const attCount = (attData.rows || []).length,
      recCount = (recData.rows || []).length;
    const totalCount = attCount + recCount;
    const countEl = $("teacherRefsCount");
    if (countEl)
      countEl.textContent = `${attCount} attendance, ${recCount} recognition records`;
    const sel = $("teacherReassignSelect");
    if (sel)
      sel.innerHTML =
        `<option value="">Select teacher</option>` +
        (tdata.teachers || [])
          .filter((t) => t.id !== id)
          .map((t) => `<option value="${t.id}">${t.full_name}</option>`)
          .join("");
    if (totalCount === 0) {
      $("teacherRefsBody").innerHTML =
        `<div style="padding:1rem;color:var(--text3);">No references found. You can safely delete this teacher.</div>`;
      const delBtn = $("btnConfirmDelete");
      if (delBtn) delBtn.disabled = false;
      return;
    }
    $("teacherRefsBody").innerHTML = (attData.rows || [])
      .map(
        (r) => `
      <div style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.03);">
        <div style="font-weight:600">${r.full_name} · <span style="font-family:var(--mono)">${r.student_id}</span></div>
        <div style="color:var(--text3);font-size:13px;">Date: ${r.date} · Status: ${r.status || "—"}</div>
      </div>`,
      )
      .join("");
    const delBtn = $("btnConfirmDelete");
    if (delBtn) delBtn.disabled = true;
  } catch (err) {
    $("teacherRefsBody").innerHTML =
      `<div style="padding:1rem;color:var(--red);">${err.message}</div>`;
  }
}

async function clearTeacherReferences() {
  const id = _currentTeacherRefsId;
  if (!id) return;
  $("btnClearRefs").disabled = true;
  try {
    const [r1, r2] = await Promise.all([
      api(`/teachers/${id}/clear-attendance`, { method: "POST" }),
      api(`/teachers/${id}/clear-recognition-logs`, { method: "POST" }),
    ]);
    const cleared = (r1.cleared || 0) + (r2.cleared || 0);
    toast(`${cleared} references cleared`);
    openTeacherRefsModal(
      id,
      $("teacherRefsTitle").textContent.replace("References for ", ""),
    );
    $("btnConfirmDelete").disabled = false;
    if ($("chkAutoDelete").checked) await deleteTeacherConfirmed();
  } catch (e) {
    setErr("teacherRefsMsg", e.message);
  } finally {
    $("btnClearRefs").disabled = false;
  }
}

async function reassignTeacherReferences() {
  const id = _currentTeacherRefsId;
  if (!id) return;
  const to = $("teacherReassignSelect")?.value;
  if (!to) {
    setErr("teacherRefsMsg", "Select a teacher to reassign to");
    return;
  }
  $("btnReassign").disabled = true;
  try {
    const [r1, r2] = await Promise.all([
      api(`/teachers/${id}/reassign-attendance`, {
        method: "POST",
        body: JSON.stringify({ to }),
      }),
      api(`/teachers/${id}/reassign-recognition-logs`, {
        method: "POST",
        body: JSON.stringify({ to }),
      }),
    ]);
    const moved = (r1.reassigned || 0) + (r2.reassigned || 0);
    toast(`${moved} references reassigned`);
    $("btnConfirmDelete").disabled = false;
    if ($("chkAutoDelete").checked) await deleteTeacherConfirmed();
  } catch (e) {
    setErr("teacherRefsMsg", e.message);
  } finally {
    $("btnReassign").disabled = false;
  }
}

async function deleteTeacherConfirmed() {
  const id = _currentTeacherRefsId;
  if (!id) return;
  $("btnConfirmDelete").disabled = true;
  try {
    await api(`/teachers/${id}`, { method: "DELETE" });
    toast("Teacher deleted");
    closeModal("teacherRefsModal");
    loadTeachers();
  } catch (e) {
    setErr("teacherRefsMsg", e.message);
    $("btnConfirmDelete").disabled = false;
  }
}

/* ═══════════════════════════════════════════════════════════════════════════
   ADMIN — MANAGE (Faculties, Subjects, Time Slots)
   ═══════════════════════════════════════════════════════════════════════════ */
async function loadManage() {
  await Promise.all([loadFaculties(), loadSubjects(), loadTimeslots()]);
  populateSubjectFacultyFilter();
}
function switchManageTab(tab, el) {
  document
    .querySelectorAll(".sub-tab")
    .forEach((t) => t.classList.remove("active"));
  document
    .querySelectorAll(".sub-tab-panel")
    .forEach((p) => p.classList.remove("active"));
  el.classList.add("active");
  $(`mtab-${tab}`).classList.add("active");
}
async function loadFaculties() {
  try {
    const data = await api("/faculties");
    faculties = data.faculties;
    $("facultyTableBody").innerHTML = faculties.length
      ? faculties
          .map(
            (f) => `
      <tr>
        <td style="font-weight:500;">${f.name}</td>
        <td style="font-family:var(--mono);font-size:12px;">${f.code || "—"}</td>
        <td><div class="action-btns">
          <button class="btn-secondary" style="font-size:11px;padding:3px 8px;" onclick="openFacultyModal(${f.id},'${f.name}','${f.code || ""}')">Edit</button>
          <button class="btn-secondary" style="font-size:11px;padding:3px 8px;color:var(--red);" onclick="deleteFaculty(${f.id},'${f.name}')">Del</button>
        </div></td>
      </tr>`,
          )
          .join("")
      : `<tr><td colspan="3" style="text-align:center;color:var(--text3);padding:1.5rem;">No faculties yet</td></tr>`;
  } catch (e) {
    $("facultyTableBody").innerHTML =
      `<tr><td colspan="3" style="color:var(--red);">${e.message}</td></tr>`;
  }
}

async function populateEnrollFaculty() {
  try {
    if (!faculties || faculties.length === 0) {
      const data = await api("/faculties");
      faculties = data.faculties || [];
    }
    const sel = $("eFaculty");
    if (!sel) return;
    sel.innerHTML =
      `<option value="">Select faculty</option>` +
      faculties
        .map((f) => `<option value="${f.id}">${f.name}</option>`)
        .join("");
  } catch (e) {
    // ignore silently
  }
}
async function populateSubjectFacultyFilter() {
  const sel = $("subjFacultyFilter"),
    semSel = $("subjSemesterFilter");
  if (sel)
    sel.innerHTML =
      `<option value="">All Faculties</option>` +
      faculties
        .map((f) => `<option value="${f.id}">${f.name}</option>`)
        .join("");
  if (semSel && semSel.options.length <= 1) {
    semSel.innerHTML =
      `<option value="">All Semesters</option>` +
      Array.from(
        { length: 8 },
        (_, i) => `<option value="${i + 1}">Semester ${i + 1}</option>`,
      ).join("");
  }
}
async function loadSubjects() {
  const fid = $("subjFacultyFilter")?.value || "",
    sem = $("subjSemesterFilter")?.value || "";
  try {
    const qs = new URLSearchParams();
    if (fid) qs.set("faculty_id", fid);
    if (sem) qs.set("semester", sem);
    const data = await api(
      `/subjects${qs.toString() ? "?" + qs.toString() : ""}`,
    );
    allSubjects = data.subjects;
    $("subjectTableBody").innerHTML = allSubjects.length
      ? allSubjects
          .map(
            (s) => `
      <tr>
        <td class="font-500">${s.name}</td>
        <td class="mono-cell">${s.code || "—"}</td>
        <td>${s.faculty_name || "—"}</td>
        <td class="mono-cell">Semester ${s.semester}</td>
        <td><div class="action-btns">
          <button class="btn-secondary btn-xs" onclick="openSubjectModalById(${s.id})">Edit</button>
          <button class="btn-secondary btn-xs text-red" onclick="deleteSubject(${s.id},'${s.name}')">Del</button>
        </div></td>
      </tr>`,
          )
          .join("")
      : `<tr><td colspan="5" style="text-align:center;color:var(--text3);padding:1.5rem;">No subjects yet</td></tr>`;
  } catch (e) {
    $("subjectTableBody").innerHTML =
      `<tr><td colspan="5" style="color:var(--red);">${e.message}</td></tr>`;
  }
}
async function loadTimeslots() {
  try {
    const data = await api("/time-slots");
    timeSlots = data.time_slots;
    $("timeslotTableBody").innerHTML = timeSlots.length
      ? timeSlots
          .map(
            (ts) => `
      <tr>
        <td style="font-weight:500;">${ts.label}</td>
        <td style="font-family:var(--mono);">${ts.start_time}</td>
        <td style="font-family:var(--mono);">${ts.end_time}</td>
        <td>${
          ts.assigned_teacher
            ? `<span class="pill pill-green" style="font-size:10px;">${ts.assigned_teacher} — ${ts.assigned_faculty} Sem ${ts.semester}</span>`
            : `<span style="color:var(--text3);font-size:11px;">Unassigned</span>`
        }
        </td>
        <td><button class="btn-secondary" style="font-size:11px;padding:3px 8px;color:var(--red);" onclick="deleteTimeSlot(${ts.id},'${ts.label}')">Del</button></td>
      </tr>`,
          )
          .join("")
      : `<tr><td colspan="5" style="text-align:center;color:var(--text3);padding:1.5rem;">No time slots</td></tr>`;
  } catch (e) {
    $("timeslotTableBody").innerHTML =
      `<tr><td colspan="5" style="color:var(--red);">${e.message}</td></tr>`;
  }
}
function openFacultyModal(id = "", name = "", code = "") {
  $("fmId").value = id;
  $("fmName").value = name;
  $("fmCode").value = code;
  $("facultyModalTitle").textContent = id ? "Edit Faculty" : "Add Faculty";
  setErr("facultyModalErr", "");
  openModal("facultyModal");
}
async function saveFaculty() {
  const id = $("fmId").value,
    body = { name: $("fmName").value.trim(), code: $("fmCode").value.trim() };
  if (!body.name) {
    setErr("facultyModalErr", "Name required");
    return;
  }
  try {
    if (id)
      await api(`/faculties/${id}`, {
        method: "PUT",
        body: JSON.stringify(body),
      });
    else
      await api("/faculties", { method: "POST", body: JSON.stringify(body) });
    toast(id ? "Faculty updated" : "Faculty created");
    closeModal("facultyModal");
    await loadFaculties();
    populateSubjectFacultyFilter();
  } catch (e) {
    setErr("facultyModalErr", e.message);
  }
}
async function deleteFaculty(id, name) {
  if (!confirm(`Delete faculty "${name}"?`)) return;
  try {
    await api(`/faculties/${id}`, { method: "DELETE" });
    toast("Faculty deleted");
    await loadFaculties();
    populateSubjectFacultyFilter();
    loadSubjects();
  } catch (e) {
    toast(e.message, "err");
  }
}
async function openSubjectModal(subject) {
  await loadFaculties();
  const selectedFaculty = $("subjFacultyFilter")?.value || "";
  const selectedSemester = $("subjSemesterFilter")?.value || "";
  const facultyId = subject?.faculty_id || selectedFaculty;
  const semester = subject?.semester || selectedSemester;
  if (!subject && (!facultyId || !semester)) {
    toast("Select a faculty and semester first", "err");
    return;
  }
  const facultyName =
    faculties.find((f) => String(f.id) === String(facultyId))?.name || "—";
  $("smId").value = subject?.id || "";
  $("smName").value = subject?.name || "";
  $("smCode").value = subject?.code || "";
  $("subjectContext").innerHTML =
    `<span>${facultyName}</span><span>Semester ${semester}</span>`;
  $("subjectContext").dataset.facultyId = facultyId;
  $("subjectContext").dataset.semester = semester;
  $("subjectModalTitle").textContent = subject ? "Edit Subject" : "Add Subject";
  setErr("subjectModalErr", "");
  openModal("subjectModal");
  setTimeout(() => $("smName")?.focus(), 0);
}
function openSubjectModalById(id) {
  const s = allSubjects.find((s) => String(s.id) === String(id));
  openSubjectModal(s || null);
}
async function saveSubject() {
  const context = $("subjectContext");
  const body = {
    name: $("smName").value.trim(),
    code: $("smCode").value.trim(),
    faculty_id: context?.dataset.facultyId || "",
    semester: context?.dataset.semester || "",
  };
  if (!body.name || !body.code) {
    setErr("subjectModalErr", "Subject name and ID required");
    return;
  }
  if (!body.faculty_id || !body.semester) {
    setErr("subjectModalErr", "Select a faculty and semester first");
    return;
  }
  try {
    const id = $("smId").value;
    await api(id ? `/subjects/${id}` : "/subjects", {
      method: id ? "PUT" : "POST",
      body: JSON.stringify(body),
    });
    toast(id ? "Subject updated" : "Subject created");
    closeModal("subjectModal");
    loadSubjects();
  } catch (e) {
    setErr("subjectModalErr", e.message);
  }
}
async function deleteSubject(id, name) {
  if (!confirm(`Delete subject "${name}"?`)) return;
  try {
    await api(`/subjects/${id}`, { method: "DELETE" });
    toast("Subject deleted");
    loadSubjects();
  } catch (e) {
    toast(e.message, "err");
  }
}
function openTimeSlotModal() {
  $("tsmLabel").value = "";
  $("tsmStart").value = "";
  $("tsmEnd").value = "";
  setErr("tsmErr", "");
  openModal("timeSlotModal");
}
async function saveTimeSlot() {
  const body = {
    label: $("tsmLabel").value.trim(),
    start_time: $("tsmStart").value,
    end_time: $("tsmEnd").value,
  };
  if (!body.label || !body.start_time || !body.end_time) {
    setErr("tsmErr", "All fields required");
    return;
  }
  try {
    await api("/time-slots", { method: "POST", body: JSON.stringify(body) });
    toast("Time slot created");
    closeModal("timeSlotModal");
    loadTimeslots();
  } catch (e) {
    setErr("tsmErr", e.message);
  }
}
async function deleteTimeSlot(id, label) {
  if (!confirm(`Delete time slot "${label}"?`)) return;
  try {
    await api(`/time-slots/${id}`, { method: "DELETE" });
    toast("Deleted");
    loadTimeslots();
  } catch (e) {
    toast(e.message, "err");
  }
}

/* ═══════════════════════════════════════════════════════════════════════════
   ADMIN — ATTENDANCE
   ═══════════════════════════════════════════════════════════════════════════ */
let attData = [];
async function loadAttendancePage() {
  const dateEl = $("attDate");
  if (!dateEl.value) dateEl.value = new Date().toISOString().slice(0, 10);
  const date = dateEl.value,
    dept = $("attDeptFilter").value;
  try {
    const [summ, depts] = await Promise.all([
      api(`/attendance/faculty-summary?date=${date}`),
      api("/departments"),
    ]);
    const df = $("attDeptFilter");
    if (df.options.length <= 1) {
      depts.departments.forEach((d) => {
        const o = document.createElement("option");
        o.value = o.textContent = d;
        df.appendChild(o);
      });
    }
    const facs = dept
      ? summ.faculties.filter((f) => f.name === dept)
      : summ.faculties;
    $("attFacultySummary").innerHTML = `<div class="metrics-grid">${facs
      .map(
        (f) => `
      <div class="metric-card">
        <div class="metric-label">${f.name}</div>
        <div style="display:flex;gap:0.5rem;align-items:baseline;margin-top:4px;">
          <span class="metric-val" style="color:var(--green);font-size:20px;">${f.present}</span>
          <span style="color:var(--text3);font-size:12px;">/ ${f.total} · ${f.rate}%</span>
        </div>
      </div>`,
      )
      .join("")}</div>`;
    const data = await api(
      `/attendance?date=${date}${dept ? "&department=" + encodeURIComponent(dept) : ""}`,
    );
    attData = data.records;
    renderAttTable(attData);
  } catch (e) {
    $("attTableWrap").innerHTML =
      `<div style="color:var(--red);">${e.message}</div>`;
  }
}
function filterAttendance() {
  const q = $("attSearch").value.toLowerCase();
  renderAttTable(
    q
      ? attData.filter(
          (r) =>
            r.name?.toLowerCase().includes(q) ||
            r.student_id?.toLowerCase().includes(q),
        )
      : attData,
  );
}
function renderAttTable(records) {
  if (!records.length) {
    $("attTableWrap").innerHTML =
      `<div style="text-align:center;color:var(--text3);padding:2rem;">No records for this date</div>`;
    return;
  }
  $("attTableWrap").innerHTML =
    `<div style="overflow-x:auto;"><table class="data-table"><thead><tr><th>Student</th><th>ID</th><th>Department</th><th>Time</th><th>Status</th></tr></thead>
    <tbody>${records.map((r) => `<tr><td style="font-weight:500;">${r.name}</td><td style="font-family:var(--mono);font-size:11px;">${r.student_id}</td><td>${r.department || "—"}</td><td style="font-family:var(--mono);font-size:12px;">${r.time}</td><td>${statusBadge(r.status)}</td></tr>`).join("")}</tbody></table></div>`;
}
function exportAttCSV() {
  const date = $("attDate").value,
    dept = $("attDeptFilter").value;
  window.open(
    `${API}/attendance/export?from=${date}&to=${date}${dept ? "&department=" + encodeURIComponent(dept) : ""}`,
  );
}

/* ═══════════════════════════════════════════════════════════════════════════
   ADMIN — REPORTS
   ═══════════════════════════════════════════════════════════════════════════ */
async function loadReports() {
  const today = new Date().toISOString().slice(0, 10);
  if (!$("repFrom").value) $("repFrom").value = today.slice(0, 7) + "-01";
  if (!$("repTo").value) $("repTo").value = today;
  const dept = $("repDept")?.value || "",
    deptQuery = dept ? `?department=${encodeURIComponent(dept)}` : "";
  try {
    const [stats, depts, hist] = await Promise.all([
      api(`/attendance/stats${deptQuery}`),
      api("/departments"),
      api(`/attendance/history${deptQuery}`),
    ]);
    const rd = $("repDept");
    if (rd.options.length <= 1) {
      depts.departments.forEach((d) => {
        const o = document.createElement("option");
        o.value = o.textContent = d;
        rd.appendChild(o);
      });
    }
    const total = stats.stats.length;
    const avgPct = total
      ? Math.round(
          stats.stats.reduce((a, s) => a + (parseFloat(s.pct) || 0), 0) / total,
        )
      : 0;
    const highest = total
      ? Math.max(...stats.stats.map((s) => parseFloat(s.pct) || 0))
      : 0;
    const below75 = stats.stats.filter(
      (s) => (parseFloat(s.pct) || 0) < 75,
    ).length;
    const totalRecog = stats.stats.reduce(
      (sum, s) => sum + (parseInt(s.present_days) || 0),
      0,
    );
    $("repAvgAtt").textContent = avgPct + "%";
    $("repHighest").textContent = highest + "%";
    $("repBelowCount").textContent = below75;
    $("repTotalRecog").textContent = totalRecog;
    drawMonthlyChart(hist.history.slice().reverse());
    drawDeptChart(stats.stats);
    $("statsTableWrap").innerHTML =
      `<div style="overflow-x:auto;"><table class="data-table">
      <thead><tr><th>ID</th><th>Name</th><th>Department</th><th>Present</th><th>Percentage</th><th>Status</th></tr></thead>
      <tbody>${
        stats.stats
          .map((s) => {
            const pctNum = parseFloat(s.pct) || 0,
              col = pctNum < 75 ? "var(--red)" : "var(--green)";
            return `<tr><td style="font-family:var(--mono);font-size:11px;color:var(--text3);">${s.student_id}</td>
          <td style="font-weight:500;">${s.full_name}</td><td>${s.department || "—"}</td>
          <td style="text-align:center;">${s.present_days}</td>
          <td><div style="display:flex;align-items:center;gap:8px;"><div style="flex:1;height:6px;background:var(--bg3);border-radius:3px;overflow:hidden;"><div style="height:100%;width:${Math.min(pctNum, 100)}%;background:${col};border-radius:3px;"></div></div><span style="color:${col};font-weight:600;font-family:var(--mono);font-size:12px;">${pctNum}%</span></div></td>
          <td><span style="color:${col};font-weight:600;font-size:11px;">${pctNum < 75 ? "Critical" : "On Track"}</span></td></tr>`;
          })
          .join("") ||
        `<tr><td colspan="6" style="text-align:center;color:var(--text3);padding:1.5rem;">No data</td></tr>`
      }</tbody></table></div>`;
  } catch (e) {
    $("repAvgAtt").textContent = "Error";
    toast(e.message, "err");
  }
}
function drawMonthlyChart(history) {
  const canvas = $("monthlyChart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (monthlyChart) monthlyChart.destroy();
  monthlyChart = new SimpleChart(ctx, {
    labels: history.map((h) => h.date),
    datasets: [
      {
        label: "Present",
        data: history.map((h) => h.present),
        color: "#22c55e",
      },
      { label: "Absent", data: history.map((h) => h.absent), color: "#ef4444" },
    ],
  });
}
function drawDeptChart(stats) {
  const canvas = $("deptChart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (deptChart) deptChart.destroy();
  const byDept = {};
  stats.forEach((s) => {
    const d = s.department || "Unassigned";
    byDept[d] = byDept[d] || { present: 0 };
    byDept[d].present += parseInt(s.present_days) || 0;
  });
  const labels = Object.keys(byDept).sort();
  deptChart = new SimpleChart(ctx, {
    type: "bar",
    labels,
    datasets: [
      {
        label: "Attendance",
        data: labels.map((d) => byDept[d].present),
        color: "#3b82f6",
      },
    ],
  });
}
function exportReport() {
  const f = $("repFrom").value,
    t = $("repTo").value,
    d = $("repDept").value;
  window.open(
    `${API}/attendance/export?from=${f}&to=${t}${d ? "&department=" + encodeURIComponent(d) : ""}`,
  );
}

/* ─────────────────────────────────────────────────────────────────────────────
   ISSUE 1 FIX: SimpleChart — always scales to container, never grows taller.
   Key changes vs original:
   1. Canvas height is driven by the HTML/CSS attribute, not computed from data.
   2. Y-axis max is rounded to a "nice" number so gridlines don't crowd.
   3. Bar width scales with the number of bars, so many bars = thin bars,
      never overflow the fixed container.
   ───────────────────────────────────────────────────────────────────────── */
class SimpleChart {
  constructor(ctx, config) {
    this.ctx = ctx;
    this.config = config;
    this.draw();
  }
  destroy() {
    this.ctx.setTransform?.(1, 0, 0, 1, 0, 0);
    this.ctx.clearRect(0, 0, this.ctx.canvas.width, this.ctx.canvas.height);
  }

  draw() {
    const { ctx, config } = this;
    const { type = "line", labels, datasets } = config;
    const dpr = window.devicePixelRatio || 1;

    // ── ISSUE 1: respect CSS max-height; do NOT let data expand the canvas ──
    const container = ctx.canvas.parentElement;
    const cssW = container ? container.clientWidth : 500;
    // Clamp height to 420 px — matches the CSS height:"420" attribute on the canvas
    const cssH = Math.min(ctx.canvas.getAttribute("height") || 420, 420);

    ctx.canvas.width = cssW * dpr;
    ctx.canvas.height = cssH * dpr;
    ctx.canvas.style.width = cssW + "px";
    ctx.canvas.style.height = cssH + "px";
    ctx.setTransform?.(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, cssW, cssH);

    if (!labels?.length) return;

    const pad = { top: 18, right: 16, bottom: 40, left: 46 };
    const cW = cssW - pad.left - pad.right;
    const cH = cssH - pad.top - pad.bottom;

    // ── Nice Y max so grid lines are round numbers ──
    const allVals = datasets.flatMap((d) => d.data).filter((v) => v != null);
    const rawMax = Math.max(...allVals, 1);
    const niceMax = this._niceMax(rawMax);
    const GRID_LINES = 4;

    // Grid lines + Y labels
    ctx.strokeStyle = "rgba(255,255,255,0.07)";
    ctx.lineWidth = 1;
    ctx.fillStyle = "#888895";
    ctx.font = `11px monospace`;
    ctx.textAlign = "right";
    for (let i = 0; i <= GRID_LINES; i++) {
      const val = Math.round((niceMax * i) / GRID_LINES);
      const y = pad.top + cH - (cH * i) / GRID_LINES;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(pad.left + cW, y);
      ctx.stroke();
      ctx.fillText(val, pad.left - 6, y + 4);
    }

    if (type === "bar") {
      const n = labels.length;
      const gW = cW / Math.max(n, 1); // group width per label
      const bCnt = datasets.length;
      const gap = 0; // NO GAP between bars for same day
      const inner = gW * 0.85; // 85% of group width for bars (more space used)
      const bW = Math.max(2, (inner - gap * Math.max(0, bCnt - 1)) / bCnt);

      datasets.forEach((ds, di) => {
        ds.data.forEach((v, i) => {
          if (v == null) return;
          const bH = (cH * v) / niceMax;
          const gX = pad.left + i * gW + (gW - inner) / 2;
          const bX = gX + di * bW; // NO gap between bars
          const bY = pad.top + cH - bH;
          ctx.fillStyle = ds.color;
          // Use crisp pixels for clear bars
          ctx.fillRect(
            Math.round(bX),
            Math.round(bY),
            Math.round(bW),
            Math.round(bH),
          );
          // Value label only if bar is wide enough
          if (bW > 16 && bH > 2) {
            ctx.fillStyle = "rgba(200,200,208,0.95)";
            ctx.font = "11px monospace";
            ctx.fontWeight = "bold";
            ctx.textAlign = "center";
            ctx.fillText(
              Math.round(v),
              Math.round(bX + bW / 2),
              Math.round(bY - 5),
            );
          }
        });
      });

      // X labels — skip every Nth if crowded
      ctx.fillStyle = "#888895";
      ctx.font = "10px monospace";
      ctx.textAlign = "center";
      const skipEvery = Math.ceil(n / Math.floor(cW / 32));
      labels.forEach((l, i) => {
        if (i % skipEvery !== 0 && i !== n - 1) return;
        ctx.fillText(
          String(l).slice(0, 8),
          pad.left + (i + 0.5) * gW,
          cssH - 10,
        );
      });
    } else {
      // Smooth line (Catmull-Rom)
      const step = cW / Math.max(labels.length - 1, 1);
      ctx.save();
      ctx.beginPath();
      ctx.rect(pad.left, pad.top, cW, cH);
      ctx.clip();

      datasets.forEach((ds) => {
        const pts = ds.data.map((v, i) => ({
          x: pad.left + i * step,
          y: pad.top + cH - (cH * (v || 0)) / niceMax,
        }));
        if (pts.length < 2) return;
        const segs = this._catmullSegs(pts);

        const buildPath = () => {
          ctx.beginPath();
          ctx.moveTo(pts[0].x, pts[0].y);
          segs.forEach(({ cp1x, cp1y, cp2x, cp2y, ex, ey }) =>
            ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, ex, ey),
          );
        };
        buildPath();
        ctx.lineTo(pts[pts.length - 1].x, pad.top + cH);
        ctx.lineTo(pts[0].x, pad.top + cH);
        ctx.closePath();
        const g = ctx.createLinearGradient(0, pad.top, 0, pad.top + cH);
        g.addColorStop(0, ds.color + "42");
        g.addColorStop(1, ds.color + "04");
        ctx.fillStyle = g;
        ctx.fill();
        buildPath();
        ctx.strokeStyle = ds.color;
        ctx.lineWidth = 2.5;
        ctx.lineJoin = "round";
        ctx.stroke();
      });
      ctx.restore();

      // Endpoint dots
      datasets.forEach((ds) => {
        const pts = ds.data.map((v, i) => ({
          x: pad.left + i * step,
          y: pad.top + cH - (cH * (v || 0)) / niceMax,
        }));
        if (pts.length < 2) return;
        [pts[0], pts[pts.length - 1]].forEach((p) => {
          ctx.beginPath();
          ctx.arc(p.x, p.y, 4, 0, Math.PI * 2);
          ctx.fillStyle = ds.color;
          ctx.fill();
          ctx.strokeStyle = "rgba(13,13,15,0.9)";
          ctx.lineWidth = 2;
          ctx.stroke();
        });
      });

      // X labels
      const skip = Math.ceil(labels.length / 6);
      ctx.fillStyle = "#888895";
      ctx.font = "10px monospace";
      ctx.textAlign = "center";
      labels.forEach((l, i) => {
        if (i % skip !== 0 && i !== labels.length - 1) return;
        ctx.fillText(
          String(l).slice(5),
          pad.left + i * (cW / (labels.length - 1 || 1)),
          cssH - 10,
        );
      });
    }
  }

  /** Round up to a "nice" max for Y axis */
  _niceMax(raw) {
    if (raw <= 0) return 10;
    const magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
    const normalized = raw / magnitude;
    let nice =
      normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
    return nice * magnitude;
  }

  _catmullSegs(pts) {
    const segs = [];
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[Math.max(i - 1, 0)],
        p1 = pts[i],
        p2 = pts[i + 1],
        p3 = pts[Math.min(i + 2, pts.length - 1)];
      segs.push({
        cp1x: p1.x + (p2.x - p0.x) / 6,
        cp1y: p1.y + (p2.y - p0.y) / 6,
        cp2x: p2.x - (p3.x - p1.x) / 6,
        cp2y: p2.y - (p3.y - p1.y) / 6,
        ex: p2.x,
        ey: p2.y,
      });
    }
    return segs;
  }
}

/* ═══════════════════════════════════════════════════════════════════════════
   ADMIN — SETTINGS
   ═══════════════════════════════════════════════════════════════════════════ */
async function loadSettings() {
  try {
    const s = await api("/settings");
    $("threshSlider").value = s.recognition_threshold;
    $("threshVal").textContent =
      Math.round(s.recognition_threshold * 100) + "%";
    $("skipSlider").value = s.frame_skip;
    $("skipVal").textContent = s.frame_skip;
    $("sysInfo").innerHTML = `
      <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border);">
        <span style="color:var(--text2);">Email</span><span>${s.email_enabled ? badge("Enabled", "green") : badge("Disabled", "red")}</span>
      </div>
      <div style="display:flex;justify-content:space-between;padding:6px 0;">
        <span style="color:var(--text2);">System Version</span><span style="font-family:var(--mono);font-size:11px;">v3.1</span>
      </div>`;
  } catch (e) {
    toast(e.message, "err");
  }
}
async function saveSettings() {
  try {
    await api("/settings", {
      method: "PUT",
      body: JSON.stringify({
        recognition_threshold: parseFloat($("threshSlider").value),
        frame_skip: parseInt($("skipSlider").value),
      }),
    });
    toast("Settings saved");
  } catch (e) {
    toast(e.message, "err");
  }
}
async function changeAdminPw() {
  const old_pw = $("oldPw").value,
    new_pw = $("newPw").value;
  if (!old_pw || !new_pw) {
    toast("Both fields required", "err");
    return;
  }
  try {
    await api("/auth/change-password", {
      method: "POST",
      body: JSON.stringify({ old_password: old_pw, new_password: new_pw }),
    });
    toast("Password updated");
    $("oldPw").value = "";
    $("newPw").value = "";
  } catch (e) {
    toast(e.message, "err");
  }
}
async function sendTestEmail() {
  const addr = $("testEmailAddr")?.value.trim();
  if (!addr) {
    toast("Enter an email address", "err");
    return;
  }
  try {
    await api("/email/test", {
      method: "POST",
      body: JSON.stringify({ email: addr }),
    });
    toast("Test email queued");
  } catch (e) {
    toast(e.message, "err");
  }
}

/* ═══════════════════════════════════════════════════════════════════════════
   ENROLL — Automatic Face Capture (unchanged from v3.0)
   ═══════════════════════════════════════════════════════════════════════════ */
function showEnrollStep(n) {
  [1, 2, 3].forEach((i) => {
    $(`epanel-${i}`).style.display = i === n ? "block" : "none";
    $(`estep-${i}`).classList.toggle("active", i === n);
    $(`estep-${i}`).classList.toggle("done", i < n);
  });
  enrollStep = n;
}
function goToStep1() {
  stopAutoCapture();
  showEnrollStep(1);
}
function goToStep2() {
  const sid = $("eId").value.trim(),
    name = $("eName").value.trim();
  if (!sid || !name) {
    toast("Student ID and name are required", "err");
    return;
  }
  enrollFrames = [];
  poseCaptureCounts = { front: 0, left: 0, right: 0, up: 0, down: 0 };
  isAutoCaptureActive = false;
  _captureComplete = false;
  _validateInFlight = false;
  renderCaptureProgress();
  showEnrollStep(2);
}
function goToStep3() {
  if (enrollFrames.length < TOTAL_FRAMES) {
    toast(
      `Capture at least ${TOTAL_FRAMES} frames (${enrollFrames.length}/${TOTAL_FRAMES})`,
      "err",
    );
    return;
  }
  stopAutoCapture();
  buildReviewPanel();
  showEnrollStep(3);
}
async function startCamera() {
  return new Promise((resolve, reject) => {
    if (enrollStream) enrollStream.getTracks().forEach((t) => t.stop());
    navigator.mediaDevices
      .getUserMedia({
        video: {
          facingMode: "user",
          width: { ideal: 640 },
          height: { ideal: 480 },
        },
        audio: false,
      })
      .then((stream) => {
        enrollStream = stream;
        const vid = $("captureCam");
        vid.srcObject = enrollStream;
        vid.onloadedmetadata = () => {
          hide($("capturePlaceholder"));
          resolve();
        };
      })
      .catch((e) => {
        toast("Camera error: " + e.message, "err");
        reject(e);
      });
  });
}
function stopAutoCapture() {
  isAutoCaptureActive = false;
  if (captureAF) {
    cancelAnimationFrame(captureAF);
    captureAF = null;
  }
  if (enrollStream) {
    enrollStream.getTracks().forEach((t) => t.stop());
    enrollStream = null;
  }
  const vid = $("captureCam");
  if (vid) vid.srcObject = null;
  hide($("stopCaptureBtn"));
  hide($("resetCaptureBtn"));
  show($("startCaptureBtn"));
  $("startCaptureBtn").disabled = false;
}
function resetCapture() {
  stopAutoCapture();
  enrollFrames = [];
  poseCaptureCounts = { front: 0, left: 0, right: 0, up: 0, down: 0 };
  _captureComplete = false;
  _validateInFlight = false;
  $("captureCount").textContent = "0";
  $("captureInstruction").textContent =
    "Click Start Capture to begin guided face collection";
  $("poseLabel").textContent = "—";
  $("qualityVal").textContent = "—";
  $("qualityFill").style.width = "0%";
  $("startCaptureBtn").style.display = "inline-block";
  $("stopCaptureBtn").style.display = "none";
  $("resetCaptureBtn").style.display = "none";
  renderCaptureProgress();
  updatePosePips();
}
function updatePosePips() {
  for (const step of POSE_SEQUENCE) {
    const pip = $(`pip-${step.pose}`);
    if (!pip) continue;
    const count = poseCaptureCounts[step.pose] || 0,
      isDone = count >= step.target,
      isCurrent = _getCurrentPoseStep()?.pose === step.pose;
    pip.classList.toggle("done", isDone);
    pip.classList.toggle("active", isCurrent && !isDone);
  }
}
async function startAutoCapture() {
  if (!enrollStream) {
    try {
      await startCamera();
    } catch (e) {
      toast("Failed to start camera: " + e.message, "err");
      return;
    }
  }
  $("startCaptureBtn").style.display = "none";
  $("stopCaptureBtn").style.display = "inline-block";
  $("resetCaptureBtn").style.display = "inline-block";
  enrollFrames = [];
  poseCaptureCounts = { front: 0, left: 0, right: 0, up: 0, down: 0 };
  isAutoCaptureActive = true;
  _captureComplete = false;
  _validateInFlight = false;
  renderCaptureProgress();
  updatePosePips();
  const firstStep = _getCurrentPoseStep();
  if (firstStep) {
    _speakInstruction(
      "Face capture started. " + firstStep.voiceInstruction,
      true,
    );
    $("poseLabel").textContent =
      firstStep.pose.charAt(0).toUpperCase() + firstStep.pose.slice(1);
    $("captureInstruction").textContent = firstStep.instruction;
  }
  _runCaptureLoop();
}
function _runCaptureLoop() {
  const video = $("captureCam"),
    overlay = $("captureOverlay"),
    ctx = overlay.getContext("2d");
  function loop(ts) {
    if (!isAutoCaptureActive || !enrollStream) return;
    overlay.width = video.videoWidth || 640;
    overlay.height = video.videoHeight || 480;
    _drawOverlay(ctx, overlay.width, overlay.height);
    if (ts - lastValidateTime >= 400) {
      lastValidateTime = ts;
      _validateAndCapture(video, overlay.width, overlay.height);
    }
    captureAF = requestAnimationFrame(loop);
  }
  captureAF = requestAnimationFrame(loop);
}
async function _validateAndCapture(video, w, h) {
  if (_validateInFlight || _captureComplete || !isAutoCaptureActive) return;
  _validateInFlight = true;
  try {
    const tmp = document.createElement("canvas");
    tmp.width = w;
    tmp.height = h;
    tmp.getContext("2d").drawImage(video, 0, 0, w, h);
    const b64 = tmp.toDataURL("image/jpeg", 0.85).split(",")[1];
    const d = await api("/capture/validate-frame", {
      method: "POST",
      body: JSON.stringify({ image: b64 }),
    });
    if (!d || _captureComplete || !isAutoCaptureActive) return;
    currentQuality = d.quality;
    currentPoseHint = String(d.pose || "front")
      .trim()
      .toLowerCase();
    const q = d.quality?.overall ?? 0;
    const qFill = $("qualityFill");
    if (qFill) {
      qFill.style.width = q + "%";
      qFill.style.background =
        q >= 70 ? "var(--green)" : q >= 45 ? "var(--amber)" : "var(--red)";
    }
    const qualityValEl = $("qualityVal");
    if (qualityValEl) qualityValEl.textContent = q + "%";
    const poseLabelEl = $("poseLabel");
    if (poseLabelEl)
      poseLabelEl.textContent = d.face_detected
        ? currentPoseHint.charAt(0).toUpperCase() + currentPoseHint.slice(1)
        : "No face";
    if (!d.face_detected || !d.quality?.passed) {
      $("captureInstruction").textContent = !d.face_detected
        ? "⚠ No face detected — move into frame"
        : "⚠ Quality too low — adjust position";
      return;
    }
    if (_captureComplete || !isAutoCaptureActive) return;
    const currentStep = _getCurrentPoseStep();
    if (!currentStep) {
      if (!_captureComplete) _onCaptureComplete();
      return;
    }
    const requiredPose = currentStep.pose,
      poseMatches = currentPoseHint === requiredPose;
    if (poseMatches) {
      const alreadyCount = poseCaptureCounts[requiredPose] || 0;
      if (alreadyCount >= currentStep.target) return;
      enrollFrames.push(b64);
      poseCaptureCounts[requiredPose] = alreadyCount + 1;
      const total = Object.values(poseCaptureCounts).reduce((a, b) => a + b, 0);
      const countEl = $("captureCount");
      if (countEl) countEl.textContent = total;
      $("captureInstruction").textContent =
        `✓ ${total} frames — ${currentStep.instruction}`;
      _soundTick();
      renderCaptureProgress();
      updatePosePips();
      if (poseCaptureCounts[requiredPose] >= currentStep.target) {
        if (total >= TOTAL_FRAMES) {
          _captureComplete = true;
          _onCaptureComplete();
        } else {
          const nextStep = _getCurrentPoseStep();
          if (nextStep) {
            _soundDing();
            setTimeout(
              () =>
                _speakInstruction(
                  nextStep.voiceInstruction || nextStep.instruction,
                  true,
                ),
              300,
            );
            $("captureInstruction").textContent =
              `✓ ${requiredPose.toUpperCase()} done! Now: ${nextStep.instruction}`;
            $("poseLabel").textContent =
              nextStep.pose.charAt(0).toUpperCase() + nextStep.pose.slice(1);
          }
        }
      }
    } else {
      $("captureInstruction").textContent =
        currentStep.instruction +
        (currentPoseHint && currentPoseHint !== requiredPose
          ? ` (seeing: ${currentPoseHint})`
          : "");
    }
  } catch {
  } finally {
    _validateInFlight = false;
  }
}
function _getCurrentPoseStep() {
  for (const step of POSE_SEQUENCE) {
    if ((poseCaptureCounts[step.pose] || 0) < step.target) return step;
  }
  return null;
}
function _onCaptureComplete() {
  stopAutoCapture();
  _soundChime();
  setTimeout(
    () =>
      _speakInstruction(
        "Face capture complete! You may now proceed to enroll.",
        true,
      ),
    400,
  );
  $("captureInstruction").textContent =
    `✅ ${enrollFrames.length} frames collected! Click "Review & Enroll" to continue.`;
  $("resetCaptureBtn").style.display = "inline-block";
  toast(`${enrollFrames.length} frames captured`);
}
function _drawOverlay(ctx, w, h) {
  ctx.clearRect(0, 0, w, h);
  const cx = w / 2,
    cy = h / 2,
    rx = w * 0.18,
    ry = h * 0.26;
  const currentStep = _getCurrentPoseStep(),
    totalCaptured = Object.values(poseCaptureCounts).reduce((a, b) => a + b, 0),
    pct = totalCaptured / TOTAL_FRAMES;
  ctx.save();
  ctx.fillStyle = "rgba(0,0,0,0.40)";
  ctx.fillRect(0, 0, w, h);
  ctx.globalCompositeOperation = "destination-out";
  ctx.beginPath();
  ctx.ellipse(cx, cy, rx + 8, ry + 8, 0, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
  const ovalColor = !currentPoseHint
    ? "#555"
    : currentQuality?.passed
      ? `hsl(${120 * pct},80%,55%)`
      : "#EF4444";
  ctx.beginPath();
  ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
  ctx.strokeStyle = ovalColor;
  ctx.lineWidth = 3;
  ctx.stroke();
  if (totalCaptured > 0) {
    ctx.beginPath();
    ctx.ellipse(cx, cy, rx + 6, ry + 6, -Math.PI / 2, 0, Math.PI * 2 * pct);
    ctx.strokeStyle = "rgba(34,197,94,0.8)";
    ctx.lineWidth = 4;
    ctx.stroke();
  }
}
function renderCaptureProgress() {
  const container = $("poseProgress");
  if (!container) return;
  container.innerHTML = POSE_SEQUENCE.map((step) => {
    const pose = step.pose,
      label = pose.charAt(0).toUpperCase() + pose.slice(1),
      count = poseCaptureCounts[pose] || 0,
      isDone = count >= step.target,
      isActive = _getCurrentPoseStep()?.pose === pose && isAutoCaptureActive,
      pct = Math.round((count / step.target) * 100);
    return `<div style="margin-bottom:0.75rem;">
      <div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:4px;">
        <div style="width:20px;height:20px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;background:${isDone ? "var(--green)" : isActive ? "var(--blue)" : "var(--bg4)"};color:${isDone || isActive ? "#fff" : "var(--text3)"};">${isDone ? "✓" : POSE_SEQUENCE.indexOf(step) + 1}</div>
        <span style="font-size:13px;font-weight:600;color:${isDone || isActive ? "var(--text)" : "var(--text3)"};">${label}</span>
        <span style="font-size:11px;color:var(--text3);margin-left:auto;">${count}/${step.target}</span>
      </div>
      <div style="width:100%;height:4px;background:var(--bg3);border-radius:2px;overflow:hidden;">
        <div style="width:${pct}%;height:100%;background:${isDone ? "var(--green)" : isActive ? "var(--blue)" : "var(--text3)"};transition:width 0.2s ease;"></div>
      </div>
    </div>`;
  }).join("");
}
function buildReviewPanel() {
  const sid = $("eId").value.trim(),
    name = $("eName").value.trim(),
    faculty = $("eFaculty")?.options[$("eFaculty")?.selectedIndex]?.text || "",
    sem = $("eSem")?.value.trim() || "",
    email = $("eEmail").value.trim();
  $("reviewGrid").innerHTML = `
    <div><span style="color:var(--text3);font-size:12px;">Student ID</span><div style="font-family:var(--mono);font-weight:600;">${sid}</div></div>
    <div><span style="color:var(--text3);font-size:12px;">Full Name</span><div style="font-weight:600;">${name}</div></div>
    <div><span style="color:var(--text3);font-size:12px;">Faculty</span><div>${faculty || "—"}</div></div>
    <div><span style="color:var(--text3);font-size:12px;">Semester</span><div>${sem || "—"}</div></div>
    <div><span style="color:var(--text3);font-size:12px;">Email</span><div>${email || "—"}</div></div>
    <div><span style="color:var(--text3);font-size:12px;">Face Samples</span><div style="color:var(--green);font-weight:600;">${enrollFrames.length} frames ✓</div></div>`;
  const badge2 = $("reviewCaptureBadge");
  badge2.textContent = `${enrollFrames.length}/${TOTAL_FRAMES} frames captured`;
  badge2.style.color =
    enrollFrames.length >= TOTAL_FRAMES ? "var(--green)" : "var(--amber)";
}
async function enrollStudent() {
  if (enrollFrames.length < TOTAL_FRAMES) {
    toast(`Need at least ${TOTAL_FRAMES} frames`, "err");
    return;
  }
  const body = {
    student_id: $("eId").value.trim(),
    full_name: $("eName").value.trim(),
    faculty_id: $("eFaculty")?.value || null,
    semester: $("eSem")?.value.trim() || null,
    email: $("eEmail").value.trim() || null,
    phone: $("ePhone").value.trim() || null,
    frames: enrollFrames,
  };
  $("enrollBtn").disabled = true;
  $("enrollProgress").style.display = "block";
  $("enrollMsg").textContent = "Processing enrollment…";
  $("enrollMsg").className = "msg";
  try {
    const res = await api("/enroll", {
      method: "POST",
      body: JSON.stringify(body),
    });
    $("enrollMsg").textContent = res.is_update
      ? "Student re-enrolled successfully!"
      : "Student enrolled successfully!";
    $("enrollMsg").className = "msg ok";
    toast(res.is_update ? "Student re-enrolled!" : "Student enrolled!");
    setTimeout(() => {
      resetEnroll();
      navigate("students");
    }, 2000);
  } catch (e) {
    $("enrollMsg").textContent = e.message;
    $("enrollMsg").className = "msg err";
    $("enrollBtn").disabled = false;
  }
}
function resetEnroll() {
  enrollFrames = [];
  poseCaptureCounts = { front: 0, left: 0, right: 0, up: 0, down: 0 };
  isAutoCaptureActive = false;
  _captureComplete = false;
  _validateInFlight = false;
  if (captureAF) {
    cancelAnimationFrame(captureAF);
    captureAF = null;
  }
  ["eId", "eName", "eEmail", "ePhone", "eFaculty"].forEach((id) => {
    if ($(id)) $(id).value = "";
  });
  if ($("eSem")) $("eSem").value = "";
  if (enrollStream) {
    enrollStream.getTracks().forEach((t) => t.stop());
    enrollStream = null;
  }
  $("captureInstruction").textContent =
    "Click Start Capture to begin guided face collection";
  $("poseLabel").textContent = "—";
  $("qualityVal").textContent = "—";
  $("qualityFill").style.width = "0%";
  $("captureCount").textContent = "0";
  $("startCaptureBtn").style.display = "inline-block";
  $("stopCaptureBtn").style.display = "none";
  $("resetCaptureBtn").style.display = "none";
  $("capturePlaceholder").style.display = "flex";
  $("enrollMsg").textContent = "";
  $("enrollMsg").className = "msg";
  $("enrollProgress").style.display = "none";
  $("enrollBtn").disabled = false;
  renderCaptureProgress();
  updatePosePips();
  showEnrollStep(1);
}

/* ═══════════════════════════════════════════════════════════════════════════
   TEACHER — DASHBOARD
   ═══════════════════════════════════════════════════════════════════════════ */
async function loadTeacherDashboard() {
  if (!userInfo) return;
  renderTeacherClassBar("teacherClassBar");
  const today = new Date().toISOString().slice(0, 10);
  try {
    const att = await api(`/teacher/attendance?date=${today}`);
    const rate = att.records?.length
      ? Math.round((att.present / att.records.length) * 100)
      : 0;
    $("tDashMetrics").innerHTML = `
      <div class="metric-card"><div class="metric-label">Your Students</div><div class="metric-val">${att.records?.length || 0}</div></div>
      <div class="metric-card"><div class="metric-label">Present Today</div><div class="metric-val" style="color:var(--green)">${att.present}</div></div>
      <div class="metric-card"><div class="metric-label">Absent Today</div><div class="metric-val" style="color:var(--red)">${att.absent}</div></div>
      <div class="metric-card"><div class="metric-label">Rate</div><div class="metric-val" style="color:var(--blue)">${rate ? rate + "%" : "—"}</div></div>`;
    $("tDashTable").innerHTML = `<div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.75rem;">
        <div class="section-title">Today · ${today}</div>
        <button class="btn-primary btn-sm" onclick="quickStartSession()">▶ Start Attendance Session</button>
      </div>
      <div style="overflow-x:auto;"><table class="data-table"><thead><tr><th>Student</th><th>ID</th><th>Time</th><th>Status</th></tr></thead>
      <tbody>${(att.records || []).map((r) => `<tr><td style="font-weight:500;">${r.full_name}</td><td style="font-family:var(--mono);font-size:11px;color:var(--text3);">${r.student_id}</td><td style="font-family:var(--mono);font-size:12px;">${r.time || "—"}</td><td>${statusBadge(r.status)}</td></tr>`).join() || `<tr><td colspan="4" style="text-align:center;color:var(--text3);padding:1.5rem;">No students in your class yet</td></tr>`}
      </tbody></table></div>
    </div>`;
  } catch (e) {
    $("tDashMetrics").innerHTML =
      `<div style="color:var(--red);">${e.message}</div>`;
  }
}

function renderTeacherClassBar(targetId) {
  const el = $(targetId);
  if (!el || !userInfo) return;
  el.style.display = "";
  el.innerHTML = `
    <div class="class-info-item"><div class="class-info-label">Faculty</div><div class="class-info-value">${userInfo.faculty || "—"}</div></div>
    <div class="class-info-item"><div class="class-info-label">Semester</div><div class="class-info-value">${userInfo.semester || "—"}</div></div>
    <div class="class-info-item"><div class="class-info-label">Subject</div><div class="class-info-value">${userInfo.subject || "—"}</div></div>
    <div class="class-info-item"><div class="class-info-label">Time Slot</div><div class="class-info-value">${userInfo.time_slot || "—"}</div></div>
    <div class="class-info-item"><div class="class-info-label">Teacher</div><div class="class-info-value">${userInfo.full_name || "—"}</div></div>`;
}
async function quickStartSession() {
  navigate("t-recognize");
  setTimeout(async () => {
    await startTeacherWebcam();
    startTeacherAuto();
    toast("Attendance session started!");
  }, 300);
}

/* ═══════════════════════════════════════════════════════════════════════════
   TEACHER — FACE RECOGNITION
   ═══════════════════════════════════════════════════════════════════════════ */
function initTeacherRecognize() {
  renderTeacherClassBar("teacherClassBar2");
  const log = $("tSessionLog");
  if (log && !log.innerHTML.trim())
    log.innerHTML = `<div style="color:var(--text3);font-size:12px;text-align:center;padding:1rem;">Session log will appear here</div>`;
  updateSessionUI();
}
function updateSessionUI() {
  const overlay = $("tRecogOverlay"),
    stopBtn = $("btnTRecogStop"),
    autoBtn = $("btnTRecogAuto"),
    bar = $("sessionStatusBar");
  if (sessionActive) {
    if (overlay) overlay.style.display = "none";
    if (stopBtn) stopBtn.style.display = "";
    if (autoBtn) {
      autoBtn.textContent = "⏸ Pause Auto";
      autoBtn.style.background = "var(--amber)";
      autoBtn.style.color = "#000";
    }
    if (bar) {
      bar.style.display = "";
      bar.innerHTML = `<span style="color:var(--green);font-weight:600;">● Auto-Recognition Active</span><span style="color:var(--text2);margin-left:0.5rem;">Scanning every 2 seconds</span>`;
    }
  } else {
    if (stopBtn) stopBtn.style.display = "none";
    if (autoBtn) {
      autoBtn.textContent = "▶ Start Auto";
      autoBtn.style.background = "";
      autoBtn.style.color = "";
    }
    if (bar) bar.style.display = "none";
  }
}
async function startTeacherWebcam() {
  try {
    teacherRecogStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user", width: 640, height: 480 },
      audio: false,
    });
    $("tRecogVideo").srcObject = teacherRecogStream;
    const overlay = $("tRecogOverlay");
    if (overlay) overlay.style.display = "none";
    const stopBtn = $("btnTRecogStop");
    if (stopBtn) stopBtn.style.display = "";
    const floatBtn = $("tRecogStopFloat");
    if (floatBtn) floatBtn.classList.remove("hidden");
  } catch (e) {
    toast("Camera error: " + e.message, "err");
  }
}
function stopTeacherWebcam() {
  stopTeacherAuto();
  if (teacherRecogStream) {
    teacherRecogStream.getTracks().forEach((t) => t.stop());
    teacherRecogStream = null;
  }
  const overlay = $("tRecogOverlay");
  if (overlay) overlay.style.display = "";
  const stopBtn = $("btnTRecogStop");
  if (stopBtn) stopBtn.style.display = "none";
  const floatBtn = $("tRecogStopFloat");
  if (floatBtn) floatBtn.classList.add("hidden");
}
function startTeacherAuto() {
  if (teacherAutoRunning) return;
  if (!teacherRecogStream) {
    toast("Start camera first", "err");
    return;
  }
  teacherAutoRunning = true;
  sessionActive = true;
  updateSessionUI();
  teacherAutoLoop = setInterval(runTeacherRecognitionFrame, 2000);
  toast("Auto-recognition started");
}
function stopTeacherAuto() {
  teacherAutoRunning = false;
  sessionActive = false;
  clearInterval(teacherAutoLoop);
  teacherAutoLoop = null;
  updateSessionUI();
}
function toggleTeacherAuto() {
  if (teacherAutoRunning) stopTeacherAuto();
  else startTeacherAuto();
}
async function runTeacherRecognitionFrame() {
  if (!teacherRecogStream || !teacherAutoRunning) return;
  const vid = $("tRecogVideo");
  if (!vid || !vid.videoWidth) return;
  const can = $("tRecogCanvas");
  can.width = vid.videoWidth;
  can.height = vid.videoHeight;
  can.getContext("2d").drawImage(vid, 0, 0);
  const b64 = can.toDataURL("image/jpeg", 0.8).split(",")[1];
  try {
    const r = await api("/teacher/recognize", {
      method: "POST",
      body: JSON.stringify({ image: b64 }),
    });
    const resultEl = $("tRecogResult");
    if (r.recognized) {
      if (resultEl)
        resultEl.innerHTML = `<div class="msg ok" style="font-size:12px;">✓ ${r.name} — ${r.confidence}%${r.attendance_marked ? " · <strong>Marked Present</strong>" : " · (already marked)"}</div>`;
      if (r.attendance_marked) {
        appendSessionLog(r);
        refreshSessionCount();
      }
    } else {
      if (resultEl)
        resultEl.innerHTML = `<div style="font-size:12px;color:var(--text3);">Scanning… (${r.confidence}%)</div>`;
    }
  } catch {}
}
async function doTeacherRecognize() {
  const vid = $("tRecogVideo"),
    can = $("tRecogCanvas");
  can.width = vid.videoWidth || 640;
  can.height = vid.videoHeight || 480;
  can.getContext("2d").drawImage(vid, 0, 0);
  const b64 = can.toDataURL("image/jpeg", 0.85).split(",")[1];
  const resultEl = $("tRecogResult");
  if (resultEl)
    resultEl.innerHTML = `<div class="msg" style="color:var(--text2);">Recognizing…</div>`;
  try {
    const r = await api("/teacher/recognize", {
      method: "POST",
      body: JSON.stringify({ image: b64 }),
    });
    if (r.recognized) {
      if (resultEl)
        resultEl.innerHTML = `<div class="msg ok">✓ ${r.name} (${r.student_id}) — ${r.confidence}%${r.attendance_marked ? " · Marked Present" : " · Already marked"}</div>`;
      if (r.attendance_marked) {
        appendSessionLog(r);
        refreshSessionCount();
      }
    } else {
      if (resultEl)
        resultEl.innerHTML = `<div class="msg err">Unknown face — ${r.confidence}%</div>`;
    }
  } catch (e) {
    if (resultEl)
      resultEl.innerHTML = `<div class="msg err">${e.message}</div>`;
  }
}
function appendSessionLog(r) {
  const log = $("tSessionLog");
  if (!log) return;
  const ph = log.querySelector(".session-placeholder");
  if (ph) ph.remove();
  const div = document.createElement("div");
  div.className = "log-entry";
  div.innerHTML = `<div style="display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap;"><span class="pill pill-green">✓</span><span style="font-weight:600;">${r.name}</span><span style="color:var(--text3);font-size:11px;font-family:var(--mono);">${r.student_id}</span><span style="color:var(--text3);font-size:11px;margin-left:auto;font-family:var(--mono);">${new Date().toLocaleTimeString()}</span></div>`;
  log.prepend(div);
  if (log.children.length > 50) log.lastElementChild.remove();
}
let _sessionCount = 0;
function refreshSessionCount() {
  _sessionCount++;
  const el = $("sessionMarkedCount"),
    label = $("sessionMarkedLabel"),
    pill = $("sessionCountPill");
  if (el) el.textContent = _sessionCount;
  if (label)
    label.textContent =
      _sessionCount === 1 ? "student marked" : "students marked";
  if (pill) {
    pill.textContent = `${_sessionCount} marked`;
    pill.className = "pill pill-green";
  }
}
async function refreshSessionLog() {
  if (currentPage === "t-recognize") refreshSessionCount();
}

/* ═══════════════════════════════════════════════════════════════════════════
   TEACHER — MANUAL ATTENDANCE
   ═══════════════════════════════════════════════════════════════════════════ */
async function loadManualAttendance() {
  renderTeacherClassBar("teacherClassBar3");
  const dateEl = $("tManualDate");
  if (!dateEl.value) dateEl.value = new Date().toISOString().slice(0, 10);
  const date = dateEl.value;
  try {
    const data = await api(`/teacher/attendance?date=${date}`);
    manualAttMap = {};
    data.records.forEach((r) => {
      manualAttMap[r.student_id] = r.status || "Absent";
    });
    if (!data.records.length) {
      $("manualAttGrid").innerHTML =
        `<div style="text-align:center;color:var(--text3);padding:2rem;">No students in your class yet</div>`;
      return;
    }
    $("manualAttGrid").innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.75rem;flex-wrap:wrap;gap:0.5rem;">
        <div style="font-size:13px;color:var(--text2);">${data.records.length} students · ${date}</div>
        <div style="display:flex;gap:0.5rem;">
          <button class="btn-secondary btn-sm" onclick="markAllPresent()">Mark All Present</button>
          <button class="btn-secondary btn-sm" onclick="markAllAbsent()">Mark All Absent</button>
        </div>
      </div>
      <div class="manual-att-grid" id="manualRows">
        ${data.records
          .map(
            (r) => `
          <div class="manual-att-row" id="row-${r.student_id}">
            <div><div class="manual-att-name">${r.full_name}</div><div class="manual-att-id">${r.student_id}</div></div>
            <button class="toggle-btn ${manualAttMap[r.student_id] === "Present" ? "present" : "absent"}" id="togbtn-${r.student_id}" onclick="toggleManualStatus('${r.student_id}')">${manualAttMap[r.student_id] || "Absent"}</button>
          </div>`,
          )
          .join("")}
      </div>`;
  } catch (e) {
    $("manualAttGrid").innerHTML =
      `<div style="color:var(--red);">${e.message}</div>`;
  }
}
function toggleManualStatus(sid) {
  manualAttMap[sid] = manualAttMap[sid] === "Present" ? "Absent" : "Present";
  const btn = $(`togbtn-${sid}`);
  btn.textContent = manualAttMap[sid];
  btn.className = `toggle-btn ${manualAttMap[sid] === "Present" ? "present" : "absent"}`;
}
function markAllPresent() {
  Object.keys(manualAttMap).forEach((sid) => {
    manualAttMap[sid] = "Present";
    const btn = $(`togbtn-${sid}`);
    if (btn) {
      btn.textContent = "Present";
      btn.className = "toggle-btn present";
    }
  });
}
function markAllAbsent() {
  Object.keys(manualAttMap).forEach((sid) => {
    manualAttMap[sid] = "Absent";
    const btn = $(`togbtn-${sid}`);
    if (btn) {
      btn.textContent = "Absent";
      btn.className = "toggle-btn absent";
    }
  });
}
async function saveAllManualAttendance() {
  const date = $("tManualDate").value,
    entries = Object.entries(manualAttMap);
  if (!entries.length) {
    toast("No students to mark", "err");
    return;
  }
  let saved = 0,
    errors = 0;
  for (const [sid, status] of entries) {
    try {
      await api("/teacher/attendance/mark", {
        method: "POST",
        body: JSON.stringify({ student_id: sid, date, status }),
      });
      saved++;
    } catch {
      errors++;
    }
  }
  toast(errors ? `Saved ${saved}, ${errors} errors` : `Saved ${saved} records`);
  if (!errors) loadManualAttendance();
}

/* ═══════════════════════════════════════════════════════════════════════════
   TEACHER — LOGS
   ═══════════════════════════════════════════════════════════════════════════ */
async function loadTeacherLogs() {
  renderTeacherClassBar("teacherClassBar4");
  const days = $("tLogDays").value;
  try {
    const data = await api(`/teacher/attendance/logs?days=${days}`);
    const tbody = $("tLogsBody");
    if (!data.logs.length) {
      tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--text3);padding:2rem;">No attendance records yet</td></tr>`;
      return;
    }
    tbody.innerHTML = data.logs
      .map(
        (r) =>
          `<tr><td>${fmtDate(r.date)}</td><td style="font-family:var(--mono);font-size:11px;">${r.student_id}</td><td style="font-weight:500;">${r.full_name}</td><td style="font-family:var(--mono);font-size:12px;">${r.time || "—"}</td><td>${statusBadge(r.status)}</td><td style="font-size:12px;color:var(--text2);">${r.note || "—"}</td></tr>`,
      )
      .join("");
  } catch (e) {
    $("tLogsBody").innerHTML =
      `<tr><td colspan="6" style="color:var(--red);">${e.message}</td></tr>`;
  }
}

/* ═══════════════════════════════════════════════════════════════════════════
   BOOT — ISSUE 6: validate token on startup before showing app
   ═══════════════════════════════════════════════════════════════════════════ */
window.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".modal-overlay").forEach((m) => {
    m.addEventListener("click", (e) => {
      if (e.target === m) m.classList.remove("open");
    });
  });
  const attDate = $("attDate");
  if (attDate && !attDate.value)
    attDate.value = new Date().toISOString().slice(0, 10);
  setApiStatus(false);
  const label = $("apiStatus");
  if (label) label.textContent = "Connecting…";
  startHealthPolling();

  if (token && userRole) {
    // ── ISSUE 6: Validate the stored token before restoring the session ──
    // Avoids the 401 cascade when the server was restarted and sessions cleared.
    fetch(`${API}/auth/validate`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((r) => r.json())
      .then((data) => {
        if (data.valid) {
          $("loginOverlay").classList.add("hidden");
          afterLogin();
        } else {
          // Token is stale — clear it and show login with a message
          ["frs_token", "frs_role", "frs_user"].forEach((k) =>
            localStorage.removeItem(k),
          );
          token = "";
          userRole = "";
          userInfo = null;
          $("loginOverlay").classList.remove("hidden");
          selectLoginRole("admin");
          const errEl = $("loginErr");
          if (errEl && data.reason === "session_expired") {
            errEl.textContent =
              "Your previous session has expired. Please sign in again.";
            errEl.className = "msg err";
          }
        }
      })
      .catch(() => {
        // Network error — show login anyway
        $("loginOverlay").classList.remove("hidden");
        selectLoginRole("admin");
      });
  } else {
    $("loginOverlay").classList.remove("hidden");
    selectLoginRole("admin");
    checkHealth();
  }
});

/* ═══════════════════════════════════════════════════════════════════════════
   VOICE GUIDANCE & AUDIO EFFECTS (unchanged from v3.0)
   ═══════════════════════════════════════════════════════════════════════════ */
let _voiceEnabled = true,
  _lastSpokenText = "",
  _lastSpokenTime = 0;
const VOICE_COOLDOWN_MS = 1800;

function _speakInstruction(text, force = false) {
  if (!_voiceEnabled || !("speechSynthesis" in window)) return;
  const now = Date.now();
  if (
    !force &&
    text === _lastSpokenText &&
    now - _lastSpokenTime < VOICE_COOLDOWN_MS
  )
    return;
  _lastSpokenText = text;
  _lastSpokenTime = now;
  try {
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 0.95;
    u.pitch = 1.0;
    u.volume = 1.0;
    const voices = window.speechSynthesis.getVoices();
    const ev = voices.find((v) => v.lang.startsWith("en"));
    if (ev) u.voice = ev;
    window.speechSynthesis.speak(u);
  } catch {}
}
if ("speechSynthesis" in window)
  window.speechSynthesis.onvoiceschanged = () => {};

let _audioCtx = null;
function _getAudioCtx() {
  if (!_audioCtx)
    _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  if (_audioCtx.state === "suspended") _audioCtx.resume();
  return _audioCtx;
}
function _playTone(freq, dur, type = "sine", gain = 0.35, delay = 0) {
  try {
    const ctx = _getAudioCtx(),
      osc = ctx.createOscillator(),
      g = ctx.createGain();
    osc.connect(g);
    g.connect(ctx.destination);
    osc.type = type;
    osc.frequency.setValueAtTime(freq, ctx.currentTime + delay);
    g.gain.setValueAtTime(0, ctx.currentTime + delay);
    g.gain.linearRampToValueAtTime(gain, ctx.currentTime + delay + 0.01);
    g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + delay + dur);
    osc.start(ctx.currentTime + delay);
    osc.stop(ctx.currentTime + delay + dur + 0.05);
  } catch {}
}
function _soundDing() {
  _playTone(880, 0.22);
}
function _soundChime() {
  _playTone(523, 0.18, "sine", 0.32, 0);
  _playTone(659, 0.18, "sine", 0.32, 0.18);
  _playTone(784, 0.28, "sine", 0.38, 0.36);
}
function _soundTick() {
  _playTone(1200, 0.04, "triangle", 0.08);
}
