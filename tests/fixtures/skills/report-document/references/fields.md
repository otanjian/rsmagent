# 字段清单（辅助资源）

`report-document` 的模板占位符与来源。缺字段时留空并记录，不编造。

| 占位符 | 含义 | 来源 |
| --- | --- | --- |
| `{{title}}` | 报告标题 | 输入 JSON `title` |
| `{{customer}}` | 客户名称 | 输入 JSON `customer` |
| `{{project_code}}` | 项目编号 | 输入 JSON `project_code` |
| `{{summary}}` | 汇总金额 | 输入 JSON `summary` |
| `{{notes}}` | 备注 | 输入 JSON `notes`，可为空 |

本文件是**辅助资源**：验收要求它与模板一同随技能包送达。渲染脚本不读取它
（渲染只依赖模板），因此它缺失与否可单独核对 —— 这正是"辅助资源齐全"这条验收的
可观察点。
