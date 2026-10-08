# 实施证据

2026-10-07（Asia/Shanghai）复核：源 `origin/master=48c0d79c36146950667f8d1884ab823deae62305`，目标 `rdai=64858d585b427ec4697dc0fb5d2ccb5911d35769`。远端 `gitea/rdai` 与目标相同，`gitea/main=b83d533fb04090a22b2cd5c27ad1476ca9a183d5` 是目标祖先，目标额外包含桌面 SAP/登录记忆/编程偏好提交 `64858d58`。本轮不变更这些既有行为，不重做 merge。

实施目录为本任务附属隔离 worktree，分支 `codex/restore-upstream-web-parity`；受测源码来自隔离目录。数据和浏览器验收使用测试夹具/隔离目录，不借用个人身份和业务数据。

进度为 **28/47 项完成**，不是整体功能验收率。当前未提交、未合并、未部署，change 保持实施中。阶段 5 受控源码更新尚未实现；真实 SAP/安装包、部署调度通知和线上 CI 阻断证据尚未齐备。

## 初始回归

- `node --test tests/test_agent_modal_scroll.cjs tests/test_functional_models.cjs tests/test_session_history_refresh_frontend.cjs`：29 项，23 通过、6 失败，见 `node-baseline.txt`。失败来自历史缓存依赖缺失的夹具。
- `python -B -m pytest tests/test_web_console_assets.py tests/test_web_console_routing.py tests/test_web_console_update.py::test_frontend_contract -q -rs -p no:cacheprovider`：25 跳过，见 `pytest-baseline.txt`。不是验收通过。

后续各批验证单独记录实际命令、候选内容与结果。尚未运行的场景保持未验收。

## 夹具修复

历史刷新夹具引入页面原有 `_historyCacheKey` 到 `_patchHistoryCache` 实现，未删除或放宽原断言。重跑上述 Node 命令：29 通过，0 失败/跳过。仅此结果关闭任务 1.4，不代表 SSE error 已修复。

## 已实现的行为及边界

- 路径登记来自实际 fork 页面描述，后端显式 GET 路由与前端路径、页签、区域共享元数据。覆盖深路径刷新、前进后退、旧链接、取消离页、异步确认回调、快速切换及资源绝对路径；未知路径仍为 404，API 不被页面兜底吞掉。
- 修复实际加载代码中的 SSE error 依赖、终态事件幂等处理、用户轻微上滚、知识库空态/迟到响应及长链接/行内代码溢出。
- 在既有 runs 存储中增加事务内终态顺序，授权先于分页；通知身份分区、断线续取、多窗口 IndexedDB 原子认领和手动运行归属接入实际页面。SQLite/WSGI 测试与浏览器模拟接口测试分别验证组件，尚未声明真实调度端到端验收。
- 新发现并登记 GAP-008：成员模型目录的加载函数仍存在，但实际模板缺少面板。已恢复授权成员目录，并验证 `/settings/models` 到 `/settings/catalog` 的投影。
- 五层同步检查会拒绝历史缺口丢失、未分类上游模块、缺路由/依赖、必要用例 skip/xfail/未收集/未运行以及测试期间候选变化。候选指纹包括所有非忽略源码、公共授权包、依赖锁和 CI 配置；计划与结果文档排除在指纹之外。
- `/api/version` 明确报告源码更新尚未验收，不暴露上游默认发布源的执行能力。四个更新成功/故障契约仍列为必要用例且尚不存在，完整门禁保持失败。

## 本地环境

Python 3.14.3，Node 24.14.1，Playwright 1.63.0，macOS。最终 worktree 的 `.venv` 和 `desktop/node_modules` 均为原依赖的 APFS 文件克隆，隔离工作区内独立存放；桌面锁文件与原环境一致，构建来自本候选源码。早期使用外部依赖符号链接导致原生 sandbox worker 无法读取环境，改为隔离目录中的真实依赖后复测通过。

浏览器依赖已新增独立锁文件 `tests/browser/package-lock.json`。本机运行使用已有同版本 Playwright；CI 安装该锁文件及 Chromium。浏览器加载实际 HTML、脚本和样式，HTTP 为合成夹具，不代表真实账户、部署接口或 SAP 宿主。

## 执行结果

`required-local/` 保存最后一次必要行为门禁的 JSON、JUnit、TAP 和执行日志，精确候选指纹见其中 `report.json`。它只证明报告列出的本地范围，不是生产验收。

本轮最终指纹为 `ab2b958b565516b15b4e574f3448b579c0009eb9ecfaf0ae72994d6ad4a99de5`，执行前、执行后及保存证据后的指纹一致。完整门禁退出 1，仅有阶段 5 四个缺失用例及该文件未收集的失败；其余检查层通过。

