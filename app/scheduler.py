"""
Adaptive Climate - section scheduler (clock-only).

Level-triggered: a slow loop asks "which section should be active now?"
and fires a crossover when the answer changes. Startup, reconnect, clock
change and paused VM all take the same path.

Climate has NO sun-relative triggers (see docs/DESIGN.md D5), so this is
far simpler than Light's scheduler: six fixed clock times, sorted, with
the active section being the last one whose time has passed (looking back
a day to cover the pre-first-boundary window). No astral, no sun.sun, no
collision policy, no year-ahead scan.

STATUS: skeleton.
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


def compute_day(profile: dict, on: date, tzinfo=None) -> list[Boundary]:
    """Return the six sections for `on`, chronologically ordered, each
    with its start and (from the next) its end.

    TODO(Phase 6): implement - resolve each section's clock trigger, sort
    by time, chain ends.
    """
    raise NotImplementedError("Phase 6")


def active_section_at(profile: dict, when: datetime, tzinfo=None) -> tuple[str, datetime]:
    """Which section should be active now, and when it started. Looks back
    a day, because between midnight and the first boundary the active
    section is yesterday's last. This is the catch-up path on startup.

    TODO(Phase 6).
    """
    raise NotImplementedError("Phase 6")
