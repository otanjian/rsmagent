# 账号记忆实施证据

实施日期：2026-10-02。集成分支：`rdai`。功能来源固定为 `origin/master@48c0d79c36146950667f8d1884ab823deae62305`，未整支覆盖。保留工作区已有「能力中心」更名及其他 change。未提交、推送或归档。

## 来源与实际装配

8 个 master 来源的 SHA-256 见 `master-baseline.json`，已逐个用 `git show <commit>:<path>` 重算核对。`doc-editor.js` 当前内容与该 master 文件完全相同，其余按账号/租户边界适配。

| master 来源 | 集成目标及裁定 | 实际运行入口 |
| --- | --- | --- |
| `channel/web/templates/views/memory.html` | `channel/web/templates/fork/views/memory.html`；沿用双标签、列表/详情结构，移除 Agent 选择，增加本人状态/管理动作 | `chat.html` 的 include 由现有模板渲染器展开，`/admin` 与 `/chat` 复用 |
| `channel/web/static/js/views/memory.js` | `channel/web/static/js/fork/views/memory.js`；沿用类别合并、表格、分页，目标固定 personal | `assets/js/fork/views/memory.js?v=<mtime>`，由 chat.html 实际装载 |
| `channel/web/static/js/views/doc-viewers.js` | 记忆 viewer 回调迁入上述 fork 模块；读取/保存改用正式记忆接口及 revision，保留共享 Markdown/title 渲染函数 | fork memory 模块调用 `createDocEditor`；技能 viewer 留在 console.js |
| `channel/web/static/js/doc-editor.js` | 原样复用 master 的通用编辑器；快捷键、未保存提醒、冲突交互共用 | `assets/js/doc-editor.js?v=<mtime>`，早于 fork memory 和 console.js |
| `channel/web/api/memory.py` | 保留列表/内容字段及分类排序契约；账号授权放在 `fork/handlers/memory.py` → `memory_console.py` | `route_registry.py` → web_channel 的活跃 fork Memory/List/Content/Save/Delete/Clear handlers |
| `agent/memory/service.py` | 保留 Agent 兼容分类/排序服务；个人分支委托扩展后的 `PersonalMemoryService`，不使用上游单用户根 | memory_console 以已验证请求身份选定服务；旧 personal handler 同样委托 |
| `agent/memory/summarizer.py` | 保留摘要/梦境生产流程，个人内容及配套记录使用统一批次发布 | MemoryFlushManager 捕获运行时身份和 scope token 后生成/提交 |
| `agent/evolution/record.py` | 保留记录格式，个人分支使用同一根/发布服务，进化执行时先暂存再提交 | executor → record → PersonalMemoryService；个人备份/undo 也验证同一归属及 generation |

单体 `console.js` 已删除原记忆列表、目标选择及记忆编辑器定义。仍保留导航、登录/租户状态、通用 fetch 包装、共享文档渲染和技能编辑等非本切片功能。装载顺序为 doc-editor → i18n（含 memory-account）→ fork memory → console；源码及实际渲染检查确认唯一记忆实现。

Chrome 隔离页面实际观察到上述带版本的 script URL，并完成真实 HTTP 的读取、编辑和冲突处理。截图见 `browser-memory.jpg`。请求精确参数由实际 fork 模块与 console fetch 包装组合测试覆盖：string、URL、Request、JSON POST 都保留租户信息且不注入 agent_id；显式旧 Agent 目标和其他 Agent API 保留原行为。浏览器工具不开放 Performance 网络读取，未把组合测试冒充完整网络抓包。

## 实现与场景证据

