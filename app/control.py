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
  on_section_change   what the scene does to a unit at a crossover: rest if
                      cooling is blocked or the room starts cold
                      (section_start), else cool
  on_room_vote        a person's too_cold / too_hot vote for the room (a
                      button press, or a setpoint change on a remote): one
                      per episode, forcing a rest or a boost
  on_person_change    what a person turning a unit on or off, or changing
                      its mode or fan, does to the unit's record (no
                      command: the person already moved the unit)
  on_climate_change   the manual-change automation: one climate state change,
                      ignored when it is our own command showing up
                      (matches_command); a setpoint change is a vote
                      (on_room_vote), anything else goes to on_person_change
  section_conditions  the outdoor conditions the container decides once per
                      section (phase 5): the room comfort point, the lean of
                      the trigger edges and the cooling block, published to
                      sensor.ac_conditions_<room> for the templates to read

A unit's state lives in its helpers (phase 3b): ac_state, ac_base,
ac_offset, ac_since, ac_grace_until, ac_unblocked, ac_forced and
ac_section_rest.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
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

# A person's vote: the room is too cold, or too hot (their word for
# too_warm). Each is a button per room; a setpoint change on a remote counts
# as one too (raised: too cold).
TOO_HOT = "too_hot"
PERSON_VOTES = (TOO_COLD, TOO_HOT)
SETPOINT_STEP = 1.0             # a vote moves the target this far
FORCED_STATES = ("rest", "boost")      # what a vote forces
VOTE_HANDS_OFF = ("leak", "config_off", "manual")   # a vote leaves these alone

DEFAULT_MIN_TEMP, DEFAULT_MAX_TEMP = 16.0, 30.0
DEFAULT_BAND = 5.0      # a sensor with no band yet votes like a low-trust one (v1 template)
DEFAULT_STALE_MINUTES = 90      # system.sensor_stale_minutes

# Outdoor conditions (phase 5). outdoor.block_mode: off never blocks; shadow
# decides and logs, but control sees no block; active applies it. A heat
# source blocks in shadow and active alike.
BLOCK_MODES = ("off", "shadow", "active")
HEAT_BLOCK_MODES = ("shadow", "active")
DEFAULT_BLOCK_MODE = "shadow"
LEAN_CAP = 0.6          # each edge stays this far on its own side of comfort
BLOCK_RELEASE = 1.0     # a block lifts only once delta_max <= margin - this
MARGIN_MIN, MARGIN_MAX = -3.0, 10.0     # -1: block at a max up to comfort + 1; -3 at most


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
    forced: bool = False                # this rest or boost is a person's vote
    section_rest: bool = False          # a section change began this rest: the room was cold


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
    blocked: bool = False               # the section's cooling block, as control sees it
    heat_source_on: bool = False        # one of the room's heat_sources is on
    block_mode: str = DEFAULT_BLOCK_MODE    # outdoor.block_mode (heat sources block unless off)
    leak_on: bool = False               # the unit's leak latch
    forced_off: bool = False            # the section's scene forces this unit off
    delta_mean: float | None = None     # comfort point - forecast mean; None: fail open
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
class PersonVote:
    """A person's vote as the ac_person_vote event reports it: accepted, or
    ignored because an episode is running. source is button or remote; a
    remote vote also names the unit changed and the setpoint chosen."""
    direction: str                      # too_cold | too_hot
    accepted: bool
    source: str
    unit: str | None = None
    chosen: float | None = None


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


def heat_blocks(ctx: Context) -> bool:
    """A heat source that is on blocks cooling at once, whatever the
    forecast, in shadow and active modes."""
    return ctx.heat_source_on and ctx.block_mode in HEAT_BLOCK_MODES


def room_blocked(ctx: Context) -> bool:
    """The room's cooling block as control sees it: the section's (active
    mode only, already folded into ctx.blocked) or a heat source's."""
    return ctx.blocked or heat_blocks(ctx)


def effective_blocked(unit: Unit, ctx: Context) -> bool:
    return room_blocked(ctx) and not unit.unblocked


def boost_capped(unit: Unit, now: datetime, config: ControlConfig) -> bool:
    return unit.since is not None and now - unit.since >= timedelta(minutes=config.boost_max_minutes)


