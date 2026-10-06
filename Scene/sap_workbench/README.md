# SAP智能工作台

## 当前口径：对话改为平台编码智能体入口（add-sap-workbench-coding-agent-entry）

场景页内对话不再自建自管 OpenCode 引擎，改为经平台既有编码智能体入口（`CodingSessionService`）创建/打开会话并挂载其嵌入地址；SAP 能力改由项目目录内的 skill（知识）与项目插件工具（能力）承载，工具每次执行由服务端按会话标识反查归属与授权。设计与任务见 [变更设计](../../openspec/changes/add-sap-workbench-coding-agent-entry/design.md)、[任务清单](../../openspec/changes/add-sap-workbench-coding-agent-entry/tasks.md)。

能力边界按实测如实标注（界面能力说明与本文档一致）：

| 能力 | 状态 | 依据 |
|---|---|---|
| 左侧画面打开事务码 | 有限可用 | 只保留一条服务端导航通道，只确认导航送达，不代表登录或业务结果 |
| 读取 SAP 业务数据 | 可用（凭据就绪时） | 场景中介通道 `sap_data_call` 的 `read_table` / `run_query`（仅单条只读 SELECT） |
| 经 BAPI 变更 SAP 业务数据 | 可用（凭据就绪时） | 同一通道的 `call_rfc`；可调用 BAPI，**可能改变业务数据**，需人工确认 |
| 读左侧页面 DOM / 填写页面字段 | 不可用 | 跨域 iframe，插件与 MCP 均读不到左侧画面 |

上表前两行与第三行的可用性都以配置中的 MCP 凭据就绪为前提：凭据缺失时界面与能力说明下发 `mcp_credentials_missing`，该行不标为可用。skill 已安装或工具已注册**不**构成页面读写可用的证据；左侧画面的读取与填写仍未通过真实工具路径验收，保持「不可用」。

中介通道只把 MCP 传输结果交回模型，**不等于**业务已完成：读到记录不代表单据已审批、已过账或未关闭，`call_rfc` 的传输成功也不代表 SAP 已完成该业务动作，模型不得据此声称已创建、已过账或已审批。连接、SAP 账号与口令始终留在服务端，参数面不接受 `connection_id`/`user`/`password`/`client`/`host`/`url`；系统变更类 ADT 工具（改源码、激活、传输放行）不在该通道的工具面内。工具可见性权限规则只减少误调用，**不是**授权依据——真实授权由服务端逐次校验归属实现。

自管引擎路径已退役但**未删除**：默认只有一个路径（平台编码入口），旧代码（`runtime.py` 非 iframe 分支、`prewarm.py`、`opencode_adapter/{server,host,native-host,credentials,context}.ts`）保留在回滚开关 `SAP_WORKBENCH_LEGACY_ENGINE` 之后（缺省关，只接受 `1/true/yes/on`）。旧库 `scenes/sap_workbench_runtime/` 与旧配置不删除，旧历史默认不迁移。走到旧分支时会写 warning 日志。见 [第 7 组证据](../../openspec/changes/add-sap-workbench-coding-agent-entry/evidence/group7-legacy-gate.md)。

**未达成项（不标为通过）**：共享标准实例的会话列表在未指定目录时返回全库会话，`sap-workbench-session-binding` 的「OpenCode 上游会话访问受控」要求本期**未达成**；SAP 现场验收维持归档状态，不因本变更标为通过。详见 design.md 的「已接受限制」「未完成与验收边界」。

以下 2026-10-04 起的各轮记录保留为历史，其中「自管引擎宿主 / 每绑定引擎 / 私有对话库」相关描述已不再对应当前路径。

2026-10-04 新修复：对话输入「打开me21n」「打开me23n」会调用 `sap_transaction_open`，在当前左侧 SAP iframe 导航，不另开页面。Chrome 实测分别显示创建采购订单及标准采购订单页面，标签页数均保持 14，原工作台与对话历史保留。只确认有限的交易 URL 导航，不提供跨域 DOM 读取/填写或保存能力。见 [本次导航验收](../../openspec/changes/fix-sap-workbench-iframe-navigation/evidence/verification.md)。

此前已按用户明确要求归档 `add-sap-workbench-scene`，实施任务完成 **49/64**，保留 **15 项未完成**。归档当时的对话导航测试未通过当前左侧 SAP iframe 验收；本次有限导航修复不把其他未完成范围标为完成。见 [归档记录](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/ARCHIVE.md) 与 [当时导航实测](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/chat-navigation-test.md)。

当前对话使用 OpenCode 原生全局/项目配置，保留全部模型、智能体、工具、MCP、技能、命令及原生权限交互；只把会话数据库保留在工作台私有目录，启动时从原生全局库只读继承模型 provider 登录，不复制历史或回写全局登录。四项 OpenSpec 技能及 `/opsx-*` 命令直接读取已有项目，不复制另一份。助手标题为「SAP智能助手」，同源嵌入时在应答框架自己的文档里裁掉 OpenCode 的「会话／更改」页签与内部空白标题条（不改动平台共享的 Web 构建；跨域嵌入保持 OpenCode 默认），标题栏使用独立的「全屏／还原」「收起对话」图标按钮，悬停显示说明，并保留键盘焦点和无障碍名称；支持左右拖动分隔线。展开、全屏和收起均保留两侧 iframe。场景自动控制/提交开关只管理场景桥，原生工具按 OpenCode 配置运行，不能用场景只读工具的历史验收证明原生工具的生产权限、审批或计量。当前证据见 [原生配置与全屏](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/native-config-fullscreen.md)。

