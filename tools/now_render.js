// Adaptive Climate - the Now view in headless Chromium (phase 6).
// Run by tools/now_render_check.py, which builds the cases:
//
//   node tools/now_render.js CASES.json STATIC_DIR OUT_DIR
//
// Each case serves the real app/static files with its /api/now payload
// stubbed in, checks what the Now view draws against the case's
// expectations, presses buttons where the case says so, and saves a
// screenshot. Exits 1 on any difference. Playwright and Chromium are the
// ones preinstalled in the environment (require('playwright')).

const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const [casesFile, staticDir, outDir] = process.argv.slice(2);
const cases = JSON.parse(fs.readFileSync(casesFile, "utf8"));
const failures = [];

function fail(name, what) {
  failures.push(`${name}: ${what}`);
}

const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };

// The stubbed API. /api/now answers with the case's payload until a vote
// step says what comes next; each vote press answers with its step's
// outcome, as Home Assistant's ac_person_vote would report it.
async function serve(context, c, posts) {
  let current = c.data;
  let presses = 0;
  await context.route("**/*", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const p = url.pathname;
    if (p === "/") {
      return route.fulfill({ path: path.join(staticDir, "index.html"), contentType: "text/html" });
    }
    if (p.startsWith("/static/")) {
      const file = path.join(staticDir, p.slice("/static/".length));
      return route.fulfill({ path: file, contentType: TYPES[path.extname(file)] });
    }
    if (p === "/api/now") return route.fulfill({ json: current });
    if (p === "/api/events") return route.fulfill({ json: { events: [] } });
    if (p === "/api/rooms") {
      return route.fulfill({ json: { rooms: c.data.rooms.map((r) => ({ id: r.id, name: r.name,
        units: r.units.map((u) => ({ id: u.id, name: u.name })),
        sensors: r.sensors.map((s) => ({ id: s.id, name: s.name })) })) } });
    }
    if (p === "/healthz") {
      return route.fulfill({ json: { status: "ok", idle: false, ha_connected: true } });
    }
    if (req.method() === "POST") {
      const body = req.postData() ? JSON.parse(req.postData()) : null;
      posts.push({ path: p, body });
      if (p.endsWith("/vote")) {
        const st = c.steps[presses++];
        if (!st || st.press !== body.direction) {
          fail(c.name, `press ${presses} posted ${body.direction}, the step expects ${st && st.press}`);
          return route.fulfill({ status: 500, json: { detail: "unexpected press" } });
        }
        if (st.after) current = st.after;
        return route.fulfill({ json: { pressed: `input_button.ac_${body.direction}_main_room`,
          direction: body.direction, outcome: st.outcome === null ? null : { seq: presses,
            direction: body.direction, accepted: st.outcome.accepted, source: "button" } } });
      }
      if (p.endsWith("/leak-fixed")) {
        return route.fulfill({ json: { confirmed: "input_boolean.ac_leak_confirmed_main_room_main_room_ac" } });
      }
    }
    return route.fulfill({ status: 404, json: { detail: "not stubbed" } });
  });
}

