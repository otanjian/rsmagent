## 1. 行为与接入点

- [x] 1.1 把自动入口接到复用路径：卡片入口以复用优先，命中最新可续绑定则以 `{binding_id}` 续会话，未命中才以 `{request_id}` 新建；显式「新建工作台会话」保持永远新建。
      → `Scene/sap_workbench/frontend/workbench.js` 的 `startSession(reuseIfAvailable)` 与 `open()`（自动入口传 `true`，显式新建按钮传 `false`）。
- [x] 1.2 复用范围限定为服务端解析的 `tenant_id` + 登录用户；不采信请求体身份，不跨用户/跨租户。
      → 复用经由 `GET /sessions`，服务端为 `store.sessions(ctx.tenant_id, ctx.user_id)`，前端不传身份参数。
- [x] 1.3 复用被拒（`config_conflict` / `session_not_running` / `session_closed`）时回退新建，不静默续旧目标、不停在错误态。
      → `startSession` 中命中该错误集时改以新 `{request_id}` 重试。
- [x] 1.4 只复用 `state != 'closed'` 的绑定；终态记录不复用，按「无可续会话」新建。

## 2. 回归

- [x] 2.1 补前端用例：卡片入口续该账号最新会话，且不产生新的 `request_id`。
      → `tests/test_sap_workbench_frontend.cjs`「card body continues the account's newest session instead of creating one」。
- [x] 2.2 补前端用例：没有可续会话时卡片入口恰好预留一次新绑定。
      → 同文件「card body starts a new bound session when no earlier session can be reused」。
- [x] 2.3 修正既有用例对「卡片入口只发起一次 POST」的假设（现在先 list 后 reserve），并覆盖并发进入只分配一次。
      → 同文件「repeated card clicks…」「two card opens during config loading…」；前端 81 项全通过。
- [ ] 2.4 在可用环境用真实账号验证：进入工作台产生会话 → 关闭对话框或离开 → 再次进入复用同一对话；显式新建后再进入复用新对话。本机无真实账号与配套服务，端到端未完成，记为待确认。

## 3. 规范与文档

- [ ] 3.1 归档时同步 `scene-application-console`、`sap-workbench-session-binding`、`agent-chat-launch` 三条主规范。
- [ ] 3.2 核对 `Scene/sap_workbench/README.md` 对入口的描述，如实说明「自动入口复用、显式入口新建」。
- [ ] 3.3 在有 OpenSpec CLI 的环境执行 `openspec validate resume-scene-session-on-entry --strict` 并记录结果。

## 4. 边界确认

- [ ] 4.1 确认不改变普通智能体卡片与其他场景入口的既有新建行为（`agent-chat-launch` 的限定只用于划界，不改变普通入口）。
