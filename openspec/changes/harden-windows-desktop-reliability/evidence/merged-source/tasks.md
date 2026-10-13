## 1. 前置与依赖确认

- [ ] 1.1 复核 `execution-isolation` 切片的真实平台验收证据已存在（由 `harden-production-readiness` 承担），确认它不是 mock 或文档完成状态；未满足时不进入阶段 2。
- [ ] 1.2 核对本 change 的 6 份增量与主规范的关系：4 个能力在 `openspec/specs/` 中确实缺失（本轮落盘证据见 `evidence/baseline.md`），2 个既有能力仅在追加场景上变更；确认不修改 `openspec/changes/archive/**`。
- [ ] 1.3 确认各依赖切片（`audit-log`、`credential-management`、`action-approval`、`resource-quota`）的当前可用状态与接口，登记其证据来源；不等待整份历史 change 完成。
- [ ] 1.4 确认本期发布矩阵是否包含 Windows：当前按 `harden-production-readiness` 的范围约束为**暂缓**；若变更该口径，须先修改 `contracts/desktop/v2.json` 的 `platforms.win32` 并提交可复现的平台探针证据，且不得只改声明。

## 2. 文档代际竞态修复（P1 前置）

- [x] 2.1 让 `registerDocument` 一次加载只推进一代。**实现已由上游提供**（`gitea/rdai` `b5b5e7fe`..`414bc6b3` 区间）：`desktop/src/main/remote/container.ts` 去掉 `did-finish-load` 监听、只保留 `did-navigate`（该文件第 160-161 行），即 design「方案与取舍」中记录的备选写法。本 change 不再自行实现。**验证（evidence/generation-fix.md）：探针测得一次加载代际恒为 1、加载完成后两次真实 bridge 调用均成功；该探针在本地上游化之前的等价实现上运行，重构建后需复跑一次以把证据重新绑到当前代码。**
- [ ] 2.2 为 `desktop/src/main/remote/host-bridge.ts:231-254` 的 `checkSender` 增加单元测试：同一文档重复触发不推进代际、导航替换后旧代际被拒、帧 id 与代际各自独立的拒绝码与消息不变。
- [ ] 2.3 复核 `desktop/src/main/remote-preload.ts:27-64` 的握手语义保持不变（单次握手、每次调用带代际）；**不**引入自动重握手，除非 2.1 在真实安装包上被证明不足以覆盖（见 design「方案与取舍」第 2 条）。
- [ ] 2.4 在真实桌面上复现 A01/A02 的前置：选择目录不再出现 `the calling document belongs to an earlier generation`，`choose` → `bind` 按序完成、取消不创建授权。**部分完成（evidence/generation-fix.md）：入口已渲染且 `canChooseWorkspace=true`、调用不再被判旧代际；`choose` → `bind` 完整序列与取消路径仍需人工在原生目录对话框上执行。**

## 3. 能力声明与部署开关

- [ ] 3.1 在 `auth/capability_matrix.py:677-700` 将 `desktop_project_execution`（按 3.3 决定是否含 `desktop_project_scripts`）的 `accepted` 置为按证据可放开的状态，并给出与端点动作一致的 `open` 映射；`open` 空集合不得作为"已启用"。
- [ ] 3.2 保持 `config.py:492-493` 的默认值为 `False`；仅在部署配置（`config.json`）显式打开 `desktop_project_execution_enabled`（脚本面按需），使新装环境不误触。
- [ ] 3.3 决定脚本面是否随本期放开：若放开，`desktop_project_scripts` 与 `desktop_project_scripts_enabled` 同步处理；若不放开，须在 meta 的 `surfaces.scripts` 中如实报告原因。
- [ ] 3.4 校验组合态优先级：`integrations/desktop/execution_capability.py:100-132` 在 accepted、开关、平台三者不同组合下的 `reason` 与 `availability()` 一致；确认 `channel/web/fork/handlers/desktop.py:705-724` 的 `_execution_enabled()` 同步放开。
- [ ] 3.5 确认 Windows 仍报告 `platform_unsupported`：不得因为 3.1 的放开而在未支持平台出现 `available=true` 或返回工具集。
- [ ] 3.6 同步发布门禁测试到本 change 口径：`tests/test_desktop_release_gates.py`（含 `test_the_real_declaration_is_not_accepted`、`test_a_switch_flip_alone_never_opens_the_composed_block`、`test_acceptance_alone_does_not_open_the_handler_gate`）、`tests/test_desktop_execution_v2_contract.py`、`tests/test_desktop_execution_broker.py`、`tests/test_desktop_meta.py`、`tests/test_desktop_compatibility_matrix.py`；每个被改动的断言须在注释中写明对应本 change 的阶段与证据。

