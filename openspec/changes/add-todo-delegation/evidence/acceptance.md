# 验收证据：add-todo-delegation

本文件按 `tasks.md` 的第 8 组与设计 D6 的四项门槛记录证据：代码位置、**实际执行**的命令与计数、
以及真实数据库上的迁移读数。
**未执行的真实门槛一律标注 NOT RUN，不得以单测或模拟结果代替。**

- 实施基线：`aedbc4dc`（G1–G5）+ `d68a5653`（G6）+ `9c717fea`（G7），本文件与 §6 的文档为 G8 提交。
- Python：`.venv/bin/python`（仓库根）。
- 演练脚本：`openspec/changes/add-todo-delegation/evidence/migration_drill.py`,输出见 §3。

---

## 1. 门槛 1：`audit-log` 切片验收（前置）

委派留痕复用既有审计能力，未新增存储：`AuditStore`（`auth/audit.py`, append-only）是唯一来源，
委派只往它写两种形状的事件。

| 落点 | 代码 | 事件形状 | 证据 |
| --- | --- | --- | --- |
| 成功委派 | `agent/todo/service.py::_apply_delegation` → `_audit_or_raise` | `action=todo.assign/todo.transfer/todo.recall/todo.reject`,`result=success`,`changes={from_assignee,to_assignee}` | `tests/test_todo_delegation_service.py:295`；`tests/test_todo_delegation_web.py:139`（真实 HTTP 后 `query_tenant` 读回） |
| 越权 / 跨租户拒绝 | `agent/todo/service.py::_refuse` → `_audit_best_effort` | 同上 `action`,`result=denied` | `tests/test_todo_delegation_service.py:180`；`tests/test_todo_delegation_web.py::TestDelegationBoundary`（跨租户、未知接收人、他人事项 ID 均留 `denied`） |

**告警口径**：跨租户尝试不引入新的告警通道，而是写成一类与其它授权拒绝**同形**的
`result="denied"` 行（`auth/audit.py::denied_event` 的既有形状）。`AuditStore.query_tenant` 的
可筛选字段（`QUERYABLE_FIELDS`）已含 `result`，因此既有审计查询/监控面无需改动即可筛出委派拒绝。
该能力自身的切片证据：`.venv/bin/python -m pytest tests/test_identity_audit.py -q` → **6 passed**
（append-only、租户隔离、密钥字段脱敏、`denied_event` 形状）。
本 change 的追加约束（写失败不放行、拒绝不放行失败）由 §2 门槛 4 的用例覆盖。

## 2. 门槛 2–4：实现与测试证据

### 门槛 2 数据迁移

| 项 | 证据 |
| --- | --- |
| v1 → v2 加列、回填、索引 | `.venv/bin/python -m pytest tests/test_todo_delegation.py -q` → **15 passed**（`TodoStoreMigrationTests`） |
| `user_version` 未推进时的可重跑性 | 同上（`_migration_2_assignee` 的列存在性守卫）；真实库读数见 §3A |
| 既有可见行为不变 | 同文件断言回填后 `assignee_id = owner_id`,`count_open` / 列表 / 详情口径与迁移前一致 |

### 门槛 3 权限补授与 fail-closed

| 项 | 证据 |
| --- | --- |
| `todo.assign` 目录项 + 内置默认集 | `.venv/bin/python -m pytest tests/test_todo_delegation_permissions.py -q` → **11 passed**，其中断言 `tenant.members.read` **未**进入成员默认集 |
| `_migration_31` 幂等、只补一项、自定义角色不动 | 同上（`Migration31BackfillTests`）；真实库读数见 §3B |
| 既有权限断言不受影响 | `.venv/bin/python -m pytest tests/test_builtin_role_editing.py -q` → **13 passed** |
| 无 `todo.assign` 时页面与接口两侧 fail-closed | `tests/test_todo_delegation_web.py::TestAssigneeProjection`（接口 403）；`node --test tests/test_todo_frontend.cjs` → **38 passed**，含「无 `todo.assign` 时不提供接收人选择并说明未开放」；`tests/test_todo_delegation_tool.py`（工具动作拒绝） |

### 门槛 4 读写授权与越权用例

| 用例族 | 文件 | 计数 |
| --- | --- | --- |
| 服务层：四个动作、读写分离、审计顺序、主体归属 | `tests/test_todo_delegation_service.py` | **26 passed** |
| HTTP 层：真实 WSGI + 会话 + 租户头，越权与边界 | `tests/test_todo_delegation_web.py` | **16 passed** |
| Agent 工具：点名才委派、跨租户、自报身份不作授权 | `tests/test_todo_delegation_tool.py` | **7 passed** |
| 前端：两分区不混算、处理人展示、收回入口、拒绝不代偿 | `tests/test_todo_frontend.cjs` | **38 passed**（含本 change 新增 10 项） |
| 待办能力全量 | `.venv/bin/python -m pytest tests/ -q -k "todo"` | **139 passed** |
| 控制台菜单迁移演练（曾受新迁移影响的既有断言） | `tests/test_console_migration_drill.py` | **12 passed** |

