"""
Adaptive Climate - storage.

CSV on disk is the archive; SQLite is the query layer. Every observation
is written to both, CSV first: if the DB write fails the data is still
recoverable, and SQLite is always rebuildable from CSV via ingest_csv().

Where Light stored one ambient_lux + per-group brightness, Climate stores
per-sensor temperature (heartbeat_sensor) and per-unit climate state
(heartbeat_unit), and the reactive tables capture a per-sensor snapshot
at reaction time - the raw material for the trust model.

Schema versions: schema/storage.schema.sql is the current shape. A fresh
database runs it and is stamped PRAGMA user_version = len(MIGRATIONS). An
older database is backed up next to itself, then brought forward by the
numbered MIGRATIONS below, each in one transaction with foreign keys off
that must pass foreign_key_check and integrity_check before it commits;
anything else rolls it back and leaves the database as it was. Migrations
recreate changed tables from the schema file's own CREATE text, so a
migrated database is identical to a fresh one.

CSV columns are only ever appended, never reordered, so a file started by
an older version keeps its shorter header and re-ingest can map columns by
position. Per-unit fields live inside the JSON `units` column.

STATUS: CSV re-ingest (ingest_csv) is not implemented; it is a Gate A
prerequisite (docs/V2_DESIGN.md). Almanac JSON shape: docs/ALMANAC_FORMAT.md.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("adaptive_climate.store")


# ---------------------------------------------------------------------
# CSV column order per record kind. Append only: never reorder or remove.
# ---------------------------------------------------------------------

HEARTBEAT_CSV = ["ts", "ts_utc", "section", "occupied", "any_unit_on",
                 "deferred_ms", "sensors", "units",
                 "outdoor_temp"]                                    # v2
REACTIVE_CSV = ["ts", "ts_utc", "section", "window_seconds", "occupied",
                "suspended_maint", "units", "sensors",
                "kind", "outdoor_temp"]                             # v2
# One row when a section run starts and one when it closes. Re-ingest
# merges a day file's rows per section in order; a blank field leaves the
# earlier value, matching record_section_run's upsert.
SECTION_RUN_CSV = ["section", "planned_start", "actual_start", "ended_at",
                   "outcome", "outcome_reason", "outdoor_mean", "outdoor_max",
                   "comfort_point", "lean", "blocked", "block_mode"]


@dataclass(frozen=True)
class UnitSample:
    """One unit's reading within a heartbeat."""
    is_on: bool
    hvac_mode: str | None
    fan_mode: str | None
    setpoint: float | None
    current_temp: float | None
    ac_state: str | None                # v1 inferred: normal | cooling | warming | leak
    control_state: str | None = None    # v2 state helper: cool | boost | rest | ...
    control_offset: int | None = None   # v2 section offset, whole steps


@dataclass(frozen=True)
class SensorSample:
    temperature: float | None       # None if unavailable


@dataclass(frozen=True)
class ReactiveUnitSample:
    setpoint_before: float | None
    setpoint_after: float | None
    changed: bool
    kind: str | None = None             # v2: setpoint | on | off | manual | knob_turn | knob_press
    power_before: bool | None = None    # v2: unit on at the window's start
    power_after: bool | None = None     # v2: unit on at the window's end


class StoreError(Exception):
    """The database can't be opened or migrated; the message says why."""


# ---------------------------------------------------------------------
# The schema file, split into statements
# ---------------------------------------------------------------------

_CREATE_RE = re.compile(
    r"CREATE\s+(TABLE|INDEX|UNIQUE\s+INDEX|VIEW)\s+(\w+)(?:\s+ON\s+(\w+))?", re.IGNORECASE)


@dataclass(frozen=True)
class _Statement:
    kind: str           # table | index | view | pragma
    name: str | None
    table: str | None   # for an index, the table it is on
    sql: str


