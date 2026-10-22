# fs-guard 平台与路径支持说明

本文件说明 `desktop/native/fs-guard` 这个 helper 在**当前已构建的两个平台**上如何工作、实测覆盖到哪里、以及哪些情况明确不支持。它对应变更 `add-windows-local-project-access` 的任务 2.2（"更新支持说明"）。

**读法约定**：下表的"已实测"只指**本文件写作时真实跑过的测试**。`cargo check --target` 只是**编译级**验证，不是运行级验证，单独标注。任何未在本文件写明的平台/存储组合都视为**未支持**。

## 1. 支持的平台与构建方式

| 平台 | 目标三元组 | 构建结果 | 验证级别 |
|---|---|---|---|
| Windows 10 22H2 / 11 x64 | `x86_64-pc-windows-gnu` | `fs-guard.exe`（1,090,560 字节，release、strip） | **运行级**：单元 45 + 集成 27 全过 |
| Linux / macOS | `x86_64-unknown-linux-gnu` 等 | 未在本机产出 | **编译级**：`cargo check` 通过，0 警告 |
| Linux / macOS | — | — | **运行级未验证**（无 POSIX 主机） |

选择 GNU 而非 MSVC target 的原因：MSVC 需要 Visual Studio Build Tools，而它需要管理员权限；GNU target 的 MinGW 链接器由 rustup 自带（`self-contained`），普通用户即可构建。这不改变 helper 的安全语义——两种 target 调用的是同一套 Win32/NT API。

依赖按平台声明，不共用：

```toml
[dependencies]                 # 平台无关
serde, serde_json

[target.'cfg(unix)'.dependencies]
libc

[target.'cfg(windows)'.dependencies]
windows-sys                    # Foundation / Security / Security_Cryptography /
                               # Storage_FileSystem / System_IO
```

写之前这些依赖是**无条件**的，所以 Windows 构建会以 51 个错误失败而不是明确报错；平台表就是修这个问题的地方。

## 2. 根锚定语义（两端一致，实现不同）

授权的根在**原生选择**时打开并持续持有。此后每个请求的相对路径都**逐组件**打开，且拒绝 reparse point。

| | unix | Windows |
|---|---|---|
| 相对打开 | `openat(parent_fd, name, O_NOFOLLOW)` | `NtCreateFile` + `OBJECT_ATTRIBUTES.RootDirectory` |
| 打开绝对根 | `open(path, O_NOFOLLOW)` | `NtCreateFile`，路径转成 `\??\C:\...` 形式 |
| 拒绝链接 | `O_NOFOLLOW` → `ELOOP` | `FILE_OPEN_REPARSE_POINT` + 读回属性判定 |
| 目录枚举 | `fdopendir` + `readdir` | `NtQueryDirectoryFile`（`RestartScan`） |
| 按名 stat | `fstatat(..., AT_SYMLINK_NOFOLLOW)` | 相对打开该条目并读回属性 |
| 身份 | `(st_dev, st_ino)` | `(卷序列号, 文件索引)` |

**两个容易踩的 NT 细节**（都靠实测发现，不是推断）：

- `NtCreateFile` **不接受 DOS 路径**。`C:\dir` 返回 `STATUS_OBJECT_PATH_SYNTAX_BAD`，必须写成 `\??\C:\dir`。
- NT 的相对打开**拒绝 `"."` 作为名字**（`STATUS_OBJECT_NAME_INVALID`）。所以 Windows 侧"重新打开目录以取得独立枚举位置"不能照搬 unix 的 `openat(parent, ".")`，改为按已存根路径绝对打开**并核对句柄身份是否与授权时一致**——路径只用于到达对象，不用于授权它。

