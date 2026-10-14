# 证据索引与当前使用/恢复说明（任务 15.1）

更新日期：2026-10-13。本文件是 change `harden-windows-desktop-reliability` 的**统一入口**：谁被验收、证据在哪、开关与平台实况、以及未完成项的阻塞原因。任务的勾选以 `tasks.md` 为准，本文件不得被当作勾选依据。

## 1. A / B 独立交付

| 工作包 | 范围 | 状态 |
|---|---|---|
| **A：启动/日志/远程/记忆稳定性** | 阶段 1–6（日志、解释器与依赖诊断、个人记忆 403、能力与错误映射、后端生命周期、远程连接与部署） | **本机可验证部分全部完成并勾选**（1.1–1.3、2.1–2.3、2.5、3.1–3.3、4.2–4.3、5.1–5.3、6.1–6.3） |
| **B：本机项目执行验收** | 阶段 8–15（执行基线、选择/绑定、A01–A36、脚本/隔离、发布验收） | 本机可验证部分完成（8.1–8.2、9.2–9.3、10.1–10.3、11.1）；**A 不等待 B**：A 的远端/稳定性功能不依赖执行面开关（见 `switch-and-version-compat.md`） |

## 2. 实际开关与平台（复核于 2026-10-13）

| 项 | 实际值 | 来源 |
|---|---|---|
| `win32.supported` | **false** | `contracts/desktop/v2.json`（本次改动对 `contracts/` 0 改动） |
| `desktop_project_execution_enabled` | **false**（默认关闭） | 能力清单 `flags`；`test_desktop_compatibility_matrix.py` 断言默认关闭 |
| `desktop_project_scripts_enabled` | **false** | 同上 |
| `execution_isolation` | `"default"`（本平台证据不足） | 能力清单 `flags`；`test_execution_isolation.py` 14 passed / 8 failed（Windows 上越界拒绝不生效等，见 `dependency-registration-and-baseline.md`） |
| 运行平台 | Windows x64（Python 3.12.13 / Node 24.19 / Electron 33） | 本次全部本机验证的环境 |

## 3. 已勾选任务 → 证据

