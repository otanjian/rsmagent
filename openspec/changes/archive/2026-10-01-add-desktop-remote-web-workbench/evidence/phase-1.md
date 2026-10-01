# 阶段一验收证据（task 6.2）

本文件记录**实际执行过**的阶段一矩阵结果，以及**未执行**项及原因。语言沿用 acceptance.md
的口径：只写真实跑过的命令与观察到的结果，未执行的命令不记为通过。

**状态：部分执行。** §2/§3/§4 的自动化部分已跑通；§5 列出的打包客户端矩阵（逐平台安装、
容器内逐页深路径、安装升级/签名）尚未执行，因此 6.2 未勾选，`desktop_remote_web_enabled`
保持 `False`。

## 1. 环境、版本与身份

| 项 | 实际值 |
| --- | --- |
| 仓库分支 / 提交 | `rdai`，HEAD `05587992`（本 change 尚未提交，工作树含改动） |
| 客户端版本 | `desktop/package.json` `cowagent-desktop@2.1.9`，实际 Electron **33.4.11** |
| OS / 架构 | macOS **26.4** / **arm64**（本机唯一可用平台；Windows x64 未执行，见 §5） |
| Node / Python | Node **v24.14.1** / Python **3.14.3**（`.venv/bin/python`） |
| 服务器侧 | 本 change 的 `desktop/e2e/serve-fixture.py`：真实 `build_web_app()` over HTTPS |
| 数据库 | 临时 `identity.db`（`WebAppHarness`），不含开发者真实身份库 |
| 测试 CA | 自签证书；仅测试进程按**公钥 SPKI 固定**信任（`main.e2e.cjs`），非"忽略证书错误"，本机信任库未改动 |
| 四个新开关 | `desktop_remote_web_enabled` / `desktop_local_files_enabled` / `desktop_native_notifications_enabled` / `desktop_local_processing_enabled` **全部默认 `False`**（`config.py`） |

合成身份（无真实客户数据）：

- 浏览器侧（`tests/desktop_identities.py`）：`PA`（平台管理员）、`U1`/`U2`（租户 A 成员）、`TA`
  （租户管理员）、`UP`（无权限成员）、`FB`（租户 B 成员）、`ANON`；`U0`（零租户账号）由既有
  "仅账号"会话路径覆盖，见 `evidence/test-identities.md`。
- 桌面 E2E（`serve-fixture.py`）：`e2e-u1`（member + 聊天菜单 + 模型 `use` 授权）、`e2e-u2`、
  `e2e-ta`（tenant_admin）、`e2e-pa`（平台管理员），以及 u1 同属的第二个租户 `beta`。

## 2. 执行命令与结果（全部实跑）

| # | 命令 | 实际结果 |
| --- | --- | --- |
| 1 | `.venv/bin/python -m pytest tests/test_desktop_auth_flow.py tests/test_desktop_external_broker.py tests/test_safe_fs.py tests/test_agent_user_file_http.py tests/test_private_agent_file_scope.py tests/test_desktop_web_session.py tests/test_desktop_contracts.py tests/test_desktop_identities.py tests/test_desktop_meta.py tests/test_desktop_web_pages.py tests/test_desktop_macos_permissions.py tests/test_desktop_build_arch.py tests/test_desktop_migration_drill.py -q -p no:randomly` | **214 passed, 8 subtests passed**（28.41s；末两位为本 change 新增的打包配置守卫与迁移演练，见 §7/§9） |
| 2 | `node --test tests/test_desktop_core_integration.cjs tests/test_desktop_context_frontend.cjs tests/test_desktop_external_broker.cjs tests/test_desktop_scheduler_poll.cjs tests/test_desktop_remote_config.cjs tests/test_desktop_remote_host.cjs tests/test_desktop_web_session_bridge.cjs tests/test_desktop_downloads.cjs tests/test_desktop_host_frontend.cjs` | **186 pass / 0 fail** |
| 3 | `.venv/bin/python scripts/check-route-coverage.py` | `206 routes (68 upstream, 138 fork), 253 method entries` → OK |
| 4 | `.venv/bin/python scripts/check-web-module-seams.py` | `OK: 22 upstream module(s), 311 fork-only symbol(s), 0 findings` |
| 5 | `npm --prefix desktop run build` | renderer 2135 modules built（1.56s）+ `tsc -p tsconfig.main.json` 无错 |
| 5b | `.venv/bin/python -m pytest tests/test_identity_store.py tests/test_identity_migration_drill.py tests/test_console_migration_drill.py tests/test_migration_recovery_acceptance.py tests/test_conversation_schema_seam.py tests/test_session_store_resolution.py tests/test_desktop_web_session.py tests/test_capability_matrix.py -q -p no:randomly` | **95 passed**（5.37s）——因编辑了 `auth/store.py` 的 docstring，对身份/会话面加跑一遍确认无回归 |
| 6 | `openspec validate add-desktop-remote-web-workbench --strict` | `Change ... is valid` |
| 7 | `node desktop/e2e/run-remote-workbench.mjs` | **17/17 通过**（约 12s），含容器内流式对话、租户切换、真实上传、W20 导航/媒体、父子双端撤销、重启远程复授权、切回本地 |

