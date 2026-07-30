"""
Adaptive Climate - helper and automation generator.

Pure and offline: config in, helper specs + automation configs out. No HA
connection, no learned state baked in. The emitted automations read the
almanac (sensor.ac_almanac_<room>) and the live entity attributes at
runtime, so changing a band, a setpoint or a mode needs only an almanac
republish - regeneration is required only when *entities* change. Output
is deterministic, so regeneration is a safe overwrite.

Everything emitted is namespaced `ac_*`, derived from the room's stable
id. That naming is what lets deploy (Phase 11) own our artifacts
completely - create, update, prune our own - while never touching a
foreign automation. Conflict handling lives in deploy, not here: this
module never looks at what is already in HA.

Per-unit hardware differences (fan availability, temperature step, min/max
range - see docs/HARDWARE.md) are handled inside the generated templates by
reading the unit's own attributes at runtime, so one generated automation
adapts to each unit and to the dry-mode-hides-fan quirk.

STATUS: helpers, scene (crossover), leak and watchdog generation are
implemented and render-tested (tools/render_generated.py). The
maintenance/quorum template is the next Phase 5 sub-step (see
build_maintenance_automation).
"""

from __future__ import annotations

import io
from typing import Any

import yaml

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

def scene_automation_id(room: str, section: str) -> str:
    return f"ac_scene_{room}_{section}"

def maintenance_automation_id(room: str) -> str:
    return f"ac_maintenance_{room}"

def leak_automation_id(room: str, unit: str) -> str:
    return f"ac_leak_{room}_{unit}"

def watchdog_automation_id(room: str) -> str:
    return f"ac_watchdog_{room}"

def correction_started_id(room: str) -> str:
    return f"input_datetime.ac_correction_started_{room}"


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


# ---------------------------------------------------------------------
# Helpers  ->  list of {domain, object_id, name, ...} specs
# Deploy creates each over the WebSocket API (input_boolean/input_select).
# name == object_id so it slugifies back to itself (docs/DESIGN.md 13).
# ---------------------------------------------------------------------

def build_helpers(config: dict) -> list[dict]:
    specs: list[dict] = []

    def boolean(entity_id: str) -> dict:
        domain, obj = _split(entity_id)
        return {"domain": domain, "object_id": obj, "name": obj}

    for room in _enabled_rooms(config):
        r = room["id"]
        specs.append(boolean(guard_id(r)))
        specs.append(boolean(hold_id(r)))
        domain, obj = _split(scene_select_id(r))
        specs.append({"domain": domain, "object_id": obj, "name": obj,
                      "options": list(SECTIONS)})
        domain, obj = _split(correction_started_id(r))
        specs.append({"domain": domain, "object_id": obj, "name": obj,
                      "has_date": True, "has_time": True})
        for unit in _leak_units(room):
            specs.append(boolean(leak_id(r, unit["id"])))
            specs.append(boolean(leak_confirm_id(r, unit["id"])))
    return specs


# ---------------------------------------------------------------------
# Templated per-unit drive (Normal state), reading almanac + live attrs.
# Sentinel tokens keep the Jinja braces clear of Python formatting.
# ---------------------------------------------------------------------

def _unit_lookup(alm: str, section: str, unit_id: str) -> str:
    return ("{% set secs = state_attr('<<ALM>>','sections') or {} %}"
            "{% set u = ((secs.get('<<S>>') or {}).get('units') or {}).get('<<U>>') or {} %}"
            ).replace("<<ALM>>", alm).replace("<<S>>", section).replace("<<U>>", unit_id)


def _skip_condition(alm: str, section: str, unit_id: str) -> str:
    """True when there is nothing safe to drive the unit to: either the
    almanac's off flag is set (reserved; the analyser never sets this
    today - scene-level off is handled separately, statically, below) or
    no setpoint has been learned yet. In BOTH cases the correct action is
    to leave the unit alone, not command it off - see _drive_unit."""
    return (_unit_lookup(alm, section, unit_id)
            + "{{ (u.get('off')) or (u.get('setpoint') is none) }}")


def _setpoint_template(alm: str, section: str, unit_id: str, entity: str) -> str:
    return (_unit_lookup(alm, section, unit_id) +
            "{% set sp = u.get('setpoint') %}"
            "{% set step = state_attr('<<E>>','target_temp_step') or 1 %}"
            "{% set lo = state_attr('<<E>>','min_temp') or 16 %}"
            "{% set hi = state_attr('<<E>>','max_temp') or 30 %}"
            "{% set snapped = ((sp / step) | round(0)) * step %}"
            "{{ [[snapped, lo] | max, hi] | min }}"
            ).replace("<<E>>", entity)


