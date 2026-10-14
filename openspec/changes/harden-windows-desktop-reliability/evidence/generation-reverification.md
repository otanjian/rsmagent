# 阶段 9 证据：当前构建复验文档代际守卫（任务 9.2）

日期：2026-10-13。任务 9.1 已由上游实现并勾选（`container.ts` 去掉 `did-finish-load`、只保留 `did-navigate`），其结论明写"重构建后需复跑一次把证据重新绑到当前代码"。9.2 要求的正是这次**不依赖历史证据**的复验，并额外要求"保留 preload 握手不自动重握手绕过守卫"。

## 1. 本次复验绑定的代码

| 项 | 值 |
|---|---|
| 分支 / HEAD | `rdai` / 验证时 **`05b32012daf5a5362fad8606451e2e42ba69e2cc`**；本 change 的实施提交为 **`5a011e25`**（含本文件更新，实施与证据一次性提交） |
| 工作树 | 含本 change 的实现改动（未提交）。**注意**：本 change 提交后应把此处 SHA 更新为提交号，否则证据绑定会漂移 |
| 关键文件 | `desktop/src/main/remote/container.ts:145-161`（代际只由 `did-navigate` 推进）、`desktop/src/main/remote/host-bridge.ts:243-246`（`stale_context` 两处拒绝）、`desktop/src/main/remote-preload.ts:37-49`（握手一次、失败为 0 代）、`desktop/src/main/remote/remote-host-ipc.ts:247-254`（`handshake` 先过 `checkSender`） |

## 2. 复验结果（当前构建实跑）

| 要求 | 证据 | 结果 |
|---|---|---|
| **一次加载只推进一代；加载完成不算新文档** | `tests/test_desktop_remote_host.cjs` → "a bridge handshake during startup survives load completion but not document replacement"：`did-navigate` 后 `checkSender` 通过 → `did-finish-load` 后**仍通过**（不换代）→ `did-navigate-in-page` 后仍通过 → 再次 `did-navigate` 后旧调用者被拒 `stale_context` | **34 / 0** |
| **旧文档拒绝** | 同上（第二次 `did-navigate` 后 `code === 'stale_context'`）；另 `tests/test_desktop_project_*.cjs` 共 20+ 处 `stale_context` 断言 | 通过 |
| **帧错误** | `host-bridge.checkSender`：帧不是登记的 shell 主帧 → `stale_context('the calling document is not the registered shell document')` | 代码 + 上述测试 |
| **代际错误** | 同上：`sender.generation !== context.generation` → `stale_context('the calling document belongs to an earlier generation')` | 代码 + 上述测试 |
| **连续调用（同一代内多次 bridge 调用）** | `tests/test_desktop_local_read.cjs` **20/0**、`tests/test_desktop_local_files_bridge.cjs` **6/0**（同一文档内反复调用预览/动作） | 通过 |
| **preload 握手不自动重握手（本次补的缺口）** | 新增 `tests/test_desktop_preload_handshake.cjs` **4/4**：握手 promise 记忆化（`if (!handshake)`，一次 `CHANNEL_HELLO`）；preload 内无 retry/backoff 词汇、无 `setTimeout`/`setInterval`；失败或被拒 → `generation = 0`；每次调用都带 `generation`；主进程 `handshake` 先跑 `checkSender` 才回答代际 | 通过 |

**为什么 preload 这一条必须单独测**：`checkSender` 的守卫只有在"被拒的文档不能自己反复重试直到被接受"时才有意义。此前没有任何测试钉住这一点——若有人日后给 preload 加一句重试，守卫就会被绕过而所有既有测试仍然全绿。

## 3. 未覆盖（如实登记）

- 9.1 证据里那支**真实 CDP 探针**（启动 Electron → 容器页 `http://localhost:9899/chat` → 经 CDP 调 `window.desktopHost.getCapabilities()` 两次）**本次没有重跑**：它需要同时运行桌面应用与服务器页面。本次以"当前构建的等价测试"覆盖同一序列（`did-navigate` / `did-finish-load` / `did-navigate-in-page` / 二次 `did-navigate`），并在上表给出结果；真机复跑探针可作为 6.4 真机验收的一部分。
- `tests/test_desktop_isolated_preview.cjs` **23 / 4**：4 项失败此前已用基线判定为**本机环境**（1 项 `EPERM: symlink`（无符号链接权限）+ 3 项多行 `\n` 正则对 CRLF 检出文件不匹配；原版代码同样 23/4）。

## 4. 判定

**9.2 勾选**：当前构建已复验连续调用、旧文档拒绝与帧/代际错误（复用既有测试，34/0），补齐了此前无人覆盖的 preload 不重握手守卫（4/4），并记录了所绑定的 SHA 与"提交后需更新"的注意事项。