def released(side_count: int, required: int) -> bool:
    """A boost or rest ends when fewer than the quorum are still on the side
    that triggered it. The quorum counts as at least 1, so with no usable
    sensor the unit goes back to cool rather than hold a rest forever."""
    return side_count < max(required, 1)


def release_offset(unit: Unit, change: int) -> int:
    """The offset a boost or rest returns to cool with: a system one moves
    it a step (change, -1 or +1); one a person's vote forced, or a rest the
    section started in because the room was cold (nobody said too cold),
    goes back to cool at its base."""
    return 0 if unit.forced or unit.section_rest else unit.offset + change


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
    """A transition: new state, since = now, and its command. It ends any
    forced state (only a vote forces one) and any section-start rest."""
    offset = unit.offset if offset is None else capped_offset(offset, ctx.config.offset_cap)
    new = replace(unit, state=state, offset=offset, since=now, forced=False, section_rest=False)
    return new, command_for(state, target_base(new, ctx), offset, ctx)


def _stay(unit: Unit, ctx: Context) -> tuple[Unit, Command | None]:
    """No transition, so no command: commands are never re-sent."""
    return unit, None


def _leak_step(unit: Unit, votes: Votes, now: datetime,
               ctx: Context) -> tuple[Unit, Command | None] | None:
    """Leak beats everything, minimum times and grace included. Returns
    None when the leak latch has nothing to do with this heartbeat."""
    if ctx.leak_on:
        return _stay(unit, ctx) if unit.state == "leak" else _go(unit, "leak", now, ctx)
    if unit.state == "leak":                 # fixed and confirmed: as at a section change
        return on_section_change(unit, ctx, ctx.forced_off, now, votes)
    return None


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
# A person's vote
# ---------------------------------------------------------------------

def forced_now(unit: Unit) -> bool:
    """A forced rest or boost: the marker, and still in the state the vote
    forced (a leftover marker on a unit that has moved on is no episode)."""
    return unit.forced and unit.state in FORCED_STATES


def in_episode(units: Iterable[Unit]) -> bool:
    """One correction per episode: while any unit in the room is in a forced
    rest or boost, a vote changes and teaches nothing."""
    return any(forced_now(u) for u in units)


def vote_base(direction: str, unit: Unit, ctx: Context,
              chosen: float | None = None) -> float | None:
    """The unit's new base: the setpoint a person chose on its remote, else
    its target one step warmer (too_cold) or cooler (too_hot); clamped to
    its range. None with no target at all (no base, no almanac)."""
    if chosen is None:
        tb = target_base(unit, ctx)
        if tb is None:
            return None
        chosen = tb + (SETPOINT_STEP if direction == TOO_COLD else -SETPOINT_STEP)
    return clamp(chosen, ctx.unit_min, ctx.unit_max)


def _votes_alone(unit: Unit, ctx: Context) -> bool:
    """A vote leaves a unit in leak (or with its latch on), config_off or
    manual as it is; the reaction is still recorded."""
    return ctx.leak_on or unit.state in VOTE_HANDS_OFF


def _keeps_since(unit: Unit, state: str) -> bool:
    """A unit already in the state a vote forces keeps its since, so its
    minimum time and the boost cap count from when that state began."""
    return unit.state == state


def _force(unit: Unit, state: str, now: datetime, ctx: Context) -> tuple[Unit, Command | None]:
    """Into a forced rest or boost at the voted base, offset 0. A unit
    already resting is already off, so it is sent nothing."""
    since = unit.since if _keeps_since(unit, state) else now
    new = replace(unit, state=state, since=since, forced=True)
    if unit.state == "rest" and state == "rest":
        return new, None
    return new, command_for(state, target_base(new, ctx), 0, ctx)


