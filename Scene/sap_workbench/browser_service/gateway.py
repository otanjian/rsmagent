"""Loopback WebSocket gateway that streams one browser node to the pane.

The main Web app is a WSGI server and cannot upgrade a WebSocket, so the
persistent screen connection lives here instead: a small aiohttp application
bound to ``127.0.0.1`` on an ephemeral port, hosted in a daemon thread by
:mod:`runner`. That mirrors ``integrations/desktop/local_gateway.py`` and keeps
graphics encoding out of the main service, as the change design requires.

Authentication happens before the upgrade: the pane presents its short-lived
view token as the WebSocket sub-protocol (a browser cannot set a custom header
on a WebSocket), the ``Origin`` must be one of the registered app origins, and
only then is the node attached.
"""
from __future__ import annotations

import asyncio
import os
import json

from common.log import logger

from . import mapping

#: A JPEG frame is far below this; the cap bounds a hostile client's messages.
FRAME_MAX = 8 * 1024 * 1024

#: Server-side ping interval, in seconds. Off by default: a live-but-idle pane
#: is normal (a static SAP screen produces no frames), so a missed pong must
#: not silently tear down a user's session. Overridable for diagnostics.
HEARTBEAT = float(os.environ.get("SAP_WORKBENCH_WS_HEARTBEAT", "0"))


def origin_allowed(claims, origin, allowed) -> bool:
    """Whether ``origin`` may attach to the pane described by ``claims``.

    A token minted with an ``origin`` only opens from exactly that origin; one
    minted without falls back to the configured allow-list.
    """
    expected = claims.get("origin")
    if expected:
        return origin == expected
    return not allowed or origin in allowed


def build_app(*, tokens, node_factory, allowed_origins=None):
    """Build the aiohttp app.

    ``node_factory(claims)`` returns the object the pane drives: an async
    ``frames()`` iterator yielding ``{"viewport": {...}, "data": b"..."}`` and
    an async ``send(commands)``. It is injected so the protocol can be tested
    without a real browser.
    """
    from aiohttp import web

    allowed = tuple(allowed_origins) if allowed_origins else ()

    async def screen(request):
        from aiohttp import WSMsgType

        offered = [p.strip() for p in
                   (request.headers.get("Sec-WebSocket-Protocol") or "").split(",")
                   if p.strip()]
        token = offered[0] if offered else ""
        claims = tokens.verify(token)
        if claims is None:
            return web.json_response(
                {"status": "error", "code": "unauthorized"}, status=401)

        origin = request.headers.get("Origin", "")
        if not origin_allowed(claims, origin, allowed):
            return web.json_response(
                {"status": "error", "code": "forbidden"}, status=403)

        # One connection per grant; reconnect obtains a freshly authorized one.
        tokens.revoke(token)

        try:
            node = await node_factory(claims)
        except Exception:  # noqa: BLE001 - report as state, never leak a stack
            logger.exception("[SapWorkbench] browser node unavailable")
            return web.json_response(
                {"status": "error", "code": "runtime_unavailable"}, status=503)
        # Echo the token back as the selected sub-protocol; a browser that
        # offered one expects the server to pick it.
        ws = web.WebSocketResponse(max_msg_size=FRAME_MAX,
                                   heartbeat=HEARTBEAT or None,
                                   protocols=[token])
        try:
            await ws.prepare(request)
        except BaseException:
            # Allocation may have succeeded before the client lost the
            # handshake. Release the attachment so retry can adopt this node.
            detach = getattr(node, 'detach', None)
            await (detach() if detach is not None else node.close())
            raise

        viewport: dict = {}

        async def pump_frames():
            async for frame in node.frames():
                incoming = frame.get("viewport") or {}
                if incoming != viewport:
                    viewport.clear()
                    viewport.update(incoming)
                    await ws.send_str(json.dumps({"t": "ready", "viewport": dict(viewport)}))
                await ws.send_bytes(frame["data"])

        async def read_input():
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(message.data)
                except ValueError:
                    continue
                size = mapping.resize_viewport(payload)
                if size is not None:
                    await node.resize(**size)
                    continue
                commands = mapping.encode_input(payload, viewport)
                if commands:
                    await node.send(commands)

        pump = asyncio.create_task(pump_frames(), name="screen-frames")
        reader = asyncio.create_task(read_input(), name="screen-input")
        async def pump_state():
            previous = None
            while True:
                state = await node.view_state()
                if state != previous:
                    await ws.send_str(json.dumps({'t': 'control', **state}))
                    previous = state
                await asyncio.sleep(1)
        tasks = {pump, reader}
        if hasattr(node, 'view_state'):
            tasks.add(asyncio.create_task(pump_state(), name='screen-state'))
        try:
            # Whichever side ends first ends the view: a closed pane must stop
            # the frame pump, not leave it streaming to a dead socket.
            done, pending = await asyncio.wait(tasks,
                                               return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            # Record why the view ended: without this a silently dropped socket
            # and a crashed browser look identical from the outside.
            for task in done:
                if task.cancelled():
                    continue
                error = task.exception()
                if error is None:
                    logger.info("[SapWorkbench] pane view ended by %s", task.get_name())
                else:
                    logger.warning("[SapWorkbench] pane view ended by %s: %r",
                                   task.get_name(), error)
        finally:
            # Handler cancellation can jump straight out of asyncio.wait.
            # Every task belongs to this view and must stop before detach lets
            # a new pane consume the same browser stream.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if ws.closed:
                logger.info("[SapWorkbench] pane socket closed (code=%s)", ws.close_code)
            # Detach, do not close: a dropped socket is usually a reconnect in
            # progress (reload, sleeping laptop, tab switch), and the manager
            # reclaims the browser if nobody comes back.
            detach = getattr(node, "detach", None)
            if detach is not None:
                await detach()
            else:
                await node.close()
        return ws

    app = web.Application()
    app.router.add_get("/screen", screen)
    return app
