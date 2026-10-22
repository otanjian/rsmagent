# 实施记录（add-im-guest-accounts）

- 日期：2026-10-21（Asia/Shanghai）。
- 基线：分支 `rdai`，提交 `e16fa750`；`identity.db` 迁移版本到 50，本次新增 51。
- 运行环境：`.venv-win\Scripts\python.exe`（Python 3.12.13），Windows PowerShell；无真实飞书 / 企业微信测试租户。
- 结论：任务 1.1–1.5、2.1–2.4、3.1、3.2、3.4、4.1、5.1 已实施并通过对应测试；4.2（页面与服务端一致性的人工验收）、4.3/4.4（真实渠道）、5.2（演练）、5.3（真实渠道证据汇总）未完成，保持未勾选。

## 数据与代码落点

| 层 | 文件 | 内容 |
| --- | --- | --- |
| 迁移 | `auth/store.py` | 迁移 51：`users.account_type`（默认 `normal`）、`tenant_channel_instances.unbound_user_policy / guest_role_id / guest_group_ids_json`、`external_identities.active`（默认 1） |
| 身份 | `auth/service.py` | 账号类型与登录/会话/改密/重置门槛；映射三态解析；访客原子开户；解绑改为停用、绑定同账号可恢复；访客运行范围与角色交集；渠道三字段校验 |
| 渠道 | `channel/external_identity.py`、`channel/chat_channel.py` | guest 模式门禁、群 ID 名单、跳过首发认领、上下文携带 `im_instance_id`/`external_identity_id`、投递前重验 |
| 授权 | `auth/service.py::check_resource_action` | 访客调用 = 成员授权 ∩ 渠道访客角色（含 `all` 旁路与私有 owner 短路） |
| 运行时 | `common/runtime_identity.py`、`auth/runtime.py` | `RuntimeIdentity` / `RequestContext` 增加可信 IM 来源字段 |
| 工具 | `agent/protocol/agent_stream.py` | 访客禁止 `scheduler` 与 `builtin:bash/shell/python/terminal` |
| 接口 | `channel/web/admin_handlers.py` | 渠道创建/编辑透传三个访客字段 |
| 界面 | `channel/web/static/js/{console,channel-workbench,identity-admin}.js` + `i18n/*` | 渠道卡片“访客访问”三项配置；成员页 IM 访客标识；外部绑定弹窗停用状态与恢复；简繁英文案 |
| 文档 | `docs/im-guest-access.md` | 管理员使用说明（配置位置、默认关闭、停用范围、解绑恢复、角色管理、本期边界、上线顺序） |
| 测试 | `tests/test_im_guest_accounts.py` | 21 个用例 |

## 已执行命令与结果

| 命令 | 结果 |
| --- | --- |
| `pytest tests/test_im_guest_accounts.py -q` | 21 passed |
| `pytest tests/test_im_guest_accounts.py tests/test_external_im_gate.py tests/test_external_identity_binding.py tests/test_tenant_channel_inbound_isolation.py tests/test_channel_instances_partition.py tests/test_external_channel_propagation.py -q` | 106 passed |
| `pytest tests/test_personal_channel_binding.py tests/test_channel_type_admissibility.py -q` | 47 passed |
| `node --check`（6 个改动 JS 文件） | 全部通过 |
| `openspec validate add-im-guest-accounts --strict` | 通过（`Change ... is valid`，exit 0；修正了一条 ADDED requirement 超过 500 字的 lint） |
| `scripts.check_change_deltas.check_deltas(Path('openspec'), 'add-im-guest-accounts')` | `[]`（无增量问题） |
| `pytest tests/test_web_console_assets.py tests/test_web_console_routing.py tests/test_upstream_drift_guards.py -q` | 28 passed、3 failed。3 个失败均为既有环境问题：`test_web_console_routing.py` 用系统 locale（本机 GBK）读取 UTF-8 的 `console.js`，`git show HEAD:` 的原始字节同样无法按 GBK 解码（0x94 非法），与本次改动无关 |
| `python scripts/check_change_deltas.py add-im-guest-accounts` | exit 1：`seam:conversation-store`、`seam:scheduler`（`agent/memory/conversation_store.py`、`agent/tools/scheduler/integration.py`、`tests/test_scheduler_web_update.py`）未在本 change 点名。属仓库级上游同步清单要求，与本 change 无关，原样记录、未改清单 |

## 覆盖的能力点（对应 spec 场景）

- 同一三元组重复/改名复用同一 `user_id`；昵称不参与身份；不同 issuer 分开。
- 首条消息原子建号：账号、成员、渠道角色、映射、审计同事务；审计写失败整体回滚。
- 并发首条消息只产生一个访客（2 线程实测）。
- 访客登录、手工插入会话、改密、平台重置一律拒绝；账号类型不可被重置或角色操作改写。
- 映射三态：未知→可开户；映射停用/账号停用→拒绝且不重新开户；存储故障按异常处理（沿用 `find_user_for_external_identity` 既有异常语义）。
- 停用映射不能绑定到别的账号；同账号重新绑定即恢复原映射（不影响账号/成员/角色停用状态）。
- 群消息必须命中实例 `guest_group_ids`（空=仅私聊），全局 `ALL_GROUP` 不参与该判断；未命中不建号。
- 访客调用取成员与渠道角色交集：给成员追加工具角色后，带 IM 来源的调用仍被拒绝；角色撤权后下一条调用立即拒绝。
- 成员停用作用于本租户全部机器人；停用唯一成员不会被“最后租户”规则误拦；重新开启渠道/重新启用成员按管理员状态生效。
- 访客不能被设为租户管理员或平台管理员。
- 渠道三字段：默认 `require_binding`、个人实例拒绝 `auto_guest`、通配群 ID 拒绝、角色必须含 `chat.use` 且不含管理能力、更新受 `expected_version` 保护、省略字段保留原值。
- 投递前重验：映射停用或渠道关闭后，已生成的回复不再发送。
- 访客不能创建定时任务、不能执行本机代码（工具门禁）。

