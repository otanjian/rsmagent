## Why

当前记忆管理默认按智能体解析到租户共享根，个人请求又可能被追加 `agent_id` 而被拒绝；个人写入工具还存在只写检索索引、不生成可管理文件的问题，导致账号已有记忆却显示为空。需要以 master 的记忆功能代码为起点，提供当前登录账号在当前租户内一致、可管理、可检索的个人记忆。

## What Changes

- 以已核对的 `origin/master@48c0d79c36146950667f8d1884ab823deae62305`（2026-10-02）为功能实现基线，基于其记忆列表、详情编辑器、模板、记忆服务及自动固化代码进行适配；接入 RongAI 现有认证、个人域和索引一致性服务。实施时记录 master 文件与实际加载模块的映射，不以旧 `console.js` 局部补丁代替 master 功能接入。
- **BREAKING（页面目标选择）**：正式“记忆管理”固定展示当前租户、当前登录账号的个人记忆，移除 Agent 下拉框及个人/智能体目标切换；普通成员和管理员使用相同页面。显式指定 Agent 的旧接口保留受授权约束的兼容语义，不再作为该页面的数据来源。
- 保留 master 的文件列表、类型/大小/更新时间、分页、Markdown 查看、编辑/保存/退出、快捷保存、未保存提醒及冲突处理；“自主进化”合并展示本人的进化记录和梦境日记，支持 master 原有的文本查看与编辑。
- 接通现有个人记忆删除、清空与索引恢复能力。删除作用于单个本人记忆条目；页面“清空我的记忆”明确覆盖本人长期/每日/主动记忆及进化、梦境记录，不影响共享资源、聊天历史或个人人设。
- 正式记忆接口委托同一 `PersonalMemoryService`；个人请求不得携带自动注入的 Agent 目标。加载失败、无权限、索引待恢复和真实空数据分别反馈。
- 统一 `memory_add(scope=user)`、自动摘要、梦境整理、进化记录和人工修改的账号归属与最终提交协议。个人 Markdown 文件是内容真值，检索索引是派生数据；任务不得在编辑或清空后提交过期结果。
- 提供可预览、可备份、可重复执行的历史迁移：直接保留已有个人文件，仅迁移可信归属明确的旧个人文件/索引记录；不向账号复制共享 Agent 记忆，不把残缺索引分块伪称为原文。

## Capabilities

### New Capabilities

无。复用现有记忆、认证和存储能力，不新增第二套个人记忆系统。

### Modified Capabilities

- `database-memory-console`：正式页面固定本人目标；保留 master 管理体验；扩展个人进化分类、可写动作和清空范围；明确加载状态、旧接口兼容及账号切换行为。
- `user-personal-context`：统一个人记忆及自动产物的归属、提交和跨智能体一致性；调整正式管理入口；规定历史个人数据迁移与恢复边界。
- `agent-memory-explicit-add-tool`：个人写入必须先进入统一文件/索引发布流程，准确反馈完整成功、待恢复和失败；无可信身份拒绝写入。

## Impact

- master 来源：`channel/web/api/memory.py`、`channel/web/templates/views/memory.html`、`channel/web/static/js/views/memory.js`、`channel/web/static/js/views/doc-viewers.js`、`channel/web/static/js/doc-editor.js`、`agent/memory/service.py`、`agent/memory/summarizer.py`、`agent/evolution/record.py`。
- RongAI 接入：`channel/web/memory_console.py`、`channel/web/fork/handlers/memory.py`、真实页面模板/脚本装配与请求封装、`agent/memory/personal.py`、`agent/memory/manager.py`、`agent/tools/memory/memory_add.py`、后台身份传递及索引提交接缝。
- 唯一数据归属：可信 `(tenant_id, user_id)` 对应 `user_root()`；Agent 只作为记忆产生的来源信息，不能决定个人存储根。共享、会话和个人作用域继续分别授权。
- 前置切片：既有运行时身份、个人安全文件访问、记忆版本/恢复协议、功能开关和脱敏审计须以实际测试证据验收。Web 与 Desktop 登录/切租户的集成验收需核查 `fix-desktop-relogin-session-sync` 的相关证据，不以整份 change 状态代替。
- 与 `rename-capabilities-menu-to-center` 无功能依赖，但页面模板及入口可能重叠，实施需保留已有菜单更名。不顺带合并 master 无关功能，不覆盖运行时配置或用户数据。
- 实施状态与证据见 `tasks.md`、`evidence/implementation.md`；真实账号数据只做只读预览，未执行迁移。
