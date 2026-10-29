"""
Adaptive Climate - deploy reconciliation offline test.

Exercises app.deploy.deploy() against stub REST/WebSocket objects (no
network, no Home Assistant) across four sequential deploys: over a v1
install (create, prune the v1 helpers, leave foreign ones alone, set the
starting values of what was created), idempotent reuse, an in-place
update when offset_cap changes, and ledger-based pruning when a room is
removed from config. Deploy only ever touches `ac_*` and sends no command
to any unit. Before each deploy, check() - the dry run - must write nothing
and list exactly what that deploy then does.

    PYTHONPATH=. python tools/deploy_reconcile_check.py
"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import control      # noqa: E402
from app import deploy as d  # noqa: E402


class StubRest:
    """Records automation writes/deletes and service calls; entity
    existence is fixed."""
    def __init__(self, entity_ids):
        self._entities = set(entity_ids)
        self.set_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.service_calls: list[tuple[str, str, dict]] = []

    async def states(self):
        return [{"entity_id": e, "state": "on" if e.startswith("input_boolean.ac_hold_") else "x"}
                for e in self._entities]

    async def set_automation(self, aid, cfg):
        self.set_calls.append(aid)

    async def delete_automation(self, aid):
        self.delete_calls.append(aid)

    async def call_service(self, domain, service, data=None):
        self.service_calls.append((domain, service, data or {}))


class StubWS:
    """In-memory helper storage, matching the {domain}/list,create,update,
    delete shape app.deploy expects from app.ha.HAWebSocket."""
    def __init__(self):
        self.store = {dom: {} for dom in d.HELPER_DOMAINS}
        self.updates: list[tuple[str, str, dict]] = []

    async def list_helpers(self, domain):
        return [{"id": oid, **v} for oid, v in self.store[domain].items()]

    async def create_helper(self, domain, payload):
        obj = payload["name"]
        self.store[domain][obj] = payload
        return {"id": obj}

    async def update_helper(self, domain, helper_id, payload):
        self.updates.append((domain, helper_id, payload))
        self.store[domain][helper_id] = payload

    async def delete_helper(self, domain, helper_id):
        self.store[domain].pop(helper_id, None)


PLAN = ("helpers_created", "helpers_reused", "helpers_updated", "helpers_removed",
        "automations_written", "automations_removed")
dry_runs = []


async def check_then_deploy(cfg, rest, ws, tmp):
    """The dry run first: it must write nothing, and its lists must be
    exactly what the real deploy that follows then does."""
    manifest = d._manifest_path(tmp)
    before = (copy.deepcopy(ws.store), len(ws.updates), len(rest.set_calls),
              len(rest.delete_calls), len(rest.service_calls),
              manifest.read_text() if manifest.exists() else None)
    dry = await d.check(cfg, rest, ws, tmp)
    after = (copy.deepcopy(ws.store), len(ws.updates), len(rest.set_calls),
             len(rest.delete_calls), len(rest.service_calls),
             manifest.read_text() if manifest.exists() else None)
    assert dry.dry_run and dry.ok, dry.problems
    assert after == before, "the dry run wrote something"
    real = await d.deploy(cfg, rest, ws, tmp)
    for key in PLAN:
        assert sorted(getattr(dry, key)) == sorted(getattr(real, key)), \
            f"{key}: check said {sorted(getattr(dry, key))}, deploy did {sorted(getattr(real, key))}"
    dry_runs.append({k: len(getattr(dry, k)) for k in PLAN})
    return real


async def main() -> int:
    cfg = json.loads((Path(__file__).resolve().parent.parent /
                      "examples" / "config.example.json").read_text())
    entities = set()
    for r in cfg["rooms"]:
        for u in r["units"]:
            entities.add(u["entity_id"])
        for s in r["sensors"]:
            entities.add(s["entity_id"])
    rooms, units = len(cfg["rooms"]), sum(len(r["units"]) for r in cfg["rooms"])
    n_helpers = 5 * rooms + 8 * units          # guard, hold, scene, 2 vote buttons; 8 per unit
    n_automations = 9 * rooms + units          # 6 scenes, maintenance, watchdog, vote; manual per unit

    entities.add("input_boolean.ac_hold_main_bedroom")      # left on by v1
    tmp = Path(tempfile.mkdtemp())
    rest = StubRest(entities)
    ws = StubWS()
    # what a v1 deploy left behind, plus a helper that isn't ours
    for r in cfg["rooms"]:
        ws.store["input_datetime"][f"ac_correction_started_{r['id']}"] = {"name": "x"}
    ws.store["input_datetime"]["foreign_clock"] = {"name": "foreign_clock"}
    ws.store["input_number"]["foreign_level"] = {"name": "foreign_level"}

    # deploy 1: from a v1 install
    r1 = await check_then_deploy(cfg, rest, ws, tmp)
    assert r1.ok, r1.problems
    assert len(r1.helpers_created) == n_helpers, len(r1.helpers_created)
    assert len(r1.automations_written) == n_automations, len(r1.automations_written)
    assert sorted(r1.helpers_removed) == sorted(
        f"input_datetime.ac_correction_started_{r['id']}" for r in cfg["rooms"]), r1.helpers_removed
    assert "foreign_clock" in ws.store["input_datetime"]
    assert "foreign_level" in ws.store["input_number"]
    assert all(h.split(".", 1)[1].startswith("ac_") for h in r1.helpers_created)
    print(f"deploy 1 (over a v1 install): created {n_helpers} helpers, wrote {n_automations} "
          "automations, pruned the v1 correction clocks, left foreign helpers alone - OK")
    holds = [n for n in r1.notes if "ac_hold_" in n]
    assert len(holds) == 1 and "ac_hold_main_bedroom is on" in holds[0], r1.notes
    assert not any(c[2].get("entity_id", "").startswith("input_boolean.ac_hold_")
                   for c in rest.service_calls)
    print("deploy 1: a hold left on by v1 is reported, not cleared - OK")

    # the new unit helpers: the right domains and ranges, and no `initial`
    # (HA would reset a helper to it at every restart)
    room, unit = cfg["rooms"][0]["id"], cfg["rooms"][0]["units"][0]["id"]
    offset = ws.store["input_number"][f"ac_offset_{room}_{unit}"]
    base = ws.store["input_number"][f"ac_base_{room}_{unit}"]
    state = ws.store["input_select"][f"ac_state_{room}_{unit}"]
    cap = cfg["system"]["control"]["offset_cap"]
    assert (offset["min"], offset["max"], offset["step"]) == (-cap, cap, 1), offset
    assert base["min"] == 0 and base["max"] >= 30, base
    assert state["options"] == list(control.STATES), state
    for helper in (f"ac_since_{room}_{unit}", f"ac_grace_until_{room}_{unit}"):
        assert ws.store["input_datetime"][helper]["has_time"], helper
    assert f"ac_unblocked_{room}_{unit}" in ws.store["input_boolean"]
    assert f"ac_forced_{room}_{unit}" in ws.store["input_boolean"]
    assert f"ac_section_rest_{room}_{unit}" in ws.store["input_boolean"]
    assert sorted(ws.store["input_button"]) == sorted(
        f"ac_{d}_{r['id']}" for r in cfg["rooms"] for d in ("too_cold", "too_hot"))
    assert all(spec == {"name": oid} for oid, spec in ws.store["input_button"].items())
    assert not any("initial" in spec for dom in ws.store.values() for spec in dom.values())
    print("deploy 1: per unit an input_select state, input_number offset (-cap..cap) and base "
          "(from 0), input_datetime since and grace_until, input_boolean unblocked, forced "
          "and section_rest; per room the input_button too_cold and too_hot; none has an "
          "initial - OK")

    # starting values: since now, offset 0, state manual - for what was just
    # created; nothing else is set, and no unit is sent a command
    starts = {(c[0], c[1]): [] for c in rest.service_calls}
    for domain, service, data in rest.service_calls:
        starts[(domain, service)].append(data)
    assert set(starts) == {("input_datetime", "set_datetime"), ("input_number", "set_value"),
                           ("input_select", "select_option")}, set(starts)
    assert all(len(v) == units for v in starts.values()), starts
    assert all(c["entity_id"].startswith("input_datetime.ac_since_") and
               abs(c["timestamp"] - time.time()) < 60
               for c in starts[("input_datetime", "set_datetime")])
    assert all(c["entity_id"].startswith("input_number.ac_offset_") and c["value"] == 0
               for c in starts[("input_number", "set_value")])
    assert all(c["entity_id"].startswith("input_select.ac_state_") and c["option"] == "manual"
               for c in starts[("input_select", "select_option")])
    print(f"deploy 1: since set to now, offset to 0 and state to manual for the {units} new "
          "units; no climate command - OK")

    # deploy 2: identical config -> idempotent, nothing pruned, nothing set
    rest.service_calls.clear()
    r2 = await check_then_deploy(cfg, rest, ws, tmp)
    assert r2.helpers_created == r2.helpers_updated == r2.helpers_removed == []
    assert len(r2.helpers_reused) == n_helpers
    assert r2.automations_removed == []
    assert len(r2.automations_written) == n_automations  # always upserted
    assert rest.service_calls == [], rest.service_calls
    print(f"deploy 2 (same config): idempotent, {n_helpers} reused, nothing pruned, no starting "
          "values re-set (a running boost keeps its clock) - OK")

    # deploy 3: a wider offset cap updates the offset helpers in place
    cfg3 = json.loads(json.dumps(cfg))
    cfg3["system"]["control"]["offset_cap"] = cap + 1
    r3 = await check_then_deploy(cfg3, rest, ws, tmp)
    assert sorted(r3.helpers_updated) == sorted(
        f"input_number.ac_offset_{r['id']}_{u['id']}" for r in cfg["rooms"] for u in r["units"])
    assert all(p["min"] == -(cap + 1) and p["max"] == cap + 1 for _, _, p in ws.updates)
    assert r3.helpers_created == r3.helpers_removed == [] and rest.service_calls == []
    print(f"deploy 3 (offset_cap {cap} -> {cap + 1}): the {units} offset helpers updated in "
          "place, nothing else touched - OK")

    # deploy 4: baby_room removed -> only its own artifacts prune
    cfg4 = json.loads(json.dumps(cfg3))
    cfg4["rooms"] = [r for r in cfg4["rooms"] if r["id"] != "baby_room"]
    r4 = await check_then_deploy(cfg4, rest, ws, tmp)
    assert len(r4.automations_removed) == 10, r4.automations_removed
    assert all("baby_room" in a for a in r4.automations_removed)
    assert len(r4.helpers_removed) == 13, r4.helpers_removed
    assert all("baby_room" in h for h in r4.helpers_removed)
    assert not any("main_room" in a or "main_bedroom" in a for a in r4.automations_removed)
    assert "foreign_clock" in ws.store["input_datetime"]
    assert "input_button.ac_too_cold_baby_room" in r4.helpers_removed
    assert "ac_vote_baby_room" in r4.automations_removed
    print("deploy 4 (baby_room removed): pruned exactly its 10 automations + 13 helpers "
          "(its vote automation and buttons among them), other rooms and foreign helpers "
          "untouched - OK")

    # a config that doesn't validate (a sensor id repeated in a room): the
    # check names it, and the deploy writes nothing
    bad = json.loads(json.dumps(cfg4))
    bad["rooms"][0]["sensors"].append(dict(bad["rooms"][0]["sensors"][0], name="Second"))
    before = (copy.deepcopy(ws.store), len(rest.set_calls), len(rest.delete_calls),
              len(rest.service_calls), d._manifest_path(tmp).read_text())
    dry = await d.check(bad, rest, ws, tmp)
    real = await d.deploy(bad, rest, ws, tmp)
    assert not dry.ok and "duplicate sensor id" in dry.problems[0], dry.problems
    assert not real.ok and real.helpers_created == real.automations_written == []
    assert (copy.deepcopy(ws.store), len(rest.set_calls), len(rest.delete_calls),
            len(rest.service_calls), d._manifest_path(tmp).read_text()) == before
    print("a config with a duplicate sensor id: the check names it and the deploy aborts "
          "without writing anything - OK")

    # a heat source the templates would read but that isn't in Home Assistant
    heated = json.loads(json.dumps(cfg4))
    heated["rooms"][0]["heat_sources"] = ["switch.no_such_heater"]
    dry = await d.check(heated, rest, ws, tmp)
    real = await d.deploy(heated, rest, ws, tmp)
    assert not dry.ok and dry.missing_entities == ["switch.no_such_heater"], dry.missing_entities
    assert not real.ok and real.helpers_created == real.automations_written == []
    assert (copy.deepcopy(ws.store), len(rest.set_calls), len(rest.delete_calls),
            len(rest.service_calls), d._manifest_path(tmp).read_text()) == before
    print("a heat source that isn't in Home Assistant is a missing entity: the deploy aborts "
          "without writing anything - OK")

    print(f"check (the dry run) before each of the 4 deploys wrote nothing and listed exactly "
          f"what the deploy then did: {[r['helpers_created'] for r in dry_runs]} created, "
          f"{[r['helpers_removed'] for r in dry_runs]} removed, "
          f"{[r['automations_written'] for r in dry_runs]} written - OK")

    print("\nALL DEPLOY RECONCILIATION TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
