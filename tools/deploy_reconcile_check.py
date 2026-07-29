"""
Adaptive Climate - deploy reconciliation offline test.

Exercises app.deploy.deploy() against stub REST/WebSocket objects (no
network, no Home Assistant) across three sequential deploys: create from
nothing, idempotent no-op reuse, and ledger-based pruning when a room is
removed from config. Proves the create/reuse/prune behaviour before it
ever runs against a live instance.

    PYTHONPATH=. python tools/deploy_reconcile_check.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import deploy as d  # noqa: E402


class StubRest:
    """Records automation writes/deletes; entity existence is fixed."""
    def __init__(self, entity_ids):
        self._entities = set(entity_ids)
        self.set_calls: list[str] = []
        self.delete_calls: list[str] = []

    async def states(self):
        return [{"entity_id": e} for e in self._entities]

    async def set_automation(self, aid, cfg):
        self.set_calls.append(aid)

    async def delete_automation(self, aid):
        self.delete_calls.append(aid)


class StubWS:
    """In-memory helper storage, matching the {domain}/list,create,delete
    shape app.deploy expects from app.ha.HAWebSocket."""
    def __init__(self):
        self.store = {dom: {} for dom in d.HELPER_DOMAINS}

    async def list_helpers(self, domain):
        return [{"id": oid, **v} for oid, v in self.store[domain].items()]

    async def create_helper(self, domain, payload):
        obj = payload["name"]
        self.store[domain][obj] = payload
        return {"id": obj}

    async def delete_helper(self, domain, helper_id):
        self.store[domain].pop(helper_id, None)


async def main() -> int:
    cfg = json.loads((Path(__file__).resolve().parent.parent /
                      "examples" / "config.example.json").read_text())
    entities = set()
    for r in cfg["rooms"]:
        for u in r["units"]:
            entities.add(u["entity_id"])
        for s in r["sensors"]:
            entities.add(s["entity_id"])

    tmp = Path(tempfile.mkdtemp())
    rest = StubRest(entities)
    ws = StubWS()

    # deploy 1: from nothing
    r1 = await d.deploy(cfg, rest, ws, tmp)
    assert r1.ok, r1.problems
    assert len(r1.helpers_created) == 12
    assert len(r1.automations_written) == 24
    assert r1.automations_removed == r1.helpers_removed == []
    print("deploy 1 (from nothing): created 12 helpers, wrote 24 automations - OK")

    # deploy 2: identical config -> idempotent, nothing pruned
    r2 = await d.deploy(cfg, rest, ws, tmp)
    assert r2.helpers_created == []
    assert len(r2.helpers_reused) == 12
    assert r2.automations_removed == []
    assert len(r2.automations_written) == 24  # always upserted
    print("deploy 2 (same config): idempotent, 12 reused, nothing pruned - OK")

    # deploy 3: baby_room removed -> only its own artifacts prune
    cfg3 = json.loads(json.dumps(cfg))
    cfg3["rooms"] = [r for r in cfg3["rooms"] if r["id"] != "baby_room"]
    r3 = await d.deploy(cfg3, rest, ws, tmp)
    assert len(r3.automations_removed) == 8
    assert all("baby_room" in a for a in r3.automations_removed)
    assert len(r3.helpers_removed) == 4
    assert all("baby_room" in h for h in r3.helpers_removed)
    assert not any("main_room" in a or "main_bedroom" in a for a in r3.automations_removed)
    print("deploy 3 (baby_room removed): pruned exactly its 8 automations + 4 helpers, "
          "other rooms untouched - OK")

    print("\nALL DEPLOY RECONCILIATION TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
