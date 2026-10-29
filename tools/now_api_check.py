"""
Adaptive Climate - Now view API check (phase 6).

Drives app.main's endpoints through FastAPI's TestClient with a stubbed
Home Assistant (no network):

  * GET /api/now: the payload's shape, key by key (so a change to it is
    deliberate), and its values for a Main Room in boost: the section, the
    unit's helpers and what it is driven to, the almanac section, each
    sensor's reading, place in its band and zone, the conditions entity,
    the block, the quorum and the margin; a stale sensor and a hold as
    warnings; Home Assistant out of reach, or no credentials, still a 200
    with that as the warning
  * POST /api/rooms/{room}/vote: presses exactly the room's button for the
    direction; refuses an unknown or disabled room (404) and an unknown
    direction (400); with the runtime live, returns Home Assistant's
    ac_person_vote outcome for the press
  * POST /api/rooms/{room}/units/{unit}/leak-fixed: turns on that unit's
    ac_leak_confirmed boolean; refuses a unit without leak detection (400)
    and an unknown unit (404)

    PYTHONPATH=. python tools/now_api_check.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient                        # noqa: E402

import app.main as main                                          # noqa: E402
from app.store import Store                                      # noqa: E402
from now_cases import HK, ROOM, UNIT, world                      # noqa: E402

SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "storage.schema.sql"

ROOM_KEYS = {"id", "name", "section", "section_name", "hold", "guard", "comfort_point", "margin",
             "conditions", "block", "heat_sources", "votes", "units", "sensors", "last_vote"}
UNIT_KEYS = {"id", "name", "entity_id", "available", "hvac_mode", "fan_mode",
             "current_temperature", "setpoint", "fan_speed", "control", "setpoint_applied",
             "target", "driving", "leak"}
CONTROL_KEYS = {"state", "base", "offset", "forced", "unblocked", "since", "in_grace"}
SENSOR_KEYS = {"id", "name", "entity_id", "virtual", "reading", "last_reported", "stale",
               "stale_since", "status", "comfort", "band", "trust", "position", "zone"}
CONDITION_KEYS = {"live", "state", "reason", "block_mode", "cold_edge", "warm_edge", "blocked",
                  "would_block", "margin", "comfort_point", "outdoor_mean", "outdoor_max",
                  "delta_mean", "delta_max", "lean", "fail_open", "computed_at"}


class StubRest:
    """Home Assistant's REST API: states, config and service calls."""
    def __init__(self, states: dict, fail: Exception | None = None, on_call=None):
        self.states_ = states
        self.fail = fail
        self.calls: list[tuple] = []
        self.on_call = on_call

    async def states(self):
        if self.fail:
            raise self.fail
        return list(self.states_.values())

    async def config(self):
        return {"time_zone": "Asia/Hong_Kong"}

    async def call_service(self, domain, service, data=None, *, return_response=False):
        self.calls.append((domain, service, data))
        if self.on_call:
            self.on_call(domain, service, data)

    async def close(self):
        pass


def setup(rest: StubRest, cfg: dict, runtime=None) -> TestClient:
    main.CONFIG_PATH.write_text(json.dumps(cfg))
    main._ha_clients = lambda: (rest, None)
    main.state.runtime = runtime
    return TestClient(main.app)


def check(ok: bool, what: str) -> None:
    if not ok:
        raise AssertionError(what)
    print(f"{what} - OK")