## 对本地运行服务的检验（2026-10-21 13:5x）

服务：`python app.py`，PID 23872，启动于 13:40:43，监听 `127.0.0.1:9899`。

| 检查 | 结果 |
| --- | --- |
| `GET /` 控制台外壳 | 200 |
| `GET /assets/js/console.js` 含 `tenant-channel-guest-policy` / `tenantGuestSection` | 通过 |
| `GET /assets/js/i18n/tenant-channel.js` 含访客文案 | 通过 |
| `GET /assets/js/identity-admin.js` 含 `member_guest_badge` / `extidRestore` | 通过 |
| 未认证 `GET /api/tenant/channels` | 400（路由存在且受租户门禁，非 404） |
| 临时会话 + `X-Tenant-ID` 调 `GET /api/tenant/channels` | 200，两条实例均带 `unbound_user_policy` / `guest_role_id` / `guest_group_ids` |
| 同一会话调 `GET /api/tenant/members` | 200，4 行成员均带 `account_type` |
| 实时 `identity.db` 迁移 | 版本 51；`users.account_type`、`external_identities.active`、渠道三列存在；`im_guest` 账号 0 个 |
| 行为验证（真实代码 + 实时库的 sqlite 备份副本） | 28/28 通过：开启策略、首条开户、重复复用、登录拒绝、群范围拒绝、解绑停用不复活、恢复原映射、投递前重验、Agent 授权交集 |

检验脚本：`tmp/verify_service.py`（部署 + 行为，28 项）、`tmp/verify_http_api.py`（真实 HTTP 接口，会话行用后即删）。

**检验发现的两个环境问题（不是代码缺陷）：**

1. 运行进程启动于 13:40:43，而 `auth/service.py` 在 13:50:55 又补了“启用时校验访客角色对目标 Agent 的授权”。因此运行中的服务缺少这一条更严格的校验，其余后端改动均早于启动时间，已在进程内。
2. AI租户1（test15）当前没有任何 `agent_bindings`，两条渠道实例的 `agent_id` 为空且租户无默认 Agent。即使开启访客，入站也会在身份解析前以“无可用智能体”拒绝；真实端到端使用前必须先给实例绑定 Agent。行为验证在副本中绑定了 `verify-agent` 以覆盖该路径。

## 真实飞书链路验证（2026-10-21 17:18，本地 9899）

前置：前两次一键创建的应用（`cli_aa52cfb928b91c15`、`cli_aa52c51b78381c09`）未发布/未启用，`tenant_access_token` 一律返回 21001；用系统保存的那组凭据直接请求飞书接口复现了同样的 21001，确认拒绝发生在飞书侧而非发送代码。

换用发布并启用的应用 `cli_aa523f094e785c14` 后：

| 时间 | 结果 |
| --- | --- |
| 17:18:25 | `Bot open_id fetched: ou_6e8758...`（token 获取成功） |
| 17:18:32 | 收到真实私聊，自动开户 `usr_i4wDfJJ_2lRA0bdS`：`account_type=im_guest`、角色 `im_guest`、映射 `(feishu, cli_aa523f094e785c14, ou_d26cac...)` active=1、审计 `im_guest.provision` |
| 17:18:34 | 回复卡片创建并发送成功（`message_id=om_x100b62bc786fc4accea65efa11f3abc`） |
| 17:18 之后 | 日志无 ERROR/Traceback |

同时验证了此前修复的模型授权：访客收到的是模型正常生成的回答，不再出现 “No model is currently authorized”。

仍待覆盖的 4.3 场景：重复私聊、改名、群 @、文件回复、停用后拒绝。此前两次失败的应用各自留下一个访客账号（共 3 个），身份按 `(provider, issuer, subject)` 区分，属预期；不再使用的可在成员页停用。

## 未验证 / 未执行

- 真实飞书、企业微信测试实例的首发/重投、同名改名、群 @、文件回复与停用验收（4.3、4.4）：本环境没有可用的真实租户与凭据。
- 页面与服务端一致性的浏览器人工验收、旧客户端草稿保留的交互验收（4.2）：仅做了服务端“省略字段保留”和版本冲突的自动化验证，未做浏览器实测。
- `require_binding → auto_guest → 关闭 → 恢复` 的完整演练与兼容构建回退、一致备份恢复（5.2）：只做了策略切换的接口级验证，未做环境演练。
- 私聊附件与记忆隔离的专项验证（3.3）：依赖既有的租户/用户归属回归；本 change 未新增隔离逻辑，也未新增该维度测试。
- 全仓 pytest：一次 121 个文件的回归在 36 分钟内未完成，已终止，改为上述定向测试集。
- 未提交、未推送、未归档。
