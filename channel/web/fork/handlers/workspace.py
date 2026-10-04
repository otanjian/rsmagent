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
import io
import json
import os
from typing import Dict, List, NamedTuple, Optional
import web

from common import safe_fs
from agent.workspace.service import (
    TRASH_RETENTION_SECONDS,
    join_rel,
    undeletable_reason,
    unuploadable_reason,
)


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
    ambiguous root, another member's ``user/<user_id>`` in a shared Agent and the
    shared root's own ``users/<user_id>`` account tree are all invisible. Used
    both to prune a recursive search before it walks into a directory and to
    filter one listing level.
    """
    from channel.web.web_channel import _db_path_visible
    from channel.web.web_channel import _shared_users_roots
    ctx_roots = roots
    # Resolved once per listing: the same rule runs for every entry, and neither
    # the tenant roots nor its shared root change between them.
    shared_roots = _shared_users_roots(ctx)

    def allowed(abs_path: str) -> bool:
        try:
            return _db_path_visible(ctx, os.path.realpath(abs_path), ctx_roots,
                                    shared_roots)
        except (TypeError, ValueError):
            return False

    return allowed


def _panel_source(ctx, session_id, agent_id, *, server_root=None):
    """The one source the file panel is allowed to show (task 3.6).

    A session bound to a local project must be served *that* project: reading
    the server's workspace instead would show a file tree the session's tools
    will never touch, and a refused local project must be reported rather than
    papered over with server files. A session with no desktop target keeps the
    pre-existing server root untouched.

    The returned :class:`Source` is internal -- ``root`` is an absolute local
    directory and never goes into a response (see ``Source.describe``).
    """
    from agent.desktop_local.source_resolver import source_for_session
    from common.runtime_identity import current_identity

    if server_root is None:
        from channel.web.web_channel import _get_workspace_root

        server_root = _get_workspace_root(session_id, agent_id)
    return source_for_session(session_id, agent_id,
                              server_root=server_root,
                              identity=current_identity())


def _panel_service(ctx, session_id, agent_id):
    """``(service, source)`` for the panel, or a refusal the caller must report.

    Raises ``web.HTTPError`` (503, JSON body) for a desktop session whose local
    project is not available here. That is the point of the shared resolver: the
    panel says "the local project is unavailable" instead of silently listing
    the server directory, which is the "回落默认目录" the requirement forbids.
    """
    from agent.workspace.service import WorkspaceService
    from channel.web.web_channel import _workspace_service

    source = _panel_source(ctx, session_id, agent_id)
    if source.is_desktop:
        if not source.available:
            raise web.HTTPError(
                "503 Service Unavailable",
                {"Content-Type": "application/json"},
                json.dumps({
                    "status": "error",
                    "code": "local_project_unavailable",
                    "message": source.refusal,
                    "source": source.describe(),
                }, ensure_ascii=False),
            )
        return WorkspaceService(source.root), source
    return _workspace_service(session_id, agent_id), source


def _panel_relative(source, raw, *, allow_root: bool = False):
    """``(relative, refusal)`` for a path the panel/editor addressed.

    Goes through the shared resolver so a local path is normalized and
    containment-checked exactly like an `@` reference or a tool argument; a
    local project never falls back to the server's path rules.
    """
    from agent.desktop_local.source_resolver import resolve_reference

    if source.is_desktop:
        resolved = resolve_reference(source, raw, allow_root=allow_root)
        if resolved.refusal:
            return None, resolved.refusal
        return resolved.relative, None
    if allow_root and not str(raw or "").strip():
        return "", None
    return str(raw or ""), None


def _panel_reference(source, raw) -> Any:
    """One resolved reference for the panel/editor, through the shared resolver."""
    from agent.desktop_local.source_resolver import resolve_reference

    return resolve_reference(source, raw)


def _panel_entry(source, entry: dict) -> dict:
    """Mark an entry with the source it came from.

    A desktop entry is a *local* reference: it gets no server ``raw_url`` /
    ``preview_url`` (the server cannot serve a device file, and minting one would
    invite the client to fetch a copy) and carries ``source: "desktop"`` so the
    client resolves it through the local project instead of ``@``-uploading it.
    """
    entry["source"] = source.kind
    if source.is_desktop:
        entry["local"] = True
    return entry


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
        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            try:
                params = web.input(path='', show_hidden='', session='', agent='')
                agent_id = _workspace_request_scope(
                    ctx, params.session or None, params.agent or None)
                # One source for the whole panel (task 3.6): the local project
                # when the session opened one, the server root otherwise. A local
                # project that cannot be honoured refuses here rather than
                # listing the server directory.
                svc, source = _panel_service(
                    ctx, params.session or None, agent_id)
                if source.is_desktop:
                    relative, refusal = _panel_relative(
                        source, params.path, allow_root=True)
                    if refusal:
                        return json.dumps({"status": "error", "code": "invalid_reference",
                                           "message": refusal}, ensure_ascii=False)
                    result = svc.list_dir(relative,
                                          show_hidden=params.show_hidden == '1')
                    result["entries"] = [
                        _panel_entry(source, e) for e in result["entries"]
                    ]
                    result["source"] = source.describe()
                    return json.dumps({"status": "success", **result},
                                      ensure_ascii=False)
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
                result["entries"] = _annotate_deletable(ctx, svc, agent_id, [
                    _panel_entry(source, _decorate_entry(svc, e))
                    for e in _visible_entries(ctx, svc, result["entries"])
                ])
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
                svc, source = _panel_service(
                    ctx, params.session or None, agent_id)
                if source.is_desktop:
                    result = svc.search(params.q, limit=limit)
                    result["results"] = [
                        _panel_entry(source, e) for e in result["results"]
                    ]
                    result["source"] = source.describe()
                    return json.dumps({"status": "success", **result},
                                      ensure_ascii=False)
                # Prune before descending: another member's user/<id> must not
                # contribute results or consume the search budget.
                result = svc.search(
                    params.q, limit=limit,
                    allow_dir=_workspace_path_allowed(
                        ctx, _db_file_root_owners(ctx)))
                result["results"] = _annotate_deletable(ctx, svc, agent_id, [
                    _panel_entry(source, _decorate_entry(svc, e))
                    for e in _visible_entries(ctx, svc, result["results"])
                ])
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
                svc, source = _panel_service(
                    ctx, params.session or None, agent_id)
                if source.is_desktop:
                    # A local project is addressed by relative id only: an
                    # absolute path here is refused instead of being authorized
                    # against server roots, because the two sources are not
                    # interchangeable (task 3.6).
                    resolved = _panel_reference(source, raw_path)
                    if resolved.refusal:
                        return json.dumps({"status": "error", "code": "invalid_reference",
                                           "message": resolved.refusal},
                                          ensure_ascii=False)
                    entry = svc.stat_file(resolved.relative)
                    entry = _panel_entry(source, entry)
                    entry["kind"] = ("directory" if entry["is_dir"]
                                     else classify_kind(resolved.absolute))
                    entry["previewable"] = bool(
                        not entry["is_dir"] and is_previewable(entry["kind"]))
                    return json.dumps({"status": "success", "file": entry,
                                       "source": source.describe()},
                                      ensure_ascii=False)
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
        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            try:
                params = web.input(session='', agent='')
                agent_id = _workspace_request_scope(
                    ctx, params.session or None, params.agent or None)
                svc, source = _panel_service(
                    ctx, params.session or None, agent_id)
                return json.dumps({"status": "success", **svc.meta(),
                                   "source": source.describe()},
                                  ensure_ascii=False)
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
        from channel.web.web_channel import _is_system_asset_rel
        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            try:
                params = web.input(path='', session='', agent='')
                raw_path = (params.path or '').strip()
                if not raw_path:
                    return json.dumps({"status": "error", "message": "path is required"})
                agent_id = _workspace_request_scope(
                    ctx, params.session or None, params.agent or None)
                svc, source = _panel_service(
                    ctx, params.session or None, agent_id)
                if source.is_desktop and not _is_system_asset_rel(raw_path):
                    # The editor must read the file the panel showed, on the same
                    # source: `_editable_target` resolves against server roots and
                    # would silently open a same-named server file instead. Memory
                    # and knowledge assets keep their server-side home.
                    resolved = _panel_reference(source, raw_path)
                    if resolved.refusal:
                        return json.dumps({"status": "error", "code": "invalid_reference",
                                           "message": resolved.refusal},
                                          ensure_ascii=False)
                    return json.dumps({"status": "success",
                                       **svc.read_text(resolved.relative)},
                                      ensure_ascii=False)
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
        from agent.tools.utils.memory_path import indexes_rel_path
        from channel.web.web_channel import _is_system_asset_rel
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
                # The session's own source decides, not the body (task 3.6): a
                # desktop session must save the file the panel showed -- the same
                # source it read from -- while memory / knowledge maintenance keeps
                # its original server-side home and permissions.
                svc, source = _panel_service(
                    ctx, body.get("session") or None, agent_id)
                if source.is_desktop and not _is_system_asset_rel(raw_path):
                    resolved = _panel_reference(source, raw_path)
                    if resolved.refusal:
                        return json.dumps({"status": "error", "code": "invalid_reference",
                                           "message": resolved.refusal},
                                          ensure_ascii=False)
                    rel = resolved.relative
                elif source.is_desktop:
                    svc, rel = _editable_target(
                        raw_path, body.get("session") or None, agent_id, ctx=ctx)
                else:
                    svc, rel = _editable_target(
                        raw_path, body.get("session") or None, agent_id, ctx=ctx)
                try:
                    result = svc.write_text(rel, content, expected_mtime=body.get("expected_mtime"))
                except WorkspaceConflictError as e:
                    return json.dumps({"status": "error", "code": "conflict", "message": str(e)})

                # A memory file feeds the vector index; re-embed it on edit so search
                # doesn't keep returning the stale pre-edit text.
                if indexes_rel_path(rel):
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


# ======================================================================
# Writable scope: where the file panel may create and delete
# ======================================================================
#
# Reading and writing text are governed by `_editable_target`, which answers
# "may this caller see this file". Uploading and deleting need a different
# answer -- "may this caller put something here / take something away" -- and
# the two must not be conflated: a member of a shared Agent can *read* the
# Agent's `knowledge/` and must still not be able to delete it.
#
# The range is derived from the Agent's visibility, never from the request body:
#
#   private Agent  -> `agents/<id>`          (the owner's own workspace)
#   shared Agent   -> `agents/<id>/user/<uid>` (this member's own subtree)
#
# Everything below funnels through `_resolve_write_target`, so there is exactly
# one place that decides, and one place to test.

#: Largest single upload the workspace panel accepts (200 MiB). The browser
#: refuses a larger file before sending, so this is the second line of defence
#: rather than the number the user sees.
WS_UPLOAD_MAX_BYTES = 200 * 1024 * 1024

#: Copy granularity for a streamed upload. The payload arrives spooled to a
#: temporary file by the multipart parser, so this bounds the buffer we hold and
#: nothing else.
WS_UPLOAD_CHUNK_BYTES = 1024 * 1024

#: Items one delete request may carry. The panel deletes the rows the caller
#: selected, not their expanded contents, so this is far above any real
#: selection while still bounding the work a single body can ask for.
WS_DELETE_MAX_TARGETS = 2000

#: Stable codes for the two ways a single file's *bytes* can be refused. Both
#: are conditions the panel must name in the reader's language, and one of them
#: (the size ceiling) is also reachable without the application seeing the
#: request at all -- the proxy answers 413 first. The panel maps the proxy's
#: status onto the same ``too_large`` wording, so the user reads one explanation
#: whichever layer stopped them (design D9, task 7.5).
WS_UPLOAD_TOO_LARGE = "too_large"
WS_UPLOAD_INCOMPLETE = "incomplete_upload"


class WorkspaceScopeError(Exception):
    """The request has no writable range here; ``code`` says which rule refused."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class WorkspaceUploadError(Exception):
    """A single file's bytes were refused; ``code`` is what the panel reports.

    Separate from :class:`WorkspaceScopeError` because the two answer different
    questions -- "may you write here" versus "did the payload arrive whole" --
    and the panel renders them with different wording.
    """

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _outside_own_directory() -> "WorkspaceScopeError":
    """The refusal for a path that is not inside the caller's own range.

    One code covers every such shape -- an escape (``../``), a colleague's
    subtree, the wrong Agent -- because the caller's remedy is the same and a
    prober learns nothing about which shapes the server happens to recognise.
    """
    return WorkspaceScopeError(
        "outside_own_directory", "outside the directory you may write to")