def main_() -> int:
    tmp = Path(tempfile.mkdtemp())
    (tmp / "config").mkdir()
    main.CONFIG_PATH = tmp / "config" / "config.json"
    main.state.store = Store(tmp, SCHEMA)
    original_clients = main._ha_clients
    now = datetime.now(HK)

    # --- the shape, and a room in boost ---
    cfg, states, almanacs, _ = world("boost", offset=-1, readings=(25.2, 24.1, 24.9),
                                     now=now, leak=True)
    rest = StubRest(states)
    client = setup(rest, cfg)
    main.state.store.current_almanac = lambda room_id, as_of=None: almanacs.get(
        room_id, {"room_id": room_id, "sections": {}})
    r = client.get("/api/now")
    check(r.status_code == 200, "GET /api/now answers 200")
    j = r.json()
    check(set(j) == {"connected", "generated_at", "tz", "boost_degrees", "rooms", "warnings"}
          and j["connected"] is True and j["tz"] == "Asia/Hong_Kong" and len(j["rooms"]) == 1,
          "the payload: connected, generated_at, tz (Home Assistant's), boost_degrees, rooms, "
          "warnings")
    room = j["rooms"][0]
    unit, sensors = room["units"][0], {s["id"]: s for s in room["sensors"]}
    check(set(room) == ROOM_KEYS and set(unit) == UNIT_KEYS and set(unit["control"]) == CONTROL_KEYS
          and all(set(s) == SENSOR_KEYS for s in room["sensors"])
          and set(room["conditions"]) == CONDITION_KEYS
          and set(room["block"]) == {"blocked", "by", "would_block", "block_mode"}
          and set(room["votes"]) == {"met", "tally", "required", "direction", "warm_side",
                                     "cold_side"}
          and set(unit["leak"]) == {"enabled", "latched", "confirming"},
          "a room's keys, a unit's (and its control helpers and leak latch), a sensor's, the "
          "conditions', the block's and the quorum's are exactly the documented ones")
    check((room["id"], room["section"], room["section_name"], room["hold"]) ==
          (ROOM, "night", "Night", False)
          and (unit["id"], unit["control"]["state"], unit["control"]["offset"],
               unit["setpoint_applied"], unit["driving"], unit["fan_speed"]) ==
          (UNIT, "boost", -1, 23.0, 20.0, "fast")
          and unit["leak"] == {"enabled": True, "latched": True, "confirming": False}
          and any(w["kind"] == "leak" and w["level"] == "error" for w in j["warnings"]),
          "the unit: boost at offset -1, applied 23, driven to 23 - 1 - 2 = 20, fan fast; its "
          "leak latch on, and at the top of the warnings")
    cond = room["conditions"]
    check(cond["live"] and (cond["cold_edge"], cond["warm_edge"], cond["block_mode"],
                            cond["outdoor_mean"], cond["outdoor_max"]) ==
          (-1.3, 0.7, "shadow", 27.0, 29.0)
          and room["block"] == {"blocked": False, "by": None, "would_block": False,
                                "block_mode": "shadow"},
          "the conditions entity, live for Night: edges -1.3 / +0.7 (24 inside, 27 outside), "
          "forecast 27 / 29, not blocked")
    check({sid: (s["reading"], s["position"], s["zone"]) for sid, s in sensors.items()} ==
          {"living1": (25.2, 1.2, "warm"), "living2": (24.1, 0.1, "ok"),
           "living3": (24.9, 0.9, "warm")}
          and room["votes"] == {"met": True, "tally": 2, "required": 2, "direction": "too_warm",
                                "warm_side": 3, "cold_side": 0}
          and room["comfort_point"] == 24.0 and room["margin"] == -1.0,
          "the sensors: readings, place in band and zone against the leaned edges; the quorum "
          "met too warm; comfort point 24, margin -1 (the config's start, nothing learned)")

    # --- warnings: a stale sensor and a hold ---
    cfg, states, almanacs, _ = world(readings=(24.3, 23.8, 24.6), stale={"living2": 180},
                                     hold=True, now=now)
    client = setup(StubRest(states), cfg)
    main.state.store.current_almanac = lambda room_id, as_of=None: almanacs.get(room_id)
    j = client.get("/api/now").json()
    s2 = next(s for s in j["rooms"][0]["sensors"] if s["id"] == "living2")
    kinds = [w["kind"] for w in j["warnings"]]
    check(s2["stale"] and s2["position"] is None and s2["zone"] == "none"
          and re.fullmatch(r"\d\d:\d\d|\d{1,2} \w{3} \d\d:\d\d", s2["stale_since"])
          and "stale" in kinds and "hold" in kinds,
          f"a sensor last reported 3 h ago is stale (since {s2['stale_since']}), with no place "
          "in its band, and warned about with the hold")

    # --- Home Assistant out of reach, or no credentials ---
    client = setup(StubRest({}, fail=ConnectionError("refused")), cfg)
    j = client.get("/api/now").json()
    check(j["connected"] is False and j["rooms"] == [] and "can't be reached" in
          j["warnings"][0]["text"], "Home Assistant out of reach: a 200 saying so")
    main._ha_clients = original_clients
    import os
    saved = {k: os.environ.pop(k, None) for k in ("AC_HA_URL", "AC_HA_TOKEN")}
    j = TestClient(main.app).get("/api/now").json()
    check(j["connected"] is False and "AC_HA_URL" in j["warnings"][0]["text"],
          "no credentials: a 200 saying so")
    os.environ.update({k: v for k, v in saved.items() if v})

    # --- the vote endpoint ---
    rest = StubRest(states)
    client = setup(rest, cfg)
    r = client.post(f"/api/rooms/{ROOM}/vote", json={"direction": "too_hot"})
    check(r.status_code == 200 and rest.calls == [
        ("input_button", "press", {"entity_id": "input_button.ac_too_hot_main_room"})]
          and r.json() == {"pressed": "input_button.ac_too_hot_main_room",
                           "direction": "too_hot", "outcome": None},
          "a too hot vote presses input_button.ac_too_hot_main_room, and nothing else; with no "
          "runtime there is no outcome to wait for")
    rest.calls.clear()
    client.post(f"/api/rooms/{ROOM}/vote", json={"direction": "too_cold"})
    check(rest.calls == [("input_button", "press",
                          {"entity_id": "input_button.ac_too_cold_main_room"})],
          "a too cold vote presses input_button.ac_too_cold_main_room")
    rest.calls.clear()
    refused = [client.post("/api/rooms/nowhere/vote", json={"direction": "too_hot"}).status_code,
               client.post(f"/api/rooms/{ROOM}/vote", json={"direction": "warmer"}).status_code,
               client.post(f"/api/rooms/{ROOM}/vote", json={}).status_code]
    disabled = json.loads(json.dumps(cfg))
    disabled["rooms"][0]["enabled"] = False
    client = setup(rest, disabled)
    refused.append(client.post(f"/api/rooms/{ROOM}/vote",
                               json={"direction": "too_hot"}).status_code)
    check(refused == [404, 400, 400, 404] and rest.calls == [],
          "refused, pressing nothing: an unknown room (404), an unknown or missing direction "
          "(400), a disabled room (404)")

    # with the runtime live: HA's ac_person_vote for the press comes back
    runtime = SimpleNamespace(last_votes={ROOM: {"seq": 4, "accepted": True}}, tz=HK)

    def heard(domain, service, data):          # HA's vote automation reports the press
        runtime.last_votes[ROOM] = {"seq": 5, "direction": "too_hot", "accepted": False,
                                    "source": "button"}
    rest = StubRest(states, on_call=heard)
    client = setup(rest, cfg, runtime)
    j = client.post(f"/api/rooms/{ROOM}/vote", json={"direction": "too_hot"}).json()
    check(j["outcome"] == {"seq": 5, "direction": "too_hot", "accepted": False,
                           "source": "button"},
          "with the runtime live, the press's ac_person_vote comes back as the outcome (here "
          "ignored: one correction per episode), not the earlier one")
    saved_wait = main.VOTE_WAIT_SECONDS
    main.VOTE_WAIT_SECONDS = 0.3
    rest = StubRest(states)                      # HA never reports it
    client = setup(rest, cfg, SimpleNamespace(last_votes={}, tz=HK))
    j = client.post(f"/api/rooms/{ROOM}/vote", json={"direction": "too_cold"}).json()
    main.VOTE_WAIT_SECONDS = saved_wait
    check(j["outcome"] is None, "no report within the wait: no outcome (the UI says \"sent\")")

    # --- leak fixed ---
    cfg_leak, states, _, _ = world("leak", leak=True, now=now)
    rest = StubRest(states)
    client = setup(rest, cfg_leak)
    r = client.post(f"/api/rooms/{ROOM}/units/{UNIT}/leak-fixed")
    check(r.status_code == 200 and rest.calls == [
        ("input_boolean", "turn_on",
         {"entity_id": "input_boolean.ac_leak_confirmed_main_room_main_room_ac"})],
          "Leak fixed turns on input_boolean.ac_leak_confirmed_main_room_main_room_ac")
    rest.calls.clear()
    no_leak, _, _, _ = world(now=now)
    client = setup(rest, no_leak)
    codes = [client.post(f"/api/rooms/{ROOM}/units/{UNIT}/leak-fixed").status_code,
             client.post(f"/api/rooms/{ROOM}/units/nope/leak-fixed").status_code]
    check(codes == [400, 404] and rest.calls == [],
          "refused, touching nothing: a unit without leak detection (400), an unknown unit "
          "(404)")
    main.state.runtime = None
    print("\nALL NOW API CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main_())
