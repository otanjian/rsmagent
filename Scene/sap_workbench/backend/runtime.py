"""Scene-owned session host and access boundary for the native OpenCode Web UI."""
import asyncio
import base64
from contextlib import suppress
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit

from aiohttp import web, ClientSession, ClientTimeout, WSMsgType

from .configuration import WorkbenchError
from .model_relay import configured_model, relay
from ..browser_service.gateway import build_app
from ..browser_service.page import PageController, model_observation
from ..browser_service.tokens import ViewTokens
from .native_events import NativeEvents
from .store import ALLOCATION_ERRORS

HOST_START_TIMEOUT = 70
SHUTDOWN_TOOL_TIMEOUT = 2
SHUTDOWN_COMPONENT_TIMEOUT = 42
SHUTDOWN_CHILD_KILL_TIMEOUT = 2
COMMIT_ACTIONS = frozenset({'commit_prepare', 'commit_execute', 'commit_status', 'commit_reconcile'})
READ_ACTIONS = frozenset({'read', 'mcp_read', 'purchase_order_read'})
BACKEND_READ_ACTIONS = frozenset({'mcp_read', 'purchase_order_read'})
BRIDGE_ACTIONS = READ_ACTIONS | frozenset({'navigate', 'fill', 'interact', 'scroll', 'transaction_open'}) | COMMIT_ACTIONS
BRIDGE_KEYS = frozenset({'service_id', 'session_id', 'message_id', 'call_id', 'action', 'input'})


async def bridge_body(request, *, action_required):
    """Validate IPC framing before touching control, action records or meters."""
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        raise WorkbenchError('invalid_request', 400) from None
    if not isinstance(body, dict) or set(body) - BRIDGE_KEYS:
        raise WorkbenchError('invalid_request', 400)
    for key in ('service_id', 'session_id', 'call_id'):
        value = body.get(key)
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise WorkbenchError('invalid_request', 400)
    if 'message_id' in body and (not isinstance(body['message_id'], str) or not 1 <= len(body['message_id']) <= 128):
        raise WorkbenchError('invalid_request', 400)
    if action_required or 'action' in body:
        action = body.get('action')
        if not isinstance(action, str) or action not in BRIDGE_ACTIONS:
            raise WorkbenchError('invalid_request', 400)
    if action_required or 'input' in body:
        if not isinstance(body.get('input'), dict):
            raise WorkbenchError('invalid_request', 400)
    return body


def sanitize(value):
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()
                if k.lower() not in {"apikey", "password", "authorization", "token", "secret"}}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    return value


async def sanitized_events(content, adapter=None):
    """Redact JSON event data without assuming TCP chunks align with events."""
    pending = bytearray()
    async for chunk in content.iter_any():
        pending.extend(chunk)
        if len(pending) > 2 * 1024 * 1024:
            raise WorkbenchError('event_too_large', 502)
        while b'\n' in pending:
            line, _, tail = pending.partition(b'\n')
            pending = bytearray(tail)
            if line.startswith(b'data:'):
                raw = line[5:].strip()
                if raw != b'[DONE]':
                    try:
                        value = sanitize(json.loads(raw))
                        values = adapter(value) if adapter else [value]
                        line = b'\n\n'.join(b'data: ' + json.dumps(item, ensure_ascii=False).encode() for item in values)
                    except (ValueError, UnicodeError):
                        raise WorkbenchError('invalid_event', 502) from None
            yield bytes(line) + b'\n'
    if pending.strip():
        raise WorkbenchError('incomplete_event', 502)


def permitted_path(method, path, remote, *, native=False):
    """Native instance APIs, or the legacy scene's restricted session surface."""
    if native:
        # The owner-authenticated private instance supplies its own native
        # configuration and permissions. Do not narrow its tools or APIs.
        return method in {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'} and bool(
            path.startswith(('/api/', '/global/')) or re.match(
                r'^/(?:agent|model|provider|config|command|skill|mcp|permission|question|event|path|vcs|project|session|file|pty|lsp|experimental|instance|reference|worktree)(?:/|$)', path))
    match = re.match(r"^/(?:api/)?session/([^/]+)", path)
    if match and match[1] not in {remote, "active", "status"}:
        return False
    if method == "POST":
        return path in {f"/api/session/{remote}/prompt", f"/api/session/{remote}/interrupt"}
    if method != "GET":
        return False
    return bool(re.fullmatch(r"/(?:api/)?(?:session(?:/[^/]+(?:/(?:context|message|todo|diff|children|status|event))?)?|agent|model|provider|config|command|skill|mcp|permission|question|event|path|vcs|project(?:/current)?)", path)
                or path in {"/global/health", "/global/config", "/global/event", "/api/event/subscribe", '/api/model/default', '/api/mcp/resource', '/api/reference', '/api/permission/request', '/api/question/request'})


def native_metadata(path, result, *, restricted=True):
    """Adapt metadata differences in the pinned Web/CLI build, not execution."""
    result = sanitize(result)
    if path == '/api/agent' and isinstance(result, dict):
        if restricted:
            result['data'] = [agent for agent in result.get('data', []) if agent.get('id') == 'sap']
        for agent in result['data']:
            request = agent.setdefault('request', {})
            request.setdefault('settings', request.get('body', {}))
    if restricted and path == '/api/provider' and isinstance(result, dict):
        result['data'] = [provider for provider in result.get('data', []) if provider.get('id') == 'sap']
    if path == '/api/model' and isinstance(result, dict):
        if restricted:
            result['data'] = [model for model in result.get('data', []) if model.get('providerID') == 'sap']
        for model in result['data']:
            model['modelID'] = model.get('api', {}).get('id', model['id'])
            model['package'] = model.get('api', {}).get('package', '@ai-sdk/openai-compatible')
            model.setdefault('time', {'released': 0})
    return result


