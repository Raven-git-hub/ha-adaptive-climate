"""
Adaptive Climate - helper and automation generator.

Emits the Home Assistant helpers and automations from the config. Nothing
here bakes in a learned value: the generator emits templates that read the
almanac at runtime, so changing a band or a mode needs only an almanac
republish - regeneration is required only when *entities* change.

Generation uses PyYAML over Python data structures, never a template
engine (rendering Jinja to produce Jinja consumes the inner pass). Output
is deterministic, so regeneration is a safe overwrite.

STATUS: skeleton. The naming block below is final and load-bearing; the
build_* functions are stubbed pending the Phase 5 decision on where the
quorum/maintenance loop runs (see docs/DESIGN.md D4).
"""

from __future__ import annotations

import io
from typing import Any

import yaml

SECTIONS = ("sunrise", "day", "afternoon", "sunset", "night", "sleep")


# ---------------------------------------------------------------------
# YAML serialisation
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
#
# The 'ac_' prefix (not Light's 'al_') lets both systems coexist on one
# host. Every id derives from the room's stable id, never its display
# name, so the object id HA derives always matches what the automations
# reference.
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


# ---------------------------------------------------------------------
# Section resolution
# ---------------------------------------------------------------------

def _sections_for(config: dict, room: dict) -> list[dict]:
    profiles = {p["id"]: p for p in config.get("schedule_profiles", [])}
    profile = profiles.get(room.get("schedule_profile", "default")) \
              or profiles.get("default") or config["schedule_profiles"][0]
    by_id = {s["id"]: s for s in profile["sections"]}
    return [by_id[s] for s in SECTIONS]


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def build_helpers(config: dict) -> dict[str, str]:
    """Return {path: yaml} for input_boolean / input_select packages.

    Per room: ac_active, ac_hold, ac_scene. Per leak-enabled unit:
    ac_leak_<room>_<unit> and ac_leak_confirmed_<room>_<unit>.

    TODO(Phase 5): implement. Mirror Light's build_helpers, add the leak
    latch pair for units with leak_detection.enabled.
    """
    raise NotImplementedError("Phase 5")


# ---------------------------------------------------------------------
# Scene automations (crossover)
# ---------------------------------------------------------------------

def build_scene_automation(config: dict, room: dict, section: dict) -> dict:
    """One automation per room per section, fired by the container via
    automation.trigger.

    On crossover it: raises the guard, clears the hold, sets the scene
    select, reads the almanac, and for each unit either forces it off
    (baked-in override) or drives it to the learned setpoint in the
    Normal state (fan LOW, mode COOL, setpoint = almanac). Emits
    conditions: [] because automation.trigger defaults to
    skip_condition: true.

    TODO(Phase 5): implement, driving the climate entity via
    climate.set_temperature / set_hvac_mode / set_fan_mode.
    """
    raise NotImplementedError("Phase 5")


# ---------------------------------------------------------------------
# Maintenance / quorum automation
# ---------------------------------------------------------------------

def build_maintenance_automation(config: dict, room: dict) -> dict:
    """Runs on HA's own clock so correction continues if the container is
    down. Evaluates the per-sensor quorum against the almanac's comfort
    and band values, and when met, drives units into Cooling/Warming and
    nudges the setpoint by at most max_step_degrees, honouring the guard
    and hold booleans.

    OPEN DECISION (docs/DESIGN.md D4): whether the quorum tally + the
    "stop below quorum or after an hour" latch live here in templates or
    in the container. This stub assumes HA. Do not implement until that
    is settled.

    TODO(Phase 5).
    """
    raise NotImplementedError("Phase 5 - pending D4")


# ---------------------------------------------------------------------
# Leak automation (stub the user wires their own sensor into)
# ---------------------------------------------------------------------

def build_leak_automation(config: dict, room: dict, unit: dict) -> dict:
    """A stub automation whose trigger the user fills with their own leak
    sensor. It raises ac_leak_<room>_<unit>; the maintenance automation
    reads that boolean to drive Leak mode (DRY). Release is latched on
    (no leak) AND (ac_leak_confirmed_<room>_<unit>).

    TODO(Phase 5).
    """
    raise NotImplementedError("Phase 5")


# ---------------------------------------------------------------------
# Watchdogs
# ---------------------------------------------------------------------

def build_watchdogs(config: dict) -> list[dict]:
    """A stuck guard silently disables both maintenance and observation,
    so a watchdog clears one left on too long. Mirror Light.

    TODO(Phase 5).
    """
    raise NotImplementedError("Phase 5")
