# 2026-10-03 联调与验收映射

## 实际环境

| 项目 | 本轮观察 |
| --- | --- |
| 本项目 | 源码 `416aebf3`，新增场景补丁未提交 |
| OpenCode | 源码 `0442518883`，Bun 从源码启动 API 44096 / Vite Web 4444，健康接口报告 `local` |
| SAP | 用户提供 `https://sap.goodsap.cn:44300/sap/bc/gui/sap/its/webgui/?sap-client=200&sap-language=zh` |
| SAP 登录后 | S4H / 200，SAP NetWeaver 758，中文，SAP GUI for HTML；精确内核 patch/主题名未核实 |
| 浏览器 | 用户现有 Chrome；没有以其 cookie/profile 作为工作台执行节点 |
| 独立浏览器节点 | 未部署；本机 Docker daemon 不可达 |
| MCP | 可参考 `rsmcode/sap-connect`；本轮没有可验收的端点/同源凭据桥 |

内置浏览器最初返回 `ERR_CERT_AUTHORITY_INVALID`。用户随后提供已能进入登录页的 Chrome，自动化接管该页后正常登录。登录凭据未写入本文件或程序配置。开发者 Chrome 的信任例外不证明全新浏览器节点可通过 TLS 校验。

## 已验证的真实行为

1. `GET /global/health` 返回 200 / healthy。
2. 在临时空项目中使用现有 `agent.coding.opencode.OpenCodeClient`，真实创建会话、同 ID 重试、读取均通过；ID、project_id、目录一致，最后删除仅本次创建的空会话。
3. 实际 Vite Web 嵌入临时测试宿主，接收 `rsm.opencode.ready` 和 `rsm.opencode.session`，后者等于预建会话 ID。测试宿主校验 origin、frame window、channel。原生提示输入、模型选择等可见，未发送提示词或调用模型。新场景的正式绑定挂载器仍未接通。
4. 使用真实场景 JS/CSS 和真实 HTTP handler、临时身份库测试配置页：管理员保存 SAP URL/Client/coding 引用成功；项目显示来自 coding 配置；简中、英文、繁中及现有深色主题正常显示。
5. 原对话草稿保留；键盘可调分栏；重复点击连接配置不重建未保存表单。Esc 触发了放弃修改确认；Chrome 自动化在该原生确认框上超时，取消/继续后的完整交互暂不算通过。窄屏 viewport 工具未实际改变目标页宽度，移动布局本轮不记实测通过。
6. SAP 真实只读路径：SAP 菜单 → 工具 → 定制 → 实施指南 → SPRO 执行项目 → SAP 参考 IMG → 显示实施指南。菜单、标题和 IMG 节点可被浏览器可访问性接口识别。没有进入配置编辑、没有保存/过账；这只是页面兼容观察，不是 OpenCode 工具联动或 G2 通过。

## 自动化测试

- 后端/回归：`tests/test_sap_workbench.py`、`test_route_registry.py`、`test_scene_original_runtime.py`、`test_scene_skills.py`、`test_coding_session_routes.py`、`test_coding_agent_type_boundary.py`、`test_web_chat_boundary.py`，排除已知基线资源摘要测试：**144 passed, 1 deselected**。另有旧代码 `setDaemon()` 弃用警告。
- 新场景前端契约及既有 OpenCode 前端：`node --test tests/test_sap_workbench_frontend.cjs tests/test_coding_frontend.cjs`：**34 passed**。
- `test_coding_settings.py` 随初次配置/路由专项运行通过；配置示例通过 `validate_config`，三个开关缺省关闭。
- Python 语法及前端 `node --check` 通过。

被排除的摘要测试有 6 个既存不一致文件：`procurement_tender/skills/bid-analysis/scripts/document_to_md.py`；`finance_report_audit/skills/financial-reprot-audit/` 下的 `requirements.txt`、`setup.py`、`SKILL.md`、`parsers/pdf_parser.py`、`strategies/related_party.py`。逐文件当前 SHA-256 与 `git show HEAD:<path>` 一致，未为通过测试改其内容或清单。新场景资产及 catalog 的摘要已更新。

