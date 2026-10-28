"""
Adaptive Climate - deployment to Home Assistant.

Two different reconciliation strategies, because Home Assistant exposes
two different APIs:

  * HELPERS (input_boolean/input_select/input_number/input_datetime) use
    HA's storage collection WebSocket commands, which include a reliable
    `{domain}/list`. So helpers are reconciled by LIST-DIFF: list what
    exists, create what's missing, update ours whose settings changed (a
    range, the options), delete our own (`ac_*`) ones no longer desired.
    Only the helpers created in this deploy get starting values.

  * AUTOMATIONS use HA's config-editor REST API
    (GET/POST/DELETE /api/config/automation/config/<id>), which has no
    equivalent reliable "list everything" call. So automations are
    reconciled by LEDGER: we keep our own local manifest of what we last
    deployed (data/config/deploy_manifest.json) and diff desired-now
    against deployed-last-time. This never depends on being able to
    enumerate HA's automations, and it never touches an id we didn't
    write ourselves. See docs/DEPLOY.md.

Either way, deploy NEVER deletes anything outside our own `ac_*`
namespace - see docs/DESIGN.md 13 and the ownership discussion in
docs/DEPLOY.md. Detecting foreign automations that also drive our
thermostats (so you can review and resolve the conflict yourself) is not
yet implemented - it needs a live-verified "list automations" path we
don't have from this sandbox; today's report always ends with a manual
reminder instead of a possibly-wrong automatic scan. See docs/DEPLOY.md.

check() is the dry run: it verifies every referenced entity exists and,
given the WebSocket client and the data dir, reports exactly what deploy()
would create, reuse, update, remove and write, from the same plan, without
writing anything. deploy() refuses to proceed if any entity is missing.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import generator as g
from app.config import ConfigError, validate
from app.ha import HAError, HARest, HAWebSocket

MANIFEST_FILENAME = "deploy_manifest.json"
HELPER_DOMAINS = ("input_boolean", "input_select", "input_number", "input_datetime")
# A newly created unit is hands off until its room's next section change,
# so deploying sends no command to any unit.
NEW_UNIT_STATE = "manual"


def slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")


@dataclass
class DeployReport:
    dry_run: bool = False            # check(): what deploy() would do, nothing written
    helpers_created: list[str] = field(default_factory=list)
    helpers_reused: list[str] = field(default_factory=list)
    helpers_updated: list[str] = field(default_factory=list)
    helpers_removed: list[str] = field(default_factory=list)
    automations_written: list[str] = field(default_factory=list)
    automations_removed: list[str] = field(default_factory=list)
    missing_entities: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and not self.missing_entities

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "helpers_created": self.helpers_created,
            "helpers_reused": self.helpers_reused,
            "helpers_updated": self.helpers_updated,
            "helpers_removed": self.helpers_removed,
            "automations_written": self.automations_written,
            "automations_removed": self.automations_removed,
            "missing_entities": self.missing_entities,
            "problems": self.problems,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------
# Manifest (our own record of what we last deployed)
# ---------------------------------------------------------------------

def _manifest_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "config" / MANIFEST_FILENAME


def load_manifest(data_dir: str | Path) -> dict:
    p = _manifest_path(data_dir)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    return {"helpers": [], "automations": []}


def save_manifest(data_dir: str | Path, manifest: dict) -> None:
    p = _manifest_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, indent=2))


# ---------------------------------------------------------------------
# check() - the dry run. No writes, no HA mutation.
# ---------------------------------------------------------------------

async def check(config: dict, rest: HARest, ws: HAWebSocket | None = None,
                data_dir: str | Path | None = None) -> DeployReport:
    """Entity existence and the holds that are on; with `ws` and `data_dir`,
    also the plan deploy() would carry out, built by the same code with
    writing switched off."""
    report = DeployReport(dry_run=True)
    try:
        validate(config)
    except ConfigError as e:
        report.problems.append(f"the config does not validate: {e}; fix it before deploying")
        return report
    try:
        states = {s["entity_id"]: s for s in await rest.states()}
    except Exception as e:
        report.problems.append(f"could not reach Home Assistant: {e}")
        return report

    for room in config.get("rooms", []):
        if not room.get("enabled", True):
            continue
        hold = states.get(g.hold_id(room["id"])) or {}
        if hold.get("state") == "on":
            # v1 raised the hold on every reaction and its scenes cleared it;
            # v2 never touches it, so one left on pauses the room for good
            report.notes.append(
                f"{g.hold_id(room['id'])} is on: the room is paused (no commands) until a "
                "person turns it off; nothing clears it automatically any more")
        for u in room.get("units", []):
            if u["entity_id"] not in states:
                report.missing_entities.append(u["entity_id"])
        for s in room.get("sensors", []):
            if s.get("source"):
                continue   # virtual sensor - no entity_id to verify
            if s["entity_id"] not in states:
                report.missing_entities.append(s["entity_id"])

    if report.missing_entities:
        report.problems.append(
            f"{len(report.missing_entities)} configured entities were not found in "
            "Home Assistant; fix the config before deploying")
        return report
    if ws is not None and data_dir is not None:
        rendered = g.render_all(config)
        await _reconcile_helpers(rendered["helpers"], ws, report, apply=False)
        await _reconcile_automations(rendered["automations"], rest,
                                     load_manifest(data_dir), report, apply=False)
        report.notes.append("dry run: the lists are what Deploy would do now; nothing "
                            "was written")
    return report


# ---------------------------------------------------------------------
# deploy() - real reconciliation.
# ---------------------------------------------------------------------

async def _reconcile_helpers(rendered_helpers: list[dict], ws: HAWebSocket,
                             report: DeployReport, apply: bool = True) -> None:
    """List-diff our helpers. apply=False plans only (the dry run): the
    report says what would happen, and nothing is written."""
    for domain in HELPER_DOMAINS:
        desired = [h for h in rendered_helpers if h["domain"] == domain]
        desired_ids = {h["object_id"] for h in desired}

        try:
            existing = await ws.list_helpers(domain)
        except HAError as e:
            report.problems.append(f"could not list {domain} helpers: {e}")
            existing = []
        existing_by_id = {e.get("id") or e.get("object_id"): e for e in existing}

        for h in desired:
            oid = h["object_id"]
            label = f"{domain}.{oid}"
            payload = {k: v for k, v in h.items() if k not in ("domain", "object_id")}
            if oid in existing_by_id:
                current = existing_by_id[oid]
                if all(current.get(k) == v for k, v in payload.items()):
                    report.helpers_reused.append(label)
                    continue
                try:
                    if apply:
                        await ws.update_helper(domain, oid, payload)
                    report.helpers_updated.append(label)
                except HAError as e:
                    report.problems.append(f"could not update {label}: {e}")
                continue
            try:
                if apply:
                    await ws.create_helper(domain, payload)
                report.helpers_created.append(label)
            except HAError as e:
                report.problems.append(f"could not create {label}: {e}")

        # Prune only our own (ac_*) helpers that are no longer desired.
        for e in existing:
            oid = e.get("id") or e.get("object_id")
            if not oid or not oid.startswith("ac_") or oid in desired_ids:
                continue
            label = f"{domain}.{oid}"
            try:
                if apply:
                    await ws.delete_helper(domain, oid)
                report.helpers_removed.append(label)
            except HAError as err:
                report.problems.append(f"could not remove stale helper {label}: {err}")


async def _reconcile_automations(rendered_automations: list[dict], rest: HARest,
                                 manifest: dict, report: DeployReport,
                                 apply: bool = True) -> None:
    """Write every desired automation (always upserted) and prune the ones
    our manifest says we wrote before. apply=False plans only."""
    desired = {a["id"]: a for a in rendered_automations}

    for aid, cfg in desired.items():
        try:
            if apply:
                await rest.set_automation(aid, cfg)
            report.automations_written.append(aid)
        except Exception as e:
            report.problems.append(f"could not write automation {aid}: {e}")

    previously_deployed = set(manifest.get("automations", []))
    stale = previously_deployed - set(desired)
    for aid in sorted(stale):
        try:
            if apply:
                await rest.delete_automation(aid)
            report.automations_removed.append(aid)
        except Exception as e:
            report.problems.append(f"could not remove stale automation {aid}: {e}")

    manifest["automations"] = sorted(desired)


async def _initialise_new_helpers(config: dict, rest: HARest,
                                  report: DeployReport) -> None:
    """Give the unit helpers created in this deploy their starting values.

    No helper carries an `initial` (HA would reset it to that at every
    restart), so a new one starts at HA's default: an input_number at its
    minimum, an input_select at its first option, an input_datetime at
    today 00:00, a boolean off. That is right for the base (0, unset), the
    grace period (in the past) and the unblock (off). The rest are set here,
    and only for helpers that didn't exist before, so a running boost or
    rest keeps its clock:

      ac_since_*   now, as an epoch timestamp (not the container's clock
                   read as HA local time)
      ac_offset_*  0 (the minimum is -offset_cap)
      ac_state_*   manual: hands off until the room's next section change,
                   so deploying sends no command to any unit
    """
    created = set(report.helpers_created)
    now = int(time.time())
    for room in config.get("rooms", []):
        if not room.get("enabled", True):
            continue
        for unit in room.get("units", []):
            r, u = room["id"], unit["id"]
            for entity_id, domain, service, data in (
                    (g.since_id(r, u), "input_datetime", "set_datetime", {"timestamp": now}),
                    (g.offset_id(r, u), "input_number", "set_value", {"value": 0}),
                    (g.state_id(r, u), "input_select", "select_option",
                     {"option": NEW_UNIT_STATE})):
                if entity_id not in created:
                    continue
                try:
                    await rest.call_service(domain, service, {"entity_id": entity_id, **data})
                except Exception as e:
                    report.problems.append(f"could not set the starting value of {entity_id}: {e}")


async def deploy(config: dict, rest: HARest, ws: HAWebSocket,
                 data_dir: str | Path) -> DeployReport:
    check_report = await check(config, rest)
    if check_report.problems:
        check_report.dry_run = False
        check_report.problems.append("deploy aborted: nothing was written")
        return check_report

    report = DeployReport(notes=list(check_report.notes))
    rendered = g.render_all(config)
    manifest = load_manifest(data_dir)

    await _reconcile_helpers(rendered["helpers"], ws, report)
    await _initialise_new_helpers(config, rest, report)
    await _reconcile_automations(rendered["automations"], rest, manifest, report)

    manifest["helpers"] = sorted(f"{h['domain']}.{h['object_id']}" for h in rendered["helpers"])
    save_manifest(data_dir, manifest)

    report.notes.append(
        "reminder: automatic detection of foreign automations that also control "
        "these thermostats is not implemented yet (see docs/DEPLOY.md) - please "
        "check manually that nothing else is driving the same climate entities")
    return report
