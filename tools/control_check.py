"""
Adaptive Climate - control core check (phase 3a).

A data-driven scenario table for app.control, the cool-or-rest state
machine, in plain JSON-compatible values: times are seconds from T0,
readings are named sets (or dicts) against an almanac-shaped section,
expectations are partial unit records and exact commands. Steps are votes,
a heartbeat (step), a section change, a person's change of a given kind
(on, off, manual), a press of the room's too_cold or too_hot button, one
climate state change (before and after, as HA's manual-change automation
sees it; a setpoint change is a vote), or the section's outdoor conditions
as the container decides them (phase 5: the comfort point, the lean of the
vote edges and the cooling block), which the steps after it then run under.
A scenario's room has the unit `u` and, where it is about the whole room,
peers: every step runs on all of them. tools/control_logic_check.py imports
SCENARIOS and replays the same table through the generated automations,
publishing each step's conditions to sensor.ac_conditions_<room> for them
to read.

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
    "very_hot":     {"s1": 26.5, "s2": 26.2, "s3": 24.3},  # 2 past +1.7, a leaned warm edge
    "cold":         {"s1": 22.5, "s2": 22.8, "s3": 24.1},  # 2 past -1: too_cold; 2 cold side
    "settled_warm": {"s1": 24.3, "s2": 24.2, "s3": 23.8},  # no votes; warm 2, cold 1
    "settled_cool": {"s1": 23.7, "s2": 23.8, "s3": 24.2},  # no votes; warm 1, cold 2
    "mixed":        {"s1": 25.5, "s2": 22.5, "s3": 22.6},  # 1 warm (trust .9) vs 2 cold (.6)
    "near":         {"s1": 24.5, "s2": 24.5, "s3": 24.0},  # +0.5: votes only past a leaned edge
    "near_cold":    {"s1": 23.5, "s2": 23.5, "s3": 24.0},  # -0.5: likewise, on the cold side
    "dead":         {"s1": None, "s2": None, "s3": None},  # no usable sensor
    "warm_one":     {"s1": 25.5, "s2": 24.5, "s3": 24.3},  # 1 past +1: short of the quorum
    "just_warm":    {"s1": 24.95, "s2": 24.95, "s3": 24.0},  # +0.95: past a leaned +0.9, not +1
}
CTX_DEFAULTS = {"setpoint_applied": 22.0, "unit_min": 16.0, "unit_max": 30.0,
                "heartbeat_interval": 10, "blocked": False, "leak_on": False,
                "forced_off": False, "sensor_stale_minutes": 90,
                "heat_source_on": False, "block_mode": "shadow"}
SECTION = "day"                 # the section a scenario's conditions are for
COND_DEFAULTS = {"margin": 6.0, "was_blocked": False, "block_mode": "shadow", "k": 0.1}
DAYS_3 = 3 * 24 * 60 * 60       # a sensor put away three days ago
BABY = {"unit_min": 18.0}
MAIN = "u"                      # the scenario's unit; peers are the room's others
_ROOM = ("peers", "peer_commands")      # a step's expectations of the room's other units


def s(name: str, steps: list, unit: dict | None = None, ctx: dict | None = None,
      peers: dict | None = None) -> dict:
    return {"name": name, "unit": unit or {}, "ctx": ctx or {}, "peers": peers or {},
            "steps": steps}


def step(readings, expect_unit: dict, command, at: int = 0, overruled: bool | None = None,
         **extra) -> dict:
    """A maintenance heartbeat for the room (control.on_heartbeat).
    overruled: whether the room overrules the section's block at it
    (unchecked if None). extra: peers / peer_commands expectations, and
    the step's own edges, reported ages, almanac or ctx."""
    room = {k: extra.pop(k) for k in _ROOM if k in extra}
    expect = {"unit": expect_unit, "command": command, **room}
    if overruled is not None:
        expect["overruled"] = overruled
    return {"do": "step", "at": at, "readings": readings, "expect": expect, **extra}


def votes(readings, expect: dict, **extra) -> dict:
    return {"do": "votes", "readings": readings, "expect": expect, **extra}


def section(expect_unit: dict, command, at: int = 0, **extra) -> dict:
    """A section change: the scene, with the room's sensors at `readings`
    (default settled_warm) against the almanac and the conditions' edges."""
    return {"do": "section_change", "at": at,
            "expect": {"unit": expect_unit, "command": command}, **extra}


def person(kind: str, expect_unit: dict, setpoint=None, at: int = 0) -> dict:
    """A person turning the unit on or off (setpoint: what it shows after),
    or changing its mode (manual)."""
    return {"do": "person", "at": at, "kind": kind, "setpoint": setpoint,
            "expect": {"unit": expect_unit}}


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


def conditions(expect: dict, **inputs) -> dict:
    """The section's conditions as the container decides them at a
    crossover (control.section_conditions). inputs: mean and max, the
    forecast's (None: no outdoor value); comfort, the room comfort point
    (default: control.comfort_point over `almanac` and `readings`, with
    `reported` ages); margin (6), was_blocked, block_mode (shadow), k (0.1).
    expect: a partial Conditions, `blocked` being control's view. The steps
    after it run under them: their vote edges, and the block and block_mode
    as control sees them."""
    return {"do": "conditions", "inputs": inputs, "expect": expect}


def climate(mode: str, temperature: float | None = None, fan: str | None = None) -> dict:
    return {"mode": mode, "temperature": temperature, "fan": fan}


# The room overrules the block (decision 143): an active block from a cold
# forecast, its warm edge leaned to +1.7.
BLOCK = conditions({"blocked": True, "warm_edge": 1.7}, comfort=24.0, mean=17.0, max=17.0,
                   block_mode="active")
OVERRULE_WAKES = ("the room overrules the block: a blocked room that warms to a quorum wakes "
                  "and cools at its base, the block lifted for the section")
OVERRULE_REST_MIN = "the room doesn't overrule the block before the rest minimum time"
OVERRULE_EVERY_REST = ("the overrule waits for every unit resting for the block, then wakes "
                       "them together")
OVERRULE_NOT_HEAT = "a heat source's block isn't overruled: the heater is why the room is warm"
OVERRULE_PLAIN_EDGE = ("the overrule counts the un-leaned warm edge (+1): past a leaned +0.9 "
                       "isn't enough, past +1 is")
OVERRULE_TOO_WARM = "a too-cold quorum doesn't overrule the block"
OVERRULE_AT_BASE = ("the overrule wakes into cool at the base, offset 0, fan low: no boost, "
                    "whatever the votes")
OVERRULE_NOT_HELD = "a rest the block no longer holds wakes by its own rule and overrules nothing"
OVERRULE_LIFTS_ALL = ("the overrule lifts the block for every unit: a person's off it didn't "
                      "wake is unblocked, and wakes by its own rule")
