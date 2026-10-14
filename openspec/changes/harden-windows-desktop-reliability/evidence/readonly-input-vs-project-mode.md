# 阶段 11 证据：readonly-input 是引用、项目模式才是授权（任务 11.1）

日期：2026-10-13。任务原文：*`readonly-input` 与项目模式语义复核：确认"选择本机目录"得到的是文件**引用**，若要让对话读取本机项目，须以项目模式打开；在 `agent/desktop_remote/mode.py` 的行为不变的前提下，用测试固化该区分。*

## 1. 语义现在在哪里（复核结论）

| 位置 | 内容 |
|---|---|
| `agent/workspace/execution_target.py` | 模块不变式原句：**"A read-only reference is not an execution grant."**；`MODE_READONLY_INPUT="readonly-input"` 与 `MODE_PROJECT_EXECUTION="project-execution"`；谓词 `is_readonly_input` 与 `allows_project_execution`（后者**仅**在 `location=desktop` 且 `project_mode=project-execution` 时为真）；desktop 目标只携带 device/workspace/binding 标识，**绝对根留在客户端** |
| `agent/desktop_remote/mode.py` | `remote_mode_for()` 的第二道守卫：目标不允许项目执行时**直接返回 False**，原注释写明"`readonly-input` is a file *reference*, not permission to execute: delegating it would hand the device a write the user never granted"；`execution_cwd` 已解析时也不委派（本地路径归本地）；`delegation_enabled()` 读不到开关时按**未说 yes**处理（关闭而非默认开放） |

即：**"选择本机目录"得到的是文件引用**；要让对话读取本机项目，必须走项目模式（`project_mode=project-execution`）——这一点此前只在注释与代码里，没有任何测试钉住。

## 2. 新增测试：`tests/test_desktop_remote_mode_semantics.py`（**9 passed**）

用**真实** `ExecutionTarget` 驱动真实决策函数，只把部署开关打桩（避免把平台混进被测对象）：

1. 两种模式是互斥谓词（含 backend 目标两者皆假）；
2. **只读引用在部署开关"开"的情况下仍然不被委派**——本任务的核心；
3. 项目模式 + 开关开 → 委派；
4. 开关关 → 不委派；
5. 已解析本地路径（`execution_cwd`）→ 不再次委派；
6. backend 目标永不委派；
7. **读不到开关时保持关闭**（"a deployment that cannot say yes has not said yes"）；
8. 无身份 → 不进入桌面运行；
9. 源码级：`mode.py` 必须保留该守卫与那句理由、`execution_target.py` 必须保留"A read-only reference is not an execution grant."——**日后若有人删掉守卫，测试会红**。

## 3. 约束与回归

- **`mode.py` 行为未变**：`git diff --numstat -- agent/desktop_remote/mode.py` = **0 处改动**（本任务只加测试）。
- `tests/test_desktop_remote_dispatch.py` **92 passed**。
- `tests/test_desktop_local_prompt_priority.py`：45 passed / 6 failed，失败原文为 **"fixture assumes a POSIX host"**（Windows 主机上多出 `PLATFORM: Wi…` 提示），属既有平台差异，与本 change 无关。

## 4. 判定与未验证

- **11.1 勾选**：readonly-input 与项目模式的区分已复核并在行为与源码两层被测试固化，`mode.py` 未改。
- **未验证**：真实桌面 + 真实本机后端上的 A01/A02（清空安装标识后首次选目录、重启后再选、快速 A/B、取消、跨会话/租户的迟到 Promise）——归 11.2/11.3 真机。
