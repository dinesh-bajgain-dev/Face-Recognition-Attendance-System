/* ═══════════════════════════════════════════════════════════════════════════
   वेदनेत्रम् · app.js  –  Frontend application logic
   Architecture:
     • All server communication through api() helper (adds auth token)
     • navigate() switches pages and fires load functions
     • Each page has its own loader (loadDashboard, loadStudents, etc.)
     • Webcam: getUserMedia for enroll, MJPEG <img src> for live recognition
═══════════════════════════════════════════════════════════════════════════ */

const API = "http://localhost:5050/api";
let authToken = localStorage.getItem("frs_token") || "";
let authRole = localStorage.getItem("frs_role") || "";
let authUsername = localStorage.getItem("frs_username") || "";
let selectedFiles = [];
let webcamFiles = [];
let webcamStream = null;
let cameraActive = false;
let charts = {};
let pollTimer = null;
let dashRecords = []; // for client-side filter
let attRecords = [];

/* ── Boot ────────────────────────────────────────────────────────────── */
window.onload = () => {
  updateClock();
  setInterval(updateClock, 1000);
  setInterval(checkAPI, 15000);
  initUploadZone();
  document.getElementById("attDate").value = todayStr();
  document.getElementById("repFrom").value = todayStr();
  document.getElementById("repTo").value = todayStr();
  document.getElementById("dashDate").textContent =
    new Date().toLocaleDateString("en-GB", {
      weekday: "long",
      year: "numeric",
      month: "long",
      day: "numeric",
    });

  if (authToken) {
    showApp();
  } else {
    showLogin();
  }
};

/* ── Utilities ───────────────────────────────────────────────────────── */
const todayStr = () => new Date().toISOString().split("T")[0];

function updateClock() {
  const el = document.getElementById("clockDisplay");
  if (el) el.textContent = new Date().toLocaleTimeString("en-GB");
}

async function api(path, opts = {}) {
  const headers = {
    ...(opts.headers || {}),
    Authorization: `Bearer ${authToken}`,
  };
  if (opts.json) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.json);
    delete opts.json;
  }
  const r = await fetch(API + path, { ...opts, headers });
  if (r.status === 401) {
    logout();
    return null;
  }
  return r;
}

function toast(msg, type = "") {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.style.background = type === "err" ? "var(--red)" : "var(--text)";
  el.style.color = type === "err" ? "#fff" : "var(--bg)";
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2800);
}

