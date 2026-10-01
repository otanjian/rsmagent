# 验证与交付证据（任务组 4）

Change: `fix-desktop-local-context-and-tool-calls` · 记录日期 2026-09-29 · 环境见 `phase-1.md`

## 4.1 目录链路针对性回归

命令固定顺序 `-q -p no:randomly`（仓库规则：不默认跑全量）。

```bash
.venv/bin/python -m pytest tests/test_desktop_local_context.py tests/test_tool_protocol_guard.py \
    tests/test_desktop_gateway.py tests/test_desktop_file_access.py tests/test_private_agent_file_scope.py \
    tests/test_agent_stream_content_blocks.py tests/test_parallel_tool_calls.py -q -p no:randomly
# → 108 passed, 3 warnings in 43.22s

node --test tests/test_desktop_workspace_menu_frontend.cjs tests/test_desktop_host_frontend.cjs \
    tests/test_desktop_local_files.cjs tests/test_desktop_ws_client.cjs tests/test_desktop_local_read.cjs \
    tests/test_desktop_device_client.cjs tests/test_desktop_local_files_bridge.cjs \
    tests/test_desktop_core_integration.cjs
# → tests 100 / pass 100 / fail 0

cd desktop/native/fs-guard && cargo test --offline
# → 23 passed; 0 failed
```

覆盖矩阵（对应 tasks.md 4.1）：

| 要求 | 证据 |
|---|---|
| 真实数据往返（本机 helper） | `tests/test_desktop_local_read.cjs`「a real open_root/list/stat/read/search round trip」——经真实 `fs-guard` 进程读写真实临时目录 |
| 非法/过期绑定 | `tests/test_desktop_local_context.py`：`VerifyReferenceTests`（他人/他会话/他 Agent/已撤销/陈旧版本/未知 workspace/停用成员）、`test_revoked_grant_fails_before_enqueue` |
| 版本与撤销 | `tests/test_desktop_local_read.cjs`「a revoked grant stops resolving even with the workspace still mapped」；`tests/test_desktop_local_files.cjs`「a tenant or server change revokes the matching grants」 |
| 超时 | `test_leased_device_without_an_answer_hits_the_deadline`（`deadline_exceeded`）、`test_no_live_lease_times_out_as_device_offline`（`device_offline`） |
| 取消 | `test_cancelled_command_reports_cancelled`、`test_pre_cancelled_run_does_not_wait`；设备侧 `tests/test_desktop_device_client.cjs`「stop closes the socket and sends nothing further」 |
| 排队回执不算成功 | `test_waits_for_succeeded_and_returns_device_payload`（先入队后终态）；`_await_terminal` 仅在终态返回 |
| 切换后的旧响应 | 服务端：`VerifyReferenceTests` 的他会话/他会话绑定；主进程：`tests/test_desktop_local_files.cjs`「bindContext refuses a stale generation」；页面：请求键比对（`console.js:7292`、`:7315`，见 4.4） |
| 模型参数不能替换绑定 | `test_model_supplied_binding_id_cannot_swap_the_target`、`test_schema_has_no_authorization_arguments` |
| 截断/分页如实返回 | `tests/test_desktop_local_read.cjs`「list maps to the helper page and keeps paging markers」「list and read_text limits are clamped to the contract on this side」 |

本次修复同时修正了 `tests/test_desktop_device_client.cjs` 的两处测试自身缺陷（不改断言口径）：
假 socket 现在遵循真实客户端的"自己关闭的 socket 不再上报 close"，且每个用例结束都会停止 client ——
此前失败用例会漏掉 `client.stop()` 并留下重连定时器，表现为整轮挂起 2 分钟而不是报告失败。
由此暴露并修掉了一个真实缺陷：空闲超时原本只关 socket、不上报重建连接，半开连接会一直静默（现在走同一重建路径）。

## 4.2 模型回归

`tests/test_tool_protocol_guard.py` 12 例通过，明细见 `phase-3.md`：标准调用、无调用的 DSML 异常、
分片拼接后的异常、围栏/行内/引用示例、结构化调用旁的文本标记；异常用例断言
"抛在写历史之前"（执行次数为零的等价条件）与错误码 `tool_protocol_error`。

**未完成**：真实模型出口冒烟未执行（无可用真实 provider 凭据/原始 SSE）。因此不宣称 provider 兼容，
也不宣称"已修复模型"——只宣称本地接缝对已知异常有了明确失败。规格要求"真实冒烟与模拟测试分别记录"，
本文件如实记录为一半。

## 4.3 当前桌面实测 — **未完成**

要求是"选择目录→询问清单→设备读取→实际工具结果→最终回答→再读已知文本文件"的真机实测。
本环境**没有**完成它，原因与现状如下（不含任何 mock 成功声明）：

1. **设备网关不由本机后端提供**。`python -m integrations.desktop --bind 127.0.0.1 --port 9877` 是独立
   aiohttp 服务（`integrations/desktop/gateway.py:291`），同源可达性依赖反向代理做 Upgrade
   （`docs/ops-runbook.md`「同源网关与反向代理」）。桌面客户端从**配置的源**推导 `wss://<host>/api/desktop/connect`
   （`device-connection.ts:24`），因此真机读取需要"Web 与设备 WSS 同源"的部署；本机直接跑 `app.py`
   （`http://localhost:9876`）时该路径不可达，`client_files` 只能如实返回 `device_offline`。
