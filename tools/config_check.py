"""
Adaptive Climate - config v2 migration offline check.

Uses the two example configs: examples/config.v1.example.json (a version 1
config as v1.0-poc shipped it) and examples/config.example.json (the same
install as version 2). Verifies:
  * migrating the v1 example gives exactly the v2 example, which validates
  * migration is idempotent and never mutates its input
  * custom values move to their new keys; dropped keys are gone
  * only v2's new keys are defaulted, never v1 keys
  * unknown versions, leftover v1 keys and bad outdoor settings are rejected,
    with the outdoor entity required for its source
  * blank() and load() produce valid version 2 configs
  * the save endpoint migrates, validates against v2 and writes version 2

    PYTHONPATH=. python tools/config_check.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import ConfigError, blank, load, migrate, validate   # noqa: E402

V1 = json.loads((ROOT / "examples" / "config.v1.example.json").read_text())
V2 = json.loads((ROOT / "examples" / "config.example.json").read_text())


def expect_error(cfg: dict, fragment: str) -> None:
    try:
        validate(migrate(cfg))
    except ConfigError as e:
        assert fragment in str(e), f"expected {fragment!r} in {e}"
        return
    raise AssertionError(f"accepted an invalid config (expected {fragment!r})")


def main() -> int:
    # --- the v1 example migrates to exactly the v2 example ---
    assert V1["version"] == 1 and V2["version"] == 2
    v1_before = copy.deepcopy(V1)
    migrated = migrate(V1)
    assert V1 == v1_before, "migrate() mutated its input"
    assert migrated == V2, "migrated v1 example differs from the v2 example"
    validate(V2)
    assert migrate(V2) == V2, "migrating a v2 config is not a no-op"
    print("examples: v1 example migrates to exactly the v2 example, which validates; "
          "idempotent, input untouched - OK")

    # --- custom values move; dropped keys go ---
    custom = copy.deepcopy(V1)
    custom["system"].update(max_step_degrees=1.5, correction_max_minutes=45)
    custom["learning"].update(analysis_window_days=30, bootstrap_min_days=3,
                              validity_delay_days=0, reactive_weight=4)
    out = migrate(custom)
    validate(out)
    assert out["system"]["control"]["boost_degrees"] == 1.5
    assert out["system"]["control"]["boost_max_minutes"] == 45
    assert "max_step_degrees" not in out["system"]
    assert "correction_max_minutes" not in out["system"]
    assert out["learning"] == {"reaction_rate": 0.5, "recency_half_life_days": 7}, \
        out["learning"]
    print("migration: max_step_degrees/correction_max_minutes move under system.control "
          "with their values; the four v1 learning keys (window, bootstrap, delay, "
          "reactive_weight) are dropped - OK")

    # --- only v2's new keys are defaulted ---
    sparse = copy.deepcopy(V1)
    sparse["system"] = {}
    del sparse["learning"]
    sparse["rooms"][0]["scenes"] = {"sleep": {"units": {"main_room_ac": {"mode": "off"}}}}
    out = migrate(sparse)
    validate(out)
    assert set(out["system"]) == {"control"}, out["system"]
    assert out["system"]["control"] == V2["system"]["control"]
    assert set(out["learning"]) == {"reaction_rate", "recency_half_life_days"}
    assert out["outdoor"] == V2["outdoor"]
    assert out["rooms"][0]["scenes"]["sleep"] == {"units": {"main_room_ac": {"mode": "off"}}}, \
        "a v1 default (transition_seconds) was filled in"
    assert all(r["heat_sources"] == [] and r["knobs"] == [] for r in out["rooms"])
    print("defaults: only the keys v2 added are filled; v1 keys such as "
          "transition_seconds and heartbeat_interval_minutes are left alone - OK")

    # --- rejections ---
    for version in (None, 3, "2"):
        bad = copy.deepcopy(V2)
        bad["version"] = version
        try:
            migrate(bad)
        except ConfigError as e:
            assert "unsupported config version" in str(e), e
        else:
            raise AssertionError(f"accepted version {version!r}")
    leftover = copy.deepcopy(V2)
    leftover["learning"]["bootstrap_min_days"] = 7
    expect_error(leftover, "bootstrap_min_days")
    stale_weight = copy.deepcopy(V2)
    stale_weight["learning"]["reactive_weight"] = 5
    expect_error(stale_weight, "reactive_weight")
    leftover = copy.deepcopy(V2)
    leftover["system"]["correction_max_minutes"] = 60
    expect_error(leftover, "correction_max_minutes")
    print("rejected: unknown versions, and v1 keys left in a version 2 config - OK")

    for source, key, good, wrong_domain in (
            ("forecast", "weather_entity", "weather.home", "sensor.outdoor"),
            ("probe", "probe_entity", "sensor.outdoor_temperature", "weather.home")):
        cfg = copy.deepcopy(V2)
        cfg["outdoor"]["source"] = source
        expect_error(cfg, f"outdoor.{key}: required when outdoor.source is '{source}'")
        del cfg["outdoor"][key]
        expect_error(cfg, f"outdoor.{key}: required")
        cfg["outdoor"][key] = wrong_domain
        expect_error(cfg, f"outdoor.{key}")
        cfg["outdoor"][key] = good
        validate(migrate(cfg))
    # The schema enforces the same rule on its own, for any other consumer.
    import jsonschema
    schema = json.loads((ROOT / "schema" / "config.schema.json").read_text())
    for source, key in (("forecast", "weather_entity"), ("probe", "probe_entity")):
        for missing in ("null", "absent"):
            cfg = copy.deepcopy(V2)
            cfg["outdoor"]["source"] = source
            if missing == "absent":
                del cfg["outdoor"][key]
            errors = list(jsonschema.Draft202012Validator(schema).iter_errors(cfg))
            assert errors and all(list(e.absolute_path)[:1] == ["outdoor"] for e in errors), \
                (source, missing, [e.message for e in errors])
    none_with_entities = copy.deepcopy(V2)
    none_with_entities["outdoor"].update(weather_entity="weather.home", probe_entity=None)
    validate(none_with_entities)
    bad_mode = copy.deepcopy(V2)
    bad_mode["outdoor"]["block_mode"] = "on"
    expect_error(bad_mode, "outdoor.block_mode")
    bad_knob = copy.deepcopy(V2)
    bad_knob["rooms"][0]["knobs"] = [{"name": "Lounge knob"}]
    expect_error(bad_knob, "'id' is a required property")
    good_room = copy.deepcopy(V2)
    good_room["rooms"][0].update(heat_sources=["switch.lounge_heater"],
                                 knobs=[{"id": "lounge", "name": "Lounge knob"}])
    validate(good_room)
    print("outdoor: weather_entity required for 'forecast', probe_entity for 'probe', "
          "domains checked; knobs need an id; heat sources accepted - OK")

    # --- blank() and load() ---
    validate(blank())
    assert blank()["version"] == 2 and blank()["system"]["control"]["offset_cap"] == 3
    assert load(ROOT / "examples" / "config.v1.example.json").raw == V2
    print("blank() is a valid version 2 config; load() migrates a v1 file - OK")

    # --- the save endpoint: migrate, validate against v2, write version 2 ---
    os.environ["AC_DATA_DIR"] = tempfile.mkdtemp()
    from fastapi import HTTPException
    import app.main as main
    assert main.api_save_config(copy.deepcopy(V1)) == {"ok": True}
    saved = json.loads(main.CONFIG_PATH.read_text())
    assert saved == V2, "save endpoint did not write the migrated version 2 config"
    assert main._raw_config() == V2
    main.CONFIG_PATH.write_text(json.dumps(V1))
    assert main._raw_config() == V2, "_raw_config() did not migrate a v1 file on read"
    try:
        main.api_save_config(leftover)
    except HTTPException as e:
        assert e.status_code == 400 and "correction_max_minutes" in e.detail, e.detail
    else:
        raise AssertionError("save endpoint accepted an invalid config")
    print("save endpoint: a v1 payload is saved as version 2; invalid v2 is a 400; "
          "a v1 file on disk reads back migrated - OK")

    print("\nALL CONFIG CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
