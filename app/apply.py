"""
Adaptive Climate - the Config page's Apply (docs/V2_DESIGN.md, Usability
fixes).

Apply replaces the old Save and Deploy pair. POST /api/apply (app/main.py)
migrates the posted config to version 2 and validates it, then asks the
deploy dry run (app.deploy.check) what would change in Home Assistant:

  * problems (the config doesn't validate, a configured entity is missing,
    Home Assistant can't be reached or listed): refused, nothing written;
  * nothing would change in Home Assistant: back up, save, hot-reload -
    "Applied, no Home Assistant changes";
  * something would: a preview of the plan and a token for it. Confirming
    sends the token back; the plan is worked out again, and only if it is
    still the one previewed (the token matches) does Apply back up, save,
    hot-reload and deploy. A plan that changed meanwhile comes back as a
    new preview. Cancelling sends nothing, so it changes nothing.

This module holds the parts that don't need the app: the backups, the
plan's summary for the page, and its token.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

from app.deploy import DeployReport

BACKUPS_KEPT = 20
BACKUP_PREFIX = "config-"
# config-20261031-142530.123456.json: fixed width, so names sort by time
BACKUP_FORMAT = "%Y%m%d-%H%M%S.%f"
# The plan's lists: what a deploy would change in Home Assistant.
CHANGE_LISTS = ("helpers_created", "helpers_updated", "helpers_removed",
                "automations_written", "automations_removed")
APPLIED_NO_CHANGES = "Applied, no Home Assistant changes"


def backups_dir(config_path: Path) -> Path:
    return Path(config_path).parent / "backups"


def backup(config_path: Path, keep: int = BACKUPS_KEPT, now: datetime | None = None) -> Path | None:
    """Copy the config file as it is now to backups/config-<timestamp>.json
    beside it, then keep only the newest `keep` backups. None when there is
    no config file yet."""
    config_path = Path(config_path)
    if not config_path.exists():
        return None
    folder = backups_dir(config_path)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime(BACKUP_FORMAT)
    target = folder / f"{BACKUP_PREFIX}{stamp}.json"
    shutil.copy2(config_path, target)
    prune(folder, keep)
    return target


def prune(folder: Path, keep: int = BACKUPS_KEPT) -> list[Path]:
    """Delete all but the newest `keep` config backups in `folder` (only
    files named like a backup); returns what was deleted."""
    found = sorted(p for p in Path(folder).glob(f"{BACKUP_PREFIX}*.json") if p.is_file())
    gone = found[:-keep] if keep > 0 else found
    for p in gone:
        p.unlink()
    return gone


def plan_summary(report: DeployReport) -> dict:
    """The deploy plan as the Apply preview shows it: what would be
    created, updated, removed and written, how much stays as it is, and any
    problems or notes (a hold left on, say)."""
    out = {k: list(getattr(report, k)) for k in CHANGE_LISTS}
    out.update(
        helpers_reused=len(report.helpers_reused),
        automations_unchanged=len(report.automations_unchanged),
        changes=report.changes,
        missing_entities=list(report.missing_entities),
        problems=list(report.problems),
        notes=[n for n in report.notes if not n.startswith("dry run:")])
    return out


def plan_token(config: dict, report: DeployReport) -> str:
    """Identifies one config and the plan it was previewed with, so a
    Confirm deploys exactly what the preview showed."""
    payload = json.dumps({"config": config,
                          "plan": {k: sorted(getattr(report, k)) for k in CHANGE_LISTS}},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def deploy_message(report: DeployReport) -> str:
    """One line for the Log: what a deploy changed."""
    parts = [f"{len(getattr(report, k))} {k.replace('_', ' ')}" for k in CHANGE_LISTS
             if getattr(report, k)]
    head = "Deployed" if report.ok else "Deploy finished with problems"
    return f"{head}: {', '.join(parts) or 'nothing changed'}" + \
        (f"; {len(report.problems)} problem(s): {'; '.join(report.problems)}"
         if report.problems else "")
