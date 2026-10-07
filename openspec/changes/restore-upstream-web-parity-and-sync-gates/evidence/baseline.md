# 2026-10-07 缺口基线与证据边界

这是创建提案前对当前代码的核查记录，不是修复完成报告。时间采用 Asia/Shanghai。

## 固定版本与实际装配

- rdai：`64858d585b427ec4697dc0fb5d2ccb5911d35769`。
- master：`48c0d79c36146950667f8d1884ab823deae62305`；核查时 `git ls-remote origin` 与本地远端引用一致。
- `git merge-base --is-ancestor origin/master rdai` 返回 0。最近纳入该源的 merge 为 `1f1c0d7f`（2026-10-02）。
- `163951b5`（2026-09-19）合并中的 `chat.html`、`console.js`、`console.css` 与其第一父提交对应 blob 完全相同；冲突记录为 keep-fork。
- 实际入口：`channel/web/fork/runtime.py` 调用 `build_web_app()`；后者使用 `_WEB_URLS`，由 `route_registry.py` 派生。上游 `URLS`/`build_app()` 是另一套装配。
- `template.render('chat.html')` 实际加载 `console.js`，未加载 `core/router.js`、`core/version.js`、`chat/scheduler-notify.js`、`core/notify.js`、`css/markdown.css`。合法参考模块未加载本身不是错误，缺少对应实际功能才是本表的问题。

## 七项缺口

| ID | 当前问题与触发 | 上游来源 | 当前实际落点 | 核查结果 / 后续必要验收 |
| --- | --- | --- | --- | --- |
| GAP-001 | 路径导航、刷新及页面历史未接通 | `c2efb0bb`、`d68644e5`，`core/router.js` | `chat.html`、`console.js` 导航、`route_registry.py` | 渲染页无 router；实际路由不匹配 `/agents`、`/settings`、`/scheduler`。需真实浏览器覆盖登录、页签、离页取消和兼容地址 |
| GAP-002 | 完成前 SSE error 调用未定义函数 | `chat/send.js` 与 `chat/scheduler-notify.js`；调用由 `1f1c0d7f` 移入 | `console.js:10036`（本 SHA） | 复用历史刷新测试夹具，执行真实 `startSSE` 并 emit error，得到 `ReferenceError: isSchedulerRequest is not defined`；stream closed=false，rendered error=0 |
| GAP-003 | 跨会话定时完成提醒未接通 | `082e1902`、`chat/scheduler-notify.js`、`core/notify.js` | 实际 `startPolling()` 只提交当前 `session_id` | 加载脚本无 `startSchedulerNotifyPolling` 定义或启动；运行记录接口已存在。未做真实双会话任务触发；不能把源码分析写成端到端通过 |
| GAP-004 | 距底部不足 80px 的上滚被抢回 | `b05ec4ac` | `console.js:7254`、`scrollChatToBottom` | 用真实事件逻辑和 DOM 式 scrollTop 钳位，500→490 后下一片段回到500，autoScrollEnabled=true。需整页真实滚动补验 |
| GAP-005 | 知识库空态与页签互斥失效 | `d081f65d` / PR #3172 | `loadKnowledgeView`、`switchKnowledgeTab` | 真实函数+轻量 DOM：空态与 docs/graph 同显；加载中切 graph 后返回非空列表，docsVisible=true、graphVisible=true。需 sources 与身份迟到补验 |
| GAP-006 | 长链接气泡溢出 | `b4c435e7`，`css/markdown.css` | 实际五份 CSS 中缺少对应普通消息换行规则 | headless Chrome 隔离页面：容器260px、内容1591px、wrap=normal；追加上游 markdown 样式后内容260px、wrap=anywhere。不是已登录整页验收 |
| GAP-007 | Web 一键更新菜单/API 未接入 | `core/version.js`、`api/update.py` | `fork/handlers/update.py` 仅 VersionHandler | 实际 HTML 无 update-menu；registry 不匹配三个 `/api/update/*`。属于已登记延期；未执行生产更新 |

提案创建时七项均为待实现/待验收，不因已经找到代码或测试片段而关闭。

## 复现方式与检查结果

1. Python 用 AST 读取实际 registry 的 RouteEntry，与上游 URLS 分别匹配路径；用 `core.template.render` 枚举实际 script/link。对实际加载脚本扫描 `isSchedulerRequest`：定义 0、调用 1。
2. Node 读取 `tests/test_session_history_refresh_frontend.cjs` 的 setup（首个 test 前）作为原有模拟环境，调用真实 `startSSE('audit-error', ...)` 后 emit `{type:'error', message:'synthetic model failure'}`；没有向生产服务发消息。
3. Node 从实际 `console.js` 提取滚动处理和知识库加载/切换函数，在可控 DOM/延迟 Promise 下驱动上述场景。后续应将场景纳入正式回归，而非把本记录当作测试工具。
4. 浏览器隔离验证加载实际五份 CSS（console、master-sync、appearance、coding、fork/sessions），以260px消息容器放入无空格链接，对比未加载的上游 markdown 规则；网络请求全部拦截，无真实账号/业务数据。
5. `.venv/bin/python -B -m pytest tests/test_web_console_assets.py tests/test_web_console_routing.py tests/test_web_console_update.py::test_frontend_contract -q -rs -p no:cacheprovider`：**25 skipped**（12 资源 + 12 路由 + 1 更新），不算通过。
6. `node --test tests/test_agent_modal_scroll.cjs tests/test_functional_models.cjs tests/test_session_history_refresh_frontend.cjs`：**29 项，23 passed / 6 failed**。失败集中在历史刷新夹具漏引入 `_patchHistoryCache` 及其连带结果；实际 console 定义了该函数，不能另算生产缺失。这与 GAP-002 实际加载集合中完全没有辅助函数的情况不同。

这些结果来自本次会话工具运行；没有另行保留完整日志或生产录屏。实施任务必须对实施候选复跑并保存可追溯结果。

## 已存在的后续接入与审计边界

已核对代码接入：资源 mtime/ETag、技能预览取消、回复中断提示、上下文用量/压缩、模型目录/回退链、定时任务创建/运行记录、Agent 弹窗滚动。模型与弹窗定向用例包含于上述通过项；其余只确认本次涉及入口存在，不宣称全部部署/提供方实测。

后续不能原样复制 9 月 19 日延期清单，把已补齐功能重复列为缺失。七项是高风险定向审计结果，不是 master 全量能力零遗漏证明。

## 本轮规划额外发现

- 现行 `console-route-lifecycle` 首条仍规定 hash 地址，须通过 delta 明确迁移，不能只修改代码与其冲突。
- `cli/update_service.py` 默认 release 源为 `zhayujie/CowAgent`，且 worker 说明没有备份步骤；fork Web 更新需单独适配来源、平台授权及恢复前提。
- 已有 `.gitea/workflows/production-readiness.yml` 对 main/rdai 配置触发；尚未验证远端 runner 和保护规则。GitHub 的 `test.yml` 不能代表实际 Gitea 已阻断。
- `doc/master合并到rdai分支规范.md` 被 `/doc/` 忽略，提案同时添加仅此文档的例外，其他本地资料继续忽略。

历史依据：`scripts/conflict-baseline.txt`；`openspec/changes/archive/2026-09-19-adopt-upstream-web-frontend-split/evidence/deferred-upstream-frontend.md`；`docs/design/master-sync-20261002/capability-map.md` 与 `validation.md`。它们解释此前决定，不代替本轮受测证据。
