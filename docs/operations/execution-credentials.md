# 执行凭据映射

清单仅含变量名和用途，不含真实秘密。完整本地技能登记见 `openspec/changes/harden-production-readiness/evidence/capability-inventory.json`（74 个启用 Agent、45 条仓库/工作区技能条目；重复名称代表不同来源）。它是基线盘点，不代表45条技能都已完成真实外部系统测试。

## 映射规则

新增配置 `execution_credentials` 消费**现有** IdentityService 凭据存储；不增加角色、grant 或第二套凭据库。每条明确 `tenant_id`、`agent_id`，建议填写 `user_id`；`resource_kind=tool`、`resource_id=builtin:bash` 与现有 Bash 授权配套。`credential` 为凭据名；`field` 可从JSON业务凭据中取一个字符串字段。每次进程启动重新解析，轮换或撤销下一次立即生效。

普通成员使用已有个人资源配置 `save_personal_resource_config`，凭据引用为 `personal:<user_id>:tool:builtin:bash`，没有凭据管理资格的成员不需要新增该权限。租户管理员/平台管理员可沿现有 `create_credential`、`rotate_credential`、`revoke_credential` 管理途径使用具名用途凭据。不要更改内置角色以让测试通过。

非秘密业务参数使用 `execution_environment` 的同样主体选择条件，`values` 仅存字符串，例如原有 `SKILL_IMAGE_GENERATION_MODEL`、provider、API endpoint。不要把密钥放这里；PATH、HOME、临时目录、解释器注入变量等由启动器管理，不能用该配置覆盖。

```json
{
  "execution_environment": [{
    "tenant_id": "<tenant-id>", "agent_id": "<agent-id>", "user_id": "<user-id>",
    "values": {"SKILL_IMAGE_GENERATION_PROVIDER": "qwen", "SKILL_IMAGE_GENERATION_MODEL": "<existing-model>", "DASHSCOPE_API_BASE": "<existing-endpoint>"}
  }],
  "execution_credentials": [{
    "tenant_id": "<tenant-id>", "agent_id": "<agent-id>", "user_id": "<user-id>",
    "env": "DASHSCOPE_API_KEY", "credential": "personal:<user-id>:tool:builtin:bash", "field": "dashscope_api_key",
    "resource_kind": "tool", "resource_id": "builtin:bash"
  }]
}
```

个人凭据可保存JSON对象以支持多业务变量；每个允许字段分别列映射。不要覆盖已有个人资源配置中的其他字段，应读取其现有用途并合并。此配置不自动共享主模型凭据；当前本机 `dashscope_api_key` 的实际租户/用户归属仍须在目标部署迁移时落实，不能将本地配置里的真实密钥复制进 change 或测试。

## 现有技能用途表

| 技能/设置 | 保留的变量名（例） | 归属及迁移方式 |
| --- | --- | --- |
| image-generation | OPENAI/GEMINI/ARK/DASHSCOPE/MINIMAX/LINKAI_API_KEY；SKILL_IMAGE_GENERATION_MODEL/PROVIDER | 使用者自己的 Bash 用途凭据；模型/端点放同主体业务设置。自定义提供商JSON走 `SKILL_IMAGE_GENERATION_CUSTOM_PROVIDER` 凭据映射。现有真实脚本已以三种角色、合成接收端验证产物 |
| Weaver E10 Api、oa-audit-manager | WEAVER_APP_KEY/SECRET/CORPID、OA_APP_KEY/SECRET、OA_USERNAME/PASSWORD 等 | 按实际OA账号所属租户/用户建用途映射；只投递到对应Agent，管理/审批权限仍由原系统决定 |
| hikvision-record | HIK_OPEN_CLIENT_APP_KEY/SECRET、HIK_HOST、HIK_FFMPEG | 录像账号凭据归本人/租户；地址与工具参数为业务设置；摄像头与实时媒体需目标环境 |
| imap-smtp-email | IMAP_HOST/PORT、SMTP_HOST/PORT、PASSWORD 等 | 每个邮箱所属用户；账号配置放本人允许目录；不共享全局邮件密码。当前未提供可用于演练的真实邮箱 |
| tencent-meeting、会议类技能 | TENCENT_MEETING_TOKEN 及已有会议配置 | 原会议账号的本人/租户用途；外部会议发送不在本地测试中触发 |
| data-analyst | DB_CONNECTION、DB_TYPE、DATA_DIR | 连接字符串走凭据，类型/输入目录走业务参数；仅允许已登记数据根 |
| ppt-hmt | OPENAI_API_KEY、GEMINI_API_KEY、各图片/TTS提供商参数等 | 依原提供商用途逐项映射，同样支持JSON字段，不将所有厂商密钥广播 |
| PDF、Excel、财务审计、采购分析及其他本地文档技能 | 输入文件、输出目录及既有业务参数 | 不需要秘密时不配置凭据；运行库只读、授权业务根可写，文档回退执行已验证 |
| PATH/语言/CA/代理、OPENAI_API_BASE | 原必要系统配置 | 保留必要运行环境，运行时缓存转到任务私有目录 |
| COW_CREDENTIAL_MASTER_KEY、会话令牌、宿主管理凭据 | 不作为业务变量 | 控制面保管，不进入任务环境；整实例备份主密钥另行托管 |

技能局部 `.env` 是否存在已登记为布尔值和变量名；局部文件必须位于当前主体获权目录，其归属不能由“父共享目录可读”代替。新启动器不会自动把未知全局变量传给每个任务。脚本自身原有局部配置读取仍遵守OS文件边界。

## 切换顺序

1. 在目标实例维护窗口，核对实际已授权的租户/用户/Agent及变量用途；导出现有配置和完整冷备份，记录标识，不记录值。
2. 通过现有凭据服务保存或更新用途凭据；保留原有角色与资源授权，配置显式映射。先用测试值和合成接收端核对当前使用者可用、另一用户/租户不可读、轮换和撤销生效。
3. 逐项执行原有合法技能，确认输入、联网、文件产物、后台输出/取消；真实外部服务使用专用测试账号/测试端点，避免业务副作用。需要真实OA/邮箱/会议/媒体环境的项目单独留待环境验收，不伪造成功。
4. 只有完整生产隔离探测和正向技能均通过才切换候选。保留旧配置的受控备份，在确认原模型/渠道仍需的配置后处理旧全局副本，不盲目删键；部署主密钥永不迁入技能用途凭据。
