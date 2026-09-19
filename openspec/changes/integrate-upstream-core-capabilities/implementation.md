# master → rdai 功能整合开发任务书

**Goal：** 在现有 rdai 架构中交付 8 个缺失接口、上下文控制、模型目录/回退链编辑和调度创建/历史；程序员按本文和 [tasks.md](tasks.md) 开始开发。

**Architecture：** 后端通过 fork handler 接统一身份与授权服务；前端通过独立模块接现有 shell；任务、运行正文、模型配置保持各自唯一存储。新增运行归属表只存授权元数据。

**Tech Stack：** Python / web.py / SQLite；Web JavaScript；Electron / React / TypeScript；pytest / node:test。无需新增运行依赖。

**Spec：** [设计决策](design.md)、[会话上下文](specs/session-context-controls/spec.md)、[能力声明](specs/database-runtime-consumers/spec.md)、[调度](specs/database-scheduler-console/spec.md)、[模型配置](specs/platform-config-console/spec.md)。

**状态：** 本文是待实施任务书，文中“新增”文件、函数和测试均需要实现。唯一完成状态记录在 tasks.md；示例测试是起始用例，不代表当前代码已通过。

## 1. 开发入口、顺序与工期

代码基线：`rdai@f5d7d764`；上游 `8f1b19f1` 已被合入，再合并 master 不会补齐功能。以隔离检出开始；当前工作区的待办 UI、规范归档等在途修改不属于本 change。若实施时 HEAD 已变化，先核对下列接入函数，更新证据中的 SHA。

| 提交 | 内容 | 前置 | 参考工时 |
| --- | --- | --- | --- |
| P1 | 动作投影、关闭逻辑、客户端缓存失效 | 现有身份与路由门禁 | 0.5–1 人日 |
| P2 | 两个上下文接口、精确 owner 校验、压缩并发、Web UI | P1 | 1–1.5 人日 |
| P3 | 模型目录和有序回退链 Web 编辑器 | 现有 models 接口 | 0.5–1 人日 |
| P4 | 实例/接收者/创建三接口及创建界面 | P1、任务授权/配额/审计切片 | 1–1.5 人日 |
| P5 | 运行归属表、执行时写入与访问层 | 真实任务执行链路 | 1–1.5 人日 |
| P6 | 运行列表/详情/删除三接口、Web 历史和通知 | P1、P5 | 0.5–1 人日 |
| P7 | 两批真实验收、兼容、回退、证据 | 对应批次功能完成 | 0.5–1 人日 |

细化后建议安排 **5–8.5 人日**；此前 4–7 人日是粗估。运行归属和压缩并发已经计入，不能再作为“接路由顺带完成”。一名熟悉仓库的后端、一名前端可按 P1→P2/P3、P4/P5→P6 的依赖分工；联调与真实渠道环境仍会影响日历工期。R1=P1/P2/P3，R2=P4/P5/P6。

Global Constraints：

- `channel/web/route_registry.py` 是唯一 HTTP 路由/策略清单；不改 `channel/web/api/**` 和 `channel/web/core/**`。
- handler 只负责解析、作用域、调用和序列化；授权不放浏览器，不复制 TaskAccessService。
- 不把 AuthSession 当业务 session；renderer 不持 bearer。所有参数 SQL 绑定，租户/owner 不信任请求字段。
- 新能力初始关闭。声明 implemented/accepted 与实际证据一致；env 只能收紧。不要删除失败断言或打开全部能力来获得绿灯。
- 本轮不切换整个 frontend shell，不开放 Web 一键更新或微信个人渠道，不回填无证据旧历史。
- 每个提交先加入能失败的行为用例，再实现，复跑该功能及边界检查；提交前人工检查 diff 范围。

## 2. 固定 HTTP 契约

下表均在现有登录和 `X-Tenant-ID` 传输链路内；POST 使用既有 Origin/写请求保护。路径中的 sid 为 URL 编码后的业务 session ID。

| 方法 / 路径 | 输入 | 200 返回 | 新 handler |
| --- | --- | --- | --- |
| GET /api/sessions/{sid}/context_usage | query agent_id，可省略并按当前租户解析 | status + ContextUsage | SessionContextUsageHandler |
| POST /api/sessions/{sid}/compact_context | query/body agent_id，二者同时存在须一致；body 可为 {} | status、ok、available、reason、compacted_turns、before、after、usage | SessionCompactContextHandler |
| GET /api/scheduler/instances | 无 | status、instances: SchedulerInstance[] | SchedulerInstancesHandler |
| GET /api/scheduler/recipients | 可选 query instance_id，省略表示所有获准实例 | status、recipients: TaskRecipient[] | SchedulerRecipientsHandler |
| POST /api/scheduler/create | name、enabled、schedule、action | status、task: SchedulerTask | SchedulerCreateHandler |
| GET /api/scheduler/runs | agent_id、task_id、limit=100、offset=0、since | status、runs: SchedulerRun[]、history_scope | SchedulerRunsHandler |
| GET /api/scheduler/runs/detail | 必填 run_id | status、run: SchedulerRunDetail | SchedulerRunDetailHandler |
| POST /api/scheduler/runs/delete | 必填 run_id | {status:"success"} | SchedulerRunDeleteHandler |

