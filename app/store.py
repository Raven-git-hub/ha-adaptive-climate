"""
Adaptive Climate - storage.

CSV on disk is the archive; SQLite is the query layer. Every observation
is written to both, CSV first: if the DB write fails the data is still
recoverable, and SQLite is always rebuildable from CSV via ingest_csv().

Where Light stored one ambient_lux + per-group brightness, Climate stores
per-sensor temperature (heartbeat_sensor) and per-unit climate state
(heartbeat_unit), and the reactive tables capture a per-sensor snapshot
at reaction time - the raw material for the trust model.

STATUS: observation writes are Phase 8 stubs; almanac persistence is
implemented (Phase 4 needs it to save and serve what the analyser builds).
Schema is final in schema/storage.schema.sql; almanac JSON shape is in
docs/ALMANAC_FORMAT.md.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


@dataclass(frozen=True)
class UnitSample:
    """One unit's reading within a heartbeat."""
    is_on: bool
    hvac_mode: str | None
    fan_mode: str | None
    setpoint: float | None
    current_temp: float | None
    ac_state: str | None            # normal | cooling | warming | leak


@dataclass(frozen=True)
class SensorSample:
    temperature: float | None       # None if unavailable


@dataclass(frozen=True)
class ReactiveUnitSample:
    setpoint_before: float | None
    setpoint_after: float | None
    changed: bool


