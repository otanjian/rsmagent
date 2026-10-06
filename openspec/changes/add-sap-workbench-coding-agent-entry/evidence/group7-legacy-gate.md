# 证据：第 7 组（退役自管引擎路径，保留回滚开关）

日期：2026-10-05。口径：`disable_keep`（停用自管引擎路径，代码保留在回滚开关之后）。

## 处置结论

自管引擎路径已不再被任何默认路径选中，但**代码不删除**：一个部署若需回退，改一个
环境变量即可回到旧行为，无需改代码。开关集中在一个模块里，避免"两个都算默认"的模糊状态。

| 项 | 位置 | 默认 | 回滚开关打开后 |
|---|---|---|---|
| 新绑定选择的模式 | `backend/runtime.py:open_workbench_runtime` | `iframe`（平台编码入口） | `screen`（每绑定自管引擎） |
| 旧 `screen` 绑定的处置 | 同上 | 迁移为 `iframe`，旧库与历史保留 | 不迁移，保持 `screen` |
| 引擎宿主（`server.ts` 等） | `runtime.py:WorkbenchRuntime.start` 的非 iframe 分支 | 不可达（无默认路径能选到 `screen`） | 原样启动 |
| 预热 | `backend/prewarm.py:kick` | `False`（不启进程） | 按 `SAP_WORKBENCH_PREWARM` 原逻辑 |
| UI 反向代理 / 事件改写 / 引擎桥接 | `runtime.py:register_routes` + `proxy` / `native_events` / `bridge` | 保留注册，但唯一消费者（引擎宿主）默认不启动 | 原样 |
| 场景目录读触发的预热钩子 | `backend/http.py:prewarm_if_configured` | 空实现（第 2 组已改） | 仍为空实现，钩子保留 |

开关：`backend/legacy.py` 的 `SAP_WORKBENCH_LEGACY_ENGINE`，只接受 `1/true/yes/on`
（大小写与空白不敏感）；未设置或其它值一律为关。`engine_enabled(environ=...)` 可注入
环境，便于逐值断言而不改进程环境。

## 为什么是"停用"而不是"删除"

- 旧库 `scenes/sap_workbench_runtime/<binding>/` 与旧配置保留，未删除。
- 旧历史处置沿用 `design.md`：默认不迁移，损失有界（实测 69 个子目录中约 15 个
  `opencode.db` 非空、合计数 MB）。
- 保留的适配器实现（`opencode_adapter/{server,host,native-host,credentials,context}.ts`）
  只在开关打开时才被 `runtime.py` 拉起；`native-host.ts` 的项目插件仍被第 3 组复用，
  不因退役而失效。
- 运行时若真的走到非 iframe 分支（开关打开，或开关出现之前建的旧行），
  `start()` 会写一条 `warning` 日志标明"自管引擎路径在用"，避免回滚被当成常态。

## 测试

新增 `tests/test_sap_workbench_legacy_gate.py`（**14 passed**）：

- `test_the_rollback_switch_reads_only_explicit_truthy_values`：`1/true/YES/" on "/''/0/false/no/缺省`
  逐值断言。
- `test_the_default_switch_off_has_no_legacy_name_to_fall_back_on`：缺省即关。
- `test_a_screen_binding_from_before_the_retirement_is_moved_to_the_platform_entry`：
  开关关 ⇒ 旧 `screen` 行在恢复时迁移为 `iframe`，旧行不删。
- `test_a_new_session_takes_the_platform_entry_while_the_switch_is_off`：开关关 ⇒ 新会话为 `iframe`。
- `test_the_rollback_switch_restores_the_self_managed_binding`：开关开 ⇒ 新会话为 `screen`
  且**不**被迁移（回滚要与被回滚的版本行为一致）。
- `test_the_warm_up_is_inert_while_the_engine_path_is_retired`：即使 `SAP_WORKBENCH_PREWARM=1`，
  `kick()` 在开关关时也返回 `False`，不产生进程。

既有覆盖未回退：`tests/test_sap_workbench_native_embed.py` 仍断言"恢复旧 screen 绑定会转为
iframe"；第 2 组保留的 `mountLegacyCode` 分支继续由投影里的 `display_mode` 选择。

## 全量回归（SAP 主题，Windows）

`python -m pytest tests/ -k sap_workbench`（排除两个仅在 POSIX 可收集的部署用例）：
**1314 passed / 4 skipped / 10 failed**。10 个失败均为环境性、与本次改动无关，已在改动前复现：

- `test_sap_workbench.py` 2 项：本机 screen gateway 起不来（`screen gateway startup failed`）。
- `test_sap_workbench_shutdown.py` 8 项：用例在 Windows 上对管道用 `select.select`
  （`WinError 10038`）。

## 未覆盖 / 下一步

- 未在真实部署上打开开关跑一次自管引擎回滚（需要 Bun 与 OpenCode 源码树）；开关只做了单元级与
  入口级断言。
- 端到端（第 8 组）：从卡片打开、恢复、刷新，确认无引擎启动等待。
