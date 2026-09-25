# encoding:utf-8
"""Object data scope (change ``unify-console-by-data-scope``, task 2.1).

:mod:`auth.object_scope` is the single authority that answers "is this object in
the caller's range?" for Agents, memories and channel instances. These tests pin
the two invariants the module exists for, because every list/detail/write path
now reads them and a single lax branch would reopen a cross-member leak:

1. the range is **derived** from a verified context, never supplied by a body;
2. the **owner check precedes the administrator exception** — a private object
   is decided by ownership alone, so a tenant administrator reaches a private
   Agent/memory/connection only as its owner.

The negative cases are the point: a predicate that returned ``False`` for
everything, or ``is_admin`` for everything, would fail one of the two directions
asserted here.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from auth.object_scope import (  # noqa: E402
    CHANNEL_SCOPE_TENANT,
    CHANNEL_SCOPE_USER,
    MANAGE,
    USE,
    ObjectScope,
)


class _Ctx:
    """A minimal stand-in for ``auth.runtime.RequestContext``."""

    def __init__(self, user_id="u1", tenant_id="t1", *, platform=False, tenant_admin=False):
        self.user_id = user_id
        self.tenant_id = tenant_id
        self.is_platform_admin = platform
        self.is_tenant_admin = tenant_admin


def _member(user_id="u1", tenant_id="t1"):
    return ObjectScope.from_context(_Ctx(user_id, tenant_id))


def _tenant_admin(user_id="admin", tenant_id="t1"):
    return ObjectScope.from_context(_Ctx(user_id, tenant_id, tenant_admin=True))


def _platform_admin(user_id="root", tenant_id="t1"):
    return ObjectScope.from_context(_Ctx(user_id, tenant_id, platform=True))


def _binding(tenant_id="t1", owner=None, agent_id="a1"):
    return {"agent_id": agent_id, "tenant_id": tenant_id, "private_owner_user_id": owner}


def _channel(scope, owner=None, tenant_id="t1"):
    return {"tenant_id": tenant_id, "scope": scope, "owner_user_id": owner}


class DerivationTests(unittest.TestCase):
    """The range is derived from the context; a missing one fails closed."""

    def test_a_missing_context_matches_nothing(self):
        scope = ObjectScope.from_context(None)
        self.assertFalse(scope.allows_agent(_binding(), action=USE))
        self.assertFalse(scope.allows_agent(_binding(), action=MANAGE))
        self.assertFalse(scope.allows_personal_memory())
        self.assertFalse(scope.allows_channel_instance(_channel(CHANNEL_SCOPE_TENANT)))
        self.assertFalse(scope.allows_public_configuration())

    def test_no_selected_tenant_matches_nothing(self):
        """A caller with no tenant selected has an empty range, not the whole roster."""
        scope = ObjectScope.from_context(_Ctx(tenant_id=None, platform=True))
        self.assertFalse(scope.allows_agent(_binding(), action=USE))
        self.assertFalse(scope.allows_personal_memory())

    def test_a_platform_admin_is_collapsed_to_the_tenant_qualification(self):
        """``all`` confers the *same* business reach as tenant admin, no wider.

        Platform administration is a cross-tenant *entry* concern
        (``/api/platform/tenants/<id>/agents``); it must not widen the data scope
        of a request that already selected one tenant.
        """
        scope = _platform_admin()
        self.assertTrue(scope.allows_agent(_binding(owner=None), action=MANAGE))
        self.assertFalse(scope.allows_agent(_binding(tenant_id="other"), action=USE))

    def test_the_range_is_frozen(self):
        scope = _member()
        with self.assertRaises(Exception):
            scope.tenant_id = "other"  # type: ignore[misc]


class AgentScopeTests(unittest.TestCase):
    """Agent range: shared is manageable by administration, private by ownership."""

    def test_a_member_uses_a_shared_agent_but_does_not_manage_it(self):
        scope = _member()
        shared = _binding(owner=None)
        self.assertTrue(scope.allows_agent(shared, action=USE))
        self.assertFalse(scope.allows_agent(shared, action=MANAGE))

    def test_administration_manages_a_shared_agent(self):
        self.assertTrue(_tenant_admin().allows_agent(_binding(owner=None), action=MANAGE))
        self.assertTrue(_platform_admin().allows_agent(_binding(owner=None), action=MANAGE))

    def test_the_owner_reaches_their_own_private_agent(self):
        scope = _member(user_id="rock")
        mine = _binding(owner="rock")
        self.assertTrue(scope.allows_agent(mine, action=MANAGE))
        self.assertTrue(scope.allows_agent(mine, action=USE))

    def test_the_owner_check_precedes_the_administrator_exception(self):
        """The invariant: an administrator is a non-owner like any other here."""
        for scope in (_member(user_id="jane"), _tenant_admin(user_id="admin"),
                      _platform_admin(user_id="root")):
            with self.subTest(scope=scope.user_id):
                anothers = _binding(owner="rock")
                self.assertFalse(scope.allows_agent(anothers, action=MANAGE))
                self.assertFalse(scope.allows_agent(anothers, action=USE))

    def test_a_cross_tenant_object_is_out_of_range_for_everyone(self):
        other = _binding(tenant_id="t2", owner=None)
        self.assertFalse(_tenant_admin().allows_agent(other, action=MANAGE))
        self.assertFalse(_platform_admin().allows_agent(other, action=MANAGE))


class MemoryScopeTests(unittest.TestCase):
    """Personal memory is always the caller's own; shared memory needs admin."""

    def test_personal_memory_is_in_scope_for_a_valid_tenant_and_user(self):
        self.assertTrue(_member().allows_personal_memory())
        self.assertTrue(_tenant_admin().allows_personal_memory())

    def test_personal_memory_is_out_of_scope_without_a_user_or_tenant(self):
        self.assertFalse(ObjectScope.from_context(_Ctx(user_id=None)).allows_personal_memory())
        self.assertFalse(ObjectScope.from_context(_Ctx(tenant_id=None)).allows_personal_memory())

    def test_an_administrator_has_no_wider_personal_memory_range(self):
        """There is no administrator branch: the predicate is self-only by shape.

        The personal root is derived from the verified identity
        (``<users_root>/<user_id>``), so an administrator cannot name another
        member's root. This pins that no such parameter exists to be passed.
        """
        import inspect

        params = list(inspect.signature(ObjectScope.allows_personal_memory).parameters)
        self.assertEqual(params, ["self"])
        self.assertTrue(_tenant_admin().allows_personal_memory())

    def test_agent_memory_follows_the_management_range(self):
        """A shared Agent's memory is a tenant resource; chat use does not confer it."""
        member = _member()
        shared = _binding(owner=None)
        self.assertFalse(member.allows_agent_memory(shared))
        self.assertTrue(_tenant_admin().allows_agent_memory(shared))
        self.assertTrue(member.allows_agent_memory(_binding(owner="u1")))
        self.assertFalse(_tenant_admin().allows_agent_memory(_binding(owner="rock")))


