## MODIFIED Requirements

### Requirement: Desktop 主进程独占会话并约束 IPC 传输

原生 Bearer SHALL 仅保存于主进程内存。本地 React 模式和原生服务 SHALL 由统一 broker 为 JSON、上传、流、语音、预览、下载及本机导入附加到准确后端 origin；远程 Web 容器的业务请求 SHALL 使用由原生会话受控派生、独立且关联撤销的 HttpOnly Cookie 会话，不在 Web 页面使用原生 Bearer；MUST NOT 通过 renderer、preload 返回值、浏览器存储、URL、日志或遥测交付令牌。应用重启 SHALL 重新授权，后端 origin 改变 SHALL 清除旧上下文并重新授权。回调 code/state/verifier 及完整回调 URL SHALL 脱离日志和遥测，授权响应与回调页面 SHALL 禁止缓存，回调页面 SHALL 禁止泄露引用来源。

可信本地界面与远程工作台 SHALL 使用不同的 preload 允许列表；远程工作台不得调用本地界面的通用业务代理、认证接口或文件系统原语。preload SHALL 仅暴露其所属环境允许的窄化 IPC，主进程 SHALL 验证 sender 是受信窗口的主 frame 及明确登记的页面入口，只接受经允许列表解析的业务 action/相对路径，不接受调用者设置认证头、任意绝对 URL 或覆盖后端 origin。broker SHALL 拒绝携带凭据的重定向。通用 `httpRelay` MUST NOT 访问原生认证端点、loopback 回调或已认证后端，也不得复用 broker 凭据。业务授权仍 SHALL 在服务端逐请求完成。

在线退出 SHALL 先撤销原生 AuthSession 及其关联 Web 会话再清理主进程会话和远程容器；远程 Web 主动退出也 SHALL 撤销该配对，普通独立浏览器会话不因该配对退出而失效；撤销失败 SHALL 停止业务请求并明确告知未完成服务端撤销，不得仅清理界面即宣称已注销。取消、超时与失败 SHALL 关闭临时监听并清理临时秘密。

#### Scenario: 不受信页面调用 IPC
- **WHEN** 子 frame、未登记页面或非受信窗口请求令牌、覆盖认证头或访问任意地址
- **THEN** 主进程拒绝调用且不暴露会话，通用转发接口不能作为旁路

#### Scenario: 携带凭据的请求跳转
- **WHEN** 已认证请求返回重定向，或用户切换到另一个后端 origin
- **THEN** broker 不转发原凭据；后端切换需要新的授权事务

#### Scenario: 各传输路径保持主进程令牌边界
- **WHEN** Desktop 上传、重连流、预览或下载文件
- **THEN** 本地 React/原生传输使用 broker 当前会话，远程 Web 传输使用配对 Cookie 及经验证的租户或资源归属；页面脚本仅取得业务数据或短生命周期 blob，不读取任何可重用会话凭据

#### Scenario: 在线退出或撤销失败
- **WHEN** 用户退出且服务端成功撤销，或撤销请求失败
- **THEN** 成功时服务端拒绝旧会话并清理本地状态；失败时停止业务请求并显示撤销未完成，不宣称旧会话已失效

#### Scenario: 远程 Web Cookie 不等于原生令牌
- **WHEN** 远程工作台读取资料、发起上传或重连事件流
- **THEN** 使用独立的 HttpOnly Cookie 会话按现有 Web 契约逐请求授权，原生 Bearer 不写入 Cookie、renderer 或页面 URL


### Requirement: Desktop 各类传输携带一致的已验证上下文

本地 React/原生服务的 JSON、上传、语音、流式连接及重连、预览、下载和本机导入 SHALL 使用同一 broker 的有效会话；远程 Web 容器 SHALL 使用与该原生身份配对的独立 Cookie 会话。两种传输的租户业务均使用经验证的目标租户，可加头的租户请求 SHALL 携带 `X-Tenant-ID`；原生服务使用 Bearer，Web 请求使用 Cookie 及适用 CSRF/来源校验；账号个人身份域及平台请求不要求租户选择，不能因为缺少 tenant 被客户端拦截。不能携带头的传输 SHALL 通过受保护的资源派生上下文或带头读取完成，不得把 AuthSession 放入 URL。平台管理目标 SHALL 不替换当前业务租户，个人业务不得仅因 URL 含 personal 而免去租户授权。

