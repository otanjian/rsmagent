## Context

动机与现场现象见 `proposal.md`。以下是已核对的现有接入点与待实施设计。

**动作模型与切片**

- `integrations/external/adapters/mcp.py`：`ACTIONS = {tools.list, tools.read, tools.call, resources.read}`，`WRITE_ACTIONS = {tools.call}`；`_action_states()` 把 `tools.call` 恒报为 `False`，因为「a tool call is a write」而 MCP 的写类在本 build 不可开放。
- `integrations/external/registry.py`：`OPENABLE_CLASSES[KIND_MCP] = {test, read_execute}`，即 `write_execute` 不在可开放集合内；`capability_projection()` 按 `configure/test/read_execute/write_execute` 逐类投影。
- `_offered_actions(opened)` 只在 `write_execute` 开放时才追加 `tools.call`；`discovered_tools_offered()` 决定是否值得为发现花一次握手。

**投放与派发**

- `_mcp_tool_provider()` 先按 `_offered_actions()` 投放连接级绑定，再追加 `_discovered_bindings()`。
- `_discovered_bindings()` 用 `mcp_external.tool_name(action=…, connection_id=…, remote_name=…)` 命名，并把远端名与 `inputSchema` 放进 `ToolBinding.tool.metadata`。
- `_mcp_dispatcher()` 把 `params` 交给 `service.invoke_action(...)`；`_invoke_tools_call()` / `_invoke_tools_read()` 期望 `{"tool": <远端名>, "arguments": {…}}`。
- `agent/tools/external/external_tool.py#execute()` 把模型参数原样转发；`agent/tools/mcp/external.py#ExternalMcpTool` 用 metadata 里的 `inputSchema` 覆盖 `params`，但只改广告形状，不改参数封装。

**发现结果的归属**

- `agent/tools/mcp/external.py` 的 memo 以 `(tenant_id, connection_id)` 为键，且每条记录都带 `version` / `enabled` / `secret_marker`：连接行一变，记录自己在下次比较时作废（`refresh_connection`）。这是「发现结果对应当前连接版本」这一事实的唯一权威，`remote_tools_for()` 之外没有第二份缓存。
- `ExecutionContext` 携带 `tenant_id`、`connection_id`、`config_version`，但不带连接行。

**配置与风险**

- MCP 非秘密配置白名单为 `_SPECS` 里的 `config_keys`，由 `_validate_mcp()` 校验并拒绝未知键。
- `integrations/external/risk.py` 用 `(kind, action)` 索引风险条目。

**控制台**

- `channel/web/static/js/external-connections.js` 按 `TypeSpec.config_keys` 驱动 MCP 表单；卡片执行状态行（`ecExecStateHtml`）已在 `add-external-connection-agent-assignment` 落地。

## Goals / Non-Goals

**Goals:**

- 让 MCP 连接发布的每个工具都能被已分配且获准的智能体发现与调用，不再需要操作者维护任何逐工具名单。
- 把「只能调用服务器确实发布过的工具」做成一道免维护的边界，而不是靠人工声明。
- 修复发现工具的调用参数封装，使投放出来的调用真正到达远端。
- 保持写入路径原样收紧：MCP `write_execute` 不开放，`tools.call` 语义、风险等级与审批要求不变。
- 不改动既有授权、配额、审计、审批与按连接分配限制的语义。

**Non-Goals:**

- 不开放 MCP `write_execute`，不新增每工具级审批或每工具级授权对象。
- 不采信远端 `readOnlyHint` 做任何判定，也不做「按工具名启发式推断副作用」。
- 不引入远端工具目录同步、内容级授权、文档级检索参数约定或结果缓存。
- 不做数据迁移，不改变连接级配置之外的任何配置语义。

## Decisions

### D1. 读动作 `tools.read` 与写动作 `tools.call` 分别归档

风险目录与运行时的放行判断都以 `(kind, action)` 为键（`RISK_CATALOGUE`、`_offered_actions`、开放类判定）。若让同一个动作按「本次调用的工具是否有副作用」改变风险等级与所属切片，就等于把 `(kind, action)` 这个稳定键拆成 `(kind, action, 参数)`——授权、审计、审批与配额都要跟着变。

