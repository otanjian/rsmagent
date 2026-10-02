# 相关回归与严格校验（任务 5.1 / 5.3）

环境：`/Users/jiantan/ai_assistant/rsmagent`，`.venv/bin/python -m pytest ... -q -p no:randomly`，脏工作区（含本 change 与在途改动）。按规则跑**相关子集**，不跑全量。

> 基准说明：本 change 在实施中**反转**了早期设计（去掉连接级逐工具声明，改为「发现工具一律按读动作投放」）。下面三节是反转后的重跑结果，命令与早期版本一致，便于逐条对比。

## 1. MCP 适配器与执行链路（本 change 直接相关，16 文件）

```
.venv/bin/python -m pytest \
  tests/test_external_mcp_adapter.py tests/test_external_oauth_binding.py \
  tests/test_external_cutover.py tests/test_external_connections_api.py \
  tests/test_external_connections_menu.py \
  tests/test_external_connection_agent_assignment.py \
  tests/test_external_connection_assignment_runtime.py \
  tests/test_external_connection_assignment_backup.py \
  tests/test_external_test_state_projection.py tests/test_external_risk.py \
  tests/test_external_action_gate.py tests/test_external_authorization.py \
  tests/test_agent_catalog_read_scope.py tests/test_mcp_tool_prefix.py \
  tests/test_mcp_tool_retrieval.py tests/test_mcp_session_recovery.py -q -p no:randomly
```

结果：`1 failed, 326 passed in 27.26s`（失败项是下面第 4 节的既有缺陷）

其中 `tests/test_external_mcp_adapter.py` 单文件：`64 passed`（本 change 重写的第 12 组共 15 条）。

## 2. 连接管理与派发闸门回归（13 文件）

```
.venv/bin/python -m pytest \
  tests/test_external_connection_service.py tests/test_external_mcp_tool_identity.py \
  tests/test_external_inheritance_runtime.py tests/test_external_dispatch_gate_surfaces.py \
  tests/test_external_non_external_regression.py tests/test_external_im_gate.py \
  tests/test_external_scheduler_boundary.py tests/test_external_maintenance_window.py \
  tests/test_external_store_version_guard.py tests/test_external_connection_schema.py \
  tests/test_external_connection_migration.py tests/test_external_connection_agent_tool.py \
  tests/test_external_connections_browser.py -q -p no:randomly
```

结果：`1 failed, 236 passed, 1 skipped in 25.08s`
（失败项见第 4 节；`test_external_connections_browser.py` 在未设置 `NODE_PATH` 时按既有约定 skip）

## 3. 前端与 i18n

```
node --test tests/test_external_connections_frontend.cjs
→ tests 46, pass 46, fail 0   （本 change 重写 3 条：表单无逐工具声明且配置只含连接级键、遗留草稿既不校验也不重提交、stdio 只提交连接级键）

node --test tests/test_console_i18n_parity.cjs tests/test_console_i18n_coverage.cjs
→ tests 10, pass 10, fail 0
```

## 4. 两处失败：变更前既有缺陷，非本次引入

| 用例 | 现象 |
| --- | --- |
| `tests/test_external_connection_service.py::test_plain_member_cannot_manage_tenant_connections` | 普通成员的租户目录读取未被拒（DID NOT RAISE） |
| `tests/test_external_connections_api.py::test_a_tenant_catalogue_read_needs_the_read_permission` | 同一路径经 HTTP 返回 `200 OK`，期望 `403 Forbidden` |

判定方式：在**变更前的 commit**（`8aec12f1`）建干净检出，以同一解释器跑同样两条用例：

```
git worktree add /tmp/rsmagent-head-mcp 8aec12f1
cd /tmp/rsmagent-head-mcp && /Users/jiantan/ai_assistant/rsmagent/.venv/bin/python -m pytest \
  tests/test_external_connection_service.py::test_plain_member_cannot_manage_tenant_connections \
  "tests/test_external_connections_api.py::test_a_tenant_catalogue_read_needs_the_read_permission" \
  -q -p no:randomly
→ 2 failed in 1.31s
```

反转设计后再次以同一方式复核（`git worktree add --detach /tmp/rsm-head HEAD` + 同一用例）：

```
→ 1 failed in 1.12s   （test_a_tenant_catalogue_read_needs_the_read_permission）
```

两条在变更前同样失败，属于既有缺陷，本 change 不顺手修改，单独上报。

## 5. 真实浏览器契约

```
COW_EXTERNAL_BROWSER_OUTPUT=$PWD/openspec/changes/add-external-mcp-readonly-tool-execution/evidence-5/browser \
  NODE_PATH=$(npm root -g) node tests/test_external_connections_browser.cjs
→ external connections browser: 14 scenarios, 0 failed
```

`results.json` 中 `pageErrors` 与 `unexpectedRoutes` 均为空。替换后的场景「MCP 表单不再出现逐条工具声明，只提交连接级配置」录到的写入体：

```
{"kind": "mcp", "name": "知识库 MCP",
 "config": {"transport": "streamable_http", "url": "http://127.0.0.1:8080/mcp", "auth": "none"}}
```

（无 `secrets` 字段，也没有任何枚举远端工具的键；断言同时要求表单里 `#ec-f-read_only_tools` 计数为 0，远程与 stdio 两种形态都不出现。）

截图落在 `evidence-5/browser/mcp-connection-level-only.png`，取代已失效的 `read-only-declaration.png`。

## 6. 严格 OpenSpec 校验

```
openspec validate add-external-mcp-readonly-tool-execution --strict
→ Change 'add-external-mcp-readonly-tool-execution' is valid
```

## 7. D8「分配即该连接的授权」落地后重跑（2026-09-27 17:1x）

第 1、2 节的两条命令按原样重跑，便于与上面的基准数字直接对比。

```
第 1 节 16 文件 → 1 failed, 337 passed in 33.50s   （基准 326 passed，+11 为本组新增用例）
第 2 节 13 文件 → 1 failed, 236 passed, 1 skipped in 46.33s   （与基准一致）
```

两条失败仍是第 4 节的既有缺陷，前后一致。

本组用例集中在 `tests/test_external_authorization.py`（新增「分配即该连接的授权」10 条）与
`tests/test_external_connection_assignment_runtime.py`（原「无读授权即不可见」改写为「分配即可见、
无功能权限仍不可见」，并修正受影响的投放集合断言）。单文件：

```
.venv/bin/python -m pytest tests/test_external_authorization.py \
  tests/test_external_connection_assignment_runtime.py -q -p no:randomly
→ 50 passed
```

### 隔离实验：两条失败与本组无关

把 `_assignment_authorizes()` 临时改为恒 `return False`（即回到改动前的行为）后重跑：

```
.venv/bin/python -m pytest \
  tests/test_external_connection_service.py::test_plain_member_cannot_manage_tenant_connections \
  tests/test_external_connections_api.py::test_a_tenant_catalogue_read_needs_the_read_permission \
  -q -p no:randomly
→ 2 failed
```

两条用例都不经过 `may_execute`（它们问的是目录读取权限 `external.connections.read`），因此在豁免
关闭后同样失败 —— 失败来自在途 change 把该权限并入内置 `member` 角色，与本组无关。

### 现场复验

`evidence-6/live_assignment_authorization.txt`：同一份现场数据上，已分配给
`tax-health-check-test15` 的连接从 `0 bindings` 变为 `15 tools`（3 个连接级 + 12 个远端工具），
未分配的 `knowledge-qa-test15` 仍为 0；`may_execute` 对两者分别为 `True` / `False`。
