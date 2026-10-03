"""自然语言 → SAP 查询计划（QueryPlan）。

采用「V2 LLM 优先 + V1 模板兜底」的两层策略：
1. V2：先调用 skills/sap-integration/scripts/query_planner.py，由 LLM 理解语义并
   生成查询计划；
2. V1：当 V2 失败或置信度不足时，回退到 config.V1_TEMPLATES 的关键词模板匹配。

最终校验层（PermissionGuard / FetchExecutor）会对表、字段、BAPI、WHERE 条件做统一检查，
不直接信任 LLM 或技能脚本的输出。
"""

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from common.log import logger

from .config import V1_TEMPLATES, get_catalog, get_template_by_intent


def _build_semantic_rules() -> List[tuple]:
    """从 catalog 的 keywords 自动生成语义兜底规则。"""
    rules: List[tuple] = []
    existing_intents = {tpl.get("intent") for tpl in V1_TEMPLATES}
    for api_key, meta in get_catalog().get("apis", {}).items():
        intent = f"catalog_{api_key.lower()}_query"
        if intent in existing_intents:
            continue
        keywords = meta.get("keywords", [])
        # 取长度 >= 4 的关键词作为 must_have，提升匹配精度
        must_have = [kw for kw in keywords if len(kw) >= 4]
        if not must_have:
            continue
        rules.append((intent, must_have, [], 1))
    return rules


@dataclass
class JoinPlan:
    """多表关联计划。"""

    table: str
    on: str
    fields: List[str] = field(default_factory=list)
    where: str = ""
    join_type: str = "left"


@dataclass
class QueryPlan:
    """SAP 查询计划。"""

    intent: str
    domain: str
    source: str  # table | multi_table | bapi
    table: str = ""
    fields: List[str] = field(default_factory=list)
    where: str = ""
    aggregation: Optional[str] = None
    group_by: List[str] = field(default_factory=list)
    order_by: List[str] = field(default_factory=list)
    max_rows: int = 5000
    top_n: int = 0  # 最终返回前 N 条；0 表示不限制
    field_mapping: Dict[str, str] = field(default_factory=dict)
    joins: List[JoinPlan] = field(default_factory=list)
    bapi_name: str = ""
    bapi_parameters: Dict[str, Any] = field(default_factory=dict)
    bapi_output_table_path: str = ""  # BAPI 结果中需要取出的内表路径
    pre_query: Optional[Dict[str, Any]] = None  # 前置查询（BAPI 结果作为本查询输入）
    confidence: float = 1.0  # V1 为 1.0，V2 由技能/LLM 返回

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "domain": self.domain,
            "source": self.source,
            "table": self.table,
            "fields": self.fields,
            "where": self.where,
            "aggregation": self.aggregation,
            "group_by": self.group_by,
            "order_by": self.order_by,
            "max_rows": self.max_rows,
            "top_n": self.top_n,
            "field_mapping": self.field_mapping,
            "joins": [
                {
                    "table": j.table,
                    "on": j.on,
                    "fields": j.fields,
                    "where": j.where,
                    "join_type": j.join_type,
                }
                for j in self.joins
            ],
            "bapi_name": self.bapi_name,
            "bapi_parameters": self.bapi_parameters,
            "bapi_output_table_path": self.bapi_output_table_path,
            "pre_query": self.pre_query,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QueryPlan":
        joins = [
            JoinPlan(
                table=j.get("table", ""),
                on=j.get("on", ""),
                fields=j.get("fields", []),
                where=j.get("where", ""),
                join_type=j.get("join_type", "left"),
            )
            for j in data.get("joins", [])
        ]
        return cls(
            intent=data.get("intent", ""),
            domain=data.get("domain", ""),
            source=data.get("source", "table"),
            table=data.get("table", ""),
            fields=data.get("fields", []),
            where=data.get("where", ""),
            aggregation=data.get("aggregation"),
            group_by=data.get("group_by", []),
            order_by=data.get("order_by", []),
            max_rows=data.get("max_rows", 5000),
            top_n=data.get("top_n", 0),
            pre_query=data.get("pre_query"),
            field_mapping=data.get("field_mapping", {}),
            joins=joins,
            bapi_name=data.get("bapi_name", ""),
            bapi_parameters=data.get("bapi_parameters", {}),
            bapi_output_table_path=data.get("bapi_output_table_path", ""),
            confidence=data.get("confidence", 0.8),
        )


