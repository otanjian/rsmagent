# 卡片上的「执行开放状态」（分配成功但业务执行关闭）

现场问题：给「税务健康体检」（`tax-health-check-test15`）分配了 MCP 连接后，智能体看不到该连接的工具。

排查结论：**分配确实成功、连接也确实可用**，卡片的说法没有骗人；骗人的是它**没说**的那件事——工具执行切片按部署策略仍未开放（`registry.open_classes('mcp')` 默认只有 `configure` + `test`，`read_execute` / `write_execute` 关闭），所以派发前的执行条件把工具全部拦下。卡片上「已配置 · 1 个智能体可用」紧挨着绿色的「连接正常」，读者只能读成「工具可以用」。

这不是新增需求：本 change 的 spec 已经写明

> 入口和摘要 MUST 由服务端投影，**分配状态与连接测试健康、启停和执行开放状态分别表达**。
> **Scenario: 分配成功但业务执行关闭** —— 分别呈现分配状态及原执行不可用原因，不把分配或连接测试成功表示为可执行。

只是实现漏了这一段：`ecTestStateHtml` 只读 `cap.test_available`，整份前端代码没有任何一处读执行相关字段。本文件记录补齐它的根因、修法与证据。

## 1. 根因

`integrations/external/registry.py#capability_projection` 逐类投影四个 class（`configure` / `test` / `read_execute` / `write_execute`），每类给 `{available, reason}`，并另给 `test_available`、`execute_available`、`read_execute_available`、`write_execute_available` 与 `unavailable_reason`。

前端只用到了 `test_available`：

| 卡片上的事实 | 由谁承载 | 原先有没有渲染 |
| --- | --- | --- |
| 分配状态 | `card.agent_assignment` | ✅ `ecAgentSummaryHtml` |
| 连接测试健康 | `card.test_status` + `cap.test_available` | ✅ `ecTestStateHtml` |
| 启停 | `card.enabled` | ✅ 徽标 |
| **执行开放状态** | `cap.classes.{read,write}_execute` | ❌ 一处都没有读 |

于是「已分配 + 连接正常」与「工具能被派发」在卡片上合成了一句话，而后者在 MCP 上是假的。

## 2. 修法（`channel/web/static/js/external-connections.js`）

新增 `ecExecStateHtml(card)`，插在分配摘要与连接测试行**之间**，作为独立一行 `<p class="ec-card-exec" data-ec-exec-state>`：

| 投影状态 | 卡片文字 |
| --- | --- |
| `read_execute` 与 `write_execute` 都关闭 | 工具执行未开放 ＋ 原因 |
| 只开 `read_execute` | 工具执行仅开放读取 ＋ 另一半的原因 |
| 只开 `write_execute` | 工具执行仅开放写入 ＋ 另一半的原因 |
| 两者都开 | 工具执行读写均已开放（无原因） |
| 该 kind 没有 `classes` 投影 | 不渲染这一行 |

三个判断上刻意收紧的地方：

- **不看 `unavailable_reason`**。它是 `test → read_execute → write_execute` 里**第一个关闭**者的原因，在「测试关闭、读取开放」的部署上它讲的是测试类，与执行无关。执行状态只能由 `classes` 逐类推出，所以四类可用性各自成句。
- **没有投影就不说话**。`classes` 缺失时不猜「执行开放」——猜正是这条 requirement 要禁止的行为。
- **原因句只讲原因**。原先 `ec_reason_awaiting_*` 的文案是「…验收证据，**测试保持关闭**。」，一旦被执行类复用就自相矛盾：MCP 这张卡上测试类是**开放**的（测试按钮可点），句子却说测试关闭。改为只留因果「尚未取得 MCP 测试环境的验收证据。」，关闭的是哪一类由紧邻的标签负责。这条改动同时落到 `tests/fixtures/console_i18n_snapshot.json`。

## 3. 证据

### 3.1 单元（`tests/test_external_connections_frontend.cjs`，43 pass）

新增三条用例，`catalogueApp` 增加 `type` 参数以便逐场景给出不同的服务端投影：

