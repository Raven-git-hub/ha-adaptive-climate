"""
Adaptive Climate - the sensor trust model.

Pure functions, no I/O, no Home Assistant, no database. The maths is
settled (see docs/TRUST_MODEL.md); everything here is directly testable
and is the single source of truth for how a reaction becomes a band and
a trust score, and how per-sensor votes become a corrective action.

Terminology:
  comfort   - the reading a sensor shows while the user is comfortable
              at the current unit setpoint. Learned, per sensor.
  band      - half-width, in degrees, of that sensor's comfort range.
              Tight = trusted, wide = not.
  trust     - normalised inverse of the band, 0..1. For display and
              tie-breaking only; the quorum itself is vote-count based.
  vote      - a sensor votes when its reading leaves [comfort-band,
              comfort+band].
  quorum    - how many votes are needed before corrective action starts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


# ---------------------------------------------------------------------
# Calibration -> band -> trust
# ---------------------------------------------------------------------

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def band_from_deviation(
    deviation: float, high_dev: float = 0.5, low_dev: float = 5.0
) -> float:
    """Map a single reaction deviation to a comfort band half-width.

    A user who reacts at 0.5 deg gets the tightest band; one who only
    reacts at 5 deg gets the widest. Clamped to the calibrated range.
    In practice the analyser feeds the recency-weighted mean of a
    sensor's reaction deviations here, not a single raw value.
    """
    return clamp(deviation, high_dev, low_dev)


def trust_from_band(
    band: float, high_dev: float = 0.5, low_dev: float = 5.0
) -> float:
    """Normalised inverse of the band: band==high_dev -> 1.0 (fully
    trusted), band==low_dev -> 0.0 (untrusted). Purely for display and
    tie-breaking."""
    span = low_dev - high_dev
    if span <= 0:
        return 0.0
    return clamp((low_dev - band) / span, 0.0, 1.0)


# ---------------------------------------------------------------------
# Voting
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class SensorReading:
    """One sensor's state at a heartbeat, against its learned model."""
    sensor_id: str
    temperature: float | None   # None if unavailable
    comfort: float | None       # None if nothing learned yet
    band: float                 # half-width, degrees

    @property
    def usable(self) -> bool:
        return self.temperature is not None and self.comfort is not None

    @property
    def deviation(self) -> float | None:
        """Signed deviation from comfort: positive = too warm, negative
        = too cold. None if not usable."""
        if not self.usable:
            return None
        return self.temperature - self.comfort  # type: ignore[operator]

    def votes(self) -> bool:
        """A sensor votes when it has left its comfort band. An
        unavailable sensor, or one with nothing learned, does not vote."""
        d = self.deviation
        return d is not None and abs(d) >= self.band


def quorum_required(n_sensors: int) -> int:
    """Votes needed for corrective action.

        1 sensor  -> 1
        2 sensors -> 2
        3+        -> ceil(n/2)   ("50% or more", unambiguous for even n)
    """
    if n_sensors <= 0:
        return 0
    if n_sensors == 1:
        return 1
    if n_sensors == 2:
        return 2
    return math.ceil(n_sensors / 2)


@dataclass(frozen=True)
class QuorumResult:
    met: bool
    tally: int
    required: int
    direction: str          # 'warm' | 'cool' | 'none'
    voters: tuple[str, ...]


def evaluate_quorum(
    readings: list[SensorReading],
    trusts: dict[str, float] | None = None,
) -> QuorumResult:
    """Tally votes and decide whether, and in which direction, to correct.

    Direction is the sign of the breaching sensors' drift. When breachers
    disagree on sign, the tie is broken by summed trust weight (a
    high-trust 'too cold' outvotes a low-trust 'too warm'); the quorum
    COUNT itself stays unweighted, per the spec. trusts is optional and
    only used to break a mixed-direction tie.
    """
    usable = [r for r in readings if r.usable]
    required = quorum_required(len(usable))

    voters = [r for r in usable if r.votes()]
    tally = len(voters)
    met = required > 0 and tally >= required

    if not met or not voters:
        return QuorumResult(met, tally, required, "none", tuple())

    trusts = trusts or {}
    warm_weight = cold_weight = 0.0
    for r in voters:
        d = r.deviation or 0.0
        w = trusts.get(r.sensor_id, 0.0) + 1e-6   # tiny floor so untrusted still counts
        if d > 0:
            warm_weight += w        # too warm -> need to cool
        elif d < 0:
            cold_weight += w        # too cold -> need to warm

    if warm_weight == cold_weight:
        direction = "none"
    else:
        direction = "cool" if warm_weight > cold_weight else "warm"

    return QuorumResult(met, tally, required, direction,
                        tuple(r.sensor_id for r in voters))


# ---------------------------------------------------------------------
# Applying a correction to a setpoint
# ---------------------------------------------------------------------

def correction_step(direction: str, max_step: float = 2.0) -> float:
    """Signed setpoint delta for one correction step, capped at max_step.

    'warm' raises the setpoint, 'cool' lowers it. 'none' is a no-op.
    """
    if direction == "warm":
        return +max_step
    if direction == "cool":
        return -max_step
    return 0.0
