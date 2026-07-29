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

    cfg = _raw_config()
    have_rooms = any(r.get("enabled", True) for r in cfg.get("rooms", []))
    have_creds = bool(os.environ.get("AC_HA_URL") and os.environ.get("AC_HA_TOKEN"))
    if have_rooms and have_creds:
        try:
            from app.ha import HARest, HAWebSocket
            from app.runtime import Runtime
            url = os.environ["AC_HA_URL"]
            token = os.environ["AC_HA_TOKEN"]
            verify = os.environ.get("AC_HA_VERIFY_SSL", "true").lower() != "false"
            rest = HARest(url, token, verify_ssl=verify)
            ws = HAWebSocket(url, token, verify_ssl=verify)
            await ws.connect()
            runtime = Runtime(cfg, state.store, rest, ws, DATA_DIR)
            await runtime.start()
            state.runtime = runtime
            log.info("runtime started (%d room(s) live)", len(runtime.rooms))
        except Exception as e:
            state.error = f"runtime failed to start: {e}"
            log.error(state.error)
    else:
        log.info("idle: %s", "no rooms configured" if not have_rooms
                 else "AC_HA_URL/AC_HA_TOKEN not set")
    yield
    if state.runtime:
        try:
            await state.runtime.stop()
        except Exception:
            pass
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


@app.get("/api/events")
def api_events(limit: int = 200, room_id: str | None = None,
               category: str | None = None, severity: str | None = None) -> dict:
    """The Log view feed."""
    if not state.store:
        raise HTTPException(503, "store not ready")
    return {"events": state.store.recent_events(
        limit=min(limit, 1000), room_id=room_id, category=category, severity=severity)}


@app.get("/api/activity/{room_id}")
def api_activity(room_id: str, day: str | None = None) -> dict:
    """One room, one day: heartbeats, reactions, section runs (for the
    Analysis chart), plus the almanac in force (for the comfort bands)."""
    if not state.store:
        raise HTTPException(503, "store not ready")
    d = day or date.today().isoformat()
    act = state.store.activity(room_id, d)
    act["almanac"] = state.store.current_almanac(room_id).get("sections", {})
    act["tz"] = str(state.runtime.tz) if getattr(state.runtime, "tz", None) else None  # type: ignore[attr-defined]
    return act


@app.post("/api/analysis/run")
async def api_run_analysis() -> dict:
    """Rebuild almanacs now. When the runtime is live this also pushes the
    result to Home Assistant; otherwise it just rebuilds from whatever the
    store holds (and finds nothing until the observer has run)."""
    if not state.store:
        raise HTTPException(503, "store not ready")
    if state.runtime is not None:
        await state.runtime.run_analysis()  # type: ignore[attr-defined]
        return {"ok": True, "via": "runtime"}
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


@app.get("/api/config")
def api_get_config() -> dict:
    return _raw_config()


@app.post("/api/config")
def api_save_config(cfg: dict) -> dict:
    try:
        import jsonschema
        schema = json.loads((SCHEMA_DIR / "config.schema.json").read_text())
        jsonschema.validate(cfg, schema)
    except Exception as e:
        raise HTTPException(400, f"config did not validate: {e}")
    save(cfg, CONFIG_PATH)
    return {"ok": True}


def _ha_clients():
    """Build a REST + WebSocket client pair from the environment, the
    same source doctor.py uses. Raises a clear HTTPException if unset."""
    from app.ha import HARest, HAWebSocket
    url = os.environ.get("AC_HA_URL")
    token = os.environ.get("AC_HA_TOKEN")
    verify_ssl = os.environ.get("AC_HA_VERIFY_SSL", "true").lower() != "false"
    if not url or not token:
        raise HTTPException(503, "AC_HA_URL / AC_HA_TOKEN are not set in the "
                                 "container environment; check .env")
    return HARest(url, token, verify_ssl=verify_ssl), HAWebSocket(url, token, verify_ssl=verify_ssl)