| 范围 | 结果 | 说明 |
| --- | --- | --- |
| 实际页面路由 Python | 6 通过 | 真实 `build_web_app()`，不是上游独立装配 |
| 通知顺序/授权 Python | 4 通过 | 迟完成、小 ID、时钟回拨、旧库迁移、205 条跨页及身份隔离 |
| 门禁故障注入 Python | 8 通过 | 临时装配缺路由、脚本、样式、历史删改、来源漂移和伪通过 |
| SSE/知识库/通知 Node | 32 通过 | 必要用例无跳过 |
| Playwright 浏览器 | 17 场景通过 | 脚本错误及意外请求均为空；含两窗口原子认领和成员目录 |
| 源码更新必要用例 | 4 个缺失，失败 | 不以版本菜单拒绝执行测试替代真正更新/恢复契约 |
| 生产检查 Python 切片 | 594 通过、9 跳过、4 subtests 通过 | 见 `production-local.log`，范围由公共脚本明确列出 |
| 收尾相关 Python 回归 | 155 通过、1 跳过 | 调度授权/运行记录、会话存储、更新接口；见 `related-python.log`，唯一跳过为阶段 5 前端正向契约 |
| 桌面类型检查及构建 | 通过 | 候选源码重新构建 renderer/main |
| 公共脚本 Web/Desktop Node | 65 通过、5 跳过 | 跳过项为实际安装包场景，不算该形态验收 |
| 桌面桥接/重登/导航专项 | 30 通过 | 组件回归，不代替 SAP/安装包验证 |
| 修改文件 Ruff F 类检查 | 通过 | 保留仓库的 F403/F405 重导出例外 |

公共生产脚本运行于最终离页回调微调前的候选（指纹 `715c3dd5580258073a0eb870b0a16051ad4df2859d24d9be820443e05363e097`）。其 Python、桌面构建和行为检查通过，同步检查失败且公共脚本最终退出 1，证明失败不会被吞掉。后续离页回调修正、未使用导入清理和指纹范围修正后，再执行 `required-local/` 的完整必要行为门禁及相关 Python 回归；不把较早公共脚本的全部结果冒充最后指纹的完整生产运行。

实际执行命令（在本 worktree）：

```sh
export NODE_PATH=/Users/jiantan/.nvm/versions/node/v24.14.1/lib/node_modules
export PYTHON_BIN="$PWD/.venv/bin/python"
"$PYTHON_BIN" -B scripts/check-upstream-sync.py --output /tmp/parity-delivery-final
bash scripts/check-production.sh --local
npm --prefix desktop run build
```

原生环境诊断报告 `process_environment=false`，脚本将它明确标作诊断而非生产验收。Linux 最终镜像、真实实例恢复、SAP iframe 和最终安装的 Desktop 包仍未验收。

## 已存在的失败与跳过

- `tests/test_sidebar_account_frontend.cjs` 的 7 个失败已在原始 `64858d58` 基线复现，涉及旧模式/权限预期和 `resetMemoryView` 夹具依赖；见 `account-baseline.txt`。不声称整个 Node 测试目录全绿。
- `tests/test_desktop_web_pages.py` 中两个 W19 断言在原始基线也失败：`test_w19_no_page_takes_a_file_handle_for_itself` 与 `test_w19_a_dropped_file_cannot_replace_the_shell`。本轮不放宽它们。
- Web 资源、路由整组跳过已改测 fork 实际装配；更新前端的正向契约仍因阶段 5 未实施而跳过，故任务 6.4 继续未完成，完整门禁也仍失败。

## CI 核查与后续条件

Gitea 的既有 `local-regression` job 与 GitHub 的 `upstream-sync` job 已接入公共门禁、固定浏览器依赖和结果制品；Gitea 的 `final-linux-image` job 保留原有职责。代码配置的 runner 标签为 `macos-production`、`linux-production`。尚未获得实际 job/check 名称与分支保护匹配的运行证据，因此 7.2、7.3 保持未完成。

只读查询得到 Gitea 服务版本 `1.27.3`；仓库 branch protections 与 Actions runners 查询均返回 404。该响应不能证明未配置，也不能证明配置已生效，只记录当前无法核实。没有修改线上配置或触发发布。

Gitea 制品步骤采用固定提交的兼容 action `ChristopherHX/gitea-upload-artifact@81f940d004763f986ba3582c007fd842dd5cb0d7`；已核对该提交禁用 GHES 不支持检查。Gitea 文档区分 [Runner 2 的兼容 action 要求](https://docs.gitea.com/runner/2/cache/) 与 [Runner 3 的 stock action 支持](https://docs.gitea.com/runner/develop/upgrade/)，因此不能从服务端版本推断 runner 兼容性。仍需在交付 runner 上验证制品上传和真实失败阻断。

源码更新后续实施需要：受信发布仓库/ref、实际进程管理方式，以及可用于成功更新和故障恢复的隔离源码安装。已向用户询问，尚未收到这些信息；不能以任意选定的上游仓库或假定的重启命令替代。任务 3.6～3.8、4.6、5、6.4/6.7、7.2/7.3/7.5/7.6 继续保留相应未验收范围；不能关闭或归档本 change。
