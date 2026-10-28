"""
Adaptive Climate - control core check (phase 3a).

A data-driven scenario table for app.control, the cool-or-rest state
machine, in plain JSON-compatible values: times are seconds from T0,
readings are named sets (or dicts) against an almanac-shaped section,
expectations are partial unit records and exact commands. Steps are votes,
a heartbeat (step), a section change, a person's change of a given kind
(on, off, manual), a press of the room's too_cold or too_hot button, or one
climate state change (before and after, as HA's manual-change automation
sees it; a setpoint change is a vote). A scenario's room has the unit `u`
and, where it is about the whole room, peers: every step runs on all of
them. tools/control_logic_check.py imports SCENARIOS and replays the same
table through the generated automations.

After the table passes, each rule is removed in turn (MUTANTS) and the
scenarios that guard it must fail - proof the table actually pins every
rule rather than passing by accident.

    PYTHONPATH=. python tools/control_check.py
"""
from __future__ import annotations

import contextlib
import dataclasses
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
                "forced_off": False, "sensor_stale_minutes": 90}
DAYS_3 = 3 * 24 * 60 * 60       # a sensor put away three days ago
BABY = {"unit_min": 18.0}
MAIN = "u"                      # the scenario's unit; peers are the room's others


def s(name: str, steps: list, unit: dict | None = None, ctx: dict | None = None,
      peers: dict | None = None) -> dict:
    return {"name": name, "unit": unit or {}, "ctx": ctx or {}, "peers": peers or {},
            "steps": steps}


def step(readings, expect_unit: dict, command, at: int = 0, **extra) -> dict:
    return {"do": "step", "at": at, "readings": readings,
            "expect": {"unit": expect_unit, "command": command}, **extra}


def votes(readings, expect: dict, **extra) -> dict:
    return {"do": "votes", "readings": readings, "expect": expect, **extra}


def section(expect_unit: dict, command, at: int = 0, **extra) -> dict:
    return {"do": "section_change", "at": at,
            "expect": {"unit": expect_unit, "command": command}, **extra}


def person(kind: str, expect_unit: dict, setpoint=None, at: int = 0) -> dict:
    """A person turning the unit on or off (setpoint: what it shows after),
    or changing its mode (manual)."""
    return {"do": "person", "at": at, "kind": kind, "setpoint": setpoint,
            "expect": {"unit": expect_unit}}


_ROOM = ("peers", "peer_commands")


def press(direction: str, expect_unit: dict, command, accepted: bool = True, at: int = 0,
          **room) -> dict:
    """A press of the room's too_cold or too_hot button; accepted or
    ignored (an episode is running). room: peers / peer_commands."""
    assert set(room) <= set(_ROOM), room
    return {"do": "press", "at": at, "direction": direction,
            "expect": {"unit": expect_unit, "command": command, "accepted": accepted, **room}}


def change(before: tuple, after: tuple, expect_unit: dict, at: int = 0, **expect) -> dict:
    """One climate state change, (mode, setpoint, fan) before and after, as
    the manual-change automation sees it. expect: command, vote ({direction,
    accepted}, or None for no vote), peers, peer_commands."""
    assert set(expect) <= {"command", "vote", *_ROOM}, expect
    return {"do": "change", "at": at, "before": climate(*before), "after": climate(*after),
            "expect": {"unit": expect_unit, **expect}}


def vote(direction: str, accepted: bool = True) -> dict:
    return {"direction": direction, "accepted": accepted}


