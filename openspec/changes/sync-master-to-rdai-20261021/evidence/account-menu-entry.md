# 自定义菜单账号入口（2026-10-21）

自定义菜单属于本人当前租户的偏好，统一从侧栏账号菜单的「自定义菜单」进入，与个人偏好相邻；移除侧栏底部和分组旁的重复编辑入口。复用同一菜单编辑器及保存/恢复默认接口，打开时关闭账号弹层并将焦点移入编辑器，关闭后返回账号按钮；移动账号面板和折叠侧栏保持可用。

账号入口随现有账号身份状态显示/隐藏；只复用账号弹层、菜单编辑器和租户内本人布局，不添加偏好存储、路由或新的编辑页面。侧栏底部保留宽屏折叠按钮，移动视口隐藏空 footer。

移动视口下编辑器底部使用两列按钮，避免操作文字挤成逐字换行；错误提示占满一行。

## 验证

- 完整必要 Web 门禁通过：`NODE_PATH=tests/browser/node_modules UPSTREAM_SYNC_BASE_SHA=369a9ded089b3744d8c5d44c06fb38793540ecf5 .venv/bin/python scripts/check-upstream-sync.py --output .sync-test/account-menu-required-final`。候选摘要 `303cbd42491136f7ec81039a89cb66d6849688ccc4072610903bea6c3c2aa836` 在运行前后一致；清单要求的 Python 42 例、Node 24 例及浏览器 39 例均已执行，无缺失或失败。见 `results/account-menu/required-report.json` 和 `browser-results.json`。
- 菜单所在的 16 个真实 Web 专项场景全部通过，页面错误为零：从账号入口保存和恢复默认、刷新后保留布局、两用户及租户切换隔离、保存失败提示、移动账号面板及折叠侧栏、编辑器初始焦点与关闭后返回账号按钮。移动按钮无水平溢出。结果见 `personal-menu-results.json`，截图见 `account-menu-editor-mobile.png`。
- localhost:9899 的实际账号菜单已人工确认「自定义菜单」位于「个人偏好」之后，编辑器说明个人租户范围，Escape 关闭后焦点返回账号按钮。未修改实际用户的菜单布局；实际用户截图仅保存在本机预览目录。
- 旧账号补充集合 `node --test tests/test_account_menu_no_personal_resources.cjs tests/test_sidebar_account_frontend.cjs` 为 62 passed / 13 failed；同命令在原 rdai `369a9ded` 上结果完全相同，失败名称一致，涉及旧静态模板与旧身份模式假设。本次不将这些历史失败算作通过，基线对比见 `legacy-account-baseline.json`。
- OpenSpec strict、delta 基线、导航能力活动 change 重叠检查和差异格式检查通过。提交后仍须在不修改分支保护的前提下通过实际 required CI 才能正常合入。

Desktop 测试仍按用户要求停止；本项只验收 Web，不据此归档整个 change。
