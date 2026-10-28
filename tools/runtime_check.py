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
  * reactive detection records a genuine user setpoint change, never raising
    the hold, and replays the almanac at once and again after the comfort
    window
  * an automation-caused change (context.parent_id set) is ignored
  * a change during the guard window is ignored
  * a late echo of our own command (what the unit's state helper implies)
    is ignored, while a setpoint change made while resting is recorded
  * a heartbeat records the unit's control_state and control_offset
  * startup replays every room and pushes its almanac before any
    crossover: started 3 minutes before 22:00 with an empty almanac but
    history in the store, the Sleep crossover finds setpoint_applied
  * a sensor Home Assistant last reported longer ago than
    sensor_stale_minutes reads as unavailable in heartbeats and reaction
    snapshots, with one warning when it goes stale (not repeated after a
    restart) and one event when it reports again
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.runtime as runtime_mod                               # noqa: E402
from app.runtime import Runtime                                  # noqa: E402
from app.store import Store, UnitSample                          # noqa: E402
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
        # not any real section, so the startup catch-up always re-fires,
        # whatever the time of day the check runs at
        states.append({"entity_id": f"input_select.ac_scene_{r}", "state": "unknown",
                       "attributes": {}})
        if r == "main_bedroom":     # control helpers on one room; the others have none yet
            for u in room["units"]:
                states += [
                    {"entity_id": f"input_select.ac_state_{r}_{u['id']}", "state": "rest",
                     "attributes": {}},
                    {"entity_id": f"input_number.ac_offset_{r}_{u['id']}", "state": "2.0",
                     "attributes": {}},
                    {"entity_id": f"input_number.ac_base_{r}_{u['id']}", "state": "0.0",
                     "attributes": {}}]
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
        self.log = []               # service calls and state pushes, in order
    async def config(self):
        return {"time_zone": "Asia/Hong_Kong", "version": "2026.7.4",
                "unit_system": {"temperature": "\u00b0C"}}
    async def states(self):
        return self._states
    async def call_service(self, domain, service, data=None):
        self.service_calls.append((domain, service, data or {}))
        self.log.append(("service", domain, service, data or {}))
    async def set_state(self, entity_id, state, attributes=None):
        self.set_states.append((entity_id, state, attributes or {}))
        self.log.append(("set_state", entity_id, attributes or {}))
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


def _state_changed(entity_id, before, after, parent_id=None, state="cool",
                   new_state=None, fan="low", new_fan=None):
    """A state_changed event: setpoint before -> after, hvac state (mode)
    state -> new_state, fan -> new_fan; unchanged unless given."""
    return {"event_type": "state_changed",
            "context": {"id": "c1", "parent_id": parent_id, "user_id": "u1"},
            "data": {"entity_id": entity_id,
                     "old_state": {"state": state,
                                   "attributes": {"temperature": before, "fan_mode": fan}},
                     "new_state": {"state": new_state or state,
                                   "attributes": {"temperature": after,
                                                  "fan_mode": new_fan or fan},
                                   "context": {"id": "c1", "parent_id": parent_id, "user_id": "u1"}}}}


def last_reaction(c, room_id: str) -> tuple:
    """(room kind, unit kind, power before, power after, setpoint before,
    setpoint after, setpoint changed) of the room's latest reaction."""
    return tuple(c.execute(
        "SELECT r.kind, ru.kind, ru.power_before, ru.power_after, ru.setpoint_before, "
        "ru.setpoint_after, ru.changed FROM reactive r "
        "JOIN reactive_unit ru ON ru.reactive_id = r.id WHERE r.room_id = ? "
        "ORDER BY r.id DESC LIMIT 1", (room_id,)).fetchone())


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
        assert len(replays(store, r, "startup")) == 1, r
    assert len(store.recent_events(category="analysis")) == 3
    print("startup: each room replayed once (no history yet: an empty almanac); no "
          "provisional almanac seeded at crossover - OK")

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
    assert len([e for e in store.recent_events(room_id="main_room", category="analysis")
                if (e.get("detail") or {}).get("reason") != "startup"]) == 1
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
    controls = c.execute(
        "SELECT h.room_id, hu.control_state, hu.control_offset FROM heartbeat_unit hu "
        "JOIN heartbeat h ON h.id = hu.heartbeat_id ORDER BY h.id, hu.unit_id").fetchall()
    assert [tuple(x) for x in controls] == [("main_room", None, None),
                                            ("main_room", None, None),
                                            ("main_bedroom", "rest", 2),
                                            ("main_bedroom", "rest", 2)], controls
    print("heartbeat: control_state and control_offset recorded from the unit's helpers "
          "(NULL where a room has none yet) - OK")

    # --- reactive: genuine user change ---
    rt._cache["input_boolean.ac_active_main_room"] = "off"
    rt._on_state_change(_state_changed("climate.lounge_lounge", 22.0, 24.0))
    assert rt.rooms["main_room"].reactive_units, "user change not buffered"
    await asyncio.sleep(0.05)  # let the scheduled hold-raise task run
    await asyncio.sleep(1.5)  # let the flush window elapse
    hold_calls = [c for c in rest.service_calls
                  if c[2].get("entity_id", "").startswith("input_boolean.ac_hold_")]
    assert not hold_calls, f"the hold was raised on a reaction: {hold_calls}"
    rx = c.execute("SELECT COUNT(*) FROM reactive WHERE room_id='main_room'").fetchone()[0]
    rxs = c.execute("SELECT COUNT(*) FROM reactive_sensor").fetchone()[0]
    assert rx == 1, rx
    assert rxs == 3, rxs   # all sensors snapshotted at reaction time
    print("reactive: user setpoint change recorded, hold not raised, 3 sensors snapshotted - OK")
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

    # --- phase 2: on / off / manual reactions ---
    lounge = "climate.lounge_lounge"

    def n_reactions(room_id: str) -> int:
        return c.execute("SELECT COUNT(*) FROM reactive WHERE room_id=?", (room_id,)).fetchone()[0]

    def holds(room_id: str) -> int:
        return sum(1 for call in rest.service_calls if call[:2] == ("input_boolean", "turn_on")
                   and call[2].get("entity_id") == f"input_boolean.ac_hold_{room_id}")

    async def window(events, room_id: str = "main_room") -> tuple:
        """Feed one burst of events, wait for its single reaction, return it."""
        count, held = n_reactions(room_id), holds(room_id)
        for e in events:
            rt._on_state_change(e)
        await eventually(lambda: n_reactions(room_id) == count + 1, "the reaction to flush")
        assert holds(room_id) == held, "the hold was raised"
        await asyncio.sleep(0.2)                 # it stays one reaction
        assert n_reactions(room_id) == count + 1
        return last_reaction(c, room_id)

    off = await window([_state_changed(lounge, 22.0, 22.0, state="cool", new_state="off")])
    assert off == ("off", "off", 1, 0, 22.0, 22.0, 0), off
    on = await window([_state_changed(lounge, 22.0, 22.0, state="off", new_state="cool")])
    assert on == ("on", "on", 0, 1, 22.0, 22.0, 0), on
    print("reactive: a person turning the unit off and back on gives an off then an on "
          "reaction with power 1->0 and 0->1, neither raising the hold - OK")

    count = n_reactions("baby_room")
    rt._on_state_change(_state_changed("climate.baby_room_ac", 22.0, 22.0, parent_id="auto-ctx",
                                       state="cool", new_state="off"))
    rt._on_state_change(_state_changed("climate.home_s_device_2_home_s_device_2", 22.0, 22.0,
                                       state="cool", new_state="off"))   # bedroom guard is on
    assert not rt.rooms["baby_room"].reactive_units
    assert not rt.rooms["main_bedroom"].reactive_units
    await asyncio.sleep(1.5)
    assert n_reactions("baby_room") == count
    print("reactive: an off carrying context.parent_id, or during the guard, is ignored - OK")

    flicker = await window([
        _state_changed(lounge, 22.0, 22.0, state="off", new_state="cool"),
        _state_changed(lounge, 22.0, 22.0, state="cool", new_state="off"),
        _state_changed(lounge, 22.0, 22.0, state="off", new_state="cool")])
    assert flicker[:4] == ("on", "on", 0, 1), flicker
    toggle = await window([
        _state_changed(lounge, 22.0, 22.0, state="cool", new_state="off"),
        _state_changed(lounge, 22.0, 22.0, state="off", new_state="cool")])
    assert toggle[:4] == ("manual", "manual", 1, 1), toggle
    back = await window([_state_changed(lounge, 22.0, 23.0), _state_changed(lounge, 23.0, 22.0)])
    assert back == ("setpoint", "setpoint", 1, 1, 22.0, 22.0, 1), back
    print("reactive: off->on->off->on in one window is one `on` (first off, last on); "
          "on->off->on is one `manual` (power unchanged); 22->23->22 is one `setpoint` - OK")

    comfort_before = len(replays(store, "main_room", "comfort window"))
    on_set = await window([
        _state_changed(lounge, 22.0, 22.0, state="off", new_state="cool"),
        _state_changed(lounge, 22.0, 24.0)])
    assert on_set == ("on", "on", 0, 1, 22.0, 24.0, 1), on_set
    await eventually(lambda: len(replays(store, "main_room", "comfort window")) > comfort_before,
                     "the comfort replay an on-with-setpoint schedules")
    print("reactive: on then a setpoint change in one window is one `on` reaction, "
          "setpoint-moving (22 -> 24, comfort replay scheduled) - OK")

    fan = await window([_state_changed(lounge, 22.0, 22.0, fan="low", new_fan="medium")])
    assert fan == ("manual", "manual", 1, 1, 22.0, 22.0, 0), fan
    mode = await window([_state_changed(lounge, 22.0, 22.0, state="cool",
                                        new_state="fan_only")])
    assert mode[:2] == ("manual", "manual"), mode
    events = store.recent_events(room_id="main_room", category="reactive", limit=1)
    assert "manual: logged, not learned" in events[0]["message"], events[0]
    assert events[0]["detail"]["units"]["main_room_ac"]["mode_after"] == "fan_only"
    print("reactive: a fan change, or a mode change, while on is `manual`, logged with "
          "the modes - OK")

    count, controls = n_reactions("main_room"), len(store.recent_events(category="control"))
    rt._on_state_change(_state_changed(lounge, 22.0, 22.0, state="cool", new_state="unavailable"))
    rt._on_state_change(_state_changed(lounge, 22.0, 22.0, state="unavailable", new_state="off"))
    rt._on_state_change(_state_changed(lounge, 22.0, 22.0, state="off", new_state="unknown"))
    rt._on_state_change(_state_changed(lounge, 22.0, 22.0, state="unknown", new_state="off"))
    await asyncio.sleep(1.5)
    assert n_reactions("main_room") == count and not rt.rooms["main_room"].reactive_units
    control = store.recent_events(category="control")
    assert len(control) == controls + 1, control
    assert "came back from unavailable changed" in control[0]["message"], control[0]
    assert control[0]["detail"]["before"]["mode"] == "cool"
    assert control[0]["detail"]["after"]["mode"] == "off"
    print("drop-out: cooling -> unavailable -> off is no reaction and one control event; "
          "off -> unknown -> off, unchanged, logs nothing - OK")

    # --- phase 3b: our own command arriving after the guard is down ---
    bedroom, bed_helper = "climate.home_s_device_2_home_s_device_2", "main_bedroom_main_bedroom_ac"
    rt._cache["input_boolean.ac_active_main_bedroom"] = "off"
    count = n_reactions("main_bedroom")
    # state rest implies off: the unit going off at the same setpoint is our rest
    rt._on_state_change(_state_changed(bedroom, 22.0, 22.0, state="cool", new_state="off"))
    assert not rt.rooms["main_bedroom"].reactive_units, "the echo of our rest was buffered"
    # state cool at base 25, offset 0 implies cool at 25
    rt._cache[f"input_select.ac_state_{bed_helper}"] = "cool"
    rt._cache[f"input_number.ac_base_{bed_helper}"] = "25.0"
    rt._cache[f"input_number.ac_offset_{bed_helper}"] = "0.0"
    rt._on_state_change(_state_changed(bedroom, 24.0, 25.0, state="off", new_state="cool"))
    assert not rt.rooms["main_bedroom"].reactive_units, "the echo of our cool was buffered"
    await asyncio.sleep(1.5)
    assert n_reactions("main_bedroom") == count
    print("reactive: a late echo of our own rest (off) and cool (cool at base + offset) is "
          "not a reaction - OK")
    # ...but a person's setpoint change while the unit rests is (decision 50)
    rt._cache[f"input_select.ac_state_{bed_helper}"] = "rest"
    resting = await window([_state_changed(bedroom, 22.0, 24.0, state="off", new_state="off")],
                           "main_bedroom")
    assert resting == ("setpoint", "setpoint", 0, 0, 22.0, 24.0, 1), resting
    # ...and so is a setpoint a person picks away from our command
    rt._cache[f"input_select.ac_state_{bed_helper}"] = "cool"
    away = await window([_state_changed(bedroom, 25.0, 23.0)], "main_bedroom")
    assert away == ("setpoint", "setpoint", 1, 1, 25.0, 23.0, 1), away
    print("reactive: a setpoint change while resting, and one away from our command, are "
          "a person's - OK")

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
    # One clock reading for all three, so 1 s and 2 s ago are always
    # different seconds (reactions are unique per room and second).
    now = datetime.now(hk)

    def record(seconds_ago: float, kind=None, after: float = 23.0) -> None:
        when = now - timedelta(seconds=seconds_ago)
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
    await check_startup_race()
    await check_stale_sensor()
    print("\nALL RUNTIME CHECKS PASSED")
    return 0


async def check_startup_race() -> None:
    """Migration leaves the almanac empty; the history is in the store.
    Started at 21:57, three minutes before the 22:00 Sleep boundary, the
    Sleep crossover must find setpoint_applied in what HA holds: startup
    replays and pushes every room before any crossover can fire."""
    hk, utc = ZoneInfo("Asia/Hong_Kong"), ZoneInfo("UTC")
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp, SCHEMA)
    for room in CONFIG["rooms"]:                  # last night's Sleep, every unit on at 23
        for minute in (10, 20, 30):
            t = datetime(2026, 8, 2, 22, minute, tzinfo=hk)
            store.record_heartbeat(
                room["id"], t.isoformat(timespec="seconds"),
                t.astimezone(utc).isoformat(timespec="seconds"), t.date().isoformat(), "sleep",
                sensors={s["id"]: 24.0 for s in room["sensors"]},
                units={u["id"]: UnitSample(True, "cool", "low", 23.0, 24.0, "normal")
                       for u in room["units"]})
    for room in CONFIG["rooms"]:
        assert store.current_almanac(room["id"], as_of=date(2026, 8, 3))["sections"] == {}
    states = _build_states()
    for s in states:
        if s["entity_id"].startswith("input_select.ac_scene_"):
            s["state"] = "night"                  # HA is in Night: no catch-up re-fire
    rest, ws = StubRest(states), StubWS()
    rt = Runtime(CONFIG, store, rest, ws, tmp)
    clock = {"t": datetime(2026, 8, 3, 21, 57, 0, tzinfo=hk)}
    rt._now = lambda: (clock["t"].isoformat(timespec="seconds"),
                       clock["t"].astimezone(utc).isoformat(timespec="seconds"),
                       clock["t"].date().isoformat(), clock["t"])

    def fired(room_id: str) -> int | None:
        entity = f"automation.ac_{room_id}_sleep"
        return next((i for i, e in enumerate(rest.log)
                     if e[:3] == ("service", "automation", "trigger")
                     and e[3].get("entity_id") == entity), None)

    saved = runtime_mod.SCHEDULER_TICK_SECONDS
    runtime_mod.SCHEDULER_TICK_SECONDS = 0.05
    try:
        await rt.start()
        assert not any(e[:3] == ("service", "automation", "trigger") for e in rest.log)
        clock["t"] = datetime(2026, 8, 3, 22, 0, 15, tzinfo=hk)     # the scheduler's next tick
        await eventually(lambda: all(fired(r["id"]) is not None for r in CONFIG["rooms"]),
                         "the 22:00 Sleep crossovers")
    finally:
        runtime_mod.SCHEDULER_TICK_SECONDS = saved
        await rt.stop()
    for room in CONFIG["rooms"]:
        r = room["id"]
        pushed = [e[2] for e in rest.log[:fired(r)]
                  if e[0] == "set_state" and e[1] == f"sensor.ac_almanac_{r}"]
        units = ((pushed[-1]["sections"] if pushed else {}).get("sleep") or {}).get("units", {})
        assert all((units.get(u["id"]) or {}).get("setpoint_applied") == 23.0
                   for u in room["units"]), \
            f"{r}: the Sleep crossover fired with HA holding sleep units {units}"
    store.close()
    print("startup race: started 21:57 with an empty almanac and history in the store, each "
          "room's 22:00 Sleep crossover fired after its almanac reached HA with "
          "setpoint_applied 23 - OK")

