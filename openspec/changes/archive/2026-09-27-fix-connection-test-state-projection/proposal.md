## Why

`external-system-access-console` 要求连接卡片「展示类型、名称、有效范围、配置来源、启停状态、**最近测试结果与时间**」，并要求「编辑参数后旧测试 SHALL 标记已过期」；`external-connection-management` 要求已保存连接的测试结果绑定配置版本与秘密版本、过期结果不得更新当前健康状态。

已保存测试的结果确实被记录（`external_connection_tests`），判定处也已存在并被声明为唯一 —— `integrations/external/runtime.py:936` 的 `test_summary_payload()` 写着 “One place decides what "the test state" is, so the catalogue, the detail view and the page cannot disagree”。但控制台实际读取的三条路径里没有一条拿到它：

1. `ExternalConnectionService._row_to_card()`（目录卡片，`integrations/external/service.py:159`）把 `test_status` 硬编码为 `"untested"`、`tested_at` 硬编码为 `None`。
2. `_detail()`（连接详情／编辑器，同文件 `:214`）同样硬编码。
3. `POST .../test` 的响应只含适配器的 `outcome`／`stages`／`metadata`，没有 `test_status`；前端 `channel/web/static/js/external-connections.js:1833` 读 `payload.test_status || 'untested'`，于是**一次成功的测试也会弹「未测试」**。

唯一正确的入口 `ExternalConnectionService.test_state()`（`GET .../test`）没有前端消费者。后果：任何已保存连接的卡片与详情永远显示「未测试／尚未测试」，即使记录中存着一次 `result=ok`。

本机真实复现（weknora-rsmagent，`conn_cN-lhbMpQlwpiL7k`，config_version 2，header 秘密版本一致，记录 `ect_33bb14a792da4907bed7c0b3` 为 `result=ok`）：

| 接口 | 返回 |
|---|---|
| `GET /api/external-connections/tenant/<id>/test` | `{"status":"ok","ran_at":1790477563,"config_version":2}` |
| `GET /api/external-connections/catalog?scope=tenant` | 同一行 `test_status:"untested", tested_at:null` |

`tests/` 中没有任何用例断言目录卡片或测试响应携带真实状态（`.cjs` fixture 直接写死 `test_status: 'untested'`），因此缺口未被拦截。

前端其实已经准备好接收它：`TEST_STATUS_KEY`（`external-connections.js:74`）同时认识 `untested`／`ok`／`partial`／`failed`／`expired`，三语 i18n 也已有 `ec_status_expired`（结果已过期）。`expired` 这一取值与 `test_summary_payload(tested_at, record)` 中至今未被使用的 `tested_at` 形参，都指向「过期」状态被设计过而未接上。

## What Changes

- 目录卡片投影（`_row_to_card` / `_cards`）与详情投影（`_detail`）SHALL 从权威判定处读取当前测试状态，不再硬编码。
- 状态按**生效行**（`effective_id`）判定：租户覆盖时取覆盖行的版本与秘密，继承平台模板时取模板行。
- 区分三态：从无记录 → `untested`；有记录但配置版本或秘密标记已不匹配 → `expired`（只带时间，**不带**旧结论／阶段／错误码／明细）；有匹配记录 → `ok`／`partial`／`failed`。
- `POST .../test`（已保存连接）的响应携带同一份状态，使测试后的提示反映真实结果。
- 批量解析：目录一页的连接一次查询取齐测试记录、一次查询取齐秘密标记，避免每卡多次查询。
- 判定规则仍只有一处：卡片键名（`test_status`／`tested_at`）由 `test_summary_payload()` 的输出映射而来，不新增第二套判定。

## Capabilities

### Modified Capabilities

- `external-connection-management`：「连接测试有边界且结果绑定版本」补读侧义务 —— 目录、详情与已保存测试响应 MUST 由同一处判定暴露版本绑定的当前状态，MUST NOT 各自固定为「未测试」；旧结论 MUST 以「已过期」而非旧结论本身呈现。

`external-system-access-console` 的卡片展示与「旧测试标记已过期」要求已存在，本 change 只实现它，不复制该责任域的规范。

## Impact

- **行为受影响**：`GET /api/external-connections/catalog`、连接详情、`POST .../test` 的响应中 `test_status`／`tested_at` 由固定值变为真实状态。
- **不受影响**：测试的执行与记录路径、`last_test()` 的严格语义（旧版本或旧秘密仍返回 `None`，`tests/test_external_test_binding.py` 的断言不变）、授权判定、目录的可见性过滤、草稿测试不落库。
- **代码面**：`integrations/external/service.py`（`_cards` / `_row_to_card` / `_detail` / `probe`）、`integrations/external/runtime.py`（`test_summary_payload`、批量读取原语）。
- **测试面**：新增 `tests/test_external_test_state_projection.py`；前端 `.cjs` 断言测试后的提示反映真实状态。
- **未覆盖（登记为残留）**：本 change 不引入按时间自动失效的「结果有效期」，`expired` 仅表示版本或秘密已不匹配；`test_summary_payload` 的 `tested_at` 形参是否移除另行处理。