function badge(status) {
  return `<span class="${status === "Present" ? "badge-present" : "badge-absent"}">${status}</span>`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function getStudentName(record) {
  return (record?.name || record?.full_name || "").trim();
}

/* ── Auth ────────────────────────────────────────────────────────────── */
function showLogin() {
  document.getElementById("loginOverlay").classList.remove("hidden");
  document.getElementById("sidebar").style.display = "none";
  document.querySelector(".main").style.display = "none";
  const sp = document.getElementById("student-panel");
  if (sp) sp.style.display = "none";
}
function showApp() {
  document.getElementById("loginOverlay").classList.add("hidden");
  const sp = document.getElementById("student-panel");
  if (sp && authRole !== "student") sp.style.display = "none";

  if (authRole === "student") {
    document.getElementById("sidebar").style.display = "none";
    document.querySelector(".main").style.display = "none";
    loadStudentPortal();
    return;
  }

  document.getElementById("sidebar").style.display = "flex";
  document.querySelector(".main").style.display = "block";

  // Populate sidebar user info
  const sui = document.getElementById("sidebarUserInfo");
  if (sui) {
    const ini = (authUsername || authRole || "?")
      .split(/[\s_]/)
      .map((w) => w[0]?.toUpperCase() || "")
      .slice(0, 2)
      .join("");
    const roleLabel = authRole === "teacher" ? "TEACHER" : "ADMIN";
    const roleColor =
      authRole === "teacher" ? "var(--amber)" : "var(--accent, #7c6af7)";
    sui.innerHTML = `
      <div class="sidebar-user-block">
        <div class="sidebar-user-avatar">${ini}</div>
        <div>
          <div class="sidebar-user-role" style="color:${roleColor}">${roleLabel}</div>
          <div class="sidebar-user-name">${escapeHtml(authUsername || authRole)}</div>
        </div>
      </div>`;
  }

  const adminNav = document.getElementById("adminNav");
  const teacherNav = document.getElementById("teacherNav");
  if (authRole === "teacher") {
    if (adminNav) adminNav.classList.add("hidden");
    if (teacherNav) teacherNav.classList.remove("hidden");
    checkAPI();
    navigate("t-dashboard");
  } else {
    if (adminNav) adminNav.classList.remove("hidden");
    if (teacherNav) teacherNav.classList.add("hidden");
    checkAPI();
    navigate("dashboard");
  }
}

async function doLogin() {
  const errEl = document.getElementById("loginErr");
  errEl.textContent = "";

  // Student login — email-based, read-only portal (no backend auth)
  if (_loginRole === "student") {
    const email = document.getElementById("loginEmail")?.value.trim();
    if (!email) {
      errEl.textContent = "Enter your registered email";
      return;
    }
    authToken = "student-portal";
    authRole = "student";
    localStorage.setItem("frs_token", authToken);
    localStorage.setItem("frs_role", authRole);
    localStorage.setItem("frs_student_email", email);
    showApp();
    return;
  }

  // Teacher login — email + password against teachers table
  if (_loginRole === "teacher") {
    const email = document.getElementById("loginEmail")?.value.trim();
    const password = document.getElementById("loginPass").value;
    if (!email || !password) {
      errEl.textContent = "Enter your email and password";
      return;
    }
    try {
      const r = await fetch(`${API}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      const d = await r.json();
      if (!r.ok) {
        errEl.textContent = d.error || "Login failed";
        return;
      }
      authToken = d.token;
      authRole = d.role || "teacher";
      authUsername = d.username || email;
      localStorage.setItem("frs_token", authToken);
      localStorage.setItem("frs_role", authRole);
      localStorage.setItem("frs_username", authUsername);
      showApp();
    } catch {
      errEl.textContent = "Cannot reach server";
    }
    return;
  }

  // Admin login — username + password against users table
  const username = document.getElementById("loginUser").value.trim();
  const password = document.getElementById("loginPass").value;
  if (!username || !password) {
    errEl.textContent = "Enter username and password";
    return;
  }
  try {
    const r = await fetch(`${API}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const d = await r.json();
    if (!r.ok) {
      errEl.textContent = d.error || "Login failed";
      return;
    }
    authToken = d.token;
    authRole = d.role || _loginRole;
    authUsername = d.username || username;
    localStorage.setItem("frs_token", authToken);
    localStorage.setItem("frs_role", authRole);
    localStorage.setItem("frs_username", authUsername);
    showApp();
  } catch {
    errEl.textContent = "Cannot reach server";
  }
}

function logout() {
  authToken = "";
  authRole = "";
  authUsername = "";
  localStorage.removeItem("frs_token");
  localStorage.removeItem("frs_role");
  localStorage.removeItem("frs_username");
  if (cameraActive) stopCamera();
  showLogin();
}

/* ── API health ──────────────────────────────────────────────────────── */
async function checkAPI() {
  const dot = document.getElementById("apiDot");
  const lbl = document.getElementById("apiStatus");
  try {
    const r = await fetch(`${API}/health`, {
      signal: AbortSignal.timeout(3000),
    });
    const d = await r.json();
    const ok = r.ok && d.db === "ok";
    dot.className = `status-dot ${ok ? "ok" : "err"}`;
    lbl.textContent = ok ? "Connected" : "DB Error";
  } catch {
    dot.className = "status-dot err";
    lbl.textContent = "Offline";
  }
}

/* ── Navigation ──────────────────────────────────────────────────────── */
function navigate(page) {
  document
    .querySelectorAll(".page")
    .forEach((p) => p.classList.remove("active"));
  document
    .querySelectorAll(".nav-item")
    .forEach((n) => n.classList.remove("active"));
  const pageEl = document.getElementById("page-" + page);
  if (!pageEl) return;
  pageEl.classList.add("active");
  const navEl = document.querySelector(`[data-page="${page}"]`);
  if (navEl) navEl.classList.add("active");

  const loaders = {
    dashboard: loadDashboard,
    students: loadStudents,
    attendance: loadAttendance,
    reports: loadReports,
    settings: loadSettings,
    recognize: () => loadLiveLog(),
    teachers: loadTeachers,
    manage: () => switchManageTab("faculties"),
    "t-dashboard": loadTeacherDashboard,
    "t-recognize": () => {},
    "t-manual": () => {
      document.getElementById("tManualDate") &&
        !document.getElementById("tManualDate").value &&
        (document.getElementById("tManualDate").value = todayStr());
      loadManualAttendance();
    },
    "t-logs": loadTeacherLogs,
    "t-reports": loadTeacherReports,
    profile: () => {},
    enroll: () => {
      _ensureDefaultData();
      _populateFacultyDropdowns();
    },
  };
  if (loaders[page]) loaders[page]();
}

/* ═══════════════════════════════════════════════════════════════════════
   DASHBOARD
═══════════════════════════════════════════════════════════════════════ */
async function loadDashboard() {
  try {
    const deptFilter = document.getElementById("dashDeptFilter")?.value || "";
    const [att, hist, persons] = await Promise.all([
      api(
        `/attendance?date=${todayStr()}${deptFilter ? "&department=" + encodeURIComponent(deptFilter) : ""}`,
      ).then((r) => r.json()),
      api(`/attendance/history`).then((r) => r.json()),
      api(
        `/students${deptFilter ? "?department=" + encodeURIComponent(deptFilter) : ""}`,
      ).then((r) => r.json()),
    ]);
    // Populate dept filter if empty
    const ddf = document.getElementById("dashDeptFilter");
    if (ddf && ddf.options.length <= 1) {
      try {
        const dr = await api("/departments");
        const dd = await dr.json();
        (dd.departments || []).forEach((d) => {
          const o = document.createElement("option");
          o.value = d;
          o.text = d;
          ddf.add(o);
        });
      } catch {}
    }
    const rate =
      persons.count > 0
        ? Math.round((att.present / persons.count) * 100) + "%"
        : "—";
    const m = document.getElementById("dashMetrics");
    if (m)
      m.innerHTML = `
      <div class="metric-card"><div class="metric-label">Total Students</div><div class="metric-val">${persons.count}</div></div>
      <div class="metric-card"><div class="metric-label">Present Today</div><div class="metric-val" style="color:var(--green)">${att.present}</div></div>
      <div class="metric-card"><div class="metric-label">Absent Today</div><div class="metric-val" style="color:var(--red)">${att.absent}</div></div>
      <div class="metric-card"><div class="metric-label">Attendance Rate</div><div class="metric-val" style="color:var(--blue)">${rate}</div></div>`;
    const dd = document.getElementById("dashDate");
    if (dd)
      dd.textContent = new Date().toLocaleDateString("en-GB", {
        weekday: "long",
        day: "2-digit",
        month: "long",
        year: "numeric",
      });
    renderHistoryChart(hist.history);
    dashRecords = att.records;
    renderDashTable(dashRecords);
  } catch (e) {
    console.error("loadDashboard:", e);
  }
}

function renderHistoryChart(history) {
  const sorted = [...history].sort((a, b) => a.date.localeCompare(b.date));
  const _hc = document.getElementById("dashboardChart");
  if (!_hc || typeof Chart === "undefined") return;
  const ctx = _hc.getContext("2d");
  if (charts.dash) charts.dash.destroy();
  charts.dash = new Chart(ctx, {
    type: "bar",
    data: {
      labels: sorted.map((h) => h.date.slice(5)),
      datasets: [
        {
          label: "Present",
          data: sorted.map((h) => h.present),
          backgroundColor: "rgba(34,197,94,0.65)",
          borderRadius: 3,
          borderSkipped: false,
        },
        {
          label: "Absent",
          data: sorted.map((h) => h.absent),
          backgroundColor: "rgba(239,68,68,0.3)",
          borderRadius: 3,
          borderSkipped: false,
        },
      ],
    },
    options: chartOpts({ stacked: true }),
  });
}

function renderRecentLogs(logs) {
  const el =
    document.getElementById("tSessionLog") ||
    document.getElementById("liveLog");
  if (!el) return;
  el.innerHTML =
    (logs || [])
      .slice(0, 15)
      .map(
        (l) => `
    <div class="log-item ${l.recognized ? "ok" : "fail"}">
      <span class="log-name">${l.full_name || "Unknown"}</span>
      <span class="log-conf">${l.confidence}% · ${(l.logged_at || "").slice(11, 16)}</span>
    </div>`,
      )
      .join("") || `<p class="muted">No events yet</p>`;
}

function renderDashTable(records) {
  const el = document.getElementById("dashAttTable");
  if (!el) return;
  if (!records || !records.length) {
    el.innerHTML = `<p style="color:var(--text3);padding:1rem 0">No attendance records for today.</p>`;
    return;
  }
  el.innerHTML = `<div style="overflow-x:auto"><table class="data-table">
    <thead><tr><th>ID</th><th>Name</th><th>Department</th><th>Time</th><th>Status</th></tr></thead>
    <tbody>${records
      .map(
        (r) => `<tr>
      <td style="font-family:var(--mono);font-size:12px">${r.student_id}</td>
      <td><a style="cursor:pointer" onclick="viewProfile('${r.student_id}')">${escapeHtml(r.name || r.full_name || "")}</a></td>
      <td style="color:var(--text3)">${r.department || "—"}</td>
      <td style="font-family:var(--mono);font-size:12px">${r.time || "—"}</td>
      <td>${badge(r.status)}</td>
    </tr>`,
      )
      .join("")}</tbody></table></div>`;
}

function filterDashTable(q) {
  const query = q.toLowerCase();
  const filtered = dashRecords.filter((r) =>
    getStudentName(r).toLowerCase().includes(query),
  );
  renderDashTable(filtered);
}

/* ═══════════════════════════════════════════════════════════════════════
   STUDENTS
═══════════════════════════════════════════════════════════════════════ */
let _stuCache = null,
  _stuDept = "";
async function loadStudents() {
  _stuCache = null;
  _stuDept = "";
  await searchStudents();
}

async function loadDepartments(selectId) {
  try {
    const r = await api("/departments");
    const d = await r.json();
    const sel = document.getElementById(selectId);
    const val = sel.value;
    // keep first option
    while (sel.options.length > 1) sel.remove(1);
    d.departments.forEach((dept) => {
      const o = document.createElement("option");
      o.value = dept;
      o.text = dept;
      sel.add(o);
    });
    sel.value = val;
  } catch {}
}

function filterStudentDept(dept, btn) {
  _stuDept = dept;
  document
    .querySelectorAll("#studentFacultyTabs .filter-btn")
    .forEach((b) => b.classList.remove("active"));
  if (btn) btn.classList.add("active");
  searchStudents(true);
}
async function searchStudents(useCache = false) {
  const q = document.getElementById("studentSearch")?.value.trim() || "";
  try {
    if (!useCache || !_stuCache) {
      const r = await api("/students");
      _stuCache = await r.json();
    }
    const all = _stuCache;
    const tabs = document.getElementById("studentFacultyTabs");
    if (tabs) {
      const depts = [
        ...new Set(
          (all.students || []).map((s) => s.department).filter(Boolean),
        ),
      ].sort();
      tabs.innerHTML =
        `<button class="filter-btn ${_stuDept === "" ? "active" : ""}" onclick="filterStudentDept('',this)">All (${all.count})</button>` +
        depts
          .map(
            (d) =>
              `<button class="filter-btn ${_stuDept === d ? "active" : ""}" onclick="filterStudentDept('${d}',this)">${d} (${(all.students || []).filter((s) => s.department === d).length})</button>`,
          )
          .join("");
    }
    let rows = all.students || [];
    if (_stuDept) rows = rows.filter((s) => s.department === _stuDept);
    if (q)
      rows = rows.filter(
        (s) =>
          s.full_name.toLowerCase().includes(q.toLowerCase()) ||
          s.student_id.toLowerCase().includes(q.toLowerCase()),
      );
    const grid = document.getElementById("studentFacultyBar");
    if (!grid) return;
    if (!rows.length) {
      grid.innerHTML = `<p style="color:var(--text3);padding:1.5rem;text-align:center">No students found.</p>`;
      return;
    }
    grid.innerHTML = `<div class="student-grid">${rows
      .map((s) => {
        const ini = (s.full_name || "?")
          .split(" ")
          .map((w) => w[0]?.toUpperCase() || "")
          .slice(0, 2)
          .join("");
        const isActive = (s.status || "active") === "active";
        const statusColor = isActive ? "var(--green)" : "var(--amber)";
        const statusBg = isActive
          ? "rgba(34,197,94,0.12)"
          : "rgba(245,158,11,0.12)";
        return `<div class="student-card" onclick="viewProfile('${s.student_id}')" title="${escapeHtml(s.full_name)}">
          <div class="stu-avatar-ini">${ini}</div>
          <div class="student-name">${escapeHtml(s.full_name)}</div>
          <div class="student-id">${s.student_id}</div>
          ${s.department ? `<div class="stu-dept">${escapeHtml(s.department)}${s.semester ? " · " + escapeHtml(s.semester) : ""}</div>` : ""}
          <span class="stu-status" style="color:${statusColor};background:${statusBg}">${s.status || "active"}</span>
        </div>`;
      })
      .join("")}</div>`;
  } catch (e) {
    console.error("searchStudents:", e);
  }
}

async function viewProfile(sid) {
  navigate("profile");
  document.getElementById("profileContent").innerHTML =
    `<p class="muted">Loading…</p>`;
  try {
    const r = await api(`/students/${sid}`);
    const s = await r.json();
    const pct = s.stats?.percentage || 0;
    const initials = s.full_name
      .split(" ")
      .map((w) => w[0]?.toUpperCase())
      .slice(0, 2)
      .join("");
    (document.getElementById("profileActions") || null || {}).innerHTML = `
      <button class="btn-secondary" onclick="openEditModal('${sid}')">Edit Student</button>`;

    document.getElementById("profileContent").innerHTML = `
      <div class="profile-header">
        <div class="profile-avatar-lg">${initials}</div>
        <div class="profile-info">
          <h2>${s.full_name}</h2>
          <p>${s.email || ""} ${s.phone ? "· " + s.phone : ""}</p>
          <div class="profile-meta">
            <span class="meta-tag">ID: ${s.student_id}</span>
            ${s.department ? `<span class="meta-tag">${s.department}</span>` : ""}
            <span class="meta-tag">Enrolled ${s.enrolled_at?.slice(0, 10) || ""}</span>
            <span class="meta-tag">${s.sample_count} training images</span>
          </div>
        </div>
      </div>
      <div class="profile-stats">
        <div class="pstat">
          <div class="pstat-label">Attendance %</div>
          <div class="pstat-val" style="color:${pct >= 75 ? "var(--green)" : pct >= 50 ? "var(--amber)" : "var(--red)"}">${pct || 0}%</div>
          <div class="pct-bar"><div class="pct-fill" style="width:${pct}%;background:${pct >= 75 ? "var(--green)" : pct >= 50 ? "var(--amber)" : "var(--red)"}"></div></div>
        </div>
        <div class="pstat"><div class="pstat-label">Present Days</div><div class="pstat-val" style="color:var(--green)">${s.stats?.total_present || 0}</div></div>
        <div class="pstat"><div class="pstat-label">Total Days</div><div class="pstat-val">${s.stats?.total_days || 0}</div></div>
      </div>
      <div class="two-col">
        <div>
          <div class="section-title">Monthly Attendance</div>
          <div class="card"><canvas id="profileChart" height="180"></canvas></div>
        </div>
        <div>
          <div class="section-title">Recent Recognition Logs</div>
          <div class="card">
            ${
              (s.logs || [])
                .map(
                  (l) => `
              <div class="log-item ${l.recognized ? "ok" : "fail"}" style="margin-bottom:4px">
                <span class="log-name">${l.recognized ? "✓ Recognized" : "✗ Failed"}</span>
                <span class="log-conf">${l.confidence}% · ${l.logged_at?.slice(11, 16) || ""}</span>
              </div>`,
                )
                .join("") || '<p class="muted">No logs yet</p>'
            }
          </div>
        </div>
      </div>`;

    // Monthly chart
    const monthly = s.monthly || [];
    if (monthly.length) {
      const ctx = (
        document.getElementById("profileChart") ||
        null ||
        {}
      ).getContext("2d");
      new Chart(ctx, {
        type: "bar",
        data: {
          labels: monthly.map((m) => m.month),
          datasets: [
            {
              label: "Present",
              data: monthly.map((m) => m.present),
              backgroundColor: "rgba(34,197,94,0.65)",
              borderRadius: 3,
            },
          ],
        },
        options: chartOpts({}),
      });
    }
  } catch (e) {
    console.error(e);
    document.getElementById("profileContent").innerHTML =
      `<p class="msg err">Failed to load profile.</p>`;
  }
}

async function deleteStudent(sid) {
  // now handled by the edit modal's confirmDeleteStudent()
  openEditModal(sid);
}

async function openEditModal(sid) {
  const r = await api(`/students/${sid}`);
  const s = await r.json();
  const val = prompt(`Edit name for ${s.full_name}:`, s.full_name);
  if (!val) return;
  await api(`/students/${sid}`, { method: "PUT", json: { full_name: val } });
  toast("Updated");
  viewProfile(sid);
}

/* ═══════════════════════════════════════════════════════════════════════
   ENROLL
═══════════════════════════════════════════════════════════════════════ */
function initUploadZone() {
  const zone = document.getElementById("uploadZone") || null;
  if (!zone) return;
  zone.addEventListener("dragover", (e) => {
    e.preventDefault();
    zone.classList.add("drag-over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("drag-over");
    handleFiles(e.dataTransfer.files);
  });
  zone.addEventListener("click", () =>
    (document.getElementById("fileInput") || null || {}).click(),
  );
}

function handleFiles(files) {
  selectedFiles = Array.from(files);
  renderFilePreviews(selectedFiles, "filePreview");
}

function renderFilePreviews(files, containerId) {
  const preview = document.getElementById(containerId);
  if (!preview) return;
  preview.innerHTML = "";
  files.slice(0, 8).forEach((f) => {
    const img = document.createElement("img");
    img.src = URL.createObjectURL(f);
    img.className = "file-thumb";
    preview.appendChild(img);
  });
  if (files.length > 8) {
    const badge = document.createElement("div");
    badge.className = "file-count";
    badge.textContent = `+${files.length - 8}`;
    preview.appendChild(badge);
  }
}

async function startWebcam() {
  try {
    const section = document.getElementById("captureCamWrap");
    section.style.display = "block";
    webcamStream = await navigator.mediaDevices.getUserMedia({ video: true });
    document.getElementById("captureCam").srcObject = webcamStream;
    webcamFiles = [];
    updateWebcamCount();
  } catch (e) {
    toast("Camera access denied", "err");
  }
}

function stopWebcam() {
  if (webcamStream) {
    webcamStream.getTracks().forEach((t) => t.stop());
    webcamStream = null;
  }
  document.getElementById("captureCamWrap").style.display = "none";
}

function captureWebcamFrame() {
  const video = document.getElementById("captureCam");
  const canvas = document.createElement("canvas");
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d").drawImage(video, 0, 0);
  canvas.toBlob(
    (blob) => {
      const file = new File([blob], `webcam_${Date.now()}.jpg`, {
        type: "image/jpeg",
      });
      webcamFiles.push(file);
      updateWebcamCount();
    },
    "image/jpeg",
    0.9,
  );
}

function updateWebcamCount() {
  document.getElementById("captureCount").textContent =
    `${webcamFiles.length} frames captured`;
}

async function enrollStudent() {
  const sid = document.getElementById("eId").value.trim();
  const name = document.getElementById("eName").value.trim();
  const faculty_id = document.getElementById("eFaculty").value.trim();
  const sem = document.getElementById("eSem")?.value.trim() || "";
  const email = document.getElementById("eEmail").value.trim();
  const phone = document.getElementById("ePhone").value.trim();
  const msg = document.getElementById("enrollMsg");

  if (!sid || !name || !faculty_id || !sem || !email) {
    setMsg("enrollMsg", "All fields are required.", "err");
    return;
  }

  const allFiles = [...selectedFiles, ...webcamFiles];
  if (!allFiles.length) {
    setMsg("enrollMsg", "Upload images or capture via webcam.", "err");
    return;
  }

  const progress = document.getElementById("enrollProgress");
  const bar = document.getElementById("enrollBar");
  progress.style.display = "block";
  bar.style.width = "5%";
  setMsg("enrollMsg", "Uploading and generating embeddings…", "");

  const form = new FormData();
  form.append("student_id", sid);
  form.append("full_name", name);
  if (faculty_id) form.append("faculty_id", faculty_id);
  if (sem) form.append("semester", sem);
  if (email) form.append("email", email);
  if (phone) form.append("phone", phone);
  allFiles.forEach((f) => form.append("images", f));

  try {
    bar.style.width = "40%";
    const r = await api("/enroll", { method: "POST", body: form });
    const d = await r.json();
    bar.style.width = "100%";
    setTimeout(() => {
      progress.style.display = "none";
      bar.style.width = "0%";
    }, 600);

    if (!r.ok) {
      setMsg("enrollMsg", d.error || "Enrollment failed.", "err");
      return;
    }
    setMsg(
      "enrollMsg",
      `✓ Enrolled ${name}. ${d.embeddings_generated} embeddings generated. ${d.images_failed} images failed.`,
      "ok",
    );
    toast(`${name} enrolled successfully`);
    stopWebcam();
    selectedFiles = [];
    webcamFiles = [];
    const filePreview = document.getElementById("filePreview") || null;
    if (filePreview) filePreview.innerHTML = "";
    ["eId", "eName", "eFaculty", "eSem", "eEmail", "ePhone"].forEach(
      (id) => { const el = document.getElementById(id); if (el) el.value = ""; },
    );
  } catch (e) {
    setMsg("enrollMsg", "Error: " + e.message, "err");
    progress.style.display = "none";
  }
}

/* ═══════════════════════════════════════════════════════════════════════
   RECOGNIZE
═══════════════════════════════════════════════════════════════════════ */
async function toggleCamera() {
  const btn = document.getElementById("camToggle") || null;
  if (!cameraActive) {
    await api("/camera/start", { method: "POST" });
    cameraActive = true;
    btn.textContent = "■ Stop Camera";
    document.getElementById("tRecogVideo").style.display = "block";
    document.getElementById("tRecogOverlay").style.display = "none";
    document.getElementById("tRecogCanvas").style.display = "none";
    document.getElementById("tRecogVideo").src = `${API}/stream`;
    startPollLiveLog();
  } else {
    stopCamera();
  }
}

async function stopCamera() {
  await api("/camera/stop", { method: "POST" });
  cameraActive = false;
  const btn = document.getElementById("camToggle") || null;
  if (btn) btn.textContent = "▶ Start Camera";
  const stream = document.getElementById("tRecogVideo");
  if (stream) {
    stream.src = "";
    stream.style.display = "none";
  }
  const ph = document.getElementById("tRecogOverlay");
  if (ph) ph.style.display = "flex";
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function startPollLiveLog() {
  if (pollTimer) clearTimeout(pollTimer);
  async function poll() {
    if (!cameraActive) return;
    await loadLiveLog();
    pollTimer = setTimeout(poll, 3000);
  }
  poll();
}

async function loadLiveLog() {
  try {
    const r = await api("/logs?limit=20");
    const d = await r.json();
    const el = document.getElementById("tSessionLog");
    if (!el) return;
    el.innerHTML =
      d.logs
        .map(
          (l) => `
      <div class="log-item ${l.recognized ? "ok" : "fail"}">
        <span class="log-name">${l.full_name || "Unknown"}</span>
        <span class="log-conf">${l.confidence}% · ${l.logged_at?.slice(11, 16) || ""}</span>
      </div>`,
        )
        .join("") ||
      `<p class="muted" style="font-size:12px">No events yet</p>`;

    // Update result card with the most recent recognized
    const latest = d.logs.find((l) => l.recognized);
    if (latest) {
      setRecogResult(
        latest.full_name,
        `${latest.confidence}% confidence`,
        "present",
      );
    }
  } catch {}
}

async function recognizeImage(input) {
  const file = input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = async (e) => {
    const b64 = e.target.result.split(",")[1];
    const img = new Image();
    img.onload = () => {
      const canvas = document.getElementById("tRecogCanvas");
      const ctx = canvas.getContext("2d");
      canvas.width = img.width;
      canvas.height = img.height;
      ctx.drawImage(img, 0, 0);
      canvas.style.display = "block";
      document.getElementById("tRecogOverlay").style.display = "none";
      document.getElementById("tRecogVideo").style.display = "none";
    };
    img.src = e.target.result;

    setRecogResult("Analyzing…", "", "");
    try {
      const r = await api("/recognize", {
        method: "POST",
        json: { image: b64 },
      });
      const d = await r.json();
      if (d.recognized) {
        setRecogResult(d.name, `${d.confidence}% match`, "present");
        if (d.attendance_marked) toast(`✓ ${d.name} marked present`);
        loadLiveLog();
      } else {
        setRecogResult(
          "Unknown",
          d.confidence ? `${d.confidence}% best match` : "No face detected",
          "unknown",
        );
      }
    } catch (err) {
      setRecogResult("Error", err.message, "unknown");
    }
  };
  reader.readAsDataURL(file);
  input.value = "";
}

function setRecogResult(name, conf, badgeType) {
  (document.getElementById("recogName") || null || {}).textContent = name;
  (document.getElementById("recogConf") || null || {}).textContent = conf;
  const icon = document.getElementById("recogIcon") || null;
  const badge = document.getElementById("recogBadge") || null;
  icon.className =
    "recog-icon" +
    (badgeType === "present" ? " ok" : badgeType === "unknown" ? " fail" : "");
  badge.className = "recog-badge";
  if (badgeType === "present") {
    badge.classList.add("present");
    badge.textContent = "PRESENT";
  } else if (badgeType === "unknown") {
    badge.classList.add("unknown");
    badge.textContent = "UNKNOWN";
  } else badge.textContent = "";
}

/* ═══════════════════════════════════════════════════════════════════════
   ATTENDANCE — Faculty-tabbed view
   State:
     activeFaculty  = "" (All) | "BCA" | "BBM" | ...
     facultyData    = full response from /api/attendance/faculty-summary
     filteredStudents = current filtered list for the active faculty
═══════════════════════════════════════════════════════════════════════ */
let activeFaculty = ""; // "" = All
let facultyData = null;
let filteredStudents = [];

async function loadAttendancePage() {
  const dateVal = document.getElementById("attDate").value || todayStr();
  // Populate department filter if empty
  const adf = document.getElementById("attDeptFilter");
  if (adf && adf.options.length <= 1) {
    try {
      const dr = await api("/departments");
      const dd = await dr.json();
      (dd.departments || []).forEach((d) => {
        const o = document.createElement("option");
        o.value = d;
        o.text = d;
        adf.add(o);
      });
    } catch {}
  }
  try {
    const r = await api(`/attendance/faculty-summary?date=${dateVal}`);
    if (!r || !r.ok) {
      console.error("faculty-summary failed");
      return;
    }
    facultyData = await r.json();
    renderFacultyTabs();
    showFaculty(activeFaculty);
  } catch (e) {
    console.error("loadAttendancePage:", e);
  }
}

// Keep backward-compat alias so navigate() still works
const loadAttendance = loadAttendancePage;

function renderFacultyTabs() {
  if (!facultyData) return;
  const container = document.getElementById("attFacultySummary");
  if (!container) return;
  const faculties = facultyData.faculties || [];

  const tabs = [
    { key: "", label: `All (${facultyData.overall?.total || 0})` },
    ...faculties.map((f) => ({
      key: f.name,
      label: `${f.name} (${f.total})`,
    })),
  ];

  container.innerHTML = `<div class="filter-bar" id="facultyTabsBar">${tabs
    .map(
      (t) => `
    <button class="filter-btn ${activeFaculty === t.key ? "active" : ""}"
         onclick="showFaculty('${t.key}')">
      ${t.label}
    </button>`,
    )
    .join("")}</div>`;
}

function showFaculty(key) {
  activeFaculty = key;
  // Update active tab highlight
  document.querySelectorAll("#facultyTabsBar .filter-btn").forEach((el) => {
    const isActive =
      key === ""
        ? el.textContent.trim().startsWith("All")
        : el.textContent.trim().startsWith(key);
    el.classList.toggle("active", isActive);
  });
  const attSearch = document.getElementById("attSearch");
  if (attSearch) attSearch.value = "";

  if (key === "") {
    showAllFaculties();
  } else {
    const fac = (facultyData?.faculties || []).find((f) => f.name === key);
    showSingleFaculty(fac);
  }
}

function showAllFaculties() {
  const faculties = facultyData?.faculties || [];
  const overall = facultyData?.overall || {};
  const dateVal = document.getElementById("attDate").value || todayStr();

  renderFacultyTabs();
  filteredStudents = faculties.flatMap((f) =>
    f.students.map((s) => ({ ...s, faculty: f.name })),
  );
  const container = document.getElementById("attTableWrap");
  container.innerHTML =
    faculties
      .map(
        (f) => `
    <div class="faculty-section" id="fac-section-${f.name.replace(/\s+/g, "_")}">
      <div class="faculty-section-header">
        <span class="fsh-name">${f.name}</span>
        <div class="fsh-pills">
          <span class="fsc-pill green">Present: ${f.present}</span>
          <span class="fsc-pill red">Absent: ${f.absent}</span>
          <span style="font-family:var(--mono);font-size:12px;color:var(--text2)">${f.rate}%</span>
        </div>
      </div>
      ${buildStudentTable(f.students, dateVal, false)}
    </div>`,
      )
      .join("") ||
    `<p class="muted" style="margin-top:1rem">No faculty data for this date.</p>`;
}

function showSingleFaculty(fac) {
  if (!fac) return;
  const dateVal = document.getElementById("attDate").value || todayStr();

  renderFacultyTabs();
  const _hdr = document.getElementById("attFacultySummary");
  if (_hdr) {
    const rc =
      fac.rate >= 75
        ? "var(--green)"
        : fac.rate >= 50
          ? "var(--amber)"
          : "var(--red)";
    // Preserve the tabs bar, append the summary card after it
    const tabsBar = _hdr.querySelector("#facultyTabsBar");
    const tabsHtml = tabsBar ? tabsBar.outerHTML : "";
    _hdr.innerHTML =
      tabsHtml +
      `<div class="card" style="margin-top:.75rem;display:flex;flex-wrap:wrap;gap:1.5rem;align-items:center;padding:1rem">
      <div><div style="font-size:18px;font-weight:700">${fac.name}</div>
      <div style="font-size:12px;color:var(--text3)">${new Date(dateVal).toLocaleDateString("en-GB", { weekday: "long", year: "numeric", month: "long", day: "numeric" })}</div></div>
      <div style="display:flex;gap:1.5rem;margin-left:auto;flex-wrap:wrap">
        <div style="text-align:center"><div style="font-size:22px;font-weight:700;color:var(--green)">${fac.present}</div><div style="font-size:11px;color:var(--text3)">Present</div></div>
        <div style="text-align:center"><div style="font-size:22px;font-weight:700;color:var(--red)">${fac.absent}</div><div style="font-size:11px;color:var(--text3)">Absent</div></div>
        <div style="text-align:center"><div style="font-size:22px;font-weight:700">${fac.total}</div><div style="font-size:11px;color:var(--text3)">Total</div></div>
        <div style="text-align:center"><div style="font-size:22px;font-weight:700;color:${rc}">${fac.rate}%</div><div style="font-size:11px;color:var(--text3)">Rate</div></div>
      </div></div>`;
  }
  filteredStudents = fac.students;
  document.getElementById("attTableWrap").innerHTML = buildStudentTable(
    fac.students,
    dateVal,
    true,
  );
}

function renderSummaryCard(f, isOverall) {
  const rate = f.rate || 0;
  const color =
    rate >= 75 ? "var(--green)" : rate >= 50 ? "var(--amber)" : "var(--red)";
  const click = isOverall ? "" : `onclick="showFaculty('${f.name}')"`;
  return `
    <div class="faculty-summary-card" ${click}>
      <div class="fsc-name">${f.name}</div>
      <div class="fsc-row">
        <div class="fsc-pills">
          <span class="fsc-pill green">${f.present} present</span>
          <span class="fsc-pill red">${f.absent} absent</span>
        </div>
        <span class="fsc-rate" style="color:${color}">${rate}%</span>
      </div>
      <div class="fsc-bar">
        <div class="fsc-fill" style="width:${rate}%;background:${color}"></div>
      </div>
    </div>`;
}

function buildStudentTable(students, dateVal, showFacultyCol) {
  if (!students || !students.length) {
    return `<p class="muted" style="padding:.75rem 0">No students in this faculty.</p>`;
  }
  const rows = students
    .map(
      (s) => `
    <tr>
      <td style="font-family:var(--mono);font-size:11px">${s.student_id}</td>
      <td><a onclick="viewProfile('${s.student_id}')">${s.name}</a></td>
      ${showFacultyCol ? "" : ""}
      <td style="font-family:var(--mono);font-size:11px">${dateVal}</td>
      <td style="font-family:var(--mono);font-size:11px">${s.time}</td>
      <td>${badge(s.status)}</td>
    </tr>`,
    )
    .join("");

  return `
    <table class="data-table">
      <thead>
        <tr>
          <th>Student ID</th>
          <th>Name</th>
          <th>Date</th>
          <th>Time</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function filterActiveTable(q) {
  if (!facultyData) return;
  q = q.toLowerCase();

  if (activeFaculty === "") {
    // Filter across all faculties, rebuild sections
    const dateVal = document.getElementById("attDate").value || todayStr();
    const container = document.getElementById("attTableWrap");
    container.innerHTML =
      (facultyData.faculties || [])
        .map((f) => {
          const filtered = f.students.filter(
            (s) =>
              s.name.toLowerCase().includes(q) ||
              s.student_id.toLowerCase().includes(q),
          );
          if (!filtered.length) return "";
          return `
        <div class="faculty-section">
          <div class="faculty-section-header">
            <span class="fsh-name">${f.name}</span>
            <div class="fsh-pills">
              <span class="fsc-pill green">${filtered.filter((s) => s.status === "Present").length} present</span>
              <span class="fsc-pill red">${filtered.filter((s) => s.status !== "Present").length} absent</span>
            </div>
          </div>
          ${buildStudentTable(filtered, dateVal, false)}
        </div>`;
        })
        .join("") ||
      `<p class="muted" style="margin-top:1rem">No results for "${q}"</p>`;
  } else {
    // Filter within single faculty
    const fac = (facultyData.faculties || []).find(
      (f) => f.name === activeFaculty,
    );
    const dateVal = document.getElementById("attDate").value || todayStr();
    if (!fac) return;
    const filtered = q
      ? fac.students.filter(
          (s) =>
            s.name.toLowerCase().includes(q) ||
            s.student_id.toLowerCase().includes(q),
        )
      : fac.students;
    document.getElementById("attTableWrap").innerHTML = buildStudentTable(
      filtered,
      dateVal,
      true,
    );
  }
}

function exportFacultyCSV() {
  const dateVal = document.getElementById("attDate").value || todayStr();
  const dept = activeFaculty;
  let url = `/attendance/export?from=${dateVal}&to=${dateVal}`;
  if (dept) url += `&department=${encodeURIComponent(dept)}`;
  window.open(`${API}${url}`, "_blank");
  toast(dept ? `Exporting ${dept} CSV` : "Exporting all faculties CSV");
}

// Backward-compat stubs so existing calls don't break
function exportCSV() {
  exportFacultyCSV();
}
function filterAttTable(q) {
  filterActiveTable(q);
}
function renderAttTable(records) {
  /* no-op — replaced by buildStudentTable */
}

/* ═══════════════════════════════════════════════════════════════════════
   REPORTS
═══════════════════════════════════════════════════════════════════════ */
async function loadReports() {
  // Always reset to Overview tab when Reports page is opened
  document.querySelectorAll("#page-reports .sub-tab").forEach((t) => t.classList.remove("active"));
  document.querySelectorAll("#page-reports .sub-tab-panel").forEach((p) => p.classList.remove("active"));
  document.querySelector("#page-reports .sub-tab")?.classList.add("active");
  document.getElementById("rtab-overview")?.classList.add("active");
  // Populate faculty filter (needed by Defaulter List tab)
  _populateFacultyDropdowns();
  try {
    const [stats, hist] = await Promise.all([
      api("/attendance/stats").then((r) => r.json()),
      api("/attendance/history").then((r) => r.json()),
    ]);

    const rows = stats.stats || [];
    const pcts = rows
      .map((r) => parseFloat(r.pct || 0))
      .filter((p) => !isNaN(p));
    const avg = pcts.length
      ? (pcts.reduce((a, b) => a + b, 0) / pcts.length).toFixed(1) + "%"
      : "—";
    const high = pcts.length ? Math.max(...pcts).toFixed(1) + "%" : "—";
    const low = pcts.filter((p) => p < 75).length;

    const _sv = (id, v) => {
      const e = document.getElementById(id);
      if (e) e.textContent = v;
    };
    _sv("repAvgAtt", avg);
    _sv("repHighest", high);
    _sv("repBelowCount", low);
    api("/logs?limit=1000")
      .then((r) => r.json())
      .then((d) => _sv("repTotalRecog", (d.logs || []).length))
      .catch(() => {});
    const sw = document.getElementById("statsTableWrap");
    if (sw) {
      if (!rows.length) {
        sw.innerHTML = `<p style="color:var(--text3);padding:1rem">No data yet.</p>`;
        return;
      }
      sw.innerHTML = `<div style="overflow-x:auto"><table class="data-table">
        <thead><tr><th>ID</th><th>Name</th><th>Department</th><th>Present</th><th>Percentage</th><th>Status</th></tr></thead>
        <tbody>${rows
          .map((r) => {
            const pct = parseFloat(r.pct || 0);
            const color =
              pct >= 75
                ? "var(--green)"
                : pct >= 50
                  ? "var(--amber)"
                  : "var(--red)";
            const label =
              pct >= 75 ? "On Track" : pct >= 50 ? "At Risk" : "Critical";
            return `<tr>
            <td style="font-family:var(--mono);font-size:11px;color:var(--text3)">${r.student_id}</td>
            <td><a style="cursor:pointer" onclick="viewProfile('${r.student_id}')">${r.full_name}</a></td>
            <td style="color:var(--text3)">${r.department || "—"}</td>
            <td style="text-align:center;font-family:var(--mono)">${r.present_days || 0}</td>
            <td><div style="display:flex;align-items:center;gap:8px">
              <div style="flex:1;height:5px;background:var(--bg3);border-radius:3px;overflow:hidden">
                <div style="width:${Math.min(pct, 100)}%;height:100%;background:${color};border-radius:3px"></div>
              </div>
              <span style="font-family:var(--mono);font-size:11px;color:${color};min-width:36px">${pct}%</span>
            </div></td>
            <td><span style="font-size:11px;font-weight:600;color:${color}">${label}</span></td>
          </tr>`;
          })
          .join("")}</tbody></table></div>`;
    }

    // Monthly trend chart
    renderMonthlyChart(hist.history);
    // Dept chart
    renderDeptChart(rows);
  } catch (e) {
    console.error(e);
  }
}

function renderMonthlyChart(history) {
  const sorted = [...history].sort((a, b) => a.date.localeCompare(b.date));
  const _mc = document.getElementById("monthlyChart");
  if (!_mc || typeof Chart === "undefined") return;
  const ctx = _mc.getContext("2d");
  if (charts.monthly) charts.monthly.destroy();
  charts.monthly = new Chart(ctx, {
    type: "line",
    data: {
      labels: sorted.map((h) => h.date.slice(5)),
      datasets: [
        {
          label: "Present",
          data: sorted.map((h) => h.present),
          borderColor: "rgba(34,197,94,0.8)",
          backgroundColor: "rgba(34,197,94,0.1)",
          tension: 0.3,
          fill: true,
          pointRadius: 3,
        },
        {
          label: "Absent",
          data: sorted.map((h) => h.absent),
          borderColor: "rgba(239,68,68,0.6)",
          backgroundColor: "rgba(239,68,68,0.05)",
          tension: 0.3,
          fill: true,
          pointRadius: 3,
        },
      ],
    },
    options: chartOpts({}),
  });
}

function renderDeptChart(rows) {
  const deptMap = {};
  rows.forEach((r) => {
    const d = r.department || "Unassigned";
    if (!deptMap[d]) deptMap[d] = { total: 0, present: 0 };
    deptMap[d].total++;
    deptMap[d].present += parseFloat(r.pct || 0) / 100;
  });
  const labels = Object.keys(deptMap);
  const data = labels.map((d) =>
    deptMap[d].total > 0
      ? ((deptMap[d].present / deptMap[d].total) * 100).toFixed(1)
      : 0,
  );
  const _dc = document.getElementById("deptChart");
  if (!_dc || typeof Chart === "undefined") return;
  const ctx = _dc.getContext("2d");
  if (charts.dept) charts.dept.destroy();
  charts.dept = new Chart(ctx, {
    type: "bar",
    data: {
      labels,
      datasets: [
        {
          label: "Avg %",
          data,
          backgroundColor: "rgba(59,130,246,0.6)",
          borderRadius: 4,
        },
      ],
    },
    options: chartOpts({}),
  });
}

function exportRange() {
  const from = document.getElementById("repFrom").value || todayStr();
  const to = document.getElementById("repTo").value || todayStr();
  window.open(`${API}/attendance/export?from=${from}&to=${to}`, "_blank");
}

/* ═══════════════════════════════════════════════════════════════════════
   SETTINGS
═══════════════════════════════════════════════════════════════════════ */
async function loadSettings() {
  try {
    const r = await api("/settings");
    const d = await r.json();
    const _ts = document.getElementById("threshSlider");
    if (_ts) _ts.value = d.recognition_threshold;
    const _ss = document.getElementById("skipSlider");
    if (_ss) _ss.value = d.frame_skip;
    const _tv = document.getElementById("threshVal");
    if (_tv) _tv.textContent = Math.round(d.recognition_threshold * 100) + "%";
    const _sv2 = document.getElementById("skipVal");
    if (_sv2) _sv2.textContent = d.frame_skip;
    const _si = document.getElementById("sysInfo");
    if (_si)
      _si.innerHTML = `
      <div style="display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--border)"><span style="color:var(--text2)">Threshold</span><span style="font-family:var(--mono);font-size:12px">${Math.round(d.recognition_threshold * 100)}%</span></div>
      <div style="display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--border)"><span style="color:var(--text2)">Frame Skip</span><span style="font-family:var(--mono);font-size:12px">every ${d.frame_skip} frames</span></div>
      <div style="display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px solid var(--border)"><span style="color:var(--text2)">Email</span><span style="font-family:var(--mono);font-size:12px;color:${d.email_enabled ? "var(--green)" : "var(--text3)"}">${d.email_enabled ? "Enabled" : "Disabled"}</span></div>
      <div style="display:flex;justify-content:space-between;padding:7px 0"><span style="color:var(--text2)">Version</span><span style="font-family:var(--mono);font-size:12px">v3.2</span></div>`;
  } catch {}
}

async function saveSettings() {
  const threshold = parseFloat(
    document.getElementById("threshSlider")?.value || 0.8,
  );
  const frameSkip = parseInt(document.getElementById("skipSlider")?.value || 2);
  try {
    const r = await api("/settings", {
      method: "PUT",
      json: { recognition_threshold: threshold, frame_skip: frameSkip },
    });
    if (r.ok) {
      toast("✓ Settings saved");
      loadSettings();
    } else toast("Failed to save");
  } catch {
    toast("Error saving settings");
  }
}

async function changePw() {
  const oldPw = document.getElementById("oldPw")?.value || "";
  const newPw = document.getElementById("newPw")?.value || "";
  if (!newPw || newPw.length < 6) {
    toast("New password must be at least 6 characters");
    return;
  }
  try {
    const r = await api("/auth/change-password", {
      method: "POST",
      json: { old_password: oldPw, new_password: newPw },
    });
    if (!r) return;
    const d = await r.json();
    if (r.ok) {
      toast("✓ Password updated — logging out");
      setTimeout(() => logout(), 2000);
    } else toast(d.error || "Password update failed");
  } catch (e) {
    toast("Error changing password");
  }
}

/* ═══════════════════════════════════════════════════════════════════════
   SHARED HELPERS
═══════════════════════════════════════════════════════════════════════ */
function setMsg(id, text, type) {
  const el = document.getElementById(id);
  if (el) {
    el.textContent = text;
    el.className = "msg " + (type || "");
  }
}

function dlBlob(content, mime, filename) {
  const blob = new Blob([content], { type: mime });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
}

function chartOpts({ stacked = false } = {}) {
  return {
    responsive: true,
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: "#1a1a1e",
        borderColor: "rgba(255,255,255,0.1)",
        borderWidth: 1,
        titleColor: "#e8e8ec",
        bodyColor: "#8a8a96",
        titleFont: { family: "'Space Mono',monospace", size: 11 },
        bodyFont: { family: "'DM Sans',sans-serif", size: 12 },
      },
    },
    scales: {
      x: {
        stacked,
        grid: { color: "rgba(255,255,255,0.04)" },
        ticks: {
          color: "#555560",
          font: { family: "'Space Mono',monospace", size: 10 },
        },
      },
      y: {
        stacked,
        grid: { color: "rgba(255,255,255,0.04)" },
        ticks: {
          color: "#555560",
          font: { family: "'Space Mono',monospace", size: 10 },
        },
      },
    },
  };
}

/* ═══════════════════════════════════════════════════════════════════════
   SERVER-SENT EVENTS — replaces all polling
   One persistent connection. Backend pushes attendance events instantly.
═══════════════════════════════════════════════════════════════════════ */
let _sse = null;

function connectSSE() {
  if (_sse) _sse.close();

  _sse = new EventSource(`${API}/events`);

  _sse.onmessage = (e) => {
    try {
      const event = JSON.parse(e.data);
      if (event.type === "attendance") {
        handleAttendanceEvent(event);
      }
    } catch {}
  };

  _sse.onerror = () => {
    // Auto-reconnect after 5s if connection drops
    setTimeout(connectSSE, 5000);
  };
}

function handleAttendanceEvent(event) {
  // 1. Update the live log panel on Recognize page
  const liveLog = document.getElementById("tSessionLog");
  if (liveLog) {
    const item = document.createElement("div");
    item.className = "log-item ok";
    item.innerHTML = `<span class="log-name">${event.name}</span>
                      <span class="log-conf">${event.confidence}% · ${event.time}</span>`;
    liveLog.prepend(item);
    // Keep log tidy — max 20 items
    while (liveLog.children.length > 20) liveLog.removeChild(liveLog.lastChild);
  }

  // 2. Update recognize result card
  setRecogResult(event.name, `${event.confidence}% confidence`, "present");

  // 3. Show toast notification
  toast(`✓ ${event.name} marked present`);

  // 4. If dashboard is visible, refresh its table live
  const dashPage = document.getElementById("page-dashboard");
  if (dashPage && dashPage.classList.contains("active")) {
    loadDashboard();
  }

  // 5. If attendance page is visible, refresh it live
  const attPage = document.getElementById("page-attendance");
  if (attPage && attPage.classList.contains("active")) {
    loadAttendance();
  }
}

// Start SSE as soon as user logs in (called from showApp)
const _originalShowApp = showApp;
// Override showApp to also start SSE
window.showApp = function () {
  _originalShowApp();
  connectSSE();
};

/* ═══════════════════════════════════════════════════════════════════════
   WEBCAM ENROLL — capture frames and send as JSON (no files, no disk)
   The browser captures frames from getUserMedia, converts each to
   base64, then POSTs them all as a JSON array to /api/enroll.
   Backend processes everything in memory — nothing written to disk.
═══════════════════════════════════════════════════════════════════════ */

// Override the original enrollStudent to support JSON mode (webcam)
window.enrollStudent = async function () {
  const sid = document.getElementById("eId").value.trim();
  const name = document.getElementById("eName").value.trim();
  const faculty_id = document.getElementById("eFaculty").value.trim();
  const sem = document.getElementById("eSem")?.value.trim() || "";
  const email = document.getElementById("eEmail").value.trim();
  const phone = document.getElementById("ePhone").value.trim();

  if (!sid || !name || !faculty_id || !sem || !email) {
    setMsg("enrollMsg", "All fields are required.", "err");
    return;
  }

  const hasFiles = selectedFiles.length > 0;
  const hasWebcam = webcamFiles.length > 0;
  if (!hasFiles && !hasWebcam) {
    setMsg("enrollMsg", "Upload images or capture via webcam.", "err");
    return;
  }

  const progress = document.getElementById("enrollProgress");
  const bar = document.getElementById("enrollBar");
  progress.style.display = "block";
  bar.style.width = "5%";
  setMsg("enrollMsg", "Processing faces…", "");

  try {
    let response, data;

    if (hasWebcam && !hasFiles) {
      // ── JSON mode: webcam frames as base64 array ──────────────────────
      bar.style.width = "30%";
      setMsg("enrollMsg", "Encoding webcam frames…", "");

      // Convert File objects (webcam blobs) to base64 strings
      const frames = await Promise.all(
        webcamFiles.map(
          (f) =>
            new Promise((res) => {
              const reader = new FileReader();
              reader.onload = (e) => res(e.target.result.split(",")[1]);
              reader.readAsDataURL(f);
            }),
        ),
      );

      bar.style.width = "60%";
      setMsg("enrollMsg", "Sending to server for embedding…", "");

      response = await api("/enroll", {
        method: "POST",
        json: {
          student_id: sid,
          full_name: name,
          faculty_id: faculty_id ? parseInt(faculty_id) : null,
          semester: sem || null,
          email: email || null,
          phone: phone || null,
          frames,
        },
      });
    } else {
      // ── FormData mode: uploaded image files ───────────────────────────
      bar.style.width = "40%";
      setMsg("enrollMsg", "Uploading images and generating embeddings…", "");

      const form = new FormData();
      form.append("student_id", sid);
      form.append("full_name", name);
      if (faculty_id) form.append("faculty_id", faculty_id);
      if (sem) form.append("semester", sem);
      if (email) form.append("email", email);
      if (phone) form.append("phone", phone);
      selectedFiles.forEach((f) => form.append("images", f));

      response = await api("/enroll", { method: "POST", body: form });
    }

    data = await response.json();
    bar.style.width = "100%";
    setTimeout(() => {
      progress.style.display = "none";
      bar.style.width = "0%";
    }, 600);

    if (!response.ok) {
      setMsg("enrollMsg", data.error || "Enrollment failed.", "err");
      return;
    }

    setMsg(
      "enrollMsg",
      `✓ ${name} enrolled. ${data.samples} samples processed.` +
        (data.is_update ? " (Embeddings updated)" : ""),
      "ok",
    );
    toast(`${name} enrolled successfully`);

    // Clean up
    stopWebcam();
    selectedFiles = [];
    webcamFiles = [];
    const filePreview = document.getElementById("filePreview") || null;
    if (filePreview) filePreview.innerHTML = "";
    ["eId", "eName", "eFaculty", "eSem", "eEmail", "ePhone"].forEach(
      (id) => { const el = document.getElementById(id); if (el) el.value = ""; },
    );
  } catch (e) {
    setMsg("enrollMsg", "Error: " + e.message, "err");
    progress.style.display = "none";
    bar.style.width = "0%";
  }
};

/* ═══════════════════════════════════════════════════════════════════════
   AUTO CAPTURE ENGINE
   Architecture:
   - getUserMedia() opens webcam into <video id="captureCam">
   - A requestAnimationFrame loop reads frames from video into a hidden canvas
   - Every 400ms it POSTs the frame to /api/capture/validate-frame
   - Backend returns { face_detected, quality{blur,brightness,overall,passed}, pose }
   - If quality passes AND the current required pose matches → capture the frame
   - Pose sequence: front(20) → left(10) → right(10) → up(10) → down(10) = 60 total
   - Canvas overlay draws: face oval guide, alignment reticle, quality ring
═══════════════════════════════════════════════════════════════════════ */

const POSE_SEQUENCE = [
  {
    pose: "front",
    target: 20,
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
const TOTAL_TARGET = 60;

let captureStream = null;
let captureAF = null; // requestAnimationFrame handle
let captureFrames = []; // collected base64 frames
let poseCaptureCounts = { front: 0, left: 0, right: 0, up: 0, down: 0 };
let captureActive = false;
let _captureComplete = false; // guard: blocks frames after all 60 collected
let _validateInFlight = false; // prevents concurrent validate calls
let lastValidateTime = 0;
let currentQuality = null;
let currentPoseHint = "front";
let captureMethod = "auto"; // "auto" | "upload"
let _lastRequiredPose = null; // tracks pose step changes for smoothing reset

function setCaptureMethod(method, btn) {
  captureMethod = method;
  document
    .querySelectorAll(".cmtab")
    .forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  document.getElementById("epanel-1").style.display =
    method === "upload" ? "block" : "none";
  document.getElementById("epanel-2").style.display =
    method === "auto" ? "flex" : "none";
  if (method === "upload") {
    stopAutoCapture();
  }
}

// ── Start guided capture ──────────────────────────────────────────────
async function startAutoCapture() {
  const sid = document.getElementById("eId").value.trim();
  const name = document.getElementById("eName").value.trim();
  const faculty_id = document.getElementById("eFaculty").value.trim();
  const sem = document.getElementById("eSem")?.value.trim() || "";
  const email = document.getElementById("eEmail").value.trim();
  const phone = document.getElementById("ePhone").value.trim();
  if (!sid || !name || !faculty_id || !sem || !email ){
    toast("All fields are required before capturing", "err");
    return;
  }

  try {
    captureStream = await navigator.mediaDevices.getUserMedia({
      video: {
        width: { ideal: 640 },
        height: { ideal: 480 },
        facingMode: "user",
      },
    });
    const video = document.getElementById("captureCam");
    video.srcObject = captureStream;
    await new Promise((res) => (video.onloadedmetadata = res));
    video.play();

    document.getElementById("capturePlaceholder").style.display = "none";
    document.getElementById("startCaptureBtn").style.display = "none";
    document.getElementById("stopCaptureBtn").style.display = "inline-flex";
    document.getElementById("resetCaptureBtn").style.display = "inline-flex";

    captureFrames = [];
    poseCaptureCounts = { front: 0, left: 0, right: 0, up: 0, down: 0 };
    captureActive = true;
    _captureComplete = false;
    _validateInFlight = false;
    _lastRequiredPose = null;

    _updatePosePips();
    _runCaptureLoop();
  } catch (e) {
    toast("Camera access denied — check browser permissions", "err");
    console.error(e);
  }
}

function stopAutoCapture() {
  captureActive = false;
  if (captureAF) {
    cancelAnimationFrame(captureAF);
    captureAF = null;
  }
  if (captureStream) {
    captureStream.getTracks().forEach((t) => t.stop());
    captureStream = null;
  }
  document.getElementById("captureCam").srcObject = null;
  document.getElementById("startCaptureBtn").style.display = "inline-flex";
  document.getElementById("stopCaptureBtn").style.display = "none";
}

function resetCapture() {
  stopAutoCapture();
  captureFrames = [];
  poseCaptureCounts = { front: 0, left: 0, right: 0, up: 0, down: 0 };
  _captureComplete = false;
  _validateInFlight = false;
  _lastRequiredPose = null;
  document.getElementById("captureCount").textContent = "0";
  document.getElementById("captureInstruction").textContent =
    "Fill in student info, then click Start Capture";
  document.getElementById("qualityFill").style.width = "0%";
  document.getElementById("qualityVal").textContent = "—";
  document.getElementById("poseLabel").textContent = "—";
  document.getElementById("capturePlaceholder").style.display = "flex";
  document.getElementById("resetCaptureBtn").style.display = "none";
  _updatePosePips();
  setMsg("enrollMsg", "", "");
}

// ── Frame loop ────────────────────────────────────────────────────────
function _runCaptureLoop() {
  const video = document.getElementById("captureCam");
  const overlay = document.getElementById("captureOverlay");
  const ctx = overlay.getContext("2d");

  function loop(ts) {
    if (!captureActive) return;

    // Match overlay size to video
    overlay.width = video.videoWidth || 640;
    overlay.height = video.videoHeight || 480;

    _drawOverlay(ctx, overlay.width, overlay.height);

    // Validate at most every 400ms (avoid hammering backend)
    if (ts - lastValidateTime >= 400) {
      lastValidateTime = ts;
      _validateAndCapture(video, overlay.width, overlay.height);
    }

    captureAF = requestAnimationFrame(loop);
  }
  captureAF = requestAnimationFrame(loop);
}

// ── Capture one frame, send to backend for quality check ─────────────
async function _validateAndCapture(video, w, h) {
  // Guard 1: don't start a new validation while one is in flight
  if (_validateInFlight || _captureComplete) return;
  _validateInFlight = true;

  try {
    // Grab frame from video into a temporary canvas
    const tmp = document.createElement("canvas");
    tmp.width = w;
    tmp.height = h;
    tmp.getContext("2d").drawImage(video, 0, 0, w, h);
    const b64 = tmp.toDataURL("image/jpeg", 0.85).split(",")[1];

    // Detect pose step change → tell backend to reset its smoothing buffer
    const currentStepNow = _getCurrentPoseStep();
    const resetPose =
      currentStepNow && currentStepNow.pose !== _lastRequiredPose;
    if (resetPose)
      _lastRequiredPose = currentStepNow ? currentStepNow.pose : null;

    const r = await api("/capture/validate-frame", {
      method: "POST",
      json: { image: b64, reset_pose: resetPose },
    });
    if (!r || _captureComplete) return;
    const d = await r.json();

    currentQuality = d.quality;
    currentPoseHint = d.pose || "front";

    // Update quality bar
    const q = d.quality?.overall ?? 0;
    const qFill = document.getElementById("qualityFill");
    if (qFill) {
      qFill.style.width = q + "%";
      qFill.style.background =
        q >= 70 ? "var(--green)" : q >= 45 ? "var(--amber)" : "var(--red)";
    }
    const qualityValEl = document.getElementById("qualityVal");
    if (qualityValEl) qualityValEl.textContent = q + "%";
    const poseLabelEl = document.getElementById("poseLabel");
    if (poseLabelEl) {
      // Show debug info (raw pose before smoothing) if backend sends it
      const debugSuffix =
        d.debug && d.debug.raw_pose && d.debug.raw_pose !== currentPoseHint
          ? ` (raw:${d.debug.raw_pose})`
          : "";
      poseLabelEl.textContent = d.face_detected
        ? currentPoseHint + debugSuffix
        : "No face";
    }

    if (!d.face_detected || !d.quality?.passed) {
      _setInstruction(
        !d.face_detected
          ? "⚠ No face detected — move into frame"
          : d.quality.blur_score < 30
            ? "⚠ Image is blurry — hold still"
            : d.quality.brightness < 40
              ? "⚠ Too dark — improve lighting"
              : "⚠ Quality too low — adjust position",
      );
      return;
    }

    // Guard 2: re-check complete flag after async wait
    if (_captureComplete) return;

    const currentStep = _getCurrentPoseStep();
    if (!currentStep) {
      // Shouldn't happen, but if all steps done just trigger complete
      if (!_captureComplete) _onCaptureComplete();
      return;
    }

    const requiredPose = currentStep.pose;
    // User-perspective pose matching — "front" also accepts slight angles
    const poseMatches =
      currentPoseHint === requiredPose ||
      (requiredPose === "front" && currentPoseHint === "front");

    if (poseMatches) {
      // Guard 3: don't exceed per-step target (prevents overshoot)
      const alreadyCount = poseCaptureCounts[requiredPose] || 0;
      if (alreadyCount >= currentStep.target) return;

      // ✓ Good frame — accept it
      captureFrames.push(b64);
      poseCaptureCounts[requiredPose] = alreadyCount + 1;

      const total = Object.values(poseCaptureCounts).reduce((a, b) => a + b, 0);
      const countEl = document.getElementById("captureCount");
      if (countEl) countEl.textContent = total; // new design: shows just number, sep and total are sibling spans

      _setInstruction(`✓ ${total} frames — ${currentStep.instruction}`);
      _updatePosePips();

      const justFinishedPose =
        poseCaptureCounts[requiredPose] >= currentStep.target;
      if (justFinishedPose) {
        // Guard 4: check total AFTER this addition
        if (total >= TOTAL_TARGET) {
          _captureComplete = true; // set BEFORE calling complete to block concurrent calls
          _onCaptureComplete();
        } else {
          // Use _getCurrentPoseStep() — it returns the FIRST step still incomplete.
          // After incrementing requiredPose to its target, this is correctly the
          // NEXT pose to collect (e.g. after front→left, it returns left).
          // _getNextPoseStep() was wrong here: it returned the step AFTER left (right)
          // because left had just become the "current" incomplete step.
          const nextStep = _getCurrentPoseStep();
          if (nextStep) {
            _soundDing();
            // Speak the next instruction with a short delay so the ding plays first
            setTimeout(
              () =>
                _speakInstruction(
                  nextStep.voiceInstruction || nextStep.instruction,
                  true,
                ),
              300,
            );
            _setInstruction(
              `✓ ${requiredPose.toUpperCase()} done!  Now: ${nextStep.instruction}`,
            );
          } else if (total >= TOTAL_TARGET) {
            _captureComplete = true;
            _onCaptureComplete();
          }
        }
      }
    } else {
      // Wrong pose — show direction hint WITH what the system currently sees
      // so the user knows the camera is working and what to adjust
      const seenLabel =
        currentPoseHint && currentPoseHint !== requiredPose
          ? ` (seeing: ${currentPoseHint})`
          : "";
      _setInstruction(currentStep.instruction + seenLabel);
    }
  } catch (e) {
    // Network hiccup — silently continue
  } finally {
    _validateInFlight = false; // always release lock
  }
}

// Helper: safe instruction setter (null-checked)
function _setInstruction(text) {
  const el = document.getElementById("captureInstruction");
  if (el) el.textContent = text;
}

function _getCurrentPoseStep() {
  for (const step of POSE_SEQUENCE) {
    if ((poseCaptureCounts[step.pose] || 0) < step.target) return step;
  }
  return null; // all complete
}

function _getNextPoseStep() {
  // Return the first step that still needs frames, AFTER the current one.
  // "current" = the first step not yet complete.
  let foundCurrent = false;
  for (const step of POSE_SEQUENCE) {
    const done = (poseCaptureCounts[step.pose] || 0) >= step.target;
    if (!foundCurrent && !done) {
      // This IS the current incomplete step — skip it, look for the next
      foundCurrent = true;
      continue;
    }
    if (foundCurrent && !done) {
      return step;
    }
  }
  return null; // no more steps after current
}

function _updatePosePips() {
  const currentPose = _getCurrentPoseStep()?.pose;
  for (const step of POSE_SEQUENCE) {
    const pip = document.getElementById(`pip-${step.pose}`);
    if (!pip) continue;
    const count = poseCaptureCounts[step.pose] || 0;
    const isDone = count >= step.target;
    const isCurrent = currentPose === step.pose;
    pip.classList.toggle("done", isDone);
    pip.classList.toggle("active", isCurrent && !isDone);
  }

  // Update per-pose progress list
  const listEl = document.getElementById("poseProgress");
  if (!listEl) return;
  listEl.innerHTML = POSE_SEQUENCE.map((step, idx) => {
    const count = poseCaptureCounts[step.pose] || 0;
    const isDone = count >= step.target;
    const isCurrent = currentPose === step.pose;
    const pct = Math.min(100, (count / step.target) * 100);
    const label = step.pose.charAt(0).toUpperCase() + step.pose.slice(1);
    const fillColor = isDone
      ? "var(--green)"
      : isCurrent
        ? "var(--amber)"
        : "var(--text3)";
    const badgeContent = isDone ? "✓" : String(idx + 1);
    const rowClass = isDone ? "done" : isCurrent ? "active" : "";
    return `<div class="pp-row ${rowClass}">
      <span class="pp-badge">${badgeContent}</span>
      <div class="pp-info">
        <div class="pp-top">
          <span class="pp-name">${label}</span>
          <span class="pp-count">${count}/${step.target}</span>
        </div>
        <div class="pp-bar"><div class="pp-fill" style="width:${pct}%;background:${fillColor}"></div></div>
      </div>
    </div>`;
  }).join("");
}

function _onCaptureComplete() {
  stopAutoCapture();
  _setInstruction(
    `✅ ${captureFrames.length} frames collected! Click "Review & Enroll" to continue.`,
  );
  document.getElementById("resetCaptureBtn").style.display = "inline-flex";
  toast(`${captureFrames.length} frames captured — ready to enroll`);
}

// ── Canvas overlay renderer ───────────────────────────────────────────
function _drawOverlay(ctx, w, h) {
  ctx.clearRect(0, 0, w, h);

  const cx = w / 2,
    cy = h / 2;
  const rx = w * 0.18,
    ry = h * 0.26;
  const currentStep = _getCurrentPoseStep();
  const totalCaptured = Object.values(poseCaptureCounts).reduce(
    (a, b) => a + b,
    0,
  );
  const pct = totalCaptured / TOTAL_TARGET;

  // Semi-transparent dark vignette outside the oval
  ctx.save();
  ctx.fillStyle = "rgba(0,0,0,0.40)";
  ctx.fillRect(0, 0, w, h);
  ctx.globalCompositeOperation = "destination-out";
  ctx.beginPath();
  ctx.ellipse(cx, cy, rx + 8, ry + 8, 0, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();

  // Oval border — colour depends on quality
  const ovalColor =
    !currentPoseHint || currentPoseHint === ""
      ? "#555"
      : currentQuality?.passed
        ? `hsl(${120 * pct}, 80%, 55%)`
        : "#EF4444";

  ctx.beginPath();
  ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
  ctx.strokeStyle = ovalColor;
  ctx.lineWidth = 3;
  ctx.stroke();

  // Progress arc around the oval (green ring)
  if (totalCaptured > 0) {
    ctx.beginPath();
    ctx.ellipse(cx, cy, rx + 6, ry + 6, -Math.PI / 2, 0, Math.PI * 2 * pct);
    ctx.strokeStyle = "rgba(34,197,94,0.8)";
    ctx.lineWidth = 4;
    ctx.stroke();
  }

  // Crosshair / alignment guide — 4 small ticks at cardinal points of oval
  ctx.strokeStyle = "rgba(255,255,255,0.35)";
  ctx.lineWidth = 1.5;
  [
    [cx, cy - ry - 4, cx, cy - ry + 12],
    [cx, cy + ry - 12, cx, cy + ry + 4],
    [cx - rx - 4, cy, cx - rx + 12, cy],
    [cx + rx - 12, cy, cx + rx + 4, cy],
  ].forEach(([x1, y1, x2, y2]) => {
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
  });

  // Pose direction arrow — drawn in USER perspective space
  // The video element is CSS-mirrored (scaleX(-1)), so canvas left = user's right.
  // We flip horizontal arrows so they point correctly from the user's viewpoint.
  if (currentStep && captureStream) {
    // User's left = canvas right; user's right = canvas left (mirrored feed)
    const arrows = {
      left: "←", // user turns left → arrow points left in their view
      right: "→", // user turns right → arrow points right in their view
      up: "↑",
      down: "↓",
      front: "",
    };
    const arrow = arrows[currentStep.pose] || "";
    if (arrow) {
      // Position arrows OUTSIDE the oval, at the edge toward which user should turn
      // Because canvas is mirrored: user's left edge = canvas RIGHT side
      const ax =
        currentStep.pose === "left"
          ? cx + rx + 24 // user's left = canvas right
          : currentStep.pose === "right"
            ? cx - rx - 24 // user's right = canvas left
            : cx;
      const ay =
        currentStep.pose === "up"
          ? cy - ry - 22
          : currentStep.pose === "down"
            ? cy + ry + 26
            : cy;

      // Draw a rounded pill behind the arrow for readability
      ctx.save();
      ctx.fillStyle = "rgba(245,158,11,0.18)";
      ctx.beginPath();
      ctx.roundRect
        ? ctx.roundRect(ax - 20, ay - 16, 40, 32, 8)
        : ctx.rect(ax - 20, ay - 16, 40, 32);
      ctx.fill();
      ctx.restore();

      ctx.font = "bold 26px Arial";
      ctx.fillStyle = "rgba(245,158,11,0.95)";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(arrow, ax, ay);
    }
  }
}

/* ═══════════════════════════════════════════════════════════════════════
   ENROLL OVERRIDE
   Replaces the previous enrollStudent() — now handles both auto-capture
   frames and uploaded files.
═══════════════════════════════════════════════════════════════════════ */
window.enrollStudent = async function () {
  const sid = document.getElementById("eId").value.trim();
  const name = document.getElementById("eName").value.trim();
  const faculty_id = document.getElementById("eFaculty").value.trim();
  const sem = document.getElementById("eSem")?.value.trim() || "";
  const email = document.getElementById("eEmail").value.trim();
  const phone = document.getElementById("ePhone").value.trim();

  if (!sid || !name || !faculty_id || !sem || !email ) {
    setMsg("enrollMsg", "All fields are required.", "err");
    return;
  }

  const isAuto = captureMethod === "auto";
  const frames = isAuto ? captureFrames : [];
  const files = isAuto ? [] : selectedFiles;

  if (isAuto && frames.length < 5) {
    setMsg(
      "enrollMsg",
      "Not enough frames captured. Minimum 5 required.",
      "err",
    );
    return;
  }
  if (!isAuto && files.length === 0) {
    setMsg("enrollMsg", "Please upload at least one image.", "err");
    return;
  }

  const progress = document.getElementById("enrollProgress");
  const bar = document.getElementById("enrollBar");
  progress.style.display = "block";
  bar.style.width = "5%";
  setMsg("enrollMsg", "Processing faces and generating embeddings…", "");

  try {
    let response;
    if (isAuto) {
      bar.style.width = "40%";
      response = await api("/enroll", {
        method: "POST",
        json: {
          student_id: sid,
          full_name: name,
          faculty_id: faculty_id ? parseInt(faculty_id) : null,
          email: email || null,
          phone: phone || null,
          semester: sem || null,
          frames,
        },
      });
    } else {
      bar.style.width = "40%";
      const form = new FormData();
      form.append("student_id", sid);
      form.append("full_name", name);
      if (faculty_id) form.append("faculty_id", faculty_id);
      if (sem) form.append("semester", sem);
      if (email) form.append("email", email);
      if (phone) form.append("phone", phone);
      files.forEach((f) => form.append("images", f));
      response = await api("/enroll", { method: "POST", body: form });
    }

    const d = await response.json();
    bar.style.width = "100%";
    setTimeout(() => {
      progress.style.display = "none";
      bar.style.width = "0%";
    }, 600);

    if (!response.ok) {
      setMsg("enrollMsg", d.error || "Enrollment failed.", "err");
      return;
    }

    setMsg(
      "enrollMsg",
      `✓ ${name} enrolled. ${d.samples} samples processed.` +
        (d.is_update ? " (Embeddings updated)" : ""),
      "ok",
    );
    toast(`${name} enrolled successfully`);

    // Reset
    captureFrames = [];
    selectedFiles = [];
    webcamFiles = [];
    (document.getElementById("filePreview") || null) &&
      ((document.getElementById("filePreview") || null || {}).innerHTML = "");
    resetCapture();
    ["eId", "eName", "eFaculty", "eSem", "eEmail", "ePhone"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.value = "";
    });
  } catch (e) {
    setMsg("enrollMsg", "Error: " + e.message, "err");
    progress.style.display = "none";
    bar.style.width = "0%";
  }
};

/* ═══════════════════════════════════════════════════════════════════════
   FULL STUDENT EDIT MODAL
═══════════════════════════════════════════════════════════════════════ */
let _editingSid = null;

window.openEditModal = async function (sid) {
  _editingSid = sid;
  document.getElementById("editModal").style.display = "flex";
  document.body.style.overflow = "hidden";
  switchModalTab("profile");

  const r = await api(`/students/${sid}`);
  if (!r) return;
  const s = await r.json();

  const titleEl = document.getElementById("editModalTitle");
  if (titleEl) titleEl.textContent = `Edit — ${s.full_name}`;

  document.getElementById("editSid").value = s.student_id;
  const dispEl = document.getElementById("editSidDisplay");
  if (dispEl) dispEl.value = s.student_id;
  document.getElementById("editName").value = s.full_name;
  document.getElementById("editDept").value = s.department || "";
  document.getElementById("editSem").value = s.semester || "";
  document.getElementById("editEmail").value = s.email || "";
  document.getElementById("editPhone").value = s.phone || "";
  document.getElementById("editStatus").value = s.status || "active";

  // Populate department datalist
  const dl = document.getElementById("editDeptList");
  if (dl) {
    dl.innerHTML = "";
    _ensureDefaultData();
    getFaculties().forEach((f) => {
      const opt = document.createElement("option");
      opt.value = f.name;
      dl.appendChild(opt);
    });
  }
  setMsg("editModalErr", "", "");
};

function closeEditModal(event) {
  if (event && event.target !== document.getElementById("editModal")) return;
  _closeEditModal();
}
function _closeEditModal() {
  const el = document.getElementById("editModal");
  if (el) el.style.display = "none";
  document.body.style.overflow = "";
  _editingSid = null;
}
// Also close on Escape
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    _closeEditModal();
    document.querySelectorAll(".modal-overlay").forEach((m) => {
      if (m.style.display === "flex") {
        m.style.display = "none";
        document.body.style.overflow = "";
      }
    });
  }
});

function switchModalTab(tab, btn) {
  document
    .querySelectorAll(".mtab")
    .forEach((b) => b.classList.remove("active"));
  if (btn) btn.classList.add("active");
  else
    document
      .querySelectorAll(".mtab")
      [
        ["profile", "attendance", "actlog"].indexOf(tab)
      ]?.classList.add("active");
  ["profile", "attendance", "actlog"].forEach((t) => {
    const el = document.getElementById(`mtab-${t}`);
    if (el) el.style.display = t === tab ? "block" : "none";
  });
  if (tab === "actlog" && _editingSid) _loadActLog(_editingSid);
  if (tab === "attendance" && _editingSid) _loadAttendanceTab(_editingSid);
}

async function _loadAttendanceTab(sid) {
  const el = document.getElementById("attEditList");
  if (!el) return;
  el.innerHTML = `<p class="muted">Loading…</p>`;
  try {
    const r = await api(`/students/${sid}`);
    if (!r) return;
    const sd = await r.json();
    _renderAttEditList(sd.attendance || []);
  } catch {
    el.innerHTML = `<p class="muted err">Failed to load attendance.</p>`;
  }
}

async function saveStudentEdit() {
  if (!_editingSid) return;
  const fields = {
    full_name: document.getElementById("editName").value.trim(),
    department: document.getElementById("editDept").value.trim() || null,
    semester: document.getElementById("editSem").value.trim() || null,
    email: document.getElementById("editEmail").value.trim() || null,
    phone: document.getElementById("editPhone").value.trim() || null,
    status: document.getElementById("editStatus").value,
  };
  if (!fields.full_name) {
    setMsg("editModalErr", "Name is required.", "err");
    return;
  }

  const r = await api(`/students/${_editingSid}`, {
    method: "PUT",
    json: fields,
  });
  if (!r) return;
  const d = await r.json();
  if (!r.ok) {
    setMsg("editModalErr", d.error || "Update failed.", "err");
    return;
  }

  setMsg("editModalErr", "✓ Profile updated successfully.", "ok");
  toast("Student updated");
  // Refresh whichever page is visible
  const activePage = document.querySelector(".page.active")?.id;
  if (activePage === "page-students") searchStudents();
  if (activePage === "page-dashboard") loadDashboard();
  if (activePage === "page-profile") viewProfile(_editingSid);
}

async function confirmDeleteStudent() {
  if (!_editingSid) return;
  if (
    !confirm(
      `Permanently delete student ${_editingSid}? This cannot be undone.`,
    )
  )
    return;
  const r = await api(`/students/${_editingSid}`, { method: "DELETE" });
  if (!r) return;
  _closeEditModal();
  toast("Student deleted");
  navigate("students");
}

// ── Attendance override in modal ──────────────────────────────────────
function _renderAttEditList(records) {
  const el = document.getElementById("attEditList") || null;
  if (!records || !records.length) {
    el.innerHTML = `<p class="muted" style="padding:.5rem 0">No attendance records yet.</p>`;
    return;
  }
  el.innerHTML = records
    .map(
      (r) => `
    <div class="att-edit-row">
      <span class="att-edit-date">${r.date}</span>
      <span class="${r.status === "Present" ? "badge-present" : "badge-absent"}">${r.status}</span>
      <span class="att-edit-time">${r.time || "—"}</span>
      <span class="att-edit-note">${r.note || ""}</span>
      <button class="att-edit-del" onclick="deleteAttRecord('${r.date}')" title="Remove">✕</button>
    </div>`,
    )
    .join("");
}

async function saveAttendanceEdit() {
  if (!_editingSid) return;
  const date = (document.getElementById("attEditDate") || null || {}).value;
  const status = (document.getElementById("attEditStatus") || null || {}).value;
  const note = (
    document.getElementById("attEditNote") ||
    null ||
    {}
  ).value.trim();
  if (!date) {
    setMsg("attEditMsg", "Select a date.", "err");
    return;
  }

  const r = await api(`/attendance/${_editingSid}/${date}`, {
    method: "PUT",
    json: { status, note },
  });
  if (!r) return;
  const d = await r.json();
  if (!r.ok) {
    setMsg("attEditMsg", d.error || "Update failed.", "err");
    return;
  }

  setMsg("attEditMsg", `✓ Attendance set to ${status} for ${date}.`, "ok");
  (document.getElementById("attEditNote") || null || {}).value = "";
  // Reload student data to refresh the list
  const sr = await api(`/students/${_editingSid}`);
  const sd = await sr.json();
  _renderAttEditList(sd.attendance || []);
}

async function deleteAttRecord(attDate) {
  if (!confirm(`Remove attendance record for ${attDate}?`)) return;
  const r = await api(`/attendance/${_editingSid}/${attDate}`, {
    method: "DELETE",
  });
  if (!r) return;
  toast("Record removed");
  const sr = await api(`/students/${_editingSid}`);
  const sd = await sr.json();
  _renderAttEditList(sd.attendance || []);
}

// ── Activity log in modal ─────────────────────────────────────────────
async function _loadActLog(sid) {
  const el = document.getElementById("actLogList") || null;
  el.innerHTML = `<p class="muted">Loading…</p>`;
  try {
    const r = await api(`/activity-logs?target_id=${sid}&limit=30`);
    const d = await r.json();
    const logs = d.logs || [];
    if (!logs.length) {
      el.innerHTML = `<p class="muted">No activity recorded yet.</p>`;
      return;
    }
    el.innerHTML = logs
      .map(
        (l) => `
      <div class="act-log-row">
        <span class="act-ts">${l.logged_at?.slice(0, 16) || ""}</span>
        <span class="act-who">${l.admin_user}</span>
        <span class="act-action">&nbsp;·&nbsp;${l.action.replace(/_/g, " ")}</span>
        ${l.detail ? `<span class="act-detail">&nbsp;— ${l.detail}</span>` : ""}
      </div>`,
      )
      .join("");
  } catch {
    el.innerHTML = `<p class="muted err">Failed to load.</p>`;
  }
}

/* ═══════════════════════════════════════════════════════════════════════
   SETTINGS PAGE — add email status + test button
═══════════════════════════════════════════════════════════════════════ */
const _origLoadSettings =
  typeof loadSettings === "function" ? loadSettings : async () => {};
window.loadSettings = async function () {
  await _origLoadSettings();
  try {
    const r = await api("/settings");
    const d = await r.json();
    // Inject email status into settings page if element exists
    let emailEl = document.getElementById("emailTestMsg");
    if (!emailEl) return;
    const on = d.email_enabled;
    emailEl.className = `email-status ${on ? "on" : "off"}`;
    emailEl.textContent = on ? "✓ Email enabled" : "✗ Email disabled";
    if (d.smtp_user)
      document.getElementById("emailTestMsg") &&
        ((document.getElementById("emailTestMsg") || {}).textContent =
          d.smtp_user);
  } catch {}
};

async function sendTestEmail() {
  const email =
    document.getElementById("testEmailAddr")?.value.trim() ||
    prompt("Enter email address to send test to:");
  if (!email) return;
  const r = await api("/email/test", { method: "POST", json: { email } });
  if (!r) return;
  const d = await r.json();
  if (r.ok) toast("Test email queued — check inbox in ~10s");
  else toast(d.error || "Failed", "err");
}

async function sendBulkAttendanceEmail() {
  if (
    !confirm(
      "Send today's attendance summary to all registered students with email addresses?",
    )
  )
    return;
  const r = await api("/email/send-attendance-summary", {
    method: "POST",
    json: { date: todayStr() },
  });
  if (!r) return;
  const d = await r.json();
  if (r.ok) {
    toast(`Attendance emails queued for ${d.date}`);
  } else {
    toast(d.error || "Failed", "err");
  }
}

/* ═══════════════════════════════════════════════════════════════════════
   ENROLL — Step navigation
═══════════════════════════════════════════════════════════════════════ */
function _setEnrollStep(n) {
  const titles = {
    1: "Student Profile",
    2: "Face Capture",
    3: "Review & Confirm",
  };
  [1, 2, 3].forEach((i) => {
    const panel = document.getElementById(`epanel-${i}`);
    const step = document.getElementById(`estep-${i}`);
    if (panel) panel.style.display = i === n ? "block" : "none";
    if (step) {
      step.classList.toggle("active", i === n);
      step.classList.toggle("done", i < n);
    }
  });
  const titleEl = document.getElementById("esbTitle");
  if (titleEl) titleEl.textContent = titles[n] || "";
}

function goToStep1() {
  _setEnrollStep(1);
}

function goToStep2() {
  const sid = document.getElementById("eId").value.trim();
  const name = document.getElementById("eName").value.trim();
  const faculty_id = document.getElementById("eFaculty").value.trim();
  const sem = document.getElementById("eSem")?.value.trim() || "";
  const email = document.getElementById("eEmail").value.trim();
  const phone = document.getElementById("ePhone").value.trim();
  if (!sid || !name || !faculty_id || !sem || !email ) {
    toast("All fields are required", "err");
    return;
  }
  _setEnrollStep(2);
  _updatePosePips(); // render progress list immediately when panel opens
}

function goToStep3() {
  // Build review grid
  const fields = [
    ["Student ID", document.getElementById("eId").value.trim()],
    ["Full Name", document.getElementById("eName").value.trim()],
    [
      "Faculty",
      document.getElementById("eFaculty").selectedOptions?.[0]?.text || "",
    ],
    ["Semester", document.getElementById("eSem")?.value.trim() || ""],
    ["Email", document.getElementById("eEmail").value.trim()],
    ["Phone", document.getElementById("ePhone").value.trim()],
  ];
  const grid = document.getElementById("reviewGrid");
  grid.innerHTML = fields
    .map(
      ([label, val]) => `
    <div class="rv-row">
      <span class="rv-label">${label}</span>
      <span class="rv-val ${val ? "" : "empty"}">${val || "—"}</span>
    </div>`,
    )
    .join("");

  // Capture summary badge
  const total = Object.values(poseCaptureCounts).reduce((a, b) => a + b, 0);
  const badge = document.getElementById("reviewCaptureBadge");
  if (total > 0) {
    badge.className = "review-capture-badge ready";
    badge.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="20,6 9,17 4,12"/></svg>
      ${total} frames captured via guided auto-capture`;
  } else {
    badge.className = "review-capture-badge warn";
    badge.innerHTML = `⚠ No face images yet — go back and capture`;
  }

  setMsg("enrollMsg", "", "");
  _setEnrollStep(3);
}

// Reset enroll page back to step 1 after successful enroll
const _origResetCapture =
  typeof resetCapture === "function" ? resetCapture : () => {};
window.resetEnrollPage = function () {
  _origResetCapture();
  _setEnrollStep(1);
  document.getElementById("reviewGrid") &&
    (document.getElementById("reviewGrid").innerHTML = "");
  setMsg("enrollMsg", "", "");
};

/* ═══════════════════════════════════════════════════════════════════════
   AUTO EMAIL on SSE attendance event
   When the SSE connection receives a new attendance mark,
   automatically queue an email — no manual trigger needed.
═══════════════════════════════════════════════════════════════════════ */
const _origHandleAttendanceEvent =
  typeof handleAttendanceEvent === "function"
    ? handleAttendanceEvent
    : () => {};

window.handleAttendanceEvent = function (event) {
  // Call original handler (updates UI panels, toast, etc.)
  _origHandleAttendanceEvent(event);

  // Auto-send email — fire and forget, no await
  // The backend already queues emails from camera stream,
  // but for image-upload recognition we also trigger from here
  // to ensure nothing is missed regardless of recognition path.
  if (event.student_id && event.student_id !== "TEST") {
    api(`/students/${event.student_id}`)
      .then((r) => r && r.json())
      .then((s) => {
        if (s && s.email) {
          // Email is already queued server-side by _mark_attendance_and_broadcast.
          // The frontend just confirms — no double send because backend deduplicates
          // on (student_id, subject, date) in email_log.
          console.log(
            `[email] attendance email queued for ${s.full_name} → ${s.email}`,
          );
        }
      })
      .catch(() => {});
  }
};

/* ═══════════════════════════════════════════════════════════════════════
   VOICE GUIDANCE & SOUND EFFECTS SYSTEM
   ─────────────────────────────────────────────────────────────────────
   Architecture:
   • Voice: Web Speech API (SpeechSynthesis) — built into every modern
     browser, no external library, no API key, works offline.
   • Sounds: Web Audio API (AudioContext) — synthesised tones, no files
     needed. Three distinct sounds:
       - "ding"  : single high tone  → pose step complete
       - "chime" : ascending 3-tone  → all capture complete / success
       - "tick"  : soft click        → each frame captured (subtle)
   • Debouncing: voice is throttled so the same phrase can't fire twice
     within 2.5 seconds (prevents spam during frame capture loop).
═══════════════════════════════════════════════════════════════════════ */

// ── Voice state ───────────────────────────────────────────────────────
let _voiceEnabled = true; // toggled by UI button
let _lastSpokenText = "";
let _lastSpokenTime = 0;
const VOICE_COOLDOWN_MS = 2500;

// Pick the best English voice available on this device
let _selectedVoice = null;
function _loadVoice() {
  if (!window.speechSynthesis) return;
  const pick = () => {
    const voices = speechSynthesis.getVoices();
    // Prefer: Samantha (macOS), Google UK English Female, any en-GB, en-US
    _selectedVoice =
      voices.find((v) => v.name === "Samantha") ||
      voices.find((v) => v.name.includes("Google UK English Female")) ||
      voices.find((v) => v.lang === "en-GB" && !v.name.includes("Male")) ||
      voices.find((v) => v.lang.startsWith("en")) ||
      voices[0] ||
      null;
  };
  pick();
  speechSynthesis.onvoiceschanged = pick; // fires asynchronously on Chrome
}
_loadVoice();

function _speakInstruction(text, force = false) {
  if (!_voiceEnabled || !window.speechSynthesis) return;
  const now = Date.now();
  if (
    !force &&
    text === _lastSpokenText &&
    now - _lastSpokenTime < VOICE_COOLDOWN_MS
  )
    return;
  _lastSpokenText = text;
  _lastSpokenTime = now;

  speechSynthesis.cancel(); // stop any currently speaking utterance
  const utt = new SpeechSynthesisUtterance(text);
  utt.voice = _selectedVoice;
  utt.rate = 0.92;
  utt.pitch = 1.0;
  utt.volume = 1.0;
  speechSynthesis.speak(utt);
}

// ── Web Audio sound effects ───────────────────────────────────────────
let _audioCtx = null;
function _getAudioCtx() {
  if (!_audioCtx) {
    _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  }
  // Resume if suspended (required after user gesture on some browsers)
  if (_audioCtx.state === "suspended") _audioCtx.resume();
  return _audioCtx;
}

function _playTone(
  freq,
  duration,
  type = "sine",
  gainVal = 0.35,
  startDelay = 0,
) {
  try {
    const ctx = _getAudioCtx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);

    osc.type = type;
    osc.frequency.setValueAtTime(freq, ctx.currentTime + startDelay);

    gain.gain.setValueAtTime(0, ctx.currentTime + startDelay);
    gain.gain.linearRampToValueAtTime(
      gainVal,
      ctx.currentTime + startDelay + 0.01,
    );
    gain.gain.exponentialRampToValueAtTime(
      0.001,
      ctx.currentTime + startDelay + duration,
    );

    osc.start(ctx.currentTime + startDelay);
    osc.stop(ctx.currentTime + startDelay + duration + 0.05);
  } catch {}
}

// Ding: single 880 Hz tone — pose step complete
function _soundDing() {
  _playTone(880, 0.22, "sine", 0.35);
}

// Chime: ascending C5-E5-G5 — all capture complete
function _soundChime() {
  _playTone(523, 0.18, "sine", 0.32, 0.0);
  _playTone(659, 0.18, "sine", 0.32, 0.18);
  _playTone(784, 0.28, "sine", 0.38, 0.36);
}

// Tick: very quiet soft click — per-frame feedback
function _soundTick() {
  _playTone(1200, 0.04, "triangle", 0.08);
}

// ── Wire sounds into capture events ──────────────────────────────────
// Override _validateAndCapture to inject sounds + first instruction
const _origValidateAndCapture = _validateAndCapture;

// We can't simply reassign async functions by name in the same scope,
// so we patch via the capture engine's integration points below.

// Announce the first instruction when capture starts
const _origStartAutoCapture = window.startAutoCapture || startAutoCapture;
window.startAutoCapture = async function () {
  await _origStartAutoCapture();
  if (captureActive) {
    // Small delay so voice starts after camera opens
    setTimeout(() => {
      _speakInstruction(
        "Face capture started. " +
          (POSE_SEQUENCE[0].voiceInstruction || POSE_SEQUENCE[0].instruction),
        true,
      );
    }, 800);
  }
};

// Patch _onCaptureComplete to play the chime + speak completion
const _origOnCaptureComplete = _onCaptureComplete;
window._onCaptureComplete = function () {
  _soundChime();
  setTimeout(() => {
    _speakInstruction(
      "Face capture complete! You may now proceed to enroll.",
      true,
    );
  }, 400);
  _origOnCaptureComplete();
};

// Patch the instruction display: intercept textContent changes
// to auto-speak them. We do this by wrapping the setter.
// The clean way is to call _speakInstruction explicitly at each update point.
// We patch _validateAndCapture since that's where instructions are set.

// Instruction auto-speak: hook into the per-frame logic
// Add a MutationObserver on the instruction element
window.addEventListener("load", () => {
  // Wait until after DOM is ready
  setTimeout(() => {
    const instrEl = document.getElementById("captureInstruction");
    if (!instrEl) return;

    let _lastObservedText = "";
    const observer = new MutationObserver(() => {
      const text = instrEl.textContent.trim();
      if (!text || text === _lastObservedText) return;
      _lastObservedText = text;

      // Speak warnings immediately (they start with ⚠)
      if (text.startsWith("⚠")) {
        _speakInstruction(text.replace("⚠", "").trim(), false);
        return;
      }

      // Speak pose-done transitions (✓ ... done!  Now: ...)
      // The transition message format is: "✓ POSE done!  Now: <next instruction>"
      if (text.includes("done!") && text.includes("Now:")) {
        // Sound is already played in _validateAndCapture — don't double-play.
        // Voice is also already queued with 300ms delay in _validateAndCapture.
        // Observer just needs to not interfere, so return early.
        return;
      }

      // Speak ongoing instruction (✓ N frames — <instruction>)
      // Format: "✓ 12 frames — Look straight at the camera"
      if (text.startsWith("✓") && text.includes("frames —")) {
        // Play a quiet tick per frame, speak the instruction every 10 frames
        const match = text.match(/(\d+) frames/);
        const n = match ? parseInt(match[1]) : 0;
        _soundTick();
        if (n > 0 && n % 10 === 0) {
          const instr = text.split("—")[1]?.trim();
          if (instr) _speakInstruction(`${n} frames. ${instr}`, false);
        }
        return;
      }

      // Completion
      if (
        text.includes("frames collected") ||
        text.includes("capture complete")
      ) {
        return; // handled by _onCaptureComplete patch above
      }
    });

    observer.observe(instrEl, {
      characterData: true,
      childList: true,
      subtree: true,
    });
  }, 500);
});

// ── Voice toggle button (injected into the capture panel) ─────────────
// We add a small toggle button to the capture status bar automatically.
window.addEventListener("load", () => {
  setTimeout(() => {
    const bar = document.querySelector(".capture-status-bar");
    if (!bar || document.getElementById("voiceToggleBtn") || null) return;

    const btn = document.createElement("button");
    btn.id = "voiceToggleBtn";
    btn.title = "Toggle voice guidance";
    btn.className = "csb-voice-btn active";
    btn.innerHTML = `
      <svg id="voiceIcon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <polygon points="11,5 6,9 2,9 2,15 6,15 11,19 11,5"/>
        <path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>
        <path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>
      </svg>`;
    btn.onclick = () => {
      _voiceEnabled = !_voiceEnabled;
      btn.classList.toggle("active", _voiceEnabled);
      btn.title = _voiceEnabled
        ? "Voice on — click to mute"
        : "Voice off — click to enable";
      if (_voiceEnabled) {
        _speakInstruction("Voice guidance enabled", true);
      } else {
        speechSynthesis.cancel();
      }
      // Update icon opacity
      btn.style.opacity = _voiceEnabled ? "1" : "0.35";
    };
    bar.appendChild(btn);
  }, 600);
});

/* ═══════════════════════════════════════════════════════════════════════
   MISSING IMPLEMENTATIONS — login role selector, modals, teachers,
   manage page, teacher panel pages, student portal
═══════════════════════════════════════════════════════════════════════ */

/* ── Login role selector ──────────────────────────────────────────────── */
let _loginRole = "admin";
function selectLoginRole(role) {
  _loginRole = role;
  document
    .querySelectorAll(".role-btn")
    .forEach((b) => b.classList.remove("active"));
  const rb = document.getElementById(`roleBtn-${role}`);
  if (rb) rb.classList.add("active");

  const userField = document.getElementById("field-username");
  const emailField = document.getElementById("field-email");
  const passField = document.getElementById("field-password");
  const hint = document.getElementById("loginHint");
  const emailLabel = document.getElementById("emailLabel");
  const emailInput = document.getElementById("loginEmail");

  if (role === "admin") {
    userField?.classList.remove("hidden");
    emailField?.classList.add("hidden");
    passField?.classList.remove("hidden");
    if (hint) hint.textContent = "Default: admin / admin123";
  } else if (role === "teacher") {
    userField?.classList.add("hidden");
    emailField?.classList.remove("hidden");
    passField?.classList.remove("hidden");
    if (emailLabel) emailLabel.textContent = "Email";
    if (emailInput) emailInput.placeholder = "teacher@college.edu";
    if (hint) hint.textContent = "Use your email and password set by admin";
  } else {
    // student
    userField?.classList.add("hidden");
    emailField?.classList.remove("hidden");
    passField?.classList.add("hidden");
    if (emailLabel) emailLabel.textContent = "Registered Email";
    if (emailInput) emailInput.placeholder = "student@college.edu";
    if (hint) hint.textContent = "Enter your registered email address";
  }
}

/* ── Generic modal close ──────────────────────────────────────────────── */
function closeModal(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.display = "none";
  el.classList.remove("open"); // timetable modal uses classList.add("open")
  document.body.style.overflow = "";
}

/* ── Alias functions for HTML-called names ────────────────────────────── */
function changeAdminPw() {
  changePw();
}
function filterAttendance() {
  filterActiveTable(document.getElementById("attSearch")?.value || "");
}
function exportAttCSV() {
  exportFacultyCSV();
}
function exportReport() {
  exportRange();
}

/* ══════════════════════════════════════════════════════════════════════
   LOCAL DATA STORE — faculties, subjects, time slots, teachers
   Stored in localStorage since the backend has no CRUD endpoints.
══════════════════════════════════════════════════════════════════════ */
const LS = {
  get(key) {
    try {
      return JSON.parse(localStorage.getItem(key) || "[]");
    } catch {
      return [];
    }
  },
  set(key, val) {
    localStorage.setItem(key, JSON.stringify(val));
  },
};
function _nextId(arr) {
  return arr.length ? Math.max(...arr.map((x) => x.id || 0)) + 1 : 1;
}

function getFaculties() {
  return LS.get("frs_faculties");
}
function setFaculties(arr) {
  LS.set("frs_faculties", arr);
}
function getSubjects() {
  return LS.get("frs_subjects");
}
function setSubjects(arr) {
  LS.set("frs_subjects", arr);
}
function getTimeSlots() {
  return LS.get("frs_timeslots");
}
function setTimeSlots(arr) {
  LS.set("frs_timeslots", arr);
}
function getTeachers() {
  return LS.get("frs_teachers");
}
function setTeachers(arr) {
  LS.set("frs_teachers", arr);
}

function _ensureDefaultData() {
  if (!getFaculties().length) {
    setFaculties([
      { id: 1, name: "BCA", code: "BCA" },
      { id: 2, name: "CSIT", code: "CSIT" },
      { id: 3, name: "BBM", code: "BBM" },
    ]);
  }
  if (!getTimeSlots().length) {
    setTimeSlots([
      { id: 1, label: "Period 1 (07:15–08:15)", start: "07:15", end: "08:15" },
      { id: 2, label: "Period 2 (08:15–09:15)", start: "08:15", end: "09:15" },
      { id: 3, label: "Period 3 (09:15–10:15)", start: "09:15", end: "10:15" },
      { id: 4, label: "Period 4 (11:00–12:00)", start: "11:00", end: "12:00" },
      { id: 5, label: "Period 5 (12:00–13:00)", start: "12:00", end: "13:00" },
    ]);
  }
}

/* ── Faculty dropdown populator (shared) ─────────────────────────────── */
let _cachedFaculties = [];

async function _loadFaculties() {
  try {
    const r = await api("/faculties");
    if (r && r.ok) {
      const data = await r.json();
      _cachedFaculties = data.faculties || [];
    }
  } catch (_) {}
  return _cachedFaculties;
}

async function _populateFacultyDropdowns() {
  const faculties = await _loadFaculties();

  const populate = (selId, keepName = false) => {
    const sel = document.getElementById(selId);
    if (!sel) return;
    const cur = sel.value;
    while (sel.options.length > 1) sel.remove(1);
    faculties.forEach((f) => {
      const o = document.createElement("option");
      o.value = keepName ? f.name : f.id;
      o.text = f.name;
      sel.add(o);
    });
    sel.value = cur;
  };

  populate("tmFaculty"); // teacher modal — value = faculty id
  populate("smFaculty"); // subject modal — value = faculty id
  populate("eFaculty"); // enrollment — value = faculty id (backend resolves code for department)
  populate("defFaculty"); // defaulter list filter
  populate("ttFacultyFilter"); // timetable filter
}

async function loadSubjectsForModal() {
  const faculty_id = document.getElementById("tmFaculty")?.value || "";
  const sel = document.getElementById("tmSubject");
  if (!sel) return;
  const cur = sel.value;
  while (sel.options.length > 1) sel.remove(1);
  try {
    const params = faculty_id
      ? `?faculty_id=${encodeURIComponent(faculty_id)}`
      : "";
    const r = await api(`/subjects${params}`);
    if (r && r.ok) {
      const data = await r.json();
      (data.subjects || []).forEach((s) => {
        const o = document.createElement("option");
        o.value = s.id;
        o.text = `${s.name} (${s.code})`;
        sel.add(o);
      });
    }
  } catch (_) {}
  sel.value = cur;
}

/* ══════════════════════════════════════════════════════════════════════
   MANAGE PAGE — Faculties
══════════════════════════════════════════════════════════════════════ */
function switchManageTab(tab, btn) {
  document
    .querySelectorAll(".sub-tab")
    .forEach((t) => t.classList.remove("active"));
  document
    .querySelectorAll(".sub-tab-panel")
    .forEach((p) => p.classList.remove("active"));
  if (btn) btn.classList.add("active");
  else {
    const tabs = ["faculties", "subjects", "timeslots", "timetable", "calendar"];
    const idx = tabs.indexOf(tab);
    document.querySelectorAll(".sub-tab")[idx]?.classList.add("active");
  }
  const panel = document.getElementById(`mtab-${tab}`);
  if (panel) panel.classList.add("active");

  if (tab === "faculties") loadFaculties();
  else if (tab === "subjects") {
    _populateFacSubjectFilters().then(() => loadSubjects());
  } else if (tab === "timeslots") loadTimeslots();
  else if (tab === "timetable") _populateTimetableFacultyFilter();
}

async function loadFaculties() {
  const tbody = document.getElementById("facultyTableBody");
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="3" style="text-align:center;color:var(--text3);padding:2rem">Loading…</td></tr>`;
  const faculties = await _loadFaculties(); // always fetches from API, updates _cachedFaculties
  if (!faculties.length) {
    tbody.innerHTML = `<tr><td colspan="3" style="text-align:center;color:var(--text3);padding:2rem">No faculties yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = faculties
    .map(
      (f) => `
      <tr>
        <td style="font-weight:500">${escapeHtml(f.name)}</td>
        <td style="font-family:var(--mono);font-size:11px;color:var(--text3)">${escapeHtml(f.code || "")}</td>
        <td>
          <button class="btn-secondary btn-sm" onclick="openFacultyModal(${f.id})">Edit</button>
          <button class="btn-danger btn-sm" style="margin-left:4px" onclick="deleteFaculty(${f.id})">Delete</button>
        </td>
      </tr>`,
    )
    .join("");
  _populateFacultyDropdowns();
}

function openFacultyModal(id) {
  // Read from _cachedFaculties (populated by _loadFaculties via API)
  const f = id ? _cachedFaculties.find((x) => x.id === id) : null;
  document.getElementById("facultyModalTitle").textContent = f
    ? "Edit Faculty"
    : "Add Faculty";
  document.getElementById("fmId").value = f ? f.id : "";
  document.getElementById("fmName").value = f ? f.name : "";
  document.getElementById("fmCode").value = f ? f.code || "" : "";
  setMsg("facultyModalErr", "", "");
  document.getElementById("facultyModal").style.display = "flex";
}

async function saveFaculty() {
  const id = parseInt(document.getElementById("fmId").value) || null;
  const name = document.getElementById("fmName").value.trim();
  const code = document.getElementById("fmCode").value.trim();
  if (!name) {
    setMsg("facultyModalErr", "Faculty name is required.", "err");
    return;
  }
  try {
    const r = id
      ? await api(`/faculties/${id}`, { method: "PUT", json: { name, code } })
      : await api("/faculties", { method: "POST", json: { name, code } });
    if (!r || !r.ok) {
      const d = await r?.json().catch(() => ({}));
      setMsg("facultyModalErr", d.error || "Save failed.", "err");
      return;
    }
  } catch (e) {
    setMsg("facultyModalErr", "Network error.", "err");
    return;
  }
  closeModal("facultyModal");
  await loadFaculties();
  toast(id ? "Faculty updated" : "Faculty added");
}

async function deleteFaculty(id) {
  if (
    !confirm(
      "Delete this faculty? This will fail if subjects, students, or assignments still reference it.",
    )
  )
    return;
  try {
    const r = await api(`/faculties/${id}`, { method: "DELETE" });
    const d = await r?.json().catch(() => ({}));
    if (!r || !r.ok) {
      toast(d.error || "Delete failed", "err");
      return;
    }
  } catch (e) {
    toast("Network error", "err");
    return;
  }
  await loadFaculties();
  toast("Faculty deleted");
}

/* ── Subjects ─────────────────────────────────────────────────────────── */
async function _populateFacSubjectFilters() {
  const faculties = await _loadFaculties();
  const sel = document.getElementById("subjFacultyFilter");
  if (sel) {
    const cur = sel.value;
    while (sel.options.length > 1) sel.remove(1);
    faculties.forEach((f) => {
      const o = document.createElement("option");
      o.value = f.id;
      o.text = f.name;
      sel.add(o);
    });
    sel.value = cur;
  }
  const semSel = document.getElementById("subjSemesterFilter");
  if (semSel && semSel.options.length <= 1) {
    for (let i = 1; i <= 8; i++) {
      const o = document.createElement("option");
      o.value = String(i);
      o.text = `Semester ${i}`;
      semSel.add(o);
    }
  }
}

async function loadSubjects() {
  const facFilter = document.getElementById("subjFacultyFilter")?.value || "";
  const semFilter = document.getElementById("subjSemesterFilter")?.value || "";
  const tbody = document.getElementById("subjectTableBody");
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--text3);padding:2rem">Loading…</td></tr>`;
  try {
    const params = new URLSearchParams();
    if (facFilter) params.set("faculty_id", facFilter);
    if (semFilter) params.set("semester", semFilter);
    const r = await api(
      `/subjects${params.toString() ? "?" + params.toString() : ""}`,
    );
    if (!r || !r.ok) {
      tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;color:var(--red);padding:2rem">Failed to load subjects.</td></tr>`;
      return;
    }
    const data = await r.json();
    const subjects = data.subjects || [];
    if (!subjects.length) {
      tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;color:var(--text3);padding:2rem">No subjects yet.</td></tr>`;
      return;
    }
    tbody.innerHTML = subjects
      .map(
        (s) => `
        <tr>
          <td class="t-name">${escapeHtml(s.name)}</td>
          <td class="t-mono">${escapeHtml(s.code || "—")}</td>
          <td><span class="t-asgn-chip">${escapeHtml(s.faculty_code || s.faculty_name || "—")}</span></td>
          <td class="t-sem-cell">${s.semester ? `<span class="t-sem-badge">Sem ${s.semester}</span>` : `<span class="t-mono">—</span>`}</td>
          <td><div class="action-btns">
            <button class="btn-secondary btn-sm" onclick="openSubjectModal(${s.id})">Edit</button>
            <button class="btn-danger btn-sm" onclick="deleteSubject(${s.id})">Delete</button>
          </div></td>
        </tr>`,
      )
      .join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;color:var(--red);padding:2rem">Error: ${escapeHtml(String(e))}</td></tr>`;
  }
}

let _subjectCache = [];

async function openSubjectModal(id) {
  await _populateFacultyDropdowns();
  let s = null;
  if (id) {
    try {
      const r = await api("/subjects");
      if (r && r.ok) {
        const data = await r.json();
        _subjectCache = data.subjects || [];
        s = _subjectCache.find((x) => x.id === id) || null;
      }
    } catch (_) {}
  }
  document.getElementById("subjectModalTitle").textContent = s
    ? "Edit Subject"
    : "Add Subject";
  document.getElementById("smId").value = s ? s.id : "";
  document.getElementById("smName").value = s ? s.name : "";
  document.getElementById("smCode").value = s ? s.code || "" : "";
  const smFac = document.getElementById("smFaculty");
  if (smFac) smFac.value = s ? s.faculty_id || "" : "";
  const smSem = document.getElementById("smSemester");
  if (smSem) smSem.value = s ? s.semester || "" : "";
  setMsg("subjectModalErr", "", "");
  document.getElementById("subjectModal").style.display = "flex";
}

async function saveSubject() {
  const id = parseInt(document.getElementById("smId").value) || null;
  const name = document.getElementById("smName").value.trim();
  const code = document.getElementById("smCode").value.trim();
  const faculty_id =
    parseInt(document.getElementById("smFaculty")?.value) || null;
  const semester = document.getElementById("smSemester")?.value || "";
  if (!name) {
    setMsg("subjectModalErr", "Subject name is required.", "err");
    return;
  }
  if (!code) {
    setMsg("subjectModalErr", "Subject ID is required.", "err");
    return;
  }
  try {
    const body = {
      name,
      code,
      faculty_id,
      semester: semester ? parseInt(semester) : null,
    };
    const r = id
      ? await api(`/subjects/${id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        })
      : await api(`/subjects`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
    if (!r || !r.ok) {
      const err = await r?.json().catch(() => ({}));
      setMsg("subjectModalErr", err.error || "Save failed.", "err");
      return;
    }
  } catch (e) {
    setMsg("subjectModalErr", String(e), "err");
    return;
  }
  closeModal("subjectModal");
  loadSubjects();
  toast(id ? "Subject updated" : "Subject added");
}

async function deleteSubject(id) {
  if (!confirm("Delete this subject?")) return;
  try {
    await api(`/subjects/${id}`, { method: "DELETE" });
  } catch (_) {}
  loadSubjects();
  toast("Subject deleted");
}

/* ── Time Slots ───────────────────────────────────────────────────────── */
let _tsmCache = [];

async function loadTimeslots() {
  const search = document.getElementById("tsmSearch")?.value.trim() || "";
  const tbody = document.getElementById("timeslotTableBody");
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="4" style="text-align:center;color:var(--text3);padding:2rem">Loading…</td></tr>`;
  try {
    const params = new URLSearchParams({ limit: 100 });
    if (search) params.set("search", search);
    const r = await api(`/timeslots?${params}`);
    if (!r || !r.ok) {
      tbody.innerHTML = `<tr><td colspan="4" style="text-align:center;color:var(--red);padding:2rem">Failed to load.</td></tr>`;
      return;
    }
    const data = await r.json();
    _tsmCache = data.time_slots || [];
    if (!_tsmCache.length) {
      tbody.innerHTML = `<tr><td colspan="4" style="text-align:center;color:var(--text3);padding:2rem">No time slots found${search ? ' matching "' + escapeHtml(search) + '"' : ""}.</td></tr>`;
      return;
    }
    tbody.innerHTML = _tsmCache
      .map(
        (s) => `
      <tr>
        <td style="font-weight:500">${escapeHtml(s.label)}</td>
        <td style="font-family:var(--mono);font-size:12px">${s.start_time || "—"}</td>
        <td style="font-family:var(--mono);font-size:12px">${s.end_time || "—"}</td>
        <td>
          <button class="btn-secondary btn-sm" onclick="openTimeSlotModal(${s.id})">Edit</button>
          <button class="btn-danger btn-sm" style="margin-left:4px" onclick="deleteTimeSlot(${s.id})">Delete</button>
        </td>
      </tr>`,
      )
      .join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4" style="text-align:center;color:var(--red);padding:2rem">Error: ${escapeHtml(String(e))}</td></tr>`;
  }
}

let _tsmEditId = null;
function openTimeSlotModal(id) {
  _tsmEditId = id || null;
  const s = id ? _tsmCache.find((x) => x.id === id) : null;
  document.getElementById("tsmLabel").value = s ? s.label : "";
  document.getElementById("tsmStart").value = s ? s.start_time || "" : "";
  document.getElementById("tsmEnd").value = s ? s.end_time || "" : "";
  document.getElementById("tsmErr").textContent = "";
  document.getElementById("timeSlotModal").style.display = "flex";
}

async function saveTimeSlot() {
  const label = document.getElementById("tsmLabel").value.trim();
  const start_time = document.getElementById("tsmStart").value;
  const end_time = document.getElementById("tsmEnd").value;
  if (!label) {
    document.getElementById("tsmErr").textContent = "Label is required.";
    return;
  }
  if (!start_time || !end_time) {
    document.getElementById("tsmErr").textContent =
      "Start and end times required.";
    return;
  }
  try {
    const body = { label, start_time, end_time };
    const r = _tsmEditId
      ? await api(`/timeslots/${_tsmEditId}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        })
      : await api(`/timeslots`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
    if (!r || !r.ok) {
      const err = await r?.json().catch(() => ({}));
      document.getElementById("tsmErr").textContent =
        err.error || "Save failed.";
      return;
    }
  } catch (e) {
    document.getElementById("tsmErr").textContent = String(e);
    return;
  }
  closeModal("timeSlotModal");
  loadTimeslots();
  toast(_tsmEditId ? "Time slot updated" : "Time slot added");
  _tsmEditId = null;
}

async function deleteTimeSlot(id) {
  if (!confirm("Delete this time slot?")) return;
  try {
    await api(`/timeslots/${id}`, { method: "DELETE" });
  } catch (_) {}
  loadTimeslots();
  toast("Time slot deleted");
}

async function deleteAllTimeslots() {
  if (
    !confirm(
      "Delete ALL time slots? This will also remove time slot assignments from all teachers. This cannot be undone.",
    )
  )
    return;
  const r = await api("/timeslots/all", { method: "DELETE" });
  if (!r || !r.ok) {
    toast("Failed to delete time slots");
    return;
  }
  loadTimeslots();
  toast("All time slots deleted");
}

/* ══════════════════════════════════════════════════════════════════════
   TEACHERS PAGE
══════════════════════════════════════════════════════════════════════ */
let _deletingTeacherId = null;

async function loadTeachers() {
  const tbody = document.getElementById("teacherTableBody");
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;color:var(--text3);padding:2rem">Loading…</td></tr>`;
  try {
    const r = await api("/teachers");
    if (!r || !r.ok) {
      tbody.innerHTML = `<tr><td colspan="9" style="text-align:center;color:var(--red);padding:2rem">Failed to load teachers.</td></tr>`;
      return;
    }
    const data = await r.json();
    const teachers = data.teachers || [];
    if (!teachers.length) {
      tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--text3);padding:2rem">No teachers yet. Click + Add Teacher.</td></tr>`;
      return;
    }
    tbody.innerHTML = teachers
      .map((t) => {
        const asgn = t.assignments || [];
        const asgnHtml =
          asgn.length === 0
            ? `<span class="t-no-asgn">No assignments</span>`
            : asgn
                .map((a) => {
                  const fac = escapeHtml(
                    a.faculty_code || a.faculty_name || "",
                  );
                  const sub = escapeHtml(
                    a.subject_code || a.subject_name || "",
                  );
                  const sem = a.semester ? `S${a.semester}` : "";
                  return `<span class="t-asgn-chip">${fac}${sem ? " " + sem : ""} · ${sub}</span>`;
                })
                .join("");
        const statusColor =
          t.status === "active" ? "var(--green)" : "var(--amber)";
        return `<tr>
          <td class="t-mono">${escapeHtml(t.teacher_id || "—")}</td>
          <td class="t-name">${escapeHtml(t.full_name || "")}</td>
          <td class="t-email">${escapeHtml(t.email || "—")}</td>
          <td class="t-asgn">${asgnHtml}</td>
          <td><span class="t-status" style="color:${statusColor}">${t.status || "active"}</span></td>
          <td><div class="action-btns">
            <button class="btn-secondary btn-sm" onclick="openTeacherModal(${t.id})">Edit</button>
            <button class="btn-danger btn-sm" onclick="deleteTeacher(${t.id})">Delete</button>
          </div></td>
        </tr>`;
      })
      .join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--red);padding:2rem">Error: ${escapeHtml(String(e))}</td></tr>`;
  }
}

let _teacherCache = [];
let _currentEditTeacherId = null;

async function openTeacherModal(id) {
  _currentEditTeacherId = id || null;
  let t = null;
  if (id) {
    try {
      const r = await api(`/teachers/${id}`);
      if (r && r.ok) {
        const data = await r.json();
        t = data.teacher || null;
      }
    } catch (_) {}
  }

  document.getElementById("teacherModalTitle").textContent = t
    ? `Edit — ${t.full_name}`
    : "Add Teacher";
  document.getElementById("tmId").value = t ? t.id : "";
  document.getElementById("tmTeacherId").value = t ? t.teacher_id || "" : "";
  document.getElementById("tmFullName").value = t ? t.full_name || "" : "";
  document.getElementById("tmPassword").value = "";
  document.getElementById("tmEmail").value = t ? t.email || "" : "";
  document.getElementById("tmPhone").value = t ? t.phone || "" : "";
  document.getElementById("tmStatus").value = t
    ? t.status || "active"
    : "active";

  setMsg("teacherModalErr", "", "");
  document.getElementById("teacherModal").style.display = "flex";
}

async function _populateAssignmentDropdowns() {
  const faculties = await _loadFaculties();
  const facSel = document.getElementById("taFaculty");
  if (facSel) {
    const cur = facSel.value;
    while (facSel.options.length > 1) facSel.remove(1);
    faculties.forEach((f) => {
      const o = document.createElement("option");
      o.value = f.id;
      o.text = f.name;
      facSel.add(o);
    });
    facSel.value = cur;
  }
  // Time slots — always reload fresh (all 810+)
  const tsSel = document.getElementById("taTimeSlot");
  if (tsSel) {
    while (tsSel.options.length > 1) tsSel.remove(1);
    try {
      const r = await api("/timeslots");
      if (r && r.ok) {
        const data = await r.json();
        (data.time_slots || []).forEach((ts) => {
          const o = document.createElement("option");
          o.value = ts.id;
          o.text = `${ts.label}${ts.start_time ? " (" + ts.start_time + " – " + ts.end_time + ")" : ""}`;
          tsSel.add(o);
        });
      }
    } catch (_) {}
  }
}

async function loadSubjectsForAssignment() {
  const faculty_id = document.getElementById("taFaculty")?.value || "";
  const semester = document.getElementById("taSemester")?.value || "";
  const sel = document.getElementById("taSubject");
  if (!sel) return;
  sel.innerHTML = '<option value="">— Select Subject —</option>';
  if (!faculty_id || !semester) {
    const o = document.createElement("option");
    o.disabled = true;
    o.text = faculty_id
      ? "Select a semester first"
      : "Select faculty & semester first";
    sel.add(o);
    return;
  }
  try {
    const params = `?faculty_id=${encodeURIComponent(faculty_id)}&semester=${encodeURIComponent(semester)}`;
    const r = await api(`/subjects${params}`);
    if (r && r.ok) {
      const data = await r.json();
      const subjects = data.subjects || [];
      if (!subjects.length) {
        const o = document.createElement("option");
        o.disabled = true;
        o.text = "No subjects for this faculty/semester";
        sel.add(o);
        return;
      }
      subjects.forEach((s) => {
        const o = document.createElement("option");
        o.value = s.id;
        o.text = `${s.name} (${s.code})`;
        sel.add(o);
      });
    }
  } catch (_) {}
}

let _editAssignmentCache = {};

function _renderAssignments(assignments) {
  const el = document.getElementById("teacherAssignmentsList");
  if (!el) return;
  // Cache all assignment data so edit can pre-populate
  assignments.forEach((a) => {
    _editAssignmentCache[a.id] = a;
  });
  if (!assignments.length) {
    el.innerHTML = `<p style="font-size:12px;color:var(--text3);margin:0 0 0.5rem">No assignments yet.</p>`;
    return;
  }
  el.innerHTML = assignments
    .map(
      (a) => `
    <div id="arow-${a.id}" style="display:flex;align-items:center;gap:0.5rem;padding:0.5rem 0.75rem;background:var(--bg2);border:1px solid var(--border);border-radius:6px;margin-bottom:0.4rem;font-size:12px">
      <span style="flex:1;line-height:1.7">
        <span style="color:var(--text2);font-size:11px;font-weight:600">${escapeHtml(a.faculty_name || "—")}</span>
        <span style="color:var(--text3);margin:0 4px">·</span>
        <span>Sem ${a.semester || "?"}</span>
        <span style="color:var(--text3);margin:0 4px">·</span>
        <span>${escapeHtml(a.subject_name || "—")} <span style="color:var(--text3);font-size:11px">${escapeHtml(a.subject_code || "")}</span></span>
        ${a.day_of_week ? `<span style="color:var(--text3);margin:0 4px">·</span><span style="color:var(--blue);font-size:11px">${escapeHtml(a.day_of_week)}</span>` : ""}
        ${a.time_slot_label ? `<span style="color:var(--text3);margin:0 4px">·</span><span style="color:var(--text3);font-size:11px">${escapeHtml(a.time_slot_label)}</span>` : ""}
      </span>
      <button class="btn-secondary btn-sm" onclick="openEditAssignment(${a.id})" style="padding:2px 10px;font-size:11px">Edit</button>
      <button class="btn-danger btn-sm" onclick="removeTeacherAssignment(${a.id})" style="padding:2px 8px;font-size:11px">×</button>
    </div>`,
    )
    .join("");
}

async function openEditAssignment(aid) {
  const current = _editAssignmentCache[aid] || {};
  const row = document.getElementById(`arow-${aid}`);
  if (!row) return;

  // Show a loading state in the row while data loads
  row.innerHTML = `<span style="color:var(--text3);font-size:11px;padding:0.25rem 0">Loading…</span>`;

  const [faculties, tsR] = await Promise.all([
    _loadFaculties(),
    api("/timeslots"),
  ]);
  const slots = (tsR && tsR.ok ? (await tsR.json()).time_slots : null) || [];

  // Load subjects for the current faculty+semester so the subject dropdown is pre-filled
  let subjects = [];
  if (current.faculty_id && current.semester) {
    const sR = await api(
      `/subjects?faculty_id=${current.faculty_id}&semester=${current.semester}`,
    );
    if (sR && sR.ok) subjects = (await sR.json()).subjects || [];
  }

  const sel = (opts, val) =>
    opts
      .map(
        (o) =>
          `<option value="${o.v}"${String(o.v) === String(val) ? " selected" : ""}>${escapeHtml(o.l)}</option>`,
      )
      .join("");

  const facOpts = sel(
    faculties.map((f) => ({ v: f.id, l: f.name })),
    current.faculty_id,
  );
  const semOpts = sel(
    [1, 2, 3, 4, 5, 6, 7, 8].map((n) => ({ v: n, l: `Semester ${n}` })),
    current.semester,
  );
  const subOpts = sel(
    subjects.map((s) => ({ v: s.id, l: `${s.name} (${s.code})` })),
    current.subject_id,
  );
  const dayOpts = sel(
    ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map((d) => ({ v: d, l: d })),
    current.day_of_week,
  );
  const slotOpts = sel(
    slots.map((s) => ({
      v: s.id,
      l: s.label + (s.start_time ? ` (${s.start_time})` : ""),
    })),
    current.time_slot_id,
  );

  row.innerHTML = `
    <div style="flex:1;width:100%">
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:0.4rem;margin-bottom:0.4rem">
        <div>
          <div style="font-size:10px;color:var(--text3);margin-bottom:2px">Faculty</div>
          <select id="eaFaculty-${aid}" onchange="editLoadSubjects(${aid})" style="font-size:12px;width:100%">
            <option value="">Select faculty</option>${facOpts}
          </select>
        </div>
        <div>
          <div style="font-size:10px;color:var(--text3);margin-bottom:2px">Semester</div>
          <select id="eaSemester-${aid}" onchange="editLoadSubjects(${aid})" style="font-size:12px;width:100%">
            <option value="">Select semester</option>${semOpts}
          </select>
        </div>
        <div>
          <div style="font-size:10px;color:var(--text3);margin-bottom:2px">Subject</div>
          <select id="eaSubject-${aid}" style="font-size:12px;width:100%">
            <option value="">Select subject</option>${subOpts}
          </select>
        </div>
        <div>
          <div style="font-size:10px;color:var(--text3);margin-bottom:2px">Day</div>
          <select id="eaDay-${aid}" style="font-size:12px;width:100%">
            <option value="">Any day</option>${dayOpts}
          </select>
        </div>
        <div>
          <div style="font-size:10px;color:var(--text3);margin-bottom:2px">Time Slot</div>
          <select id="eaSlot-${aid}" style="font-size:12px;width:100%">
            <option value="">Select time slot</option>${slotOpts}
          </select>
        </div>
        <div style="display:flex;align-items:flex-end;gap:0.4rem">
          <button class="btn-primary btn-sm" onclick="saveAssignmentEdit(${aid})" style="font-size:12px;flex:1">Save</button>
          <button class="btn-secondary btn-sm" onclick="_reloadAssignments()" style="font-size:12px;flex:1">Cancel</button>
        </div>
      </div>
    </div>`;
}

async function editLoadSubjects(aid) {
  const fid = document.getElementById(`eaFaculty-${aid}`)?.value || "";
  const sem = document.getElementById(`eaSemester-${aid}`)?.value || "";
  const sel = document.getElementById(`eaSubject-${aid}`);
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">Select subject</option>';
  if (!fid || !sem) return;
  const r = await api(
    `/subjects?faculty_id=${encodeURIComponent(fid)}&semester=${encodeURIComponent(sem)}`,
  );
  if (r && r.ok) {
    const data = await r.json();
    (data.subjects || []).forEach((s) => {
      const o = document.createElement("option");
      o.value = s.id;
      o.text = `${s.name} (${s.code})`;
      if (String(s.id) === String(prev)) o.selected = true;
      sel.add(o);
    });
  }
}

async function saveAssignmentEdit(aid) {
  const faculty_id =
    parseInt(document.getElementById(`eaFaculty-${aid}`)?.value) || null;
  const semester =
    parseInt(document.getElementById(`eaSemester-${aid}`)?.value) || null;
  const subject_id =
    parseInt(document.getElementById(`eaSubject-${aid}`)?.value) || null;
  const time_slot_id =
    parseInt(document.getElementById(`eaSlot-${aid}`)?.value) || null;
  const day_of_week = document.getElementById(`eaDay-${aid}`)?.value || null;

  try {
    const r = await api(`/teacher-assignments/${aid}`, {
      method: "PUT",
      json: { faculty_id, semester, subject_id, time_slot_id, day_of_week },
    });
    if (!r || !r.ok) {
      const err = await r?.json().catch(() => ({}));
      toast(err.error || "Failed to save", "err");
      return;
    }
    toast("Assignment updated");
    _reloadAssignments();
    loadTeachers();
  } catch (e) {
    toast(String(e), "err");
  }
}

async function _reloadAssignments() {
  if (!_currentEditTeacherId) return;
  const r = await api(`/teachers/${_currentEditTeacherId}`);
  if (r && r.ok) {
    const data = await r.json();
    _renderAssignments(data.teacher?.assignments || []);
  }
}

async function addTeacherAssignment() {
  const tid = _currentEditTeacherId;
  if (!tid) return;
  const faculty_id =
    parseInt(document.getElementById("taFaculty")?.value) || null;
  const semester =
    parseInt(document.getElementById("taSemester")?.value) || null;
  const subject_id =
    parseInt(document.getElementById("taSubject")?.value) || null;
  const time_slot_id =
    parseInt(document.getElementById("taTimeSlot")?.value) || null;
  const day_of_week = document.getElementById("taDay")?.value || null;
  setMsg("assignmentErr", "", "");
  try {
    const r = await api(`/teachers/${tid}/assignments`, {
      method: "POST",
      json: { faculty_id, semester, subject_id, time_slot_id, day_of_week },
    });
    if (!r || !r.ok) {
      const err = await r?.json().catch(() => ({}));
      setMsg("assignmentErr", err.error || "Failed to add assignment.", "err");
      return;
    }
  } catch (e) {
    setMsg("assignmentErr", String(e), "err");
    return;
  }
  // Reload teacher to refresh assignments list
  const tr = await api(`/teachers/${tid}`);
  if (tr && tr.ok) {
    const data = await tr.json();
    _renderAssignments(data.teacher?.assignments || []);
  }
  // Reset dropdowns
  ["taFaculty", "taSemester", "taSubject", "taTimeSlot", "taDay"].forEach(
    (id) => {
      const el = document.getElementById(id);
      if (el) el.value = "";
    },
  );
  toast("Assignment added");
  loadTeachers();
}

async function removeTeacherAssignment(aid) {
  if (!confirm("Remove this assignment?")) return;
  try {
    await api(`/teacher-assignments/${aid}`, { method: "DELETE" });
  } catch (_) {}
  const tid = _currentEditTeacherId;
  if (tid) {
    const tr = await api(`/teachers/${tid}`);
    if (tr && tr.ok) {
      const data = await tr.json();
      _renderAssignments(data.teacher?.assignments || []);
    }
  }
  toast("Assignment removed");
  loadTeachers();
}

async function saveTeacher() {
  const id = parseInt(document.getElementById("tmId").value) || null;
  const teacher_id = document.getElementById("tmTeacherId").value.trim();
  const full_name = document.getElementById("tmFullName").value.trim();
  const password = document.getElementById("tmPassword").value;
  const email = document.getElementById("tmEmail").value.trim();
  const phone = document.getElementById("tmPhone").value.trim();
  const status = document.getElementById("tmStatus").value;

  if (!teacher_id) {
    setMsg("teacherModalErr", "Teacher ID is required.", "err");
    return;
  }
  if (!full_name) {
    setMsg("teacherModalErr", "Full name is required.", "err");
    return;
  }
  if (!id && !password) {
    setMsg("teacherModalErr", "Password is required for new teacher.", "err");
    return;
  }
  if (password && password.length < 6) {
    setMsg("teacherModalErr", "Password must be at least 6 characters.", "err");
    return;
  }

  const body = { teacher_id, full_name, email, phone, status };
  if (password) body.password = password;

  try {
    const r = id
      ? await api(`/teachers/${id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        })
      : await api(`/teachers`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
    if (!r || !r.ok) {
      const err = await r?.json().catch(() => ({}));
      setMsg("teacherModalErr", err.error || "Save failed.", "err");
      return;
    }
    if (!id) {
      toast("Teacher created");
      closeModal("teacherModal");
      loadTeachers();
      return;
    }
  } catch (e) {
    setMsg("teacherModalErr", String(e), "err");
    return;
  }
  closeModal("teacherModal");
  loadTeachers();
  toast("Teacher updated");
}

async function deleteTeacher(id) {
  _deletingTeacherId = id;
  const t = _teacherCache.find((x) => x.id === id);
  const name = t ? t.full_name : `ID ${id}`;
  if (!confirm(`Delete teacher "${name}"? This cannot be undone.`)) {
    _deletingTeacherId = null;
    return;
  }
  try {
    const r = await api(`/teachers/${id}`, { method: "DELETE" });
    if (!r || !r.ok) {
      const err = await r?.json().catch(() => ({}));
      toast(err.error || "Delete failed.", "err");
      _deletingTeacherId = null;
      return;
    }
  } catch (_) {}
  loadTeachers();
  toast("Teacher deleted");
  _deletingTeacherId = null;
}

function reassignTeacherReferences() {
  toast("No teacher references to reassign.", "err");
}
function clearTeacherReferences() {
  toast("No teacher references to clear.", "err");
}
function deleteTeacherConfirmed() {
  if (!_deletingTeacherId) return;
  deleteTeacher(_deletingTeacherId);
  closeModal("teacherRefsModal");
}

/* ══════════════════════════════════════════════════════════════════════
   TEACHER PANEL — Dashboard
══════════════════════════════════════════════════════════════════════ */
async function loadTeacherDashboard() {
  const dayEl = document.getElementById("tDashDay");
  if (dayEl)
    dayEl.textContent = new Date().toLocaleDateString("en-GB", {
      weekday: "long",
      year: "numeric",
      month: "long",
      day: "numeric",
    });

  try {
    const [todayData, statsData, schedData] = await Promise.all([
      api("/teacher/me/today").then((r) => r?.json()),
      api("/teacher/me/stats").then((r) => r?.json()),
      api("/teacher/me/schedule").then((r) => r?.json()),
    ]);

    // Stats row
    const m = document.getElementById("tDashMetrics");
    if (m && statsData) {
      m.innerHTML = `
        <div class="metric-card">
          <div class="metric-label">My Classes</div>
          <div class="metric-val">${statsData.class_count}</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Sessions Held</div>
          <div class="metric-val" style="color:var(--blue)">${statsData.total_sessions}</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Today's Present</div>
          <div class="metric-val" style="color:var(--green)">${statsData.today_marked}</div>
        </div>
        <div class="metric-card">
          <div class="metric-label">Today</div>
          <div class="metric-val" style="font-size:1rem">${new Date().toLocaleDateString("en-GB", { weekday: "short" })}</div>
        </div>`;
    }

    // Today's classes
    const todayEl = document.getElementById("tTodayClasses");
    if (todayEl) {
      const classes = todayData?.classes || [];
      if (!classes.length) {
        todayEl.innerHTML = `<div class="teacher-no-class">
          <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity=".4"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>
          <div>No classes scheduled for today</div>
        </div>`;
      } else {
        todayEl.innerHTML = classes
          .map((cls) => _renderClassCard(cls))
          .join("");
      }
    }

    // Weekly schedule
    const weekEl = document.getElementById("tWeeklySchedule");
    if (weekEl && schedData) {
      const days = schedData.days || ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
      const sched = schedData.schedule || {};
      const otherSlots = sched["Other"] || [];
      const slotHtml = (cls) => `
        <div class="weekly-slot">
          <div class="weekly-slot-subject">${escapeHtml(cls.subject_name || "—")}</div>
          <div class="weekly-slot-meta">${cls.faculty_code || ""} Sem ${cls.semester}</div>
          ${cls.time_slot_label ? `<div class="weekly-slot-time">${cls.time_slot_label}</div>` : ""}
        </div>`;
      const otherHtml = otherSlots.length
        ? `
        <div style="margin-top:1rem;padding:0.75rem 1rem;background:var(--bg2);border-radius:8px">
          <div style="font-size:11px;color:var(--text3);margin-bottom:0.5rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em">No day assigned</div>
          <div style="display:flex;flex-wrap:wrap;gap:0.5rem">
            ${otherSlots.map(slotHtml).join("")}
          </div>
        </div>`
        : "";
      weekEl.innerHTML = `<div class="weekly-grid">
        ${days
          .map(
            (day) => `
          <div class="weekly-day">
            <div class="weekly-day-label">${day}</div>
            <div class="weekly-day-slots">
              ${
                (sched[day] || []).length
                  ? (sched[day] || []).map(slotHtml).join("")
                  : `<div class="weekly-slot-empty">—</div>`
              }
            </div>
          </div>`,
          )
          .join("")}
      </div>${otherHtml}`;
    }

    // My Assigned Classes — uses /teacher/me which already has full assignments list
    _loadAssignedClasses();
  } catch (e) {
    console.error("loadTeacherDashboard:", e);
  }
}

async function _loadAssignedClasses() {
  const cardEl = document.getElementById("tAssignedCards");
  const tableEl = document.getElementById("tAssignedTable");
  if (!cardEl) return;
  cardEl.innerHTML = `<div class="text-muted text-center p-2rem text-13px">Loading…</div>`;

  try {
    const r = await api("/teacher/me");
    if (!r?.ok) return;
    const data = await r.json();
    const assignments = data.teacher?.assignments || data.assignments || [];

    if (!assignments.length) {
      cardEl.innerHTML = `<div class="teacher-no-class">
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity=".4"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>
        <div>No classes assigned yet</div>
      </div>`;
      if (tableEl) tableEl.innerHTML = "";
      return;
    }

    // Card view
    cardEl.innerHTML = assignments
      .map(
        (a) => `
      <div class="teacher-class-card">
        <div class="tcc-header">
          <div>
            <div class="tcc-subject">${escapeHtml(a.subject_name || "—")}</div>
            <div class="tcc-meta">${escapeHtml(a.faculty_code || "")}  ·  Semester ${a.semester}</div>
          </div>
          ${
            a.student_count != null
              ? `<span class="pill" style="background:var(--blue-bg,#eef4ff);color:var(--blue)">${a.student_count} students</span>`
              : ""
          }
        </div>
        ${
          a.day_of_week || a.time_slot_label
            ? `<div class="tcc-time">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
          ${a.day_of_week ? escapeHtml(a.day_of_week) + " · " : ""}${escapeHtml(a.time_slot_label || "")}
          ${a.start_time ? `· ${a.start_time.slice(0, 5)}–${(a.end_time || "").slice(0, 5)}` : ""}
        </div>`
            : ""
        }
        <div style="font-size:11px;color:var(--text3);margin-top:0.25rem">${escapeHtml(a.faculty_name || "")}</div>
        <div class="tcc-actions" style="margin-top:0.5rem">
          <button class="btn-primary btn-sm" onclick="openSessionModal(${a.id})">Start Attendance</button>
        </div>
      </div>`,
      )
      .join("");

    // Timetable view (Mon–Sat grid)
    if (tableEl) {
      const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
      const grouped = {};
      days.forEach((d) => (grouped[d] = []));
      const unscheduled = [];
      assignments.forEach((a) => {
        if (a.day_of_week && grouped[a.day_of_week])
          grouped[a.day_of_week].push(a);
        else unscheduled.push(a);
      });
      const maxRows = Math.max(1, ...days.map((d) => grouped[d].length));
      const cellHtml = (
        a,
      ) => `<td style="vertical-align:top;padding:0.5rem 0.75rem">
        <div style="font-weight:600;font-size:12px">${escapeHtml(a.subject_name || "—")}</div>
        <div style="font-size:11px;color:var(--text3)">${escapeHtml(a.faculty_code || "")} Sem ${a.semester}</div>
        <div style="font-size:11px;color:var(--blue)">${a.time_slot_label || ""}</div>
        ${a.student_count != null ? `<div style="font-size:11px;color:var(--text3)">${a.student_count} students</div>` : ""}
      </td>`;
      const unscheduledHtml = unscheduled.length
        ? `
        <div style="margin-top:1rem;padding:0.75rem 1rem;background:var(--bg2);border-radius:8px">
          <div style="font-size:11px;color:var(--text3);margin-bottom:0.5rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em">No day assigned</div>
          <div style="display:flex;flex-wrap:wrap;gap:0.5rem">
            ${unscheduled
              .map(
                (
                  a,
                ) => `<div style="background:var(--bg3,#222);border-radius:6px;padding:0.4rem 0.6rem">
              <div style="font-weight:600;font-size:12px">${escapeHtml(a.subject_name || "—")}</div>
              <div style="font-size:11px;color:var(--text3)">${escapeHtml(a.faculty_code || "")} Sem ${a.semester}</div>
              <div style="font-size:11px;color:var(--blue)">${a.time_slot_label || ""}</div>
            </div>`,
              )
              .join("")}
          </div>
        </div>`
        : "";
      tableEl.innerHTML = `<table class="data-table" style="min-width:600px">
        <thead><tr>${days.map((d) => `<th style="text-align:center;min-width:120px">${d}</th>`).join("")}</tr></thead>
        <tbody>
          ${Array.from(
            { length: maxRows },
            (_, i) => `
            <tr>${days
              .map((d) => {
                const a = grouped[d][i];
                return a ? cellHtml(a) : `<td></td>`;
              })
              .join("")}</tr>`,
          ).join("")}
        </tbody>
      </table>${unscheduledHtml}`;
    }
  } catch (e) {
    console.error("_loadAssignedClasses:", e);
    if (cardEl)
      cardEl.innerHTML = `<div class="text-muted text-13px p-1rem">Failed to load assignments</div>`;
  }
}

function switchAssignedView(view, btn) {
  const cardEl = document.getElementById("tAssignedCards");
  const tableEl = document.getElementById("tAssignedTable");
  document
    .querySelectorAll("#acViewCard, #acViewTable")
    .forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  if (view === "card") {
    cardEl?.classList.remove("hidden");
    tableEl?.classList.add("hidden");
  } else {
    cardEl?.classList.add("hidden");
    tableEl?.classList.remove("hidden");
  }
}

function _renderClassCard(cls) {
  const sess = cls.session;
  const sessStatus = sess ? sess.status : null;
  const markedCount = sess ? sess.marked_count || 0 : 0;
  const statusHtml =
    sessStatus === "open"
      ? `<span class="pill pill-amber">Session Open · ${markedCount} marked</span>`
      : sessStatus === "closed"
        ? `<span class="pill pill-green">Done · ${markedCount} marked</span>`
        : `<span class="pill" style="background:var(--bg3);color:var(--text3)">Not started</span>`;

  return `<div class="teacher-class-card">
    <div class="tcc-header">
      <div>
        <div class="tcc-subject">${escapeHtml(cls.subject_name || "—")}</div>
        <div class="tcc-meta">${cls.faculty_code || ""}  ·  Semester ${cls.semester}</div>
      </div>
      ${statusHtml}
    </div>
    ${
      cls.time_slot_label
        ? `<div class="tcc-time">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
      ${cls.time_slot_label}${cls.start_time ? ` · ${cls.start_time.slice(0, 5)}–${(cls.end_time || "").slice(0, 5)}` : ""}
    </div>`
        : ""
    }
    <div class="tcc-actions">
      ${
        sessStatus === "open"
          ? `<button class="btn-primary btn-sm" onclick="continueSession(${sess.id}, '${escapeHtml(cls.subject_name || "")}')">Continue</button>
           <button class="btn-secondary btn-sm" onclick="openQRModal(${sess.id})" title="Show QR code for students to scan">QR Code</button>`
          : sessStatus === "closed"
            ? `<button class="btn-secondary btn-sm" onclick="viewSessionReport(${sess.id})">View Report</button>`
            : `<button class="btn-primary btn-sm" onclick="openSessionModal(${cls.id || cls.assignment_id})">Start Attendance</button>`
      }
    </div>
  </div>`;
}

function toggleWeeklySchedule() {
  const el = document.getElementById("tWeeklySchedule");
  if (el) el.classList.toggle("hidden");
}

/* ── Teacher Recognition Page ─────────────────────────────────────────── */
let _teacherWebcamStream = null;
let _teacherAutoInterval = null;
let _teacherAutoActive = false;
let _sessionMarked = new Set();
let _cameraFacing = "user"; // "user" = front, "environment" = rear
let _photoCamStream = null;
let _photoCamFacing = "environment";

async function startTeacherWebcam() {
  try {
    if (_teacherWebcamStream) {
      _teacherWebcamStream.getTracks().forEach((t) => t.stop());
    }
    const constraints = {
      video: {
        facingMode: _cameraFacing,
        width: { ideal: 1280 },
        height: { ideal: 720 },
      },
    };
    _teacherWebcamStream =
      await navigator.mediaDevices.getUserMedia(constraints);
    const video = document.getElementById("tRecogVideo");
    video.srcObject = _teacherWebcamStream;
    video.style.display = "block";
    const overlay = document.getElementById("tRecogOverlay");
    if (overlay) overlay.style.display = "none";
    document.getElementById("btnTRecogStop")?.classList.remove("hidden");
    document.getElementById("tRecogStopFloat")?.classList.remove("hidden");
    document.getElementById("btnSwitchCam")?.classList.remove("hidden");
  } catch (e) {
    toast(
      "Camera access denied — " + (e.message || "check permissions"),
      "err",
    );
  }
}

async function switchCamera() {
  _cameraFacing = _cameraFacing === "user" ? "environment" : "user";
  toast(`Switching to ${_cameraFacing === "user" ? "front" : "rear"} camera…`);
  await startTeacherWebcam();
}

function stopTeacherWebcam() {
  if (_teacherAutoActive) toggleTeacherAuto();
  if (_teacherWebcamStream) {
    _teacherWebcamStream.getTracks().forEach((t) => t.stop());
    _teacherWebcamStream = null;
  }
  const video = document.getElementById("tRecogVideo");
  if (video) {
    video.srcObject = null;
    video.style.display = "none";
  }
  const overlay = document.getElementById("tRecogOverlay");
  if (overlay) overlay.style.display = "flex";
  document.getElementById("btnTRecogStop")?.classList.add("hidden");
  document.getElementById("tRecogStopFloat")?.classList.add("hidden");
  document.getElementById("btnSwitchCam")?.classList.add("hidden");
  const btn = document.getElementById("btnTRecogAuto");
  if (btn) btn.textContent = "▶ Start Auto";
  _teacherAutoActive = false;
}

function toggleTeacherAuto() {
  const btn = document.getElementById("btnTRecogAuto");
  if (_teacherAutoActive) {
    clearInterval(_teacherAutoInterval);
    _teacherAutoInterval = null;
    _teacherAutoActive = false;
    if (btn) btn.textContent = "▶ Start Auto";
    const sb = document.getElementById("sessionStatusBar");
    if (sb) {
      sb.classList.add("hidden");
      sb.textContent = "";
    }
  } else {
    if (!_teacherWebcamStream) {
      toast("Start camera first", "err");
      return;
    }
    _teacherAutoActive = true;
    if (btn) btn.textContent = "⏸ Pause Auto";
    const sb = document.getElementById("sessionStatusBar");
    if (sb) {
      sb.classList.remove("hidden");
      sb.textContent = "Auto recognition active — scanning every 2 seconds";
    }
    _sessionMarked.clear();
    _teacherAutoInterval = setInterval(doTeacherRecognize, 2000);
  }
}

async function doTeacherRecognize() {
  const video = document.getElementById("tRecogVideo");
  if (!video || !video.srcObject || video.readyState < 2) return;

  const canvas = document.createElement("canvas");
  canvas.width = video.videoWidth || 640;
  canvas.height = video.videoHeight || 480;
  canvas.getContext("2d").drawImage(video, 0, 0);
  const b64 = canvas.toDataURL("image/jpeg", 0.8).split(",")[1];

  try {
    const r = await api("/recognize", { method: "POST", json: { image: b64 } });
    if (!r) return;
    const d = await r.json();

    const resultEl = document.getElementById("tRecogResult");
    if (d.recognized) {
      if (resultEl)
        resultEl.innerHTML = `<span style="color:var(--green);font-size:13px;font-weight:600">✓ ${escapeHtml(d.name)} — ${d.confidence}%${d.attendance_marked ? " (marked)" : " (already marked)"}</span>`;

      if (d.attendance_marked) {
        toast(`✓ ${d.name} marked present`);
        _sessionMarked.add(d.student_id);
        const countEl = document.getElementById("sessionCountPill");
        if (countEl) countEl.textContent = `${_sessionMarked.size} marked`;
        const markedCountEl = document.getElementById("sessionMarkedCount");
        if (markedCountEl) markedCountEl.textContent = _sessionMarked.size;
        const markedLabelEl = document.getElementById("sessionMarkedLabel");
        if (markedLabelEl) markedLabelEl.textContent = "marked today";

        const log = document.getElementById("tSessionLog");
        if (log) {
          const placeholder = log.querySelector(".session-placeholder");
          if (placeholder) placeholder.remove();
          const item = document.createElement("div");
          item.className = "log-item ok";
          item.innerHTML = `<span class="log-name">${escapeHtml(d.name)}</span><span class="log-conf">${d.confidence}% · ${new Date().toLocaleTimeString("en-GB").slice(0, 5)}</span>`;
          log.prepend(item);
          while (log.children.length > 30) log.removeChild(log.lastChild);
        }
      }
    } else {
      if (resultEl)
        resultEl.innerHTML = `<span style="color:var(--text3);font-size:13px">${d.confidence ? `No match (${d.confidence}% best)` : "No face detected"}</span>`;
    }
  } catch {}
}

/* ── Teacher Manual Attendance ────────────────────────────────────────── */
async function loadManualAttendance() {
  const dateVal = document.getElementById("tManualDate")?.value || todayStr();
  const grid = document.getElementById("manualAttGrid");
  if (!grid) return;
  grid.innerHTML = `<p class="muted">Loading…</p>`;

  try {
    const [stuR, attR] = await Promise.all([
      api("/students").then((r) => r?.json()),
      api(`/attendance?date=${dateVal}`).then((r) => r?.json()),
    ]);
    if (!stuR) return;
    const students = stuR.students || [];
    const attMap = {};
    (attR?.records || []).forEach((r) => {
      attMap[r.student_id] = r.status;
    });

    if (!students.length) {
      grid.innerHTML = `<p class="muted">No students enrolled.</p>`;
      return;
    }
    grid.innerHTML = `<div style="overflow-x:auto"><table class="data-table">
      <thead><tr><th>ID</th><th>Name</th><th>Department</th><th>Mark</th></tr></thead>
      <tbody>${students
        .map(
          (s) => `
        <tr>
          <td style="font-family:var(--mono);font-size:11px">${s.student_id}</td>
          <td>${escapeHtml(s.full_name)}</td>
          <td style="color:var(--text3)">${s.department || "—"}</td>
          <td>
            <select class="input-sm manual-status-sel" data-sid="${s.student_id}">
              <option value="Present" ${attMap[s.student_id] === "Present" ? "selected" : ""}>Present</option>
              <option value="Absent" ${!attMap[s.student_id] || attMap[s.student_id] === "Absent" ? "selected" : ""}>Absent</option>
            </select>
          </td>
        </tr>`,
        )
        .join("")}</tbody></table></div>`;
  } catch {
    grid.innerHTML = `<p class="msg err">Failed to load students.</p>`;
  }
}

async function saveAllManualAttendance() {
  const dateVal = document.getElementById("tManualDate")?.value || todayStr();
  const selects = document.querySelectorAll(".manual-status-sel");
  if (!selects.length) {
    toast("No students loaded", "err");
    return;
  }

  let saved = 0,
    failed = 0;
  for (const sel of selects) {
    const sid = sel.dataset.sid;
    const status = sel.value;
    try {
      const r = await api(`/attendance/${sid}/${dateVal}`, {
        method: "PUT",
        json: { status },
      });
      if (r?.ok) saved++;
      else failed++;
    } catch {
      failed++;
    }
  }
  toast(
    failed
      ? `Saved ${saved}, failed ${failed}`
      : `✓ ${saved} records saved for ${dateVal}`,
  );
}

/* ── Teacher Logs ─────────────────────────────────────────────────────── */
async function loadTeacherLogs() {
  const days = parseInt(document.getElementById("tLogDays")?.value || 30);
  const tbody = document.getElementById("tLogsBody");
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--text3);padding:2rem">Loading…</td></tr>`;

  try {
    const r = await api(`/logs?limit=${days * 30}`);
    if (!r || !r.ok) throw new Error("failed");
    const d = await r.json();
    const logs = d.logs || [];

    if (!logs.length) {
      tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--text3);padding:2rem">No logs found.</td></tr>`;
      return;
    }
    tbody.innerHTML = logs
      .map(
        (l) => `
        <tr>
          <td style="font-family:var(--mono);font-size:11px">${(l.logged_at || "").slice(0, 10)}</td>
          <td style="font-family:var(--mono);font-size:11px;color:var(--text3)">${escapeHtml(l.student_id || "—")}</td>
          <td>${escapeHtml(l.full_name || "")}</td>
          <td style="font-family:var(--mono);font-size:11px">${(l.logged_at || "").slice(11, 16)}</td>
          <td>${badge(l.recognized ? "Present" : "Absent")}</td>
          <td style="color:var(--text3);font-size:11px">${l.confidence ? l.confidence + "%" : "—"}</td>
        </tr>`,
      )
      .join("");
  } catch {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--red);padding:2rem">Failed to load logs.</td></tr>`;
  }
}

/* ══════════════════════════════════════════════════════════════════════
   STUDENT PORTAL
══════════════════════════════════════════════════════════════════════ */
async function loadStudentPortal() {
  const panel = document.getElementById("student-panel");
  if (!panel) return;
  panel.style.display = "flex";

  const email = localStorage.getItem("frs_student_email") || "";

  try {
    const r = await fetch(`${API}/students`, {
      headers: { Authorization: `Bearer ${authToken}` },
    });
    if (!r.ok) throw new Error();
    const d = await r.json();
    const students = d.students || [];
    const student = email
      ? students.find((s) => s.email?.toLowerCase() === email.toLowerCase())
      : null;

    if (!student) {
      const wn = document.getElementById("studentWelcomeName");
      if (wn) wn.textContent = "Welcome";
      const ws = document.getElementById("studentWelcomeSub");
      if (ws)
        ws.textContent = email
          ? `No student record found for ${email}`
          : "Please log in with your email.";
      return;
    }

    const hn = document.getElementById("studentHeaderName");
    if (hn) hn.textContent = student.student_id;
    const wn = document.getElementById("studentWelcomeName");
    if (wn) wn.textContent = `Welcome, ${student.full_name}`;
    const ws = document.getElementById("studentWelcomeSub");
    if (ws)
      ws.textContent = `${student.department || ""} · ${student.email || ""}`;

    // Load full profile
    const pr = await fetch(`${API}/students/${student.student_id}`, {
      headers: { Authorization: `Bearer ${authToken}` },
    });
    if (!pr.ok) throw new Error();
    const p = await pr.json();

    const pct = p.stats?.percentage || 0;
    const color =
      pct >= 75 ? "var(--green)" : pct >= 50 ? "var(--amber)" : "var(--red)";

    const sp = document.getElementById("sStat-present");
    if (sp) sp.textContent = p.stats?.total_present || 0;
    const st = document.getElementById("sStat-total");
    if (st) st.textContent = p.stats?.total_days || 0;
    const sc = document.getElementById("sStat-pct");
    if (sc) {
      sc.textContent = `${pct}%`;
      sc.style.color = color;
    }

    // Per-subject breakdown
    const subjectEl = document.getElementById("studentSubjectSummary");
    if (subjectEl) {
      const subs = p.by_subject || [];
      if (!subs.length) {
        subjectEl.innerHTML = `<div class="text-muted text-12px p-1rem">No subject-wise records yet. (Overall: ${pct}%)</div>`;
      } else {
        subjectEl.innerHTML = subs
          .map((s) => {
            const sp = s.pct !== null ? parseFloat(s.pct) : null;
            const sc =
              sp === null
                ? "var(--text3)"
                : sp < 60
                  ? "var(--danger)"
                  : sp < 75
                    ? "var(--amber)"
                    : "var(--green)";
            return `<div style="padding:0.5rem 0;border-bottom:1px solid var(--border)">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">
              <span class="text-13px"><span class="subject-tag" style="font-size:11px">${escapeHtml(s.subject_code)}</span> ${escapeHtml(s.subject_name)}</span>
              <span style="font-size:12px;font-weight:600;color:${sc}">${sp !== null ? sp + "%" : "—"}</span>
            </div>
            <div style="height:4px;background:var(--border);border-radius:2px">
              <div style="height:4px;width:${sp ?? 0}%;background:${sc};border-radius:2px;transition:width .4s"></div>
            </div>
            <div style="font-size:11px;color:var(--text3);margin-top:2px">${s.present}/${s.total} classes</div>
          </div>`;
          })
          .join("");
      }
    }

    const attBody = document.getElementById("studentAttBody");
    if (attBody) {
      const records = (p.attendance || []).slice(0, 90);
      if (!records.length) {
        attBody.innerHTML = `<tr><td colspan="4" class="text-center text-muted p-2rem">No records yet.</td></tr>`;
      } else {
        attBody.innerHTML = records
          .map((rec) => {
            const sc =
              rec.status === "Present"
                ? "color:var(--green)"
                : "color:var(--danger)";
            const sub = rec.subject_code
              ? `<span class="subject-tag" style="font-size:10px">${escapeHtml(rec.subject_code)}</span>`
              : `<span style="color:var(--text3)">—</span>`;
            return `<tr>
            <td style="font-family:var(--mono);font-size:11px">${rec.date}</td>
            <td style="font-size:11px">${sub}</td>
            <td style="color:var(--text3);font-size:11px">—</td>
            <td style="${sc};font-weight:600;font-size:12px">${rec.status}</td>
          </tr>`;
          })
          .join("");
      }
    }
  } catch (e) {
    console.error("loadStudentPortal:", e);
    const ws = document.getElementById("studentWelcomeSub");
    if (ws)
      ws.textContent = "Could not load your attendance data. Check connection.";
  }
}

/* ═══════════════════════════════════════════════════════════════════════
   TIMETABLE MANAGEMENT  (Admin — Manage → Timetable tab)
═══════════════════════════════════════════════════════════════════════ */
let _ttSlots = [],
  _ttTeachers = [],
  _ttSubjects = [],
  _ttData = [];

async function loadTimetable() {
  const facId = document.getElementById("ttFacultyFilter")?.value || "";
  const sem = document.getElementById("ttSemesterFilter")?.value || "";
  const grid = document.getElementById("timetableGrid");
  if (!grid) return;
  if (!facId || !sem) {
    grid.innerHTML = `<div class="text-muted text-center p-2rem text-13px">Select a faculty and semester to view the timetable</div>`;
    return;
  }
  grid.innerHTML = `<div class="text-muted text-center p-1rem">Loading…</div>`;
  try {
    const r = await api(`/timetable?faculty_id=${facId}&semester=${sem}`);
    if (!r) return;
    const d = await r.json();
    _ttSlots = d.slots || [];
    _ttData = d.timetable || [];
    _renderTimetableGrid(facId, sem);
  } catch (e) {
    grid.innerHTML = `<div class="msg err">Failed to load timetable</div>`;
  }
}

function _renderTimetableGrid(facId, sem) {
  const grid = document.getElementById("timetableGrid");
  if (!grid) return;
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

  // Build lookup: day+slotId → entry
  const lookup = {};
  _ttData.forEach((e) => {
    lookup[`${e.day_of_week}_${e.time_slot_id}`] = e;
  });

  const dayLabels = {
    Mon: "Monday",
    Tue: "Tuesday",
    Wed: "Wednesday",
    Thu: "Thursday",
    Fri: "Friday",
    Sat: "Saturday",
  };

  grid.innerHTML = `
    <div class="tt-grid">
      <div class="tt-head-cell tt-corner"></div>
      ${days.map((d) => `<div class="tt-head-cell">${dayLabels[d]}</div>`).join("")}
      ${_ttSlots
        .map(
          (slot) => `
        <div class="tt-slot-label">
          <div class="tt-slot-name">${slot.label}</div>
          <div class="tt-slot-time">${(slot.start_time || "").slice(0, 5)}–${(slot.end_time || "").slice(0, 5)}</div>
        </div>
        ${days
          .map((day) => {
            const e = lookup[`${day}_${slot.id}`];
            if (e) {
              return `<div class="tt-cell tt-cell-filled" title="${e.teacher_name || "?"}">
              <div class="tt-cell-subject">${escapeHtml(e.subject_code || e.subject_name || "—")}</div>
              <div class="tt-cell-teacher">${escapeHtml(e.teacher_name || "")}</div>
              <button class="tt-cell-del" onclick="deleteTimetableEntry(${e.id})" title="Remove">✕</button>
            </div>`;
            }
            return `<div class="tt-cell tt-cell-empty" onclick="quickAssignSlot('${day}',${slot.id},'${slot.label}',${facId},${sem})">
            <span class="tt-cell-add">+</span>
          </div>`;
          })
          .join("")}
      `,
        )
        .join("")}
    </div>`;
}

async function openTimetableModal() {
  document.getElementById("ttmConflict").textContent = "";
  document.getElementById("ttmErr").textContent = "";
  await _loadTimetableModalDropdowns();
  const m = document.getElementById("timetableModal");
  m.style.display = "";   // clear any inline display:none from closeModal
  m.classList.add("open");
}

async function _loadTimetableModalDropdowns() {
  // Faculties — rebuild every time (HTML ships with 1 placeholder option that blocks the guard)
  const facSel = document.getElementById("ttmFaculty");
  if (facSel) {
    const faculties = await _loadFaculties();
    const prev = facSel.value;
    facSel.innerHTML =
      `<option value="">Select faculty</option>` +
      faculties
        .map((f) => `<option value="${f.id}">${escapeHtml(f.name)}</option>`)
        .join("");
    if (prev) facSel.value = prev;
  }
  // Slots
  const slotSel = document.getElementById("ttmSlot");
  if (slotSel && _ttSlots.length) {
    const prev = slotSel.value;
    slotSel.innerHTML =
      `<option value="">Select period</option>` +
      _ttSlots
        .map(
          (s) =>
            `<option value="${s.id}">${s.label} (${(s.start_time || "").slice(0, 5)}–${(s.end_time || "").slice(0, 5)})</option>`,
        )
        .join("");
    if (prev) slotSel.value = prev;
  }
  // Teachers
  const teachSel = document.getElementById("ttmTeacher");
  if (teachSel) {
    const prev = teachSel.value;
    try {
      const r = await api("/teachers");
      if (r && r.ok) {
        const d = await r.json();
        teachSel.innerHTML =
          `<option value="">Unassigned</option>` +
          (d.teachers || [])
            .map(
              (t) => `<option value="${t.id}">${escapeHtml(t.full_name)}</option>`,
            )
            .join("");
        if (prev) teachSel.value = prev;
      }
    } catch (_) {}
  }
}

async function ttmLoadSubjects() {
  const fid = document.getElementById("ttmFaculty")?.value || "";
  const sem = document.getElementById("ttmSemester")?.value || "";
  const sel = document.getElementById("ttmSubject");
  if (!sel) return;
  if (!fid) {
    sel.innerHTML = `<option value="">— select faculty first —</option>`;
    return;
  }
  sel.innerHTML = `<option value="">Loading…</option>`;
  const qs = `faculty_id=${fid}${sem ? "&semester=" + sem : ""}`;
  const r = await api(`/subjects?${qs}`);
  if (!r) return;
  const d = await r.json();
  sel.innerHTML =
    `<option value="">Select subject</option>` +
    (d.subjects || [])
      .map(
        (s) =>
          `<option value="${s.id}">${escapeHtml(s.code)} — ${escapeHtml(s.name)}</option>`,
      )
      .join("");
}

async function checkTimetableConflict() {
  const fid = document.getElementById("ttmFaculty")?.value;
  const sem = document.getElementById("ttmSemester")?.value;
  const day = document.getElementById("ttmDay")?.value;
  const sid = document.getElementById("ttmSlot")?.value;
  const conflEl = document.getElementById("ttmConflict");
  if (!fid || !sem || !day || !sid) {
    conflEl.textContent = "Fill all required fields first";
    return;
  }
  const r = await api("/timetable/check", {
    method: "POST",
    json: {
      faculty_id: +fid,
      semester: +sem,
      day_of_week: day,
      time_slot_id: +sid,
    },
  });
  if (!r) return;
  const d = await r.json();
  if (d.available) {
    conflEl.style.color = "var(--green)";
    conflEl.textContent = "✓ Slot is available";
  } else {
    conflEl.style.color = "var(--red)";
    conflEl.textContent = "✕ " + (d.message || "Slot occupied");
  }
}

async function saveTimetableEntry() {
  const fid = document.getElementById("ttmFaculty")?.value;
  const sem = document.getElementById("ttmSemester")?.value;
  const day = document.getElementById("ttmDay")?.value;
  const slotId = document.getElementById("ttmSlot")?.value;
  const teachId = document.getElementById("ttmTeacher")?.value;
  const subjId = document.getElementById("ttmSubject")?.value;
  const errEl = document.getElementById("ttmErr");
  errEl.textContent = "";
  if (!fid || !sem || !day || !slotId) {
    errEl.textContent = "Faculty, Semester, Day, Period are required";
    return;
  }
  const r = await api("/timetable", {
    method: "POST",
    json: {
      faculty_id: +fid,
      semester: +sem,
      day_of_week: day,
      time_slot_id: +slotId,
      teacher_id: teachId ? +teachId : null,
      subject_id: subjId ? +subjId : null,
    },
  });
  if (!r) return;
  const d = await r.json();
  if (!r.ok) {
    errEl.textContent = d.error || "Save failed";
    return;
  }
  toast("Timetable entry saved");
  closeModal("timetableModal");
  loadTimetable();
}

async function deleteTimetableEntry(id) {
  if (!confirm("Remove this timetable entry?")) return;
  const r = await api(`/timetable/${id}`, { method: "DELETE" });
  if (r?.ok) {
    toast("Entry removed");
    loadTimetable();
  } else toast("Delete failed", "err");
}

async function quickAssignSlot(day, slotId, slotLabel, facId, sem) {
  document.getElementById("ttmConflict").textContent = "";
  document.getElementById("ttmErr").textContent = "";
  // Load dropdowns first, then set pre-filled values (innerHTML rebuild wipes .value)
  await _loadTimetableModalDropdowns();
  document.getElementById("ttmFaculty").value = facId;
  document.getElementById("ttmSemester").value = sem;
  document.getElementById("ttmDay").value = day;
  document.getElementById("ttmSlot").value = slotId;
  await ttmLoadSubjects();
  const _m = document.getElementById("timetableModal");
  _m.style.display = "";
  _m.classList.add("open");
}

/* ═══════════════════════════════════════════════════════════════════════
   MANAGE → TIMETABLE TAB  (faculty filter population)
═══════════════════════════════════════════════════════════════════════ */
async function _populateTimetableFacultyFilter() {
  const sel = document.getElementById("ttFacultyFilter");
  if (!sel) return;
  // _populateFacultyDropdowns() already handles ttFacultyFilter — just ensure
  // it's populated if the cache is available, without a second API call.
  if (sel.options.length > 1) return; // already populated
  const faculties = await _loadFaculties();
  while (sel.options.length > 1) sel.remove(1);
  faculties.forEach((f) => {
    const o = document.createElement("option");
    o.value = f.id;
    o.textContent = f.name;
    sel.appendChild(o);
  });
}

/* ═══════════════════════════════════════════════════════════════════════
   ATTENDANCE SESSION FLOW
═══════════════════════════════════════════════════════════════════════ */
let _activeSession = null;
let _sessionAssignment = null;

async function openSessionModal(assignmentId) {
  // Fetch assignment details from teacher's schedule
  try {
    const r = await api("/teacher/me/schedule");
    if (!r) return;
    const d = await r.json();
    const allAssignments = d.all || [];
    const cls = allAssignments.find((a) => a.assignment_id === assignmentId);
    if (!cls) {
      toast("Assignment not found", "err");
      return;
    }

    _sessionAssignment = cls;
    document.getElementById("sessionSubjectId").value = cls.subject_id || "";
    document.getElementById("sessionFacultyId").value = cls.faculty_id || "";
    document.getElementById("sessionSemester").value = cls.semester || "";
    document.getElementById("sessionDayOfWeek").value = cls.day_of_week || "";
    document.getElementById("sessionTimeSlotId").value = cls.time_slot_id || "";
    document.getElementById("sessionDate").value = todayStr();
    document.getElementById("sessionSubjectDisplay").textContent =
      `${cls.subject_name || "Unknown"} · ${cls.faculty_name || ""} · Semester ${cls.semester}`;
    document.getElementById("sessionStartErr").textContent = "";
    document.getElementById("sessionStartModal").classList.add("open");
  } catch (e) {
    toast("Failed to load class info", "err");
  }
}

async function confirmStartSession() {
  const errEl = document.getElementById("sessionStartErr");
  errEl.textContent = "";
  const subjId = document.getElementById("sessionSubjectId").value;
  const facId = document.getElementById("sessionFacultyId").value;
  const sem = document.getElementById("sessionSemester").value;
  const day = document.getElementById("sessionDayOfWeek").value;
  const slotId = document.getElementById("sessionTimeSlotId").value;
  const sesDate = document.getElementById("sessionDate").value || todayStr();
  const method =
    document.querySelector("input[name='sessionMethod']:checked")?.value ||
    "manual";

  if (!subjId || !facId || !sem) {
    errEl.textContent = "Missing class information";
    return;
  }

  const r = await api("/attendance/sessions", {
    method: "POST",
    json: {
      faculty_id: +facId,
      semester: +sem,
      subject_id: +subjId,
      time_slot_id: slotId ? +slotId : null,
      day_of_week: day,
      session_date: sesDate,
      method,
    },
  });
  if (!r) return;
  const d = await r.json();
  if (!r.ok && !d.session) {
    errEl.textContent = d.error || "Failed to start session";
    return;
  }

  _activeSession = d.session;
  closeModal("sessionStartModal");
  toast(d.resumed ? "Resumed existing session" : "Session started");

  // Navigate to the correct method panel
  navigate("t-recognize");
  selectAttMethod(method, document.getElementById(`mBtn-${method}`));
  if (method === "manual") loadManualSessionStudents(_activeSession.id);
  else if (method === "camera") startTeacherWebcam();
}

function continueSession(sessionId, subjectName) {
  navigate("t-recognize");
  _activeSession = { id: sessionId };
  toast(`Continuing session for ${subjectName}`);
  loadManualSessionStudents(sessionId);
}

function viewSessionReport(sessionId) {
  toast("Session report coming soon");
}

/* ── Attendance method switcher ────────────────────────────────────── */
let _currentAttMethod = "camera";

function selectAttMethod(method, btn) {
  _currentAttMethod = method;
  document
    .querySelectorAll(".att-method-btn")
    .forEach((b) => b.classList.remove("active"));
  if (btn) btn.classList.add("active");
  document.getElementById("photoAttPanel")?.classList.add("hidden");
  document.getElementById("manualAttPanel")?.classList.add("hidden");
  // Live camera is the default panel (already visible)
  const liveArea = document.querySelector(".teacher-recog-area");
  if (method === "photo") {
    if (liveArea) liveArea.style.display = "none";
    document.getElementById("photoAttPanel")?.classList.remove("hidden");
  } else if (method === "manual") {
    if (liveArea) liveArea.style.display = "none";
    document.getElementById("manualAttPanel")?.classList.remove("hidden");
    if (_activeSession) loadManualSessionStudents(_activeSession.id);
  } else {
    if (liveArea) liveArea.style.display = "";
  }
}

/* ── Manual attendance list ──────────────────────────────────────── */
let _manualStudents = [];

async function loadManualSessionStudents(sessionId) {
  const listEl = document.getElementById("manualStudentList");
  if (!listEl) return;
  listEl.innerHTML = `<div class="text-muted text-13px p-1rem">Loading students…</div>`;
  try {
    const r = await api(`/attendance/sessions/${sessionId}`);
    if (!r) return;
    const d = await r.json();
    const sess = d.session;
    _manualStudents = sess?.students || [];
    _renderManualList();
  } catch {
    listEl.innerHTML = `<div class="msg err">Failed to load students</div>`;
  }
}

function _renderManualList() {
  const listEl = document.getElementById("manualStudentList");
  if (!listEl) return;
  if (!_manualStudents.length) {
    listEl.innerHTML = `<div class="text-muted text-13px p-1rem">No students found for this class</div>`;
    return;
  }
  listEl.innerHTML = _manualStudents
    .map(
      (s, i) => `
    <div class="manual-student-row" id="msr-${i}">
      <div class="msr-info">
        <div class="msr-name">${escapeHtml(s.full_name)}</div>
        <div class="msr-id">${s.student_id}</div>
      </div>
      <div class="msr-toggle">
        <button class="msr-btn ${s.status === "Present" ? "active" : ""}"
                onclick="toggleManualStatus(${i},'Present')" id="msrP-${i}">Present</button>
        <button class="msr-btn msr-absent ${s.status === "Absent" ? "active" : ""}"
                onclick="toggleManualStatus(${i},'Absent')" id="msrA-${i}">Absent</button>
      </div>
    </div>`,
    )
    .join("");
}

function toggleManualStatus(idx, status) {
  _manualStudents[idx].status = status;
  const pBtn = document.getElementById(`msrP-${idx}`);
  const aBtn = document.getElementById(`msrA-${idx}`);
  pBtn?.classList.toggle("active", status === "Present");
  aBtn?.classList.toggle("active", status === "Absent");
}

function markAllPresent() {
  _manualStudents.forEach((_, i) => toggleManualStatus(i, "Present"));
}
function markAllAbsent() {
  _manualStudents.forEach((_, i) => toggleManualStatus(i, "Absent"));
}

async function submitManualAttendance() {
  if (!_activeSession) {
    toast("No active session", "err");
    return;
  }
  const records = _manualStudents.map((s) => ({
    student_id: s.student_id,
    status: s.status || "Absent",
    note: "",
  }));
  const r = await api(`/attendance/sessions/${_activeSession.id}/bulk`, {
    method: "POST",
    json: { records },
  });
  if (!r) return;
  const d = await r.json();
  if (r.ok) {
    toast(`Attendance submitted — ${d.newly_marked} new records`);
    _activeSession = null;
    navigate("t-dashboard");
  } else {
    toast(d.error || "Submit failed", "err");
  }
}

/* ── Photo / Classroom attendance ───────────────────────────────── */
let _photoDetected = [];

async function startPhotoCamera() {
  try {
    if (_photoCamStream) _photoCamStream.getTracks().forEach((t) => t.stop());
    const constraints = { video: { facingMode: _photoCamFacing } };
    _photoCamStream = await navigator.mediaDevices.getUserMedia(constraints);
    const video = document.getElementById("photoAttVideo");
    if (!video) return;
    video.srcObject = _photoCamStream;
    video.style.display = "block";
    document.getElementById("photoAttOverlay").style.display = "none";
    document.getElementById("btnCapturePhoto").style.display = "block";
    document.getElementById("btnSwitchPhotoCam")?.classList.remove("hidden");
  } catch (e) {
    toast("Camera access denied", "err");
  }
}

async function switchPhotoCamera() {
  _photoCamFacing = _photoCamFacing === "environment" ? "user" : "environment";
  await startPhotoCamera();
}

async function captureClassPhoto() {
  const video = document.getElementById("photoAttVideo");
  const canvas = document.getElementById("photoAttCanvas");
  if (!video || !canvas) return;
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d").drawImage(video, 0, 0);
  const b64 = canvas.toDataURL("image/jpeg", 0.85).split(",")[1];

  // Stop camera immediately after capture
  _stopPhotoCamera();

  await _processClassroomImage(b64);
}

function _stopPhotoCamera() {
  if (_photoCamStream) {
    _photoCamStream.getTracks().forEach((t) => t.stop());
    _photoCamStream = null;
  }
  const video = document.getElementById("photoAttVideo");
  if (video) {
    video.srcObject = null;
    video.style.display = "none";
  }
  document.getElementById("photoAttOverlay").style.display = "flex";
  document.getElementById("btnCapturePhoto").style.display = "none";
  document.getElementById("btnSwitchPhotoCam")?.classList.add("hidden");
}

function handlePhotoUpload(event) {
  const file = event.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = async (e) => {
    const b64 = e.target.result.split(",")[1];
    await _processClassroomImage(b64);
  };
  reader.readAsDataURL(file);
}

async function _processClassroomImage(b64) {
  const previewEl = document.getElementById("photoAttPreview");
  const resultEl = document.getElementById("photoAttResult");
  if (previewEl)
    previewEl.innerHTML = `<div class="text-muted text-13px text-center p-1rem">Processing…</div>`;
  if (resultEl) resultEl.classList.add("hidden");
  try {
    const r = await api("/recognize/batch", {
      method: "POST",
      json: { image: b64 },
    });
    if (!r) return;
    const d = await r.json();
    _photoDetected = d.recognized || [];
    const countEl = document.getElementById("photoDetectedCount");
    if (countEl)
      countEl.textContent = `${_photoDetected.length} identified, ${d.unknown || 0} unknown`;
    const listEl = document.getElementById("photoDetectedList");
    if (listEl) {
      if (!_photoDetected.length) {
        listEl.innerHTML = `<div class="text-muted text-13px p-0-75rem">No students identified</div>`;
      } else {
        listEl.innerHTML = _photoDetected
          .map(
            (s, i) => `
          <div class="photo-student-row">
            <div class="msr-info">
              <div class="msr-name">${escapeHtml(s.name)}</div>
              <div class="msr-id">${s.student_id} · ${s.confidence}% confidence</div>
            </div>
            <div class="msr-toggle">
              <button class="msr-btn ${s.status === "Present" ? "active" : ""}" onclick="togglePhotoStatus(${i},'Present')">Present</button>
              <button class="msr-btn msr-absent ${s.status === "Absent" ? "active" : ""}" onclick="togglePhotoStatus(${i},'Absent')">Absent</button>
            </div>
          </div>`,
          )
          .join("");
      }
    }
    if (previewEl)
      previewEl.innerHTML = `<img src="data:image/jpeg;base64,${b64}" style="max-width:100%;border-radius:6px">`;
    if (resultEl) resultEl.classList.remove("hidden");
  } catch {
    toast("Recognition failed", "err");
  }
}

function togglePhotoStatus(idx, status) {
  _photoDetected[idx].status = status;
  const rows = document.querySelectorAll(".photo-student-row");
  const row = rows[idx];
  if (!row) return;
  row.querySelectorAll(".msr-btn").forEach((b) => b.classList.remove("active"));
  row
    .querySelector(
      `.msr-btn${status === "Absent" ? ".msr-absent" : ""}:not(.msr-absent)`,
    )
    ?.classList.add("active");
  if (status === "Absent")
    row.querySelectorAll(".msr-btn")[1]?.classList.add("active");
  else row.querySelectorAll(".msr-btn")[0]?.classList.add("active");
}

function retakePhoto() {
  _stopPhotoCamera();
  document.getElementById("photoAttResult")?.classList.add("hidden");
  document.getElementById("photoAttPreview").innerHTML =
    `<div class="text-muted text-13px text-center p-2rem">Capture or upload a photo to detect faces</div>`;
  const overlay = document.getElementById("photoAttOverlay");
  if (overlay) overlay.style.display = "flex";
  _photoDetected = [];
}

async function submitPhotoAttendance() {
  if (!_activeSession) {
    toast("No active session — start from dashboard", "err");
    return;
  }
  if (!_photoDetected.length) {
    toast("No detected students to submit", "err");
    return;
  }
  const records = _photoDetected.map((s) => ({
    student_id: s.student_id,
    status: s.status || "Present",
  }));
  const r = await api(`/attendance/sessions/${_activeSession.id}/bulk`, {
    method: "POST",
    json: { records },
  });
  if (!r) return;
  const d = await r.json();
  if (r.ok) {
    toast("Attendance submitted successfully");
    _activeSession = null;
    navigate("t-dashboard");
  } else {
    toast(d.error || "Submit failed", "err");
  }
}

/* day_of_week support is built into addTeacherAssignment above */

/* ── Teacher Reports ──────────────────────────────────────────────────── */

let _rptData = [];

async function loadTeacherReports() {
  // Set default date range if empty
  const fromEl = document.getElementById("rptFrom");
  const toEl = document.getElementById("rptTo");
  if (fromEl && !fromEl.value) {
    const d = new Date();
    d.setDate(d.getDate() - 30);
    fromEl.value = d.toISOString().slice(0, 10);
  }
  if (toEl && !toEl.value) toEl.value = new Date().toISOString().slice(0, 10);

  const params = new URLSearchParams({
    from: fromEl?.value || "",
    to: toEl?.value || "",
    faculty_id: document.getElementById("rptFaculty")?.value || "",
    semester: document.getElementById("rptSemester")?.value || "",
    subject_id: document.getElementById("rptSubject")?.value || "",
  });

  const body = document.getElementById("rptBody");
  if (body)
    body.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--text3);padding:2rem">Loading…</td></tr>`;

  try {
    const r = await api(`/reports/teacher-performance?${params}`);
    if (!r || !r.ok) {
      toast("Failed to load report", "err");
      return;
    }
    const data = await r.json();
    _rptData = data.students || [];

    // Populate Faculty filter (from teacher's assigned faculties only)
    const facSel = document.getElementById("rptFaculty");
    if (facSel && data.faculties?.length) {
      const cur = facSel.value;
      facSel.innerHTML = '<option value="">All Faculties</option>';
      data.faculties.forEach((f) => {
        const o = document.createElement("option");
        o.value = f.id;
        o.text = `${f.code} — ${f.name}`;
        if (String(f.id) === String(cur)) o.selected = true;
        facSel.add(o);
      });
    }

    // Populate Subject filter (from teacher's assigned subjects only)
    const subSel = document.getElementById("rptSubject");
    if (subSel && data.subjects?.length) {
      const cur = subSel.value;
      subSel.innerHTML = '<option value="">All Subjects</option>';
      data.subjects.forEach((s) => {
        const o = document.createElement("option");
        o.value = s.id;
        o.text = `${s.name} (${s.code})`;
        if (String(s.id) === String(cur)) o.selected = true;
        subSel.add(o);
      });
    }

    _renderRptTable(_rptData);
    _renderRptMetrics(_rptData);
    _renderRptRiskSummary(_rptData);
  } catch (e) {
    console.error("loadTeacherReports:", e);
    toast("Report error", "err");
  }
}

function _riskLabel(pct) {
  if (pct === null || pct === undefined)
    return { label: "No Data", cls: "risk-nodata" };
  if (pct < 60) return { label: "Critical", cls: "risk-critical" };
  if (pct < 75) return { label: "At Risk", cls: "risk-atrisk" };
  return { label: "Good", cls: "risk-good" };
}

function _renderRptTable(rows) {
  const body = document.getElementById("rptBody");
  if (!body) return;
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--text3);padding:2rem">No students found for the selected filters.</td></tr>`;
    return;
  }
  body.innerHTML = rows
    .map((s) => {
      const pct = s.pct !== null ? parseFloat(s.pct) : null;
      const risk = _riskLabel(pct);
      const barW = pct !== null ? Math.round(pct) : 0;
      const barColor =
        pct === null
          ? "var(--text3)"
          : pct < 60
            ? "var(--red)"
            : pct < 75
              ? "var(--amber)"
              : "var(--green)";
      return `<tr>
      <td style="font-weight:500">${escapeHtml(s.full_name)}<br><span style="font-size:11px;color:var(--text3)">${escapeHtml(s.student_id)}</span></td>
      <td>${escapeHtml(s.department)}<br><span style="font-size:11px;color:var(--text3)">Sem ${s.semester}</span></td>
      <td style="color:var(--green);font-weight:600">${s.present}</td>
      <td style="color:var(--red)">${s.absent}</td>
      <td>${s.total}</td>
      <td>
        <div style="display:flex;align-items:center;gap:0.5rem">
          <div style="flex:1;height:6px;background:var(--bg3);border-radius:3px;min-width:60px">
            <div style="height:100%;width:${barW}%;background:${barColor};border-radius:3px;transition:width 0.3s"></div>
          </div>
          <span style="font-size:12px;font-weight:600;color:${barColor};min-width:36px">${pct !== null ? pct + "%" : "—"}</span>
        </div>
      </td>
      <td><span class="risk-badge ${risk.cls}">${risk.label}</span></td>
    </tr>`;
    })
    .join("");
}

function _renderRptMetrics(rows) {
  const el = document.getElementById("rptMetrics");
  if (!el) return;
  const total = rows.length;
  const withAtt = rows.filter((r) => r.total > 0);
  const avgPct = withAtt.length
    ? Math.round(
        withAtt.reduce((s, r) => s + parseFloat(r.pct || 0), 0) /
          withAtt.length,
      )
    : 0;
  const critical = rows.filter(
    (r) => r.pct !== null && parseFloat(r.pct) < 60,
  ).length;
  const atRisk = rows.filter(
    (r) => r.pct !== null && parseFloat(r.pct) >= 60 && parseFloat(r.pct) < 75,
  ).length;
  const good = rows.filter(
    (r) => r.pct !== null && parseFloat(r.pct) >= 75,
  ).length;

  el.innerHTML = `
    <div class="metric-card">
      <div class="metric-label">Total Students</div>
      <div class="metric-val">${total}</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Avg Attendance</div>
      <div class="metric-val" style="color:var(--blue)">${avgPct}%</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Critical (&lt;60%)</div>
      <div class="metric-val" style="color:var(--red)">${critical}</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">At Risk (&lt;75%)</div>
      <div class="metric-val" style="color:var(--amber)">${atRisk}</div>
    </div>
    <div class="metric-card">
      <div class="metric-label">Good (≥75%)</div>
      <div class="metric-val" style="color:var(--green)">${good}</div>
    </div>`;
}

function _renderRptRiskSummary(rows) {
  const el = document.getElementById("rptRiskSummary");
  if (!el) return;
  const critical = rows.filter((r) => r.pct !== null && parseFloat(r.pct) < 60);
  const atRisk = rows.filter(
    (r) => r.pct !== null && parseFloat(r.pct) >= 60 && parseFloat(r.pct) < 75,
  );
  if (!critical.length && !atRisk.length) {
    el.innerHTML = "";
    return;
  }

  const makeCard = (students, title, cls) =>
    students.length
      ? `
    <div class="risk-card ${cls}">
      <div class="risk-card-title">${title} (${students.length})</div>
      <div class="risk-card-list">
        ${students
          .slice(0, 5)
          .map(
            (s) =>
              `<div class="risk-card-row">
            <span>${escapeHtml(s.full_name)}</span>
            <span style="font-weight:600">${s.pct !== null ? s.pct + "%" : "—"}</span>
          </div>`,
          )
          .join("")}
        ${students.length > 5 ? `<div style="font-size:11px;color:var(--text3);padding-top:0.25rem">+${students.length - 5} more…</div>` : ""}
      </div>
    </div>`
      : "";

  el.innerHTML =
    makeCard(critical, "⚠ Critical — Below 60%", "risk-card-critical") +
    makeCard(atRisk, "At Risk — Below 75%", "risk-card-atrisk");
}

function _filterRptTable() {
  const q = (
    document.getElementById("rptSearch")?.value ||
    document.getElementById("rptStudentFilter")?.value ||
    ""
  ).toLowerCase();
  const filtered = q
    ? _rptData.filter(
        (r) =>
          (r.full_name || "").toLowerCase().includes(q) ||
          (r.student_id || "").toLowerCase().includes(q) ||
          (r.department || "").toLowerCase().includes(q),
      )
    : _rptData;
  _renderRptTable(filtered);
}

function exportReportCSV() {
  if (!_rptData.length) {
    toast("No data to export", "err");
    return;
  }
  const rows = [
    [
      "Student ID",
      "Name",
      "Department",
      "Semester",
      "Present",
      "Absent",
      "Total",
      "Attendance %",
      "Status",
    ],
    ..._rptData.map((s) => {
      const pct = s.pct !== null ? s.pct : "";
      const risk = _riskLabel(s.pct !== null ? parseFloat(s.pct) : null).label;
      return [
        s.student_id,
        s.full_name,
        s.department,
        s.semester,
        s.present,
        s.absent,
        s.total,
        pct,
        risk,
      ];
    }),
  ];
  const csv = rows
    .map((r) => r.map((v) => `"${String(v).replace(/"/g, '""')}"`).join(","))
    .join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `attendance_report_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
  toast("CSV downloaded");
}

// ══════════════════════════════════════════════════════════════════════════
//  REPORT TABS  (Overview / Defaulters / Corrections)
// ══════════════════════════════════════════════════════════════════════════

function switchReportTab(tab, btn) {
  document
    .querySelectorAll("#page-reports .sub-tab")
    .forEach((t) => t.classList.remove("active"));
  document
    .querySelectorAll("#page-reports .sub-tab-panel")
    .forEach((p) => p.classList.remove("active"));
  if (btn) btn.classList.add("active");
  const panel = document.getElementById(`rtab-${tab}`);
  if (panel) panel.classList.add("active");
  if (tab === "defaulters") loadDefaulters();
  if (tab === "corrections") loadAdminCorrections();
}

// ── Defaulter List ────────────────────────────────────────────────────────

let _defData = [];

async function loadDefaulters() {
  const faculty = document.getElementById("defFaculty")?.value || "";
  const semester = document.getElementById("defSemester")?.value || "";
  const subject = document.getElementById("defSubject")?.value || "";
  const threshold = document.getElementById("defThreshold")?.value || 75;
  const body = document.getElementById("defBody");
  if (!body) return;
  body.innerHTML = `<tr><td colspan="8" class="text-center text-muted p-2rem">Loading…</td></tr>`;

  const qs = new URLSearchParams({ threshold });
  if (faculty) qs.set("faculty_id", faculty);
  if (semester) qs.set("semester", semester);
  if (subject) qs.set("subject_id", subject);

  const res = await api(`/reports/defaulters?${qs}`);
  const data = await res.json();
  _defData = data.defaulters || [];

  const summary = document.getElementById("defSummary");
  if (summary)
    summary.textContent = `${_defData.length} student-subject combinations below ${threshold}%`;

  // Populate subject dropdown from result
  const subs = [...new Map(_defData.map((r) => [r.subject_id, r])).values()];
  const subjEl = document.getElementById("defSubject");
  if (subjEl && subjEl.options.length <= 1 && subs.length) {
    subs.forEach((s) => {
      const o = document.createElement("option");
      o.value = s.subject_id;
      o.textContent = `${s.subject_code} — ${s.subject_name}`;
      subjEl.appendChild(o);
    });
  }

  if (!_defData.length) {
    body.innerHTML = `<tr><td colspan="8" class="text-center text-muted p-2rem">No defaulters — all students meet the threshold.</td></tr>`;
    return;
  }
  body.innerHTML = _defData
    .map((r) => {
      const pct = r.pct !== null ? parseFloat(r.pct) : null;
      const cls =
        pct === null
          ? ""
          : pct < 60
            ? "color:var(--danger);font-weight:700"
            : pct < 75
              ? "color:var(--amber);font-weight:600"
              : "";
      return `<tr>
      <td><span class="mono text-12px">${escapeHtml(r.student_id)}</span></td>
      <td>${escapeHtml(r.full_name)}</td>
      <td>${escapeHtml(r.department || "—")}</td>
      <td>${r.semester || "—"}</td>
      <td><span class="subject-tag">${escapeHtml(r.subject_code)}</span> ${escapeHtml(r.subject_name)}</td>
      <td>${r.present}</td>
      <td>${r.total}</td>
      <td style="text-align:center;${cls}">${pct !== null ? pct + "%" : "—"}</td>
    </tr>`;
    })
    .join("");
}

function exportDefaultersCSV() {
  if (!_defData.length) {
    toast("No data to export", "err");
    return;
  }
  const rows = [
    [
      "Student ID",
      "Name",
      "Department",
      "Semester",
      "Subject Code",
      "Subject",
      "Present",
      "Total",
      "%",
    ],
    ..._defData.map((r) => [
      r.student_id,
      r.full_name,
      r.department || "",
      r.semester || "",
      r.subject_code,
      r.subject_name,
      r.present,
      r.total,
      r.pct ?? "",
    ]),
  ];
  const csv = rows
    .map((r) => r.map((v) => `"${String(v).replace(/"/g, '""')}"`).join(","))
    .join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `defaulters_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
  toast("Defaulters CSV downloaded");
}

// ── Admin Corrections View ────────────────────────────────────────────────

async function loadAdminCorrections() {
  const status = document.getElementById("corrStatusFilter")?.value || "";
  const body = document.getElementById("corrBody");
  if (!body) return;
  body.innerHTML = `<tr><td colspan="7" class="text-center text-muted p-2rem">Loading…</td></tr>`;
  const res = await api("/corrections");
  const data = await res.json();
  let rows = data.corrections || [];
  if (status) rows = rows.filter((r) => r.status === status);
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="7" class="text-center text-muted p-2rem">No correction requests found.</td></tr>`;
    return;
  }
  body.innerHTML = rows
    .map((r) => {
      const badge =
        r.status === "pending"
          ? `<span class="risk-badge risk-atrisk">Pending</span>`
          : r.status === "approved"
            ? `<span class="risk-badge risk-good">Approved</span>`
            : `<span class="risk-badge risk-critical">Rejected</span>`;
      const actions =
        r.status === "pending"
          ? `<button class="btn-sm btn-primary"    onclick="adminReviewCorrection(${r.id},'approved')">Approve</button>
         <button class="btn-sm btn-danger ml-4" onclick="adminReviewCorrection(${r.id},'rejected')">Reject</button>`
          : "—";
      return `<tr>
      <td>${escapeHtml(r.student_name || r.student_id)}</td>
      <td>${r.subject_code ? `<span class="subject-tag">${escapeHtml(r.subject_code)}</span>` : "—"}</td>
      <td class="mono text-12px">${r.date || "—"}</td>
      <td class="text-13px">${escapeHtml(r.reason)}</td>
      <td>${badge}</td>
      <td class="text-13px text-secondary">${r.reviewed_at ? r.reviewed_at.slice(0, 10) : "—"}</td>
      <td>${actions}</td>
    </tr>`;
    })
    .join("");
}

