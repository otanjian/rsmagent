"""Cookie-authenticated, owner-scoped OpenCode browser API.

The shared upstream credential never crosses this boundary. Lists and event
streams are filtered as well as individual requests: authenticating only an
nginx subrequest would expose the shared engine's global session feed.
"""
from __future__ import annotations

import json
import ntpath
import posixpath
import re
import time
from urllib.parse import parse_qsl, unquote

import requests
import web

from agent.coding import resolve_settings
from agent.coding.browser_auth import COOKIE_PREFIX, same_origin, verify
from channel.web.fork.authorization import _db_scope, _require_agent_action, _require_private_owner, _require_tenant_agent_binding
from channel.web.fork.handlers.coding import _coding_error, _conversation_store, _owned_link, _require_coding_profile, _run_coding, _coding_service


def _deny(message="coding access unavailable", status="403 Forbidden"):
    _coding_error(message, status, "coding_browser_refused")


def _path(value):
    return ntpath.normcase(ntpath.normpath(value)) if re.match(r"^[A-Za-z]:", value) else posixpath.normpath(value)


def _scopes():
    from channel.web.auth_handlers import _session_token
    from channel.web.fork.handlers.chat import _require_chat_use

    settings = resolve_settings()
    if not settings.enabled or not settings.browser_sso or not settings.password:
        _deny()
    token = _session_token()
    scopes = []
    for name, cookie in web.cookies().items():
        if not name.startswith(COOKIE_PREFIX):
            continue
        claim = verify(settings.password, token, cookie, service=settings.service_id)
        if not claim:
            continue
        web.ctx.env["HTTP_X_TENANT_ID"] = claim["tenant"]
        try:
            with _db_scope() as ctx:
                agent = _require_tenant_agent_binding(ctx, claim["agent"])
                _require_private_owner(ctx, agent)
                _require_chat_use(ctx)
                _require_agent_action(ctx, agent, "use", "agent.use")
                _require_coding_profile(agent)
                store = _conversation_store(agent)
                source = _owned_link(ctx, claim["session"], agent, store)
                if source["service_id"] != settings.service_id:
                    continue
                links, cursor = [], None
                while True:
                    page = store.list_coding_links(owner=ctx.user_id, tenant_id=ctx.tenant_id, limit=200, after=cursor)
                    links.extend(link for link in page["links"] if link["service_id"] == settings.service_id
                                 and _path(link["project_dir"]) == _path(source["project_dir"]))
                    cursor = page["next_cursor"]
                    if not cursor:
                        break
                scopes.append({"claim": claim, "ctx": ctx, "store": store, "source": source,
                               "directory": source["project_dir"], "ids": {link["external_session_id"] for link in links}})
        except web.HTTPError:
            if int(web.ctx.status.split()[0]) not in {400, 401, 403, 404, 409}:
                raise
    if not scopes:
        _deny("open the coding session again from the platform", "401 Unauthorized")
    web.ctx.status = "200 OK"
    return settings, scopes


def _clean(value):
    """Only used for connection metadata, never conversation content."""
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()
                if not re.search(r"password|secret|token|api.?key|authorization|^key$|^env$|^headers$|^options$|^prompt$", key, re.I)}
    return value


def _event(value, scopes):
    if not isinstance(value, dict) or not isinstance(value.get("payload"), dict):
        return None
    event = value["payload"]
    kind = event.get("type", "")
    if kind in {"server.connected", "server.heartbeat"}:
        return {"directory": "global", "payload": {"type": kind, "properties": {}}}
    directory = value.get("directory")
    matching = [scope for scope in scopes if directory and _path(directory) == _path(scope["directory"])]
    if not matching:
        return None
    props = event.get("properties", {})
    info = props.get("info", {})
    part = props.get("part", {})
    session = (props.get("sessionID") or info.get("sessionID") or part.get("sessionID")
               or (info.get("id") if kind.startswith("session.") else None))
    # Unknown/global events fail closed; notably the engine's bulk sync events
    # contain data from unrelated sessions and must never reach the browser.
    if session and any(session in scope["ids"] for scope in matching):
        return value
    return None


