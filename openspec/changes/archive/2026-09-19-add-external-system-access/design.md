## Context

动机与交付范围见 [proposal.md](proposal.md)。交互参考 [已确认 HTML Demo](../../../docs/design/demos/external-system-access.html)；规范以本 change 的九份 delta spec 及既有授权规范为准。以下为本次只读代码核对所得，不代表外部环境联调或生产验收通过。

| 能力 | OneAgent 参考行为 | 本项目当前前端 | 本项目当前后端 | 本次处理 |
|---|---|---|---|---|
| MCP | 配置、三种传输、全局/租户覆盖、测试 | 无正式连接配置页 | ToolManager、MCP 客户端及 OAuth 已有；缺管理 API | 复用协议能力，补控制面与范围感知的解析 |
| ERP | 多连接、默认项、SAP 配置；U9/金蝶含模拟分支 | `Scene/_shared/frontend/config-panel-erp.html` 已有，未接主菜单 | ERP JSON API、SAP RFC/ADT、采购及分析场景已在 Scene 注册 | 收口至统一入口，迁移秘密与数据，保留场景引用 |
| OA | 租户单连接、账号及 OpenAPI、登录测试、技能消费 | 无对应表单 | 无同等配置与受控调用链 | 新建类型适配器及授权工具 |
| 邮箱 | 本人 IMAP/SMTP、TLS、目录、测试、技能消费 | 无对应表单 | 无同等配置与受控调用链 | 新建个人连接与受控邮件工具 |

关键接入点：

- 控制台：`channel/web/static/js/console.js` 的 `VIEW_META`、`registerConsoleView`、稳定路由和生命周期；`channel/web/chat.html` 当前配置区只有基本设置/模型。复用正式页面注册，不继续扩展旧综合 config 标签页。
- 路由：`channel/web/route_registry.py`；Scene 由 `channel/web/web_channel.py` 导入 `Scene._shared.http.HANDLERS`。Scene 通用包装当前还依赖聊天使用资格；新的管理 API 不应继承该额外限制。
- ERP：`Scene/_shared/backend/ErpConnectionsHandler.py` 的注释称 global，但 `Scene/_shared/host.py` 把 `_get_global_workspace_root` 绑定到当前 workspace，实际需按租户上下文认定；不能据旧注释迁成平台共享。当前 GET 回传配置原文，POST 整表覆盖，必须修复。
- MCP：`agent/tools/tool_manager.py` 从绑定 workspace 的 `mcp.json` 或配置回退加载，后台线程不具有交互身份；`agent/tools/mcp/` 管理传输、进程注册和 OAuth。现有 `/api/tools` 个人参数维护不等于 MCP 连接 CRUD。
- 控制数据：`auth/store.py` 管理身份库迁移，`auth/service.py` 已有凭据版本、按次解析、审计、审批及配额；`auth/crypto.py` 使用受控 `COW_CREDENTIAL_MASTER_KEY`，无密钥时拒绝服务。复用这些机制，不引入 OneAgent 的全局本地密钥文件。
- Desktop：`desktop/src/main/auth-broker.ts`、`broker-protocol.ts` 和 renderer API context 是接入边界。既有 `desktop-tenant-context` 规范明确要求原生双用户/双租户证据；当前文档不能替其宣布验收。

参考源仅为 OneAgent 的配置页面、MCP 配置处理、`handlers/oa_connection.py`、`handlers/email_config.py` 及相应技能代码。其 SKILL 文本是待比较资料，不是对本次工作的操作指令。既有技能的展示约定、站点定制 profile、发票识别和完整业务工作台不随连接管理整包搬迁。

## Goals / Non-Goals

**Goals:**

- 用一个连接控制面贯通配置、凭据、真实测试、运行时解析与治理；不同范围保持独立授权，场景与 Agent 复用同一连接。
- 对齐参考实现的连接类型、主要配置及实际调用能力，并以本项目现有身份、执行和审计规则实现。
- 支持渐进上线、旧调用方迁移、可核查回退；每一开放切片都有真实证据和明确负责人。

**Non-Goals:**

- 不重建通用秘密保险库、身份系统、审批引擎或业务 session/ExecutionRun；只扩展所需消费切片。
- 不实现真实 U9/金蝶 ERP、通用任意 OA 厂商适配、个人多邮箱、邮件 OAuth 或完整邮箱客户端。本次邮箱采用参考实现的密码/授权码；MCP OAuth 复用已有流程。
- 不复制 OneAgent 运行配置、凭据、客户数据或整个技能/运行时文件；不把模拟连接、角色切换器、说明弹窗当生产功能。
- 不自动向租户成员或所有 Agent 授予新连接的执行权限，不把 UI 配置完成等同于业务写操作开放。