2026-10-04 最新对话布局：右下角「AI 对话」展开页面内的右侧栏，与 SAP 左右并排并占满高度；收起后 SAP 恢复铺满。对话区域无悬浮圆角和阴影，窄屏改为上下排列。只修改场景 CSS 和资源摘要，展开/收起保留原 iframe、SAP 页面及正在生成的回复；当前 Chrome 页面已热更新样式，Web/桌面后端均已提供新 CSS。见 [页内对话布局](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/docked-chat-layout.md)。

2026-10-04 最新要求：SAP Web GUI 改为原生 iframe，直接占满工作区；右下角「AI 对话」展开嵌入式 OpenCode，不再新建独立 Chrome 或传输截图。新建会话先关闭当前租户、当前账号的旧工作台资源，保留会话历史；重复请求共用一次创建，被替换的旧请求不能恢复并抢占新会话。说明与当前验证见 [原生嵌入和会话替换](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/native-iframe-session-replacement.md)。

SAP 字段输入和点击由用户在原生页面操作。场景新增的 `sap_transaction_open({transaction})` 只从已保存 SAP URL 生成交易导航，父页面更新当前左侧 iframe 并确认接收；工具不读取其 DOM，也不声称已观察登录、页面标题或业务结果。通用 Playwright 管理另一浏览器，不能替代当前左侧导航。URL 重载可能要求重新登录或丢弃未保存输入，不保证 SAP 内部会话相同。SAP 的 cookie、登录、证书及嵌入限制由浏览器和 SAP 服务管理；iframe 不提供独立 Chrome profile，不移除响应头、不跳过浏览器证书检查，也不从页面提取账号密码。其他交易及 SSO/控件兼容仍需确认。

标题、状态栏、操作按钮及「SAP 页面」标题在会话视图中仍隐藏，配置和准备页保留入口。以下专属 Chrome 画面流、视口同步及工具测试记录属于切换前历史，不作为当前原生 iframe 的控制验收。旧记录见 [SAP 视口同步](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/responsive-sap-viewport.md)和 [接入评估](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/native-embed-assessment.md)。

2026-10-04 卡片入口更新：点击卡片上除「配置」按钮外的区域，会读取配置并直接新建工作台会话，无需再点一次新建；「配置」只打开表单。配置/能力未就绪时显示提示，连续点击不重复分配。相关前端 **96 pass**、入口资源路由 **1 pass**，详见 [卡片直接新建](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/card-create-session.md)。以下保留上一轮布局及部署记录。

2026-10-04 先前浮动界面：工作台会话以 SAP 页面为主，右下角「AI 对话」按钮展开嵌入式 OpenCode 面板，默认收起。收起只隐藏面板，保留同一个 iframe、草稿和正在生成的回复；重新展开不创建会话或切换控制权。相关前端 **90 pass**、入口/资源路由 **1 pass**，Chrome 桌面和 390px 窄屏使用隔离布局数据验证；Web/桌面后端已保留原环境重载。本次只改场景前端及资源摘要，实际 SAP/OpenCode 操作验收继续暂缓，完整计划仍为 **42/57，剩余 15 项**。见 [浮动对话布局](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/floating-chat-layout.md)。以下各轮结果保留为历史。

2026-10-04 本轮补齐固定 Debian 基础镜像的独立 OCI 下载/校验工具。amd64、arm64 官方原 payload 已实际下载、校验并成功导入 Docker，原 Dockerfile 推进到 APT；软件源连接失败，沿用既有 VM 代理的单次构建返回 502，显示镜像仍未构建成功。部署/清理相关组合 **90 pass，0.45 秒**；上一轮完整 SAP **1182 pass / 2 skip、Node 170 pass** 保留为历史，本轮未重跑全套或重启主服务，15 份已加载源码摘要未变。完整计划仍为 **42/57，剩余 15 项**，实际 SAP/OpenCode 操作检查继续暂缓，提交未开放。最新证据见 [基础镜像恢复](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/oci-base-recovery.md)；上一轮修复与重载见 [MCP/DOM 收尾](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/mcp-dom-closeout.md)。

2026-10-04 上一轮交付新增 `sap_purchase_order_read`：按十位单据号，通过本绑定 MCP 读取标准 NB 物料订单的有界 EKKO/EKPO/EKET 投影，拒绝截断或不完整结果；不证明完整业务单据或此前提交成功。字段填写新增编辑器/grid cell 的 `aria-invalid` 拒绝判定。完整 SAP Python **1123 pass / 2 skip，92.25 秒**、相关 Node **165 pass**、Bun **4 pass / 81 assertions，1.99 秒**；两个后端已保留完整原环境重载，18 项公共资源及匿名 API 检查通过。计划保持 **42/57，剩余 15 项**，实际 SAP/OpenCode 操作检查继续暂缓，提交适配器仍未注册。该轮证据见 [采购订单只读收尾](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/purchase-order-read-closeout.md)。以下合同复核及其 895/161/3、重载快照保留为上一轮记录。

