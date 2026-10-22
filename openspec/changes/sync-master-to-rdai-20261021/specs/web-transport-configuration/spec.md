## Purpose

允许部署者明确选择 Web 服务的直接 HTTPS 传输，并在证书配置错误时得到可靠的启动失败。传输配置应与 database 认证、既有反向代理及 Desktop 本机/远程连接规则共存，不通过隐式降级或信任未知代理扩大访问。

## ADDED Requirements

本轮必须验收真实 Web HTTPS/代理及后端来源边界；Desktop 本机/远程连接场景按 tasks 12.2 后补，不能凭后端测试声称桌面连接已通过。

### Requirement: 直接 HTTPS 按显式配置启动并拒绝错误降级

Web 服务 SHALL 支持显式启用直接 HTTPS 并使用部署者配置的可读证书与私钥；缺失、不可读或无法加载时 SHALL 启动失败并提供不含密钥正文的诊断，MUST NOT 静默回落明文 HTTP。未启用时 SHALL 保持当前部署传输方式，启用 HTTPS 不替代 database 身份、请求来源及对象授权。

#### Scenario: 使用有效证书启动
- **WHEN** 部署者启用直接 HTTPS 且提供有效证书与私钥
- **THEN** 真实 Web 入口通过 TLS 服务，登录及获准业务请求可完成，匿名和越权请求仍被拒绝

#### Scenario: 证书缺失或无效
- **WHEN** 直接 HTTPS 已启用但证书路径不可读或密钥不能加载
- **THEN** 服务明确启动失败，不在同一地址悄悄开放 HTTP，也不在日志输出私钥正文

### Requirement: 新传输选项保持既有代理与客户端连接边界

既有反向代理部署 SHALL 继续按原信任配置工作，客户端提供的转发头 MUST NOT 自动授予代理信任、改变租户身份或绕过来源检查。Desktop SHALL 保持已支持的 loopback HTTP 本机连接和受信 HTTPS 远程连接，MUST NOT 因本机兼容而接受任意远程 HTTP 或忽略证书验证。不同传输形态 SHALL 由实际连接及登录验证后声明支持。

#### Scenario: 本机连接和远程连接并存
- **WHEN** Desktop 分别连接受支持的 loopback HTTP 后端和有效 HTTPS 远程后端
- **THEN** 两种连接均可按原身份流程使用；远程证书错误仍被拒绝，不受本机例外影响

#### Scenario: 非受信客户端伪造转发头
- **WHEN** 请求直接到达服务并伪造代理来源或协议头
- **THEN** 不能借此改变授权身份或绕过既有来源、Cookie 与传输限制