E2E 驱动的是**产品自身的编译产物**（`dist/main/index.js` 由 `main.e2e.cjs` 加载；差异仅三点测试替身：
私有 profile、按 SPKI 固定测试证书、`shell.openExternal` 落盘），不是模拟服务器或替身容器。

## 3. W01–W20 行结果

浏览器侧证据统一为 `tests/test_desktop_web_pages.py`（本 change 的页面审计）：逐行断言该行
**有真实签发页 + 真实容器视图**，且各角色**可达或按投影自己的词表诚实拒绝**（拒绝不得报成可用），
并枚举 `window.open` / 同帧导航调用点以确认容器能承载。

| ID | 浏览器侧（服务端/页面） | 桌面侧（容器内） | 实际结果 |
| --- | --- | --- | --- |
| W01 普通对话 | 页面/视图存在，`member` 可达（同文件 W_ROWS） | 新建 + 真实流式：E2E `a chat turn streams token by token from the real upstream`（真上游逐 token，非一次性 blob）与 `switching tenant ...`（generation 阻断旧响应） | ✅ 关键路径通过；停止/刷新恢复/上下文压缩未在容器内执行 |
| W02 多智能体 | 同 `workbench.chat` 视图，可达/拒绝已断言 | 未执行（容器内逐页深路径无自动化） | ⚠️ 可达性通过，深路径未执行 |
| W03 会话历史 | `workbench.history` 签发 + 视图存在 | 未执行 | ⚠️ 同上 |
| W04 智能体工作台 | `workbench.agents` 签发 + 视图存在 | 未执行 | ⚠️ 同上 |
| W05 智能体管理 | `admin.agents`（`tenant_admin`）可达，`member` 诚实拒绝 | 未执行 | ⚠️ 同上 |
| W06 场景应用 | `workbench.scenes` 签发 + 视图存在；业务服务缺失时按投影说明 | 未执行 | ⚠️ 同上（业务服务缺失项应记"正确不可用"） |
| W07 知识库 | `workbench.knowledge` 签发 + 视图存在 | 未执行 | ⚠️ 同上 |
| W08 待办 | `workbench.todos` 签发 + 视图存在 | 未执行 | ⚠️ 同上 |
| W09 技能与记忆 | `admin.skills`/`admin.memory`（`tenant_admin`） | 未执行 | ⚠️ 同上 |
| W10 模型与配置 | `admin.models`（`tenant_admin`）；密钥掩码由既有配置页行为覆盖 | 未执行 | ⚠️ 同上（见 §6 模型目录发现） |
| W11 消息渠道 | `admin.channels` 签发 + 视图存在 | 未执行 | ⚠️ 同上 |
| W12 系统接入 | `admin.external_connections` 签发 + 视图存在 | 未执行 | ⚠️ 同上 |
| W13 定时任务 | `workbench.schedules` 签发 + 视图存在 | 未执行 | ⚠️ 同上 |
| W14 账户与偏好 | 无独立签发页（账户菜单），强制改密 gate：`test_a_forced_password_change_is_a_gate_and_not_a_broken_shell` | 本地模式登录/改密向导：E2E `a fresh profile boots local, ... signs in through the browser` | ✅ 登录/改密 gate 与本地壳通过；资料/头像/语言主题未在容器内执行 |
| W15 组织权限 | `admin.members`/`admin.roles`/`admin.organization`（`tenant_admin`），`member` 拒绝 | 未执行 | ⚠️ 同上 |
| W16 平台与租户 | `admin.tenants`（`platform_admin`）；零租户 `PA` 由 `test_desktop_identities.py` 覆盖 | 未执行 | ⚠️ 同上 |
| W17 运维 | `admin.logs`/`admin.audit`/`admin.token_usage`（`tenant_admin`） | 未执行 | ⚠️ 同上 |
| W18 品牌 | `admin.branding`（`platform_admin`） | 未执行 | ⚠️ 同上 |
| W19 附件与文件 | `test_desktop_web_pages.py` 的 W19 组（页面用自己的文件对话框、页面不得自持句柄、落地文件型 URL 已声明） | E2E `a same-origin upload through the page file input reaches the server`（`#file-input` 真实字节落盘）＋`tests/test_desktop_downloads.cjs` 30 例（另存为策略、artifact_ref 分类、路径穿越拒绝） | ✅ 上传与另存为策略通过；目录上传/拖拽/预览/取消在容器内未执行 |
| W20 容器与路由 | `test_desktop_web_pages.py` 的适配器/popup/导航调用点枚举 | E2E 三条：外链交系统浏览器且壳不变、站外导航被拒、页面媒体权限被拒；`tests/test_desktop_remote_host.cjs` 28 例（同帧路由空间、off-origin 分类、内容窗口隔离、对话框限流、无媒体/设备权限） | ✅ 拒绝与分类通过；媒体权限"恢复"未执行（当前策略恒为 deny） |

