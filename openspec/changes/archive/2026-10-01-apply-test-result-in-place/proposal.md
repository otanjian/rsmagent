## Why

`external-system-access-console` 要求测试「呈现具体阶段、成功/失败/超时/协议部分成功和脱敏原因」（`操作状态与配置生效一致`），并要求页面「支持窄屏、键盘访问、焦点返回」（`正式页面遵循已有客户端体验`）。上一个 change 已让服务端把记录结果送进响应，但控制台收到后的处理是把整块列表拆掉重建：`ecRunTest`（`external-connections.js:1822`）成功时调用 `loadExternalConnectionsView()`，后者置 `status='loading'`、清空 `cards`、重绘 —— 列表消失，恢复要等 `/types` 与各 scope 的 `/catalog` 两个**串行**请求。本机实测（`weknora-rsmagent`，点击卡片上的「测试连接」）：

| 时刻 (ms) | 观测 |
|---|---|
| 3298132 | 基线：`连接正常` / 最近测试 11:18:45 |
| 3308716 | toast：`连接正常` |
| 3308719 | **卡片消失**（列表被加载面板替换） |
| 3308803 | 卡片回来：`连接正常` / 最近测试 11:25:44 |

本机 84ms，真实网络下是两个 RTT。这次操作还使焦点丢失 —— 按钮随面板一起被销毁，`document.activeElement` 落回 body，键盘用户失去位置。

更重要的是：整页重拉目前还**兼着一个未被察觉的职责**。`ecRunTest` 发往 `ecPathFor(card.scope, card.id)` —— 卡片**自身**的 id；而卡片呈现的是**生效行**（`effective_id`）的状态。租户对平台模板建立覆盖后，卡片 `id=模板`、`effective_id=覆盖行`：该按钮测的是**模板**，卡片显示的却是**覆盖行**。整页重拉让模板的新结果被自然忽略，从而掩盖了这处不一致。因此任何「把响应写到卡片上」的改动都必须显式处理它，否则会重现上一个 change 刚修掉的缺陷类别 —— 界面呈现一份并非生效配置的结论。

## What Changes

- 一次测试完成后，其记录结果 SHALL **就地**反映到对应卡片；该过程 MUST NOT 使目录退回加载态，MUST NOT 触发目录整体重拉。
- 写入 SHALL 以「响应描述的行 = 卡片呈现的生效行」为条件。响应来自非生效行时卡片保持不动，提示仍如实报告被测对象。
- 结果因配置在测试期间变更而被丢弃（`409 test_result_stale`）时，SHALL 只重读该连接的状态并据此更新，MUST NOT 把被丢弃的结果显示为当前结论，也 MUST NOT 退回整页重拉。
- 重绘后 SHALL 把键盘焦点还给触发该操作的控件。
- 结果写入前 SHALL 校验身份未变；身份或租户已变的迟到响应 MUST NOT 写入。

服务端行为、测试的执行与记录路径、其余写操作（保存／删除／启停／租户授权）的整页重拉均不变。

## Capabilities

### Modified Capabilities

- `external-system-access-console`：
  - `操作状态与配置生效一致` 补「测试结果就地更新」义务 —— 结果按生效行写入卡片、不使列表退回加载态、不触发整体重拉、被丢弃的结果不得显示为当前结论。
  - `正式页面遵循已有客户端体验` 补「操作后重绘保持键盘焦点」义务。

后端投影已由 `2026-09-27-fix-connection-test-state-projection` 落地（归档于 `openspec/changes/archive/`），本 change 只补前端消费侧，不重复声明服务端职责。

## Impact

- **行为受影响**：在连接卡片上触发「测试连接」后，列表不再消失重建；卡片就地更新；键盘焦点保持。
- **不受影响**：所有服务端接口与响应形状、测试执行与记录、`loadExternalConnectionsView()` 其余 7 处调用点（保存／删除／启停／租户授权／刷新／重试／身份切换）、筛选与统计口径、`overridden` 卡片测试按钮所测的行（本 change 不改「测哪一行」的语义）。
- **代码面**：`channel/web/static/js/external-connections.js`（`ecRunTest`、新增「应用测试结果」判定点、测试按钮的可定位标记、重绘后的焦点恢复）。
- **测试面**：`tests/test_external_connections_frontend.cjs` 新增用例（不退回加载态、不再发 `/types`、就地更新、非生效行不写入、409 只重读单个连接、焦点保持、迟到响应不写入）。
- **登记为残留**：`overridden` 卡片上的测试按钮仍然测平台模板行；「测生效行」是否更合理另议，不在本 change 内改变语义。
