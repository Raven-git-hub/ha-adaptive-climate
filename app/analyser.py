"""
Adaptive Climate - analyser: reaction-driven learning.

The almanac is a deterministic replay of every reaction, in time order,
from a seed (docs/V2_DESIGN.md, Learning model; docs/TRUST_MODEL.md).
Targets move only when a person acts. Quiet heartbeats only seed and add
confidence, so the system can never learn from its own output.

Per room and section:

  seed        unit: the earliest heartbeat in that room-section with the
              unit on and a setpoint. Sensor: its earliest reading in that
              section. The seed is the replay's starting state, whenever it
              was taken. An unseeded unit or sensor gets no almanac entry,
              so the generated automations leave it alone.
  setpoint    every reaction whose setpoint_after differs from its
              setpoint_before moves S toward the raw chosen value:
              S += reaction_rate * (chosen - S).
  applied     S rounded to a whole degree toward the latest setpoint-moving
              reaction's direction, sign(chosen - S_before); the direction
              stands while S doesn't move; half up before there is one.
  comfort     30-60 min after each setpoint-moving reaction, the window's
              mean reading moves comfort toward it at reaction_rate, unless
              the window holds another reaction, a heartbeat with a unit in
              boost/rest/leak, or fewer than 2 heartbeats. It applies once
              the window has closed, to the reaction's section even across
              a boundary.
  band        recency-weighted (half-life over all history) mean deviation
              at reactions, measured against comfort as it stood in the
              replay then, from sensors that drifted at least the high-trust
              deviation the way the reaction says: warmer wanted teaches
              sensors below comfort, cooler wanted those above.
  confidence  1 - 0.5^(quiet/3); quiet = completed runs since the last
              reaction with at least one heartbeat and no reaction.
  status      provisional until the first setpoint-moving reaction, then
              learned.

A setpoint-moving reaction is a setpoint change or knob turn, or a reaction
of any kind in which a unit's setpoint changed (an `on` with a new setpoint).

Pure over a sqlite connection, a room dict and an as_of datetime. Nothing
after as_of is read, so the same database and as_of give the same almanac.
Run tools/analyser_demo.py for the worked examples.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from app.trust import band_from_deviation, trust_from_band

SECTIONS = ("sunrise", "day", "afternoon", "sunset", "night", "sleep")

# Comfort window after a setpoint-moving reaction (inclusive at both ends).
COMFORT_WINDOW = (timedelta(minutes=30), timedelta(minutes=60))
COMFORT_MIN_HEARTBEATS = 2
DISQUALIFYING_STATES = ("boost", "rest", "leak")

# A NULL kind is a row recorded before kinds existed: a setpoint change.
SETPOINT_MOVING_KINDS = (None, "setpoint", "knob_turn")

_EPS = 1e-9


# ---------------------------------------------------------------------
# Configuration and output
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class LearningConfig:
    reaction_rate: float = 0.5
    recency_half_life_days: float = 7.0
    high_trust_deviation: float = 0.5
    low_trust_deviation: float = 5.0

    @classmethod
    def from_config(cls, config: dict) -> LearningConfig:
        """From a (migrated) config dict: learning.* and system.trust.*."""
        learn = config.get("learning", {})
        trust = config.get("system", {}).get("trust", {})
        base = cls()
        return cls(
            reaction_rate=learn.get("reaction_rate", base.reaction_rate),
            recency_half_life_days=learn.get("recency_half_life_days",
                                             base.recency_half_life_days),
            high_trust_deviation=trust.get("high_trust_deviation", base.high_trust_deviation),
            low_trust_deviation=trust.get("low_trust_deviation", base.low_trust_deviation),
        )


@dataclass
class SectionAlmanac:
    section: str
    status: str                     # provisional | learned
    valid_from: date
    confidence: float = 0.0
    unit_setpoints: dict[str, float] = field(default_factory=dict)
    unit_setpoints_applied: dict[str, float] = field(default_factory=dict)
    unit_off: dict[str, bool] = field(default_factory=dict)
    sensor_comfort: dict[str, float] = field(default_factory=dict)
    sensor_band: dict[str, float] = field(default_factory=dict)
    sensor_trust: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class _Reaction:
    id: int
    at: datetime                    # UTC
    section: str
    local_date: str
    kind: str | None                # room-level
    units: dict                     # unit_id -> (setpoint_before, setpoint_after)
    sensors: dict                   # sensor_id -> temperature


# ---------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------

def changed_setpoint(units: dict) -> bool:
    """Did any unit's setpoint change? units: {unit_id: (before, after)}.
    A missing value on either side counts as no change."""
    return any(before is not None and after is not None and abs(after - before) >= _EPS
               for before, after in units.values())


def is_setpoint_moving(kind: str | None, units: dict | None = None) -> bool:
    """A setpoint change or knob turn, or a reaction of any kind in which a
    unit's setpoint changed - an `on` that comes with a new setpoint, say
    (docs/V2_DESIGN.md). It moves S, sets the rounding direction, makes the
    section learned and opens a comfort window."""
    return kind in SETPOINT_MOVING_KINDS or changed_setpoint(units or {})


def round_toward(value: float, direction: int, step: float = 1.0) -> float:
    """`value` on a multiple of `step`: up when direction > 0, down when
    < 0, half up when 0. A value already on a step (within float noise)
    stays put. Not Python's round(), whose ties go to even and would send
    a learned 22.5 back to 22."""
    q = value / step
    nearest = math.floor(q + 0.5)
    if abs(q - nearest) < _EPS:
        n = nearest
    elif direction > 0:
        n = math.ceil(q)
    elif direction < 0:
        n = math.floor(q)
    else:
        n = nearest
    return float(n * step)


def confidence_from_quiet(quiet: int) -> float:
    return 1.0 - 0.5 ** (quiet / 3)


def wanted_direction(kind: str | None, units: dict) -> int:
    """What the person asked for: +1 warmer, -1 cooler, 0 nothing to learn
    a band from. On means too warm, off too cold; a setpoint change or knob
    turn goes by how the person moved the setpoint (summed over units)."""
    if kind == "on":
        return -1
    if kind == "off":
        return 1
    if kind not in SETPOINT_MOVING_KINDS:
        return 0                    # manual, knob_press
    delta = math.fsum(after - before for before, after in units.values()
                      if before is not None and after is not None)
    return 1 if delta > _EPS else -1 if delta < -_EPS else 0


def _utc(dt: datetime) -> str:
    """The format ts_utc is stored in, so string comparisons are exact."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _at(ts: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


