## Why

桌面使用者希望文件解析、业务计算、技能脚本及 Agent 工具循环尽可能运行在自己的电脑，同时继续使用 `rdai` 的统一账号、权限、企业知识库和共享 Skill，并在桌面、Web 和多台设备之间使用同一份会话、个人知识和个人 Skill。`master` 已有本机 Python 后端架构，但当前企业远程模式把 Agent 循环留在服务器，已有设备命令通道也不能直接提供“本地 Agent 主动消费企业服务”的完整能力，需要在同一 RongAI 运行时中补齐本机执行和服务器统一存储。

## What Changes

- 新增明确选择的 `enterprise-local`（企业连接·本机运行）模式：复用现有桌面 Python 后端、Agent Registry/Bridge、模型循环和工具接口；本机负责对话编排、项目文件、计算和成果生成，服务器提供企业服务。保留既有 `local`、`remote`、Web 和 Channel 行为，不自动迁移存量安装。
- 分离本机运行时地址与企业服务 HTTPS origin，复用原生 PKCE 登录和主进程 broker。服务器继续唯一持有账号、Membership、角色、资源 grant、企业 Agent 配置及公共凭据；桌面不复制身份库、不建立第二套角色权限，也不恢复上游共享密码或免登录旁路。
- 为本地 Agent 提供获权 Agent 配置、逐动作授权、企业模型调用、共享知识检索、Skill 目录与包分发、企业工具调用及必要个人状态访问的窄化服务接口。沿用既有审计、审批、硬配额和 ExecutionRun；服务器不因此启动第二个 Agent 循环。
- 企业知识默认在服务器按租户、owner、Agent 绑定和资源资格检索，仅返回必要片段及来源；本机项目索引留在本机，不默认复制整库或上传原始项目文件。
- 共享 Skill 由服务器发布、授权并锁定版本，桌面按需下载完整指令/模板/脚本包到隔离缓存。可移植计算在本机执行；依赖企业凭据或服务器业务连接的步骤调用受控企业工具，不向本机下发公共秘密。
- 将本机计算与业务 API 解耦，首批落地固定 CSV/XLSX 处理、财务分析 Skill 及排产导入的代表性链路；显示实际执行位置、数据传输和产物归属。脚本隔离按真实平台单独验收，本机故障不得静默改为服务器计算。
- 新模式默认增量同步会话消息、必要工具结果和会话管理状态，服务器保存唯一已确认记录，桌面保留缓存、草稿和持久待同步队列。Web 与桌面使用同一业务会话标识；同一会话同一时刻只有一个执行者，空闲且状态确认后可切换运行位置。
- 个人知识正文/显式导入的资料、个人 Skill 完整定义与资源包、允许跨端的非秘密个人配置统一保存到服务器，桌面编辑经版本校验提交。按租户和本人范围隔离，同步不自动共享；补齐个人 Skill 定义的存储与维护，不能只同步使用参数。
- 同步覆盖断网补传、幂等重试、双向增量拉取、版本冲突、删除标记、存储限额和账号切换。原始项目文件、大型产物及本机索引仍留在本机，用户明确选为个人知识/附件后才按用途上传；旧纯本地历史与资源通过显式导入纳入统一存储。
- 保持 Web 原有地址、API、权限、默认服务器执行和存量数据语义；其正常写入也进入统一变更记录，桌面关闭、同步服务暂停或新能力未开放时仍可正常使用。将 Web 与桌面交叉读写及回归列为发布门槛。
- 企业资源首期在线重验，缓存和待同步草稿不构成离线执行权限；账号/租户切换隔离待同步队列，不把旧身份数据提交到新身份。

## Capabilities

### New Capabilities

- `desktop-enterprise-local-runtime`: 企业连接下的本机 Agent 生命周期、数据归属、运行位置和存量迁移。
- `desktop-enterprise-service-access`: 本地 Agent 消费企业服务的受信调用上下文、逐次授权、模型/工具代理、执行登记与计量。
- `desktop-shared-knowledge-access`: 服务器知识检索、来源引用、私有范围和本地索引共存。
- `desktop-shared-skill-delivery`: 获权技能发现、完整包分发、版本缓存及本机/企业服务执行兼容。
- `desktop-local-compute-routing`: 本机业务计算的统一派发、首批场景、资源限制、平台能力和可观察验收。
- `desktop-account-data-sync`: 会话及个人资源的服务器权威存储、多端增量同步、冲突/删除/恢复和同步可见状态。

### Modified Capabilities

- `desktop-tenant-context`: 增加企业连接与本机运行时并存时的双地址身份边界、统一上下文代际和生命周期失效规则；原有 PKCE、原生凭据及逐请求租户验证契约保持。
- `session-history-workbench`: 同一账号范围内呈现跨端会话，保留历史管理与 Web 默认运行合同，增加运行占用和本机附件可用性反馈。
- `tenant-knowledge-console`: 本人自有知识的跨端版本化维护与索引同步，保持实际数据根、owner 和共享库写权限。
- `tenant-skills-tools-console`: 在现有页面维护本人私有 Skill 定义和完整资源包，统一版本与删除状态，保留公共管理及旧正文接口兼容。

## Impact

- 桌面接入点：`desktop/src/main/index.ts`、`python-manager.ts`、`auth-broker.ts`、`remote/profiles.ts`、`local-execution/`、`project-execution/` 及 renderer 的连接、对话、资源页面。
- Python 接入点：`agent/protocol/`、`agent/skills/`、`agent/memory/`、`agent/knowledge/`、`agent/desktop_local/`、模型统一分发及 `common/runtime_identity.py`。企业逻辑通过 fork/provider 接缝接入，遵守 `fork-upstream-decoupling`，不整文件覆盖上游。
- 服务端 API 通过 `channel/web/route_registry.py` 的唯一清单登记，复用 `channel/web/fork/`、`auth/` 和 `integrations/desktop/` 的真实授权服务。已有命令绑定的 Skill 下载协议继续兼容，新模式不伪造远程执行命令。
- 数据唯一归属：服务器保存企业身份/权限、企业 Agent 定义、共享知识/Skill、会话和个人知识/Skill 的已确认版本、非秘密个人状态、审批/配额及审计；桌面保存按企业来源、租户、用户隔离的缓存、草稿、待同步操作和本机项目/产物。设备是来源与执行者标记，不另建一份云端会话身份；未确认数据明确标为待同步。客户端执行回执不冒充服务器亲自观测的证据。
- 存储接缝：扩展现有 conversation/session、知识源和 Skill 存储及 Web 写入口，增加稳定标识、版本、变更记录、幂等收件记录和删除标记；不复制身份库，也不把同步库变成第二套业务权威。文件类资源需完整校验后提交，适配既有文件存储的恢复流程。
- 跨 change 依赖：身份与 Desktop 实机验收、`resource-execution-authorization`、`agent-runtime-capability-enforcement`、`credential-management`、`audit-log`、`action-approval`、`resource-quota`、`token-usage-console` 的相关切片必须逐项复用并验证。脚本/凭据路径与进行中的 `harden-production-readiness` 协调；其 Linux/macOS 证据不代替本 change 的 Windows 验收，反之亦然。
- 与 `add-sap-workbench-scene` 仅共享通用接缝，不修改其未提交工作、不将 SAP 本机自动化作为本 change 的完成前提。此次交付为 proposal/design/specs/tasks，不修改代码、部署配置、角色或现有能力开关。