"⚠️ 可达性通过、深路径未执行"的含义：该行在**服务端投影与页面装载**层面已被真实断言（页面确实
签发、视图确实在壳内、角色可达或诚实拒绝），但 acceptance.md 要求的"最低必测路径"（如 W03 的
分页/搜索/重命名、W11 的扫码界面、W17 的审计筛选）**没有**在容器里执行，也没有自动化脚本；
这类行必须在打包客户端上人工执行后才能记通过。

## 4. A01–A12 行结果

| ID | 场景 | 证据与命令 | 实际结果 |
| --- | --- | --- | --- |
| A01 | PKCE code 重放、错误 verifier/state、过期、撤销 | `tests/test_desktop_web_session.py`（`test_replaying_a_bootstrap_id_is_refused`、`test_the_bootstrap_id_is_single_use_on_the_wire`、origin/instance/protocol 拒绝）＋E2E `entering remote mode authorizes this client and attaches the server console` | ✅ |
| A02 | 普通 Cookie/Bearer 冒充 Web 引导 | `test_a_plain_login_token_cannot_bootstrap`、`test_a_plain_login_bearer_is_refused`、`test_a_cookie_cannot_stand_in_for_the_native_bearer` | ✅ |
| A03 | 两 worker 同 bootstrap ID；响应丢失后新 ID | `tests/test_desktop_web_session.py::test_concurrent_bootstraps_leave_one_live_child`、`test_a_new_bootstrap_id_replaces_the_previous_child`（部分唯一索引 `idx_desktop_web_links_active`） | ✅ |
| A04 | 父会话撤销后旧子 Cookie | `test_child_stops_when_parent_is_revoked` ＋E2E `revoking the child session server-side stops business requests, not just a probe`（业务请求而非仅 auth/check） | ✅ |
| A05 | Web 退出、壳退出、改密、退出时断网 | `test_the_native_logout_revokes_the_paired_child`、`test_child_revocation_tears_down_the_parent`、`test_password_change_revokes_the_pair`、`test_an_independent_browser_session_survives_the_pair_logout`；离线撤销不谎称完成见 `tests/test_desktop_remote_config.cjs`（撤销失败置 `blockedReason`）＋E2E `detaching ends the pairing on both sides`（轮询到服务端 `revoked_at` 落库） | ✅ |
| A06 | U1(A)→U1(B)→U1(A)，旧响应迟到 | E2E `switching tenant is a one-shot address that replaces the previous document context`（导航 generation 递增、一次性参数剥离）＋`tests/test_desktop_remote_host.cjs::a late call from a replaced document is a stale_context refusal` | ✅ |
| A07 | U1→U2、S1→S2、重启应用 | `tests/test_desktop_remote_config.cjs`（更高版本配置被拒且不覆盖、单活动服务器、`setBackendOrigin` 清 session/中止在途/销毁分区）＋E2E `the next launch boots remote without the local backend and requires a fresh authorization` | ✅ |
| A08 | iframe/弹窗/预览 HTML/同源非 shell/外站调用桥 | `tests/test_desktop_remote_host.cjs`（另一 webContents/iframe/弹窗拒绝、同源内容页不得借桥、预览面永不给 preload、外站 origin 拒绝）＋E2E `the container runs the server console with the narrow bridge and nothing wider`、`a page cannot forge the bridge or reach a native capability it was not given` | ✅ |
| A09 | TLS 错误、HTTP 地址、含用户信息/前缀路径 | `tests/test_desktop_remote_config.cjs`（HTTP 在请求前拒绝、TLS 归类为 `tls` 而非瞬时、userinfo/query/fragment 逐项拒绝）＋E2E `a plain HTTP address is refused and never stored`、`the exact HTTPS origin is probed and stored` | ✅ |
| A10 | 缺协议、旧 Electron、未知 bridge major | `tests/test_desktop_remote_config.cjs`（必需协议 major 不符＝硬不兼容、可选能力单独关闭、旧 Electron 线强制回本地并给原因）＋E2E 远程启动未起本地后端 | ✅ |
| A11 | 秘密扫描 | `tests/test_desktop_web_session_bridge.cjs`（秘密只作 Cookie、正文禁含秘密）、`tests/test_desktop_remote_host.cjs`（`a bridge call carries the method, its params and the generation only`、`no call ever smuggles a secret token into the payload`）、`tests/test_desktop_web_session.py`（`test_ids_and_secret_never_land_in_the_body`、`test_status_never_exposes_a_secret`） | ✅ |
| A12 | 本地模式回归 | E2E `a fresh profile boots local, runs the bundled backend and signs in through the browser`、`switching back to local restores the bundled backend on the next launch`（下次启动不再出现 `[remote] remote mode`，并要求重新登录）＋`tests/test_desktop_remote_config.cjs`（本地模式保持运行中的后端可达） | ✅（本机 macOS arm64；其他平台未执行） |

