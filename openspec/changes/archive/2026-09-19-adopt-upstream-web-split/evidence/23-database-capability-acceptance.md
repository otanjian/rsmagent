# 23. database 能力逐项验收（`adopt-upstream-web-split`，规范 §6.4）

- 记录日期：2026-09-19
- 候选：本 change 的 PR 头（合并提交 `163951b5` + 收口提交 `4cd86956`/`762c301b`/`ad6f7666`/`bd44bf1f`/`4cd829ff` 的工作树）
- 规范依据：`doc/master合并到rdai分支规范.md` §6.4
- 门槛口径：能力只有同时取得 **database 正向业务成功 + 授权隔离通过 + 真实入口可达** 三类证据才可标为「已通过」。
- 用例文件：`tests/test_web_database_capability_acceptance.py`（新增，26 个用例）

## 1. 方法与夹具（为什么这样取证）

本轮合并的核心风险不是"上游多了一个业务功能"，而是**上游拆分的 web 层被重新落到 database 身份上**
（`evidence/11`/`19`）。因此验收的对象是**入口与授权**：真实 `build_web_app()` 路由 → 会话/租户解析 →
平台/租户平面判定 → handler。夹具按 §6.4 的字面要求搭建：

| 要求 | 实现 |
| --- | --- |
| 独立测试身份库 | `WebAppHarness` 的私有 `identity.db`（真实 `IdentityService`，非行注入） |
| 至少两个租户 | `acme`（bootstrapped，平台管理员 `root`）与 `globex`（经平台管理员路径创建的**真**租户管理员） |
| 多个用户 | 每租户一个管理员 + 一个 builtin `member`；平台管理员一个 |
| 真实入口 | `build_web_app()` 的真实 WSGI 应用；每请求按 `X-Tenant-ID` 指定租户 |
| 三类证据 | 正向业务动作（读回校验）、匿名/无权/跨租户拒绝、`closed` 策略与 handler 到达 |

一个必须写明的夹具约束：`COW_DATA_DIR` 指向 `<tmp>/data`，与 `<tmp>/instance` 并列。若把它指到实例根之上
（`<tmp>`），`common/state_dir.py::_assert_tenant_roots_do_not_contain` 会把租户共享根判为
"home/global escape" 而让 `/api/memory` 返回 503 `memory_unavailable`——那是夹具自伤，不是产品缺陷
（首轮 5 个失败里就有这一条，已按上址修正后消失）。

夹具用 `patch.dict(os.environ)` 把 `config.json`、`system/models.json` 的写入限制在临时目录，**不触碰运行树**。

## 2. 逐能力判定（对照 `doc/master合并到rdai-同步报告-2026-09-18.md` §6 清单）

状态口径：**✅ 通过** = 三类证据齐备；**🟡 部分** = 入口与授权已实测，但某一类（真实模型答复/真实提供方/
真实客户端）依赖外部条件未产生；**⬜ 未通过** = 保持关闭或未验收。

