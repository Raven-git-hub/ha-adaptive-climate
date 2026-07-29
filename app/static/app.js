// Adaptive Climate - UI shell (vanilla JS, no build step).
// Almanac view is built (Phase 15 first slice); Now / Analysis / Config /
// Log remain placeholders until their data paths land.

const view = document.getElementById("view");
const strip = document.getElementById("status-strip");

const SECTIONS = ["sunrise", "day", "afternoon", "sunset", "night", "sleep"];
const fmt = (x, d = 1) => (x === null || x === undefined ? "—" : Number(x).toFixed(d));

const views = {
  now:      renderNow,
  analysis: () => placeholder("Analysis"),
  almanac:  renderAlmanac,
  config:   renderConfig,
  log:      () => placeholder("Log"),
};

document.querySelectorAll("#tabs button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    (views[b.dataset.view] || (() => placeholder(b.dataset.view)))();
  });
});

function placeholder(name) {
  view.innerHTML = `<p class="placeholder">"${name}" view — not yet built.</p>`;
}

// ---- Almanac -------------------------------------------------------

async function renderAlmanac() {
  view.innerHTML = `<p class="placeholder">Loading almanac…</p>`;
  let rooms;
  try {
    rooms = (await (await fetch("/api/rooms")).json()).rooms;
  } catch {
    view.innerHTML = `<p class="placeholder">Could not reach the API.</p>`;
    return;
  }
  if (!rooms.length) {
    view.innerHTML = `<p class="placeholder">No rooms configured yet.</p>`;
    return;
  }

  const parts = [`<div class="toolbar">
     <button id="rerun">Re-run analysis</button>
     <span id="rerun-msg" class="muted"></span></div>`];

  for (const room of rooms) {
    let alm = { sections: {} };
    try { alm = await (await fetch(`/api/almanac/${room.id}`)).json(); } catch {}
    parts.push(renderRoom(room, alm.sections || {}));
  }
  view.innerHTML = parts.join("");

  document.getElementById("rerun").addEventListener("click", async (e) => {
    const msg = document.getElementById("rerun-msg");
    e.target.disabled = true; msg.textContent = "running…";
    try {
      const r = await (await fetch("/api/analysis/run", { method: "POST" })).json();
      const n = Object.values(r.built || {}).flat().length;
      msg.textContent = n ? `rebuilt ${n} section almanac(s)` : "no data to learn from yet";
    } catch { msg.textContent = "failed"; }
    e.target.disabled = false;
    renderAlmanac();
  });
}

function renderRoom(room, sections) {
  const learned = SECTIONS.some((s) => sections[s]);
  if (!learned) {
    return `<section class="room"><h2>${room.name}</h2>
      <p class="placeholder">No almanac yet — observing (provisional).</p></section>`;
  }
  const units = room.units, sensors = room.sensors;
  const head = `<tr><th>Section</th><th>State</th>
    ${units.map((u) => `<th>${u.name}<br><span class="muted">setpoint</span></th>`).join("")}
    ${sensors.map((s) => `<th>${s.name}<br><span class="muted">comfort · band · trust</span></th>`).join("")}</tr>`;

  const rows = SECTIONS.map((sec) => {
    const d = sections[sec];
    if (!d) return `<tr class="empty"><td>${sec}</td><td>—</td>
      <td colspan="${units.length + sensors.length}"></td></tr>`;
    const uCells = units.map((u) => {
      const v = (d.units[u.id] || {});
      return `<td>${v.off ? "off" : fmt(v.setpoint)}</td>`;
    }).join("");
    const sCells = sensors.map((s) => {
      const v = (d.sensors[s.id] || {});
      const t = v.trust ?? 0;
      return `<td><span class="comfort">${fmt(v.comfort)}</span>
        <span class="muted">±${fmt(v.band)}</span>
        <span class="trust"><span style="width:${Math.round(t * 100)}%"></span></span></td>`;
    }).join("");
    return `<tr><td>${sec}</td><td><span class="state ${d.state}">${d.state}</span></td>${uCells}${sCells}</tr>`;
  }).join("");

  return `<section class="room"><h2>${room.name}</h2>
    <table class="almanac">${head}${rows}</table></section>`;
}

// ---- health strip --------------------------------------------------

async function poll() {
  try {
    const j = await (await fetch("/healthz")).json();
    strip.textContent = j.idle ? "idle (no runtime)"
      : j.ha_connected ? "connected" : "degraded";
  } catch { strip.textContent = "unreachable"; }
}
poll();
setInterval(poll, 15000);

// ---- Now --------------------------------------------------------

