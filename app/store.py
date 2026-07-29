"""
Adaptive Climate - storage.

CSV on disk is the archive; SQLite is the query layer. Every observation
is written to both, CSV first: if the DB write fails the data is still
recoverable, and SQLite is always rebuildable from CSV via ingest_csv().

Where Light stored one ambient_lux + per-group brightness, Climate stores
per-sensor temperature (heartbeat_sensor) and per-unit climate state
(heartbeat_unit), and the reactive tables capture a per-sensor snapshot
at reaction time - the raw material for the trust model.

STATUS: skeleton. Schema is final in schema/storage.schema.sql.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
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

    # --- writes (TODO Phase 8) -------------------------------------
    def record_heartbeat(self, *a, **k):  raise NotImplementedError("Phase 8")
    def record_reactive(self, *a, **k):   raise NotImplementedError("Phase 8")
    def record_section_run(self, *a, **k): raise NotImplementedError("Phase 8")
    def publish_almanac(self, *a, **k):   raise NotImplementedError("Phase 8")
    def log_event(self, *a, **k):         raise NotImplementedError("Phase 8")
    def ingest_csv(self, *a, **k):        raise NotImplementedError("Phase 8")

    # --- reads (TODO Phase 8) --------------------------------------
    def current_almanac(self, room_id: str) -> dict: raise NotImplementedError("Phase 8")
    def recent_events(self, *a, **k) -> list:        raise NotImplementedError("Phase 8")
    def activity(self, room_id: str, day: str) -> dict: raise NotImplementedError("Phase 8")
    def save_config_version(self, cfg: dict) -> None: raise NotImplementedError("Phase 8")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