def native_prompt(body):
    if not isinstance(body, dict):
        raise WorkbenchError('invalid_prompt')
    if 'text' in body:
        if set(body) - {'id', 'text', 'files', 'agents', 'delivery', 'resume'}:
            raise WorkbenchError('invalid_prompt')
        prompt = {key: body[key] for key in ('text', 'files', 'agents') if key in body}
        body = {key: body[key] for key in ('id', 'delivery', 'resume') if key in body}
        body['prompt'] = prompt
    if set(body) - {'prompt', 'id', 'delivery', 'resume'}:
        raise WorkbenchError('invalid_prompt')
    prompt = body.get('prompt')
    if (not isinstance(prompt, dict) or set(prompt) - {'text', 'files', 'agents'}
            or not isinstance(prompt.get('text'), str) or len(prompt['text']) > 16000
            or prompt.get('files') or prompt.get('agents')):
        raise WorkbenchError('text_prompt_required')
    return {**body, 'prompt': {'text': prompt['text']}}


class WorkbenchRuntime:
    def __init__(self, gateway, store, row, token, origin):
        self.gateway, self.store, self.row = gateway, store, row
        self.tenant, self.user = row["tenant_id"], row["user_id"]
        self.id, self.remote = row["id"], row["remote_session_id"]
        self.config = row["snapshot"]["config"]
        self.display_mode = row.get('display_mode', 'screen')
        self.project = row["snapshot"]["project"]
        self.token, self.origin = token, origin  # platform token stays server side
        self.secret = secrets.token_urlsafe(32)
        self.cookie = "sap_scene_" + self.id
        self.browser_secret = secrets.token_urlsafe(32)
        self.boot = ViewTokens(ttl=60)
        self.views = ViewTokens(ttl=60)
        self.client = None
        self.process = None
        self._spawn_task = None
        self._owned_children = []
        self.runner = None
        self.port = None
        self.public_origin = None
        self.upstream = None
        self.controller = None
        self.lease = None
        self.attach_lock = asyncio.Lock()
        self.tasks = {}
        from .navigation import IframeNavigation
        self.navigation = IframeNavigation(self)
        self.last_active = time.monotonic()
        self.closed = False
        self._close_task = None
        self.guard = None
        self.log = None
        self.output_task = None
        # Only scene deployment code may install a validated executor and a
        # complete independent SAP business verifier. Configuration/model
        # parameters cannot supply callbacks or turn a page receipt into proof.
        # The current deployment deliberately has no submission adapter.
        self.submission_adapter = None
        from .mcp_runtime import ConfiguredMcp
        self.mcp = ConfiguredMcp(self)

    async def authorize(self):
        from auth.runtime import resolve_context, IdentityContextError
        from auth.service import get_identity_service
        from .http import runtime_coding
        if self.closed:
            raise WorkbenchError("session_closed", 410)
        if self.store.session(self.tenant, self.user, self.id)['state'] == 'closed':
            raise WorkbenchError('session_closed', 410)
        try:
            ctx = resolve_context(get_identity_service(), self.token, self.tenant)
            if ctx.user_id != self.user or ctx.must_change_password:
                raise WorkbenchError("session_forbidden", 403)
            runtime_coding(ctx, self.row["agent_id"])
        except IdentityContextError as error:
            code = 'platform_login_required' if error.status == 401 else 'session_forbidden'
            raise WorkbenchError(code, error.status) from None
        current = self.store.read_config(self.tenant)
        if not current["config"]["enabled"]:
            raise WorkbenchError("disabled", 403)
        if current["version"] != self.row["config_version"]:
            # Configuration and credentials never silently retarget a live
            # binding. Its conversation is preserved and can be closed safely.
            if self.controller:
                self.controller.pause()
            raise WorkbenchError("config_conflict", 409)
        return ctx

    def audit(self, action, details):
        from auth.service import get_identity_service
        get_identity_service().record_business_audit(actor_user_id=self.user, tenant_id=self.tenant,
            action="sap_workbench." + action, target=self.id, redacted_changes=details)

    async def start(self):
        from config import get_data_root
        from .environment import runtime_paths, require_local_runtime
        await self.authorize()
        self.allocation('host_starting', state='creating')
        paths = runtime_paths(self.project)
        require_local_runtime(paths, self.config['browser_service_ref'], require_browser=self.display_mode != 'iframe')
        root = Path(__file__).resolve().parents[3]
        runtime_root = Path(get_data_root()) / "scenes/sap_workbench_runtime" / self.id
        runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.assets = paths.assets
        self.client = ClientSession(timeout=ClientTimeout(total=30), trust_env=False)
        if self.display_mode != 'iframe':
            self.model = await configured_model(self.client)
        self.store.recover_actions(self.id)
        app = build_app(tokens=self.views, node_factory=self.attach)
        app.middlewares.append(self.boundary)
        app.router.add_post('/bootstrap', self.bootstrap)
        app.router.add_post('/bridge/call', self.bridge)
        app.router.add_post('/bridge/cancel', self.cancel)
        if self.display_mode != 'iframe':
            app.router.add_post('/model/chat/completions', lambda request: relay(self, request))
        app.router.add_route('*', '/{tail:.*}', self.proxy)
        # This loopback IPC has explicit request deadlines and a session
        # heartbeat. Some macOS local transports reject SO_KEEPALIVE during
        # accept, preventing Bun tool calls from reaching any route.
        self.runner = web.AppRunner(app, shutdown_timeout=1, access_log=None, tcp_keepalive=False)
        await self.runner.setup()
        await web.TCPSite(self.runner, '127.0.0.1', 0).start()
        self.port = self.runner.addresses[0][1]
        hostname = urlsplit(self.origin).hostname
        if hostname not in {'localhost', '127.0.0.1'}:
            raise WorkbenchError('local_runtime_origin_required', 503)
        self.public_origin = f'http://{hostname}:{self.port}'
        # Only essential environment is inherited. Platform/SAP/provider
        # credentials are never inherited by the model execution process.
        env = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
        if self.display_mode == 'iframe':
            for key in ('OPENCODE_CONFIG', 'OPENCODE_CONFIG_DIR', 'OPENCODE_CONFIG_CONTENT',
                        'OPENCODE_DB', 'OPENCODE_DISABLE_CHANNEL_DB',
                        'OPENCODE_TEST_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME',
                        'XDG_STATE_HOME', 'XDG_CACHE_HOME'):
                if key in os.environ:
                    env[key] = os.environ[key]
        bun = paths.bun
        host = root / 'Scene/sap_workbench/opencode_adapter/server.ts'
        source = str(paths.source)
        self.log = open(runtime_root / 'host.log', 'ab')
        os.chmod(runtime_root / 'host.log', 0o600)
        self._spawn_task = asyncio.create_task(asyncio.create_subprocess_exec(bun, str(host), stdin=asyncio.subprocess.PIPE,
                       stdout=asyncio.subprocess.PIPE, stderr=self.log, env=env, cwd=source))
        self._spawn_task.add_done_callback(self._host_spawned)
        self.process = await asyncio.shield(self._spawn_task)
        setup = {"directory": str(runtime_root), "project": self.project, "root": source, "token": self.secret,
                 "modelURL": f'http://127.0.0.1:{self.port}/model', "model": getattr(self, 'model', {}).get('model'),
                 "bridgeURL": f'http://127.0.0.1:{self.port}/bridge/', "service": self.row["service_id"],
                 "displayMode": self.display_mode}
        self.process.stdin.write(json.dumps(setup).encode())
        await self.process.stdin.drain()
        self.process.stdin.close()
        async with asyncio.timeout(45):
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    raise WorkbenchError("opencode_host_failed", 503)
                try:
                    message = json.loads(line)
                    if isinstance(message.get('port'), int):
                        self.upstream = f'http://127.0.0.1:{message["port"]}'
                        break
                except (ValueError, AttributeError):
                    pass
        async def drain_output():
            while await self.process.stdout.read(8192):
                pass
        self.output_task = asyncio.create_task(drain_output())
        self.guard = asyncio.create_task(self.monitor())
        async with asyncio.timeout(15):
            while True:
                agents = await self.api('/api/agent?' + urlencode({'location[directory]': self.project}))
                if (agents.get('data') if self.display_mode == 'iframe'
                        else any(a['id'] == 'sap' for a in agents.get('data', []))):
                    break
                await asyncio.sleep(0.1)
        await self.ensure_conversation()
        self.store.update_session(self.tenant, self.user, self.id, state='creating', control='manual',
                                  allocation_stage='conversation_ready', allocation_error='',
                                  generation=self.row['generation'] + 1)
        self.row['generation'] += 1
        self.audit('session.conversation_ready', {'generation': self.row['generation'], 'remote_session': self.remote})
        if self.display_mode == 'iframe':
            self.allocation('ready', state='ready')
        return self

    def allocation(self, stage, *, state, error=''):
        self.store.update_session(self.tenant, self.user, self.id, allocation_stage=stage,
                                  allocation_error=error, state=state, control='manual')

    async def ensure_conversation(self):
        # A timeout is not proof of absence. Only a definitive 404 permits
        # creation with the previously reserved stable ID; retries reconcile it.
        result = await self.api('/api/session/' + self.remote, missing_ok=True)
        if result is None:
            body = {"id": self.remote, "location": {"directory": self.project}}
            if self.display_mode != 'iframe':
                body.update(agent='sap', model={"providerID": "sap", "id": self.model['model']})
            await self.api('/api/session', body)
            result = await self.api('/api/session/' + self.remote)
        info = result.get('data') if isinstance(result, dict) else None
        location = info.get('location') if isinstance(info, dict) else None
        if (not isinstance(info, dict) or info.get('id') != self.remote
                or (self.display_mode != 'iframe' and info.get('agent') != 'sap') or not isinstance(location, dict)
                or location.get('directory') != self.project):
            raise WorkbenchError('opencode_session_mismatch', 409)
        if self.display_mode == 'iframe':
            # Replace only the old scene-forced selections. Native user choices
            # and all durable messages stay in the same database/session.
            query = '?' + urlencode({'location[directory]': self.project})
            native_config = None
            if info.get('agent') == 'sap':
                agents = await self.api('/api/agent' + query)
                if not any(agent['id'] == 'sap' for agent in agents['data']):
                    native_config = await self.api('/config' + '?' + urlencode({'directory': self.project}))
                    await self.api('/api/session/' + self.remote + '/agent',
                                   {'agent': native_config.get('default_agent') or 'build'})
            if (info.get('model') or {}).get('providerID') == 'sap':
                models = await self.api('/api/model' + query)
                if not any(model.get('providerID') == 'sap' and model.get('id') == info['model'].get('id')
                           for model in models['data']):
                    native_config = native_config or await self.api('/config' + '?' + urlencode({'directory': self.project}))
                    provider, separator, model = native_config.get('model', '').partition('/')
                    if not separator and not models['data']:
                        raise WorkbenchError('model_configuration_unsupported', 502)
                    selected = {'providerID': provider, 'id': model} if separator else {
                        'providerID': models['data'][0]['providerID'], 'id': models['data'][0]['id']}
                    await self.api('/api/session/' + self.remote + '/model', {'model': selected})

    def host_headers(self):
        if self.display_mode == 'iframe':
            encoded = base64.b64encode(('opencode:' + self.secret).encode()).decode()
            return {'Authorization': 'Basic ' + encoded}
        return {'Authorization': 'Bearer ' + self.secret}

    async def api(self, path, body=None, *, missing_ok=False):
        async with self.client.request('GET' if body is None else 'POST', self.upstream + path,
                      json=body, headers=self.host_headers()) as result:
            if missing_ok and body is None and result.status == 404:
                return None
            if result.status >= 300:
                raise WorkbenchError('opencode_request_failed', 502)
            if result.status == 204:
                return None
            return await result.json()

    def projection(self):
        grant = self.boot.issue(self.tenant, self.user, origin=self.origin)
        if self.display_mode == 'iframe':
            return {'binding_id': self.id, 'remote_session_id': self.remote, 'port': self.port,
                    'bootstrap_token': grant, 'origin': self.public_origin, 'display_mode': 'iframe',
                    'sap_url': self.config['sap']['web_gui_url'], 'gui_automation': False, 'control': 'manual'}
        view = self.views.issue(self.tenant, self.user, origin=self.origin, url=self.config['sap']['web_gui_url'])
        return {'binding_id': self.id, 'remote_session_id': self.remote, 'port': self.port,
                'bootstrap_token': grant, 'token': view, 'origin': self.public_origin,
                'control': self.controller.control if self.controller else 'manual', 'readonly': False}

    @web.middleware
    async def boundary(self, request, handler):
        try:
            await self.authorize()
            if request.path.startswith(('/bridge/', '/model/')):
                if not hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + self.secret):
                    raise web.HTTPUnauthorized()
            elif request.path not in {'/bootstrap', '/screen'}:
                if not hmac.compare_digest(request.cookies.get(self.cookie, ''), self.browser_secret):
                    raise web.HTTPUnauthorized()
                if (request.method not in {'GET', 'HEAD'} or request.headers.get('Upgrade', '').lower() == 'websocket') and request.headers.get('Origin') != self.public_origin:
                    raise web.HTTPForbidden()
            response = await handler(request)
            response.headers['Referrer-Policy'] = 'no-referrer'
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['Content-Security-Policy'] = f"frame-ancestors {self.origin}"
            return response
        except WorkbenchError as error:
            return web.json_response({'code': error.code}, status=error.status)
        except web.HTTPException as error:
            return web.json_response({'code': 'scene_request_refused', 'message': error.reason}, status=error.status)

    async def bootstrap(self, request):
        if request.headers.get('Origin') != self.origin:
            raise web.HTTPForbidden()
        body = await request.post()
        token = body.get('token', '')
        if not self.boot.verify(token, tenant_id=self.tenant, user_id=self.user):
            raise web.HTTPUnauthorized()
        self.boot.revoke(token)
        directory = base64.urlsafe_b64encode(self.project.encode()).decode().rstrip('=')
        query = urlencode({'rsm_embed': '1', 'rsm_parent_origin': self.origin, 'rsm_channel': self.id})
        response = web.Response(status=303, headers={'Location': f'/{directory}/session/{self.remote}?{query}'})
        response.set_cookie(self.cookie, self.browser_secret, httponly=True, samesite='Lax', path='/')
        return response

    async def attach(self, claims):
        async with self.attach_lock:
            return await self._attach(claims)

    async def _attach(self, claims):
        await self.authorize()
        if self.display_mode == 'iframe':
            raise WorkbenchError('iframe_page_control_unavailable', 409)
        if self.lease and self.lease._node.attached:
            raise WorkbenchError('screen_already_attached', 409)
        identity = {**claims, 'user_id': self.user + ':' + self.id,
                    'max_screens': self.config['max_sessions'], 'max_width': 1280, 'max_height': 800}
        self.allocation('browser_starting', state='creating')
        try:
            lease = await self.gateway.manager.acquire(identity)
        except BaseException:
            self.allocation('conversation_ready', state='unavailable', error='browser_unavailable')
            raise
        if self.controller is None or self.controller.node is not lease._node:
            self.controller = PageController(lease._node, self.config['sap']['allowed_origins'], revalidate=self.authorize)
        self.controller.pause()
        self.lease = lease
        self.allocation('ready', state='ready')
        return BoundScreen(self, lease)

    async def set_control(self, mode):
        await self.authorize()
        self.last_active = time.monotonic()
        if self.display_mode == 'iframe':
            if mode != 'manual':
                raise WorkbenchError('iframe_page_control_unavailable', 409)
            self.store.update_session(self.tenant, self.user, self.id, control='manual')
            return {'control': 'manual'}
        if not self.controller or not self.lease._node.attached:
            raise WorkbenchError('browser_not_connected', 409)
        if mode not in {'manual', 'automatic'}:
            raise WorkbenchError('invalid_control')
        self.controller.pause()
        if mode == 'automatic':
            if not self.config['automation_enabled']:
                raise WorkbenchError('automation_disabled', 403)
            async with self.controller.lock:
                page = await self.controller.read()
                if page['login']:
                    raise WorkbenchError('sap_login_required', 409)
                self.controller.control = 'automatic'
        self.store.update_session(self.tenant, self.user, self.id, control=mode)
        self.audit('control.' + mode, {'generation': self.row['generation']})
        return {'control': mode}

    async def bridge(self, request):
        from auth.service import get_identity_service
        body = await bridge_body(request, action_required=True)
        if body.get('service_id') != self.row['service_id'] or body.get('session_id') != self.remote:
            raise web.HTTPForbidden()
        await self.authorize()
        if self.display_mode == 'iframe' and body['action'] not in BACKEND_READ_ACTIONS | {'transaction_open'}:
            raise WorkbenchError('iframe_page_control_unavailable', 409)
        if body['action'] == 'transaction_open' and self.display_mode != 'iframe':
            raise WorkbenchError('iframe_navigation_unavailable', 409)
        if body['action'] in COMMIT_ACTIONS:
            return await self.commit_bridge(body)
        if self.display_mode != 'iframe' and (not self.controller or not self.lease or not self.lease._node.attached):
            raise WorkbenchError('browser_not_connected', 409)
        action, arguments, call = body.get('action'), body.get('input'), body.get('call_id')
        if action == 'transaction_open':
            from .navigation import transaction_url
            if set(arguments) != {'transaction'}:
                raise WorkbenchError('invalid_request', 400)
            transaction_url(self.config['sap']['web_gui_url'], arguments['transaction'])
        if action == 'purchase_order_read' and (set(arguments) != {'document_number'} or
                not isinstance(arguments['document_number'], str) or
                not re.fullmatch(r'[0-9]{10}', arguments['document_number']) or
                not int(arguments['document_number'])):
            raise WorkbenchError('purchase_order_number_invalid', 400)
        self.last_active = time.monotonic()
        epoch = self.controller.epoch if self.controller else 0
        record = self.store.admit_action(self.id, call, self.row['generation'], action, arguments)
        try:
            try:
                svc = get_identity_service()
                admitted = svc.consume_quota(user_id=self.user, tenant_id=self.tenant, metric='tool_calls', amount=1)
            except Exception:
                raise WorkbenchError('tool_quota_unavailable', 503) from None
            if not admitted:
                raise web.HTTPTooManyRequests()
            await self.authorize()
            try:
                self.audit('action.dispatch', {'action_id': record, 'kind': action, 'generation': self.row['generation']})
            except Exception:
                raise WorkbenchError('audit_unavailable', 503) from None
        except BaseException:
            # Nothing has been dispatched yet. Keep an uncertain meter charge,
            # but never leave an unexecuted action recorded as running.
            self.store.finish_action(record, 'failed')
            raise
        if action == 'transaction_open':
            operation = self.navigation.open(arguments)
        elif action == 'purchase_order_read':
            from .purchase_order import PurchaseOrderReader
            operation = PurchaseOrderReader(self.mcp, self.config['sap']['client']).read(arguments['document_number'])
        elif action == 'mcp_read':
            operation = self.mcp.call(arguments)
        else:
            operation = self.controller.execute(action, arguments, epoch=epoch)
        task = asyncio.create_task(operation)
        self.tasks[call] = task
        try:
            result = await task
            await self.authorize()
            if action not in BACKEND_READ_ACTIONS and action != 'transaction_open':
                result = model_observation(result, arguments if action in {'read', 'fill'} else {})
            output = json.dumps(result, ensure_ascii=False, allow_nan=False)
            # Complete private table reads are never clipped into a partial
            # model document. Refuse an oversized assembled projection.
            if action == 'purchase_order_read' and len(output.encode('utf-8')) > 32000:
                raise WorkbenchError('purchase_order_result_too_large', 409)
            self.store.finish_action(record, 'succeeded')
            self.audit('action.succeeded', {'action_id': record, 'kind': action,
                       **({'effect': result['action_effect']['effect'], 'risk': result['action_effect']['risk']}
                          if action not in BACKEND_READ_ACTIONS and result.get('action_effect') else {})})
            return web.json_response({'output': output})
        except WorkbenchError:
            self.store.finish_action(record, 'unknown' if action not in READ_ACTIONS else 'failed')
            if self.controller:
                self.controller.pause()
            # These are scene-owned codes, never arbitrary SAP response text.
            # Preserve them so the model can distinguish a rejected value,
            # an expired page and manual takeover without retrying blindly.
            raise
        except (asyncio.CancelledError, Exception):
            self.store.finish_action(record, 'unknown' if action not in READ_ACTIONS else 'failed')
            if self.controller:
                self.controller.pause()
            raise WorkbenchError('action_stopped_check_page', 409) from None
        finally:
            self.tasks.pop(call, None)

    async def commit_bridge(self, body):
        """Formal approval entry point; no generic click/key or model approval.

        Status survives loss of the browser. Recovery is read-only and requires
        the registered verifier; it never invokes the submission callback.
        Until a deployment registers the full SAP verifier, even a true
        commit_enabled flag cannot issue a save or create a misleading request.
        """
        from .commit import CommitConsumer
        from auth.service import get_identity_service
        action, args, call = body['action'], body['input'], body['call_id']
        expected = {'revision'} if action == 'commit_prepare' else {'action_id'}
        maximum = 256 if action == 'commit_prepare' else 128
        if (set(args) != expected or any(not isinstance(value, str) or not 1 <= len(value) <= maximum
                                        for value in args.values())):
            raise WorkbenchError('invalid_request', 400)
        if call in self.tasks:
            raise WorkbenchError('action_already_dispatched', 409)
        adapter = self.submission_adapter
        if action != 'commit_status' and (
                adapter is None or not callable(getattr(adapter, 'verify', None))
                or (action in {'commit_prepare', 'commit_execute'} and not callable(getattr(adapter, 'dispatch', None)))):
            raise WorkbenchError('commit_executor_unavailable', 503)
        if action in {'commit_prepare', 'commit_execute'} and not self.config.get('commit_enabled'):
            raise WorkbenchError('commit_disabled', 403)
        consumer = CommitConsumer(self)
        locked_controller = None

        def current_controller():
            controller = locked_controller
            if (controller is None or self.controller is not controller or not self.lease
                    or self.lease._node is not controller.node or not controller.node.attached):
                raise WorkbenchError('control_changed', 409)
            return controller

        async def observe():
            await self.authorize()
            controller = current_controller()
            if controller.control != 'automatic':
                raise WorkbenchError('manual_control', 409)
            epoch = controller.epoch
            page = await controller.read()
            current_controller()
            await controller._check(epoch)
            if action == 'commit_prepare' and args['revision'] != page['revision']:
                raise WorkbenchError('stale_page', 409)
            # Ask the exact CDP connection used by this pane; never accept the
            # target id from the model or another Chrome process.
            info = await controller.node._cdp.call('Target.getTargetInfo')
            current_controller()
            target = info.get('targetInfo', {}) if isinstance(info, dict) else {}
            parsed = urlsplit(target.get('url', '') if isinstance(target.get('url'), str) else '')
            if (target.get('type') != 'page' or f'{parsed.scheme}://{parsed.netloc}' not in controller.origins
                    or not isinstance(target.get('targetId'), str) or not target['targetId']):
                raise WorkbenchError('commit_page_unsupported', 409)
            await controller._check(epoch)
            return {'page': page, 'target_id': target['targetId'], 'control_epoch': epoch}

        async def run():
            nonlocal locked_controller
            if action != 'commit_execute':
                try:
                    allowed = get_identity_service().consume_quota(user_id=self.user, tenant_id=self.tenant,
                                                                 metric='tool_calls', amount=1)
                except Exception:
                    raise WorkbenchError('tool_quota_unavailable', 503) from None
                if not allowed:
                    raise WorkbenchError('tool_quota_exhausted', 429)
            await self.authorize()
            self.last_active = time.monotonic()
            if action == 'commit_status':
                return await consumer.status(args['action_id'])
            if action == 'commit_reconcile':
                return await consumer.reconcile(args['action_id'], verify=adapter.verify)
            controller = self.controller
            if not controller:
                raise WorkbenchError('browser_not_connected', 409)
            locked_controller = controller
            async with controller.lock:
                current_controller()
                if action == 'commit_prepare':
                    return await consumer.prepare(call, observe=observe)
                return await consumer.execute(args['action_id'], observe=observe,
                                              dispatch=adapter.dispatch, verify=adapter.verify)

        task = asyncio.create_task(run())
        self.tasks[call] = task
        try:
            result = await task
            return web.json_response({'output': json.dumps(result, ensure_ascii=False)})
        except BaseException:
            if action == 'commit_execute' and self.controller:
                self.controller.pause()
            raise
        finally:
            self.tasks.pop(call, None)

    async def cancel(self, request):
        body = await bridge_body(request, action_required=False)
        if body.get('service_id') != self.row['service_id'] or body.get('session_id') != self.remote:
            raise web.HTTPForbidden()
        await self.authorize()
        if self.controller:
            self.controller.pause()
        task = self.tasks.get(body.get('call_id'))
        if task:
            task.cancel()
        return web.json_response({'ok': True})

    async def proxy(self, request):
        path = request.path
        native = self.display_mode == 'iframe'
        # The native UI must use the canonical Session V2 runner; the CLI also
        # exposes legacy health, which would incorrectly select V1 prompts.
        if path == '/global/health':
            raise web.HTTPNotFound()
        if native and path == '/api/model/default' and request.method == 'GET':
            # This pinned Web build asks for a default route absent in the CLI.
            # Resolve the native configuration; do not shrink its model catalog.
            directory = request.query.get('location[directory]', self.project)
            config = await self.api('/config?' + urlencode({'directory': directory}))
            provider, separator, model = (config.get('model') or '').partition('/')
            if separator:
                selected = {'providerID': provider, 'id': model}
            else:
                catalog = await self.api('/api/model?' + urlencode({'location[directory]': directory}))
                if not catalog.get('data'):
                    raise WorkbenchError('model_configuration_unsupported', 502)
                first = catalog['data'][0]
                selected = {'providerID': first['providerID'], 'id': first['id']}
            return web.json_response({'location': {'directory': directory}, 'data': selected})
        if not native and path == '/api/health' and request.method == 'GET':
            return web.json_response({'pid': self.process.pid})
        if not native and path == '/lsp' and request.method == 'GET':
            return web.json_response([])
        if not native and path == '/api/project/current' and request.method == 'GET':
            return web.json_response({'location': {'directory': self.project},
                                      'data': {'id': self.row['service_id'], 'directory': self.project,
                                               'worktree': self.project, 'time': {'created': 0, 'updated': 0}}})
        if not native and path in {'/api/model/default', '/api/mcp', '/api/mcp/resource'} and request.method == 'GET':
            location = {'directory': self.project, 'project': {'id': self.row['service_id'], 'directory': self.project}}
            if path == '/api/model/default':
                value = {'id': self.model['model'], 'providerID': 'sap'}
            elif path == '/api/mcp/resource':
                value = {'resources': [], 'templates': []}
            else:
                value = [{'name': entry['id'], 'status': {'status': 'connected' if self.mcp.ready else 'pending'}}
                         for entry in self.config['mcp']['connections'] if entry['enabled']]
            return web.json_response({'location': location, 'data': value})
        if permitted_path(request.method, path, self.remote, native=native):
            query = dict(request.query)
            for key in ('directory', 'location[directory]'):
                if not native and key in query:
                    query[key] = self.project
            if native and request.headers.get('Upgrade', '').lower() == 'websocket':
                return await self.proxy_websocket(request, query)
            body = None
            if native:
                body = await request.read()
                self.last_active = time.monotonic()
            elif request.method == 'POST':
                body = await request.json()
                if path.endswith('/prompt'):
                    body = native_prompt(body)
                    self.last_active = time.monotonic()
            headers = self.host_headers()
            for key in ('Content-Type', 'Accept'):
                if key in request.headers:
                    headers[key] = request.headers[key]
            upstream = await self.client.request(request.method, self.upstream + path, params=query,
                       **({'data': body} if native else {'json': body}), headers=headers,
                       timeout=ClientTimeout(total=None, sock_connect=5))
            try:
                if 'text/event-stream' in upstream.headers.get('Content-Type', ''):
                    response = web.StreamResponse(status=upstream.status, headers={'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store'})
                    await response.prepare(request)
                    async for chunk in sanitized_events(upstream.content, NativeEvents()):
                        await self.authorize()
                        await response.write(chunk)
                    return response
                if upstream.status == 204:
                    return web.Response(status=204)
                if native and 'json' not in upstream.headers.get('Content-Type', ''):
                    return web.Response(body=await upstream.read(), status=upstream.status,
                                        headers={'Content-Type': upstream.headers.get('Content-Type', 'application/octet-stream')})
                try:
                    result = native_metadata(path, await upstream.json(), restricted=not native)
                except (ValueError, Exception):
                    result = {'code': 'opencode_response_unavailable'}
                return web.json_response(result, status=upstream.status)
            finally:
                upstream.close()
        # Assets are immutable native OpenCode build output. SPA routes never
        # fall back for /api, files, terminal or unknown backend requests.
        if request.method == 'GET' and (path.startswith('/assets/') or path in {'/favicon.ico', '/favicon.svg', '/manifest.webmanifest'}):
            asset = (self.assets / path.lstrip('/')).resolve()
            if self.assets.resolve() not in asset.parents or not asset.is_file() or asset.suffix == '.map':
                raise web.HTTPNotFound()
            return web.FileResponse(asset)
        directory = base64.urlsafe_b64encode(self.project.encode()).decode().rstrip('=')
        spa_route = path in {'/', f'/{directory}/session/{self.remote}'}
        if native:
            spa_route = spa_route or bool(re.fullmatch(r'/[A-Za-z0-9_-]+/session(?:/[A-Za-z0-9_-]+)?', path))
        if request.method == 'GET' and spa_route:
            html = (self.assets / 'index.html').read_text(encoding='utf-8')
            # Scene-owned presentation only; the native OpenCode build stays
            # untouched. Hide exactly the compact session/changes tab bar.
            style = '<style>header:has(#opencode-titlebar-right),[data-slot="tabs-list"]:has([data-value="session"]):has([data-value="changes"]) {display:none!important}</style>'
            return web.Response(text=html.replace('</head>', style + '</head>'), content_type='text/html')
        from common.log import logger
        logger.info('[SapWorkbench] refused UI route %s %s', request.method, path[:180])
        raise web.HTTPForbidden()

    async def proxy_websocket(self, request, query):
        protocols = [item.strip() for item in request.headers.get('Sec-WebSocket-Protocol', '').split(',') if item.strip()]
        async with self.client.ws_connect(self.upstream + request.path, params=query,
                                         headers=self.host_headers(), protocols=protocols) as upstream:
            downstream = web.WebSocketResponse(protocols=protocols)
            await downstream.prepare(request)

            async def forward(source, target):
                async for message in source:
                    await self.authorize()
                    self.last_active = time.monotonic()
                    if message.type == WSMsgType.TEXT:
                        await target.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await target.send_bytes(message.data)
                    elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                        break

            tasks = [asyncio.create_task(forward(downstream, upstream)), asyncio.create_task(forward(upstream, downstream))]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await downstream.close()
            return downstream

    async def monitor(self):
        try:
            while True:
                await asyncio.sleep(2)
                await self.authorize()
                if time.monotonic() - self.last_active > self.config['idle_seconds']:
                    break
                if self.process and self.process.returncode is not None:
                    break
        except Exception:
            pass
        await self.close()

    async def close(self):
        if self._close_task is None:
            self.closed = True
            self._close_task = asyncio.create_task(self._cleanup())
        # A disconnected caller cannot interrupt resource cleanup. Concurrent
        # callers wait for the same cleanup, including a resume request.
        await asyncio.shield(self._close_task)

    def _host_spawned(self, task):
        if task.cancelled():
            return
        try:
            process = task.result()
        except BaseException:
            return
        self.process = process
        self._owned_children.append(process)
        self.owned_children()
        # The spawn belongs to this runtime even if its HTTP waiter or
        # allocation was cancelled before create_subprocess_exec returned.
        if self.closed:
            with suppress(ProcessLookupError, OSError):
                process.kill()

    def owned_children(self):
        """Exact child handles only; never discover processes by PID/profile."""
        for process in (self.process, getattr(self.mcp, 'process', None)):
            if process is not None and not any(process is old for old in self._owned_children):
                self._owned_children.append(process)
        owner = getattr(self.gateway, '_remember_child', None)
        if owner is not None:
            for process in self._owned_children:
                owner(process)
        return list(self._owned_children)

    def kill_owned_children(self):
        from common.log import logger
        for process in self.owned_children():
            if getattr(process, 'returncode', None) is None:
                try:
                    process.kill()
                except Exception:
                    logger.warning('[SapWorkbench] shutdown component=child code=kill_failed')

    async def _close_host(self):
        if self._spawn_task is not None and not self._spawn_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._spawn_task), SHUTDOWN_CHILD_KILL_TIMEOUT)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # The done callback keeps the eventual child owned and kills
                # it if it is delivered after this runtime became closed.
                return
        process = self.process
        if process is None or process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), SHUTDOWN_CHILD_KILL_TIMEOUT)

    async def _cleanup(self):
        from common.log import logger
        self.navigation.close()
        self.owned_children()
        if self.controller:
            with suppress(Exception):
                self.controller.pause()
        pending = list(self.tasks.values())
        for task in pending:
            task.cancel()
        if self.guard and self.guard is not asyncio.current_task():
            self.guard.cancel()
        if pending:
            await asyncio.wait(pending, timeout=SHUTDOWN_TOOL_TIMEOUT)
        # Persist uncertainty before waiting on external components. Shutdown
        # does not promise that an already delivered SAP action was undone.
        for operation in (lambda: self.allocation('paused', state='paused'),
                          lambda: self.store.recover_actions(self.id)):
            try:
                operation()
            except Exception:
                logger.warning('[SapWorkbench] shutdown component=ledger code=unavailable')

        async def clean(component, callback):
            try:
                await callback()
            except BaseException:
                logger.warning('[SapWorkbench] shutdown component=%s code=cleanup_failed', component)

        callbacks = [('host', self._close_host), ('mcp', self.mcp.close)]
        if self.lease:
            callbacks.append(('browser', self.lease.close))
        if self.runner:
            callbacks.append(('http', self.runner.cleanup))
        if self.client:
            callbacks.append(('client', self.client.close))
        components = [asyncio.create_task(clean(name, callback)) for name, callback in callbacks]
        _, unfinished = await asyncio.wait(components, timeout=SHUTDOWN_COMPONENT_TIMEOUT)
        if unfinished:
            logger.warning('[SapWorkbench] shutdown component=runtime code=cleanup_timeout')
            self.kill_owned_children()
            for task in unfinished:
                task.cancel()
            await asyncio.wait(unfinished, timeout=SHUTDOWN_CHILD_KILL_TIMEOUT)
        # A component can fail quickly before killing its process, too. Close
        # must not release the runtime registry while an owned child survives.
        self.kill_owned_children()
        async def reap(process):
            try:
                await process.wait()
            except Exception:
                logger.warning('[SapWorkbench] shutdown component=child code=reap_failed')
        reapers = [asyncio.create_task(reap(process)) for process in self.owned_children()
                   if getattr(process, 'returncode', None) is None]
        if reapers:
            _, unfinished = await asyncio.wait(reapers, timeout=SHUTDOWN_CHILD_KILL_TIMEOUT)
            for task in unfinished:
                task.cancel()
        if self.output_task and not self.output_task.done():
            self.output_task.cancel()
        if self.log:
            with suppress(Exception):
                self.log.close()
        with suppress(Exception):
            self.store.recover_actions(self.id)


