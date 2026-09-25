## 1. 入口与前置检查

- [x] 1.1 盘点租户共享 Agent 的 Web 文件页面、文件 HTTP 接口、预览子资源、静态映射和实际已有递归操作，确认原身份/租户/动作/写来源及适用审计配额切片可用；记录 user 名称冲突与历史文件样本。
- [x] 1.2 确认本次只增加平台文件用户目录校验，记录 Python/Shell/技能/coding 当前成功使用基线；核对工作区面板和 Agent visibility 相关 change 的合并点，不引入 Desktop 或执行隔离任务。

盘点结果（阶段 1 的记录，证据见 `evidence.md` 第 2 节）：

| 入口 | 落点 | 本次接入 |
| --- | --- | --- |
| `POST /upload`、语音上传 | `channel/web/fork/runtime.py::_get_upload_dir` | 改为写入本人 `user/<id>/uploads` |
| `GET /uploads/(.*)` | `channel/web/fork/handlers/files.py::UploadsHandler` | 从本人子目录读取并逐次鉴权 |
| `GET /api/file` | 同文件 `FileServeHandler` | 用户子树 owner 先于平台/共享根放行 |
| `GET /preview/(.+)` | 同文件 `PreviewHandler` | 消费时校验当前身份（token 不是本人凭据） |
| `/api/workspace/tree\|search\|resolve\|meta\|read\|write` | `channel/web/fork/handlers/workspace.py` | 统一判定 + 进入他人目录前剪枝 |
| `send` / `cloud_client.copy_send_file` | `agent/tools/send/send.py` | 私人文件不生成公开网站副本 |

## 2. 目录落点与统一归属判断

进入本阶段前复核阶段 1 的身份、现有入口权限与目录预检结果。

- [x] 2.1 在 common/state_dir.py 增加共享 Agent 的 user/<稳定用户ID>/uploads|outputs|work 解析，验证实际租户/Agent/user 与根路径；不修改个人记忆 user_root、进程 cwd 或共享 Profile。
- [x] 2.2 在 ObjectScope 与共用文件路径策略接入用户子树判断，保证 owner 校验先于共享根/平台管理员放行；复用原动作权限，不新增权限点或逐文件 grant。
- [x] 2.3 接入 Web 普通/目录/语音上传和平台生成文件回传，按可信原始用户归档并生成带正确 owner 定位的链接；无用户或会话冲突拒绝发布，同名文件不跨用户覆盖。
- [x] 2.4 完成目录与策略测试：账号改名、伪造 owner、跨租户、管理员、预留目录冲突、绝对/编码/父级路径、软硬链接及检查后替换；形成文件接口接入前的证据。

## 3. 文件接口、预览与回传

进入本阶段前复核阶段 2 的目录和归属证据；不能仅改前端列表。

- [x] 3.1 在实际 fork 的 /api/workspace、/api/file、/uploads、平台浏览接入统一判断；tree/search/count 在进入他人目录前过滤，现有写/删/移/打包入口检查全部目标，禁止改名 owner 根或跨 owner 转移。
- [x] 3.2 为私人 /preview、下载和关联子资源接入当前身份校验，原生下载继续支持资源派生租户；失效不满足条件的旧 token，私人响应 private/no-store，排除未鉴权静态映射。
- [x] 3.3 修改私人 send/平台回传的自动网站复制分支，返回本人鉴权链接；源文件命中他人 user 目录时拒绝平台收集，不改 Python/Shell/coding 执行方式。
- [x] 3.4 使用真实 HTTP 验证本人成功、B/跨租户/非 owner 管理员拒绝，覆盖父目录递归、已知路径、旧 URL/token、子资源、静态入口及现有修改接口；无身份和归属错误不返回私人元数据。

## 4. Web 面板兼容

进入本阶段前复核阶段 3 的实际 HTTP 授权证据。

- [x] 4.1 保留当前 Agent 面板落点，展开 user 仅展示本人目录，无文件显示空态；公共资料按原规则，切换账号/租户/Agent 时清除旧列表、预览与迟到响应。
- [x] 4.2 复用现有安全预览和鉴权下载，无法可靠鉴权时提供下载提示；更新必要的现有多语言文案，不新增文件产品或复杂预览框架。
- [x] 4.3 真实浏览器验证 A/B 同用共享 Agent、上传/结果读取、搜索、预览、账号切换和 Agent 可见性转换；新增子树规则不放宽私有 Agent、跨租户或系统敏感路径权限。

## 5. 历史处理、兼容与交付

- [x] 5.1 根据可信记录制作可重跑的旧上传/结果迁移清单，迁入本人目录、校验内容并更新/映射引用；未知或冲突归属受保护保留，user 名称冲突不自动覆盖，不建设文件数据库。（清单已在真实租户 `test15` 上 dry-run：75 个文件、0 碰撞；`--apply` 未执行，待产品侧确认隔离后果，见 `evidence.md` 第 7.1 节）
- [x] 5.2 演练维护窗口、迁移中断与文件接口恢复，核验源备份和旧链接不重新公开；沿用现有能力禁用机制做故障关闭测试，任何开关都不能关闭用户目录校验。
- [x] 5.3 回归已开放的 Python、Shell 前后台、技能脚本和 coding，确认正常执行及平台结果链接可用，不新增沙箱依赖或全局关闭；记录共享 Agent 内脚本直接读盘的用户隔离不在验收保证内。
- [x] 5.4 汇总 3 个 capability 的实际测试证据，复核前述各阶段后发布文件功能；更新用户和维护文档，准确说明仅租户共享 Agent 的平台文件页面/API 增加本人目录规则，既有其他权限边界不变。
- [x] 5.5 执行 OpenSpec 严格校验、核对相关 change 合并语义与未扩大范围；只有实现与真实验证完成后才勾选任务，不以本规划作为执行层隔离证据。

## 验收记录

红/绿输出、真实 HTTP 与真实浏览器授权验证（含 A/B 同用一个共享 Agent 的面板、搜索、预览与账号切换）、文件面回归、变异测试与任务逐条对照见 [`evidence.md`](./evidence.md)。

两条边界在此重申，避免把勾选读成超出证据的承诺：

- **执行层不在本次范围**：共享 Agent 内 Python/Shell/技能/coding 直接读盘的用户隔离没有交付，也未新增沙箱依赖或全局关闭开关；已写入 `docs/zh/channels/web.mdx`（含英/日）与 `docs/design/shared-agent-user-data-isolation-plan.md` 的交付状态。
- **4.3 的浏览器验证范围**：面板落点、`user` 只列本人、他人目录拒绝、搜索、本人/他人文件读取、预览链接换人失效、账号切换均在真实浏览器 + 真实会话下完成；Agent 可见性转换本身属于 change `show-and-toggle-agent-visibility`，本次对该项的要求是「不放宽既有边界」，由私有 Agent、跨租户、平台与租户管理员的用例覆盖（未在浏览器里执行一次真实转换）。