| # | 能力（§6 行） | database 正向 | 授权隔离 | 真实入口可达 | 判定 | 证据 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 对话与流式传输 | ✅ `/message` 到达 handler；`stream/poll/cancel` 匿名拒绝 | ✅ 匿名 401、跨租户 403 | ✅ 非 503 | 🟡 身份/授权/链路通过；**真实模型答复**由既有桩测试覆盖，未在本环境产生 | `TenantPlaneAcceptance`；`tests/test_web_consumer_closure.py`、`test_chat_identity_context.py`、`test_comprehensive_chat_entry.py` |
| 2 | 附件与工作区 | ✅ `/api/workspace/tree` 返回 `status=success` 的真实列表 | ✅ 匿名 401、跨租户 403；越界路径由既有切片拒绝 | ✅ | ✅ 通过（Web 面） | `TenantPlaneAcceptance`；`test_console_workspace_transport.py`、`test_fork_multipart_agent_scope.py`、`test_scoped_project_browse.py` |
| 3 | 模型与工具 | ✅ `/api/models` 平台管理员读回；搜索凭据/目录/备用链写入-回读一致 | ✅ 租户管理员与普通成员 403、平台写 403 | ✅ | ✅ 通过（平台平面） | `PlatformPlaneAcceptance`、`SearchProviderAcceptance`、`FallbackChainAcceptance`、`ModelCatalogAcceptance` |
| 4 | 技能 / MCP | ✅ `/api/skills` 返回 `status=success` | ✅ 匿名 401、跨租户 403 | ✅ | ✅ 通过（读面）；MCP OAuth 回调为设计上免鉴权（注册表登记） | `TenantPlaneAcceptance`；`test_skill_*`（全量回归内） |
| 5 | 知识与记忆 | ✅ `/api/memory`、`/api/knowledge/list` 返回 `status=success` | ✅ 匿名 401、跨租户 403；owner 拒绝先于读取由既有切片背书 | ✅ | ✅ 通过（读面） | `TenantPlaneAcceptance`；`test_memory_console_scope.py`、`test_plan_3_1_joint_acceptance.py` §11.2 |
| 6 | 定时任务 | ✅ `/api/scheduler` 返回 `status=success`；5 个恢复入口按 registry 派生 | ✅ 匿名 401、无租户 400、无权限 403、跨租户 403 | ✅ 无 `closed` 策略 | ✅ 通过 | `TenantPlaneAcceptance`；`test_recovered_entry_acceptance.py`、`test_scheduler_task_authorization.py`、`test_scheduler_web_update.py` |
| 7 | 渠道实例与消息收发 | ✅ `/api/tenant/channels` 返回 `status=success`；实例创建/改名/断开由既有切片 | ✅ 跨租户不可区分（403/404 同形） | ✅ | ✅ 通过（**配置面**）；个人渠道**执行面**保持关闭 | `TenantPlaneAcceptance`；`test_tenant_channel_isolation_acceptance.py`、`test_console_channel_manager_resolution.py` |
| 8 | 项目操作 | ✅ `/api/projects/browse` 返回 personal scope 的真实列表 | ✅ 匿名 401、跨租户 403；`O_NOFOLLOW` 逐次重解析 | ✅ | ✅ 通过 | `TenantPlaneAcceptance`；`test_scoped_project_browse.py`、`test_project_browser.py` |
| 9 | OpenAI 兼容 API | ✅ 数据库会话 + 租户后到达精确拒绝（404 `no_agent`），不再是 503 | ✅ 匿名 401 | ✅ | ✅ 通过（授权/链路）；真实推理另需提供方 | `tests/test_consumer_closure_acceptance.py` |
| 10 | 一键更新 | — | — | ⬜ 3 条 `/api/update/*` 未注册（保持不路由，见 §candidate/`evidence/21` §E） | ⬜ 未通过（按决策不路由） | `KnownGapAcceptance` |
| 11 | 日志与版本 | ✅ `/api/version` 公开可达且带 `version`/`install_kind`/`update_supported`/`platform`；`/api/logs` 平台管理员 200 | ✅ 租户管理员/成员 403、匿名 401 | ✅ | ✅ 通过 | `VersionAcceptance`、`PlatformPlaneAcceptance`；`test_web_console_update.py` |
| 12 | 语音 | — | — | ✅ 已登记；`voice` 开关与许可按既有切片 | 🟡 部分：本轮未新增语音验证，沿用既有覆盖 | 全量回归内 `test_custom_voice.py` 等 |
| 13 | 多 Agent 运行时隔离 | ✅ `/api/agents` 返回 `status=success`，按调用者范围收窄 | ✅ 跨租户 403；owner 边界由既有切片 | ✅ | ✅ 通过（Web 面） | `TenantPlaneAcceptance`；`test_plan_3_1_joint_acceptance.py`、`test_scope_consistency_acceptance.py` |
| 14 | 控制台前端 | — | — | ✅ `/chat` 由真实应用提供 | ⬜ 未通过：前端模块化属 `adopt-upstream-web-frontend-split`，本 change 按 `keep-fork` 保持逐字节相同 | 同步报告 §0.5 |
| 15 | Desktop | — | — | ⬜ `desktop_tenant_context` 切片保持 `accepted=false`；8 条桌面接口未路由 | ⬜ 未通过：真实打包客户端演练未执行（承接方 change） | `evidence/deferred-upstream-frontend.md` §D1 |

## 3. 链路完整性（"没有 503"的可执行证据）

| 断言 | 用例 | 结果 |
| --- | --- | --- |
| 注册表中不存在 `closed` 策略 | `test_no_registered_route_is_closed_in_database_mode` | 通过（`derive_route_policy()` 全量 0 条 `closed`） |
| 平台平面 4 条入口平台管理员 200 | `PlatformPlaneAcceptance` | 通过 |
| 租户平面 10 条入口成员返回 `status=success` | `test_the_listing_entries_answer_the_member_with_a_success_payload` | 通过（`/api/history` 需 `session_id`，单列可达断言） |
| 未收口端点如实 404 | `test_the_update_and_scheduler_run_endpoints_are_not_routed` | 通过（9 条 `/api/update/*`、`/api/scheduler/runs*`、`/create`、`/recipients`、`/instances`） |
| 上下文用量端点被会话通配吞掉 → 405 | `test_the_context_budget_endpoints_are_swallowed_not_served` | 通过（`/api/sessions/<id>/{context_usage,compact_context}` 未被服务） |

