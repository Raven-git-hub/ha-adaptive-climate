"""
Adaptive Climate - Now view render check (phase 6).

Builds /api/now's payload for each case through the real app.nowview (via
tools/now_cases.py), then loads the real UI (app/static) in headless
Chromium with that payload stubbed in (tools/now_render.js, Playwright), and
checks what is drawn: the label strip, the lock, the fan, the markers
(placed, hollow, pinned with an overflow tick, and where the leaned edges
land), the table's order and notes, the buttons and the status strip. It
also presses the buttons against a stubbed API, checks the fan spins about
its own centre, and saves a screenshot of each case to docs/screenshots/.

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
PHONE = {"width": 390, "height": 844}

# Readings that put the three sensors in an order of their own, warmest first.
COOL = (24.3, 23.8, 24.6)


def case(name: str, expect: dict, *, viewport=DESKTOP, scheme="dark", full=False,
         interact=None, **world) -> dict:
    return {"name": name, "data": payload(**world), "expect": expect, "viewport": viewport,
            "scheme": scheme, "full": full, "interact": interact}


def cases() -> list[dict]:
    base_rows = ["living3", "living1", "living2"]
    return [
        case("cool", {"label": "Cool · Low", "sub": "", "lock": None, "fan": "slow",
                      "placed": 3, "hollow": 0, "overflow": 0, "rows": base_rows,
                      "setpoint": "23°", "driving": "driving 23°", "chevrons": 0,
                      "forecast": True, "forecast_pinned": False},
             readings=COOL),
        case("boost", {"label": "Boost · Medium", "sub": "", "fan": "fast",
                       "driving": "driving 20°", "chevrons": 1, "offset": "offset −1",
                       "quorum": "Quorum met, too warm: 2 votes (2 needed)"},
             state="boost", offset=-1, readings=(25.2, 24.1, 24.9)),
        case("rest", {"label": "Resting", "sub": "", "fan": "stopped",
                      "driving": "not cooling", "quorum": "Quorum met, too cold: 2 votes (2 needed)"},
             state="rest", readings=(22.4, 23.9, 22.6)),
        case("forced-rest", {"label": "Resting", "sub": "you said too cold", "fan": "stopped"},
             state="rest", forced=True, base=24.0, readings=(23.2, 23.5, 23.9)),
        case("forced-boost", {"label": "Boost · Medium", "sub": "you said too hot",
                              "fan": "fast", "driving": "driving 20°"},
             state="boost", forced=True, base=22.0, readings=(25.4, 24.6, 25.1)),
        case("user-off", {"label": "Off (by you)", "sub": "", "fan": "stopped"},
             state="user_off", readings=COOL),
        case("manual", {"label": "Manual", "sub": "dry · auto", "fan": "slow"},
             state="manual", readings=COOL),
        case("config-off", {"label": "Off (scheduled)", "sub": "", "fan": "stopped"},
             state="config_off", readings=COOL),
        case("leak", {"label": "Leak · Off", "sub": "until it's fixed", "fan": "stopped",
                      "leak_button": True, "strip_kinds": ["leak"]},
             state="leak", leak=True, readings=COOL, interact="leak"),
        case("blocked", {"label": "Resting", "lock": "blocked", "dashed": False,
                         "warm_off": True},
             state="rest", block_mode="active", forecast=(22.0, 24.5), readings=COOL),
        case("shadow-would-block", {"label": "Cool · Low", "lock": "would block",
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
        # exactly on the cold edge: they must land on the arc boundaries.
        case("strong-lean", {"placed": 3, "rows": ["living1", "living3", "living2"],
                             "angles": {"living1": 45.0, "living2": -45.0, "living3": 16.88},
                             "edges": [-0.4, 1.6], "forecast_pinned": True},
             readings=(25.6, 23.6, 24.6), forecast=(18.0, 20.0)),
        case("two-units", {"label": "Cool · Low", "unit_chips": 1},
             readings=COOL, second_unit="rest"),
        case("light", {"label": "Cool · Low"}, scheme="light", readings=COOL),
        case("phone-dark", {"label": "Boost · Medium", "sub": "you said too hot"},
             viewport=PHONE, full=True, state="boost", forced=True, base=22.0,
             readings=(25.4, 24.6, 25.1), interact="vote"),
        case("phone-light", {"label": "Resting", "lock": "would block"}, viewport=PHONE,
             full=True, scheme="light", state="rest", forecast=(22.0, 24.5),
             readings=(22.6, 23.9, 22.9), stale={"living3": 120}),
        case("overview", {"strip_kinds": ["hold", "stale"], "rooms": 3}, full=True,
             rooms="all", hold=True, readings=COOL, stale={"living2": 180}),
    ]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
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
