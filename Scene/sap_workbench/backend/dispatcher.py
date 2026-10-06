"""Stable public listener that puts one session runtime behind the base path.

A session runtime binds an *ephemeral loopback* port (see :mod:`runtime`), so a
browser that is not on this machine can never dial it. This module is the single
address the public entry proxy can reach: it decides which session a request
belongs to and forwards it to that session's loopback port, streaming
server-sent events and WebSocket upgrades instead of buffering them.

It is deliberately not a general-purpose proxy:

* it is started only when an https public origin is configured, so the default
  deployment keeps the previous loopback-only behaviour;
* it binds loopback and is reachable only through the trusted entry;
* a request that names no **live** session is refused -- it is never guessed
  from an unknown host, and it never widens a session to another one;
* it adds no authorization of its own. The runtime keeps its per-request
  boundary (session cookie) and platform authorization, and its responses are
  forwarded unchanged, including ``Set-Cookie`` and the frame-ancestors policy.
"""
from __future__ import annotations

import asyncio
import inspect

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

#: The runtime names its session cookie ``sap_scene_<binding>``; the suffix is
#: the binding the cookie belongs to, which is what makes cookie routing exact
#: rather than a search.
COOKIE_PREFIX = 'sap_scene_'

#: Only *connecting* is bounded. A session is long-lived by nature -- an event
#: stream idles between messages -- so the relay must not impose a read timeout.
CONNECT_TIMEOUT = 10

#: Hop-by-hop headers are connection-scoped and must not be relayed. Relaying
#: ``Content-Length`` after re-framing would also desynchronise the response, so
#: the body is always re-sent chunked.
HOP_BY_HOP = frozenset({
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailer', 'transfer-encoding', 'upgrade', 'content-length',
})


def cookie_bindings(cookies):
    """Every binding named by a session cookie, in request order.

    A browser keeps the session cookie of every binding it has ever bootstrapped
    under ``Path=/sapcode``, so one request can carry several ``sap_scene_*``
    cookies while only the newest still names a live session. The caller filters
    these against ``port_of`` rather than refusing the whole request.
    """
    return [name[len(COOKIE_PREFIX):] for name in cookies
            if name.startswith(COOKIE_PREFIX) and len(name) > len(COOKIE_PREFIX)]


def resolve_binding(request, port_of):
    """The live binding this request belongs to, or ``None``.

    Order matters. The bootstrap ``POST`` happens *before* any session cookie
    exists, so it carries the binding explicitly; afterwards the session cookie
    names the session. Every candidate is confirmed against ``port_of`` so a
    forged or stale name resolves to nothing.

    Stale ``sap_scene_*`` cookies from earlier, already-closed sessions are
    ordinary: they are filtered out by ``port_of`` and must not hide the one
    cookie that still names a live session. Only when several cookies name
    distinct *live* sessions -- which the owner lock already prevents -- is the
    request genuinely ambiguous and refused rather than guessed.
    """
    query = request.query.get('binding')
    if query and port_of(query) is not None:
        return query
    live = [candidate for candidate in cookie_bindings(request.cookies)
            if port_of(candidate) is not None]
    return live[0] if len(live) == 1 else None


def _no_redirects():
    """The installed aiohttp's spelling for "do not follow redirects".

    The relay must return the redirect the runtime produced rather than acting on
    it: the bootstrap handshake *is* a 303 the browser has to perform, and
    following it here would swallow both the ``Location`` and the session cookie
    it carries. aiohttp renamed the switch from ``allow_redirects`` to
    ``follow_redirects``, so the spelling is read from the installed version
    instead of assuming one.
    """
    parameters = inspect.signature(ClientSession._request).parameters
    for spelling in ('follow_redirects', 'allow_redirects'):
        if spelling in parameters:
            return {spelling: False}
    raise RuntimeError('this aiohttp exposes no way to stop following redirects')


#: Resolved once: the relay's correctness depends on it, so an unusable aiohttp
#: fails at import rather than silently following a session's redirect.
NO_REDIRECTS = _no_redirects()


