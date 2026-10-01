# 实施基线（任务 1.1 / 1.5）

记录本次实施的起点事实，供后续阶段对照。均为静态检查或本机命令的实际输出，未运行真实桌面客户端验收。

## 1. 起点（任务 1.1）

| 项目 | 值 | 来源 |
|---|---|---|
| 起点 commit | `05587992ceba7fc15e37747872bbb0cba876f49c` | `git rev-parse HEAD` |
| 分支 | `rdai` | `git branch --show-current` |
| 身份库 schema 版本 | **35**（`schema_migrations` 已应用 35 条，`max(version)=35`） | `sqlite3 identity.db "SELECT max(version), count(*) FROM schema_migrations"` |
| 迁移机制 | `auth/store.py` 的 `_migrations` 追加式列表；新迁移从版本 **36** 起 | `auth/store.py:29-35` |
| Electron（锁定） | `^33.2.0`（`desktop/package.json`），本机已装 **33.4.11** | `node_modules/electron/package.json` |
| Node | v24.14.1 | `node -v` |
| Python（项目环境） | 3.14.3（`.venv/bin/python`） | `.venv/bin/python --version` |
| Web 装配方式 | `channel/web/core/template.py` 组装页面，**classic/split 对前端不可见**（字节与拆分前一致） | `channel/web/core/template.py:10-11` |
| Web shell 入口 | `channel/web/chat.html`；`classic`=单侧栏，`split`=区域切换，两者共用模块 | `channel/web/chat.html:82` |
| 桌面入口 | `desktop/src/main/index.ts`（`startBackend`/`createWindow`/`setupIPC`） | 读文件确认 |
| 原生会话 | `desktop/src/main/auth-broker.ts`：PKCE + 主进程内存 Bearer | 读文件确认 |

### 相关活跃 change（实施时基线）

`openspec list` 输出中与本 change 有消费关系（proposal「活跃 change」列出）者：

| change | 状态 | 关系 |
|---|---|---|
| `add-traceable-knowledge-ingestion` | 16/33 | W07 知识转换状态以其实际发布能力为准 |
| `add-scheduler-self-delivery` | 34/36 | W13 通知来源与 scheduler 通知以其证据为准 |
| `rename-console-entry-system-access` | 19/20 | W12「系统接入」入口命名 |
| `add-audit-and-token-console` | ✓ Complete | W17 审计/Token 页面 |
| `add-external-mcp-readonly-tool-execution` | ✓ Complete | W12 MCP 分配与只读执行 |
| `promote-team-chat-launch-option` | ✓ Complete | W01/W02 团队对话入口 |

不复制上述 change 未完成实现，也不替其声称验收通过。

### 阶段一功能基线（W01–W20 的现行来源）

W01–W20 的「已发布功能」真值来自服务器 `auth/capability_matrix.py` 的切片声明（`slices` 与 `consumer_availability()`），不是本 change 新建的清单。本机当前声明（`check_consistency()` 返回 `[]`，一致）：

| 切片 | capability | 开放动作 | implemented | accepted |
|---|---|---|---|---|
| `scheduler` | database-scheduler-console | list/toggle/update/delete(read,config)+run(execute) | ✓ | ✓ |
| `memory_browse` | database-memory-console | list/content(read)+save/delete/clear(config) | ✓ | ✓ |
| `project_browse` | scoped-project-browser | browse(read)+import(execute) | ✓ | ✓ |
| `weixin_scan` | channel-scan-onboarding | qr/poll(config) | ✓ | ✓ |
| `desktop_tenant_context` | desktop-tenant-context | **无开放动作**（`open={}`） | ✓ | ✗（未接受） |
| `external_connections` | external-connection-management | 12 个动作（read/config） | ✓ | ✓ |
| `session_context_usage` | session-context-controls | usage(read) | ✓ | ✓ |
| `session_context_compact` | session-context-controls | compact(execute) | ✓ | ✓ |
| scheduler 子切片 ×6 | database-scheduler-console | instances/recipients/create/runs.list/runs.detail/runs.delete | ✓ | ✓ |

未开放能力（如知识转换 `knowledge_conversion_enabled=false`、`knowledge_source_upload_enabled=false`）在 W07 矩阵行必须记为「正确不可用＋原因」，不得记为执行通过。

**仍待 1.1 完成项**：W01–W20 逐行的「实际发布基线」需在阶段一功能承载（任务 5.3–5.5）逐页核对时落表，本文件只固定来源与现状声明。此为**执行中**标记，不是通过。

## 2. 构建目标（任务 1.5）

| 目标平台 | 现状 | 说明 |
|---|---|---|
| Windows 10/11 x64 | 既有 `electron-builder.win.js` / `win.target=nsis(x64)` | 远程模式首批支持 |
| macOS arm64/x64 | `mac.target=dmg,zip`；`electron-builder.js` | 远程模式首批支持 |
| 旧 Electron 发行线 | `index.ts:252-256` 存在 Electron 22（Win7）兼容分支（`net.fetch` 缺省回退） | **不加载远程模块**；保持本地模式 |

**WebContentsView 支持确认**：`node_modules/electron/electron.d.ts:16906` 定义 `class WebContentsView extends View`，本机已装 Electron 33.4.11 ≥ 引入版本 30，满足阶段一容器实现前置。

`desktop/package.json` 的 `publish.url` 目前指向品牌官网（`https://www.rsm.global/china/zh-hans`），**不是**更新 feed（设计 D11 明确禁止把品牌官网当 feed）。远程模式发布前需配置受控 feed；本文件记录该缺口，不修改。

## 3. 未完成标记

- 未运行真实打包客户端（W/A/F/L 矩阵均为待执行）。
- 未创建 `evidence/phase-1.md` 的通过记录；阶段一门槛见 `acceptance.md` 第 3 节。