async function adminReviewCorrection(id, status) {
  const note =
    status === "rejected" ? prompt("Rejection reason (optional):") || "" : "";
  const res = await api(`/corrections/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status, review_note: note }),
  });
  const data = await res.json();
  if (data.error) {
    toast(data.error, "err");
    return;
  }
  toast(status === "approved" ? "Approved — attendance updated" : "Rejected");
  loadAdminCorrections();
}

// ══════════════════════════════════════════════════════════════════════════
//  BULK STUDENT IMPORT
// ══════════════════════════════════════════════════════════════════════════

function openImportStudentsModal() {
  document.getElementById("importCsvFile").value = "";
  document.getElementById("importErr").textContent = "";
  document.getElementById("importResult").style.display = "none";
  document.getElementById("importProgress").style.display = "none";
  document.getElementById("importStudentsModal").style.display = "flex";
}

async function submitStudentImport() {
  const fileEl = document.getElementById("importCsvFile");
  const errEl = document.getElementById("importErr");
  const prog = document.getElementById("importProgress");
  const result = document.getElementById("importResult");
  errEl.textContent = "";
  result.style.display = "none";

  if (!fileEl.files[0]) {
    errEl.textContent = "Please select a CSV file.";
    return;
  }

  prog.style.display = "block";
  document.getElementById("importProgressMsg").textContent =
    "Uploading and processing…";

  const form = new FormData();
  form.append("file", fileEl.files[0]);

  const res = await fetch(`${API}/students/import`, {
    method: "POST",
    headers: { Authorization: `Bearer ${authToken}` },
    body: form,
  });
  prog.style.display = "none";
  const data = await res.json();

  if (data.error) {
    errEl.textContent = data.error;
    return;
  }

  result.style.display = "block";
  const failHtml = data.failed?.length
    ? `<div style="margin-top:0.5rem;max-height:120px;overflow-y:auto;font-size:11px;color:var(--danger)">
        ${data.failed.map((f) => `Row ${f.row}: ${escapeHtml(f.reason)}`).join("<br>")}
       </div>`
    : "";
  result.innerHTML = `
    <div class="msg ok">
      ✓ Import complete — <strong>${data.created}</strong> created,
      <strong>${data.updated}</strong> updated,
      <strong>${data.failed?.length || 0}</strong> failed (of ${data.total} total)
    </div>${failHtml}`;
  loadStudents();
}

// ══════════════════════════════════════════════════════════════════════════
//  ACADEMIC CALENDAR (Admin — Manage → Calendar tab)
// ══════════════════════════════════════════════════════════════════════════

let _academicYears = [];

async function loadAcademicYears() {
  const body = document.getElementById("academicYearsBody");
  if (!body) return;
  const res = await api("/calendar/academic-years");
  const data = await res.json();
  _academicYears = data.academic_years || [];

  // Populate holiday year filter selects
  ["holYearFilter", "holYearSelect"].forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    const cur = el.value;
    el.innerHTML = `<option value="">— None —</option>`;
    _academicYears.forEach((y) => {
      const o = document.createElement("option");
      o.value = y.id;
      o.textContent = y.name + (y.is_current ? " (current)" : "");
      el.appendChild(o);
    });
    el.value = cur;
  });

  if (!_academicYears.length) {
    body.innerHTML = `<tr><td colspan="5" class="text-center text-muted p-2rem">No academic years added yet.</td></tr>`;
    return;
  }
  body.innerHTML = _academicYears
    .map(
      (y) => `<tr>
    <td>${escapeHtml(y.name)}</td>
    <td class="mono text-12px">${y.start_date}</td>
    <td class="mono text-12px">${y.end_date}</td>
    <td style="text-align:center">${y.is_current ? "✓" : ""}</td>
    <td>
      <button class="btn-sm btn-secondary" onclick="openAcademicYearModal(${y.id})">Edit</button>
      <button class="btn-sm btn-danger ml-4" onclick="deleteAcademicYear(${y.id})">Delete</button>
    </td>
  </tr>`,
    )
    .join("");
}

function openAcademicYearModal(id) {
  document.getElementById("acYearErr").textContent = "";
  document.getElementById("acYearId").value = id || "";
  document.getElementById("acYearTitle").textContent = id
    ? "Edit Academic Year"
    : "Add Academic Year";
  if (id) {
    const y = _academicYears.find((y) => y.id === id);
    if (y) {
      document.getElementById("acYearName").value = y.name;
      document.getElementById("acYearStart").value = y.start_date;
      document.getElementById("acYearEnd").value = y.end_date;
      document.getElementById("acYearCurrent").checked = y.is_current;
    }
  } else {
    document.getElementById("acYearName").value = "";
    document.getElementById("acYearStart").value = "";
    document.getElementById("acYearEnd").value = "";
    document.getElementById("acYearCurrent").checked = false;
  }
  document.getElementById("academicYearModal").style.display = "flex";
}

async function saveAcademicYear() {
  const id = document.getElementById("acYearId").value;
  const errEl = document.getElementById("acYearErr");
  errEl.textContent = "";
  const payload = {
    name: document.getElementById("acYearName").value.trim(),
    start_date: document.getElementById("acYearStart").value,
    end_date: document.getElementById("acYearEnd").value,
    is_current: document.getElementById("acYearCurrent").checked,
  };
  if (!payload.name || !payload.start_date || !payload.end_date) {
    errEl.textContent = "Name, start date, and end date are required.";
    return;
  }
  const method = id ? "PUT" : "POST";
  const url = id
    ? `/calendar/academic-years/${id}`
    : "/calendar/academic-years";
  const res = await api(url, { method, json: payload });
  const data = await res.json();
  if (data.error) {
    errEl.textContent = data.error;
    return;
  }
  closeModal("academicYearModal");
  loadAcademicYears();
  toast(id ? "Academic year updated" : "Academic year created");
}

async function deleteAcademicYear(id) {
  if (
    !confirm(
      "Delete this academic year? All associated holidays will also be deleted.",
    )
  )
    return;
  const res = await api(`/calendar/academic-years/${id}`, { method: "DELETE" });
  const data = await res.json();
  if (data.error) {
    toast(data.error, "err");
    return;
  }
  toast("Deleted");
  loadAcademicYears();
}

async function loadHolidays() {
  const body = document.getElementById("holidaysBody");
  if (!body) return;
  const yearId = document.getElementById("holYearFilter")?.value || "";
  const qs = yearId ? `?academic_year_id=${yearId}` : "";
  const res = await api(`/calendar/holidays${qs}`);
  const data = await res.json();
  const rows = data.holidays || [];
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="3" class="text-center text-muted p-2rem">No holidays added yet.</td></tr>`;
    return;
  }
  body.innerHTML = rows
    .map(
      (h) => `<tr>
    <td class="mono text-12px">${h.date}</td>
    <td>${escapeHtml(h.name)}</td>
    <td><button class="btn-sm btn-danger" onclick="deleteHoliday(${h.id})">Delete</button></td>
  </tr>`,
    )
    .join("");
}