class WriteScope(NamedTuple):
    """The caller's writable range inside one Agent's workspace.

    ``agent_rel`` is the Agent's own directory and ``root_rel`` the range that
    may be written and deleted in. They differ only for a shared Agent, whose
    members may act inside their own ``user/<uid>`` subtree and not across the
    whole Agent.
    """

    agent_rel: str
    root_rel: str
    user_id: str
    visibility: str


def _write_scope(ctx, svc, agent_id: str) -> WriteScope:
    """Derive the caller's writable range, or refuse with a reason code."""
    from agent.registry import get_agent_registry
    from channel.web.fork.handlers.agents import _agent_visibility
    from common.runtime_identity import current_identity
    from common.state_dir import StateDirError, agent_user_root
    try:
        visibility = _agent_visibility(ctx, agent_id)
    except (KeyError, ValueError):
        raise WorkspaceScopeError("agent_not_found") from None

    profile = get_agent_registry().get(agent_id)
    agent_rel = svc.to_rel(os.path.realpath(profile.workspace))
    # A session that has opened a project directory moves the file API's root,
    # so the Agent's own workspace is then outside it. There is no honest range
    # to hand out in that state, and picking whichever of the two roots happens
    # to resolve would be a write the user never asked for.
    if agent_rel.startswith(".."):
        raise WorkspaceScopeError(
            "not_agent_workspace",
            "the file panel is not showing the Agent's own workspace")

    ident = current_identity()
    if agent_id and ident.agent_id != agent_id:
        ident = ident.derive(agent_id=agent_id)
    try:
        user_root = agent_user_root(ident, ensure=False)
    except StateDirError:
        raise WorkspaceScopeError("unsafe_user_directory") from None
    if user_root is None:
        raise WorkspaceScopeError("no_user", "no authenticated user")

    user_rel = svc.to_rel(str(user_root))
    if user_rel != join_rel(agent_rel, join_rel("user", user_root.name)):
        raise WorkspaceScopeError(
            "not_agent_workspace",
            "the file panel is not showing the Agent's own workspace")

    return WriteScope(
        agent_rel=agent_rel,
        root_rel=agent_rel if visibility == "private" else user_rel,
        user_id=user_root.name,
        visibility=visibility,
    )


