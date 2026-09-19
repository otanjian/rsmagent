# 验证证据：scope-inbound-attachment-to-bound-agent

日期：2026-09-17。范围：仅 `wecom_bot`（见 `proposal.md` 的交付边界）。

## 1. 缺陷现场（修复前，真实运行进程）

企业微信机器人绑定租户智能体 `tax-health-check-test15`（租户 `tnt_EA3qM-lHPLD8ZPwW`），用户上传的 9 份客户资料实际落点为：

```
2026-09-17 10:03  /Users/jiantan/cow/agents/my-assistant-admin/tmp/wecom_76885883307d1c09b2853c6c4fbb249d.docx
```

`my-assistant-admin` 是**进程全局默认智能体**，与绑定 Agent 无关。上一批（`2026-09-14 18:25`，9 个文件）落在同一错误目录。

同一时刻两种解析结果（同一函数，仅身份不同）：

```
tmp_dir() 无身份            → /Users/jiantan/cow/agents/my-assistant-admin/tmp
tmp_dir(tax-health-check)  → .../tenants/test15/agents/tax-health-check-test15/tmp
```

租户智能体受执行隔离约束，读不到 `~/cow/...`，故机器人回报「本批 9 个文件未投递到本工作区，且我无权访问 /Users/jiantan/cow/agents/...（跨租户隔离）」。

## 2. 失败测试（RED，修复前）

命令：`.venv/bin/python -m pytest tests/test_wecom_inbound_attachment_scope.py -v`

结果：`4 failed, 1 passed`。失败的正是落点断言；通过的是反向控制（传统单实例渠道保持既有语义），符合「反向控制须在修复前后都通过」的要求。

修复前日志直接复现缺陷（渠道绑定 `agent-tax-health`，文件却落到 `agent-default`）：

```
[WecomBot] File downloaded: .../test_the_bound_agent_can_read_0/agent-default/tmp/wecom_msg_read.docx
```

## 3. 修复后（GREEN）

命令：`.venv/bin/python -m pytest tests/test_wecom_inbound_attachment_scope.py tests/test_wecom_bot_inbound_identity.py -v`

结果：`11 passed`（新增 6 项 + 既有企微入站身份 5 项）。

覆盖：文件落点、图片落点、两条接收路径一致、绑定 Agent 可读回、传统单实例保持既有语义、归属不可解析时失败关闭。

## 4. 回归

相关套件（`test_wecom_bot_inbound_identity` / `test_wecom_inbound_attachment_scope` / `test_tenant_channel_inbound_anchor` / `test_tenant_channel_inbound_closure` / `test_state_dir_tenant_containment` / `test_personal_channel_inbound` / `test_agent_routing`）：`81 passed`。

全量套件：`.venv/bin/python -m pytest tests/ -q -p no:randomly` → `29 failed, 4622 passed, 6 skipped`。

**29 项失败为既有失败，与本变更无关**，隔离验证如下：

```
$ git stash push -- channel/wecom_bot/wecom_bot_channel.py
$ pytest <29 项失败所在文件> -q -p no:randomly
29 failed, 39 passed          # 撤掉本变更后同样 29 项失败
$ git stash pop
$ pytest <同一组文件> -q -p no:randomly
29 failed, 39 passed          # 恢复后失败集合逐项一致
```

失败集中在 `test_weixin_qr_flow.py`（22 项，`channel type is not available for tenant configuration`）、`test_channel_startup_open.py`（3）、`test_compat_surface_closure.py`（2）、`test_personal_console_frontend.py`（1）、`test_plan_3_1_joint_acceptance.py`（1），均与本变更路径无关。

## 5. 实际运行进程

带 `COW_CREDENTIAL_MASTER_KEY` 重启后（PID 21244）：

```
[WecomBot] ✅ Subscribe success
[FeiShu] ✅ Websocket thread started, ready to receive messages
[WebChannel] ✅ Web console is running
```

`curl http://127.0.0.1:9899/chat` → `HTTP 200`。

## 6. 未覆盖（不得以本节以外的表述宣称已验证）

- **真实企业微信入站往返未实测**：需用户在企微客户端向「客户税务健康体检 BOT」发送一份文件，确认落点变为 `~/.cow/tenant-roots/tenants/test15/agents/tax-health-check-test15/tmp/`。本轮只证到「单元层落点正确 + 进程带修复运行」，未证「真实厂商消息落点」。
- **其余 6 个渠道未修复**：`feishu` / `weixin` / `qq` / `slack` / `telegram` / `discord` 同样在解析阶段调用裸的 `state_dir.tmp_dir()`，缺陷仍在。
- **飞书渠道同缺陷的现场证据**：`business-analysis-test15` 的飞书实例本次未做入站附件实测。
- **历史错位文件未搬运**：`~/cow/agents/my-assistant-admin/tmp/` 下 18 个 `wecom_*.docx` 仍原样保留；本次已手工把新批 9 个投递到该租户 `projects/_intake_incoming/` 供继续作业，但这是运维动作，不是代码行为。
