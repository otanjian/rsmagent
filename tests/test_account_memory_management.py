"""Account memory publication, recovery, management and migration contracts."""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from tests.test_personal_memory_scope_protocol import _Fixture
from common.runtime_identity import use_identity
from agent.memory.personal import PersonalMemoryError, PersonalMemoryService


class AccountMemoryTests(_Fixture):
    def test_all_categories_are_owned_and_clear_has_explicit_scope(self):
        svc = self._service(self.alice, index_dbs=[self._index_db()])
        for entry in ('MEMORY.md', 'memory/note.md', 'memory/dreams/day.md', 'memory/evolution/day.md'):
            svc.save(entry, 'ALICE-' + entry)
        self._service(self.bob).save('MEMORY.md', 'BOB')
        self.assertEqual(len(svc.list_entries('evolution')), 2)
        self.assertEqual(len(svc.list_entries()), 2)
        self.assertEqual(self._rows(svc.label_for('memory/dreams/day.md')), 0)
        svc.clear()
        self.assertEqual(len(svc.list_entries('all')), 2)
        revision = svc._collection_revision(svc.list_entries('all'))
        svc.clear(expected_revision=revision, clear_scope='all_personal')
        self.assertEqual(svc.list_entries('all'), [])
        self.assertEqual(self._service(self.bob).read('MEMORY.md')['content'], 'BOB')

    def test_stale_batch_cannot_overwrite_edit_or_revive_clear(self):
        svc = self._service(self.alice, index_dbs=[])
        first = svc.save('MEMORY.md', 'FIRST')
        old = svc.scope_token()
        svc.save('MEMORY.md', 'EDITED', expected_revision=first['revision'])
        with self.assertRaises(PersonalMemoryError):
            svc.publish({'MEMORY.md': 'OLD MODEL OUTPUT'}, expected_scope=old)
        old = svc.scope_token()
        svc.clear(clear_scope='all_personal')
        with self.assertRaises(PersonalMemoryError):
            svc.publish({'memory/dreams/day.md': 'OLD DIARY'}, expected_scope=old)
        svc.publish({'MEMORY.md': 'NEW'}, expected_scope=svc.scope_token())
        self.assertEqual(svc.read('MEMORY.md')['content'], 'NEW')

    def test_explicit_add_persists_and_repeated_add_is_idempotent(self):
        from agent.memory.manager import MemoryManager
        from agent.memory.config import MemoryConfig
        from agent.tools.memory.memory_add import MemoryAddTool
        with use_identity(self._ident(self.alice)):
            manager = MemoryManager(MemoryConfig(workspace_root=self.ws, min_score=0), embedding_provider=None)
            tool = MemoryAddTool(manager, user_id=self.alice)
            result = tool.execute({'content': 'ACCOUNTMEMORYBEACON'})
            self.assertEqual(result.status, 'success', result.result)
            tool.execute({'content': 'ACCOUNTMEMORYBEACON'})
            svc = self._service(self.alice)
            entries = svc.list_entries()
            self.assertEqual(len(entries), 1)
            self.assertEqual(svc.read(entries[0]['id'])['content'], 'ACCOUNTMEMORYBEACON')
            hits = asyncio.run(manager.search('ACCOUNTMEMORYBEACON', user_id=self.alice))
            self.assertTrue(hits)
            self.assertEqual(asyncio.run(manager.search('ACCOUNTMEMORYBEACON', user_id=self.bob)), [])

    def test_retry_reindexes_current_body_instead_of_only_purging(self):
        svc = self._service(self.alice, index_dbs=[self._index_db()])
        with patch('agent.memory.personal._index_label', side_effect=OSError('offline')):
            result = svc.save('MEMORY.md', 'INDEXRECOVERY')
        self.assertEqual(result['index_state'], 'pending')
        self.assertEqual(svc.retry_pending_index()['index_state'], 'ok')
        self.assertGreater(self._rows(svc.label_for('MEMORY.md')), 0)

    def test_body_batch_recovers_after_interrupted_second_file(self):
        from common import safe_fs
        svc = self._service(self.alice, index_dbs=[])
        original = safe_fs.write_text_atomic
        def fail_diary(root, path, text, *args, **kwargs):
            if path == 'memory/dreams/day.md':
                raise OSError('interrupted diary write')
            return original(root, path, text, *args, **kwargs)
        with patch.object(safe_fs, 'write_text_atomic', side_effect=fail_diary):
            with self.assertRaises(OSError):
                svc.publish({'MEMORY.md': 'MAIN', 'memory/dreams/day.md': 'DIARY'},
                            expected_scope=svc.scope_token())
        self.assertEqual(svc.read('memory/dreams/day.md')['content'], 'DIARY')
        self.assertEqual(svc.read('MEMORY.md')['content'], 'MAIN')
        self.assertFalse((svc.user_root() / '.memory-publish.json').exists())

    def test_writer_flag_blocks_tools_and_generated_content_but_not_clear(self):
        svc = self._service(self.alice, index_dbs=[])
        svc.save('MEMORY.md', 'BEFORE')
        with patch('auth.policy.personal_capability_enabled', return_value=False):
            self.assertFalse(svc.list_entries()[0]['actions']['edit'])
            with self.assertRaises(PersonalMemoryError):
                svc.add('DISABLED')
            with self.assertRaises(PersonalMemoryError):
                svc.publish({'MEMORY.md': 'DISABLED'}, expected_scope=svc.scope_token())
            self.assertEqual(svc.read('MEMORY.md')['content'], 'BEFORE')
            svc.clear(clear_scope='all_personal')
        self.assertEqual(svc.list_entries(), [])

    def test_migration_is_idempotent_owner_scoped_and_rollback_preserves_edits(self):
        from agent.memory.migration import PersonalMemoryMigration
        svc = self._service(self.alice, index_dbs=[self._index_db()])
        legacy = Path(self.ws) / 'memory/users' / self.alice
        legacy.mkdir(parents=True)
        (legacy / 'old.md').write_text('LEGACYALICE')
        other = Path(self.ws) / 'memory/users' / self.bob
        other.mkdir(parents=True)
        (other / 'old.md').write_text('BOBSECRET')
        migration = PersonalMemoryMigration(svc)
        preview = migration.preview()
        self.assertNotIn('LEGACYALICE', json.dumps(preview))
        self.assertEqual(len(preview['entries']), 1)
        migration.apply(preview)
        migration.apply(preview)
        self.assertEqual(len(svc.list_entries()), 1)
        saved = svc.read('memory/old.md')
        svc.save('memory/old.md', 'EDITED', expected_revision=saved['revision'])
        migration.rollback()
        self.assertEqual(svc.read('memory/old.md')['content'], 'EDITED')
        svc.clear(clear_scope='all_personal')
        with self.assertRaises(PersonalMemoryError):
            migration.apply(preview)
        self.assertEqual(migration.preview()['entries'], [])

    def test_migration_index_fragments_do_not_claim_original_text(self):
        from agent.memory.migration import PersonalMemoryMigration
        from agent.memory.personal import _index_label
        svc = self._service(self.alice, index_dbs=[self._index_db()])
        _index_label(self._index_db(), svc.label_for('memory/lost.md'), 'ONLYFRAGMENT', self.alice)
        migration = PersonalMemoryMigration(svc)
        preview = migration.preview()
        self.assertEqual(preview['entries'][0]['kind'], 'index_fragments')
        migration.apply(preview)
        content = svc.read(preview['entries'][0]['target'])['content']
        self.assertIn('可能不完整', content)
        self.assertIn('ONLYFRAGMENT', content)

    def test_producer_identity_mismatch_is_refused(self):
        from agent.memory.personal import personal_service_for
        with use_identity(self._ident(self.alice)):
            with self.assertRaises(PersonalMemoryError):
                personal_service_for(self.bob)

    def test_evolution_stages_personal_edits_until_valid_commit(self):
        from agent.memory.personal import _personal_stage
        from agent.tools.write.write import Write
        svc = self._service(self.alice, index_dbs=[])
        svc.save('MEMORY.md', 'BEFORE')
        with use_identity(self._ident(self.alice)):
            stage = {'root': svc.user_root(), 'token': svc.scope_token(), 'changes': {}}
            token = _personal_stage.set(stage)
            try:
                tool = Write({'cwd': str(svc.user_root())})
                result = tool.execute({'path': 'MEMORY.md', 'content': 'STAGED'})
                self.assertEqual(result.status, 'success', result.result)
                self.assertEqual(svc.read('MEMORY.md')['content'], 'BEFORE')
                svc.clear(clear_scope='all_personal')
                with self.assertRaises(PersonalMemoryError):
                    svc.publish(stage['changes'], expected_scope=stage['token'])
            finally:
                _personal_stage.reset(token)


    def test_preview_is_read_only_and_migration_resumes_after_precommit_failure(self):
        from agent.memory.migration import PersonalMemoryMigration
        svc = self._service(self.alice, index_dbs=[self._index_db()])
        legacy = Path(self.ws) / 'memory/users' / self.alice
        legacy.mkdir(parents=True)
        (legacy / 'one.md').write_text('FIRST')
        migration = PersonalMemoryMigration(svc)
        self.assertFalse(svc.user_root().exists())
        preview = migration.preview()
        self.assertFalse(svc.user_root().exists(), 'preview created owner state')
        with patch.object(svc, 'publish', side_effect=OSError('process stopped before commit')):
            with self.assertRaises(OSError):
                migration.apply(preview)
        recovered = PersonalMemoryMigration(svc).apply(preview)
        self.assertEqual(recovered['index_state'], 'ok')
        self.assertEqual(svc.read('memory/one.md')['content'], 'FIRST')
        (legacy / 'two.md').write_text('SECOND')
        second = migration.preview()
        migration.apply(second)
        migration.rollback(second['batch_id'])
        self.assertEqual(svc.read('memory/one.md')['content'], 'FIRST')
        self.assertEqual(svc.read('memory/two.md')['content'], '')

    def test_summary_dream_and_evolution_produce_real_owner_files(self):
        from agent.memory.summarizer import MemoryFlushManager
        from agent.evolution.record import append_session_evolution
        from datetime import datetime
        class Model:
            def call(self, request):
                return {'choices':[{'message':{'content':'[MEMORY]\n个人长期偏好\n[DREAM]\n本次归纳记录'}}]}
        with use_identity(self._ident(self.alice)):
            manager = MemoryFlushManager(Path(self.ws), Model())
            self.assertTrue(manager.write_daily_summary('SUMMARY-BODY', user_id=self.alice))
            self.assertTrue(manager.deep_dream(user_id=self.alice, force=True))
            append_session_evolution(self.ws, 'EVOLUTION-BODY', user_id=self.alice)
            svc = self._service(self.alice)
            self.assertEqual(len(svc.list_entries('all')), 4)
            self.assertIn('个人长期偏好', svc.read('MEMORY.md')['content'])
            today = datetime.now().strftime('%Y-%m-%d')
            self.assertIn('本次归纳记录', svc.read(f'memory/dreams/{today}.md')['content'])
            self.assertIn('EVOLUTION-BODY', svc.read(f'memory/evolution/{today}.md')['content'])
        self.assertEqual(self._service(self.bob).list_entries('all'), [])

    def test_dream_drops_stale_model_output_after_manual_edit(self):
        from agent.memory.summarizer import MemoryFlushManager
        svc = self._service(self.alice, index_dbs=[])
        original = svc.save('MEMORY.md', 'BEFORE')
        class Model:
            def call(self, request):
                svc.save('MEMORY.md', 'HUMAN-EDIT', expected_revision=original['revision'])
                return {'choices':[{'message':{'content':'[MEMORY]\nSTALE\n[DREAM]\nSTALE-DIARY'}}]}
        with use_identity(self._ident(self.alice)):
            manager = MemoryFlushManager(Path(self.ws), Model())
            manager.write_daily_summary('DAILY', user_id=self.alice)
            self.assertFalse(manager.deep_dream(user_id=self.alice, force=True))
        self.assertEqual(svc.read('MEMORY.md')['content'], 'HUMAN-EDIT')
        self.assertEqual(svc.list_entries('dream'), [])

    def test_two_agents_append_then_stale_summary_cannot_restore_clear(self):
        from agent.memory.summarizer import MemoryFlushManager
        with use_identity(self._ident(self.alice)):
            svc = self._service(self.alice)
            token = svc.scope_token()
            one = MemoryFlushManager(Path(self.ws), None)
            two = MemoryFlushManager(Path(self.ws), None)
            self.assertTrue(one.write_daily_summary('AGENT-ONE', user_id=self.alice, scope_generation=token))
            self.assertFalse(two.write_daily_summary('AGENT-TWO', user_id=self.alice, scope_generation=token))
            self.assertTrue(two.write_daily_summary('AGENT-TWO', user_id=self.alice, scope_generation=svc.scope_token()))
            entry = svc.list_entries()[0]['id']
            self.assertIn('AGENT-ONE', svc.read(entry)['content'])
            self.assertIn('AGENT-TWO', svc.read(entry)['content'])
            token = svc.scope_token()
            with patch('auth.policy.personal_capability_enabled', return_value=False):
                self.assertFalse(two.write_daily_summary('DISABLED', user_id=self.alice, scope_generation=token))
                svc.clear(clear_scope='all_personal')
            self.assertFalse(one.write_daily_summary('STALE', user_id=self.alice, scope_generation=token))
            self.assertTrue(one.write_daily_summary('NEW', user_id=self.alice, scope_generation=svc.scope_token()))
            self.assertNotIn('STALE', svc.read(svc.list_entries()[0]['id'])['content'])

    def test_clear_cleans_index_only_rows_and_retry_keeps_new_writes(self):
        from agent.memory.personal import _index_label
        svc = self._service(self.alice, index_dbs=[self._index_db()])
        orphan = svc.label_for('memory/orphan.md')
        _index_label(self._index_db(), orphan, 'ORPHAN', self.alice)
        self.assertGreater(self._rows(orphan), 0)
        with patch('agent.memory.personal._purge_label', side_effect=OSError('offline')):
            self.assertEqual(svc.clear(clear_scope='all_personal')['index_state'], 'pending')
        svc.save('MEMORY.md', 'NEW-AFTER-CLEAR')
        self.assertEqual(svc.retry_pending_index()['index_state'], 'ok')
        self.assertEqual(self._rows(orphan), 0)
        self.assertGreater(self._rows(svc.label_for('MEMORY.md')), 0)

    def test_deleted_legacy_source_is_excluded_and_audit_has_no_body(self):
        from agent.memory.migration import PersonalMemoryMigration
        svc = self._service(self.alice, index_dbs=[self._index_db()])
        legacy = Path(self.ws) / 'memory/users' / self.alice
        legacy.mkdir(parents=True)
        (legacy / 'old.md').write_text('DO-NOT-REVIVE')
        saved = svc.save('memory/old.md', 'PRIVATE-AUDIT-BEACON')
        svc.delete('memory/old.md', expected_revision=saved['revision'])
        preview = PersonalMemoryMigration(svc).preview()
        self.assertEqual(preview['entries'], [])
        self.assertEqual(preview['excluded'][0]['reason'], 'previously_deleted')
        rows = self.svc._store.execute("SELECT redacted_changes FROM audit_events WHERE action LIKE 'personal_memory.%'")
        self.assertTrue(rows)
        self.assertNotIn('PRIVATE-AUDIT-BEACON', str([dict(row) for row in rows]))

    def test_personal_evolution_backup_is_private_and_clear_invalidates_undo(self):
        from agent.evolution.backup import create_backup, restore_personal_backup
        from common import safe_fs
        with use_identity(self._ident(self.alice)):
            svc = self._service(self.alice)
            svc.save('MEMORY.md', 'PRIVATE-BEFORE-EVOLUTION')
            skill = Path(self.ws) / 'skills/custom/SKILL.md'
            skill.parent.mkdir(parents=True)
            skill.write_text('SKILL-BEFORE')
            backup = create_backup(Path(self.ws), [svc.user_root() / 'MEMORY.md', skill], personal_service=svc)
            self.assertFalse((Path(self.ws) / 'memory/.evolution_backups').exists())
            self.assertTrue(safe_fs.is_file(svc.user_root(), f'memory/.evolution_backups/{backup}.json'))
            svc.save('MEMORY.md', 'EVOLVED', expected_revision=svc.read('MEMORY.md')['revision'])
            skill.write_text('SKILL-EVOLVED')
            with use_identity(self._ident(self.bob)):
                self.assertIsNone(restore_personal_backup(self.ws, backup))
            self.assertEqual(svc.read('MEMORY.md')['content'], 'EVOLVED')
            self.assertEqual(restore_personal_backup(self.ws, backup)['index_state'], 'ok')
            self.assertEqual(svc.read('MEMORY.md')['content'], 'PRIVATE-BEFORE-EVOLUTION')
            self.assertEqual(skill.read_text(), 'SKILL-BEFORE')
            svc.clear(clear_scope='all_personal')
            skill.write_text('NEW-SKILL')
            with self.assertRaises(PersonalMemoryError):
                restore_personal_backup(self.ws, backup)
            self.assertIsNone(svc.read('MEMORY.md')['revision'])
            self.assertEqual(skill.read_text(), 'NEW-SKILL')
            self.assertTrue(safe_fs.is_file(svc.user_root(), f'memory/.evolution_backups/{backup}.json'))

    def test_personal_undo_checks_flag_workspace_and_reports_pending(self):
        from agent.evolution.backup import create_backup, restore_personal_backup
        with use_identity(self._ident(self.alice)):
            svc = self._service(self.alice, index_dbs=[self._index_db()])
            svc.save('MEMORY.md', 'BEFORE')
            backup = create_backup(Path(self.ws), [svc.user_root() / 'MEMORY.md'], personal_service=svc)
            svc.save('MEMORY.md', 'AFTER', expected_revision=svc.read('MEMORY.md')['revision'])
            with patch('auth.policy.personal_capability_enabled', return_value=False):
                with self.assertRaises(PersonalMemoryError):
                    restore_personal_backup(self.ws, backup)
            with self.assertRaises(PersonalMemoryError):
                restore_personal_backup(Path(self.ws) / 'other-agent', backup)
            with patch.object(PersonalMemoryService, '_index_dbs', return_value=[self._index_db()]), \
                    patch('agent.memory.personal._index_label', side_effect=OSError('offline')):
                self.assertEqual(restore_personal_backup(self.ws, backup)['index_state'], 'pending')
            self.assertEqual(svc.read('MEMORY.md')['content'], 'BEFORE')
            self.assertEqual(svc.retry_pending_index()['index_state'], 'ok')

    def test_actual_evolution_executor_commits_personal_tools_and_undo_snapshot(self):
        from agent.evolution.executor import run_evolution_for_session
        from agent.evolution.config import EvolutionConfig
        from agent.tools.write.write import Write
        from tests.test_evolution_safety import _Agent, _Bridge
        with use_identity(self._ident(self.alice)), \
                patch('agent.evolution.executor.get_evolution_config', return_value=EvolutionConfig(enabled=True)):
            svc = self._service(self.alice)
            svc.save('MEMORY.md', 'BEFORE')
            path = str(svc.user_root() / 'MEMORY.md')
            for verdict, expected in [('[SILENT]', False), ('Updated account preference', True)]:
                agent = _Agent(self.ws, Write({'cwd': self.ws}))
                bridge = _Bridge(agent, verdict, write_path=path)
                self.assertEqual(run_evolution_for_session(bridge, 'session', user_id=self.alice), expected)
                if not expected:
                    self.assertEqual(svc.read('MEMORY.md')['content'], 'BEFORE')
            self.assertEqual(svc.read('MEMORY.md')['content'], 'modified')
            self.assertTrue(svc.list_entries('evolution'))
            self.assertIn('personal-', bridge.injected[0])
            self.assertFalse((Path(self.ws) / 'MEMORY.md').exists())
            self.assertFalse((Path(self.ws) / 'memory/.evolution_backups').exists())


