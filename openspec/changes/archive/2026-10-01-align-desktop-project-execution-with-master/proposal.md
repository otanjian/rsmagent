## Why

当前桌面“选择本机目录”既有 `crypto.getRandomValues` 脱离对象调用造成的 `Illegal invocation`，也存在行为偏差：选择后仅建立只读输入绑定，Skill 脚本仍在后端运行，产出不进入所选目录。用户明确要求本地与远程模式都恢复 `master` 的项目体验，并尽量复用其代码：选择目录即在该目录工作，原有 Skill 继续有效，业务产出直接保存在本机项目中。

## What Changes

- 以 GitHub `origin/master@8f1b19f1e72db0b46772f78f9c760b04b1836428`（2026-09-18，2026-09-30 已 fetch 核对）为行为与代码参考，复用项目选择、`project_dir` / `workspace_dir`、`apply_project_dir()`、SkillManager、现有文件工具与 Bash 实现，不整体覆盖当前企业化代码。
- 修复选择器两处随机数调用及不稳定安装标识；目录、会话和执行目标确认完成后才显示可用。
- 本地模式在登记的本机后端应用项目 cwd；远程模式保留服务器 Agent 编排，按需启动本机 Python 执行器，通过已有设备连接和命令服务转发项目工具调用。新增代码限于执行上下文、传输、技能资源部署、产出访问与必要隔离适配。
- 以原生选择形成的明确项目执行授权支持本机目录原地读写；普通 Web 的个人项目根限制、受控复制导入和旧版只读连接继续独立适用。
- 按原有 Agent 绑定与 `skill.use` 权限选择 Skill；远程技能的说明、脚本、模板及依赖清单按版本部署到本机缓存，业务 cwd 为所选目录。权限、运行环境和路径由真实执行端解析。
- 文件面板、`@`、工具、产出卡片及历史引用统一使用同一执行目标。新增本机产出引用、预览、系统打开和打开所在文件夹；不默认上传整个目录或产出，也不承诺工具结果不经过服务器/模型。
- 对文件写入、脚本、取消、重复投递及结果不明建立有副作用的命令契约，复用既有 ExecutionRun，不新增第二个 Agent 编排循环。
- **BREAKING（新能力显式启用后）**：用户选择“打开本机项目”时，项目相关工具及相对产出定位到本机目录，不再按旧只读输入路径在服务器生成产物。旧 grant、旧客户端、旧任务与历史文件不自动迁移或扩大权限；协议协商区分只读 v1 和执行 v2。

## Capabilities

### New Capabilities

- `desktop-project-execution`：本地/远程统一的本机项目语义、master 代码复用、逐轮执行目标、权限与生命周期。
- `desktop-skill-runtime`：技能选择、版本部署、本机路径及依赖、真实环境和上下文一致性。
- `desktop-execution-delivery`：远程工具投递、作用域、命令去重、取消、故障恢复和能力协商。
- `desktop-project-artifacts`：本机产出归属、文件面板/引用/预览、历史和显式传输。

### Modified Capabilities

- `scoped-project-browser`：在保留普通 Web 个人根与复制导入规则的前提下，增加原生授权的本机项目原地绑定；统一选择与执行的归属复核。
- `execution-isolation`：加入客户端项目执行目标的注册根及双端执行边界，明确本机执行不能以 cwd、字符串检查或现有配置开关充当脚本隔离验收。

## Impact

- **主要代码**：`desktop/src/main/{index.ts,remote-preload.ts,remote/*}`、`desktop/src/renderer/src/components/WorkspaceSelector.tsx`、`channel/web/static/js/{console.js,fork/desktop-host.js,workspace.js}`、`channel/web/fork/{runtime.py,handlers/workspace.py}`、`bridge/{agent_bridge.py,agent_initializer.py}`、`agent/{protocol,workspace,skills,tools,permission,prompt}`、`integrations/desktop/*`、协议/迁移/桌面打包及测试。新增 worker 放在现有 Agent 工具包旁，具体代码复用清单见 `design.md`。
- **唯一归属**：服务端拥有身份/授权、业务会话、ExecutionRun、命令状态和工件元数据；客户端拥有本机绝对路径、活动项目授权、技能缓存、执行进程及本机原始文件。客户端执行回执账本只用于效果确认与去重，不成为第二份业务任务真值。
- **依赖切片**：复用现行 `desktop-tenant-context`、`agent-runtime-capability-enforcement`、`resource-execution-authorization`、`audit-log`、`credential-management`、`action-approval`、`resource-quota` 与修改后的 `execution-isolation`。仅核对本能力实际消费的切片证据；不能因其他 change 尚未整体归档而搁置已具备前置的工作。
- **进行中 change 协调**：复用 `add-desktop-remote-web-workbench` 的配对、bridge、设备、网关与命令记录；保留其 v1 只读/固定解析器边界。`fix-desktop-local-context-and-tool-calls` 的来源引用保持兼容，本 change 新执行模式的本机产出规则优先；`use-personal-workspace-for-shared-agents` 决定未选项目时的后端默认目录；`guard-shared-knowledge-skill-writes` 的共享维护约束保留，项目业务产出文案消除冲突。归档协调清单见 `integration-map.md`，不将其他 change 的未完成实测冒充为已验收。
- **范围**：支持交互会话的文件读写和 Skill 脚本；不重写模型协议、租户认证、远程知识/MCP、调度器或外部 OpenCode，不实现后台唤醒设备、无授权定时执行、自动全量同步、任意服务器代码直通 preload 或数据全程不出本机保证。
- **交付状态**：本次只创建规划文档。生产代码、协议文件和 feature flag 尚未修改；所有实施及真实平台验收任务保持未勾选。
- **阅读入口**：[技术设计与 master 复用表](design.md)、[80 项实施任务](tasks.md)、[36 项验收矩阵](acceptance.md)、[执行协议草案](execution-contract.md)、[跨 change 协调](integration-map.md)。六份规格位于 `specs/`，覆盖 30 条需求与 63 个规范场景。