扩展运行旧场景 Node 套件时，`legacy registry preserves the generic workbench fallback` 与 `a workbench scene with a registered renderer dispatches instead of activating` 失败；在临时目录从 HEAD 提取源码/测试后同样失败。该基线运行另出现 empty-catalog 失败；不归因本次 SAP 补丁。没有改这些不相关测试。

## 规范到证据

| 规范/任务范围 | 证据 | 结论 |
| --- | --- | --- |
| 新增场景、目录登记、专用分发、无普通会话副作用（2.1/2.2/2.6） | 真实 catalog/assets/handler 测试 + 前端运行时分发测试 | 配置阶段通过 |
| 配置字段归属与 MCP 同源规则（2.7） | UI 保存 + 配置 schema 拒绝账号/密码/任意命令，示例校验 | 配置规则通过，凭据实际复用未实现 |
| 租户/权限/CSRF/撤权/注销（3.2/3.8 部分） | 真实 WSGI 身份栈、错误身份/外国 Origin/拒绝写入测试 | 仅已实现配置接口通过 |
| 版本/迁移/审计（3.1/3.9 部分） | 重复建表、旧表保留、两个并发写者、失败审计回滚 | 配置存储通过；会话存取未实现 |
| OpenCode 会话与嵌入（1.2） | 当前源码进程真实 API 与 iframe 通知 | 通过；不等于工具路径通过 |
| G0 工具/凭据/上游授权/配额 | `runtime-dependencies.md` 文件级评估 | 未通过，相关能力关闭 |
| G1 可视远程浏览器 | 无按用户隔离的执行节点/noVNC 流验收 | 未通过 |
| G2 同一页面自动操作 | 仅 Chrome 手工控制路径探测 | 未通过 |
| G3 业务提交 | 未执行提交 | 未通过 |
| G4 发布 | 部分回归通过，端到端验收和依赖未完成 | 不建议启用运行功能 |

本文件不含秘密、会话 cookie 或模型对话正文。测试替身只用于已列明的本地授权/界面契约；没有把模拟工具或源码声明作为真实 SAP 自动化的成功证据。

## 入口可发现性修复

用户反馈找不到 SAP 工作台后，核实实际目录共 27 个场景，`sap_workbench` 属于“数据”分类。原场景页首次默认选中“采购”，因此不会直接展示 SAP 卡片。首次默认改为“全部”；用户后续选择的分类仍保留。真实目录驱动的前端测试覆盖首次展示、采购/数据切换以及重入后的筛选保留。

本次专项验证：场景展示相关 4 项 Node 测试、SAP 专用分发 5 项 Node 测试、目录与资产授权 1 项后端测试通过。运行中的 `localhost:9899/chat` 已返回新版本场景脚本 URL，脚本内容与磁盘源码一致。Chrome 自动化连接超时，本次没有把自动化 DOM 契约测试描述为用户登录页面的视觉验收；用户刷新后可在“全部”或“数据”中进入。实际 SAP 自动控制能力仍未接通。

## 点击无响应修复（2026-10-03）

实际宿主的 `wsGuardUnsaved(next)` 在没有未保存编辑时返回 `true`，由调用方继续执行；只有用户确认放弃编辑时才执行 `next`。SAP 入口误将 guard 的返回值直接返回，导致干净状态下没有创建窗口或读取配置。修复仅调整 SAP 场景的 `open()`：guard 返回 `false` 时停止，否则立即执行打开逻辑，并更新该场景资源摘要。未修改宿主 guard 或普通智能体代码。

