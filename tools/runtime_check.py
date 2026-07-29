"""
Adaptive Climate - runtime offline check.

Drives app.runtime.Runtime against stub REST/WebSocket objects and a real
Store (no Home Assistant, no network). Verifies:
  * startup catch-up fires a crossover per room (automation.trigger on the
    room's scene automation, resolved by config-id -> entity_id)
  * a provisional almanac is seeded and pushed on first crossover
  * a heartbeat samples every sensor and unit and records it
  * reactive detection records a genuine user setpoint change, raising hold
  * an automation-caused change (context.parent_id set) is ignored
  * a change during the guard window is ignored

    PYTHONPATH=. python tools/runtime_check.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime import Runtime                                  # noqa: E402
from app.store import Store                                      # noqa: E402
from app.generator import SECTIONS                               # noqa: E402

SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "storage.schema.sql"
CONFIG = json.loads((Path(__file__).resolve().parent.parent /
                     "examples" / "config.example.json").read_text())


def _build_states():
    """A plausible /api/states payload: scene automations (with attributes.id),
    climate units, temperature sensors, and our helpers."""
    states = []
    for room in CONFIG["rooms"]:
        r = room["id"]
        for sec in SECTIONS:
            states.append({
                "entity_id": f"automation.ac_{r}_{sec}",
                "state": "on",
                "attributes": {"id": f"ac_scene_{r}_{sec}"}})
        states.append({"entity_id": f"input_boolean.ac_active_{r}", "state": "off", "attributes": {}})
        states.append({"entity_id": f"input_boolean.ac_hold_{r}", "state": "off", "attributes": {}})
        states.append({"entity_id": f"input_select.ac_scene_{r}", "state": "day", "attributes": {}})
        for u in room["units"]:
            states.append({
                "entity_id": u["entity_id"], "state": "cool",
                "attributes": {"temperature": 22.0, "current_temperature": 23.1,
                               "fan_mode": "low", "hvac_modes": ["cool", "fan_only", "dry", "off"],
                               "fan_modes": ["low", "medium"], "min_temp": 16, "max_temp": 30,
                               "target_temp_step": 1}})
        for i, s in enumerate(room["sensors"]):
            states.append({"entity_id": s["entity_id"], "state": str(24.0 + i * 0.5),
                           "attributes": {"unit_of_measurement": "\u00b0C"}})
    return states


class StubRest:
    def __init__(self, states):
        self._states = states
        self.service_calls = []
        self.set_states = []
    async def config(self):
        return {"time_zone": "Asia/Hong_Kong", "version": "2026.7.4",
                "unit_system": {"temperature": "\u00b0C"}}
    async def states(self):
        return self._states
    async def call_service(self, domain, service, data=None):
        self.service_calls.append((domain, service, data or {}))
    async def set_state(self, entity_id, state, attributes=None):
        self.set_states.append((entity_id, state, attributes or {}))
    async def close(self):
        pass


class StubWS:
    def __init__(self):
        self.callback = None
    async def connect(self):
        pass
    async def subscribe_states(self, callback):
        self.callback = callback
        return 1
    async def close(self):
        pass


def _state_changed(entity_id, before, after, parent_id=None, state="cool"):
    return {"event_type": "state_changed",
            "context": {"id": "c1", "parent_id": parent_id, "user_id": "u1"},
            "data": {"entity_id": entity_id,
                     "old_state": {"state": state, "attributes": {"temperature": before}},
                     "new_state": {"state": state, "attributes": {"temperature": after},
                                   "context": {"id": "c1", "parent_id": parent_id, "user_id": "u1"}}}}


async def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp, SCHEMA)
    rest = StubRest(_build_states())
    ws = StubWS()
    # tiny reactive window so the flush completes fast
    CONFIG.setdefault("system", {})["reactive_window_seconds"] = 1
    rt = Runtime(CONFIG, store, rest, ws, tmp)

    await rt.start()

    # --- startup catch-up: one automation.trigger per room ---
    triggers = [c for c in rest.service_calls if c[:2] == ("automation", "trigger")]
    trig_entities = {c[2]["entity_id"] for c in triggers}
    assert len(rt.rooms) == 3
    for r in ("main_room", "main_bedroom", "baby_room"):
        assert any(e.startswith(f"automation.ac_{r}_") for e in trig_entities), r
    print(f"startup: fired catch-up crossover for all 3 rooms ({len(triggers)} triggers) - OK")

    # --- provisional almanac seeded + pushed ---
    from datetime import date
    alm = store.current_almanac("main_room", as_of=date.today())
    assert alm["sections"], "no provisional almanac seeded"
    seeded_section = next(iter(alm["sections"]))
    assert alm["sections"][seeded_section]["state"] == "provisional"
    pushes = [s for s in rest.set_states if s[0] == "sensor.ac_almanac_main_room"]
    assert pushes, "almanac not pushed to HA"
    print(f"startup: provisional almanac seeded ({seeded_section}) and pushed to HA - OK")

    # --- heartbeat sampling ---
    rs = rt.rooms["main_room"]
    await rt._do_heartbeat(rs)
    c = store._conn
    hb = c.execute("SELECT COUNT(*) FROM heartbeat WHERE room_id='main_room'").fetchone()[0]
    hbs = c.execute("SELECT COUNT(*) FROM heartbeat_sensor").fetchone()[0]
    hbu = c.execute("SELECT setpoint, is_on, ac_state FROM heartbeat_unit").fetchone()
    assert hb == 1 and hbs == 3, (hb, hbs)
    assert hbu[0] == 22.0 and hbu[1] == 1 and hbu[2] == "normal", tuple(hbu)
    print(f"heartbeat: recorded 1 heartbeat, 3 sensors, unit setpoint 22 state 'normal' - OK")

    # --- reactive: genuine user change ---
    rt._cache["input_boolean.ac_active_main_room"] = "off"
    rt._on_state_change(_state_changed("climate.lounge_lounge", 22.0, 24.0))
    assert rt.rooms["main_room"].reactive_units, "user change not buffered"
    await asyncio.sleep(0.05)  # let the scheduled hold-raise task run
    hold_calls = [c for c in rest.service_calls
                  if c[:2] == ("input_boolean", "turn_on")
                  and c[2].get("entity_id") == "input_boolean.ac_hold_main_room"]
    assert hold_calls, "hold not raised on reaction"
    await asyncio.sleep(1.5)  # let the flush window elapse
    rx = c.execute("SELECT COUNT(*) FROM reactive WHERE room_id='main_room'").fetchone()[0]
    rxs = c.execute("SELECT COUNT(*) FROM reactive_sensor").fetchone()[0]
    assert rx == 1, rx
    assert rxs == 3, rxs   # all sensors snapshotted at reaction time
    print(f"reactive: user setpoint change recorded, hold raised, 3 sensors snapshotted - OK")

    # --- reactive: automation-caused change is ignored ---
    before_rx = c.execute("SELECT COUNT(*) FROM reactive").fetchone()[0]
    rt._on_state_change(_state_changed("climate.baby_room_ac", 24.0, 27.0, parent_id="auto-ctx"))
    assert not rt.rooms["baby_room"].reactive_units, "automation change wrongly buffered"
    # --- reactive: change during guard window is ignored ---
    rt._cache["input_boolean.ac_active_main_bedroom"] = "on"
    rt._on_state_change(_state_changed("climate.home_s_device_2_home_s_device_2", 22.0, 25.0))
    assert not rt.rooms["main_bedroom"].reactive_units, "guarded change wrongly buffered"
    print("reactive: automation-caused and guard-window changes correctly ignored - OK")

    await rt.stop()

    # --- restart with almanac already seeded + select already on-section:
    #     catch-up must NOT re-fire the scene (no gratuitous beep) ---
    seeded_section = seeded_section  # from above
    rest2 = StubRest(_build_states())
    # HA now reports the scene select already sitting on the active section
    active_now, _ = __import__("app.scheduler", fromlist=["active_section_at"]).active_section_at(
        rt.rooms["main_room"].profile, rt._now()[3], rt.tz)
    for s in rest2._states:
        if s["entity_id"].startswith("input_select.ac_scene_"):
            s["state"] = active_now
    ws2 = StubWS()
    rt2 = Runtime(CONFIG, store, rest2, ws2, tmp)   # same store: almanac persists
    await rt2.start()
    refires = [c for c in rest2.service_calls if c[:2] == ("automation", "trigger")]
    assert refires == [], f"restart re-fired scenes (would beep): {refires}"
    print("restart: almanac present + already in section -> no scene re-fire (no beep) - OK")
    await rt2.stop()
    store.close()
    print("\nALL RUNTIME CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
