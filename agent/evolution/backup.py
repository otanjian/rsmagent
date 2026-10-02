"""File backup / rollback support for self-evolution.

Before the evolution agent edits MEMORY.md or a skill file, we snapshot the
current state into ``memory/.evolution_backups/<backup_id>/`` so a later "undo"
can restore it. File-level restore only — simple and reliable.
"""

from __future__ import annotations

import json
import hashlib
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from common.log import logger

_BACKUP_DIRNAME = ".evolution_backups"
_MANIFEST_NAME = "manifest.json"
# Keep only the most recent N backups to bound disk usage.
_MAX_BACKUPS = 10


def _backups_root(workspace_dir: Path) -> Path:
    return Path(workspace_dir) / "memory" / _BACKUP_DIRNAME


def create_backup(workspace_dir: Path, files: List[Path], *, personal_service=None) -> Optional[str]:
    """Snapshot ``files`` (those that exist) under a new backup id.

    Returns the backup_id, or None when there is nothing to back up.
    """
    if personal_service is not None:
        return _create_personal_backup(workspace_dir, files, personal_service)
    existing = [Path(f) for f in files if Path(f).exists()]
    if not existing:
        return None

    backup_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + str(int(time.time() * 1000) % 1000)
    root = _backups_root(workspace_dir)
    target = root / backup_id
    try:
        target.mkdir(parents=True, exist_ok=True)
        ws = Path(workspace_dir)
        manifest = []
        for idx, src in enumerate(existing):
            # Store under a flat index plus the relative path so restore knows
            # where it came from, even for nested skill files.
            try:
                rel = str(src.relative_to(ws))
            except ValueError:
                rel = src.name
            dst = target / f"{idx}.bak"
            shutil.copy2(src, dst)
            manifest.append({"rel": rel, "bak": f"{idx}.bak"})
        (target / _MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _prune_old_backups(root)
        # Caller logs a combined backup+review line; keep this at debug.
        logger.debug(f"[Evolution] Created backup {backup_id} ({len(manifest)} file(s))")
        return backup_id
    except Exception as e:
        logger.warning(f"[Evolution] Failed to create backup: {e}")
        return None


def restore_backup(workspace_dir: Path, backup_id: str) -> bool:
    """Restore all files captured under ``backup_id``. Returns success."""
    if not backup_id:
        return False
    if backup_id.startswith('personal-'):
        return bool(restore_personal_backup(workspace_dir, backup_id))
    target = _backups_root(workspace_dir) / backup_id
    manifest_path = target / _MANIFEST_NAME
    if not manifest_path.exists():
        logger.warning(f"[Evolution] Backup not found: {backup_id}")
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        ws = Path(workspace_dir)
        # Validate the entire snapshot before changing any workspace file.
        # Otherwise a missing payload silently reports success, or a malformed
        # later entry leaves an earlier file restored even though undo failed.
        if not isinstance(manifest, list) or not manifest:
            raise ValueError("backup manifest must contain file entries")
        restores = []
        for entry in manifest:
            if not isinstance(entry, dict) or not all(
                isinstance(entry.get(key), str) and entry[key]
                for key in ("bak", "rel")
            ):
                raise ValueError("invalid backup manifest entry")
            bak = target / entry["bak"]
            dst = ws / entry["rel"]
            if not bak.is_file():
                raise FileNotFoundError(f"missing backup payload: {entry['bak']}")
            restores.append((bak, dst))
        for bak, dst in restores:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bak, dst)
        logger.info(f"[Evolution] Restored backup {backup_id} ({len(manifest)} file(s))")
        return True
    except Exception as e:
        logger.warning(f"[Evolution] Failed to restore backup {backup_id}: {e}")
        return False


def _workspace_fingerprint(workspace_dir):
    return hashlib.sha256(str(Path(workspace_dir).resolve()).encode()).hexdigest()


