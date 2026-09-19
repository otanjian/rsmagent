# 阶段 1 迁移前基线（task 1.6）

环境：`.venv` Python 3.14.3 / pytest 9.1.1 / node v24.14.1
目标提交：`rdai@b5c5090f`
工作树：干净（仅新增本 change 目录）

## 迁移前结果（全部通过）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 路由覆盖 | `.venv/bin/python scripts/check-route-coverage.py` | `176 routes (68 upstream, 108 fork), 221 method entries` → OK |
| 接缝回归（7 文件） | pytest `test_route_registry` `test_upstream_core_seams` `test_no_resurrection_legacy_identity` `test_conversation_schema_seam` `test_scheduler_identity_seam` `test_startup_hook_seam` `test_channel_signature_seam` | 91 passed, 2 skipped |
| 授权回归（6 文件） | pytest `test_http_policy` `test_identity_resource_authorization` `test_scheduler_web_update` `test_upstream_drift_guards` `test_recovered_entry_acceptance` `test_desktop_auth_flow` | 123 passed |
| 排练报告 | pytest `test_sync_report` | 9 passed |
| 分支片段契约 | `node --test tests/test_fork_fragments.cjs` | 6 pass, 0 fail |
| 执行权限 UI | `node --test tests/test_execution_permission_ui.cjs` | 5 pass, 0 fail |

合计：pytest 229 通过 / node 11 通过。

## 迁移不变量（阶段 1 必须保持）

1. 路由覆盖计数不变：176 routes（68 upstream / 108 fork）、221 method entries。
2. 上述 6 条命令结果不回退（通过数不减、skip 不增）。
3. 迁移前后同一请求的授权判定结果一致（阶段 1 新增对照断言）。