function _holDateHint() {
  const from = document.getElementById("holFromDate")?.value;
  const to = document.getElementById("holToDate")?.value;
  const hint = document.getElementById("holDateHint");
  if (!hint) return;
  if (from && to && to > from) {
    const d1 = new Date(from), d2 = new Date(to);
    const days = Math.round((d2 - d1) / 86400000) + 1;
    hint.textContent = `${days} day holiday will be created (${from} to ${to})`;
  } else if (from && to && to === from) {
    hint.textContent = "Single-day holiday";
  } else if (from && (!to || to < from)) {
    hint.textContent = to ? "To Date must be on or after From Date" : "Single-day holiday";
  } else {
    hint.textContent = "";
  }
}

function openHolidayModal() {
  document.getElementById("holErr").textContent = "";
  document.getElementById("holFromDate").value = "";
  document.getElementById("holToDate").value = "";
  document.getElementById("holName").value = "";
  document.getElementById("holDateHint").textContent = "";
  document.getElementById("holYearSelect").value =
    document.getElementById("holYearFilter")?.value || "";
  document.getElementById("holidayModal").style.display = "flex";
}

async function saveHoliday() {
  const errEl = document.getElementById("holErr");
  errEl.textContent = "";
  const name = document.getElementById("holName").value.trim();
  const fromDate = document.getElementById("holFromDate").value;
  const toDate = document.getElementById("holToDate").value;
  const yearId = document.getElementById("holYearSelect").value || null;

  if (!fromDate || !name) {
    errEl.textContent = "Holiday name and From Date are required.";
    return;
  }
  if (toDate && toDate < fromDate) {
    errEl.textContent = "To Date must be on or after From Date.";
    return;
  }

  const res = await api("/calendar/holidays", {
    method: "POST",
    json: { name, from_date: fromDate, to_date: toDate || fromDate, academic_year_id: yearId },
  });
  const data = await res.json();
  if (data.error) {
    errEl.textContent = data.error;
    return;
  }
  closeModal("holidayModal");
  loadHolidays();
  const added = data.added || 1;
  toast(added > 1 ? `${added} holiday days added` : "Holiday added");
}

