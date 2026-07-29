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

    # --- observation writes (TODO Phase 8) -------------------------
    def record_heartbeat(self, *a, **k):  raise NotImplementedError("Phase 8")
    def record_reactive(self, *a, **k):   raise NotImplementedError("Phase 8")
    def record_section_run(self, *a, **k): raise NotImplementedError("Phase 8")
    def log_event(self, *a, **k):         raise NotImplementedError("Phase 8")
    def ingest_csv(self, *a, **k):        raise NotImplementedError("Phase 8")
    def recent_events(self, *a, **k) -> list:        raise NotImplementedError("Phase 8")
    def activity(self, room_id: str, day: str) -> dict: raise NotImplementedError("Phase 8")
    def save_config_version(self, cfg: dict) -> None: raise NotImplementedError("Phase 8")

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
