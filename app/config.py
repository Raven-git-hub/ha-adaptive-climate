"""
Adaptive Climate - config loader.

Validates config against schema/config.schema.json, applies defaults,
and runs cross-reference checks the schema cannot express (e.g. a room's
schedule_profile must exist; every scene unit id must be a real unit in
that room). The loaded Config is the typed view the rest of the app uses.

STATUS: skeleton.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Raised when the config is unusable, with a human-readable message."""


@dataclass
class Unit:
    id: str
    name: str
    entity_id: str
    leak_enabled: bool = False


@dataclass
class Sensor:
    id: str
    name: str
    entity_id: str


@dataclass
class Room:
    id: str
    name: str
    enabled: bool
    schedule_profile: str
    units: list[Unit]
    sensors: list[Sensor]
    presence_sensors: list[str]

    @property
    def unit_ids(self) -> list[str]:
        return [u.id for u in self.units]

    @property
    def sensor_ids(self) -> list[str]:
        return [s.id for s in self.sensors]


@dataclass
class Config:
    raw: dict[str, Any]
    # typed accessors (homeassistant, system, rooms, schedule_profiles)
    # filled by load(). TODO(Phase 9).

    @property
    def active_rooms(self) -> list[Room]:
        raise NotImplementedError("Phase 9")


def blank() -> dict:
    """A minimal valid config: no rooms, the default schedule profile."""
    return {
        "version": 1,
        "homeassistant": {"base_url": "http://homeassistant.local:8123"},
        "system": {},
        "rooms": [],
        "schedule_profiles": [_default_profile()],
    }


def _default_profile() -> dict:
    times = {"sunrise": "05:30", "day": "08:00", "afternoon": "14:00",
             "sunset": "16:00", "night": "20:30", "sleep": "22:00"}
    return {
        "id": "default", "name": "Default",
        "sections": [
            {"id": s, "name": s.capitalize(),
             "trigger": {"type": "clock", "time": t}}
            for s, t in times.items()
        ],
    }


def load(path: str | Path, schema_path: str | Path) -> Config:
    """Load, validate, default and cross-check. TODO(Phase 9)."""
    raise NotImplementedError("Phase 9")


def loads(text: str, schema_path: str | Path) -> Config:
    raise NotImplementedError("Phase 9")


def save(cfg: dict, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(cfg, indent=2))
