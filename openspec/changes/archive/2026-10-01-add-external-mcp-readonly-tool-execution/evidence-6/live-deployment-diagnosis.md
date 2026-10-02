# 现场诊断：税务健康体检为何看不到知识库 MCP 工具

对应用户报告:`给税务健康体检已经分配了,mcp知识库,为什么看不到`。
本文件记录**当前部署**(仓库根 `config.json` + `identity.db`)的实测状态,不改动任何数据或开关。

## 结论

工具不可见由**两道运行闸门**造成,与「分配」无关;其中第二道是本次 change 修复的代码缺口(`tools.read` 动作不存在),第一道(执行主体缺少 `external:mcp:mcp.tools.read` 逐资源授权)由**第 7 组**从设计上消除:连接已分配给该智能体时,分配即该连接的授权。

## 逐条核对

| # | 条件 | 现场实测 | 判定 |
| --- | --- | --- | --- |
| 1 | 智能体已分配该连接 | `external_connection_agent_assignments`:`tax-health-check-test15` → `conn_cN-lhbMpQlwpiL7k` | ✅ 已满足 |
| 2 | 连接已启用 | `external_connections.enabled = 1`(`weknora-rsmagent`) | ✅ 已满足 |
| 3 | 连接声明 `read_only_tools` | 本 change 已**删除**该字段（它曾是早期设计的连接级逐工具名单）。现场 `config_json` 里仍留有早期写入的该键，代码按惰性键忽略 | ✅ 不再需要 |
| 4 | 部署开放 `read_execute` 切片 | `config.json` → `external_connections.readiness.mcp = {"test": true, "read_execute": true}` | ✅ 已开放 |
| 5 | 智能体经只读动作触达工具 | `tools.read` 动作在本 change 之前不存在;`tools.call` 属写语义且 MCP `write_execute` 不在可开放集合内 | ✅ 本 change 已修 |
| 6 | 执行者持有 `external:mcp:mcp.tools.read` 资源授权 | `tenant_resource_grants` / `role_resource_grants` 中无 `resource_id LIKE 'external:%'` 记录 | ✅ 第 7 组后不再需要(见下) |

## 第 5 条的历史成因

`OPENABLE_CLASSES[KIND_MCP] = {"test", "read_execute"}`。`read_execute` 开放时,MCP 只投放 `resources.read` / `prompts.get`;唯一的工具动作 `tools.call` 被归入 `write_execute`,而 MCP 的写切片不可开放。于是**任何**智能体、**任何**分配、**任何**开关组合下,MCP 发现的工具都不会出现在工具列表里 —— 这正是第 5 条描述的缺口,也是 `add-external-connection-agent-assignment` 只在卡片上暴露症状、却无法让工具可用的原因。

## 远端可用工具(来自最近一次连接测试的 metadata)

`weknora-rsmagent` 最近一次 `external_connection_tests` 记录 `tool_count: 12`:

```
add_document, ask, delete_document, grep_chunks, list_documents, list_knowledge_bases,
read_document, search_knowledge, update_document, wiki_index, wiki_read_page, wiki_search
```

按本 change 反转后的判定口径,**全部 12 个都是可调用的**:连接没有逐工具名单,可调用的名字集合就是服务器发布的这一份。

```
add_document, ask, delete_document, grep_chunks, list_documents, list_knowledge_bases,
read_document, search_knowledge, update_document, wiki_index, wiki_read_page, wiki_search
```

即 `add_document` / `delete_document` / `update_document` 也经 `tools.read` 可调用——它们不再有本地逐工具判定拦一道。这是反转设计的**明示后果**:远端服务器既是发布者也是自述者,本地不再替它分类;`tools.call`(写语义、high 风险、需审批、本 build 不可开放)保持原样,不再承担发现工具的载体。

## 让工具真正可用的最小运维动作

1. `config.json` → `external_connections.readiness.mcp.read_execute = true`(开放部署切片) —— **本部署已完成**;
2. ~~给该智能体的执行主体授予 `external:mcp:mcp.tools.read`~~ —— **第 7 组后不再需要**:连接已分配给目标智能体,分配即该连接的授权(功能权限 `tool.execute` 仍在,内置 `member` 默认持有);
3. 该连接已分配给目标智能体 —— 已满足(见第 1 行)。

三项齐备后,智能体将看到 `mcp.tools.read.<connection_id>.<remote_tool>` 绑定,并经参数封装直接调用远端工具。**不再需要**在连接上声明工具名单,也**不再需要**单独的平台资源授权行。

## 执行记录（用户授权后）

改动一：`config.json` → `external_connections.readiness.mcp` 增加 `"read_execute": true`
（原文件备份在 `/tmp/config.json.bak-20260927-161849`）。

改动二（**已被后续设计反转取代**）：经 `ExternalConnectionService.update_connection()` 为
`conn_cN-lhbMpQlwpiL7k` 写入 `read_only_tools`，actor = `usr_9ZxVPz7M2FuOro1q`
（租户 `test15` 的管理员），`expected_version=3`，**未传 `secrets`**（凭据未被触碰）。
结果：`version 3 → 4`，非秘密配置变为：

