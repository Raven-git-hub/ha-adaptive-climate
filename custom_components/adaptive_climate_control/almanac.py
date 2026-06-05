"""
Almanac for Adaptive Climate Control.

Maintains a personal comfort profile indexed by period and day-of-year.
Learns from user interventions using exponential weighted moving average (EWMA).
Retains 21 days of raw intervention events; older events are discarded but
their influence lives on in the accumulated slot values.
"""

from __future__ import annotations

import logging
from datetime import datetime, date
from collections import deque
from typing import Any

from .const import (
    PERIOD_NAMES,
    PERIODS,
    ALMANAC_DAYS,
    ALMANAC_LEARNING_WINDOW,
    ALMANAC_EWMA_ALPHA,
    TRUST_HIGH,
    TRUST_MEDIUM,
    TRUST_LOW,
    TRUST_THRESHOLD_MIN,
    TRUST_THRESHOLD_MAX,
    SENSOR_HISTORY_WINDOW,
)

_LOGGER = logging.getLogger(__name__)


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def get_day_of_year(dt: date | None = None) -> int:
    """Return day of year (1-365). Day 366 maps to 365."""
    if dt is None:
        dt = date.today()
    day = dt.timetuple().tm_yday
    return min(day, ALMANAC_DAYS)


def get_current_period(dt: datetime | None = None) -> str:
    """Return the name of the current time period."""
    if dt is None:
        dt = datetime.now()
    hour = dt.hour
    for period, bounds in PERIODS.items():
        start = bounds["start"]
        end = bounds["end"]
        if start < end:
            if start <= hour < end:
                return period
        else:
            if hour >= start or hour < end:
                return period
    return "overnight"


def compute_action_threshold(trust_score: float) -> float:
    """
    Derive action threshold from trust score.
    trust=1.0 -> 0.5 degrees
    trust=0.0 -> 5.0 degrees
    """
    trust_score = max(0.0, min(1.0, trust_score))
    return TRUST_THRESHOLD_MIN + (1.0 - trust_score) * (
        TRUST_THRESHOLD_MAX - TRUST_THRESHOLD_MIN
    )


def trust_seed_from_label(label: str) -> float:
    """Convert a user-facing trust label to a numeric seed."""
    return {
        "reliable":   TRUST_HIGH,
        "uncertain":  TRUST_MEDIUM,
        "unreliable": TRUST_LOW,
    }.get(label.lower(), TRUST_MEDIUM)


# -------------------------------------------------------------------
# Sensor state
# -------------------------------------------------------------------

