"""基于 pyrfc + SAP NW RFC SDK 的 RFC Provider。

通过调用 SAP 标准函数 RFC_READ_TABLE 读取透明表数据，
支持字段选择、WHERE 条件、分批读取，适合生产环境大数据量抽取。
"""


import logging
import os
import platform
import re
import sys
from typing import Any, Dict, List, Optional

from .provider import SAPDataProvider
from .scene_config import FIELD_MAPPINGS, RFC_QUERIES

logger = logging.getLogger(__name__)
_SDK_DLL_DIR_HANDLES = []


class RfcDependencyError(RuntimeError):
    """RFC 运行时依赖不可用时返回的可诊断错误。"""

    code = "sap_rfc_dependency_unavailable"


def _rfc_dependency_message(cause: BaseException) -> str:
    """根据当前运行环境生成 RFC 依赖安装提示。"""
    runtime = "Python {}.{} / {} {}".format(
        sys.version_info[0],
        sys.version_info[1],
        platform.system() or "unknown",
        platform.machine() or "unknown",
    )
    system = platform.system().lower()
    if system == "darwin":
        hint = (
            "当前运行环境为 macOS；项目附带的 pyrfc 轮子仅支持 Windows/Linux "
            "CPython 3.12，无法在此加载 SAP NW RFC SDK。请在 Windows/Linux "
            "Python 3.12 环境安装匹配的 pyrfc 和 SDK，或将连接方式改为 SAP ADT/HTTP。"
        )
    elif sys.version_info[:2] != (3, 12):
        hint = (
            "项目附带的 pyrfc 轮子仅支持 CPython 3.12。请使用 Python 3.12，"
            "并安装与操作系统匹配的 SAP NW RFC SDK 和 pyrfc 轮子。"
        )
    else:
        hint = (
            "请确认已安装与操作系统匹配的 SAP NW RFC SDK，并将 SDK 的 lib/bin "
            "目录加入运行时路径后再安装 pyrfc。"
        )
    detail = str(cause).strip()
    suffix = " 原始错误：{}".format(detail) if detail else ""
    return "SAP RFC 依赖不可用（{}）。{}{}".format(runtime, hint, suffix)


def _prepare_sap_nwrfc_sdk() -> None:
    """在导入 pyrfc 前注册项目内置或环境变量指定的 SDK 路径。"""
    candidates = []
    configured = os.getenv("SAPNWRFC_HOME")
    if configured:
        candidates.append(configured)
    system = platform.system().lower()
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    if system == "windows":
        candidates.append(os.path.join(project_root, "nwrfc750", "nwrfcsdk"))
    elif system == "linux":
        candidates.append(os.path.join(project_root, "nwrfc750P_19_linux", "nwrfcsdk"))

    sdk_dir = next(
        (path for path in candidates if os.path.isdir(os.path.join(path, "lib"))),
        None,
    )
    if not sdk_dir:
        return

    os.environ.setdefault("SAPNWRFC_HOME", sdk_dir)
    lib_dir = os.path.join(sdk_dir, "lib")
    if system == "windows":
        # Python 3.8+ no longer searches the process PATH for extension DLLs;
        # keep the handle alive for the lifetime of the process.
        add_dll_directory = getattr(os, "add_dll_directory", None)
        if add_dll_directory:
            try:
                _SDK_DLL_DIR_HANDLES.append(add_dll_directory(lib_dir))
            except OSError:
                pass
        os.environ["PATH"] = lib_dir + os.pathsep + os.path.join(sdk_dir, "bin") + os.pathsep + os.environ.get("PATH", "")
    elif system == "linux":
        current = os.environ.get("LD_LIBRARY_PATH", "")
        os.environ["LD_LIBRARY_PATH"] = lib_dir + (os.pathsep + current if current else "")


