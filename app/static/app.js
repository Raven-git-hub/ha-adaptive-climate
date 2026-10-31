// Adaptive Climate - UI shell (vanilla JS, no build step).
// Views: Now (now.js), Analysis, Almanac, Config and Log.

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
    NowView.stop();             // the Now view polls only while it is open
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
  const head = `<tr><th>Section</th><th>Status</th>
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
    const status = d.status ?? "\u2014";      // provisional | learned (API: sections[s].status)
    return `<tr><td>${sec}</td><td><span class="state ${status}">${status}</span></td>${uCells}${sCells}</tr>`;
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
// The Now view lives in now.js (NowView, renderNow).

// ---- Config -------------------------------------------------------

// ---- Config: shared state -------------------------------------

let cfg = null;                 // working copy of the config
let cfgMode = "form";           // 'form' | 'raw'
let entityLists = { climate: [], sensor: [], binary_sensor: [], weather: [], connected: false };
let cfgSchema = null;           // schema/config.schema.json, for defaults and ranges
let pendingApply = null;        // a preview awaiting Confirm: {config, token}
const uiOpen = { rooms: new Set(), profiles: new Set() };
let cfgListenersWired = false;

const DEFAULT_TIMES = { sunrise: "05:30", day: "08:00", afternoon: "14:00",
                        sunset: "16:00", night: "20:30", sleep: "22:00" };

function slugify(s) {
  return (s || "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "room";
}

// `base`, or base_2, base_3... - the first not already in `taken`.
function uniqueId(base, taken) {
  const used = new Set(taken);
  if (!used.has(base)) return base;
  let n = 2;
  while (used.has(`${base}_${n}`)) n++;
  return `${base}_${n}`;
}

// Ids the server rejects as duplicates: rooms, and units and sensors within
// a room (a repeated sensor id makes one sensor overwrite the other).
function duplicateIds(config) {
  const found = [];
  const check = (items, what) => {
    const seen = new Set();
    for (const it of items || []) {
      if (seen.has(it.id)) found.push(`${what} "${it.id}"`);
      seen.add(it.id);
    }
  };
  check(config.rooms, "room");
  for (const r of config.rooms || []) {
    check(r.units, `unit in ${r.id}:`);
    check(r.sensors, `sensor in ${r.id}:`);
  }
  return found;
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

  pendingApply = null;
  view.innerHTML = `
    <div class="toolbar">
      <button id="cfg-apply" class="primary">Apply</button>
      <span id="cfg-msg" class="cfg-msg" role="status"></span>
      <span class="spacer"></span>
      <button id="cfg-mode" data-action="toggle-mode">${cfgMode === "form" ? "Raw JSON" : "Form view"}</button>
    </div>
    <p class="muted cfg-intro"><b>Apply</b> checks the config, then works out what it would change
      in Home Assistant. If nothing there changes it is saved and in force at once; if helpers or
      automations change you see them first and confirm before they are deployed. No restart is
      needed. Outdoor and block settings take effect from each room's next section.</p>
    <div id="cfg-apply-panel"></div>
    <div id="cfg-body"></div>
    <datalist id="dl-climate"></datalist>
    <datalist id="dl-sensor"></datalist>
    <datalist id="dl-binary_sensor"></datalist>
  `;

  wireConfigToolbar();
  if (!cfgListenersWired) { wireConfigDelegation(); cfgListenersWired = true; }
  await Promise.all([loadEntities(), loadSchema()]);
  renderConfigBody();
}

async function loadSchema() {
  if (cfgSchema) return;
  try { cfgSchema = await (await fetch("/api/schema")).json(); }
  catch { cfgSchema = null; }   /* fields still work, without defaults */
}

// The schema node for a config path ("system.control.boost_degrees").
function schemaAt(path) {
  let node = cfgSchema;
  for (const part of path.split(".")) {
    node = node && node.properties ? node.properties[part] : undefined;
  }
  return node || null;
}

async function loadEntities() {
  try {
    const [c, s, b, w] = await Promise.all([
      fetch("/api/entities?domain=climate").then((r) => r.json()),
      fetch("/api/entities?domain=sensor").then((r) => r.json()),
      fetch("/api/entities?domain=binary_sensor").then((r) => r.json()),
      fetch("/api/entities?domain=weather").then((r) => r.json()),
    ]);
    entityLists = { climate: c.entities || [], sensor: s.entities || [],
                    binary_sensor: b.entities || [], weather: w.entities || [],
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

// ---- Config: toolbar (apply/mode) ------------------------------

function wireConfigToolbar() {
  document.getElementById("cfg-apply").addEventListener("click", () => {
    // raw mode applies what is in the text box, not the last form state
    if (cfgMode === "raw") {
      try { cfg = JSON.parse(document.getElementById("cfg-raw").value); }
      catch (e) { return showApply({ status: "refused", problems: ["invalid JSON: " + e.message] }); }
    }
    const dupes = duplicateIds(cfg);
    if (dupes.length) {
      return showApply({ status: "refused", problems: [
        "duplicate id " + dupes.join(", ") + " - give each its own id"] });
    }
    postApply({ config: JSON.parse(JSON.stringify(cfg)) });
  });
}

// ---- Config: Apply (POST /api/apply) ----------------------------
// refused: shown, nothing written. preview: the plan, with Confirm and
// Cancel; Confirm posts the same config back with the preview's token, Cancel
// sends nothing. applied: saved and in force (and deployed, if Home Assistant
// changed). error: the reload failed; nothing changed.

function setCfgMsg(text, kind) {
  const msg = document.getElementById("cfg-msg");
  if (!msg) return;
  msg.textContent = text;
  msg.className = "cfg-msg" + (kind ? " " + kind : "");
}

async function postApply(body) {
  const btn = document.getElementById("cfg-apply");
  btn.disabled = true;
  document.querySelectorAll("#cfg-apply-panel button").forEach((b) => { b.disabled = true; });
  setCfgMsg(body.confirm ? "applying and deploying\u2026" : "checking\u2026");
  let j;
  try {
    const r = await fetch("/api/apply", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    j = await r.json().catch(() => ({}));
    if (!r.ok || !j.status) j = { status: "error", error: j.detail || `request failed (${r.status})` };
  } catch (e) {
    j = { status: "error", error: "could not reach the container: " + e.message };
  }
  btn.disabled = false;
  showApply(j, body.config);
  if (j.status === "applied") {
    // show what was saved: the version 2 config, as the server holds it now
    try { cfg = await (await fetch("/api/config")).json(); } catch { /* keep the local copy */ }
    renderConfigBody();
  }
}

function showApply(j, config) {
  const panel = document.getElementById("cfg-apply-panel");
  pendingApply = j.status === "preview" ? { config, token: j.token } : null;
  if (j.status === "preview") {
    setCfgMsg("Review the Home Assistant changes below", "warn");
    panel.innerHTML = previewHtml(j);
  } else if (j.status === "applied") {
    setCfgMsg(j.message, j.deploy && !j.deploy.ok ? "warn" : "ok");
    panel.innerHTML = appliedHtml(j);
  } else if (j.status === "refused") {
    setCfgMsg("Not applied", "err");
    panel.innerHTML = refusedHtml(j);
  } else {
    setCfgMsg("Not applied", "err");
    panel.innerHTML = `<section class="apply-panel err" data-apply="error">
      <h3>Not applied</h3><p>${escapeHtml(String(j.error || "unknown error"))}</p>
      <p class="muted">${escapeHtml(j.message || "Nothing was changed.")}</p></section>`;
  }
}

const PLAN_GROUPS = [
  ["helpers_created", "Helpers created"], ["helpers_updated", "Helpers updated"],
  ["helpers_removed", "Helpers removed"], ["automations_written", "Automations written"],
  ["automations_removed", "Automations removed"],
];

function planHtml(plan) {
  const groups = PLAN_GROUPS.filter(([k]) => (plan[k] || []).length).map(([k, label]) => `
    <div class="plan-group" data-group="${k}">
      <h4>${label} <span class="count">${plan[k].length}</span></h4>
      <ul>${plan[k].map((x) => `<li><code>${escapeHtml(x)}</code></li>`).join("")}</ul>
    </div>`).join("");
  return `<div class="plan-groups">${groups || '<p class="muted">Nothing changes in Home Assistant.</p>'}</div>
    <p class="muted">Unchanged: ${plan.helpers_reused ?? 0} helpers, ${plan.automations_unchanged ?? 0} automations.</p>`;
}

function listHtml(title, items, cls) {
  if (!items || !items.length) return "";
  return `<div class="apply-list ${cls || ""}"><h4>${title}</h4>
    <ul>${items.map((x) => `<li>${escapeHtml(String(x))}</li>`).join("")}</ul></div>`;
}

function previewHtml(j) {
  const plan = j.plan || {};
  return `<section class="apply-panel preview" data-apply="preview" aria-label="Apply preview">
    <h3>Apply would change Home Assistant</h3>
    <p class="muted">Nothing has been saved yet. <b>Confirm</b> saves this config, puts it in force
      and deploys these changes; <b>Cancel</b> changes nothing.</p>
    ${j.stale ? '<p class="warn">Home Assistant changed since the last preview; this is the plan now.</p>' : ""}
    ${planHtml(plan)}
    ${listHtml("Problems", plan.problems, "err") || '<p class="muted">Problems: none.</p>'}
    ${listHtml("Notes", plan.notes, "warn")}
    <div class="apply-actions">
      <button class="primary" data-action="apply-confirm">Confirm and deploy</button>
      <button data-action="apply-cancel">Cancel</button>
    </div>
  </section>`;
}

function appliedHtml(j) {
  const rl = j.reload || {};
  const changed = rl.changed ? (rl.changed.length ? rl.changed.join(", ") : "nothing") : null;
  const crossed = (rl.crossovers || []).map((c) => `${c.room} \u2192 ${c.section}`);
  const dep = j.deploy;
  return `<section class="apply-panel ${dep && !dep.ok ? "warn" : "ok"}" data-apply="applied">
    <h3>${escapeHtml(j.message)}</h3>
    <p class="muted">Saved as config version ${j.version}${changed !== null ? `; in force now (changed: ${escapeHtml(changed)})` : ""}${
      rl.runtime === "started" ? "; the runtime started" : ""}.
      ${j.backup ? `The previous config is in <code>backups/${escapeHtml(j.backup)}</code>.` : ""}</p>
    ${crossed.length ? `<p class="muted">Section change on the new schedule: ${escapeHtml(crossed.join(", "))}.</p>` : ""}
    ${dep ? `<h4 class="sub">Deployed</h4>${planHtml({ ...dep, helpers_reused: dep.helpers_reused.length,
                                                     automations_unchanged: dep.automations_unchanged.length })}
      ${listHtml("Problems", dep.problems, "err")}${listHtml("Notes", dep.notes, "muted")}` : ""}
  </section>`;
}

function refusedHtml(j) {
  const plan = j.plan || {};
  return `<section class="apply-panel err" data-apply="refused">
    <h3>Not applied: fix these first</h3>
    ${listHtml("Problems", j.problems, "err")}
    ${listHtml("Not found in Home Assistant", plan.missing_entities, "err")}
    <p class="muted">Nothing was saved or deployed.</p>
  </section>`;
}

// ---- Config: delegated input/change/click (survives body re-renders) ---

// An edit while a preview is up: the preview no longer matches the form.
function previewOutdated() {
  if (!pendingApply) return;
  pendingApply = null;
  document.getElementById("cfg-apply-panel").innerHTML = "";
  setCfgMsg("Changed since the preview: Apply again to see the changes", "warn");
}

function wireConfigDelegation() {
  view.addEventListener("input", (e) => {
    const t = e.target;
    if (t.id === "cfg-raw") { previewOutdated(); return; }
    if (!t.dataset || !t.dataset.path || t.dataset.live !== "1") return;
    applyFieldValue(t);
    previewOutdated();
    if (t.dataset.checkEntity) updateEntityCheck(t);
    if (t.dataset.vtype === "negnumber") updateMarginReadout(t);
  });
  view.addEventListener("change", (e) => {
    const t = e.target;
    if (!t.dataset) return;
    if (t.dataset.sensorType === "1") {
      handleSensorTypeChange(t);
      previewOutdated();
      renderConfigBody();
      return;
    }
    if (!t.dataset.path) return;
    applyFieldValue(t);
    previewOutdated();
    if (t.dataset.checkEntity) updateEntityCheck(t);
  });
  view.addEventListener("click", (e) => {
    const b = e.target.closest("[data-action]");
    if (!b || !view.contains(b)) return;
    const fn = cfgActions[b.dataset.action];
    if (fn && b.tagName === "A") e.preventDefault();
    if (fn) fn(b.dataset);
  });
}

function applyFieldValue(t) {
  let v;
  if (t.type === "checkbox") v = t.checked;
  else v = t.value;
  if (t.dataset.vtype === "number") v = v === "" ? undefined : parseFloat(v);
  else if (t.dataset.vtype === "negnumber") v = v === "" ? undefined : -parseFloat(v) + 0;
  else if (t.dataset.vtype === "nullable") v = v === "" ? null : v;
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

function handleSensorTypeChange(t) {
  const room = cfg.rooms.find((r) => r.id === t.dataset.room);
  if (!room) return;
  const sensor = room.sensors[Number(t.dataset.idx)];
  if (!sensor) return;
  if (t.value === "virtual") {
    if (!room.units.length) return;   // <option disabled> guards this too
    delete sensor.entity_id;
    sensor.source = { type: "unit_attribute",
                      unit_id: sensor.source?.unit_id || room.units[0].id,
                      attribute: "current_temperature" };
  } else {
    delete sensor.source;
    if (sensor.entity_id === undefined) sensor.entity_id = "sensor.";
  }
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
  "apply-confirm": () => {
    if (!pendingApply) return;
    postApply({ config: pendingApply.config, confirm: true, token: pendingApply.token });
  },
  "apply-cancel": () => {
    pendingApply = null;
    document.getElementById("cfg-apply-panel").innerHTML = "";
    setCfgMsg("Cancelled: nothing was saved or deployed");
  },
  "reset-default": (d) => {
    const node = schemaAt(d.path);
    if (!node || !("default" in node)) return;
    setPath(cfg, d.path, JSON.parse(JSON.stringify(node.default)));
    previewOutdated();
    renderConfigBody();
  },
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
    if (!confirm(`Remove room "${d.room}" from config? (Apply afterward to remove it from Home Assistant too.)`)) return;
    cfg.rooms = cfg.rooms.filter((r) => r.id !== d.room);
    renderConfigBody();
  },
  "add-unit": (d) => {
    const room = cfg.rooms.find((r) => r.id === d.room);
    const typed = slugify(prompt("Unit id:", "")); if (!typed) return;
    const id = uniqueId(typed, room.units.map((u) => u.id));
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
    // generated, unique within the room; editable in the id column
    const id = uniqueId(`${room.id}_sensor`, room.sensors.map((s) => s.id));
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

// ---- Config: settings (structured, with the schema's defaults) ----

const fmtNum = (x) => (typeof x === "number" ? String(+x.toFixed(3)).replace("-", "\u2212") : String(x));

function rangeText(node, negate) {
  let lo = node.minimum ?? node.exclusiveMinimum, hi = node.maximum;
  if (negate) [lo, hi] = [hi === undefined ? undefined : -hi, lo === undefined ? undefined : -lo];
  if (lo === undefined && hi === undefined) return "";
  const above = !negate && node.exclusiveMinimum !== undefined ? " (above)" : "";
  if (hi === undefined) return `${fmtNum(lo)}${above} or more`;
  if (lo === undefined) return `up to ${fmtNum(hi)}`;
  return `${fmtNum(lo)}${above} to ${fmtNum(hi)}`;
}

// The line under a field: the schema's default, its range, and a reset link.
function defaultLine(path, opts = {}) {
  const node = schemaAt(path);
  if (!node || !("default" in node)) return "";
  const def = opts.negate ? -node.default : node.default;
  const shown = opts.label ? opts.label(node.default) : fmtNum(def === null ? "none" : def);
  const range = node.type === "number" || node.type === "integer" ? rangeText(node, opts.negate) : "";
  return `<span class="setting-default">default ${escapeHtml(shown)}${range ? ` \u00b7 ${range}` : ""}
    \u00b7 <a href="#" data-action="reset-default" data-path="${path}">reset to default</a></span>`;
}

function numInput(path, val, node, opts = {}) {
  const neg = !!opts.negate;
  const shown = val === undefined || val === null ? "" : neg ? -val + 0 : val;
  let lo = node?.minimum ?? node?.exclusiveMinimum, hi = node?.maximum;
  if (neg) [lo, hi] = [hi === undefined ? undefined : -hi, lo === undefined ? undefined : -lo];
  const step = opts.step || (node?.type === "integer" ? "1" : "0.1");
  return `<input type="number" id="f-${path.replace(/\./g, "-")}" step="${step}"
    ${lo !== undefined ? `min="${lo}"` : ""} ${hi !== undefined ? `max="${hi}"` : ""}
    data-path="${path}" data-vtype="${neg ? "negnumber" : "number"}" data-live="1" value="${shown}" />`;
}

// One setting: label, input (and unit), default + range + reset, one line of help.
function setting(path, label, help, opts = {}) {
  const node = schemaAt(path) || {};
  const val = getPath(cfg, path, node.default);
  const id = `f-${path.replace(/\./g, "-")}`;
  const control = opts.control ? opts.control(val, node)
    : `${numInput(path, val, node, opts)}${opts.unit ? `<span class="unit">${opts.unit}</span>` : ""}`;
  return `<div class="setting${opts.wide ? " wide" : ""}" data-setting="${path}">
    <label for="${id}">${label}</label>
    <div class="setting-input">${control}</div>
    ${defaultLine(path, opts)}
    <p class="help">${help}</p>
  </div>`;
}

function selectControl(path, choices) {
  return (val) => `<select id="f-${path.replace(/\./g, "-")}" data-path="${path}" data-live="1">
    ${choices.map(([v, text, disabled]) => `<option value="${v}" ${v === val ? "selected" : ""}
      ${disabled ? "disabled" : ""}>${escapeHtml(text)}</option>`).join("")}</select>`;
}

// The weather entities Home Assistant has, each with its temperature now and
// whether it offers an hourly forecast; one without is listed but disabled.
function weatherControl(val) {
  const list = entityLists.weather || [];
  const opts = list.map((e) => {
    const temp = e.temperature === null || e.temperature === undefined ? "no temperature"
      : `${fmtNum(e.temperature)}${e.temperature_unit || "\u00b0"}`;
    const why = e.hourly ? "hourly forecast" : "no hourly forecast";
    return `<option value="${escapeHtml(e.entity_id)}" ${e.entity_id === val ? "selected" : ""}
      ${e.hourly ? "" : "disabled"} data-hourly="${e.hourly ? 1 : 0}">${escapeHtml(e.name)} (${escapeHtml(e.entity_id)}) \u2014 ${temp}, ${why}</option>`;
  });
  if (val && !list.some((e) => e.entity_id === val)) {
    opts.unshift(`<option value="${escapeHtml(val)}" selected>${escapeHtml(val)} \u2014 ${
      entityLists.connected ? "not found in Home Assistant" : "can't check: Home Assistant not reachable"}</option>`);
  }
  opts.unshift(`<option value="" ${val ? "" : "selected"}>\u2014 none \u2014</option>`);
  return `<select id="f-outdoor-weather_entity" data-path="outdoor.weather_entity" data-live="1"
    data-vtype="nullable">${opts.join("")}</select>`;
}

// "Stop cooling when the section's warmest hour is at or below comfort + X°",
// X = -margin: the margin is entered the way it reads.
function marginControl(val, node) {
  return `<span class="sentence">Stop cooling when the section's warmest hour is at or below
    comfort + ${numInput("outdoor.initial_margin", val, node, { negate: true, step: "0.5" })}\u00b0
    <span class="readout" data-readout="margin">${marginReadout(val)}</span></span>`;
}

function marginReadout(margin) {
  if (margin === undefined || margin === null || Number.isNaN(margin)) return "";
  const x = -margin + 0;
  return `(margin ${fmtNum(margin)}: comfort ${x < 0 ? "\u2212" : "+"} ${fmtNum(Math.abs(x))}\u00b0 or below)`;
}

function updateMarginReadout(t) {
  const out = t.closest(".setting")?.querySelector('[data-readout="margin"]');
  if (out) out.textContent = marginReadout(getPath(cfg, "outdoor.initial_margin"));
}

function outdoorHtml() {
  const out = cfg.outdoor || (cfg.outdoor = {});
  const note = entityLists.connected ? "" : `<p class="muted">Home Assistant isn't reachable, so its
    weather entities can't be listed: ${escapeHtml(entityLists.note || "")}</p>`;
  return `<details class="panel settings" open data-panel="outdoor"><summary>Outdoor</summary>
    ${note}
    <div class="settings-grid">
      ${setting("outdoor.source", "Source",
        "Where the outdoor temperature comes from: a weather entity's hourly forecast, or none (no lean, no block).",
        { control: selectControl("outdoor.source", [["forecast", "forecast \u2014 a weather entity's hourly forecast"],
          ["none", "none \u2014 no outdoor input"], ["probe", "probe \u2014 not supported yet", true]]) })}
      ${setting("outdoor.weather_entity", "Weather entity",
        "Its hourly forecast is read at each section change; needed when the source is forecast. One without an hourly forecast can't be picked.",
        { control: weatherControl, label: () => "none", wide: true })}
      ${setting("outdoor.block_mode", "Cooling block",
        "Whether a cool forecast blocks cooling for a section. Takes effect from each room's next section.",
        { control: selectControl("outdoor.block_mode", [["off", "off \u2014 never block"],
          ["shadow", "shadow \u2014 decide and log, don't apply"], ["active", "active \u2014 block cooling"]]) })}
      ${setting("outdoor.initial_margin", "Block margin",
        "Where each room's margin starts; it then learns from what people do. Takes effect from each room's next section.",
        { control: marginControl, negate: true, wide: true,
          label: (m) => `comfort + ${fmtNum(-m)}\u00b0 (margin ${fmtNum(m)})` })}
      ${setting("outdoor.lean_per_degree", "Lean per degree",
        "How far the trigger edges lean, in band widths per \u00b0C between the room's comfort point and outdoors.",
        { step: "0.01", unit: "band / \u00b0C" })}
    </div>
  </details>`;
}