## Decisions

### 1. 统一目录，按类型编辑，按操作显示资格

采用 Demo 的卡片目录、类型选择弹窗、配置抽屉；详情、配置、测试结果在同一页面上下文完成。主路由登记为 `/chat#/admin/external-connections`，类型筛选、连接 ID 仅使用已登记的无秘密路由参数；`/admin` 既有别名交由统一路由规范化。菜单放在「消息渠道」之后，classic/split 共用。

前端新增按需加载模块与独立样式/i18n 键，使用已有 AJAX/身份 context、能力投影和离页保护。页面资格由服务端合并当前获准范围得到；不以 `role === admin` 判断。不新增生产角色切换或页面专属主题入口。

卡片分开表达 `enabled`、配置来源、测试状态和运行能力。测试结果为未测试/测试中/成功/部分成功/失败/过期，运行不可用原因另外显示；启用不代表认证通过。列表统计与筛选仅针对可见连接，加载失败不是空态。已保存秘密只显示「已配置」，显示密码按钮仅作用于当前新输入。

弃选方案：继续在旧配置页堆四个标签页会保留重复入口，也难以表达多个 ERP/MCP 与个人范围；把全部连接做一个超长表单会失去类型校验与操作边界。

### 2. 控制库作为唯一数据源，类型适配器不拥有配置副本

在身份控制库增加连接表，由新的 `integrations/external/` 服务层维护。业务数据库、Agent workspace、浏览器与 Scene 不再存权威连接副本。新增模块名为实施建议，职责边界为约束。

| 数据 | 核心字段/约束 | 说明 |
|---|---|---|
| `external_connections` | id、kind、scope、tenant_id、owner_user_id、name、config_json、enabled、version、base_connection_id、deleted_at | config_json 仅非秘密；类型和归属创建后不可改 |
| `external_connection_secret_refs` | connection_id、slot、credential_id/平台秘密 ID、version | 两种秘密归属互斥；引用不能由客户端任意指定 |
| `external_connection_catalog_versions` | scope key、kind、revision、default_connection_id | ERP 默认/批量更新 CAS；默认必须属于同一租户且可用 |
| `external_connection_tenant_access` | platform_connection_id、tenant_id、enabled、revision | 平台 MCP 明确租户可用名单，默认不向新租户自动开放 |
| `external_connection_tests` | test_id、connection_id、actor、scope、版本组合、阶段/结果码、时间 | 仅脱敏摘要与限量工具元数据，不存原始请求/密码/正文 |
| `external_connection_migrations` | source_locator 摘要、source_hash、scope、旧标识映射、批次、结果 | 幂等导入、引用核对与审计，不复制源秘密 |

用数据库 CHECK/唯一索引与事务保证：平台范围只允许 MCP 且无 tenant/owner；租户 MCP/ERP/OA 必有 tenant 且无 owner；邮箱必有 tenant+owner；每租户 OA 一个、每租户用户邮箱一个、每租户平台 MCP 覆盖一个；软删除记录不占活动单例。ERP 默认由目录行唯一持有，切换先检查目录版本，不依赖前端取消其他默认。

租户/个人秘密复用 `credentials`、`credential_versions`。平台 MCP 采用独立 `platform_connection_secrets` 及版本表，复用 `auth.crypto`、审计和版本机制，避免给现有租户凭据表塞虚构 tenant 或允许任意 tenant=NULL 的泛化秘密。平台秘密不可从通用凭据 API 取出，只在已授权的平台连接调用点解析；租户覆盖必须提供自己的秘密。OAuth 刷新令牌同样按秘密处理。

创建/更新事务同时提交配置、秘密版本引用、目录版本和审计；失败整体回滚。秘密操作使用 `keep / replace / clear`，`replace` 才携带 value，禁止传掩码作为新值。源 URL 禁止 userinfo，常规参数禁止夹带 token；MCP 认证 headers 与 env 值默认按秘密存储，显式公共参数仅保留无敏感值。元数据长度/条数有上限。

弃选方案：每类型保留 JSON 会重复实现加密、事务与租户隔离，且无法可靠防止并发默认冲突；把平台秘密复制给各租户会破坏撤权与唯一归属。

