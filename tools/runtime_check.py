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
  * a person's vote comes only from Home Assistant's ac_person_vote event:
    a setpoint change on a remote opens no reactive window, and HA's event
    for it is recorded as one too_cold / too_hot reaction (the unit and the
    value chosen, every sensor's reading), never raising the hold, replayed
    at once and again after the comfort window; a button's accepted vote
    the same with no unit rows; an ignored one is logged as an "ignored
    press" and teaches nothing, even when the runtime's own cache shows no
    forced state (HA alone judges the episode), and an accepted one is
    recorded even when the cache shows one
  * an automation-caused change (context.parent_id set) is ignored
  * a change during the guard window is ignored
  * a late echo of our own command (what the unit's state helper implies)
    is ignored
  * a heartbeat records the unit's control_state and control_offset
  * startup replays every room and pushes its almanac before any
    crossover: started 3 minutes before 22:00 with an empty almanac but
    history in the store, the Sleep crossover finds setpoint_applied
  * a sensor Home Assistant last reported longer ago than
    sensor_stale_minutes reads as unavailable in heartbeats and reaction
    snapshots, with one warning when it goes stale (not repeated after a
    restart) and one event when it reports again
  * several changes in one reactive window record the first setpoint_before,
    and a vote's setpoint change inside a window doesn't ride on it
  * a sleep section run crossing midnight closes against its start date
  * on restart, comfort-window replays are re-armed for recent setpoint-moving
    reactions only
  * outdoor input (phase 4), with outdoor.source 'forecast' and a stubbed
    weather.get_forecasts: fetched once at startup (the rooms' catch-up
    crossovers and the hourly loop share it) and again by the loop only
    once the cache is an hour old; at a crossover the cache is reused when
    under 2 hours old and refetched otherwise; a section run records the
    mean and max of the hours in [planned start, next section's planned
    start) - a normal window, sleep across midnight, and a window the
    forecast covers only part of, said so in the event - and a catch-up
    inside a run that has them keeps them; a service error, an unavailable
    entity, an empty forecast and a timeout each fail open (no value, the
    crossover not held up), with exactly one warning per failure streak
    (not repeated after a restart) and one info event on recovery, and
    rooms crossing together make one attempt; heartbeats and reactions
    record the weather entity's temperature, null when it is unavailable
    or last updated more than 3 hours ago; with source 'none', null; the
    real REST client posts to ?return_response, returns service_response,
    and raises its own timeout as HATimeoutError
  * the forecast's UTC times (as Met.no's weather.forecast_home gives them)
    are matched to section windows by Hong Kong time: a window crossing UTC
    midnight but not local midnight (Day from 07:00), one crossing local
    midnight but not UTC midnight (Sleep), and one whose hour is on the
    previous UTC date (Sunrise)

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
from app.ha import HAError                                       # noqa: E402
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
        self.subscribed: dict[str, object] = {}
    def on_reconnect(self, cb):
        pass
    def on_status(self, cb):
        pass
    async def connect(self):
        pass
    async def subscribe_events(self, event_type, callback):
        self.subscribed[event_type] = callback
        return len(self.subscribed)
    async def subscribe_states(self, callback):
        self.callback = callback
        return await self.subscribe_events("state_changed", callback)
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


def vote_event(room: str, direction: str, accepted=True, source: str = "button",
               chosen: float | None = None, unit: str | None = None) -> dict:
    """An ac_person_vote event as HA's vote or manual-change automation
    fires it."""
    data = {"room": room, "direction": direction, "accepted": accepted, "source": source}
    if source == "remote":
        data.update(chosen=chosen, unit=unit)
    return {"event_type": "ac_person_vote", "data": data,
            "context": {"id": "c2", "parent_id": "the automation's run", "user_id": None}}


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
    assert c.execute("SELECT outdoor_temp FROM heartbeat").fetchone()[0] is None   # source none
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

    # --- a person's vote: a setpoint change on the remote ---
    assert set(ws.subscribed) == {"state_changed", "ac_person_vote"}, ws.subscribed
    ws.subscribed.clear()                         # a dropped socket loses both
    await rt._resubscribe()
    assert set(ws.subscribed) == {"state_changed", "ac_person_vote"}, ws.subscribed
    rt._cache["input_boolean.ac_active_main_room"] = "off"
    rt._on_state_change(_state_changed("climate.lounge_lounge", 22.0, 24.0))
    assert not rt.rooms["main_room"].reactive_units, "a remote's setpoint change opened a window"
    await asyncio.sleep(1.5)  # a window would have flushed by now
    assert c.execute("SELECT COUNT(*) FROM reactive").fetchone()[0] == 0
    ws.subscribed["ac_person_vote"](vote_event("main_room", "too_cold", source="remote",
                                               chosen=24.0, unit="main_room_ac"))
    await eventually(lambda: replays(store, "main_room", "vote"), "the vote's replay")
    hold_calls = [c for c in rest.service_calls
                  if c[2].get("entity_id", "").startswith("input_boolean.ac_hold_")]
    assert not hold_calls, f"the hold was raised on a reaction: {hold_calls}"
    rx = c.execute("SELECT COUNT(*) FROM reactive WHERE room_id='main_room'").fetchone()[0]
    rxs = c.execute("SELECT COUNT(*) FROM reactive_sensor").fetchone()[0]
    assert rx == 1, rx
    assert rxs == 3, rxs   # all sensors snapshotted at reaction time
    assert last_reaction(c, "main_room") == ("too_cold", "too_cold", None, None, None, 24.0, 1)
    print("vote: subscribed to ac_person_vote as well as state_changed, both again after a "
          "reconnect - OK")
    print("vote: a remote's setpoint change opens no window of the runtime's own; HA's event for "
          "it is one too_cold reaction (unit, chosen 24), hold not raised, 3 sensors "
          "snapshotted - OK")
    assert len(replays(store, "main_room", "vote")) == 1
    alm = store.current_almanac("main_room", as_of=rt._today())["sections"][sec]
    assert alm["status"] == "learned", alm
    assert alm["units"]["main_room_ac"]["setpoint"] == 23.0          # 22 + 0.5 x (24 - 22)
    assert alm["units"]["main_room_ac"]["setpoint_applied"] == 23.0
    assert alm["sensors"]["living1"]["comfort"] == 24.5               # 24 + 0.5 x (24 + 1 - 24)
    print("vote: replayed at once - learned, setpoint 22 -> 23, applied 23, comfort moved "
          "toward reading + 1 - OK")
    await eventually(lambda: replays(store, "main_room", "comfort window"),
                     "the comfort-window replay")
    print("vote: replayed again once the comfort window had closed - OK")

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
    for before, after, mode, new_mode in ((22.0, 23.0, "off", "cool"), (23.0, 23.0, "cool", "off"),
                                          (23.0, 25.0, "off", "cool")):
        rt._on_state_change(_state_changed("climate.lounge_lounge", before, after, state=mode,
                                           new_state=new_mode))
    buffered = rt.rooms["main_room"].reactive_units["main_room_ac"]
    assert (buffered.setpoint_before, buffered.setpoint_after) == (22.0, 25.0), buffered
    await asyncio.sleep(1.5)  # let the flush window elapse
    rows = c.execute(
        "SELECT r.kind, ru.setpoint_before, ru.setpoint_after FROM reactive r "
        "JOIN reactive_unit ru ON ru.reactive_id = r.id "
        "WHERE r.room_id='main_room' ORDER BY r.id").fetchall()
    assert len(rows) == 2, [tuple(r) for r in rows]   # the earlier vote + this window
    assert tuple(rows[-1]) == ("on", 22.0, 25.0), tuple(rows[-1])
    print("reactive: on at 23, off, on at 25 in one window recorded once as an `on`, before "
          "22, after 25 - OK")

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
    print("reactive: off->on->off->on in one window is one `on` (first off, last on); "
          "on->off->on is one `manual` (power unchanged) - OK")

    comfort_before = len(replays(store, "main_room", "comfort window"))
    on_set = await window([_state_changed(lounge, 22.0, 24.0, state="off", new_state="cool")])
    assert on_set == ("on", "on", 0, 1, 22.0, 24.0, 1), on_set
    await eventually(lambda: len(replays(store, "main_room", "comfort window")) > comfort_before,
                     "the comfort replay an on-with-setpoint schedules")
    on_then_vote = await window([
        _state_changed(lounge, 22.0, 22.0, state="off", new_state="cool"),
        _state_changed(lounge, 22.0, 24.0),                         # a vote: HA's to record
        _state_changed(lounge, 24.0, 24.0, fan="low", new_fan="high")])
    assert on_then_vote == ("on", "on", 0, 1, 22.0, 22.0, 0), on_then_vote
    print("reactive: turned on at a new setpoint (one change) is one `on` reaction, "
          "setpoint-moving (22 -> 24, comfort replay scheduled); an on and then a setpoint "
          "change in one window is an `on` that leaves the vote's setpoint to HA's event - OK")

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
    # ...while a person's setpoint change, resting or away from our command,
    # is a vote: HA's event records it, not a window of the runtime's own
    rt._cache[f"input_select.ac_state_{bed_helper}"] = "rest"
    rt._on_state_change(_state_changed(bedroom, 22.0, 24.0, state="off", new_state="off"))
    rt._cache[f"input_select.ac_state_{bed_helper}"] = "cool"
    rt._on_state_change(_state_changed(bedroom, 25.0, 23.0))
    assert not rt.rooms["main_bedroom"].reactive_units
    await asyncio.sleep(1.5)
    assert n_reactions("main_bedroom") == count
    print("reactive: a setpoint change while resting, or away from our command, opens no "
          "window: it is a vote, which HA's event reports - OK")

    # --- a person's vote from a button: HA alone judges the episode ---
    forced = f"input_boolean.ac_forced_{bed_helper}"
    state = f"input_select.ac_state_{bed_helper}"
    vote_cb = ws.subscribed["ac_person_vote"]

    def ignored(room_id: str) -> list:
        return [e for e in store.recent_events(limit=1000, room_id=room_id, category="reactive")
                if (e.get("detail") or {}).get("ignored")]
    rt._cache[forced], rt._cache[state] = "off", "cool"     # the cache shows no forced state
    count, n_ignored = n_reactions("main_bedroom"), len(ignored("main_bedroom"))
    n_replays = len(replays(store, "main_bedroom", "vote"))
    almanac_before = store.current_almanac("main_bedroom", as_of=rt._today())
    vote_cb(vote_event("main_bedroom", "too_hot", accepted=False))
    await eventually(lambda: len(ignored("main_bedroom")) == n_ignored + 1, "the ignored press")
    await asyncio.sleep(0.3)
    assert n_reactions("main_bedroom") == count
    assert len(replays(store, "main_bedroom", "vote")) == n_replays
    assert store.current_almanac("main_bedroom", as_of=rt._today()) == almanac_before
    msg = ignored("main_bedroom")[0]["message"]
    assert "ignored press: too_hot from the button" in msg, msg
    print("vote: HA says ignored while the runtime's cache shows no forced state - an "
          "\"ignored press\" is logged, and nothing is recorded, replayed or learned - OK")

    rt._cache[forced], rt._cache[state] = "on", "rest"      # the cache shows an episode
    vote_cb(vote_event("main_bedroom", "too_cold", accepted=True))
    await eventually(lambda: n_reactions("main_bedroom") == count + 1, "the accepted vote")
    await eventually(lambda: len(replays(store, "main_bedroom", "vote")) == n_replays + 1,
                     "the vote's replay")
    kind, units, sensors = c.execute(
        "SELECT r.kind, (SELECT COUNT(*) FROM reactive_unit WHERE reactive_id = r.id), "
        "(SELECT COUNT(*) FROM reactive_sensor WHERE reactive_id = r.id) FROM reactive r "
        "WHERE r.room_id = 'main_bedroom' ORDER BY r.id DESC LIMIT 1").fetchone()
    assert (kind, units, sensors) == ("too_cold", 0, 2), (kind, units, sensors)
    print("vote: HA says accepted while the cache shows a forced rest - recorded (a "
          "too_cold reaction, no unit rows, both sensors snapshotted) and replayed at once; "
          "the runtime doesn't judge - OK")

    await asyncio.sleep(1.05)                    # reactions are unique per room and second
    vote_cb(vote_event("main_bedroom", "too_cold", accepted="True"))     # a string flag
    await eventually(lambda: n_reactions("main_bedroom") == count + 2, "a string 'True'")
    warnings = len(store.recent_events(category="reactive", severity="warning"))
    vote_cb(vote_event("no_such_room", "too_cold"))
    vote_cb(vote_event("main_bedroom", "sideways"))
    await eventually(lambda: len(store.recent_events(category="reactive", severity="warning"))
                     == warnings + 2, "the two warnings")
    assert n_reactions("main_bedroom") == count + 2
    count = n_reactions("main_bedroom")
    rt._on_state_change({"event_type": "state_changed", "context": {"parent_id": None},
                         "data": {"entity_id": "input_button.ac_too_cold_main_bedroom",
                                  "old_state": {"state": "2026-08-01T09:00:00+00:00"},
                                  "new_state": {"state": "2026-08-01T10:00:00+00:00"}}})
    await asyncio.sleep(1.5)
    assert n_reactions("main_bedroom") == count
    print("vote: an accepted flag sent as a string still counts; an unknown room or "
          "direction is a warning, not a reaction; the button's own state change is not a "
          "reaction (only HA's event is) - OK")

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
    await check_outdoor()
    await check_outdoor_utc()
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
    await rt._record_vote(vote_event("main_room", "too_cold"))         # a vote's snapshot
    clock["t"] += timedelta(seconds=5)
    rt._on_state_change(_state_changed("climate.lounge_lounge", 22.0, 22.0, state="cool",
                                       new_state="off"))
    await rt._flush_reactive(rt.rooms["main_room"])                      # a window's
    snaps = [(kind, dict(store._conn.execute(
        "SELECT rs.sensor_id, rs.temperature FROM reactive_sensor rs WHERE rs.reactive_id = ?",
        (rid,)).fetchall())) for rid, kind in store._conn.execute(
        "SELECT id, kind FROM reactive WHERE room_id = 'main_room' ORDER BY id")]
    want = {"living1": None, "living2": 24.5, "living3": 25.0}
    assert snaps == [("too_cold", want), ("off", want)], snaps

    wall["last_reported"] = (clock["t"] + timedelta(minutes=10)).isoformat()   # back again
    await beat(rt)
    assert readings() == [None, None, None, 24.0], readings()
    assert stale_events() == [("warning", True), ("info", False)], stale_events()
    await rt.stop()
    store.close()
    print("stale sensor: last reported 3 days ago but still showing 24.0, it heartbeats as "
          "unavailable and is left out of a vote's and a window's reaction snapshot; one "
          "warning when it went "
          "stale, none repeated after a restart, one event when it reported again - OK")


# Forecast temperatures by local hour (any other hour: 26.0). Sunset,
# 16:00-20:30, holds 16..20: mean 24.1, max 25.3. Sleep, 22:00-05:30, holds
# 22..05: mean 21.25, max 23.0. 21:00 and 06:00 sit just outside them.
TEMPS = {15: 27.0, 16: 25.3, 17: 24.6, 18: 24.0, 19: 23.5, 20: 23.1, 21: 30.0,
         22: 23.0, 23: 22.5, 0: 22.0, 1: 21.5, 2: 21.0, 3: 20.5, 4: 20.0, 5: 19.5, 6: 25.0}


class ForecastRest(StubRest):
    """StubRest with weather.get_forecasts: 48 hourly entries from the
    current hour, in UTC as Met.no's weather.forecast_home returns them
    ("2026-10-29T04:00:00+00:00"), or `fixed` entries, or a failure as
    `mode` says (error, timeout, empty)."""
    def __init__(self, states, clock, mode="ok", fixed=None):
        super().__init__(states)
        self.clock, self.mode, self.fixed, self.forecast_calls = clock, mode, fixed, []

    async def call_service(self, domain, service, data=None, *, return_response=False):
        if (domain, service) != ("weather", "get_forecasts"):
            return await super().call_service(domain, service, data)
        assert return_response and \
            data == {"entity_id": "weather.forecast_home", "type": "hourly"}, data
        self.forecast_calls.append(self.clock["t"])
        if self.fixed is not None:
            return {"weather.forecast_home": {"forecast": self.fixed}}
        if self.mode == "error":
            raise HAError("500: Internal Server Error")
        if self.mode == "timeout":
            await asyncio.sleep(5)
        if self.mode == "empty":
            return {"weather.forecast_home": {"forecast": []}}
        first = self.clock["t"].replace(minute=0, second=0, microsecond=0)
        hours = [first + timedelta(hours=k) for k in range(48)]
        return {"weather.forecast_home": {"forecast": [
            {"datetime": h.astimezone(timezone.utc).isoformat(), "condition": "sunny",
             "temperature": TEMPS.get(h.hour, 26.0)} for h in hours]}}


async def check_outdoor() -> None:
    """Phase 4: the forecast fetch and cache, the section run's mean and
    max, failures failing open, and the point value on heartbeats and
    reactions."""
    hk, utc = ZoneInfo("Asia/Hong_Kong"), ZoneInfo("UTC")
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp, SCHEMA)
    config = json.loads(json.dumps(CONFIG))
    config["outdoor"].update(source="forecast", weather_entity="weather.forecast_home")
    clock = {"t": datetime(2026, 8, 3, 15, 50, 0, tzinfo=hk)}
    states = _build_states()
    weather = {"entity_id": "weather.forecast_home", "state": "sunny",
               "attributes": {"temperature": 30.5, "supported_features": 3},
               "last_updated": clock["t"].astimezone(utc).isoformat()}
    states.append(weather)
    c = store._conn

    def runtime(rest) -> Runtime:
        rt = Runtime(config, store, rest, StubWS(), tmp)
        rt._now = lambda: (clock["t"].isoformat(timespec="seconds"),
                           clock["t"].astimezone(utc).isoformat(timespec="seconds"),
                           clock["t"].date().isoformat(), clock["t"])
        return rt

    async def settle(rt) -> None:
        """Let the crossovers' outdoor follow-ups finish."""
        await asyncio.sleep(0)
        await asyncio.gather(*[t for t in list(rt._followups)
                               if t.get_name().startswith("ac-outdoor-")])

    def run(room: str, day: str, section: str) -> tuple:
        return tuple(c.execute("SELECT outdoor_mean, outdoor_max FROM section_run WHERE "
                               "room_id = ? AND local_date = ? AND section = ?",
                               (room, day, section)).fetchone())

    def said(room: str, section: str) -> list[str]:
        return [e["message"] for e in reversed(store.recent_events(
            limit=1000, room_id=room, category="outdoor"))
            if (e.get("detail") or {}).get("section") == section]

    def streak_events() -> list[tuple]:
        return [(e["severity"], e["detail"]["failing"], e["detail"].get("kind")) for e in
                reversed(store.recent_events(limit=1000, category="outdoor"))
                if "failing" in (e.get("detail") or {})]

    def set_weather(state: str) -> None:
        weather["state"] = state
        rt._on_state_change({"event_type": "state_changed", "context": {},
                             "data": {"entity_id": "weather.forecast_home", "old_state": {},
                                      "new_state": {"state": state}}})

    # --- startup: one fetch, shared by three catch-up crossovers and the loop ---
    rest = ForecastRest(states, clock)
    rt = runtime(rest)
    await rt.start()
    await settle(rt)
    await asyncio.sleep(0.1)                         # the forecast loop's first pass
    assert rest.forecast_calls == [clock["t"]], rest.forecast_calls
    # 15:50, inside Afternoon (14:00-16:00); the forecast starts at 15:00
    assert run("main_room", "2026-08-03", "afternoon") == (27.0, 27.0)
    assert said("main_room", "afternoon") == [
        "Main Room -> afternoon: forecast mean 27.0, max 27.0 over 15:00-16:00 only "
        "(weather.forecast_home): the forecast covers 1 of the 2 hours in 14:00-16:00"], \
        said("main_room", "afternoon")
    print("outdoor: startup fetched the forecast once for three catch-up crossovers and the "
          "hourly loop; Afternoon, which it covers only from 15:00, records what is covered "
          "and says so - OK")

    # --- a normal window, from the cache (10 minutes old) ---
    main = rt.rooms["main_room"]
    clock["t"] = datetime(2026, 8, 3, 16, 0, 5, tzinfo=hk)
    await rt._crossover(main, "sunset", datetime(2026, 8, 3, 16, 0, tzinfo=hk), catch_up=False)
    await settle(rt)
    assert len(rest.forecast_calls) == 1, "a 10-minute-old forecast was fetched again"
    assert run("main_room", "2026-08-03", "sunset") == (24.1, 25.3), \
        run("main_room", "2026-08-03", "sunset")
    assert said("main_room", "sunset") == [
        "Main Room -> sunset: forecast mean 24.1, max 25.3 over 16:00-20:30 "
        "(weather.forecast_home)"]
    # --- sleep across midnight, refetched (the cache is 6 hours old) ---
    clock["t"] = datetime(2026, 8, 3, 22, 0, 5, tzinfo=hk)
    await rt._crossover(main, "sleep", datetime(2026, 8, 3, 22, 0, tzinfo=hk), catch_up=False)
    await settle(rt)
    assert rest.forecast_calls[1:] == [clock["t"]], rest.forecast_calls
    assert run("main_room", "2026-08-03", "sleep") == (21.25, 23.0), \
        run("main_room", "2026-08-03", "sleep")
    assert said("main_room", "sleep") == [
        "Main Room -> sleep: forecast mean 21.2, max 23.0 over 22:00-05:30 (weather.forecast_home)"]
    print("outdoor: Sunset 16:00-20:30 from the 10-minute-old cache (mean 24.1, max 25.3); "
          "Sleep 22:00-05:30 across midnight, refetched (mean 21.25, max 23.0); the hours "
          "just outside each window left out - OK")

    # --- the cache: under 2 hours reused, over refetched ---
    fetched = clock["t"]
    clock["t"] = fetched + timedelta(hours=1, minutes=59)
    assert await rt._forecast_for(runtime_mod.outdoor.CACHE_MAX_AGE) is rt._forecast
    assert len(rest.forecast_calls) == 2
    clock["t"] = fetched + timedelta(hours=2, minutes=1)
    await rt._forecast_for(runtime_mod.outdoor.CACHE_MAX_AGE)
    assert rest.forecast_calls[2:] == [clock["t"]] and rt._forecast.fetched_at == clock["t"]
    print("outdoor: a crossover reuses a forecast 1 h 59 min old and refetches one 2 h 01 "
          "min old - OK")
    await rt.stop()

    # --- a restart inside Sleep: the run keeps the values decided at 22:00 ---
    clock["t"] = datetime(2026, 8, 4, 1, 0, 0, tzinfo=hk)
    rest = ForecastRest(states, clock)
    rt = runtime(rest)
    await rt.start()
    await settle(rt)
    await asyncio.sleep(0.1)
    assert rest.forecast_calls == [clock["t"]], rest.forecast_calls
    assert run("main_room", "2026-08-03", "sleep") == (21.25, 23.0)
    assert len(said("main_room", "sleep")) == 1, said("main_room", "sleep")
    # a room with no Sleep run yet gets what the forecast still covers, 01..05
    assert run("baby_room", "2026-08-03", "sleep") == (20.5, 21.5)
    assert "covers 5 of the 8 hours in 22:00-05:30" in said("baby_room", "sleep")[0]
    print("outdoor: a 01:00 restart inside Sleep keeps the values recorded at 22:00; a room "
          "starting its Sleep run then records the 5 of 8 hours still forecast - OK")

    # --- failures fail open: one warning per streak, one event on recovery ---
    saved_timeout = runtime_mod.FORECAST_TIMEOUT_SECONDS
    runtime_mod.FORECAST_TIMEOUT_SECONDS = 0.3
    try:
        for i, mode in enumerate(("error", "unavailable", "empty", "timeout")):
            day = datetime(2026, 8, 5 + i, 8, 0, tzinfo=hk)
            clock["t"] = day + timedelta(seconds=5)
            if mode == "unavailable":
                set_weather("unavailable")
            else:
                rest.mode = mode
            calls, before = len(rest.forecast_calls), streak_events()
            loop = asyncio.get_running_loop()
            began = loop.time()
            for rs in rt.rooms.values():                 # three rooms cross together
                await rt._crossover(rs, "day", day, catch_up=False)
            assert loop.time() - began < 0.2, "a failing forecast held up the crossovers"
            assert all(("automation", "trigger", {"entity_id": f"automation.ac_{r}_day"})
                       in rest.service_calls for r in rt.rooms), mode
            await settle(rt)
            assert len(rest.forecast_calls) == calls + (mode != "unavailable"), \
                (mode, rest.forecast_calls[calls:])
            for r in rt.rooms:
                assert run(r, day.date().isoformat(), "day") == (None, None), (mode, r)
            assert said("main_room", "day")[-1] == (
                "Main Room -> day: no forecast from weather.forecast_home, so no outdoor value "
                "for 08:00-14:00"), said("main_room", "day")
            clock["t"] += timedelta(minutes=10)          # the next try fails too
            assert await rt._forecast_for(runtime_mod.FORECAST_REFRESH) is None
            assert streak_events()[len(before):] == [("warning", True, mode)], \
                (mode, streak_events())
            clock["t"] += timedelta(minutes=10)          # and then it works again
            if mode == "unavailable":
                set_weather("sunny")
            else:
                rest.mode = "ok"
            assert await rt._forecast_for(runtime_mod.FORECAST_REFRESH) is rt._forecast
            assert streak_events()[len(before):] == [("warning", True, mode),
                                                      ("info", False, None)], streak_events()
        warning = next(e["message"] for e in store.recent_events(
            limit=1000, category="outdoor", severity="warning")
            if (e.get("detail") or {}).get("kind") == "timeout")
        assert warning.startswith("Outdoor forecast from weather.forecast_home failed: no answer "
                                  "within 0.3 s. Section runs get no forecast values until it "
                                  "works again"), warning
        print("outdoor: a service error, an unavailable entity, an empty forecast and a "
              "timeout each fail open - the crossovers fire at once, three rooms share one "
              "attempt, their runs keep no value - with one warning per streak, not "
              "repeated by the next failed try, and one info event on recovery - OK")

        # a restart inside a streak doesn't warn again
        clock["t"] = datetime(2026, 8, 10, 12, 0, tzinfo=hk)
        rest.mode = "error"
        assert await rt._forecast_for(runtime_mod.FORECAST_REFRESH) is None
        n = len(streak_events())
        await rt.stop()
        clock["t"] += timedelta(minutes=5)
        rest = ForecastRest(states, clock, mode="error")
        rt = runtime(rest)
        await rt.start()
        await settle(rt)
        await asyncio.sleep(0.1)
        assert rest.forecast_calls and len(streak_events()) == n, streak_events()[n - 1:]
        clock["t"] += timedelta(minutes=5)
        rest.mode = "ok"
        await rt._forecast_for(runtime_mod.FORECAST_REFRESH)
        assert streak_events()[n:] == [("info", False, None)], streak_events()[n - 1:]
        print("outdoor: a restart inside a failure streak doesn't repeat its warning; the "
              "recovery is still logged once - OK")
    finally:
        runtime_mod.FORECAST_TIMEOUT_SECONDS = saved_timeout

    # --- heartbeats and reactions record the weather entity's temperature ---
    main = rt.rooms["main_room"]

    def outdoor_temps(table: str) -> list:
        return [r[0] for r in c.execute(
            f"SELECT outdoor_temp FROM {table} WHERE room_id = 'main_room' ORDER BY id")]
    beats = len(outdoor_temps("heartbeat"))
    for age, state in ((timedelta(0), "sunny"), (timedelta(hours=3), "sunny"),
                       (timedelta(hours=3, minutes=1), "sunny"), (timedelta(0), "unavailable")):
        clock["t"] += timedelta(minutes=10)
        weather["last_updated"] = (clock["t"] - age).astimezone(utc).isoformat()
        weather["state"] = state
        await rt._do_heartbeat(main)
    assert outdoor_temps("heartbeat")[beats:] == [30.5, 30.5, None, None], \
        outdoor_temps("heartbeat")[beats:]
    weather.update(state="sunny", last_updated=clock["t"].astimezone(utc).isoformat())
    await rt._record_vote(vote_event("main_room", "too_cold"))               # a vote
    clock["t"] += timedelta(seconds=5)
    rt._on_state_change(_state_changed("climate.lounge_lounge", 22.0, 22.0, state="cool",
                                       new_state="off"))
    await rt._flush_reactive(main)                                            # a window
    clock["t"] += timedelta(hours=3, minutes=1)              # the entity hasn't updated since
    await rt._record_vote(vote_event("main_room", "too_hot"))
    reactions = [tuple(r) for r in c.execute(
        "SELECT kind, outdoor_temp FROM reactive WHERE room_id = 'main_room' ORDER BY id")]
    assert reactions[-3:] == [("too_cold", 30.5), ("off", 30.5), ("too_hot", None)], reactions
    print("outdoor: heartbeats, a vote and a window record the weather entity's 30.5; null "
          "when it is unavailable or last updated more than 3 hours ago (exactly 3 is still "
          "fresh) - OK")
    await rt.stop()

    # --- the hourly loop refetches only once the cache is an hour old ---
    saved_refresh = runtime_mod.FORECAST_REFRESH
    runtime_mod.FORECAST_REFRESH = timedelta(seconds=0.3)    # the loop's sleep and its age
    try:
        rest = ForecastRest(states, clock)
        rt = runtime(rest)
        await rt.start()
        await settle(rt)
        await asyncio.sleep(0.7)                  # two more passes; the clock hasn't moved
        assert len(rest.forecast_calls) == 1, rest.forecast_calls
        clock["t"] += timedelta(hours=1)
        await eventually(lambda: len(rest.forecast_calls) == 2, "the hourly refetch")
        await rt.stop()
    finally:
        runtime_mod.FORECAST_REFRESH = saved_refresh
    print("outdoor: the forecast loop refetches once the cached forecast is FORECAST_REFRESH "
          "old, not before - OK")

    # --- the real REST client: asks for the response, maps its own timeout ---
    import httpx
    from app.ha import HARest, HATimeoutError
    seen = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.url.query.decode(),
                     json.loads(request.content)))
        if mode["timeout"]:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"changed_states": [], "service_response": {
            "weather.forecast_home": {"forecast": [{"datetime": "2026-08-12T08:00:00+00:00",
                                           "temperature": 29.0}]}}})
    mode = {"timeout": False}
    client = HARest("http://ha.test:8123", "token")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    got = await client.call_service("weather", "get_forecasts",
                                    {"entity_id": "weather.forecast_home", "type": "hourly"},
                                    return_response=True)
    assert seen == [("POST", "/api/services/weather/get_forecasts", "return_response",
                     {"entity_id": "weather.forecast_home", "type": "hourly"})], seen
    assert list(got) == ["weather.forecast_home"] and \
        runtime_mod.outdoor.parse_forecast(got, "weather.forecast_home")[0].temperature == 29.0
    mode["timeout"] = True
    try:
        await client.call_service("weather", "get_forecasts", {}, return_response=True)
    except HATimeoutError as e:
        assert str(e) == "weather.get_forecasts: no answer within 10 s", e
    else:
        raise AssertionError("an httpx timeout wasn't raised as HATimeoutError")
    clock["t"] += timedelta(hours=3)                 # through the runtime: a timeout warning
    rt = runtime(client)
    rt._cache["weather.forecast_home"] = "sunny"
    n = len(streak_events())
    assert await rt._fetch_forecast() is None
    assert streak_events()[n:] == [("warning", True, "timeout")], streak_events()[n:]
    assert "failed: weather.get_forecasts: no answer within 10 s." in store.recent_events(
        category="outdoor", severity="warning")[0]["message"]
    await client.close()
    store.close()
    print("outdoor: the REST client posts to ?return_response and returns service_response; "
          "its own timeout is an HATimeoutError, logged as a timeout failure - OK")


