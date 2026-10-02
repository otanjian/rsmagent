---
name: summary-workbook
description: 固定验收用工作簿技能。读取含三条合成记录（金额 100/200/50）的固定工作簿，生成汇总工作簿与结果 JSON。仅用于验收，不承诺任何税务或会计规则。
metadata:
  cowagent:
    requires:
      python:
        - xlsxwriter
    install:
      - kind: pip
        package: xlsxwriter
        label: 写出汇总工作簿（scripts/summarize.py 的 Report 写入器）
---

# 固定工作簿汇总（验收夹具）

本技能是 `align-desktop-project-execution-with-master` 验收 A06 的固定样例：输入工作簿
含**三条**合成记录，金额为 100、200、50，汇总预期为 **350**。

## 用途

只验证"数据处理 + 文件生成 + 产出落在所选项目"这条链路，不承诺任何税务或会计计算规则，
也不读真实业务数据。

## 依赖与执行

脚本路径相对本 SKILL.md。使用本项目 Python 环境（依赖见 `requirements.txt`）。

```sh
python scripts/make_input.py --output <输入工作簿路径>
python scripts/summarize.py --input <输入工作簿路径> --output <产出目录>
```

`summarize.py` 只在 `--output` 指定的目录里写文件，产出：

- `汇总报告.xlsx` —— 含 `汇总` 单元格（值 350）；
- `汇总结果.json` —— 机器可读的核对结果。

## 边界

- 三条记录固定为 100/200/50；脚本**不**接受外部数据源，也不联网。
- 汇总只做加法；不做税、折扣、汇率或审批判断。
- 输入工作簿的字节在运行前后必须一致（验收会核对输入摘要不变）。
