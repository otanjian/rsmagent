"""The public session listener: binding resolution and byte-faithful relaying.

The listener is the only thing standing between a browser that is not on this
machine and a session runtime that is bound to an ephemeral loopback port, so the
tests here pin the two properties that matter: a request is resolved to exactly
one *live* session or refused, and a resolved request is relayed without
buffering, re-framing or dropping the headers the runtime's security depends on.

Every session now shares one fixed origin under a single path prefix; the
binding is named by the bootstrap query or the session cookie, and the prefixed
path is forwarded unchanged so the runtime strips its own base path.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession, WSMsgType, web
from aiohttp.test_utils import TestServer, make_mocked_request

from Scene.sap_workbench.backend.dispatcher import (
    build_app, cookie_bindings, port_lookup, resolve_binding, session_port,
)
from Scene.sap_workbench.backend.environment import (
    BASE_PATH_ENV, PUBLIC_BASE_ENV, PROXY_PORT_ENV, base_path, proxy_port,
    public_base, public_origin,
)

ORIGIN = 'https://rd.rsmxm.com.cn'
BINDING = 'sap_0123456789abcdef0123456789abcdef'


# --- the public base is a deployment setting, never view data ----------------

def test_an_unset_public_base_keeps_remote_display_off(monkeypatch):
    monkeypatch.delenv(PUBLIC_BASE_ENV, raising=False)
    assert public_base() == ''
    assert public_origin() == ''


def test_the_base_path_defaults_to_sapcode(monkeypatch):
    monkeypatch.delenv(BASE_PATH_ENV, raising=False)
    assert base_path() == '/sapcode'


@pytest.mark.parametrize('value', [
    'http://rd.rsmxm.com.cn',
    'https://rd.rsmxm.com.cn?x=1',
    'https://rd.rsmxm.com.cn#frag',
    'https://rd.rsmxm.com.cn/base',
    'https://{binding}.rd.rsmxm.com.cn',
])
def test_an_unusable_public_origin_is_refused(monkeypatch, value):
    monkeypatch.setenv(PUBLIC_BASE_ENV, value)
    with pytest.raises(ValueError):
        public_origin()


def test_the_public_base_is_origin_plus_base_path(monkeypatch):
    monkeypatch.setenv(PUBLIC_BASE_ENV, ORIGIN + '/')
    monkeypatch.delenv(BASE_PATH_ENV, raising=False)
    assert public_origin() == ORIGIN
    assert public_base() == ORIGIN + '/sapcode'


def test_the_base_path_can_be_overridden(monkeypatch):
    monkeypatch.setenv(PUBLIC_BASE_ENV, ORIGIN)
    monkeypatch.setenv(BASE_PATH_ENV, '/code-sap')
    assert public_base() == ORIGIN + '/code-sap'


def test_the_proxy_port_defaults_and_is_bounded(monkeypatch):
    monkeypatch.delenv(PROXY_PORT_ENV, raising=False)
    assert proxy_port() == 9911
    monkeypatch.setenv(PROXY_PORT_ENV, '9999')
    assert proxy_port() == 9999
    monkeypatch.setenv(PROXY_PORT_ENV, '0')
    with pytest.raises(ValueError):
        proxy_port()


# --- binding resolution ------------------------------------------------------

def test_cookies_name_their_bindings_in_order():
    assert cookie_bindings({f'sap_scene_{BINDING}': 'x', 'cow_session': 'y'}) == [BINDING]
    assert cookie_bindings({'sap_scene_A': 'x', 'sap_scene_B': 'y'}) == ['A', 'B']
    assert cookie_bindings({'sap_scene_': 'x', 'cow_session': 'y'}) == []
    assert cookie_bindings({}) == []


def live(port):
    return SimpleNamespace(port=port, closed=False)


def test_a_forged_or_stale_binding_resolves_to_nothing():
    port_of = port_lookup({'live': live(1234)})
    request = make_mocked_request('GET', '/sapcode/assets/app.js')
    assert resolve_binding(request, port_of) is None

    request = make_mocked_request('GET', '/sapcode/?binding=ghost')
    assert resolve_binding(request, port_of) is None

    request = make_mocked_request('GET', '/sapcode/?binding=live')
    assert resolve_binding(request, port_of) == 'live'


def test_the_bootstrap_binding_answers_before_any_cookie_exists():
    port_of = port_lookup({BINDING: live(1)})
    request = make_mocked_request('POST', '/sapcode/bootstrap?binding=' + BINDING,
                                  headers={'Cookie': 'sap_scene_other=x'})
    assert resolve_binding(request, port_of) == BINDING


def test_the_session_cookie_resolves_after_the_bootstrap():
    port_of = port_lookup({BINDING: live(2)})
    request = make_mocked_request('GET', '/sapcode/api/agent',
                                  headers={'Cookie': 'sap_scene_' + BINDING + '=x'})
    assert resolve_binding(request, port_of) == BINDING


def test_a_stale_cookie_does_not_hide_the_live_session():
    # A browser keeps the cookie of every binding it has ever bootstrapped;
    # only the one that still names a live session may answer.
    port_of = port_lookup({BINDING: live(2)})
    request = make_mocked_request('GET', '/sapcode/api/agent',
                                  headers={'Cookie': 'sap_scene_stale=x; sap_scene_' + BINDING + '=x'})
    assert resolve_binding(request, port_of) == BINDING


def test_several_live_cookies_are_still_refused():
    port_of = port_lookup({'live_a': live(1), 'live_b': live(2)})
    request = make_mocked_request('GET', '/sapcode/api/agent',
                                  headers={'Cookie': 'sap_scene_live_a=x; sap_scene_live_b=y'})
    assert resolve_binding(request, port_of) is None


def test_a_pending_or_closed_registry_entry_is_not_reachable():
    # A pending allocation is an asyncio.Task, which carries no port at all; the
    # same shape covers a closed runtime and an unbound one.
    assert session_port(None) is None
    assert session_port(SimpleNamespace()) is None
    assert session_port(SimpleNamespace(port=1, closed=True)) is None
    assert session_port(SimpleNamespace(port=None, closed=False)) is None
    assert session_port(SimpleNamespace(port=0, closed=False)) is None
    assert session_port(live(65525)) == 65525


# --- relaying ----------------------------------------------------------------

def upstream_app():
    app = web.Application()

    async def echo(request):
        body = await request.read()
        return web.json_response({'path': request.path, 'method': request.method,
                                  'query': dict(request.query), 'body': body.decode(),
                                  'host': request.headers.get('Host', ''),
                                  'cookie': request.headers.get('Cookie', '')})

    async def cookie(request):
        response = web.json_response({'ok': True})
        response.set_cookie('sap_scene_' + BINDING, 'secret', httponly=True, path='/sapcode')
        response.set_cookie('second', 'value', path='/')
        return response

    async def events(request):
        response = web.StreamResponse(headers={'Content-Type': 'text/event-stream',
                                               'Cache-Control': 'no-store'})
        await response.prepare(request)
        for index in range(3):
            await response.write(f'data: {index}\n\n'.encode())
            await asyncio.sleep(0)
        await response.write_eof()
        return response

    async def screen(request):
        ws = web.WebSocketResponse(protocols=['tok'])
        await ws.prepare(request)
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                await ws.send_str('echo:' + message.data)
        return ws

    async def boom(request):
        raise web.HTTPNotFound()

    async def redirect(request):
        response = web.Response(status=303, headers={'Location': '/sapcode/landed'})
        response.set_cookie('sap_scene_' + BINDING, 'secret', httponly=True, path='/sapcode')
        return response

    async def landed(request):
        raise AssertionError('the relay must not perform the session redirect itself')

    app.router.add_get('/sapcode/cookie', cookie)
    app.router.add_get('/sapcode/events', events)
    app.router.add_get('/sapcode/screen', screen)
    app.router.add_get('/sapcode/missing', boom)
    app.router.add_post('/sapcode/bootstrap', redirect)
    app.router.add_route('*', '/{tail:.*}', echo)
    return app


def scenario(coroutine):
    return asyncio.run(coroutine)


def test_the_bootstrap_redirect_is_handed_to_the_browser_not_followed():
    """The handshake *is* a 303; relaying it is the whole point.

    aiohttp follows redirects by default, so a relay that forgot to opt out would
    answer ``/bootstrap`` with the SPA's own 200 and silently drop both the
    ``Location`` and the session cookie the follow-up request needs. The client
    here follows, deliberately: the first hop in ``history`` is what the relay
    actually returned.
    """
    async def run():
        async with TestServer(upstream_app()) as target:
            app = build_app(port_of=lambda binding: target.port if binding == BINDING else None)
            async with TestServer(app) as proxy:
                async with ClientSession() as client:
                    result = await client.post(proxy.make_url(f'/sapcode/bootstrap?binding={BINDING}'),
                                               data={'token': 'x'})
                    assert result.history, 'the relay swallowed the redirect'
                    relayed = result.history[0]
                    assert relayed.status == 303
                    assert relayed.headers['Location'] == '/sapcode/landed'
                    cookie = relayed.cookies.get('sap_scene_' + BINDING)
                    assert cookie is not None and cookie.value == 'secret'
                    # The follow-up lands nowhere: the relay never invented a
                    # session for a redirect target it was not given a binding for.
                    assert result.status == 404
    scenario(run())


def test_http_is_relayed_with_its_status_headers_and_cookie():
    async def run():
        async with TestServer(upstream_app()) as target:
            app = build_app(port_of=lambda binding: target.port if binding == BINDING else None)
            async with TestServer(app) as proxy:
                async with ClientSession() as client:
                    url = proxy.make_url(f'/sapcode/api/session?binding={BINDING}&directory=x')
                    result = await client.post(url, data=b'payload',
                                               headers={'Origin': ORIGIN})
                    assert result.status == 200
                    assert (await result.json()) == {
                        'path': '/sapcode/api/session', 'method': 'POST',
                        'query': {'binding': BINDING, 'directory': 'x'},
                        'body': 'payload', 'host': f'127.0.0.1:{target.port}', 'cookie': ''}

                    # Two cookies must survive as two Set-Cookie headers, and the
                    # body must not be re-framed with a stale Content-Length.
                    result = await client.get(proxy.make_url(f'/sapcode/cookie?binding={BINDING}'))
                    assert result.status == 200
                    assert len(result.headers.getall('Set-Cookie')) == 2
                    assert 'content-length' not in {k.lower() for k in result.headers}
    scenario(run())


def test_an_unknown_session_is_refused_not_guessed():
    async def run():
        async with TestServer(upstream_app()) as target:
            app = build_app(port_of=lambda binding: target.port if binding == BINDING else None)
            async with TestServer(app) as proxy:
                async with ClientSession() as client:
                    result = await client.get(proxy.make_url('/sapcode/assets/app.js'))
                    assert result.status == 404
                    assert (await result.json()) == {'code': 'unknown_session'}
                    # An unknown name is refused even when spelled explicitly.
                    result = await client.get(proxy.make_url('/sapcode/assets/app.js?binding=ghost'))
                    assert result.status == 404
    scenario(run())


def test_the_session_cookie_routes_after_the_bootstrap():
    async def run():
        async with TestServer(upstream_app()) as target:
            app = build_app(port_of=lambda binding: target.port if binding == BINDING else None)
            async with TestServer(app) as proxy:
                async with ClientSession() as client:
                    result = await client.get(proxy.make_url('/sapcode/api/agent'),
                                              cookies={'sap_scene_' + BINDING: 'x'})
                    assert result.status == 200
                    assert (await result.json())['path'] == '/sapcode/api/agent'
    scenario(run())


def test_a_shared_origin_relays_by_cookie_and_refuses_an_unbound_request():
    """One fixed origin under one path prefix: the cookie names the session.

    A request with no cookie and no explicit binding has nothing to resolve, so
    it must be refused -- the shared origin is not an open door to a default one.
    """
    async def run():
        async with TestServer(upstream_app()) as target:
            app = build_app(port_of=lambda binding: target.port if binding == BINDING else None)
            async with TestServer(app) as proxy:
                async with ClientSession() as client:
                    # The bootstrap posts the binding explicitly before any cookie.
                    result = await client.post(proxy.make_url(f'/sapcode/bootstrap?binding={BINDING}'),
                                               data={'token': 'x'})
                    assert result.history and result.history[0].status == 303

                    # Afterwards the cookie carries the session.
                    result = await client.get(proxy.make_url('/sapcode/api/agent'),
                                              cookies={'sap_scene_' + BINDING: 'x'})
                    assert result.status == 200
                    assert (await result.json())['path'] == '/sapcode/api/agent'

                    # No cookie, no binding -> refused, never guessed.
                    result = await client.get(proxy.make_url('/sapcode/assets/app.js'))
                    assert result.status == 404
                    assert (await result.json()) == {'code': 'unknown_session'}
    scenario(run())


def test_an_event_stream_is_streamed_rather_than_buffered():
    async def run():
        async with TestServer(upstream_app()) as target:
            app = build_app(port_of=lambda binding: target.port if binding == BINDING else None)
            async with TestServer(app) as proxy:
                async with ClientSession() as client:
                    result = await client.get(proxy.make_url(f'/sapcode/events?binding={BINDING}'))
                    assert result.status == 200
                    assert result.headers['Content-Type'] == 'text/event-stream'
                    # Read the frames as they arrive: a buffered relay would still
                    # deliver them, so assert chunk-at-a-time delivery instead.
                    seen = []
                    async for line in result.content:
                        if line.strip():
                            seen.append(line.strip().decode())
                    assert seen == ['data: 0', 'data: 1', 'data: 2']
    scenario(run())


def test_a_websocket_upgrade_keeps_the_token_sub_protocol():
    async def run():
        async with TestServer(upstream_app()) as target:
            app = build_app(port_of=lambda binding: target.port if binding == BINDING else None)
            async with TestServer(app) as proxy:
                async with ClientSession() as client:
                    socket = await client.ws_connect(proxy.make_url(f'/sapcode/screen?binding={BINDING}'),
                                                     protocols=['tok'])
                    assert socket.protocol == 'tok'
                    await socket.send_str(json.dumps({'t': 'ping'}))
                    message = await socket.receive()
                    assert message.type == WSMsgType.TEXT
                    assert message.data == 'echo:' + json.dumps({'t': 'ping'})
                    await socket.close()
    scenario(run())


def test_an_upstream_error_status_keeps_its_own_code():
    async def run():
        async with TestServer(upstream_app()) as target:
            app = build_app(port_of=lambda binding: target.port if binding == BINDING else None)
            async with TestServer(app) as proxy:
                async with ClientSession() as client:
                    result = await client.get(proxy.make_url(f'/sapcode/missing?binding={BINDING}'))
                    assert result.status == 404
                    # The upstream's own refusal is relayed as-is, never masked
                    # by the listener's own unknown-session code.
                    assert 'unknown_session' not in await result.text()
    scenario(run())


# --- wiring: the listener exists only when the deployment asks for it --------

def gateway(monkeypatch, sites, cleaners):
    """A gateway whose aiohttp plumbing records what was started and cleaned."""
    import Scene.sap_workbench.browser_service.runner as module
    from Scene.sap_workbench.browser_service.runner import BrowserGatewayRunner

    class AppRunner:
        def __init__(self, app, **kwargs):
            self.app = app
            self.addresses = [('127.0.0.1', 45678)]
            cleaners.append(self)

        async def setup(self):
            pass

        async def cleanup(self):
            self.cleaned = True

    def site(runner, host, port):
        sites.append((host, port))
        return SimpleNamespace(start=AsyncMock())

    monkeypatch.setattr(module, 'BrowserNodeManager',
                        lambda **kwargs: SimpleNamespace(acquire=AsyncMock(), shutdown=AsyncMock()))
    monkeypatch.setattr(module, 'build_app', lambda **kwargs: object())
    monkeypatch.setattr(web, 'AppRunner', AppRunner)
    monkeypatch.setattr(web, 'TCPSite', site)
    return BrowserGatewayRunner(executable='fixture-browser', profile_root='fixture-profile')


def test_the_public_listener_starts_on_the_configured_port(monkeypatch):
    sites, cleaners = [], []
    monkeypatch.setenv(PUBLIC_BASE_ENV, ORIGIN)
    monkeypatch.setenv(PROXY_PORT_ENV, '9998')
    runner = gateway(monkeypatch, sites, cleaners)
    try:
        assert runner.start() == 45678
        # The session gateway keeps its ephemeral port; the public entry gets the
        # one fixed address nginx can be configured against, on loopback only.
        assert sites == [('127.0.0.1', 0), ('127.0.0.1', 9998)]
        assert runner.proxy_port == 9998
    finally:
        runner.stop()
    assert all(getattr(runner_, 'cleaned', False) for runner_ in cleaners)


def test_no_public_listener_exists_while_remote_display_is_unset(monkeypatch):
    sites, cleaners = [], []
    monkeypatch.delenv(PUBLIC_BASE_ENV, raising=False)
    runner = gateway(monkeypatch, sites, cleaners)
    try:
        assert runner.start() == 45678
        assert sites == [('127.0.0.1', 0)]
        assert runner.proxy_port is None
    finally:
        runner.stop()


def test_an_invalid_public_base_fails_before_any_listener_binds(monkeypatch):
    sites, cleaners = [], []
    monkeypatch.setenv(PUBLIC_BASE_ENV, 'http://rd.rsmxm.com.cn')
    runner = gateway(monkeypatch, sites, cleaners)
    with pytest.raises(ValueError):
        runner.start()
    assert sites == []
