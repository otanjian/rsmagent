# 验收证据：工作区面板落在当前 Agent 自己的目录

环境：本机 `localhost:9899`，租户 `test15`（共享根 `/Users/jiantan/.cow/tenant-roots/tenants/test15`），当前会话 `session_27eba46e-002d-49cf-8695-de79def69094`，当前 Agent `my-assistant-admin-test15`。浏览器为 Cursor 内置浏览器（已登录），代码为本次改动后的工作树。

## 1. 打开入口：落在当前 Agent 自己的目录（任务 6.2）

在页面内挂钩 `window.fetch` 记录 `/api/workspace/*`、`/api/agents*` 请求，点击 `#workspace-toggle-btn`：

```
requests = [
  "/api/workspace/tree?path=agents%2Fmy-assistant-admin-test15&session=session_27eba46e-…&agent=my-assistant-admin-test15"
]
wsCurrentDir = "agents/my-assistant-admin-test15"
面包屑      = "/agents/my-assistant-admin-test15"
页签        = 文件（active）、预览（非 active）
文件列表    = knowledge, memory, scheduler, tmp, MEMORY.md, RULE.md, AGENT.md, USER.md
```

- 只有 **一次** 列举请求，路径就是该 Agent 自己的目录（`path=agents/<agent id>`），不是工作区根、也不是上次浏览过的目录。
- 列表内容与 `ls ~/.cow/tenant-roots/tenants/test15/agents/my-assistant-admin-test15` 一致（该目录的 `AGENT.md`、`MEMORY.md`、`RULE.md`、`USER.md`、`knowledge/`、`memory/`、`scheduler/`、`tmp/`）。
- 复现前提：同一个会话上一次停在「预览」页签、`wsCurrentDir` 为别的目录时，重新打开仍落在上述目录。

证据截图：`evidence/1-agent-folder.png`

## 2. 目录被拒且无本人私有 Agent：本地化提示（任务 6.3）

把页面内的 `activeAgentId` 换成本租户未绑定的标识 `no-such-agent-xyz`（`_require_tenant_agent_binding` 返回 404），再点入口：

```
requests = [
  "/api/workspace/tree?path=agents%2Fno-such-agent-xyz&session=…&agent=no-such-agent-xyz",   # 404
  "/api/agents?view=personal",                                                               # 该管理员无私有 Agent
  "/api/workspace/tree?path=&session=…&agent=no-such-agent-xyz"                               # 工作区根候选，同样 404
]
面板文案 = "没有权限打开该智能体的目录"
```

- 只探询一次本人的私有 Agent（`fallbackTried` 记账），没有重试循环。
- 面板不再呈现服务端的原始串 `agent not found`，而是本地化文案。

证据截图：`evidence/2-refused-agent.png`

## 3. 未在真实实例验证的部分（任务 6.4）

「**拥有本人私有 Agent** 的成员对他人私有 Agent 的会话点入口 → 回落到 `agents/<我的私有智能体>` 并出现切换提示」未做真实实例验证：本机 `test15` 只有租户管理员账号，`GET /api/agents?view=personal` 对其返回空列表（租户/平台管理员按设计豁免于「无 Agent 选择即本人私有助理」的锚定）。该路径由 `tests/test_console_workspace_frontend.cjs::a refused Agent folder falls back to the caller own private Agent` 以 mock 响应钉住（请求序列与落点目录均断言）。
