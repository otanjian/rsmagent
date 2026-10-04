"""Tenant-authorized SAP scene setup. No normal-agent activation or execution."""
from functools import wraps
from http import HTTPStatus
import json
from pathlib import Path

import web

from .configuration import WorkbenchError, capabilities, configuration_checks, validate_config
from .deployment import BROWSER_SERVICE_REFS
from .store import WorkbenchStore


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


def _store(ctx):
    from config import get_data_root
    if not ctx.tenant_id or not ctx.user_id:
        raise WorkbenchError("missing_identity", 403)
    # Platform state is outside agent-browsable shared/project directories.
    # Rows are independently tenant scoped; this is not a global scene config.
    return WorkbenchStore(Path(get_data_root()) / "scenes" / "sap_workbench.sqlite3")


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
    payload = {"version": saved["version"], "can_manage": _can_manage(ctx),
               "coding": selected, "opencode": oc, "capabilities": capabilities(config),
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


class SapWorkbenchConfigHandler:
    @_endpoint
    def GET(self):
        from channel.web.fork.authorization import _db_scope
        from channel.web.fork.handlers.chat import _require_chat_use

        with _db_scope() as ctx:
            _require_chat_use(ctx)
            saved = _store(ctx).read_config(ctx.tenant_id)
            return _json(_projection(ctx, saved))

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
            return _json(_projection(ctx, saved))


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
                try:
                    result = browser_gateway.submit(open_workbench_runtime(browser_gateway, store,
                        ctx.tenant_id, ctx.user_id, coding, saved, _session_token(), _request_origin(),
                        request_id=body.get('request_id'), row=row), timeout=125)
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
