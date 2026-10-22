# 顶部栏隐藏外链入口（2026-10-22）

按用户截图隐藏顶部栏的文档、官网和 GitHub 三个按钮。共享头部仅添加既有 `hidden` 类，保留节点与文档标识以兼容语言、品牌更新；不新增配置或存储。语言、主题和租户切换沿用现有行为。

现有 Web 浏览器场景增加可见性检查，覆盖三语言切换、移动控制台以及刷新后的业务工作台；同时确认语言和主题控制仍可见。先前的账号行折叠按钮验收见 `sidebar-account-collapse.md`，本次最终候选一并重验。

## 验证

- 基线为 rdai `67d87e31662423a4479dd96700342776d319d498`。`NODE_PATH=tests/browser/node_modules UPSTREAM_SYNC_BASE_SHA=67d87e31662423a4479dd96700342776d319d498 .venv/bin/python scripts/check-upstream-sync.py --output .sync-test/sidebar-header-required` 通过，运行前后摘要一致：`66a373b400d35cb90e64a38375ed0b643077855be00f2d5137e07f40e2c592a0`。
- 必需 Python 42 例、Node 24 例及浏览器 39 例全部执行；实际浏览器 44 场景全部通过，页面错误和意外路由为空。结果见 `results/header-shortcuts/required-report.json`、`browser-results.json`、`personal-menu-results.json`。
- 复核展开、收起及移动控制台截图，三个外链按钮均隐藏；账号行折叠按钮、收起后的上下排列以及移动抽屉保持正常。截图使用隔离测试账号，见 `results/header-shortcuts/`。
- 本地 localhost:9899 的 `/chat` 和 `/admin` 返回 200，三处按钮均带隐藏样式；无需重启服务即可提供更新的共享模板。
- OpenSpec strict、delta 基线及活动导航 change 重叠核对通过。代码与证据进入同一 PR #8，最终提交须经受保护分支的实际 required Web CI 后正常合入；CI 和交付提交记录于 PR。

Desktop 测试继续停止，本项不作为桌面验收证据。