OVERRULE_FORCED_REST = ("the overrule wakes a forced rest: a too_cold vote under the block "
                        "doesn't hold a room that has since warmed")


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

    # --- outdoor conditions (phase 5): the comfort point, lean and block --
    s("comfort point: the trust-weighted mean of the usable sensors' comfort readings", [
        conditions({"comfort_point": 24.47}, mean=24.47, max=24.47,
                   almanac={"sensors": {"s1": {"comfort": 24.0, "band": 1.0, "trust": 0.9},
                                        "s2": {"comfort": 25.0, "band": 1.0, "trust": 0.5},
                                        "s3": {"comfort": 26.0, "band": 1.0, "trust": 0.1}}})]),
    s("comfort point: a plain mean while no sensor has trust", [
        conditions({"comfort_point": 25.0},
                   mean=25.0, max=25.0,
                   almanac={"sensors": {sid: {"comfort": c, "band": 5.0, "trust": 0.0}
                                        for sid, c in (("s1", 24.0), ("s2", 25.0),
                                                       ("s3", 26.0))}})]),
    s("comfort point: an unavailable or stale sensor, or one with no comfort, is left out", [
        conditions({"comfort_point": 26.0}, mean=26.0, max=26.0,
                   readings={"s1": None, "s2": 25.0, "s3": 24.0}, reported={"s2": m(91)},
                   almanac={"sensors": {"s1": {"comfort": 24.0, "trust": 0.9},
                                        "s2": {"comfort": 25.0, "trust": 0.5},
                                        "s3": {"comfort": 26.0, "trust": 0.1},
                                        "s4": {"comfort": 30.0, "trust": 1.0}}})]),
    s("lean table: delta -6 (hot outside) cools early, rests late", [
        conditions({"delta_mean": -6.0, "lean": -0.6, "cold_edge": -1.6, "warm_edge": 0.4},
                   comfort=24.0, mean=30.0, max=31.0)]),
    s("lean table: delta 0 leans nothing", [
        conditions({"delta_mean": 0.0, "lean": 0.0, "cold_edge": -1.0, "warm_edge": 1.0},
                   comfort=24.0, mean=24.0, max=26.0)]),
    s("lean table: delta +6 (cool outside) rests early, cools late", [
        conditions({"delta_mean": 6.0, "lean": 0.6, "cold_edge": -0.4, "warm_edge": 1.6},
                   comfort=24.0, mean=18.0, max=19.0)]),
    s("lean table: delta +12 (cold) caps the cold edge, not the warm", [
        conditions({"delta_mean": 12.0, "lean": 1.2, "cold_edge": -0.4, "warm_edge": 2.2},
                   comfort=24.0, mean=12.0, max=13.0)]),
    s("lean caps: the hot side stops at -0.6 on both edges", [
        conditions({"lean": -1.0, "cold_edge": -1.6, "warm_edge": 0.4},
                   comfort=24.0, mean=34.0, max=35.0)]),
    s("lean caps: the cold edge stops at -0.4; the warm edge is uncapped", [
        conditions({"lean": 0.9, "cold_edge": -0.4, "warm_edge": 1.9},
                   comfort=24.0, mean=15.0, max=17.5)]),
    s("lean: k is outdoor.lean_per_degree", [
        conditions({"lean": 0.4, "cold_edge": -0.6, "warm_edge": 1.4},
                   comfort=24.0, mean=22.0, max=23.0, k=0.2)]),
    s("lean: delta +6 rests on readings half a band cold", [
        votes("near_cold", {"met": False}),
        conditions({"cold_edge": -0.4}, comfort=24.0, mean=18.0, max=20.0),
        votes("near_cold", {"met": True, "tally": 2, "direction": "too_cold"}),
        step("near_cold", {"state": "rest"}, OFF)],
      unit={"since": -m(10)}),
    s("lean: delta -6 boosts on readings half a band warm", [
        conditions({"warm_edge": 0.4}, comfort=24.0, mean=30.0, max=31.0),
        step("near", {"state": "boost"}, cmd("cool", "medium", 20.0))],
      unit={"since": -m(10)}),
    s("block: delta max at the margin blocks; just under it doesn't", [
        conditions({"delta_max": 6.0, "would_block": True, "blocked": True},
                   comfort=24.0, mean=19.0, max=18.0, block_mode="active"),
        conditions({"delta_max": 5.9, "would_block": False, "blocked": False},
                   comfort=24.0, mean=19.0, max=18.1, block_mode="active")]),
    s("block: the margin is the room's", [
        conditions({"would_block": False}, comfort=24.0, mean=19.0, max=18.0, margin=6.5),
        conditions({"would_block": True}, comfort=24.0, mean=19.0, max=18.0, margin=5.5)]),
    s("block at margin -1: a max up to comfort + 1 blocks; above it doesn't", [
        conditions({"delta_max": -1.0, "would_block": True, "margin": -1.0},
                   comfort=24.0, mean=23.0, max=25.0, margin=-1.0),
        conditions({"delta_max": -1.1, "would_block": False},
                   comfort=24.0, mean=23.0, max=25.1, margin=-1.0)]),
    s("block hysteresis at margin -1: held until the max reaches comfort + 2", [
        conditions({"delta_max": -1.9, "would_block": True},
                   comfort=24.0, mean=23.0, max=25.9, margin=-1.0, was_blocked=True),
        conditions({"delta_max": -2.0, "would_block": False},
                   comfort=24.0, mean=23.0, max=26.0, margin=-1.0, was_blocked=True)]),
    s("block hysteresis: blocked last section, it holds until delta max <= margin - 1", [
        conditions({"would_block": True}, comfort=24.0, mean=20.0, max=18.5, was_blocked=True),
        conditions({"would_block": True}, comfort=24.0, mean=20.0, max=18.99, was_blocked=True),
        conditions({"would_block": False}, comfort=24.0, mean=20.0, max=19.0, was_blocked=True),
        conditions({"would_block": False}, comfort=24.0, mean=20.0, max=18.5)]),
    s("block active: the scene rests; one warm sensor, short of the quorum, doesn't wake it", [
        conditions({"blocked": True}, comfort=24.0, mean=17.0, max=17.0, block_mode="active"),
        section({"state": "rest"}, OFF),
        step("warm_one", {"state": "rest"}, None, at=m(20), overruled=False)]),
    s("block shadow: decided and kept, but control cools as if unblocked", [
        conditions({"would_block": True, "blocked": False}, comfort=24.0, mean=17.0,
                   max=17.0),
        section({"state": "cool"}, cmd("cool", "low", 22.0)),
        step("very_hot", {"state": "boost"}, cmd("cool", "medium", 20.0), at=m(10))]),
    s("block off: never blocked, whatever the forecast", [
        conditions({"would_block": False, "blocked": False, "cold_edge": -0.4},
                   comfort=24.0, mean=10.0, max=10.0, block_mode="off", was_blocked=True),
        section({"state": "cool"}, cmd("cool", "low", 22.0))]),
    s("block active: a too_hot vote unblocks the unit, so its boost isn't rested", [
        conditions({"blocked": True}, comfort=24.0, mean=17.0, max=17.0, block_mode="active"),
        section({"state": "rest", "unblocked": False}, OFF),
        press("too_hot", {"state": "boost", "forced": True, "unblocked": True, "base": 21.0},
              cmd("cool", "medium", 19.0), at=m(5)),
        step("hot", {"state": "boost", "unblocked": True}, None, at=m(15)),
        step("settled_cool", {"state": "cool", "offset": 0, "unblocked": True},
             cmd("cool", "low", 21.0), at=m(25)),
        step("very_hot", {"state": "boost"}, cmd("cool", "medium", 19.0), at=m(35))]),
    s("block active: a too_cold vote leaves the block as it is", [
        conditions({"blocked": True}, comfort=24.0, mean=17.0, max=17.0, block_mode="active"),
        press("too_cold", {"state": "rest", "forced": True, "unblocked": False}, OFF)],
      unit={"since": -m(10)}),
    s("block shadow: a too_hot vote doesn't unblock (nothing is blocked)", [
        conditions({"would_block": True, "blocked": False}, comfort=24.0, mean=17.0,
                   max=17.0),
        press("too_hot", {"state": "boost", "forced": True, "unblocked": False},
              cmd("cool", "medium", 19.0))],
      unit={"since": -m(10)}),
    s("heat source: on, it blocks in shadow mode, whatever the forecast", [
        conditions({"blocked": False}, comfort=24.0, mean=30.0, max=31.0),
        section({"state": "rest"}, OFF),
        step("hot", {"state": "rest"}, None, at=m(20))],
      ctx={"heat_source_on": True}),
    s("heat source: on, it blocks in active mode too", [
        step("settled_warm", {"state": "rest"}, OFF)],
      unit={"since": -m(10)}, ctx={"heat_source_on": True, "block_mode": "active"}),
    s("heat source: block_mode off ignores it", [
        conditions({"blocked": False}, comfort=24.0, mean=30.0, max=31.0, block_mode="off"),
        section({"state": "cool"}, cmd("cool", "low", 22.0)),
        step("hot", {"state": "boost"}, cmd("cool", "medium", 20.0), at=m(10))],
      ctx={"heat_source_on": True}),
    s("heat source: a too_hot vote unblocks against it", [
        press("too_hot", {"state": "boost", "forced": True, "unblocked": True},
              cmd("cool", "medium", 19.0)),
        step("hot", {"state": "boost"}, None, at=m(10))],
      unit={"state": "rest", "since": -m(20)}, ctx={"heat_source_on": True}),
    s("fail open: no outdoor value leans nothing and blocks nothing", [
        conditions({"fail_open": "no_outdoor", "cold_edge": -1.0, "warm_edge": 1.0,
                    "would_block": False, "blocked": False, "lean": None},
                   comfort=24.0, mean=None, max=None, block_mode="active", was_blocked=True),
        section({"state": "cool"}, cmd("cool", "low", 22.0)),
        step("near", {"state": "cool"}, None, at=m(10))]),
    s("fail open: no comfort point leans nothing and blocks nothing", [
        conditions({"fail_open": "no_comfort_point", "comfort_point": None, "cold_edge": -1.0,
                    "warm_edge": 1.0, "blocked": False},
                   readings="dead", mean=10.0, max=10.0, block_mode="active"),
        section({"state": "cool"}, cmd("cool", "low", 22.0))]),

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

    # --- the room overrules the block (decision 143) ------------------------
    # BLOCK: an active block from a cold forecast (delta_mean 7, so the warm
    # edge leans to +1.7); "hot" is past +1 on two sensors but not past +1.7.
    s(OVERRULE_WAKES, [
        BLOCK,
        section({"state": "rest", "unblocked": False}, OFF),
        step("hot", {"state": "cool", "offset": 0, "base": 22.0, "unblocked": True,
                     "since": m(20), "forced": False}, cmd("cool", "low", 22.0), at=m(20),
             overruled=True, peers={"v": {"state": "cool", "unblocked": True}},
             peer_commands={"v": cmd("cool", "low", 22.0)}),
        step("hot", {"state": "cool", "unblocked": True}, None, at=m(30), overruled=False),
        step("very_hot", {"state": "boost", "unblocked": True}, cmd("cool", "medium", 20.0),
             at=m(40), overruled=False)],
      peers={"v": {}}),
    s(OVERRULE_REST_MIN, [
        BLOCK,
        section({"state": "rest"}, OFF),
        step("hot", {"state": "rest", "unblocked": False}, None, at=m(10), overruled=False),
        step("hot", {"state": "rest", "unblocked": False}, None, at=m(14, 50), overruled=False),
        step("hot", {"state": "cool", "offset": 0, "since": m(15), "unblocked": True},
             cmd("cool", "low", 22.0), at=m(15), overruled=True)]),
    s(OVERRULE_EVERY_REST, [
        step("hot", {"state": "rest"}, None, overruled=False,
             peers={"v": {"state": "rest", "unblocked": False}}),
        step("hot", {"state": "cool", "offset": 0, "unblocked": True}, cmd("cool", "low", 22.0),
             at=m(10), overruled=True, peers={"v": {"state": "cool", "unblocked": True}},
             peer_commands={"v": cmd("cool", "low", 22.0)})],
      unit={"state": "rest", "since": -m(30)}, peers={"v": {"state": "rest", "since": -m(5)}},
      ctx={"blocked": True}),
    s(OVERRULE_NOT_HEAT, [
        BLOCK,
        section({"state": "rest"}, OFF),
        step("very_hot", {"state": "rest", "unblocked": False}, None, at=m(30),
             overruled=False)],
      ctx={"heat_source_on": True}),
    s(OVERRULE_PLAIN_EDGE, [
        conditions({"blocked": True, "warm_edge": 0.9}, comfort=24.0, mean=25.0, max=25.0,
                   margin=-1.0, block_mode="active"),
        section({"state": "rest"}, OFF),
        step("just_warm", {"state": "rest"}, None, at=m(20), overruled=False),
        step("hot", {"state": "cool", "offset": 0, "unblocked": True},
             cmd("cool", "low", 22.0), at=m(30), overruled=True)]),
    s(OVERRULE_TOO_WARM, [
        step("cold", {"state": "rest", "unblocked": False}, None, overruled=False)],
      unit={"state": "rest", "since": -m(30)}, ctx={"blocked": True}),
    s(OVERRULE_AT_BASE, [
        step("very_hot", {"state": "cool", "offset": 0, "base": 23.0, "unblocked": True},
             cmd("cool", "low", 23.0), overruled=True)],
      unit={"state": "rest", "offset": -1, "base": 23.0, "since": -m(30)},
      ctx={"blocked": True}),
    s(OVERRULE_NOT_HELD, [
        step("hot", {"state": "cool", "offset": 1}, cmd("cool", "low", 23.0), overruled=False)],
      unit={"state": "rest", "unblocked": True, "since": -m(30)}, ctx={"blocked": True}),
    s(OVERRULE_LIFTS_ALL, [
        BLOCK,
        step("hot", {"state": "cool", "unblocked": True}, cmd("cool", "low", 22.0),
             overruled=True, peers={"v": {"state": "user_off", "unblocked": True}},
             peer_commands={"v": None}),
        step("very_hot", {"state": "boost"}, cmd("cool", "medium", 20.0), at=m(10),
             peers={"v": {"state": "cool", "unblocked": True}},
             peer_commands={"v": cmd("cool", "low", 22.0)})],
      unit={"state": "rest", "since": -m(30)}, peers={"v": {"state": "user_off", "since": -m(60)}}),
    s(OVERRULE_FORCED_REST, [
        BLOCK,
        section({"state": "rest"}, OFF),
        press("too_cold", {"state": "rest", "forced": True, "base": 23.0, "since": 0}, None,
              at=m(5)),
        step("hot", {"state": "cool", "offset": 0, "base": 23.0, "forced": False,
                     "unblocked": True}, cmd("cool", "low", 23.0), at=m(20), overruled=True)]),
    s("a block a too_hot vote has lifted for every unit has nothing left to overrule", [
        BLOCK,
        section({"state": "rest"}, OFF),
        press("too_hot", {"state": "boost", "forced": True, "unblocked": True},
              cmd("cool", "medium", 19.0), at=m(5)),
        step("hot", {"state": "boost"}, None, at=m(20), overruled=False)]),
    s("shadow: control sees no block, so there is none to overrule", [
        conditions({"would_block": True, "blocked": False}, comfort=24.0, mean=17.0, max=17.0),
        section({"state": "cool"}, cmd("cool", "low", 22.0)),
        step("hot", {"state": "cool", "unblocked": False}, None, at=m(20), overruled=False)]),

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
    s("leak release in a cold room with a cooler forecast: a section rest", [
        conditions({"delta_mean": 6.0}, comfort=24.0, mean=18.0, max=19.0),
        step("cold", {"state": "rest", "section_rest": True, "since": 0}, OFF)],
      unit={"state": "leak", "since": -m(10)}),
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
    s("a person's off stays off without a too-warm quorum", [
        step("settled_warm", {"state": "user_off"}, None),
        step("cold", {"state": "user_off"}, None, at=m(10)),
        step("dead", {"state": "user_off"}, None, at=m(20))],
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

    # --- section start: a cold room with a cooler forecast starts resting -------
    # Comfort 24, forecast mean 18 (delta_mean +6, max 19: no block).
    s("section start: a cold room with a cooler forecast rests, and wakes at its base, "
      "offset 0", [
        conditions({"delta_mean": 6.0, "would_block": False, "blocked": False},
                   comfort=24.0, mean=18.0, max=19.0),
        section({"state": "rest", "section_rest": True, "offset": 0, "base": 22.0,
                 "since": 0}, OFF, readings="cold"),
        step("cold", {"state": "rest", "section_rest": True}, None, at=m(10)),
        step("settled_warm", {"state": "cool", "offset": 0, "section_rest": False,
                              "since": m(20)}, cmd("cool", "low", 22.0), at=m(20))],
      unit={"offset": 1, "since": -m(60)}),
    s("section start: a quorum just under comfort, voting or not, is a cold room", [
        conditions({"delta_mean": 6.0}, comfort=24.0, mean=18.0, max=19.0),
        section({"state": "rest", "section_rest": True}, OFF, readings="settled_cool")]),
    s("section start: a cold room with a forecast mean at or above comfort starts in cool", [
        conditions({"delta_mean": -3.0}, comfort=24.0, mean=27.0, max=29.0),
        section({"state": "cool", "section_rest": False}, cmd("cool", "low", 22.0),
                readings="cold"),
        conditions({"delta_mean": 0.0}, comfort=24.0, mean=24.0, max=26.0),
        section({"state": "cool", "section_rest": False}, cmd("cool", "low", 22.0),
                readings="cold", at=m(10))]),
    s("section start: a warm room with a cooler forecast starts in cool, ending a section "
      "rest", [
        conditions({"delta_mean": 6.0}, comfort=24.0, mean=18.0, max=19.0),
        section({"state": "cool", "section_rest": False}, cmd("cool", "low", 22.0),
                readings="settled_warm")],
      unit={"state": "rest", "section_rest": True, "since": -m(5)}),
    s("section start: no conditions, no outdoor value or no comfort point starts a cold room "
      "in cool (fail open)", [
        section({"state": "cool", "section_rest": False}, cmd("cool", "low", 22.0),
                readings="cold"),
        conditions({"fail_open": "no_outdoor", "delta_mean": None}, comfort=24.0, mean=None,
                   max=None),
        section({"state": "cool", "section_rest": False}, cmd("cool", "low", 22.0),
                readings="cold", at=m(10)),
        conditions({"fail_open": "no_comfort_point", "delta_mean": None}, comfort=None,
                   mean=18.0, max=19.0),
        section({"state": "cool", "section_rest": False}, cmd("cool", "low", 22.0),
                readings="cold", at=m(20))]),
    s("section start: blocked, a cold room rests for the block, not as a section rest", [
        conditions({"blocked": True, "delta_mean": 7.0}, comfort=24.0, mean=17.0, max=17.0,
                   block_mode="active"),
        section({"state": "rest", "section_rest": False}, OFF, readings="cold")]),
    s("section start: a heat source's rest wakes at +1 once it is off, as before", [
        conditions({"delta_mean": 6.0}, comfort=24.0, mean=18.0, max=19.0),
        section({"state": "rest", "section_rest": False}, OFF, readings="cold"),
        step("settled_warm", {"state": "cool", "offset": 1}, cmd("cool", "low", 23.0),
             at=m(20), ctx={"heat_source_on": False})],
      ctx={"heat_source_on": True}),
    s("section start: a too_hot vote in a section rest is taken: a forced boost, then cool "
      "at the voted base", [
        conditions({"delta_mean": 6.0}, comfort=24.0, mean=18.0, max=19.0),
        section({"state": "rest", "section_rest": True}, OFF, readings="cold"),
        press("too_hot", {"state": "boost", "forced": True, "section_rest": False,
                          "base": 21.0, "offset": 0, "since": m(5)},
              cmd("cool", "medium", 19.0), at=m(5)),
        step("settled_cool", {"state": "cool", "offset": 0, "forced": False, "base": 21.0},
             cmd("cool", "low", 21.0), at=m(15))]),
    s("section start: a too_cold vote in a section rest is taken: forced, since kept, "
      "nothing sent", [
        conditions({"delta_mean": 6.0}, comfort=24.0, mean=18.0, max=19.0),
        section({"state": "rest", "section_rest": True}, OFF, readings="cold"),
        press("too_cold", {"state": "rest", "forced": True, "section_rest": False,
                           "base": 23.0, "since": 0}, None, at=m(5)),
        step("settled_warm", {"state": "cool", "offset": 0, "base": 23.0},
             cmd("cool", "low", 23.0), at=m(20))]),
    s("section start: a person's on ends a section rest", [
        conditions({"delta_mean": 6.0}, comfort=24.0, mean=18.0, max=19.0),
        section({"state": "rest", "section_rest": True}, OFF, readings="cold"),
        person("on", {"state": "cool", "section_rest": False}, setpoint=23.0, at=m(5))]),

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

    # --- a person's off lasts until a person or the room says otherwise ---------------
    # (decision 139: a section change keeps it; a person's on, a too_hot vote or
    # a too-warm quorum ends it; a leak or a forced-off section still wins)
    s("a person's off survives a section change: offset and base reset, since kept, nothing "
      "sent", [
        section({"state": "user_off", "offset": 0, "base": 22.0, "since": -m(21),
                 "unblocked": False, "grace_until": None, "forced": False,
                 "section_rest": False}, None)],
      unit={"state": "user_off", "offset": 1, "base": 24.0, "since": -m(21), "unblocked": True}),
    s("a person's off lasts past the section change until the room is too warm: then cool at "
      "the base, offset 0", [
        section({"state": "user_off", "base": 22.0, "offset": 0}, None),
        step("settled_warm", {"state": "user_off"}, None, at=m(10)),
        step("hot", {"state": "cool", "offset": 0, "base": 22.0, "since": m(20)},
             cmd("cool", "low", 22.0), at=m(20)),
        step("hot", {"state": "boost"}, cmd("cool", "medium", 20.0), at=m(30))],
      unit={"state": "user_off", "offset": -1, "since": -m(21)}),
    s("a too-warm quorum wakes a person's off at its base, dropping the section offset", [
        step("hot", {"state": "cool", "offset": 0, "base": 24.0, "since": 0, "forced": False},
             cmd("cool", "low", 24.0))],
      unit={"state": "user_off", "offset": -2, "base": 24.0, "since": -m(30)}),
    s("the wake counts against the section's leaned warm edge", [
        conditions({"warm_edge": 1.6}, comfort=24.0, mean=18.0, max=19.0),
        step("hot", {"state": "user_off"}, None),
        conditions({"warm_edge": 0.4}, comfort=24.0, mean=30.0, max=31.0),
        step("near", {"state": "cool", "offset": 0}, cmd("cool", "low", 22.0), at=m(10))],
      unit={"state": "user_off", "since": -m(30)}),
    s("a too-warm quorum doesn't wake a person's off before the rest minimum time", [
        step("hot", {"state": "user_off"}, None),
        step("hot", {"state": "cool", "since": m(5)}, cmd("cool", "low", 22.0), at=m(5))],
      unit={"state": "user_off", "since": -m(10)}),
    s("a too-warm quorum doesn't wake a person's off while cooling is blocked", [
        step("very_hot", {"state": "user_off"}, None)],
      unit={"state": "user_off", "since": -m(30)}, ctx={"blocked": True}),
    s("a person's off that a person unblocked wakes while cooling is blocked", [
        step("hot", {"state": "cool", "unblocked": True}, cmd("cool", "low", 22.0))],
      unit={"state": "user_off", "unblocked": True, "since": -m(30)}, ctx={"blocked": True}),
    s("no seed: a too-warm quorum leaves a person's off alone", [
        step("hot", {"state": "user_off"}, None)],
      unit={"state": "user_off", "since": -m(30)}, ctx={"setpoint_applied": None}),
    s("user_off_wakes_on_warm false: a person's off waits for a person, however warm the room", [
        section({"state": "user_off"}, None),
        step("very_hot", {"state": "user_off"}, None, at=m(20)),
        step("very_hot", {"state": "user_off"}, None, at=m(30)),
        press("too_hot", {"state": "boost", "forced": True, "base": 21.0},
              cmd("cool", "medium", 19.0), at=m(35))],
      unit={"state": "user_off", "since": -m(30)},
      ctx={"control": {"user_off_wakes_on_warm": False}}),
    s("a person's on ends a person's off kept through a section change", [
        section({"state": "user_off"}, None),
        person("on", {"state": "cool", "base": 23.0, "offset": 0, "unblocked": True,
                      "since": m(5), "grace_until": m(35)}, setpoint=23.0, at=m(5))],
      unit={"state": "user_off", "since": -m(21)}),
    s("a too_hot vote ends a person's off kept through a section change: a forced boost", [
        section({"state": "user_off"}, None),
        press("too_hot", {"state": "boost", "forced": True, "base": 21.0, "offset": 0,
                          "since": m(5)}, cmd("cool", "medium", 19.0), at=m(5))],
      unit={"state": "user_off", "since": -m(21)}),
    s("a leak during a person's off: the leak wins, and its release starts the unit as at a "
      "section change", [
        step("cold", {"state": "leak", "since": m(5)}, OFF, at=m(5), ctx={"leak_on": True}),
        step("hot", {"state": "leak"}, None, at=m(15), ctx={"leak_on": True}),
        step("settled_warm", {"state": "cool", "offset": 0, "base": 22.0, "since": m(25)},
             cmd("cool", "low", 22.0), at=m(25))],
      unit={"state": "user_off", "since": -m(21)}),
    s("a section change with the leak latched takes a person's off into leak", [
        section({"state": "leak"}, OFF)],
      unit={"state": "user_off", "since": -m(21)}, ctx={"leak_on": True}),
    s("a forced-off section takes a person's off into config_off; the next section starts it as "
      "usual", [
        section({"state": "config_off"}, OFF, forced_off=True),
        section({"state": "cool"}, cmd("cool", "low", 22.0), at=m(10))],
      unit={"state": "user_off", "since": -m(21)}),

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
                        unblocked=d.get("unblocked", False), forced=d.get("forced", False),
                        section_rest=d.get("section_rest", False))