字段名与既有客户端类型同名同义：ContextUsage、TaskSchedule、TaskAction、SchedulerTask、SchedulerRun、SchedulerRunDetail、SchedulerInstance、TaskRecipient。不改既有字段的意义。额外字段：

- 压缩 reason：`compacted | nothing_to_compact | no_live_context`；前者 ok=true，后二者 ok=false；无实例 available=false。无可压缩内容仍是 200。
- 运行列表 history_scope 恒为 `"attributed_only"`，新客户端固定说明覆盖范围，旧客户端可忽略。
- limit 必须是 1–500 的整数，offset 是非负整数，since 是非负 Unix 秒整数；非法参数 400。since 使用 `started_at >= since`，客户端以 run_id 去重。
- agent_id 省略或空值在 runs 中表示“聚合有权查看的范围”；不是默认 Agent，也不是全库。非空值只收窄；无权访问的显式 Agent 返回 404。
- records 的 extras 只投影 task_name、action_type、channel_type、instance_id、trigger、output_preview，不返回原始 extras 或内部授权快照。

新接口错误使用真实 HTTP 状态和 `{status:"error",code,message}`。沿用认证中间件的现有错误码；业务新增码如下：

| 状态 | 场景 / code | 客户端处理 |
| --- | --- | --- |
| 400 | invalid_request、invalid_schedule、invalid_target | 保留输入，展示可修正错误 |
| 401 | 现有身份失效响应 | 回登录，不重复请求 |
| 403 | 现有 permission / Origin / quota 拒绝码 | 展示拒绝原因，不当作空数据 |
| 404 | session_not_found、run_not_found、target_not_found | 不泄露他人对象存在性 |
| 409 | session_busy、context_changed、run_running | 提示稍后重试，不显示成功 |
| 503 | 现有 closed 策略；新存储故障 run_store_unavailable | 停止轮询，显示暂不可用 |
| 500 | 未预期服务异常 | 记录内部异常，响应不带路径、凭据或数据库细节 |

handler 中 `except web.HTTPError: raise` 放在宽泛 Exception 处理之前，防止把 403/404/409 包装成 HTTP 200。现有 `/api/models` 某些失败返回 200 + status=error，新模块必须同时检查 HTTP 状态和 body.status。

创建请求例子（仅测试标识）：

```json
{
  "name": "日报提醒",
  "enabled": true,
  "schedule": {"type": "interval", "interval": "1h"},
  "action": {
    "type": "send_message",
    "channel_type": "feishu",
    "instance_id": "fixture-instance",
    "receiver": "fixture-recipient",
    "content": "请提交日报"
  }
}
```

create 不接收 owner、tenant_id、revision、write_coordinator、task id 或 public scope；传入受保护字段返回 400。action.instance_id 必填。Agent 由获准实例的当前绑定决定；客户端 agent_id 若存在且不一致返回 400。enabled 必须为 bool；name 非空。schedule 使用现有 at/interval/cron 校验及 `SchedulerService._calculate_next_run`，计算结果为空或时间已过返回 invalid_schedule，不自行实现另一套解析器。

## 3. P1：能力投影与客户端请求门禁

**修改：** `auth/capability_matrix.py`、`auth/service.py::context_for_tenant`、`channel/web/route_registry.py`；Web `channel/web/static/js/console.js`。

**新增：** `channel/web/static/js/functional-capabilities.js`；`tests/test_feature_action_projection.py`、`tests/test_feature_action_clients.cjs`。

保持现有 scheduler slice 的 list/run/toggle/update/delete。新建下表 slice，每个只有表中一个 action，便于逐项验收。新增 `feature_action_availability() -> dict[str, dict]` 返回完整八键；`context_for_tenant` 把它赋给 feature_actions。

| feature_actions 键 | slice ID / action | access |
| --- | --- | --- |
| session_context.usage | session_context_usage / usage | read |
| session_context.compact | session_context_compact / compact | execute |
| scheduler.instances | scheduler_instances / instances | read |
| scheduler.recipients | scheduler_recipients / recipients | read |
| scheduler.create | scheduler_create / create | config |
| scheduler.runs.list | scheduler_runs_list / list | read |
| scheduler.runs.detail | scheduler_runs_detail / detail | read |
| scheduler.runs.delete | scheduler_runs_delete / delete | config |

