"""
Adaptive Climate - compare one deployed automation with our render.

Fetches an automation from Home Assistant through the same API deploy uses
(app.ha.HARest.get_automation: GET /api/config/automation/config/<id>) and
prints, for each id asked about:

  * whether our render has it, and whether our deploy manifest records it;
  * what the read returned (a copy, no copy, or the error);
  * the first difference as-is (both sides through JSON, id dropped: the
    comparison deploy used before it normalised), and the first difference
    after app.deploy.normalised, which is what deploy compares now;
  * the verdict: unchanged, or why a deploy would write it again.

With --show it also prints both sides in full (ours, then Home Assistant's),
for sharing. --all checks every automation the config renders, one line
each. Nothing is written to Home Assistant.

    AC_HA_URL=... AC_HA_TOKEN=... python tools/automation_diff.py [--show] [ID ...]
    python tools/automation_diff.py --all

On the host (the image includes tools/):

    docker compose run --rm -e AC_HA_URL -e AC_HA_TOKEN -e AC_HA_VERIFY_SSL \\
      adaptive-climate python tools/automation_diff.py ac_watchdog_main_room

The id defaults to ac_watchdog_main_room. The config is AC_CONFIG, else
<AC_DATA_DIR>/config/config.json (AC_DATA_DIR defaults to /data, then
./data); the manifest is <AC_DATA_DIR>/config/deploy_manifest.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import deploy as d                       # noqa: E402
from app import generator as g                    # noqa: E402
from app.config import ConfigError, migrate       # noqa: E402

DEFAULT_ID = "ac_watchdog_main_room"


def _as_is(automation: dict) -> Any:
    """The comparison deploy used before it normalised: JSON, no id."""
    return json.loads(json.dumps({k: v for k, v in automation.items() if k != "id"}))


async def diagnose(config: dict, rest: Any, manifest: dict, aid: str,
                   show: bool = False) -> list[str]:
    """The report for one automation id, as lines."""
    rendered = {a["id"]: a for a in g.render_all(config)["automations"]}
    ours = rendered.get(aid)
    if ours is None:
        near = ", ".join(sorted(rendered)[:12])
        return [f"{aid}: not in our render of this config (it renders: {near}, ...)"]
    out = [f"{aid}:",
           f"  in our deploy manifest: {'yes' if aid in manifest.get('automations', []) else 'NO'}"]
    try:
        theirs = await rest.get_automation(aid)
    except Exception as e:
        out.append(f"  read from Home Assistant: FAILED ({type(e).__name__}: {e})")
        theirs = None
    else:
        out.append("  read from Home Assistant: " + (
            "no copy (404)" if theirs is None else
            f"a copy with keys {list(theirs)}"))
    if theirs is not None:
        raw = d.first_difference(_as_is(ours), _as_is(theirs))
        norm = d.first_difference(d.normalised(ours), d.normalised(theirs))
        out.append("  as-is (JSON, id dropped): " + ("equal" if raw is None
                                                     else f"first difference {raw}"))
        out.append("  normalised (what deploy compares): " + (
            "equal" if norm is None else f"first difference {norm}"))
    why, _ = await d.ha_copy_differs(aid, ours, rest, manifest)
    out.append("  verdict: " + ("unchanged - a deploy leaves it as it is" if why is None
                                else f"a deploy would write it: {why}"))
    if show:
        out.append("  ours:")
        out.append(json.dumps(ours, indent=2, ensure_ascii=False))
        out.append("  Home Assistant's:")
        out.append(json.dumps(theirs, indent=2, ensure_ascii=False))
    return out


async def summary(config: dict, rest: Any, manifest: dict) -> list[str]:
    """--all: one line per rendered automation."""
    lines, unchanged = [], 0
    for a in g.render_all(config)["automations"]:
        why, _ = await d.ha_copy_differs(a["id"], a, rest, manifest)
        unchanged += why is None
        lines.append(f"  {'unchanged' if why is None else 'WRITE    '} {a['id']}"
                     + ("" if why is None else f": {why}"))
    return lines + [f"{unchanged} of {len(lines)} unchanged"]


def _data_dir() -> Path:
    env = os.environ.get("AC_DATA_DIR")
    if env:
        return Path(env)
    return Path("/data") if Path("/data/config").exists() else Path("data")


async def _run(args: argparse.Namespace) -> int:
    from app.ha import HARest
    url, token = os.environ.get("AC_HA_URL"), os.environ.get("AC_HA_TOKEN")
    if not url or not token:
        print("set AC_HA_URL and AC_HA_TOKEN")
        return 2
    data_dir = _data_dir()
    config_path = Path(os.environ.get("AC_CONFIG") or data_dir / "config" / "config.json")
    try:
        config = migrate(json.loads(config_path.read_text()))
    except (OSError, json.JSONDecodeError, ConfigError) as e:
        print(f"could not read the config at {config_path}: {e}")
        return 2
    manifest = d.load_manifest(data_dir)
    print(f"config {config_path}; manifest {d._manifest_path(data_dir)} "
          f"({len(manifest.get('automations', []))} automations recorded); "
          f"Home Assistant {url}")
    rest = HARest(url, token,
                  verify_ssl=os.environ.get("AC_HA_VERIFY_SSL", "true").lower() != "false")
    try:
        if args.all:
            lines = await summary(config, rest, manifest)
        else:
            lines = []
            for aid in args.ids or [DEFAULT_ID]:
                lines += await diagnose(config, rest, manifest, aid, show=args.show)
    finally:
        await rest.close()
    print("\n".join(lines))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("ids", nargs="*", help=f"automation ids (default {DEFAULT_ID})")
    p.add_argument("--show", action="store_true", help="print both sides in full")
    p.add_argument("--all", action="store_true", help="one line per rendered automation")
    return asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
