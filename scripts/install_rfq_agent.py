#!/usr/bin/env python3
"""Install the RFQ agent into one explicitly selected tenant, using domain services."""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PRESET = ROOT / "agent/presets/rfq_quote"
SKILL = ROOT / "skills/rfq-quote"


def ensure_access(identity, tenant_id, actor_user_id, agent_id):
    """Grant only the newly installed specialist and its skill to tenant admins."""
    role = next(r for r in identity.list_roles(tenant_id) if r["code"] == "tenant_admin")
    grants = list(role["resource_grants"])
    for kind, resource_id in (("agent", "agent:"+agent_id), ("skill", "builtin:rfq-quote")):
        for action in ("read", "use"):
            grant = {"resource_kind": kind, "resource_id": resource_id, "action": action}
            if grant not in grants:
                grants.append(grant)
    if grants != role["resource_grants"]:
        identity.update_role(actor_user_id, tenant_id, role["id"], role["name"],
                             role["permissions"], role["version"], resource_grants=grants)


def install(admin, identity, *, tenant_id, actor_user_id, agent_id):
    from agent.registry import AgentRegistry, _AGENT_ID_RE, set_agent_registry

    if not _AGENT_ID_RE.fullmatch(agent_id):
        raise ValueError("invalid agent id")
    membership = identity.get_membership(actor_user_id, tenant_id)
    if (not membership or not membership.get("active") or not membership.get("user_active")
            or "tenant_admin" not in identity.role_codes_for(actor_user_id, tenant_id)):
        raise ValueError("安装须指定目标租户内有效的租户管理员")
    tenant = identity.get_tenant(tenant_id)
    if not tenant or not tenant.get("active") or tenant.get("archived_at"):
        raise ValueError("目标租户不可用")
    shared = identity.tenant_shared_root(tenant_id)
    if not shared:
        raise ValueError("目标租户缺少独立工作区")
    workspace = Path(shared).resolve() / "agents" / agent_id
    if workspace.resolve() != workspace:
        raise ValueError("目标智能体工作区不能是符号链接")
    registry = AgentRegistry.from_config(admin._load())
    current = next((p for p in registry.list() if p.id == agent_id), None)
    binding = identity.get_agent_binding(agent_id)
    marker = workspace / "rfq-preset.json"
    expected = {"preset": "rfq_quote", "tenant_id": tenant_id, "version": 1}
    if current or binding:
        if (current and current.workspace_path == workspace and binding
                and binding["tenant_id"] == tenant_id and marker.is_file()
                and json.loads(marker.read_text(encoding="utf-8")) == expected):
            ensure_access(identity, tenant_id, actor_user_id, agent_id)
            return {"status": "already_installed", "id": agent_id, "workspace": str(workspace), "tenant": tenant["name"]}
        raise ValueError("ID 已被其他智能体或未完成安装占用，不覆盖现有配置")
    if workspace.exists():
        raise ValueError("工作区已存在，不覆盖任何已有文件")
    profile = json.loads((PRESET / "profile.json").read_text(encoding="utf-8"))
    profile.pop("id")
    # Fail before touching a roster if the portable runtime cannot create reports.
    import xlsxwriter  # noqa: F401
    import openpyxl  # noqa: F401

    created = False
    bound = False
    set_agent_registry(registry)
    try:
        admin.create_agent(agent_id=agent_id, workspace=str(workspace), knowledge_mode="own", skill_mode="own", **profile)
        created = True
        for name in ("AGENT.md", "RULE.md"):
            shutil.copyfile(PRESET/name, workspace/name)
        with (workspace/"RULE.md").open("a", encoding="utf-8") as handle:
            handle.write("\n## 本机执行环境\n\n已验证的 Python 解释器：`"+sys.executable+"`。运行 rfq-quote 核算脚本时使用该解释器，以便加载已安装的 Excel 依赖。\n")
        # The general create path seeds USER.md from the instance default.
        # A tenant specialist must not inherit another tenant's operator profile.
        (workspace/"USER.md").write_text("# 使用者\n\n以当前租户会话中的用户及其提供的业务资料为准。\n", encoding="utf-8")
        shutil.copytree(SKILL, workspace/"skills/rfq-quote", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # A pre-authored specialist has completed naming/onboarding at installation.
        (workspace/"BOOTSTRAP.md").unlink(missing_ok=True)
        marker.write_text(json.dumps(expected, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        identity.bind_agent(tenant_id=tenant_id, agent_id=agent_id, origin="admin_created", actor_user_id=actor_user_id)
        bound = True
        ensure_access(identity, tenant_id, actor_user_id, agent_id)
        return {"status": "installed", "id": agent_id, "workspace": str(workspace), "tenant": tenant["name"]}
    except Exception:
        # Compensate only resources created by this invocation. Never touch an old Agent.
        if bound:
            identity.release_deleted_agent(agent_id=agent_id, actor_user_id=actor_user_id)
        if created:
            admin.delete_agent(agent_id, require_unreferenced=False)
            if workspace.exists():
                shutil.rmtree(workspace)
        raise
    finally:
        set_agent_registry(None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT/"config.json")
    parser.add_argument("--identity-db", type=Path)
    parser.add_argument("--tenant-code", required=True)
    parser.add_argument("--username", required=True, help="目标租户管理员账号，用于归属与审计")
    args = parser.parse_args()
    settings = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if settings.get("identity_mode") != "database":
        parser.error("此安装器面向本项目的多租户数据库模式")
    db = args.identity_db or Path(settings.get("identity_db_path") or args.config.parent/"identity.db")
    if not db.is_file():
        parser.error("identity 数据库不存在")
    con = sqlite3.connect("file:"+str(db.resolve())+"?mode=ro", uri=True)
    try:
        row = con.execute("SELECT t.id,u.id FROM tenants t JOIN memberships m ON m.tenant_id=t.id JOIN users u ON u.id=m.user_id WHERE t.code=? AND u.username=?", (args.tenant_code, args.username)).fetchone()
    finally:
        con.close()
    if not row:
        parser.error("账号不属于指定租户")
    from agent.admin import AgentAdminService
    from auth.service import IdentityService
    try:
        result = install(AgentAdminService(str(args.config)), IdentityService(str(db)),
                         tenant_id=row[0], actor_user_id=row[1], agent_id="rfq-quote-"+args.tenant_code)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
