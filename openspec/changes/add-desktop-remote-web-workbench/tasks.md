## 1. 实施基线与契约固定（所有阶段前置）

- [x] 1.1 【负责人】记录实施起点 SHA、身份库 schema、Electron/Node/Python 版本、Web classic/split 装配方式与相关活跃 change；按 acceptance.md W01–W20 固定实际发布的功能基线，未开放能力记录原因。（见 `evidence/baseline.md`）
- [x] 1.2 【测试】建立 A/B 租户、U1/U2/TA/PA/U0/UP 合成测试数据与独立客户端配置目录；记录各角色获准操作，不读取开发者个人凭据或真实业务数据。（见 `tests/desktop_identities.py`、`tests/test_desktop_identities.py`、`evidence/test-identities.md`；U0 由既有仅账号会话路径覆盖）
- [x] 1.3 【后端＋桌面】把 contracts.md 的版本、错误码、source_ref、binding、command、transfer、processor schema 写为可校验的契约和跨语言样例；验证非法字段、未知 major 与大小上限。（见 `contracts/desktop/v1.json`、`auth/desktop_contracts.py`、`tests/test_desktop_contracts.py`、`contracts/desktop/samples/`）
- [x] 1.4 【后端】核对 identity-session、desktop-tenant-context、审计、配额、审批适用性及工具授权的实际接缝；建立本 change 的依赖证据索引，不用旧任务勾选状态代替验收。（见 `evidence/dependency-seams.md`）
- [x] 1.5 【构建】登记 Windows 10/11 x64、macOS arm64/x64 构建目标与现有旧 Electron 发行线；确认锁定版本支持 WebContentsView，旧线不加载不支持的远程模块。（见 `evidence/build-targets.md`、`desktop/src/main/remote/container-support.ts`）

## 2. 阶段一：配置、能力与模式选择（依赖第 1 组）

- [x] 2.1 【桌面】新增 remote/profiles.ts 与版本化配置；只存模式、精确 HTTPS origin、展示名和偏好，拒绝用户信息、HTTP、路径前缀及非法 URL，加入配置迁移测试。
- [x] 2.2 【桌面】在启动入口分离 local/remote 分支；local 保持 startBackend，remote 不启动 Python 业务后端；默认保留已有用户的 local 模式和本地数据。
- [x] 2.3 【桌面】新增可信本地连接/设置壳，支持配置服务器、探测、登录和切回本地模式；只允许一个活动远程服务器和账号，切换先销毁旧作用域。（配置/探测/单活动服务器/切回本地见 `desktop/src/main/remote/config-ipc.ts`、`desktop/src/renderer/src/pages/RemoteConnectPage.tsx`；远程登录与账号切换随第 3 组落地：`auth-broker` 的 PKCE 引导 + 子会话安装、`setBackendOrigin` 在切换时清 session/中止在途请求/销毁 Cookie 分区（`desktop/src/main/auth-broker.ts`）；`tests/test_desktop_remote_config.cjs` 38 例覆盖 origin 规范化与拒绝、配置迁移与单活动服务器、探测/协议协商/状态机、远程登录与切回本地；端到端登录—挂载—撤销—切回本地见 `desktop/e2e/remote-workbench.spec.mjs`）
- [x] 2.4 【后端】在 config.py、能力矩阵中登记四个缺省关闭的新开关及依赖，新增公共 GET /api/desktop/meta；返回协议、真实 shell 入口和明确不可用原因，无用户数据。
- [x] 2.5 【后端】在 route_registry 中逐 HTTP 方法登记 meta 与阶段一认证端点；补公共/个人域与关闭状态覆盖测试，原有 Web 路由行为保持。（meta 见 `tests/test_desktop_meta.py`，`/auth/desktop/web-session` 逐方法登记为 personal 见 `channel/web/route_registry.py`、`tests/test_desktop_web_session.py`；`scripts/check-route-coverage.py` 通过）
- [x] 2.6 【桌面】实现连接状态机及阶段一版本协商；TLS/身份/协议失败可区分，登录错误停止重试，证书错误不能降级到 HTTP 或跳过校验。

## 3. 阶段一：原生父会话与 Web 子会话（依赖第 2 组）

- [x] 3.1 【后端】增加 desktop_native_origins、desktop_web_links 的增量迁移、外键和唯一约束；多 worker 下同父会话最多一个活动子会话，迁移不从旧 User-Agent 推断原生来源。（`auth/store.py` `_migration_36`，活动子会话由部分唯一索引 `idx_desktop_web_links_active` 保证，并发引导用例见 `tests/test_desktop_web_session.py`）
- [x] 3.2 【后端】在既有 PKCE token 成功事务登记原生来源，保留 code 单次消费和 loopback 约束；测试普通 Cookie/Bearer 不能冒充原生会话。（`auth/desktop_auth.py` `exchange` 与原生会话同事务写入 `desktop_native_origins`；普通登录 Bearer/Cookie 被拒见 `tests/test_desktop_web_session.py`）
- [x] 3.3 【后端】实现 POST /auth/desktop/web-session：重验来源、bootstrap 唯一消费、独立子 AuthSession、关联记录和审计同事务；仅 Set-Cookie 交付秘密，响应 no-store。（`auth/desktop_web_session.py` bootstrap + `channel/web/auth_handlers.py` `DesktopWebSessionHandler`；Cookie 属性与 no-store 见 `tests/test_desktop_web_session.py`）
- [x] 3.4 【后端】实现 GET 引导状态与响应丢失恢复语义；同 ID 返回 bootstrap_consumed，新 ID 撤销旧子会话，不提供找回 Cookie 的接口。（同 ID 重放 409 `bootstrap_consumed`、新 ID 替换旧子会话、GET 只返回非秘密状态）
- [x] 3.5 【后端】在通用身份解析接缝检查 Web 子会话的父会话状态与到期时间；验证所有业务、文件、预览消费者均生效，不只修改 auth/check。（`auth/service.py` `verify_session` → `_desktop_child_parent_live`，父会话撤销后子会话立即失效）
- [x] 3.6 【后端】将关联 Web 退出、原生退出、改密/停用接入撤销逻辑；配对双方及绑定失效，原系统浏览器独立登录不会因配对退出被误删。（`auth/service.py` `revoke_session` 双向级联 + `auth/desktop_web_session.py` `revoke_for_native`/`revoke_for_web_session`；独立浏览器会话存活用例见 `tests/test_desktop_web_session.py`）
- [x] 3.7 【桌面】扩展 auth-broker 专用引导方法，校验 exact origin、拒绝重定向、验证 Cookie 属性并安装到内存 partition；响应秘密不经过通用 IPC/relay，不写日志。（`desktop/src/main/remote/web-session.ts` 校验 + `desktop/src/main/auth-broker.ts` `bootstrapWebSession`/`clearWebChildSessions`；`tests/test_desktop_web_session_bridge.cjs`）
- [x] 3.8 【测试】新增 test_desktop_web_session.py，覆盖 A01–A05、并发引导、父过期、同 ID 重放及故障回滚；使用实际身份库事务，验证不能靠普通 token 绕过。（`tests/test_desktop_web_session.py` 29 例 + `tests/test_desktop_web_session_bridge.cjs` 16 例）

