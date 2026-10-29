"""
Adaptive Climate - control logic check (phase 3b): the generated Jinja
against app/control.py.

Replays every scenario in tools/control_check.py through the automations
app/generator.py actually emits, run the way Home Assistant runs them -
automation variables rendered in order (with HA's literal parsing of each
result), conditions, choose blocks and service calls against a stubbed HA
- and compares the unit's helpers and the commands sent with app/control.py:

  votes steps           the maintenance automation's `votes` variable
  conditions steps      what the container publishes to
                        sensor.ac_conditions_<room>, read back through the
                        maintenance and scene automations' `blocked`,
                        `cold_edge` and `warm_edge`
  heartbeat steps       the maintenance automation; with the leak latch on,
                        the leak automation too, and on a release the leak
                        release automation
  section changes       the scene automation for the section
  person steps          the manual-change automation, fed a climate change
                        of that kind (on, off, manual)
  button presses        the room's vote automation, fed a press of that
                        button
  climate changes       the manual-change automation, fed that change, once
                        per trigger that fires (only one may go on)

Every step publishes the scenario's conditions (phase 5) as the runtime
does, attributes from app.runtime's own _conditions_detail, or ones made up
from the step's block and edges; a step with neither has no conditions
entity at all, so most of the table runs on the templates' fail-open
fallback. The room's heat source is a switch, on when the scenario says.

A scenario's room may have more than one unit: every unit's helpers and
commands must be control's. A vote's ac_person_vote event must say what
control says - accepted exactly when control.in_episode found no forced
state in the room - and anything else must fire none. After every command,
each unit's delayed echo of it is fed back to its manual-change automation,
which must ignore it. Each change's kind is also checked against the
runtime's reaction detector (UnitWindow).

Then: the vote fuzz from v1's maintenance_logic_check, against
control.evaluate_votes, and the cases the table can't express (the hold,
an unsupported fan, the guard around commands, the conditions entity
missing, unknown, unavailable or another section's, the heat sources'
block_mode, `blocked` coming from one place, the manual-change conditions,
the vote automation's triggers).

Retired from tools/maintenance_logic_check.py, whose templates no longer
exist: the scene and correction setpoint-template cases (setpoint_applied
clamped, offset -2 / 0 for the Cooling and Warming paths) and the skip
condition (no setpoint_applied, or the almanac's never-set `off` flag).
The scenario table covers what they protected: clamping, and no seed
meaning no command.

The jinja2 shims are states, state_attr, now and namespace; HA's own
Jinja dialect isn't run here (tools/doctor.py talks to a live instance).

    PYTHONPATH=. python tools/control_logic_check.py
"""
from __future__ import annotations

import ast
import dataclasses
import random
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jinja2 import Environment                   # noqa: E402
from jinja2.utils import Namespace               # noqa: E402

import control_check as table                    # noqa: E402
from app import control                          # noqa: E402
from app import generator as g                   # noqa: E402
from app.runtime import Runtime, UnitWindow      # noqa: E402

ROOM, UNIT, SECTION = "r", "u", table.SECTION
ENTITY, LEAK_SENSOR, HEATER = "climate.r_u", "binary_sensor.r_leak", "switch.r_heater"
FAN_MODES = ["quiet", "low", "medium", "high", "auto"]


# ---------------------------------------------------------------------
# A stubbed Home Assistant that runs automation configs
# ---------------------------------------------------------------------

_IS_NUMERIC = re.compile(r"^[+-]?(?!0\d)\d*(?:\.\d*)?$")
_TRUE, _FALSE = ("1", "true", "yes", "on", "enable"), ("0", "false", "no", "off", "disable")


def parse_result(text: str):
    """Home Assistant's Template._parse_result: a rendered string becomes a
    native value when it reads as a Python literal other than a string."""
    text = text.strip()
    try:
        result = ast.literal_eval(text)
        if not isinstance(result, (str, complex)) and (
                not isinstance(result, (int, float)) or isinstance(result, bool)
                or _IS_NUMERIC.match(text) is not None):
            return result
    except (ValueError, TypeError, SyntaxError, MemoryError):
        pass
    return text


