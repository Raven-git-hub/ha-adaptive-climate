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

// ---- Config: shared state -------------------------------------

let cfg = null;                 // working copy of the config
let cfgMode = "form";           // 'form' | 'raw'
let entityLists = { climate: [], sensor: [], binary_sensor: [], connected: false };
const uiOpen = { rooms: new Set(), profiles: new Set() };
let cfgListenersWired = false;

const DEFAULT_TIMES = { sunrise: "05:30", day: "08:00", afternoon: "14:00",
                        sunset: "16:00", night: "20:30", sleep: "22:00" };

function slugify(s) {
  return (s || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "room";
}

// --- path helpers: "rooms.0.units.1.entity_id", "system.trust.high_trust_deviation" ---
function getPath(obj, path, def) {
  const parts = path.split(".");
  let cur = obj;
  for (const p of parts) {
    if (cur == null) return def;
    cur = cur[/^\d+$/.test(p) ? Number(p) : p];
  }
  return cur === undefined ? def : cur;
}
function setPath(obj, path, value) {
  const parts = path.split(".");
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    const p = /^\d+$/.test(parts[i]) ? Number(parts[i]) : parts[i];
    if (cur[p] == null || typeof cur[p] !== "object") cur[p] = {};
    cur = cur[p];
  }
  const last = parts[parts.length - 1];
  if (value === undefined) delete cur[last]; else cur[last] = value;
}

function defaultProfile(id, name) {
  return { id, name, sections: Object.entries(DEFAULT_TIMES).map(([sid, time]) => ({
    id: sid, name: sid[0].toUpperCase() + sid.slice(1), trigger: { type: "clock", time } })) };
}

// ---- Config: entry point ---------------------------------------

async function renderConfig() {
  view.innerHTML = `<p class="placeholder">Loading config\u2026</p>`;
  try {
    cfg = await (await fetch("/api/config")).json();
  } catch {
    view.innerHTML = `<p class="placeholder">Could not reach the API.</p>`;
    return;
  }
  if (uiOpen.rooms.size === 0) (cfg.rooms || []).forEach((r) => uiOpen.rooms.add(r.id));

  view.innerHTML = `
    <div class="toolbar">
      <button id="cfg-save">Validate &amp; Save</button>
      <button id="cfg-check">Check deploy</button>
      <button id="cfg-deploy" class="danger">Deploy to Home Assistant</button>
      <span class="spacer"></span>
      <button id="cfg-mode" data-action="toggle-mode">${cfgMode === "form" ? "Raw JSON" : "Form view"}</button>
      <span id="cfg-msg" class="muted"></span>
    </div>
    <p class="muted">A change to section <b>times</b> is live from Save alone. A change to what a
      scene <b>does</b> \u2014 a unit's auto/off mode, transition time, or any room/unit/sensor \u2014
      needs <b>Deploy</b>. The running observer picks up a saved config after a container restart.</p>
    <div id="cfg-body"></div>
    <pre id="cfg-report" class="report"></pre>
    <datalist id="dl-climate"></datalist>
    <datalist id="dl-sensor"></datalist>
    <datalist id="dl-binary_sensor"></datalist>
  `;

  wireConfigToolbar();
  if (!cfgListenersWired) { wireConfigDelegation(); cfgListenersWired = true; }
  await loadEntities();
  renderConfigBody();
}

async function loadEntities() {
  try {
    const [c, s, b] = await Promise.all([
      fetch("/api/entities?domain=climate").then((r) => r.json()),
      fetch("/api/entities?domain=sensor").then((r) => r.json()),
      fetch("/api/entities?domain=binary_sensor").then((r) => r.json()),
    ]);
    entityLists = { climate: c.entities || [], sensor: s.entities || [],
                    binary_sensor: b.entities || [],
                    connected: c.connected, note: c.note };
    const fill = (id, list) => {
      const dl = document.getElementById(id);
      if (dl) dl.innerHTML = list.map((e) =>
        `<option value="${escapeHtml(e.entity_id)}">${escapeHtml(e.name)}</option>`).join("");
    };
    fill("dl-climate", entityLists.climate);
    fill("dl-sensor", entityLists.sensor);
    fill("dl-binary_sensor", entityLists.binary_sensor);
  } catch { /* pickers still work as free text */ }
}

function entityKnown(domain, entityId) {
  if (!entityLists.connected) return null;   // unknown either way; don't warn
  return entityLists[domain].some((e) => e.entity_id === entityId);
}