因此读与写继续分开：

| 动作 | 切片 | 风险 | 审批 | 可开放性 |
| --- | --- | --- | --- | --- |
| `tools.read` | `read_execute` | 只读、低 | 不要求 | MCP 可开放（默认关闭） |
| `tools.call` | `write_execute` | 高 | 要求 | MCP 不开放 |

### D2. 不再有逐工具声明，发现工具一律按读动作投放

**本节是对本 change 早期设计的反转。** 早期设计新增非秘密配置 `read_only_tools`，由连接管理员显式声明哪些远端工具是只读的，未声明的名字在适配器内拒绝；当时的理由是「远端服务器既是自述者也是受益者，`readOnlyHint` 不能作为授权」。

现场使用证明该理由不成立为一个可运维的要求：操作者无法从控制台得知远端发布了哪些名字，只能手写，写错即 fail-closed 且无提示；而大多数操作者的真实意图就是「这个连接的工具都能用」。用一份必须人工维护的名单去表达「全都可以用」，得到的是留空、工具不投放、再回头排查。

新立场：

- MCP 连接的字段集合里**不存在**逐工具可调用声明；发现结果不被任何本地名单过滤，发布什么就投放什么。
- 放行由**部署的 `read_execute` 就绪开关**与**按连接的分配/资源执行授权**承担，两者都是既有机制，本 change 不新增第三处闸门，也不减少前两处。

**明确记录的代价**：本 build 里 `tools.read` 与 `tools.call` 对发现工具的效果相同，差别只剩风险等级与审批，而 `write_execute` 本不可开放。因此远端工具的真实副作用不再有本地判定——远端服务器发布什么，什么就能被执行。这是把授权面交给连接的远端这一决策的直接后果，风险目录注释与 `proposal.md` 同步写明，不留「仍然安全」的暗示。

被拒的替代方案：

1. **保留名单但默认全放行**（`read_only_tools: true`）——比去掉更糟：既留字段又留歧义。
2. **按工具名启发式判定只读**（`get_*` / `search_*` / `list_*`）——名字是远端自己起的，启发式只会制造「看起来安全」的错觉。
3. **开放 MCP `write_execute`，全部按写动作投放**——会要求每个发现工具调用都走审批，等于把本 change 要解决的「工具不可用」换成「工具可用但要逐次审批」，且 `write_execute` 的开放本身另有验收门槛，不在本 change 范围。

### D3. `tools.read` 的可行名字限定在该连接当前发现结果内

去掉了人工名单之后，唯一还值得保留的边界是「服务器确实发布过这个名字」。判定读 `agent/tools/mcp/external.py` 的 memo：memo 的每条记录本身就带连接 `version`，因此「该连接在当前版本上的发现结果」是现成事实，不需要新缓存、不需要每次调用都握手。

- 命中 → 转发；未命中，或该连接在当前版本上没有发现记录（发现尚在进行、失败、或连接刚变更）→ **拒绝**，错误码 `tool_not_published`，且**不建立到远端的连接**（与原先的拒绝语义一致：这是关于我们已知事实的判断，问服务器反而会把服务器的答复变成依据）。
- 这是 fail-closed 的：发现结果未知时不放行。代价是「发现还没跑完就调用」会被拒——但调用方本来就看不到工具名，能调用的前提是发现已经给出过名字。
- memo 只读一次，且需要在适配器里访问它。适配器已在 `_mcp_tool_provider()` / `_discovered_bindings()` 中使用同一 memo，因此这是既有依赖的延伸，不是新的分层。

被拒的替代方案：不做任何名字判定，只保留 `{tool, arguments}` 形状校验。那样模型可以任填名字；远端是否认识由远端答复，本地的最后一道「服务器发布过」事实就没有了。

### D4. 发现工具的远端名由绑定注入，模型只提供该工具自己的参数

