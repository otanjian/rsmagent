"""Fork web layer (change adopt-upstream-web-split, design D2).

Fork-owned implementation, moved verbatim out of the former
channel/web/web_channel.py monolith. Upstream's api/ modules are not
edited. Imports inside function bodies are lazy so these modules can
reference each other without import cycles.
"""

from __future__ import annotations
from auth.runtime import authorized_target, authorized_target_scope
from bridge.context import *
from bridge.reply import Reply, ReplyType
from common.log import logger
from contextlib import contextmanager
from urllib.parse import quote
import datetime
import json
import mimetypes
import os
import random
import web


@contextmanager
def _uploads_identity_scope():
    """Resolve an upload read-back's tenant from the addressed Agent.

    The console renders an uploaded thumbnail or clip as an ``<img>``/``<audio>``
    subresource, and a browser subresource request cannot carry ``X-Tenant-ID``.
    The route is therefore declared ``tenant_from_resource`` in the roster, the
    gate authenticates the caller without a tenant selection, and this scope
    supplies the tenant from the addressed Agent's binding — the same shape as
    ``_stream_identity_scope``, which derives it from the recorded request's
    owner.

    The *resource* decides the tenant, never the client: the binding is read
    server-side and membership in that tenant is resolved through
    ``resolve_context``. A caller-supplied selection is only cross-checked, and an
    Agent that resolves to no tenant is reported as not-found so an unauthenticated
    caller cannot probe which ids are bound. Object-level ownership and the
    authoritative ``agent.read`` check stay in the handler.
    """
    from channel.web.web_channel import _chat_error
    from channel.web.web_channel import _request_agent_id
    from auth.runtime import resolve_context, to_runtime_identity, IdentityContextError
    from channel.web.auth_handlers import _get_service, _session_token
    from common.runtime_identity import use_identity

    svc, token = _get_service(), _session_token()
    if not token:
        _chat_error("unauthorized", "401 Unauthorized", "unauthorized")
    params = web.input(agent_id='', agent='', tenant_id='')
    agent_id = _request_agent_id(params)

    def _fail(exc: "IdentityContextError"):
        from http import HTTPStatus
        _chat_error(str(exc), f"{exc.status} {HTTPStatus(exc.status).phrase}", exc.code)

    try:
        # Authenticate before anything else: an unbound-Agent 404 must not be
        # observable to a caller who has no valid session at all.
        resolve_context(svc, token, None)
    except IdentityContextError as e:
        _fail(e)

    binding = svc.get_agent_binding(agent_id) if agent_id else None
    tenant_id = binding["tenant_id"] if binding else None
    if not tenant_id:
        raise web.notfound()

    for selected in (web.ctx.env.get("HTTP_X_TENANT_ID", ""),
                     getattr(params, "tenant_id", "") or ""):
        if selected and selected != tenant_id:
            _chat_error("conflicting tenant selection", "400 Bad Request",
                        "conflicting_tenant")

    try:
        ctx = resolve_context(svc, token, tenant_id)
    except IdentityContextError as e:
        _fail(e)
    if ctx.must_change_password:
        _chat_error("password change required", code="password_change_required")

    with use_identity(to_runtime_identity(ctx)):
        yield ctx, agent_id


@contextmanager
def _file_identity_scope():
    """Resolve a file read's tenant from the addressed file (its workspace).

    ``GET /api/file`` backs a plain ``<a download>`` navigation and ``<img>``
    subresources, so the browser issues it without ``X-Tenant-ID``. The route is
    declared ``tenant_from_resource`` in the roster, the gate authenticates the
    caller without a tenant selection, and this scope derives the tenant from
    the file's own workspace and verifies membership through ``resolve_context``
    — the same shape as ``_uploads_identity_scope``. A supplied selection is only
    cross-checked; the resource stays authoritative.
    """
    from channel.web.web_channel import _chat_error
    from channel.web.web_channel import _tenant_owning_path
    from auth.runtime import resolve_context, to_runtime_identity, IdentityContextError
    from channel.web.auth_handlers import _get_service, _session_token
    from common.runtime_identity import use_identity

    svc, token = _get_service(), _session_token()
    if not token:
        _chat_error("unauthorized", "401 Unauthorized", "unauthorized")

    def _fail(exc: "IdentityContextError"):
        from http import HTTPStatus
        _chat_error(str(exc), f"{exc.status} {HTTPStatus(exc.status).phrase}", exc.code)

    # Authenticate before anything else: an unresolvable path must not be
    # observable to a caller who has no valid session at all.
    try:
        resolve_context(svc, token, None)
    except IdentityContextError as e:
        _fail(e)

    params = web.input(path="", tenant_id="")
    raw = params.path
    real_path = os.path.realpath(raw) if raw else None
    tenant_id = _tenant_owning_path(svc, real_path) if real_path else None
    if not tenant_id:
        raise web.notfound()

    for selected in (web.ctx.env.get("HTTP_X_TENANT_ID", ""),
                     getattr(params, "tenant_id", "") or ""):
        if selected and selected != tenant_id:
            _chat_error("conflicting tenant selection", "400 Bad Request",
                        "conflicting_tenant")

    try:
        ctx = resolve_context(svc, token, tenant_id)
    except IdentityContextError as e:
        _fail(e)
    if ctx.must_change_password:
        _chat_error("password change required", code="password_change_required")

    with use_identity(to_runtime_identity(ctx)):
        yield ctx


