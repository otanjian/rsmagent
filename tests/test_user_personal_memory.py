# encoding:utf-8
"""Per-user personal long-term memory (change personal-conversation-and-memory).

The user layer already exists on paper: ``state_dir.user_root()`` resolves to
``shared_root()/users/<user_id>`` and the memory schema carries ``user_id`` /
``scope``. What is missing is that every production call site passes
``base=<agent workspace>``, which collapses the per-user layer back onto the
Agent, and that ``user_id`` is never threaded in.

These tests pin the user-visible contract, not the plumbing:

* personal memory *files* live in the user domain, not in an Agent workspace;
* a user's personal memory is visible from every Agent they may use;
* another user cannot see it, by search or by path;
* restored LLM context is filtered the same way session listing is.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch

from auth.service import IdentityService
from common.runtime_identity import RuntimeIdentity, use_identity
from common import state_dir

def _mkdb():
    return os.path.join(tempfile.mkdtemp(), "identity.db")


class PersonalMemoryTestCase(unittest.TestCase):
    """Two users of one tenant, and two Agents they may both use."""

    def setUp(self):
        self.db = _mkdb()
        self.svc = IdentityService(self.db)
        self.shared = tempfile.mkdtemp(prefix="shared-")
        self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root=self.shared, allow_weak=True)
        self.tid = self.svc.list_tenants()[0]["id"]
        self.root = self.svc.list_platform_users()[0]

        self.alice = self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            operation="create-new", username="alice", display_name="Alice",
            temporary_password="TmpPass123!", roles=[])["user_id"]
        self.bob = self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            operation="create-new", username="bob", display_name="Bob",
            temporary_password="TmpPass123!", roles=[])["user_id"]

        # Two Agent workspaces, so "visible from every Agent" is testable.
        self.ws_x = tempfile.mkdtemp(prefix="agent-x-")
        self.ws_y = tempfile.mkdtemp(prefix="agent-y-")

        # ``state_dir.shared_root()`` resolves the tenant through the process
        # singleton, so point it at this test's service.
        self._svc_patch = patch("auth.service.get_identity_service",
                                lambda: self.svc)
        self._svc_patch.start()

    def tearDown(self):
        self._svc_patch.stop()
        from agent.memory import clear_conversation_store_cache
        from agent.memory.config import reset_memory_configs
        clear_conversation_store_cache()
        reset_memory_configs()

    # -- helpers ---------------------------------------------------------

    def _ident(self, user_id, agent_id="agent-x"):
        return RuntimeIdentity(agent_id=agent_id, user_id=user_id, tenant_id=self.tid)

    def _manager(self, ws):
        from agent.memory.config import MemoryConfig
        from agent.memory.manager import MemoryManager
        # Keyword-only (no embedding provider) so the test is deterministic and
        # offline. ``min_score=0`` because a single-document corpus produces a
        # BM25 rank near zero, whose weighted score would otherwise be filtered
        # by the default threshold — irrelevant to what this test pins down.
        config = MemoryConfig(workspace_root=ws, min_score=0.0)
        return MemoryManager(config=config, embedding_provider=None)

    # -- 1.1 personal files live in the user domain ----------------------

    def test_personal_memory_files_land_in_user_domain(self):
        from agent.memory.summarizer import MemoryFlushManager

        with use_identity(self._ident(self.alice)):
            user_root = state_dir.user_root()
            self.assertTrue(
                os.path.realpath(str(user_root)).startswith(os.path.realpath(self.shared)),
                f"user root {user_root} is not under the tenant shared root {self.shared}")
            self.assertTrue(str(user_root).endswith(self.alice))

            manager = MemoryFlushManager(workspace_dir=Path(self.ws_x))
            main = manager.get_main_memory_file(self.alice)
            daily = manager.get_today_memory_file(self.alice)

            for path in (main, daily):
                self.assertFalse(
                    str(path).startswith(self.ws_x),
                    f"personal memory {path} was written into the Agent workspace")
                self.assertTrue(
                    str(path).startswith(str(user_root)),
                    f"personal memory {path} is not under the user domain {user_root}")

    def test_daily_summary_writes_to_user_domain(self):
        from agent.memory.summarizer import MemoryFlushManager

        with use_identity(self._ident(self.alice)):
            manager = MemoryFlushManager(workspace_dir=Path(self.ws_x))
            ok = manager.write_daily_summary(
                "PROJECTOR-QUARTZ is Alice's code word", user_id=self.alice)
            self.assertTrue(ok)

            written = [p for p in os.listdir(str(state_dir.user_root()))
                       if p.endswith(".md") or os.path.isdir(os.path.join(str(state_dir.user_root()), p))]
            daily_root = state_dir.memory_dir(ensure=False)
            found = any(
                "PROJECTOR-QUARTZ" in open(os.path.join(root, name), encoding="utf-8").read()
                for root, _dirs, names in os.walk(str(daily_root))
                for name in names if name.endswith(".md"))
            self.assertTrue(found, f"daily summary was not written under {daily_root}")

    # -- 1.2/1.3 cross-Agent visibility, and isolation -------------------

    def test_personal_memory_visible_from_every_agent(self):
        with use_identity(self._ident(self.alice)):
            main = state_dir.memory_file()
            main.parent.mkdir(parents=True, exist_ok=True)
            main.write_text("PROJECTOR-QUARTZ is Alice's code word\n", encoding="utf-8")

            # Agent X (where it was written) and Agent Y (a different workspace)
            # must both surface it for Alice.
            for ws, agent in ((self.ws_x, "agent-x"), (self.ws_y, "agent-y")):
                with use_identity(self._ident(self.alice, agent)):
                    manager = self._manager(ws)
                    import asyncio
                    asyncio.run(manager.sync())
                    hits = asyncio.run(
                        manager.search("PROJECTOR-QUARTZ", user_id=self.alice))
                    self.assertTrue(
                        hits,
                        f"Alice's personal memory is not visible from {agent}")

    def test_other_user_cannot_search_personal_memory(self):
        with use_identity(self._ident(self.alice)):
            main = state_dir.memory_file()
            main.parent.mkdir(parents=True, exist_ok=True)
            main.write_text("PROJECTOR-QUARTZ is Alice's code word\n", encoding="utf-8")

            manager = self._manager(self.ws_x)
            import asyncio
            asyncio.run(manager.sync())

            own = asyncio.run(manager.search("PROJECTOR-QUARTZ", user_id=self.alice))
            self.assertTrue(own, "Alice cannot read her own personal memory")

        with use_identity(self._ident(self.bob)):
            manager = self._manager(self.ws_x)
            import asyncio
            asyncio.run(manager.sync())
            other = asyncio.run(manager.search("PROJECTOR-QUARTZ", user_id=self.bob))
            self.assertFalse(other, "Bob can read Alice's personal memory")

    # -- 5.2 one scope filter, whichever keyword backend runs -----------

    def test_keyword_isolation_across_all_backends(self):
        """ASCII (FTS5), CJK (trigram) and short (LIKE) queries all filter.

        Three near-identical WHERE clauses used to be built by hand. These
        queries take different code paths, so a filter forgotten in one of them
        shows up as a leak here.
        """
        from agent.memory.manager import MemoryManager
        from agent.memory.config import MemoryConfig
        from agent.memory.storage import MemoryChunk

        manager = MemoryManager(config=MemoryConfig(workspace_root=self.ws_x),
                                embedding_provider=None)
        storage = manager.storage

        def chunk(mid, text, user_id, scope):
            return MemoryChunk(
                id=mid, user_id=user_id, scope=scope, source="memory",
                path=f"memory/{mid}.md", start_line=1, end_line=1,
                text=text, embedding=None, hash=f"h-{mid}", metadata={})

        # Both users' own rows share a word, so each backend must return a
        # non-empty personal result while still excluding the other user's.
        storage.save_chunks_batch([
            chunk("a1", "ALICEPRIVATE MARKERWORD", self.alice, "user"),
            chunk("b1", "BOBPRIVATE MARKERWORD", self.bob, "user"),
            chunk("sh", "COMMONSHARED MARKERWORD", None, "shared"),
            chunk("a2", "爱丽丝专属密码", self.alice, "user"),
            chunk("b2", "鲍勃专属密码", self.bob, "user"),
            chunk("sh2", "公共共享专属密码", None, "shared"),
        ])

        # Each keyword backend is exercised directly, so a filter omitted from
        # one of them cannot hide behind another path having been taken.
        backends = [("like", storage._search_like, "MARKERWORD")]
        if storage.fts5_available:
            backends.append(("fts5", storage._search_fts5, "MARKERWORD"))
        if storage.trigram_fts5_available:
            backends.append(("trigram", storage._search_fts5_trigram, "专属密码"))

        for name, fn, query in backends:
            alice = {r.path for r in fn(query, self.alice, ["shared", "user"], 10)}
            bob = {r.path for r in fn(query, self.bob, ["shared", "user"], 10)}
            self.assertTrue(alice, f"{name}: Alice cannot see her own memory")
            self.assertTrue(bob, f"{name}: Bob cannot see his own memory")
            self.assertFalse(
                any("b1" in p or "b2" in p for p in alice),
                f"{name}: Alice saw Bob's private row for {query!r}")
            self.assertFalse(
                any("a1" in p or "a2" in p for p in bob),
                f"{name}: Bob saw Alice's private row for {query!r}")
            self.assertTrue(
                any(p.startswith("memory/sh") for p in alice),
                f"{name}: shared memory became invisible to Alice")

    # -- 1.4 memory_get must not read another user's files ---------------

    def test_memory_get_rejects_another_users_file(self):
        from agent.memory.manager import MemoryManager
        from agent.memory.config import MemoryConfig
        from agent.tools.memory.memory_get import MemoryGetTool

        secret_dir = os.path.join(self.ws_x, "memory", "users", self.bob)
        os.makedirs(secret_dir, exist_ok=True)
        with open(os.path.join(secret_dir, "MEMORY.md"), "w", encoding="utf-8") as f:
            f.write("BOBS-SECRET-TOKEN\n")

        manager = MemoryManager(config=MemoryConfig(workspace_root=self.ws_x),
                               embedding_provider=None)
        tool = MemoryGetTool(manager)

        with use_identity(self._ident(self.alice)):
            denied = tool.execute({"path": f"memory/users/{self.bob}/MEMORY.md"})
            self.assertEqual(denied.status, "error",
                             "memory_get leaked another user's file")

            allowed = tool.execute({"path": "MEMORY.md"})
            self.assertEqual(allowed.status, "success",
                             "memory_get broke the shared root MEMORY.md")

    # -- 1.9/1.10 auto-consolidation scope, and legacy unchanged --------

    def test_daily_summary_does_not_touch_shared_memory(self):
        """A user-scoped flush must never write over the Agent's MEMORY.md."""
        from agent.memory.summarizer import MemoryFlushManager

        shared_memory = Path(self.ws_x) / "MEMORY.md"
        shared_memory.write_text("SHARED-ORIGINAL\n", encoding="utf-8")

        with use_identity(self._ident(self.alice)):
            manager = MemoryFlushManager(workspace_dir=Path(self.ws_x))
            manager.write_daily_summary("ALICE-PRIVATE-NOTE", user_id=self.alice)

        self.assertEqual(shared_memory.read_text(encoding="utf-8"),
                         "SHARED-ORIGINAL\n",
                         "user-scoped flush overwrote the shared MEMORY.md")

    def test_legacy_flush_without_user_stays_in_workspace(self):
        """No identity (legacy / machine run): exactly the old location."""
        from agent.memory.summarizer import MemoryFlushManager

        with use_identity(RuntimeIdentity()):
            manager = MemoryFlushManager(workspace_dir=Path(self.ws_x))
            self.assertEqual(manager.get_main_memory_file(),
                             Path(self.ws_x) / "MEMORY.md")
            self.assertTrue(
                str(manager.get_today_memory_file()).startswith(
                    str(Path(self.ws_x) / "memory")),
                "legacy daily memory left the Agent workspace")

    def test_evolution_record_follows_the_user(self):
        from agent.evolution.record import append_session_evolution

        with use_identity(self._ident(self.alice)):
            append_session_evolution(
                Path(self.ws_x), "ALICE-EVOLVED", user_id=self.alice)
            log_dir = state_dir.memory_dir(ensure=False) / "evolution"
            self.assertTrue(
                any("ALICE-EVOLVED" in p.read_text(encoding="utf-8")
                    for p in log_dir.rglob("*.md")),
                f"personal evolution log not written under {log_dir}")
        self.assertFalse(
            (Path(self.ws_x) / "memory" / "evolution").exists(),
            "personal evolution was written into the Agent workspace")

    # -- 4.9 the prompt carries the user's personal memory ---------------

    def test_prompt_loads_personal_memory_for_the_user(self):
        from agent.prompt.workspace import load_context_files

        with use_identity(self._ident(self.alice)):
            personal = state_dir.memory_file()
            personal.parent.mkdir(parents=True, exist_ok=True)
            personal.write_text("PROJECTOR-QUARTZ\n", encoding="utf-8")
            loaded = load_context_files(self.ws_x)

        paths = [c.path for c in loaded]
        self.assertIn(f"users/{self.alice}/MEMORY.md", paths,
                      "the user's personal memory was not offered to the prompt")
        self.assertTrue(any("PROJECTOR-QUARTZ" in c.content for c in loaded))

    def test_legacy_prompt_has_no_personal_segment(self):
        from agent.prompt.workspace import load_context_files

        with use_identity(RuntimeIdentity()):
            loaded = load_context_files(self.ws_x)
        self.assertFalse(
            any(c.path.startswith("users/") for c in loaded),
            "legacy prompt builder appended a personal memory segment")

    # -- 4.10 ownership comes from the identity, not from a tool arg -----

    def test_add_tool_scope_ignores_arguments(self):
        from agent.memory.config import MemoryConfig
        from agent.memory.manager import MemoryManager
        from agent.tools.memory.memory_add import MemoryAddTool

        captured = {}

        class _Manager:
            async def add_memory(self, content, user_id=None, scope="shared", **kw):
                captured["user_id"] = user_id
                captured["scope"] = scope

        tool = MemoryAddTool(_Manager(), user_id=self.alice)
        # Callers may only pick the scope, never whose memory it is.
        tool.execute({"content": "x", "scope": "user",
                      "user_id": self.bob, "owner": self.bob})
        self.assertEqual(captured["user_id"], self.alice,
                         "a tool argument decided whose private memory was written")

    # -- 1.5 restored LLM context is filtered by owner -------------------

    def test_load_messages_filters_by_owner(self):
        from agent.memory import get_conversation_store

        store = get_conversation_store(self.ws_x)
        with use_identity(self._ident(self.alice)):
            store.append_messages(
                session_id="s-shared", channel_type="web",
                messages=[{"role": "user", "content": "ALICE-PRIVATE-LINE"}])

        with use_identity(self._ident(self.bob)):
            loaded = store.load_messages("s-shared")
            self.assertFalse(
                any("ALICE-PRIVATE-LINE" in str(m.get("content", "")) for m in loaded),
                "Bob's LLM context restored Alice's private messages")

    # -- 6.3 shared knowledge is indexed from outside the workspace ------

    def test_shared_knowledge_is_indexed_without_crashing(self):
        """An Agent with no ``knowledge/`` of its own scans the tenant's copy.

        That directory lives *outside* the Agent workspace (it is the shared
        base), so the index label cannot come from
        ``relative_to(workspace_dir)``: doing so raised ``ValueError`` and
        aborted the whole sync, leaving both memory and knowledge unindexed.
        """
        import asyncio
        import sqlite3

        knowledge = Path(self.shared) / "knowledge"
        knowledge.mkdir(parents=True, exist_ok=True)
        (knowledge / "note.md").write_text(
            "# Log\n\nKNOWLEDGEBEACON is a shared note\n", encoding="utf-8")

        with use_identity(self._ident(self.alice)):
            manager = self._manager(self.ws_x)
            asyncio.run(manager.sync())  # must not raise
            hits = asyncio.run(manager.search("KNOWLEDGEBEACON", user_id=self.alice))
            self.assertTrue(hits, "shared knowledge was not indexed at all")

            db = manager.config.get_db_path()
            con = sqlite3.connect(str(db))
            try:
                labels = {row[0] for row in con.execute(
                    "SELECT path FROM chunks WHERE text LIKE '%KNOWLEDGEBEACON%'")}
            finally:
                con.close()

        self.assertIn("knowledge/note.md", labels,
                      f"shared knowledge was indexed under an unstable label: {labels}")


