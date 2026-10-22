# 会话布局与遗漏入口修正（2026-10-22）

用户追加验收包括：智能体进入会话后欢迎页偏移、隐藏账号菜单的检查更新、找回品牌区下方的新建会话按钮。

## 原因及调整

- 实际点击智能体卡片已复现欢迎图标偏左，回归断言失败。旧渲染函数丢失 master 欢迎节点的容器属性，又错误启用要求 `.home-intro` 的旧 hero 布局。本次克隆完整原始节点，按真实结构选择旧 hero 状态；示例卡片保留 master 的提示文字及 `data-send` 行为。
- 原 rdai 基线 `c9bd4942261156f56c02ffd65e4d81c6153e1594` 的新建按钮模板位于品牌与主菜单之间。当前控制器和样式仍在，仅模板遗漏，现原位接回，不新增创建流程或存储。
- 用户复核指出右侧智能体下拉仍缺失；原因是 sidebar surface 仍依赖旧版样式开关。本次与历史面板一致按可用智能体数量显示箭头，不启用旧版菜单重排；保留同一目录和下拉渲染。增加实际选择检查，并修复手机选择成功后抽屉未关闭的问题，统一在会话提交成功后关闭手机侧栏并聚焦输入框。
- 检查更新按钮已有隐藏样式，但账号就绪事件会为管理员移除。本次去掉重新显示动作；底层更新控制器不变。原浏览器测试改为确认入口隐藏，再直接调用控制器验证原确认和身份失效保护，不把该测试表述为可见更新入口验收。

## 验证

- 修复前新增实际点击场景在图标居中断言失败，截图与用户问题一致；保存 `results/navigation-followups/before-results.json` 及 `agent-conversation-before.png`。修复后专项 17 场景通过。
- 基线为 rdai `67d87e31662423a4479dd96700342776d319d498`。`NODE_PATH=tests/browser/node_modules UPSTREAM_SYNC_BASE_SHA=67d87e31662423a4479dd96700342776d319d498 .venv/bin/python scripts/check-upstream-sync.py --output .sync-test/sidebar-agent-picker-final-required` 通过，运行前后摘要均为 `9e42c8b61812f3d74be56353fd9810bf147bdfd01cd6f2f138649f3e2e3bc512`。
- 必需 Python 42 例、Node 24 例和浏览器 40 场景全部执行；实际浏览器 45 场景全部通过，页面错误及意外路由为空。报告、专项结果及截图保存于 `results/navigation-followups/`。
- 实际点击智能体卡片、侧栏按钮、历史新建及输入框加号，检查会话 ID 更新、输入框焦点、历史面板开关、刷新、1024×768 与 390×844 视口；欢迎图标、标题、卡片保持居中，输入框位于视口内。卡片命令和提示文本也经实际点击检查。
- 原位按钮位于品牌区与导航之间，收起时加号能用 Enter 建立新会话，手机点击后抽屉关闭；管理员和成员的账号菜单均未显示检查更新，既有更新确认和身份失效保护通过。
- 下拉追加验证覆盖展开、收起和手机的实际智能体选取，确认所选智能体及会话 ID 更新、菜单关闭、手机抽屉收起；展开和收起菜单截图分别为 `sidebar-agent-picker.png`、`sidebar-agent-picker-collapsed.png`。手机抽屉关闭的修复前失败记录见 `picker-mobile-before-results.json`。
- 新增成功创建后的焦点处理后，隔离会话单元测试夹具缺少 `focusChatComposer` 替身，曾有两例报未定义；补齐原有渲染替身后 14 例全部通过，最终完整门禁亦通过，未在产品代码中添加掩盖错误的条件分支。
- 复核最终宽屏、收起账号弹层与移动截图；localhost:9899 页面返回 200，恢复的新建按钮存在，实际 `/assets/js/fork/extensions.js` 与 `/assets/js/fork/source-update.js` 返回 200 且逐字节匹配受测候选。
- OpenSpec strict、delta 基线与活动导航 change 重叠核对通过。沿用 PR #8，在最终提交通过实际 required Web CI 后正常合入；最终 CI 与交付提交记录于 PR。

Desktop 测试继续停止，本次 Web 证据不替代延期的桌面验收。
