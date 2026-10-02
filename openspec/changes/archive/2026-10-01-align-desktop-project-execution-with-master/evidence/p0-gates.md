# P0 门槛检查（任务 1.8 / A33 / A36）

任务 1.8 要求：**复用表、平台方案、工具分类和依赖证据四项可复核后**才进入对应平台实现；
**未完成平台必须保留明确未完成项，不能靠关闭开关结项**。

本文只做一件事：把四项证据的**复核入口**和**平台结论**集中到一处，并逐平台给出
"可否进入实现"的判定。证据本体在各自文件里，本文不复制结论。

复核方式：逐项打开被引用文件并核对其真实性（文件存在、内容与任务要求对应），
同时对可执行项跑一次，不把文字记录当成已验收。

## 1. 四项 P0 输入

| P0 输入 | 证据入口 | 复核结果 |
| --- | --- | --- |
| 复用表 | `evidence/master-reuse.md`（D1 逐项差异与复用结论）、`integration-map.md` §3 与 §5.1—5.3（跨 change 边界、推荐归档顺序） | 可复核：chooser、`project_store`、cwd、工具、`SkillManager`、提示、产出七个接缝逐项有"复用/新增"结论，且跨 change 的易误读 v1 条款已列明。 |
| 平台方案 | `evidence/platform-inventory.md`（发布平台、最低版本来源、打包与签名）、`evidence/platform-probes.md`（macOS 真实 `sandbox-exec` 探针、必需许可、读边界准确口径） | 可复核：macOS 一侧到"真实内核拒绝"级别；Windows 一侧**只有未完成项**，见 §2。 |
| 工具分类 | `evidence/execution-boundary-map.md` §1（`device-ops.ts::SUPPORTED_OPS`、fs-guard 穷举 `match` 与 `UnknownOp` 断言）、`platform-inventory.md` §5（按平台的 cwd/落盘分类） | 可复核：v1 只读 helper（构造上只读）、v2 只读、v2 落盘、v2 脚本、`_CWD_TOOLS` 五类分明；`win32` 是"不可用平台"而非"少一个落盘工具"。 |
| 依赖证据 | `evidence/platform-inventory.md` §4（逐层锁定强度）、`evidence/skill-dependencies.md`（技能依赖基线）、`desktop/build/check-skill-dependencies.py` + `build-backend.sh` 的 `--verify-imports` | 可复核但有**明确缺口**：清单文件 + 构建期真实 import 校验收口；Python 侧**无哈希锁**（§4）。缺口已归因，未用"有清单"结项。 |

可执行复核（本轮实跑）：

```bash
.venv/bin/python -m pytest tests/test_desktop_execution_v2_contract.py \
  tests/test_desktop_execution_broker.py tests/test_desktop_compatibility_matrix.py \
  tests/test_desktop_execution_types.py -q -p no:randomly   # 128 passed
.venv/bin/python scripts/gen_desktop_execution_types.py --check   # 与 TS 镜像一致
node --test tests/test_desktop_execution_contract.cjs             # 19 passed
```

## 2. 逐平台判定

| 平台 | 工具面 | 判定 | 依据 |
| --- | --- | --- | --- |
| macOS（`posix`） | 只读 + 落盘 + 脚本 | **可进入实现，且已实现** | 真实 `sandbox-exec` 边界（动态路径、软链接逃逸、派生进程、读写越界、信号只给 children）；`test_desktop_local_execution.cjs` 50 项、`test_desktop_local_worker.py` 44 项通过。 |
| Windows（`win32`） | **无任何工具** | **不可进入实现，明确未完成** | 契约 `platforms.win32.supported=false`；`resolveScriptSupport` 在 win32 返回 `unsupported_platform`；无 Job Object/受限令牌探针。broker 对 win32 帧回 `unsupported_platform`(422)，未来若接受 Windows 启动器仍以 `feature_unavailable`(503) 拒绝 `bash`，绝不翻译 POSIX 语义（两段拒绝均有断言）。 |
| Linux | 不适用 | **不是发布平台** | 无 Linux 构建 workflow；`posix` 指隔离家族，不等于已发布 Linux 安装包。 |
| Windows 7/8/8.1（传统渠道） | **无任何工具** | **不可进入实现，明确未完成** | Electron 22 + Python 3.8 渠道；执行 v2 所需启动器与探针在该组合上完全未测。 |
| 安装包内（签名后） | 不新增工具 | **明确未完成** | 任务 1.5 / 4.9 / 10.1—10.3：随包 Python 与签名后启动器未做探针。 |

## 3. 「不能靠关闭开关结项」的落实

这一条的实质是：**关闭开关是部署动作，不是验收结论**。本 change 的落实方式是让
"关闭"与"未实现"成为两件可区分、可分别观测的事：

1. 开关默认关闭且**只影响可用性**：`desktop_project_execution_enabled` /
   `desktop_project_scripts_enabled` 默认 `false`，此时 broker 四条路径一律
   `503 feature_unavailable`，且**不删不改任何行**（
   `tests/test_desktop_execution_broker.py::test_closing_the_switch_stops_new_calls`
   与 `::test_closing_the_switch_deletes_and_rewrites_nothing`）。
2. 关闭**不能**把未实现平台说成可用：`win32` 的 `platforms.supported=false` 是契约常量，
   开关打开也只回 `platform_unsupported`；`files_write_verified` / `scripts_verified`
   无法在缺少 script 工具时被置真（`test_scripts_may_not_be_claimed_without_a_script_tool`）。
3. 关闭**不能**让只读目标升级为执行：`readonly-input` 的 grant 即使强开开关也无法变为
   `project-execution`（`tests/test_desktop_compatibility_matrix.py` 四行组合全测）。
4. 未完成平台在本文 §2 与 `evidence/release.md` §7 各留一条，**不因开关状态被勾选**。

因此第 10 组的平台矩阵仍以"Windows 与安装包内未做"为**未完成**，与开关默认值无关。

## 4. 结论

四项 P0 输入均可复核：**复用表、工具分类、依赖证据（带哈希锁缺口归因）已具备**，
**平台方案在 macOS 上到内核级、在 Windows 上明确未完成**。据此：

- 已实现部分（macOS / posix 全链路）可以继续，且已完成；
- Windows 与安装包级验收**不得**以"开关已关闭"替代，保留在任务 1.5 / 4.5 / 4.9 /
  10.1—10.3 / 10.7；
- 任务 11.6 的最终启用登记，以这些未完成项关闭为前提。
