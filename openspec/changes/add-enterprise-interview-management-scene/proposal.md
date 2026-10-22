## Why

“企业智能体规划顾问”的访谈已保存在现有会话中，缺的是让指定角色查看该智能体全部用户会话的权限，以及从会话手动生成报告的入口。扩展现有角色授权和历史页即可满足需求。

## What Changes

- 新增功能权限 `history.read_all`（查看指定智能体全部会话）及 Agent 资源动作 `history_read_all`，复用现有“功能权限＋资源授权”机制。角色同时具备既有 `history.read`、新权限及明确目标 grant 后，可只读当前租户该共享普通智能体的全部可信已保存会话；内置角色不自动增权。
- 现有角色页配置目标智能体；历史页增加“我的会话 / 获授权智能体的全部会话”和目标选择，展示创建人。复用现有标题搜索、归档筛选及分页，不新建场景应用或企业档案。
- 他人会话在历史页内展示完整只读问答；不授予发送、修改、删除或代替受访者确认的能力。本人继续访谈仍走原智能体，默认及旧客户端仍只查本人范围。
- 为 `enterprise-planning-advisor` 的会话增加“生成调研报告”：以请求者现有运行/模型权限及配额执行一次受控模型调用，成功后在当前页面预览并下载 Markdown，不向原访谈追加消息。
- 报告仅分析完整已保存问答，涵盖企业岗位、工作输入/方法/输出、频率耗时、错误风险、资料缺口及高价值可落地场景。附件原文件不解析，正文已有的资料摘要按访谈陈述引用，不声称已核验原件。
- 报告按需生成，不在服务端留存；页面提示下载留存。取消报告表、后台任务、轮询恢复、版本列表、来源更新监听和附件权限联动。超长输入明确提示限制，超时或断开后由用户手动重试。

## Capabilities

### New Capabilities

- `enterprise-research-reports`: 从单会话完整问答即时生成有来源和风险分析的调研报告，提供当前页面预览及 Markdown 下载，明确运行授权、失败和不自动保存的边界。

### Modified Capabilities

- `business-permission-catalog`: 登记 `history.read_all`，保持内置默认集合不变，明确不隐含写入或执行。
- `role-resource-authorization`: 为指定共享普通 Agent 增加 `history_read_all` 动作，复用角色事务与撤权规则；管理员通用资源快捷放行不能替代目标授权。
- `rbac-authorization`: 增加显式授权的共享 Agent 历史跨 owner 只读例外，保持其他个人资产及写动作边界。
- `session-history-workbench`: 扩展历史页查询范围、创建人展示和他人会话只读详情，区分读取与写入授权。
- `platform-all-authorization`: 为平台 all 增加 `history_read_all` 的显式 grant 例外，其余 all 语义不变。
- `audit-log`: 登记租户内跨 owner 历史读取事件（`session.history.read_all`），不记录问答正文。

## Impact

- 修改点集中在 `auth/policy.py`、现有角色资源配置、历史页、`channel/web/fork/handlers/sessions.py`、会话授权及 ConversationStore 的读取路径。复用现有模型客户端，不另建运行平台。
- 角色/grants 仍由身份服务维护；访谈记录仍由原 ConversationStore 维护，owner 不变。报告仅作为本次响应和浏览器当前页面内容，下载后由用户保存；不新增数据库表、报告目录或共享知识库内容。
- 依赖现有身份/租户、审计、CSRF、模型凭据、运行授权和硬配额的真实切片；读取和生成依次验收，使用现有配置机制中的一个新功能开关控制上线。没有新增任意代码、Shell、MCP 或外发工具能力。
- 并行 change 的依赖与归档顺序见 Dependencies；不承担远端历史同步或无归属旧数据修复。本次仅修改 OpenSpec 产物，沿用原 change 标识。

## Dependencies

- 依赖 `fix-coding-session-history-management`：两者均 MODIFIED `session-history-workbench` 的“保留历史列表及其管理操作”。归档顺序 SHALL 显式确定，后归档方 MUST 以先归档方结果为基线重写该 MODIFIED 块并重跑 `scripts/check_change_deltas.py`；本 change 不预先合并对方文本。
- `enterprise-planning-advisor` 必须已作为该租户绑定的共享普通 Agent 存在（`private_owner_user_id` 为空）；其供给不在本 change 运行时范围内，缺失时报告入口不出现。
- change 标识沿用历史命名；本次不新增 scene capability，因此没有 `scene-*` delta。
