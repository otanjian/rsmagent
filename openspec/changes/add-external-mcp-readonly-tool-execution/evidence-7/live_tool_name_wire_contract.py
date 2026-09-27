# encoding:utf-8
"""现场复验：模型面工具名符合线路契约（只读；除一次 1-token 探针外不调外部）。

现场报告（2026-09-27 19:03）在智能体里问「你可以使用的mcp工具？」得到：

    Invalid 'tools[18].function.name': string does not match pattern
    '^[a-zA-Z0-9_-]+$'  (Status: 400)

名字里的 `.` 来自动作 id（``tools.read``）与旧分隔符。本脚本在同一份现场数据上复验
修复后的三段：

1. 投影：`external_tools_for()` 交给智能体的名字逐个符合 `^[a-zA-Z0-9_-]+$` 且 ≤128；
2. 边界：`build_tools_schema()`（真正发出去的那一层）产出的名字同样符合；
3. 实发：把该 schema 原样发给现场使用的模型，期望不再出现 400。

第 3 步是唯一的外部调用：max_tokens=1，只为看 provider 是否接受这组工具定义。

只读：不写库、不改 `config.json`、不调用远端写动作。发现 memo 由适配器的后台发现填充
（与运行中进程同一条路径）；脚本只等待它出现，不阻塞调用路径。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.error
import urllib.request

CONNECTION_ID = "conn_cN-lhbMpQlwpiL7k"          # weknora-rsmagent
ASSIGNED_AGENT = "tax-health-check-test15"
ACTOR_USER_ID = "usr_EMjtqQ_5s9oey1y1"           # test15 的普通成员

#: 与台账里记录的 provider 反馈同一条约束，字面写出来便于逐字比对。
WIRE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
MAX_TOOL_NAME = 128


def main() -> None:
    from config import load_config, conf
    load_config()
    settings = conf() or {}

    from agent.protocol.agent_stream import build_tools_schema
    from agent.tools.external.external_tool import external_tools_for
    from integrations.external.adapters.mcp import _mcp_tool_provider
    from agent.tools.mcp import external as mcp_external

    db = sqlite3.connect("identity.db")
    db.row_factory = sqlite3.Row
    row = dict(db.execute("SELECT * FROM external_connections WHERE id=?",
                          (CONNECTION_ID,)).fetchone())
    tenant = row["tenant_id"]
    print("connection        =", row["name"], row["id"], "enabled=%s"
          % row["enabled"], "version=%s" % row["version"])
    print("tenant            =", tenant)
    print("agent             =", ASSIGNED_AGENT, "actor =", ACTOR_USER_ID)

    # 后台发现与运行中进程同一条路径；等它落地再投影，否则「空」不是「没发布」。
    _mcp_tool_provider(tenant, "verification")
    deadline = time.time() + 40
    published = mcp_external.remembered_tool_names(
        tenant_id=tenant, connection_id=CONNECTION_ID,
        version=int(row["version"]))
    while not published and time.time() < deadline:
        time.sleep(0.5)
        published = mcp_external.remembered_tool_names(
            tenant_id=tenant, connection_id=CONNECTION_ID,
            version=int(row["version"]))
    print("\n[0] server published =", len(published or ()), "remote tools")

    tools = list(external_tools_for(tenant_id=tenant,
                                    actor_user_id=ACTOR_USER_ID,
                                    agent_id=ASSIGNED_AGENT).values())
    names = [getattr(t, "name", "") for t in tools]
    print("[1] external_tools_for -> %d tools" % len(tools))
    for name in names:
        print("      %-62s %s" % (name, _verdict(name)))
    print("    non-conforming  : %d" % len([n for n in names if not _wire_ok(n)]))
    print("    chars outside the pattern, anywhere in the set : %r" % sorted(
        {c for n in names for c in n if not re.match(r"[A-Za-z0-9_-]", c)}))

    schema = build_tools_schema(tools)
    schema_names = [entry["name"] for entry in schema]
    print("\n[2] build_tools_schema -> %d entries (the layer that is actually"
          " sent)" % len(schema))
    print("    non-conforming  : %d" % len([n for n in schema_names
                                           if not _wire_ok(n)]))
    print("    dropped by gate : %r" % sorted(set(names) - set(schema_names)))

    payload = {"model": settings.get("model") or "deepseek-v4-flash",
               "max_tokens": 1,
               "messages": [{"role": "user", "content": "hi"}],
               "tools": [{"type": "function", "function": {
                   "name": entry["name"],
                   "description": entry["description"] or "",
                   "parameters": entry["input_schema"] or {"type": "object",
                                                           "properties": {}}}}
                   for entry in schema]}
    status, message = _post(settings, payload)
    print("\n[3] provider with the real tool list -> status=%s %s"
          % (status, message))
    print("    19:03 的失败是同一批工具定义得到 400；本次期望 200。")


def _wire_ok(name: str) -> bool:
    return bool(WIRE_NAME_RE.match(name)) and len(name) <= MAX_TOOL_NAME


def _verdict(name: str) -> str:
    return "ok" if _wire_ok(name) else "NON-CONFORMING"


def _post(settings, payload):
    key = settings.get("deepseek_api_key") or ""
    base = (settings.get("deepseek_api_base")
            or "https://api.deepseek.com").rstrip("/")
    request = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, "(accepted)"
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8"))
            detail = (body.get("error") or {}).get("message", "")
        except Exception:  # noqa: BLE001 - the status is the finding either way
            detail = error.reason
        return error.code, detail[:220]


if __name__ == "__main__":
    main()