def on_person_vote(direction: str, unit: Unit, now: datetime, ctx: Context,
                   chosen: float | None = None) -> tuple[Unit, Command | None]:
    """One unit's part in an accepted vote. too_cold: a cooling or boosting
    unit rests (off), a resting one stays resting, both forced; user_off
    stays off with the new base. too_hot: a unit cooling, resting, boosting
    or in user_off boosts, forced, and is unblocked if cooling is blocked.
    Base = vote_base, offset 0, no grace."""
    if _votes_alone(unit, ctx):
        return _stay(unit, ctx)
    base = vote_base(direction, unit, ctx, chosen)
    if base is None:
        return _stay(unit, ctx)
    voted = replace(unit, base=base, offset=0, grace_until=None, section_rest=False)
    if direction == TOO_COLD:
        if unit.state == "user_off":
            return voted, None               # the person's off stands
        return _force(voted, "rest", now, ctx)
    return _force(unblock_for_vote(voted, ctx), "boost", now, ctx)


def unblock_for_vote(unit: Unit, ctx: Context) -> Unit:
    """A too_hot vote while cooling is blocked unblocks the unit for the
    rest of the section, as a person's on does, so its forced boost isn't
    sent straight back to rest by the block."""
    return replace(unit, unblocked=True) if room_blocked(ctx) else unit


def on_room_vote(direction: str, units: dict[str, Unit], now: datetime,
                 ctxs: dict[str, Context], chosen: dict[str, float] | None = None
                 ) -> tuple[bool, dict[str, tuple[Unit, Command | None]]]:
    """A person's vote for the whole room: (accepted, {unit_id: (unit,
    command)}). Ignored, changing nothing, while an episode is running;
    else every unit takes its part. chosen: {unit_id: setpoint} for a unit
    changed on its remote."""
    if in_episode(units.values()):
        return False, {uid: (u, None) for uid, u in units.items()}
    chosen = chosen or {}
    return True, {uid: on_person_vote(direction, u, now, ctxs[uid], chosen.get(uid))
                  for uid, u in units.items()}


def setpoint_direction(before: Climate, after: Climate) -> str:
    """A setpoint raised on a remote says too cold; lowered, too hot."""
    return TOO_COLD if after.temperature > before.temperature else TOO_HOT


# ---------------------------------------------------------------------
# Outdoor conditions (phase 5) - decided by the container once per section
# and published to sensor.ac_conditions_<room>; the templates read them
# ---------------------------------------------------------------------

NO_OUTDOOR, NO_COMFORT_POINT = "no_outdoor", "no_comfort_point"    # why it fails open


@dataclass(frozen=True)
class Conditions:
    """A room's conditions for one section: how far every sensor's trigger
    edges lean (in band widths) and the cooling block. Locked for the
    section. delta = comfort point - outdoor: positive is colder outside."""
    section: str
    block_mode: str = DEFAULT_BLOCK_MODE
    margin: float | None = None
    comfort_point: float | None = None
    outdoor_mean: float | None = None
    outdoor_max: float | None = None
    delta_mean: float | None = None
    delta_max: float | None = None
    lean: float | None = None           # k x delta_mean, before the per-edge caps
    cold_edge: float = -1.0
    warm_edge: float = 1.0
    would_block: bool = False           # the decision, shadow mode's "would block" included
    fail_open: str | None = None        # NO_OUTDOOR or NO_COMFORT_POINT: no lean, no block

    @property
    def blocked(self) -> bool:
        """The block as control sees it: the decision, in active mode only."""
        return self.would_block and self.block_mode == "active"


def comfort_point(section_almanac: dict | None,
                  readings: dict[str, float | None]) -> float | None:
    """The room comfort point: the trust-weighted mean of the comfort
    readings of the usable sensors (a comfort reading, and a reading now:
    neither unavailable nor stale), or their plain mean while none has any
    trust. None with no usable sensor. To 2 decimals."""
    learned = (section_almanac or {}).get("sensors") or {}
    pairs = []
    for sid, reading in readings.items():
        entry = learned.get(sid) or {}
        if reading is not None and entry.get("comfort") is not None:
            pairs.append((entry["comfort"], entry.get("trust") or 0.0))
    if not pairs:
        return None
    weight = math.fsum(t for _, t in pairs)
    if weight > 0:
        return round(math.fsum(c * t for c, t in pairs) / weight, 2)
    return round(math.fsum(c for c, _ in pairs) / len(pairs), 2)