function controlHtml() {
  return `<details class="panel settings" open data-panel="control"><summary>System and control</summary>
    <div class="settings-grid">
      ${setting("system.sensor_stale_minutes", "Sensor stale after",
        "A sensor Home Assistant hasn't reported for this long counts as unavailable: no vote, no learning.",
        { unit: "min", step: "5" })}
      ${setting("system.control.person_step_degrees", "Vote step",
        "A too cold or too hot vote moves each sensor's comfort reading this far from its reading.",
        { unit: "\u00b0", step: "0.5" })}
      ${setting("system.control.boost_degrees", "Boost step",
        "Boost cools this many degrees below the unit's base and offset.", { unit: "\u00b0", step: "0.5" })}
      ${setting("system.control.boost_max_minutes", "Boost cap",
        "After this long in boost a unit goes back to cool, a step cooler.", { unit: "min", step: "5" })}
      ${setting("system.control.cool_min_heartbeats", "Minimum cool time",
        "The least time in cool or boost before the next change, to spare the compressor.",
        { unit: "heartbeats" })}
      ${setting("system.control.rest_min_heartbeats", "Minimum rest time",
        "The least time resting (off) before a unit wakes.", { unit: "heartbeats" })}
      ${setting("learning.reaction_rate", "Learning rate",
        "How far each reaction moves the learned setpoint and comfort readings toward what the person chose.",
        { step: "0.05" })}
    </div>
  </details>`;
}

