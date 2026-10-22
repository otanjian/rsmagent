## MODIFIED Requirements

### Requirement: 有效平台身份默认动态全权限

系统 SHALL 从服务端验证的平台管理员资格派生 all，覆盖已登记菜单、技能、工具、模型、智能体及相应功能动作，并自动包括后续新增合法资源。平台管理员资格 SHALL 由平台作用域的内置角色绑定（平台级内置角色 `platform_admin` 与账号的绑定关系）作为唯一来源，由服务端在每次受保护请求重新解析；账号上的 `is_platform_admin` 标记为同一写事务内维护的派生镜像，MUST NOT 作为独立授权来源或绕过角色绑定的凭证。平台管理员 SHALL 无需逐资源 grant，唯一例外为目标 Agent 的 `history_read_all`：跨 owner 读取该租户共享普通 Agent 的全部会话 SHALL 要求平台管理员以目标租户的有效成员身份持有该 Agent 的显式 grant；平台 all、Agent 管理权与所有权 MUST NOT 代替该 grant，且该例外 MUST NOT 扩展到其他功能动作、资源动作或数据范围。普通角色保存接口 MUST NOT 接受客户端 all、通配符或平台身份标识。未知资源/动作 SHALL 被拒绝，不因 all 解释为可调用任意名称。

#### Scenario: 新增资源后平台管理员使用

- **WHEN** 新资源已经合法登记、启用且调用所需上下文和消费者满足条件
- **THEN** 平台管理员无需补勾选即可获得其相应授权，普通角色没有对应显式授权则不会自动获得；`history_read_all` 的例外见末条场景

#### Scenario: 普通角色伪造全权限

- **WHEN** 普通调用者修改前端 all 标志或在角色保存中提交通配符/平台资格
- **THEN** 服务端拒绝非法角色变更或按真实身份拒绝调用，不产生部分授权

#### Scenario: 平台资格由角色绑定派生

- **WHEN** 账号获得或失去 `platform_admin` 平台内置角色绑定
- **THEN** 后续请求按当前绑定关系重新派生 all，不读取前端状态、缓存摘要或与绑定不一致的镜像标记

#### Scenario: 跨用户历史读取仍需目标授权

- **WHEN** 平台管理员请求某共享普通 Agent 的全部用户会话，但其在目标租户的角色未授予该 Agent 的 `history_read_all`，或并未加入该租户
- **THEN** 请求被拒绝或回到本人范围，平台 all 与 Agent 管理权不替代该 grant；其他功能的 all 语义不变