`GetFileInformationByHandleEx(FileIdBothDirectoryInfo)` 的枚举位置**不可靠**（换新句柄后第二次枚举仍可能为空），因此枚举改用 `NtQueryDirectoryFile` 并显式 `RestartScan`。另注意 `FileIdBothDirectoryInfo = 10` 是 `GetFileInformationByHandleEx` 的枚举，**不能**用作 `NtQueryDirectoryFile` 的信息类（会得 `STATUS_INVALID_INFO_CLASS`）；后者用的是 `FileBothDirectoryInformation = 3`。

## 3. 路径形态支持情况

| 形态 | 结果 | 验证 |
|---|---|---|
| 普通相对路径 `a/b.txt` | 支持 | 单元测试 |
| 中文与空格 `资料/明细 2024.csv`、`我的 项目 空格` | 支持；列出的名字可原样再打开 | 集成测试 `non_ascii_and_spaced_names_round_trip` |
| 大小写不同的名字 | 按卷语义：同对象或明确拒绝，二者之一 | 集成测试 `a_name_that_differs_only_in_case_is_handled_consistently` |
| 深路径 + 长组件（12 层 + 120 字符组件） | 支持 | 集成测试 `a_deep_and_long_path_still_resolves` |
| 超过 `MAX_PATH_BYTES`(4096) 的路径 | 明确拒绝 `invalid_params`；**不截断** | 集成测试 `a_path_beyond_the_protocol_bound_is_refused_not_truncated` |
| **恰好** 4096 字节的路径 | 不因长度被拒（是真实名称查找） | 同上（边界对照） |
| 8.3 短名别名 | **不作为授权标识**。`FileBothDirectoryInformation` 记录里带 `ShortName`，可用于识别；当前策略是永不按别名解析 | 结构字段已就位，策略见设计 D2 |
| 绝对路径、盘符相对 `C:foo`、UNC `\\server\share`、`\\?\` 设备命名空间 | 拒绝 | 单元测试（两端各一份） |
| `..`、`.`、空组件、`/` 或 `\` 混入、`:` / ADS、控制字符、NUL | 拒绝 | 共用 `validate_relative_path` + 单元测试 |
| Windows 保留设备名 `con`/`nul`/`com1`… | 两端都拒绝（即使 unix 上合法） | 共用单元测试 |
| 符号链接（unix）、reparse point（Windows：symlink/junction/挂载点/云占位） | **拒绝**，不跟随 | 单元测试 + 集成测试 |
| 硬链接文件（`nlink > 1`） | 拒绝；列目录时计入 `skipped` | 集成测试 |
| 字符设备 / 块设备 / FIFO / socket（unix） | 拒绝 | 集成测试（unix-only，夹具需 `/dev/null`） |
| 非固定盘（网络盘、映射盘、可移动盘）作为根 | 拒绝 `unsupported` | 单元测试（Windows 走 `GetDriveTypeW`） |

## 4. 明确不支持

- **写入、重命名、删除**：只读入口 `fs-guard(.exe)` 的操作表是穷尽 `match`，未知 op 一律 `unknown_op`；没有任何请求字段能开启它们。
- **执行脚本 / shell / 加载项目代码**：helper 不派生任何 worker，不读取 argv。
- **非本地固定卷**：见上表。首期只支持本地 NTFS 普通目录；扩展存储类型需另做验收。
- **网络**：helper 不做任何网络调用。

## 5. 未验证 / 待补

- **POSIX 运行级回归未做**：本机无 POSIX 主机，unix 侧只到编译级。任务 2.1 里的"回归POSIX"目前是类型级通过。
- **`a_device_file_is_refused` 是 unix-only**：夹具要 `/dev/null`。Windows 的 reparse 拒绝由后端自身测试覆盖（真实建立 reparse point），**未跳过该保证**。
- **未在 Windows 10 22H2 实机跑过**：本次验证在 Windows 11 x64 上进行；22H2 属目标 OS 之一，仍需真机证据（属验收矩阵 W02–W08）。
- **安装包路径未验证**：helper 尚未随包分发（属任务 5.1）。