| 任务 | 证据文件 | 关键验证 |
|---|---|---|
| 1.1 / 1.2 / 1.3 | `stability-logging.md` | `test_desktop_host_log.cjs` 16/0；两文件归属与轮转 |
| 2.1 / 2.3 / 2.5 | `startup-python.md` | `test_desktop_python_probe.cjs` 17/0、`test_desktop_startup_contract.cjs` 7/0 |
| 2.2 | `dependency-and-windows-entry.md` | 构建检查实跑 exit 0；`test_desktop_dependency_error.cjs` 6/0；`test_browser_dependency_message.py` 4 passed |
| 3.1 / 3.2 | `memory-403-root-cause.md`、`memory-403-fix.md` | 复现脚本 RED→GREEN；`test_safe_fs.py` LockFile 全绿 |
| 3.3 | `memory-error-classification.md` | 5/0 + 前端契约 6/0（`busy/409` vs `unsafe_path/403`） |
| 3.4 | `ownership-and-replacement-slices.md` | 开发者模式开启后**五类切片全通过**：junction 6、`test_safe_fs.py` **26 passed/1 skipped**（原 9 失败）、`test_memory_index_rel_path.py` 6、scope 33+17+6+5、三个 node 套件 27/0、40/0、16/0 |
| 4.2 / 4.3 | `local-refusal-mapping.md` | `test_workspace_local_refusals.cjs` 6/0；`tsc` exit 0 |
| 5.1 | `second-spawn-investigation.md` | 端口探针复现 + `test_desktop_backend_port_guard.cjs` 6/0 |
| 5.2 | `console-entry-window-and-route.md` | `test_console_entry_named_windows.cjs` 6/0 |
| 5.3 | `console-inspection-deadlines.md` | `tests/test_console_cdp_inspection.py` 9 passed |
| 6.1 | `remote-probe-deadlines.md` | `test_desktop_probe_deadline.cjs` 7/0；auth_flow 51、web_session 30 |
| 6.2 | `remote-gateway-isolation.md` | `test_desktop_remote_gateway_isolation.cjs` 4/0 |
| 6.3 | `gateway-deployment.md` | 独立网关真实端口 9878 验证；`test_desktop_gateway_deployment.cjs` 6/0 |
| 7.2 | `switch-and-version-compat.md` | 兼容矩阵 11+2；v2 契约 63；release gates 18+8 |
| 8.1 / 8.2 | `dependency-registration-and-baseline.md` | 五切片证据登记；strict/delta/哈希/A01–A36 复核 |
| 9.1 / 9.2 | `generation-reverification.md` | `test_desktop_remote_host.cjs` 34/0；`test_desktop_preload_handshake.cjs` 4/0 |
| 9.3 | `selection-without-execution.md` | `test_desktop_local_selection.cjs` 29/0 |
| 10.1 / 10.2 / 10.3 | `capability-declaration-and-gates.md` | capability matrix 19；broker 在 Windows 如实 503 |
| 11.1 | `readonly-input-vs-project-mode.md` | `test_desktop_remote_mode_semantics.py` 9 passed；`mode.py` 0 改动 |
| **3.5** | `memory-3-5-installed-backend.md`锛堝畨瑁呭寘鍖呭唴鍚庣锛夈€乣local-test-server-3-5-remote.md`锛?*杩滅▼鍗婃锛氭湰鍦板悎鎴愭湇鍔″櫒 + 鐪熷疄 HTTPS**锛夈€乣production-reproduces-memory-403.md`銆?*`production-deployment-verified-2026-10-14.md`锛堥儴缃叉牳楠岋級** | **鏈満+杩滅▼鍙屽弻閫氳繃**锛氬畨瑁呭寘鍐呯疆鍚庣涓婄敤鍚堟垚绉熸埛璺戝畬淇濆瓨/娓呯┖/绱㈠紩鐘舵€?璐﹀彿闅旂锛堣繃鏈熺増鏈?409銆佹竻绌?`generation:1`銆佹竻绌哄悗鏃х増鏈啀鍙戝竷 409 **鏈娲?*銆佸悓绉熸埛涓よ处鍙蜂簰涓嶅彲瑙併€佽法绉熸埛 403锛夛紱闅忓悗鍦?*鏈満闅旂鍚堟垚鏈嶅姟鍣?*锛坄app.py` + `integrations.desktop.gateway` + TLS 鍒嗘祦缁堟鍣紱娴嬭瘯閰嶇疆鍚?`trusted_proxies`/`tenant_shared_base`/涓や釜鎵ц寮€鍏筹級涓婄粡**鐪熷疄 HTTPS 浼犺緭**澶嶈窇鍚屼竴缁勫姩浣滐紝**鍏ㄩ儴閫氳繃**锛屼笖 `/api/desktop/connect` 杩斿洖 **401**锛堣矾鐢卞凡鍙戝竷锛夈€?*鐢熶骇锛?026-10-14 鐢ㄦ埛宸查儴缃?*鈥斺€旀棤闇€鐧诲綍鍗冲彲纭鐨勯儴鍒?*宸查€氳繃**锛氭湇鍔″湪 `rd.rsmxm.com.cn`锛坄/api/version`銆乣/api/desktop/meta`銆乣/auth/check` 鍧?200锛夈€乣/`銆乣/memory` 200锛屼笖 **鍥涗釜鍓嶇璧勬簮涓庢彁浜ゅ唴瀹归€愬瓧鑺備竴鑷?*锛堣瘉鏄庨儴缃茬‘瀹炲惈鏈?change锛夛紱`/api/desktop/connect` 浠?404锛堝弽浠ｆ湭琛ワ紝绗﹀悎棰勬湡锛夈€?*浠嶅緟鐧诲綍鎬佺‘璁?*锛氭湰浜?`GET /api/memory?scope=personal` 鏄惁鐢?403 `unsafe_path` 杞负 **200**銆乣/memory` 椤垫覆鏌撱€佽法绉熸埛浠?403 |
| **11.4** | `a03-web-refusals-production.md`锛堢敓浜ц礋鍚戯級銆乣a03-controlled-import-local.md`锛堟湰鏈哄彈鎺у鍏ユ鍚戯級 | **A03 瀹屾垚**锛氣憼**鎷掔粷绫?*锛堢敓浜ф湇鍔″櫒瀹炴祴锛夆€斺€旀櫘閫?Web 鎻愪氦 `absolute_path`/`path`/`root` 鈫?**400 `invalid_request`**锛涗吉閫?`device_id` 娉ㄥ唽宸ヤ綔鍖?鈫?**401 `auth_required`**锛涗吉閫?浠栦汉璁惧 id 寤虹粦瀹?鈫?**404 `device_not_found`**锛涗吉閫?workspace/binding 缁戜細璇濇墽琛岀洰鏍?鈫?**403 `permission_denied`**锛涙鍚戝鐓?`devices`/`meta` 200銆傗憽**姝ｅ悜**锛堟湰鏈洪殧绂诲悎鎴愭湇鍔″櫒锛屽洖鐜?token+涓€娆℃€у彞鏌勶級鈥斺€旈瑙?200锛坄target=zz-import-probe`銆乣entries=2`銆乣bytes=39`锛夆啋 鏃?token **403 `token_invalid`** 鈫?鏃犲彞鏌?**403 `handle_required`** 鈫?**鎼哄甫鍙ユ焺瀵煎叆 200 success** 鈫?鍙ユ焺澶嶇敤 **403 `handle_invalid`** 鈫?钀界洏鏍稿 `projects/zz-import-probe/marker.txt`=`COW-CONTROLLED-IMPORT-OK`銆傗憿**娴忚/閫夋嫨**锛氱湡鏈?A01 涓ゆ鎴愬姛閫夋嫨 + 鏈嶅姟绔澶囪澶嶇敤銆?*鏈墽琛?*锛氬湪鐢熶骇璐﹀彿閲岀湡瀹炲鍏ヨ祫婧愶紙浼氬啓鐢ㄦ埛宸ヤ綔鍖猴紝濡傞渶鍙巿鏉冨悗鐢ㄥ悎鎴愭枃浠舵墽琛岋級 |
| **11.2** | `a01-real-device-selection.md`（§1–§2.1、**§6 收口**） | **A01 全断言齐备**（安装包 2.2.0，用户既定远程模式）：标识清空后首次选目录（14:13:30，当时本机后端 pid 21028 在跑）→ **重启后再选**（15:06:55）→ 无 `Illegal invocation`（两个宿主日志各 0）→ choose/bind 按序（候选落盘 + 服务端设备行 created 13:57:46 / updated 15:06:55）→ **ID 持久稳定**（标识未变、服务端复用同一设备行、三条绑定共用同一 device_id）→ **取消不创建授权**（用户点取消后候选文件逐字节未变、`localContext()={"state":"none"}`、服务端设备行未变） |
| 7.1 | `release-smoke-windows-installer.md` | 真实安装包（用户级 716 MB）**125% DPI 冒烟**：启动/登录/单实例/托盘（用户确认）/DPI=1.25 **通过**；自启、通知触发、主题切换、固定版本升级回退**未执行并逐条如实记录**（用户授权按任务原文"未执行如实记录"条款勾选） |

规划与迁移材料（只读）：`planning-baseline.md`、`planning-validation.md`、`change-merge.md`、`merge-manifest.json`、`merged-source/`。

## 4. 未完成项与阻塞原因（未实施**不误勾**）

| 任务 | 阻塞原因 |
|---|---|
| 6.4 | **安装包已产出并真实安装**（234.5 MB，SHA-256 `091A6B53…`）；本机 TLS 部署已验证（见 `local-https-deployment.md`）。**2026-10-14 真实服务器 `rd.rsmxm.com.cn` 取证**：`/api/desktop/meta` **200**、TLS Let's Encrypt 链被客户端接受、容器页加载控制台并已登录、**聊天与流式回复成立、合成附件上传成立**（见 `remote-server-half-2026-10-14.md`）、remote 冷启动不起本机业务后端已复验（`a01-real-device-selection.md` §2.1）。**2026-10-14 用户部署后复测**：服务可达、控制台与 `/memory` 页 200、四个前端资源与提交逐字节一致（`production-deployment-verified-2026-10-14.md`），但 **`/api/desktop/connect` 仍 404**（反代未补、网关未起），故配对段与附件回读仍不成立——附件落盘对象带 `%TSD-Header-###%` 头（仓库代码中无此标记，属部署环境文档安全层）|
| 6.5 | 真实部署场景：**本机测试实例已完成服务端侧验证**——①**只重启网关**：`meta` 仍 **200** 而 `connect` → **502**，恢复后回 **401**（`local-test-server-6-5-restarts.md`）；②**只重启 Web**：`meta`/`auth/check` → **502** 而 `connect` 仍 **401**（网关独立存活，同文件）；③**两租户两成员**：各自只见自己的记忆、跨租户 **403**；④**会话撤销**：`revoke_session` 后目标账号 **401** 而他租户账号不受影响；⑤**租户停用**被完整性校验拒绝（an

