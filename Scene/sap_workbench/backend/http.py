"""Tenant-authorized SAP scene setup. No normal-agent activation or execution."""
import base64
from functools import wraps
from http import HTTPStatus
import hmac
import json
from pathlib import Path

import web

from .configuration import WorkbenchError, capabilities, configuration_checks, validate_config
from .deployment import BROWSER_SERVICE_REFS
from .store import WorkbenchStore
from common.log import logger


def _json(payload):
    web.header("Content-Type", "application/json; charset=utf-8")
    web.header("Cache-Control", "no-store")
    return json.dumps({"status": "success", **payload}, ensure_ascii=False)


def _endpoint(fn):
    @wraps(fn)
    def handle(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except WorkbenchError as error:
            raise web.HTTPError(
                f"{error.status} {HTTPStatus(error.status).phrase}",
                {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
                json.dumps({"status": "error", "code": error.code, "message": error.code})) from None
    return handle


def _body(keys):
    raw = web.data()
    if len(raw) > 65536:
        raise WorkbenchError("request_too_large", 413)
    try:
        value = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError):
        raise WorkbenchError("invalid_json") from None
    if not isinstance(value, dict):
        raise WorkbenchError("invalid_request")
    # The console's global fetch shim stamps ``agent_id`` onto every same-origin
    # JSON body for Agent routing (channel/web/static/js/console.js); web.py also
    # merges query params into the payload. It is transport metadata, not scene
    # input, so drop it instead of refusing an otherwise valid request.
    value.pop("agent_id", None)
    if set(value) - set(keys):
        raise WorkbenchError("invalid_request")
    return value


def _scene_store():
    """The scene database, addressed without an identity.

    Only the plugin bridge may need a store before ownership is known: it looks
    up the owner *from* that store, so requiring a tenant to open it would be
    circular. Every other caller keeps :func:`_store`, which insists on an
    authenticated identity.
    """
    from config import get_data_root
    return WorkbenchStore(Path(get_data_root()) / "scenes" / "sap_workbench.sqlite3")


def _store(ctx):
    if not ctx.tenant_id or not ctx.user_id:
        raise WorkbenchError("missing_identity", 403)
    # Platform state is outside agent-browsable shared/project directories.
    # Rows are independently tenant scoped; this is not a global scene config.
    return _scene_store()


def _can_manage(ctx):
    return bool(ctx.is_platform_admin or ctx.is_tenant_admin)


def _require_manage(ctx):
    if not _can_manage(ctx):
        raise WorkbenchError("config_forbidden", 403)


def _coding(ctx, agent_id):
    from channel.web.fork.authorization import (
        _require_agent_action, _require_private_owner, _require_tenant_agent_binding,
    )
    from channel.web.fork.handlers.coding import _require_coding_profile

    _require_tenant_agent_binding(ctx, agent_id)
    _require_private_owner(ctx, agent_id)
    _require_agent_action(ctx, agent_id, "use", "agent.use")
    profile = _require_coding_profile(agent_id)
    if not profile.enabled:
        raise WorkbenchError("coding_disabled", 403)
    return {"id": profile.id, "name": profile.name,
            "project_dir": profile.coding_project_dir or ""}


def _install_project_toolkit(ctx, agent_id):
    """Install the skill and plugin into the coding project; never fatal.

    A configuration that names a coding Agent is still a valid configuration
    when the project directory is unreachable or files are read-only, so the
    install reports its outcome instead of failing the save. It must report
    honestly: a file left untouched because a user edited it is a difference,
    not a success. Nothing here claims a capability is available.
    """
    from . import project_toolkit
    try:
        project = _coding(ctx, agent_id)["project_dir"]
    except WorkbenchError as error:
        return {"artifacts": [], "conflicts": [], "error": error.code}
    if not project:
        return {"artifacts": [], "conflicts": [], "error": "project_directory_missing"}
    try:
        return project_toolkit.install(project)
    except ValueError as error:
        return {"artifacts": [], "conflicts": [], "error": str(error)}
    except OSError:
        return {"artifacts": [], "conflicts": [], "error": "project_directory_unwritable"}


def _coding_options(ctx):
    from agent.registry import get_agent_registry
    options = []
    for profile in get_agent_registry().list(include_disabled=False):
        if not profile.is_coding:
            continue
        selected = _optional_coding(ctx, profile.id)
        if selected:
            options.append(selected)
    return options


def _optional_coding(ctx, agent_id):
    # web.py HTTPError mutates the response before it is raised. A denial that
    # only filters an option must not turn the enclosing successful config
    # response into 403/404 (or retain its headers).
    previous = dict(web.ctx)
    try:
        return _coding(ctx, agent_id)
    except (web.HTTPError, WorkbenchError):
        return None
    finally:
        web.ctx.clear()
        web.ctx.update(previous)


def runtime_coding(ctx, agent_id):
    """Reuse synchronous Web authorization from the non-WSGI gateway thread.

    web.py's HTTPError constructor writes thread-local response headers. The
    gateway has no WSGI response, so supply a temporary context and translate
    denial before it reaches aiohttp. No await is allowed inside this scope.
    """
    from channel.web.fork.handlers.chat import _require_chat_use
    previous = dict(web.ctx)
    try:
        web.ctx.headers = []
        _require_chat_use(ctx)
        return _coding(ctx, agent_id)
    except web.HTTPError:
        raise WorkbenchError('session_forbidden', 403) from None
    finally:
        web.ctx.clear()
        web.ctx.update(previous)


def _projection(ctx, saved):
    from agent.coding import settings_for_console

    config = saved["config"]
    selected = None
    if config["coding_agent_id"]:
        if not _can_manage(ctx):
            selected = _coding(ctx, config["coding_agent_id"])
        else:
            # Controllers must be able to repair a retired binding, but never
            # receive the inaccessible Agent's profile/project data.
            selected = _optional_coding(ctx, config["coding_agent_id"])
    oc = settings_for_console()
    mcp_credentials = bool(config["mcp"]["username"] and saved.get("mcp_password_configured", False))
    payload = {"version": saved["version"], "can_manage": _can_manage(ctx),
               "coding": selected, "opencode": oc,
               "capabilities": capabilities(config, mcp_credentials=mcp_credentials),
               "checks": configuration_checks(config, selected, oc,
                                                mcp_password_configured=saved.get("mcp_password_configured", False)),
               "credential_source": "scene_config"}
    if _can_manage(ctx):
        payload["config"] = config
        payload["coding_options"] = _coding_options(ctx)
        payload["mcp_password_configured"] = saved.get("mcp_password_configured", False)
    else:
        payload["summary"] = {"system_id": config["sap"]["system_id"],
                              "client": config["sap"]["client"],
                              "login_mode": config["sap"]["login_mode"]}
    return payload


def prewarm_if_configured(ctx):
    """No engine to warm any more, so this does nothing.

    It used to spawn a throwaway Bun host from the scene catalog read, because
    the create that followed paid for an engine start. The conversation is now a
    platform coding session mounted from its own ``iframe_url`` and the scene
    starts no engine at all, so there is nothing to warm. Kept as a named hook
    for ``scenes/api.py`` until the warm-up module is retired with the rest of
    the self-managed engine path.
    """
    return None


class SapWorkbenchConfigHandler:
    @_endpoint
    def GET(self):
        from channel.web.fork.authorization import _db_scope
        from channel.web.fork.handlers.chat import _require_chat_use

        with _db_scope() as ctx:
            _require_chat_use(ctx)
            saved = _store(ctx).read_config(ctx.tenant_id)
            projected = _projection(ctx, saved)
            return _json(projected)

    @_endpoint
    def PUT(self):
        from auth.service import get_identity_service
        from channel.web.auth_handlers import require_management_write
        from channel.web.fork.authorization import _db_scope
        from channel.web.fork.handlers.chat import _require_chat_use

        require_management_write()
        with _db_scope() as ctx:
            _require_chat_use(ctx)
            _require_manage(ctx)
            body = _body({"version", "config", "mcp_password", "clear_mcp_password"})
            clean = validate_config(body.get("config"))
            if clean['browser_service_ref'] and clean['browser_service_ref'] not in BROWSER_SERVICE_REFS:
                raise WorkbenchError('browser_service_unavailable')
            if clean["coding_agent_id"]:
                _coding(ctx, clean["coding_agent_id"])

            def audit(change):
                get_identity_service().record_business_audit(
                    actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id,
                    action="sap_workbench.config.update", target="scene:sap_workbench",
                    redacted_changes=change)

            saved = _store(ctx).save_config(ctx.tenant_id, ctx.user_id, body.get("version"),
                                           clean, audit=audit, mcp_password=body.get("mcp_password"),
                                           clear_mcp_password=body.get("clear_mcp_password", False))
            payload = _projection(ctx, saved)
            if clean["coding_agent_id"]:
                # The skill and the plugin are what the shared coding service
                # discovers from the project directory, so they are installed
                # when the binding that names that project is saved -- not when
                # a session opens, which has no time budget for file writes.
                payload["project_toolkit"] = _install_project_toolkit(ctx, clean["coding_agent_id"])
            return _json(payload)


class SapWorkbenchCheckHandler:
    @_endpoint
    def POST(self):
        from channel.web.auth_handlers import require_management_write, _session_token
        from channel.web.fork.authorization import _db_scope
        from channel.web.fork.handlers.chat import _require_chat_use

        require_management_write()
        with _db_scope() as ctx:
            _require_chat_use(ctx)
            _require_manage(ctx)
            _body(set())
            from ..browser_service.runner import browser_gateway
            from .probe import probe_connections
            store = _store(ctx)
            saved = store.read_config(ctx.tenant_id)
            payload = _projection(ctx, saved)
            if not payload['coding'] or not saved['config']['sap']['web_gui_url']:
                raise WorkbenchError('config_missing')
            results = browser_gateway.submit(probe_connections(store, ctx.tenant_id, ctx.user_id, _session_token(), saved, payload['coding']), timeout=90)
            return _json({"checks": results, "capabilities": payload["capabilities"],
                          "mode": "read_only_connections", "network_tested": True})


class SapWorkbenchBrowserHandler:
    """Legacy unbound screen entry is closed; all views require a session lease."""

    @_endpoint
    def POST(self):
        from channel.web.auth_handlers import require_management_write
        from channel.web.fork.authorization import _db_scope
        from channel.web.fork.handlers.chat import _require_chat_use
        require_management_write()
        with _db_scope() as ctx:
            _require_chat_use(ctx)
            _body(set())
            raise WorkbenchError('bound_session_required', 410)


def _request_origin():
    """The Origin the browser will send, derived from this very request."""
    env = web.ctx.env
    forwarded = (env.get("HTTP_X_FORWARDED_PROTO") or "").split(",")[0].strip()
    scheme = "https" if forwarded == "https" or env.get("wsgi.url_scheme") == "https" else "http"
    host = env.get("HTTP_HOST") or env.get("SERVER_NAME") or ""
    return f"{scheme}://{host}" if host else ""


class SapWorkbenchSessionsHandler:
    @_endpoint
    def GET(self):
        from channel.web.fork.authorization import _db_scope
        from channel.web.fork.handlers.chat import _require_chat_use
        with _db_scope() as ctx:
            _require_chat_use(ctx)
            return _json({'sessions': _store(ctx).sessions(ctx.tenant_id, ctx.user_id)})

    @_endpoint
    def POST(self):
        from channel.web.auth_handlers import require_management_write, _session_token
        from channel.web.fork.authorization import _db_scope
        from channel.web.fork.handlers.chat import _require_chat_use

        require_management_write()
        with _db_scope() as ctx:
            _require_chat_use(ctx)
            from ..browser_service.runner import browser_gateway
            from .runtime import open_workbench_runtime, close_runtime
            body = _body({"request_id", "binding_id", "action", "control", "navigation_id"})
            store = _store(ctx)
            action = body.get('action', 'open')
            if not isinstance(action, str) or action not in {'open', 'control', 'close', 'heartbeat', 'navigation_ack'}:
                raise WorkbenchError('invalid_action')
            if 'binding_id' in body and (not isinstance(body['binding_id'], str)
                                        or not 1 <= len(body['binding_id']) <= 128):
                raise WorkbenchError('invalid_request')
            if 'control' in body and (not isinstance(body['control'], str)
                                      or body['control'] not in {'manual', 'automatic'}):
                raise WorkbenchError('invalid_control')
            if action in {'control', 'close', 'heartbeat', 'navigation_ack'} and not body.get('binding_id'):
                raise WorkbenchError('bound_session_required', 400)
            if action == 'navigation_ack' and (not isinstance(body.get('navigation_id'), str)
                                               or not 1 <= len(body['navigation_id']) <= 128):
                raise WorkbenchError('invalid_request', 400)
            if action == 'control' and 'control' not in body:
                raise WorkbenchError('invalid_control')
            row = store.session(ctx.tenant_id, ctx.user_id, body['binding_id']) if body.get('binding_id') else None
            # Cleanup is owner scoped but does not require the old SAP/coding
            # config to remain enabled or accessible. It never opens resources.
            if action == 'close':
                return _json(browser_gateway.submit(close_runtime(browser_gateway, store, row), timeout=15))
            if row and row['state'] == 'closed':
                # A superseded binding is terminal. Answer before reading the
                # current configuration or touching anything external.
                raise WorkbenchError('session_closed', 410)
            saved = store.read_config(ctx.tenant_id)
            config = saved['config']
            if not config['enabled']:
                raise WorkbenchError('disabled', 503)
            if config['browser_service_ref'] not in BROWSER_SERVICE_REFS:
                raise WorkbenchError('browser_service_unavailable', 503)
            coding = _coding(ctx, config['coding_agent_id'])
            if row:
                _coding(ctx, row['agent_id'])
            elif not isinstance(body.get('request_id'), str) or not 8 <= len(body['request_id']) <= 128:
                raise WorkbenchError('invalid_request_id')
            if action == 'open':
                # The project artifacts are part of this scene's delivery, and an
                # install that ran only when the configuration was last saved
                # leaves every deployment configured earlier on whatever revision
                # was current then. That is not cosmetic: the previous revision
                # gates itself on an environment variable the retired per-session
                # host sets, so a stale install registers no navigation tool at
                # all while the workbench still looks saved and enabled. Re-running
                # is idempotent and refuses to overwrite a user's edit, so doing it
                # on open is what lets the delivery heal instead of rot.
                refreshed = _install_project_toolkit(
                    ctx, row['agent_id'] if row else config['coding_agent_id'])
                if refreshed.get('conflicts'):
                    logger.warning('[SapWorkbench] project artifacts left untouched (user edits): %s',
                                   ', '.join(refreshed['conflicts']))
                elif refreshed.get('error'):
                    logger.warning('[SapWorkbench] project artifacts not installed: %s',
                                   refreshed['error'])
                try:
                    # No engine start is inside this budget any more: the binding
                    # is a registry write. The headroom is for the owner's older
                    # bindings to finish closing, not for a cold start.
                    result = browser_gateway.submit(open_workbench_runtime(browser_gateway, store,
                        ctx.tenant_id, ctx.user_id, coding, saved, _session_token(), _request_origin(),
                        request_id=body.get('request_id'), row=row), timeout=25)
                except WorkbenchError:
                    raise
                except Exception:
                    raise WorkbenchError('runtime_unavailable', 503) from None
                return _json(result)
            instance = browser_gateway.runtimes.get(row['id'])
            if not instance or getattr(instance, 'closed', False) or not hasattr(instance, 'set_control'):
                if row['state'] == 'closed':
                    raise WorkbenchError('session_closed', 410)
                raise WorkbenchError('session_not_running', 409)
            if action == 'heartbeat':
                async def heartbeat():
                    await instance.authorize()
                    import time
                    instance.last_active = time.monotonic()
                    return {'binding_id': row['id'], 'state': 'ready',
                            'navigation': instance.navigation.command() if instance.display_mode == 'iframe' else None}
                return _json(browser_gateway.submit(heartbeat(), timeout=15))
            if action == 'navigation_ack':
                async def acknowledge():
                    await instance.authorize()
                    if instance.display_mode != 'iframe':
                        raise WorkbenchError('iframe_navigation_unavailable', 409)
                    instance.navigation.acknowledge(body['navigation_id'])
                    return {'binding_id': row['id'], 'state': 'navigation_applied'}
                return _json(browser_gateway.submit(acknowledge(), timeout=15))
            if action == 'control':
                return _json(browser_gateway.submit(instance.set_control(body.get('control')), timeout=15))


def _require_coding_service():
    """Authenticate the caller as the OpenCode coding service itself.

    The project plugin runs inside the shared coding service, which has no
    console session, so the ordinary identity gate cannot apply. It must not be
    replaced with a token-less "trusted caller" shortcut either: the caller
    proves itself with the service's own HTTP credential, which only a process
    that already holds it can present. This step decides *who is calling* only;
    the handler still resolves *whose workbench* from the session id, so this
    credential is never what authorizes a particular binding's browser.
    """
    from agent.coding import resolve_settings
    settings = resolve_settings()
    if not settings.enabled or not settings.password:
        raise WorkbenchError('bridge_unavailable', 503)
    scheme, _, payload = web.ctx.env.get('HTTP_AUTHORIZATION', '').partition(' ')
    if scheme.lower() != 'basic' or not payload:
        raise WorkbenchError('bridge_unauthorized', 401)
    try:
        decoded = base64.b64decode(payload, validate=True).decode('utf-8')
    except Exception:
        raise WorkbenchError('bridge_unauthorized', 401) from None
    username, separator, password = decoded.partition(':')
    if not separator or not (hmac.compare_digest(username, settings.username)
                             and hmac.compare_digest(password, settings.password)):
        raise WorkbenchError('bridge_unauthorized', 401)
    # A direct loopback call from the co-located service carries no forwarding
    # headers; the public reverse proxy always adds them. Without this check the
    # service credential alone would be enough to drive SAP navigation from the
    # network, which is more than "the coding service on this host may ask".
    for key in ('HTTP_X_FORWARDED_FOR', 'HTTP_X_REAL_IP', 'HTTP_X_FORWARDED_HOST'):
        if web.ctx.env.get(key):
            raise WorkbenchError('bridge_unauthorized', 401)
    if web.ctx.env.get('REMOTE_ADDR') not in {'127.0.0.1', '::1'}:
        raise WorkbenchError('bridge_unauthorized', 401)


class SapWorkbenchBridgeHandler:
    """Left-pane navigation channel for the project plugin.

    The plugin knows only its OpenCode session id and a transaction code. Every
    identifier that decides *whose* pane is driven is resolved here from that
    session id: a caller cannot name a user, a SAP account or a binding, and an
    unresolved session is refused rather than served from a nearby binding.
    Execution then runs through the live binding's own ``dispatch``, so the
    existing owner authorization, action ledger and meter still decide the
    outcome -- an absent or closed binding yields no SAP operation at all.
    """

    @_endpoint
    def POST(self):
        import asyncio

        from ..browser_service.runner import browser_gateway

        _require_coding_service()
        body = _body({"session_id", "transaction", "call_id"})
        session_id, transaction, call = body.get('session_id'), body.get('transaction'), body.get('call_id')
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 128:
            raise WorkbenchError('invalid_request', 400)
        if not isinstance(transaction, str) or not 1 <= len(transaction) <= 32:
            raise WorkbenchError('invalid_request', 400)
        if not isinstance(call, str) or not 1 <= len(call) <= 128:
            raise WorkbenchError('invalid_request', 400)
        row = _scene_store().binding_for_session(session_id)
        if not row:
            # Unbound, ambiguous or closed: never borrow another binding.
            raise WorkbenchError('session_not_bound', 403)
        instance = browser_gateway.runtimes.get(row['id'])
        if isinstance(instance, asyncio.Task) or not instance or getattr(instance, 'closed', False):
            raise WorkbenchError('session_not_running', 409)
        # The navigation itself waits for the visible pane to acknowledge the
        # command (bounded inside IframeNavigation); this outer budget only has
        # to outlast it so a real scene refusal is not reported as a gateway
        # timeout.
        return _json(browser_gateway.submit(instance.navigate(transaction, call), timeout=35))


class SapWorkbenchDataBridgeHandler:
    """Business-data channel for the project plugin.

    Same ownership rule as the navigation channel: the plugin presents its
    OpenCode session id plus *business* arguments, and every identifier that
    decides whose SAP account is used -- the binding, the registered connection
    and the stored credential -- is resolved here from that session id. The
    plugin, and therefore the model, never receives an account, a password or a
    connection id; the connection id is attached inside the runtime's own MCP
    worker (``SapMcpLogin._call``).

    The reachable surface is the business set (``read_table`` / ``run_query`` /
    ``call_rfc``), not the whole gateway catalogue. ``call_rfc`` can reach a
    BAPI that writes a business document, which is why every call is admitted
    through the ordinary ledger, meter and audit path; the ADT tools that change
    the system itself are not reachable through here at all.
    """

    @_endpoint
    def POST(self):
        import asyncio

        from ..browser_service.runner import browser_gateway

        _require_coding_service()
        body = _body({"session_id", "connection", "tool", "arguments", "call_id"})
        session_id, call = body.get('session_id'), body.get('call_id')
        connection, tool = body.get('connection'), body.get('tool')
        arguments = body.get('arguments')
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 128:
            raise WorkbenchError('invalid_request', 400)
        if not isinstance(call, str) or not 1 <= len(call) <= 128:
            raise WorkbenchError('invalid_request', 400)
        if (not isinstance(connection, str) or not 1 <= len(connection) <= 64
                or not isinstance(tool, str) or not 1 <= len(tool) <= 64
                or not isinstance(arguments, dict)):
            raise WorkbenchError('invalid_request', 400)
        row = _scene_store().binding_for_session(session_id)
        if not row:
            # Unbound, ambiguous or closed: never borrow another binding.
            raise WorkbenchError('session_not_bound', 403)
        instance = browser_gateway.runtimes.get(row['id'])
        if isinstance(instance, asyncio.Task) or not instance or getattr(instance, 'closed', False):
            raise WorkbenchError('session_not_running', 409)
        # The first call of a binding also starts the private MCP worker, which
        # is allowed 60s to establish the SAP connections; this outer budget has
        # to outlast both that start and the worker's own 30s call budget so a
        # real refusal is not reported as a gateway timeout.
        return _json(browser_gateway.submit(
            instance.data_call(connection, tool, arguments, call), timeout=75))
