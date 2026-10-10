# optimize-console-login-startup 本机实施与验证记录

日期：2026-10-10。范围：仓库内可实施代码、测试与运维文档；**不包含**目标站点 `rd.rsmxm.com.cn` 的抓包基线、候选代理重载、真实账号/桌面验收与效果对照（见文末“未完成”）。

## 代码改动

- `channel/web/static/js/fork/auth-check-prefetch.js`（新增）：固定 `GET /auth/check` 的有界请求原语。
  - 从发出到响应正文读完的 5,000 ms 期限；`AbortController` 尽力取消；成功/失败/超时/取消统一清理计时器。
  - 结果从不 reject，而是捕获为 `{kind:'response'|'timeout'|'error'|'cancelled'}`；保留 HTTP 状态与正文可解析性。
  - `takeStartup()` 一次性交付启动预取；`request()` 供普通检查/手动重试；`discardStartup()` 丢弃并取消未消费预取。
- `channel/web/chat.html`：在 `<head>`、重型脚本之前同步加载该原语（带逐文件版本戳），使首次只读检查与资源下载重叠。
- `channel/web/static/js/console.js`：
  - `_accountCheckOutcome()` 一次性消费有效启动预取；过期成功结果丢弃后重新检查；预取已超时/失败时交由现有重试入口，不自动重放。
  - `refreshAccountIdentity()` 消费预取结果，超时进入新增 `timeout` 状态；保留原状态机、脚本顺序、请求去重与 `_authEpoch`/`_accountCheckSeq` 隔离。
  - `_invalidateAccountIdentity()` 丢弃未消费的启动预取（登录/退出/身份失效）。
  - `_renderSidebarAccount()` 在账号 UI 就绪时显示超时文案与重试入口。
- `channel/web/static/js/i18n/account.js`：新增 `account_check_timeout`（简/繁/英）。
- `docker/nginx.conf.example`：对公开外壳与第一方 JS/CSS 启用 gzip；`gzip_types` 排除 `application/json`、`text/event-stream`，WebSocket 升级不压缩，`proxy_buffering off` 不变。
- `docs/operations/production-readiness.md`：记录压缩范围与回退步骤。

## 本机 gzip 体积（非线上证据，仅压缩潜力）

`gzip -5`，来自当前工作树实际文件：

| 资源 | 原始 | gzip | 比例 |
| --- | ---: | ---: | ---: |
| `js/console.js` | 1,129,027 | 290,918 | 25.8% |
| `js/fork/auth-check-prefetch.js` | 4,864 | 1,839 | 37.8% |
| `css/console.css` | 225,089 | 48,650 | 21.6% |
| `chat.html`（源模板） | 267,258 | 41,056 | 15.4% |
| 第一方 js/css（`js/console.js` + `css/console.css` + `js/fork/*.js` + `js/i18n/*.js`，31 个文件） | 1,916,186 | 488,680 | 25.5% |

复现：`gzip -5 -c <file> | wc -c`。

## 本机回归结果

前置基线（改动前 `d674bc15`）已确认以下失败为**既有问题**，非本次引入：
`tests/test_sidebar_account_frontend.cjs` 7 项失败（legacy 模式/`mode:unknown`/`resetMemoryView` 夹具，已记录于 `openspec/changes/archive/2026-10-08-restore-upstream-web-parity-and-sync-gates/evidence/implementation/account-baseline.txt`）；`tests/test_console_i18n_coverage.cjs`+`parity` 3 项失败（翻译快照陈旧）。

| 命令 | 结果 |
| --- | --- |
| `node --test tests/test_sidebar_account_frontend.cjs` | 68 项，61 通过，7 失败（均与前置基线同名） |
| `node --test tests/test_account_check_prefetch_frontend.cjs` | 10/10 通过 |
| `node --test tests/test_desktop_relogin_frontend.cjs` | 8/8 通过 |
| `node --test tests/test_account_menu_no_personal_resources.cjs tests/test_401_recent_password_no_logout.cjs` | 13/13 通过 |
| `pytest tests/test_web_console_assets.py tests/test_deployment_response_compression.py tests/test_web_console_routing.py tests/test_workspace_layout_template.py -q -p no:randomly` | 32 通过 |
| `pytest tests/test_web_console_update.py tests/test_desktop_auth_flow.py tests/test_identity_auth_security.py -q -p no:randomly` | 82 通过 |
| `node --check`（console.js / auth-check-prefetch.js / i18n/account.js） | 通过 |
| `openspec validate optimize-console-login-startup --strict` | valid |

新增覆盖（`test_account_check_prefetch_frontend.cjs` + `test_sidebar_account_frontend.cjs`）：
首次请求复用、无预取时正常检查、过期成功结果丢弃重查、超时/失败不自动重放、超时渲染与重试入口、身份失效丢弃未消费预取、超时后迟到结果隔离（原有用例）、请求/正文挂起超时、取消、HTTP 状态保留、非法正文不冒充成功。

## 未完成（需目标环境，未勾选）

- 1.1 / 3.3：同环境冷暖缓存各 ≥5 次的前后对照与线上传输字节（本机数据不能替代）。
- 1.2（目标站点候选配置）/ 1.3：目标 Windows Nginx 的应用与 gzip/identity、Vary、304 的线上核验；本机无 Nginx 可执行文件。
- 3.2（浏览器部分）：未认证、有效会话刷新、强制改密/租户入口、合法深链接的浏览器验收与“初始化/轮询不重复”确认。
- 4.1：目标版本交付、代理语法校验后定向重载、真实授权账号登录与桌面重登录。
