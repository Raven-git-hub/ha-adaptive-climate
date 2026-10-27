"""
Adaptive Climate - analyser comparison: frozen v1 against the v2 replay.

Runs the v1 analyser as tagged v1.0-poc (tools/fixtures/analyser_v1.py) and
the v2 reaction replay (app/analyser.py) over the same store, and diffs what
each learns: per unit the learned setpoint and the setpoint actually
applied, per sensor the comfort reading, band and trust.

v1 had no applied setpoint of its own: its templates rounded the learned
setpoint with Jinja's round(), whose ties go to even, so that is what
"applied" means for v1 here. v1 learned from the complete days before its
as_of, so it is given the day after --as-of to cover the same data.

    python tools/compare_analyser.py DATA_DIR [--config FILE] [--as-of ISO] [--json]

DATA_DIR is a data directory (db/, csv/, config/). Point it at a COPY of
real data: the store opens it the normal way, so a v1 or phase 0 database
is backed up and migrated in place first. --config defaults to
DATA_DIR/config/config.json (version 1 or 2; migrated in memory). --as-of
defaults to now; a bare date means the end of that day, local time.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.analyser import SECTIONS, LearningConfig, analyse_room   # noqa: E402
from app.config import migrate                                     # noqa: E402
from app.store import Store                                        # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "analyser_v1.py"
SCHEMA = ROOT / "schema" / "storage.schema.sql"


def load_v1():
    spec = importlib.util.spec_from_file_location("analyser_v1", FIXTURE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["analyser_v1"] = module         # dataclasses need it registered
    spec.loader.exec_module(module)
    return module


def v1_applied(setpoint: float | None) -> float | None:
    # What v1's templates drove: Jinja's round(), i.e. Python's, ties to even.
    return None if setpoint is None else float(round(setpoint))


def _v1_config(v1, raw: dict):
    """v1's learning knobs, from the config as written if it still has
    them (a version 1 config), else v1's defaults."""
    learn = raw.get("learning", {}) or {}
    trust = (raw.get("system", {}) or {}).get("trust", {}) or {}
    base = v1.LearningConfig()
    return v1.LearningConfig(**{
        **{k: learn[k] for k in ("analysis_window_days", "bootstrap_min_days",
                                 "validity_delay_days", "reactive_weight") if k in learn},
        "recency_half_life_days": learn.get("recency_half_life_days",
                                            base.recency_half_life_days),
        "high_trust_deviation": trust.get("high_trust_deviation", base.high_trust_deviation),
        "low_trust_deviation": trust.get("low_trust_deviation", base.low_trust_deviation),
    })


def compare(conn, raw_config: dict, as_of: datetime) -> list[dict]:
    """One row per (room, section, unit or sensor) that either analyser
    has an opinion on, with both sides' values."""
    v1 = load_v1()
    config = migrate(raw_config)
    lc1, lc2 = _v1_config(v1, raw_config), LearningConfig.from_config(config)
    rows: list[dict] = []
    for room in config.get("rooms", []):
        if not room.get("enabled", True):
            continue
        old = {s.section: s for s in v1.analyse_room(
            conn, room, lc1, (as_of + timedelta(days=1)).date())}
        new = {s.section: s for s in analyse_room(conn, room, lc2, as_of)}
        for sec in SECTIONS:
            a, b = old.get(sec), new.get(sec)
            if a is None and b is None:
                continue
            for u in room["units"]:
                sp1 = a.unit_setpoints.get(u["id"]) if a else None
                sp2 = b.unit_setpoints.get(u["id"]) if b else None
                if sp1 is None and sp2 is None:
                    continue
                rows.append({
                    "room": room["id"], "section": sec, "kind": "unit", "id": u["id"],
                    "v1": {"setpoint": sp1, "applied": v1_applied(sp1),
                           "state": a.state if a else None},
                    "v2": {"setpoint": sp2,
                           "applied": b.unit_setpoints_applied.get(u["id"]) if b else None,
                           "status": b.status if b else None},
                })
            for s in room["sensors"]:
                c1 = a.sensor_comfort.get(s["id"]) if a else None
                c2 = b.sensor_comfort.get(s["id"]) if b else None
                if c1 is None and c2 is None:
                    continue
                rows.append({
                    "room": room["id"], "section": sec, "kind": "sensor", "id": s["id"],
                    "v1": {"comfort": c1, "band": a.sensor_band.get(s["id"]) if a else None,
                           "trust": a.sensor_trust.get(s["id"]) if a else None},
                    "v2": {"comfort": c2, "band": b.sensor_band.get(s["id"]) if b else None,
                           "trust": b.sensor_trust.get(s["id"]) if b else None},
                })
    return rows


def _f(v, fmt: str = "5.2f") -> str:
    return "  -  " if v is None else format(v, fmt)


def report(rows: list[dict]) -> str:
    lines = [f"{'room':<14}{'section':<10}{'unit / sensor':<16}"
             f"{'v1 setpoint/applied':>21}   {'v2 setpoint/applied':>21}"]
    for r in rows:
        head = f"{r['room']:<14}{r['section']:<10}{r['id']:<16}"
        a, b = r["v1"], r["v2"]
        if r["kind"] == "unit":
            flag = "   <- applied differs" if a["applied"] != b["applied"] else ""
            lines.append(f"{head}{_f(a['setpoint']):>13} {_f(a['applied'], '3.0f'):>6}   "
                         f"{_f(b['setpoint']):>13} {_f(b['applied'], '3.0f'):>6}"
                         f"  {b['status'] or '-'}{flag}")
        else:
            lines.append(f"{head}  comfort {_f(a['comfort'])} / {_f(b['comfort'])}"
                         f"  band {_f(a['band'])} / {_f(b['band'])}"
                         f"  trust {_f(a['trust'], '4.2f')} / {_f(b['trust'], '4.2f')}")
    units = [r for r in rows if r["kind"] == "unit"]
    differ = sum(r["v1"]["applied"] != r["v2"]["applied"] for r in units)
    lines.append(f"\n{len(units)} unit setpoint(s) compared; {differ} would apply a "
                 f"different temperature under v2.")
    return "\n".join(lines)


def _as_of(text: str | None) -> datetime:
    if not text:
        return datetime.now().astimezone()
    when = datetime.fromisoformat(text)
    if len(text) == 10:                      # a bare date: the end of that day
        when = datetime.combine(when.date(), time(23, 59, 59))
    return when if when.tzinfo else when.astimezone()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("data_dir", type=Path)
    ap.add_argument("--config", type=Path)
    ap.add_argument("--as-of")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    config_path = args.config or args.data_dir / "config" / "config.json"
    raw = json.loads(config_path.read_text())
    store = Store(args.data_dir, SCHEMA)
    try:
        rows = compare(store._conn, raw, _as_of(args.as_of))
    finally:
        store.close()
    print(json.dumps(rows, indent=2) if args.json else report(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
