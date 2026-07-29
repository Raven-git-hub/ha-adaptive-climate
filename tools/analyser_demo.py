"""
Adaptive Climate - analyser worked-example harness.

Seeds a synthetic SQLite store with heartbeats + one reaction, runs the
analyser, persists via Store.publish_almanac, reads it back through
Store.current_almanac, and asserts the docs/TRUST_MODEL.md worked example:

  * unit setpoint learned ~21, pulled up toward 22 by the 5x reaction
  * sensor comfort stays ~25
  * band tightens to ~1.0 (reaction at |25 - 24|), trust ~0.89

Runs without Docker or Home Assistant:  PYTHONPATH=. python tools/analyser_demo.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analyser import LearningConfig, analyse_room          # noqa: E402
from app.store import Store                                    # noqa: E402

SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "storage.schema.sql"

ROOM = {"id": "lounge",
        "units": [{"id": "lounge_ac"}],
        "sensors": [{"id": "wall"}]}
SECTION = "day"
AS_OF = date(2026, 8, 1)


def _seed(store: Store) -> None:
    c = store._conn
    # 8 settled days: unit sits at 21, wall sensor reads a steady 25.
    for i in range(8, 0, -1):
        d = (AS_OF - timedelta(days=i)).isoformat()
        cur = c.execute(
            "INSERT INTO heartbeat(room_id,ts,ts_utc,local_date,section,"
            "sensor_n,any_unit_on) VALUES(?,?,?,?,?,?,?)",
            ("lounge", f"{d}T14:10:00+08:00", f"{d}T06:10:00+00:00", d,
             SECTION, 1, 1))
        hb = cur.lastrowid
        c.execute("INSERT INTO heartbeat_unit(heartbeat_id,unit_id,is_on,"
                  "hvac_mode,fan_mode,setpoint,current_temp,ac_state) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (hb, "lounge_ac", 1, "cool", "low", 21.0, 23.0, "normal"))
        c.execute("INSERT INTO heartbeat_sensor(heartbeat_id,sensor_id,temperature) "
                  "VALUES(?,?,?)", (hb, "wall", 25.0))

    # One reaction on the most recent day: wall had drifted to 24, user
    # bumped the setpoint 21 -> 22 ("too cold now").
    rd = (AS_OF - timedelta(days=1)).isoformat()
    cur = c.execute(
        "INSERT INTO reactive(room_id,ts,ts_utc,local_date,section,"
        "window_seconds) VALUES(?,?,?,?,?,?)",
        ("lounge", f"{rd}T14:40:00+08:00", f"{rd}T06:40:00+00:00", rd, SECTION, 120))
    rid = cur.lastrowid
    c.execute("INSERT INTO reactive_unit(reactive_id,unit_id,setpoint_before,"
              "setpoint_after,changed) VALUES(?,?,?,?,?)",
              (rid, "lounge_ac", 21.0, 22.0, 1))
    c.execute("INSERT INTO reactive_sensor(reactive_id,sensor_id,temperature) "
              "VALUES(?,?,?)", (rid, "wall", 24.0))
    c.commit()


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp, SCHEMA)
    _seed(store)

    sections = analyse_room(store._conn, ROOM, LearningConfig(), AS_OF)
    day = next(s for s in sections if s.section == SECTION)

    print(f"section={day.section} state={day.state} sample_days={day.sample_days} "
          f"valid_from={day.valid_from} confidence={day.confidence}")
    sp = day.unit_setpoints["lounge_ac"]
    band = day.sensor_band["wall"]
    trust = day.sensor_trust["wall"]
    comfort = day.sensor_comfort["wall"]
    print(f"  unit setpoint = {sp:.3f}  (learned 21, pulled toward 22 by 5x reaction)")
    print(f"  sensor comfort = {comfort:.3f}  band = ±{band:.3f}  trust = {trust:.3f}")

    # persist + read back through the public store API. A learning-state
    # almanac only comes into force on its valid_from (the validity delay),
    # so we read as of that date.
    store.publish_almanac("lounge", sections)
    served = store.current_almanac("lounge", as_of=day.valid_from)
    print("\nserved almanac (docs/ALMANAC_FORMAT.md shape):")
    print(json.dumps(served["sections"][SECTION], indent=2))

    # assertions
    assert day.state == "learning", day.state
    assert 21.0 < sp < 22.0, sp                      # nudged up, not all the way
    assert abs(comfort - 25.0) < 1e-6, comfort       # comfort unmoved
    assert abs(band - 1.0) < 1e-6, band              # band == reaction deviation
    assert abs(trust - 0.889) < 0.01, trust
    served_day = served["sections"][SECTION]
    assert served_day["units"]["lounge_ac"]["setpoint"] == sp
    assert served_day["sensors"]["wall"]["band"] == band
    print("\nALL ASSERTIONS PASSED")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