## 4. P1：本地模式选择 / 确认 / 读写（A01–A04）

- [ ] 4.1 `readonly-input` 与项目模式语义复核：确认"选择本机目录"得到的是文件**引用**，若要让对话读取本机项目，须以项目模式打开；在 `agent/desktop_remote/mode.py` 的行为不变的前提下，用测试固化该区分。
- [ ] 4.2 在真实桌面 + 真实本机后端上执行 A01：清空有效安装标识后首次选目录、重启后再次选择；确认无 `Illegal invocation`、choose/bind 按序、ID 持久稳定、取消不创建授权。
- [ ] 4.3 执行 A02：快速选择 A/B、取消、切换会话/租户后旧 Promise 返回；确认只有当前代次可提交、无错绑。
- [ ] 4.4 执行 A03：从普通 Web 提交本机绝对路径、伪造 chooser handle、复用他人选择记录；确认均被拒绝，且个人项目根浏览/选择与受控导入不受影响。
- [ ] 4.5 执行 A04：本地模式选择中文空格路径，连续执行 read/edit/write/ls/search_files 与 Bash；确认六工具作用于同一真实项目，服务端/其他项目哨兵不变。
- [ ] 4.6 把 4.2–4.5 的实测输出（日志、截图、产出哈希）落入 `evidence/`。

## 5. P2：远程模式与命令交付（A05、A08–A20）

- [ ] 5.1 执行 A05：远程模式经真实模型调用同组六个工具，确认原 ExecutionRun 收到真实结果、worker 在设备 A 执行、无第二个模型循环。
- [ ] 5.2 执行 A08–A10（会话/Agent 切换与资格）、A11–A13（技能包、平台缺项、附件与知识资源）。
- [ ] 5.3 执行 A14–A17（幂等回执、进程强杀后的 outcome_unknown、参数/作用域冲突、退出码与超限）。
- [ ] 5.4 执行 A18–A20（取消与部分效果、断网存活期、双 alias 同根并发与版本冲突）。
- [ ] 5.5 复核 `integrations/desktop/execution_broker.py` 的 epoch/租约与 `desktop_project_execution` 组合态一致；确认 A32 的"关闭开关"路径封锁新任务且保留在途效果。
- [ ] 5.6 把 5.1–5.5 的证据落入 `evidence/`。

## 6. P3：技能运行与产物（A06–A07、A21–A25）

- [ ] 6.1 执行 A06/A07：本地与远程各跑固定 Excel/文档技能，确认汇总值、模板资源完整性、产出位于所选项目且可被系统应用打开。
- [ ] 6.2 执行 A21–A23：文件树/搜索/预览/编辑/@ 引用与刷新、跨项目切换、历史卡片的生成设备与项目定位、设备离线与客户端重启后的候选恢复（不自动恢复授权）。
- [ ] 6.3 执行 A24/A25：含脚本 HTML 预览无原生桥/无秘密/不可越界；系统打开、复制路径、另存为与显式上传的边界。
- [ ] 6.4 把 6.1–6.3 的证据落入 `evidence/`。

## 7. P4：打包、平台与降级（A26–A33）

- [ ] 7.1 执行 A26/A27：真实脚本经 `os.open`、动态路径、链接替换与子进程访问项目外文件时的 OS 边界拒绝；无环境秘密继承。
- [ ] 7.2 执行 A28：未批准动作、并发额度耗尽、磁盘不足、审计写入失败、许可过期按现有治理策略失败，无"成功空结果"。
- [ ] 7.3 执行 A29–A31：新旧客户端/服务器组合、无项目与只读输入、scheduler 或机器主体引用本机项目、页面试图传任意 shell/模块。
- [ ] 7.4 执行 A32/A33：关闭开关的降级与在途处理；在**真实安装包**上复跑 A04–A07、A12、A18、A24、A26–A27。
- [ ] 7.5 在 `evidence/` 记录 Windows 未纳入本期的处置（未执行、不宣称已验证），并说明不影响其现有功能代码。
- [ ] 7.6 全部 A01–A33 通过后，才可勾选 3.1 的 `accepted` 放开为对外可用；在此之前保持门禁语义。

## 8. 文档与归档

- [ ] 8.1 运行 `python scripts/check_change_deltas.py accept-desktop-local-project-execution`（py312 环境）确认 active 校验通过：MODIFIED 标题与基线逐字一致且保留场景、ADDED 标题未已存在。
- [ ] 8.2 归档时把 6 份增量应用到 `openspec/specs/`，再运行一次校验以确认 archived 方向（每个 requirement/scenario 均已存在于主规范）。
- [ ] 8.3 逐条勾选本 tasks.md 前，确认对应证据已落在 `evidence/`，且未把"接口占位或模拟实现"当作通过。
