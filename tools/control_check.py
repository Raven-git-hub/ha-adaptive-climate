"""
Adaptive Climate - control core check (phase 3a).

A data-driven scenario table for app.control, the cool-or-rest state
machine, in plain JSON-compatible values: times are seconds from T0,
readings are named sets (or dicts) against an almanac-shaped section,
expectations are partial unit records and exact commands. Phase 3b imports
SCENARIOS and replays the same table against the rendered Jinja.

After the table passes, each rule is removed in turn (MUTANTS) and the
scenarios that guard it must fail - proof the table actually pins every
rule rather than passing by accident.

    PYTHONPATH=. python tools/control_check.py
"""
from __future__ import annotations

import contextlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import control                                          # noqa: E402

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))


def m(minutes: int, seconds: int = 0) -> int:
    """minutes:seconds as seconds, for readable times."""
    return minutes * 60 + seconds


def cmd(mode: str, fan: str | None = None, temp: float | None = None) -> dict:
    return {"hvac_mode": mode, "fan_mode": fan, "temperature": temp}


OFF = cmd("off")

# Three sensors, comfort 24 with band 1, trust high / mid / low.
ALMANAC = {"sensors": {"s1": {"comfort": 24.0, "band": 1.0, "trust": 0.9},
                       "s2": {"comfort": 24.0, "band": 1.0, "trust": 0.5},
                       "s3": {"comfort": 24.0, "band": 1.0, "trust": 0.1}}}
READINGS = {
    "hot":          {"s1": 25.5, "s2": 25.2, "s3": 24.3},  # 2 past +1: too_warm; 3 warm side
    "cold":         {"s1": 22.5, "s2": 22.8, "s3": 24.1},  # 2 past -1: too_cold; 2 cold side
    "settled_warm": {"s1": 24.3, "s2": 24.2, "s3": 23.8},  # no votes; warm 2, cold 1
    "settled_cool": {"s1": 23.7, "s2": 23.8, "s3": 24.2},  # no votes; warm 1, cold 2
    "mixed":        {"s1": 25.5, "s2": 22.5, "s3": 22.6},  # 1 warm (trust .9) vs 2 cold (.6)
    "near":         {"s1": 24.5, "s2": 24.5, "s3": 24.0},  # +0.5: votes only past a leaned edge
    "dead":         {"s1": None, "s2": None, "s3": None},  # no usable sensor
}
CTX_DEFAULTS = {"setpoint_applied": 22.0, "unit_min": 16.0, "unit_max": 30.0,
                "heartbeat_interval": 10, "blocked": False, "leak_on": False,
                "forced_off": False}
BABY = {"unit_min": 18.0}


def s(name: str, steps: list, unit: dict | None = None, ctx: dict | None = None) -> dict:
    return {"name": name, "unit": unit or {}, "ctx": ctx or {}, "steps": steps}


def step(readings, expect_unit: dict, command, at: int = 0, **extra) -> dict:
    return {"do": "step", "at": at, "readings": readings,
            "expect": {"unit": expect_unit, "command": command}, **extra}


def votes(readings, expect: dict, **extra) -> dict:
    return {"do": "votes", "readings": readings, "expect": expect, **extra}


def section(expect_unit: dict, command, at: int = 0, **extra) -> dict:
    return {"do": "section_change", "at": at,
            "expect": {"unit": expect_unit, "command": command}, **extra}


def person(kind: str, expect_unit: dict, setpoint=None, power_after: bool = True,
           at: int = 0) -> dict:
    return {"do": "person", "at": at, "kind": kind, "setpoint": setpoint,
            "power_after": power_after, "expect": {"unit": expect_unit}}


