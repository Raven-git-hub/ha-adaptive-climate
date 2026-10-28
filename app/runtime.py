"""
Adaptive Climate - runtime.

Ties the HA client, scheduler and store together into a live system.

Observation (passive):
  * heartbeat loop - every heartbeat_interval, sample each room's sensors
    and units and record them. Defers (does not skip) while a room's guard
    is on, so it never samples a unit mid-transition.
  * reactive detection - a state_changed subscription; a person's change
    to a unit (NOT automation-caused) - its setpoint, turning it on or off,
    or a mode or fan change while on - opens a consolidation window,
    snapshots every sensor, and records one reactive event of kind on, off,
    setpoint or manual. HA's manual-change automation applies the same
    change to the unit's control helpers; the runtime no longer raises
    ac_hold, which is now only a manual pause. A unit dropping out to
    unavailable/unknown and back is never a reaction; if it comes back
    changed, that is logged as a control event.

Learning (docs/V2_DESIGN.md, Rebuilding) - replay_room() re-runs the
reaction replay for a room and pushes the result:
  * at startup, for every room, before any crossover (catch-up included);
  * right after each recorded reaction;
  * 65 minutes after each setpoint-moving reaction, once its comfort window
    has closed (re-armed at startup for reactions in the last 65 minutes);
  * on a heartbeat that can seed something the almanac lacks: a section
    with no almanac, or a unit or sensor not yet in it;
  * nightly at 00:15, as a consistency check.

Control (active):
  * scheduler loop - level-triggered; every tick, ask the scheduler which
    section should be active per room and fire the crossover (automation
    .trigger on the room's scene automation) when the answer changes.
  * almanac push - the almanac lives on sensor.ac_almanac_<room> as state
    attributes; API-set states do not survive an HA restart, so re-push on
    a timer and on homeassistant_start.

Automation-caused changes are told apart from user ones primarily by
context.parent_id (our service calls carry it), backed by the guard boolean,
configured external_guards, and the same check HA's manual-change
automation makes: a change that shows what the unit's state helper
implies (app.control.matches_command) is our own command arriving late.
See docs/DESIGN.md 4, 12.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app import control
from app.analyser import LearningConfig, analyse_room, is_setpoint_moving
from app.config import ConfigError, validate
from app.generator import (almanac_id, base_id, guard_id, offset_id, scene_automation_id,
                           scene_select_id, state_id)
from app.scheduler import active_section_at
from app.store import ReactiveUnitSample, UnitSample

log = logging.getLogger("adaptive_climate.runtime")

SCHEDULER_TICK_SECONDS = 20
ALMANAC_PUSH_SECONDS = 300
GUARD_DEFER_POLL_SECONDS = 2
GUARD_DEFER_MAX_SECONDS = 60
# The comfort window closes 60 minutes after a reaction; replay 5 later.
COMFORT_REPLAY_DELAY = timedelta(minutes=65)
OFFISH = ("off", "unavailable", "unknown", None)
NO_STATE = ("unavailable", "unknown", None)     # the unit isn't reporting at all


def _profiles(config: dict) -> dict:
    return {p["id"]: p for p in config.get("schedule_profiles", [])}


def _parse_ts(value: str | None) -> datetime | None:
    """An HA timestamp (ISO 8601, with its offset), or None."""
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _ago(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 120:
        return f"{minutes} min"
    hours = minutes // 60
    return f"{hours} h" if hours < 48 else f"{hours // 24} d {hours % 24} h"


def _number(state: str | None, default: float | None = 0.0) -> float | None:
    """An input_number's state string as a float, or default."""
    try:
        return float(state)
    except (TypeError, ValueError):
        return default


def _infer_ac_state(hvac_mode, fan_mode, leak_on: bool) -> str | None:
    if leak_on:
        return "leak"
    if hvac_mode == "dry":
        return "leak"
    if hvac_mode == "fan_only":
        return "warming"
    if hvac_mode == "cool":
        return "cooling" if (fan_mode or "").lower() == "medium" else "normal"
    return None


