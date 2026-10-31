// Adaptive Climate - the Config page in headless Chromium (phase 7).
// Run by tools/config_render_check.py, which builds the cases from the real
// endpoints' answers:
//
//   node tools/config_render.js CASES.json STATIC_DIR OUT_DIR
//
// Each case serves the real app/static files with GET /api/config,
// /api/schema and /api/entities answered as the case says, and each
// POST /api/apply checked against the body the case expects next and
// answered with the real answer to it. The case's actions then drive the
// page and check what it shows. Exits 1 on any difference. Playwright and
// Chromium are the ones preinstalled in the environment.

const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const [casesFile, staticDir, outDir] = process.argv.slice(2);
const cases = JSON.parse(fs.readFileSync(casesFile, "utf8"));
const failures = [];
const TYPES = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };

// Objects equal whatever their key order (1 and 1.0 are one number in JSON).
function canon(x) {
  if (Array.isArray(x)) return x.map(canon);
  if (x && typeof x === "object") {
    return Object.fromEntries(Object.keys(x).sort().map((k) => [k, canon(x[k])]));
  }
  return x;
}
const same = (a, b) => JSON.stringify(canon(a)) === JSON.stringify(canon(b));

function firstDifference(a, b, where = "") {
  if (same(a, b)) return null;
  if (a && b && typeof a === "object" && typeof b === "object") {
    for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) {
      const d = firstDifference(a[k], b[k], `${where}.${k}`);
      if (d) return d;
    }
  }
  return `${where || "body"}: posted ${JSON.stringify(a)}, expected ${JSON.stringify(b)}`;
}

async function serve(context, c, posted) {
  let config = c.data.config;
  await context.route("**/*", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const p = url.pathname;
    if (p === "/") return route.fulfill({ path: path.join(staticDir, "index.html"), contentType: "text/html" });
    if (p.startsWith("/static/")) {
      const file = path.join(staticDir, p.slice("/static/".length));
      return route.fulfill({ path: file, contentType: TYPES[path.extname(file)] });
    }
    if (req.method() === "POST" && p === "/api/apply") {
      const body = JSON.parse(req.postData() || "null");
      const want = c.posts[posted.length];
      posted.push(body);
      if (!want) {
        failures.push(`${c.name}: an unexpected POST /api/apply (${posted.length})`);
        return route.fulfill({ status: 500, json: { detail: "unexpected" } });
      }
      const diff = firstDifference(body, want.body);
      if (diff) failures.push(`${c.name}: POST ${posted.length}: ${diff}`);
      if (want.respond.status === "applied") config = want.config_after;
      return route.fulfill({ json: want.respond });
    }
    if (p === "/api/config") return route.fulfill({ json: config });
    if (p === "/api/schema") return route.fulfill({ json: c.data.schema });
    if (p === "/api/entities") {
      return route.fulfill({ json: c.data.entities[url.searchParams.get("domain")] ||
        { entities: [], connected: false } });
    }
    if (p === "/api/now") {
      return route.fulfill({ json: { connected: true, generated_at: "2026-10-31T12:00:00+08:00",
        tz: "Asia/Hong_Kong", boost_degrees: 2, rooms: [], warnings: [] } });
    }
    if (p === "/api/events") return route.fulfill({ json: { events: [] } });
    if (p === "/api/rooms") return route.fulfill({ json: { rooms: [] } });
    if (p === "/healthz") return route.fulfill({ json: { status: "ok", idle: false, ha_connected: true } });
    return route.fulfill({ status: 404, json: { detail: "not stubbed" } });
  });
}

const norm = (s) => (s || "").replace(/\s+/g, " ").trim();

