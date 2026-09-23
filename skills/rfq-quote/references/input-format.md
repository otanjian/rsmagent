# 核算输入约定

完整样例见 `../assets/demo/quote-input.json`。复制结构并逐项替换为本次资料，不把演示取值当缺省企业政策。所有金额均为同一币种、同一不含税口径；若原表含税，先明确税率和可抵扣口径并转换，保留换算依据。

顶层必填：`rfq_id`、`customer`、`part`、`currency`、`tax_basis`、`price_version`、`valid_days`（正整数）、`management_rate`（小数）、`markup_floor`（小数）、`policy_source`。`demo` 为布尔值，真实订单为 false。

## 材料和成本

`material`：`net_area` 净展开 m²，`thickness_mm` 厚度 mm，`density` 密度 g/cm³，`utilization` 0 至 1 之间且非零，`price` 板材元/kg，`scrap_price` 回收元/kg，`source` 来源与版本，`utilization_reason` 利用率依据。脚本以完整精度计算毛重和废料重，金额才四舍五入至分；不预先把下料面积四舍五入。

`cost_lines`：每行含 `category`（直接材料/加工费/表面处理/包装运输）、`item`、`quantity`、`unit`、`price`、`source`、`basis`。板材和废料由脚本自动生成，不要重复录入。加工用量用完整工时 h，表处用净展开面积，包装运费用每台摊分数量。四类必须完整；不适用项可显式填 0 并写明原因，未知值不能填 0。

## 三档报价

`tiers` 依次三项，`kind` 为 standard、urgent、volume。每项含 `name`、`quantity`、`days`（提议交期，须生产确认）、`markup`、`source`、`adjustments`。

每个调整项指定 `item`、`source`，以及下列二选一：

- `amount`：制造成本单台固定差额。
- `category` 和 `rate`：对应基础成本分类合计 × 比例（降本为负）。这是相对基础档金额，不是重复叠乘。

逐项调整制造成本以后重新计管理费。常规档 adjustments 为空，加急加成率与常规相同；批量档数量不可小于常规。脚本不推断采购阶梯，是否满足重量/件数门槛必须先核实，并写在来源中。报价单价保留两位，毛利率按实际单价计算。

## 历史校核

`history` 无法可靠构造时设为 null，输出“缺少校核依据”，不能为了通过而编补差额。可用时提供：`actual_cost` 历史实际总成本、`source`、`comparability`（逐项相似依据与局限）、`adjustments`（每项 `amount`、`reason`、`source`）。修正必须有独立依据，不能使用“补齐到本次成本”的倒算值。

偏差 =（本次总成本－历史同口径总成本）÷历史同口径总成本。超过 8% 停止报价；3% 至 8% 提交复核。无偏差也不代表已获得最终报价审批。

## 风险

`risks` 每项包含 `item`、`kind`、`assumption`、`alternative`、`exposure`、`source`、`owner`、`deadline`。`exposure` 是**单台总成本**最大绝对变化，包含相应管理费，订单敞口按常规档数量计算；未知设 null，不能填 0。金额敞口是独立情形，不得不加判断地累加互斥风险。

`kind` 中 material/thickness/welding/heat_treatment/tolerance/history_loss 至少 A，surface/hardware/packaging 至少 B，其他按金额评级；金额未知至少 A。`resolved` 默认为 false，设 true 必须同时写 `confirmation`（确认人、时间和证据）。确认改变成本时先更新成本输入再重新核算。

输出永远是内部草稿。`calculate()` 不发送、不写台账、也不能代表人为批准。风险清单与三个工作簿为本次输入快照，改参数后运行新目录，保留旧版本。