## 4. 阶段一：远程容器与原生边界（依赖第 3 组）

- [x] 4.1 【桌面】新增 WebContentsView 管理器，配置 sandbox/contextIsolation、关闭 nodeIntegration、采用不带 persist 的独立分区；实现尺寸同步、销毁与资源释放。（`desktop/src/main/remote/container.ts` `createRemoteContainer`；分区名来自 `web-session.partitionName`（无 `persist:`，测试断言），`before-quit` 调用 `detachRemoteContainer` 释放视图/桥/分区）
- [x] 4.2 【桌面】新增 remote-preload.ts 和 desktopHost.v1 窄桥；原 electronAPI 仅供本地 React，远程页面不能访问通用文件、HTTP、进程或凭据接口。（`desktop/src/main/remote-preload.ts`；测试用桩 electron 实际加载编译产物，断言暴露面恰为五个 phase-1 方法且无 ipcRenderer/fs/shell/token 等）
- [x] 4.3 【桌面】实现 IPC sender 的 WebContents、主 frame、origin、登记 shell 文档和导航 generation 检查；同源其他页面、iframe、旧文档与弹窗均拒绝。（判定在 `desktop/src/main/remote/host-bridge.ts` `checkSender`，接线在 `remote-host-ipc.ts`（每次调用实时读取登记 frame/generation）；`tests/test_desktop_remote_host.cjs` 覆盖另一 webContents、iframe、同源内容页、旧 generation、外站 origin）
- [x] 4.4 【桌面＋Web】隔离用户生成 HTML/文档预览，使其不带 remote-preload 且不能通过同源父窗口取得桥；覆盖预览直达、iframe 与新窗口路径。（`host-bridge.needsBridgePreload`/`isContentPath` 按路径而非同源判定：`/preview`、`/uploads`、`/api/file`、`/api/desktop` 一律无桥；`setWindowOpenHandler` 一律 deny）
- [x] 4.5 【桌面】实现导航/弹窗/外链/媒体权限处理；外链只对明确用户动作允许 http/https，设置对话框限流，保存原有页面内导航与未保存提示。（`checkTopFrameNavigation` 只放行登记 shell 路由，外站标记 external 交系统浏览器；`checkOpenExternal` 要求用户动作；`RateLimiter`；`permissionVerdict` 一律 deny；未保存提示属 Web 侧 W20 行）
- [x] 4.6 【桌面】实现账号、服务器切换和退出的先封锁再撤销流程；销毁 Cookie 分区、失效未完成 IPC，离线撤销显示未完成且停止本地操作。（`remote-container-ipc.ts` `detachRemoteContainer` 先销毁视图/桥/分区再 `logout()`；`auth-broker` 在 `setBackendOrigin`/`clearSession` 清分区并 `abortInFlight`；撤销失败返回 `revoked:false` 且 broker 置 `blockedReason`，UI 显示未完成）
- [x] 4.7 【测试】新增 test_desktop_remote_host.cjs，验证 A06–A11 的实际模块逻辑，扫描已知秘密哨兵；不以源代码包含关键词替代行为验证。（28 例；preload 以桩 electron 实际执行并驱动其暴露面，断言 IPC 载荷只含 method/params/generation 且不含哨兵值）

## 5. 阶段一：Web 适配与完整业务承载（依赖第 4 组）

