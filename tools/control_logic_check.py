"""
Adaptive Climate - control logic check (phase 3b): the generated Jinja
against app/control.py.

Replays every scenario in tools/control_check.py through the automations
app/generator.py actually emits, run the way Home Assistant runs them -
automation variables rendered in order (with HA's literal parsing of each
result), conditions, choose blocks and service calls against a stubbed HA
- and compares the unit's helpers and the commands sent with app/control.py:

  votes steps           the maintenance automation's `votes` variable
  heartbeat steps       the maintenance automation; with the leak latch on,
                        the leak automation too, and on a release the leak
                        release automation
  section changes       the scene automation for the section
  person steps          the manual-change automation, fed a climate change
                        of that kind (knob steps are phase 8: not replayed)
  climate changes       the manual-change automation, fed that change

After every command, the unit's delayed echo of it is fed back to the
manual-change automation, which must ignore it. Each change's kind is
also checked against the runtime's reaction detector (UnitWindow).

Then: the vote fuzz from v1's maintenance_logic_check, against
control.evaluate_votes, and the cases the table can't express (the hold,
an unsupported fan, the guard around commands, the blocked variable, the
manual-change conditions).

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
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jinja2 import Environment                   # noqa: E402
from jinja2.utils import Namespace               # noqa: E402

import control_check as table                    # noqa: E402
from app import control                          # noqa: E402
from app import generator as g                   # noqa: E402
from app.runtime import UnitWindow               # noqa: E402

ROOM, UNIT, SECTION = "r", "u", "day"
ENTITY, LEAK_SENSOR = "climate.r_u", "binary_sensor.r_leak"
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


class HA:
    """Entity states, the services our automations call, and a runner."""

    _env = Environment()
    _compiled: dict[str, object] = {}

    def __init__(self, room: str = ROOM, entity: str = ENTITY) -> None:
        self.room, self.entity = room, entity
        self.entities: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict, bool]] = []    # service, entity, data, guard on
        self.now: datetime | None = None

    # -- state --
    def set(self, entity_id: str, state, **attributes) -> None:
        self.entities[entity_id] = {"state": str(state), "attributes": dict(attributes)}

    def states(self, entity_id: str) -> str:
        e = self.entities.get(entity_id)
        return e["state"] if e else "unknown"

    def state_attr(self, entity_id: str, attribute: str):
        return (self.entities.get(entity_id) or {}).get("attributes", {}).get(attribute)

    def climate(self) -> control.Climate:
        e = self.entities[self.entity]
        return control.Climate(e["state"], e["attributes"].get("temperature"),
                               e["attributes"].get("fan_mode"))

    # -- templates --
    def render(self, template: str, variables: dict):
        if "{{" not in template and "{%" not in template:
            return template
        tmpl = self._compiled.get(template)
        if tmpl is None:
            tmpl = self._compiled[template] = self._env.from_string(template)
        return parse_result(tmpl.render(states=self.states, state_attr=self.state_attr,
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
            elif "delay" in step:
                pass
            else:
                raise AssertionError(f"unexpected action {step}")


# ---------------------------------------------------------------------
# A scenario's room, config and world
# ---------------------------------------------------------------------

def _config(ctx: control.Context, sensors: list[str], forced_off: bool) -> tuple[dict, dict]:
    room = {"id": ROOM, "name": "R", "enabled": True,
            "units": [{"id": UNIT, "name": "U", "entity_id": ENTITY,
                       "leak_detection": {"enabled": True, "sensor_entity_id": LEAK_SENSOR}}],
            "sensors": [{"id": sid, "name": sid, "entity_id": f"sensor.{sid}"}
                        for sid in sensors],
            "scenes": {SECTION: {"units": {UNIT: {"mode": "off"}}}} if forced_off else {}}
    config = {"system": {"heartbeat_interval_minutes": ctx.heartbeat_interval,
                         "reactive_min_delta": ctx.min_delta,
                         "control": dataclasses.asdict(ctx.config)},
              "rooms": [room]}
    return config, room


def _epoch(when: datetime | None) -> float | None:
    return None if when is None else float(int(when.timestamp()))


def _seed(ha: HA, unit: control.Unit, ctx: control.Context) -> None:
    """The unit's helpers as the record says, and the unit itself showing
    the command its state implies."""
    cap = ctx.config.offset_cap
    ha.set(g.state_id(ROOM, UNIT), unit.state, options=list(control.STATES))
    ha.set(g.offset_id(ROOM, UNIT), float(unit.offset), min=-cap, max=cap)
    lo, hi, _ = g.BASE_RANGE
    ha.set(g.base_id(ROOM, UNIT), float(unit.base or 0), min=lo, max=hi)
    for entity_id, when in ((g.since_id(ROOM, UNIT), unit.since),
                            (g.grace_until_id(ROOM, UNIT), unit.grace_until)):
        if when is None:
            ha.set(entity_id, "unknown")
        else:
            ha.set(entity_id, when.strftime("%Y-%m-%d %H:%M:%S"), timestamp=_epoch(when))
    ha.set(g.unblocked_id(ROOM, UNIT), "on" if unit.unblocked else "off")
    implied = control.implied_command(unit, ctx)
    mode = "fan_only" if implied is None else implied.hvac_mode
    ha.set(ENTITY, mode, temperature=(implied and implied.temperature) or 22.0,
           fan_mode=(implied and implied.fan_mode) or "low", fan_modes=FAN_MODES)


def _context(ha: HA, ctx: control.Context, readings: dict, almanac: dict) -> None:
    """Everything a step reads besides the unit's own helpers."""
    for name, state in ((g.guard_id(ROOM), "off"), (g.hold_id(ROOM), "off"),
                        (g.leak_id(ROOM, UNIT), "on" if ctx.leak_on else "off"),
                        (g.leak_confirm_id(ROOM, UNIT), "off"), (LEAK_SENSOR, "off")):
        ha.set(name, state)
    ha.set(g.scene_select_id(ROOM), SECTION, options=list(g.SECTIONS))
    units = {} if ctx.setpoint_applied is None else {UNIT: {"setpoint_applied": ctx.setpoint_applied}}
    ha.set(g.almanac_id(ROOM), "ok",
           sections={SECTION: {"units": units, "sensors": (almanac or {}).get("sensors", {})}})
    for sid, value in readings.items():
        ha.set(f"sensor.{sid}", "unavailable" if value is None else value)
    a = ha.entities[ENTITY]["attributes"]
    a.update(min_temp=ctx.unit_min, max_temp=ctx.unit_max)


