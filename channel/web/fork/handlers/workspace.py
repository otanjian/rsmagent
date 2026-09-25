"""Fork web layer (change adopt-upstream-web-split, design D2).

Fork-owned implementation, moved verbatim out of the former
channel/web/web_channel.py monolith. Upstream's api/ modules are not
edited. Imports inside function bodies are lazy so these modules can
reference each other without import cycles.
"""

from __future__ import annotations
from bridge.context import *
from common.log import logger
from urllib.parse import quote
import json
import os
import web


def _project_brand_name() -> str:
    """Project the effective brand name for the legacy /config.title field.

    Returns the published brand name so the compatibility projection, browser
    title and welcome screen stay in sync. On failure it returns the default.
    This is a read-only projection: it must never become a write path.
    """
    from channel.web.web_channel import _branding_service
    try:
        svc = _branding_service()
        record = svc.get_published()
        return record.get("brand_name") or "容大AI"
    except Exception:
        pass
    return "容大AI"


def _workspace_system_service(ctx, svc):
    """State-root workspace for system assets, confined to the caller's tenant.

    ``_system_workspace_service()`` resolves ``state_root()``, which with no
    ``agent_id`` in the ambient identity falls back to the *global default*
    Agent. In database mode that would let a non-default tenant read another
    tenant's ``MEMORY.md``/``knowledge/`` through the preview editor's
    state-root fallback. Anchor to the caller's tenant-bound default Agent
    instead; when the tenant has no Agent at all, reuse the session workspace
    root (already tenant-scoped) rather than a global directory.
    """
    from channel.web.web_channel import _db_path_owner_forbidden
    from channel.web.web_channel import _resolve_tenant_default_agent
    from channel.web.web_channel import _system_workspace_service
    if ctx is None:
        return _system_workspace_service()
    from agent.workspace.service import WorkspaceService
    resolved = _resolve_tenant_default_agent(ctx)
    # The default must also pass the private-owner rule: when a tenant has no
    # shared Agent the resolution order can land on a private one, which the
    # caller is not allowed to read. Reuse the tenant-scoped session root then.
    if resolved and not _db_path_owner_forbidden(ctx, resolved):
        try:
            from agent.registry import get_agent_registry
            return WorkspaceService(get_agent_registry().get(resolved).workspace)
        except (KeyError, ValueError, TypeError):
            pass
    return svc


def _workspace_request_scope(ctx, session_id: str, agent_id: Optional[str]) -> str:
    """Validate the file panel's ``agent``/``session`` selectors.

    Returns the resolved (tenant-bound) agent id. An Agent bound to another
    tenant or privately owned by someone else is refused (404/403), and a
    session the caller does not own is refused too, mirroring the other
    session-scoped console routes.
    """
    from channel.web.web_channel import _require_owned_session
    from channel.web.web_channel import _require_private_owner
    from channel.web.web_channel import _require_tenant_agent_binding
    resolved = _require_tenant_agent_binding(ctx, agent_id)
    _require_private_owner(ctx, resolved)
    if session_id:
        _require_owned_session(ctx, session_id, resolved)
    return resolved


def _workspace_path_allowed(ctx, roots: list):
    """An ``abs_path -> bool`` admission rule for the caller's file panel.

    One seam for the whole file surface's ownership rule
    (:func:`channel.web.web_channel._db_path_visible`): a private Agent, an
    ambiguous root, and another member's ``user/<user_id>`` in a shared Agent
    are all invisible. Used both to prune a recursive search before it walks
    into a directory and to filter one listing level.
    """
    from channel.web.web_channel import _db_path_visible
    ctx_roots = roots

    def allowed(abs_path: str) -> bool:
        try:
            return _db_path_visible(ctx, os.path.realpath(abs_path), ctx_roots)
        except (TypeError, ValueError):
            return False

    return allowed


def _visible_entries(ctx, svc, entries: list) -> list:
    """Drop entries the caller may not read (private-Agent / user-subtree).

    Applied before decorating so a hidden entry never gets `raw_url` /
    `preview_url` minted for it, and directory names do not leak either.
    """
    from channel.web.web_channel import _db_file_root_owners
    if ctx is None or not getattr(ctx, "tenant_id", None):
        return entries
    allowed = _workspace_path_allowed(ctx, _db_file_root_owners(ctx))
    visible = []
    for entry in entries:
        abs_path = entry.get("abs_path") or os.path.join(svc.root, entry.get("path") or "")
        if allowed(abs_path):
            visible.append(entry)
    return visible


