"""
Adaptive Climate - the Now view's data (docs/V2_DESIGN.md, UI; phase 6).

Pure: Home Assistant's states, the config, the almanac in force and a few
runtime facts in; one JSON-able dict out, everything the Now view draws in
a single poll. It reads the world the way the generated templates do, so
the view shows what Home Assistant is acting on, through app.control:

  * the conditions entity counts only while its state is the section in
    force (else edges -1/+1 and no block), as _conditions_variables reads it;
  * a sensor is stale past system.sensor_stale_minutes since its (or its
    unit's) last_reported (control.is_stale), and then has no position;
  * positions and the quorum are control.evaluate_votes against those edges;
  * the block is the conditions' own, or a heat source that is on (shadow
    and active modes), as control.room_blocked;
  * the temperature a unit is driven to now is control.command_for.

The JS (app/static/now.js) only draws: no control rule is computed there.
"""

from __future__ import annotations

from datetime import datetime

from app import control
from app.generator import (SECTIONS, base_id, conditions_id, forced_id, grace_until_id,
                           guard_id, hold_id, leak_confirm_id, leak_id, offset_id,
                           scene_select_id, since_id, state_id, unblocked_id)

NO_STATE = ("unavailable", "unknown", None)
SLOW_FANS = ("low", "quiet", "auto", "silent")     # anything else on is "fast"
FAIL_OPEN_TEXT = {
    "missing": "no conditions published yet",
    "unknown": "the conditions entity is unknown",
    "unavailable": "the conditions entity is unavailable",
    "other_section": "conditions published for another section",
    control.NO_OUTDOOR: "no outdoor value",
    control.NO_COMFORT_POINT: "no comfort point yet",
}


