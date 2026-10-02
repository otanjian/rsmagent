# encoding:utf-8
"""Local artifacts carry their own source (change task 9.1).

The requirement this file pins:

* a locally produced file records ownership, device, project, run/tool call,
  relative path, name, kind, size and a source version;
* the absolute root never travels as protocol or artifact metadata;
* nothing is published until the file has been *really* verified -- a model that
  only claims to have written a file, or a write that landed on another machine,
  produces no usable card.

Every test below fails if the corresponding half is removed, which is the point:
"there is a card" and "the card points at a file that exists" are different
claims, and only the second one is allowed to reach a user.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch


class _TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="artifact-source-")
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "proj")
        os.makedirs(self.root, exist_ok=True)

    def write(self, relative: str, content: str = "hi") -> str:
        path = os.path.join(self.root, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path


class SourceVersionTests(_TempCase):
    """The version token is about content, never about the file's name."""

    def test_a_content_change_is_a_new_version_even_at_the_same_size(self):
        from agent.protocol.artifact import source_version

        path = self.write("a.txt", "aaaa")
        first = source_version(path)
        self.write("a.txt", "aaab")
        self.assertNotEqual(first, source_version(path))

    def test_an_untouched_file_keeps_its_version(self):
        from agent.protocol.artifact import source_version

        path = self.write("a.txt", "same")
        self.assertEqual(source_version(path), source_version(path))

    def test_the_name_is_not_part_of_the_version(self):
        """Two different reports must not compare equal because both are 报告.xlsx."""
        from agent.protocol.artifact import source_version

        left = self.write("a/报告.xlsx", "one")
        right = self.write("b/报告.xlsx", "two")
        self.assertNotEqual(source_version(left), source_version(right))

    def test_a_file_over_the_digest_budget_falls_back_to_stat(self):
        """Hashing a huge artifact must not stall the click that produced it."""
        from agent.protocol.artifact import (
            SOURCE_VERSION_DIGEST_MAX_BYTES, source_version)

        path = os.path.join(self.root, "big.bin")
        with open(path, "wb") as handle:
            handle.write(b"\0" * (SOURCE_VERSION_DIGEST_MAX_BYTES + 1))
        with patch("agent.protocol.artifact.open",
                   side_effect=AssertionError("a large file must not be read")):
            version = source_version(path)
        self.assertTrue(version.startswith("stat:"), version)


class ProtocolKindTests(_TempCase):
    """The contract's six buckets, derived from the extension."""

    def test_each_preview_kind_maps_onto_a_contract_kind(self):
        from agent.protocol.artifact import protocol_kind
        from auth.desktop_contracts_v2 import ARTIFACT

        for name, expected in (
            ("a.md", "text"), ("a.txt", "text"), ("a.html", "text"),
            ("a.csv", "text"), ("a.py", "text"),
            ("a.xlsx", "office"),
            ("a.png", "image"),
            ("a.pdf", "pdf"),
            ("a.zip", "archive"),
            ("a.exe", "binary"),
        ):
            self.assertEqual(protocol_kind(name), expected, name)
            self.assertIn(protocol_kind(name), ARTIFACT["kinds"])