2026-10-04 上一轮合同复核：4.1 按独立部署组件提供、版本固定及主服务无在线依赖的原合同完成，计划为 **42/57，剩余 15 项**。完整 SAP Python 第二轮 **895 pass / 2 skip，91.57 秒**，相关 Node **161 pass**，Bun host **3 pass / 68 assertions**；首轮进程组清理失败及修复链路保留。Web 与桌面后端已保留完整原环境加载本轮源码，18 项公共 HTTP/静态资源及匿名拒绝检查通过，未进行新的 Chrome UI 或 SAP/OpenCode 操作检查。Linux 单元的 `runtime_verified` 和 `workbench_binding_verified` 仍为 `false`，远程显示未登记为可用节点；4.3/4.4/4.7 的真实运行验收保留。不扩建权限或统一登录，不开放业务提交。最新范围与重载证据见 [本轮合同收尾](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/next-contract-closeout.md)；以下 859、旧重载和现场记录属于上一阶段。

入口：**场景应用 → 工具 → SAP智能工作台**。当前 SAP 主区域是直接加载已保存 URL 的 iframe，右下角浮动面板是原生 OpenCode Web。没有新增智能体类型，也不修改普通 Agent 执行器或 OpenCode 核心。

配置、连接检查、新建/恢复、嵌入式对话及 SAP MCP 通道继续保留。此前的画面流、人工接管及页面工具实现留在场景目录作为历史实现，当前 iframe 会话明确拒绝这些工具，不启动隐藏 Chrome。**页面**业务保存/过账/删除仍未开放；需要变更业务数据时走 `sap_data_call` 的 `call_rfc`（BAPI），同样只回报传输结果，不宣称业务已完成。OpenCode host 仍面向本机 Web/桌面工作台，远程网关尚未验收。

2026-10-04 上一收尾：完成 3.2 的 API/目标登记合同复核，补齐已观察供应商 F4 搜索弹窗的打开/关闭识别，查询/选值仍拒绝；上一轮会话参数、总容量和退出清理修复均包含在完整 SAP Python 回归 **859 pass / 1 skip** 中。Web 与桌面后端在该轮沿原环境重新加载当时最新代码，保留数据根、密钥和历史。Chrome 中工作台可打开并读回已保存配置；桌面仍显示正常登录入口。按用户安排，实际 SAP/OpenCode 操作检查继续暂缓，提交保持关闭。当时计划为 **41/57，剩余 16 项**，完整条件、该轮运行快照与截图见 [后续收尾记录](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/incremental-closeout.md)。

上一继续阶段的真实 MCP/ADT、审批消费者、持久 unknown 恢复及 Linux 显示单元证据保留在 [继续交付记录](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/resumed-delivery.md) 与 [Linux 部署说明](deployment/BROWSER.md)。实际 SAP 保存执行器和完整业务核验器尚未注册，审批或配置开关不能开放提交；历史现场结果不作为本轮新补丁的现场重测。

`sap_purchase_order_read(document_number)` 查询已有订单，只接受非全零的十位数字字符串，使用场景 Client 和当前绑定的 MCP；模型不能指定 SQL、expected 或身份。两轮三表 count/read/count 共 18 次，只返回完整支持投影，最长读取 90 秒、工具超时 110 秒；模型输出超过 32,000 UTF-8 字节则拒绝。标准 NB 库存物料、相同订单/计价单位和已列两位币种之外的形状拒绝。价格条件、税额、伙伴、完整 GUI expected 和 TCURX 未核验；依次读取不是事务快照，`business_validated`、`complete_business_document`、`submission_authority` 始终为 `false`。查询未找到不能证明此前未保存，不自动重存；私有 `.compare` 不接入提交消费者。

## 使用

1. 点击卡片「配置」，选择现有 SAP coding 配置，确认项目目录，保存 SAP URL、Client、语言及 MCP 账号密码；勾选「可视工作台」。「自动页面操作」不能开启跨域 iframe 控制，「业务提交」保持关闭；仅保存地址和密码不会启用会话。
2. 「测试已保存连接」检查本机 Chrome、OpenCode 运行依赖、Web 入口资源、项目目录和 MCP Python，并分别检测 SAP HTTPS、OpenCode API 与两个 MCP 的真实 SAP 登录。本机文件检查和联网检测显示为独立分项；后台 HTTPS 证书结果独立于 Chrome。
3. 点击 SAP智能工作台卡片上除「配置」以外的区域，先关闭当前账号旧工作台资源，再创建新会话并进入 SAP iframe；新建/已连接期间重复点击不重复分配。旧会话历史保留，但被替换的绑定不可恢复；「恢复已有会话」选择尚未被替换的绑定。打开卡片不调用模型。
4. 在嵌入的 SAP 页面登录。证书须由使用者在当前浏览器中处理；iframe 不会解决证书错误。SAP 若在登录后返回禁止嵌入的响应头，或 SSO/第三方 cookie 不兼容，仍可能阻止页面加载。
5. 点击右下角「AI 对话」展开 OpenCode；收起或再次展开保留同一个 iframe、草稿和回复，不调用模型。输入「打开me21n」或「打开me23n」，助手使用工作台工具在当前左侧导航；SAP 字段由用户输入和点击。对话保留 OpenCode 原生模型、智能体、工具、MCP 和技能配置。点击助手标题栏「全屏」铺满内容视口，再点击「还原」回到并排布局。拖动中间分隔线调整两侧宽度，也支持左右方向键微调和 Home/End 边界定位；全屏还原保留宽度。
6. 助手全屏时在宿主按 Escape 先还原，随后收起，再次关闭视图。关闭视图卸载 SAP iframe，保留 OpenCode 历史；新建会话会回收旧 host。iframe 内键盘由 SAP/OpenCode 处理，不跨域截获。配置通过场景卡片的「配置」入口维护。

