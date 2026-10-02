# 跨部署 Agent 的 database 身份接缝

本接缝复用 master 的 `PeerTransport`、`InvokeRequest`、`serve_invoke` 和三种业务模式（delegate、speak、clear）。`auth/peer_identity.py` 只负责跨部署身份与消息完整性；成员、租户、资源授权和个人数据边界继续由现有 IAM 决定。database 模式可以执行配置正确且获权的请求，不存在按身份模式整体关闭的分支。

## 部署和绑定

每个部署使用独立的 Ed25519 私钥种子（32 字节、64 位十六进制），通过环境变量注入。代码使用已有 PyCryptodome 依赖。私钥不写入 `config.json`，不传给中继，不复用模型 API Key 或身份库加密密钥。正常云连接仍按既有 `use_linkai`、`cloud_deployment_id` 配置启动，且需要已有 LinkAI SDK。

以下示例是部署 A 的 `peer_identity` 配置；部署 B 反向配置自己的密钥、公钥信任和租户映射。占位值须替换为实际值，不会自动产生信任关系。

```json
{
  "peer_identity": {
    "deployment_id": "deployment-a",
    "signing_key_env": "RSM_PEER_SIGNING_KEY",
    "trusted_deployments": {
      "deployment-b": {
        "public_key": "B的32字节Ed25519公钥十六进制",
        "tenants": {"B的租户ID": "A的租户ID"}
      }
    },
    "peers": {
      "remote-agent-b": {
        "deployment_id": "deployment-b",
        "tenants": {"A的租户ID": "B的租户ID"},
        "source_agents": ["local-agent-a"]
      }
    }
  }
}
```

`trusted_deployments.*.tenants` 是入站租户映射；`peers.*.tenants` 是出站路由。`source_agents` 可进一步限制允许发起转交的本地 Agent。peer 名称须与 master 团队名录中的目标 ID 一致，并能在目标部署解析。名录只描述成员，不授予资源访问权。

接收部署的管理员通过现有外部身份绑定接口，把远端身份绑定到本地真实成员：

- 平台管理员：`POST /api/platform/users/{local_user_id}/external-identities`。
- 租户管理员：`POST /api/tenant/members/{local_member_id}/external-identities`。
- 请求体的 `provider` 为 `peer`，`issuer` 为远端 `deployment_id`，`subject` 为紧凑 JSON 字符串 `["远端租户ID","远端用户ID"]`，与 `auth.peer_identity.binding_subject()` 一致。

此绑定不提升本地成员权限。成员必须有效、已完成首次密码修改，持有 `chat.use`、目标 Agent 的 `agent.use`，并满足 Agent 的租户与私有 owner 边界。模型和工具仍走现有执行授权。删除绑定、停用成员、撤销资源授权后，新请求不能执行；出站会话失效或权限撤销后也不能签发新请求或接收结果。

## 线协议

请求增加 `peer_identity: {claims, signature}`。签名绑定部署、受众、源/目标租户、源成员、请求 ID、随机 nonce、时间窗口，以及任务、会话、源/目标 Agent、模式、团队、历史和超时的摘要。源身份来自已认证的 `RuntimeIdentity`，不会从工具参数的 `user_id` 或 `tenant_id` 读取。

中继必须原样转发请求字段和 `peer_identity`，并在每个 `agent_invoke_event`、`agent_invoke_result` 返回中保留接收端生成的 `peer_identity`。中继可以沿用既有 action/data/payload 包装；不能改写参与摘要的字段。接收端忽略中继追加的身份和别名声明。未携带签名或旧中继丢弃签名时会得到明确拒绝或超时，不会回退为匿名执行。

响应签名绑定原请求的 nonce、摘要、请求 ID、受众、响应类型、内容摘要和递增序号。伪造、乱序/重复及过期帧不会交给调用者。请求接受窗口最多 60 秒，业务时限沿用 master 的最多 600 秒。短期请求凭据不能替代远端即时撤销广播；目标端每次重新核对本地绑定、成员和授权，源端在签发和接收响应时复核本地会话/授权。

身份库迁移 45 新增 `peer_identity_nonces`。在执行前以唯一约束和事务占用 nonce，进程重启或并发重放不会再次执行；到期行在新请求进入时清理。表中不保存任务正文、密钥、签名或 Cookie。远端会话按源部署、源租户、源成员和源会话做命名空间隔离。代答和清空共同使用 `session_team_*`，避免原有规范化差异使清空落到另一条空会话。

## 验证和运维边界

`tests/test_peer_database_identity.py` 覆盖两套独立身份库及密钥、真实 CloudPeerTransport 往返、三种模式、签名篡改、目标 owner、当前成员/绑定/映射撤销、登录失效、持久及并发重放、返回帧校验、实际 ConversationStore 的清空范围。模型在这些自动化中是测试替身。

本轮另以两进程运行真实 DeepSeek 模型，delegate、speak、clear 均成功，代答会话的 `context_start_seq` 从 0 到 2，转交记录保留正确的接收方 tenant/owner。该演练使用文件中继；它验证候选应用协议与真实模型，不代表实际托管云中继已部署相应透传行为。真实远程环境仍须验证中继透传、连接断开、配套版本与网络条件。

迁移为追加表，不重写既有成员/授权数据。恢复旧程序前备份身份库；旧程序会忽略该表。回滚后再次启用本功能应保留未过期 nonce，避免把已执行请求重新接受。不要把测试私钥、身份库或带业务内容的原始模型日志纳入版本库。
