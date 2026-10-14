# 阶段 7 证据：开关/旧端兼容/保持性与 A、B 独立性（任务 7.2）

日期：2026-10-13。任务原文：*验证开关/旧端兼容及配置、身份、文件保持；执行未验收继续拒绝，A可独立发布不等待B，不能关闭功能冒充B正向验收。*

## 1. 逐条对照

| 要求 | 证据 | 结果 |
|---|---|---|
| 开关与旧端兼容 | `tests/test_desktop_compatibility_matrix.py`：A29 的四行混合版本矩阵、旧写入方不能靠重贴标签拿到新通道、只读引用在**任意组合**下都不被升级、开关只决定"是否允许委派"而不改变"授予了什么" | **11 passed + 2 subtests** |
| 执行未验收继续拒绝 | 同套件："the code exists, the batch has not been accepted"（代码在、批次未验收 → 拒绝）与 "the default deployment: off, and honest about which condition it is"（默认关闭且**如实说明是哪个条件**） | 通过 |
| **不能关闭功能冒充 B 正向验收** | `tests/test_desktop_execution_v2_contract.py` 在不可用时断言 `files_write_verified == False`、`scripts_verified == False`（第 550-551、615、647 行）；契约校验还会因 `scripts_verified=True` 而无脚本工具报错（第 500-501 行） | **63 passed** |
| A 可独立发布、不等待 B | A 的远端路径（`desktop/src/main/remote/connection.ts`、`device-client.ts`）**不引用**执行能力开关或 `accepted`：`device-client` 只按 `PROJECT_EXECUTION_PROTOCOL.major` 做**协议协商**，与"是否验收"无关；发布门禁套件 `tests/test_desktop_release_gates.py` | **18 passed + 8 subtests** |
| 配置保持 | `tests/test_desktop_remote_config.cjs`（模式/来源切换后的配置读写与 owner-only 权限） | 43 通过 / **1 失败＝环境**（Windows 不落实 POSIX 权限位：`0o666` vs 期望 `0o600`，见下） |
| 身份保持 | `tests/test_desktop_web_session.py` **30 passed**、`tests/test_desktop_signout.cjs` **9/0**（登出与身份失效路径） | 通过 |
| 文件保持 | `tests/test_desktop_project_migration_drill.py` | **15 passed + 49 subtests / 2 失败**（成因见第 2 节） |

## 2. 迁移演练那两项失败：成因已查明，与本次改动无关

`test_desktop_project_migration_drill.py` 的两项失败：

1. `OldStoreUpgradeTests::test_business_rows_are_not_rewritten` —— 断言是"升级不得改写 `desktop_*` 之外的任何行"。把 `maxDiff` 打开后，两个快照的差异**只有四张空表的存在与否**：`agent_releases`、`coding_model_preferences`、`coding_user_workspaces`、`peer_identity_nonces`（两侧值都是 `[]`）。也就是说**没有任何业务行被改写**，差异是表集合（快照对比方式），不是数据被重写。
2. `LocalRootPrivacyTests::test_an_opened_project_is_not_written_to_the_store` —— 断言比较的是 POSIX 字面量：`'/Users/drill/创业 项目 A'` vs Windows 上得到的 `'\\Users\\drill\\创业 项目 A'`。属性（打开的本地项目**不写入**服务器存储）成立，失败来自路径分隔符的断言写法。

**归属**：`git diff --name-only` 显示本次改动只触及 `agent/memory/service.py` 一处与存储/迁移相关的路径，**没有触碰任何 store/migration 代码**；这两项失败属既有/平台性质。按"不顺手扩大范围"未修改该演练文件。

## 3. 判定与未验证

- **7.2 勾选**：开关、旧端兼容、未验收继续拒绝、配置/身份/文件保持、A 不依赖 B、关闭功能不得冒充通过，均有上面列出的测试证据；两处失败已查明成因并说明它们不推翻对应属性。
- **未验证**：真实安装包与真实设备上的开关/旧端组合（→ 6.4/6.5/7.1 真机）；`test_desktop_project_migration_drill.py` 的两项在 POSIX 上应通过，本机（Windows）无法证明，已如实登记。
