## 背景

`ecRunTest` 成功后走 `loadExternalConnectionsView()`，即整页重拉：`status='loading'` + `cards=[]` + `ecRender()`，再串行请求 `/types` 与各 scope 的 `/catalog`。卡片在这段时间内消失，焦点落回 body。

这一步同时掩盖着一处不一致：`ecRunTest` 发往 `ecPathFor(card.scope, card.id)`（卡片自身 id），而卡片呈现的是 `effective_id`（生效行）的状态。`source='overridden'` 的卡片 `id` 是平台模板、`effective_id` 是租户覆盖行 —— 该按钮测的是模板。整页重拉让模板的新结果被忽略，所以今天的界面「恰好」是对的；一旦改成把响应写进卡片，若不显式判断，就会让卡片显示一份并非生效配置的结论。

## 当前接入点

| 位置 | 现状 |
|---|---|
| `external-connections.js:1822` `ecRunTest()` | 成功后 `loadExternalConnectionsView()`；toast 已读 `payload.test_status` |
| `external-connections.js:336` `loadExternalConnectionsView()` | 整页重拉：置 `loading`、清空 `cards`，串行两轮请求 |
| `external-connections.js:466` `ecRender()` | 同步替换面板 `innerHTML` |
| `external-connections.js:640` `ecTestStateHtml(card)` | 卡片测试状态块，含测试按钮 |
| `external-connections.js:644` | `TEST_STATUS_KEY[card.test_status]`，未知值退化为 `untested` |
| `external-connections.js:1833` | toast 读 `payload.test_status \|\| 'untested'` |
| `external-connection-handlers` `POST .../test` | 响应含 `connection_id`、`test_status`、`tested_at`（前一 change 已落地） |
| `external-connection-handlers` `POST .../test` 409 | `test_result_stale`：测试期间配置变更，结果被丢弃 |
| 其余 7 处 `loadExternalConnectionsView()` | 保存／删除／启停／租户授权／刷新／重试／身份切换 —— 各自改变了版本、凭据、启用状态或授权，整页重拉是正确选择 |

## 方案

### 1. 一个判定点

新增 `ecApplyTestResult(card, payload)`，是唯一决定「这份响应能不能写到这张卡片、写什么」的地方：

- **守卫**：仅当 `payload.connection_id === card.effective_id` 才写入。卡片呈现的是生效行，因此描述别的行的响应不得改写它。
- **写入**：`test_status` 与 `tested_at` 两个字段，键名与服务端一致，沿用 `test_summary_payload` 的取值（前端不做二次判定，未知值仍由 `TEST_STATUS_KEY` 退化为 `untested`）。
- **返回**：是否写入，供调用方决定是否需要重绘。

### 2. 成功路径

`ecRunTest` 成功时：toast（不变）→ `ecApplyTestResult` → 若写入则 `ecRender()`。不置 `loading`、不清空 `cards`，因此只有一次同步重绘，没有中间态可绘制。

### 3. 结果被丢弃

`409 test_result_stale` 时，只重读该连接（`GET .../test`）并按其结果写入。选择重读而非就地标「已过期」：后者对「此前从未测过」的卡片会误标，而「已过期」只在确有旧记录时才成立。选择重读而非整页重拉：后者同样能修好，但代价是又回到本 change 要消除的闪烁。

### 4. 身份守卫

异步回调在写入前校验当前 identity token 与本请求发出时一致；不一致则丢弃。`草稿与异步结果隔离` 已要求迟到请求不得写入新身份，就地写入是一条新的写入路径，必须自行承担这条守卫。

### 5. 焦点

重绘会销毁按钮，因此给测试按钮加稳定的定位标记 `data-ec-test-for="<card.id>"`；重绘后按该标记找回同一控件并 `focus()`。选中单一属性选择器而非复合选择器，既避开实现细节，也让前端 stub 能定位它。

## 三态与写入矩阵

| 响应 | 卡片写入 |
|---|---|
| 2xx 且 `connection_id === effective_id` | `test_status` / `tested_at` |
| 2xx 且 `connection_id !== effective_id` | 不写入；toast 仍报告被测对象 |
| 409 `test_result_stale` | 重读该连接后按其结果写入 |
| 其它非 2xx | 不写入；toast 报错（不变） |
| 回调时身份已变 | 不写入 |

## 边界与错误

- `ecRender()` 是同步的整块面板替换，因此「就地更新」指**不进入加载态**，而不是不做重绘。重绘后列表、筛选、滚动位置由同一份 `ecState` 决定，不会丢失。
- 重读单个连接的失败不得使页面进入失败态：保留卡片现状并提示。
- 卡片的分组与统计只依赖启用状态与筛选条件，测试不改动它们，因此无需重算。

## 不做的事

- 不改「测哪一行」：`overridden` 卡片上的按钮仍测平台模板行。是否改为测生效行是另一个决定，登记为残留。
- 不把其余写操作改成就地更新：它们改变版本、凭据或启用状态，响应不含卡片所需的全部字段。
- 不引入前端缓存、乐观状态或轮询。
- 不改服务端任何接口与响应形状。

## 分阶段门槛

本 change 全程只动前端消费侧，不涉及开放开关或数据迁移；测试能力的开放仍由 `external_connections.readiness.<kind>.test` 与 `registry.open_classes()` 决定。

## 验证

- `tests/test_external_connections_frontend.cjs`：测试后不出现加载态、不再发 `/types`、卡片就地显示新状态与时间、非生效行响应不改写卡片、409 只重读单个连接并更新、重绘后焦点回到同一测试控件、身份已变的响应不写入。
- 真实运行面：在本机控制台对 `weknora-rsmagent` 点击「测试连接」，卡片不消失，时间就地更新，`performance.timeOrigin` 不变（证明未发生文档级导航）。
