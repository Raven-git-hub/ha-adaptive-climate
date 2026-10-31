"""
Adaptive Climate - Config page render check (phase 7).

Every answer the page gets is the real endpoint's: for each case a World
(tools/apply_cases.py: the app over TestClient, a real Runtime and Store,
and a stubbed Home Assistant deployed with the real app.deploy) serves
GET /api/config, /api/schema and /api/entities, and POST /api/apply is
called with exactly the config the page is expected to post, in order.
tools/config_render.js then loads the real UI (app/static) in headless
Chromium at desktop width with those answers stubbed in, drives the Config
page, checks what it shows and posts, and saves screenshots to
docs/screenshots/config-*.png:

  * settings: the Outdoor and System and control panels as structured
    fields, each with one line of help, the schema's default and range and
    a "reset to default" link; the weather dropdown lists an entity with an
    hourly forecast (its temperature, selectable) and a daily-only one
    (listed, disabled, "no hourly forecast"), photographed with its list
    held open (headless Chromium draws no native popup, so the select is
    given a size for the shot); probe is shown disabled, "not
    supported yet"; the block margin reads "stop cooling when the section's
    warmest hour is at or below comfort + X" with X = -margin. A reset puts
    the default back, X = 2 posts margin -2, and Apply answers "Applied, no
    Home Assistant changes";
  * preview: a change Home Assistant would see previews its helpers and
    automations with Confirm and Cancel; Cancel posts nothing and clears it;
    Apply again and Confirm posts the preview's token and shows the deploy;
  * refused: a value out of the schema's range is refused with the reason,
    nothing applied;
  * raw JSON: the text box goes through the same Apply, and invalid JSON is
    refused on the page without posting.

    PYTHONPATH=. python tools/config_render_check.py
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from apply_cases import World, edited, forecast_config            # noqa: E402

logging.disable(logging.CRITICAL)
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
DESKTOP = {"width": 1280, "height": 900}
STALE = '[data-setting="system.sensor_stale_minutes"] input'
STEP = '[data-setting="system.control.person_step_degrees"]'
MARGIN = '[data-setting="outdoor.initial_margin"]'
WEATHER = "#f-outdoor-weather_entity"
PANEL = "#cfg-apply-panel"


def served(w: World) -> dict:
    """What the page reads before it posts anything."""
    get = lambda url, **p: w.client.get(url, params=p).json()        # noqa: E731
    return {"config": get("/api/config"), "schema": get("/api/schema"),
            "entities": {d: get("/api/entities", domain=d)
                         for d in ("climate", "sensor", "binary_sensor", "weather")}}


def post(w: World, cfg: dict, confirm: bool = False, token: str | None = None) -> dict:
    """One expected POST /api/apply: its body, the real answer, and what
    GET /api/config serves afterwards."""
    body = {"config": cfg} if not confirm else {"config": cfg, "confirm": True, "token": token}
    answer = w.apply(cfg, confirm, token)
    return {"body": body, "respond": answer, "config_after": w.client.get("/api/config").json()}


def case_settings() -> dict:
    start = edited(forecast_config(), **{"system.control.person_step_degrees": 1.5})
    want = edited(start, **{"system.control.person_step_degrees": 1.0,
                            "outdoor.initial_margin": -2})
    with World(on_disk=start, deployed=start) as w:
        data = served(w)
        posts = [post(w, want)]
    assert posts[0]["respond"]["message"] == "Applied, no Home Assistant changes", posts
    hourly = "Forecast Home (weather.forecast_home) — 24.3°C, hourly forecast"
    daily = "Hong Kong Observatory (weather.hong_kong_observatory) — 25°C, " \
            "no hourly forecast"
    defaults = {
        "outdoor.source": "default none", "outdoor.weather_entity": "default none",
        "outdoor.block_mode": "default shadow",
        "outdoor.initial_margin": "default comfort + 1° (margin −1) · −10 to 3",
        "outdoor.lean_per_degree": "default 0.1 · 0 to 1",
        "system.sensor_stale_minutes": "default 90 · 10 to 10080",
        "system.control.person_step_degrees": "default 1 · 0 to 5",
        "system.control.boost_degrees": "default 2 · 0.1 to 10",
        "system.control.boost_max_minutes": "default 60 · 1 to 240",
        "system.control.cool_min_heartbeats": "default 1 · 0 to 12",
        "system.control.rest_min_heartbeats": "default 2 · 0 to 12",
        "learning.reaction_rate": "default 0.5 · 0 (above) to 1",
    }
    actions = [["wait", '[data-panel="outdoor"]']]
    for path, line in defaults.items():
        actions += [["text", f'[data-setting="{path}"] .setting-default', line],
                    ["text", f'[data-setting="{path}"] .setting-default a', "reset to default"],
                    ["count", f'[data-setting="{path}"] p.help', 1]]
    actions += [
        ["value", WEATHER, "weather.forecast_home"],
        ["option", WEATHER, "weather.forecast_home", hourly, False],
        ["option", WEATHER, "weather.hong_kong_observatory", daily, True],
        ["option", WEATHER, "", "— none —", False],
        ["option", "#f-outdoor-source", "probe", "probe — not supported yet", True],
        ["value", "#f-outdoor-source", "forecast"],
        ["value", "#f-outdoor-block_mode", "shadow"],
        ["text", '[data-setting="outdoor.block_mode"] p.help',
         "Takes effect from each room's next section."],
        ["text", f"{MARGIN} .sentence",
         "Stop cooling when the section's warmest hour is at or below comfort +"],
        ["value", f"{MARGIN} input", "1"],
        ["text", f"{MARGIN} .readout", "(margin −1: comfort + 1° or below)"],
        ["value", f"{STEP} input", "1.5"],
        ["shot", "config-settings", '[data-panel="outdoor"]', '[data-panel="control"]'],
        ["open", WEATHER],
        ["shot", "config-weather", '[data-setting="outdoor.source"]',
         '[data-setting="outdoor.weather_entity"]'],
        ["close", WEATHER],
        ["click", f"{STEP} .setting-default a"],
        ["value", f"{STEP} input", "1"],
        ["fill", f"{MARGIN} input", "2"],
        ["text", f"{MARGIN} .readout", "(margin −2: comfort + 2° or below)"],
        ["click", "#cfg-apply"],
        ["wait", f'{PANEL} [data-apply="applied"]'],
        ["posts", 1],
        ["text", "#cfg-msg", "Applied, no Home Assistant changes"],
        ["text", PANEL, "Applied, no Home Assistant changes"],
        ["text", PANEL, "Saved as config version 2; in force now (changed: system, outdoor)"],
        ["absent", f"{PANEL} .plan-group"],
        ["shot", "config-applied", ".toolbar", PANEL],
    ]
    return {"name": "settings", "data": data, "posts": posts, "actions": actions}


def case_preview() -> dict:
    start = forecast_config()
    want = edited(start, **{"system.sensor_stale_minutes": 60})
    with World(on_disk=start, deployed=start) as w:
        data = served(w)
        first = post(w, want)
        again = post(w, want)
        token = again["respond"]["token"]
        confirm = post(w, want, confirm=True, token=token)
    assert first["respond"]["status"] == again["respond"]["status"] == "preview"
    assert confirm["respond"]["status"] == "applied" and confirm["respond"]["deployed"]
    n = len(first["respond"]["plan"]["automations_written"])
    actions = [
        ["wait", STALE],
        ["fill", STALE, "60"],
        ["click", "#cfg-apply"],
        ["wait", f'{PANEL} [data-apply="preview"]'],
        ["posts", 1],
        ["text", PANEL, "Apply would change Home Assistant"],
        ["text", PANEL, "Nothing has been saved yet."],
        ["text", f'{PANEL} [data-group="automations_written"] h4', f"Automations written {n}"],
        ["count", f'{PANEL} [data-group="automations_written"] li', n],
        ["count", f"{PANEL} .plan-group", 1],
        ["text", PANEL, "Unchanged: 39 helpers, " f"{30 - n} automations."],
        ["text", PANEL, "Problems: none."],
        ["text", f'{PANEL} button[data-action="apply-confirm"]', "Confirm and deploy"],
        ["text", f'{PANEL} button[data-action="apply-cancel"]', "Cancel"],
        ["text", "#cfg-msg", "Review the Home Assistant changes below"],
        ["shot", "config-apply-preview", ".toolbar", PANEL],
        ["click", f'{PANEL} button[data-action="apply-cancel"]'],
        ["absent", f"{PANEL} .apply-panel"],
        ["text", "#cfg-msg", "Cancelled: nothing was saved or deployed"],
        ["posts", 1],
        ["click", "#cfg-apply"],
        ["wait", f'{PANEL} [data-apply="preview"]'],
        ["posts", 2],
        ["click", f'{PANEL} button[data-action="apply-confirm"]'],
        ["wait", f'{PANEL} [data-apply="applied"]'],
        ["posts", 3],
        ["text", "#cfg-msg", "Applied and deployed to Home Assistant"],
        ["text", f"{PANEL} h3", "Applied and deployed to Home Assistant"],
        ["text", f"{PANEL} h4.sub", "Deployed"],
        ["text", f'{PANEL} [data-group="automations_written"] h4', f"Automations written {n}"],
        ["value", STALE, "60"],
        ["shot", "config-apply-deployed", ".toolbar", PANEL],
    ]
    return {"name": "preview", "data": data, "posts": [first, again, confirm],
            "actions": actions}


def case_refused() -> dict:
    start = forecast_config()
    with World(on_disk=start, deployed=start) as w:
        data = served(w)
        posts = [post(w, edited(start, **{"system.sensor_stale_minutes": 5}))]
    reason = ("the config does not validate: system.sensor_stale_minutes: 5 is less than "
              "the minimum of 10")
    assert posts[0]["respond"] == {"status": "refused", "problems": [reason], "plan": None}
    actions = [
        ["wait", STALE],
        ["fill", STALE, "5"],
        ["click", "#cfg-apply"],
        ["wait", f'{PANEL} [data-apply="refused"]'],
        ["posts", 1],
        ["text", "#cfg-msg", "Not applied"],
        ["text", f"{PANEL} h3", "Not applied: fix these first"],
        ["text", f"{PANEL} .apply-list.err li", reason],
        ["text", PANEL, "Nothing was saved or deployed."],
        ["absent", f'{PANEL} button[data-action="apply-confirm"]'],
        ["shot", "config-apply-refused", ".toolbar", PANEL],
    ]
    return {"name": "refused", "data": data, "posts": posts, "actions": actions}


def case_raw() -> dict:
    start = forecast_config()
    want = edited(start, **{"learning.reaction_rate": 0.4})
    with World(on_disk=start, deployed=start) as w:
        data = served(w)
        posts = [post(w, want)]
    assert posts[0]["respond"]["deployed"] is False
    actions = [
        ["wait", "#cfg-mode"],
        ["click", "#cfg-mode"],
        ["wait", "#cfg-raw"],
        ["fill", "#cfg-raw", "{ not json"],
        ["click", "#cfg-apply"],
        ["wait", f'{PANEL} [data-apply="refused"]'],
        ["text", f"{PANEL} .apply-list.err li", "invalid JSON:"],
        ["posts", 0],
        ["fill", "#cfg-raw", json.dumps(want, indent=2)],
        ["click", "#cfg-apply"],
        ["wait", f'{PANEL} [data-apply="applied"]'],
        ["posts", 1],
        ["text", f"{PANEL} h3", "Applied, no Home Assistant changes"],
        ["text", "#cfg-raw", '"reaction_rate": 0.4'],
    ]
    return {"name": "raw", "data": data, "posts": posts, "actions": actions}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("config-*.png"):       # a case removed takes its screenshot with it
        old.unlink()
    cases = [case_settings(), case_preview(), case_refused(), case_raw()]
    for c in cases:
        c["viewport"] = DESKTOP
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cases.json"
        path.write_text(json.dumps(cases))
        proc = subprocess.run(["node", str(ROOT / "tools" / "config_render.js"), str(path),
                               str(ROOT / "app" / "static"), str(OUT)],
                              capture_output=True, text=True, timeout=600)
    print(proc.stdout, end="")
    if proc.returncode:
        print(proc.stderr, end="")
        print(f"\nCONFIG PAGE RENDER CHECK FAILED ({proc.returncode})")
        return 1
    shots = sorted(p.name for p in OUT.glob("config-*.png"))
    print(f"\nALL CONFIG PAGE RENDER CHECKS PASSED ({len(cases)} cases; screenshots in "
          f"{OUT.relative_to(ROOT)}: {', '.join(shots)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
