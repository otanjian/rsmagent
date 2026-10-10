## Why

生产准备检查已复现原生 Bash 参数级隔离可被计算路径绕过、子进程可读取模拟部署主密钥、登录限流容量过期后不释放、整实例身份数据未进入通用备份等问题，构建和发布入口还存在敏感文件进入镜像及上游镜像错配风险。需要在保留现有功能的前提下完成必要修复，并形成可重复的部署、恢复和发布验收。

## What Changes

- **保留功能**：保持已授权普通成员、租户管理员和平台管理员的既有能力，保留 Bash、脚本技能、前后台任务、文件操作、授权网络访问、外部系统接入及取消/输出接口；不得以关闭功能、改为仅管理员可用、撤销既有权限或缩减验收来完成修复。
- **P0 执行与凭据**：在真实进程启动接缝补齐 OS 强制隔离，复用现有 worker、进程生命周期和环境构造中的可用实现；前台、后台和派生进程采用相同边界。按原有身份与资源授权注入任务所需凭据，保留技能变量名及用途，隔离部署主密钥和无关凭据。
- **P0 镜像**：排除运行数据、凭据、日志和本地缓存，采用明确的运行文件复制集合；生产构建来自干净源码，检查最终镜像与历史层。
- **P1 身份保护**：修复限流过期空桶回收；统一 HTTP 鉴权遇非预期异常返回 503，禁止继续调用业务处理器。
- **P1 部署与恢复**：统一当前项目的镜像构建与 Compose 入口，固定生产依赖及基础镜像；增加停机整实例备份/恢复模式，覆盖身份库与全部实际数据根，保留现有工作区备份兼容性。
- **P1 运维**：增加日志轮转、轻量就绪检查、重启与持久化配置，验证 HTTPS 代理和可信代理来源；保留现有桌面存活探针。
- **P1 发布验收**：建立可在本机运行、再接入实际交付仓库的统一检查入口；修复桌面 renderer 类型错误和已确认的测试契约问题，完成服务端、Web、macOS 正向功能与安全负向验收。
- **范围约束**：本期保持单机、单进程、SQLite；暂缓 Windows 桌面测试并明确记录未执行，不影响其现有功能代码，也不宣称其已验证。多 worker、分布式队列、数据库迁移和在线增量备份不纳入本次修复。

## Capabilities

### New Capabilities

- `production-deployment`：当前项目的可复现镜像、敏感数据构建隔离、基础运行配置、日志轮转与就绪检查。
- `instance-backup-recovery`：维护窗口内完整、一致、可校验的整实例备份与异目录恢复。
- `production-release-validation`：保留功能的生产准入、目标平台证据、固定候选产物及回滚记录。

### Modified Capabilities

- `execution-isolation`：明确 OS 执行边界、前后台及派生进程覆盖、个人资源作用域，以及功能保持和候选切换门槛；保留外部共享 OpenCode 既有例外范围。
- `credential-management`：补充进程环境按用途构造、授权技能变量兼容和部署凭据隔离。
- `identity-session`：补充有界登录限流的过期容量回收与并发行为。
- `enterprise-access-enforcement`：补充统一鉴权在非预期身份解析异常时不可降级的行为与故障恢复验收。

## Impact

- 接入点：`agent/permission/isolation.py`、`agent/protocol/agent_stream.py`、`agent/tools/bash/`、`agent/desktop_local/worker.py`，以及已有 Desktop 隔离启动器中的可复用逻辑。新增服务端实现保持在 fork 自有模块，通过窄接缝接入，保持工具、ExecutionRun、scheduler 和 Desktop 的公共契约。
- 身份与运维：`auth/ratelimit.py`、`auth/http_policy.py`、`cli/commands/backup.py`、`common/log.py`、fork Web handlers/路由清单、Docker 构建与 Compose、生产依赖清单及检查脚本；桌面只修必要类型与兼容问题。
- 数据唯一归属不变：`identity.db` 管理身份、成员、角色、凭据引用和审计；原业务库及登记工作区拥有会话、任务和文件；隔离启动器只消费经验证的执行上下文，不成为第二套身份、权限或任务状态存储；备份清单描述快照而不接管运行时所有权。
- 依赖现行 `resource-execution-authorization`、`tenant-resource-isolation`、`audit-log`、`action-approval`、`resource-quota`、`fork-upstream-decoupling` 与 Desktop 相关规范。实施前验证所需现有切片，不等待整份历史 change 完成；真实执行隔离和恢复能力不能用 mock 或本文档完成状态代替。
- 与进行中的 `fix-desktop-relogin-session-sync`、`fix-account-memory-management` 仅在验收和共享代码处协调，复用其证据时标注构建与实际范围，不代勾其未完成任务；不覆盖当前其他未提交修改。
- 部署依赖只为实际服务端目标选择一个经验证的隔离后端。现有 macOS 本地执行沙箱具有禁网语义，不能原样替代需要访问外部系统的服务端执行；具体运行条件和选择门槛在 design 中说明。
