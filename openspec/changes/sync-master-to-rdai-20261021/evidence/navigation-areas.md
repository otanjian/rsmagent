# Web 导航区域修正（2026-10-21）

## 用户裁定与实现

用户确认保留「业务工作台／控制台」两个明确入口；区域内使用 master 页面、组件、菜单编辑器和路由交互。此前统一侧栏将控制台入口分散，现按后续裁定恢复合并前 rdai 的名称、位置、分组和顺序；底部账号区上方显示区域切换，区域标题反映当前位置。

- 内置页通过已有 route_registry → ConsoleRoutes 的 area 归属过滤；自定义产物和外链在业务工作台显示。
- 同一份本人菜单 JSON 保留完整分组、顺序、自定义标题、隐藏偏好及未知项；只过滤侧栏，不迁移数据或新增状态/存储协议。
- 控制台入口读取已有 `/api/menu` 的 `admin-home` 可用投影，页面和 API 继续按现有权限拒绝越权请求。
- 主要菜单的默认位置、分组和顺序同样以合并前 rdai 为准：业务区依次为对话、智能体、定时任务、知识库、场景应用、历史会话；控制台概览置顶，随后为智能体开发、模型与接入、组织与权限、平台管理。区域切换入口固定在侧栏底部、账号区上方，按当前区域显示控制台或业务工作台返回入口。用户已保存的自定义分组和排序继续优先，不进行布局迁移。默认不额外显示产物、待办、兼容平台用户页，原功能及其他入口保留。
- 切换复用 navigateTo 和主路由，保留深链接、历史、未保存文档保护；控制台概览继续从当前获准菜单生成快捷入口。
- 桌面测试按用户要求停止；本次只验证 Web，不声明 Desktop 通过。

## 验证

- `NODE_PATH=tests/browser/node_modules COW_SYNC_BROWSER_OUTPUT=.sync-test/navigation-position node tests/test_sync_master_web_browser.cjs`：16/16 真实浏览器场景通过，pageErrors 为空；使用真实隔离 WSGI、IAM、菜单存储和文件，不替代真实模型验收。报告和业务区、控制台、移动抽屉截图在 `results/navigation/`。
- 显式检查原始默认项顺序和四个控制台分组，区域切换位于导航滚动区之外、账号区之上；验证混合自定义布局、隐藏项、未知项保存不丢失、恢复默认、刷新及前进后退、权限拒绝、三语言标签/标题、技能编辑离开保护、移动和折叠侧栏。
- localhost:9899 的实际用户页面已人工核对名称、顺序和底部切换；保留账号已有品牌文案。实际用户截图仅保存在本机预览目录，不纳入仓库。
- 受影响后端补充集合（sync contract、menu grant、navigation mode、console menu mapping）为 67 passed / 2 failed。两个失败是 `ZeroVisibilityRegressionTests::test_a_member_keeps_every_page_it_could_already_open` 和 `test_a_tenant_admin_keeps_its_management_pages`，涉及 migration 20 的历史 `workbench.artifacts` 投影；同命令选中两例在原 rdai `0bd9406e` 上也失败。该历史基线问题不计为本次通过项，当前修正没有改动后端迁移或权限。
- OpenSpec strict、delta 基线和活动 change 同能力重叠检查通过。完整必要 Web 门禁通过，见 `results/navigation/required-report.json`，候选摘要 `230773555f70d8fae7594070851bf22ad85071f6e8577a516b391b515ed0f634` 在运行前后一致。此前菜单语言切换未更新页面标题的问题已修复。随后仅增加移动截图等待抽屉展开的稳定性断言；最终提交由实际 required CI 全量复验。

Gitea [run 749](https://rd.rsmxm.com.cn/Gitea/rsmai/rsmagent/actions/runs/749) 对提交 `fd9136921ec672635931d16aaba92c77d352dde5` 的实际必需 Web 检查成功，41 份证据文件上传成功。最终运行摘要 `76d6443c4f304608e10496644213817c3134fdf96fd243554016554d29d74d61` 在检查前后一致；已包含移动抽屉展开断言，全部必需用例执行通过。API 运行状态和从实际 job log 提取的原始报告保存在 `ci-run-749.json`、`ci-required-report.json`。后续仅补充本验收文档、截图及任务状态，不修改运行代码；最终 PR head 仍须在同一保护下再次通过检查后正常合入。
