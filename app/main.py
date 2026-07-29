"""Adaptive Climate - application entry point (FastAPI).

Serves the web UI, the JSON API the UI reads, the config/deploy endpoints,
and /healthz. Comes up idle with no rooms and stays healthy - it does
nothing until a room is added.

STATUS: skeleton. Lifespan + /healthz are wired; data endpoints are stubs.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import blank, save
from app.store import Store

logging.basicConfig(
    level=os.environ.get("AC_LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("adaptive_climate")

DATA_DIR = Path(os.environ.get("AC_DATA_DIR", "/data"))
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"
CONFIG_PATH = DATA_DIR / "config" / "config.json"


@dataclass
class State:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    config: object | None = None
    store: Store | None = None
    runtime: object | None = None
    error: str | None = None


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state.store = Store(DATA_DIR, SCHEMA_DIR / "storage.schema.sql")
    if not CONFIG_PATH.exists():
        save(blank(), CONFIG_PATH)
        log.info("wrote a blank config to %s", CONFIG_PATH)
    # TODO(Phase 9/10): load config, start runtime if rooms + token present.
    log.info("adaptive-climate up (skeleton); idle until rooms are configured")
    yield
    if state.store:
        state.store.close()


app = FastAPI(title="Adaptive Climate", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> JSONResponse:
    # Idle (no rooms) is healthy; once a runtime exists, degraded when the
    # HA websocket is down. Skeleton: idle-only.
    connected = bool(getattr(state.runtime, "ws", None)
                     and state.runtime.ws.connected)  # type: ignore[attr-defined]
    idle = state.runtime is None
    ok = idle or connected
    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "status": "ok" if ok else "degraded",
            "idle": idle,
            "ha_connected": connected,
            "error": state.error,
            "uptime_seconds": int(
                (datetime.now(timezone.utc) - state.started_at).total_seconds()),
        },
    )
