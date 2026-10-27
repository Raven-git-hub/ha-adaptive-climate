"""
Adaptive Climate - store write/read offline check.

Writes heartbeats and a reaction through the real Store API (CSV + SQLite),
confirms both the CSV archive and the SQLite tables contain them, exercises
section runs / events / config versioning / activity(), and then runs the
analyser over what was written to confirm the whole observe->learn path
holds together end to end.

Then the storage migrations:
  * a database built from the frozen v1 schema (tools/fixtures/) is
    backed up, migrated, keeps every row, and ends up identical in
    sqlite_master to a fresh one
  * a migration forced to fail, by raising half-way or by leaving a row
    that fails foreign_key_check, rolls back and leaves the database
    exactly as it was
  * migration 2 closes the section runs v1 left open, from v1 or from a
    phase 0 database, and a sleep run crossing midnight closes properly
  * a database newer than the code is refused

    PYTHONPATH=. python tools/store_check.py
"""
from __future__ import annotations

import csv as csvmod
import json
import sqlite3
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.store as store_mod                                  # noqa: E402
from app.analyser import LearningConfig, analyse_room          # noqa: E402
from app.store import (HEARTBEAT_CSV, REACTIVE_CSV,            # noqa: E402
                       SECTION_RUN_CSV, ReactiveUnitSample, Store,
                       StoreError, UnitSample)

SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "storage.schema.sql"
V1_SCHEMA = Path(__file__).resolve().parent / "fixtures" / "storage_v1.schema.sql"
ROOM = {"id": "main_room", "units": [{"id": "main_room_ac"}],
        "sensors": [{"id": "living1"}, {"id": "living2"}, {"id": "living3"}]}
AS_OF = date(2026, 8, 1)