def _view(unit: control.Unit) -> dict:
    return {"state": unit.state, "offset": unit.offset, "base": unit.base or None,
            "since": _secs(unit.since), "grace_until": _secs(unit.grace_until),
            "unblocked": unit.unblocked, "forced": unit.forced,
            "section_rest": unit.section_rest}


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


def _fresh(st: dict, now: datetime, stale_minutes: int) -> dict:
    readings = st.get("readings", "settled_warm")
    readings = READINGS[readings] if isinstance(readings, str) else readings
    reported = {sid: now - timedelta(seconds=ago) for sid, ago in st.get("reported", {}).items()}
    return control.fresh_readings(readings, reported, now, stale_minutes)


def _votes(st: dict, now: datetime = T0,
           stale_minutes: int = control.DEFAULT_STALE_MINUTES) -> control.Votes:
    return control.evaluate_votes(_fresh(st, now, stale_minutes), st.get("almanac", ALMANAC),
                                  *st.get("edges", (-1.0, 1.0)))


def _conditions(st: dict, now: datetime = T0,
                stale_minutes: int = control.DEFAULT_STALE_MINUTES) -> control.Conditions:
    """A conditions step's control.Conditions."""
    i = {**COND_DEFAULTS, **st["inputs"]}
    comfort = i["comfort"] if "comfort" in i else control.comfort_point(
        i.get("almanac", ALMANAC), _fresh(i, now, stale_minutes))
    return control.section_conditions(SECTION, comfort, i.get("mean"), i.get("max"),
                                      i["margin"], i["was_blocked"], i["block_mode"], i["k"])


