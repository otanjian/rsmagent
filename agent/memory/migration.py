"""Owner-scoped recovery of legacy personal files and index-only memories.

Preview is read-only. Apply re-discovers every candidate under the same verified
identity; a caller cannot turn a preview into an arbitrary file import.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from common import safe_fs
from agent.memory.personal import PersonalMemoryError, scope_transaction, read_scope_state


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class PersonalMemoryMigration:
    def __init__(self, service):
        self.service = service
        self.identity = service._require_scope()
        self.root = service.user_root()

    def _candidates(self):
        uid = self.identity.user_id
        prefix = f'memory/users/{uid}/'
        candidates, excluded = {}, []
        deleted = set(read_scope_state(self.root).get('deleted_labels', []))
        # A post-clear import cannot prove the legacy copy survived intentionally.
        if self.service.scope_generation():
            return {}, [{'reason': 'scope_previously_cleared'}]
        for db in self.service._index_dbs():
            workspace = Path(db).parent.parent.parent
            legacy = f'memory/users/{uid}'
            try:
                names = safe_fs.list_names(workspace, legacy, suffix='.md', skip_dotfiles=True)
                for name in names:
                    text = safe_fs.read_text(workspace, f'{legacy}/{name}')
                    target = 'MEMORY.md' if name == 'MEMORY.md' else f'memory/{name}'
                    self._candidate(candidates, target, text, 'file', f'{workspace}/{legacy}/{name}')
            except (safe_fs.UnsafePathError, OSError):
                excluded.append({'reason': 'unsafe_legacy_directory'})
            if not Path(db).is_file() or Path(db).is_symlink():
                continue
            with sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT path,start_line,end_line,text FROM chunks "
                    "WHERE scope='user' AND user_id=? AND source='memory' ORDER BY path,start_line",
                    (uid,)).fetchall()
            grouped = {}
            for row in rows:
                if not row['path'].startswith(prefix):
                    excluded.append({'reason': 'unverified_index_path'})
                    continue
                grouped.setdefault(row['path'], []).append(dict(row))
            for label, chunks in grouped.items():
                if label in deleted:
                    excluded.append({'reason': 'previously_deleted'})
                    continue
                name = label[len(prefix):]
                target = 'MEMORY.md' if name == 'MEMORY.md' else f'memory/{name}'
                try:
                    self.service._entry_relative(target)
                    if self.service._read_entry(target) is not None or target in candidates:
                        continue
                except PersonalMemoryError:
                    excluded.append({'reason': 'unsupported_legacy_entry'})
                    continue
                # No claim of complete recovery: overlaps/gaps are kept as source
                # fragments instead of synthesising a purported original file.
                fragments = list(dict.fromkeys(c['text'] for c in chunks))
                body = '# 恢复的记忆片段\n\n来源：历史个人记忆索引；原文件缺失，内容可能不完整。\n\n'
                body += '\n\n---\n\n'.join(fragments)
                target = f'memory/recovered-{_hash(label)[:24]}.md'
                self._candidate(candidates, target, body, 'index_fragments', label)
        for target in list(candidates):
            if self.service.label_for(target) in deleted:
                del candidates[target]
                excluded.append({'target': target, 'reason': 'previously_deleted'})
        return candidates, excluded

    @staticmethod
    def _candidate(result, target, content, kind, source):
        if content is None:
            return
        fingerprint = _hash(content)
        existing = result.get(target)
        if existing and existing['fingerprint'] != fingerprint:
            target = f'memory/recovered-{fingerprint[:24]}.md'
        result[target] = {'target': target, 'fingerprint': fingerprint,
                          'kind': kind, 'source_id': _hash(source), 'content': content}

    def preview(self):
        # No lock-file creation or recovery on a preview. An optimistic version
        # check detects concurrent owner mutations; apply revalidates everything.
        token = self.service.scope_token()
        candidates, excluded = self._candidates()
        rows, existing = [], []
        for directory in ('', 'memory', 'memory/evolution', 'memory/dreams'):
            names = ['MEMORY.md'] if not directory else safe_fs.list_names(
                self.root, directory, suffix='.md', skip_dotfiles=True)
            for name in names:
                entry = f'{directory}/{name}' if directory else name
                if self.service._entry_id_pattern().fullmatch(entry) and safe_fs.is_file(self.root, entry):
                    existing.append({'id': entry, 'revision': self.service._entry_info(entry)['revision']})
        for item in candidates.values():
            row = {k: v for k, v in item.items() if k != 'content'}
            current = self.service._read_entry(item['target'])
            row['state'] = ('ready' if current is None else
                            'existing' if _hash(current) == item['fingerprint'] else 'conflict')
            rows.append(row)
        if token != self.service.scope_token():
            raise PersonalMemoryError('预览期间记忆发生变化，请重试', code='stale_revision', status=409)
        batch = _hash(json.dumps([self.identity.tenant_id, self.identity.user_id,
                                  token, rows], sort_keys=True))[:24]
        return {'batch_id': batch, 'tenant_id': self.identity.tenant_id, 'user_id': self.identity.user_id,
                'scope_token': token, 'existing': existing, 'entries': rows, 'excluded': excluded}

    def _ledger(self):
        raw = safe_fs.read_text(self.root, '.memory-migration/state.json')
        return json.loads(raw) if raw else {}

    def apply(self, preview):
        with scope_transaction(self.root):
            if (preview.get('tenant_id'), preview.get('user_id')) != (
                    self.identity.tenant_id, self.identity.user_id):
                raise PersonalMemoryError('迁移归属不匹配', code='forbidden', status=403)
            if preview.get('scope_token', {}).get('generation') != self.service.scope_generation():
                raise PersonalMemoryError('清空后旧迁移不可重放', code='stale_revision', status=409)
            candidates, _ = self._candidates()
            batch_id = preview.get('batch_id')
            if not isinstance(batch_id, str) or not batch_id:
                raise PersonalMemoryError('缺少迁移批次标识', code='invalid_plan')
            ledger = self._ledger()
            results = []
            for row in preview.get('entries', []):
                target = row['target']
                item = candidates.get(target)
                if not item or item['fingerprint'] != row['fingerprint']:
                    results.append({'target': target, 'state': 'source_changed'})
                    continue
                prior = ledger.get(target)
                current = self.service.read(target)
                if prior and current['revision'] == prior.get('revision'):
                    results.append({'target': target, 'state': 'already_imported'})
                    continue
                resumable = prior and prior.get('state') == 'publishing' and current['revision'] is None
                if current['revision'] is not None or (prior and not resumable):
                    results.append({'target': target, 'state': 'conflict'})
                    continue
                # Backup only this owner's source text, never another user's DB rows.
                safe_fs.write_text_atomic(self.root,
                    f".memory-migration/backups/{item['fingerprint']}.md", item['content'])
                ledger[target] = {'revision': item['fingerprint'], 'state': 'publishing',
                                  'source_id': item['source_id'], 'generation': self.service.scope_generation(),
                                  'batch_id': batch_id}
                safe_fs.write_text_atomic(self.root, '.memory-migration/state.json', json.dumps(ledger))
                outcome = self.service.publish({target: item['content']},
                                               expected_scope=self.service.scope_token())
                ledger[target]['state'] = outcome['index_state']
                safe_fs.write_text_atomic(self.root, '.memory-migration/state.json', json.dumps(ledger))
                results.append({'target': target, 'state': outcome['index_state']})
            return {'batch_id': batch_id, 'entries': results,
                    'index_state': self.service.retry_pending_index()['index_state']}

    def rollback(self, batch_id=None):
        with scope_transaction(self.root):
            ledger, results = self._ledger(), []
            if batch_id is None and ledger:
                batch_id = next(reversed(ledger.values())).get('batch_id')
            for target, row in ledger.items():
                if row.get('batch_id') != batch_id:
                    continue
                current = self.service.read(target)
                if current['revision'] == row.get('revision') and row.get('state') != 'rolled_back':
                    self.service.delete(target, expected_revision=row['revision'])
                    row['state'] = 'rolled_back'
                    results.append({'target': target, 'state': 'rolled_back'})
                else:
                    results.append({'target': target, 'state': 'kept'})
            safe_fs.write_text_atomic(self.root, '.memory-migration/state.json', json.dumps(ledger))
            return {'entries': results, 'index_state': self.service.retry_pending_index()['index_state']}