class WorkspaceTreeHandler:
    def GET(self):
        from channel.web.web_channel import _db_file_root_owners
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _decorate_entry
        from channel.web.web_channel import _visible_entries
        from channel.web.web_channel import _workspace_service
        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            try:
                params = web.input(path='', show_hidden='', session='', agent='')
                agent_id = _workspace_request_scope(
                    ctx, params.session or None, params.agent or None)
                svc = _workspace_service(params.session or None, agent_id)
                # Filter inside the listing (before the entry cap) so another
                # member's user/<id> neither appears nor crowds out the
                # caller's own entries; the post-filter below is the same rule
                # applied once more to the decorated rows.
                allowed = _workspace_path_allowed(
                    ctx, _db_file_root_owners(ctx))
                # The addressed directory is subject to the same rule as its
                # entries. Filtering alone answers "exists but empty" for
                # another member's user/<id>, which is the path-existence leak
                # the rule forbids; refuse it like any other invisible path.
                if not allowed(os.path.realpath(svc.resolve(params.path))):
                    raise web.HTTPError('403 Forbidden')
                result = svc.list_dir(params.path,
                                      show_hidden=params.show_hidden == '1',
                                      allow_entry=allowed)
                result["entries"] = [
                    _decorate_entry(svc, e)
                    for e in _visible_entries(ctx, svc, result["entries"])
                ]
                return json.dumps({"status": "success", **result}, ensure_ascii=False)
            except web.HTTPError:
                raise
            except (ValueError, FileNotFoundError) as e:
                return json.dumps({"status": "error", "message": str(e)})
            except Exception as e:
                logger.error(f"[WebChannel] Workspace tree error: {e}")
                return json.dumps({"status": "error", "message": str(e)})


class WorkspaceSearchHandler:
    def GET(self):
        from channel.web.web_channel import _db_file_root_owners
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _decorate_entry
        from channel.web.web_channel import _visible_entries
        from channel.web.web_channel import _workspace_service
        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            try:
                params = web.input(q='', limit='30', session='', agent='')
                try:
                    limit = max(1, min(100, int(params.limit)))
                except (TypeError, ValueError):
                    limit = 30
                agent_id = _workspace_request_scope(
                    ctx, params.session or None, params.agent or None)
                svc = _workspace_service(params.session or None, agent_id)
                # Prune before descending: another member's user/<id> must not
                # contribute results or consume the search budget.
                result = svc.search(
                    params.q, limit=limit,
                    allow_dir=_workspace_path_allowed(
                        ctx, _db_file_root_owners(ctx)))
                result["results"] = [
                    _decorate_entry(svc, e)
                    for e in _visible_entries(ctx, svc, result["results"])
                ]
                return json.dumps({"status": "success", **result}, ensure_ascii=False)
            except web.HTTPError:
                raise
            except Exception as e:
                logger.error(f"[WebChannel] Workspace search error: {e}")
                return json.dumps({"status": "error", "message": str(e)})