发现绑定已经在**工具名**里携带远端名，因此派发时不必也不该再要求模型填：`_mcp_dispatcher()` 在绑定 metadata 带 `remote_tool` 时，把模型参数封装为 `{tool: <远端名>, arguments: <模型参数>}`。连接级绑定（调用方自己给出 `{tool, arguments}`）保持原样，不做二次封装。

| 绑定 | 传给远端的参数 |
| --- | --- |
| 发现工具 `mcp.tools.read.<连接>.<远端>` | `{tool: <远端>, arguments: <模型参数>}`，远端名由绑定注入 |
| 连接级 `mcp.tools.read.<连接>` / `mcp.tools.call.<连接>` | 原样透传，调用方显式给出 `{tool, arguments}` |

不采用「两者都接受」的宽松判定：模型参数里若正好有名为 `tool` 的字段（远端 schema 合法字段），宽松判定会让它被解释成远端工具名——一次参数注入。

### D5. 投放分流与失效

`_discovered_bindings()` 不再分流：每个候选都以 `tools.read` 绑定（`write=False`），绑定名与 metadata 沿用既有形状。`tools.call` 只作为**连接级**动作存在，且仅在 `write_execute` 开放时投放——MCP 当前不在可开放集合内，故实际不会投放。

发现的门槛由「读切片的 `tools.read` 是否可投放」决定：

- `read_execute` 开放 → 为每个连接投放其发现工具（本 change 的目标状态）；
- `read_execute` 关闭 → 不投放、也不花握手，工具面维持「执行未开放」的既有呈现。

上界沿用既有常量：单连接最多 `MAX_TOOLS_PER_CONNECTION` 个候选、远端名不超过 `MAX_REMOTE_NAME`。

失效沿用既有机制，不新增缓存寿命：memo 以连接行（version、enabled、凭据标记）为键，连接变更必然重新发现；D3 的边界判定读的就是这份 memo，因此声明层面的失效问题不再存在。

### D6. 控制台字段与兼容

- MCP 表单移除只读工具声明字段及其客户端校验；i18n 三语与 `tests/fixtures/console_i18n_snapshot.json` 同步删除对应键。表单提交的 `config` 只含服务端接受的连接级键。
- 存量行为：已保存行里遗留的 `read_only_tools` 是惰性键。`registry.validate_config` 只在新建/更新/草案测试时运行（读与调用路径不校验），因此既有行不会因它报 `unknown_field`，也不改变行为；该行下一次保存时字段自然消失。不做数据迁移。
- 平台模板与租户覆盖不受影响：本字段既不参与继承也不再被写入；覆盖行使用自己的完整有效配置。
- 卡片上的执行状态行由 `add-external-connection-agent-assignment` 提供，本 change 只让「读」这一半真的能开。

### D7. 门槛与顺序

本 change 只打开**代码路径**，不打开部署切片。实际可用仍要求该部署在就绪配置里开放 MCP `read_execute`（`registry.open_classes(KIND_MCP)`），并完成 MCP 测试环境验收；未经该验收的部署看到的仍是「工具执行未开放」及原因。stdio 连接另受既有本地进程隔离门槛约束，本 change 不放宽。

### D8. 分配即该连接的授权

连接已配置分配（`configured=1`）且当前 Agent 在关系内时，**分配就是这份连接的授权**：`may_execute()` 不再要求 `external:<kind>:<kind>.<action>` 逐资源授权。按连接分配表达的是「这个智能体可以用这条连接」，而这条连接是租户自己的资源——与「个人邮箱凭所有权使用」（`may_execute` 的第 0 条入口）同源。

**为什么改**：现场「已经分配了 MCP，为什么看不到工具」的最后一处阻塞不在本 change 的字段上。连接 `conn_cN-lhbMpQlwpiL7k` 已分配给 `tax-health-check-test15`、12 个远端工具已发现、`read_execute` 已开放，但 `authorized_tools()` 仍返回空：租户 `test15` 的 `tenant_resource_grants` 里没有 `external:mcp:mcp.tools.read`。也就是说，分配了连接的人还要再向平台要一行逐资源授权行——这与「连接是租户自己配置的」这一事实相矛盾。

