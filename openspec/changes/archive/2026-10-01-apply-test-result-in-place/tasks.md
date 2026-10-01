# Tasks: 测试结果就地更新卡片，不重建目录

> 依据：`openspec/specs/external-system-access-console` 的「操作状态与配置生效一致」要求测试呈现真实结果、「正式页面遵循已有客户端体验」要求键盘体验与焦点返回。
> 需求侧 delta 见 `specs/external-system-access-console/spec.md`。服务端投影由归档 change `2026-09-27-fix-connection-test-state-projection` 提供，不在本 change 内改动。

## 1. 回归测试（RED）

- [x] 1.1 `tests/test_external_connections_frontend.cjs`：测试成功后渲染过程中不出现目录加载态，且不再发起 `/types` 请求 —— 用例「a test lands on the tested card without tearing the catalogue down」。RED 证据：断言「no render shows the list taken off the screen」以 `1 !== 0` 失败，捕获到的正是加载面板那一帧
- [x] 1.2 同文件：卡片就地呈现响应中的 `test_status` 与 `tested_at`，且整次操作只有一次请求
- [x] 1.3 同文件：响应的 `connection_id` 不等于卡片 `effective_id` 时，卡片状态与时间均不被改写 —— 用例「a verdict for a row the card does not render is not written onto it」
- [x] 1.4 同文件：`409 test_result_stale` 时只有一次单连接状态读取，并按其结果更新卡片 —— 用例「a discarded result re-reads the one connection instead of the catalogue」。RED 证据：`1 !== 2`（旧实现只发一次请求且不改卡片）
- [x] 1.5 同文件：就地重绘后焦点仍在同一测试控件 —— 用例「the test control keeps the keyboard position after the card is repainted」。RED 证据：旧实现重绘后焦点不再回到控件
- [x] 1.6 同文件：回调到达前身份已变时响应被丢弃 —— 用例「a result arriving under a new identity is dropped」
- [x] 1.7 同文件：不得从用户已移开的控件抢回焦点 —— 用例「a repaint does not take focus from wherever the user moved on to」

### 1.1 测试基础设施补充（为上述断言提供可观测性）

- [x] 1.8 `element().focus()` 记录到 `harness.focused` 并写入 `document.activeElement`：重绘会连带销毁原节点，页面只能靠再次 `focus()` 留住键盘位置，那一次调用必须可观测
- [x] 1.9 `innerHTML` setter 记录每次渲染到 `harness.renders`：stub 的 `fetch` 无 I/O，整条链路在一个事件循环回合内跑完，按 macrotask 采样看不到中间帧，只有逐次记录渲染才能观察到「列表被换掉」这一帧
- [x] 1.10 `catalogueApp` 分发目录条目时复制卡片对象（真实响应每次都是新对象），否则页面就地写入会穿透到各用例共享的 fixture 常量
- [x] 1.11 `catalogueApp` 支持指定卡片所在 scope、原样返回拒绝响应（409）、以及单连接状态读取的返回

## 2. 实现（GREEN）

- [x] 2.1 `external-connections.js`：新增 `ecCardRendersRow` / `ecApplyTestResult` —— 唯一决定「这份回答能否写到这张卡片、写什么」的地方，以「回答描述的行 = 卡片渲染的生效行」为守卫，写入 `test_status` / `tested_at`
- [x] 2.2 同文件：`ecRunTest` 成功路径改为 toast → 应用 → 重绘，不再调用 `loadExternalConnectionsView()`
- [x] 2.3 同文件：`409 test_result_stale` 走 `ecRereadTestState`，只重读该连接的状态并应用；重读失败保留卡片现状
- [x] 2.4 同文件：两条写入路径都在回调处校验 identity token 未变，已变则丢弃
- [x] 2.5 同文件：新增 `ecActiveElement` / `ecFocusIsIdle` / `ecRestoreTestFocus`，重绘后按名回收焦点；用户已移开则不动
- [x] 2.6 复用既有的 `data-ec-action="test"` + `data-id` 定位控件，不新增仅供测试使用的属性

## 3. 验证

- [x] 3.1 新增用例全通过；既有断言不变。`ℹ tests 23 / pass 23 / fail 0`
- [x] 3.2 变异检验（每条守卫被移除时，对应用例必须失败）：
  - 去掉 `ecApplyTestResult` 的生效行守卫 → 「a verdict for a row the card does not render…」失败
  - 去掉 `ecFocusIsIdle` 判断 → 「a repaint does not take focus from wherever the user moved on to」失败
  - 去掉 identity 守卫 → 「a result arriving under a new identity is dropped」失败
  - 409 退回整页重拉 → 「a discarded result re-reads the one connection…」失败
  - 成功后退回整页重拉 → 「a test lands on the tested card without tearing the catalogue down」失败
- [x] 3.3 相关子集不变红：`node --test tests/test_external_connections_frontend.cjs`（23/23）、`node --test tests/test_desktop_external_broker.cjs`（6/6）、`pytest tests/test_external_test_state_projection.py tests/test_external_test_binding.py -q -p no:randomly`（30 passed）
- [x] 3.4 `openspec validate apply-test-result-in-place --strict`
- [x] 3.5 真实运行面：本机控制台对 `weknora-rsmagent` 点击「测试连接」，`POST .../test` 是唯一请求，渲染过程中从未出现加载面板，卡片始终在文档中，`performance.timeOrigin` 不变，时间就地由 11:27:00 更新为 11:42:19。证据见 `evidence/live-console-in-place-test/`

## 4. 收口

- [x] 4.1 登记残留：`overridden` 卡片上的测试按钮仍测平台模板行（`ecPathFor(card.scope, card.id)`），「测生效行」是否更合理另议 —— 本 change 只保证不把该结果写进卡片
- [x] 4.2 记录真实运行面观测值作为证据
- [ ] 4.3 单独提出（不属于本 change）：`tests/test_external_connections_browser.cjs` 在本机失败于 `#app` 始终 hidden —— 用变更前的模块重跑同样失败，是既有缺陷
