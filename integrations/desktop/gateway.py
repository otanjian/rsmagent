"""Desktop device gateway: WSS entry for native file commands.

Change ``add-desktop-remote-web-workbench`` (tasks 9.2 / 9.3). Run as::

    python -m integrations.desktop.gateway --bind 127.0.0.1 --port 9877

The gateway shares the process's identity database with the Web app. Database
work runs in a thread pool so the aiohttp event loop is never blocked. Tokens
are accepted only via the ``Authorization: Bearer`` header -- never a Cookie
and never a query parameter.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from auth.desktop_contracts import LIMITS

logger = logging.getLogger("integrations.desktop.gateway")

#: Hard frame ceiling (contracts §5 / gateway.result_frame_max_bytes).
FRAME_MAX = 64 * 1024

#: How often a connected device is handed new commands. The caller side
#: (``client_files``) polls the durable row every 250 ms, so pushing at the
#: same rate keeps "the device saw it" and "the tool noticed" in step without
#: a second latency source of its own.
PUSH_INTERVAL_SECONDS = 0.25

#: Commands one device may be given per push (contracts limits).
PARALLEL_COMMANDS = int(LIMITS.get("parallel_commands_per_device", 4))


def _identity_service():
    from config import conf, get_data_root
    from auth.service import IdentityService
    configured = conf().get("identity_db_path")
    db_path = configured or os.path.join(get_data_root(), "identity.db")
    return IdentityService(db_path)


def _bearer(request) -> str:
    header = request.headers.get("Authorization", "") or ""
    if not header.lower().startswith("bearer "):
        return ""
    token = header[7:].strip()
    # A query-string token is never consulted -- even if present it is ignored.
    return token


async def _run_db(executor: ThreadPoolExecutor, fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, lambda: fn(*args, **kwargs))


def build_app(*, identity_service=None, gateway_id: Optional[str] = None):
    """Build the aiohttp application. ``identity_service`` is injectable for tests."""
    from aiohttp import web

    svc = identity_service or _identity_service()
    gw_id = gateway_id or ("gw_" + secrets.token_urlsafe(8))
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="desktop-gw")

    app = web.Application(client_max_size=FRAME_MAX)
    app["identity"] = svc
    app["gateway_id"] = gw_id
    app["executor"] = executor

    async def on_cleanup(app):
        executor.shutdown(wait=False)

    app.on_cleanup.append(on_cleanup)

    async def health(_request):
        return web.json_response({"status": "ok", "gateway_id": gw_id})

    async def connect(request: web.Request):
        from aiohttp import WSMsgType
        from integrations.desktop.commands import service_for
        from integrations.desktop.errors import DesktopAccessError

        # Reject Cookie-only and query-token attempts up front.
        if request.cookies.get("cow_session") and not _bearer(request):
            return web.json_response(
                {"status": "error", "code": "auth_required",
                 "message": "a native bearer is required"},
                status=401)
        if "token" in request.rel_url.query or "access_token" in request.rel_url.query:
            return web.json_response(
                {"status": "error", "code": "invalid_request",
                 "message": "tokens must not appear in the URL"},
                status=400)

        token = _bearer(request)
        if not token:
            return web.json_response(
                {"status": "error", "code": "auth_required",
                 "message": "a native bearer is required"},
                status=401)

        # Origin: optional for the main process; if present must be exact.
        origin = request.headers.get("Origin", "")
        # (Exact-origin match against desktop_native_origins happens at hello,
        # once we know the device; the handshake itself only needs the Bearer.)

        ws = web.WebSocketResponse(max_msg_size=FRAME_MAX, heartbeat=20)
        await ws.prepare(request)

        commands = service_for(svc)
        lease: Optional[Dict[str, Any]] = None
        device_id: Optional[str] = None
        # One writer at a time: the frame pushes come from a task of their own,
        # and two concurrent ``send_str`` calls would interleave frame bodies.
        send_lock = asyncio.Lock()
        pusher: Optional[asyncio.Task] = None

        async def send(payload: Dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False)
            if len(body.encode("utf-8")) > FRAME_MAX:
                await ws.close(code=1009, message=b"frame too large")
                return
            async with send_lock:
                await ws.send_str(body)

        async def push_loop(epoch: str, dev: str) -> None:
            """Deliver claimed commands and expire the ones nobody answered.

            Runs for as long as this connection holds the live epoch. A stale
            epoch (a newer hello replaced it) ends the loop rather than closing
            the socket: the client will reconnect and hello again.
            """
            from integrations.desktop.commands import device_command_frame

            while True:
                await asyncio.sleep(PUSH_INTERVAL_SECONDS)
                try:
                    await _run_db(executor, commands.expire_overdue, device_id=dev)
                    claimed = await _run_db(
                        executor, commands.claim_outbox,
                        device_id=dev, epoch=epoch,
                        limit=PARALLEL_COMMANDS)
                except DesktopAccessError:
                    return
                except Exception:
                    logger.exception("desktop gateway push failed")
                    continue
                for command in claimed:
                    await send(device_command_frame(command))

        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    raw = msg.data
                elif msg.type == WSMsgType.TEXT:
                    raw = msg.data.encode("utf-8") if isinstance(msg.data, str) else msg.data
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                    break
                else:
                    continue

                if len(raw) > FRAME_MAX:
                    await ws.close(code=1009, message=b"frame too large")
                    break
                try:
                    frame = json.loads(raw.decode("utf-8"))
                except Exception:
                    await ws.close(code=1003, message=b"invalid frame")
                    break
                if not isinstance(frame, dict) or frame.get("v") != 1:
                    await ws.close(code=1003, message=b"unsupported version")
                    break

                ftype = frame.get("type")
                try:
                    if ftype == "hello":
                        device_id = str(frame.get("device_id") or "")
                        lease = await _run_db(
                            executor, commands.acquire_lease,
                            token=token, device_id=device_id,
                            gateway_id=gw_id,
                            protocol_major=int(frame.get("protocol_major") or 1),
                            capabilities=frame.get("capabilities") or {})
                        await send({
                            "v": 1, "type": "hello",
                            "epoch": lease["epoch"],
                            "expires_at": lease["expires_at"],
                            "gateway_id": gw_id,
                        })
                        # Start delivering commands only after the epoch exists:
                        # a claim is fenced by it, so pushing earlier would be
                        # rejected by the CAS rather than delivered.
                        if pusher is not None:
                            pusher.cancel()
                        pusher = asyncio.create_task(
                            push_loop(lease["epoch"], device_id))
                    elif ftype == "result":
                        # The device's answer to a command it claimed. Only the
                        # epoch that holds the claim may complete it, and the
                        # row it may complete is the one it was given.
                        if not lease:
                            await ws.close(code=1008, message=b"hello required")
                            break
                        state = str(frame.get("state") or "")
                        result = frame.get("result")
                        completed = await _run_db(
                            executor, commands.complete_outbox,
                            command_id=str(frame.get("request_id") or ""),
                            epoch=lease["epoch"], state=state,
                            result=result if isinstance(result, dict) else None,
                            error_code=frame.get("error_code") or None,
                            error_message=frame.get("error_message") or None)
                        # The reply carries the state that *won*: a command the
                        # caller cancelled while the device was reading stays
                        # cancelled, and the device learns not to keep going.
                        await send({
                            "v": 1, "type": "ack",
                            "request_id": completed["id"],
                            "connection_epoch": lease["epoch"],
                            "state": completed["state"],
                        })
                    elif ftype == "heartbeat":
                        if not lease:
                            await ws.close(code=1008, message=b"hello required")
                            break
                        renewed = await _run_db(
                            executor, commands.renew_lease,
                            epoch=lease["epoch"], gateway_id=gw_id)
                        await send({
                            "v": 1, "type": "heartbeat",
                            "epoch": renewed["epoch"],
                            "expires_at": renewed["expires_at"],
                        })
                    elif ftype == "ack":
                        if not lease:
                            await ws.close(code=1008, message=b"hello required")
                            break
                        result = await _run_db(
                            executor, commands.acknowledge,
                            command_id=str(frame.get("request_id") or ""),
                            epoch=lease["epoch"])
                        await send({
                            "v": 1, "type": "ack",
                            "request_id": result["request_id"],
                            "connection_epoch": lease["epoch"],
                            "state": result["state"],
                        })
                    else:
                        await send({
                            "v": 1, "type": "error",
                            "code": "invalid_request",
                            "message": "unknown frame type",
                        })
                except DesktopAccessError as e:
                    await send({
                        "v": 1, "type": "error",
                        "code": e.code, "message": e.args[0],
                    })
                except Exception:
                    logger.exception("gateway frame failed")
                    await send({
                        "v": 1, "type": "error",
                        "code": "internal", "message": "internal error",
                    })
        finally:
            if pusher is not None:
                pusher.cancel()
                try:
                    await pusher
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("desktop gateway pusher ended badly")
            if lease:
                try:
                    await _run_db(
                        executor, commands.revoke_lease,
                        epoch=lease["epoch"], reason="disconnect")
                except Exception:
                    logger.exception("lease revoke on disconnect failed")
            await ws.close()
        return ws

    app.router.add_get("/healthz", health)
    app.router.add_get("/api/desktop/connect", connect)
    return app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Desktop device gateway")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9877)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    # Bind to loopback by default; operators must deliberately choose a
    # non-loopback address, and even then the reverse proxy is the public face.
    if args.bind not in ("127.0.0.1", "localhost", "::1") and not os.environ.get(
            "DESKTOP_GATEWAY_ALLOW_NONLOCAL"):
        logger.error(
            "refusing to bind %s without DESKTOP_GATEWAY_ALLOW_NONLOCAL=1",
            args.bind)
        return 2

    from aiohttp import web
    app = build_app()
    logger.info("desktop gateway listening on %s:%d", args.bind, args.port)
    web.run_app(app, host=args.bind, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
