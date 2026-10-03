"""SAP 数据分析全局配置。

集中管理：
- 高管/微信入口默认连接与参数
- 领域、表、字段、BAPI 白名单
- V1 自然语言查询模板
- 领域与权限的映射

避免将业务规则硬编码在 query_planner / permission_guard / fetch_executor 中。
"""

from Scene.catalog import skill_path
from auth.policy import SAP_DOMAIN_PERMISSIONS as DOMAIN_PERMISSION_MAP

import json
import os
from typing import Any, Dict, List, Optional


# =============================================================================
# SAP 查询能力目录（catalog）加载
# =============================================================================
def _load_catalog() -> Dict[str, Any]:
    """加载 skills/sap-integration/references/sap_query_catalog.json。"""
    path = skill_path('sap-integration') / 'references' / 'sap_query_catalog.json'
    if path.is_file():
        with path.open(encoding="utf-8") as source:
            return json.load(source)
    return {}


_CATALOG = _load_catalog()


def get_catalog() -> Dict[str, Any]:
    """返回已加载的 catalog（外部模块可调用）。"""
    return _CATALOG


# =============================================================================
# 高管 / 微信入口默认配置
# =============================================================================
EXECUTIVE_DEFAULTS: Dict[str, Any] = {
    "role": "executive",
    "default_connection_id": "",
    "default_company_code": "",
    "default_period_rule": "current_month",  # current_month / current_quarter / current_year
    "enabled_intents": [
        "sales_overview",
        "procurement_overview",
        "finance_overview",
        "inventory_overview",
    ],
}

# =============================================================================
# 领域级数据查询权限映射
# =============================================================================


# 拥有该权限即跳过所有领域/表/BAPI 校验
ADMIN_PERMISSION = "sap.query.admin"

# =============================================================================
# 表级白名单：按领域划分（从 catalog 自动生成）
# =============================================================================
def _build_domain_table_whitelist(catalog: Dict[str, Any]) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    for api_key, meta in catalog.get("apis", {}).items():
        if meta.get("type") != "table":
            continue
        domain = meta.get("domain", "master_data")
        result.setdefault(domain, []).append(api_key.upper())
    # 保证每个领域都有列表
    for domain in DOMAIN_PERMISSION_MAP:
        result.setdefault(domain, [])
    return result


DOMAIN_TABLE_WHITELIST = _build_domain_table_whitelist(_CATALOG)

# 聚合成统一的表 -> 领域集合映射，用于校验
TABLE_DOMAIN_MAP: Dict[str, List[str]] = {}
for _domain, _tables in DOMAIN_TABLE_WHITELIST.items():
    for _table in _tables:
        TABLE_DOMAIN_MAP.setdefault(_table.upper(), []).append(_domain)

# =============================================================================
# BAPI 白名单：按领域划分（从 catalog 自动生成，仅允许只读类 BAPI）
# =============================================================================
def _build_domain_bapi_whitelist(catalog: Dict[str, Any]) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    for api_key, meta in catalog.get("apis", {}).items():
        if meta.get("type") != "bapi" or not meta.get("readonly", False):
            continue
        domain = meta.get("domain", "master_data")
        result.setdefault(domain, []).append(api_key.upper())
    return result


DOMAIN_BAPI_WHITELIST = _build_domain_bapi_whitelist(_CATALOG)

BAPI_DOMAIN_MAP: Dict[str, List[str]] = {}
for _domain, _bapis in DOMAIN_BAPI_WHITELIST.items():
    for _bapi in _bapis:
        BAPI_DOMAIN_MAP.setdefault(_bapi.upper(), []).append(_domain)

# =============================================================================
# 字段级敏感字段黑名单（全局生效，无论是否有表权限）
# =============================================================================
SENSITIVE_FIELD_BLACKLIST = {
    "PASSWORD",
    "PASSWRD",
    "CREDIT_CARD",
    "BANKL",
    "BANKN",
    "STCD1",
    "STCD2",
}