def _db_path_owner_forbidden(ctx: "Optional[RequestContext]", agent_id: "Optional[str]") -> bool:
    """True when the caller may not read ``agent_id``'s assets.

    In database mode, if the agent is privately owned (``private_owner_user_id``
    set) only that owner may read its memory/knowledge/files. **An administrator
    is not an exception**: ``tenant_admin`` reaches the *governance* surface
    (ownership, usage, the stop action) but not the member's private content, and
    platform-admin status is already no exception at the tenant layer. Anything
    else would let "manage the tenant" double as "read every member's workspace".
    Legacy mode is a no-op.

    Pure predicate so both the raising gate (:func:`_require_private_owner`) and
    the file-path authorization (:func:`_authorize_db_file_path`) share one rule.
    """
    if ctx is None or not getattr(ctx, "tenant_id", None) or not agent_id:
        return False
    from auth.service import get_identity_service
    binding = get_identity_service().get_agent_binding(agent_id)
    if not binding:
        return False
    owner = binding.get("private_owner_user_id")
    if not owner:
        # tenant-shared asset: any permission-holder of the tenant may read.
        return False
    return owner != getattr(ctx, "user_id", None)


def _platform_file_root() -> str:
    """Platform-admin read-only browse root; defaults to data root."""
    from channel.web.web_channel import conf
    from channel.web.web_channel import get_data_root
    configured = (conf().get("platform_file_root") or "").strip()
    if configured:
        return os.path.realpath(os.path.expanduser(configured))
    return os.path.realpath(get_data_root())


def _tenant_workspace_root_owners() -> list:
    """``[(realpath, agent_id_or_None)]`` every workspace the server may serve.

    Same static registration as :func:`_tenant_workspace_roots`, but keeping the
    owning Agent so the private-owner rule can be applied to a path *without* a
    request identity. ``None`` marks a tenant shared root (shared by definition).
    """
    owners = []
    try:
        from auth.service import get_identity_service
        svc = get_identity_service()
        for rec in svc.tenant_shared_roots():
            root = (rec or {}).get("shared_root")
            if root:
                owners.append((os.path.realpath(root), None))
        from agent.registry import get_agent_registry
        registry = get_agent_registry()
        for binding in svc.list_agent_bindings():
            agent_id = (binding or {}).get("agent_id")
            if not agent_id:
                continue
            try:
                workspace = registry.get(agent_id).workspace
            except (KeyError, ValueError, TypeError):
                continue
            if workspace:
                owners.append((os.path.realpath(workspace), agent_id))
    except Exception as e:
        logger.debug(f"[WebChannel] tenant workspace roots unavailable: {e}")
    return owners


def _tenant_workspace_roots() -> list:
    """Every tenant/Agent workspace the server may mint a capability for.

    ``/preview`` is capability-authorized: the HMAC directory token, not a
    request identity, authorizes the read (the sandboxed iframe cannot send the
    session cookie). This list backs the defense-in-depth root check for that
    path, so it has to cover every workspace ``_build_preview_url`` can be
    called with — each tenant's shared root and each bound Agent's workspace —
    independent of the request (a public preview request carries no identity).
    """
    return [root for root, _ in _tenant_workspace_root_owners()]


