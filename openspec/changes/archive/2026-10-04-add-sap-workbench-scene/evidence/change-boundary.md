# 实施基线与改动边界

2026-10-04 卡片直接新建：仅修改 `Scene/_shared/frontend/adapter.js` 的 SAP 分支一行，将普通打开参数设为 `new-session`；自动新建、成功配置判断和重复入口处理放在场景 `frontend/workbench.js`，仅更新该文件的资源摘要。配置按钮、其他场景和后端接口保持原合同。相关前端 96 pass、入口/资源路由 1 pass；见 [本次记录](card-create-session.md)。此前差异及其他 change 保留。

2026-10-04 后续收尾：新增窄范围供应商 F4 识别与关闭页面条件，仅改场景 `effects.py`/`page.py`；补充 F4、临时显示进程与目标覆盖专项，更正已登记 SAP `/browser` 路由的注释（该旧入口仍返回 410，功能无变化）。proposal/design/spec 的当前独立登录与未来流程已分开，3.2 按原管理员登记合同完成，未为它新增资源目录/公共 IAM 改造。全套 859 pass / 1 skip，两个源码后端保留原环境加载，当前 41/57；实际操作继续暂停，见 [后续收尾](incremental-closeout.md)。其余已跟踪差异原样保留，rsmcode/opencode 与 sap-connect 均 clean，未提交/推送/归档。

2026-10-04 本机独立收尾补记：新增 sessions 输入校验、跨租户总节点容量和正常退出/迟到 spawn 清理，仅修改 `Scene/sap_workbench/backend/http.py`、`backend/runtime.py`、`browser_service/manager.py`、`runner.py`、`node.py`，新增三份 SAP 专项测试及场景/change 说明。原入口注册、普通 coding 工作区差异、桌面和 rsmcode 源码保持。SAP 全套 819 pass / 1 skip；两个后端已保留原环境重载，实际 SAP/OpenCode 操作仍暂缓。当前结果见 [本机收尾](local-closeout.md)，以下基线、文件计数和各轮回归为历史记录。

2026-10-03 开始 apply：本项目 `416aebf3`；参考 OpenCode `0442518883`。开始时仅本 change 文档未跟踪，无既有程序改动。

| 路径 | 必要改动 | 替代方案/限制 |
| --- | --- | --- |
| `Scene/sap_workbench/**` | 新场景界面、配置与存储、专属 handler、说明 | 新业务全部放本目录 |
| `Scene/catalog.json` | 增加一个场景 ID | 沿用目录读取方式 |
| `Scene/source-manifest.json` | 登记本项目原创前端资源和摘要 | 不改变原迁入资源内容/来源 |
| `Scene/_shared/frontend/adapter.js` | 增加 `sap_workbench` 专用分发 | 实际入口优先使用 SceneOriginal；单独加 registry 无效 |
| `channel/web/static/js/scenes/index.js` | 阻止 SAP 专用运行时缺失后普通激活；首次展示默认“全部”分类 | 原场景保持原有回退；后续用户选择的分类仍保留 |
| `channel/web/route_registry.py` | 登记场景配置/就绪接口及授权 | 不自建路由表 |
| `channel/web/web_channel.py` | 引入专属 handlers | 无 SAP 业务实现 |
| `tests/test_sap_workbench*` | 新增专项测试 | 实际授权、持久化、并发/拒绝行为 |
| `tests/test_scene_skills.py` | 场景数 26 → 27 | 既有 81 子场景断言保持，原创资源继续校验摘要 |
| `tests/test_scenes_frontend.cjs` | 校验首次进入及数据分类可发现 SAP 工作台 | 读取真实场景目录，覆盖分类切换和返回时保留筛选 |

不修改普通 Agent/Bridge、通用 coding 后端、其他场景、身份核心、桌面主进程及 `rsmcode`。本轮只对已具备依赖的实现勾选任务；未通过 G0 的执行链保持关闭。

## G0 环境事实

