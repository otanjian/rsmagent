"""基于 sap-adt-cli 的 ADT SQL Provider。

通过调用 vendor/sap-adt-cli/scripts/sap_adt_cli.py
的 run-sql 子命令执行 Open SQL，适合没有 SAP NW RFC SDK 时的快速验证和轻量查询。

注意：ADT Data Preview 不适合大批量数据抽取，数据量大的场景请使用 RfcProvider。
"""

import json
import os
import re
import subprocess
from typing import Any, Dict, List

from .provider import SAPDataProvider
from .scene_config import ADT_SQL_TEMPLATES, FIELD_MAPPINGS, RFC_QUERIES, _build_sql_where


class AdtSqlProvider(SAPDataProvider):
    """使用 sap-adt-cli 执行 ADT SQL 查询。"""

    # 相对项目根目录的默认 sap-adt-cli 脚本路径
    DEFAULT_CLI_PATH = os.path.join("vendor", "sap-adt-cli", "scripts", "sap_adt_cli.py")

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        client: str,
        verify_ssl: bool = True,
        cli_path: str = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.client = client
        self.verify_ssl = verify_ssl

        if cli_path:
            self.cli_path = cli_path
        elif os.environ.get("SAP_ADT_CLI_PATH"):
            self.cli_path = os.environ.get("SAP_ADT_CLI_PATH")
        else:
            # 从当前文件位置向上推算项目根目录
            # Packaged CLI belongs to this scene.
            project_root = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            self.cli_path = os.path.join(project_root, self.DEFAULT_CLI_PATH)

    def _run_sql(self, sql: str, max_rows: int = 1000) -> List[Dict[str, Any]]:
        """调用 sap-adt-cli 执行 SQL 并返回 JSON 结果。"""
        env = os.environ.copy()
        env.update({
            "SAP_URL": self.base_url,
            "SAP_USERNAME": self.username,
            "SAP_PASSWORD": self.password,
            "SAP_CLIENT": str(self.client),
            "SAP_VERIFY_SSL": "1" if self.verify_ssl else "0",
        })

        cmd = [
            "python",
            self.cli_path,
            "run-sql",
            sql,
            "--max-rows",
            str(max_rows),
        ]

        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"ADT SQL 执行失败: {err}")

        stdout = result.stdout.strip()
        if not stdout:
            return []

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"ADT SQL 返回结果不是有效 JSON: {stdout[:500]}...") from e

        # sap-adt-cli 可能直接返回对象列表，也可能包装在 {"rows": [...]} 中
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if "rows" in data:
                return data["rows"]
            if "data" in data:
                return data["data"]
        return []

    def execute_sql(self, sql: str, max_rows: int = 1000) -> List[Dict[str, Any]]:
        """直接执行 Open SQL，供 FetchExecutor 多表查询使用。"""
        return self._run_sql(sql, max_rows=max_rows)

    def test_connection(self) -> Dict[str, Any]:
        """执行一条轻量 SQL 验证连接。"""
        rows = self._run_sql("SELECT COUNT(*) AS cnt FROM t000", max_rows=1)
        return {"status": "success", "sample": rows}

    def fetch(self, scene_id: str, params: Dict[str, Any], raw: bool = False) -> List[Dict[str, Any]]:
        """根据场景模板执行 SQL 并映射字段。

        Args:
            scene_id: 场景/子场景 ID。
            params: 查询参数。
            raw: 为 True 时返回原始 SAP 字段名，不做中文映射。
        """
        if scene_id in ADT_SQL_TEMPLATES:
            template = ADT_SQL_TEMPLATES[scene_id]
            where_sql = _build_sql_where(params.get("supplier_code_range", ""))
            where_clause = f"AND {where_sql}" if where_sql else ""
            render_params = {
                "max_rows": params.get("max_rows", 1000),
                "where_clause": where_clause,
            }
            # 允许外部传入模板所需参数
            for key in params:
                if key not in render_params:
                    render_params[key] = params[key]
            sql = template.format(**render_params)
        elif scene_id in RFC_QUERIES:
            # 没有专用 ADT 模板时，根据 RFC 配置自动生成单表 SQL
            query = RFC_QUERIES[scene_id]
            table = query["table"]
            fields = ", ".join(query["fields"])
            max_rows = params.get("max_rows", 1000)
            conditions = []
            base_where = query.get("where", "")
            if base_where:
                conditions.append(base_where)
            filters = query.get("filters", {})
            lifnr_field = filters.get("supplier_code_range")
            if lifnr_field:
                where_sql = _build_sql_where(
                    params.get("supplier_code_range", ""),
                    lifnr_field=lifnr_field,
                )
                if where_sql:
                    conditions.append(where_sql)

            # 物料编码列表
            matnr_field = filters.get("materials")
            materials = params.get("materials")
            if matnr_field and materials:
                if isinstance(materials, str):
                    material_list = [m.strip() for m in re.split(r"[,\s\n]+", materials) if m.strip()]
                else:
                    material_list = [str(m).strip() for m in materials if str(m).strip()]
                if material_list:
                    # 根据字段名确定补零长度：LIFNR(供应商)=10，MATNR(物料)=18，其他默认18
                    _field_upper = matnr_field.upper().split(".")[-1]
                    _pad_len = 10 if _field_upper == "LIFNR" else 18
                    def _pad_code(value: str) -> str:
                        return value.zfill(_pad_len) if re.match(r"^\d+$", value) else value
                    padded = [_pad_code(v) for v in material_list]
                    values_str = ", ".join([f"'{v.replace(chr(39), chr(39)+chr(39))}'" for v in padded])
                    conditions.append(f"{matnr_field} IN ({values_str})")

            # 物料/供应商名称列表（短文本模糊匹配）
            name_field = filters.get("material_names")
            material_names = params.get("material_names")
            if name_field and material_names:
                if isinstance(material_names, str):
                    name_list = [n.strip() for n in re.split(r"[,;，；、\s\n]+", material_names) if n.strip()]
                else:
                    name_list = [str(n).strip() for n in material_names if str(n).strip()]
                if name_list:
                    like_conds = [f"{name_field} LIKE '%{n.replace(chr(39), chr(39)+chr(39))}%'" for n in name_list]
                    if len(like_conds) == 1:
                        conditions.append(like_conds[0])
                    else:
                        conditions.append("(" + " OR ".join(like_conds) + ")")

            # 日期范围
            date_field = filters.get("date_range")
            date_range = params.get("date_range")
            if date_field and date_range:
                start_date = end_date = None
                if isinstance(date_range, dict):
                    start_date = date_range.get("start")
                    end_date = date_range.get("end")
                elif isinstance(date_range, str):
                    m = re.match(r"^(\d{4}[-/]\d{2}[-/]\d{2})\s*[~至-]\s*(\d{4}[-/]\d{2}[-/]\d{2})$", date_range.strip())
                    if m:
                        start_date, end_date = m.group(1), m.group(2)
                if start_date and end_date:
                    def _norm_date(value: str) -> str:
                        return value.strip().replace("-", "").replace("/", "")
                    conditions.append(f"{date_field} BETWEEN '{_norm_date(start_date)}' AND '{_norm_date(end_date)}'")
            where_part = " AND ".join(conditions)
            if where_part:
                sql = f"SELECT {fields} FROM {table} WHERE {where_part} UP TO {max_rows} ROWS"
            else:
                sql = f"SELECT {fields} FROM {table} UP TO {max_rows} ROWS"
        else:
            raise ValueError(f"场景 {scene_id} 没有配置 ADT SQL 模板")

        rows = self._run_sql(sql, max_rows=params.get("max_rows", 1000))
        if raw:
            return rows
        mapping = FIELD_MAPPINGS.get(scene_id, {})
        return [{mapping.get(k, k): v for k, v in row.items()} for row in rows]