def _fan_supported_template(entity: str, mode: str) -> str:
    return f"{{{{ '{mode}' in (state_attr('{entity}','fan_modes') or []) }}}}"


def _leak_active_template(room: str, unit_id: str) -> str:
    return f"{{{{ states('{leak_id(room, unit_id)}') == 'on' }}}}"


def _live_section_variables(room: str) -> dict:
    """variables: block computing the currently active section and its
    almanac unit data from live HA state (input_select + the almanac
    sensor), for automations that are triggered by an event rather than a
    scheduled crossover and so don't know the section at generation time -
    the same live-lookup pattern build_maintenance_automation uses."""
    alm = almanac_id(room)
    return {
        "section": f"{{{{ states('{scene_select_id(room)}') }}}}",
        "secs": f"{{{{ state_attr('{alm}','sections') or {{}} }}}}",
        "sect": "{{ secs.get(section) or {} }}",
        "unit_data": "{{ sect.get('units') or {} }}",
    }


def _drive_mode_actions_live(unit: dict, hvac_mode: str, offset: float, fan_mode: str) -> list:
    """Drive one unit into hvac_mode using the LIVE `unit_data` variable
    (from _live_section_variables) rather than a literal section - for
    automations fired by an event (leak trip / leak release), not a
    crossover. Skips the temperature call if no setpoint is known yet
    rather than inventing one, but still switches hvac_mode/fan so the
    unit visibly changes state (e.g. leaves DRY) even before an almanac
    exists - unlike the crossover's "no almanac = untouched" safety rule,
    a unit already mid safety-override needs a definite way out."""
    e, uid = unit["entity_id"], unit["id"]
    return [
        _set("climate.set_hvac_mode", e, {"hvac_mode": hvac_mode}),
        {"choose": [{
            "conditions": [{"condition": "template", "value_template":
                            "{{ (unit_data.get('" + uid + "') or {}).get('setpoint') is not none }}"}],
            "sequence": [_set("climate.set_temperature", e,
                              {"temperature": _correction_setpoint_template(uid, e, offset)})],
        }], "default": []},
        {"choose": [{
            "conditions": [{"condition": "template",
                            "value_template": _fan_supported_template(e, fan_mode)}],
            "sequence": [_set("climate.set_fan_mode", e, {"fan_mode": fan_mode})],
        }], "default": []},
    ]


def _set(service: str, entity: str, data: dict | None = None) -> dict:
    step: dict = {"service": service, "target": {"entity_id": entity}}
    if data:
        step["data"] = data
    return step


def _drive_dry_actions_literal(alm: str, section: str, unit: dict) -> list:
    """Same as _drive_mode_actions_live but for the crossover automations,
    which know the section literally at generation time - reuses the
    literal-section setpoint lookup rather than the live `unit_data`
    variable."""
    e, uid = unit["entity_id"], unit["id"]
    return [
        _set("climate.set_hvac_mode", e, {"hvac_mode": "dry"}),
        {"choose": [{
            "conditions": [{"condition": "template",
                            "value_template": _unit_lookup(alm, section, uid)
                                              + "{{ u.get('setpoint') is not none }}"}],
            "sequence": [_set("climate.set_temperature", e,
                              {"temperature": _setpoint_template(alm, section, uid, e)})],
        }], "default": []},
        {"choose": [{
            "conditions": [{"condition": "template",
                            "value_template": _fan_supported_template(e, "low")}],
            "sequence": [_set("climate.set_fan_mode", e, {"fan_mode": "low"})],
        }], "default": []},
    ]