在注册表构建阶段读取 `RDAI_DISABLED_ACTIONS` 一次，解析后只移除对应 slice.open 动作；保留原 implemented/accepted。未知键报启动配置错误。新八项均要求 implemented 且 accepted 才允许进入 open，包含 read/config；不要只依赖已有 execute 检查。返回 reason 固定为 `not_implemented | not_accepted | disabled_by_deployment | ""`，按该顺序判断。测试用新进程或重建注册表验证环境变量，不在请求中反复读 env。

Web 新模块命名空间 `window.RdaiFunctionalCapabilities`：

- `available(context, key) -> boolean`：仅 context.status=success 且 feature_actions[key].available===true。
- `capture(contextRevision, agentId, sessionId) -> object`：请求开始记录版本；提交结果时与当前值全部相等才更新 UI。
- 接入 `_fetchTenantAuthorization`；`_invalidateAuthContext` 递增 `_authContextSeq`、清除旧 promise 引用/数据并触发新功能轮询停止。旧 finally 不可清除新请求。登出、租户切换、网络重连都失效，不能借用原菜单“未知则可见”的宽松语义。

R1/R2 UI 各自依赖需要的动作：压缩用 compact；用量用 usage；创建入口同时依赖 instances/recipients/create；列表、详情、删除各自依赖对应键。暂不可用与无权限分开表达。

**首个测试：** `feature_action_availability()` 的键集合恰好为上述八键；关闭其中一个只影响对应 route 的 policy 与投影，其他旧 scheduler 动作仍开放。客户端缺 feature_actions、缺单键、available=false 均不得发请求；延迟返回的旧租户响应不得恢复按钮。

## 4. P2：会话作用域、压缩算法与 Web 入口

**新增：** `channel/web/fork/handlers/context.py`、`channel/web/static/js/functional-context.js`；`tests/test_session_context_scope.py`、`tests/test_context_compaction_concurrency.py`、`tests/test_functional_context.cjs`。

**修改：** `channel/web/web_channel.py` 导出、route_registry、`fork/authorization.py::_require_owned_session`、`agent/protocol/agent.py::compact_context`、Web chat.html/资产登记/console.js。

新增函数 `_owned_context_target(ctx, session_id: str, agent_id: str | None) -> tuple[str, ConversationStore]`：

1. 先将现有 `_require_owned_session` 的查询限定到已绑定 store 的存储 Agent 键和当前 tenant，保留其“无行时不拒绝”以兼容其他入口，避免同名 session 误匹配他人行。新 handler 再按 history.read → `_require_session_scope` 取得真实 Agent；从 registry 的 profile.workspace 取得受信 ConversationStore。
2. 用该已绑定 store 的 `_dimensions()["agent_id"]` 匹配 sessions 的 agent_id，同时匹配 tenant_id=ctx.tenant_id、owner=ctx.user_id、session_id 和 channel_type='web'。无行 404。不使用仅 WHERE session_id 的现有检查作为最终证据。
3. 返回业务 Agent ID 和 store。压缩另检查 chat.use、`_require_agent_action(ctx, resolved, "use")`；不得因为管理员而省略 durable owner 条件。
4. 通过 bridge.peek_agent 原业务 session_id + resolved 查现有运行时；无实例返回 no_live_context。GET 不 get_agent、不模型调用、不初始化工具。

POST 活动检查调用 `get_cancel_registry().has_active(bridge.scoped_session_key(sid, resolved))`；true 返回 session_busy。然后调用既有 compact_context(keep_recent_turns=2)，不把此参数开放给客户端。

**必须改原 compact_context 内部的并发窗口：**

1. 持 messages_lock 深拷贝为 snapshot；在 snapshot 上 identify_complete_turns，分出 discarded/kept。
2. 锁外对 discarded 计算摘要；在另一个深拷贝的 kept 消息上构造 replacement，保证 snapshot 本身未被注入摘要。
3. 持锁比较 `self.messages != snapshot`：不同则返回 ok=false/reason=context_changed，不赋值、不写长期记忆。
4. 相同则替换 self.messages，并在同一锁内将 last_usage 清空；锁外才 write_daily_summary。保留原摘要降级算法；长期记忆写失败不撤销已提交的压缩。
5. handler 把 context_changed 映射 409；正常/no-op 返回即时 usage。不要在 handler 外持 messages_lock 调该方法。

并发起始测试可以直接验证原算法，放入新测试文件：