async def check_stale_sensor() -> None:
    """A sensor put away keeps its last value in HA without going
    unavailable; only last_reported shows it. Past sensor_stale_minutes it
    must read as unavailable everywhere the runtime samples it."""
    hk, utc = ZoneInfo("Asia/Hong_Kong"), ZoneInfo("UTC")
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp, SCHEMA)
    states = _build_states()
    clock = {"t": datetime(2026, 8, 3, 12, 0, 0, tzinfo=hk)}
    put_away = datetime(2026, 7, 31, 12, 3, 0, tzinfo=hk)     # three days before
    wall = next(s for s in states if s["entity_id"] == "sensor.living_room_temp")
    for st in states:
        st["last_reported"] = clock["t"].isoformat()
    wall["last_reported"] = put_away.astimezone(utc).isoformat()

    def runtime() -> Runtime:
        rt = Runtime(CONFIG, store, StubRest(states), StubWS(), tmp)
        rt._now = lambda: (clock["t"].isoformat(timespec="seconds"),
                           clock["t"].astimezone(utc).isoformat(timespec="seconds"),
                           clock["t"].date().isoformat(), clock["t"])
        return rt

    def readings() -> list:
        return [r[0] for r in store._conn.execute(
            "SELECT hs.temperature FROM heartbeat_sensor hs JOIN heartbeat h ON h.id = "
            "hs.heartbeat_id WHERE h.room_id = 'main_room' AND hs.sensor_id = 'living1' "
            "ORDER BY h.id")]

    def stale_events() -> list:
        return [(e["severity"], e["detail"]["stale"]) for e in reversed(
            store.recent_events(category="validation", room_id="main_room"))
            if "stale" in (e.get("detail") or {})]

    async def beat(rt: Runtime) -> None:
        clock["t"] += timedelta(minutes=10)
        for st in states:
            if st is not wall:
                st["last_reported"] = clock["t"].isoformat()
        await rt._do_heartbeat(rt.rooms["main_room"])

    rt = runtime()
    await rt.start()
    await beat(rt)
    await beat(rt)
    assert readings() == [None, None], readings()
    assert stale_events() == [("warning", True)], stale_events()
    message = store.recent_events(category="validation", room_id="main_room")[0]["message"]
    assert "last reported 3 d 0 h ago, at 2026-07-31 12:03" in message, message
    await rt.stop()

    rt = runtime()                                # a restart while it is still stale
    await rt.start()
    await beat(rt)
    assert readings() == [None, None, None] and stale_events() == [("warning", True)]
    rt._on_state_change(_state_changed("climate.lounge_lounge", 22.0, 23.0))
    await rt._flush_reactive(rt.rooms["main_room"])
    snap = dict(store._conn.execute(
        "SELECT rs.sensor_id, rs.temperature FROM reactive_sensor rs JOIN reactive r "
        "ON r.id = rs.reactive_id WHERE r.room_id = 'main_room'").fetchall())
    assert snap == {"living1": None, "living2": 24.5, "living3": 25.0}, snap

    wall["last_reported"] = (clock["t"] + timedelta(minutes=10)).isoformat()   # back again
    await beat(rt)
    assert readings() == [None, None, None, 24.0], readings()
    assert stale_events() == [("warning", True), ("info", False)], stale_events()
    await rt.stop()
    store.close()
    print("stale sensor: last reported 3 days ago but still showing 24.0, it heartbeats as "
          "unavailable and is left out of the reaction snapshot; one warning when it went "
          "stale, none repeated after a restart, one event when it reported again - OK")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
