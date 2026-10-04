# 验收与回归证据（`fix-desktop-relogin-session-sync`）

本文记录第 4 阶段（真实客户端验收）的实际执行结果，包括**未通过 / 未覆盖**的部分。
第 5 阶段的真实模型、设备网关及报表内容验证见 [所选本地目录读取复测](selected-source-evidence.md)；本页直接读取结果不代表当时已覆盖模型到网关的完整链路。
所有数值均来自本机实跑，无 mock：Electron 为 `desktop/node_modules` 提供的运行时，
应用为 `desktop/dist/main`（本次构建），后端为同一次构建的 Python 后端，
容器页面为后端自带的真实 Web 控制台。

## 环境

- macOS 26.4（darwin 25.4.0），Electron 由 `desktop/node_modules` 提供。
- 运行入口：`node desktop/e2e/run-remote-workbench.mjs --spec <spec>`。
- profile、后端数据目录、选择用目录均在运行时的私有临时目录内，
  与本机已安装的实例（`~/.cow`、仓库根 `identity.db`）完全隔离。
- 本地模式后端绑定 `http://localhost:9876`（应用自身公告值，`index.ts` 的
  `announceLocalBackendOrigin`）。

## 4.1 本地模式：退出 → 重登 → 重选目录（通过）

`desktop/e2e/relogin-session-sync.spec.mjs`，6/6 通过：

```
[e2e] fs-guard helper: .../desktop/native/fs-guard/target/release/fs-guard
✔ R01: a container that just signed in can pick a directory and really read it (4972ms)
✔ R02: the account menu ends both ends, and leaves no page that looks signed in (268ms)
✔ R03: signing in again picks a new directory, and the old authorization does not return (15355ms)
✔ R04: the recovery entry ends the account, and a password submit there cannot mint a Web-only session (276ms)
✔ R05: after two sign-outs and two sign-ins the workbench is still usable (15336ms)
✔ R06: changing accounts does not carry the previous authorization over (15925ms)
ℹ tests 6  ℹ pass 6  ℹ fail 0
```

对应用户可见结论：

| 用例 | 断言的事（不是像素，是两端状态） |
| --- | --- |
| R01 | 选目录后**真的读到了字节**（`alpha-note\n`），且该页确实是容器（`CowDesktopAccount.isDesktop() === true`、`canSignOut() === true`） |
| R02 | 账号菜单退出后容器被销毁、授权表清空、broker `session === null` 且 `blockedReason` 为空；本地壳重新出现自己的登录入口——**没有任何页面还能显示“已登录”** |
| R03 | 重登后新选的目录**读的是新目录**（`beta-note\n`），退出前的授权没有回流 |
| R04 | 恢复入口真的结束账号；在容器内提交密码表单**不能**制造只有 Web 的会话（`phase` 停在 `unauthenticated`） |
| R05 | 两退两登后第三次会话仍可用（`gamma-note\n`），恢复路径不是单向门 |
| R06 | 换账号（root → 普通成员）不继承上一账号的授权；新账号读自己的目录；新账号退出仍然可用 |

`R01` 的读取路径此前在本 harness 下会被拒为 `the local file helper is not available`
（`app.getAppPath()` 在 E2E 下是 `desktop/e2e`，应用的 helper 候选路径都不存在）。
这是 **harness 前置条件**，不是产品缺陷：已在本 change 内让 runner 用应用自身的
`COW_FS_GUARD` 覆盖解析 `desktop/native/fs-guard`，helper 缺失时 runner 直接报
“缺少前置条件”，而不是让每个 spec 把功能缺口误读成产品拒绝。

## 4.1 未覆盖部分（保持未完成）

- **远程模式**：退出后重新连接远端并读取目录，未在真实客户端验收。
- **退出失败重试**：真实客户端未走“服务端不确认 → 壳提示重试退出 → 重试成功”的完整链路；
  该分支由自动化用例覆盖（见下方）。
- 无权限账号在**绑定时**遇到的“具体权限拒绝（区别于 `not signed in`）”未在真实客户端验收。

## 4.2 无 `client_files` 授权与 feature flag（未覆盖）

本次验收未构造“无 `client_files` 授权”的账号去触发具体权限拒绝，也未在关闭本地文件
feature flag 的条件下跑退出/登录；两项保持未完成。

已确认并保持的边界：验收未修改任何真实租户、角色或权限配置，也未扩大切片开放状态；
验收用的第二个账号只持有与 root 相同的 agent 访问，未新增权限。

## 3.2 自动化回归（通过）