def check_fresh_store() -> None:
    tmp = Path(tempfile.mkdtemp())
    store = Store(tmp, SCHEMA)
    c = store._conn
    assert c.execute("PRAGMA user_version").fetchone()[0] == len(store_mod.MIGRATIONS)
    assert store.backup_path is None and not list((tmp / "db").glob("*backup*"))
    print("fresh database: stamped with the latest storage version, no backup - OK")

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
    assert store.close_section_run("main_room", f"{rd}T14:00:00+08:00") == (rd, "day")
    store.log_event(f"{rd}T14:40:00+08:00", f"{rd}T06:40:00+00:00", "info",
                    "reactive", "user bumped main_room_ac 22->23", room_id="main_room")
    store.save_config_version({"version": 2, "rooms": []})
    store.save_config_version({"version": 2, "rooms": []})  # dup -> ignored

    # --- CSV archive present and correct ---
    room_csv = tmp / "csv" / "main_room"
    hb_csv = room_csv / f"{(AS_OF - timedelta(days=1)).isoformat()}_heartbeat.csv"
    assert hb_csv.exists(), "heartbeat CSV missing"
    rows = list(csvmod.DictReader(hb_csv.open()))
    assert rows and json.loads(rows[0]["sensors"])["living1"] == 24.9
    rx_csv = room_csv / f"{rd}_reactive.csv"
    assert rx_csv.exists(), "reactive CSV missing"
    print("CSV archive: heartbeat + reactive files written with correct contents - OK")

    # --- CSV columns: v1's first, v2's appended at the end ---
    v1_hb = ["ts", "ts_utc", "section", "occupied", "any_unit_on", "deferred_ms",
             "sensors", "units"]
    v1_rx = ["ts", "ts_utc", "section", "window_seconds", "occupied", "suspended_maint",
             "units", "sensors"]
    hb_header = next(csvmod.reader(hb_csv.open()))
    rx_header = next(csvmod.reader(rx_csv.open()))
    assert hb_header == HEARTBEAT_CSV and hb_header[:len(v1_hb)] == v1_hb, hb_header
    assert rx_header == REACTIVE_CSV and rx_header[:len(v1_rx)] == v1_rx, rx_header
    sr_csv = room_csv / f"{rd}_section_run.csv"
    sr_rows = list(csvmod.DictReader(sr_csv.open()))
    assert next(csvmod.reader(sr_csv.open())) == SECTION_RUN_CSV
    assert [r["section"] for r in sr_rows] == ["day", "day"], sr_rows
    assert sr_rows[0]["actual_start"] == f"{rd}T08:00:03+08:00" and sr_rows[0]["ended_at"] == ""
    assert sr_rows[1]["ended_at"] == f"{rd}T14:00:00+08:00" and sr_rows[1]["actual_start"] == ""
    print("CSV columns: v2 columns appended after v1's; section runs archived (start + close) - OK")

    # --- a sleep run that crosses midnight closes against the day it began ---
    d0, d1 = rd, AS_OF.isoformat()
    store.record_section_run("main_bedroom", d0, "sleep",
                             planned_start=f"{d0}T22:00:00+08:00",
                             actual_start=f"{d0}T22:00:02+08:00")
    # a catch-up restart at 01:00 inside the same run leaves it open
    assert store.close_section_run("main_bedroom", f"{d1}T01:00:00+08:00",
                                   keep=(d0, "sleep")) is None
    # sunrise next morning closes it, though the close date is a day later
    assert store.close_section_run("main_bedroom", f"{d1}T05:30:01+08:00",
                                   keep=(d1, "sunrise")) == (d0, "sleep")
    runs = [tuple(r) for r in c.execute(
        "SELECT local_date, section, ended_at FROM section_run WHERE room_id='main_bedroom'")]
    assert runs == [(d0, "sleep", f"{d1}T05:30:01+08:00")], runs
    bed_csv = tmp / "csv" / "main_bedroom"
    sleep_rows = list(csvmod.DictReader((bed_csv / f"{d0}_section_run.csv").open()))
    assert [r["ended_at"] for r in sleep_rows] == ["", f"{d1}T05:30:01+08:00"], sleep_rows
    assert not (bed_csv / f"{d1}_section_run.csv").exists(), "close row filed under the close date"
    assert store.close_section_run("main_bedroom", f"{d1}T08:00:00+08:00") is None
    print("section runs: a sleep run crossing midnight closes against its start date, "
          "in SQLite and in its own CSV day file; a same-run restart keeps it open - OK")

    # --- SQLite row counts ---
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

    # --- end to end: the reaction replay over what the store recorded ---
    as_of = datetime.combine(AS_OF, time(0, 15), tzinfo=ZoneInfo("Asia/Hong_Kong"))
    sections = analyse_room(store._conn, ROOM, LearningConfig(), as_of)
    day = next(s for s in sections if s.section == "day")
    sp = day.unit_setpoints["main_room_ac"]
    # reaction on living1 at reading 23.9 vs its comfort 24.9 -> band ~1.0, trust up
    b1 = day.sensor_band["living1"]
    print(f"analyser over stored data: setpoint={sp:.3f}, living1 band=±{b1:.3f} "
          f"trust={day.sensor_trust['living1']:.3f}")
    assert sp == 22.5, sp                            # seed 22, halfway to the chosen 23
    assert abs(b1 - 1.0) < 1e-6, b1                  # |24.9 - 23.9|, warmer wanted, below
    assert day.sensor_band["living2"] == 5.0          # sat at comfort -> learns nothing
    print("end-to-end observe->learn path holds together")

    # --- almanac: status and setpoint_applied stored and served ---
    store.publish_almanac("main_room", sections)
    served = store.current_almanac("main_room", as_of=day.valid_from)["sections"]["day"]
    assert served["status"] == "learned", served
    assert served["units"]["main_room_ac"] == {"setpoint": 22.5, "setpoint_applied": 23.0,
                                               "off": False}
    assert "state" not in served and "sample_days" not in served
    print("almanac: status 'learned' and setpoint_applied 23 stored and served - OK")

    # --- v2 columns round-trip; allowed-value lists are enforced ---
    store.record_heartbeat(
        "baby_room", f"{rd}T15:00:00+08:00", f"{rd}T07:00:00+00:00", rd, "day",
        sensors={"baby": 26.0},
        units={"baby_room_ac": UnitSample(True, "cool", "medium", 22.0, 25.0, "cooling",
                                          control_state="boost", control_offset=-1)},
        outdoor_temp=31.5)
    store.record_reactive(
        "baby_room", f"{rd}T15:05:00+08:00", f"{rd}T07:05:00+00:00", rd, "day",
        window_seconds=120, kind="on", outdoor_temp=31.4,
        units={"baby_room_ac": ReactiveUnitSample(22.0, 22.0, False, kind="on",
                                                  power_before=False, power_after=True)},
        sensors={"baby": 26.1})
    store.record_section_run("baby_room", rd, "day", outdoor_mean=29.0, outdoor_max=32.0,
                             comfort_point=24.1, lean=-0.49, blocked=False,
                             block_mode="shadow")
    for category in ("control", "block", "outdoor", "storage"):
        store.log_event(f"{rd}T15:10:00+08:00", f"{rd}T07:10:00+00:00", "info",
                        category, f"{category} event", room_id="baby_room")
    hb = c.execute("SELECT h.outdoor_temp, hu.control_state, hu.control_offset FROM heartbeat h "
                   "JOIN heartbeat_unit hu ON hu.heartbeat_id=h.id "
                   "WHERE h.room_id='baby_room'").fetchone()
    assert tuple(hb) == (31.5, "boost", -1), tuple(hb)
    rx = c.execute("SELECT r.kind, r.outdoor_temp, ru.kind, ru.power_before, ru.power_after "
                   "FROM reactive r JOIN reactive_unit ru ON ru.reactive_id=r.id "
                   "WHERE r.room_id='baby_room'").fetchone()
    assert tuple(rx) == ("on", 31.4, "on", 0, 1), tuple(rx)
    sr = c.execute("SELECT outdoor_mean, outdoor_max, comfort_point, lean, blocked, block_mode "
                   "FROM section_run WHERE room_id='baby_room'").fetchone()
    assert tuple(sr) == (29.0, 32.0, 24.1, -0.49, 0, "shadow"), tuple(sr)

    # --- every reaction kind round-trips through CSV and SQLite ---
    kinds = {  # minute -> (room kind, setpoint before/after, changed, power before/after)
        20: ("off", 22.0, 22.0, False, True, False),
        25: ("manual", 22.0, 22.0, False, True, True),
        30: ("setpoint", 22.0, 24.0, True, True, True),
        35: ("on", 22.0, 24.0, True, False, True),       # on with a new setpoint
    }
    for minute, (kind, before, after, changed, p0, p1) in kinds.items():
        store.record_reactive(
            "baby_room", f"{rd}T15:{minute}:00+08:00", f"{rd}T07:{minute}:00+00:00", rd, "day",
            window_seconds=120, kind=kind,
            units={"baby_room_ac": ReactiveUnitSample(before, after, changed, kind=kind,
                                                      power_before=p0, power_after=p1)},
            sensors={"baby": 26.1})
    db_rows = [tuple(r) for r in c.execute(
        "SELECT r.kind, ru.kind, ru.setpoint_before, ru.setpoint_after, ru.changed, "
        "ru.power_before, ru.power_after FROM reactive r "
        "JOIN reactive_unit ru ON ru.reactive_id = r.id "
        "WHERE r.room_id = 'baby_room' ORDER BY r.ts")]
    csv_rows = list(csvmod.DictReader((tmp / "csv" / "baby_room" / f"{rd}_reactive.csv").open()))
    assert len(db_rows) == len(csv_rows) == 1 + len(kinds), (db_rows, csv_rows)
    for (kind, before, after, changed, p0, p1), db, row in zip(
            kinds.values(), db_rows[1:], csv_rows[1:]):
        assert db == (kind, kind, before, after, int(changed), int(p0), int(p1)), db
        unit = json.loads(row["units"])["baby_room_ac"]
        assert row["kind"] == kind and unit == {
            "setpoint_before": before, "setpoint_after": after, "changed": changed,
            "kind": kind, "power_before": p0, "power_after": p1}, (row, unit)
    print("reaction kinds: on, off, manual and setpoint, with their power and setpoint "
          "fields, round-trip through the CSV and SQLite alike - OK")
    for bad in ("INSERT INTO heartbeat_unit(heartbeat_id,unit_id,is_on,control_state) "
                "VALUES (1,'x',1,'warming')",
                "INSERT INTO reactive_unit(reactive_id,unit_id,kind) VALUES (1,'x','fan')",
                "INSERT INTO almanac(room_id,section,valid_from,status,built_at) "
                "VALUES ('r','day','2026-08-01','learning','now')",
                "INSERT INTO event(ts,ts_utc,severity,category,message) "
                "VALUES ('t','t','info','nonsense','m')"):
        try:
            c.execute(bad)
        except sqlite3.IntegrityError:
            c.rollback()
        else:
            raise AssertionError(f"accepted a value outside the allowed list: {bad}")
    print("v2 columns: outdoor/control/kind/power/section-run values round-trip; "
          "unknown states, kinds, statuses and categories rejected - OK")
    store.close()


