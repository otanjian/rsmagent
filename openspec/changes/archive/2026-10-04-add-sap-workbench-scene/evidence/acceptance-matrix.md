# 验收映射与阶段结论

2026-10-04 最新卡片入口：点击除「配置」按钮以外的区域直接新建会话，配置按钮只打开表单；相关前端 **96 pass**、资源路由 **1 pass**。Chrome 实际点击卡片标题后自动显示真实 SAP 登录页，右下角展开工作台内原生 OpenCode 空白会话；未发送消息、输入登录凭据或调用 SAP 业务工具。Web/桌面沿原环境重载，18 项公共资源检查通过。完整计划仍 **42/57，剩余 15 项**；见 [卡片直接新建](card-create-session.md)。下述 90 项为上一轮布局验证。

2026-10-04 最新 UI：SAP 主界面加右下角「AI 对话」按钮，默认收起的原生 OpenCode 浮动面板保留同一 iframe、草稿和回复；开关不调用 API 或改变控制权。相关 Node **90 pass**、入口/资源路由 Python **1 pass**，Chrome 桌面/390px 窄屏、草稿与模拟流保留、Escape/Enter 已通过隔离布局数据验证。真实 Chrome 工作台入口加载最新文案；Web/桌面后端保留原环境重载，18 项公共资源/匿名 API 检查通过。实际 SAP/OpenCode 操作检查继续暂缓，**42/57，剩余 15 项**不变；详见 [浮动对话布局](floating-chat-layout.md) 和 [本次重载](floating-chat-services-reloaded.json)。以下部署、全套和现场结果按轮次保留为历史。

2026-10-04 本轮独立部署：固定官方 Debian OCI 两架构下载、payload 校验及 Docker 导入通过，原 Dockerfile 基础 stage 已通过；APT main/security 快照连接失败，沿用已有 VM 代理的一次构建为 502，未完成 Linux 镜像/显示运行验收。独立 CLI 与部署/清理组合 **90 pass，0.45 秒**，其中 CLI 38 项，不累加。本轮未重跑全套、Node、Bun 或联网组件专项，未重启主服务，15 份已加载源码摘要未变。完整计划仍为 **42/57，剩余 15 项**，实际 SAP/OpenCode 操作继续暂缓；最新证据见 [基础镜像恢复](oci-base-recovery.md)。上一轮完整 **1182 pass / 2 skip、Node 170 pass** 及重载结果保留在 [MCP/DOM 收尾](mcp-dom-closeout.md)。

2026-10-04 上一轮采购订单只读收尾：完整 SAP Python **1123 pass / 2 skip，92.25 秒**、相关 Node **165 pass**、Bun **4 pass / 81 assertions，1.99 秒**；两个后端已保留完整原环境重载，18 项公共资源及匿名 API 检查通过；重点 Python 260 与 PO 模块 139 属重叠专项，不累加。首次重点组合的 259 pass / 1 fail 为撤权 Mock 迭代耗尽，修复 fixture 后通过。新增固定三表完整有界 JSON 读取及字段拒绝状态，计划保持 **42/57，剩余 15 项**；没有实际 SAP/OpenCode 操作、新 UI 或提交验收。该轮指针为 [采购订单只读收尾](purchase-order-read-closeout.md)，以下 895/161/3、旧服务快照及现场记录保留为上一轮证据。

2026-10-04 上一轮合同复核：4.1 的独立组件提供、版本固定及主服务无在线依赖按原合同完成，当前 **42/57，剩余 15 项**。SAP Python 第二轮 **895 pass / 2 skip，91.57 秒**，Node **161 pass**，Bun **3 pass / 68 assertions，2.11 秒**；首轮 **1 failed / 889 passed / 2 skipped** 的进程组退出故障和修复链路保留，专项不与全套相加。两个后端已保留完整原环境重载，18 项公共资源/匿名 API 检查通过；没有新的 Chrome UI 或 SAP/OpenCode 操作验收，见 [本轮重载快照](next-contract-services-reloaded.json)。Linux 运行和工作台绑定验证仍为 `false`，4.3/4.4/4.7 未完成，远程显示不开放。实际操作继续暂缓；以下 859/819 和旧运行快照是原阶段证据。最新指针为 [本轮合同收尾](next-contract-closeout.md)，不将此次勾选、回归或重载解释为容器及现场验收通过。

