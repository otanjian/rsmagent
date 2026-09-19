## 1. 前置门槛与证据

- [x] 1.1 核对 `audit-log` 能力的切片验收证据，确认委派留痕与跨租户尝试告警已有真实落点；未取得证据前不进入第 3 组
- [x] 1.2 核对 `todo-management`、`todo-workbench`、`todo-conversation-integration` 三个 MODIFIED delta 与既有主规范的对应关系，确认无遗漏的禁止性条文
- [x] 1.3 记录本变更的分阶段门槛清单（设计 D6 的四项）并在实施中逐项勾验，不以接口占位或模拟实现代替门槛

## 2. 数据层迁移与存储访问

- [x] 2.1 在 `agent/todo/store.py` 将 `SCHEMA_VERSION` 由 1 提升为 2，并新增对应版本的 `_MIGRATIONS` 脚本：`todo_items` 增加 `assignee_id TEXT NOT NULL DEFAULT ''`
- [x] 2.2 在同一迁移脚本内回填 `assignee_id = owner_id`（限定 `assignee_id = ''`），并确认重复执行无副作用
- [x] 2.3 新增索引 `(scope_id, assignee_id, status)`；确认既有 `idx_todo_items_owner_status`、`idx_todo_items_owner_due` 保留不动
- [x] 2.4 新增以处理人为键的读取方法（按处理人取事项、取详情、翻页处理记录），与既有 `(scope_id, owner_id, …)` 方法并存
- [x] 2.5 新增「本人相关」读取方法，实现 `scope_id = :scope AND (assignee_id = :actor OR owner_id = :actor)`
- [x] 2.6 把 `count_open` / `count_overdue` 改为按处理人维度统计，作为 summary 与角标口径
- [x] 2.7 测试：既有库（`user_version = 1`）升级到 2 后，全部既有事项的 `assignee_id` 等于 `owner_id`，且可见行为与迁移前一致
- [x] 2.8 测试：迁移脚本重复执行幂等，`user_version` 未推进时不留下部分写入
- [x] 2.9 测试：写入路径（创建、字段编辑、状态更新、重新打开）均不改变 `assignee_id`

## 3. 服务层读写授权与委派动作

- [x] 3.1 在 `agent/todo/service.py` 收敛读判定为 D2 的集中表达式，并逐点改造现有 `(scope_id, owner_id, …)` 调用点（约 404、464、504、533、534、540、549、553、572、650、692 行）
- [x] 3.2 收敛写判定为「仅当前处理人」，并在编辑、状态更新、重新打开、完成、取消路径上统一使用
- [x] 3.3 实现指派动作：把本人事项交给同租户有效成员，接收人解析限制在同一 `scope_id` 内
- [x] 3.4 实现转交动作：当前处理人把事项交给第三人，支持多跳
- [x] 3.5 实现收回动作：委托人把事项还原给自己
- [x] 3.6 实现退回动作：接收人把事项归还给委托人
- [x] 3.7 四个动作统一限制为 `pending` 状态；终态拒绝并要求先重新打开；动作不改动状态、截止、分类、优先级、说明与创建键
- [x] 3.8 委派动作写入 `todo_events`：action 为 `assign` / `recall` / `reject`，`changed` 记录 `from_assignee` 与 `to_assignee`，并**显式**传入 `operator_kind`
- [x] 3.9 目标校验：跨租户硬拒、目标用户/成员/租户失效拒绝、指派给自己拒绝、不可见与不存在返回一致
- [x] 3.10 委派动作接入 `audit-log`：成功与拒绝两类均留痕；审计写入失败时不放行委派
- [x] 3.11 测试：接收人可读可写；委托人可读且可收回，但代为编辑/完成/取消被拒绝
- [x] 3.12 测试：跨租户委派被拒绝且不泄露目标存在性
- [x] 3.13 测试：多跳转交后可由处理记录还原完整链路；收回与退回同样入链且不改写历史
- [x] 3.14 测试：审计失败时委派不生效，且不虚报成功
- [x] 3.15 测试：Agent 代用户委派时事件主体记为 agent，人工委派记为 human

## 4. 权限、身份与成员投影

