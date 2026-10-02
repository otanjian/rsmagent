# 本轮冲突及基线漂移

独立克隆排练后中止，再从固定目标正式执行 merge --no-ff --no-commit。实际 50 个冲突文件，历史基线 46 项；新增 32 项，消失 28 项。本文描述最终决定，不把脚本的初次文本选择当作行为验收。

| 路径 | 漂移 | 最终决定 |
| --- | --- | --- |
| `.gitignore` | 既有 | 合并上游忽略规则和 rdai 本地目录规则，清理残留标记。 |
| `README.md` | 既有 | 保留 rdai 删除：上游 README 描述另一产品安装形态。 |
| `agent/memory/conversation_store.py` | 既有 | 采用 master 增量持久化、run_id、游标和用户消息索引，所有查询/清空继续按 tenant/owner/agent 维度；修复新 context_start_seq 读面。 |
| `agent/memory/manager.py` | 新增 | 采用 master rerank/reconcile/索引失效行为，保留个人与租户共享索引根及显式相对路径。 |
| `agent/memory/storage.py` | 新增 | 保留 rdai 扩展 schema，不能丢失 owner/tenant 或 tombstone；上游非冲突增量继承。 |
| `agent/memory/summarizer.py` | 新增 | 合并 master 去重认领/重试与原子写入，保留 rdai scope generation，失败释放认领。 |
| `agent/prompt/workspace.py` | 新增 | 保留 rdai 实际工作区根及资源授权路径，上游其余变更保留。 |
| `agent/protocol/agent.py` | 新增 | run_stream 同时接受 rdai attachments 和 master on_executor；后处理合并。 |
| `agent/protocol/agent_stream.py` | 既有 | 采用 master StepWriter、流式与 token budget 逻辑，保留外部工具授权及附件。 |
| `agent/registry.py` | 既有 | 采用 master builtin description，同时保留可配置 ID 和容大品牌。 |
| `agent/skills/service.py` | 新增 | 保留技能资源投影，合并 master 下载/删除元数据；删除按已加载条目的确切路径，不删除内建技能。 |
| `agent/tools/__init__.py` | 新增 | 合并双方工具导出，保留新增 current_time。 |
| `agent/tools/mcp/mcp_client.py` | 新增 | 采用 master 可执行文件解析、SSE 发现期限及 RPC 空结果修复；保留 rdai strict errors、外部执行上下文及环境变量白名单。 |
| `agent/tools/memory/memory_get.py` | 新增 | 保留个人资源路径和授权，接入 master shared knowledge 读取。 |
| `agent/tools/scheduler/scheduler_tool.py` | 新增 | 采用 master 时区/任务形状，保留 rdai scope 校验。 |
| `agent/tools/scheduler/task_store.py` | 既有 | 合并上游字段和排序，保留 rdai CAS revision、lease 与任务 owner。 |
| `agent/tools/tool_manager.py` | 新增 | 使用 master MCP disabled 判定；保留 database 外部工具连接/执行鉴权。 |
| `app.py` | 既有 | 上游迁移、启动和 PID 改进与 rdai 强制接缝/身份启动检查合并；迁移顺序保持。 |
| `bridge/agent_bridge.py` | 新增 | 继承 master 流式/团队能力，同时保留 verified identity、附件及桌面执行边界。 |
| `bridge/agent_initializer.py` | 新增 | 保留 owner/tenant 初始化和个人 memory；采用上游工具及提示词更新。 |
| `channel/channel.py` | 新增 | tenant_id 与 master peers 并存；tenant stamp 仅用于观测，不授予身份。 |
| `channel/channel_factory.py` | 新增 | 构造参数并集：实例/凭据/agent/members/tenant_id/peers 均向下传递。 |
| `channel/channel_instances.py` | 既有 | 保留 database 实例与隔离凭据，合并 master peers 及渠道元数据，禁止回到扁平全局凭据。 |
| `channel/dingtalk/dingtalk_channel.py` | 新增 | master 流式卡片与 rdai 租户/实例上下文并存。 |
| `channel/web/README.md` | 既有 | 保留删除：上游单入口文档不对应 rdai 实际装配。 |
| `channel/web/chat.html` | 既有 | 保留 rdai shell、权限片段和现有 console 装载；实际挂载 timeline、技能弹窗、master i18n 和样式增量。 |
| `channel/web/web_channel.py` | 既有 | 保留 rdai 路由/handler 装配；新增技能上传及用户消息历史入口绑定实际 fork handler。 |
| `channel/wecom_bot/wecom_bot_channel.py` | 新增 | master 媒体改进与 rdai 实例身份戳合并。 |
| `channel/weixin/weixin_channel.py` | 新增 | 使用 master 每实例 credentials_path，保留 rdai 私有/租户实例身份。 |
| `cli/commands/backup.py` | 新增 | 合并双方备份条目及上游修复，保留 rdai 数据库/产品路径。 |
| `cli/commands/skill.py` | 新增 | 采用 master preview/stage/commit；显式 skills_dir 参数使 database adapter 写入获权目录。 |
| `config.py` | 既有 | 保留 database-only 认证，禁止恢复 web_password/external_api_token；上游其它默认值及原子保存保留。 |
| `desktop/package.json` | 既有 | 版本采用 master 2.2.0，产品描述和名称保留 rdai。 |
| `desktop/src/main/app-icon.ts` | 新增 | master 标题更新改进受 rdai runtimeIconTitleEnabled 控制，品牌资源保留。 |
| `desktop/src/main/index.ts` | 新增 | 合并 master 启动/更新逻辑，保留原生 database 登录和本地/远程 Web 容器。 |
| `desktop/src/main/menu.ts` | 新增 | 采用上游菜单行为，默认文档地址及品牌保持 rdai。 |
| `desktop/src/main/python-manager.ts` | 新增 | 采用 master 后台启动改进；保留 rdai 数据目录/认证，未复活桌面 token。 |
| `desktop/src/renderer/src/components/ContextUsagePopover.tsx` | 新增 | 保留认证/上下文切换清空规则；仅同一上下文的普通请求失败可继续展示上游缓存图表。 |
| `desktop/src/renderer/src/i18n.ts` | 新增 | 新增文案继承 master，已定制的容大品牌文案保留。 |
| `desktop/src/renderer/src/pages/SkillsPage.tsx` | 新增 | 采用 master 技能上传预览安装，动作取服务端 can_install/editable/deletable；MCP 进入现有受控系统接入页。 |
| `desktop/src/renderer/src/types.ts` | 既有 | 保留双方业务类型，删除残余冲突分隔符；补服务端技能动作类型。 |
| `docs/ja/README.md` | 既有 | 保留 rdai 整文件删除，上游日文 README 不作为本产品交付文档。 |
| `docs/zh/README-Hant.md` | 既有 | 保留 rdai 整文件删除，上游繁体 README 不作为本产品交付文档。 |
| `docs/zh/README.md` | 既有 | 保留 rdai 整文件删除，上游中文 README 不作为本产品交付文档。 |
| `plugins/cow_cli/cow_cli.py` | 新增 | 命令行为及上游修复优先 master，默认产品名保留容大AI。 |
| `tests/test_channel_instances.py` | 新增 | 合并新 peers/实例测试，断言仍覆盖 tenant_id 和真实实例隔离。 |
| `tests/test_feishu_progress_card.py` | 新增 | 保留 rdai 外部身份相关测试桩，继承非冲突上游卡片回归。 |
| `tests/test_knowledge_web.py` | 既有 | 采用新增上传断言，同时保持 patch/import 命中实际 fork handler。 |
| `tests/test_security_ssrf_browser_navigate.py` | 新增 | 采用 master SSRF 回归更新。 |
| `tests/test_web_console_update.py` | 新增 | 合并上游更新检查，认证断言命中 database；上游 split-only 菜单测试如实记录跳过。 |

