"""
Adaptive Climate - helper and automation generator.

Pure and offline: config in, helper specs + automation configs out. No HA
connection, no learned state baked in. The emitted automations read the
almanac (sensor.ac_almanac_<room>), each unit's control helpers and the
live entity attributes at runtime, so changing a band, a setpoint or a
mode needs only an almanac republish - regeneration is required only when
*entities* change. Output is deterministic, so regeneration is a safe
overwrite.

Everything emitted is namespaced `ac_*`, derived from the room's and the
unit's stable ids. That naming is what lets deploy own our artifacts
completely - create, update, prune our own - while never touching a
foreign automation. Conflict handling lives in deploy, not here: this
module never looks at what is already in HA.

The control logic is app/control.py's, rendered as Jinja (docs/V2_DESIGN.md,
Control: cool or rest): the scene runs on_section_change, maintenance runs
evaluate_votes and step, the manual-change automation runs
on_climate_change, and the leak automations run step's leak rules.
tools/control_logic_check.py replays tools/control_check.py's scenario
table through these templates against app/control.py.

Per-unit hardware differences (fan availability, min/max range - see
docs/HARDWARE.md) are handled inside the generated templates by reading the
unit's own attributes at runtime, so one generated automation adapts to
each unit and to units that hide their fan modes in some hvac modes.
"""

from __future__ import annotations

import io
from typing import Any

import yaml

from app import control

SECTIONS = ("sunrise", "day", "afternoon", "sunset", "night", "sleep")


# ---------------------------------------------------------------------
# YAML serialisation (for human inspection / the render harness; the
# canonical output is the Python dicts that deploy consumes over the API)
# ---------------------------------------------------------------------

class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def _str_representer(dumper: yaml.Dumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_representer)


def dump(obj: Any) -> str:
    buf = io.StringIO()
    yaml.dump(obj, buf, Dumper=_Dumper, sort_keys=False,
              default_flow_style=False, allow_unicode=True, width=100)
    return buf.getvalue()


# ---------------------------------------------------------------------
# Naming - single source of truth for every generated entity id.
# ---------------------------------------------------------------------

def guard_id(room: str) -> str:        return f"input_boolean.ac_active_{room}"
def hold_id(room: str) -> str:         return f"input_boolean.ac_hold_{room}"
def scene_select_id(room: str) -> str: return f"input_select.ac_scene_{room}"
def almanac_id(room: str) -> str:      return f"sensor.ac_almanac_{room}"

def leak_id(room: str, unit: str) -> str:
    return f"input_boolean.ac_leak_{room}_{unit}"

def leak_confirm_id(room: str, unit: str) -> str:
    return f"input_boolean.ac_leak_confirmed_{room}_{unit}"

# A unit's control state (app.control.Unit), one helper per field.
def state_id(room: str, unit: str) -> str:
    return f"input_select.ac_state_{room}_{unit}"

def offset_id(room: str, unit: str) -> str:
    return f"input_number.ac_offset_{room}_{unit}"

def base_id(room: str, unit: str) -> str:
    return f"input_number.ac_base_{room}_{unit}"

def since_id(room: str, unit: str) -> str:
    return f"input_datetime.ac_since_{room}_{unit}"

def grace_until_id(room: str, unit: str) -> str:
    return f"input_datetime.ac_grace_until_{room}_{unit}"

def unblocked_id(room: str, unit: str) -> str:
    return f"input_boolean.ac_unblocked_{room}_{unit}"

def scene_automation_id(room: str, section: str) -> str:
    return f"ac_scene_{room}_{section}"

def maintenance_automation_id(room: str) -> str:
    return f"ac_maintenance_{room}"

def manual_automation_id(room: str, unit: str) -> str:
    return f"ac_manual_{room}_{unit}"

def leak_automation_id(room: str, unit: str) -> str:
    return f"ac_leak_{room}_{unit}"

def watchdog_automation_id(room: str) -> str:
    return f"ac_watchdog_{room}"


def _split(entity_id: str) -> tuple[str, str]:
    """('input_boolean.ac_hold_x') -> ('input_boolean', 'ac_hold_x')."""
    domain, _, object_id = entity_id.partition(".")
    return domain, object_id


def _enabled_rooms(config: dict) -> list[dict]:
    return [r for r in config.get("rooms", []) if r.get("enabled", True)]


def _sections_for(config: dict, room: dict) -> list[dict]:
    profiles = {p["id"]: p for p in config.get("schedule_profiles", [])}
    profile = profiles.get(room.get("schedule_profile", "default")) \
        or profiles.get("default") or config["schedule_profiles"][0]
    by_id = {s["id"]: s for s in profile["sections"]}
    return [by_id[s] for s in SECTIONS]


def _leak_units(room: dict) -> list[dict]:
    return [u for u in room["units"]
            if u.get("leak_detection", {}).get("enabled")]