function settingsHtml() {
  const sys = cfg.system || (cfg.system = {});
  const trust = sys.trust || (sys.trust = {});
  sys.control || (sys.control = {});
  const ha = cfg.homeassistant || (cfg.homeassistant = {});
  return outdoorHtml() + controlHtml() + `<details class="panel"><summary>Connection, observation &amp; trust</summary>
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
      <label>High-trust deviation (\u00b0) ${numField("system.trust.high_trust_deviation", trust.high_trust_deviation ?? 0.5, {step:"0.1"})}</label>
      <label>Low-trust deviation (\u00b0) ${numField("system.trust.low_trust_deviation", trust.low_trust_deviation ?? 5.0, {step:"0.5"})}</label>
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
  const rows = room.sensors.map((s, j) => {
    const isVirtual = !!(s.source && s.source.type === "unit_attribute");
    const sourceCell = isVirtual
      ? virtualSourceField(room, i, j, s.source.unit_id)
      : entityField(`rooms.${i}.sensors.${j}.entity_id`, s.entity_id, "sensor");
    return `<tr>
      <td>${textField(`rooms.${i}.sensors.${j}.id`, s.id)}</td>
      <td>${textField(`rooms.${i}.sensors.${j}.name`, s.name)}</td>
      <td>
        <select data-sensor-type="1" data-room="${room.id}" data-idx="${j}">
          <option value="physical" ${isVirtual ? "" : "selected"}>Physical</option>
          <option value="virtual" ${isVirtual ? "selected" : ""}
            ${room.units.length === 0 ? "disabled" : ""}>AC internal</option>
        </select>
      </td>
      <td>${sourceCell}</td>
      <td><button class="danger-outline" data-action="remove-sensor" data-room="${room.id}" data-idx="${j}">remove</button></td>
    </tr>`;
  }).join("");
  return `<h3>Temperature sensors <button class="small" data-action="add-sensor" data-room="${room.id}">+ add</button></h3>
    <table class="now"><tr><th>id</th><th>name</th><th>type</th><th>reading source</th><th></th></tr>
    ${rows || `<tr class="empty"><td colspan="5">no sensors yet</td></tr>`}</table>`;
}

function virtualSourceField(room, i, j, currentUnitId) {
  const opts = room.units.map((u) =>
    `<option value="${u.id}" ${u.id === currentUnitId ? "selected" : ""}>${escapeHtml(u.name || u.id)} (${escapeHtml(u.entity_id || "")})</option>`
  ).join("");
  if (!opts) return `<span class="muted">add a unit first</span>`;
  return `<select data-path="rooms.${i}.sensors.${j}.source.unit_id" data-live="1">${opts}</select>`;
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
  return `<h3>Scenes <span class="muted">(Apply previews the Home Assistant change)</span></h3>
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
  "config", "validation", "control", "block", "outdoor", "storage"];
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

renderNow();                    // the Now tab is the one open at load