def _resolve_write_target(scope: WriteScope, *segments: str,
                          allow_root: bool = False,
                          for_upload: bool = False) -> str:
    """Return the served-root-relative path to act on, or refuse it.

    ``segments`` are joined before anything is checked, so a caller that holds
    the destination folder and the entry's own relative path as two values does
    not have to join them itself -- the join is exactly where an escaping
    ``..`` used to slip past a string prefix comparison.

    Four rules, in the order that fails fastest and explains best:

    1. the path must be a plain downward path (``..``, ``.``, empty, absolute
       and drive-letter components are refused by
       :func:`common.safe_fs.split_relative`);
    2. it must be inside the caller's range -- so a member of a shared Agent
       cannot reach a colleague's subtree even by naming it;
    3. the range's own root is not a *target* of its own (deleting it would take
       the recycle bin with it), though ``allow_root`` lets an upload *land* in
       it, which is the ordinary "drop into the folder I am looking at" case;
    4. the Agent's own entries are not targets, matched on the path's components
       *relative to the Agent's directory*.

    ``for_upload`` narrows rule 4 to the only entry a write must never address --
    the recycle bin. A file added to the Agent's ``memory/`` does not break the
    Agent; deleting ``memory/`` does, so the protected list constrains deletion
    alone (design D13). The two rules are chosen here, in one place, so the
    upload and delete paths cannot drift apart.

    The range root itself is never classified, only refused as a target: for a
    shared Agent it is ``user/<uid>``, which reads as "the per-user container's
    child" in the Agent's frame and would refuse every write to the very folder
    the panel is showing.

    Both refusals that are not about a specific protected entry answer with
    ``outside_own_directory``, including a malformed path: the caller's remedy
    is the same, and one stable code tells a prober nothing about which shapes
    the server happens to recognise.
    """
    raw = "/".join(seg or "" for seg in segments)
    raw = raw.replace("\\", "/").strip("/")
    # Validated textually first, before any syscall and before any prefix
    # comparison: `a/../../b` is not inside `a` on paper, but the filesystem
    # would resolve it out of the range, so the string comparison alone would
    # hand out a write the range forbids.
    try:
        parts = safe_fs.split_relative(raw) if raw else []
    except safe_fs.UnsafePathError:
        raise _outside_own_directory() from None
    root_parts = scope.root_rel.split("/")
    if parts[:len(root_parts)] != root_parts:
        raise _outside_own_directory()
    if len(parts) == len(root_parts):
        if not allow_root:
            raise WorkspaceScopeError("own_directory_root")
        return scope.root_rel
    # Rule 4 is stated against the Agent's own directory, which is what the
    # refusal list in `agent.workspace.service` describes: `memory/` means the
    # Agent's memory, not a folder the member happened to name that. The range
    # is the Agent's directory or a subtree of it, so its components are a
    # prefix of the range's and the same split serves both frames.
    agent_relative = "/".join(parts[len(scope.agent_rel.split("/")):])
    reason = (unuploadable_reason if for_upload
              else undeletable_reason)(agent_relative)
    if reason:
        raise WorkspaceScopeError(reason)
    return "/".join(parts)


