# 普通私有智能体个人工作目录统一——交付与运维说明

日期：2026-10-10。对应 OpenSpec change：`unify-private-agent-personal-workspace`（已于 2026-10-10
归档至 `openspec/changes/archive/2026-10-10-unify-private-agent-personal-workspace/`）。
行为契约以归档 delta 已并入的 `openspec/specs/` 为准，其中新增能力
`agent-work-file-layout-migration`，并修改 `shared-agent-personal-workspace`、
`agent-user-file-directories`、`agent-workspace-scaffold`、`platform-file-browsing`、
`user-private-agent-management`；本文只作交付、升级、维护窗口与回退的操作说明，不作为需求基线。

---

## 1 一句话结论

普通**私有** Agent 在“未选择项目”时，业务工作目录、文件面板落点、上传/删除/回收站范围
与租户**共享**普通 Agent 使用同一规则：`<agent workspace>/user/<不可变用户ID>/`
（含 `uploads/`、`outputs/`、`work/`）。Agent 配置根（`AGENT.md`、`USER.md`、`RULE.md`、
`MEMORY.md`、`skills/`、`knowledge/`、`scheduler/`、`tmp/`）仍在 Agent 根，不随业务目录移动。

## 2 生效范围

| 场景 | 业务工作目录 / 落点 |
| --- | --- |
| 普通（私有或共享）Agent，未选择项目（含从未选、已清除、项目目录已失效） | `<agent workspace>/user/<用户ID>` |
| 普通 Agent，已选有效项目 | 该项目目录（优先，不变） |
| 普通私有 Agent 的**非 owner** | 拒绝；不因可见性变化而放宽 |
| coding / 本机项目 / Desktop 来源 | 原行为（不变） |
| 无经验证用户的请求（系统初始化、无身份上下文） | 原 Agent 工作目录（不变） |

- 用户目录由经验证的租户 + 用户 + 宿主 Agent 决定；客户端或模型自报的 owner、显示名不改变落点。
- 面板上传、删除、回收站恢复的可写范围与 cwd 一致：普通 Agent 限本人 `user/<用户ID>` 子树；
  指向 Agent 根配置文件（`AGENT.md`、根 `memory/`、根业务文件等）的旧客户端上传/删除请求被拒绝，
  不代理改投到其他目录。
- 文本编辑（`_editable_target`）与配置、知识、技能、记忆维护仍走各自原有鉴权，本 change 未放宽。

## 3 相对路径基准与提示

未选择项目的新一轮工具调用，业务相对路径以本人目录为基准，例如相对 `output/报告.html`
落在 `.../user/<用户ID>/output/报告.html`。系统提示词按实际生效目录区分措辞（项目目录 / 本人
目录 / `users/<用户ID>` 个人数据存储），`tmp/` 标明为内部/兼容用途。

## 4 旧数据的离线整理与有限兼容

迁移脚本 `scripts/migrate_agent_user_files.py` 现在同时整理两类旧文件：

- 旧 `tmp/`、`uploads/`、`outputs/` 载荷 → 按**原 basename** 进入本人 `uploads/` / `outputs/`
  （保持既有平台回调契约）；
- Agent 根下的**业务**文件（文档、报告等）→ 进入本人目录并**保持原相对结构**
  （`客户甲/报告.xlsx` → `user/<uid>/客户甲/报告.xlsx`，不按 basename 合并）。

归属只来自可信记录（会话存储中由服务端写入的 owner/正文/extras），**不**凭文件名或当前 owner 推断。
无法唯一归属、被两人引用、与配置混用或目标冲突的文件进入 `user/_legacy/<原相对路径>/...`
隔离区（对文件接口不可读），只报告不猜测。

已移动附件的兼容：迁移导出受保护的单跳 `旧绝对路径 -> 新绝对路径` 表，`GET /api/file` 与
`/preview` 在**任何鉴权之前**查表定位，随后对**新路径**重新鉴权。旧路径被新内容占用、映射损坏或
目标缺失时返回冲突/缺失，不读取同名替代物；映射不链式重定向，客户端不能创建映射，映射故障不影响
新会话与新文件写入。

## 5 维护窗口与停写前提

迁移是**离线**操作，必须在维护窗口内执行，且要真正停止所有写入者：

1. 停止 Web/API 服务、调度器、在途任务与外部写入（渠道回调、脚本、同步任务）。
2. **仅停用 Agent 不算停止写入**——停用不阻止文件写入；必须确认无相关服务与外部写入。
3. 对目标 `<agent workspace>` 做受保护快照/备份（见第 7 节）；迁移脚本本身对业务文件只做
   同文件系统原子改名，不生成逐文件备份，快照是回退依据。
4. 预检为只读：不创建目标目录、不移动文件、不写运行状态。含混归属、配置混用、不安全路径与
   目标冲突会以隔离条目报告，应在实际升级前处理。
5. 迁移期间保持停写；中断后按清单与真实内容重跑（幂等），不重复搬移已完成内容。

源内容、归属或预检范围在预检后发生变化时，停止并重新预检。

## 6 命令

```bash
# 预检（只读；清单写到受保护位置）
.venv/bin/python -m scripts.migrate_agent_user_files \
    --workspace <agent workspace> --manifest /secure/backup/<ws>.manifest.json

# 迁移（维护窗口内、确认停写后）
.venv/bin/python -m scripts.migrate_agent_user_files \
    --workspace <agent workspace> --apply --rewrite-trash \
    --manifest /secure/backup/<ws>.manifest.json \
    --mapping            # 省略值即写入运行时读取的状态根默认路径

# 抽验（按清单核对摘要与源残留）
.venv/bin/python -m scripts.migrate_agent_user_files --verify /secure/backup/<ws>.manifest.json

# 恢复（回退；--apply 才真正移动）
.venv/bin/python -m scripts.migrate_agent_user_files --restore /secure/backup/<ws>.manifest.json --apply
```