class RfcProvider(SAPDataProvider):
    """使用 pyrfc 通过 RFC 读取 SAP 表数据。"""

    # SAP 编码字段内部长度（ALPHA = IN）。当 WHERE 中这些字段的值长度不足时，
    # 自动前导补零以匹配 SAP 内部存储格式。
    ALPHA_FIELD_LENGTHS = {
        "MATNR": 18,  # 物料编码
        "KUNNR": 10,  # 客户编码
        "LIFNR": 10,  # 供应商编码
        "VBELN": 10,  # 销售订单/发票号
        "EBELN": 10,  # 采购订单号
        "AUFNR": 12,  # 生产订单号
        "BELNR": 10,  # 凭证号
    }

    def __init__(
        self,
        ashost: str,
        sysnr: str,
        client: str,
        username: str,
        password: str,
        lang: str = "ZH",
        saprouter: Optional[str] = None,
    ):
        _prepare_sap_nwrfc_sdk()
        try:
            import pyrfc
        except (ImportError, OSError) as e:
            # ImportError 覆盖未安装 pyrfc；OSError 覆盖已安装但找不到
            # SAP NW RFC SDK 动态库，二者都需要给出环境相关的处理建议。
            raise RfcDependencyError(_rfc_dependency_message(e)) from e

        params = {
            "ashost": ashost,
            "sysnr": sysnr,
            "client": client,
            "user": username,
            "passwd": password,
            "lang": lang,
        }
        if saprouter:
            params["saprouter"] = saprouter
        elif os.getenv("SAP_SAPROUTER"):
            params["saprouter"] = os.getenv("SAP_SAPROUTER")

        self.conn = pyrfc.Connection(**params)

    def test_connection(self) -> Dict[str, Any]:
        """调用 RFC_PING 验证连接。"""
        self.conn.call("RFC_PING")
        attrs = self.conn.get_connection_attributes()
        return {
            "status": "success",
            "system": attrs.get("sysId"),
            "client": attrs.get("client"),
            "user": attrs.get("user"),
            "host": attrs.get("host"),
        }

    def fetch(self, scene_id: str, params: Dict[str, Any], raw: bool = False) -> List[Dict[str, Any]]:
        """根据场景配置读取 SAP 表并映射字段。

        Args:
            scene_id: 场景/子场景 ID。
            params: 查询参数。
            raw: 为 True 时返回原始 SAP 字段名，不做中文映射。
        """
        if scene_id not in RFC_QUERIES:
            raise ValueError(f"场景 {scene_id} 没有配置 RFC 查询")

        query = RFC_QUERIES[scene_id]
        where = self._build_where(
            query.get("where", ""),
            params,
            query.get("filters", {}),
        )
        rows = self._fetch_table(
            table=query["table"],
            fields=query["fields"],
            where=where,
            max_rows=params.get("max_rows", 10000),
            batch_size=params.get("batch_size", 5000),
        )
        if raw:
            return rows
        mapping = FIELD_MAPPINGS.get(scene_id, {})
        return [{mapping.get(k, k): v for k, v in row.items()} for row in rows]

    def _build_where(
        self,
        base_where: str,
        params: Dict[str, Any],
        filters: Dict[str, str],
    ) -> str:
        """根据传入参数和字段映射动态拼接 WHERE 条件。

        Args:
            base_where: 场景配置中写死的 WHERE 片段。
            params: 前端传入的过滤参数，例如 company_code、purchasing_org、
                supplier_code_range。
            filters: 当前查询支持的过滤字段映射。
                例如 {"supplier_code_range": "LIFNR"} 表示供应商编码范围
                过滤当前表的 LIFNR 字段。
        """
        conditions = []
        if base_where:
            conditions.append(base_where)

        def _escape(value: str) -> str:
            return value.replace("'", "''")

        company_code = params.get("company_code")
        field = filters.get("company_code")
        if company_code and field:
            conditions.append(f"{field} = '{_escape(company_code)}'")

        purchasing_org = params.get("purchasing_org")
        field = filters.get("purchasing_org")
        if purchasing_org and field:
            conditions.append(f"{field} = '{_escape(purchasing_org)}'")

        supplier_range = params.get("supplier_code_range")
        field = filters.get("supplier_code_range")
        if supplier_range and field:
            # 支持格式：1000000000 - 1999999999
            parts = [p.strip() for p in supplier_range.split("-")]
            if len(parts) == 2:
                conditions.append(
                    f"{field} BETWEEN '{_escape(parts[0])}' AND '{_escape(parts[1])}'"
                )
            else:
                conditions.append(f"{field} = '{_escape(supplier_range)}'")

        # 物料编码列表（支持逗号/换行/空格分隔的字符串或列表）
        materials = params.get("materials")
        field = filters.get("materials")
        if materials and field:
            if isinstance(materials, str):
                material_list = [m.strip() for m in re.split(r"[,;，；、\s\n]+", materials) if m.strip()]
            else:
                material_list = [str(m).strip() for m in materials if str(m).strip()]
            if material_list:
                # 根据字段名确定补零长度：LIFNR(供应商)=10，MATNR(物料)=18，其他默认18
                _field_upper = field.upper().split(".")[-1]
                _pad_len = 10 if _field_upper == "LIFNR" else 18
                def _pad_matnr(value: str) -> str:
                    return value.zfill(_pad_len) if re.match(r"^\d+$", value) else value
                padded = [_pad_matnr(v) for v in material_list]
                values_str = ", ".join([f"'{_escape(v)}'" for v in padded])
                conditions.append(f"{field} IN ({values_str})")

        # 物料名称列表（短文本模糊匹配，多个 LIKE 用 OR 连接，括号分组保证与日期
        # AND 条件的优先级正确；编码非空时前端不传名称，故不会与 MATNR IN 共存）
        material_names = params.get("material_names")
        field = filters.get("material_names")
        if material_names and field:
            if isinstance(material_names, str):
                name_list = [n.strip() for n in re.split(r"[,;，；、\s\n]+", material_names) if n.strip()]
            else:
                name_list = [str(n).strip() for n in material_names if str(n).strip()]
            if name_list:
                like_conds = [f"{field} LIKE '%{_escape(n)}%'" for n in name_list]
                if len(like_conds) == 1:
                    conditions.append(like_conds[0])
                else:
                    conditions.append("(" + " OR ".join(like_conds) + ")")

        # 日期范围（支持字符串 start-end 或字典 {start, end}）
        date_range = params.get("date_range")
        field = filters.get("date_range")
        if date_range and field:
            start_date = end_date = None
            if isinstance(date_range, dict):
                start_date = date_range.get("start")
                end_date = date_range.get("end")
            elif isinstance(date_range, str):
                # 支持格式：2024-08-05 ~ 2026-08-05 或 2024-08-05 - 2026-08-05
                m = re.match(r"^(\d{4}[-/]\d{2}[-/]\d{2})\s*[~至-]\s*(\d{4}[-/]\d{2}[-/]\d{2})$", date_range.strip())
                if m:
                    start_date, end_date = m.group(1), m.group(2)
            if start_date and end_date:
                # 统一转换为 YYYYMMDD 格式
                def _normalize_sap_date(value: str) -> str:
                    value = value.strip().replace("-", "").replace("/", "")
                    return value if re.match(r"^\d{8}$", value) else value
                conditions.append(
                    f"{field} BETWEEN '{_normalize_sap_date(start_date)}' AND '{_normalize_sap_date(end_date)}'"
                )

        return " AND ".join(conditions)

    def _alpha_convert_where(self, where: str) -> str:
        """把 WHERE 中的外部格式编码值转换为 SAP 内部格式（前导补零）。

        处理以下模式（字段名可带表前缀，如 MARA.MATNR）：
        - FIELD = 'value'
        - FIELD BETWEEN 'value1' AND 'value2'
        - FIELD IN ('value1', 'value2', ...)
        仅对纯数字且长度不足的值补零；已满足长度或含非数字字符的值保持原样。
        """
        if not where:
            return where

        fields_pattern = "|".join(self.ALPHA_FIELD_LENGTHS)
        field_boundary = r"(?<![A-Za-z0-9_])"

        def _convert_value(field: str, value: str) -> str:
            length = self.ALPHA_FIELD_LENGTHS.get(field.upper())
            if not length:
                return value
            if not re.match(r"^\d+$", value):
                return value
            if len(value) >= length:
                return value
            return value.zfill(length)

        result = where

        # 1. FIELD = 'value' / FIELD = "value"
        def _replace_eq(match: re.Match) -> str:
            prefix = match.group(1)  # 表前缀，如 "MARA."
            field = match.group(2)
            spacing = match.group(3)
            quote = match.group(4)
            value = match.group(5)
            new_value = _convert_value(field, value)
            return f"{prefix}{field}{spacing}= {quote}{new_value}{quote}"

        eq_pattern = (
            rf"{field_boundary}"
            rf"((?:[A-Za-z0-9_]+\.)?)"
            rf"({fields_pattern})"
            rf"(?![A-Za-z0-9_])"
            rf"(\s*)"
            rf"=\s*"
            rf"('|\")"
            rf"([^'\"]*)"
            rf"\4"
        )
        result = re.sub(eq_pattern, _replace_eq, result, flags=re.IGNORECASE)

        # 2. FIELD BETWEEN 'value1' AND 'value2'
        def _replace_between(match: re.Match) -> str:
            prefix = match.group(1)
            field = match.group(2)
            spacing1 = match.group(3)
            quote1 = match.group(4)
            v1 = match.group(5)
            spacing2 = match.group(6)
            quote2 = match.group(7)
            v2 = match.group(8)
            new_v1 = _convert_value(field, v1)
            new_v2 = _convert_value(field, v2)
            return (
                f"{prefix}{field}{spacing1}BETWEEN {quote1}{new_v1}{quote1}{spacing2}"
                f"AND {quote2}{new_v2}{quote2}"
            )

        between_pattern = (
            rf"{field_boundary}"
            rf"((?:[A-Za-z0-9_]+\.)?)"  # 1 prefix
            rf"({fields_pattern})"        # 2 field
            rf"(?![A-Za-z0-9_])"
            rf"(\s*)"                     # 3 spacing1
            rf"BETWEEN\s+"
            rf"('|\")"                    # 4 quote1
            rf"([^'\"]*)"                 # 5 v1
            rf"\4"
            rf"(\s+)"                     # 6 spacing2
            rf"AND\s+"
            rf"('|\")"                    # 7 quote2
            rf"([^'\"]*)"                 # 8 v2
            rf"\7"
        )
        result = re.sub(between_pattern, _replace_between, result, flags=re.IGNORECASE)

        # 3. FIELD IN ('value1', 'value2', ...)
        def _replace_in(match: re.Match) -> str:
            prefix = match.group(1)
            field = match.group(2)
            spacing = match.group(3)
            values_str = match.group(4)

            def _replace_in_value(m: re.Match) -> str:
                quote = m.group(1)
                value = m.group(2)
                new_value = _convert_value(field, value)
                return f"{quote}{new_value}{quote}"

            new_values_str = re.sub(r"('|\")([^'\"]*)\1", _replace_in_value, values_str)
            return f"{prefix}{field}{spacing}IN ({new_values_str})"

        in_pattern = (
            rf"{field_boundary}"
            rf"((?:[A-Za-z0-9_]+\.)?)"
            rf"({fields_pattern})"
            rf"(?![A-Za-z0-9_])"
            rf"(\s*)"
            rf"IN\s*\("
            rf"([^)]*)"
            rf"\)"
        )
        result = re.sub(in_pattern, _replace_in, result, flags=re.IGNORECASE)

        return result

    def _split_where(self, where: str, max_len: int = 72) -> List[str]:
        """把 WHERE 拆分为多行，每行不超过 max_len，不在 IN 子句的值中间截断。

        RFC_READ_TABLE 的 OPTIONS 每行最长 72 字符。SAP 会把各行 TEXT 直接
        拼接成完整 WHERE 子句。本方法按顶层 AND/OR 关键字拆分（识别括号
        深度，不拆分 IN (...) 内部），对超长的 IN 子句在值之间的 ", " 处
        换行，保证每个值完整、拼接后语法正确。
        """
        where = where.strip()
        if len(where) <= max_len:
            return [where]

        # 第一步：按顶层 AND/OR 拆分（跳过括号内的内容，避免破坏 IN 列表
        # 和 BETWEEN ... AND ... 中间的 AND）
        tokens: List[str] = []
        current = ""
        depth = 0
        i = 0
        while i < len(where):
            ch = where[i]
            if ch == "(":
                depth += 1
                current += ch
                i += 1
            elif ch == ")":
                depth = max(0, depth - 1)
                current += ch
                i += 1
            elif depth == 0:
                m = re.match(r"\s+(AND|OR)\s+", where[i:], re.IGNORECASE)
                if m:
                    if current.strip():
                        tokens.append(current.strip())
                    tokens.append(m.group(1).upper())
                    current = ""
                    i += m.end()
                else:
                    current += ch
                    i += 1
            else:
                current += ch
                i += 1
        if current.strip():
            tokens.append(current.strip())

        # 第二步：组装行，超长片段调用 _split_long_token 在安全位置换行
        lines: List[str] = []
        current_line = ""

        for token in tokens:
            is_op = token in ("AND", "OR")
            if is_op:
                candidate = (current_line + " " + token).strip() if current_line else token
                if len(candidate) <= max_len:
                    current_line = candidate
                else:
                    if current_line:
                        lines.append(current_line)
                    current_line = token
                continue

            if len(token) <= max_len:
                candidate = (current_line + " " + token).strip() if current_line else token
                if len(candidate) <= max_len:
                    current_line = candidate
                else:
                    if current_line:
                        lines.append(current_line)
                    current_line = token
            else:
                if current_line:
                    lines.append(current_line)
                    current_line = ""
                lines.extend(self._split_long_token(token, max_len))

        if current_line:
            lines.append(current_line)

        # SAP 直接拼接 OPTIONS 各行 TEXT，行间不自动加空格。
        # 为避免 ')AND 或 AND'value 这类粘连导致语法错误，给非末行补尾随空格。
        for i in range(len(lines) - 1):
            if lines[i] and not lines[i].endswith(" "):
                lines[i] = lines[i] + " "
        return lines

    def _split_long_token(self, token: str, max_len: int) -> List[str]:
        """拆分超长的 WHERE 片段。

        1. 若片段形如 ``FIELD IN ('v1', 'v2', ...)``，在值之间的 ", " 处换行，
           保证每个值完整。
        2. 否则若含 OR 关键字（如括号分组的多个 LIKE），在 OR 处换行，
           避免截断值或破坏括号。
        3. 兜底按 max_len 硬截断。
        """
        # 1. IN 子句
        m = re.match(r"^(\S+\s+IN\s*\()(.*)\)(.*)$", token, re.IGNORECASE | re.DOTALL)
        if m:
            prefix = m.group(1)   # 如 "MATNR IN ("
            body = m.group(2)     # 如 "'000000001000000048', '000000001000000049'"
            suffix = m.group(3)   # 通常为空

            segments = re.split(r"(,\s*)", body)
            lines: List[str] = []
            current = prefix
            idx = 0
            while idx < len(segments):
                chunk = segments[idx] + (segments[idx + 1] if idx + 1 < len(segments) else "")
                idx += 2
                if len(current) + len(chunk) <= max_len:
                    current += chunk
                else:
                    lines.append(current)
                    current = chunk
            current += ")" + suffix
            lines.append(current)
            return lines

        # 2. 含 OR 的表达式（如括号分组的多个 LIKE），在 OR 处换行
        if re.search(r"\s+OR\s+", token, re.IGNORECASE):
            parts = re.split(r"(\s+OR\s+)", token, flags=re.IGNORECASE)
            lines = []
            current = ""
            for part in parts:
                candidate = current + part
                if len(candidate) <= max_len:
                    current = candidate
                else:
                    if current:
                        lines.append(current)
                    current = part
            if current:
                lines.append(current)
            return lines

        # 3. 兜底硬截断
        return [token[i : i + max_len] for i in range(0, len(token), max_len)]

    def _fetch_table(
        self,
        table: str,
        fields: List[str],
        where: str = "",
        max_rows: int = 10000,
        batch_size: int = 5000,
    ) -> List[Dict[str, Any]]:
        """使用 RFC_READ_TABLE 分批读取表数据。

        RFC_READ_TABLE 的 OPTIONS 每行最长 72 字符，这里按 AND/OR 条件拆分，
        避免把一个布尔表达式从中间截断导致 SAP 解析失败。
        """
        all_rows: List[Dict[str, Any]] = []
        skip = 0

        field_list = [{"FIELDNAME": f.upper()} for f in fields]
        options = []
        if where:
            converted_where = self._alpha_convert_where(where)
            if converted_where != where:
                logger.info(f"[RfcProvider] alpha convert: {where!r} -> {converted_where!r}")
            for text in self._split_where(converted_where):
                options.append({"TEXT": text})
            logger.info(
                f"[RfcProvider] table={table} where={converted_where!r} options={options}"
            )

        while True:
            result = self.conn.call(
                "RFC_READ_TABLE",
                QUERY_TABLE=table.upper(),
                FIELDS=field_list,
                OPTIONS=options,
                DELIMITER="|",
                ROWCOUNT=batch_size,
                ROWSKIPS=skip,
            )

            field_names = [f["FIELDNAME"] for f in result["FIELDS"]]
            data = result["DATA"]

            if not data:
                break

            for row in data:
                values = row["WA"].split("|")
                # 防止字段数与值数量不一致
                if len(values) != len(field_names):
                    values = values + [""] * (len(field_names) - len(values))
                    values = values[: len(field_names)]
                all_rows.append({name: val.strip() for name, val in zip(field_names, values)})

            skip += batch_size
            if len(data) < batch_size or (max_rows and len(all_rows) >= max_rows):
                break

        return all_rows[:max_rows] if max_rows else all_rows

    def call_bapi(self, bapi_name: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """通用 BAPI/RFC 调用接口，供扩展使用。"""
        normalized = self._normalize_bapi_parameters(bapi_name, parameters)
        return self.conn.call(bapi_name, **normalized)

    def _normalize_bapi_parameters(
        self, bapi_name: str, parameters: Dict[str, Any]
    ) -> Dict[str, Any]:
        """根据 sap_query_catalog.json 对 BAPI 参数做规范化（alpha 类型前导补零等）。"""
        try:
            catalog = self._load_query_catalog()
        except Exception:
            return parameters

        api_meta = catalog.get("apis", {}).get(bapi_name.upper())
        if not api_meta or api_meta.get("type") != "bapi":
            return parameters

        alpha_fields = catalog.get("alpha_fields", {})
        param_defs = api_meta.get("parameters", {})
        result = {}
        for key, value in parameters.items():
            upper_key = key.upper()
            param_def = param_defs.get(upper_key, {})
            param_type = param_def.get("type", "")
            if param_type == "alpha" and isinstance(value, str):
                length = param_def.get("length") or alpha_fields.get(upper_key, 0)
                value = self._alpha_pad(value, int(length) if length else 0)
            elif param_type == "date" and isinstance(value, str):
                # 统一去掉横杠，确保 YYYYMMDD
                value = value.replace("-", "")
            result[key] = value
        return result

    def _alpha_pad(self, value: str, length: int) -> str:
        """对纯数字编码值按 SAP 内部长度前导补零。"""
        if not length or not value or not re.match(r"^\d+$", value):
            return value
        if len(value) >= length:
            return value
        return value.zfill(length)

    def _load_query_catalog(self) -> Dict[str, Any]:
        """加载 SAP 查询能力目录。"""
        from .config import get_catalog
        return get_catalog()


    def fetch_by_plan(self, plan) -> List[Dict[str, Any]]:
        """根据 QueryPlan 读取 SAP 表数据。

        Args:
            plan: Scene.sap_data_analysis.backend.sap.query_planner.QueryPlan 实例

        Returns:
            字段名已映射为中文表头的字典列表
        """
        from .query_planner import QueryPlan

        if not isinstance(plan, QueryPlan):
            raise ValueError("plan 必须是 QueryPlan 实例")

        if plan.source != "table":
            raise ValueError(f"RfcProvider.fetch_by_plan 暂不支持 source={plan.source}")

        # RFC_READ_TABLE 不接受 SUM(field) 这类 SQL 聚合表达式，提前清洗为真实字段
        fields = self._clean_fields(plan.fields)

        rows = self._fetch_table(
            table=plan.table,
            fields=fields,
            where=plan.where,
            max_rows=plan.max_rows,
            batch_size=5000,
        )
        mapping = plan.field_mapping or {}
        return [{mapping.get(k, k): v for k, v in row.items()} for row in rows]

    @staticmethod
    def _clean_fields(fields: List[str]) -> List[str]:
        """清洗字段列表，移除 RFC_READ_TABLE 不支持的聚合函数包裹。"""
        cleaned: List[str] = []
        seen: set[str] = set()
        for f in fields:
            raw = f.strip()
            m = re.match(
                r"^(SUM|COUNT|AVG|MIN|MAX)\s*\(\s*([A-Za-z0-9_\.]+)\s*\)$",
                raw,
                re.IGNORECASE,
            )
            if m:
                raw = m.group(2)
            key = raw.upper()
            if key and key not in seen:
                seen.add(key)
                cleaned.append(raw)
        return cleaned

    def close(self):
        """关闭 RFC 连接。"""
        try:
            self.conn.close()
        except Exception:
            pass