// ---- Config: toolbar (save/check/deploy/mode) -------------------

function wireConfigToolbar() {
  const msg = document.getElementById("cfg-msg");
  const report = document.getElementById("cfg-report");

  document.getElementById("cfg-save").addEventListener("click", async () => {
    msg.textContent = "saving\u2026";
    const r = await fetch("/api/config", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
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
                 "Assistant and lets them start controlling these thermostats. Continue?")) return;
    msg.textContent = "deploying\u2026"; report.textContent = "";
    const btn = document.getElementById("cfg-deploy"); btn.disabled = true;
    const r = await fetch("/api/deploy", { method: "POST" });
    const j = await r.json().catch(() => ({ detail: "request failed" }));
    msg.textContent = r.ok && j.ok ? "deployed" : "deploy finished with problems";
    report.textContent = JSON.stringify(j, null, 2);
    btn.disabled = false;
  });
}

// ---- Config: delegated input/change/click (survives body re-renders) ---

function wireConfigDelegation() {
  view.addEventListener("input", (e) => {
    const t = e.target;
    if (!t.dataset || !t.dataset.path || t.dataset.live !== "1") return;
    applyFieldValue(t);
    if (t.dataset.checkEntity) updateEntityCheck(t);
  });
  view.addEventListener("change", (e) => {
    const t = e.target;
    if (!t.dataset || !t.dataset.path) return;
    applyFieldValue(t);
    if (t.dataset.checkEntity) updateEntityCheck(t);
  });
  view.addEventListener("click", (e) => {
    const b = e.target.closest("[data-action]");
    if (!b || !view.contains(b)) return;
    const fn = cfgActions[b.dataset.action];
    if (fn) fn(b.dataset);
  });
}

function applyFieldValue(t) {
  let v;
  if (t.type === "checkbox") v = t.checked;
  else v = t.value;
  if (t.dataset.vtype === "number") v = v === "" ? undefined : parseFloat(v);
  else if (t.dataset.vtype === "csv") v = v.split(",").map((x) => x.trim()).filter(Boolean);

  if (t.dataset.leakDerive === "1") {
    const trimmed = (typeof v === "string" ? v.trim() : v) || "";
    // schema requires sensor_entity_id (if present at all) to start with
    // 'binary_sensor.', so an emptied field must delete the key, not save "".
    setPath(cfg, t.dataset.path, trimmed === "" ? undefined : trimmed);
    const enabledPath = t.dataset.path.replace(/\.sensor_entity_id$/, ".enabled");
    setPath(cfg, enabledPath, trimmed !== "");
    return;
  }
  setPath(cfg, t.dataset.path, v);
}

function updateEntityCheck(t) {
  const span = t.parentElement.querySelector(".entity-check");
  if (!span) return;
  const known = entityKnown(t.dataset.checkEntity, t.value);
  span.textContent = known === null ? "" : known ? "\u2713 in HA" : "\u26a0 not found in HA";
  span.className = "entity-check" + (known === false ? " warn" : known ? " ok" : "");
}

// ---- Config: actions (structural changes -> re-render body only) ---

