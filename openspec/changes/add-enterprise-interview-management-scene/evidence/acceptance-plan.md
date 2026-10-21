# 读取阶段与报告上线真实验收计划

状态：**待执行**（本机没有目标租户、真实受访者账号、可用模型凭据与配套浏览器会话；单元/回归已由本地测试覆盖，不能替代本计划）。

对应任务：2.4（读取阶段门槛）、4.1（生成上线门槛）、4.2（开关与回退）、3.5（失败矩阵中的真实链路部分）。

## 环境与前置

- 两个租户（A、B），A 内至少 2 名受访者（alice、bob）和 1 名只读管理者（reader）。
- reader 的角色同时具备 `history.read`、`history.read_all`，并在角色编辑中只对 `enterprise-planning-advisor` 勾选「查看全部会话（只读）」。
- `enterprise-planning-advisor` 已绑定租户 A 且为共享普通 Agent（`private_owner_user_id` 为空）。
- 开关 `enterprise_interview_management_enabled` 默认关闭；先在关闭状态完成第 1 组。

## 1. 开关关闭（4.2）

1. 以 reader 登录，打开历史页：不出现范围/目标选择器；`GET /api/sessions/read_targets` 返回 403（`feature_disabled`）。
2. 直接请求 `GET /api/sessions?read_scope=agent_all&agent_id=enterprise-planning-advisor` 与 `POST /api/history/report`：均被拒绝。
3. 原本人历史、「我的会话」范围、原智能体访谈与原写操作均正常；角色授权未被自动修改。

预期：新增入口不可见，直接请求拒绝，原历史无差异。

## 2. 读取授权（2.4）

1. 打开开关，重新登录。reader 看到范围切换与目标选择，目标只有 `enterprise-planning-advisor`。
2. 「获授权智能体的全部会话」：能看到 alice、bob 的会话，行内创建人可辨认；标题搜索、归档筛选、分页覆盖全部已保存记录（含进行中与已归档）。
3. 打开他人会话：只读弹层展示完整问答，可分页；没有输入框、置顶/重命名/归档/删除入口；不能恢复为当前会话。
4. 撤掉 reader 的 `history_read_all`（或 `history.read_all`）后刷新：列表回到本人范围，旧链接/已打开的只读详情不再扩大；直接替换 Agent/会话 ID 仍被拒绝。
5. 以租户管理员、平台管理员分别验证：没有目标 grant 时不能跨用户读取；对私有 Agent、另一租户 Agent、编码 Agent 直接请求被拒绝。
6. 回归原写操作：发送、重命名、删除、置顶、归档/恢复、清空上下文、消息删除、项目操作均与改动前一致；他人会话的写请求按原 owner 拒绝。
7. 浏览器网络面板确认每次读取有 `session.history.read_all` 审计（可在审计页按动作筛选）。

## 3. 报告生成（4.1 / 3.5）

1. reader 打开 alice 的访谈 →「生成调研报告」：按钮进入生成态且不能重复点击；成功后展示 Markdown，含来源截止与「不自动保存，请下载留存」。
2. 下载 `.md`，刷新页面：本地文件保留，页面没有服务端报告可恢复；问答仍按授权可查。
3. 进行中的访谈：返回阶段报告，原会话没有新增消息；受访者继续访谈后重新生成，新报告包含新增问答。
4. 只读管理者缺 `model.use` 或模型 grant：可看问答，生成入口被拒绝；缺配额时返回 `quota_exhausted`。
5. 超长会话：返回 `input_too_long`，不产生截断报告；历史页仍可分页查看完整问答。
6. 生成期间删改源问答或撤权：返回 `source_changed` / 权限拒绝，不返回结果；仅追加问答时允许按原截止返回。
7. 断开/超时/服务重启：页面回到可重试状态，没有待恢复任务，不自动重试；用量按实际发生计量。
8. 多标签页同时生成：允许各自消耗额度（一次请求方案的既定取舍），不出现「恰好一次」承诺。
9. 报告内容抽查：岗位、逐项输入/方法/输出、频率耗时、错误与复核、资料缺口、场景建议齐全；引用 `[#序号]`；未确认与建议分开；不含附件原文或编造 ROI。

## 4. 回退

1. 关闭开关：新增 UI/API 立即不可用；在途请求不返回新功能结果；原本人历史、访谈与角色授权不变。
2. 用旧版本客户端访问：未知权限/动作被安全拒绝，不扩权回退。

## 本机已覆盖（不等同于上述验收）

- `pytest tests/test_enterprise_history_read_all.py tests/test_interview_report.py`
- `node --test tests/test_history_read_scope_frontend.cjs tests/test_session_history_frontend.cjs ...`
- `openspec validate add-enterprise-interview-management-scene --strict`
- `scripts/check_change_deltas.py add-enterprise-interview-management-scene`


## 本机执行记录（2026-10-21）

执行方式：临时数据目录 + 独立 `identity.db` + 两个真实租户/用户 + 真实 WSGI 路由与 Cookie 会话（`build_web_app().request`）。模型调用使用本地替身（不消耗真实模型额度），因此**不能替代真实模型验收**；读取阶段与请求编排是真实代码路径。

```
pytest tests/test_enterprise_interview_acceptance.py -q -p no:randomly   → 14 passed
pytest tests/test_enterprise_history_read_all.py tests/test_interview_report.py        tests/test_identity_policy.py tests/test_identity_resource_authorization.py        tests/test_session_history_shared.py tests/test_session_idor_closure.py        tests/test_route_registry.py tests/test_http_policy.py             → 126 passed
node --test tests/test_history_read_scope_frontend.cjs ...               → 119 passed
```

已由本机端到端覆盖：

- 两租户/多受访者：reader 只能选到显式授权的 `enterprise-planning-advisor`；列表返回 alice/bob 两条会话并显示创建人；只读详情返回完整问答、无 `active_request`。
- 缺权/撤权：删除 `history_read_all` grant 后下一请求被拒；平台管理员与 tenant_admin 无目标 grant 时列表被拒、目标选择为空；foreign Agent 被拒。
- 读写分离：非 owner 的改名请求被拒（403/404），owner 本人改名成功。
- 归档/分页：`archived=1` 只返回归档记录，`page_size=1` 正确给出 `has_more`。
- 报告：一次 POST 生成并返回 Markdown，提示词含完整问答与 `[#序号]`；写 `session.interview_report.generate` 审计；超长（>60k）返回 413 且不调用模型；模型超时返回 504；生成期间删改源问答返回 409 `source_changed`。
- 开关：关闭后 `/api/sessions/read_targets` 与 `/api/history/report` 返回 403 `feature_disabled`。
- 多页输入：500 条问答（3 个 200 条分页）完整进入提示词，`[#500]` 存在。

仍未执行（需真实环境/真实模型/浏览器）：

- 真实模型输出质量、真实凭据与硬配额计数（4.1）。
- 开关关闭时的**在途请求**行为、关闭后浏览器页面状态（4.2）。
- 真实浏览器下的三语/窄屏/主题与下载交互（2.4/4.1 的 UI 部分）。
- 服务重启/断连下的失败体验（3.5 的剩余部分）。
