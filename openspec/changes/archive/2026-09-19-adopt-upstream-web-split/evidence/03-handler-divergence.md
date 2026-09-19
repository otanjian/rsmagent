# handler 分歧度量：fork 究竟是在「扩展」上游还是在「平行实现」（阻塞发现）

工具：`ast` + `difflib.SequenceMatcher`，对比 fork 工作树与 `origin/master` 的 handler 类源码。
输出：`/tmp/handler_divergence.json`、`/tmp/handler_wrapper_map.json`

## 结论一：56 / 64 个上游 handler 的方法体内调用了 fork 专有 helper

只有 8 个上游 handler 不含 fork 调用（`AssetsHandler`、`AuthCheckHandler`、`AuthLoginHandler`、`AuthLogoutHandler`、`HealthHandler`、`PromptOptimizeHandler`、`RootHandler`、`VersionHandler`）。
调用扇出最高的：`AgentsHandler`(18)、`SkillsHandler`(11)、`SkillContentHandler`(7)、`CancelHandler`(6)、`FileServeHandler`(6)、`ToolsHandler`(6)。

## 结论二：分歧是「方法体内部交织」，不是边界包装

以 `KnowledgeListHandler.GET` 为例（上游 `api/knowledge.py` vs fork）：

| 上游 | fork |
| --- | --- |
| `_require_auth()`（首行门禁） | 删除（legacy 退役） |
| 直接 `params = web.input(...)` | 外层包 `with _db_scope() as ctx:`（导致整块缩进） |
| — | `_require_read_permission(ctx, "knowledge.read")` |
| `_get_workspace_root(agent_id=_request_agent_id(params))` | `_require_tenant_agent_binding(ctx, ...)` + `_require_private_owner(ctx, agent_id)` + `_knowledge_workspace_root(agent_id)` |
| `svc.list_tree()` / 响应体 | 完全相同 |

即 fork 版本 ≈ 上游版本 − `_require_auth()` + 身份作用域包裹 + 权限/对象校验 + 受作用域限定的资源解析。业务逻辑与响应形状保持一致。

## 结论三：近似度低主要来自缩进位移，但语义差异不可忽略

`difflib` 按行比较，`with _db_scope()` 包裹使整块缩进 → 近似度被显著压低（`KnowledgeListHandler` 仅 0.485，而实际语义差异是上述 6 处系统化改写）。

64 个共享 handler 的近似度分布：
- ≥0.85（近似一致）：6 个
- 0.6–0.85：9 个
- <0.6（大幅改写）：49 个

## 这对方案的影响（关键阻塞）

原设计假设「上游 handler 保持不动，fork 授权经接缝附着」。该假设对 56 个 handler **不成立**，因为：

1. **预派发包装无法提供 `ctx`**：`_db_scope()` 的 `ctx` 在方法体中部被继续使用（后续校验、资源解析），包装器无法注入。
2. **受作用域限定的资源解析写在体内**：`_knowledge_workspace_root(agent_id)` 取代了上游的 `_get_workspace_root(...)`。若不替换该解析，上游方法体会解析出**未按租户/owner 限定**的路径 → 跨租户数据泄漏，属安全失败而非功能缺口。
3. **上游 `_get_workspace_root` 定义在上游 `core/_common.py`**：要让上游方法体不改而动，fork 必须改写上游文件——正是本 change 要消除的行为。

因此「采用上游 handler + 注入接缝」在不编辑上游核心文件的前提下不可达成；「fork 子类覆写」需要复制上游方法体（等于平行实现）。

## 结论

fork 的 Web 层在事实上**平行实现**了上游 49+ 个 handler。这不是本次合并造成的，而是既有状态。可选路线见 `design.md` 的修订决策。

文件内边界引用仅 9 处（见 `02-fork-symbol-map.md`），说明把 fork 实现整体迁入 fork 自有模块是**低风险的机械操作**；难点不在迁移，而在是否接受「上游 handler 改进需人工移植」。
