// Adaptive Climate - the Now view (docs/V2_DESIGN.md, UI; phase 6).
//
// Draws what GET /api/now returns. Every rule (staleness, a sensor's place
// in its band, the quorum, the conditions in force, the block, what a unit
// is driven to) is computed by app/nowview.py from the same app.control the
// Home Assistant templates are rendered from; this file only lays it out:
//
//   donut       fixed arcs, blue (rest) / green (comfortable) / amber (cool),
//               one numbered pointer per sensor outside the ring, the fan in
//               the centre and the state in the label strip at the bottom
//   scale       the applied setpoint, the section offset as chevrons, what
//               the unit is driven to now, and the forecast (magenta)
//   table       the sensors, warmest first, numbered as on the donut
//   buttons     Too cold / Too hot (the room's vote buttons in HA), and
//               Leak fixed while a leak is latched
//
// Polls every 15 s while the Now tab is open.

const NowView = (() => {
  const POLL_MS = 15000;
  // The gauge, in degrees clockwise from 12 o'clock. The arcs are fixed:
  // the leaned edges land on +-EDGE, and a marker pins at +-SWEEP, PAST band
  // widths beyond its edge. The bottom (SWEEP..360-SWEEP) is the label strip.
  const SWEEP = 135, EDGE = 45, PAST = 2;
  const R = 86, ORBIT = 108, LEVEL = 17;     // ring radius; marker orbits (up to 3)
  const SCALE_RANGE = 5, SCALE_PX = 21;     // correction scale: +-5 degC

  let timer = null;
  let data = null;
  const messages = {};          // room -> {text, kind, until}
  const pending = new Set();    // rooms with a vote in flight
  const confirming = new Set(); // "room/unit" asking to confirm a leak fix
  let wired = false;

  // ---- helpers ---------------------------------------------------

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const deg = (x, d = 1) => (x === null || x === undefined ? "—" : `${Number(x).toFixed(d)}°`);
  const signed = (x, d = 1) => `${x > 0 ? "+" : x < 0 ? "−" : "±"}${Math.abs(x).toFixed(d)}`;

  function polar(r, a) {
    const t = (a * Math.PI) / 180;
    return [r * Math.sin(t), -r * Math.cos(t)];
  }

  function arc(r, a0, a1) {
    const [x0, y0] = polar(r, a0), [x1, y1] = polar(r, a1);
    const large = Math.abs(a1 - a0) > 180 ? 1 : 0;
    return `M${x0.toFixed(2)},${y0.toFixed(2)} A${r},${r} 0 ${large} 1 ${x1.toFixed(2)},${y1.toFixed(2)}`;
  }

  // A sensor's place on the gauge from its position in band widths:
  // piecewise, so comfort is at the top and the leaned cold and warm edges
  // land exactly on the arc boundaries; past an edge, PAST band widths take
  // it to the end of the scale, where it pins with an overflow tick.
  function markerAngle(p, cold, warm) {
    if (!(warm > 0)) warm = 1;
    if (!(cold < 0)) cold = -1;
    if (p >= warm) {
      const t = (p - warm) / PAST;
      return { angle: EDGE + (SWEEP - EDGE) * Math.min(t, 1), overflow: t > 1 };
    }
    if (p <= cold) {
      const t = (cold - p) / PAST;
      return { angle: -EDGE - (SWEEP - EDGE) * Math.min(t, 1), overflow: t > 1 };
    }
    return { angle: p >= 0 ? (EDGE * p) / warm : (-EDGE * p) / cold, overflow: false };
  }

  // What the label strip says: [state, qualifier]. Wording from
  // docs/V2_DESIGN.md (UI), plus the forced votes.
  function stateLabel(unit, room) {
    if (!unit) return ["No unit", ""];
    if (!unit.available) return ["Unavailable", unit.hvac_mode || "not reporting"];
    const c = unit.control;
    let label;
    if (!c) label = ["Not controlled", "no state helper yet"];
    else {
      switch (c.state) {
        case "cool": label = ["Cool · Low", c.in_grace ? "you turned it on" : ""]; break;
        case "boost": label = ["Boost · Medium", c.forced ? "you said too hot" : ""]; break;
        case "rest": label = ["Resting", c.forced ? "you said too cold" : ""]; break;
        case "user_off": label = ["Off (by you)", ""]; break;
        case "manual": label = ["Manual",
          [unit.hvac_mode, unit.fan_mode].filter(Boolean).join(" · ")]; break;
        case "config_off": label = ["Off (scheduled)", ""]; break;
        case "leak": label = ["Leak · Off", "until it's fixed"]; break;
        default: label = [c.state, ""];
      }
    }
    if (room && room.hold && !label[1]) label[1] = "paused (hold)";
    return label;
  }

  function lockState(room) {
    const b = room.block;
    if (b.blocked && b.by === "heat") return { text: "heater on", lines: ["heater", "on"], dashed: false };
    if (b.blocked) return { text: "blocked", lines: ["blocked"], dashed: false };
    if (b.would_block) return { text: "would block", lines: ["would", "block"], dashed: true };
    return null;
  }

  // The lock's words sit inside the ring, where "cool" would be.
  function lockLabel(lock) {
    const y0 = lock.lines.length === 1 ? 4 : -2;
    return `<text class="lock-label${lock.dashed ? " dashed" : ""}" x="50" y="${y0}">${
      lock.lines.map((w, i) => `<tspan x="50" y="${y0 + i * 11}">${esc(w)}</tspan>`).join("")}</text>`;
  }

  // ---- the donut -------------------------------------------------

  function lockIcon(x, y, dashed) {
    const dash = dashed ? ' stroke-dasharray="2.2 1.6"' : "";
    return `<g class="lock${dashed ? " dashed" : ""}" transform="translate(${x},${y})">
      <circle r="13" class="lock-bg"/>
      <path d="M-4.5,-1.5 v-3 a4.5,4.5 0 0 1 9,0 v3" class="lock-line"${dash}/>
      <rect x="-6.5" y="-1.5" width="13" height="9.5" rx="1.8" class="lock-body"${dash}/>
    </g>`;
  }

  function fanIcon(speed) {
    const blade = "M0,-3 C7,-6 9,-17 2,-21 C-3,-18 -4,-9 0,-3 Z";
    return `<g class="fan ${speed}" data-speed="${speed}">
      <g class="blades">
        <path d="${blade}"/><path d="${blade}" transform="rotate(120)"/>
        <path d="${blade}" transform="rotate(240)"/>
      </g>
      <circle r="3.6" class="hub"/>
    </g>`;
  }

  function markers(room) {
    const cold = room.conditions.cold_edge, warm = room.conditions.warm_edge;
    const placed = [], hollow = [];
    room.sensorsSorted.forEach((s) => {
      if (s.position === null || s.position === undefined) hollow.push(s);
      else placed.push({ s, ...markerAngle(s.position, cold, warm) });
    });
    // Close markers step outward to a second or third orbit.
    placed.sort((a, b) => a.angle - b.angle);
    const lastAt = [];
    placed.forEach((m) => {
      let level = 0;
      while (level < 2 && lastAt[level] !== undefined && Math.abs(m.angle - lastAt[level]) < 15) level++;
      lastAt[level] = m.angle;
      m.level = level;
    });
    const out = placed.map(({ s, angle, overflow, level }) => {
      const orbit = ORBIT + level * LEVEL;
      const [x, y] = polar(orbit, angle);
      const [tx, ty] = polar(R + 12, angle);
      const [b1x, b1y] = polar(orbit - 9, angle - 3.2), [b2x, b2y] = polar(orbit - 9, angle + 3.2);
      let tick = "";
      if (overflow) {
        // A double chevron just past the pinned marker, pointing on round
        // the scale: the sensor is further out than the gauge shows.
        const dir = angle > 0 ? 1 : -1;
        const chev = (a) => {
          const [px, py] = polar(orbit + 5, a), [tx2, ty2] = polar(orbit, a + dir * 4.5);
          const [qx, qy] = polar(orbit - 5, a);
          return `M${px.toFixed(1)},${py.toFixed(1)} L${tx2.toFixed(1)},${ty2.toFixed(1)} L${qx.toFixed(1)},${qy.toFixed(1)}`;
        };
        tick = `<path class="overflow-tick" d="${chev(angle + dir * 8)} ${chev(angle + dir * 13)}"/>`;
      }
      return `<g class="marker zone-${s.zone}" data-sensor="${esc(s.id)}" data-n="${s.n}"
          data-angle="${angle.toFixed(2)}" data-overflow="${overflow ? 1 : 0}">
        <title>${s.n}. ${esc(s.name)}: ${deg(s.reading)} (${signed(s.position, 2)} band)</title>
        <path class="pointer" d="M${tx.toFixed(1)},${ty.toFixed(1)} L${b1x.toFixed(1)},${b1y.toFixed(1)} L${b2x.toFixed(1)},${b2y.toFixed(1)} Z"/>
        <circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="9.5"/>
        <text x="${x.toFixed(1)}" y="${(y + 3.6).toFixed(1)}">${s.n}</text>${tick}
      </g>`;
    }).join("");
    const gap = 22, x0 = -((hollow.length - 1) * gap) / 2;
    const hol = hollow.map((s, i) => `<g class="marker hollow" data-sensor="${esc(s.id)}" data-n="${s.n}">
        <title>${s.n}. ${esc(s.name)}: ${s.stale ? `stale since ${esc(s.stale_since)}` : esc(s.status)}</title>
        <circle cx="${x0 + i * gap}" cy="124" r="8.5"/>
        <text x="${x0 + i * gap}" y="127.5">${s.n}</text></g>`).join("");
    return out + hol;
  }

  function donut(room) {
    const unit = room.units[0];
    const [label, sub] = stateLabel(unit, room);
    const lock = lockState(room);
    const greyWarm = room.block.blocked;
    const g = 1.2;     // gap between arcs, degrees
    const speed = unit ? unit.fan_speed : "stopped";
    const [lx, ly] = polar(R, 90);
    return `<svg class="donut" viewBox="-152 -152 304 304" role="img"
        aria-label="${esc(room.name)}: ${esc(label)}${sub ? ", " + esc(sub) : ""}${lock ? ", " + lock.text : ""}">
      <path class="arc cold" d="${arc(R, -SWEEP, -EDGE - g)}"/>
      <path class="arc ok" d="${arc(R, -EDGE + g, EDGE - g)}"/>
      <path class="arc warm${greyWarm ? " off" : ""}" d="${arc(R, EDGE + g, SWEEP)}"/>
      <path class="arc strip" d="${arc(R, SWEEP + g, 360 - SWEEP - g)}"/>
      <text class="zone-label" x="-55" y="4">rest</text>
      ${lock ? "" : `<text class="zone-label" x="55" y="4">cool</text>`}
      <line class="top-tick" x1="0" y1="${-R - 12}" x2="0" y2="${-R + 12}"/>
      <text class="comfort-label" y="-52">comfort</text>
      <text class="comfort-value" y="-36">${deg(room.comfort_point)}</text>
      <g transform="translate(0,8)">${fanIcon(speed)}</g>
      <text class="unit-temp" y="47">${unit && unit.current_temperature !== null
        ? `unit reads ${deg(unit.current_temperature)}` : ""}</text>
      ${lock ? lockIcon(lx, ly, lock.dashed) + lockLabel(lock) : ""}
      <g class="strip-label">
        <rect x="-58" y="${R - 17}" width="116" height="${sub ? 38 : 28}" rx="${sub ? 12 : 14}"/>
        <text class="state-main" y="${R + 1}">${esc(label)}</text>
        ${sub ? `<text class="state-sub" y="${R + 14}">${esc(sub)}</text>` : ""}
      </g>
      ${markers(room)}
    </svg>`;
  }

  // ---- the correction scale -------------------------------------

  function correctionScale(room) {
    const unit = room.units[0];
    const c = unit && unit.control;
    const cy = 128, x = 34;
    const y = (d) => cy - Math.max(-SCALE_RANGE, Math.min(SCALE_RANGE, d)) * SCALE_PX;
    const ticks = [];
    for (let d = -SCALE_RANGE; d <= SCALE_RANGE; d++) {
      ticks.push(`<line class="tick${d === 0 ? " zero" : ""}" x1="${x - (d % 5 ? 4 : 7)}" x2="${x + (d % 5 ? 4 : 7)}" y1="${y(d)}" y2="${y(d)}"/>`);
    }
    const offset = c ? c.offset : 0;
    const chevrons = [];
    for (let k = 1; k <= Math.min(Math.abs(offset), 3); k++) {
      const up = offset > 0, yy = up ? y(k) + 6 : y(-k) - 6;
      chevrons.push(`<path class="chevron" d="M${x - 9},${yy + (up ? 4 : -4)} L${x},${yy + (up ? -4 : 4)} L${x + 9},${yy + (up ? 4 : -4)}"/>`);
    }
    const applied = unit ? unit.setpoint_applied : null;
    const cond = room.conditions;
    let forecast = "";
    if (cond.live && cond.outdoor_mean !== null && room.comfort_point !== null) {
      const d = cond.outdoor_mean - room.comfort_point;
      const yy = y(d), over = Math.abs(d) > SCALE_RANGE;
      // Past the end of the scale the marker turns into an arrow pointing
      // on, with its label on the scale's side of it.
      const shape = !over ? `M${x + 11},${yy} l9,-6 v12 Z`
        : d > 0 ? `M${x + 19},${yy - 7} l6,10 h-12 Z` : `M${x + 19},${yy + 7} l6,-10 h-12 Z`;
      const ly = over && d < 0 ? yy - 14 : over ? yy + 14 : yy - 1;
      forecast = `<g class="forecast${over ? " pinned" : ""}" data-delta="${d.toFixed(2)}">
        <title>Forecast for the section: mean ${deg(cond.outdoor_mean)}, max ${deg(cond.outdoor_max)}; comfort point ${deg(room.comfort_point)}</title>
        <path d="${shape}"/>
        <text x="${x + 25}" y="${ly}">out ${deg(cond.outdoor_mean)}</text>
        <text class="small" x="${x + 25}" y="${ly + 11}">max ${deg(cond.outdoor_max)}</text>
      </g>`;
    }
    const driving = unit && unit.driving !== null && unit.driving !== undefined
      ? `driving ${deg(unit.driving, 0)}` : unit && unit.control ? "not cooling" : "";
    return `<svg class="scale" viewBox="0 0 120 270" role="img"
        aria-label="Applied setpoint ${deg(applied, 0)}, offset ${offset}, ${driving}">
      <text class="scale-cap" x="${x}" y="10">warmer</text>
      <line class="axis" x1="${x}" x2="${x}" y1="${y(SCALE_RANGE)}" y2="${y(-SCALE_RANGE)}"/>
      ${ticks.join("")}
      ${chevrons.join("")}
      <g class="setpoint">
        <rect x="${x - 26}" y="${cy - 14}" width="52" height="28" rx="7"/>
        <text x="${x}" y="${cy + 6}">${applied === null || applied === undefined ? "—" : deg(applied, 0)}</text>
      </g>
      ${offset ? `<text class="offset" x="${x}" y="${offset > 0 ? y(Math.min(offset, 3)) - 14 : y(-Math.min(-offset, 3)) + 22}">offset ${offset > 0 ? "+" : "−"}${Math.abs(offset)}</text>` : ""}
      ${forecast}
      <text class="scale-cap" x="${x}" y="246">cooler</text>
      <text class="driving" x="${x}" y="264">${esc(driving)}</text>
    </svg>`;
  }

  // ---- the sensor table ------------------------------------------

  const ZONE = { warm: ["▲", "past the warm edge: votes too warm"],
                 ok: ["●", "comfortable"],
                 cold: ["▼", "past the cold edge: votes too cold"],
                 none: ["○", "no reading"] };

  function quorumLine(room) {
    const v = room.votes;
    const votes = `${v.tally} vote${v.tally === 1 ? "" : "s"} (${v.required} needed)`;
    if (!v.required) return "No usable sensor: no quorum";
    if (v.met && v.direction === "too_warm") return `Quorum met, too warm: ${votes}`;
    if (v.met && v.direction === "too_cold") return `Quorum met, too cold: ${votes}`;
    if (v.met) return `Quorum met, but the sides tie: no action: ${votes}`;
    return `No quorum: ${votes}`;
  }

  function table(room) {
    const rows = room.sensorsSorted.map((s) => {
      const [glyph, what] = ZONE[s.zone] || ZONE.none;
      const trust = Math.round((s.trust || 0) * 100);
      const state = s.stale ? `stale since ${esc(s.stale_since)}`
        : s.status === "unavailable" ? "unavailable"
        : s.status === "unlearned" ? "no comfort reading yet" : "";
      return `<tr class="zone-${s.zone}${s.stale || s.status === "unavailable" ? " dim" : ""}" data-sensor="${esc(s.id)}">
        <td class="n">${s.n}</td>
        <td class="zone" title="${what}"><span class="bar"></span><span class="glyph" aria-label="${what}">${glyph}</span></td>
        <td class="reading">${deg(s.reading)}</td>
        <td class="name">${esc(s.name)}${state ? `<span class="note">${state}</span>` : ""}</td>
        <td class="band">${s.band ? `±${Number(s.band).toFixed(1)}°` : "—"}</td>
        <td class="trust-cell" title="trust ${trust}%"><span class="trustbar" role="img" aria-label="trust ${trust}%"><span style="width:${trust}%"></span></span></td>
      </tr>`;
    }).join("");
    return `<div class="sensors">
      <p class="quorum">${esc(quorumLine(room))}</p>
      <table class="sensor-table">
        <thead><tr><th>#</th><th><span class="sr">zone</span></th><th>Reading</th><th>Sensor</th><th>Band</th><th>Trust</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="6" class="empty">no sensors</td></tr>`}</tbody>
      </table></div>`;
  }

  // ---- a room card -----------------------------------------------

  function prepare(room) {
    // Warmest first by place in band; no position (stale, unavailable,
    // unlearned) last. Each sensor's number is its row, as on the donut.
    const sorted = [...room.sensors].sort((a, b) => {
      const pa = a.position ?? -Infinity, pb = b.position ?? -Infinity;
      return pb - pa;
    });
    sorted.forEach((s, i) => { s.n = i + 1; });
    room.sensorsSorted = sorted;
    return room;
  }

  function chips(room) {
    const c = room.conditions;
    const out = [];
    if (room.section_name) out.push(`<span class="chip">${esc(room.section_name)}</span>`);
    if (room.hold) out.push(`<span class="chip warn">paused (hold)</span>`);
    if (c.live && c.outdoor_mean !== null) {
      out.push(`<span class="chip">out ${deg(c.outdoor_mean)} · max ${deg(c.outdoor_max)}</span>`);
    } else {
      out.push(`<span class="chip muted">no lean (fail open)</span>`);
    }
    if (room.margin !== null && room.margin !== undefined) {
      out.push(`<span class="chip" title="cooling is blocked when the section's warmest forecast hour is at or below comfort ${room.margin >= 0 ? "−" : "+"} ${Math.abs(room.margin).toFixed(1)}°">margin ${signed(room.margin)}</span>`);
    }
    out.push(`<span class="chip muted">block ${esc(room.block.block_mode)}</span>`);
    return out.join("");
  }

  function otherUnits(room) {
    return room.units.slice(1).map((u) => {
      const [label, sub] = stateLabel(u, room);
      return `<span class="unit-chip fan-${u.fan_speed}" data-unit="${esc(u.id)}">${esc(u.name)}: ${esc(label)}${sub ? ` · ${esc(sub)}` : ""}${u.driving ? ` · ${deg(u.driving, 0)}` : ""}</span>`;
    }).join("");
  }

  function actions(room) {
    const busy = pending.has(room.id);
    const msg = messages[room.id];
    const leaks = room.units.filter((u) => u.leak.latched).map((u) => {
      const key = `${room.id}/${u.id}`;
      if (confirming.has(key)) {
        return `<span class="confirm">Is ${esc(u.name)}'s leak fixed and dry?
          <button class="btn danger" data-action="leak-yes" data-room="${esc(room.id)}" data-unit="${esc(u.id)}">Yes, release it</button>
          <button class="btn" data-action="leak-cancel" data-room="${esc(room.id)}" data-unit="${esc(u.id)}">Cancel</button></span>`;
      }
      return u.leak.confirming
        ? `<span class="note">Leak fix confirmed: waiting for Home Assistant to release it</span>`
        : `<button class="btn danger" data-action="leak" data-room="${esc(room.id)}" data-unit="${esc(u.id)}">Leak fixed…</button>`;
    }).join("");
    return `<div class="actions">
      <button class="btn vote cold" data-action="vote" data-direction="too_cold" data-room="${esc(room.id)}"${busy ? " disabled" : ""}>▼ Too cold</button>
      <button class="btn vote warm" data-action="vote" data-direction="too_hot" data-room="${esc(room.id)}"${busy ? " disabled" : ""}>▲ Too hot</button>
      <span class="vote-msg ${msg ? esc(msg.kind) : ""}" role="status">${msg ? esc(msg.text) : busy ? "sending…" : ""}</span>
      ${leaks}
    </div>`;
  }

  function card(room) {
    prepare(room);
    const others = otherUnits(room);
    return `<section class="now-room" data-room="${esc(room.id)}">
      <div class="now-head"><h2>${esc(room.name)}</h2><div class="chips">${chips(room)}</div></div>
      <div class="now-body">
        <div class="gauge">${donut(room)}${correctionScale(room)}</div>
        ${table(room)}
      </div>
      ${others ? `<div class="unit-chips">${others}</div>` : ""}
      ${actions(room)}
    </section>`;
  }

  function strip(warnings) {
    if (!warnings.length) {
      return `<div class="now-strip ok" role="status"><span class="icon">✓</span> All clear</div>`;
    }
    const icon = { error: "⛔", warning: "⚠", info: "ℹ" };
    return `<ul class="now-strip" role="status">${warnings.map((w) =>
      `<li class="${esc(w.level)}" data-kind="${esc(w.kind)}"><span class="icon" aria-label="${esc(w.level)}">${icon[w.level] || "•"}</span> ${esc(w.text)}</li>`).join("")}</ul>`;
  }

  // ---- data and events -------------------------------------------

  function draw() {
    const view = document.getElementById("view");
    if (!data) return;
    const now = Date.now();
    for (const [room, m] of Object.entries(messages)) if (m.until < now) delete messages[room];
    if (!data.rooms.length) {
      view.innerHTML = strip(data.warnings || []) +
        `<p class="placeholder">${data.connected === false ? "Not connected to Home Assistant." : "No rooms configured yet."}</p>`;
      return;
    }
    view.innerHTML = strip(data.warnings || []) +
      `<div class="now-rooms">${data.rooms.map(card).join("")}</div>`;
  }

  async function load() {
    try {
      const r = await fetch("/api/now");
      data = await r.json();
    } catch {
      data = { connected: false, rooms: [], warnings: [
        { level: "error", kind: "connection", text: "The Adaptive Climate API can't be reached." }] };
    }
    draw();
  }

  function say(room, text, kind, seconds = 20) {
    messages[room] = { text, kind, until: Date.now() + seconds * 1000 };
  }

  async function vote(room, direction) {
    pending.add(room);
    draw();
    try {
      const r = await fetch(`/api/rooms/${encodeURIComponent(room)}/vote`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ direction }) });
      const j = await r.json();
      if (!r.ok) say(room, `Not sent: ${j.detail || r.status}`, "err");
      else if (!j.outcome) say(room, "Sent", "info");
      else if (!j.outcome.accepted) say(room, "Already correcting, ignored", "warn");
      else say(room, direction === "too_cold" ? "Got it, resting" : "Got it, cooling harder", "ok");
    } catch {
      say(room, "Not sent: the API can't be reached", "err");
    }
    pending.delete(room);
    await load();
  }

  async function leakFixed(room, unit) {
    confirming.delete(`${room}/${unit}`);
    try {
      const r = await fetch(`/api/rooms/${encodeURIComponent(room)}/units/${encodeURIComponent(unit)}/leak-fixed`,
        { method: "POST" });
      const j = await r.json();
      say(room, r.ok ? "Leak fix confirmed" : `Not sent: ${j.detail || r.status}`, r.ok ? "ok" : "err");
    } catch {
      say(room, "Not sent: the API can't be reached", "err");
    }
    await load();
  }

  function wire() {
    if (wired) return;
    wired = true;
    document.getElementById("view").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-action]");
      if (!b || !document.querySelector(".now-rooms")) return;
      const { action, room, unit, direction } = b.dataset;
      if (action === "vote") vote(room, direction);
      else if (action === "leak") { confirming.add(`${room}/${unit}`); draw(); }
      else if (action === "leak-cancel") { confirming.delete(`${room}/${unit}`); draw(); }
      else if (action === "leak-yes") leakFixed(room, unit);
    });
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
  }

  async function render() {
    wire();
    stop();
    const view = document.getElementById("view");
    if (!data) view.innerHTML = `<p class="placeholder">Loading live state…</p>`;
    await load();
    timer = setInterval(load, POLL_MS);
  }

  return { render, stop, markerAngle, stateLabel, EDGE, SWEEP, PAST };
})();

function renderNow() { NowView.render(); }
