"""
Adaptive Climate - Apply API check (phase 7).

Drives POST /api/apply through FastAPI's TestClient against a running app:
its real Store, a real Runtime started on the config on disk, and a
stubbed Home Assistant deployed with the real app.deploy (tools/
apply_cases.py). No network. Checks:

  * no Home Assistant change (learning.reaction_rate 0.5 -> 0.4, which no
    template reads): backed up, saved as version 2 over the version 1 file
    on disk, hot-reloaded into the same Runtime (one config event,
    "learning changed"), and nothing deployed - "Applied, no Home Assistant
    changes";
  * a Home Assistant change (sensor_stale_minutes 90 -> 60, which the
    templates read): a preview listing exactly the automations whose render
    changes, with a token, and nothing written; not confirming (Cancel)
    leaves the file, the backups, the running config, Home Assistant and
    the log as they were; a Confirm with another token is a new preview,
    still writing nothing; Confirm with the preview's token backs up,
    saves, reloads and deploys exactly the previewed automations, logged as
    a deploy event, after which Home Assistant holds them;
  * problems refuse, changing nothing: a config that doesn't validate, a
    sensor Home Assistant doesn't have, Home Assistant out of reach, and no
    credentials;
  * a reload that fails returns the error and leaves the file, the running
    config and Home Assistant as they were;
  * an idle container (no runtime) starts its runtime on the config applied;
  * every Apply that goes through first copies the config to
    data/config/backups/config-<timestamp>.json, and only the newest 20
    are kept (anything else in the folder is left alone);
  * GET /api/entities?domain=weather lists each weather entity's current
    temperature and whether it has an hourly forecast; GET /api/schema
    serves the config schema.

    PYTHONPATH=. python tools/apply_api_check.py
"""
from __future__ import annotations

import copy
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app.main as main                                          # noqa: E402
from app import generator as g                                   # noqa: E402
from apply_cases import DAILY, HOURLY, REAL_HA_CLIENTS, V1, V2, World, edited  # noqa: E402

logging.getLogger("httpx").setLevel(logging.WARNING)


def check(ok: bool, what: str) -> None:
    if not ok:
        raise AssertionError(what)
    print(f"{what} - OK")


def rendered_diff(before: dict, after: dict) -> list[str]:
    a = {x["id"]: x for x in g.render_all(before)["automations"]}
    b = {x["id"]: x for x in g.render_all(after)["automations"]}
    return sorted(k for k in b if b[k] != a.get(k))