const cfgActions = {
  "toggle-mode": () => {
    if (cfgMode === "form") {
      cfgMode = "raw";
    } else {
      try { cfg = JSON.parse(document.getElementById("cfg-raw").value); }
      catch (e) { alert("Invalid JSON, staying in raw view: " + e.message); return; }
      cfgMode = "form";
    }
    document.getElementById("cfg-mode").textContent = cfgMode === "form" ? "Raw JSON" : "Form view";
    renderConfigBody();
  },
  "toggle-room": (d) => { toggleSet(uiOpen.rooms, d.room); renderConfigBody(); },
  "toggle-profile": (d) => { toggleSet(uiOpen.profiles, d.profile); renderConfigBody(); },

  "add-room": () => {
    const id = slugify(prompt("Room id (letters/numbers/underscore, stable once deployed):", ""));
    if (!id || cfg.rooms.some((r) => r.id === id)) return;
    cfg.rooms.push({ id, name: id, enabled: true,
      schedule_profile: (cfg.schedule_profiles[0] || {}).id || "default",
      units: [], sensors: [], presence_sensors: [], scenes: {} });
    uiOpen.rooms.add(id);
    renderConfigBody();
  },
  "remove-room": (d) => {
    if (!confirm(`Remove room "${d.room}" from config? (Deploy afterward to remove it from Home Assistant too.)`)) return;
    cfg.rooms = cfg.rooms.filter((r) => r.id !== d.room);
    renderConfigBody();
  },
  "add-unit": (d) => {
    const room = cfg.rooms.find((r) => r.id === d.room);
    const id = slugify(prompt("Unit id:", "")); if (!id) return;
    room.units.push({ id, name: id, entity_id: "climate.", leak_detection: { enabled: false } });
    renderConfigBody();
  },
  "remove-unit": (d) => {
    const room = cfg.rooms.find((r) => r.id === d.room);
    room.units.splice(Number(d.idx), 1);
    renderConfigBody();
  },
  "add-sensor": (d) => {
    const room = cfg.rooms.find((r) => r.id === d.room);
    const id = slugify(prompt("Sensor id:", "")); if (!id) return;
    room.sensors.push({ id, name: id, entity_id: "sensor." });
    renderConfigBody();
  },
  "remove-sensor": (d) => {
    const room = cfg.rooms.find((r) => r.id === d.room);
    room.sensors.splice(Number(d.idx), 1);
    renderConfigBody();
  },

  "add-profile": () => {
    const id = slugify(prompt("Time profile id:", "")); if (!id) return;
    if (cfg.schedule_profiles.some((p) => p.id === id)) return;
    cfg.schedule_profiles.push(defaultProfile(id, id));
    uiOpen.profiles.add(id);
    renderConfigBody();
  },
  "remove-profile": (d) => {
    if (cfg.schedule_profiles.length <= 1) { alert("At least one time profile is required."); return; }
    if (!confirm(`Remove time profile "${d.profile}"? Rooms using it will need reassigning.`)) return;
    cfg.schedule_profiles = cfg.schedule_profiles.filter((p) => p.id !== d.profile);
    renderConfigBody();
  },
};

function toggleSet(set, id) { set.has(id) ? set.delete(id) : set.add(id); }

// ---- Config: render body ----------------------------------------

function renderConfigBody() {
  const body = document.getElementById("cfg-body");
  body.innerHTML = cfgMode === "raw" ? rawHtml() : formHtml();
}

function rawHtml() {
  return `<textarea id="cfg-raw" spellcheck="false">${escapeHtml(JSON.stringify(cfg, null, 2))}</textarea>
    <p class="muted">Switching back to Form view parses this JSON; fix any syntax errors first.</p>`;
}

function formHtml() {
  return settingsHtml() + profilesHtml() + roomsHtml();
}

function numField(path, val, opts = {}) {
  return `<input type="number" ${opts.step ? `step="${opts.step}"` : ""} data-path="${path}"
    data-vtype="number" data-live="1" value="${val ?? ""}" />`;
}
function textField(path, val, opts = {}) {
  return `<input type="text" data-path="${path}" data-live="1" value="${escapeHtml(val ?? "")}"
    ${opts.placeholder ? `placeholder="${escapeHtml(opts.placeholder)}"` : ""} />`;
}
function entityField(path, val, domain) {
  const known = entityKnown(domain, val || "");
  const cls = "entity-check" + (known === false ? " warn" : known ? " ok" : "");
  return `<span class="entity-field">
    <input type="text" list="dl-${domain}" data-path="${path}" data-live="1"
      data-check-entity="${domain}" value="${escapeHtml(val || "")}" placeholder="${domain}." />
    <span class="${cls}">${known === false ? "\u26a0 not found in HA" : known ? "\u2713 in HA" : ""}</span>
  </span>`;
}

function leakField(path, val, domain) {
  // Optional and clearable: blank means "no leak sensor picked - wire one
  // manually in HA if you like" (docs/DESIGN.md D6's original fallback).
  // Populated means "the system knows to go into Leak mode when this
  // triggers" - data-leak-derive tells the delegation handler to also set
  // leak_detection.enabled to match, and to delete the key on clear rather
  // than save an empty string (the schema requires a real binary_sensor.
  // prefix when the field is present at all).
  const known = entityKnown(domain, val || "");
  const cls = "entity-check" + (known === false ? " warn" : known ? " ok" : "");
  return `<span class="entity-field">
    <input type="text" list="dl-${domain}" data-path="${path}" data-live="1"
      data-check-entity="${domain}" data-leak-derive="1"
      value="${escapeHtml(val || "")}" placeholder="none \u2014 optional" />
    <span class="${cls}">${val ? (known === false ? "\u26a0 not found in HA" : known ? "\u2713 in HA" : "")
      : "blank = wire manually in HA"}</span>
  </span>`;
}

