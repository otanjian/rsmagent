## 背景

`test_summary_payload()` 自称是「测试状态」的唯一判定处，但控制台读取的两条投影路径不经过它：目录卡片 `_row_to_card()` 与详情 `_detail()` 把 `test_status` 写成常量 `"untested"`，`POST .../test` 的响应则完全没有这个字段。于是「已保存测试结果」在记录里存在、在接口上取不到。

改动面小，但错误方向有代价：把目录做成「前端逐卡补拉 `GET .../test`」也能让徽标正确，却会引入每页 N 次请求、首屏状态闪烁，并且治不了 `POST` 提示那条（后端仍缺字段）；更糟的是它把「谁说了算」从服务端挪到浏览器，与 `test_summary_payload` 的既有声明相反。因此选择在服务端把三条读路径统一接到既有判定处。

## 当前接入点

| 位置 | 现状 |
|---|---|
| `integrations/external/service.py:143` `_row_to_card()` | 目录卡片；`test_status` / `tested_at` 硬编码 |
| `integrations/external/service.py:186` `_detail()` | 详情与编辑器回显；同样硬编码 |
| `integrations/external/service.py:401` `_cards()` | 批量装配卡片，已在此解析 `effective_id`（继承/覆盖语义） |
| `integrations/external/service.py:355` `catalog()` | 目录查询，`LIMIT/OFFSET` 有界 |
| `integrations/external/service.py:1484` `test_state()` | 唯一正确入口，只被 `GET .../test` 使用 |
| `integrations/external/runtime.py:727` `last_test()` | 严格：版本或秘密不匹配即返回 `None` |
| `integrations/external/runtime.py:325` `secret_versions()` | 逐连接的秘密标记（SHA-256，跨进程稳定） |
| `integrations/external/runtime.py:936` `test_summary_payload()` | 状态形状的唯一判定处 |
| `integrations/external/runtime.py:510` `probe()` | 已保存测试的记录与响应装配 |
| `channel/web/static/js/external-connections.js:640` / `:1823` | 卡片徽标与测试后提示的消费者 |

## 方案

### 1. 判定仍只有一处

`test_summary_payload(tested_at, record)` 继续决定「测试状态」的形状与取值，并补上过期分支：`record` 为空但存在**更旧的记录**时返回 `expired`，且只带时间，不带 `stage` / `code` / `detail`。旧结论本身 MUST NOT 出现在过期状态里 —— 这正是 `last_test()` 拒绝返回陈旧记录所要保护的。

卡片键名（`test_status` / `tested_at`）由该输出映射：`status` → `test_status`，`ran_at` → `tested_at`。映射只做改名，不重新判定，避免出现第二套规则。

### 2. 批量读取

目录一页的连接需要三样东西：每行的当前配置版本（行上已有）、每行当前的秘密标记、每行的最新记录。前两样各以一次 `IN (...)` 查询取齐；第三样在 `runtime` 内新增批量原语，把「最新且匹配」这条规则实现一次，并让 `last_test()` 委托它（单连接即单元素），避免两处规则漂移。

秘密标记的批量版本抽出纯函数（已取到的 `secret_refs` 行 → `connection_id → {slot: marker}`），`secret_versions()` 委托它，保持同一套 SHA-256 规则。

批量读取保留 `last_test()` 原有的「只看最新 5 条」窗口，并把窗口放进 SQL（`ROW_NUMBER() OVER (PARTITION BY connection_id ...)` 后取 `rank <= 5`）：窗口一旦去掉，读者会一直往回翻到更旧的记录，把已经作废的结果重新报成 `ok`；窗口只放在 Python 侧，则一次读取的行数仍随该连接的测试次数增长，正是 `LIMIT 5` 原本要避免的。窗口取 5 与单连接读者一致，两处不多不少地表达同一件事。

### 3. 生效行

状态按 `effective_id` 判定：租户覆盖平台模板时取覆盖行，继承模板时取模板行（其秘密也在模板行上）。`_cards()` 已经在算 `effective_id`，批量读取以它为键，不另建一套继承解析。

### 4. 三态语义

| 情形 | 状态 | 时间 | 是否携带旧结论 |
|---|---|---|---|
| 无任何记录 | `untested` | — | — |
| 有记录，版本或秘密已不匹配 | `expired` | 该记录时间 | 否 |
| 有匹配记录 | `ok` / `partial` / `failed` | 该记录时间 | 是 |

「草稿测试后首次保存」落在第一行（草稿本就不落库），与既有口径一致。

## 边界与错误

- `last_test()` 的严格语义不变：过期记录仍不返回，`tests/test_external_test_binding.py` 的既有断言（编辑或轮换后 `last_test() is None`）继续成立。
- 过期状态 MUST NOT 让调用方从卡片上读回旧的阶段、错误码或明细；`expired` 只说明「曾经测过、现在不算数」。
- 批量读取失败时按空集合处理（卡片退化为 `untested`），不使整个目录请求失败：目录可用性优先于徽标准确性。此点以测试固定。
- 平台范围目录（无租户上下文）沿用同一路径，`tenant_id=None`。

## 不做的事

- 不引入按时间自动失效的「结果有效期」；`expired` 只表示版本或秘密不匹配。
- 不新增第二个测试状态接口，也不改 `GET .../test` 的既有响应形状。
- 不改 `test_summary_payload` 的 `tested_at` 形参（当前未被使用）；是否移除另行处理，避免与本次修复混在一起。
- 不顺手调整前端 `TEST_STATUS_KEY`／i18n：它们已经认识 `expired`，本 change 只需让服务端真的送出它。

## 分阶段门槛

本 change 不涉及开放开关：测试能力的开放仍由 `external_connections.readiness.<kind>.test` 与 `registry.open_classes()` 决定，状态投影只呈现已记录的事实，不改变任何能力是否可用。

## 验证

- `tests/test_external_test_state_projection.py`：目录卡片/详情反映已记录结果、与 `GET .../test` 一致、编辑与轮换后转 `expired` 且不带旧结论、草稿测试后首次保存为 `untested`、继承与覆盖按生效行判定、`POST .../test` 响应携带状态、5 条窗口由 SQL 限制。
- 前端 `.cjs`：测试后提示呈现真实状态而非固定「未测试」。
- 真实运行面：本机 `weknora-rsmagent` 连接测试通过后，目录卡片与详情显示 `ok` 与时间。
