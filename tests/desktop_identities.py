# encoding:utf-8
"""Synthetic identities for the desktop change, with their permitted operations.

Change ``add-desktop-remote-web-workbench``, task 1.2. Every desktop test needs
the same cast, and none of it may touch a developer's real credentials or real
business data: the harness builds an isolated ``identity.db`` and tenant roots in
a temporary directory, and the desktop *client* config directory is a per-test
``mkdtemp`` (see ``tests/test_desktop_remote_config.cjs``).

The cast (matching ``acceptance.md``'s A01-A12 actors):

===================  ==========================================================
name                 identity and what it may do
===================  ==========================================================
``PA``               platform administrator (the harness root user). Reaches
                     the platform console; member of tenant A.
``U1``, ``U2``       plain members of tenant A with ``chat.use`` / ``agent.use``
                     and a read grant on the tenant's Agent. No platform entry.
``TA``               tenant administrator of tenant A. Manages the tenant, not
                     the platform.
``UP``               member of tenant A whose role grants **nothing**: proves a
                     signed-in account with no qualification is refused.
``FB``               member of tenant B (``other``): proves cross-tenant refusal.
``ANON``             no credential at all: only public endpoints answer.
                     (No tenant membership at all -- ``U0`` in ``tasks.md`` -- is
                     the account-only session the console already covers in
                     ``test_web_database_capability_acceptance``; this module does
                     not invent a second path for it.)

Nothing here grants a desktop capability: the desktop surfaces are still closed
by the capability matrix, so these identities exercise the *boundary* (who may
call what) rather than a remote workbench that is not open yet.
"""

from __future__ import annotations

from typing import Any, Dict

#: Operations each identity is expected to be allowed to perform over HTTP.
#: Kept declarative so the accompanying test can assert the matrix is real
#: rather than merely documented. ``/api/tenant/permissions`` is the plain
#: tenant-scoped read every member holds; ``/api/tenant`` is deliberately *not*
#: listed for them because it demands ``tenant.info.read``.
PERMITTED = {
    "PA": ["/api/desktop/meta", "/api/platform/users", "/api/tenant/permissions"],
    "U1": ["/api/desktop/meta", "/api/tenant/permissions"],
    "U2": ["/api/desktop/meta", "/api/tenant/permissions"],
    "TA": ["/api/desktop/meta", "/api/tenant/permissions", "/api/tenant/roles"],
    "UP": ["/api/desktop/meta", "/api/tenant/permissions"],
    "FB": ["/api/desktop/meta"],
    "ANON": ["/api/desktop/meta"],
}

#: Operations each identity must NOT be able to perform.
DENIED = {
    "U1": ["/api/platform/users", "/api/tenant/roles"],
    "U2": ["/api/platform/users", "/api/tenant/roles"],
    "UP": ["/api/platform/users", "/api/tenant/roles"],
    "TA": ["/api/platform/users"],
    "FB": ["/api/platform/users", "/api/tenant/roles"],
    "ANON": ["/api/platform/users", "/api/tenant/permissions"],
}


class DesktopIdentities:
    """The synthetic cast, built on a :class:`tests._helpers.WebAppHarness`."""

    def __init__(self, harness) -> None:
        self.harness = harness
        self.tokens: Dict[str, str] = {}
        self.user_ids: Dict[str, str] = {}
        self.tenant_b: Dict[str, Any] = {}

    @classmethod
    def build(cls, harness) -> "DesktopIdentities":
        from tests._helpers import IdentityStack

        cast = cls(harness)
        h = harness
        h.add_agent("primary")

        member_role = h.role(
            "desktop-member",
            ["chat.use", "agent.use", "agent.read"],
            grants=[("agent", "primary", "read")],
        )
        for name in ("desktop-u1", "desktop-u2"):
            uid = h.member(name, [member_role["code"]])
            key = "U1" if name.endswith("u1") else "U2"
            cast.tokens[key] = h.login(name)
            cast.user_ids[key] = uid

        # A signed-in account with no qualification at all.
        bare_role = h.role("desktop-bare", [])
        cast.user_ids["UP"] = h.member("desktop-up", [bare_role["code"]])
        cast.tokens["UP"] = h.login("desktop-up")

        # The tenant administrator is a ``TenantStack`` helper, not a harness one.
        h.stack.tenant_admin()
        cast.tokens["TA"] = h.service.login("tenant-admin", IdentityStack.MEMBER_PASSWORD).token
        cast.user_ids["TA"] = h.stack.members["tenant-admin"]

        # The harness root is the platform administrator.
        cast.tokens["PA"] = h.login("root")
        cast.user_ids["PA"] = h.admin_id

        # Tenant B.
        cast.tenant_b = h.stack.other_tenant()
        cast.tokens["FB"] = h.service.login("foreign", IdentityStack.MEMBER_PASSWORD).token
        cast.user_ids["FB"] = cast.tenant_b["user_id"]

        return cast

    def request(self, identity: str, path: str):
        """One GET as ``identity`` (``ANON`` sends no credential)."""
        if identity == "ANON":
            return self.harness.get(path, token=None, tenant=False)
        return self.harness.get(path, token=self.tokens[identity])