def as_boolean(value) -> bool:
    """template.result_as_boolean (cv.boolean, False when invalid)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.lower() in _TRUE
    return False


class _States:
    """HA's `states`: states('sensor.x') for the state string, and
    states.sensor.x for the state object (none when it doesn't exist), with
    last_reported - now, unless the entity was given one."""

    def __init__(self, ha: "HA") -> None:
        self._ha = ha

    def __call__(self, entity_id: str) -> str:
        return self._ha.states(entity_id)

    def __getattr__(self, domain: str):
        ha = self._ha

        class Domain:
            def __getattr__(self, obj: str):
                e = ha.entities.get(f"{domain}.{obj}")
                if e is None:
                    return None
                return SimpleNamespace(state=e["state"], attributes=e["attributes"],
                                       last_reported=e.get("last_reported") or ha.now)
        return Domain()


class HA:
    """Entity states, the services our automations call, and a runner."""

    _env = Environment()
    _compiled: dict[str, object] = {}

    def __init__(self, room: str = ROOM, entity: str = ENTITY, lag: bool = False) -> None:
        self.room, self.entity = room, entity
        self.entities: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict, bool]] = []    # service, entity, data, guard on
        self.now: datetime | None = None
        # lag: like the real units, a unit reports a new hvac mode only after
        # a moment (here: at the next wait, or the end of the run), and
        # offers fan modes only in cool
        self.lag = lag
        self.pending_mode: str | None = None
        self.waits: list[bool] = []
        self.events: list[tuple[str, dict]] = []                # event type, data

    def settle(self) -> None:
        """Time passes: a lagging unit now reports the mode it was sent."""
        if self.pending_mode is None:
            return
        e = self.entities[self.entity]
        e["state"] = self.pending_mode
        e["attributes"]["fan_modes"] = FAN_MODES if self.pending_mode == "cool" else None
        self.pending_mode = None

    # -- state --
    def set(self, entity_id: str, state, **attributes) -> None:
        self.entities[entity_id] = {"state": str(state), "attributes": dict(attributes)}

    def states(self, entity_id: str) -> str:
        e = self.entities.get(entity_id)
        return e["state"] if e else "unknown"

    def state_attr(self, entity_id: str, attribute: str):
        return (self.entities.get(entity_id) or {}).get("attributes", {}).get(attribute)

    def climate(self) -> control.Climate:
        return self.climate_of(self.entity)

    def climate_of(self, entity_id: str) -> control.Climate:
        e = self.entities[entity_id]
        return control.Climate(e["state"], e["attributes"].get("temperature"),
                               e["attributes"].get("fan_mode"))

    # -- templates --
    def render(self, template: str, variables: dict):
        if "{{" not in template and "{%" not in template:
            return template
        tmpl = self._compiled.get(template)
        if tmpl is None:
            tmpl = self._compiled[template] = self._env.from_string(template)
        return parse_result(tmpl.render(states=_States(self), state_attr=self.state_attr,
                                        now=lambda: self.now, namespace=Namespace, **variables))

    def render_complex(self, value, variables: dict):
        if isinstance(value, str):
            return self.render(value, variables)
        if isinstance(value, dict):
            return {k: self.render_complex(v, variables) for k, v in value.items()}
        if isinstance(value, list):
            return [self.render_complex(v, variables) for v in value]
        return value

    # -- services --
    def call(self, service: str, entity_id: str, data: dict) -> None:
        self.calls.append((service, entity_id, data, self.states(g.guard_id(self.room)) == "on"))
        e = self.entities.setdefault(entity_id, {"state": "unknown", "attributes": {}})
        a = e["attributes"]
        if service in ("input_boolean.turn_on", "input_boolean.turn_off"):
            e["state"] = service.rsplit("_", 1)[1]
        elif service == "input_select.select_option":
            assert data["option"] in a["options"], (entity_id, data)
            e["state"] = data["option"]
        elif service == "input_number.set_value":
            value = float(data["value"])
            assert a["min"] <= value <= a["max"], (entity_id, value)
            e["state"] = str(value)
        elif service == "input_datetime.set_datetime":
            a["timestamp"] = float(int(data["timestamp"]))     # whole seconds
            e["state"] = datetime.fromtimestamp(a["timestamp"], table.T0.tzinfo).strftime(
                "%Y-%m-%d %H:%M:%S")
        elif service == "climate.set_hvac_mode":
            if self.lag and entity_id == self.entity:
                self.pending_mode = data["hvac_mode"]      # reported later
            else:
                e["state"] = data["hvac_mode"]
        elif service == "climate.set_temperature":
            a["temperature"] = data["temperature"]
        elif service == "climate.set_fan_mode":
            assert data["fan_mode"] in (a.get("fan_modes") or []), data
            a["fan_mode"] = data["fan_mode"]
        else:
            raise AssertionError(f"unexpected service {service}")

    # -- automations --
    def run(self, automation: dict, trigger: dict | None = None,
            overrides: dict | None = None) -> dict | None:
        """Render the variables, check the conditions, run the actions.
        Returns the variables, or None if a condition stopped it."""
        variables: dict = {"trigger": trigger}
        for key, template in automation.get("variables", {}).items():
            variables[key] = (overrides[key] if overrides and key in overrides
                              else self.render_complex(template, variables))
        self.rendered = variables               # kept even when a condition stops the run
        if not all(self.check(c, variables) for c in automation.get("condition", [])):
            return None
        self.execute(automation["action"], variables)
        self.settle()
        return variables

    def variables(self, automation: dict, overrides: dict | None = None,
                  until: str | None = None) -> dict:
        variables: dict = {"trigger": None}
        for key, template in automation["variables"].items():
            variables[key] = (overrides[key] if overrides and key in overrides
                              else self.render_complex(template, variables))
            if key == until:
                break
        return variables

    def check(self, condition: dict, variables: dict) -> bool:
        kind = condition["condition"]
        if kind == "template":
            return as_boolean(self.render(condition["value_template"], variables))
        if kind == "state":
            return self.states(condition["entity_id"]) == condition["state"]
        if kind == "not":
            return not all(self.check(c, variables) for c in condition["conditions"])
        raise AssertionError(f"unexpected condition {condition}")

    def execute(self, sequence: list, variables: dict) -> None:
        for step in sequence:
            if "service" in step:
                self.call(step["service"], step["target"]["entity_id"],
                          self.render_complex(step.get("data", {}), variables))
            elif "choose" in step:
                for option in step["choose"]:
                    if all(self.check(c, variables) for c in option["conditions"]):
                        self.execute(option["sequence"], variables)
                        break
                else:
                    self.execute(step.get("default", []), variables)
            elif "event" in step:
                self.events.append((step["event"],
                                    self.render_complex(step.get("event_data", {}), variables)))
            elif "delay" in step:
                pass
            elif "wait_template" in step:
                self.settle()                   # waiting lets the unit report
                done = as_boolean(self.render(step["wait_template"], variables))
                self.waits.append(done)
                if not done and not step.get("continue_on_timeout", True):
                    return
            else:
                raise AssertionError(f"unexpected action {step}")


# ---------------------------------------------------------------------
# A scenario's room, config and world
# ---------------------------------------------------------------------

def entity_of(uid: str) -> str:
    """The climate entity of the scenario's unit `u` or of a peer."""
    return ENTITY if uid == UNIT else f"climate.r_{uid}"


def _config(ctx: control.Context, sensors: list[str], forced_off: bool,
            peers=()) -> tuple[dict, dict]:
    room = {"id": ROOM, "name": "R", "enabled": True,
            "units": [{"id": UNIT, "name": "U", "entity_id": ENTITY,
                       "leak_detection": {"enabled": True, "sensor_entity_id": LEAK_SENSOR}}]
                     + [{"id": pid, "name": pid, "entity_id": entity_of(pid)} for pid in peers],
            "sensors": [{"id": sid, "name": sid, "entity_id": f"sensor.{sid}"}
                        for sid in sensors],
            "heat_sources": [HEATER],
            "scenes": {SECTION: {"units": {UNIT: {"mode": "off"}}}} if forced_off else {}}
    config = {"system": {"heartbeat_interval_minutes": ctx.heartbeat_interval,
                         "reactive_min_delta": ctx.min_delta,
                         "sensor_stale_minutes": ctx.sensor_stale_minutes,
                         "control": dataclasses.asdict(ctx.config)},
              "outdoor": {"block_mode": ctx.block_mode},
              "rooms": [room]}
    return config, room


def _epoch(when: datetime | None) -> float | None:
    return None if when is None else float(int(when.timestamp()))


def _seed(ha: HA, unit: control.Unit, ctx: control.Context, uid: str = UNIT) -> None:
    """The unit's helpers as the record says, and the unit itself showing
    the command its state implies."""
    cap = ctx.config.offset_cap
    ha.set(g.state_id(ROOM, uid), unit.state, options=list(control.STATES))
    ha.set(g.offset_id(ROOM, uid), float(unit.offset), min=-cap, max=cap)
    lo, hi, _ = g.BASE_RANGE
    ha.set(g.base_id(ROOM, uid), float(unit.base or 0), min=lo, max=hi)
    for entity_id, when in ((g.since_id(ROOM, uid), unit.since),
                            (g.grace_until_id(ROOM, uid), unit.grace_until)):
        if when is None:
            ha.set(entity_id, "unknown")
        else:
            ha.set(entity_id, when.strftime("%Y-%m-%d %H:%M:%S"), timestamp=_epoch(when))
    ha.set(g.unblocked_id(ROOM, uid), "on" if unit.unblocked else "off")
    ha.set(g.forced_id(ROOM, uid), "on" if unit.forced else "off")
    ha.set(g.section_rest_id(ROOM, uid), "on" if unit.section_rest else "off")
    implied = control.implied_command(unit, ctx)
    mode = "fan_only" if implied is None else implied.hvac_mode
    ha.set(entity_of(uid), mode, temperature=(implied and implied.temperature) or 22.0,
           fan_mode=(implied and implied.fan_mode) or "low", fan_modes=FAN_MODES)


def _context(ha: HA, ctx: control.Context, readings: dict, almanac: dict,
             uids=(UNIT,)) -> None:
    """Everything a step reads besides the units' own helpers."""
    for name, state in ((g.guard_id(ROOM), "off"), (g.hold_id(ROOM), "off"),
                        (g.leak_id(ROOM, UNIT), "on" if ctx.leak_on else "off"),
                        (g.leak_confirm_id(ROOM, UNIT), "off"), (LEAK_SENSOR, "off")):
        ha.set(name, state)
    ha.set(g.scene_select_id(ROOM), SECTION, options=list(g.SECTIONS))
    units = {} if ctx.setpoint_applied is None else {
        uid: {"setpoint_applied": ctx.setpoint_applied} for uid in uids}
    ha.set(g.almanac_id(ROOM), "ok",
           sections={SECTION: {"units": units, "sensors": (almanac or {}).get("sensors", {})}})
    for sid, value in readings.items():
        ha.set(f"sensor.{sid}", "unavailable" if value is None else value)
    for uid in uids:
        a = ha.entities[entity_of(uid)]["attributes"]
        a.update(min_temp=ctx.unit_min, max_temp=ctx.unit_max)
    ha.set(HEATER, "on" if ctx.heat_source_on else "off")


def conditions_for(ctx: control.Context, edges, cond: control.Conditions | None
                   ) -> control.Conditions | None:
    """What the container has published for a step: the scenario's
    conditions (with the step's own edges, if it gives some), or ones made
    up from the step's block and edges, or none at all (no entity)."""
    if cond is not None:
        return cond if edges is None else dataclasses.replace(
            cond, cold_edge=edges[0], warm_edge=edges[1])
    if not ctx.blocked and edges is None:
        return None
    cold, warm = edges or (-1.0, 1.0)
    return control.Conditions(SECTION, "active" if ctx.blocked else ctx.block_mode,
                              cold_edge=cold, warm_edge=warm, would_block=ctx.blocked)


def publish(ha: HA, cond: control.Conditions | None, state: str | None = None,
            **attributes) -> None:
    """sensor.ac_conditions_r as the runtime pushes it (state the section,
    attributes from Runtime._conditions_detail), or no entity for None."""
    entity = g.conditions_id(ROOM)
    if cond is None:
        ha.entities.pop(entity, None)
        return
    ha.set(entity, state or cond.section, room_id=ROOM,
           **{**Runtime._conditions_detail(cond), "edges": {},
              "computed_at": "2026-08-01T12:00:00+08:00", **attributes})


def _view(ha: HA, now: datetime, uid: str = UNIT) -> dict:
    """The unit's helpers as control.Unit fields, in seconds from T0. A grace
    that isn't in the future is no grace; base 0 is unset."""
    def at(entity_id):
        ts = ha.state_attr(entity_id, "timestamp")
        return None if ts is None else ts - table.T0.timestamp()
    grace = at(g.grace_until_id(ROOM, uid))
    return {"state": ha.states(g.state_id(ROOM, uid)),
            "offset": int(float(ha.states(g.offset_id(ROOM, uid)))),
            "base": float(ha.states(g.base_id(ROOM, uid))) or None,
            "since": at(g.since_id(ROOM, uid)),
            "grace_until": grace if grace is not None and grace > table._secs(now) else None,
            "unblocked": ha.states(g.unblocked_id(ROOM, uid)) == "on",
            "forced": ha.states(g.forced_id(ROOM, uid)) == "on",
            "section_rest": ha.states(g.section_rest_id(ROOM, uid)) == "on"}


def _control_view(unit: control.Unit, now: datetime) -> dict:
    view = table._view(unit)
    if view["grace_until"] is not None and view["grace_until"] <= table._secs(now):
        view["grace_until"] = None
    return view


def _sent(calls: list, entity: str | None = None) -> dict | None:
    """The climate command in a run's service calls (to `entity`, if given),
    as control.Command's fields; None if nothing was sent."""
    climate = [c for c in calls if c[0].startswith("climate.") and entity in (None, c[1])]
    if not climate:
        return None
    sent = {"hvac_mode": None, "fan_mode": None, "temperature": None}
    for service, _, data, _ in climate:
        sent.update(data)
    return sent


def _command(command: control.Command | None) -> dict | None:
    return None if command is None else dataclasses.asdict(command)


def _person_event(kind: str, ha: HA, setpoint) -> tuple[control.Climate, control.Climate]:
    """A climate change of this kind, starting from what the unit shows."""
    now = ha.climate()
    on_mode = now.mode if now.mode != "off" else "cool"
    t0 = now.temperature if now.temperature is not None else 22.0
    if kind == "on":
        return control.Climate("off", t0, now.fan), control.Climate("cool", setpoint, now.fan)
    if kind == "off":
        return control.Climate(on_mode, t0, now.fan), control.Climate("off", t0, now.fan)
    if kind == "manual":
        return (control.Climate(on_mode, t0, now.fan),
                control.Climate("fan_only" if on_mode != "fan_only" else "dry", t0, now.fan))
    raise AssertionError(kind)


def _trigger(before: control.Climate, after: control.Climate, parent_id=None,
             entity_id: str = ENTITY) -> dict:
    def state(c: control.Climate) -> dict:
        return {"state": c.mode, "attributes": {"temperature": c.temperature, "fan_mode": c.fan},
                "context": {"parent_id": parent_id}}
    return {"entity_id": entity_id, "from_state": state(before), "to_state": state(after)}


def _press(direction: str, before: str | None = "2026-07-31T09:00:00+00:00",
           after: str = "2026-08-01T04:00:00+00:00") -> dict:
    """A press of the room's button: its state, the time of its last press,
    moves on (from unknown on a new button's first press)."""
    def state(value):
        return None if value is None else {"state": value, "attributes": {},
                                           "context": {"parent_id": "an automation, say"}}
    return {"entity_id": g.button_id(ROOM, direction), "from_state": state(before),
            "to_state": state(after)}


def manual_triggers(entity_id: str) -> list[dict]:
    """The manual-change automation's triggers: mode changes only (to: null
    skips attribute-only changes), and the setpoint and fan attributes - so
    a current_temperature update fires nothing."""
    return [{"platform": "state", "entity_id": entity_id, "to": None, "id": "mode"},
            {"platform": "state", "entity_id": entity_id, "attribute": "temperature",
             "id": "temperature"},
            {"platform": "state", "entity_id": entity_id, "attribute": "fan_mode",
             "id": "fan_mode"}]


def fired(automation: dict, change: dict) -> list[dict]:
    """Home Assistant's state-trigger matching, for the forms we generate,
    on the change's entity: an attribute trigger fires when that
    attribute's value changed; `to: null` when the state changed, never on
    an attribute-only change; a bare one on any change. One trigger
    variable per trigger that fires, with its id: HA runs the automation
    once for each."""
    old, new = change["from_state"], change["to_state"]
    out = []
    for t in automation["trigger"]:
        assert t["platform"] == "state", t
        if t["entity_id"] != change["entity_id"]:
            continue
        if old is None or new is None:
            hit = old is not new
        elif "attribute" in t:
            hit = old["attributes"].get(t["attribute"]) != new["attributes"].get(t["attribute"])
        elif "to" in t and t["to"] is None:
            hit = old["state"] != new["state"]
        else:
            hit = old != new
        if hit:
            out.append({**change, "platform": "state", "attribute": t.get("attribute"),
                        "id": t.get("id")})
    return out


def _runtime_kind(before: control.Climate, after: control.Climate, min_delta: float) -> str:
    """What the runtime's reaction detector makes of the same single change."""
    w = UnitWindow(before.mode != "off", after.mode != "off", before.mode, after.mode,
                   before.fan, after.fan, before.temperature, after.temperature,
                   control.setpoint_moved(before.temperature, after.temperature, min_delta))
    return w.kind


def _event_says(pv: control.PersonVote | None) -> dict | None:
    """The ac_person_vote event control's vote stands for."""
    if pv is None:
        return None
    data = {"room": ROOM, "direction": pv.direction, "accepted": pv.accepted,
            "source": pv.source}
    if pv.source == "remote":
        data.update(chosen=pv.chosen, unit=pv.unit)
    return data


# ---------------------------------------------------------------------
# Replaying the table
# ---------------------------------------------------------------------

class Replay:
    def __init__(self) -> None:
        self.steps = self.runs = self.echoes = self.kinds = self.ours = self.twice = 0
        self.events = self.ignored = 0
        self.last_vars: dict | None = None
        self.first_rendered: dict | None = None

    def scenario(self, sc: dict) -> list[str]:
        units = table._room(sc)
        ha = HA()
        first = table._ctx(sc["ctx"])
        for uid, unit in units.items():
            _seed(ha, unit, table._ctxs(first, units)[uid], uid)
        self.cond = None
        for i, st in enumerate(sc["steps"], 1):
            now = table._time(st.get("at", 0))
            ha.now = now
            try:
                if st["do"] == "conditions":
                    self.cond = table._conditions(st, now, first.sensor_stale_minutes)
                    diff = table.conditions_differ(st, self.cond) or self.read_back(
                        ha, units, table._ctx(sc["ctx"], table.under(self.cond)))
                else:
                    ctx = table._ctx(sc["ctx"], table.under(self.cond), st.get("ctx"))
                    units, diff = self.step(ha, units, table.with_edges(st, self.cond),
                                            now, ctx)
            except Exception as e:                      # a template that fails is a difference
                return [f"step {i} ({st['do']}): raised {type(e).__name__}: {e}"]
            if diff:
                return [f"step {i} ({st['do']}): {diff}"]
        return []

    def read_back(self, ha: HA, units: dict, ctx: control.Context) -> str | None:
        """The conditions just decided, published as the runtime does, must
        read back through the templates as control sees them: maintenance's
        edges and `blocked` (heat sources included), and the scene's."""
        readings = table.READINGS["settled_warm"]
        config, room = _config(ctx, list(readings), False, [u for u in units if u != UNIT])
        _context(ha, ctx, readings, table.ALMANAC, list(units))
        publish(ha, self.cond)
        want = {"blocked": control.room_blocked(ctx), "cold_edge": self.cond.cold_edge,
                "warm_edge": self.cond.warm_edge}
        got = ha.variables(g.build_maintenance_automation(config, room), until="warm_edge")
        got = {k: got[k] for k in want}
        scene = ha.variables(g.build_scene_automation(config, room, {"id": SECTION}),
                             until="blocked")["blocked"]
        self.steps += 1
        if got != want or scene != want["blocked"]:
            return f"the templates read {got} (scene blocked {scene}), control {want}"
        return None

    def step(self, ha: HA, units: dict, st: dict, now: datetime,
             ctx: control.Context) -> tuple[dict, str | None]:
        do = st["do"]
        readings = st.get("readings")
        readings = table.READINGS.get(readings, readings) if isinstance(readings, str) \
            else (readings or table.READINGS["settled_warm"])
        almanac = st.get("almanac", table.ALMANAC)
        forced = st.get("forced_off", ctx.forced_off)
        peers = [uid for uid in units if uid != UNIT]
        config, room = _config(ctx, list(readings), forced, peers)
        _context(ha, ctx, readings, almanac, list(units))
        publish(ha, conditions_for(ctx, st.get("edges"), self.cond))
        for sid, ago in st.get("reported", {}).items():      # still showing a value, but stale
            ha.entities[f"sensor.{sid}"]["last_reported"] = now - timedelta(seconds=ago)
        overrides: dict = {}                    # the templates read everything from HA
        self.room, self.config, self.ctxs = room, config, table._ctxs(ctx, units)

        if do == "votes":
            got = ha.variables(g.build_maintenance_automation(config, room), overrides,
                               until="votes")["votes"]
            want = dataclasses.asdict(table._votes(st, now, ctx.sensor_stale_minutes))
            want.pop("positions")
            self.steps += 1
            self.runs += 1
            return units, None if got == want else f"votes {got}, control {want}"

        if do == "person":
            before, after = _person_event(st["kind"], ha, st.get("setpoint"))
            st = {**st, "before": dataclasses.asdict(before), "after": dataclasses.asdict(after)}
        new, commands, pv = table.apply(st, units, now, ctx)
        commands = commands or {}

        if do == "step":
            alternatives = []
            if ctx.leak_on:
                alternatives.append((g.build_leak_automation(config, room, room["units"][0]),
                                     {"from_state": {"state": "off"},
                                      "to_state": {"state": "on"}}))
            elif units[UNIT].state == "leak":
                alternatives.append((g.build_leak_release_automation(config, room,
                                                                     room["units"][0]),
                                     {"to_state": {"state": "on"}}))
            for automation, trigger in alternatives:     # the same transition, event-driven
                side = HA()
                side.entities = {k: {"state": v["state"], "attributes": dict(v["attributes"])}
                                 for k, v in ha.entities.items()}
                side.now = now
                diff = self.compare(side, automation, trigger, overrides, units, new, commands,
                                    now, only=[UNIT])
                if diff:
                    return new, f"{automation['id']}: {diff}"
            diff = self.compare(ha, g.build_maintenance_automation(config, room), None,
                                overrides, units, new, commands, now)
            return new, diff and f"maintenance: {diff}"

        if do == "section_change":
            scene = g.build_scene_automation(config, room, {"id": SECTION, "name": SECTION})
            diff = self.compare(ha, scene, None, overrides, units, new, commands, now)
            return new, diff and f"scene: {diff}"

        if do == "press":
            vote = g.build_vote_automation(config, room)
            triggers = fired(vote, _press(st["direction"]))
            if [t["id"] for t in triggers] != [st["direction"]]:
                return new, f"the {st['direction']} button fired {triggers}"
            diff = self.compare(ha, vote, triggers, {}, units, new, commands, now, pv=pv)
            return new, diff and f"vote: {diff}"

        # a climate change (or a person's on/off/manual, made one)
        before, after = control.Climate(**st["before"]), control.Climate(**st["after"])
        manual = g.build_manual_change_automation(config, room, room["units"][0])
        kind = control.classify_change(before, after, ctx.min_delta)
        if do == "person" and kind != st["kind"]:
            return new, f"the event built for {st['kind']} classifies as {kind}"
        if kind is not None and _runtime_kind(before, after, ctx.min_delta) != kind:
            return new, f"runtime kind {_runtime_kind(before, after, ctx.min_delta)}, " \
                        f"control {kind}"
        self.kinds += 1
        ha.set(ENTITY, after.mode, **{**ha.entities[ENTITY]["attributes"],
                                      "temperature": after.temperature,
                                      "fan_mode": after.fan})
        if manual["trigger"] != manual_triggers(ENTITY):
            return new, f"manual change triggers {manual['trigger']}"
        triggers = fired(manual, _trigger(before, after))
        self.twice += len(triggers) > 1
        diff = self.compare(ha, manual, triggers, {}, units, new, commands, now, pv=pv,
                            echo=pv is not None)
        if diff is None and self.first_rendered is not None:
            want_ours = control.matches_command(control.implied_command(units[UNIT], ctx),
                                                before, after, ctx.min_delta)
            got = self.first_rendered["change"]
            if got["kind"] != kind or got["ours"] != want_ours:
                diff = f"kind/ours {got['kind']}/{got['ours']}, control {kind}/{want_ours}"
            self.ours += want_ours
        elif diff is None and not triggers and new != units:
            diff = "no trigger fired, but control changed the unit"
        return new, diff and f"manual change: {diff}"

    def compare(self, ha: HA, automation: dict, trigger, overrides: dict, olds: dict,
                news: dict, commands: dict, now: datetime, echo: bool = True,
                only: list[str] | None = None, pv: control.PersonVote | None = None
                ) -> str | None:
        """Run one automation - once per trigger, if given a list of the
        triggers that fired - and every unit's helpers and command must be
        control's, with nothing written or sent without a change, any
        ac_person_vote event control's vote, and a run that goes on at most
        once per change."""
        befores = {uid: ha.climate_of(entity_of(uid)) for uid in olds}
        ha.calls, ha.events = [], []
        self.first_rendered = None
        went_on = 0
        for i, t in enumerate(trigger if isinstance(trigger, list) else [trigger]):
            self.last_vars = ha.run(automation, t, overrides)
            went_on += self.last_vars is not None
            if i == 0:
                self.first_rendered = ha.rendered
            self.runs += 1
        self.steps += 1
        if went_on > 1:
            return f"one change went on {went_on} times"
        uids = only or list(olds)
        for uid in uids:
            got, want = _view(ha, now, uid), _control_view(news[uid], now)
            if got != want:
                return f"{uid} helpers {got}, control {want}"
            sent, wanted = _sent(ha.calls, entity_of(uid)), _command(commands.get(uid))
            if sent != wanted:
                return f"{uid} sent {sent}, control {wanted}"
            unit_helpers = [h(ROOM, uid) for h in (g.state_id, g.offset_id, g.base_id,
                                                   g.since_id, g.grace_until_id,
                                                   g.unblocked_id, g.forced_id,
                                                   g.section_rest_id)]
            if news[uid] == olds[uid] and not automation["id"].startswith("ac_scene_") and any(
                    e in unit_helpers for _, e, _, _ in ha.calls):
                return f"{uid} helpers written without a transition"
        if _sent(ha.calls) is not None:
            if not all(guarded for service, _, _, guarded in ha.calls
                       if service.startswith("climate.")):
                return "a command was sent with the guard down"
            if ha.states(g.guard_id(ROOM)) != "off":
                return "the guard was left up"
        diff = self.check_event(ha, olds, pv)
        if diff:
            return diff
        if echo:
            for uid in uids:
                if _sent(ha.calls, entity_of(uid)) is not None:
                    diff = self.echo(ha, uid, befores[uid], news, now)
                    if diff:
                        return diff
        return None

    def check_event(self, ha: HA, olds: dict, pv: control.PersonVote | None) -> str | None:
        """The run's ac_person_vote events: exactly control's vote, if any,
        accepted exactly when control.in_episode found no forced rest or
        boost in the room before it."""
        events = [data for kind, data in ha.events if kind == g.PERSON_VOTE_EVENT]
        want = _event_says(pv)
        if want is None:
            return None if not events else f"a vote event {events} where control has no vote"
        if events != [want]:
            return f"vote events {events}, control {want}"
        if events[0]["accepted"] != (not control.in_episode(olds.values())):
            return (f"the event says accepted={events[0]['accepted']}, but control.in_episode "
                    f"is {control.in_episode(olds.values())}")
        self.events += 1
        self.ignored += not events[0]["accepted"]
        return None

    def echo(self, ha: HA, uid: str, before: control.Climate, news: dict,
             now: datetime) -> str | None:
        """The unit reports our command late, after the guard is down: its
        manual-change automation must ignore it, as must control."""
        after = ha.climate_of(entity_of(uid))
        results, pv = control.on_climate_change(news, uid, before, after, now, self.ctxs)
        if pv is not None or any(results[u][0] != news[u] for u in news):
            return f"control takes {uid}'s echo {before} -> {after} for a person"
        snapshot = {u: _view(ha, now, u) for u in news}
        ha.calls, ha.events = [], []
        unit = next(u for u in self.room["units"] if u["id"] == uid)
        manual = g.build_manual_change_automation(self.config, self.room, unit)
        self.echoes += 1
        for t in fired(manual, _trigger(before, after, entity_id=entity_of(uid))):
            if ha.run(manual, t) is not None or {u: _view(ha, now, u) for u in news} != snapshot \
                    or not ha.rendered["change"]["ours"]:
                return (f"{uid}'s manual-change automation took the echo {before} -> {after} "
                        "for a person")
        return None


# ---------------------------------------------------------------------
# The vote fuzz (from v1's maintenance_logic_check), and the extra cases
# ---------------------------------------------------------------------

def _walk(sequence: list):
    """Every action in a sequence, through choose blocks."""
    for step in sequence:
        yield step
        for option in step.get("choose", []):
            yield from _walk(option["sequence"])
        yield from _walk(step.get("default", []))


def _fuzz_votes(n: int, seed: int = 7) -> int:
    rnd = random.Random(seed)
    fails = 0
    stale_count = 0
    for i in range(n):
        k = rnd.randint(1, 5)
        readings, sensors, ages = {}, {}, {}
        for j in range(k):
            sid = f"s{j}"
            readings[sid] = round(rnd.uniform(18, 30), 1) if rnd.random() > 0.15 else None
            ages[sid] = rnd.choice((60, 89 * 60, 90 * 60, 90 * 60 + 1, 5 * 3600, 3 * 86400))
            if rnd.random() > 0.1:
                sensors[sid] = {"comfort": round(rnd.uniform(20, 27), 1),
                                "band": round(rnd.uniform(0.5, 5.0), 2),
                                "trust": round(rnd.uniform(0.0, 1.0), 2)}
        edges = (-1.0, 1.0) if i % 2 == 0 else (round(rnd.uniform(-1.6, -0.4), 2),
                                                round(rnd.uniform(0.4, 2.2), 2))
        ctx = table._ctx({})
        config, room = _config(ctx, list(readings), False)
        ha = HA()
        ha.now = table.T0
        _seed(ha, control.Unit(), ctx)
        _context(ha, ctx, readings, {"sensors": sensors})
        publish(ha, conditions_for(ctx, edges if i % 2 else None, None))
        reported = {sid: table.T0 - timedelta(seconds=age) for sid, age in ages.items()}
        for sid, when in reported.items():
            ha.entities[f"sensor.{sid}"]["last_reported"] = when
        got = ha.variables(g.build_maintenance_automation(config, room), until="votes")["votes"]
        fresh = control.fresh_readings(readings, reported, table.T0, ctx.sensor_stale_minutes)
        stale_count += sum(1 for sid in readings if readings[sid] is not None and fresh[sid] is None)
        want = dataclasses.asdict(control.evaluate_votes(fresh, {"sensors": sensors}, *edges))
        want.pop("positions")
        if got != want:
            fails += 1
            print(f"[FAIL] vote fuzz {i}: jinja {got}, control {want}")
    print(f"{n - fails}/{n} fuzzed votes match control.evaluate_votes "
          f"({stale_count} readings stale, at report ages up to 3 days)")
    return fails


def _extra_cases() -> int:
    fails = 0

    def expect(ok: bool, what: str) -> None:
        nonlocal fails
        print(f"  {'OK ' if ok else 'BAD'} {what}")
        fails += not ok

    def world(unit: control.Unit, readings="hot", **ctx_changes) -> tuple[HA, dict, dict]:
        ctx = table._ctx(ctx_changes)
        readings = table.READINGS[readings]
        config, room = _config(ctx, list(readings), False)
        ha = HA()
        ha.now = table.T0
        _seed(ha, unit, ctx)
        _context(ha, ctx, readings, table.ALMANAC)
        return ha, config, room

    cool = control.Unit(state="cool", since=table._time(-table.m(60)))

    ha, config, room = world(cool)
    ha.set(g.hold_id(ROOM), "on")
    ran = ha.run(g.build_maintenance_automation(config, room))
    expect(ran is None and not ha.calls, "hold on: maintenance doesn't run")

    ha, config, room = world(control.Unit(state="rest"))
    ha.set(g.hold_id(ROOM), "on")
    ha.run(g.build_scene_automation(config, room, {"id": SECTION}))
    expect(_sent(ha.calls) is None and ha.states(g.state_id(ROOM, UNIT)) == "cool"
           and ha.states(g.guard_id(ROOM)) == "off",
           "hold on: the scene writes the helpers but sends no command")

    ha, config, room = world(control.Unit(state="leak"))
    ha.set(g.hold_id(ROOM), "on")
    ha.run(g.build_leak_release_automation(config, room, room["units"][0]),
           {"to_state": {"state": "on"}})
    expect(_sent(ha.calls) is None and ha.states(g.state_id(ROOM, UNIT)) == "cool",
           "hold on: a leak release writes the helpers but sends no command")

    ha, config, room = world(cool, leak_on=True)
    ha.set(g.hold_id(ROOM), "on")
    ha.run(g.build_leak_automation(config, room, room["units"][0]))
    expect(_sent(ha.calls) == {"hvac_mode": "off", "fan_mode": None, "temperature": None},
           "hold on: a leak trip still turns the unit off (no setpoint, no fan)")

    ha, config, room = world(cool)
    ha.entities[ENTITY]["attributes"]["fan_modes"] = None
    ha.run(g.build_maintenance_automation(config, room))
    services = [c[0] for c in ha.calls if c[0].startswith("climate.")]
    expect(services == ["climate.set_hvac_mode", "climate.set_temperature"],
           f"no fan offered: the fan call is skipped, the rest sent ({services})")

    # A unit reports its new mode only after a moment, and offers no fan
    # modes until it is in cool (the Main Bedroom, released from a leak while
    # still reporting dry, came up in cool on its remembered fan, high).
    def lagging(unit: control.Unit, mode: str, **ctx_changes) -> tuple[HA, dict, dict]:
        ctx = table._ctx(ctx_changes)
        readings = table.READINGS["settled_warm"]
        config, room = _config(ctx, list(readings), False)
        ha = HA(lag=True)
        ha.now = table.T0
        _seed(ha, unit, ctx)
        _context(ha, ctx, readings, table.ALMANAC)
        ha.set(ENTITY, mode, temperature=24.0, fan_mode="high", fan_modes=None,
               min_temp=16.0, max_temp=30.0)
        return ha, config, room

    for what, unit, mode, build in (
            ("a leak release", control.Unit(state="leak", since=table._time(-table.m(30))), "dry",
             lambda c, r: (g.build_leak_release_automation(c, r, r["units"][0]),
                           {"to_state": {"state": "on"}})),
            ("a scene", control.Unit(state="rest"), "off",
             lambda c, r: (g.build_scene_automation(c, r, {"id": SECTION}), None)),
            ("maintenance waking a rest", control.Unit(state="rest", since=table._time(-table.m(60))),
             "off", lambda c, r: (g.build_maintenance_automation(c, r), None))):
        ha, config, room = lagging(unit, mode)
        automation, trigger = build(config, room)
        ha.run(automation, trigger)
        sent = _sent(ha.calls) or {}
        expect(sent.get("hvac_mode") == "cool" and sent.get("fan_mode") == "low",
               f"{what} with the unit still reporting {mode} (no fan modes): fan low is "
               f"still sent once it reports cool ({sent})")
    waits = [step for step in _walk(g.build_scene_automation(config, room, {"id": SECTION})["action"])
             if "wait_template" in step]
    expect(len(waits) == 1 and waits[0].get("timeout") == "00:00:30"
           and waits[0].get("continue_on_timeout") is True,
           "the wait for the new mode times out after 30 s and carries on")
    ha, config, room = lagging(control.Unit(state="rest"), "off")
    ha.settle = lambda: None                     # a unit that never reports the new mode
    ha.run(g.build_scene_automation(config, room, {"id": SECTION}))
    sent = _sent(ha.calls) or {}
    expect(ha.waits == [False] and sent.get("temperature") == 22.0
           and sent.get("fan_mode") is None and ha.states(g.guard_id(ROOM)) == "off",
           "a unit that never reports the new mode: the wait times out, the fan is skipped, "
           "and the run carries on and lowers the guard")

    ha, config, room = world(cool)
    ha.run(g.build_maintenance_automation(config, room))
    order = [c[1] for c in ha.calls]
    expect(order[0] == g.guard_id(ROOM) and order[-1] == g.guard_id(ROOM)
           and order.index(g.state_id(ROOM, UNIT)) < order.index(ENTITY),
           "maintenance raises the guard, writes the helpers, sends, lowers the guard")

    # A virtual sensor reads its unit's attribute, so it goes stale with its unit.
    vctx = table._ctx({})
    vconfig, vroom = _config(vctx, ["s1"], False)
    vroom["sensors"].append({"id": "internal", "name": "internal",
                             "source": {"type": "unit_attribute", "unit_id": UNIT,
                                        "attribute": "current_temperature"}})
    vroom_alm = {"sensors": {"s1": {"comfort": 24.0, "band": 1.0, "trust": 0.5},
                             "internal": {"comfort": 24.0, "band": 1.0, "trust": 0.5}}}
    votes_seen = []
    for unit_age in (60, 3 * 3600):
        vha = HA()
        vha.now = table.T0
        _seed(vha, control.Unit(), vctx)
        _context(vha, vctx, {"s1": 25.5}, vroom_alm)
        vha.entities[ENTITY]["attributes"]["current_temperature"] = 25.5
        vha.entities[ENTITY]["last_reported"] = table.T0 - timedelta(seconds=unit_age)
        votes_seen.append(vha.variables(g.build_maintenance_automation(vconfig, vroom),
                                        until="votes")["votes"])
    expect([(v["required"], v["tally"], v["met"]) for v in votes_seen] == [(2, 2, True),
                                                                            (1, 1, True)],
           "a virtual sensor goes stale with its unit: 2 of 2 vote while the unit reports, "
           "1 of 1 once it last reported 3 h ago")

    # The conditions entity (phase 5), read only while it is the section's.
    def reads(state, heat: str = "off", mode: str = "shadow", select: str = SECTION,
              **attributes) -> tuple:
        """(blocked, cold_edge, warm_edge) as maintenance reads them, and the
        scene for SECTION's blocked, with sensor.ac_conditions_r in `state`
        (None: no such entity) holding `attributes`."""
        ctx = table._ctx({"block_mode": mode})
        readings = table.READINGS["settled_warm"]
        cfg, rm = _config(ctx, list(readings), False)
        h = HA()
        h.now = table.T0
        _seed(h, cool, ctx)
        _context(h, ctx, readings, table.ALMANAC)
        h.set(g.scene_select_id(ROOM), select, options=list(g.SECTIONS))
        h.set(HEATER, heat)
        if state is not None:
            h.set(g.conditions_id(ROOM), state, **attributes)
        v = h.variables(g.build_maintenance_automation(cfg, rm), until="warm_edge")
        scene = h.variables(g.build_scene_automation(cfg, rm, {"id": SECTION}),
                            until="blocked")["blocked"]
        return v["blocked"], v["cold_edge"], v["warm_edge"], scene

    leaned = {"cold_edge": -0.4, "warm_edge": 1.6, "blocked": True, "would_block": True,
              "block_mode": "active"}
    for what, got, want in (
            ("published for the section: read", reads(SECTION, **leaned),
             (True, -0.4, 1.6, True)),
            ("missing: edges -1/+1, not blocked", reads(None), (False, -1.0, 1.0, False)),
            ("unknown: edges -1/+1, not blocked", reads("unknown", **leaned),
             (False, -1.0, 1.0, False)),
            ("unavailable: edges -1/+1, not blocked", reads("unavailable", **leaned),
             (False, -1.0, 1.0, False)),
            ("another section's: edges -1/+1, not blocked", reads("night", **leaned),
             (False, -1.0, 1.0, False)),
            ("the section's but with no attributes: edges -1/+1, not blocked", reads(SECTION),
             (False, -1.0, 1.0, False)),
            ("shadow (would block): not blocked, edges read",
             reads(SECTION, **{**leaned, "blocked": False, "block_mode": "shadow"}),
             (False, -0.4, 1.6, False)),
            ("published for the next section before the select moves: the scene for it "
             "reads it, maintenance (still in the last section) doesn't",
             reads(SECTION, select="night", **leaned), (False, -1.0, 1.0, True)),
            ("a heat source on, no entity: blocked (the config's shadow mode)",
             reads(None, heat="on"), (True, -1.0, 1.0, True)),
            ("a heat source on, no entity, the config's block_mode off: not blocked",
             reads(None, heat="on", mode="off"), (False, -1.0, 1.0, False)),
            ("a heat source on, the entity's block_mode off wins over the config's shadow",
             reads(SECTION, heat="on", block_mode="off"), (False, -1.0, 1.0, False)),
            ("a heat source on, the entity's block_mode active",
             reads(SECTION, heat="on", mode="off", block_mode="active"),
             (True, -1.0, 1.0, True)),
            ("a heat source on, the entity unknown: the config's mode",
             reads("unknown", heat="on", mode="active"), (True, -1.0, 1.0, True))):
        expect(got == want, f"conditions entity {what} ({got})")

    sections_of = {"maintenance": "section", "scene": repr(SECTION), "leak release": "section",
                   "vote": "section", "manual change": "section"}
    automations = {"maintenance": g.build_maintenance_automation(config, room),
                   "scene": g.build_scene_automation(config, room, {"id": SECTION}),
                   "leak release": g.build_leak_release_automation(config, room,
                                                                   room["units"][0]),
                   "vote": g.build_vote_automation(config, room),
                   "manual change": g.build_manual_change_automation(config, room,
                                                                     room["units"][0])}
    counting = ("maintenance", "scene", "leak release")      # count votes: edges too
    one_place = []
    for name, a in automations.items():
        want = g._conditions_variables(config, room, sections_of[name],
                                       edges=name in counting)
        text = repr(a)
        one_place.append({k: a["variables"].get(k) for k in want} == want
                         and text.count(g.conditions_id(ROOM)) == 1
                         and text.count(HEATER) == 1)
    expect(all(one_place) and "cold_edge" not in automations["vote"]["variables"],
           "blocked and the edges come from one place (_conditions_variables), in every "
           "automation that reads them; the conditions entity and the heat source are read "
           "nowhere else")
    votes_expr = g._votes_variables(config, room, g.SENSORS_LIVE)["votes"]
    expect(all(automations[n]["variables"].get("votes") == votes_expr for n in counting)
           and all("votes" not in automations[n]["variables"]
                   for n in ("vote", "manual change"))
           and list(automations["scene"]["variables"])[:6] == [
               "now_ts", "conditions", "blocked", "cold_edge", "warm_edge", "sensor_data"],
           "the votes come from one place (_votes_variables): maintenance's, the scene's and "
           "a leak release's are the same expression, read after the edges")
    scene_sensors = automations["scene"]["variables"]["sensor_data"]
    expect(f".get('{SECTION}')" in scene_sensors and "secs" not in scene_sensors,
           "the scene counts the votes against its own section's almanac, not the select's")

    manual = g.build_manual_change_automation(config, room, room["units"][0])
    expect(manual["trigger"] == manual_triggers(ENTITY),
           "manual change triggers on mode changes (to: null) and on the temperature and "
           "fan_mode attributes, nothing else")
    reading = _trigger(control.Climate("cool", 22.0, "low"), control.Climate("cool", 22.0, "low"))
    reading["from_state"]["attributes"]["current_temperature"] = 24.1
    reading["to_state"]["attributes"]["current_temperature"] = 24.3
    expect(fired(manual, reading) == [], "a current_temperature update fires no trigger")
    person = (control.Climate("cool", 22.0, "low"), control.Climate("off", 22.0, "low"))
    for what, trig, guard in (
            ("a change carrying a parent context", _trigger(*person, parent_id="x"), "off"),
            ("a change while the guard is up", _trigger(*person), "on"),
            ("a unit coming back from unavailable",
             _trigger(control.Climate("unavailable"), person[1]), "off"),
            ("a unit dropping out", _trigger(person[0], control.Climate("unknown")), "off")):
        ha, config, room = world(cool)
        ha.set(g.guard_id(ROOM), guard)
        triggers = fired(manual, trig)
        expect(bool(triggers) and all(ha.run(manual, t) is None for t in triggers),
               f"manual change ignores {what}")
    ha, config, room = world(cool)
    triggers = fired(manual, _trigger(*person))
    expect(len(triggers) == 1 and ha.run(manual, triggers[0]) is not None
           and ha.states(g.state_id(ROOM, UNIT)) == "user_off",
           "manual change takes a person's off (one trigger: the mode)")

    # A person's vote: the buttons, the hold, one run per change.
    def vote_events(h: HA) -> list[dict]:
        return [d for kind, d in h.events if kind == g.PERSON_VOTE_EVENT]

    ha, config, room = world(cool)
    vote = g.build_vote_automation(config, room)
    expect([(t["entity_id"], t.get("to", "x"), t["id"]) for t in vote["trigger"]]
           == [(g.button_id(ROOM, d), None, d) for d in control.PERSON_VOTES],
           "the vote automation triggers on each button's state (to: null), the trigger id "
           "naming the direction")
    snapshot = _view(ha, table.T0)
    ha.set(g.hold_id(ROOM), "on")
    ha.run(vote, fired(vote, _press("too_cold"))[0])
    expect(vote_events(ha) == [{"room": ROOM, "direction": "too_cold", "accepted": True,
                                "source": "button"}]
           and not ha.calls and _view(ha, table.T0) == snapshot,
           "hold on: a press is reported (accepted, so it is learned) but changes and sends "
           "nothing")
    ha, config, room = world(cool)
    ha.set(g.hold_id(ROOM), "on")
    manual = g.build_manual_change_automation(config, room, room["units"][0])
    up = (control.Climate("cool", 22.0, "low"), control.Climate("cool", 24.0, "low"))
    ha.set(ENTITY, "cool", **{**ha.entities[ENTITY]["attributes"], "temperature": 24.0})
    for t in fired(manual, _trigger(*up)):
        ha.run(manual, t)
    expect([(d["direction"], d["accepted"], d["source"]) for d in vote_events(ha)]
           == [("too_cold", True, "remote")] and not ha.calls
           and ha.states(g.state_id(ROOM, UNIT)) == "cool",
           "hold on: a remote setpoint change is reported but changes and sends nothing")

    for what, change, runs in (
            ("a new button's first press (from unknown)", _press("too_hot", before="unknown"), 1),
            ("a button coming back from unavailable", _press("too_hot", before="unavailable"), 0),
            ("a button going unavailable", _press("too_hot", after="unavailable"), 0),
            ("a restart restoring the button (no old state)", _press("too_hot", before=None), 0)):
        ha, config, room = world(cool)
        vote = g.build_vote_automation(config, room)
        went = [ha.run(vote, t) is not None for t in fired(vote, change)]
        expect(sum(went) == runs and len(vote_events(ha)) == runs,
               f"{what}: {'a vote' if runs else 'no vote'}")
    rename = _press("too_hot", before="2026-08-01T04:00:00+00:00")
    rename["from_state"]["attributes"] = {"friendly_name": "old"}
    expect(fired(g.build_vote_automation(config, room), rename) == [],
           "a button's rename (attribute only) fires nothing")

    ha, config, room = world(cool)
    manual = g.build_manual_change_automation(config, room, room["units"][0])
    both = (control.Climate("cool", 22.0, "low"), control.Climate("dry", 24.0, "low"))
    ha.set(ENTITY, "dry", **{**ha.entities[ENTITY]["attributes"], "temperature": 24.0})
    triggers = fired(manual, _trigger(*both))
    went = [ha.run(manual, t) is not None for t in triggers]
    expect([t["id"] for t in triggers] == ["mode", "temperature"] and went == [True, False]
           and len(vote_events(ha)) == 1 and ha.states(g.state_id(ROOM, UNIT)) == "rest",
           "a mode and a setpoint changed at once fire two triggers, but only the first goes "
           "on: one vote, not two")

    ha, config, room = world(cool)
    order: list[str] = []
    call = ha.call

    def traced_call(service, entity_id, data):
        order.append(service)
        call(service, entity_id, data)

    class Traced(list):
        def append(self, item):
            order.append(item[0])
            super().append(item)
    ha.call, ha.events = traced_call, Traced()
    vote = g.build_vote_automation(config, room)
    ha.run(vote, fired(vote, _press("too_hot"))[0])
    climate = [i for i, sv in enumerate(order) if sv.startswith("climate.")]
    helpers = [i for i, sv in enumerate(order) if sv.startswith("input_select.")]
    expect(order[:2] == [g.PERSON_VOTE_EVENT, "input_boolean.turn_on"] and climate and helpers
           and helpers[0] < climate[0] and order[-1] == "input_boolean.turn_off",
           "a press: the event first, then the guard up, the helpers, the commands, the guard "
           "down")
    return fails


def main() -> int:
    replay = Replay()
    failures = {sc["name"]: replay.scenario(sc) for sc in table.SCENARIOS}
    for name, why in failures.items():
        if why:
            print(f"[FAIL] {name}: {why[0]}")
    passed = sum(1 for why in failures.values() if not why)
    print(f"{passed}/{len(table.SCENARIOS)} scenarios: the generated automations match "
          f"app.control ({replay.steps} template runs, {replay.echoes} echoes ignored, "
          f"{replay.kinds} changes' kind and is-this-ours checked, {replay.ours} of them ours, "
          f"{replay.twice} firing two triggers, run once; {replay.events} vote events, "
          f"{replay.ignored} of them ignored, each accepted exactly when control.in_episode "
          f"found no forced state)")
    fuzz = _fuzz_votes(40)
    print("other cases:")
    extra = _extra_cases()
    if passed != len(table.SCENARIOS) or fuzz or extra:
        print("\nMISMATCH: the generated Jinja disagrees with app.control")
        return 1
    print("\nALL CONTROL LOGIC CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