def _view(ha: HA, now: datetime) -> dict:
    """The unit's helpers as control.Unit fields, in seconds from T0. A grace
    that isn't in the future is no grace; base 0 is unset."""
    def at(entity_id):
        ts = ha.state_attr(entity_id, "timestamp")
        return None if ts is None else ts - table.T0.timestamp()
    grace = at(g.grace_until_id(ROOM, UNIT))
    return {"state": ha.states(g.state_id(ROOM, UNIT)),
            "offset": int(float(ha.states(g.offset_id(ROOM, UNIT)))),
            "base": float(ha.states(g.base_id(ROOM, UNIT))) or None,
            "since": at(g.since_id(ROOM, UNIT)),
            "grace_until": grace if grace is not None and grace > table._secs(now) else None,
            "unblocked": ha.states(g.unblocked_id(ROOM, UNIT)) == "on"}


def _control_view(unit: control.Unit, now: datetime) -> dict:
    view = table._view(unit)
    if view["grace_until"] is not None and view["grace_until"] <= table._secs(now):
        view["grace_until"] = None
    return view


def _sent(calls: list) -> dict | None:
    """The climate command in a run's service calls, as control.Command's
    fields; None if nothing was sent."""
    climate = [c for c in calls if c[0].startswith("climate.")]
    if not climate:
        return None
    sent = {"hvac_mode": None, "fan_mode": None, "temperature": None}
    for service, _, data, _ in climate:
        sent.update(data)
    return sent


def _command(command: control.Command | None) -> dict | None:
    return None if command is None else dataclasses.asdict(command)