def under(cond: control.Conditions | None) -> dict:
    """The context the steps after a conditions step run under: the block
    and block_mode as control sees them, and delta_mean (a section start's)."""
    return {} if cond is None else {"blocked": cond.blocked, "block_mode": cond.block_mode,
                                    "delta_mean": cond.delta_mean}


def with_edges(st: dict, cond: control.Conditions | None) -> dict:
    """A step voting against the conditions' leaned edges, unless it gives
    its own."""
    if cond is None or "edges" in st:
        return st
    return {**st, "edges": [cond.cold_edge, cond.warm_edge]}


def conditions_differ(st: dict, cond: control.Conditions) -> str | None:
    got = {k: getattr(cond, k) for k in st["expect"]}
    return None if got == st["expect"] else f"conditions {got}, expected {st['expect']}"


def plain_votes(st: dict, now: datetime = T0,
                stale_minutes: int = control.DEFAULT_STALE_MINUTES) -> control.Votes:
    """A heartbeat's votes against the un-leaned edges, whatever the
    conditions lean: what the room overrules a block on."""
    return _votes({**st, "edges": control.PLAIN_EDGES}, now, stale_minutes)


def apply(st: dict, units: dict[str, control.Unit], now: datetime, ctx: control.Context
          ) -> tuple[dict[str, control.Unit], dict[str, control.Command | None] | None,
                     control.PersonVote | None, bool]:
    """One step on the room through app.control: the units after it, each
    unit's command (None for a person's on/off/manual, which sends
    nothing), the vote, if the step was one, and whether a heartbeat's room
    overruled the block."""
    ctxs = _ctxs(ctx, units)
    do, results, pv, overruled = st["do"], None, None, False
    if do == "step":
        v = _votes(st, now, ctx.sensor_stale_minutes)
        overruled, results = control.on_heartbeat(
            units, v, plain_votes(st, now, ctx.sensor_stale_minutes), now, ctxs)
    elif do == "section_change":
        forced = st.get("forced_off", ctx.forced_off)
        v = _votes(st, now, ctx.sensor_stale_minutes)
        results = {uid: control.on_section_change(u, ctxs[uid], forced and uid == MAIN, now, v)
                   for uid, u in units.items()}
    elif do == "person":
        new = control.on_person_change(st["kind"], units[MAIN], st.get("setpoint"), now, ctx)
        return {**units, MAIN: new}, None, None, False
    elif do == "press":
        results, pv = control.on_button(st["direction"], units, now, ctxs)
    elif do == "change":
        results, pv = control.on_climate_change(units, MAIN, control.Climate(**st["before"]),
                                                control.Climate(**st["after"]), now, ctxs)
    else:
        raise AssertionError(do)
    return ({uid: r[0] for uid, r in results.items()},
            {uid: r[1] for uid, r in results.items()}, pv, overruled)