def _leak_enabled(unit: dict) -> bool:
    return bool(unit.get("leak_detection", {}).get("enabled"))


def _forced_off_sections(room: dict, unit: dict) -> list[str]:
    """Sections whose scene config forces this unit off (config_off)."""
    scenes = room.get("scenes", {})
    return [s for s in SECTIONS
            if scenes.get(s, {}).get("units", {}).get(unit["id"], {}).get("mode", "auto") == "off"]


# ---------------------------------------------------------------------
# Helpers  ->  list of {domain, object_id, name, ...} specs
# Deploy creates each over the WebSocket API. name == object_id so it
# slugifies back to itself (docs/DESIGN.md 13). No helper carries an
# `initial`: in Home Assistant that overrides the restored value at every
# restart, which would wipe a person's base or a user_off. Deploy sets the
# starting values of the helpers it has just created instead.
# ---------------------------------------------------------------------

BASE_RANGE = (0, 100, 0.5)      # 0 means unset; admits any degC or degF setpoint


def build_helpers(config: dict) -> list[dict]:
    specs: list[dict] = []
    cap = control.ControlConfig.from_config(config).offset_cap

    def spec(entity_id: str, **extra) -> dict:
        domain, obj = _split(entity_id)
        return {"domain": domain, "object_id": obj, "name": obj, **extra}

    for room in _enabled_rooms(config):
        r = room["id"]
        specs.append(spec(guard_id(r)))
        specs.append(spec(hold_id(r)))
        specs.append(spec(scene_select_id(r), options=list(SECTIONS)))
        for unit in room["units"]:
            u = unit["id"]
            specs.append(spec(state_id(r, u), options=list(control.STATES)))
            specs.append(spec(offset_id(r, u), min=-cap, max=cap, step=1, mode="box"))
            lo, hi, step = BASE_RANGE
            specs.append(spec(base_id(r, u), min=lo, max=hi, step=step, mode="box"))
            specs.append(spec(since_id(r, u), has_date=True, has_time=True))
            specs.append(spec(grace_until_id(r, u), has_date=True, has_time=True))
            specs.append(spec(unblocked_id(r, u)))
        for unit in _leak_units(room):
            specs.append(spec(leak_id(r, unit["id"])))
            specs.append(spec(leak_confirm_id(r, unit["id"])))
    return specs


# ---------------------------------------------------------------------
# The control core as Jinja (app/control.py).
#
# Each per-unit template reads the unit's helpers once (_read_unit), runs
# one entry point of app.control, and renders one dict:
#
#   {go, state, offset, base, since, grace, unb, cmd}
#
# go: write the helpers, and send cmd if there is one. base 0 means unset;
# since and grace are epoch seconds (whole seconds, as input_datetime keeps
# them); cmd is {hvac_mode, fan_mode, temperature} or none. Template
# fragments use <<NAME>> tokens, filled by _fill, to keep Jinja braces clear
# of Python formatting.
# ---------------------------------------------------------------------

NOW_TS = "{{ now().timestamp() | int }}"


def _fill(template: str, **subs: Any) -> str:
    for key, value in subs.items():
        template = template.replace(f"<<{key}>>", str(value))
    return template


def _live_section_variables(room: dict) -> dict:
    """The active section and its almanac from live HA state (the scene
    select and the almanac sensor), for automations that don't know the
    section at generation time."""
    alm = almanac_id(room["id"])
    return {
        "section": f"{{{{ states('{scene_select_id(room['id'])}') }}}}",
        "secs": f"{{{{ state_attr('{alm}','sections') or {{}} }}}}",
        "sect": "{{ secs.get(section) or {} }}",
        "unit_data": "{{ sect.get('units') or {} }}",
    }


def _blocked_template(room: dict) -> str:
    """Whether cooling is blocked for the room: the one place every control
    template reads it from. False until phase 5 publishes the block decision
    and the room's heat sources here."""
    return "{{ false }}"


def _sa_live(unit: dict) -> str:
    return f"(unit_data.get('{unit['id']}') or {{}}).get('setpoint_applied')"


def _sa_literal(room: dict, section: str, unit: dict) -> str:
    return _fill("((((state_attr('<<ALM>>','sections') or {}).get('<<S>>') or {})"
                 ".get('units') or {}).get('<<U>>') or {}).get('setpoint_applied')",
                 ALM=almanac_id(room["id"]), S=section, U=unit["id"])


def _leak_on(room: dict, unit: dict) -> str:
    if not _leak_enabled(unit):
        return "false"
    return f"states('{leak_id(room['id'], unit['id'])}') == 'on'"


