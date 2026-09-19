# fork 符号清单与依赖分析（change task 2.1）

分析工具：`ast` 解析 fork 工作树 + `git show` 读取固定 `e5e2a52d`（共同祖先）与 `origin/master`，用于区分 fork 新增与上游既有。
输出：`/tmp/fork_symbol_map.json`

## 规模

| 项目 | 数量 |
| --- | --- |
| `web_channel.py` 模块级符号 | 291 |
| fork handler 类 | 79 |
| 上游 handler 类（api/+core/ 与共同祖先） | 76 |
| **fork 专有 handler** | **15** |
| **fork 专有非 handler 符号** | **149** |
| 合计待迁出符号 | 164 |

## fork 专有 handler（15）

`BrandingAssetHandler` `BrandingManageHandler` `BrandingPublicHandler` `BrandingResetHandler`
`MemoryClearHandler` `MemoryDeleteHandler` `MemorySaveHandler`
`PersonalChannelHandler` `PersonalChannelInstanceHandler`
`PersonalMemoryContentHandler` `PersonalMemoryHandler`
`ProjectImportCancelHandler` `ProjectImportHandler` `ProjectImportPreviewHandler`
`_MemoryWriteHandler`

## 依赖结构（决定可行性的关键结论）

- 149 个 fork 专有非 handler 符号中，**绝大多数 fork 内部依赖为 0**；依赖最多的仅 5 个。
- 仅 **9 个** fork 专有符号引用了非 fork 的 `web_channel` 符号，且每个边界引用只被 **1 个** fork 符号使用：

  | 被引用符号 | 引用方数量 |
  | --- | --- |
  | `_raw_web_input` | 1 |
  | `_read_uploaded_file_bytes_limited` | 1 |
  | `_get_workspace_root` | 1 |
  | `_agent_admin_service` | 1 |
  | `_request_agent_id` | 1 |
  | `_system_workspace_service` | 1 |
  | `_LOG_SECRET_RE` | 1 |
  | `_NAVIGATION_MODES` | 1 |
  | `_SCHEDULER_STATUS_LINES` | 1 |

- 被 fork 符号引用的模块常量仅 4 个：`_LOG_SECRET_RE`、`_NAVIGATION_MODES`、`_SCHEDULER_STATUS_LINES`、`_WEB_URLS`。
- 依赖扇出最高的符号：`ProjectImportHandler`(12)、`ProjectImportPreviewHandler`(10)、`BrandingManageHandler`(6)、`ProjectImportCancelHandler`(6)。

## 结论

跨边界耦合浅（9 处、单点引用），意味着 fork 定制可以按域整体迁出，无需重写实现；只需为 9 处边界引用选择归属（随迁、或从上游模块导入）。此为阶段 1 的可执行性证据。