class ChannelInstanceScopeTests(unittest.TestCase):
    """Channel instances: a private connection is decided by ownership alone."""

    def test_a_member_manages_their_own_user_connection(self):
        self.assertTrue(_member(user_id="u1").allows_channel_instance(
            _channel(CHANNEL_SCOPE_USER, "u1")))

    def test_another_members_connection_is_refused_even_for_an_administrator(self):
        row = _channel(CHANNEL_SCOPE_USER, "rock")
        self.assertFalse(_member(user_id="jane").allows_channel_instance(row))
        self.assertFalse(_tenant_admin().allows_channel_instance(row))
        self.assertFalse(_platform_admin().allows_channel_instance(row))

    def test_an_ownerless_user_connection_is_refused(self):
        self.assertFalse(_member().allows_channel_instance(
            _channel(CHANNEL_SCOPE_USER, None)))

    def test_a_tenant_connection_needs_administration(self):
        row = _channel(CHANNEL_SCOPE_TENANT)
        self.assertFalse(_member().allows_channel_instance(row))
        self.assertTrue(_tenant_admin().allows_channel_instance(row))
        self.assertTrue(_platform_admin().allows_channel_instance(row))

    def test_an_unknown_scope_never_falls_through_to_public(self):
        """A legacy/unusable scope is ownership-first, then administration."""
        self.assertFalse(_tenant_admin().allows_channel_instance(_channel("legacy")))
        self.assertTrue(_tenant_admin().allows_channel_instance(_channel("legacy", "admin")))
        self.assertFalse(_tenant_admin().allows_channel_instance(_channel("legacy", "rock")))

    def test_a_cross_tenant_connection_is_out_of_range(self):
        row = _channel(CHANNEL_SCOPE_TENANT, tenant_id="t2")
        self.assertFalse(_tenant_admin().allows_channel_instance(row))
        self.assertFalse(_member().allows_channel_instance(
            _channel(CHANNEL_SCOPE_USER, "u1", tenant_id="t2")))


