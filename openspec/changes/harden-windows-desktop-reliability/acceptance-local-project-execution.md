# 本机项目执行验收矩阵与证据要求

本矩阵承接原 change 的 A01–A33，并补齐原始计划实际存在的 A34–A36。旧归档 acceptance.md 仅作来源，本文件为合并后当前计划；全部用例仍待执行。

先复验已有实现，确认缺口才补代码；当前版本有效的自动化及集成结果可复用，一份证据可覆盖多个用例。真实原生选择、模型链路、OS 隔离与安装包仍需相应真实证据，不能以 mock 替代。工作包 A 的普通远程及 Windows 限制提示结果可供 B 引用，不重复跑同一序列。隔离不足阻止执行开放，不阻止代际、文案和非执行选择修复。

本文是待执行验收计划，所有用例当前均未验收。文档生成和 OpenSpec 校验通过只表示规划有效，不表示功能、权限或平台隔离已经实现。

## 1. 基线、环境与固定样例

- 上游对照固定为 `origin/master@8f1b19f1e72db0b46772f78f9c760b04b1836428`；记录实施分支 SHA、桌面版本、后端版本和每个实际复用接缝。
- 本地模式必须使用真实桌面、原生目录选择和登记的本机 Python 后端；远程模式必须使用真实远程后端、同源网关、设备连接、本机 worker 和实际模型结构化工具调用。只在 JS 中 mock `CowDesktopHost` 不算端到端通过。
- 本机项目执行正向矩阵覆盖已有实现且满足隔离证据的 macOS/POSIX 发布目标；Windows 本期只验收平台拒绝、能力提示及普通远程业务，不执行未支持工具的正向用例，不宣称 Windows 已支持本机执行。各目标的最低版本、架构和签名以实际发布表为准，POSIX 标签不代表所有 POSIX 平台已支持。开发态通过不能替代安装包通过。
- 准备两个用户、两个租户、两个 Agent、两个业务会话、设备 A/B 和项目 A/B。A/B 项目各有同名但内容不同的文件；服务端放一个同名哨兵文件，以发现错误落盘或串读。
- 项目路径包含中文和空格。固定工作簿含三条合成记录，金额为 100、200、50；汇总预期为 350。业务示例只验证数据处理及文件生成，不承诺税务计算规则。保留原始输入摘要，测试后核对原文件未被意外覆盖。
- 固定文档技能含 `SKILL.md`、脚本、模板和辅助资源，使用现有技能格式；除受控资源定位适配外不改写业务脚本。工作簿按单元格值、类型与结构验收，文档按预期字段及有效文件验收；记录产出哈希用于追溯，不要求带时间戳的 Office 文件每次二进制完全相同。

## 2. 必测用例

