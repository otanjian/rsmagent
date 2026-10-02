# master → rdai：2026-10-02 候选审阅记录

本轮 merge 候选已于 2026-10-02 经本会话用户人工复核通过，按已批准范围提交并向本地 rdai 交付。50 个冲突已处理，master 的跨部署 Agent 转交已补齐 database 身份适配，并以真实模型验证 delegate、speak、clear；没有通过按 database 模式整体禁用来结束适配。相同能力的业务实现优先复用 master，IAM、租户、owner、资源授权接入 rdai 现有边界。

本记录不声称完成真实远程部署、所有提供方或安装包验收。已知失败和缺口见 [验证记录](validation.md)。用户已在获知这些限制后明确回复“人工复核通过”；本次批准用于本地合并，不将缺失证据改写为测试通过，不推送远端。

## 固定版本与范围

| 项目 | 本轮值 |
| --- | --- |
| 源 | GitHub otanjian/rsmagent 的 master；48c0d79c36146950667f8d1884ab823deae62305 |
| 目标 | 原工作目录本地 rdai；f664bc06a90e33e4551400876cc92fec9cf60247 |
| 原目标跟踪分支 | gitea/main；同一目标 SHA，无额外本地提交 |
| 共同祖先 | 8f1b19f1e72db0b46772f78f9c760b04b1836428 |
| 同步分支 | codex/sync-master-to-rdai-20261002-103448 |
| 代码受测树（证据文档加入前） | 23b4a1150badb457c4c60c53c922cc1cc439ba33 |
| 人工复核通过的完整候选树 | 34569876a25119b97e9cbe5edf47bff5be46265e；提交前仅更新本索引和验证记录的审查状态，代码不变 |
| 新增上游范围 | 581 个提交；base→source 512 个文件；209 个上游 Python 测试文件 |
| 正式合并方式 | 从固定目标执行 git merge --no-ff --no-commit 固定源 SHA |

[refs.txt](refs.txt)、[上游提交](upstream-commits.txt)、[全部上游文件与运行入口分类](upstream-inventory.tsv) 是范围依据。实际冲突 50 项，历史 46 项；新增 32 项、消失 28 项的逐项处置见 [conflicts.md](conflicts.md)。历史 scripts/conflict-baseline.txt 保留其明确冻结的版本，当前差异由本记录登记，不把消失条目静默删掉。

独立克隆位于原仓库的同级目录 rsmagent-sync-master-to-rdai-20261002-103448/repo。人工复核前原仓库工作树干净，rdai 仍在固定目标；交付采用快进到本轮 merge 的方式，远端 master 和其历史均保留。原仓库起初没有本地 master 分支，本次不改写或删除任何 master 分支。

## 审阅重点

1. [跨部署身份协议及部署配置](../peer-database-identity.md)：复用 master PeerTransport/serve_invoke，新增 Ed25519 请求及响应签名、显式双向租户映射、现有外部成员绑定和持久防重放。检查失败只拒绝不合法请求；正确配置的 database 请求已实际执行。
2. [能力对照](capability-map.md)：database 装配主要是 fork handlers/runtime 和 console.js。上游 split 文件存在不等于运行生效，技能、历史、团队、流式等增量已核对实际调用入口。
3. 技能安装使用 master stage/commit 业务，数据库适配限定预览领取人及最终租户/私有目录；禁止私有 Agent 借共享 fallback 写入租户库。删除以已授权加载条目的确切目录为准。
4. 团队清空先检查所有涉及会话的 owner，再写入；新 get_context_start_seq 读面保留 tenant/owner 维度。跨部署 speak/clear 统一使用 SessionService 规范化后的 session_team_* ID。
5. 桌面 2.2.0 保留原生 database 登录与 Web 容器，实机完成选目录、真实模型读文本/Word、同进程退出重登后读取新目录；不以文件面板测试替代聊天读取。

## 证据与准确性

- [验证记录、复跑命令和未通过项](validation.md)。不同测试组有重叠，不累计成唯一测试总数。
- [真实 peer 三模式结果](peer-real-model.json)：双进程、独立身份库、真实 DeepSeek；中继为本机文件转发，未连接实际托管 LinkAI 中继。
- [桌面传输及构建摘要](desktop-transfer-and-build.json)：仅保存合成测试身份和白名单元数据、构建哈希、Word 文件传输字节/hash 对照。
- [桌面聊天截图](desktop-relogin-local-read.png)及[技能实际安装截图](skills-installed.png)。内容来自隔离测试资料，不含生产业务正文。
- [日志清单](local-log-manifest.json)记录原始日志 SHA-256。原始日志保存在独立运行目录，可能含测试路径或模型调试信息，不纳入仓库。安全摘要：[路由](route-coverage-final.txt)、[接缝](web-seams-final.txt)、[peer lint](peer-lint-final.txt)、[桌面重登](desktop-relogin-with-peer-migration.txt)。

代码受测树不包含本证据目录及新增说明文档，随后只加入文档和截图，不改变受测代码。完整暂存树由运行目录 candidate-tree.txt 记录，提交前须与 git write-tree 对比。人工桌面截图关联较早代码树 7c6af02ab7646f0043d24b2e646288001cf73cd1；它与最终代码树的差异仅为 peer 接缝、追加身份库表及相关测试，桌面构建的 286 个文件逐一哈希一致。最终 peer、核心回归及桌面重登已复跑。

## 人工复核与交付

审查人：本会话用户。审查日期：2026-10-02（Asia/Shanghai）。审查结论：用户明确回复“人工复核通过”。审查对象为上表完整候选树及本目录报告；批准后只更新审查状态文档，不改变受测代码。未修改远端 CI/分支保护，也未创建 PR。本轮只生成保留双方父提交的 merge，并将原仓库 rdai 快进到该提交；推送需另有指令。实际 merge 提交及父提交以包含本记录的 Git 历史为准。

提交前确认原 rdai 仍是固定目标且干净、两端远端未漂移、候选无未暂存代码且暂存树未改变。merge 第一父提交须为目标 SHA，第二父提交须为源 SHA。若原 rdai 或远端已变化，保留当前候选并重新评估，不能强行覆盖。

## 回滚

本地交付前的 rdai SHA 为 f664bc06a90e33e4551400876cc92fec9cf60247，同时保存在 merge 第一父提交中；本轮没有对生产数据库运行迁移。已共享 merge 的撤销使用确认第一父提交后的 git revert -m 1，不改写共享历史。追加的 migration 45 仅创建 peer_identity_nonces 表，不重写既有成员或授权；身份库升级/重入测试通过。恢复前备份数据库，旧程序可忽略此表；重新启用 peer 时应保留未过期 nonce，避免重放。代码回滚不等于数据恢复。
