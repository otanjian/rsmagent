# 平台门不可用配置绕过——实证（2026-10-14，本机测试实例）

用户问"本地跑通后改服务器配置行不行"。**服务器/部署开关能改**（本机测试实例已把两个执行开关置 true，属任务 10.1 的"隔离合成环境用明确测试配置验证"），但**执行面的门不在配置上**。本文件用**未改动的产品代码**把这一点证死。

## 1. 契约层：平台 × 工具

`auth/desktop_contracts_v2`（读 `contracts/desktop/v2.json`）：

| tool | posix | win32 |
|---|---|---|
| read / write / edit / ls / search_files / bash | **True** | **False** |

```
platform_supported: posix=True  win32=False
launcher:           posix='posix-spawn-launcher-v1'  win32=''
```

契约原文（`v2.json` 的 platforms 注释）：*"the Windows launcher and probes are not accepted yet; v2 frames are refused with `platform_unsupported` rather than translated"*。

## 2. 即使 `available=True`（"其余全开"），能力块仍被平台门改回不可用

调用 `desktop_contracts_v2.execution_capability(platform=…, available=True, reason='available', files_write_verified=True, scripts_verified=True)`：

| platform | available | reason | tools | scripts_verified |
|---|---|---|---|---|
| **win32** | **False** | **`platform_unsupported`** | `[]` | False（契约钳制） |
| posix | True | `available` | 六个工具全有 | True |

代码依据：`desktop_contracts_v2.py:655-661`——"if available and not supported: available, reason = False, 'platform_unsupported'"，注释写明"绝不宣称本构建不具备的强制能力"。

## 3. 组合态（应用真正入口）+ 部署开关 ON

`integrations.desktop.execution_capability.execution_state(settings={两个开关:True}, platform='win32')`：

```
win32 + 开关ON -> available=False  reason=not_accepted
```

即：**部署开关已打开**，但声明 `accepted=False` 仍把它挡在前面（`availability()` 的优先级：`not_implemented` → `not_accepted` → `disabled_by_deployment`）。

## 4. 把"声明"临时置为已验收（仅内存演示，**未改产品代码**）

在内存里让 `_slice_availability()` 把 `not_accepted` 视为已验收，再调同一入口：

| platform | available | reason |
|---|---|---|
| **win32** | **False** | **`platform_unsupported`** |
| posix | True | `available` |

→ **"即使 accepted + configured 都打开，Windows 仍然不可用"** 成立；平台门由**客户端所在机器**（设备上报 `windows→win32`）决定，与服务器位置、部署开关无关。

## 5. 运行时（本机测试实例，开关 ON）真实上报

```
GET https://127.0.0.1:8443/api/desktop/meta
project_execution = {"available": false, "reason": "not_accepted", "platform": "win32",
                     "tools": [], "files_write_verified": false, "scripts_verified": false}
```

## 6. 结论

| 动作 | 能否让 Windows 跑执行 |
|---|---|
| 改服务器/部署配置（开关 ON） | ❌ 只能把原因从 `not_accepted` 推进到 `platform_unsupported` |
| 改 `contracts/desktop/v2.json` 的 `win32.supported` | ❌ 任务 8.2 禁止的"只改支持声明"；且 `launcher` 仍为空，契约不会宣称不存在的强制能力 |
| 让设备谎报平台 | ❌ 同样是伪造支持声明；且 POSIX 语法在 Windows shell 上不是用户批准过的命令 |
| **实现 Windows 执行器 + 探针并验收**（新 change，已记录于 `deferred-windows-execution-change.md`） | ✅ 唯一正路 |
| **换 POSIX 执行端（Mac/Linux/CI）** | ✅ 立刻可跑（change 原话"POSIX 按真实目标验收"） |

复现：`python %TEMP%\cow-pack\platform-gate-demo.py`