def _relay_headers(request):
    """The client's headers, minus connection scope and the local ``Host``."""
    return {key: value for key, value in request.headers.items()
            if key.lower() not in HOP_BY_HOP and key.lower() != 'host'}


async def _proxy_websocket(request, port, query):
    """Relay one WebSocket upgrade, preserving the offered sub-protocol.

    The scene's sockets authenticate with a token carried as the sub-protocol
    (a browser cannot set a header on a socket), so the offered list must reach
    the runtime and the runtime's *selected* protocol must reach the browser.
    """
    protocols = [item.strip() for item in
                 request.headers.get('Sec-WebSocket-Protocol', '').split(',') if item.strip()]
    timeout = ClientTimeout(total=None, sock_connect=CONNECT_TIMEOUT)
    async with ClientSession(timeout=timeout, trust_env=False) as client:
        async with client.ws_connect(f'http://127.0.0.1:{port}{request.path}', params=query,
                                     protocols=protocols, headers=_relay_headers(request)) as upstream:
            selected = upstream.protocol
            downstream = web.WebSocketResponse(protocols=[selected] if selected else ())
            await downstream.prepare(request)

            async def forward(source, target):
                async for message in source:
                    if message.type == WSMsgType.TEXT:
                        await target.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await target.send_bytes(message.data)
                    elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                        break

            tasks = [asyncio.create_task(forward(downstream, upstream)),
                     asyncio.create_task(forward(upstream, downstream))]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await downstream.close()
            return downstream


async def _proxy_http(request, port, query):
    """Relay one HTTP request, streaming the response body unchanged.

    ``auto_decompress`` stays off so a compressed body keeps the
    ``Content-Encoding`` it arrives with; the body is re-framed chunked, so the
    upstream length is dropped rather than relayed.
    """
    body = await request.read() if request.method not in {'GET', 'HEAD'} else None
    timeout = ClientTimeout(total=None, sock_connect=CONNECT_TIMEOUT)
    async with ClientSession(timeout=timeout, trust_env=False, auto_decompress=False) as client:
        async with client.request(request.method, f'http://127.0.0.1:{port}{request.path}',
                                  params=query, data=body,
                                  headers=_relay_headers(request), **NO_REDIRECTS) as upstream:
            response = web.StreamResponse(status=upstream.status, reason=upstream.reason)
            for key, value in upstream.headers.items():
                if key.lower() in HOP_BY_HOP:
                    continue
                response.headers.add(key, value)
            await response.prepare(request)
            if upstream.status not in {204, 304}:
                async for chunk in upstream.content.iter_chunked(65536):
                    await response.write(chunk)
            await response.write_eof()
            return response


def build_app(*, port_of):
    """Build the listener.

    ``port_of(binding)`` returns the loopback port of a **live** session, or
    ``None`` when the binding names nothing this scene currently serves. The
    listener forwards the prefixed path unchanged: the session runtime strips
    its own base path, so the dispatcher must not.
    """

    async def relay(request):
        binding = resolve_binding(request, port_of)
        if binding is None:
            return web.json_response({'code': 'unknown_session'}, status=404)
        port = port_of(binding)
        if port is None:  # closed between resolution and use
            return web.json_response({'code': 'unknown_session'}, status=404)
        query = list(request.query.items())
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            return await _proxy_websocket(request, port, query)
        return await _proxy_http(request, port, query)

    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', relay)
    return app


def session_port(runtime):
    """The loopback port of a live runtime, or ``None``.

    A registry entry is reachable only once its host has bound a port; a pending
    allocation, a closed runtime, or one still starting must not be advertised.
    """
    port = getattr(runtime, 'port', None)
    if not isinstance(port, int) or port <= 0 or getattr(runtime, 'closed', True):
        return None
    return port


def port_lookup(runtimes):
    """``port_of`` over the gateway's live runtime registry."""
    def port_of(binding):
        return session_port(runtimes.get(binding))
    return port_of
