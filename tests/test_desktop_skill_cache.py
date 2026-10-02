# encoding:utf-8
"""The device-side skill version cache (change tasks 8.2 / 8.3).

The spec's requirement is that a skill's resources are *complete and deployed by
version* before anything runs: verified, published atomically, scoped per
server/tenant/user, with the version a run uses pinned so an upgrade cannot swap
resources out from under it. The failure modes each test below pins:

* a package that escapes the cache root (``..``, an absolute path, a link) being
  written anyway, so a "skill" becomes an arbitrary file write;
* a truncated or tampered payload being published because only its *presence*
  was checked and not its size/digest;
* a file that the manifest never declared riding along inside the package
  ("包外文件"), or a credential being packaged as skill content;
* an upgrade or a garbage collection deleting a directory a run is still using
  -- which is what the repository's existing whole-tree ``rmtree`` sync would do;
* the cache's mere *presence* being treated as authorization, so a revoked
  ``skill.use`` keeps working because the bytes happen to be on disk;
* one tenant's or user's cache leaking into another's.
"""

from __future__ import annotations

import copy
import hashlib
import os
import tempfile
import unittest


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def manifest_for(skill_id: str = "builtin:excel", *,
                 files: dict | None = None, **overrides) -> dict:
    """A package manifest, with resource bytes supplied by the caller.

    ``files`` maps a relative path to its bytes; the manifest's resource list is
    derived from it so a test can never accidentally declare one digest and ship
    another.
    """
    payloads = dict(files or {"SKILL.md": b"---\nname: excel\n---\n"})
    resources = [
        {"relative_path": rel, "digest": _digest(body), "size": len(body)}
        for rel, body in sorted(payloads.items())
    ]
    digest = "sha256:" + hashlib.sha256(
        "".join(f"{r['relative_path']}:{r['digest']};" for r in resources).encode()
    ).hexdigest()
    manifest = {
        "skill_id": skill_id,
        "name": skill_id.split(":", 1)[-1],
        "digest": digest,
        "platform": "posix",
        "dependencies": [],
        "resources": resources,
    }
    manifest.update(overrides)
    return manifest


def payloads_for(manifest: dict, files: dict | None = None) -> dict:
    return dict(files or {"SKILL.md": b"---\nname: excel\n---\n"})


DEFAULT_FILES = {"SKILL.md": b"---\nname: excel\n---\n"}


class _CacheCase(unittest.TestCase):
    def setUp(self):
        from agent.desktop_local import skill_cache

        self.mod = skill_cache
        self._tmp = tempfile.TemporaryDirectory(prefix="skill-cache-")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.cache = skill_cache.SkillCache(self.root)
        self.scope = skill_cache.SkillScope(
            origin="https://master.example", tenant_id="t-1", user_id="u-1")

    def publish(self, files=None, *, scope=None, skill_id="builtin:excel", **overrides):
        """Publish ``files`` with a manifest derived from exactly those bytes.

        Taking the bytes (not the manifest) is what keeps a test from declaring
        one digest and shipping another, which would make every digest check look
        like the thing under test when it is really a contradiction in the fixture.
        """
        files = dict(DEFAULT_FILES if files is None else files)
        manifest = manifest_for(skill_id, files=files, **overrides)
        return self.cache.publish(scope or self.scope, manifest, files)

    def errors(self):
        return self.mod.SkillCacheError