# =============================================================================
# V1 查询模板
# 每个模板声明：意图、领域、来源、表、字段、默认 WHERE、字段中文映射、匹配关键词
# =============================================================================
V1_TEMPLATES: List[Dict[str, Any]] = [
    # -------------------------------------------------------------------------
    # 主数据 - 客户
    # -------------------------------------------------------------------------
    {
        "intent": "new_customers_this_month",
        "domain": "master_data",
        "source": "table",
        "table": "KNA1",
        "fields": ["KUNNR", "NAME1", "ORT01", "LAND1", "ERDAT"],
        "where_template": "ERDAT BETWEEN '{date_from_sap}' AND '{date_to_sap}'",
        "aggregation": None,
        "group_by": [],
        "order_by": ["ERDAT DESC"],
        "max_rows": 5000,
        "field_mapping": {
            "KUNNR": "客户编码",
            "NAME1": "客户名称",
            "ORT01": "城市",
            "LAND1": "国家",
            "ERDAT": "创建日期",
        },
        "keywords": [
            "新增客户", "新客户", "本月客户", "这个月新增了多少家客户",
            "今年新增客户", "今年新增多少客户", "今年有多少新客户", "本年度新增客户",
            "截止目前新增客户", "截至目前新增客户", "截止目前有多少客户",
            "累计新增客户", "总共新增客户", "客户编号", "客户号",
            "第一季度", "Q1", "第1季度",
            "第二季度", "Q2", "第2季度",
            "第三季度", "Q3", "第3季度",
            "第四季度", "Q4", "第4季度",
        ],
        "required_context": ["date_from", "date_to"],
    },
    {
        "intent": "customer_list",
        "domain": "master_data",
        "source": "table",
        "table": "KNA1",
        "fields": ["KUNNR", "NAME1", "ORT01", "LAND1", "ERDAT"],
        "where_template": "",
        "aggregation": None,
        "group_by": [],
        "order_by": ["KUNNR"],
        "max_rows": 5000,
        "field_mapping": {
            "KUNNR": "客户编码",
            "NAME1": "客户名称",
            "ORT01": "城市",
            "LAND1": "国家",
            "ERDAT": "创建日期",
        },
        "keywords": [
            "客户列表", "所有客户", "客户主数据", "有多少家客户", "截止目前有多少家客户",
            "今年有多少家客户", "累计有多少家客户", "总共有多少客户",
        ],
        "required_context": [],
    },
    {
        "intent": "customer_credit_limit",
        "domain": "master_data",
        "source": "table",
        "table": "KNKK",
        "fields": ["KUNNR", "KKBER", "KLIMK", "OBLIG", "SKFOR", "KNKAU"],
        "where_template": "",
        "aggregation": None,
        "group_by": [],
        "order_by": ["KUNNR", "KKBER"],
        "max_rows": 5000,
        "field_mapping": {
            "KUNNR": "客户编码",
            "KKBER": "信用控制范围",
            "KLIMK": "信用额度",
            "OBLIG": "信用风险总额",
            "SKFOR": "应收账款",
            "KNKAU": "销售值",
        },
        "keywords": [
            "信用额度", "客户信用", "信用限额", "客户额度", "信用控制",
        ],
        "required_context": [],
    },
    # -------------------------------------------------------------------------
    # 主数据 - 供应商
    # -------------------------------------------------------------------------
    {
        "intent": "new_suppliers_this_month",
        "domain": "master_data",
        "source": "table",
        "table": "LFA1",
        "fields": ["LIFNR", "NAME1", "ORT01", "LAND1", "ERDAT"],
        "where_template": "LOEVM = '' AND ERDAT BETWEEN '{date_from_sap}' AND '{date_to_sap}'",
        "aggregation": None,
        "group_by": [],
        "order_by": ["ERDAT DESC"],
        "max_rows": 5000,
        "field_mapping": {
            "LIFNR": "供应商编码",
            "NAME1": "供应商名称",
            "ORT01": "城市",
            "LAND1": "国家",
            "ERDAT": "创建日期",
        },
        "keywords": [
            "新增供应商", "新供应商", "本月供应商", "供应商编号", "供应商号",
            "第一季度", "Q1", "第1季度",
            "第二季度", "Q2", "第2季度",
            "第三季度", "Q3", "第3季度",
            "第四季度", "Q4", "第4季度",
        ],
        "required_context": ["date_from", "date_to"],
    },
    # -------------------------------------------------------------------------
    # 主数据 - 物料
    # -------------------------------------------------------------------------
    {
        "intent": "new_materials_this_period",
        "domain": "master_data",
        "source": "table",
        "table": "MARA",
        "fields": ["MATNR", "MTART", "MATKL", "MEINS", "ERSDA", "ERNAM"],
        "where_template": "ERSDA BETWEEN '{date_from_sap}' AND '{date_to_sap}'",
        "aggregation": None,
        "group_by": [],
        "order_by": ["ERSDA DESC"],
        "max_rows": 5000,
        "field_mapping": {
            "MATNR": "物料编码",
            "MTART": "物料类型",
            "MATKL": "物料组",
            "MEINS": "基本单位",
            "ERSDA": "创建日期",
            "ERNAM": "创建者",
        },
        "keywords": [
            "新增物料", "新物料", "物料号", "物料编号", "物料列表", "物料主数据",
            "今年物料", "本月物料", "物料创建",
            "第一季度", "Q1", "第1季度",
            "第二季度", "Q2", "第2季度",
            "第三季度", "Q3", "第3季度",
            "第四季度", "Q4", "第4季度",
        ],
        "required_context": ["date_from", "date_to"],
    },
    # -------------------------------------------------------------------------
    # 销售
    # -------------------------------------------------------------------------
    {
        "intent": "sales_orders_this_month",
        "domain": "sales",
        "source": "table",
        "table": "VBAK",
        "fields": ["VBELN", "AUDAT", "KUNNR", "NETWR", "WAERK", "VKORG"],
        "where_template": "AUDAT BETWEEN '{date_from_sap}' AND '{date_to_sap}'",
        "aggregation": None,
        "group_by": [],
        "order_by": ["AUDAT DESC"],
        "max_rows": 5000,
        "field_mapping": {
            "VBELN": "销售订单号",
            "AUDAT": "订单日期",
            "KUNNR": "客户编码",
            "NETWR": "净值",
            "WAERK": "货币",
            "VKORG": "销售组织",
        },
        "keywords": [
            "销售订单", "本月销售", "这个月销售", "销售情况",
            "第一季度", "Q1", "第1季度",
            "第二季度", "Q2", "第2季度",
            "第三季度", "Q3", "第3季度",
            "第四季度", "Q4", "第4季度",
        ],
        "required_context": ["date_from", "date_to"],
    },
    {
        "intent": "sales_amount_this_month",
        "domain": "sales",
        "source": "table",
        "table": "VBRK",
        "fields": ["VBELN", "FKDAT", "KUNNR", "NETWR", "WAERK"],
        "where_template": "FKDAT BETWEEN '{date_from_sap}' AND '{date_to_sap}'",
        "aggregation": "SUM",
        "group_by": ["WAERK"],
        "order_by": [],
        "max_rows": 5000,
        "field_mapping": {
            "VBELN": "发票号",
            "FKDAT": "发票日期",
            "KUNNR": "客户编码",
            "NETWR": "净值",
            "WAERK": "货币",
        },
        "keywords": [
            "销售额", "销售金额", "本月销售额", "销售收入", "销量", "销售情况",
            "上半年销售", "下半年销售", "今年销售", "去年销售", "今年销量", "去年销量",
            "第一季度", "Q1", "第1季度",
            "第二季度", "Q2", "第2季度",
            "第三季度", "Q3", "第3季度",
            "第四季度", "Q4", "第4季度",
        ],
        "required_context": ["date_from", "date_to"],
    },
    {
        "intent": "top_customers_by_sales",
        "domain": "sales",
        "source": "table",
        "table": "VBRK",
        "fields": ["KUNNR", "NETWR", "WAERK"],
        "where_template": "FKDAT BETWEEN '{date_from_sap}' AND '{date_to_sap}'",
        "aggregation": "SUM",
        "group_by": ["KUNNR", "WAERK"],
        "order_by": ["NETWR DESC"],
        "max_rows": 5000,
        "field_mapping": {
            "KUNNR": "客户编码",
            "NETWR": "销售金额",
            "WAERK": "货币",
        },
        "keywords": [
            "销售排名", "Top 客户", "销售额排名前", "客户销售排名",
            "第一季度", "Q1", "第1季度",
            "第二季度", "Q2", "第2季度",
            "第三季度", "Q3", "第3季度",
            "第四季度", "Q4", "第4季度",
        ],
        "required_context": ["date_from", "date_to"],
    },
    # -------------------------------------------------------------------------
    # 采购
    # -------------------------------------------------------------------------
    {
        "intent": "purchase_orders_this_month",
        "domain": "procurement",
        "source": "table",
        "table": "EKKO",
        "fields": ["EBELN", "BEDAT", "LIFNR", "EKORG", "WAERS", "ERNAM"],
        "where_template": "BEDAT BETWEEN '{date_from_sap}' AND '{date_to_sap}'",
        "aggregation": None,
        "group_by": [],
        "order_by": ["BEDAT DESC"],
        "max_rows": 5000,
        "field_mapping": {
            "EBELN": "采购订单号",
            "BEDAT": "订单日期",
            "LIFNR": "供应商编码",
            "EKORG": "采购组织",
            "WAERS": "货币",
            "ERNAM": "创建人",
        },
        "keywords": [
            "采购订单", "本月采购", "采购情况",
            "第一季度", "Q1", "第1季度",
            "第二季度", "Q2", "第2季度",
            "第三季度", "Q3", "第3季度",
            "第四季度", "Q4", "第4季度",
        ],
        "required_context": ["date_from", "date_to"],
    },
    {
        "intent": "purchase_amount_this_month",
        "domain": "procurement",
        "source": "table",
        "table": "EKPO",
        "fields": ["EBELN", "EBELP", "BEDAT", "LIFNR", "NETPR", "BRTWR", "WAERS"],
        "where_template": "BEDAT BETWEEN '{date_from_sap}' AND '{date_to_sap}'",
        "aggregation": "SUM",
        "group_by": ["WAERS"],
        "order_by": [],
        "max_rows": 5000,
        "field_mapping": {
            "EBELN": "采购订单号",
            "EBELP": "行项目",
            "BEDAT": "订单日期",
            "LIFNR": "供应商编码",
            "NETPR": "净价",
            "BRTWR": "总金额",
            "WAERS": "货币",
        },
        "keywords": [
            "采购金额", "采购额", "本月采购金额", "采购总额",
            "第一季度", "Q1", "第1季度",
            "第二季度", "Q2", "第2季度",
            "第三季度", "Q3", "第3季度",
            "第四季度", "Q4", "第4季度",
        ],
        "required_context": ["date_from", "date_to"],
    },
    {
        "intent": "top_suppliers_by_purchase",
        "domain": "procurement",
        "source": "table",
        "table": "EKPO",
        "fields": ["LIFNR", "BRTWR", "WAERS"],
        "where_template": "BEDAT BETWEEN '{date_from_sap}' AND '{date_to_sap}'",
        "aggregation": "SUM",
        "group_by": ["LIFNR", "WAERS"],
        "order_by": ["BRTWR DESC"],
        "max_rows": 5000,
        "field_mapping": {
            "LIFNR": "供应商编码",
            "BRTWR": "采购金额",
            "WAERS": "货币",
        },
        "keywords": [
            "供应商排名", "采购排名", "采购额排名前", "供应商采购排名",
            "第一季度", "Q1", "第1季度",
            "第二季度", "Q2", "第2季度",
            "第三季度", "Q3", "第3季度",
            "第四季度", "Q4", "第4季度",
        ],
        "required_context": ["date_from", "date_to"],
    },
    # -------------------------------------------------------------------------
    # 财务
    # -------------------------------------------------------------------------
    {
        "intent": "finance_overview",
        "domain": "finance",
        "source": "table",
        "table": "BKPF",
        "fields": ["BUKRS", "BELNR", "GJAHR", "BLDAT", "MONAT", "WAERS"],
        "where_template": "BUKRS = '{company_code}' AND BLDAT BETWEEN '{date_from_sap}' AND '{date_to_sap}'",
        "aggregation": None,
        "group_by": [],
        "order_by": ["BLDAT DESC"],
        "max_rows": 5000,
        "field_mapping": {
            "BUKRS": "公司代码",
            "BELNR": "凭证号",
            "GJAHR": "会计年度",
            "BLDAT": "凭证日期",
            "MONAT": "会计期间",
            "WAERS": "货币",
        },
        "keywords": [
            "财务情况", "财务凭证", "本月财务", "财务分析",
            "第一季度", "Q1", "第1季度",
            "第二季度", "Q2", "第2季度",
            "第三季度", "Q3", "第3季度",
            "第四季度", "Q4", "第4季度",
        ],
        "required_context": ["company_code", "date_from", "date_to"],
    },
    # -------------------------------------------------------------------------
    # 库存
    # -------------------------------------------------------------------------
    {
        "intent": "inventory_overview",
        "domain": "inventory",
        "source": "table",
        "table": "MARD",
        "fields": ["MATNR", "WERKS", "LGORT", "LABST", "MEINS"],
        "where_template": "LABST > 0",
        "aggregation": None,
        "group_by": [],
        "order_by": ["MATNR"],
        "max_rows": 5000,
        "field_mapping": {
            "MATNR": "物料编码",
            "WERKS": "工厂",
            "LGORT": "库存地点",
            "LABST": "库存数量",
            "MEINS": "单位",
        },
        "keywords": [
            "库存", "库存情况", "库存数量", "物料库存",
            "第一季度", "Q1", "第1季度",
            "第二季度", "Q2", "第2季度",
            "第三季度", "Q3", "第3季度",
            "第四季度", "Q4", "第4季度",
        ],
        "required_context": [],
    },
    {
        "intent": "material_inventory",
        "domain": "inventory",
        "source": "table",
        "table": "MARD",
        "fields": ["MATNR", "WERKS", "LGORT", "LABST", "MEINS"],
        "where_template": "",
        "aggregation": None,
        "group_by": [],
        "order_by": ["MATNR"],
        "max_rows": 5000,
        "field_mapping": {
            "MATNR": "物料编码",
            "WERKS": "工厂",
            "LGORT": "库存地点",
            "LABST": "库存数量",
            "MEINS": "单位",
        },
        "keywords": [
            "物料库存",
        ],
        "required_context": ["matnr"],
    },
    # -------------------------------------------------------------------------
    # 生产
    # -------------------------------------------------------------------------
    {
        "intent": "production_order_items",
        "domain": "production",
        "source": "multi_table",
        "table": "AUFK",
        "fields": ["AUFK.AUFNR", "AUFK.AUART", "AUFK.WERKS", "AFPO.POSNR", "AFPO.MATNR", "AFPO.MEINS", "AFPO.MENGE"],
        "where_template": "AUFK.ERDAT BETWEEN '{date_from_sap}' AND '{date_to_sap}'",
        "aggregation": None,
        "group_by": [],
        "order_by": ["AUFK.ERDAT DESC"],
        "max_rows": 5000,
        "joins": [
            {
                "table": "AFPO",
                "on": "AFPO.AUFNR = AUFK.AUFNR",
                "fields": ["POSNR", "MATNR", "MEINS", "MENGE"],
                "join_type": "inner",
            }
        ],
        "field_mapping": {
            "AUFK.AUFNR": "生产订单号",
            "AUFK.AUART": "订单类型",
            "AUFK.WERKS": "工厂",
            "AFPO.POSNR": "行项目",
            "AFPO.MATNR": "物料编码",
            "AFPO.MEINS": "单位",
            "AFPO.MENGE": "数量",
        },
        "keywords": [
            "生产订单明细", "生产订单行项目", "生产订单物料", "工单明细",
            "第一季度", "Q1", "第1季度",
            "第二季度", "Q2", "第2季度",
            "第三季度", "Q3", "第3季度",
            "第四季度", "Q4", "第4季度",
        ],
        "required_context": ["date_from", "date_to"],
    },
    {
        "intent": "production_order_schedule",
        "domain": "production",
        "source": "multi_table",
        "table": "AUFK",
        "fields": ["AUFK.AUFNR", "AUFK.AUART", "AUFK.WERKS", "AFKO.GSTRP", "AFKO.GLTRP", "AFKO.GAMNG"],
        "where_template": "AUFK.ERDAT BETWEEN '{date_from_sap}' AND '{date_to_sap}'",
        "aggregation": None,
        "group_by": [],
        "order_by": ["AFKO.GSTRP DESC"],
        "max_rows": 5000,
        "joins": [
            {
                "table": "AFKO",
                "on": "AFKO.AUFNR = AUFK.AUFNR",
                "fields": ["GSTRP", "GLTRP", "GAMNG"],
                "join_type": "left",
            }
        ],
        "field_mapping": {
            "AUFK.AUFNR": "生产订单号",
            "AUFK.AUART": "订单类型",
            "AUFK.WERKS": "工厂",
            "AFKO.GSTRP": "计划开始日期",
            "AFKO.GLTRP": "计划完成日期",
            "AFKO.GAMNG": "订单数量",
        },
        "keywords": [
            "生产订单排程", "生产订单进度", "生产订单计划", "生产订单完成情况",
            "工单进度", "工单计划",
            "第一季度", "Q1", "第1季度",
            "第二季度", "Q2", "第2季度",
            "第三季度", "Q3", "第3季度",
            "第四季度", "Q4", "第4季度",
        ],
        "required_context": ["date_from", "date_to"],
    },
    {
        "intent": "production_orders_this_month",
        "domain": "production",
        "source": "multi_table",
        "table": "AUFK",
        "fields": ["AUFK.AUFNR", "AUFK.AUART", "AUFK.WERKS", "AFKO.GAMNG", "AUFK.ERDAT"],
        "where_template": "AUFK.ERDAT BETWEEN '{date_from_sap}' AND '{date_to_sap}'",
        "aggregation": None,
        "group_by": [],
        "order_by": ["AUFK.ERDAT DESC"],
        "max_rows": 5000,
        "joins": [
            {
                "table": "AFKO",
                "on": "AFKO.AUFNR = AUFK.AUFNR",
                "fields": ["GAMNG"],
                "join_type": "left",
            }
        ],
        "field_mapping": {
            "AUFK.AUFNR": "生产订单号",
            "AUFK.AUART": "订单类型",
            "AUFK.WERKS": "工厂",
            "AFKO.GAMNG": "订单数量",
            "AUFK.ERDAT": "创建日期",
        },
        "keywords": [
            "生产订单", "生产情况", "本月生产", "这个月生产", "生产订单列表",
            "工单", "生产工单", "新生产订单",
            "第一季度", "Q1", "第1季度",
            "第二季度", "Q2", "第2季度",
            "第三季度", "Q3", "第3季度",
            "第四季度", "Q4", "第4季度",
        ],
        "required_context": ["date_from", "date_to"],
    },
    # -------------------------------------------------------------------------
    # 财务
    # -------------------------------------------------------------------------
    {
        "intent": "finance_receivables",
        "domain": "finance",
        "source": "table",
        "table": "BSID",
        "fields": ["KUNNR", "BUKRS", "BELNR", "GJAHR", "BUZEI", "DMBTR", "BUDAT"],
        "where_template": "BUDAT BETWEEN '{date_from_sap}' AND '{date_to_sap}'",
        "aggregation": None,
        "group_by": [],
        "order_by": ["BUDAT DESC"],
        "max_rows": 5000,
        "field_mapping": {
            "KUNNR": "客户编码",
            "BUKRS": "公司代码",
            "BELNR": "凭证号",
            "GJAHR": "会计年度",
            "BUZEI": "行项目",
            "DMBTR": "金额",
            "BUDAT": "过账日期",
        },
        "keywords": [
            "应收账款", "客户未清项", "应收款", "客户欠款", "未清客户账款",
            "第一季度", "Q1", "第1季度",
            "第二季度", "Q2", "第2季度",
            "第三季度", "Q3", "第3季度",
            "第四季度", "Q4", "第4季度",
        ],
        "required_context": ["date_from", "date_to"],
    },
    {
        "intent": "customer_payment_history",
        "domain": "finance",
        "source": "table",
        "table": "BSAD",
        "fields": ["KUNNR", "BUKRS", "BELNR", "GJAHR", "BUZEI", "DMBTR", "BUDAT", "AUGDT", "AUGBL"],
        "where_template": "BUDAT BETWEEN '{date_from_sap}' AND '{date_to_sap}'",
        "aggregation": None,
        "group_by": [],
        "order_by": ["BUDAT DESC"],
        "max_rows": 5000,
        "field_mapping": {
            "KUNNR": "客户编码",
            "BUKRS": "公司代码",
            "BELNR": "凭证号",
            "GJAHR": "会计年度",
            "BUZEI": "行项目",
            "DMBTR": "金额",
            "BUDAT": "过账日期",
            "AUGDT": "清账日期",
            "AUGBL": "清账凭证号",
        },
        "keywords": [
            "付款历史", "客户付款", "客户已清项", "客户清账", "清账历史",
            "第一季度", "Q1", "第1季度",
            "第二季度", "Q2", "第2季度",
            "第三季度", "Q3", "第3季度",
            "第四季度", "Q4", "第4季度",
        ],
        "required_context": ["date_from", "date_to"],
    },
    {
        "intent": "finance_payables",
        "domain": "finance",
        "source": "table",
        "table": "BSIK",
        "fields": ["LIFNR", "BUKRS", "BELNR", "GJAHR", "BUZEI", "DMBTR", "BUDAT"],
        "where_template": "BUDAT BETWEEN '{date_from_sap}' AND '{date_to_sap}'",
        "aggregation": None,
        "group_by": [],
        "order_by": ["BUDAT DESC"],
        "max_rows": 5000,
        "field_mapping": {
            "LIFNR": "供应商编码",
            "BUKRS": "公司代码",
            "BELNR": "凭证号",
            "GJAHR": "会计年度",
            "BUZEI": "行项目",
            "DMBTR": "金额",
            "BUDAT": "过账日期",
        },
        "keywords": [
            "应付账款", "供应商未清项", "应付款", "供应商欠款", "未清供应商账款",
            "第一季度", "Q1", "第1季度",
            "第二季度", "Q2", "第2季度",
            "第三季度", "Q3", "第3季度",
            "第四季度", "Q4", "第4季度",
        ],
        "required_context": ["date_from", "date_to"],
    },
    {
        "intent": "finance_gl_accounts",
        "domain": "finance",
        "source": "table",
        "table": "SKA1",
        "fields": ["KTOPL", "SAKNR", "TXT20"],
        "where_template": "",
        "aggregation": None,
        "group_by": [],
        "order_by": ["SAKNR"],
        "max_rows": 5000,
        "field_mapping": {
            "KTOPL": "科目表",
            "SAKNR": "总账科目号",
            "TXT20": "科目短文本",
        },
        "keywords": [
            "总账科目", "科目表", "会计科目", "科目列表",
        ],
        "required_context": [],
    },
]

