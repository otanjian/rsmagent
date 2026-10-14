# 生产部署复现了本 change 修复的原始故障（2026-10-14，只读核对）

用户在其服务器 `https://rd.rsxm.com.cn` 登录后（账号 `RC004`/`tanjian`，租户 `tnt_EA3qM-lHPLD8ZPwW`，code `test15`），在**未改动任何数据**的只读核对中，个人记忆接口对**本人**返回拒绝对**合法本人目录**的误报。

## 1. 现场（容器页同源 fetch，只读）

| 请求 | 响应 |
|---|---|
| `GET /api/memory?scope=personal&category=memory&page=1&page_size=5`（带 `X-Tenant-ID: tnt_EA3qM-lHPLD8ZPwW`，即**用户自己的租户**） | **403** `{"status":"error","code":"unsafe_path","message":"个人记忆目录被替换"}` |
| `GET /api/memory/personal`（同上） | **403** 同上 |
| `GET /api/memory?...`（改用**他人租户** id `tnt_N8gC2oB4jo5flatC`） | **403** `{"code":"forbidden"}` ✓ 正确拒绝 |
| 控制台界面 `/memory` 页 | 页面显示 **"个人记忆目录被替换"** + 提示"请刷新重试，登录失效时请重新登录"（**用户可见**） |
| `GET /api/desktop/meta` | **200**，`remote_web` 四项全 true，协议版本齐全 |
| `GET /api/version` | `{"version":"2.2.0","install_kind":"git","platform":"win32","update_supported":false}` |
| `GET /auth/me` | **200**，用户 `usr_hcWvTEbXtYDWq1ho`、租户 `tnt_EA3qM-lHPLD8ZPwW`、角色 `member`、`must_change_password:false` |

## 2. 与本地记录的根因**逐字吻合**

本 change 3.1 的根因记录（`memory-403-root-cause.md`）写的是：

> `common/safe_fs.py` 的 `file_lock()` 在**没有 `dir_fd` 支持**的平台（Windows）走路径回退分支，用 `_resolve_chain()` 解析**锁文件本身**；而该函数要求每一级组件都是**目录**……
> 第二次起：`.memory.lock` 已存在且是普通文件 → `UnsafePathError("non-directory component '.memory.lock'")`
> → `scope_transaction raised: code=unsafe_path status=403 message=个人记忆目录被替换`

现场返回的 `code=unsafe_path` 与 `message=个人记忆目录被替换` **完全一致**；而 `/api/version` 报 `platform: win32`（`install_kind: git`）说明**该服务器就是 Windows**、且是按 git 安装的运行形态。

## 3. 对照：本仓库已修复的代码在同一路径上正常

| 环境 | 同一"读取本人个人记忆"动作 |
|---|---|
| **生产服务器**（2.2.0，git 安装，**未含本 change 的 3.x 修复**） | **403 `unsafe_path`**（本人目录被误报为"被替换"） |
| **本机安装包的包内后端**（同一份代码 + 本 change 修复） | **200 success**（`total`、`revision`、`index_state`、`owner` 正常；见 `memory-3-5-installed-backend.md`） |
| 本机修复前的复现脚本 | `scripts/repro/repro-personal-memory-403.py` RED → 修复后 GREEN（3.1/3.2 证据） |

## 4. 结论与影响

1. **这是一次真实的价值验证**：本 change 修的 3.x 故障**正在生产环境上发生**（用户可见："个人记忆目录被替换"），而修复后的代码在同一场景通过。
2. **3.5 的"remote 只访问服务器本人记忆"正向核对因此无法在生产上完成**——不是权限/隔离问题（反向的 `forbidden` 与跨租户拒绝都正确），而是**该部署尚未包含修复**：本人记忆读取本身就 403。
3. **下一步取决于部署**：把本 change 的 3.x 修复（目前在**未提交**的工作区）部署到该服务器后，在 `/memory` 页应能正常列出本人记忆，届时可完成 3.5 的正向核对。**在此之前 3.5 不勾选**（与用户选择一致）。
4. 本次核对**全程只读**：没有在该服务器上写入、上传或修改任何记忆/配置；容器页浏览后已切回首页。
5. 附带澄清（避免误读）：`platform: win32` 出现在 `/api/version`，与"客户端是 Windows"无关——它是**服务端自身**的平台（`install_kind: git`），这也解释了为何此故障在服务端以 Windows 的 `file_lock` 分支形态出现。

## 5. 复现命令

```powershell
# 应用处于 remote 模式并已登录；CDP 端口 9232
python %TEMP%\cow-pack\remote-memory-inpage-check.py http://127.0.0.1:9232   # 正/反向核对
python %TEMP%\cow-pack\remote-memory-detail.py     http://127.0.0.1:9232   # 完整拒绝体 + 服务端版本
python %TEMP%\cow-pack\ui-memory-page-check.py     http://127.0.0.1:9232   # 控制台 /memory 页的可见错误
```

（说明：容器页的会话在**独立分区**里，CDP 取不到 cookie，且宿主 shell 无法解析该域名 DNS，故所有请求都在**页面内同源 fetch**完成。）
