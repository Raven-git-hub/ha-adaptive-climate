"""
Adaptive Climate - Now view render check (phase 6).

Builds /api/now's payload for each case through the real app.nowview (via
tools/now_cases.py), then loads the real UI (app/static) in headless
Chromium with that payload stubbed in (tools/now_render.js, Playwright), and
checks what is drawn: the label strip, the lock, the fan, the markers
(placed, hollow, pinned with an overflow tick, and where the leaned edges
land), the table's order and notes, the buttons and the status strip. It
also presses the vote buttons against a stubbed API (each reply must follow
Home Assistant's report and the direction pressed, and the card the state
that follows), checks the fan spins about its own centre, and saves a
screenshot of each case to docs/screenshots/. The UI is desktop only.

    PYTHONPATH=. python tools/now_render_check.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from now_cases import payload                                   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
DESKTOP = {"width": 1180, "height": 760}

# Readings that put the three sensors in an order of their own, warmest first.
COOL = (24.3, 23.8, 24.6)
FORCED_BOOST = dict(state="boost", forced=True, base=22.0, readings=(25.4, 24.6, 25.1))
FORCED_REST = dict(state="rest", forced=True, base=24.0, readings=(23.2, 23.5, 23.9))


def case(name: str, expect: dict, *, scheme="dark", full=False, interact=None,
         steps=None, shot_step=None, **world) -> dict:
    """steps: presses of the vote buttons, each {press, outcome ({accepted}, or
    None for no report), after (the next /api/now payload, if it changes),
    and what the card must then show (reply, label, sub, centre)};
    shot_step: the step after which the screenshot is taken."""
    return {"name": name, "data": payload(**world), "expect": expect, "viewport": DESKTOP,
            "scheme": scheme, "full": full, "interact": interact, "steps": steps or [],
            "shot_step": shot_step}


def step(press: str, outcome: dict | None, reply: str, after: dict | None = None,
         **expect) -> dict:
    return {"press": press, "outcome": outcome, "reply": reply,
            "after": payload(**after) if after else None, "expect": expect}


def cases() -> list[dict]:
    base_rows = ["living3", "living1", "living2"]
    off = {"centre": "Off", "steps": 0, "correction": None, "learned": "learned 23\u00b0"}
    return [
        case("cool", {"label": "Cool \u00b7 Low", "sub": "", "lock": None, "fan": "slow",
                      "placed": 3, "hollow": 0, "overflow": 0, "rows": base_rows,
                      "centre": "23\u00b0", "steps": 0, "correction": None,
                      "learned": "learned 23\u00b0", "forecast": True, "forecast_pinned": False},
             readings=COOL),
        # Offset -1 in boost: set to 23 - 1 - 2 = 20, three degrees under the
        # learned 23: three chevrons down.
        case("boost", {"label": "Boost \u00b7 Medium", "sub": "", "fan": "fast",
                       "centre": "20\u00b0", "steps": -3, "correction": "\u22123\u00b0",
                       "learned": "learned 23\u00b0",
                       "quorum": "Quorum met, too warm: 2 votes (2 needed)"},
             state="boost", offset=-1, readings=(25.2, 24.1, 24.9)),
        case("rest", {"label": "Resting", "sub": "", "fan": "stopped", **off,
                      "quorum": "Quorum met, too cold: 2 votes (2 needed)"},
             state="rest", readings=(22.4, 23.9, 22.6)),
        case("forced-rest", {"label": "Resting", "sub": "you said too cold", "fan": "stopped",
                             **off}, **FORCED_REST),
        # A too-hot vote moved the base to 22; boosting, the unit is set to 20.
        case("forced-boost", {"label": "Boost \u00b7 Medium", "sub": "you said too hot",
                              "fan": "fast", "centre": "20\u00b0", "steps": -3,
                              "correction": "\u22123\u00b0", "learned": "learned 23\u00b0"},
             **FORCED_BOOST),
        case("user-off", {"label": "Off (by you)", "sub": "", "fan": "stopped", **off},
             state="user_off", readings=COOL),
        # In manual the unit's own setpoint is what it is set to: 25, two over.
        case("manual", {"label": "Manual", "sub": "dry \u00b7 auto", "fan": "slow",
                        "centre": "25\u00b0", "steps": 2, "correction": "+2\u00b0"},
             state="manual", unit_setpoint=25.0, readings=COOL),
        case("config-off", {"label": "Off (scheduled)", "sub": "", "fan": "stopped", **off},
             state="config_off", readings=COOL),
        case("leak", {"label": "Leak \u00b7 Off", "sub": "until it's fixed", "fan": "stopped",
                      "leak_button": True, "strip_kinds": ["leak"], **off},
             state="leak", leak=True, readings=COOL, interact="leak"),
        case("blocked", {"label": "Resting", "lock": "blocked", "dashed": False,
                         "warm_off": True},
             state="rest", block_mode="active", forecast=(22.0, 24.5), readings=COOL),
        case("shadow-would-block", {"label": "Cool \u00b7 Low", "lock": "would block",
                                    "dashed": True, "warm_off": False},
             state="cool", forecast=(22.0, 24.5), readings=COOL),
        case("heat-source", {"label": "Resting", "lock": "heater on", "dashed": False,
                             "warm_off": True},
             state="rest", heat_on=True, readings=COOL),
        case("stale-sensor", {"placed": 2, "hollow": 1, "rows": ["living3", "living1",
                                                                "living2"],
                              "notes": {"living2": "stale since 17:40"},
                              "strip_kinds": ["stale"]},
             readings=COOL, stale={"living2": 180}),
        case("overflow", {"placed": 3, "overflow": 1, "rows": ["living1", "living3", "living2"],
                          "angles": {"living1": 135.0}},
             readings=(30.0, 23.8, 24.6)),
        # Delta +6 (24 inside, 18 outside): the cold edge leans to -0.4 and the
        # warm edge to +1.6. living1 sits exactly on the warm edge, living2
        # exactly on the cold edge: they must land on the arc boundaries. The
        # unit boosts at offset -2: set to 19, four under, three chevrons.
        case("strong-lean", {"placed": 3, "rows": ["living1", "living3", "living2"],
                             "angles": {"living1": 45.0, "living2": -45.0, "living3": 16.88},
                             "edges": [-0.4, 1.6], "forecast_pinned": True,
                             "centre": "19\u00b0", "steps": -3, "correction": "\u22124\u00b0"},
             state="boost", offset=-2, readings=(25.6, 23.6, 24.6), forecast=(18.0, 20.0)),
        case("two-units", {"label": "Cool \u00b7 Low", "unit_chips": 1},
             readings=COOL, second_unit="rest"),
        # Offset +1: set to 24, a chevron up.
        case("light", {"label": "Cool \u00b7 Low", "centre": "24\u00b0", "steps": 1,
                       "correction": "+1\u00b0"},
             scheme="light", offset=1, readings=COOL),
        # The vote buttons at desktop width: each reply comes from Home
        # Assistant's report of the press, and the card follows its state.
        case("vote-too-hot", {"label": "Cool \u00b7 Low"}, readings=COOL, shot_step=0, steps=[
            step("too_hot", {"accepted": True}, "Got it, cooling harder", after=FORCED_BOOST,
                 label="Boost \u00b7 Medium", sub="you said too hot", centre="20\u00b0")]),
        case("vote-too-cold", {"label": "Cool \u00b7 Low"}, readings=COOL, shot_step=1, steps=[
            step("too_cold", {"accepted": True}, "Got it, resting", after=FORCED_REST,
                 label="Resting", sub="you said too cold", centre="Off"),
            step("too_hot", {"accepted": False}, "Already correcting, ignored",
                 label="Resting", sub="you said too cold"),
            step("too_cold", None, "Sent")]),
        case("overview", {"strip_kinds": ["hold", "stale"], "rooms": 3}, full=True,
             rooms="all", hold=True, readings=COOL, stale={"living2": 180}),
    ]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("now-*.png"):          # a case removed takes its screenshot with it
        old.unlink()
    all_cases = cases()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cases.json"
        path.write_text(json.dumps(all_cases))
        proc = subprocess.run(["node", str(ROOT / "tools" / "now_render.js"), str(path),
                               str(ROOT / "app" / "static"), str(OUT)],
                              capture_output=True, text=True, timeout=600)
    print(proc.stdout, end="")
    if proc.returncode:
        print(proc.stderr, end="")
        print(f"\nNOW VIEW RENDER CHECK FAILED ({proc.returncode})")
        return 1
    print(f"\nALL NOW VIEW RENDER CHECKS PASSED ({len(all_cases)} cases, screenshots in "
          f"{OUT.relative_to(ROOT)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
