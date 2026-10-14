# 阶段 4 证据：平台/helper 原因与 workspace 错误映射（任务 4.2、4.3）

日期：2026-10-13（Windows x64 / Python 3.12.13 / TypeScript 主进程 tsc 干净）。对应规范：`specs/scoped-project-browser/spec.md` 第 51、57–59、67、70–75 行；设计 `design.md` 第 69–71 行（D5）。

## 1. 问题（规范禁止的那一条）

规范第 59 行：**"读取入口关闭并提示修复安装或构建组件，不将设备在线状态误报为离线"**；第 67 行：**"未知设备错误 SHALL 显示一般失败和诊断标识，MUST NOT 默认等同离线或身份失效"**。

现状三处（`channel/web/static/js/workspace.js`）都把两种不同情况折叠成同一句：

```js
if (code === 'device_offline' || code === 'device_error') return t('ws_local_offline');
```

而本机真正的失败原因是 helper 缺失：`desktop/src/main/remote/remote-container-ipc.ts` 在 `guardBinary` 为 `null` 时抛**裸 Error**（无 `code`），`project-browser/browser.ts` 于是归一为 `device_error`（`errorCode: (err as {code?})?.code || 'device_error'`），控制台显示「本机未连接，暂时无法读取或保存项目文件」——设备其实在线、租约有效。`platform_unsupported` 在这三处**根本没有映射**，落到通用失败文案。

## 2. 改动

| 位置 | 改动 |
|---|---|
| `desktop/src/main/remote/remote-container-ipc.ts` | fs-guard 缺失的抛出改为**协商错误码**：`Object.assign(new Error(...), { code: 'helper_missing' })`。设备码由 `agent/tools/client_files/client_files.py` 原样透传（`command.get("error_code")`），因此能一路到达面板 |
| `channel/web/static/js/workspace.js` | 新增单一映射表 `WS_LOCAL_REFUSAL_KEYS` + `wsLocalRefusalText(code)`，**三处站点共用**（读取 `wsErrorMessage`、预览 `wsLocalPreviewMessage`、动作 `wsLocalActionMessage`）；`device_error` 与 `device_offline` 分开，新增 `helper_missing`、`platform_unsupported` |
| `channel/web/static/js/i18n/core.js` | 新增 `ws_local_device_failed` / `ws_local_helper_missing` / `ws_local_platform_unsupported`（简中、繁中、英文） |
| `tests/fixtures/console_i18n_snapshot.json` | 按"只增不改"登记上述三个键 × 三种语言 |

**一处过度统一（被既有测试抓住后收紧）**：`wsErrorMessage` 处理的是**工作区请求**，其中 `not_found` 指工作区资源，不是本机文件。首次统一时它也被表接管，`test_console_workspace_frontend.cjs` 立刻报 `expected 'boom'`——于是为这条路径加了更窄的 `WS_LOCAL_WORKSPACE_CODES`（仅 device/source/授权类码），`not_found` 保留服务器原文。这正是"多个分支共用**同一措辞**"与"各分支**各自判断哪些码属于本机**"的区别。

设计 D5 合规：沿用既有 `platform_unsupported` 拼写；helper 缺失用**协商错误码**（D5 明确允许"独立原因字段**或**协商错误码"）；`device_error` 保留一般失败语义；`device_offline` 只用于确实离线；**未新建能力快照、缓存或状态机**（4.2 的硬约束），也没有改动 `contracts/desktop/*`。

## 3. 验证（本机实跑）

`tests/test_workspace_local_refusals.cjs`（新增，**6/6 通过**）：

1. 三条解码路径确实共用同一张表（且折叠写法已消失）；
2. `device_offline` / `device_error` / `helper_missing` / `platform_unsupported` **四者文案互不相同**；
3. 只有真离线用离线句（其余三者不得复用）；
4. 设备侧 helper 缺失带码（`code: 'helper_missing'`），不是裸 Error；
5. 表中每个键在三种语言里都存在；
6. helper 与平台文案**必须点名组件并要求安装/重建**，且**不得出现"重新连接/reconnect"**。

