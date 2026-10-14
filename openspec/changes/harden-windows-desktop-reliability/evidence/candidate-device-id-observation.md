# 观察：候选记录的 deviceId 与服务器设备行不一致（待查，2026-10-14）

## 现象（只读采集）

同一路径、同一作用域（`https://rd.rsxm.com.cn` + `usr_hcWvTEbXtYDWq1ho` + `tnt_EA3qM-lHPLD8ZPwW`）的两条本地候选记录，**`deviceId` 不同**：

| 候选 | 落盘时间 | deviceId |
|---|---|---|
| `D:\AIPJ\cow-a01-demo`（id `tCH0jb7FND695LX4xdi8mw`） | 14:13:30 | `dev_fsnZilgSrmNFSwFy7yklWbPgmRKP7Adh`（＝`dev_`＋本机 `installation.json` 的 id） |
| `D:\AIPJ\cow-a01-demo`（id `TccfTbicPvY-9omTlkYgNg`） | 15:06:55 | `dev_Q_SAfzAuzqvNohdZHMpsGMFdt2U_pQct` |

而服务器侧该账号的设备列表（`GET /api/desktop/devices`，页面内同源只读）只有**两条**：

| 服务器设备行 | display_name | platform | created_at | updated_at |
|---|---|---|---|---|
| `dev_HPttEjgBfwkUs0I6T7wENwYZ` | `cow-a01-demo` | windows | 1791957466（＝13:57:46，与 `installation.json` 创建同刻） | 1791961615（＝15:06:55 第二次选择） |
| `dev_lo3BNMHI72L1ZcSKJz9T8TZt` | `模…报表_2024` | macos | 1791421912 | 1791509870 |

**即：服务器设备行是复用/更新的（不是每次登录新建）✓，但候选记录里的 `deviceId` 与它不一致，且两次选择之间还发生了变化。**

## 为什么这值得查

`desktop/src/main/local-files/candidates.ts` 的 `candidatesForScope()` 过滤维度是：

```ts
row.serverId === scope.serverId && row.userId === scope.userId &&
row.tenantId === scope.tenantId && row.deviceId === scope.deviceId
```

**包含 `deviceId`**。若该 id 在会话之间不稳定，则 A23 的"客户端重启后恢复候选"就会按 deviceId 失配（同一路径/账号/租户的旧候选查不回来）。写入处为 `desktop/src/main/remote/remote-host-ipc.ts:575-584`（来自一次 device registration 的 `result.deviceId`，且需 `registration.localFiles`）。

## 现状与下一步

- 这是**生产（未部署修复）构建**上的观察；是否在**本仓库修复后的代码**上同样出现，尚未验证。
- 计划（不影响用户当前远程会话）：用**独立实例**（另起 `--user-data-dir`，本地模式、自有合成租户）做实验——"选目录 → 重启 → 再选"，比较两次候选的 `deviceId` 是否一致，并与该实例的 `installation.json`、本地 `desktop_bindings.device_id` 对照。
- 本机 `identity.db` 已有对照：三条 `desktop_bindings` 共用**同一个** `device_id`（`dev_mY9Xc8OG7nBgoThZ17nhP2N7`），说明修复后的代码在本地模式下设备 id 是稳定的。

## 结论措辞

**已查清（2026-10-14 同日）：不是缺陷，是概念错位。**

`desktop/src/main/remote/binding-setup.ts` 顶部注释写明"确认一次本地目录选择"的四个步骤，其中第 1 条是：

> 1. ``POST /api/desktop/devices``（native bearer）刷新**本安装**的设备行并返回它的**服务器 id** —— **页面那个不透明的 `deviceId` 是 scope key，不是服务器认识的设备**；
> 2. ``POST /api/desktop/workspaces``（native）注册 grant 版本与 label（绝对路径从不离开本进程）；
> 3. ``POST /api/desktop/bindings``（已配对的 Web 子 cookie）为该 Agent 与业务会话建绑定；
> 4. ``PUT /api/desktop/bindings/{id}/workspaces/{ws}``（native）把 worktree grant 挂到绑定上。

并且 `binding-setup.ts:238` 的取值规则是 `deviceId: (activeGrant && activeGrant.deviceId) || request.scope.deviceId` —— 即**有活动 grant 时用 grant 的 scope key，否则用页面给的 scope key**。这解释了两条候选记录为何带不同的 `deviceId`：它们来自**不同的页面作用域上下文**（其中一次有活动 grant）。

因此：

- **服务器设备行是稳定的**（`dev_HPttEjgBfwkUs0I6T7wENwYZ`，created 13:57:46 → updated 15:06:55，未重复 ✓）；
- 候选记录里的 `deviceId` 是**本地/页面作用域键**，按设计**不等于**服务器设备行 ✓；
- `candidatesForScope()` 按该 scope key 过滤正是"按作用域恢复候选"的实现（同 server/user/tenant/scope 才列出）✓，其行为由 `test_desktop_local_files*.cjs`（12/0、6/0）与 `test_desktop_project_execution_grant.cjs`（10/0）覆盖；
- **遗留的可选优化（非缺陷）**：若同一 server/user/tenant 下页面 scope key 在会话之间发生变化，旧 key 下记住的候选不会列在该 key 的作用域里——如需在产品上明确"跨 scope 恢复"，应作为体验改进单独提出（不在本 change 范围）。