```json
{"transport": "streamable_http",
 "read_only_tools": ["ask", "grep_chunks", "list_documents",
                     "list_knowledge_bases", "read_document",
                     "search_knowledge", "wiki_index", "wiki_read_page",
                     "wiki_search"],
 "url": "http://localhost:8080/mcp/<id>", "auth": "header",
 "header_name": "Authorization"}
```

该写入当时是为了让早先的「逐工具名单」设计放行工具。名单设计随后被移除（见下方
「反转后复验」），留在这里的 `read_only_tools` 成为**惰性键**：读与调用路径都不校验它，
它不改变任何行为，并在该行下一次从控制台保存时自然消失。**不清理也不会出错。**

审计：`audit_events` 记 `external_connection.update` / `success`（time 1790497155）。

### 独立进程复验（`load_config()` 后，**旧口径**，已被下方复验取代）

```
open_classes(mcp)            = ['configure', 'read_execute', 'test']
discovered_tools_offered     = True
offered actions              = ('tools.list', 'resources.read', 'tools.read')
projected bindings (3)       = mcp.tools.list.<id>, mcp.resources.read.<id>,
                               mcp.tools.read.<id>
live discovery               = ok=True （握手 + tools/list 成功）
remote tools (12)            = add_document, ask, delete_document, grep_chunks,
                               list_documents, list_knowledge_bases,
                               read_document, search_knowledge, update_document,
                               wiki_index, wiki_read_page, wiki_search
```

「9 个已声明放行 / 3 个未声明拒绝」的分流当时成立，名单移除后不再存在——见下方复验。

### 回归

```
.venv/bin/python -m pytest tests/test_external_mcp_adapter.py \
  tests/test_external_connections_api.py tests/test_external_connection_service.py \
  tests/test_external_dispatch_gate_surfaces.py tests/test_external_authorization.py \
  -q -p no:randomly
→ 2 failed, 169 passed
```

两条失败即第 4 节所述的变更前既有缺陷，与本次改动无关。

### 运行中进程尚未生效 → 已重启

`app.py`（原 PID 35987，已运行 3 小时）在启动时一次性 `load_config()`，此后不再读盘，
而 `registry._readiness_config()` 读的是该进程内的配置字典。控制台改配置会走
`CowCli` 的 `load_config()` 热重载，直接编辑 `config.json` 则不会。

已在用户授权后重启（`COW_DESKTOP=1` + 原 `COW_CREDENTIAL_MASTER_KEY`，独立会话）：

```
config.json 修改于      2026-09-27 16:18:53
连接声明写入于          2026-09-27 16:19:15（audit external_connection.update）
app.py 重启于           2026-09-27 16:23:55（PID 78281，PPID=1）
lsof -iTCP:9899         LISTEN
日志 ERROR              无
GET /                   303（未登录重定向，符合预期）
```

重启后以同一份配置复验连接测试：`outcome=ok`，`tool_count=12`，
`open_classes = ['configure', 'read_execute', 'test']` —— 策略链路（明文 http 例外）
在加载配置的进程里同样放行。

## 反转后复验（2026-09-27 16:38，只读，未改动任何数据）

名单移除后以同一份现场数据重新投影。脚本只读：`load_config()` → 直接调用适配器与发现 memo，
不写库、不改 `config.json`。

```
open classes            = ['configure', 'read_execute', 'test']
offered actions         = ('tools.list', 'resources.read', 'tools.read')
discovered_tools_offered= True

== OneAgent HTTP MCP (conn_8r2lE23PRk0Sd_Wb, enabled=1, version=3)
   config keys         = ['auth', 'header_name', 'transport', 'url']

== weknora-rsmagent (conn_cN-lhbMpQlwpiL7k, enabled=1, version=4)
   config keys         = ['auth', 'header_name', 'read_only_tools', 'transport', 'url']
   leftover declaration= True ['ask', 'grep_chunks', 'list_documents', ...]
```

后台发现完成后（等待 `remembered_tool_names` 给出结果，不阻塞调用路径）：

```
memo names (12)                = add_document, ask, delete_document, grep_chunks,
                                 list_documents, list_knowledge_bases, read_document,
                                 search_knowledge, update_document, wiki_index,
                                 wiki_read_page, wiki_search
discovered bindings for the assigned connection: 13
   mcp.tools.read.conn_cN-lhbMpQlwpiL7k            ← 连接级读动作
   mcp.tools.read.conn_cN-lhbMpQlwpiL7k.add_document
   mcp.tools.read.conn_cN-lhbMpQlwpiL7k.ask
   mcp.tools.read.conn_cN-lhbMpQlwpiL7k.delete_document
   mcp.tools.read.conn_cN-lhbMpQlwpiL7k.grep_chunks
   mcp.tools.read.conn_cN-lhbMpQlwpiL7k.list_documents
   mcp.tools.read.conn_cN-lhbMpQlwpiL7k.list_knowledge_bases
   mcp.tools.read.conn_cN-lhbMpQlwpiL7k.read_document
   mcp.tools.read.conn_cN-lhbMpQlwpiL7k.search_knowledge
   mcp.tools.read.conn_cN-lhbMpQlwpiL7k.update_document
   mcp.tools.read.conn_cN-lhbMpQlwpiL7k.wiki_index
   mcp.tools.read.conn_cN-lhbMpQlwpiL7k.wiki_read_page
   mcp.tools.read.conn_cN-lhbMpQlwpiL7k.wiki_search
```