- 本机原 `config.json` 未配置 `opencode` 或 SAP 工作台，本轮没有修改该生产配置。用户随后提供 SAP Web GUI URL、Client 200 和测试凭据；凭据只用于用户已打开的 Chrome 登录，不进入源码、配置、示例、测试或证据文件。
- 已从 `rsmcode/opencode@0442518883` 启动新开发进程：API `127.0.0.1:44096`，Web `localhost:4444`；未重启已有服务。既有 `OpenCodeClient` 创建/读取/同 ID 重试和真实 iframe ready/session 通过。详情见 `integration.md`。
- Chrome 登录后观察到系统 S4H、SAP NetWeaver 758、中文 Client 200；已只读进入 SPRO 参考 IMG。内置浏览器证书校验失败，用户在 Chrome 完成人工证书处理；独立执行节点的 CA 信任仍需部署，不能关闭校验。
- 本机 Docker CLI 可用，daemon 不可达（Colima socket 不存在）；不启动/重配用户服务。
- 本机测试 Python 3.14.3，pytest、web.py、Playwright、cryptography 已安装；这不代表浏览器执行节点或 SAP 环境已就绪。
- OpenCode 当前会话路径使用 V2；源码明确记录 canonical 工具注册的 MCP/插件接入缺口。旧插件声明不能代替真实工具调用证据。外置 SDK host 是否可完整承载现有 Web 尚未证明，不宣称只能改核心。
- 尚未获得首条可写单据交易、实际 MCP 端点及按用户隔离的浏览器执行节点。没有测试提交或声称自动操作通过。

## 依赖切片

| 能力 | 可复用接口/观察 | 本 change 的门槛 |
| --- | --- | --- |
| 平台身份与场景 API | `_db_scope`、`chat.use`、coding 资源 use 检查、`require_management_write` | 用真实身份栈验证跨用户/租户/撤权与 CSRF |
| 正式审计 | `IdentityService.record_business_audit` | 配置写入记录版本和动作，不记录凭据 |
| 凭据 | 有加密、个人资源配置及按资源解析接口 | 需验证 SAP 会话级资源可合法使用、过期/注销撤销；不调用私有方法或伪造成员权限 |
| OpenCode 项目/会话 | 既有 coding 配置与服务；实际 API/iframe 已验证 | 逐会话上游访问隔离尚未通过，开发实例不得作为生产 SAP 接入 |
| OpenCode 工具 | V2 上下文有 sessionID，但注册接入存在源码缺口 | 真实无副作用调用及取消通过后才接通 |
| SAP 同源账号 | 已有 sap_connect 接受 SAP user/password/client | 需受信桥注入、两侧实际身份核验；不让密码进入模型工具正文 |
| 配额 | 已有 tokens/tool_calls 等消费接口 | 缺少外部 OpenCode 模型硬计量证据，不能用浏览器并发限制替代 |
| 审批 | 复用既有高风险动作机制 | G3 前验证参数指纹、职责分离和未知提交恢复 |

以上未满足项影响相关运行能力，不阻止独立场景界面、配置校验与存储开发；不能把本文件视为 G0 通过证据。

## 本轮差异核对

初次验证修改 7 个既有文件；用户反馈找不到 SAP 入口后，补充一处首次分类展示修复及既有前端测试，共涉及表中 8 个既有文件。新增业务全部在 `Scene/sap_workbench/`，专项测试在 `tests/test_sap_workbench*`。`git -C ../rsmcode/opencode status --short` 为空。没有提交 Git，没有修改真实账号或保存 SAP 业务内容。

配置数据库放平台数据根 `scenes/sap_workbench.sqlite3`，不放 Agent 可浏览的共享项目目录；没有改公共身份/聊天存储。后续如启用实际会话，仍须完成会话表存取、凭据和租约授权，当前建表不代表运行能力完成。

## 2026-10-03 真实运行更新（取代以上早期环境状态）