class CrossTenantPersonalMemoryTestCase(unittest.TestCase):
    """Two tenants, each with a user: the user layer must not join them (5.4/5.5).

    The isolation gate draws *tenant* boundaries; the user layer draws the
    user boundary on top. Both matter here: the tenant boundary must still block
    the other tenant's roots (this change must not have loosened it), and one
    tenant's personal memory must be structurally unreachable from the other.
    """

    def setUp(self):
        self.db = _mkdb()
        self.svc = IdentityService(self.db)
        self.acme_root = tempfile.mkdtemp(prefix="acme-")
        self.beta_root = tempfile.mkdtemp(prefix="beta-")
        self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root=self.acme_root, allow_weak=True)
        self.root = self.svc.list_platform_users()[0]
        self.acme = self.svc.list_tenants()[0]
        beta = self.svc.create_tenant(
            actor_user_id=self.root["id"], code="beta", name="Beta",
            shared_root=self.beta_root, admin_username="betaadmin",
            admin_display="Beta Admin", admin_password="Str0ng2Pass",
            recent_password="Str0ngAdminPass")

        self.alice = self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.acme["id"],
            operation="create-new", username="alice", display_name="Alice",
            temporary_password="TmpPass123!", roles=[])["user_id"]
        self.beta_user = self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=beta["id"],
            operation="create-new", username="bea", display_name="Bea",
            temporary_password="TmpPass123!", roles=[])["user_id"]
        self.beta_id = beta["id"]
        self.ws = tempfile.mkdtemp(prefix="agent-")

        self._svc_patch = patch("auth.service.get_identity_service", lambda: self.svc)
        self._svc_patch.start()

    def tearDown(self):
        self._svc_patch.stop()
        from agent.memory import clear_conversation_store_cache
        from agent.memory.config import reset_memory_configs
        clear_conversation_store_cache()
        reset_memory_configs()

    def _ident(self, user_id, tenant_id):
        return RuntimeIdentity(agent_id="agent-x", user_id=user_id,
                               tenant_id=tenant_id)

    def test_personal_memory_is_not_reachable_across_tenants(self):
        import asyncio
        from agent.memory.manager import MemoryManager
        from agent.memory.config import MemoryConfig

        # Alice writes into Acme's user domain.
        with use_identity(self._ident(self.alice, self.acme["id"])):
            main = state_dir.memory_file()
            main.parent.mkdir(parents=True, exist_ok=True)
            main.write_text("ACME-ONLY-TOKEN\n", encoding="utf-8")
            self.assertTrue(str(main).startswith(self.acme_root))

        # Bea's user domain is under a different shared root entirely.
        with use_identity(self._ident(self.beta_user, self.beta_id)):
            self.assertFalse(
                str(state_dir.memory_file()).startswith(self.acme_root),
                "a Beta user resolved into Acme's shared root")
            manager = MemoryManager(
                config=MemoryConfig(workspace_root=self.ws, min_score=0.0),
                embedding_provider=None)
            asyncio.run(manager.sync())
            hits = asyncio.run(
                manager.search("ACME-ONLY-TOKEN", user_id=self.beta_user))
            self.assertFalse(hits, "Beta read Acme's personal memory")

    def test_tenant_boundary_still_blocks_the_other_tenant(self):
        from agent.permission.isolation import resolve_boundary

        with use_identity(self._ident(self.alice, self.acme["id"])):
            boundary = resolve_boundary()

        self.assertEqual(boundary.tenant_id, self.acme["id"])
        real_beta = os.path.realpath(self.beta_root)
        self.assertIn(real_beta, [os.path.realpath(b) for b in boundary.blocked],
                      "the other tenant's shared root is no longer blocked")

    def test_user_domain_is_a_legal_root(self):
        from agent.permission.isolation import resolve_boundary

        with use_identity(self._ident(self.alice, self.acme["id"])):
            boundary = resolve_boundary()
            user_root = str(state_dir.user_root())

        roots = [os.path.realpath(r) for r in boundary.read_roots]
        self.assertIn(os.path.realpath(user_root), roots,
                      "the user domain is no longer a legal read root")
        self.assertIn(os.path.realpath(user_root),
                      [os.path.realpath(r) for r in boundary.write_roots])


if __name__ == "__main__":
    unittest.main()