// What the first room card shows.
function facts() {
  const room = document.querySelector(".now-room");
  const t = (sel) => { const e = room.querySelector(sel); return e ? e.textContent.trim() : null; };
  const rows = [...room.querySelectorAll("tbody tr[data-sensor]")];
  const placed = [...room.querySelectorAll(".marker:not(.hollow)")];
  const numbered = {};
  room.querySelectorAll(".marker").forEach((m) => { numbered[m.dataset.sensor] = Number(m.dataset.n); });
  return {
    rooms: document.querySelectorAll(".now-room").length,
    label: t(".state-main"), sub: t(".state-sub") || "",
    lock: room.querySelector(".lock-label")
      ? [...room.querySelectorAll(".lock-label tspan")].map((x) => x.textContent).join(" ") : null,
    dashed: !!room.querySelector(".lock.dashed"),
    warm_off: !!room.querySelector(".arc.warm.off"),
    fan: (room.querySelector(".fan") || {}).dataset?.speed,
    placed: placed.length,
    hollow: room.querySelectorAll(".marker.hollow").length,
    overflow: room.querySelectorAll('.marker[data-overflow="1"]').length,
    ticks: room.querySelectorAll(".overflow-tick").length,
    forecast_pinned: !!room.querySelector(".forecast.pinned"),
    rows: rows.map((r) => r.dataset.sensor),
    row_numbers: rows.map((r) => Number(r.querySelector("td.n").textContent)),
    marker_numbers: rows.map((r) => numbered[r.dataset.sensor]),
    notes: Object.fromEntries(rows.map((r) => [r.dataset.sensor,
      (r.querySelector(".note") || { textContent: "" }).textContent.trim()])),
    angles: Object.fromEntries(placed.map((m) => [m.dataset.sensor, parseFloat(m.dataset.angle)])),
    centre: t(".setpoint text"),
    steps: Number((room.querySelector(".chevrons") || { dataset: {} }).dataset.steps),
    chevrons: room.querySelectorAll(".chevron").length,
    chevrons_up: room.querySelector(".chevrons").dataset.steps > 0,
    correction: t(".correction"), learned: t(".learned"),
    reply: (document.querySelector(".vote-msg") || { textContent: "" }).textContent.trim(),
    forecast: !!room.querySelector(".forecast"),
    quorum: t(".quorum"),
    leak_button: !!room.querySelector('button[data-action="leak"]'),
    unit_chips: room.querySelectorAll(".unit-chip").length,
    strip_kinds: [...document.querySelectorAll(".now-strip li")].map((li) => li.dataset.kind),
    overflowing: document.documentElement.scrollWidth > window.innerWidth + 1,
  };
}

function compare(c, got) {
  const want = c.expect;
  for (const [key, value] of Object.entries(want)) {
    if (key === "strip_kinds") {
      for (const k of value) if (!got.strip_kinds.includes(k)) fail(c.name, `no ${k} warning in ${got.strip_kinds}`);
    } else if (key === "notes") {
      for (const [sid, note] of Object.entries(value)) {
        if (got.notes[sid] !== note) fail(c.name, `${sid} note "${got.notes[sid]}", expected "${note}"`);
      }
    } else if (key === "angles") {
      for (const [sid, a] of Object.entries(value)) {
        if (!(Math.abs(got.angles[sid] - a) < 0.01)) fail(c.name, `${sid} at ${got.angles[sid]}°, expected ${a}°`);
      }
    } else if (key === "edges") {
      // checked in the page below
    } else if (JSON.stringify(got[key]) !== JSON.stringify(value)) {
      fail(c.name, `${key} ${JSON.stringify(got[key])}, expected ${JSON.stringify(value)}`);
    }
  }
  // Always: every row's number is its marker's, ticks go with overflow,
  // and nothing scrolls sideways.
  if (JSON.stringify(got.row_numbers) !== JSON.stringify(got.row_numbers.map((_, i) => i + 1))) {
    fail(c.name, `rows numbered ${got.row_numbers}`);
  }
  if (JSON.stringify(got.marker_numbers) !== JSON.stringify(got.row_numbers)) {
    fail(c.name, `markers numbered ${got.marker_numbers}, rows ${got.row_numbers}`);
  }
  if (got.ticks !== got.overflow) fail(c.name, `${got.ticks} overflow ticks for ${got.overflow} pinned markers`);
  if (got.overflowing) fail(c.name, "the page scrolls sideways");
}