```python
import copy
import threading
from types import SimpleNamespace
from unittest.mock import Mock
from agent.protocol.agent import Agent

def test_compaction_does_not_replace_messages_added_during_summary():
    agent = Agent.__new__(Agent)
    agent.messages_lock = threading.Lock()
    agent.last_usage = {"input_tokens": 123}
    agent.messages = [
        {"role": role, "content": [{"type": "text", "text": f"{role}-{n}"}]}
        for n in range(4) for role in ("user", "assistant")
    ]
    before = copy.deepcopy(agent.messages)
    arrived = {"role": "user", "content": [{"type": "text", "text": "new"}]}
    flush = Mock()
    def summarize(messages, max_messages=0):
        with agent.messages_lock:
            agent.messages.append(copy.deepcopy(arrived))
        return "summary"
    flush._summarize_messages.side_effect = summarize
    flush._clean_summary_output.side_effect = lambda value: value
    agent.memory_manager = SimpleNamespace(flush_manager=flush)
    result = agent.compact_context()
    assert result["ok"] is False
    assert result["reason"] == "context_changed"
    assert agent.messages == before + [arrived]
    flush.write_daily_summary.assert_not_called()
```

另测两线程在摘要 barrier 同时等待，释放后恰好一次提交；两个线程必须 join(timeout) 并断言退出，捕捉死锁。完整 HTTP 矩阵用 tests/_helpers.py 的 WebAppHarness；创建 sessions 测试数据通过 identity_scope + ConversationStore.append_messages，不伪造 handler 的授权结果。

Web `RdaiFunctionalContext` 导出 `mount({root, getContext, getSession, request, t})` 返回 `refresh()/dispose()`；getSession 返回 `{agentId,sessionId,persisted}`。在 console.js 当前会话切换/销毁处 refresh/dispose；首次落库前不轮询。展示 estimated 标记、used/limit、压缩中禁用和失败原因。

## 5. P3：模型目录与回退链

**新增：** `channel/web/static/js/functional-models.js`、`tests/test_functional_models.cjs`。

**修改：** console.js 的 `openChatFallbackModal`、`renderVendorsSection` / provider 卡片；chat.html、`fork/handlers/pages.py` 资产登记、`static/js/i18n/models-config.js`。

新模块只导出 `window.RdaiFunctionalModels`，纯数据函数与 DOM 分开：

- `buildFallbackPayload(enabled, rows)` → set_capability JSON；rows 每项 provider/model 必填，保留原顺序，自定义 provider ID 原样保留；enabled=true 且空链抛输入错误。
- `buildCatalogPayload(providerId, draft)` → save_catalog JSON；draft 为 `{seed,overrides,hidden}`，models 只取 overrides，绝不能取 effective。
- `mountFallback({root, capability, providers, save, t})`、`mountCatalog({root, provider, save, t})` → 各返回 dispose()；save 接 payload 并返回 Promise，失败保留草稿，成功重新 GET /api/models 回读。

回退链请求：

```json
{"action":"set_capability","capability":"chat_fallback","enabled":true,"chain":[{"provider":"zhipu","model":"model-a"},{"provider":"custom:fixture","model":"model-b"}]}
```

目录请求：

```json
{"action":"save_catalog","provider_id":"zhipu","models":[{"name":"custom-model","capabilities":["text"],"context_window":32000}],"hidden":["preset-model"]}
```

读取 provider.seed/catalog/hidden/effective；draft.overrides 来自 catalog。修改预设写覆盖；删除预设清其覆盖并加 hidden；恢复预设移除 hidden；删除新增模型只删覆盖；恢复默认清空 models/hidden；重命名视为删除旧项并新增。保留条目未编辑字段，重复 name、非法窗口/输出上限和未知 capability 由 UI 拒绝，后端仍做最终校验。禁用回退链保留节点配置，不能丢链。

Node 起始测试（模块以 IIFE 暴露命名空间，不依赖执行时已有 DOM）：

```javascript
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const sandbox = {window: {}};
vm.runInNewContext(
  fs.readFileSync('channel/web/static/js/functional-models.js', 'utf8'),
  sandbox
);
const api = sandbox.window.RdaiFunctionalModels;
test('catalog saves overrides without freezing untouched presets', () => {
  const result = api.buildCatalogPayload('zhipu', {
    seed: [{name: 'preset', capabilities: ['text']}],
    overrides: [{name: 'added', capabilities: ['text']}],
    hidden: []
  });
  assert.deepEqual(JSON.parse(JSON.stringify(result.models)), [
    {name: 'added', capabilities: ['text']}
  ]);
});
```

补链条排序保存回读、隐藏/恢复、custom provider、失败保留草稿和 XSS 文本转义用例；zh/zh-Hant/en 同步文案。权限仍由 models 平台策略判断，不修改普通成员模型目录页面的读权限。