## 配置归属

| 设置 | 位置 |
| --- | --- |
| SAP URL、系统、Client、语言、允许来源 | 场景配置；URL 不放凭据 |
| OpenCode API/Web 地址、服务认证 | 平台已有 OpenCode 配置 |
| 项目目录 | 所选 coding 的 `coding_project_dir`；不复制项目 |
| 浏览器执行节点 | 下拉选择本机专属浏览器，登记值为 `sap-browser-worker`；已有 `local` 配置继续可用，未知节点不能启动会话 |
| MCP 端点 | 固定 `sap-abap=http://127.0.0.1:8110/mcp`（8100 被 weknora-lite 占用）、`sap-pyrfc=http://127.0.0.1:8200/mcp` |
| MCP SAP 账号密码 | 场景配置；密码独立加密，响应只显示是否已配置 |
| OpenCode 项目 MCP | 按用户授权在项目 `opencode.json` 登记端点，不写 SAP 密码 |
| 租户容量与空闲回收 | 场景配置，默认 4 会话、900 秒 |
| 执行进程浏览器总上限 | 环境项 `SAP_WORKBENCH_NODE_MAX_SESSIONS`，默认 4、允许 1..32；所有租户合计，启动和关闭中继续占额度；Web 与桌面分别计数 |
| 远程显示 | 环境项 `SAP_WORKBENCH_PUBLIC_BASE`，一个 https origin（无 path/query/fragment），形如 `https://rd.rsmxm.com.cn`；未设置即仅本机，为默认。所有会话共享该 origin 下的一个固定路径前缀 `SAP_WORKBENCH_BASE_PATH`（默认 `/sapcode`），完整入口为 `<origin>/sapcode`，不新增域名、不新增 DNS 或证书。公网入口以 `SAP_WORKBENCH_PROXY_PORT`（默认 9911）接入同一回环端口 |

MCP 桥注入系统、Client、凭据和连接 ID，模型工具不接受这些身份参数。Web GUI 单独登录，不从页面提取密码。密码留空保留、输入替换、勾选清除则撤销；需已有 `COW_CREDENTIAL_MASTER_KEY`，不降级明文。OpenCode 服务密码与 SAP 密码用途不同。

远程显示默认关闭，需要显式配置才开启，且必须是 https origin（无 query/fragment/path）；路径前缀由 `SAP_WORKBENCH_BASE_PATH` 单独提供、绝不出现在 origin 中。配置不可用时在监听建立前失败，不启动半开的入口。开启后会话 runtime 仍只绑定 `127.0.0.1` 临时端口，由同一事件循环内的入口代理（回环 `SAP_WORKBENCH_PROXY_PORT`，默认 9911）解析到唯一**在会话**：引导阶段用显式 `?binding=` 查询，之后用会话 Cookie `sap_scene_<binding>`（路径固定为 base_path）；代理把带前缀的路径原样中转、不剥前缀，会话 runtime 自行剥掉 base_path 后路由。nginx 把 `location ^~ /sapcode/` 指向 `127.0.0.1:9911` 并透传 `Upgrade`/`Connection`、关闭 buffering/cache。HTTP、SSE 事件流和 WebSocket 升级都按字节中转。代理只监听回环、不额外鉴权、不做目录列举，未知或已关闭的 binding 返回 `unknown_session`；runtime 的会话 Cookie、Origin 校验、平台鉴权和 `frame-ancestors` 策略原样透传。所有会话共享同一 origin + 同一前缀、靠会话 Cookie（及引导的显式 binding）区分，与控制台同源（不新增域名/DNS/证书）。静态资源以 base=`/sapcode` 构建，入口 `<origin>/sapcode/` 返回 index.html，`/sapcode/assets/*` 与根级 favicon/manifest/主题预载脚本由 runtime 直接提供。左侧 SAP 画面由浏览器直接加载公网 SAP 地址，不经过该代理；托管画面（`screen`）模式当前被会话创建固定为 `iframe`，不在远程显示范围内。本机 `http://127.0.0.1:<port>` 路径不受影响。

仅用户指定的测试源 `https://sap.goodsap.cn:44300` 在 **MCP → SAP** 后台连接中跳过 TLS 校验；不更改 Chrome、系统证书或全局 HTTP 校验。无秘密载荷示例见 `config.example.json`，通过 UI 保存，不手改 SQLite。

## 运行依赖