class BoundScreen:
    def __init__(self, runtime, lease):
        self.runtime, self.lease = runtime, lease

    async def frames(self):
        async for frame in self.lease.frames():
            await self.runtime.authorize()
            yield frame

    async def view_state(self):
        await self.runtime.authorize()
        controller = self.runtime.controller
        return {'control': controller.control, 'epoch': controller.epoch,
                'login_required': controller.login_required}

    async def resize(self, width, height):
        await self.runtime.authorize()
        if self.lease.viewport == {'width': width, 'height': height}:
            return
        # Layout changes invalidate previously observed action coordinates.
        controller = self.runtime.controller
        controller.pause()
        async with controller.lock:
            await self.lease.resize(width, height)

    async def send(self, commands):
        await self.runtime.authorize()
        # Hovering is not a takeover; clicks, keys and scrolling are.
        controller = self.runtime.controller
        meaningful = any(command.get('params', {}).get('type') != 'mouseMoved' for command in commands)
        if meaningful:
            controller.pause()
        async with controller.lock:
            await self.lease.send(commands)
        if meaningful:
            self.runtime.last_active = time.monotonic()

    async def detach(self):
        self.runtime.controller.pause()
        try:
            await self.lease.detach()
        finally:
            if not self.runtime.closed and self.runtime.lease is self.lease:
                self.runtime.allocation('conversation_ready', state='paused')