def _read_unit(room: dict, unit: dict, sa: str, leak: str) -> str:
    """The unit's record and context: st, off, b (0 = unset), since, grace,
    unb, leak, sa (setpoint_applied), tb (target base), lo and hi (its
    range), and r, the result namespace, starting as the unit is."""
    r, u = room["id"], unit["id"]
    return _fill(
        "{%- set st = states('<<STATE>>') -%}"
        "{%- set off = states('<<OFFSET>>') | float(0) | int -%}"
        "{%- set b = states('<<BASE>>') | float(0) -%}"
        "{%- set since = state_attr('<<SINCE>>', 'timestamp') -%}"
        "{%- set grace = state_attr('<<GRACE>>', 'timestamp') -%}"
        "{%- set unb = states('<<UNB>>') == 'on' -%}"
        "{%- set leak = <<LEAK>> -%}"
        "{%- set sa = <<SA>> -%}"
        "{%- set tb = b if b else sa -%}"
        "{%- set lo = state_attr('<<E>>', 'min_temp') or <<LO>> -%}"
        "{%- set hi = state_attr('<<E>>', 'max_temp') or <<HI>> -%}"
        "{%- set r = namespace(go=false, state=st, offset=off, base=b, since=since, "
        "grace=grace, unb=unb, cmd=none) -%}",
        STATE=state_id(r, u), OFFSET=offset_id(r, u), BASE=base_id(r, u),
        SINCE=since_id(r, u), GRACE=grace_until_id(r, u), UNB=unblocked_id(r, u),
        LEAK=leak, SA=sa, E=unit["entity_id"],
        LO=repr(control.DEFAULT_MIN_TEMP), HI=repr(control.DEFAULT_MAX_TEMP))


_RESULT = ("{{ {'go': r.go, 'state': r.state, 'offset': r.offset, 'base': r.base, "
           "'since': r.since, 'grace': r.grace, 'unb': r.unb, 'cmd': r.cmd} }}")


def _clamp(value: str) -> str:
    return f"[[{value}, lo] | max, hi] | min"


def _command(state: str, base: str, offset: str, boost: float) -> str:
    """control.command_for as one Jinja expression; state, base and offset
    are Jinja expressions."""
    return _fill(
        "({'hvac_mode': 'cool', 'fan_mode': 'low', "
        "'temperature': (none if <<B>> is none else <<COOL>>) } if <<S>> == 'cool' "
        "else {'hvac_mode': 'cool', 'fan_mode': 'medium', "
        "'temperature': (none if <<B>> is none else <<BOOST>>) } if <<S>> == 'boost' "
        "else {'hvac_mode': 'off', 'fan_mode': none, 'temperature': none} "
        "if <<S>> in ['rest', 'config_off', 'user_off', 'leak'] "
        "else none)",
        S=state, B=base, COOL=_clamp(f"{base} + {offset}"),
        BOOST=_clamp(f"{base} + {offset} - {boost!r}"))


def _go(state: str, cfg: control.ControlConfig, offset: str | None = None) -> str:
    """control._go: a transition to `state` (offset capped if given), since
    = now, and its command."""
    capped = "" if offset is None else _fill(
        "{%- set r.offset = [[<<O>>, -<<CAP>>] | max, <<CAP>>] | min -%}",
        O=offset, CAP=cfg.offset_cap)
    return capped + _fill(
        "{%- set r.state = '<<S>>' -%}{%- set r.since = now_ts -%}{%- set r.go = true -%}"
        "{%- set r.cmd = <<CMD>> -%}",
        S=state, CMD=_command(f"'{state}'", "tb", "r.offset", cfg.boost_degrees))


def _min_time_met(seconds: float) -> str:
    """control.min_time_met: n heartbeats, met at (n - 0.5) x the interval."""
    return f"(since is none or now_ts - since >= {seconds!r})"


def _leak_trip(cfg: control.ControlConfig) -> str:
    """control._leak_step with the latch on: into leak, ahead of everything."""
    return "{%- if st != 'leak' -%}" + _go("leak", cfg) + "{%- endif -%}"


def _section_change(cfg: control.ControlConfig, forced_off: str) -> str:
    """control.on_section_change. A cleared grace is written as now, which
    is no longer in the future."""
    return _fill(
        "{%- set r.offset = 0 -%}{%- set r.base = sa if sa is not none else 0 -%}"
        "{%- set r.unb = false -%}{%- set r.grace = now_ts -%}{%- set r.since = now_ts -%}"
        "{%- set r.go = true -%}"
        "{%- if leak -%}{%- set r.state = 'leak' -%}"
        "{%- elif <<FORCED>> -%}{%- set r.state = 'config_off' -%}"
        "{%- elif sa is none -%}{%- if st == 'leak' -%}{%- set r.state = 'rest' -%}{%- endif -%}"
        "{%- else -%}{%- set r.state = 'rest' if blocked else 'cool' -%}"
        "{%- endif -%}"
        "{%- if leak or <<FORCED>> or sa is not none -%}{%- set r.cmd = <<CMD>> -%}"
        "{%- endif -%}",
        FORCED=forced_off, CMD=_command("r.state", "sa", "0", cfg.boost_degrees))