class _Schema:
    """schema/storage.schema.sql as individual statements, so a migration
    can recreate one table (and its indexes) from exactly the text a fresh
    database is built from."""

    def __init__(self, text: str) -> None:
        self.statements: list[_Statement] = []
        buf = ""
        for line in text.splitlines(keepends=True):
            buf += line
            if sqlite3.complete_statement(buf):
                self.statements.append(self._classify(buf.strip()))
                buf = ""
        if re.sub(r"--[^\n]*", "", buf).strip():
            raise StoreError("storage schema ends in an incomplete statement")

    @staticmethod
    def _classify(sql: str) -> _Statement:
        body = re.sub(r"--[^\n]*", "", sql).strip()
        if body.upper().startswith("PRAGMA"):
            return _Statement("pragma", None, None, sql)
        m = _CREATE_RE.match(body)
        if not m:
            raise StoreError(f"unexpected statement in storage schema: {body[:60]!r}")
        kind = m.group(1).split()[-1].lower()
        return _Statement(kind, m.group(2), m.group(3) if kind == "index" else m.group(2), sql)

    def pragmas(self) -> list[str]:
        return [s.sql for s in self.statements if s.kind == "pragma"]

    def objects(self) -> list[str]:
        return [s.sql for s in self.statements if s.kind != "pragma"]

    def create(self, name: str) -> str:
        for s in self.statements:
            if s.kind in ("table", "view") and s.name == name:
                return s.sql
        raise StoreError(f"storage schema has no table or view {name!r}")

    def indexes(self, table: str) -> list[str]:
        return [s.sql for s in self.statements if s.kind == "index" and s.table == table]


# ---------------------------------------------------------------------
# Migrations. Migration n takes a database from user_version n-1 to n.
# Each runs inside one transaction with foreign keys off and the legacy
# rename behaviour on (a rename must not rewrite other tables' REFERENCES).
# ---------------------------------------------------------------------

def _recreate(conn: sqlite3.Connection, schema: _Schema, name: str) -> None:
    conn.execute(schema.create(name))
    for sql in schema.indexes(name):
        conn.execute(sql)


def _rebuild_table(conn: sqlite3.Connection, schema: _Schema, name: str) -> None:
    """Recreate `name` from the schema file and copy its rows across for
    every column the old and new shapes share. Ids are copied, so child
    rows stay attached. The old table's indexes go with it."""
    old = f"_old_{name}"
    conn.execute(f"ALTER TABLE {name} RENAME TO {old}")
    conn.execute(schema.create(name))
    new_cols = [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]
    old_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({old})")}
    cols = ", ".join(c for c in new_cols if c in old_cols)
    conn.execute(f"INSERT INTO {name} ({cols}) SELECT {cols} FROM {old}")
    conn.execute(f"DROP TABLE {old}")
    for sql in schema.indexes(name):
        conn.execute(sql)


def _migration_1(conn: sqlite3.Connection, schema: _Schema) -> None:
    """v1 (v1.0-poc) -> v2 shape.

    The almanac is derived, so it is dropped and recreated empty (status
    provisional/learned, setpoint_applied). Tables whose columns or allowed
    values change are rebuilt with their rows: new nullable columns for
    outdoor and control data and reaction kinds, new event categories,
    and section_run as an ingest_file kind."""
    conn.execute("DROP VIEW current_almanac")
    for name in ("almanac_sensor", "almanac_unit", "almanac"):
        conn.execute(f"DROP TABLE {name}")
    for name in ("almanac", "almanac_unit", "almanac_sensor"):
        _recreate(conn, schema, name)
    for name in ("heartbeat", "heartbeat_unit", "reactive", "reactive_unit",
                 "section_run", "event", "ingest_file"):
        _rebuild_table(conn, schema, name)
    conn.execute(schema.create("current_almanac"))


def _migration_2(conn: sqlite3.Connection, schema: _Schema) -> None:
    """Close the section runs that were left open.

    Before this fix a run was closed by the date of its close, so a run that
    crossed midnight (sleep) never closed, and a restart across a boundary
    orphaned the run it interrupted. Per room, in start order, every open
    run except the latest is closed at the next run's start; only the
    genuinely current run stays open. Data only: the schema is unchanged."""
    by_room: dict[str, list[tuple[datetime, int, str, str | None]]] = {}
    for run_id, room_id, planned, actual, ended in conn.execute(
            "SELECT id, room_id, planned_start, actual_start, ended_at FROM section_run"):
        start = actual or planned
        if not start:
            continue    # nothing to order it by; leave it as it is
        when = datetime.fromisoformat(start)
        if when.tzinfo is None:
            continue    # every writer stamps an offset; don't guess one
        by_room.setdefault(room_id, []).append((when, run_id, start, ended))
    for runs in by_room.values():
        runs.sort(key=lambda r: (r[0], r[1]))
        for (_, run_id, _, ended), (_, _, next_start, _) in zip(runs, runs[1:]):
            if ended is None:
                conn.execute("UPDATE section_run SET ended_at=? WHERE id=?",
                             (next_start, run_id))


