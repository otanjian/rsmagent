## Context

动机见 proposal.md。设计基线为 `rdai@f5d7d764`、上游 `8f1b19f1`；工作区另有未提交变更，实施分支必须单独建立。此前实测的测试基线仅证明对应提交，不能代替本 change 的验收。

关键现状：

| 位置 | 现有实现 | 本次接入 |
| --- | --- | --- |
| `auth/service.py::context_for_tenant` | `/auth/context` 返回 consumers、console_pages | 增加 feature_actions |
| `auth/capability_matrix.py::Slice.route` | 从 open 动作派生路由策略 | 同源派生动作状态，关闭时拒绝调用 |
| `channel/web/fork/authorization.py` | `_db_scope`、`_require_session_scope` | 上下文操作的身份与 owner 边界 |
| `bridge/agent_bridge.py::peek_agent` | 按 Agent/session 观察已有实例 | 不用会创建实例的 get_agent |
| `agent/protocol/agent.py::compact_context` | 摘要前后分别持 messages_lock，中间释放 | 补快照比较，防止覆盖期间的新消息 |
| `channel/web/fork/handlers/models.py` | save_catalog 与 chain 均已实现 | 只补当前 Web 的编辑器 |
| `agent/tools/scheduler/authorization.py::TaskAccessService.create_task` | owner、scope、revision、quota、审计 | HTTP 创建唯一写入入口 |
| `agent/tools/scheduler/integration.py::_record_scheduler_run` | 写既有 runs，辅助记录失败不阻断投递 | 记录同次执行的归属快照 |
| `agent/memory/conversation_store.py` | runs 查询不自动限制 tenant/owner | 独立的 fork 记录访问层 |

## Goals / Non-Goals

**Goals:**

- 8 个接口有确定契约和真实授权；Web 的支持状态一致。
- 程序员按 `implementation.md` 的函数签名、SQL、测试样例和提交顺序实施，无需再次选择数据方案。
- 新逻辑与上游 `channel/web/api/**`、`channel/web/core/**` 分离。

**Non-Goals:**

- 本轮不完成前端 109 处结构迁移，也不声明已满足“退出旧单体”的最终要求。
- 不开放微信个人渠道或 Web 一键更新，不新增公共任务创建接口，不自动认领旧运行记录。
- 不建立另一份任务库、运行正文库、角色体系或认证系统。

## Decisions

### D1：交付两批，按能力验收开放

R1 为动作投影、上下文、模型编辑器；R2 为六个调度接口和 Web 界面。原方案粗估 4–7 人日；细化后按 implementation.md 的 5–8.5 人日排期，已包含运行归属、压缩并发和验收。前端结构迁移继续独立跟踪。

对比“先全部迁移 shell”，本方案能够先交付功能；对比“仅隐藏入口”，本方案要求正向链路真正通过。隐藏入口只是未交付状态，不算完成。

### D2：能力状态增加字段，不新建权限服务

在 `/auth/context` 原响应追加：

```json
{"feature_actions":{"session_context.usage":{"available":true,"reason":""},"session_context.compact":{"available":false,"reason":"disabled_by_deployment"},"scheduler.create":{"available":true,"reason":""}}}
```

实际返回八个完整键；列表见 implementation.md。`available` 只描述服务功能，不描述具体对象授权。缺字段的旧服务端一律按新能力关闭处理。
示例里的两个取值都取自终态可能出现的形状（本 change 交付时八键全部 `available:true`；
`reason` 的三个取值 `not_implemented` / `not_accepted` / `disabled_by_deployment`
在运营方用 `RDAI_DISABLED_ACTIONS` 关停时会真实出现）。

在现有 capability matrix 中为新动作声明独立 slice，保留原 scheduler 五动作。前端投影与 `S(slice, action)` 路由策略读取同一 finalized slice；禁止前端自行从路由存在性推断可用。

部署只允许通过新环境变量 `RDAI_DISABLED_ACTIONS` 额外关闭动作（逗号分隔完整键）；它不能打开未实现/未验收动作。启动时解析一次，修改后重启；不承诺热切换。每次构建 URL/策略表必须与动作投影使用同一份解析结果。未知键令配置检查失败，避免错误拼写导致原本希望关闭的功能继续开放。

客户端维护 `(身份会话, tenant_id, contextRevision)` 版本，登出、换租户和重连使版本递增；旧响应不能写入新版本。Web 接 `channel/web/static/js/console.js::_fetchTenantAuthorization`、`_invalidateAuthContext`，失效时递增序号并解除旧请求复用。

### D3：上下文操作使用 fork handler，并解决真实并发窗口