def _forced_off_live(room: dict, unit: dict) -> str:
    return f"(section in {_forced_off_sections(room, unit)!r})"


def _step(config: dict, room: dict, unit: dict) -> str:
    """control.step for one unit at a heartbeat."""
    cfg = control.ControlConfig.from_config(config)
    interval = config.get("system", {}).get("heartbeat_interval_minutes", 10)
    cool_min = _min_time_met((cfg.cool_min_heartbeats - 0.5) * interval * 60)
    rest_min = _min_time_met((cfg.rest_min_heartbeats - 0.5) * interval * 60)
    release_floor = "[votes.required, 1] | max"
    return (
        "{%- if leak -%}" + _leak_trip(cfg)
        + "{%- elif st == 'leak' -%}" + _section_change(cfg, _forced_off_live(room, unit))
        + f"{{%- elif st in {list(control.HANDS_OFF)!r} or tb is none -%}}"
        + "{%- elif st == 'cool' -%}"
        + f"{{%- if (grace is not none and now_ts < grace) or not {cool_min} -%}}"
        + "{%- elif blocked and not unb -%}" + _go("rest", cfg)
        + "{%- elif votes.met and votes.direction == 'too_warm' -%}" + _go("boost", cfg)
        + "{%- elif votes.met and votes.direction == 'too_cold' -%}" + _go("rest", cfg)
        + "{%- endif -%}"
        + "{%- elif st == 'boost' -%}"
        + f"{{%- if not {cool_min} -%}}"
        + "{%- elif blocked and not unb -%}" + _go("rest", cfg)
        + f"{{%- elif (since is not none and now_ts - since >= {cfg.boost_max_minutes * 60!r}) "
          f"or votes.warm_side < {release_floor} -%}}" + _go("cool", cfg, "off - 1")
        + "{%- endif -%}"
        + "{%- elif st == 'rest' -%}"
        + f"{{%- if (blocked and not unb) or not {rest_min} -%}}"
        + f"{{%- elif votes.cold_side < {release_floor} -%}}" + _go("cool", cfg, "off + 1")
        + "{%- endif -%}"
        + "{%- endif -%}")


def _climate_change(config: dict) -> str:
    """control.on_climate_change: implied_command, matches_command,
    classify_change and on_person_change, over the trigger's `before` and
    `after` ({mode, temperature, fan}). Sets ours and kind as well as r."""
    cfg = control.ControlConfig.from_config(config)
    md = repr(float(config.get("system", {}).get("reactive_min_delta", 0.5)))
    moved = _fill("(before.temperature is not none and after.temperature is not none "
                  "and (after.temperature - before.temperature) | abs >= <<MD>>)", MD=md)
    return _fill(
        "{%- set cmd = <<CMD>> -%}"
        "{%- set ours = false if cmd is none else ((after.mode == 'off' and not <<MOVED>>) "
        "if cmd.hvac_mode == 'off' else (after.mode == cmd.hvac_mode and "
        "(cmd.temperature is none or (after.temperature is not none and "
        "(after.temperature - cmd.temperature) | abs < <<MD>>)))) -%}"
        "{%- set pb = before.mode != 'off' -%}{%- set pa = after.mode != 'off' -%}"
        "{%- set kind = ('on' if pa else 'off') if pb != pa else ('setpoint' if <<MOVED>> "
        "else ('manual' if pa and (before.mode != after.mode or before.fan != after.fan) "
        "else none)) -%}"
        "{%- set g = now_ts + <<GRACE>> -%}"
        "{%- if ours or st == 'leak' or kind is none -%}"
        "{%- elif kind == 'setpoint' -%}"
        "{%- set r.base = after.temperature if after.temperature is not none else 0 -%}"
        "{%- set r.offset = 0 -%}{%- set r.go = true -%}"
        "{%- if pa and st != 'manual' -%}"
        "{%- set r.state = 'cool' -%}{%- set r.since = now_ts -%}{%- set r.grace = g -%}"
        "{%- endif -%}"
        "{%- elif kind == 'on' -%}"
        "{%- set r.state = 'cool' -%}"
        "{%- set r.base = after.temperature if after.temperature is not none else 0 -%}"
        "{%- set r.offset = 0 -%}{%- set r.unb = true -%}{%- set r.since = now_ts -%}"
        "{%- set r.grace = g -%}{%- set r.go = true -%}"
        "{%- elif kind == 'off' -%}"
        "{%- set r.state = 'user_off' -%}{%- set r.since = now_ts -%}{%- set r.go = true -%}"
        "{%- elif kind == 'manual' -%}"
        "{%- set r.state = 'manual' -%}{%- set r.since = now_ts -%}{%- set r.go = true -%}"
        "{%- endif -%}",
        CMD=_command("st", "tb", "off", cfg.boost_degrees), MOVED=moved, MD=md,
        GRACE=cfg.person_grace_minutes * 60)