新增回归测试直接加载宿主源码中的 guard，覆盖干净状态、无宿主 guard、关闭后重开，以及有未保存编辑时延迟打开。修复前仅“宿主 guard 存在且编辑器干净”用例失败（未创建窗口），修复后前端 8 项全部通过；`test_sap_workbench.py` 与 `test_scene_original_runtime.py` 后端共 35 项通过。新场景脚本与实际服务返回的 runtime 均通过语法检查。

已重启 `localhost:9899` 的 Web 后端，`/api/health`、`/chat`、`/scene-assets/runtime.js` 均返回 200，实际 runtime 包含修复后的 guard 分支。Google Chrome 自动化在标记会话、读取浏览器状态和新开测试标签页时均超时；已请求用户恢复 Chrome 扩展连接。本轮尚无真实 Chrome 点击和截图验收，不将上述本地契约测试计为浏览器验收。工作台当前仍为连接配置阶段，SAP/OpenCode 会话和自动控制能力未接通。

## 配置保存 400 修复（2026-10-03，内置浏览器实测）

本轮改用 Cursor 内置浏览器完成真实点击路径，不再依赖外部 Chrome 扩展。以真实租户管理员 `test15`（`tnt_EA3qM-lHPLD8ZPwW`）登录 `http://localhost:9899/chat`，依次执行：侧栏「场景应用」→ 卡片「SAP 工作台」→ 工作台弹窗打开 → 「连接配置」表单 → 「保存配置」。

- 现象：保存返回 `400 {"code":"invalid_request"}`，界面回退到通用「请求失败，请重试。」
- 根因：控制台全局 fetch 垫片（`channel/web/static/js/console.js`，约 6521 行）会给每个同源 JSON 请求注入路由元数据——`X-Tenant-ID` 头，以及正文里的 `agent_id`（正文已有同名键时不注入）；`web.py` 又会把查询参数并入正文。SAP 场景的 `_body()` 原本用 `set(value) - set(keys)` 严格拒绝任何未声明顶层键，于是把平台注入的 `agent_id` 当成非法字段。
- 对照证明（同一浏览器内用 `XMLHttpRequest` 绕开垫片）：带 `X-Tenant-ID`、正文**不含** `agent_id` → `200`，保存 `version` 加一；正文**含** `agent_id` → `400 invalid_request`。同一正文仅此一个字段之差。
- 修复：`Scene/sap_workbench/backend/http.py` 的 `_body()` 在未知键校验前丢弃 `agent_id`（传输元数据，不属于场景输入），其余严格校验不变。
- 回归测试：`tests/test_sap_workbench.py::test_platform_agent_routing_metadata_is_ignored`，同时覆盖 `PUT /config` 与 `POST /check` 携带 `agent_id` 的情况。

重启 `localhost:9899`（同一 `COW_WEB_PORT`/`COW_TENANT_BASE`/`COW_CREDENTIAL_MASTER_KEY`，改用 screen 会话后台运行）后复验：

- 真实 UI 保存返回「配置已保存。运行功能仍需通过联调后开放。」，`version` 递增。
- 真实 UI「检查已保存配置」返回分项：SAP 页面/项目目录/浏览器执行节点「已填写」，OpenCode 对话/MCP 连接「未配置」，全部「未联调」。这不冒充远端连接成功。
- 自动化：`tests/test_sap_workbench.py` 26 passed；相关后端子集（workbench/route/scene-original/coding/route-registry/web-chat）142 passed；`node --test tests/test_sap_workbench_frontend.cjs tests/test_coding_frontend.cjs` 37 passed。

保留的测试配置数据（供后续联调复用，未删除）：`scenes/sap_workbench.sqlite3` 的 `cj-sap_workbench-configs` 中租户 `tnt_EA3qM-lHPLD8ZPwW` 一行，`version=2`，`enabled/automation_enabled/commit_enabled=false`，`coding_agent_id=sap`，SAP `system_id=S4H`、`client=200`、`language=ZH`、`login_mode=password`，`web_gui_url` 为测试系统地址，`allowed_origins` 为其来源，`browser_service_ref=sap-browser-worker`。该行只含非秘密字段；SAP 密码未持久化。`opencode` 分项显示「未配置」是因为本机 `config.json` 仍无 `opencode` 块，本轮未新增该块。