@dataclass
class UnitWindow:
    """One unit's changes inside a reactive window: the first and the last
    value of each, so the window becomes a single reaction."""
    power_before: bool
    power_after: bool
    mode_before: str
    mode_after: str
    fan_before: str | None
    fan_after: str | None
    setpoint_before: float | None
    setpoint_after: float | None
    setpoint_changed: bool = False    # any setpoint change >= reactive_min_delta in the window

    @property
    def kind(self) -> str:
        """on/off if power changed (first vs last), else setpoint if the
        setpoint was changed at all in the window, else manual (a mode or fan
        change while on)."""
        if self.power_before != self.power_after:
            return "on" if self.power_after else "off"
        return "setpoint" if self.setpoint_changed else "manual"

    def sample(self) -> ReactiveUnitSample:
        return ReactiveUnitSample(self.setpoint_before, self.setpoint_after,
                                  self.setpoint_changed, kind=self.kind,
                                  power_before=self.power_before,
                                  power_after=self.power_after)


def room_kind(unit_kinds) -> str:
    """The room-level kind: off if any unit went off, else on, else
    setpoint, else manual."""
    kinds = set(unit_kinds)
    return next(k for k in ("off", "on", "setpoint", "manual") if k in kinds)


def _snapshot(state: dict) -> dict:
    """What a unit was doing, for the drop-out comparison."""
    attrs = state.get("attributes") or {}
    return {"power": "on" if state.get("state") != "off" else "off",
            "mode": state.get("state"), "setpoint": attrs.get("temperature"),
            "fan": attrs.get("fan_mode")}


@dataclass
class RoomState:
    room: dict
    profile: dict
    section: str | None = None
    section_started: datetime | None = None
    reactive_units: dict[str, UnitWindow] = field(default_factory=dict)
    reactive_deadline: datetime | None = None
    reactive_task: object | None = None


