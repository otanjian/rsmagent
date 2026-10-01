# 阶段二第 7 组证据：目录句柄 helper（`fs-guard`）

本文件记录**实际执行过**的验证，以及**未执行**项及原因。口径沿用 `evidence/phase-1.md`：
只写真实跑过的命令与观察到的结果，未执行的命令不记为通过。

**状态：部分执行。** 7.1 / 7.2（macOS）/ 7.4 / 7.5 / 7.7（macOS 探针）已实现并跑通；
7.3（Windows）、7.6（打包签名）未执行，7.7 的 Windows 半未执行。因此**分组 7 未整体通过**，
`local_files` 保持关闭。

## 1. 环境

| 项 | 实际值 |
| --- | --- |
| 仓库分支 | `rdai`（本 change 尚未提交，工作树含改动） |
| OS / 架构 | macOS **26.4** / **arm64** |
| Rust | `cargo` / `rustc` **1.94.1**（本机唯一可用平台；Windows 未执行，见 §5） |
| 依赖 | `serde`、`serde_json`、`libc`，锁于 `Cargo.lock`；无网络/进程类依赖 |

## 2. 执行命令与结果（全部实跑）

```bash
cd desktop/native/fs-guard
CARGO_TARGET_DIR=/tmp/fsguard-target cargo fmt --check
CARGO_TARGET_DIR=/tmp/fsguard-target cargo clippy --all-targets
CARGO_TARGET_DIR=/tmp/fsguard-target cargo test
CARGO_TARGET_DIR=/tmp/fsguard-target cargo build --release
```

| 命令 | 结果 |
| --- | --- |
| `cargo fmt --check` | 无 diff |
| `cargo clippy --all-targets` | **0 warning / 0 error** |
| `cargo test` | **51 passed; 0 failed**（单元 28 + 进程级探针 23） |
| `cargo build --release` | 成功（`opt-level="z"`、`codegen-units=1`、`strip`） |

`CARGO_TARGET_DIR` 指向 `/tmp` 是本轮的个人选择，仓库内不存在 `target/`；
`desktop/native/fs-guard/.gitignore` 已忽略 `/target`，`Cargo.lock` **未被忽略**（二进制工程需要锁定）。

## 3. 交付物

| 文件 | 作用 |
| --- | --- |
| `Cargo.toml` / `Cargo.lock` | 工程与锁定依赖 |
| `src/protocol.rs` | 有界 stdio 帧、请求/响应、错误码词表 |
| `src/paths.rs` | 路径校验、逐组件 `openat` + `O_NOFOLLOW`、文件身份 |
| `src/grants.rs` | 根授权注册表（内存态、递增版本、根身份复验） |
| `src/ops.rs` | `list` / `stat` / `read` / `search` 与全部上界 |
| `src/main.rs` | 双线程 stdio 主循环、`op` 全量 `match` 白名单 |
| `tests/stdio.rs` | 进程级竞争与逃逸探针（F03–F06 的 macOS 半） |

## 4. 逐项对应

### 7.1 工程、锁文件、有界协议、根授权与动作白名单 — 已实现

- 帧 = 4 字节大端长度 + 该长度的 JSON，上限 **64 KiB**；长度在**读体之前**校验，
  超限直接拒绝且不为它分配内存。超限属于流失步，报错后进程干净退出（退出码 0，已测）。
- 请求结构 `deny_unknown_fields`：多一个未声明字段即拒；`op` 是不含通配的穷举 `match`。
- **拒绝任意路径打开**：只有 `open_root` 接受绝对路径，且要求必须是原生选择器给出的绝对路径
  （`/` 开头），非绝对路径直接拒。其余动作只接受**相对路径**。
- **拒绝执行**：不存在 `exec` / `shell` / `spawn` / `write` / `unlink` / `rename` / `chmod`，
  测试逐个断言 `unknown_op`。
- 动作：`open_root` / `close_root` / `list` / `stat` / `read` / `search` / `cancel`。

### 7.2 根 FD、逐组件相对打开、no-follow、身份检查（macOS） — 已实现

- 逐组件 `openat(父 fd, name, O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK)`，非最后一段强制 `O_DIRECTORY`。
  符号链接命中 `ELOOP` → `rejected_entry`。**不使用 `realpath` 后重开**。
- **真实 symlink 竞态探针**（`tests/stdio.rs`）：先正常读取并列出 `safe/`，再把 `safe` 换成
  指向未授权目录的链接，然后请求 `safe/secret.txt`：返回拒绝，且断言响应中**不出现**目标内容
  （同时断言原文与 base64 两种形式都不出现）。
- **根被替换**：把已授权根移走并在原路径放链接后，后续请求返回 `unsupported`（要求重新选择）。

### 7.3 相对父句柄打开与 reparse/volume/identity（Windows） — **未执行**

本机为 macOS，无法编译或运行 Windows 目标；`paths.rs` 现为 unix-only，Windows 上该 crate
**无法构建**。按规范"缺少可靠路径能力的平台 SHALL 关闭本地文件能力"，在这条实现并探针通过前，
Windows 侧必须保持 `local_files` 关闭。**不记为通过**。

### 7.4 硬链接、特殊文件、敏感清单、隐藏策略、Unicode/长名 — 已实现