class WorkspaceResolveHandler:
    """
    Metadata + preview/raw URLs for one entry, given a relative or absolute path.

    Directories resolve as well (the client then browses instead of previewing),
    just without the file URLs.
    """

    def GET(self):
        from channel.web.web_channel import _authorize_db_file_path
        from channel.web.web_channel import _build_preview_url
        from channel.web.web_channel import _db_path_visible
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _is_system_asset_rel
        from channel.web.web_channel import _workspace_service
        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            try:
                from agent.protocol.artifact import classify_kind, is_previewable
                params = web.input(path='', session='', agent='')
                raw_path = (params.path or '').strip()
                if not raw_path:
                    return json.dumps({"status": "error", "message": "path is required"})

                agent_id = _workspace_request_scope(
                    ctx, params.session or None, params.agent or None)
                svc = _workspace_service(params.session or None, agent_id)
                if os.path.isabs(os.path.expanduser(raw_path)):
                    abs_path = os.path.realpath(os.path.expanduser(raw_path))
                    # Absolute paths are authorized against the *caller's*
                    # tenant roots, never the static all-tenant list behind
                    # /preview (which has no request identity to scope by).
                    allowed, via = _authorize_db_file_path(ctx, abs_path)
                    if not allowed:
                        if via == "forbidden":
                            raise web.forbidden()
                        raise web.notfound()
                    is_dir = os.path.isdir(abs_path)
                    if not is_dir and not os.path.isfile(abs_path):
                        return json.dumps({"status": "error", "message": "File not found"})
                    kind = "directory" if is_dir else classify_kind(abs_path)
                    entry = {
                        "name": os.path.basename(abs_path),
                        "path": svc.to_rel(abs_path),
                        "abs_path": abs_path,
                        "is_dir": is_dir,
                        "kind": kind,
                        "previewable": (not is_dir) and is_previewable(kind),
                        "size": 0 if is_dir else os.path.getsize(abs_path),
                        "mtime": os.path.getmtime(abs_path),
                    }
                else:
                    try:
                        entry = svc.stat_file(raw_path)
                    except FileNotFoundError:
                        # Memory/knowledge live in the Agent's workspace, not the
                        # project. Retry there so their cards still preview when
                        # a project is open — scoped to the caller's tenant.
                        if _is_system_asset_rel(raw_path):
                            entry = _workspace_system_service(ctx, svc).stat_file(raw_path)
                        else:
                            raise
                    # Relative addressing is not an ownership bypass: the
                    # resolved abs_path decides, so a private Agent workspace
                    # nested under the shared root stays invisible.
                    if not _db_path_visible(ctx, entry["abs_path"]):
                        raise web.notfound()

                # A directory has nothing to serve; the client browses into it.
                if not entry["is_dir"]:
                    entry["raw_url"] = f"/api/file?path={quote(entry['abs_path'])}"
                    entry["preview_url"] = _build_preview_url(entry["abs_path"])
                return json.dumps({"status": "success", "file": entry}, ensure_ascii=False)
            except web.HTTPError:
                raise
            except (ValueError, FileNotFoundError) as e:
                return json.dumps({"status": "error", "message": str(e)})
            except Exception as e:
                logger.error(f"[WebChannel] Workspace resolve error: {e}")
                return json.dumps({"status": "error", "message": str(e)})


