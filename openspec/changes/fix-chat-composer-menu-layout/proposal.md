## Why

本会话修复了对话输入区菜单被压缩、远离触发按钮或展开到屏幕下方的问题，并按用户要求缩短了工作空间按钮的目录提示。现行 `workbench-appearance-preferences` 仍要求首页菜单使用输入组下方空间，需要补记本次已交付的行为，避免后续按旧规范重新引入缺陷。

## What Changes

- 工作空间选择菜单参考 master 的模型菜单，紧邻工具栏上方展开；工作空间和模型菜单不再受首页剩余空间公式压缩，最大高度为 380px。
- 智能体选择菜单在桌面保持头像按钮上方、右对齐和 380px 高度上限；手机端保留宽度保护并在工具栏上方展开。
- `/` 指令菜单和 `@` 引用菜单恢复输入框上方展开，最大高度分别为 320px、300px，长列表在菜单内部滚动。
- 文件夹按钮的目录悬停提示仅显示最后两个非空路径段，兼容 `/` 与 `\`，不改写实际工作空间路径。
- 补齐本会话提交、合并、运行版本核验与部署记录；区分菜单交付、已有发布治理方案及远端已有实现。

本 change 为已实现行为的追溯补记。实施提交为 `c5a3fe6e`、`f550c5d9`、`95f633d7`、`3463be15`、`27fe5b6b`；完成状态以证据为准，不因文档齐全推定其他 change 完成。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `workbench-appearance-preferences`：修正首页菜单方向和可达性契约，明确不同菜单的锚点、高度上限、窄屏行为及工作空间目录提示。

## Impact

- 样式：`channel/web/static/css/appearance.css`、`console.css`、`workspace.css`。
- 目录提示：`channel/web/static/js/console.js` 与 `chat/workspace-selector.js` 的 `_wsSelUpdateLabel()`。
- 回归：`tests/test_home_composer_menu_layout.cjs`；既有智能体前端测试及生产页面配合隔离响应的浏览器检查。
- 数据唯一归属沿用工作空间选择状态、服务端项目绑定和既有智能体/模型目录；提示文本为只读展示，不新增配置、API、数据库迁移或 feature flag。
- 依赖已存在的输入区、菜单和外观样式。与 `add-shared-agent-release-governance` 无实施前置关系，该方案及历史共享发布切片保持各自验收范围。
- 本次服务重启、Gitea 合并和固定提交部署是既有运维动作，记入证据，不新增部署能力或宣称 Windows 执行隔离就绪。
