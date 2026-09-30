# 测试身份矩阵（task 1.2）

本文件记录本 change 使用的**合成**测试身份与独立客户端配置目录，供后续各组测试复用。
不包含任何开发者个人凭据或真实业务数据。

## 合成身份

构造入口：`tests/desktop_identities.py::DesktopIdentities.build(harness)`；
矩阵断言：`tests/test_desktop_identities.py`。所有身份建立在一个临时
`identity.db` 与临时租户根上（`tempfile.TemporaryDirectory`）。

| 代号 | 身份 | 获准操作（在矩阵中被断言为 200） |
| --- | --- | --- |
| `PA` | 平台管理员（harness `root`） | `/api/desktop/meta`、`/api/platform/users`、`/api/tenant/permissions` |
| `U1` | 租户 A 普通成员（`chat.use`/`agent.use`/`agent.read` + Agent read grant） | `/api/desktop/meta`、`/api/tenant/permissions` |
| `U2` | 同 `U1` 的第二个成员 | 同 `U1` |
| `TA` | 租户 A 租户管理员（内置 `tenant_admin` 角色） | `/api/desktop/meta`、`/api/tenant/permissions`、`/api/tenant/roles` |
| `UP` | 租户 A 成员，角色不携带任何权限 | `/api/desktop/meta`、`/api/tenant/permissions` |
| `FB` | 租户 B（code `other`）成员 | `/api/desktop/meta` |
| `ANON` | 无凭据 | `/api/desktop/meta` |

被拒绝的操作同样声明在 `DENIED` 并由测试断言，例如 `U1`/`U2`/`UP`/`FB` 不能访问
`/api/platform/users` 与 `/api/tenant/roles`，`ANON` 不能访问任何租户/平台接口。

`U0`（完全无租户成员的账号）由既有“仅账号”会话路径覆盖：控制台
`zeroTenant` 分支与 `tests/test_web_database_capability_acceptance.py` 已覆盖该形态，
本 change 不另造第二条构造路径，避免出现两套含义不同的“零租户”身份。

## 独立客户端配置目录

桌面端本地配置（`desktop-remote.json`）在测试中始终指向
`fs.mkdtempSync(os.tmpdir()/desktop-profiles-*)`，由
`tests/test_desktop_remote_config.cjs` 建立；断言包括 `0o600` 权限与
“更高版本配置被拒绝且不覆盖”的行为。测试不读取也不写入任何真实
`app.getPath('userData')`。

## 与能力矩阵的关系

四个桌面切片（`desktop_remote_web`、`desktop_local_files`、
`desktop_local_processing`、`desktop_native_notifications`）当前
`implemented=False`、`accepted=False`、`open={}`；`test_desktop_identities.py`
断言它们在身份齐备时**仍然是关闭的**，因此本组测试只验证边界（谁能调用什么），
不会因为测试数据存在而被误认为远程工作台已开放。
