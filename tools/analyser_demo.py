"""
Adaptive Climate - analyser worked examples (the phase 1 acceptance check).

Builds small synthetic stores through the real Store API, replays them with
app.analyser.analyse_room, and asserts the reaction-driven learning model of
docs/V2_DESIGN.md and docs/TRUST_MODEL.md:

  * 8 days at 21 plus one reaction 21 -> 22 applies 22 (v1 applied 21)
  * 300 quiet heartbeats leave the seed unchanged
  * rounding goes toward the last reaction, both ways, keeps its direction
    while S doesn't move, and rounds half up before there is one
  * a reaction is setpoint-moving when a setpoint changed, whatever its kind:
    an `on` with a new setpoint sets the direction and opens a window
  * the replay is deterministic, whatever order the rows were written in
  * the comfort window: accepted, disqualified by a second reaction, by too
    few heartbeats or by a boost heartbeat, and kept to its section across
    a boundary
  * bands are direction-matched and measured against the replayed comfort
  * on and off teach bands as cooler and warmer wanted, manual teaches
    nothing, and every kind resets quiet and disqualifies a window
  * the confidence curve
  * no seed, no almanac entry
  * tools/compare_analyser.py shows v1 against v2 on the same data

Runs without Docker or Home Assistant:  PYTHONPATH=. python tools/analyser_demo.py
"""
from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analyser import (LearningConfig, analyse_room,           # noqa: E402
                          confidence_from_quiet, round_toward)
from app.store import ReactiveUnitSample, Store, UnitSample       # noqa: E402
from tools.compare_analyser import compare                        # noqa: E402

SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "storage.schema.sql"
HK = ZoneInfo("Asia/Hong_Kong")
ROOM = {"id": "lounge", "units": [{"id": "ac"}],
        "sensors": [{"id": "wall"}, {"id": "door"}]}
CONFIG = {"version": 2, "system": {}, "rooms": [ROOM]}
STEADY = {"wall": 25.0, "door": 25.0}


def t(day: int, hh: int, mm: int = 0) -> datetime:
    """A local time on 1-10 August 2026."""
    return datetime(2026, 8, day, hh, mm, tzinfo=HK)


