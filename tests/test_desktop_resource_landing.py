# encoding:utf-8
"""Server attachments become local inputs only by explicit landing (task 8.6).

The contract (``execution-contract.md`` §6) is one sentence with three obligations
in it:

    服务器附件用于本机任务时，资源准备返回 ``(resource_id, version, digest)``；
    客户端先下载到本轮临时输入目录并校验，再向工具提供执行端位置。

and one prohibition: reading a remote MCP native reference "同理，不把
``server:/...`` 当本机路径".

So a server-side attachment reaches a local tool through a **verified landing**,
never by path resolution. This file pins the verification, because a landing
without it is worse than a refusal: the tool would read *something* and the model
would report success, while nobody can say whether the bytes are the ones the
server authorized.

The checks, in the order they must happen:

1. **Version** -- a resource whose version is not the one requested is not
   "close enough"; a pin that drifts is ``resource_unavailable``.
2. **Digest of what was fetched** -- the fetcher's own claim must match the bytes
   it handed over. This is the half that catches a corrupt or substituted
   transfer, and it is independent of whether the caller knew a digest.
3. **Digest the caller pinned** -- when the reference carries one, the landed
   bytes must match it.
4. **Atomicity** -- verification happens *before* the file becomes visible, so a
   failed check cannot leave a half-file for a later tool to read. A partial file
   that exists is indistinguishable from a good one to the next caller.
5. **Idempotence per (id, digest)** -- a run that stages the same attachment for
   five tool calls downloads and verifies once, and the second call must not
   re-publish over a file another call may be reading.

Also pinned here: the landing root is **given**, never derived from a request, and
a ``resource_id`` cannot steer the write out of it. A resource id is server input,
so it is exactly the value that must not be trusted as a path.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest

from agent.desktop_local import resource_landing as landing_mod


def digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeFetcher:
    """A fetcher whose answers a test controls, including lying ones."""

    def __init__(self, data: bytes = b"payload", *, version: str = "v1",
                 digest: str = "", name: str = "", error=None):
        self.data = data
        self.version = version
        self.digest = digest or digest_of(data)
        self.name = name
        self.error = error
        self.calls = 0

    def __call__(self, resource_id: str):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return landing_mod.FetchedResource(
            version=self.version, digest=self.digest, data=self.data,
            name=self.name)


class LandingCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="landing-")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def landing(self, fetcher, **kwargs):
        return landing_mod.ResourceLanding(fetcher, self.root, **kwargs)

    def request(self, resource_id: str = "att_1", **kwargs):
        return landing_mod.LandingRequest(resource_id=resource_id, **kwargs)


class HappyPathTests(LandingCase):
    def test_a_verified_resource_lands_inside_the_root(self):
        fetcher = FakeFetcher(b"hello")
        result = self.landing(fetcher).land(self.request())

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.digest, digest_of(b"hello"))
        self.assertEqual(result.size, 5)
        path = os.path.join(self.root, result.relative)
        self.assertTrue(os.path.isfile(path))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"hello")

    def test_the_landed_location_is_inside_the_given_root(self):
        fetcher = FakeFetcher(b"x")
        result = self.landing(fetcher).land(self.request())

        self.assertTrue(
            os.path.realpath(result.absolute).startswith(os.path.realpath(self.root)))

    def test_a_caller_pinned_digest_that_matches_is_accepted(self):
        data = b"hello"
        fetcher = FakeFetcher(data)
        result = self.landing(fetcher).land(self.request(digest=digest_of(data)))

        self.assertTrue(result.ok, result.message)

    def test_a_run_downloads_a_resource_once_and_reuses_it(self):
        """Five tool calls staging the same attachment must not mean five copies."""
        fetcher = FakeFetcher(b"hello")
        engine = self.landing(fetcher)

        first = engine.land(self.request())
        second = engine.land(self.request())

        self.assertTrue(first.ok and second.ok)
        self.assertEqual(fetcher.calls, 1, "the second landing must not re-fetch")
        self.assertTrue(second.reused)
        self.assertEqual(first.relative, second.relative)

    def test_a_cached_landing_does_not_answer_a_different_pin(self):
        """A pin the cached copy does not satisfy must not be answered from cache.

        Without this, a caller who pins digest B would be handed the path of an
        earlier landing of digest A and told it was verified -- a cache hit that
        is itself the unverified read 8.6 exists to prevent.
        """
        fetcher = FakeFetcher(b"hello")
        engine = self.landing(fetcher)

        first = engine.land(self.request())
        second = engine.land(self.request(digest=digest_of(b"other")))

        self.assertTrue(first.ok, first.message)
        self.assertFalse(second.ok)
        self.assertEqual(second.code, "resource_digest_mismatch")

    def test_reuse_is_scoped_to_the_requested_version(self):
        """A re-pinned version is a different resource, not a cached answer."""
        fetcher = FakeFetcher(b"hello", version="v1")
        engine = self.landing(fetcher)
        first = engine.land(self.request(version="v1"))

        fetcher.version = "v2"
        fetcher.data = b"hello v2"
        fetcher.digest = digest_of(b"hello v2")
        second = engine.land(self.request(version="v2"))

        self.assertTrue(first.ok and second.ok)
        self.assertNotEqual(first.digest, second.digest)
        self.assertFalse(second.reused)


class VersionTests(LandingCase):
    def test_a_fetched_version_that_drifts_from_the_pin_is_refused(self):
        fetcher = FakeFetcher(b"hello", version="v2")
        result = self.landing(fetcher).land(self.request(version="v1"))

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "resource_unavailable")

    def test_an_unpinned_request_accepts_whatever_version_arrives(self):
        fetcher = FakeFetcher(b"hello", version="v9")
        result = self.landing(fetcher).land(self.request())

        self.assertTrue(result.ok, result.message)


class DigestTests(LandingCase):
    def test_a_payload_whose_bytes_do_not_match_its_own_digest_is_refused(self):
        """The fetcher's claim must match what it handed over.

        This is the check that does not depend on the caller having pinned
        anything: a corrupt or substituted transfer is caught here.
        """
        fetcher = FakeFetcher(b"hello")
        fetcher.digest = digest_of(b"something else")
        result = self.landing(fetcher).land(self.request())

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "resource_digest_mismatch")

    def test_a_payload_that_fails_the_callers_pin_is_refused(self):
        fetcher = FakeFetcher(b"hello")
        result = self.landing(fetcher).land(self.request(digest=digest_of(b"other")))

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "resource_digest_mismatch")

    def test_a_digest_may_be_written_with_a_sha256_prefix(self):
        data = b"hello"
        fetcher = FakeFetcher(data)
        result = self.landing(fetcher).land(
            self.request(digest=f"sha256:{digest_of(data)}"))

        self.assertTrue(result.ok, result.message)

    def test_a_tampered_resource_leaves_no_file_behind(self):
        """Verification precedes publication, so a bad landing is invisible.

        A leftover partial file is the failure mode this pins: to the next
        caller it is indistinguishable from a good one.
        """
        fetcher = FakeFetcher(b"hello")
        result = self.landing(fetcher).land(self.request(digest=digest_of(b"other")))

        self.assertFalse(result.ok)
        self.assertEqual(os.listdir(self.root), [],
                         "a refused landing must not leave a partial or final file")

    def test_an_unparsable_pin_is_refused_rather_than_ignored(self):
        """A typo'd pin must not silently downgrade to "no pin".

        Treating an unparsable digest as absent would turn a mistake into an
        *unverified* landing, which is worse than a refusal: the tool reads
        something and the run reports success.
        """
        fetcher = FakeFetcher(b"hello")
        result = self.landing(fetcher).land(self.request(digest="not-a-digest"))

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_reference")
        self.assertEqual(os.listdir(self.root), [])

    def test_a_source_reporting_no_digest_is_refused_without_a_caller_pin(self):
        """One of the two must be verifiable, or nothing is verified."""
        fetcher = FakeFetcher(b"hello")
        fetcher.digest = ""
        result = self.landing(fetcher).land(self.request())

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "resource_unavailable")
        self.assertEqual(os.listdir(self.root), [])

    def test_a_source_reporting_no_digest_is_fine_when_the_caller_pinned_one(self):
        fetcher = FakeFetcher(b"hello")
        fetcher.digest = ""
        result = self.landing(fetcher).land(self.request(digest=digest_of(b"hello")))

        self.assertTrue(result.ok, result.message)

    def test_a_source_whose_digest_is_unusable_is_refused(self):
        fetcher = FakeFetcher(b"hello")
        fetcher.digest = "definitely not a digest"
        result = self.landing(fetcher).land(self.request())

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "resource_digest_mismatch")

    def test_a_tampered_resource_does_not_become_reusable(self):
        """The refusal must not be remembered as a successful landing."""
        fetcher = FakeFetcher(b"hello")
        engine = self.landing(fetcher)
        first = engine.land(self.request(digest=digest_of(b"other")))
        second = engine.land(self.request())

        self.assertFalse(first.ok)
        self.assertTrue(second.ok, second.message)
        self.assertFalse(second.reused)


class RootAndPathSafetyTests(LandingCase):
    def test_a_resource_id_cannot_escape_the_landing_root(self):
        """A resource id is server input; it must not steer the write."""
        fetcher = FakeFetcher(b"hello")
        result = self.landing(fetcher).land(self.request(resource_id="../../etc/passwd"))

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_reference")

    def test_an_absolute_resource_id_is_refused(self):
        fetcher = FakeFetcher(b"hello")
        result = self.landing(fetcher).land(self.request(resource_id="/etc/passwd"))

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_reference")

    def test_a_backslash_resource_id_is_refused(self):
        fetcher = FakeFetcher(b"hello")
        result = self.landing(fetcher).land(self.request(resource_id="..\\evil"))

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "invalid_reference")

    def test_a_nested_resource_id_lands_in_a_nested_directory(self):
        fetcher = FakeFetcher(b"hello")
        result = self.landing(fetcher).land(self.request(resource_id="a/b/c.txt"))

        self.assertTrue(result.ok, result.message)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "a", "b", "c.txt")))

    def test_a_missing_root_is_a_refusal_not_a_created_directory(self):
        """The root is granted, never provisioned from a request."""
        missing = os.path.join(self.root, "nope")
        engine = landing_mod.ResourceLanding(FakeFetcher(b"x"), missing)
        result = engine.land(self.request())

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "resource_unavailable")
        self.assertFalse(os.path.exists(missing))

    def test_a_relative_root_is_refused(self):
        engine = landing_mod.ResourceLanding(FakeFetcher(b"x"), "relative/dir")
        result = engine.land(self.request())

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "resource_unavailable")


class LimitTests(LandingCase):
    def test_a_payload_over_the_cap_is_refused_without_landing(self):
        fetcher = FakeFetcher(b"x" * 100)
        result = self.landing(fetcher, max_bytes=10).land(self.request())

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "limit_exceeded")
        self.assertEqual(os.listdir(self.root), [])

    def test_a_declared_size_over_the_cap_is_refused_before_fetching(self):
        """The declaration is a cheap check; honouring it avoids the download."""
        fetcher = FakeFetcher(b"x")
        result = self.landing(fetcher, max_bytes=10).land(self.request(size=1000))

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "limit_exceeded")
        self.assertEqual(fetcher.calls, 0, "a declared oversize must not be fetched")


class FetchFailureTests(LandingCase):
    def test_a_fetcher_that_raises_is_a_refusal_not_an_exception(self):
        fetcher = FakeFetcher(error=RuntimeError("network down"))
        result = self.landing(fetcher).land(self.request())

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "resource_unavailable")

    def test_a_fetcher_that_returns_nothing_is_a_refusal(self):
        result = self.landing(lambda _rid: None).land(self.request())

        self.assertFalse(result.ok)
        self.assertEqual(result.code, "resource_unavailable")

    def test_an_empty_resource_is_landed_not_treated_as_a_failure(self):
        """Empty is a real answer; only "no answer" is a refusal."""
        fetcher = FakeFetcher(b"")
        result = self.landing(fetcher).land(self.request())

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.size, 0)


class ReferenceTests(unittest.TestCase):
    """``resource:`` is recognised, and never mistaken for a project path."""

    def setUp(self):
        from agent.desktop_local import resource_refs

        self.refs = resource_refs

    def test_a_resource_reference_parses_with_its_pin(self):
        parsed = self.refs.parse_ref("resource:att_1@v7#sha256:abc")

        self.assertEqual(parsed.kind, self.refs.RESOURCE)
        self.assertEqual(parsed.relative, "att_1")
        self.assertEqual(parsed.version, "v7")
        # Kept verbatim, not normalized: the reference has to round-trip exactly,
        # and only the landing side knows what a digest is allowed to look like.
        self.assertEqual(parsed.digest, "sha256:abc")

    def test_a_resource_reference_round_trips_its_digest_prefix(self):
        original = "resource:a/b.txt@v7#sha256:abc"
        parsed = self.refs.parse_ref(original)

        self.assertEqual(self.refs.format_ref(parsed), original)

    def test_a_bare_resource_reference_parses_without_a_pin(self):
        parsed = self.refs.parse_ref("resource:att_1")

        self.assertEqual(parsed.kind, self.refs.RESOURCE)
        self.assertEqual(parsed.relative, "att_1")
        self.assertEqual(parsed.version, "")
        self.assertEqual(parsed.digest, "")

    def test_a_resource_reference_has_no_local_path_until_it_is_landed(self):
        """Resolving one must refuse rather than resolve to a folder named so.

        This is the ``skill:`` lesson applied again: reading
        ``resource:att_1`` as a project-relative path would look for a directory
        literally called ``resource:att_1``, turning "not landed" into a
        confusing "file not found".
        """
        with self.assertRaises(self.refs.ResourceRefError) as caught:
            self.refs.resolve_ref("resource:att_1@v7", project_root="/tmp")

        self.assertEqual(caught.exception.code, "resource_not_landed")

    def test_a_resource_reference_round_trips(self):
        parsed = self.refs.parse_ref("resource:att_1@v7#abc")
        again = self.refs.parse_ref(self.refs.format_ref(parsed))

        self.assertEqual(again.kind, self.refs.RESOURCE)
        self.assertEqual(again.relative, "att_1")
        self.assertEqual(again.version, "v7")
        self.assertEqual(again.digest, "abc")


if __name__ == "__main__":
    unittest.main()