def _static_path_private_owner(real_path: str) -> Optional[str]:
    """The owner of ``real_path`` when it sits in a *privately owned* workspace.

    Resolution uses the static workspace registration, so it works for a
    capability preview that carries no identity. ``None`` means "not privately
    owned" — a shared root, a shared Agent, a platform path, an unknown path, or
    an ambiguous one. Ambiguity is not resolved here; :func:`_is_path_allowed`
    already refuses what cannot be attributed, and a privately owned workspace is
    attributed the same way there.

    A lookup that *fails* is not an answer, and must not be read as one: raising
    :class:`_PrivateOwnerLookupFailed` keeps a store outage from converting a
    private file into a public one (task 3.6 property 4). "Unknown" and
    "unreachable" are opposite conclusions and are reported differently.

    This is the consumption-side half of the private-owner rule: ownership is
    re-derived from the path at read time, so a token minted while a member owned
    the workspace cannot outlive the ownership.
    """
    kind, agent_id = _db_path_owner(real_path, _tenant_workspace_root_owners())
    if kind != "agent" or not agent_id:
        return None
    from auth.service import get_identity_service
    binding = get_identity_service().get_agent_binding(agent_id)
    return (binding or {}).get("private_owner_user_id") or None


def _preview_consumer_user_id() -> Optional[str]:
    """The verified user id of the current request, or ``None`` when unresolved."""
    from channel.web.auth_handlers import _get_service, _session_token

    token = _session_token()
    if not token:
        return None
    try:
        from auth.runtime import resolve_context
        ctx = resolve_context(_get_service(), token, None)
    except Exception:
        return None
    return getattr(ctx, "user_id", None) or None


def _preview_consumer_may_read(real_path: str) -> bool:
    """Whether the *current* request may consume a capability preview of ``path``.

    Public workspace files need no identity: the HMAC token is the whole
    authorization, which is what lets an anonymous iframe render an Agent's
    generated page. Two things are **not** public even with a valid token:

    * a **privately owned** Agent's file — the token only proves the URL was
      once issued, so it must not survive as a bearer grant to someone else's
      workspace;
    * a member's ``user/<user_id>`` file in a **shared** Agent (change
      ``isolate-shared-agent-user-data``) — sharing the Agent does not share the
      files, and the ``/preview`` token is not an owner credential. An
      unparsable owner inside the container is refused for everyone.

    For both, the current session is resolved and must be the owner; no session,
    an expired session, or a different user all refuse. Anything that prevents an
    answer — the store failing, the session lookup failing — refuses too.
    """
    try:
        user_state, user_owner = _static_path_user_state(real_path)
        owner = _static_path_private_owner(real_path)
    except Exception as e:
        logger.warning(f"[WebChannel] preview ownership check failed closed: {e}")
        return False

    if user_state in ("user", "unowned"):
        if user_state == "unowned":
            return False
        return _preview_consumer_user_id() == user_owner
    if owner is None:
        return True
    return _preview_consumer_user_id() == owner


class UploadHandler:
    def POST(self):
        from channel.web.web_channel import WebChannel
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _raw_web_input
        from channel.web.web_channel import _require_agent_action
        from channel.web.web_channel import _require_chat_csrf
        from channel.web.web_channel import _require_private_owner
        from channel.web.web_channel import _require_tenant_agent_binding
        from channel.web.web_channel import _scoped_agent_id
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'no-store')
        with _db_scope() as ctx:
            # Database mode: an upload writes into a tenant-bound agent's upload
            # dir, so the target agent must be bound to the caller's tenant and
            # execution-authorized (attachments belong to the chat workflow).
            _require_chat_csrf()
            params = _raw_web_input()
            # The client keeps agent_id out of the multipart body (a field in
            # both query and body arrives as a list), so resolve it across both:
            # reading the body alone scopes the write to the default Agent.
            agent_id = _require_tenant_agent_binding(ctx, _scoped_agent_id(params))
            _require_private_owner(ctx, agent_id)
            _require_agent_action(ctx, agent_id, "use", "agent.use")
            with authorized_target_scope(agent_id=agent_id):
                return WebChannel().upload_file()