def _drive_unit(alm: str, section: str, unit: dict, forced_off: bool, room_id: str) -> dict:
    """One action step: force off (deliberate, static, from config scenes),
    or drive to the Normal state (cool, snapped+clamped almanac setpoint,
    fan low if supported) - or, if no almanac has been learned for this
    section yet, do NOTHING and leave the unit exactly as it is.

    SAFETY: "no almanac yet" must never be treated as "turn off". Before
    the runtime exists to seed a provisional almanac (Phase 10), every
    section starts with no learned setpoint; if that were mapped to an
    off command, deploying would force every unit off at every crossover
    on a fresh install. forced_off (below) is the ONLY path that commands
    off from missing data, and it fires solely from the user's own scene
    configuration.

    LEAK SAFETY: if this unit has leak detection enabled, an active leak
    (input_boolean.ac_leak_<room>_<unit> is 'on') takes priority over
    EVERYTHING below, including an explicit forced-off scene - a crossover
    landing while a leak is active must re-assert DRY, not silently revert
    to Normal or Off. See docs/DESIGN.md D6.
    """
    e = unit["entity_id"]
    if forced_off:
        base = _set("climate.set_hvac_mode", e, {"hvac_mode": "off"})
    else:
        base = {
            "choose": [{
                "conditions": [{"condition": "template",
                                "value_template": _skip_condition(alm, section, unit["id"])}],
                "sequence": [],   # no almanac yet -> leave the unit alone, do not touch it
            }],
            "default": [
                _set("climate.set_hvac_mode", e, {"hvac_mode": "cool"}),
                _set("climate.set_temperature", e,
                     {"temperature": _setpoint_template(alm, section, unit["id"], e)}),
                {"choose": [{
                    "conditions": [{"condition": "template",
                                    "value_template": _fan_supported_template(e, "low")}],
                    "sequence": [_set("climate.set_fan_mode", e, {"fan_mode": "low"})],
                }], "default": []},
            ],
        }

    if not unit.get("leak_detection", {}).get("enabled"):
        return base

    return {
        "choose": [{
            "conditions": [{"condition": "template",
                            "value_template": _leak_active_template(room_id, unit["id"])}],
            "sequence": _drive_dry_actions_literal(alm, section, unit),
        }],
        "default": [base],
    }


# ---------------------------------------------------------------------
# Scene automation (crossover) - one per room per section.
# Fired by the container via automation.trigger at the boundary.
# ---------------------------------------------------------------------

def build_scene_automation(config: dict, room: dict, section: dict) -> dict:
    r = room["id"]
    sid = section["id"]
    alm = almanac_id(r)
    scenes = room.get("scenes", {})
    unit_modes = (scenes.get(sid, {}).get("units", {}))
    transition = int(scenes.get(sid, {}).get("transition_seconds", 0) or 0)

    actions: list[dict] = [
        _set("input_boolean.turn_on", guard_id(r)),
        _set("input_select.select_option", scene_select_id(r), {"option": sid}),
        _set("input_boolean.turn_off", hold_id(r)),
    ]
    for unit in room["units"]:
        forced_off = unit_modes.get(unit["id"], {}).get("mode", "auto") == "off"
        actions.append(_drive_unit(alm, sid, unit, forced_off, r))
    if transition > 0:
        actions.append({"delay": {"seconds": transition}})
    actions.append(_set("input_boolean.turn_off", guard_id(r)))

    return {
        "id": scene_automation_id(r, sid),
        "alias": f"AC {room.get('name', r)} \u2014 {section.get('name', sid)}",
        "mode": "restart",
        "trigger": [],          # fired by the container via automation.trigger
        "condition": [],        # skip_condition defaults true for automation.trigger
        "action": actions,
    }


# ---------------------------------------------------------------------
# Leak automation - a stub the user wires their own leak sensor into,
# plus the confirm-to-release side of the latch (docs/DESIGN.md D6).
# ---------------------------------------------------------------------

def build_leak_automation(config: dict, room: dict, unit: dict) -> dict:
    """Raises the leak boolean AND immediately drives the unit into Leak
    state (DRY, fan LOW if supported, almanac setpoint if known) - the
    boolean alone drives nothing on its own; this is the actual safety
    response. When the unit's leak_detection.sensor_entity_id is set
    (chosen from the Config page's live picker), the trigger is wired
    directly to that binary_sensor going 'on'. Left unset, the trigger is
    empty for the user to wire a sensor into by hand in Home Assistant -
    the manual-wiring path stays fully supported, and drives the same
    response once fired. See docs/DESIGN.md D6."""
    r, u = room["id"], unit["id"]
    sensor = unit.get("leak_detection", {}).get("sensor_entity_id")
    trigger = [{"platform": "state", "entity_id": sensor, "to": "on"}] if sensor else []
    return {
        "id": leak_automation_id(r, u),
        "alias": f"AC {room.get('name', r)} \u2014 leak latch ({unit.get('name', u)})",
        "mode": "queued",
        "trigger": trigger,
        "condition": [],
        "variables": _live_section_variables(r),
        "action": [_set("input_boolean.turn_on", leak_id(r, u))]
                  + _drive_mode_actions_live(unit, "dry", 0, "low"),
    }


