# 控制台与工作台的入口、窗口与落地地址

对应 change `harden-windows-desktop-reliability` 任务 5.2；契约依据：`console-entry-consistency`（首页直接导航动作 SHALL 跳转获准页面）、`console-route-lifecycle`（页面与页签具有稳定地址；重复选择同一页面不重复添加历史项）。

## 怎么用

| 操作 | 结果 |
|---|---|
| 点侧栏「控制台」（`/admin`） | 在**当前窗口**切到管理区，落在管理首页 |
| 点侧栏「工作台」（`/chat`） | 在**当前窗口**切回工作台（对话），地址规范化为 `/` |
| 首次点击 | 进入该区域的默认页（控制台 → 管理首页；工作台 → 对话） |
| **重复点击** | 停在同一地址：不新开窗口、不追加历史项，浏览器后退只需一次 |
| 中键 / Ctrl+点击 / 脚本尚未加载时 | 按 `<a href>` 在**当前窗口**导航到登记路径（`/admin`、`/chat`，后者按既有兼容规则规范化为 `/`） |
| 键盘 Enter/空格 | 与点击一致（`console.js` 同时绑定 keydown） |

## 为什么不带 `target`

改动前：`<a id="nav-open-admin" href="/admin" target="cow-admin">`、`<a id="nav-return-workbench" href="/chat" target="cow-workbench">`。实测差异（报告第 4 条）与原因：

1. 桌面容器对任何弹窗一律拒绝（`desktop/src/main/remote/container.ts` 的 `setWindowOpenHandler` 返回 `deny`），因此带 `target` 的入口在桌面端**点击后什么都不发生**；
2. 在浏览器里则新开一个命名窗口，而新窗口会继承 `sessionStorage` 里的 `cow_admin_pending_view`，于是落到上次待办的 `/scheduler` 而不是入口自己的 `/admin`；
3. `console.js` 的 `_openNavArea` 本来就写着"在**同一窗口**内切换"（避免整页加载闪登录遮罩），`target` 与实现意图直接矛盾。

现在两个入口都改为普通链接：JS 处理器（`preventDefault` + `_openNavArea`）负责同窗口切换，处理器未生效时按 `href` 同窗口导航。

## 排查

- **出现多余的控制台/工作台窗口**：属于旧版本行为留下的窗口，直接关闭即可；当前版本不再产生。
- **`/admin` 总是落到某一页而不是首页**：`cow_admin_pending_view` 只在区域切换需要指定视图时写入，并在下次进入管理区时**一次性读取并删除**（`_bootAreaDefaultView`）。若反复落到同一页，检查是否有代码重新写入该键。
- **深链接恢复失败**：按 `console-route-lifecycle` 既有规则处理（未知页面、非法页签、无权限各有独立的拒绝与恢复路径），本次未改动这些规则。
- **内容窗口（附件/预览）**：由容器的内容路径处理器按文档打开隔离窗口（无 preload、无 bridge、弹窗被拒），与上面的两个入口无关；它不是"命名窗口"，重复打开同一文档会各自开窗。
