"""
Adaptive Climate - analyser.

Rebuilds the almanac from the rolling window of heartbeats and reactive
events. Two things are learned per section (see docs/TRUST_MODEL.md):

  * per unit: the SETPOINT the unit sits on during settled periods -
    a recency-weighted rolling average of heartbeat setpoints (unit on),
    with reactive setpoint_after values weighted `reactive_weight` times
    (you explicitly choosing a number is the strongest signal).

  * per sensor: the COMFORT reading it shows while settled (recency-
    weighted mean of heartbeat temperatures), plus a BAND and TRUST
    inferred from the deviations at which the user reacted on that sensor:
        band  = clamp(weighted_mean(|reaction_temp - comfort|), high, low)
        trust = trust_from_band(band)          # app/trust.py
    A sensor with no reactions gets the widest band (low trust): we assume
    nothing until taught.

Heartbeats are already the settled record (they defer during guard
windows), so every heartbeat sample is treated as settled. Reactive
events supply the 5x-weighted "the user chose this" signal on top.

Pure over a sqlite connection + a room dict; no HA, no config loading.
Run tools/analyser_demo.py for a worked-example check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from app.trust import band_from_deviation, trust_from_band

SECTIONS = ("sunrise", "day", "afternoon", "sunset", "night", "sleep")


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class LearningConfig:
    analysis_window_days: int = 21
    bootstrap_min_days: int = 7
    validity_delay_days: int = 2
    reactive_weight: float = 5.0
    recency_half_life_days: float = 7.0
    high_trust_deviation: float = 0.5
    low_trust_deviation: float = 5.0


@dataclass
class SectionAlmanac:
    section: str
    state: str                      # bootstrap | learning
    valid_from: date
    sample_days: int = 0
    confidence: float | None = None
    unit_setpoints: dict[str, float | None] = field(default_factory=dict)
    unit_off: dict[str, bool] = field(default_factory=dict)
    sensor_comfort: dict[str, float | None] = field(default_factory=dict)
    sensor_band: dict[str, float] = field(default_factory=dict)
    sensor_trust: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------
# Weighting helpers
# ---------------------------------------------------------------------

def _recency_weight(local_date: str, as_of: date, half_life: float) -> float:
    """Favour recent days: weight halves every `half_life` days of age."""
    age = (as_of - date.fromisoformat(local_date)).days
    if age < 0:
        age = 0
    return 0.5 ** (age / half_life)


def _weighted_mean(pairs: list[tuple[float, float]]) -> float | None:
    """pairs of (value, weight). None if there is no weight."""
    total = sum(w for _, w in pairs)
    if total <= 0:
        return None
    return sum(v * w for v, w in pairs) / total


# ---------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------

def analyse_room(conn, room: dict, cfg: LearningConfig,
                 as_of: date) -> list[SectionAlmanac]:
    """Rebuild every section's almanac for one room, over the rolling
    window ending the day before `as_of`."""
    room_id = room["id"]
    unit_ids = [u["id"] for u in room["units"]]
    sensor_ids = [s["id"] for s in room["sensors"]]

    lo = (as_of - timedelta(days=cfg.analysis_window_days)).isoformat()
    hi = as_of.isoformat()   # exclusive: only complete days feed the build

    # --- settled unit setpoints (heartbeats, unit on) ----------------
    hb_unit: dict[str, dict[str, list]] = {s: {} for s in SECTIONS}
    for sec, uid, d, sp in conn.execute(
        "SELECT h.section, hu.unit_id, h.local_date, hu.setpoint "
        "FROM heartbeat h JOIN heartbeat_unit hu ON hu.heartbeat_id = h.id "
        "WHERE h.room_id=? AND h.local_date>=? AND h.local_date<? "
        "AND hu.is_on=1 AND hu.setpoint IS NOT NULL",
        (room_id, lo, hi),
    ):
        hb_unit[sec].setdefault(uid, []).append((d, sp))

    # --- settled sensor temperatures ---------------------------------
    hb_sensor: dict[str, dict[str, list]] = {s: {} for s in SECTIONS}
    hb_dates: dict[str, set] = {s: set() for s in SECTIONS}
    for sec, sid, d, t in conn.execute(
        "SELECT h.section, hs.sensor_id, h.local_date, hs.temperature "
        "FROM heartbeat h JOIN heartbeat_sensor hs ON hs.heartbeat_id = h.id "
        "WHERE h.room_id=? AND h.local_date>=? AND h.local_date<? "
        "AND hs.temperature IS NOT NULL",
        (room_id, lo, hi),
    ):
        hb_sensor[sec].setdefault(sid, []).append((d, t))
        hb_dates[sec].add(d)

    # --- reactive setpoints (user-chosen; weighted heavier) ----------
    re_unit: dict[str, dict[str, list]] = {s: {} for s in SECTIONS}
    for sec, uid, d, sp in conn.execute(
        "SELECT r.section, ru.unit_id, r.local_date, ru.setpoint_after "
        "FROM reactive r JOIN reactive_unit ru ON ru.reactive_id = r.id "
        "WHERE r.room_id=? AND r.local_date>=? AND r.local_date<? "
        "AND ru.changed=1 AND ru.setpoint_after IS NOT NULL",
        (room_id, lo, hi),
    ):
        re_unit[sec].setdefault(uid, []).append((d, sp))

    # --- reactive sensor readings (drive the band) -------------------
    re_sensor: dict[str, dict[str, list]] = {s: {} for s in SECTIONS}
    for sec, sid, d, t in conn.execute(
        "SELECT r.section, rs.sensor_id, r.local_date, rs.temperature "
        "FROM reactive r JOIN reactive_sensor rs ON rs.reactive_id = r.id "
        "WHERE r.room_id=? AND r.local_date>=? AND r.local_date<? "
        "AND rs.temperature IS NOT NULL",
        (room_id, lo, hi),
    ):
        re_sensor[sec].setdefault(sid, []).append((d, t))

    out: list[SectionAlmanac] = []
    for sec in SECTIONS:
        sample_days = len(hb_dates[sec])
        if sample_days == 0:
            continue  # no data; runtime seeds a provisional target instead

        def rw(d: str) -> float:
            return _recency_weight(d, as_of, cfg.recency_half_life_days)

        # per unit setpoint
        unit_setpoints: dict[str, float | None] = {}
        for uid in unit_ids:
            pairs = [(sp, rw(d)) for d, sp in hb_unit[sec].get(uid, [])]
            pairs += [(sp, rw(d) * cfg.reactive_weight)
                      for d, sp in re_unit[sec].get(uid, [])]
            unit_setpoints[uid] = _weighted_mean(pairs)

        # per sensor comfort / band / trust
        comfort: dict[str, float | None] = {}
        band: dict[str, float] = {}
        trust: dict[str, float] = {}
        for sid in sensor_ids:
            c = _weighted_mean([(t, rw(d)) for d, t in hb_sensor[sec].get(sid, [])])
            comfort[sid] = c
            if c is None:
                b = cfg.low_trust_deviation
            else:
                devs = [(abs(t - c), rw(d)) for d, t in re_sensor[sec].get(sid, [])]
                mean_dev = _weighted_mean(devs)
                b = (band_from_deviation(mean_dev, cfg.high_trust_deviation,
                                         cfg.low_trust_deviation)
                     if mean_dev is not None else cfg.low_trust_deviation)
            band[sid] = b
            trust[sid] = trust_from_band(b, cfg.high_trust_deviation,
                                         cfg.low_trust_deviation)

        state = "bootstrap" if sample_days < cfg.bootstrap_min_days else "learning"
        valid_from = as_of if state == "bootstrap" \
            else as_of + timedelta(days=cfg.validity_delay_days)
        confidence = min(1.0, sample_days / max(1, cfg.bootstrap_min_days))

        out.append(SectionAlmanac(
            section=sec, state=state, valid_from=valid_from,
            sample_days=sample_days, confidence=round(confidence, 3),
            unit_setpoints=unit_setpoints,
            unit_off={uid: False for uid in unit_ids},
            sensor_comfort=comfort, sensor_band=band, sensor_trust=trust,
        ))
    return out
