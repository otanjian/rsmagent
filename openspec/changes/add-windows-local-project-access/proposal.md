## Why

Windows 用户需要让 AI 在本人明确选择的本机项目中读写文件、运行脚本并使用兼容技能；当前POSIX-only实现和v2全量平台门阻断了这条链路。本change分两阶段独立发布：先交付有限文件操作，再交付有真实OS隔离的Python/PowerShell脚本与技能；整体完成包含两者，不以开启平台标志代替实现。

## What Changes

- 第一阶段支持 Windows 10 22H2 / Windows 11 x64 本地NTFS的 `ls`、`search_files`、`read`、`write`、`edit`，覆盖面板与真实模型调用，本机/远程模式同一授权目录。
- 增加 Windows 根句柄访问后端，逐组件按句柄解析，拒绝 reparse point、目录替换、路径别名及写入硬链接逃逸；复用 Rust helper 的帧、取消和大小限制，不用字符串前缀检查充当安全边界。
- 现有只读 fs-guard 的操作白名单保持不变；独立的有限写入 helper 仅提供声明的文件操作，不执行 Python、Shell、项目代码或插件。
- 两阶段采用新版project_execution合同并复用执行信封、取消、结果和files/scripts能力面；合同先定稿再接业务，旧v2的win32.supported=false及Bash语义保持。版本/工具/schema/运行时协商以合同为唯一来源，平台验收是能力交集的一个输入。默认复用开关，有明确灰度/应急需求才增加只收紧能力的控制项。
- 用户分别选择只读输入或本机项目读写；读写授权不授予脚本/删除/任意系统访问。既有只读 grant 不升级，重启/退出/切换身份后不恢复授权。
- 提供明确本机来源、写入前版本冲突阻断、已写入文件及未确认副作用；覆盖文件验证权限保持，不以临时文件较宽权限替换原目标。必要内容可进入模型，不自动上传整个项目，不回落服务器同名路径。
- 第一阶段安装包随附helper，无需系统Python/Rust、管理员权限或修改项目ACL；真实安装包/远程/负向探针通过后开放文件面。
- 第二阶段先Python和兼容技能、后PowerShell 7，两者均包含在整体交付中；比较随包worker复用与独立CPython的隔离/依赖/维护成本后选择。LPAC为隔离候选，Job Object管理进程树；最小隔离及权限生命周期原型通过后才接业务，不创建第二个Agent循环。
- 第二阶段脚本直接在已确认项目运行；实际需要ACL变更时须明示确认、验证原访问语义及对象移动后的恢复，不仅以删除所加ACE作为恢复证明。不能安全配置时拒绝；异常恢复未完成如实报告、停止相关进程并阻断受影响作用域，不承诺所有异常自动恢复。
- 两阶段共用协议骨架及现有开关，分别核验文件/脚本授权及平台证据；第一阶段不等待第二阶段，第一阶段完成不等于整个change完成。脚本入口不能通过文件helper运行，旧只读/读写grant不自动升级。隔离身份可按运行或同一项目授权会话管理，由原型选择满足边界且恢复成本较小的方案，不强制每次命令修改ACL。
- 第一阶段无通用删除/重命名工具，第二阶段脚本可在自身获准项目内产生包括删除在内的副作用并明确告知，不承诺自动回滚。不提供模型指定任意程序的通用启动入口或系统级可执行文件白名单产品，脚本后代仍须受相同隔离与Job约束。Bash/cmd、GUI/COM/Office自动化、任意包安装、UNC/网络盘/云占位文件、跨端同步不在本次。
- 验收按原生OS边界、连接模式及运行时分层，复用同版本有效证据，不要求所有用例遍历全部组合；两目标OS及两种连接模式各自保留真实证据。
- 项目授权可包含用户放在项目内的.env等内容，不承诺自动识别/隐藏全部秘密；宿主身份库、控制面令牌和未授权企业凭据必须隔离。首批交付列明Excel/文档Skill及依赖版本，不推导任意开发工具链可用。helper故障按脚本实际安全依赖决定在途任务是否终止。

## Capabilities

### New Capabilities

- `windows-local-project-files`：Windows 文件工具的句柄边界、有限操作、可验证写入、统一执行合同兼容及安装包门槛。既有通用代码复用，本次新增 Windows 文件后端。
- `windows-local-project-scripts`：第二阶段Windows脚本/兼容技能的明确授权、运行时语义、隔离启动、真实终态与独立发布门槛。

### Modified Capabilities

- `scoped-project-browser`：追加 Windows 本机项目只读/读写选择、明确能力和一致文件来源，普通服务器浏览根与导入语义不放宽。
- `execution-isolation`：追加Windows脚本阶段的OS强制边界、隔离身份授权和可恢复清理，既有通用隔离要求保持。

## Impact

- Desktop：`desktop/native/fs-guard/`、`local-files/`、`project-browser/`、`project-execution/{command-runner,journal,file-versions,device-execution}.ts`、`remote/{binding-setup,remote-container-ipc,device-connection}.ts`、renderer/工作台来源和错误提示、Windows 打包与签名。
- 服务端：交付新版合同（计划contracts/desktop/v3.json）、类型/校验、operations授权及持久迁移、文件版本前置条件、脚本工具（暂名run_script）的注册/schema和运行时协商。工具名定稿前核对注册及配置，内部同名函数不等于工具撞车。默认复用部署开关，不新增文件/脚本两套顶层协议。现有网关/派发增量接入；Windows补release.yml构建、extraResources、签名和真实包路径验证。
- 数据归属：账号、租户、许可、业务运行及审计继续在服务器权威存储；本机绝对根、句柄和活动 grant 仅在桌面，文件默认原地保留。必要文件结果/摘要可进入服务器和模型，不能宣称所有数据不出设备。journal 持久存本机但不构成授权。
- 依赖/协调：复用 `desktop-tenant-context`、`scoped-project-browser`、`resource-execution-authorization`、`audit-log`、`resource-quota` 及适用审批切片。承接 [Windows执行延期记录](../harden-windows-desktop-reliability/evidence/deferred-windows-execution-change.md)，候选实现以本次新合同决策为准；其历史平台skip按用例映射补Windows真实覆盖，不机械取消mac专属skip。harden四份通用执行规范不重复；harden与add-shared-agent-release-governance同时修改execution-isolation的“权限模式在租户语境下收紧”，同步须串行合并并重验基线，不整文件覆盖，不把其他change全部完成作为原型前置。与enterprise-local保持模式区别。
- 不改记忆锁修复范围；其历史生产故障说明平台适配需要验证，不能作为脚本逃逸或隔离已完成的证据。此 change 仅规划，未实施、未开放平台声明或生产开关。