def _scope_error_response(error: WorkspaceScopeError):
    """A refusal the panel can act on: a stable code plus the plain reason."""
    messages = {
        "outside_own_directory": "You can only change files under your own directory",
        "own_directory_root": "That is your own directory itself",
        "agent_internal": "That belongs to the Agent and cannot be removed",
        "user_container": "That is the per-user container, not your own directory",
        "trash_not_targetable": "That is inside the recycle bin",
        "no_user": "no authenticated user",
        "agent_not_found": "agent not found",
        "not_agent_workspace": "the file panel is not showing the Agent's own workspace",
        "unsafe_user_directory": "refused",
    }
    return json.dumps({
        "status": "error",
        "code": error.code,
        "message": messages.get(error.code, str(error)),
    }, ensure_ascii=False)


def _upload_error_response(error: WorkspaceUploadError):
    """A refused payload: a stable code, plus the measured numbers.

    The panel needs the code to pick the wording and the numbers to say which
    limit was hit. A bare 500 would leave the user with "it failed" and no way
    to tell an oversized file from a cut-short transfer -- and the two have
    different remedies (split the file, or upload again).

    The limit is stated in bytes, not in MB: this is the message an operator
    reads in a log, and rounding it to whole megabytes is the panel's job (it
    has the reader's language and the same constant).
    """
    limit = WS_UPLOAD_MAX_BYTES
    messages = {
        WS_UPLOAD_TOO_LARGE: "file is larger than %d bytes" % limit,
        WS_UPLOAD_INCOMPLETE: "the upload did not arrive whole and was not saved",
    }
    return json.dumps({
        "status": "error",
        "code": error.code,
        "message": messages.get(error.code, str(error)),
    }, ensure_ascii=False)