class SensorState:
    """
    Tracks the state of a single temperature sensor within a room.
    Maintains a rolling window of recent readings and a learned trust score.
    """

    def __init__(self, sensor_id: str, trust_seed: float = TRUST_MEDIUM) -> None:
        self.sensor_id = sensor_id
        self.trust_score: float = trust_seed
        self.reading_history: deque[tuple[datetime, float]] = deque()

    def record_reading(self, temperature: float, dt: datetime | None = None) -> None:
        """Add a temperature reading to the rolling window."""
        if dt is None:
            dt = datetime.now()
        self.reading_history.append((dt, temperature))
        self._prune_history(dt)

    def _prune_history(self, now: datetime) -> None:
        """Discard readings older than SENSOR_HISTORY_WINDOW minutes."""
        cutoff = now.timestamp() - (SENSOR_HISTORY_WINDOW * 60)
        while self.reading_history and self.reading_history[0][0].timestamp() < cutoff:
            self.reading_history.popleft()

    def recent_readings(self) -> list[float]:
        """Return temperature values from the rolling window."""
        return [temp for _, temp in self.reading_history]

    def latest_reading(self) -> float | None:
        """Return the most recent temperature reading."""
        if self.reading_history:
            return self.reading_history[-1][1]
        return None

    @property
    def action_threshold(self) -> float:
        """Current action threshold derived from trust score."""
        return compute_action_threshold(self.trust_score)

    def update_trust(self, was_predictive: bool) -> None:
        """
        Nudge trust score up or down based on whether this sensor
        predicted a user intervention accurately.
        """
        delta = 0.05 if was_predictive else -0.03
        self.trust_score = max(0.0, min(1.0, self.trust_score + delta))

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict for storage."""
        return {
            "sensor_id": self.sensor_id,
            "trust_score": self.trust_score,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SensorState":
        """Restore from stored dict."""
        obj = cls(data["sensor_id"], data.get("trust_score", TRUST_MEDIUM))
        return obj


# -------------------------------------------------------------------
# Almanac slot
# -------------------------------------------------------------------

class AlmanacSlot:
    """
    A single cell in the almanac, representing one period on one day of year.
    Stores the learned comfort band and confidence for that slot.
    """

    def __init__(
        self,
        period: str,
        day_of_year: int,
        default_temp: float,
    ) -> None:
        self.period = period
        self.day_of_year = day_of_year
        self.good_band_low: float = default_temp - 1.0
        self.good_band_high: float = default_temp + 1.0
        self.target_temp: float = default_temp
        self.confidence: int = 0
        self.last_updated: datetime | None = None

    def apply_learning(self, new_temp: float, direction: str) -> None:
        """
        Update this slot using EWMA when a user intervention is recorded.

        direction: "up" means user raised temperature (was too cold)
                   "down" means user lowered temperature (was too hot)
        """
        alpha = ALMANAC_EWMA_ALPHA

        self.target_temp = alpha * new_temp + (1.0 - alpha) * self.target_temp

        if direction == "up":
            self.good_band_low = alpha * new_temp + (1.0 - alpha) * self.good_band_low
        elif direction == "down":
            self.good_band_high = alpha * new_temp + (1.0 - alpha) * self.good_band_high

        self.confidence = min(self.confidence + 1, 100)
        self.last_updated = datetime.now()

        _LOGGER.debug(
            "Slot [%s|day %d] updated: target=%.1f band=[%.1f, %.1f] confidence=%d",
            self.period,
            self.day_of_year,
            self.target_temp,
            self.good_band_low,
            self.good_band_high,
            self.confidence,
        )

    def confidence_pct(self) -> float:
        """Return confidence as a 0-100 float."""
        return float(self.confidence)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict for storage."""
        return {
            "period": self.period,
            "day_of_year": self.day_of_year,
            "good_band_low": self.good_band_low,
            "good_band_high": self.good_band_high,
            "target_temp": self.target_temp,
            "confidence": self.confidence,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AlmanacSlot":
        """Restore from stored dict."""
        obj = cls(data["period"], data["day_of_year"], data["target_temp"])
        obj.good_band_low = data.get("good_band_low", data["target_temp"] - 1.0)
        obj.good_band_high = data.get("good_band_high", data["target_temp"] + 1.0)
        obj.confidence = data.get("confidence", 0)
        last_updated = data.get("last_updated")
        obj.last_updated = datetime.fromisoformat(last_updated) if last_updated else None
        return obj


# -------------------------------------------------------------------
# Intervention event
# -------------------------------------------------------------------

class InterventionEvent:
    """
    A single recorded user adjustment.
    Stored in the 21-day rolling event log.
    """

    def __init__(
        self,
        dt: datetime,
        period: str,
        day_of_year: int,
        new_temp: float,
        direction: str,
        sensor_readings: dict[str, float],
    ) -> None:
        self.dt = dt
        self.period = period
        self.day_of_year = day_of_year
        self.new_temp = new_temp
        self.direction = direction
        self.sensor_readings = sensor_readings

    def to_dict(self) -> dict[str, Any]:
        return {
            "dt": self.dt.isoformat(),
            "period": self.period,
            "day_of_year": self.day_of_year,
            "new_temp": self.new_temp,
            "direction": self.direction,
            "sensor_readings": self.sensor_readings,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InterventionEvent":
        return cls(
            dt=datetime.fromisoformat(data["dt"]),
            period=data["period"],
            day_of_year=data["day_of_year"],
            new_temp=data["new_temp"],
            direction=data["direction"],
            sensor_readings=data.get("sensor_readings", {}),
        )


# -------------------------------------------------------------------
# Room almanac
# -------------------------------------------------------------------

class RoomAlmanac:
    """
    The full almanac for a single room.
    Contains all period/day slots and the rolling intervention log.
    """

    def __init__(self, room_id: str, default_temps: dict[str, float]) -> None:
        self.room_id = room_id
        self.slots: dict[str, AlmanacSlot] = {}
        self.events: deque[InterventionEvent] = deque()
        self.sensors: dict[str, SensorState] = {}
        self._initialise_slots(default_temps)

    def _initialise_slots(self, default_temps: dict[str, float]) -> None:
        """Pre-populate all 1825 slots with seed temperatures."""
        for period in PERIOD_NAMES:
            default = default_temps.get(period, 21.0)
            for day in range(1, ALMANAC_DAYS + 1):
                key = self._slot_key(period, day)
                self.slots[key] = AlmanacSlot(period, day, default)

    def _slot_key(self, period: str, day_of_year: int) -> str:
        return f"{period}:{day_of_year}"

    def get_slot(self, period: str | None = None, day: int | None = None) -> AlmanacSlot:
        """Return the slot for the given period and day, defaulting to now."""
        if period is None:
            period = get_current_period()
        if day is None:
            day = get_day_of_year()
        return self.slots[self._slot_key(period, day)]

    def add_sensor(self, sensor_id: str, trust_label: str = "uncertain") -> None:
        """Register a sensor with this room."""
        trust = trust_seed_from_label(trust_label)
        self.sensors[sensor_id] = SensorState(sensor_id, trust)

    def record_sensor_reading(
        self, sensor_id: str, temperature: float, dt: datetime | None = None
    ) -> None:
        """Log a temperature reading from a sensor."""
        if sensor_id in self.sensors:
            self.sensors[sensor_id].record_reading(temperature, dt)

    def record_intervention(
        self,
        new_temp: float,
        direction: str,
        dt: datetime | None = None,
    ) -> None:
        """
        Record a user adjustment and apply learning to the relevant slot.
        Snapshot current sensor readings at the moment of intervention.
        """
        if dt is None:
            dt = datetime.now()

        period = get_current_period(dt)
        day = get_day_of_year(dt.date())

        sensor_readings = {
            sid: state.latest_reading()
            for sid, state in self.sensors.items()
            if state.latest_reading() is not None
        }

        event = InterventionEvent(dt, period, day, new_temp, direction, sensor_readings)
        self.events.append(event)
        self._prune_events()

        slot = self.get_slot(period, day)
        slot.apply_learning(new_temp, direction)

        self._update_sensor_trust(sensor_readings, direction)

        _LOGGER.info(
            "Room [%s] intervention recorded: %.1f degrees (%s) period=%s day=%d",
            self.room_id,
            new_temp,
            direction,
            period,
            day,
        )

    def _prune_events(self) -> None:
        """Discard events older than ALMANAC_LEARNING_WINDOW days."""
        cutoff = datetime.now().timestamp() - (ALMANAC_LEARNING_WINDOW * 86400)
        while self.events and self.events[0].dt.timestamp() < cutoff:
            self.events.popleft()

    def _update_sensor_trust(
        self, sensor_readings: dict[str, float], direction: str
    ) -> None:
        """
        Nudge trust scores based on which sensors were most predictive.
        """
        slot = self.get_slot()
        for sensor_id, reading in sensor_readings.items():
            if sensor_id not in self.sensors:
                continue
            if direction == "down" and reading > slot.good_band_high:
                self.sensors[sensor_id].update_trust(was_predictive=True)
            elif direction == "up" and reading < slot.good_band_low:
                self.sensors[sensor_id].update_trust(was_predictive=True)
            else:
                self.sensors[sensor_id].update_trust(was_predictive=False)

    def current_target(self) -> float:
        """Return the almanac target temperature for the current slot."""
        return self.get_slot().target_temp

    def current_good_band(self) -> tuple[float, float]:
        """Return the good band for the current slot."""
        slot = self.get_slot()
        return slot.good_band_low, slot.good_band_high

    def current_confidence(self) -> float:
        """Return confidence for the current slot."""
        return self.get_slot().confidence_pct()

    def all_slots(self) -> list[AlmanacSlot]:
        """Return all slots for CSV snapshot."""
        return list(self.slots.values())

    def all_events(self) -> list[InterventionEvent]:
        """Return all events currently in the learning window."""
        return list(self.events)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the full room almanac for persistent storage."""
        return {
            "room_id": self.room_id,
            "slots": {k: v.to_dict() for k, v in self.slots.items()},
            "events": [e.to_dict() for e in self.events],
            "sensors": {k: v.to_dict() for k, v in self.sensors.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], default_temps: dict[str, float]) -> "RoomAlmanac":
        """Restore a RoomAlmanac from stored data."""
        obj = cls(data["room_id"], default_temps)
        for key, slot_data in data.get("slots", {}).items():
            obj.slots[key] = AlmanacSlot.from_dict(slot_data)
        for event_data in data.get("events", []):
            obj.events.append(InterventionEvent.from_dict(event_data))
        for sensor_id, sensor_data in data.get("sensors", {}).items():
            obj.sensors[sensor_id] = SensorState.from_dict(sensor_data)
        return obj

    def all_slots(self) -> list:
        """Return all slots for CSV snapshot."""
        return list(self.slots.values())

    def all_events(self) -> list:
        """Return all events currently in the learning window."""
        return list(self.events)