新增 `channel/web/fork/handlers/context.py`，导出 `SessionContextUsageHandler`、`SessionCompactContextHandler`，具体路径位于 session 通配路由前。权限顺序为：现有 HTTP 策略 → `_db_scope()` → `_require_read_permission(ctx, 'history.read')` → `_require_session_scope(...)`；压缩还要求 `chat.use` 与该 Agent 的 use 权限。非 owner 保持既有 404 隐藏策略。

现有 `_require_owned_session` 只按 session_id 查询，且无记录时放行。先将该查询限定到存储 Agent 键和当前 tenant，避免同名 session 误匹配，保留其他入口的无行行为。新 handler 必须追加精确的 durable owner 检查：在受信 Agent 对应的 ConversationStore 中按存储 Agent 键、tenant_id、owner、session_id、channel_type=web 匹配；无匹配返回 404。默认 Agent 的存储键来自已绑定 store 的 `_dimensions()`，可能为 `''`，不能直接用 API Agent ID。已归属会话而无实时实例返回 available=false；未持久化的新会话由客户端等待首次保存后查询。引用的是业务 session ID，禁止把 AuthSession token 当业务会话 ID。`peek_agent` 接原业务 session ID；`scoped_session_key` 只用于取消注册表的活动检查，不能拿来替换 peek_agent 的键。

压缩开始时调用取消注册表的 `has_active(bridge.scoped_session_key(session_id, agent_id))`，活动生成返回 409/session_busy。还必须防止检查之后的新消息：在 `Agent.compact_context` 内对 messages 做深拷贝快照，摘要在锁外计算，提交时在 messages_lock 内比较当前消息与快照；不相等返回 `ok=false, reason=context_changed`，HTTP 映射为 409。所有 kept-turn 编辑作用于深拷贝；摘要写长期记忆移动到成功提交之后。保留原方法签名，不植入 fork 身份分支。

该选择复用现有摘要算法。拒绝通过在 HTTP 外层持 messages_lock 再调用 compact_context 实现互斥：它是普通 Lock，现有方法会再次获取而死锁。并发两个压缩对同一快照至多一个提交成功；另一个通过比较失败退出。

### D4：模型编辑器通过明确调用点接入

新模块 `functional-models.js` 只导出 `window.RdaiFunctionalModels`，在 `openChatFallbackModal` 替换表单主体，在 provider 卡片增加“模型目录”按钮。只加载这一份实现；禁止整套装载上游 views/models.js 再重声明函数。

持久化继续使用 `/api/models` 的 `set_capability/chat_fallback/chain` 与 `save_catalog/models/hidden`。目录是覆盖层：不得把整个 effective 列表作为 models 全量保存；编辑后的覆盖写 models，删除预设写 hidden。平台管理员资格仍由现有 platform 路由判断。新增状态和错误提示补齐 zh、zh-Hant、en。

### D5：HTTP 创建沿用现有渠道范围和任务服务

新增 `channel/web/fork/scheduler_targets.py` 统一处理实例、目录和目标解析，供三接口共用。实例范围使用 `IdentityService.list_tenant_channel_instances(actor_user_id=..., tenant_id=...)` 的现有结果；普通成员只见自己的 user 范围实例，管理者见获准的 tenant 实例及本人实例，不因本 change 扩大范围。

目录枚举必须按已授权 instance_id 查询 RecipientStore，添加可选 instance_ids 参数并保留原无参签名兼容；HTTP 永远传集合，空集合返回空。只投影已授权实例的接收者，不把全局目录交给浏览器筛选。创建时再次通过同一实例范围、可信目录和当前绑定验证；客户端 supplied instance_id 必填，不回退 channel_type 或默认 Agent。

HTTP 本期只创建 personal 任务，owner 由 TaskAccessService 写入。公共任务仍使用已有获权入口。沿用上游 name/schedule/action 响应结构与时间计算，调用 `TaskAccessService.create_task` 而不是 `store.add_task`。默认创建按钮提交期间禁用；网络不明结果不自动重试 POST，避免引入新的隐式重复创建语义。

### D6：用同库授权扩展表固定运行记录归属

新增 `agent/tools/scheduler/run_access.py` 和 `run_repository.py`。`runs` 仍是结果唯一来源；扩展表只保存归属快照，避免给上游 runs 及方法签名加入租户参数。

```sql
CREATE TABLE IF NOT EXISTS fork_scheduler_run_scopes (
    run_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL CHECK (scope IN ('personal', 'public')),
    agent_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    provenance_version INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    CHECK (scope <> 'personal' OR owner_user_id <> '')
);
CREATE INDEX IF NOT EXISTS idx_fork_run_scope_owner
ON fork_scheduler_run_scopes(tenant_id, owner_user_id, agent_id, run_id);
CREATE INDEX IF NOT EXISTS idx_fork_run_scope_public
ON fork_scheduler_run_scopes(tenant_id, scope, agent_id, run_id);
```