def _restore_dest_guard(scope: WriteScope):
    """``rel -> refusal code or None`` for one restore destination.

    Restore is not "put it back where it was", it is **another write**: an
    Agent's visibility can change between the delete and the restore, so a path
    that was inside the writable range then may be outside it now (a private
    Agent made shared narrows the range from the whole workspace to one
    ``user/<uid>``). Re-running the same seam is what makes that narrowing
    actually hold; it also re-applies the protected-entry rule.
    """
    def guard(rel: str):
        try:
            _resolve_write_target(scope, rel)
        except WorkspaceScopeError as error:
            return error.code
        return None
    return guard


def _annotate_deletable(ctx, svc, agent_id, entries: list) -> list:
    """Add ``deletable`` (and ``undeletable_reason``) to each listed entry.

    The panel disables its delete button per row, so it needs the answer
    ``delete`` would give *before* the user picks something and is refused. That
    answer is computed here, by the same ``_resolve_write_target`` the route
    itself runs, rather than re-derived in JavaScript -- a second copy of the
    protected-entry list is a copy that drifts, and it would drift silently,
    because the server still refuses.

    When the caller has no writable range at all (the panel is showing a project
    directory, or this install has no end-user identity), the shape of the
    refusal belongs to the range, not to the entry: every row carries that one
    code instead of thirteen separate answers.
    """
    try:
        scope = _write_scope(ctx, svc, agent_id)
    except WorkspaceScopeError as error:
        for entry in entries:
            entry["deletable"] = False
            entry["undeletable_reason"] = error.code
        return entries
    for entry in entries:
        try:
            _resolve_write_target(scope, entry.get("path"))
            entry["deletable"] = True
        except WorkspaceScopeError as error:
            entry["deletable"] = False
            entry["undeletable_reason"] = error.code
    return entries


def _query_param(name: str) -> str:
    """One value from the request's query string, or ``''``.

    Read from the URL directly rather than through ``web.input``: the JSON
    handlers need ``web.data()`` for their body, and letting two readers share
    the request stream makes the result depend on which ran first.
    """
    from urllib.parse import parse_qs
    raw = getattr(web.ctx, "query", "") or ""
    return (parse_qs(raw.lstrip("?").strip("&")).get(name) or [""])[0]


def _panel_scope_params(body=None):
    """``(agent, session)`` for one panel request, query string first.

    The console's fetch wrapper appends both to the URL for every
    ``/api/workspace/*`` call, while the drag-and-drop upload puts them in the
    multipart body; accepting either means one handler shape serves both
    without the caller having to know which.
    """
    body = body or {}
    agent = (_query_param("agent") or (body.get("agent") or "")).strip()
    session = (_query_param("session") or (body.get("session") or "")).strip()
    return agent or None, session or None


def _scope_for_request(ctx, agent_param, session_param):
    """``(svc, scope)`` for one panel request, or raise the refusal."""
    from channel.web.web_channel import _workspace_service
    agent_id = _workspace_request_scope(ctx, session_param, agent_param)
    svc = _workspace_service(session_param, agent_id)
    return svc, _write_scope(ctx, svc, agent_id)


