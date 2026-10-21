# Desktop 后补验收（已按用户要求停止）与 Web CI

候选基于已交付的 `0bd9406e0e92dd993fbddb0bded6f73be9097f1a`，后补分支 `codex/sync-master-followup-validation-20261021`，PR #4。最终完成状态以 tasks.md 及本文件更新后的逐项证据为准。

## 已发现并修复

- `app.py` 默认字符串渠道的启动日志崩溃；36 项启动/渠道测试通过。
- Desktop TypeScript disabled 属性及 CSS 括号错误；完整类型检查、renderer/main 构建通过。
- 只读 fs-guard 被第二次 write-ops 构建覆盖：按 bin 分开构建，并新增真实 stdio 包资源核验；readonly 39 单测＋28 stdio 通过，写模式 5 项通过。
- 登录地址初始化竞态、工作区文件适配器装配顺序、Settings 被迟到容器盖住、本机/远程切换复用旧容器、自动配对后断开状态及后端退出竞态。实际 Electron 33.4.11 远程流程 17/17、源运行重登/文件面板 6/6 通过；真实模型验收另记，不由固定响应替代。
- 桌面脚本 937 项：931 通过、0 失败、6 项仅 Windows 的平台跳过，后者不算验收。
- 实际 macOS 包启动发现 --user-data-dir 被覆盖、COW_DATA_DIR 在 bundled spawn 被硬编码覆盖；已修正，后续本机真实模型和页面验收 6/6 通过（0 skip），桌面完整验收仍未完成。
- Gitea run 740 捕获到真实页面 `messagesDiv is not defined`：utils.js 的 ResizeObserver 在 defer 加载间隙早于 chat/state.js 执行。将注册移至 chat/state.js 初始化之后，保留 master 滚动行为，增加提前 resize 回归。

## CI

已安装仓库专用 runner `rsmagent-web-sync-macos-arm64`（repo scope、capacity 1、独立 HOME/cache/work），Web required job 按 PR 触发。原先没有 runner / required protection，不伪报已阻断。

真实运行 736/738/740 记录了 Node/path、Gitea 子路径 checkout、Chromium 页面时序及 artifact 上传问题。修复 checkout 使用事件中的完整 clone URL；artifact v4 丢弃 /Gitea，改用固定 SHA 的官方 upload-artifact v3。

独立验证 target 与 PR #5 用于实际必要用例 skip → 合并拒绝 → 恢复通过验收；不向 rdai 注入故障。run 745/747 成功，run 746 必要用例 skip 失败；实际合并请求返回 405，恢复后返回 200。同等保护已启用在 rdai，9.4 已完成，详见 ci-status.md。

## 未完成范围

用户已停止桌面测试。macOS 包真实本机模型/文件、换身份及 Web/桌面菜单已取得部分通过证据；独立远程真实后端验证发现多租户会话未同步到原生文件绑定（tenant selection required），未修复。Windows 后端与 helper 构建有通过结果，但打包失败、安装/升级/回退未验收，AppContainer 跳过不算通过。12.1–12.4 保持未完成，不发布安装包。远程测试服务和隧道已停止。