# ---------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------

def build_v1_db(data_dir: Path) -> Path:
    """A database exactly as v1.0-poc would have left it, with a row or two
    in every table."""
    db = data_dir / "db" / "adaptive_climate.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db)
    c.executescript(V1_SCHEMA.read_text())
    c.executescript("""
        INSERT INTO config_version VALUES (1, '2026-07-01T00:00:00', '{"version":1}', 'sha-1');
        -- v1 closed runs by the close date: every sleep run stayed open, and a
        -- restart across a boundary orphaned the run it interrupted. Ids are
        -- deliberately not in time order.
        INSERT INTO section_run(id,room_id,local_date,section,planned_start,actual_start,
                                ended_at,outcome)
          VALUES (1,'main_room','2026-07-31','day','2026-07-31T08:00:00+08:00',
                  '2026-07-31T08:00:03+08:00','2026-07-31T14:00:00+08:00','ran'),
                 (2,'main_room','2026-07-30','sleep','2026-07-30T22:00:00+08:00',
                  '2026-07-30T22:00:02+08:00',NULL,'ran'),
                 (3,'main_room','2026-07-31','sunrise','2026-07-31T05:30:00+08:00',
                  '2026-07-31T05:30:01+08:00','2026-07-31T08:00:02+08:00','ran'),
                 (4,'main_room','2026-07-31','afternoon','2026-07-31T14:00:00+08:00',
                  '2026-07-31T14:00:01+08:00',NULL,'ran'),
                 (5,'main_room','2026-07-31','sunset','2026-07-31T16:00:00+08:00',
                  '2026-07-31T17:10:00+08:00',NULL,'caught_up'),
                 (6,'baby_room','2026-07-30','sleep','2026-07-30T22:00:00+08:00',
                  '2026-07-30T22:00:01+08:00',NULL,'ran'),
                 (7,'baby_room','2026-07-31','sunrise','2026-07-31T05:30:00+08:00',
                  '2026-07-31T05:30:02+08:00',NULL,'ran'),
                 (8,'main_room','2026-07-29','night',NULL,NULL,NULL,'missed');
        INSERT INTO heartbeat(id,room_id,ts,ts_utc,local_date,section,sensor_n,any_unit_on,
                              deferred_ms)
          VALUES (7,'main_room','2026-07-31T14:10:00+08:00','2026-07-31T06:10:00+00:00',
                  '2026-07-31','day',2,1,0),
                 (8,'main_room','2026-07-31T14:20:00+08:00','2026-07-31T06:20:00+00:00',
                  '2026-07-31','day',2,1,1500);
        INSERT INTO heartbeat_sensor VALUES (7,'living1',24.9),(7,'living2',24.4),
                                            (8,'living1',25.1),(8,'living2',NULL);
        INSERT INTO heartbeat_unit(heartbeat_id,unit_id,is_on,hvac_mode,fan_mode,setpoint,
                                   current_temp,ac_state)
          VALUES (7,'main_room_ac',1,'cool','low',22.0,23.0,'normal'),
                 (8,'main_room_ac',1,'fan_only','low',22.0,22.5,'warming');
        INSERT INTO reactive(id,room_id,ts,ts_utc,local_date,section,window_seconds,
                             suspended_maint)
          VALUES (3,'main_room','2026-07-31T14:40:00+08:00','2026-07-31T06:40:00+00:00',
                  '2026-07-31','day',120,1);
        INSERT INTO reactive_unit VALUES (3,'main_room_ac',22.0,23.0,1);
        INSERT INTO reactive_sensor VALUES (3,'living1',23.9),(3,'living2',24.4);
        INSERT INTO almanac(id,room_id,section,valid_from,state,sample_days,confidence,built_at)
          VALUES (1,'main_room','day','2026-07-31','learning',8,1.0,'2026-07-31T00:15:00');
        INSERT INTO almanac_unit VALUES (1,'main_room_ac',22.4,0);
        INSERT INTO almanac_sensor VALUES (1,'living1',24.9,1.0,0.889);
        INSERT INTO event(ts,ts_utc,room_id,severity,category,message) VALUES
          ('2026-07-31T14:40:00+08:00','2026-07-31T06:40:00+00:00','main_room','info',
           'reactive','user changed main_room_ac'),
          ('2026-07-31T14:00:00+08:00','2026-07-31T06:00:00+00:00','main_room','info',
           'crossover','Main Room -> afternoon'),
          ('2026-07-31T00:15:00+08:00','2026-07-30T16:15:00+00:00',NULL,'warning',
           'connection','websocket down');
        INSERT INTO ingest_file VALUES ('csv/main_room/2026-07-31_heartbeat.csv','heartbeat',
          'main_room','2026-07-31','abc',2,0,'2026-07-31T23:00:00');
    """)
    c.commit()
    c.close()
    return db