SCENARIOS = [
    # --- votes ---------------------------------------------------------
    s("votes: two of three past the warm edge", [votes("hot", {
        "met": True, "tally": 2, "required": 2, "direction": "too_warm",
        "warm_side": 3, "cold_side": 0})]),
    s("votes: two of three past the cold edge", [votes("cold", {
        "met": True, "direction": "too_cold", "warm_side": 1, "cold_side": 2})]),
    s("votes: mixed sides resolved by trust", [votes("mixed", {
        "met": True, "tally": 3, "direction": "too_warm"})]),
    s("votes: an exact trust tie is none", [votes(
        {"s1": 25.5, "s2": 22.5, "s3": 24.1}, {"met": True, "direction": "none"},
        almanac={"sensors": {k: {"comfort": 24.0, "band": 1.0, "trust": 0.5}
                             for k in ("s1", "s2", "s3")}})]),
    s("votes: no usable sensor", [votes("dead", {
        "met": False, "required": 0, "warm_side": 0, "cold_side": 0})]),
    s("votes: a leaned warm edge", [
        votes("near", {"met": False}),
        votes("near", {"met": True, "tally": 2, "direction": "too_warm"}, edges=[-1.0, 0.4])]),
    s("votes: no comfort can't vote; a missing band is 5.0", [votes(
        {"s1": 30.0, "s2": 30.0, "s3": 24.0}, {"met": False, "required": 2, "tally": 1},
        almanac={"sensors": {"s1": {"band": 1.0, "trust": 0.9},
                             "s2": {"comfort": 24.0, "trust": 0.5},
                             "s3": {"comfort": 24.0, "band": 1.0, "trust": 0.1}}})]),

    # --- cool ----------------------------------------------------------
    s("cool -> boost when too warm", [step("hot", {"state": "boost", "since": 0},
                                           cmd("cool", "medium", 20.0))],
      unit={"since": -m(10)}),
    s("cool -> rest when too cold", [step("cold", {"state": "rest"}, OFF)],
      unit={"since": -m(10)}),
    s("cool stays cool without votes, and sends nothing", [
        step("settled_warm", {"state": "cool", "since": -m(10)}, None)],
      unit={"since": -m(10)}),
    s("cool: minimum time not met", [step("hot", {"state": "cool"}, None)],
      unit={"since": -m(4)}),
    s("cool: a heartbeat 20 s early (9:40) still counts as one", [
        step("hot", {"state": "boost"}, cmd("cool", "medium", 20.0))],
      unit={"since": -m(9, 40)}),
    s("cool: 4:59 is short of half a heartbeat", [step("hot", {"state": "cool"}, None)],
      unit={"since": -m(4, 59)}),
    s("mixed votes resolved by trust: boost, not rest", [
        step("mixed", {"state": "boost"}, cmd("cool", "medium", 20.0))],
      unit={"since": -m(10)}),
    s("a base a person set is used; base 0 falls back to setpoint_applied", [
        step("hot", {"state": "boost"}, cmd("cool", "medium", 23.0))],
      unit={"since": -m(10), "base": 25.0}),

    # --- rest ----------------------------------------------------------
    s("rest -> cool when the cold side drops below the quorum, offset +1", [
        step("settled_warm", {"state": "cool", "offset": 1, "since": 0},
             cmd("cool", "low", 23.0))],
      unit={"state": "rest", "since": -m(20)}),
    s("rest: 19:40 counts as two heartbeats", [
        step("settled_warm", {"state": "cool", "offset": 1}, cmd("cool", "low", 23.0))],
      unit={"state": "rest", "since": -m(19, 40)}),
    s("rest: 14:50 is short of 1.5 heartbeats", [step("settled_warm", {"state": "rest"}, None)],
      unit={"state": "rest", "since": -m(14, 50)}),
    s("rest holds while still too cold", [step("cold", {"state": "rest"}, None)],
      unit={"state": "rest", "since": -m(20)}),

    # --- boost ---------------------------------------------------------
    s("boost -> cool back at comfort, offset -1", [
        step("settled_cool", {"state": "cool", "offset": -1}, cmd("cool", "low", 21.0))],
      unit={"state": "boost", "since": -m(10)}),
    s("boost holds while still too warm, and sends nothing", [
        step("hot", {"state": "boost"}, None)],
      unit={"state": "boost", "since": -m(30)}),
    s("boost cap steps down twice", [
        step("hot", {"state": "cool", "offset": -1, "since": 0}, cmd("cool", "low", 21.0)),
        step("hot", {"state": "boost", "since": m(10)}, cmd("cool", "medium", 19.0), at=m(10)),
        step("hot", {"state": "cool", "offset": -2}, cmd("cool", "low", 20.0), at=m(70)),
        step("hot", {"state": "boost"}, cmd("cool", "medium", 18.0), at=m(80))],
      unit={"state": "boost", "since": -m(60)}),

    # --- offsets and limits ----------------------------------------------
    s("offset cap: a boost at -3 steps down to -3, not -4", [
        step("hot", {"state": "cool", "offset": -3}, cmd("cool", "low", 19.0))],
      unit={"state": "boost", "offset": -3, "since": -m(60)}),
    s("offset cap: a rest at +3 wakes to +3, not +4", [
        step("settled_warm", {"state": "cool", "offset": 3}, cmd("cool", "low", 25.0))],
      unit={"state": "rest", "offset": 3, "since": -m(20)}),
    s("clamp: the baby room's boost floors at 18", [
        step("hot", {"state": "boost"}, cmd("cool", "medium", 18.0))],
      unit={"base": 19.0, "offset": -1, "since": -m(10)}, ctx=BABY),
    s("clamp: a wake at 29 + 3 tops out at 30", [
        step("settled_warm", {"state": "cool", "offset": 3}, cmd("cool", "low", 30.0))],
      unit={"state": "rest", "base": 29.0, "offset": 3, "since": -m(20)}, ctx=BABY),

    # --- the cooling block -------------------------------------------------
    s("blocked: cool rests with no votes at all", [step("settled_warm", {"state": "rest"}, OFF)],
      unit={"since": -m(10)}, ctx={"blocked": True}),
    s("blocked: too-warm votes are ignored (rest, not boost)", [
        step("hot", {"state": "rest"}, OFF)], unit={"since": -m(10)}, ctx={"blocked": True}),
    s("blocked: a rest doesn't wake", [step("settled_warm", {"state": "rest"}, None)],
      unit={"state": "rest", "since": -m(20)}, ctx={"blocked": True}),
    s("blocked: a boost goes to rest", [step("hot", {"state": "rest"}, OFF)],
      unit={"state": "boost", "since": -m(10)}, ctx={"blocked": True}),
    s("unblocked by a person's on, for that unit", [
        person("on", {"state": "cool", "base": 22.0, "offset": 0, "unblocked": True,
                      "since": 0, "grace_until": m(30)}, setpoint=22.0),
        step("settled_warm", {"state": "cool"}, None, at=m(31)),
        step("hot", {"state": "boost"}, cmd("cool", "medium", 20.0), at=m(41))],
      unit={"state": "rest", "since": -m(20)}, ctx={"blocked": True}),

    # --- a person's grace ----------------------------------------------------
    s("grace holds cool against a cold vote, then lets it rest", [
        person("setpoint", {"state": "cool", "base": 24.0, "grace_until": m(30)}, setpoint=24.0),
        step("cold", {"state": "cool"}, None, at=m(20)),
        step("cold", {"state": "rest"}, OFF, at=m(31))],
      unit={"since": -m(10)}),

    # --- leak ----------------------------------------------------------------
    s("leak overrides the minimum time and grace", [
        step("settled_warm", {"state": "leak", "since": 0}, cmd("dry", "low", 22.0))],
      unit={"since": -m(1), "grace_until": m(20)}, ctx={"leak_on": True}),
    s("leak overrides a person's off", [step("cold", {"state": "leak"}, cmd("dry", "low", 22.0))],
      unit={"state": "user_off"}, ctx={"leak_on": True}),
    s("leak holds, and sends nothing again", [step("hot", {"state": "leak"}, None)],
      unit={"state": "leak", "since": -m(10)}, ctx={"leak_on": True}),
    s("leak release: as at a section change", [
        step("hot", {"state": "cool", "offset": 0, "base": 22.0, "unblocked": False,
                     "since": 0}, cmd("cool", "low", 22.0))],
      unit={"state": "leak", "offset": -2, "base": 24.0, "unblocked": True, "since": -m(10)}),
    s("leak release while blocked rests", [step("hot", {"state": "rest"}, OFF)],
      unit={"state": "leak", "since": -m(10)}, ctx={"blocked": True}),
    s("leak release in a forced-off section", [step("hot", {"state": "config_off"}, OFF)],
      unit={"state": "leak", "since": -m(10)}, ctx={"forced_off": True}),
    s("leak release with no almanac still leaves dry", [
        step("hot", {"state": "cool"}, cmd("cool", "low", None))],
      unit={"state": "leak", "since": -m(10)}, ctx={"setpoint_applied": None}),
    s("leak with no almanac still goes dry", [step("hot", {"state": "leak"}, cmd("dry", "low"))],
      unit={"since": -m(10)}, ctx={"setpoint_applied": None, "leak_on": True}),

    # --- nothing to drive / hands off -------------------------------------------
    s("no setpoint: maintenance leaves the unit alone", [step("hot", {"state": "cool"}, None)],
      unit={"since": -m(10)}, ctx={"setpoint_applied": None}),
    s("user_off is hands off", [step("hot", {"state": "user_off"}, None)],
      unit={"state": "user_off", "since": -m(30)}),
    s("manual is hands off", [step("cold", {"state": "manual"}, None)],
      unit={"state": "manual", "since": -m(30)}),
    s("config_off is hands off", [step("hot", {"state": "config_off"}, None)],
      unit={"state": "config_off", "since": -m(30)}),

    # --- release with no usable sensor -------------------------------------------
    s("no usable sensor: a rest wakes", [
        step("dead", {"state": "cool", "offset": 1}, cmd("cool", "low", 23.0))],
      unit={"state": "rest", "since": -m(20)}),
    s("no usable sensor: a boost releases", [
        step("dead", {"state": "cool", "offset": -1}, cmd("cool", "low", 21.0))],
      unit={"state": "boost", "since": -m(10)}),

    # --- section change ----------------------------------------------------------
    s("section change resets and cools", [
        section({"state": "cool", "offset": 0, "base": 22.0, "unblocked": False,
                 "grace_until": None, "since": 0}, cmd("cool", "low", 22.0))],
      unit={"state": "boost", "offset": -2, "base": 25.0, "unblocked": True,
            "grace_until": m(10), "since": -m(5)}),
    s("section change while blocked rests", [section({"state": "rest"}, OFF)],
      ctx={"blocked": True}),
    s("section change in a forced-off section", [
        section({"state": "config_off"}, OFF, forced_off=True)]),
    s("section change with the leak latched re-sends dry", [
        section({"state": "leak"}, cmd("dry", "low", 22.0))],
      unit={"state": "leak"}, ctx={"leak_on": True}),
    s("section change ends user_off", [section({"state": "cool"}, cmd("cool", "low", 22.0))],
      unit={"state": "user_off"}),
    s("section change ends manual", [section({"state": "cool"}, cmd("cool", "low", 22.0))],
      unit={"state": "manual"}),
    s("section change overrides the minimum time", [
        section({"state": "cool"}, cmd("cool", "low", 22.0))],
      unit={"state": "rest", "since": -m(1)}),
    s("section change with no almanac: fields reset, state kept, nothing sent", [
        section({"state": "user_off", "offset": 0, "base": None}, None)],
      unit={"state": "user_off", "offset": 1, "base": 24.0}, ctx={"setpoint_applied": None}),
    s("section change clamps to the baby room's 18", [
        section({"state": "cool"}, cmd("cool", "low", 18.0))],
      ctx={**BABY, "setpoint_applied": 17.0}),

    # --- a person's change ----------------------------------------------------------
    s("person setpoint: cool at the new base, with grace", [
        person("setpoint", {"state": "cool", "base": 24.0, "offset": 0, "since": 0,
                            "grace_until": m(30)}, setpoint=24.0)],
      unit={"state": "boost", "offset": -1, "since": -m(10)}),
    s("person off: user_off", [person("off", {"state": "user_off", "since": 0},
                                      power_after=False)]),
    s("person on: cool, unblocked, with grace", [
        person("on", {"state": "cool", "base": 23.0, "offset": 0, "unblocked": True,
                      "grace_until": m(30)}, setpoint=23.0)],
      unit={"state": "rest", "offset": 2}),
    s("person manual: manual", [person("manual", {"state": "manual"})]),
    s("a latched leak ignores people", [
        person("setpoint", {"state": "leak"}, setpoint=25.0),
        person("off", {"state": "leak"}, power_after=False),
        person("on", {"state": "leak", "unblocked": False}, setpoint=25.0),
        person("manual", {"state": "leak"})],
      unit={"state": "leak", "since": -m(5)}),
    s("setpoint change while resting stays rest, and wakes later at the new base", [
        person("setpoint", {"state": "rest", "base": 24.0, "offset": 0, "grace_until": None,
                            "since": -m(20)}, setpoint=24.0, power_after=False),
        step("settled_warm", {"state": "cool", "offset": 1}, cmd("cool", "low", 25.0), at=m(1))],
      unit={"state": "rest", "offset": -1, "since": -m(20)}),
    s("setpoint change while user_off stays user_off", [
        person("setpoint", {"state": "user_off", "base": 20.0, "offset": 0, "grace_until": None},
               setpoint=20.0, power_after=False)],
      unit={"state": "user_off", "offset": 1}),
    s("setpoint change in manual stays manual, with no grace", [
        person("setpoint", {"state": "manual", "base": 25.0, "offset": 0, "grace_until": None,
                            "since": -m(15)}, setpoint=25.0),
        person("knob_turn", {"state": "manual", "base": 26.0, "grace_until": None},
               setpoint=26.0, at=m(1)),
        step("cold", {"state": "manual"}, None, at=m(2))],
      unit={"state": "manual", "offset": 2, "since": -m(15)}),
    s("a knob turn counts as a setpoint change; a knob press changes nothing", [
        person("knob_turn", {"state": "cool", "base": 23.0, "grace_until": m(30)}, setpoint=23.0),
        person("knob_press", {"state": "cool", "base": 23.0}, at=m(5))],
      unit={"state": "rest"}),
]


