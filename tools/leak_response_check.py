"""
Adaptive Climate - leak response check.

Proves the full wet/dry cycle actually commands the air conditioner and
keeps the unit's state helper in step, not just the boolean latch. Runs
the generated automations through the stubbed Home Assistant of
tools/control_logic_check.py, on the example config with leak detection
wired on the baby room:

  1. wet: the leak automation raises the latch, puts the state helper in
     leak and drives dry (fan low, the unit's setpoint), under the guard
  2. a crossover landing mid-leak re-asserts dry instead of cooling, and
     maintenance holds leak without re-sending anything
  3. a person's change while latched leaves the unit in leak, and the
     unit's delayed echo of dry is not taken for a person at all
  4. release is blocked while the sensor still reads wet, even if confirmed
  5. release once dry and confirmed: latch and confirmation off, and the
     unit treated as at a section change (cool, fan low, the setpoint)
  6. units without leak detection never read a latch, and their rooms'
     automations are identical to a config with no leak detection at all

    PYTHONPATH=. python tools/leak_response_check.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import control_check as table                    # noqa: E402
from control_logic_check import HA, _sent         # noqa: E402
from app import control                           # noqa: E402
from app import generator as g                    # noqa: E402

CONFIG = json.loads((Path(__file__).resolve().parent.parent /
                     "examples" / "config.example.json").read_text())
ROOM, UNIT, ENTITY = "baby_room", "baby_room_ac", "climate.baby_room_ac"
SENSOR = "binary_sensor.baby_room_leak"
SECTION = "night"


def _world(cfg: dict, room: dict, state: str, setpoint_applied: float | None = 24.0) -> HA:
    ha = HA(ROOM, ENTITY)
    ha.now = table.T0
    since = table.T0.timestamp() - 3600
    for entity_id, value in ((g.guard_id(ROOM), "off"), (g.hold_id(ROOM), "off"),
                             (g.leak_id(ROOM, UNIT), "off"),
                             (g.leak_confirm_id(ROOM, UNIT), "off"), (SENSOR, "off"),
                             (g.unblocked_id(ROOM, UNIT), "off")):
        ha.set(entity_id, value)
    ha.set(g.scene_select_id(ROOM), SECTION, options=list(g.SECTIONS))
    ha.set(g.state_id(ROOM, UNIT), state, options=list(control.STATES))
    ha.set(g.offset_id(ROOM, UNIT), 1.0, min=-3, max=3)
    ha.set(g.base_id(ROOM, UNIT), 0.0, min=0, max=100)
    ha.set(g.since_id(ROOM, UNIT), "x", timestamp=since)
    ha.set(g.grace_until_id(ROOM, UNIT), "x", timestamp=since)
    units = {} if setpoint_applied is None else {UNIT: {"setpoint_applied": setpoint_applied}}
    ha.set(g.almanac_id(ROOM), "ok", sections={SECTION: {"units": units, "sensors": {}}})
    ha.set(ENTITY, "cool", temperature=25.0, fan_mode="low", min_temp=18, max_temp=30,
           fan_modes=["quiet", "low", "medium", "high", "auto"])
    for s in room["sensors"]:
        ha.set(s["entity_id"], 24.0)
    return ha


def main() -> int:
    cfg = json.loads(json.dumps(CONFIG))
    room = next(r for r in cfg["rooms"] if r["id"] == ROOM)
    unit = room["units"][0]
    unit["leak_detection"] = {"enabled": True, "sensor_entity_id": SENSOR}
    leak = g.build_leak_automation(cfg, room, unit)
    release = g.build_leak_release_automation(cfg, room, unit)
    maintenance = g.build_maintenance_automation(cfg, room)
    manual = g.build_manual_change_automation(cfg, room, unit)
    scene = g.build_scene_automation(cfg, room, {"id": SECTION, "name": "Night"})
    assert leak["trigger"] == [{"platform": "state", "entity_id": SENSOR, "to": "on"}]

    # 1. wet
    ha = _world(cfg, room, "cool")
    ha.set(SENSOR, "on")
    ha.run(leak, {"to_state": {"state": "on"}})
    assert ha.states(g.leak_id(ROOM, UNIT)) == "on"
    assert ha.states(g.state_id(ROOM, UNIT)) == "leak"
    assert ha.state_attr(g.since_id(ROOM, UNIT), "timestamp") == table.T0.timestamp()
    assert _sent(ha.calls) == {"hvac_mode": "dry", "fan_mode": "low", "temperature": 24.0}, \
        _sent(ha.calls)
    assert all(guarded for s, _, _, guarded in ha.calls if s.startswith("climate."))
    assert ha.states(g.guard_id(ROOM)) == "off"
    print("wet: latch on, state helper leak, dry / fan low / the unit's setpoint, "
          "sent under the guard - OK")
    dry = ha.climate()

    # with no almanac yet, a leak still goes dry, leaving the setpoint alone
    bare = _world(cfg, room, "cool", setpoint_applied=None)
    bare.run(leak, {"to_state": {"state": "on"}})
    assert _sent(bare.calls) == {"hvac_mode": "dry", "fan_mode": "low", "temperature": None}
    print("wet with no almanac yet: dry and fan low, no setpoint - OK")

    # 2. a crossover mid-leak re-asserts dry; maintenance holds
    ha.calls = []
    ha.run(scene)
    assert ha.states(g.state_id(ROOM, UNIT)) == "leak"
    assert _sent(ha.calls)["hvac_mode"] == "dry", _sent(ha.calls)
    print("crossover mid-leak: stays leak and re-sends dry, not cool - OK")
    ha.calls = []
    ha.now = table._time(table.m(30))
    ha.run(maintenance)
    assert ha.states(g.state_id(ROOM, UNIT)) == "leak" and ha.calls == []
    print("maintenance mid-leak: holds leak and sends nothing - OK")

    # 3. people while latched, and the unit's echo of dry
    before = ha.climate()
    for after in (control.Climate("off", before.temperature, before.fan),
                  control.Climate("cool", 22.0, "low"),
                  control.Climate("dry", 20.0, None)):
        trig = {"from_state": {"state": before.mode, "attributes": {
                    "temperature": before.temperature, "fan_mode": before.fan},
                    "context": {"parent_id": None}},
                "to_state": {"state": after.mode, "attributes": {
                    "temperature": after.temperature, "fan_mode": after.fan},
                    "context": {"parent_id": None}}}
        ha.run(manual, trig)
        assert ha.states(g.state_id(ROOM, UNIT)) == "leak", after
    echo = {"from_state": {"state": "cool", "attributes": {"temperature": 25.0},
                           "context": {"parent_id": None}},
            "to_state": {"state": dry.mode, "attributes": {"temperature": dry.temperature,
                                                           "fan_mode": dry.fan},
                         "context": {"parent_id": None}}}
    assert ha.run(manual, echo) is None
    print("people while latched: off, cool and a new setpoint all leave the unit in leak; "
          "the unit's delayed echo of dry isn't a person - OK")

    # 4. confirmed while still wet: release is blocked
    ha.set(g.leak_confirm_id(ROOM, UNIT), "on")
    ha.set(SENSOR, "on")
    assert ha.run(release, {"to_state": {"state": "on"}}) is None
    assert ha.states(g.leak_id(ROOM, UNIT)) == "on"
    print("release while the sensor still reads wet: blocked, even though confirmed - OK")

    # 5. dry and confirmed: released, as at a section change
    ha.set(SENSOR, "off")
    ha.calls = []
    ha.run(release, {"to_state": {"state": "on"}})
    assert ha.states(g.leak_id(ROOM, UNIT)) == "off"
    assert ha.states(g.leak_confirm_id(ROOM, UNIT)) == "off"
    assert ha.states(g.state_id(ROOM, UNIT)) == "cool"
    assert float(ha.states(g.offset_id(ROOM, UNIT))) == 0
    assert _sent(ha.calls) == {"hvac_mode": "cool", "fan_mode": "low", "temperature": 24.0}
    print("release once dry and confirmed: latch and confirmation off, state cool at offset 0, "
          "cool / fan low / 24 - OK")

    # 6. no regression for units without leak detection
    plain = next(r for r in cfg["rooms"] if r["id"] == "main_room")
    baseline = json.loads(json.dumps(CONFIG))
    plain0 = next(r for r in baseline["rooms"] if r["id"] == "main_room")
    for build in (lambda c, r: g.build_scene_automation(c, r, {"id": SECTION}),
                  g.build_maintenance_automation,
                  lambda c, r: g.build_manual_change_automation(c, r, r["units"][0])):
        assert g.dump(build(cfg, plain)) == g.dump(build(baseline, plain0))
    text = g.dump(g.render_all(baseline))
    assert "ac_leak_" not in text, "a unit without leak detection reads a latch"
    print("units without leak detection: no latch read anywhere, and their rooms' "
          "automations unchanged - OK")

    print("\nALL LEAK RESPONSE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
