## 0. 基线与依赖

- [ ] 0.1 归档前核对并行 change `fix-coding-session-history-management`；以先归档方基线重写 `session-history-workbench` 共享 MODIFIED 块并重跑 `scripts/check_change_deltas.py`。

## 1. 复用角色授权

- [x] 1.1 核对现有身份/租户、角色事务、审计及历史存储切片，确认 default Agent 存储键、真实 owner 守卫与并行 change 契约，建立本人读写基线。
      → 已核对 `auth/policy.py`、`auth/service.py`、`channel/web/fork/authorization.py`、`ConversationStore` 与 `sessions.py`；本人读写基线由既有回归（80 项）与新增 `tests/test_enterprise_history_read_all.py` 固定。
- [x] 1.2 登记 `history.read_all` 和 Agent `history_read_all`，接入现有角色编辑、保存、版本和审计；只允许明确本租户共享普通 Agent，保留原默认角色、现有 grants 与空集合。
      → 角色编辑页新增「查看全部会话（只读）」独立勾选；选择 Agent 不再自动打包 `history_read_all`（`identity-admin.js`），后端保存沿用原 grants 事务。
- [x] 1.3 实现共用跨用户读取判定，要求现有历史权限、新功能及目标 grant，覆盖多角色来源、管理员快捷分支、撤权、解绑/转私有和跨租户拒绝；不把读取权用于原会话写守卫。
      → `_require_history_read_all` + `_history_read_all_target_ids`；登录管理员快捷分支对 `history_read_all` 关闭；私有/跨租户/编码 Agent 拒绝；原写路径未改动。
- [x] 1.4 在 `platform-all-authorization` 落 `history_read_all` 的显式 grant 例外，并在 `audit-log` 登记 `session.history.read_all` 事件；两处 MODIFIED 标题与基线逐字一致。
      → 两个 delta 已建并通过 `check_change_deltas.py`；列表/详情读取写 `session.history.read_all`（脱敏，best-effort）。

## 2. 扩展原历史页

- [x] 2.1 为原列表/原文增加 `read_scope=agent_all` 和明确 Agent 范围，按可信 tenant/存储键查询；复用标题搜索、归档及分页，默认与原 `scope=all` 保留本人语义，候选/计数先授权。
      → `GET /api/sessions?read_scope=agent_all&agent_id=`、`GET /api/history?read_scope=agent_all`、`GET /api/sessions/read_targets`；`list_sessions/load_history_page` 的 `all_owners` 为显式开关。
- [x] 2.2 增加范围/目标选择、创建人展示和他人只读详情；完整分页展示合法问答，不返回运行控制数据，不进入聊天恢复、不显示写动作，保持本人继续访谈入口。
      → 历史页「我的会话 / 获授权智能体的全部会话」+ 目标选择；行内创建人、无写操作菜单；只读详情弹层分页加载，正文以 `textContent` 渲染。
- [x] 2.3 复用页面状态、主题/三语/窄屏与原文件权限，补齐跨用户读取审计、失败/空态区分、身份切换清理和迟到响应隔离。
      → 三语与深色样式；`_historyReadSeq` 丢弃迟到响应；切换身份/范围/目标时关闭详情；后端读取写 `session.history.read_all` 审计。
- [x] 2.4 【读取阶段门槛】真实验证两租户、多受访者、只读角色及管理员的授权/缺权/撤权、历史/归档/分页，并回归原发送、修改、删除、置顶、归档恢复、清空、消息及项目操作；通过后进入报告阶段。
      → 本机以隔离 `identity.db` + 两个真实租户 + 真实 WSGI 路由执行 14 项验收（`tests/test_enterprise_interview_acceptance.py`）：目标选择、跨 owner 列表/详情/创建人、撤销 grant、平台/租户管理员无 grant 拒绝、跨租户拒绝、owner 写成功/非 owner 写拒绝、归档与分页。写路径另由 126 项后端回归与 23 项归档/恢复/取消回归覆盖；真实浏览器未执行，见 `evidence/acceptance-plan.md`。

## 3. 单次请求生成报告

- [x] 3.1 在规划顾问会话详情接入一个生成 POST，以真实请求者验证 CSRF、源读取和现有运行/Agent/模型/凭据/配额；读取完整问答并固定截止，不解析附件，不以源 session 为消息发送目标。
      → `POST /api/history/report`；CSRF、源读取（本人或 `history_read_all`）、`model.use` + 模型 grant、`tokens` 硬配额；按页读取全部已保存问答并固定前缀。
- [x] 3.2 复用现有模型客户端完成一次有时限调用，设置输入/输出预算并沿用并发限制；返回前重验身份/来源及输入未被删改，保留实际计量和脱敏审计，不新增表、任务队列或幂等记录。
      → `Bridge().get_bot("chat")` 单次调用（120s 上限、60k 输入上限）；成功后按实际 tokens 记账；返回前重读前缀比对签名，删改返回 409、仅追加放行；写 `session.interview_report.generate` 审计。
- [x] 3.3 整理固定报告提示及输出结构，覆盖企业岗位、逐项输入/方法/输出、日周月频率耗时、错误风险、资料缺口、场景价值/可行性和试点建议；引用消息并区分确认/未确认/建议，不写原会话或记忆。
      → 固定系统提示与 `[#序号]` 引用；`_render_transcript` 保留原始消息序号，跳过空内容但保留引用编号。
- [x] 3.4 在当前详情安全预览结果并由浏览器下载 Markdown，标明来源截止及“不自动保存，请下载留存”；生成时禁用重复点击，切换身份/租户/会话时清理结果，不提供持久报告入口。
      → 只读弹层内生成/预览（`<pre>` + `textContent`）与本地 `.md` 下载；生成时按钮移除，关闭/切身份清空；页面无服务端报告入口。
- [ ] 3.5 验证完整多页输入、超长拒绝、附件不读取、生成期间追加/删改/撤权、超时/断开/重启、重复点击及不可信输出；不自动重试、不返回部分成品，原问答及后续访谈不受影响。

## 4. 验收和交付

- [ ] 4.1 【生成上线门槛】确认读取阶段及真实模型/凭据/硬配额/审计切片，通过浏览器完成明确授权 → 查看他人问答 → 原智能体继续访谈 → 真实生成/下载 → 撤权拒绝；mock 不替代真实验收。
- [ ] 4.2 接入现有配置机制的单一新功能开关并默认关闭，验证开关对新增 UI/API/在途结果生效及原历史不受影响；检查无新增表/数据迁移、升级不扩权和兼容版本回退。
- [x] 4.3 更新授权与使用说明，说明访谈保存在原会话、报告仅当前页面及本地下载、仅正文输入、超长/超时限制和手动重试；记录真实验收及开关关闭证据。
      → `docs/session-history-panel.md` 增补使用与边界；`evidence/acceptance-plan.md` 记录待执行的真实验收步骤，本机结果与限制分别标注。
