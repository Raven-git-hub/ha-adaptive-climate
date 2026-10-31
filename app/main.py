"""Adaptive Climate - application entry point (FastAPI).

Serves the web UI, the JSON API the UI reads, and /healthz. Comes up idle
with no rooms and stays healthy - it does nothing until a room is added.

The Config page's Apply (POST /api/apply, app/apply.py) validates a config,
previews what it would change in Home Assistant, and saves it, hot-reloads
the running runtime (Runtime.reload) and, when Home Assistant would change,
deploys it once confirmed. A config change no longer needs a restart.
Rooms are read lightly from the raw config here; full validated loading is
Phase 9.
"""

from __future__ import annotations

import asyncio
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

from app import apply as applying
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
    next Apply writes it back as version 2."""
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
    have_rooms = _has_rooms(cfg)
    have_creds = bool(os.environ.get("AC_HA_URL") and os.environ.get("AC_HA_TOKEN"))
    if have_rooms and have_creds:
        try:
            state.runtime = await _start_runtime(cfg)
            log.info("runtime started (%d room(s) live)", len(state.runtime.rooms))  # type: ignore[attr-defined]
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


def _has_rooms(cfg: dict) -> bool:
    return any(r.get("enabled", True) for r in cfg.get("rooms", []))


async def _start_runtime(cfg: dict):
    """A live Runtime on `cfg`, started, with its own Home Assistant
    clients from the environment. On a failure nothing is left running."""
    from app.ha import HARest, HAWebSocket
    from app.runtime import Runtime
    url = os.environ["AC_HA_URL"]
    token = os.environ["AC_HA_TOKEN"]
    verify = os.environ.get("AC_HA_VERIFY_SSL", "true").lower() != "false"
    rest = HARest(url, token, verify_ssl=verify)
    ws = HAWebSocket(url, token, verify_ssl=verify)
    runtime = Runtime(cfg, state.store, rest, ws, DATA_DIR)
    try:
        await ws.connect()
        await runtime.start()
    except Exception:
        try:
            await runtime.stop()
            await ws.close()
            await rest.close()
        except Exception:
            pass
        raise
    return runtime


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


@app.get("/api/schema")
def api_schema() -> dict:
    """The config schema, for the Config page's defaults, ranges and
    choices."""
    return json.loads((SCHEMA_DIR / "config.schema.json").read_text())


@app.post("/api/config")
def api_save_config(cfg: dict) -> dict:
    """Migrate (a version 1 payload is accepted), validate against the
    version 2 schema, and save the version 2 result. Only writes the file:
    the running runtime and Home Assistant see it at the next Apply or
    restart. The Config page uses POST /api/apply, which also reloads and
    deploys."""
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
    if domain not in ENTITY_DOMAINS:
        raise HTTPException(400, "domain must be one of " +
                            ", ".join(f"'{d}'" for d in ENTITY_DOMAINS))
    try:
        rest, _ = _ha_clients()
    except HTTPException:
        return {"entities": [], "connected": False,
               "note": "AC_HA_URL/AC_HA_TOKEN not set; enter entity ids manually"}
    try:
        states = await rest.states()
    except Exception as e:
        return {"entities": [], "connected": False, "note": f"could not reach Home Assistant: {e}"}
    finally:
        await rest.close()
    out = [_entity_entry(s, domain) for s in states if s["entity_id"].startswith(f"{domain}.")]
    out.sort(key=lambda e: e["name"].lower())
    return {"entities": out, "connected": True}


ENTITY_DOMAINS = ("climate", "sensor", "binary_sensor", "weather")


def _entity_entry(s: dict, domain: str) -> dict:
    """A picker entry. A weather entity also says its state, its current
    temperature and whether it offers an hourly forecast (supported_features
    bit 2), which outdoor.source 'forecast' needs - as the doctor's step 7
    lists them."""
    attrs = s.get("attributes", {}) or {}
    entry = {"entity_id": s["entity_id"], "name": attrs.get("friendly_name", s["entity_id"])}
    if domain == "weather":
        from app import outdoor
        temp = attrs.get("temperature")
        entry.update(state=s.get("state"),
                     temperature=temp if isinstance(temp, (int, float)) else None,
                     temperature_unit=attrs.get("temperature_unit"),
                     hourly=outdoor.supports_hourly(s))
    return entry


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
    _log_deploy(report)
    return report.as_dict()


def _log(severity: str, category: str, message: str, detail: dict | None = None) -> None:
    if not state.store:
        return
    now = datetime.now().astimezone()
    try:
        state.store.log_event(now.isoformat(timespec="seconds"),
                              now.astimezone(timezone.utc).isoformat(timespec="seconds"),
                              severity, category, message, detail=detail)
    except Exception as e:
        log.warning("could not log a %s event: %s", category, e)


def _log_deploy(report) -> None:
    _log("info" if report.ok else "warning", "deploy", applying.deploy_message(report),
         {k: v for k, v in report.as_dict().items() if k != "helpers_reused"})


# One Apply at a time, so two never interleave their backup, save and reload.
_apply_lock = asyncio.Lock()


def _refused(problems: list[str], plan: dict | None = None) -> dict:
    return {"status": "refused", "problems": problems, "plan": plan}


@app.post("/api/apply")
async def api_apply(body: dict) -> dict:
    """The Config page's one button (app/apply.py). Body: {config, confirm,
    token}. Validates (a version 1 config is migrated and saved as version
    2), then plans the deploy as the dry run does:

      refused  - the config doesn't validate, or the plan has problems
                 (missing entities, Home Assistant out of reach): nothing
                 written; `problems` says why;
      preview  - Home Assistant would change: the `plan` and its `token`;
                 nothing written. Send it back with confirm: true to apply;
                 a plan that has changed since comes back as a new preview
                 (`stale`);
      applied  - backed up (`backup`), saved, hot-reloaded (`reload`) and,
                 when Home Assistant changes, deployed (`deploy`);
      error    - the reload (or starting an idle runtime) failed: the file
                 and the running config are as they were.
    """
    from app import deploy as deploying
    if not isinstance(body, dict) or not isinstance(body.get("config"), dict):
        raise HTTPException(400, "body must be {config: {...}, confirm?, token?}")
    confirm = body.get("confirm") is True
    async with _apply_lock:
        try:
            cfg = migrate(body["config"], SCHEMA_DIR / "config.schema.json")
            validate(cfg, SCHEMA_DIR / "config.schema.json")
        except ConfigError as e:
            return _refused([f"the config does not validate: {e}"])
        try:
            rest, ws = _ha_clients()
        except HTTPException as e:
            return _refused([f"{e.detail}. Apply needs Home Assistant to work out what would "
                             "change in it, so nothing was saved"])
        try:
            await ws.connect()
            report = await deploying.check(cfg, rest, ws, DATA_DIR)
        except Exception as e:
            report = deploying.DeployReport(dry_run=True)
            report.problems.append(f"could not reach Home Assistant: {e}")
        finally:
            await _close(rest, ws)
        plan = applying.plan_summary(report)
        if not report.ok:
            return _refused(plan["problems"], plan)
        token = applying.plan_token(cfg, report)
        if report.changes and not (confirm and body.get("token") == token):
            return {"status": "preview", "plan": plan, "token": token,
                    "stale": confirm}

        saved = applying.backup(CONFIG_PATH)
        out = {"status": "applied", "backup": saved.name if saved else None,
               "version": cfg["version"], "plan": plan}
        try:
            out["reload"] = await _put_in_force(cfg)
        except Exception as e:
            log.warning("apply: reload failed: %s", e)
            return {"status": "error", "stage": "reload", "backup": out["backup"],
                    "error": f"{type(e).__name__}: {e}" if not isinstance(e, ConfigError)
                    else str(e),
                    "message": "Not applied: the config file and the running system are as "
                               "they were"}
        if not report.changes:
            out.update(deployed=False, message=applying.APPLIED_NO_CHANGES)
            return out
        rest, ws = _ha_clients()
        try:
            await ws.connect()
            result = await deploying.deploy(cfg, rest, ws, DATA_DIR)
        except Exception as e:
            result = deploying.DeployReport()
            result.problems.append(f"the deploy failed: {e}")
        finally:
            await _close(rest, ws)
        _log_deploy(result)
        if state.runtime is not None:
            try:
                await state.runtime.refresh_automations()  # type: ignore[attr-defined]
            except Exception as e:
                log.warning("apply: automation refresh failed: %s", e)
        out.update(deployed=True, deploy=result.as_dict(),
                   message="Applied and deployed to Home Assistant" if result.ok else
                   "Applied; the deploy to Home Assistant finished with problems")
        return out


async def _close(rest, ws) -> None:
    for client in (ws, rest):
        try:
            await client.close()
        except Exception:
            pass


async def _put_in_force(cfg: dict) -> dict:
    """Save `cfg` and put it in force: the live runtime reloads it (and
    writes the file only once the new config is built, so a failure leaves
    both as they were); an idle container starts its runtime when the
    config now has rooms."""
    def commit() -> None:
        save(cfg, CONFIG_PATH)
    if state.runtime is not None:
        return await state.runtime.reload(cfg, commit)  # type: ignore[attr-defined]
    if not _has_rooms(cfg):
        commit()
        _log("info", "config", "Config applied (no rooms enabled, so nothing is running)",
             {"changed": None, "runtime": "idle"})
        return {"runtime": "idle"}
    runtime = await _start_runtime(cfg)
    try:
        commit()
    except Exception:
        await runtime.stop()
        raise
    state.runtime, state.error = runtime, None
    _log("info", "config", f"Config applied: the runtime started with "
         f"{len(runtime.rooms)} room(s)", {"changed": None, "runtime": "started"})
    return {"runtime": "started", "rooms": list(runtime.rooms)}


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
