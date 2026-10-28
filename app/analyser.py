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
              S += reaction_rate * (chosen - S). A person's vote (too_cold
              / too_hot) moves every unit's S the same way, toward the
              value chosen on a remote, else one step from the applied
              setpoint in force (warmer for too_cold).
  applied     tracked through the replay, on whole degrees. A seed applies
              its nearest step (half up). A setpoint-moving reaction whose
              chosen value differs from the applied value in force moves it
              to the step nearest the new S (an exact half toward the
              reaction's direction, sign(chosen - S_before)), but at least
              one step toward the chosen value and never past it; one whose
              chosen value equals the applied value keeps it. Nothing else
              moves it, so quiet days never do.
  comfort     at a vote, each usable sensor's comfort moves at
              reaction_rate toward its reading plus person_step_degrees
              (too_cold) or minus it (too_hot). Then, 30-60 min after each
              setpoint-moving reaction, the window's mean reading moves
              comfort toward it at reaction_rate, unless the window holds
              another reaction, a heartbeat with a unit in boost/rest/leak
              (a vote's forced state included), or fewer than 2 heartbeats.
              It applies once the window has closed, to the reaction's
              section even across a boundary.
  band        starts at the low-trust deviation; every reaction with a
              direction nudges each sensor's band at reaction_rate, by its
              deviation d from comfort (as it stood in the replay before the
              reaction): on the person's side (below comfort when warmer is
              wanted) and inside its band, toward |d|; on their side and
              already past it, toward 0.8 x band; on the other side, toward
              band + |d|. Clamped to [high, low] trust deviation; quiet
              periods never move it.
  confidence  1 - 0.5^(quiet/3); quiet = completed runs since the last
              reaction with at least one heartbeat and no reaction.
  status      provisional until the first setpoint-moving reaction, then
              learned.

A setpoint-moving reaction is a vote, a setpoint change or knob turn (rows
recorded before the votes), or a reaction of any kind in which a unit's
setpoint changed (an `on` with a new setpoint).

Pure over a sqlite connection, a room dict and an as_of datetime. Nothing
after as_of is read, so the same database and as_of give the same almanac.
Run tools/analyser_demo.py for the worked examples.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from app.control import SETPOINT_STEP
from app.trust import trust_from_band

SECTIONS = ("sunrise", "day", "afternoon", "sunset", "night", "sleep")

# Comfort window after a setpoint-moving reaction (inclusive at both ends).
COMFORT_WINDOW = (timedelta(minutes=30), timedelta(minutes=60))
COMFORT_MIN_HEARTBEATS = 2
DISQUALIFYING_STATES = ("boost", "rest", "leak")

# A person's vote: the too_cold / too_hot buttons, or a remote's setpoint
# change, as Home Assistant's ac_person_vote event reports it.
VOTE_KINDS = ("too_cold", "too_hot")
# A NULL kind is a row recorded before kinds existed: a setpoint change.
SETPOINT_MOVING_KINDS = (None, "setpoint", "knob_turn") + VOTE_KINDS
BAND_TIGHTEN = 0.8      # a sensor already past its band on the person's side

_EPS = 1e-9


# ---------------------------------------------------------------------
# Configuration and output
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class LearningConfig:
    reaction_rate: float = 0.5
    high_trust_deviation: float = 0.5
    low_trust_deviation: float = 5.0
    person_step_degrees: float = 1.0

    @classmethod
    def from_config(cls, config: dict) -> LearningConfig:
        """From a (migrated) config dict: learning.*, system.trust.* and
        system.control.person_step_degrees."""
        learn = config.get("learning", {})
        trust = config.get("system", {}).get("trust", {})
        control = config.get("system", {}).get("control", {})
        base = cls()
        return cls(
            reaction_rate=learn.get("reaction_rate", base.reaction_rate),
            high_trust_deviation=trust.get("high_trust_deviation", base.high_trust_deviation),
            low_trust_deviation=trust.get("low_trust_deviation", base.low_trust_deviation),
            person_step_degrees=control.get("person_step_degrees", base.person_step_degrees),
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
    units: dict                     # unit_id -> (setpoint_before, setpoint_after);
                                    # a remote's vote: (None, the value chosen)
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
    """A vote, a setpoint change or knob turn, or a reaction of any kind in
    which a unit's setpoint changed - an `on` that comes with a new
    setpoint, say (docs/V2_DESIGN.md). It moves S and the applied setpoint,
    makes the section learned and opens a comfort window."""
    return kind in SETPOINT_MOVING_KINDS or changed_setpoint(units or {})


def nearest_step(value: float, direction: int, step: float = 1.0) -> float:
    """`value` on its nearest multiple of `step`; an exact half goes up when
    direction >= 0 and down when < 0. Not Python's round(), whose ties go
    to even and would send a learned 22.5 back to 22."""
    q = value / step
    low = math.floor(q + _EPS)
    if abs(q - low - 0.5) < _EPS:
        n = low if direction < 0 else low + 1
    else:
        n = math.floor(q + 0.5)
    return float(n * step)


def next_applied(applied: float, s: float, chosen: float, direction: int,
                 step: float = 1.0) -> float:
    """setpoint_applied after a setpoint-moving reaction (docs/TRUST_MODEL.md).
    applied: the value in force when the person reacted; s: the new S;
    chosen: what they set; direction: the reaction's, for an exact half.

    A chosen value equal to the applied value keeps it. Otherwise the new
    value is the step nearest S, but at least one step from the applied
    value toward the chosen one and never past it: a correction always
    shows, and never overshoots what the person asked for."""
    if abs(chosen - applied) < _EPS:
        return applied
    near = nearest_step(s, direction, step)
    if chosen > applied:
        cap = math.floor(chosen / step + _EPS) * step       # the last step not past it
        return float(min(max(near, applied + step), cap))
    cap = math.ceil(chosen / step - _EPS) * step
    return float(max(min(near, applied - step), cap))


def confidence_from_quiet(quiet: int) -> float:
    return 1.0 - 0.5 ** (quiet / 3)


def wanted_direction(kind: str | None, units: dict) -> int:
    """What the person asked for: +1 warmer, -1 cooler, 0 nothing to learn
    a band from. too_cold and off mean warmer, too_hot and on cooler; a
    setpoint change or knob turn goes by how the person moved the setpoint
    (summed over units)."""
    if kind in ("on", "too_hot"):
        return -1
    if kind in ("off", "too_cold"):
        return 1
    if kind not in SETPOINT_MOVING_KINDS:
        return 0                    # manual, knob_press
    delta = math.fsum(after - before for before, after in units.values()
                      if before is not None and after is not None)
    return 1 if delta > _EPS else -1 if delta < -_EPS else 0


def nudge_band(band: float, deviation: float, want: int, cfg: LearningConfig) -> float:
    """One reaction's nudge to a sensor's band (docs/TRUST_MODEL.md).
    deviation: reading - comfort at the reaction; want: +1 warmer wanted,
    -1 cooler. On the person's side (below comfort when they want it
    warmer) and inside its band, the band moves toward |d|, so the sensor
    would have spoken; on their side and already past it, toward 0.8 x
    band; on the other side, toward band + |d|. A sensor right at comfort
    says nothing either way. Clamped to [high, low] trust deviation."""
    if abs(deviation) < _EPS or not want:
        return band
    d = abs(deviation)
    if want * deviation < 0:                         # the person's side
        toward = d if d < band else BAND_TIGHTEN * band
    else:
        toward = band + d
    band += cfg.reaction_rate * (toward - band)
    return min(max(band, cfg.high_trust_deviation), cfg.low_trust_deviation)


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


def _move_setpoint(uid: str, chosen: float, setpoint: dict, direction: dict, applied: dict,
                   rate: float) -> None:
    """A setpoint-moving reaction for one unit: S toward the value chosen,
    the rounding direction from S toward it, and the applied setpoint by
    next_applied."""
    s_before = setpoint[uid]
    setpoint[uid] = s_before + rate * (chosen - s_before)
    if abs(chosen - s_before) >= _EPS:
        direction[uid] = 1 if chosen > s_before else -1
    applied[uid] = next_applied(applied[uid], setpoint[uid], chosen, direction[uid])


def _replay_section(conn, room_id: str, section: str, unit_seeds: dict, sensor_seeds: dict,
                    room_reactions: list[_Reaction], cfg: LearningConfig,
                    as_of: datetime) -> SectionAlmanac:
    rate = cfg.reaction_rate
    setpoint = dict(unit_seeds)                      # unit -> S
    direction = {u: 0 for u in setpoint}             # +1 / -1 / 0: none yet
    applied = {u: nearest_step(s, 0) for u, s in setpoint.items()}   # setpoint_applied
    comfort = dict(sensor_seeds)                     # sensor -> comfort reading
    band = {s: cfg.low_trust_deviation for s in comfort}
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

        # Each sensor's band, against comfort as it stands before this
        # reaction moves it.
        want = wanted_direction(r.kind, r.units)
        usable = {sid: t for sid, t in r.sensors.items()
                  if t is not None and comfort.get(sid) is not None}
        for sid, t in usable.items():
            band[sid] = nudge_band(band[sid], t - comfort[sid], want, cfg)

        if r.kind in VOTE_KINDS:
            # The person is a sensor that always wins: comfort moves toward
            # each reading a step their way, and every unit's S toward the
            # value they chose on a remote, else a step from the applied
            # setpoint in force.
            for sid, t in usable.items():
                comfort[sid] += rate * (t + want * cfg.person_step_degrees - comfort[sid])
            for uid in setpoint:
                chosen = (r.units.get(uid) or (None, None))[1]
                if chosen is None:
                    chosen = applied[uid] + want * SETPOINT_STEP
                _move_setpoint(uid, chosen, setpoint, direction, applied, rate)
            continue

        # A unit whose setpoint changed makes this a setpoint-moving reaction
        # whatever its kind (an `on` with a new setpoint included): S moves,
        # and so does the applied setpoint, from the value in force toward
        # what the person chose. On/off alone, or manual, leaves both alone.
        for uid, (before, after) in r.units.items():
            if uid in setpoint and changed_setpoint({uid: (before, after)}):
                _move_setpoint(uid, after, setpoint, direction, applied, rate)

    trust = {sid: trust_from_band(b, cfg.high_trust_deviation, cfg.low_trust_deviation)
             for sid, b in band.items()}

    last_reaction = here[-1].at if here else None
    return SectionAlmanac(
        section=section,
        status="learned" if any(is_setpoint_moving(r.kind, r.units) for r in here)
        else "provisional",
        valid_from=as_of.date(),
        confidence=_confidence(conn, room_id, section, room_reactions, last_reaction, as_of),
        unit_setpoints=setpoint,
        unit_setpoints_applied=dict(applied),
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
