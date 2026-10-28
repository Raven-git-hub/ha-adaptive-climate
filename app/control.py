"""
Adaptive Climate - the cool-or-rest control core.

Pure functions, no I/O, no Home Assistant: the per-unit state machine of
docs/V2_DESIGN.md (Control: cool or rest). Phase 3b renders the same rules
as Jinja in the generated maintenance, scene and manual-change automations,
and tools/control_check.py replays one scenario table against both. So each
rule is a small named function, written the way a template can say it.

  evaluate_votes      positions in band widths, the pooled quorum and its
                      direction (app.trust), and the release counts
  step                one maintenance heartbeat for one unit
  on_section_change   what the scene does to a unit at a crossover
  on_person_change    what a person's change does to the unit's record (no
                      command: the person already moved the unit)
  on_climate_change   the manual-change automation: one climate state change,
                      ignored when it is our own command showing up
                      (matches_command), else classified and passed to
                      on_person_change

A unit's state lives in its helpers (phase 3b): ac_state, ac_base,
ac_offset, ac_since, ac_grace_until and ac_unblocked.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from app import trust

STATES = ("cool", "boost", "rest", "user_off", "manual", "config_off", "leak")
HANDS_OFF = ("user_off", "manual", "config_off")   # maintenance never moves these

# Vote direction: the side the room is on. app.trust names what the room
# needs instead ('cool' = needs cooling = too warm); this is the one place
# the two meet.
TOO_WARM, TOO_COLD, NONE = "too_warm", "too_cold", "none"
_FROM_TRUST = {"cool": TOO_WARM, "warm": TOO_COLD, "none": NONE}

DEFAULT_MIN_TEMP, DEFAULT_MAX_TEMP = 16.0, 30.0
DEFAULT_BAND = 5.0      # a sensor with no band yet votes like a low-trust one (v1 template)
DEFAULT_STALE_MINUTES = 90      # system.sensor_stale_minutes


# ---------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class ControlConfig:
    """system.control (config v2, phase 0)."""
    boost_degrees: float = 2.0
    boost_max_minutes: int = 60
    cool_min_heartbeats: int = 1
    rest_min_heartbeats: int = 2
    person_grace_minutes: int = 30
    offset_cap: int = 3

    @classmethod
    def from_config(cls, config: dict) -> ControlConfig:
        control = config.get("system", {}).get("control", {})
        base = cls()
        return cls(**{k: control.get(k, getattr(base, k)) for k in base.__dataclass_fields__})


@dataclass(frozen=True)
class Unit:
    """One unit's control state: its helpers."""
    state: str = "cool"
    offset: int = 0
    base: float | None = None           # 0 or None means unset: fall back to setpoint_applied
    since: datetime | None = None       # when the current state began
    grace_until: datetime | None = None  # end of a person's grace period
    unblocked: bool = False             # a person turned it on during a block


@dataclass(frozen=True)
class Command:
    hvac_mode: str                      # cool | off
    fan_mode: str | None = None
    temperature: float | None = None    # None: leave the unit's setpoint as it is


@dataclass(frozen=True)
class Context:
    """What a template knows at a heartbeat or crossover."""
    setpoint_applied: float | None      # the almanac's, for this section
    unit_min: float = DEFAULT_MIN_TEMP
    unit_max: float = DEFAULT_MAX_TEMP
    heartbeat_interval: int = 10        # minutes (system.heartbeat_interval_minutes)
    blocked: bool = False               # the section's cooling block (or a heat source)
    leak_on: bool = False               # the unit's leak latch
    forced_off: bool = False            # the section's scene forces this unit off
    min_delta: float = 0.5              # system.reactive_min_delta
    sensor_stale_minutes: int = DEFAULT_STALE_MINUTES   # system.sensor_stale_minutes
    config: ControlConfig = ControlConfig()


@dataclass(frozen=True)
class Climate:
    """What a climate entity shows: its hvac mode, setpoint and fan."""
    mode: str
    temperature: float | None = None
    fan: str | None = None


@dataclass(frozen=True)
class Votes:
    met: bool
    tally: int
    required: int
    direction: str                      # too_warm | too_cold | none
    warm_side: int                      # usable sensors above their comfort reading
    cold_side: int                      # usable sensors below it
    positions: dict = field(default_factory=dict)