- OpenCode 历史源码记录 `0442518883`，后续 Web 观察为 1.18.34，Bun 1.3.14；历史提交自身的 Web package 是 1.18.31，不能拼成同一次发布锁。当前源码为 `9acdb1d09f`，26 个 host 引用的静态接缝未变化；当前来源的 canonical host 最新隔离测试已通过 4 项 / 81 个断言，完整 SAP 主题、SSO 和远程兼容仍待验收。`SAP_OPENCODE_ROOT` 缺省为相邻 `rsmcode/opencode`，详情见 [兼容基线](deployment/README.md)。
- 从 OpenCode `packages/app` 执行 `bun run build --outDir <平台数据根/scenes/sap_workbench_assets 的绝对路径>`。首次构建到空目录，不覆盖其他应用资源。资源路径以每个后端的数据根为准：源码 Web 缺省为 `<rsmagent>/scenes/sap_workbench_assets`；设置 `COW_DATA_DIR="$HOME/.cow"` 的桌面端使用 `$HOME/.cow/scenes/sap_workbench_assets`。可以复用同一次完整静态构建：先复制到目标旁的临时目录，逐文件核对后再重命名到尚不存在的目标；目标已存在时先核对版本，不覆盖。只复制该静态目录，不复制配置、数据库、runtime 或 Chrome profile。
- 已运行的 OpenCode API 提供当前模型配置；场景按需启动独立 Bun host、模型转发及 UI 网关，绑定 loopback 临时端口。
- 系统 Chrome。既有隔离浏览器验证使用 Chrome 154.0.8037.95、macOS 26.4、Python 3.14.3、aiohttp 3.14.3；未冻结系统 Chrome 自动升级，其他版本需重新验收。`SAP_WORKBENCH_CHROME` 可指定绝对执行文件路径，离线检查与浏览器 runner 使用同一选择逻辑；显式指定但不可执行时会报告失败。`SAP_WORKBENCH_HEADLESS=0` 用于本机人工登录/证书；默认无头模式。配置文件独立于日常 Chrome。
- 两个 sap-connect MCP 网关已启动。`SAP_MCP_PYTHON` 可指定安装 `requirements-runtime.txt` 的 Python，缺省使用相邻 `sap-connect/sap-pyrfc/.venv/bin/python`。
- 主服务按原方式启动。缺少运行依赖时仍可打开配置页，不影响普通聊天。

当前 canonical host 在主后端所在本机执行，项目目录按该机器的文件系统检查。OpenCode API 地址可用于读取模型配置和检测健康，但其健康响应不能证明另一台机器上的项目目录可用。本机检查结果不会作为远程执行节点的目录验收；远程执行节点仍待实现，远程显示已按上文的固定 origin + `/sapcode` 路径前缀与回环入口代理实现并通过隔离测试，公网入口经 nginx `location ^~ /sapcode/` 与真实浏览器现场验收已完成（引导 303 + 会话 Cookie → index.html 全链路可用）。旧配置中未登记的节点可进入配置页修复，新保存和会话启动会拒绝未知节点。执行进程总上限由服务端环境提供，客户端和租户配置不能抬高它；现有浏览器重连和同槽位重建不重复占额度。这是保守的默认上限，真实负载未测，不作为已测容量建议。

现有 Web 构建已完整准备到桌面独立资源目录，1787 个文件逐一一致，桌面数据根的五项离线检查通过，见 [桌面资源准备](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/desktop-assets.md)。本轮再次沿原桌面生命周期重载后端并核对公共资源，尚未完成桌面正常登录后的双栏验收。

可在项目根目录先执行离线检查：

```sh
.venv/bin/python -B -m Scene.sap_workbench.backend.environment \
  --data-root /path/to/platform-data \
  --project /path/to/sap-project \
  --browser-ref sap-browser-worker
```

命令输出 `offline_local_environment` JSON，逐项显示 Chrome 执行文件、Bun/OpenCode 源码和依赖目录、Web 入口文件、项目绝对目录、MCP Python 的检查结果；全部通过返回 0，否则返回 1。它不启动 Chrome、OpenCode 或 MCP，不访问网络，也不创建目录或数据库；`-B` 禁止生成 Python 字节码缓存。Chrome 探测不导入主应用日志或配置模块，冷启动隔离测试确认未创建日志、数据、项目或 runtime 目录。检查仅覆盖存在性、执行/目录访问权限和 HTML 引用的入口文件，不递归验证全部构建块，不证明软件版本兼容、浏览器证书可信、模型可调用或 SAP/MCP 登录成功。MCP Python 缺失会在检测中显示失败，但不阻止不使用 MCP 的画面会话。

连接探测有总时限：两个 HTTP 检测各最多 10 秒，MCP 初始化最多 50 秒，worker 正常关闭最多 12 秒、强制结束后等待最多 2 秒，均处于 HTTP 请求的 90 秒预算内。两个 MCP disconnect 并行执行、各最多 5 秒，随后在原任务中退出连接栈、最多 5 秒，以保持 AnyIO 取消作用域的任务归属。完整 host 启动最多 70 秒；超时会回收启动资源，保留原绑定及固定错误码供重试，不把未知状态当作成功。