- `an assigned connection whose execution is closed says so beside the assignment`：同一张卡上「已配置 · 2 个智能体可用」与「工具执行未开放」并存，原因取服务端投影原句；并断言该原因句**不含**「测试保持关闭」（这张卡上测试是开放的）。
- `only the classes a deployment really opened are reported open`：读取仅开 / 写入仅开 / 读写全开三种投影，各自只出现自己的标签，另外三种标签一个都不出现。
- `a card with no type projection claims nothing about execution`：投影缺失时四种标签全不出现（先断言摘要仍在，避免「空白即通过」）。

RED 记录（实现前，同样的用例）：

```
✖ an assigned connection whose execution is closed says so beside the assignment
  AssertionError: the closed-execution label has a translation
✖ an execution class open for reads only is not reported as full execution
  AssertionError: the read-only label has a translation
✖ an execution class open for reads and writes is reported as both
  AssertionError: the read-and-write label has a translation
```

把卡片接线（`+ ecExecStateHtml(card)`）临时去掉后复跑，失败点落在真正的根因上：

```
✖ an assigned connection whose execution is closed says so beside the assignment
  AssertionError: and the closed execution is stated as its own fact
✖ only the classes a deployment really opened are reported open
  AssertionError: reads only: the card states it
```

### 3.2 真实浏览器（`tests/test_external_connections_browser.cjs`，13 scenarios / 0 failed）

新增场景 `分配成功但业务执行关闭时，卡片分别呈现两件事`，用**真实静态资源与真实 CSS**、真实服务端投影形状（`classes` 四类齐全、`test` 开、执行全关）跑一次：

- 同一张卡上同时出现 `已配置 · 2 个智能体可用`、`连接正常`、`工具执行未开放`，三者各自成行；
- 原因句等于服务端投影（`尚未取得 MCP 测试环境的验收证据。`）；
- **测试按钮是启用的**（`test_available: true`）——这条证明这张卡上「测试保持关闭」会是假话，也证明两类状态确实分开；
- 「工具执行未开放」不出现在 `.ec-test-state` 行内（分别表达，不是挂在测试行上的装饰）；
- 把读取类临时打开后重载，该行改为「工具执行仅开放读取」，原因换成**未开放那一半**（写入类）的原因，且不借用已开放那一半的句子。

截图：`browser/execution-closed-on-card.png`（对应现场那张「已配置 + 连接正常」的卡片）。

接线去掉后的 RED（浏览器层）：

```
locator.waitFor: Timeout 15000ms exceeded.
  - waiting for locator('[data-ec-card]').first().locator('[data-ec-exec-state]') to be visible
```

—— 卡片整行都不存在，正是现场看到的样子。

## 4. 复核

```
node --test tests/test_external_connections_frontend.cjs   → 43 pass / 0 fail
node --test tests/test_console_i18n_parity.cjs             → 6 pass / 0 fail
COW_EXTERNAL_BROWSER_OUTPUT=... openspec/changes/.../evidence-5/browser \
  NODE_PATH=$(npm root -g) node tests/test_external_connections_browser.cjs
                                                          → 13 scenarios, 0 failed
.venv/bin/python -m pytest tests/test_external_connections_menu.py \
  tests/test_external_connection_agent_assignment.py \
  tests/test_external_connection_assignment_runtime.py \
  tests/test_external_test_state_projection.py \
  tests/test_external_authorization.py -q -p no:randomly  → 84 passed
```

`test_console_i18n_parity.cjs` 在本轮一并转绿：分配功能新增的 47 个 key 之前只写进了 i18n 文件、没有回填 `tests/fixtures/console_i18n_snapshot.json`（该文件是「拆分前后逐字相同」的基准），本轮把 47 个 key 按语言补齐并更新 5 条原因文案，`deep-equal` 重新成立。

相关回归见 `regression.md` 第 5c 节。

## 5. 本文件**不**改变的事

执行切片是否开放仍由部署就绪开关决定，本 change 不动它：MCP `read_execute` 依然默认关闭，工具依然不会被派发。本轮只补齐「卡片把这件事说出来」。真正的读取型工具执行路径（含 `tools.call` 的参数封装与只读工具声明）属于另一件事，另行提案。