## 5. 未执行项与阻塞（不记为通过）

1. **打包客户端矩阵（6.2 主项）**：未执行。本机没有本 change 的发行包；官方打包链路是
   `.github/workflows/release.yml`（PyInstaller onedir 后端 → `electron-builder --config
   electron-builder.js`，随后才签名/公证），本机 `.venv` 为 Python 3.14 且未安装 PyInstaller，
   本地临时构建既非签名发行包也不满足"实际发行包"口径，故**不做**。
2. **Windows 10/11 x64**：未执行（无该平台机器）。acceptance.md 要求完整矩阵在
   Windows 11 x64 与 macOS arm64 必跑，Windows 侧只能由 release pipeline 的 win 产物补齐。
3. **容器内逐页深路径（W02–W18 的最低必测路径）**：未执行，无自动化脚本，需在打包客户端上人工执行。
4. **安装 / 升级 / 回退 / 签名（L08、14.5）与通知（L01–L04）**：属阶段三，保持未勾选。
5. **打包客户端上的远程腿**：即使本机打包，HTTPS 控制台需要**装入测试机信任库的测试 CA**
   （acceptance.md §1），本 change 的 E2E 用的是仅测试进程生效的 SPKI 固定；静默改本机信任库不是
   可接受的自动化动作，因此该腿留给打包矩阵执行。