def _dump(db: Path) -> tuple[int, list[str]]:
    """user_version plus a full SQL dump: equal dumps mean equal databases."""
    c = sqlite3.connect(db)
    try:
        return c.execute("PRAGMA user_version").fetchone()[0], list(c.iterdump())
    finally:
        c.close()


def _shape(db: Path) -> tuple[int, list[tuple]]:
    c = sqlite3.connect(db)
    try:
        return (c.execute("PRAGMA user_version").fetchone()[0],
                c.execute("SELECT type, name, tbl_name, sql FROM sqlite_master "
                          "ORDER BY type, name").fetchall())
    finally:
        c.close()


def _backups(db: Path, label: str = "v1") -> list[Path]:
    return sorted(p for p in db.parent.glob(f"{db.name}.{label}-backup-*")
                  if not p.name.endswith(("-wal", "-shm")))


# What migration 2 should do to build_v1_db's section runs: close each open
# run at the next run's start in its room, leave each room's latest open,
# and leave a run with no start time alone.
EXPECTED_RUNS = [
    (1, "2026-07-31T14:00:00+08:00"),   # already closed
    (2, "2026-07-31T05:30:01+08:00"),   # sleep across midnight -> next sunrise
    (3, "2026-07-31T08:00:02+08:00"),   # already closed
    (4, "2026-07-31T17:10:00+08:00"),   # orphaned by a restart -> the caught-up run
    (5, None),                           # main_room's current run
    (6, "2026-07-31T05:30:02+08:00"),   # other room, its own next run
    (7, None),                           # baby_room's current run
    (8, None),                           # no start time: nothing to order it by
]


