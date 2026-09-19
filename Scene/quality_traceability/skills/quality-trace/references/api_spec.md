# 气袋质量追溯API接口规范

## 一、气袋总查询

### 编排信息

| 属性     | 值                  |
| -------- | ------------------- |
| 编排ID   | 13                  |
| 类型     | sequential          |
| 分组     | 质量智能追溯        |
| 状态     | published           |
| 超时时间 | 60000 ms            |
| 更新时间 | 2026-08-04T02:09:01 |

### Webhook 调用信息

| 属性         | 值                                                                                                   |
| ------------ | ---------------------------------------------------------------------------------------------------- |
| Webhook名称  | 气袋总查询-webhook                                                                                   |
| HTTP方法     | POST                                                                                                 |
| Content-Type | application/json                                                                                     |
| 认证方式     | 无认证                                                                                               |
| 调用地址     | `<your-airbag-webhook-url>` |

### 请求参数

| 参数名     | 类型   | 必填 | 说明     |
| ---------- | ------ | ---- | -------- |
| box_no     | string | 否   | 箱号     |
| p_line     | string | 否   | 产线     |
| barcode_no | string | 否   | 气袋条码 |

> **安全约束**: 三个参数至少提供一个，不允许全部为空。

### 请求体示例

```json
{
  "box_no": "",
  "p_line": "",
  "barcode_no": ""
}
```

### 调用示例 (curl)

```bash
curl -X POST "<your-airbag-webhook-url>" \
  -H "Content-Type: application/json" \
  -d '{"box_no": "", "p_line": "", "barcode_no": ""}'
```

---

## 二、气袋明细查询(限定面料LOT反查)

### 编排信息

| 属性     | 值                  |
| -------- | ------------------- |
| 编排ID   | 14                  |
| 类型     | sequential          |
| 分组     | 质量智能追溯        |
| 状态     | published           |
| 超时时间 | 60000 ms            |
| 更新时间 | 2026-08-04T02:10:49 |

### Webhook 调用信息

| 属性         | 值                                                                                                   |
| ------------ | ---------------------------------------------------------------------------------------------------- |
| Webhook名称  | 气袋明细查询(面料LOT)-webhook                                                                        |
| HTTP方法     | POST                                                                                                 |
| Content-Type | application/json                                                                                     |
| 认证方式     | 无认证                                                                                               |
| 调用地址     | `<your-airbag-webhook-url>` |

### 请求参数

| 参数名 | 类型   | 必填 | 说明    |
| ------ | ------ | ---- | ------- |
| f_lot  | string | 是   | 面料LOT |

> **安全约束**: 面料LOT为必填参数，不允许为空。

### 请求体示例

```json
{
  "f_lot": ""
}
```

### 调用示例 (curl)

```bash
curl -X POST "<your-airbag-webhook-url>" \
  -H "Content-Type: application/json" \
  -d '{"f_lot": ""}'
```

---

## 三、气袋原料追溯查询

### 编排信息

| 属性     | 值                  |
| -------- | ------------------- |
| 编排ID   | 15                  |
| 类型     | sequential          |
| 分组     | 质量智能追溯        |
| 状态     | published           |
| 超时时间 | 60000 ms            |
| 更新时间 | 2026-08-04T06:59:08 |

### Webhook 调用信息

| 属性         | 值                                                                                                   |
| ------------ | ---------------------------------------------------------------------------------------------------- |
| Webhook名称  | 气袋总查询_原料追溯-webhook                                                                          |
| HTTP方法     | POST                                                                                                 |
| Content-Type | application/json                                                                                     |
| 认证方式     | 无认证                                                                                               |
| 调用地址     | `<your-airbag-webhook-url>` |

### 请求参数

| 参数名      | 类型   | 必填 | 说明         |
| ----------- | ------ | ---- | ------------ |
| header_id   | string | 是   | 作业单号     |
| cut_version | string | 否   | 裁片换料版本 |
| sew_version | string | 否   | 线料换料版本 |

> **安全约束**: 作业单号(header_id)为必填参数，不允许为空。
> **业务逻辑**: header_id / cut_version / sew_version 可从气袋总查询结果中获取。

### 请求体示例

```json
{
  "header_id": "",
  "cut_version": "",
  "sew_version": ""
}
```

### 调用示例 (curl)

```bash
curl -X POST "<your-airbag-webhook-url>" \
  -H "Content-Type: application/json" \
  -d '{"header_id": "", "cut_version": "", "sew_version": ""}'
```

### 业务编排说明

```
用户查询气袋原料追溯的典型流程：

┌─────────────┐     barcode_no      ┌─────────────┐
│  气袋总查询  │ ──────────────────> │  获取气袋信息 │
│  (编排ID:13) │                     │  header_id   │
└─────────────┘                     │  cut_version │
                                     │  sew_version │
                                     └──────┬──────┘
                                            │
                                            v
┌─────────────────────┐  header_id    ┌──────────────┐
│  气袋原料追溯查询     │ <──────────── │  组合查询     │
│  (编排ID:15)         │  cut_version  └──────────────┘
│                     │  sew_version
└─────────┬───────────┘
          │
          v
   ┌──────────────┐
   │  原料追溯结果  │
   │  (原料清单)    │
   └──────────────┘
```

---

## 环境变量配置

```env
# 气袋总查询Webhook地址
AIRBAG_WEBHOOK_URL=<your-airbag-webhook-url>

# 气袋明细查询(面料LOT)Webhook地址
AIRBAG_DETAIL_WEBHOOK_URL=<your-airbag-webhook-url>

# 气袋原料追溯查询Webhook地址
AIRBAG_MATERIAL_WEBHOOK_URL=<your-airbag-webhook-url>
```

## Python调用示例

```python
from scripts.trace_query import AirbagQuery, AirbagDetailQuery, AirbagMaterialQuery

# ========== 气袋总查询 ==========
query = AirbagQuery()

# 按箱号查询
result = query.query_airbags(box_no="BOX001")

# 按产线查询
result = query.query_airbags(p_line="LINE01")

# 按气袋条码查询
result = query.query_airbags(barcode_no="BC001")

# 组合查询
result = query.query_airbags(box_no="BOX001", p_line="LINE01")

# 格式化输出
print(query.format_result(result))

# ========== 气袋明细查询(面料LOT) ==========
detail_query = AirbagDetailQuery()

# 按面料LOT查询
result = detail_query.query_by_fabric_lot(f_lot="LOT20260801")

# 格式化输出
print(detail_query.format_result(result))

# ========== 气袋原料追溯查询 ==========
material_query = AirbagMaterialQuery()

# 方式1：直接按作业单号查原料
result = material_query.query_materials(header_id="H001", cut_version="V1", sew_version="V1")
print(material_query.format_result(result))

# 方式2：通过气袋条码自动两步追溯（推荐）
trace_result = material_query.trace_materials_by_barcode(barcode_no="BC001")
print(material_query.format_trace_result(trace_result))
```

## 更新日志

| 版本  | 日期       | 说明                                                                |
| ----- | ---------- | ------------------------------------------------------------------- |
| 1.0.3 | 2026-08-04 | 新增气袋原料追溯查询功能；支持条码自动两步追溯                      |
| 1.0.2 | 2026-08-04 | 新增气袋明细查询(面料LOT反查)；气袋总查询新增p_line、barcode_no参数 |
| 1.0.1 | 2026-07-31 | 安全加固：强制要求查询条件，禁止全表查询                            |
| 1.0.0 | 2026-07-31 | 初始版本，支持气袋总查询                                            |
