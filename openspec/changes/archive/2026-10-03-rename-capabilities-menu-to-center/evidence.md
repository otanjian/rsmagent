# 实施与验收记录

日期：2026-10-02。

## 已实施

- Web 控制台的 `menu_skills` 与 `skills_title` 已统一为简繁中文「能力中心」、英文「Capability Center」。
- 主页面及对应侧栏、技能页面模板的默认文案同步更新；翻译快照同步六个值。
- 产品与快照共修改 6 个文件、16 行文案；未修改导航实现、权限、路由、技能工具功能、原生 Desktop 字典或智能体详情「能力」页签。

## 已通过的检查

1. `node --test tests/test_console_i18n_coverage.cjs tests/test_console_i18n_parity.cjs`：10 项通过，0 失败。
2. 对运行中的 `http://localhost:9899/admin` 执行 HTTP 读取检查：实际返回的菜单与主标题默认 HTML 均为「能力中心」；按该 HTML 中的资源版本地址获取 navigation/core 翻译脚本，三种语言的菜单与标题值一致。此检查使用真实服务响应，不使用模拟接口。
3. `openspec validate rename-capabilities-menu-to-center --strict`：通过。
4. `git diff --check`：通过；差异检查确认仅修改目标文案与快照，`VIEW_META.skills` 继续引用 `menu_skills`。

## 待完成的真实界面验收

任务 3.2 保留未勾选：Chrome 控制台当前停在登录页；Electron 内嵌控制台已有有效会话，但继续使用已加载的旧翻译。应用的 Reload 快捷键刷新了外层 renderer，未刷新内嵌页面；控制台与工作台之间的导航也保留了当前文档。检查过程中控制台被继续操作，因此停止对该窗口的后续交互。

尚未完成新名称在真实已登录页面上的语言切换、classic/split、刷新及旧地址、窄屏和侧栏收起的完整目视验收。HTTP 及自动测试通过不等同于这些界面验收通过，当前 change 尚不应按全部完成归档。

## 发布与恢复

- 随正常 Web 静态资源发布；真实服务已返回带更新版本地址的资源，新文档加载后可获取新文案。
- 无数据库或业务数据迁移，无新增 feature flag，不改变现有 classic/split 或消费者开放规则。
- 回退本次两个翻译文件、三个 HTML 文件和快照中的目标文案即可恢复旧显示；无需调整地址或资源数据。