class VoiceAsrHandler:
    """Receive a mic recording, persist it under uploads/ and run ASR.
    Returns {status, text, audio_url} so the UI can render a playback bubble."""
    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _get_upload_dir
        from channel.web.web_channel import _raw_web_input
        from channel.web.web_channel import _require_agent_action
        from channel.web.web_channel import _require_private_owner
        from channel.web.web_channel import _require_tenant_agent_binding
        from channel.web.web_channel import _scoped_agent_id
        web.header('Content-Type', 'application/json; charset=utf-8')

        saved_path = None
        try:
            params = _raw_web_input()
            with _db_scope() as ctx:
                # Mic recording lands in a tenant-bound agent's upload dir;
                # voice input is part of the chat flow. The recording route is
                # multipart, so the Agent comes from the query string as well as
                # the body — resolving the body alone writes the recording into
                # the default Agent's workspace.
                agent_id = _require_tenant_agent_binding(ctx, _scoped_agent_id(params))
                _require_private_owner(ctx, agent_id)
                _require_agent_action(ctx, agent_id, "use", "agent.use")
                file_obj = params.get("file")
                if file_obj is None:
                    return json.dumps({"status": "error", "message": "no audio file"})

                filename = getattr(file_obj, "filename", "") or "recording.webm"
                ext = os.path.splitext(filename)[1].lower() or ".webm"
                if ext not in (".webm", ".ogg", ".opus", ".mp4", ".m4a", ".mp3", ".wav"):
                    ext = ".webm"

                upload_dir = _get_upload_dir(agent_id)
                os.makedirs(upload_dir, exist_ok=True)
                ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
                saved_name = f"voice_input_{ts}_{random.randint(0, 9999)}{ext}"
                saved_path = os.path.join(upload_dir, saved_name)
                with open(saved_path, "wb") as f:
                    f.write(file_obj.file.read() if hasattr(file_obj, "file") else file_obj.value)

                suffix = f"?agent_id={agent_id}" if agent_id else ""
                audio_url = f"/uploads/{saved_name}{suffix}"

                from bridge.bridge import Bridge
                reply = Bridge().fetch_voice_to_text(saved_path)
                if reply is None:
                    return json.dumps({
                        "status": "error",
                        "message": "ASR returned no reply",
                        "audio_url": audio_url,
                    })

                from bridge.reply import ReplyType
                if reply.type == ReplyType.TEXT:
                    return json.dumps({
                        "status": "success",
                        "text": reply.content or "",
                        "audio_url": audio_url,
                    })
                return json.dumps({
                    "status": "error",
                    "message": reply.content or "ASR failed",
                    "audio_url": audio_url,
                })
        except web.HTTPError:
            raise
        except Exception as e:
            logger.exception(f"[VoiceAsrHandler] failed: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class VoiceTtsHandler:
    """On-demand TTS for the in-chat "read aloud" button. Returns the
    audio URL and (when session_id is given) persists it onto the message."""
    def POST(self):
        from channel.web.web_channel import WebChannel
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _require_agent_action
        from channel.web.web_channel import _require_private_owner
        from channel.web.web_channel import _require_tenant_agent_binding
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            data = json.loads(web.data() or b"{}")
            text = (data.get("text") or "").strip()
            session_id = (data.get("session_id") or "").strip()
            if not text:
                return json.dumps({"status": "error", "message": "empty text"})
            with _db_scope() as ctx:
                if ctx is not None:
                    # Database mode: TTS output attaches to a tenant-bound
                    # agent's session; the caller must be chat-authorized for it.
                    agent_id = _require_tenant_agent_binding(ctx, data.get("agent_id"))
                    _require_private_owner(ctx, agent_id)
                    _require_agent_action(ctx, agent_id, "use", "agent.use")
                else:
                    agent_id = data.get("agent_id")
                # `@singleton` makes WebChannel a factory function — go via instance.
                channel = WebChannel()
                if not channel._tts_provider_ready():
                    return json.dumps({"status": "error", "message": "tts not configured"})

                from bridge.bridge import Bridge
                reply = Bridge().fetch_text_to_voice(text)
                if reply is None or reply.type != ReplyType.VOICE or not reply.content:
                    msg = getattr(reply, "content", "") or "tts failed"
                    return json.dumps({"status": "error", "message": str(msg)})

                url = channel._publish_tts_audio(reply.content, agent_id)
                if not url:
                    return json.dumps({"status": "error", "message": "publish failed"})

                if session_id:
                    try:
                        from agent.memory import get_conversation_store
                        from agent.registry import get_agent_registry
                        profile = get_agent_registry().get(agent_id)
                        get_conversation_store(profile.workspace).attach_extras_to_last_assistant(
                            session_id, {"audio": {"url": url, "kind": "tts"}},
                        )
                    except Exception as e:
                        logger.debug(f"[VoiceTtsHandler] persist skipped: {e}")

                return json.dumps({"status": "success", "audio_url": url})
        except web.HTTPError:
            raise
        except Exception as e:
            logger.exception(f"[VoiceTtsHandler] failed: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class UploadsHandler:
    def GET(self, file_name):
        # The tenant comes from the addressed Agent, not a header: the console
        # loads this as an <img>/<audio> subresource, which cannot send one.
        from channel.web.web_channel import _db_path_visible
        from channel.web.web_channel import _get_upload_dir
        from channel.web.web_channel import _require_agent_action
        from channel.web.web_channel import _require_private_owner
        from channel.web.web_channel import _require_tenant_agent_binding
        with _uploads_identity_scope() as (ctx, requested_agent_id):
            try:
                agent_id = _require_tenant_agent_binding(ctx, requested_agent_id)
                _require_private_owner(ctx, agent_id)
                _require_agent_action(ctx, agent_id, "read", "agent.read")
                # The upload directory is the *caller's* own user subtree now,
                # so this read is already owner-scoped; the explicit check keeps
                # the two halves (where it is stored, who may read it) in one
                # place even if the directory resolution ever changes.
                upload_dir = _get_upload_dir(agent_id)
                full_path = os.path.realpath(os.path.join(upload_dir, file_name))
                if not _is_under(full_path, os.path.realpath(upload_dir)):
                    raise web.notfound()
                if not os.path.isfile(full_path):
                    raise web.notfound()
                if not _db_path_visible(ctx, full_path):
                    raise web.notfound()
                content_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
                web.header('Content-Type', content_type)
                if _path_in_user_subtree(full_path):
                    web.header('Cache-Control', 'private, no-store')
                else:
                    web.header('Cache-Control', 'public, max-age=86400')
                with open(full_path, 'rb') as f:
                    return f.read()
            except web.HTTPError:
                raise
            except Exception as e:
                logger.error(f"[WebChannel] Error serving upload: {e}")
                raise web.notfound()


def _db_file_root_owners(ctx) -> list:
    """``[(realpath, agent_id_or_None)]`` roots a caller may serve files from.

    Platform and tenant-shared roots carry ``None``; a bound Agent's workspace
    carries its ``agent_id`` so callers can apply the private/shared ownership
    rule to the root a path actually resolves into.
    """
    from channel.web.web_channel import _platform_file_root
    from auth.service import get_identity_service
    svc = get_identity_service()
    roots = []
    if getattr(ctx, "is_platform_admin", False):
        roots.append((os.path.realpath(_platform_file_root()), None))
    shared = svc.tenant_shared_root(ctx.tenant_id) if getattr(ctx, "tenant_id", None) else None
    if shared:
        roots.append((os.path.realpath(shared), None))
    from agent.registry import get_agent_registry
    registry = get_agent_registry()
    if getattr(ctx, "tenant_id", None):
        for agent_id in svc.tenant_agent_ids(ctx.tenant_id):
            try:
                profile = registry.get(agent_id)
                roots.append((os.path.realpath(profile.workspace), agent_id))
            except (KeyError, ValueError):
                continue
    return roots


def _db_file_serve_roots(ctx) -> list:
    """Roots a database-mode caller may serve files from.

    Flat projection of :func:`_db_file_root_owners`, kept as the seam existing
    callers and tests patch.
    """
    from channel.web.web_channel import _db_file_root_owners
    return [root for root, _ in _db_file_root_owners(ctx)]


def _db_path_owner(real_path: str, roots: list) -> tuple:
    """Which of ``roots`` owns ``real_path``: ``(kind, agent_id)``.

    ``kind`` is ``platform``, ``shared``, ``agent``, ``ambiguous`` or ``none``.
    The **most specific** (longest) matching root wins, so an Agent workspace
    nested inside the tenant shared root keeps its identity instead of being
    swallowed by the shared root.

    Two *distinct* Agent workspaces of equal specificity make the ownership
    ambiguous and the caller must fail closed. An Agent workspace that literally
    equals a shared root is not ambiguous: the directory is the tenant's shared
    root, whose contents are shared by definition.
    """
    from channel.web.web_channel import _platform_file_root
    real_path = os.path.realpath(real_path)
    platform_root = os.path.realpath(_platform_file_root())
    try:
        if os.path.commonpath([real_path, platform_root]) == platform_root:
            return "platform", None
    except ValueError:
        pass

    matches = []
    for root, agent_id in roots:
        root = os.path.realpath(root)
        if root == platform_root:
            continue
        try:
            if os.path.commonpath([real_path, root]) == root:
                matches.append((len(root), agent_id))
        except ValueError:
            continue
    if not matches:
        return "none", None

    longest = max(length for length, _ in matches)
    top = {agent_id for length, agent_id in matches if length == longest}
    agent_ids = {agent_id for agent_id in top if agent_id}
    if len(agent_ids) > 1:
        return "ambiguous", None
    if agent_ids:
        return "agent", next(iter(agent_ids))
    return "shared", None


def _is_under(child: str, parent: str) -> bool:
    """True when ``child`` equals or sits inside ``parent`` (already real)."""
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def _static_path_user_state(real_path: str) -> tuple:
    """``(state, owner)`` against the *static* Agent workspace registration.

    The capability-preview counterpart of the handler rule, resolved without a
    request identity: a public preview request carries none, yet it must still
    be told that a path is somebody's private file. The ``user/`` container
    belongs to an Agent workspace, so a tenant shared root contributes no
    container; the most specific workspace wins.
    """
    from common.state_dir import classify_agent_user_path
    if not real_path:
        return "none", None
    real = os.path.realpath(real_path)
    best = None
    for root, agent_id in _tenant_workspace_root_owners():
        if not agent_id:
            continue
        candidate = os.path.realpath(root)
        if not _is_under(real, candidate):
            continue
        if best is None or len(candidate) > len(best):
            best = candidate
    if best is None:
        return "none", None
    return classify_agent_user_path(real, best)


def _user_subtree_roots(ctx, roots=None) -> list:
    """Roots for the user-container rule: the caller's own plus the static ones.

    The static half keeps the rule sound for a *platform-root* path pointing
    into another tenant's Agent workspace — a path the caller's own root list
    would not name — so a platform administrator's platform eligibility cannot
    carry them into a member's ``user/`` subtree. Both halves are server-derived.
    """
    from channel.web.web_channel import _db_file_root_owners
    from channel.web.web_channel import _tenant_workspace_root_owners
    merged = list(roots) if roots is not None else (
        list(_db_file_root_owners(ctx)) if ctx is not None else [])
    seen = {os.path.realpath(root) for root, _ in merged}
    for workspace, agent_id in _tenant_workspace_root_owners():
        if not agent_id:
            continue
        real = os.path.realpath(workspace)
        if real not in seen:
            seen.add(real)
            merged.append((workspace, agent_id))
    return merged


def _user_subtree_state(real_path: str, roots: list) -> tuple:
    """``(state, owner)`` for the Agent workspace most specifically owning the path.

    ``state`` is ``common.state_dir.classify_agent_user_path``'s vocabulary
    (``none``/``container``/``user``/``unowned``). It is computed from the
    *real* path, so a symlink alias is judged by where it actually points.
    """
    from common.state_dir import classify_agent_user_path
    if not real_path:
        return "none", None
    real = os.path.realpath(real_path)
    best = None
    for workspace, agent_id in roots or ():
        if not agent_id:
            continue
        candidate = os.path.realpath(workspace)
        if not _is_under(real, candidate):
            continue
        if best is None or len(candidate) > len(best):
            best = candidate
    if best is None:
        return "none", None
    return classify_agent_user_path(real, best)


def _owner_of_db_path(ctx, real_path: str) -> tuple:
    """Single-resource :func:`_db_path_owner` against the caller's roots."""
    from channel.web.web_channel import _db_file_root_owners
    return _db_path_owner(real_path, _db_file_root_owners(ctx))


def _db_path_visible(ctx, real_path: str, roots: list = None) -> bool:
    """False when ``real_path`` is ambiguous, another member's private Agent, or
    somebody else's ``user/<user_id>`` files in a shared Agent.

    The single ownership rule for the file surface: apply it to the path that was
    actually addressed, never to the Agent the request merely *declared*.
    ``roots`` lets a caller listing many entries resolve the tenant's roots once.
    The user-container rule is decided first and by ownership alone, so neither
    sharing the Agent nor an administrator qualification widens it; the bare
    container stays visible because its entries are filtered one at a time.
    """
    from channel.web.web_channel import _db_file_root_owners
    from channel.web.web_channel import _db_path_owner_forbidden
    if not real_path:
        return False
    if roots is None:
        roots = _db_file_root_owners(ctx)
    state, owner = _user_subtree_state(real_path, _user_subtree_roots(ctx, roots))
    if state == "unowned":
        return False
    if state == "user":
        return bool(getattr(ctx, "user_id", None)) and owner == ctx.user_id
    if state == "container":
        return True
    kind, agent_id = _db_path_owner(real_path, roots)
    if kind == "ambiguous":
        return False
    if kind == "agent" and _db_path_owner_forbidden(ctx, agent_id):
        return False
    return True


def _path_in_user_subtree(real_path: str) -> bool:
    """True when the path belongs to a user's private ``user/<id>`` subtree.

    Identity-free (static registration), used for response hardening: a private
    file's response must not be cached by a shared cache.
    """
    try:
        state, _ = _static_path_user_state(real_path)
    except Exception:
        return True  # fail closed: an unanswerable lookup is not "public"
    return state in ("user", "unowned")


def _authorize_db_file_path(ctx, real_path: str) -> tuple:
    """Authorize ``real_path`` against database file roots.

    Returns ``(allowed, via)`` where ``via`` is ``platform``, ``tenant``,
    ``forbidden``, or ``not_found``. Missing tenant context raises 403.
    Platform-root reads by a platform admin are audited as ``platform.file.read``.

    Tenant containment alone is not authority: the path's *owning Agent* must
    also pass the private-owner rule, so a member cannot name a shared Agent and
    then address another member's private Agent workspace (absolute or nested).
    """
    from channel.web.web_channel import _db_file_serve_roots
    from channel.web.web_channel import _db_path_owner_forbidden
    from channel.web.web_channel import _platform_file_root
    if not getattr(ctx, "tenant_id", None):
        raise web.HTTPError("403 Forbidden")

    from auth.service import get_identity_service

    svc = get_identity_service()
    real_path = os.path.realpath(real_path)

    # The user subtree is decided by ownership alone and *before* every
    # pass-through below, so neither the platform-root eligibility nor an
    # administrator qualification can open another member's shared-Agent files.
    user_state, user_owner = _user_subtree_state(
        real_path, _user_subtree_roots(ctx))
    if user_state == "unowned":
        return False, "not_found"
    if user_state == "user" and user_owner != getattr(ctx, "user_id", None):
        return False, "forbidden"

    platform_root = os.path.realpath(_platform_file_root())
    try:
        under_platform = os.path.commonpath([real_path, platform_root]) == platform_root
    except ValueError:
        under_platform = False

    if under_platform:
        if not getattr(ctx, "is_platform_admin", False):
            return False, "forbidden"
        try:
            svc.record_audit(
                action="platform.file.read",
                target=real_path,
                actor_user_id=getattr(ctx, "user_id", None),
                actor_username=getattr(ctx, "username", None),
                tenant_id=ctx.tenant_id,
            )
        except Exception as e:  # pragma: no cover - audit is best effort
            logger.warning(f"[WebChannel] platform.file.read audit unavailable: {e}")
        return True, "platform"

    contained = False
    for root in _db_file_serve_roots(ctx):
        root = os.path.realpath(root)
        if root == platform_root:
            continue
        try:
            if os.path.commonpath([real_path, root]) == root:
                contained = True
                break
        except ValueError:
            continue
    if not contained:
        return False, "not_found"

    kind, agent_id = _owner_of_db_path(ctx, real_path)
    if kind == "ambiguous":
        return False, "not_found"
    if kind == "agent" and _db_path_owner_forbidden(ctx, agent_id):
        return False, "forbidden"
    return True, "tenant"


class FileServeHandler:
    def GET(self):
        from channel.web.web_channel import _authorize_db_file_path
        from channel.web.web_channel import _require_agent_action
        from channel.web.web_channel import _require_private_owner
        from channel.web.web_channel import _require_tenant_agent_binding
        with _file_identity_scope() as ctx:
            try:
                params = web.input(path="", agent_id="")
                file_path = params.path
                if not file_path or not os.path.isabs(file_path):
                    raise web.notfound()
                # Resolve symlinks and confine access to tenant/platform roots;
                # never fall back to operator home or the whole filesystem.
                file_path = os.path.realpath(file_path)
                if params.agent_id:
                    agent_id = _require_tenant_agent_binding(ctx, params.agent_id)
                    _require_private_owner(ctx, agent_id)
                    _require_agent_action(ctx, agent_id, "read", "agent.read")
                    # A declared Agent is a cross-check, never the authority:
                    # the path's own owner is what authorizes the read, so a
                    # mismatch (shared Agent claimed for a private Agent's file)
                    # is refused rather than trusted.
                    owner_kind, owner_agent = _owner_of_db_path(ctx, file_path)
                    if owner_kind == "agent" and owner_agent != agent_id:
                        raise web.notfound()
                allowed, via = _authorize_db_file_path(ctx, file_path)
                if not allowed:
                    if via == "forbidden":
                        raise web.forbidden()
                    raise web.notfound()
                if not os.path.isfile(file_path):
                    raise web.notfound()
                content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
                file_name = os.path.basename(file_path)
                from urllib.parse import quote
                web.header('Content-Type', content_type)
                web.header('Content-Disposition', f"inline; filename*=UTF-8''{quote(file_name)}")
                if _path_in_user_subtree(file_path):
                    # A member's private file: never let a shared cache retain it.
                    web.header('Cache-Control', 'private, no-store')
                else:
                    web.header('Cache-Control', 'public, max-age=3600')
                with open(file_path, 'rb') as f:
                    return f.read()
            except web.HTTPError:
                raise
            except Exception as e:
                logger.error(f"[WebChannel] Error serving file: {e}")
                raise web.notfound()


class PreviewHandler:
    """
    Directory-mounted file server for the preview panel: /preview/<token>/<relpath>

    Unlike /api/file (single file, query param) this mounts the file's directory,
    so relative assets inside a generated HTML page resolve normally. The token is
    HMAC-signed, which is what authorizes the request - the sandboxed iframe can't
    send the auth cookie.
    """

    def GET(self, path_info):
        # Preview is capability-authorized: the URL carries an HMAC-signed
        # directory token because the sandboxed iframe (opaque origin) cannot
        # send the session cookie. This holds in database mode too, so the old
        # blanket 503 gate is gone (task 2.4, open-database-runtime).
        from channel.web.web_channel import _decode_dir_token
        from channel.web.web_channel import _inject_preview_chrome
        from channel.web.web_channel import _is_path_allowed
        try:
            token, _, rel_path = (path_info or "").partition("/")
            if not token or not rel_path:
                raise web.notfound()

            from urllib.parse import unquote
            rel_path = unquote(rel_path)

            try:
                base_dir = _decode_dir_token(token)
            except ValueError:
                raise web.notfound()

            full_path = os.path.realpath(os.path.join(base_dir, rel_path))
            base_real = os.path.realpath(base_dir)
            # Confine to the mounted directory, then to the globally allowed roots.
            if os.path.commonpath([full_path, base_real]) != base_real:
                raise web.notfound()
            if not _is_path_allowed(full_path) or not os.path.isfile(full_path):
                raise web.notfound()
            # Ownership is re-derived at consumption: a token minted for a
            # private workspace is not a bearer grant to it. Public workspace
            # files keep working without identity (the token is the authority).
            if not _preview_consumer_may_read(full_path):
                raise web.notfound()

            content_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
            web.header('Content-Type', content_type)
            # A member's private file (their own ``user/<id>`` subtree, or a
            # private Agent's) must not be retained by a shared cache; public
            # workspace previews keep the existing revalidate-every-time policy.
            if _path_in_user_subtree(full_path):
                web.header('Cache-Control', 'private, no-store')
            else:
                web.header('Cache-Control', 'no-cache')
            web.header('X-Content-Type-Options', 'nosniff')
            is_html = content_type.startswith("text/html")
            if is_html:
                # Agent-generated pages are untrusted. The CSP sandbox forces an
                # opaque origin even when the page is opened as a top-level tab,
                # so it can't read the console's localStorage auth token; the
                # panel's iframe already applies the same flags.
                #
                # No frame-ancestors here: the desktop renderer is loaded from
                # file:// (or the Vite dev server), so 'self' would block its
                # preview iframe outright. The sandbox is what carries the
                # security guarantee; framing alone reveals nothing extra.
                web.header(
                    'Content-Security-Policy',
                    "sandbox allow-scripts allow-popups allow-forms allow-modals",
                )
            with open(full_path, 'rb') as f:
                data = f.read()
            return _inject_preview_chrome(data) if is_html else data
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Error serving preview: {e}")
            raise web.notfound()