async function act(page, c, a, posted) {
  const [kind, sel, ...rest] = a;
  const what = `${kind} ${sel}${rest.length ? " " + JSON.stringify(rest) : ""}`;
  const bad = (msg) => failures.push(`${c.name}: ${what}: ${msg}`);
  switch (kind) {
    case "wait":
      await page.waitForSelector(sel, { timeout: 5000 }).catch(() => bad("never appeared"));
      return;
    case "click": await page.click(sel, { timeout: 5000 }); return;
    case "fill": await page.fill(sel, rest[0], { timeout: 5000 }); return;
    case "open":
      // a native dropdown's popup isn't drawn in a headless screenshot, so
      // show its list in place: a <select> with a size lists every option
      await page.$eval(sel, (e) => { e.size = e.options.length; });
      return;
    case "close": await page.$eval(sel, (e) => { e.size = 0; }); return;
    case "posts":
      if (posted.length !== sel) failures.push(`${c.name}: ${posted.length} POST(s) so far, expected ${sel}`);
      return;
    case "absent":
      if (await page.$(sel)) bad("is shown");
      return;
    case "count": {
      const n = (await page.$$(sel)).length;
      if (n !== rest[0]) bad(`found ${n}`);
      return;
    }
    case "text": {
      const el = await page.$(sel);
      if (!el) return bad("not found");
      const t = norm(await el.evaluate((e) => (e.tagName === "TEXTAREA" ? e.value : e.textContent)));
      if (!t.includes(norm(rest[0]))) bad(`reads "${t.slice(0, 300)}"`);
      return;
    }
    case "value": {
      const v = await page.$eval(sel, (e) => e.value).catch(() => null);
      if (v !== rest[0]) bad(`value is ${JSON.stringify(v)}`);
      return;
    }
    case "option": {
      const [value, text, disabled] = rest;
      const o = await page.$eval(sel, (s, v) => {
        const opt = [...s.options].find((x) => x.value === v);
        return opt ? { text: opt.textContent.replace(/\s+/g, " ").trim(), disabled: opt.disabled } : null;
      }, value);
      if (!o) return bad("no such option");
      if (!o.text.includes(text)) bad(`option reads "${o.text}"`);
      if (o.disabled !== disabled) bad(`option is ${o.disabled ? "" : "not "}disabled`);
      return;
    }
    case "shot": {
      // ["shot", name, ...selectors]: from the top of the first element to
      // the bottom of the last, saved as OUT_DIR/<name>.png
      const boxes = [];
      for (const s of rest) {
        const el = await page.$(s);
        if (!el) return bad(`${s} not found for the screenshot`);
        await el.scrollIntoViewIfNeeded();
        boxes.push(await el.evaluate((e) => {
          const r = e.getBoundingClientRect();
          return { x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height };
        }));
      }
      const x = Math.max(0, Math.min(...boxes.map((b) => b.x)) - 12);
      const y = Math.max(0, Math.min(...boxes.map((b) => b.y)) - 12);
      const right = Math.max(...boxes.map((b) => b.x + b.w)) + 12;
      const bottom = Math.max(...boxes.map((b) => b.y + b.h)) + 12;
      await page.screenshot({ path: path.join(outDir, `${sel}.png`), fullPage: true,
                              clip: { x, y, width: right - x, height: bottom - y } });
      return;
    }
    default:
      bad("unknown action");
  }
}

(async () => {
  const browser = await chromium.launch();
  for (const c of cases) {
    const context = await browser.newContext({ viewport: c.viewport, colorScheme: "dark" });
    const posted = [];
    await serve(context, c, posted);
    const page = await context.newPage();
    page.on("pageerror", (e) => failures.push(`${c.name}: page error: ${e.message}`));
    const before = failures.length;
    await page.goto("http://ac.test/");
    await page.click('#tabs button[data-view="config"]');
    for (const a of c.actions) {
      try {
        await act(page, c, a, posted);
      } catch (e) {               // a click or fill that can't happen is a failure, not a crash
        failures.push(`${c.name}: ${a[0]} ${a[1]}: ${e.message.split("\n")[0]}`);
        break;
      }
    }
    if (posted.length !== c.posts.length) {
      failures.push(`${c.name}: ${posted.length} POST(s), the case expects ${c.posts.length}`);
    }
    const ok = failures.length === before;
    console.log(`${ok ? "ok  " : "FAIL"} ${c.name}: ${c.actions.length} steps, ${posted.length} ` +
      `POST /api/apply checked against the real answers`);
    await context.close();
  }
  await browser.close();
  if (failures.length) {
    console.error(failures.map((f) => "  " + f).join("\n"));
    process.exit(1);
  }
})();