## 消失的基线项

| 路径 | 本轮解释 |
| --- | --- |
| `agent/admin.py` | 本轮无文本冲突；上游增量已自动合并，并按能力表核对实际调用、授权及受影响测试。 |
| `agent/tools/scheduler/integration.py` | 本轮无文本冲突；上游增量已自动合并，并按能力表核对实际调用、授权及受影响测试。 |
| `channel/web/static/css/console.css` | master 早已拆分样式；rdai 仍加载 monolith，新增 master-sync.css 只承接本轮新增组件样式并在实际 shell 装载。 |
| `channel/web/static/js/console.js` | master 在共同祖先时已拆分此文件，故本轮不会再产生删除冲突；已将 split 模块的交互增量移入实际加载的 rdai console，并补依赖/重连用例。 |
| `channel/web/static/vendor/README.md` | 目标仍不存在该文件，未复活；权限选择器继续由 RBAC 替代，旧公证脚本/上游说明保持退役。 |
| `desktop/build/notarize-dmg.sh` | 目标仍不存在该文件，未复活；权限选择器继续由 RBAC 替代，旧公证脚本/上游说明保持退役。 |
| `desktop/src/main/preload.ts` | 本轮无文本冲突；上游增量已自动合并，并按能力表核对实际调用、授权及受影响测试。 |
| `desktop/src/renderer/src/api/client.ts` | 本轮无文本冲突；上游增量已自动合并，并按能力表核对实际调用、授权及受影响测试。 |
| `desktop/src/renderer/src/components/PermissionSelector.tsx` | 目标仍不存在该文件，未复活；权限选择器继续由 RBAC 替代，旧公证脚本/上游说明保持退役。 |
| `docs/intro/architecture.mdx` | base→source 无本轮增量，历史冲突处置仍由 rdai 目标继承；不是丢弃本轮上游修改。 |
| `docs/intro/features.mdx` | base→source 无本轮增量，历史冲突处置仍由 rdai 目标继承；不是丢弃本轮上游修改。 |
| `docs/intro/index.mdx` | base→source 无本轮增量，历史冲突处置仍由 rdai 目标继承；不是丢弃本轮上游修改。 |
| `docs/ja/intro/architecture.mdx` | base→source 无本轮增量，历史冲突处置仍由 rdai 目标继承；不是丢弃本轮上游修改。 |
| `docs/ja/intro/features.mdx` | base→source 无本轮增量，历史冲突处置仍由 rdai 目标继承；不是丢弃本轮上游修改。 |
| `docs/ja/intro/index.mdx` | base→source 无本轮增量，历史冲突处置仍由 rdai 目标继承；不是丢弃本轮上游修改。 |
| `docs/zh/intro/architecture.mdx` | base→source 无本轮增量，历史冲突处置仍由 rdai 目标继承；不是丢弃本轮上游修改。 |
| `docs/zh/intro/index.mdx` | base→source 无本轮增量，历史冲突处置仍由 rdai 目标继承；不是丢弃本轮上游修改。 |
| `tests/test_agent_web_management.py` | base→source 无本轮增量，历史冲突处置仍由 rdai 目标继承；不是丢弃本轮上游修改。 |
| `tests/test_claude_thinking.py` | base→source 无本轮增量，历史冲突处置仍由 rdai 目标继承；不是丢弃本轮上游修改。 |
| `tests/test_config_subagent_toggle.py` | base→source 无本轮增量，历史冲突处置仍由 rdai 目标继承；不是丢弃本轮上游修改。 |
| `tests/test_dashscope_provider.py` | base→source 无本轮增量，历史冲突处置仍由 rdai 目标继承；不是丢弃本轮上游修改。 |
| `tests/test_doc_edit.py` | base→source 无本轮增量，历史冲突处置仍由 rdai 目标继承；不是丢弃本轮上游修改。 |
| `tests/test_models_handler.py` | base→source 无本轮增量，历史冲突处置仍由 rdai 目标继承；不是丢弃本轮上游修改。 |
| `tests/test_openai_chat_api.py` | base→source 无本轮增量，历史冲突处置仍由 rdai 目标继承；不是丢弃本轮上游修改。 |
| `tests/test_qianfan_provider.py` | base→source 无本轮增量，历史冲突处置仍由 rdai 目标继承；不是丢弃本轮上游修改。 |
| `tests/test_scheduler_web_update.py` | base→source 无本轮增量，历史冲突处置仍由 rdai 目标继承；不是丢弃本轮上游修改。 |
| `tests/test_web_chat_content_type.py` | base→source 无本轮增量，历史冲突处置仍由 rdai 目标继承；不是丢弃本轮上游修改。 |
| `tests/test_workspace_edit.py` | base→source 无本轮增量，历史冲突处置仍由 rdai 目标继承；不是丢弃本轮上游修改。 |

## 再确认的整文件删除

README.md、docs/zh/README.md、docs/zh/README-Hant.md、docs/ja/README.md、desktop/src/renderer/src/components/PermissionSelector.tsx 五项均保持删除。channel/web/README.md、channel/web/static/vendor/README.md 也保持删除。没有删除 master 分支或其历史。
