# encoding:utf-8
"""现场复验：分配即该连接的授权（只读脚本，不改动任何数据或开关）。

对应用户报告「给税务健康体检已经分配了 mcp 知识库，为什么看不到」。本 change 的
第 7 组把「分配」作为该连接的授权，去掉叠加的逐资源授权要求。本脚本在同一份现场数据
（仓库根 `config.json` + `identity.db`）上复验：

1. 关系本身：`tax-health-check-test15` 是否在该连接的关系内（`REASON_ASSIGNED`）；
2. 授权判定：`may_execute()` 对该智能体是否放行，且**不需要**逐资源授权；
3. 边界：同一连接、同一成员、换成未分配的智能体是否仍然拒绝；
4. 智能体面：`authorized_tools()` 的最终投影（这正是现场报告为 0 的那一层）。

只读：不写库、不改 `config.json`、不调用远端写动作。发现 memo 由适配器的后台发现填充
（与运行中进程同一条路径），脚本只是等待它出现，不阻塞调用路径。
"""

from __future__ import annotations

import sqlite3
import time

CONNECTION_ID = "conn_cN-lhbMpQlwpiL7k"          # weknora-rsmagent
ASSIGNED_AGENT = "tax-health-check-test15"
UNASSIGNED_AGENT = "knowledge-qa-test15"
ACTOR_USER_ID = "usr_EMjtqQ_5s9oey1y1"           # test15 的普通成员


def main() -> None:
    from config import load_config
    load_config()   # the deployment's own configuration, exactly as app.py reads it

    from auth.service import IdentityService
    from integrations.external import assignment, authorization, registry
    from integrations.external import tools as external_tools
    from integrations.external.adapters.mcp import _mcp_tool_provider
    import agent.tools.mcp.external as mcp_external

    db = sqlite3.connect("identity.db")
    db.row_factory = sqlite3.Row
    row = dict(db.execute("SELECT * FROM external_connections WHERE id=?",
                          (CONNECTION_ID,)).fetchone())
    tenant = row["tenant_id"]
    svc = IdentityService("identity.db")

    print("connection        =", row["name"], row["id"], "kind=%s scope=%s"
          % (row["kind"], row["scope"]), "enabled=%s" % row["enabled"],
          "version=%s" % row["version"])
    print("tenant            =", tenant)
    print("actor             =", ACTOR_USER_ID)
    print("open classes      =", sorted(registry.open_classes(row["kind"])))

    # 1. the relation itself -------------------------------------------------
    # The relation is keyed on the logical id: a tenant override stands in for
    # its platform template. This row has no base, so the two coincide.
    logical = str(row["base_connection_id"] or row["id"])
    allowed, reason = assignment.assignment_allows(
        svc._store, tenant_id=tenant, logical_id=logical,
        agent_id=ASSIGNED_AGENT)
    print("\n[1] assignment    =", "allowed=%s reason=%s" % (allowed, reason))

    # 2. the authorization decision -----------------------------------------
    def decision(agent_id):
        return authorization.may_execute(
            svc, actor_user_id=ACTOR_USER_ID, tenant_id=tenant,
            kind=row["kind"], action="tools.read", scope="tenant",
            agent_id=agent_id, connection_id=CONNECTION_ID)

    print("[2] may_execute   = assigned=%s unassigned=%s (no external: grant"
          " exists in this deployment)" % (decision(ASSIGNED_AGENT),
                                           decision(UNASSIGNED_AGENT)))

    # A memo entry exists from the warm-up pass, but a *pending* entry reads as
    # an empty set, so "empty" is not yet "published nothing": wait for the
    # background discovery to land (or the deadline) before projecting.
    def memo_names():
        return mcp_external.remembered_tool_names(
            tenant_id=tenant, connection_id=CONNECTION_ID,
            version=int(row["version"]))

    _mcp_tool_provider(tenant, "verification")   # triggers background discovery
    deadline = time.time() + 40
    names = memo_names()
    while not names and time.time() < deadline:
        time.sleep(0.5)
        names = memo_names()

    print("\n[3] memo names    =", len(names or ()), "published by the server")
    if names:
        print("      ", ", ".join(sorted(names)))

    for label, agent_id in (("assigned  ", ASSIGNED_AGENT),
                            ("unassigned", UNASSIGNED_AGENT)):
        bindings = external_tools.authorized_tools(
            tenant_id=tenant, actor_user_id=ACTOR_USER_ID, identity=svc,
            agent_id=agent_id)
        mcp = [b for b in bindings if b.tool.kind == row["kind"]]
        print("    %s %-22s -> %d bindings" % (label, agent_id, len(mcp)))
        if label.strip() == "assigned":
            for b in mcp:
                print("      ", b.tool.name)

    # 4. the seam an agent turn actually builds its tool list from -----------
    # ``external_tools_for`` resolves its own identity service from
    # ``conf()['identity_db_path']``, so this is a different wiring from the
    # injected one above; both are checked because both are production paths.
    from agent.tools.external.external_tool import external_tools_for

    print("\n[4] external_tools_for (the agent turn's own seam)")
    for label, agent_id in (("assigned  ", ASSIGNED_AGENT),
                            ("unassigned", UNASSIGNED_AGENT)):
        tools = external_tools_for(tenant_id=tenant,
                                   actor_user_id=ACTOR_USER_ID, agent_id=agent_id)
        print("    %s %-22s -> %d tools" % (label, agent_id, len(tools)))
        if label.strip() == "assigned":
            for tool in tools:
                print("      ", getattr(tool, "name", tool))


if __name__ == "__main__":
    main()