覆盖的具体越权用例：委托人代为处理（编辑 / 完成 / 取消）、他人事项 ID、跨租户目标、未知接收人、
指派给自己、终态委派、旧版本重试、无 `todo.assign`。

## 3. 真实数据库上的升级路径（task 8.1）

命令：

```
.venv/bin/python openspec/changes/add-todo-delegation/evidence/migration_drill.py
```

### 3.A todo 库 `user_version` 1 → 2

**既有库的来源要如实说明**：本机与仓库内**不存在**真实部署遗留的待办库
（`~/.cow` 下无 `todo/todos.db`），因此这一半用**由真实 migration-1 脚本建出的 v1 库**
（`_MIGRATIONS[1]` + `PRAGMA user_version = 1`，不是手抄 DDL）并播入 5 条跨 2 租户 /
3 所有者 / 4 种状态的行来演练。

```
迁移前 user_version      : 1
迁移前行数               : 5
迁移前是否已有 assignee_id: False
迁移前索引               : ['idx_todo_items_owner_due', 'idx_todo_items_owner_status']
迁移前行指纹             : 331b5cd0de45405d

迁移后 user_version      : 2 (SCHEMA_VERSION = 2)
迁移后行数               : 5 -> 与迁移前一致: True
迁移后索引               : ['idx_todo_items_assignee_status', 'idx_todo_items_owner_due', 'idx_todo_items_owner_status']
回填 assignee_id = owner  : 5 / 5
残留空 assignee_id        : 0
关键字段是否逐行一致      : True
既有索引是否保留          : True

再次打开（幂等）          : True | user_version 2 | 行数 5

模拟中断（加列后未推进版本，user_version 重置为 1）...
中断后重跑 user_version   : 2
中断后重跑结果与之前一致  : True
中断后回填 assignee_id = owner: 5 / 5
```

结论：行数与关键字段（`id/scope_id/owner_id/title/status/created_at/version`）逐行一致；
`assignee_id` 全量回填为 `owner_id`，因此 D2 的两条判定退化为旧行为；两个既有索引保留，
只**新增**一个 `(scope_id, assignee_id, status)`；重复打开为 no-op；
「加列已完成但版本未推进」这一中断态可无损重跑（SQLite 无 `ADD COLUMN IF NOT EXISTS`，
这正是守卫存在的理由）。

### 3.B 身份库 `schema_migrations` 30 → 31

这一半用的是**仓库内真实 `identity.db` 的副本**（2 租户 / 5 用户 / 128,631 条审计事件），
先把它退回变更前状态（删掉 version 31 记录并摘掉内置角色上的 `todo.assign`），
再以真实 `IdentityStore` 打开。**不修改仓库内的原文件**。

```
回退到变更前状态的内置角色: ['member', 'tenant_admin']

迁移前最高版本           : 30
迁移前角色数             : 3
  e2e_m            builtin=0 version=6  perms=2  todo.assign=False tenant.members.read=False
  member           builtin=1 version=17 perms=22 todo.assign=False tenant.members.read=False
  tenant_admin     builtin=1 version=10 perms=24 todo.assign=False tenant.members.read=True
迁移前各表行数           : {'memberships': 5, 'users': 5, 'tenants': 2, 'agent_bindings': 9,
                            'membership_roles': 6, 'audit_events': 128631}
迁移前已记录版本         : 30 -> 30

迁移后最高版本           : 31 (migration_versions 上限 = 31)
迁移后角色数             : 3
  e2e_m            builtin=0 version=6  perms=2  todo.assign=False tenant.members.read=False
  member           builtin=1 version=18 perms=23 todo.assign=True  tenant.members.read=False
  tenant_admin     builtin=1 version=11 perms=25 todo.assign=True  tenant.members.read=True
迁移后各表行数           : {'memberships': 5, 'users': 5, 'tenants': 2, 'agent_bindings': 9,
                            'membership_roles': 6, 'audit_events': 128631}
非角色表是否逐表一致      : True | 行数一致: True
内置角色是否获得 todo.assign: {'member': True, 'tenant_admin': True}
member 是否被顺带放开成员目录: False
自定义角色是否被动过      : True | 自定义角色: ['e2e_m']

再次打开（幂等）          : True | 版本: 31
```

结论：内置 `member` / `tenant_admin` 各**恰好**多一个权限（22→23 / 24→25）且 `version+1`；
自定义角色 `e2e_m` 的 `version` 与权限**逐位不变**；非角色表（`memberships` / `users` /
`tenants` / `agent_bindings` / `membership_roles` / `audit_events` 共 128,631 行）
的行数与内容指纹**逐表一致**；`member` 的 `tenant.members.read` 保持 `False`
（本迁移不兼任成员目录放开）；重复打开为 no-op。

## 4. 回滚口径（task 8.2）