async function renderNow() {
  view.innerHTML = `<p class="placeholder">Loading live state\u2026</p>`;
  let data;
  try {
    const r = await fetch("/api/now");
    if (r.status === 503) {
      view.innerHTML = `<p class="placeholder">Not connected to Home Assistant yet
        \u2014 set AC_HA_URL / AC_HA_TOKEN and restart the container.</p>`;
      return;
    }
    data = await r.json();
  } catch {
    view.innerHTML = `<p class="placeholder">Could not reach the API.</p>`;
    return;
  }
  if (!data.rooms.length) {
    view.innerHTML = `<p class="placeholder">No rooms configured yet.</p>`;
    return;
  }
  view.innerHTML = data.rooms.map(renderNowRoom).join("");
}

function pill(label, on) {
  return `<span class="pill ${on ? "on" : "off"}">${label}</span>`;
}

function renderNowRoom(room) {
  const guardOn = room.guard === "on", holdOn = room.hold === "on";
  const units = room.units.map((u) => `
    <tr>
      <td>${u.name}</td>
      <td>${u.state ?? "\u2014"}</td>
      <td>${fmt(u.current_temperature)}\u00b0</td>
      <td>${fmt(u.setpoint)}\u00b0</td>
      <td>${u.fan_mode ?? "\u2014"}</td>
    </tr>`).join("");
  const sensors = room.sensors.map((s) => `
    <tr><td>${s.name}</td><td>${s.reading ?? "\u2014"} ${s.unit ?? ""}</td></tr>`).join("");

  return `<section class="room">
    <h2>${room.name}
      <span class="muted">scene: ${room.scene ?? "\u2014"}</span>
      ${pill("active", guardOn)} ${pill("hold", holdOn)}
    </h2>
    <table class="now">
      <tr><th>Unit</th><th>State</th><th>Current</th><th>Setpoint</th><th>Fan</th></tr>
      ${units || `<tr class="empty"><td colspan="5">no units</td></tr>`}
    </table>
    <table class="now sensors">
      <tr><th>Sensor</th><th>Reading</th></tr>
      ${sensors || `<tr class="empty"><td colspan="2">no sensors</td></tr>`}
    </table>
  </section>`;
}

// ---- Config -------------------------------------------------------

async function renderConfig() {
  view.innerHTML = `<p class="placeholder">Loading config\u2026</p>`;
  let cfg;
  try {
    cfg = await (await fetch("/api/config")).json();
  } catch {
    view.innerHTML = `<p class="placeholder">Could not reach the API.</p>`;
    return;
  }

  view.innerHTML = `
    <div class="toolbar">
      <button id="cfg-save">Validate &amp; Save</button>
      <button id="cfg-check">Check deploy</button>
      <button id="cfg-deploy" class="danger">Deploy to Home Assistant</button>
      <span id="cfg-msg" class="muted"></span>
    </div>
    <p class="muted">Raw config JSON. A structured room/unit/sensor picker is
      planned; this text editor is the honest first slice \u2014 it validates
      against the same schema the backend enforces.</p>
    <textarea id="cfg-text" spellcheck="false">${escapeHtml(JSON.stringify(cfg, null, 2))}</textarea>
    <pre id="cfg-report" class="report"></pre>
  `;

  const msg = document.getElementById("cfg-msg");
  const report = document.getElementById("cfg-report");
  const textarea = document.getElementById("cfg-text");

  document.getElementById("cfg-save").addEventListener("click", async () => {
    let parsed;
    try { parsed = JSON.parse(textarea.value); }
    catch (e) { msg.textContent = "invalid JSON: " + e.message; return; }
    msg.textContent = "saving\u2026";
    const r = await fetch("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(parsed),
    });
    const j = await r.json().catch(() => ({}));
    msg.textContent = r.ok ? "saved" : "rejected: " + (j.detail || r.status);
  });

  document.getElementById("cfg-check").addEventListener("click", async () => {
    msg.textContent = "checking\u2026"; report.textContent = "";
    const r = await fetch("/api/deploy/check");
    const j = await r.json().catch(() => ({ detail: "request failed" }));
    msg.textContent = r.ok ? "check complete" : "check failed";
    report.textContent = JSON.stringify(j, null, 2);
  });

  document.getElementById("cfg-deploy").addEventListener("click", async () => {
    if (!confirm("This writes helpers and automations to your real Home " +
                 "Assistant and lets them start controlling these thermostats. Continue?")) {
      return;
    }
    msg.textContent = "deploying\u2026"; report.textContent = "";
    const btn = document.getElementById("cfg-deploy"); btn.disabled = true;
    const r = await fetch("/api/deploy", { method: "POST" });
    const j = await r.json().catch(() => ({ detail: "request failed" }));
    msg.textContent = r.ok && j.ok ? "deployed" : "deploy finished with problems";
    report.textContent = JSON.stringify(j, null, 2);
    btn.disabled = false;
  });
}

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