| ID | 场景与操作 | 必须观察到的结果 | 关联能力 / 阶段 | 本机状态与依据 |
|---|---|---|---|---|
| A01 | 清空有效安装标识后，真实 Electron 首次选目录；重启后再次选择 | 无 `Illegal invocation`；choose/bind 按序完成；ID 持久稳定；取消不创建授权 | project-execution / P1 | 逻辑层已覆盖（installation_identity 8/1skip、local_dir_crypto 7/0、local_selection 29/0）；真机选目录未执行 |
| A02 | 快速选择 A/B、取消选择、切换会话或租户后让旧 Promise 返回 | 只有当前代次可提交；无错绑；保留旧项目时 UI 清楚显示旧项目仍生效 | project-execution / P1 | 逻辑层已覆盖（local_selection 29/0 + 9.3 代际/迟到证据）；真机未执行 |
| A03 | 从普通 Web 提交本机绝对路径、伪造 chooser handle、复用另一用户选择记录 | 均被拒绝；原个人项目根浏览/选择和受控导入仍正常 | scoped-project-browser / P1 | 逻辑层已覆盖（target_resolution_acceptance 19 + 16 subtests）；真机未执行 |
| A04 | 本地模式选择中文空格路径，连续执行 read/edit/write/ls/search_files 和实际 Bash | 六个工具均访问同一真实项目；服务端/其他项目哨兵不变；复用原 Python 工具类 | project-execution / P1 | 逻辑层部分覆盖（local_e2e 15/1skip）；真机六工具未执行 |
| A05 | 远程模式通过真实模型调用同组六个工具 | 原 ExecutionRun 收到真实结果；worker 在设备 A 执行；服务端同名路径不被读写；没有第二个模型循环 | project-execution、execution-delivery / P2 | 逻辑层部分覆盖（remote_execution_acceptance 63/0）；真机远程模型链路未执行 |
| A06 | 本地与远程各运行固定 Excel 技能 | 汇总单元格为 350；报告真实位于所选项目；可用系统应用打开；输入摘要不变 | skill-runtime、project-artifacts / P3 | 逻辑层部分覆盖（remote_skill_acceptance 10/0；skill_acceptance 因本机内置技能部署路径 `WinError 3` 失败 5 项）；真机未执行 |
| A07 | 本地与远程各运行带模板和辅助资源的文档技能 | 模板资源齐全、输出字段正确；技能/记忆目录未搬进项目；项目以外缓存未被写入 | skill-runtime / P3 | 逻辑层部分覆盖（同上）；真机未执行 |
| A08 | A 上长任务运行中切到 B，再在 B 发新任务；两个会话共用缓存 Agent | 旧任务仍固定在获权 A，新任务使用 B；工具 cwd 不互相覆盖；旧卡片仍指 A | project-execution、project-artifacts / P2/P3 | 逻辑层部分覆盖（remote_host 34/0、target_resolution、web_pages 25/3）；真机未执行 |
| A09 | 队列接收后、开始之前分别撤销 Membership、Agent.use、skill.use 或项目 grant | 对应命令在副作用前失败；没有借缓存技能或旧许可执行；错误与审计可关联 | execution-delivery、skill-runtime / P2 | 逻辑层部分覆盖（`remote_host` 34/0、`action_approval_consumer` 26/0 覆盖服务端撤销/审批路径；**队列命令的"撤销后副作用前失败"走 broker，本平台 `503 platform_unsupported` 不可达**）；真机撤销时序未执行 |
| A10 | 团队会话转给无对应技能/工具资格的 Agent；服务端代理工具被黑名单禁止 | 按实际执行 Agent 的资格拒绝，代理不能跳过现有工具门禁 | project-execution、skill-runtime / P2 | 逻辑层部分覆盖（remote_execution_acceptance、target_resolution）；真机未执行 |
| A11 | 技能包传输中断、摘要错误、路径穿越、symlink、超预算；并发执行时升级包 | 坏包不发布、不使用半包；当前运行固定旧版本；新运行使用获权新版本；无跨作用域命中 | skill-runtime / P3 | 逻辑层部分覆盖（remote_host 34/0、skill_acceptance 部分）；真机未执行 |
| A12 | 缺 Python/处理库、技能硬编码服务器路径，或 Linux 服务器驱动 Windows 设备 | 显示真实平台和缺项；正确使用客户端 Shell 语义；不能悄悄回退服务端执行 | skill-runtime / P3 | 逻辑层部分覆盖（skill_acceptance 部分；**真机 meta 已验证平台缺项提示**）；真机未执行 |
| A13 | 技能消费服务器附件、知识/MCP 结果或 web_fetch 下载文件 | 获权资源显式准备并校验后本机可读；无法落地时明确失败；服务端绝对路径不冒充本机路径 | project-execution、skill-runtime / P3 | 逻辑层部分覆盖（remote_host、skill_acceptance 部分）；真机未执行 |
| A14 | 在收到已持久完成回执前断网，再重复投递同 command | 实际脚本计数只增加一次；返回原结果；聊天不重复追加第二次成功 tool_result | execution-delivery / P2 | 逻辑层已覆盖（`device_execution` 29/0；`command_runner` **31/0**——连跑 4 次全绿；早前一次 1 项失败经复核为**时序抖动**，非既有缺陷） |
| A15 | 在开始意图落盘后、启动或完成记录前强杀主进程/worker，再恢复 | 可判定未开始时明确拒绝/过期；无法判定时 outcome_unknown；禁止自动重跑消除未知 | execution-delivery / P2 | 逻辑层已覆盖（device_execution 29/0） |
| A16 | 同 command ID 改参数、资源摘要、作用域；过期后重放已清理回执的命令 | 冲突/过期被拒绝；不再产生副作用；重连 epoch 变化不会把旧命令当新调用 | execution-delivery / P2 | 逻辑层已覆盖（device_execution 29/0） |
| A17 | 脚本非零退出、超时、大量 stdout、资源超限；脚本启动后台子进程 | 返回真实退出/截断/超限原因；后台句柄只由原设备/项目/运行接续；不能跨会话 kill | execution-delivery / P2 | 逻辑层已覆盖（local_execution 36/0/23skip、v2 契约 63/0） |
| A18 | 取消长脚本，撤销授权，退出账号或切换租户；脚本已写一部分文件 | 进程树真实结束才标记取消；已写文件保留并提示部分效果；迟到结果不污染新会话 | project-execution、execution-delivery / P2 | 逻辑层已覆盖（device_execution 29/0） |
| A19 | 断网短于与长于失联存活期，再恢复连接 | 短断线须重验；长断线终止进程树；不接新任务，不转投服务器，不自动重跑 | execution-delivery / P2 | 逻辑层已覆盖（device_execution 29/0） |
| A20 | 两会话通过不同 workspace alias 指向同一实际根写文件，外部编辑器同时改文件 | 同根有副作用调用串行；普通 edit 检出版本冲突；不承诺 Bash 事务回滚 | execution-delivery / P2 | 逻辑层已覆盖（`device_execution` 29/0；`command_runner` **31/0**，含 `A20: our own successful write makes the next edit current again`） |
| A21 | 文件树、搜索、预览、编辑、@ 引用后执行技能，刷新页面并切换项目 | 来源与当前项目一致；@ 不重复上传；刷新先重验；文件变动能刷新，失效订阅被清理 | project-artifacts / P3 | 逻辑层已覆盖（project_refresh 22/0、project_source 20/0、project_watch 6/0） |
| A22 | 从项目 A 生成产出后切到 B、同路径替换/删除文件，再打开历史卡片 | 定位生成时设备和项目；变化/缺失明确显示；不对服务器同名路径 stat 或下载 | project-artifacts / P3 | 逻辑层已覆盖（project_source 20/0、project_refresh 22/0） |
| A23 | 设备离线、设备 B 查看设备 A 历史、客户端重启恢复候选 | 保留获权元数据和原因；不自动上传、复制或恢复授权；重新获权后才允许访问 | project-artifacts / P3 | 逻辑层已覆盖（`local_files` **12/0**：清空授权不动候选、取消不产生授权、租户/服务器变更撤销对应授权；`local_files_bridge` **6/0**：断连清授权但**不删候选**、选目录**记住候选**；`candidates.ts` 明述 "candidates … are not active grants … a restart shows the candidate list and asks the user to reconnect"）——**设备离线与"设备 B 查看设备 A 历史"仍需真机** |
| A24 | 预览含脚本的 HTML，尝试调用 preload/IPC、读取认证或其他目录 | 无原生桥、无秘密、无法越界；正常获权内容可预览；受保护资源 URL 会失效 | project-artifacts / P3 | 逻辑层已覆盖（isolated_preview 27/0） |
| A25 | 系统打开、打开所在文件夹、复制路径、另存为、明确上传/分享 | 原文件可用；文件变动或无关联应用真实提示；上传产生独立获权服务器副本；未操作时不上传 | project-artifacts / P3 | 逻辑层已覆盖（project_native_actions 40/0、isolated_preview 27/0；web_pages 3 项既有失败） |
| A26 | 真实脚本通过 Python os.open、动态路径、链接替换及子进程访问项目外文件 | 实际 OS 边界拒绝；测试不依赖命令字符串黑名单；允许项目/必要运行库/临时目录正常工作 | execution-isolation / P0/P4 | 逻辑层已覆盖（local_execution 36/0） |
| A27 | 脚本尝试读身份库、主进程配置、其他用户缓存或写技能目录；请求未授权网络/凭据 | 无环境秘密继承；只读缓存不可改；网络/凭据遵循声明范围；普通 full-access 不突破边界 | execution-isolation、skill-runtime / P0/P4 | 逻辑层已覆盖（local_execution 36/0） |
| A28 | 未批准动作、并发额度耗尽、磁盘不足、审计写入失败、许可过期 | 按现有治理策略失败；预留能释放；无“成功空结果”；新读写/执行能力不绕过治理切片 | 依赖切片 / P2/P4 | 逻辑层已覆盖（execution_commands 27/0、remote_execution_acceptance） |
| A29 | 旧服务器+新客户端、新服务器+旧客户端、新双方但开关关闭/能力缺失 | 只读 v1 按原规则运行；新能力明确不可用；旧 grant 不升级；无 exec 降级到 v1 | execution-delivery / P4 | 逻辑层已覆盖（compatibility_matrix 11+2sub；mutation_evidence_scripts 11 失败＝缺另一 change 的 evidence 目录） |
| A30 | 无项目、只读输入、关闭项目后继续会话；普通 Web 与 Channel 回归 | 恢复既有本人工作区/输出来源；共享 Skill/知识维护限制不丢失；原会话接口正常 | project-execution / P4 | 逻辑层已覆盖（target_resolution_acceptance 19 + 16 subtests） |
| A31 | scheduler 或机器主体引用本机项目，页面试图传任意 shell/模块到 bridge | 未在范围内的本机后台执行明确拒绝；不伪造 Membership；无页面通用 exec IPC | project-execution、execution-delivery / P4 | 逻辑层已覆盖（target_resolution_acceptance 19 + 16 subtests） |
| A32 | 有运行、已完成及 outcome_unknown 记录时关闭开关并降级客户端 | 封锁新任务，确认/保留在途效果；原文件、回执及历史不丢失；旧端可解释地展示不可用 | execution-delivery、project-artifacts / P4 | **本平台不适用**：broker 端点在本平台一律 `503 platform_unsupported`（9 个 A32/epoch 用例不可达，**未放宽门禁**）——需受支持平台 |
| A33 | 在真实安装包上重复 A04—A07、A12、A18、A24、A26—A27 | Python、库、模板和平台启动器全部随包可用；签名/系统权限未破坏执行边界 | 全部 / P4 | **本平台不适用**（真安装包上的执行用例需要实机项目选择）；安装包本身已产出并冒烟（见 `release-smoke-windows-installer.md`） |
| A34 | 工具输出包含摘要、错误、用户请求的 pwd；观察页面说明与传输 | 文案准确说明结果会送往服务器/模型；无整目录默认同步；路径文本不被当成授权依据 | project-artifacts / P3 | 逻辑层部分覆盖（run_inputs 27/0、input_staging 9/0）；真机页面说明未执行 |
| A35 | 模型只声称已生成文件，或 worker 上报了不存在/越界的产出 | 不生成可用产出卡片；只有经过真实文件验证的引用才可发布 | project-artifacts / P3 | 逻辑层部分覆盖（run_inputs 27/0）；真机越界产物未执行 |
| A36 | 对每个具有 cwd 或落盘能力的现有工具逐项回归 | 均有本机执行、显式资源传输或明确不支持的分类；不能遗漏后静默写服务端 | project-execution / P4 | 逻辑层部分覆盖（input_staging 9/0、run_inputs 27/0）；**发现守卫漏判**：`tool_disposition` 1 失败——`_WRITE_PATTERNS` 不认 `common.atomic_write.write_text_atomic`，故 `write`/`edit` 被漏判为非写者（见 `a-item-local-coverage.md` §4） |