class WorkspaceUploadHandler:
    """Receive one file from the panel's drag-and-drop into a folder.

    **One file per request** on purpose. The client walks the drop, then sends
    its files three at a time; that shape is what makes the progress readout
    exact (a finished byte count plus the current file's own progress, instead
    of one number scaled out of a single monolithic body), lets a failed file be
    retried alone, and keeps the server's memory flat. A single multipart body
    holding 5000 files would parse into 5000 temporary files before the first
    one was written.

    Form fields: ``dir`` (destination directory, relative to the file API's
    root), ``relative_path`` (the file's own path below ``dir``, which is what
    carries the dragged folder structure), and one file part named ``file``.

    An existing name is not overwritten: the file lands as ``name (1).ext`` and
    the response says so. A drag that eats a file already in the folder is not
    something the user asked for and not something they could undo -- the panel
    ships no move, so the overwritten file would simply be gone.
    """

    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _ensure_list
        from channel.web.web_channel import _raw_web_input
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'no-store')
        with _db_scope() as ctx:
            from channel.web.auth_handlers import require_management_write
            require_management_write()
            try:
                params = _raw_web_input()
                agent, session = _panel_scope_params({
                    "agent": params.get("agent"), "session": params.get("session")})
                svc, scope = _scope_for_request(ctx, agent, session)

                parts = [p for p in _ensure_list(params.get("file"))
                         if getattr(p, "filename", None)]
                if not parts:
                    return json.dumps({"status": "error", "code": "no_file",
                                       "message": "no file in the request"})
                part = parts[0]

                dest_dir = _resolve_write_target(
                    scope, params.get("dir"), allow_root=True, for_upload=True)
                relative = (params.get("relative_path") or "").strip()
                if not relative:
                    # Without a relative path the payload is just its basename,
                    # which is what a plain (non-folder) drop sends.
                    relative = os.path.basename(part.filename.replace("\\", "/"))
                target = _resolve_write_target(
                    scope, dest_dir, relative, for_upload=True)

                written = self._stream_to(svc, target, part)
                logger.info("[WebChannel] Workspace upload: %s (%s bytes)%s",
                            written["path"], written["size"],
                            "" if not written["renamed"] else " (renamed)")
                return json.dumps({"status": "success", **written},
                                  ensure_ascii=False)
            except WorkspaceScopeError as e:
                return _scope_error_response(e)
            except WorkspaceUploadError as e:
                return _upload_error_response(e)
            except web.HTTPError:
                raise
            except (ValueError, FileNotFoundError) as e:
                return json.dumps({"status": "error", "message": str(e)})
            except Exception as e:
                logger.error(f"[WebChannel] Workspace upload error: {e}")
                return json.dumps({"status": "error", "message": str(e)})

    @staticmethod
    def _stream_to(svc, target: str, part) -> Dict:
        """Write the part to ``target``, renaming on collision.

        Streamed in bounded chunks, then the byte count is compared with what
        the transport declared. A body cut short that still parsed as a complete
        multipart part is the one failure mode a size check catches cheaply, and
        a silently truncated file is worse than a refused one.
        """
        destination = svc.available_name(target)
        directory = destination.rpartition("/")[0]
        if directory:
            svc.ensure_dir(directory)

        source = getattr(part, "file", None)
        if source is None:
            data = part.value if isinstance(part.value, (bytes, bytearray)) else b""
            source = io.BytesIO(data)
        declared = None
        try:
            source.seek(0, os.SEEK_END)
            declared = source.tell()
            source.seek(0)
        except (OSError, ValueError, AttributeError, io.UnsupportedOperation):
            # Not seekable: there is nothing to compare against and nothing to
            # rewind, so the size check degrades to "wrote what we read".
            declared = None

        # Write to a sibling temp file and rename at the end, so a connection
        # that dies mid-transfer leaves the name free and no half file in the
        # folder. ``available_name`` already proved the name is free, and
        # O_CREAT|O_EXCL re-proves it atomically: without that, a symlink planted
        # at the temp name in between would be written *through*.
        tmp_rel = svc.available_name(join_rel(
            directory, ".ws-upload-%s.part" % os.urandom(6).hex()))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        written = 0
        try:
            fd = os.open(svc.resolve(tmp_rel), flags, 0o600)
            with os.fdopen(fd, "wb") as handle:
                while True:
                    block = source.read(WS_UPLOAD_CHUNK_BYTES)
                    if not block:
                        break
                    written += len(block)
                    if written > WS_UPLOAD_MAX_BYTES:
                        raise WorkspaceUploadError(
                            WS_UPLOAD_TOO_LARGE,
                            "file is larger than %d bytes" % WS_UPLOAD_MAX_BYTES)
                    handle.write(block)
                handle.flush()
                os.fsync(handle.fileno())
            if declared is not None and written != declared:
                raise WorkspaceUploadError(
                    WS_UPLOAD_INCOMPLETE,
                    "incomplete upload: %d of %d bytes" % (written, declared))
            safe_fs.rename(svc.root, tmp_rel, destination)
            tmp_rel = None
        finally:
            if tmp_rel is not None:
                try:
                    safe_fs.remove_tree(svc.root, tmp_rel)
                except (OSError, safe_fs.UnsafePathError):
                    pass

        return {
            "path": destination,
            "name": destination.rpartition("/")[2],
            "size": written,
            "renamed": destination != target,
        }


class WorkspaceDeleteHandler:
    """Move selected files or whole folders into the caller's recycle bin.

    Soft by construction: each target is one atomic rename into
    ``user/<uid>/.trash``, so a directory of any size moves instantly and a
    failure part-way leaves the rest untouched. Nothing is destroyed, which is
    why the panel does not need to be right about the user's intent.
    """

    def POST(self):
        from channel.web.web_channel import _db_scope
        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            from channel.web.auth_handlers import require_management_write
            require_management_write()
            try:
                body = json.loads(web.data() or b'{}')
                agent, session = _panel_scope_params(body)
                svc, scope = _scope_for_request(ctx, agent, session)

                raw = body.get("targets")
                if isinstance(raw, str):
                    raw = [raw]
                if not isinstance(raw, list) or not raw:
                    return json.dumps({"status": "error", "code": "no_targets",
                                       "message": "targets are required"})
                if len(raw) > WS_DELETE_MAX_TARGETS:
                    return json.dumps({
                        "status": "error", "code": "too_many_targets",
                        "message": "at most %d items per request"
                                   % WS_DELETE_MAX_TARGETS})

                items: List[Dict] = []
                refused: List[Dict] = []
                seen = set()
                for entry in raw:
                    rel = entry if isinstance(entry, str) else (entry or {}).get("path")
                    try:
                        target = _resolve_write_target(scope, rel)
                    except WorkspaceScopeError as e:
                        refused.append({"rel": rel, "code": e.code})
                        continue
                    if target in seen:
                        continue
                    seen.add(target)
                    try:
                        items.append({
                            "rel": target,
                            "is_dir": svc.is_dir(target),
                            "size": svc.entry_size(target),
                        })
                    except FileNotFoundError:
                        refused.append({"rel": target, "code": "not_found"})

                if not items:
                    return json.dumps({"status": "success", "batch_id": None,
                                       "deleted": [], "failed": refused},
                                      ensure_ascii=False)

                result = svc.move_to_trash(scope.user_id, items,
                                           agent_rel=scope.agent_rel)
                logger.info("[WebChannel] Workspace delete: %d item(s) to batch %s",
                            len(result["moved"]), result["batch_id"])
                return json.dumps({
                    "status": "success",
                    "batch_id": result["batch_id"],
                    "deleted": [m["rel"] for m in result["moved"]],
                    "failed": refused + result["failed"],
                    "reclaimed": sum(m["size"] or 0 for m in result["moved"]),
                }, ensure_ascii=False)
            except WorkspaceScopeError as e:
                return _scope_error_response(e)
            except web.HTTPError:
                raise
            except (ValueError, FileNotFoundError) as e:
                return json.dumps({"status": "error", "message": str(e)})
            except Exception as e:
                logger.error(f"[WebChannel] Workspace delete error: {e}")
                return json.dumps({"status": "error", "message": str(e)})


