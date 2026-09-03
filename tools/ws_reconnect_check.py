"""Verify HAWebSocket supervisor: reconnects on drop, replays subscriptions,
gives up only on auth failure. Uses a fake websockets.connect."""
import asyncio, json, sys
sys.path.insert(0, '.')
import app.ha as ha

class FakeWS:
    def __init__(self, script):  # script: list of drops before staying up
        self.sent = []; self._q = asyncio.Queue(); self.closed=False
    async def recv(self):
        return await self._q.get()
    async def send(self, m): self.sent.append(m)
    async def close(self): self.closed=True
    def __aiter__(self): return self
    async def __anext__(self):
        msg = await self._q.get()
        if msg is StopAsyncIteration: raise StopAsyncIteration
        return msg
    async def feed_handshake(self, auth_ok=True):
        await self._q.put(json.dumps({"type":"auth_required","ha_version":"2025.1"}))
        await self._q.put(json.dumps({"type":"auth_ok" if auth_ok else "auth_invalid","message":"bad"}))
    def drop(self):  # end the reader's async-for
        self._q.put_nowait(StopAsyncIteration)

sockets = []
auth_ok_seq = [True, True]  # first connect ok, reconnect ok

async def fake_connect(url, ssl=None, open_timeout=10):
    ws = FakeWS(None); sockets.append(ws)
    ok = auth_ok_seq[len(sockets)-1] if len(sockets)-1 < len(auth_ok_seq) else True
    asyncio.get_event_loop().create_task(ws.feed_handshake(ok))
    return ws

async def main():
    ha.websockets.connect = fake_connect
    resubs = []; statuses = []
    c = ha.HAWebSocket("http://ha.local:8123", "tok")
    c.on_reconnect(lambda: resubs.append(1) or asyncio.sleep(0))
    c.on_status(lambda conn, why: statuses.append((conn, why)))

    await c.connect()
    await asyncio.sleep(0.05)
    assert c.connected, "should be connected after connect()"
    assert len(sockets) == 1
    # simulate a real subscribe on socket 1
    # now drop the socket -> supervisor should reconnect
    sockets[0].drop()
    await asyncio.sleep(0.05)  # reader exits, sets connected False
    # backoff first delay is 1s; wait it out
    await asyncio.sleep(1.2)
    assert len(sockets) == 2, f"expected reconnect, got {len(sockets)} sockets"
    assert c.connected, "should be reconnected"
    assert len(resubs) >= 2, f"resubscribe should replay on connect+reconnect, got {len(resubs)}"
    ups = [s for s in statuses if s[0]]; downs=[s for s in statuses if not s[0]]
    assert ups and downs, f"should log up and down transitions: {statuses}"
    print("PASS reconnect+resubscribe+status. sockets=%d resubs=%d statuses=%s"
          % (len(sockets), len(resubs), statuses))
    await c.close()
    assert c._closing and not c.connected

    # --- auth failure is fatal: no infinite retry ---
    sockets.clear(); statuses.clear()
    global auth_ok_seq
    auth_ok_seq = [True, False]  # reconnect gets auth_invalid
    c2 = ha.HAWebSocket("http://ha.local:8123","tok")
    c2.on_status(lambda conn,why: statuses.append((conn,why)))
    await c2.connect(); await asyncio.sleep(0.05)
    sockets[0].drop()
    await asyncio.sleep(1.4)  # one backoff + fatal auth
    assert any("auth" in s[1] for s in statuses), f"auth failure should be surfaced: {statuses}"
    assert c2._supervisor.done(), "supervisor should stop on auth failure"
    print("PASS auth-failure-is-fatal. statuses=%s" % statuses)
    await c2.close()

asyncio.run(main())
print("ALL TESTS PASSED")
