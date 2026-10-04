# 代码核对基线

- 核对日期：2026-10-03（Asia/Shanghai）。本文件仅记录只读代码核对，不是功能、性能或打包验收通过证明。
- 上游参考：`origin/master@48c0d79c36146950667f8d1884ab823deae62305`，前序讨论已通过 `git fetch origin master` 核对。
- 当前分支：`rdai@416aebf3`。开始本 change 时存在 `add-sap-workbench-scene`、Scene/SAP、路由和测试的未提交改动；本提案只写入自己的 change 目录。

## 已观察事实

1. master 的 `desktop/src/main/index.ts` 启动本机后端，`python-manager.ts` 创建随包后端或开发 Python 子进程；Agent 循环和命令工具在该进程所在机器运行。普通命令工具有 Windows cmd.exe 分支。
2. rdai 保留本地启动并增加 remote 模式；remote 不启动本地业务后端。`auth-broker.ts` 已有 PKCE、原生会话、tenant 和 epoch，但认证请求仍围绕一个 backendOrigin。
3. `agent/desktop_local/worker.py` 明确只执行单个工具，不启动模型循环/Web/scheduler；`agent/desktop_remote/` 的现有主要方向是服务器将项目工具下发给桌面。
4. `desktop/src/main/project-execution/skill-transfer.ts` 使用 `/api/desktop/execution/skill-package`，下载绑定 command、设备、grant 与 Skill digest。不能直接当作本地 Agent 无 command 的企业 Skill 拉取 API。
5. `agent/skills/loader.py` 按本机目录发现技能；manager 存在历史兼容回退，新模式需要明确的受信 provider 和拒绝失败边界。
6. 现有知识 Web 路由提供目录、读取、图谱、导入和原始资料等能力；本地 Agent 的远程检索适配需新增，并复用服务端数据根、owner、Agent 绑定与知识过滤。
7. `auth/capability_matrix.py` 中 `desktop_local_processing` 为 implemented=False/accepted=False；项目执行与脚本切片为 implemented=True/accepted=False，相关开关默认关闭。
8. `desktop/src/main/local-execution/sandbox.ts` 的项目脚本路径在 macOS 使用沙箱且不允许网络；Windows 返回不支持。该事实仅描述这个项目执行器，不等于 master 普通 Windows 命令不可用。
9. `Scene/production_plan/backend/api.py` 的导入 handler 直接解析上传的 Excel/CSV；财务分析 Skill 已有可复用脚本，端到端本机迁移、打包依赖与平台兼容仍待验证。
10. `harden-production-readiness` 与 SAP 场景 change 正在进行；新模式必须逐切片复用最终证据，不把进行中任务当已完成。

## 本次验证范围

2026-10-03 按用户追加要求，将会话、个人知识和个人 Skill 的服务器统一存储与同步纳入同一 change。已核对 `session-history-workbench`、`tenant-knowledge-console`、`tenant-skills-tools-console` 及 `business-permission-catalog` 的现行规范：历史须保留业务 ID 和不制造持久空记录；个人知识按实际自有根/owner 判定；Skill 正文已有 `resource_id`、`expected_mtime` 与功能/资源权限合同。新增完整个人 Skill 定义维护和同步合同属于待实现范围，不能把现有个人参数功能当作完整包同步已实现。

本次仅生成 OpenSpec 产物并进行格式/一致性验证。未实施代码、未运行真实企业登录/知识检索/Skill 下载/本机计算验收、未修改部署开关或生产数据。后续实施证据必须记录候选提交、平台、安装包、依赖、使用的合成数据与实际执行结果。

同步范围补充后的文档检查：10 个 capability delta、49 项 requirement、118 个 scenario、72 项未勾选实施任务；proposal/spec 映射与任务编号检查通过，`openspec validate add-desktop-local-runtime-with-enterprise-services --strict --no-interactive` 通过。以上为提案完整性，不是运行验收。
