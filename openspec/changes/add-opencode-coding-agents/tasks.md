## 1. 固定接入契约（后续任务的前置）

先读 `design.md`，按本文件顺序执行。根目录为 `/Users/jiantan/ai_assistant/rsmagent`；OpenCode 配套仓库为 `/Users/jiantan/ai_assistant/rsmcode/opencode`。仅新增共享服务接入，不扩展设计中的非目标。

- [x] 1.1 对已运行的开发服务或独立验证实例验证：V2 固定 ID 创建两次返回同一会话、读取、活跃状态；兼容接口重命名、interrupt 后删除；确认同一会话在 Web 中可打开。记录版本、实际请求/响应形状和结果至本 change 的 `evidence.md`，不得记录密码。禁止为验证重启用户现有服务。
- [x] 1.2 验证浏览器从平台 origin 嵌入 OpenCode 根路径：实际认证可完成、目标会话可加载、SSE 输出可见、终端 WebSocket 可交互；记录响应头和部署地址角色。确认使用本地 app 构建，不是远程官方 UI。此任务未通过时先修正部署/接口适配记录，不能把模拟成功作为前置通过。
- [x] 1.3 确认当前 `ChatHandler` 返回的真实页面和脚本列表、`get_conversation_store()` 的全局存储与 Agent 维度；记录到 `evidence.md`。保留当前未提交改动；本地 OpenCode 的 `bun.lock` 已有改动，不将其顺手提交或覆盖。

验收：真实 API 与浏览器请求能够操作同一会话。前置只验证连接能力，不建设新部署平台；项目、接口、认证条件已有明确可复现结果后进入第 2 组。

## 2. 类型、配置及入口边界

涉及：`agent/registry.py`、`agent/admin.py`、`config.py`、`config-template.json`、`channel/web/fork/handlers/agents.py`、`channel/web/fork/common.py`、`bridge/agent_initializer.py`、`bridge/agent_bridge.py`、`channel/web/api/agents.py` 的 scheduler 重载，以及已有默认/团队/渠道设置入口。

- [x] 2.1 扩展 `tests/test_agent_registry.py`、`tests/test_agent_admin.py`：覆盖旧配置缺省 normal、coding 项目必填、类型不可变、复制保留新字段但不复制会话，以及已有非空远端项目不触发本地初始化；先确认新增行为断言失败。
- [x] 2.2 实现 `agent_type`、`coding_project_dir` 的解析、序列化、创建/更新/复制和白名单投影；配置只增加 `design.md` 定义的 `opencode` 对象，缺省关闭、密码读取环境变量。沿用原状态工作区，不能把代码项目塞进 `workspace`。
- [x] 2.3 复用既有读/用授权，新增稳定错误 `coding_disabled`、`coding_web_only`、`coding_service_changed`。普通初始化明确拒绝 coding，scheduler 初始化和重载跳过；默认选择/设置、个人助理模板、团队、委派、渠道/任务候选及其后端提交校验限定 normal，保留显式 Web coding 入口。
- [x] 2.4 在 `tests/test_agent_initializer_routing.py`、新增 `tests/test_coding_agent_routes.py` 覆盖直接伪造普通调用、无使用授权、关闭开关和 coding 通用默认拒绝；运行本组测试，确认普通创建/默认逻辑原断言仍通过。

交付接口：智能体投影包含 `agent_type`；管理投影包含 `coding_project_dir`；读取旧档案无写入副作用。不开启 OpenCode 也能验证普通行为兼容。

## 3. 轻量会话适配与存储

新增：`agent/coding/__init__.py`、`agent/coding/opencode.py`、`agent/coding/sessions.py`、`channel/web/fork/handlers/coding.py`。
修改：`agent/memory/conversation_schema.py`、`agent/memory/conversation_store.py`、`channel/web/route_registry.py`、`channel/web/web_channel.py`、`channel/web/fork/handlers/sessions.py`、`channel/web/fork/runtime.py`。

