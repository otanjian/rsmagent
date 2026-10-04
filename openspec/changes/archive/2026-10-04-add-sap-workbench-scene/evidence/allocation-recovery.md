# 会话分配与失败恢复

2026-10-03，用户要求暂停 OpenCode 与 SAP Web GUI 的实际操作检查测试后，继续完成任务 3.3。以下仅是代码、临时数据库与本机隔离 HTTP 的验证，不新增现场浏览器、模型或 MCP 调用。

## 实现

- 场景绑定表新增 `allocation_stage`、`allocation_error`，迁移持有 SQLite 写锁并按缺失列增量添加；原配置、绑定 ID、历史及动作记录保留。
- 预留 → host 启动 → 对话就绪 → 浏览器分配 → 就绪分别持久化。对话可用但浏览器尚未连接时，前端显示“对话已就绪，正在连接 SAP 页面”。
- 重复创建仍使用主体与请求键派生的同一远端 ID；同一网关中的并发等待者共享一次资源分配，取消等待不取消创建。
- 恢复先 GET 原会话，只有明确 404 才 POST 原 ID 并独立 GET 核对。鉴权错误、服务故障和传输异常不变成创建。采用的会话必须匹配 ID、`sap` agent 与项目目录。
- 原生 Session V2 的 `create` 已按输入 ID 返回既有记录，相关实现位于参考源码 `packages/core/src/session.ts`；场景不修改该核心逻辑。
- OpenCode 已创建、浏览器分配失败时保留对话及失败阶段，下一次画面连接只补齐浏览器。画面 WebSocket 握手丢失/取消时解除连接占用，使同一浏览器可以重新连接。
- 启动失败先回收资源，再保留阶段及固定错误码，不持久化原始异常文本。恢复从数据库读取最新代次，不沿用清理前的旧行。

## 验证

以下命令通过 **124 项 Python 测试**：

```sh
.venv/bin/python -m pytest -q tests/test_sap_workbench_lifecycle.py tests/test_sap_workbench_runtime.py tests/test_sap_workbench_access.py tests/test_sap_workbench_browser.py tests/test_sap_workbench.py
```

覆盖旧表并发迁移、真实平台主体隔离、取消/重复创建、单侧失败、丢失创建响应后的原 ID 采用、恢复目标不匹配、握手取消、故障清理与代次恢复。BrowserNode、模型和远端会话使用替身或隔离 HTTP，不启动真实 OpenCode/SAP。

**76 项 Node 测试通过**，包括新增的“画面未接通前不能显示连接成功”断言；SAP 前端资源 SHA-256 已同步。

```sh
node --test tests/test_sap_workbench_dom.cjs tests/test_sap_workbench_frontend.cjs tests/test_scenes_frontend.cjs tests/test_coding_frontend.cjs
```

本次没有重跑 Bun host 或实际双栏操作检查。先前 415 项 Python、Bun host 和现场结果保持原日期/范围，不作为当前补丁的全部重跑结果。为保留暂停前的工作台会话，本次未重启现有后端；新后端逻辑在下次正常重启加载。

## 后续范围

- **按最新要求暂停**：OpenCode/SAP 实际操作检查、完整目标单据、表格分页、桌面登录后的双栏及相应现场压力测试。
- **此前已延后**：统一 SAP 登录、完整生产权限/审批与配额、双真实 SAP 身份联动、远程浏览器部署、G3 业务提交及提交恢复。
- 现有双栏会话已切回人工控制。没有保存、暂存、提交或删除 SAP 单据。

任务 3.3 完成后，原完整清单为 **35/57，22 项未完成**，不将暂停/延后项标为完成，也不归档 change。
