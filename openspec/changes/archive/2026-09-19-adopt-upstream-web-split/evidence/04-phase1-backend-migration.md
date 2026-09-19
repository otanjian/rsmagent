# 阶段 1：Web 后端 fork 实现迁出（tasks 2.1–2.6）

日期：2026-09-19
分支：`codex/adopt-upstream-web-split`（自 `rdai@b5c5090f`）
源：`rdai:channel/web/web_channel.py`（649 KB / 13,765 行 / 290 个模块级符号）

## 结果

`channel/web/web_channel.py` 从 13,765 行收敛为 **171 行**的入口模块（URL 表 + `build_web_app()` + 命名空间契约），
fork 实现迁入 **20 个模块**、**290 个符号**，逐字复制、零改写。

```
channel/web/fork/
  authorization.py          31 符号   授权/作用域 helper
  common.py                 23 符号   共享管道
  runtime.py                77 符号   WebChannel / WebMessage / SSEStreamState / SERVING 等
  handlers/agents.py        13         handlers/sessions.py    10
  handlers/auth.py           7         handlers/skills.py       6
  handlers/branding.py      11         handlers/update.py       1
  handlers/channels.py       8         handlers/workspace.py   27
  handlers/chat.py          12
  handlers/config.py         2         （17 个视图模块，镜像上游 api/ 划分）
  handlers/files.py         20
  handlers/knowledge.py      8         handlers/logs.py         4
  handlers/memory.py        11         handlers/models.py       1
  handlers/pages.py          4         handlers/scheduler.py   13
```

79 个 handler 全部迁出（15 个 fork 专有 + 64 个 fork 对上游视图的平行实现）。

## 迁移方法（可复现，不手抄）

`/tmp/rsm-agent-tools/`：

| 脚本 | 作用 |
| --- | --- |
| `analyze_fork_web_symbols.py` | 枚举 fork 专有符号与依赖（task 2.1） |
| `analyze_handler_wrappers.py` | 逐 handler 统计对 fork helper 的调用（发现 56/64） |
| `analyze_handler_divergence.py` | 逐 handler 度量与上游的源码分歧（证据 03） |
| `build_domain_map.py` | 依上游 `api/` 归属把符号分配到目标模块 |
| `emit_fork_web.py` | 按 AST 行区间**逐字切片**生成模块，并推导 import |
| `emit_entry_module.py` | 生成入口模块（保留原命名空间契约） |

源固定为 `rdai:<path>`（`FORK_SOURCE_REF`），迁移不依赖工作树状态，可重跑。

## 迁移中发现的三个必须处理的问题

1. **装饰器丢失**：`ClassDef.lineno`/`FunctionDef.lineno` 指向 `def`/`class` 行，直接切片会丢掉
   `@contextmanager`（`_db_scope` 等 6 处）与 `@singleton`（`WebChannel`），改变运行行为。
   已改为按 `min(lineno, *decorator.lineno)` 起切。**若只看测试通过数不会发现**：`WebChannel`
   失去 `@singleton` 后 `test_web_sse_cancel` 等 3 个文件在收集期即报错。

2. **模块间循环依赖**：fork 的 helper 跨视图互相调用，形成 **11 模块的强连通分量**，
   朴实模块级 import 必然循环。已改为在函数体内按需 import。

3. **命名空间契约（决定架构）**：仓库中已有多个模块把 `channel.web.web_channel` 当作内部接缝，
   在函数内惰性 import 共享 helper：

   ```
   Scene/_shared/http.py        _db_scope, _require_chat_use, _build_preview_url
   Scene/_shared/host.py        _get_workspace_root
   scenes/api.py                _db_scope, _require_chat_use
   scenes/api_workbench.py      _db_scope, _require_chat_use, _get_workspace_root
   channel/web/admin_handlers.py        _channel_target_candidates, _reload_agent_runtime
   channel/web/admin_overview.py        _agent_admin_service, _iter_tenant_agents, _tenant_ids_for_context
   channel/web/memory_console.py        _db_path_owner_forbidden
   channel/web/project_import.py        _read_uploaded_file_bytes_limited
   channel/web/route_registry.py        _stream_identity_scope
   auth/service.py                      _session_model_catalog, _tenant_admin_owns_agent
   agent/workspace/project_browser.py   _guard_not_database
   channel/channel_factory.py           WebChannel
   ```

   即 `web_channel` 已是既成的内部接缝。若 fork 模块直接互相 import，则**对
   `web_channel.<name>` 的 monkeypatch 不再拦截使用方**——不是测试洁癖，而是接缝语义被破坏。
   实测该差异导致 195 个用例失败（`patch("channel.web.web_channel._db_scope")` 不再生效）。

   **决定**：fork 模块对「hub 表面」（被 `web_channel.<name>` 访问或从中 import 的名字）的引用，
   一律在函数体内经 `channel.web.web_channel` 惰性解析。这与既有接缝语义一致，使 195 个用例
   **在测试零改动**的前提下全部恢复，也让 `web_channel` 继续作为唯一解析点。

## 入口模块保留的契约

- `_WEB_URLS = _derive_web_urls()`（由 `route_registry` 派生，非手写字面量）
- `build_web_app()`（安装 `enforce_http_policy` processor）
- 全部 handler 名在 `globals()` 中（`web.application(_WEB_URLS, globals())` 与
  `check_route_coverage(vars(web_channel))` 依赖）
- 原单体 49 条模块级 import 逐字保留（其它代码经 `web_channel.<name>` 读取/打桩）
- `globals().update(_SCENE_HANDLERS)`（场景 handler 注入，原第 139 行）
- `WebChannel` / `SERVING` / `SSEStreamState` / `WebMessage`（`app.py` 与 `channel_factory` 按名解析）

## 验证

| 检查 | 基线（`rdai@b5c5090f`） | 迁移后 |
| --- | --- | --- |
| 路由覆盖 | `176 routes (68 upstream, 108 fork), 221 method entries` → OK | 同 |
| 全量 pytest | 33 failed / 5414 passed / 7 skipped | 见 `05-verification.md` |
| 测试改动 | — | **0 个测试文件被修改** |

测试零改动是本次迁移最强的保真证据：同一套断言、同一套打桩点在迁移后仍然成立。

## 未纳入本阶段

- `channel/web/static/js/console.js`（932 KB）与 `console.css`（197 KB）的拆分属阶段 3（前端）。
- 上游 `channel/web/api/**`、`core/**` 的引入与路由权威清单跨模块化属阶段 2（合并）。
- 漂移守护（登记 fork 平行实现的 handler 及其上游来源）为必做项，属阶段 2。
