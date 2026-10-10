# 实施和验证记录

本记录整理本会话已执行的检查，产品代码基线为 `27fe5b6b21254ad3069dda377f2b107ae41b1c0c`，参照的 master 为 `48c0d79c36146950667f8d1884ab823deae62305`。文档补记不重做业务实现。

## 菜单和路径检查

| 检查 | 结果与边界 |
| --- | --- |
| `NODE_PATH="$(npm root -g)" node --test tests/test_home_composer_menu_layout.cjs tests/test_composer_agents_frontend.cjs` | 智能体菜单修复后 8/8 通过；其中布局检查加载产品 CSS，覆盖 1440×900、1280×720、375×812，92px/220px 输入高度，首页/已有会话及 3/12 个智能体 |
| `NODE_PATH="$(npm root -g)" node --test tests/test_home_composer_menu_layout.cjs` | 最终 `/`、`@` 修复后 2/2 通过；既有工作空间和智能体菜单布局继续通过，移除了已失效的向下展开断言 |
| 两份 JS 的 `node --check` | 目录提示修复后语法检查通过 |
| 实际 `_wsSelUpdateLabel()` 的隔离执行 | 每份源码 7 类路径 × 默认/已选择两种状态，共 28 案例通过；提示正确且原始状态和项目名称未改变 |
| 最终完整页面浏览器检查 | 使用本地服务渲染的页面和生产 CSS/JS，后端使用隔离响应；3 种视口分别真实输入 `/`、`@`，菜单位于输入框上方、横向不越界、末项可内部滚动到达，页面脚本错误为 0 |

路径案例包括 POSIX 多级路径、Windows 盘符路径、UNC 路径、末尾重复斜杠、单级路径、根路径及空值。对照示例分别为 `user/usr_demo`、`projects/demo`、`workspace`、`/` 与本地化选择提示。按钮之外的完整目录状态未被改写。

模型菜单由 `c5a3fe6e` 恢复公共工具栏规则及 380px 上限，已核对样式；本记录不将工作空间几何测试声称为独立模型菜单全流程验收。

## 最终指令/引用菜单几何

坐标为浏览器 CSS 像素，末项在菜单滚动到底后可见。计算样式的上方间距为 6px；按输入框外边框测得为 5px，来源于 1px 边框。

| 视口 | 菜单 | 顶部 | 底部 | 高度 | 横向范围 |
| --- | --- | ---: | ---: | ---: | --- |
| 1440×900 | `/` | 179.17 | 499.17 | 320 | 365–1283 |
| 1440×900 | `@` | 199.17 | 499.17 | 300 | 365–1283 |
| 1280×720 | `/` | 162.95 | 482.95 | 320 | 285–1203 |
| 1280×720 | `@` | 182.95 | 482.95 | 300 | 285–1203 |
| 375×812 | `/` | 268.66 | 588.66 | 320 | 19–356 |
| 375×812 | `@` | 288.66 | 588.66 | 300 | 19–356 |

保留两个代表性截图：[桌面指令菜单](browser/1440x900-slash-menu.png)、[手机引用菜单](browser/375x812-mention-menu.png)。截图展示的是滚动到列表末尾后的菜单，内容和账号均为测试数据。

旧的 `test_appearance_browser.cjs` 整页夹具没有当前模板清单替换，并在工作台投影中返回了不应存在的 `revision`，直接复用时先遇到模板解析/初始化失败。最终专项检查读取本地服务已渲染 HTML，隔离投影按当前契约去除该字段；未修改产品认证逻辑。检查中 `/auth/context`、`/api/todos/summary` 未在旧夹具配置，不能据此声称整个外观集成套件、真实后端授权或待办功能验收通过。

## 版本、进程和部署

- 最初排查发现服务器源码提交已更新，但 Python 进程启动早于后端代码变化，源码 HEAD 与运行进程版本不能等同。本会话按授权重启目标应用，并通过 `RDAI-AppWatchdog`、`RDAI-NginxReload` 恢复/重载服务。
- 目标为 Windows 上 `C:\rdai\rsmagent` 的 `rdai` 分支，经 `rd.rsmxm.com.cn` 代理访问。后端与 Web 静态资源同由目标应用服务；没有把其他站点的 9898 或 OpenCode 相关进程作为重启目标。
- 每次推送前 fetch 并合并 `gitea/rdai`；部署前检查服务器干净工作树和 `rdai` 分支，fetch `rsmai/rdai` 并核对目标 SHA，仅允许 fast-forward。最终已部署产品提交为 `27fe5b6b21254ad3069dda377f2b107ae41b1c0c`。
- 在 `http://localhost:9899` 和公网入口，最后一次 CSS 部署检查均为：首页 200、页面引用的 CSS 200、按 LF 归一化后的 SHA-256 与工作区相同、`/api/health` 200。JS 目录提示提交 `3463be15` 同样核对了两端页面实际引用的 `console.js` 摘要。
- 后续 CSS/JS 修复属于静态资源更新，页面版本参数随资源更新，刷新可加载；没有数据库迁移或新 feature flag，文档补记也不需要进程重启。
- `/api/ready` 曾返回 503：数据库和目录检查通过，执行隔离为 false。Windows 执行隔离未满足是已知独立限制；健康探针与资源一致不能替代整体就绪验收。

## 规范校验

补记完成后执行 OpenSpec 严格检查，以及仓库脚本中仅负责规范增量的检查函数：

```sh
openspec validate fix-chat-composer-menu-layout --strict --no-interactive
.venv/bin/python - <<'PY'
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location('check', 'scripts/check_change_deltas.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
problems = module.check_deltas(Path('openspec'), 'fix-chat-composer-menu-layout')
assert not problems, problems
print('OK: delta requirements and baseline scenarios are consistent')
PY
```

两项均通过：OpenSpec 报告 change valid，delta 校验确认 MODIFIED 标题存在、主规范场景完整保留、新增 requirement 不重复。9 个产物文件和 10 个相对链接均已核对。

也尝试了完整入口 `.venv/bin/python scripts/check_change_deltas.py fix-chat-composer-menu-layout`。该入口默认同时要求任意 change 覆盖 `scripts/conflict-baseline.txt` 的全仓历史同步 seam，因此额外报告 `agent/memory/conversation_store.py`、`agent/tools/scheduler/integration.py`、`tests/test_scheduler_web_update.py` 三项未由本 change 覆盖；这些均不是本会话菜单改动。完整入口结果保留为未通过，不通过在菜单设计中虚构 seam 所有权或修改全仓基线来消除此报告。以上直接调用的是同一脚本真实的 delta 检查函数，不等同于全仓冲突覆盖通过。

发布治理任务另核对为 0/36，未因本次补记勾选。主 spec 同步及归档不属于本次补记动作。