class Store:
    def __init__(self, data_dir: str | Path, schema_path: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "db" / "adaptive_climate.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "csv").mkdir(parents=True, exist_ok=True)

        fresh = not self.db_path.exists() or self.db_path.stat().st_size == 0
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if fresh:
            self._conn.executescript(Path(schema_path).read_text())
            self._conn.commit()
        else:
            self._conn.execute("PRAGMA foreign_keys = ON")

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
                         deferred_ms: int = 0) -> int:
        any_on = 1 if any(u.is_on for u in units.values()) else 0
        # CSV archive first (per-sensor/per-unit encoded as JSON columns)
        self._append_csv(
            room_id, local_date, "heartbeat",
            ["ts", "ts_utc", "section", "occupied", "any_unit_on",
             "deferred_ms", "sensors", "units"],
            [ts, ts_utc, section,
             "" if occupied is None else int(occupied), any_on, deferred_ms,
             json.dumps(sensors),
             json.dumps({uid: u.__dict__ for uid, u in units.items()})])
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO heartbeat(room_id,ts,ts_utc,local_date,section,"
                    "sensor_n,occupied,any_unit_on,deferred_ms) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (room_id, ts, ts_utc, local_date, section, len(sensors),
                     None if occupied is None else int(occupied), any_on, deferred_ms))
                hb = cur.lastrowid
                self._conn.executemany(
                    "INSERT INTO heartbeat_sensor(heartbeat_id,sensor_id,temperature) "
                    "VALUES(?,?,?)",
                    [(hb, sid, t) for sid, t in sensors.items()])
                self._conn.executemany(
                    "INSERT INTO heartbeat_unit(heartbeat_id,unit_id,is_on,hvac_mode,"
                    "fan_mode,setpoint,current_temp,ac_state) VALUES(?,?,?,?,?,?,?,?)",
                    [(hb, uid, int(u.is_on), u.hvac_mode, u.fan_mode, u.setpoint,
                      u.current_temp, u.ac_state) for uid, u in units.items()])
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
                        suspended_maint: bool = False) -> int:
        self._append_csv(
            room_id, local_date, "reactive",
            ["ts", "ts_utc", "section", "window_seconds", "occupied",
             "suspended_maint", "units", "sensors"],
            [ts, ts_utc, section, window_seconds,
             "" if occupied is None else int(occupied), int(suspended_maint),
             json.dumps({uid: u.__dict__ for uid, u in units.items()}),
             json.dumps(sensors)])
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO reactive(room_id,ts,ts_utc,local_date,section,"
                    "window_seconds,occupied,suspended_maint) VALUES(?,?,?,?,?,?,?,?)",
                    (room_id, ts, ts_utc, local_date, section, window_seconds,
                     None if occupied is None else int(occupied), int(suspended_maint)))
                rid = cur.lastrowid
                self._conn.executemany(
                    "INSERT INTO reactive_unit(reactive_id,unit_id,setpoint_before,"
                    "setpoint_after,changed) VALUES(?,?,?,?,?)",
                    [(rid, uid, u.setpoint_before, u.setpoint_after, int(u.changed))
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
                           outcome_reason: str | None = None) -> None:
        """Upsert one room/date/section run. Called at crossover."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO section_run(room_id,local_date,section,planned_start,"
                "actual_start,ended_at,outcome,outcome_reason) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(room_id,local_date,section) DO UPDATE SET "
                "planned_start=COALESCE(excluded.planned_start,section_run.planned_start),"
                "actual_start=COALESCE(excluded.actual_start,section_run.actual_start),"
                "ended_at=COALESCE(excluded.ended_at,section_run.ended_at),"
                "outcome=excluded.outcome,"
                "outcome_reason=COALESCE(excluded.outcome_reason,section_run.outcome_reason)",
                (room_id, local_date, section, planned_start, actual_start,
                 ended_at, outcome, outcome_reason))
            self._conn.commit()

    def close_section_run(self, room_id: str, local_date: str, section: str,
                          ended_at: str) -> None:
        """Stamp the end of a section when the next one fires, so Analysis
        target bands stop at the real boundary (a Light cutover lesson)."""
        with self._lock:
            self._conn.execute(
                "UPDATE section_run SET ended_at=? WHERE room_id=? AND local_date=? "
                "AND section=? AND ended_at IS NULL",
                (ended_at, room_id, local_date, section))
            self._conn.commit()

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
        # Reverse path (rebuild SQLite from the CSV archive). Deferred; the
        # forward CSV+SQLite write path above is what the runtime needs.
        raise NotImplementedError("Phase 8 - CSV re-ingest (recovery path, later)")

    # --- reads -----------------------------------------------------
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

    # --- almanac persistence (implemented) -------------------------
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
                        "INSERT INTO almanac(room_id,section,valid_from,state,"
                        "sample_days,confidence,built_at) VALUES(?,?,?,?,?,?,?)",
                        (room_id, sa.section, vf, sa.state, sa.sample_days,
                         sa.confidence, built_at))
                    aid = cur.lastrowid
                    self._conn.executemany(
                        "INSERT INTO almanac_unit(almanac_id,unit_id,setpoint,off) "
                        "VALUES(?,?,?,?)",
                        [(aid, uid, sa.unit_setpoints.get(uid),
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
                "SELECT a.id, a.section, a.valid_from, a.state, a.sample_days, "
                "a.confidence FROM almanac a JOIN ("
                "  SELECT section, MAX(valid_from) vf FROM almanac "
                "  WHERE room_id=? AND valid_from<=? GROUP BY section) latest "
                "ON latest.section=a.section AND latest.vf=a.valid_from "
                "WHERE a.room_id=?",
                (room_id, as_of_str, room_id)).fetchall()
            sections: dict = {}
            for h in headers:
                units = {r["unit_id"]: {"setpoint": r["setpoint"],
                                        "off": bool(r["off"])}
                         for r in self._conn.execute(
                             "SELECT unit_id,setpoint,off FROM almanac_unit "
                             "WHERE almanac_id=?", (h["id"],))}
                sensors = {r["sensor_id"]: {"comfort": r["comfort"],
                                            "band": r["band"], "trust": r["trust"]}
                           for r in self._conn.execute(
                             "SELECT sensor_id,comfort,band,trust FROM almanac_sensor "
                             "WHERE almanac_id=?", (h["id"],))}
                sections[h["section"]] = {
                    "state": h["state"], "valid_from": h["valid_from"],
                    "sample_days": h["sample_days"], "confidence": h["confidence"],
                    "units": units, "sensors": sensors,
                }
        return {"room_id": room_id, "sections": sections}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