def _runs(c) -> list[tuple]:
    return [tuple(r) for r in c.execute("SELECT id, ended_at FROM section_run ORDER BY id")]


def check_migration() -> None:
    tmp = Path(tempfile.mkdtemp())
    db = build_v1_db(tmp)
    v1_dump = _dump(db)
    assert v1_dump[0] == 0

    store = Store(tmp, SCHEMA)
    c = store._conn
    assert c.execute("PRAGMA user_version").fetchone()[0] == len(store_mod.MIGRATIONS) == 2
    assert _backups(db) == [store.backup_path], (_backups(db), store.backup_path)
    assert _dump(store.backup_path) == v1_dump, "backup is not a copy of the v1 database"
    print(f"migration: v1 database backed up to {store.backup_path.name}, now at version 2 - OK")

    def n(table: str) -> int:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    expected = {"config_version": 1, "section_run": 8, "heartbeat": 2, "heartbeat_sensor": 4,
                "heartbeat_unit": 2, "reactive": 1, "reactive_unit": 1, "reactive_sensor": 2,
                "ingest_file": 1, "event": 3 + 1,   # + the migration's own storage event
                "almanac": 0, "almanac_unit": 0, "almanac_sensor": 0}
    assert {t: n(t) for t in expected} == expected, {t: n(t) for t in expected}
    hb = c.execute("SELECT h.deferred_ms, hu.hvac_mode, hu.ac_state, h.outdoor_temp, "
                   "hu.control_state, hu.control_offset FROM heartbeat h "
                   "JOIN heartbeat_unit hu ON hu.heartbeat_id = h.id WHERE h.id = 8").fetchone()
    assert tuple(hb) == (1500, "fan_only", "warming", None, None, None), tuple(hb)
    assert [tuple(r) for r in c.execute(
        "SELECT sensor_id, temperature FROM heartbeat_sensor WHERE heartbeat_id = 8 "
        "ORDER BY sensor_id")] == [("living1", 25.1), ("living2", None)]
    rx = c.execute("SELECT r.window_seconds, ru.setpoint_before, ru.setpoint_after, r.kind, "
                   "ru.kind, ru.power_before FROM reactive r "
                   "JOIN reactive_unit ru ON ru.reactive_id = r.id WHERE r.id = 3").fetchone()
    assert tuple(rx) == (120, 22.0, 23.0, None, None, None), tuple(rx)
    sr = c.execute("SELECT ended_at, outcome, outdoor_mean, blocked FROM section_run "
                   "WHERE id = 1").fetchone()
    assert tuple(sr) == ("2026-07-31T14:00:00+08:00", "ran", None, None), tuple(sr)
    assert _runs(c) == EXPECTED_RUNS, _runs(c)
    cats = [e["category"] for e in store.recent_events(limit=10)]
    assert sorted(cats) == ["connection", "crossover", "reactive", "storage"], cats
    storage = store.recent_events(category="storage")[0]
    assert str(store.backup_path) in storage["message"], storage
    assert storage["detail"] == {"from": 0, "to": 2, "backup": str(store.backup_path)}
    assert store.current_almanac("main_room", as_of=date(2026, 8, 1)) == \
        {"room_id": "main_room", "sections": {}}
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    print("migration: every row kept (ids, joins, values), new columns null, almanac "
          "emptied, backup path logged as a storage event, checks clean - OK")
    print("migration 2: open sleep and orphaned runs closed at the next run's start, "
          "per room; each room's current run left open - OK")
    # with history repaired, the live close finds the right run: main_room's
    # current one, not the start-less run with the higher id
    assert store.close_section_run("main_room", "2026-07-31T20:30:00+08:00") == \
        ("2026-07-31", "sunset")
    store.close()

    fresh = Path(tempfile.mkdtemp())
    Store(fresh, SCHEMA).close()
    migrated_shape = _shape(db)
    assert migrated_shape == _shape(fresh / "db" / "adaptive_climate.db"), \
        "migrated database differs from a fresh one"
    print(f"migration: sqlite_master identical to a fresh database "
          f"({len(migrated_shape[1])} objects, user_version {migrated_shape[0]}) - OK")

    again = Store(tmp, SCHEMA)
    assert again.backup_path is None and _backups(db) == [store.backup_path]
    assert len(again.recent_events(category="storage")) == 1
    again.close()
    print("migration: reopening a migrated database neither migrates nor backs up again - OK")


