"""
Adaptive Climate - analyser worked examples (the phase 1 acceptance check).

Builds small synthetic stores through the real Store API, replays them with
app.analyser.analyse_room, and asserts the reaction-driven learning model of
docs/V2_DESIGN.md and docs/TRUST_MODEL.md:

  * 8 days at 21 plus one reaction 21 -> 22 applies 22 (v1 applied 21)
  * 300 quiet heartbeats leave the seed unchanged
  * setpoint_applied is tracked through the replay: a correction moves it
    at least one step toward the chosen value and never past it, otherwise
    to the step nearest S; Main Room sleep's S 24.17 applies 24, not 25;
    a chosen value equal to the applied one keeps it; quiet days never
    change it
  * a reaction is setpoint-moving when a setpoint changed, whatever its kind:
    an `on` with a new setpoint sets the direction and opens a window
  * the replay is deterministic, whatever order the rows were written in
  * the comfort window: accepted, disqualified by a second reaction, by too
    few heartbeats or by a boost heartbeat, and kept to its section across
    a boundary
  * a person's vote (too_cold / too_hot, from a button or a remote): every
    unit's applied setpoint steps one from the value in force (or goes to
    the remote's value), and each sensor's comfort moves halfway toward its
    reading plus or minus person_step_degrees; its forced rest or boost
    disqualifies the comfort window
  * bands move by reaction: on the person's side and inside the band,
    toward |d|; past it, toward 0.8 x band; on the other side, toward
    band + |d|; clamped; measured against the replayed comfort; quiet
    periods never move them
  * on and off teach bands as cooler and warmer wanted, manual teaches
    nothing, and every kind resets quiet and disqualifies a window
  * the confidence curve
  * the block margin: clamped to 0..10, so a margin learned down from 1 by
    an unblocked section whose forecast max was above the comfort point
    stops at 0 (blocking at the comfort point itself) rather than going
    negative, and climbs again from a blocked one; capped at 10
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

from app.analyser import (LearningConfig, MarginConfig, analyse_room,   # noqa: E402
                          confidence_from_quiet, is_setpoint_moving, learn_margin,
                          margin_step, nearest_step, next_applied, nudge_band,
                          wanted_direction)
from app.control import MARGIN_MAX, MARGIN_MIN                    # noqa: E402
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

    def vote(self, when: datetime, kind: str, readings: dict | None = None,
             chosen: float | None = None, section: str = "day") -> None:
        """A person's vote as the runtime records HA's ac_person_vote event:
        no unit rows for a button, the value chosen for a remote."""
        units = {} if chosen is None else {"ac": ReactiveUnitSample(None, chosen, True,
                                                                    kind=kind)}
        self.store.record_reactive(
            "lounge", when.isoformat(timespec="seconds"),
            when.astimezone(timezone.utc).isoformat(timespec="seconds"),
            when.date().isoformat(), section, 0, units=units,
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
    assert abs(day.sensor_band["wall"] - 3.0) < 1e-9          # 5.0 halfway to its 1.0 off
    assert abs(day.sensor_trust["wall"] - 0.444) < 0.001
    assert day.sensor_band["door"] == 5.0                     # sat at comfort: learns nothing
    ok("provisional at 21 before; learned, S 21.5, applied 22 after; wall band 5.0 -> 3.0 "
       "(halfway to the 1.0 it was off), trust 0.444; door untouched")

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
    print("rounding: setpoint_applied, tracked through the replay")
    cases = [(21.5, 0, 22.0), (22.5, 0, 23.0), (22.5, 1, 23.0), (22.5, -1, 22.0),
             (21.2, 1, 21.0), (21.8, -1, 22.0), (22.0 + 1e-12, -1, 22.0)]
    for value, direction, want in cases:
        assert nearest_step(value, direction) == want, (value, direction)
    ok("nearest_step: the nearest whole degree, an exact half toward the reaction's direction "
       "(up with none: 22.5 -> 23, not 22), float noise stays on its step")
    assert next_applied(24.0, 23.4, 24.0, 1) == 24.0      # nearest would be 23
    assert next_applied(22.0, 22.9, 22.6, 1) == 22.0      # a step would pass 22.6
    ok("next_applied: a chosen value equal to the applied one keeps it; a step that would "
       "pass the chosen value isn't taken")

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
    ok("a reaction that leaves the setpoint where it was moves nothing: still 21")

    sc = Scenario()
    sc.beat(t(1, 8), setpoint=23.0)
    sc.react(t(1, 10), 21.0, 22.0)              # boosting at 21; the person sets 22
    day = sc.replay(t(1, 23))["day"]
    assert day.unit_setpoints == {"ac": 22.5} and day.unit_setpoints_applied == {"ac": 22.0}
    ok("seed 23, boosted at 21, person sets 22: the gesture is warmer but 22 is below the "
       "applied 23, so it moves down a step to 22")

    check_overapplied()

    # A correction from an applied value that sits on a .5 tie still moves.
    sc = Scenario()
    sc.beat(t(1, 8), setpoint=22.0)
    sc.react(t(1, 10), 22.0, 23.0)              # S 22.5: a tie, applied 23
    sc.react(t(1, 12), 23.0, 24.0)              # S 23.25: nearest is 23, still 23 applied
    day = sc.replay(t(1, 23))["day"]
    assert day.unit_setpoints == {"ac": 23.25} and day.unit_setpoints_applied == {"ac": 24.0}
    down = Scenario()
    down.beat(t(1, 8), setpoint=22.0)
    down.react(t(1, 10), 22.0, 23.0)
    down.react(t(1, 12), 23.0, 22.0)            # S 22.25, back down a step
    assert down.replay(t(1, 23))["day"].unit_setpoints_applied == {"ac": 22.0}
    ok("from 23, applied off the tie S 22.5: a correction to 24 moves one step although "
       "S 23.25 is nearer 23, and one to 22 moves one step down")

    # Quiet days never change it: the 24 above, though S 23.25 is nearer 23.
    confidences = []
    for day_n in range(2, 7):
        sc.beats(t(day_n, 8), t(day_n, 13, 50), setpoint=24.0)
        sc.run(t(day_n, 8), t(day_n, 14))
    for day_n in range(1, 7):
        day = sc.replay(t(day_n, 23))["day"]
        assert day.unit_setpoints == {"ac": 23.25}, (day_n, day.unit_setpoints)
        assert day.unit_setpoints_applied == {"ac": 24.0}, (day_n, day.unit_setpoints_applied)
        confidences.append(round(day.confidence, 3))
    assert confidences == sorted(confidences) and confidences[0] < confidences[-1], confidences
    ok(f"five quiet days at 24: still S 23.25 and applied 24 at every nightly replay, while "
       f"confidence rises {confidences[0]} -> {confidences[-1]}")


def check_overapplied() -> None:
    """Main Room sleep: S 24.17, which the direction rule applied as 25."""
    sc = Scenario()
    sc.beat(t(1, 22, 10), section="sleep", setpoint=24.0)
    chosen = (23.0, 24.0, 23.0, 24.0, 23.0, 25.0)       # the last one up
    before = 24.0
    for day_n, after in enumerate(chosen, start=1):
        sc.react(t(day_n, 23), before, after, section="sleep")
        before = after
    sleep = sc.replay(t(7, 8))["sleep"]
    assert abs(sleep.unit_setpoints["ac"] - 24.171875) < 1e-9, sleep.unit_setpoints
    assert sleep.unit_setpoints_applied == {"ac": 24.0}, sleep.unit_setpoints_applied
    ok("Main Room sleep: seed 24, then 23, 24, 23, 24, 23 and finally 25: S 24.17 applies 24 "
       "(nearest, and a step up from the 23 in force), not 25")


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
    assert day.unit_setpoints_applied == {"ac": 22.0}    # a step up from 21, nearest 22.25
    assert day.sensor_comfort == {"wall": 24.0, "door": 24.5}, day.sensor_comfort
    ok("an `on` that also sets 21 -> 23 moves S to 22.25 and the applied setpoint a step up "
       "from 21 to 22, and opens a comfort window")

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
    assert day.unit_setpoints_applied == {"ac": 21.0}     # ...which the on leaves alone
    ok("an `on` with no setpoint change moves nothing: still provisional, no comfort "
       "window, and an earlier correction's applied value stands")


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


def check_votes() -> None:
    print("a person's vote: too_cold / too_hot")
    assert is_setpoint_moving("too_cold") and is_setpoint_moving("too_hot")
    assert wanted_direction("too_cold", {}) == 1 and wanted_direction("too_hot", {}) == -1
    sc = Scenario()
    sc.beat(t(1, 8), setpoint=22.0)
    sc.vote(t(1, 10), "too_cold", readings={"wall": 23.0, "door": 26.0})
    day = sc.replay(t(1, 10, 1))["day"]
    assert day.status == "learned" and day.unit_setpoints == {"ac": 22.5}
    assert day.unit_setpoints_applied == {"ac": 23.0}, day.unit_setpoints_applied
    assert day.sensor_comfort == {"wall": 24.5, "door": 26.0}, day.sensor_comfort
    assert day.sensor_band == {"wall": 3.5, "door": 5.0}, day.sensor_band
    ok("a too_cold press, applied 22: S 22.5 and the applied setpoint a step up to 23; comfort "
       "halfway to reading + 1 (wall 25 -> 24.5 toward 24, door 25 -> 26.0 toward 27); wall, "
       "2.0 below on the person's side, band 5.0 -> 3.5; door, on the other side, held at "
       "5.0 - all straight away, before any window")

    sc.vote(t(1, 12), "too_cold", readings={"wall": 24.5, "door": 26.0})
    day = sc.replay(t(1, 12, 1))["day"]
    assert day.unit_setpoints == {"ac": 23.25} and day.unit_setpoints_applied == {"ac": 24.0}
    ok("a second vote steps from the applied setpoint in force: 23 -> 24 (S 23.25)")

    sc = Scenario()
    sc.beat(t(1, 8), setpoint=22.0)
    sc.vote(t(1, 10), "too_hot", readings={"wall": 26.0, "door": 25.0}, chosen=20.0)
    day = sc.replay(t(1, 10, 1))["day"]
    assert day.unit_setpoints == {"ac": 21.0} and day.unit_setpoints_applied == {"ac": 21.0}
    assert day.sensor_comfort == {"wall": 25.0, "door": 24.5}, day.sensor_comfort
    ok("a remote's too_hot to 20: S 21, applied a step down to 21 (toward 20, not past it); "
       "comfort halfway to reading - 1 (wall 25 -> 25.0 toward 25, door 25 -> 24.5 toward 24)")

    sc = Scenario()
    sc.beat(t(1, 8), setpoint=22.0)
    sc.vote(t(1, 10), "too_cold", readings={"wall": 23.0, "door": 26.0})
    half = sc.replay(t(1, 10, 1), LearningConfig(person_step_degrees=0.5))["day"]
    assert half.sensor_comfort == {"wall": 24.25, "door": 25.75}, half.sensor_comfort
    ok("person_step_degrees 0.5: wall 25 -> 24.25 (toward 23.5), door 25 -> 25.75")

    def windowed(state: str) -> dict:
        sc = Scenario()
        sc.beat(t(1, 8), setpoint=22.0)
        sc.vote(t(1, 10), "too_cold", readings={"wall": 23.0, "door": 26.0})
        for minute in (30, 40, 50, 60):
            sc.beat(t(1, 10) + timedelta(minutes=minute), readings={"wall": 24.0, "door": 24.0},
                    state=state)
        return sc.replay(t(1, 23))["day"].sensor_comfort
    assert windowed("rest") == {"wall": 24.5, "door": 26.0}
    assert windowed("user_off") == {"wall": 24.25, "door": 25.0}
    ok("its forced rest disqualifies the vote's comfort window (only the vote's own move "
       "stands); with no rest or boost in it (a unit left off) the window applies on top")

    sc = Scenario()
    sc.beat(t(1, 8), setpoint=22.0)
    sc.react(t(1, 10), 22.0, 23.0, readings={"wall": 23.0, "door": 26.0})
    day = sc.replay(t(1, 10, 1))["day"]
    assert day.sensor_comfort == {"wall": 25.0, "door": 25.0}
    assert day.sensor_band == {"wall": 3.5, "door": 5.0}
    ok("a `setpoint` row recorded before the votes moves no comfort straight away (only its "
       "window does), but its band nudge is the same rule")


def check_band_nudge() -> None:
    print("bands: one nudge per reaction")
    cfg = LearningConfig()
    cases = [(5.0, -1.0, 1, 3.0, "the person's side, inside the band: toward |d|"),
             (1.0, -2.0, 1, 0.9, "the person's side, past the band: toward 0.8 x band"),
             (1.0, 1.5, 1, 1.75, "the other side: toward band + |d|"),
             (2.0, 1.0, -1, 1.5, "too hot, above comfort, inside the band: toward |d|"),
             (2.0, -1.0, -1, 2.5, "too hot, below comfort: toward band + |d|"),
             (0.5, -3.0, 1, 0.5, "past the band at the floor: clamped to 0.5"),
             (5.0, 2.0, 1, 5.0, "the other side at the ceiling: clamped to 5.0"),
             (3.0, 0.0, 1, 3.0, "right at comfort: no change"),
             (3.0, -1.0, 0, 3.0, "no direction (manual): no change")]
    for band, d, want, expect, what in cases:
        got = nudge_band(band, d, want, cfg)
        assert abs(got - expect) < 1e-9, (what, got)
        ok(f"band {band}, d {d:+}, {'warmer' if want > 0 else 'cooler' if want < 0 else 'none'}"
           f" wanted -> {got:g} ({what})")

    sc = Scenario()
    sc.beat(t(1, 8))
    sc.react(t(1, 10), 21.0, 22.0, readings={"wall": 25.0, "door": 25.0})
    sc.beats(t(1, 10, 30), t(1, 11), readings={"wall": 23.0, "door": 25.0})
    sc.react(t(1, 14), 22.0, 23.0, readings={"wall": 22.5, "door": 25.0})
    day = sc.replay(t(1, 23))["day"]
    assert day.sensor_comfort["wall"] == 24.0
    assert abs(day.sensor_band["wall"] - 3.25) < 1e-9, day.sensor_band
    ok("deviation is taken against comfort as replayed then (24.0 after the first window), "
       "so 22.5 is 1.5 off, not 2.5: band 5.0 -> 3.25")

    sc = Scenario()
    sc.beat(t(1, 8))
    sc.vote(t(1, 10), "too_cold", readings={"wall": 24.0, "door": 25.0})    # wall 1.0 below
    after_one = sc.replay(t(1, 11))["day"].sensor_band
    sc.beats(t(1, 11), t(9, 11), readings={"wall": 20.0, "door": 30.0})     # a week of quiet
    assert sc.replay(t(9, 12))["day"].sensor_band == after_one == {"wall": 3.0, "door": 5.0}
    ok("a week of quiet heartbeats far from comfort leaves the bands where the last reaction "
       "put them (no recency decay)")


def check_on_off_manual() -> None:
    print("on / off / manual reactions")
    readings = {"wall": 23.5, "door": 26.5}      # wall 1.5 below comfort, door 1.5 above
    for kind, learns, ignores in (("on", "door", "wall"), ("off", "wall", "door")):
        sc = Scenario()
        sc.beat(t(1, 8))
        sc.react(t(1, 10), 21.0, 21.0, readings=readings, kind=kind)
        day = sc.replay(t(1, 23))["day"]
        assert abs(day.sensor_band[learns] - 3.25) < 1e-9 and day.sensor_band[ignores] == 5.0
        assert day.unit_setpoints == {"ac": 21.0} and day.status == "provisional"
        ok(f"`{kind}` ({'cooler' if kind == 'on' else 'warmer'} wanted) tightens {learns}, on "
           f"the {'warm' if kind == 'on' else 'cold'} side (5.0 -> 3.25), and widens "
           f"{ignores}, held at 5.0; S stays put")

    sc = Scenario()
    sc.beat(t(1, 8))
    sc.react(t(1, 10), 21.0, 21.0, readings=readings, kind="manual")
    day = sc.replay(t(1, 23))["day"]
    assert day.sensor_band == {"wall": 5.0, "door": 5.0} and day.unit_setpoints == {"ac": 21.0}
    assert day.status == "provisional"
    ok("`manual` (a mode or fan change) teaches nothing")

    # a vote's readings a step from comfort, so its own comfort move is nil
    at_comfort = {"too_cold": {"wall": 24.0, "door": 24.0},
                  "too_hot": {"wall": 26.0, "door": 26.0}}
    for kind in ("on", "off", "manual", None, "too_cold", "too_hot"):
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
        sc.react(t(1, 10, 45), 22.0, 22.0, kind=kind or "setpoint",
                 readings=at_comfort.get(kind))
        assert sc.replay(t(1, 23))["day"].sensor_comfort == {"wall": 25.0, "door": 25.0}, kind
    ok("every kind - on, off, manual, setpoint, too_cold, too_hot - resets the quiet count "
       "(0.75 -> 0.370) and disqualifies a comfort window it falls inside")


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


def check_margin() -> None:
    print("the block margin: within 0 to 10")
    assert (MARGIN_MIN, MARGIN_MAX) == (0.0, 10.0)
    assert margin_step(1.0, -3.0, 0.5) == 0.0 and margin_step(0.0, -3.0, 0.5) == 0.0
    assert margin_step(0.0, 0.9, 0.5) == 0.45
    assert margin_step(9.0, 12.5, 0.5) == 10.0 and margin_step(10.0, 3.0, 0.5) == 6.5
    ok("a step toward -3 from 1 (or from 0) stops at 0; toward 12.5 from 9 stops at 10; "
       "inside the range it is halfway")

    def decided(sc: Scenario, start: datetime, section: str, comfort: float, mx: float,
                blocked: bool) -> None:
        """A section run decided in shadow mode at its crossover."""
        day = start.date().isoformat()
        sc.store.record_section_run("lounge", day, section,
                                    planned_start=start.isoformat(timespec="seconds"),
                                    actual_start=start.isoformat(timespec="seconds"))
        sc.store.record_section_outdoor("lounge", day, section, mx - 1.0, mx)
        sc.store.record_section_conditions("lounge", day, section, comfort, 0.1, blocked,
                                           "shadow")

    def margin(sc: Scenario, as_of: datetime, initial: float) -> tuple[float, list]:
        return learn_margin(sc.store._conn, ROOM, MarginConfig(initial=initial), as_of)

    sc = Scenario()
    # Day, unblocked: the forecast max 27.0 is 3 above the comfort point
    # (delta max -3). The scene starts cooling at 08:00; a too_cold vote at
    # 08:15 pulls M toward -3.
    decided(sc, t(2, 8), "day", 24.0, 27.0, False)
    sc.beat(t(2, 8, 5), state="cool")
    sc.vote(t(2, 8, 15), "too_cold")
    m, steps = margin(sc, t(2, 8, 16), 1.0)
    assert m == 0.0 and [(st.before, st.target, st.after) for st in steps] == [(1.0, -3.0, 0.0)]
    ok("learned down from 1: a too_cold vote 15 min after Day's scene started cooling, delta "
       "max -3, would take it halfway to -1; it stops at 0, the comfort point itself")
    sc.vote(t(2, 8, 25), "too_cold")
    m, steps = margin(sc, t(2, 8, 26), 1.0)
    assert m == 0.0 and steps[-1].after == 0.0
    ok("a second one leaves it at 0, never negative")
    # Sunset, decided blocked at M 0: the max 23.6 is 0.4 below the comfort
    # point. A too_hot vote there pulls M back up toward 0.9.
    decided(sc, t(2, 16), "sunset", 24.0, 23.6, True)
    sc.vote(t(2, 16, 10), "too_hot", section="sunset")
    m, steps = margin(sc, t(2, 16, 11), 1.0)
    assert m == 0.45 and steps[-1].target == 0.9, (m, steps[-1])
    ok("and it climbs again: a too_hot vote in Sunset, blocked at delta max 0.4, moves it "
       "from 0 halfway to 0.9")

    sc = Scenario()
    decided(sc, t(3, 20, 30), "night", 24.0, 12.0, True)
    sc.vote(t(3, 20, 40), "too_hot", section="night")
    m, _ = margin(sc, t(3, 20, 41), 9.0)
    assert m == 10.0
    ok("capped at 10: from 9 toward 12.5 (a too_hot vote in a Night blocked at delta max 12)")


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
    check_votes()
    check_band_nudge()
    check_on_off_manual()
    check_confidence()
    check_margin()
    check_missing_seed()
    print("\nALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