def build_leak_release_automation(config: dict, room: dict, unit: dict) -> dict:
    """Latch release: fires when the user confirms the fix, and immediately
    restores the unit to Normal (cool, fan LOW, almanac setpoint if known)
    rather than leaving it in DRY until the next scheduled crossover, which
    could be hours away. When the sensor is known, a condition also
    requires it to no longer read 'on' - confirmation alone is not enough
    while the sensor still reports wet. Without a known sensor, release
    depends on confirmation alone, as before (the user is expected to have
    checked)."""
    r, u = room["id"], unit["id"]
    sensor = unit.get("leak_detection", {}).get("sensor_entity_id")
    condition = []
    if sensor:
        # "not currently reading on" - expressed via the not: wrapper since a
        # plain condition entry has no native != operator.
        condition = [{"condition": "not", "conditions":
                     [{"condition": "state", "entity_id": sensor, "state": "on"}]}]
    return {
        "id": f"{leak_automation_id(r, u)}_release",
        "alias": f"AC {room.get('name', r)} \u2014 leak release ({unit.get('name', u)})",
        "mode": "single",
        "trigger": [{"platform": "state", "entity_id": leak_confirm_id(r, u), "to": "on"}],
        "condition": condition,
        "variables": _live_section_variables(r),
        "action": [
            _set("input_boolean.turn_off", leak_id(r, u)),
            _set("input_boolean.turn_off", leak_confirm_id(r, u)),
        ] + _drive_mode_actions_live(unit, "cool", 0, "low"),
    }


# ---------------------------------------------------------------------
# Watchdog - clear a guard boolean stuck on too long, since a stuck guard
# silently disables both maintenance and observation. One per room.
# ---------------------------------------------------------------------

def build_watchdog_automation(config: dict, room: dict) -> dict:
    r = room["id"]
    return {
        "id": watchdog_automation_id(r),
        "alias": f"AC {room.get('name', r)} \u2014 guard watchdog",
        "mode": "single",
        "trigger": [{"platform": "state", "entity_id": guard_id(r),
                     "to": "on", "for": {"minutes": 5}}],
        "condition": [],
        "action": [_set("input_boolean.turn_off", guard_id(r))],
    }


# ---------------------------------------------------------------------
# Maintenance / quorum automation - NEXT Phase 5 sub-step.
# ---------------------------------------------------------------------

# ---------------------------------------------------------------------
# Maintenance / quorum automation (D4: HA owns maintenance, runs on HA's
# own clock so correction survives container downtime).
#
# Design, matching docs/TRUST_MODEL.md exactly:
#   * a sensor votes when |reading - comfort| >= band; unavailable or
#     never-learned sensors are excluded from both the tally and the
#     quorum base (n = usable sensor count, not configured count)
#   * quorum: n==1->1, n==2->2, n>=3->ceil(n/2)  ==(n+1)//2 for n>=3
#   * direction: trust-weighted sum of the breaching sensors' sign
#   * a unit is corrected only if it is already on, has a learned
#     setpoint, is not forced off for the section, and its leak boolean
#     (if any) is not on - leak mode owns the unit instead
#   * the one-hour cap uses input_datetime.ac_correction_started_<room>:
#     reset to now() every heartbeat the quorum is NOT met (idle clock);
#     left untouched while it IS met, so elapsed-since-idle approximates
#     how long correction has been continuously warranted. Once elapsed
#     passes correction_max_minutes, correction pauses until a heartbeat
#     drops back below quorum and resets the clock.
# ---------------------------------------------------------------------

def _sensor_reading_expr(sensor: dict, room: dict) -> str:
    """The Jinja fragment that yields this sensor's current reading (a
    float or none) inside the maintenance vote template. Physical sensor:
    reads its own entity state. Virtual (source-based): reads the
    referenced unit's attribute. The rest of the vote/quorum logic is
    identical - only where the number comes from changes."""
    source = sensor.get("source")
    if source is None:
        return f"states('{sensor['entity_id']}') | float(none)"
    if source.get("type") == "unit_attribute":
        uid = source.get("unit_id")
        unit = next((u for u in room["units"] if u["id"] == uid), None)
        if unit is None:
            return "none"
        attr = source.get("attribute", "current_temperature")
        return f"state_attr('{unit['entity_id']}', '{attr}') | float(none)"
    return "none"


