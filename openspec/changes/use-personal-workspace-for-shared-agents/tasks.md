## 1. 默认目录解析

- [x] 1.1 确认前序本人目录落点、物化与 owner 校验实际可用；核对共用 cwd、默认空间投影及相对产物引用的调用点，保留其他进行中工作。
- [x] 1.2 复用 `agent_user_root` 增加共享普通 Agent 默认目录辅助函数，保持项目优先、私有 Agent 原行为；区分只读投影与目录准备，不新增字段协议或项目记录。
- [x] 1.3 测试两用户、无 session 的已登录请求、身份/Agent 不匹配、目录缺失及不安全容器；确认未知身份不猜测用户、准备失败不退公共执行根。

## 2. 会话与文件引用接入

进入本阶段前检查 1.3 的实际结果，不能用硬编码路径或固定身份替代解析。

- [x] 2.1 在 Bridge 现有会话 cwd 应用处接入个人默认目录，复用工具重定向和宿主/父任务继承；不改 Profile、进程 cwd 或状态根，防止展示刷新改动在途实例。
- [x] 2.2 更新 `default_workspace` 等现有默认值投影，核对文件面板正常落点、Web/Desktop 提示及 `@` 的明确文件引用；保留现有浏览根、项目策略、搜索范围和界面名称。
- [x] 2.3 修正新回复/工件的相对路径基准，复用现有消息/工件字段保存明确定位；覆盖 SSE、轮询和重放，旧数据不扫描迁移、不按新 cwd 猜测历史文件。
- [x] 2.4 必要时调整工作目录提示与平台内置资源引用，验证共享设定、技能、知识、MCP 和个人记忆来源保持原位；不复制配置或批量改外部技能。

## 3. 验证与交付

核心接入完成后再做端到端验证；目录切换不是新的脚本隔离保证。本 change 无新增 feature flag，验收现有模式及执行授权保持有效即可。

- [x] 3.1 用两用户并发验证真实 pwd、相对文件生成与本人鉴权下载；验证有效项目优先、清除项目及既有失效项目回默认行为，确认提示与实际 cwd 一致。
- [x] 3.2 验证首次空目录、准备失败、跨租户/他人文件拒绝，失败时无公共目录写入；使用现有权限测试，不扩展为新的脚本沙箱验收。
- [x] 3.3 回归私有 Agent、代表性共享技能、团队/子任务继承、同后端 Desktop 字段兼容、旧绝对附件，以及生成后切项目/重开会话仍可打开新产物；共用后台入口只检查受影响行为，不重构 scheduler。
- [x] 3.4 更新默认目录与相对路径说明，记录无需文件迁移、无需新开关、版本回退保留个人文件的操作，以及尚未承诺的执行隔离边界。
- [x] 3.5 执行相关检查及 `openspec validate use-personal-workspace-for-shared-agents --strict`，记录真实验证结果后再勾选实施任务。

## 实施与验证记录

改动落在既有接入点，未新增开关、字段协议或迁移：

- 目录解析：`agent/workspace/personal_default.py`（`personal_default_dir` / `is_tenant_shared_agent`，只读投影与 `ensure=True` 准备分离，容器不安全时抛 `StateDirError`）。
- 会话 cwd：`bridge/agent_bridge.py::_apply_session_project` 在无有效项目时应用本人目录并记录 `scope="personal"`；`Agent.apply_project_dir(project_dir, scope=...)` 记录 scope，`agent/prompt/builder.py` 据此选择提示措辞。
- 默认空间投影：`channel/web/fork/runtime.py::_default_workspace` 与 `_annotate_sessions_with_projects`；项目选择/清除路由改用 `apply_session_workspace`，清除后回到本人目录。
- 相对引用基准：`_session_workspace_root`（实时实例 cwd → 会话项目 → 本人目录 → 配置根）统一 SSE、轮询与重放的基准；`write`/`edit` 结果新增 `abs_path`，重放优先使用该已保存定位，切项目后仍能打开原产物。
- 平台资源：`workspace_dir` 不变，技能、知识、MCP 与个人记忆来源保持原位（提示词与配置目录断言覆盖）。

验证（真机与回归）见 `docs/design/shared-agent-personal-workspace-delivery.md` §8：本能力 45 + 5 用例全绿，
文件授权/项目/团队/子任务/会话列表回归 240 通过，工作目录与 Desktop 字段回归 88 通过，
`openspec validate use-personal-workspace-for-shared-agents --strict` 通过。
两处已知既有缺陷（跨文件全局身份服务污染、并行 Desktop 工作改动的 `console.js` 断言）与本 change 无关，
未在此处处理。