最后一条是本轮新发现的口径差异：这两条不是"未注册"（404），而是被 fork 既有的
`/api/sessions/(.*)` 捕获后方法不允许（405）。桌面端仍然拿不到结果，但缺口形状要如实记录，
不能笼统写成 404。

## 4. 命令与真实输出

```text
$ ./.venv/bin/python -m pytest tests/test_web_database_capability_acceptance.py -q -p no:randomly
........................                                                 [100%]
26 passed, 1 warning in 2.24s

$ ./.venv/bin/python -m pytest tests/test_web_database_capability_acceptance.py -q   # 随机顺序
..........................                                               [100%]
26 passed, 1 warning in 2.24s
```

§6.4 点名的起点套件 + 本轮验收 + 相关隔离/恢复套件（同一进程、`-p no:randomly`）：

```text
$ ./.venv/bin/python -m pytest -q -p no:randomly \
    tests/test_web_database_capability_acceptance.py \
    tests/test_web_consumer_closure.py \
    tests/test_consumer_closure_acceptance.py \
    tests/test_capability_matrix.py \
    tests/test_personal_console_multi_tenant_authorization.py \
    tests/test_recovered_entry_acceptance.py \
    tests/test_tenancy_isolation_acceptance.py \
    tests/test_tenant_channel_isolation_acceptance.py \
    tests/test_scope_consistency_acceptance.py \
    tests/test_plan_3_1_joint_acceptance.py \
    tests/test_http_policy.py \
    tests/test_route_registry.py
184 passed, 1 warning in 113.29s (0:01:53)
```

全量回归（含本文件的交付工作树；`pytest tests/ -q -p no:randomly --ignore=tests/e2e`）于 2026-09-19
04:15 完成：

```text
30 failed, 5775 passed, 30 skipped, 423 subtests passed in 1480.21s (0:24:40)
```

30 项失败与合并前的同一集合逐条相同（`evidence/21` §F），故本文件新增的 26 条用例是
5749 → 5775 的 **+26** 的全部来源；验收本身不引入任何失败。

## 5. 未通过 / 未覆盖（不伪造）

1. **一键更新（10）**：3 条 `/api/update/*` 按 D9/`evidence/21` §E 的决策**不路由**；运行期实测 404。
   这是决策结果，不是遗漏，但按 §6.4 口径**不能**标为已通过。
2. **控制台前端（14）**：前端模块化与页面级验收移交 `adopt-upstream-web-frontend-split`；本 change 保持
   `keep-fork`，线上控制台在合并前后逐字节相同。
3. **Desktop（15）**：`desktop_tenant_context` 切片保持 `accepted=false`、路由关闭；8 条桌面消费接口
   未路由（其中 2 条 405）。真实打包客户端演练未执行，承接方 change 任务 0.6/6.5。
4. **个人渠道执行面（7 的一半）**：`PERSONAL_RUNTIME_ACCEPTED_TYPES` 与 `PUBLIC_PERSONAL_INGRESS_TYPES`
   仍为空集、总开关默认关闭；真实提供方往返未产生，配置已保存 ≠ 渠道已连接。
5. **真实模型答复 / 真实推理**：本轮验收走真实入口与真实授权，模型调用仍由既有桩测试覆盖；
   §6.4 的"正向业务动作"在对话一项只到 handler 与授权，不宣称端到端推理通过。
6. **语音（12）**：本轮未新增语音专项验收，沿用既有套件；不因本文件而升级其结论。

## 6. 结论

- 本轮合并**没有**引入新的未授权入口：注册表 0 条 `closed`，未收口端点如实 404/405。
- 平台平面（模型/配置/渠道/日志）与租户平面（记忆/调度/历史/技能/知识/智能体/会话/工作区/项目/渠道）
  在 `identity_mode=database`、双租户多用户下取得正向、隔离、可达三类证据。
- 依赖外部条件的能力（一键更新决策、控制台前端模块化、Desktop 真实客户端、个人渠道真实执行、
  真实模型推理）保持原状：不因本文件标为通过，也不因合并而放松。
- 因此同步报告 §0.6 的口径由"§6.4 一项都没有"更新为"Web/后端切片已取得三类证据；外部条件切片仍为未通过"，
  同步报告 §8 的"§6.4 双租户逐项验收"由**未执行**改为**已执行（部分通过）**。