def _command(command: control.Command | None) -> dict | None:
    return None if command is None else command.__dict__


def compare(expect: dict, units: dict[str, control.Unit], commands: dict | None,
            pv: control.PersonVote | None, view=_view, overruled: bool = False) -> str | None:
    """A step's expectations against the room after it: the unit's fields
    and command, the peers', the vote (accepted, or for a climate change
    its direction and acceptance, or None for none) and a heartbeat's
    overrule."""
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
    if "overruled" in expect and overruled != expect["overruled"]:
        return f"overruled {overruled}, expected {expect['overruled']}"
    return None


def run(scenario: dict) -> list[str]:
    """The scenario's failures (empty when it passes)."""
    try:
        units, cond = _room(scenario), None
        for i, st in enumerate(scenario["steps"], 1):
            now, expect = _time(st.get("at", 0)), st["expect"]
            if st["do"] == "conditions":
                cond = _conditions(st, now, _ctx(scenario["ctx"]).sensor_stale_minutes)
                why = conditions_differ(st, cond)
                if why:
                    return [f"step {i}: {why}"]
                continue
            ctx = _ctx(scenario["ctx"], under(cond), st.get("ctx"))
            st = with_edges(st, cond)
            if st["do"] == "votes":
                v = _votes(st, now, ctx.sensor_stale_minutes)
                got = {k: getattr(v, k) for k in expect}
                if got != expect:
                    return [f"step {i}: votes {got}, expected {expect}"]
                continue
            units, commands, pv, overruled = apply(st, units, now, ctx)
            why = compare(expect, units, commands, pv, overruled=overruled)
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
_PERSON_VOTE = control.on_person_vote
_COMFORT_POINT = control.comfort_point
_SECTION_CONDITIONS = control.section_conditions
_SECTION_START = control.section_start
_GO = control._go
_ON_HEARTBEAT = control.on_heartbeat
_OVERRULED_STEP = control.overruled_step
_RESTS_FOR_BLOCK = control.rests_for_block


