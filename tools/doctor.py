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
    PYTHONPATH=. python tools/doctor.py --v2-preflight    # before the v2 cutover

Checks, in order:
  1. REST /api/ reachable, token accepted
  2. REST /api/config (HA version, timezone, temperature unit)
  3. WebSocket handshake + auth
  4. Create + delete input_boolean.ac_doctor_probe (proves Deploy will work)
  5. At least one climate.* entity exists
  6. (if AC_CONFIG set) every configured unit exists and supports the modes
     the state machine needs; sensors exist; fan mode reported per unit
     while the unit is in `cool` (see docs/HARDWARE.md)

  7. (--v2-preflight) the Home Assistant behaviour v2's generated helpers
     and templates rely on, tried for real on throwaway ac_preflight_*
     helpers created and deleted through the same helper API deploy uses:
       - input_datetime.set_datetime with a timestamp round-trips through
         the timestamp attribute
       - an input_number shaped like ac_base_* accepts 0 and a mid-range value
       - HA's own Jinja (the template API) renders state_attr(..., 'timestamp'),
         the trigger-style s.attributes.get(...) snapshot the manual-change
         automation uses, and now().timestamp()
     The helpers are deleted afterwards, and leftovers from an earlier run
     first, whatever fails in between.

Exits non-zero on the first hard failure. Reports (does not fail on) mode
gaps and sensors that are currently unavailable.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import generator as g                                 # noqa: E402
from app.ha import HARest, HAWebSocket, HAAuthError, HAError  # noqa: E402

REQUIRED_HVAC = ("cool", "dry", "off")   # cool and boost, leak, rest and the off states
REQUIRED_FAN = ("low", "medium")         # cool / boost

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
OK, FAIL, WARN = f"{GREEN}\u2713{RESET}", f"{RED}\u2717{RESET}", f"{YELLOW}!{RESET}"

def line(marker, msg):  print(f"  {marker} {msg}")


# ---------------------------------------------------------------------
# v2 preflight
# ---------------------------------------------------------------------

PREFLIGHT_PREFIX = "ac_preflight_"
PREFLIGHT_DOMAINS = ("input_datetime", "input_number")
ITEM_DATETIME = "input_datetime: set_datetime with a timestamp round-trips"
ITEM_NUMBER = "input_number (0-100, step 0.5, as ac_base_*): accepts 0 and 23.5"
ITEM_TEMPLATE = "template API: timestamp attribute, .attributes.get, now()"
ITEM_CLEANUP = "throwaway helpers deleted, nothing left behind"


async def _state_attr(rest, entity_id: str, attribute: str | None = None, tries: int = 10):
    """An entity's state (or one attribute), waiting briefly for a helper
    that was only just created or changed."""
    last = None
    for _ in range(tries):
        try:
            s = await rest.state(entity_id)
            last = s.get("state") if attribute is None else (s.get("attributes") or {}).get(attribute)
            if last not in (None, "unknown"):
                return last
        except Exception:
            pass
        await asyncio.sleep(0.3)
    return last


async def _clear_preflight(ws) -> tuple[list[str], list[str]]:
    """Try to delete every ac_preflight_* helper, carrying on past one that
    fails. Returns (removed, still there)."""
    removed, failed = [], []
    for domain in PREFLIGHT_DOMAINS:
        for item in await ws.list_helpers(domain):
            oid = item.get("id") or item.get("object_id") or ""
            if not oid.startswith(PREFLIGHT_PREFIX):
                continue
            try:
                await ws.delete_helper(domain, oid)
                removed.append(f"{domain}.{oid}")
            except Exception:
                failed.append(f"{domain}.{oid}")
    return removed, failed