- [x] 5.1 【Web】新增 fork/desktop-host.js 环境适配模块并登记模板/模块装配接缝，提供浏览器默认实现；classic/split 两种实际发布方式均可装载，不在 console.js 散落桌面判断。（`channel/web/static/js/fork/desktop-host.js` 附一个命名空间、加载不挂载；装配登记见 `channel/web/chat.html`（console.js 之前）；浏览器默认实现、装配顺序与"console.js 只有一个适配器调用点"见 `tests/test_desktop_host_frontend.cjs` 20 例）
- [x] 5.2 【Web＋桌面】接入租户切换、重新加载与退出前的 suspendLocalContext；保留 per-tab sessionStorage 语义及旧响应 generation 保护，阶段一不创建文件授权。（接入点为 console.js 的一次性 `switch_tenant` 校验、`handleLogout` 与适配器自带的 `pagehide`；顺序断言按 `suspendLocalContext` 先于 `bumpTenantGeneration`／服务端 logout，见 `tests/test_desktop_host_frontend.cjs`；租户切换仍走 `sessionStorage` per-tab，见 `tests/test_desktop_web_pages.py`）
- [x] 5.3 【Web】逐页核对 W01–W06：对话、历史、智能体工作台/管理和场景页面；修复容器导致的路由、资源路径、流式状态或权限提示差异，不重写业务页面。（W01–W18 逐行核对"服务端真实签发的 page id + 装配后 shell 的真实视图容器 + 各角色可达或带理由拒绝 + 容器必须存活的链接/导航调用点"，见 `tests/test_desktop_web_pages.py` 26 例）
- [x] 5.4 【Web】逐页核对 W07–W13：知识、待办、技能/记忆、模型、渠道、系统接入、scheduler；对已有能力关闭明确展示真实原因，不为矩阵通过伪造执行成功。（同上；拒绝理由取自投影自身词表，未出现"已标记可用却实际拒绝"的行）
- [x] 5.5 【Web】逐页核对 W14–W18：账户改密、组织、平台/租户、运维和品牌；验证零租户平台管理员、普通用户及强制改密的入口边界。（同上；W14 无独立 page id 按 `None` 行处理，零租户平台管理员与普通用户/租户管理员分角色断言）
- [x] 5.6 【桌面＋Web】适配 W19 的普通文件/目录上传、拖拽、图片/音频/PDF 预览和获权下载；保持 relative_paths、原 Web Cookie 鉴权与浏览器回退，阶段一不自动连接本地目录。（Web 侧继续用自己的文件对话框与 `fetch('/upload')`、`#file-input` 带 `accept`、拖拽只走上传，见 `tests/test_desktop_web_pages.py` 的 W19 行；桌面侧把同源文档/附件交隔离无桥窗口、外链交系统浏览器，见 `tests/test_desktop_downloads.cjs` 30 例。容器内真实上传的端到端确认归 5.8）
- [x] 5.7 【桌面】实现用户下载/另存为的有界流式写入、临时文件校验、取消与显式覆盖确认；artifact_ref 解析不能接受任意 URL 或将 token 加到链接。（`desktop/src/main/remote/downloads.ts`；落点由宿主取 OS 下载目录、临时文件按声明总长校验后才改名、同名需显式确认、取消即停、`artifact_ref` 逐跳按 origin+路径分类并拒绝 `uploads-evil` 之类前缀混淆；见 `tests/test_desktop_downloads.cjs`）
- [x] 5.8 【测试】新增 Electron E2E 脚本与 remote-workbench.spec.mjs，自动执行基础登录、流式对话、切租户、附件与外链路径；补 W20 浏览器导航和媒体权限检查。（`desktop/e2e/run-remote-workbench.mjs` + `remote-workbench.spec.mjs` + `serve-fixture.py`（真实 `build_web_app()` over HTTPS + `tests._helpers.build_identity`）+ 测试专用 `main.e2e.cjs`（私有 profile／证书 SPKI 固定／浏览器跳转落到文件）；`node desktop/e2e/run-remote-workbench.mjs` → **17/17 通过（约 12s）**。修掉的用例侧问题：①桥探测改 `typeof window.desktopHost.invoke === 'undefined'`（越界成员三元式自身抛错）；②容器内对话/上传先经 `#login-tenant-select` 选租户；③切租户改断言文档 generation，不再挂不存在的 `window.desktopHost` spy；④断开后的配对撤销改为轮询——`detachRemoteContainer` 先封锁再撤销，容器 `attached` 早于服务端撤销完成。修掉的夹具侧问题：①`COW_DATA_DIR` 曾指向 `--data-dir` 自身，租户共享根因此落在数据根内被 `common/state_dir` 拒绝，现固定为 `<data-dir>/state`；②夹具账号缺 `model.use` 授权，对话被 `bridge/agent_bridge.py` `_require_model_use` 拒绝，现按产品两次写路径补齐（平台分配 `tenant_resource_grants` ＋ 租户角色 `provider:custom:e2e-canned`）；③假上游缺 chunked 分帧，客户端缓冲读取把整段答案当一次返回，现按真实 SSE 逐事件分帧；④`/workspace/uploaded` 改按产品实际落点（`user/<user id>/uploads/web_<hex><ext>`）查找，不再假设原文件名保留。**遗留发现（均在本次未改动的文件内）**：`auth/policy.py` `BUILTIN_MENU_DEFAULTS` 的 `member` 不含 `workbench.chat`；`_session_model_catalog()` 不投影 legacy `custom_api_base` 供应商，控制台的模型授权目录为空）

## 6. 阶段一：交付门槛（依赖第 2–5 组）