class Runtime:
    def __init__(self, config: dict, store, rest, ws, data_dir) -> None:
        self.config = config
        self.store = store
        self.rest = rest
        self.ws = ws
        self.data_dir = data_dir
        self.tz = None
        self.rooms: dict[str, RoomState] = {}
        self._cache: dict[str, str] = {}          # entity_id -> last state string
        self._automation_entity: dict[str, str] = {}   # config id -> automation.<entity>
        self._tasks: list[asyncio.Task] = []
        self._unit_index: dict[str, tuple[str, str]] = {}   # unit entity -> (room_id, unit_id)
        self._followups: set[asyncio.Task] = set()          # pending comfort-window replays
        self._dropouts: dict[str, dict] = {}                 # unit entity -> state when it dropped out
        self._stale: dict[tuple[str, str], bool] = {}        # (room, sensor) -> stale, as last logged
        self._stopping = False

    # ---- config helpers -------------------------------------------

    def _learning_config(self) -> LearningConfig:
        return LearningConfig.from_config(self.config)

    @property
    def _sys(self) -> dict:
        return self.config.get("system", {})

    # ---- time -----------------------------------------------------

    def _now(self):
        n = datetime.now(self.tz)
        return n.isoformat(timespec="seconds"), \
            n.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds"), \
            n.date().isoformat(), n

    # ---- lifecycle ------------------------------------------------

    async def start(self) -> None:
        cfg = await self.rest.config()
        tzname = cfg.get("time_zone") or "UTC"
        self.tz = ZoneInfo(tzname)
        self.store.save_config_version(self.config)
        try:
            validate(self.config)
        except ConfigError as e:
            # Keep running on it (stopping would stop observation too), but
            # say so where it is seen: deploy refuses this config.
            ts, ts_utc, _, _ = self._now()
            self.store.log_event(ts, ts_utc, "error", "config",
                                 f"the config does not validate: {e}. Running on it as it is; "
                                 "fix it on the Config page, save, and restart",
                                 detail={"error": str(e)})

        states = await self.rest.states()
        for s in states:
            self._cache[s["entity_id"]] = s.get("state")
        self._automation_entity = {
            s["attributes"]["id"]: s["entity_id"]
            for s in states
            if s["entity_id"].startswith("automation.") and s.get("attributes", {}).get("id")
        }

        profs = _profiles(self.config)
        for room in self.config.get("rooms", []):
            if not room.get("enabled", True):
                continue
            profile = profs.get(room.get("schedule_profile", "default")) \
                or profs.get("default") or self.config["schedule_profiles"][0]
            self.rooms[room["id"]] = RoomState(room=room, profile=profile)
            for u in room["units"]:
                self._unit_index[u["entity_id"]] = (room["id"], u["id"])

        # Register the resubscribe hook BEFORE the first subscribe, so a
        # drop at any point is repaired by the ws supervisor re-running it.
        # Without this, a single websocket drop silently ends reactive
        # detection - user setpoint corrections stop being captured - and
        # the connection never recovers until a container restart.
        self.ws.on_reconnect(self._resubscribe)
        self.ws.on_status(self._on_ws_status)
        await self.ws.subscribe_states(self._on_state_change)

        # Which sensors were last logged as stale, so a restart doesn't
        # repeat the warning for one that still is.
        for e in reversed(self.store.recent_events(limit=1000, category="validation")):
            d = e.get("detail") or {}
            if "stale" in d and "sensor" in d:
                self._stale[(e["room_id"], d["sensor"])] = bool(d["stale"])

        # Replay every room and push its almanac before any crossover can
        # fire, catch-up included: after a migration the almanac is empty,
        # and a crossover that finds no setpoint_applied leaves every unit
        # as it is (in manual). Waiting for the first heartbeat's replay
        # left a crossover three minutes after startup with nothing.
        for room_id in self.rooms:
            await self.replay_room(room_id, "startup")

        # initial catch-up crossover for every room
        _, _, _, now = self._now()
        for rs in self.rooms.values():
            section, started = active_section_at(rs.profile, now, self.tz)
            await self._crossover(rs, section, started, catch_up=True)

        # A restart drops pending comfort-window replays; re-arm them for the
        # setpoint-moving reactions of the last 65 minutes rather than wait
        # for the nightly replay.
        now = self._now()[3]
        since = (now - COMFORT_REPLAY_DELAY).astimezone(ZoneInfo("UTC")).isoformat(
            timespec="seconds")
        for room_id in self.rooms:
            for ts_utc, kind, units in self.store.reactions_since(room_id, since):
                if is_setpoint_moving(kind, units):
                    self._schedule_comfort_replay(
                        room_id, datetime.fromisoformat(ts_utc) + COMFORT_REPLAY_DELAY)

        self._tasks = [
            asyncio.create_task(self._scheduler_loop(), name="ac-scheduler"),
            asyncio.create_task(self._heartbeat_loop(), name="ac-heartbeat"),
            asyncio.create_task(self._almanac_push_loop(), name="ac-almanac-push"),
            asyncio.create_task(self._nightly_loop(), name="ac-nightly"),
        ]
        log.info("runtime started: %d room(s), tz=%s", len(self.rooms), tzname)

    async def stop(self) -> None:
        self._stopping = True
        pending = self._tasks + list(self._followups)
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []
        self._followups.clear()

    # ---- state-change stream --------------------------------------

    async def _resubscribe(self) -> None:
        """Re-run every websocket subscription after a (re)connect. HA's
        subscription ids do not survive a dropped socket, so the state
        stream must be re-established or reactive detection stays dead."""
        await self.ws.subscribe_states(self._on_state_change)

    def _on_ws_status(self, connected: bool, reason: str) -> None:
        """Record websocket up/down transitions in the event log so a drop
        (and its recovery) is visible in the UI's Log view, not just stdout."""
        try:
            ts, ts_utc, _, _ = self._now()
            self.store.log_event(
                ts, ts_utc, "info" if connected else "warning", "connection",
                f"Home Assistant websocket {'up' if connected else 'down'}: {reason}")
        except Exception as e:
            log.warning("could not log ws status event: %s", e)

    def _on_state_change(self, event: dict) -> None:
        data = event.get("data", {})
        entity_id = data.get("entity_id")
        if not entity_id:
            return
        new = data.get("new_state") or {}
        self._cache[entity_id] = new.get("state")

        if entity_id in self._unit_index:
            self._maybe_reactive(event, entity_id, data)

    def _is_user_change(self, event: dict, data: dict, room_id: str) -> bool:
        """A change is a person's only if it was not caused by an automation:
        no context.parent_id, the room guard is off, and no external guard is
        on. (_is_our_command then rules out a late echo of our own command.)"""
        new = data.get("new_state") or {}
        ctx = new.get("context") or event.get("context") or {}
        if ctx.get("parent_id"):
            return False
        if self._cache.get(guard_id(room_id)) == "on":
            return False
        for eg in self._sys.get("external_guards", []):
            if self._cache.get(eg) == "on":
                return False
        return True

    def _maybe_reactive(self, event: dict, entity_id: str, data: dict) -> None:
        """Buffer a person's change to a unit - its setpoint, its power (off
        to any mode is on, any mode to off is off) or, while it stays on, its
        mode or fan - into the room's reactive window."""
        room_id, unit_id = self._unit_index[entity_id]
        old_s, new_s = data.get("old_state") or {}, data.get("new_state") or {}
        old_mode, new_mode = old_s.get("state"), new_s.get("state")
        if old_mode in NO_STATE or new_mode in NO_STATE:
            # Dropping out or coming back is the unit, not a person.
            self._track_dropout(entity_id, room_id, unit_id, old_s, new_s)
            return
        if not self._is_user_change(event, data, room_id):
            return
        old = old_s.get("attributes") or {}
        new = new_s.get("attributes") or {}
        before, after = old.get("temperature"), new.get("temperature")
        min_delta = self._sys.get("reactive_min_delta", 0.5)
        was = control.Climate(old_mode, before, old.get("fan_mode"))
        now_shows = control.Climate(new_mode, after, new.get("fan_mode"))
        if control.classify_change(was, now_shows, min_delta) is None:
            return      # another attribute (current_temperature, ...) changed
        if self._is_our_command(room_id, unit_id, was, now_shows, new, min_delta):
            return      # our own command, showing up after the guard came down
        setpoint_changed = control.setpoint_moved(before, after, min_delta)
        power_before, power_after = old_mode != "off", new_mode != "off"

        rs = self.rooms[room_id]
        # One record per window: the first value of everything before the
        # window's first change, and the last value after its last change.
        w = rs.reactive_units.get(unit_id)
        if w is None:
            w = rs.reactive_units[unit_id] = UnitWindow(
                power_before, power_after, old_mode, new_mode,
                old.get("fan_mode"), new.get("fan_mode"), before, after)
        else:
            w.power_after, w.mode_after, w.fan_after = power_after, new_mode, new.get("fan_mode")
            w.setpoint_after = after
        w.setpoint_changed = w.setpoint_changed or setpoint_changed
        window = self._sys.get("reactive_window_seconds", 120)
        rs.reactive_deadline = datetime.now(self.tz) + timedelta(seconds=window)
        if rs.reactive_task is None or rs.reactive_task.done():
            rs.reactive_task = asyncio.create_task(self._flush_reactive_after(rs))

    def _is_our_command(self, room_id: str, unit_id: str, was: control.Climate,
                        now_shows: control.Climate, attrs: dict, min_delta: float) -> bool:
        """The manual-change automation's test (app.control.matches_command):
        does the unit now show the command its state helper implies? Reads
        the helpers from the state cache and setpoint_applied from the
        almanac for the room's section."""
        state = self._cache.get(state_id(room_id, unit_id))
        if state not in control.STATES:
            return False            # no state helper yet: every change is a person's
        rs = self.rooms[room_id]
        section = (self.store.current_almanac(room_id, as_of=self._today())["sections"]
                   .get(rs.section or "") or {})
        applied = ((section.get("units") or {}).get(unit_id) or {}).get("setpoint_applied")
        unit = control.Unit(state=state, offset=int(_number(self._cache.get(
            offset_id(room_id, unit_id)))), base=_number(self._cache.get(
                base_id(room_id, unit_id))) or None)
        ctx = control.Context(
            setpoint_applied=applied,
            unit_min=attrs.get("min_temp") or control.DEFAULT_MIN_TEMP,
            unit_max=attrs.get("max_temp") or control.DEFAULT_MAX_TEMP,
            config=control.ControlConfig.from_config(self.config))
        return control.matches_command(control.implied_command(unit, ctx), was, now_shows,
                                       min_delta)

    def _track_dropout(self, entity_id: str, room_id: str, unit_id: str,
                       old_s: dict, new_s: dict) -> None:
        """Remember what a unit was doing when it went unavailable/unknown;
        if it comes back doing something else, log that as a control event.
        Never a reaction: nobody can say who changed it."""
        old_mode, new_mode = old_s.get("state"), new_s.get("state")
        if old_mode not in NO_STATE and new_mode in NO_STATE:
            self._dropouts[entity_id] = _snapshot(old_s)
            return
        if old_mode not in NO_STATE or new_mode in NO_STATE:
            return
        before = self._dropouts.pop(entity_id, None)
        if before is None:
            return      # it was already out when we started watching
        after = _snapshot(new_s)
        changed = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
        if not changed:
            return
        room = self.rooms[room_id].room
        name = next((u.get("name", u["id"]) for u in room["units"] if u["id"] == unit_id), unit_id)
        what = ", ".join(f"{k} {a} -> {b}" for k, (a, b) in changed.items())
        ts, ts_utc, _, _ = self._now()
        self.store.log_event(
            ts, ts_utc, "warning", "control",
            f"{name} came back from {old_mode} changed ({what}); not counted as a reaction",
            room_id=room_id,
            detail={"unit": unit_id, "was": old_mode, "before": before, "after": after})

    async def _flush_reactive_after(self, rs: RoomState) -> None:
        try:
            while True:
                now = datetime.now(self.tz)
                if rs.reactive_deadline and now >= rs.reactive_deadline:
                    break
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            return
        await self._flush_reactive(rs)

    async def _flush_reactive(self, rs: RoomState) -> None:
        room_id = rs.room["id"]
        windows = dict(rs.reactive_units)
        rs.reactive_units = {}
        rs.reactive_deadline = None
        if not windows:
            return
        # Fetch fresh states so any virtual (unit-attribute) sensors can read
        # the reference unit's current_temperature snapshot at reaction time.
        states = {s["entity_id"]: s for s in await self.rest.states()}
        sensors = {s["id"]: self._sensor_reading(s, rs.room, states) for s in rs.room["sensors"]}
        ts, ts_utc, local_date, now = self._now()
        window = self._sys.get("reactive_window_seconds", 120)
        units = {uid: w.sample() for uid, w in windows.items()}
        kind = room_kind(u.kind for u in units.values())
        self.store.record_reactive(room_id, ts, ts_utc, local_date, rs.section or "day",
                                   window, units, sensors, kind=kind)

        def told(uid: str, w: UnitWindow) -> str:
            if w.kind in ("on", "off"):
                return f"turned {uid} {w.kind}"
            if w.kind == "setpoint":
                return f"set {uid} {w.setpoint_before} -> {w.setpoint_after}"
            return (f"changed {uid} {w.mode_before}/{w.fan_before} -> "
                    f"{w.mode_after}/{w.fan_after} (manual: logged, not learned)")
        self.store.log_event(
            ts, ts_utc, "info", "reactive",
            f"{rs.room['name']}: a person {'; '.join(told(u, w) for u, w in windows.items())}",
            room_id=room_id,
            detail={"kind": kind, "units": {u: {**w.sample().__dict__,
                                                "mode_before": w.mode_before,
                                                "mode_after": w.mode_after,
                                                "fan_before": w.fan_before,
                                                "fan_after": w.fan_after}
                                            for u, w in windows.items()}})
        log.info("reactive (%s) recorded for %s: %s", kind, room_id, list(units))
        await self.replay_room(room_id, "reaction")
        if is_setpoint_moving(kind, {u: (v.setpoint_before, v.setpoint_after)
                                     for u, v in units.items()}):
            self._schedule_comfort_replay(room_id, now + COMFORT_REPLAY_DELAY)

    # ---- heartbeat ------------------------------------------------

    def _sensor_value(self, entity_id: str) -> float | None:
        v = self._cache.get(entity_id)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _sensor_entity(self, sensor: dict, room: dict) -> str | None:
        """The entity whose reports a sensor's readings come from: its own,
        or for a virtual sensor its unit's."""
        source = sensor.get("source")
        if source is None:
            return sensor.get("entity_id")
        unit = next((u for u in room["units"] if u["id"] == source.get("unit_id")), None)
        return unit["entity_id"] if unit else None

    def _stale_since(self, sensor: dict, room: dict, states: dict) -> datetime | None:
        """When Home Assistant last reported the sensor's entity, if that is
        longer ago than system.sensor_stale_minutes (control.is_stale); else
        None. A sensor put away can keep its last value for days without
        going unavailable."""
        entity = self._sensor_entity(sensor, room)
        reported = _parse_ts(((states.get(entity) or {}) if entity else {}).get("last_reported"))
        minutes = self._sys.get("sensor_stale_minutes", control.DEFAULT_STALE_MINUTES)
        return reported if control.is_stale(reported, self._now()[3], minutes) else None

    def _note_staleness(self, rs: RoomState, states: dict) -> None:
        """One warning event when a sensor goes stale, one when it reports
        again. What was last logged survives a restart (start() reads it
        back from the event log), so a restart doesn't repeat the warning."""
        room = rs.room
        minutes = self._sys.get("sensor_stale_minutes", control.DEFAULT_STALE_MINUTES)
        for sensor in room["sensors"]:
            key = (room["id"], sensor["id"])
            since = self._stale_since(sensor, room, states)
            if (since is not None) == self._stale.get(key, False):
                continue
            self._stale[key] = since is not None
            entity = self._sensor_entity(sensor, room)
            name = f"{room['name']}: {sensor.get('name', sensor['id'])} ({entity})"
            ts, ts_utc, _, now = self._now()
            if since is not None:
                message = (f"{name} last reported {_ago(now - since)} ago, at "
                           f"{since.astimezone(self.tz).strftime('%Y-%m-%d %H:%M')}; past "
                           f"{minutes} minutes it counts as unavailable - no vote, no "
                           "learning - until it reports again")
            else:
                message = f"{name} is reporting again; it votes and learns again"
            self.store.log_event(
                ts, ts_utc, "warning" if since is not None else "info", "validation", message,
                room_id=room["id"],
                detail={"sensor": sensor["id"], "entity_id": entity, "stale": since is not None,
                        "last_reported": since.isoformat() if since else None,
                        "stale_minutes": minutes})

    def _sensor_reading(self, sensor: dict, room: dict, states: dict) -> float | None:
        """Physical sensor -> read its own entity state (cheap path via the
        cache when we don't have fresh attributes). Virtual (source-based)
        sensor -> read the referenced unit's attribute out of a states dict,
        which the caller has just fetched. The rest of the trust model
        doesn't need to know which is which. A stale sensor reads None, like
        an unavailable one, so heartbeats and reaction snapshots - and so
        seeds, comfort windows and bands - leave it out."""
        if self._stale_since(sensor, room, states) is not None:
            return None
        source = sensor.get("source")
        if source is None:
            return self._sensor_value(sensor["entity_id"])
        if source.get("type") == "unit_attribute":
            uid = source.get("unit_id")
            unit = next((u for u in room["units"] if u["id"] == uid), None)
            if unit is None:
                return None
            attrs = (states.get(unit["entity_id"], {}) or {}).get("attributes", {}) or {}
            v = attrs.get(source.get("attribute", "current_temperature"))
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        return None

    def _unit_sample(self, entity_id: str, leak_on: bool, attrs: dict,
                     control_state: str | None, control_offset: int | None) -> UnitSample:
        state = self._cache.get(entity_id)
        is_on = state not in OFFISH
        hvac = state if state not in ("unavailable", "unknown", None) else None
        return UnitSample(
            is_on=is_on, hvac_mode=hvac, fan_mode=attrs.get("fan_mode"),
            setpoint=attrs.get("temperature"), current_temp=attrs.get("current_temperature"),
            ac_state=_infer_ac_state(hvac, attrs.get("fan_mode"), leak_on),
            control_state=control_state, control_offset=control_offset)

    async def _do_heartbeat(self, rs: RoomState) -> None:
        room_id = rs.room["id"]
        deferred_ms = 0
        waited = 0
        while self._cache.get(guard_id(room_id)) == "on" and waited < GUARD_DEFER_MAX_SECONDS:
            await asyncio.sleep(GUARD_DEFER_POLL_SECONDS)
            waited += GUARD_DEFER_POLL_SECONDS
            deferred_ms = waited * 1000

        # pull fresh attributes for units (setpoint/current_temp aren't in the
        # cheap state cache, which only holds the state string)
        states = {s["entity_id"]: s for s in await self.rest.states()}
        for eid, s in states.items():
            self._cache[eid] = s.get("state")

        self._note_staleness(rs, states)
        sensors = {s["id"]: self._sensor_reading(s, rs.room, states) for s in rs.room["sensors"]}
        units = {}
        for u in rs.room["units"]:
            attrs = (states.get(u["entity_id"], {}) or {}).get("attributes", {}) or {}
            leak_on = self._cache.get(f"input_boolean.ac_leak_{room_id}_{u['id']}") == "on"
            # the unit's control helpers, as maintenance last left them
            state = self._cache.get(state_id(room_id, u["id"]))
            offset = _number(self._cache.get(offset_id(room_id, u["id"])), None)
            units[u["id"]] = self._unit_sample(
                u["entity_id"], leak_on, attrs,
                state if state in control.STATES else None,
                None if offset is None else int(offset))

        ts, ts_utc, local_date, _ = self._now()
        section = rs.section or "day"
        self.store.record_heartbeat(room_id, ts, ts_utc, local_date,
                                    section, sensors, units,
                                    deferred_ms=deferred_ms)
        reason = self._seed_reason(room_id, section, sensors, units)
        if reason:
            await self.replay_room(room_id, reason)

    def _seed_reason(self, room_id: str, section: str, sensors: dict, units: dict) -> str | None:
        """Why this heartbeat should trigger a replay, if it can seed
        something the almanac in force lacks: the section itself, or a unit
        (on, with a setpoint) or sensor (with a reading) not in it yet."""
        alm = self.store.current_almanac(room_id, as_of=self._today())["sections"].get(section)
        known_units = set((alm or {}).get("units", {}))
        known_sensors = set((alm or {}).get("sensors", {}))
        seedable = any(u.is_on and u.setpoint is not None and uid not in known_units
                       for uid, u in units.items()) or \
            any(t is not None and sid not in known_sensors for sid, t in sensors.items())
        if not seedable:
            return None
        return "first heartbeat in section" if alm is None else "new seed in section"

    # ---- crossover / control --------------------------------------

    async def _crossover(self, rs: RoomState, section: str, started, catch_up: bool) -> None:
        room_id = rs.room["id"]
        ts, ts_utc, local_date, now = self._now()
        # A run is dated by when it started, so a catch-up after midnight
        # into a sleep that began yesterday is still yesterday's sleep run.
        run_date = started.date().isoformat() if started else local_date

        # Close whatever run the room still has open - found by being open,
        # not by date, so a sleep run that crossed midnight closes too. A
        # catch-up restart inside the same run leaves that run open.
        self.store.close_section_run(room_id, ts, keep=(run_date, section))

        # On startup catch-up, the world may already be in the right section
        # (a plain restart, not real downtime). If HA's scene select already
        # shows this section, don't re-fire the scene - re-driving the units
        # is needless, and it makes the ACs beep on every restart. A genuine
        # missed crossover (select disagrees) still re-fires, which is the
        # point of catch-up. There is no provisional seeding here any more:
        # a section with no almanac is seeded by the replay its first
        # heartbeat triggers, and until then the scene leaves the units alone.
        already_here = self._cache.get(scene_select_id(room_id)) == section
        refire = not (catch_up and already_here)

        if refire:
            entity = self._automation_entity.get(scene_automation_id(room_id, section))
            if entity:
                await self._safe_service("automation", "trigger", {"entity_id": entity})
            else:
                await self._safe_service("input_select", "select_option",
                                         {"entity_id": scene_select_id(room_id), "option": section})

        self.store.record_section_run(
            room_id, run_date, section,
            planned_start=started.isoformat(timespec="seconds") if started else None,
            actual_start=ts,
            outcome="caught_up" if catch_up else "ran")
        note = " (catch-up)" if catch_up else ""
        if not refire:
            note = " (catch-up, already in section - not re-fired)"
        self.store.log_event(ts, ts_utc, "info", "crossover",
                             f"{rs.room['name']} -> {section}{note}", room_id=room_id)
        rs.section, rs.section_started = section, started
        log.info("crossover %s -> %s%s", room_id, section, note)

    # ---- almanac push ---------------------------------------------

    def _today(self) -> date:
        """Today in HA's time zone, the one the replay dates almanacs in."""
        return date.fromisoformat(self._now()[2])

    async def _push_almanac(self, room_id: str) -> None:
        alm = self.store.current_almanac(room_id, as_of=self._today())
        sections = alm.get("sections", {})
        await self._safe_set_state(
            almanac_id(room_id),
            state=str(len(sections)),
            attributes={"sections": sections, "room_id": room_id,
                        "updated": self._now()[0]})

    # ---- learning -------------------------------------------------

    async def replay_room(self, room_id: str, reason: str) -> None:
        """Re-run the reaction replay for one room as of now, publish it and
        push it to HA. A failure is logged and leaves the almanac in force."""
        rs = self.rooms[room_id]
        ts, ts_utc, _, now = self._now()
        try:
            sections = analyse_room(self.store._conn, rs.room, self._learning_config(), now)
            self.store.publish_almanac(room_id, sections)
            await self._push_almanac(room_id)
        except Exception as e:
            log.warning("replay for %s (%s) failed: %s", room_id, reason, e)
            self.store.log_event(ts, ts_utc, "warning", "analysis",
                                 f"replay for {rs.room['name']} ({reason}) failed: {e}",
                                 room_id=room_id, detail={"reason": reason})
            return
        self.store.log_event(ts, ts_utc, "info", "analysis",
                             f"replayed almanac for {rs.room['name']} ({reason}): "
                             f"{len(sections)} section(s)", room_id=room_id,
                             detail={"reason": reason,
                                     "sections": [s.section for s in sections]})

    def _schedule_comfort_replay(self, room_id: str, due: datetime) -> None:
        """Replay the room at `due`, once a reaction's comfort window has
        closed. Pending replays are lost on restart; start() re-arms them."""
        async def later() -> None:
            delay = (due - datetime.now(self.tz)).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
            await self.replay_room(room_id, "comfort window")
        task = asyncio.create_task(later(), name=f"ac-comfort-{room_id}")
        self._followups.add(task)
        task.add_done_callback(self._followups.discard)

    async def run_analysis(self, reason: str = "nightly") -> None:
        for room_id in self.rooms:
            await self.replay_room(room_id, reason)

    # ---- loops ----------------------------------------------------

    async def _scheduler_loop(self) -> None:
        while not self._stopping:
            try:
                _, _, _, now = self._now()
                for rs in self.rooms.values():
                    section, started = active_section_at(rs.profile, now, self.tz)
                    if section != rs.section:
                        await self._crossover(rs, section, started, catch_up=False)
            except Exception as e:
                log.warning("scheduler tick error: %s", e)
            await asyncio.sleep(SCHEDULER_TICK_SECONDS)

    async def _heartbeat_loop(self) -> None:
        interval = self._sys.get("heartbeat_interval_minutes", 10) * 60
        while not self._stopping:
            await asyncio.sleep(interval)
            for rs in list(self.rooms.values()):
                try:
                    await self._do_heartbeat(rs)
                except Exception as e:
                    log.warning("heartbeat error for %s: %s", rs.room["id"], e)

    async def _almanac_push_loop(self) -> None:
        while not self._stopping:
            for room_id in list(self.rooms):
                try:
                    await self._push_almanac(room_id)
                except Exception as e:
                    log.warning("almanac push error for %s: %s", room_id, e)
            await asyncio.sleep(ALMANAC_PUSH_SECONDS)

    async def _nightly_loop(self) -> None:
        while not self._stopping:
            now = datetime.now(self.tz)
            target = now.replace(hour=0, minute=15, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            if self._stopping:
                return
            try:
                await self.run_analysis()
            except Exception as e:
                log.warning("nightly analysis error: %s", e)

    # ---- safe HA wrappers -----------------------------------------

    async def _safe_service(self, domain: str, service: str, data: dict) -> None:
        try:
            await self.rest.call_service(domain, service, data)
        except Exception as e:
            log.warning("service %s.%s failed: %s", domain, service, e)

    async def _safe_set_state(self, entity_id: str, state: str, attributes: dict) -> None:
        try:
            await self.rest.set_state(entity_id, state, attributes)
        except Exception as e:
            log.warning("set_state %s failed: %s", entity_id, e)