class WorkspaceTrashHandler:
    """List what the caller's recycle bin is holding.

    Also the bin's cleanup point. Retention is housekeeping, and the one moment
    a bin's age is certainly irrelevant is when its owner is looking at it, so
    the sweep rides on the read instead of needing a scheduler of its own. It
    only removes batches the bin was already going to drop, so it never changes
    what this response returns.
    """

    def GET(self):
        from channel.web.web_channel import _db_scope
        web.header('Content-Type', 'application/json; charset=utf-8')
        web.header('Cache-Control', 'no-store')
        with _db_scope() as ctx:
            try:
                params = web.input(agent='', session='')
                agent, session = _panel_scope_params({
                    "agent": params.agent, "session": params.session})
                svc, scope = _scope_for_request(ctx, agent, session)
                try:
                    svc.cleanup_trash(scope.user_id, agent_rel=scope.agent_rel)
                except (OSError, safe_fs.UnsafePathError) as e:
                    logger.warning("[WebChannel] trash retention skipped: %s", e)
                entries = svc.list_trash(scope.user_id, agent_rel=scope.agent_rel)
                for entry in entries:
                    entry["name"] = (entry.get("rel") or "").rpartition("/")[2]
                return json.dumps({
                    "status": "success",
                    "entries": entries,
                    "total_size": sum(e.get("size") or 0 for e in entries),
                    "retention_days": TRASH_RETENTION_SECONDS // 86400,
                }, ensure_ascii=False)
            except WorkspaceScopeError as e:
                return _scope_error_response(e)
            except web.HTTPError:
                raise
            except Exception as e:
                logger.error(f"[WebChannel] Workspace trash error: {e}")
                return json.dumps({"status": "error", "message": str(e)})


class WorkspaceTrashRestoreHandler:
    """Put entries back where they were deleted from.

    A destination that is taken again is restored as ``name (1).ext`` rather
    than refused: the panel has no move, so a refusal would strand the entry in
    the bin with no way to act on it.
    """

    def POST(self):
        from channel.web.web_channel import _db_scope
        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            from channel.web.auth_handlers import require_management_write
            require_management_write()
            try:
                body = json.loads(web.data() or b'{}')
                agent, session = _panel_scope_params(body)
                svc, scope = _scope_for_request(ctx, agent, session)
                batch_id = (body.get("batch_id") or "").strip()
                if not batch_id:
                    return json.dumps({"status": "error", "code": "no_batch",
                                       "message": "batch_id is required"})
                indices = _indices_or_none(body.get("indices"))
                result = svc.restore_from_trash(
                    scope.user_id, batch_id, indices,
                    agent_rel=scope.agent_rel,
                    dest_guard=_restore_dest_guard(scope))
                logger.info("[WebChannel] Workspace trash restore: %d of %d",
                            len(result["restored"]),
                            len(result["restored"]) + len(result["failed"]))
                return json.dumps({"status": "success", **result},
                                  ensure_ascii=False)
            except WorkspaceScopeError as e:
                return _scope_error_response(e)
            except web.HTTPError:
                raise
            except safe_fs.UnsafePathError:
                return json.dumps({"status": "error", "code": "unsafe_path",
                                   "message": "invalid batch id"})
            except (ValueError, FileNotFoundError) as e:
                return json.dumps({"status": "error", "message": str(e)})
            except Exception as e:
                logger.error(f"[WebChannel] Workspace trash restore error: {e}")
                return json.dumps({"status": "error", "message": str(e)})