def climate(mode: str, temperature: float | None = None, fan: str | None = None) -> dict:
    return {"mode": mode, "temperature": temperature, "fan": fan}


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
    # A step's `reported` gives, per sensor, how many seconds ago Home Assistant
    # last reported it (default: just now); past sensor_stale_minutes its
    # reading is taken as unavailable, though its state still shows a value.
    s("votes: a stale sensor has no vote and counts toward no quorum", [votes("hot", {
        "met": False, "tally": 1, "required": 2, "direction": "none",
        "warm_side": 2}, reported={"s1": m(91)})]),
    s("votes: 90 minutes since the last report is not stale yet", [votes("hot", {
        "met": True, "tally": 2, "required": 2, "direction": "too_warm"},
        reported={"s1": m(90)})]),
    s("votes: sensor_stale_minutes is the config's", [votes("hot", {
        "met": False, "tally": 1, "required": 2}, reported={"s1": m(31)})],
      ctx={"sensor_stale_minutes": 30}),
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
    s("a sensor put away 3 days ago doesn't boost with its frozen reading", [
        step("hot", {"state": "cool", "since": -m(60)}, None, reported={"s1": DAYS_3, "s2": DAYS_3})],
      unit={"since": -m(60)}),
    s("a stale cold reading doesn't hold a rest", [
        step("cold", {"state": "cool", "offset": 1}, cmd("cool", "low", 23.0),
             reported={"s1": DAYS_3, "s2": DAYS_3})],
      unit={"state": "rest", "since": -m(60)}),
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
        person("on", {"state": "cool", "base": 24.0, "grace_until": m(30)}, setpoint=24.0),
        step("cold", {"state": "cool"}, None, at=m(20)),
        step("cold", {"state": "rest"}, OFF, at=m(31))],
      unit={"state": "user_off", "since": -m(10)}),

    # --- leak ----------------------------------------------------------------
    s("leak overrides the minimum time and grace", [
        step("settled_warm", {"state": "leak", "since": 0}, OFF)],
      unit={"since": -m(1), "grace_until": m(20)}, ctx={"leak_on": True}),
    s("leak overrides a person's off", [step("cold", {"state": "leak"}, OFF)],
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
    s("leak turns the unit off, not dry", [
        step("hot", {"state": "leak"}, OFF), step("hot", {"state": "leak"}, None, at=m(10))],
      unit={"state": "boost", "offset": -1, "since": -m(30)}, ctx={"leak_on": True}),
    s("leak release with no almanac rests, off, sending nothing", [
        step("hot", {"state": "rest", "since": 0}, None),
        step("cold", {"state": "rest"}, None, at=m(20))],
      unit={"state": "leak", "since": -m(10)}, ctx={"setpoint_applied": None}),
    s("leak with no almanac still goes off", [step("hot", {"state": "leak"}, OFF)],
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
    s("section change with the leak latched re-sends off", [
        section({"state": "leak"}, OFF)],
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
    s("person off: user_off", [person("off", {"state": "user_off", "since": 0})]),
    s("person on: cool, unblocked, with grace", [
        person("on", {"state": "cool", "base": 23.0, "offset": 0, "unblocked": True,
                      "grace_until": m(30)}, setpoint=23.0)],
      unit={"state": "rest", "offset": 2}),
    s("person manual: manual", [person("manual", {"state": "manual"})]),
    s("a latched leak ignores people", [
        change(("off", 22.0, "low"), ("off", 25.0, "low"), {"state": "leak", "base": None},
               command=None, vote=vote("too_cold")),
        person("off", {"state": "leak"}),
        person("on", {"state": "leak", "unblocked": False}, setpoint=25.0),
        person("manual", {"state": "leak"})],
      unit={"state": "leak", "since": -m(5)}, ctx={"leak_on": True}),

    # --- a person's vote: the too_cold / too_hot buttons -----------------------------
    s("too_cold while cooling: a forced rest, then cool at base+1 with no extra offset", [
        press("too_cold", {"state": "rest", "base": 23.0, "offset": 0, "forced": True,
                           "since": 0, "grace_until": None}, OFF),
        step("settled_warm", {"state": "rest", "forced": True}, None, at=m(10)),
        step("cold", {"state": "rest", "forced": True}, None, at=m(20)),
        step("settled_warm", {"state": "cool", "base": 23.0, "offset": 0, "forced": False,
                              "since": m(30)}, cmd("cool", "low", 23.0), at=m(30))],
      unit={"since": -m(60)}),
    s("too_hot while cooling: a forced boost, then back at comfort to cool at base-1", [
        press("too_hot", {"state": "boost", "base": 21.0, "offset": 0, "forced": True,
                          "since": 0}, cmd("cool", "medium", 19.0)),
        step("hot", {"state": "boost", "forced": True}, None, at=m(10)),
        step("settled_cool", {"state": "cool", "base": 21.0, "offset": 0, "forced": False},
             cmd("cool", "low", 21.0), at=m(20))],
      unit={"since": -m(60)}),
    s("too_hot: the boost cap releases a forced boost to cool at base-1", [
        press("too_hot", {"state": "boost", "base": 21.0}, cmd("cool", "medium", 19.0)),
        step("hot", {"state": "cool", "base": 21.0, "offset": 0, "forced": False},
             cmd("cool", "low", 21.0), at=m(60))],
      unit={"since": -m(60)}),
    s("too_hot from user_off: a forced boost", [
        press("too_hot", {"state": "boost", "base": 21.0, "offset": 0, "forced": True,
                          "since": 0}, cmd("cool", "medium", 19.0))],
      unit={"state": "user_off", "offset": 1, "since": -m(30)}),
    s("too_hot from a system rest: a forced boost, ahead of the rest's minimum time", [
        press("too_hot", {"state": "boost", "base": 21.0, "offset": 0, "forced": True,
                          "since": 0}, cmd("cool", "medium", 19.0))],
      unit={"state": "rest", "offset": 1, "since": -m(5)}),
    s("too_hot during a system boost: base-1, and the boost keeps its since", [
        press("too_hot", {"state": "boost", "base": 21.0, "offset": 0, "forced": True,
                          "since": -m(20)}, cmd("cool", "medium", 19.0)),
        step("hot", {"state": "cool", "base": 21.0, "offset": 0}, cmd("cool", "low", 21.0),
             at=m(40))],
      unit={"state": "boost", "offset": -1, "since": -m(20)}),
    s("too_cold during a system rest: base+1, still resting, since kept, nothing sent", [
        press("too_cold", {"state": "rest", "base": 23.0, "offset": 0, "forced": True,
                           "since": -m(5)}, None),
        step("settled_warm", {"state": "cool", "base": 23.0, "offset": 0},
             cmd("cool", "low", 23.0), at=m(10))],
      unit={"state": "rest", "offset": -1, "since": -m(5)}),
    s("too_cold in user_off: base+1, the unit stays off, and no episode starts", [
        press("too_cold", {"state": "user_off", "base": 23.0, "offset": 0, "forced": False,
                           "since": -m(30)}, None),
        press("too_cold", {"state": "user_off", "base": 24.0}, None, at=m(1))],
      unit={"state": "user_off", "since": -m(30)}),
    s("one correction per episode: a second press of either button is ignored; "
      "one after the release counts", [
        press("too_cold", {"state": "rest", "base": 23.0, "forced": True}, OFF),
        press("too_hot", {"state": "rest", "base": 23.0, "forced": True, "since": 0}, None,
              accepted=False, at=m(2)),
        press("too_cold", {"state": "rest", "base": 23.0, "forced": True}, None,
              accepted=False, at=m(3)),
        step("settled_warm", {"state": "cool", "base": 23.0, "offset": 0, "forced": False},
             cmd("cool", "low", 23.0), at=m(20)),
        press("too_cold", {"state": "rest", "base": 24.0, "forced": True, "since": m(25)}, OFF,
              at=m(25))],
      unit={"since": -m(60)}),
    s("a forced state on another unit holds the whole room's episode", [
        press("too_hot", {"state": "cool", "base": None, "forced": False}, None, accepted=False,
              peers={"v": {"state": "rest", "base": 23.0, "forced": True}},
              peer_commands={"v": None})],
      unit={"since": -m(60)}, peers={"v": {"state": "rest", "base": 23.0, "forced": True,
                                           "since": -m(5)}}),
    s("a vote moves every unit in the room", [
        press("too_cold", {"state": "rest", "base": 23.0, "forced": True}, OFF,
              peers={"v": {"state": "rest", "base": 23.0, "offset": 0, "forced": True,
                           "since": 0}},
              peer_commands={"v": OFF})],
      unit={"since": -m(60)}, peers={"v": {"state": "boost", "offset": -1, "since": -m(10)}}),
    s("presses with the unit in leak are taken but leave it alone", [
        press("too_cold", {"state": "leak", "base": None, "forced": False}, None),
        press("too_hot", {"state": "leak", "base": None, "forced": False}, None, at=m(1))],
      unit={"state": "leak", "since": -m(5)}, ctx={"leak_on": True}),
    s("leak beats a vote: a latched unit not yet in leak is left alone", [
        press("too_hot", {"state": "cool", "base": None, "forced": False}, None)],
      unit={"since": -m(60)}, ctx={"leak_on": True}),
    s("a press with the unit in config_off is taken but leaves it alone", [
        press("too_hot", {"state": "config_off", "base": None, "forced": False}, None)],
      unit={"state": "config_off", "since": -m(5)}),
    s("a press with the unit in manual is taken but leaves it alone", [
        press("too_cold", {"state": "manual", "base": None, "forced": False}, None)],
      unit={"state": "manual", "since": -m(5)}),
    s("a section change during a forced state ends it, and the next press counts", [
        press("too_cold", {"state": "rest", "base": 23.0, "forced": True}, OFF),
        section({"state": "cool", "base": 22.0, "offset": 0, "forced": False},
                cmd("cool", "low", 22.0), at=m(5)),
        press("too_cold", {"state": "rest", "base": 23.0, "forced": True}, OFF, at=m(6))],
      unit={"since": -m(60)}),
    s("a person's on ends a forced rest, and the next press counts", [
        press("too_cold", {"state": "rest", "base": 23.0, "forced": True}, OFF),
        person("on", {"state": "cool", "base": 24.0, "forced": False, "unblocked": True,
                      "grace_until": m(32)}, setpoint=24.0, at=m(2)),
        press("too_hot", {"state": "boost", "base": 23.0, "forced": True, "grace_until": None},
              cmd("cool", "medium", 21.0), at=m(3))],
      unit={"since": -m(60)}),
    s("a vote's base is clamped to the unit's range (bottom)", [
        press("too_hot", {"state": "boost", "base": 18.0}, cmd("cool", "medium", 18.0))],
      unit={"base": 18.5, "since": -m(60)}, ctx=BABY),
    s("a vote's base is clamped to the unit's range (top)", [
        press("too_cold", {"state": "rest", "base": 30.0}, OFF)],
      unit={"base": 29.5, "since": -m(60)}, ctx=BABY),
    s("no base and no almanac: a press is taken but leaves the unit alone", [
        press("too_cold", {"state": "cool", "base": None, "forced": False}, None)],
      unit={"since": -m(60)}, ctx={"setpoint_applied": None}),
    s("a leftover marker on a cooling unit doesn't hold an episode", [
        press("too_cold", {"state": "rest", "base": 23.0, "forced": True}, OFF)],
      unit={"since": -m(60), "forced": True}),

    # --- the remote: a setpoint change is a vote ------------------------------------
    s("a remote setpoint up is too_cold, with the chosen value as the base", [
        change(("cool", 22.0, "low"), ("cool", 25.0, "low"),
               {"state": "rest", "base": 25.0, "offset": 0, "forced": True, "since": 0},
               command=OFF, vote=vote("too_cold")),
        step("settled_warm", {"state": "cool", "base": 25.0, "offset": 0},
             cmd("cool", "low", 25.0), at=m(20))],
      unit={"since": -m(60)}),
    s("a remote setpoint down is too_hot, with the chosen value as the base", [
        change(("cool", 22.0, "low"), ("cool", 20.0, "low"),
               {"state": "boost", "base": 20.0, "offset": 0, "forced": True},
               command=cmd("cool", "medium", 18.0), vote=vote("too_hot"))],
      unit={"since": -m(60)}),
    s("a remote setpoint change during a forced state is ignored", [
        press("too_cold", {"state": "rest", "base": 23.0, "forced": True}, OFF),
        change(("off", 22.0, "low"), ("off", 25.0, "low"),
               {"state": "rest", "base": 23.0, "forced": True}, at=m(2),
               command=None, vote=vote("too_cold", accepted=False))],
      unit={"since": -m(60)}),
    s("a remote setpoint up while user_off keeps the unit off, at the chosen base", [
        change(("off", 22.0, "low"), ("off", 24.0, "low"),
               {"state": "user_off", "base": 24.0, "offset": 0, "forced": False},
               command=None, vote=vote("too_cold"))],
      unit={"state": "user_off", "offset": 1, "since": -m(30)}),
    s("a setpoint change on a unit in manual is a vote for the room's other units", [
        change(("dry", 22.0, None), ("dry", 24.0, None), {"state": "manual", "base": None},
               command=None, vote=vote("too_cold"),
               peers={"v": {"state": "rest", "base": 23.0, "forced": True}},
               peer_commands={"v": OFF})],
      unit={"state": "manual", "since": -m(15)}, peers={"v": {"since": -m(60)}}),
    s("a delayed echo of a forced boost is ours", [
        press("too_hot", {"state": "boost", "base": 21.0}, cmd("cool", "medium", 19.0)),
        change(("cool", 22.0, "low"), ("cool", 19.0, "medium"),
               {"state": "boost", "base": 21.0, "forced": True}, at=m(1), command=None,
               vote=None)],
      unit={"since": -m(60)}),

    # --- a climate change: our own command showing up late, or a person ---
    s("a delayed echo of our own off command is ignored", [
        change(("cool", 22.0, "low"), ("off", 22.0, "low"),
               {"state": "rest", "since": -m(3), "grace_until": None})],
      unit={"state": "rest", "since": -m(3)}),
    s("a delayed echo of our own cool command is ignored", [
        change(("off", 25.0, "low"), ("cool", 23.0, "low"),
               {"state": "cool", "offset": 1, "since": -m(2), "grace_until": None,
                "unblocked": False})],
      unit={"state": "cool", "offset": 1, "since": -m(2)}),
    s("a delayed echo of our own boost command is ignored", [
        change(("cool", 23.0, "low"), ("cool", 21.0, "medium"),
               {"state": "boost", "offset": 1, "since": -m(2)})],
      unit={"state": "boost", "offset": 1, "since": -m(2)}),
    s("a delayed echo of our own leak off is ignored", [
        change(("cool", 22.0, "low"), ("off", 22.0, "low"), {"state": "leak", "since": -m(1)})],
      unit={"state": "leak", "since": -m(1)}, ctx={"leak_on": True}),
    s("a setpoint within reactive_min_delta of our command is ours", [
        change(("cool", 21.0, "low"), ("cool", 22.4, "low"), {"state": "cool", "base": None})]),
    s("a setpoint change on a resting unit is a person's: a forced rest, waking at the new base", [
        change(("off", 22.0, "low"), ("off", 24.0, "low"),
               {"state": "rest", "base": 24.0, "offset": 0, "since": -m(20), "forced": True,
                "grace_until": None}, command=None, vote=vote("too_cold")),
        step("settled_warm", {"state": "cool", "offset": 0}, cmd("cool", "low", 24.0), at=m(1))],
      unit={"state": "rest", "offset": -1, "since": -m(20)}),
    s("a person turning a resting unit on is not an echo", [
        change(("off", 22.0, "low"), ("cool", 23.0, "low"),
               {"state": "cool", "base": 23.0, "unblocked": True, "grace_until": m(30)},
               command=None, vote=None)],
      unit={"state": "rest"}),
    s("a mode change away from our command is manual", [
        change(("cool", 22.0, "low"), ("fan_only", 22.0, "low"), {"state": "manual", "since": 0},
               vote=None)]),
    s("in manual, a change of nothing we track does nothing", [
        change(("dry", 22.0, None), ("dry", 22.2, None), {"state": "manual", "since": -m(10)},
               vote=None)],
      unit={"state": "manual", "since": -m(10)}),
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
                        unblocked=d.get("unblocked", False), forced=d.get("forced", False))


def _view(unit: control.Unit) -> dict:
    return {"state": unit.state, "offset": unit.offset, "base": unit.base or None,
            "since": _secs(unit.since), "grace_until": _secs(unit.grace_until),
            "unblocked": unit.unblocked, "forced": unit.forced}


def _ctx(*layers: dict) -> control.Context:
    merged, config = dict(CTX_DEFAULTS), {}
    for layer in layers:
        layer = dict(layer or {})
        config.update(layer.pop("control", {}))
        merged.update(layer)
    return control.Context(config=control.ControlConfig(**config), **merged)


def _room(scenario: dict) -> dict[str, control.Unit]:
    """The scenario's units: `u`, then its peers."""
    return {MAIN: _unit(scenario["unit"]),
            **{pid: _unit(d) for pid, d in scenario.get("peers", {}).items()}}


def _ctxs(ctx: control.Context, uids) -> dict[str, control.Context]:
    """Each unit's context: the scenario's for `u`; peers have no leak latch
    and no forced-off section."""
    peer = dataclasses.replace(ctx, leak_on=False, forced_off=False)
    return {uid: ctx if uid == MAIN else peer for uid in uids}


def _votes(st: dict, now: datetime = T0,
           stale_minutes: int = control.DEFAULT_STALE_MINUTES) -> control.Votes:
    readings = st["readings"]
    readings = READINGS[readings] if isinstance(readings, str) else readings
    reported = {sid: now - timedelta(seconds=ago) for sid, ago in st.get("reported", {}).items()}
    readings = control.fresh_readings(readings, reported, now, stale_minutes)
    return control.evaluate_votes(readings, st.get("almanac", ALMANAC),
                                  *st.get("edges", (-1.0, 1.0)))


def apply(st: dict, units: dict[str, control.Unit], now: datetime, ctx: control.Context
          ) -> tuple[dict[str, control.Unit], dict[str, control.Command | None] | None,
                     control.PersonVote | None]:
    """One step on the room through app.control: the units after it, each
    unit's command (None for a person's on/off/manual, which sends
    nothing), and the vote, if the step was one."""
    ctxs = _ctxs(ctx, units)
    do, results, pv = st["do"], None, None
    if do == "step":
        v = _votes(st, now, ctx.sensor_stale_minutes)
        results = {uid: control.step(u, v, now, ctxs[uid]) for uid, u in units.items()}
    elif do == "section_change":
        forced = st.get("forced_off", ctx.forced_off)
        results = {uid: control.on_section_change(u, ctxs[uid], forced and uid == MAIN, now)
                   for uid, u in units.items()}
    elif do == "person":
        new = control.on_person_change(st["kind"], units[MAIN], st.get("setpoint"), now, ctx)
        return {**units, MAIN: new}, None, None
    elif do == "press":
        results, pv = control.on_button(st["direction"], units, now, ctxs)
    elif do == "change":
        results, pv = control.on_climate_change(units, MAIN, control.Climate(**st["before"]),
                                                control.Climate(**st["after"]), now, ctxs)
    else:
        raise AssertionError(do)
    return ({uid: r[0] for uid, r in results.items()},
            {uid: r[1] for uid, r in results.items()}, pv)


def _command(command: control.Command | None) -> dict | None:
    return None if command is None else command.__dict__


def compare(expect: dict, units: dict[str, control.Unit], commands: dict | None,
            pv: control.PersonVote | None, view=_view) -> str | None:
    """A step's expectations against the room after it: the unit's fields
    and command, the peers', and the vote (accepted, or for a climate
    change its direction and acceptance, or None for none)."""
    checks = [("unit", MAIN, expect["unit"])] + [
        ("peer " + pid, pid, want) for pid, want in expect.get("peers", {}).items()]
    for what, uid, want in checks:
        got = {k: view(units[uid])[k] for k in want}
        if got != want:
            return f"{what} {got}, expected {want}"
    wanted_cmds = ([(MAIN, expect["command"])] if "command" in expect else []) + list(
        expect.get("peer_commands", {}).items())
    for uid, want in wanted_cmds:
        got = _command((commands or {}).get(uid))
        if got != want:
            return f"{uid} command {got}, expected {want}"
    if "accepted" in expect and (pv is None or pv.accepted != expect["accepted"]):
        return f"vote {pv}, expected accepted={expect['accepted']}"
    if "vote" in expect:
        got = None if pv is None else {"direction": pv.direction, "accepted": pv.accepted}
        if got != expect["vote"]:
            return f"vote {got}, expected {expect['vote']}"
    return None


def run(scenario: dict) -> list[str]:
    """The scenario's failures (empty when it passes)."""
    try:
        units = _room(scenario)
        for i, st in enumerate(scenario["steps"], 1):
            now, ctx, expect = _time(st.get("at", 0)), _ctx(scenario["ctx"], st.get("ctx")), st["expect"]
            if st["do"] == "votes":
                v = _votes(st, now, ctx.sensor_stale_minutes)
                got = {k: getattr(v, k) for k in expect}
                if got != expect:
                    return [f"step {i}: votes {got}, expected {expect}"]
                continue
            units, commands, pv = apply(st, units, now, ctx)
            why = compare(expect, units, commands, pv)
            if why:
                return [f"step {i}: {why}"]
    except Exception as e:                       # a broken rule may crash: that's a failure too
        return [f"raised {type(e).__name__}: {e}"]
    return []


# ---------------------------------------------------------------------
# Rules removed one at a time: the named scenarios must then fail
# ---------------------------------------------------------------------

_VOTE_BASE = control.vote_base
_SECTION_CHANGE = control.on_section_change
_PERSON_CHANGE = control.on_person_change


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
        ("one correction per episode", {"in_episode": lambda units: False},
         ["one correction per episode: a second press of either button is ignored; "
          "one after the release counts",
          "a forced state on another unit holds the whole room's episode",
          "a remote setpoint change during a forced state is ignored"]),
        ("an episode is a forced rest or boost, not a leftover marker",
         {"forced_now": lambda unit: unit.forced},
         ["a leftover marker on a cooling unit doesn't hold an episode"]),
        ("a forced state releases to cool at its base",
         {"release_offset": lambda unit, change: unit.offset + change},
         ["too_cold while cooling: a forced rest, then cool at base+1 with no extra offset",
          "too_hot while cooling: a forced boost, then back at comfort to cool at base-1",
          "too_hot: the boost cap releases a forced boost to cool at base-1",
          "too_cold during a system rest: base+1, still resting, since kept, nothing sent",
          "a remote setpoint up is too_cold, with the chosen value as the base"]),
        ("a vote leaves leak, config_off and manual alone",
         {"_votes_alone": lambda unit, ctx: False},
         ["presses with the unit in leak are taken but leave it alone",
          "leak beats a vote: a latched unit not yet in leak is left alone",
          "a press with the unit in config_off is taken but leaves it alone",
          "a press with the unit in manual is taken but leaves it alone",
          "a setpoint change on a unit in manual is a vote for the room's other units",
          "a latched leak ignores people"]),
        ("a unit already in the forced state keeps its since",
         {"_keeps_since": lambda unit, state: False},
         ["too_hot during a system boost: base-1, and the boost keeps its since",
          "too_cold during a system rest: base+1, still resting, since kept, nothing sent"]),
        ("the remote's chosen value as the base",
         {"vote_base": lambda direction, unit, ctx, chosen=None:
          _VOTE_BASE(direction, unit, ctx)},
         ["a remote setpoint up is too_cold, with the chosen value as the base",
          "a remote setpoint down is too_hot, with the chosen value as the base",
          "a remote setpoint up while user_off keeps the unit off, at the chosen base"]),
        ("a section change ends a forced state",
         {"on_section_change": lambda unit, ctx, forced_off, now: (lambda r: (
             dataclasses.replace(r[0], forced=unit.forced), r[1]))(
                 _SECTION_CHANGE(unit, ctx, forced_off, now))},
         ["a section change during a forced state ends it, and the next press counts"]),
        ("a person's on, off or manual ends a forced state",
         {"on_person_change": lambda kind, unit, new_setpoint, now, ctx: dataclasses.replace(
             _PERSON_CHANGE(kind, unit, new_setpoint, now, ctx), forced=unit.forced)},
         ["a person's on ends a forced rest, and the next press counts"]),
        ("the is-this-ours check",
         {"matches_command": lambda command, before, after, min_delta: False},
         ["a delayed echo of our own off command is ignored",
          "a delayed echo of our own cool command is ignored",
          "a delayed echo of our own boost command is ignored",
          "a setpoint within reactive_min_delta of our command is ours",
          "a delayed echo of a forced boost is ours"]),
        ("an off command's echo keeps the setpoint (just off instead)",
         {"_off_matches": lambda before, after, min_delta: after.mode == "off"},
         ["a setpoint change on a resting unit is a person's: a forced rest, waking at the "
          "new base", "a remote setpoint change during a forced state is ignored"]),
        ("stale sensors taken as unavailable",
         {"is_stale": lambda last_reported, now, stale_minutes: False},
         ["votes: a stale sensor has no vote and counts toward no quorum",
          "votes: sensor_stale_minutes is the config's",
          "a sensor put away 3 days ago doesn't boost with its frozen reading",
          "a stale cold reading doesn't hold a rest"]),
        ("the stale cut-off at sensor_stale_minutes (stale from the minute instead)",
         {"is_stale": lambda last_reported, now, stale_minutes: last_reported is not None
          and (now - last_reported).total_seconds() >= stale_minutes * 60},
         ["votes: 90 minutes since the last report is not stale yet"]),
        ("the reactive_min_delta tolerance (exact setpoint instead)",
         {"_same_setpoint": lambda shown, commanded, min_delta: shown == commanded},
         ["a setpoint within reactive_min_delta of our command is ours"]),
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