```
.venv/bin/python -m pytest tests/test_desktop_auth_flow.py tests/test_desktop_web_session.py \
  tests/test_desktop_local_context.py tests/test_desktop_execution_broker.py \
  tests/test_desktop_external_broker.py -q -p no:randomly
→ 146 passed in 60.62s

node --test tests/test_desktop_host_frontend.cjs      → 21 passed
node --test tests/test_desktop_context_frontend.cjs   → 14 passed
node --test tests/test_desktop_remote_config.cjs      → 39 passed
node --test tests/test_desktop_external_broker.cjs    →  7 passed
node --test tests/test_desktop_signout.cjs            →  9 passed
node --test tests/test_desktop_directory_identity.cjs →  9 passed
node --test tests/test_desktop_account_frontend.cjs   →  5 passed
```

其中 `test_desktop_signout.cjs` 覆盖退出单飞与失败阻断；`test_desktop_external_broker.cjs`
覆盖“状态未确认时问会话本身”；`test_desktop_web_session.py` 覆盖配对退出后 403 cross_origin
仍可从 `GET /auth/me` 得到 401 的线路行为。

## 相邻 E2E（同一次 harness，暴露既有问题）

- `desktop/e2e/directory-selection.spec.mjs` → **6/6 通过**（同一 fixture、同一 helper、同一构建）。
- `desktop/e2e/remote-workbench.spec.mjs` → **2/17 通过、15/17 失败**，且**非本 change 引入**。
  运行输出与本仓已归档记录一致：

  ```
  Error: timed out waiting for the onboarding wizard to be gone (last: false)
  ...
  <html lang="zh" class="platform-mac cow-remote-covering"> intercepts pointer events
  ```

  根因：本地登录后应用会把本地 console 自动装入容器视图
  （`auth-broker.ts` 的 `desktop-auth-begin` → `tryAutoBindLocalWeb`），容器 `TOP_INSET = 0`
  全覆盖，`index.css` 的 `html.cow-remote-covering` 让 shell `pointer-events: none`。
  于是：

  1. 第 1 个用例点不到 shell 的“跳过”，向导文案仍在 DOM 里（`第 1 / 2 步` 被
     `document.body.innerText` 读到），等待超时；
  2. 第 2 个用例要在 shell 的 Settings 里进连接设置，点击被 covering 层拦截
     （`locator.click: Timeout 30000ms exceeded`，日志明确写出 intercepts pointer events）；
  3. 其余 13 个用例全部是**级联失败**：壳从未进入远程模式，于是
     “the attach must have created a live link”“no live child link to revoke”
     “timed out waiting for the remote-mode boot line / first streamed token / tenant switch
     to commit / upload to be stored” 依次出现。

  没有任何一条失败与退出、登录、会话同步、账号或本地读取相关。该结论与
  `openspec/changes/archive/2026-10-01-align-desktop-project-execution-with-master/evidence/desktop-e2e-a01-a02.md`
  （第 105–110 行）一致：这是该 spec 需要改写为“在容器内的 web console 里进远程设置”的范围，
  本 change 不在未验证的情况下改动它。该失败依赖自动装载的时序（归档记录当时为 3/17），
  属既有且不稳定的问题，不是本次改动引入。

  同一 harness 下的 `directory-selection.spec.mjs` 6/6 通过，说明 fixture、fs-guard 注入与
  新种的第二个账号都没有破坏既有旅程。

## 本次 harness 改动（均为可复现性，不改变产品行为）

1. `run-remote-workbench.mjs`：启动时先清理上次残留的 Electron 进程。
   残留应用会占住内置后端的固定端口，使**本次**应用连到上一次的进程上——症状是“产品在退出时卡住”，
   而实际是脏环境；只在 `after()` 清理已经晚了一轮。
2. `run-remote-workbench.mjs`：解析并以 `COW_FS_GUARD` 注入 `fs-guard` helper（见上）。
3. `serve-fixture.py`：本地身份库额外种一个普通成员账号，供“换账号”用例使用；
   不新增权限、不改变 root 账号。

## 数据与安全

- 不涉及数据库迁移：身份库表结构、AuthSession 与配对关系均未变化。
- 未记录 token、Cookie、用户 ID 或任何真实账号信息；本文所有账号均为 fixture 在
  私有临时目录中现场生成。
- 回滚：桌面客户端与随包 Web 控制台是同一次构建，整体替换回上一版本即可；
  回滚后按 [升级指南](/zh/guide/upgrade) 的既有方式操作，无需数据迁移或清理。
