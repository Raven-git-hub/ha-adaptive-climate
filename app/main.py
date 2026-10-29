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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.analyser import LearningConfig, MarginConfig, analyse_room, learn_margin
from app.config import ConfigError, blank, migrate, save, validate
from app.store import Store, StoreError

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
    """Lightweight read of the live config JSON, migrated to version 2 with
    v2's new keys defaulted. Not validated, so a broken file still loads
    for the UI to show and fix. A file on disk stays version 1 until the
    next Save writes it back as version 2."""
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            return blank()
        try:
            return migrate(raw)
        except ConfigError as e:
            log.warning("config at %s could not be migrated: %s", CONFIG_PATH, e)
            return raw
    return blank()


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        state.store = Store(DATA_DIR, SCHEMA_DIR / "storage.schema.sql")
    except StoreError as e:
        # A failed migration rolled back and left the database untouched.
        # Stay up but degraded (no runtime) rather than crash: a restart
        # loop would retry the migration, and take a new backup, every time.
        state.error = f"storage unavailable: {e}"
        log.error(state.error)
        yield
        return
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


# A websocket drop is expected (HA restarts, network blips) and the client
# now reconnects on its own. Don't flap the container to unhealthy for a
# transient gap: report degraded only once the socket has been down past
# this grace window without recovering - the genuinely-stuck case.
WS_DOWN_GRACE_SECONDS = 180


@app.get("/healthz")
def healthz() -> JSONResponse:
    ws = getattr(state.runtime, "ws", None)
    connected = bool(ws and ws.connected)
    idle = state.runtime is None

    # How long the socket has been down, if it is. last_disconnected_at is
    # a monotonic (event-loop) timestamp, so compare against loop time.
    ws_down_seconds: float | None = None
    if ws is not None and not connected:
        last_down = getattr(ws, "last_disconnected_at", None)
        if last_down is not None:
            try:
                import asyncio
                ws_down_seconds = asyncio.get_event_loop().time() - last_down
            except Exception:
                ws_down_seconds = None

    # Healthy when idle, connected, or only briefly disconnected (the
    # supervisor is actively reconnecting). Stuck-down past the grace
    # window - or down with no recovery in progress - is degraded.
    reconnecting = (ws_down_seconds is not None
                    and ws_down_seconds < WS_DOWN_GRACE_SECONDS)
    ok = (idle or connected or reconnecting) and state.store is not None

    return JSONResponse(
        status_code=200 if ok else 503,
        content={
            "status": "ok" if ok else "degraded",
            "idle": idle,
            "ha_connected": connected,
            "ha_reconnecting": bool(reconnecting and not connected),
            "ws_down_seconds": (round(ws_down_seconds, 1)
                                if ws_down_seconds is not None else None),
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
    """The almanac in force, with the room's learned block margin (the
    runtime's, else replayed from the store)."""
    if not state.store:
        raise HTTPException(503, "store not ready")
    alm = state.store.current_almanac(room_id)
    if state.runtime is not None and room_id in state.runtime.rooms:  # type: ignore[attr-defined]
        alm["margin"] = round(state.runtime.margin(room_id), 2)        # type: ignore[attr-defined]
    else:
        cfg = _raw_config()
        room = next((r for r in cfg.get("rooms", []) if r.get("id") == room_id), None)
        if room is not None:
            alm["margin"] = round(learn_margin(state.store._conn, room,
                                               MarginConfig.from_config(cfg),
                                               datetime.now().astimezone())[0], 2)
    return alm


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
        await state.runtime.run_analysis("manual")  # type: ignore[attr-defined]
        return {"ok": True, "via": "runtime"}
    cfg = _raw_config()
    lc = LearningConfig.from_config(cfg)
    now = datetime.now().astimezone()
    built = {}
    for room in cfg.get("rooms", []):
        if not room.get("enabled", True):
            continue
        sections = analyse_room(state.store._conn, room, lc, now)
        state.store.publish_almanac(room["id"], sections)
        built[room["id"]] = [s.section for s in sections]
    return {"ok": True, "built": built}


@app.get("/api/config")
def api_get_config() -> dict:
    return _raw_config()


@app.post("/api/config")
def api_save_config(cfg: dict) -> dict:
    """Migrate (a version 1 payload is accepted), validate against the
    version 2 schema, and save the version 2 result."""
    try:
        cfg = migrate(cfg, SCHEMA_DIR / "config.schema.json")
        validate(cfg, SCHEMA_DIR / "config.schema.json")
    except ConfigError as e:
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
    """Dry run: what POST /api/deploy would create, reuse, update, remove
    and write, from the same plan, plus entity existence and any hold that
    is on. No writes to Home Assistant."""
    from app.deploy import check
    rest, ws = _ha_clients()
    try:
        await ws.connect()
        report = await check(_raw_config(), rest, ws, DATA_DIR)
    finally:
        await ws.close()
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
            source = sconf.get("source") or {}
            if source.get("type") == "unit_attribute":
                # virtual: read the referenced unit's current_temperature
                uid = source.get("unit_id")
                unit = next((u for u in room.get("units", []) if u["id"] == uid), None)
                reading = None
                if unit:
                    ua = st(unit["entity_id"]) or {}
                    reading = (ua.get("attributes") or {}).get(
                        source.get("attribute", "current_temperature"))
                sensors_out.append({
                    "id": sconf["id"], "name": sconf.get("name", sconf["id"]),
                    "entity_id": f"(from {unit['entity_id']})" if unit else "(virtual)",
                    "reading": reading, "unit": "\u00b0C",
                })
            else:
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


@app.middleware("http")
async def revalidate_ui(request: Request, call_next):
    """The UI is served without a cache lifetime, so a browser may keep an
    old app.js for days after an upgrade: v1's read the almanac's `state`,
    v2's API sends `status`, and the Almanac tab showed "undefined". With
    no-cache the browser revalidates every load (a 304 when unchanged)."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