def _unit_var(unit: dict) -> str:
    return f"unit_{unit['id']}"


def _template(expr: str) -> dict:
    return {"condition": "template", "value_template": expr}


def _set(service: str, entity: str, data: dict | None = None) -> dict:
    step: dict = {"service": service, "target": {"entity_id": entity}}
    if data:
        step["data"] = data
    return step


def _when(expr: str, sequence: list, default: list | None = None) -> dict:
    return {"choose": [{"conditions": [_template(expr)], "sequence": sequence}],
            "default": default or []}


def _write_unit(room: dict, unit: dict) -> dict:
    """Write a unit's result back to its helpers, when it has one."""
    r, u, v = room["id"], unit["id"], _unit_var(unit)
    return _when(f"{{{{ {v}.go }}}}", [
        _when(f"{{{{ {v}.state in {list(control.STATES)!r} }}}}", [
            _set("input_select.select_option", state_id(r, u), {"option": f"{{{{ {v}.state }}}}"})]),
        _set("input_number.set_value", offset_id(r, u), {"value": f"{{{{ {v}.offset }}}}"}),
        _set("input_number.set_value", base_id(r, u), {"value": f"{{{{ {v}.base }}}}"}),
        _when(f"{{{{ {v}.since is not none }}}}", [
            _set("input_datetime.set_datetime", since_id(r, u),
                 {"timestamp": f"{{{{ {v}.since }}}}"})]),
        _when(f"{{{{ {v}.grace is not none }}}}", [
            _set("input_datetime.set_datetime", grace_until_id(r, u),
                 {"timestamp": f"{{{{ {v}.grace }}}}"})]),
        _when(f"{{{{ {v}.unb }}}}", [_set("input_boolean.turn_on", unblocked_id(r, u))],
              [_set("input_boolean.turn_off", unblocked_id(r, u))]),
    ])


MODE_WAIT = "00:00:30"     # how long a command waits for the unit to report its new mode


def _send_command(unit: dict) -> dict:
    """Send a unit's command, when it has one: the mode, then the setpoint
    if the command sets one, then the fan. A unit reports its new mode a
    moment later, and offers fan modes only in some modes (none in dry or
    off), so the fan waits for the new mode (up to MODE_WAIT, carrying on if
    it never comes) before its guard is read; a fan call the mode rejects is
    skipped, as in v1. Every command path - scene, maintenance, leak release
    - sends through here."""
    e, c = unit["entity_id"], f"{_unit_var(unit)}.cmd"
    return _when(f"{{{{ {c} is not none }}}}", [
        _set("climate.set_hvac_mode", e, {"hvac_mode": f"{{{{ {c}.hvac_mode }}}}"}),
        _when(f"{{{{ {c}.temperature is not none }}}}", [
            _set("climate.set_temperature", e, {"temperature": f"{{{{ {c}.temperature }}}}"})]),
        _when(f"{{{{ {c}.fan_mode is not none }}}}", [
            {"wait_template": f"{{{{ states('{e}') == {c}.hvac_mode }}}}",
             "timeout": MODE_WAIT, "continue_on_timeout": True},
            _when(f"{{{{ {c}.fan_mode in (state_attr('{e}', 'fan_modes') or []) }}}}", [
                _set("climate.set_fan_mode", e, {"fan_mode": f"{{{{ {c}.fan_mode }}}}"})]),
        ]),
    ])


def _hold_off(room: dict) -> str:
    return f"{{{{ states('{hold_id(room['id'])}') == 'off' }}}}"


# ---------------------------------------------------------------------
# Scene automation (crossover) - one per room per section, fired by the
# container via automation.trigger at the boundary: on_section_change for
# every unit. While ac_hold is on it writes the helpers but sends nothing.
# ---------------------------------------------------------------------

def build_scene_automation(config: dict, room: dict, section: dict) -> dict:
    r = room["id"]
    sid = section["id"]
    cfg = control.ControlConfig.from_config(config)
    scenes = room.get("scenes", {})
    transition = int(scenes.get(sid, {}).get("transition_seconds", 0) or 0)

    variables: dict[str, str] = {"now_ts": NOW_TS, "blocked": _blocked_template(room)}
    for unit in room["units"]:
        forced = "true" if sid in _forced_off_sections(room, unit) else "false"
        variables[_unit_var(unit)] = (
            _read_unit(room, unit, _sa_literal(room, sid, unit), _leak_on(room, unit))
            + _section_change(cfg, forced) + _RESULT)

    actions: list[dict] = [
        _set("input_boolean.turn_on", guard_id(r)),
        _set("input_select.select_option", scene_select_id(r), {"option": sid}),
    ]
    actions += [_write_unit(room, unit) for unit in room["units"]]
    actions.append(_when(_hold_off(room), [_send_command(unit) for unit in room["units"]]))
    if transition > 0:
        actions.append({"delay": {"seconds": transition}})
    actions.append(_set("input_boolean.turn_off", guard_id(r)))

    return {
        "id": scene_automation_id(r, sid),
        "alias": f"AC {room.get('name', r)} — {section.get('name', sid)}",
        "mode": "restart",
        "trigger": [],          # fired by the container via automation.trigger
        "condition": [],        # skip_condition defaults true for automation.trigger
        "variables": variables,
        "action": actions,
    }