class DesktopOriginTests(_TempCase):
    """Origin fields: identifiers only, complete only once the file is real."""

    def target(self, **overrides):
        from agent.workspace.execution_target import desktop_target

        fields = {"device_id": "dev-1", "workspace_id": "ws-1",
                  "binding_id": "bind-1", "grant_version": 3,
                  "project_mode": "project-execution"}
        fields.update(overrides)
        return desktop_target(**fields)

    def test_a_backend_target_gets_no_origin(self):
        from agent.protocol.artifact import desktop_origin

        self.assertIsNone(desktop_origin(None))
        from agent.workspace.execution_target import BACKEND_TARGET

        self.assertIsNone(desktop_origin(BACKEND_TARGET))

    def test_a_server_artifact_keeps_its_existing_shape(self):
        """No origin => byte-identical metadata, so server cards do not change."""
        from agent.protocol.artifact import build_artifact

        path = self.write("report.txt", "x")
        artifact = build_artifact(path, self.root)
        self.assertNotIn("origin", artifact)
        self.assertEqual(sorted(artifact),
                         ["dir", "file_name", "kind", "path", "previewable",
                          "rel_path", "size", "type"])

    def test_a_local_artifact_records_the_contract_fields(self):
        from agent.protocol.artifact import build_artifact, desktop_origin
        from auth.desktop_contracts_v2 import validate_artifact

        path = self.write("output/报告.xlsx", "x")
        origin = desktop_origin(self.target(), run_id="run-1",
                                tool_call_id="call-1", owner="u1", tenant_id="t1")
        artifact = build_artifact(path, self.root, origin)
        local = artifact["origin"]
        self.assertEqual(local["source"], "desktop")
        self.assertEqual(local["device_id"], "dev-1")
        self.assertEqual(local["workspace_id"], "ws-1")
        self.assertEqual(local["run_id"], "run-1")
        self.assertEqual(local["tool_call_id"], "call-1")
        self.assertEqual(local["relative_path"], "output/报告.xlsx")
        self.assertEqual(local["file_name"], "报告.xlsx")
        self.assertEqual(local["kind"], "office")
        self.assertEqual(local["owner"], "u1")
        self.assertEqual(local["tenant_id"], "t1")
        self.assertTrue(local["source_version"])
        # The reference itself must satisfy the v2 artifact contract.
        self.assertEqual(validate_artifact(local), [])

    def test_the_origin_never_carries_a_path(self):
        from agent.protocol.artifact import build_artifact, desktop_origin

        path = self.write("output/report.txt", "x")
        artifact = build_artifact(path, self.root, desktop_origin(self.target()))
        blob = json.dumps(artifact["origin"], ensure_ascii=False)
        self.assertNotIn(self.root, blob)
        self.assertNotIn("/Users", blob)

    def test_the_artifact_id_is_stable_and_path_free(self):
        from agent.protocol.artifact import artifact_id

        first = artifact_id("run-1", "call-1", "output/report.txt")
        self.assertEqual(first, artifact_id("run-1", "call-1", "output/report.txt"))
        self.assertNotEqual(first, artifact_id("run-1", "call-1", "output/other.txt"))
        self.assertNotIn("output", first)

    def test_a_vanished_file_publishes_nothing(self):
        """A model's claim is not evidence; the file has to be there."""
        from agent.protocol.artifact import build_artifact, desktop_origin

        origin = desktop_origin(self.target())
        missing = os.path.join(self.root, "never-written.txt")
        self.assertIsNone(build_artifact(missing, self.root, origin))

    def test_a_path_climbing_out_of_the_project_is_not_published(self):
        from agent.protocol.artifact import _local_origin, desktop_origin

        outside = os.path.join(self._tmp.name, "elsewhere.txt")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("x")
        self.assertIsNone(
            _local_origin(outside, "../elsewhere.txt", desktop_origin(self.target())))