### 3. API 共用服务，范围端点显式分开

统一返回 `{data, request_id}`；错误 `{error:{code,message,fields?}, request_id}`。详情只返回非秘密配置、秘密槽存在性、版本、来源、能力和获准 actions。401 为身份失效，403 为已知操作无资格；越界 ID 统一 404；400/422 为格式/字段校验；409 为版本、单例、引用或幂等冲突；429 为限流/配额；503 为依赖或部署切片未就绪。测试的业务失败返回测试对象中各阶段错误，不能只读 HTTP 200 就显示成功。

| API | 职责与条件 |
|---|---|
| `GET /api/external-connections/catalog` | 当前主体可见卡片与脱敏汇总，仅调用已获准范围；支持 kind/status/q/cursor |
| `GET /api/external-connections/types` | 各类型允许范围、真实 readiness、表单版本及操作能力；不回传秘密或内部路径 |
| `GET/POST /api/external-connections` | 当前租户 MCP/ERP/OA；POST 必须 Idempotency-Key |
| `GET/PATCH/DELETE /api/external-connections/{id}` | 租户对象管理；PATCH/DELETE 使用 If-Match |
| `POST /api/external-connections/test`、`/{id}/test` | 新配置草稿或已保存配置测试；后者可带 draft_patch，按版本解析 keep 槽 |
| `GET/PUT /api/external-connections/erp-default` | 读取/原子修改默认，PUT 使用目录 If-Match，null 表示确认清空 |
| `POST /api/external-connections/{platform_id}/override`、`/{platform_id}/restore` | 本租户覆盖/恢复，校验平台可用性和相关版本；继承卡片明确平台来源 |
| `/api/personal/external-connections` 及 `/{id}`、`/test`、`/{id}/test` | 与租户 CRUD/test 同形，只允许当前租户本人邮箱；没有 owner 参数 |
| `/api/platform/external-connections` 及 `/{id}`、`/test`、`/{id}/test` | 只允许平台 MCP，合法零租户平台管理员可管理；不能调用个人/租户工具 |
| `PUT /api/platform/external-connections/{id}/tenant-access` | 显式设置获准消费租户，使用版本条件并审计撤权 |

所有管理 mutation 接入现有认证、CSRF/来源与 management-write 策略。静态 `/catalog`、`/types`、`/test`、`/erp-default` 在 `/{id}` 路由前注册。统一目录只有卡片投影：租户用户查看平台继承项时不暴露命令、秘密 headers/env、完整内部服务细节，非秘密模板编辑投影亦由单独管理资格决定。

资源种类登记为 `external_connection`。建议权限键为 `external.connections.read/manage/test`、`personal.email.manage/test`、`platform.external_connections.read/manage/test`，结合资源对象授权与 capability slice。功能权限、对象范围、真实服务 readiness 三者共同决定菜单/按钮；保存不发放 `tool.execute` 或任何工具 grant。保留现有 `erp.connections.view/manage` 的**等价 ERP 范围**映射，不能借兼容权限获得 MCP/OA/邮箱权限。只拥有工具执行资格的主体通过运行时解析，不需要连接管理页资格。

创建幂等键限定 actor+scope+endpoint，有负载指纹；含秘密使用服务端 keyed fingerprint。短期幂等日志仅存脱敏结果，默认保留 24 小时。PATCH 超时后先读取版本核对，不盲重放；测试是有界请求，允许重试但不复用过期成功。所有消费配额按实际租户计费，平台管理测试走独立平台运维额度，不虚构租户。

仅保留本项目已有 ERP 兼容 API：`/api/erp/connections`、`/options` 委托新服务；GET 加版本和秘密存在性，POST 升级为目录版本条件事务。旧客户端无版本写入返回 409 `upgrade_required`。不存在的 OneAgent `/api/mcp`、OA、email 路由不为外观对齐而重复新增；正式前端使用上述统一服务接口。

### 4. 适配器契约与每次调用的授权边界

类型适配器提供 `validate_config`、`probe`、`describe_capabilities`、`invoke`，它们接受已由服务端构建的执行上下文，不自行从全局配置/环境猜租户。顺序为：确认真实主体与实际租户 → 解析连接和当前版本 → 验证 Agent/资源/动作范围 → 风险与审批 → 预留配额 → 在派发前再次核验状态 → 按需解析秘密 → 执行 → 记录脱敏结果并结算配额。新模块接入现有 ExecutionRun、取消和审计关联，不另造任务中心。