## 6. P4：实例、可信接收者和任务创建

**新增：** `channel/web/fork/scheduler_targets.py`、`tests/test_scheduler_create_scope.py`。

**修改：** `fork/handlers/scheduler.py`、web_channel 导出、route_registry、`agent/tools/scheduler/recipient_store.py::list`。

新类 `SchedulerTargetService(identity_service, recipient_store)` 的接口：

- `list_instances(ctx) -> list[dict]`：调用 IdentityService.list_tenant_channel_instances(actor_user_id=ctx.user_id, tenant_id=ctx.tenant_id)，取现有授权范围，再过滤未开放渠道、web/unknown、无有效 Agent 绑定、不可投递实例。
- `list_recipients(ctx, instance_id: str | None = None) -> list[dict]`：先取得合法实例集合；显式 ID 不在集合中返回 target_not_found；对 RecipientStore.list(instance_ids=set) 查询。返回值仅为 TaskRecipient 的白名单字段。
- `resolve_target(ctx, instance_id: str, receiver: str) -> tuple[str, dict]`：重新验证当前实例授权/绑定与目录精确 get(instance_id,receiver)，返回 Agent ID 和可信接收者元数据；过期/不存在/越权拒绝。

RecipientStore 是 JSON 文件目录，`list(self, instance_ids: set[str] | None = None) -> list[dict]` 在服务器持锁读取后按结构化 instance_id 过滤再排序；None 保持旧调用，全空集合返回 []。不要拆解由冒号拼接的 key，不把全局目录发到客户端过滤。list_instances 的 recipient_count 仅统计获准实例目录。

创建处理顺序固定：

1. 解析 payload/类型；进入 _db_scope，使用 _scheduler_actor(ctx)。拒绝受保护字段和 HTTP public 创建。
2. resolve_target 取可信 Agent/receiver/channel/session。客户端 channel_type 若与可信值不同返回 invalid_target；receiver_name、is_group、notify_session_id 只由目录赋值。
3. 白名单保留 send_message 的 content，或 agent_task 的 task_description/silent；去掉另一种动作专属字段。验证非空文本。
4. 沿用上游 create 的 schedule 校验及下一次运行计算，生成 uuid4().hex。组成 personal task_data；不自行赋 owner/revision。
5. `_scheduler_access(ctx).create_task(actor, resolved_agent_id, task_data)` 是唯一写入口；服务执行成员、Agent use、配额、owner、审计校验。响应 task 使用既有领域返回形状。
6. 创建并不代替执行时校验。定时触发/手动触发仍在 integration 的现有 trusted execution identity 下重验 owner、绑定、投递 readiness。重绑定后不可继续投递给旧对象。

起始 HTTP 失败用例（能力开放后运行；关闭行为由 P1 单测覆盖）：

```python
import json

def test_create_rejects_untrusted_target_without_writing_task(web_app):
    app = web_app("scheduler-create")
    app.add_agent("shared-agent")
    role = app.role("creator", ["chat.use", "agent.use", "agent.read"],
                    grants=[("agent", "agent:shared-agent", "use")])
    app.member("alice", [role["code"]])
    token = app.login("alice")
    response = app.post("/api/scheduler/create", {
        "name": "test",
        "enabled": True,
        "schedule": {"type": "interval", "interval": "1h"},
        "action": {"type": "send_message", "channel_type": "feishu",
                   "instance_id": "untrusted-instance",
                   "receiver": "unknown", "content": "hello"}
    }, token=token)
    assert response.status.startswith("404")
    assert json.loads(response.data)["code"] == "target_not_found"
    listed = app.get("/api/scheduler", token=token)
    assert json.loads(listed.data)["tasks"] == []
```

其余矩阵：本人实例正向；同租户他人 user 实例；跨租户实例；管理者可见 tenant 实例但不可见他人 user 实例；伪造 receiver；省略 instance_id；伪造 owner/Agent/public；无 Agent use；配额达到上限；非法 Origin；创建成功后执行重绑定拒绝。创建成功必须审计一次且任务只落现有全局 TaskStore。

## 7. P5：运行归属表与访问层

**新增：** `agent/tools/scheduler/run_repository.py`、`run_access.py`；`tests/test_scheduler_run_scope.py`。