更新于 2026-10-04。按用户后续要求，本轮优先本机可用链路，MCP 配置凭据与 Web GUI 登录分开。未验收的生产权限、统一登录及业务提交仍保留为后续任务；不会因文档或替身测试通过而启用保存/过账。

上一已验证的本机收尾为 **859 Python pass / 1 skip，86.25 秒**：3.2 按原 API/登记合同完成，新增 6 类目标覆盖拒绝用例；31 项新 F4 兼容和 3 项临时显示进程/文件用例包含在总数中。此前输入、总容量和退出清理修复保留。桌面与 Web 后端在该轮均已保留原数据根/主密钥重载；Chrome 登录保持，工作台可打开并读回配置，未启动新 runtime 或执行 SAP/OpenCode 实际操作。桌面仍为正常浏览器登录入口，7.2 未完成。当时完整计划为 **41/57，剩余 16 项**，具体条件见 [范围复核](scope-closeout-review.md)，结果、截图与运行快照见 [后续收尾](incremental-closeout.md)。Node 153 和 Bun 3/68 沿用对应未改源码的上一阶段结果，未重复执行。

上一独立收尾为 **819 Python pass / 1 skip，82.38 秒、40/57**，记录保留在 [本机收尾](local-closeout.md)。本次纠正将独立 SAP 资源目录当作 3.2 必需实现的旧归因，不新增生产策略或放宽普通运行请求；独立工具/模型授权的 5.5 仍未完成。

上一继续阶段推进到 **40/57，剩余 17 项**：正式审批消费者、持久 unknown/只读恢复及提交边界测试（6.2/6.4/6.5）完成；真实 SAP 保存/完整业务 verifier 尚未注册，G3 仍关闭。该阶段结果为 **747 Python pass / 1 skip、153 Node pass、3 Bun host pass / 68 assertions**。固定 SAP ADT 配置账号三元组核验、两个真实 MCP 只读调用与原生回复、Web 重启后的历史恢复和双栏有证据；左栏仍为证书警告，桌面仍需正常登录/首次改密。容器构建遇基础镜像 HEAD EOF，Colima 已恢复停机。见 [继续交付记录](resumed-delivery.md)；下文暂停、未重启、37/57 与 493 保留为各阶段历史，以页首和本机收尾记录为最新状态。

最新安排：用户已暂停 OpenCode 与 SAP Web GUI 的实际操作检查测试。后续仅推进独立实现及隔离测试，已有现场结果不代表后续代码已重新进行现场验收。会话创建与失败恢复的新增实现见 [分配恢复记录](allocation-recovery.md)；本机运行依赖检查、节点修复和有界探测/启动/清理见 [运行准备记录](environment-readiness.md)。

前次 19 项输入、31 项总容量、22 项退出和本次新增用例均包含在 859 总数中；专项组合有重叠，不累加。详情见 [范围复核](scope-closeout-review.md)、[负载准备](load-readiness.md) 和 [退出清理](shutdown-cleanup.md)。下表保留逐项能力来源，历史条目的“本轮未重启/尚未加载”不覆盖本次已经完成的重载。