其余：

| 检查 | 结果 |
|---|---|
| `desktop` 主进程类型检查（`tsc -p tsconfig.main.json --noEmit`） | **exit 0** |
| `tests/test_desktop_local_files.cjs` / `_bridge` / `_read` / `test_desktop_remote_host.cjs` | 12 / 6 / 20 / 34 全通过，0 失败 |
| `tests/test_desktop_execution_v2_contract.py` | **63 passed** |
| `contracts/desktop/v2.json` | `win32.supported = false` **未变**（第 20 行）；bash 在 win32 上仍以 `unsupported_platform (422)` 拒绝 |
| `tests/test_console_i18n_parity.cjs` | 与改动前**同为 3 项既有失败**（`coding_unavailable` 文案漂移等，见 `memory-error-classification.md` 第 4 节），未新增 |

### 3.1 受影响的既有测试（跟随共享表更新）

改动把三处映射收进一张表，两个用 `vm` **只抽取单个函数**的测试因此需要同步（否则只会抛 `wsLocalRefusalText is not defined`）：

- `tests/test_desktop_project_native_actions.cjs`：沙箱增加 `extractRefusalTable(workspaceJs)`（新助手，抽取表 + 查表函数）；
- `tests/test_desktop_isolated_preview.cjs`：三条"内联 return"断言改为对内联的**共享表**断言（同一意图，改的是位置）。

最终计数与**基线对照**（把 `workspace.js`、`remote-container-ipc.ts` 与上述两个测试文件全部换回 `origin/rdai` 原版再跑）：

| 测试 | 我的版本 | 原版基线 | 残留失败性质 |
|---|---|---|---|
| `test_console_workspace_frontend.cjs` | 34 / **0** | 33 / 1 | —（原版 1 项为既有） |
| `test_desktop_project_native_actions.cjs` | 38 / **2** | 38 / **2** | `EPERM: symlink`（本会话无创建符号链接权限） |
| `test_desktop_isolated_preview.cjs` | 23 / **4** | 23 / **4** | 1 项 `EPERM: symlink`；3 项多行 `\n` 正则对 CRLF 文件不匹配（`host-bridge.ts`、`remote-container-ipc.ts`、`workspace.js` 在本机检出为 CRLF，其中 `host-bridge.ts` 本次未改） |
| `test_workspace_local_refusals.cjs`（新增） | **6 / 0** | 不适用 | — |
| 其余（delete/upload/sap/project_refresh/local_files/local_read/remote_host） | 全绿，0 失败 | — | — |

即：残留失败**在两版上完全一致**，属本机环境（符号链接权限、CRLF 检出），不是本 change 引入。

## 4. 任务 4.1 / 4.4 的核对结果（本机可得部分）

- **4.1**：`win32.supported=false` 未变、契约套件 63 项通过（"保持 Windows 未支持"成立）；能力块的原因优先级在 `execution_capability.execution_state` 中保持原样（先声明/开关、后平台），本次未改其逻辑，也未新增第二套规范。
- **4.4**：本次已交付的部分是"双语文案 + Windows 不提示重登/重连"（由第 3 节第 6 项测试守住）；**未验证**的是真实面板上"按能力关闭新增入口、保留已有绑定原因/清除/云端切换"的交互行为，以及旧响应回退——需要真实 Windows/macOS 客户端与页面会话（归阶段 7/11 的真实环境验收）。
- 因此 **4.1 / 4.4 不勾选**；4.2 / 4.3 勾选。

## 5. 未验证

- 真实浏览器里四段新文案的渲染（需要登录会话与页面）：本机验证到"码 → 键 → 三语文案"的契约层。
- 真实 `helper_missing` 端到端路径（需要一个未随包分发 fs-guard 的构建）：本机以源码级契约 + 类型检查覆盖；`evidence/local-read-windows.md` 中"仍属推断的一环（原始异常文本未被捕获）"已由本次的协商码消除推断成分。