2. **现有 Electron E2E 不能直接承载该链路**。`desktop/e2e/serve-fixture.py` 只提供 HTTP(S) 与
   `/control` 探针，没有 WS(Upgrade) 转发，也没有启动 gateway 进程；`desktop/e2e/main.e2e.cjs` 没有原生目录
   对话框替身；`_ModelHandler` 只流固定文本，不产生结构化 `tool_calls`。补齐这四项（WS 中继 + gateway 进程 +
   选择器替身 + 脚本化 tool_call 模型）是本次未完成的工作量，而不是"已用 mock 通过"。
3. **顺带发现的既有缺陷（不属于本 change，未顺手修改）**：`desktop/e2e/remote-workbench.spec.mjs:383`
   仍断言本机后端源为 `http://127.0.0.1:<port>`，而产品已改为有意播报 `http://localhost:<port>`
   （`desktop/src/main/index.ts:519`，为了与 Web 控制台共享 Cookie jar）。实测
   `node desktop/e2e/run-remote-workbench.mjs` → 17 例中 2 通过、15 失败（372s，首例即
   `the bundled backend is a plain loopback origin, got http://localhost:9876`，其后为级联超时）。
   该失败与本 change 无关，但会挡住任何基于该 harness 的真机验证；需单独立项处理。

**人工执行清单**（有可用部署时按此验收，全部为真实数据）：

1. 部署同源 HTTPS：Web 应用 + `python -m integrations.desktop`，反向代理把 `/api/desktop/connect` 以 Upgrade
   转到 gateway；确认客户端 `GET /api/desktop/meta` 报 `desktop_local_files` 可用。
2. 桌面客户端连该源并登录；临时目录内放一个中文名文件、一个子目录和一个已知文本文件；**服务器默认目录另放不同文件**。
3. 选择本机目录 → 确认界面显示该目录名，且绑定/workspace/版本齐备（失败时应给原因而不是新标签）。
4. 提问"这个目录有哪些文件" → 核对工具返回与临时目录一致，且**不**包含服务器默认目录的条目。
5. 读取已知文本文件 → 内容与磁盘一致，最终回答基于真实读取。
6. 记录：客户端与服务器版本、源、目录名、工具返回摘要与最终回答（脱敏，不含绝对路径）。

## 4.4 兼容、回退与文档

| 项 | 结论 | 证据 |
|---|---|---|
| 普通浏览器不受影响 | 无宿主 → 不提供本机目录入口，`bindContext` 返回 `no_host`；`/message` 不带 `desktop_context`，服务端按"未选择目录"处理 | `tests/test_desktop_workspace_menu_frontend.cjs`「a browser cannot choose a local workspace directory」「CowDesktopHost.canChooseWorkspace is true only when the host opens local files」；`tests/test_desktop_local_context.py::test_absent_reference_is_none` |
| 服务器项目保持原规则 | 引用与项目选择相互独立：无引用时 `client_files` 明确拒绝而不回落；服务器 cwd 仍走既有项目解析 | `test_no_reference_is_invalid_request`；`console.js:7073`（仅当当前选择是 `desktop:` 且已确认才带引用） |
| 能力开关关闭 | `client_files` 返回 `feature_unavailable`；桥不暴露 `chooseWorkspace`/`bindContext`；IPC 侧同样拒绝 | `test_feature_closed_refuses_honestly`；`remote-host-ipc.ts:228,267`；`local-files-bridge.ts:36` |
| 旧目录标签需重连/重选 | 引用只存在内存中（页面 + 主进程映射），刷新或重启后为空 → 必须重新选择；候选文件不会自动成为授权 | `console.js:7051`、`local-read-assembly.ts:75`；`tests/test_desktop_local_files.cjs`「candidates persist absolute paths with 0600 and never auto-activate」 |
| 断开/撤销回退 | `disconnectWorkspace` → 通知读路径撤销；撤销后同一 workspace 不再解析；登出/租户切换同样清理 | `remote-host-ipc.ts:256-262`；`tests/test_desktop_local_read.cjs`「a revoked grant stops resolving…」「dispose drops every workspace and the connection」；`tests/test_desktop_local_files.cjs`「disconnect clears the active grant without deleting candidates」 |
| 无数据库迁移 / 无跨平台发布任务 | 未新增表、迁移或发布步骤；沿用既有开关与既有平台门槛 | `phase-2.md` §2.1；本 change 未改 `auth/store.py` schema、`desktop/electron-builder` 配置 |

文档：用户指引与运维手册已补充本 change 的行为与限制
（`openspec/changes/add-desktop-remote-web-workbench/docs/user-guide.md` 的「本机目录的可用条件」一节、
`docs/ops-runbook.md` 的「本机目录读取依赖」一节）。

## 未完成清单（不得勾选）

- 4.3 真机实测（选择目录→清单→读取→最终回答）——受阻于 4.3 第 1/2 条。
- 1.2 与 4.2 的"真实模型出口冒烟"——本环境无真实 provider 调用条件。
- 上述两项完成前，本 change 不得宣称目录链路端到端可用，也不得据此开启任何部署开关。
