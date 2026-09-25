"""Fork web layer (change adopt-upstream-web-split, design D2).

Fork-owned implementation, moved verbatim out of the former
channel/web/web_channel.py monolith. Upstream's api/ modules are not
edited. Imports inside function bodies are lazy so these modules can
reference each other without import cycles.
"""

from __future__ import annotations
from auth.object_scope import MANAGE as SCOPE_MANAGE, USE as SCOPE_USE, ObjectScope
from bridge.context import *
from common import i18n
from common.log import logger
import json
import web


class ToolsHandler:
    def GET(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _require_catalog_read
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.tools.tool_manager import ToolManager
            from common import i18n
            with _db_scope() as ctx:
                _require_catalog_read(ctx, "tool.read")
                tm = ToolManager()
                if not tm.tool_classes:
                    tm.load_tools()
                tools = []
                lang = i18n.get_language()
                for name, cls in tm.tool_classes.items():
                    try:
                        instance = cls()
                        desc = instance.description
                        if lang == i18n.ZH_HANT and desc:
                            desc = i18n.to_traditional(desc)
                        elif lang == "en" and name == "scheduler":
                            desc = (
                                "Create, query and manage scheduled tasks (reminders, periodic tasks, etc.).\n\n"
                                "⚠️ IMPORTANT: Only use this tool when delayed or periodic execution is needed."
                            )
                        tools.append({
                            "resource_id": f"builtin:{name}",
                            "name": name,
                            "description": desc,
                            "requires_explicit_binding": bool(cls.requires_explicit_binding),
                        })
                    except Exception:
                        tools.append({"resource_id": f"builtin:{name}", "name": name, "description": "",
                                      "requires_explicit_binding": bool(getattr(cls, "requires_explicit_binding", False))})
                # MCP tools: namespaced by their connection/server name, matching
                # the auth catalog projection convention.
                mcp_instances = getattr(tm, "_mcp_tool_instances", None) or {}
                for tname, mcp_tool in mcp_instances.items():
                    conn = getattr(mcp_tool, "server_name", "default")
                    tools.append({
                        "resource_id": f"mcp:{conn}:{tname}",
                        "name": tname,
                        "description": mcp_tool.description or "",
                    })
                tools = _filter_tool_catalog(ctx, tools, "read")
                _attach_personal_states(ctx, tools, "tool")
            return json.dumps({"status": "success", "tools": tools}, ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Tools API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        """工具的个人参数：保存 / 清除本人已获授权工具的参数（任务 5.5）.

        The parameters live in the resource's detail component on this page, so
        the write belongs to the page's own endpoint instead of a second personal
        surface (and a second authority) to keep in step. Nothing here touches a
        tool definition, an MCP connection, the tenant's grants, or another
        member's configuration: ``actor_user_id`` is the session's, there is no
        ``user_id`` parameter, and the service re-checks the caller's own ``tool``
        ``execute`` grant on every save.
        """
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _personal_channel_error
        from channel.web.web_channel import _personal_channel_service
        from channel.web.web_channel import _require_catalog_read
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            body = json.loads(web.data() or b'{}')
            action = str(body.get("action") or "").strip()
            if action not in ("save-personal", "clear-personal"):
                return json.dumps({"status": "error", "code": "bad_request",
                                   "message": f"unknown action: {action}"},
                                  ensure_ascii=False)
            with _db_scope() as ctx:
                _require_catalog_read(ctx, "tool.read")
                service = _personal_channel_service()
                target = dict(actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id,
                              resource_kind="tool",
                              resource_id=str(body.get("resource_id") or "").strip())
                if action == "save-personal":
                    saved = service.save_personal_resource_config(
                        **target, params=body.get("params") or {},
                        secret=body.get("secret"))
                    return json.dumps({"status": "success", "config": saved},
                                      ensure_ascii=False)
                service.clear_personal_resource_config(**target)
                return json.dumps({"status": "success", "config": None},
                                  ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Tool personal config error: {e}")
            return _personal_channel_error(e)


def _annotate_skill_actions(ctx: "Optional[RequestContext]",
                            skills: List[dict]) -> List[dict]:
    """Report, per skill, whether the page may offer the global toggle.

    The switch used to be rendered unconditionally, so a member was shown a
    control whose request is refused: the global enable/disable writes the state
    every member and Agent reads, which needs the management qualification
    (``_require_skill_write_scope``) on top of the per-resource ``enable`` grant.

    The rule is the write path's, asked here instead of raised — same scope
    authority, same grant check, once per row — so the page cannot advertise a
    verb the write would refuse. Legacy mode (``ctx is None``) annotates every
    row as available, matching the unrestricted behaviour of every other gate in
    this module.
    """
    if ctx is None:
        for skill in skills:
            skill["actions"] = {"enable": True}
        return skills
    from auth.service import get_identity_service

    manage = ObjectScope.from_context(ctx).allows_public_configuration()
    service = get_identity_service()
    for skill in skills:
        rid = skill.get("resource_id") or (
            f"{skill.get('source', 'builtin')}:{skill.get('name', '')}")
        skill["actions"] = {"enable": bool(
            manage and service.check_resource_action(
                ctx.user_id, ctx.tenant_id, "skill", rid, "enable",
                permission="skill.enable"))}
    return skills


def _filter_tool_catalog(ctx: "Optional[RequestContext]", tools: List[dict], action: str) -> List[dict]:
    """Narrow a tool list to entries the caller may act on (``read``/``execute``/...).

    A platform admin is unrestricted. The built-in tenant_admin reads its own
    tenant's whole catalog (the read-only management view); a database-mode
    member sees only tools explicitly granted for ``action`` via their
    ``resource_id``. Legacy mode is unrestricted.
    """
    from channel.web.web_channel import _resource_ids
    if ctx is not None and ctx.is_tenant_admin:
        return tools
    allowed = _resource_ids(ctx, "tool", action, permission="tool.read" if action != "read" else None)
    if allowed is None:
        return tools
    out = []
    for tool in tools:
        if tool.get("resource_id") in allowed:
            out.append(tool)
    return out


def _attach_personal_states(ctx: "Optional[RequestContext]", rows: List[dict],
                            kind: str) -> List[dict]:
    """Attach the caller's own parameters to each row of a shared catalog page.

    The 工具与技能 detail component carries 个人参数 in the same panel as the
    resource's own description (task 5.5). There is no 我的资源 page any more, so
    the rows this page already returns are where the values have to arrive: one
    request, one authority, and no second surface to keep in step with the first.

    A row the caller holds no use grant for keeps its key with ``None`` rather
    than a synthesized default, so "nothing personal here" is distinguishable
    from "granted, nothing saved yet" — the console shows an editor only for the
    latter (and a saved configuration read-only for a row whose grant was since
    withdrawn, which is what ``clear`` stays available for).
    """
    if ctx is None:  # legacy mode: no per-member configuration exists
        return rows
    from auth.service import get_identity_service

    states = get_identity_service().personal_resource_states(
        actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id, resource_kind=kind)
    for row in rows:
        row["personal"] = states.get(str(row.get("resource_id") or ""))
    return rows


class SkillsHandler:
    def GET(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _filter_skill_catalog
        from channel.web.web_channel import _request_agent_id
        from channel.web.web_channel import _require_catalog_read
        from channel.web.web_channel import _skill_service
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from common import i18n
            with _db_scope() as ctx:
                _require_catalog_read(ctx, "skill.read")
                params = web.input(agent_id='')
                # The library page lists everything installed, unnarrowed by the
                # Agent's selection: a skill it has not selected still has to be
                # visible here for the selection to be editable at all.
                service = _skill_service(_request_agent_id(params))
                skills = service.query()
                skills = _filter_skill_catalog(ctx, skills, "read")
                skills = _annotate_skill_actions(ctx, skills)
                _attach_personal_states(ctx, skills, "skill")
                if i18n.get_language() == i18n.ZH_HANT:
                    for skill in skills:
                        if isinstance(skill, dict):
                            for k, v in list(skill.items()):
                                if k in ("name", "description", "display_name") and isinstance(v, str):
                                    skill[k] = i18n.to_traditional(v)
            return json.dumps({"status": "success", "skills": skills}, ensure_ascii=False)
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Skills API error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _personal_channel_service
        from channel.web.web_channel import _raise_if_skill_name_ambiguous
        from channel.web.web_channel import _request_agent_id
        from channel.web.web_channel import _require_read_permission
        from channel.web.web_channel import _require_resource_action
        from channel.web.web_channel import _require_skill_write_scope
        from channel.web.web_channel import _resolved_skill
        from channel.web.web_channel import _skill_service
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            with _db_scope() as ctx:
                _require_read_permission(ctx, "skill.read")
                body = json.loads(web.data())
                action = body.get("action")
                name = body.get("name")
                resource_id = body.get("resource_id")
                if not action:
                    return json.dumps({"status": "error", "message": "action is required"})
                if not name and not resource_id:
                    return json.dumps({"status": "error", "message": "name or resource_id is required"})
                service = _skill_service(_request_agent_id(body))
                if not action:
                    return json.dumps({"status": "error", "message": "action is required"})
                # Resolve to the exact authorization object *before* the gates and
                # the write: the grant is recorded as ``{source}:{name}``, so a
                # bare name would be checked against the wrong string, and the
                # service must be told the same object the gates approved.
                entry, rid = _resolved_skill(service, name or "", resource_id or "")
                target = {"resource_id": rid}
                if action in ("save-personal", "clear-personal"):
                    # 个人参数 (task 5.5). Deliberately *before* the public write
                    # scope below: this is an owner-scoped write of the caller's
                    # own parameters, not a change to the tenant's shared skill,
                    # so it answers to the caller's ``skill`` ``use`` grant and the
                    # use-owner rules the service enforces — asking for the
                    # management qualification here would hide the member's own
                    # editor behind an administrator's authority.
                    personal = _personal_channel_service()
                    personal_target = dict(actor_user_id=ctx.user_id,
                                           tenant_id=ctx.tenant_id,
                                           resource_kind="skill", resource_id=rid)
                    if action == "save-personal":
                        saved = personal.save_personal_resource_config(
                            **personal_target, params=body.get("params") or {},
                            secret=body.get("secret"))
                        return json.dumps({"status": "success", "config": saved},
                                          ensure_ascii=False)
                    personal.clear_personal_resource_config(**personal_target)
                    return json.dumps({"status": "success", "config": None},
                                      ensure_ascii=False)
                # Enabling a skill writes the anchor Agent's own configuration, so
                # that Agent must be inside the caller's scope first (task 2.2): a
                # per-resource grant must not reach the tenant's shared surface.
                _require_skill_write_scope(ctx, _request_agent_id(body))
                if action in ("open", "close"):
                    _require_resource_action(ctx, "skill", rid, "enable", "skill.enable")
                    service.open(target) if action == "open" else service.close(target)
                else:
                    return json.dumps({"status": "error", "message": f"unknown action: {action}"})
            return json.dumps({"status": "success"}, ensure_ascii=False)
        except web.HTTPError:
            raise
        except ValueError as e:
            _raise_if_skill_name_ambiguous(e)
            logger.error(f"[WebChannel] Skills POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})
        except Exception as e:
            logger.error(f"[WebChannel] Skills POST error: {e}")
            return json.dumps({"status": "error", "message": str(e)})


class SkillContentHandler:
    """
    A skill's definition file, for the console's viewer and editor.

    Addressed by skill name rather than by path, because the loader is what
    resolves a name to a file: a workspace skill shadows a builtin of the same
    name, and a builtin sits outside the workspace that the file APIs are
    confined to.

    Unlike the skill list, the text is served exactly as stored - no
    simplified-to-traditional conversion. What comes back here is what a save
    would write, and rewriting someone's file into another script because of
    the console's display language is not a conversion they asked for.
    """

    def GET(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _raise_if_skill_name_ambiguous
        from channel.web.web_channel import _request_agent_id
        from channel.web.web_channel import _require_catalog_read
        from channel.web.web_channel import _require_resource_action
        from channel.web.web_channel import _skill_service
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            with _db_scope() as ctx:
                _require_catalog_read(ctx, "skill.read")
                params = web.input(name='', resource_id='', agent_id='')
                name = (getattr(params, 'name', '') or '').strip()
                resource_id = (getattr(params, 'resource_id', '') or '').strip()
                if not name and not resource_id:
                    return json.dumps({"status": "error", "message": "name or resource_id is required"})
                service = _skill_service(_request_agent_id(params))
                # Resolve to the exact authorization object, then check the grant.
                entry = service.resolve(resource_id=resource_id or None, name=name or None)
                rid = resource_id or f"{entry.skill.source}:{entry.skill.name}"
                # A tenant admin browses its own tenant's skills read-only; the
                # write path (POST) still requires ``skill.edit`` and a grant.
                if not (ctx is not None and ctx.is_tenant_admin):
                    _require_resource_action(ctx, "skill", rid, "read", "skill.read")
                result = service.read_content(entry.skill.name, resource_id=rid)
            return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except (ValueError, FileNotFoundError) as e:
            _raise_if_skill_name_ambiguous(e)
            return json.dumps({"status": "error", "message": str(e)})
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Skill content error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

    def POST(self):
        from channel.web.web_channel import _db_scope
        from channel.web.web_channel import _raise_if_skill_name_ambiguous
        from channel.web.web_channel import _request_agent_id
        from channel.web.web_channel import _require_read_permission
        from channel.web.web_channel import _require_resource_action
        from channel.web.web_channel import _require_skill_write_scope
        from channel.web.web_channel import _resolved_skill
        from channel.web.web_channel import _skill_service
        web.header('Content-Type', 'application/json; charset=utf-8')
        try:
            from agent.workspace.service import WorkspaceConflictError

            with _db_scope() as ctx:
                _require_read_permission(ctx, "skill.read")
                body = json.loads(web.data() or b'{}')
                name = (body.get("name") or "").strip()
                resource_id = (body.get("resource_id") or "").strip()
                if not name and not resource_id:
                    return json.dumps({"status": "error", "message": "name or resource_id is required"})
                content = body.get("content")
                if not isinstance(content, str):
                    return json.dumps({"status": "error", "message": "content must be a string"})
                service = _skill_service(_request_agent_id(body))
                # Resolve to the exact authorization object, then enforce edit.
                entry, rid = _resolved_skill(service, name, resource_id)
                # The edit lands in the anchor Agent's state root, so the scope
                # gate comes first (task 2.2): a member's ``skill.edit`` must not
                # rewrite the tenant's *shared* skill library, and a non-owner
                # administrator must not rewrite a member's private one.
                _require_skill_write_scope(ctx, _request_agent_id(body))
                _require_resource_action(ctx, "skill", rid, "edit", "skill.edit")

                try:
                    result = service.write_content(
                        name or None, content, expected_mtime=body.get("expected_mtime"),
                        resource_id=rid,
                    )
                except WorkspaceConflictError as e:
                    return json.dumps({"status": "error", "code": "conflict", "message": str(e)})

                logger.info(f"[WebChannel] Skill saved: {name or resource_id} ({result['size']} bytes)")
                return json.dumps({"status": "success", **result}, ensure_ascii=False)
        except (ValueError, FileNotFoundError) as e:
            _raise_if_skill_name_ambiguous(e)
            return json.dumps({"status": "error", "message": str(e)})
        except PermissionError:
            return json.dumps({"status": "error", "message": "permission denied"})
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[WebChannel] Skill write error: {e}")
            return json.dumps({"status": "error", "message": str(e)})

