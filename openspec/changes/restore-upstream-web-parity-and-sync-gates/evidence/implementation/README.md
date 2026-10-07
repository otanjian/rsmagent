# 实施与验收进度

固定上游 `48c0d79c36146950667f8d1884ab823deae62305`；修复基线 `rdai@64858d585b427ec4697dc0fb5d2ccb5911d35769`。上游已是 rdai 的祖先，本轮使用普通补偿修复，没有重放空 merge。

## 当前候选

源码树指纹：`44a370ab36f1bb8b9e2636401a7fb8ec9a6fb98d940d0814b950bec3f5535491`。完整 `scripts/check-production.sh --local` 退出 0。`required-local/report.json` 的执行前后指纹相同；报告只证明本地必要行为，`production_acceptance=false`。计划和证据文档不参与指纹，持续能力/依赖/用例清单与 CI 配置参与。

## 已完成

- 路径、区域、页签与实际后端统一登记；深链接、刷新、前进后退、旧链接、离页保护、快速导航、登录选租户、classic/split 和撤权均有验证。
- 修复 SSE error 依赖/清理、轻微上滚、知识库空态和迟到响应、消息换行，恢复成员模型目录模板。
- 通知使用原存储的事务终态顺序、授权分页、稳定用户 ID/租户分区及多窗口原子认领。两条控制台创建任务经真实调度器投递；批量和迟完成写入原数据库，验证 205 条跨页、重连、双窗口、偏好和撤权点击。操作系统通知展示被捕获，没有向真实用户发送消息。
- 更新服务及菜单完成：受信源/固定目标、平台权限/CSRF/审计、锁与幂等、独立 venv、冷备份/恢复演练及健康失败恢复。开始开放流量后失败不盲目覆盖数据。目标部署验收仍待完成，见 [源码更新记录](source-update.md)。
- SAP 兼容验收发现账号失效后旧工作台未清理，已补 iframe/草稿/迟到响应清理，不以新身份发送旧绑定控制请求。实际场景 UI 使用受控跨域 SAP 页面；Electron 使用真实生产容器、preload、IPC 和沙箱，身份配置隔离。
- 五层检查、合并规范及 CI 配置完成。缺入口/依赖/样式、历史项删除、来源漂移和必需用例 skip/xfail/未收集/缺浏览器均使检查失败。

## 验证结果

| 范围 | 结果 | 证据 |
| --- | --- | --- |
| 必要 Python：路由、通知、更新恢复、故障注入 | 31 通过，无跳过 | `required-local/pytest-*.xml` |
| 必要 Node：SSE、知识库、通知 | 32 通过，无跳过 | `required-local/node-*.tap` |
| 实际页面及真实后端浏览器 | 24 场景通过，0 脚本错误 | `required-local/browser/results.json` |
| 真实 Electron 容器 | 2 场景通过，0 脚本错误 | `required-local/native/native-results.json` |
| 公共生产 Python 切片 | 594 通过、9 跳过、4 subtests 通过 | `production-final.log` |
| 相关 Python 回归 | 131 通过 | `related-python-final.log` |
| SAP iframe 后端 | 18 通过 | `sap-python-final.log` |
| 前端/SAP/桥接专项 | 170 通过，无跳过 | `related-node-final.log` |
| 桌面类型检查及构建 | 通过 | `production-final.log` |
| 公共 Web/Desktop 行为 | 65 通过、5 安装包用例跳过 | `production-final.log` |
| 修改文件 Ruff F 类、OpenSpec strict、diff 空白检查 | 通过 | 收尾执行记录 |

补充回归在最后的通知用户 ID 小调整前完成；该调整由最终公共检查的实际后端浏览器及通知必要用例复验。各组有覆盖重叠，不累计为独立用例总数。安装包跳过和环境诊断不算相应形态通过。

环境：macOS、Python 3.14.3、Node 24.14.1、Playwright 1.63.0、Electron 33.4.11。桌面由候选源码重新构建；`.venv` 和 `desktop/node_modules` 是工作树内独立 APFS 克隆。CI 安装锁定的浏览器依赖及 Chromium。

```sh
export PYTHON_BIN="$PWD/.venv/bin/python"
export NODE_PATH=/Users/jiantan/.nvm/versions/node/v24.14.1/lib/node_modules
bash scripts/check-production.sh --local
"$PYTHON_BIN" scripts/check-upstream-sync.py --output artifacts/upstream-sync
```

## 仍需外部条件的验收

1. 任务 5.1 / 5.9：需要部署者指定受信仓库/ref、独立进程管理器和隔离的完整源码实例。本机更新测试使用真实 Git/venv/HTTP 进程及身份/凭据/工作区恢复，载荷不是完整生产 Agent。Windows、Docker、安装包明确拒绝 Unix 源码更新。
2. 任务 7.3：Gitea 正确 API 前缀为 `/Gitea/api/v1`。站点、组织、仓库 runner 均为 0，分支保护为空；run 696 的 `local-regression`（macos-production）和 `final-linux-image`（linux-production）均排队，见 `ci-inventory.json`。先前 404 来自错误 API 前缀，已更正，不是权限不足。持续 CI 主机待用户指定，实际失败候选阻断未验证。没有修改远端保护或生产服务。

本机 Docker daemon 未运行，Linux 最终镜像未验收。原生环境诊断 `process_environment=false`；最终安装包、Windows 和生产 SAP 业务事务不在此本地验收范围。change 保持未完成，不归档。

## 历史结果

`previous-implementation.md` 和 `previous-required-local/` 保留上一批 28/47 阶段、源码更新未实现时的结果。旧 `production-local.log` 等也只属于相应历史候选，不是当前结论。

原始 `64858d58` 已复现 `tests/test_sidebar_account_frontend.cjs` 的 7 个旧模式/夹具失败，以及 `tests/test_desktop_web_pages.py` 两个 W19 静态断言失败。本轮未放宽这些断言，不声称整个仓库测试全绿。原先资源/路由/更新前端的整组跳过已改测实际 fork 装配并保留业务断言。
