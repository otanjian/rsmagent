# 本机项目：来源与数据流

对应变更 `add-windows-local-project-access` 的任务 4.4（"更新双语来源和数据流说明"）。本文件说明**一次本机读取或保存，数据从哪来、经过谁、到哪去**，以及每一跳的信任性质。

**读法约定**：每条链路标出**谁有权决定**以及**模型能看到什么**。凡是"模型不能指定"的地方都写明——那是这条链路的安全性质，不是实现细节。

界面文案的**双语**（简中 / 繁中 / 英文）不在本文件：它们集中在 `channel/web/static/js/i18n/core.js`，本机文件相关的键（`ws_local_*`）三语齐备，并由 `tests/fixtures/console_i18n_snapshot.json` 钉住。本文件只讲**数据**。

## 1. 两条独立链路

本机项目有两条**互不替代**的链路。把它们混为一谈是本文件存在的首要理由：

| | **模型链路** | **面板链路** |
|---|---|---|
| 谁发起 | 模型（工具调用） | 用户（在编辑器里点保存） |
| 执行者 | `agent/desktop_local/worker.py`（Python，子进程） | `fs-guard` / `project-fs-guard`（Rust，原生） |
| 工具 | `read` `write` `edit` `ls` `search_files` `bash` | 只有读与写 |
| 授权 | 项目 grant 的 `operations` 集合 | 同一个 grant，但**不经过模型** |
| 版本校验 | 帧携带 `expected_version` 时写前阻断 | 面板读返回 `version`，保存时带回 |

**共同点**：两者的根目录都来自**原生选择器确认过的 grant**，模型与页面都**不能**指定目录。

## 2. 模型链路：工具调用 → 本机

```
模型工具调用（read/write/edit/ls/search_files/bash）
    │  arguments 由模型给出；目录不可指定
    ▼
LocalReadAssembly.runDeviceTool            desktop/src/main/remote/local-read-assembly.ts
    │  根目录来自 grant（不是帧里的字段）
    ▼
WorkerSession ──spawn──▶ script-launcher --wrap ──Job Object──▶ python worker
    │                            └── stdio 直通（不解析）──────┘
    ▼
worker.py 执行工具，cwd = 用户在原生选择器里确认的目录
```

**每一跳的性质**：

| 跳 | 谁决定 | 说明 |
|---|---|---|
| 模型 → `arguments` | 模型 | 只能给**项目相对路径**；绝对路径越界会被拒 |
| `runDeviceTool` 解析根 | **设备** | 根来自 grant 表，**帧里没有这个字段可填** |
| `script-launcher` | **设备** | 只做 Job 归属与资源上限，**不解析 stdio**——worker 的 JSON-RPC 协议原样通过 |
| `worker.py` | 设备 | cwd 固定为项目根；工具集是**静态白名单** |

**模型看不到的东西**：项目的绝对路径、设备标识、其他项目。

## 3. 面板链路：编辑器 → 本机

```
面板读（projectRead）
    │
    ▼
ProjectBrowser.read                         desktop/src/main/project-browser/browser.ts
    │  经 device-ops 发 read_text
    ▼
fs-guard read → { text, total, version }
    │  version = fgv1:<16 hex>，绑定 grant/相对路径/对象身份/size/mtime
    ▼
返回面板：{ path, text, size, mtime, version, editable }
    │
    ▼ 用户编辑
面板保存（projectWrite）
    │  带上读到的 version
    ▼
browser.write → 帧 arguments.expected_version + 帧顶层 expected_version
    ▼
project-fs-guard write
    └── 版本不匹配 → file_changed，**不写入**
```

**`version` 为什么必须由设备给**：它是**不透明**值，绑定**文件对象身份**，客户端**算不出来**——合同 `version_precondition` 明写 `note_not_checksum`。所以两次传递（读给出、写带回）不是便利，是**唯一可行**的方式。

**保存是比较并交换**，不是盲覆盖：文件在用户编辑期间被改动，保存会被拒而不是把别人的改动盖掉。

## 4. 写入之后：什么才算"产物"

一次写入**不等于**一个有引用的产物。`test_desktop_device_artifacts.cjs` 钉住的规则：

| 情况 | 是否发布引用 |
|---|---|
| 写入成功且文件真的在盘上 | **是**，且引用命名**真实文件** |
| `bash` 帧写了文件 | **否**——脚本副作用不是"这份产物" |
| 运行失败 | **否** |
| 写入后运行才失败 | **否** |
| 帧里的路径逃出项目 | **否**，且不命名任何文件 |
| 目标是目录 | **否** |

引用里的 `source version` 是**内容摘要**，不是文件名。

## 5. 本机文件进入服务端：只能显式上传

```
面板/模型 → materialize 命令（显式）
    │  必须带 transfer_id 或 approved version
    ▼
本机确认版本一致 → 分块上传 → 服务端 commit
    └── 版本不符 → file_changed，**不上传**
```

**读操作完全不碰上传通道**（`no read op touches the upload transport`）——**不存在默认整目录上传**。这是一条被测试钉住的性质，不是约定。

## 6. 撤权与失效

| 事件 | 结果 |
|---|---|
| 用户撤回 grant | 之后**任何** prepare 与 start 都拒绝（`grant_revoked`），不是只在 prepare |
| 重新选择目录 | grant 版本 +1，**旧帧成为 miss**，不是写进新目录 |
| 退出登录 / 换租户 | 只有该会话停止；其他独立作用域不受影响 |
| helper 不可用 | 能力块报 `feature_unavailable` + `helper_reason`，界面给**可操作的修复提示**（三语） |
| 桌面重启 | **旧的 grant 不复活**，必须重新选择 |

## 7. 隔离边界（当前状态，别读成已完成）

| 层 | 状态 |
|---|---|
| **Job Object**（进程树、资源上限、崩溃不残留） | **稳定可用**（实测 20/20），已接入 worker 启动 |
| **访问边界（AppContainer/LPAC）** | **可用但不稳定**（实测 13–16/20），且**尚未授予项目目录访问权**（那是任务 7.2） |

**两者不可替代**：Job 管进程树与资源，AppContainer 管"能碰到什么"。当前**脚本进程仍以用户自身权限运行**——这一点在 `docs/isolation-status.md` 里写得更细。

## 8. 未验证

- **Windows 目标 OS（Win10 22H2）**：本机只有 Win11。
- **真安装包**：本机产出过安装包并确认内含两个 helper，但未在干净目标 OS 上安装验证。
- **签名**：未签名，分发时有 SmartScreen 警告。
