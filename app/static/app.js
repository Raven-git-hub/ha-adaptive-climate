// Adaptive Climate - UI shell (vanilla JS, no build step).
// Almanac view is built (Phase 15 first slice); Now / Analysis / Config /
// Log remain placeholders until their data paths land.

const view = document.getElementById("view");
const strip = document.getElementById("status-strip");

const SECTIONS = ["sunrise", "day", "afternoon", "sunset", "night", "sleep"];
const fmt = (x, d = 1) => (x === null || x === undefined ? "—" : Number(x).toFixed(d));

const views = {
  now:      renderNow,
  analysis: renderAnalysis,
  almanac:  renderAlmanac,
  config:   renderConfig,
  log:      renderLog,
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

// ---- Analysis ------------------------------------------------------

const SENSOR_COLORS = ["#4bb3fd", "#4fd6a0", "#e0a030", "#c78bff", "#ff8f6b"];
const analysisState = { room: null, day: null };

// Local YYYY-MM-DD from a Date's local components. Using toISOString() here
// would convert to UTC and, in a positive-offset zone like +08:00, roll the
// date back a day - which silently fetched the wrong days (the chart's blank).
function ymdLocal(dt) {
  const p = (n) => String(n).padStart(2, "0");
  return `${dt.getFullYear()}-${p(dt.getMonth() + 1)}-${p(dt.getDate())}`;
}

async function renderAnalysis() {
  view.innerHTML = `<p class="placeholder">Loading\u2026</p>`;
  let rooms;
  try {
    rooms = (await (await fetch("/api/rooms")).json()).rooms;
  } catch {
    view.innerHTML = `<p class="placeholder">Could not reach the API.</p>`;
    return;
  }
  if (!rooms.length) { view.innerHTML = `<p class="placeholder">No rooms configured.</p>`; return; }
  if (!analysisState.room || !rooms.find((r) => r.id === analysisState.room))
    analysisState.room = rooms[0].id;
  if (!analysisState.day) analysisState.day = ymdLocal(new Date());

  view.innerHTML = `
    <div class="toolbar">
      <select id="an-room">${rooms.map((r) =>
        `<option value="${r.id}" ${r.id === analysisState.room ? "selected" : ""}>${r.name}</option>`).join("")}</select>
      <button id="an-prev">\u2039 prev</button>
      <input type="date" id="an-date" value="${analysisState.day}" />
      <button id="an-next">next \u203a</button>
      <span id="an-msg" class="muted"></span>
    </div>
    <div id="an-legend" class="legend"></div>
    <div id="an-chart"></div>`;

  document.getElementById("an-room").addEventListener("change", (e) => {
    analysisState.room = e.target.value; renderAnalysis();
  });
  document.getElementById("an-date").addEventListener("change", (e) => {
    analysisState.day = e.target.value; renderAnalysis();
  });
  document.getElementById("an-prev").addEventListener("click", () => { shiftDay(-1); });
  document.getElementById("an-next").addEventListener("click", () => { shiftDay(1); });

  // Fetch the selected day plus the two before it, for a 3-day window.
  const days = [-2, -1, 0].map((d) => {
    const dt = new Date(analysisState.day + "T00:00:00");
    dt.setDate(dt.getDate() + d);
    return ymdLocal(dt);
  });
  let parts;
  try {
    parts = await Promise.all(days.map((d) =>
      fetch(`/api/activity/${analysisState.room}?day=${d}`).then((r) => r.json())));
  } catch {
    document.getElementById("an-msg").textContent = "could not load activity";
    return;
  }
  const act = {
    heartbeats: parts.flatMap((p) => p.heartbeats || []).sort((a, b) => a.ts.localeCompare(b.ts)),
    reactions: parts.flatMap((p) => p.reactions || []),
    section_runs: parts.flatMap((p) => p.section_runs || []),
    almanac: parts[parts.length - 1].almanac || {},   // current, from the selected day
    tz: parts[parts.length - 1].tz || null,
  };
  drawAnalysis(act, rooms.find((r) => r.id === analysisState.room), days);
}

function shiftDay(delta) {
  const d = new Date(analysisState.day + "T00:00:00");
  d.setDate(d.getDate() + delta);
  analysisState.day = ymdLocal(d);
  renderAnalysis();
}

function drawAnalysis(act, room, days) {
  const chart = document.getElementById("an-chart");
  const legend = document.getElementById("an-legend");
  const hbs = act.heartbeats || [];
  if (!hbs.length) {
    chart.innerHTML = `<p class="placeholder">No heartbeats recorded in this window yet.
      A heartbeat lands every ten minutes; check back once some have accrued.</p>`;
    legend.innerHTML = "";
    return;
  }

  const toEpoch = (ts) => Math.floor(new Date(ts).getTime() / 1000);
  // The window bounds must be interpreted at the SAME UTC offset as the
  // heartbeat timestamps (e.g. +08:00), not the browser's local zone, or the
  // pinned x-range is shifted off the data and nothing draws. Take the offset
  // from the data itself.
  const offMatch = (hbs[0].ts || "").match(/([+-]\d{2}:\d{2}|Z)$/);
  const off = !offMatch ? "" : offMatch[1] === "Z" ? "+00:00" : offMatch[1];
  const dayStart = (d) => Math.floor(new Date(`${d}T00:00:00${off}`).getTime() / 1000);
  const xMin = dayStart(days[0]);
  const xMax = dayStart(days[days.length - 1]) + 86400;
  const xs = hbs.map((h) => toEpoch(h.ts));

  const sensors = room.sensors;
  const units = room.units;
  // one y-series per sensor (temperature), plus one per unit (setpoint)
  const sensorSeries = sensors.map((s) => hbs.map((h) => h.sensors[s.id] ?? null));
  const unitSeries = units.map((u) => hbs.map((h) => (h.units[u.id] || {}).setpoint ?? null));

  const data = [xs, ...sensorSeries, ...unitSeries];

  const series = [{}];
  sensors.forEach((s, i) => series.push({
    label: s.name, stroke: SENSOR_COLORS[i % SENSOR_COLORS.length], width: 2,
    points: { show: false },
  }));
  units.forEach((u) => series.push({
    label: u.name + " setpoint", stroke: "#8b95a5", width: 1.5, dash: [6, 4],
    points: { show: false },
  }));

  // section spans + comfort bands + reaction markers, drawn under the lines
  const runs = act.section_runs || [];
  const almanac = act.almanac || {};
  const reactions = (act.reactions || []).map((r) => toEpoch(r.ts));

  const bandHook = (u) => {
    const ctx = u.ctx;
    ctx.save();
    // section boundary verticals + labels
    for (const run of runs) {
      if (!run.actual_start) continue;
      const x = u.valToPos(toEpoch(run.actual_start), "x", true);
      ctx.strokeStyle = "rgba(139,149,165,.25)";
      ctx.setLineDash([2, 3]); ctx.beginPath();
      ctx.moveTo(x, u.bbox.top); ctx.lineTo(x, u.bbox.top + u.bbox.height); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "rgba(139,149,165,.7)"; ctx.font = "10px system-ui";
      ctx.fillText(run.section, x + 3, u.bbox.top + 11);
    }
    // comfort bands per sensor per section (faint fill)
    for (const run of runs) {
      const sec = almanac[run.section];
      if (!sec || !run.actual_start) continue;
      const x0 = u.valToPos(toEpoch(run.actual_start), "x", true);
      const x1 = run.ended_at ? u.valToPos(toEpoch(run.ended_at), "x", true)
                              : u.bbox.left + u.bbox.width;
      sensors.forEach((s, i) => {
        const sd = (sec.sensors || {})[s.id];
        if (!sd || sd.comfort == null) return;
        const yTop = u.valToPos(sd.comfort + (sd.band ?? 0), "y", true);
        const yBot = u.valToPos(sd.comfort - (sd.band ?? 0), "y", true);
        ctx.fillStyle = hexToRgba(SENSOR_COLORS[i % SENSOR_COLORS.length], 0.06);
        ctx.fillRect(x0, yTop, Math.max(1, x1 - x0), yBot - yTop);
      });
    }
    // reaction markers
    for (const rx of reactions) {
      const x = u.valToPos(rx, "x", true);
      ctx.strokeStyle = "#e05252"; ctx.setLineDash([]); ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(x, u.bbox.top); ctx.lineTo(x, u.bbox.top + u.bbox.height); ctx.stroke();
    }
    ctx.restore();
  };

  const opts = {
    width: chart.clientWidth || 900,
    height: 420,
    scales: {
      x: { time: true, range: [xMin, xMax] },
      y: { range: [15, 30] },
    },
    axes: [
      { stroke: "#8b95a5", grid: { stroke: "rgba(139,149,165,.08)" } },
      { stroke: "#8b95a5", grid: { stroke: "rgba(139,149,165,.08)" },
        label: "\u00b0C", labelSize: 30 },
    ],
    series,
    hooks: { drawClear: [bandHook] },
    legend: { show: false },
  };
  if (act.tz) {
    try { opts.tzDate = (ts) => uPlot.tzDate(new Date(ts * 1000), act.tz); } catch {}
  }

  chart.innerHTML = "";
  try {
    const plot = new uPlot(opts, data, chart);
    window.addEventListener("resize", () => plot.setSize({ width: chart.clientWidth, height: 420 }),
      { once: true });
  } catch (e) {
    chart.innerHTML = `<p class="placeholder">Chart failed to render: ${e.message}</p>`;
    return;
  }

  legend.innerHTML = sensors.map((s, i) =>
    `<span class="key"><i style="background:${SENSOR_COLORS[i % SENSOR_COLORS.length]}"></i>${s.name}</span>`).join("")
    + units.map((u) => `<span class="key"><i class="dash"></i>${u.name} setpoint</span>`).join("")
    + `<span class="key"><i style="background:#e05252"></i>your intervention</span>`
    + `<span class="muted">${reactions.length === 0 ? "\u2014 a day with no red marks is a day it got right" : ""}</span>`;
}

function hexToRgba(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

// ---- Log -----------------------------------------------------------

const logState = { room: "", category: "", severity: "" };
const CATEGORIES = ["crossover", "maintenance", "quorum", "correction", "reactive",
  "hold", "heartbeat", "analysis", "almanac", "leak", "connection", "deploy",
  "config", "validation"];
const SEVERITIES = ["debug", "info", "warning", "error"];

async function renderLog() {
  view.innerHTML = `<p class="placeholder">Loading log\u2026</p>`;
  let rooms = [];
  try { rooms = (await (await fetch("/api/rooms")).json()).rooms; } catch {}

  const sel = (id, opts, cur) =>
    `<select id="${id}"><option value="">all</option>${opts.map((o) =>
      `<option value="${o.v ?? o}" ${(o.v ?? o) === cur ? "selected" : ""}>${o.t ?? o}</option>`).join("")}</select>`;

  view.innerHTML = `
    <div class="toolbar">
      ${sel("log-room", rooms.map((r) => ({ v: r.id, t: r.name })), logState.room)}
      ${sel("log-cat", CATEGORIES, logState.category)}
      ${sel("log-sev", SEVERITIES, logState.severity)}
      <button id="log-refresh">refresh</button>
      <span id="log-msg" class="muted"></span>
    </div>
    <table class="log"><thead><tr><th>Time</th><th>Room</th><th>Severity</th>
      <th>Category</th><th>Message</th></tr></thead><tbody id="log-body"></tbody></table>`;

  const apply = () => {
    logState.room = document.getElementById("log-room").value;
    logState.category = document.getElementById("log-cat").value;
    logState.severity = document.getElementById("log-sev").value;
    loadLog();
  };
  ["log-room", "log-cat", "log-sev"].forEach((id) =>
    document.getElementById(id).addEventListener("change", apply));
  document.getElementById("log-refresh").addEventListener("click", loadLog);

  loadLog();
}

async function loadLog() {
  const body = document.getElementById("log-body");
  const msg = document.getElementById("log-msg");
  const qs = new URLSearchParams();
  if (logState.room) qs.set("room_id", logState.room);
  if (logState.category) qs.set("category", logState.category);
  if (logState.severity) qs.set("severity", logState.severity);
  let events;
  try {
    events = (await (await fetch("/api/events?" + qs.toString())).json()).events;
  } catch {
    msg.textContent = "could not load log"; return;
  }
  msg.textContent = `${events.length} event(s)`;
  if (!events.length) {
    body.innerHTML = `<tr class="empty"><td colspan="5">nothing logged yet for this filter</td></tr>`;
    return;
  }
  body.innerHTML = events.map((e, i) => {
    const t = (e.ts || "").replace("T", " ").slice(0, 19);
    const detail = e.detail ? `<tr class="detail" id="d${i}" hidden><td colspan="5"><pre>${
      escapeHtml(typeof e.detail === "string" ? e.detail : JSON.stringify(e.detail, null, 2))}</pre></td></tr>` : "";
    return `<tr class="ev ${e.severity}" ${e.detail ? `data-d="d${i}"` : ""}>
      <td>${t}</td><td>${e.room_id ?? "\u2014"}</td>
      <td><span class="sev ${e.severity}">${e.severity}</span></td>
      <td>${e.category}</td><td>${escapeHtml(e.message)}</td></tr>${detail}`;
  }).join("");
  body.querySelectorAll("tr.ev[data-d]").forEach((row) => {
    row.style.cursor = "pointer";
    row.addEventListener("click", () => {
      const d = document.getElementById(row.dataset.d);
      if (d) d.hidden = !d.hidden;
    });
  });
}