async def v2_preflight(rest, ws) -> list[tuple[str, bool, str]]:
    """Try, on throwaway helpers, the Home Assistant behaviour v2 relies on.
    Returns (item, passed, detail) for each item; the helpers are always
    deleted, and the last item says whether that worked."""
    results: list[tuple[str, bool, str]] = []
    dt_name, num_name = f"{PREFLIGHT_PREFIX}since", f"{PREFLIGHT_PREFIX}base"
    lo, hi, step = g.BASE_RANGE     # the ac_base_* helper's shape (ITEM_NUMBER names it)
    stamp = int(time.time()) - 3 * 86400 + 37        # whole seconds, well clear of the default
    try:
        leftovers, stuck = await _clear_preflight(ws)
        if stuck:
            raise HAError(f"could not remove leftovers from an earlier run: {', '.join(stuck)}")
        if leftovers:
            results.append(("leftovers from an earlier run removed", True, ", ".join(leftovers)))

        dt = await ws.create_helper("input_datetime",
                                    {"name": dt_name, "has_date": True, "has_time": True})
        num = await ws.create_helper("input_number", {"name": num_name, "min": lo, "max": hi,
                                                      "step": step, "mode": "box"})
        dt_id = f"input_datetime.{(dt or {}).get('id') or dt_name}"
        num_id = f"input_number.{(num or {}).get('id') or num_name}"

        # 1. set_datetime(timestamp) -> the timestamp attribute
        try:
            await rest.call_service("input_datetime", "set_datetime",
                                    {"entity_id": dt_id, "timestamp": stamp})
            got = await _state_attr(rest, dt_id, "timestamp")
            ok = got is not None and float(got) == float(stamp)
            results.append((ITEM_DATETIME, ok, f"sent {stamp}, timestamp attribute {got}"))
        except Exception as e:
            results.append((ITEM_DATETIME, False, f"{type(e).__name__}: {e}"))

        # 2. an ac_base_*-shaped input_number takes 0 and a setpoint
        try:
            seen = []
            for value in (0, 23.5):
                await rest.call_service("input_number", "set_value",
                                        {"entity_id": num_id, "value": value})
                for _ in range(10):
                    state = await _state_attr(rest, num_id)
                    if state is not None and float(state) == value:
                        break
                    await asyncio.sleep(0.3)
                seen.append(state)
            ok = [float(v) if v not in (None, "unknown") else None for v in seen] == [0.0, 23.5]
            results.append((ITEM_NUMBER, ok, f"read back {seen}"))
        except Exception as e:
            results.append((ITEM_NUMBER, False, f"{type(e).__name__}: {e}"))

        # 3. HA's own Jinja, through the template API
        try:
            checks = []
            text = await rest.render_template(f"{{{{ state_attr('{dt_id}', 'timestamp') | int }}}}")
            checks.append(("state_attr timestamp", text.strip() == str(stamp), text.strip()))
            # the manual-change automation's snapshot of trigger.from_state /
            # to_state, over a state object read the same way
            snap_src = g._climate_snapshot(f"states.{dt_id}")
            snap = ast.literal_eval((await rest.render_template(snap_src)).strip())
            want = {"mode": None, "temperature": None, "fan": None}
            got_dt = await _state_attr(rest, dt_id)
            want["mode"] = got_dt
            checks.append(("s.attributes.get snapshot", snap == want, repr(snap)))
            attr = await rest.render_template(
                f"{{%- set s = states.{dt_id} -%}}{{{{ s.attributes.get('timestamp') | int }}}}")
            checks.append((".attributes.get('timestamp')", attr.strip() == str(stamp), attr.strip()))
            now_text = (await rest.render_template(g.NOW_TS)).strip()
            skew = abs(int(now_text) - int(time.time()))
            checks.append((f"now().timestamp() | int (clock skew {skew}s)", skew < 300, now_text))
            ok = all(c[1] for c in checks)
            results.append((ITEM_TEMPLATE, ok,
                            "; ".join(f"{n}: {'ok' if p else 'got ' + v}" for n, p, v in checks)))
        except Exception as e:
            results.append((ITEM_TEMPLATE, False, f"{type(e).__name__}: {e}"))
    except Exception as e:
        results.append(("create the throwaway helpers", False, f"{type(e).__name__}: {e}"))
        done = {item for item, _, _ in results}
        results += [(item, False, "not run: the helpers could not be created")
                    for item in (ITEM_DATETIME, ITEM_NUMBER, ITEM_TEMPLATE) if item not in done]
    finally:
        try:
            for _ in range(3):                         # sweep until a pass finds nothing
                removed, stuck = await _clear_preflight(ws)
                if not removed and not stuck:
                    break
            results.append((ITEM_CLEANUP, not removed and not stuck,
                            ", ".join(removed + stuck) or f"{PREFLIGHT_PREFIX}* gone"))
        except Exception as e:
            results.append((ITEM_CLEANUP, False,
                            f"{type(e).__name__}: {e}; delete {PREFLIGHT_PREFIX}* helpers "
                            "by hand (Settings > Devices & services > Helpers)"))
    return results


async def _run(url: str, token: str, verify_ssl: bool,
               config_path: str | None, preflight: bool = False) -> int:
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
                        line(OK, f"    {u['entity_id']} has cool/dry/off")

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

    # --- 7. v2 preflight (opt-in) ----------------------------------
    if preflight:
        print("\nv2 preflight (throwaway ac_preflight_* helpers)")
        for item, passed, detail in await v2_preflight(rest, ws):
            line(OK if passed else FAIL, f"{item}: {detail}")
            hard_failures += not passed

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
    preflight = "--v2-preflight" in sys.argv[1:]
    if not url or not token:
        print("Set AC_HA_URL and AC_HA_TOKEN first."); return 2
    try:
        return asyncio.run(_run(url, token, verify_ssl, config, preflight))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