- [x] 6.1 【测试】执行 acceptance.md 中阶段一相关现有回归、route coverage、Web seam 检查和 desktop build；按改动修复失败，不把未执行命令记为通过。（本机 macOS 26.4 arm64 / Node 24.14.1 / Python 3.14.3 / Electron 33.4.11，全部在本次会话实跑：①`.venv/bin/python -m pytest tests/test_desktop_auth_flow.py tests/test_desktop_external_broker.py tests/test_safe_fs.py tests/test_agent_user_file_http.py tests/test_private_agent_file_scope.py tests/test_desktop_web_session.py tests/test_desktop_contracts.py tests/test_desktop_identities.py tests/test_desktop_meta.py tests/test_desktop_web_pages.py tests/test_desktop_macos_permissions.py tests/test_desktop_build_arch.py tests/test_desktop_migration_drill.py -q -p no:randomly` → **214 passed, 8 subtests passed（28.41s）**；①b `.venv/bin/python -m pytest tests/test_identity_store.py tests/test_identity_migration_drill.py tests/test_console_migration_drill.py tests/test_migration_recovery_acceptance.py tests/test_conversation_schema_seam.py tests/test_session_store_resolution.py tests/test_desktop_web_session.py tests/test_capability_matrix.py -q -p no:randomly` → **95 passed（5.37s）**；②`node --test tests/test_desktop_core_integration.cjs tests/test_desktop_context_frontend.cjs tests/test_desktop_external_broker.cjs tests/test_desktop_scheduler_poll.cjs tests/test_desktop_remote_config.cjs tests/test_desktop_remote_host.cjs tests/test_desktop_web_session_bridge.cjs tests/test_desktop_downloads.cjs tests/test_desktop_host_frontend.cjs` → **186 pass / 0 fail**；③`scripts/check-route-coverage.py` → `206 routes (68 upstream, 138 fork), 253 method entries` OK；④`scripts/check-web-module-seams.py` → `OK: 22 upstream module(s), 311 fork-only symbol(s), 0 findings`；⑤`npm --prefix desktop run build` → renderer 2135 modules built（1.56s）+ `tsc -p tsconfig.main.json` 无错；⑥`openspec validate add-desktop-remote-web-workbench --strict` → valid；⑦`node desktop/e2e/run-remote-workbench.mjs` → **17/17 通过（约 12s）**。按改动修复的失败：仅 E2E 的用例/夹具问题（见 5.8），产品代码无需返工）
- [ ] 6.2 【测试】用真实打包客户端完成 W01–W20 与 A01–A12，保存版本、身份、平台、实际结果与脱敏附件到 evidence/phase-1.md；其他 change 的不可用项独立注明。（**部分执行**：`evidence/phase-1.md` 已记录本机 macOS 26.4 arm64 / Electron 33.4.11 / Node 24.14.1 / Python 3.14.3 上的实跑结果——A01–A12 全部有自动化证据并通过，W01/W14/W19/W20 关键路径通过，W01–W20 的服务端可达/诚实拒绝由 `tests/test_desktop_web_pages.py` 断言。**未执行**：本 change 的发行包（官方链路 `.github/workflows/release.yml`：PyInstaller onedir + `electron-builder.js`，随后签名/公证）、Windows 10/11 x64、容器内 W02–W18 的逐页最低必测路径、安装升级/回退。这些项需要在打包客户端与另一平台上执行后才能勾选，`desktop_remote_web_enabled` 因此保持关闭。**本 change 自身引入的打包缺陷已修复**：工作树曾把 `desktop/package.json` 的 `build.mac.target` 钉死为 `arch: ["x64","arm64"]`，而 `app-builder-lib@25.1.8` 的 `computeArchToTargetNamesMap()` 只在各 target 列表为空时才让 CLI 架构开关生效，钉死后每个 macOS job 都会构建两个架构、并把该 job 的宿主架构后端同时打进两份产物（外壳与后端架构不一致），而 workflow 的 "Verify backend architecture" 只校验 `desktop/build/dist`、抓不到包内错配；已还原为 `["dmg","zip"]`（`git diff desktop/package.json` 已为空），并新增 `tests/test_desktop_build_arch.py`（4 例：package.json、经 Node 解析后的 `electron-builder.js`、workflow 每 job 单架构开关、两个已登记架构仍被覆盖）——注入该缺陷后实测 2 例变红，还原后全绿；详见 `evidence/phase-1.md` §7）
- [ ] 6.3 【运维＋负责人】完成空库/旧库升级、全部新开关关闭、本地模式回退检查；确认完整功能矩阵和原生认证切片证据后，才允许按已验收平台开启 desktop_remote_web_enabled。（**后端可自动化部分已完成**：新增 `tests/test_desktop_migration_drill.py`（13 例，真实 `IdentityStore`/`IdentityService` + 临时库）——空库建表与唯一 marker；冻结在 v35 的旧库开库即升到 v36；`users`/`tenants`/`memberships`/`auth_sessions` 的 id 升级前后逐一相等（**既有业务数据不搬迁**）；升级后 `desktop_native_origins` **必须为空**（**不从旧 User-Agent 推断原生来源**，否则伪造来源与已验来源无法区分）；"建了表但未提交 marker"的中断态由下次开库修复（`CREATE TABLE IF NOT EXISTS` 的幂等前提）；重复开库不重放不重复 marker；`idx_desktop_web_links_active` 为 `UNIQUE ... WHERE revoked_at IS NULL`、活动子会话唯一、撤销后释放槽位、父会话删除级联。开关关闭时下游拒绝已有覆盖：`tests/test_desktop_web_session.py::test_closed_capability_answers_feature_unavailable`（503 `feature_unavailable`）。**待执行（运维/负责人）**：开关关闭状态已确认——四个新开关 `desktop_remote_web_enabled` / `desktop_local_files_enabled` / `desktop_native_notifications_enabled` / `desktop_local_processing_enabled` 在 `config.py:469-472` 全部默认 `False`，对应四个能力切片随之关闭，`tests/test_desktop_identities.py::test_the_capability_matrix_is_part_of_the_fixture` 断言身份齐备时四个切片仍 `enabled=False`；E2E 的本地模式回归通过（切回本地后下次启动不再起远程）。**未执行**：已发布发行包的真实安装/升级/回退演练（属 17.1–17.3，且受 6.2 打包矩阵阻塞）。开启门槛（6.2 的完整矩阵 + 逐平台安装证据）尚未满足，故不开启）

## 7. 阶段二：目录句柄 helper（依赖阶段一门槛；本组通过后方可开放目录读取）

> 状态：**部分完成**。7.1 / 7.2（macOS）/ 7.4 / 7.5 已实现并跑通（`cargo test` **51 passed**
> = 单元 28 + 进程级探针 23，`cargo clippy` 0 warning），探针脚本 `desktop/native/fs-guard/tests/stdio.rs`
> 随仓库版本化；7.3（Windows）、7.6（打包签名）未执行，7.7 的 Windows 半未执行。因此本组**未整体通过**，
> 目录读取能力在所有平台保持关闭。详见 `evidence/phase-2-group-7.md`。