def _create_personal_backup(workspace_dir, files, service):
    """An account pass never copies private bodies to an Agent's shared root."""
    from common import safe_fs
    from agent.memory.personal import scope_transaction
    identity = service._require_scope()
    root, workspace = service.user_root(), Path(workspace_dir)
    with scope_transaction(root):
        entries = []
        for file in files:
            file = Path(file)
            if file.is_relative_to(root):
                relative = file.relative_to(root).as_posix()
                service._entry_relative(relative)
                scope, body = 'personal', service._read_entry(relative)
            else:
                relative = file.relative_to(workspace).as_posix()
                scope, body = 'workspace', safe_fs.read_text(workspace, relative)
            if body is not None:
                entries.append({'scope': scope, 'relative': relative, 'body': body})
        if not entries:
            return None
        backup_id = 'personal-' + uuid.uuid4().hex
        manifest = {'user_id': identity.user_id, 'tenant_id': identity.tenant_id,
                    'generation': service.scope_generation(),
                    'workspace': _workspace_fingerprint(workspace), 'entries': entries}
        safe_fs.write_text_atomic(root, f'memory/.evolution_backups/{backup_id}.json',
                                  json.dumps(manifest, ensure_ascii=False))
        snapshots = [name for name in safe_fs.list_names(root, 'memory/.evolution_backups', suffix='.json')
                     if re.fullmatch(r'personal-[0-9a-f]{32}\.json', name)]
        snapshots.sort(key=lambda name: safe_fs.stat(root, f'memory/.evolution_backups/{name}').st_mtime_ns)
        for name in snapshots[:-_MAX_BACKUPS]:
            safe_fs.unlink(root, f'memory/.evolution_backups/{name}')
        return backup_id


def restore_personal_backup(workspace_dir, backup_id):
    """Restore only this owner's snapshot, with a current publish token.

    The backup generation must survive unchanged: an explicit clear permanently
    invalidates old undo snapshots even though their files are retained.
    """
    from common import safe_fs
    from agent.memory.personal import PersonalMemoryService, PersonalMemoryError, scope_transaction
    if not re.fullmatch(r'personal-[0-9a-f]{32}', backup_id):
        raise PersonalMemoryError('无效的个人进化备份', code='invalid_backup')
    service = PersonalMemoryService()
    service._require_write_capability()
    identity = service._require_scope()
    root, workspace = service.user_root(), Path(workspace_dir)
    with scope_transaction(root):
        raw = safe_fs.read_text(root, f'memory/.evolution_backups/{backup_id}.json')
        if raw is None:
            return None
        data = json.loads(raw)
        if (data.get('user_id'), data.get('tenant_id'), data.get('workspace')) != (
                identity.user_id, identity.tenant_id, _workspace_fingerprint(workspace)):
            raise PersonalMemoryError('进化备份归属不匹配', code='forbidden', status=403)
        token = service.scope_token()
        if data.get('generation') != token['generation']:
            raise PersonalMemoryError('清空前的进化备份不可恢复', code='stale_revision', status=409)
        personal, shared, before = {}, {}, {}
        entries = data.get('entries')
        if not isinstance(entries, list) or not entries:
            raise PersonalMemoryError('进化备份已损坏', code='invalid_backup')
        for entry in entries:
            relative, body = entry['relative'], entry['body']
            if not isinstance(body, str):
                raise PersonalMemoryError('进化备份已损坏', code='invalid_backup')
            if entry['scope'] == 'personal':
                service._entry_relative(relative)
                personal[relative] = body
            elif entry['scope'] == 'workspace':
                safe_fs.split_relative(relative)
                # An account snapshot only includes the persona and editable
                # skills of its original workspace, never users or control data.
                if relative != 'AGENT.md' and not (
                        relative.startswith('skills/') and relative.endswith('/SKILL.md')):
                    raise PersonalMemoryError('无效的工作区备份条目', code='invalid_backup')
                before[relative] = safe_fs.read_text(workspace, relative)
                shared[relative] = body
            else:
                raise PersonalMemoryError('无效的备份范围', code='invalid_backup')
        written = []
        try:
            for relative, body in shared.items():
                safe_fs.write_text_atomic(workspace, relative, body)
                written.append(relative)
            return service.publish(personal, expected_scope=token)
        except Exception:
            for relative in reversed(written):
                if before[relative] is None:
                    safe_fs.unlink(workspace, relative)
                else:
                    safe_fs.write_text_atomic(workspace, relative, before[relative])
            raise


def _prune_old_backups(root: Path) -> None:
    """Drop the oldest backups beyond _MAX_BACKUPS (sorted by name = chronological)."""
    try:
        dirs = sorted(
            [d for d in root.iterdir() if d.is_dir()],
            key=lambda p: p.name,
        )
        for old in dirs[:-_MAX_BACKUPS]:
            shutil.rmtree(old, ignore_errors=True)
    except Exception as e:
        logger.debug(f"[Evolution] Backup prune skipped: {e}")