class ArtifactPayloadTests(_TempCase):
    """The SSE payload: a local card must not advertise a server URL."""

    def target(self):
        from agent.workspace.execution_target import desktop_target

        return desktop_target(device_id="dev-1", workspace_id="ws-1",
                              binding_id="bind-1", grant_version=1,
                              project_mode="project-execution")

    def test_a_server_artifact_keeps_its_urls(self):
        from agent.protocol.artifact import build_artifact
        from channel.web.fork.runtime import _build_artifact_payload

        path = self.write("server.txt", "x")
        payload = _build_artifact_payload(build_artifact(path, self.root))
        self.assertTrue(payload["raw_url"].startswith("/api/file?path="))
        self.assertTrue(payload["preview_url"].startswith("/preview/"))
        self.assertNotIn("source", payload)

    def test_a_local_artifact_gets_no_server_url_or_absolute_path(self):
        from agent.protocol.artifact import build_artifact, desktop_origin
        from channel.web.fork.runtime import _build_artifact_payload

        path = self.write("output/报告.xlsx", "x")
        origin = desktop_origin(self.target(), run_id="run-1", tool_call_id="call-1",
                                owner="u1", tenant_id="t1")
        payload = _build_artifact_payload(build_artifact(path, self.root, origin))
        self.assertEqual(payload["source"], "desktop")
        self.assertTrue(payload["local"])
        self.assertEqual(payload["device_id"], "dev-1")
        self.assertEqual(payload["workspace_id"], "ws-1")
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(payload["tool_call_id"], "call-1")
        self.assertEqual(payload["relative_path"], "output/报告.xlsx")
        self.assertEqual(payload["rel_path"], "output/报告.xlsx")
        self.assertEqual(payload["artifact_kind"], "office")
        self.assertTrue(payload["source_version"])
        for forbidden in ("abs_path", "raw_url", "preview_url", "path"):
            self.assertNotIn(forbidden, payload, forbidden)
        blob = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(self.root, blob)
        self.assertNotIn("/api/file", blob)
        self.assertNotIn("/preview/", blob)

    def test_a_card_that_says_it_must_be_revalidated(self):
        """The client must re-check the file before it previews or opens it."""
        from agent.protocol.artifact import build_artifact, desktop_origin
        from channel.web.fork.runtime import _build_artifact_payload
        from auth.desktop_contracts_v2 import ARTIFACT

        path = self.write("output/report.txt", "x")
        payload = _build_artifact_payload(
            build_artifact(path, self.root, desktop_origin(self.target())))
        self.assertTrue(payload["revalidate"])
        self.assertTrue(ARTIFACT["revalidate_before_preview"])


class EmissionTests(_TempCase):
    """The executor publishes an origin only for a run it can actually vouch for."""

    def setUp(self):
        super().setUp()
        from agent.desktop_local import LocalRootRegistry, reset_registry

        reset_registry()
        self.addCleanup(reset_registry)
        self.registry = LocalRootRegistry()
        patch_ = patch("agent.desktop_local.registry", return_value=self.registry)
        patch_.start()
        self.addCleanup(patch_.stop)

    def _identity(self):
        from common.runtime_identity import RuntimeIdentity
        from agent.workspace.execution_target import desktop_target

        self.registry.register(user_id="u1", tenant_id="t1", device_id="dev-1",
                               workspace_id="ws-1", binding_id="bind-1",
                               grant_version=1, project_mode="project-execution",
                               absolute_path=self.root)
        target = desktop_target(device_id="dev-1", workspace_id="ws-1",
                                binding_id="bind-1", grant_version=1,
                                project_mode="project-execution")
        return RuntimeIdentity(user_id="u1", tenant_id="t1").derive(
            execution_target=target, execution_cwd=self.root)

    def _executor(self, events, project_dir="/server/workspace"):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class _Agent:
            workspace_dir = "/server/workspace"
            workspace_scope = "project"

            def __init__(self):
                self.project_dir = project_dir

            def effective_cwd(self):
                return self.project_dir

        return AgentStreamExecutor(agent=_Agent(), model=None, system_prompt="",
                                   tools=[], on_event=events.append)

    def test_a_local_write_carries_device_run_and_tool_call(self):
        from common.runtime_identity import use_identity

        path = self.write("output/report.txt", "x")
        events = []
        executor = self._executor(events)
        with use_identity(self._identity()):
            executor._maybe_emit_artifact(
                {"id": "call-1", "name": "write", "arguments": {"path": path}},
                {"status": "success", "result": {"abs_path": path}})
        artifacts = [e for e in events if e["type"] == "artifact"]
        self.assertEqual(len(artifacts), 1)
        origin = artifacts[0]["data"]["origin"]
        self.assertEqual(origin["device_id"], "dev-1")
        self.assertEqual(origin["tool_call_id"], "call-1")
        self.assertEqual(origin["owner"], "u1")
        self.assertEqual(origin["relative_path"], "output/report.txt")

    def test_a_server_run_publishes_no_origin(self):
        from common.runtime_identity import RuntimeIdentity, use_identity

        path = self.write("output/report.txt", "x")
        events = []
        executor = self._executor(events, project_dir=self.root)
        with use_identity(RuntimeIdentity(user_id="u1", tenant_id="t1")):
            executor._maybe_emit_artifact(
                {"id": "call-1", "name": "write", "arguments": {"path": path}},
                {"status": "success", "result": {"abs_path": path}})
        artifacts = [e for e in events if e["type"] == "artifact"]
        self.assertEqual(len(artifacts), 1)
        self.assertNotIn("origin", artifacts[0]["data"])

    def test_a_file_that_does_not_exist_is_not_announced(self):
        from common.runtime_identity import use_identity

        events = []
        executor = self._executor(events)
        with use_identity(self._identity()):
            executor._maybe_emit_artifact(
                {"id": "call-1", "name": "write",
                 "arguments": {"path": os.path.join(self.root, "ghost.xlsx")}},
                {"status": "success", "result": {"abs_path": os.path.join(self.root, "ghost.xlsx")}})
        self.assertEqual([e for e in events if e["type"] == "artifact"], [])

    def test_a_revoked_grant_stamps_no_origin(self):
        """No resolvable local directory here => no device identity on the file."""
        from common.runtime_identity import use_identity

        path = self.write("output/report.txt", "x")
        identity = self._identity()
        self.registry.revoke(user_id="u1")
        events = []
        executor = self._executor(events, project_dir=self.root)
        with use_identity(identity):
            executor._maybe_emit_artifact(
                {"id": "call-1", "name": "write", "arguments": {"path": path}},
                {"status": "success", "result": {"abs_path": path}})
        artifacts = [e for e in events if e["type"] == "artifact"]
        self.assertEqual(len(artifacts), 1)
        self.assertNotIn("origin", artifacts[0]["data"])


