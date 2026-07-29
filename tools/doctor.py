"""
Adaptive Climate - connection doctor.

Points at a real Home Assistant and reports whether everything the runtime
depends on works there. NO sun checks (climate has no sun-relative
sections).

    export AC_HA_URL=http://192.168.1.251:8123
    export AC_HA_TOKEN=<paste long-lived token>
    # optional: check that the units you're going to control exist and support
    # the modes we need
    export AC_CONFIG=examples/config.example.json

    PYTHONPATH=. python tools/doctor.py

Checks, in order:
  1. REST /api/ reachable, token accepted
  2. REST /api/config (HA version, timezone, temperature unit)
  3. WebSocket handshake + auth
  4. Create + delete input_boolean.ac_doctor_probe (proves Deploy will work)
  5. At least one climate.* entity exists
  6. (if AC_CONFIG set) every configured unit exists and supports the modes
     the state machine needs; sensors exist; fan mode reported per unit
     while the unit is in `cool` (see docs/HARDWARE.md)

Exits non-zero on the first hard failure. Reports (does not fail on) mode
gaps and sensors that are currently unavailable.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ha import HARest, HAWebSocket, HAAuthError, HAError  # noqa: E402

REQUIRED_HVAC = ("cool", "fan_only", "dry", "off")   # Normal, Warming, Leak, and off
REQUIRED_FAN = ("low", "medium")                     # Normal / Cooling

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
OK, FAIL, WARN = f"{GREEN}\u2713{RESET}", f"{RED}\u2717{RESET}", f"{YELLOW}!{RESET}"

def line(marker, msg):  print(f"  {marker} {msg}")


async def _run(url: str, token: str, verify_ssl: bool,
               config_path: str | None) -> int:
    print(f"\nAdaptive Climate doctor -> {url}")
    print("=" * 62)

    hard_failures = 0
    warnings = 0

    # --- 1. REST reachable + token ----------------------------------
    rest = HARest(url, token, verify_ssl=verify_ssl)
    try:
        pong = await rest.ping()
        line(OK, f"REST reachable: {pong.get('message', 'ok')}")
    except HAAuthError as e:
        line(FAIL, f"REST auth failed: {e}"); return 2
    except Exception as e:
        line(FAIL, f"REST unreachable: {e}"); return 2

    # --- 2. HA config -----------------------------------------------
    try:
        cfg = await rest.config()
        line(OK, f"HA {cfg.get('version')}  tz={cfg.get('time_zone')}  "
                 f"unit={cfg.get('unit_system', {}).get('temperature')}")
    except Exception as e:
        line(FAIL, f"REST /api/config failed: {e}"); hard_failures += 1

    # --- 3. WebSocket handshake + auth ------------------------------
    ws = HAWebSocket(url, token, verify_ssl=verify_ssl)
    try:
        await ws.connect()
        line(OK, f"WebSocket connected (HA {ws.ha_version})")
    except HAAuthError as e:
        line(FAIL, f"WebSocket auth failed: {e}")
        await rest.close(); return 2
    except Exception as e:
        line(FAIL, f"WebSocket connect failed: {e}")
        await rest.close(); return 2

    # --- 4. Helper create/delete round-trip -------------------------
    probe_id = "ac_doctor_probe"
    created = False
    try:
        try:
            await ws.create_helper("input_boolean",
                                   {"name": probe_id, "icon": "mdi:stethoscope"})
            created = True
            # HA uses object_id == slug(name). Confirm it exists via REST.
            _ = await rest.state(f"input_boolean.{probe_id}")
            line(OK, f"created + read back input_boolean.{probe_id}")
        except HAError as e:
            # If it exists from a prior aborted run, that still proves auth
            # scope. Try to read it and continue to delete.
            try:
                _ = await rest.state(f"input_boolean.{probe_id}")
                line(WARN, f"probe already existed ({e}); reusing")
                warnings += 1
                created = True
            except Exception:
                line(FAIL, f"could not create input_boolean helper: {e}")
                hard_failures += 1
    finally:
        if created:
            try:
                await ws.delete_helper("input_boolean", probe_id)
                line(OK, f"deleted input_boolean.{probe_id}")
            except Exception as e:
                line(WARN, f"probe not cleaned up ({e}); remove it manually")
                warnings += 1

    # --- 5. At least one climate.* entity ---------------------------
    try:
        states = await rest.states()
        climates = [s for s in states if s["entity_id"].startswith("climate.")]
        if climates:
            line(OK, f"climate entities present: {len(climates)}")
        else:
            line(WARN, "no climate entities found; nothing to control yet")
            warnings += 1
    except Exception as e:
        line(FAIL, f"could not list states: {e}"); hard_failures += 1
        states = []

    # --- 6. Configured units + sensors ------------------------------
    if config_path:
        print(f"\nchecking against {config_path}")
        try:
            config = json.loads(Path(config_path).read_text())
        except Exception as e:
            line(FAIL, f"could not read config: {e}"); hard_failures += 1
            config = None

        if config:
            by_id = {s["entity_id"]: s for s in states}
            for room in config.get("rooms", []):
                if not room.get("enabled", True):
                    continue
                print(f"  room: {room['name']}")
                for u in room.get("units", []):
                    e = by_id.get(u["entity_id"])
                    if not e:
                        line(FAIL, f"    unit {u['entity_id']} not found")
                        hard_failures += 1; continue
                    attrs = e.get("attributes", {}) or {}
                    hvac_modes = set(attrs.get("hvac_modes") or [])
                    fan_modes = attrs.get("fan_modes")   # may be None in some states
                    missing_hvac = [m for m in REQUIRED_HVAC if m not in hvac_modes]
                    if missing_hvac:
                        line(FAIL, f"    {u['entity_id']} missing hvac_modes: {missing_hvac}")
                        hard_failures += 1
                    else:
                        line(OK, f"    {u['entity_id']} has cool/fan_only/dry/off")

                    if fan_modes is None:
                        line(WARN, f"    {u['entity_id']} reports fan_modes=None "
                                   f"(likely current mode {e['state']!r} hides them; "
                                   "docs/HARDWARE.md); re-check while in cool")
                        warnings += 1
                    else:
                        missing_fan = [m for m in REQUIRED_FAN if m not in fan_modes]
                        if missing_fan:
                            line(WARN, f"    {u['entity_id']} missing fan modes {missing_fan}; "
                                       "generator will skip set_fan_mode on this unit")
                            warnings += 1
                        else:
                            line(OK, f"    {u['entity_id']} fan supports low + medium")

                    step = attrs.get("target_temp_step")
                    rng = (attrs.get("min_temp"), attrs.get("max_temp"))
                    line(OK, f"    {u['entity_id']} step={step}  range={rng[0]}\u2013{rng[1]}")

                for s in room.get("sensors", []):
                    e = by_id.get(s["entity_id"])
                    if not e:
                        line(FAIL, f"    sensor {s['entity_id']} not found")
                        hard_failures += 1; continue
                    if e["state"] in ("unavailable", "unknown", None):
                        line(WARN, f"    sensor {s['entity_id']} is {e['state']!r} now "
                                   "(no vote until it returns)")
                        warnings += 1
                    else:
                        unit = e.get("attributes", {}).get("unit_of_measurement")
                        line(OK, f"    sensor {s['entity_id']} = {e['state']} {unit or ''}")

    await ws.close()
    await rest.close()

    print("\n" + "=" * 62)
    if hard_failures:
        print(f"{RED}FAILED{RESET}: {hard_failures} hard problem(s); "
              f"{warnings} warning(s).")
        return 1
    if warnings:
        print(f"{GREEN}OK{RESET} with {warnings} warning(s).")
        return 0
    print(f"{GREEN}ALL CHECKS PASSED{RESET}")
    return 0


def main() -> int:
    url = os.environ.get("AC_HA_URL")
    token = os.environ.get("AC_HA_TOKEN")
    verify_ssl = os.environ.get("AC_HA_VERIFY_SSL", "true").lower() != "false"
    config = os.environ.get("AC_CONFIG")
    if not url or not token:
        print("Set AC_HA_URL and AC_HA_TOKEN first."); return 2
    try:
        return asyncio.run(_run(url, token, verify_ssl, config))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
