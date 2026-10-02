# 接口与实施契约

本文件细化 `design.md` 的实施决定；可观察行为以 `specs/` 为准。所有接口均为拟新增或拟修改，不能视作当前已经可调用。字段采用 snake_case，时间用 UTC ISO 8601，大小用字节，ID 为不可猜测随机值但不代替授权。下面路径均相对用户配置的精确 HTTPS origin；V1 不支持 base path。

## 1. 通用响应与作用域

普通 JSON 使用以下格式，失败必须有真实非 2xx HTTP 状态；异步创建使用 202，不用 HTTP 200 隐藏错误。沿用现有接口的兼容格式，不批量改写旧 API。

```json
{"status":"success","data":{},"request_id":"req_..."}
```

```json
{"status":"error","code":"device_offline","message":"目标设备当前离线","retryable":true,"request_id":"req_..."}
```

三种凭据域：`W` 为关联 Web Cookie（写请求保留 CSRF/来源检查），`N` 为服务器登记来源的原生 Bearer，`P` 为无身份的公共元数据。所有租户接口固定 `X-Tenant-ID`，服务端重验有效 User/Membership/Tenant；账号/配对/设备登记接口不要求伪造租户。Web/原生配对不允许客户端选择任意 user_id。对象不存在或不属于调用者按既有 404/403 隐藏规则处理，不泄漏其他设备元数据。

业务绑定 `B` 的验证顺序：有效 N/W → 活动父子 link → 当前真实用户 → 有效租户成员 → Agent 同租户及可用 → 业务 session 本人归属 → 设备本人归属 → workspace/grant 版本 → 目标操作授权。每次命令、分块和最终发布执行，不能只在握手时检查。

`client_files` / `client_inspect` 工具走现有 `tool.execute` 与相应资源执行授权、`agent.use` 和 owner 校验；Web 本地文件面板也调用同一服务。连接本人设备与选择目录属于本人配置动作，不单独授予读执行；配置成功但工具未获权时清楚显示不可用于 Agent。管理员的既有资源 grant 规则保持，但没有任何管理员旁路能跳过本人设备/目录匹配。

## 2. 阶段一：连接元数据与登录引导

| 方法与路径 | 凭据 | 输入/输出及约束 |
|---|---|---|
| `GET /api/desktop/meta` | P | 公共协议版本、支持 origin 入口与部署开关；不返回用户、目录、连接清单 |
| 既有 `/auth/desktop/authorize`、`/auth/desktop/token` | 既有 PKCE | 不改变系统浏览器确认与单次 code 消费；成功事务新增 native 来源登记 |
| `POST /auth/desktop/web-session` | N | `{bootstrap_id,instance_id,web_protocol:1}`；原子创建 link/子会话，专用响应处理 Set-Cookie |
| `GET /auth/desktop/web-session` | N | 返回当前 link_id、user_id、期限、状态；不返回 Cookie 或 token |
| 既有 `POST /auth/logout` | N 或 W | 若为桌面关联会话则撤销配对；普通 Web 沿用原独立会话退出 |

`/api/desktop/meta` 示例：

```json
{
  "status":"success",
  "data":{
    "remote_web":{"implemented":true,"accepted":false,"configured":false,"available":false,"reason":"not_accepted"},
    "protocols":{"web_session":{"major":1,"minor":0},"bridge":{"major":1,"minor":0},"files":{"major":1,"minor":0}},
    "entry_path":"/",
    "console_entry_paths":["/"],
    "features":{"local_files":false,"local_processing":false,"notifications":false}
  }
}
```

该示例用于说明 schema，不是当前验收状态。`console_entry_paths` 从真实 shell 路由登记生成，不能因为示例只有 `/` 就遗漏后续合法入口；这些路径不包含 `/preview`、`/uploads`、`/api/file` 等内容页。主进程只用它收窄本地已识别的工作台入口规则，服务器响应不能授权跨 origin 或任意协议。

引导端点要求：