def check_failed_migration() -> None:
    real = store_mod.MIGRATIONS

    def fail_half_way(conn: sqlite3.Connection, schema) -> None:
        store_mod._rebuild_table(conn, schema, "heartbeat")      # real work first...
        raise RuntimeError("simulated failure half-way through")

    def leave_dangling_row(conn: sqlite3.Connection, schema) -> None:
        real[0][1](conn, schema)                                  # the whole migration...
        conn.execute("INSERT INTO heartbeat_sensor VALUES (999, 'ghost', 1.0)")

    for label, migration, expect in (
            ("raises half-way", fail_half_way, "simulated failure"),
            ("fails foreign_key_check", leave_dangling_row, "foreign_key_check")):
        tmp = Path(tempfile.mkdtemp())
        db = build_v1_db(tmp)
        before = _dump(db)
        store_mod.MIGRATIONS = [("v1", migration)]
        try:
            Store(tmp, SCHEMA)
        except StoreError as e:
            message = str(e)
        else:
            raise AssertionError(f"{label}: the migration should have failed")
        finally:
            store_mod.MIGRATIONS = real
        assert expect in message and "unchanged at storage version 0" in message, message
        assert _dump(db) == before, f"{label}: the database was changed"
        assert len(_backups(db)) == 1, "the backup is taken before the migration runs"
        print(f"failed migration ({label}): rolled back, database identical to before (full dump + user_version) - OK")

        recovered = Store(tmp, SCHEMA)   # still a v1 database the real migration accepts
        assert recovered._conn.execute("PRAGMA user_version").fetchone()[0] == len(real)
        assert recovered._conn.execute("SELECT COUNT(*) FROM heartbeat").fetchone()[0] == 2
        recovered.close()
    print("failed migration: the untouched database then migrates normally - OK")


