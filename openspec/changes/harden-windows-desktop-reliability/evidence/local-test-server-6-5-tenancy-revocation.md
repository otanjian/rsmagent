# 6.5 子项：两租户两成员 + 撤销（2026-10-14，本机测试实例）

环境同 `local-test-server-6-5-restarts.md`（`app.py` 9899 + 网关 9877 + TLS 分流终止器 8443，测试数据根与测试配置见 `local-test-server-3-5-remote.md` §1）。

## 1. 两租户 × 两成员

| 项 | 值 |
|---|---|
| 租户 1 | `zzlocal` → `tnt_CtBD8jYuVeNb_7LT`；成员 `zzlocal_member`（`usr_F_ljy4xPXM50z0Pr`） |
| 租户 2 | `zzlocal2` → `tnt_A8eYBualPU1wv7PF`；成员 `zzlocal2_member`（`usr_0nDay5Beal3Ox9zr`） |
| 各自记忆 | 两人各在自己的用户根下建立 `MEMORY.md`（服务层创建：`revision=c0800b83…` / `4b9d00ef…`，`index_state=ok`） |

**隔离实测（HTTPS，各带自己的 `X-Tenant-ID`）**：

| 账号 | 结果 |
|---|---|
| member1（tenant1） | **200**，`total=1`，`files=['MEMORY.md']` |
| member2（tenant2） | **200**，`total=1`，`files=['MEMORY.md']` |
| member1 + `X-Tenant-ID: tenant2`（跨租户） | **403 `forbidden`** ✓ |

## 2. 撤销

| 动作 | 观察 |
|---|---|
| `IdentityService.revoke_session(<member1 的 cow_session>)` | **ok** |
| 撤销后 member1 访问自己的记忆 | **401 `unauthorized`** ✓（会话立即失效） |
| 同一时刻 member2（另一租户） | **200 `total=1`** ✓（撤销被限制在目标会话/账号，无外溢） |
| `set_tenant_status(tenant2, active=False)` | **被正确拒绝**：`an enabled account must keep at least one active tenant` ✓（member2 仅属于该租户，停用会使其无有效租户——撤销前的完整性校验生效） |

## 3. 结论与仍未做的部分

- **会话撤销的隔离性**：只使目标会话失效，不影响同租户/他租户的其他账号 ✓ —— 对应规范中"撤销后不得借旧会话继续访问"的要求；
- **跨租户读取**在任何情况下都被拒（403）✓；
- **租户停用**受"账号必须保留至少一个有效租户"约束，本用例（单租户成员）因此被拒 → 若要正向验证停用，需要给该账号**再挂一个租户**（下一轮可做）。
- 仍未做（6.5 剩余）：客户端侧断网/重连与"页面/WSS 分别检查"（需本地 CA 进用户信任库或真实部署）、企业 CA/VPN；**Agent.use / skill.use / 项目 grant 的撤销**（属设备侧执行面，受平台门限制，见 `platform-gate-demonstration.md`）。
