# 相关回归与严格校验（任务 5.5）

环境：`/Users/jiantan/ai_assistant/rsmagent`，`.venv/bin/python -m pytest ... -q -p no:randomly`，脏工作区（含本 change 及在途改动）。全部按规则跑**相关子集**，不跑全量。

## 1. 分配能力（本 change 直接相关）

```
.venv/bin/python -m pytest \
  tests/test_external_connection_agent_assignment.py \
  tests/test_external_connection_agent_tool.py \
  tests/test_external_connection_assignment_backup.py \
  tests/test_external_connection_assignment_runtime.py \
  tests/test_external_connection_migration.py \
  tests/test_external_connection_schema.py \
  tests/test_external_connection_service.py \
  tests/test_external_connections_api.py \
  tests/test_external_authorization.py \
  tests/test_external_inheritance_runtime.py \
  tests/test_external_dispatch_gate_surfaces.py -q -p no:randomly
```

结果：`2 failed, 245 passed in 24.57s`

## 2. 连接管理回归

```
... tests/test_external_connection_service.py tests/test_external_connections_api.py \
  tests/test_external_test_state_projection.py tests/test_external_cutover.py \
  tests/test_external_maintenance_window.py tests/test_external_mcp_tool_identity.py \
  tests/test_external_risk.py tests/test_external_store_version_guard.py \
  tests/test_external_action_gate.py tests/test_external_non_external_regression.py \
  tests/test_external_im_gate.py tests/test_external_scheduler_boundary.py
```

结果：`2 failed, 269 passed in 26.23s`

## 3. 智能体与对象可见性回归

```
... tests/test_agent_admin.py tests/test_agent_registry.py \
  tests/test_agent_onboarding_copy_wire.py tests/test_agent_workbench.py \
  tests/test_agent_catalog_read_scope.py tests/test_object_scope.py
```

结果：`134 passed, 3 subtests passed in 2.81s`

## 4. 前端与页面

```
node --test tests/test_external_connections_frontend.cjs \
  tests/test_external_identity_frontend.cjs tests/test_i18n_external_identity_keys.cjs
```

结果：`tests 68, pass 68, fail 0`

```
COW_EXTERNAL_BROWSER_OUTPUT=$PWD/openspec/changes/add-external-connection-agent-assignment/evidence-5/browser \
  NODE_PATH=$(npm root -g) node tests/test_external_connections_browser.cjs
```

结果：`external connections browser: 9 scenarios, 0 failed`（`results.json` 中 `pageErrors` 与 `unexpectedRoutes` 均为空）

## 5. 严格 OpenSpec 校验

```
openspec validate add-external-connection-agent-assignment --strict
→ Change 'add-external-connection-agent-assignment' is valid
```

## 5b. 补充修复后的复跑（中文输入法，`ime-composition.md`）

修复 `channel/web/static/js/external-connections.js` 的输入法组合处理之后，按上面同样的子集复跑：

| 子集 | 结果 |
| --- | --- |
| 分配能力（第 1 节，11 文件） | `2 failed, 245 passed` — 仍是下面两条既有缺陷 |
| 连接管理回归（第 2 节，12 文件） | `2 failed, 269 passed` — 同上 |
| 智能体与可见性（第 3 节，6 文件） | `134 passed, 3 subtests passed` |
| 前端（第 4 节，3 文件） | `tests 71, pass 71, fail 0`（新增 3 条输入法用例） |
| 真实浏览器契约 | `12 scenarios, 0 failed`（新增 3 条输入法场景，`pageErrors`、`unexpectedRoutes` 均为空） |
| `tests/test_external_connections_browser.py` | `1 passed` |

## 5c. 补充修复后的复跑（执行开放状态，`execution-availability.md`）

补齐卡片上的执行开放状态（spec「分配成功但业务执行关闭」原先未落地）之后：

| 子集 | 结果 |
| --- | --- |
| 连接管理 / 分配（第 1、2 节的代表文件，见下） | `84 passed` |
| 前端（`test_external_connections_frontend.cjs`） | `43 pass / 0 fail`（新增 3 条执行状态用例） |
| i18n 基准（`test_console_i18n_parity.cjs`） | `6 pass / 0 fail`（补齐 47 个新 key 与 5 条原因文案后由红转绿） |
| 真实浏览器契约 | `13 scenarios, 0 failed`（新增 1 条执行状态场景，`pageErrors`、`unexpectedRoutes` 均为空） |

```
.venv/bin/python -m pytest tests/test_external_connections_menu.py \
  tests/test_external_connection_agent_assignment.py \
  tests/test_external_connection_assignment_runtime.py \
  tests/test_external_test_state_projection.py \
  tests/test_external_authorization.py -q -p no:randomly
→ 84 passed
```

## 6. 两处失败：变更前既有缺陷，非本次引入

| 用例 | 现象 |
| --- | --- |
| `tests/test_external_connection_service.py::test_plain_member_cannot_manage_tenant_connections` | 普通成员的租户目录读取未被拒（DID NOT RAISE） |
| `tests/test_external_connections_api.py::test_a_tenant_catalogue_read_needs_the_read_permission` | 同一路径经 HTTP 返回 `200 OK`，期望 `403 Forbidden` |

判定方式：在**变更前的 commit**（`8aec12f1`）用 `git worktree add` 建干净检出，以同一解释器跑同样两条用例：

```
git worktree add /tmp/rsmagent-head 8aec12f1
cd /tmp/rsmagent-head && /Users/jiantan/ai_assistant/rsmagent/.venv/bin/python -m pytest \
  tests/test_external_connection_service.py::test_plain_member_cannot_manage_tenant_connections \
  "tests/test_external_connections_api.py::test_a_tenant_catalogue_read_needs_the_read_permission" \
  -q -p no:randomly
→ 2 failed in 1.25s
```

两条在变更前同样失败，属于既有缺陷，本 change 不顺手修改，单独上报。

## 7. 未实施的保持原状

- 未开放 MCP `tools.call` 等执行切片：本 change 未改动执行开关，分配成功不等于第三方业务执行已验收（见 `usage-and-upgrade.md` 第 6 节）。
- 任务清单无遗留未勾选项；未勾选即代表未实施，本轮已全部完成。