def check_phase0_database() -> None:
    """A database left at storage version 1 (v2 phase 0, before the section
    run fix) gets migration 2 alone, behind its own backup."""
    real = store_mod.MIGRATIONS
    tmp = Path(tempfile.mkdtemp())
    db = build_v1_db(tmp)
    store_mod.MIGRATIONS = real[:1]
    try:
        Store(tmp, SCHEMA).close()
    finally:
        store_mod.MIGRATIONS = real
    phase0 = _dump(db)
    assert phase0[0] == 1
    c = sqlite3.connect(db)
    still_open = c.execute("SELECT COUNT(*) FROM section_run WHERE ended_at IS NULL").fetchone()[0]
    c.close()
    assert still_open == 6, still_open

    store = Store(tmp, SCHEMA)
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert _backups(db, "phase0") == [store.backup_path], store.backup_path
    assert _dump(store.backup_path) == phase0
    assert _runs(store._conn) == EXPECTED_RUNS, _runs(store._conn)
    details = [e["detail"] for e in store.recent_events(category="storage")]
    assert {"from": 1, "to": 2, "backup": str(store.backup_path)} in details, details
    store.close()
    print(f"phase 0 database: backed up to {store.backup_path.name}, migration 2 alone "
          f"closes its open runs - OK")


def check_newer_database_refused() -> None:
    tmp = Path(tempfile.mkdtemp())
    Store(tmp, SCHEMA).close()
    c = sqlite3.connect(tmp / "db" / "adaptive_climate.db")
    c.execute("PRAGMA user_version = 99")
    c.close()
    try:
        Store(tmp, SCHEMA)
    except StoreError as e:
        assert "newer than this code" in str(e), e
    else:
        raise AssertionError("opened a database newer than the code")
    print("a database newer than the code is refused - OK")


def main() -> int:
    check_fresh_store()
    check_migration()
    check_failed_migration()
    check_phase0_database()
    check_newer_database_refused()
    print("\nALL STORE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