class WorkspaceTrashPurgeHandler:
    """Destroy entries in the bin. An absent ``batch_id`` empties it."""

    def POST(self):
        from channel.web.web_channel import _db_scope
        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            from channel.web.auth_handlers import require_management_write
            require_management_write()
            try:
                body = json.loads(web.data() or b'{}')
                agent, session = _panel_scope_params(body)
                svc, scope = _scope_for_request(ctx, agent, session)
                batch_id = (body.get("batch_id") or "").strip()
                indices = _indices_or_none(body.get("indices"))
                if batch_id:
                    result = svc.purge_trash(scope.user_id, batch_id, indices,
                                             agent_rel=scope.agent_rel)
                else:
                    # "Empty the bin": every batch this user owns.
                    purged: List[Dict] = []
                    failed: List[Dict] = []
                    for entry in svc.list_trash(scope.user_id,
                                                agent_rel=scope.agent_rel):
                        one = svc.purge_trash(scope.user_id, entry["batch_id"],
                                              agent_rel=scope.agent_rel)
                        purged.extend(one["purged"])
                        failed.extend(one["failed"])
                    result = {"purged": purged, "failed": failed}
                logger.info("[WebChannel] Workspace trash purge: %d item(s)",
                            len(result["purged"]))
                return json.dumps({"status": "success", **result},
                                  ensure_ascii=False)
            except WorkspaceScopeError as e:
                return _scope_error_response(e)
            except web.HTTPError:
                raise
            except safe_fs.UnsafePathError:
                return json.dumps({"status": "error", "code": "unsafe_path",
                                   "message": "invalid batch id"})
            except (ValueError, FileNotFoundError) as e:
                return json.dumps({"status": "error", "message": str(e)})
            except Exception as e:
                logger.error(f"[WebChannel] Workspace trash purge error: {e}")
                return json.dumps({"status": "error", "message": str(e)})


def _indices_or_none(raw) -> Optional[List[int]]:
    """``None`` means "everything"; anything else must be a list of indices."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError("indices must be a list")
    return [int(i) for i in raw]


def _ensure_own_user_dir(agent_id: str) -> bool:
    """Create ``<agent workspace>/user/<user id>`` for the current identity.

    The per-user container of a shared Agent (change
    ``isolate-shared-agent-user-data``) comes into existence only once
    something is filed into it, so a member who has not uploaded anything yet
    would find the console's file panel landing on a directory that is not
    there. Materializing the caller's *own* directory — empty and idempotent —
    is what lets the panel stay where that member's files actually live.

    The user id comes from the verified request identity and nowhere else, and
    the Agent's workspace is resolved from the registry, so neither the body nor
    a session's opened project directory can redirect the creation. A malformed
    user id, or a ``user`` container that is a symlink or a file, is refused
    rather than silently downgraded to the shared directory: falling back would
    turn a bad identity into a cross-user write.

    Returns ``False`` when the request carries no end user (nothing to make).
    """
    from common.runtime_identity import current_identity
    from common.state_dir import StateDirError, agent_user_root
    ident = current_identity()
    if agent_id and ident.agent_id != agent_id:
        ident = ident.derive(agent_id=agent_id)
    try:
        root = agent_user_root(ident, ensure=True)
    except StateDirError as e:
        logger.warning(f"[WebChannel] user directory refused: {e}")
        raise web.HTTPError(
            "403 Forbidden", {"Content-Type": "application/json"},
            json.dumps({"status": "error", "message": "refused",
                        "code": "unsafe_user_directory"}))
    return root is not None


class WorkspaceUserDirHandler:
    """Make sure the caller's own per-user directory of a shared Agent exists.

    The console's file panel anchors a tenant-shared Agent at that member's
    ``user/<user id>`` folder (spec ``platform-file-browsing``), which is where
    the platform files the member uploads and is handed back actually live. That
    folder is only created by a write, so the panel asks for it explicitly once
    before listing instead of relying on a read to have a side effect.

    The directory is always the caller's own: ``user_id`` is taken from the
    verified identity, never from the body, so this route cannot address another
    member's subtree even if a client asks it to. ``agent`` goes through the
    same tenant-binding / private-owner / owned-session checks as every other
    workspace route.
    """

    def POST(self):
        from channel.web.web_channel import _db_scope
        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            # A console write funnels through the unified origin/CSRF gate before
            # any identity or filesystem work, like every other management write.
            from channel.web.auth_handlers import require_management_write
            require_management_write()
            try:
                body = json.loads(web.data() or b'{}')
                agent_id = _workspace_request_scope(
                    ctx, body.get("session") or None, body.get("agent") or None)
                if not agent_id:
                    return json.dumps({"status": "error",
                                       "message": "agent is required"})
                if not _ensure_own_user_dir(agent_id):
                    return json.dumps({"status": "error", "code": "no_user",
                                       "message": "no authenticated user"})
                return json.dumps({"status": "success", "agent": agent_id},
                                  ensure_ascii=False)
            except web.HTTPError:
                raise
            except Exception as e:
                logger.error(f"[WebChannel] Workspace user dir error: {e}")
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
                # A cleared project resolves to the session default (the caller's
                # own directory for a shared Agent), not to the shared root.
                try:
                    from bridge.bridge import Bridge
                    ab = Bridge().get_agent_bridge()
                    agent = ab.get_cached_agent(session_id, agent_id)
                    if agent is not None:
                        ab.apply_session_workspace(agent, session_id, agent_id)
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
                        if agent is not None:
                            ab.apply_session_workspace(agent, session_id, agent_id)
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