function settingsHtml() {
  const sys = cfg.system || (cfg.system = {});
  const trust = sys.trust || (sys.trust = {});
  const learn = cfg.learning || (cfg.learning = {});
  const ha = cfg.homeassistant || (cfg.homeassistant = {});
  return `<details class="panel"><summary>System, learning &amp; connection settings</summary>
    <div class="field-grid">
      <label>Home Assistant URL ${textField("homeassistant.base_url", ha.base_url)}</label>
      <label class="chk"><input type="checkbox" data-path="homeassistant.verify_ssl" data-live="1"
        ${ha.verify_ssl !== false ? "checked" : ""}/> Verify SSL</label>
      <label>Temperature unit
        <select data-path="system.temperature_unit" data-live="1">
          <option value="C" ${sys.temperature_unit !== "F" ? "selected" : ""}>Celsius</option>
          <option value="F" ${sys.temperature_unit === "F" ? "selected" : ""}>Fahrenheit</option>
        </select></label>
      <label>Heartbeat interval (min) ${numField("system.heartbeat_interval_minutes", sys.heartbeat_interval_minutes ?? 10)}</label>
      <label>Reactive min delta (\u00b0) ${numField("system.reactive_min_delta", sys.reactive_min_delta ?? 0.5, {step:"0.1"})}</label>
      <label>Max maintenance step (\u00b0) ${numField("system.max_step_degrees", sys.max_step_degrees ?? 2.0, {step:"0.5"})}</label>
      <label>Correction cap (min) ${numField("system.correction_max_minutes", sys.correction_max_minutes ?? 60)}</label>
      <label>High-trust deviation (\u00b0) ${numField("system.trust.high_trust_deviation", trust.high_trust_deviation ?? 0.5, {step:"0.1"})}</label>
      <label>Low-trust deviation (\u00b0) ${numField("system.trust.low_trust_deviation", trust.low_trust_deviation ?? 5.0, {step:"0.5"})}</label>
      <label>Analysis window (days) ${numField("learning.analysis_window_days", learn.analysis_window_days ?? 21)}</label>
      <label>Bootstrap min days ${numField("learning.bootstrap_min_days", learn.bootstrap_min_days ?? 7)}</label>
      <label>Validity delay (days) ${numField("learning.validity_delay_days", learn.validity_delay_days ?? 2)}</label>
      <label>Reactive weight (\u00d7) ${numField("learning.reactive_weight", learn.reactive_weight ?? 5)}</label>
      <label class="wide">External guard booleans (comma-separated, coexistence only)
        <input type="text" data-path="system.external_guards" data-vtype="csv" data-live="1"
          value="${escapeHtml((sys.external_guards || []).join(", "))}" /></label>
    </div>
  </details>`;
}

function profilesHtml() {
  const profiles = cfg.schedule_profiles || [];
  return `<div class="panel-header"><h2>Time Profiles</h2>
    <button data-action="add-profile">+ Add profile</button></div>
    ${profiles.map((p, i) => profileHtml(p, i)).join("")}`;
}

function profileHtml(p, i) {
  const open = uiOpen.profiles.has(p.id);
  const rows = SECTIONS.map((sid) => {
    const idx = p.sections.findIndex((s) => s.id === sid);
    const sec = p.sections[idx] || { name: sid, trigger: { time: "" } };
    return `<tr><td>${sec.name || sid}</td><td>
      <input type="time" data-path="schedule_profiles.${i}.sections.${idx}.trigger.time"
        data-live="1" value="${sec.trigger?.time || ""}" /></td></tr>`;
  }).join("");
  return `<details class="panel" ${open ? "open" : ""}>
    <summary data-action="toggle-profile" data-profile="${p.id}">${p.name || p.id}
      <span class="muted">(${p.id})</span></summary>
    <div class="field-grid">
      <label>Name ${textField(`schedule_profiles.${i}.name`, p.name)}</label>
    </div>
    <table class="now"><tr><th>Section</th><th>Time</th></tr>${rows}</table>
    <button class="danger-outline" data-action="remove-profile" data-profile="${p.id}">Remove profile</button>
  </details>`;
}

function roomsHtml() {
  const rooms = cfg.rooms || [];
  const profileOpts = (cfg.schedule_profiles || []).map((p) => p.id);
  return `<div class="panel-header"><h2>Rooms</h2>
    <button data-action="add-room">+ Add room</button></div>
    ${rooms.map((r, i) => roomHtml(r, i, profileOpts)).join("")}`;
}