def _sensor_entry_template(sensor: dict, room: dict, low_dev: float) -> str:
    sid = sensor["id"]
    reading = _sensor_reading_expr(sensor, room)
    return (
        "{'id': '<<ID>>', "
        "'reading': <<R>>, "
        "'comfort': (sensor_data.get('<<ID>>') or {}).get('comfort'), "
        "'band': (sensor_data.get('<<ID>>') or {}).get('band', <<LOW>>), "
        "'trust': (sensor_data.get('<<ID>>') or {}).get('trust', 0.0)}"
    ).replace("<<ID>>", sid).replace("<<R>>", reading).replace("<<LOW>>", repr(low_dev))


def _vote_expr(room: dict, low_dev: float) -> str:
    """A single Jinja expression -> dict literal {tally, required, met,
    direction}, computed from `sensor_data` (set earlier in variables).
    Trim markers keep the rendered string a clean Python literal so HA's
    variable parser (literal_eval) accepts it."""
    entries = ", ".join(_sensor_entry_template(s, room, low_dev) for s in room["sensors"])
    return (
        "{%- set sensors = [" + entries + "] -%}"
        "{%- set usable = sensors | selectattr('reading','ne',none) | selectattr('comfort','ne',none) | list -%}"
        "{%- set n = usable | length -%}"
        "{%- set required = 1 if n==1 else (2 if n==2 else ((n+1)//2 if n>=3 else 0)) -%}"
        "{%- set ns = namespace(tally=0, warm=0.0, cool=0.0) -%}"
        "{%- for s in usable -%}"
        "{%- set dev = s.reading - s.comfort -%}"
        "{%- if dev|abs >= s.band -%}"
        "{%- set ns.tally = ns.tally + 1 -%}"
        "{%- set w = s.trust + 0.000001 -%}"
        "{%- if dev > 0 -%}{%- set ns.warm = ns.warm + w -%}"
        "{%- else -%}{%- set ns.cool = ns.cool + w -%}{%- endif -%}"
        "{%- endif -%}"
        "{%- endfor -%}"
        "{%- set met = required > 0 and ns.tally >= required -%}"
        "{%- set direction = ('cool' if ns.warm > ns.cool else ('warm' if ns.cool > ns.warm else 'none')) "
        "if met else 'none' -%}"
        "{{ {'tally': ns.tally, 'required': required, 'met': met, 'direction': direction} }}"
    )


def _correction_setpoint_template(unit_id: str, entity: str, offset: float) -> str:
    return (
        "{% set u = unit_data.get('<<U>>') or {} %}"
        "{% set sp = u.get('setpoint') %}"
        "{% set step = state_attr('<<E>>','target_temp_step') or 1 %}"
        "{% set lo = state_attr('<<E>>','min_temp') or 16 %}"
        "{% set hi = state_attr('<<E>>','max_temp') or 30 %}"
        "{% set target = sp + (<<OFF>>) %}"
        "{% set snapped = ((target / step) | round(0)) * step %}"
        "{{ [[snapped, lo] | max, hi] | min }}"
    ).replace("<<U>>", unit_id).replace("<<E>>", entity).replace("<<OFF>>", repr(offset))


def _correction_gate_template(room: str, unit: dict) -> str:
    """True only when this unit should be corrected this heartbeat: a
    direction is active, the unit has a learned setpoint, is not forced
    off, is currently on, and (if leak-enabled) its leak boolean is off."""
    uid, entity = unit["id"], unit["entity_id"]
    leak_clause = ""
    if unit.get("leak_detection", {}).get("enabled"):
        leak_clause = f" and states('{leak_id(room, uid)}') != 'on'"
    return (
        "{{ vote.direction in ('warm','cool')"
        f" and (unit_data.get('{uid}') or {{}}).get('setpoint') is not none"
        f" and not (unit_data.get('{uid}') or {{}}).get('off', false)"
        f" and states('{entity}') not in ('off','unavailable','unknown')"
        f"{leak_clause} }}}}"
    )