用户主体、获准机器主体、委托调度必须走原有独立身份规则；不创建 Membership 来绕过。运行时发现也携带由 Agent Registry/执行上下文建立的范围，而不是借用管理请求的临时 ContextVar。Channel 与 scheduler 只有在当前主体传递证据通过后才开放新类型；没有个人委托的机器主体不能发个人邮件。

认证 session/连接池键至少包含 scope、实际消费 tenant、owner（若有）、connection/effective id、配置版本、秘密版本和必要 policy revision。MCP 平台连接也不把含租户业务状态的会话跨租户复用。凭据明文不跨请求缓存；保持外部认证会话必须具备当前版本约束，停用/轮换/撤权通知主动关闭会话，派发前检查兜底。异步刷新只传秘密引用和授权上下文，不传长期明文配置。

拒绝采用“给所有 Bash 自动注入 OA/邮件环境变量”的参考方式。优先实现结构化工具与内部适配器；必要脚本只在经过隔离的专用 runner 中，以单次最小环境或管道传入凭据、固定可执行入口和参数 schema，执行后清理。未经执行隔离验收，不能退化为宿主机 shell。

### 5. 按类型的实现范围与参数

| 类型 | 配置与特殊行为 | 测试与运行接入 |
|---|---|---|
| MCP | `transport` 三选一；远程 url/认证/headers；stdio command/args/env；enabled；平台可用名单、来源和租户覆盖 | 复用 MCP 客户端及 OAuth，探测 handshake+tools/list；工具稳定 ID 与旧 `mcp:<server>:<tool>` 别名映射；不从名前缀判授权 |
| SAP RFC | provider=rfc、ashost、sysnr、client、user、passwd、lang，沿用实际 provider 所需参数 | SDK 就绪检查、登录及最小只读探测；复用采购同步/数据分析适配器，禁止任意 BAPI 名调用 |
| SAP ADT SQL | provider=adt_sql、base_url、client、user、passwd、verify_ssl、timeout | 真实认证及受限只读查询；网络策略和查询权限约束，保留现有场景输入限制 |
| OA | name/base_url/username/password、tenant_key/custom_page_config_id/app_key/app_secret/corp_id | 分离登录与 OpenAPI 就绪；结构化列表/详情/审批记录；流程创建、提交、退回和支持的转办类动作逐项登记 |
| 邮箱 | name；imap_host/port/user/pass/tls/reject_unauthorized/mailbox；smtp_host/port/user/pass/from/secure/reject_unauthorized；附件目录 | IMAP/SMTP 分别探测；结构化查询、读取、下载、标记、发送；SMTP 新增明确 `tls_mode` 映射旧 secure，支持 implicit_tls/starttls，禁止悄悄降级 |

OA 不假定所有 E9/E10 站点暴露相同接口。适配器报告能力和所需参数，待办/已办/我发起/抄送的完整分页与流程详情分别实现，门户 widget 不冒充全量列表。OpenAPI 未配置不阻止只依赖账号的合法查询，但对应写动作不展示为可用。业务表单 profile、发票 OCR 等站点扩展不作为连接配置必备，也不得用模拟结果补齐。

平台 MCP 覆盖采用完整有效配置快照而非逐字段合并：`base_connection_id` 固定、tenant-owned secrets 独立，平台模板更新不重写覆盖。有效可用性仍与平台源 enabled/tenant-access 相与。恢复继承删除覆盖并使旧池失效；平台源删除须处理获准范围内的引用和覆盖，不级联擦掉租户数据。目录的 `effective_id` 对同一平台逻辑连接稳定，另给实际 row id、source、version，不因创建覆盖导致重复卡片。工具授权映射绑定该逻辑身份且执行时仍验证实际配置/来源。

外部动作风险目录默认将 SMTP 发送、OA 流程创建/审批/退回/转办等列为高风险，消费现有单动作审批；标记已读/未读也登记为写动作并有独立动作资格，不隐含在 fetch 中。MCP 未分类工具不能信任服务端自报的 readonly hint，默认按高风险处理；管理员登记的风险分类须审计。SAP 本次保留已有只读/同步能力，不新增任意写 BAPI；未来写动作同样必须进入风险目录。