async function interact(c, page, posts) {
  if (c.steps.length) {
    for (let i = 0; i < c.steps.length; i++) {
      const st = c.steps[i];
      await page.click(`button[data-direction="${st.press}"]`);
      try {
        await page.waitForFunction((want) => document.querySelector(".vote-msg").textContent.trim() === want
          && !document.querySelector('button[data-action="vote"][disabled]'), st.reply, { timeout: 5000 });
      } catch {
        fail(c.name, `press ${i + 1} (${st.press}) replied "${await page.textContent(".vote-msg")}", expected "${st.reply}"`);
      }
      const got = await page.evaluate(facts);
      for (const [key, value] of Object.entries(st.expect)) {
        if (JSON.stringify(got[key]) !== JSON.stringify(value)) {
          fail(c.name, `after press ${i + 1} (${st.press}): ${key} ${JSON.stringify(got[key])}, expected ${JSON.stringify(value)}`);
        }
      }
      if (i === c.shot_step) await shot(c, page);
    }
    const votes = posts.filter((p) => p.path === "/api/rooms/main_room/vote").map((p) => p.body.direction);
    if (JSON.stringify(votes) !== JSON.stringify(c.steps.map((st) => st.press))) {
      fail(c.name, `posted ${votes}, pressed ${c.steps.map((st) => st.press)}`);
    }
  } else if (c.interact === "leak") {
    await page.click('button[data-action="leak"]');
    const confirm = await page.textContent(".actions .confirm");
    if (!confirm.includes("leak fixed and dry?")) fail(c.name, `no confirm step: "${confirm}"`);
    if (posts.length) fail(c.name, "the first click posted before the confirm");
    await page.click('button[data-action="leak-yes"]');
    await page.waitForFunction(() => document.querySelector(".vote-msg").textContent.includes("confirmed"));
    const leak = posts.find((p) => p.path === "/api/rooms/main_room/units/main_room_ac/leak-fixed");
    if (!leak) fail(c.name, `no leak-fixed post: ${JSON.stringify(posts)}`);
  }
}

// The fan spins about its own centre, at the speed its unit runs, and not
// at all when off or when the person asks for reduced motion.
async function fanCheck(browser, c) {
  const context = await browser.newContext({ viewport: c.viewport, reducedMotion: "no-preference" });
  const posts = [];
  await serve(context, c, posts);
  const page = await context.newPage();
  await page.goto("http://ac.test/");
  await page.waitForSelector(".now-room");
  const at = () => page.evaluate(() => {
    const b = document.querySelector(".now-room .fan .blades").getBoundingClientRect();
    const hub = document.querySelector(".now-room .fan .hub").getBoundingClientRect();
    return { blades: [b.x + b.width / 2, b.y + b.height / 2], hub: [hub.x + hub.width / 2, hub.y + hub.height / 2],
             animation: getComputedStyle(document.querySelector(".now-room .fan .blades")).animationName,
             duration: getComputedStyle(document.querySelector(".now-room .fan .blades")).animationDuration };
  });
  const first = await at();
  await page.waitForTimeout(230);
  const second = await at();
  if (first.animation !== "fan-spin") fail(`${c.name} fan`, `animation ${first.animation}`);
  for (const s of [first, second]) {
    const dx = Math.abs(s.blades[0] - s.hub[0]), dy = Math.abs(s.blades[1] - s.hub[1]);
    if (dx > 3 || dy > 3) fail(`${c.name} fan`, `blades centred ${s.blades}, hub ${s.hub}`);
  }
  await context.close();
  return first.duration;
}

