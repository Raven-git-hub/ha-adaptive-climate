"""
Adaptive Climate - Home Assistant client (REST + WebSocket).

REST for state pushes (the almanac sensor) and service calls
(climate.set_temperature / set_hvac_mode / set_fan_mode); WebSocket for
the state-change stream that drives reactive detection.

Two hard-won rules carried from Light's cutover:
  * Start the WebSocket read loop BEFORE any subscription, or the stream
    is dead while the log says "connected".
  * Thread context.parent_id through so automation-caused changes are not
    mistaken for user interventions.

STATUS: skeleton.
"""

from __future__ import annotations


class HAError(Exception): ...
class HAAuthError(HAError): ...


class HARest:
    def __init__(self, url: str, token: str, verify_ssl: bool = True) -> None:
        self.url, self.token, self.verify_ssl = url, token, verify_ssl

    async def ping(self):            raise NotImplementedError("Phase 7")
    async def config(self) -> dict:  raise NotImplementedError("Phase 7")
    async def state(self, entity_id: str): raise NotImplementedError("Phase 7")
    async def set_state(self, entity_id: str, state, attributes=None): raise NotImplementedError("Phase 7")
    async def call_service(self, domain: str, service: str, data: dict): raise NotImplementedError("Phase 7")


class HAWebSocket:
    def __init__(self, url: str, token: str, verify_ssl: bool = True) -> None:
        self.url, self.token, self.verify_ssl = url, token, verify_ssl
        self.connected = False
        self.ha_version: str | None = None

    async def connect(self):         raise NotImplementedError("Phase 7")
    async def subscribe_states(self, callback): raise NotImplementedError("Phase 7")
    async def create_helper(self, domain: str, payload: dict): raise NotImplementedError("Phase 7")
    async def delete_helper(self, domain: str, helper_id: str): raise NotImplementedError("Phase 7")
    async def send(self, message: dict): raise NotImplementedError("Phase 7")