# ---------------------------------------------------------------------
# Leak automations - the latch (docs/DESIGN.md D6): a trip puts the unit
# into leak and turns it off at once (dry would still run the compressor);
# a confirmed release treats it as at a section change. The trip ignores ac_hold (safety); the release, like a scene,
# writes the helpers but sends nothing while ac_hold is on.
# ---------------------------------------------------------------------

def build_leak_automation(config: dict, room: dict, unit: dict) -> dict:
    """Raises the leak boolean AND puts the unit into leak, turned off,
    under the guard - the boolean alone drives nothing. When the unit's
    leak_detection.sensor_entity_id is set, the trigger is wired directly to
    that binary_sensor going 'on'. Left unset, the trigger is empty for the
    user to wire a sensor into by hand in Home Assistant."""
    r, u = room["id"], unit["id"]
    cfg = control.ControlConfig.from_config(config)
    sensor = unit.get("leak_detection", {}).get("sensor_entity_id")
    trigger = [{"platform": "state", "entity_id": sensor, "to": "on"}] if sensor else []
    return {
        "id": leak_automation_id(r, u),
        "alias": f"AC {room.get('name', r)} — leak latch ({unit.get('name', u)})",
        "mode": "queued",
        "trigger": trigger,
        "condition": [],
        "variables": {
            **_live_section_variables(room), "now_ts": NOW_TS,
            _unit_var(unit): _read_unit(room, unit, _sa_live(unit), "true")
                             + _leak_trip(cfg) + _RESULT,
        },
        "action": [
            _set("input_boolean.turn_on", leak_id(r, u)),
            _when(f"{{{{ {_unit_var(unit)}.go }}}}", [
                _set("input_boolean.turn_on", guard_id(r)),
                _write_unit(room, unit),
                _send_command(unit),
                _set("input_boolean.turn_off", guard_id(r)),
            ]),
        ],
    }


def build_leak_release_automation(config: dict, room: dict, unit: dict) -> dict:
    """Latch release: fires when the user confirms the fix, and treats a
    unit in leak as at a section change straight away rather than leaving
    it off until the next scheduled crossover. When the sensor is known,
    a condition also requires it to no longer read 'on' - confirmation
    alone is not enough while the sensor still reports wet."""
    r, u = room["id"], unit["id"]
    cfg = control.ControlConfig.from_config(config)
    sensor = unit.get("leak_detection", {}).get("sensor_entity_id")
    condition = []
    if sensor:
        # "not currently reading on" - expressed via the not: wrapper since a
        # plain condition entry has no native != operator.
        condition = [{"condition": "not", "conditions":
                     [{"condition": "state", "entity_id": sensor, "state": "on"}]}]
    return {
        "id": f"{leak_automation_id(r, u)}_release",
        "alias": f"AC {room.get('name', r)} — leak release ({unit.get('name', u)})",
        "mode": "single",
        "trigger": [{"platform": "state", "entity_id": leak_confirm_id(r, u), "to": "on"}],
        "condition": condition,
        "variables": {
            **_live_section_variables(room), "now_ts": NOW_TS,
            "blocked": _blocked_template(room),
            # the latch is being released, so it reads as off
            _unit_var(unit): _read_unit(room, unit, _sa_live(unit), "false")
                             + "{%- if st == 'leak' -%}"
                             + _section_change(cfg, _forced_off_live(room, unit))
                             + "{%- endif -%}" + _RESULT,
        },
        "action": [
            _set("input_boolean.turn_off", leak_id(r, u)),
            _set("input_boolean.turn_off", leak_confirm_id(r, u)),
            _when(f"{{{{ {_unit_var(unit)}.go }}}}", [
                _set("input_boolean.turn_on", guard_id(r)),
                _write_unit(room, unit),
                _when(_hold_off(room), [_send_command(unit)]),
                _set("input_boolean.turn_off", guard_id(r)),
            ]),
        ],
    }


# ---------------------------------------------------------------------
# Watchdog - clear a guard boolean stuck on too long, since a stuck guard
# silently disables both maintenance and observation. One per room.
# ---------------------------------------------------------------------

def build_watchdog_automation(config: dict, room: dict) -> dict:
    r = room["id"]
    return {
        "id": watchdog_automation_id(r),
        "alias": f"AC {room.get('name', r)} — guard watchdog",
        "mode": "single",
        "trigger": [{"platform": "state", "entity_id": guard_id(r),
                     "to": "on", "for": {"minutes": 5}}],
        "condition": [],
        "action": [_set("input_boolean.turn_off", guard_id(r))],
    }