class HistoryTests(_TempCase):
    """History rebuilds a local card as a local reference, never as a URL."""

    def setUp(self):
        super().setUp()
        from agent.desktop_local import LocalRootRegistry, reset_registry

        reset_registry()
        self.addCleanup(reset_registry)
        self.registry = LocalRootRegistry()
        self.server_root = os.path.join(self._tmp.name, "server")
        os.makedirs(self.server_root, exist_ok=True)
        for patcher in (
            patch("agent.desktop_local.registry", return_value=self.registry),
            # The server-root lookup belongs to the tenant-scoped workspace
            # resolution, which is not what this test is about: it needs *some*
            # server root so the two branches can be told apart.
            patch("channel.web.fork.runtime._session_workspace_root",
                  return_value=self.server_root),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _desktop_target(self, **overrides):
        from agent.workspace.execution_target import desktop_target

        fields = {"device_id": "dev-1", "workspace_id": "ws-1",
                  "binding_id": "bind-1", "grant_version": 1,
                  "project_mode": "project-execution"}
        fields.update(overrides)
        return desktop_target(**fields)

    def write_step(self, path):
        """One persisted `write` step for ``path``, as history finds it."""
        return [{"type": "tool", "name": "write", "id": "call-1",
                 "arguments": {"path": path},
                 "result": json.dumps({"abs_path": path})}]

    def replay(self, steps):
        """Rebuild a message's cards the way the history endpoint does."""
        from channel.web.fork.runtime import _artifacts_from_steps
        from common.runtime_identity import RuntimeIdentity, use_identity

        with use_identity(RuntimeIdentity(user_id="u1", tenant_id="t1")):
            return _artifacts_from_steps(steps, "s-1", "agent-1")

    def test_a_local_history_card_has_no_server_url(self):
        from channel.web.fork.runtime import _artifacts_from_steps
        from common.runtime_identity import RuntimeIdentity, use_identity

        self.registry.register(user_id="u1", tenant_id="t1", device_id="dev-1",
                               workspace_id="ws-1", binding_id="bind-1",
                               grant_version=1, project_mode="project-execution",
                               absolute_path=self.root)
        path = self.write("output/report.txt", "x")
        steps = [{"type": "tool", "name": "write", "id": "call-1",
                  "arguments": {"path": path},
                  "result": json.dumps({"abs_path": path})}]
        # History replay runs inside the request's identity scope, which is what
        # re-authorizes the session's stored target.
        with use_identity(RuntimeIdentity(user_id="u1", tenant_id="t1")):
            with patch("agent.workspace.project_store.get_execution_target",
                       return_value=self._desktop_target()):
                cards = _artifacts_from_steps(steps, "s-1", "agent-1")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["source"], "desktop")
        self.assertEqual(cards[0]["relative_path"], "output/report.txt")
        self.assertNotIn("raw_url", cards[0])
        self.assertNotIn("abs_path", cards[0])
        self.assertNotIn(self.root, json.dumps(cards, ensure_ascii=False))

    def test_a_file_written_elsewhere_is_not_claimed(self):
        """Another machine's path must not become a card on this one."""
        from channel.web.fork.runtime import _artifacts_from_steps

        self.registry.register(user_id="u1", tenant_id="t1", device_id="dev-1",
                               workspace_id="ws-1", binding_id="bind-1",
                               grant_version=1, project_mode="project-execution",
                               absolute_path=self.root)
        # A path from another machine: neither this process's server root nor the
        # directory of the device this session is bound to.
        other = os.path.join(self._tmp.name, "other-machine", "report.txt")
        os.makedirs(os.path.dirname(other), exist_ok=True)
        with open(other, "w", encoding="utf-8") as handle:
            handle.write("x")
        steps = [{"type": "tool", "name": "write", "id": "call-1",
                  "arguments": {"path": other},
                  "result": json.dumps({"abs_path": other})}]
        # The session is bound to a *different* device whose directory this
        # process does not hold, so nothing resolves here.
        with patch("agent.workspace.project_store.get_execution_target",
                   return_value=self._desktop_target(
                       device_id="dev-2", workspace_id="ws-2", binding_id="bind-2")):
            cards = _artifacts_from_steps(steps, "s-1", "agent-1")
        self.assertEqual(cards, [])

    def test_a_card_names_the_project_that_produced_it_not_the_one_open_now(self):
        """切目录: produced in project A, the session is on project B by now.

        The card has to keep A's device/workspace/binding and A's relative path.
        Re-deriving it from the session's *current* target is how a replay would
        either lose the card or re-file it under B, where a same-named file may
        well exist -- a link to a different file that looks like the same one.
        """
        self.registry.register(user_id="u1", tenant_id="t1", device_id="dev-1",
                               workspace_id="ws-A", binding_id="bind-A",
                               grant_version=1, project_mode="project-execution",
                               absolute_path=self.root)
        project_b = os.path.join(self._tmp.name, "project-b")
        os.makedirs(project_b, exist_ok=True)
        self.registry.register(user_id="u1", tenant_id="t1", device_id="dev-1",
                               workspace_id="ws-B", binding_id="bind-B",
                               grant_version=2, project_mode="project-execution",
                               absolute_path=project_b)
        path = self.write("output/report.txt", "produced in A")

        cards = self.replay(self.write_step(path))

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["workspace_id"], "ws-A")
        self.assertEqual(cards[0]["binding_id"], "bind-A")
        self.assertEqual(cards[0]["relative_path"], "output/report.txt")
        self.assertEqual(cards[0]["resolution"], "ok")
        # And the panel is told to re-read it rather than trust a copy.
        self.assertTrue(cards[0]["revalidate"])
        from agent.protocol.artifact import source_version

        self.assertEqual(cards[0]["source_version"], source_version(path))

    def test_a_revoked_project_does_not_come_back_from_the_recent_list(self):
        """不把最近候选当授权: a revoked grant is not an authorization.

        The directory is still on the disk and was the most recently used one,
        which is exactly why "recently used" must not be the rule.
        """
        self.registry.register(user_id="u1", tenant_id="t1", device_id="dev-1",
                               workspace_id="ws-1", binding_id="bind-1",
                               grant_version=1, project_mode="project-execution",
                               absolute_path=self.root)
        path = self.write("output/report.txt", "x")
        self.registry.revoke(user_id="u1", tenant_id="t1", device_id="dev-1")

        self.assertEqual(self.replay(self.write_step(path)), [])

    def test_a_deleted_file_keeps_its_card_and_says_it_is_gone(self):
        """删除: the record survives; "drop when gone" is the rule for live cards.

        Live emission drops an artifact whose file is not there, because that
        card is a promise about a file you can open. A history card is a record
        that the run produced one, so dropping it would delete the only trace --
        and minting a server reference instead would point at a file this
        process never had.
        """
        self.registry.register(user_id="u1", tenant_id="t1", device_id="dev-1",
                               workspace_id="ws-1", binding_id="bind-1",
                               grant_version=1, project_mode="project-execution",
                               absolute_path=self.root)
        path = self.write("output/report.txt", "x")
        os.remove(path)

        cards = self.replay(self.write_step(path))

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["resolution"], "missing")
        self.assertEqual(cards[0]["relative_path"], "output/report.txt")
        # Nothing claims a version it could not read, and nothing became a URL.
        self.assertEqual(cards[0]["source_version"], "")
        self.assertNotIn("raw_url", cards[0])
        self.assertNotIn("preview_url", cards[0])
        self.assertNotIn("abs_path", cards[0])
        self.assertNotIn(self.root, json.dumps(cards, ensure_ascii=False))

    def test_a_history_card_is_never_uploaded_to_the_server(self):
        """不自动上传: the payload offers no server copy to fall back on.

        "Make the old card work again by putting the file on the server" is the
        tempting fix, and the one this task refuses: it would move the user's
        file off their machine to make a link render.
        """
        self.registry.register(user_id="u1", tenant_id="t1", device_id="dev-1",
                               workspace_id="ws-1", binding_id="bind-1",
                               grant_version=1, project_mode="project-execution",
                               absolute_path=self.root)
        path = self.write("output/report.txt", "x")

        cards = self.replay(self.write_step(path))

        blob = json.dumps(cards, ensure_ascii=False)
        self.assertIn('"source": "desktop"', blob)
        for forbidden in ("raw_url", "preview_url", "abs_path", "/api/file"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, blob)
        # Nothing was copied out of the project into the server workspace.
        self.assertEqual(os.listdir(self.server_root), [])