def _person_event(kind: str, ha: HA, setpoint, power_after: bool,
                  min_delta: float) -> tuple[control.Climate, control.Climate]:
    """A climate change of this kind, starting from what the unit shows."""
    now = ha.climate()
    on_mode = now.mode if now.mode != "off" else "cool"
    t0 = now.temperature if now.temperature is not None else 22.0
    if kind == "setpoint":
        if setpoint is not None and abs(setpoint - t0) < min_delta:
            t0 = setpoint - 2
        mode = on_mode if power_after else "off"
        return control.Climate(mode, t0, now.fan), control.Climate(mode, setpoint, now.fan)
    if kind == "on":
        return control.Climate("off", t0, now.fan), control.Climate("cool", setpoint, now.fan)
    if kind == "off":
        return control.Climate(on_mode, t0, now.fan), control.Climate("off", t0, now.fan)
    if kind == "manual":
        return (control.Climate(on_mode, t0, now.fan),
                control.Climate("fan_only" if on_mode != "fan_only" else "dry", t0, now.fan))
    raise AssertionError(kind)


def _trigger(before: control.Climate, after: control.Climate, parent_id=None) -> dict:
    def state(c: control.Climate) -> dict:
        return {"state": c.mode, "attributes": {"temperature": c.temperature, "fan_mode": c.fan},
                "context": {"parent_id": parent_id}}
    return {"from_state": state(before), "to_state": state(after)}


def manual_triggers(entity_id: str) -> list[dict]:
    """The manual-change automation's triggers: mode changes only (to: null
    skips attribute-only changes), and the setpoint and fan attributes - so
    a current_temperature update fires nothing."""
    return [{"platform": "state", "entity_id": entity_id, "to": None},
            {"platform": "state", "entity_id": entity_id, "attribute": "temperature"},
            {"platform": "state", "entity_id": entity_id, "attribute": "fan_mode"}]


def fired(automation: dict, change: dict) -> list[dict]:
    """Home Assistant's state-trigger matching, for the forms we generate:
    an attribute trigger fires when that attribute's value changed; `to:
    null` when the state changed, never on an attribute-only change; a bare
    one on any change. One trigger variable per trigger that fires: HA runs
    the automation once for each."""
    old, new = change["from_state"], change["to_state"]
    out = []
    for t in automation["trigger"]:
        assert t["platform"] == "state", t
        if "attribute" in t:
            hit = old["attributes"].get(t["attribute"]) != new["attributes"].get(t["attribute"])
        elif "to" in t and t["to"] is None:
            hit = old["state"] != new["state"]
        else:
            hit = old != new
        if hit:
            out.append({**change, "platform": "state", "entity_id": t["entity_id"],
                        "attribute": t.get("attribute")})
    return out


def _runtime_kind(before: control.Climate, after: control.Climate, min_delta: float) -> str:
    """What the runtime's reaction detector makes of the same single change."""
    w = UnitWindow(before.mode != "off", after.mode != "off", before.mode, after.mode,
                   before.fan, after.fan, before.temperature, after.temperature,
                   control.setpoint_moved(before.temperature, after.temperature, min_delta))
    return w.kind


# ---------------------------------------------------------------------
# Replaying the table
# ---------------------------------------------------------------------