结论：

- 遗留的 `read_only_tools` 键**不参与任何判定**：12 个已发布工具全部按 `tools.read` 投放，
  与名单内容（9 个）无关；
- 另一条 MCP 连接 `OneAgent HTTP MCP` 的发现失败（`policy/target_not_allowed`：其 url 是
  `https://api.example.com/mcp` 占位主机，被出网策略拒绝），因此没有逐工具绑定 ——
  这是策略拒绝，不是名单问题；该连接也未分配给任何智能体；
- 智能体面最终可见的列表还要过 `authorized_tools()` 的资源授权（第 6 行），本部署该项未授权，
  所以「卡片/投影层已投放 12 个」与「智能体实际看到 0 个」在当前部署上仍会并存，直到补上第 2 项运维动作。

复现脚本（只读，临时文件，未入库）：

```bash
.venv/bin/python - <<'PY'
from config import load_config; load_config()
import sqlite3, agent.tools.mcp.external as mcp_external
from integrations.external.adapters.mcp import _mcp_tool_provider
db = sqlite3.connect("identity.db"); db.row_factory = sqlite3.Row
row = dict(db.execute("SELECT * FROM external_connections WHERE name='weknora-rsmagent'").fetchone())
provider = _mcp_tool_provider(row["tenant_id"], "verification")   # 触发后台发现
print(mcp_external.remembered_tool_names(tenant_id=row["tenant_id"],
                                         connection_id=row["id"], version=row["version"]))
PY
```

## 复现命令

```bash
sqlite3 identity.db "SELECT agent_id, logical_connection_id FROM external_connection_agent_assignments;"
sqlite3 identity.db "SELECT name, enabled, version, config_json FROM external_connections WHERE kind='mcp';"
sqlite3 identity.db "SELECT json_extract(detail_json,'\$.metadata.tools') FROM external_connection_tests WHERE result='ok' ORDER BY created_at DESC LIMIT 1;"
sqlite3 identity.db "SELECT resource_kind, resource_id, action FROM tenant_resource_grants WHERE resource_id LIKE 'external:%';"
.venv/bin/python -c "import json;print(json.load(open('config.json'))['external_connections']['readiness'])"
```

## 第 7 组落地后复验（2026-09-27 17:09，只读，未改动任何数据）

脚本:`evidence-6/live_assignment_authorization.py`,输出:`evidence-6/live_assignment_authorization.txt`。
同一份现场数据(仓库根 `config.json` + `identity.db`),只读,不写库也不改配置。

```
connection   = weknora-rsmagent conn_cN-lhbMpQlwpiL7k kind=mcp scope=tenant enabled=1 version=5
actor        = usr_EMjtqQ_5s9oey1y1（test15 的普通成员，无任何 external: 逐资源授权）

[1] assignment    = allowed=True reason=assigned
[2] may_execute   = assigned=True unassigned=False
[3] memo names    = 12 published by the server
    assigned   tax-health-check-test15 -> 15 bindings   ← 第 6 行缺口消除
    unassigned knowledge-qa-test15    -> 0 bindings
[4] external_tools_for (智能体回合自己的装配缝)
    assigned   tax-health-check-test15 -> 15 tools
    unassigned knowledge-qa-test15    -> 0 tools
```

对照:同一脚本在改动前会给出 `may_execute = False`、`assigned -> 0 bindings` —— 这正是现场
「已经分配了却看不到」的直接原因。修复面是 `may_execute` 的第四条入口:
`configured=1` 且当前 Agent 在关系内即视为该连接的授权;逐资源授权不再叠加,而功能权限、
切片、风险、审批与配额照旧(`tests/test_external_authorization.py` 的「分配即该连接的授权」一组,
10 条用例含边界)。

### 运行中进程已重启（代码改动，需重启生效）

原 PID 78281 于 2026-09-27 16:23:55 启动，在本次改动之前；已按原环境
（`COW_DESKTOP=1` + 同一 `COW_CREDENTIAL_MASTER_KEY`，迁移前主密钥已先用
`resolve_secret` 验证可解密存量凭据）重启：

```
kill 78281                 → app-lifecycle.log 记 clean interpreter exit（uptime 2638s）
新进程                      PID 89609，PPID=1（start_new_session 独立会话，不受启动它的 shell 影响）
lsof -iTCP:9899 LISTEN      ✅
GET /                      303（未登录重定向，符合预期）
新日志 ERROR 行数           0
```

重启后以同一份数据复验 `[4]`：`tax-health-check-test15 → 15 tools`。
