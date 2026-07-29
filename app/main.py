"""Adaptive Climate - application entry point (FastAPI).

Serves the web UI, the JSON API the UI reads, and /healthz. Comes up idle
with no rooms and stays healthy - it does nothing until a room is added.

STATUS: skeleton runtime (Phases 9/10 not wired), but the almanac path is
live end to end: /api/analysis/run builds almanacs from whatever the store
holds, and /api/almanac/<room> serves them for the Almanac view. Rooms are
read lightly from the raw config here; full validated loading is Phase 9.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.analyser import LearningConfig, analyse_room
from app.config import blank, save
from app.store import Store

logging.basicConfig(
    level=os.environ.get("AC_LOG_LEVEL", "info").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("adaptive_climate")

DATA_DIR = Path(os.environ.get("AC_DATA_DIR", "/data"))
APP_DIR = Path(__file__).resolve().parent
SCHEMA_DIR = APP_DIR.parent / "schema"
CONFIG_PATH = DATA_DIR / "config" / "config.json"
STATIC_DIR = APP_DIR / "static"


@dataclass
class State:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    store: Store | None = None
    runtime: object | None = None
    error: str | None = None


state = State()


def _raw_config() -> dict:
    """Lightweight read of the live config JSON (no validation - Phase 9
    does that). Enough for the UI to enumerate rooms and for the analyser
    to know each room's units and sensors."""
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            return blank()
    return blank()


def _learning_config(cfg: dict) -> LearningConfig:
    learn = cfg.get("learning", {})
    trust = cfg.get("system", {}).get("trust", {})
    base = LearningConfig()
    return LearningConfig(
        analysis_window_days=learn.get("analysis_window_days", base.analysis_window_days),
        bootstrap_min_days=learn.get("bootstrap_min_days", base.bootstrap_min_days),
        validity_delay_days=learn.get("validity_delay_days", base.validity_delay_days),
        reactive_weight=learn.get("reactive_weight", base.reactive_weight),
        high_trust_deviation=trust.get("high_trust_deviation", base.high_trust_deviation),
        low_trust_deviation=trust.get("low_trust_deviation", base.low_trust_deviation),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state.store = Store(DATA_DIR, SCHEMA_DIR / "storage.schema.sql")
    if not CONFIG_PATH.exists():
        save(blank(), CONFIG_PATH)
        log.info("wrote a blank config to %s", CONFIG_PATH)
    log.info("adaptive-climate up; idle until a runtime is wired (Phase 10)")
    yield
    if state.store:
        state.store.close()


app = FastAPI(title="Adaptive Climate", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> JSONResponse:
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


@app.get("/api/rooms")
def api_rooms() -> dict:
    """Room / unit / sensor names for the UI to enumerate."""
    cfg = _raw_config()
    rooms = [{
        "id": r["id"], "name": r.get("name", r["id"]),
        "units": [{"id": u["id"], "name": u.get("name", u["id"])} for u in r.get("units", [])],
        "sensors": [{"id": s["id"], "name": s.get("name", s["id"])} for s in r.get("sensors", [])],
    } for r in cfg.get("rooms", []) if r.get("enabled", True)]
    return {"rooms": rooms}


@app.get("/api/almanac/{room_id}")
def api_almanac(room_id: str) -> dict:
    if not state.store:
        raise HTTPException(503, "store not ready")
    return state.store.current_almanac(room_id)


@app.post("/api/analysis/run")
def api_run_analysis() -> dict:
    """Rebuild almanacs now from whatever the store holds. Real end to end;
    it simply finds nothing to learn until the observer (Phase 10) writes
    heartbeats."""
    if not state.store:
        raise HTTPException(503, "store not ready")
    cfg = _raw_config()
    lc = _learning_config(cfg)
    today = date.today()
    built = {}
    for room in cfg.get("rooms", []):
        if not room.get("enabled", True):
            continue
        sections = analyse_room(state.store._conn, room, lc, today)
        state.store.publish_almanac(room["id"], sections)
        built[room["id"]] = [s.section for s in sections]
    return {"ok": True, "built": built}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