def lean_edges(lean: float | None) -> tuple[float, float]:
    """(cold_edge, warm_edge) in band widths for a lean of k x delta_mean:
    the cold edge -1 + lean, capped at +-0.6; the warm edge +1 + lean,
    capped at -0.6 on the hot side and uncapped on the cold. The caps keep
    each edge on its own side of the comfort reading, where boosts and
    rests release. No lean: -1, +1."""
    if lean is None:
        return -1.0, 1.0
    return (round(-1.0 + max(-LEAN_CAP, min(LEAN_CAP, lean)), 3),
            round(1.0 + max(lean, -LEAN_CAP), 3))


def decide_block(delta_max: float | None, margin: float, was_blocked: bool) -> bool:
    """Block cooling when the section's warmest forecast hour is at or below
    the comfort point minus the margin (delta_max >= margin): with a margin
    of -1, at a max up to comfort + 1. A room blocked last section stays
    blocked until delta_max <= margin - 1 (comfort + 2 at -1). The margin
    may be negative."""
    if delta_max is None:
        return False
    if was_blocked:
        return delta_max > margin - BLOCK_RELEASE
    return delta_max >= margin


def section_conditions(section: str, comfort: float | None, outdoor_mean: float | None,
                       outdoor_max: float | None, margin: float, was_blocked: bool,
                       block_mode: str, k: float) -> Conditions:
    """A section's conditions from the room comfort point and the forecast's
    mean (which leans the edges) and max (which blocks). No outdoor value or
    no comfort point fails open: edges -1/+1 and no block. block_mode off
    never blocks; shadow decides (would_block) but control sees no block."""
    base = Conditions(section, block_mode, margin, comfort, outdoor_mean, outdoor_max)
    if outdoor_mean is None:
        return replace(base, fail_open=NO_OUTDOOR)
    if comfort is None:
        return replace(base, fail_open=NO_COMFORT_POINT)
    delta_mean = round(comfort - outdoor_mean, 2)
    delta_max = None if outdoor_max is None else round(comfort - outdoor_max, 2)
    lean = round(k * delta_mean, 3)
    cold, warm = lean_edges(lean)
    would = block_mode != "off" and decide_block(delta_max, margin, was_blocked)
    return replace(base, delta_mean=delta_mean, delta_max=delta_max, lean=lean,
                   cold_edge=cold, warm_edge=warm, would_block=would)


# ---------------------------------------------------------------------
# The entry points
# ---------------------------------------------------------------------

def step(unit: Unit, votes: Votes, now: datetime, ctx: Context) -> tuple[Unit, Command | None]:
    """One maintenance heartbeat. Returns the unit's new record and the
    command to send, which is None unless it changed state."""
    leak = _leak_step(unit, votes, now, ctx)
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
            return _go(unit, "cool", now, ctx, offset=release_offset(unit, -1))
        return _stay(unit, ctx)

    if unit.state == "rest":
        if blocked or not min_time_met(unit, now, cfg.rest_min_heartbeats, interval):
            return _stay(unit, ctx)
        if released(votes.cold_side, votes.required):
            return _go(unit, "cool", now, ctx, offset=release_offset(unit, +1))
        return _stay(unit, ctx)

    return _stay(unit, ctx)


START_BLOCKED, START_HEAT_SOURCE, START_COLD = "blocked", "heat_source", "cold"
START_FAIL_OPEN, START_NOT_COOLER, START_NOT_COLD = "fail_open", "not_cooler_outside", "not_cold"


def starts_cold(votes: Votes | None, ctx: Context) -> bool:
    """The room starts the section cold: at least a quorum (counted as at
    least 1) of the usable sensors below their comfort readings in the new
    section's almanac, so a rest started now wouldn't release at once, and
    the section's forecast mean below the room comfort point (delta_mean >
    0). No forecast or no comfort point (delta_mean None) fails open."""
    return (votes is not None and ctx.delta_mean is not None and ctx.delta_mean > 0
            and not released(votes.cold_side, votes.required))


