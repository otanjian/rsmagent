# A03（11.4）生产服务器负向验证：普通 Web 无法提交本机路径/伪造 handle/复用他人选择（2026-10-14）

用户在其服务器 `https://rd.rsxm.com.cn` 登录后（`RC004` / 租户 `tnt_EA3qM-lHPLD8ZPwW`），在**容器页同源 fetch**（带会话 cookie + `X-Tenant-ID`）执行。所有请求都是**应当被拒绝**的请求；未改动任何数据。

## 1. 负向断言（全部被拒 ✓）

| # | 请求 | 结果 | 判定 |
|---|---|---|---|
| 1 | `POST /api/desktop/workspaces` 带 `absolute_path: "D:\\AIPJ\\cow-a01-demo"` | **400** `{"code":"invalid_request","message":"absolute client paths are not accepted"}` | ✓ 服务端显式拒收客户端绝对路径（`desktop.py` 的分支：`if "absolute_path" in body or "path" in body or "root" in body`） |
| 2 | 同上，带 `path` | **400** 同上 | ✓ |
| 3 | 同上，带 `root: "C:/Users/admin/.cow"` | **400** 同上 | ✓ |
| 4 | `POST /api/desktop/workspaces` 带**伪造 `device_id`** | **401** `{"code":"auth_required","message":"a native bearer session is required"}` | ✓ 仅 cookie 的 Web 会话不能注册工作区（需原生 bearer） |
| 5 | `POST /api/desktop/bindings` 带伪造 `device_id` | **404** `{"code":"resource_not_found","message":"device not found"}` | ✓ 伪造 chooser/设备 handle 无效 |
| 6 | `POST /api/desktop/bindings` 带**另一台安装**的设备 id（`dev_mY9Xc8OG7nBgoThZ17nhP2N7`） | **404** `device not found` | ✓ 不能复用他人/他机的选择记录 |
| 7 | `POST /api/desktop/sessions/session_probe/execution-target` 带伪造 `binding_id`/`device_id`/`workspace_id` | **403** `{"code":"permission_denied","message":"local root registration is loopback-only"}` | ✓ 本机根注册仅回环可达 |

## 2. 正向对照（原有能力未受影响 ✓）

| 请求 | 结果 |
|---|---|
| `GET /api/desktop/devices` | **200**，返回该账号 2 台设备（本机 Windows 行 `dev_HPttEjgBfwkUs0I6T7wENwYZ`） |
| `GET /api/desktop/meta` | **200**，`remote_web` 四项 true |
| 工作台自身的"个人项目根浏览/选择" | 正常工作（同日 A01：14:13:30 与 15:06:55 两次成功选择并落候选，见 `a01-real-device-selection.md`） |

## 3. 未执行的一项（如实登记）

- **"受控导入不受影响"的正向用例未执行**：它需要在用户账号里真正导入一份资源（写入其工作区）。为避免未经授权写入生产数据，本次只做了拒绝类探测与只读对照；如需覆盖，可由用户授权后导入一份**合成文件**（或由用户自己操作、我抓证据）。

## 4. 复现

```powershell
# 应用处于 remote 模式并已登录（CDP 9232）
python %TEMP%\cow-pack\a03-negative-tests.py
```