@app.get("/api/entities")
async def api_entities(domain: str) -> dict:
    """Live entities for the Config page's pickers, so a typo can't silently
    resolve to 'unknown'. Degrades gracefully (empty list + a note) when HA
    credentials aren't set or the connection fails, rather than erroring -
    the config editor must still be usable while idle."""
    if domain not in ("climate", "sensor", "binary_sensor"):
        raise HTTPException(400, "domain must be 'climate', 'sensor', or 'binary_sensor'")
    url = os.environ.get("AC_HA_URL")
    token = os.environ.get("AC_HA_TOKEN")
    if not url or not token:
        return {"entities": [], "connected": False,
               "note": "AC_HA_URL/AC_HA_TOKEN not set; enter entity ids manually"}
    from app.ha import HARest
    verify = os.environ.get("AC_HA_VERIFY_SSL", "true").lower() != "false"
    rest = HARest(url, token, verify_ssl=verify)
    try:
        states = await rest.states()
    except Exception as e:
        return {"entities": [], "connected": False, "note": f"could not reach Home Assistant: {e}"}
    finally:
        await rest.close()
    out = [{"entity_id": s["entity_id"],
           "name": (s.get("attributes", {}) or {}).get("friendly_name", s["entity_id"])}
          for s in states if s["entity_id"].startswith(f"{domain}.")]
    out.sort(key=lambda e: e["name"].lower())
    return {"entities": out, "connected": True}


@app.get("/api/deploy/check")
async def api_deploy_check() -> dict:
    """Dry run: entity existence only. No writes to Home Assistant."""
    from app.deploy import check
    rest, _ = _ha_clients()
    try:
        report = await check(_raw_config(), rest)
    finally:
        await rest.close()
    return report.as_dict()


@app.post("/api/deploy")
async def api_deploy() -> dict:
    """Real reconciliation: creates/updates our helpers and automations in
    Home Assistant, prunes our own stale ones. See app/deploy.py and
    docs/DEPLOY.md for exactly what this does and does not touch."""
    from app.deploy import deploy
    rest, ws = _ha_clients()
    try:
        await ws.connect()
        report = await deploy(_raw_config(), rest, ws, DATA_DIR)
    finally:
        await ws.close()
        await rest.close()
    return report.as_dict()


@app.get("/api/now")
async def api_now() -> dict:
    """Live state for the Now view: per room, the guard/hold/scene helpers
    plus each unit's and sensor's current reading from Home Assistant."""
    rest, _ = _ha_clients()
    try:
        states = {s["entity_id"]: s for s in await rest.states()}
    finally:
        await rest.close()

    from app.generator import guard_id, hold_id, scene_select_id

    cfg = _raw_config()
    rooms_out = []
    for room in cfg.get("rooms", []):
        if not room.get("enabled", True):
            continue
        r = room["id"]

        def st(entity_id: str) -> dict | None:
            s = states.get(entity_id)
            if not s:
                return None
            return {"state": s.get("state"), "attributes": s.get("attributes", {})}

        units_out = []
        for u in room.get("units", []):
            s = st(u["entity_id"])
            attrs = (s or {}).get("attributes", {})
            units_out.append({
                "id": u["id"], "name": u.get("name", u["id"]), "entity_id": u["entity_id"],
                "state": (s or {}).get("state"),
                "current_temperature": attrs.get("current_temperature"),
                "setpoint": attrs.get("temperature"),
                "fan_mode": attrs.get("fan_mode"),
            })
        sensors_out = []
        for sconf in room.get("sensors", []):
            s = st(sconf["entity_id"])
            sensors_out.append({
                "id": sconf["id"], "name": sconf.get("name", sconf["id"]),
                "entity_id": sconf["entity_id"],
                "reading": (s or {}).get("state"),
                "unit": ((s or {}).get("attributes", {}) or {}).get("unit_of_measurement"),
            })

        guard = st(guard_id(r))
        hold = st(hold_id(r))
        scene = st(scene_select_id(r))
        rooms_out.append({
            "id": r, "name": room.get("name", r),
            "guard": (guard or {}).get("state"),
            "hold": (hold or {}).get("state"),
            "scene": (scene or {}).get("state"),
            "units": units_out,
            "sensors": sensors_out,
        })
    return {"rooms": rooms_out}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