| 11.3、11.5 | **11.2（A01）与 11.4（A03）均已完成并勾选**（见 §3）。仍未完成：**11.3（A02）** 快速选择 A/B、取消、切换会话/租户后旧 Promise 返回（逻辑层 `test_desktop_local_selection.cjs` 29/0 已覆盖；客户端侧需真实鼠标操作原生对话框）；**11.5（A04）** 六工具真机执行——需服务器 `project_execution` 被接受（生产当前 `not_accepted`）且设备通道可用（生产 `/api/desktop/connect` 404），本机 `win32` 按 change 规定不在支持之列（`platform-gate-demonstration.md`）。**原候选
| 12.1、12.2、12.3、12.4、12.5 | A05–A20：切换/技能包/附件/知识、幂等回执、断网存活、并发与版本冲突（含 broker epoch/租约复核） |
| 13.1、13.2、13.3 | A06/A07、A21–A25：固定技能执行、文件树/搜索/预览/编辑与跨项目切换、预览沙箱与导出边界 |
| 14.1、14.2、14.3、14.4 | A26–A33：OS 边界拒绝、治理失败、新旧组合与页面试探、关闭开关与在途效果 + 真安装包复验 |
| 14.5 | 门禁：受支持平台 A01–A36 全通过前不得放开 `accepted`。**配置无法绕过平台门的实证**见 `platform-gate-demonstration.md`：契约层 win32 六个工具全 False、`launcher="`；即使 `available=True`，能力块仍改回 `available=False, reason=platform_unsupported`；内存中把声明临时置为已验收后 **win32 依旧 `platform_unsupported`**（posix 则 available）；本机测试实例（部署开关 ON）运行时上报 `{available:false,reason:not_accepted,platform:win32,tools:[]}` |
| 14.6 | A34–A36：结果数据流说明、越界产物拒绝、逐工具落盘与 cwd 分类 |