def _mutants() -> list[tuple[str, dict, list[str]]]:
    c = control
    leak_step = c._leak_step

    def leak_waits(unit, votes, now, ctx):       # leak queued behind hands-off, grace, min time
        if ctx.leak_on and unit.state != "leak" and (
                unit.state in (*c.HANDS_OFF, "user_off") or c.in_grace(unit, now)
                or not c.min_time_met(unit, now, ctx.config.cool_min_heartbeats,
                                      ctx.heartbeat_interval)):
            return None
        return leak_step(unit, votes, now, ctx)

    def cold_room(votes):                        # a rest started now wouldn't release at once
        return votes is not None and not c.released(votes.cold_side, votes.required)

    def cooler(ctx):
        return ctx.delta_mean is not None and ctx.delta_mean > 0

    def resend(unit, ctx):                       # re-send the state's command every heartbeat
        base = c.target_base(unit, ctx)
        return unit, (c.command_for(unit.state, base, unit.offset, ctx)
                      if base is not None else None)

    def wakes(config=True, warm=True, block=True, min_time=True):
        """control.user_off_wakes with one of its conditions taken out."""
        def rule(unit, votes, now, ctx):
            cfg = ctx.config
            return ((not config or cfg.user_off_wakes_on_warm) and votes.met
                    and (not warm or votes.direction == c.TOO_WARM)
                    and (not block or not c.effective_blocked(unit, ctx))
                    and (not min_time or c.min_time_met(unit, now, cfg.rest_min_heartbeats,
                                                        ctx.heartbeat_interval)))
        return rule

    def section_change_then(fix):                # the scene, then `fix` on a kept user_off
        def rule(unit, ctx, forced_off, now, votes):
            new, command = _SECTION_CHANGE(unit, ctx, forced_off, now, votes)
            return fix(new, command, now) if new.state == "user_off" else (new, command)
        return rule

    def kept_first(unit, ctx, forced_off, now, votes):   # a person's off ahead of leak, config_off
        if unit.state == "user_off":
            return dataclasses.replace(unit, offset=0, base=ctx.setpoint_applied, unblocked=False,
                                       grace_until=None, forced=False, section_rest=False), None
        return _SECTION_CHANGE(unit, ctx, forced_off, now, votes)

    def overrules(min_time=True, every=True):
        """control.overrules_block without its minimum time, or with any
        resting unit's minimum time instead of every one's."""
        def rule(units, plain, now, ctxs):
            resting = [(u, ctxs[uid]) for uid, u in units.items()
                       if c.rests_for_block(u, ctxs[uid])]
            met = [c.min_time_met(u, now, x.config.rest_min_heartbeats, x.heartbeat_interval)
                   for u, x in resting]
            return (bool(resting) and c.room_overrules(plain, resting[0][1])
                    and (not min_time or (all(met) if every else any(met))))
        return rule

    def overrule_wake(state="cool", offset=0, lift_all=True):
        """control.overruled_step waking into another state or keeping the
        offset (None), or lifting the block only for the units it wakes."""
        def rule(unit, votes, now, ctx):
            if c.rests_for_block(unit, ctx):
                return c._go(c.lift_block(unit), state, now, ctx, offset=offset)
            return c.step(c.lift_block(unit) if lift_all else unit, votes, now, ctx)
        return rule

    kept = ["a person's off survives a section change: offset and base reset, since kept, "
            "nothing sent"]
    woke = ["a person's off lasts past the section change until the room is too warm: then cool "
            "at the base, offset 0",
            "a too-warm quorum wakes a person's off at its base, dropping the section offset",
            "the wake counts against the section's leaned warm edge",
            "a too-warm quorum doesn't wake a person's off before the rest minimum time",
            "a person's off that a person unblocked wakes while cooling is blocked"]

    return [
        ("the room overrules the block on a too-warm quorum",
         {"overrules_block": lambda *a: False},
         [OVERRULE_WAKES, OVERRULE_REST_MIN, OVERRULE_EVERY_REST, OVERRULE_PLAIN_EDGE,
          OVERRULE_AT_BASE, OVERRULE_LIFTS_ALL, OVERRULE_FORCED_REST]),
        ("the overrule waits out the rest minimum time", {"overrules_block": overrules(False)},
         [OVERRULE_REST_MIN, OVERRULE_EVERY_REST]),
        ("every unit resting for the block has its rest minimum first (any one's instead)",
         {"overrules_block": overrules(every=False)}, [OVERRULE_EVERY_REST]),
        ("a heat source's block isn't overruled",
         {"room_overrules": lambda plain, ctx: ctx.blocked and plain.met
          and plain.direction == c.TOO_WARM}, [OVERRULE_NOT_HEAT]),
        ("only a too-warm quorum overrules (any quorum instead)",
         {"room_overrules": lambda plain, ctx: ctx.blocked and not c.heat_blocks(ctx)
          and plain.met}, [OVERRULE_TOO_WARM]),
        ("the overrule counts the un-leaned edges (the section's leaned ones instead)",
         {"on_heartbeat": lambda units, votes, plain, now, ctxs: _ON_HEARTBEAT(
             units, votes, votes, now, ctxs)}, [OVERRULE_WAKES, OVERRULE_PLAIN_EDGE]),
        ("the overrule lifts the block for every unit (only the units it wakes instead)",
         {"overruled_step": overrule_wake(lift_all=False)}, [OVERRULE_LIFTS_ALL]),
        ("the overrule wakes at the base, offset 0 (the offset kept instead)",
         {"overruled_step": overrule_wake(offset=None)}, [OVERRULE_AT_BASE]),
        ("the overrule wakes into cool, not boost", {"overruled_step": overrule_wake("boost")},
         [OVERRULE_WAKES, OVERRULE_AT_BASE]),
        ("a rest the block no longer holds isn't the overrule's to wake",
         {"rests_for_block": lambda unit, ctx: unit.state == "rest" and c.room_blocked(ctx)
          and not ctx.leak_on and c.target_base(unit, ctx) is not None}, [OVERRULE_NOT_HELD]),
        ("a forced rest is the block's to wake too (left resting instead)",
         {"rests_for_block": lambda unit, ctx: _RESTS_FOR_BLOCK(unit, ctx) and not unit.forced},
         [OVERRULE_FORCED_REST]),
        ("a person's off survives a section change", {"keeps_user_off": lambda unit: False},
         kept + [woke[0],
                 "user_off_wakes_on_warm false: a person's off waits for a person, however warm "
                 "the room",
                 "a person's on ends a person's off kept through a section change",
                 "a too_hot vote ends a person's off kept through a section change: a forced "
                 "boost"]),
        ("a kept person's off keeps its since (since = now instead)",
         {"on_section_change": section_change_then(
             lambda new, command, now: (dataclasses.replace(new, since=now), command))}, kept),
        ("a kept person's off is sent nothing (off re-sent instead)",
         {"on_section_change": section_change_then(
             lambda new, command, now: (new, c.Command("off")))}, kept),
        ("a leak and a forced-off section beat a kept person's off",
         {"on_section_change": kept_first},
         ["a section change with the leak latched takes a person's off into leak",
          "a forced-off section takes a person's off into config_off; the next section starts it "
          "as usual"]),
        ("a too-warm quorum wakes a person's off", {"user_off_wakes": lambda *a: False}, woke),
        ("the wake is system.control.user_off_wakes_on_warm's (always on instead)",
         {"user_off_wakes": wakes(config=False)},
         ["user_off_wakes_on_warm false: a person's off waits for a person, however warm the "
          "room"]),
        ("only a too-warm quorum wakes a person's off (any quorum instead)",
         {"user_off_wakes": wakes(warm=False)},
         ["a person's off stays off without a too-warm quorum"]),
        ("the wake waits out the rest minimum time", {"user_off_wakes": wakes(min_time=False)},
         ["a too-warm quorum doesn't wake a person's off before the rest minimum time"]),
        ("a block holds a person's off", {"user_off_wakes": wakes(block=False)},
         ["a too-warm quorum doesn't wake a person's off while cooling is blocked"]),
        ("the wake cools at the base, offset 0 (the offset kept instead)",
         {"_go": lambda unit, state, now, ctx, offset=None: _GO(
             unit, state, now, ctx, None if unit.state == "user_off" else offset)},
         ["a too-warm quorum wakes a person's off at its base, dropping the section offset"]),
        ("minimum time", {"min_time_met": lambda unit, now, n, interval: True},
         ["cool: minimum time not met", "cool: 4:59 is short of half a heartbeat",
          "rest: 14:50 is short of 1.5 heartbeats",
          "a too-warm quorum doesn't wake a person's off before the rest minimum time"]),
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
          "blocked: a rest doesn't wake", "blocked: a boost goes to rest",
          "a too-warm quorum doesn't wake a person's off while cooling is blocked"]),
        ("per-unit unblock", {"effective_blocked": lambda unit, ctx: c.room_blocked(ctx)},
         ["unblocked by a person's on, for that unit",
          "a person's off that a person unblocked wakes while cooling is blocked",
          "block active: a too_hot vote unblocks the unit, so its boost isn't rested",
          "heat source: a too_hot vote unblocks against it"]),
        ("the lean caps", {"lean_edges": lambda lean: (-1.0, 1.0) if lean is None
                           else (round(-1.0 + lean, 3), round(1.0 + lean, 3))},
         ["lean caps: the hot side stops at -0.6 on both edges",
          "lean caps: the cold edge stops at -0.4; the warm edge is uncapped",
          "lean table: delta +12 (cold) caps the cold edge, not the warm"]),
        ("the lean (edges always -1, +1)", {"lean_edges": lambda lean: (-1.0, 1.0)},
         ["lean table: delta -6 (hot outside) cools early, rests late",
          "lean table: delta +6 (cool outside) rests early, cools late",
          "lean: delta +6 rests on readings half a band cold",
          "lean: delta -6 boosts on readings half a band warm",
          "the wake counts against the section's leaned warm edge"]),
        ("the block's hysteresis",
         {"decide_block": lambda delta_max, margin, was_blocked:
          delta_max is not None and delta_max >= margin},
         ["block hysteresis: blocked last section, it holds until delta max <= margin - 1",
          "block hysteresis at margin -1: held until the max reaches comfort + 2"]),
        ("the block at the margin itself (> instead of >=)",
         {"decide_block": lambda delta_max, margin, was_blocked:
          delta_max is not None and (delta_max > margin - 1 if was_blocked
                                     else delta_max > margin)},
         ["block: delta max at the margin blocks; just under it doesn't",
          "block at margin -1: a max up to comfort + 1 blocks; above it doesn't"]),
        ("shadow mode doesn't apply the block",
         {"Conditions": type("ShadowApplied", (c.Conditions,), {
             "blocked": property(lambda self: self.would_block)})},
         ["block shadow: decided and kept, but control cools as if unblocked",
          "block shadow: a too_hot vote doesn't unblock (nothing is blocked)"]),
        ("heat sources block", {"heat_blocks": lambda ctx: False},
         ["heat source: on, it blocks in shadow mode, whatever the forecast",
          "heat source: on, it blocks in active mode too",
          "heat source: a too_hot vote unblocks against it"]),
        ("block_mode off ignores heat sources", {"heat_blocks": lambda ctx: ctx.heat_source_on},
         ["heat source: block_mode off ignores it"]),
        ("a too_hot vote unblocks while blocked", {"unblock_for_vote": lambda unit, ctx: unit},
         ["block active: a too_hot vote unblocks the unit, so its boost isn't rested",
          "heat source: a too_hot vote unblocks against it"]),
        ("the trust-weighted comfort point (plain mean instead)",
         {"comfort_point": lambda alm, readings: (lambda cs: round(sum(cs) / len(cs), 2) if cs
                                                  else None)(
             [e["comfort"] for sid, e in ((alm or {}).get("sensors") or {}).items()
              if readings.get(sid) is not None and e.get("comfort") is not None])},
         ["comfort point: the trust-weighted mean of the usable sensors' comfort readings"]),
        ("only usable sensors make the comfort point",
         {"comfort_point": lambda alm, readings: _COMFORT_POINT(
             alm, {sid: 0.0 for sid in (alm or {}).get("sensors") or {}})},
         ["comfort point: an unavailable or stale sensor, or one with no comfort, is left out",
          "fail open: no comfort point leans nothing and blocks nothing"]),
        ("fail open with no outdoor value (the last section's block carried over instead)",
         {"section_conditions": lambda section, comfort, mean, mx, margin, was, mode, k: (
             lambda cond: dataclasses.replace(cond, would_block=was and mode != "off")
             if mean is None else cond)(
                 _SECTION_CONDITIONS(section, comfort, mean, mx, margin, was, mode, k))},
         ["fail open: no outdoor value leans nothing and blocks nothing"]),
        ("boost cap", {"boost_capped": lambda unit, now, config: False},
         ["boost cap steps down twice"]),
        ("offset cap", {"capped_offset": lambda offset, cap: offset},
         ["offset cap: a boost at -3 steps down to -3, not -4",
          "offset cap: a rest at +3 wakes to +3, not +4"]),
        ("clamp to the unit's range", {"clamp": lambda value, lo, hi: value},
         ["clamp: the baby room's boost floors at 18", "clamp: a wake at 29 + 3 tops out at 30",
          "section change clamps to the baby room's 18"]),
        ("leak first", {"_leak_step": leak_waits},
         ["leak overrides the minimum time and grace", "leak overrides a person's off",
          "a leak during a person's off: the leak wins, and its release starts the unit as at a "
          "section change"]),
        ("commands only on transitions", {"_stay": resend},
         ["cool stays cool without votes, and sends nothing",
          "boost holds while still too warm, and sends nothing",
          "leak holds, and sends nothing again"]),
        ("trust-weighted direction", {"_trusts": lambda trusts: {}},
         ["votes: mixed sides resolved by trust",
          "mixed votes resolved by trust: boost, not rest"]),
        ("no-setpoint no-op",
         {"target_base": lambda unit, ctx: unit.base or ctx.setpoint_applied or 0.0},
         ["no setpoint: maintenance leaves the unit alone",
          "no seed: a too-warm quorum leaves a person's off alone"]),
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
         {"on_section_change": lambda unit, ctx, forced_off, now, votes: (lambda r: (
             dataclasses.replace(r[0], forced=unit.forced), r[1]))(
                 _SECTION_CHANGE(unit, ctx, forced_off, now, votes))},
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
        ("a cold room with a cooler forecast starts the section resting",
         {"starts_cold": lambda votes, ctx: False},
         ["section start: a cold room with a cooler forecast rests, and wakes at its base, "
          "offset 0",
          "section start: a quorum just under comfort, voting or not, is a cold room",
          "section start: a too_hot vote in a section rest is taken: a forced boost, then cool "
          "at the voted base",
          "section start: a too_cold vote in a section rest is taken: forced, since kept, "
          "nothing sent",
          "leak release in a cold room with a cooler forecast: a section rest"]),
        ("a section rest wakes at its base, not +1",
         {"release_offset": lambda unit, change: 0 if unit.forced else unit.offset + change},
         ["section start: a cold room with a cooler forecast rests, and wakes at its base, "
          "offset 0"]),
        ("the cold start needs a cooler forecast",
         {"starts_cold": lambda votes, ctx: cold_room(votes)},
         ["section start: a cold room with a forecast mean at or above comfort starts in cool",
          "section start: no conditions, no outdoor value or no comfort point starts a cold "
          "room in cool (fail open)"]),
        ("the cold start needs delta_mean above 0 (>= instead)",
         {"starts_cold": lambda votes, ctx: cold_room(votes) and ctx.delta_mean is not None
          and ctx.delta_mean >= 0},
         ["section start: a cold room with a forecast mean at or above comfort starts in cool"]),
        ("the cold start needs a cold room", {"starts_cold": lambda votes, ctx: cooler(ctx)},
         ["section start: a warm room with a cooler forecast starts in cool, ending a section "
          "rest"]),
        ("the cold room is a quorum below comfort (votes past the cold edge instead)",
         {"starts_cold": lambda votes, ctx: cooler(ctx) and votes.met
          and votes.direction == c.TOO_COLD},
         ["section start: a quorum just under comfort, voting or not, is a cold room"]),
        ("a block's rest is the block's, not a section rest",
         {"section_start": lambda votes, ctx: ("rest", c.START_COLD)
          if c.starts_cold(votes, ctx) else _SECTION_START(votes, ctx)},
         ["section start: blocked, a cold room rests for the block, not as a section rest",
          "section start: a heat source's rest wakes at +1 once it is off, as before"]),
        ("a vote ends a section rest",
         {"on_person_vote": lambda direction, unit, now, ctx, chosen=None: (lambda r: (
             dataclasses.replace(r[0], section_rest=unit.section_rest), r[1]))(
                 _PERSON_VOTE(direction, unit, now, ctx, chosen))},
         ["section start: a too_hot vote in a section rest is taken: a forced boost, then cool "
          "at the voted base",
          "section start: a too_cold vote in a section rest is taken: forced, since kept, "
          "nothing sent"]),
        ("a person's on, off or manual ends a section rest",
         {"on_person_change": lambda kind, unit, new_setpoint, now, ctx: dataclasses.replace(
             _PERSON_CHANGE(kind, unit, new_setpoint, now, ctx), section_rest=unit.section_rest)},
         ["section start: a person's on ends a section rest"]),
        ("a section rest is no episode",
         {"in_episode": lambda units: any(c.forced_now(u) or u.section_rest for u in units)},
         ["section start: a too_hot vote in a section rest is taken: a forced boost, then cool "
          "at the voted base",
          "section start: a too_cold vote in a section rest is taken: forced, since kept, "
          "nothing sent"]),
        ("the reactive_min_delta tolerance (exact setpoint instead)",
         {"_same_setpoint": lambda shown, commanded, min_delta: shown == commanded},
         ["a setpoint within reactive_min_delta of our command is ours"]),
    ]


