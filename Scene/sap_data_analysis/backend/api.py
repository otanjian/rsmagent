"""SAP 数据分析 API Handler。

路由：
  POST /api/sap-data-analysis/analyze
  GET  /api/sap-data-analysis/{file_id}/csv
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

import web

from Scene._shared.host import (
    get_current_roles,
    get_current_tenant_id,
    get_current_username,
)
from Scene.sap_data_analysis.backend.sap import SAPProviderFactory
from Scene.sap_data_analysis.backend.sap.config import DOMAIN_PERMISSION_MAP, EXECUTIVE_DEFAULTS
from Scene.sap_data_analysis.backend.sap.data_exporter import DataExporter
from Scene.sap_data_analysis.backend.sap.fetch_executor import FetchExecutor
from Scene.sap_data_analysis.backend.sap.permission_guard import PermissionGuard
from Scene.sap_data_analysis.backend.sap.query_planner import QueryPlan, QueryPlanner, merge_context_where, validate_where_safety
from Scene.sap_data_analysis.backend.sap.dashboard_builder import (
    build_dashboard,
    build_finance_dashboard,
    build_inventory_dashboard,
    build_master_data_dashboard,
    build_procurement_dashboard,
    build_production_dashboard,
    build_sales_dashboard,
)
from Scene.sap_data_analysis.backend.sap.visualizer import _MATPLOTLIB_AVAILABLE, generate_chart
from Scene._shared.host import (
    _get_global_workspace_root,
    _require_auth,
    _require_permission,
)
from common.log import logger


class SapDataAnalysisHandler:
    """SAP 数据分析主 Handler。"""

    REQUIRED_SCENE_PERMISSION = "scenes.use.data"

    def POST(self):
        _require_auth()
        _require_permission(self.REQUIRED_SCENE_PERMISSION)
        web.header("Content-Type", "application/json; charset=utf-8")

        try:
            body = json.loads(web.data() or b"{}")
            question = (body.get("question") or "").strip()
            connection_id = body.get("connection_id", "")
            context = body.get("context") or {}
            use_defaults = bool(body.get("use_defaults", False))
            role = body.get("role", "")
            follow_up_context = body.get("follow_up_context") or {}
            query_plan_dict = body.get("query_plan") or {}
            plan_only = bool(body.get("plan_only", False))

            # 高管/微信入口：自动填充默认连接和参数
            if use_defaults:
                connection_id, context = self._apply_defaults(connection_id, context, role)

            # 聊天窗口继续分析：合并上一轮问题与当前问题
            if follow_up_context:
                prev_question = (follow_up_context.get("question") or "").strip()
                prev_plan = follow_up_context.get("query_plan") or {}
                if prev_question and prev_plan:
                    logger.info(
                        f"[SapDataAnalysisHandler] follow-up from chat: prev='{prev_question}', current='{question}'"
                    )
                    question = self._merge_follow_up_question(prev_question, question)
                    # 继承连接 ID，避免前端重复传递
                    if not connection_id:
                        connection_id = follow_up_context.get("connection_id", "")
                    # 继承上一轮数据范围参数（当前问题显式参数优先）
                    prev_context = follow_up_context.get("context") or {}
                    merged_context = dict(prev_context)
                    merged_context.update(context)
                    context = merged_context

            if not question and not query_plan_dict:
                return self._error("问题不能为空")

            # 加载并校验 SAP 连接（连接参数一律由服务端按 connection_id 解析）
            try:
                connection = self._find_connection(connection_id)
            except Exception as e:
                return self._error(
                    str(e), status=int(getattr(e, "status", 409) or 409),
                    code=getattr(e, "code", "erp_connection_unavailable"))

            # 如果前端传入了 query_plan，直接反序列化执行（工作台执行用户确认后的计划）
            if query_plan_dict:
                try:
                    plan = QueryPlan.from_dict(query_plan_dict)
                except Exception as e:
                    logger.error(f"[SapDataAnalysisHandler] invalid query_plan: {e}")
                    return self._error(f"查询计划格式错误: {e}")
            else:
                # 范围参数强制校验：至少填写一项
                if not self._has_scope_param(context) and not self._has_scope_keyword(question):
                    return self._error(
                        "请至少填写一项数据范围参数：公司代码、日期范围或最大行数"
                    )

                # 生成查询计划
                plan = self._build_plan(question, context)

            refusal = self._validate_plan(plan)
            if refusal is not None:
                return refusal

            # 仅生成计划模式：返回计划供用户确认/修改
            if plan_only:
                return json.dumps(
                    {
                        "status": "success",
                        "question": question,
                        "query_plan": plan.to_dict(),
                    },
                    ensure_ascii=False,
                )

            # 执行查询
            output_mode = body.get("output_mode", "table")
            return self._execute_plan(plan, question, connection, output_mode=output_mode)

        except ValueError as e:
            return self._error(str(e))
        except Exception as e:
            logger.exception(f"[SapDataAnalysisHandler] analyze error: {e}")
            return self._error(f"分析失败: {e}")

    def _build_plan(self, question: str, context: Dict[str, Any]) -> QueryPlan:
        """根据问题生成查询计划。"""
        planner = QueryPlanner()
        guard = PermissionGuard()
        available_tables = guard.allowed_tables()
        plan = planner.plan(question, context, available_tables=available_tables)
        # 强制合并用户范围参数到 where
        plan = merge_context_where(plan, context)
        return plan

    def _validate_plan(self, plan):
        if not validate_where_safety(plan.where):
            return self._error("查询条件包含非法字符，已被拦截")
        try:
            PermissionGuard().check_plan(plan)
        except PermissionError as exc:
            return self._error(str(exc), status=403)
        return None

    def _execute_plan(
        self,
        plan: QueryPlan,
        question: str,
        connection: Any,
        output_mode: str = "table",
    ) -> str:
        """执行查询计划并返回结果。

        采用 LLM 驱动的错误修正循环：
        每次执行失败 → 将完整错误上下文发给 LLM → LLM 分析并生成修正计划 → 重试。
        最多重试 3 次，全部由 AI 决策，不包含任何硬编码修正规则。
        """
        MAX_RETRIES = 3
        MAX_EMPTY_RETRIES = 1  # 空结果最多让 LLM 调一次，调完还是 0 就认了
        corrections: List[str] = []
        current_plan = plan
        current_question = question
        rows: List[Dict[str, Any]] = []
        empty_retries = 0

        for attempt in range(MAX_RETRIES + 1):
            refusal = self._validate_plan(current_plan)
            if refusal is not None:
                return refusal
            # 创建 SAP Provider
            try:
                provider = self._create_provider(connection)
            except Exception as e:
                logger.error(f"[SapDataAnalysisHandler] provider create error: {e}")
                return self._error(f"创建 SAP 连接失败: {e}")

            try:
                executor = FetchExecutor(provider)
                rows = executor.execute(current_plan)
            except Exception as e:
                error_str = str(e)
                logger.warning(
                    f"[SapDataAnalysisHandler] attempt {attempt + 1}/{MAX_RETRIES + 1} failed: {error_str[:200]}"
                )

                if attempt >= MAX_RETRIES:
                    corrections.append(f"[第{attempt + 1}次失败] {error_str[:200]}")
                    return self._error(
                        f"SAP 查询在 {MAX_RETRIES + 1} 次尝试后仍然失败。\n\n"
                        f"最后一次错误：{error_str}\n\n"
                        f"修正历史：\n" + "\n".join(f"  - {c}" for c in corrections)
                    )

                # LLM 驱动的错误分析与修正
                logger.info(
                    f"[SapDataAnalysisHandler] sending error to LLM for analysis and correction"
                )
                fixed_plan, correction_desc = self._llm_replan(
                    current_plan, current_question, error_str, connection
                )
                if fixed_plan is None:
                    corrections.append(
                        f"[第{attempt + 1}次失败] {error_str[:150]} → LLM 无法生成修正计划"
                    )
                    return self._error(
                        f"SAP 查询失败，LLM 也无法生成有效的修正计划。\n"
                        f"错误：{error_str}\n\n"
                        f"诊断信息：\n" + "\n".join(f"  - {c}" for c in corrections)
                    )

                corrections.append(correction_desc)
                current_plan = fixed_plan
                current_question = question
                continue
            finally:
                try:
                    provider.close()
                except Exception:
                    pass

            # 执行成功，但结果为空
            if not rows:
                if empty_retries < MAX_EMPTY_RETRIES and attempt < MAX_RETRIES:
                    # 空结果可能是因为条件太严或编码未补零，让 LLM 调一次
                    logger.info(
                        f"[SapDataAnalysisHandler] empty result (attempt {empty_retries + 1}), "
                        f"asking LLM to adjust"
                    )
                    fixed_plan, correction_desc = self._llm_replan(
                        current_plan, current_question,
                        "查询返回 0 条数据，可能是查询条件过于严格或日期/编码格式不正确",
                        connection,
                    )
                    if fixed_plan:
                        corrections.append(correction_desc)
                        current_plan = fixed_plan
                        empty_retries += 1
                        continue

                # LLM 调过了还是 0，或者 LLM 没有更好的建议 → 就当真的没数据
                corrections.append("[结果] SAP 查询返回 0 条数据")
                return json.dumps(
                    {
                        "status": "empty",
                        "question": question,
                        "summary": "SAP 查询执行成功，但未返回任何数据。\n这可能是系统中确实不存在匹配的记录。",
                        "total_rows": 0,
                        "query_plan": current_plan.to_dict(),
                        "output_mode": output_mode,
                        "corrections": corrections if corrections else None,
                    },
                    ensure_ascii=False,
                )

            break  # 有数据，跳出循环

        # 保存结果到租户目录
        exporter = DataExporter()
        query_plan_dict = current_plan.to_dict()
        file_path = exporter.save_json(
            data=rows,
            meta=exporter.build_meta(
                question=question,
                connection_id=connection.id,
                connection_name=connection.name,
                query_plan=query_plan_dict,
                total_rows=len(rows),
                file_path="",
            ),
        )

        # 更新 meta 中的文件路径
        meta = exporter.build_meta(
            question=question,
            connection_id=connection.id,
            connection_name=connection.name,
            query_plan=query_plan_dict,
            total_rows=len(rows),
            file_path=file_path,
        )
        with open(file_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        payload["meta"] = meta
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        summary = exporter.build_summary(current_plan, len(rows))
        download_url = f"/api/file?path={quote(file_path)}"

        message = self._build_chat_message(
            question=question,
            connection_name=connection.name,
            file_path=file_path,
        )

        preview_rows = rows[:20]

        chart_result = None
        dashboard_result = None
        insights_result = None

        if output_mode in ("chart", "dashboard") and _MATPLOTLIB_AVAILABLE:
            chart_result = generate_chart(
                rows=rows,
                intent=current_plan.intent,
                title=current_plan.intent,
            )

        if output_mode == "dashboard" and _MATPLOTLIB_AVAILABLE:
            dashboard_result = self._build_domain_dashboard(rows, current_plan.domain, current_plan.intent)

        try:
            from Scene.sap_data_analysis.backend.sap.ai_insights import generate_insights_for_sap_rows
            insights_result = generate_insights_for_sap_rows(rows, domain=current_plan.domain)
        except Exception as e:
            logger.warning(f"[SapDataAnalysisHandler] insights generation failed: {e}")
            insights_result = None

        return json.dumps(
            {
                "status": "success",
                "question": question,
                "summary": summary,
                "data_file": file_path,
                "download_url": download_url,
                "total_rows": len(rows),
                "query_plan": query_plan_dict,
                "message": message,
                "preview_data": preview_rows,
                "output_mode": output_mode,
                "chart": chart_result,
                "dashboard": dashboard_result,
                "insights": insights_result,
                "corrections": corrections if corrections else None,
            },
            ensure_ascii=False,
        )

    def GET(self, file_id: str):
        _require_auth()
        _require_permission(self.REQUIRED_SCENE_PERMISSION)

        try:
            exporter = DataExporter()
            file_path = exporter.resolve_file_path(unquote(file_id))
            if not os.path.isfile(file_path):
                raise web.notfound()

            csv_content = exporter.json_to_csv(file_path)
            web.header("Content-Type", "text/csv; charset=utf-8")
            web.header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{quote(unquote(file_id).replace('.json', '.csv'))}",
            )
            return csv_content
        except web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"[SapDataAnalysisHandler] csv error: {e}")
            raise web.notfound()

    # -------------------------------------------------------------------------
    # 内部工具
    # -------------------------------------------------------------------------
    def _error(self, message: str, status: int = 400, code: str = "") -> str:
        status_text = {400: "Bad Request", 403: "Forbidden", 404: "Not Found",
                       409: "Conflict", 503: "Service Unavailable"}.get(status, "Error")
        web.ctx.status = f"{status} {status_text}"
        payload = {"status": "error", "message": message}
        if code:
            payload["code"] = code
        return json.dumps(payload, ensure_ascii=False)

    def _has_scope_param(self, context: Dict[str, Any]) -> bool:
        company_code = (context.get("company_code") or "").strip()
        date_from = (context.get("date_from") or "").strip()
        date_to = (context.get("date_to") or "").strip()
        max_rows = context.get("max_rows")
        return bool(company_code or date_from or date_to or max_rows)

    def _has_scope_keyword(self, question: str) -> bool:
        """判断问题本身是否包含可推导范围的时间/范围关键词。"""
        import re

        q = question.lower()
        # 1. 明确年份，如 2025、2025年
        if re.search(r"(?<!\d)(19|20)\d{2}(?!\d)", q):
            return True
        # 2. 通用相对时间词与范围表达
        time_keywords = [
            "今年", "去年", "明年", "前年", "上年", "下年", "上一年", "下一年",
            "本月", "上月", "下月", "当月", "这个月", "上个月", "下个月",
            "本季度", "上季度", "下季度", "这个季度", "上个季度", "下个季度",
            "近",  # 近三个月、近半年、近一年
            "同期", "同比", "环比",
            "累计", "总共", "所有", "全部", "ytd", "year to date",
        ]
        return any(kw in q for kw in time_keywords)

    def _apply_defaults(
        self,
        connection_id: str,
        context: Dict[str, Any],
        role: str,
    ):
        """高管/微信入口：根据角色填充默认连接和范围参数。"""
        defaults = dict(EXECUTIVE_DEFAULTS)
        if not connection_id and defaults.get("default_connection_id"):
            connection_id = defaults["default_connection_id"]

        if not context.get("company_code") and defaults.get("default_company_code"):
            context["company_code"] = defaults["default_company_code"]

        if not context.get("date_from") or not context.get("date_to"):
            date_from, date_to = self._resolve_period_rule(
                defaults.get("default_period_rule", "current_month")
            )
            context.setdefault("date_from", date_from)
            context.setdefault("date_to", date_to)

        context.setdefault("max_rows", 5000)
        return connection_id, context

    def _resolve_period_rule(self, rule: str):
        """根据规则解析日期范围。"""
        from datetime import datetime, timedelta

        today = datetime.now()
        if rule == "current_month":
            start = today.replace(day=1)
            next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
            end = next_month - timedelta(days=1)
            return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
        if rule == "current_quarter":
            quarter = (today.month - 1) // 3
            start = today.replace(month=quarter * 3 + 1, day=1)
            next_q = today.replace(month=quarter * 3 + 4, day=1) if quarter < 3 else today.replace(year=today.year + 1, month=1, day=1)
            end = next_q - timedelta(days=1)
            return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
        if rule == "current_year":
            start = today.replace(month=1, day=1)
            end = today.replace(month=12, day=31)
            return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
        return "", ""

    def _find_connection(self, connection_id: str):
        """Resolve the SAP connection through the external-connection service.

        The old implementation read ``erp_connections.json`` and, when no id was
        given, quietly took the default or the only row. It now resolves the
        named connection -- or the tenant's catalogue default -- and refuses with
        a code when there is genuinely no default, instead of guessing. A
        connection whose provider has no production adapter also refuses here.
        """
        from integrations.external.adapters import erp_scene

        return erp_scene.resolve_erp_connection(
            (connection_id or "").strip() or None,
            tenant_id=get_current_tenant_id())

    def _create_provider(self, connection):
        return connection.create_provider()

    def _build_chat_message(self, question: str, connection_name: str, file_path: str) -> str:
        return (
            f"用户问题：{question}\n\n"
            f"已自动从 SAP 系统 [{connection_name}] 抽取数据，结果文件：\n"
            f"file:///{file_path}\n\n"
            f"请基于该数据回答用户问题，并给出 3-5 条进一步分析建议。\n"
            f"对于需要再次查询 SAP 才能完成的建议，请在末尾标注："
            f"'可在聊天窗口输入 /sap <具体问题> 让我继续分析。'"
        )

    def _merge_follow_up_question(self, prev_question: str, current_question: str) -> str:
        """合并上一轮问题与当前后续问题，使 query_planner 能理解完整意图。"""
        # 如果当前问题已经包含主语/表名，直接返回当前问题
        standalone_keywords = ["客户", "供应商", "物料", "采购", "生产", "库存", "财务", "凭证"]
        if any(kw in current_question for kw in standalone_keywords):
            return current_question
        # 否则拼接为完整问句
        return f"{prev_question}，{current_question}"

    def _build_domain_dashboard(
        self, rows: List[Dict[str, Any]], domain: str, intent: str
    ) -> Dict[str, Any]:
        """根据领域选择对应的看板生成函数。"""
        builders = {
            "finance": build_finance_dashboard,
            "sales": build_sales_dashboard,
            "procurement": build_procurement_dashboard,
            "inventory": build_inventory_dashboard,
            "production": build_production_dashboard,
            "master_data": build_master_data_dashboard,
        }
        builder = builders.get(domain, build_dashboard)
        return builder(rows, intent=intent)

    def _llm_replan(
        self,
        failed_plan: QueryPlan,
        question: str,
        error_message: str,
        connection: Dict[str, Any],
    ) -> Tuple[Optional[QueryPlan], str]:
        """当执行层自修正失败时，将错误上下文发给 V2 LLM 重新生成计划。

        Returns:
            (fixed_plan, correction_description) 或 (None, error_description)
        """
        import subprocess
        import sys

        planner = QueryPlanner()
        skill_path = planner.skill_planner_path
        if not os.path.isfile(skill_path):
            return None, "[LLM重规划] 技能脚本不存在，无法重试"

        try:
            # 构造带有失败上下文的消息
            context = {
                "is_replan": True,
                "original_question": question,
                "failed_table": failed_plan.table,
                "failed_fields": failed_plan.fields,
                "failed_where": failed_plan.where,
                "failed_source": failed_plan.source,
                "error_message": error_message,
            }
            payload = {
                "question": question,
                "context": context,
                "available_tables": [],
            }
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            result = subprocess.run(
                [sys.executable, skill_path, "--json"],
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                timeout=90,
                env=env,
                encoding="utf-8",
            )
            if result.returncode != 0:
                return None, f"[LLM重规划] 技能脚本返回非零: {result.stderr}"

            data = json.loads(result.stdout)
            if data.get("status") != "success" or "plan" not in data:
                return None, f"[LLM重规划] 技能脚本返回无有效计划: {data}"

            fixed_plan = QueryPlan.from_dict(data["plan"])
            logger.info(
                f"[SapDataAnalysisHandler] LLM re-plan: "
                f"table={fixed_plan.table} source={fixed_plan.source} "
                f"fields={fixed_plan.fields[:5]}..."
            )
            return fixed_plan, (
                f"[LLM重规划] 原计划(表={failed_plan.table}, 字段={failed_plan.fields}) "
                f"失败于 '{error_message[:100]}'，"
                f"重规划后(表={fixed_plan.table}, 字段={fixed_plan.fields})"
            )
        except Exception as e:
            logger.error(f"[SapDataAnalysisHandler] LLM re-plan exception: {e}")
            return None, f"[LLM重规划] 异常: {e}"