| 规范/能力 | 自动化证据 | 真实现场证据 | 当前结论 |
| --- | --- | --- | --- |
| SAP 主界面与浮动 OpenCode 面板 | 前端/场景/coding 组合 90 pass，入口资源路由 1 pass；同一 iframe/画面/连接、无新请求、默认收起、重连和三语 | Chrome 实际加载场景 JS/CSS 的桌面/390px 布局 fixture；草稿及模拟流保留，Escape/Enter；真实 Web 卡片打开新版入口，未启动新业务会话 | 最新 UI 范围通过；本次未执行真实 SAP 或模型操作，不完成 7.2，见 [布局证据](floating-chat-layout.md) |
| 工作台入口、独立场景分发 | `test_sap_workbench_frontend.cjs`、`test_scenes_frontend.cjs`、`test_scenes_api.py` | Chrome 场景卡片、配置页、双栏已打开 | 本机 Web 通过 |
| 连接配置、秘密不回显、恢复与并发编辑 | `test_sap_workbench.py` 的固定端点、密码保存/替换/清除、并发版本测试；`test_configuration_edit_and_restart_preserve_existing_session_target` | 原租户配置保留；当前 admin 自有私有 coding 与 SAP 场景配置已通过正常 UI 保存，实际检测和 MCP 调用成功 | 7.7 所列配置交付完成；不代表跨用户生产授权通过 |
| 3.2 API/登记目标/普通请求封闭 | 配置/访问/输入专项 78 项；真实临时平台身份，6 类运行参数覆盖在分配前拒绝，保存配置/会话不变 | 本次仅正常 Chrome 入口与配置显示，未启动 runtime | 管理员配置登记、coding/固定节点、方法注册和逐次绑定复验满足原合同；无需增建独立 SAP 资源目录；5.5 另行验收 |
| 本机运行准备、节点配置与有界恢复 | `test_sap_workbench_environment.py`、`test_sap_workbench_deadline.py`、配置/前端/探测/runtime/MCP 隔离测试；离线 `RuntimePaths` 5 项和实际 worker Python 3.10 同任务退出检查通过 | 本轮未执行真实联网或界面操作；主后端未重启 | 3.10 按当前本机同进程节点及 host 消费端范围完成，新增补丁下次重启加载；不代表远程目录或真实登录通过 |
| 兼容版本期望与漂移报告 | `test_sap_workbench_compatibility.py` 30 项；只读元数据报告九项匹配、一项 OpenCode 来源漂移；26 个 host 源码引用静态接缝未变 | 当前来源未现场重测；历史来源与后续 Web 版本不是同一次发布锁 | 部署报告完成，1.5 的主题/SSO/远程/完整版本验收未完成，见 [兼容基线](compatibility-baseline.md) |
| OpenCode 原生 UI 与工具接入 | `host.test.ts` 使用真实 Session V2 HTTP、取消及原生 Web reducer；模型和 SAP 桥为替身 | 原生流式回复、页面工具和 MCP 工具结果显示，历史恢复 | 本机单会话链路通过，未修改 OpenCode 核心 |
| 唯一浏览器绑定与控制权 | `test_sap_workbench_runtime.py`：主体/目标/快照/动作代次；`test_sap_workbench_manager.py`：并发预留、第二画面拒绝、断线复用、回收、取消及关闭；`test_sap_workbench_access.py`、`deployment.test.ts`：真实双用户身份/网关和两个实际 host 隔离 | 左侧点击接管后，右侧自动控制暂停；页面回读与画面一致 | 本机隔离专项通过；生产部署与两个实际 SAP 登录态并发未验收 |
| 控制状态向界面同步 | gateway 状态变化/撤权断流、前端控制帧与登录提示测试 | 当前账号会话中人工控制拒绝自动动作，恢复允许对话操作后检查成功；后端重启恢复时默认人工控制 | 当前账号现场通过 |
| SAP 页面观察 | `test_sap_workbench_controls.py`；Chrome 独立 DOM 样例验证字段、嵌套 frame、中文、下拉选项、表格、弹窗和状态消息 | SAP Easy Access、SPRO、IMG、ME21N 标题/字段回读成功 | 已实际覆盖供应商 F4、消息弹窗、日期、首行短文本/数量；完整表格分页和全部控件未验收 |
| 页面操作 | 过期快照/未知控件/接管/未观察到变化的失败测试；实际 IMG 标签回归 | 当前账号自动 SPRO→参考 IMG、ME21N、F4/关闭、日期往返、首行中文短文本/数量填写及独立回读通过；空单据检查实际返回 E“凭证不包含项” | 本机不保存链路通过；不等价于完整可保存采购订单通过业务校验 |
| 小屏、主题、配置返回和关闭 | 前端恢复/关闭状态测试；实际工作台 JS/CSS 在独立 Chrome 样例页运行 | 390×844 对话输入可见、面板切换、深色主题、关闭后无残留遮罩；样例使用模拟数据 | 仅布局验收通过，见 `layout-mobile-fixture.png`、`layout-dark-fixture.png` |
| MCP 固定端点、配置凭据和测试 TLS 例外 | `test_sap_workbench_mcp_login.py`、配置测试 | 两网关实际只读调用成功，sap-pyrfc 使用 ADT 回退 | 原生 RFC 未验证；一次瞬时失败根因未确定 |
| MCP 凭据生命周期与共享项目隔离 | 73 项专项，真实临时平台身份/配置/存储与 fake IPC/SAP identity；轮换/清除/改目标/注销/撤权即时回收、并发 consumer、旧结果丢弃、connection_id 冒用拒绝 | 本轮未重测真实双 SAP 用户或现场连接销毁；whoami 为 registry 元数据，系统 SID 明确 unverified | 独立实现通过，5.8 保留完整验收，见 [MCP 生命周期](mcp-lifecycle.md) |
| 封闭自动动作效果 | 99 项 Python、8 项 DOM Node；fresh revision、焦点链、dirty 防离页、帮助类型和来源字段/弹窗/树/IMG 特定回读 | 更严格的帮助标题、选项 value 和页面规则本轮未现场重测 | 6.1 首批效果登记完成；未知页签、帮助、定制页面与持久化操作关闭，见 [动作效果](action-effects.md) |
| 已观察供应商 F4 搜索窗口的窄范围兼容 | 新增 31 项，effects/controls 组合 101 pass；原焦点/唯一 dialog/frame DOM guard 3 pass，独立历史文本 fixture 不依赖 change 活跃目录 | 历史 seq101/111/121 仅提供标签/关闭依据，新 label/text 及过滤值未真实重测 | 仅完整固定文本打开/观察/同归属 Escape 关闭；搜索输入/执行/结果选值/页签拒绝，不推断过滤值为空，见 [F4 兼容](f4-compatibility.md) |
| 受限表格滚动 | 69 项 Python、9 项新增固定 DOM；表格 ID/视口/索引窗口、四方向、安全派发点和具体稳定读回 | 本轮无真实虚拟表格或分页验证 | 独立滚动实现完成，5.3/5.7 真实兼容保留，见 [表格滚动](table-scroll.md) |
| 请求解析、排队复验与预留失败 | 121 项桥/模型/runtime/lifecycle；异常 body/call_id 副作用前拒绝，锁内复验，quota/audit 提前失败终态，同桶确定未派发退款 | 未新增真实服务/模型请求；仅真实临时平台身份与 loopback 替身 | 补齐既有桥，完整生产资源与计量仍待验收，见 [桥边界](bridge-boundary.md) |
| 取消/启动回收、前端请求竞态、字段写入和回读 | 新增 9 项资源回收、14 项字段回读；固定 DOM 与前端测试覆盖 focus 替换、CBS 同格定位、超长值和旧请求；最终完整套件见下文，专项不重复相加 | 本轮没有现场操作或服务重启 | 独立修复通过，真实控件及新代码加载仍待验收，见 [独立收尾](independent-closeout.md) |
| 桌面双栏 | 桌面构建通过；补齐独立静态目录，1787 个文件逐一核对，五项离线检查通过 | 仅到平台登录页，本轮没有创建桌面双栏 | 尚未完成登录后的验收，见 [桌面资源准备](desktop-assets.md) |
| 正式审批、提交记录及 unknown 只读恢复 | 52 项真实临时 IAM 审批测试与 14 项受信 bridge；精确 target/参数/代次/审批、消费期间撤权、quota/audit、并发只消费一次、未知状态和丢失浏览器后只读核对 | 未注册完整 SAP 保存执行器和只读业务 verifier，未执行业务提交 | 6.2/6.4/6.5 的实现与隔离验收完成，6.3/6.6 现场保留；G3 未通过且关闭 |
| 配置账号真实 SAP 身份、可选统一登录 guard | 146 项严格响应/绑定/取消/大小/源/代次专项；配置账号与浏览器证明区分 | 一次固定 ADT 请求实际匹配 SID/Client/用户名；未核验浏览器或 existing connection_id 的服务端身份 | 配置账号 ADT 核验通过；2.9/3.11 完整运行态与现场未完成 |
| 独立 Linux VNC 显示部署 | 原 42 项隔离部署/协议，新增 3 项实际临时进程与文件，合计 45 pass；并非容器认证/显示验收 | 早期 build/pull 与官方 ECR 在基础镜像 HEAD 返回 EOF；最新官方 OCI 已导入，build 推进到 APT 失败/502，Colima/context 已恢复 | 4.1 按原组件提供/版本固定/启动独立合同完成；构建、运行及工作台接入未通过，4.3/4.4/4.7 保留，见 [本轮合同收尾](next-contract-closeout.md) 与 [重试证据](linux-display-retry.md) |
| 采购订单已有单据只读工具 | 重点 Python 260、PO 模块 139；固定三表 COUNT/read/COUNT 两轮、精确列/键/数量/金额、完整 JSON 及桥参数/撤权/大小拒绝；Bun 4/81 含真实 host 工具 fixture | 本轮无真实 SAP/MCP/模型或 OpenCode 页面操作 | 仅标准 NB 物料投影，三个完整业务/提交标志 false；private compare 不接 CommitConsumer，6.3 未完成，见 [只读收尾](purchase-order-read-closeout.md) |
| 字段显式拒绝状态 | Python 与固定 DOM Node 验证编辑器/grid cell aria-invalid 的 true/grammar/spelling/unknown 优先拒绝，无标记 unavailable；包含于本轮全套/Node 总数 | 未重新操作真实字段 | 页面值接受规则增强，不推断 SAP 业务校验或保存成功，见 [只读收尾](purchase-order-read-closeout.md) |
| 固定 Debian OCI 下载与导入 | 新 CLI 38 项，原部署/清理组合 90 pass；错误/大小/时间/摘要与原文件保护 | 两架构官方 payload 真实下载及 Docker load 原摘要成功，原 build 进入 APT 后连接失败/502；清理构建资源并恢复 VM/context | 独立基础镜像恢复通过；完整镜像、Chrome/VNC/远程运行仍未验收，见 [恢复记录](oci-base-recovery.md) |
| MCP SDK 包装与 worker 管道 | 新增 47 项 SDK 包装与 6 项管道 Python；实际安装 MCP 1.29 / worker Python 3.10 离线转换、文本/structured 一致及大小/深度/节点/敏感拒绝 | 无 SAP/MCP 请求，StreamReader 与 SDK 返回值在隔离 fixture 中验证 | 256 KiB 管道支撑已有文本预算，48,000 字节私有 JSON 限制不变，见 [MCP/DOM 收尾](mcp-dom-closeout.md) |
| 旧 DOM marker、编辑状态与 F4 原字段回读 | 新增 6 项 F4 Python、5 项 DOM Node；同字段完整语义/title/profile/代次、显式拒绝、两次稳定回读、marker 清理及 editor/cell/grid 状态复验 | 未进行新的浏览器或真实 SAP 控件操作 | 已登记动作消费者修复；未知帮助/页签/分页未开放，不证明业务成功，见 [MCP/DOM 收尾](mcp-dom-closeout.md) |
| 本轮局部提交摘要边界 | 13 项新增 Python；低于边界值参与摘要，局部截断、行列上限和同前缀值在审批创建前拒绝 | 未注册保存适配器，未执行 SAP 提交 | 独立消费者修补通过，不完成 6.3，见 [提交快照边界](commit-snapshot-bounds.md) |
| 本轮显示停止与进程组回收 | 定向最终 52 pass / 1 skip；真实 orphan fixture 文件重复十次各 6 pass；持续信号/等待错误不跳过其他组 | 固定上游 noVNC/websockify 传输 fixture 已有独立证据，Linux 容器/Chrome/认证/画面/输入未验收 | 首轮全套 EPERM 失败保留，有限重试只以实际成功或 ESRCH 结束，见 [清理失败修复](linux-display-cleanup-eperm.md) |
| 本轮页签/表格只读观察 | 186 Python / 78 Node 定向组合，新增 16 / 8；引用唯一性、frame 可见性、ARIA 计数/索引来源与投影截断 | 没有新的现场控件证据 | 只读元数据通过；页签执行、完整分页和完整业务数据关闭，5.3 未完成，见 [观察合同](tabular-observation-contract.md) |

