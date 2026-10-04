"""Database authorization for upstream skill staging and installation services."""
from __future__ import annotations

import json

import web

from channel.web.api.skills import (
    _LOCAL_PATH_PREFIXES, _market_spec, _preview_response, _staging,
)

ACTIONS = frozenset(("preview", "confirm", "discard", "install", "delete"))


def _authorize(ctx, agent_id):
    from channel.web.web_channel import _require_read_permission, _require_tenant_agent_binding
    from channel.web.fork.authorization import _require_skill_write_scope
    _require_read_permission(ctx, "skill.edit")
    if agent_id:
        agent_id = _require_tenant_agent_binding(ctx, agent_id)
    _require_skill_write_scope(ctx, agent_id)
    from channel.web.web_channel import _skill_service
    from common import state_dir
    from pathlib import Path
    if agent_id:
        root = Path(_skill_service(agent_id).manager.custom_dir).resolve()
        shared = state_dir.skills_dir(base=state_dir.shared_root()).resolve()
        if root == shared:
            _require_skill_write_scope(ctx, None)
    return agent_id


def can_install(ctx, agent_id, service):
    """Project the write gates without constructing an HTTP error response."""
    from auth.object_scope import MANAGE, ObjectScope
    from auth.service import get_identity_service
    from common import state_dir
    from pathlib import Path
    if ctx is None:
        return True
    if not ctx.is_platform_admin and "skill.edit" not in ctx.permissions:
        return False
    scope = ObjectScope.from_context(ctx)
    if not agent_id:
        return scope.allows_public_configuration()
    binding = get_identity_service().get_agent_binding(agent_id)
    if not scope.allows_agent(binding, action=MANAGE):
        return False
    root = Path(service.manager.custom_dir).resolve()
    shared = state_dir.skills_dir(base=state_dir.shared_root()).resolve()
    return root != shared or scope.allows_public_configuration()


def _claim(ctx, agent_id):
    return (ctx.tenant_id, ctx.user_id, agent_id or "")


def _create_preview(ctx, agent_id):
    token, path = _staging.create(agent_id)
    _staging.get(token)["identity"] = _claim(ctx, agent_id)
    return token, path


def apply(ctx, body):
    from channel.web.fork.runtime import _request_agent_id
    from channel.web.fork.authorization import _resolved_skill
    from channel.web.web_channel import _require_resource_action, _skill_service
    from cli.commands.skill import commit_staged, stage_skill

    action = body.get("action")
    agent_id = _authorize(ctx, _request_agent_id(body))
    service = _skill_service(agent_id)
    if action in ("confirm", "discard"):
        token = body.get("token")
        staged = _staging.get(token)
        if not staged:
            return json.dumps({"status": "error", "message": "preview expired, fetch it again"})
        if staged.get("identity") != _claim(ctx, agent_id):
            raise web.forbidden()
        if action == "discard":
            _staging.discard(token)
            return json.dumps({"status": "success"})
        names = body.get("names")
        if names is not None and not isinstance(names, list):
            raise ValueError("names must be a list")
        try:
            installed = commit_staged(staged["dir"], names, agent_id=agent_id,
                                      skills_dir=service.manager.custom_dir)
        finally:
            _staging.discard(token)
        service.manager.refresh_skills()
        return json.dumps({"status": "success", "installed": installed}, ensure_ascii=False)
    if action == "preview":
        spec = _market_spec(body.get("source") or "", body.get("value") or "")
        token, path = _create_preview(ctx, agent_id)
        try:
            result = stage_skill(spec, path)
        except Exception:
            _staging.discard(token)
            raise
        return _preview_response(token, path, result, agent_id)
    if action == "install":
        spec = str(body.get("spec") or body.get("name") or "").strip()
        if not spec or spec.startswith(_LOCAL_PATH_PREFIXES):
            raise ValueError("a remote skill specification is required")
        token, path = _create_preview(ctx, agent_id)
        try:
            result = stage_skill(spec, path)
            if result.error:
                return json.dumps({"status": "error", "message": result.error})
            installed = commit_staged(path, agent_id=agent_id,
                                      skills_dir=service.manager.custom_dir)
        finally:
            _staging.discard(token)
        service.manager.refresh_skills()
        return json.dumps({"status": "success", "installed": installed,
                           "messages": result.messages}, ensure_ascii=False)
    if action == "delete":
        entry, rid = _resolved_skill(service, body.get("name") or "", body.get("resource_id") or "")
        _require_resource_action(ctx, "skill", rid, "edit", "skill.edit")
        if service._ships_with_install(entry.skill):
            raise ValueError("built-in skills cannot be deleted")
        service.delete({"resource_id": rid})
        return json.dumps({"status": "success"})
    raise ValueError("unknown skill action")


class SkillUploadHandler:
    """Use the upstream upload parser and staging service after object authorization."""
    MAX_TOTAL_BYTES = 50 * 1024 * 1024
    MAX_FILES = 2000

    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.fork.runtime import _scoped_agent_id
        from channel.web.core._common import _multipart_lists
        from channel.web.api.knowledge import _read_uploaded_file_bytes_limited
        from cli.commands.skill import stage_skill_upload

        web.header('Content-Type', 'application/json; charset=utf-8')
        with _db_scope() as ctx:
            content_length = int(getattr(web.ctx, 'env', {}).get('CONTENT_LENGTH') or 0)
            if content_length > self.MAX_TOTAL_BYTES:
                raise web.HTTPError('413 Payload Too Large', {}, 'upload too large')
            params = _multipart_lists(self.MAX_FILES * 2 + 16)
            agent_id = _authorize(ctx, _scoped_agent_id(params))
            uploaded, paths = params.get('files') or [], params.get('paths') or []
            if not uploaded or len(uploaded) > self.MAX_FILES:
                raise web.badrequest('invalid file count')
            files, total = [], 0
            for index, file_obj in enumerate(uploaded):
                if file_obj is None:
                    continue
                rel = paths[index] if index < len(paths) and isinstance(paths[index], str) else ''
                rel = rel or getattr(file_obj, 'filename', '') or ''
                content = _read_uploaded_file_bytes_limited(file_obj, self.MAX_TOTAL_BYTES)
                total += len(content)
                if total > self.MAX_TOTAL_BYTES:
                    raise web.HTTPError('413 Payload Too Large', {}, 'upload too large')
                files.append((rel, content))
            token, path = _create_preview(ctx, agent_id)
            try:
                result = stage_skill_upload(files, path)
            except Exception:
                _staging.discard(token)
                raise
            return _preview_response(token, path, result, agent_id)