class WorkspaceMetaHandler:
    def GET(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _workspace_service
        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            try:
                params = web.input(session='', agent='')
                agent_id = _workspace_request_scope(
                    ctx, params.session or None, params.agent or None)
                svc = _workspace_service(params.session or None, agent_id)
                return json.dumps({"status": "success", **svc.meta()}, ensure_ascii=False)
            except web.HTTPError:
                raise
            except Exception as e:
                logger.error(f"[WebChannel] Workspace meta error: {e}")
                return json.dumps({"status": "error", "message": str(e)})


class WorkspaceReadHandler:
    """
    Text content of one workspace file, for the preview panel's editor.

    Returns the `mtime` the client passes back on save and an `editable` flag,
    so the editor never opens a file it would be unable to write back.
    """

    def GET(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _editable_target
        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            try:
                params = web.input(path='', session='', agent='')
                raw_path = (params.path or '').strip()
                if not raw_path:
                    return json.dumps({"status": "error", "message": "path is required"})
                agent_id = _workspace_request_scope(
                    ctx, params.session or None, params.agent or None)
                svc, rel = _editable_target(
                    raw_path, params.session or None, agent_id, ctx=ctx)
                return json.dumps({"status": "success", **svc.read_text(rel)}, ensure_ascii=False)
            except web.HTTPError:
                raise
            except (ValueError, FileNotFoundError) as e:
                return json.dumps({"status": "error", "message": str(e)})
            except Exception as e:
                logger.error(f"[WebChannel] Workspace read error: {e}")
                return json.dumps({"status": "error", "message": str(e)})


class WorkspaceWriteHandler:
    """
    Save edited text back to a workspace file.

    A human editing a file in the console is not an agent tool call, so the
    session's agent permission mode does not apply here; the guard is the
    workspace boundary enforced by `_editable_target`.

    `expected_mtime` carries the timestamp the editor loaded. When it no longer
    matches, the response is `code: "conflict"` so the client can offer to
    reload or overwrite rather than silently discarding the newer content -
    which the agent may well have written mid-edit.
    """

    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _editable_target
        from channel.web.web_channel import _is_memory_rel
        from channel.web.web_channel import _mark_memory_dirty
        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            # A console write funnels through the unified origin/CSRF gate before
            # any identity or filesystem work, like every other management write.
            from channel.web.auth_handlers import require_management_write
            require_management_write()
            try:
                from agent.workspace.service import WorkspaceConflictError

                body = json.loads(web.data() or b'{}')
                raw_path = (body.get("path") or "").strip()
                if not raw_path:
                    return json.dumps({"status": "error", "message": "path is required"})
                content = body.get("content")
                if not isinstance(content, str):
                    return json.dumps({"status": "error", "message": "content must be a string"})

                agent_id = _workspace_request_scope(
                    ctx, body.get("session") or None, body.get("agent") or None)
                svc, rel = _editable_target(
                    raw_path, body.get("session") or None, agent_id, ctx=ctx)
                try:
                    result = svc.write_text(rel, content, expected_mtime=body.get("expected_mtime"))
                except WorkspaceConflictError as e:
                    return json.dumps({"status": "error", "code": "conflict", "message": str(e)})

                # A memory file feeds the vector index; re-embed it on edit so search
                # doesn't keep returning the stale pre-edit text.
                if _is_memory_rel(rel):
                    _mark_memory_dirty(agent_id)

                logger.info(f"[WebChannel] Workspace file saved: {result['path']} ({result['size']} bytes)")
                return json.dumps({"status": "success", **result}, ensure_ascii=False)
            except web.HTTPError:
                raise
            except (ValueError, FileNotFoundError) as e:
                return json.dumps({"status": "error", "message": str(e)})
            except PermissionError:
                return json.dumps({"status": "error", "message": "permission denied"})
            except Exception as e:
                logger.error(f"[WebChannel] Workspace write error: {e}")
                return json.dumps({"status": "error", "message": str(e)})


class ProjectsHandler:
    """List the project picker state for a session (current + recents)."""

    def GET(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _project_state
        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            try:
                params = web.input(session='', agent='')
                state = _project_state(params.session or None, params.agent or None)
                return json.dumps({"status": "success", **state}, ensure_ascii=False)
            except web.HTTPError:
                raise
            except Exception as e:
                logger.error(f"[WebChannel] Projects list error: {e}")
                return json.dumps({"status": "error", "message": str(e)})


class ProjectSelectHandler:
    """Bind a session to a project directory, or clear it (project_dir=null).

    The value may be a relative identifier as returned by the scoped browser or an
    absolute path recorded before this change. Either way it is re-resolved here,
    at *selection* time (task 6.2): the session's durable owner is re-checked
    through the module's own seam, and the path is re-validated through
    ``common.safe_fs``, so a directory replaced by a symlink after the listing --
    or a path that left the caller's root -- refuses instead of binding the
    session to the new target.
    """

    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _project_state
        from channel.web.web_channel import _render_project_refusal
        from channel.web.web_channel import _require_owned_session
        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            try:
                from agent.workspace import project_browser
                from agent.workspace import project_store
                body = json.loads(web.data() or b"{}")
                session_id = (body.get("session") or body.get("session_id") or "").strip()
                agent_id = body.get("agent") or body.get("agent_id")
                if not session_id:
                    return json.dumps({"status": "error", "message": "session is required"})
                _require_owned_session(ctx, session_id, agent_id)
                raw = body.get("project_dir")
                if raw in (None, ""):
                    applied = project_store.set_project_dir(session_id, None, agent_id)
                else:
                    resolved = project_browser.resolve_selection(
                        _project_identity(), raw, session_id=session_id,
                        session_owner=_project_session_owner(ctx, agent_id))
                    applied = project_store.set_project_dir(
                        session_id, resolved, agent_id
                    )
                # Retarget an already-instantiated session agent immediately, so the
                # change takes effect on the next message without a fresh get_agent.
                try:
                    from bridge.bridge import Bridge
                    ab = Bridge().get_agent_bridge()
                    agent = ab.get_cached_agent(session_id, agent_id)
                    if agent is not None and getattr(agent, "apply_project_dir", None):
                        agent.apply_project_dir(applied)
                except Exception as e:
                    logger.debug(f"[WebChannel] project apply-to-agent skipped: {e}")
                state = _project_state(session_id, agent_id)
                return json.dumps({"status": "success", **state}, ensure_ascii=False)
            except (ValueError, FileNotFoundError) as e:
                return json.dumps({"status": "error", "message": str(e)})
            except web.HTTPError:
                raise
            except Exception as e:
                logger.error(f"[WebChannel] Project select error: {e}")
                return _render_project_refusal(e)


class ProjectCreateHandler:
    """Create a new project folder under the projects root and select it."""

    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _project_state
        from channel.web.web_channel import _require_owned_session
        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            try:
                from agent.workspace import project_store
                body = json.loads(web.data() or b"{}")
                session_id = (body.get("session") or body.get("session_id") or "").strip()
                agent_id = body.get("agent") or body.get("agent_id")
                name = (body.get("name") or "").strip()
                if not name:
                    return json.dumps({"status": "error", "message": "name is required"})
                if session_id:
                    _require_owned_session(ctx, session_id, agent_id)
                path = project_store.create_project(name)
                if session_id:
                    project_store.set_project_dir(session_id, path, agent_id)
                    try:
                        from bridge.bridge import Bridge
                        ab = Bridge().get_agent_bridge()
                        agent = ab.get_cached_agent(session_id, agent_id)
                        if agent is not None and getattr(agent, "apply_project_dir", None):
                            agent.apply_project_dir(path)
                    except Exception as e:
                        logger.debug(f"[WebChannel] project apply-to-agent skipped: {e}")
                state = _project_state(session_id or None, agent_id)
                return json.dumps({"status": "success", "path": path, **state}, ensure_ascii=False)
            except (ValueError, FileExistsError) as e:
                return json.dumps({"status": "error", "message": str(e)})
            except web.HTTPError:
                raise
            except Exception as e:
                logger.error(f"[WebChannel] Project create error: {e}")
                return json.dumps({"status": "error", "message": str(e)})


class ProjectOrderHandler:
    """Persist the user's chosen sidebar order of project spaces."""

    def POST(self):
        from channel.web.web_channel import _db_scope
        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            try:
                from agent.workspace import project_store
                body = json.loads(web.data() or b"{}")
                order = body.get("order")
                if not isinstance(order, list):
                    return json.dumps({"status": "error", "message": "order must be a list"})
                saved = project_store.set_order(order)
                return json.dumps({"status": "success", "order": saved}, ensure_ascii=False)
            except web.HTTPError:
                raise
            except Exception as e:
                logger.error(f"[WebChannel] Project order error: {e}")
                return json.dumps({"status": "error", "message": str(e)})


class ProjectManageHandler:
    """Rename (PUT) or delete (DELETE) a project record.

    Neither touches the folder on disk: a rename only sets a display name, and a
    delete only forgets the RongAI record and unbinds any sessions (they revert
    to the default workspace). The files stay exactly where they are.
    """

    def PUT(self):
        from channel.web.web_channel import _db_scope
        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            try:
                from agent.workspace import project_store
                body = json.loads(web.data() or b"{}")
                path = (body.get("path") or "").strip()
                if not path:
                    return json.dumps({"status": "error", "message": "path is required"})
                name = project_store.rename_project(path, body.get("name") or "")
                return json.dumps({"status": "success", "name": name}, ensure_ascii=False)
            except web.HTTPError:
                raise
            except Exception as e:
                logger.error(f"[WebChannel] Project rename error: {e}")
                return json.dumps({"status": "error", "message": str(e)})

    def DELETE(self):
        from channel.web.web_channel import _db_scope
        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            try:
                from agent.workspace import project_store
                body = json.loads(web.data() or b"{}")
                path = (body.get("path") or "").strip()
                agent_id = body.get("agent") or body.get("agent_id")
                if not path:
                    return json.dumps({"status": "error", "message": "path is required"})
                unbound = project_store.delete_project(path, agent_id)
                return json.dumps({"status": "success", "unbound": unbound}, ensure_ascii=False)
            except web.HTTPError:
                raise
            except Exception as e:
                logger.error(f"[WebChannel] Project delete error: {e}")
                return json.dumps({"status": "error", "message": str(e)})


def _project_identity():
    """The verified ambient identity of this request.

    Never a request value: ``_db_scope`` established it from the session and the
    tenant selection, and every project-browser/import call is scoped by it.
    """
    from common.runtime_identity import current_identity
    return current_identity()


def _project_quota_reserve(identity):
    """A quota-reservation callback for one import, bound to the caller's root."""
    from agent.workspace import project_browser
    from channel.web import project_import

    def reserve(plan):
        return project_import.reserve_project_slot(
            identity, plan, project_browser.trusted_root(identity))

    return reserve


def _project_field(params, *names) -> str:
    """The first non-empty field of ``params`` (a dict or a ``web.storage``)."""
    for name in names:
        value = params.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _project_import_binding(source, name) -> Dict[str, Any]:
    """What a local import handle is bound to.

    ``source`` is the grant's identity: the resolved directory the user selected
    and the preview read, compared again at redemption so a handle cannot be
    replayed for a different directory (``_matches_payload`` compares that key).

    ``_name`` is the target name the preview showed. It is recorded under a
    private key because the import must publish *that* name, not whatever the
    publishing request carries: retargeting a confirmed import is a change to
    what the user agreed to, and the value is read back from the handle record
    instead of being re-trusted from the body. The preview's own normalization
    is applied here, so an honest request and its preview cannot disagree on
    spelling.
    """
    return {"source": os.path.realpath(source), "_name": str(name or "").strip()}


def _project_session_id(params) -> str:
    return _project_field(params, "session", "session_id")


def _project_require_session(ctx, params) -> str:
    """Verify the optional session binding an import was requested for.

    An import may name the chat session it is for; when it does, the session must
    be the caller's own and a web session. The binding is checked *before* any
    path is read, and it is re-checked at redemption, so a handle issued for one
    session cannot be redeemed for another.
    """
    from channel.web.web_channel import _require_owned_session
    session_id = _project_session_id(params)
    if session_id:
        _require_owned_session(ctx, session_id,
                               _project_field(params, "agent", "agent_id") or None)
    return session_id


def _project_session_owner(ctx, agent_id):
    """A ``session_owner`` seam for ``project_browser.resolve_selection``.

    Reports the durable owner of the addressed session the way the module needs
    it, so the re-verification happens inside the selection path instead of being
    assumed from an earlier check. ``_require_owned_session`` already refused
    another member's session and a non-web one; this re-reads the same row. A
    session with no row yet has no owner to conflict with -- the same rule the
    select/create handlers already apply, because a brand-new chat may be bound --
    while a row recorded for another channel type reports a value that can never
    equal a user id, so the module refuses it.
    """

    from channel.web.web_channel import _require_tenant_agent_binding
    def lookup(session_id: str):
        resolved = _require_tenant_agent_binding(ctx, agent_id)
        from agent.registry import get_agent_registry
        from agent.memory import get_conversation_store
        try:
            profile = get_agent_registry().get(resolved)
        except (KeyError, ValueError):
            return ""
        store = get_conversation_store(profile.workspace)
        with store._lock:
            con = store._connect()
            try:
                row = con.execute(
                    "SELECT owner, channel_type FROM sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()
            finally:
                con.close()
        if row is None:
            return str(getattr(ctx, "user_id", "") or "")
        owner, channel = row[0], row[1]
        if channel != "web":
            return "\x00not-a-web-session"
        return str(owner or "")

    return lookup


class ProjectBrowseHandler:
    """List the directories inside the caller's own project root (task 6.1).

    Response compatibility is deliberate. ``status``, ``path``, ``parent`` and
    ``dirs`` keep the names the console picker reads today and ``dirs`` entries
    keep ``{name, path}``; what changed is what the values *are* -- relative
    identifiers inside the caller's own root, where ``""`` is the root itself --
    plus three deliberate additions:

    * ``breadcrumbs`` -- the bounded path from the root to ``path`` in the same
      identifiers, ready to be sent back as a ``path`` parameter, so the console
      can render a trail without ever holding a host path;
    * ``scope`` -- the tenant/user the listing was resolved for, taken from the
      verified request context, not from a parameter;
    * ``parent`` -- the parent *inside* the root and ``None`` at the root, so "go
      up" can never climb above the member's private tree.

    A ``path`` recorded before this change arrives as an absolute host path; it is
    accepted only after the server proves it resolves inside the caller's own
    root (the single place that translation happens), and anything else is
    refused with a stable code. Every component is re-resolved per request through
    ``common.safe_fs`` (``O_NOFOLLOW``), so a directory swapped for a symlink
    refuses instead of enumerating whatever it now points at, and a member's
    private tree nested under an outer root is never offered. There is no
    ``agent.read`` or platform-``all`` bypass: ownership is the boundary.
    """

    def GET(self):
        from channel.web.web_channel import _DRIVES_SENTINEL
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _render_project_refusal
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            with _db_scope() as ctx:
                from agent.workspace import project_browser

                params = web.input(path='')
                raw = (getattr(params, 'path', '') or '').strip()
                if raw == _DRIVES_SENTINEL:
                    raise project_browser.ProjectBrowserError(
                        "驱动器列表不在本人项目根内，已拒绝",
                        code=project_browser.CODE_UNSAFE_PATH, status=403)
                identity = _project_identity()
                entry = project_browser.normalize_selection(identity, raw)
                result = project_browser.browse(identity, entry)
                return json.dumps({
                    "status": "success",
                    "path": result["path"],
                    "parent": result["parent"],
                    "dirs": result["dirs"],
                    "breadcrumbs": result["breadcrumbs"],
                    "scope": {"kind": "personal",
                              "tenant_id": ctx.tenant_id,
                              "user_id": ctx.user_id},
                }, ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Project browse error: {e}")
            return _render_project_refusal(e)


class ProjectImportPreviewHandler:
    """Preview what a controlled project import would create (tasks 6.3/6.4).

    Two transports, and the transport is what decides the boundary:

    * a **local** (desktop) selection posts ``source`` as an absolute host path
      and must be a loopback request carrying the per-start token
      (``channel.web.project_import``). The preview reports the target, the entry
      and byte counts, what would be skipped and whether something already holds
      the name, and issues the single-use handle the import must present;
    * a **remote** browser cannot see a server path at all: it posts multipart
      ``files`` + ``paths`` and the manifest is previewed from the bytes. The
      session is the grant, and no path is resolved.
    """

    def POST(self):
        from channel.web.web_channel import _chat_body
        from channel.web.web_channel import _chat_error
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _render_project_refusal
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            with _db_scope() as ctx:
                from agent.workspace import project_browser
                from channel.web import project_import

                if _project_request_is_upload():
                    params = project_import.upload_params()
                    _project_require_session(ctx, params)
                    payload = project_import.preview_upload_request(None, params)
                    return json.dumps({"status": "success",
                                       "transport": "upload", **payload},
                                      ensure_ascii=False)
                body = _chat_body()
                _project_require_session(ctx, body)
                project_import.require_local_transport(body)
                source = _project_field(body, "source", "path", "dir")
                if not source:
                    _chat_error("source is required", "400 Bad Request",
                                "invalid_request")
                identity = _project_identity()
                payload = project_browser.preview_import(
                    identity, source, name=body.get("name"),
                    source_roots=project_import.source_roots())
                handle = project_import.issue_handle(
                    identity, purpose=project_import.PURPOSE_PROJECT_IMPORT,
                    payload=_project_import_binding(source, body.get("name")),
                    session_id=_project_session_id(body))
                return json.dumps({
                    "status": "success",
                    "transport": "local",
                    "handle": handle,
                    "expires_in": project_import.HANDLE_TTL_SECONDS,
                    **payload,
                }, ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Project import preview error: {e}")
            return _render_project_refusal(e)


class ProjectImportHandler:
    """Publish a previewed local directory, or an uploaded one (tasks 6.3/6.4).

    The local transport is the dangerous one, because it asks the *server* to
    read a path the *client* named. Four conditions are required before that path
    is resolved at all: loopback, the per-start token, the verified database
    identity, and the single-use handle the preview issued for that exact source.
    A request that merely supplies a path has no handle and is refused with
    ``handle_required``; a handle issued to another member, already consumed, or
    for a different source is refused with ``handle_invalid``.

    The upload transport names no server path, so the session is the grant: the
    manifest is validated as plain downward relative paths, staged inside the
    member's own root and published with one rename -- the same staging, publish,
    rollback and quota code the local copy uses.

    Either way the import never overwrites an existing project, never targets the
    source directory, and leaves nothing behind on cancel or failure.
    """

    def POST(self):
        from channel.web.web_channel import _chat_body
        from channel.web.web_channel import _chat_error
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _render_project_refusal
        from channel.web.web_channel import _require_chat_csrf
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            with _db_scope() as ctx:
                _require_chat_csrf()
                from agent.workspace import project_browser
                from channel.web import project_import

                if _project_request_is_upload():
                    params = project_import.upload_params()
                    _project_require_session(ctx, params)
                    identity = _project_identity()
                    result = project_import.import_upload_request(
                        identity, params,
                        quota_reserve=_project_quota_reserve(identity))
                    return json.dumps({"status": "success",
                                       "transport": "upload", **result},
                                      ensure_ascii=False)

                body = _chat_body()
                source = _project_field(body, "source", "path", "dir")
                handle = _project_field(body, "handle")
                if not source:
                    _chat_error("source is required", "400 Bad Request",
                                "invalid_request")
                # Transport first: a request that is not allowed to name a server
                # path must not consume the capability that would let it.
                project_import.require_local_transport(body)
                _project_require_session(ctx, body)
                if not handle:
                    raise project_import.refuse(
                        "导入必须携带预览返回的一次性句柄",
                        code=project_import.CODE_HANDLE_REQUIRED, status=403)
                identity = _project_identity()
                record = project_import.consume_handle(
                    identity, handle,
                    purpose=project_import.PURPOSE_PROJECT_IMPORT,
                    session_id=_project_session_id(body) or None,
                    payload=_project_import_binding(source, body.get("name")))
                # Publish the target the preview showed, not whatever this
                # request happens to say: the handle's binding has already
                # refused a different source, so the recorded name is the same
                # value the user confirmed and the preview stays the contract.
                recorded = record.get("payload") or {}
                result = project_browser.import_directory(
                    identity, source, name=recorded.get("_name") or None,
                    source_roots=project_import.source_roots(),
                    quota_reserve=_project_quota_reserve(identity),
                    stage_hook=project_import.stage_hook(identity, record))
                project_import.purge_handles()
                return json.dumps({"status": "success",
                                   "transport": "local", **result},
                                  ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Project import error: {e}")
            return _render_project_refusal(e)


class ProjectImportCancelHandler:
    """Cancel an issued import handle (task 6.4).

    Cancelling is the member's decision about their own handle: nothing is
    published, a copy already in flight is abandoned at its publish step (the
    stage hook removes the staging tree and releases the reservation), and the
    handle is no longer redeemable. A handle that is not the caller's is reported
    as invalid, so the endpoint cannot be used to probe or cancel someone else's
    import.
    """

    def POST(self):
        from channel.web.web_channel import _chat_body
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _render_project_refusal
        from channel.web.web_channel import _require_chat_csrf
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            with _db_scope() as ctx:
                _require_chat_csrf()
                from channel.web import project_import

                body = _chat_body()
                handle = _project_field(body, "handle")
                identity = _project_identity()
                if not handle or not project_import.cancel_handle(
                        identity, handle,
                        purpose=project_import.PURPOSE_PROJECT_IMPORT):
                    raise project_import.refuse(
                        "导入句柄无效，已拒绝",
                        code=project_import.CODE_HANDLE_INVALID, status=403)
                return json.dumps({"status": "success", "cancelled": True},
                                  ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Project import cancel error: {e}")
            return _render_project_refusal(e)


def _project_request_is_upload() -> bool:
    """Whether this request is the multipart upload transport.

    Decided by the request's content type, before any body is read, so the two
    transports cannot be confused and a JSON body is never parsed as a manifest.
    """
    content_type = web.ctx.env.get("CONTENT_TYPE") or ""
    return content_type.lower().startswith("multipart/form-data")


