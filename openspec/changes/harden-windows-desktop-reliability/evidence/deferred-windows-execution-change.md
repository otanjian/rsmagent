# 后续事项记录：Windows 本机执行隔离 → 另立新 change（用户决定，2026-10-14）

用户明确：**"还没做的先不做，后续我再做一个新的 change 去补充"**。本文把这件事的**边界、原因与待办**固定下来，避免以后重复讨论或误判成本。

## 1. 本 change 里明确不做（且不允许用配置绕过）

| 项 | 状态 |
|---|---|
| Windows（`win32`）上的**本机脚本/工具执行** | **不在本 change 范围**。契约 `contracts/desktop/v2.json` 声明 `"win32": {"supported": false, "launcher": ""}`，注释：*"the Windows launcher and probes are not accepted yet; v2 frames are refused with `platform_unsupported` rather than translated"* |
| 受影响的任务 | 11.5、12.1–12.4、12.5、13.1–13.3、14.1–14.4、14.6 的**执行断言**（这些项的"逻辑层覆盖 + 拒绝语义"已在验收矩阵里如实标注，A32/A33 标"本平台不适用"） |
| 禁止的绕过方式 | 把 `win32.supported` 改成 `true`、或让设备谎报平台——即任务 8.2 禁止的**"只改支持声明"**；以及任务 10.1 的**"不放宽生产门禁测试"**。生产 `accepted` 的放开仍归任务 14.5（需受支持平台跑通 A01–A36） |

change 自身也已经是这么定的（`tasks.md` 的 Workflow follow-up 最后一条）：

> Windows 原生文件访问及执行隔离单独提案：文件句柄/reparse point 边界与执行沙箱分别验收，再申请修改 win32 支持契约；不作为本 change 的阻塞任务。

## 2. 为什么 Windows 现在跑不了执行（技术原因，供新 change 立项用）

执行链路建立在 **POSIX 原语**上，Windows 缺对应零件：

| 能力 | POSIX 现成 | Windows 缺 |
|---|---|---|
| 进程树真实结束（A17/A18 取消与部分效果） | `os.setsid()` + `os.killpg(SIGKILL)`（`agent/desktop_local/worker.py`） | 无进程组/SIGKILL → 需 **Job Object**（含 kill-on-close） |
| 文件边界强制（A26/A27 OS 边界拒绝） | `dir_fd` / `O_NOFOLLOW` 锚定打开（`common/safe_fs.py`） | **无 `dir_fd`**；本 change 修的"个人记忆目录被替换"（`unsafe_path`）正是其后果（`file_lock` 路径回退把锁文件当目录组件） |
| 凭据/权限隔离 | uid/gid + 权限位 | ACL / 受限令牌，模型不同 |
| 命令语法契约 | 契约描述了 POSIX 语法 | 未描述；且契约注释明确 `bash` 在 win32 **即使有 launcher 也仍不支持**，因为"Windows shell 执行 POSIX 命令字符串不是用户批准过的命令" |
| 边界探针 | 有并被验收 | 无（契约要求 launcher **and probes** 都被验收，才可声明 supported） |

**结论：不是"技术上不可能"，是"还没做、也还没验收"**；且契约的原则是"绝不宣称本构建不具备的强制能力"（`desktop_contracts_v2.py:650-653`）。

## 3. 新 change 的候选工作项（供将来立项与估工）

1. **Windows 执行启动器**：Job Object 进程树与强杀、kill-on-close、命名管道 stdio、退出码/超时/取消语义与 POSIX 对齐；
2. **文件边界强制**：无 `dir_fd` 情况下的锚定打开策略（句柄 + `FILE_FLAG_OPEN_REPARSE_POINT` + reparse point/junction 检查 + ACL 校验），并给出与 `safe_fs` 等价的拒绝码；
3. **凭据/权限隔离**：受限令牌、环境变量白名单、不继承秘密的验证；
4. **命令语法契约**：为 win32 明确可用工具与语法（`bash` 是否映射到 `cmd`/PowerShell、脚本工具如何声明），并写入 `contracts/desktop/v2.json`；
5. **探针与验收**：把 A26/A27 的 OS 边界拒绝、A15/A17/A18 的强杀与部分效果、A14–A20 的交付语义在 Windows 上真实跑通；
6. **契约与门禁**：以上证据齐备后，才申请 `win32.supported=true`，并复盘 10.1 的 accepted/open 映射；
7. **回归**：`test_desktop_*` 里当前按平台跳过的用例（如 `local_execution.cjs` 的 23 项 skip、`project_source.cjs` 的 17 项 skip）在 Windows 上转为真实执行。

## 4. 与本 change 的关系

- 本 change **不因这项缺失而阻塞**（tasks.md 明确"不作为本 change 的阻塞任务"）；
- 本 change 的 B 包（阶段 8–15）在 Windows 上按 14.4 的要求**如实标注"不适用"**，归档前需在受支持平台（POSIX）跑通 A01–A36 的适用项；
- 新 change 立项时可直接复用本文件 §2/§3 作为问题陈述与工作分解。