class PublishTests(_CacheCase):
    def test_a_verified_package_is_published_under_its_digest(self):
        published = self.publish()

        self.assertEqual(published.skill_id, "builtin:excel")
        self.assertEqual(published.digest, manifest_for()["digest"])
        self.assertTrue(os.path.isdir(published.path))
        self.assertEqual(
            open(os.path.join(published.path, "SKILL.md"), "rb").read(),
            b"---\nname: excel\n---\n")

    def test_the_published_version_is_addressed_by_digest_not_by_name(self):
        """A name alone cannot identify a version; the digest is the identity."""
        first = self.publish({"SKILL.md": b"v1"})
        second = self.publish({"SKILL.md": b"v2"})

        self.assertNotEqual(first.path, second.path)
        self.assertEqual(open(os.path.join(first.path, "SKILL.md"), "rb").read(), b"v1")
        self.assertEqual(open(os.path.join(second.path, "SKILL.md"), "rb").read(), b"v2")

    def test_publishing_the_same_version_twice_is_idempotent(self):
        first = self.publish()
        again = self.publish()

        self.assertEqual(first.path, again.path)
        self.assertTrue(os.path.isdir(again.path))

    def test_a_truncated_payload_is_refused_by_size(self):
        manifest = manifest_for()
        payloads = {"SKILL.md": b"short"}  # declared digest/size are for the full body

        with self.assertRaises(self.errors()) as caught:
            self.cache.publish(self.scope, manifest, payloads)

        self.assertEqual(caught.exception.code, "size_mismatch")
        self.assertEqual(self.cache.list_versions(self.scope, "builtin:excel"), [])

    def test_a_tampered_payload_is_refused_by_digest(self):
        """Same size, different bytes: only the digest can catch this.

        Deliberately length-preserving, so the size check cannot fire first and
        make this look like a digest test when it is really testing lengths.
        """
        manifest = manifest_for(files={"SKILL.md": b"A" * 32})
        tampered = {"SKILL.md": b"B" * 32}

        with self.assertRaises(self.errors()) as caught:
            self.cache.publish(self.scope, manifest, tampered)

        self.assertEqual(caught.exception.code, "digest_mismatch")
        self.assertEqual(self.cache.list_versions(self.scope, "builtin:excel"), [])

    def test_an_undeclared_file_cannot_ride_along(self):
        """Only what the manifest declares may be published (包外文件)."""
        manifest = manifest_for()
        payloads = dict(payloads_for(manifest))
        payloads["extra/backdoor.sh"] = b"rm -rf /"

        with self.assertRaises(self.errors()) as caught:
            self.cache.publish(self.scope, manifest, payloads)

        self.assertEqual(caught.exception.code, "undeclared_file")
        self.assertEqual(self.cache.list_versions(self.scope, "builtin:excel"), [])

    def test_a_declared_file_that_is_missing_is_refused(self):
        manifest = manifest_for(files={"SKILL.md": b"a", "templates/t.docx": b"b"})

        with self.assertRaises(self.errors()) as caught:
            self.cache.publish(self.scope, manifest, {"SKILL.md": b"a"})

        self.assertEqual(caught.exception.code, "missing_file")

    def test_a_failed_publish_leaves_no_partial_version_behind(self):
        manifest = manifest_for()
        with self.assertRaises(self.errors()):
            self.cache.publish(self.scope, manifest, {"SKILL.md": b"short"})

        skill_dir = self.cache.skill_dir(self.scope, "builtin:excel")
        leftovers = [n for n in os.listdir(skill_dir)] if os.path.isdir(skill_dir) else []
        self.assertEqual(leftovers, [], "no version is left behind")
        # Staging must be cleared too: a half-written directory parked next to
        # the real versions is how "never published" becomes "published".
        staging = os.path.join(self.cache.scope_root(self.scope), "staging")
        staged = [n for n in os.listdir(staging)] if os.path.isdir(staging) else []
        self.assertEqual(staged, [], "staging is cleaned up, not just abandoned")

    def test_a_crash_at_the_atomic_step_leaves_no_version_and_no_staging(self):
        """The one moment that matters: the move into place.

        Validation happens before staging, so a *rejected* package never reaches
        the cleanup path. This drives the failure that does -- the atomic rename
        itself -- so "either no version or the whole version" is tested rather
        than assumed.
        """
        from unittest.mock import patch

        manifest = manifest_for()
        payloads = payloads_for(manifest)

        with patch("os.replace", side_effect=OSError("device went away")):
            with self.assertRaises(OSError):
                self.cache.publish(self.scope, manifest, payloads)

        skill_dir = self.cache.skill_dir(self.scope, "builtin:excel")
        versions = [n for n in os.listdir(skill_dir)] if os.path.isdir(skill_dir) else []
        self.assertEqual(versions, [], "no half-published version")
        staging = os.path.join(self.cache.scope_root(self.scope), "staging")
        staged = [n for n in os.listdir(staging)] if os.path.isdir(staging) else []
        self.assertEqual(staged, [], "the staged copy is removed")


