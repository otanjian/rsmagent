# Tasks: 让已保存的测试结果真正到达目录、详情与测试响应

> 依据：`openspec/specs/external-system-access-console` 要求卡片展示最近测试结果与时间、编辑后旧测试标记已过期；`openspec/specs/external-connection-management` 要求结果绑定配置与秘密版本。
> 需求侧 delta 见 `specs/external-connection-management/spec.md`。判定处仍只有 `test_summary_payload()` 一处。

## 1. 回归测试（RED）

- [x] 1.1 新增 `tests/test_external_test_state_projection.py`：已保存连接测试通过后，目录卡片（`catalog`）的 `test_status` 为 `ok` 且 `tested_at` 等于记录时间；当前实现下必失败
- [x] 1.2 同文件：连接详情（`get_connection`）与 `GET .../test`（`test_state`）对同一连接给出一致状态
- [x] 1.3 同文件：编辑连接后卡片转 `expired`，且过期状态**不含**旧的 `stage`／`code`／`detail`；凭据轮换后同样转 `expired`
- [x] 1.4 同文件：从无记录（含「草稿测试后首次保存」）仍为 `untested`
- [x] 1.5 同文件：继承平台 MCP 模板与租户覆盖两种情形下，状态按 `effective_id` 判定，不读非生效行的记录
- [x] 1.6 同文件：目录一页多张卡片时，测试记录与秘密标记各以一次 `IN (...)` 查询取齐（用计数 store 断言查询次数不随卡片数线性增长）
- [x] 1.7 前端 `tests/test_external_connections_frontend.cjs`：测试成功后提示呈现真实状态，不再固定「未测试」

## 2. 实现（GREEN）

- [x] 2.1 `integrations/external/runtime.py`：抽出秘密标记的纯函数（`secret_refs` 行 → `connection_id → {slot: marker}`），`secret_versions()` 委托之；新增批量版本，供目录一次查询取齐
- [x] 2.2 同文件：新增「最新且匹配」的批量读取原语（按 `(connection_id, config_version)` 与当前秘密标记匹配），`last_test()` 委托它，保持其严格语义不变
- [x] 2.3 同文件：`test_summary_payload()` 补过期分支 —— 记录为空而存在更旧记录时返回 `expired`，只带时间，不带 `stage`／`code`／`detail`
- [x] 2.4 `integrations/external/service.py`：`_row_to_card()` / `_detail()` 不再硬编码，改为接收已判定的状态；`_cards()` 与详情路径批量解析后注入，键名映射（`status`→`test_status`、`ran_at`→`tested_at`）收在一处
- [x] 2.5 同文件：`probe()` 在已保存连接成功记录后，把同一份状态放进响应（`test_status`／`tested_at`）；草稿与未记录路径不添加
- [x] 2.6 批量解析失败时退化为 `untested` 而不使目录请求失败

## 3. 验证

- [x] 3.1 新增测试文件全通过；`tests/test_external_test_binding.py` 的既有断言不变（`last_test()` 仍拒绝陈旧记录）
- [x] 3.2 相关子集回归：`tests/test_external_*.py`（含 `test_external_connections_api.py`、`test_external_connection_service.py`、`test_external_mcp_adapter.py`、`test_external_store_version_guard.py`）
- [x] 3.3 前端用例：`node --test tests/test_external_connections_frontend.cjs tests/test_external_connections_browser.cjs`
- [x] 3.4 `openspec validate fix-connection-test-state-projection --strict`
- [x] 3.5 真实运行面：本机 `weknora-rsmagent` 连接测试通过后，目录卡片与详情显示 `ok` 与测试时间（而非「尚未测试」）

## 4. 收口

- [x] 4.1 若 3.5 与本机原有连接状态不符（例如已存在的 `expired` 连接），记录实际观测值作为证据
- [x] 4.2 登记残留：`test_summary_payload` 的 `tested_at` 形参当前未被使用；按时间自动失效的「结果有效期」未纳入本 change

## 验证记录

