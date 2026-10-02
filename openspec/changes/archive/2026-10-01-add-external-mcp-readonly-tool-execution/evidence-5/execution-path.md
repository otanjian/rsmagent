# 只读执行链路：从发现到远端（任务 3.6 / 5.4）

## 1. 一次只读调用实际经过的链路

```
智能体工具（agent/tools/mcp/external.py#ExternalMcpTool）
  └─ integrations.external.tools.dispatch(按绑定名 re-authorize)
       └─ adapters/mcp.py#_mcp_dispatcher
            ├─ 绑定 metadata 有 remote_tool → 封装为 {tool: <远端名>, arguments: 模型参数}
            └─ ConnectionRuntime.invoke（开放类 → 风险目录 → 审批 → 配额 → 审计）
                 └─ adapters/mcp.py#_invoke_tools_read
                      ├─ remembered_tool_names(...) 无该连接当前版本的发现结果
                      │     → tool_not_published（未建连、未发请求）
                      ├─ 名字不在已发布的发现结果内
                      │     → tool_not_published（未建连、未发请求）
                      └─ 已发布 → call_tool_strict(<远端名>, arguments)
```

判定都在**建立连接之前**：

- `tools.read` 的切片由运行时按 `action in adapter.write_actions` 推导（`runtime.py:953`）。`tools.read` 不在 `WRITE_ACTIONS`，因此要求 `read_execute`；`tools.call` 要求 `write_execute`，两者不互相打开。
- 可调用的名字集合来自**服务器自己的发布**（`tools/list` 的发现结果，按连接行 memo）。连接没有逐工具声明字段，远端 `annotations.readOnlyHint` 从不参与判定。

## 2. 落地后的证据（`tests/test_external_mcp_adapter.py`）

| 用例 | 断言 |
| --- | --- |
| `test_read_only_tool_calls_are_a_read_action_separate_from_the_write_one` | `tools.read` 在 `ACTIONS`、不在 `WRITE_ACTIONS`、风险条目 low/write=False |
| `test_a_per_tool_declaration_is_not_a_configuration_field` | `read_only_tools` 不是 MCP 非秘密配置键；提交它以 `unknown_field` 被拒 |
| `test_a_stored_row_that_still_carries_the_removed_key_keeps_working` | 遗留行可读、可调、再保存时该键自然消失 |
| `test_a_published_tool_runs_through_the_read_action` | 已发布工具经读动作跑通，无审批，参数原样到达远端 |
| `test_a_tool_the_connection_did_not_publish_is_never_reached` / `test_a_tool_that_stops_being_published_is_refused` | 未发布（或不再发布）的名字以 `tool_not_published` 被拒，远端未被触达 |
| `test_a_read_before_the_connection_was_discovered_is_refused` | 发现结果未知（未 memo / 版本不匹配）时不放行 |
| `test_the_remote_read_only_hint_changes_nothing` | 远端自称只读不改变投放、动作归属与风险等级 |
| `test_published_candidates_are_offered_when_only_the_read_class_is_open` / `test_a_connection_needs_no_declaration_to_offer_its_tools` | 只读切片开放即投放全部已发布候选；写动作不因此开放 |
| `test_a_discovered_tool_call_carries_the_remote_name_and_the_model_arguments` | 模型只给该工具自己的参数，远端名由绑定注入 |
| `test_a_remote_parameter_named_tool_is_not_read_as_the_tool_name` | 远端 schema 里的 `tool` 字段原样进入 `arguments`（无参数注入） |
| `test_a_connection_level_read_keeps_the_explicit_form` | 连接级绑定不二次封装 |
| `test_closing_read_execution_refuses_a_read_tool_that_was_just_listed` / `test_a_disabled_connection_refuses_the_read_action` | 切片关闭、连接停用后重新放行判定 |

未发布/未知两条路径同时断言 `initialized == 0`、`calls == []`：拒绝发生在握手之前，名字不会作为探测被送到远端。

分配维度（`tests/test_external_connection_assignment_runtime.py`）：

- `test_a_published_read_tool_is_assigned_like_any_other_capability`：未分配时看不到也调不通（`ASSIGNMENT_CODES`），分配后投放。
- `test_the_read_tool_is_absent_without_the_read_grant`：分配之外还要有 `external:mcp:mcp.tools.read` 资源授权。

控制台契约（`tests/test_external_connections_frontend.cjs` / `tests/test_external_connections_browser.cjs`）：

- MCP 抽屉不渲染逐工具声明字段（远程与 stdio 两种形态都不渲染）；
- 保存请求的 `config` 只有连接级键（远程 `auth/transport/url`，stdio `transport/command/args/env_keys`），遗留 draft 里的旧键不会被重新提交。

## 3. 部署侧仍需满足的条件（本 change 不代操作者打开）

本 change 只打开**代码路径**。要让「已分配 MCP 的智能体真的能只读调用」，该部署还需：

1. 就绪配置里开放 MCP `read_execute`（`config.json` → `external_connections.readiness.mcp.read_execute = true`）；
2. 执行者（成员 / 智能体所用角色）持有 `external:mcp:mcp.tools.read` 的资源执行授权（`tool:<rid>:execute` + 功能权限 `external.connections.execute`）。

**不再需要**「连接声明哪些工具可调用」这一步：可调用集合就是服务器发布的那一份。缺上面任意一件，工具面都不会出现该连接的工具——这是设计意图，不是缺陷：工具可见性与实际调用都走既有资源执行授权与按连接分配，不为读动作另开旁路。

## 4. 未实施项（保持原状）

- MCP `write_execute` 仍未开放，`tools.call` 的风险等级（high）与审批要求未变，第三方业务写入未验收。
- 未采信远端 `readOnlyHint`，也未做「按名字启发式推断只读」：服务器的注解是自述，不是授权。
- 未引入每工具级授权对象、远端工具目录同步或结果缓存：目录变更靠连接 `version` 前进触发重新发现（`agent/tools/mcp/external.py` 的 memo 以行内容与秘密标记为键），未新增缓存寿命。
- 逐工具判定的撤销方式与新增方式相同——服务器不再发布该名字，或发现结果按行失效——没有本地名单需要维护。
