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
    const [att, hist, persons] = await Promise.all([
      api(`/attendance?date=${todayStr()}`).then((r) => r.json()),
      api(`/attendance/history`).then((r) => r.json()),
      api(`/students`).then((r) => r.json()),
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
          barThickness: 40,
          maxBarThickness: 40,
        },
        {
          label: "Absent",
          data: sorted.map((h) => h.absent),
          backgroundColor: "rgba(239,68,68,0.3)",
          borderRadius: 3,
          borderSkipped: false,
          barThickness: 40,
          maxBarThickness: 40,
        },
      ],
    },
    options: chartOpts({ stacked: true, maintainAspectRatio: false }),
  });
}

function renderDashTable(records) {
  document.getElementById("dashTableBody").innerHTML = records
    .map(
      (r) => `
    <tr>
      <td style="font-family:var(--mono);font-size:12px">${r.student_id}</td>
      <td><a onclick="viewProfile('${r.student_id}')">${r.name}</a></td>
      <td style="color:var(--text3)">${r.department || "—"}</td>
      <td style="font-family:var(--mono);font-size:12px">${r.time}</td>
      <td>${badge(r.status)}</td>
    </tr>`,
    )
    .join("");
}

function filterDashTable(q) {
  const filtered = dashRecords.filter((r) =>
    r.name.toLowerCase().includes(q.toLowerCase()),
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
      <button class="btn-secondary" onclick="openEditModal('${sid}')">Edit</button>
      <button class="btn-danger btn-secondary" onclick="deleteStudent('${sid}')">Delete</button>`;

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
  if (!confirm(`Remove student ${sid} from the system?`)) return;
  const r = await api(`/students/${sid}`, { method: "DELETE" });
  const d = await r.json();
  if (d.deleted) {
    toast("Student removed");
    navigate("students");
  } else toast("Not found", "err");
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
    document.getElementById("filePreview").innerHTML = "";
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
  const pw = document.getElementById("newPw").value;
  if (!pw || pw.length < 6) {
    setMsg("pwMsg", "Minimum 6 characters.", "err");
    return;
  }
  setMsg(
    "pwMsg",
    "(Password change requires a dedicated endpoint — coming soon)",
    "",
  );
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

function chartOpts({ stacked = false, maintainAspectRatio = true } = {}) {
  return {
    responsive: true,
    maintainAspectRatio,
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
    document.getElementById("filePreview").innerHTML = "";
    ["eId", "eName", "eDept", "eEmail", "ePhone"].forEach(
      (id) => (document.getElementById(id).value = ""),
    );
  } catch (e) {
    setMsg("enrollMsg", "Error: " + e.message, "err");
    progress.style.display = "none";
    bar.style.width = "0%";
  }
};
