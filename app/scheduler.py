"""
Adaptive Climate - section scheduler (clock-only).

Level-triggered: a slow loop (in the runtime) asks "which section should be
active now?" and fires a crossover when the answer changes. Startup,
reconnect, clock change and a paused VM all take the same path - there is
no separate catch-up routine.

Climate has NO sun-relative triggers (docs/DESIGN.md D5), so this is far
simpler than Light's scheduler: six fixed clock times, resolved for a
date, sorted, with the active section being the last one whose time has
passed (looking back a day to cover the pre-first-boundary window). No
astral, no sun.sun, no collision policy, no year-ahead scan.

Pure functions over a profile dict + a datetime. tzinfo is threaded through
so the runtime can resolve boundaries in Home Assistant's own timezone
(pulled from /api/config); pass None for naive local times in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

SECTIONS = ("sunrise", "day", "afternoon", "sunset", "night", "sleep")


@dataclass
class Boundary:
    section: str
    name: str
    planned: datetime
    ends: datetime | None = None


def resolve_trigger(trigger: dict, on: date, tzinfo=None) -> datetime:
    """Only 'clock' triggers exist. HH:MM on the given local date."""
    if trigger["type"] != "clock":
        raise ValueError(f"unsupported trigger type: {trigger['type']}")
    hh, mm = (int(x) for x in trigger["time"].split(":"))
    return datetime.combine(on, time(hh, mm), tzinfo=tzinfo)


def _by_id(profile: dict) -> dict:
    return {s["id"]: s for s in profile["sections"]}


def compute_day(profile: dict, on: date, tzinfo=None) -> list[Boundary]:
    """The six sections for `on`, chronologically ordered, each with its
    start (`planned`) and end (`ends`). The last section of the day ends at
    the first section of the next day, so the list covers a full 24h with
    no gap."""
    by_id = _by_id(profile)
    bs = [
        Boundary(section=sid, name=by_id[sid].get("name", sid),
                 planned=resolve_trigger(by_id[sid]["trigger"], on, tzinfo))
        for sid in SECTIONS
    ]
    bs.sort(key=lambda b: b.planned)
    for i, b in enumerate(bs):
        if i < len(bs) - 1:
            b.ends = bs[i + 1].planned
        else:
            # wraps into the next day's earliest section (same clock time)
            b.ends = resolve_trigger(by_id[bs[0].section]["trigger"],
                                     on + timedelta(days=1), tzinfo)
    return bs


def active_section_at(profile: dict, when: datetime,
                      tzinfo=None) -> tuple[str, datetime]:
    """Which section should be active at `when`, and when it started.

    Looks back a day, because between midnight and the day's first boundary
    the active section is yesterday's last. This is exactly the catch-up
    path taken on startup, reconnect, or after downtime - no special case.
    """
    today = when.date()
    todays = compute_day(profile, today, tzinfo)
    past = [b for b in todays if b.planned <= when]
    if past:
        active = past[-1]
        return active.section, active.planned

    # before today's first boundary -> yesterday's last section, still running
    yesterday = compute_day(profile, today - timedelta(days=1), tzinfo)
    active = yesterday[-1]
    return active.section, active.planned