async def open_runtime(gateway, store, row, token, origin):
    if row.get('state') == 'closed':
        raise WorkbenchError('session_closed', 410)
    # All creation runs on the gateway loop; the placeholder Task makes
    # concurrent retries converge on the same allocation and external ID.
    existing = gateway.runtimes.get(row['id'])
    if isinstance(existing, asyncio.Task):
        runtime = await asyncio.shield(existing)
    elif existing and not existing.closed:
        runtime = existing
    elif existing:
        await existing.close()
        if gateway.runtimes.get(row['id']) is existing:
            gateway.runtimes.pop(row['id'], None)
        return await open_runtime(gateway, store, row, token, origin)
    else:
        # Count both pending and live hosts before any process is allocated.
        # This runs on one event loop and inserts the reservation without await.
        active = [value for value in gateway.runtimes.values()
                  if isinstance(value, asyncio.Task) or not value.closed
                  or (getattr(value, '_close_task', None) is not None and not value._close_task.done())]
        tenant_active = sum(1 for value in active if
            getattr(value, 'sap_tenant', getattr(value, 'tenant', None)) == row['tenant_id'])
        if len(active) >= 8 or tenant_active >= row['snapshot']['config']['max_sessions']:
            raise WorkbenchError('browser_capacity_exhausted', 429)
        async def create():
            current = store.session(row['tenant_id'], row['user_id'], row['id'])
            instance = WorkbenchRuntime(gateway, store, current, token, origin)
            asyncio.current_task().sap_runtime = instance
            try:
                # Bound the whole startup, including model configuration and
                # stdin drain; per-request/socket timeouts alone are not enough.
                try:
                    async with asyncio.timeout(HOST_START_TIMEOUT):
                        await instance.start()
                except asyncio.TimeoutError:
                    raise WorkbenchError('opencode_start_timeout', 503) from None
                # The allocation owns its registry entry. A client timing out
                # or cancelling its wait must not orphan this running host.
                if gateway.runtimes.get(row['id']) is asyncio.current_task():
                    gateway.runtimes[row['id']] = instance
                return instance
            except BaseException as error:
                phase = store.session(row['tenant_id'], row['user_id'], row['id'])['allocation_stage']
                try:
                    await instance.close()
                finally:
                    if not isinstance(error, asyncio.CancelledError):
                        code = error.code if isinstance(error, WorkbenchError) and error.code in ALLOCATION_ERRORS else 'runtime_unavailable'
                        instance.allocation(phase, state='unavailable', error=code)
                    if gateway.runtimes.get(row['id']) is asyncio.current_task():
                        gateway.runtimes.pop(row['id'], None)
                raise
        pending = asyncio.create_task(create())
        pending.sap_tenant = row['tenant_id']
        gateway.runtimes[row['id']] = pending
        # Consume a failure even if every HTTP waiter disconnected. Awaiters
        # still receive the exception normally; no background host is hidden.
        pending.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        runtime = await asyncio.shield(pending)
    # Only the same owner reaches here after the normal authenticated API.
    runtime.token, runtime.origin = token, origin
    await runtime.authorize()
    return runtime.projection()