class _StubResult:
    """The slice of ``ToolResult`` the artifact reporter reads."""

    def __init__(self, status="success", ext_data=None):
        self.status = status
        self.ext_data = ext_data or {}
        self.result = "ok"


class RemoteArtifactTests(_TempCase):
    """A device's own report becomes a card here, but is never stat'd here."""

    def _desktop_identity(self):
        from agent.workspace.execution_target import desktop_target
        from common.runtime_identity import RuntimeIdentity

        target = desktop_target(device_id="dev-1", workspace_id="ws-1",
                                binding_id="bind-1", grant_version=1,
                                project_mode="project-execution")
        return RuntimeIdentity(user_id="u1", tenant_id="t1").derive(
            execution_target=target)

    def _executor(self, events):
        from agent.protocol.agent_stream import AgentStreamExecutor

        class _Agent:
            project_dir = "/server/workspace"
            workspace_dir = "/server/workspace"
            workspace_scope = "project"

            def effective_cwd(self):
                return self.project_dir

        return AgentStreamExecutor(agent=_Agent(), model=None, system_prompt="",
                                   tools=[], on_event=events.append)

    def _entry(self, **overrides):
        entry = {
            "source": "desktop", "artifact_id": "art_remote_1",
            "device_id": "dev-1", "workspace_id": "ws-1", "run_id": "run-1",
            "tool_call_id": "call-1", "relative_path": "output/报告.xlsx",
            "file_name": "报告.xlsx", "kind": "office", "size": 20480,
            "source_version": "sha256:abc",
        }
        entry.update(overrides)
        return entry

    def test_a_device_report_becomes_a_local_card(self):
        from common.runtime_identity import use_identity

        events = []
        executor = self._executor(events)
        with use_identity(self._desktop_identity()):
            executor._maybe_emit_remote_artifacts(
                _StubResult(ext_data={"desktop_artifacts": [self._entry()]}),
                "call-1")
        artifacts = [e for e in events if e["type"] == "artifact"]
        self.assertEqual(len(artifacts), 1)
        origin = artifacts[0]["data"]["origin"]
        self.assertEqual(origin["device_id"], "dev-1")
        self.assertEqual(origin["relative_path"], "output/报告.xlsx")
        self.assertEqual(origin["source_version"], "sha256:abc")

    def test_a_device_report_is_never_turned_into_a_server_path(self):
        """The device's file must not be stat'd (or served) from here."""
        from agent.protocol.artifact import classify_kind
        from channel.web.fork.runtime import _build_artifact_payload
        from common.runtime_identity import use_identity

        # A same-named *server* file exists, and it must stay invisible: the
        # device's 报告.xlsx is another machine's file.
        self.write("output/报告.xlsx", "a different file entirely")
        events = []
        executor = self._executor(events)
        with use_identity(self._desktop_identity()):
            executor._maybe_emit_remote_artifacts(
                _StubResult(ext_data={"desktop_artifacts": [self._entry()]}),
                "call-1")
        data = [e for e in events if e["type"] == "artifact"][0]["data"]
        self.assertNotIn("path", data)
        payload = _build_artifact_payload(data)
        self.assertEqual(payload["source"], "desktop")
        self.assertEqual(payload["relative_path"], "output/报告.xlsx")
        self.assertEqual(payload["kind"], classify_kind("报告.xlsx"))
        # The previewability rule is the *same* rule a server artifact gets: an
        # xlsx is not previewed inline, it is opened by a system application.
        from agent.protocol.artifact import is_previewable

        self.assertEqual(payload["previewable"],
                         is_previewable(classify_kind("报告.xlsx")))
        self.assertFalse(payload["previewable"])
        for forbidden in ("abs_path", "raw_url", "preview_url"):
            self.assertNotIn(forbidden, payload, forbidden)

    def test_a_report_that_violates_the_contract_is_dropped_whole(self):
        from common.runtime_identity import use_identity

        cases = {
            "absolute path": {"relative_path": "/etc/passwd"},
            "climbing path": {"relative_path": "../escape.txt"},
            "unknown kind": {"kind": "executable"},
            "no version": {"source_version": ""},
            "foreign workspace": {"workspace_id": "ws-other"},
        }
        for label, override in cases.items():
            events = []
            executor = self._executor(events)
            with use_identity(self._desktop_identity()):
                executor._maybe_emit_remote_artifacts(
                    _StubResult(ext_data={"desktop_artifacts": [self._entry(**override)]}),
                    "call-1")
            self.assertEqual([e for e in events if e["type"] == "artifact"], [],
                             label)

    def test_a_failed_result_reports_nothing(self):
        from common.runtime_identity import use_identity

        events = []
        executor = self._executor(events)
        with use_identity(self._desktop_identity()):
            executor._maybe_emit_remote_artifacts(
                _StubResult(status="error",
                            ext_data={"desktop_artifacts": [self._entry()]}),
                "call-1")
        self.assertEqual([e for e in events if e["type"] == "artifact"], [])

    def test_a_server_run_publishes_no_device_card(self):
        from common.runtime_identity import RuntimeIdentity, use_identity

        events = []
        executor = self._executor(events)
        with use_identity(RuntimeIdentity(user_id="u1", tenant_id="t1")):
            executor._maybe_emit_remote_artifacts(
                _StubResult(ext_data={"desktop_artifacts": [self._entry()]}),
                "call-1")
        self.assertEqual([e for e in events if e["type"] == "artifact"], [])