# =============================================================================
# 从 catalog 自动生成兜底 V1 模板
# =============================================================================
def _build_catalog_templates(catalog: Dict[str, Any]) -> List[Dict[str, Any]]:
    """为 catalog 中每个表/BAPI 生成基础 V1 兜底模板。

    显式定义的 V1_TEMPLATES 优先；这里只补充没有显式模板的 API。
    """
    existing_tables = set()
    existing_bapis = set()
    for tpl in V1_TEMPLATES:
        if tpl.get("source") == "bapi":
            existing_bapis.add(tpl.get("bapi_name", "").upper())
        else:
            existing_tables.add(tpl.get("table", "").upper())

    templates: List[Dict[str, Any]] = []
    for api_key, meta in catalog.get("apis", {}).items():
        api_type = meta.get("type", "table")
        domain = meta.get("domain", "master_data")
        keywords = meta.get("keywords", [])
        if not keywords:
            continue

        if api_type == "table":
            if api_key.upper() in existing_tables:
                continue
            date_field = meta.get("date_field", "")
            cc_field = meta.get("company_code_field", "")
            fields = meta.get("default_fields", list(meta.get("fields", {}).keys())) or []
            where_template = ""
            required_context: List[str] = []
            if date_field and cc_field:
                where_template = f"{cc_field} = '{{company_code}}' AND {date_field} BETWEEN '{{date_from_sap}}' AND '{{date_to_sap}}'"
                required_context = ["company_code", "date_from", "date_to"]
            elif date_field:
                where_template = f"{date_field} BETWEEN '{{date_from_sap}}' AND '{{date_to_sap}}'"
                required_context = ["date_from", "date_to"]
            elif cc_field:
                where_template = f"{cc_field} = '{{company_code}}'"
                required_context = ["company_code"]

            templates.append(
                {
                    "intent": f"catalog_{api_key.lower()}_query",
                    "domain": domain,
                    "source": "table",
                    "table": api_key.upper(),
                    "fields": fields,
                    "where_template": where_template,
                    "aggregation": None,
                    "group_by": [],
                    "order_by": [fields[0]] if fields else [],
                    "max_rows": 5000,
                    "field_mapping": meta.get("fields", {}),
                    "keywords": keywords,
                    "required_context": required_context,
                }
            )
        elif api_type == "bapi":
            if api_key.upper() in existing_bapis:
                continue
            params = meta.get("parameters", {})
            bapi_params: Dict[str, Any] = {}
            required_context = []
            for pk, pv in params.items():
                if not pv.get("required", False):
                    continue
                ptype = pv.get("type", "")
                if ptype == "date":
                    bapi_params[pk] = "{date_to_sap}"
                    if "date_to" not in required_context:
                        required_context.append("date_to")
                elif pk.upper() in ("COMPANYCODE",):
                    bapi_params[pk] = "{company_code}"
                    if "company_code" not in required_context:
                        required_context.append("company_code")
                elif ptype == "alpha":
                    bapi_params[pk] = ""
                else:
                    bapi_params[pk] = ""

            templates.append(
                {
                    "intent": f"catalog_{api_key.lower()}_query",
                    "domain": domain,
                    "source": "bapi",
                    "table": "",
                    "fields": meta.get("default_fields", list(meta.get("field_mapping", {}).keys())) or [],
                    "where_template": "",
                    "aggregation": None,
                    "group_by": [],
                    "order_by": [],
                    "max_rows": 5000,
                    "field_mapping": meta.get("field_mapping", {}),
                    "keywords": keywords,
                    "required_context": required_context,
                    "bapi_name": api_key.upper(),
                    "bapi_parameters": bapi_params,
                    "bapi_output_table_path": meta.get("output_path", ""),
                }
            )
    return templates


_CATALOG_TEMPLATES = _build_catalog_templates(_CATALOG)

# 合并显式模板与 catalog 兜底模板；显式模板优先级更高
V1_TEMPLATE_INDEX: Dict[str, Dict[str, Any]] = {}
for _tpl in V1_TEMPLATES + _CATALOG_TEMPLATES:
    V1_TEMPLATE_INDEX[_tpl["intent"]] = _tpl


def get_template_by_intent(intent: str) -> Optional[Dict[str, Any]]:
    """根据意图标识获取 V1 模板。"""
    return V1_TEMPLATE_INDEX.get(intent)


def list_enabled_intents() -> List[str]:
    """返回所有已注册的 V1 意图标识。"""
    return list(V1_TEMPLATE_INDEX.keys())