async def close_runtime(gateway, store, row, *, terminal=False):
    """Owner-checked HTTP cleanup stays available after config changes/disable."""
    instance = gateway.runtimes.get(row['id'])
    if isinstance(instance, asyncio.Task):
        instance.cancel()
        await asyncio.gather(instance, return_exceptions=True)
    elif instance:
        await instance.close()
    if gateway.runtimes.get(row['id']) is instance:
        gateway.runtimes.pop(row['id'], None)
    store.update_session(row['tenant_id'], row['user_id'], row['id'], state='closed' if terminal or row['state'] == 'closed' else 'paused', control='manual',
                         allocation_stage='paused', allocation_error='')
    store.recover_actions(row['id'])
    return {'closed': True}


async def open_workbench_runtime(gateway, store, tenant, user, coding, saved, token, origin,
                                 *, request_id=None, row=None):
    """Replace only this owner's old hosts before reserving runtime capacity.

    The owner lock serializes distinct creations/resumes. Shield the whole
    replacement so HTTP cancellation cannot leave half-cleaned old hosts.
    Same-request retries join one task; a superseded request cannot reopen
    its closed binding or close a later conversation.
    """
    from weakref import WeakValueDictionary
    if not hasattr(gateway, 'session_locks'):
        gateway.session_locks = WeakValueDictionary()
        gateway.session_requests = {}
    owner = (str(store.path.resolve()), tenant, user)
    key = (*owner, 'resume', row['id']) if row else (*owner, coding['id'], request_id)
    pending = gateway.session_requests.get(key)
    if pending is None:
        lock = gateway.session_locks.setdefault(owner, asyncio.Lock())

        async def replace():
            async with lock:
                current = store.session(tenant, user, row['id']) if row else store.reserve_session(
                    tenant, user, coding['id'], request_id, saved, coding['project_dir'], display_mode='iframe')
                if current['state'] == 'closed':
                    raise WorkbenchError('session_closed', 410)
                others = [old for old in store.active_sessions(tenant, user) if old['id'] != current['id']]
                # Begin every old host's idempotent cleanup together. Capacity
                # for the new host is checked only after every cleanup joins.
                await asyncio.gather(*(close_runtime(gateway, store, old, terminal=True) for old in others))
                if current['display_mode'] != 'iframe':
                    await close_runtime(gateway, store, current)
                    store.update_session(tenant, user, current['id'], display_mode='iframe')
                    current = store.session(tenant, user, current['id'])
                return await open_runtime(gateway, store, current, token, origin)

        pending = asyncio.create_task(replace())
        gateway.session_requests[key] = pending

        def done(task):
            if gateway.session_requests.get(key) is task:
                gateway.session_requests.pop(key, None)
            if not task.cancelled():
                task.exception()
        pending.add_done_callback(done)
    return await asyncio.shield(pending)