审批绑定实际主体、tenant、connection/version、动作与目标标识、规范化参数摘要和附件内容摘要；审批人/发起人分离遵循现有规则。外部动作返回超时后置为 outcome_unknown：有幂等协议则使用受控 idempotency key，否则先查远端状态或由操作者核对；不得自动再次发送/审批，不能承诺跨系统 exactly-once。

### 6. 有界测试、网络与附件策略

连接测试在受限工作池中同步返回，默认总超时 30 秒、单租户同时 2 个、全局同时 16 个，均可由部署策略收紧。HTTP 响应、工具发现数量、IMAP 探测结果有可配置上限；超时必须取消 I/O 并清理资源，不能只让前端停止转圈。SAP SDK 等不可中断调用使用可终止受控 worker；隔离/终止能力未就绪时该探测不开放，不能占满 Web 主进程。

已保存测试记录绑定 effective config revision、secret revisions 与 actor/scope；更新健康状态使用 CAS。草稿测试只在请求内组装秘密，响应后释放；草稿结果不写连接健康记录，保存成功后显示未测试，重新测试才更新卡片，避免保存与草稿测试竞态。测试中改表单即废弃 UI 结果；请求上下文 epoch 防止迟到响应污染另一租户。

目标地址策略允许管理员配置确有需要的企业内网域名/网段/端口；默认拒绝未许可 loopback、链路本地、云元数据和非法 scheme。验证初始地址、DNS 解析及每次重定向；固定认证 origin，不向跨源跳转转发认证头；SSRF 校验与实际连接使用同一解析结果或受控代理。TLS 校验默认启用；例外必须部署许可、显式配置且审计。

目录输入解析为获准工作区内的规范化根，工具执行时再次校验最终打开对象，防止 symlink/TOCTOU。附件经过大小/数量/总存储配额、文件名和内容类型处理；HTML 只以安全文本/净化内容呈现。日志不含秘密、邮件正文、审批正文或未经筛选的远端错误，使用 request_id/ExecutionRun/connection id 定位。

### 7. 兼容、开放开关与依赖证据

采用服务端类型切片开关：`external_connections.catalog`、每类型 `configure/test/read_execute/write_execute`，MCP 另有 `stdio_execute/oauth`，平台共享与各客户端另有 readiness。开关默认关闭，只有服务端依赖已验收才进入 capability projection；缺少证据时配置可保留但相关动作关闭。前端不得通过本地开关绕过。

| 门槛 | 前置与证据 | 可开放范围 |
|---|---|---|
| G0 基线 | 当前路径、权限键、旧连接/工具引用、数据归属、受影响客户端清单 | 开发和隔离测试，无生产开放 |
| G1 控制面 | 凭据加密/轮换/撤权、内部审计事务、范围授权、并发/幂等和泄漏负例通过 | 获准类型配置与目录；不含测试/执行 |
| G2 真实测试 | G1 + 目标策略、测试超时清理、配额；各适配器真实测试环境证据 | 按类型开放测试；stdio 另需 execution-isolation |
| G3 只读执行 | G2 + 动态工具授权、绑定 Agent、身份传递、配额及撤权/池失效证据 | 按类型/客户端开放获准只读工具与场景 |
| G4 业务写入 | G3 + action-approval、具体风险注册、动作摘要绑定、重复提交/结果未知验收 | 逐动作开放写操作，不一次性开放全部工具 |
| G5 切换发布 | G1–G4 对已声明交付切片完成 + 本项目存量迁移/回退演练、UI/兼容回归 | 生产菜单与已验收动作；未验收类型/客户端保持关闭并明确原因 |

Web、Desktop、Channel、scheduler 的验收记录分开。Web 可独立发布；Desktop broker 契约未通过时不阻塞 Web，但不能把 Desktop 任务标完成或宣称全端交付。只读先开放是中间里程碑，完整 change 收尾仍须完成承诺的 OA/邮箱写动作及目标客户端验收。

证据记录在实施期 `evidence/`，包括版本/构建、测试命令与结果、脱敏真实联调记录、权限主体矩阵、故障注入及审核人。初始不预填“通过”。依赖缺口先修复所消费的切片，必要时建立明确关联任务，不以等待整个企业化 change 或已有函数名作为通过理由。

## Risks / Trade-offs