- `bootstrap_id`/`instance_id` 为主进程随机生成的 128 位以上标识，不能由远程页面任意指定；唯一键 `(native_session_id,bootstrap_id)`。
- 请求仅由专用 broker 方法发送，不进入 `desktop-request` 的任意路径代理；`http-relay` 同样禁止。
- 成功只在 Set-Cookie 中交付子会话值，Cookie 为 host-only、HttpOnly、Secure、SameSite=Lax、Path=/；不得返回 Domain 跨域 Cookie，不得与 native Bearer 相同。客户端内存分区不持久化。
- 主进程禁止认证重定向，严格按配置 origin 验证服务端 TLS；不把 Set-Cookie 交给 preload 或业务 JSON。Web 子会话必须被身份服务当作独立 session 解析并强制检查父会话。
- 重放 `bootstrap_id` 返回 409 `bootstrap_consumed`；新的引导 ID 撤销前一子会话。网络响应丢失不复制旧秘密，新的成功引导只保留一个活动子会话。
- 所有响应 `Cache-Control: no-store`；不得把响应头记入调试日志。父/子会话、link、原生来源登记与必需审计采用同一 DB 事务。

## 3. 原生桥允许列表

远程页面只见 `window.desktopHost`，版本为 `1.0`。下列方法不等于原生凭据接口，所有返回数据均无秘密。

| 方法 | 参数 | 行为 |
|---|---|---|
| `getCapabilities()` | 无 | 支持的 bridge major/minor 与只读功能位 |
| `suspendLocalContext()` | 无 | 立即暂停本地请求并递增 generation；幂等 |
| `bindContext()` | `{binding_id}` | N 向服务器解析，再核对当前视图/generation；仅阶段二启用 |
| `chooseWorkspace()` | `{binding_id}` | 打开可信原生授权 UI；返回 workspace ID、展示名、grant_version，不返回绝对路径 |
| `disconnectWorkspace()` | `{workspace_id}` | 本地先停止，再同步服务器撤销；离线也停止读取 |
| `saveArtifact()` | `{artifact_ref}` | 解析当前获权服务器产物，显示另存为；不接受 URL/路径 |
| `openExternal()` | `{url}` | 仅用户激活的合法 http/https 外链；不携带鉴权头 |
| `notify()` | `{notification_ref}` | 主进程从本人服务器记录取可信元数据，不照单展示任意正文 |
| `onHostEvent()` | 受限订阅 | 返回取消函数；事件仅含连接/下载状态和非秘密资源 ID |

本地壳另有可信 bridge 负责 server profiles、原生登录、窗口与更新设置，不能让远程页面访问。原有 `electronAPI` 保留给本地 React 模式，不注入 remote view。主进程校验 document generation，不仅做 hostname 检查。网页发起授权 UI 前提供用户激活证明并限流，远程事件不能无限弹出原生对话框。

## 4. 阶段二：设备、绑定与目录

| 方法与路径 | 域 | 输入/输出 |
|---|---|---|
| `POST /api/desktop/devices` | N，账号 | `{installation_id,display_name,platform,client_version}` → 本人 device ID；owner 从会话取 |
| `GET /api/desktop/devices` | N/W，账号 | 仅本人登记设备及脱敏状态 |
| `DELETE /api/desktop/devices/{id}` | N/W，账号 | 禁用本人设备，吊销其绑定/grant，不删除本机文件 |
| `POST /api/desktop/bindings` | W，租户 | `{device_id,agent_id,business_session_id,context_nonce}` → 绑定；Cookie 必须关联当前 native link |
| `POST /api/desktop/bindings/resolve` | N，租户 | `{binding_id}` → 经服务端验证的非秘密上下文；不能解析其他 native link |
| `DELETE /api/desktop/bindings/{id}` | N/W，租户 | 幂等撤销，停止新命令/未发布传输 |
| `POST /api/desktop/workspaces` | N，租户 | 原生明确选择后 `{device_id,label,grant_version}` → workspace ID；禁止 absolute_path 字段 |
| `PUT /api/desktop/bindings/{id}/workspaces/{workspace_id}` | N，租户 | `{grant_version}` 将本人原生授权绑定本人业务会话 |
| `DELETE /api/desktop/workspaces/{id}` | N/W，租户 | 撤销服务器 grant；在线通知客户端，离线以后不得自动恢复 |
| `POST /api/desktop/commands` | N/W 或内部 runtime，租户 | `B + op + params` → 202 request_id；同一权限服务消费，不信任客户端声称 runtime |
| `GET /api/desktop/commands/{id}` | N/W，租户 | 仅本人所属业务会话读取有界状态/结果 |
| `POST /api/desktop/commands/{id}/cancel` | N/W，租户 | 幂等取消；已完成返回 completed，不谎称撤回已交付数据 |

