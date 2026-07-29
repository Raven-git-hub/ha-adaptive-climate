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
        self._pending: dict[int, _Pending] = {}
        self._id = 0
        self.connected = False
        self.ha_version: str | None = None

    # ---- lifecycle -------------------------------------------------

    async def connect(self) -> None:
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

    async def close(self) -> None:
        self.connected = False
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
        """Create an input_boolean / input_select via the storage collection."""
        return await self.request(
            {"type": f"{domain}/create", **payload})

    async def delete_helper(self, domain: str, helper_id: str) -> dict:
        return await self.request(
            {"type": f"{domain}/delete", f"{domain}_id": helper_id})

    async def list_helpers(self, domain: str) -> list[dict]:
        return await self.request({"type": f"{domain}/list"})
