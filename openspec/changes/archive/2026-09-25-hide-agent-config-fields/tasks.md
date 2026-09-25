## 1. 规范

- [x] 1.1 `agent-digital-employee-profile` 的「管理概况页签扩展」改为不提供「分类」「关联场景」控件，并写明二者仍是持久化字段、仍可经 API 读写。（证据 1）
- [x] 1.2 `agent-capability-bindings` 的「能力页签管理 UI」改为由界面管理技能与工具两类绑定，并写明 SOP 无控制台编辑入口但 `sops` 字段保留。（证据 1）
- [x] 1.3 `openspec validate hide-agent-config-fields --strict` 通过。（证据 1）

## 2. 实现

- [x] 2.1 `console.js` 的 `renderAgentDetail()`：删去「分类」与「关联场景」两个 `.agent-field` 块，以及末尾对 `refreshAgentCategoryDropdown()` / `refreshAgentSceneDropdown()` 的调用。（证据 2、3、5）
- [x] 2.2 撤除只服务这两个控件的 helper：`_sceneCatalogCache`、`sceneCatalog()`、`sceneCatalogOptions()`、`refreshAgentSceneDropdown()`、`sceneCategoryOptions()`、`refreshAgentCategoryDropdown()`。（证据 2、5）
- [x] 2.3 `console.js` 的 `renderAgentCapabilitiesPane()`：删去 SOP 区块标记、`#agent-sop-add` 的 click 绑定、`.agent-tag-x[data-sop]` 的移除绑定与该区块专用的局部变量。（证据 2、3、5）
- [x] 2.4 `saveAgentProfile()` 保持不写改动：确认 `category` / `scene_id` 走既有 `catEl ? ... : agent.category || ''` 兜底，控件缺失时回传现值。（证据 3、5、6）
- [x] 2.5 全仓检索确认没有遗留调用者与悬空断言：`agent-edit-category`、`agent-edit-scene` 仅剩 `saveAgentProfile` 的两处兜底读取（第 4049/4050 行），`agent-sop-input`、`agent-sop-add`、`agent-sops-list`、`sceneCatalog` 在 `channel/web/` 中不再出现。（证据 5）

## 3. 测试

- [x] 3.1 先写红：在 `tests/test_agent_config_fields_hidden_frontend.cjs` 中断言 `renderAgentDetail()` 的产物不含「分类」「关联场景」标签与对应元素标识，且仍含「职位」「标签」「问候语」「人设摘要」；断言 `renderAgentCapabilitiesPane()` 的产物不含 SOP 区块，且仍含技能与工具两区块。（证据 2）
- [x] 3.2 先写红：断言控件缺失时 `saveAgentProfile()` 仍把 `category` / `scene_id` 的现值写进 payload，且不为空。（证据 2、3）
- [x] 3.3 端到端而非桩断言：harness 只暴露渲染产物里出现过的元素，使「控件仍被渲染」与「保存把值写空」成为同一事实的两面。（证据 3、6）
- [x] 3.4 调整既有用例：`test_agent_profile_frontend.cjs` 的 `WITHDRAWN_IDS`、两处切片锚点与 `saveAgentProfile` 断言按新口径更新；`test_tenant_default_agent_frontend.cjs` / `test_user_default_agent_frontend.cjs` 内对两个 helper 的 stub 复核为无害（两文件全绿）。（证据 6、4）
- [x] 3.5 红/绿核验：三条新断言在干净检出（HEAD `6a8ebccd`）上为红、在工作树上为绿。（证据 2、3、4）
- [x] 3.6 全量前端回归（`node --test tests/*.cjs`）：工作树 10 条失败全部落在干净基线的失败集合内，新增失败为 0。（证据 4）

## 4. 文档与证据

- [x] 4.1 检索 `docs/`、`webhelp/`、`doc/`：用户文档无描述这三处控件的内容，无需同步；`docs/design/agent-digital-employee-config.md` 第 59 行属历史验收记录，按不修改历史的原则保持原样。（证据 7）
- [x] 4.2 写入 `evidence.md`：红/绿输出、回归对比、服务端投递产物与 `openspec validate --strict` 输出。（证据 1–7）

## 5. 验收

- [ ] 5.1 三处控件在控制台不再出现，同页其余控件与保存行为不变（**待有登录会话的人逐屏确认**；已用「服务端投递产物 == 磁盘源码」作为等效证据，证据 5、8）。
- [ ] 5.2 既有绑定 `category` / `scene_id` / `sops` 的智能体保存其他字段后，三值均未变化（**待实测**；第 3 节的端到端用例已在测试层覆盖同一事实）。
- [x] 5.3 `openspec validate hide-agent-config-fields --strict` 通过。（证据 1）

> 5.1 / 5.2 的逐屏确认需要一个已登录的控制台会话，本次执行环境没有凭据（`/admin` 返回登录门），故未勾选。运行中的 `localhost:9899` 已经投递了改动后的 `console.js`，刷新页面即可看到结果。
