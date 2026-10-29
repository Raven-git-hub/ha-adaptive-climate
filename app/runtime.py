"""
Adaptive Climate - runtime.

Ties the HA client, scheduler and store together into a live system.

Observation (passive):
  * heartbeat loop - every heartbeat_interval, sample each room's sensors
    and units and record them. Defers (does not skip) while a room's guard
    is on, so it never samples a unit mid-transition.
  * a person's votes - an ac_person_vote subscription. Home Assistant's
    vote automation (a too_cold / too_hot button press) and manual-change
    automation (a setpoint change on a remote) decide whether a vote is
    accepted, one per episode, and say so in the event; the runtime judges
    nothing itself. An accepted vote is recorded straight away as a
    reaction of kind too_cold / too_hot with every sensor's reading, and
    the room replayed; an ignored one is logged as an "ignored press" and
    teaches nothing.
  * reactive detection - a state_changed subscription; a person's change
    to a unit (NOT automation-caused) - turning it on or off, or a mode or
    fan change while on - opens a consolidation window, snapshots every
    sensor, and records one reactive event of kind on, off or manual. A
    setpoint change alone is a vote, which only HA's event records, so one
    physical action is one reaction. HA's manual-change automation applies
    the same change to the unit's control helpers; the runtime never raises
    ac_hold, which is only a manual pause. A unit dropping out to
    unavailable/unknown and back is never a reaction; if it comes back
    changed, that is logged as a control event.

Learning (docs/V2_DESIGN.md, Rebuilding) - replay_room() re-runs the
reaction replay for a room and pushes the result:
  * at startup, for every room, before any crossover (catch-up included);
  * right after each recorded reaction (a vote's at once, so HA's next
    heartbeat already sees its comfort move);
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
    attributes, with the room's learned block margin; API-set states do not
    survive an HA restart, so re-push on a timer, on every websocket
    (re)connect and on homeassistant_start. The conditions entity likewise.

Outdoor input and conditions (docs/V2_DESIGN.md, Outdoor temperature):
  * with outdoor.source 'forecast', weather.get_forecasts (hourly) for
    outdoor.weather_entity is fetched at startup and then hourly, and the
    latest result cached with its fetch time;
  * at each crossover the room's conditions are decided once for the
    section (phase 5): the room comfort point, the forecast's mean and max
    over [planned start, next section's planned start), the lean of the
    vote edges and the cooling block (with hysteresis on the run before),
    published to sensor.ac_conditions_<room> - before the scene fires
    whenever the forecast cache is under 2 hours old (or there is no
    forecast to fetch), so the scene starts from them. Otherwise the scene
    fires on fail-open conditions and a follow-up task fetches the forecast
    and publishes the real ones, which the next heartbeat's maintenance
    reads; a slow or failed fetch never holds up a crossover. The run
    records the mean and max, the comfort point, the lean, the decision and
    the block_mode, with one `outdoor` and one `block` event. A restart
    inside a run keeps what it decided;
  * every heartbeat and reaction records the weather entity's current
    temperature as outdoor_temp (None if unavailable or not updated for 3
    hours);
  * no outdoor value fails open (no lean, no block): one `outdoor` warning
    per failure streak (or for outdoor.source none or probe) and one info
    event when the forecast works again.
  The probe source isn't read yet. The margin is learned by the replay
  (app.analyser.learn_margin) and logged as a `block` event when it moves.

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
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app import control, outdoor
from app.analyser import (LearningConfig, MarginConfig, MarginStep, analyse_room,
                          is_setpoint_moving, learn_margin)
from app.config import ConfigError, validate
from app.generator import (PERSON_VOTE_EVENT, almanac_id, base_id, conditions_id, guard_id,
                           offset_id, scene_automation_id, scene_select_id, state_id)
from app.ha import HATimeoutError
from app.scheduler import active_section_at, section_window
from app.store import ReactiveUnitSample, UnitSample

log = logging.getLogger("adaptive_climate.runtime")

SCHEDULER_TICK_SECONDS = 20
ALMANAC_PUSH_SECONDS = 300
GUARD_DEFER_POLL_SECONDS = 2
GUARD_DEFER_MAX_SECONDS = 60
# The comfort window closes 60 minutes after a reaction; replay 5 later.
COMFORT_REPLAY_DELAY = timedelta(minutes=65)
# The forecast loop fetches when the cached forecast is this old: hourly.
FORECAST_REFRESH = timedelta(hours=1)
FORECAST_TIMEOUT_SECONDS = 20
# An attempt this soon after a failed one shares its failure, so rooms
# crossing together make one attempt between them.
FORECAST_SHARED = timedelta(seconds=60)
OFFISH = ("off", "unavailable", "unknown", None)
NO_STATE = ("unavailable", "unknown", None)     # the unit isn't reporting at all
HA_START_EVENT = "homeassistant_start"


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


def _round2(value: float) -> float:
    """To 2 decimals, a negative margin that rounds to zero as 0.0, not -0.0."""
    return round(value, 2) + 0.0


def _deg(value: float) -> str:
    """A margin for the log: to 2 decimals, trailing zeros dropped down to
    one (6.0, 6.55, 6.06, -1.0, -0.63; never -0.0)."""
    text = f"{_round2(value):.2f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def _accepted(data: dict) -> bool:
    """ac_person_vote's accepted flag, which a template may render as a
    string."""
    return data.get("accepted") is True or str(data.get("accepted")).lower() == "true"


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
    conditions: control.Conditions | None = None    # as published for the section
    conditions_at: str | None = None                 # when they were computed (or re-read)


@dataclass(frozen=True)
class SectionOutdoor:
    """A section run's forecast values, and what to record and say about
    them once the run's row exists."""
    mean: float | None = None
    max: float | None = None
    record: bool = False            # new values for the run (not kept from its start)
    severity: str | None = None     # the `outdoor` event, if any
    message: str | None = None
    detail: dict = field(default_factory=dict)


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
        self._forecast: outdoor.Forecast | None = None       # the latest forecast fetched
        self._forecast_lock = asyncio.Lock()                 # one fetch at a time
        self._forecast_failed_at: datetime | None = None     # the last failed attempt
        self._forecast_failing = False                       # in a failure streak, as last logged
        self._margins: dict[str, float] = {}                 # room -> the replay's block margin
        self.last_votes: dict[str, dict] = {}                # room -> HA's latest ac_person_vote
        self._vote_seq = 0                                   # counts them, for the vote endpoint
        self._stopping = False

    # ---- config helpers -------------------------------------------

    def _learning_config(self) -> LearningConfig:
        return LearningConfig.from_config(self.config)

    @property
    def _sys(self) -> dict:
        return self.config.get("system", {})

    @property
    def _weather_entity(self) -> str | None:
        """outdoor.weather_entity when the outdoor source is the forecast,
        else None (no forecast is read)."""
        out = self.config.get("outdoor") or {}
        return out.get("weather_entity") if out.get("source") == "forecast" else None

    @property
    def _outdoor(self) -> dict:
        return self.config.get("outdoor") or {}

    def margin(self, room_id: str) -> float:
        """The room's block margin as the latest replay learned it."""
        return self._margins.get(room_id, MarginConfig.from_config(self.config).initial)

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
        await self._subscribe()

        # Which sensors were last logged as stale, so a restart doesn't
        # repeat the warning for one that still is.
        for e in reversed(self.store.recent_events(limit=1000, category="validation")):
            d = e.get("detail") or {}
            if "stale" in d and "sensor" in d:
                self._stale[(e["room_id"], d["sensor"])] = bool(d["stale"])
        # Whether the forecast was last logged as failing, likewise.
        for e in reversed(self.store.recent_events(limit=1000, category="outdoor")):
            d = e.get("detail") or {}
            if "failing" in d:
                self._forecast_failing = bool(d["failing"])
        source = self._outdoor.get("source", "none")
        if source in ("probe", "none"):
            # No outdoor value at all: every section fails open. One warning
            # per streak, as for a failing forecast.
            self._note_forecast(source, None)

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
        if self._weather_entity:
            # The catch-up crossovers' follow-ups may be fetching already;
            # the loop's first pass then finds that forecast and waits.
            self._tasks.append(asyncio.create_task(self._forecast_loop(), name="ac-forecast"))
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

    async def _subscribe(self) -> None:
        await self.ws.subscribe_states(self._on_state_change)
        await self.ws.subscribe_events(PERSON_VOTE_EVENT, self._on_person_vote)
        await self.ws.subscribe_events(HA_START_EVENT, self._on_ha_start)

    async def _resubscribe(self) -> None:
        """Re-run every websocket subscription after a (re)connect. HA's
        subscription ids do not survive a dropped socket, so the state
        stream and the vote events must be re-established or reactive
        detection stays dead. A drop is usually Home Assistant restarting,
        which loses the states we set, so the almanac and the conditions are
        pushed again too."""
        await self._subscribe()
        await self._republish()

    def _on_ha_start(self, event: dict) -> None:
        """homeassistant_start: re-push what an HA restart loses."""
        task = asyncio.create_task(self._republish(), name="ac-republish")
        self._followups.add(task)
        task.add_done_callback(self._followups.discard)

    async def _republish(self) -> None:
        """Push every room's almanac and conditions again."""
        for room_id in list(self.rooms):
            try:
                await self._push_almanac(room_id)
                await self._push_conditions(room_id)
            except Exception as e:
                log.warning("republish for %s failed: %s", room_id, e)

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
        if control.classify_change(was, now_shows, min_delta) == "setpoint":
            return      # a vote: HA's manual-change automation reports it (_on_person_vote)
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
            if setpoint_changed:
                # only a change that moved it (an on at a new setpoint): not
                # a vote's setpoint, which HA's event has recorded already
                w.setpoint_after = after
        w.setpoint_changed = w.setpoint_changed or setpoint_changed
        window = self._sys.get("reactive_window_seconds", 120)
        rs.reactive_deadline = datetime.now(self.tz) + timedelta(seconds=window)
        if rs.reactive_task is None or rs.reactive_task.done():
            rs.reactive_task = asyncio.create_task(self._flush_reactive_after(rs))

    def _on_person_vote(self, event: dict) -> None:
        """An ac_person_vote event from Home Assistant's vote or
        manual-change automation. The latest per room is kept (numbered) for
        the Now view, which shows a press's outcome."""
        data = event.get("data") or {}
        if data.get("room") in self.rooms:
            self._vote_seq += 1
            self.last_votes[data["room"]] = {
                "seq": self._vote_seq, "direction": data.get("direction"),
                "accepted": _accepted(data), "source": data.get("source"),
                "unit": data.get("unit"), "at": self._now()[0]}
        task = asyncio.create_task(self._record_vote(event))
        self._followups.add(task)
        task.add_done_callback(self._followups.discard)

    async def _record_vote(self, event: dict) -> None:
        """Record an accepted vote as a reaction (too_cold / too_hot, every
        sensor's reading, a remote's unit and chosen setpoint) and replay the
        room at once; log an ignored one. HA decided which it is."""
        data = event.get("data") or {}
        room_id, direction = data.get("room"), data.get("direction")
        source, unit_id, chosen = data.get("source"), data.get("unit"), data.get("chosen")
        accepted = _accepted(data)
        ts, ts_utc, local_date, now = self._now()
        rs = self.rooms.get(room_id)
        if rs is None or direction not in control.PERSON_VOTES:
            self.store.log_event(ts, ts_utc, "warning", "reactive",
                                 f"a vote event this config can't place: room {room_id!r}, "
                                 f"direction {direction!r}; not recorded", detail=data)
            return
        name = rs.room.get("name", room_id)
        via = f"{unit_id}'s remote, to {chosen}" if source == "remote" else "the button"
        if not accepted:
            self.store.log_event(
                ts, ts_utc, "info", "reactive",
                f"{name}: ignored press: {direction} from {via}, during a forced rest or "
                "boost (one correction per episode); nothing changed or learned",
                room_id=room_id, detail={"vote": data, "ignored": True})
            return
        states = {s["entity_id"]: s for s in await self.rest.states()}
        sensors = {s["id"]: self._sensor_reading(s, rs.room, states) for s in rs.room["sensors"]}
        units = {}
        if source == "remote" and unit_id in {u["id"] for u in rs.room["units"]}:
            units[unit_id] = ReactiveUnitSample(None, _number(chosen, None), True,
                                                kind=direction)
        try:
            self.store.record_reactive(room_id, ts, ts_utc, local_date, rs.section or "day",
                                       0, units, sensors, kind=direction,
                                       outdoor_temp=self._outdoor_temp(states))
        except sqlite3.IntegrityError as e:
            self.store.log_event(ts, ts_utc, "warning", "reactive",
                                 f"{name}: a {direction} vote from {via} came in the same second "
                                 f"as another reaction and was not recorded ({e})",
                                 room_id=room_id, detail={"vote": data})
            return
        self.store.log_event(ts, ts_utc, "info", "reactive",
                             f"{name}: a person voted {direction} ({via})", room_id=room_id,
                             detail={"kind": direction, "vote": data, "sensors": sensors})
        log.info("vote (%s, %s) recorded for %s", direction, source, room_id)
        await self.replay_room(room_id, "vote")
        self._schedule_comfort_replay(room_id, now + COMFORT_REPLAY_DELAY)

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
                                   window, units, sensors, kind=kind,
                                   outdoor_temp=self._outdoor_temp(states))

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
                                    deferred_ms=deferred_ms,
                                    outdoor_temp=self._outdoor_temp(states))
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

        # The section's conditions go out before its scene whenever they need
        # no forecast fetch (the cache is under 2 hours old, there is no
        # forecast to read, or a restart keeps what the run already has), so
        # the scene starts from them. A fetch never holds a crossover up: the
        # scene then starts on fail-open conditions, and a follow-up
        # publishes the real ones for the next heartbeat's maintenance.
        kept = self.store.section_run_conditions(room_id, run_date, section) if catch_up \
            else None
        decided = kept is not None and kept["block_mode"] is not None
        ov = self._kept_outdoor(kept)
        pending = False
        if decided:
            cond = self._kept_conditions(rs, section, started, kept)
        elif ov is None and self._weather_entity and started is not None \
                and not self._forecast_fresh():
            pending = True
            cond = control.Conditions(section, self._block_mode, self.margin(room_id),
                                      fail_open=control.NO_OUTDOOR)
        else:
            if ov is None:
                ov = self._section_outdoor(rs, section, started, self._forecast)
            cond = await self._decide(rs, run_date, section, started, ov)
        await self._publish_conditions(rs, cond)

        if refire:
            entity = self._automation_entity.get(scene_automation_id(room_id, section))
            if entity:
                await self._safe_service("automation", "trigger", {"entity_id": entity})
            else:
                await self._safe_service("input_select", "select_option",
                                         {"entity_id": scene_select_id(room_id), "option": section})

        start, start_detail = await self._section_start(rs, section, cond, pending) \
            if refire else ("", None)

        self.store.record_section_run(
            room_id, run_date, section,
            planned_start=started.isoformat(timespec="seconds") if started else None,
            actual_start=ts,
            outcome="caught_up" if catch_up else "ran")
        note = " (catch-up)" if catch_up else ""
        if not refire:
            note = " (catch-up, already in section - not re-fired)"
        self.store.log_event(ts, ts_utc, "info", "crossover",
                             f"{rs.room['name']} -> {section}{note}{start}", room_id=room_id,
                             detail=start_detail)
        rs.section, rs.section_started = section, started
        log.info("crossover %s -> %s%s%s", room_id, section, note, start)
        if pending:
            # After the scene has fired, so the forecast never holds it up.
            task = asyncio.create_task(
                self._conditions_later(rs, run_date, section, started),
                name=f"ac-outdoor-{room_id}")
            self._followups.add(task)
            task.add_done_callback(self._followups.discard)
        else:
            self._settle_section(rs, run_date, ov, cond, kept=decided)

    async def _section_start(self, rs: RoomState, section: str, cond: control.Conditions,
                             pending: bool) -> tuple[str, dict]:
        """How the scene starts the room's units, for the crossover event:
        control.section_start from what the scene reads - the sensors now,
        against the section's almanac and the conditions just published
        (their edges and delta_mean), and the heat sources. The scene decides
        it again in Home Assistant from the same values; a unit the scene
        forces off, latched by a leak or with no almanac yet starts as
        before. (": starts in rest: why", detail)."""
        room = rs.room
        try:
            states = {s["entity_id"]: s for s in await self.rest.states()}
        except Exception as e:
            log.warning("section start for %s: states fetch failed (%s); using the cache",
                        room["id"], e)
            states = {}
        for eid, s in states.items():
            self._cache[eid] = s.get("state")
        readings = {s["id"]: self._sensor_reading(s, room, states) for s in room["sensors"]}
        alm = self.store.current_almanac(room["id"], as_of=self._today())["sections"].get(section)
        votes = control.evaluate_votes(readings, alm, cond.cold_edge, cond.warm_edge)
        heat = [e for e in room.get("heat_sources") or [] if self._cache.get(e) == "on"]
        ctx = control.Context(setpoint_applied=None, blocked=cond.blocked,
                              heat_source_on=bool(heat), block_mode=cond.block_mode,
                              delta_mean=cond.delta_mean)
        state, why = control.section_start(votes, ctx)
        usable = sum(p is not None for p in votes.positions.values())
        need = max(votes.required, 1)
        sensors = f"{votes.cold_side} of {usable} sensors below their comfort readings " \
                  f"({need} needed)"
        mean = None if cond.outdoor_mean is None else \
            f"the forecast mean {cond.outdoor_mean:.1f}"
        cp = None if cond.comfort_point is None else f"the comfort point {cond.comfort_point:.1f}"
        reason = {
            control.START_BLOCKED: "cooling is blocked",
            control.START_HEAT_SOURCE: f"{', '.join(heat)} {'is' if len(heat) == 1 else 'are'} on",
            control.START_COLD: f"{sensors}, and {mean} is below {cp}",
            control.START_NOT_COOLER: f"{mean} is not below {cp}",
            control.START_NOT_COLD: sensors,
            control.START_FAIL_OPEN:
                "no forecast yet (fetching it), so no cold start: fail open" if pending
                else "no comfort point, so no cold start: fail open"
                if cond.fail_open == control.NO_COMFORT_POINT
                else "no outdoor value, so no cold start: fail open"}[why]
        return f": starts in {state}: {reason}", {
            "section": section, "start": state, "why": why, "cold_side": votes.cold_side,
            "usable": usable, "needed": need, "delta_mean": cond.delta_mean,
            "comfort_point": cond.comfort_point, "outdoor_mean": cond.outdoor_mean,
            "heat_sources_on": heat}

    # ---- outdoor and conditions -----------------------------------

    @property
    def _block_mode(self) -> str:
        return self._outdoor.get("block_mode", control.DEFAULT_BLOCK_MODE)

    def _forecast_fresh(self) -> bool:
        """Is the cached forecast young enough for a crossover to use
        without fetching (under 2 hours)?"""
        return self._forecast is not None and \
            self._now()[3] - self._forecast.fetched_at < outdoor.CACHE_MAX_AGE

    def _outdoor_temp(self, states: dict) -> float | None:
        """The outdoor value a heartbeat or reaction records: the weather
        entity's current temperature, or None (no forecast source, or the
        entity unavailable or not updated for 3 hours)."""
        entity = self._weather_entity
        return outdoor.current_temperature(states.get(entity), self._now()[3]) if entity else None

    @staticmethod
    def _kept_outdoor(kept: dict | None) -> SectionOutdoor | None:
        """A catch-up inside a run that already has its outdoor values keeps
        them: they were decided at its start, and the forecast no longer
        holds the hours already past."""
        if kept is None or (kept["outdoor_mean"] is None and kept["outdoor_max"] is None):
            return None
        return SectionOutdoor(kept["outdoor_mean"], kept["outdoor_max"])

    def _section_outdoor(self, rs: RoomState, section: str, started: datetime | None,
                         forecast: outdoor.Forecast | None) -> SectionOutdoor:
        """A section run's forecast mean and max over its window, [planned
        start, next section's planned start), and the event saying so. A
        window the forecast covers only part of uses what is covered and
        says so; with no forecast, or none of the window, the run gets no
        outdoor value. Nothing without a forecast source."""
        entity = self._weather_entity
        if entity is None or started is None:
            return SectionOutdoor()
        start, end = section_window(rs.profile, section, started, self.tz)
        window = outdoor.span_text(start, end, self.tz)
        head = f"{rs.room['name']} -> {section}"
        detail = {"section": section, "entity_id": entity,
                  "window": [start.isoformat(), end.isoformat()]}
        if forecast is None:
            return SectionOutdoor(severity="info", detail={**detail, "mean": None, "max": None},
                                  message=f"{head}: no forecast from {entity}, so no outdoor "
                                          f"value for {window}")
        stats = outdoor.window_stats(forecast.hours, start, end)
        detail.update(mean=stats.mean, max=stats.max, hours=stats.hours,
                      expected=stats.expected, partial=stats.partial,
                      fetched_at=forecast.fetched_at.isoformat(timespec="seconds"))
        if stats.hours == 0:
            return SectionOutdoor(severity="warning", detail=detail,
                                  message=f"{head}: the forecast from {entity} has no hours in "
                                          f"{window}, so no outdoor value")
        values = f"forecast mean {stats.mean:.1f}, max {stats.max:.1f}"
        if stats.partial:
            covered = outdoor.span_text(stats.first, min(stats.last + outdoor.HOUR, end), self.tz)
            message = (f"{head}: {values} over {covered} only ({entity}): the forecast covers "
                       f"{stats.hours} of the {stats.expected} hours in {window}")
        else:
            message = f"{head}: {values} over {window} ({entity})"
        return SectionOutdoor(stats.mean, stats.max, True, "info", message, detail)

    async def _decide(self, rs: RoomState, run_date: str, section: str,
                      started: datetime | None, ov: SectionOutdoor) -> control.Conditions:
        """The section's conditions: the room comfort point from the
        section's almanac and the sensors usable now (reporting, not
        stale), the lean from the forecast mean, and the block from its max
        with hysteresis on the room's run before (shadow decisions count)."""
        room_id = rs.room["id"]
        try:
            states = {s["entity_id"]: s for s in await self.rest.states()}
        except Exception as e:
            log.warning("conditions for %s: states fetch failed (%s); using the cache",
                        room_id, e)
            states = {}
        for eid, s in states.items():
            self._cache[eid] = s.get("state")
        readings = {s["id"]: self._sensor_reading(s, rs.room, states) for s in rs.room["sensors"]}
        alm = self.store.current_almanac(room_id, as_of=self._today())["sections"].get(section)
        was = started is not None and self.store.previous_blocked(room_id, run_date, section,
                                                                  started)
        return control.section_conditions(
            section, control.comfort_point(alm, readings), ov.mean, ov.max,
            self.margin(room_id), was, self._block_mode,
            self._outdoor.get("lean_per_degree", 0.1))

    def _kept_conditions(self, rs: RoomState, section: str, started: datetime | None,
                         kept: dict) -> control.Conditions:
        """A restart inside a run that already decided its conditions keeps
        them (locked for the section), block_mode included, rebuilt from
        what the run stored, with the margin as it stood at the run's start."""
        cp, mean, mx, lean = (kept["comfort_point"], kept["outdoor_mean"], kept["outdoor_max"],
                              kept["lean"])
        margin = self.margin(rs.room["id"]) if started is None else learn_margin(
            self.store._conn, rs.room, MarginConfig.from_config(self.config), started)[0]
        cold, warm = control.lean_edges(lean)
        fail_open = None if lean is not None else \
            control.NO_OUTDOOR if mean is None else control.NO_COMFORT_POINT
        return control.Conditions(
            section, kept["block_mode"], margin, cp, mean, mx,
            None if lean is None else round(cp - mean, 2),
            None if lean is None or mx is None else round(cp - mx, 2),
            lean, cold, warm, bool(kept["blocked"]), fail_open)

    async def _conditions_later(self, rs: RoomState, run_date: str, section: str,
                                started: datetime) -> None:
        """The follow-up to a crossover that fired on fail-open conditions:
        fetch the forecast, decide and publish the real conditions."""
        forecast = await self._forecast_for(outdoor.CACHE_MAX_AGE)
        ov = self._section_outdoor(rs, section, started, forecast)
        cond = await self._decide(rs, run_date, section, started, ov)
        if rs.section == section:                    # not overtaken by the next crossover
            await self._publish_conditions(rs, cond)
        self._settle_section(rs, run_date, ov, cond, kept=False)

    def _settle_section(self, rs: RoomState, run_date: str, ov: SectionOutdoor | None,
                        cond: control.Conditions, kept: bool) -> None:
        """Once the run's row exists: record its outdoor values and
        conditions, with one `outdoor` event (when there is something to say)
        and one `block` event."""
        room_id, section = rs.room["id"], cond.section
        ts, ts_utc, _, _ = self._now()
        if ov is not None and ov.record:
            self.store.record_section_outdoor(room_id, run_date, section, ov.mean, ov.max)
        if ov is not None and ov.message:
            self.store.log_event(ts, ts_utc, ov.severity, "outdoor", ov.message,
                                 room_id=room_id, detail=ov.detail)
        if not kept:
            self.store.record_section_conditions(room_id, run_date, section, cond.comfort_point,
                                                 cond.lean, cond.would_block, cond.block_mode)
        self.store.log_event(ts, ts_utc, "info", "block", self._block_message(rs, cond, kept),
                             room_id=room_id,
                             detail={**self._conditions_detail(cond), "run_date": run_date,
                                     "kept": kept})

    def _section_name(self, rs: RoomState, section: str) -> str:
        return next((s.get("name") for s in rs.profile.get("sections", [])
                     if s["id"] == section and s.get("name")), section.title())

    def _block_message(self, rs: RoomState, cond: control.Conditions, kept: bool) -> str:
        """One line for the Log: the decision, the values it came from, and
        the lean. "would block Night: forecast max 17.0, comfort 23.9,
        margin 6.0" in shadow mode."""
        name, sec = rs.room.get("name", rs.room["id"]), self._section_name(rs, cond.section)
        tail = " (kept from the run's start)" if kept else ""
        if cond.fail_open == control.NO_COMFORT_POINT:
            return (f"{name}: no comfort point for {sec} (no reporting sensor has a comfort "
                    f"reading yet), so no lean and no block: fail open{tail}")
        if cond.fail_open is not None:
            why = ("no forecast for the section" if self._weather_entity
                   else f"outdoor.source is {self._outdoor.get('source', 'none')}")
            return f"{name}: no outdoor value for {sec} ({why}), so no lean and no block: " \
                   f"fail open{tail}"
        values = (f"forecast max {cond.outdoor_max:.1f}, " if cond.outdoor_max is not None
                  else "") + f"comfort {cond.comfort_point:.1f}, margin {_deg(cond.margin)}"
        if cond.would_block and cond.delta_max is not None and cond.delta_max < cond.margin:
            values += (f", held from the last section until delta {cond.delta_max:.1f} is "
                       f"{_deg(cond.margin - control.BLOCK_RELEASE)} or less")
        lean = (f"lean {cond.lean:+.2f} from the forecast mean {cond.outdoor_mean:.1f}: edges "
                f"{cond.cold_edge:+.2f} / {cond.warm_edge:+.2f}")
        if cond.blocked:
            what = f"blocking {sec}"
        elif cond.would_block:
            what = f"would block {sec}"
            values += " (shadow mode: not applied)"
        elif cond.block_mode == "off":
            what = f"{sec} not blocked (block_mode off)"
        else:
            what = f"not blocking {sec}"
        return f"{name}: {what}: {values}; {lean}{tail}"

    @staticmethod
    def _conditions_detail(cond: control.Conditions) -> dict:
        return {"section": cond.section, "block_mode": cond.block_mode,
                "would_block": cond.would_block, "blocked": cond.blocked,
                "margin": None if cond.margin is None else _round2(cond.margin),
                "comfort_point": cond.comfort_point, "outdoor_mean": cond.outdoor_mean,
                "outdoor_max": cond.outdoor_max, "delta_mean": cond.delta_mean,
                "delta_max": cond.delta_max, "lean": cond.lean, "cold_edge": cond.cold_edge,
                "warm_edge": cond.warm_edge, "fail_open": cond.fail_open}

    async def _publish_conditions(self, rs: RoomState, cond: control.Conditions) -> None:
        rs.conditions, rs.conditions_at = cond, self._now()[0]
        await self._push_conditions(rs.room["id"])

    async def _push_conditions(self, room_id: str) -> None:
        """sensor.ac_conditions_<room>: state the section, attributes the
        conditions as the templates read them (cold_edge, warm_edge, and
        blocked as control should see it: active mode only), what they came
        from, and each sensor's trigger edges in degrees against the
        section's almanac now."""
        rs = self.rooms.get(room_id)
        cond = rs.conditions if rs else None
        if cond is None:
            return
        learned = (self.store.current_almanac(room_id, as_of=self._today())["sections"]
                   .get(cond.section) or {}).get("sensors") or {}
        edges = {}
        for sid, e in learned.items():
            if e.get("comfort") is not None:
                band = e.get("band") or control.DEFAULT_BAND
                edges[sid] = {"cold": round(e["comfort"] + cond.cold_edge * band, 2),
                              "warm": round(e["comfort"] + cond.warm_edge * band, 2)}
        await self._safe_set_state(
            conditions_id(room_id), state=cond.section,
            attributes={"room_id": room_id, **self._conditions_detail(cond), "edges": edges,
                        "computed_at": rs.conditions_at})

    async def _forecast_for(self, max_age: timedelta) -> outdoor.Forecast | None:
        """The cached forecast if it is younger than max_age, else a fresh
        one, or None if that fails. One fetch at a time; an attempt within
        FORECAST_SHARED of a failed one shares its failure."""
        async with self._forecast_lock:
            now = self._now()[3]
            if self._forecast is not None and now - self._forecast.fetched_at < max_age:
                return self._forecast
            if self._forecast_failed_at is not None and \
                    now - self._forecast_failed_at < FORECAST_SHARED:
                return None
            return await self._fetch_forecast()

    async def _fetch_forecast(self) -> outdoor.Forecast | None:
        """weather.get_forecasts (hourly) for the weather entity. A missing
        or unavailable entity, a service error, a timeout or an empty
        forecast fails open: None, the cache left as it was."""
        entity = self._weather_entity
        state = self._cache.get(entity)
        hours, kind, reason = [], None, None
        if state is None:
            kind, reason = "missing", f"{entity} isn't in Home Assistant"
        elif state in outdoor.NO_STATE:
            kind, reason = "unavailable", f"{entity} is {state}"
        else:
            try:
                response = await asyncio.wait_for(
                    self.rest.call_service("weather", "get_forecasts",
                                           {"entity_id": entity, "type": "hourly"},
                                           return_response=True),
                    FORECAST_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                kind, reason = "timeout", f"no answer within {FORECAST_TIMEOUT_SECONDS} s"
            except HATimeoutError as e:                  # the REST client's own timeout
                kind, reason = "timeout", str(e)
            except Exception as e:
                kind, reason = "error", f"weather.get_forecasts failed ({type(e).__name__}: {e})"
            else:
                hours = outdoor.parse_forecast(response, entity, self.tz)
                if not hours:
                    kind = "empty"
                    reason = ("the hourly forecast is empty"
                              if isinstance(response, dict) and entity in response
                              else f"the response has no forecast for {entity}")
        now = self._now()[3]
        if kind is not None:
            self._forecast_failed_at = now
            self._note_forecast(kind, reason)
            return None
        self._forecast = outdoor.Forecast(entity, now, tuple(hours))
        self._forecast_failed_at = None
        self._note_forecast(None, None, len(hours))
        return self._forecast

    def _note_forecast(self, kind: str | None, reason: str | None, hours: int = 0) -> None:
        """One warning when the forecast starts failing (kind says how), one
        info event when it works again. What was last logged survives a
        restart (start() reads it back), so a restart inside a failure
        streak doesn't repeat the warning. outdoor.source none or probe (not
        read yet) is a streak of its own kind: no outdoor value at all."""
        failing = kind is not None
        if failing == self._forecast_failing:
            return
        self._forecast_failing = failing
        entity = self._weather_entity
        ts, ts_utc, _, _ = self._now()
        if kind in ("none", "probe"):
            unread = ", which this build doesn't read yet (only the forecast is)" \
                if kind == "probe" else ""
            self.store.log_event(
                ts, ts_utc, "warning", "outdoor",
                f"outdoor.source is '{kind}'{unread}: no outdoor value is recorded, and every "
                "section fails open - no lean and no cooling block (heat sources still block)",
                detail={"failing": True, "kind": kind, "source": kind})
        elif failing:
            self.store.log_event(
                ts, ts_utc, "warning", "outdoor",
                f"Outdoor forecast from {entity} failed: {reason}. Section runs get no "
                "forecast values until it works again (a cached one under 2 hours old is still "
                "used), so their conditions fail open - no lean and no cooling block; it is "
                "retried hourly and at each section change",
                detail={"failing": True, "kind": kind, "reason": reason, "entity_id": entity})
        else:
            self.store.log_event(
                ts, ts_utc, "info", "outdoor",
                f"Outdoor forecast from {entity} is working again ({hours} hourly entries)",
                detail={"failing": False, "entity_id": entity, "hours": hours})

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
                        "margin": _round2(self.margin(room_id)),
                        "updated": self._now()[0]})

    # ---- learning -------------------------------------------------

    async def replay_room(self, room_id: str, reason: str) -> None:
        """Re-run the reaction replay for one room as of now, publish it and
        push it to HA. A failure is logged and leaves the almanac in force."""
        rs = self.rooms[room_id]
        ts, ts_utc, _, now = self._now()
        try:
            sections = analyse_room(self.store._conn, rs.room, self._learning_config(), now)
            margin, steps = learn_margin(self.store._conn, rs.room,
                                         MarginConfig.from_config(self.config), now)
            self.store.publish_almanac(room_id, sections)
            previous = self._margins.get(room_id)
            self._margins[room_id] = margin
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
        if previous is not None and steps and abs(margin - previous) > 1e-9:
            self._note_margin(rs, steps[-1])

    def _note_margin(self, rs: RoomState, step: MarginStep) -> None:
        """A `block` event when a reaction moves the room's margin."""
        name = rs.room.get("name", rs.room["id"])
        sec = self._section_name(rs, step.section)
        if step.blocked:
            was = "blocked" if step.block_mode == "active" else "would have been blocked (shadow)"
            why = (f"a {'too_hot vote' if step.kind == 'too_hot' else 'unit turned on'} in "
                   f"{sec} {step.local_date}, which {was}")
        else:
            minutes = int((step.at - step.cooling_since).total_seconds() // 60)
            why = (f"a {'too_cold vote' if step.kind == 'too_cold' else 'unit turned off'} "
                   f"{minutes} min after cooling started in {sec} {step.local_date}, unblocked")
        ts, ts_utc, _, _ = self._now()
        self.store.log_event(
            ts, ts_utc, "info", "block",
            f"{name}: block margin {_deg(step.before)} -> {_deg(step.after)}: {why} "
            f"(delta max {step.delta_max:.1f}, so toward {step.target:.1f})",
            room_id=rs.room["id"],
            detail={"margin": {"before": step.before, "after": step.after,
                               "target": step.target},
                    "kind": step.kind, "section": step.section, "run_date": step.local_date,
                    "blocked": step.blocked, "block_mode": step.block_mode,
                    "delta_max": step.delta_max,
                    "cooling_since": step.cooling_since.isoformat()
                    if step.cooling_since else None})

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
            await self._republish()
            await asyncio.sleep(ALMANAC_PUSH_SECONDS)

    async def _forecast_loop(self) -> None:
        """Fetch the forecast at startup, then hourly: whenever the cached
        one is FORECAST_REFRESH old (a crossover's own fetch counts)."""
        while not self._stopping:
            try:
                await self._forecast_for(FORECAST_REFRESH)
            except Exception as e:
                log.warning("forecast refresh error: %s", e)
            await asyncio.sleep(FORECAST_REFRESH.total_seconds())

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