def _process_increment_scope(root, count):
    from agent.memory.personal import _scope_update
    for _ in range(count):
        def increment(state):
            state['op_version'] += 1
        _scope_update(Path(root), increment)


def test_scope_file_serializes_independent_processes(tmp_path):
    import multiprocessing
    from agent.memory.personal import read_scope_state
    ctx = multiprocessing.get_context('spawn')
    workers = [ctx.Process(target=_process_increment_scope, args=(str(tmp_path), 15)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(20)
        assert worker.exitcode == 0
    assert read_scope_state(tmp_path)['op_version'] == 30


def test_two_tenants_two_users_two_agents_tool_to_formal_api_and_relogin(web_app):
    from agent.memory.manager import MemoryManager
    from agent.memory.config import MemoryConfig
    from agent.tools.memory.memory_add import MemoryAddTool
    from common.runtime_identity import RuntimeIdentity
    from agent.registry import get_agent_registry
    from pathlib import Path
    h = web_app('account-memory-e2e')
    h.add_agent('memory-a', 'memory-b')
    role = h.role('memory-users', ['chat.use','agent.use','agent.read'],
                  [('agent','agent:memory-a','use'), ('agent','agent:memory-b','use')])
    alice = h.member('alice', ['member', role['code']])
    bob = h.member('bob', ['member', role['code']])
    token = h.login('alice')
    identity = RuntimeIdentity(tenant_id=h.tenant_id, user_id=alice, agent_id='memory-a')
    with use_identity(identity):
        managers = [MemoryManager(MemoryConfig(workspace_root=get_agent_registry().get(
            agent_id=agent).workspace, min_score=0), embedding_provider=None) for agent in ('memory-a','memory-b')]
        result = MemoryAddTool(managers[0],user_id=alice).execute({'content':'ACCOUNTOWNERALPHABRAVO'})
        assert result.status == 'success', result.result
        for manager in managers:
            hits = asyncio.run(manager.search('ACCOUNTOWNERALPHABRAVO', user_id=alice))
            assert hits
            assert asyncio.run(manager.search('ACCOUNTOWNERALPHABRAVO', user_id=bob)) == []
    for login_token in (token, h.login('alice')):
        response = h.get('/api/memory?scope=personal',token=login_token)
        body = json.loads(response.data)
        assert body['total'] == 1, body
        filename = body['list'][0]['filename']
        content = json.loads(h.get('/api/memory/content?scope=personal&filename='+filename, token=login_token).data)
        assert content['content'] == 'ACCOUNTOWNERALPHABRAVO'
    assert json.loads(h.get('/api/memory?scope=personal',token=h.login('bob')).data)['total'] == 0
    # Same login account admitted to another tenant must still have a different
    # file root. The normal request middleware resolves its selected tenant.
    second = h.service.create_tenant(actor_user_id=h.admin_id, code='second', name='Second',
        shared_root='', admin_username='second-root', admin_display='Second Root',
        admin_password='SecondPass123!',recent_password=h.ADMIN_PASSWORD)['id']
    h.service.create_member(actor_user_id=h.admin_id,tenant_id=second,operation='bind-existing',
        username='alice',display_name='Alice',temporary_password='',roles=['member'])
    response = h.get('/api/memory?scope=personal',token=token,headers={'X-Tenant-ID':second})
    assert json.loads(response.data)['total'] == 0, response.data
    h.add_agent('second-agent',tenant_id=second)
    with use_identity(RuntimeIdentity(tenant_id=second,user_id=alice,agent_id='second-agent')):
        svc = PersonalMemoryService()
        svc.add('SECONDONLY')
        assert svc.read('MEMORY.md')['content'] == ''
        assert all('ALPHABRAVO' not in svc.read(e['id'])['content'] for e in svc.list_entries())
    # Full deletion in one tenant cannot touch the other, and no old Agent
    # index is allowed to revive the deleted body on sync.
    collection = json.loads(h.get('/api/memory?scope=personal',token=token).data)['collection_revision']
    response = h.post('/api/memory/clear', {'scope':'personal','clear_scope':'all_personal','revision':collection},token=token)
    assert response.status.startswith('200'), response.data
    with use_identity(identity):
        for manager in managers:
            assert asyncio.run(manager.search('ACCOUNTOWNERALPHABRAVO', user_id=alice)) == []
    assert json.loads(h.get('/api/memory?scope=personal',token=token,headers={'X-Tenant-ID':second}).data)['total'] == 1