class CodingBrowserProxyHandler:
    def GET(self, tail):
        return _run_coding(lambda: self._request("GET", tail))

    def POST(self, tail):
        return _run_coding(lambda: self._request("POST", tail))

    def PATCH(self, tail):
        return _run_coding(lambda: self._request("PATCH", tail))

    def DELETE(self, tail):
        return _run_coding(lambda: self._request("DELETE", tail))

    def _request(self, method, tail):
        settings, scopes = _scopes()
        web.header("Cache-Control", "no-store", unique=True)
        if web.ctx.env.get("HTTP_UPGRADE"):
            _deny("terminal connections are not supported by this browser entry")
        if method != "GET" and not same_origin(settings.web_url, web.ctx.env.get("HTTP_ORIGIN", "")):
            _deny("cross-origin request refused")
        path = "/" + tail
        if "%" in tail or "\\" in tail or any(part in {".", ".."} for part in tail.split("/")):
            _deny()
        query = parse_qsl(web.ctx.query.lstrip("?"), keep_blank_values=True)
        directories = [value for key, value in query if key in {"directory", "location[directory]"}]
        header_dir = web.ctx.env.get("HTTP_X_OPENCODE_DIRECTORY")
        if header_dir:
            directories.append(unquote(header_dir))
        if any("workspace" in key.lower() for key, _ in query) or web.ctx.env.get("HTTP_X_OPENCODE_WORKSPACE"):
            _deny()
        if directories:
            scopes = [scope for scope in scopes if all(_path(value) == _path(scope["directory"]) for value in directories)]
            if not scopes:
                _deny()
        scope = scopes[0]
        directory = scope["directory"]
        ids = set().union(*(entry["ids"] for entry in scopes))
        match = re.fullmatch(r"/session/(ses_[A-Za-z0-9_-]+)(/.*)?", path)
        if match and match[1] not in ids:
            _deny("session not found", "404 Not Found")
        if match:
            scope = next(entry for entry in scopes if match[1] in entry["ids"])
            directory = scope["directory"]
        metadata = {"/global/health", "/global/config", "/path", "/project", "/project/current", "/config", "/provider", "/provider/auth",
                    "/agent", "/command", "/mcp", "/mcp/resource", "/experimental/resource", "/skill", "/lsp", "/formatter", "/vcs", "/api/reference"}
        lists = {"/session", "/session/status", "/permission", "/question"}
        files = {"/file", "/file/content", "/file/status", "/find", "/find/file", "/find/symbol"}
        replies = re.fullmatch(r"/(permission|question)/([^/]+)/(reply|reject)", path)
        allowed = method == "GET" and (path in metadata | lists | files | {"/global/event"} or match)
        if method in {"POST", "PATCH", "DELETE"}:
            allowed = bool(match or replies)
        if not allowed:
            _deny("operation is not available through the platform browser entry")
        # Files share the Agent's configured project, as in the existing coding
        # contract. A browser parameter cannot retarget the filesystem root.
        for key, value in query:
            if path in files and key == "path" and (value.startswith(("/", "\\")) or ":" in value
                    or ".." in value.replace("\\", "/").split("/")):
                _deny()
        params = [(key, value) for key, value in query if key not in {"directory", "location[directory]"}]
        params.append(("directory", directory))
        headers = {**settings.auth_headers(), "Accept": "application/json", "x-opencode-directory": directory}
        if method != "GET":
            headers["Content-Type"] = "application/json"
        client = requests.Session()
        client.trust_env = False
        streaming = False

        def upstream(route, *, verb="GET", payload=None, stream=False):
            return client.request(verb, settings.api_url + route, params=params, headers=headers,
                                  data=payload, timeout=(5, 45), allow_redirects=False, stream=stream)

        try:
            if path == "/global/event":
                response = upstream(path, stream=True)
                if response.status_code != 200 or "text/event-stream" not in response.headers.get("Content-Type", ""):
                    response.close()
                    _deny("coding event service unavailable", "502 Bad Gateway")
                web.header("Content-Type", "text/event-stream", unique=True)
                web.header("X-Accel-Buffering", "no")
                streaming = True
                return self._events(response, client)
            if path == "/path":
                return self._json({"home": directory, "state": "", "config": "", "worktree": directory, "directory": directory})
            if path == "/provider/auth":
                return self._json({})
            if path == "/api/reference":
                return self._json({"data": []})
            if replies:
                listing = upstream("/" + replies[1])
                listing.raise_for_status()
                if not any(item.get("id") == replies[2] and item.get("sessionID") in ids for item in listing.json()):
                    _deny("request not found", "404 Not Found")
            data = web.data() if method != "GET" else None
            if data and len(data) > 16 * 1024 * 1024:
                _deny("request too large", "413 Request Entity Too Large")
            response = upstream("/project/current" if path == "/project" else path, verb=method, payload=data)
            if response.status_code == 204:
                web.ctx.status = "204 No Content"
                return ""
            if not 200 <= response.status_code < 300:
                # Engine errors may contain upstream credentials or file paths.
                _deny("coding service request failed", "502 Bad Gateway")
            value = response.json()
            if path == "/project":
                value = [value]
            if path in metadata:
                value = _clean(value)
            if path in {"/config", "/global/config"}:
                value = {key: value[key] for key in ("model", "small_model", "username", "compaction") if key in value}
            if path == "/session":
                value = [item for item in value if item.get("id") in ids]
            if path == "/session/status":
                value = {key: item for key, item in value.items() if key in ids}
            if path in {"/permission", "/question"}:
                value = [item for item in value if item.get("sessionID") in ids]
            if match and match[2] == "/children":
                value = [item for item in value if item.get("id") in ids]
            if match and match[2] == "/fork" and method == "POST":
                # Record a fork before the UI can request it. The existing
                # attach service verifies project identity and durable owner.
                web.ctx.env["HTTP_X_TENANT_ID"] = scope["claim"]["tenant"]
                with _db_scope() as ctx:
                    _coding_service(scope["store"]).attach(source_session_id=scope["source"]["session_id"],
                        external_session_id=value["id"], agent_id=scope["claim"]["agent"],
                        tenant_id=ctx.tenant_id, user_id=ctx.user_id)
            return self._json(value)
        except (requests.RequestException, ValueError):
            _deny("coding service unavailable", "502 Bad Gateway")
        finally:
            if not streaming:
                client.close()

    @staticmethod
    def _json(value):
        web.header("Content-Type", "application/json; charset=utf-8", unique=True)
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _events(response, client):
        try:
            settings, scopes = _scopes()
            checked = time.monotonic()
            # OpenCode emits single-line JSON data records. Do not forward SSE
            # ids, comments or unknown frames which could carry unrelated data.
            for line in response.iter_lines(chunk_size=1024):
                if time.monotonic() - checked >= 2:
                    settings, scopes = _scopes()
                    checked = time.monotonic()
                if not line.startswith(b"data: ") or len(line) > 4 * 1024 * 1024:
                    continue
                try:
                    value = _event(json.loads(line[6:]), scopes)
                except (ValueError, TypeError, AttributeError):
                    continue
                if value is not None:
                    yield ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode()
        except (web.HTTPError, requests.RequestException):
            return
        finally:
            response.close()
            client.close()