class PathAndSecretTests(_CacheCase):
    def test_a_traversing_resource_path_is_refused(self):
        manifest = manifest_for()
        manifest["resources"][0]["relative_path"] = "../../escaped.txt"

        with self.assertRaises(self.errors()) as caught:
            self.cache.publish(self.scope, manifest, {"../../escaped.txt": b"x"})

        self.assertEqual(caught.exception.code, "path_outside_package")

    def test_an_absolute_resource_path_is_refused(self):
        manifest = manifest_for()
        manifest["resources"][0]["relative_path"] = "/etc/passwd"

        with self.assertRaises(self.errors()) as caught:
            self.cache.publish(self.scope, manifest, {"/etc/passwd": b"x"})

        self.assertEqual(caught.exception.code, "path_outside_package")

    def test_a_credential_shaped_file_is_refused_as_skill_content(self):
        for name in (".env", "credentials.json", "id_rsa", "server.key", ".git/config"):
            with self.subTest(name=name):
                files = {"SKILL.md": b"ok", name: b"secret"}
                manifest = manifest_for(files=files)
                # Declared or not, a credential must not become skill content.
                with self.assertRaises(self.errors()) as caught:
                    self.cache.publish(self.scope, manifest, files)
                self.assertEqual(caught.exception.code, "secret_refused")

    def test_the_expansion_budget_is_enforced(self):
        cache = self.mod.SkillCache(self.root, limits={"expanded_max_bytes": 16})
        files = {"SKILL.md": b"x" * 64}
        manifest = manifest_for(files=files)

        with self.assertRaises(self.errors()) as caught:
            cache.publish(self.scope, manifest, files)

        self.assertEqual(caught.exception.code, "budget_exceeded")

    def test_the_file_count_budget_is_enforced(self):
        cache = self.mod.SkillCache(self.root, limits={"files_max": 2})
        files = {"SKILL.md": b"a", "b.txt": b"b", "c.txt": b"c"}
        manifest = manifest_for(files=files)

        with self.assertRaises(self.errors()) as caught:
            cache.publish(self.scope, manifest, files)

        self.assertEqual(caught.exception.code, "budget_exceeded")

    def test_the_transfer_budget_is_enforced(self):
        cache = self.mod.SkillCache(self.root, limits={"transfer_max_bytes": 4})
        files = {"SKILL.md": b"x" * 64}

        with self.assertRaises(self.errors()) as caught:
            cache.publish(self.scope, manifest_for(files=files), files)

        self.assertEqual(caught.exception.code, "budget_exceeded")

    def test_a_symlink_inside_a_published_version_is_refused_on_verify(self):
        """Defence in depth: the bytes are re-checked where they will be used."""
        published = self.publish({"SKILL.md": b"ok"})
        digest = published.digest
        os.symlink("/etc/passwd", os.path.join(published.path, "escape"))

        self.assertFalse(self.cache.verify(self.scope, "builtin:excel", digest))
        with self.assertRaises(self.errors()) as caught:
            self.cache.acquire(self.scope, "builtin:excel", digest)
        self.assertEqual(caught.exception.code, "link_refused")


class ScopeIsolationTests(_CacheCase):
    def test_two_tenants_never_share_a_published_version(self):
        other = self.mod.SkillScope(
            origin="https://master.example", tenant_id="t-2", user_id="u-1")
        mine = self.publish()
        theirs = self.publish(scope=other)

        self.assertNotEqual(mine.path, theirs.path)
        # Neither scope's root may contain the other's: sharing a *directory* is
        # what would let one tenant's grant read another's cached skill.
        mine_root = os.path.realpath(self.cache.scope_root(self.scope))
        theirs_root = os.path.realpath(self.cache.scope_root(other))
        self.assertFalse(mine_root.startswith(theirs_root + os.sep))
        self.assertFalse(theirs_root.startswith(mine_root + os.sep))

    def test_two_users_on_one_tenant_never_share(self):
        other = self.mod.SkillScope(
            origin="https://master.example", tenant_id="t-1", user_id="u-2")
        mine = self.publish()
        theirs = self.publish(scope=other)
        self.assertNotEqual(mine.path, theirs.path)

    def test_a_different_server_never_lets_a_tenant_name_collide(self):
        other = self.mod.SkillScope(
            origin="https://other.example", tenant_id="t-1", user_id="u-1")
        mine = self.publish()
        theirs = self.publish(scope=other)
        self.assertNotEqual(mine.path, theirs.path)

    def test_an_origin_cannot_climb_out_of_the_cache_root(self):
        hostile = self.mod.SkillScope(
            origin="../../../../etc", tenant_id="t", user_id="u")
        published = self.cache.publish(
            hostile, manifest_for(), payloads_for(manifest_for()))

        root = os.path.realpath(self.root)
        self.assertTrue(
            os.path.realpath(published.path).startswith(root + os.sep),
            "the version must stay under the cache root")