`context_nonce` 是浏览器本地当前 generation 对应随机值，不作为权限真值或服务器全局 context_version。阶段二只支持由关联桌面 Web 容器创建的文件绑定；普通外部浏览器不能只凭知道 device ID 使用后台设备。若以后需要“浏览器聊天＋桌面助手”，单独增加配对授权流程。

工作区引用示例：

```json
{"source":"client","device_id":"dev_x","workspace_id":"ws_x","relative_path":"2026年/销售台账.xlsx","version":{"size":24832,"mtime_ns":"1790572800000000000","file_identity":"opaque"}}
```

`relative_path` 只用 `/`，不做能改变实际文件名的 Unicode 归一化；拒绝空组件、`.`、`..`、NUL、绝对路径和平台特殊名字，URI 展示字符串只解码一次。根目录由操作中省略 path 表示，不接受 `..`。`file_identity` 是本地生成的作用域内不透明标识，不是原始 inode/本机路径。sha256 在读出后补充。

## 5. 网关部署与命令协议

新增受监督入口 `python -m integrations.desktop.gateway --bind 127.0.0.1 --port 9877`（拟建）。默认端口可配置；Docker 内只绑定受控内部网络，不独立暴露公网。网关与 Web 使用同一代码版本、配置及本机 identity.db，数据库工作放在线程池，禁止阻塞 aiohttp event loop。

反向代理仅将 `/api/desktop/connect` 升级请求交给网关；其余 `/api/desktop/*` 仍交给现有 Web。上游必须保留 Host/可信代理来源处理，禁止客户端伪造 Forwarded headers 改变精确 origin。生产需要 Upgrade 转发和至少 90 秒空闲超时，数据块请求上限至少 4 MiB 加协议头。

网关握手必须带 N Authorization，拒绝 Cookie-only、查询 token、非法 Origin；允许主进程无 Origin，若提供则必须精确匹配。连接后客户端 `hello` 带 device_id、协议范围和能力位，服务器返回随机连接 epoch；设备 ID 仍按 N 的 user 校验。只有服务器确认的 epoch 可处理后续帧。

```json
{
  "v":1,"type":"command","request_id":"cmd_x","connection_epoch":"ce_x",
  "binding_id":"bind_x","workspace_id":"ws_x","grant_version":3,
  "op":"read_text","params":{"relative_path":"说明.txt","offset":0,"limit":16384,"encoding":"utf-8"},
  "deadline":"2026-09-28T08:01:00Z","params_sha256":"..."
}
```

响应 `type=ack|result|error|cancelled|heartbeat`，命令响应必须回传 request_id/epoch/binding/grant_version；连接级 heartbeat 仅带连接标识与 epoch，不伪造业务绑定。结果帧上限 64 KiB，目录分页同时受编码后字节预算约束，不能只按 200 项计数；大结果必须分步骤或传文件。客户端身份字段不覆盖记录 owner；文件正文为不可信数据，不可被解释成协议指令。

操作 schema：

| op | 参数 | 返回与限制 |
|---|---|---|
| `list` | `relative_path?`, `cursor?`, `limit<=200` | entries、next_cursor、skipped；cursor 绑定根版本与枚举状态，变化后拒绝旧 cursor |
| `stat` | `relative_path` | 普通文件/目录类型、大小、版本；不泄漏宿主绝对路径 |
| `search` | `relative_path?`, `query`, `mode=name|text` | 字面量搜索，最多 100 条；V1 不支持远程正则、Shell 或 Office 全文解析 |
| `read_text` | `relative_path`, `offset`, `limit<=16384`, `encoding` | offset 单位为字节，base64 字节片段及 next_offset/version；服务器按声明编码处理边界，不丢字符 |
| `materialize` | `relative_path`, `expected_version?` | 使用传输协议返回 committed artifact_ref |
| `inspect` | `processor_id`, `source_ref`, `options` | 阶段三固定处理器，未开放时拒绝 |

命令状态：`queued -> dispatched -> acknowledged -> running -> succeeded|failed|cancelled|expired`。持久 outbox 在发出前 CAS 领取 lease；超时可重投同一 ID，不能新建业务执行。每个设备同一时刻只允许一个获权连接 epoch，新连接原子取代旧租约；旧 gateway 回写被 fencing 拒绝。

每帧操作前重验 native/link/binding/grant，心跳额外检查撤销与租约；不能把心跳周期当作下一操作允许延迟撤权的理由。权限变更期间已开始的 OS read 不能保证收回已经读取的字节，但结果交付前必须再次校验。

## 6. 传输 HTTP 协议与发布

