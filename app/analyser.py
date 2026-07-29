"""
Adaptive Climate - analyser.

Rebuilds the almanac nightly from the rolling window of heartbeats and
reactive events. Two things are learned per section (see
docs/TRUST_MODEL.md):

  * per unit: the SETPOINT the unit sits on during settled periods -
    learned exactly as Light learned brightness (recency-weighted rolling
    average, reactive events at 5x weight, confidence bands).

  * per sensor: the COMFORT reading it shows while settled, plus a BAND
    and TRUST inferred from the deviations at which the user has reacted
    on that sensor. band = clamp(weighted_mean(reaction_deviations),
    high_dev, low_dev); trust = normalised inverse (app/trust.py).

STATUS: skeleton. The setpoint-learning half ports almost directly from
Light's analyser; the per-sensor comfort/trust half is new and is the
Phase 4 work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

SECTIONS = ("sunrise", "day", "afternoon", "sunset", "night", "sleep")


@dataclass(frozen=True)
class LearningConfig:
    analysis_window_days: int = 21
    bootstrap_min_days: int = 7
    validity_delay_days: int = 2
    reactive_weight: float = 5.0
    high_trust_deviation: float = 0.5
    low_trust_deviation: float = 5.0


@dataclass
class SectionAlmanac:
    section: str
    state: str                      # provisional | bootstrap | learning
    sample_days: int = 0
    confidence: float | None = None
    unit_setpoints: dict[str, float | None] = field(default_factory=dict)
    unit_off: dict[str, bool] = field(default_factory=dict)
    sensor_comfort: dict[str, float] = field(default_factory=dict)
    sensor_band: dict[str, float] = field(default_factory=dict)
    sensor_trust: dict[str, float] = field(default_factory=dict)


def analyse_room(conn, room: dict, cfg: LearningConfig,
                 as_of: date) -> list[SectionAlmanac]:
    """Rebuild every section's almanac for one room.

    TODO(Phase 4):
      1. Pull the rolling window of heartbeats (+ per-sensor, per-unit)
         and reactive events for this room.
      2. Per section, per unit: recency-weighted setpoint average over
         settled samples (reactive at 5x). Port from Light.
      3. Per section, per sensor: comfort = weighted mean of settled
         readings; band = clamp(weighted_mean(reaction deviations),
         high, low); trust = trust_from_band(band). Use app/trust.py.
      4. Assign provisional/bootstrap/learning per day count.
    """
    raise NotImplementedError("Phase 4")