async function deleteHoliday(id) {
  if (!confirm("Delete this holiday?")) return;
  const res = await api(`/calendar/holidays/${id}`, { method: "DELETE" });
  const data = await res.json();
  if (data.error) {
    toast(data.error, "err");
    return;
  }
  toast("Deleted");
  loadHolidays();
}

// ══════════════════════════════════════════════════════════════════════════
//  QR CODE ATTENDANCE  (teacher generates, student scans)
// ══════════════════════════════════════════════════════════════════════════

let _qrSessionId = null;
let _qrExpiryTs = null;
let _qrTimer = null;
let _qrExpirySeconds = 90;

async function openQRModal(sessionId) {
  _qrSessionId = sessionId;
  document.getElementById("qrModal").style.display = "flex";
  await refreshQR();
}

async function refreshQR() {
  if (!_qrSessionId) return;
  clearInterval(_qrTimer);
  document.getElementById("qrCanvas").innerHTML =
    `<div class="text-muted text-13px">Generating…</div>`;

  const res = await api(`/attendance/sessions/${_qrSessionId}/qr`, {
    method: "POST",
    body: JSON.stringify({ expiry_seconds: _qrExpirySeconds }),
  });
  const data = await res.json();
  if (data.error) {
    toast(data.error, "err");
    return;
  }

  _qrExpiryTs = data.expires_at;
  _qrExpirySeconds = data.expires_at - Math.floor(Date.now() / 1000);
  document.getElementById("qrExpirySec").textContent = _qrExpirySeconds;

  // Render QR using qrcode.js library
  const canvas = document.getElementById("qrCanvas");
  canvas.innerHTML = "";
  if (typeof QRCode !== "undefined") {
    new QRCode(canvas, {
      text: data.qr_data,
      width: 196,
      height: 196,
      colorDark: "#0d0d0f",
      colorLight: "#ffffff",
      correctLevel: QRCode.CorrectLevel.M,
    });
  } else {
    canvas.innerHTML = `<div class="text-13px" style="word-break:break-all;padding:0.5rem">${data.qr_data}</div>`;
  }

  // Countdown timer
  const bar = document.getElementById("qrExpiryBar");
  _qrTimer = setInterval(() => {
    const remaining = _qrExpiryTs - Math.floor(Date.now() / 1000);
    const pct = Math.max(0, (remaining / _qrExpirySeconds) * 100);
    if (bar) bar.style.width = pct + "%";
    const cd = document.getElementById("qrCountdown");
    if (cd)
      cd.textContent =
        remaining > 0 ? `Expires in ${remaining}s` : "Expired — click Refresh";
    if (remaining <= 0) {
      clearInterval(_qrTimer);
      if (bar) bar.style.width = "0%";
    }
  }, 1000);
}

