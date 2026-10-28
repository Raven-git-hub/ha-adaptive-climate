"""
Adaptive Climate - config loader.

Loads a config of either version, migrates version 1 forward to 2, fills
in the keys v2 added, and validates against schema/config.schema.json.
The loaded Config is the typed view the rest of the app uses.

Defaults are filled only for the keys v2 introduced (learning.reaction_rate,
system.sensor_stale_minutes, system.control.*, outdoor.*,
rooms[].heat_sources and rooms[].knobs), read from the schema so each lives
in one place. learning.recency_half_life_days, which an earlier v2 build
filled in, is taken out of any config: bands no longer use it. v1
keys are left as written: the code that reads them carries its own
fallbacks, and some differ from the schema's documented default (a scene's
transition_seconds falls back to 0 in the generator but documents 120), so
filling them would change behaviour.

STATUS: migration, defaults and schema validation are implemented. The
cross-reference checks the schema cannot express (a room's
schedule_profile must exist; every scene unit id must be a real unit in
that room) and the typed accessors are still to come (Roadmap Phase 9).
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "config.schema.json"
CURRENT_VERSION = 2

# v1 -> v2: system keys that moved under system.control, as (old, new).
_MOVED_TO_CONTROL = (("max_step_degrees", "boost_degrees"),
                     ("correction_max_minutes", "boost_max_minutes"))
# v1 learning keys v2 drops: the replay has no window, bootstrap or delay,
# and learns from reactions alone, so no reaction-vs-heartbeat weight.
_DROPPED_LEARNING = ("analysis_window_days", "bootstrap_min_days", "validity_delay_days",
                     "reactive_weight")
# Learning keys an earlier v2 build wrote that v2 now drops, from a config of
# either version: bands move by reaction, not by a recency-weighted mean.
_RETIRED_LEARNING = ("recency_half_life_days",)
# New keys inside objects that also hold v1 keys (only these get defaults).
_NEW_LEARNING = ("reaction_rate",)
_NEW_SYSTEM = ("sensor_stale_minutes",)
_NEW_ROOM = ("heat_sources", "knobs")


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
    return migrate({
        "version": CURRENT_VERSION,
        "homeassistant": {"base_url": "http://homeassistant.local:8123"},
        "system": {},
        "rooms": [],
        "schedule_profiles": [_default_profile()],
    })


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


def _load_schema(schema_path: str | Path) -> dict:
    return json.loads(Path(schema_path).read_text())


# ---------------------------------------------------------------------
# Migration and defaults
# ---------------------------------------------------------------------

def migrate(cfg: dict, schema_path: str | Path = SCHEMA_PATH) -> dict:
    """Return a version-2 copy of `cfg` (version 1 or 2) with v2's new keys
    defaulted. Never mutates its argument. On a v2 config it only fills
    new keys that are missing, so it is idempotent. Does not validate."""
    if not isinstance(cfg, dict):
        raise ConfigError("config must be a JSON object")
    out = copy.deepcopy(cfg)
    version = out.get("version")
    if version == 1:
        _v1_to_v2(out)
    elif version != CURRENT_VERSION:
        raise ConfigError(f"unsupported config version {version!r} "
                          f"(expected 1 or {CURRENT_VERSION})")
    learning = out.get("learning")
    if isinstance(learning, dict):
        for key in _RETIRED_LEARNING:
            learning.pop(key, None)
    _fill_new_defaults(out, _load_schema(schema_path))
    return out


def _v1_to_v2(cfg: dict) -> None:
    cfg["version"] = 2
    system = cfg.setdefault("system", {})
    if isinstance(system, dict):
        control = system.setdefault("control", {})
        for old, new in _MOVED_TO_CONTROL:
            if old in system:
                value = system.pop(old)
                if isinstance(control, dict):
                    control.setdefault(new, value)
    learning = cfg.get("learning")
    if isinstance(learning, dict):
        for key in _DROPPED_LEARNING:
            learning.pop(key, None)


def _fill(obj: Any, props: dict, keys: tuple[str, ...] | None = None) -> None:
    """Set each listed property (all of `props` when keys is None) that is
    missing from obj to its schema default."""
    if not isinstance(obj, dict):
        return   # malformed; validation reports it
    for key in (keys if keys is not None else tuple(props)):
        if key not in obj and "default" in props[key]:
            obj[key] = copy.deepcopy(props[key]["default"])


def _fill_new_defaults(cfg: dict, schema: dict) -> None:
    props = schema["properties"]
    _fill(cfg.setdefault("learning", {}), props["learning"]["properties"], _NEW_LEARNING)
    system = cfg.setdefault("system", {})
    if isinstance(system, dict):
        _fill(system, props["system"]["properties"], _NEW_SYSTEM)
        _fill(system.setdefault("control", {}),
              props["system"]["properties"]["control"]["properties"])
    _fill(cfg.setdefault("outdoor", {}), props["outdoor"]["properties"])
    room_props = schema["$defs"]["room"]["properties"]
    rooms = cfg.get("rooms")
    for room in rooms if isinstance(rooms, list) else []:
        _fill(room, room_props, _NEW_ROOM)


# ---------------------------------------------------------------------
# Validation and loading
# ---------------------------------------------------------------------

def validate(cfg: dict, schema_path: str | Path = SCHEMA_PATH) -> None:
    """Raise ConfigError if `cfg` is not a valid version-2 config."""
    import jsonschema

    outdoor = cfg.get("outdoor") if isinstance(cfg, dict) else None
    if isinstance(outdoor, dict):
        # The schema enforces these too; checked first for a clearer message.
        for source, key in (("forecast", "weather_entity"), ("probe", "probe_entity")):
            if outdoor.get("source") == source and not outdoor.get(key):
                raise ConfigError(f"outdoor.{key}: required when outdoor.source is '{source}'")

    validator = jsonschema.Draft202012Validator(_load_schema(schema_path))
    error = jsonschema.exceptions.best_match(validator.iter_errors(cfg))
    if error is not None:
        where = ".".join(str(p) for p in error.absolute_path) or "config"
        raise ConfigError(f"{where}: {error.message}")
    _check_unique_ids(cfg)


def _check_unique_ids(cfg: dict) -> None:
    """Ids the schema can't make unique: room ids, and unit and sensor ids
    within a room. A repeated sensor id once made a second sensor overwrite
    the first in every heartbeat, so a live sensor was never seen."""
    def unique(items: list, where: str, what: str, scope: str = "") -> None:
        seen: dict = {}
        for j, item in enumerate(items):
            iid = item.get("id")
            if iid in seen:
                raise ConfigError(
                    f"{where}.{j}.id: duplicate {what} id {iid!r}{scope} ({where}.{seen[iid]} "
                    f"has it too); each {what} needs its own id")
            seen[iid] = j

    rooms = cfg.get("rooms") or []
    unique(rooms, "rooms", "room")
    for i, room in enumerate(rooms):
        scope = f" in room {room.get('id')!r}"
        unique(room.get("units") or [], f"rooms.{i}.units", "unit", scope)
        unique(room.get("sensors") or [], f"rooms.{i}.sensors", "sensor", scope)


def loads(text: str, schema_path: str | Path = SCHEMA_PATH) -> Config:
    """Parse, migrate to version 2, default and validate."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"config is not valid JSON: {e}") from e
    cfg = migrate(raw, schema_path)
    validate(cfg, schema_path)
    return Config(raw=cfg)


def load(path: str | Path, schema_path: str | Path = SCHEMA_PATH) -> Config:
    """Load, migrate, default and validate the config file at `path`."""
    return loads(Path(path).read_text(), schema_path)


def save(cfg: dict, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(cfg, indent=2))