def check_unit_start() -> int:
    """control.unit_start against on_section_change over a grid of unit
    states, the leak latch, a forced-off section, an almanac or none, a
    block, a heat source, the forecast and the readings: the state it
    records is the one the scene starts the unit in (the unit's own with no
    almanac), and its reason is cold exactly when the scene marks a section
    rest."""
    bad, n = [], 0
    for state in (*control.STATES, "unknown"):
        for leak in (False, True):
            for forced in (False, True):
                for sa in (None, 22.0):
                    for layer in ({}, {"blocked": True, "block_mode": "active"},
                                  {"heat_source_on": True}, {"delta_mean": 6.0},
                                  {"delta_mean": -3.0}):
                        for readings in ("cold", "settled_warm", "dead"):
                            ctx = _ctx({"setpoint_applied": sa, "leak_on": leak}, layer)
                            unit, v = _unit({"state": state}), _votes({"readings": readings})
                            new, _ = control.on_section_change(unit, ctx, forced, T0, v)
                            got, why = control.unit_start(unit, ctx, forced, v)
                            want = unit.state if got == control.NO_START else got
                            n += 1
                            if new.state != want or new.section_rest != (why == control.START_COLD):
                                bad.append(f"{state}, leak {leak}, forced {forced}, sa {sa}, "
                                           f"{layer}, {readings}: unit_start {got}/{why}, "
                                           f"the scene {new.state}")
    for line in bad[:5]:
        print(f"[FAIL] {line}")
    print(f"unit_start records the state on_section_change starts the unit in, and cold "
          f"exactly for a section rest: {n - len(bad)}/{n} cases")
    return len(bad)


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
    if passed != len(SCENARIOS) or check_unit_start():
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