# ---------------------------------------------------------------------
# Maintenance automation (D4: HA owns maintenance, on its own clock, so
# control survives container downtime). Every heartbeat: the pooled
# quorum (evaluate_votes), then step for each unit. Only a transition
# writes helpers and sends a command, under one raised guard.
# ---------------------------------------------------------------------

def _fresh(entity_id: str, reading: str, stale_minutes: int) -> str:
    """`reading`, or none once Home Assistant last reported the entity more
    than stale_minutes ago (control.is_stale): a sensor put away can keep
    its last value for days without going unavailable."""
    domain, obj = _split(entity_id)
    return (f"({reading} if states.{domain}.{obj} is not none and "
            f"(now() - states.{domain}.{obj}.last_reported).total_seconds() <= "
            f"{stale_minutes * 60} else none)")


def _sensor_reading_expr(sensor: dict, room: dict,
                         stale_minutes: int = control.DEFAULT_STALE_MINUTES) -> str:
    """The Jinja fragment that yields this sensor's current reading (a
    float or none) inside the votes template. Physical sensor: reads its
    own entity state. Virtual (source-based): reads the referenced unit's
    attribute. Either is none once its entity is stale. The rest of the
    vote logic is identical - only where the number comes from changes."""
    source = sensor.get("source")
    if source is None:
        return _fresh(sensor["entity_id"], f"states('{sensor['entity_id']}') | float(none)",
                      stale_minutes)
    if source.get("type") == "unit_attribute":
        uid = source.get("unit_id")
        unit = next((u for u in room["units"] if u["id"] == uid), None)
        if unit is None:
            return "none"
        attr = source.get("attribute", "current_temperature")
        return _fresh(unit["entity_id"],
                      f"state_attr('{unit['entity_id']}', '{attr}') | float(none)",
                      stale_minutes)
    return "none"


def _votes_expr(room: dict, stale_minutes: int = control.DEFAULT_STALE_MINUTES) -> str:
    """control.evaluate_votes as one Jinja expression -> {met, tally,
    required, direction, warm_side, cold_side}, from `sensor_data` and the
    `cold_edge` / `warm_edge` variables. A sensor's position is (reading -
    comfort) / band; it votes past either edge. One pooled count against the
    quorum; the summed trust of each side picks the direction (too_warm /
    too_cold / none), in the room's sensor order, as app.trust sums it."""
    entries = ", ".join(
        f"{{'reading': {_sensor_reading_expr(s, room, stale_minutes)}, "
        f"'e': sensor_data.get('{s['id']}') or {{}} }}" for s in room["sensors"])
    return _fill(
        "{%- set sensors = [<<ENTRIES>>] -%}"
        "{%- set ns = namespace(n=0, tally=0, warm=0.0, cold=0.0, ws=0, cs=0) -%}"
        "{%- for s in sensors -%}"
        "{%- set comfort = s.e.get('comfort') -%}"
        "{%- if s.reading is not none and comfort is not none -%}"
        "{%- set p = (s.reading - comfort) / (s.e.get('band') or <<BAND>>) -%}"
        "{%- set ns.n = ns.n + 1 -%}"
        "{%- if p > 0 -%}{%- set ns.ws = ns.ws + 1 -%}"
        "{%- elif p < 0 -%}{%- set ns.cs = ns.cs + 1 -%}{%- endif -%}"
        "{%- if p <= cold_edge or p >= warm_edge -%}"
        "{%- set ns.tally = ns.tally + 1 -%}"
        "{%- set w = (s.e.get('trust') or 0.0) + 0.000001 -%}"
        "{%- if p > 0 -%}{%- set ns.warm = ns.warm + w -%}"
        "{%- elif p < 0 -%}{%- set ns.cold = ns.cold + w -%}{%- endif -%}"
        "{%- endif -%}"
        "{%- endif -%}"
        "{%- endfor -%}"
        "{%- set n = ns.n -%}"
        "{%- set required = 1 if n == 1 else (2 if n == 2 else ((n + 1) // 2 if n >= 3 else 0)) -%}"
        "{%- set met = required > 0 and ns.tally >= required -%}"
        "{%- set direction = ('too_warm' if ns.warm > ns.cold else ('too_cold' "
        "if ns.cold > ns.warm else 'none')) if met else 'none' -%}"
        "{{ {'met': met, 'tally': ns.tally, 'required': required, 'direction': direction, "
        "'warm_side': ns.ws, 'cold_side': ns.cs} }}",
        ENTRIES=entries, BAND=repr(control.DEFAULT_BAND))


