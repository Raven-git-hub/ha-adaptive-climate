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
    runtime's, else replayed from the store; + 0.0 so a margin that rounds
    to zero from below is 0.0, not -0.0)."""
    if not state.store:
        raise HTTPException(503, "store not ready")
    alm = state.store.current_almanac(room_id)
    if state.runtime is not None and room_id in state.runtime.rooms:  # type: ignore[attr-defined]
        alm["margin"] = round(state.runtime.margin(room_id), 2) + 0.0  # type: ignore[attr-defined]
    else:
        cfg = _raw_config()
        room = next((r for r in cfg.get("rooms", []) if r.get("id") == room_id), None)
        if room is not None:
            alm["margin"] = round(learn_margin(state.store._conn, room,
                                               MarginConfig.from_config(cfg),
                                               datetime.now().astimezone())[0], 2) + 0.0
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


def _outdoor_failing() -> str | None:
    """The outdoor warning in force, if the latest outdoor streak event
    says the forecast (or the source) is failing."""
    if not state.store:
        return None
    for e in state.store.recent_events(limit=200, category="outdoor"):
        detail = e.get("detail") or {}
        if "failing" in detail:
            return e["message"] if detail["failing"] else None
    return None


def _enabled_room(config: dict, room_id: str) -> dict:
    room = next((r for r in config.get("rooms", [])
                 if r.get("id") == room_id and r.get("enabled", True)), None)
    if room is None:
        raise HTTPException(404, f"no enabled room {room_id!r}")
    return room


@app.get("/api/now")
async def api_now() -> dict:
    """Everything the Now view draws, in one poll (app/nowview.py): per
    room the section, each unit's control helpers and what it is driven to,
    the almanac section, each sensor's reading, staleness and place in its
    band, the conditions entity, the block, leak latches and holds, the
    margin and the latest vote; and the status strip's warnings. With Home
    Assistant out of reach it still answers, with that as the warning."""
    from zoneinfo import ZoneInfo
    from app import nowview
    cfg = _raw_config()
    try:
        validate(cfg)
        config_error = None
    except ConfigError as e:
        config_error = str(e)
    runtime = state.runtime
    try:
        rest, _ = _ha_clients()
    except HTTPException as e:
        return {"connected": False, "rooms": [], "warnings": [
            {"level": "error", "room": None, "kind": "connection", "text": e.detail}]}
    try:
        states = {s["entity_id"]: s for s in await rest.states()}
        tz = getattr(runtime, "tz", None) or ZoneInfo(
            (await rest.config()).get("time_zone") or "UTC")
    except Exception as e:
        return {"connected": False, "rooms": [], "warnings": [
            {"level": "error", "room": None, "kind": "connection",
             "text": f"Home Assistant can't be reached: {e}"}]}
    finally:
        await rest.close()
    now = datetime.now(tz)
    rooms = [r for r in cfg.get("rooms", []) if r.get("enabled", True)]
    almanacs = {r["id"]: state.store.current_almanac(r["id"], as_of=now.date())
                for r in rooms} if state.store else {}
    if runtime is not None:
        margins = {r["id"]: runtime.margin(r["id"]) for r in rooms  # type: ignore[attr-defined]
                   if r["id"] in runtime.rooms}                      # type: ignore[attr-defined]
        last_votes = dict(runtime.last_votes)                        # type: ignore[attr-defined]
    else:
        margins = {r["id"]: learn_margin(state.store._conn, r, MarginConfig.from_config(cfg),
                                         now)[0] for r in rooms} if state.store else {}
        last_votes = {}
    return nowview.build_now(cfg, states, almanacs, now, margins, last_votes,
                             _outdoor_failing(), config_error)


VOTE_WAIT_SECONDS = 4.0


@app.post("/api/rooms/{room_id}/vote")
async def api_vote(room_id: str, body: dict) -> dict:
    """Press the room's too_cold or too_hot button in Home Assistant, as a
    dashboard would. Home Assistant's vote automation decides whether the
    vote is accepted (one correction per episode) and reports it in
    ac_person_vote; with the runtime live, this waits up to
    VOTE_WAIT_SECONDS for that report (the next one for the room in the
    direction pressed) and returns it as `outcome` (None: not heard in time,
    which the UI shows as "sent")."""
    import asyncio
    from app import control
    from app.generator import button_id
    _enabled_room(_raw_config(), room_id)
    direction = (body or {}).get("direction")
    if direction not in control.PERSON_VOTES:
        raise HTTPException(400, f"direction must be one of {', '.join(control.PERSON_VOTES)}")
    entity = button_id(room_id, direction)
    runtime = state.runtime
    votes = getattr(runtime, "last_votes", None)
    seq = ((votes or {}).get(room_id) or {}).get("seq", 0)
    rest, _ = _ha_clients()
    try:
        await rest.call_service("input_button", "press", {"entity_id": entity})
    except Exception as e:
        raise HTTPException(502, f"Home Assistant didn't take the press: {e}")
    finally:
        await rest.close()
    outcome = None
    if votes is not None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + VOTE_WAIT_SECONDS
        while loop.time() < deadline:
            latest = votes.get(room_id)
            # this press's report: newer than the press, and in its direction
            # (a press of the other button meanwhile is someone else's)
            if latest and latest["seq"] > seq and latest.get("direction") == direction:
                outcome = latest
                break
            await asyncio.sleep(0.1)
    return {"pressed": entity, "direction": direction, "outcome": outcome}


@app.post("/api/rooms/{room_id}/units/{unit_id}/leak-fixed")
async def api_leak_fixed(room_id: str, unit_id: str) -> dict:
    """A person confirms a latched leak is fixed: turns on the unit's
    input_boolean.ac_leak_confirmed_<room>_<unit>. Home Assistant's leak
    release automation then releases the latch, if the leak sensor no
    longer reads wet, and treats the unit as at a section change."""
    from app.generator import leak_confirm_id
    room = _enabled_room(_raw_config(), room_id)
    unit = next((u for u in room.get("units", []) if u["id"] == unit_id), None)
    if unit is None:
        raise HTTPException(404, f"no unit {unit_id!r} in {room_id!r}")
    if not unit.get("leak_detection", {}).get("enabled"):
        raise HTTPException(400, f"{unit_id!r} has no leak detection")
    entity = leak_confirm_id(room_id, unit_id)
    rest, _ = _ha_clients()
    try:
        await rest.call_service("input_boolean", "turn_on", {"entity_id": entity})
    except Exception as e:
        raise HTTPException(502, f"Home Assistant didn't take it: {e}")
    finally:
        await rest.close()
    return {"confirmed": entity}


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