**修改：** `agent/tools/scheduler/integration.py::_record_scheduler_run`。建表完整 SQL 以 [design.md D6](design.md#d6用同库授权扩展表固定运行记录归属) 为准；repository.ensure_schema() 重复调用幂等，无旧数据回填。

新增数据类型均放 run_repository.py：

| 类型 | 字段 / 含义 |
| --- | --- |
| RunScope（frozen dataclass） | run_id, tenant_id, owner_user_id, scope, agent_id, task_id, session_id, provenance_version=1, created_at；字段和表一致 |
| RunGrant（frozen dataclass） | tenant_id, user_id, personal_agent_ids: tuple[str,...], public_agent_ids: tuple[str,...]；只能由访问服务创建 |
| RunQuery（frozen dataclass） | agent_id: str="", task_id: str="", since: int\|None=None, limit=100, offset=0；先完成范围/数值校验 |

新增 `RunScopeRepository(store: ConversationStore)`：

- `ensure_schema() -> None`：同库建表/索引；失败抛 TaskAuthorizationError(code=run_store_unavailable,status=503)，不吞成空数据。
- `record(scope: RunScope) -> None`：检查已有 runs 的 run_id/task_source/agent_id/task_id/session_id 与快照一致；INSERT OR IGNORE 后核对完整归属，禁止 update 认领。
- `list_visible(grant: RunGrant, query: RunQuery) -> list[dict]`：授权条件先于分页；返回内部 run+scope 行，服务层再投影。
- `get_visible(grant: RunGrant, run_id: str) -> dict | None`：同一授权 WHERE，只有一个 run_id 参数。
- `delete_visible(resolve_grant: Callable[[], RunGrant], run_id: str) -> dict`：BEGIN IMMEDIATE 后调用可信 resolver 重新取得成员/授权范围，在同一连接查记录、检查状态、删两表并提交；返回被删 ID 用于审计。不存在404，running409。异常回滚；绝不删除 messages。

新增 `RunAccessService(repository, task_access, actor_resolver, agent_ids)`：task_access 使用现有 _scheduler_access(ctx)；actor_resolver 每次由已验证 AuthSession/tenant 重建 TaskActor，不能信任旧页面权限；agent_ids 使用 _scheduler_agent_ids。

- `list_runs(query: RunQuery) -> dict`：生成 view grant、查询、投影，返回 HTTP 成功体。
- `get_run(run_id: str) -> dict`：view grant 查行；无行抛 run_not_found；按第 8 节读取安全正文。
- `delete_run(run_id: str) -> None`：manage grant 交事务删除；按既有审计写操作 ID/结果，不记录正文。

**grant 算法：** 当前有效成员才有 grant。personal_agent_ids 为当前租户绑定且启用的 Agent，personal 分支另外强制 owner=当前用户；不能以 agent.use 撤销为由剥夺既有 owner 的查看/管理权。公共 view 按 TaskAccessService.decide 的现行规则允许成员查看这些 Agent 的 public 任务；公共 manage 仅允许该服务认定的管理员。通过相同 scope/owner 的最小 task 对象调用 decide，避免复制 admin 判断。任务删除后继续按执行快照判定，当前 task 的 scope/owner 改变不能改写历史归属。

SQL 模板如下；PA/UA 由上述可信 grant 产生，分别展开为与参数数目相同的 ? 占位符，不能插入 ID 文本。空集合对应支路改为 `0=1`。

```sql
SELECT r.*, s.tenant_id AS scope_tenant_id,
       s.owner_user_id AS scope_owner_user_id, s.scope AS run_scope
FROM runs AS r
JOIN fork_scheduler_run_scopes AS s ON s.run_id = r.run_id
WHERE r.task_source = 'scheduler'
  AND s.provenance_version = 1
  AND r.agent_id = s.agent_id AND r.task_id = s.task_id
  AND r.session_id = s.session_id
  AND s.tenant_id = ?
  AND (
    (s.scope = 'personal' AND s.owner_user_id = ? AND s.agent_id IN (/* PA */))
    OR
    (s.scope = 'public' AND s.agent_id IN (/* UA */))
  )
ORDER BY r.started_at DESC, r.run_id DESC
LIMIT ? OFFSET ?;
```

可选 agent_id/task_id/since 条件插入 ORDER BY 之前，全部参数绑定；since 为 `r.started_at >= ?`。先全库 LIMIT 再 Python 过滤是错误实现。详情和删除复用同一 WHERE builder，不调用 ConversationStore.list_runs/get_run_detail 作为授权查询。

执行记录写入：

1. _record_scheduler_run 先确认可信执行身份：personal owner.tenant_id/user_id 与执行身份一致；public 使用重验后的 tenant/Agent 绑定。无法证明只写原 runs，不赋 scope。
2. 本次新增记录的 runs.agent_id 和 scope.agent_id 统一写真实业务 Agent ID；默认 Agent 也先通过可信 runtime/registry 解析，不写空值。这是 runs 的业务键，不等于消息表默认 Agent 的存储空键。
3. 原 create_run 成功后 record(scope)，本次 generated run_id 原样复用。异常记录可诊断错误；保留原 (store,run_id) 返回，保证 finish_run 仍可收尾且任务不重投。
4. 不修改已存在 runs 的归属。写 runs 后进程退出的半成品无 scope，不可见。scope 写失败不能令发送重试，也不能退回全局历史查询。

**首个 repository 用例：** 同一共享 Agent 同时插入 Alice/Bob 各 3 个 personal run、一个 public run 和一个无 scope 旧 run；Alice limit=2/offset=0 返回两条获准记录，offset=2 后仍为获准记录，不受 Bob 插入影响；Bob 的详情/删除404；管理员不得读 Alice 私有记录；public 按 view/manage 分别验证。固定 started_at 相同再验证 run_id 次序。

再覆盖：Agent 改绑后旧 tenant 快照不随迁移；删除 task 不泄露历史；重复建表/record；冲突归属不可改写；record 在 create_run 后故障不影响派发/finish；权限撤销、租户失效、running 删除和并发双删除；SQLite rollback 后两表一致。此组测试用真实临时 SQLite，不用 mock SQL 返回来证明分页权限。

## 8. P6：历史接口、正文关联和调度界面

**修改：** fork scheduler.py 加列表/详情/删除 handler；web_channel、route_registry。

**新增：** `channel/web/static/js/functional-scheduler.js`、`tests/test_scheduler_run_http.py`、`tests/test_functional_scheduler.cjs`。Web 三个业务模块为 models/context/scheduler；capabilities 是共享门禁模块。

HTTP 使用 _db_scope → _scheduler_actor / _scheduler_access → RunAccessService；全局 ConversationStore 只从注册表的默认 workspace 解析，异常返回503，不能接受客户端文件路径。repository 是唯一接触 runs SQL 的新增组件。

详情的 full_output：

1. 获准 run 的 output_preview 可直接显示；full_output 初始为 null。
2. 只有 ctx 具备 history.read，且该 run.session_id 在 Web sessions 表中精确归属当前 tenant/user/Agent，才读取正文。非 Web、无归属或当前无会话权限保持 null。
3. 查询 messages 强制 tenant_id、owner、session_id、Agent 存储键，并且 run_id=当前 run_id、role='assistant'；按 seq 合并其文本。存储 Agent 键从 registry workspace 对应 store 的 _dimensions() 得到，默认 Agent 可能为 ''。
4. 找不到精确关联返回 null；首版不使用 started_at 附近的消息或最后一条调度消息猜测归属。不新增正文副本。消息含其他 run_id 的场景必须测试。

前端接口保持旧方法形状：保留 api.getSchedulerRuns(): Promise<SchedulerRun[]> 给通知等旧调用；新增 `getSchedulerRunPage(...): Promise<{runs: SchedulerRun[], history_scope: "attributed_only"}>` 给历史页，旧方法调用新方法并返回 runs。body.status!=success 统一抛错误，不使用 `catch(() => [])`。getSchedulerRecipients 增加可选 instanceId 参数；旧无参仍得到服务端已过滤合集。

Web `RdaiFunctionalScheduler.mount({root,getContext,request,t})` → refresh()/dispose()，接 console.js 的 `loadTasksView` / `refreshTasksView`；实例→接收者→创建，提交期间禁用，不自动重试 create。历史支持加载更多、详情、删除；删除确认后调用真实 API，成功才移除。详情失败显示错误，full_output=null 时展示 preview 并标注“预览”。

通知轮询依赖 scheduler.runs.list；暂停/登出/换租户/重连先清定时器和已见 run_id，更新 context revision。每次请求带 since，保留同秒重复边界并按 run_id 去重；单次到达 limit 时先翻页取完再推进 since，防止同秒多次运行漏通知。权限/服务错误停止轮询并显示状态；恢复连接后重取能力投影才启动。

新静态文件统一在 chat.html 中按 capabilities→models/context/scheduler→console.js 顺序 defer 加载，在 fork/handlers/pages.py 的资产版本清单登记。DOM 读取放 mount，避免加载时节点还不存在；dispose 清事件/计时器。新增文案加入现有 `static/js/i18n/models-config.js`、`tasks-records.js`、`core.js`，分别管理模型、调度和上下文；三语言齐全。

## 9. 验证命令与发布门槛

以下在仓库根运行；新测试文件实现后才可执行。本机用 .venv/bin/python，默认 python3 已观察到启动被终止，不能把该环境问题归为业务测试通过。

```bash
# P1 / P2
.venv/bin/python -m pytest -q tests/test_feature_action_projection.py tests/test_session_context_scope.py tests/test_context_compaction_concurrency.py tests/test_web_database_capability_acceptance.py tests/test_route_registry.py tests/test_web_module_seams.py
node --test tests/test_feature_action_clients.cjs tests/test_functional_context.cjs

# P3
.venv/bin/python -m pytest -q tests/test_model_catalog_api.py tests/test_chat_model_fallback.py
node --test tests/test_functional_models.cjs tests/test_console_search_providers.cjs

# P4 / P5 / P6
.venv/bin/python -m pytest -q tests/test_scheduler_create_scope.py tests/test_scheduler_run_scope.py tests/test_scheduler_run_http.py tests/test_scheduler_task_authorization.py tests/test_scheduler_web_update.py tests/test_scheduler_run_records.py tests/test_scheduler_cross_channel_recipients.py tests/test_conversation_runs.py
node --test tests/test_functional_scheduler.cjs tests/test_feature_action_clients.cjs

# 两批通用门禁
.venv/bin/python -m pytest -q tests/test_no_resurrection_legacy_identity.py tests/test_route_registry.py tests/test_web_module_seams.py

# 发布候选的完整回归；在干净隔离检出执行一次并保存日志
.venv/bin/python -m pytest -q
```

修改 tests/test_web_database_capability_acceptance.py 时，只移除本批确已交付接口的缺口预期，换成真实 build_web_app 正向/拒绝断言。Web 一键更新等未交付项仍保持关闭预期。route provenance 测试必须证明八个类来自 fork；上游 api/core 文件与参考提交保持一致。

真实验收矩阵（模拟 provider/派发不替代下面门槛）：

| 批次 | 操作 | 通过证据 |
| --- | --- | --- |
| R1 | 查询/压缩本人非默认 Agent 会话 | 用量更新、最近消息保留；非 owner 拒绝 |
| R1 | 两节点回退链与目录覆盖保存、刷新、回读 | 顺序/hidden 一致；未编辑 seed 没写入覆盖 |
| R1 | 当前 server 配旧 client、旧 server 配新 client | 旧 client 可忽略新字段；新 client 不请求缺失能力 |
| R2 | 真实测试渠道创建个人任务、执行一次 | 只有一次任务和投递；owner/tenant/Agent/scope 正确 |
| R2 | 运行历史、详情、删除 | 页面与接口一致；旧无归属记录不展示；删除不删消息 |
| R1/R2 | 两用户同 Agent、两租户、管理员、权限撤销 | 不越权、不跨身份复用状态；权限失败阻断发布 |
| R1/R2 | 设置关闭变量并重启、回退前一版本 | 声明/路由/入口一致关闭；原任务/会话仍可用 |

**已知基线处置：** 原隔离 HEAD 为 5778 passed、27 failed、31 skipped（其中 1 个失败是源清单子测试），27 项都在合并前复现，但不能据此豁免本轮发布。22 个微信相关失败对应未开放能力，保留失败证据和关闭边界；两项外部连接权限失败必须按当前 spec 判断真实漏洞还是旧预期，若真实授权缺陷则阻断发布并修复。个人前端旧文件断言、菜单版本断言和 source-manifest 引用的未跟踪 README 要补正确断言/跟踪依据，不能简单删除测试。基线治理单独提交，避免与功能变更混在一起。

## 10. 交付证据、启用与回退

实施时新增 `evidence/acceptance.md`，每批记录：代码 SHA、测试命令/计数/失败说明、Web 端版本、真实测试步骤与脱敏结果、迁移/重启/回退结果。新增 `evidence/source-map.md`，记录功能→上游 SHA/符号→fork 调用点→验收测试，供后续前端结构迁移复用。

执行顺序：

1. P1 合入时新八动作均关闭；已实现的 slice 只更新 implemented，真实验收后才 accepted/open。测试环境可通过明确测试 fixture 注入待测注册表，生产变量不能绕过验收。
2. P2/P3 和 R1 验收完成后开放 usage/compact；models 沿用现有平台能力。先保留 R2 六键在 RDAI_DISABLED_ACTIONS 中。
3. P5 建表和写入先上线，历史查询仍关闭。执行一条真实测试任务，确认 run 和 scope 正确；不批量归属既有 runs。
4. P4/P6/R2 验收完成后分别开放目标/创建与历史动作，删除对应部署 deny 键并重启；刷新 Web 端上下文。
5. 紧急回退：将受影响完整键写入 RDAI_DISABLED_ACTIONS 并重启当前版本→确认503和停止请求→回退该批代码/客户端。保留新增表及原 runs/messages，不执行删表脚本，不逆向改写已存配置。

OpenSpec 交付前检查：

```bash
openspec validate integrate-upstream-core-capabilities --strict
openspec status --change integrate-upstream-core-capabilities
```

开始实现使用 `/openspec-apply integrate-upstream-core-capabilities`。按 tasks.md 更新进度；未满足真实验收前，不把新增功能开放或将 change 归档。