## 卡片「配置」入口直达连接配置（2026-10-03，内置浏览器实测）

用户要求把场景卡片上的分类标签位改成「配置」，点击后打开当前的工作台弹窗。落入本次 change 的实现：

- `Scene/sap_workbench/scene.json` 声明 `card_action: "configure"`；目录数据按原样透传该字段（`Scene/catalog.py::read_catalog` 不裁剪场景字段）。
- `channel/web/static/js/scenes/index.js`：声明了配置入口的卡片底部用「配置」控件替换分类标签；卡片随之从 `<button>` 改为带 `role="button"` 的 `<div>`（`<button>` 不允许嵌套按钮），并补 Enter/Space 激活。点击分发在卡片上用事件委托判断（`innerHTML` 生成的控件没有可供绑定的节点引用），命中「配置」时走配置分支，其余区域仍走原打开分支。
- 两个入口改为共用 `openSceneById(sceneId, action)`：准备与校验路径完全一致，只有最后一步分发不同。这一步是必需的——`window.SceneOriginal` 只在 `ensureWorkbenches()` 之后才存在，若「配置」入口只等 `ensureRegistry()`，首次点击会因 `configure` 尚未定义而**静默退化**为普通打开。
- `Scene/_shared/frontend/adapter.js` 新增 `configure(scene)`：`sap_workbench` 调 `window.SapWorkbench.open({view: 'settings'})`，其余场景回落到 `open(scene)`。
- `Scene/sap_workbench/frontend/workbench.js` 的 `open(options)` 接受 `{view: 'settings'}`，在加载完配置后落到连接表单；`can_manage` 为假时不落表单。三语文案新增 `scenes_configure`。

真实浏览器复验（`test15` 租户管理员，`http://localhost:9899/chat`）：全新加载页面 → 侧栏「场景应用」→ SAP 工作台卡片底部为「配置」（卡片为 `DIV[role=button]`，`hasButton: true`，`label: "配置"`，无「数据」标签）→ 点击「配置」→ `#sap-workbench-dialog` 打开且 `open: true`、`main` 隐藏、`form` 可见、焦点落在 `system_id`、表单含 15 个具名字段。此前未改分发的版本在同一路径下会打开工作台主视图（`main` 可见、`form` 隐藏），已据此定位并修复。

自动化：`node --test tests/test_scenes_frontend.cjs tests/test_sap_workbench_frontend.cjs` 24 项中 23 项通过；新增 4 项覆盖「配置」控件渲染、点击只走配置分发（不 activate、不建会话）、无配置入口场景不出现该控件、以及管理员落表单 / 普通使用者不落表单。`tests/test_sap_workbench.py tests/test_route_registry.py tests/test_scene_original_runtime.py` 共 64 项通过。`tests/test_scene_skills.py` 的 SAP 三个资源摘要已随改动更新，剩余 6 项 `finance_report_audit`/`procurement_tender` 摘要不一致为既有问题，改动前后一致。

未通过项（非本次引入）：`tests/test_scenes_frontend.cjs::a workbench scene with a registered renderer dispatches instead of activating`。用 `git stash` 回退 `scenes/index.js` 到 HEAD 后同一条用例同样失败，确认为既有缺陷，未在本轮顺手修改。

测试用配置数据继续保留（`scenes/sap_workbench.sqlite3`，租户 `tnt_EA3qM-lHPLD8ZPwW`，`version=2`）。

## SAP MCP 配置补齐（2026-10-03，Google Chrome 实测）

用户指定使用 `rsmcode/sap-connect`，并提供两个已运行端点。本轮复用现有进程，未重复部署，未修改程序代码：