# ---------------------------------------------------------------------
# Votes
# ---------------------------------------------------------------------

def is_stale(last_reported: datetime | None, now: datetime, stale_minutes: int) -> bool:
    """A sensor Home Assistant last reported more than stale_minutes ago is
    treated as unavailable: one put away can keep its last value for days
    without going unavailable. Unknown (None) is not stale."""
    return last_reported is not None and (now - last_reported).total_seconds() > stale_minutes * 60


def fresh_readings(readings: dict[str, float | None], last_reported: dict[str, datetime | None],
                   now: datetime, stale_minutes: int) -> dict[str, float | None]:
    """The readings with every stale sensor's reading taken as None, so it
    has no vote and counts toward no quorum (the maintenance template does
    the same from each entity's last_reported)."""
    return {sid: None if is_stale(last_reported.get(sid), now, stale_minutes) else value
            for sid, value in readings.items()}

def _trusts(trusts: dict[str, float]) -> dict[str, float]:
    return trusts


def evaluate_votes(sensors: dict[str, float | None], section_almanac: dict | None,
                   cold_edge: float = -1.0, warm_edge: float = 1.0) -> Votes:
    """sensors: {sensor_id: reading, or None if unavailable}. section_almanac:
    one section of the almanac as HA reads it, {"sensors": {id: {comfort,
    band, trust}}}. A sensor's position is (reading - comfort) / band; it
    votes past either edge (phase 5 leans them). The quorum and its
    trust-weighted direction are app.trust's, unchanged from v1."""
    learned = (section_almanac or {}).get("sensors") or {}
    positions: dict[str, float | None] = {}
    trusts: dict[str, float] = {}
    for sid, reading in sensors.items():
        entry = learned.get(sid) or {}
        comfort, band = entry.get("comfort"), entry.get("band") or DEFAULT_BAND
        positions[sid] = None if reading is None or comfort is None else (reading - comfort) / band
        trusts[sid] = entry.get("trust") or 0.0
    q = trust.evaluate_edges(positions, _trusts(trusts), cold_edge, warm_edge)
    usable = [p for p in positions.values() if p is not None]
    return Votes(q.met, q.tally, q.required, _FROM_TRUST[q.direction],
                 warm_side=sum(1 for p in usable if p > 0),
                 cold_side=sum(1 for p in usable if p < 0),
                 positions=positions)


# ---------------------------------------------------------------------
# Rules - one each, so a template can mirror them and a test can drop one
# ---------------------------------------------------------------------

def target_base(unit: Unit, ctx: Context) -> float | None:
    """The unit's base, or the almanac's setpoint_applied while it is unset."""
    return unit.base if unit.base else ctx.setpoint_applied


def min_time_met(unit: Unit, now: datetime, heartbeats: int, interval: int) -> bool:
    """n heartbeats in the state, met at (n - 0.5) x the interval, so a
    heartbeat that lands a little early (9:40 for 10) still counts."""
    return unit.since is None or now - unit.since >= timedelta(minutes=(heartbeats - 0.5) * interval)


def in_grace(unit: Unit, now: datetime) -> bool:
    return unit.grace_until is not None and now < unit.grace_until


def effective_blocked(unit: Unit, ctx: Context) -> bool:
    return ctx.blocked and not unit.unblocked


def boost_capped(unit: Unit, now: datetime, config: ControlConfig) -> bool:
    return unit.since is not None and now - unit.since >= timedelta(minutes=config.boost_max_minutes)


def released(side_count: int, required: int) -> bool:
    """A boost or rest ends when fewer than the quorum are still on the side
    that triggered it. The quorum counts as at least 1, so with no usable
    sensor the unit goes back to cool rather than hold a rest forever."""
    return side_count < max(required, 1)


def capped_offset(offset: int, cap: int) -> int:
    return max(-cap, min(cap, offset))


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def command_for(state: str, base: float | None, offset: int, ctx: Context) -> Command | None:
    """What the unit is told to do in a state, clamped to its range."""
    lo, hi = ctx.unit_min, ctx.unit_max
    if state == "cool":
        return Command("cool", "low", None if base is None else clamp(base + offset, lo, hi))
    if state == "boost":
        return Command("cool", "medium", None if base is None
                       else clamp(base + offset - ctx.config.boost_degrees, lo, hi))
    if state in ("rest", "config_off", "user_off", "leak"):
        return Command("off")            # leak too: dry still runs the compressor
    return None                          # manual: the person's settings stand


