## MODIFIED Requirements

### Requirement: 密码保护与受限首次改密

系统 SHALL 使用带随机盐和版本化工作因子的 PBKDF2-HMAC-SHA256 保存普通账号密码，并对不存在账号、密码错误和停用账号使用统一认证错误及限流。普通账号新建或平台重置产生的临时密码 SHALL 有到期时间且仅在生成响应展示一次；到期前可重试取得仅允许改密及退出的受限会话，不维护首次登录消费状态。本人改密 SHALL 验证原密码，成功后清除强制改密状态并撤销全部旧会话；失败不得部分修改密码或会话。Cookie 状态变更 SHALL 校验 CSRF 与来源，密码及登录凭据 MUST NOT 出现在 URL、日志或审计中。

IM 访客 SHALL 使用不可验证的密码占位且不生成初始密码，不进入首次改密流程；登录资格仍必须检查 account_type，MUST NOT 仅依赖密码不可猜测。普通改密与重置拒绝访客，未知类型不能按普通账号放行。

#### Scenario: 临时密码登录后重试改密
- **WHEN** 用户以尚未到期的临时密码登录，改密未成功后再次登录或重试
- **THEN** 系统仍仅允许改密和退出，拒绝平台、成员及业务访问；改密成功后临时密码与全部旧会话失效并要求重新登录

#### Scenario: 临时密码过期或认证被探测
- **WHEN** 请求使用已过期临时密码、错误密码或不存在账号，或超过限流阈值
- **THEN** 系统拒绝认证且不泄漏账号存在性；达到限流阈值时返回 429 和可重试提示

#### Scenario: 跨站改密或持久化失败
- **WHEN** Cookie 改密请求未通过 CSRF/来源校验，或密码和审计无法同事务提交
- **THEN** 系统拒绝变更，原密码及会话保持原状态，不提示改密成功

### Requirement: 身份模式唯一与独立登录会话

系统 SHALL 仅以数据库身份模式运行，平台登录使用普通全局账号密码与独立 AuthSession；MUST NOT 提供 `legacy` 模式，MUST NOT 保留共享密码或旧 HMAC 认证接口。会话凭据 SHALL 是高熵、不透明、可到期和撤销的随机值，数据库仅保存摘要、用户及必要会话状态，不保存当前租户或权限快照。业务 session、Agent ID、客户端自报身份、共享密码和旧 HMAC token MUST NOT 作为认证凭据。`/auth/login/check/logout` SHALL 保留原兼容字段并增量提供账号及有效租户信息；Web 使用 HttpOnly、SameSite Cookie，HTTPS 使用 Secure，Web 兼容 token 字段为空且不向脚本交付可重用会话令牌。Desktop 等程序化客户端 SHALL 通过受验证的 `Authorization: Bearer` 携带同一会话值访问，MUST NOT 继续使用旧 `cow_auth_token` 或 URL 查询参数 token。身份库不可用 SHALL 返回 503，不得回退共享密码、免登录或匿名身份。

im_guest SHALL 仅经可信 IM 入站取得受限业务上下文，MUST NOT 创建或消费普通/受限 AuthSession、Desktop 授权及 token 兑换结果或现有个人 API 凭据。统一账号资格检查 SHALL 同时覆盖签发与使用边界；角色调整、历史会话或手工请求不得绕过，认证失败沿用统一错误与限流。

#### Scenario: Web 使用数据库登录
- **WHEN** 有效普通账号完成认证
- **THEN** 系统设置受保护 Cookie 并返回兼容认证字段及本人身份信息，后续登录状态独立于聊天 session，响应不包含可重用令牌或密码摘要

#### Scenario: 程序化客户端使用 Bearer
- **WHEN** Desktop 或程序化客户端通过登录接口取得会话，并以同一会话值作为 Bearer 访问业务接口
- **THEN** 系统按该会话解析身份并授权，不要求也不接受旧共享密码 token

#### Scenario: 拒绝旧凭据和故障回退
- **WHEN** 请求携带旧共享密码、旧 HMAC token、URL 查询参数 token 或仅有客户端身份声明，或无法读取身份库
- **THEN** 旧凭据和声明不能通过认证；身份库故障返回 503，系统不开放旧认证路径或业务数据

#### Scenario: 访客尝试平台或桌面认证
- **WHEN** im_guest 尝试 Web/Desktop 登录、桌面授权兑换或个人凭据入口
- **THEN** 系统拒绝签发平台会话或令牌，不提供改密后登录路径

#### Scenario: 访客历史会话被消费
- **WHEN** 请求持有指向 im_guest 的历史或手工构造有效会话行
- **THEN** 会话消费边界仍因账号类型拒绝，不能进入平台或租户业务

### Requirement: 消费者开放不得绕过认证

系统 SHALL 对已开放的运行消费者逐请求验证身份与授权，MUST NOT 因已有 Cookie、Bearer 或查询参数 token 放行未授权能力，MUST NOT 恢复 URL 长效令牌认证或借用人工 Membership，MUST NOT 提供免认证或匿名运行入口。

经过提供方验证的 IM 访客 SHALL 在执行前解析到持久用户、有效租户成员及获准渠道，按当前权限执行；不持有平台登录会话不等于免认证。访客缺少可信 IM 来源或试图建立独立后台任务时 MUST 拒绝。

#### Scenario: 未授权访问运行入口
- **WHEN** 已认证但无相应权限的用户调用已开放的运行入口
- **THEN** 服务端按身份与授权拒绝，不因已有 Cookie 或 Bearer 放行

#### Scenario: 后台消费者身份缺失
- **WHEN** 后台线程或定时触发丢失身份上下文后尝试执行消费者动作
- **THEN** 系统拒绝执行并记录可诊断告警，不降级为匿名或服务身份