- `sap-abap`：`http://127.0.0.1:8100/mcp`，进程工作目录为 `rsmcode/sap-connect/sap-abap`。真实 MCP `initialize` / `tools/list` 成功，服务版本 `0.1.0`，返回 68 个工具（含 `sap_connect` / `sap_disconnect` / `sap_whoami`）。
- `sap-pyrfc`：`http://127.0.0.1:8200/mcp`，进程工作目录为 `rsmcode/sap-connect/sap-pyrfc`。同样握手成功，服务名 `sap-pyrfc-mcp`，版本 `1.29.0`，返回 8 个工具。
- 本轮 OpenCode API `http://127.0.0.1:4096/global/health` 返回 200 / healthy，Web `http://127.0.0.1:3000` 返回 200，Chrome 中可见现有 `sapwork` 项目。上述端口是本轮实际运行值，替代此前临时联调端口。

在 Chrome 的真实租户管理员会话中通过「SAP 工作台 → 连接配置」保存：系统 `S4H` / Client `200` / `ZH`；SAP URL 沿用测试系统地址；允许来源修正为 `https://sap.goodsap.cn:44300`；coding 引用为现有「SAP 智能助手」，项目目录 `/Users/jiantan/ai_assistant/sapwork/`，OpenCode Web 为 `http://127.0.0.1:3000`，浏览器节点 `local`、并发 4、空闲回收 900 秒。

新增上述两条启用的远程 HTTP MCP 配置。两网关的传输层实际无需额外认证，因此选择「无额外认证」；SAP 业务凭据来源仍为表单固定的 `sap_session`，没有新增独立 SAP 账号密码。原有三个启用意向开关保持原值，不代表运行能力通过验收。

真实 UI 验证：

1. 保存显示「配置已保存。运行功能仍需通过联调后开放。」；刷新状态并重新进入表单后，修正值和两条 MCP 连接均回显。
2. 将 Client 临时改为 `201`（URL 仍为 `200`）后保存，被「URL 中的 SAP Client / 语言与表单不一致。」拒绝；恢复 `200` 后保存成功，错误值未覆盖有效配置。
3. 保存截图：`/Users/jiantan/.codex/visualizations/2026/10/03/01a0ffc5-a61c-7101-8f15-a7c6b3d30292/sap-mcp-config-saved.png`。

本轮只验证配置持久化、OpenCode 可访问性和 MCP 协议握手；未调用 `sap_connect`、未执行模型对话或 SAP 业务操作。SAP 登录凭据复用桥、OpenCode 会话工具接入及同一 Web GUI 页面联动仍未完成，不能据本轮结果认定 G0/G2 通过。配置页「检查」仍只检查完整性，以上远端握手来自独立协议验证。没有把 SAP 密码写入配置、项目文件或此记录。

## 外置工具适配与同源登录消费组件（2026-10-03）

用户继续要求解决运行缺口后，新增 `opencode_adapter/host.ts` / `tools.ts`、`backend/mcp_login.py` 及独立依赖说明；没有改动 OpenCode、普通执行器或其他场景。本轮组件尚未挂入在线工作台，不能声称右侧已经能操作左侧。

- 外置 host 使用现有 OpenCode CLI HTTP 路由，与 canonical ApplicationTools 共享 memo map；真实创建 V2 会话并通过 prompt 执行场景工具。验证 bridge 中的 service/session/message/call 身份来自受信上下文，模型附带的 `session_id` 被参数投影移除。
- `/interrupt` 确实传播到场景 HTTP 工具，并向桥发出对应 call 的取消通知；取消不承诺撤销已经送达 SAP 的操作。没有注册保存/过账工具。
- 发现当前 OpenCode 首轮 prompt 会与 Location 配置初始化竞争；测试等待专用 agent 的权限规则就绪后，工具目录仅含允许的场景工具。上线接入仍须强制该门槛，单纯 health=200 不足以开始模型执行。
- MCP 登录消费者从同一可信登录密码构造 ADT/PyRFC 参数，显式设置 `insecure=false` / `tls_verify=true`；拒绝 soft ping 失败、身份不一致、旧登录代次、模型覆盖 connection_id/密码及未开放的写工具。部分失败时断开所有本次新建的连接，不保存密码。
- 后台使用默认受信 CA 访问登记 SAP 主机，TLS 返回 verify_code 20：`unable to get local issuer certificate`。已请求 CA 文件路径或服务器证书链修复；本轮未绕过校验、未使用真实 SAP 凭据。