def _num(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _time(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _attrs(state: dict | None) -> dict:
    return (state or {}).get("attributes") or {}


def _clock(when: datetime, now: datetime) -> str:
    """hh:mm in Home Assistant's time zone; with the date once it isn't today."""
    local = when.astimezone(now.tzinfo)
    return local.strftime("%H:%M") if local.date() == now.date() \
        else local.strftime("%d %b %H:%M").lstrip("0")


def _profile(config: dict, room: dict) -> dict:
    profiles = {p["id"]: p for p in config.get("schedule_profiles", [])}
    return profiles.get(room.get("schedule_profile", "default")) or profiles.get("default") \
        or (config.get("schedule_profiles") or [{}])[0]


def read_conditions(states: dict, room_id: str, section: str | None, config: dict) -> dict:
    """sensor.ac_conditions_<room> as the templates read it: live only while
    its state is the section in force; otherwise edges -1/+1, not blocked,
    and `reason` says why. block_mode is the entity's, or the config's."""
    s = states.get(conditions_id(room_id))
    a = _attrs(s)
    st = (s or {}).get("state")
    if s is None:
        reason = "missing"
    elif st in ("unknown", "unavailable"):
        reason = st
    elif st != section:
        reason = "other_section"
    else:
        reason = None
    live = reason is None
    mode = a.get("block_mode") if a.get("block_mode") in control.BLOCK_MODES else \
        (config.get("outdoor") or {}).get("block_mode", control.DEFAULT_BLOCK_MODE)
    cold, warm = (_num(a.get("cold_edge")), _num(a.get("warm_edge"))) if live else (None, None)
    out = {"live": live, "state": st, "reason": reason, "block_mode": mode,
           "cold_edge": -1.0 if cold is None else cold,
           "warm_edge": 1.0 if warm is None else warm,
           "blocked": live and a.get("blocked") is True,
           "would_block": live and a.get("would_block") is True}
    for key in ("margin", "comfort_point", "outdoor_mean", "outdoor_max", "delta_mean",
                "delta_max", "lean"):
        out[key] = _num(a.get(key)) if live else None
    out["fail_open"] = (a.get("fail_open") if live else reason)
    out["computed_at"] = a.get("computed_at") if live else None
    return out


def _sensor(sensor: dict, room: dict, states: dict, learned: dict, now: datetime,
            stale_minutes: int) -> dict:
    source = sensor.get("source")
    if source is None:
        entity = sensor.get("entity_id")
        st = states.get(entity)
        reading = None if (st or {}).get("state") in NO_STATE else _num((st or {}).get("state"))
    else:
        unit = next((u for u in room["units"] if u["id"] == source.get("unit_id")), None)
        entity = unit["entity_id"] if unit else None
        st = states.get(entity) if entity else None
        reading = None if (st or {}).get("state") in NO_STATE else \
            _num(_attrs(st).get(source.get("attribute", "current_temperature")))
    reported = _time((st or {}).get("last_reported"))
    stale = control.is_stale(reported, now, stale_minutes)
    entry = learned.get(sensor["id"]) or {}
    status = "stale" if stale and reading is not None else \
        "unavailable" if reading is None else \
        "unlearned" if entry.get("comfort") is None else "ok"
    return {"id": sensor["id"], "name": sensor.get("name", sensor["id"]), "entity_id": entity,
            "virtual": source is not None, "reading": reading,
            "last_reported": reported.isoformat() if reported else None,
            "stale": stale and reading is not None,
            "stale_since": _clock(reported, now) if stale and reported else None,
            "status": status, "comfort": entry.get("comfort"), "band": entry.get("band"),
            "trust": entry.get("trust")}


def _fan_speed(hvac: str | None, fan: str | None) -> str:
    """The centre fan: stopped while off (or not reporting), slow on a low
    fan, fast on anything stronger."""
    if hvac in NO_STATE or hvac == "off":
        return "stopped"
    return "slow" if (fan or "").lower() in SLOW_FANS or not fan else "fast"


def _unit(config: dict, room: dict, unit: dict, states: dict, section_units: dict,
          now: datetime) -> dict:
    r, u = room["id"], unit["id"]
    st = states.get(unit["entity_id"])
    a = _attrs(st)
    hvac = (st or {}).get("state")
    helper = (states.get(state_id(r, u)) or {}).get("state")
    has_helpers = helper in control.STATES

    def on(entity_id: str) -> bool:
        return (states.get(entity_id) or {}).get("state") == "on"

    def stamp(entity_id: str) -> datetime | None:
        ts = _num(_attrs(states.get(entity_id)).get("timestamp"))
        return None if ts is None else datetime.fromtimestamp(ts, now.tzinfo)

    base = _num((states.get(base_id(r, u)) or {}).get("state")) or None    # 0: unset
    offset = int(_num((states.get(offset_id(r, u)) or {}).get("state")) or 0)
    grace = stamp(grace_until_id(r, u))
    since = stamp(since_id(r, u))
    applied = (section_units.get(u) or {}).get("setpoint_applied")
    ctx = control.Context(setpoint_applied=applied,
                          unit_min=_num(a.get("min_temp")) or control.DEFAULT_MIN_TEMP,
                          unit_max=_num(a.get("max_temp")) or control.DEFAULT_MAX_TEMP,
                          config=control.ControlConfig.from_config(config))
    target = control.target_base(control.Unit(base=base), ctx)
    command = control.command_for(helper, target, offset, ctx) \
        if helper in ("cool", "boost") else None
    leak_on = bool(unit.get("leak_detection", {}).get("enabled"))
    return {
        "id": u, "name": unit.get("name", u), "entity_id": unit["entity_id"],
        "available": hvac not in NO_STATE, "hvac_mode": hvac, "fan_mode": a.get("fan_mode"),
        "current_temperature": _num(a.get("current_temperature")),
        "setpoint": _num(a.get("temperature")),
        "fan_speed": _fan_speed(hvac, a.get("fan_mode")),
        "control": None if not has_helpers else {
            "state": helper, "base": base, "offset": offset,
            "forced": on(forced_id(r, u)) and helper in control.FORCED_STATES,
            "unblocked": on(unblocked_id(r, u)),
            "since": since.isoformat() if since else None,
            "in_grace": grace is not None and now < grace,
        },
        "setpoint_applied": applied, "target": target,
        "driving": None if command is None else command.temperature,
        "leak": {"enabled": leak_on, "latched": leak_on and on(leak_id(r, u)),
                 "confirming": leak_on and on(leak_confirm_id(r, u))},
    }


def build_room(config: dict, room: dict, states: dict, almanac: dict, now: datetime,
               margin: float | None = None, last_vote: dict | None = None) -> dict:
    """One room's card."""
    r = room["id"]
    selected = (states.get(scene_select_id(r)) or {}).get("state")
    section = selected if selected in SECTIONS else None
    sect = (((almanac or {}).get("sections") or {}).get(section) or {}) if section else {}
    learned = sect.get("sensors") or {}
    stale_minutes = (config.get("system") or {}).get("sensor_stale_minutes",
                                                     control.DEFAULT_STALE_MINUTES)
    cond = read_conditions(states, r, section, config)
    sensors = [_sensor(s, room, states, learned, now, stale_minutes) for s in room["sensors"]]
    usable = {s["id"]: None if s["stale"] else s["reading"] for s in sensors}
    votes = control.evaluate_votes(usable, sect, cond["cold_edge"], cond["warm_edge"])
    for s in sensors:
        p = votes.positions.get(s["id"])
        s["position"] = None if p is None else round(p, 3)
        s["zone"] = "none" if p is None else "cold" if p <= cond["cold_edge"] else \
            "warm" if p >= cond["warm_edge"] else "ok"
    heat = [{"entity_id": e, "on": (states.get(e) or {}).get("state") == "on"}
            for e in room.get("heat_sources") or []]
    heat_on = any(h["on"] for h in heat)
    blocked = control.room_blocked(control.Context(
        setpoint_applied=None, blocked=cond["blocked"], heat_source_on=heat_on,
        block_mode=cond["block_mode"]))
    profile = _profile(config, room)
    section_name = next((s.get("name") for s in profile.get("sections", [])
                         if s.get("id") == section and s.get("name")),
                        section.title() if section else None)
    comfort = cond["comfort_point"] if cond["live"] and cond["comfort_point"] is not None \
        else control.comfort_point(sect, usable)
    return {
        "id": r, "name": room.get("name", r), "section": section,
        "section_name": section_name,
        "hold": (states.get(hold_id(r)) or {}).get("state") == "on",
        "guard": (states.get(guard_id(r)) or {}).get("state") == "on",
        "comfort_point": comfort, "margin": margin,
        "conditions": cond,
        "block": {"blocked": blocked,
                  "by": "forecast" if cond["blocked"] else "heat" if blocked else None,
                  "would_block": cond["would_block"] and not cond["blocked"],
                  "block_mode": cond["block_mode"]},
        "heat_sources": heat,
        "votes": {k: getattr(votes, k) for k in ("met", "tally", "required", "direction",
                                                  "warm_side", "cold_side")},
        "units": [_unit(config, room, u, states, sect.get("units") or {}, now)
                  for u in room["units"]],
        "sensors": sensors,
        "last_vote": last_vote,
    }


def warnings_for(rooms: list[dict], outdoor_failing: str | None = None,
                 config_error: str | None = None) -> list[dict]:
    """The status strip: what needs a person's eye, most serious first."""
    out = []
    if config_error:
        out.append({"level": "error", "room": None, "kind": "config",
                    "text": f"The config doesn't validate: {config_error}"})
    if outdoor_failing:
        out.append({"level": "warning", "room": None, "kind": "outdoor",
                    "text": outdoor_failing})
    for room in rooms:
        name = room["name"]
        for u in room["units"]:
            if u["leak"]["latched"]:
                out.append({"level": "error", "room": room["id"], "kind": "leak",
                            "text": f"{name}: {u['name']} leak latched - off until it is "
                                    "fixed and confirmed (Leak fixed)"})
            elif not u["available"]:
                out.append({"level": "warning", "room": room["id"], "kind": "unit",
                            "text": f"{name}: {u['name']} is {u['hvac_mode'] or 'missing'}"})
        if room["hold"]:
            out.append({"level": "warning", "room": room["id"], "kind": "hold",
                        "text": f"{name} is paused (ac_hold is on): no commands are sent "
                                "until a person turns it off"})
        cond = room["conditions"]
        if not cond["live"] or cond["fail_open"]:
            out.append({"level": "info", "room": room["id"], "kind": "conditions",
                        "text": f"{name}: {FAIL_OPEN_TEXT.get(cond['fail_open'], 'no conditions')}"
                                " - failing open: no lean, no block"})
        for s in room["sensors"]:
            if s["stale"]:
                out.append({"level": "warning", "room": room["id"], "kind": "stale",
                            "text": f"{name}: {s['name']} stale since {s['stale_since']} - no "
                                    "vote, no learning"})
            elif s["status"] == "unavailable":
                out.append({"level": "warning", "room": room["id"], "kind": "sensor",
                            "text": f"{name}: {s['name']} is unavailable"})
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(out, key=lambda w: order[w["level"]])


def build_now(config: dict, states: dict, almanacs: dict, now: datetime,
              margins: dict | None = None, last_votes: dict | None = None,
              outdoor_failing: str | None = None, config_error: str | None = None) -> dict:
    """Everything the Now view draws: per enabled room, and the warnings."""
    rooms = [build_room(config, room, states, almanacs.get(room["id"]) or {}, now,
                        (margins or {}).get(room["id"]), (last_votes or {}).get(room["id"]))
             for room in config.get("rooms", []) if room.get("enabled", True)]
    return {"connected": True, "generated_at": now.isoformat(timespec="seconds"),
            "tz": str(now.tzinfo), "boost_degrees": control.ControlConfig.from_config(
                config).boost_degrees,
            "rooms": rooms, "warnings": warnings_for(rooms, outdoor_failing, config_error)}