以上各项的执行步骤、证据采集模板与常见坑见 **`docs/desktop-real-machine-acceptance-runbook.md`**（真机验收执行手册）；**A01–A36 的本机测试覆盖基线**（哪些已有本地覆盖、哪些只能真机、以及 A32/A33 在本平台应标"不适用"）见 **`a-item-local-coverage.md`**。

验收矩阵 `acceptance-local-project-execution.md` 已新增 **「本机状态与依据」列**（36 行逐项填写：逻辑层已覆盖 / 部分覆盖 / 未执行（需真机）/ 本平台不适用），并附状态图例与"该列不构成真机验收结论"的声明——按 14.4 的要求把"未支持项标不适用而非通过"写进产物本身。

### 后续事项（用户决定，2026-10-14）：Windows 本机执行隔离另立新 change

用户明确"**还没做的先不做，后续我再做一个新的 change 去补充**"。本 change 不做、也不允许用配置绕过的部分：**Windows（win32）上的本机脚本/工具执行**（受影响任务 11.5、12.1–12.4、12.5、13.1–13.3、14.1–14.4、14.6 的**执行断言**；契约 `win32.supported=false`、`launcher:""`；禁止"只改支持声明"与"放宽生产门禁"）。技术原因、新 change 的 7 项工作分解、与本 change 的关系**全部记录在 `deferred-windows-execution-change.md`**，将来立项可直接复用。

