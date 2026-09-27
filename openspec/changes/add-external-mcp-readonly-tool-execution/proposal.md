## Why

现场问题：给智能体分配了 MCP 连接（`tax-health-check-test15` ← `weknora-rsmagent`）后，智能体既看不到也调不到该连接的工具。

根因分三层。「分配状态与执行开放状态分开表达」已由 `add-external-connection-agent-assignment` 补齐；「只读工具没有可走的动作」与「发现工具的调用参数从未被封装」由本 change 处理。本 change 最初给只读调用加了一条**逐工具声明**（`read_only_tools`），它要求操作者手写远端工具名才能放行任何一个工具。实测确认该前置是纯负担：操作者要么不知道远端发布了哪些名字，要么只想要「这个连接的工具都能用」，于是声明栏留空、工具照旧不投放，而留空的本意（不投放）又被读成「出问题了」。逐工具声明在动作模型里也不成立——`tools.read` 只认名单内的名字，名单为空时它没有任何可投放的候选。

本 change 因此改为：**去掉逐工具声明，MCP 连接的每个发现工具一律以读动作 `tools.read` 投放**。放行依据是部署的 `read_execute` 就绪开关与按连接的分配，不再需要一份由操作者维护的名单。

代价必须写清楚，而不是留给读者推断：去掉声明后，本 build 里 `tools.read` 与 `tools.call` 对发现工具的效果相同，差别只剩风险等级与审批，而 MCP 的 `write_execute` 本就不可开放。也就是说，**远端服务器发布什么工具，什么工具就能被执行**，本地不再有「这个工具有没有副作用」的闸门；剩下的边界只有连接分配与 `read_execute` 开关。风险目录 `(mcp, tools.read)` 的注释与本文档同步改写，不再声称放行依据是连接声明。

## What Changes

- MCP 增加读动作 `tools.read`，与写动作 `tools.call` 分属读/写切片：`tools.read` 落在 `read_execute`（可开放、默认关闭、不要求单动作审批），`tools.call` 仍落在 `write_execute`（MCP 不开放，保持高风险与审批要求）。
- **移除**连接级逐工具声明：`read_only_tools` 不再是 MCP 的非秘密配置键，`registry.validate_config` 对它按未知字段拒绝；发现结果不再被任何本地名单过滤。
- 发现工具一律以 `tools.read` 投放：`read_execute` 开放即可见，不再有「已声明 / 未声明」的分流。
- `tools.read` 的调用只接受**该连接当前发现结果内**的远端工具名；名字不在其中，或该连接在当前版本上还没有发现结果时，拒绝且不向远端发出任何请求。这保留了「只能调用服务器确实发布过的工具」这道免维护的边界，且不引入需要人工维护的名单。
- 修正发现工具的调用参数封装：远端工具名由绑定注入，模型参数进入 `arguments`；连接级动作保持显式 `{tool, arguments}` 语义不变。
- 风险目录增加 `(mcp, tools.read)`：只读、低风险、不要求审批；其注释如实说明本地不再有逐工具判定，风险由部署开关与连接分配承担。
- 控制台 MCP 配置表单**移除**只读工具声明字段及其校验，并确认提交的配置只含服务端接受的连接级键。
- **分配即该连接的授权**：连接已配置分配且当前 Agent 在关系内时，`may_execute` 不再要求 `external:<kind>:<kind>.<action>` 逐资源授权（只跳过授权本身；功能权限、切片、风险、审批与配额照旧）。未配置的存量连接、空分配、状态缺失、无受信 Agent 与跨租户 Agent 保持原授权要求。
- **不**开放 MCP `write_execute`，**不**改变任何就绪开关默认值。

## Capabilities

### Modified Capabilities

- `mcp-connection-integration`: 增补读动作与发现工具的一律投放，声明「连接不存在逐工具可调用声明字段」，并把 `tools.read` 的可行名字限定在该连接当前发现结果内。
- `external-system-access-console`: MCP 表单只暴露连接级配置字段，不再提供逐工具声明。

## Impact

- 适配器与动作模型：`integrations/external/adapters/mcp.py`（新动作、一律按读投放、发现结果边界、参数封装）；`integrations/external/registry.py`（确认 `read_only_tools` 不属于 MCP 非秘密配置键集合：提交它按 `unknown_field` 拒绝，字段集合与校验不再承认它）；`integrations/external/risk.py`（新增只读风险条目并改写结论口径）。
- 运行时与工具面：`integrations/external/tools.py`、`integrations/external/runtime.py`（动作放行按切片判定，复用现有授权/配额/审计，不新增旁路；两处都向 `may_execute` 传入 `connection_id`，使「分配即授权」在投放与派发上同判）；`integrations/external/authorization.py`（`may_execute` 增加「分配即该连接的授权」这一条入口）；`agent/tools/mcp/external.py`（只增一个按连接版本读取发现结果的只读访问器，供适配器做边界判定）。
- 前端：`channel/web/static/js/external-connections.js` 及 i18n、`tests/fixtures/console_i18n_snapshot.json`；卡片执行状态的呈现沿用 `add-external-connection-agent-assignment` 已落地的那一行。
- 存储：无模式变更、无数据迁移。已保存行里遗留的 `read_only_tools` 成为惰性键：`validate_config` 只在新建/更新/草案测试时运行，读与调用路径不校验，因此既不会报错也不会改变行为，并在该行下一次保存时自然消失。
- 前置：读调用仍受 MCP `read_execute` 就绪开关约束；写入路径与第三方业务动作验收不在本 change 内。`agent-runtime-capability-enforcement` 的按连接分配限制与配额、审批保持不变；既有资源执行授权在「连接已分配」时不再叠加（未分配与存量连接不变），本 change 的 spec delta 已写明该边界。
- 与在途 change 共享 `mcp.py`、`external-connections.js` 等文件，实施时保留其在途改动。
