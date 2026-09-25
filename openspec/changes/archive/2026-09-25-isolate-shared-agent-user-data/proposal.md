## Why

同一租户的多人共用 Agent 时，文件页面和文件接口无法区分各自上传及生成的文件。本次按用户确认的最小范围，在当前租户共享 Agent 工作区增加 `user/<user_id>/`，使用户通过 RongAI 平台文件页面和文件 HTTP 接口只能访问自己的用户子目录。

## What Changes

- 在 `<租户共享 Agent workspace>/user/<不可变用户ID>/` 下保存本人的上传、回传结果及工作文件；账号名称仅作展示，tenant/Agent/user 由服务端可信身份及绑定确定。
- 复用现有文件入口、功能/资源权限和 ObjectScope，只增加统一用户目录归属判断；不新增文件数据库、逐文件 grant 或 file.read/file.write 权限点。
- **BREAKING**：任何文件入口命中 `user/<user_id>/` 时均需本人身份，Agent 共享、父目录可访问、管理员资格、旧链接或目录 token 不再直接放行。目录树、搜索、计数、下载、预览及既有修改/打包操作执行同一规则。
- 现有文件面板继续浏览获权的当前 Agent 目录；展开 user 时只看到本人子目录，递归搜索不扫描其他账号。共享配置与公共资料继续执行原有权限，不能把整个 Agent 根默认变为可读。
- Web 上传与平台回传文件使用本人目录；已在该目录的文件直接引用，平台收集的结果按可信请求用户归档。私人 send 不自动复制到公开网站，下载/预览经登录鉴权。
- 已知归属的旧上传/结果通过轻量清单迁移或修正引用；未知归属不猜测分配，留在不可经普通文件接口访问的旧数据保留区。
- **范围明确**：Python、Shell、技能脚本和 coding 保持既有开放方式及授权，不引入执行沙箱或新增隔离验收门槛；不保证租户共享智能体内部脚本直接读盘时的用户隔离；同一共享智能体的终端执行和 OpenCode 原生文件服务不纳入平台文件接口保证，不检测程序自行复制文件内容的行为。此范围限定不改变既有跨租户、私有 Agent 和系统敏感路径权限规则。
- 排除 Desktop、运行环境/模型缓存/个人记忆重构、渠道体系改造、OpenCode 实例与反向代理改造、文件版本/分享和复杂预览建设。没有可信用户的后台入口不得通过平台发布“私人文件”，其执行能力保持原状。

## Capabilities

### New Capabilities

- `agent-user-file-directories`: 租户共享 Agent 内用户子目录、可信文件落点、页面/API 归属判断、轻量历史处理及执行能力兼容边界。

### Modified Capabilities

- `platform-file-browsing`: 平台文件页面和 HTTP 入口识别 Agent 的 user 子目录；递归过滤、本人预览下载及旧链接不绕过归属。
- `tenant-resource-isolation`: 在原有租户/Agent 文件授权中补充用户子目录优先规则；Agent 共享或可见性转换不公开子目录，允许后续已知文件的限定迁移。

## Impact

- 主要接入点：`common/state_dir.py` 增加 Agent 用户目录帮助函数；`auth/object_scope.py` 增加归属判断；实际 Web fork 的 `runtime.py`、`handlers/files.py`、`handlers/workspace.py` 及现有路由登记收口文件访问。
- 回传链路：Web 上传/语音/生成结果、artifact、send 与 `common/cloud_client.py` 的自动网站复制分支；仅调整平台落点和文件链接，不改执行器权限或 OpenCode 原生交互。
- 前端：复用既有文件面板、目录树和预览下载；无需新增文件应用或完整 CRUD API。
- 与 `refine-workspace-panel-agent-root` 保持当前 Agent 落点兼容，只增加 user 子树过滤；验证 `show-and-toggle-agent-visibility` 转换后本人目录判断仍优先，无需等待其无关任务。coding 原共享服务方案继续保留。
- 按动作复用已存在身份、审计、配额和写来源校验；本 change 的验收是平台文件访问验收，不是执行环境安全验收。文件访问规则不可通过功能开关关闭。
- 本次重写替代此前同名 change 的全链路隔离设计，保留 change 名称；交付范围是上列平台文件面（不含执行层隔离），实现与验证证据见 `evidence.md`。