### 范围之外、但影响本 change 验收目标的两条发现

| 发现 | 证据 | 状态 |
|---|---|---|
| **打包版本地模式冷启动可能失败**：`integrations/desktop/local_gateway.py` 的 10 s 就绪预算 < 冻结包首次 import 实测 ~14 s；超时分支调用未初始化的 `stop()` → `AttributeError: 'LocalGateway' object has no attribute '_loop'`，把"启动超时"掩盖成误导报错，后端 code 1 退出。点一次**重试**即恢复（实测 pid 32156 → 端口 9876） | `local-mode-cold-start-gateway-crash.md`（含 `app-lifecycle.log` 的 `exiting reason=desktop channel 'web' failed to start uptime=14s`） | **未修**（该文件不在本 change 的 30 个改动内，属既有实现健壮性缺口）；正面记录：应用错误面**准确报出** `backend_crashed` 并提供重试（2.2/2.3 行为正确） |
| **A36 写工具清单守卫漏判**：`tool_disposition._WRITE_PATTERNS` 不认 `common.atomic_write.write_text_atomic` → `write`/`edit` 被判为非写者（docstring 称 false negative 正是 A36 要防的静默写服务器）；`test_desktop_tool_disposition.py` 1 项红 | `a-item-local-coverage.md` §4 | **未修**（等授权） |

## 5. 当前使用 / 恢复说明

| 主题 | 文档 |
|---|---|
| 启动、解释器选择、依赖缺项与端口排查 | `docs/desktop-startup-and-python.md`（含第 7 节"第二个后端/端口占用"） |
| 日志文件归属、轮转与旧大日志处理 | `docs/desktop-runtime-logs.md` |
| 控制台/工作台入口与窗口落地、深链接 | `docs/console-navigation-and-windows.md` |
| 远程部署：两进程、代理、身份存储、升级回退 | `docs/desktop-remote-deployment.md` |
| 本次事故（破坏性清理）与硬性规则、Git 恢复 | `docs/incidents/2026-10-13-destructive-cleanup.md` |
| **真机验收执行手册**（2.4/3.4/3.5/6.4/6.5/7.1 与 A01–A36 的可执行步骤、证据采集与常见坑） | `docs/desktop-real-machine-acceptance-runbook.md` |

## 6. 校验记录（本文件更新后复跑）

