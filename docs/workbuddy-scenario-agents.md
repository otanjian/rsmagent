# 62 个场景智能体接入说明

本批覆盖原网站除“图纸询价智能报价”之外的全部 62 个一键体验场景：制造业 20、人力资源 15、财务 12、销售 15。已配置的图纸报价单独保留。

## 使用

登录 test15，切换到“AI租户1”，打开左侧“智能体”，选择场景后发送：

> 用内置演示数据跑一遍

也可以上传自己的资料并描述任务。每个智能体都有独立技能、业务规则、输出模板、十种任务示例和原包演示文件。原网站公开演示数据共 292 份，按场景隔离；真实业务不会自动混入演示数据。

## 项目结构

- `agent/presets/workbuddy/catalog.json`：62 个场景及智能体配置。
- `agent/presets/workbuddy/skills/wb-*/`：场景技能入口、原始 workflow、规则模板、任务示例、演示资料和来源校验。
- `agent/presets/workbuddy/shared/scenario_io.py`：准备演示、带位置提取证据、生成 Excel/Word/PDF/文本、文件结构检查。安装时复制进每个技能，使安装包自包含。
- `scripts/install_workbuddy_agents.py`：通过项目 AgentAdminService 与 IdentityService 安装、绑定租户并授予目标租户管理员最小的智能体/技能 read/use 权限。
- `scripts/build_workbuddy_presets.py`：从已审核的本地公开场景包重建预设，不执行上游安装脚本或包内代码。
- `tests/test_workbuddy_agents.py`：全目录、全部原始资料、批量安装、租户隔离、回滚、幂等与交付工具检查。

## 安装到指定租户

在项目根目录使用项目 Python 环境，主机需已安装 uv。安装器会在目标租户的 runtimes/workbuddy 内准备独立 Python 及依赖，供业务智能体使用；不放宽隔离策略：

```sh
.venv/bin/python scripts/install_workbuddy_agents.py --tenant-code test15 --username test15 --dry-run
.venv/bin/python scripts/install_workbuddy_agents.py --tenant-code test15 --username test15
```

可用 `--only bank-reconciliation` 选择单个场景；`--report /path/install.json` 保存完成清单。重复执行跳过已完成的同版本安装，保留用户修改；其他智能体占用的 ID/目录或不同版本会停止，不覆盖。失败时只回滚当次新建的智能体，之前成功的场景可以续装。安装不会修改默认智能体、模型、其他租户或原图纸报价。

通过 CLI 改变名册后，在线服务的对话路由可能仍持有旧对象。应在无执行中任务时用项目 CLI 重启，或通过现有管理 API 的正常名册编辑路径热加载；网页卡片出现并不单独证明对话已可用。

```sh
.venv/bin/python -m cli.cli restart --no-logs
.venv/bin/python -m pytest -q tests/test_workbuddy_agents.py tests/test_rfq_quote.py tests/test_rfq_agent_install.py
```

## 能力与验收口径

每个场景由模型遵循其专用规则读取原始资料、编写本次计算与分析脚本、生成所需文件；公共 IO 工具不替代业务分析，也不是 62 个固定答案的计算器。`prepare` 只准备输入，`check` 只证明文件结构可读，业务结论还需要来源核对及计算验算。

保留原包业务流程、判断标准和输出字段。真实业务的地区/期间/税率/法规/审批规则需按当前资料确认；原案例金额、人数、结论及旧政策均不能作为预设答案。只支持报告和通知草稿交付，不自动写 ERP/CRM、发薪、提交申报或向外部发消息。PDF 扫描件仍需视觉/OCR，无法识别的页明确列为缺口。

全量自动检查涵盖 62 个包、292 份资料、62 个独立安装与授权。模型完整演示属于抽样验收，不能据此声称所有真实业务场景已逐项完成结果验收。

## 本次验收

2026-09-22：31 项自动测试通过，62 个安装包与工作区副本一致，62 项智能体/技能/工具授权核对通过。浏览器抽测银行余额调节、版本一致性巡检、员工花名册体检、历史报价折扣核查，合计生成并交付 11 份文件。花名册首次运行发现项目解释器越过租户边界，已改用租户内独立 Python 并完成交付。银行余额与版本差异、报价金额另做独立复算。详见 `docs/workbuddy-scenario-verification.json`。

## 清单

