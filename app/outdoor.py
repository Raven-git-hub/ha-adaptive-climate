"""
Adaptive Climate - outdoor input (docs/V2_DESIGN.md, Outdoor temperature).

Pure functions over Home Assistant's own shapes: a weather entity's state
and a weather.get_forecasts response. The runtime fetches, caches and
records; the doctor reads the same way. Phase 4 only records the values;
nothing in control reads them until phase 5 (the lean and the block).

  * A section's values: the hourly forecast entries whose times fall in the
    section's window [planned start, next section's planned start), their
    mean (which will drive the lean) and max (the block).
  * A point value, on every heartbeat and reaction: the weather entity's
    current `temperature` attribute, or None if the entity is unavailable
    or hasn't updated for POINT_MAX_AGE.

Only the forecast source is read. `probe` stays in the config schema but
isn't read yet; `none` records nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

FORECAST_HOURLY = 2                      # WeatherEntityFeature.FORECAST_HOURLY (supported_features bit)
POINT_MAX_AGE = timedelta(hours=3)       # a weather entity not updated for longer has no point value
CACHE_MAX_AGE = timedelta(hours=2)       # a crossover fetches first if the cached forecast is older
HOUR = timedelta(hours=1)
NO_STATE = ("unavailable", "unknown", None)


@dataclass(frozen=True)
class Hour:
    """One hourly forecast entry."""
    at: datetime
    temperature: float
    condition: str | None = None


@dataclass(frozen=True)
class Forecast:
    """The latest hourly forecast fetched, with when it was fetched."""
    entity_id: str
    fetched_at: datetime
    hours: tuple[Hour, ...]


@dataclass(frozen=True)
class WindowStats:
    """A section window's forecast: the mean and max of the hours that fall
    in it, how many did, and how many the window holds on the forecast's
    hourly grid. `first` and `last` are the first and last hour used."""
    mean: float | None
    max: float | None
    hours: int
    expected: int
    first: datetime | None = None
    last: datetime | None = None

    @property
    def partial(self) -> bool:
        return 0 < self.hours < self.expected


def _float(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None             # NaN is no value


def _time(v, tz=None) -> datetime | None:
    """An ISO 8601 time; a naive one is taken to be in tz."""
    try:
        t = datetime.fromisoformat(v) if isinstance(v, str) and v else None
    except ValueError:
        return None
    if t is not None and t.tzinfo is None and tz is not None:
        t = t.replace(tzinfo=tz)
    return t


def supports_hourly(state: dict | None) -> bool:
    """Does the weather entity offer an hourly forecast (supported_features
    bit 2)?"""
    features = ((state or {}).get("attributes") or {}).get("supported_features")
    try:
        return bool(int(features or 0) & FORECAST_HOURLY)
    except (TypeError, ValueError):
        return False


def parse_forecast(response, entity_id: str, tz=None) -> list[Hour]:
    """The entries for entity_id in a weather.get_forecasts response
    ({entity_id: {"forecast": [...]}}), in time order. An entry with no time
    or no numeric temperature is skipped, so a forecast of nothing usable
    is empty."""
    body = response.get(entity_id) if isinstance(response, dict) else None
    entries = body.get("forecast") if isinstance(body, dict) else None
    hours = []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        at, temperature = _time(e.get("datetime"), tz), _float(e.get("temperature"))
        if at is not None and temperature is not None:
            hours.append(Hour(at, temperature, e.get("condition")))
    return sorted(hours, key=lambda h: h.at)


def current_temperature(state: dict | None, now: datetime,
                        max_age: timedelta = POINT_MAX_AGE) -> float | None:
    """The weather entity's current temperature attribute, or None if it is
    unavailable, has no last_updated, or last updated longer than max_age
    ago (exactly max_age is still fresh)."""
    if not state or state.get("state") in NO_STATE:
        return None
    updated = _time(state.get("last_updated"))
    if updated is None or now - updated > max_age:
        return None
    return _float((state.get("attributes") or {}).get("temperature"))


def _slots(anchor: datetime, start: datetime, end: datetime) -> int:
    """How many of anchor + k hours (any whole k) fall in [start, end)."""
    def first_at_or_after(t: datetime) -> int:      # ceil((t - anchor) / 1 h)
        return -int((anchor - t).total_seconds() // 3600)
    return max(0, first_at_or_after(end) - first_at_or_after(start))


def window_stats(hours, start: datetime, end: datetime) -> WindowStats:
    """The mean and max of the hourly entries whose times fall in
    [start, end). A window the forecast covers only part of uses what is
    covered (`partial`); one it doesn't reach has no values."""
    hours = list(hours)
    expected = _slots(hours[0].at, start, end) if hours else 0
    inside = [h for h in hours if start <= h.at < end]
    if not inside:
        return WindowStats(None, None, 0, expected)
    temps = [h.temperature for h in inside]
    return WindowStats(round(sum(temps) / len(temps), 2), max(temps), len(inside),
                       max(expected, len(inside)), inside[0].at, inside[-1].at)


def span_text(start: datetime, end: datetime, tz=None) -> str:
    """'16:00-20:30', in tz."""
    def hm(t: datetime) -> str:
        return (t.astimezone(tz) if tz else t).strftime("%H:%M")
    return f"{hm(start)}-{hm(end)}"