连接清理采用同任务 deadline，兼容实际 MCP worker 的 Python 3.10，不新增 `async_timeout` 依赖。网关 `submit` 到期会取消外层请求，已登记且被 `shield` 保护的分配或清理仍归 runtime 管理，重试可核对原绑定继续恢复。实际 worker 的正常 AnyIO 退出、慢退出触发 deadline 和超时后继续执行均通过离线检查，未连接真实网关。

可执行 `.venv/bin/python -B -m Scene.sap_workbench.backend.compatibility` 只读核对十项版本元数据，不启动软件或访问网络。当前九项匹配，OpenCode 来源漂移一项，命令据实返回 1；报告显式标记不是完整发布锁，也未接入运行时阻断。当前来源 host 隔离运行已有独立证据；SSO、主题/内核 patch、远程显示仍未验收。

配置密码轮换/清除、账号/目标/Client 改变、平台注销或撤权后，旧 MCP consumer 立即有界关闭；读取途中发生变更则丢弃旧结果，旧绑定不会自动消费新凭据。同项目两个平台用户使用独立 worker 和私有连接 ID，模型不能提供连接 ID。网关 `whoami` 仅能核对连接 registry 元数据，返回明确标记系统 SID 为 `unverified`，不能把配置中的系统标识当作 SAP 实测值，见 [MCP 生命周期记录](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/mcp-lifecycle.md)。

## 数据、恢复与回退

数据根的 `scenes/sap_workbench.sqlite3` 保存配置、凭据、绑定及动作状态；原生历史在 `scenes/sap_workbench_runtime/<binding>/opencode.db`。公共聊天/身份表不迁移，runtime、浏览器 profile 和构建资源排除 Git。

平台重启可恢复同一历史，自动控制需重新开启。浏览器丢失后重建，不重放未知动作。断流重新申请令牌；配置版本变化后旧绑定停止，新建会话应用新目标。容量包含启动中的 host 和 Chrome；第二个画面连接不会替换正在使用的浏览器。断流后保留短暂重连窗口，超时回收。状态轮询不延长空闲租约，自动暂停/登录过期状态通过画面连接更新到界面。

会话创建阶段写入场景表的 `allocation_stage`：`reserved` → `host_starting` → `conversation_ready` → `browser_starting` → `ready`。对话启动后、画面连接前不会标为全部就绪；浏览器分配失败保留原对话，重试只补齐浏览器。原会话读取只有明确 404 才触发使用原 ID 的创建；超时、拒绝或异常响应不当作会话不存在。恢复核对远端 ID、agent 与项目目录，失败码仅保存固定值。旧数据库通过幂等增列迁移保留配置、历史和原 ID。画面握手中断会释放连接占用，让重连继续采用原浏览器。

平台登录过期或权限失效时，工作台停止重连并提示重新登录；登录后恢复同一会话。结束操作会等待资源清理，停用场景后仍可结束自己的会话；请求中断后重试不会启动第二个 host。配置页只列出当前租户可用的 coding 配置，其他租户的原配置不会自动复制过来。连接检测显示分项 HTTP 状态及明确原因（认证失败、项目目录不存在、界面资源或 MCP 工具缺失），不会显示网关原始异常中的秘密。MCP 子进程断开后会清理旧连接，下次调用重新初始化。旧受管画面模式切换 OpenCode 会话时解除旧自动绑定；原生 iframe 模式保留 SAP 页面及专属实例内的原生导航。

左侧输入通过真实文本框接收中文组合输入和粘贴，再传入专属 SAP 浏览器；发送后立即清空本地文本框。点击 SAP 画面即可输入，Ctrl/Cmd+V 不会被转发为远端剪贴板快捷键。导航等待命令被消费及页面变化，填写等待同一页面和业务字段的值连续两次回读一致；`business_validated: false` 表示仅验证页面值，不代表 SAP 业务校验或保存成功。自动填写最大 300 个 UTF-16 单元，保证值可完整回读；更长文本在输入前拒绝，使用人工录入，不能截断用户文本。编辑器或所属 grid cell 的 `aria-invalid` 为 `true`、`grammar`、`spelling` 或未知值时，值相等也不能通过填写接受判定；无标记保留 `unavailable`，不推断业务校验通过。失败消息会提示重新读取、人工接管或检查连接，不能把未观察到变化当作成功。

首批自动动作仅在登记的 SPRO / ME21N 页面形状中开放。导航要求最新 revision，并在 Enter 前重新核对代次、命令值、焦点链和无模态；已修改或不确定输入禁止自动离页。填写、参考 IMG、树节点、F4、选值、关闭帮助及 Check 共用效果登记，回读各自目标状态。F4 / 树键也核对焦点；只归属符合帮助标题基线的唯一弹窗，选值需要显式 option.value 并写回原字段。未知帮助、未知页签、维护页面、保存/过账/暂存/保留及原始键/脚本工具均不开放。更严格的控件规则本轮尚未真实 SAP 重测，见 [动作效果登记](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/action-effects.md)。