def _drive_correction_unit(room: dict, unit: dict) -> dict:
    r, uid, entity = room["id"], unit["id"], unit["entity_id"]
    cool_seq = [
        _set("climate.set_hvac_mode", entity, {"hvac_mode": "cool"}),
        _set("climate.set_temperature", entity,
             {"temperature": _correction_setpoint_template(uid, entity, -2)}),
        {"choose": [{"conditions": [{"condition": "template",
                     "value_template": _fan_supported_template(entity, "medium")}],
                    "sequence": [_set("climate.set_fan_mode", entity, {"fan_mode": "medium"})]}],
         "default": []},
    ]
    warm_seq = [
        _set("climate.set_hvac_mode", entity, {"hvac_mode": "fan_only"}),
        _set("climate.set_temperature", entity,
             {"temperature": _correction_setpoint_template(uid, entity, 0)}),
        {"choose": [{"conditions": [{"condition": "template",
                     "value_template": _fan_supported_template(entity, "low")}],
                    "sequence": [_set("climate.set_fan_mode", entity, {"fan_mode": "low"})]}],
         "default": []},
    ]
    return {
        "choose": [{
            "conditions": [{"condition": "template",
                            "value_template": _correction_gate_template(r, unit)}],
            "sequence": [{
                "choose": [
                    {"conditions": [{"condition": "template",
                                     "value_template": "{{ vote.direction == 'cool' }}"}],
                     "sequence": cool_seq},
                    {"conditions": [{"condition": "template",
                                     "value_template": "{{ vote.direction == 'warm' }}"}],
                     "sequence": warm_seq},
                ],
                "default": [],
            }],
        }],
        "default": [],
    }


def build_maintenance_automation(config: dict, room: dict) -> dict:
    r = room["id"]
    alm = almanac_id(r)
    trust_cfg = config.get("system", {}).get("trust", {})
    low_dev = trust_cfg.get("low_trust_deviation", 5.0)
    interval = config.get("system", {}).get("heartbeat_interval_minutes", 10)
    max_minutes = config.get("system", {}).get("correction_max_minutes", 60)
    started = correction_started_id(r)

    variables = {
        "section": f"{{{{ states('{scene_select_id(r)}') }}}}",
        "secs": f"{{{{ state_attr('{alm}','sections') or {{}} }}}}",
        "sect": "{{ secs.get(section) or {} }}",
        "sensor_data": "{{ sect.get('sensors') or {} }}",
        "unit_data": "{{ sect.get('units') or {} }}",
        "vote": _vote_expr(room, low_dev),
        "elapsed_minutes": (
            f"{{% set st = states('{started}') %}}"
            "{% if st in ('unknown','unavailable',none) %}0"
            "{% else %}{{ ((now() - as_datetime(st)).total_seconds() / 60) | round(1) }}"
            "{% endif %}"
        ),
    }

    reset_clock = {
        "choose": [{
            "conditions": [{"condition": "template",
                            "value_template": "{{ not vote.met }}"}],
            "sequence": [_set("input_datetime.set_datetime", started,
                              {"datetime": "{{ now() }}"})],
        }],
        "default": [],
    }

    correct_if_within_cap = {
        "choose": [{
            "conditions": [
                {"condition": "template", "value_template": "{{ vote.met }}"},
                {"condition": "template",
                 "value_template": f"{{{{ elapsed_minutes < {max_minutes} }}}}"},
            ],
            "sequence": [_drive_correction_unit(room, u) for u in room["units"]],
        }],
        "default": [],
    }

    return {
        "id": maintenance_automation_id(r),
        "alias": f"AC {room.get('name', r)} \u2014 maintenance",
        "mode": "single",
        "trigger": [{"platform": "time_pattern", "minutes": f"/{interval}"}],
        "condition": [
            {"condition": "state", "entity_id": guard_id(r), "state": "off"},
            {"condition": "state", "entity_id": hold_id(r), "state": "off"},
        ],
        "variables": variables,
        "action": [reset_clock, correct_if_within_cap],
    }


# ---------------------------------------------------------------------
# Assemble everything deploy needs.
# ---------------------------------------------------------------------

def render_all(config: dict) -> dict:
    """Return {"helpers": [...], "automations": [...]} for the whole config.
    Maintenance automations are omitted until that sub-step lands."""
    automations: list[dict] = []
    for room in _enabled_rooms(config):
        for section in _sections_for(config, room):
            automations.append(build_scene_automation(config, room, section))
        automations.append(build_maintenance_automation(config, room))
        for unit in _leak_units(room):
            automations.append(build_leak_automation(config, room, unit))
            automations.append(build_leak_release_automation(config, room, unit))
        automations.append(build_watchdog_automation(config, room))
    return {"helpers": build_helpers(config), "automations": automations}