| 场景 | 实现及可复查证据 |
| --- | --- |
| 账号、租户与 Agent 归属 | 正式/兼容 API、工具和服务共用可信 `(tenant_id,user_id)`；`test_two_tenants_two_users_two_agents_tool_to_formal_api_and_relogin` 使用真实身份库、cookie、两 Agent、两租户，验证写入→列表/正文→检索→重新登录→隔离→清空 |
| 四类个人条目 | MEMORY.md、memory/*.md、evolution/*.md、dreams/*.md；同名记录使用完整相对标识；个人四类可编辑，只有正式记忆参与检索 |
| 完整管理与旧行为 | 正式保存/删除要求 revision；全分类清空要求集合版本；旧无参数清空仍仅 memory 分类。旧 Agent 根授权/只读历史保持原契约，个人管理员无他人入口 |
| 并发、安全路径 | 用户根文件锁 + 进程内重入锁；所有正文/索引提交、清空/恢复共用操作版本。POSIX 路径从根目录逐段锚定，拒绝 owner 的父路径替换。独立双进程各 15 次状态更新及已有受控交错、软链接测试通过 |
| 写入与检索 | memory_add 先发布文件；稳定默认 ID、追加去重；索引仅枚举当前租户绑定 Agent，失败明确 pending。清空清理本人 index-only 残留，重试按当前文件重建，不删除清空后的合法新内容 |
| 自动产物 | 摘要、梦境实际文件及 evolution 记录经真实发布服务持久化；模型输出受控。真实 executor + Write 工具测试验证 SILENT 丢弃、有效结果提交、个人记录及备份，未调用外部模型 |
| 进化备份/撤销 | 个人备份只落在本人根，验证 owner、租户、原工作区和 generation；清空保留备份文件但禁止旧备份恢复。测试覆盖他人不可见、关闭写开关、索引故障及重试；共享/无个人身份备份原入口不变 |
| 功能开关/审计 | 关闭 personal_memory_write 阻止人工、工具、自动及 undo 新增/修改；读取、删除、清空、索引修复保留。测试覆盖清空后重新打开仍拒绝旧任务。审计不含正文 |
| 前端身份变化 | owner 代次 + AbortController +请求序号；退出/登录/租户变化清理列表和编辑器；迟到读取不覆盖新页面，旧草稿和延迟确认不能提交给新账号 |
| 迁移 | 默认只读预览，清单、源内容备份和指纹账本；已存在/新编辑内容不覆盖；片段标注不完整；重复执行、中断续跑、按批撤回、已删除/清空不重放有隔离测试 |

`common.safe_fs` 的 Windows fallback 仍以系统 ACL 为外部边界；本次未作 Windows 实机验收。新增根祖先链测试只适用于提供 dir_fd/O_NOFOLLOW 的平台，未伪称 Windows 同等路径竞争保证。

## 验证记录

实施前后使用相同 Python3.14 虚拟环境 `.venv/bin/python`。主回归命令：

```bash
.venv/bin/python -m pytest -q tests/test_memory_*.py tests/test_personal_memory*.py \
  tests/test_user_personal_memory.py tests/test_summarizer*.py tests/test_evolution*.py \
  tests/test_safe_fs.py tests/test_doc_edit.py tests/test_account_memory_management.py \
  tests/test_route_registry.py tests/test_web_module_seams.py \
  tests/test_desktop_auth_flow.py tests/test_desktop_web_session.py

node --test tests/test_memory_target_picker_frontend.cjs tests/test_memory_write_frontend.cjs \
  tests/test_console_i18n_parity.cjs tests/test_console_i18n_coverage.cjs \
  tests/test_desktop_host_frontend.cjs tests/test_desktop_context_frontend.cjs \
  tests/test_desktop_signout.cjs tests/test_desktop_account_frontend.cjs

.venv/bin/python scripts/check-route-coverage.py
.venv/bin/python scripts/check-web-module-seams.py
openspec validate fix-account-memory-management --strict
git diff --check
```

最终结果见同目录 `validation.json`。跳过项是 Windows 反斜杠路径用例，不影响本机已跑场景。前端 75 项通过；路由检查 230 路由/281 方法条目；模块接缝检查 22 上游模块、364 fork 符号、0 发现。

实施前共享知识检索失败的复核：用例用 `knowledge/log.md` 当普通知识文件，而原同步逻辑有意排除 index.md/log.md 控制文件。将夹具改成 `knowledge/note.md` 后继续断言共享知识可检索及用户数据隔离，产品知识过滤规则没有改动。原失败没有删用例或标跳过。文档编辑装配测试改为读取实际模板展开及装载后的 JS，保留原编辑器断言。

## 迁移/部署与未完成项

截图账号的目标部署仅执行了只读预览：现有个人文件 1、候选 0、排除 0，generation/op_version 均 0；无需复制文件才能显示。没有对真实账号运行 apply/rollback、删除、清空或写入。报告不包含正文；候选为 0，不产生迁移批次或备份。建议每批不超过 50 条、迁移备份保留 30 天；具体命令和故障撤回步骤见 `docs/account-memory-management.md`。

localhost:9899 使用 `autoreload=False`。初次应用代码时未重启；2026-10-02 用户反馈编辑按钮缺失后，复现新前端配旧后端返回缺少 actions 的问题，已将旧进程 1069 正常退出并由原 Desktop 监管进程重新启动为 24755。健康检查 HTTP 200，实际账号页面已显示清空、编辑及删除按钮；未修改记忆正文。后续代码部署仍须重启后端并刷新页面，不要混用旧/新进程写同一用户根。开关回撤时先停止调度、关闭新增/修改并重启写进程，旧队列退出后再重开。

**任务 7.2 未完成。** Chrome 实际页面完成双标签、Markdown、Cmd+S、未保存提醒、冲突后覆盖保存和清空范围提示；删除/清空的实际数据变更由隔离 API 端到端测试验证。初次后续浏览器命令曾被工具的请求策略加载错误阻断；本次补验已恢复浏览器访问并核实真实部署编辑入口。新版本 Desktop 完整实机操作及剩余场景仍未验证，不能用宿主/登录自动化测试替代。Desktop 当前账号内嵌控制台复用这套 HTML/JS；旧独立 React MemoryPage 的显式 Agent 兼容页面未改造。没有向未验收客户端宣称已完整交付，也未据此归档。
