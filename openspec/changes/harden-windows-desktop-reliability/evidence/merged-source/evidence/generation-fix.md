# 文档代际去重的验证（tasks 2.1）

> **2026-10-09 更新（拉取 `gitea/rdai` 之后）**：同一缺陷上游已修，采用 design「方案与取舍」中记录的**备选**写法——直接去掉 `did-finish-load` 监听、只保留 `did-navigate`（`desktop/src/main/remote/container.ts:160-161`）。本 change 不再自行实现；本文件记录的探针是在**本地上游化之前**的等价实现（按帧标识去重、保留两个监听）上运行的，两者对"一次加载只推进一代"的效果一致。重构建后需复跑一次探针，把证据重新绑到当前代码。

## 结论

`desktop/src/main/remote/container.ts` 的 `registerDocument` 去重已生效：**一次加载只推进一代**，且同一文档在加载完成后的多次 bridge 调用都成功——不再被 `host-bridge.ts` 的 `stale_context` 拒绝。

## 方法

桌面不可脚本点击，但它的容器页可经 Electron 远程调试（CDP）驱动：

1. 以远程调试端口启动：`./node_modules/electron/dist/electron.exe . --remote-debugging-port=9222`
2. `GET http://127.0.0.1:9222/json` 取容器页 target（`http://localhost:9899/chat`）
3. 经该 target 的 WebSocket 执行 `Runtime.evaluate`，在页面内调用 **preload 的** `window.desktopHost.getCapabilities()`——它每次调用都真实经过 IPC（页面侧 `CowDesktopHost.getCapabilities` 有记忆化，故不使用它），因此能暴露"加载完成后调用被判为旧代际"的故障。

探针脚本（一次性，置于系统临时目录）：

```js
const EXPR = `(async () => {
  const out = {};
  const fresh = async (label) => {
    try {
      const r = await window.desktopHost.getCapabilities();
      out[label + '_ok'] = true; out[label + '_generation'] = r && r.generation;
      out[label + '_localFiles'] = r && r.localFiles; out[label + '_methods'] = r && r.methods ? r.methods.length : null;
    } catch (e) { out[label + '_ok'] = false; out[label + '_code'] = e && e.code; out[label + '_message'] = String(e && e.message); }
  };
  await fresh('call1');
  await new Promise((r) => setTimeout(r, 1500));
  await fresh('call2');
  return JSON.stringify(out);
})()`
```

## 原始输出

两次 bridge 调用（间隔 1.5s）：

```json
{"call1_ok":true,"call1_generation":1,"call1_localFiles":true,"call1_methods":21,
 "call2_ok":true,"call2_generation":1,"call2_localFiles":true,"call2_methods":21}
```

入口可用性：

```json
{"methods":["getCapabilities","suspendLocalContext","saveArtifact","openExternal","signOut","onHostEvent",
 "bindContext","chooseWorkspace","disconnectWorkspace","localContext",
 "projectSource","projectTree","projectSearch","projectResolve","projectRead","projectWrite",
 "projectOpenFile","projectRevealFile","projectCopyPath","projectSaveFileAs","projectPreviewFile"],
 "hasChooseWorkspace":true,"localFiles":true,"generation":1,"canChooseWorkspace":true,
 "localFilesSnapshot":{"available":true,"reason":"","connected":null,"transfers":[],
 "note":"directories are never auto-uploaded; choose and bind per session"}}
```

## 判定

- `call1_ok` 与 `call2_ok` 均为 true，且都没有 `code=stale_context` / 消息 `the calling document belongs to an earlier generation`。**修复前**，`did-navigate` 与 `did-finish-load` 各加一次会把代际推到 2，而 preload 在两次事件之间只学一次（=1）且不刷新，于是第二次及之后的调用都会被拒——这正是此前点击「选择本机目录」所弹的错误。
- 两次调用读到的代际都是 `1` 且稳定，说明一次加载只推进一代。
- `canChooseWorkspace: true`、方法列表含 `chooseWorkspace`、`localFiles: true`、`localFilesSnapshot.reason` 为空，说明「选择本机目录」入口会被渲染且未被"未连接"阻断。
- 页面位于 `http://localhost:9899/chat`（相对入口 `/` 发生过一次导航），仍在同一代际内，符合预期。

## 本次未覆盖

- **守卫的拒绝侧**（导航替换后旧文档必须被拒）未在此探针中重跑；其既有覆盖是 `desktop/e2e/remote-workbench.spec.mjs:643-690`（切换租户 = 一次提交式导航，断言代际必须前进）。本机未安装 Playwright，未执行。
- **choose → bind 的完整序列**（含原生目录对话框与服务器侧绑定落库）需要人工选择本地目录，见 tasks 2.4；本轮只验证到"入口可用、调用不再被判旧代际"。
- `checkSender` 的单元测试（tasks 2.2）未新增：desktop 侧目前只有 `e2e/` spec，无单测 harness。
