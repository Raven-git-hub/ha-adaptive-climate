"""
Adaptive Climate - render generated helpers + automations offline.

Loads a config JSON, renders every helper spec and automation the
generator emits, checks the YAML round-trips, and prints a summary plus
one sample scene automation. No Docker, no Home Assistant.

    PYTHONPATH=. python tools/render_generated.py examples/config.example.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402
from app import generator as g  # noqa: E402


def main(path: str) -> int:
    config = json.loads(Path(path).read_text())
    out = g.render_all(config)

    # every automation must round-trip through YAML
    text = g.dump(out["automations"])
    reparsed = yaml.safe_load(text)
    assert reparsed == out["automations"], "automation YAML did not round-trip"
    assert yaml.safe_load(g.dump(out["helpers"])) == out["helpers"]

    print(f"helpers    : {len(out['helpers'])}")
    for h in out["helpers"]:
        extra = f"  options={h['options']}" if "options" in h else ""
        print(f"  {h['domain']}.{h['object_id']}{extra}")

    print(f"\nautomations: {len(out['automations'])}")
    for a in out["automations"]:
        print(f"  {a['id']}")

    sample = next(a for a in out["automations"] if a["id"].startswith("ac_scene_"))
    print("\n--- sample scene automation (YAML) ---")
    print(g.dump(sample))

    print("YAML round-trip OK")
    return 0


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else "examples/config.example.json"
    sys.exit(main(p))