| 项 | 命令 | 结果 |
| --- | --- | --- |
| 新增用例 | `pytest tests/test_external_test_state_projection.py -q -p no:randomly` | `13 passed` |
| 相关子集 | `pytest tests/test_external_*.py -q -p no:randomly` | `868 passed, 1 skipped, 2 failed` |
| 前端用例 | `node --test tests/test_external_connections_frontend.cjs` | `17 passed`（含本次新增 2 例） |
| 浏览器用例 | `node --test tests/test_external_connections_browser.cjs` | 本机未装 playwright，用例自跳过（`SKIP`） |
| 严格校验 | `openspec validate fix-connection-test-state-projection --strict` | `Change ... is valid` |

上述 2 例失败为**环境依赖的既有缺陷**，与本次改动无关：在改动前的提交上、同机同环境下运行同样失败。

```
FAILED tests/test_external_connection_service.py::test_plain_member_cannot_manage_tenant_connections
FAILED tests/test_external_connections_api.py::test_a_tenant_catalogue_read_needs_the_read_permission
```

两者都期望普通成员读租户目录被拒（403），实际返回 200 —— 判定依据是本机真实花名册/`identity.db`，不是投影逻辑。已单独提出，不在本 change 内顺手修。

### 新增用例确实会转红（不是摆设）

**1.7 前端两例。** 把服务端响应摘成改动前的样子（`{outcome: 'ok', recorded: true}`，不带 `test_status`）后：

```
✖ a saved test announces the state the server recorded for it
ℹ pass 16
ℹ fail 1
```

**1.6 的窗口。** 把 `lookback_rank <= %d` 放宽到 500（等于取消窗口）后：

```
>       assert _card(svc, stack, connection["id"])["test_status"] == "expired"
E       AssertionError: assert 'ok' == 'expired'
```

没有这道窗口，读者会一直往回翻到那条旧记录，把已经过期的结果重新报成 `ok`。窗口必须在，而且必须在 SQL 里：否则一次读取的行数随该连接的测试次数增长，正是「一次只读 5 条」原本要避免的。

### 真实运行面观测（3.5 / 4.1）

对本机 `identity.db` 的副本（不动生产数据）跑改动后的投影：

```
== tenant catalogue ==
  conn__UXqG__StjiUYim8      YaRuiSAP                 untested  tested_at=None
  conn_8r2lE23PRk0Sd_Wb      OneAgent HTTP MCP        untested  tested_at=None
  conn_cN-lhbMpQlwpiL7k      weknora-rsmagent         ok        tested_at=1790477563
  conn_hkZBBZI4xaGu2pRE      OneAgent OA              untested  tested_at=None
== detail vs test_state for weknora ==
  detail : status=ok tested_at=1790477563 version=2
  state  : status=ok ran_at=1790477563
  card   : status=ok tested_at=1790477563
== live probe through the changed response path ==
  outcome=ok recorded=True test_status=ok tested_at=1790478901
  stages=[('policy', 'ok'), ('auth', 'ok'), ('protocol', 'ok')]
== after the probe, all three surfaces still agree ==
  detail : status=ok tested_at=1790478901 version=2
== editing the row expires the verdict instead of erasing it ==
  state  : status=expired ran_at=1790478901 stage='' code=''
```

三条处置与预期一致，且与本机原有状态相符：同一连接在改动前一律显示 `untested`（缺陷就是这一条），改动后显示 `ok` 与真实测试时间；把配置版本加一后转为 `expired` 且不携带旧 `stage`／`code`。其余三条连接本就没有测试记录，仍为 `untested`，未被本次改动改写。

（`stages` 里出现的是 `policy`／`auth`／`protocol`，与 `runtime.probe` 的 `ProbeResult` 阶段命名一致；`test_binding` 用的 `handshake` 只是那些用例自己的脚本化 adapter。）

### 前端契约早已存在（补充证据）

`channel/web/static/js/external-connections.js` 在改动前的 HEAD 上就已经声明 `expired` 状态、并读取 `payload.test_status`：

```
$ git show HEAD:channel/web/static/js/external-connections.js | rg -n "TEST_STATUS_KEY =|payload.test_status"
74:    var TEST_STATUS_KEY = {
76:        partial: 'ec_status_partial', failed: 'ec_status_failed', expired: 'ec_status_expired',
1833:        var status = payload.test_status || 'untested';
```

也就是说这是**后端没有兑现前端已经写下的契约**，而不是前端少写了一半。1.7 的两条用例因此放在「服务端回什么、console 就显示什么」这一侧。
