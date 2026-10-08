## Why

`master` 的提交已进入 `rdai` 历史，但部分控制台功能仍停留在未加载的上游模块，另有调用方移植后遗漏依赖的运行错误。2026-10-07 核查确认页面路径路由及六项交互/入口缺口；现有验收还存在整组跳过和测试夹具漂移，需要同时补齐实际功能与可阻断此类遗漏的同步检查。

## What Changes

- 按固定 `master=48c0d79c36146950667f8d1884ab823deae62305`、`rdai=64858d585b427ec4697dc0fb5d2ccb5911d35769` 建立七项缺口基线。本轮源已是目标祖先，后续实施采用普通修复提交，不制造空 merge；实施前重新核对目标变化。
- 补齐页面路径与页签地址、直接访问、刷新、前进后退及登录恢复；将工作台/管理区归属纳入既有视图登记，保留权限、离页保护、旧 `/chat` 和两类旧 hash 地址兼容。**BREAKING（规范语义）**：规范地址由 hash 改为 path，旧链接继续解析。
- 修复 SSE 错误事件调用未加载函数、流式滚动抢位、知识库空态/迟到列表覆盖页签、长链接撑破气泡；恢复跨会话定时任务通知，复用已授权的运行记录与通知偏好。
- 实施时追加恢复成员模型目录丢失的 HTML 面板（GAP-008），作为路径权限验收的一部分；沿用既有目录 API 和授权投影。
- 接入 Web 源码更新菜单及检查/启动/状态接口，使用 database 平台级授权和明确的 fork 发布来源、固定目标提交、并发锁、恢复与审计；Docker/安装包继续显示适用的部署升级方式，不伪装支持源码自更新。
- 扩展现有来源映射、路由和接缝检查，增加可持续核对的能力清单、实际前端加载/符号依赖检查、上游入口对照、必要用例执行清单和真实页面验收；在本地统一入口及实际 GitHub/Gitea 交付流水线复用，验收阻断效果。
- 修改 `doc/master合并到rdai分支规范.md`，限定 `keep-fork` 为逐功能裁定，明确历史缺口跨轮继承、同候选证据、跳过/未收集不得计为通过；仅为此文档设置 Git 忽略例外。
- 本次提案不承担退出整个 `console.js`/`console.css` 的全量迁移。受影响功能先有唯一生效实现，移植范围记录上游来源；完整模块化仍遵循既有裁定和退役要求，不作为修复这些缺口的前置。

## Capabilities

### New Capabilities

- `web-console-runtime-parity`: 规定实际控制台的错误事件处理、跨会话任务提醒、流式滚动、知识库面板状态和消息换行行为；这是为既有产品能力补足规范，不是新增业务运行时。
- `web-console-source-update`: 规定 fork 发布源下的 Web 更新入口、平台授权、目标一致性、并发和恢复契约。

### Modified Capabilities

- `console-route-lifecycle`: 将规范地址改为路径，兼容旧地址，并明确实际后端路由、资源路径与区域登记的一致性。
- `web-console-frontend-modules`: 把延期项与持续能力清单关联，要求实际加载依赖完整、模块漂移及必要行为验证，避免“文件合入”冒充生效。
- `fork-upstream-decoupling`: 加强无冲突语义漂移、历史缺口继承、必要用例执行和实际交付检查；既有冲突决策不能授权丢弃未来上游增量。

## Impact

- 前端：`channel/web/chat.html`、实际使用的 `console.js`/`console.css`、`static/js/{core,chat,views,fork}/**`、模板和增量样式；遵守单次装载与既有覆盖映射，避免同名全局符号重复声明。
- 后端：`route_registry.py`、`fork/handlers/{pages,update,scheduler}.py`、现有更新服务及任务运行记录查询；所有生产测试指向 `build_web_app()`。不把上游独立 `build_app()` 当作 rdai 的验收对象。
- 检查与交付：复用 `scripts/check-production.sh`、路由/接缝工具、pytest/Node/浏览器 runner，扩展 `.github/workflows/test.yml` 与 `.gitea/workflows/production-readiness.yml` 的受影响检查；远端 runner/必需检查需真实验证，不能仅凭 YAML 宣称生效。
- 数据唯一归属：IAM/租户/owner 仍由既有服务决定，业务会话与运行记录仍在原存储；通知游标/去重只作客户端状态；更新状态与锁复用更新服务，能力清单只记录同步证据，不参与业务授权。
- 跨 change 依赖：继承 `console-navigation-availability`、`database-scheduler-console`、`session-history-workbench`、`web-console-module-seams` 和现有审计/实例恢复契约；与进行中的 `harden-production-readiness` 共用检查入口及恢复能力，只依赖相应已验收切片。保持 SAP/桌面 Web 容器导航和重登行为，不扩展桌面本地运行时范围。
- 证据边界：四项缺陷已做代码片段/隔离浏览器复现，两项入口缺口由源码加载链确认；未做生产定时触发或实际源码更新。完整依据见 `evidence/baseline.md`；产物完成不代表修复或门禁已经交付。