# ---------------------------------------------------------------------
# Running the table
# ---------------------------------------------------------------------

def _time(seconds):
    return None if seconds is None else T0 + timedelta(seconds=seconds)


def _secs(when):
    return None if when is None else (when - T0).total_seconds()


def _unit(d: dict) -> control.Unit:
    return control.Unit(state=d.get("state", "cool"), offset=d.get("offset", 0),
                        base=d.get("base", 0.0), since=_time(d.get("since", -m(60))),
                        grace_until=_time(d.get("grace_until")),
                        unblocked=d.get("unblocked", False))


def _view(unit: control.Unit) -> dict:
    return {"state": unit.state, "offset": unit.offset, "base": unit.base or None,
            "since": _secs(unit.since), "grace_until": _secs(unit.grace_until),
            "unblocked": unit.unblocked}


def _ctx(*layers: dict) -> control.Context:
    merged, config = dict(CTX_DEFAULTS), {}
    for layer in layers:
        layer = dict(layer or {})
        config.update(layer.pop("control", {}))
        merged.update(layer)
    return control.Context(config=control.ControlConfig(**config), **merged)


def _votes(st: dict) -> control.Votes:
    readings = st["readings"]
    readings = READINGS[readings] if isinstance(readings, str) else readings
    return control.evaluate_votes(readings, st.get("almanac", ALMANAC),
                                  *st.get("edges", (-1.0, 1.0)))