async def check_outdoor_utc() -> None:
    """Met.no's forecast times are UTC; Hong Kong is UTC+8, so UTC midnight
    is 08:00 there. Each entry must be matched to a section window by its
    local time. With Day moved to 07:00, Day (07:00-14:00 HKT, 23:00-06:00
    UTC) crosses UTC midnight but not local midnight; Sleep (22:00-05:30
    HKT, 14:00-21:30 UTC) crosses local midnight but not UTC midnight; and
    Sunrise (05:30-07:00 HKT) has its one hour, 06:00, on the previous UTC
    date. Every hour outside those windows is forecast at 40, so an entry
    matched by its UTC date or UTC hour shows in the max."""
    hk, utc = ZoneInfo("Asia/Hong_Kong"), ZoneInfo("UTC")
    od = runtime_mod.outdoor
    one = od.parse_forecast({"weather.forecast_home": {"forecast": [
        {"datetime": "2026-10-29T04:00:00+00:00", "temperature": 25.0}]}},
        "weather.forecast_home", hk)[0]
    assert one.at.isoformat() == "2026-10-29T12:00:00+08:00", one.at
    print("outdoor: a forecast time of 2026-10-29T04:00:00+00:00 reads as 12:00 Hong Kong "
          "time - OK")

    temps = {"2026-10-29T22:00:00+00:00": 24.0,                       # 06:00 HKT, Sunrise
             "2026-10-29T23:00:00+00:00": 26.0,                       # 07:00 HKT, Day ...
             "2026-10-30T00:00:00+00:00": 27.0, "2026-10-30T01:00:00+00:00": 28.0,
             "2026-10-30T02:00:00+00:00": 29.0, "2026-10-30T03:00:00+00:00": 30.0,
             "2026-10-30T04:00:00+00:00": 31.0, "2026-10-30T05:00:00+00:00": 32.0,
             "2026-10-30T14:00:00+00:00": 23.0,                       # 22:00 HKT, Sleep ...
             "2026-10-30T15:00:00+00:00": 22.5, "2026-10-30T16:00:00+00:00": 22.0,
             "2026-10-30T17:00:00+00:00": 21.5, "2026-10-30T18:00:00+00:00": 21.0,
             "2026-10-30T19:00:00+00:00": 20.5, "2026-10-30T20:00:00+00:00": 20.0,
             "2026-10-30T21:00:00+00:00": 19.5}                       # ... 05:00 HKT
    first = datetime(2026, 10, 29, 12, 0, tzinfo=utc)
    fixed = []
    for k in range(40):                                  # 29 Oct 12:00 to 31 Oct 03:00 UTC
        at = (first + timedelta(hours=k)).isoformat()
        fixed.append({"datetime": at, "temperature": temps.get(at, 40.0),
                      "condition": "cloudy"})
    assert all(f["datetime"].endswith("+00:00") for f in fixed)

    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp, SCHEMA)
    config = json.loads(json.dumps(CONFIG))
    config["outdoor"].update(source="forecast", weather_entity="weather.forecast_home")
    day = next(s for s in config["schedule_profiles"][0]["sections"] if s["id"] == "day")
    day["trigger"]["time"] = "07:00"
    clock = {"t": datetime(2026, 10, 30, 6, 10, 0, tzinfo=hk)}
    states = _build_states() + [{"entity_id": "weather.forecast_home", "state": "cloudy",
                                 "attributes": {"temperature": 27.0, "supported_features": 3},
                                 "last_updated": clock["t"].isoformat()}]
    rest = ForecastRest(states, clock, fixed=fixed)
    rt = Runtime(config, store, rest, StubWS(), tmp)
    rt._now = lambda: (clock["t"].isoformat(timespec="seconds"),
                       clock["t"].astimezone(utc).isoformat(timespec="seconds"),
                       clock["t"].date().isoformat(), clock["t"])

    async def settle() -> None:
        await asyncio.sleep(0)
        await asyncio.gather(*[t for t in list(rt._followups)
                               if t.get_name().startswith("ac-outdoor-")])

    def run(day: str, section: str) -> tuple:
        return tuple(store._conn.execute(
            "SELECT outdoor_mean, outdoor_max FROM section_run WHERE room_id = 'main_room' "
            "AND local_date = ? AND section = ?", (day, section)).fetchone())

    def said(section: str) -> str:
        return next(e["message"] for e in store.recent_events(
            limit=1000, room_id="main_room", category="outdoor")
            if (e.get("detail") or {}).get("section") == section)

    await rt.start()                                     # 06:10 HKT: Sunrise, caught up
    await settle()
    assert rt._forecast.hours[0].at.utcoffset() == timedelta(hours=8)
    assert run("2026-10-30", "sunrise") == (24.0, 24.0), run("2026-10-30", "sunrise")
    assert said("sunrise") == ("Main Room -> sunrise: forecast mean 24.0, max 24.0 over "
                               "05:30-07:00 (weather.forecast_home)"), said("sunrise")
    main = rt.rooms["main_room"]
    clock["t"] = datetime(2026, 10, 30, 7, 0, 5, tzinfo=hk)
    await rt._crossover(main, "day", datetime(2026, 10, 30, 7, 0, tzinfo=hk), catch_up=False)
    await settle()
    assert run("2026-10-30", "day") == (29.0, 32.0), run("2026-10-30", "day")
    assert said("day") == ("Main Room -> day: forecast mean 29.0, max 32.0 over 07:00-14:00 "
                           "(weather.forecast_home)"), said("day")
    clock["t"] = datetime(2026, 10, 30, 22, 0, 5, tzinfo=hk)
    await rt._crossover(main, "sleep", datetime(2026, 10, 30, 22, 0, tzinfo=hk), catch_up=False)
    await settle()
    assert run("2026-10-30", "sleep") == (21.25, 23.0), run("2026-10-30", "sleep")
    assert said("sleep") == ("Main Room -> sleep: forecast mean 21.2, max 23.0 over "
                             "22:00-05:30 (weather.forecast_home)"), said("sleep")
    assert len(rest.forecast_calls) == 2                  # startup, and Sleep's refetch
    await rt.stop()
    store.close()
    print("outdoor: UTC forecast times matched by Hong Kong time - Day 07:00-14:00 crosses "
          "UTC midnight but not local (mean 29.0, max 32.0 from 23:00-05:00 UTC), Sleep "
          "22:00-05:30 crosses local midnight but not UTC (mean 21.25, max 23.0 from "
          "14:00-21:00 UTC), Sunrise's 06:00 is 22:00 UTC the day before - OK")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