def _go(unit: Unit, state: str, now: datetime, ctx: Context,
        offset: int | None = None) -> tuple[Unit, Command | None]:
    """A transition: new state, since = now, and its command."""
    offset = unit.offset if offset is None else capped_offset(offset, ctx.config.offset_cap)
    new = replace(unit, state=state, offset=offset, since=now)
    return new, command_for(state, target_base(new, ctx), offset, ctx)


def _stay(unit: Unit, ctx: Context) -> tuple[Unit, Command | None]:
    """No transition, so no command: commands are never re-sent."""
    return unit, None


def _leak_step(unit: Unit, now: datetime, ctx: Context) -> tuple[Unit, Command | None] | None:
    """Leak beats everything, minimum times and grace included. Returns
    None when the leak latch has nothing to do with this heartbeat."""
    if ctx.leak_on:
        return _stay(unit, ctx) if unit.state == "leak" else _go(unit, "leak", now, ctx)
    if unit.state == "leak":                 # fixed and confirmed: as at a section change
        return on_section_change(unit, ctx, ctx.forced_off, now)
    return None


def _kept_off(power_after: bool) -> bool:
    """A setpoint change made while the unit is off keeps its state."""
    return not power_after


def _kept_manual(state: str) -> bool:
    """So does one made while a person has the unit in manual."""
    return state == "manual"


def setpoint_moved(before: float | None, after: float | None, min_delta: float) -> bool:
    """The runtime's test for a setpoint change: both known, and apart by
    at least reactive_min_delta."""
    return before is not None and after is not None and abs(after - before) >= min_delta


def implied_command(unit: Unit, ctx: Context) -> Command | None:
    """The command the unit's state stands for, which a delayed echo of
    our own command would show. None in manual (or an unknown state):
    then every change is a person's."""
    return command_for(unit.state, target_base(unit, ctx), unit.offset, ctx)


def _off_matches(before: Climate, after: Climate, min_delta: float) -> bool:
    """An off command sets no setpoint, so a setpoint change made while
    off is still a person's (decision 50)."""
    return after.mode == "off" and not setpoint_moved(before.temperature, after.temperature,
                                                      min_delta)


def matches_command(command: Command | None, before: Climate, after: Climate,
                    min_delta: float) -> bool:
    """Is this change our own command showing up? For an off command:
    the unit is off and its setpoint didn't change. Otherwise: the mode is
    the command's, and so is the setpoint, to within reactive_min_delta
    (any setpoint, if the command sets none). The fan is never compared."""
    if command is None:
        return False
    if command.hvac_mode == "off":
        return _off_matches(before, after, min_delta)
    if after.mode != command.hvac_mode:
        return False
    return command.temperature is None or _same_setpoint(after.temperature, command.temperature,
                                                         min_delta)


def _same_setpoint(shown: float | None, commanded: float, min_delta: float) -> bool:
    """Closer than a setpoint change: a unit's rounding or unit conversion
    is not a person."""
    return shown is not None and abs(shown - commanded) < min_delta


def classify_change(before: Climate, after: Climate, min_delta: float) -> str | None:
    """One change's kind, as the runtime's reaction detector names it:
    on or off if the power changed (off to any mode is on), else setpoint
    if the setpoint moved, else manual for a mode or fan change while on.
    None for anything else (current_temperature, say)."""
    power_before, power_after = before.mode != "off", after.mode != "off"
    if power_before != power_after:
        return "on" if power_after else "off"
    if setpoint_moved(before.temperature, after.temperature, min_delta):
        return "setpoint"
    if power_after and (before.mode != after.mode or before.fan != after.fan):
        return "manual"
    return None


# ---------------------------------------------------------------------
# The three entry points
# ---------------------------------------------------------------------