class QueryPlanner:
    """自然语言查询计划生成器。"""

    def __init__(self, project_root: Optional[str] = None):
        if project_root is None:
            project_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
        self.project_root = project_root
        self.skill_planner_path = os.path.join(
            project_root, "skills", "sap-integration", "scripts", "query_planner.py"
        )

    def plan(
        self,
        question: str,
        context: Dict[str, Any],
        available_tables: Optional[List[str]] = None,
        last_context: Optional[Dict[str, Any]] = None,
    ) -> QueryPlan:
        """生成查询计划。

        策略：
        1. catalog 语义匹配（基于 keywords + 实体编码），覆盖大部分标准查询；
        2. V2 LLM 处理复杂/多表/兜底场景；
        3. 旧 V1 模板兜底。
        """
        if context is None:
            context = {}
        if last_context:
            self._inherit_context(question, context, last_context)
        self._extract_entity_codes(question, context)
        context = self._infer_date_context(question.lower(), context)

        # 1) catalog 语义匹配
        logger.info("[QueryPlanner] trying catalog semantic match")
        plan = self._catalog_based_plan(question, context, available_tables)
        if plan:
            logger.info(f"[QueryPlanner] catalog matched intent={plan.intent}")
            return plan

        # 2) V2 LLM
        logger.info("[QueryPlanner] trying V2 LLM plan")
        plan = self._v2_plan(question, context, available_tables)
        if plan and plan.confidence >= 0.5:
            logger.info(f"[QueryPlanner] V2 matched intent={plan.intent}")
            self._append_entity_conditions_to_plan(plan, context)
            return plan

        # 3) 旧 V1 模板兜底
        logger.info("[QueryPlanner] V2 no confident match, falling back to V1")
        plan = self._v1_plan(question, context)
        if plan:
            logger.info(f"[QueryPlanner] V1 matched intent={plan.intent}")
            self._append_entity_conditions_to_plan(plan, context)
            return plan

        raise ValueError(
            "未能识别您的查询意图。请尝试使用更明确的关键词，例如："
            "'本月新增客户'、'本月销售额'、'本月采购金额'、'库存情况'、"
            "'本月生产订单'、'生产订单明细'、'应收账款'、'应付账款'、'总账科目'。"
        )

    # -------------------------------------------------------------------------
    # V1：模板匹配
    # -------------------------------------------------------------------------
    # 语义意图规则：独立于关键词的兜底匹配
    _SEMANTIC_RULES = [
        # 意图标识, 必须包含, 不能包含, 优先级分数
        ("new_customers_this_month", ["新增", "客户"], [], 2),
        ("customer_list", ["客户"], ["新增", "销售", "采购", "订单", "发票", "信用", "付款"], 1),
        ("customer_credit_limit", ["客户", "信用"], [], 2),
        ("customer_payment_history", ["客户", "付款"], [], 2),
        ("new_suppliers_this_month", ["新增", "供应商"], [], 2),
        ("sales_orders_this_month", ["销售订单"], [], 2),
        ("sales_amount_this_month", ["销售额", "销售金额", "销售收入"], [], 2),
        ("purchase_orders_this_month", ["采购订单"], [], 2),
        ("purchase_amount_this_month", ["采购金额", "采购额"], [], 2),
        ("inventory_overview", ["库存"], [], 2),
        ("material_inventory", ["物料", "库存"], [], 3),
        ("finance_receivables", ["应收", "客户未清"], [], 2),
        ("finance_payables", ["应付", "供应商未清"], [], 2),
        ("finance_gl_accounts", ["总账科目", "科目表", "会计科目"], [], 2),
        ("finance_overview", ["财务", "凭证"], ["应收", "应付", "科目"], 2),
        ("production_order_items", ["生产订单明细", "生产订单行项目", "工单明细"], [], 3),
        ("production_order_schedule", ["生产订单进度", "生产订单排程", "生产订单计划", "生产订单完成情况"], [], 3),
        ("production_orders_this_month", ["生产订单", "生产情况", "工单"], ["明细", "行项目", "进度", "排程", "计划", "完成"], 2),
    ] + _build_semantic_rules()

    def _v1_plan(self, question: str, context: Dict[str, Any]) -> Optional[QueryPlan]:
        question_lower = question.lower()
        best_tpl: Optional[Dict[str, Any]] = None
        best_score = 0

        for tpl in V1_TEMPLATES:
            score = 0
            for kw in tpl.get("keywords", []):
                if kw.lower() in question_lower:
                    score += 1
            if score > best_score:
                best_score = score
                best_tpl = tpl

        # 关键词没命中时，用语义规则兜底
        if not best_tpl or best_score == 0:
            semantic_intent = self._match_semantic_intent(question_lower)
            if semantic_intent:
                tpl = get_template_by_intent(semantic_intent)
                if tpl:
                    best_tpl = tpl
                    best_score = 1

        if not best_tpl or best_score == 0:
            return None

        # 根据自然语言推导/补全日期范围
        context = self._infer_date_context(question_lower, context)
        return self._build_plan_from_template(best_tpl, context)

    def _match_semantic_intent(self, question_lower: str) -> Optional[str]:
        best_intent = None
        best_score = 0
        for intent, must_have, must_not, score in self._SEMANTIC_RULES:
            if any(kw not in question_lower for kw in must_have):
                continue
            if any(kw in question_lower for kw in must_not):
                continue
            if score > best_score:
                best_score = score
                best_intent = intent
        return best_intent

    def _match_semantic_intent_with_score(self, question_lower: str) -> tuple[Optional[str], int]:
        best_intent = None
        best_score = 0
        for intent, must_have, must_not, score in self._SEMANTIC_RULES:
            if any(kw not in question_lower for kw in must_have):
                continue
            if any(kw in question_lower for kw in must_not):
                continue
            if score > best_score:
                best_score = score
                best_intent = intent
        return best_intent, best_score

    # -------------------------------------------------------------------------
    # Catalog 语义匹配（核心改造：用 catalog keywords + 实体编码自动生成计划）
    # -------------------------------------------------------------------------
    def _catalog_based_plan(
        self,
        question: str,
        context: Dict[str, Any],
        available_tables: Optional[List[str]] = None,
    ) -> Optional[QueryPlan]:
        """基于 sap_query_catalog.json 的 keywords 和实体编码自动匹配并生成计划。"""
        catalog = get_catalog()
        if not catalog:
            return None

        question_lower = question.lower()
        apis = catalog.get("apis", {})
        if not apis:
            return None

        scored: List[tuple[str, Dict[str, Any], float]] = []
        for api_key, meta in apis.items():
            score = self._score_api_match(api_key, meta, question_lower, context)
            if score > 0:
                scored.append((api_key, meta, score))

        if not scored:
            return None

        scored.sort(key=lambda x: x[2], reverse=True)
        best_key, best_meta, best_score = scored[0]
        second_score = scored[1][2] if len(scored) > 1 else 0

        # 阈值策略：最高分要够高，且与第二名拉开差距
        if best_score < 8 or (second_score > 0 and best_score - second_score < 3):
            return None

        return self._build_plan_from_catalog(best_key, best_meta, context)

    _DOMAIN_BOOST_KEYWORDS = {
        "inventory": ["库存", "存货", "仓储", "仓库"],
        "sales": ["销售", "卖出", "订单", "发票"],
        "procurement": ["采购", "买入", "供应商"],
        "finance": ["财务", "会计", "凭证", "应收", "应付", "账款"],
        "production": ["生产", "工单", "制造"],
        "master_data": ["主数据", "客户", "供应商", "物料主数据"],
    }

    def _score_api_match(
        self, api_key: str, meta: Dict[str, Any], question_lower: str, context: Dict[str, Any]
    ) -> float:
        """给 catalog 中的每个 API 打分。"""
        score = 0.0
        api_type = meta.get("type", "table")
        domain = meta.get("domain", "master_data")

        # 1. keywords 匹配（命中即 +5，长关键词额外加分）
        for kw in meta.get("keywords", []):
            kw_lower = kw.lower()
            if kw_lower in question_lower:
                score += 5 + len(kw_lower)

        # 2. 领域 boost：问题中的领域词与 API domain 匹配
        for boost_kw in self._DOMAIN_BOOST_KEYWORDS.get(domain, []):
            if boost_kw.lower() in question_lower:
                score += 10

        # 3. 实体编码与表/BAPI 的匹配
        if api_type == "table":
            for cf in self._get_table_code_fields(api_key):
                ctx_key = self._code_field_to_context_key(cf)
                if ctx_key and context.get(ctx_key):
                    score += 15
        elif api_type == "bapi":
            for pk, pv in meta.get("parameters", {}).items():
                if pv.get("type") == "alpha":
                    ctx_key = self._bapi_param_to_context_key(pk)
                    if ctx_key and context.get(ctx_key):
                        score += 15

        # 4. 表/BAPI 名称本身出现在问题中
        if api_key.lower() in question_lower:
            score += 10

        return score

    def _build_plan_from_catalog(
        self, api_key: str, meta: Dict[str, Any], context: Dict[str, Any]
    ) -> QueryPlan:
        """根据 catalog 元数据构造 QueryPlan。"""
        api_type = meta.get("type", "table")
        domain = meta.get("domain", "master_data")
        intent = f"catalog_{api_key.lower()}"
        fields = list(meta.get("default_fields", list(meta.get("fields", {}).keys())) or [])

        if api_type == "bapi":
            parameters = self._build_bapi_parameters(meta, context)
            return QueryPlan(
                intent=intent,
                domain=domain,
                source="bapi",
                table="",
                fields=fields,
                where="",
                field_mapping=meta.get("field_mapping", {}),
                bapi_name=api_key.upper(),
                bapi_parameters=parameters,
                bapi_output_table_path=meta.get("output_path", ""),
                max_rows=min(int(context.get("max_rows", 5000)), 10000),
            )

        # table 查询
        where = self._build_where_from_catalog(api_key, meta, context)
        order_by = []
        date_field = meta.get("date_field", "")
        if date_field:
            order_by.append(f"{date_field} DESC")
        elif fields:
            order_by.append(fields[0])

        return QueryPlan(
            intent=intent,
            domain=domain,
            source="table",
            table=api_key.upper(),
            fields=fields,
            where=where,
            field_mapping=meta.get("fields", {}),
            order_by=order_by,
            max_rows=min(int(context.get("max_rows", 5000)), 10000),
        )

    def _build_where_from_catalog(
        self, table: str, meta: Dict[str, Any], context: Dict[str, Any]
    ) -> str:
        """根据 catalog 元数据和 context 生成 WHERE 条件。"""
        conditions: List[str] = []
        date_field = meta.get("date_field", "")
        cc_field = meta.get("company_code_field", "")
        date_from_sap = context.get("date_from_sap", "")
        date_to_sap = context.get("date_to_sap", "")

        # 实体编码条件
        for cf in self._get_table_code_fields(table):
            ctx_key = self._code_field_to_context_key(cf)
            val = context.get(ctx_key) if ctx_key else None
            if val:
                conditions.append(f"{cf} = '{self._escape(val)}'")
                break

        # 日期范围
        if date_field and date_from_sap and date_to_sap:
            conditions.append(f"{date_field} BETWEEN '{date_from_sap}' AND '{date_to_sap}'")

        # 公司代码
        company_code = context.get("company_code", "")
        if cc_field and company_code:
            if not any(cf == cc_field for cf in self._get_table_code_fields(table)):
                conditions.append(f"{cc_field} = '{self._escape(company_code)}'")

        return " AND ".join(conditions)

    def _build_bapi_parameters(
        self, meta: Dict[str, Any], context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """根据 catalog BAPI 参数定义和 context 生成参数。"""
        parameters: Dict[str, Any] = {}
        for pk, pv in meta.get("parameters", {}).items():
            ptype = pv.get("type", "")
            ctx_key = self._bapi_param_to_context_key(pk)
            if ctx_key and context.get(ctx_key):
                val = context[ctx_key]
                if ptype == "date":
                    val = str(val).replace("-", "")
                parameters[pk] = val
            elif pv.get("required") and pv.get("default") is not None:
                parameters[pk] = pv["default"]
        return parameters

    def _get_table_code_fields(self, table: str) -> List[str]:
        """返回某张表适合的实体编码字段。"""
        mapping = {
            "KNA1": ["KUNNR"], "KNKK": ["KUNNR"], "BSID": ["KUNNR"], "BSAD": ["KUNNR"], "MSLB": ["KUNNR"],
            "LFA1": ["LIFNR"], "EKKO": ["LIFNR"], "EKPO": ["LIFNR", "EBELN"], "BSIK": ["LIFNR"], "BSAK": ["LIFNR"], "MKOL": ["LIFNR"],
            "MARA": ["MATNR"], "MAKT": ["MATNR"], "MARD": ["MATNR"], "MCHB": ["MATNR"],
            "VBAK": ["VBELN"], "VBRK": ["VBELN"], "VBAP": ["VBELN", "MATNR"],
            "EBAN": ["EBELN"],
            "AUFK": ["AUFNR"], "AFKO": ["AUFNR"], "AFPO": ["AUFNR"],
            "BKPF": ["BUKRS"], "BSEG": ["BUKRS"], "BSIS": ["BUKRS"], "BSAS": ["BUKRS"],
            "T001W": ["WERKS"], "T001L": ["WERKS"], "MARD": ["MATNR", "WERKS"],
        }
        return mapping.get(table.upper(), [])

    def _code_field_to_context_key(self, field: str) -> Optional[str]:
        mapping = {
            "KUNNR": "kunnr", "LIFNR": "lifnr", "MATNR": "matnr",
            "VBELN": "vbeln", "EBELN": "ebeln", "AUFNR": "aufnr",
            "BELNR": "belnr", "BUKRS": "company_code",
            "WERKS": "werks",
        }
        return mapping.get(field.upper())

    def _bapi_param_to_context_key(self, param: str) -> Optional[str]:
        mapping = {
            "CUSTOMER": "kunnr", "CUSTOMERNO": "kunnr", "CUSTOMER_NUMBER": "kunnr",
            "VENDOR": "lifnr", "VENDORNO": "lifnr",
            "MATERIAL": "matnr",
            "SALESDOCUMENT": "vbeln",
            "PURCHASEORDER": "ebeln",
            "COMPANYCODE": "company_code",
        }
        return mapping.get(param.upper())

    def _infer_date_context(
        self, question_lower: str, context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """根据问题中的时间词推导日期范围，并补充到 context。"""
        today = datetime.now()
        result = dict(context)

        def _ensure_from_to(date_from: str, date_to: str):
            result["date_from"] = result.get("date_from") or date_from
            result["date_to"] = result.get("date_to") or date_to

        # 具体季度（第一季度 / Q1 / 去年Q1 等），优先级高于"本季度/上季度"和"今年"
        if self._match_specific_quarter(question_lower, today, result):
            pass

        # 今年/本年度/Year-to-date
        elif any(kw in question_lower for kw in ["今年", "本年度", "ytd", "year to date"]):
            start = today.replace(month=1, day=1)
            _ensure_from_to(start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))

        # 上半年 / 下半年
        elif any(kw in question_lower for kw in ["上半年", "前半"]):
            _ensure_from_to(today.strftime("%Y") + "-01-01", today.strftime("%Y") + "-06-30")
        elif any(kw in question_lower for kw in ["下半年", "后半"]):
            _ensure_from_to(today.strftime("%Y") + "-07-01", today.strftime("%Y") + "-12-31")

        # 去年
        elif any(kw in question_lower for kw in ["去年", "上一年", "last year"]):
            last_year = today.year - 1
            _ensure_from_to(f"{last_year}-01-01", f"{last_year}-12-31")

        # 具体年份，如 2025年、2025年同期
        elif self._match_specific_year(question_lower, today, result):
            pass

        # 去年同期/同比（未指定年份时，默认去年同期）
        elif any(kw in question_lower for kw in ["同期", "同比"]):
            last_year = today.year - 1
            result["date_from"] = f"{last_year}-01-01"
            result["date_to"] = today.strftime("%Y-%m-%d")

        # 近 N 个月 / 近 N 天
        elif self._match_recent_period(question_lower, today, _ensure_from_to):
            pass

        # 这几个月（模糊表达，默认近 3 个月）
        elif "这几个月" in question_lower:
            start = today
            for _ in range(3):
                start = (start.replace(day=1) - timedelta(days=1)).replace(day=1)
            _ensure_from_to(start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d"))

        # 本月
        elif any(kw in question_lower for kw in ["本月", "这个月", "当月"]):
            start = today.replace(day=1)
            next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
            end = next_month - timedelta(days=1)
            _ensure_from_to(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))

        # 上月
        elif any(kw in question_lower for kw in ["上个月", "上月", "last month"]):
            first_day_of_this_month = today.replace(day=1)
            end = first_day_of_this_month - timedelta(days=1)
            start = end.replace(day=1)
            _ensure_from_to(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))

        # 本季度
        elif any(kw in question_lower for kw in ["本季度", "这个季度", "当季"]):
            quarter = (today.month - 1) // 3
            start = today.replace(month=quarter * 3 + 1, day=1)
            if quarter < 3:
                next_q = today.replace(month=quarter * 3 + 4, day=1)
            else:
                next_q = today.replace(year=today.year + 1, month=1, day=1)
            end = next_q - timedelta(days=1)
            _ensure_from_to(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))

        # 上季度
        elif any(kw in question_lower for kw in ["上季度", "上个季度", "last quarter"]):
            quarter = (today.month - 1) // 3
            this_q_start = today.replace(month=quarter * 3 + 1, day=1)
            end = this_q_start - timedelta(days=1)
            start = end.replace(day=1, month=end.month - 2 if end.month > 2 else end.month + 10)
            if quarter == 0:
                start = start.replace(year=today.year - 1)
            _ensure_from_to(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))

        # 截止目前/累计/全部
        elif any(kw in question_lower for kw in ["截止目前", "截至目前", "累计", "总共", "所有", "全部"]):
            # 留空让后端查询全部；但 max_rows 默认已存在
            result.setdefault("date_from", "")
            result.setdefault("date_to", "")

        # 如果都没有，但用户也没填日期，且问题不是库存/客户列表，默认本月
        elif not result.get("date_from") and not result.get("date_to"):
            templates_with_default_month = {
                "new_customers_this_month",
                "new_suppliers_this_month",
                "sales_orders_this_month",
                "sales_amount_this_month",
                "top_customers_by_sales",
                "purchase_orders_this_month",
                "purchase_amount_this_month",
                "top_suppliers_by_purchase",
                "finance_overview",
                "finance_receivables",
                "finance_payables",
                "production_orders_this_month",
                "production_order_items",
                "production_order_schedule",
            }
            # 不在此集合的意图（如 inventory_overview、customer_list）不默认本月

        return result

    def _match_specific_quarter(
        self,
        question_lower: str,
        today: datetime,
        result: Dict[str, Any],
    ) -> bool:
        """处理 '第一季度 / Q1 / 去年Q1 / 去年第一季度' 等具体季度表达。"""
        quarter_map = {
            "第一季度": 1, "q1": 1, "第1季度": 1,
            "第二季度": 2, "q2": 2, "第2季度": 2,
            "第三季度": 3, "q3": 3, "第3季度": 3,
            "第四季度": 4, "q4": 4, "第4季度": 4,
        }
        quarter = None
        for kw, q in quarter_map.items():
            if kw in question_lower:
                quarter = q
                break
        if quarter is None:
            return False

        year = today.year
        if any(kw in question_lower for kw in ["去年", "上一年", "上年度"]):
            year -= 1

        start_month = (quarter - 1) * 3 + 1
        end_month = start_month + 2
        start = datetime(year, start_month, 1)
        if end_month == 12:
            end = datetime(year, 12, 31)
        else:
            end = (datetime(year, end_month + 1, 1) - timedelta(days=1))

        result["date_from"] = start.strftime("%Y-%m-%d")
        result["date_to"] = end.strftime("%Y-%m-%d")
        return True

    def _match_specific_year(
        self,
        question_lower: str,
        today: datetime,
        result: Dict[str, Any],
    ) -> bool:
        """处理 '2025年'、'2025年同期' 等具体年份表达，会强制覆盖已有日期。"""
        import re

        match = re.search(r"(?<!\d)(19|20)(\d{2})(?!\d)", question_lower)
        if not match:
            return False
        year = int(match.group(1) + match.group(2))
        if "同期" in question_lower:
            result["date_from"] = f"{year}-01-01"
            result["date_to"] = today.strftime("%Y-%m-%d")
        else:
            result["date_from"] = f"{year}-01-01"
            result["date_to"] = f"{year}-12-31"
        return True

    def _match_recent_period(
        self,
        question_lower: str,
        today: datetime,
        _ensure_from_to,
    ) -> bool:
        """处理 '近 N 个月/天' 这类相对时间表达。"""
        cn_numbers = {
            "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
        }

        def _parse_number(s: str) -> int:
            s = s.strip()
            if s.isdigit():
                return int(s)
            total = 0
            for ch in s:
                if ch in cn_numbers:
                    total += cn_numbers[ch]
            return total if total > 0 else 1

        match = re.search(r"(?:近|最近)\s*([一二两三四五六七八九十\d]+|半)\s*个?\s*(月|天|日|年)", question_lower)
        if not match:
            return False

        raw_num = match.group(1)
        unit = match.group(2)
        if raw_num == "半":
            # 近半年统一按 6 个月处理
            num = 6
            unit = "月"
        else:
            num = _parse_number(raw_num)

        end = today
        if unit == "月":
            # 近 N 个月：从今天往前推 N 个月（含今天）
            month = today.month - num
            year = today.year
            while month <= 0:
                month += 12
                year -= 1
            try:
                start = today.replace(year=year, month=month)
            except ValueError:
                # 目标月份没有今天这一天，取该月最后一天
                next_month = (today.replace(year=year, month=month, day=1) + timedelta(days=32)).replace(day=1)
                start = next_month - timedelta(days=1)
            _ensure_from_to(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        elif unit == "年":
            # 近 N 年：从今天往前推 N 年（含今天）
            try:
                start = today.replace(year=today.year - num)
            except ValueError:
                start = today.replace(year=today.year - num, month=2, day=28)
            _ensure_from_to(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        else:
            # 近 N 天/日：从今天往前推 N-1 天（含今天）
            start = today - timedelta(days=num - 1)
            _ensure_from_to(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))

        return True

    def _build_plan_from_template(
        self, tpl: Dict[str, Any], context: Dict[str, Any]
    ) -> QueryPlan:
        """根据模板和上下文构造 QueryPlan，主要是渲染 WHERE 条件。"""
        date_from = context.get("date_from", "")
        date_to = context.get("date_to", "")
        company_code = context.get("company_code", "")

        date_from_sap = date_from.replace("-", "") if date_from else ""
        date_to_sap = date_to.replace("-", "") if date_to else ""

        where_template = tpl.get("where_template", "")
        # 如果日期范围推导后为空，且模板依赖 BETWEEN 日期条件，退化为空 WHERE
        if not date_from_sap and not date_to_sap and "between" in where_template.lower():
            where = ""
        else:
            where = where_template.format(
                date_from_sap=date_from_sap,
                date_to_sap=date_to_sap,
                company_code=company_code,
                date_from=date_from,
                date_to=date_to,
            )

        # 如果模板未包含公司代码过滤，但用户填写了，则强制追加
        if company_code and "company_code" not in tpl.get("required_context", []):
            company_condition = f"BUKRS = '{self._escape(company_code)}'"
            if where:
                where = f"{where} AND {company_condition}"
            else:
                where = company_condition

        max_rows = min(
            int(context.get("max_rows", tpl.get("max_rows", 5000))),
            10000,
        )

        # catalog 自动生成的 BAPI 模板，参数需要渲染上下文变量
        bapi_parameters = dict(tpl.get("bapi_parameters", {}))
        for key, value in bapi_parameters.items():
            if isinstance(value, str):
                bapi_parameters[key] = value.format(
                    date_from_sap=date_from_sap,
                    date_to_sap=date_to_sap,
                    company_code=company_code,
                    date_from=date_from,
                    date_to=date_to,
                )

        return QueryPlan(
            intent=tpl["intent"],
            domain=tpl["domain"],
            source=tpl["source"],
            table=tpl.get("table", ""),
            fields=list(tpl.get("fields", [])),
            where=where,
            aggregation=tpl.get("aggregation"),
            group_by=list(tpl.get("group_by", [])),
            order_by=list(tpl.get("order_by", [])),
            max_rows=max_rows,
            field_mapping=dict(tpl.get("field_mapping", {})),
            joins=[
                JoinPlan(
                    table=j.get("table", ""),
                    on=j.get("on", ""),
                    fields=j.get("fields", []),
                    where=j.get("where", ""),
                    join_type=j.get("join_type", "left"),
                )
                for j in tpl.get("joins", [])
            ],
            bapi_name=tpl.get("bapi_name", ""),
            bapi_parameters=bapi_parameters,
            bapi_output_table_path=tpl.get("bapi_output_table_path", ""),
            confidence=1.0,
        )

    # -------------------------------------------------------------------------
    # 多轮上下文与实体编码继承
    # -------------------------------------------------------------------------
    def _is_follow_up(self, question: str) -> bool:
        """根据问题长度和关键词判断是否为 follow-up。"""
        q = question.strip()
        if len(q) < 15:
            return True
        prefixes = ["再", "还", "那", "按", "给", "查看", "看"]
        if any(q.startswith(p) for p in prefixes):
            return True
        keywords = ["刚才", "之前", "上面", "这个客户", "该客户", "这个供应商", "该供应商"]
        if any(kw in q for kw in keywords):
            return True
        return False

    def _inherit_context(
        self, question: str, context: Dict[str, Any], last_context: Dict[str, Any]
    ) -> None:
        """把上一轮上下文继承到本轮。"""
        if not self._is_follow_up(question):
            return
        inheritable = [
            "company_code", "date_from", "date_to",
            "kunnr", "lifnr", "matnr", "vbeln", "ebeln", "aufnr", "belnr",
        ]
        for key in inheritable:
            if not context.get(key) and last_context.get(key):
                context[key] = last_context[key]

    def _extract_entity_codes(self, question: str, context: Dict[str, Any]) -> None:
        """从问题中提取公司代码、客户号、供应商号、物料号、工厂等编码。"""
        if not context.get("company_code"):
            m = re.search(r"公司(?:代码)?\s*[:：]?\s*(\d{4})", question)
            if m:
                context["company_code"] = m.group(1)

        code_patterns = [
            ("kunnr", r"客户\s*(?:号|编码)?\s*[:：]?\s*(\d{1,10})", 10),
            ("kunnr", r"\b(\d{1,10})\s*号?\s*客户", 10),
            ("lifnr", r"供应商\s*(?:号|编码)?\s*[:：]?\s*(\d{1,10})", 10),
            ("matnr", r"物料\s*(?:号|编码)?\s*[:：]?\s*(\d{1,18})", 18),
            ("vbeln", r"销售订单\s*(?:号)?\s*[:：]?\s*(\d{1,10})", 10),
            ("vbeln", r"发票\s*(?:号)?\s*[:：]?\s*(\d{1,10})", 10),
            ("ebeln", r"采购订单\s*(?:号)?\s*[:：]?\s*(\d{1,10})", 10),
            ("aufnr", r"生产订单\s*(?:号)?\s*[:：]?\s*(\d{1,12})", 12),
            ("werks", r"工厂\s*[:：]?\s*(\d{1,4})", 4),
        ]
        for key, pattern, length in code_patterns:
            if context.get(key):
                continue
            m = re.search(pattern, question)
            if m:
                context[key] = m.group(1).zfill(length)

    def _append_entity_conditions_to_plan(
        self, plan: QueryPlan, context: Dict[str, Any]
    ) -> None:
        """根据 context 中的编码，自动向 plan.where 追加表级过滤条件。"""
        if not plan.table or plan.source == "bapi":
            return

        table_code_map = {
            "KNA1": ["KUNNR"], "KNKK": ["KUNNR"], "BSID": ["KUNNR"], "BSAD": ["KUNNR"], "MSLB": ["KUNNR"],
            "LFA1": ["LIFNR"], "EKKO": ["LIFNR"], "EKPO": ["LIFNR", "EBELN"], "BSIK": ["LIFNR"], "BSAK": ["LIFNR"], "MKOL": ["LIFNR"],
            "MARA": ["MATNR"], "MAKT": ["MATNR"], "MARD": ["MATNR"], "MCHB": ["MATNR"],
            "VBAK": ["VBELN"], "VBRK": ["VBELN"], "VBAP": ["VBELN", "MATNR"],
            "EBAN": ["EBELN"],
            "AUFK": ["AUFNR"], "AFKO": ["AUFNR"], "AFPO": ["AUFNR"],
            "T001W": ["WERKS"], "T001L": ["WERKS"],
        }
        code_fields = table_code_map.get(plan.table.upper())
        if not code_fields:
            return

        code_context_keys = {
            "KUNNR": "kunnr", "LIFNR": "lifnr", "MATNR": "matnr",
            "VBELN": "vbeln", "EBELN": "ebeln", "AUFNR": "aufnr",
            "WERKS": "werks",
        }

        for cf in code_fields:
            val = context.get(code_context_keys.get(cf, ""))
            if not val:
                continue
            if re.search(rf"\b{cf}\b", plan.where, re.IGNORECASE):
                continue
            condition = f"{cf} = '{self._escape(val)}'"
            if plan.where:
                plan.where = f"{plan.where} AND {condition}"
            else:
                plan.where = condition
            break

    # -------------------------------------------------------------------------
    # V2：技能脚本兜底
    # -------------------------------------------------------------------------
    def _v2_plan(
        self,
        question: str,
        context: Dict[str, Any],
        available_tables: Optional[List[str]] = None,
    ) -> Optional[QueryPlan]:
        if not os.path.isfile(self.skill_planner_path):
            logger.warning(f"[QueryPlanner] V2 skill planner not found: {self.skill_planner_path}")
            return None

        try:
            payload = {
                "question": question,
                "context": context,
                "available_tables": available_tables,
            }
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            result = subprocess.run(
                [sys.executable, self.skill_planner_path, "--json"],
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                timeout=90,
                env=env,
                encoding="utf-8",
            )
            if result.returncode != 0:
                stderr = result.stderr
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", errors="replace")
                logger.warning(f"[QueryPlanner] V2 planner error: {stderr or ''}")
                return None

            stdout = result.stdout
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            data = json.loads(stdout)
            if data.get("status") != "success" or "plan" not in data:
                return None

            plan = QueryPlan.from_dict(data["plan"])
            plan.confidence = min(plan.confidence, 0.9)
            return plan
        except Exception as e:
            logger.warning(f"[QueryPlanner] V2 planner exception: {e}")
            return None

    # -------------------------------------------------------------------------
    # 工具
    # -------------------------------------------------------------------------
    @staticmethod
    def _escape(value: str) -> str:
        """转义 Open SQL 字符串值。"""
        return value.replace("'", "''")


def merge_context_where(plan: QueryPlan, context: Dict[str, Any]) -> QueryPlan:
    """将用户填写的范围参数强制合并进 QueryPlan.where。

    这是最终校验层的一部分，确保无论 V1/V2 生成的计划如何，
    都包含用户明确指定的公司代码、日期范围等过滤条件。
    """
    conditions = []
    if plan.where:
        conditions.append(plan.where)

    company_code = context.get("company_code")
    date_from = context.get("date_from", "")
    date_to = context.get("date_to", "")

    if company_code and plan.table.upper() not in {"MARD", "MCHB", "MKOL", "MSLB"}:
        # 不同业务表的公司代码字段不同，这里做简化处理
        bukrs_field = _guess_company_code_field(plan.table)
        if bukrs_field:
            conditions.append(f"{bukrs_field} = '{company_code.replace(chr(39), chr(39)+chr(39))}'")

    if date_from and date_to and plan.table:
        date_field = _guess_date_field(plan.table)
        if date_field:
            conditions.append(
                f"{date_field} BETWEEN '{date_from.replace('-', '')}' AND '{date_to.replace('-', '')}'"
            )

    plan.where = " AND ".join(conditions)
    return plan


def _guess_company_code_field(table: str) -> str:
    """根据表名猜测公司代码字段。"""
    table_upper = table.upper()
    mapping = {
        "BKPF": "BUKRS",
        "BSEG": "BUKRS",
        "BSAK": "BUKRS",
        "BSID": "BUKRS",
        "BSAD": "BUKRS",
        "BSAS": "BUKRS",
        "BSIS": "BUKRS",
        "BSIK": "BUKRS",
        "T001": "BUKRS",
        "COEP": "BUKRS",
        "COBK": "BUKRS",
    }
    return mapping.get(table_upper, "")


def _guess_date_field(table: str) -> str:
    """根据表名猜测日期字段。"""
    table_upper = table.upper()
    mapping = {
        "KNA1": "ERDAT",
        "LFA1": "ERDAT",
        "MARA": "ERSDA",
        "VBAK": "AUDAT",
        "VBRK": "FKDAT",
        "EKKO": "BEDAT",
        "EKPO": "BEDAT",
        "BKPF": "BLDAT",
        "BSEG": "BUDAT",
        "EBAN": "BADAT",
        "AUFK": "ERDAT",
        "AFKO": "GSTRP",
        "BSID": "BUDAT",
        "BSAD": "BUDAT",
        "BSIK": "BUDAT",
        "BSAS": "BUDAT",
        "BSIS": "BUDAT",
        "BSAK": "BUDAT",
    }
    return mapping.get(table_upper, "")


def validate_where_safety(where: str) -> bool:
    """对 WHERE 条件做基础安全校验，拒绝明显危险的内容。luo手工增加"""
    if not where:
        return True
    # 禁止分号、注释、UNION 等 SQL 注入特征
    forbidden = [";", "--", "/*", "*/", "UNION", "INSERT", "UPDATE", "DELETE", "DROP"]
    upper = where.upper()
    for token in forbidden:
        if token in upper:
            return False
    return True