## G0–G4

- **G0**：本机外置 host、原生 Web、MCP 和真实页面控制可工作。完整多用户授权、模型硬配额及远程环境未验收。
- **G1**：专属 Chrome、同一 target 的画面/人工输入、断线恢复可工作。SSO、代表性负载及全部 SAP 控件未验收。
- **G2**：读取、SPRO/ME21N 跳转、自动参考 IMG、F4/关闭、日期往返、首行中文短文本与数量填写/独立回读、空单据业务错误均有真实证据。完整可保存单据及表格分页仍未验收。
- **G3**：未启用；没有保存、过账或删除任何 SAP 单据。
- **G4**：专项回归、配置示例、边界与证据已交付；桌面登录后的双栏、完整生产范围仍待验收，change 不归档。

既有扩展回归记录：Python 415 项通过；相关 Node 76 项通过；Bun host 2 项 / 45 断言、双 host 部署隔离 1 项 / 18 断言通过。扩展 Node 中 3 个失败在 HEAD 原代码复现。该轮 OpenSpec strict 和 Git 空白检查通过；非 SAP 资源摘要问题保持原记录。细节、隔离测试边界和实际配置页 404 修复见 [收尾回归记录](runtime-regression.md)。这些数量保留为对应轮次的记录，不作为本轮新增补丁的测试总数。