def step(unit: Unit, votes: Votes, now: datetime, ctx: Context) -> tuple[Unit, Command | None]:
    """One maintenance heartbeat. Returns the unit's new record and the
    command to send, which is None unless it changed state."""
    leak = _leak_step(unit, now, ctx)
    if leak is not None:
        return leak
    if unit.state in HANDS_OFF:
        return _stay(unit, ctx)
    if target_base(unit, ctx) is None:
        return _stay(unit, ctx)              # no seed yet: leave the unit alone

    cfg, interval = ctx.config, ctx.heartbeat_interval
    blocked = effective_blocked(unit, ctx)

    if unit.state == "cool":
        if in_grace(unit, now) or not min_time_met(unit, now, cfg.cool_min_heartbeats, interval):
            return _stay(unit, ctx)
        if blocked:
            return _go(unit, "rest", now, ctx)
        if votes.met and votes.direction == TOO_WARM:
            return _go(unit, "boost", now, ctx)
        if votes.met and votes.direction == TOO_COLD:
            return _go(unit, "rest", now, ctx)
        return _stay(unit, ctx)

    if unit.state == "boost":
        if not min_time_met(unit, now, cfg.cool_min_heartbeats, interval):
            return _stay(unit, ctx)
        if blocked:
            return _go(unit, "rest", now, ctx)
        if boost_capped(unit, now, cfg) or released(votes.warm_side, votes.required):
            return _go(unit, "cool", now, ctx, offset=unit.offset - 1)
        return _stay(unit, ctx)

    if unit.state == "rest":
        if blocked or not min_time_met(unit, now, cfg.rest_min_heartbeats, interval):
            return _stay(unit, ctx)
        if released(votes.cold_side, votes.required):
            return _go(unit, "cool", now, ctx, offset=unit.offset + 1)
        return _stay(unit, ctx)

    return _stay(unit, ctx)


def on_section_change(unit: Unit, ctx: Context, forced_off: bool,
                      now: datetime) -> tuple[Unit, Command | None]:
    """The scene at a crossover (and a confirmed leak release). Resets the
    section-scoped fields, then: leak if the latch is on (off is re-sent),
    else config_off if the scene forces the unit off, else rest if cooling
    is blocked, else cool. user_off and manual end here. Minimum times
    don't apply."""
    reset = replace(unit, offset=0, base=ctx.setpoint_applied, unblocked=False,
                    grace_until=None, since=now)
    if ctx.leak_on:
        state = "leak"
    elif forced_off:
        state = "config_off"
    elif ctx.setpoint_applied is None:
        if unit.state != "leak":
            return reset, None               # no almanac yet: leave the unit alone
        # A released leak can't stay in leak. Its unit is already off: rest
        # leaves it off, and wakes it once there is a setpoint to cool to.
        return replace(reset, state="rest"), None
    else:
        state = "rest" if ctx.blocked else "cool"
    new = replace(reset, state=state)
    return new, command_for(state, target_base(new, ctx), 0, ctx)


def on_person_change(kind: str, unit: Unit, new_setpoint: float | None, power_after: bool,
                     now: datetime, ctx: Context) -> Unit:
    """What a person's change does to the unit's record. No command: the
    person already moved the unit. kind as the reaction detector records
    it: setpoint, on, off, manual (knob_turn counts as setpoint, and a knob
    press changes nothing). A latched leak keeps its state."""
    if unit.state == "leak":
        return unit
    grace = now + timedelta(minutes=ctx.config.person_grace_minutes)
    if kind in ("setpoint", "knob_turn"):
        if _kept_off(power_after) or _kept_manual(unit.state):
            # changed while off, or in manual: remember it for the next time
            # the unit cools
            return replace(unit, base=new_setpoint, offset=0)
        return replace(unit, state="cool", base=new_setpoint, offset=0, since=now,
                       grace_until=grace)
    if kind == "on":
        return replace(unit, state="cool", base=new_setpoint, offset=0, unblocked=True,
                       since=now, grace_until=grace)
    if kind == "off":
        return replace(unit, state="user_off", since=now)
    if kind == "manual":
        return replace(unit, state="manual", since=now)
    return unit


def on_climate_change(unit: Unit, before: Climate, after: Climate, now: datetime,
                      ctx: Context) -> Unit:
    """The manual-change automation, once it knows the change carries no
    parent context, the guard is off and the unit is reporting: our own
    command showing up late changes nothing; anything else is a person's."""
    if matches_command(implied_command(unit, ctx), before, after, ctx.min_delta):
        return unit
    kind = classify_change(before, after, ctx.min_delta)
    if kind is None:
        return unit
    return on_person_change(kind, unit, after.temperature, after.mode != "off", now, ctx)
