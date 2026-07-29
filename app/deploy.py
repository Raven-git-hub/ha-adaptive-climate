"""
Adaptive Climate - deployment to Home Assistant.

Creates/updates the generated helpers over the WebSocket API and writes
the generated automations over REST. Verifies that every referenced
entity exists and that each helper's friendly name slugifies to the
object id the automations reference (derived from room.id, never
room.name), and REFUSES rather than deploy a broken reference.

STATUS: skeleton.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


def slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")


@dataclass
class DeployReport:
    helpers_created: list[str] = field(default_factory=list)
    helpers_reused: list[str] = field(default_factory=list)
    automations_written: list[str] = field(default_factory=list)
    automations_removed: list[str] = field(default_factory=list)
    missing_entities: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "helpers_created": self.helpers_created,
            "helpers_reused": self.helpers_reused,
            "automations_written": self.automations_written,
            "automations_removed": self.automations_removed,
            "missing_entities": self.missing_entities,
            "problems": self.problems,
        }


async def check(config, ws, rest) -> DeployReport:
    """Dry run: verify entities and helper-name slugging. TODO(Phase 11)."""
    raise NotImplementedError("Phase 11")


async def deploy(config, ws, rest) -> DeployReport:
    """Create helpers, write automations, prune stale ones. TODO(Phase 11)."""
    raise NotImplementedError("Phase 11")