#### Scenario: 登录后上传并回读附件
- **WHEN** 成员在 Desktop 当前租户的获准会话上传附件并打开预览
- **THEN** 写入与回读归属一致，不因遗漏租户上下文返回 missing_tenant，也不落入默认智能体空间

#### Scenario: 服务重连及新增业务方法
- **WHEN** 流重新连接或客户端调用一个使用公共传输入口的新业务方法
- **THEN** 请求仍按当前可信上下文鉴权，不沿用旧租户，不因新方法未单独配置而漏掉授权信息

#### Scenario: 网页和文件连接器的作用域一致
- **WHEN** Web 会话申请本地文件绑定并向主进程交付绑定标识
- **THEN** 主进程经原生会话向服务器解析绑定，服务端验证双方同一配对、用户、有效租户及业务 session；不采信页面自报的用户、角色或租户作为授权真值


## ADDED Requirements

### Requirement: 原生会话受控引导独立 Web 会话

远程模式 SHALL 在完成既有系统浏览器 PKCE 授权后，由主进程向精确后端 origin 申请一次性 Web 会话引导。服务端 SHALL 验证该原生会话的服务端签发来源，不能只凭 Bearer 形式、公开 client_id 或 User-Agent 判断。服务端创建不同凭据值的关联 Web AuthSession，通过仅由主进程处理的 Set-Cookie 交付；主进程仅安装到本次远程容器的内存会话分区，页面 JSON、IPC、URL及日志均不接触凭据。引导事务 SHALL 原子、单次消费、绑定父会话和引导 ID；重放不再次返回 Cookie，响应丢失后的新引导先撤销前一子会话。

关联 Web 会话 SHALL 在每请求验证父原生会话仍有效，过期时间不得超过父会话；父会话失效、配对退出、账号停用或改密后，配对的业务与本地文件消费者均不可继续。应用重启 SHALL 销毁内存分区并重新执行原生授权，不能从磁盘恢复 Cookie 或 Bearer。既有未登记签发来源的原生会话 SHALL 重新授权，不能推断来源后赋权。

#### Scenario: 单次引导与并发重放
- **WHEN** 同一引导 ID 被两个 worker 同时处理
- **THEN** 至多创建一个关联 Web 会话并交付一次 Set-Cookie，重复请求不获得新的会话秘密

#### Scenario: 普通 Web Cookie 冒充原生会话
- **WHEN** 普通 Web 会话的值被当作 Bearer 提交引导请求
- **THEN** 服务端因缺少原生签发来源拒绝，不创建子会话

#### Scenario: 主进程安装 Cookie 后父会话失效
- **WHEN** 原生会话被撤销而 Web Cookie 尚未自然到期
- **THEN** Web 下一次请求和连接器下一次操作均拒绝，不能凭独立 Cookie 延续已撤销授权

### Requirement: 本地作用域绑定不改变每标签租户语义

远程 Web SHALL 保留各标签的独立租户选择，不在 AuthSession 保存 current_tenant，也不建立全局租户广播。单个桌面远程容器的本地能力 SHALL 通过独立绑定记录关联当前 Web 上下文和原生会话；切换先暂停本地桥、递增本地 generation，再由服务端验证并创建新绑定。旧绑定、连接及未发布传输失效，不重定向既有运行或复用旧授权。设备目录授权 SHALL 绑定账号、服务器与租户，切换后不得自动激活上一作用域的目录。

#### Scenario: 桌面切换不影响独立浏览器
- **WHEN** 桌面由租户 A 切换 B，独立浏览器标签仍选择 A
- **THEN** 桌面只更新自己的业务和设备绑定，独立标签选择不变；没有修改共享 AuthSession 当前租户

#### Scenario: A 到 B 到 A 的旧绑定响应
- **WHEN** 初次 A 绑定解析响应在再次进入 A 后迟到
- **THEN** 客户端按 generation 拒绝该响应，不能复活旧授权、旧请求或旧文件读取
