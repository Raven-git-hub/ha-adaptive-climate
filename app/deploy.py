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
    write ourselves. Each desired automation is read back by its own id
    and written only if HA's copy differs. See docs/DEPLOY.md.

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
An automation Home Assistant already holds exactly as rendered (and our
manifest records) is unchanged and isn't written again, so the plan's
`changes` count says whether a deploy would change Home Assistant at all;
the Config page's Apply reads it (docs/V2_DESIGN.md, Usability fixes).
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
HELPER_DOMAINS = ("input_boolean", "input_select", "input_number", "input_datetime",
                  "input_button")
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
    automations_unchanged: list[str] = field(default_factory=list)
    automations_removed: list[str] = field(default_factory=list)
    # why each written automation is written (ha_copy_differs), for /api/deploy/check
    automations_why: dict[str, str] = field(default_factory=dict)
    missing_entities: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and not self.missing_entities

    @property
    def changes(self) -> int:
        """How many things in Home Assistant this plan changes: helpers
        created, updated or removed, automations written or removed. 0 means
        a deploy would leave Home Assistant exactly as it is."""
        return sum(len(x) for x in (self.helpers_created, self.helpers_updated,
                                    self.helpers_removed, self.automations_written,
                                    self.automations_removed))

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "dry_run": self.dry_run,
            "helpers_created": self.helpers_created,
            "helpers_reused": self.helpers_reused,
            "helpers_updated": self.helpers_updated,
            "helpers_removed": self.helpers_removed,
            "automations_written": self.automations_written,
            "automations_unchanged": self.automations_unchanged,
            "automations_removed": self.automations_removed,
            "automations_why": self.automations_why,
            "changes": self.changes,
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
        for e in room.get("heat_sources") or []:
            # the generated templates read them live; a typo would never block
            if e not in states:
                report.missing_entities.append(e)

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


# ---------------------------------------------------------------------
# Comparing an automation with Home Assistant's copy
# ---------------------------------------------------------------------
#
# Home Assistant's config editor keeps what we post in automations.yaml and
# hands it back from there, but the copy can come back shaped differently
# without meaning anything different: the id added and keys reordered;
# defaults (description "", mode single, max 10, max_exceeded warning, empty
# variables); the 2024.8 and 2024.10 renames (trigger/condition/action ->
# triggers/conditions/actions, platform -> trigger, service -> action); one
# trigger, condition or action as a bare item rather than a list of one;
# a number as a string; a template's whitespace (line breaks, runs of
# spaces). Both sides are brought to one shape before comparing, so an
# identical deploy compares equal while any other change to a template,
# trigger, condition or action still counts.

_PLURAL = {"trigger": "triggers", "condition": "conditions", "action": "actions"}
_DEFAULTS = {"description": "", "mode": "single", "max_exceeded": "warning",
             "variables": {}, "trigger_variables": {}}
_QUEUE_MAX = 10              # HA's default `max` for a queued or parallel automation
_TEMPLATE = re.compile(r"\{\{|\{%|\{#")
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def _as_list(value: Any) -> list:
    return [] if value is None else value if isinstance(value, list) else [value]


def _trigger(t: Any) -> Any:
    if not isinstance(t, dict):
        return t
    t = dict(t)
    if "platform" in t and "trigger" not in t:
        t["trigger"] = t.pop("platform")
    return t


def _condition(c: Any) -> Any:
    if not isinstance(c, dict):
        return c                                     # a template shorthand
    c = dict(c)
    if "conditions" in c:                            # and / or / not
        c["conditions"] = [_condition(x) for x in _as_list(c["conditions"])]
    return c


def _actions(value: Any) -> list:
    return [_action(s) for s in _as_list(value)]


def _action(s: Any) -> Any:
    if not isinstance(s, dict):
        return s
    s = dict(s)
    if "service" in s and "action" not in s:
        s["action"] = s.pop("service")
    if "choose" in s:
        s["choose"] = [
            {**o, "conditions": [_condition(c) for c in _as_list(o.get("conditions"))],
             "sequence": _actions(o.get("sequence"))} if isinstance(o, dict) else o
            for o in _as_list(s["choose"])]
    for key in ("default", "then", "else", "sequence", "parallel"):
        if key in s:
            s[key] = _actions(s[key])
    for key in ("if", "conditions"):
        if key in s:
            s[key] = [_condition(c) for c in _as_list(s[key])]
    if isinstance(s.get("repeat"), dict):
        s["repeat"] = _action(s["repeat"])
    return s