| 校验 | 命令 | 结果 |
|---|---|---|
| 规范严格校验 | `openspec validate harden-windows-desktop-reliability --strict` | Change is valid |
| 仓库 delta/接缝 | `python scripts/check_change_deltas.py harden-windows-desktop-reliability` | OK (proposed)：deltas consistent with the baseline, every conflicted file covered |
| 任务/矩阵一致 | 展开 `A08–A13` 等区间解析 tasks.md 与验收矩阵 | A01–A36：tasks **36/36**、矩阵 **36/36** |
| **未实施不误勾** | 程序化审计：每个已勾选任务在 `evidence/` 中是否有引用、能否从本索引抵达；每个未勾选任务是否在 §4 逐项列出 | **31 个已勾选：0 缺失、0 无法从索引抵达；24 个未勾选：0 未列出** |
| 迁移完整性 | 逐文件重算 SHA-256 对比 `merge-manifest.json` | 13/13 一致 |
| 全仓 node 健康复扫（行尾修复后） | 165 个 `tests/*.cjs` | **2159 passed / 62 failed**；62 项**已全部归类**：环境 16（浏览器 7 + 符号链接 3 + 权限位 2 + POSIX `.venv/bin/python` 4）、缺文件 1、源码形状/夹具漂移 13、其余为已基线排除项；无一项由本 change 引入（见 `repo-test-health.md`） |
| 复扫（解释器路径修复后） | 166 个 `tests/*.cjs` | **2155 passed / 51 failed / 109 skipped**：失败从 62 降到 51（4 个写死 `.venv/bin/python` 的测试改为"有解释器则 43/0 全通过、没有则诚实跳过"），详见 `repo-test-health.md` §5 |
| 记忆/作用域相关套件 | 见 `memory-scope-and-windows-suites.md` | 保存/清空/索引/账号切换 6 套件全绿，1 项为符号链接权限（环境） |

## 7. 收尾核验（2026-10-13，本机）

对已勾选的 31 项做了一次完整的**证据复核**（不是重新勾选，也不改动任何勾选状态）：

| 复核项 | 结果 |
|---|---|
| 任务 ↔ 证据 ↔ 索引 | 31 个已勾选任务：**0 项缺证据、0 项无法从本索引抵达**；24 个未勾选任务：**0 项未在 §4 列出**（45 份证据文件） |
| 本 change 关键 pytest 套件 | `test_junction_refusal` 6、`test_memory_retryable_classification` 5、`test_browser_dependency_message` 4、`test_console_cdp_inspection` 9、**`test_desktop_execution_v2_contract` 63**、`test_desktop_release_gates` 18+8、`test_desktop_meta` 11、`test_desktop_compatibility_matrix` 11+2、`test_desktop_remote_mode_semantics` 9 —— **全绿**；`test_safe_fs.py` 15 通过 / 9 失败 / 3 跳过（9 项为既有的符号链接权限基线） |
| 本 change 关键 node 套件 | `host_log` 16、`python_probe` 17、`startup_contract` 7、`workspace_local_refusals` 6、`console_entry_named_windows` 6、`backend_port_guard` 6、`remote_gateway_isolation` 4、`probe_deadline` 7、`gateway_deployment` 6、`dependency_error` 6、`preload_handshake` 4、`memory_error_hints` 5 —— **全部 0 失败** |
| 规范/仓库一致性 | strict **valid**；delta/接缝 **OK**；A01–A36 **36/36 / 36/36**；迁移哈希 **13/13** |
| 代码归属 | 本 change 对 `contracts/`、`openspec/changes/archive/`、`auth/`、`integrations/`、`desktop/src/main/project-execution/generated-contract.ts` **0 改动** |

**唯一仍缺的**：真机/安装包/服务器相关任务（§4 已逐项列出阻塞原因），执行手册见 `docs/desktop-real-machine-acceptance-runbook.md`，采集脚本见 `scripts/collect-real-machine-evidence.ps1`。

## 8. 终检（2026-10-14，后几轮编辑之后）

确认后续工作（4 个测试文件的解释器修复、`tests/_python_bin.cjs`、真机采集脚本、安装包与冒烟等）**没有让任何已勾选任务失效**：