以下端点仅 N 调用，并绑定对应已获权命令；不允许浏览器凭任意 transfer ID 注入文件。普通网页上传继续使用原有 `/upload`。

| 方法与路径 | 契约 |
|---|---|
| `POST /api/desktop/transfers` | `{request_id,source_ref,source_version,total_bytes,filename}`；幂等键为 command ID + source version；预留额度后返回 transfer ID、chunk_size、expires_at |
| `PUT /api/desktop/transfers/{id}/chunks/{offset}` | 二进制正文，Content-Length 与块摘要；顺序写入，同 offset 同 hash 幂等成功，异内容 409 |
| `GET /api/desktop/transfers/{id}` | 返回 state、acknowledged_offset、期限及已提交 artifact_ref；无路径或秘密 |
| `POST /api/desktop/transfers/{id}/commit` | `{total_bytes,sha256,source_version_after}`；完整性/版本/归属/额度/审计通过后原子发布 |
| `DELETE /api/desktop/transfers/{id}` | 取消未提交传输，重复取消幂等；已提交文件不在此接口静默删除 |

状态：`reserved -> receiving -> verifying -> publishing -> committed`；前四态可以转 `cancelled|expired|failed`，终态不反向跳转。`commit` 重复调用返回原提交结果，不重复计量；失败后重启用同一记录查询/修复，不猜测已成功。

发布账本分为“持久 intent、原子文件改名、DB 完成”三步，不假定跨文件系统与数据库原子事务。所有普通文件入口必须拒绝未 committed 路径，包括 workspace tree/search/raw/preview/agent materialization。存在孤儿目标时 reconciler 比对摘要、归属、审计/预留记录后处理，禁止直接扫目录视为已发布。

额度适配扩展现有 quota service，租户总量和可选个人上限均检查；预留与实际使用在同一身份库事务内切换。存储指标按实际存量计算，不随日/月窗口清零；物化文件真实删除后才减少 used。临时文件也占用预留，不能因为没发布就不计量。配额服务不可用返回 `quota_unavailable`，此能力不使用通用 fail-open 选项放行。

源文件版本核验使用 file identity + size + mtime，并计算最终 hash。文件打开后被替换应继续持有原对象或失败；不重新按旧字符串路径跳到新对象。已有服务器缓存可直接用于明确引用的版本，若用户要求“最新”，必须联系设备重新确认。

## 7. 默认限额与错误码

以下是 V1 可配置默认值，不代表当前代码已设置。实施时统一发布到 capabilities；客户端可以更严格，不能自行放宽服务器上限。

| 项目 | 默认值 |
|---|---|
| WSS 单帧 / 控制结果 | 64 KiB |
| 心跳 / 离线判定 / lease | 20 秒 / 60 秒 / 60 秒，续租必须 CAS |
| 重连 | 1、2、4、8、16、30 秒上限，±20% 抖动；身份错误停止重试 |
| 同设备并行命令 / 待处理队列 | 4 / 32，超限 429；不同任务公平调度 |
| 普通读取截止 / 离线等待 | 60 秒 / 30 秒；超时返回可恢复错误 |
| 目录每页 / 单次递归搜索候选 | 200 / 10,000，达到上限返回 truncated |
| 单文本搜索文件 / 搜索总时间 | 2 MiB / 30 秒 |
| 文件传输单块 / 窗口 | 4 MiB / 每文件 1 块在途 |
| 单文件 / 单任务新增输入 | 512 MiB / 2 GiB，仍受实际剩余额度限制 |
| 每设备并行传输 | 2 |
| 传输超时 | 单块 60 秒，整体 30 分钟，可查询已确认状态 |
| 未提交暂存 TTL | 24 小时，取消优先立即回收 |
| 任务输入保留 | 任务结束后 7 天，运行引用延长；手动删除受现有权限控制 |
| CSV/XLSX 本地输入 / 总输出 | 50 MiB / 1 MiB |
| 解析时限 / 工作进程内存 | 60 秒 / 512 MiB，平台强制限制 |
| XLSX 展开总量 / ZIP entry 数 | 200 MiB / 10,000，同时限制实际流式展开 |
| 解析行数 / 列数 / 单值长度 | 100,000 / 256 / 32 KiB，超限明确返回或截断标识 |
| 样本返回 | 默认 20 行，最大 100 行；总输出限制优先 |