def _scalars(value: Any) -> Any:
    """Numbers as floats (a numeric string too), a template's whitespace
    collapsed to single spaces; everything else as it is."""
    if isinstance(value, dict):
        return {k: _scalars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scalars(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        if _TEMPLATE.search(value):
            return " ".join(value.split())
        if _NUMBER.match(value.strip()):
            return float(value)
    return value


def normalised(automation: dict) -> dict:
    """An automation in the one shape both sides are compared in (see
    above): no id, plural top-level keys, `trigger` and `action` for the
    platform and the service, lists throughout, defaults left out, numbers
    as floats and templates' whitespace collapsed."""
    a = json.loads(json.dumps(automation))
    top = {_PLURAL.get(k, k): v for k, v in a.items() if k != "id"}
    top["triggers"] = [_trigger(t) for t in _as_list(top.get("triggers"))]
    top["conditions"] = [_condition(c) for c in _as_list(top.get("conditions"))]
    top["actions"] = _actions(top.get("actions"))
    top = _scalars(top)
    for key, default in _DEFAULTS.items():
        if key in top and top[key] in (default, None):
            del top[key]
    if top.get("mode") in ("queued", "parallel") and top.get("max") == float(_QUEUE_MAX):
        del top["max"]
    return top


def first_difference(ours: Any, theirs: Any, path: str = "") -> str | None:
    """Where two configs first differ, as a path and both values; None if
    they are equal."""
    def show(v: Any) -> str:
        text = json.dumps(v, ensure_ascii=False)
        return text if len(text) <= 160 else text[:157] + "..."
    if isinstance(ours, dict) and isinstance(theirs, dict):
        for key in list(ours) + [k for k in theirs if k not in ours]:
            if key not in theirs:
                return f"{path}.{key}: ours {show(ours[key])}, Home Assistant's missing"
            if key not in ours:
                return f"{path}.{key}: ours missing, Home Assistant's {show(theirs[key])}"
            found = first_difference(ours[key], theirs[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(ours, list) and isinstance(theirs, list):
        for i, (a, b) in enumerate(zip(ours, theirs)):
            found = first_difference(a, b, f"{path}[{i}]")
            if found:
                return found
        if len(ours) != len(theirs):
            return f"{path}: ours has {len(ours)} items, Home Assistant's {len(theirs)}"
        return None
    if ours == theirs and type(ours) is type(theirs):
        return None
    where = ""
    if isinstance(ours, str) and isinstance(theirs, str) and max(len(ours), len(theirs)) > 60:
        at = next((i for i, (a, b) in enumerate(zip(ours, theirs)) if a != b),
                  min(len(ours), len(theirs)))
        lo = max(0, at - 20)
        where = f" (around character {at}: ours {show(ours[lo:at + 40])}, " \
                f"Home Assistant's {show(theirs[lo:at + 40])})"
    return f"{path or '.'}: ours {show(ours)}, Home Assistant's {show(theirs)}{where}"


async def ha_copy_differs(aid: str, desired: dict, rest: HARest,
                          manifest: dict) -> tuple[str | None, dict | None]:
    """Why writing this automation would change Home Assistant, or None if
    HA already holds exactly it, written by us; with HA's copy, if read.
    Anything we can't confirm - an id our manifest doesn't record, no copy
    in HA, a copy that differs, a read that fails or a client that can't
    read - is a reason, so the plan errs toward writing."""
    if aid not in set(manifest.get("automations", [])):
        return "not in our deploy manifest", None
    read = getattr(rest, "get_automation", None)
    if read is None:
        return "this client can't read automations back", None
    try:
        current = await read(aid)
    except Exception as e:
        return f"reading Home Assistant's copy failed: {type(e).__name__}: {e}", None
    if current is None:
        return "Home Assistant has no copy", None
    diff = first_difference(normalised(desired), normalised(current))
    return (None if diff is None else f"Home Assistant's copy differs at {diff}"), current


async def _reconcile_automations(rendered_automations: list[dict], rest: HARest,
                                 manifest: dict, report: DeployReport,
                                 apply: bool = True) -> None:
    """Write every desired automation that Home Assistant doesn't already
    hold as rendered (new, changed, or not in our manifest), and prune the
    ones our manifest says we wrote before. apply=False plans only."""
    desired = {a["id"]: a for a in rendered_automations}

    for aid, cfg in desired.items():
        why, _ = await ha_copy_differs(aid, cfg, rest, manifest)
        if why is None:
            report.automations_unchanged.append(aid)
            continue
        try:
            if apply:
                await rest.set_automation(aid, cfg)
            report.automations_written.append(aid)
            report.automations_why[aid] = why
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
    today 00:00, a boolean off, a button unknown (never pressed). That is
    right for the base (0, unset), the grace period (in the past), the
    unblock and the forced marker (off: no forced state), and the room's
    two vote buttons. The rest are set here,
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