class Scenario:
    """A fresh store for one example, written through the Store API."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.store = Store(self.dir, SCHEMA)

    def beat(self, when: datetime, section: str = "day", setpoint: float | None = 21.0,
             on: bool = True, readings: dict | None = None, state: str | None = None) -> None:
        self.store.record_heartbeat(
            "lounge", when.isoformat(timespec="seconds"),
            when.astimezone(timezone.utc).isoformat(timespec="seconds"),
            when.date().isoformat(), section,
            sensors=dict(STEADY if readings is None else readings),
            units={"ac": UnitSample(on, "cool" if on else "off", "low", setpoint, 24.0,
                                    "normal", control_state=state)})

    def beats(self, start: datetime, end: datetime, section: str = "day", **kw) -> None:
        when = start
        while when <= end:
            self.beat(when, section, **kw)
            when += timedelta(minutes=10)

    def react(self, when: datetime, before: float, after: float, section: str = "day",
              readings: dict | None = None, kind: str | None = None) -> None:
        self.store.record_reactive(
            "lounge", when.isoformat(timespec="seconds"),
            when.astimezone(timezone.utc).isoformat(timespec="seconds"),
            when.date().isoformat(), section, 120,
            units={"ac": ReactiveUnitSample(before, after, before != after)},
            sensors=dict(STEADY if readings is None else readings), kind=kind)

    def run(self, start: datetime, end: datetime | None, section: str = "day") -> None:
        self.store.record_section_run(
            "lounge", start.date().isoformat(), section,
            actual_start=start.isoformat(timespec="seconds"),
            ended_at=end.isoformat(timespec="seconds") if end else None)

    def replay(self, as_of: datetime, cfg: LearningConfig = LearningConfig()) -> dict:
        return {s.section: s for s in analyse_room(self.store._conn, ROOM, cfg, as_of)}


def ok(msg: str) -> None:
    print(f"  {msg} - OK")


# ---------------------------------------------------------------------

def check_headline() -> None:
    print("headline: 8 days at 21, then one reaction 21 -> 22")
    sc = Scenario()
    for day in range(1, 9):
        sc.beats(t(day, 8), t(day, 13, 50), readings={"wall": 25.0, "door": 25.0})
    before = sc.replay(t(8, 9, 55))["day"]
    assert before.status == "provisional" and before.unit_setpoints_applied == {"ac": 21.0}
    sc.react(t(8, 10), 21.0, 22.0, readings={"wall": 24.0, "door": 25.0})
    day = sc.replay(t(9, 0, 15))["day"]
    assert day.status == "learned", day.status
    assert day.unit_setpoints == {"ac": 21.5} and day.unit_setpoints_applied == {"ac": 22.0}
    assert abs(day.sensor_band["wall"] - 1.0) < 1e-9          # drifted 1.0 below comfort
    assert abs(day.sensor_trust["wall"] - 0.889) < 0.001
    assert day.sensor_band["door"] == 5.0                     # sat at comfort: learns nothing
    ok("provisional at 21 before; learned, S 21.5, applied 22 after; wall band 1.0, "
       "trust 0.889; door untouched")

    sc.store.publish_almanac("lounge", list(sc.replay(t(9, 0, 15)).values()))
    served = sc.store.current_almanac("lounge", as_of=t(9, 0, 15).date())["sections"]["day"]
    assert served["status"] == "learned"
    assert served["units"]["ac"] == {"setpoint": 21.5, "setpoint_applied": 22.0, "off": False}
    ok("published and served: setpoint_applied 22 in the almanac HA reads")
    print("  served:", json.dumps(served))

    rows = compare(sc.store._conn, CONFIG, t(9, 0, 15))
    unit = next(r for r in rows if r["kind"] == "unit" and r["section"] == "day")
    assert unit["v1"]["applied"] == 21.0 and unit["v2"]["applied"] == 22.0, unit
    ok(f"compare_analyser: v1 learned {unit['v1']['setpoint']:.3f} and applied 21 (the "
       f"snap-back); v2 applies 22")


def check_quiet_heartbeats() -> None:
    print("quiet heartbeats")
    sc = Scenario()
    sc.beat(t(1, 8), setpoint=21.0, readings={"wall": 25.0, "door": 25.0})
    when = t(1, 8, 10)
    for _ in range(300):                      # the system's own output, never a person's
        sc.beat(when, setpoint=24.0, readings={"wall": 27.0, "door": 23.0})
        when += timedelta(minutes=10)
        if when.hour >= 14:
            when = (when + timedelta(days=1)).replace(hour=8, minute=0)
    day = sc.replay(t(10, 23))["day"]
    assert day.unit_setpoints == {"ac": 21.0} and day.unit_setpoints_applied == {"ac": 21.0}
    assert day.sensor_comfort == {"wall": 25.0, "door": 25.0}
    assert day.status == "provisional"
    ok("300 heartbeats at 24 / 27 / 23 leave the seed 21 and comforts 25 unchanged")


def check_rounding() -> None:
    print("rounding")
    cases = [(21.5, 0, 22.0), (22.5, 0, 23.0), (21.2, 1, 22.0), (21.8, -1, 21.0),
             (22.0 + 1e-12, -1, 22.0), (21.0, 1, 21.0), (21.5, -1, 21.0)]
    for value, direction, want in cases:
        assert round_toward(value, direction) == want, (value, direction)
    ok("round_toward: half up with no direction (22.5 -> 23, not 22), up, down, "
       "float noise stays on its step")

    sc = Scenario()
    sc.beat(t(1, 8), setpoint=22.0)
    sc.react(t(1, 10), 22.0, 23.0)
    assert sc.replay(t(1, 23))["day"].unit_setpoints_applied == {"ac": 23.0}
    assert round(22.5) == 22                    # what v1's round() would have applied
    ok("seed 22, reaction 22 -> 23: S 22.5 applies 23 (round-half-even would give 22)")

    sc = Scenario()
    sc.beat(t(1, 8), setpoint=22.0)
    sc.react(t(1, 10), 22.0, 21.0)
    assert sc.replay(t(1, 23))["day"].unit_setpoints_applied == {"ac": 21.0}
    ok("seed 22, reaction 22 -> 21: S 21.5 applies 21 (half up would give 22)")
    sc.react(t(1, 12), 21.0, 21.0)              # up and back down in one window
    day = sc.replay(t(1, 23))["day"]
    assert day.unit_setpoints == {"ac": 21.5} and day.unit_setpoints_applied == {"ac": 21.0}
    ok("a reaction that leaves S where it is keeps the downward direction: still 21")

    sc = Scenario()
    sc.beat(t(1, 8), setpoint=23.0)
    sc.react(t(1, 10), 21.0, 22.0)              # boosting at 21; the person sets 22
    day = sc.replay(t(1, 23))["day"]
    assert day.unit_setpoints == {"ac": 22.5} and day.unit_setpoints_applied == {"ac": 22.0}
    ok("seed 23, boosted at 21, person sets 22: the gesture is warmer but S moved down "
       "to 22.5, so 22 applies")


def check_on_with_setpoint() -> None:
    print("setpoint-moving by setpoint change, whatever the kind")
    window = [t(1, 14, 30), t(1, 14, 40), t(1, 14, 50)]

    sc = Scenario()
    sc.beat(t(1, 8), setpoint=22.0)
    sc.react(t(1, 10), 22.0, 21.0)               # S 21.5, direction down: applies 21
    sc.react(t(1, 14), 21.0, 23.0, kind="on")    # turned on at a new setpoint
    for when in window:
        sc.beat(when, setpoint=23.0, readings={"wall": 23.0, "door": 24.0})
    day = sc.replay(t(1, 23))["day"]
    assert day.unit_setpoints == {"ac": 22.25}, day.unit_setpoints
    assert day.unit_setpoints_applied == {"ac": 23.0}    # up; the old rule floored to 22
    assert day.sensor_comfort == {"wall": 24.0, "door": 24.5}, day.sensor_comfort
    ok("an `on` that also sets 21 -> 23 moves S to 22.25, turns the direction up "
       "(applies 23) and opens a comfort window")

    sc = Scenario()
    sc.beat(t(1, 8), setpoint=22.0)
    sc.react(t(1, 14), 22.0, 22.0, kind="on")   # turned on, setpoint untouched
    for when in window:
        sc.beat(when, setpoint=22.0, readings={"wall": 23.0, "door": 24.0})
    day = sc.replay(t(1, 23))["day"]
    assert day.status == "provisional"
    assert day.unit_setpoints == {"ac": 22.0} and day.unit_setpoints_applied == {"ac": 22.0}
    assert day.sensor_comfort == {"wall": 25.0, "door": 25.0}, day.sensor_comfort
    sc.react(t(1, 10), 22.0, 21.0)              # an earlier cooler correction...
    day = sc.replay(t(1, 23))["day"]
    assert day.unit_setpoints_applied == {"ac": 21.0}     # ...whose direction the on keeps
    ok("an `on` with no setpoint change moves nothing: still provisional, no comfort "
       "window, and an earlier direction stands")


def check_deterministic() -> None:
    print("determinism")

    def build(reverse: bool) -> Scenario:
        sc = Scenario()
        beats = [(t(d, h, m),) for d in (1, 2) for h in range(8, 14) for m in (0, 20, 40)]
        reactions = [(t(1, 10), 21.0, 22.0, {"wall": 23.6, "door": 25.2}),
                     (t(2, 9), 22.0, 23.0, {"wall": 24.1, "door": 26.0}),
                     (t(2, 12), 23.0, 21.0, {"wall": 25.5, "door": 27.0})]
        for (when,) in (reversed(beats) if reverse else beats):
            sc.beat(when, readings={"wall": 24.0 + when.minute / 100, "door": 25.0})
        for when, b, a, r in (reversed(reactions) if reverse else reactions):
            sc.react(when, b, a, readings=r)
        return sc

    a, b = build(False), build(True)
    as_of = t(3, 1)
    first = [dataclasses.asdict(s) for s in a.replay(as_of).values()]
    assert first == [dataclasses.asdict(s) for s in a.replay(as_of).values()]
    assert first == [dataclasses.asdict(s) for s in b.replay(as_of).values()]
    ok("the same data replays to the same almanac, twice and when written in reverse order")


def check_comfort_window() -> None:
    print("comfort window (30-60 min after a setpoint-moving reaction)")

    def scenario(window_beats: list[tuple[int, str | None]], extra=None) -> Scenario:
        sc = Scenario()
        sc.beat(t(1, 8), readings={"wall": 25.0, "door": 25.0})
        sc.react(t(1, 10), 21.0, 22.0)
        for minute, state in window_beats:
            sc.beat(t(1, 10) + timedelta(minutes=minute), readings={"wall": 23.0, "door": 24.0},
                    state=state)
        if extra:
            extra(sc)
        return sc

    full = [(30, None), (40, None), (50, None), (60, None)]
    sc = scenario(full)
    assert sc.replay(t(1, 10, 45))["day"].sensor_comfort == {"wall": 25.0, "door": 25.0}
    assert sc.replay(t(1, 11, 5))["day"].sensor_comfort == {"wall": 24.0, "door": 24.5}
    ok("accepted: comfort moves halfway to the window mean (wall 25 -> 24, door 25 -> 24.5), "
       "and only once the window has closed")

    sc = scenario(full, lambda s: s.react(t(1, 10, 45), 22.0, 22.0, kind="manual"))
    assert sc.replay(t(1, 23))["day"].sensor_comfort == {"wall": 25.0, "door": 25.0}
    ok("disqualified by another reaction (of any kind) inside the window")
    sc = scenario([(35, None)])
    assert sc.replay(t(1, 23))["day"].sensor_comfort == {"wall": 25.0, "door": 25.0}
    ok("disqualified by fewer than 2 heartbeats in the window")
    sc = scenario([(30, None), (40, "boost"), (50, None)])
    assert sc.replay(t(1, 23))["day"].sensor_comfort == {"wall": 25.0, "door": 25.0}
    sc = scenario([(30, None), (40, "cool"), (50, None)])
    assert sc.replay(t(1, 23))["day"].sensor_comfort == {"wall": 24.0, "door": 24.5}
    ok("disqualified by a heartbeat in boost; a cool (or null) state is fine")

    sc = Scenario()                              # night 20:30-22:00, sleep from 22:00
    sc.beat(t(1, 20, 40), section="night", readings={"wall": 25.0, "door": 25.0})
    sc.react(t(1, 21, 45), 21.0, 22.0, section="night")
    for minute in (30, 40, 50, 60):              # 22:15-22:45: already sleep
        sc.beat(t(1, 21, 45) + timedelta(minutes=minute), section="sleep",
                readings={"wall": 23.0, "door": 23.0})
    out = sc.replay(t(2, 1))
    assert out["night"].sensor_comfort == {"wall": 24.0, "door": 24.0}, out["night"]
    assert out["sleep"].sensor_comfort == {"wall": 23.0, "door": 23.0}   # its own seed only
    ok("a window crossing into sleep updates the reaction's section (night), not sleep")


def check_band_direction() -> None:
    print("bands: direction matching, replayed comfort, recency")
    readings = {"wall": 23.5, "door": 26.5}      # wall 1.5 below comfort, door 1.5 above
    for before, after, learns, ignores, label in ((21.0, 22.0, "wall", "door", "warmer"),
                                                  (22.0, 21.0, "door", "wall", "cooler")):
        sc = Scenario()
        sc.beat(t(1, 8))
        sc.react(t(1, 10), before, after, readings=readings)
        day = sc.replay(t(1, 23))["day"]
        assert abs(day.sensor_band[learns] - 1.5) < 1e-9 and day.sensor_band[ignores] == 5.0
        ok(f"{label} wanted ({before:.0f} -> {after:.0f}) teaches only {learns}, the sensor "
           f"on the {'cold' if label == 'warmer' else 'warm'} side")

    sc = Scenario()
    sc.beat(t(1, 8))
    sc.react(t(1, 10), 21.0, 22.0, readings={"wall": 24.8, "door": 25.0})
    assert sc.replay(t(1, 23))["day"].sensor_band["wall"] == 5.0
    ok("a 0.2 drift is below the high-trust deviation and teaches nothing")

    sc = Scenario()
    sc.beat(t(1, 8))
    sc.react(t(1, 10), 21.0, 22.0, readings={"wall": 25.0, "door": 25.0})
    sc.beats(t(1, 10, 30), t(1, 11), readings={"wall": 23.0, "door": 25.0})
    sc.react(t(1, 14), 22.0, 23.0, readings={"wall": 22.5, "door": 25.0})
    day = sc.replay(t(1, 23))["day"]
    assert day.sensor_comfort["wall"] == 24.0
    assert abs(day.sensor_band["wall"] - 1.5) < 1e-9, day.sensor_band
    ok("deviation is taken against comfort as replayed then (24.0 after the first window), "
       "so 22.5 is 1.5 off, not 2.5")

    sc = Scenario()
    sc.beat(t(1, 8))
    sc.react(t(1, 10), 21.0, 22.0, readings={"wall": 24.0, "door": 25.0})    # 1.0 off
    sc.react(t(8, 10), 22.0, 23.0, readings={"wall": 23.0, "door": 25.0})    # 2.0 off
    day = sc.replay(t(8, 23))["day"]
    assert abs(day.sensor_band["wall"] - (0.5 * 1.0 + 1.0 * 2.0) / 1.5) < 1e-9
    ok("a week-old reaction weighs half (half-life 7 days): band (0.5x1 + 1x2)/1.5 = 1.667")


def check_on_off_manual() -> None:
    print("on / off / manual reactions")
    readings = {"wall": 23.5, "door": 26.5}      # wall 1.5 below comfort, door 1.5 above
    for kind, learns, ignores in (("on", "door", "wall"), ("off", "wall", "door")):
        sc = Scenario()
        sc.beat(t(1, 8))
        sc.react(t(1, 10), 21.0, 21.0, readings=readings, kind=kind)
        day = sc.replay(t(1, 23))["day"]
        assert abs(day.sensor_band[learns] - 1.5) < 1e-9 and day.sensor_band[ignores] == 5.0
        assert day.unit_setpoints == {"ac": 21.0} and day.status == "provisional"
        ok(f"`{kind}` ({'cooler' if kind == 'on' else 'warmer'} wanted) teaches only {learns}, "
           f"the sensor on the {'warm' if kind == 'on' else 'cold'} side; S stays put")

    sc = Scenario()
    sc.beat(t(1, 8))
    sc.react(t(1, 10), 21.0, 21.0, readings=readings, kind="manual")
    day = sc.replay(t(1, 23))["day"]
    assert day.sensor_band == {"wall": 5.0, "door": 5.0} and day.unit_setpoints == {"ac": 21.0}
    assert day.status == "provisional"
    ok("`manual` (a mode or fan change) teaches nothing")

    for kind in ("on", "off", "manual", None):
        sc = Scenario()
        sc.beat(t(1, 8))
        for day in range(1, 7):
            sc.run(t(day, 8), t(day, 14))
            sc.beat(t(day, 9))
        before = sc.replay(t(7, 1))["day"].confidence
        sc.react(t(4, 10), 21.0, 22.0 if kind is None else 21.0, kind=kind)
        after = sc.replay(t(7, 1))["day"].confidence
        assert abs(before - 0.75) < 1e-9 and abs(after - confidence_from_quiet(2)) < 1e-9, kind

        sc = Scenario()
        sc.beat(t(1, 8))
        sc.react(t(1, 10), 21.0, 22.0)
        sc.beats(t(1, 10, 30), t(1, 11), readings={"wall": 23.0, "door": 24.0})
        sc.react(t(1, 10, 45), 22.0, 22.0, kind=kind or "setpoint")
        assert sc.replay(t(1, 23))["day"].sensor_comfort == {"wall": 25.0, "door": 25.0}, kind
    ok("every kind - on, off, manual, setpoint - resets the quiet count (0.75 -> 0.370) and "
       "disqualifies a comfort window it falls inside")


def check_confidence() -> None:
    print("confidence = 1 - 0.5^(quiet/3)")
    assert [round(confidence_from_quiet(q), 4) for q in (0, 3, 6, 9)] == \
        [0.0, 0.5, 0.75, 0.875]
    sc = Scenario()
    sc.beat(t(1, 8))
    for day in range(1, 7):
        sc.run(t(day, 8), t(day, 14))
        sc.beat(t(day, 9))
    assert abs(sc.replay(t(7, 1))["day"].confidence - 0.75) < 1e-9
    ok("6 completed quiet runs with heartbeats: 0.75")

    sc.run(t(7, 8), t(7, 14))                    # no heartbeat in it: doesn't count
    sc.run(t(8, 8), None)                        # still open: doesn't count
    sc.beat(t(8, 9))
    assert abs(sc.replay(t(8, 12))["day"].confidence - 0.75) < 1e-9
    ok("a run with no heartbeat, or not yet completed, adds nothing")

    sc.react(t(4, 10), 21.0, 22.0)               # day 4's run had a reaction
    assert abs(sc.replay(t(8, 12))["day"].confidence - confidence_from_quiet(2)) < 1e-9
    ok("a reaction resets it: only days 5 and 6 count after the day-4 reaction (0.370)")


def check_missing_seed() -> None:
    print("no seed, no entry")
    sc = Scenario()
    sc.beat(t(1, 8), section="day")
    sc.beat(t(1, 16, 30), section="sunset", on=False, setpoint=24.0)   # off all section
    sc.react(t(1, 17), 24.0, 23.0, section="sunset")
    out = sc.replay(t(1, 23))
    assert "night" not in out, "a section with no heartbeats got an entry"
    assert out["sunset"].unit_setpoints == {} and out["sunset"].unit_setpoints_applied == {}
    assert set(out["sunset"].sensor_comfort) == {"wall", "door"}
    sc.store.publish_almanac("lounge", list(out.values()))
    served = sc.store.current_almanac("lounge", as_of=t(1, 23).date())["sections"]
    assert served["sunset"]["units"] == {} and "night" not in served
    ok("a unit never on in a section, even with a reaction there, gets no entry; a section "
       "with no heartbeat gets none at all, so the scene leaves the unit alone")


def main() -> int:
    check_headline()
    check_quiet_heartbeats()
    check_rounding()
    check_on_with_setpoint()
    check_deterministic()
    check_comfort_window()
    check_band_direction()
    check_on_off_manual()
    check_confidence()
    check_missing_seed()
    print("\nALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