def run(scenario: dict) -> list[str]:
    """The scenario's failures (empty when it passes)."""
    try:
        unit = _unit(scenario["unit"])
        for i, st in enumerate(scenario["steps"], 1):
            now, ctx, expect = _time(st.get("at", 0)), _ctx(scenario["ctx"], st.get("ctx")), st["expect"]
            if st["do"] == "votes":
                v = _votes(st)
                got = {k: getattr(v, k) for k in expect}
                if got != expect:
                    return [f"step {i}: votes {got}, expected {expect}"]
                continue
            command = "n/a"
            if st["do"] == "step":
                unit, command = control.step(unit, _votes(st), now, ctx)
            elif st["do"] == "section_change":
                unit, command = control.on_section_change(
                    unit, ctx, st.get("forced_off", ctx.forced_off), now)
            elif st["do"] == "person":
                unit = control.on_person_change(st["kind"], unit, st.get("setpoint"),
                                                st.get("power_after", True), now, ctx)
            view = _view(unit)
            got = {k: view[k] for k in expect["unit"]}
            if got != expect["unit"]:
                return [f"step {i}: unit {got}, expected {expect['unit']}"]
            if "command" in expect:
                got_cmd = None if command is None else command.__dict__
                if got_cmd != expect["command"]:
                    return [f"step {i}: command {got_cmd}, expected {expect['command']}"]
    except Exception as e:                       # a broken rule may crash: that's a failure too
        return [f"raised {type(e).__name__}: {e}"]
    return []