def build_maintenance_automation(config: dict, room: dict) -> dict:
    r = room["id"]
    interval = config.get("system", {}).get("heartbeat_interval_minutes", 10)

    variables = {
        **_live_section_variables(room),
        "sensor_data": "{{ sect.get('sensors') or {} }}",
        "now_ts": NOW_TS,
        "blocked": _blocked_template(room),
        # phase 5 leans these per sensor from the conditions entity
        "cold_edge": "{{ -1.0 }}",
        "warm_edge": "{{ 1.0 }}",
        "votes": _votes_expr(room, config.get("system", {}).get(
            "sensor_stale_minutes", control.DEFAULT_STALE_MINUTES)),
    }
    for unit in room["units"]:
        variables[_unit_var(unit)] = (
            _read_unit(room, unit, _sa_live(unit), _leak_on(room, unit))
            + _step(config, room, unit) + _RESULT)

    any_go = " or ".join(f"{_unit_var(u)}.go" for u in room["units"])
    return {
        "id": maintenance_automation_id(r),
        "alias": f"AC {room.get('name', r)} — maintenance",
        "mode": "single",
        "trigger": [{"platform": "time_pattern", "minutes": f"/{interval}"}],
        "condition": [
            {"condition": "state", "entity_id": guard_id(r), "state": "off"},
            {"condition": "state", "entity_id": hold_id(r), "state": "off"},
        ],
        "variables": variables,
        "action": [_when(f"{{{{ {any_go} }}}}", [
            _set("input_boolean.turn_on", guard_id(r)),
            *[_write_unit(room, unit) for unit in room["units"]],
            *[_send_command(unit) for unit in room["units"]],
            _set("input_boolean.turn_off", guard_id(r)),
        ])],
    }


# ---------------------------------------------------------------------
# Manual-change automation - one per unit. A person's change to the unit
# (no parent context, guard off, the unit reporting, and not our own
# command showing up late) is applied to its helpers straight away, so it
# holds while the container is down. No command: the person already moved
# the unit. The container records the same change as a reaction.
# ---------------------------------------------------------------------

def _climate_snapshot(state: str) -> str:
    return _fill("{%- set s = <<S>> -%}"
                 "{{ {'mode': s.state if s else none, "
                 "'temperature': s.attributes.get('temperature') if s else none, "
                 "'fan': s.attributes.get('fan_mode') if s else none} }}", S=state)


def build_manual_change_automation(config: dict, room: dict, unit: dict) -> dict:
    r, u, v = room["id"], unit["id"], _unit_var(unit)
    return {
        "id": manual_automation_id(r, u),
        "alias": f"AC {room.get('name', r)} — manual change ({unit.get('name', u)})",
        "mode": "queued",
        "max": 10,
        # Mode changes (to: null fires on state changes only), and the two
        # attributes a person changes: current_temperature updates don't fire
        # it. A change that moves the mode and an attribute fires twice; the
        # second run sees the first's helper writes, so it either finds the
        # change is now "ours" or writes the same record again.
        "trigger": [
            {"platform": "state", "entity_id": unit["entity_id"], "to": None},
            {"platform": "state", "entity_id": unit["entity_id"], "attribute": "temperature"},
            {"platform": "state", "entity_id": unit["entity_id"], "attribute": "fan_mode"},
        ],
        "variables": {
            **_live_section_variables(room), "now_ts": NOW_TS,
            "before": _climate_snapshot("trigger.from_state"),
            "after": _climate_snapshot("trigger.to_state"),
            v: _read_unit(room, unit, _sa_live(unit), _leak_on(room, unit))
               + _climate_change(config)
               + "{{ {'go': r.go, 'state': r.state, 'offset': r.offset, 'base': r.base, "
                 "'since': r.since, 'grace': r.grace, 'unb': r.unb, 'cmd': none, "
                 "'ours': ours, 'kind': kind} }}",
        },
        "condition": [
            _template("{{ trigger.to_state is not none and "
                      "trigger.to_state.context.parent_id is none }}"),
            {"condition": "state", "entity_id": guard_id(r), "state": "off"},
            _template("{{ before.mode not in [none, 'unavailable', 'unknown'] and "
                      "after.mode not in [none, 'unavailable', 'unknown'] }}"),
            _template(f"{{{{ {v}.kind is not none and not {v}.ours }}}}"),
        ],
        "action": [_write_unit(room, unit)],
    }


# ---------------------------------------------------------------------
# Assemble everything deploy needs.
# ---------------------------------------------------------------------

def render_all(config: dict) -> dict:
    """Return {"helpers": [...], "automations": [...]} for the whole config."""
    automations: list[dict] = []
    for room in _enabled_rooms(config):
        for section in _sections_for(config, room):
            automations.append(build_scene_automation(config, room, section))
        automations.append(build_maintenance_automation(config, room))
        for unit in room["units"]:
            automations.append(build_manual_change_automation(config, room, unit))
        for unit in _leak_units(room):
            automations.append(build_leak_automation(config, room, unit))
            automations.append(build_leak_release_automation(config, room, unit))
        automations.append(build_watchdog_automation(config, room))
    return {"helpers": build_helpers(config), "automations": automations}