**本机状态图例**（新增列，依据 `evidence/a-item-local-coverage.md`）：

- `逻辑层已覆盖`：本机测试套件覆盖了该项的**逻辑**（结果见证据文件）；**设备侧动作仍需真机执行**。
- `逻辑层部分覆盖`：只有部分子场景有本地用例。
- `未执行（需真机）`：本机没有对应用例。
- `本平台不适用`：因 `win32.supported=false` / 平台门先拒，该项在本平台无法成立——按 14.4 要求标"不适用"，**不得读作通过**。

> 这一列**不构成**任何真机验收结论；勾选仍以 `tasks.md` 为准。

## 3. 证据与阶段门槛

实施时再创建以下证据文件；本次不预填“通过”。日志使用合成业务数据，去除认证秘密、无关文件和个人信息；记录关联 ID、输入/产出摘要及可复核步骤。

| 证据文件 | 内容 | 对应门槛 |
|---|---|---|---|
| `evidence/master-reuse.md` | 固定 SHA、当前 diff、逐接缝复用/适配原因、未复制旧权限逻辑说明 | P0：复用范围可审查 |
| `evidence/platform-probes.md` | 每平台真实动态路径/子进程探针、选定启动 API、资源上限可强制性、包支持矩阵 | P0/P4：禁止用 cwd 或 mock 充当隔离 |
| `evidence/governance.md` | 消费的身份、权限、审批、审计、凭据和硬配额切片及真实负向结果 | 每个阶段启用前：依赖切片已具备 |
| `evidence/local-e2e.md` | 本地 chooser→Agent→原工具→实际项目文件与卡片；版本、日志及结果 | P1/P3：本地完整闭环 |
| `evidence/remote-e2e.md` | 远程真实模型→网关→桌面 worker→结果→原运行；无服务端错误落盘 | P2/P3：远程完整闭环 |
| `evidence/faults.md` | 回执丢失、崩溃、重复投递、取消、断线、并发/换目录的注入点和实际效果 | P2：有副作用命令可恢复且不自动重复 |
| `evidence/release.md` | A01—A36 与模式/平台的覆盖索引、安装包结果、兼容/迁移/回退及最终 flag 状态 | P4：两种模式均交付 |

一次通过须记录环境、操作、预期、实际、证据和结论；“待测”“依赖环境不可用”不得登记为通过。失败保留原结果及修复后的复测记录。文件面和脚本面独立记录门禁；本次合并 change 的完成要求包括受支持平台的文件面、脚本面及远程执行验收，Windows 以明确未支持和负向验收交付。未支持平台不算正向通过，但必须完成其限制提示验证。

## 4. 自动化层次

1. 单元与契约测试：crypto 接收者、目标解析、schema、摘要/去重、版本校验、来源 adapter；验证真实失败分支，不只验证函数被调用。
2. 进程级集成：实际 Python 工具类和 stdio worker、持久 journal、杀进程恢复、真实文件与子进程边界。
3. 桌面与服务端集成：真实 Electron chooser/IPC、登记的本机后端及远程网关、多个认证作用域；真实 Chromium 测试避免 JS mock 掩盖 native API 问题。
4. 安装包及模型端到端：真实发布环境、固定合成样例与真实模型工具消息，完整观察落盘及 UI 结果。模型回复文案不要求逐字一致，文件内容、作用域、消息结构和终态必须符合断言。

文档当前的格式验证不代替上述任一实施验收层次。
