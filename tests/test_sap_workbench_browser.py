"""G1 browser node: view tokens, input encoding and the screen gateway.

These exercise the pure pieces (token lifetime, coordinate/input encoding) and
the aiohttp gateway's handshake with an injected fake node, so the protocol can
be pinned without launching a real Chrome. The real Chrome+CDP round trip lives
in the opt-in live test, not here.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer
from aiohttp.test_utils import make_mocked_request

from Scene.sap_workbench.browser_service import mapping
from Scene.sap_workbench.browser_service.gateway import build_app
from Scene.sap_workbench.browser_service.tokens import ViewTokens
from Scene.sap_workbench.browser_service.node import _CDP, BrowserNode, BrowserNodeError

VIEW = {"width": 1000, "height": 500}


@pytest.mark.parametrize('size', [None, [], {}, {'t':'resize','width':True,'height':500},
    {'t':'resize','width':0,'height':500}, {'t':'resize','width':4097,'height':500},
    {'t':'resize','width':'1000','height':500}, {'t':'resize','width':1000,'height':1.5}])
def test_resize_rejects_invalid_dimensions(size):
    assert mapping.resize_viewport(size) is None


def test_browser_resize_changes_layout_without_navigating_or_spoofing_frame_dimensions():
    async def scenario():
        node = BrowserNode(url='https://sap.test', profile_dir='unused', executable='unused')
        node._cdp = SimpleNamespace(call=AsyncMock())
        await node.resize(1600, 900)
        node._cdp.call.assert_awaited_once_with('Emulation.setDeviceMetricsOverride', {
            'width':1600, 'height':900, 'deviceScaleFactor':1, 'mobile':False})
        assert node.viewport == {'width':1280,'height':800}, 'frames remain authoritative'
        with pytest.raises(BrowserNodeError):
            await node.resize(100000, 900)
        assert node._cdp.call.await_count == 1
    asyncio.run(scenario())


def test_slow_navigation_preserves_browser_but_protocol_failure_does_not():
    async def scenario():
        class CDP:
            failure = "Page.navigate: timed out"
            async def call(self, *args, **kwargs):
                raise BrowserNodeError(self.failure)
        node = BrowserNode(url="https://sap.test", profile_dir="unused", executable="unused")
        node._cdp = CDP()
        await node._navigate()
        node._cdp.failure = "cdp connection is closed"
        with pytest.raises(BrowserNodeError, match="closed"):
            await node._navigate()
    asyncio.run(scenario())


def test_chrome_exit_wakes_a_waiting_frame_stream():
    async def scenario():
        exit_chrome = asyncio.Event()
        class Socket:
            async def __aiter__(self):
                await exit_chrome.wait()
                if False:
                    yield None
        cdp = _CDP(Socket())
        cdp.start()
        async def collect():
            return [event async for event in cdp.events()]
        frames = asyncio.create_task(collect())
        await asyncio.sleep(0)
        exit_chrome.set()
        assert await asyncio.wait_for(frames, timeout=1) == []
        assert cdp.closed
    asyncio.run(scenario())


# --- tokens ---------------------------------------------------------------

def test_view_token_is_bound_and_expires():
    clock = [1000.0]
    tokens = ViewTokens(ttl=30, clock=lambda: clock[0])
    token = tokens.issue("t1", "u1", generation=7)
    assert tokens.verify(token, tenant_id="t1", user_id="u1")["generation"] == 7
    # A token for one tenant/user must not verify for another.
    assert tokens.verify(token, tenant_id="t2", user_id="u1") is None
    assert tokens.verify(token, tenant_id="t1", user_id="u2") is None
    assert tokens.verify("not-a-token") is None
    clock[0] += 31
    assert tokens.verify(token, tenant_id="t1", user_id="u1") is None


def test_view_token_can_be_revoked():
    tokens = ViewTokens(ttl=30, clock=lambda: 0.0)
    token = tokens.issue("t1", "u1")
    assert tokens.verify(token, tenant_id="t1", user_id="u1") is not None
    tokens.revoke(token)
    assert tokens.verify(token, tenant_id="t1", user_id="u1") is None


# --- coordinate mapping ---------------------------------------------------

def test_viewport_point_scales_and_clamps():
    assert mapping.viewport_point({"w": 1000, "h": 500}, VIEW, 500, 250) == (500, 250)
    # Half-size canvas maps to the same viewport point.
    assert mapping.viewport_point({"w": 500, "h": 250}, VIEW, 250, 125) == (500, 250)
    # Out-of-bounds clicks clamp to the viewport rather than escaping it.
    assert mapping.viewport_point({"w": 1000, "h": 500}, VIEW, 99999, -50) == (1000, 0)


def test_viewport_point_survives_a_degenerate_canvas():
    assert mapping.viewport_point({"w": 0, "h": 0}, VIEW, 10, 10) == (0, 0)


# --- input encoding -------------------------------------------------------

def test_mouse_events_encode_to_dispatch_calls():
    down = mapping.encode_input(
        {"t": "mouse", "event": "down", "button": "left", "x": 500, "y": 250,
         "canvas": {"w": 1000, "h": 500}}, VIEW)
    assert down == [{"method": "Input.dispatchMouseEvent", "params": {
        "type": "mousePressed", "x": 500, "y": 250, "button": "left", "clickCount": 1}}]

    up = mapping.encode_input(
        {"t": "mouse", "event": "up", "button": "left", "x": 500, "y": 250,
         "canvas": {"w": 1000, "h": 500}}, VIEW)
    assert up[0]["params"]["type"] == "mouseReleased"

    move = mapping.encode_input(
        {"t": "mouse", "event": "move", "x": 10, "y": 20,
         "canvas": {"w": 1000, "h": 500}}, VIEW)
    assert move[0]["params"]["type"] == "mouseMoved"
    assert move[0]["params"]["button"] == "none"


def test_wheel_event_carries_deltas():
    commands = mapping.encode_input(
        {"t": "mouse", "event": "wheel", "x": 1, "y": 2, "deltaX": 0, "deltaY": -120,
         "canvas": {"w": 1000, "h": 500}}, VIEW)
    assert commands[0]["params"]["type"] == "mouseWheel"
    assert commands[0]["params"]["deltaY"] == -120


def test_printable_text_uses_insert_text():
    commands = mapping.encode_input({"t": "text", "text": "你好"}, VIEW)
    assert commands == [{"method": "Input.insertText", "params": {"text": "你好"}}]
    # A printable key press also inserts its text rather than faking keydown.
    keyed = mapping.encode_input({"t": "key", "code": "KeyA", "key": "a", "text": "a"}, VIEW)
    assert keyed == [{"method": "Input.insertText", "params": {"text": "a"}}]


@pytest.mark.parametrize("message,vk", [
    ({"t": "key", "code": "Enter", "key": "Enter"}, 13),
    ({"t": "key", "code": "Tab", "key": "Tab"}, 9),
    ({"t": "key", "code": "Backspace", "key": "Backspace"}, 8),
    ({"t": "key", "code": "F4", "key": "F4"}, 115),
    ({"t": "key", "code": "ArrowDown", "key": "ArrowDown"}, 40),
])
def test_control_keys_dispatch_key_events(message, vk):
    commands = mapping.encode_input(message, VIEW)
    types = [c["params"]["type"] for c in commands]
    assert types == ["rawKeyDown", "keyUp"]
    assert commands[0]["params"]["windowsVirtualKeyCode"] == vk


def test_shortcut_keeps_the_key_and_sets_the_modifier():
    commands = mapping.encode_input(
        {"t": "key", "code": "KeyA", "key": "a", "text": "a", "ctrl": True}, VIEW)
    assert commands[0]["params"]["type"] == "rawKeyDown"
    assert commands[0]["params"]["windowsVirtualKeyCode"] == 65
    assert commands[0]["params"]["modifiers"] == 2


def test_unknown_key_is_ignored_rather_than_guessed():
    assert mapping.encode_input({"t": "key", "code": "Unknown99", "key": "?"}, VIEW) == []
    assert mapping.encode_input({"t": "nonsense"}, VIEW) == []


# --- gateway handshake ----------------------------------------------------

class FakeNode:
    """Records the CDP commands the gateway forwards and yields one frame."""

    def __init__(self):
        self.commands = []
        self.closed = False
        self._sent = False

    async def frames(self):
        yield {"viewport": VIEW, "data": b"\xff\xd8jpegbytes"}
        await asyncio.sleep(3600)

    async def send(self, commands):
        self.commands.extend(commands)

    async def close(self):
        self.closed = True


def run(coro):
    return asyncio.run(coro)


def gateway(tokens, node):
    async def node_factory(claims):
        node.claims = claims
        return node
    return build_app(tokens=tokens, node_factory=node_factory, allowed_origins=["http://localhost:9899"])


def ws_url(server, token, origin="http://localhost:9899"):
    return f"http://127.0.0.1:{server.port}/screen"


def connect(client, token, origin="http://localhost:9899"):
    return client.ws_connect("/screen", protocols=[token], headers={"Origin": origin})


def test_gateway_rejects_a_missing_or_bad_token_and_a_foreign_origin():
    tokens = ViewTokens(ttl=30, clock=lambda: 0.0)

    async def scenario():
        async with TestServer(gateway(tokens, FakeNode())) as server:
            async with TestClient(server) as client:
                for headers, protocols, status in (
                    ({"Origin": "http://localhost:9899"}, [], 401),
                    ({"Origin": "http://localhost:9899"}, ["nonsense"], 401),
                    ({"Origin": "http://evil.test"}, [tokens.issue("t1", "u1")], 403),
                ):
                    with pytest.raises(aiohttp.WSServerHandshakeError) as error:
                        await client.ws_connect("/screen", protocols=protocols, headers=headers)
                    assert error.value.status == status

    run(scenario())


def test_gateway_binds_the_token_to_the_requesting_origin():
    tokens = ViewTokens(ttl=30, clock=lambda: 0.0)

    async def scenario():
        async with TestServer(gateway(tokens, FakeNode())) as server:
            async with TestClient(server) as client:
                # A token minted for a specific origin only opens from it, even
                # when the origin is otherwise on the allow-list.
                pinned = tokens.issue("t1", "u1", origin="http://localhost:9899")
                async with client.ws_connect(
                        "/screen", protocols=[pinned],
                        headers={"Origin": "http://localhost:9899"}) as ws:
                    assert json.loads(await ws.receive_str())["t"] == "ready"
                with pytest.raises(aiohttp.WSServerHandshakeError) as error:
                    await client.ws_connect("/screen", protocols=[pinned],
                                            headers={"Origin": "http://localhost:9899"})
                assert error.value.status == 401
                pinned = tokens.issue("t1", "u1", origin="http://localhost:9899")
                with pytest.raises(aiohttp.WSServerHandshakeError) as error:
                    await client.ws_connect("/screen", protocols=[pinned],
                                            headers={"Origin": "http://other.local"})
                assert error.value.status == 403

    run(scenario())


def test_gateway_reports_an_unavailable_runtime_instead_of_upgrading():
    tokens = ViewTokens(ttl=30, clock=lambda: 0.0)

    async def broken_factory(claims):
        raise RuntimeError("no chrome")

    async def scenario():
        app = build_app(tokens=tokens, node_factory=broken_factory,
                        allowed_origins=["http://localhost:9899"])
        async with TestServer(app) as server:
            async with TestClient(server) as client:
                with pytest.raises(aiohttp.WSServerHandshakeError) as error:
                    await client.ws_connect(
                        "/screen", protocols=[tokens.issue("t1", "u1")],
                        headers={"Origin": "http://localhost:9899"})
                assert error.value.status == 503

    run(scenario())


def test_gateway_resize_publishes_new_dimensions_before_mapping_following_input():
    async def scenario():
        class ResizingNode(FakeNode):
            def __init__(self):
                super().__init__()
                self.viewport = dict(VIEW)
                self.resizes = []
                self.changed = asyncio.Event()
                self.input_received = asyncio.Event()
            async def frames(self):
                yield {'viewport':dict(self.viewport),'data':b'initial'}
                await self.changed.wait()
                yield {'viewport':dict(self.viewport),'data':b'resized'}
                await asyncio.sleep(3600)
            async def resize(self, width, height):
                self.resizes.append((width,height))
                self.viewport = {'width':width,'height':height}
                self.changed.set()
            async def send(self, commands):
                await super().send(commands)
                self.input_received.set()
        tokens, node = ViewTokens(ttl=30), ResizingNode()
        async with TestServer(gateway(tokens,node)) as server, TestClient(server) as client:
            async with connect(client,tokens.issue('t1','u1')) as ws:
                await ws.receive_str(); await ws.receive_bytes()
                await ws.send_json({'t':'resize','width':True,'height':900})
                await ws.send_json({'t':'resize','width':1600,'height':900})
                ready = json.loads(await asyncio.wait_for(ws.receive_str(),2))
                assert ready == {'t':'ready','viewport':{'width':1600,'height':900}}
                assert await ws.receive_bytes() == b'resized'
                await ws.send_json({'t':'mouse','event':'down','button':'left',
                    'canvas':{'w':800,'h':450},'x':400,'y':225})
                await asyncio.wait_for(node.input_received.wait(),2)
                assert node.resizes == [(1600,900)]
                assert node.commands[0]['params']['x'] == 800
                assert node.commands[0]['params']['y'] == 450
    run(scenario())


def test_gateway_streams_frames_and_forwards_input():
    tokens = ViewTokens(ttl=30, clock=lambda: 0.0)
    token = tokens.issue("t1", "u1", generation=3)
    node = FakeNode()

    async def scenario():
        async with TestServer(gateway(tokens, node)) as server:
            async with TestClient(server) as client:
                async with client.ws_connect(
                        "/screen", protocols=[token],
                        headers={"Origin": "http://localhost:9899"}) as ws:
                    ready = json.loads(await ws.receive_str())
                    assert ready == {"t": "ready", "viewport": VIEW}
                    frame = await ws.receive()
                    assert frame.type == aiohttp.WSMsgType.BINARY
                    assert frame.data == b"\xff\xd8jpegbytes"
                    await ws.send_str(json.dumps(
                        {"t": "mouse", "event": "down", "button": "left", "x": 500, "y": 250,
                         "canvas": {"w": 1000, "h": 500}}))
                    for _ in range(50):
                        if node.commands:
                            break
                        await asyncio.sleep(0.02)
                    assert node.commands[0]["method"] == "Input.dispatchMouseEvent"
                    assert node.claims["generation"] == 3
                for _ in range(50):
                    if node.closed:
                        break
                    await asyncio.sleep(0.02)
                assert node.closed

    run(scenario())


def test_gateway_pushes_control_changes_and_closes_when_authorization_expires():
    class StatefulNode(FakeNode):
        def __init__(self):
            super().__init__()
            self.state = {'control': 'automatic', 'epoch': 1, 'login_required': False}
            self.revoked = False

        async def view_state(self):
            if self.revoked:
                raise RuntimeError('access revoked')
            return dict(self.state)

    async def scenario():
        tokens = ViewTokens(ttl=30, clock=lambda: 0.0)
        node = StatefulNode()
        async with TestServer(gateway(tokens, node)) as server, TestClient(server) as client:
            async with connect(client, tokens.issue('t1', 'u1')) as ws:
                async def next_control():
                    while True:
                        message = await ws.receive(timeout=3)
                        if message.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(message.data)
                            if data.get('t') == 'control':
                                return data
                assert (await next_control())['control'] == 'automatic'
                node.state.update(control='manual', epoch=2, login_required=True)
                assert await next_control() == {'t': 'control', **node.state}
                node.revoked = True
                assert (await ws.receive(timeout=3)).type == aiohttp.WSMsgType.CLOSE
            assert node.closed

    run(scenario())


@pytest.mark.parametrize('cancelled', [False, True])
def test_lost_screen_handshake_detaches_allocated_browser_for_retry(monkeypatch, cancelled):
    async def scenario():
        tokens = ViewTokens(ttl=30, clock=lambda: 0.0)
        node = SimpleNamespace(detach=AsyncMock(), close=AsyncMock())
        factory = AsyncMock(return_value=node)
        app = build_app(tokens=tokens, node_factory=factory)
        token = tokens.issue('t1', 'u1')
        request = make_mocked_request('GET', '/screen', headers={'Sec-WebSocket-Protocol': token}, app=app)
        error = asyncio.CancelledError if cancelled else ConnectionResetError
        monkeypatch.setattr(aiohttp.web.WebSocketResponse, 'prepare', AsyncMock(side_effect=error()))
        handler = (await app.router.resolve(request)).handler
        with pytest.raises(error):
            await handler(request)
        node.detach.assert_awaited_once()
        node.close.assert_not_awaited()
        assert tokens.verify(token) is None
        factory.assert_awaited_once()
    run(scenario())