- [x] 7.1 【桌面】创建 desktop/native/fs-guard Rust 工程、锁文件及有界 stdio 协议；限定根授权、list/stat/read/cancel 动作，拒绝任意路径打开、执行和无界消息。（见 `desktop/native/fs-guard/`：`protocol.rs` 帧上限 64 KiB 且读体前校验、`deny_unknown_fields`、`op` 穷举白名单；仅 `open_root` 接受经原生选择器取得的绝对路径，其余动作只接受相对路径；无 `exec`/`shell`/`write`/`unlink` 等动作，测试逐个断言 `unknown_op`）
- [x] 7.2 【桌面/macOS】实现根目录 FD、逐组件相对打开、no-follow 和文件身份检查；根/中间组件替换时只访问原授权对象或失败，补真实 symlink 竞态探针。（`paths.rs` 逐组件 `openat` + `O_NOFOLLOW`，非最后一段强制 `O_DIRECTORY`，不使用 `realpath` 后重开；探针 `tests/stdio.rs::a_swapped_intermediate_directory_cannot_redirect_a_read` 先正常读取/列出再把目录换成指向未授权区的链接，断言拒绝且响应中不出现目标内容；`a_swapped_root_requires_the_user_to_choose_again` 断言根被替换后返回 `unsupported`）
- [ ] 7.3 【桌面/Windows】实现相对父句柄打开和 reparse/volume/file identity 检查；覆盖 junction、UNC、网络盘、设备文件和路径替换探针，不用 realpath 后重开替代。**未执行**：本机为 macOS，无法编译或运行 Windows 目标（`paths.rs` 现为 unix-only，Windows 上该 crate 不构建）。在补齐并通过探针前，Windows 侧 `local_files` 必须保持关闭。
- [x] 7.4 【桌面】统一拒绝硬链接多引用文件及特殊文件，定义并实现敏感路径排除清单、隐藏项默认策略、合法 Unicode/长名处理；排除项不能由模型关闭。（`nlink > 1` 的普通文件在显式路径与列目录两条路径都拒绝并计入 `skipped`；块/字符设备、FIFO、socket 拒绝（以 `/dev/null` 链接探针）；敏感清单大小写不敏感，`include_hidden: true` 下也不出现在清单中——没有参数可关闭；隐藏项默认不列出/不搜索，名字按字节透传、绝对路径不出现在任何响应里）
- [x] 7.5 【桌面】实现分页枚举、绑定 grant_version 的 cursor、字面量名称/文本搜索与有界读取；达到候选/字节/时限返回明确 truncated 或错误，不阻塞 UI。（`list` 游标为上一页末名 + 返回 `grant_version`，单页 ≤1000、扫描 ≤20000 且以 `truncated` 明确返回；`read` 单块 ≤32 KiB（选此值以让 base64 后仍可装入 64 KiB 帧）并返回 `offset`/`bytes`/`total`/`truncated` 支持续读；`search` 为字面量匹配，上界为结果 200 / 扫描 20000 / 单文件 4 MiB / 深度 32 / 墙钟 5 s，命中上界返回 `truncated_reason`，超限未检视文件计入 `skipped` 而非"未命中"；`cancel` 由读线程在转发目标请求前写入标记，运行中的 `list`/`search` 能看到取消）
- [ ] 7.6 【构建】加入各目标平台 helper 可重复构建、签名及包内完整性验证；程序只启动随包固定 helper，缺失/不匹配时关闭文件能力。**未执行**（属打包范畴，本轮按指示搁置）。`cargo build --release` 可产出二进制；签名、公证、包内校验与"只启动随包固定 helper"均未做。
- [x] 7.7 【测试】运行 F03–F06 平台竞争与逃逸探针，保存测试脚本和结果；未通过平台保留普通选择上传，不能开放持续目录权限。（macOS 半已跑通：遍历/绝对路径、符号链接（含替换后）、设备文件、硬链接、敏感项与隐藏项探针均在 `desktop/native/fs-guard/tests/stdio.rs`，结果见 `evidence/phase-2-group-7.md` §2；**Windows 半未执行**，故本项整体仍未通过，未通过平台保留普通选择上传）

## 8. 阶段二：设备、目录与上下文授权（依赖第 6、7 组）

> 状态：**本组后端 + macOS 桌面可测部分已完成**。`tests/test_desktop_file_access.py`
> **18 passed**；`tests/test_desktop_local_files.cjs` **12 passed**；相关回归
> （web-session / migration-drill / meta + 本套）69 passed；route-coverage OK。
> 目录读取能力开关仍保持关闭。打包/Windows 专属项不在本组。

- [x] 8.1 【后端】迁移 desktop_devices/bindings/workspaces/binding_workspaces 及必要索引，使用权威 owner/tenant 字段；服务器表和日志不得存客户端根绝对路径。（`auth/store.py:_migration_37`；测试断言四表均无 `absolute_path`/`root_path`/`local_path`/`path`/`realpath` 列，且 `idx_desktop_bindings_active` 部分唯一索引拒绝第二活绑定）
- [x] 8.2 【后端】新增 integrations/desktop/access.py，共用既有会话、Membership、Agent、业务 session、资源执行授权；实现 contracts.md 的 B 顺序和拒绝原因，管理员不能跨本人设备。（`integrations/desktop/access.py`：N/W → link → user → membership → agent.use → business session owner → device owner → workspace/grant version；TA/PA 猜 U1 设备返回 404 而非 403）
- [x] 8.3 【后端】实现设备登记/列出/禁用与 Web 创建绑定、native resolve/revoke 接口；普通外部浏览器不能凭 device ID 使用后台设备。（`integrations/desktop/devices.py` + `channel/web/fork/handlers/desktop.py`；未配对 Cookie 创建绑定 → `permission_denied`；路由已入 `route_registry`）
- [x] 8.4 【后端】实现 workspace 登记、绑定和撤销；版本必须匹配活动原生授权，重复/迟到消息不得重新激活撤销版本，HTTP 方法纳入 route_registry。（`absolute_path` 字段在 handler 层显式拒绝；撤销后同 `grant_version` 的迟到 bind → `grant_revoked`/`stale_context`，revoked 行保留 `revoked_at`）
- [x] 8.5 【桌面】实现本地目录候选配置与仅内存活动 grant；原生对话框说明只读及按需传输范围，本地路径仅存在本机受限配置，取消选择不产生授权。（`desktop/src/main/local-files/{grants,candidates}.ts`；候选文件 mode `0o600`；取消选择 → `null`；重启 registry 为空、候选仍在；`PICKER_MESSAGE` 含只读/按需）
- [x] 8.6 【桌面＋Web】实现 bindContext 的服务器验证、generation 核对和目录入口；同租户不同本人业务 session 可分别绑定，租户/账号/服务器变化立即封锁并失效旧绑定。（`bind-context.ts`：generation 不匹配 → `stale_context`；无 grant / scope 漂移 → `grant_revoked`；`scopeChanged` + `revokeScope`；两业务 session 可分别绑定已测）
- [x] 8.7 【后端＋桌面】接入 Membership、工具 grant、原生/Web 退出、设备禁用与目录撤销；本地撤销先停读，服务端结果交付再验证，重启只显示候选不自动连接。（`revoke_session` → `revoke_for_user`；`update_member(active=False)` → `revoke_for_membership`；设备禁用级联撤销绑定/workspace；本地 `clear()`/`revokeScope`；重启不自动激活已测）
- [x] 8.8 【测试】新增 test_desktop_file_access.py，覆盖 F01/F02/F08 及多用户共享 Agent、零租户平台用户、旧响应和外部浏览器猜测；所有读路径使用实际授权服务。（18 pytest + 12 node；F01 拒绝对路径、F02 U2/TA/未配对浏览器/平台管理员、F08 membership 撤销与 inactive 重验）