class _RaisingSink:
    """An ``on_event`` that fails, i.e. a client transport that went away."""

    def __init__(self):
        self.calls = 0

    def __call__(self, event):
        self.calls += 1
        raise RuntimeError("the event sink is gone")

    #: ``_executor`` wires ``on_event=events.append``, so the sink has to look
    #: like a list too.
    def append(self, event):
        self.calls += 1
        raise RuntimeError("the event sink is gone")


class EmissionIsolationTests(RemoteArtifactTests):
    """Reporting a card is a projection -- it can never rewrite a tool result.

    ``_maybe_emit_remote_artifacts`` is called *inside* ``_execute_tool``'s
    ``try``, whose ``except Exception`` turns whatever escapes into
    ``status="error"`` with the exception text as the tool's output. So a bug in
    card reporting would be handed to the model as a *failed* tool call, and the
    model would retry or give up on a write that had in fact already succeeded.
    The local half is worse: ``_maybe_emit_artifact`` runs in the streaming
    loop, where a raise aborts the turn outright.

    Card problems must therefore degrade to "no card" and say so in the log.
    What is forbidden is inventing a failure -- or a card -- out of either.
    """

    def test_a_missing_event_sink_is_not_an_error(self):
        """A partially built executor reports nothing, and does not raise.

        Real callers construct the executor through ``__init__``, but the tool
        dispatch seam is routinely driven by minimal executors (the approval
        gate's own suite is one). A *reporting* step must not be the thing that
        breaks them: it reported ``AttributeError`` as the tool's output, which
        is how a successful write became a failure.
        """
        from common.runtime_identity import use_identity

        executor = object.__new__(type(self._executor([])))
        with use_identity(self._desktop_identity()):
            executor._maybe_emit_remote_artifacts(
                _StubResult(ext_data={"desktop_artifacts": [self._entry()]}),
                "call-1")

    def test_a_validator_that_raises_does_not_fail_the_tool_call(self):
        from common.runtime_identity import use_identity

        events = []
        executor = self._executor(events)
        with patch("auth.desktop_contracts_v2.validate_artifact",
                   side_effect=RuntimeError("bad contract reader")):
            with use_identity(self._desktop_identity()):
                executor._maybe_emit_remote_artifacts(
                    _StubResult(ext_data={"desktop_artifacts": [self._entry()]}),
                    "call-1")
        self.assertEqual([e for e in events if e["type"] == "artifact"], [])

    def test_a_broken_event_sink_does_not_fail_the_tool_call(self):
        from common.runtime_identity import use_identity

        sink = _RaisingSink()
        executor = self._executor(sink)
        with use_identity(self._desktop_identity()):
            executor._maybe_emit_remote_artifacts(
                _StubResult(ext_data={"desktop_artifacts": [self._entry()]}),
                "call-1")
        self.assertGreaterEqual(sink.calls, 1, "the card should have been attempted")

    def _anchored_executor(self, events, root):
        """An executor whose cwd *is* ``root``, so a write under it can be a card."""
        from agent.protocol.agent_stream import AgentStreamExecutor

        class _Agent:
            project_dir = root
            workspace_dir = root
            workspace_scope = "project"

            def effective_cwd(self):
                return root

        return AgentStreamExecutor(agent=_Agent(), model=None, system_prompt="",
                                   tools=[], on_event=events.append)

    def _write_tool_call(self, path):
        return {"id": "call-local-1", "name": "write", "arguments": {"path": path}}

    def test_a_local_write_still_reports_when_the_sink_is_broken(self):
        events = []
        executor = self._anchored_executor(events, self.root)
        path = self.write("notes.txt", "hello")

        executor._maybe_emit_artifact(self._write_tool_call(path),
                                      {"status": "success",
                                       "result": {"abs_path": path}})
        self.assertTrue([e for e in events if e["type"] == "artifact"])

        # Same write, sink now broken: the card is attempted, nothing escapes.
        broken = self._anchored_executor(_RaisingSink(), self.root)
        broken._maybe_emit_artifact(self._write_tool_call(path),
                                    {"status": "success",
                                     "result": {"abs_path": path}})

    def test_a_local_write_in_a_partial_executor_does_not_raise(self):
        events = []
        executor = self._anchored_executor(events, self.root)
        path = self.write("notes.txt", "hello")

        partial = object.__new__(type(executor))
        partial._ARTIFACT_TOOLS = ("write", "edit")
        partial._emitted_artifacts = set()
        partial._maybe_emit_artifact(self._write_tool_call(path),
                                     {"status": "success",
                                      "result": {"abs_path": path}})


if __name__ == "__main__":
    unittest.main()
