"""
Adaptive Climate - doctor offline check.

Runs tools/doctor.py's whole _run() against a stub Home Assistant (REST and
WebSocket, no network) holding two weather entities: weather.home, with an
hourly forecast, and weather.daily, without one (supported_features bit 2
clear). Verifies the outdoor step, which runs without --v2-preflight:
  * both entities are listed with their current temperature and whether
    they offer an hourly forecast
  * the configured weather.home shows its next 6 hourly entries, from the
    current hour, fetched through weather.get_forecasts (hourly, with its
    response)
  * a configured entity that lacks hourly support, is missing, or returns
    an empty forecast is flagged: a hard failure (exit 1) with
    outdoor.source 'forecast', a warning with 'none'
  * with no config, the entities are still listed

    PYTHONPATH=. python tools/doctor_check.py
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.doctor as doctor                                    # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")
NOW = datetime.now(timezone.utc)
HOUR0 = NOW.replace(minute=0, second=0, microsecond=0)
STATES = [
    {"entity_id": "climate.lounge", "state": "cool", "attributes": {}},
    {"entity_id": "weather.home", "state": "sunny",
     "attributes": {"temperature": 29.4, "temperature_unit": "\u00b0C",
                    "supported_features": 3}},                    # daily + hourly
    {"entity_id": "weather.daily", "state": "cloudy",
     "attributes": {"temperature": 28.0, "temperature_unit": "\u00b0C",
                    "supported_features": 1}},                    # daily only
]


class StubRest:
    def __init__(self, forecast: str = "ok"):
        self.forecast, self.forecast_calls = forecast, []
    async def ping(self):
        return {"message": "API running."}
    async def config(self):
        return {"version": "2026.10.1", "time_zone": "Asia/Hong_Kong",
                "unit_system": {"temperature": "\u00b0C"}}
    async def state(self, entity_id):
        return {"entity_id": entity_id, "state": "off", "attributes": {}}
    async def states(self):
        return STATES
    async def call_service(self, domain, service, data=None, *, return_response=False):
        assert (domain, service) == ("weather", "get_forecasts"), (domain, service)
        assert return_response and data["type"] == "hourly", data
        self.forecast_calls.append(data["entity_id"])
        if self.forecast == "empty":
            return {data["entity_id"]: {"forecast": []}}
        # the last hour, then 24 ahead: the doctor shows from the current hour
        return {data["entity_id"]: {"forecast": [
            {"datetime": (HOUR0 + timedelta(hours=k)).isoformat(),
             "temperature": 25.0 + k, "condition": "sunny"} for k in range(-1, 24)]}}
    async def close(self):
        pass


class StubWS:
    ha_version = "2026.10.1"
    def __init__(self, url, token, verify_ssl=True):
        pass
    async def connect(self):
        pass
    async def create_helper(self, domain, payload):
        return {"id": payload["name"]}
    async def delete_helper(self, domain, helper_id):
        return {}
    async def close(self):
        pass


def run(outdoor: dict | None, forecast: str = "ok") -> tuple[int, str, StubRest]:
    """The doctor's whole run (no --v2-preflight) against the stub, with a
    config holding `outdoor` (no config at all if None): (exit code,
    output without colours, the REST stub)."""
    rest = StubRest(forecast)
    doctor.HARest = lambda url, token, verify_ssl=True: rest
    doctor.HAWebSocket = StubWS
    path = None
    if outdoor is not None:
        path = Path(tempfile.mkdtemp()) / "config.json"
        path.write_text(json.dumps({"version": 2, "rooms": [], "outdoor": outdoor}))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = asyncio.run(doctor._run("http://ha.test:8123", "token", True,
                                       str(path) if path else None, preflight=False))
    return code, ANSI.sub("", out.getvalue()), rest


def outdoor_lines(text: str) -> list[str]:
    section = text.split("outdoor input (weather entities)\n", 1)[1].split("\n\n", 1)[0]
    return [line.strip() for line in section.splitlines()]


def main() -> int:
    forecast = {"source": "forecast", "weather_entity": "weather.home"}
    code, text, rest = run(forecast)
    lines = outdoor_lines(text)
    assert code == 0, text
    assert "\u2713 weather.home (configured): 29.4 \u00b0C, sunny; hourly forecast: yes" in lines, lines
    assert "\u00b7 weather.daily: 28.0 \u00b0C, cloudy; hourly forecast: no" in lines, lines
    assert rest.forecast_calls == ["weather.home"], rest.forecast_calls
    assert "\u2713 weather.home: next 6 hourly entries:" in lines, lines
    shown = [line for line in lines if re.match(r"\u00b7 +\w{3} \d\d:\d\d ", line)]
    assert [float(line.split()[3]) for line in shown] == [25.0 + k for k in range(6)], shown
    first = HOUR0.astimezone(doctor.ZoneInfo("Asia/Hong_Kong")).strftime("%a %H:%M")
    assert shown[0] == f"\u00b7   {first}  25 \u00b0C  sunny", shown[0]
    assert "v2 preflight" not in text
    print("doctor: lists weather.home (29.4, hourly) and weather.daily (28.0, no hourly), and "
          "shows weather.home's next 6 hourly entries from the current hour, without "
          "--v2-preflight - OK")

    for outdoor, kind, want in (
            ({**forecast, "weather_entity": "weather.daily"}, "ok",
             "\u2717 outdoor.weather_entity weather.daily has no hourly forecast "
             "(supported_features bit 2 not set); pick one with hourly forecast: yes"),
            ({**forecast, "weather_entity": "weather.gone"}, "ok",
             "\u2717 outdoor.weather_entity weather.gone not found"),
            (forecast, "empty", "\u2717 weather.home returned an empty hourly forecast")):
        code, text, rest = run(outdoor, kind)
        assert code == 1 and want in outdoor_lines(text), (want, outdoor_lines(text))
        assert rest.forecast_calls == (["weather.home"] if kind == "empty" else []), \
            rest.forecast_calls
    print("doctor: with outdoor.source 'forecast', a configured entity without an hourly "
          "forecast, a missing one and an empty forecast are each a hard failure - OK")

    code, text, _ = run({"source": "none", "weather_entity": "weather.daily"})
    assert code == 0 and ("! outdoor.weather_entity weather.daily has no hourly forecast "
                          "(supported_features bit 2 not set) (outdoor.source is 'none', so "
                          "it isn't read); pick one with hourly forecast: yes"
                          in outdoor_lines(text)), outdoor_lines(text)
    code, text, rest = run(None)
    lines = outdoor_lines(text)
    assert code == 0 and rest.forecast_calls == [] and len(
        [line for line in lines if line.startswith(("\u2713 weather.", "\u00b7 weather."))]) == 2, lines
    print("doctor: with outdoor.source 'none' the same problem is a warning; with no config "
          "the weather entities are still listed - OK")
    print("\nALL DOCTOR CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
