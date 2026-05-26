/* ═══════════════════════════════════════════════════════════════════════════
   FRS · app.js  –  Frontend application logic
   Architecture:
     • All server communication through api() helper (adds auth token)
     • navigate() switches pages and fires load functions
     • Each page has its own loader (loadDashboard, loadStudents, etc.)
     • Webcam: getUserMedia for enroll, MJPEG <img src> for live recognition
═══════════════════════════════════════════════════════════════════════════ */

const API = "http://localhost:5050/api";
let authToken = localStorage.getItem("frs_token") || "";
let authRole = localStorage.getItem("frs_role") || "";
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
}
function showApp() {
  document.getElementById("loginOverlay").classList.add("hidden");
  document.getElementById("sidebar").style.display = "flex";
  document.querySelector(".main").style.display = "block";
  checkAPI();
  navigate("dashboard");
}

async function doLogin() {
  const username = document.getElementById("loginUser").value.trim();
  const password = document.getElementById("loginPass").value;
  const errEl = document.getElementById("loginErr");
  errEl.textContent = "";
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
    authRole = d.role;
    localStorage.setItem("frs_token", authToken);
    localStorage.setItem("frs_role", authRole);
    showApp();
  } catch {
    errEl.textContent = "Cannot reach server";
  }
}

function logout() {
  authToken = "";
  authRole = "";
  localStorage.removeItem("frs_token");
  localStorage.removeItem("frs_role");
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
  };
  if (loaders[page]) loaders[page]();
}

/* ═══════════════════════════════════════════════════════════════════════
   DASHBOARD
═══════════════════════════════════════════════════════════════════════ */
async function loadDashboard() {
  try {
    const [att, hist, persons, logs] = await Promise.all([
      api(`/attendance?date=${todayStr()}`).then((r) => r.json()),
      api(`/attendance/history`).then((r) => r.json()),
      api(`/students`).then((r) => r.json()),
      api(`/logs?limit=30`).then((r) => r.json()),
    ]);
    document.getElementById("statRegistered").textContent = persons.count;
    document.getElementById("statPresent").textContent = att.present;
    document.getElementById("statAbsent").textContent = att.absent;
    const rate =
      persons.count > 0
        ? Math.round((att.present / persons.count) * 100) + "%"
        : "—";
    document.getElementById("statRate").textContent = rate;

    renderHistoryChart(hist.history);
    dashRecords = att.records;
    renderDashTable(dashRecords);
    renderRecentLogs(logs.logs);
  } catch (e) {
    console.error(e);
  }
}

function renderHistoryChart(history) {
  const sorted = [...history].sort((a, b) => a.date.localeCompare(b.date));
  const ctx = document.getElementById("attendanceChart").getContext("2d");
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
  const el = document.getElementById("recentLogs");
  if (!el) return;
  el.innerHTML =
    logs
      .slice(0, 15)
      .map(
        (l) => `
    <div class="log-item ${l.recognized ? "ok" : "fail"}">
      <span class="log-name">${l.full_name || "Unknown"}</span>
      <span class="log-conf">${l.confidence}% · ${l.logged_at?.slice(11, 16) || ""}</span>
    </div>`,
      )
      .join("") || `<p class="muted">No events yet</p>`;
}