当前源码实现及验收范围见 [live-workbench.md](live-workbench.md)。已通过真实模型/MCP/页面导航及 ME21N 日期填写、独立回读与恢复；未完成完整单据业务校验和提交。用户后续明确授权 MCP 项目配置、配置账号密码和单一测试源的后台 TLS 例外；没有浏览器自动跳过证书。

左侧采用场景内 CDP 截屏/输入桥，复用现有 ChromeLauncher，仅操作专属 profile；本轮没有修改公共浏览器工具。右侧采用场景内 canonical Session V2 host 和事件投影，无 OpenCode 核心改动。`git -C ../rsmcode/opencode status --short` 仍为空。

此次收尾修复仅限 `Scene/sap_workbench/**`、SAP 专项测试及文档；额外必要更新 `tests/test_scenes_api.py` 的场景数 26 → 27 和 `Scene/source-manifest.json` 中 SAP 资产摘要。已有其他工作区差异予以保留，没有把它们重置或归入本次修复。

当前运行数据、Chrome profile、构建产物和私有数据库不纳入源码。项目 `sapwork/opencode.json` 只登记用户授权的固定 MCP 端点，不含 SAP 凭据。权限/审批与提交作为后续未验收阶段保留，不以“先跑通”宣称生产就绪。

## 2026-10-03 并行收尾：当前工作区边界与回归

本次核对时本项目 `HEAD` 仍为 `416aebf3`，`git diff --name-only` 共列出 16 个已跟踪文件。以下记录补充早期的 7/8 个文件计数，不删除或改写当时的实施基线。新增场景目录、专项测试及 change 文档仍为未跟踪文件，不计入这 16 个已跟踪文件。

### 11 个 SAP 场景必要接入和回归文件

| 路径 | 已有差异的必要性与边界 |
| --- | --- |
| `.gitignore` | 忽略 SAP 场景数据库、浏览器登录状态、构建资源和运行目录，避免运行数据进入源码。 |
| `Scene/_shared/frontend/adapter.js` | 仅增加 SAP 工作台打开及配置面的专用分发。 |
| `Scene/catalog.json` | 追加 `sap_workbench`，保留原场景目录项。 |
| `Scene/source-manifest.json` | 登记原创 SAP 前端资产和目录适配来源；本次仅同步 SAP 资产摘要。 |
| `channel/web/route_registry.py` | 登记 SAP 场景专属配置、检测、会话和浏览器接口；无普通执行器业务。 |
| `channel/web/web_channel.py` | 引入上述 SAP handlers，业务实现保留在场景目录。 |
| `channel/web/static/js/scenes/index.js` | 首次显示全部分类、SAP 运行时失败后拒绝普通激活，以及声明式卡片配置入口和键盘分发。 |
| `channel/web/static/js/i18n/home-scenes.js` | 为卡片配置入口增加简体、繁体、英文文案。 |
| `tests/test_scene_skills.py` | 场景总数 26 → 27；81 个既有子场景和来源摘要检查保留。 |
| `tests/test_scenes_api.py` | 场景 API 的总数断言 26 → 27。 |
| `tests/test_scenes_frontend.cjs` | 覆盖 SAP 场景可发现性、分类保留、配置控件及既有普通工作台分发。 |

### 5 个既有 coding 前端差异

交接时工作区另有 `channel/web/static/css/coding.css`、`channel/web/static/js/coding.js`、`channel/web/static/js/console.js`、`channel/web/static/js/i18n/coding.js`、`tests/test_coding_frontend.cjs` 的差异。本次只读核对并保留，没有编辑、重置或把它们归入 SAP 收尾新增改动；SAP 场景使用独立 host 和适配器，这些普通 coding 差异不作为扩大场景实施边界的依据。

`git diff --name-only -- agent auth bridge scenes desktop rsmcode` 为空；没有普通 Agent/Bridge、通用 coding 后端、身份核心、其他场景业务和桌面主进程的已跟踪改动。`git -C ../rsmcode/opencode status --short` 为空，参考 OpenCode 工作区干净。