- 普通文件 `nlink > 1` → 拒绝（显式路径与列目录两条路径都拒绝；列目录计入 `skipped`）。
- 块/字符设备、FIFO、socket → 拒绝（以 `/dev/null` 链接形式做了探针）。
- 敏感清单（`.ssh`、`id_rsa`、`.netrc`、`keychains`、`credentials` 等）**大小写不敏感**匹配；
  拒绝读取，且在 `include_hidden: true` 下也**不出现在清单里**——没有参数可以关闭排除。
- 隐藏项（`.` 开头）默认不列出、不搜索；显式 `include_hidden` 可列出，但敏感项仍被排除。
- 名字按字节透传、按需 lossy 展示，调用方只回显相对路径；绝对路径不出现在任何响应里（已断言）。

### 7.5 分页枚举、grant_version 游标、字面量搜索、有界读取 — 已实现

- `list`：游标为上一页末名，`grant_version` 随页返回；单页 ≤ `MAX_PAGE`(1000)，扫描 ≤ 20000 项，
  命中扫描上限以 `truncated` 明确返回；被拒条目计入 `skipped` 且不跨入。
- `read`：单块 ≤ 32 KiB（选此值是为了 base64 后仍能装进 64 KiB 帧），返回
  `offset` / `bytes` / `total` / `truncated`，可续读；越界 `offset` 拒绝。
- `search`：**字面量**名称/文本匹配（不做正则，避免不可信输入的 DoS）；上界为
  结果数 200、扫描项 20000、单文件 4 MiB、深度 32、墙钟 5 s；命中任一上界返回
  `truncated` + `truncated_reason`（`results`/`candidates`/`deadline`），
  超限未检视的文件计入 `skipped` 而**不**当作"未命中"。
  已测跨 64 KiB 块边界的命中（靠重叠窗口）。
- `cancel`：读线程在转发目标请求**之前**写入取消标记，因此运行中的 `list`/`search`
  能在枚举/读取过程中看到取消并返回 `cancelled`；标记集合有上限（1024）避免无界增长。

### 7.6 可重复构建、签名、包内完整性、只启动随包 helper — **未执行**

属打包范畴，按指示搁置。`cargo build --release` 已能产出二进制，但签名、公证、
包内校验与"只启动随包固定 helper、缺失/不匹配即关闭文件能力"均未做。**不记为通过**。

### 7.7 F03–F06 探针 — macOS 半已跑通，Windows 半未执行

见 §2 的 `cargo test` 结果与 §4 各项引用；脚本即 `desktop/native/fs-guard/tests/stdio.rs`
（随仓库版本化，可直接复跑）。**Windows 半未执行**，因此 7.7 未整体勾选。

## 5. 本轮发现并修复的两个真实缺陷

两个都是在写探针/复读自己的代码时发现的，且都是"检查看起来成立、实际不会触发"的类型：

1. **目录偏移被 `dup` 共享**：`read_names` 原先用 `dup(root_fd)` 交给 `fdopendir`。`dup` 共享
   同一个 open file description，因此**目录偏移也共享**——第一次列目录把偏移推到末尾，
   第二次列同一目录直接得到空结果。表现为"分页只返回第一页、`include_hidden` 第二次调用返回空"。
   改用 `openat(parent, ".")` 取得独立偏移的**新**文件描述，两处调用点都已改。
2. **`verify_identity` 拿 fd 与自己比**：原实现比较"持有 fd 的 `fstat` 身份"与"打开时记录的身份"，
   而同一个 fd 的身份永不改变，这个检查**永远不可能失败**，等于没做。改为对记录的路径做
   `lstat`（不跟随链接）比对身份，从而真正检测"路径已指向别的对象"，并新增两个测试
   （根被替换、根被删除）覆盖。

第 1 个缺陷若只写"能列出目录"的测试会直接漏过；第 2 个若只写单元测试而不构造"替换后应失败"
的场景也会漏过——两者都是先写"必须失败"的断言才暴露的。

## 6. 打包可达性核对（为 7.6 排雷）

`desktop/package.json` 的 `build.files` 是**窄白名单**：`["dist/**/*", "resources/**/*"]`。
因此新增的 `desktop/native/**`（含 Rust 源码与 `target/`）**不会**被扫进 `.app`，
不存在"源码/构建产物随包"的隐患；本轮无需为此加排除规则。

这同时说明 7.6 的正确做法是**显式**登记：编译好的 helper 必须像现有后端那样作为
`extraResources` 条目加入（现为 `build/dist/cowagent-backend` → `backend/cowagent-backend`），
即"随包固定 helper"是一次可复核的显式改动，而不是靠通配符顺带带进去。在 7.6 补齐前，
主进程不应尝试启动任何 helper——本轮也只交付了 helper 本体，未接入主进程。

## 7. 结论

分组 7 的 macOS 侧（7.1 / 7.2 / 7.4 / 7.5，探针见 7.7 macOS 半）已实现并有实跑证据；
7.3、7.6 与 7.7 的 Windows 半未执行，因此：

- 分组 7 **不勾选**为完成；
- 目录读取能力（`local_files`）在**所有**平台保持关闭，直到 7.3/7.6/7.7 补齐；
- 未通过平台的降级路径是"普通选择上传"，符合 spec 的"未通过平台保留普通选择上传"。
