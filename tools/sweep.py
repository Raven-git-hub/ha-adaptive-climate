"""
Adaptive Climate - schedule sweep.

Computes a year of section boundaries for every profile in a config and
asserts they are well-formed: six sections per day, strictly increasing
starts, ends chaining without gap or overlap, and full 24h coverage. Trivial
now that triggers are clock-only, but kept as a regression guard against a
profile with out-of-order or duplicate times.

    PYTHONPATH=. python tools/sweep.py [config.json]
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.scheduler import SECTIONS, active_section_at, compute_day  # noqa: E402


def check_profile(profile: dict, days: int = 366) -> None:
    start = date(2026, 1, 1)
    for i in range(days):
        on = start + timedelta(days=i)
        bs = compute_day(profile, on)

        assert len(bs) == 6, f"{profile['id']} {on}: expected 6 boundaries"
        assert {b.section for b in bs} == set(SECTIONS), \
            f"{profile['id']} {on}: section set mismatch"

        for a, b in zip(bs, bs[1:]):
            assert a.planned < b.planned, \
                f"{profile['id']} {on}: non-increasing starts at {a.section}->{b.section}"
            assert a.ends == b.planned, \
                f"{profile['id']} {on}: gap/overlap {a.section}->{b.section}"

        # last ends exactly one day after the first starts (full wrap)
        assert bs[-1].ends == bs[0].planned + timedelta(days=1), \
            f"{profile['id']} {on}: day does not wrap cleanly"

        # active_section_at agrees at each boundary and just before midnight
        for b in bs:
            sec, _ = active_section_at(profile, b.planned)
            assert sec == b.section, f"{on}: active at {b.planned} != {b.section}"
        # one minute before the first boundary -> yesterday's last section
        pre = bs[0].planned - timedelta(minutes=1)
        sec, _ = active_section_at(profile, pre)
        assert sec == bs[-1].section, \
            f"{on}: pre-first-boundary should be yesterday's last ({bs[-1].section}), got {sec}"


def main(path: str) -> int:
    config = json.loads(Path(path).read_text())
    profiles = config.get("schedule_profiles", [])
    for p in profiles:
        check_profile(p)
        print(f"profile '{p['id']}': a year of boundaries OK "
              f"(6/day, strictly increasing, full 24h wrap, active lookups agree)")
    print(f"swept {len(profiles)} profile(s) - all clean")
    return 0


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else "examples/config.example.json"
    sys.exit(main(p))