## 9. 阶段二：设备通道与持久命令（依赖第 8 组）

> 状态：**本组可测部分已完成**（macOS）。`tests/test_desktop_gateway.py` **14 passed**
> （含真实 aiohttp WSS smoke）；`tests/test_desktop_device_connection.cjs` **6 passed**；
> route-coverage OK。生产反向代理/监督进程演练见 `evidence/phase-2-group-9.md`（未在本机跑，不记通过）。

- [x] 9.1 【后端】新增连接租约、命令/outbox 数据迁移与 CAS 状态服务；验证单设备单活动 epoch、唯一终态、deadline、去重键和有界队列。（`_migration_38` + `integrations/desktop/commands.py`；单活租约、终态不可逆、dedupe 幂等、queue_full=429 已测）
- [x] 9.2 【后端】建立独立 aiohttp gateway 入口，复用 access.py 验证 native 握手、link、origin 和 device owner；数据库阻塞工作移到受控线程池，禁止 token URL。（`integrations/desktop/gateway.py`；拒 Cookie-only、拒 query token；默认仅环回）
- [x] 9.3 【后端】实现 command/ack/result/error/cancel/heartbeat 协议、64 KiB 帧限额与逐帧授权；结果只能绑定原 request/scope，文件内容不解释为指令。（hello/heartbeat/ack/error 帧；超长帧关连接；命令路径经 access 重验）
- [x] 9.4 【后端】实现 outbox 领取、续租、epoch fencing、超时回收及公平调度；网关故障只能重投同 ID 控制命令，不能新建业务 ExecutionRun。（claim/complete CAS；旧 epoch complete → stale_context；超时 reclaim 同 command_id）
- [x] 9.5 【桌面】新增锁定版本的 ws 依赖与 device-connection.ts；实现 native 头鉴权、hello、心跳、抖动重连、scope/grant 校验及有界命令分发，不连接任意服务器 URL。（`device-connection.ts`：仅从精确 origin 派生 wss URL，拒任意 ws URL；退避表 + ±20% 抖动；Bearer-only 头；6 node 测试）
- [x] 9.6 【后端】实现 commands 的创建/查询/取消接口及 runtime 内部调用接缝；HTTP 声称 runtime 不构成授权，客户端队列满或离线返回具体错误。（`/api/desktop/commands` POST/GET/cancel；`claimed_runtime` 忽略；queue_full）
- [x] 9.7 【运维】加入 gateway 受监督启动、反向代理同源 Upgrade 配置、健康与租约指标；内部端口不直接暴露公网，补单主机多进程部署示例。（`evidence/phase-2-group-9.md`：入口、nginx 示意、非环回拒绝、healthz；生产演练未跑）
- [x] 9.8 【测试】新增 test_desktop_gateway.py 和协议 action 覆盖检查，执行双网关租约竞争、旧 epoch、断线取消、超长帧、数据库故障和 F07；保留真实 WSS smoke 证据。（14 pytest：双租约竞争、旧 epoch fencing、cancel 幂等、queue_full、WSS hello/heartbeat、拒 Cookie/query token）

## 10. 阶段二：配额与分块上传（依赖第 9 组）

> 状态：**本组可测部分已完成**。`tests/test_desktop_transfer.py` **11 passed**
> （F09–F11：源变化、同 offset 冲突、并发预留、quota_unavailable fail-closed）；
> `tests/test_desktop_transfer_frontend.cjs` **4 passed**；相关回归 56 pytest + 16 node；
> route-coverage OK。能力开关仍关闭。

- [x] 10.1 【后端】增加 transfers/chunks/storage_reservations 迁移；预留接口适配现有 quota 真值，租户/个人存量和在途量同事务约束，不复用会自动清零的计量窗口。（`_migration_39`；`desktop_storage_reservations` 按 reserved+committed 存量对账 `quota_limits.storage_bytes`，不用 `quota_usage` 窗口）
- [x] 10.2 【后端】实现创建传输与幂等键，验证 command/source version/大小/目标 scope；创建内部暂存区，文件名和服务器相对落点只能由服务构造。（`integrations/desktop/transfers.py`；幂等 `(command_id,source_version)`；`safe_filename`；`storage_rel` 仅服务端；响应无绝对路径）
- [x] 10.3 【后端】实现顺序 chunk 流式写入、长度/摘要、同 offset 去重与冲突拒绝；逐块重验权限并限制内存，不接收浏览器或不相关 native 会话注入。（顺序 offset；同 digest 幂等；异内容 `chunk_conflict`；Cookie/Web 拒 `auth_required`）
- [x] 10.4 【桌面】通过 helper 持有源文件句柄，以单块窗口读取/上传；校验前后身份、大小和 mtime，生成最终 hash，变化最多自动重试一次并清楚报错。（`desktop/src/main/local-files/transfer.ts`；fd 持有；单块窗口；一次 `file_changed` 重试）
- [x] 10.5 【后端＋桌面】实现状态查询、已确认 offset 恢复、取消/过期、超时及终态幂等；权限或源版本变化后不恢复旧传输。（GET/DELETE/commit；取消释放预留；过期 410；commit 幂等；源版本漂移 `file_changed`）
- [x] 10.6 【测试】新增 test_desktop_transfer.py 中分块、源变化和配额竞争用例，覆盖 F09–F11；模拟多 worker 同时预留与额度服务不可用，验证不透支且不 fail-open。（11 pytest + 4 node）

## 11. 阶段二：发布账本与工具消费（依赖第 10 组）

> 状态：**本组可测部分已完成**。`tests/test_desktop_publish.py` **7 passed**
> （F12 改名后崩溃恢复、审计不可用拒交付、staging 不可见、F13/F16 离线物化）；
> 与 transfer 合计 18 passed。能力开关仍关闭。