class Replay:
    def __init__(self) -> None:
        self.steps = self.runs = self.echoes = self.kinds = self.ours = self.twice = 0
        self.skipped: list[str] = []
        self.last_vars: dict | None = None
        self.first_rendered: dict | None = None

    def scenario(self, sc: dict) -> list[str]:
        unit = table._unit(sc["unit"])
        ha = HA()
        first = table._ctx(sc["ctx"])
        _seed(ha, unit, first)
        for i, st in enumerate(sc["steps"], 1):
            now = table._time(st.get("at", 0))
            ctx = table._ctx(sc["ctx"], st.get("ctx"))
            ha.now = now
            try:
                unit, diff = self.step(ha, unit, st, now, ctx)
            except Exception as e:                      # a template that fails is a difference
                return [f"step {i} ({st['do']}): raised {type(e).__name__}: {e}"]
            if diff:
                return [f"step {i} ({st['do']}): {diff}"]
        return []

    def step(self, ha: HA, unit: control.Unit, st: dict, now: datetime,
             ctx: control.Context) -> tuple[control.Unit, str | None]:
        do = st["do"]
        readings = st.get("readings")
        readings = table.READINGS.get(readings, readings) if isinstance(readings, str) \
            else (readings or table.READINGS["settled_warm"])
        almanac = st.get("almanac", table.ALMANAC)
        forced = st.get("forced_off", ctx.forced_off)
        config, room = _config(ctx, list(readings), forced)
        _context(ha, ctx, readings, almanac)
        edges = st.get("edges")
        overrides = {"blocked": True} if ctx.blocked else {}
        if edges:
            overrides.update(cold_edge=edges[0], warm_edge=edges[1])

        if do == "votes":
            got = ha.variables(g.build_maintenance_automation(config, room), overrides,
                               until="votes")["votes"]
            want = dataclasses.asdict(table._votes(st))
            want.pop("positions")
            self.steps += 1
            self.runs += 1
            return unit, None if got == want else f"votes {got}, control {want}"

        if do == "step":
            new, command = control.step(unit, table._votes(st), now, ctx)
            alternatives = []
            if ctx.leak_on:
                alternatives.append((g.build_leak_automation(config, room, room["units"][0]),
                                     {"from_state": {"state": "off"},
                                      "to_state": {"state": "on"}}))
            elif unit.state == "leak":
                alternatives.append((g.build_leak_release_automation(config, room,
                                                                     room["units"][0]),
                                     {"to_state": {"state": "on"}}))
            for automation, trigger in alternatives:     # the same transition, event-driven
                side = HA()
                side.entities = {k: {"state": v["state"], "attributes": dict(v["attributes"])}
                                 for k, v in ha.entities.items()}
                side.now = now
                diff = self.compare(side, automation, trigger, overrides, unit, new, command,
                                    now)
                if diff:
                    return new, f"{automation['id']}: {diff}"
            diff = self.compare(ha, g.build_maintenance_automation(config, room), None,
                                overrides, unit, new, command, now)
            return new, diff and f"maintenance: {diff}"

        if do == "section_change":
            new, command = control.on_section_change(unit, ctx, forced, now)
            scene = g.build_scene_automation(config, room, {"id": SECTION, "name": SECTION})
            diff = self.compare(ha, scene, None, overrides, unit, new, command, now)
            return new, diff and f"scene: {diff}"

        if do in ("person", "change"):
            manual = g.build_manual_change_automation(config, room, room["units"][0])
            if do == "person":
                new = control.on_person_change(st["kind"], unit, st.get("setpoint"),
                                               st.get("power_after", True), now, ctx)
                if st["kind"] not in ("setpoint", "on", "off", "manual"):
                    self.skipped.append(st["kind"])        # knob events arrive in phase 8
                    _seed(ha, new, ctx)
                    return new, None
                before, after = _person_event(st["kind"], ha, st.get("setpoint"),
                                              st.get("power_after", True), ctx.min_delta)
            else:
                before, after = (control.Climate(**st["before"]), control.Climate(**st["after"]))
                new = control.on_climate_change(unit, before, after, now, ctx)
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
            diff = self.compare(ha, manual, triggers, {}, unit, new, None, now, echo=False)
            if diff is None and self.first_rendered is not None:
                want_ours = control.matches_command(control.implied_command(unit, ctx),
                                                    before, after, ctx.min_delta)
                got = self.first_rendered[g._unit_var(room["units"][0])]
                if got["kind"] != kind or got["ours"] != want_ours:
                    diff = f"kind/ours {got['kind']}/{got['ours']}, control {kind}/{want_ours}"
                self.ours += want_ours
            elif diff is None and not triggers and new != unit:
                diff = "no trigger fired, but control changed the unit"
            return new, diff and f"manual change: {diff}"

        raise AssertionError(do)

    def compare(self, ha: HA, automation: dict, trigger, overrides: dict, old: control.Unit,
                new: control.Unit, command, now: datetime, echo: bool = True) -> str | None:
        """Run one automation - once per trigger, if given a list of the
        triggers that fired - and its helpers and command must be control's,
        with nothing written or sent without a change."""
        before = ha.climate()
        ha.calls = []
        self.first_rendered = None
        for i, t in enumerate(trigger if isinstance(trigger, list) else [trigger]):
            self.last_vars = ha.run(automation, t, overrides)
            if i == 0:
                self.first_rendered = ha.rendered
            self.runs += 1
        self.steps += 1
        got, want = _view(ha, now), _control_view(new, now)
        if got != want:
            return f"helpers {got}, control {want}"
        sent, wanted = _sent(ha.calls), _command(command)
        if sent != wanted:
            return f"sent {sent}, control {wanted}"
        if sent is not None:
            if not all(guarded for service, _, _, guarded in ha.calls
                       if service.startswith("climate.")):
                return "a command was sent with the guard down"
            if ha.states(g.guard_id(ROOM)) != "off":
                return "the guard was left up"
        elif new == old and not automation["id"].startswith("ac_scene_") and any(
                e not in (g.leak_id(ROOM, UNIT), g.leak_confirm_id(ROOM, UNIT))
                for _, e, _, _ in ha.calls):
            return "helpers written without a transition"
        if echo and sent is not None:
            return self.echo(ha, automation, before, new, now)
        return None

    def echo(self, ha: HA, automation: dict, before: control.Climate, new: control.Unit,
             now: datetime) -> str | None:
        """The unit reports our command late, after the guard is down: the
        manual-change automation must ignore it, as must control."""
        after = ha.climate()
        room = {"id": ROOM, "units": [{"id": UNIT, "entity_id": ENTITY}], "sensors": []}
        config = {"system": {}, "rooms": [room]}
        ctx = _echo_ctx(ha)
        if control.on_climate_change(new, before, after, now, ctx) != new:
            return f"control takes the echo {before} -> {after} for a person"
        snapshot = _view(ha, now)
        ha.calls = []
        manual = g.build_manual_change_automation(config, room, room["units"][0])
        self.echoes += 1
        for t in fired(manual, _trigger(before, after)):
            if ha.run(manual, t) is not None or _view(ha, now) != snapshot or not ha.rendered[
                    g._unit_var(room["units"][0])]["ours"]:
                return (f"the manual-change automation took the echo {before} -> {after} "
                        "for a person")
        return None


