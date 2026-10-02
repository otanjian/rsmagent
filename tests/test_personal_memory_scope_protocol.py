# encoding:utf-8
"""本人记忆的路径归属与「正文/索引/清空」并发协议（change 5.6-5.8）.

这组用例覆盖 ``enable-member-personal-console`` 交付时留下的两个真实缺口，
两者的共同点是**顺序用例看不出来**：

1. ``_target()`` 只做标识正则，列表与正文读取会跟随 ``user_root()`` 下的软链接。
   这里在条目、中间目录和根三个位置分别放置链接，断言枚举、元数据、读取、
   保存、删除和清空都拒绝，并且其他用户与共享记忆不受影响。
2. ``save`` 在释放正文锁之后才调用 ``_after_write()``，于是存在
   「正文已保存 → 清空删除正文并清理索引成功 → 原保存重新写入旧索引」的窗口。
   这里用可控屏障把两个操作真正交错起来，断言清空成功后旧正文与旧索引都不复活，
   并且被超越的发布者不得写回、也不得删除较新版本的索引。

另外覆盖重启后的不完整发布：进程在登记发布意图之后中断，重启必须继续屏蔽该标签
并如实报告 pending/incomplete，而不是把未完成的发布当作成功。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import web

from auth.runtime import RequestContext
from auth.service import IdentityService
from common import state_dir
from common.runtime_identity import RuntimeIdentity, use_identity


def _mkdb():
    return os.path.join(tempfile.mkdtemp(), "identity.db")


class _Fixture(unittest.TestCase):
    def setUp(self):
        web.ctx.headers = []
        web.ctx.status = "200 OK"
        self.db = _mkdb()
        self.svc = IdentityService(self.db)
        self.shared = tempfile.mkdtemp(prefix="acme-")
        self.svc.bootstrap(
            tenant_code="acme", tenant_name="Acme", admin_username="root",
            admin_display="Root", admin_password="Str0ngAdminPass",
            shared_root=self.shared, allow_weak=True)
        self.tid = self.svc.list_tenants()[0]["id"]
        self.root = self.svc.list_platform_users()[0]
        self.alice = self._member("alice")
        self.bob = self._member("bob")
        self.ws = tempfile.mkdtemp(prefix="agent-x-")
        self._svc_patch = patch("auth.service.get_identity_service",
                                lambda: self.svc)
        self._svc_patch.start()
        self.addCleanup(self._svc_patch.stop)
        self.addCleanup(self._reset_caches)

    @staticmethod
    def _reset_caches():
        from agent.memory import clear_conversation_store_cache
        from agent.memory.config import reset_memory_configs
        clear_conversation_store_cache()
        reset_memory_configs()

    def _member(self, username):
        self.svc.create_member(
            actor_user_id=self.root["id"], tenant_id=self.tid,
            operation="create-new", username=username, display_name=username,
            temporary_password="MemTempPass1", roles=["member"])
        return [m for m in self.svc.list_members(self.tid)["items"]
                if m["username"] == username][0]["user_id"]

    def _ident(self, user_id, agent_id="agent-x", tenant_id=None):
        return RuntimeIdentity(agent_id=agent_id, user_id=user_id,
                               tenant_id=tenant_id or self.tid)

    def _service(self, user_id, **kw):
        from agent.memory.personal import PersonalMemoryService
        return PersonalMemoryService(
            identity=self._ident(user_id), **kw)

    def _user_root(self, user_id):
        with use_identity(self._ident(user_id)):
            return Path(state_dir.user_root())

    def _index_db(self):
        return Path(self.ws) / "memory" / "long-term" / "index.db"

    def _rows(self, label, db=None):
        path = Path(db or self._index_db())
        if not path.exists():
            return 0
        conn = sqlite3.connect(str(path))
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE path = ?", (label,)).fetchone()[0]
        finally:
            conn.close()

    def _label(self, user_id, entry_id="memory/notes.md"):
        return f"memory/users/{user_id}/{entry_id[len('memory/'):]}"


# --- 5.6 path ownership ----------------------------------------------------

class PathOwnershipTests(_Fixture):
    def _assert_refused(self, callable_, *args, **kwargs):
        from agent.memory.personal import PersonalMemoryError
        with self.assertRaises(PersonalMemoryError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.code, "unsafe_path")
        self.assertEqual(raised.exception.status, 403)

    def test_a_symlinked_entry_is_not_listed_read_or_written_through(self):
        root = self._user_root(self.alice)
        os.makedirs(root / "memory", exist_ok=True)
        outside = Path(tempfile.mkdtemp()) / "secret.md"
        outside.write_text("BOB-SECRET", encoding="utf-8")
        os.symlink(str(outside), str(root / "memory" / "notes.md"))

        svc = self._service(self.alice)
        self.assertEqual(svc.list_entries(), [])
        self._assert_refused(svc.read, "memory/notes.md")
        self._assert_refused(svc.save, "memory/notes.md", "pwned")
        self.assertEqual(outside.read_text(encoding="utf-8"), "BOB-SECRET")

    def test_a_symlinked_memory_directory_is_refused_for_every_operation(self):
        root = self._user_root(self.alice)
        root.mkdir(parents=True, exist_ok=True)
        elsewhere = Path(tempfile.mkdtemp())
        (elsewhere / "notes.md").write_text("ELSEWHERE", encoding="utf-8")
        os.symlink(str(elsewhere), str(root / "memory"))

        svc = self._service(self.alice)
        self._assert_refused(svc.list_entries)
        self._assert_refused(svc.read, "memory/notes.md")
        self._assert_refused(svc.delete, "memory/notes.md")
        self._assert_refused(svc.save, "memory/notes.md", "pwned")
        self.assertTrue((elsewhere / "notes.md").exists())
        self.assertEqual((elsewhere / "notes.md").read_text(encoding="utf-8"),
                         "ELSEWHERE")

    def test_a_symlinked_user_root_is_refused(self):
        real_root = Path(tempfile.mkdtemp())
        (real_root / "MEMORY.md").write_text("REAL", encoding="utf-8")
        linked = Path(self.shared) / "users" / self.alice
        linked.parent.mkdir(parents=True, exist_ok=True)
        if linked.exists() or linked.is_symlink():
            linked.unlink()
        os.symlink(str(real_root), str(linked))

        svc = self._service(self.alice)
        from agent.memory.personal import PersonalMemoryError
        with self.assertRaises(PersonalMemoryError) as raised:
            svc.user_root()
        self.assertEqual(raised.exception.code, "unsafe_path")

    def test_replacing_the_directory_after_listing_does_not_leak_content(self):
        svc = self._service(self.alice)
        svc.save("memory/notes.md", "MINE")
        root = self._user_root(self.alice)
        abroad = Path(tempfile.mkdtemp())
        (abroad / "notes.md").write_text("THEIRS", encoding="utf-8")
        os.rename(str(root / "memory"), str(root / "memory-away"))
        os.symlink(str(abroad), str(root / "memory"))
        self._assert_refused(svc.read, "memory/notes.md")
        self.assertEqual(
            (root / "memory-away" / "notes.md").read_text(encoding="utf-8"),
            "MINE")

    def test_other_users_and_shared_memory_are_untouched_by_a_clear(self):
        alice = self._service(self.alice)
        bob = self._service(self.bob)
        alice.save("memory/notes.md", "ALICE")
        bob.save("memory/notes.md", "BOB")
        alice.clear()
        self.assertEqual(alice.read("memory/notes.md")["content"], "")
        self.assertEqual(bob.read("memory/notes.md")["content"], "BOB")


# --- 5.7 commit/publish atomicity ------------------------------------------

class CommitPublishAtomicityTests(_Fixture):
    def test_a_save_in_flight_cannot_revive_content_after_a_clear(self):
        """The measured window: save commits body, clear removes it, save
        publishes the old index anyway. The publish is inside the same critical
        section as the commit, so clear cannot interleave."""
        svc = self._service(self.alice, index_dbs=[self._index_db()])
        label = self._label(self.alice)
        entered = threading.Event()
        release = threading.Event()
        from agent.memory import personal as personal_mod
        real_index_label = personal_mod._index_label

        def slow_publish(db, label_, text, user_id, scope="user"):
            entered.set()
            release.wait(5)
            return real_index_label(db, label_, text, user_id, scope)

        errors = []

        def saver():
            try:
                svc.save("memory/notes.md", "OLD-CONTENT")
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        with patch.object(personal_mod, "_index_label", slow_publish):
            thread = threading.Thread(target=saver)
            thread.start()
            self.assertTrue(entered.wait(5), "index publish never started")
            cleared = threading.Event()

            def clearer():
                try:
                    svc.clear()
                except BaseException as exc:  # pragma: no cover
                    errors.append(exc)
                finally:
                    cleared.set()

            clearing = threading.Thread(target=clearer)
            clearing.start()
            # The clear must not be able to interleave with the publish: the
            # body is already committed and the index write is in progress.
            self.assertFalse(cleared.wait(0.3),
                             "clear interleaved with an in-flight publish")
            release.set()
            thread.join(10)
            clearing.join(10)

        self.assertEqual(errors, [])
        # Body gone, index gone: nothing the old save wrote may survive.
        self.assertFalse(
            (self._user_root(self.alice) / "memory" / "notes.md").exists())
        self.assertEqual(self._rows(label), 0)
        self.assertEqual(svc.read("memory/notes.md")["content"], "")

    def test_an_overtaken_publisher_does_not_write_rows_back(self):
        svc = self._service(self.alice, index_dbs=[self._index_db()])
        label = self._label(self.alice)
        stale_token = svc.scope_token()["op_version"]
        svc.save("memory/notes.md", "CURRENT")
        self.assertEqual(svc._after_write("memory/notes.md", "STALE",
                                          stale_token), "obsolete")
        rows = sqlite3.connect(str(self._index_db())).execute(
            "SELECT text FROM chunks WHERE path = ?", (label,)).fetchall()
        self.assertTrue(rows)
        self.assertNotIn("STALE", "".join(r[0] for r in rows))

    def test_an_overtaken_publisher_does_not_purge_a_newer_version(self):
        svc = self._service(self.alice, index_dbs=[self._index_db()])
        label = self._label(self.alice)
        stale_token = svc.scope_token()["op_version"]
        svc.save("memory/notes.md", "CURRENT")
        self.assertGreater(self._rows(label), 0)
        self.assertEqual(svc._after_remove([label], stale_token), "obsolete")
        self.assertGreater(self._rows(label), 0)

    def test_consecutive_edits_and_a_delete_leave_only_the_new_version(self):
        svc = self._service(self.alice, index_dbs=[self._index_db()])
        label = self._label(self.alice)
        first = svc.save("memory/notes.md", "V1")
        second = svc.save("memory/notes.md", "V2",
                          expected_revision=first["revision"])
        svc.delete("memory/notes.md", expected_revision=second["revision"])
        svc.save("memory/notes.md", "V3")
        rows = sqlite3.connect(str(self._index_db())).execute(
            "SELECT text FROM chunks WHERE path = ?", (label,)).fetchall()
        body = "".join(r[0] for r in rows)
        self.assertIn("V3", body)
        self.assertNotIn("V1", body)
        self.assertNotIn("V2", body)

    def test_the_clear_advances_both_generation_and_operation_version(self):
        svc = self._service(self.alice)
        svc.save("memory/notes.md", "V1")
        before = svc.scope_token()
        svc.clear()
        after = svc.scope_token()
        self.assertGreater(after["generation"], before["generation"])
        self.assertGreater(after["op_version"], before["op_version"])


# --- restart safety --------------------------------------------------------

class InterruptedPublishTests(_Fixture):
    def _scope_file(self, user_id):
        return self._user_root(user_id) / ".memory-scope.json"

    def _plant_stale_publish(self, user_id, labels):
        root = self._user_root(user_id)
        root.mkdir(parents=True, exist_ok=True)
        state = {"generation": 1, "op_version": 7, "cleared_at": None,
                 "pending_index": [],
                 "publishing": {"pid": os.getpid() + 99999, "token": 7,
                                "kind": "save", "labels": labels,
                                "at": "2026-09-15 01:00:00"}}
        (root / ".memory-scope.json").write_text(
            json.dumps(state), encoding="utf-8")

    def test_a_stale_publish_intent_keeps_the_label_masked(self):
        from agent.memory.personal import pending_index_labels
        label = self._label(self.alice)
        self._plant_stale_publish(self.alice, [label])
        with use_identity(self._ident(self.alice)):
            self.assertIn(label, pending_index_labels())

    def test_an_in_flight_publish_intent_of_this_process_is_not_masked(self):
        from agent.memory.personal import pending_index_labels
        label = self._label(self.alice)
        root = self._user_root(self.alice)
        root.mkdir(parents=True, exist_ok=True)
        state = {"generation": 0, "op_version": 3, "cleared_at": None,
                 "pending_index": [],
                 "publishing": {"pid": os.getpid(), "token": 3, "kind": "save",
                                "labels": [label], "at": None}}
        (root / ".memory-scope.json").write_text(json.dumps(state),
                                                 encoding="utf-8")
        with use_identity(self._ident(self.alice)):
            self.assertNotIn(label, pending_index_labels())

    def test_recovery_promotes_the_interrupted_publish_to_pending(self):
        label = self._label(self.alice)
        self._plant_stale_publish(self.alice, [label])
        svc = self._service(self.alice)
        status = svc.scope_status()
        self.assertTrue(status["recovered_incomplete_publish"])
        self.assertIn(label, status["pending"])
        # Idempotent: a second look finds nothing left to reconcile.
        self.assertFalse(svc.scope_status()["recovered_incomplete_publish"])

    def test_recovery_never_reports_the_interrupted_publish_as_complete(self):
        from agent.memory.personal import pending_index_labels
        label = self._label(self.alice)
        self._plant_stale_publish(self.alice, [label])
        svc = self._service(self.alice)
        list(svc.list_entries())  # any entry point may trigger recovery
        with use_identity(self._ident(self.alice)):
            self.assertIn(label, pending_index_labels())

    def test_corrupt_scope_state_refuses_publication_without_resetting_generation(self):
        from agent.memory.personal import PersonalMemoryError
        root = self._user_root(self.alice)
        root.mkdir(parents=True, exist_ok=True)
        marker = root / '.memory-scope.json'
        marker.write_text('{not json', encoding='utf-8')
        with self.assertRaises(PersonalMemoryError) as raised:
            self._service(self.alice).save('memory/notes.md', 'MUST-NOT-PUBLISH')
        self.assertEqual(raised.exception.code, 'scope_unavailable')
        self.assertEqual(marker.read_text(), '{not json')
        self.assertFalse((root / 'memory/notes.md').exists())


# --- index publish seam in MemoryManager.sync ------------------------------

class SyncPublishSeamTests(_Fixture):
    def test_a_sync_that_lost_the_scope_version_drops_the_user_domain_work(self):
        """``MemoryManager.sync`` re-reads user-domain files; if the user
        cleared while the embedding call was in flight, the captured version no
        longer matches and the publish is dropped instead of reviving content.
        """
        from agent.memory import personal as personal_mod

        svc = self._service(self.alice)
        svc.save("memory/notes.md", "BEFORE")

        ident = RuntimeIdentity(agent_id="agent-x", user_id=self.alice,
                                tenant_id=self.tid)
        captured = personal_mod.read_scope_token(ident)
        with use_identity(ident):
            self.assertTrue(personal_mod.scope_publish_is_current(
                ident, captured))
            svc.clear()
            self.assertFalse(personal_mod.scope_publish_is_current(
                ident, captured))

    def test_the_captured_token_tracks_both_generation_and_operation_version(self):
        svc = self._service(self.alice)
        first = svc.save("memory/notes.md", "V1")
        token = svc.scope_token()
        svc.save("memory/notes.md", "V2",
                 expected_revision=first["revision"])
        edited = svc.scope_token()
        self.assertGreater(edited["op_version"], token["op_version"])
        self.assertEqual(edited["generation"], token["generation"])
        svc.clear()
        cleared = svc.scope_token()
        self.assertGreater(cleared["generation"], edited["generation"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
