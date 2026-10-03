"""SAP 数据分析领域/表级/BAPI 权限校验。

校验规则：
1. 若用户拥有 sap.query.admin，跳过所有校验；
2. 检查 QueryPlan.domain 是否在用户权限范围内；
3. 检查 QueryPlan.table 及所有 joins.table 是否在用户可访问的表白名单中；
4. 检查 fields 是否包含敏感字段；
5. 检查 QueryPlan.bapi_name 是否在允许调用的 BAPI 白名单中。
"""

from typing import List, Set

from Scene._shared.host import get_current_permissions, get_current_roles
from common.log import logger

from .config import (
    ADMIN_PERMISSION,
    BAPI_DOMAIN_MAP,
    DOMAIN_PERMISSION_MAP,
    DOMAIN_TABLE_WHITELIST,
    SENSITIVE_FIELD_BLACKLIST,
    TABLE_DOMAIN_MAP,
)
from .query_planner import QueryPlan


class PermissionGuard:
    """SAP 数据分析权限校验器。"""

    def __init__(self, user_permissions: List[str] = None, user_roles: List[str] = None):
        self.permissions = set(get_current_permissions() if user_permissions is None else user_permissions)
        self.roles = set(get_current_roles() if user_roles is None else user_roles)
        self.is_admin = ADMIN_PERMISSION in self.permissions or "admin" in self.roles

    def check_plan(self, plan: QueryPlan) -> None:
        """校验 QueryPlan，无权限时抛出 PermissionError。"""
        if self.is_admin:
            logger.debug("[PermissionGuard] admin user, skipping checks")
            return

        self._check_domain(plan.domain)
        self._check_tables(plan)
        self._check_fields(plan)
        self._check_bapi(plan)

    def _check_domain(self, domain: str) -> None:
        required = DOMAIN_PERMISSION_MAP.get(domain)
        if not required:
            raise PermissionError(f"未知的 SAP 数据领域: {domain}")
        if required not in self.permissions:
            raise PermissionError(f"您暂无权限查询该领域数据: {domain}")

    def _check_tables(self, plan: QueryPlan) -> None:
        tables = [plan.table] + [j.table for j in plan.joins]
        for table in tables:
            if not table:
                continue
            table_upper = table.upper()
            allowed_domains = TABLE_DOMAIN_MAP.get(table_upper)
            if not allowed_domains:
                raise PermissionError(f"表 {table} 不在允许查询的白名单中")

            # 用户只要拥有任意一个允许该表的领域权限即可
            has_any = any(
                DOMAIN_PERMISSION_MAP.get(d) in self.permissions for d in allowed_domains
            )
            if not has_any:
                raise PermissionError(f"您暂无权限查询表 {table}")

    def _check_fields(self, plan: QueryPlan) -> None:
        all_fields: Set[str] = set(plan.fields)
        for j in plan.joins:
            all_fields.update(j.fields)

        for field in all_fields:
            field_upper = field.upper()
            if field_upper in SENSITIVE_FIELD_BLACKLIST:
                raise PermissionError(f"字段 {field} 属于敏感字段，禁止查询")

    def _check_bapi(self, plan: QueryPlan) -> None:
        if not plan.bapi_name:
            return
        bapi_upper = plan.bapi_name.upper()
        allowed_domains = BAPI_DOMAIN_MAP.get(bapi_upper)
        if not allowed_domains:
            raise PermissionError(f"BAPI {plan.bapi_name} 不在允许调用的白名单中")

        has_any = any(
            DOMAIN_PERMISSION_MAP.get(d) in self.permissions for d in allowed_domains
        )
        if not has_any:
            raise PermissionError(f"您暂无权限调用 BAPI {plan.bapi_name}")

    def allowed_domains(self) -> List[str]:
        """返回当前用户有权查询的领域列表。"""
        if self.is_admin:
            return list(DOMAIN_PERMISSION_MAP.keys())
        return [
            domain
            for domain, perm in DOMAIN_PERMISSION_MAP.items()
            if perm in self.permissions
        ]

    def allowed_tables(self) -> List[str]:
        """返回当前用户有权查询的表列表。"""
        allowed = self.allowed_domains()
        tables: Set[str] = set()
        for domain in allowed:
            tables.update(DOMAIN_TABLE_WHITELIST.get(domain, []))
        return sorted(tables)