- [x] 3.1 新增 `tests/test_opencode_client.py`，按第 1 组固定的响应测试 HTTP 方法、路径、认证头、超时及 401/403/404/5xx 分类；实现具体 OpenCode 客户端方法 `create_session(id, project_dir)`、`get_session(id)`、`active_sessions()`、`rename_session(id, title, project_dir)`、`interrupt_session(id)`、`delete_session(id, project_dir)`。不增加 provider 基类或消息流转换。
- [x] 3.2 新增 `tests/test_coding_session_store.py`，使用临时真实 SQLite 验证一张关联表、原子缓存更新、唯一外部关联、owner 过滤及无消息记录仍可列举；通过现有 schema/store 接缝添加 `opencode_session_links` 及小范围存取方法，不另建数据库、不重写 sessions 主键。
- [x] 3.3 实现 `design.md` 的预留/创建/重试：已验证身份参与稳定 ID 生成，保留 request_id 和 creating/ready；并发重复请求命中同一关联，项目改变时拒绝复用未完成请求。测试远端已创建但响应丢失后重试、平台重启后恢复、不同主体相同 request_id 不串会话。
- [x] 3.4 实现 create/open/attach/sync 四个接口并登记路由与全部方法策略；复用请求上下文、目标分配、history 和 owner 授权。attach 验证 source_session_id 归属、远端存在、真实目录、根会话及已有归属。新增接口绝不接收凭据、任意上游 URL 或客户端 owner。
- [x] 3.5 实现每批 50 条稳定游标刷新、最多 4 个远端读取在途及活跃状态读取；返回 changed/removed/unavailable/next_cursor。新增 `tests/test_coding_session_sync.py` 覆盖标题时间更新、超过 50 条全部可达、ready 记录明确 404 清理、creating 记录不因查询 404 被删除、网络故障保留、服务标识改变，以及刷新迟到不能复活删除项或覆盖新标题。
- [x] 3.6 既有列表复用 sessions 缓存并投影 coding 类型；重命名和删除按关联分流到远端，成功后原子更新/删除缓存。置顶和归档保持平台本地语义，普通清上下文/删除消息等不适用于 coding 的入口明确关闭。测试远端失败保留记录、运行中删除先 interrupt、重复删除幂等、项目文件不被删除；Agent 删除沿用会话占用阻断。
- [x] 3.7 运行本组新增测试及路由/schema 回归；确认不存在全量导入共享历史、消息镜像、后台队列或第二套会话列表。以真实 OpenCode 再执行一次平台 create→rename→delete 闭环后进入前端组。

平台 HTTP 请求/响应及上游方法以 `design.md` 第 4 节为唯一契约。可直接用于接口验收的示例请求：

```json
{"agent_id":"erp-coder","request_id":"b347bcfb-48d9-4cf2-8849-ec49ab29a144"}
```

同一已登录主体重复 POST 此请求，返回相同 `session_id`；更换主体不能复用该关联。

## 4. OpenCode 嵌入与平台界面

OpenCode 修改范围：`packages/app/src/app.tsx`、`src/entry.tsx`、实际新/旧布局组件，新增 `src/context/rsm-embed.tsx`；会话创建/分叉导航处只接通知钩子。
平台修改范围：新增 `channel/web/static/js/coding.js` 和局部 CSS/i18n，`channel/web/chat.html`、实际装载的 `static/js/console.js` 最小接缝、`channel/web/fork/handlers/pages.py` 缓存版本处理。

- [x] 4.1 在 OpenCode 实现只对 `rsm_embed=1` 生效的上下文，固定当前服务、保留目录/会话路由，隐藏重复导航及全局设置；保留输入、工具确认、文件、差异、终端和分叉。补 `src/context/rsm-embed.test.ts` 验证嵌入上下文跨内部导航保留及普通模式不改变。
- [x] 4.2 实现 ready/session 两种通知，覆盖初始加载、内部新建、分叉和导航；父页校验 origin、source、channel 后调用 attach，失败明确反馈，不修改原归属。新通知重复投递不得重复登记；普通 OpenCode 独立访问不需要父窗口。
- [x] 4.3 平台新增唯一 `window.CodingChat` 模块，提供 `launch/open/refresh/leave`；在实际生效的智能体创建/详情加入类型、项目及默认服务展示，在工作台/新对话/历史打开按类型调用。coding 隐藏普通输入、模型/权限模式/团队等无效控制；返回 normal 完整恢复，前端选择切换遵守原离页确认。
- [x] 4.4 实现 `setTimeout` 5000ms 刷新、可见性暂停/恢复、在途防重、稳定游标逐批处理、身份/页面世代号和管理成功后的即时刷新。历史页走已有列表重绘；临时不可达保留缓存；未收到 ready 时显示加载，15 秒后提供重试，重试仅打开同一会话。
- [x] 4.5 新增 `tests/test_coding_frontend.cjs`，用现有 Node 测试方式覆盖普通/coding 切换、重复点击、隐藏停表、恢复单次刷新、迟到响应、错误消息来源、iframe 新会话通知，以及服务失败后恢复；核对新 JS/CSS 确实被当前 ChatHandler 输出加载，不能仅测试未服务的拆分文件。
- [x] 4.6 嵌入模式补齐外壳界面：隐藏 OpenCode 标题栏（渠道徽标与会话标签）与只由该徽标开关的开发诊断条，避免 iframe 内出现第二套会话导航与开发工具，独立访问行为不变；补 `rsm-embed.test.ts` 的 `titlebar` / `debug-tools` 断言。

