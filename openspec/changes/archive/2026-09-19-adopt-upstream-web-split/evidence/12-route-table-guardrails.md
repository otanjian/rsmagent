# 12 - 上游 URL 表的采纳与护栏作用域

对应 tasks `3.2a` / `3.2b`。本文件记录 D8 在入口模块的落地，以及它暴露出的
一处护栏口径问题——后者是本次改动里最容易被忽略、也最容易做错的部分。

## 1. 落地内容

`channel/web/web_channel.py`（627 → 787 行）：

- `URLS`：上游 `origin/master` 的 URL 表，**用 `ast` 从上游文件逐字提取**后
  写入，不手抄（87 行 / 77 组 pattern-handler）。
- `_upstream_namespace()`：惰性 `import channel.web.api.<module>`，收集各模块内
  的 `*Handler` 类，然后**只保留 `URLS` 实际引用的名字**。表内出现无处提供的
  名字时显式 `RuntimeError`，而不是回落到 fork 的同名类。
- `build_app()`：`web.application(URLS, _upstream_namespace(), autoreload=False)`。
  上游 `channel/web/core/channel.py:1507` 调用它，故不可删除。
- `build_web_app()`：不变，仍以 `_WEB_URLS` + 本模块 `globals()` 构建，并挂
  `enforce_http_policy`。

两处刻意的设计选择：

- **惰性 import 是硬约束**，不是性能优化。分支上 `channel/web/api/**` 尚不存在
  （随 merge commit 引入），而 fork 的线上路径不得在 import 期依赖上游栈；否则
  这一提交在分支上就无法成立。
- **上游 handler 类不进 `globals()`**。上游 `api/` 的 handler 类与 fork 的
  handler 类 **76 个名字里 64 个同名**（`ChatHandler` / `AuthLoginHandler` /
  `ConfigHandler` …），而 `web.py` 是按名字在映射里解析 URL 表的。谁后 import
  谁生效，于是两套表里必有一套解析到另一栈的 handler——**静默走错授权路径，
  而不是崩溃**。这是 D8 选择双命名空间的唯一理由。

## 2. 实测

```
build_web_app() -> application          # fork 线上路径不受影响
_WEB_URLS: 352 项    URLS: 154 项
build_app() -> ModuleNotFoundError: common.channel_registry
```

最后一行是**预期且正确的**：`common/channel_registry.py` 属于上游新增的 55 个
.py 文件之一，随 merge commit 引入，不在本提交内（本轮曾尝试在分支上预先吸收
`channel/web/api/**`+`core/**`，随即发现它们还依赖 `common/channel_registry.py`、
`models/model_catalog.py` 等，半个上游树只会制造无关失败，已回退）。`build_app()`
的可运行性由阶段 2 门槛在完整树上验收。

## 3. 连带发现：护栏把「整个 web 层」当成了「fork 的 web 层」

`tests/test_route_registry.py::test_core_files_no_longer_carry_route_literals`
按名禁用字面量 `'/api/health', 'HealthHandler'`，而上游的 `URLS` 逐字包含它。
D8 落地后该断言立即失败——**失败原因却是上游的合法代码**。

问题的根在 `tests/_helpers.py::web_layer_source()`：它读 `channel/web/**/*.py`
全部文件。阶段 1 这样写是对的（当时层内只有 fork 的代码），但阶段 2 起层内还
有**逐字采纳的上游模块**，于是所有基于它的护栏都会被上游的合法实现污染。这不是
理论风险：上游 `api/auth.py` 保留了口令登录，而 fork 的
`test_no_resurrection_legacy_identity` 断言「已退役的共享口令登录不得复活」——
吸收上游后这条护栏会因上游代码而失败。

### 处置

`web_layer_source(include_upstream: bool = False)` 默认排除
`channel/web/api/**` 与 `channel/web/core/**`，并新增
`upstream_web_layer_source()` / `include_upstream=True` 供确需全层的断言使用。
理由写在 helper 的 docstring 里：这些模块不是 fork 的代码，fork 的护栏对它们
既无管辖权、也无解释力。当前分支上 `api/`、`core/` 尚不存在，故此改动**不改变
任何现有断言的结果**（实测 25 passed）；它的作用是在阶段 2 引入上游模块后，
让这批护栏继续问它们真正想问的问题。

### 断言本身改强，而非放宽

字面量禁令从「按名禁一个已知字符串」改为**逐模块正则**
`'/…', 'XHandler'`，即禁止任何手写 pattern-handler 对——覆盖面严格变大；作用域
只缩到三处有明确理由的例外：入口模块（按 D8 承载上游 `URLS`）、
`route_registry.py`（即清单本身）、上游 `api/`+`core/`（非 fork 管辖）。

新增 `test_upstream_url_table_is_verbatim_and_separate`，补上两道此前没有的保证：

1. **上游表逐字未改**：对解析后的 `(pattern, handler)` 对取 sha256 并钉住
   （`UPSTREAM_URLS_SHA256`，来源 commit `origin/master 8f1b19f1` 记录在测内）。
   取解析后内容而非文本，故重排字面量不误报，改动任一路由、handler 名或顺序即
   失败。这就是这张表的 drift gate，与前端 manifest gate 同一思路：上游换表时
   必须显式更新摘要。
2. **两栈不混用**：`build_app()` 必须用 `URLS`，`build_web_app()` 必须用
   `_WEB_URLS`。64 个同名 handler 之下，混用不会崩，只会静默错栈。

### 非空验证

护栏只有能失败才有价值（阶段 1 的 `2.9b` 已因「迁移后静默变空」栽过一次），
故逐一构造诱因确认失败落在**预期的那条**断言上：

| 诱因 | 预期失败 | 实测 |
| --- | --- | --- |
| 把 `'/api/health'` 改成 `'/api/healthz'` | 摘要不符 | `test_upstream_url_table_is_verbatim_and_separate` |
| `build_app()` 改用 `_WEB_URLS` | 两栈混用 | 同上 |
| 在 fork 模块追加 `('/api/health', 'HealthHandler')` | 手写表 | `test_core_files_no_longer_carry_route_literals` |

三次诱因均只使预期测试失败；还原后 25 passed。

## 4. 回归

`tests/test_route_registry.py` 25 passed；
`test_upstream_core_seams` / `test_channel_signature_seam` / `test_http_policy` /
`test_no_resurrection_legacy_identity` / `test_agent_web_management` /
`test_web_navigation_mode` / `test_scheduler_web_update` / `test_doc_edit` /
`test_config_subagent_toggle` 合计 127 passed, 2 skipped。

## 5. 对后续阶段的约束

- 阶段 2 引入 `channel/web/api/**` 后，**必须重跑**上述护栏，确认
  `web_layer_source()` 的新作用域确实使它们继续有意义（尤其
  `test_no_resurrection_legacy_identity`，它最可能被上游的口令登录代码污染）。
- `UPSTREAM_URLS_SHA256` 只在采纳更新版上游表时更新，且须同时更新
  `UPSTREAM_URLS_SOURCE`；把它当作 merge 的检查项之一，不要顺手"修绿"。