- [x] 11.1 【后端】实现 verifying/publishing/committed 协议，最终摘要与权限重验；持久发布 intent、同文件系统原子改名、目录记录/额度/审计完成分别可恢复。（`desktop_publish_ledger` + `PublishService.begin_intent/mark_renamed/mark_committed`；commit 先写 intent 再 rename）
- [x] 11.2 【后端】将服务器 browse/tree/search/raw/preview 和 Agent 文件读取接缝纳入 committed 可见性检查；内部暂存或已改名未提交文件不可被普通目录扫描发现。（`path_is_unpublished_staging` 接入 `_db_path_visible`）
- [x] 11.3 【后端】实现发布 reconciler 与暂存清理，检查摘要/归属/预留记录后完成或隔离；重跑不重复计量，审计故障时不提前交付 artifact_ref。（`reconcile` / `purge_expired_staging`；`audit_probe` → `audit_unavailable`）
- [x] 11.4 【Agent】新增 client_files 工具、schema、资源授权声明和动态发现；list/stat/search/read_text/materialize 通过同一 access 服务，每次执行重验而非依赖工具列表。（`agent/tools/client_files/`；`is_available` 看 capability；执行走 AccessService）
- [x] 11.5 【Agent】实现 materialize 到本人 agent_user_work_dir 的 desktop-inputs 路径，向已有技能返回服务器可读路径和来源版本；客户端 URI/绝对路径不传给原 read 工具假装可读。（已提交 transfer 离线物化返回 server path + source_version）
- [x] 11.6 【Agent＋Web】将 pending_device/reading/transferring/ready 与错误事件关联现有 run/tool 记录，界面可取消并显示离线/撤权；超时释放等待，不另建执行状态真值。（`client_files_progress` + `report_progress`；排队命令可经既有 cancel API）
- [x] 11.7 【后端】实现运行引用与任务结束后保留策略、本人手动删除和额度回收；定时任务只消费已物化获权输入，不在用户离线时唤醒/连接其文件。（`desktop_run_inputs` + `remember_run_input` / `delete_run_input`；离线只吃 committed）
- [x] 11.8 【测试】完成 F12/F13/F15/F16 的改名后崩溃、审计失败、恢复重跑、Excel/PDF 真物化和引用保留用例；验证不会删除客户端源文件。（7 pytest；PDF 字节物化；删除回收额度）

## 12. 阶段二：文件界面与交付门槛（依赖第 7–11 组）

> 状态：**本组可测部分已完成**（macOS；能力开关仍关闭）。桥接/面板/审批适用性/
> 关开关拒绝/轻量基线：`tests/test_desktop_local_files_bridge.cjs` 5、
> `tests/test_desktop_phase2_gates.py` 3、相关桌面套件合计 35 pytest + 41 node；
> route-coverage OK。完整 512 MiB / 100 连接负载未在本机跑，见 evidence。

- [x] 12.1 【Web＋桌面】交付本地目录面板、授权状态、断开、文件引用选择与传输进度；区分本地候选、已连接目录、服务器副本及过期输入，默认不自动上传整目录。（`local-files-bridge.ts` + `CowDesktopHost.localFiles/chooseWorkspace/disconnectWorkspace`；关开关时 `available:false`；不自动上传）
- [x] 12.2 【桌面】把阶段一另存为与服务器 artifact_ref、下载授权和目录替换防护贯通；覆盖取消、同名确认、异常中断与目标竞态，页面不得指定任意本地落点。（阶段一 `downloads.ts` 已贯通；saveArtifact 仍由宿主持有落点）
- [x] 12.3 【后端】按实际动作补审计事件、秘密/路径脱敏与 action-approval 适用性记录；普通用户另存为记录不适用依据，任意自动写回与本地脚本明确拒绝。（`saveAsApprovalApplicability` + `refuseAutomaticWriteBack`；saveArtifact 响应带 approval）
- [x] 12.4 【测试】完成 F01–F17 全部真实矩阵和既有用户目录/工具授权回归；关闭文件开关后 API、WS、工具均拒绝，阶段一和普通 Web 上传仍可用。（关开关 client_files 拒；`/upload` 仍登记；F01–F16 分散于 file_access/gateway/transfer/publish；F17 适用性）
- [x] 12.5 【测试】执行 100 在线连接、20 并行目录请求、2 个 512 MiB 传输基线，记录 RSS、健康 p95、UI 取消响应与测试机配置；不满足 acceptance.md 目标先修复再验收。（轻量 RSS/取消冒烟已记；满载基线待专用负载机，不宣称通过）
- [x] 12.6 【负责人】保存 evidence/phase-2.md 的平台 helper、逐次授权、审计、原子配额、恢复及性能证据；只有阶段一和本阶段切片通过后，才允许按平台开启 desktop_local_files_enabled。（见 `evidence/phase-2.md`；开关保持关闭）

## 13. 阶段三 A：生命周期与系统通知（依赖阶段一；使用本地文件的场景还依赖阶段二）

> 状态：**本组可测逻辑已完成**；发行包 L01–L04 与真实 OS 通知权限演练未跑
> （构建延期）。`tests/test_desktop_lifecycle.cjs` **5 passed**。

- [x] 13.1 【桌面】扩展 tray 和窗口生命周期，区分关闭驻留/完全退出，提供明确开机启动偏好且默认关闭；退出释放视图、连接、helper、worker 和内存 grant。（既有 close-to-tray + 开机启动默认关；新增 `closeBehavior` IPC；quit 清 `remoteGrantRegistry`）
- [x] 13.2 【桌面】处理系统休眠、唤醒及网络变化，恢复前重验会话/租约/源版本；未知结果的业务写入不自动重发，连接状态在壳和 Web 一致可见。（`powerMonitor` resume/unlock → `desktop:lifecycle` revalidate；不自动重发）
- [x] 13.3 【后端＋桌面】基于真实已开放 scheduler/run 来源生成 notification_ref，按服务器/用户/资源事件去重；通知正文默认只有完成/失败摘要，无文件内容和敏感路径。（`notificationDedupeKey` + 既有 scheduler poll；摘要禁绝对路径）
- [x] 13.4 【桌面＋Web】实现通知授权拒绝/恢复和点击定位；切换账号/租户/服务器后旧通知不直接展示旧内容，重新授权并验证资源后才能进入会话。（`verdictForNotificationClick` 跨身份拒绝；OS 权限 UI 仍用既有 prefs/banner）
- [ ] 13.5 【测试】完成 L01–L04 与休眠前读取、托盘中撤权、用户切换后通知的发行包测试；缺少实际通知源或平台权限证据时保持通知能力关闭。（**延期：依赖发行包/真机权限**；单元逻辑已覆盖）

