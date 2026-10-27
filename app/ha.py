"""
Adaptive Climate - Home Assistant client (REST + WebSocket).

Plumbing only, no business logic. REST for state pushes (the almanac
sensor) and service calls (climate.set_temperature / set_hvac_mode /
set_fan_mode); WebSocket for the auth handshake, helper creation, and
the state-change stream that will drive reactive detection (Phase 10).

Two hard-won rules from the Light cutover:
  * Start the WebSocket read loop BEFORE any subscription, or the stream
    is dead while the log says "connected".
  * Thread context.parent_id through so automation-caused changes are not
    mistaken for user interventions.

Deliberately dependency-light: httpx and websockets, both already in
requirements.txt. No home-assistant client library - a large surface for
what we need. Public interface is a small set of async methods; result
shapes match HA's own (dicts of the JSON it returns).
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx
import websockets

log = logging.getLogger("adaptive_climate.ha")


class HAError(Exception): ...
class HAAuthError(HAError): ...
class HAConnectionError(HAError): ...


# ---------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------

class HARest:
    def __init__(self, url: str, token: str, verify_ssl: bool = True,
                 timeout: float = 10.0) -> None:
        self.url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"}
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _c(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=self._headers, verify=self._verify_ssl,
                timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str) -> Any:
        r = await (await self._c()).get(f"{self.url}{path}")
        if r.status_code == 401:
            raise HAAuthError("token rejected by Home Assistant")
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, payload: dict) -> Any:
        r = await (await self._c()).post(f"{self.url}{path}", json=payload)
        if r.status_code == 401:
            raise HAAuthError("token rejected by Home Assistant")
        r.raise_for_status()
        return r.json() if r.content else {}

    async def _delete(self, path: str) -> None:
        r = await (await self._c()).delete(f"{self.url}{path}")
        if r.status_code == 401:
            raise HAAuthError("token rejected by Home Assistant")
        if r.status_code == 404:
            return
        r.raise_for_status()

    # --- public
    async def ping(self) -> dict:                       return await self._get("/api/")
    async def config(self) -> dict:                     return await self._get("/api/config")
    async def states(self) -> list[dict]:               return await self._get("/api/states")
    async def state(self, entity_id: str) -> dict:      return await self._get(f"/api/states/{entity_id}")
    async def set_state(self, entity_id: str, state: str,
                        attributes: dict | None = None) -> dict:
        return await self._post(f"/api/states/{entity_id}",
                                {"state": state, "attributes": attributes or {}})
    async def call_service(self, domain: str, service: str,
                           data: dict | None = None) -> Any:
        return await self._post(f"/api/services/{domain}/{service}", data or {})

    async def render_template(self, template: str) -> str:
        """POST /api/template: Home Assistant renders a template with its own
        Jinja and returns the text (used by the doctor's v2 preflight)."""
        r = await (await self._c()).post(f"{self.url}/api/template", json={"template": template})
        if r.status_code == 401:
            raise HAAuthError("token rejected by Home Assistant")
        r.raise_for_status()
        return r.text

    # --- automation config editor API (used by deploy.py) -----------
    # These are the same endpoints Home Assistant's own frontend
    # automation editor uses (GET/POST/DELETE /api/config/automation/
    # config/<id>). There is no reliable "list all automations" call in
    # this family, which is why deploy.py tracks what it has deployed in
    # its own local manifest rather than depending on enumeration - see
    # docs/DEPLOY.md.
    async def get_automation(self, automation_id: str) -> dict | None:
        try:
            return await self._get(f"/api/config/automation/config/{automation_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def set_automation(self, automation_id: str, config: dict) -> Any:
        body = {k: v for k, v in config.items() if k != "id"}
        return await self._post(f"/api/config/automation/config/{automation_id}", body)

    async def delete_automation(self, automation_id: str) -> None:
        await self._delete(f"/api/config/automation/config/{automation_id}")


# ---------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------

def _ws_url(rest_url: str) -> str:
    p = urlparse(rest_url)
    scheme = "wss" if p.scheme == "https" else "ws"
    return f"{scheme}://{p.netloc}/api/websocket"


@dataclass
class _Pending:
    fut: asyncio.Future
    stream: Callable[[dict], Awaitable[None] | None] | None = None


class HAWebSocket:
    """One WebSocket, one message id sequence. Concurrent request()s are
    demultiplexed by id. `subscribe_states(cb)` registers a coroutine
    fired on every `state_changed` event, with an untracked-message stream
    handler for that subscription's id."""

    def __init__(self, url: str, token: str, verify_ssl: bool = True) -> None:
        self._url = _ws_url(url)
        self._token = token
        self._ssl_ctx: ssl.SSLContext | None = (None if verify_ssl or
                                                self._url.startswith("ws://")
                                                else ssl._create_unverified_context())
        self._ws: Any = None
        self._reader: asyncio.Task | None = None
        self._supervisor: asyncio.Task | None = None
        self._pending: dict[int, _Pending] = {}
        self._id = 0
        self.connected = False
        self.ha_version: str | None = None
        # Reconnect bookkeeping. A drop is normal (HA restarts, network
        # blips); the reader used to just set connected=False and stop,
        # leaving the socket dead forever with no restart - which silently
        # killed reactive detection and pinned /healthz to 503. The
        # supervisor now re-establishes the socket with backoff and
        # replays subscriptions. See docs/DESIGN.md 12.
        self._closing = False
        self._on_reconnect: Callable[[], Awaitable[None]] | None = None
        self._on_status: Callable[[bool, str], None] | None = None
        self._reconnect_attempts = 0
        # Exposed for /healthz to tell "reconnecting" from "stuck/never up".
        self.last_connected_at: float | None = None
        self.last_disconnected_at: float | None = None

    def on_reconnect(self, cb: Callable[[], Awaitable[None]]) -> None:
        """Register a coroutine replayed after every (re)connect, once the
        reader is live. The runtime uses this to re-run subscribe_states so
        the state_changed stream (reactive detection) survives a drop."""
        self._on_reconnect = cb

    def on_status(self, cb: Callable[[bool, str], None]) -> None:
        """Register a callback fired on every up/down transition as
        cb(connected: bool, reason: str). The runtime logs these as
        `connection`-category events so drops are visible in the UI."""
        self._on_status = cb

    # ---- lifecycle -------------------------------------------------

    async def _open_once(self) -> None:
        """One connect + auth attempt. Starts the reader and marks
        connected; raises on any failure so the supervisor can back off."""
        try:
            self._ws = await websockets.connect(self._url, ssl=self._ssl_ctx,
                                                open_timeout=10)
        except Exception as e:
            raise HAConnectionError(f"websocket connect failed: {e}") from e

        hello = json.loads(await self._ws.recv())
        if hello.get("type") != "auth_required":
            raise HAConnectionError(f"unexpected greeting: {hello}")
        self.ha_version = hello.get("ha_version")

        await self._ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        auth_result = json.loads(await self._ws.recv())
        if auth_result.get("type") != "auth_ok":
            raise HAAuthError(auth_result.get("message", "auth failed"))

        # Rule from the Light cutover: reader BEFORE any subscription.
        self._reader = asyncio.create_task(self._read_loop(), name="ha-ws-reader")
        self.connected = True
        self.last_connected_at = asyncio.get_event_loop().time()

    async def connect(self) -> None:
        """Establish the connection and keep it up. The first attempt is
        awaited so callers still fail fast on a bad URL/token (auth errors
        are never retried); after that a supervisor task reconnects on drop.

        Idempotent: a call while already supervised is a no-op, so the
        one-shot deploy path and the long-lived runtime path can share one
        client without fighting over the socket."""
        if self._supervisor and not self._supervisor.done():
            return
        self._closing = False
        await self._open_once()          # fail fast on the first attempt
        if self._on_status:
            self._on_status(True, "connected")
        self._supervisor = asyncio.create_task(self._supervise(), name="ha-ws-supervisor")

    async def _supervise(self) -> None:
        """Own the socket for its lifetime: wait on the reader, and when it
        exits (a drop) reconnect with capped exponential backoff, replaying
        the resubscribe hook each time. Runs until close(). An HAAuthError
        is fatal - a rejected token cannot be fixed by retrying - so it
        stops and stays down for /healthz to surface."""
        BASE, CAP = 1.0, 60.0
        # Replay subscriptions for the socket connect() already opened.
        await self._after_connect()
        while not self._closing:
            if self._reader:
                try:
                    await self._reader          # returns when the socket drops
                except (asyncio.CancelledError, Exception):
                    pass
            if self._closing:
                break
            self.connected = False
            self.last_disconnected_at = asyncio.get_event_loop().time()
            if self._on_status:
                self._on_status(False, "reader stopped; reconnecting")
            # Reconnect with backoff until we're up again or told to close.
            while not self._closing:
                self._reconnect_attempts += 1
                delay = min(CAP, BASE * (2 ** min(self._reconnect_attempts - 1, 6)))
                await asyncio.sleep(delay)
                if self._closing:
                    return
                try:
                    await self._open_once()
                except HAAuthError as e:
                    log.error("ws re-auth rejected, giving up: %s", e)
                    if self._on_status:
                        self._on_status(False, f"auth rejected: {e}")
                    return
                except Exception as e:
                    log.warning("ws reconnect attempt %d failed: %s",
                                self._reconnect_attempts, e)
                    continue
                self._reconnect_attempts = 0
                if self._on_status:
                    self._on_status(True, "reconnected")
                await self._after_connect()
                break

    async def _after_connect(self) -> None:
        """Replay whatever must exist on a fresh socket (subscriptions)."""
        if self._on_reconnect:
            try:
                await self._on_reconnect()
            except Exception as e:
                log.warning("resubscribe hook failed: %s", e)

    async def close(self) -> None:
        self._closing = True
        self.connected = False
        if self._supervisor:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except (asyncio.CancelledError, Exception):
                pass
            self._supervisor = None
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
            self._reader = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        for p in self._pending.values():
            if not p.fut.done():
                p.fut.set_exception(HAConnectionError("connection closed"))
        self._pending.clear()

    # ---- reader ----------------------------------------------------

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mid = msg.get("id")
                p = self._pending.get(mid) if mid is not None else None
                mtype = msg.get("type")
                if mtype == "result" and p is not None:
                    if msg.get("success"):
                        p.fut.set_result(msg.get("result"))
                    else:
                        err = msg.get("error") or {}
                        p.fut.set_exception(HAError(
                            f"{err.get('code','error')}: {err.get('message','unknown')}"))
                    if p.stream is None:
                        self._pending.pop(mid, None)
                elif mtype == "event" and p is not None and p.stream is not None:
                    ev = msg.get("event") or {}
                    res = p.stream(ev)
                    if asyncio.iscoroutine(res):
                        asyncio.create_task(res)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("ws reader stopped: %s", e)
        finally:
            self.connected = False

    # ---- request/response -----------------------------------------

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def request(self, payload: dict, *, stream_cb=None,
                      timeout: float = 15.0) -> Any:
        if not self.connected or self._ws is None:
            raise HAConnectionError("not connected")
        mid = self._next_id()
        payload = {"id": mid, **payload}
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[mid] = _Pending(fut=fut, stream=stream_cb)
        await self._ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise HAError(f"timeout waiting for id={mid} ({payload.get('type')})")

    # ---- convenience ----------------------------------------------

    async def subscribe_states(self, callback) -> int:
        """Fire `callback(event_dict)` on every state_changed. Returns the
        subscription id (for future unsubscribe)."""
        return await self.request({"type": "subscribe_events",
                                   "event_type": "state_changed"},
                                  stream_cb=callback)

    async def create_helper(self, domain: str, payload: dict) -> dict:
        """Create an input_* helper via the storage collection."""
        return await self.request(
            {"type": f"{domain}/create", **payload})

    async def update_helper(self, domain: str, helper_id: str, payload: dict) -> dict:
        """Change a helper's settings (a range, the options) in place."""
        return await self.request(
            {"type": f"{domain}/update", f"{domain}_id": helper_id, **payload})

    async def delete_helper(self, domain: str, helper_id: str) -> dict:
        return await self.request(
            {"type": f"{domain}/delete", f"{domain}_id": helper_id})

    async def list_helpers(self, domain: str) -> list[dict]:
        return await self.request({"type": f"{domain}/list"})