通过受信的 ConversationStore 获取同库连接；与 `_db_path/_connect/_lock` 的接触封装在 repository，HTTP 不接收数据库路径。重复建表必须幂等，错误关闭新历史能力，不能回退旧无范围查询。

在 `_record_scheduler_run` 写 runs 成功后登记快照：personal 的 tenant/user 来自任务 owner 并与当前可信执行身份核对；public 的 tenant 来自已重验的执行作用域和绑定。run_id、task_id、Agent 均来自本次执行，不来自 HTTP 参数。扩展记录使用 insert-if-absent，冲突时比较所有归属字段，拒绝改写。进程在两次写之间退出只产生不可见旧记录，不扩大权限；辅助记录失败不重新执行任务。

第一版**不做旧记录自动回填**。列表追加 `history_scope="attributed_only"`，页面固定说明“仅显示已确认归属的运行记录”；不查询、返回或推断他人未归属记录数。后续恢复必须另有可审计的历史证据，当前绑定不能作为旧记录归属的证据。

### D7：列表先限域，详情使用安全会话读取，删除使用单事务

RunAccessService 每次构建可信 actor，验证当前租户有效成员资格。授权语义沿用 TaskAccessService：个人记录 owner-only；公共记录按原公共任务 view/manage 判定，非 owner 管理员不获得个人记录正文。任务已删除时以不可变范围快照构造最小授权对象，不能把个人记录降为公共记录。

SQL JOIN runs 与扩展表，WHERE 必有 s.tenant_id；个人支路必须有 s.owner_user_id；公共支路必须在已授权 Agent 集合内。指定 agent_id 只进一步收窄，省略或空值只是聚合当前授权范围。按 started_at DESC、run_id DESC 排序后才 LIMIT/OFFSET；不得读取一页无范围数据再过滤。since、task_id 和页大小均用参数绑定，limit 上限 500。

详情先取得授权 run，再通过包含存储 Agent 键、tenant_id、owner、session_id 的会话读取取得正文；正文还必须以 messages.run_id 精确关联该次运行，禁止调用当前无 owner 限制且使用时间邻近匹配的 `get_run_detail`。没有可证实的会话权限或精确关联只返回获准预览和 full_output=null；外部 IM 会话首版沿用此降级，不能从 receiver 推断 Web owner。

删除开启事务，在同一授权 WHERE 下重新取得记录，running 返回 409；删除扩展表与 runs，保持 messages 不变。事务期间重新校验操作资格，审计只写 run/task ID 与结果；失败回滚，不返回伪成功。成功后的重复删除返回 404。

### D8：可执行任务书是本 change 的工程入口

`implementation.md` 固定 HTTP 契约、类型、模块职责、函数签名、关键算法、首个失败用例及测试矩阵。`tasks.md` 是唯一实施进度表；开发任务书不复制另一份完成状态。原 `docs/superpowers/plans/2026-09-19-rdai-functional-integration.md` 保留为总体范围与估算，入口链接指向本 change。

## Risks / Trade-offs

- 旧记录无归属 → 首版不展示且明确说明覆盖范围；新增记录从执行时开始携带快照。
- 运行记录连接使用存储内部成员 → 隔离在一个 repository，加入来源结构回归，后续只维护这一处接缝。
- 摘要期间有写操作 → 深拷贝与提交比较，冲突返回 409；禁止只做按钮禁用或初始 busy 检查。
- 跨渠道投递目标高度敏感 → 沿用现有渠道对象范围、可信目录、实际执行重验；不扩大个人渠道开放范围。
- Web 保留单体增加短期维护成本 → 新功能放独立模块并登记上游来源，结构迁移不在本次标完成。
- 历史全量测试并非全绿 → 新增/已开放功能失败与权限失败阻断发布；已知未开放渠道失败单列证据，不通过删除或跳过测试制造全绿。

## Migration Plan

1. 从记录的 rdai 提交建立隔离实施分支；先记录测试和原数据备份，不处理其他工作区在途修改。
2. 合入能力投影与客户端关闭逻辑；新增动作保持未开放。旧服务端/旧客户端双向兼容验收通过。
3. R1 的上下文并发保护、Web 入口和模型编辑器完成，真实验收后声明相应 slice 已实现/验收并开放。
4. R2 部署新增表与记录写入，运行历史仍关闭；证实新增实际执行记录的快照正确后接三项历史接口。
5. 创建/目标接口与历史接口分别验收，最后开放六个调度动作；不自动补齐旧记录归属。
6. 每批记录准确 SHA、数据迁移结果和测试清单。紧急回退先设置 RDAI_DISABLED_ACTIONS 并重启同一版本关闭动作，再回退客户端/后端提交；保留扩展表，旧代码忽略它，不删除运行或会话数据。

本设计无需要程序员自行选择的架构分叉；端到端测试使用隔离租户、测试模型与测试渠道，具体账号由部署环境提供，不写入文档或仓库。