function closeQRModal() {
  clearInterval(_qrTimer);
  _qrSessionId = null;
  closeModal("qrModal");
}

// ══════════════════════════════════════════════════════════════════════════
//  STUDENT PANEL — Per-subject attendance + correction requests
// ══════════════════════════════════════════════════════════════════════════

async function loadStudentPanel() {
  const res = await api("/student/me/attendance");
  const data = await res.json();
  if (data.error) return;

  const overall = data.overall || {};
  const _sv = (id, v) => {
    const e = document.getElementById(id);
    if (e) e.textContent = v;
  };
  _sv("sStat-present", overall.present ?? "—");
  _sv("sStat-total", overall.total ?? "—");
  _sv("sStat-pct", overall.pct != null ? overall.pct + "%" : "—");

  // Subject-wise breakdown
  const summaryEl = document.getElementById("studentSubjectSummary");
  if (summaryEl) {
    const subs = data.by_subject || [];
    if (!subs.length) {
      summaryEl.innerHTML = `<div class="text-muted text-12px p-1rem">No subject-wise records yet.</div>`;
    } else {
      summaryEl.innerHTML = subs
        .map((s) => {
          const pct = s.pct !== null ? parseFloat(s.pct) : null;
          const bar = pct !== null ? pct : 0;
          const color =
            pct === null
              ? "var(--text3)"
              : pct < 60
                ? "var(--danger)"
                : pct < 75
                  ? "var(--amber)"
                  : "var(--green)";
          return `<div style="padding:0.5rem 0;border-bottom:1px solid var(--border)">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
            <span class="text-13px"><span class="subject-tag" style="font-size:11px">${escapeHtml(s.subject_code)}</span> ${escapeHtml(s.subject_name)}</span>
            <span style="font-size:12px;font-weight:600;color:${color}">${pct !== null ? pct + "%" : "—"}</span>
          </div>
          <div style="height:4px;background:var(--border);border-radius:2px">
            <div style="height:4px;width:${bar}%;background:${color};border-radius:2px;transition:width .4s"></div>
          </div>
          <div style="font-size:11px;color:var(--text3);margin-top:3px">${s.present}/${s.total} classes</div>
        </div>`;
        })
        .join("");
    }
  }

  // Recent records table
  const attBody = document.getElementById("studentAttBody");
  if (attBody) {
    const rec = data.recent || [];
    if (!rec.length) {
      attBody.innerHTML = `<tr><td colspan="4" class="text-center text-muted p-2rem">No attendance records found.</td></tr>`;
    } else {
      attBody.innerHTML = rec
        .map((r) => {
          const statusCls =
            r.status === "Present"
              ? "color:var(--green)"
              : "color:var(--danger)";
          return `<tr>
          <td class="mono text-12px">${r.date || "—"}</td>
          <td class="text-12px">${r.subject_name ? `<span class="subject-tag">${escapeHtml(r.subject_code || "")}</span>` : "—"}</td>
          <td class="text-12px">—</td>
          <td style="${statusCls};font-weight:600;font-size:12px">${r.status}</td>
        </tr>`;
        })
        .join("");
    }
  }
}