# ---------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------

def _unit_seeds(conn, room_id: str, until: str) -> dict[tuple[str, str], float]:
    rows = conn.execute(
        "SELECT section, unit_id, setpoint FROM ("
        "  SELECT h.section, hu.unit_id, hu.setpoint, ROW_NUMBER() OVER ("
        "    PARTITION BY h.section, hu.unit_id ORDER BY h.ts_utc, h.id) AS rn"
        "  FROM heartbeat h JOIN heartbeat_unit hu ON hu.heartbeat_id = h.id"
        "  WHERE h.room_id = ? AND h.ts_utc <= ? AND hu.is_on = 1"
        "    AND hu.setpoint IS NOT NULL) WHERE rn = 1",
        (room_id, until))
    return {(sec, uid): sp for sec, uid, sp in rows}


def _sensor_seeds(conn, room_id: str, until: str) -> dict[tuple[str, str], float]:
    rows = conn.execute(
        "SELECT section, sensor_id, temperature FROM ("
        "  SELECT h.section, hs.sensor_id, hs.temperature, ROW_NUMBER() OVER ("
        "    PARTITION BY h.section, hs.sensor_id ORDER BY h.ts_utc, h.id) AS rn"
        "  FROM heartbeat h JOIN heartbeat_sensor hs ON hs.heartbeat_id = h.id"
        "  WHERE h.room_id = ? AND h.ts_utc <= ? AND hs.temperature IS NOT NULL"
        ") WHERE rn = 1",
        (room_id, until))
    return {(sec, sid): t for sec, sid, t in rows}