### 本次隔离回归及历史摘要失败

执行以下 11 个现有 Python 测试文件，使用临时身份、数据库和 OpenCode HTTP 替身；没有启动或操控真实 Chrome、OpenCode、SAP、MCP，也没有调用真实模型：

```sh
.venv/bin/python -m pytest -q \
  tests/test_coding_session_routes.py \
  tests/test_coding_session_lifecycle.py \
  tests/test_coding_session_sync.py \
  tests/test_coding_agent_routes.py \
  tests/test_coding_agent_type_boundary.py \
  tests/test_coding_page_assets.py \
  tests/test_coding_session_store.py \
  tests/test_coding_settings.py \
  tests/test_web_chat_boundary.py \
  tests/test_scenes_api.py \
  tests/test_scene_skills.py
```

pytest 原始汇总为 **181 passed、236 subtests passed、6 failed、1 warning，70.90 秒**。六个失败均来自 `SceneMigrationTests.test_original_files_match_recorded_source_hashes` 中以下非 SAP 资源的 `subTest`，普通 coding、Web 聊天边界及场景 API 回归通过；一条 warning 为既有 `setDaemon()` 弃用提示。

- `procurement_tender/skills/bid-analysis/scripts/document_to_md.py`
- `finance_report_audit/skills/financial-reprot-audit/requirements.txt`
- `finance_report_audit/skills/financial-reprot-audit/setup.py`
- `finance_report_audit/skills/financial-reprot-audit/SKILL.md`
- `finance_report_audit/skills/financial-reprot-audit/parsers/pdf_parser.py`
- `finance_report_audit/skills/financial-reprot-audit/strategies/related_party.py`

对照方法为读取 `git show HEAD:Scene/source-manifest.json`，再读取上述各自的 `git show HEAD:Scene/<path>` 原始字节并计算 SHA-256。六项在 `HEAD` 中也与 `HEAD` manifest 记录不等，确认不是 SAP 场景引入的问题；本次没有修改这些资源或它们的摘要。当前 manifest 共 242 个资源条目，只有上述六个既有条目不匹配，SAP 场景资产摘要匹配。

本次证据仅覆盖独立实现与隔离回归。用户暂停的 OpenCode/SAP 实际操作检查、尚未完成的真实双用户/SSO/完整单据验收，以及已延后的统一登录、生产权限与审批、远程部署、G3 提交，继续保留未完成状态，不能由本次回归代替。


## 2026-10-03 凭据生命周期、版本基线与动作效果

本轮新增/修改仅在 `Scene/sap_workbench/**`、`tests/test_sap_workbench*` 及本 change 文档，未增加既有公共文件改动。当前 16 个既有差异仍是上文 11 个 SAP 必要接入/回归和 5 个保留的普通 coding 差异；没有修改或回退 `rsmcode` 工作区。

新增 MCP 即时回收/同项目隔离、只读兼容元数据报告，以及首批 SPRO/ME21N 自动动作效果、焦点和特定回读检查；随后补齐受限表格滚动和桥请求/排队/派发前复验、失败终态及模型预留处理。最终 SAP 套件 470 项通过、1 项真实 Chrome 测试按要求跳过；相关 Node 93 项通过。工具包仅做 TypeScript 语法构建，不执行 host。任务 6.1 的独立登记实现完成；1.5、5.3、5.8 的完整现场验收仍保留。OpenSpec strict 与 Git 空白检查通过，真实操作未重测，主后端未重启。

只读核对发现用户当前 OpenCode 来源为 `9acdb1d09f`，与历史 `0442518883` 不同；历史提交的 Web package 1.18.31 与后续 1.18.34 不能组成同一发布锁。26 个场景 host 引用的源码接缝静态未变，尚不能证明运行兼容。版本报告九项匹配、一项漂移，详见 [兼容基线](compatibility-baseline.md)；保持当前源码和历史验收边界，不修改外部程序。
