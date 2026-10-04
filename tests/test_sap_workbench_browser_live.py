"""Opt-in live check of the browser node against a real Chrome.

Skipped unless ``SAP_WORKBENCH_LIVE=1``: it spawns a real Chrome and is far too
slow and machine-dependent for the default suite. It exists because the unit
tests prove the wiring and this proves the thing actually renders and types.

What it proves, end to end (pane WS -> gateway -> CDP -> page):

* frames really arrive as JPEG over the gateway, and
* the input mapping really lands text and clicks in the page.
"""
import asyncio
import base64
import json
import os
import tempfile
import urllib.parse

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from Scene.sap_workbench.browser_service.gateway import build_app
from Scene.sap_workbench.browser_service.node import BrowserNode
from Scene.sap_workbench.browser_service.tokens import ViewTokens

pytestmark = pytest.mark.skipif(
    os.environ.get("SAP_WORKBENCH_LIVE") != "1",
    reason="set SAP_WORKBENCH_LIVE=1 to drive a real Chrome")

PAGE = """<!doctype html><meta charset="utf-8">
<input id="q" autofocus placeholder="type">
<div id="hits">0</div>
<button id="btn" style="position:fixed;left:0;top:40px;width:200px;height:100px"
        onclick="document.getElementById('hits').textContent=(+document.getElementById('hits').textContent+1)">hit</button>
"""


def _chrome() -> str:
    from agent.tools.browser.browser_env import detect_system_chrome
    path = (detect_system_chrome() or {}).get("path", "")
    if not path:
        pytest.skip("no system Chrome available")
    return path


async def _evaluate(node, expression):
    result = await node._cdp.call("Runtime.evaluate",
                                  {"expression": expression, "returnByValue": True})
    return result["result"].get("value")


def test_live_node_streams_frames_and_lands_input():
    asyncio.run(_scenario())


async def _scenario():
    target = "data:text/html;charset=utf-8," + urllib.parse.quote(PAGE)
    node = BrowserNode(url=target, profile_dir=tempfile.mkdtemp(prefix="sap-node-"),
                       executable=_chrome(), headless=True)
    tokens = ViewTokens(ttl=60)

    async def node_factory(claims):
        # Same contract as the manager: the factory hands back a started node.
        await node.start()
        return node

    app = build_app(tokens=tokens, node_factory=node_factory, allowed_origins=None)
    try:
        async with TestServer(app) as server:
            async with TestClient(server) as client:
                token = tokens.issue("t1", "u1", origin=None, url=target)
                async with client.ws_connect("/screen", protocols=[token],
                                             headers={"Origin": "http://localhost:9899"}) as ws:
                    ready = json.loads(await ws.receive_str())
                    viewport = ready["viewport"]
                    assert ready["t"] == "ready" and viewport["width"] > 0

                    frame = await ws.receive()
                    assert frame.type == aiohttp.WSMsgType.BINARY
                    jpeg = frame.data
                    assert jpeg[:2] == b"\xff\xd8" and jpeg[-2:] == b"\xff\xd9"
                    assert len(jpeg) > 1000, "a real screen, not an empty canvas"
                    # Round-trip check: the base64 payload decodes to the same JPEG.
                    assert base64.b64decode(base64.b64encode(jpeg)) == jpeg

                    # Typing: insertText into the autofocused input.
                    await ws.send_str(json.dumps({"t": "text", "text": "AB你好"}))
                    await _wait(lambda: _evaluate(node, "document.getElementById('q').value"))
                    assert await _evaluate(node, "document.getElementById('q').value") == "AB你好"

                    # Clicking: canvas coordinates map 1:1 to the viewport here.
                    box = {"w": viewport["width"], "h": viewport["height"]}
                    for name in ("down", "up"):
                        await ws.send_str(json.dumps(
                            {"t": "mouse", "event": name, "button": "left",
                             "x": 50, "y": 90, "canvas": box}))
                    await _wait(lambda: _evaluate(node, "document.getElementById('hits').textContent"))
                    assert await _evaluate(node, "document.getElementById('hits').textContent") == "1"
    finally:
        await node.close()


async def _wait(read, timeout=10.0):
    """Poll until ``read`` returns something truthy (and not just ``\"0\"``)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            value = await read()
        except Exception:  # noqa: BLE001 - the page may not be ready yet
            value = None
        if value:
            return value
        await asyncio.sleep(0.2)
    return None