def _reactions(conn, room_id: str, until: str) -> list[_Reaction]:
    """Every reaction in the room up to `until`, in time order."""
    head = conn.execute(
        "SELECT id, ts_utc, section, local_date, kind FROM reactive "
        "WHERE room_id = ? AND ts_utc <= ? ORDER BY ts_utc, id", (room_id, until)).fetchall()
    units: dict[int, dict] = {}
    for rid, uid, before, after in conn.execute(
            "SELECT ru.reactive_id, ru.unit_id, ru.setpoint_before, ru.setpoint_after "
            "FROM reactive_unit ru JOIN reactive r ON r.id = ru.reactive_id "
            "WHERE r.room_id = ? AND r.ts_utc <= ?", (room_id, until)):
        units.setdefault(rid, {})[uid] = (before, after)
    sensors: dict[int, dict] = {}
    for rid, sid, t in conn.execute(
            "SELECT rs.reactive_id, rs.sensor_id, rs.temperature "
            "FROM reactive_sensor rs JOIN reactive r ON r.id = rs.reactive_id "
            "WHERE r.room_id = ? AND r.ts_utc <= ?", (room_id, until)):
        sensors.setdefault(rid, {})[sid] = t
    return [_Reaction(rid, _at(ts), sec, d, kind, units.get(rid, {}), sensors.get(rid, {}))
            for rid, ts, sec, d, kind in head]


# ---------------------------------------------------------------------
# The replay
# ---------------------------------------------------------------------

def _comfort_window(conn, room_id: str, r: _Reaction,
                    room_reactions: list[_Reaction]) -> dict[str, float] | None:
    """Mean reading per sensor over heartbeats 30-60 min after r, or None
    if the window is disqualified."""
    lo, hi = r.at + COMFORT_WINDOW[0], r.at + COMFORT_WINDOW[1]
    if any(o.id != r.id and lo <= o.at <= hi for o in room_reactions):
        return None
    span = (room_id, _utc(lo), _utc(hi))
    in_window = "SELECT id FROM heartbeat WHERE room_id = ? AND ts_utc BETWEEN ? AND ?"
    count = conn.execute(f"SELECT COUNT(*) FROM ({in_window})", span).fetchone()[0]
    if count < COMFORT_MIN_HEARTBEATS:
        return None
    states = ",".join("?" * len(DISQUALIFYING_STATES))
    if conn.execute(
            f"SELECT 1 FROM heartbeat_unit WHERE heartbeat_id IN ({in_window}) "
            f"AND control_state IN ({states}) LIMIT 1", span + DISQUALIFYING_STATES).fetchone():
        return None
    readings: dict[str, list[float]] = {}
    for sid, t in conn.execute(
            f"SELECT sensor_id, temperature FROM heartbeat_sensor WHERE heartbeat_id IN "
            f"({in_window}) AND temperature IS NOT NULL ORDER BY heartbeat_id", span):
        readings.setdefault(sid, []).append(t)
    return {sid: math.fsum(ts) / len(ts) for sid, ts in readings.items()}


def _confidence(conn, room_id: str, section: str, room_reactions: list[_Reaction],
                last_reaction: datetime | None, as_of: datetime) -> float:
    """1 - 0.5^(quiet/3) over the completed runs since the last reaction in
    this room-section that had a heartbeat and no reaction."""
    since = _utc(last_reaction) if last_reaction else ""
    beats = [_at(ts) for (ts,) in conn.execute(
        "SELECT ts_utc FROM heartbeat WHERE room_id = ? AND section = ? AND ts_utc > ? "
        "AND ts_utc <= ? ORDER BY ts_utc", (room_id, section, since, _utc(as_of)))]
    reacted = [r.at for r in room_reactions]         # already in time order
    quiet = 0
    for start_s, end_s in conn.execute(
            "SELECT actual_start, ended_at FROM section_run WHERE room_id = ? AND section = ? "
            "AND actual_start IS NOT NULL AND ended_at IS NOT NULL", (room_id, section)):
        start, end = _at(start_s), _at(end_s)
        if end > as_of or (last_reaction is not None and start <= last_reaction):
            continue
        had_beat = bisect.bisect_left(beats, start) < bisect.bisect_left(beats, end)
        had_reaction = bisect.bisect_left(reacted, start) < bisect.bisect_left(reacted, end)
        if had_beat and not had_reaction:
            quiet += 1
    return confidence_from_quiet(quiet)


