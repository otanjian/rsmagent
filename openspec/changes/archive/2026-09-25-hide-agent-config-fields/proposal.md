## Why

智能体管理详情面当前有三处控件在控制台里没有对应的管理闭环：

- 「概况」页签的**分类**下拉，选项由场景目录的 `category` 去重得来，本身没有独立的分类管理流程，读起来像是场景绑定的影子。
- 「概况」页签的**关联场景**下拉，直接渲染内部 `scene_id`，绑定后把内部标识暴露给管理员；而场景目录本身没有对应的管理面。
- 「能力」页签的 **SOP 流程** 区块，其提示语已自述「仅作能力清单展示，不驱动状态机」，加了 ID 也不影响运行。

三处都在交付前的控制台走查中被判为「占位但不产生配置价值」：界面留了输入口，管理员却拿不到对应回报，反而稀释了同页真正可用的控件（职位、标签、问候、人设、技能、工具白/黑名单）。本次把它们从界面上撤下，配置数据本身不清理。

## What Changes

- 「概况」页签 MUST NOT 再渲染「分类」「关联场景」两个下拉控件；同页其余字段（头像、名称、类型、职位、标签、问候语、人设摘要、默认模型、知识库、默认智能体动作）保持不变。
- 「能力」页签 MUST NOT 再渲染「SOP 流程」区块（标题、提示、已绑 ID 列表、输入框与「添加」按钮）；技能与工具白/黑名单两区块保持不变。
- 三个字段的服务端契约与持久化 MUST 保持不变：`category`、`scene_id`、`sops` 仍可读写、仍在 `revision` 乐观并发内、仍参与克隆与预设导入。**MUST NOT** 出现数据迁移、清空或回填。
- 保存路径 MUST NOT 因控件消失而改写或清空既有值：保存概况时这三个字段按当前读到的值原样回传。
- 运行时行为不变：场景字段继承（`agent-digital-employee-profile` 的「场景绑定与字段继承」）与 SOP 注入系统提示（`agent-capability-runtime` 侧）MUST 保持原样。
- 本变更 **MUST NOT** 删除任何 i18n key：`agents_category`、`agents_category_none`、`agents_scene`、`agents_scene_none`、`agents_sops_*` 留在字典中，避免三语快照与键位归属出现无声漂移。

## Capabilities

### New Capabilities

（无。本次只收缩既有管理界面的呈现面，不引入新的可验收行为域。）

### Modified Capabilities

- `agent-digital-employee-profile`：修改「管理概况页签扩展」。该 requirement 目前要求概况页提供「职位、分类、标签、问候、人设摘要与关联场景」的编辑控件；本次把「分类」与「关联场景」从可编辑清单中移除，并在 requirement 正文写明字段仍然持久化、仍可经智能体管理 API 读写，只是控制台不再提供入口。
- `agent-capability-bindings`：修改「能力页签管理 UI」。该 requirement 目前要求能力页分别管理知识、技能、SOP、工具四类绑定，且「SOP Phase 1 SHALL 至少支持按 ID 添加/删除」；本次把 SOP 的直接操作界面撤出控制台，并改写「添加 SOP 标识」场景，使 `sops` 仍作为持久化字段与运行时上下文输入存在，但不再由页面按钮产生。

## Impact

- **前端接入点**：`channel/web/static/js/console.js`
  - `renderAgentDetail()`：删去「分类」「关联场景」两个 `.agent-field` 块。
  - `refreshAgentCategoryDropdown()`、`refreshAgentSceneDropdown()`、`sceneCategoryOptions()`：随控件一并撤除（各自在元素缺失时本就会 early-return，撤除后不再有调用者）。
  - `renderAgentCapabilitiesPane()`：删去 SOP 区块及其「添加」「移除」事件绑定。
  - `renderAgentDetail()` 末尾的两处刷新调用随函数撤除。
  - `saveAgentProfile()` **不改逻辑**：既有的 `catEl ? ... : agent.category || ''` 与 `sceneEl ? ... : agent.scene_id || ''` 兜底在控件消失后正好回传当前值。
- **服务端**：无改动。`agent/admin.py`、`channel/web/fork/handlers/agents.py`、`agent/registry.py`、`agent/effective_capabilities.py`、`bridge/agent_bridge.py` 的 `category` / `scene_id` / `sops` 契约全部保留。
- **数据**：无迁移、无清空、无回填。既有租户与 WorkBuddy 预设（`scripts/build_workbuddy_presets.py` 写入 `sops`）继续可用。
- **i18n**：不新增、不删除 key；`tests/fixtures/console_i18n_snapshot.json` 不变。
- **测试**：`tests/test_agent_profile_frontend.cjs` 需从「断言分类/场景经下拉写入」改为「断言控件缺失时保存回传既有值」；新增断言页面不再产出这两个控件与 SOP 区块。
- **Desktop**：`desktop/dist/renderer/js/console.js` 是构建产物，不由本 change 的源改动直接更新；本次只改 Web 源，桌面侧在下一次打包时同步。