验收：点击平台 coding 卡片后在原对话区域使用 OpenCode，刷新/切换/分叉后的当前会话和统一历史一致；没有新增通用前端通信总线。

## 5. 联调、迁移与交付

- [x] 5.1 执行下方聚焦测试命令。修复本次引入的失败；记录已有失败及原因，不删除普通行为或权限断言来让测试通过。
- [x] 5.2 浏览器真实验收：创建 coding→分配给用户→新建并发送消息→观察工具确认/代码差异/终端→自动标题同步→内部分叉同步→返回历史再打开→平台重命名→OpenCode 删除后列表收敛→运行中平台删除；普通智能体并行对话保持正常。记录实际结果和必要截图。结论与保留说明见 `evidence.md` 第 5.2 节（面板内敲字受跨 origin iframe 限制，仍以同一地址顶层页签验证）。
- [x] 5.3 验证权限与故障：无分配用户平台接口拒绝；撤权后下一请求拒绝；OpenCode 失联保留历史、恢复后刷新；创建响应丢失重试不重复；两种智能体共存、跨页超过 50 条、重新登录及平台重启后关联正确。外部共享边界按本 change 验收，不追加容器隔离工作。
- [x] 5.4 新增 `docs/opencode-coding-agents.md`，给出全局配置、远端项目含义、配套 Web 根路径部署、现有浏览器认证流程、共享服务边界和升级/回滚步骤。分别验证旧数据无类型、关联表重复初始化、enabled=false、重新启用；不修改旧消息、不删除远端项目。
- [x] 5.5 运行 `openspec validate add-opencode-coding-agents --strict`，逐条对照本 change specs，核对只改相关文件；分别记录两仓库实现版本和验收证据。只有实际实现及验证完成的任务才能勾选，文档已齐全不等于功能已交付。

实现版本（验收时）：

```
rsmagent          e37df20c（分支 rdai）      —— 本 change 的改动 + 既有未提交改动
rsmcode/opencode  5a8335857b（分支 dev）     —— packages/app 版本 1.18.31，含 rsm-embed 定制
openspec validate add-opencode-coding-agents --strict  -> Change 'add-opencode-coding-agents' is valid
```


聚焦测试（实现后执行，当前文档生成阶段未执行这些功能测试）：

```bash
# 在 rsmagent 根目录、使用项目现有 Python 环境
python -m pytest -q tests/test_agent_registry.py tests/test_agent_admin.py tests/test_agent_initializer_routing.py tests/test_coding_agent_routes.py
python -m pytest -q tests/test_opencode_client.py tests/test_coding_session_store.py tests/test_coding_session_sync.py tests/test_route_registry.py tests/test_conversation_schema_seam.py tests/test_history_agent_workspace.py
node --test tests/test_coding_frontend.cjs tests/test_agent_workbench_frontend.cjs tests/test_user_default_agent_frontend.cjs

# 在 /Users/jiantan/ai_assistant/rsmcode/opencode/packages/app
bun test --conditions=solid --preload ./happydom.ts ./src/context/rsm-embed.test.ts
bun typecheck
bun run build
```

每组完成后检查其变更和验证结果，再进入下一组。预计 5～7 人日，以第 1 组实测为准；不需要为本 change 拆出额外平台项目。