**边界（比放宽更重要）**：

- **只替代逐资源授权**。功能权限（`tool.execute`）仍在 `_assignment_authorizes()` 内单独要求；把分配当成「可以执行任何工具」，等于让外部能力成为唯一一种不需要功能权限的资源。连接启用状态、执行切片、风险等级、审批绑定与配额都在 `may_execute` 之外逐次判定，本次不改变它们的顺序。
- **只对明确分配成立**（`REASON_ASSIGNED`），而不是 `assignment_allows()` 的 `allowed`。后者对 `configured=0`（存量连接沿用原权限）返回 allowed，因为**分配闸门**本就不该收窄存量连接；若照抄 used，会把逐资源授权要求对所有存量连接一并丢掉——与兼容规则的目的相反。
- **空分配是「禁止所有智能体」**，配置存在但无人被分配时没有豁免；分配状态缺失与查询失败同样回落逐资源授权（fail-closed）。无受信 Agent 上下文、跨租户 Agent 都不成立。
- **判定位置不变**：`may_execute()` 仍是投放（`authorized_tools()`）与派发（`ConnectionRuntime.invoke()`）共用的同一条判定，因此不会出现「能调但看不到」。两处都传入 `connection_id`（新增参数），否则这一条入口在投放路径上无从生效。

**替代方案与否决理由**：

- 让平台管理员给租户补一行 `tenant_resource_grants` 记录 → 否决。它把「租户自己配置并分配给自己的智能体」变成需要平台方介入的运维动作，且每次新建连接都要重复一次。
- 删除 `may_execute()` 里的资源授权检查 → 否决。租户/平台连接仍有需要逐资源授权的场景（租户未分配给任何智能体、平台模板由平台统一分配），整条闸门不能拆。

### D9. 模型面工具名是一条线路契约，不是拼写习惯

工具名以 `^[a-zA-Z0-9_-]{1,128}$` 为契约：字符集与长度都是提供方的硬约束，越界时被拒绝的是**整次请求**，不是那个工具。现场报错 `Invalid 'tools[18].function.name': string does not match pattern '^[a-zA-Z0-9_-]+$'` 只给一个下标，不告诉你是谁的名字有问题——模型连一句「你好」都答不出来。

**为什么改**：名字由三段组成，其中两段不是我们能约束的——动作标识（`tools.read`，自带点号）、连接标识（`conn_<token>`）、以及**第三方服务器发布的远端工具名**。旧分隔符 `.` 与动作标识里的 `.` 一起进入名字，于是每个外部工具都不可发送。这不是「某个名字不好看」，而是「这个名字根本发不出去」。

**做法**：

- 分隔符改为 `_`，并且不再把它当作可解析的定界符：没有任何代码把名字反解成部件（`find_binding` 比整名，远端名走绑定 metadata）。可读性由分隔符承担，唯一性由规范化+摘要承担。
- 每一段经 `_wire_part()` 规范化（契约外字符 → `_`）。规范化和长度截断都会丢信息，因此**只在丢信息时**追加原始远端名的 SHA-1 前 12 位。名字于是只由输入决定：`tools.read` 与 `tools-read` 不共用名字，且结果与服务器的列表顺序无关——派发按绑定携带的远端名执行，一个可能指向另一个工具的名字会调用到另一个工具。
- 长度预算按**模型可见**的名字算（含 `external_` 前缀），而不是按裸绑定名。上界组合（22 字符连接标识 + 128 字符远端名）此前必然超过 128 —— 只修字符集会撞上第二个 400。
- 无法在契约内命名时返回空，调用方跳过并记录：一个名字不成立的工具不该让整轮对话失败。
- 纵深防御放在 `build_tools_schema()`（真正发出去的那一层）：不合规的名字使该工具不投放并记录 error。今天由组合保证契约成立，这一层是为了让下一个改错名字的人只损失一个工具。

**替代方案与否决理由**：

