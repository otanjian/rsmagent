# 生产准备检查基线

记录日期：2026-10-02（Asia/Shanghai）。此文件记录 change 创建前同一会话的检查结果，用于安排实现和回归；不是实现完成或生产验收证明。

- 工作区：`rsmagent`，分支 `rdai`，HEAD `31afbad9f08f7954d7258d9045b2b773c40fc5ec`，检查时存在用户未提交修改。未固定完整工作区快照，不能把该提交单独称为全部被测代码。
- 本机：macOS，项目 Python 3.14.3。未进行生产 Linux 压测、完整远程验收或灾备恢复。Windows 环境不满足，用户明确暂缓该桌面测试。
- 所有安全复现只使用临时目录、合成文件和模拟凭据；未读取真实密钥值、调用真实业务外部系统或修改业务实现。

## 合成复现

| 项目 | 方法与观测 | 证据边界 |
| --- | --- | --- |
| 计算路径绕过 | 构造合法根及被阻断测试根；明文 `cat` 被拒绝，解释器计算生成相同路径通过 `_check_bash`，随后真实 Bash 工具读出 `SYNTHETIC_OTHER_TENANT_DATA` | 隔离检查＋实际工具执行，不是经真实用户 HTTP 登录的完整攻击链；实现须补真实授权链回归 |
| 部署环境泄漏 | 将主密钥环境变量替换为 `SYNTHETIC_DEPLOYMENT_KEY`，真实 Bash 工具读取成功 | 模拟秘密；未输出真实部署凭据 |
| 身份库备份缺失 | 在临时 data 根创建配置及合成 `identity.db`，调用现有 `create_backup_archive` | 输出仅含 manifest 和配置，数据根身份库不在包内；外部接入专项备份不能代替全实例 |
| 限流永久容量占用 | 容量设为 2、时间窗 10 秒，填满后将时钟推进到 10000 秒并访问旧桶促使清理，再测试全新账号/来源 | 仍拒绝，retry_after=1，账号和来源键仍各为 2；需要回收空键 |

临时原始输出曾位于 `/tmp/rsmagent-prod-audit.U37sKz/`，属于可能被清理的会话文件，不作为唯一长期交付证据。实施阶段需生成独立脱敏复现/验证记录。

## 抽样检查

| 检查 | 当时结果 | 解释 |
| --- | --- | --- |
| 认证、HTTP gate、路由、执行隔离、授权失败、限流、租户、外部连接、CLI 备份组 | 178 passed、2 failed、2 subtests passed | 非全量；两项失败见下文 |
| 追加审计 Python 组，包含上述两项失败重跑 | 71 passed、2 failed | 与前组有重叠，不直接相加为完整测试计数 |
| 审计前端 `test_audit_console_frontend.cjs` | 3 passed | 仅该文件 |
| 路由覆盖 | 230 routes、281 methods，OK | 不代表全业务链通过 |
| 模块接缝 | 22 upstream modules、364 fork-only symbols、0 findings | 静态接缝检查 |
| Desktop renderer 类型检查 | 9 个 `ChannelsPage.tsx` 图标类型错误 | 构建脚本未以 renderer 类型检查为独立门槛 |

两项失败不能直接记为新生产越权：

1. `test_bash_read_outside_home_blocked` 的合成边界未统一 macOS 临时目录别名与 realpath；将边界 canonicalize 后检查拒绝。真实边界解析已有 realpath，需修夹具并保留拒绝属性。
2. `test_a_tenant_catalogue_read_needs_the_read_permission` 预期默认 member 无读取权限，但现行默认权限与迁移已授予该权限。需创建真正缺权限的角色进行负向测试，不能简单删除 403 断言。

## 静态部署观察

- 根 Dockerfile 和默认 Compose 指向上游镜像，当前本项目代码没有由这两个默认入口构建发布。
- `docker/Dockerfile.latest` 使用 `ADD .`，根目录没有 `.dockerignore`；本地存在配置、身份库、日志、密钥文件及备份。未实际构建或发布含秘密镜像。
- 生产依赖版本未完整锁定，构建删除了系统安全仓库配置；尚未执行 CVE 扫描，不能据此声称某个具体 CVE 已存在。
- 主日志是普通 FileHandler；存活接口返回固定 ok；默认 Compose 缺少重启/健康和资源配置。不能据此推断部署外部没有监控、TLS 或备份，实际环境需核查。
- 当前 GitHub workflow 不证明 Gitea/main 的发布保护已生效；既有 `docs/design/master-sync-20261002/validation.md` 也明确保留远程/安装包验收缺口。
