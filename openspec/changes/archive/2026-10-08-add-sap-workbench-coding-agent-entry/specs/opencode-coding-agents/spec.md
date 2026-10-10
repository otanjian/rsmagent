## ADDED Requirements

### Requirement: 标准服务的就绪探测与存活保障

系统 SHALL 在打开工作台前确认所引用的共享 OpenCode 服务已就绪，未就绪时 SHALL 以可辨识原因即刻报告，MUST NOT 让用户等待一个不会成功的长时间请求。冷启动成本 SHALL 由开机与存活守护承担，MUST NOT 由用户点击触发。部署 SHALL 守护服务的 API 与 Web 嵌入端口（可选包含 MCP 网关），使其在异常退出后自动恢复；MCP 网关不可用 MUST NOT 阻断 API 与 Web 嵌入服务的启动。

#### Scenario: 服务未就绪时打开

- **WHEN** 用户打开工作台而共享服务尚未就绪
- **THEN** 立即返回可辨识的未就绪原因与可重试状态，不发起长达百秒的等待，不启动替代引擎

#### Scenario: 服务异常退出后自动恢复

- **WHEN** 共享服务进程异常退出
- **THEN** 守护在短期内将其重新拉起，相关入口随其恢复可用，不要求人工介入

#### Scenario: MCP 网关启动失败

- **WHEN** MCP 网关启动缓慢或失败
- **THEN** 服务的 API 与 Web 嵌入能力仍被启动并提供服务，MCP 不可用被单独报告