def _replay_section(conn, room_id: str, section: str, unit_seeds: dict, sensor_seeds: dict,
                    room_reactions: list[_Reaction], cfg: LearningConfig,
                    as_of: datetime) -> SectionAlmanac:
    rate = cfg.reaction_rate
    setpoint = dict(unit_seeds)                      # unit -> S
    direction = {u: 0 for u in setpoint}             # +1 / -1 / 0: none yet
    comfort = dict(sensor_seeds)                     # sensor -> comfort reading
    samples: dict[str, list[tuple[float, float]]] = {s: [] for s in comfort}
    here = [r for r in room_reactions if r.section == section]

    # Reactions at their own time; comfort windows once they have closed.
    events = [(r.at, 1, r.id, r) for r in here]
    events += [(r.at + COMFORT_WINDOW[1], 0, r.id, r) for r in here
               if is_setpoint_moving(r.kind, r.units) and r.at + COMFORT_WINDOW[1] <= as_of]
    for _, order, _, r in sorted(events, key=lambda e: e[:3]):
        if order == 0:                               # a comfort window closes
            means = _comfort_window(conn, room_id, r, room_reactions)
            for sid, mean in (means or {}).items():
                if comfort.get(sid) is not None:
                    comfort[sid] += rate * (mean - comfort[sid])
            continue

        want = wanted_direction(r.kind, r.units)
        if want:
            age = max(0, (as_of.date() - date.fromisoformat(r.local_date)).days)
            weight = 0.5 ** (age / cfg.recency_half_life_days)
            for sid, t in r.sensors.items():
                c = comfort.get(sid)
                if t is None or c is None:
                    continue
                dev = t - c
                if (want > 0 and dev <= -cfg.high_trust_deviation) or \
                        (want < 0 and dev >= cfg.high_trust_deviation):
                    samples[sid].append((abs(dev), weight))

        # A unit whose setpoint changed makes this a setpoint-moving reaction
        # whatever its kind (an `on` with a new setpoint included): S moves
        # and the rounding direction follows how it moved. On/off alone, or
        # manual, leaves both alone.
        for uid, (before, after) in r.units.items():
            if uid not in setpoint or not changed_setpoint({uid: (before, after)}):
                continue
            s_before = setpoint[uid]
            setpoint[uid] = s_before + rate * (after - s_before)
            if abs(after - s_before) >= _EPS:
                direction[uid] = 1 if after > s_before else -1

    band: dict[str, float] = {}
    trust: dict[str, float] = {}
    for sid, pairs in samples.items():
        total = math.fsum(w for _, w in pairs)
        b = (band_from_deviation(math.fsum(d * w for d, w in pairs) / total,
                                 cfg.high_trust_deviation, cfg.low_trust_deviation)
             if total > 0 else cfg.low_trust_deviation)
        band[sid] = b
        trust[sid] = trust_from_band(b, cfg.high_trust_deviation, cfg.low_trust_deviation)

    last_reaction = here[-1].at if here else None
    return SectionAlmanac(
        section=section,
        status="learned" if any(is_setpoint_moving(r.kind, r.units) for r in here)
        else "provisional",
        valid_from=as_of.date(),
        confidence=_confidence(conn, room_id, section, room_reactions, last_reaction, as_of),
        unit_setpoints=setpoint,
        unit_setpoints_applied={u: round_toward(s, direction[u]) for u, s in setpoint.items()},
        unit_off={u: False for u in setpoint},
        sensor_comfort=comfort, sensor_band=band, sensor_trust=trust,
    )


def analyse_room(conn, room: dict, cfg: LearningConfig, as_of: datetime) -> list[SectionAlmanac]:
    """Replay one room's almanac as of `as_of` (a timezone-aware datetime;
    its local date becomes valid_from). Sections with no seed at all are
    left out."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    room_id = room["id"]
    until = _utc(as_of)
    unit_seeds = _unit_seeds(conn, room_id, until)
    sensor_seeds = _sensor_seeds(conn, room_id, until)
    reactions = _reactions(conn, room_id, until)

    out: list[SectionAlmanac] = []
    for sec in SECTIONS:
        units = {u["id"]: unit_seeds[(sec, u["id"])] for u in room["units"]
                 if (sec, u["id"]) in unit_seeds}
        sensors = {s["id"]: sensor_seeds[(sec, s["id"])] for s in room["sensors"]
                   if (sec, s["id"]) in sensor_seeds}
        if units or sensors:
            out.append(_replay_section(conn, room_id, sec, units, sensors, reactions, cfg, as_of))
    return out
