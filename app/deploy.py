"""
Adaptive Climate - deployment to Home Assistant.

Two different reconciliation strategies, because Home Assistant exposes
two different APIs:

  * HELPERS (input_boolean/input_select/input_datetime) use HA's storage
    collection WebSocket commands, which include a reliable `{domain}/list`.
    So helpers are reconciled by LIST-DIFF: list what exists, create what's
    missing, delete our own (`ac_*`) ones no longer desired.

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

check() verifies every referenced entity exists; deploy() refuses to
proceed if any are missing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app import generator as g
from app.generator import correction_started_id
from app.ha import HAError, HARest, HAWebSocket

MANIFEST_FILENAME = "deploy_manifest.json"
HELPER_DOMAINS = ("input_boolean", "input_select", "input_datetime")


def slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")


@dataclass
class DeployReport:
    helpers_created: list[str] = field(default_factory=list)
    helpers_reused: list[str] = field(default_factory=list)
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
            "helpers_created": self.helpers_created,
            "helpers_reused": self.helpers_reused,
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
# check() - dry run: entity existence only. No writes, no HA mutation.
# ---------------------------------------------------------------------

async def check(config: dict, rest: HARest) -> DeployReport:
    report = DeployReport()
    try:
        states = {s["entity_id"]: s for s in await rest.states()}
    except Exception as e:
        report.problems.append(f"could not reach Home Assistant: {e}")
        return report

    for room in config.get("rooms", []):
        if not room.get("enabled", True):
            continue
        for u in room.get("units", []):
            if u["entity_id"] not in states:
                report.missing_entities.append(u["entity_id"])
        for s in room.get("sensors", []):
            if s["entity_id"] not in states:
                report.missing_entities.append(s["entity_id"])

    if report.missing_entities:
        report.problems.append(
            f"{len(report.missing_entities)} configured entities were not found in "
            "Home Assistant; fix the config before deploying")
    return report


# ---------------------------------------------------------------------
# deploy() - real reconciliation.
# ---------------------------------------------------------------------

async def _reconcile_helpers(rendered_helpers: list[dict], ws: HAWebSocket,
                             report: DeployReport) -> None:
    for domain in HELPER_DOMAINS:
        desired = [h for h in rendered_helpers if h["domain"] == domain]
        desired_ids = {h["object_id"] for h in desired}

        try:
            existing = await ws.list_helpers(domain)
        except HAError as e:
            report.problems.append(f"could not list {domain} helpers: {e}")
            existing = []
        existing_ids = {e.get("id") or e.get("object_id") for e in existing}

        for h in desired:
            oid = h["object_id"]
            label = f"{domain}.{oid}"
            if oid in existing_ids:
                report.helpers_reused.append(label)
                continue
            payload = {k: v for k, v in h.items() if k not in ("domain", "object_id")}
            try:
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
                await ws.delete_helper(domain, oid)
                report.helpers_removed.append(label)
            except HAError as err:
                report.problems.append(f"could not remove stale helper {label}: {err}")


async def _reconcile_automations(rendered_automations: list[dict], rest: HARest,
                                 manifest: dict, report: DeployReport) -> None:
    desired = {a["id"]: a for a in rendered_automations}

    for aid, cfg in desired.items():
        try:
            await rest.set_automation(aid, cfg)
            report.automations_written.append(aid)
        except Exception as e:
            report.problems.append(f"could not write automation {aid}: {e}")

    previously_deployed = set(manifest.get("automations", []))
    stale = previously_deployed - set(desired)
    for aid in stale:
        try:
            await rest.delete_automation(aid)
            report.automations_removed.append(aid)
        except Exception as e:
            report.problems.append(f"could not remove stale automation {aid}: {e}")

    manifest["automations"] = sorted(desired)


async def _reset_correction_clocks(config: dict, rest: HARest,
                                   report: DeployReport) -> None:
    """Explicitly set every room's correction_started helper to now().

    HA gives a freshly-created input_datetime a default value ("today,
    00:00:00") rather than leaving it unset. The maintenance template's
    elapsed-time check only treats an unknown/unavailable state as "fresh
    clock"; a real-but-stale midnight value would instead look like an
    already-long-running correction, incorrectly capping a correction that
    should be allowed to start. Run every deploy (idempotent, harmless if
    a correction happens to be mid-flight - it just gets a fresh hour)."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for room in config.get("rooms", []):
        if not room.get("enabled", True):
            continue
        entity_id = correction_started_id(room["id"])
        try:
            await rest.call_service("input_datetime", "set_datetime",
                                    {"entity_id": entity_id, "datetime": now_str})
        except Exception as e:
            report.problems.append(f"could not reset {entity_id}: {e}")


async def deploy(config: dict, rest: HARest, ws: HAWebSocket,
                 data_dir: str | Path) -> DeployReport:
    check_report = await check(config, rest)
    if check_report.missing_entities:
        check_report.problems.append("deploy aborted: fix missing entities first")
        return check_report

    report = DeployReport()
    rendered = g.render_all(config)
    manifest = load_manifest(data_dir)

    await _reconcile_helpers(rendered["helpers"], ws, report)
    await _reset_correction_clocks(config, rest, report)
    await _reconcile_automations(rendered["automations"], rest, manifest, report)

    manifest["helpers"] = sorted(f"{h['domain']}.{h['object_id']}" for h in rendered["helpers"])
    save_manifest(data_dir, manifest)

    report.notes.append(
        "reminder: automatic detection of foreign automations that also control "
        "these thermostats is not implemented yet (see docs/DEPLOY.md) - please "
        "check manually that nothing else is driving the same climate entities")
    return report