`sap_page_read` 还返回有界表格 ID/视口摘要；可用 `sap_page_scroll(revision, table, direction)` 请求固定四方向滚动，寻找后续行列。仅 ME21N 的已观察 grid 支持该入口，派发点避开编辑器/覆盖层，并按方向回读同一表格的具体变化；未知虚拟控件、无安全点或未观察到变化时停止，完整分页和实际兼容性待验收，见 [表格滚动](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/table-scroll.md)。桥请求在副作用前验证输入，解析/排队/输入派发前复验绑定；配额/审计提前失败不会遗留运行中动作，见 [桥边界](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/bridge-boundary.md)。

停用或回退前先暂停、结束活动工作台，保留数据库和审计。不要删除 SAP 单据、项目或登录数据来修复连接。提交审批与未知状态记录已有实现，真实 SAP 保存执行器和完整业务核验尚未注册，不宣称能回滚已送达操作。

源码桌面端与 Web 后端并行运行时应使用不同数据目录：Web 使用项目默认目录，桌面启动设置 `COW_DATA_DIR="$HOME/.cow"`，由桌面管理其 9876 端口，Web 继续使用 9899。两者有各自配置及登录态；不能把桌面成功启动到登录页视为桌面 SAP 双栏已验收。

## 加载最新场景代码

最新浮动对话前端已在 Web 9899 和桌面后端 9876 重新加载；原环境、主密钥、配置和原生历史保留。重载前通过正常界面暂停并关闭旧工作台视图，未保存 SAP 单据；恢复后新浏览器需要重新登录 SAP。18 项公共 HTTP/静态资源/匿名拒绝检查以及 Chrome 真实入口显示通过，见 [本次重载快照](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/floating-chat-services-reloaded.json)。以下为上一轮源码重载及通用操作说明。

以下是加载 Python 新源码时的步骤。本轮 Web 与桌面后端均沿原启动环境重载，保留数据根和主密钥；18 项公共 HTTP/静态资源及匿名拒绝检查通过，15 份相关源码摘要未变，见 [MCP 与 DOM 重载快照](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/mcp-dom-services-reloaded.json)。未创建工作台 runtime、调用真实 SAP/MCP/模型或进行新的 Chrome/OpenCode 页面操作检查；[采购订单只读重载快照](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/purchase-order-services-reloaded.json)、[合同收尾重载快照](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/next-contract-services-reloaded.json) 和 [更早重载快照](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/local-services-reloaded.json) 保留为历史。后续源码修改仍须重新加载：

1. 先核对尚未保存的 SAP 输入，暂停并结束活动工作台资源，保留配置数据库和 OpenCode 历史。
2. 用当前启动器的原环境重启对应 Python 后端，保留 `COW_DATA_DIR`、`COW_TENANT_BASE`、`COW_CREDENTIAL_MASTER_KEY` 和场景运行路径变量。主密钥必须保持原值，不能用新密钥替换已有加密凭据的密钥。
3. 已加载原启动环境时，源码 Web 可在项目根运行 `COW_WEB_PORT=9899 .venv/bin/python -B app.py`；源码桌面可在 `desktop` 目录运行 `COW_DATA_DIR="$HOME/.cow" npm run dev`，由桌面重新构建外壳并管理其后端。先停止原对应启动器，避免并行启动第二个同端口后端。仅修改 SAP 场景代码无需重建 OpenCode 核心；两个数据根各自的静态资源须已准备。
4. 后端重新就绪后完整刷新 Web 页面或重新打开桌面窗口，再从场景卡片进入并恢复原工作台历史。恢复时新建的场景 Bun host 才会载入最新 `tools.ts`；已有 host 不会因刷新 iframe 自动重载代码。自动控制需要重新明确开启，失败或未知动作不重播。

源码更新、静态资源复制和监听存在都不能单独证明新 Python 代码已加载或通过现场兼容验收。本轮核对源码摘要、原环境及公共资源，未重新进行登录后的 UI 验收；上一收尾的 Chrome 配置回读保留为历史。专属 SAP 浏览器证书及桌面登录后的实际双栏仍保留原现场前置条件。

## 验证

MCP SDK 包装、管道容量及 F4 回读专项可独立执行下列命令。SDK 检查使用实际 `SAP_MCP_PYTHON` 解释器，在本轮完整套件中已运行；专项与全套重叠，不累加：

```sh
SAP_MCP_PYTHON=/path/to/sap-connect/sap-pyrfc/.venv/bin/python \
  .venv/bin/python -B -m pytest -q tests/test_sap_workbench_mcp_sdk_wrapper.py tests/test_sap_workbench_mcp_pipe.py tests/test_sap_workbench_help_readback.py
```

采购订单完整 JSON/桥边界专项可执行下列命令，上一轮四文件组合 260 passed；字段拒绝状态另由 `tests/test_sap_workbench_field_validation.py` 覆盖：

```sh
.venv/bin/python -B -m pytest -q tests/test_sap_workbench_purchase_order_bridge.py tests/test_sap_workbench_mcp_data.py tests/test_sap_workbench_bridge_boundary.py tests/test_sap_workbench_purchase_order.py
```

完整 SAP 隔离回归及相关 Node：

```sh
SAP_WORKBENCH_LIVE=0 SAP_WORKBENCH_UPSTREAM=0 SAP_MCP_PYTHON=/path/to/sap-connect/sap-pyrfc/.venv/bin/python \
  .venv/bin/python -B -m pytest -q -rs tests/test_sap_workbench*.py
node --test tests/test_sap_workbench_field_validation.cjs tests/test_sap_workbench_observations.cjs tests/test_sap_workbench_dom.cjs tests/test_sap_workbench_frontend.cjs tests/test_scenes_frontend.cjs tests/test_coding_frontend.cjs
```

