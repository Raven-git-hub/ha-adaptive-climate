"""
Adaptive Climate - store write/read offline check.

Writes heartbeats and a reaction through the real Store API (CSV + SQLite),
confirms both the CSV archive and the SQLite tables contain them, exercises
section runs / events / config versioning / activity(), and then runs the
analyser over what was written to confirm the whole observe->learn path
holds together end to end.

    PYTHONPATH=. python tools/store_check.py
"""
from __future__ import annotations

import csv as csvmod
import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analyser import LearningConfig, analyse_room          # noqa: E402
from app.store import (SensorSample, Store, UnitSample,        # noqa: E402
                       ReactiveUnitSample)

SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "storage.schema.sql"
ROOM = {"id": "main_room", "units": [{"id": "main_room_ac"}],
        "sensors": [{"id": "living1"}, {"id": "living2"}, {"id": "living3"}]}
AS_OF = date(2026, 8, 1)


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp, SCHEMA)

    # 8 settled days of heartbeats; unit at 22, three sensors at distinct comforts
    for i in range(8, 0, -1):
        d = (AS_OF - timedelta(days=i)).isoformat()
        store.record_heartbeat(
            "main_room", f"{d}T14:10:00+08:00", f"{d}T06:10:00+00:00", d, "day",
            sensors={"living1": 24.9, "living2": 24.4, "living3": 25.3},
            units={"main_room_ac": UnitSample(True, "cool", "low", 22.0, 23.0, "normal")},
        )

    # one reaction on the most recent day: living1 drifted to 23.9, user bumped 22->23
    rd = (AS_OF - timedelta(days=1)).isoformat()
    store.record_reactive(
        "main_room", f"{rd}T14:40:00+08:00", f"{rd}T06:40:00+00:00", rd, "day",
        window_seconds=120,
        units={"main_room_ac": ReactiveUnitSample(22.0, 23.0, True)},
        sensors={"living1": 23.9, "living2": 24.4, "living3": 25.3},
    )

    # section run + event + config version
    store.record_section_run("main_room", rd, "day",
                             planned_start=f"{rd}T08:00:00+08:00",
                             actual_start=f"{rd}T08:00:03+08:00")
    store.close_section_run("main_room", rd, "day", f"{rd}T14:00:00+08:00")
    store.log_event(f"{rd}T14:40:00+08:00", f"{rd}T06:40:00+00:00", "info",
                    "reactive", "user bumped main_room_ac 22->23", room_id="main_room")
    store.save_config_version({"version": 1, "rooms": []})
    store.save_config_version({"version": 1, "rooms": []})  # dup -> ignored

    # --- CSV archive present and correct ---
    hb_csv = tmp / "csv" / "main_room" / f"{(AS_OF - timedelta(days=1)).isoformat()}_heartbeat.csv"
    assert hb_csv.exists(), "heartbeat CSV missing"
    rows = list(csvmod.DictReader(hb_csv.open()))
    assert rows and json.loads(rows[0]["sensors"])["living1"] == 24.9
    rx_csv = tmp / "csv" / "main_room" / f"{rd}_reactive.csv"
    assert rx_csv.exists(), "reactive CSV missing"
    print("CSV archive: heartbeat + reactive files written with correct contents - OK")

    # --- SQLite row counts ---
    c = store._conn
    assert c.execute("SELECT COUNT(*) FROM heartbeat WHERE room_id='main_room'").fetchone()[0] == 8
    assert c.execute("SELECT COUNT(*) FROM heartbeat_sensor").fetchone()[0] == 24  # 8*3
    assert c.execute("SELECT COUNT(*) FROM reactive").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM config_version").fetchone()[0] == 1  # dedup worked
    print("SQLite: 8 heartbeats, 24 sensor rows, 1 reactive, config dedup - OK")

    # --- reads ---
    evs = store.recent_events(room_id="main_room")
    assert len(evs) == 1 and evs[0]["category"] == "reactive"
    act = store.activity("main_room", rd)
    assert len(act["heartbeats"]) == 1 and len(act["reactions"]) == 1
    assert act["section_runs"][0]["ended_at"] == f"{rd}T14:00:00+08:00"
    print("reads: recent_events + activity(day) return written data, section closed - OK")

    # --- end to end: analyser over what the store recorded ---
    sections = analyse_room(store._conn, ROOM, LearningConfig(), AS_OF)
    day = next(s for s in sections if s.section == "day")
    sp = day.unit_setpoints["main_room_ac"]
    # reaction on living1 at reading 23.9 vs its comfort 24.9 -> band ~1.0, trust up
    b1 = day.sensor_band["living1"]
    print(f"analyser over stored data: setpoint={sp:.3f}, living1 band=±{b1:.3f} "
          f"trust={day.sensor_trust['living1']:.3f}")
    assert 22.0 < sp <= 23.0, sp                     # pulled up by the 5x reaction
    assert abs(b1 - 1.0) < 1e-6, b1                  # |24.9 - 23.9|
    assert day.sensor_band["living2"] == 5.0          # never reacted -> widest
    print("end-to-end observe->learn path holds together")

    store.close()
    print("\nALL STORE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