| HTTP/协议码 | code | 客户端处置 |
|---|---|---|
| 400 | `invalid_request`, `invalid_path`, `unsupported_origin` | 修正输入，不自动重试 |
| 401 | `auth_required`, `session_revoked` | 暂停连接、清理可见业务，重新授权 |
| 403 | `permission_denied`, `grant_revoked`, `unsafe_path` | 停止动作；不尝试换身份/路径绕过 |
| 404 | `resource_not_found` | 不泄漏是否存在其他用户资源 |
| 409 | `bootstrap_consumed`, `stale_context`, `file_changed`, `chunk_conflict` | 各按引导/作用域/版本策略恢复，不盲目重放写入 |
| 410 | `transfer_expired` | 清理旧传输，必要时重新申请额度 |
| 413 | `limit_exceeded` | 展示具体限额，选择较小范围 |
| 422 | `unsupported_format`, `checksum_mismatch` | 不执行或发布损坏结果 |
| 429 | `queue_full`, `quota_exceeded` | 按 Retry-After 或用户动作重试，不绕过额度 |
| 503 | `feature_unavailable`, `device_offline`, `quota_unavailable`, `audit_unavailable` | 按依赖状态恢复，页面不转为空列表 |
| 504 | `deadline_exceeded` | 停止等待，显示已完成/未完成步骤 |

WSS error/result 使用相同 code/retryable，不靠 close 文本传业务内容。非法帧、超长帧和未知 major 直接关闭；取消/完成竞争以 DB 终态 CAS 的胜出结果为准，不能同时报告成功和已取消。

## 8. 固定本地解析器

`csv.inspect.v1` 参数：`encoding=utf-8|gb18030`（默认 utf-8，解码失败不猜测）、`delimiter` 单字符（默认逗号）、`header=true|false`、`sample_rows<=100`。返回列名、扫描行数、样本、编码、是否完整；不得接受 Python 表达式、路径列表或动态模块。

`xlsx.inspect.v1` 参数：`sheets` 为最多 10 个表名，空则仅列清单，`sample_rows<=100`、`value_mode=cached|formula_text`。使用只读解析，禁外部链接，检查容器实际格式及宏部件；不支持 `.xls/.xlsm/.xlsb`。cached 模式说明值是文件内已有缓存、可能为空或陈旧，不宣称已计算公式。

返回统一包含 `{processor_id,processor_version,source_sha256,processed_scope,truncated,warnings,data}`。取消或时限触发终止 worker、回收快照；输出 JSON 采用 schema 校验后才交付服务器。worker 不接收服务器 URL、凭据、网络参数；网络访问实际阻断由平台运行限制实现，不能仅因没有网络参数就宣称阻断。

## 9. 代码组织与接缝清单

以下新增位置是实施目标，可以按相邻命名惯例调整，但职责不得合并成第二套身份/业务真值。

| 目标 | 文件/目录 |
|---|---|
| 客户端模式、配置与连接 | `desktop/src/main/remote/{profiles,connection,web-container}.ts` |
| 原生认证交接 | `desktop/src/main/auth-broker.ts` 的受限扩展、`remote/web-session.ts` |
| 窄桥及本地文件 | `desktop/src/main/remote-preload.ts`、`local-files/{service,grants,transfer}.ts` |
| 路径 helper | `desktop/native/fs-guard/`，平台编译、锁定依赖及打包签名 |
| 网关客户端 | `desktop/src/main/remote/device-connection.ts` |
| 本地固定解析 | `desktop/build/local-worker/`、`desktop/src/main/local-processing/` |
| Web 环境适配 | `channel/web/static/js/fork/desktop-host.js`、配套样式/片段，注册装配接缝 |
| HTTP handlers | `channel/web/fork/handlers/desktop.py`，在 route_registry 逐方法登记 |
| 认证与数据迁移 | `auth/desktop_auth.py`、`auth/session.py`、`auth/store.py`，原生来源与父子检查接缝 |
| 文件业务服务与网关 | `integrations/desktop/{access,store,commands,transfers,gateway,reconcile}.py` |
| 工具消费 | `agent/tools/client_files/`、`agent/tools/client_inspect/`，工具注册和授权声明 |
| 配额与审计适配 | 复用 `auth/service.py`、`auth/audit.py`；仅扩展指标/预留接口及消费点 |

禁止在上述模块直接查询用户角色后自行拼“管理员放行”；使用现有统一授权服务。所有新增 API/WS frame 操作均列入正反向测试，HTTP route 覆盖检查与独立 gateway action 覆盖检查必须同时存在。
