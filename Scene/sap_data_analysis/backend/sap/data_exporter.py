"""SAP 数据分析结果落地与 CSV 按需转换。

JSON 文件保存到租户目录：
  {tenant_workspace}/.one/sap_analysis/sap_data_{timestamp}_{hash}.json

CSV 不常驻磁盘，仅在用户请求 /api/sap-data-analysis/{file_id}/csv 时实时从 JSON 转换返回。
"""

import csv
import hashlib
import io
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from Scene._shared.host import get_current_tenant_id
from common.log import logger


class DataExporter:
    """SAP 数据分析数据导出器。"""

    def __init__(self, tenant_id: Optional[str] = None):
        self.tenant_id = tenant_id or get_current_tenant_id()

    def _analysis_dir(self) -> str:
        from Scene._shared.host import _get_tenant_workspace_root
        return os.path.join(_get_tenant_workspace_root(self.tenant_id), ".one", "sap_analysis")

    def save_json(
        self,
        data: List[Dict[str, Any]],
        meta: Dict[str, Any],
    ) -> str:
        """保存分析结果 JSON，返回文件绝对路径。"""
        analysis_dir = self._analysis_dir()
        os.makedirs(analysis_dir, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        short_hash = hashlib.sha1(
            f"{meta.get('question', '')}{time.time()}".encode("utf-8")
        ).hexdigest()[:8]
        file_name = f"sap_data_{timestamp}_{short_hash}.json"
        file_path = os.path.join(analysis_dir, file_name)

        payload = {
            "meta": meta,
            "data": data,
        }

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        logger.info(f"[DataExporter] saved JSON: {file_path}")
        return file_path

    def json_to_csv(self, json_path: str) -> str:
        """读取 JSON 文件并将 data 数组转换为 CSV 字符串。"""
        with open(json_path, "r", encoding="utf-8") as f:
            payload = json.load(f)

        rows = payload.get("data", [])
        if not rows:
            return ""

        headers = list(rows[0].keys())
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
        # 添加 UTF-8 BOM，使 Excel 直接打开中文不乱码
        return "\ufeff" + output.getvalue()

    def resolve_file_path(self, file_id: str) -> str:
        """根据 file_id 解析 JSON 文件路径，并进行路径逃逸校验。"""
        # file_id 形如 sap_data_20250721_143022_xxx.json 或 xxx.json
        file_id = os.path.basename(file_id)
        analysis_dir = self._analysis_dir()
        file_path = os.path.normpath(os.path.join(analysis_dir, file_id))
        if not os.path.abspath(file_path).startswith(os.path.abspath(analysis_dir)):
            raise ValueError("非法文件路径")
        return file_path

    def build_meta(
        self,
        question: str,
        connection_id: str,
        connection_name: str,
        query_plan: Dict[str, Any],
        total_rows: int,
        file_path: str,
    ) -> Dict[str, Any]:
        """构造 JSON 文件中的 meta 部分。"""
        return {
            "question": question,
            "connection_id": connection_id,
            "connection_name": connection_name,
            "system": "sap",
            "tenant_id": self.tenant_id,
            "query_plan": query_plan,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_rows": total_rows,
            "files": {
                "json": file_path,
            },
        }

    def build_summary(
        self,
        plan: Any,
        total_rows: int,
    ) -> str:
        """根据查询计划与结果生成简要文字摘要。"""
        intent_labels = {
            "new_customers_this_month": "新增客户",
            "new_suppliers_this_month": "新增供应商",
            "sales_orders_this_month": "销售订单",
            "sales_amount_this_month": "销售金额",
            "top_customers_by_sales": "销售排名",
            "purchase_orders_this_month": "采购订单",
            "purchase_amount_this_month": "采购金额",
            "top_suppliers_by_purchase": "采购排名",
            "finance_overview": "财务凭证",
            "inventory_overview": "库存数据",
            "customer_list": "客户列表",
        }
        label = intent_labels.get(plan.intent, "数据")

        if plan.aggregation == "COUNT" or plan.aggregation == "SUM":
            return f"共查询到 {label} {total_rows} 条记录"
        return f"已抽取 {label} {total_rows} 条记录"