以下 host 测试执行实际 OpenCode 运行模块。上一轮已在当前来源执行，4 项 / 81 个断言通过；本轮 Bun/TypeScript 未修改、未重跑，供应商和 SAP 桥是隔离替身：

```sh
SAP_OPENCODE_ROOT=/path/to/rsmcode/opencode bun test Scene/sap_workbench/opencode_adapter/host.test.ts Scene/sap_workbench/opencode_adapter/deployment.test.ts Scene/sap_workbench/opencode_adapter/purchase_order.test.ts
```

host 测试运行真实 Session V2 和未修改的 Web reducer，供应商和 SAP 桥使用替身。真实联调见 [验收记录](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/live-workbench.md)。原生 RFC SDK 未安装，sap-pyrfc 当前使用 ADT 回退，不能宣称 RFC 直连通过。

实际操作检查仍按用户安排暂缓。本轮完整 SAP Python **1182 pass / 2 skip**、Node **170 pass**，修复 MCP 响应/管道与 DOM/F4 回读，详见 [MCP 与 DOM 收尾](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/mcp-dom-closeout.md)。两个 skip 是暂停的真实 Chrome fixture 和默认不启用的固定上游下载 fixture；实际安装 SDK 的离线合同本轮没有跳过。Bun **4 pass / 81 assertions**是上一轮记录，隔离测试不替代现场验收。

上一轮独立实现与验证范围见 [上一轮合同收尾](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/next-contract-closeout.md)：提交摘要的局部截断边界拒绝、显示监督器停止与进程组失败隔离、页签/表格有界只读元数据均已验证；页签执行、完整分页和业务提交仍未开放。Python 第二轮 **895 pass / 2 skip**，跳过真实 Chrome fixture 和默认不启用的固定上游资源专项；Node **161 pass**，真实 canonical host 的隔离测试 **3 pass / 68 assertions，2.11 秒**。供应商、SAP 桥及 DOM 为受控 fixture，不代替现场。上一收尾的后端重载及入口核对见 [后续收尾记录](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/incremental-closeout.md)；[继续交付记录](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/resumed-delivery.md) 的真实只读联调、双栏和历史恢复是对应阶段证据。

上一收尾的 SAP Python 套件为 **859 pass / 1 skip，86.25 秒**（真实 Chrome fixture 未启动）。相关 Node 153 pass（70 项固定 DOM）、canonical host / 双 host 部署隔离 3 pass / 68 assertions 是对应阶段的运行结果。历史 493、747、819 的结果保留在各阶段证据，当时完整计划为 41/57；专项数量存在重叠，不相加，详情见 [验收矩阵](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/acceptance-matrix.md)。该轮 Web/桌面 Python 后端已重载；入口核对仅使用用户指定的浏览器接口，没有用 pytest fixture 绕开证书警告。

页面观察最多遍历 24 个文档，快照限制为 48000 个 JSON 字符，并标记已知的截断。MCP/host 恰好退出时不会打断剩余清理；关闭中的 Chrome 在实际退出前保留容量名额。场景网关首次成功启动才安装退出钩子，使用已有节点 worker 并行清理，兜底仅针对确切自有 child handle；55 秒为场景 stop 的共享等待预算，不是整个应用/操作系统的绝对退出上限，见 [退出清理证据](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/shutdown-cleanup.md)。

已观察的中文供应商 F4“限制值范围 (1)”窗口仅在完整 label/text、唯一弹窗、登记页面、同代次归属和焦点链满足时支持打开/读取/Escape 关闭；不因“集中删除标志”过滤标签误拒。七个标签不证明输入值为空，搜索、执行、结果选值与页签仍未开放，未知/修改形状拒绝，见 [F4 兼容证据](../../openspec/changes/archive/2026-10-04-add-sap-workbench-scene/evidence/f4-compatibility.md)。该补丁没有做真实 SAP 重测。

SAP Web GUI 的表格输入可能先以 CBS 占位控件显示，点击后才生成输入框。场景适配器依据当前单元格的可编辑标记、列标题和行号识别它，用真实鼠标事件激活同一单元格后填写，并独立回读值；不会填入其他获得焦点的输入框。模型页面工具返回精简观察，完整快照仍用于 revision 校验；`sap_page_read` 可用 `query` 按字段/控件名称片段查询、`row` 指定当前表格行号，例如 `{"query":"短文本","row":1}`、`{"query":"数量","row":1}`、`{"query":"检查"}`。普通读取有数量上限，不能靠重复同一读取获取遗漏的列；当前不宣称完整表格分页或所有 SAP 控件均已支持。

模型转发仅允许单次生成，输出上限 4096；异常请求在调用前拒绝，未知用量保留预留。当前共享配额接口使用调用时的时间桶，不能按原预留结算跨桶响应，因此场景不对跨桶结果执行退款，防止冲减新请求的用量。完整生产配额验收仍未完成，不修改公共身份模块来掩盖该限制。
