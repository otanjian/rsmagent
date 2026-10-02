# A01 / A02 真机证据（真实 Electron + 真实原生选择器）

对应任务 2.5、验收 A01 与 A02。原文为 TAP 输出，保存在同目录
`e2e-a01-a02-run.log`（本次实跑，**6/6 通过**；另有连续 3 次复跑均 6/6，
见下方"稳定性"一节）。

运行方式（仓库根）：

```bash
node desktop/e2e/run-remote-workbench.mjs --spec ./directory-selection.spec.mjs
```

## 运行前置：必须先构建，且失败会明确说明

spec 启动的是**构建产物**（`main.e2e.cjs` require `dist/main/index.js`，窗口加载
`dist/renderer/index.html`），而 runner 本身**不构建**。因此"没构建"是**缺少前置**，
不是测试结论。runner 现在会在启动前检查这两项并以非零码快速失败：

```
[e2e] runner failed: Error: the app has not been built, so there is nothing for Electron to load:
  missing desktop/dist/renderer/index.html -- run `npm run build:renderer` in desktop/
```

复现与代价（这是本次真实踩到的坑，记录以免重演）：`dist/renderer/` 曾为空，
症状是 `electron.launch: Timeout 180000ms exceeded`，call log 只停在
`ws connected`，看起来像**产品卡死**而不是构建缺失——排查方向会被完全带偏。
修复后同一检查在 **0.02s** 返回（对比 180s），并直接指出缺哪个产物、跑哪条命令。
运行前置统一为：`cd desktop && npm run build`（= `build:renderer` + `build:main`）。

另：一次超时会让 Playwright 留下 Electron 进程（spec 的清理是
`if (app) await app.close()`，而 launch 抛错时 `app` 从未赋值，恰好是无效的那种）
——残留进程会占住内置后端的固定端口，使**下一次**运行因为无关原因失败。
runner 因此增加了收尾清理：按"可执行文件是 Electron **且** 参数含
`e2e/main.e2e.cjs`"精确匹配后 SIGKILL（不按整条命令行做子串匹配——那会误杀调用它的
shell，实测已踩到）。实测三次复跑后残留进程数为 0。

## 稳定性

连续 3 次运行均为 `# pass 6 / # fail 0`（约 29–45s/次），期间无残留进程。
此前出现过的 6/6 全红**不是** flaky：唯一原因是渲染层未构建（见上）。

## 真实的部分 / 被脚本化的部分

| 环节 | 是否真实 |
|---|---|
| Electron 主进程 | **真实**：`desktop/dist/main`（随包代码），非 mock |
| 安装标识、设备/工作区/grant 注册表 | **真实**：主进程模块实例，证据从主进程读回 |
| 本地后端与 console 页面 | **真实**：应用自带的 Python 后端 + `channel/web/static/js/console.js` |
| 登录 | **真实**：产品自己的 PKCE 流程（页面 → 系统浏览器 → loopback 回调） |
| `wsSelChooseLocalDir()` → `chooseWorkspace` → `bindContext` | **真实**：产品的入口与处理器 |
| 原生目录选择面板的**两个 OS 决定** | 脚本化：`main.e2e.cjs` 覆写 `dialog.showOpenDialog`，用 `dialog-script.json` 队列表达"用户选了哪个目录 / 用户取消 / 用户隔多久才回答"，并把每次调用与回答写进 `dialog-calls.jsonl` 供断言 |

被脚本化的只有"测试点不到原生面板"这一件事；`chooseWorkspace` 处理器、选择服务、
`activateGrant`、注册表全部是产品代码。取消与"慢回答被超越"因此是**用户时序的复现**，
不是逻辑替身。

## 覆盖的断言（逐条）

A01（首次无安装标识 / 取消）：
- console 容器真的附着了，且页面暴露 `CowDesktopHost.canChooseWorkspace() === true`；
- 清空有效安装标识（文件 + 记忆化值）后首次选目录：恰好一次原生调用，
  `properties === ['openDirectory']`，对话框文案含"只读访问"；
- 该次选择产生**恰好一条**授权，`purpose === 'readonly-input'`，`absolutePath`
  与用户所选目录一致（路径从主进程注册表读回，页面拿不到）；
- 页面上**没有** `Illegal invocation`（2.1 修的接收者缺陷回归）；
- 安装标识落在应用自己的 userData 路径上且已写入；
- 取消：不新增、不重激活、不递增任何授权，且 chip 仍显示原目录（A02 的
  "保留旧项目时 UI 清楚显示旧项目仍生效"）。

A02（被更新的选择超越 / 会话已移动）：

- 第一次 `wsSelChooseLocalDir()` 被"用户"挂住不回答，随后第二次选择 `beta` 并生效；
- 挂住的那次**确实**以真实目录回答（`dialog-calls.jsonl` 有 `return` 记录且
  `canceled === false`），即拦截它的是**代次保护**而不是"对话框被关掉"；
- 结果是 chip 仍为 `beta`，注册表里 `held` 0 条、`beta` 1 条 —— 被超越的选择
  没有提交；
- 另一条：对话框打开后用户用产品自己的 `newChat()` 进入新会话（实测 `sessionId`
  已改变），再让旧 Promise 带真实目录返回 —— `_desktopContextForRequest()`
  在新会话为 `null`（**无错绑**），chip 也不是被放弃的那个目录。

A01（重启）：

- 重启前标识存在；重启后标识字节不变、路径不变；
- 重启后第二次选择仍能到达授权表（grant 计数按绝对路径核对）。

## 环境

macOS 26.4（darwin 25.4.0），Electron 由 `desktop/node_modules` 提供，
Playwright 由本机解析（`run-remote-workbench.mjs` 的解析顺序），
fixture 为 `desktop/e2e/serve-fixture.py` 提供的真实 HTTPS console + 私有身份库；
本地模式后端绑定 `http://localhost:9876`（应用自己的公告值，见 `index.ts`）。

## 顺带发现（既有、非本次引入，未顺手改）

`desktop/e2e/remote-workbench.spec.mjs`（上一 change 的 spec）在本机 **3/17 通过**。
两处都是 harness/spec 假设过期，不是本次改动引入：

1. `signInLocally()` 断言内置后端为 `http://127.0.0.1:<port>`；产品自
   `7d4cf3db` 起按 `announceLocalBackendOrigin('http://localhost:<port>')` 公告
   **localhost**（同一 commit 内的注释写明原因：与后端打印给用户的 console URL
   共用 cookie jar）。本次已把该断言改为接受任何 loopback 名，
   **该 spec 的第 1 个用例因此由失败转为通过**（`evidence` 见
   `remote-workbench-loopback-fix.md`）。
2. 修掉 (1) 之后，第 2 个用例失败于
   `<html class="platform-mac cow-remote-covering"> intercepts pointer events`
   —— 本地登录后应用会把本地 console 自动装入容器视图（`local-web-bind.ts`），
   容器 `TOP_INSET = 0` 全覆盖，shell 被 `pointer-events: none`
   （`index.css` 的 `html.cow-remote-covering`）。该 spec 之后的旅程假设
   "React shell 的 Settings 可点"，在容器已附着的本机状态下**真实用户也点不到**。
   这是该 spec 需要重写为"在容器内的 web console 里进远程设置"的事，
   属另一 change 的范围，本次不在未验证的情况下改动它，仅记录。
