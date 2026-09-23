# 图纸询价智能报价智能体

面向钣金、机柜和焊接结构件询价。使用普通智能体、独立工作区与专属 `rfq-quote` 技能，读取本次上传资料并交付成本核算表、三档报价草稿、待确认清单。无需 WorkBuddy 客户端或外部安装脚本。

## 安装到租户

```sh
.venv/bin/python -m pip install -r skills/rfq-quote/requirements.txt
.venv/bin/python scripts/install_rfq_agent.py --tenant-code test15 --username test15
```

安装器先验证账号属于目标租户且是有效管理员，再通过项目的 `AgentAdminService` 和 `IdentityService` 创建智能体、写入租户归属与审计，并给该租户管理员角色补齐本智能体与本技能的 read/use 资源授权，保留其他权限。ID 为 `rfq-quote-test15`，位于该租户 shared_root 下的 `agents/rfq-quote-test15`；保持租户现有默认智能体。重复安装跳过已存在的完整实例并检查授权，不覆盖运行时修改。若 ID/目录被其他对象占用则停止。部署后在“智能体工作台”刷新即可找到。

代码模板位于 `agent/presets/rfq_quote/`，技能及演示文件位于 `skills/rfq-quote/`。实例持有技能副本，后续修改仓库模板不会自动覆盖实例，需有意识地升级并复核差异。

## 使用

给智能体发送：

> 用内置演示数据跑一遍 P-CAB-02 询价，生成成本核算表、常规/加急/批量报价草稿和待确认清单，说明为什么暂时不能发出。

真实询价上传邮件、清晰图纸、材料价、工时费率和历史实际成本，再说明数量、交期及企业定价政策。缺项会明确标出；不会从演示资料补齐。当前计算脚本针对钣金，图纸理解由所配置模型与文件读取工具完成，不承诺 CAD 自动解析。

直接验证无需调用模型：

```sh
.venv/bin/python skills/rfq-quote/scripts/quote.py --demo --output /tmp/rfq-quote-demo
.venv/bin/python -m pytest tests/test_rfq_quote.py tests/test_rfq_agent_install.py -q
```

真实输入格式见技能的 `references/input-format.md`。每次输出目录必须未包含旧成果。计算使用 Decimal、逐行四舍五入至分，Excel 保留计算公式和结果缓存；修改原始输入后重新运行以更新风险、校核和三个工作簿。所有结果始终是内部草稿，脚本不具有发送或审批功能。

## 来源与适配

场景来源：[WorkBuddy 实践案例与客户指南](https://www.workbuddy.link/p/ZljbYQzFpALFLcPwKPlct1)。原始体验包版本 1.2.2，来源链接与 SHA-256 见技能的 `references/source.json`；四份演示输入原样保存。成本与风险参考原文保存在 `references/upstream-*.md`。

原页面与模板有不同的金额、校平口径及折扣。适配版不复用其合计：保留完整重量精度、逐行算金额、档位变动后重算管理费，并明确标记演示假设。台账实际只有五条记录，且缺少足以还原旧件成本的细节，因此演示不会伪造“历史校核通过”。
