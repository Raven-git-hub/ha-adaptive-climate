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
  * a database newer than the code is refused

    PYTHONPATH=. python tools/store_check.py
"""
from __future__ import annotations

import csv as csvmod
import json
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

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
    store.close_section_run("main_room", rd, "day", f"{rd}T14:00:00+08:00")
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

    # --- almanac: v1 analyser states are published as v2 status ---
    store.publish_almanac("main_room", sections)
    served = store.current_almanac("main_room", as_of=day.valid_from)["sections"]["day"]
    assert day.state == "learning" and served["status"] == "learned", served
    assert served["units"]["main_room_ac"] == {"setpoint": sp, "setpoint_applied": None,
                                               "off": False}
    assert "state" not in served and "sample_days" not in served
    print("almanac: 'learning' published as status 'learned', setpoint_applied null - OK")

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
        INSERT INTO section_run(id,room_id,local_date,section,planned_start,actual_start,
                                ended_at,outcome)
          VALUES (1,'main_room','2026-07-31','day','2026-07-31T08:00:00+08:00',
                  '2026-07-31T08:00:03+08:00','2026-07-31T14:00:00+08:00','ran');
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


def _backups(db: Path) -> list[Path]:
    return sorted(p for p in db.parent.glob(f"{db.name}.v1-backup-*")
                  if not p.name.endswith(("-wal", "-shm")))


def check_migration() -> None:
    tmp = Path(tempfile.mkdtemp())
    db = build_v1_db(tmp)
    v1_dump = _dump(db)
    assert v1_dump[0] == 0

    store = Store(tmp, SCHEMA)
    c = store._conn
    assert c.execute("PRAGMA user_version").fetchone()[0] == len(store_mod.MIGRATIONS) == 1
    assert _backups(db) == [store.backup_path], (_backups(db), store.backup_path)
    assert _dump(store.backup_path) == v1_dump, "backup is not a copy of the v1 database"
    print(f"migration: v1 database backed up to {store.backup_path.name}, now at version 1 - OK")

    def n(table: str) -> int:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    expected = {"config_version": 1, "section_run": 1, "heartbeat": 2, "heartbeat_sensor": 4,
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
    sr = c.execute("SELECT ended_at, outcome, outdoor_mean, blocked FROM section_run").fetchone()
    assert tuple(sr) == ("2026-07-31T14:00:00+08:00", "ran", None, None), tuple(sr)
    cats = [e["category"] for e in store.recent_events(limit=10)]
    assert sorted(cats) == ["connection", "crossover", "reactive", "storage"], cats
    storage = store.recent_events(category="storage")[0]
    assert str(store.backup_path) in storage["message"], storage
    assert storage["detail"] == {"from": 0, "to": 1, "backup": str(store.backup_path)}
    assert store.current_almanac("main_room", as_of=date(2026, 8, 1)) == \
        {"room_id": "main_room", "sections": {}}
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    print("migration: every row kept (ids, joins, values), new columns null, almanac "
          "emptied, backup path logged as a storage event, checks clean - OK")
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
        assert recovered._conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert recovered._conn.execute("SELECT COUNT(*) FROM heartbeat").fetchone()[0] == 2
        recovered.close()
    print("failed migration: the untouched database then migrates normally - OK")


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
    check_newer_database_refused()
    print("\nALL STORE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