function roomHtml(room, i, profileOpts) {
  const open = uiOpen.rooms.has(room.id);
  return `<details class="panel" ${open ? "open" : ""}>
    <summary data-action="toggle-room" data-room="${room.id}">
      ${room.name || room.id} <span class="muted">(${room.id})</span>
      ${room.enabled === false ? '<span class="pill off">disabled</span>' : ""}</summary>
    <div class="field-grid">
      <label>Name ${textField(`rooms.${i}.name`, room.name)}</label>
      <label class="chk"><input type="checkbox" data-path="rooms.${i}.enabled" data-live="1"
        ${room.enabled !== false ? "checked" : ""}/> Enabled</label>
      <label>Time profile
        <select data-path="rooms.${i}.schedule_profile" data-live="1">
          ${profileOpts.map((pid) => `<option value="${pid}" ${pid === room.schedule_profile ? "selected" : ""}>${pid}</option>`).join("")}
        </select></label>
    </div>
    ${unitsHtml(room, i)}
    ${sensorsHtml(room, i)}
    ${scenesHtml(room, i)}
    <button class="danger-outline" data-action="remove-room" data-room="${room.id}">Remove room</button>
  </details>`;
}

function unitsHtml(room, i) {
  const rows = room.units.map((u, j) => `<tr>
    <td>${textField(`rooms.${i}.units.${j}.id`, u.id)}</td>
    <td>${textField(`rooms.${i}.units.${j}.name`, u.name)}</td>
    <td>${entityField(`rooms.${i}.units.${j}.entity_id`, u.entity_id, "climate")}</td>
    <td>${leakField(`rooms.${i}.units.${j}.leak_detection.sensor_entity_id`,
      u.leak_detection?.sensor_entity_id, "binary_sensor")}</td>
    <td><button class="danger-outline" data-action="remove-unit" data-room="${room.id}" data-idx="${j}">remove</button></td>
  </tr>`).join("");
  return `<h3>AC units <button class="small" data-action="add-unit" data-room="${room.id}">+ add</button></h3>
    <table class="now"><tr><th>id</th><th>name</th><th>entity_id</th><th>leak sensor</th><th></th></tr>
    ${rows || `<tr class="empty"><td colspan="5">no units yet</td></tr>`}</table>`;
}

function sensorsHtml(room, i) {
  const rows = room.sensors.map((s, j) => `<tr>
    <td>${textField(`rooms.${i}.sensors.${j}.id`, s.id)}</td>
    <td>${textField(`rooms.${i}.sensors.${j}.name`, s.name)}</td>
    <td>${entityField(`rooms.${i}.sensors.${j}.entity_id`, s.entity_id, "sensor")}</td>
    <td><button class="danger-outline" data-action="remove-sensor" data-room="${room.id}" data-idx="${j}">remove</button></td>
  </tr>`).join("");
  return `<h3>Temperature sensors <button class="small" data-action="add-sensor" data-room="${room.id}">+ add</button></h3>
    <table class="now"><tr><th>id</th><th>name</th><th>entity_id</th><th></th></tr>
    ${rows || `<tr class="empty"><td colspan="4">no sensors yet</td></tr>`}</table>`;
}

function scenesHtml(room, i) {
  if (!room.units.length) return `<h3>Scenes</h3><p class="muted">add a unit first</p>`;
  const head = `<tr><th>Section</th><th>Transition (s)</th>${room.units.map((u) =>
    `<th>${u.name || u.id}</th>`).join("")}</tr>`;
  const rows = SECTIONS.map((sid) => {
    const scene = (room.scenes || {})[sid] || {};
    const trans = numField(`rooms.${i}.scenes.${sid}.transition_seconds`, scene.transition_seconds ?? 0);
    const cells = room.units.map((u) => {
      const mode = getPath(room, `scenes.${sid}.units.${u.id}.mode`, "auto");
      return `<td><select data-path="rooms.${i}.scenes.${sid}.units.${u.id}.mode" data-live="1">
        <option value="auto" ${mode === "auto" ? "selected" : ""}>auto</option>
        <option value="off" ${mode === "off" ? "selected" : ""}>off</option>
      </select></td>`;
    }).join("");
    return `<tr><td>${sid}</td><td>${trans}</td>${cells}</tr>`;
  }).join("");
  return `<h3>Scenes <span class="muted">(needs Deploy to take effect)</span></h3>
    <table class="now">${head}${rows}</table>`;
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