- [平台共享 MCP 扩展凭据边界] → 使用独立平台归属、显式租户可用名单、每次消费授权及独立测试，不修改租户秘密可跨租户使用的禁令。
- [SAP SDK、OA 版本和企业网络无法由本地夹具保证] → 契约测试与真实受控环境证据分列；缺环境时保持对应 readiness 关闭，不能报告全功能上线。
- [旧 ERP API 消费者依赖密码回显或盲写] → 本变更明确不兼容部分，前后端联动升级，旧版拒绝保存，提供掩码/keep 兼容投影及迁移说明。
- [MCP 文件按 workspace 加载，迁为租户连接可能扩大 Agent 范围] → 源文件到 Agent/租户绑定逐项映射，保留原绑定/工具授权，不合并同名不同配置。
- [平台配置更新或撤权与长连接竞态] → 版本化解析、主动关闭旧池、派发前复核；已发送的外部动作按真实状态记录，不声称可回滚。
- [共享 OA 账号拥有广泛远端权限] → 本地工具按动作/资源收窄、写动作审批、审计记录实际发起主体和共享账号标识；不把远端权限等同于本地执行资格。
- [附件和 stdio 会触达宿主资源] → 重用路径边界与执行隔离；无证据不执行，禁止临时宿主 shell 兜底。
- [控制台与 Scene 的既有接线] → 仅增加独立页面登记与连接解析边界，合并时核对导航与权限改动，不覆盖既有文件或业务库。

## Migration Plan

1. **盘点和冻结映射**：扫描本项目实际 workspace 中的 MCP JSON（含 `mcpServers/mcp_servers` 和配置回退来源）及租户 `.one/erp_connections.json`；只生成脱敏报告，记录哈希、源到 tenant/Agent 的映射、旧连接 ID/工具名、默认项和重复项。平台归属必须由权威部署信息确认；路径名称或旧 global 注释不算依据。
2. **先部署关闭开关的控制面**：执行向前兼容数据库迁移，验证主密钥与备份可恢复，完成 G1；所有既有消费仍保持原受控路径，生产新写入尚未开放。迁移过程本身不额外启用原本关闭的 MCP 执行。
3. **分范围维护窗口迁移**：暂停目标范围配置写入和新执行，生成加密受控备份；按源哈希和映射幂等导入。ERP 复用原 ID，MCP 保留旧工具别名与 Agent 绑定；不能映射或加密的记录中止该范围。U9/金蝶记录保留为禁用演示态，默认项冲突交由管理员明确选择。
4. **核对并原子切换**：核对配置非秘密字段、秘密存在/版本、继承/默认关系、工具与场景引用和最小授权集合；提交该范围 `store_version` 后新旧 UI/API/runtime 都改读新服务。新源读取失败时失败关闭，不自动回退 JSON 或 `conf().mcp_servers`；旧写入口转接兼容层或拒绝，禁止双写。
5. **清理旧明文与刷新**：验证加密备份及恢复后，将旧配置中的秘密从在线可读源移除并按运维保留策略处理受控备份；清理旧进程/session/工具缓存，重启或刷新所需消费者。报告区分“在线源已移除”与“受控备份仍保留”，不承诺无法证明的物理擦除。
6. **分类型验收与发布**：完成 G2–G4 真正需要的依赖证据，再通过 G5 开放菜单/动作；按 Web、Desktop、Channel、scheduler 独立验收。发布文档列出仍关闭类型、客户端及原因。
7. **恢复策略**：切换前失败回滚当前迁移事务，未迁移范围维持原策略；切换后功能回退先关闭新动作/停用连接，保留新库与加密凭据，使用能读取新服务的兼容构建恢复 UI/消费者。禁止回退至只读旧明文 JSON 的版本；若必须整库恢复，须维护窗口、停止所有调用、核对密钥版本与已撤销秘密，必要时重新轮换后再开放。外部已产生副作用通过业务补偿处理，数据库回退不能撤回邮件或审批。

## Open Questions

以下是部署输入，不改变本 change 范围与架构；必须在对应 G2/G3/G4 开放前登记，缺失只阻止相关切片上线：

- 用于真实验收的 SAP RFC/ADT、OA E9/E10、IMAP/SMTP 和三类 MCP 测试环境及维护人；仅提供非生产测试账号，秘密通过正式凭据入口录入。
- 企业网络允许域名/网段/端口、CA、连接/测试并发与超时、附件大小与保留周期；默认值由上述设计给出，部署可以收紧。
- stdio 隔离执行器可用镜像/程序清单、SAP SDK 的安装许可与运行环境、Desktop 原生签名构建及测试设备；未满足前相应能力保持关闭。
