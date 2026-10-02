# 验证结果及准入缺口

环境为 macOS 26.4 arm64、Python 3.14.3、Node 24.14.1、Electron 33.4.11、Desktop 2.2.0 dev 构建。依赖来自候选 requirements 和 desktop lockfile；补装测试所需 pytest/ruff、Playwright、python-docx、pypdf、openpyxl 及真实云客户端依赖 linkai 0.2.0。使用独立克隆、独立 venv、独立身份数据库及合成文件，没有迁移或修改生产数据。

固定代码树为 23b4a1150badb457c4c60c53c922cc1cc439ba33。测试组有重叠，以下数量不能相加成唯一测试总数。日志哈希见 [local-log-manifest.json](local-log-manifest.json)；原始日志位于与候选 repo 同级的运行目录。

## 结果

| 检查 | 当前结果 | 证据/边界 |
| --- | --- | --- |
| 路由覆盖 | 230 routes；70 upstream/160 fork；281 methods；通过 | route-coverage-final.log；不是业务全链路证明 |
| 模块接缝 | 22 upstream modules；364 fork-only symbols；0 findings | web-seams-final.log |
| 规范 6.2 的 14 个 Python 文件 + 新 peer 身份测试 | 260 passed，1 skipped | core-auth-with-peer-final.log；skip 为已移除的旧 _check_auth，不是现行认证检查被跳过 |
| 209 个上游增量 Python 测试文件，最终代码 | 1832 passed，28 skipped，55 subtests passed；17 warnings | upstream-regression-final-3.log；包含线程清理警告；具体 skip 下列说明 |
| 文档读取依赖补全后的追加复跑 | 43 passed，0 skipped | document-read-final.log；覆盖此前 3 项 pypdf 与 2 项 openpyxl 缺依赖跳过，未再假装把整组 1832 的旧计数改写 |
| peer、云入站、团队及 IAM 迁移相关组 | 117 passed | peer-final.log，包含新文件的 23 个参数化用例 |
| 追加的技能/历史/会话/多租户候选组 | 148 passed | candidate-final-regression.log；在 peer 追加前运行，后续没有更改其运行代码 |
| 实际装载前端定向回归 | 80 passed，0 skipped | frontend-final-subset-2.log，含规范 fragment/执行权限、技能预览、历史恢复及 i18n |
| 全部 tests/*.cjs 原始命令 | 1809 passed，11 failed，6 skipped | node-final-3.log；同环境目标基线 1807 passed，11 failed，6 skipped，见下节 |
| appearance browser 补 Node 模块解析路径 | 当前/基线均在 #app 隐藏处超时 | appearance-browser-final.log、appearance-browser-baseline.log；解决缺 Playwright 后仍有基线用例合同问题，没有记作通过 |
| npm --prefix desktop run build | 通过 | desktop-build-final-3.log；Vite renderer+主进程 tsc，不代表 renderer 独立类型检查通过 |
| renderer tsc --noEmit | 9 个既有 ChannelsPage LucideIcon 类型错误 | renderer-types-final-2.log；同环境目标基线 11 个，其中相同 9 个+被 master 修复的 2 个 BasicSettings 错误 |
| 实际 Electron 重登 E2E（追加迁移后再跑） | 6 passed，0 skipped | desktop-relogin-with-peer-migration.log；R01–R06，只证明本地回环的客户端/文件面板生命周期，D2 另有真实模型证据 |
| Cloud/MCP/外部工具合组 | 222 passed，1 failed | cloud-mcp-final-2.log；同环境基线复现目录读权限断言，见下节 |
| 能力声明检查的目标基线对照 | 19 passed，1 failed | capability-baseline.log；候选也存在同一缺少主规范文档问题 |
| peer 新增代码 F 类 lint | 通过 | peer-lint-final.log |
| 双独立身份库、两进程、真实 DeepSeek | delegate/speak/clear 均 done；前两者回显合成标记 | peer-real-model.json；实际应用协议和模型，文件中继，未宣称托管云网络通过 |

上游组 28 项跳过：2 项 Windows 反斜线、6 项 PowerShell、2 项 Windows 上传路径、3 项 pypdf、2 项 openpyxl、12 项未装载的上游 split console、1 项 split update menu。文档读取的 5 项后来补依赖复跑通过；平台和 split 专项仍未执行。database 实际 console 的技能、历史与权限另有定向测试及实机证据，不能用 split 源码测试代替。

## 失败及其同环境基线

原始全 Node 命令的 11 项失败与目标基线相同：1 项 appearance 文件因根目录模块解析找不到 Playwright；1 项仍读已退役 personal-console.js；2 项 scene 旧 fallback；5 项 sidebar 旧认证合同；2 项 recent-session harness 缺 _accountAppVisible。6 项跳过包含未解析到 Playwright 的 browser 合同测试。补设 NODE_PATH 后 appearance 实际启动 Chrome，但候选和基线同样在隐藏 #app 等待超时。未修改这些既有测试去迎合新实现，也未以跳过改写成功率。

external connections 失败是 test_a_tenant_catalogue_read_needs_the_read_permission：断言 403，当前实际 200；在固定目标以相同测试和依赖重现相同差异。这里属于既有目录读取合同待复核，不将其解释成新 peer 身份适配失败，也不宣称整个 MCP 检查通过。

能力声明测试失败是 desktop_remote_web 对应的 desktop-remote-web-workbench 主规范缺失，仅有归档材料；不是功能调用返回失败。renderer 类型错误为旧 IconComponent 对 LucideIcon 的 size 类型约束。上述事项均须由人工评审明确处置；本轮没有新增封禁去规避失败。

## 桌面真实路径：D1–D6

服务使用用户提供的 localhost:9899，由候选项目以隔离 COW_DATA_DIR 启动。原生桌面使用自己的隔离 profile，普通成员通过原生 database 登录。不是操作者生产 Cookie，也不是注入登录状态的模型调用。

| 规范项 | 本轮事实 | 限制 |
| --- | --- | --- |
| D1 目录及授权 | 原生选择器选 local-a，文件面板列出并读取；重登后新选 local-b | 本机 macOS dev；面板读取不充当 D2 |
| D2 聊天读取 | 真实 DeepSeek 调用 client_files list/read_text/materialize，读取文本与 Word；后台放置不同内容的同名 decoy。local-b Word 36606 字节，传输 SHA-256 与来源完全一致，后端 read 使用传输副本 | 第一次 A 的最小 Word 文件通过模型实际 bash/unzip 提取；补装 python-docx 后用有效 Word 再验证 B。未伪造内容回复；不是所有 Office/PDF 格式验收 |
| D3 同进程退出重登 | 原生应用保持同一进程，退出回原生 gate，同账号重新登录，旧 grant 不恢复；新选 B 后真实模型再次读文件。最终 R01–R06 还覆盖两轮重登和 A→B | 手工 D2 为同一普通成员；不同账号 D3 由实际 Electron E2E 验证，不等同第二个成员的真实模型全链路 |
| D4 重启/断网/重连 | 连接与恢复有现有单元/前端覆盖，客户端另做了全新启动和退出重登 | 没有完成规范要求的完整实机断网/服务重启/撤权时序矩阵，不能标为全部通过 |
| D5 负向 | 实机工具权限不足时拒绝 client_files，修正测试成员授权后再走正向；E2E Web-only 登录不得铸造原生会话，换账号旧目录授权不得沿用；身份/路径/设备测试仍成立 | 手工未遍历每种 OS 链接替换和连接断开时序 |
| D6 本地+远程 | localhost 手工与 https://127.0.0.1 E2E 成功 | 两者都是本机回环，没有真实远程服务器、安装包、Windows/Linux 证据；用户未提供远程候选环境 |

截图和传输白名单在本目录；原始模型日志、私钥、数据库、签名请求和配置不入库。截图对应树到最终树只有 peer 接缝/追加表/测试的变化，286 个桌面构建文件哈希均相同，最终桌面重登再次通过。

## 复跑命令

以下在隔离候选 repo 根目录执行。日志输出到其父运行目录，测试配置不使用生产数据库。

```bash
.venv/bin/python scripts/check-route-coverage.py
.venv/bin/python scripts/check-web-module-seams.py
.venv/bin/python -m pytest -q -ra -p no:randomly \
  tests/test_sync_report.py tests/test_upstream_core_seams.py \
  tests/test_no_resurrection_legacy_identity.py tests/test_conversation_schema_seam.py \
  tests/test_scheduler_identity_seam.py tests/test_startup_hook_seam.py \
  tests/test_channel_signature_seam.py tests/test_route_registry.py \
  tests/test_http_policy.py tests/test_identity_resource_authorization.py \
  tests/test_scheduler_web_update.py tests/test_upstream_drift_guards.py \
  tests/test_recovered_entry_acceptance.py tests/test_desktop_auth_flow.py \
  tests/test_peer_database_identity.py
.venv/bin/python -c 'from pathlib import Path; import subprocess; files=Path("docs/design/master-sync-20261002/upstream-test-files.txt").read_text().splitlines(); raise SystemExit(subprocess.call([".venv/bin/python","-m","pytest","-q","-ra","-p","no:randomly",*[p for p in files if p.endswith(".py") and Path(p).is_file()]]))'
.venv/bin/python -m pytest -q -ra -p no:randomly tests/test_read_edit_improvements.py tests/test_spreadsheet_uncached_formulas.py
node --test tests/*.cjs
NODE_PATH="$PWD/desktop/node_modules" node --test tests/test_appearance_browser.cjs
node --test tests/test_fork_fragments.cjs tests/test_execution_permission_ui.cjs
npm --prefix desktop run build
desktop/node_modules/.bin/tsc -p desktop/tsconfig.json --noEmit
node desktop/e2e/run-remote-workbench.mjs --spec ./relogin-session-sync.spec.mjs
.venv/bin/python -m ruff check --select F auth/peer_identity.py common/cloud_client.py agent/multiagent/inbound.py tests/test_peer_database_identity.py
```

E2E 需要 runner 文档中的 Python 依赖、Playwright 和已构建 fs-guard。真实 peer 演练另用隔离运行目录的 prepare-peer-acceptance.py 与 run-peer-acceptance.py，私钥由测试专用环境注入，发送端和接收端分别装配真实服务。可审阅的结果在 peer-real-model.json；它不替代真正远程托管中继验收。

## 仍需明确处置

1. 实际远程候选部署、托管中继保留签名字段的端到端验证；完整桌面 D4/D6 和打包平台覆盖。
2. 本表所列同环境基线失败、跳过及历史能力缺口；不得合计为“全绿”。真实渠道/MCP/模型提供方只按实际证据范围声明。
3. 交付目标为 Gitea main 而非 GitHub master；新增上游 GitHub workflow 不证明交付分支已有 required checks。本轮不推送、不改远端保护，暂以人工审阅准入。
4. 用户指定规范要求人工复核后才能提交 merge。本会话用户已于 2026-10-02 在获知上述限制后明确回复“人工复核通过”，批准本地合并；已知失败、跳过及远程验收缺口仍按本记录保留，不因此转为通过。复核候选树和提交范围见 index.md。
