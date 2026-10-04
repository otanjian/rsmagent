# SAP 工作台本机兼容基线

`compatibility.json` 固定本场景部署所期望的已观测版本：Chrome 154.0.8037.95、macOS 26.4、主 Python/aiohttp 3.14.3、OpenCode 源码 `0442518883` / Web 1.18.34、Bun 1.3.14，以及 MCP Python 3.10.0 / SDK 1.29.0 / AnyIO 4.14.2。这是既有测试的版本基线；没有冻结或修改用户的系统 Chrome，也不接管它的自动升级。版本漂移需复核并在恢复现场测试后重新验收，不能自动扩大已测兼容范围。

从本项目根目录运行只读报告：

```sh
.venv/bin/python -B -m Scene.sap_workbench.backend.compatibility
```

命令读取 Chrome `Info.plist`、macOS `SystemVersion.plist`、主 Python 版本与包元数据、Bun Homebrew 安装元数据、OpenCode Git `HEAD`/refs 和 Web `package.json`、MCP venv 的 `pyvenv.cfg` 与包元数据。它不启动 Chrome、Bun、OpenCode、MCP 或浏览器驱动，不发送 HTTP/模型/SAP 请求，不创建目录、数据库或配置。`-B` 避免命令自身写 Python 字节码。

输出 `matches`、`drift` 或 `unknown`，分别代表本机元数据与基线一致、不一致或无法离线读到。十项均匹配时退出码为 0；存在漂移/缺失元数据时为 1；基线格式错误时为 2。该报告未接入运行时自动阻断，也不会升级或降级任何组件。它与 `backend.environment` 的运行准备检查相互补充：版本匹配不能代替执行文件、目录、Web 入口资源和连接检查。

可指定 `--chrome`、`--system-plist`、`--opencode-root`、`--bun`、`--mcp-venv` 和 `--baseline` 的本机文件路径。默认遵循 `SAP_WORKBENCH_CHROME`、`SAP_OPENCODE_ROOT`、`SAP_MCP_PYTHON`；Bun 通过当前 `PATH` 查找。MCP 读取 venv 文件，不执行其解释器或导入 SDK。OpenCode 支持仓库子目录、普通 `.git`、链接工作树和 packed refs；读取提交号不会核对未提交文件，仍需单独核对源码工作区。Bun 仅识别 Homebrew 安装元数据，独立复制的二进制报告 `unknown`，不会通过执行二进制推测版本。

本机历史 SAP 页面显示 SAP NetWeaver 758、S4H / Client 200、中文和“原始屏幕”。“原始屏幕”是观察到的页面标签，精确主题标识与内核 patch 未核实。已有证据覆盖 SPRO → SAP 参考 IMG / SIMG，以及 ME21N 导航、日期往返、供应商 F4、首行短文本/数量填写和错误回读，均停在保存前；基线不含账号、密码、访问令牌或单据业务正文。

SSO 与重定向域、远程显示/noVNC、全部交易与控件（含完整表格分页）、完整有效单据与提交、双真实 SAP 用户及代表性负载、桌面登录后的双栏均未验收。当前画面采用场景内 Chrome CDP 截屏/输入桥，没有单独 WebDriver/VNC 发布版本可冻结。只读报告不会重新连接 SAP 来确认当前服务版本。文件固定部署期望值并明确漂移，不能据此将原任务 1.5 或完整 G0–G4 勾选完成；用户暂停的实际操作检查继续暂停。

## 2026-10-03 离线结果

本次执行报告的十项元数据中九项匹配，一项漂移：历史 OpenCode 来源期望为 `0442518883`，当前包含该源码的 `rsmcode` 仓库 `HEAD` 为 `9acdb1d09ff4daed68550c2efb7ddb5250c6ab5b`，所以报告返回 1。Chrome、macOS、主 Python、aiohttp、Web package 版本、Bun、MCP Python/SDK/AnyIO 均与记录一致。`rsmcode/opencode` 工作区仍干净；工作区干净不能代替来源版本匹配。

历史来源号与后续 Web 版本观察来自不同阶段：`git show 0442518883:opencode/packages/app/package.json` 的版本实际为 1.18.31，1.18.34 是后续版本观察。因此保留这些期望值用于发现变化，并设置 `complete_deployment_lock: false`；它们尚不能作为已证明一致、可完整还原同一次验收的发布包锁。不能把历史来源号与后续版本号拼接成一次完整兼容通过证据。

另外只读比较 Git 对象：历史提交的 `opencode` 子树为 `ca2f9f13c88d2a61c4ea5a52239dbfc95c2d9d2a`，当前为 `ffc44ac76180f59bfe7087eec1e9cb9925c0ff7d`，两者不同。这处漂移包含 OpenCode 源码变化，不能解释成仅仓库其他目录变化，也不能因为 Web package 仍显示 1.18.34 就认定来源一致。保留历史部署期望与用户当前源码，不执行 checkout、升级或服务重启；更新兼容基线需先复核新来源并补对应验收。

`tests/test_sap_workbench_compatibility.py` 的 **30 项隔离测试通过，0.24 秒**，覆盖元数据缺失、版本漂移、普通/packed/worktree Git 引用、独立 Bun 无元数据、格式错误和只读输出。此数量仅对应兼容报告测试，不含 SAP/OpenCode 现场测试。

## 场景 host 的静态接口复核

对 `opencode_adapter/host.ts` 的直接源码导入及动态节点清单共 26 个文件，逐项读取当前源码并与 `0442518883` 中的原始字节比较：全部存在、全部相同。`ApplicationTools.register`、`Tool.Context` 的 `sessionID/assistantMessageID/toolCallID`、`Config`/`ConfigMigrateV1`、`SessionExecutionLocal`、HTTP API/handlers、授权及位置中间件没有观察到签名或组合接缝变化。Effect、platform-node、sql-sqlite-bun 的 catalog 版本仍为 4.0.0-beta.83。

当前 core runner 的 LLM 改动新增 `x-opencode-session-id` 及 parent header；场景模型桥继续只验证自己的 Bearer，目标身份来自绑定，不解析这些新增 header，所以没有发现必须补丁适配的接口变更。旧 provider 路径的超时、Cloudflare 和 Gemini 参数变化没有提供当前 SAP canonical host 已断裂的证据。此复核只读 Git/源码/元数据，没有执行 Bun、启动 host、连接模型、SAP 或 MCP；没有据此升级部署期望或宣称新来源运行兼容已验收。当前不存在明确必须修改的外部源码或场景接口，来源漂移仍需后续验收。

## 2026-10-03 独立 Linux 显示部署准备

新增 [独立浏览器部署说明](BROWSER.md)、`browser.Dockerfile`、`browser-compose.yaml` 及独立监督/健康检查资源，沿原设计提供有头 Chrome、Xvfb、真实 RFB/WebSocket 桥和 noVNC。新的 Linux 版本锁与本页历史本机兼容基线分别记录，不修改系统 Chrome 或主服务启动要求。

首次检查时 Colima / daemon 未运行；随后根代理沿既有 profile 启动 daemon，并以显式 `TARGETARCH=arm64` 的 legacy builder 真正尝试构建，首个固定 Debian 镜像的 Docker Hub HEAD 请求返回 EOF，尚未进入 APT/Chrome。本单元已通过 42 项隔离测试与 Compose 配置解析，并实际校验 noVNC/websockify 上游源码资源；成功构建、容器运行及工作台绑定仍未验收，任务 4.1 保留未完成。本页前述“用户暂停”与现场范围是各阶段历史记录；最新继续执行授权不将历史未测事项自动改成通过。
