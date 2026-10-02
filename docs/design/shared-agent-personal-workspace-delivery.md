# 共享智能体个人工作目录——交付与边界说明

日期：2026-09-29。对应 OpenSpec change：`use-personal-workspace-for-shared-agents`。
行为契约以 `openspec/changes/use-personal-workspace-for-shared-agents/specs/shared-agent-personal-workspace/spec.md`
为准（归档后并入 `openspec/specs/`）；本文只作交付、升级与回退的操作说明。

---

## 1 一句话结论

租户共享的**普通** Agent 在“未选择项目”时，文件/Shell 工具的默认工作目录由 Agent 根改为
`<agent workspace>/user/<不可变用户ID>/`。目录落点、文件面板位置和默认空间提示三者一致。

## 2 生效范围

| 场景 | 默认工作目录 |
| --- | --- |
| 共享普通 Agent，未选择项目（含从未选、已清除、项目目录已失效） | `<agent workspace>/user/<用户ID>` |
| 共享普通 Agent，已选有效项目 | 该项目目录（优先，不变） |
| 本人私有 Agent | 原 Agent 工作目录（不变） |
| coding 等外部 Agent | 原行为（不变） |
| 无经验证用户的请求（系统初始化、无身份上下文） | 原 Agent 工作目录（不变） |

- 用户目录由经验证的租户 + 用户 + 宿主 Agent 决定；客户端或模型自报的 owner、显示名不改变落点。
- 个人默认目录**不登记为项目**，不进入项目最近记录；`/api/projects` 仍用 `current` /
  `default_workspace` / `projects_root` / `recents` 四个原字段表达，“默认空间”名称不变。
- Agent 设定、`RULE.md`、知识库、技能、MCP、会话存储与个人记忆来源不变，仍在 `workspace_dir`
  之下；个人目录里的同名文件不会替换这些配置。

## 3 相对路径基准变化（**BREAKING**）

共享 Agent 无项目会话中，**新一轮**工具调用的相对业务路径以本人目录为基准，例如相对
`output/报告.html` 落在 `.../user/<用户ID>/output/报告.html`。系统提示词会按实际生效目录区分措辞：
选择项目时描述为项目目录，未选择项目时描述为本人目录，两者不会互相冒充。

不迁移、不重写任何既有数据：

- 既有文件、历史消息、已保存的绝对链接保持原存储与原处理方式；
- 缺少原始目录信息的旧相对文本沿用既有缺失处理，**不**按新工作目录猜测同名文件；
- 旧绝对附件链接仍按原明确定位与授权打开。

## 4 无开关、无迁移、无协议变更

- **无新增 feature flag**：生效范围由既有身份、租户绑定与 Agent 类型决定，不新增部署开关。
- **无数据/schema 迁移**：不新增字段、不建迁移脚本、不改 `uploads/outputs/work` 布局。
- **无协议版本变更**：不新增客户端升级拦截；Desktop 与 Web 继续消费原字段。
- 目录准备仅在需要执行时进行（`agent_user_root(..., ensure=True)`），只读投影（默认空间提示、
  会话列表）不创建目录。

## 5 失败语义

`user` 容器是链接/文件、路径不安全、身份无效或存储错误导致准备失败时，本次依赖该目录的执行
**明确失败并给出原因**，不会静默使用 Agent 根或租户根，也不会在公共根生成文件。已有的跨租户、
跨用户文件拒绝与 owner 校验保持不变。

## 6 回退

回退通过代码版本恢复，按既有服务重启流程处理在途请求：不需要数据回滚，也不需要移动文件。
回退后共享 Agent 的无项目会话回到 Agent 根执行；已生成的个人目录文件保留在原处（可继续通过
文件面板按 owner 规则访问），不搬回公共根。HTTP 的 owner 校验不因回退而放宽。

## 7 未承诺的边界

本能力调整的是**业务工作位置**，不是用户级执行隔离：

- 同一租户内的 Python/Shell/技能仍按既有执行授权运行，可以直接读盘到其他位置；
  **默认 cwd 改变不等于脚本沙箱**，本能力不承诺“共享 Agent 内脚本读盘的按用户隔离”。
- 不改变外部 coding、本机文件访问（Desktop 本地目录）与无用户机器任务的既有契约。
- 不新增共享资料入口，不引入成员间分享。

## 8 验证记录（本次实际执行）

| 范围 | 命令（子集） | 结果 |
| --- | --- | --- |
| 本能力单测（解析器/会话 cwd/真实 bash+write/提示词/切项目后重放） | `pytest tests/test_shared_agent_personal_workspace.py` | 45 passed |
| 本能力真机 WSGI 验证（两用户默认提示、只读投影、非项目记录、owner 下载、不安全容器） | `pytest tests/test_shared_agent_personal_workspace_wire.py` | 5 passed |
| 写入工具/编辑工具/工件与传输回归（含 `abs_path` 新字段） | `pytest tests/test_edit_tool.py tests/test_doc_edit.py tests/test_read_edit_improvements.py tests/test_tool_protocol_guard.py tests/test_console_file_transport.py tests/test_subagent_history.py tests/test_private_agent_file_scope.py` | 121 passed |
| 写入工具前端用例 | `node --test tests/test_agent_tools_frontend.cjs` | 11 pass |
| 文件授权/项目浏览/团队/子任务/会话列表回归 | `pytest tests/test_agent_user_file_http.py tests/test_agent_user_file_access.py tests/test_agent_user_file_directories.py tests/test_platform_file_browsing.py tests/test_project_browser.py tests/test_scoped_project_browse.py tests/test_session_history_search.py tests/test_subagent.py tests/test_session_team_runtime.py` | 240 passed, 19 subtests passed |
| 工作目录/Desktop 字段兼容回归 | `pytest tests/test_workspace_user_dir.py tests/test_console_workspace_transport.py tests/test_history_agent_workspace.py tests/test_desktop_contracts.py tests/test_desktop_web_session.py tests/test_desktop_meta.py` | 88 passed |
| 控制台工作目录前端用例 | `node --test tests/test_console_workspace_frontend.cjs tests/test_desktop_workspace_menu_frontend.cjs` | 26 pass |

已知既有缺陷（**与本 change 无关**）：先跑 `tests/test_workspace_user_dir.py`
再跑 `tests/test_multi_agent_state_isolation.py` 时后者 12 个用例失败（`init_scheduler` 报
`tenant 'acme' has no configured shared root`），单独运行该文件 14 passed。属跨文件全局身份服务污染，
未在本 change 内处理。

`tests/test_desktop_web_pages.py::test_the_console_asks_the_adapter_and_nothing_else` 当前失败：该用例枚举
`console.js` 对 `CowDesktopHost` 的调用名，而 `console.js` / `desktop-host.js` 正被并行进行的 Desktop
本地上下文工作修改（会话期间 10:10 仍在改写），新调用 `bindContext` 尚未同步进该断言。本 change 未触碰
这两个前端文件，也未消费该适配器。