def _echo_ctx(ha: HA) -> control.Context:
    units = (ha.state_attr(g.almanac_id(ROOM), "sections") or {}).get(SECTION, {}).get("units", {})
    a = ha.entities[ENTITY]["attributes"]
    return control.Context(setpoint_applied=(units.get(UNIT) or {}).get("setpoint_applied"),
                           unit_min=a.get("min_temp") or 16.0, unit_max=a.get("max_temp") or 30.0)


# ---------------------------------------------------------------------
# The vote fuzz (from v1's maintenance_logic_check), and the extra cases
# ---------------------------------------------------------------------

def _fuzz_votes(n: int, seed: int = 7) -> int:
    rnd = random.Random(seed)
    fails = 0
    for i in range(n):
        k = rnd.randint(1, 5)
        readings, sensors = {}, {}
        for j in range(k):
            sid = f"s{j}"
            readings[sid] = round(rnd.uniform(18, 30), 1) if rnd.random() > 0.15 else None
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
        got = ha.variables(g.build_maintenance_automation(config, room),
                           {"cold_edge": edges[0], "warm_edge": edges[1]}, until="votes")["votes"]
        want = dataclasses.asdict(control.evaluate_votes(readings, {"sensors": sensors}, *edges))
        want.pop("positions")
        if got != want:
            fails += 1
            print(f"[FAIL] vote fuzz {i}: jinja {got}, control {want}")
    print(f"{n - fails}/{n} fuzzed votes match control.evaluate_votes")
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

    ha, config, room = world(cool)
    ha.run(g.build_maintenance_automation(config, room))
    order = [c[1] for c in ha.calls]
    expect(order[0] == g.guard_id(ROOM) and order[-1] == g.guard_id(ROOM)
           and order.index(g.state_id(ROOM, UNIT)) < order.index(ENTITY),
           "maintenance raises the guard, writes the helpers, sends, lowers the guard")

    automations = [g.build_maintenance_automation(config, room),
                   g.build_scene_automation(config, room, {"id": SECTION}),
                   g.build_leak_release_automation(config, room, room["units"][0])]
    blocked = {a["variables"]["blocked"] for a in automations}
    expect(blocked == {g._blocked_template(room)}
           and HA().render(g._blocked_template(room), {}) is False,
           "blocked comes from one place and is false until phase 5")

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
          f"{replay.twice} firing two triggers; "
          f"{len(replay.skipped)} knob steps not replayed: {sorted(set(replay.skipped))})")
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