上一轮 SAP 独立套件 **251 项通过，耗时 49.19 秒**，相关 Node **80 项通过**；MCP 与 deadline 专项另一次执行 **50 项通过**，保留为对应版本记录。本机离线 5 项检查原始结果见 [offline-environment.json](offline-environment.json)。实际 worker Python 3.10.0 的纯内存 AnyIO 正常退出、慢退出 deadline 和恢复记录见 [worker-compatibility.json](worker-compatibility.json)。同任务 deadline 不新增 `async_timeout` 依赖。

加入表格滚动及桥接修复后的上一轮 SAP 独立套件 **470 passed、1 skipped，35.92 秒**；相关 Node **93 项通过**，含 17 项固定 DOM guard。保留为对应源码阶段记录。

独立收尾后的最终 SAP 套件为 **493 passed、1 skipped，34.87 秒**；相关 Node **153 项通过**，包含 **70 项固定 DOM**。跳过的是用户暂停的真实 Chrome 画面/输入测试，实际 worker Python 3.10 的纯内存退出测试通过。字段与资源专项都包含在最终套件中，不累加。执行命令：

```sh
SAP_WORKBENCH_LIVE=0 SAP_MCP_PYTHON=/Users/jiantan/ai_assistant/rsmcode/sap-connect/sap-pyrfc/.venv/bin/python \
  .venv/bin/python -B -m pytest -q -rs tests/test_sap_workbench*.py
node --test tests/test_sap_workbench_dom.cjs tests/test_sap_workbench_frontend.cjs tests/test_scenes_frontend.cjs tests/test_coding_frontend.cjs
```