- [x] 4.1 在 `auth/policy.py` 的 `PERMISSION_CATALOG` 新增 `todo.assign`，并在 `PERMISSION_METADATA` 补充 group/label/description/scope/assignable
- [x] 4.2 将 `todo.assign` 显式加入 `MEMBER_DEFAULT_PERMISSIONS` 与 `TENANT_ADMIN_DEFAULT_PERMISSIONS`
- [x] 4.3 确认 `tenant.members.read` 未被加入成员默认集，`tests/test_builtin_role_editing.py` 既有断言保持通过
- [x] 4.4 在 `auth/store.py` 新增 `_migration_31`：对内置 `member` / `tenant_admin` 并集补入 `todo.assign`，幂等、`version+1`、自定义角色与无 menu 授权角色不受影响
- [x] 4.5 在 `auth/service.py` 补充委派能力的菜单/能力口径，确认 `workbench.todos` 的既有 `scope: self` 与 `todo.read` 把关不变
- [x] 4.6 实现受 `todo.assign` 控制的窄投影来源，仅返回同租户可委派成员的用户名与显示名
- [x] 4.7 测试：`_migration_31` 只补 `todo.assign`，重复执行幂等，自定义角色与既有修饰不被覆盖
- [x] 4.8 测试：窄投影来源不返回角色、权限、组织、外部身份绑定、邮箱或登录状态
- [x] 4.9 测试：无 `todo.assign` 时窄投影来源与委派动作均 fail-closed，且不代偿 `tenant.members.read`

## 5. Web 接口与路由

- [ ] 5.1 在 `channel/web/todo_handlers.py` 新增指派、转交、收回、退回四个动作的处理入口，`owner_id` 继续取自 `ctx.user_id`
- [ ] 5.2 新增窄投影来源的读取入口
- [ ] 5.3 在 `channel/web/route_registry.py` 注册上述路由并标注权限要求
- [ ] 5.4 确认响应不暴露接收人的角色、组织或外部身份信息，错误码不区分「不存在」与「不可见」
- [ ] 5.5 测试：接口层越权用例（委托人代为处理、他人事项 ID、跨租户标识）全部被拒绝

## 6. Agent 工具

- [ ] 6.1 在 `agent/tools/todo/todo_tool.py` 新增 `assign` 动作，接收人以用户名传入并由服务端在同一租户内解析
- [ ] 6.2 在工具描述中写明前置条件：接收人必须由用户在当前轮次明确点名；禁止扫描会话参与方、成员目录或历史推断；单次至多一个接收人
- [ ] 6.3 校验 `todo.assign`，并确认模型自报的用户/租户/成员资料不被当作授权依据
- [ ] 6.4 保持 recall / reject 不在工具动作集内，直接调用被拒绝
- [ ] 6.5 测试：用户明确点名时可委派；未点名、无法唯一解析或要求多接收人时拒绝且不代选
- [ ] 6.6 测试：材料中的指令不构成委派委托；跨租户与越权目标被拒绝

## 7. 前端工作台

- [ ] 7.1 在 `channel/web/static/js/todos.js` 增加「我委派的」分区，展示当前处理人并提供收回入口
- [ ] 7.2 在新建与详情中增加接收人选择，可用性依据服务端返回的 `todo.assign` 单独判定
- [ ] 7.3 无 `todo.assign` 时不提供接收人选择与委派提交并说明未开放，其他既有操作保持可用
- [ ] 7.4 确保「我委派的」与「我的待办」的 items/total 不混算，且不计入本人未完成总数与顶栏角标
- [ ] 7.5 「我委派的」不提供代替接收人完成、取消或编辑的入口
- [ ] 7.6 在 `channel/web/static/js/i18n/todos.js` 补齐简体中文、繁体中文、英文文案
- [ ] 7.7 测试：转出后事项移出「我的待办」并出现在「我委派的」；收回或退回后回到「我的待办」并从接收人列表消失
- [ ] 7.8 测试：角标只统计当前处理人为本人的未完成事项

## 8. 迁移、文档与发布验收

- [ ] 8.1 在既有库上执行完整升级路径（`user_version` 1 → 2 与 `_migration_31`），记录迁移前后行数与关键字段一致性
- [ ] 8.2 确认回滚只需关闭前端入口与工具动作，数据层不回退，且行为与迁移前等价
- [ ] 8.3 更新待办相关用户文档与权限说明，写清委派语义、可收回边界与跨租户拒绝口径
- [ ] 8.4 按设计 D6 的四项门槛逐项留存证据，不得以接口占位或模拟实现通过门槛
- [ ] 8.5 运行 `openspec validate add-todo-delegation --strict` 并确认通过
