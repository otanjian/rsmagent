---
name: quality-trace
version: 1.2.0
description: |
  用于质量智能追溯的企业级Skill，支持气袋总查询、气袋明细查询(面料LOT反查)、气袋原料追溯查询。
  在用户要求"气袋查询"、"气袋总查询"、"查询气袋"、"面料LOT查询"、"气袋明细查询"、"原料追溯"、"气袋用了什么原料"时触发。
author: hmt-team
tags: [quality, traceability, airbag, inspection, query, fabric-lot, material]
requires:
  python: ">=3.10"
  packages:
    - requests>=2.31.0
    - tenacity>=8.2.0
config:
  airbag_webhook_url:
    type: string
    default: "<your-airbag-webhook-url>"
    description: "气袋总查询Webhook地址"
  airbag_detail_webhook_url:
    type: string
    default: "<your-airbag-webhook-url>"
    description: "气袋明细查询(面料LOT)Webhook地址"
  airbag_material_webhook_url:
    type: string
    default: "<your-airbag-webhook-url>"
    description: "气袋原料追溯查询Webhook地址"
metadata:
  openclaw:
    requires: {}
---

# 气袋质量追溯技能

## 功能概述
本技能提供气袋质量追溯查询能力，包含三个核心功能：
1. 气袋总查询 - 按箱号/产线/条码查询气袋基本信息
2. 气袋明细查询 - 按面料LOT反查气袋明细
3. 气袋原料追溯查询 - 查询气袋所用原料（支持条码自动两步追溯）

## 触发场景
- 当用户询问"气袋查询"、"气袋总查询"、"查询气袋"时 → 调用气袋总查询
- 当用户询问"面料LOT查询"、"气袋明细查询"、"面料反查"时 → 调用气袋明细查询
- 当用户询问"原料追溯"、"气袋用了什么原料"、"查原料"时 → 调用气袋原料追溯查询

## 核心能力

### 1. 气袋总查询
- 支持按**箱号(box_no)**、**产线(p_line)**、**气袋条码(barcode_no)** 查询
- 支持组合条件查询
- **至少提供一个查询条件**，禁止全表查询
- 返回结果包含 header_id、cut_version、sew_version 等字段（供原料追溯使用）

### 2. 气袋明细查询(限定面料LOT反查)
- 支持按**面料LOT(f_lot)** 反查气袋明细
- **面料LOT为必填参数**，不允许为空

### 3. 气袋原料追溯查询
- 支持按**作业单号(header_id)** + 裁片换料版本(cut_version) + 线料换料版本(sew_version) 查询所用原料
- **header_id 为必填参数**
- 支持**条码自动两步追溯**：输入气袋条码 → 自动查气袋总查询获取 header_id/cut_version/sew_version → 自动查原料

## 安全约束
- **所有查询均必须提供查询条件**，禁止无条件查询
- 气袋总查询：box_no / p_line / barcode_no 至少提供一个
- 气袋明细查询：f_lot 必填
- 气袋原料追溯：header_id 必填
- 违反约束将抛出 ValueError 异常

## 执行流程

### 气袋总查询流程
1. 用户提供查询条件（箱号/产线/气袋条码，至少一项）
2. 调用气袋总查询Webhook接口
3. 获取气袋数据并格式化输出

### 气袋明细查询流程
1. 用户提供面料LOT号
2. 调用气袋明细查询Webhook接口
3. 获取气袋明细数据并格式化输出

### 气袋原料追溯流程
1. **方式一（直接查询）**：用户提供 header_id（+可选 cut_version/sew_version）
2. **方式二（条码追溯）**：用户提供 barcode_no → 自动调用气袋总查询获取 header_id/cut_version/sew_version → 再调用原料追溯接口
3. 获取原料追溯数据并格式化输出

## 输出格式
- **文本摘要**: 自然语言描述查询结果
- **结构化数据**: JSON格式详细数据

## 目录结构
- `scripts/`: 查询脚本（AirbagQuery + AirbagDetailQuery + AirbagMaterialQuery）
- `references/`: API接口文档
- `API/`: 原始编排接口文档

## 边界与禁用场景
- 不处理大量数据的分页展示（单次调用）
- 不支持数据修改操作
- 禁止无条件全表查询

## 更新日志

### v1.2.0 (2026-08-04)
- 新增气袋原料追溯查询功能（编排ID: 15）
- 支持条码自动两步追溯（barcode_no → 气袋总查询 → 原料追溯）
- 支持直接按作业单号查原料

### v1.1.0 (2026-08-04)
- 新增气袋明细查询(面料LOT反查)功能
- 气袋总查询新增产线(p_line)、气袋条码(barcode_no)参数
- 支持组合条件查询

### v1.0.1 (2026-07-31)
- 安全加固：强制要求查询条件，禁止全表查询

### v1.0.0 (2026-07-31)
- 初始版本，支持气袋总查询
