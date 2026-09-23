## Why

当前多智能体对话入口隐藏在历史页的新对话菜单中，侧栏的新建、功能导航和会话历史缺少清晰层次。需要让侧栏顶部的单一「新建对话」控件与会话历史页「新对话」一致地发起单人或团队对话，并修正团队选择器混入 coding 智能体及成员尚未保存就切换会话的问题。

## What Changes

- 工作台侧栏采用「品牌与快速发起 → 功能导航 → 最近会话 → 底部控制台与账号」结构；顶部只放一个「新建对话」控件：主按钮直接开始单人对话，右侧折叠箭头展开选择菜单（逐个智能体的单人对话与「多智能体对话」），与会话历史页的「新对话」共用同一份菜单实现。功能导航顺序为对话、智能体、场景应用、知识库、定时任务。
- 最近会话默认显示按既有授权与排序规则取出的前 5 条，标题区提供「查看全部」；保留已归档入口、重命名、归档和完整历史页的原有能力，沿用历史页路由。
- 复用团队选择弹窗，增加名称/职责搜索、已选人数、显式默认响应者及状态反馈；至少选中 2 位不同的合格智能体才可开始。
- coding 智能体在所有团队成员选择界面中完全排除，包括创建弹窗、搜索结果、预选、默认响应者候选、已选摘要、会话内邀请和团队 @ 候选；不显示禁用行、类型提示行或编码智能体名称。普通单智能体编码入口、智能体目录和编码历史保持可用。
- 当前处于 coding 会话时仍可从侧栏发起普通智能体团队，不将当前 coding 对象预选进团队；取消保持原编码会话。
- 团队启动采用准备、校验、保存、提交视图的顺序，成员保存成功前不切换活动会话或开放新会话输入；处理重复点击、失败重试、取消离页和身份切换。服务端独立校验默认响应者与全部成员的类型、可用性和使用权限。
- 保留现有会话 owner 与 members 数据模型、@ 指定成员及委派方式，支持主题、三语、键盘和移动端。

## Capabilities

### New Capabilities

- `workbench-sidebar-launch`: 规定侧栏四区布局、单一启动控件与其选择菜单、导航选中态、响应式与发布回退边界。

### Modified Capabilities

- `session-history-workbench`: 将完整历史页入口归入最近会话区，定义 5 条预览、团队标识及历史功能的兼容保留，替换与新布局冲突的旧菜单位置约定。
- `agent-team-conversation`: 增加团队候选的 normal 类型边界、成员选择与默认响应者、可靠创建、首轮完整名册和恢复行为；保留现有名册变化触发运行时重建的要求。

## Impact

- Web 接入点：`channel/web/chat.html`、实际加载的 `channel/web/static/js/console.js`、`channel/web/static/css/console.css`、外观样式及 i18n 命名空间。`static/js/chat/new-chat.js` 与 `templates/layout/sidebar.html` 有旧版相似实现，实施前须按真实脚本/模板引用核对，不将修改未加载副本视为完成。
- 会话接入点：`channel/web/fork/handlers/sessions.py`、现有请求作用域与智能体使用授权、`agent/workspace/session_prefs.py`；优先复用 `/api/agents/workbench` 和 `/api/sessions/{session_id}/settings`，不引入另一套团队数据或运行平台。
- 数据唯一归属：智能体类型/可用性以服务端档案和使用范围投影为准；业务会话仍归显式 owner；members 仍由现有作用域下的 session preferences 持久化；最近会话只是统一历史的授权投影。前端选中集合仅是本次弹窗草稿。
- 依赖基线：`agent-chat-launch`、`agent-entry-navigation`、`console-navigation-availability`、`console-route-lifecycle`、`sidebar-account-menu` 和 `workbench-appearance-preferences`。未归档 `add-opencode-coding-agents` 的类型投影、普通路径拒绝和编码历史切片须核实真实实现及证据；不修改其源文件或替其归档。
- 与进行中的 `add-multimodal-image-input`、`port-upstream-tasks-page` 共享 `chat.html`/`console.js` 等文件；实施时按功能切片合并，保持附件草稿、任务菜单和后台任务行为。
- 本 change 仅完成规划产物；不修改产品代码、不启动实现、不新增数据库迁移。发布时使用仅控制侧栏呈现的临时开关，团队类型过滤和服务端拒绝不受该开关影响。
