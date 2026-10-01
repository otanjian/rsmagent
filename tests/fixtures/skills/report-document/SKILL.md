---
name: report-document
description: 固定验收用文档技能。按模板与辅助资源渲染一份报告文档，字段来自输入 JSON。仅用于验收模板资源是否齐全、输出字段是否正确。
metadata:
  cowagent:
    requires:
      python: []
---

# 固定文档渲染（验收夹具）

本技能是 `align-desktop-project-execution-with-master` 验收 A07 的固定样例：带**模板**
（`assets/template.md`）与**辅助资源**（`references/fields.md`），输出字段由输入 JSON 决定。

## 资源

| 资源 | 作用 |
| --- | --- |
| `assets/template.md` | 报告模板，含 `{{...}}` 占位符 |
| `references/fields.md` | 字段清单与来源说明（辅助资源） |
| `scripts/render.py` | 渲染脚本 |

三个资源缺一不可：模板缺失时脚本必须失败，而不是产出一份"看起来像报告"的空文档。

## 执行

脚本路径相对本 SKILL.md。**只用标准库**，不需要额外安装。

```sh
python scripts/render.py --input <输入 JSON> --output <产出目录>
```

产出 `报告.md` 与 `渲染结果.json`，都只写在 `--output` 里。

## 边界

- 不联网、不读模板以外的外部文件。
- 输入 JSON 缺字段时列出缺失字段并按模板留空，不编造值。
