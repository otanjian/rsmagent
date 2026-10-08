# 证据：第 4 组（服务端按会话标识解析归属与导航通道）

日期：2026-10-05。改动文件：`Scene/sap_workbench/backend/store.py`、`backend/runtime.py`、`backend/http.py`、`channel/web/route_registry.py`、`channel/web/web_channel.py`、`tests/test_sap_workbench_plugin_bridge.py`。

## 目的

项目插件运行在**共享的编码服务进程**里，它只知道自己的 OpenCode `sessionID` 与项目目录。它没有任何平台会话，因此既不能走控制台的身份门，也不能反过来用"可信调用方"这种无凭据捷径。两个问题必须分开回答：

1. **谁在调用** —— 用编码服务自身的 HTTP 凭据证明；只有已经持有该凭据的进程能出示。
2. **驱动谁的左侧画面** —— 只从 `sessionID` 反查归属；请求体不能指定用户、SAP 账号或绑定。

## 实现

### 归属解析（4.1）

`WorkbenchStore.binding_for_session(remote_session_id)`：

- 按 `remote_session_id` 反查，`state='closed'` 的行不返回；
- 派生会话 id 已包含 tenant/user/agent/request，故一行唯一；**命中两行时返回 `None`**。两个候选意味着该标识已不再唯一标识一个绑定，任选其一都会让一个 owner 的调用打到另一个 owner 的浏览器；
- 非法或超长标识直接返回 `None`。

### 执行链复用（4.2 / 4.5）

`WorkbenchRuntime.bridge()` 原先把「校验服务/会话匹配」与「执行」写在一个方法里。现拆为：

- `bridge()`：仅做宿主的 framimg 校验（`service_id` / `session_id` 匹配），随后委托 `dispatch()`；
- `dispatch(action, arguments, call)`：`authorize()` → `admit_action` → `consume_quota` → `audit('action.dispatch')` → 执行 → `finish_action` → `audit('action.succeeded')`；
- `navigate(transaction, call)`：插件通道入口，调用 `dispatch('transaction_open', ...)`。

关键点：通道**经存活的绑定实例**执行，`authorize()` 会用开启时捕获的平台令牌重新解析主体并复核配置版本与 coding 可用性，因此不需要、也没有新增任何"无令牌授权路径"。绑定不存在或已关闭 ⇒ 不产生任何 SAP 操作。

### 调用方认证（4.3）

`_require_coding_service()`：

- 校验 HTTP Basic 与 `CodingSettings.username` / `password`（`hmac.compare_digest`，常量时间）；编码未启用或未配密码 ⇒ 503；
- **拒绝携带转发头（`X-Forwarded-For` / `X-Real-IP` / `X-Forwarded-Host`）或 `REMOTE_ADDR` 非回环的请求**。反向代理与 RDAI 应用同机，因此"回环"本身不足以证明是本地调用；转发头的缺失才是。没有这条，服务凭据就能从公网驱动 SAP 导航；
- 路由必须在注册表登记，故取 `public`（调用方本无控制台会话，任何含会话的策略都无法表达它），**门由 handler 承担**，并在注册表注释中写明。

请求体只接受 `session_id` / `transaction` / `call_id`；`_body` 的精确键集使 `user_id`、`binding_id`、`sap_user` 之类的字段直接 `invalid_request`——调用方连"以为选中了某个 owner"都做不到。

## 测试

`tests/test_sap_workbench_plugin_bridge.py`：**27 passed**。

| 覆盖 | 断言 |
|---|---|
| 归属解析 | 仅凭会话标识解析出 tenant/user/binding；`None`/空/未知/超长/非字符串/已关闭/歧义 一律 `None` |
| 调用方认证 | 无凭据、非 Basic、非 base64、错密码、错用户名、非回环、三种转发头 ⇒ `bridge_unauthorized` 401；`127.0.0.1` 与 `::1` 接受；编码未启用 ⇒ `bridge_unavailable` 503 |
| 身份伪造 | 请求体含 `user_id` / `binding_id` ⇒ `invalid_request` |
| 未归属 | 未知会话 ⇒ `session_not_bound` 403，且账本为空 |
| 未运行 | 已解析但无存活实例 ⇒ `session_not_running` 409，且账本为空 |
| 导航执行 | 经账本/配额/审计，账本终态 `succeeded`，`action.dispatch` 与 `action.succeeded` 均记 |
| 非法事务码 | `invalid_transaction`，账本为空，未调用导航 |
| 授权撤销 | `session_forbidden`，账本为空，未调用导航 |
| 非 iframe 绑定 | `iframe_navigation_unavailable`，账本为空 |

## 回归

| 范围 | 结果 |
|---|---|
| `tests/test_route_registry.py`（路由注册表三腿不变量，含 handler 方法集与实际实现一致） | 27 passed |
| `tests/test_sap_workbench_bridge_boundary.py`、`..._iframe_navigation.py`、`..._purchase_order_bridge.py`、`..._commit_bridge.py`、`..._lifecycle.py` | 120 passed |
| `tests/test_sap_workbench_plugin_bridge.py` | 27 passed |

### 两个失败用例，均为既有环境问题，非本次改动引入

1. `tests/test_sap_workbench.py::test_session_rejects_invalid_request_key_and_password`、`::test_platform_agent_routing_metadata_is_ignored`：`OSError [Errno 10048] bind 127.0.0.1:9911 已被占用`。9911 由**正在运行的真实应用**占用（`python -u app.py`，pid 9784，启动于 16:33，早于本次改动与本次测试）。测试用的是固定代理端口。
2. `tests/test_sap_workbench_shutdown.py::test_interpreter_exit_reaps_exact_children_and_preserves_history[8 个参数]`：`OSError [WinError 10038]` 来自测试自身对管道调用 `select.select`，Windows 不支持。与本次改动无关。

## 未覆盖 / 下一步

- 调用该通道的**项目插件属于第 3 组**：插件的桥接 URL 与凭据来自编码服务进程环境（`OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD` 已在服务进程内），URL 需在启动脚本中注入。
- 第 5 组（权限规则收窄）尚未验证"同项目普通会话即使直接构造调用仍被拒"这一条的**真实服务路径**；当前已有 handler 级拒绝测试，但没有跑通共享服务内的真实插件调用。
- 真实应用（pid 9784）仍运行旧代码，需重启后本次端点才生效；未在未获指示的情况下重启线上进程。
