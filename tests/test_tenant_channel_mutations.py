# encoding:utf-8
"""Mutation check for the tenant-owned channel isolation guards (10.3).

A test that passes is only meaningful if it would FAIL when the guard it claims
to protect is removed. This harness applies a small, surgical mutation to each
isolation guard, runs the tests that claim to protect it, and asserts that they
fail. It then restores the source exactly.

Run directly (``python -m tests.test_tenant_channel_mutations``) or under
unittest. It writes no files and leaves the tree as it found it — a failure
restores the source before raising.
"""

import filecmp
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(ROOT, ".venv", "bin", "python")


def _run(test_ids):
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    # The mutation is written immediately before this subprocess starts, and
    # CPython validates a cached ``.pyc`` by the source's size *and* mtime. On a
    # filesystem with coarse timestamps the rewritten file can carry the same
    # stamp as the one the cache was built from, so the subprocess would import
    # the *unmutated* bytecode and report the guard as surviving when it did not.
    # Dropping the caches for the imported tree removes that whole class of flake
    # (and prevents the run from writing new ones back).
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Only the project's own packages: walking the virtualenv as well would throw
    # away its caches too, which is slow and unrelated to the mutation.
    skip = {".venv", ".git", "node_modules", "__pycache__"}
    for base, dirs, _files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip]
        cache = os.path.join(base, "__pycache__")
        if os.path.isdir(cache):
            shutil.rmtree(cache, ignore_errors=True)
    proc = subprocess.run(
        [PYTHON, "-m", "pytest", "-q", *test_ids],
        cwd=ROOT, env=env, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


class MutationTests(unittest.TestCase):
    """Each case: a file plus one or more (old, new) edits, and the tests that
    must flip to failing once it is applied."""

    MUTATIONS = [
        # 1. Drop the tenant predicate from the Agent check: a tenant could then
        #    bind another tenant's Agent and route inbound traffic into it.
        (
            "auth/service.py",
            [('if not binding or binding["tenant_id"] != tenant_id:',
              'if not binding:')],
            ["tests/test_tenant_channel_instances_service.py::"
             "CreateInstanceTests::test_agent_from_another_tenant_is_rejected",
             "tests/test_tenant_channel_isolation_acceptance.py::"
             "CrossTenantIsolationAcceptance::"
             "test_binding_another_tenants_agent_is_refused"],
        ),
        # 2. Drop the tenant predicate from the list query: one tenant's instances
        #    would appear in another tenant's console.
        #
        #    The query now answers a *range* (task 6.1) — the tenant's public rows
        #    plus the caller's own — so the anchor is the administrative branch's
        #    ``WHERE``. The member branch reads ``tenant_id=? AND scope='user'``,
        #    which is a different string, hence the anchor is still unique.
        (
            "auth/service.py",
            [('                " WHERE tenant_id=? AND (scope=\'tenant\'"',
              '                " WHERE (scope=\'tenant\'"')],
            ["tests/test_tenant_channel_instances_service.py::"
             "ListInstanceTests::test_list_never_returns_another_tenants_instances",
             "tests/test_tenant_channel_isolation_acceptance.py::"
             "CrossTenantIsolationAcceptance::"
             "test_b_tenant_cannot_see_a_tenants_instances"],
        ),
        # 3. Reintroducing the webhook singleton merges two tenants' runtime
        #    identity and credentials. The real factory isolation test must fail.
        (
            "channel/channel_factory.py",
            [("ch = _fresh(WechatComAppChannel, defer_init=True) if multi_instance else WechatComAppChannel()",
              "ch = WechatComAppChannel(defer_init=True)")],
            ["tests/test_tenant_channel_type_gate.py::"
             "test_webhook_instances_initialize_with_their_own_credentials"],
        ),
        # 4. Drop the ownership half of the range: every member's personal
        #    instance would surface in the administration list, which is not the
        #    tenant's to administer. ``? IS NOT NULL`` keeps the binding count and
        #    makes the clause always true for an authenticated caller — the
        #    binding is a real user id, never NULL.
        (
            "auth/service.py",
            [('                "     OR (scope=\'user\' AND owner_user_id=?))"',
              '                "     OR (scope=\'user\' AND ? IS NOT NULL))"')],
            ["tests/test_tenant_channel_instances_service.py::"
             "InstanceScopeTests::"
             "test_the_public_list_does_not_expose_personal_instances"],
        ),
        # 5. Drop the ownership half of a *member's* range: a plain member would
        #    list every colleague's personal instance in the tenant. This is the
        #    guard task 6.1 added — before it, a member was refused the listing
        #    outright, so there was nothing to widen. The same always-true
        #    substitution keeps the binding count, so the mutation reaches the
        #    assertion instead of dying on a binding error.
        (
            "auth/service.py",
            [('        else:\n'
              '            rows = self._store.execute(\n'
              '                "SELECT * FROM tenant_channel_instances"\n'
              '                " WHERE tenant_id=? AND scope=\'user\' AND owner_user_id=?"',
              '        else:\n'
              '            rows = self._store.execute(\n'
              '                "SELECT * FROM tenant_channel_instances"\n'
              '                " WHERE tenant_id=? AND scope=\'user\' AND ? IS NOT NULL"')],
            ["tests/test_tenant_channel_member_access.py::"
             "MemberListingTests::"
             "test_a_colleagues_connection_never_appears_for_this_member"],
        ),
    ]

    def _assert_mutation_caught(self, relpath, edits, test_ids):
        target = os.path.join(ROOT, relpath)
        backup_dir = tempfile.mkdtemp()
        backup = os.path.join(backup_dir, os.path.basename(relpath))
        shutil.copy2(target, backup)
        try:
            with open(target, encoding="utf-8") as fh:
                src = fh.read()
            mutated = src
            for old, new in edits:
                self.assertEqual(mutated.count(old), 1,
                                 f"mutation anchor not unique in {relpath}: {old!r}")
                mutated = mutated.replace(old, new)
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(mutated)

            code, output = _run(test_ids)
            self.assertNotEqual(
                code, 0,
                f"the mutated guard survived — these tests did not catch it:\n{output}")
        finally:
            shutil.copy2(backup, target)
            shutil.rmtree(backup_dir, ignore_errors=True)

        # The restore must be exact, or the next run would start from a mutant.
        with open(target, encoding="utf-8") as fh:
            restored = fh.read()
        self.assertEqual(restored, src, f"{relpath} was not restored exactly")

    def test_each_isolation_guard_is_pinned_by_a_failing_test(self):
        if not os.path.exists(PYTHON):
            self.skipTest("no .venv interpreter")
        for relpath, edits, test_ids in self.MUTATIONS:
            with self.subTest(mutation=relpath):
                self._assert_mutation_caught(relpath, edits, test_ids)


if __name__ == "__main__":
    unittest.main()
