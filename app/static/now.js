// Adaptive Climate - the Now view (docs/V2_DESIGN.md, UI; phase 6).
//
// Draws what GET /api/now returns. Every rule (staleness, a sensor's place
// in its band, the quorum, the conditions in force, the block, what a unit
// is driven to) is computed by app/nowview.py from the same app.control the
// Home Assistant templates are rendered from; this file only lays it out:
//
//   donut       fixed arcs, blue (rest) / green (comfortable) / amber (cool),
//               one numbered pointer per sensor outside the ring (close ones
//               fanned out, with leaders to their places), the fan in the
//               centre and the state in the label strip at the bottom
//   scale       what the unit is set to now, the correction from the
//               learned setpoint as chevrons, and the forecast (magenta)
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
  const R = 86, ORBIT = 108;                 // ring radius; a marker's orbit
  // Markers whose centres would be closer than a marker's width (plus a
  // hair) fan out along the arc to SPACING apart, on an orbit a little
  // further out, FAN_ORBIT, so each one's leader to its place shows.
  const MARK_R = 9.5, SPACING = 25, FAN_ORBIT = 122;
  const FAN_SEP = (SPACING / ORBIT) * (180 / Math.PI);       // degrees apart
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
  // A setpoint: whole degrees as such, a half as one decimal.
  const temp = (x) => `${Number.isInteger(x) ? x : Number(x).toFixed(1)}°`;

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

  // Where each marker goes, from their true angles in ascending order:
  // [{at, fanned}]. Markers closer than FAN_SEP form a group, laid out
  // FAN_SEP apart round the mean of its members' angles and kept within
  // the scale's ends; groups that then come closer than FAN_SEP merge,
  // until none do. Order round the arc is kept.
  function spread(angles) {
    const span = (g) => FAN_SEP * (g.n - 1);
    const start = (g) => Math.max(-SWEEP, Math.min(g.mean - span(g) / 2, SWEEP - span(g)));
    const groups = angles.map((a) => ({ n: 1, mean: a }));
    for (let k = 0; k + 1 < groups.length;) {
      const a = groups[k], b = groups[k + 1];
      if (start(b) - (start(a) + span(a)) < FAN_SEP - 1e-6) {
        groups.splice(k, 2, { n: a.n + b.n, mean: (a.mean * a.n + b.mean * b.n) / (a.n + b.n) });
        k = Math.max(k - 1, 0);          // the merged group may now reach the one before
      } else k++;
    }
    return groups.flatMap((g) => Array.from({ length: g.n },
      (_, i) => ({ at: start(g) + i * FAN_SEP, fanned: g.n > 1 })));
  }

  function markers(room) {
    const cold = room.conditions.cold_edge, warm = room.conditions.warm_edge;
    const placed = [], hollow = [];
    room.sensorsSorted.forEach((s) => {
      if (s.position === null || s.position === undefined) hollow.push(s);
      else placed.push({ s, ...markerAngle(s.position, cold, warm) });
    });
    // Round the arc; two pinned at one end go furthest out nearest the end.
    placed.sort((a, b) => a.angle - b.angle || a.s.position - b.s.position);
    const where = spread(placed.map((m) => m.angle));
    placed.forEach((m, i) => Object.assign(m, where[i]));
    // A fanned group's leaders go under every marker.
    const leaders = placed.filter((m) => m.fanned).map(({ s, angle, at }) => {
      // From an arrowhead on the ring at the sensor's own place, out to its
      // marker, wherever the fan put it.
      const [tx, ty] = polar(R + 11, angle);
      const [h1x, h1y] = polar(R + 18, angle - 2.6), [h2x, h2y] = polar(R + 18, angle + 2.6);
      const [hx, hy] = polar(R + 17, angle), [mx, my] = polar(FAN_ORBIT, at);
      return `<g class="leader" data-sensor="${esc(s.id)}">
        <path class="leader-line" d="M${hx.toFixed(1)},${hy.toFixed(1)} L${mx.toFixed(1)},${my.toFixed(1)}"/>
        <path class="pointer" d="M${tx.toFixed(1)},${ty.toFixed(1)} L${h1x.toFixed(1)},${h1y.toFixed(1)} L${h2x.toFixed(1)},${h2y.toFixed(1)} Z"/>
      </g>`;
    }).join("");
    const out = placed.map(({ s, angle, overflow, at, fanned }) => {
      const orbit = fanned ? FAN_ORBIT : ORBIT;
      const [x, y] = polar(orbit, at);
      // A lone marker points at the ring itself; a fanned one has its leader.
      let pointer = "";
      if (!fanned) {
        const [tx, ty] = polar(R + 12, angle);
        const [b1x, b1y] = polar(orbit - 9, angle - 3.2), [b2x, b2y] = polar(orbit - 9, angle + 3.2);
        pointer = `<path class="pointer" d="M${tx.toFixed(1)},${ty.toFixed(1)} L${b1x.toFixed(1)},${b1y.toFixed(1)} L${b2x.toFixed(1)},${b2y.toFixed(1)} Z"/>`;
      }
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
          data-angle="${angle.toFixed(2)}" data-at="${at.toFixed(2)}" data-fanned="${fanned ? 1 : 0}"
          data-overflow="${overflow ? 1 : 0}">
        <title>${s.n}. ${esc(s.name)}: ${deg(s.reading)} (${signed(s.position, 2)} band)</title>
        ${pointer}
        <circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${MARK_R}"/>
        <text x="${x.toFixed(1)}" y="${(y + 3.6).toFixed(1)}">${s.n}</text>${tick}
      </g>`;
    }).join("");
    const gap = 22, x0 = -((hollow.length - 1) * gap) / 2;
    const hol = hollow.map((s, i) => `<g class="marker hollow" data-sensor="${esc(s.id)}" data-n="${s.n}">
        <title>${s.n}. ${esc(s.name)}: ${s.stale ? `stale since ${esc(s.stale_since)}` : esc(s.status)}</title>
        <circle cx="${x0 + i * gap}" cy="124" r="8.5"/>
        <text x="${x0 + i * gap}" y="127.5">${s.n}</text></g>`).join("");
    return leaders + out + hol;
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
    // Chevrons: what the unit is set to against the learned setpoint, one
    // per degree up to three, up for warmer (the offset, a vote's moved
    // base, a boost all count).
    const corr = unit ? unit.correction : null;
    const steps = corr === null || corr === undefined ? 0
      : Math.sign(corr) * Math.min(3, Math.round(Math.abs(corr)));
    // Stacked out from the centre box, 14 px apart: they count degrees,
    // they don't sit on the ticks.
    const chevronY = (k) => cy + Math.sign(-steps) * (21 + (k - 1) * 14);
    const chevrons = [];
    for (let k = 1; k <= Math.abs(steps); k++) {
      const up = steps > 0, yy = chevronY(k);
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
    // The centre: what the unit is set to now, or Off. The learned value
    // goes underneath.
    const centre = !unit || !unit.available ? "\u2014" : unit.off ? "Off"
      : unit.set_to === null || unit.set_to === undefined ? "\u2014" : temp(unit.set_to);
    const learned = applied === null || applied === undefined ? "not learned yet"
      : `learned ${temp(applied)}`;
    const corrText = steps ? `${corr > 0 ? "+" : "\u2212"}${temp(Math.abs(corr))}` : "";
    const corrY = (chevronY(1) + chevronY(Math.abs(steps))) / 2 + 3.5;   // beside them
    return `<svg class="scale" viewBox="0 0 120 270" role="img"
        aria-label="Set to ${centre}${steps ? `, ${corrText} from the learned setpoint` : ""}; ${learned}">
      <text class="scale-cap" x="${x}" y="10">warmer</text>
      <line class="axis" x1="${x}" x2="${x}" y1="${y(SCALE_RANGE)}" y2="${y(-SCALE_RANGE)}"/>
      ${ticks.join("")}
      <g class="chevrons" data-steps="${steps}">${chevrons.join("")}</g>
      <g class="setpoint${unit && unit.off ? " off" : ""}">
        <rect x="${x - 26}" y="${cy - 14}" width="52" height="28" rx="7"/>
        <text x="${x}" y="${cy + 6}">${centre}</text>
      </g>
      ${steps ? `<text class="correction" x="${x - 13}" y="${corrY}">${corrText}</text>` : ""}
      ${forecast}
      <text class="scale-cap" x="${x}" y="246">cooler</text>
      <text class="learned" x="${x}" y="264">${esc(learned)}</text>
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
      return `<span class="unit-chip fan-${u.fan_speed}" data-unit="${esc(u.id)}">${esc(u.name)}: ${esc(label)}${sub ? ` · ${esc(sub)}` : ""}${u.off ? "" : u.set_to !== null && u.set_to !== undefined ? ` · set to ${temp(u.set_to)}` : ""}</span>`;
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

  // What the card says after a press: from Home Assistant's own report of
  // it (accepted or ignored, and the direction it took), "Sent" when none
  // came back in time.
  function reply(ok, j, pressed) {
    if (!ok) return [`Not sent: ${j.detail || "error"}`, "err"];
    const o = j.outcome;
    if (!o) return ["Sent", "info"];
    if (!o.accepted) return ["Already correcting, ignored", "warn"];
    return (o.direction || pressed) === "too_cold"
      ? ["Got it, resting", "ok"] : ["Got it, cooling harder", "ok"];
  }

  async function vote(room, direction) {
    pending.add(room);
    draw();
    try {
      const r = await fetch(`/api/rooms/${encodeURIComponent(room)}/vote`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ direction }) });
      const j = await r.json();
      say(room, ...reply(r.ok, j, direction));
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

  return { render, stop, markerAngle, spread, stateLabel, reply, EDGE, SWEEP, PAST,
           R, FAN_SEP };
})();

function renderNow() { NowView.render(); }