- 只改分隔符 → 否决。动作标识里的 `.` 仍在，报错照旧。
- 只把非法字符替换成 `_` → 否决。替换会合并不同的远端工具（`tools.read` 与 `tools-read` 同名），模型无法分别寻址，而两个名字背后是两个不同的远端调用。
- 保留点号名作为内部身份、另造一套模型面名字 → 否决。两套名字会漂移，而漂移的表现正是「能调但看不到」这一类缺陷；名字发不出去就应该在源头改成能发出去的名字。
- 在提供方适配器（`models/deepseek/*`）里转义 → 否决。契约是模型面的，不是某一个厂商适配器的；放在最后一道边界之前，所有厂商都受同一份保证，且名字在日志、审计、工具列表里也是同一个。

## Risks / Trade-offs

- **已分配的连接不再需要逐资源授权行** → 这是 D8 的直接后果。留在本地的手段是连接启用状态、`read_execute` 开关、风险与审批、逐次配额，以及「分配关系本身」；不再声称存在一层按角色的外部资源授权。撤销方式是把该 Agent 从连接的关系里移除（下一回合即生效，判定不缓存）。
- **远端发布的写工具会被当作只读调用执行** → 这是 D2 决策的直接后果，已在风险目录注释、`proposal.md` 与本节写明。留在本地的手段只有连接分配、资源执行授权与 `read_execute` 开关；不再声称存在逐工具判定。
- **发现结果未知时拒绝调用** → D3 的 fail-closed 代价：发现尚未完成、发现失败或连接刚变更时，调用被拒而不是被放行。这是刻意的取舍；被拒的错误文案需要说明是「发现结果不可用」而不是「工具不存在」，以免运维误判。
- **新增动作需要下游认识它** → 授权、配额、审批按 `(kind, action)` 通用处理；风险目录缺条目时既有实现按 fail-closed 分支处理。
- **既有测试大量以发现绑定为 `tools.call` 的载体** → 本 change 需要把这些用例迁移到 `tools.read`，并把「MCP 工具调用必须审批」的契约继续由**连接级** `tools.call` 覆盖，而不是删掉该覆盖。

## Migration Plan

1. 动作、字段移除与风险条目先落地；确认存量行（含遗留 `read_only_tools`）仍可读写与调用，行为不变（除发现工具不再投放，因为此前也没有可投放的候选）。
2. 修正发现工具的参数封装，用既有测试证明连接级动作语义未变。
3. 再把发现工具按读动作投放，并加上 D3 的发现结果边界（仍受 `read_execute` 门槛）。
4. 最后移除控制台字段、i18n 与卡片/表单证据。

## Open Questions

- 是否保留一个「连接级开关」表达「本连接的工具全部可用」？→ 已定：**不保留**。开关与「去掉声明」表达同一件事，却多一个会被误读为「部分放行」的字段；可用性由 `read_execute` 就绪开关与分配表达。
- 是否把 MCP 加入 `write_execute` 可开放集合？→ 已定：**不加入**。那会把全部发现调用变成逐次审批，与本 change 的目标相反，且写类开放另有验收门槛。
- `tools.read` 的名字边界用「声明名单」还是「发现结果」？→ 已定：**发现结果**（D3）。名单需要人工维护且与「发布什么就能用什么」冲突；发现结果是现成事实。
- 「发现结果未知」是否有独立错误码？→ 已定：与「名字不在发现结果内」共用 `tool_not_published`，文案区分两种情形，避免下游多一个需要认识的状态。

## 实施差异记录（与本文其余部分的唯一出入）

- D6 写作「该行下一次保存时字段自然消失」：实施不做任何主动清理。若运维希望立刻清掉遗留键，正常编辑并保存该连接即可；本 change 不提供迁移脚本。
- 归档证据 `evidence-5/browser/` 中拍摄的只读工具声明字段已随本 change 移除，相关截图按新表单重拍；`evidence-6/live-deployment-diagnosis.md` 中「`read_only_tools` 声明」一项改用「不存在该字段」核对。