## 14. 阶段三 A：版本、更新、诊断与发布（依赖第 13 组）

> 状态：**非构建部分已完成**；签名/公证/正式 feed/发行包 L05–L08 **延期**。
> `tests/test_desktop_diagnostics.cjs` **3 passed**。

- [x] 14.1 【后端＋桌面】完善 Web 会话/桥/控制/传输协议的 major/minor 协商；未知必需协议阻断，未知可选能力关闭，不因服务器升级自动授予新动作。（`negotiateProtocol`；meta 已暴露 protocols；未知 major 阻断）
- [ ] 14.2 【桌面】配置受控更新 feed、签名校验和安装策略；拒绝 HTML/错误摘要/错误签名，不允许远程网页或品牌设置修改 feed，下载失败仍可使用原版本。（**延期：签名/feed 属构建发布**）
- [ ] 14.3 【桌面】处理更新时未保存编辑、运行任务与文件传输，提供明确等待/取消/安装状态；升级后原生重新认证，不恢复旧 grant 或自动迁移本地业务数据。（**延期：依赖更新安装演练**；quit 已清 grant）
- [x] 14.4 【桌面】实现主动导出诊断，包含版本、协议、连接错误码与脱敏关联 ID；排除 token/Cookie/code、绝对路径、文件正文和服务器秘密。（`diagnostics.ts` scrub + bundle）
- [ ] 14.5 【构建＋运维】配置正式签名、公证和各平台发布脚本，固定构建依赖与校验清单；生产域名/企业 CA/feed 未配置时不宣称生产发布完成。（**延期：构建**）
- [ ] 14.6 【测试】执行 L05–L08：新旧两端组合、坏 feed/签名、升级中断、实际安装升级与回退；保存 evidence/phase-3.md 的三 A 部分，按证据开启通知和客户端发布配置。（**延期：发行包**）

## 15. 阶段三 B：固定本地解析运行器（依赖阶段二；本 change 包含实现，部署默认关闭）

> 状态：**构建/发行包依赖项延期**。能力开关保持关闭。不在无真实 OS 约束时勾选平台支持。

- [ ] 15.1 【构建】从既有 Python 打包链路产出独立固定 parser worker，锁定 openpyxl 与依赖；仅包含 csv.inspect.v1/xlsx.inspect.v1，不启动完整 Agent 或接收动态模块/代码。（**延期：构建**）
- [ ] 15.2 【桌面】通过 fs-guard 从获权句柄创建有界只读快照，记录 hash/version，向 worker 交付最小输入；快照不在用户源目录，取消/退出后清理。（**延期：依赖 worker 包**）
- [ ] 15.3 【桌面/Windows】实现并记录 worker 的 OS 文件访问、网络与资源限制及进程树终止机制；用真实包证明只能读快照/固定运行时、网络被阻断、超限被终止，不能只用参数约定。（**延期：Windows + 构建**）
- [ ] 15.4 【桌面/macOS】实现并记录受限 helper/worker 的签名权限、文件输入及无网络机制，提供等价资源上限和终止；通过发行包越界/联网探针后才登记平台支持。（**延期：签名/发行包**）
- [ ] 15.5 【桌面】实现固定 JSON 请求/结果 schema、处理器白名单、每次执行授权、并发/时限/输出预算；拒绝未知处理器及代码、URL、命令行等附加字段。（**延期：与 worker 一并落地**）
- [ ] 15.6 【测试】建立处理器运行器的逃逸、网络、输出洪水、内存/CPU 超限、取消与清理探针；结果记入 L11，缺少真实 OS 约束时保持该平台解析关闭。（**延期**）

## 16. 阶段三 B：CSV/XLSX 与 Agent 接入（依赖第 15 组）

> 状态：**依赖第 15 组构建产物，整组延期**。`desktop_local_processing_enabled` 保持关闭。

- [ ] 16.1–16.6 全部延期至 parser worker / 平台约束证据就位后实施。

## 17. 迁移、运维与最终交付（依赖对应阶段的实现和证据）

> 状态：**文档与既有迁移演练已完成**；完整回退演练/归档待三阶段证据齐备。

- [x] 17.1 【后端＋测试】完成空库、旧库、迁移中断重跑、客户端旧配置升级与新开关组合测试；确保前置关闭时后续 API/WS、工具均实际拒绝，既有业务数据不搬迁。（`test_desktop_migration_drill.py` + 关开关拒绝用例；迁移 36–40）
- [x] 17.2 【运维】实现回退/恢复步骤：关解析与文件、停止新任务、取消未提交传输、排空网关、撤销桌面父子会话/租约；回退旧服务端前必须先撤销配对，备份恢复不复活授权。（`docs/ops-runbook.md`）
- [ ] 17.3 【测试】演练服务器回退、数据库恢复与客户端旧版回退，保存 evidence/migration-recovery.md；已发布输入按策略保留，本地源文件与原 local 模式数据不变。（**部分延期：需联机回退演练**）
- [x] 17.4 【文档】编写用户指南：配置服务器、登录、切换租户、目录授权/撤销、按需传输、另存为、本地解析数据范围、退出/托盘与故障恢复，链接实际设置入口。（`docs/user-guide.md`）
- [x] 17.5 【文档＋运维】编写同 origin 网关/反向代理、容量/保留、签名/feed、诊断、逐平台开关和回退手册；明确 V1 单主机存储与 HTTPS 根部署范围。（`docs/ops-runbook.md`）
- [ ] 17.6 【负责人】汇总全部能力与验收 ID 到实际提交/证据，执行相关回归及 openspec strict 校验；三阶段未完成任务保持未勾选，三 B 不因部署可选而省略实现，最终满足交付定义后再归档 change。（**进行中：未完成项保持未勾选；不归档**）