def main_() -> int:
    # --- no Home Assistant change: saved, reloaded, not deployed ---
    with World(on_disk=V1, deployed=V2) as w:
        rt = w.runtime
        check(w.file()["version"] == 1 and rt is not None and rt.config == V2,
              "setup: the file on disk is version 1, Home Assistant is deployed from it, and "
              "the runtime runs on it migrated")
        new = edited(V2, **{"learning.reaction_rate": 0.4})
        j = w.apply(new)
        check(j["status"] == "applied" and j["deployed"] is False
              and j["message"] == "Applied, no Home Assistant changes"
              and j["plan"]["changes"] == 0 and "deploy" not in j,
              "reaction_rate 0.5 -> 0.4: \"Applied, no Home Assistant changes\" (the plan has 0 "
              "changes), no deploy")
        check(w.file() == new and w.file()["version"] == 2,
              "the file is saved as version 2 with the change")
        check(w.backups() == [j["backup"]] and
              json.loads((main.CONFIG_PATH.parent / "backups" / j["backup"]).read_text()) == V1,
              f"the version 1 file was backed up first, to backups/{j['backup']}")
        check(w.runtime is rt and rt.config["learning"]["reaction_rate"] == 0.4
              and j["reload"]["changed"] == ["learning"],
              "hot-reloaded into the same runtime (no restart): it runs on reaction_rate 0.4")
        ev = w.events("config")
        check(len(ev) == 1 and ev[0]["message"] == "Config applied without a restart: "
              "learning changed", "one config event: \"learning changed\"")
        check(w.ha.writes == [] and w.events("deploy") == [],
              "Home Assistant: no automation or helper written, no deploy event")

        # --- a Home Assistant change: preview, Cancel, a stale Confirm, Confirm ---
        stale = edited(new, **{"system.sensor_stale_minutes": 60})
        differ = rendered_diff(new, stale)
        before = w.snapshot()
        p = w.apply(stale)
        plan = p["plan"]
        check(p["status"] == "preview" and sorted(plan["automations_written"]) == differ
              and plan["changes"] == len(differ) and not plan["helpers_created"]
              and not plan["helpers_updated"] and not plan["helpers_removed"]
              and not plan["automations_removed"] and plan["problems"] == []
              and plan["automations_unchanged"] == 30 - len(differ) and p["token"],
              f"sensor_stale_minutes 90 -> 60: a preview listing exactly the {len(differ)} "
              f"automations whose templates read it ({', '.join(differ[:3])}, ...), the other "
              f"{30 - len(differ)} unchanged, no helper change, no problems, with a token")
        check(w.snapshot() == before,
              "the preview wrote nothing: the file, backups, running config, Home Assistant "
              "and log are as they were")
        # Cancel is the page sending nothing: still nothing changed
        check(w.snapshot() == before and w.runtime.config == new,
              "Cancel (no Confirm sent) changes nothing: the runtime still runs on 90 minutes")
        q = w.apply(stale, confirm=True, token="not-the-preview")
        check(q["status"] == "preview" and q["stale"] is True and q["token"] == p["token"]
              and w.snapshot() == before,
              "a Confirm whose token isn't the plan's comes back as a new preview, writing "
              "nothing")
        r = w.apply(stale, confirm=True, token=p["token"])
        dep = r.get("deploy") or {}
        check(r["status"] == "applied" and r["deployed"] is True
              and r["message"] == "Applied and deployed to Home Assistant"
              and dep["ok"] and sorted(dep["automations_written"]) == differ
              and dep["helpers_created"] == dep["helpers_updated"] == [],
              "Confirm with the preview's token: applied and deployed, writing exactly the "
              "previewed automations")
        check(w.file() == stale and len(w.backups()) == 2 and w.backups()[-1] == r["backup"]
              and json.loads((main.CONFIG_PATH.parent / "backups" / r["backup"]).read_text())
              == new,
              "backed up first (the config as it was), then saved")
        check(w.runtime is rt and rt.config["system"]["sensor_stale_minutes"] == 60
              and w.events("config")[-1]["message"] ==
              "Config applied without a restart: system changed",
              "hot-reloaded: the runtime runs on 60 minutes, with one config event")
        check(sorted(x[1] for x in w.ha.writes) == differ
              and all(w.ha.automations[a] == json.loads(json.dumps(
                  {"id": a, **{k: v for k, v in x.items() if k != "id"}}))
                      for x in g.render_all(stale)["automations"] for a in [x["id"]])
              and w.events("deploy")[-1]["message"] ==
              f"Deployed: {len(differ)} automations written",
              f"Home Assistant now holds the new render; one deploy event (\"Deployed: "
              f"{len(differ)} automations written\")")
        again = w.apply(stale)
        check(again["status"] == "applied" and again["deployed"] is False,
              "applying the same config again: no Home Assistant changes")

        # --- problems refuse, changing nothing ---
        before = w.snapshot()
        bad = w.apply(edited(stale, **{"system.sensor_stale_minutes": 5}))
        check(bad["status"] == "refused" and bad["problems"] == [
            "the config does not validate: system.sensor_stale_minutes: 5 is less than the "
            "minimum of 10"] and w.snapshot() == before,
              "a config that doesn't validate is refused, naming the field, and nothing is "
              "written")
        missing = copy.deepcopy(stale)
        missing["rooms"][0]["sensors"].append({"id": "window", "name": "Window",
                                               "entity_id": "sensor.no_such_window"})
        m = w.apply(missing)
        check(m["status"] == "refused" and m["plan"]["missing_entities"] ==
              ["sensor.no_such_window"] and "not found in Home Assistant" in m["problems"][0]
              and w.snapshot() == before,
              "a sensor Home Assistant doesn't have is refused as a missing entity, nothing "
              "written")
        w.ha.fail = ConnectionError("connection refused")
        u = w.apply(stale)
        w.ha.fail = None
        check(u["status"] == "refused" and "could not reach Home Assistant" in u["problems"][0]
              and w.snapshot() == before,
              "Home Assistant out of reach: refused, nothing written")
        clients = main._ha_clients
        main._ha_clients = REAL_HA_CLIENTS          # the environment has no credentials
        n = w.apply(stale)
        main._ha_clients = clients
        check(n["status"] == "refused" and "AC_HA_URL" in n["problems"][0]
              and w.snapshot() == before,
              "no credentials: refused (Apply can't tell what would change), nothing written")

        # --- a reload that fails changes nothing ---
        real_reload = rt.reload

        async def failing(cfg, commit=None):
            raise RuntimeError("the new rooms could not be built")
        rt.reload = failing
        e = w.apply(edited(stale, **{"system.control.boost_degrees": 2.5}), confirm=False)
        check(e["status"] == "preview" and w.snapshot() == before,
              "(a change Home Assistant would see previews first)")
        e = w.apply(edited(stale, **{"system.control.boost_degrees": 2.5}), confirm=True,
                    token=e["token"])
        rt.reload = real_reload
        snap = w.snapshot()
        check(e["status"] == "error" and e["stage"] == "reload"
              and "could not be built" in e["error"] and w.file() == stale
              and rt.config == stale and snap[3:6] == before[3:6],
              "a reload that fails: the error comes back; the file, the running config and "
              "Home Assistant are as they were, and nothing is deployed")

        # --- backups: the newest 20 kept ---
        folder = main.CONFIG_PATH.parent / "backups"
        (folder / "notes.txt").write_text("mine")
        start = len(w.backups())
        made = []
        for k in range(22):
            made.append(w.apply(edited(stale, **{"learning.reaction_rate":
                                                 round(0.3 + 0.01 * k, 2)}))["backup"])
        kept = w.backups()
        check(len(kept) == 20 and kept == sorted(kept) and kept[-1] == made[-1]
              and kept == sorted(set(kept) | set(made))[-20:]
              and (folder / "notes.txt").read_text() == "mine",
              f"after {start} + 22 Applies only the newest 20 backups are kept (named by time, "
              "so they sort), and a file that isn't a backup is left alone")

        # --- the weather picker and the schema ---
        j = w.client.get("/api/entities", params={"domain": "weather"}).json()
        by = {x["entity_id"]: x for x in j["entities"]}
        check(j["connected"] and set(by) == {HOURLY, DAILY}
              and (by[HOURLY]["temperature"], by[HOURLY]["hourly"]) == (24.3, True)
              and (by[DAILY]["temperature"], by[DAILY]["hourly"]) == (25, False)
              and by[HOURLY]["temperature_unit"] == "°C",
              "GET /api/entities?domain=weather: each weather entity with its temperature and "
              "whether it has an hourly forecast (forecast_home yes, hong_kong_observatory no)")
        sch = w.client.get("/api/schema").json()
        check(sch["properties"]["outdoor"]["properties"]["initial_margin"]["default"] == -1
              and sch["properties"]["system"]["properties"]["sensor_stale_minutes"]["minimum"]
              == 10, "GET /api/schema serves the config schema (defaults and ranges)")

    # --- an idle container starts its runtime on the config applied ---
    with World(on_disk=edited(V2, rooms=[]), deployed=V2, runtime=False) as w:
        started = []

        async def start(cfg):
            from app.runtime import Runtime
            rt = Runtime(cfg, main.state.store, w.ha, w.ha, w.tmp)
            await rt.start()
            started.append(rt)
            return rt
        real_start = main._start_runtime
        main._start_runtime = start
        j = w.apply(V2)
        main._start_runtime = real_start
        check(j["status"] == "applied" and j["reload"]["runtime"] == "started"
              and w.runtime is started[0] and set(w.runtime.rooms) ==
              {"main_room", "main_bedroom", "baby_room"} and w.file() == V2
              and w.events("config")[-1]["message"] ==
              "Config applied: the runtime started with 3 room(s)",
              "idle (no rooms) -> Apply with three rooms: the runtime starts on them, no "
              "restart")

    print("\nALL APPLY API CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main_())
