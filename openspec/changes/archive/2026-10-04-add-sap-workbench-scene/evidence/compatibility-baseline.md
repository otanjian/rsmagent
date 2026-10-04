# 本机兼容基线与来源漂移

记录日期：2026-10-03。此记录仅覆盖独立兼容基线、离线版本报告及源码接口核对。用户暂停的 OpenCode 与 SAP Web GUI 实际操作测试继续暂停，本次没有执行浏览器、Bun、host、模型、SAP 或 MCP 操作。

## 文件与只读报告

- `Scene/sap_workbench/deployment/compatibility.json`：保留各阶段已记录版本作为部署期望，不升级历史来源。
- `Scene/sap_workbench/deployment/README.md`：说明版本来源、漂移、历史验收边界及当前结果。
- `Scene/sap_workbench/backend/compatibility.py`：读取 plist、包元数据、Homebrew 安装元数据和 Git 文件；不执行版本命令或连接网络。
- `tests/test_sap_workbench_compatibility.py`：缺版、漂移、Git 引用、格式错误和只读输出的专项测试。

实际执行：

```sh
.venv/bin/python -B -m Scene.sap_workbench.backend.compatibility
```

报告模式为 `offline_compatibility_baseline`，`network_tested/services_started/system_modified/sap_current_version_checked` 均为 `false`。报告显式带有：

```json
{
  "baseline_policy": {
    "complete_deployment_lock": false,
    "runtime_enforced": false
  },
  "all_metadata_matches": false
}
```

这两个策略字段读取基线声明，缺失或非布尔值默认 `false`。`all_metadata_matches` 只比较版本元数据，不能解释为完整发布锁、源码未修改或 SAP/SSO 运行兼容已验收。

## 十项实际离线结果

| 项目 | 历史期望 | 实际读取 | 状态 |
| --- | --- | --- | --- |
| Chrome | 154.0.8037.95 | 154.0.8037.95 | matches |
| macOS | 26.4 | 26.4 | matches |
| 主 Python | 3.14.3 | 3.14.3 | matches |
| aiohttp | 3.14.3 | 3.14.3 | matches |
| OpenCode 来源 | 0442518883 | 9acdb1d09ff4daed68550c2efb7ddb5250c6ab5b | drift |
| OpenCode Web package | 1.18.34 | 1.18.34 | matches |
| Bun | 1.3.14 | 1.3.14 | matches |
| MCP Python | 3.10.0 | 3.10.0 | matches |
| MCP SDK | 1.29.0 | 1.29.0 | matches |
| AnyIO | 4.14.2 | 4.14.2 | matches |

共 **9 matches / 1 drift**，实际 CLI 退出码为 **1**。Bun 版本来自 Homebrew 安装元数据，MCP Python 来自 `pyvenv.cfg`，SDK/AnyIO 来自 venv 包元数据，均没有执行这些二进制或导入 MCP 服务。独立复制的 Bun 无法离线识别时报告 `unknown`。

`git -C ../rsmcode/opencode status --short` 为空。OpenCode 位于 `rsmcode` 仓库子目录，来源读取的是包含它的仓库 HEAD；干净工作区不代表历史来源一致。对照两提交的 `opencode` Git 子树：历史为 `ca2f9f13c88d2a61c4ea5a52239dbfc95c2d9d2a`，当前为 `ffc44ac76180f59bfe7087eec1e9cb9925c0ff7d`，两者不同；因此此漂移包含 OpenCode 内容变化，不能解释成仅其他目录改变。

历史来源号与后续 Web 版本观察来自不同阶段：`0442518883` 的 `opencode/packages/app/package.json` 实际为 1.18.31，当前观察为 1.18.34。保留历史期望用于揭示变化，不把它们组合成已通过同一次完整验收的发布锁，也没有 checkout、升级、降级或修改用户系统 Chrome 自动更新。

## 场景外置 host 的静态接口

从 `opencode_adapter/host.ts` 的直接源码导入和动态节点列表提取 **26 个文件**；全部存在，当前文件与 `git show 0442518883:opencode/<path>` 的原始字节逐项比较均一致。覆盖工具注册、配置迁移、数据库/事件/会话节点、会话执行、HTTP API/handlers、授权及位置中间件。

当前 `ApplicationTools.register`、`Tool.Context` 的 `sessionID/assistantMessageID/toolCallID`、`Config/ConfigMigrateV1`、`SessionExecutionLocal` 和 HTTP 组合接缝没有发现明显签名断裂。Effect、platform-node、sql-sqlite-bun 的 catalog 版本仍为 4.0.0-beta.83。

当前 core runner 的 LLM 改动新增会话及 parent header，场景模型桥继续只验证自己的 Bearer，目标由绑定解析，不使用这些新增 header。旧 provider 的超时、Cloudflare 与 Gemini 参数变化没有提供当前 SAP canonical host 必须修改的接口证据。没有观察到明确需要修补的场景或外部源码接口；这只是静态核对，未执行 Bun 导入、host 或供应商请求，不宣称漂移后的运行兼容已通过。

## 隔离测试与未验范围

```sh
.venv/bin/python -B -m pytest -q tests/test_sap_workbench_compatibility.py
```

最新结果：**30 passed，0.22 秒**。测试覆盖十项漂移、缺失元数据、普通/packed/worktree Git 引用、无法识别的 Bun、不合法基线、文件字节和修改时间保持、无进程/网络连接，以及报告显式策略和缺失策略的安全默认值。Git 空白检查及新增文件尾部空白检查通过。

SAP NetWeaver 758、S4H / Client 200、中文、“原始屏幕”、SPRO/参考 IMG 和 ME21N 保存前操作来自既有现场记录，本次不连接 SAP 确认当前版本。精确 SAP 内核 patch/主题、SSO/重定向域、远程显示/noVNC、完整控件与交易覆盖、有效完整单据/提交、双真实 SAP 用户与代表性负载、桌面登录后双栏，以及版本漂移后的现场兼容仍未验收。

基线与报告没有冻结系统 Chrome、没有接入运行时自动阻断，也不构成完整部署锁。原任务 **1.5 保留未完成**，完整 G0–G4 不据本次离线结果勾选通过；生产范围、远程部署、统一登录、G3 与实际操作测试沿用已有延后/暂停安排。