| 分类 | 智能体 | 技能 / ID 前缀 | 演示文件数 |
|---|---|---|---:|
| 制造业 | 经营复盘 | `wb-biz-review` | 5 |
| 制造业 | BOM导入核对 | `wb-bom-import` | 4 |
| 制造业 | 合同条款确认 | `wb-contract-review` | 5 |
| 制造业 | 交期平衡与产销协同 | `wb-deadline-balance` | 4 |
| 制造业 | 交付风险评估 | `wb-delivery-risk` | 5 |
| 制造业 | 工程变更管理ECN | `wb-ecn` | 3 |
| 制造业 | 库存结构与呆滞料复盘 | `wb-idle-stock-review` | 5 |
| 制造业 | 异常分级与升级归口 | `wb-incident-triage` | 4 |
| 制造业 | 来料跟催 | `wb-material-chase` | 4 |
| 制造业 | 客户单据转订单 | `wb-order-intake` | 5 |
| 制造业 | 订单接单评审 | `wb-order-review` | 4 |
| 制造业 | 排产与插单调整 | `wb-priority-adjust` | 4 |
| 制造业 | 利润风险提示 | `wb-profit-risk` | 7 |
| 制造业 | 执行偏差追踪与补产 | `wb-progress-deviation` | 5 |
| 制造业 | 品质异常分析 | `wb-quality-analysis` | 5 |
| 制造业 | 对账差异核对 | `wb-reconciliation` | 4 |
| 制造业 | 齐套与缺料预警 | `wb-shortage-alert` | 7 |
| 制造业 | SOP生成与校验 | `wb-sop-gen` | 9 |
| 制造业 | 开工条件核查 | `wb-trial-check` | 5 |
| 制造业 | 版本一致性巡检 | `wb-version-audit` | 3 |
| 人力资源 | 劳动仲裁举证资料包 | `wb-arbitration-evidence-pack` | 4 |
| 人力资源 | 劳动合同台账重建与到期预警 | `wb-contract-expiry-ledger` | 5 |
| 人力资源 | 面试题与量化录用条件生成 | `wb-interview-criteria-gen` | 4 |
| 人力资源 | 月度人力成本一页纸 | `wb-labor-cost-onepager` | 4 |
| 人力资源 | 入职合规启动包 | `wb-onboarding-compliance-pack` | 4 |
| 人力资源 | 加班费敞口测算 | `wb-overtime-exposure` | 4 |
| 人力资源 | 算薪跨部门数据拼图 | `wb-payroll-data-assembly` | 5 |
| 人力资源 | 工资条生成与异常波动拦截 | `wb-payslip-anomaly` | 3 |
| 人力资源 | 绩效改进计划（PIP）文书与合规流程 | `wb-pip-docs` | 4 |
| 人力资源 | 试用期考核留痕与录用条件对照 | `wb-probation-evidence` | 4 |
| 人力资源 | 招聘漏斗月报与渠道 ROI | `wb-recruitment-funnel-report` | 4 |
| 人力资源 | 简历格式归一与结构化 | `wb-resume-normalize` | 5 |
| 人力资源 | 核心人才保留成本测算 | `wb-retention-cost` | 4 |
| 人力资源 | 员工花名册法定栏位体检与重建 | `wb-roster-audit` | 5 |
| 人力资源 | 社保基数三数比对体检 | `wb-social-insurance-base-check` | 4 |
| 财务 | 应收账款账龄分析与逾期预警 | `wb-ar-aging` | 4 |
| 财务 | 银行余额调节表自动编制 | `wb-bank-reconciliation` | 4 |
| 财务 | 预算编制数据收集 | `wb-budget-collection` | 4 |
| 财务 | 资金日报自动拼表 | `wb-cash-daily-report` | 4 |
| 财务 | 费用异常波动分析 | `wb-cost-variance` | 3 |
| 财务 | 报销单合规预审 | `wb-expense-precheck` | 4 |
| 财务 | 往来对账差异清单 | `wb-reconciliation-diff` | 4 |
| 财务 | 税收优惠体检与漏享扫描 | `wb-tax-benefit-scan` | 4 |
| 财务 | 纳税日历与逾期预警 | `wb-tax-calendar` | 3 |
| 财务 | 纳税申报前风险清单 | `wb-tax-risk-checklist` | 4 |
| 财务 | 凭证全量稽核扫描 | `wb-voucher-audit` | 3 |
| 财务 | 经营周报自动拼表 | `wb-weekly-report` | 3 |
| 销售 | 逾期应收账龄分档与差异化催收方案 | `wb-ar-aging-collection` | 6 |
| 销售 | 客户对账差异逐笔排查 | `wb-ar-reconciliation-gap` | 6 |
| 销售 | 关联方串标风险自查 | `wb-bid-collusion-selfcheck` | 6 |
| 销售 | 投不投的可行性初筛 | `wb-bid-go-nogo` | 6 |
| 销售 | 招标文件实质性条款逐条清单化 | `wb-bid-mandatory-clause` | 6 |
| 销售 | 商务标废标项逐条自查 | `wb-bid-void-selfcheck` | 6 |
| 销售 | 渠道价格体系一致性与窜货证据链 | `wb-channel-price-fleeing` | 6 |
| 销售 | 合同条款与标准模板偏离比对 | `wb-contract-clause-deviation` | 5 |
| 销售 | 合同到期与续签盘点 | `wb-contract-renewal-sweep` | 5 |
| 销售 | 合同评审跨部门资料一次性齐套 | `wb-contract-review-pack` | 6 |
| 销售 | 客户额度占用与超限发货预警 | `wb-credit-limit-exposure` | 6 |
| 销售 | 丢单复盘事实时间线重建 | `wb-lost-deal-timeline` | 6 |
| 销售 | 历史报价一致性与越权折扣核查 | `wb-quote-discount-audit` | 6 |
| 销售 | 经销商返利兑现核对 | `wb-rebate-reconciliation` | 6 |
| 销售 | 停滞商机识别与唤醒清单 | `wb-stalled-deal-detection` | 6 |

来源：[WorkBuddy 场景目录](https://www.workbuddy.link/p/ZljbYQzFpALFLcPwKPlct1)，核对版本 184。每个技能的 source.json 保存原包 URL、ZIP SHA-256 和逐文件 SHA-256。