**行为回滚 = 关闭入口，数据层不回退。**

1. 前端：停用 `todo_scope_delegated` 分区与接收人选择（`channel/web/static/js/todos.js`），
   或在角色层收回 `todo.assign`——后者是既有 RBAC 面，无需发版。
   `can_assign`/`can_transfer`/`can_recall`/`can_reject` 由服务端按该权限计算，
   权限一收回，入口自动消失，无需改前端。
2. Agent 工具：`assign` 动作在 `todo.assign` 收回后即 fail-closed
   （`agent/tools/todo/todo_tool.py::_assign` 在解析接收人**之前**判权限）。
3. 数据层：保留 `assignee_id` 列与新增索引。因为回填后 `assignee_id = owner_id`
   对全部既有行成立，且除委派动作外**没有任何写入路径**会改这一列
   （`tests/test_todo_delegation.py::TodoStoreAssignmentTests`），
   所以关闭入口后的可见行为与迁移前**逐条等价**，无需 `DROP COLUMN`（也无破坏性回退可做）。
4. 版本推进不回退：`user_version=2` 与 `schema_migrations` 的 31 记录保留。
   旧二进制不读该列即可正常服务，重开新版本也不会重跑回填（§3 的幂等读数）。

## 5. NOT RUN（环境限制，非结论）

- **真实部署遗留待办库的升级**：本机不存在此类库（见 §3.A），故以真实 v1 脚本建库演练替代。
  真实部署升级窗口的读数为 **NOT RUN**。
- **真机多租户浏览器演练**：委派跨两租户的浏览器端到端（点击→收回→角标）**NOT RUN**；
  本轮以真实 HTTP（WSGI，含会话与租户头）+ node 前端套件覆盖同一契约。

## 6. 既有失败（与本次无关，基线对比已确认）

相关回归集 `pytest -k "todo or audit or builtin_role or console or identity or delegation"`
→ **3 failed, 1179 passed, 26 skipped**。3 项失败均在变更前基线上同样失败，且成因与委派无交集：

| 失败 | 成因 | 基线（`1de6fe7f`）对比 |
| --- | --- | --- |
| `tests/test_personal_console_frontend.py::test_frontend_behavior` | 读取 `channel/web/static/js/personal-console.js`，该文件已由更早的 `9850e453 feat(console): unify console by data scope and retire the personal console` 删除，测试未同步 | 同样失败 |
| `tests/test_weixin_qr_flow.py`（22 项） | `channel type is not available for tenant configuration`（来自 `auth/service.py` 的通道可用性判定） | 该文件在基线上同样 **22 failed, 18 passed** |

基线对比方式：`git worktree add /tmp/rsm-base 1de6fe7f` 后运行同一批文件，得到 **23 failed, 18 passed**
（22 项微信 + 1 项个人控制台），与当前工作区计数一致；已用 `git worktree remove --force` 清理。

## 7. 文档同步（task 8.3）

委派语义此前没有任何用户文档落点：`rg "待办|todo" docs/zh/channels/web.mdx` 在改动前**零命中**，
`docs/` 下也没有 todo 工具页（`docs/tools/` 只有 bash / memory / send 等）。因此本次**新增**
工作台待办的用户小节，而不是修改既有段落，三种语言同步：

| 文档 | 新增内容 |
| --- | --- |
| `docs/zh/channels/web.mdx` | `### 待办与委派`（简体中文） |
| `docs/channels/web.mdx` | `### Todos and Delegation`（英文） |
| `docs/ja/channels/web.mdx` | `### タスクと委任`（日文） |

三段都写明同一套口径：两分区与角标不混算、四动作含义（含「退回不是指派」）、
只有当前处理人可处理、`pending` 边界、跨租户一律拒绝且「不属于本租户」与「不存在」同答、
读写留痕与 `result=denied`、以及对话中仅在明确点名时由 Agent 代为指派。

权限说明面向管理员的两处落点：

1. `auth/policy.py::PERMISSION_METADATA` 的 `todo.assign` 条目（`group: 待办`、
   `label: 委派待办`、`scope: personal`、`assignable: True`），由
   `channel/web/admin_handlers.py:876`（`permission_catalog_with_metadata()`）
   直接供给「管理 → 角色」编辑器，因此**无需前端改动**即出现在授权界面。
2. 上表用户文档中的「权限」一条，说明默认授予角色与无权限时的降级表现。

需求基线本身写在 `openspec/changes/add-todo-delegation/specs/`（4 个 delta，262 行），
归档后进入 `openspec/specs/todo-delegation`、`todo-management`、`todo-workbench`、
`todo-conversation-integration` 主规范；`design.md` 记录 D6 门槛、委派主体归属
与 `tenant.members.read` 既有把关口径的更正。

文档相关既有测试未受影响：`.venv/bin/python -m pytest tests/test_help_site.py tests/test_doc_edit.py -q`
→ **36 passed, 76 subtests passed**。

