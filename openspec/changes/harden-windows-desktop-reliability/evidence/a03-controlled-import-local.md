# A03 正向半段：受控导入（2026-10-14，本机测试实例）

`a03-web-refusals-production.md` 覆盖了 A03 的**拒绝类**断言（生产服务器上全部通过）。本文件补上 A03 的**正向**断言——"个人项目根浏览/选择与**受控导入**仍正常"。为避免在生产账号里写入数据，此项在**本机隔离合成服务器**上执行（环境见 `local-test-server-3-5-remote.md` §1，另加测试配置项 `project_import_source_roots` 与固定的 `local_import_token`）。

## 1. 受控导入的四个条件（代码依据）

`channel/web/fork/handlers/workspace.py`（`ProjectImportPreviewHandler` / `ProjectImportHandler`）与 `channel/web/project_import.py`：

1. **回环传输**：`is_loopback_request()`——`REMOTE_ADDR` 必须是回环**且请求不得带任何转发头**（`X-Forwarded-*`/`Forwarded`/`X-Real-IP`/`Client-IP`），所以经反代进来的"看起来是本机"的请求会被判非回环；
2. **每次启动的 token**：`local_import_token()`（本次用配置 `local_import_token` 固定，属"受控部署"选项；否则自动随机并发布到 `<data_root>/local_import.token`，0600）；
3. **已验证的库身份**（会话/成员）；
4. **一次性句柄**：预览返回，只对该次预览的 source 有效、单次消费、有 TTL。

## 2. 实测（直连 `http://127.0.0.1:9899`，回环）

| 步骤 | 结果 |
|---|---|
| 预览 `POST /api/projects/import/preview`（带 `source` + `token`） | **200**，`transport=local`，`handle=5JOJkhLIitEP4X7X`，`target=zz-import-probe`，`entries=2`，`bytes=39` |
| 预览**不带 token** | **403 `token_invalid`** ✓ |
| 直接导入**不带句柄** | **403 `handle_required`** ✓（"仅仅提供一个路径"不被接受） |
| 导入 `POST /api/projects/import`（带预览句柄） | **200 `success`**，`target=zz-import-probe`，`path=…\tenants\tenants\zzlocal\users\usr_F_ljy4xPXM50z0Pr\project(s)…` |
| **句柄复用**（同一句柄再导入一次） | **403 `handle_invalid`** ✓（单次性生效） |
| 落盘核对 | `projects/zz-import-probe/marker.txt`（26 B，内容 **`COW-CONTROLLED-IMPORT-OK`**）、`projects/zz-import-probe/second.txt`（13 B）✓ |

导入目标位于**该成员自己的用户根**下（`tenants/tenants/zzlocal/users/usr_F_ljy4xPXM50z0Pr/projects/…`），未越出个人项目根 ✓。

## 3. A03 的整体判定

| A03 断言 | 证据 |
|---|---|
| 普通 Web 提交本机绝对路径 → 拒绝 | 生产：`workspaces` 带 `absolute_path`/`path`/`root` → **400 `invalid_request`**（`a03-web-refusals-production.md`） |
| 伪造 chooser handle → 拒绝 | 生产：伪造 `device_id` 注册工作区 → **401 `auth_required`**；伪造/他人设备 id 建绑定 → **404 `device_not_found`** |
| 复用他人选择记录 → 拒绝 | 生产：复用另一台安装的设备 id → **404** |
| **个人项目根浏览/选择仍正常** | 真机：A01 两次成功选择并落候选、服务端设备行复用（`a01-real-device-selection.md`）；浏览/选择逻辑由 `test_desktop_target_resolution_acceptance.py`（19+16sub）覆盖 |
| **受控导入仍正常** | 本文件：预览→导入→落盘 全链路通过，且三个反向路径都被拒 |
| 未执行 | 在**生产账号**里真实导入一份资源（会写入用户工作区；如需要可由用户授权后以合成文件执行） |

## 4. 复现

```powershell
python %TEMP%\cow-pack\pin-import-token.py        # 固定 token + source roots（测试配置）
# 重启测试后端后：
python %TEMP%\cow-pack\local-server-controlled-import.py
```