# ---------------------------------------------------------------------
# Rules removed one at a time: the named scenarios must then fail
# ---------------------------------------------------------------------

def _mutants() -> list[tuple[str, dict, list[str]]]:
    c = control
    leak_step = c._leak_step

    def leak_waits(unit, now, ctx):              # leak queued behind hands-off, grace, min time
        if ctx.leak_on and unit.state != "leak" and (
                unit.state in c.HANDS_OFF or c.in_grace(unit, now)
                or not c.min_time_met(unit, now, ctx.config.cool_min_heartbeats,
                                      ctx.heartbeat_interval)):
            return None
        return leak_step(unit, now, ctx)

    def resend(unit, ctx):                       # re-send the state's command every heartbeat
        base = c.target_base(unit, ctx)
        return unit, (c.command_for(unit.state, base, unit.offset, ctx)
                      if base is not None else None)

    return [
        ("minimum time", {"min_time_met": lambda unit, now, n, interval: True},
         ["cool: minimum time not met", "cool: 4:59 is short of half a heartbeat",
          "rest: 14:50 is short of 1.5 heartbeats"]),
        ("half-heartbeat jitter tolerance",
         {"min_time_met": lambda unit, now, n, interval:
          unit.since is None or now - unit.since >= timedelta(minutes=n * interval)},
         ["cool: a heartbeat 20 s early (9:40) still counts as one",
          "rest: 19:40 counts as two heartbeats"]),
        ("grace", {"in_grace": lambda unit, now: False},
         ["grace holds cool against a cold vote, then lets it rest"]),
        ("cooling block", {"effective_blocked": lambda unit, ctx: False},
         ["blocked: cool rests with no votes at all",
          "blocked: too-warm votes are ignored (rest, not boost)",
          "blocked: a rest doesn't wake", "blocked: a boost goes to rest"]),
        ("per-unit unblock", {"effective_blocked": lambda unit, ctx: ctx.blocked},
         ["unblocked by a person's on, for that unit"]),
        ("boost cap", {"boost_capped": lambda unit, now, config: False},
         ["boost cap steps down twice"]),
        ("offset cap", {"capped_offset": lambda offset, cap: offset},
         ["offset cap: a boost at -3 steps down to -3, not -4",
          "offset cap: a rest at +3 wakes to +3, not +4"]),
        ("clamp to the unit's range", {"clamp": lambda value, lo, hi: value},
         ["clamp: the baby room's boost floors at 18", "clamp: a wake at 29 + 3 tops out at 30",
          "section change clamps to the baby room's 18"]),
        ("leak first", {"_leak_step": leak_waits},
         ["leak overrides the minimum time and grace", "leak overrides a person's off"]),
        ("commands only on transitions", {"_stay": resend},
         ["cool stays cool without votes, and sends nothing",
          "boost holds while still too warm, and sends nothing",
          "leak holds, and sends nothing again"]),
        ("trust-weighted direction", {"_trusts": lambda trusts: {}},
         ["votes: mixed sides resolved by trust",
          "mixed votes resolved by trust: boost, not rest"]),
        ("no-setpoint no-op",
         {"target_base": lambda unit, ctx: unit.base or ctx.setpoint_applied or 0.0},
         ["no setpoint: maintenance leaves the unit alone"]),
        ("release counts against at least one",
         {"released": lambda side_count, required: side_count < required},
         ["no usable sensor: a rest wakes", "no usable sensor: a boost releases"]),
        ("a setpoint change while off keeps the state",
         {"_kept_off": lambda power_after: False},
         ["setpoint change while resting stays rest, and wakes later at the new base",
          "setpoint change while user_off stays user_off"]),
        ("a setpoint change in manual keeps manual",
         {"_kept_manual": lambda state: False},
         ["setpoint change in manual stays manual, with no grace"]),
    ]


