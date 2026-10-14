# 6.4 真实服务器半段验证（2026-10-14，打包版 2.2.0 → `rd.rsmxm.com.cn`）

用户已在其真实服务器上登录，并授权执行"发 1 条测试消息 + 1 个合成附件"。以下为实测，**结论不夸大**：聊天与流式成立，附件上传成立，但**附件内容回读未成立**（原因是部署环境的文档安全层，见 §3）。

## 1. 环境与链路事实

| 项 | 实测 |
|---|---|
| 客户端 | **安装包 `容大AI-Setup-2.2.0-x64.exe`**（打包版），窗口 `RongAI`，应用 pid **15652** |
| 包内后端 | `cowagent-backend.exe` pid **21028**（父=应用），监听 **127.0.0.1:9876**；`~/.cow/run.log`：`Local access: http://localhost:9876`、`Listening on 127.0.0.1 only` |
| 模式 | `desktop-remote.json`：`mode=remote`，profile `srv_mv7p61im` → `https://rd.rsmxm.com.cn`；宿主日志 `[remote] local web auto-bound to https://rd.rsmxm.com.cn` |
| 探测 | `GET /api/desktop/meta` → **200**，`remote_web = {implemented,accepted,configured,available} = true`；`project_execution.available=false, reason="not_accepted"` |
| TLS | **TLSv1.3**，CN `rd.rsmxm.com.cn`，签发 **Let's Encrypt / YE1**，有效至 **2026-12-15**，1 SAN → 客户端接受该证书，容器页正常加载（6.5"适用 CA"路径的真实域名正面样本） |
| 设备通道 | `GET/POST(Upgrade) /api/desktop/connect` → **404**；宿主日志每 ~30s `[WARN] [desktop] device connection reconnecting: 1006:handshake_failed`（本次会话累计 44 次）。**本次聊天/附件全程不依赖该通道**，故仍可完成下述验证 |
| `Illegal invocation` | 0 处 |

## 2. 聊天与流式（成立）

- 在容器页（ASTER 控制台，登录 `me23n`）**新建会话**，向 `#chat-input` 写入并点击 composer 的 `fa-paper-plane` 发送键（注意：**仅派发 Enter 事件不触发发送**，必须点该按钮——已记录为坑）。
- 发送的合成消息：`【桌面端验收测试，可删除】请读取我上传的附件，并原样回复其中的 marker 值（形如 COW-...）。`
- **流式证据**（页面文本长度随时间增长，同一会话内）：t=0.0s **1929** → 12.2s 1946 → 16.2s 1957 → 20.2s 1962 → 24.2s 1968 → **28.3s 2110**（随后稳定）。
- 回复中可见**真实工具调用**：`read 0.04s | bash 0.12s | read 0.04s | bash 0.32s`（两次尝试分别 14:24、14:25），并给出结论文案（见 §3）。

## 3. 合成附件：上传成立，**内容回读未成立**

| 环节 | 结果 |
|---|---|
| 生成 | `%TEMP%\cow-pack\desktop-acceptance-probe.xlsx`（openpyxl 生成，4,999 B，`B2 = COW-A01-2F8A1C`） |
| 挂载 | 经 CDP `DOM.setFileInputFiles` 挂到 `#file-input`（accept 含 `.xlsx`），UI 出现 `desktop-acceptance-probe.xlsx` ✓ |
| 上传 | ✓ 会话中出现该附件；服务器侧文件名 `web_645459ba.xlsx`；工作区路径含 `/agents/solution-29178a6213b1b286bcb06340/user/usr_hcWvTEbXtYDWq1ho`、`uploads`、`work`、`desktop:J_ugyp9mx1Z6OTwDTjPzCw` |
| **回读** | ❌ 助手的 `read` 读到的文件头是 **`%TSD-Header-###%`**，其结论为"这是 TSD 系统的加密表格文件，无法直接解析，marker 值在文件元数据中可能被隐藏/加密"。两次尝试（14:24、14:25）均如此 |

**定性**：我在本仓库全量搜索 `TSD-Header`/`%TSD`，**没有任何命中** → 该头**不是本产品**的加密格式，而来自**该部署环境的文档安全/加密层**（上传即被透明加密落盘）。因此：
- 本 change 不因此判定为缺陷；
- 但"**合成附件上传回读**"这一验收点在**当前这份部署**上**不能判为通过**——需要平台侧解密路径（或改用该环境支持的附件类型/入口）后重测。**我未把它记作通过。**

## 4. 对 6.4 的净结论

| 6.4 要求 | 状态 |
|---|---|
| 真实 Windows x64 安装包经 HTTPS 服务器完成**探测** | ✅（`meta` 200，含 `remote_web` 全绿） |
| **浏览器确认/配对** | ✅ 部分：容器页加载并已登录（`me23n`）；**设备通道配对不可用**（`/api/desktop/connect` 404），故"配对"作为设备侧步骤未成立 |
| **聊天、流式回复** | ✅ 成立（含真实工具调用） |
| **合成附件上传** | ✅ 成立 |
| **附件回读** | ❌ 未成立（环境文档安全层加密，见 §3） |
| 记录版本、代理与 PID | ✅ 2.2.0 打包版 / pid 15652 / 后端 21028 |
| **证明无本机 Python** | ✅ **冷启动已复验**（见 `a01-real-device-selection.md` §2.1）：在 `mode=remote` 已配置的前提下重启打包版，**没有任何 `cowagent-backend` 进程** → 规范"remote 模式不启动本机 Python 业务后端"在冷启动条件下成立。附带说明：本次会话早期出现过的 9876 后端属于**先本地启动、后由用户切到远程**的另一条路径，由 `local-web-bind.ts`（"Auto-bind the in-process local backend into the remote Web container"）设计承载，不是违规 |

## 5. 复现要点（含坑）

1. `容大AI.exe --remote-debugging-port=9228`；容器页 target = `https://rd.rsmxm.com.cn/`，应用自身页 = `file://…/app.asar/dist/renderer/index.html`。
2. 发送消息：**必须点 `.composer-card` 内 `i.fa-paper-plane` 的按钮**（派发 Enter 无效，且输入框会被清空造成"看似已发"的假象）；发送后可用 `performance.getEntriesByType('resource')` 校验是否真的发出（只有 `/poll`、`/api/scheduler/runs`、`/api/todos/summary` 等后台轮询＝没发出）。
3. 附件：`DOM.setFileInputFiles` 指向 `#file-input`（页面有 8 个 file input，聊天用的就是它）。
4. 流式观测：每 2–4 秒采样 `document.body.innerText.length`，持续增长即为流式。