class RefCountAndGcTests(_CacheCase):
    def test_acquire_pins_a_version_and_release_unpins_it(self):
        published = self.publish()
        digest = published.digest

        path = self.cache.acquire(self.scope, "builtin:excel", digest)
        self.assertEqual(os.path.realpath(path), os.path.realpath(published.path))
        self.assertEqual(self.cache.refcount(self.scope, "builtin:excel", digest), 1)

        self.assertEqual(self.cache.release(self.scope, "builtin:excel", digest), 0)

    def test_release_is_not_allowed_to_go_negative(self):
        published = self.publish()
        digest = published.digest
        self.cache.release(self.scope, "builtin:excel", digest)
        self.assertEqual(self.cache.refcount(self.scope, "builtin:excel", digest), 0)

    def test_gc_never_removes_a_pinned_version(self):
        published = self.publish()
        digest = published.digest
        self.cache.acquire(self.scope, "builtin:excel", digest)

        removed = self.cache.gc(self.scope)

        self.assertEqual(removed, [])
        self.assertTrue(os.path.isdir(published.path))

    def test_gc_removes_an_unpinned_version(self):
        published = self.publish()
        digest = published.digest

        removed = self.cache.gc(self.scope)

        self.assertEqual(list(removed), [digest])
        self.assertFalse(os.path.exists(published.path))

    def test_gc_removes_only_the_versions_nothing_uses(self):
        """The repository's existing whole-tree sync would drop both."""
        used = self.publish({"SKILL.md": b"v1"})
        idle = self.publish({"SKILL.md": b"v2"})
        self.cache.acquire(self.scope, "builtin:excel", used.digest)

        removed = self.cache.gc(self.scope)

        self.assertEqual(list(removed), [idle.digest])
        self.assertTrue(os.path.isdir(used.path))
        self.assertFalse(os.path.exists(idle.path))

    def test_an_upgrade_cannot_replace_the_version_a_run_is_using(self):
        old = self.publish({"SKILL.md": b"v1"})
        self.cache.acquire(self.scope, "builtin:excel", old.digest)

        new = self.publish({"SKILL.md": b"v2"})

        self.assertNotEqual(old.path, new.path)
        self.assertEqual(open(os.path.join(old.path, "SKILL.md"), "rb").read(), b"v1")

    def test_moving_to_a_new_version_releases_the_old_one(self):
        old = self.publish({"SKILL.md": b"v1"})
        old_digest = old.digest
        self.cache.acquire(self.scope, "builtin:excel", old_digest)

        # The run moves on: it pins the new version and drops the old reference.
        self.cache.release(self.scope, "builtin:excel", old_digest)
        new = self.publish({"SKILL.md": b"v2"})
        self.cache.acquire(self.scope, "builtin:excel", new.digest)

        self.assertEqual(list(self.cache.gc(self.scope)), [old_digest])
        self.assertTrue(os.path.isdir(new.path))


class AuthorizationTests(_CacheCase):
    """Cache presence is not authorization (spec: 撤权后不能借缓存继续使用)."""

    def test_resolve_refuses_a_cached_skill_once_authorization_is_gone(self):
        published = self.publish()
        digest = published.digest

        self.assertEqual(
            self.cache.resolve(self.scope, "builtin:excel", digest,
                               authorized=lambda _sid: False),
            None)

    def test_resolve_returns_the_path_while_authorization_holds(self):
        published = self.publish()
        digest = published.digest

        resolved = self.cache.resolve(self.scope, "builtin:excel", digest,
                                      authorized=lambda _sid: True)
        self.assertEqual(os.path.realpath(resolved), os.path.realpath(published.path))

    def test_resolve_never_falls_back_to_an_older_cached_version(self):
        """A revoked-then-reauthorized skill must run exactly the asked-for one."""
        old = self.publish({"SKILL.md": b"v1"})
        new = self.publish({"SKILL.md": b"v2"})

        resolved = self.cache.resolve(
            self.scope, "builtin:excel", new.digest,
            authorized=lambda _sid: True)

        self.assertEqual(os.path.realpath(resolved), os.path.realpath(new.path))
        self.assertNotEqual(os.path.realpath(resolved), os.path.realpath(old.path))

    def test_acquire_is_gated_by_the_authorization_check_as_well(self):
        published = self.publish()
        digest = published.digest
        cache = self.mod.SkillCache(self.root, authorized=lambda _sid: False)

        with self.assertRaises(self.errors()) as caught:
            cache.acquire(self.scope, "builtin:excel", digest)
        self.assertEqual(caught.exception.code, "not_authorized")