(async () => {
  const browser = await chromium.launch();
  for (const c of cases) {
    const context = await browser.newContext({
      viewport: c.viewport, colorScheme: c.scheme, reducedMotion: "reduce", deviceScaleFactor: 1 });
    const posts = [];
    await serve(context, c, posts);
    const page = await context.newPage();
    page.on("pageerror", (e) => fail(c.name, `page error: ${e.message}`));
    page.on("console", (m) => { if (m.type() === "error") fail(c.name, `console: ${m.text()}`); });
    await page.goto("http://ac.test/");
    await page.waitForSelector(".now-room");
    const got = await page.evaluate(facts);
    compare(c, got);
    if (c.expect.edges) {
      const [cold, warm] = c.expect.edges;
      const angles = await page.evaluate(([cold, warm]) => [
        NowView.markerAngle(cold, cold, warm).angle, NowView.markerAngle(warm, cold, warm).angle,
        NowView.markerAngle(0, cold, warm).angle,
        NowView.markerAngle(warm + 2, cold, warm).angle, NowView.markerAngle(cold - 2, cold, warm).angle,
        NowView.markerAngle(warm + 2.01, cold, warm).overflow], c.expect.edges);
      const want = [-45, 45, 0, 135, -135, true];
      if (JSON.stringify(angles) !== JSON.stringify(want)) {
        fail(c.name, `edges ${cold}/${warm} map to ${angles}, expected ${want}`);
      }
    }
    if (c.steps.length) await interact(c, page, posts);       // screenshots at its shot_step
    else if (c.interact === "leak") {
      await shot(c, page);
      await interact(c, page, posts);
    } else await shot(c, page);
    console.log(`${failures.some((f) => f.startsWith(c.name + ":")) ? "FAIL" : "ok  "} ${c.name}: ` +
      `${got.label}${got.sub ? " / " + got.sub : ""}${got.lock ? ", lock: " + got.lock : ""}, fan ${got.fan}, ` +
      `${got.placed} placed + ${got.hollow} hollow (${got.overflow} pinned), rows ${got.rows.join(" > ")}`);
    await context.close();
  }
  // The reply mapping on its own: Home Assistant's report decides, by the
  // direction it took.
  {
    const c = cases.find((x) => x.name === "cool");
    const context = await browser.newContext({ viewport: c.viewport });
    await serve(context, c, []);
    const page = await context.newPage();
    await page.goto("http://ac.test/");
    await page.waitForSelector(".now-room");
    const got = await page.evaluate(() => [
      NowView.reply(true, { outcome: { accepted: true, direction: "too_hot" } }, "too_hot"),
      NowView.reply(true, { outcome: { accepted: true, direction: "too_cold" } }, "too_cold"),
      NowView.reply(true, { outcome: { accepted: false, direction: "too_hot" } }, "too_hot"),
      NowView.reply(true, { outcome: { accepted: false, direction: "too_cold" } }, "too_cold"),
      NowView.reply(true, { outcome: null }, "too_hot"),
      NowView.reply(false, { detail: "no enabled room 'x'" }, "too_cold"),
    ].map((r) => r[0]));
    const want = ["Got it, cooling harder", "Got it, resting", "Already correcting, ignored",
      "Already correcting, ignored", "Sent", "Not sent: no enabled room 'x'"];
    if (JSON.stringify(got) !== JSON.stringify(want)) fail("replies", `${JSON.stringify(got)}, expected ${JSON.stringify(want)}`);
    console.log(`${failures.some((f) => f.startsWith("replies")) ? "FAIL" : "ok  "} replies: accepted too hot -> cooling harder, accepted too cold -> resting, ignored -> ignored, no report -> sent`);
    await context.close();
  }

  // Switching tabs: the Log view replaces the Now view, and Now comes back.
  {
    const c = cases.find((x) => x.name === "cool");
    const context = await browser.newContext({ viewport: c.viewport, reducedMotion: "reduce" });
    await serve(context, c, []);
    const page = await context.newPage();
    page.on("pageerror", (e) => fail("tabs", `page error: ${e.message}`));
    await page.goto("http://ac.test/");
    await page.waitForSelector(".now-room");
    await page.click('#tabs button[data-view="log"]');
    await page.waitForSelector("table.log, .toolbar");
    if (await page.$(".now-room")) fail("tabs", "the Now view stayed after opening Log");
    await page.click('#tabs button[data-view="now"]');
    await page.waitForSelector(".now-room");
    console.log(`${failures.some((f) => f.startsWith("tabs")) ? "FAIL" : "ok  "} tabs: Now -> Log -> Now, no page errors`);
    await context.close();
  }
  const boost = cases.find((c) => c.name === "boost");
  const cool = cases.find((c) => c.name === "cool");
  const fast = await fanCheck(browser, boost);
  const slow = await fanCheck(browser, cool);
  if (!(parseFloat(fast) < parseFloat(slow))) fail("fan", `fast ${fast} isn't quicker than slow ${slow}`);
  console.log(`${failures.some((f) => f.startsWith("fan") || f.includes(" fan:")) ? "FAIL" : "ok  "} the fan spins about its own centre: one turn in ${slow} on low, ${fast} on medium`);
  await browser.close();
  if (failures.length) {
    console.error(failures.map((f) => "  " + f).join("\n"));
    process.exit(1);
  }
})();

async function shot(c, page) {
  const file = path.join(outDir, `now-${c.name}.png`);
  if (c.full) await page.screenshot({ path: file, fullPage: true });
  else await page.locator(".now-room").first().screenshot({ path: file });
}