@contextlib.contextmanager
def _patched(patches: dict):
    saved = {name: getattr(control, name) for name in patches}
    try:
        for name, fn in patches.items():
            setattr(control, name, fn)
        yield
    finally:
        for name, fn in saved.items():
            setattr(control, name, fn)


def main() -> int:
    names = [sc["name"] for sc in SCENARIOS]
    assert len(names) == len(set(names)), "scenario names must be unique"
    failures = {sc["name"]: run(sc) for sc in SCENARIOS}
    for name, why in failures.items():
        if why:
            print(f"[FAIL] {name}: {why[0]}")
    passed = sum(1 for why in failures.values() if not why)
    steps = sum(len(sc["steps"]) for sc in SCENARIOS)
    print(f"{passed}/{len(SCENARIOS)} scenarios ({steps} steps) match app.control")
    if passed != len(SCENARIOS):
        return 1

    print("\nrules removed one at a time:")
    unguarded = 0
    for rule, patches, must_fail in _mutants():
        assert set(must_fail) <= set(names), set(must_fail) - set(names)
        with _patched(patches):
            failed = {sc["name"] for sc in SCENARIOS if run(sc)}
        missed = [n for n in must_fail if n not in failed]
        unguarded += bool(missed)
        print(f"  {'OK ' if not missed else 'BAD'} without {rule}: {len(failed)} scenario(s) "
              f"fail, including all {len(must_fail)} that guard it"
              + (f" - but these passed: {missed}" if missed else ""))
    if unguarded:
        print("\nSOME RULES ARE NOT PINNED BY THE TABLE")
        return 1
    print("\nALL CONTROL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