class ContractAndSharingTests(_CacheCase):
    """The two ends must agree, and the numbers must be the contract's."""

    def test_the_default_limits_are_the_contracts_own_numbers(self):
        """A limit invented here would be unreviewable; these are checked in."""
        import json

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "contracts", "desktop", "v2.json"),
                  encoding="utf-8") as handle:
            limits = json.load(handle)["limits"]

        self.assertEqual(self.mod.DEFAULT_LIMITS["transfer_max_bytes"],
                         limits["skill_package_transfer_max_bytes"])
        self.assertEqual(self.mod.DEFAULT_LIMITS["expanded_max_bytes"],
                         limits["skill_package_expanded_max_bytes"])
        self.assertEqual(self.mod.DEFAULT_LIMITS["files_max"],
                         limits["skill_package_files_max"])

    def test_a_skill_cache_error_speaks_the_shared_rule_codes(self):
        """One definition of the rules, so the server cannot accept what the
        device refuses (or the reverse). The cache must raise the shared codes,
        not a private paraphrase of them."""
        from agent.desktop_local import package_rules

        cases = [
            ({"../../x": b"x"}, "path_outside_package"),
            ({".env": b"x"}, "secret_refused"),
        ]
        for files, code in cases:
            with self.subTest(code=code):
                manifest = manifest_for(files=files)
                with self.assertRaises(self.errors()) as caught:
                    self.cache.publish(self.scope, manifest, files)
                # The code is the shared one, and it is what package_rules calls
                # the same refusal.
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(hasattr(package_rules, "PackageRuleError"))

    def test_the_manifest_builder_and_the_cache_share_these_rules(self):
        """A package the server builds must be one the device accepts.

        This is the seam that would otherwise drift silently: the server would
        happily *build* a package the device then refuses, and the failure would
        look like a transfer problem rather than two disagreeing rulebooks.
        """
        from agent.skills.manifest import build_skill_manifest
        from agent.skills.types import Skill, SkillEntry

        skill_dir = os.path.join(self.root, "authoring", "excel")
        os.makedirs(os.path.join(skill_dir, "scripts"))
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nname: excel\ndescription: d\n---\n")
        with open(os.path.join(skill_dir, "scripts", "fill.py"), "w",
                  encoding="utf-8") as handle:
            handle.write("print('fill')\n")
        skill = Skill(name="excel", description="d",
                      file_path=os.path.join(skill_dir, "SKILL.md"),
                      base_dir=skill_dir, source="builtin", content="", frontmatter={})

        built = build_skill_manifest(
            SkillEntry(skill=skill), platform="posix",
            is_authorized=lambda _sid: True)
        payloads = read_payloads(built)
        published = self.cache.publish(self.scope, built.to_dict(), payloads)

        self.assertEqual(published.digest, built.digest)
        self.assertTrue(self.cache.verify(self.scope, "builtin:excel", built.digest))

    def test_the_manifest_never_carries_the_servers_own_path_to_the_device(self):
        """The wire form is what the device sees; a server path in it would be
        the "服务器绝对路径直接传入本机脚本" the spec forbids."""
        from agent.skills.manifest import SkillManifest, SkillResource

        built = SkillManifest(
            skill_id="builtin:excel", name="excel", digest="sha256:x",
            platform="posix",
            resources=(SkillResource("SKILL.md", "sha256:y", 1),),
            base_dir="/srv/secret/skills/excel")

        wire = built.to_dict()
        self.assertNotIn("base_dir", wire)
        self.assertNotIn("/srv", repr(wire))


def read_payloads(built):
    from agent.skills.manifest import read_skill_payloads
    return read_skill_payloads(built)
