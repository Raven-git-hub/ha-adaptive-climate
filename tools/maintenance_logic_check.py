"""
Adaptive Climate - maintenance/quorum logic check.

Renders the generator's `vote` Jinja fragment (the heart of
build_maintenance_automation) through a real Jinja2 engine against mocked
HA globals (states/state_attr/namespace), for a battery of synthetic
sensor scenarios, and cross-checks tally/required/met/direction against
the already-tested app.trust core (evaluate_quorum). This is the closest
check possible without a live Home Assistant trace - it proves the
template's algorithm, not that HA's own Jinja dialect accepts every
construct verbatim (run tools/doctor.py against a live instance for that,
once it gains a template-render check).

    PYTHONPATH=. python tools/maintenance_logic_check.py
"""
from __future__ import annotations

import ast
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jinja2 import Environment          # noqa: E402
from jinja2.utils import Namespace      # noqa: E402

from app import generator as g                                   # noqa: E402
from app.trust import SensorReading, evaluate_quorum              # noqa: E402


def render_vote(sensor_specs, low_dev: float = 5.0) -> dict:
    """sensor_specs: list of (id, entity, reading_or_None, comfort_or_None,
    band, trust). Renders the exact Jinja the generator would emit for a
    room with these sensors, against mocked HA globals."""
    room = {"sensors": [{"id": sid, "entity_id": ent} for sid, ent, *_ in sensor_specs]}
    tmpl_src = g._vote_expr(room, low_dev)

    live = {sid: reading for sid, _, reading, *_ in sensor_specs}
    entity_of = {sid: ent for sid, ent, *_ in sensor_specs}
    sensor_data = {sid: {"comfort": comfort, "band": band, "trust": trust}
                   for sid, _, _, comfort, band, trust in sensor_specs
                   if comfort is not None}

    def states(entity_id: str) -> str:
        for sid, ent in entity_of.items():
            if ent == entity_id:
                r = live[sid]
                return "unavailable" if r is None else str(r)
        return "unknown"

    env = Environment()
    env.globals["states"] = states
    env.globals["namespace"] = Namespace
    out = env.from_string(tmpl_src).render(sensor_data=sensor_data)
    return ast.literal_eval(out)


def reference(sensor_specs) -> object:
    readings = [SensorReading(sid, reading, comfort, band if comfort is not None else 5.0)
                for sid, _, reading, comfort, band, _ in sensor_specs]
    trusts = {sid: trust for sid, _, _, _, _, trust in sensor_specs}
    return evaluate_quorum(readings, trusts)


def _hand_scenarios() -> list:
    return [
        # docs/TRUST_MODEL.md worked example: comfort 25, band 1.0, reading 24 -> warm
        [("a", "sensor.a", 24.0, 25.0, 1.0, 0.889)],
        # real lounge data, quiescent: three distinct comforts, none breaching
        [("living1", "sensor.l1", 24.9, 24.9, 5.0, 0.0),
         ("living2", "sensor.l2", 24.4, 24.4, 5.0, 0.0),
         ("living3", "sensor.l3", 25.3, 25.3, 5.0, 0.0)],
        # same room, tight bands, 2 of 3 breach cold -> quorum ceil(3/2)=2, warm
        [("living1", "sensor.l1", 23.0, 24.9, 1.0, 0.889),
         ("living2", "sensor.l2", 23.0, 24.4, 1.0, 0.889),
         ("living3", "sensor.l3", 25.3, 25.3, 5.0, 0.0)],
        # one sensor unavailable -> quorum recomputed on the remaining 2
        [("living1", "sensor.l1", None, 24.9, 1.0, 0.889),
         ("living2", "sensor.l2", 27.0, 24.4, 1.0, 0.889),
         ("living3", "sensor.l3", 27.0, 25.3, 1.0, 0.889)],
        # mixed direction, trust tie-break: high-trust cold beats low-trust warm
        [("a", "sensor.a", 20.0, 25.0, 1.0, 0.889),
         ("b", "sensor.b", 30.0, 25.0, 5.0, 0.0)],
    ]


def _fuzz_scenarios(n: int, seed: int = 7) -> list:
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        k = rnd.randint(1, 5)
        specs = []
        for i in range(k):
            sid = f"s{i}"
            has_reading = rnd.random() > 0.15
            has_comfort = rnd.random() > 0.1
            reading = round(rnd.uniform(18, 30), 1) if has_reading else None
            comfort = round(rnd.uniform(20, 27), 1) if has_comfort else None
            band = round(rnd.uniform(0.5, 5.0), 2)
            trust = round(rnd.uniform(0.0, 1.0), 2)
            specs.append((sid, f"sensor.{sid}", reading, comfort, band, trust))
        out.append(specs)
    return out


def main() -> int:
    scenarios = _hand_scenarios() + _fuzz_scenarios(40)
    fails = 0
    for i, spec in enumerate(scenarios):
        got = render_vote(spec)
        ref = reference(spec)
        ok = (got["tally"] == ref.tally and got["required"] == ref.required
              and got["met"] == ref.met and got["direction"] == ref.direction)
        if not ok:
            fails += 1
            print(f"[FAIL] scenario {i}: jinja={got}  reference={ref}")

    print(f"{len(scenarios) - fails}/{len(scenarios)} scenarios matched the app.trust reference")
    if fails:
        print("MISMATCH: the generated Jinja disagrees with app.trust")
        return 1
    print("Jinja vote/quorum/direction logic matches app.trust exactly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