class PublicConfigurationScopeTests(unittest.TestCase):
    """Public definitions/credentials need administration on its own."""

    def test_only_administration_maintains_public_configuration(self):
        self.assertFalse(_member().allows_public_configuration())
        self.assertTrue(_tenant_admin().allows_public_configuration())
        self.assertTrue(_platform_admin().allows_public_configuration())


class AgentUserSubtreeScopeTests(unittest.TestCase):
    """``user/<user_id>`` under an Agent workspace is owner-decided.

    Change ``isolate-shared-agent-user-data`` (task 2.2): the platform file
    surface reads this predicate *before* any shared-root or administrator
    pass-through, so sharing the Agent must not hand one member another
    member's files.
    """

    def setUp(self):
        import shutil
        import tempfile

        self.ws = tempfile.mkdtemp(prefix="cow-scope-user-")
        self.addCleanup(shutil.rmtree, self.ws, True)
        self.mine = os.path.join(self.ws, "user", "u1", "uploads", "a.txt")
        self.theirs = os.path.join(self.ws, "user", "rock", "uploads", "a.txt")
        os.makedirs(os.path.dirname(self.mine))
        os.makedirs(os.path.dirname(self.theirs))
        for path in (self.mine, self.theirs):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("x")

    def test_a_member_owns_only_their_own_subtree(self):
        scope = _member(user_id="u1")
        self.assertTrue(scope.owns_agent_user_subtree(self.mine, self.ws))
        self.assertFalse(scope.owns_agent_user_subtree(self.theirs, self.ws))

    def test_no_verified_user_owns_nothing(self):
        self.assertFalse(_member(user_id=None).owns_agent_user_subtree(
            self.mine, self.ws))

    def test_administrators_get_no_shortcut(self):
        self.assertFalse(_tenant_admin(user_id="admin").owns_agent_user_subtree(
            self.mine, self.ws))
        self.assertFalse(_platform_admin(user_id="root").owns_agent_user_subtree(
            self.mine, self.ws))

    def test_container_and_unowned_paths_are_not_owned(self):
        scope = _member(user_id="u1")
        self.assertFalse(scope.owns_agent_user_subtree(
            os.path.join(self.ws, "user"), self.ws))
        stray = os.path.join(self.ws, "user", "not a user", "x")
        os.makedirs(os.path.dirname(stray))
        self.assertFalse(scope.owns_agent_user_subtree(stray, self.ws))

    def test_ordinary_workspace_paths_are_not_owned(self):
        ordinary = os.path.join(self.ws, "reports", "q1.csv")
        os.makedirs(os.path.dirname(ordinary))
        self.assertFalse(_member().owns_agent_user_subtree(ordinary, self.ws))


if __name__ == "__main__":
    unittest.main()