def _migration_3(conn: sqlite3.Connection, schema: _Schema) -> None:
    """A person's vote: the reaction kinds too_cold and too_hot.

    reactive and reactive_unit are rebuilt with their rows, so their kind
    checks allow the two new kinds; reactive_sensor keeps its rows, still
    attached by id."""
    for name in ("reactive", "reactive_unit"):
        _rebuild_table(conn, schema, name)


# (label for the pre-migration backup file, migration). Migration n takes
# storage version n-1 to n; the label names the shape it upgrades from.
# Append only.
MIGRATIONS: list[tuple[str, Callable[[sqlite3.Connection, _Schema], None]]] = [
    ("v1", _migration_1),
    ("phase0", _migration_2),
    ("pre-votes", _migration_3),
]

def _now_pair() -> tuple[str, str]:
    n = datetime.now().astimezone()
    return (n.isoformat(timespec="seconds"),
            n.astimezone(timezone.utc).isoformat(timespec="seconds"))


def _int_or_none(v) -> int | None:
    return None if v is None else int(v)


class Store:
    def __init__(self, data_dir: str | Path, schema_path: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "db" / "adaptive_climate.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "csv").mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.backup_path: Path | None = None    # set when this open migrated the database
        try:
            self._prepare(_Schema(Path(schema_path).read_text()))
        except BaseException:
            self._conn.close()
            raise
        self._conn.execute("PRAGMA foreign_keys = ON")

    # --- schema creation and migration ------------------------------
    def _prepare(self, schema: _Schema) -> None:
        latest = len(MIGRATIONS)
        has_tables = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()
        if not has_tables:
            for sql in schema.pragmas():
                self._conn.execute(sql)
            self._transaction(lambda: [self._conn.execute(sql) for sql in schema.objects()],
                              latest)
            return

        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if current > latest:
            raise StoreError(f"{self.db_path} is at storage version {current}, newer than "
                             f"this code understands ({latest}); refusing to open it")
        if current == latest:
            return

        self.backup_path = self._backup(MIGRATIONS[current][0])
        for n in range(current + 1, latest + 1):
            fn = MIGRATIONS[n - 1][1]
            try:
                self._transaction(lambda: fn(self._conn, schema), n)
            except Exception as e:
                raise StoreError(
                    f"storage migration {n} failed and was rolled back, so the database is "
                    f"unchanged at storage version {n - 1} (backup at {self.backup_path}): {e}"
                ) from e
        ts, ts_utc = _now_pair()
        self.log_event(ts, ts_utc, "info", "storage",
                       f"database migrated from storage version {current} to {latest}; "
                       f"backup at {self.backup_path}",
                       detail={"from": current, "to": latest, "backup": str(self.backup_path)})
        log.info("migrated %s from storage version %d to %d (backup %s)",
                 self.db_path, current, latest, self.backup_path)

    def _transaction(self, work: Callable[[], object], version: int) -> None:
        """Run work() in one transaction, check foreign keys and integrity,
        stamp user_version and commit; roll back on any failure."""
        conn = self._conn
        saved = conn.isolation_level
        conn.isolation_level = None                     # explicit BEGIN/COMMIT below
        conn.execute("PRAGMA foreign_keys = OFF")       # a no-op inside a transaction
        conn.execute("PRAGMA legacy_alter_table = ON")
        try:
            conn.execute("BEGIN IMMEDIATE")
            work()
            problems = conn.execute("PRAGMA foreign_key_check").fetchall()
            if problems:
                raise StoreError(f"foreign_key_check found {len(problems)} problem(s), "
                                 f"first {tuple(problems[0])}")
            integrity = [r[0] for r in conn.execute("PRAGMA integrity_check")]
            if integrity != ["ok"]:
                raise StoreError(f"integrity_check failed: {'; '.join(integrity[:3])}")
            conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA legacy_alter_table = OFF")
            conn.isolation_level = saved

    def _backup(self, label: str) -> Path:
        """Copy the database to <name>.<label>-backup-<timestamp> next to it,
        via SQLite's online backup so committed WAL content is included."""
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        base = f"{self.db_path.name}.{label}-backup-{stamp}"
        path, n = self.db_path.with_name(base), 1
        while path.exists():
            path, n = self.db_path.with_name(f"{base}-{n}"), n + 1
        dst = sqlite3.connect(path)
        try:
            self._conn.backup(dst)
        finally:
            dst.close()
        return path

    # --- CSV archive helper ----------------------------------------
    def _append_csv(self, room_id: str, local_date: str, kind: str,
                    header: list[str], row: list) -> None:
        d = self.data_dir / "csv" / room_id
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{local_date}_{kind}.csv"
        new = not path.exists()
        with path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(header)
            w.writerow(row)

    # --- observation writes (CSV first, then SQLite) ----------------
    def record_heartbeat(self, room_id: str, ts: str, ts_utc: str,
                         local_date: str, section: str,
                         sensors: dict[str, float | None],
                         units: dict[str, UnitSample],
                         occupied: bool | None = None,
                         deferred_ms: int = 0,
                         outdoor_temp: float | None = None) -> int:
        any_on = 1 if any(u.is_on for u in units.values()) else 0
        # CSV archive first (per-sensor/per-unit encoded as JSON columns)
        self._append_csv(
            room_id, local_date, "heartbeat", HEARTBEAT_CSV,
            [ts, ts_utc, section,
             "" if occupied is None else int(occupied), any_on, deferred_ms,
             json.dumps(sensors),
             json.dumps({uid: u.__dict__ for uid, u in units.items()}),
             outdoor_temp])
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO heartbeat(room_id,ts,ts_utc,local_date,section,"
                    "sensor_n,occupied,any_unit_on,deferred_ms,outdoor_temp) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (room_id, ts, ts_utc, local_date, section, len(sensors),
                     None if occupied is None else int(occupied), any_on, deferred_ms,
                     outdoor_temp))
                hb = cur.lastrowid
                self._conn.executemany(
                    "INSERT INTO heartbeat_sensor(heartbeat_id,sensor_id,temperature) "
                    "VALUES(?,?,?)",
                    [(hb, sid, t) for sid, t in sensors.items()])
                self._conn.executemany(
                    "INSERT INTO heartbeat_unit(heartbeat_id,unit_id,is_on,hvac_mode,"
                    "fan_mode,setpoint,current_temp,ac_state,control_state,control_offset) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [(hb, uid, int(u.is_on), u.hvac_mode, u.fan_mode, u.setpoint,
                      u.current_temp, u.ac_state, u.control_state, u.control_offset)
                     for uid, u in units.items()])
                self._conn.commit()
                return hb
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def record_reactive(self, room_id: str, ts: str, ts_utc: str,
                        local_date: str, section: str, window_seconds: int,
                        units: dict[str, ReactiveUnitSample],
                        sensors: dict[str, float | None],
                        occupied: bool | None = None,
                        suspended_maint: bool = False,
                        kind: str | None = None,
                        outdoor_temp: float | None = None) -> int:
        self._append_csv(
            room_id, local_date, "reactive", REACTIVE_CSV,
            [ts, ts_utc, section, window_seconds,
             "" if occupied is None else int(occupied), int(suspended_maint),
             json.dumps({uid: u.__dict__ for uid, u in units.items()}),
             json.dumps(sensors),
             kind, outdoor_temp])
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO reactive(room_id,ts,ts_utc,local_date,section,"
                    "window_seconds,occupied,suspended_maint,kind,outdoor_temp) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (room_id, ts, ts_utc, local_date, section, window_seconds,
                     None if occupied is None else int(occupied), int(suspended_maint),
                     kind, outdoor_temp))
                rid = cur.lastrowid
                self._conn.executemany(
                    "INSERT INTO reactive_unit(reactive_id,unit_id,setpoint_before,"
                    "setpoint_after,changed,kind,power_before,power_after) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    [(rid, uid, u.setpoint_before, u.setpoint_after, int(u.changed),
                      u.kind, _int_or_none(u.power_before), _int_or_none(u.power_after))
                     for uid, u in units.items()])
                self._conn.executemany(
                    "INSERT INTO reactive_sensor(reactive_id,sensor_id,temperature) "
                    "VALUES(?,?,?)",
                    [(rid, sid, t) for sid, t in sensors.items()])
                self._conn.commit()
                return rid
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def record_section_run(self, room_id: str, local_date: str, section: str,
                           planned_start: str | None = None,
                           actual_start: str | None = None,
                           ended_at: str | None = None,
                           outcome: str = "ran",
                           outcome_reason: str | None = None,
                           outdoor_mean: float | None = None,
                           outdoor_max: float | None = None,
                           comfort_point: float | None = None,
                           lean: float | None = None,
                           blocked: bool | None = None,
                           block_mode: str | None = None) -> None:
        """Upsert one room/date/section run. Called at crossover."""
        self._append_csv(
            room_id, local_date, "section_run", SECTION_RUN_CSV,
            [section, planned_start, actual_start, ended_at, outcome, outcome_reason,
             outdoor_mean, outdoor_max, comfort_point, lean, _int_or_none(blocked),
             block_mode])
        with self._lock:
            self._conn.execute(
                "INSERT INTO section_run(room_id,local_date,section,planned_start,"
                "actual_start,ended_at,outcome,outcome_reason,outdoor_mean,outdoor_max,"
                "comfort_point,lean,blocked,block_mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(room_id,local_date,section) DO UPDATE SET "
                "planned_start=COALESCE(excluded.planned_start,section_run.planned_start),"
                "actual_start=COALESCE(excluded.actual_start,section_run.actual_start),"
                "ended_at=COALESCE(excluded.ended_at,section_run.ended_at),"
                "outcome=excluded.outcome,"
                "outcome_reason=COALESCE(excluded.outcome_reason,section_run.outcome_reason),"
                "outdoor_mean=COALESCE(excluded.outdoor_mean,section_run.outdoor_mean),"
                "outdoor_max=COALESCE(excluded.outdoor_max,section_run.outdoor_max),"
                "comfort_point=COALESCE(excluded.comfort_point,section_run.comfort_point),"
                "lean=COALESCE(excluded.lean,section_run.lean),"
                "blocked=COALESCE(excluded.blocked,section_run.blocked),"
                "block_mode=COALESCE(excluded.block_mode,section_run.block_mode)",
                (room_id, local_date, section, planned_start, actual_start,
                 ended_at, outcome, outcome_reason, outdoor_mean, outdoor_max,
                 comfort_point, lean, _int_or_none(blocked), block_mode))
            self._conn.commit()

    def close_section_run(self, room_id: str, ended_at: str,
                          keep: tuple[str, str] | None = None) -> tuple[str, str] | None:
        """Stamp the end of a section when the next one fires, so Analysis
        target bands stop at the real boundary (a Light cutover lesson).

        Closes the room's most recent open run, found by being open rather
        than by date, so a run that crossed midnight (sleep) closes against
        the date it started on. keep=(local_date, section) leaves that run
        open: a catch-up restart inside the same run. Returns the closed
        run's (local_date, section), or None if nothing was closed."""
        with self._lock:
            run = self._conn.execute(
                "SELECT id, local_date, section FROM section_run "
                "WHERE room_id=? AND ended_at IS NULL "
                "ORDER BY COALESCE(actual_start, planned_start) IS NULL, "
                "COALESCE(actual_start, planned_start) DESC, id DESC LIMIT 1",
                (room_id,)).fetchone()
        if run is None or (keep is not None and (run["local_date"], run["section"]) == tuple(keep)):
            return None
        # The close row goes in the run's own day file, so re-ingest merges it
        # into the run it ends.
        self._append_csv(room_id, run["local_date"], "section_run", SECTION_RUN_CSV,
                         [run["section"], None, None, ended_at] + [None] * 8)
        with self._lock:
            self._conn.execute("UPDATE section_run SET ended_at=? WHERE id=?",
                               (ended_at, run["id"]))
            self._conn.commit()
        return run["local_date"], run["section"]

    def log_event(self, ts: str, ts_utc: str, severity: str, category: str,
                  message: str, room_id: str | None = None,
                  detail: dict | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO event(ts,ts_utc,room_id,severity,category,message,detail) "
                "VALUES(?,?,?,?,?,?,?)",
                (ts, ts_utc, room_id, severity, category, message,
                 json.dumps(detail) if detail is not None else None))
            self._conn.commit()
            return cur.lastrowid

    def save_config_version(self, cfg: dict) -> None:
        """Snapshot a config the first time we see it (dedup by sha), so any
        historical row can be interpreted with the config that produced it."""
        payload = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
        sha = hashlib.sha256(payload.encode()).hexdigest()
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM config_version WHERE payload_sha=?", (sha,)).fetchone()
            if exists:
                return
            self._conn.execute(
                "INSERT INTO config_version(applied_at,payload,payload_sha) VALUES(?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), payload, sha))
            self._conn.commit()

    def ingest_csv(self, *a, **k):
        # Reverse path (rebuild SQLite from the CSV archive). Not built yet;
        # it is a Gate A prerequisite in docs/V2_DESIGN.md.
        raise NotImplementedError("CSV re-ingest (recovery path) - required before Gate A")

    # --- reads -----------------------------------------------------
    def reactions_since(self, room_id: str, since_utc: str) -> list[tuple[str, str | None, dict]]:
        """(ts_utc, kind, {unit_id: (setpoint_before, setpoint_after)}) of the
        room's reactions at or after since_utc, oldest first. The runtime uses
        it at startup to re-arm comfort replays."""
        with self._lock:
            heads = self._conn.execute(
                "SELECT id, ts_utc, kind FROM reactive WHERE room_id=? AND ts_utc>=? "
                "ORDER BY ts_utc, id", (room_id, since_utc)).fetchall()
            return [(h["ts_utc"], h["kind"], {
                u["unit_id"]: (u["setpoint_before"], u["setpoint_after"])
                for u in self._conn.execute(
                    "SELECT unit_id, setpoint_before, setpoint_after FROM reactive_unit "
                    "WHERE reactive_id=?", (h["id"],))}) for h in heads]

    def recent_events(self, limit: int = 200, room_id: str | None = None,
                      category: str | None = None,
                      severity: str | None = None) -> list[dict]:
        q = ("SELECT ts, ts_utc, room_id, severity, category, message, detail "
             "FROM event WHERE 1=1")
        params: list = []
        if room_id:
            q += " AND room_id=?"; params.append(room_id)
        if category:
            q += " AND category=?"; params.append(category)
        if severity:
            q += " AND severity=?"; params.append(severity)
        q += " ORDER BY ts_utc DESC LIMIT ?"; params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("detail"):
                try:
                    d["detail"] = json.loads(d["detail"])
                except (json.JSONDecodeError, TypeError):
                    pass  # leave as-is if it wasn't JSON
            out.append(d)
        return out

    def activity(self, room_id: str, day: str) -> dict:
        """Everything the Analysis view needs for one room/day: heartbeats
        (with per-sensor and per-unit detail), reactions, and section runs."""
        with self._lock:
            hbs = self._conn.execute(
                "SELECT id, ts, section, occupied, any_unit_on, deferred_ms "
                "FROM heartbeat WHERE room_id=? AND local_date=? ORDER BY ts",
                (room_id, day)).fetchall()
            heartbeats = []
            for h in hbs:
                sensors = {r["sensor_id"]: r["temperature"] for r in self._conn.execute(
                    "SELECT sensor_id,temperature FROM heartbeat_sensor WHERE heartbeat_id=?",
                    (h["id"],))}
                units = {r["unit_id"]: {"is_on": bool(r["is_on"]),
                                        "hvac_mode": r["hvac_mode"], "fan_mode": r["fan_mode"],
                                        "setpoint": r["setpoint"], "current_temp": r["current_temp"],
                                        "ac_state": r["ac_state"]}
                         for r in self._conn.execute(
                             "SELECT * FROM heartbeat_unit WHERE heartbeat_id=?", (h["id"],))}
                heartbeats.append({"ts": h["ts"], "section": h["section"],
                                   "sensors": sensors, "units": units})

            rxs = self._conn.execute(
                "SELECT id, ts, section FROM reactive WHERE room_id=? AND local_date=? "
                "ORDER BY ts", (room_id, day)).fetchall()
            reactions = []
            for rx in rxs:
                units = {r["unit_id"]: {"before": r["setpoint_before"],
                                        "after": r["setpoint_after"], "changed": bool(r["changed"])}
                         for r in self._conn.execute(
                             "SELECT * FROM reactive_unit WHERE reactive_id=?", (rx["id"],))}
                sensors = {r["sensor_id"]: r["temperature"] for r in self._conn.execute(
                    "SELECT sensor_id,temperature FROM reactive_sensor WHERE reactive_id=?",
                    (rx["id"],))}
                reactions.append({"ts": rx["ts"], "section": rx["section"],
                                  "units": units, "sensors": sensors})

            runs = [dict(r) for r in self._conn.execute(
                "SELECT section, planned_start, actual_start, ended_at, outcome, "
                "outcome_reason FROM section_run WHERE room_id=? AND local_date=? "
                "ORDER BY planned_start", (room_id, day))]
        return {"room_id": room_id, "day": day, "heartbeats": heartbeats,
                "reactions": reactions, "section_runs": runs}

    # --- almanac persistence ---------------------------------------
    def publish_almanac(self, room_id: str, sections: list) -> None:
        """Persist a list of SectionAlmanac (app.analyser). Idempotent on
        (room_id, section, valid_from): re-running a day overwrites."""
        built_at = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            try:
                for sa in sections:
                    vf = sa.valid_from.isoformat()
                    self._conn.execute(
                        "DELETE FROM almanac WHERE room_id=? AND section=? AND valid_from=?",
                        (room_id, sa.section, vf))
                    cur = self._conn.execute(
                        "INSERT INTO almanac(room_id,section,valid_from,status,"
                        "confidence,built_at) VALUES(?,?,?,?,?,?)",
                        (room_id, sa.section, vf, sa.status, sa.confidence, built_at))
                    aid = cur.lastrowid
                    self._conn.executemany(
                        "INSERT INTO almanac_unit(almanac_id,unit_id,setpoint,"
                        "setpoint_applied,off) VALUES(?,?,?,?,?)",
                        [(aid, uid, sa.unit_setpoints.get(uid),
                          sa.unit_setpoints_applied.get(uid),
                          int(sa.unit_off.get(uid, False)))
                         for uid in sa.unit_setpoints])
                    self._conn.executemany(
                        "INSERT INTO almanac_sensor(almanac_id,sensor_id,comfort,band,trust) "
                        "VALUES(?,?,?,?,?)",
                        [(aid, sid, sa.sensor_comfort.get(sid),
                          sa.sensor_band.get(sid), sa.sensor_trust.get(sid))
                         for sid in sa.sensor_comfort])
                self._conn.commit()
            except sqlite3.Error:
                self._conn.rollback()
                raise

    def current_almanac(self, room_id: str, as_of: date | None = None) -> dict:
        """The almanac in force on `as_of` (default today), shaped per
        docs/ALMANAC_FORMAT.md: latest valid_from <= as_of per section.
        Returns {"room_id","sections":{section:{...}}}."""
        as_of_str = (as_of or date.today()).isoformat()
        with self._lock:
            headers = self._conn.execute(
                "SELECT a.id, a.section, a.valid_from, a.status, a.confidence "
                "FROM almanac a JOIN ("
                "  SELECT section, MAX(valid_from) vf FROM almanac "
                "  WHERE room_id=? AND valid_from<=? GROUP BY section) latest "
                "ON latest.section=a.section AND latest.vf=a.valid_from "
                "WHERE a.room_id=?",
                (room_id, as_of_str, room_id)).fetchall()
            sections: dict = {}
            for h in headers:
                units = {r["unit_id"]: {"setpoint": r["setpoint"],
                                        "setpoint_applied": r["setpoint_applied"],
                                        "off": bool(r["off"])}
                         for r in self._conn.execute(
                             "SELECT unit_id,setpoint,setpoint_applied,off FROM almanac_unit "
                             "WHERE almanac_id=?", (h["id"],))}
                sensors = {r["sensor_id"]: {"comfort": r["comfort"],
                                            "band": r["band"], "trust": r["trust"]}
                           for r in self._conn.execute(
                             "SELECT sensor_id,comfort,band,trust FROM almanac_sensor "
                             "WHERE almanac_id=?", (h["id"],))}
                sections[h["section"]] = {
                    "status": h["status"], "valid_from": h["valid_from"],
                    "confidence": h["confidence"],
                    "units": units, "sensors": sensors,
                }
        return {"room_id": room_id, "sections": sections}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