def section_start(votes: Votes | None, ctx: Context) -> tuple[str, str]:
    """The state a section starts a unit in, once a leak, the scene's
    forced off and a missing almanac are out of the way, and why: rest if
    cooling is blocked (the section's block, or a heat source), rest if the
    room starts cold (starts_cold), else cool (why not rest: fail open, not
    cooler outside, or not cold)."""
    if room_blocked(ctx):
        return "rest", START_BLOCKED if ctx.blocked else START_HEAT_SOURCE
    if starts_cold(votes, ctx):
        return "rest", START_COLD
    if ctx.delta_mean is None:
        return "cool", START_FAIL_OPEN
    return "cool", START_NOT_COOLER if ctx.delta_mean <= 0 else START_NOT_COLD


def on_section_change(unit: Unit, ctx: Context, forced_off: bool, now: datetime,
                      votes: Votes | None) -> tuple[Unit, Command | None]:
    """The scene at a crossover (and a confirmed leak release). Resets the
    section-scoped fields, then: leak if the latch is on (off is re-sent),
    else config_off if the scene forces the unit off, else section_start:
    rest if cooling is blocked (the section's block or a heat source), or if
    the room starts cold (votes: the room's, against the new section's
    almanac), else cool. A rest the room's cold start began is marked
    section_rest, so it wakes at the base. user_off, manual, a forced state
    and a section rest end here. Minimum times don't apply."""
    reset = replace(unit, offset=0, base=ctx.setpoint_applied, unblocked=False,
                    grace_until=None, since=now, forced=False, section_rest=False)
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
        state, why = section_start(votes, ctx)
        reset = replace(reset, section_rest=why == START_COLD)
    new = replace(reset, state=state)
    return new, command_for(state, target_base(new, ctx), 0, ctx)


def on_person_change(kind: str, unit: Unit, new_setpoint: float | None, now: datetime,
                     ctx: Context) -> Unit:
    """What a person turning the unit on or off, or changing its mode or
    fan, does to the unit's record. No command: the person already moved
    the unit. kind as the reaction detector records it: on, off, manual (a
    setpoint change is a vote, on_room_vote). Each ends a forced state and
    a section-start rest; a latched leak keeps its state."""
    if unit.state == "leak":
        return unit
    if kind == "on":
        return replace(unit, state="cool", base=new_setpoint, offset=0, unblocked=True,
                       since=now, forced=False, section_rest=False,
                       grace_until=now + timedelta(minutes=ctx.config.person_grace_minutes))
    if kind == "off":
        return replace(unit, state="user_off", since=now, forced=False, section_rest=False)
    if kind == "manual":
        return replace(unit, state="manual", since=now, forced=False, section_rest=False)
    return unit


def person_change_kind(unit: Unit, before: Climate, after: Climate, ctx: Context) -> str | None:
    """A climate change's kind if it is a person's: None when it is our own
    command showing up late, or not something a person changes."""
    if matches_command(implied_command(unit, ctx), before, after, ctx.min_delta):
        return None
    return classify_change(before, after, ctx.min_delta)


def on_button(direction: str, units: dict[str, Unit], now: datetime,
              ctxs: dict[str, Context]) -> tuple[dict[str, tuple[Unit, Command | None]],
                                                 PersonVote]:
    """The vote automation: a press of the room's too_cold or too_hot
    button."""
    accepted, results = on_room_vote(direction, units, now, ctxs)
    return results, PersonVote(direction, accepted, "button")


def on_climate_change(units: dict[str, Unit], uid: str, before: Climate, after: Climate,
                      now: datetime, ctxs: dict[str, Context]
                      ) -> tuple[dict[str, tuple[Unit, Command | None]], PersonVote | None]:
    """The manual-change automation for unit `uid`, once it knows the change
    carries no parent context, the guard is off and the unit is reporting.
    Our own command showing up late changes nothing. A setpoint change is a
    vote for the room, with the value chosen as that unit's new base; on,
    off and manual change that unit's record alone."""
    results = {u: (x, None) for u, x in units.items()}
    kind = person_change_kind(units[uid], before, after, ctxs[uid])
    if kind == "setpoint":
        direction = setpoint_direction(before, after)
        accepted, results = on_room_vote(direction, units, now, ctxs, {uid: after.temperature})
        return results, PersonVote(direction, accepted, "remote", uid, after.temperature)
    if kind is not None:
        results[uid] = (on_person_change(kind, units[uid], after.temperature, now, ctxs[uid]),
                        None)
    return results, None