没有连接真实浏览器、OpenCode、模型或 SAP/MCP 网关。工具包 TypeScript 仅作临时文件语法构建，通过且不执行 host；新增 host 测试中的导航 revision 契约断言未运行，不当作运行兼容证据。OpenSpec strict、Git 差异空白检查、94 个场景/change/专项文本文件空白检查及 3 项 SAP 静态资源摘要通过；`rsmcode/opencode` 工作树仍 clean。版本报告返回 1 是已记录的源码漂移，不能当作全部兼容通过，原始报告见 [compatibility-report.json](compatibility-report.json)。Bun host 和真实现场未重测，主后端本轮未重启。

本轮扩展回归已完成 181 项通过、236 个子测试通过；另有 6 个非 SAP 资源摘要检查失败，在 HEAD 原代码同样复现。该限制保留为现有项目问题，不通过改动其他场景的摘要掩盖，也不把扩展回归的通过数合并为 SAP 专项测试总数。

运行准备另恢复了已退出的 OpenCode API `4096` 与 Web `3000`；仅核对后台进程和 loopback 监听，未发起 API 或实际操作测试。桌面/主 Web/两个 MCP 原进程保留，主后端补丁下次重启加载。详细状态见 [运行准备记录](environment-readiness.md#缺失服务恢复)。

本轮末尾仅核对监听：Web 9899、桌面后端 9876/前端 5173、OpenCode API 4096/Web 3000、MCP 8100/8200 均在监听；没有发起 HTTP 请求或重启这些服务。监听不代表新源码已加载或新来源兼容通过。

## 当前交付状态

Web 当前账号 `admin / 默认租户` 已通过正常产品界面建立自己的测试配置及双栏绑定，并完成实际操作。原 `test15 / AI租户1` 配置和历史保留；未复制跨租户凭据、未修改身份、未绕过登录。旧的“原账号阻塞 Web 验收”结论已解除。

桌面端已启动，登录后的双栏仍待完成：其独立后端的授权页面显示“登录信息有误，请重试”，已保留正常登录窗口等待用户完成平台登录。本次 SAP 浏览器证书由用户亲自处理，后续恢复沿用该专属 profile；MCP 的测试 TLS 例外不代表后台 SAP HTTPS 检测通过。

任务 3.10 按当前本机同进程节点及 host 消费端范围完成；6.1 首批封闭效果登记完成后，完整原计划为 **37/57，剩余 20 项**。本轮独立收尾未增加整项勾选，剩余编号和暂停/延后原因见 [独立收尾记录](independent-closeout.md)。未勾选任务包含用户已延后的统一登录、生产权限/审批/配额、远程部署和 G3，以及当前暂停的现场测试。1.5、5.8 的独立部分已有新增证据，仍不将完整真实验收勾选完成。当前本机 Web 的既有不保存操作证据见 [后续现场验收](live-workbench.md) 与 [收尾回归记录](runtime-regression.md)。这不代表完整 G0–G4 已验收，change 保持未归档。
