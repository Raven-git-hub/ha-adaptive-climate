"""
Adaptive Climate - runtime offline check.

Drives app.runtime.Runtime against stub REST/WebSocket objects and a real
Store (no Home Assistant, no network). Verifies:
  * startup catch-up fires a crossover per room (automation.trigger on the
    room's scene automation, resolved by config-id -> entity_id)
  * no provisional almanac is seeded at crossover
  * a heartbeat samples every sensor and unit and records it, and the first
    heartbeat in an unseeded section (or one that can seed a unit the
    almanac lacks) replays and pushes the almanac
  * reactive detection records a genuine user setpoint change, raising hold,
    replays the almanac at once and again after the comfort window
  * an automation-caused change (context.parent_id set) is ignored
  * a change during the guard window is ignored
  * several changes in one reactive window record the first setpoint_before
  * a sleep section run crossing midnight closes against its start date
  * on restart, comfort-window replays are re-armed for recent setpoint-moving
    reactions only

    PYTHONPATH=. python tools/runtime_check.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.runtime as runtime_mod                               # noqa: E402
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
    def on_reconnect(self, cb):
        pass
    def on_status(self, cb):
        pass
    async def connect(self):
        pass
    async def subscribe_states(self, callback):
        self.callback = callback
        return 1
    async def close(self):
        pass


def replays(store, room_id: str, reason: str) -> list[dict]:
    return [e for e in store.recent_events(limit=1000, room_id=room_id, category="analysis")
            if (e.get("detail") or {}).get("reason") == reason]


async def eventually(cond, what: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


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
    # tiny reactive window so the flush completes fast, and a comfort-window
    # replay one second after each reaction instead of 65 minutes
    CONFIG.setdefault("system", {})["reactive_window_seconds"] = 1
    runtime_mod.COMFORT_REPLAY_DELAY = timedelta(seconds=1)
    rt = Runtime(CONFIG, store, rest, ws, tmp)

    await rt.start()

    # --- startup catch-up: one automation.trigger per room ---
    triggers = [c for c in rest.service_calls if c[:2] == ("automation", "trigger")]
    trig_entities = {c[2]["entity_id"] for c in triggers}
    assert len(rt.rooms) == 3
    for r in ("main_room", "main_bedroom", "baby_room"):
        assert any(e.startswith(f"automation.ac_{r}_") for e in trig_entities), r
    print(f"startup: fired catch-up crossover for all 3 rooms ({len(triggers)} triggers) - OK")

    # --- no provisional seeding at crossover any more ---
    for r in rt.rooms:
        assert store.current_almanac(r, as_of=rt._today())["sections"] == {}, r
    assert not store.recent_events(category="analysis")
    print("startup: no provisional almanac seeded at crossover; nothing replayed yet - OK")

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

    # --- the first heartbeat in an unseeded section replays and pushes ---
    sec = rs.section
    alm = store.current_almanac("main_room", as_of=rt._today())["sections"]
    assert list(alm) == [sec], list(alm)
    assert alm[sec]["status"] == "provisional"
    assert alm[sec]["units"]["main_room_ac"]["setpoint_applied"] == 22.0
    assert set(alm[sec]["sensors"]) == {"living1", "living2", "living3"}
    assert len(replays(store, "main_room", "first heartbeat in section")) == 1
    pushed = [s for s in rest.set_states if s[0] == "sensor.ac_almanac_main_room"][-1]
    assert pushed[2]["sections"][sec]["units"]["main_room_ac"]["setpoint_applied"] == 22.0
    await asyncio.sleep(1.05)                    # heartbeats are unique per second
    await rt._do_heartbeat(rs)                   # nothing new to seed: no replay
    assert len(store.recent_events(room_id="main_room", category="analysis")) == 1
    print(f"heartbeat: the first one in '{sec}' replayed and pushed a provisional almanac "
          f"(applied 22); the next one, with nothing new to seed, did not - OK")

    bed_rs = rt.rooms["main_bedroom"]
    bed_unit = next(s for s in rest._states
                    if s["entity_id"] == "climate.home_s_device_2_home_s_device_2")
    bed_unit["state"] = "off"
    await rt._do_heartbeat(bed_rs)
    bed_alm = store.current_almanac("main_bedroom", as_of=rt._today())["sections"]
    assert bed_alm[bed_rs.section]["units"] == {}, bed_alm
    bed_unit["state"] = "cool"
    await asyncio.sleep(1.05)
    await rt._do_heartbeat(bed_rs)
    bed_alm = store.current_almanac("main_bedroom", as_of=rt._today())["sections"]
    assert bed_alm[bed_rs.section]["units"]["main_bedroom_ac"]["setpoint_applied"] == 22.0
    assert len(replays(store, "main_bedroom", "new seed in section")) == 1
    print("heartbeat: a unit off at its section's first heartbeat is seeded by the replay "
          "the first heartbeat with it on triggers - OK")

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
    assert len(replays(store, "main_room", "reaction")) == 1
    alm = store.current_almanac("main_room", as_of=rt._today())["sections"][sec]
    assert alm["status"] == "learned", alm
    assert alm["units"]["main_room_ac"]["setpoint"] == 23.0          # 22 + 0.5 x (24 - 22)
    assert alm["units"]["main_room_ac"]["setpoint_applied"] == 23.0
    print("reactive: replayed at once - learned, setpoint 22 -> 23 and applied 23 - OK")
    await eventually(lambda: replays(store, "main_room", "comfort window"),
                     "the comfort-window replay")
    print("reactive: replayed again once the comfort window had closed - OK")

    # --- reactive: automation-caused change is ignored ---
    before_rx = c.execute("SELECT COUNT(*) FROM reactive").fetchone()[0]
    rt._on_state_change(_state_changed("climate.baby_room_ac", 24.0, 27.0, parent_id="auto-ctx"))
    assert not rt.rooms["baby_room"].reactive_units, "automation change wrongly buffered"
    # --- reactive: change during guard window is ignored ---
    rt._cache["input_boolean.ac_active_main_bedroom"] = "on"
    rt._on_state_change(_state_changed("climate.home_s_device_2_home_s_device_2", 22.0, 25.0))
    assert not rt.rooms["main_bedroom"].reactive_units, "guarded change wrongly buffered"
    print("reactive: automation-caused and guard-window changes correctly ignored - OK")

    # --- reactive: three changes in one window are one record that keeps
    #     the FIRST setpoint_before and the LAST setpoint_after ---
    for before, after in ((22.0, 23.0), (23.0, 24.0), (24.0, 25.0)):
        rt._on_state_change(_state_changed("climate.lounge_lounge", before, after))
    buffered = rt.rooms["main_room"].reactive_units["main_room_ac"]
    assert (buffered.setpoint_before, buffered.setpoint_after) == (22.0, 25.0), buffered
    await asyncio.sleep(1.5)  # let the flush window elapse
    rows = c.execute(
        "SELECT ru.setpoint_before, ru.setpoint_after FROM reactive r "
        "JOIN reactive_unit ru ON ru.reactive_id = r.id "
        "WHERE r.room_id='main_room' ORDER BY r.id").fetchall()
    assert len(rows) == 2, [tuple(r) for r in rows]   # the earlier reaction + this window
    assert tuple(rows[-1]) == (22.0, 25.0), tuple(rows[-1])
    print("reactive: 22->23->24->25 in one window recorded once as before 22, after 25 - OK")

    # --- section runs: a sleep run crossing midnight, with a restart inside it ---
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo
    hk, utc = ZoneInfo("Asia/Hong_Kong"), ZoneInfo("UTC")
    real_now = rt._now

    def at(*args):
        t = dt(*args, tzinfo=hk)
        return lambda: (t.isoformat(timespec="seconds"),
                        t.astimezone(utc).isoformat(timespec="seconds"), t.date().isoformat(), t)
    bed = rt.rooms["main_bedroom"]
    sleep_start = dt(2026, 8, 3, 22, 0, tzinfo=hk)
    rt._now = at(2026, 8, 3, 22, 0, 2)
    await rt._crossover(bed, "sleep", sleep_start, catch_up=False)
    rt._now = at(2026, 8, 4, 1, 0, 0)       # container restarts at 01:00, still in sleep
    await rt._crossover(bed, "sleep", sleep_start, catch_up=True)
    rt._now = at(2026, 8, 4, 5, 30, 1)
    await rt._crossover(bed, "sunrise", dt(2026, 8, 4, 5, 30, tzinfo=hk), catch_up=False)
    rt._now = real_now
    runs = [tuple(r) for r in c.execute(
        "SELECT local_date, section, ended_at FROM section_run "
        "WHERE room_id='main_bedroom' AND local_date LIKE '2026-08-%' ORDER BY id")]
    assert runs == [("2026-08-03", "sleep", "2026-08-04T05:30:01+08:00"),
                    ("2026-08-04", "sunrise", None)], runs
    print("section runs: sleep from 22:00 survives a 01:00 restart as one run and is closed "
          "by the 05:30 sunrise against its start date - OK")

    await rt.stop()

    # --- restart: re-arm comfort replays for recent setpoint-moving reactions ---
    runtime_mod.COMFORT_REPLAY_DELAY = timedelta(seconds=3)
    hk = ZoneInfo("Asia/Hong_Kong")

    def record(seconds_ago: float, kind=None, after: float = 23.0) -> None:
        when = datetime.now(hk) - timedelta(seconds=seconds_ago)
        store.record_reactive(
            "baby_room", when.isoformat(timespec="seconds"),
            when.astimezone(timezone.utc).isoformat(timespec="seconds"),
            when.date().isoformat(), "day", 120,
            units={"baby_room_ac": runtime_mod.ReactiveUnitSample(22.0, after, after != 22.0)},
            sensors={"baby": 24.0}, kind=kind)
    record(1)                       # setpoint-moving, inside the window: re-armed
    record(10)                      # too old: its window is already handled
    record(2, kind="manual", after=22.0)   # no setpoint change: no comfort window

    # --- restart with the select already on-section: catch-up must NOT
    #     re-fire the scene (no gratuitous beep) ---
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
    print("restart: already in section -> no scene re-fire (no beep) - OK")
    # (main_room's own reactions above are also under 3 s old, so re-armed too)
    baby = [t.get_name() for t in rt2._followups if t.get_name() == "ac-comfort-baby_room"]
    assert len(baby) == 1, [t.get_name() for t in rt2._followups]
    await eventually(lambda: replays(store, "baby_room", "comfort window"),
                     "the re-armed comfort-window replay")
    await asyncio.sleep(0.5)
    assert len(replays(store, "baby_room", "comfort window")) == 1
    print("restart: one comfort replay re-armed (the recent setpoint change, not the old "
          "one or the manual one), and it ran - OK")
    await rt2.stop()
    store.close()
    print("\nALL RUNTIME CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