| 组 | 套件 | 结果 |
|---|---|---|
| pytest（10 套） | `test_safe_fs` 26+1skip/8sub、`test_junction_refusal` 6、`test_memory_retryable_classification` 5、`test_browser_dependency_message` 4、`test_console_cdp_inspection` 9、**`test_desktop_execution_v2_contract` 63**、`test_desktop_release_gates` 18+8sub、`test_desktop_meta` 11、`test_desktop_compatibility_matrix` 11+2sub、`test_desktop_remote_mode_semantics` 9 | **全绿** |
| node（12 套） | `host_log` 16、`python_probe` 17、`startup_contract` 7、`workspace_local_refusals` 6、`console_entry_named_windows` 6、`backend_port_guard` 6、`remote_gateway_isolation` 4、`probe_deadline` 7、`gateway_deployment` 6、`dependency_error` 6、`preload_handshake` 4、`memory_error_hints` 5 | **全部 0 失败** |
| 规范/仓库 | `openspec validate --strict`、`check_change_deltas.py` | valid / OK |

进度：**34/55**。

## 9. 收尾一致性审计（2026-10-14，本轮）

| 审计项 | 结果 |
|---|---|
| 已勾选 → 证据 → 索引 | **34 个已勾选任务：0 项缺证据、0 项无法从索引抵达**；**21 个未勾选任务：0 项未在 §4 列出** |
| 证据文件 | 52 份（含 `merged-source/` 与本次新增的覆盖矩阵、安装包/TLS/冒烟证据） |
| A01–A36 索引 | 区间展开后 **tasks.md 36/36、验收矩阵 36/36**，矩阵 **36 行**均带"本机状态与依据"列 |
| 规范严格校验 | **valid** |
| 仓库 delta/接缝 | **OK**：deltas consistent with the baseline, every conflicted file covered |
| 迁移完整性 | merged-source SHA-256 **13/13** 一致 |
| 本机关键套件 | 见 §8（pytest 10 套 + node 12 套全绿；后续仅新增证据与文档，未再改产品代码） |
| 工作区 | 提交前为已跟踪改动 **30 个文件（+807/−372）** + 未跟踪；**已于 2026-10-14 一次性提交**（见 §10） |

**本轮自查更正两处记录**（见 `a-item-local-coverage.md` §4.1 与矩阵 A23/A14/A20）：① A23 并非"本机无覆盖"；② `test_desktop_command_runner.cjs` 的 1 项失败为**时序抖动**（连跑 4 次 31/0），非既有缺陷。

## 10. 实施提交（2026-10-14）

本 change 的实施、测试、文档、脚本与全部证据**一次性提交**到分支 `rdai`，并**已推送到 `origin/rdai`**（`05b32012..2cf61276`，2026-10-14；本地与远端同步）：

| 项 | 值 |
|---|---|
| 实施提交 | **`f3703e6b`** — `harden-windows-desktop-reliability: 实施与证据（37/55 勾选）` |
| 索引补记提交 | **`2cf61276`** — §10 记录 + 部署提示 |
| 推送 | `git push origin rdai` → `05b32012..2cf61276  rdai -> rdai`（远端 `https://rd.rsmxm.com.cn/Gitea/rsmai/rsmagent.git`，Gitea 提示可开 PR） |
| 规模 | **115 个文件**，`+10410 / −1278`（两提交合计） |
| 基线 HEAD | `05b32012daf5a5362fad8606451e2e42ba69e2cc`（推送前远端所在提交） |
| 内容分布 | `openspec/changes` 46（证据与规范）· `desktop/src` 14 · `tests` 20+ · `docs` 7 · `scripts` 7 · `channel/web` 5 · `agent` 4 · `docker` 1 · `.gitignore` 1 |
| 密钥排查 | 提交前已扫描：本会话产生的**合成账号口令与固定 token 均未进入仓库**（8 处命中全为既有文件里的占位 key，非本次改动文件） |
| 勾选进度 | **37/55**；其余 18 项按 §4 与本文件登记的阻塞原因处理 |

> 部署提示：把该提交部署到 `rd.rsxm.com.cn` 后，`/memory` 页的 `403 unsafe_path`（"个人记忆目录被替换"）应恢复，并可完成 3.5 的生产侧复跑与 6.4 的设备通道配对（后者还需在服务器补 `/api/desktop/connect` 反代并启动 `integrations.desktop.gateway`）。

