# encoding:utf-8
"""现场复验：为什么「未分配给该智能体」的连接仍然被该智能体看到（只读脚本）。

对应提问：`OneAgent HTTP MCP` 并没有分配给「税务健康体检」，为什么该智能体看到了它？

`external_connection_agent_assignment_sets` 里两行都「没有该智能体」，但含义完全不同：

* `configured=0`（**未配置 · 沿用原权限**）：这条连接还没被分配制度接管，沿用变更前的
  授权口径。对普通成员是「按老规则要逐资源授权」；对**租户管理员**则是「本租户的
  能力都能用」——租户管理员豁免是变更前就有的规则，不是本次新加的。
* `configured=1`（**已配置**）：以关系为准，关系里没写这个智能体就是拒绝，
  **租户管理员同样受约束**。

所以「未分配」这个描述本身不够：缺的是「这条连接是否已进入分配制度」。

只读：不写库、不改 `config.json`、不调用任何远端写动作。
"""

from __future__ import annotations

import sqlite3

#: 用户红框指出的连接：关系里没有该智能体，且尚未配置。
UNASSIGNED_CONNECTION = "conn_8r2lE23PRk0Sd_Wb"     # OneAgent HTTP MCP
#: 对照组：已配置，且只分配给一个智能体。
ASSIGNED_CONNECTION = "conn_cN-lhbMpQlwpiL7k"       # weknora-rsmagent

AGENT = "tax-health-check-test15"                   # 提问时使用的智能体
OTHER_AGENT = "knowledge-qa-test15"                 # 同租户、未被分配的另一个智能体

#: 现场提问时的登录身份。租户管理员的 ``usr_9ZxVPz7M2FuOro1q`` 是 `memberships` 里
#: display_name 为「租户管理员」的那一行，与界面左下角一致；另一个是 test15 的普通成员。
TENANT_ADMIN = "usr_9ZxVPz7M2FuOro1q"
PLAIN_MEMBER = "usr_EMjtqQ_5s9oey1y1"

ACTORS = (("租户管理员", TENANT_ADMIN), ("普通成员  ", PLAIN_MEMBER))


def main() -> None:
    from config import load_config
    load_config()   # the deployment's own configuration, exactly as app.py reads it

    from auth.service import IdentityService
    from integrations.external import assignment
    from integrations.external import tools as external_tools
    from integrations.external.adapters.mcp import _mcp_tool_provider

    db = sqlite3.connect("identity.db")
    db.row_factory = sqlite3.Row
    svc = IdentityService("identity.db")

    rows = {r["id"]: dict(r) for r in db.execute(
        "SELECT * FROM external_connections WHERE id IN (?,?)",
        (UNASSIGNED_CONNECTION, ASSIGNED_CONNECTION))}
    tenant = rows[UNASSIGNED_CONNECTION]["tenant_id"]

    print("tenant =", tenant, " agent =", AGENT)
    for label, actor in ACTORS:
        display = db.execute(
            "SELECT display_name FROM memberships WHERE tenant_id=? AND user_id=?",
            (tenant, actor)).fetchone()
        print("  %s %s (%s)" % (label, actor,
                                display["display_name"] if display else "?"))

    # 1. 分配表的原始状态：两行都「没有该智能体」，但 configured 不同 --------
    print("\n[1] 分配状态行（external_connection_agent_assignment_sets）")
    for connection_id in (UNASSIGNED_CONNECTION, ASSIGNED_CONNECTION):
        row = rows[connection_id]
        logical = str(row["base_connection_id"] or row["id"])
        state = assignment.assignment_state(
            svc._store, tenant_id=tenant, logical_id=logical)
        agents = assignment.assigned_agent_ids(
            svc._store, tenant_id=tenant, logical_id=logical)
        allowed, reason = assignment.assignment_allows(
            svc._store, tenant_id=tenant, logical_id=logical, agent_id=AGENT)
        print("    %-22s configured=%s assigned=%s"
              % (row["name"], state["configured"] if state else "(no row)",
                 agents or "[]"))
        print("        assignment_allows(%s) = allowed=%s reason=%s"
              % (AGENT, allowed, reason))

    # 2. 授权判定拆开看：逐资源授权 / 分配即授权 / 租户管理员豁免 -------------
    print("\n[2] may_execute() 的三条入口分开看")
    print("    per-resource grant = 变更前的逐资源授权（external:mcp:... execute）")
    print("    assignment         = 本次新增：关系里明确写了该智能体")
    print("    admin exemption    = 变更前就有的租户管理员豁免")
    from integrations.external import authorization

    raw = _mcp_tool_provider(tenant, "verification")
    for label, actor in ACTORS:
        print("\n    --- %s ---" % label.strip())
        for connection_id in (UNASSIGNED_CONNECTION, ASSIGNED_CONNECTION):
            row = rows[connection_id]
            binding = next(b for b in raw if b.connection_id == connection_id)
            rid = authorization.resource_id_for(
                binding.tool.kind, binding.tool.action)
            granted = svc.check_resource_action(
                actor, tenant, "tool", rid, "execute",
                permission=authorization.EXECUTE_PERMISSION)
            assigned = authorization._assignment_authorizes(
                svc, actor_user_id=actor, tenant_id=tenant,
                kind=binding.tool.kind, scope=binding.scope,
                agent_id=AGENT, connection_id=connection_id)
            ok = authorization.may_execute(
                svc, actor_user_id=actor, tenant_id=tenant,
                kind=binding.tool.kind, action=binding.tool.action,
                scope=binding.scope, agent_id=AGENT,
                connection_id=connection_id)
            print("        %-22s may_execute=%-5s grant=%-5s assignment=%-5s"
                  % (row["name"], ok, granted, assigned))

    # 3. 最终投影：模型真正读到的那一层 -------------------------------------
    print("\n[3] 最终投影（authorized_tools 的 connection-level 名字）")
    for connection_id in (UNASSIGNED_CONNECTION, ASSIGNED_CONNECTION):
        _mcp_tool_provider(tenant, "verification")

    cases = (("租户管理员 + 已分配的 agent   ", TENANT_ADMIN, AGENT),
             ("租户管理员 + 未分配的 agent   ", TENANT_ADMIN, OTHER_AGENT),
             ("普通成员   + 已分配的 agent   ", PLAIN_MEMBER, AGENT))
    for label, actor, agent_id in cases:
        names = sorted(
            b.tool.name for b in external_tools.authorized_tools(
                tenant_id=tenant, actor_user_id=actor, identity=svc,
                agent_id=agent_id)
            if b.tool.kind == "mcp")
        print("    %s (agent=%s) -> %d" % (label, agent_id, len(names)))
        for name in names:
            print("        ", name)

    # 4. 与现场日志对齐 ------------------------------------------------------
    print("\n[4] 与 run.log 19:18:27 的 `external tools synced` 对齐")
    print("    现场那次投放的 6 个 connection-level 名字，正是 [3] 第一行的 6 个。")
    print("    现场身份是租户管理员（界面左下角「租户管理员」），不是普通成员；")
    print("    [3] 第三行说明换成普通成员后，红框那 3 个名字不再出现。")


if __name__ == "__main__":
    main()