6. **阶段二/三能力**：`desktop_local_files_enabled`、`desktop_native_notifications_enabled`、
   `desktop_local_processing_enabled` 全为 `False`，`tests/test_desktop_identities.py` 断言身份齐备时
   四个切片仍为关闭，属其他 change 的"正确不可用"。

## 6. 本次执行暴露的遗留问题（均在本 change 未改动的文件内）

1. `auth/policy.py` `BUILTIN_MENU_DEFAULTS` 的 `member` 不含 `workbench.chat`：任何携带菜单授权的
   自定义角色都会把投影切到"菜单绑定"规则，普通成员打开 `/chat` 得到 `menu_denied`（页面显示"无权
   访问"）。E2E 夹具按租户管理员的真实写路径补授该菜单（`_grant_chat_menu`），未改动该默认值。
2. `channel/web/fork/runtime.py::_session_model_catalog()` 不投影 legacy `custom_api_base` 供应商：
   控制台"模型授权"目录为空，操作者无法从 UI 授权夹具所用模型；夹具按 `RESOURCE_NAMESPACES` 的
   命名空间规则构造 `provider:custom:<code>`，并同时完成平台分配与租户角色授权
   （`serve-fixture.py::_allocate_model` / `_grant_model_use`）。未授权时对话被
   `bridge/agent_bridge.py::_require_model_use` 拒绝为 "No model is currently authorized for you"。
3. 桌面 E2E 夹具自身的三处问题已在本 change 内修掉（见 tasks.md 5.8）：数据根与租户根重叠、
   假上游缺 chunked 分帧导致整段返回、断开后的配对撤销断言未轮询。

## 7. 本次执行中修复的打包缺陷（`desktop/package.json` 架构钉死）

执行 6.2 的打包链路复核时发现本 change 的工作树把 `desktop/package.json` 的 `build.mac.target`
从 `["dmg", "zip"]` 改成了逐项钉死架构的写法：

```json
"target": [
  { "target": "dmg", "arch": ["x64", "arm64"] },
  { "target": "zip", "arch": ["x64", "arm64"] }
]
```

**为什么这是缺陷（源码级核对，不是推断）**：`app-builder-lib@25.1.8` 的
`out/targets/targetFactory.js::computeArchToTargetNamesMap()` 只在"每个 target 列表都为空"时原样返回
命令行架构映射（`if (targetNames.length > 0) return raw`）；一旦 `mac.target` 的某一项带 `arch` 数组，
该数组会被**合并进来且只增不减**。而 release workflow 的每个 macOS job 都是用 CLI 架构开关选架构
（`--mac --arm64` / `--mac --x64`，release.yml:47-53），因此钉死架构会让**每个 job 都构建 x64 与 arm64
两份**，CLI 开关被静默忽略。这与 `desktop/electron-builder.js` 头部自己写下的不变量直接冲突
（"Never pin `arch` on mac.target in package.json：an arch listed there wins over the --arm64/--x64 CLI flag,
so every runner would build every arch and pair a foreign shell with the backend PyInstaller just built for
the host"）。

**实际后果**：PyInstaller 后端是按 job 的宿主架构构建的，而 workflow 的 "Verify backend architecture"
只校验 `desktop/build/dist` 里的后端（release.yml:98-113），不校验每个 `.app` 内实际打进去的后端。于是
同一 job 产出的两份产物中有一份是"外壳架构 ≠ 后端架构"——外壳无法拉起自己的后端；同时每个 macOS job
的构建时间与下载量翻倍。这正是 6.2 要交付的发行包，因此必须修。

**修复**：把 `mac.target` 还原为 `["dmg", "zip"]`，架构继续由 release workflow 的逐 job CLI 开关决定；
还原后该文件与 `HEAD` 完全一致（`git diff desktop/package.json` 为空），本 change 不再改动它。

