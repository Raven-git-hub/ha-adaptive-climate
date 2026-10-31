"""
Adaptive Climate - a stubbed Home Assistant and a running app for the Apply
checks (phase 7): tools/apply_api_check.py drives POST /api/apply through
it, and tools/config_render_check.py takes the real endpoints' answers from
it for the Config page it renders.

A World is one data dir with the app's real FastAPI app (app.main) over
FastAPI's TestClient, its real Store, a real Runtime started on the config
on disk, and one StubHA standing in for Home Assistant's REST and WebSocket
APIs (states, services, the automation config editor, helper storage).
Before the app starts, Home Assistant is deployed from a config of the
world's choosing, with the real app.deploy, so it holds exactly what that
config renders.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient                        # noqa: E402

import app.main as main                                          # noqa: E402
from app import deploy                                           # noqa: E402
from app.generator import SECTIONS                               # noqa: E402

HK = ZoneInfo("Asia/Hong_Kong")
# the app's own client factory (from the environment), before a World stubs it
REAL_HA_CLIENTS = main._ha_clients
V1 = json.loads((ROOT / "examples" / "config.v1.example.json").read_text())
V2 = json.loads((ROOT / "examples" / "config.example.json").read_text())
HOURLY = "weather.forecast_home"                 # Met.no: hourly and daily
DAILY = "weather.hong_kong_observatory"          # daily only
WEATHER = [
    {"entity_id": HOURLY, "state": "cloudy",
     "attributes": {"friendly_name": "Forecast Home", "temperature": 24.3,
                    "temperature_unit": "°C", "supported_features": 3}},
    {"entity_id": DAILY, "state": "sunny",
     "attributes": {"friendly_name": "Hong Kong Observatory", "temperature": 25,
                    "temperature_unit": "°C", "supported_features": 1}},
]


def forecast_config() -> dict:
    """The example config with the forecast as its outdoor source."""
    cfg = copy.deepcopy(V2)
    cfg["outdoor"].update(source="forecast", weather_entity=HOURLY)
    return cfg


def edited(cfg: dict, **changes) -> dict:
    """A copy of cfg with dotted paths set: edited(c, **{"system.x": 1})."""
    out = copy.deepcopy(cfg)
    for dotted, value in changes.items():
        node = out
        *head, last = dotted.split(".")
        for k in head:
            node = node[int(k)] if k.isdigit() else node[k]
        node[last] = value
    return out


def ha_states(cfg: dict) -> list[dict]:
    """What Home Assistant has: each room's units and sensors and our scene
    automations, and two weather entities, one with an hourly forecast."""
    now = datetime.now(timezone.utc).isoformat()
    out = copy.deepcopy(WEATHER)
    for w in out:
        w["last_updated"] = now
    for room in cfg["rooms"]:
        for sec in SECTIONS:
            out.append({"entity_id": f"automation.ac_{room['id']}_{sec}", "state": "on",
                        "attributes": {"id": f"ac_scene_{room['id']}_{sec}"}})
        for u in room["units"]:
            out.append({"entity_id": u["entity_id"], "state": "cool",
                        "attributes": {"temperature": 23.0, "current_temperature": 24.0,
                                       "fan_mode": "low", "min_temp": 16, "max_temp": 30}})
        for i, s in enumerate(room["sensors"]):
            if s.get("entity_id"):
                out.append({"entity_id": s["entity_id"], "state": str(24.0 + 0.2 * i),
                            "attributes": {"friendly_name": s.get("name", s["id"]),
                                           "unit_of_measurement": "°C"}})
    return out


class StubHA:
    """Home Assistant's REST and WebSocket APIs in one object. `writes`
    records what only a deploy does (automations written or deleted,
    helpers created, updated or deleted); `calls` every service call."""

    def __init__(self, states: list[dict]):
        self.states_ = states
        self.automations: dict[str, dict] = {}
        self.helpers = {d: {} for d in deploy.HELPER_DOMAINS}
        self.writes: list[tuple] = []
        self.calls: list[tuple] = []
        self.pushed: list[tuple] = []
        self.fail: Exception | None = None

    # --- REST ---
    async def config(self):
        return {"time_zone": "Asia/Hong_Kong", "version": "2026.10.1"}

    async def states(self):
        if self.fail:
            raise self.fail
        return copy.deepcopy(self.states_)

    async def call_service(self, domain, service, data=None, *, return_response=False):
        self.calls.append((domain, service, data or {}))
        if (domain, service) == ("weather", "get_forecasts"):
            first = datetime.now(HK).replace(minute=0, second=0, microsecond=0)
            return {HOURLY: {"forecast": [
                {"datetime": (first + timedelta(hours=k)).astimezone(timezone.utc).isoformat(),
                 "temperature": 26.0} for k in range(48)]}}
        return None

    async def set_state(self, entity_id, state, attributes=None):
        self.pushed.append((entity_id, state))

    async def get_automation(self, aid):
        return copy.deepcopy(self.automations.get(aid))

    async def set_automation(self, aid, cfg):
        self.writes.append(("set_automation", aid))
        self.automations[aid] = json.loads(json.dumps(
            {"id": aid, **{k: v for k, v in cfg.items() if k != "id"}}))

    async def delete_automation(self, aid):
        self.writes.append(("delete_automation", aid))
        self.automations.pop(aid, None)

    # --- WebSocket ---
    def on_reconnect(self, cb):
        pass

    def on_status(self, cb):
        pass

    async def connect(self):
        pass

    async def subscribe_events(self, event_type, callback):
        return 1

    async def subscribe_states(self, callback):
        return 1

    async def list_helpers(self, domain):
        if self.fail:
            raise self.fail
        return [{"id": oid, **v} for oid, v in self.helpers[domain].items()]

    async def create_helper(self, domain, payload):
        self.writes.append(("create_helper", domain, payload["name"]))
        self.helpers[domain][payload["name"]] = payload
        return {"id": payload["name"]}

    async def update_helper(self, domain, helper_id, payload):
        self.writes.append(("update_helper", domain, helper_id))
        self.helpers[domain][helper_id] = payload

    async def delete_helper(self, domain, helper_id):
        self.writes.append(("delete_helper", domain, helper_id))
        self.helpers[domain].pop(helper_id, None)

    async def close(self):
        pass


class World:
    """The app over TestClient, with Home Assistant deployed from `deployed`
    and `on_disk` written as the config file (a version 1 config stays
    version 1 on disk until an Apply). `runtime`: start a real Runtime on
    the config on disk, as the app's lifespan would with credentials set.
    Use as a context manager."""

    def __init__(self, on_disk: dict, deployed: dict | None = None, runtime: bool = True,
                 states: list[dict] | None = None):
        self.tmp = Path(tempfile.mkdtemp())
        self.on_disk, self.deployed = on_disk, deployed
        self.ha = StubHA(states if states is not None else ha_states(deployed or V2))
        self.want_runtime = runtime

    def __enter__(self) -> World:
        main.DATA_DIR = self.tmp
        main.CONFIG_PATH = self.tmp / "config" / "config.json"
        main.CONFIG_PATH.parent.mkdir(parents=True)
        main.CONFIG_PATH.write_text(json.dumps(self.on_disk, indent=2))
        self._saved_env = {k: os.environ.pop(k, None) for k in ("AC_HA_URL", "AC_HA_TOKEN")}
        main._ha_clients = lambda: (self.ha, self.ha)
        main.state.runtime, main.state.error = None, None
        self._client = TestClient(main.app)
        self.client = self._client.__enter__()        # the lifespan: the Store, idle
        self.client.portal.call(self._start)
        return self

    async def _start(self) -> None:
        if self.deployed is not None:
            report = await deploy.deploy(self.deployed, self.ha, self.ha, self.tmp)
            assert report.ok, report.problems
            self.ha.writes.clear()
        if self.want_runtime:
            from app.runtime import Runtime
            rt = Runtime(main._raw_config(), main.state.store, self.ha, self.ha, self.tmp)
            await rt.start()
            main.state.runtime = rt
        await asyncio.sleep(0)

    def __exit__(self, *exc) -> None:
        self._client.__exit__(*exc)                   # stops the runtime, closes the store
        main.state.runtime = None
        os.environ.update({k: v for k, v in self._saved_env.items() if v})

    # --- reading the world ---
    @property
    def runtime(self):
        return main.state.runtime

    def file(self) -> dict:
        return json.loads(main.CONFIG_PATH.read_text())

    def backups(self) -> list[str]:
        folder = main.CONFIG_PATH.parent / "backups"
        return sorted(p.name for p in folder.glob("config-*.json")) if folder.exists() else []

    def events(self, category: str) -> list[dict]:
        return list(reversed(main.state.store.recent_events(limit=1000, category=category)))

    def apply(self, cfg: dict, confirm: bool = False, token: str | None = None) -> dict:
        body = {"config": cfg}
        if confirm:
            body.update(confirm=True, token=token)
        r = self.client.post("/api/apply", json=body)
        assert r.status_code == 200, r.text
        return r.json()

    def snapshot(self) -> tuple:
        """Everything an Apply could change: the file, the backups, the
        running config, Home Assistant's automations and helpers, and the
        config and deploy events."""
        rt = self.runtime
        return (main.CONFIG_PATH.read_text(), self.backups(),
                json.dumps(rt.config, sort_keys=True) if rt else None,
                json.dumps(self.ha.automations, sort_keys=True),
                json.dumps(self.ha.helpers, sort_keys=True), len(self.ha.writes),
                len(self.events("config")), len(self.events("deploy")))