验证：`bun test host.test.ts` **1 passed / 28 assertions**（真实 OpenCode HTTP/Session V2/工具/取消链路，模型供应商与 SAP 页面桥为测试替身）；Python 新 MCP 消费者及场景/浏览器/旧场景回归 **63 passed**。全部测试 OpenCode 数据位于临时目录并清理，未向用户现有对话写入测试消息。G0/G2 仍未通过，未勾选完整联调任务；当前 change 仍为 **9/57**。

## 固定 MCP、定向 TLS 例外及配置凭据（2026-10-03，最新要求）

用户明确调整：MCP 地址写死；仅这套测试 SAP 跳过后台证书校验；MCP 账号密码先在配置维护。这替代前一轮必须等待 SAP CA 和 Web GUI 同源登录的本阶段要求，不表示取消后续会话授权与页面绑定。

- `backend/deployment.py` 固定两个 loopback MCP 地址（8100/8200），配置页只读展示；配置 API 与登录消费者均拒绝换地址。测试 TLS 例外只匹配 `https://sap.goodsap.cn:44300`，分别设置 ADT `insecure=true`、PyRFC `tls_verify=false`；同主机其他端口、其他主机仍严格校验，未修改浏览器或全局 HTTP 信任。
- 新增 `mcp.username` 和独立只写密码字段；两网关使用 `scene_config`。服务端复用 `auth.crypto` 与部署主密钥，在新增的场景凭据表存 AES-256-GCM 密文，GET/PUT 只返回布尔状态。留空保留、替换、清除、目标/账号变化撤销、乐观锁与审计失败回滚均覆盖测试。普通成员无维护/回读权限。
- 只改 SAP 场景源码、其测试/说明和该场景资产摘要；另在 `.gitignore` 排除本场景数据库（现在包含密文凭据）及浏览器登录状态。未修改普通智能体、OpenCode 或 sap-connect 核心。旧工作区的其他改动保留。
- 本地后端按原环境重启，PID `94333`。Google Chrome 使用现有租户管理员会话进入「SAP 工作台 → 配置」，成功保存用户此前提供的测试账号密码；刷新状态后再次打开，显示「密码已设置」，密码输入框为空，两个固定端点仍启用。截图位于 `/Users/jiantan/.codex/visualizations/2026/10/03/01a0ffc5-a61c-7101-8f15-a7c6b3d30292/sap-mcp-fixed-config.png`。
- 真实验证从已保存配置解密凭据，通过已运行的 MCP 1.29.0 SDK 调用本场景消费者：两个端点均完成 initialize/list、sap_connect、sap_whoami 及只读 ADT discovery/healthcheck，结果均为 `ready=true`。秘密只在进程内和受限子进程 stdin 中传递，未放命令参数、文件或输出。临时连接在 finally 中断开；未调用模型、写表或提交 SAP 单据。
- 自动化：`tests/test_sap_workbench.py tests/test_sap_workbench_mcp_login.py tests/test_sap_workbench_browser.py tests/test_scene_original_runtime.py` **85 passed**；`node --test tests/test_sap_workbench_frontend.cjs` **15 passed**。

限制：配置页「检查」仍只检查完整性；上述真实探测来自独立验证脚本，不冒充在线自动联调。OpenCode 右侧对话和同一左侧页面的完整绑定仍未接通，自动操作/提交能力继续关闭。