async function openStudentCorrectionModal() {
  document.getElementById("corrErr").textContent = "";
  document.getElementById("corrSuccess").style.display = "none";
  document.getElementById("corrDate").value = new Date()
    .toISOString()
    .slice(0, 10);
  document.getElementById("corrReason").value = "";

  // Load student's subjects for dropdown
  const subjEl = document.getElementById("corrSubject");
  if (subjEl && subjEl.options.length <= 1) {
    const res = await api("/student/me/attendance");
    const data = await res.json();
    (data.by_subject || []).forEach((s) => {
      const o = document.createElement("option");
      o.value = s.subject_id;
      o.textContent = `${s.subject_code} — ${s.subject_name}`;
      subjEl.appendChild(o);
    });
  }
  document.getElementById("studentCorrectionModal").style.display = "flex";
}

async function submitCorrectionRequest() {
  const errEl = document.getElementById("corrErr");
  const okEl = document.getElementById("corrSuccess");
  errEl.textContent = "";
  okEl.style.display = "none";

  const payload = {
    date: document.getElementById("corrDate").value,
    subject_id: document.getElementById("corrSubject").value || null,
    reason: document.getElementById("corrReason").value.trim(),
  };
  if (!payload.date || !payload.reason) {
    errEl.textContent = "Date and reason are required.";
    return;
  }

  const res = await api("/corrections", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (data.error) {
    errEl.textContent = data.error;
    return;
  }
  okEl.style.display = "block";
  document.getElementById("corrReason").value = "";
}

// ── Wire calendar tab load ────────────────────────────────────────────────

const _origSwitchManageTab = switchManageTab;
// Extend switchManageTab to handle the new calendar tab
window.switchManageTab = function (tab, btn) {
  _origSwitchManageTab(tab, btn);
  if (tab === "calendar") {
    loadAcademicYears();
    loadHolidays();
  }
};
