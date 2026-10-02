# `remote-workbench.spec.mjs` 的 loopback 断言修正（既有缺陷，一行级）

本次改动只做了一件事：把上一 change 的 Electron E2E 里两处**过期**的 loopback 断言
放宽为"任何 loopback 名"。

```diff
-  assert.match(backendOrigin, /^http:\/\/127\.0\.0\.1:\d+$/,
+  assert.match(backendOrigin, /^http:\/\/(127\.0\.0\.1|localhost|\[::1\]):\d+$/,
...
-  assert.ok(backendOrigin.startsWith('http://127.0.0.1:'),
+  assert.ok(/^http:\/\/(127\.0\.0\.1|localhost|\[::1\]):/.test(backendOrigin),
```

## 为什么是修正 spec 而不是修正产品

产品**有意**公告 `localhost`：

- `desktop/src/main/index.ts` 在 `pythonBackend.on('ready')` 里
  `announceLocalBackendOrigin(\`http://localhost:${port}\`)`，同一处的注释写明原因 ——
  系统浏览器打开授权 URL 时，只有与后端打印给用户的 console URL 同名，cookie/session
  才在同一个浏览器 origin 内；
- 该行为与 spec 位于**同一个 commit**（`7d4cf3db`，本次基线），即 spec 的断言在当时
  就已与其同批的产品行为不一致。

断言要表达的命题是"内置后端是一个纯 loopback 源"，`localhost` 与 `127.0.0.1` 对该
命题等价，因此放宽而不是改产品。

## 效果（实测）

修掉之后，该 spec 的**第 1 个用例由失败转为通过**
（`a fresh profile boots local, runs the bundled backend and signs in through the
browser`，2.9 s）。完整跑分 **3 通过 / 14 失败**，原始输出见
`remote-workbench-spec-run.log`。

剩下的失败**不是**本次改动引入，也不是这一行的后续：根因是该 spec 的后续旅程假设
"本地登录后 React shell 的 Settings 仍可点"，而产品在本地登录后会
`tryAutoBindLocalWeb()` 把本地 console 装入容器视图（`remote/local-web-bind.ts`），
容器 `TOP_INSET = 0` 全覆盖且 `index.css` 的 `html.cow-remote-covering` 把 shell 设为
`pointer-events: none`（`shell-covering.ts` 的注释说明这是为了避免 shell 抢走
guest 的点击）。Playwright 的原话：

```
- <html lang="zh" class="platform-mac cow-remote-covering"> intercepts pointer events
```

即真实用户在该状态下同样点不到 shell 的按钮。要把该 spec 修好需要把它改写成
"在容器内的 web console 里进入远程设置"，属 `add-desktop-remote-web-workbench` /
`fix-desktop-local-context-and-tool-calls` 的后续，本次只在证据里记录，不顺手改。