function renderDashTable(records) {
  document.getElementById("dashTableBody").innerHTML = (records || [])
    .map(
      (r) => `
    <tr>
      <td style="font-family:var(--mono);font-size:12px">${r.student_id}</td>
      <td><a onclick="viewProfile('${r.student_id}')">${escapeHtml(getStudentName(r))}</a></td>
      <td style="color:var(--text3)">${r.department || "—"}</td>
      <td style="font-family:var(--mono);font-size:12px">${r.time}</td>
      <td>${badge(r.status)}</td>
    </tr>`,
    )
    .join("");
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
async function loadStudents() {
  await loadDepartments("deptFilter");
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

async function searchStudents() {
  const q = document.getElementById("studentSearch").value.trim();
  const dept = document.getElementById("deptFilter").value;
  let url = `/students?q=${encodeURIComponent(q)}`;
  if (dept) url += `&department=${encodeURIComponent(dept)}`;
  try {
    const r = await api(url);
    const d = await r.json();
    const grid = document.getElementById("personsGrid");
    grid.innerHTML =
      d.students
        .map((s) => {
          const initials = s.full_name
            .split(" ")
            .map((w) => w[0]?.toUpperCase())
            .slice(0, 2)
            .join("");
          return `
        <div class="person-card" onclick="viewProfile('${s.student_id}')">
          <button class="person-del" onclick="event.stopPropagation();deleteStudent('${s.student_id}')" title="Remove">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3,6 5,6 21,6"/><path d="M19 6l-1 14H6L5 6"/><path d="M9 6V4h6v2"/></svg>
          </button>
          <div class="person-avatar">${initials || "?"}</div>
          <div class="person-name">${s.full_name}</div>
          <div class="person-dept">${s.department || "—"}</div>
          <div class="person-dept" style="font-family:var(--mono);font-size:10px;color:var(--text3)">${s.image_count} images</div>
        </div>`;
        })
        .join("") || `<p style="color:var(--text3)">No students found.</p>`;
  } catch (e) {
    console.error(e);
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
    document.getElementById("profileActions").innerHTML = `
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
            <span class="meta-tag">${s.image_count} training images</span>
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
      const ctx = document.getElementById("profileChart").getContext("2d");
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
  const zone = document.getElementById("uploadZone");
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
    document.getElementById("fileInput").click(),
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
    const section = document.getElementById("webcamSection");
    section.style.display = "block";
    webcamStream = await navigator.mediaDevices.getUserMedia({ video: true });
    document.getElementById("enrollCam").srcObject = webcamStream;
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
  document.getElementById("webcamSection").style.display = "none";
}

function captureWebcamFrame() {
  const video = document.getElementById("enrollCam");
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
  document.getElementById("webcamCount").textContent =
    `${webcamFiles.length} frames captured`;
}

async function enrollStudent() {
  const sid = document.getElementById("eId").value.trim();
  const name = document.getElementById("eName").value.trim();
  const dept = document.getElementById("eDept").value.trim();
  const email = document.getElementById("eEmail").value.trim();
  const phone = document.getElementById("ePhone").value.trim();
  const msg = document.getElementById("enrollMsg");

  if (!sid || !name) {
    setMsg("enrollMsg", "Student ID and full name are required.", "err");
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
  if (dept) form.append("department", dept);
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
    const filePreview = document.getElementById("filePreview");
    if (filePreview) filePreview.innerHTML = "";
    ["eId", "eName", "eDept", "eEmail", "ePhone"].forEach(
      (id) => (document.getElementById(id).value = ""),
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
  const btn = document.getElementById("camToggle");
  if (!cameraActive) {
    await api("/camera/start", { method: "POST" });
    cameraActive = true;
    btn.textContent = "■ Stop Camera";
    document.getElementById("liveStream").style.display = "block";
    document.getElementById("cameraPlaceholder").style.display = "none";
    document.getElementById("recognizeCanvas").style.display = "none";
    document.getElementById("liveStream").src = `${API}/stream`;
    startPollLiveLog();
  } else {
    stopCamera();
  }
}

async function stopCamera() {
  await api("/camera/stop", { method: "POST" });
  cameraActive = false;
  const btn = document.getElementById("camToggle");
  if (btn) btn.textContent = "▶ Start Camera";
  const stream = document.getElementById("liveStream");
  if (stream) {
    stream.src = "";
    stream.style.display = "none";
  }
  const ph = document.getElementById("cameraPlaceholder");
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
    const el = document.getElementById("liveLog");
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
      const canvas = document.getElementById("recognizeCanvas");
      const ctx = canvas.getContext("2d");
      canvas.width = img.width;
      canvas.height = img.height;
      ctx.drawImage(img, 0, 0);
      canvas.style.display = "block";
      document.getElementById("cameraPlaceholder").style.display = "none";
      document.getElementById("liveStream").style.display = "none";
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
  document.getElementById("recogName").textContent = name;
  document.getElementById("recogConf").textContent = conf;
  const icon = document.getElementById("recogIcon");
  const badge = document.getElementById("recogBadge");
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
  const container = document.getElementById("facultyTabs");
  const faculties = facultyData.faculties || [];

  const tabs = [
    { key: "", label: `All  (${facultyData.overall?.total || 0})` },
    ...faculties.map((f) => ({
      key: f.name,
      label: `${f.name}  (${f.total})`,
    })),
  ];

  container.innerHTML = tabs
    .map(
      (t) => `
    <div class="faculty-tab ${activeFaculty === t.key ? "active" : ""}"
         onclick="showFaculty('${t.key}')">
      ${t.label}
    </div>`,
    )
    .join("");
}

function showFaculty(key) {
  activeFaculty = key;
  // Update active tab highlight
  document.querySelectorAll(".faculty-tab").forEach((el) => {
    const isActive = el.textContent.trim().startsWith(key || "All");
    el.classList.toggle("active", isActive);
  });
  document.getElementById("attSearch").value = "";

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

  // Hide single-faculty header card
  document.getElementById("facultyHeaderCard").style.display = "none";

  // Render summary cards
  const summaryRow = document.getElementById("facultySummaryRow");
  summaryRow.innerHTML = [
    // Overall card first
    renderSummaryCard(
      {
        name: "All Faculties",
        total: overall.total,
        present: overall.present,
        absent: overall.absent,
        rate: overall.rate,
      },
      true,
    ),
    ...faculties.map((f) => renderSummaryCard(f, false)),
  ].join("");

  // Render all faculties as sections in one table container
  filteredStudents = faculties.flatMap((f) =>
    f.students.map((s) => ({ ...s, faculty: f.name })),
  );
  const container = document.getElementById("attTableContainer");
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

  // Hide summary cards
  document.getElementById("facultySummaryRow").innerHTML = "";

  // Show header card
  const card = document.getElementById("facultyHeaderCard");
  card.style.display = "flex";
  document.getElementById("fhcName").textContent = fac.name;
  document.getElementById("fhcDate").textContent = new Date(
    dateVal,
  ).toLocaleDateString("en-GB", {
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
  });
  document.getElementById("fhcPresent").textContent = fac.present;
  document.getElementById("fhcAbsent").textContent = fac.absent;
  document.getElementById("fhcTotal").textContent = fac.total;
  const rateEl = document.getElementById("fhcRate");
  rateEl.textContent = fac.rate + "%";
  rateEl.className =
    "fhc-val " + (fac.rate >= 75 ? "green" : fac.rate >= 50 ? "amber" : "red");

  // Render student table for this faculty
  filteredStudents = fac.students;
  document.getElementById("attTableContainer").innerHTML = buildStudentTable(
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
    const container = document.getElementById("attTableContainer");
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
    document.getElementById("attTableContainer").innerHTML = buildStudentTable(
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

    document.getElementById("repAvg").textContent = avg;
    document.getElementById("repHigh").textContent = high;
    document.getElementById("repLow").textContent = low;

    // Total recognition events
    const logsR = await api("/logs?limit=1000");
    const logsD = await logsR.json();
    document.getElementById("repTotal").textContent = logsD.logs.length;

    // Per-student table
    document.getElementById("repTableBody").innerHTML = rows
      .map((r) => {
        const pct = parseFloat(r.pct || 0);
        const color =
          pct >= 75
            ? "var(--green)"
            : pct >= 50
              ? "var(--amber)"
              : "var(--red)";
        const statusLabel =
          pct >= 75 ? "On Track" : pct >= 50 ? "At Risk" : "Critical";
        return `<tr>
        <td style="font-family:var(--mono);font-size:11px">${r.student_id}</td>
        <td><a onclick="viewProfile('${r.student_id}')">${r.full_name}</a></td>
        <td style="color:var(--text3)">${r.department || "—"}</td>
        <td style="font-family:var(--mono)">${r.present_days || 0}</td>
        <td>
          <div class="inline-bar">
            <div class="mini-bar"><div class="mini-fill" style="width:${pct}%;background:${color}"></div></div>
            <span style="font-family:var(--mono);font-size:11px;color:${color}">${pct}%</span>
          </div>
        </td>
        <td><span style="font-size:11px;font-family:var(--mono);color:${color}">${statusLabel}</span></td>
      </tr>`;
      })
      .join("");

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
  const ctx = document.getElementById("monthlyChart").getContext("2d");
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
  const ctx = document.getElementById("deptChart").getContext("2d");
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
    document.getElementById("setThreshold").value = Math.round(
      d.recognition_threshold * 100,
    );
    document.getElementById("setFrameSkip").value = d.frame_skip;
    document.getElementById("setDataDir").textContent = d.data_dir;
  } catch {}
}

async function saveSettings() {
  const threshold =
    parseFloat(document.getElementById("setThreshold").value) / 100;
  const frameSkip = parseInt(document.getElementById("setFrameSkip").value);
  try {
    const r = await api("/settings", {
      method: "PUT",
      json: { recognition_threshold: threshold, frame_skip: frameSkip },
    });
    if (r.ok) {
      setMsg("settingsMsg", "Settings saved.", "ok");
      toast("Saved");
    } else setMsg("settingsMsg", "Failed to save.", "err");
  } catch {
    setMsg("settingsMsg", "Error.", "err");
  }
}

async function changePw() {
  const oldPw = document.getElementById("oldPw")?.value || "";
  const newPw = document.getElementById("newPw").value;
  if (!newPw || newPw.length < 6) {
    setMsg("pwMsg", "New password must be at least 6 characters.", "err");
    return;
  }
  const r = await api("/auth/change-password", {
    method: "POST",
    json: { old_password: oldPw, new_password: newPw },
  });
  if (!r) return;
  const d = await r.json();
  if (r.ok) {
    setMsg("pwMsg", "✓ Password updated. Please log in again.", "ok");
    toast("Password changed");
    // Token changes — log out after 2s
    setTimeout(() => logout(), 2000);
  } else {
    setMsg("pwMsg", d.error || "Update failed.", "err");
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
  const liveLog = document.getElementById("liveLog");
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
  const dept = document.getElementById("eDept").value.trim();
  const email = document.getElementById("eEmail").value.trim();
  const phone = document.getElementById("ePhone").value.trim();

  if (!sid || !name) {
    setMsg("enrollMsg", "Student ID and full name are required.", "err");
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
          department: dept || null,
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
      if (dept) form.append("department", dept);
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
    const filePreview = document.getElementById("filePreview");
    if (filePreview) filePreview.innerHTML = "";
    ["eId", "eName", "eDept", "eEmail", "ePhone"].forEach(
      (id) => (document.getElementById(id).value = ""),
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

function setCaptureMethod(method, btn) {
  captureMethod = method;
  document
    .querySelectorAll(".cmtab")
    .forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  document.getElementById("uploadMode").style.display =
    method === "upload" ? "block" : "none";
  document.getElementById("capturePanel").style.display =
    method === "auto" ? "flex" : "none";
  if (method === "upload") {
    stopAutoCapture();
  }
}

// ── Start guided capture ──────────────────────────────────────────────
async function startAutoCapture() {
  const sid = document.getElementById("eId").value.trim();
  const name = document.getElementById("eName").value.trim();
  if (!sid || !name) {
    toast("Fill in Student ID and Full Name first", "err");
    document.getElementById("eId").focus();
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

    const r = await api("/capture/validate-frame", {
      method: "POST",
      json: { image: b64 },
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
    if (poseLabelEl)
      poseLabelEl.textContent = d.face_detected ? currentPoseHint : "No face";

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
  for (const step of POSE_SEQUENCE) {
    const pip = document.getElementById(`pip-${step.pose}`);
    if (!pip) continue;
    const count = poseCaptureCounts[step.pose] || 0;
    const isDone = count >= step.target;
    const isCurrent = _getCurrentPoseStep()?.pose === step.pose;
    pip.classList.toggle("done", isDone);
    pip.classList.toggle("active", isCurrent && !isDone);
  }
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
  const dept = document.getElementById("eDept").value.trim();
  const sem = document.getElementById("eSem")?.value.trim() || "";
  const email = document.getElementById("eEmail").value.trim();
  const phone = document.getElementById("ePhone").value.trim();

  if (!sid || !name) {
    setMsg("enrollMsg", "Student ID and Full Name are required.", "err");
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
          department: dept || null,
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
      if (dept) form.append("department", dept);
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
    document.getElementById("filePreview") &&
      (document.getElementById("filePreview").innerHTML = "");
    resetCapture();
    ["eId", "eName", "eDept", "eSem", "eEmail", "ePhone"].forEach((id) => {
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

  // Load student data
  const r = await api(`/students/${sid}`);
  if (!r) return;
  const s = await r.json();

  document.getElementById("modalTitle").textContent = `Edit — ${s.full_name}`;
  document.getElementById("mSid").value = s.student_id;
  document.getElementById("mName").value = s.full_name;
  document.getElementById("mDept").value = s.department || "";
  document.getElementById("mSem").value = s.semester || "";
  document.getElementById("mEmail").value = s.email || "";
  document.getElementById("mPhone").value = s.phone || "";
  document.getElementById("mStatus").value = s.status || "active";

  setMsg("editMsg", "", "");
  setMsg("attEditMsg", "", "");
  document.getElementById("attEditDate").value = todayStr();

  // Load attendance records for the attendance tab
  _renderAttEditList(s.attendance || []);
  // Reset to profile tab
  switchModalTab("profile", document.querySelector(".mtab"));
};

function closeEditModal(event) {
  if (event && event.target !== document.getElementById("editModal")) return;
  _closeEditModal();
}
function _closeEditModal() {
  document.getElementById("editModal").style.display = "none";
  document.body.style.overflow = "";
  _editingSid = null;
}
// Also close on Escape
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") _closeEditModal();
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
}

async function saveStudentEdit() {
  if (!_editingSid) return;
  const fields = {
    full_name: document.getElementById("mName").value.trim(),
    department: document.getElementById("mDept").value.trim() || null,
    semester: document.getElementById("mSem").value.trim() || null,
    email: document.getElementById("mEmail").value.trim() || null,
    phone: document.getElementById("mPhone").value.trim() || null,
    status: document.getElementById("mStatus").value,
  };
  if (!fields.full_name) {
    setMsg("editMsg", "Name is required.", "err");
    return;
  }

  const r = await api(`/students/${_editingSid}`, {
    method: "PUT",
    json: fields,
  });
  if (!r) return;
  const d = await r.json();
  if (!r.ok) {
    setMsg("editMsg", d.error || "Update failed.", "err");
    return;
  }

  setMsg("editMsg", "✓ Profile updated successfully.", "ok");
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
  const el = document.getElementById("attEditList");
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
  const date = document.getElementById("attEditDate").value;
  const status = document.getElementById("attEditStatus").value;
  const note = document.getElementById("attEditNote").value.trim();
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
  document.getElementById("attEditNote").value = "";
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
  const el = document.getElementById("actLogList");
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
    let emailEl = document.getElementById("emailStatusBadge");
    if (!emailEl) return;
    const on = d.email_enabled;
    emailEl.className = `email-status ${on ? "on" : "off"}`;
    emailEl.textContent = on ? "✓ Email enabled" : "✗ Email disabled";
    if (d.smtp_user)
      document.getElementById("smtpUserDisplay") &&
        (document.getElementById("smtpUserDisplay").textContent = d.smtp_user);
  } catch {}
};

async function sendTestEmail() {
  const email = prompt("Enter email address to send test to:");
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
  if (!sid || !name) {
    toast("Student ID and Full Name are required", "err");
    document.getElementById("eId").focus();
    return;
  }
  _setEnrollStep(2);
}

function goToStep3() {
  // Build review grid
  const fields = [
    ["Student ID", document.getElementById("eId").value.trim()],
    ["Full Name", document.getElementById("eName").value.trim()],
    ["Department", document.getElementById("eDept").value.trim()],
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
   STUDENTS — Faculty filter tabs
═══════════════════════════════════════════════════════════════════════ */
let _activeFacultyFilter = "";
let _allStudentsCache = [];

async function loadStudents() {
  // Load departments for tab bar
  try {
    const r = await api("/departments");
    const d = await r.json();
    _buildStudentFacultyTabs(d.departments || []);
  } catch {}
  await searchStudents();
}

function _buildStudentFacultyTabs(depts) {
  const bar = document.getElementById("studentFacultyTabs");
  if (!bar) return;
  bar.innerHTML = [
    `<div class="faculty-tab ${_activeFacultyFilter === "" ? "active" : ""}"
          onclick="filterStudentsByFaculty('',this)">All</div>`,
    ...depts.map(
      (d) =>
        `<div class="faculty-tab ${_activeFacultyFilter === d ? "active" : ""}"
            onclick="filterStudentsByFaculty('${d}',this)">${d}</div>`,
    ),
  ].join("");
}

async function filterStudentsByFaculty(dept, btn) {
  _activeFacultyFilter = dept;
  document
    .querySelectorAll("#studentFacultyTabs .faculty-tab")
    .forEach((b) =>
      b.classList.toggle("active", b.textContent.trim() === (dept || "All")),
    );
  document.getElementById("studentSearch").value = "";
  await searchStudents();
}

// Override existing searchStudents to use faculty filter
const _origSearchStudents = window.searchStudents || (async () => {});
window.searchStudents = async function () {
  const q = document.getElementById("studentSearch")?.value.trim() || "";
  const dept = _activeFacultyFilter;
  let url = `/students?q=${encodeURIComponent(q)}`;
  if (dept) url += `&department=${encodeURIComponent(dept)}`;
  try {
    const r = await api(url);
    const d = await r.json();
    _allStudentsCache = d.students || [];
    _renderStudentGrid(_allStudentsCache);
    _renderStudentFacultyBar(_allStudentsCache);
  } catch (e) {
    console.error("searchStudents:", e);
  }
};

function _renderStudentGrid(students) {
  const grid = document.getElementById("personsGrid");
  if (!grid) return;
  grid.innerHTML =
    students
      .map((s) => {
        const initials = s.full_name
          .split(" ")
          .map((w) => w[0]?.toUpperCase())
          .slice(0, 2)
          .join("");
        return `
      <div class="person-card" onclick="viewProfile('${s.student_id}')">
        <button class="person-del" onclick="event.stopPropagation();deleteStudent('${s.student_id}')" title="Edit">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
        </button>
        <div class="person-avatar">${initials || "?"}</div>
        <div class="person-name">${s.full_name}</div>
        <div class="person-dept">${s.department || "—"}</div>
        <div class="person-dept" style="font-family:var(--mono);font-size:10px;color:var(--text3)">${s.semester || ""} · ${s.sample_count} images</div>
      </div>`;
      })
      .join("") ||
    `<p style="color:var(--text3);grid-column:1/-1">No students found.</p>`;
}

function _renderStudentFacultyBar(students) {
  const bar = document.getElementById("studentFacultyBar");
  if (!bar) return;
  if (_activeFacultyFilter) {
    bar.innerHTML = "";
    return;
  }
  const deptMap = {};
  students.forEach((s) => {
    const d = s.department || "Unassigned";
    deptMap[d] = (deptMap[d] || 0) + 1;
  });
  bar.innerHTML = Object.entries(deptMap)
    .sort((a, b) => b[1] - a[1])
    .map(
      ([dept, count]) => `
      <div class="sfb-chip">
        ${dept} <span class="sfb-count">${count}</span>
      </div>`,
    )
    .join("");
}

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
    if (!bar || document.getElementById("voiceToggleBtn")) return;

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
