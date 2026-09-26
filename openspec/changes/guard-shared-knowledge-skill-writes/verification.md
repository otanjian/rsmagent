# 验证记录：guard-shared-knowledge-skill-writes

本文件记录本次实现的验证证据与未完成项，供任务 2.1 / 3.1 / 3.2 核对。
本 change 只改提示词生成，不新增执行器拦截；下列断言均为“提示内容”断言，
不得据此宣称技术上无法修改共享文件。

## 改动范围

- 新增 `agent/prompt/shared_assets.py`：按当前可信身份、Agent 绑定、
  `common.state_dir` 实际数据根与既有管理资格解析本轮维护范围，生成维护说明；
  无法确认时回落为保守只读。
- `agent/prompt/builder.py`：每轮计算维护范围；知识库章节在只读时改用受约束措辞；
  维护说明段在所有章节之后追加，且不依赖知识开关或索引是否存在。
- `agent/prompt/workspace.py`：默认 RULE.md 模板的强制自动写入改为受维护资格约束；
  布局说明中“读写共享层”改为“读取，写入需维护资格”。不改写已有客户磁盘文件。
- `agent/protocol/agent.py`：提示重建失败的缓存回退路径追加保守指令。
- `auth/service.py`：新增 `is_tenant_admin` 公开访问器，复用既有 `_is_tenant_admin`，
  不新增授权逻辑。
- `tests/test_shared_asset_prompt_guidance.py`：新增 19 项断言。

## 自动化验证（任务 2.1）

`tests/test_shared_asset_prompt_guidance.py` 19 passed，覆盖：

- 普通成员使用共享 Agent → 知识/技能均只读；
- 本人私有 Agent 的独立数据根 → 可维护，并解析出本人输出落点；
- 私有 Agent 知识/技能回落到公共目录 → 仍按公共来源判为只读；
- 平台/租户管理资格 → 共享内容可维护；
- 身份服务不可用 / 无身份 → 保守只读；
- 每轮独立：管理员一轮的可写结论不串到下一轮普通成员；两个用户经
  `use_identity` 在两个线程中同时解析，各自拿到自己的结论（并发不串用）；
- 只读文案包含禁止 write/edit/bash/脚本绕开、拒绝“自称管理员”、个人输出落点、
  旧模板/skill 文案受本轮约束；
- 无个人输出目录时只返回文字建议；
- 索引缺失时约束段仍存在且知识章节明显改为只读措辞；
- 默认可写时保留原强制自动写入措辞；
- 默认 RULE 模板中英文均为受约束措辞；
- 缓存回退路径追加保守说明。

**已接入模型请求（phase 2 准入条件）**：两项集成断言走真实的
`Agent.get_full_system_prompt` → `AgentStreamExecutor._call_llm_stream` 路径，
用捕获请求的模型替身替换传输层，断言维护说明出现在实际构造的 `LLMRequest.system`
上；其中一项覆盖缓存回退路径。

**断言强度（变异检查）**：把 `build_shared_asset_guidance` 改为返回空后重跑，
7 项断言失败（含集成断言），确认断言确实依赖该段落，而不是被别处的交叉引用蒙混通过。

命令：

```
.venv/bin/python -m pytest tests/test_shared_asset_prompt_guidance.py -q
# 19 passed
```

## 回归

```
.venv/bin/python -m pytest tests/ -q
# 6603 passed, 41 failed, 30 skipped, 622 subtests passed
```

41 项失败全部为既有/环境问题，与本次改动无关：将同一批失败用例在剔除本次
4 个受跟踪文件改动后重跑，得到完全相同的 `35 failed, 6 passed`（同一文件集合、
同一数量）。失败集中在真实租户渠道实例数据污染（`test_channel_startup_open`）、
能力开关台账漂移（`test_compat_surface_closure`）、微信扫码/外部连接等环境依赖
用例，均未触及本 change 修改的提示代码路径。

## 范围核对（任务 3.1）

- 仅修改提示生成与模板文案 + 一个只读访问器；不涉及工具分发、Shell 解析、
  文件写入、执行隔离检查、权限码或管理 API。
- 未涉及 Desktop、OpenCode、MCP 独立后台任务；无数据迁移、新表、feature flag。
- 未写入共享 Agent 公共缓存或磁盘 AGENT.md/RULE.md；已有客户 RULE.md 不批量回写。

## 未执行项

- **任务 2.2 受控对话观察未执行**：需要运行中的部署、真实模型与临时共享资料；
  本环境未执行，未对任何线上资产测试，也没有用「少量样本成功」替代安全结论。
  该项保留未勾选，待有可用环境时补齐。已就位的部分：最终提示到模型请求的接入
  已有集成断言（见上），2.2 只欠真实模型的行为观察。
- 提示词局限：模型可能忽略提示或受用户/技能文案干扰；本次不提供强制保护，
  实际可写性仍由既有执行层与知识/技能管理入口决定。

## 回滚

删除 `agent/prompt/shared_assets.py` 与 `tests/test_shared_asset_prompt_guidance.py`，
并还原 `agent/prompt/builder.py`、`agent/prompt/workspace.py`、
`agent/protocol/agent.py`、`auth/service.py` 即可恢复原提示生成。

## 产物校验

```
openspec validate guard-shared-knowledge-skill-writes --strict
# Change 'guard-shared-knowledge-skill-writes' is valid
```
