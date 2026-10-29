"""
Adaptive Climate - Now view cases (phase 6), shared by tools/now_api_check.py
and tools/now_render_check.py.

Builds Home Assistant states, an almanac and the conditions entity for the
example config's Main Room at 20:40 on 3 August 2026 (Night), and turns them
into /api/now's payload through the real app.nowview.build_now, so the render
harness draws exactly what the API would send. The conditions entity's
attributes come from app.runtime's own _conditions_detail, as the runtime
pushes them.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import control, nowview                                 # noqa: E402
from app import generator as g                                   # noqa: E402
from app.config import migrate                                   # noqa: E402
from app.runtime import Runtime                                  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HK = ZoneInfo("Asia/Hong_Kong")
NOW = datetime(2026, 8, 3, 20, 40, tzinfo=HK)          # Night: 20:30-22:00
SECTION = "night"
ROOM, UNIT = "main_room", "main_room_ac"
SENSORS = ("living1", "living2", "living3")
HEATER = "switch.lounge_heater"
# The unit's mode and fan in each control state, as our commands leave it.
SHOWS = {"cool": ("cool", "low"), "boost": ("cool", "medium"), "rest": ("off", None),
         "user_off": ("off", None), "manual": ("dry", "auto"), "config_off": ("off", None),
         "leak": ("off", None)}


def example_config() -> dict:
    return migrate(json.loads((ROOT / "examples" / "config.example.json").read_text()))


def world(state: str = "cool", *, offset: int = 0, base: float = 0.0, forced: bool = False,
          readings=(24.3, 23.8, 24.6), comfort=24.0, band=1.0, trust=(0.8, 0.5, 0.1),
          forecast: tuple | None = (27.0, 29.0), block_mode: str = "shadow",
          was_blocked: bool = False, margin: float = -1.0, stale: dict | None = None,
          heat_on: bool | None = None, leak: bool = False, hold: bool = False,
          second_unit: str | None = None, applied: float = 23.0, rooms: str = "one",
          unit_available: bool = True, unit_setpoint: float | None = None,
          now: datetime = NOW) -> tuple[dict, dict, dict, dict]:
    """(config, states, almanacs, margins) for one Main Room situation.
    forecast: (mean, max) for the conditions entity, None for no entity.
    stale: {sensor_id: minutes since last reported}. rooms "all" keeps the
    example's other rooms (with nothing in HA for them). now: the time the
    states are reported against (the API test uses the real clock)."""
    cfg = example_config()
    room = next(r for r in cfg["rooms"] if r["id"] == ROOM)
    if rooms == "one":
        cfg["rooms"] = [room]
    if heat_on is not None:
        room["heat_sources"] = [HEATER]
    if leak:
        room["units"][0]["leak_detection"] = {"enabled": True,
                                              "sensor_entity_id": "binary_sensor.lounge_leak"}
    if second_unit:
        room["units"].append({"id": "dining_ac", "name": "Dining AC",
                              "entity_id": "climate.dining"})
    cfg["outdoor"].update(source="forecast", weather_entity="weather.forecast_home",
                          block_mode=block_mode)
    states: dict = {}

    def put(entity_id, st, last_reported=None, **attributes):
        states[entity_id] = {"entity_id": entity_id, "state": st, "attributes": attributes,
                             "last_reported": (last_reported or now).isoformat()}

    put(g.scene_select_id(ROOM), SECTION, options=list(g.SECTIONS))
    put(g.guard_id(ROOM), "off")
    put(g.hold_id(ROOM), "on" if hold else "off")
    for sid, value in zip(SENSORS, readings):
        sensor = next(s for s in room["sensors"] if s["id"] == sid)
        age = (stale or {}).get(sid)
        put(sensor["entity_id"], "unavailable" if value is None else str(value),
            now - timedelta(minutes=age if age is not None else 2),
            unit_of_measurement="°C")
    for unit, st, frc in ((room["units"][0], state, forced),
                          *(((room["units"][1], second_unit, False),) if second_unit else ())):
        u = unit["id"]
        mode, fan = SHOWS[st]
        put(g.state_id(ROOM, u), st, options=list(control.STATES))
        put(g.base_id(ROOM, u), str(float(base)))
        put(g.offset_id(ROOM, u), str(float(offset)))
        put(g.forced_id(ROOM, u), "on" if frc else "off")
        put(g.unblocked_id(ROOM, u), "off")
        put(g.since_id(ROOM, u), "2026-08-03 20:30:00",
            timestamp=(now - timedelta(minutes=10)).timestamp())
        put(g.grace_until_id(ROOM, u), "2026-08-03 00:00:00",
            timestamp=now.replace(hour=0, minute=0).timestamp())
        ctx = control.Context(setpoint_applied=applied)
        cmd = control.command_for(st, control.target_base(control.Unit(base=base or None), ctx),
                                  offset, ctx)
        shown = unit_setpoint if unit_setpoint is not None and unit["id"] == UNIT else \
            cmd.temperature if cmd and cmd.temperature else applied
        put(unit["entity_id"], mode if unit_available else "unavailable",
            temperature=shown,
            current_temperature=24.9, fan_mode=fan or "low", min_temp=16, max_temp=30,
            fan_modes=["low", "medium", "high", "auto"])
        if unit.get("leak_detection", {}).get("enabled"):
            put(g.leak_id(ROOM, u), "on" if leak else "off")
            put(g.leak_confirm_id(ROOM, u), "off")
    if heat_on is not None:
        put(HEATER, "on" if heat_on else "off")
    sect_sensors = {sid: {"comfort": comfort, "band": band,
                          "trust": t} for sid, t in zip(SENSORS, trust)}
    almanac = {"room_id": ROOM, "sections": {SECTION: {
        "status": "learned", "valid_from": "2026-08-03", "confidence": 0.6,
        "units": {UNIT: {"setpoint": applied + 0.2, "setpoint_applied": applied, "off": False},
                  "dining_ac": {"setpoint": applied, "setpoint_applied": applied, "off": False}},
        "sensors": sect_sensors}}}
    if forecast is not None:
        usable = {sid: (None if (stale or {}).get(sid, 0) > 90 else v)
                  for sid, v in zip(SENSORS, readings)}
        cp = control.comfort_point({"sensors": sect_sensors}, usable)
        cond = control.section_conditions(SECTION, cp, forecast[0], forecast[1], margin,
                                          was_blocked, block_mode, 0.1)
        put(g.conditions_id(ROOM), SECTION, room_id=ROOM, **Runtime._conditions_detail(cond),
            edges={}, computed_at=(now - timedelta(minutes=10)).isoformat())
    return cfg, states, {ROOM: almanac}, {ROOM: margin}


def payload(last_vote: dict | None = None, outdoor_failing: str | None = None,
            config_error: str | None = None, **kw) -> dict:
    """/api/now's JSON for a world(**kw)."""
    cfg, states, almanacs, margins = world(**kw)
    return json.loads(json.dumps(nowview.build_now(
        cfg, states, almanacs, NOW, margins, {ROOM: last_vote} if last_vote else {},
        outdoor_failing, config_error)))