**防回归**：新增 `tests/test_desktop_build_arch.py`（4 例，纯配置断言，不进 `.app`）：

1. `build.mac.target` 的每一项都不得带 `arch`（含 `"dmg:arm64"` 这种后缀写法）；
2. 经 Node 实际加载 `desktop/electron-builder.js`，确认解析后的 `mac.target` 同样没有架构钉死；
3. release workflow 的每个 `platform: mac` job 必须且只能选一个架构开关，且与 `matrix.arch` 一致；
4. `macos-14`/`macos-15-intel` 两个已登记架构仍都被覆盖。

第 1、2 例在**注入**该缺陷后确实变红（实测 `2 failed`，报错文本含"ship a foreign-arch backend inside
one of them"），还原后 4 例全绿——即该守卫能抓住这次的回归，而不是恒真断言。

**顺带观察（未改动）**：`build.win.target` 在 `HEAD` 上就钉着 `arch: ["x64"]`，按同一条优先级规则它会
覆盖 `--win --arm64`；目前 workflow 只跑 `--win --x64`，结果一致，故属既有配置、非本 change 引入，
保持原样未改。

## 8. 打包可达性核对（本轮静态核对，为 6.2 的打包矩阵排雷）

为避免 6.2 在打包客户端上才发现"模块没进包"，本轮按"本 change 新增/改动的每个文件，在
PyInstaller onedir 后端与 electron-builder 客户端里是否可达"逐个核对（`desktop/build/cowagent-backend.spec`
的 `hiddenimports`/`datas` + `desktop/package.json` 的 `files`/`extraResources`）：

| 新增物 | 打包路径 | 结论 |
| --- | --- | --- |
| `channel/web/fork/handlers/desktop.py` | `spec` 只按目录收集 `channel/web/api`、`channel/web/core`，**不含** `fork/`；但 `channel/web/web_channel.py` 在模块层**静态** `from channel.web.fork.handlers.desktop import (...)`，而 `web_channel` 本身在 `hiddenimports` 里，因此 PyInstaller 会顺静态导入链收进包 | ✅ 可达（曾疑为漏收，核对后被否证） |
| `auth/desktop_web_session.py` | `channel/web/auth_handlers.py` 内以字面量 `from auth.desktop_web_session import ...` 静态导入，字节码分析可识别 | ✅ 可达 |
| `channel/web/static/js/fork/desktop-host.js` | `datas` 已含 `channel/web/static`；`chat.html` 的 `<script defer src="assets/js/fork/desktop-host.js">` 随 `channel/web/chat.html` 一并打包 | ✅ 可达 |
| `auth/desktop_contracts.py` + `contracts/desktop/v1.json` | `contracts/` **不在** `datas`，因此**不会**进后端包；但两者只在测试里被导入（产品代码零引用），且 `load_contract()` 的模块级载入只发生在测试进程中，故不影响运行 | ✅ 不构成缺陷（见下条说明） |

关于契约文件的落地口径（本轮已核实，便于 6.2 不再重复排查）：`contracts/desktop/v1.json` 是**规范真值**，
两侧实现各自**硬编码**常量、由测试断言二者与契约一致（如 `desktop/src/main/remote/host-bridge.ts` 的
`BRIDGE_MAJOR` 对 `contracts bridge.version`，`tests/test_desktop_remote_config.cjs` 读 JSON 比对），
而**不是**运行时读该 JSON。这是刻意的：打包后的客户端不便在运行时读取仓库内 JSON，测试期比对既保证了
单一真值、又不把数据文件拖进发行包。故 `contracts/` 不入包是正确的，`v1.json` 里 "both read this file"
的措辞指的是"两侧实现以该文件为准并被测试比对"，不是运行时读取。

## 9. 迁移演练（6.3 的后端可自动化部分，本轮新增）

6.3 原先整条标为"待执行"，但其**后端一半并不依赖签名发行包或第二个平台**——空库/旧库升级、
迁移中断重跑、"既有业务数据不搬迁"都可以对真实 store 演练。本轮补齐为
`tests/test_desktop_migration_drill.py`（13 例，全部使用临时 `identity.db`，不触碰真实数据）：