- `--business REL`：显式指定业务路径（相对工作区，可重复）；缺省扫描 Agent 根。
- `--exclude NAME`：额外保留的根条目（可重复）。
- `--mapping` 的值缺省为 `common.state_dir.state_path("migration", "legacy_attachment_map.json")`，
  即运行时读取路径；也可用环境变量 `RSM_LEGACY_ATTACHMENT_MAP` 覆盖。
- 映射**只在 `--apply` 时写出**：预检写出的“预期映射”会让旧路径被识别为已占用而拒绝读取。
- `--restore` 只回退已发布（upload/output/business）条目；隔离与重复条目保持原处，避免把文件重新
  暴露给租户。原位置在上线后已有新文件时，该条目标记为 `occupied` 并跳过，**不覆盖**新数据。

## 7 脚本依赖与硬编码路径清单

| 依赖 | 说明 |
| --- | --- |
| `--workspace` | Agent 工作区绝对路径；省略时用 `agent.registry` 全量（需运行时环境） |
| 会话存储 | `agent.memory.conversation_store.conversation_store_path(workspace)`，需含 `messages` 表 |
| 旧区域 | 工作区根的 `tmp/`、`uploads/`、`outputs/` |
| 隔离区 | `user/_legacy/<原相对路径>` |
| 回收站 | `user/<uid>/.trash/<batch>/batch.json`，`items[].rel` 为原路径 |
| 业务清单排除项 | Agent 根配置与实体（`AGENT.md`、`memory`、`scheduler`、`skills`、`knowledge`、`tmp`、`user` 等） |
| 映射状态文件 | `state_path("migration", "legacy_attachment_map.json")`，或 `RSM_LEGACY_ATTACHMENT_MAP` |

清单/结果、映射均为受保护文件（权限 `0600`），写在公开工作文件范围之外；`--rewrite-trash`
会把原 `batch.json` 备份为 `batch.json.pre-migration.bak`。

## 8 回退

- 代码回退按既有服务重启流程处理在途请求；业务文件已按第 6 节清单/备份恢复后，数据与代码一致
  才恢复服务。
- 回退恢复前必须再次停写；已有新增/修改的文件先保留，冲突由运维按清单处理，**不**用旧快照抹除。
- 旧附件映射属于运行时兼容资产，随备份到期一起清理会破坏历史附件定位；应在备份保留期内维持有效。

## 9 未承诺的边界

- 调整的是**业务工作位置**，不是用户级执行隔离：同租户内的 Python/Shell/技能仍按既有执行授权
  运行，可直接读盘；默认 cwd 改变不等于脚本沙箱。
- 不改变外部 coding、桌面本机目录与无用户机器任务的既有契约。
- 不新增共享资料入口或成员间分享。
- 脚本的硬编码路径（第 7 节）不在附件兼容承诺内；内容中缺少原 cwd 的相对文本不猜测。

## 10 验证记录（本次实际执行）

| 范围 | 命令（子集） | 结果 |
| --- | --- | --- |
| 目录/工作区/上传删除/文件访问 | `pytest tests/test_shared_agent_personal_workspace.py tests/test_shared_agent_personal_workspace_wire.py tests/test_workspace_upload_delete.py tests/test_agent_user_file_access.py tests/test_agent_user_file_http.py` | 133 passed |
| 离线整理（预检/清单/结构/冲突/跨设备/幂等/回收站/恢复） | `pytest tests/test_agent_user_file_migration.py` | 44 passed |
| 单跳附件映射（决策表/一跳拒绝/损坏表） | `pytest tests/test_legacy_attachment_map.py` | 10 passed |
| 旧附件 HTTP + 预览 + 历史工件定位与重新鉴权 | `pytest tests/test_legacy_attachment_http.py` | 9 passed |
| 历史/子智能体/桌面工件/传输回归 | `pytest tests/test_history_agent_workspace.py tests/test_subagent_history.py tests/test_desktop_artifact_source.py tests/test_console_file_transport.py tests/test_private_agent_file_scope.py` | 81 passed |
| 控制台工作目录前端 | `node --test tests/test_console_workspace_frontend.cjs tests/test_console_workspace_upload_frontend.cjs tests/test_agent_welcome_frontend.cjs` | 67 pass |
| 历史/预览/私人范围/共享根用户树 | `pytest tests/test_history_agent_workspace.py tests/test_private_agent_file_scope.py tests/test_console_file_transport.py tests/test_shared_root_users_tree.py` | 全通过 |
| Desktop/coding/调度身份契约 | `pytest tests/test_scheduler_identity_revalidation.py tests/test_scheduler_identity_seam.py tests/test_scheduler_run_scope.py tests/test_coding_agent_type_boundary.py tests/test_desktop_contracts.py tests/test_history_agent_workspace.py` | 102 passed |

已知既有缺陷（**与本 change 无关**，已在本机变更前复现）：
`tests/test_desktop_project_migration_drill.py::OldStoreUpgradeTests::test_business_rows_are_not_rewritten`
在身份库升级后 `coding_model_preferences`/`credential_versions` 行序不同而失败；stash 掉本 change 的
`files.py`/`runtime.py` 改动后同样失败，属既有权库升级行为，未在本 change 内处理。

真实数据迁移**未在本机执行**：脚本能力与用例已在临时旧数据上验证，生产迁移仍在明确目标与
维护窗口内在目标环境执行，需留存停写与备份证据；文档完成不等于生产迁移完成。
