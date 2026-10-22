# 账号行折叠按钮（2026-10-22）

侧栏折叠按钮移至账号行右侧，替换省略号，与头像/账号菜单按钮独立；移除最底部独立按钮栏。收起后头像与展开按钮上下排列，保留账号菜单、键盘操作、提示和刷新后的状态恢复；移动视口继续使用抽屉。复用现有折叠偏好键，统一共享导航与 rdai 样式的状态。

原有按钮仅更新 html 折叠类，rdai 宽度、账号弹层和历史控件仍读取 app 的另一标记；本次让旧消费者镜像既有持久状态，宽屏入口统一调用同一折叠函数。收起时账号弹层可从导航栏旁展开，导航容器仍独立滚动。按钮具有随状态更新的三语言无障碍名称，Enter/Space 使用原生按钮行为。

## 验证

- 基线为 rdai `67d87e31662423a4479dd96700342776d319d498`。`NODE_PATH=tests/browser/node_modules UPSTREAM_SYNC_BASE_SHA=67d87e31662423a4479dd96700342776d319d498 .venv/bin/python scripts/check-upstream-sync.py --output .sync-test/sidebar-collapse-required` 通过，运行前后摘要一致：`1b5f5372f95ff2c61599cf369d94d138b13c3868acaeb35efb948de771c5c146`。
- 必需 Python 42 例、Node 24 例及浏览器 39 例全部执行；实际浏览器 44 场景全部通过，页面错误和意外路由为空。结果见 `results/sidebar-collapse/required-report.json`、`browser-results.json`。
- 菜单专项 16 场景通过：展开时按钮位于账号右侧且独立点击；账号菜单已打开时折叠会关闭弹层；收起后侧栏宽度不超过 72px，头像与展开按钮上下排列；刷新后导航图标和折叠状态恢复；账号弹层可见且真实点击命中；Enter/Space 可收起与展开；业务/控制台切换、个人菜单编辑以及手机抽屉仍可用。
- 截图 `sidebar-account-expanded.png`、`sidebar-account-collapsed.png`、`sidebar-account-collapsed-menu.png` 来自隔离测试账号。刷新后的截图等待菜单实际加载完成，避免把中间加载状态当作布局结果。
- OpenSpec strict、delta 基线和活动导航 change 重叠核对通过；来源审查已更新，沿用既有分支保护，最终提交须经实际 required Web CI 后正常合入。

Desktop 测试继续停止，本项不作为桌面验收证据。
