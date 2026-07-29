// Adaptive Climate - UI shell (vanilla JS, no build step).
// Almanac view is built (Phase 15 first slice); Now / Analysis / Config /
// Log remain placeholders until their data paths land.

const view = document.getElementById("view");
const strip = document.getElementById("status-strip");

const SECTIONS = ["sunrise", "day", "afternoon", "sunset", "night", "sleep"];
const fmt = (x, d = 1) => (x === null || x === undefined ? "—" : Number(x).toFixed(d));

const views = {
  now:      () => placeholder("Now"),
  analysis: () => placeholder("Analysis"),
  almanac:  renderAlmanac,
  config:   () => placeholder("Config"),
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