| 性质 | 用例 | 断言要点 |
| --- | --- | --- |
| 空库升级 | `EmptyStoreUpgradeTests` | 新库自动建出 `desktop_native_origins` / `desktop_web_links`；migration 36 marker **恰好一条**；冻结到 v35 时两表**尚不存在**（防止"36 什么都没做也算通过"） |
| 建表者归属 | 同上 | `idx_desktop_web_links_active` 确为 `UNIQUE ... WHERE revoked_at IS NULL`（不是普通索引） |
| 旧库升级 | `OldStoreUpgradeTests` | 冻结在 v35 的库开库后补上 v36；`users`/`tenants`/`memberships`/`auth_sessions` 的 **id 集合升级前后完全相等**（业务数据不搬迁） |
| 不推断来源 | 同上 | 升级一个**已有原生会话**的旧库后，`desktop_native_origins` 必须为 **0 行**——task 3.1 明确禁止从旧 User-Agent 推断来源，否则伪造来源与已验来源无法区分 |
| 中断重跑 | `InterruptedRerunTests` | "表已建、marker 未提交"的危险态由下次开库修复（`CREATE TABLE IF NOT EXISTS` 的幂等前提）；重复开库不重放、不重复 marker；中断态下旧数据仍完好且升级最终完成 |
| 约束契约 | `DesktopLinkConstraintTests` | 活动子会话唯一、撤销后释放槽位、父会话删除级联删除 link |

### 9.1 本轮由此发现并修正的一处文档失真

写"bootstrap id 单次消费"的 DB 级断言时，真实 schema **没有** `UNIQUE(native_session_id, bootstrap_id)`，
但 `auth/store._migration_36` 的 docstring 声称有。核对后确认**行为是对的、docstring 是错的**：

- 单次消费由 `auth/desktop_web_session.py::bootstrap` 的一次 `SELECT ... WHERE native_session_id=? AND bootstrap_id=?`
  （**不排除已撤销行**）实现，命中即 409 `bootstrap_consumed` 并记审计——这样错误码才是可区分的
  `bootstrap_consumed`，而不是笼统的约束冲突；
- 并发两个引导由部分唯一索引裁决：败者的 `INSERT` 被拒并转成"another bootstrap is active"，**不会**留下两个活动子会话。

即 `desktop_web_session.py` 的模块 docstring（"partial unique index ... is the actual guard, not a
check-then-insert"）本来就说对了，是 `store.py` 的迁移 docstring 把职责写错了。本轮只改**注释**（无代码行为变更），
把这个区别写清楚，避免后来者以为数据库保证单次消费、进而删掉那次 `SELECT`。测试也相应断言**真实机制**
（活动期重放被部分唯一索引拦下），而不是我最初误设的约束。

### 9.2 仍未执行的部分

已发布发行包的**真实安装 / 升级 / 回退**演练（17.1–17.3 部分）仍受 6.2 打包矩阵阻塞；开启
`desktop_remote_web_enabled` 的批准仍属运维＋负责人。

## 10. 结论

- 阶段一**可自动化的部分全部通过**（§2 八条命令），E2E 17/17，无产品代码返工。
- 阶段一矩阵仍有 §5 的未执行项，**6.2 不勾选**；`desktop_remote_web_enabled` 保持 `False`，
  6.3 的开启门槛（完整功能矩阵 + 原生认证切片证据 + 逐平台安装证据）尚未满足。
- §7 的打包缺陷已修复并加了防回归守卫，`desktop/package.json` 相对 `HEAD` 不再有改动。
- §8 的打包可达性核对未发现新的漏收项；该项核对结论可直接复用于 6.2 的打包矩阵。
- §9 把 6.3 的后端一半（空库/旧库/中断重跑/数据不搬迁/不推断来源）从"待执行"变成了可自动化证据，
  并纠正了一处会误导后来者的迁移 docstring；**6.3 整体仍不勾选**，因为已发布包的真实安装/回退
  演练与开关开启批准仍未完成。
