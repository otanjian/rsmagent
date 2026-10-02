# encoding:utf-8
"""成员「我的记忆」：可信作用域、版本条件与可恢复的索引一致性.

（``enable-member-personal-console`` / ``fix-account-memory-management``）

为什么单独一个模块
------------------
现有 ``MemoryService(workspace_root)`` 是**该私有 Agent 记忆**的读取入口：它按 Agent
工作区列举 ``MEMORY.md`` 和 ``memory/*.md``。成员本人的长期记忆不是 Agent 的属性，
而是「当前租户 + 当前用户」的事实（``state_dir.user_root()`` /
``shared_root()/users/<user_id>``），跨本人获准的任意智能体可见。两者共用归属校验，
但存储根不同，所以入口和判决都必须分开，而不是给前者加一个 ``scope`` 参数。

本模块遵守以下规则：

1. **归属来自身份，不来自请求。** 入口只接受相对标识，没有 agent、没有 user_id、
   没有绝对路径；解析结果永远落在调用者自己的用户域内，因此「读取他人个人记忆」
   没有可构造的地址。缺少可信租户/用户时一律拒绝。
2. **写入带版本条件。** 编辑、删除、清空都携带上一次读到的 revision，
   冲突返回 409 而不是覆盖新内容。
3. **修改与索引一致且可恢复。** 内容删除/清空后，索引清理在所有已知 Agent 的索引库
   上进行；任一失败不算成功，失败标签进入待重试记录，并在检索入口继续屏蔽，
   直到重试成功——删除的正文不会从旧索引返回。
4. **清空推进作用域版本。** 清空前排队的自动固化任务（flush/dream）携带派发时的
   版本，版本过期即拒绝写回；清空后的新任务按新版本正常固化。

5. **路径归属在使用时校验。** 入口只接受相对标识，解析与读写都经
   ``common.safe_fs`` 的锚定访问：逐段 ``O_NOFOLLOW`` 打开、软链接（条目、
   中间目录或根）与 ``..``/绝对路径一律拒绝。校验过的目录描述符就是实际使用的
   目录，因此「先检查后替换」不能把操作重定向到根外。

6. **正文、索引与清空共用一个操作版本。** 作用域状态里保存单调递增的
   ``op_version``（每次保存/删除/清空都 +1）与 ``generation``（仅清空 +1）。
   变更在提交正文时先登记发布意图，索引发布前后都重新校验版本；版本已变的发布
   不得写回旧内容，也不得删除较新版本的索引。发布意图持久化，进程中断后由
   ``recover_incomplete_publish`` 提升为待重试并继续屏蔽，重启不会把未完成
   的发布当作成功。

索引标签（与 ``MemoryManager.sync`` 使用的标签一致）::

    MEMORY.md          -> memory/users/<user_id>/MEMORY.md
    memory/notes.md    -> memory/users/<user_id>/notes.md

发布串行化
----------
``scope_transaction`` 同时持有进程内可重入锁和用户根的持久文件锁。
正文、索引、清空与恢复共享该边界；控制文件损坏时拒绝发布，不重置版本。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from common import safe_fs
from common.log import logger
from common.safe_fs import UnsafePathError

MAIN_ENTRY_ID = "MEMORY.md"

#: Only the four documented categories are addressable. Every wider
#: grammar (nested dirs, other extensions, absolute paths) is a way for a
#: caller to name something that is not a personal memory entry.
_ENTRY_ID_RE = re.compile(
    r"^(?:MEMORY\.md|memory/(?:evolution/|dreams/)?[A-Za-z0-9._-]+\.md)$")

#: The per-scope marker file, beside the memory it governs. Holds the clear
#: generation and the index labels awaiting a retried purge.
_SCOPE_FILE = ".memory-scope.json"

#: Guards read-modify-write of the scope file within one process.
_scope_lock = threading.RLock()

#: Serialises the read-compare-write of an entry's version condition. The
#: console is served by one process, so this is what makes "two pages save the
#: same revision" resolve to exactly one winner instead of both passing the
#: check before either write lands.
_entry_lock = threading.RLock()
_held_roots = threading.local()
_personal_stage = ContextVar('personal_memory_stage', default=None)


def personal_file(path):
    """Map an ordinary file tool's personal-memory target to the owner service."""
    from common.runtime_identity import current_identity
    from common import state_dir
    ident = current_identity()
    if not ident.user_id or not ident.tenant_id:
        return None
    service = PersonalMemoryService(identity=ident)
    absolute = Path(os.path.abspath(path))
    root = service.user_root()
    resolved = Path(os.path.realpath(absolute))
    users = Path(state_dir.shared_root(ident)) / 'users'
    try:
        relative = absolute.relative_to(root).as_posix()
    except ValueError:
        # Other members' private files must not become an alternative entry.
        if absolute.is_relative_to(users) or resolved.is_relative_to(users.resolve()):
            raise PersonalMemoryError('不可访问其他账号的个人文件', code='forbidden', status=403)
        return None
    if not _ENTRY_ID_RE.fullmatch(relative):
        if relative.startswith('.memory') or relative.startswith('memory/'):
            raise PersonalMemoryError('不可修改记忆控制文件', code='invalid_entry')
        return None
    return service, relative


def read_personal_file(target):
    service, entry = target
    stage = _personal_stage.get()
    if stage and stage['root'] == service.user_root() and entry in stage['changes']:
        return stage['changes'][entry]
    return service.read(entry)['content']


def write_personal_file(target, content, previous):
    service, entry = target
    stage = _personal_stage.get()
    if stage:
        if stage['root'] != service.user_root():
            raise PersonalMemoryError('后台任务身份不匹配', code='no_identity', status=403)
        if stage['token'] != service.scope_token():
            raise PersonalMemoryError('记忆任务版本过期', code='stale_revision', status=409)
        service._require_write_capability()
        stage['changes'][entry] = content
        return {'index_state': 'staged'}
    return service.save(entry, content, expected_revision=(
        _revision_of(previous) if previous is not None else None))


@contextmanager
def scope_transaction(root):
    """One reentrant commit boundary shared by files, indices and processes."""
    with _entry_lock:
        key = os.path.abspath(root)
        held = getattr(_held_roots, 'roots', set())
        if key in held:
            yield
            return
        try:
            with safe_fs.file_lock(root, '.memory.lock'):
                _held_roots.roots = held | {key}
                try:
                    yield
                finally:
                    _held_roots.roots = held
        except UnsafePathError as error:
            raise PersonalMemoryError('个人记忆目录被替换', code='unsafe_path', status=403) from error


def personal_service_for(user_id):
    """Resolve a producer's user against its captured, trusted runtime identity."""
    from common.runtime_identity import current_identity
    ident = current_identity()
    if not user_id or user_id != ident.user_id or not ident.tenant_id:
        raise PersonalMemoryError('个人记忆任务身份不匹配', code='no_identity', status=403)
    return PersonalMemoryService(identity=ident)


class PersonalMemoryError(Exception):
    """A refused personal-memory operation.

    ``code`` is the machine-readable reason the console branches on;
    ``status`` is the HTTP status the handler should answer with.
    """

    def __init__(self, message: str, *, code: str = "error", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _revision_of(text: str) -> str:
    """Content revision: a hash, not a timestamp.

    Two edits within the same second must not look like the same version, and a
    timestamp can be preserved by a copy. The hash changes whenever the bytes do.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` without ever exposing a half-written file."""
    safe_fs.write_text_atomic(path.parent, path.name, text)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --- scope state: clear generation + operation version + index repairs -------


def _default_scope_state() -> Dict[str, Any]:
    return {"generation": 0, "op_version": 0, "cleared_at": None,
            "pending_index": [], "publishing": None, "deleted_labels": []}


def _coerce_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_publishing(value) -> Optional[Dict[str, Any]]:
    """A persisted publish intent, or ``None`` when absent/unreadable.

    A malformed record is dropped rather than guessed at: the labels it named
    are unknown, and inventing a set would either mask the wrong entries or
    claim a publish completed that never did.
    """
    if not isinstance(value, dict):
        return None
    labels = value.get("labels")
    if not isinstance(labels, list):
        return None
    return {
        "pid": _coerce_int(value.get("pid")),
        "token": _coerce_int(value.get("token")),
        "kind": str(value.get("kind") or ""),
        "labels": [str(x) for x in labels],
        "at": value.get("at"),
    }


def read_scope_state(root: Path) -> Dict[str, Any]:
    """Absence is a new scope; unreadable recovery state must fail closed."""
    try:
        raw = safe_fs.read_text(Path(root), _SCOPE_FILE)
    except (UnsafePathError, OSError) as e:
        logger.warning("[PersonalMemory] unreadable scope marker: %s", e)
        raise PersonalMemoryError("记忆恢复状态不可读取", code="scope_unavailable", status=503) from e
    if raw is None:
        return _default_scope_state()
    try:
        data = json.loads(raw)
    except Exception as e:
        logger.warning("[PersonalMemory] unreadable scope marker: %s", e)
        raise PersonalMemoryError("记忆恢复状态已损坏", code="scope_unavailable", status=503) from e
    if not isinstance(data, dict):
        raise PersonalMemoryError("记忆恢复状态已损坏", code="scope_unavailable", status=503)
    if (any(not isinstance(data.get(k, 0), int) or data.get(k, 0) < 0
            for k in ('generation', 'op_version'))
            or not isinstance(data.get('pending_index', []), list)
            or not isinstance(data.get('deleted_labels', []), list)
            or (data.get('publishing') is not None
                and _coerce_publishing(data['publishing']) is None)):
        raise PersonalMemoryError("记忆恢复状态已损坏", code="scope_unavailable", status=503)
    state = _default_scope_state()
    state["generation"] = _coerce_int(data.get("generation"))
    state["op_version"] = _coerce_int(data.get("op_version"))
    state["cleared_at"] = data.get("cleared_at")
    pending = data.get("pending_index")
    state["pending_index"] = ([str(x) for x in pending]
                              if isinstance(pending, list) else [])
    state["publishing"] = _coerce_publishing(data.get("publishing"))
    state['deleted_labels'] = [str(label) for label in data.get('deleted_labels', [])]
    return state


def _write_scope_state(root: Path, state: Dict[str, Any]) -> None:
    payload = dict(state)
    payload.pop("incomplete", None)
    safe_fs.write_text_atomic(
        Path(root), _SCOPE_FILE,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _scope_update(root: Path, mutate: Callable[[Dict[str, Any]], Any]) -> Any:
    """Read-modify-write the scope marker under :data:`_scope_lock`."""
    with scope_transaction(root), _scope_lock:
        state = read_scope_state(root)
        result = mutate(state)
        _write_scope_state(root, state)
        return result


def _stale_publish(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The publish intent left behind by another (dead) process, if any."""
    publishing = state.get("publishing")
    if not isinstance(publishing, dict):
        return None
    if _coerce_int(publishing.get("pid")) == os.getpid():
        # This process is still inside the operation that recorded it.
        return None
    return publishing


def read_scope_generation(identity=None) -> int:
    """The clear generation for ``identity``'s personal memory (0 when unset)."""
    from common import state_dir
    try:
        root = Path(state_dir.user_root(identity))
    except Exception:
        return 0
    return _coerce_int(read_scope_state(root).get("generation"))


def read_scope_token(identity=None) -> Dict[str, int]:
    """``{"generation": n, "op_version": n}`` for a publisher to carry.

    A consolidation task that captures this pair can refuse to publish when
    either has moved on: the clear generation catches "the user cleared", and
    the operation version catches "another writer committed meanwhile".
    """
    from common import state_dir
    try:
        root = Path(state_dir.user_root(identity))
    except Exception:
        return {"generation": 0, "op_version": 0}
    state = read_scope_state(root)
    return {"generation": _coerce_int(state.get("generation")),
            "op_version": _coerce_int(state.get("op_version"))}


def scope_publish_is_current(identity=None, token: Optional[Dict[str, int]] = None
                             ) -> bool:
    """True when ``token`` still matches the persisted scope state."""
    if not token:
        return True
    return read_scope_token(identity) == {
        "generation": _coerce_int(token.get("generation")),
        "op_version": _coerce_int(token.get("op_version")),
    }


def pending_index_labels_for_root(root) -> Set[str]:
    """The masking set of one explicit storage root.

    The personal domain resolves its root from the identity; an Agent workspace
    is not identity-addressed, so the Agent writer records its tombstones in
    *its own* workspace scope file and retrieval has to look there. Both
    domains must answer this question with the same rule, or one of them would
    serve content the other considers deleted.
    """
    state = read_scope_state(Path(root))
    labels = set(str(x) for x in state.get("pending_index") or [])
    stale = _stale_publish(state)
    if stale:
        labels.update(stale["labels"])
    return labels


def pending_index_labels(identity=None) -> Set[str]:
    """Labels whose index contents may not be trusted yet.

    The retrieval entry point subtracts these from its results, so a failed
    purge degrades to "not found" rather than "deleted body still returned" --
    and so does a publish that was interrupted before it could be confirmed.
    """
    from common import state_dir
    try:
        root = Path(state_dir.user_root(identity))
    except Exception:
        return set()
    return pending_index_labels_for_root(root)


def scope_incomplete(identity=None) -> bool:
    """True when a previous process left a publish unreconciled."""
    from common import state_dir
    try:
        root = Path(state_dir.user_root(identity))
    except Exception:
        return False
    return _stale_publish(read_scope_state(root)) is not None


def _recover_file_publish(root: Path) -> None:
    raw = safe_fs.read_text(root, '.memory-publish.json')
    if raw is None:
        return
    try:
        journal = json.loads(raw)
        bodies = journal['bodies']
        if not isinstance(bodies, dict) or not all(
                _ENTRY_ID_RE.fullmatch(e) and isinstance(b, str) for e, b in bodies.items()):
            raise ValueError('invalid publish journal')
        if journal['token'] == read_scope_state(root)['op_version']:
            for entry, body in bodies.items():
                safe_fs.write_text_atomic(root, entry, body)
        safe_fs.unlink(root, '.memory-publish.json')
    except (KeyError, TypeError, ValueError) as e:
        raise PersonalMemoryError('记忆发布记录不可恢复', code='scope_unavailable', status=503) from e


def recover_incomplete_publish(root: Path) -> bool:
    """Promote an interrupted publish into the pending journal.

    Called by the service before it serves or mutates a scope. The interrupted
    labels stay masked from retrieval and are reported as pending, so a restart
    never turns "the index publish may not have completed" into "everything is
    consistent". Idempotent.
    """
    root = Path(root)

    def _mutate(state: Dict[str, Any]) -> bool:
        stale = _stale_publish(state)
        if stale is None:
            return False
        merged = list(dict.fromkeys(
            list(state.get("pending_index") or []) + list(stale["labels"])))
        state["pending_index"] = merged
        state["publishing"] = None
        return True

    with scope_transaction(root):
        _recover_file_publish(root)
        return bool(_scope_update(root, _mutate))


def _record_pending(root: Path, labels: Iterable[str]) -> None:
    labels = list(labels)
    if not labels:
        return

    def _mutate(state: Dict[str, Any]) -> None:
        state["pending_index"] = list(dict.fromkeys(
            list(state.get("pending_index") or []) + labels))

    _scope_update(root, _mutate)


def _clear_pending(root: Path, done: Iterable[str]) -> None:
    done = set(done)
    if not done:
        return

    def _mutate(state: Dict[str, Any]) -> None:
        state["pending_index"] = [x for x in state.get("pending_index") or []
                                  if x not in done]

    _scope_update(root, _mutate)


# --- index plumbing ---------------------------------------------------------


def _purge_label(db_path, label: str) -> None:
    """Remove one label's rows from one index database.

    Module-level so a test (or an operator tool) can substitute a failing
    implementation and prove the caller degrades correctly. Raises on failure.
    """
    from agent.memory.storage import MemoryStorage
    if not Path(db_path).exists():
        return
    storage = MemoryStorage(Path(db_path))
    try:
        if label.endswith('/*'):
            rows = storage.conn.execute("SELECT path FROM files WHERE source='memory' "
                                        "UNION SELECT path FROM chunks WHERE source='memory'").fetchall()
            for row in rows:
                path = row['path']
                if path.startswith(label[:-1]):
                    storage.delete_by_path(path)
        else:
            storage.delete_by_path(label)
    finally:
        storage.close()


def _index_label(db_path, label: str, text: str, user_id: str,
                 scope: str = "user") -> None:
    """Replace one label's rows with ``text``'s current chunks.

    Embeddings are deliberately not synthesised here: this process does not own
    the Agent's embedding provider, and writing an unverified vector is worse
    than writing none. The file metadata row is therefore left absent, so the
    Agent's own next ``sync()`` re-reads the file and adds vectors.

    ``scope`` must match what ``MemoryManager.sync`` records for the same file,
    or the published rows land in a retrieval scope that will never see them:
    personal entries are ``user``, an Agent workspace's own files are
    ``shared``.
    """
    from agent.memory.chunker import TextChunker
    from agent.memory.storage import MemoryStorage, MemoryChunk

    path = Path(db_path)
    # The first edit on a fresh install arrives before that Agent has ever
    # synced, so the index database (and its directory) may not exist yet.
    path.parent.mkdir(parents=True, exist_ok=True)
    storage = MemoryStorage(path)
    try:
        storage.delete_by_path(label)
        chunks = TextChunker().chunk_markdown(text)
        if not chunks:
            return
        batch = []
        for chunk in chunks:
            chunk_id = hashlib.md5(
                f"{label}:{chunk.start_line}:{chunk.end_line}".encode("utf-8")
            ).hexdigest()
            batch.append(MemoryChunk(
                id=chunk_id,
                user_id=user_id,
                scope=scope,
                source="memory",
                path=label,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                text=chunk.text,
                embedding=None,
                hash=MemoryStorage.compute_hash(chunk.text),
                metadata=None,
            ))
        storage.save_chunks_batch(batch)
    finally:
        storage.close()


class PersonalMemoryService:
    """List/read/edit/delete/clear for one (tenant, user) personal memory."""

    def __init__(self, identity=None, *, index_dbs=None, registry_provider=None):
        self._explicit = identity
        self._index_dbs_override = list(index_dbs) if index_dbs is not None else None
        self._registry_provider = registry_provider

    # -- identity / paths ----------------------------------------------------

    def _identity(self):
        from common.runtime_identity import current_identity
        return self._explicit if self._explicit is not None else current_identity()

    def _require_scope(self):
        """The trusted (tenant, user) this service may act for."""
        ident = self._identity()
        user_id = getattr(ident, "user_id", None)
        tenant_id = getattr(ident, "tenant_id", None)
        if not user_id or not tenant_id:
            # No verified tenant+user means there is no personal memory to
            # address. Guessing "the default user" here is exactly the bug the
            # scope rule exists to prevent.
            raise PersonalMemoryError(
                "本人记忆需要已验证的租户与用户身份",
                code="no_identity", status=403)
        if not re.fullmatch(r"[A-Za-z0-9._-]+", str(user_id)):
            raise PersonalMemoryError("无效的用户标识", code="no_identity", status=403)
        return ident

    def user_root(self) -> Path:
        from common import state_dir
        self._require_scope()
        root = Path(state_dir.user_root(self._identity()))
        # A symlinked user root would make every containment promise in
        # ``safe_fs`` meaningless: the caller would be writing "inside" a
        # directory that actually points somewhere else.
        if os.path.islink(root):
            raise PersonalMemoryError(
                "本人记忆根目录被替换", code="unsafe_path", status=403)
        return root

    def _entry_id_pattern(self):
        """The addressable-id grammar of this domain.

        An override point rather than a module constant because the two domains
        really do address different sets: personal memory is exactly
        ``MEMORY.md`` and ``memory/*.md`` under the user's own root, while an
        Agent workspace also owns ``memory/dreams/`` and ``memory/evolution/``.
        """
        return _ENTRY_ID_RE

    def _entry_relative(self, entry_id: str) -> str:
        """Validate the addressable id and return the path relative to the root.

        The grammar and the anchored access are deliberately separate: this
        only decides *which* entry is named, ``safe_fs`` decides whether the
        name still resolves to a plain file inside the caller's own root.
        """
        self._require_scope()
        if not isinstance(entry_id, str) or not self._entry_id_pattern().fullmatch(entry_id):
            raise PersonalMemoryError(
                "无效的记忆标识", code="invalid_entry", status=400)
        return entry_id if entry_id != MAIN_ENTRY_ID else MAIN_ENTRY_ID

    def _target(self, entry_id: str) -> Path:
        """Absolute path of an entry after full anchored validation.

        Kept for callers that need to name the file (logging, display); every
        read/write/delete in this module goes through ``common.safe_fs`` so the
        validated directory is the directory that is used.
        """
        relative = self._entry_relative(entry_id)
        try:
            return Path(safe_fs.resolve_within(self.user_root(), relative))
        except UnsafePathError as e:
            raise PersonalMemoryError(
                "本人记忆路径被替换，已拒绝访问",
                code="unsafe_path", status=403) from e

    def _read_entry(self, entry_id: str) -> Optional[str]:
        relative = self._entry_relative(entry_id)
        try:
            return safe_fs.read_text(self.user_root(), relative)
        except UnsafePathError as e:
            raise PersonalMemoryError(
                "本人记忆路径被替换，已拒绝访问",
                code="unsafe_path", status=403) from e

    def _remove_entry(self, entry_id: str) -> bool:
        relative = self._entry_relative(entry_id)
        try:
            return safe_fs.unlink(self.user_root(), relative)
        except UnsafePathError as e:
            raise PersonalMemoryError(
                "本人记忆路径被替换，已拒绝访问",
                code="unsafe_path", status=403) from e

    def label_for(self, entry_id: str) -> str:
        """The index label ``MemoryManager.sync`` uses for this entry."""
        ident = self._require_scope()
        self._entry_relative(entry_id)
        if entry_id == MAIN_ENTRY_ID:
            return f"memory/users/{ident.user_id}/{MAIN_ENTRY_ID}"
        return f"memory/users/{ident.user_id}/{entry_id[len('memory/'):]}"

    # -- listing / reading ---------------------------------------------------

    def list_entries(self, category="memory") -> List[Dict[str, Any]]:
        with scope_transaction(self.user_root()):
            try:
                return self._list_entries_locked(category)
            except UnsafePathError as e:
                raise PersonalMemoryError('记忆目录被替换', code='unsafe_path', status=403) from e

    def _list_entries_locked(self, category):
        self._require_scope()
        root = self.user_root()
        recover_incomplete_publish(root)
        entries: List[Dict[str, Any]] = []
        if category not in ('memory', 'evolution', 'dream', 'all'):
            raise PersonalMemoryError("未知记忆分类", code="unknown_category")
        if category in ('memory', 'all') and safe_fs.is_file(root, MAIN_ENTRY_ID):
            entries.append(self._entry_info(MAIN_ENTRY_ID))
        directories = {'memory': ['memory'], 'dream': ['memory/dreams'],
                       'evolution': ['memory/evolution', 'memory/dreams'],
                       'all': ['memory', 'memory/evolution', 'memory/dreams']}[category]
        for directory in directories:
            names = safe_fs.list_names(root, directory, suffix='.md', skip_dotfiles=True)
            for name in sorted(names, reverse=True):
                entry_id = f'{directory}/{name}'
                if self._entry_id_pattern().fullmatch(entry_id) and safe_fs.is_file(root, entry_id):
                    entries.append(self._entry_info(entry_id))
        if category in ('evolution', 'dream'):
            entries.sort(key=lambda e: (e['id'].rsplit('/', 1)[-1], e['id']), reverse=True)
        return entries

    def _entry_info(self, entry_id: str) -> Dict[str, Any]:
        root = self.user_root()
        text = self._read_entry(entry_id)
        if text is None:
            text = ""
        info = safe_fs.stat(root, self._entry_relative(entry_id))
        return {
            "id": entry_id,
            "size": len(text.encode("utf-8")),
            "updated_at": datetime.fromtimestamp(
                info.st_mtime).strftime("%Y-%m-%d %H:%M:%S") if info else "",
            "revision": _revision_of(text),
            "type": self.entry_type(entry_id),
            # The verbs the console may offer for this row (task 8.1). Editing
            # and deleting are the same operation set the write paths enforce
            # with a revision check, so the page never invents a verb the API
            # would refuse; ``create`` is not a memory concept here (the entries
            # are files under the member's own root).
            "actions": {"edit": self.write_enabled(), "delete": True},
        }

    @staticmethod
    def entry_type(entry_id):
        if entry_id == MAIN_ENTRY_ID:
            return 'global'
        if entry_id.startswith('memory/evolution/'):
            return 'evolution'
        if entry_id.startswith('memory/dreams/'):
            return 'dream'
        return 'daily'

    def write_enabled(self):
        try:
            self._require_write_capability()
            return True
        except PersonalMemoryError:
            return False

    def read(self, entry_id: str) -> Dict[str, Any]:
        with scope_transaction(self.user_root()):
            return self._read_locked(entry_id)

    def _read_locked(self, entry_id):
        self._require_scope()
        recover_incomplete_publish(self.user_root())
        text = self._read_entry(entry_id)
        if text is None:
            # A valid id that has no file is "empty", not an error: the console
            # shows an empty personal memory on a fresh account, and the same
            # answer must not distinguish "absent" from "another user's".
            return {"id": entry_id, "content": "", "revision": None}
        return {"id": entry_id, "content": text, "revision": _revision_of(text)}

    # -- writing -------------------------------------------------------------

    @staticmethod
    def _require_write_capability() -> None:
        """Refuse *adding* memory when ``personal_memory_write`` is withdrawn.

        Task 9.1. Only the adding path is gated: reading, deleting and clearing
        stay reachable, so a deployment that withdraws the capability cannot
        strand a member with personal memory they may no longer retract. An
        unevaluable switch is a closed one — nothing here may fail open.
        """
        try:
            from auth.policy import personal_capability_enabled
        except Exception:  # noqa: BLE001 - fail closed
            enabled = False
        else:
            enabled = personal_capability_enabled("personal_memory_write")
        if not enabled:
            raise PersonalMemoryError(
                "本人记忆写入尚未在本部署启用", code="capability_disabled", status=403)

    def save(self, entry_id: str, content, expected_revision: Optional[str] = None
             ) -> Dict[str, Any]:
        self._require_write_capability()
        if not isinstance(content, str):
            raise PersonalMemoryError(
                "记忆内容必须是文本", code="invalid_content", status=400)
        relative = self._entry_relative(entry_id)
        root = self.user_root()
        try:
            # The whole mutation -- version check, body commit and index
            # publish -- is one critical section. Releasing the lock between the
            # two is what allowed "body saved -> clear removed it and purged the
            # index -> the original save wrote the old index back".
            with scope_transaction(root):
                recover_incomplete_publish(root)
                current = self._read_entry(entry_id)
                current_revision = (_revision_of(current)
                                    if current is not None else None)
                if current_revision is not None:
                    if not expected_revision:
                        raise PersonalMemoryError(
                            "该条目已存在，保存需要当前版本",
                            code="revision_required", status=409)
                    if expected_revision != current_revision:
                        raise PersonalMemoryError(
                            "记忆已被其他页面修改，请刷新后重试",
                            code="stale_revision", status=409)
                elif expected_revision:
                    # A caller that believes it is editing something that is
                    # gone must not silently create it.
                    raise PersonalMemoryError(
                        "记忆条目已不存在，请刷新后重试",
                        code="stale_revision", status=409)

                token = self._begin_mutation("save", [self.label_for(entry_id)])
                try:
                    safe_fs.write_text_atomic(root, relative, content)
                except UnsafePathError as e:
                    self._abort_mutation(token)
                    raise PersonalMemoryError(
                        "本人记忆路径被替换，已拒绝写入",
                        code="unsafe_path", status=403) from e
                except BaseException:
                    self._abort_mutation(token)
                    raise
                index_state = self._after_write(entry_id, content, token)
                self._finish_mutation(token, [self.label_for(entry_id)],
                                      index_state)
        except UnsafePathError as e:
            raise PersonalMemoryError(
                "本人记忆路径被替换，已拒绝访问",
                code="unsafe_path", status=403) from e
        revision = _revision_of(content)
        return {"id": entry_id, "revision": revision,
                "size": len(content.encode("utf-8")),
                "index_state": index_state}

    def delete(self, entry_id: str, expected_revision: Optional[str] = None
               ) -> Dict[str, Any]:
        self._require_scope()
        root = self.user_root()
        label = self.label_for(entry_id)
        with scope_transaction(root):
            recover_incomplete_publish(root)
            current = self._read_entry(entry_id)
            if current is None:
                raise PersonalMemoryError("记忆条目不存在", code="not_found",
                                          status=404)
            if expected_revision and expected_revision != _revision_of(current):
                raise PersonalMemoryError(
                    "记忆已被其他页面修改，请刷新后重试",
                    code="stale_revision", status=409)
            token = self._begin_mutation("delete", [label])
            try:
                self._remove_entry(entry_id)
            except BaseException:
                self._abort_mutation(token)
                raise
            index_state = self._after_remove([label], token)
            self._finish_mutation(token, [label], index_state)
        return {"id": entry_id, "index_state": index_state}

    def clear(self, expected_revision: Optional[str] = None,
              clear_scope="memory") -> Dict[str, Any]:
        if clear_scope not in ('memory', 'all_personal'):
            raise PersonalMemoryError("无效的清空范围", code="invalid_scope")
        with scope_transaction(self.user_root()):
            return self._clear_locked(expected_revision, clear_scope)

    def _clear_locked(self, expected_revision: Optional[str], clear_scope="memory") -> Dict[str, Any]:
        root = self.user_root()
        recover_incomplete_publish(root)
        entries = self.list_entries('all') if clear_scope == 'all_personal' else self.list_entries()
        if expected_revision is not None:
            combined = self._collection_revision(entries)
            if expected_revision != combined:
                raise PersonalMemoryError(
                    "记忆已被其他页面修改，请刷新后重试",
                    code="stale_revision", status=409)

        labels = [self.label_for(entry["id"]) for entry in entries]
        if clear_scope == 'all_personal':
            # Include index-only legacy rows, even when no file remains to list.
            labels.append(f'memory/users/{self._require_scope().user_id}/*')
        # Order matters: the generation and the publish intent are recorded
        # *before* anything is removed. A queued consolidation task that reads
        # the version after this point sees a stale value and refuses; one that
        # already passed the check and is mid-write is caught by the pending
        # filter, because its content is gone and its label stays masked.
        token = self._begin_mutation("clear", labels, bump_generation=True)

        for entry in entries:
            try:
                self._remove_entry(entry["id"])
            except OSError as e:
                logger.warning("[PersonalMemory] clear unlink failed %s: %s",
                               entry["id"], e)
                self._abort_mutation(token, labels)
                raise PersonalMemoryError(
                    "清空未完成，请重试", code="clear_incomplete", status=500)
        # Only the selected category is removed. The explicit all_personal
        # scope includes diaries/logs; backups, persona and chat stay outside it.

        index_state = self._after_remove(labels, token)
        self._finish_mutation(token, labels, index_state)
        return {"status": "success" if index_state == "ok" else "incomplete",
                "index_state": index_state,
                "removed": len(entries),
                "generation": self.scope_generation()}

    def _collection_revision(self, entries: List[Dict[str, Any]]) -> str:
        payload = "\n".join(f"{e['id']}:{e['revision']}" for e in sorted(
            entries, key=lambda x: x["id"]))
        return _revision_of(payload)

    def publish(self, changes, *, expected_scope, append=False, deduplicate=False):
        """Publish a generated batch against the scope captured before work.

        A persisted body journal makes a main-memory + diary update recoverable.
        The same lock/version protocol governs human edits and index publishing.
        """
        self._require_write_capability()
        root = self.user_root()
        with scope_transaction(root):
            recover_incomplete_publish(root)
            if expected_scope != self.scope_token():
                raise PersonalMemoryError("记忆已更新，请基于最新内容重试",
                                          code='stale_revision', status=409)
            bodies = {}
            for entry, content in changes.items():
                self._entry_relative(entry)
                if not isinstance(content, str):
                    raise PersonalMemoryError('记忆内容必须是文本', code='invalid_content')
                current = self._read_entry(entry)
                if deduplicate and current is not None and (
                        current == content or ('\n\n' + content.strip() + '\n\n')
                        in ('\n\n' + current.strip() + '\n\n')):
                    continue
                bodies[entry] = ((current.rstrip() + '\n\n' + content.lstrip())
                                 if append and current else content)
            if not bodies:
                return {'index_state': 'pending' if self.scope_status()['pending'] else 'ok',
                        'ids': list(changes), 'unchanged': True}
            labels = [self.label_for(entry) for entry in bodies]
            token = self._begin_mutation('publish', labels)
            try:
                safe_fs.write_text_atomic(root, '.memory-publish.json', json.dumps(
                    {'token': token, 'bodies': bodies}, ensure_ascii=False))
                _recover_file_publish(root)
            except Exception:
                self._abort_mutation(token, labels)
                raise
            states = [self._after_write(entry, body, token) for entry, body in bodies.items()]
            state = 'ok' if all(s == 'ok' for s in states) else 'pending'
            self._finish_mutation(token, labels, state)
            return {'index_state': state, 'ids': list(changes),
                    'revisions': {e: _revision_of(b) for e, b in bodies.items()}}

    def add(self, content, entry_id=None, *, expected_scope=None):
        """Idempotent explicit remember, sharing the generated publish path."""
        if not isinstance(content, str) or not content.strip():
            raise PersonalMemoryError('记忆内容不能为空', code='invalid_content')
        content = content.strip()
        entry_id = entry_id or f'memory/note-{_revision_of(content)[:24]}.md'
        with scope_transaction(self.user_root()):
            result = self.publish({entry_id: content},
                                  expected_scope=expected_scope or self.scope_token(),
                                  append=True, deduplicate=True)
            return {'id': entry_id, **result}

    # -- scope generation / operation version --------------------------------

    def scope_generation(self) -> int:
        self._require_scope()
        return _coerce_int(read_scope_state(self.user_root()).get("generation"))

    def scope_token(self) -> Dict[str, int]:
        """The version pair a publisher must still match when it commits."""
        self._require_scope()
        state = read_scope_state(self.user_root())
        return {"generation": _coerce_int(state.get("generation")),
                "op_version": _coerce_int(state.get("op_version"))}

    def _begin_mutation(self, kind: str, labels: List[str], *,
                        bump_generation: bool = False) -> int:
        """Record the intent to publish and return its operation token.

        Must be called with :data:`_entry_lock` held. The token is the value of
        ``op_version`` this operation commits at; any publisher that finds a
        different value has been overtaken and must not write.
        """
        root = self.user_root()

        def _mutate(state: Dict[str, Any]) -> int:
            state["op_version"] = _coerce_int(state.get("op_version")) + 1
            if kind in ('delete', 'clear'):
                state['deleted_labels'] = list(dict.fromkeys(
                    state.get('deleted_labels', []) + labels))
            if bump_generation:
                state["generation"] = _coerce_int(state.get("generation")) + 1
                state["cleared_at"] = _now()
            state["publishing"] = {
                "pid": os.getpid(),
                "token": state["op_version"],
                "kind": kind,
                "labels": list(labels),
                "at": _now(),
            }
            return state["op_version"]

        return int(_scope_update(root, _mutate))

    def _abort_mutation(self, token: int, labels: Optional[List[str]] = None
                        ) -> None:
        """Release the publish intent after a failure, keeping labels masked."""
        root = self.user_root()

        def _mutate(state: Dict[str, Any]) -> None:
            publishing = state.get("publishing")
            if (isinstance(publishing, dict)
                    and _coerce_int(publishing.get("token")) == token):
                state["publishing"] = None
                labels_ = publishing.get("labels") or []
            else:
                labels_ = labels or []
            if labels_:
                state["pending_index"] = list(dict.fromkeys(
                    list(state.get("pending_index") or []) + list(labels_)))

        try:
            _scope_update(root, _mutate)
        except Exception as e:  # noqa: BLE001 - abort must not mask the cause
            logger.warning("[PersonalMemory] abort bookkeeping failed: %s", e)

    def _finish_mutation(self, token: int, labels: List[str],
                         index_state: str) -> None:
        root = self.user_root()
        kind = (read_scope_state(root).get('publishing') or {}).get('kind', 'update')

        def _mutate(state: Dict[str, Any]) -> None:
            publishing = state.get("publishing")
            if (isinstance(publishing, dict)
                    and _coerce_int(publishing.get("token")) == token):
                state["publishing"] = None
            if index_state == "ok":
                done = set(labels)
                state["pending_index"] = [x for x in state.get("pending_index") or []
                                          if x not in done]
            else:
                state["pending_index"] = list(dict.fromkeys(
                    list(state.get("pending_index") or []) + list(labels)))

        _scope_update(root, _mutate)
        if self._chunk_identity()[1] == 'user':
            # Never include body text, source paths, or request payloads. Audit
            # failure after a file commit must not invite a blind write retry.
            try:
                from auth.service import get_identity_service
                ident = self._require_scope()
                get_identity_service().record_business_audit(
                    actor_user_id=ident.user_id, tenant_id=ident.tenant_id,
                    action='personal_memory.' + kind, target='personal-memory',
                    redacted_changes={'operation_version': token, 'entry_count': len(labels),
                                      'index_state': index_state},
                    result='success' if index_state == 'ok' else 'pending')
            except Exception:
                logger.warning('[PersonalMemory] committed operation audit unavailable')

    # -- index maintenance ---------------------------------------------------

    def _index_dbs(self) -> List[Path]:
        """Every index database that may hold a copy of this user's memory.

        Each Agent keeps its own index, which is what makes personal memory
        visible from every Agent the user may use — and therefore what makes a
        purge in one Agent's database insufficient.
        """
        self._index_discovery_failed = False
        if self._index_dbs_override is not None:
            return [Path(p) for p in self._index_dbs_override]
        db_paths: List[Path] = []

        def _add(workspace) -> None:
            if not workspace:
                return
            path = Path(workspace) / "memory" / "long-term" / "index.db"
            if path not in db_paths:
                db_paths.append(path)

        try:
            provider = self._registry_provider
            if provider is None:
                from agent.registry import get_agent_registry
                provider = get_agent_registry()
            from auth.service import get_identity_service
            bindings = get_identity_service().list_agent_bindings(
                self._require_scope().tenant_id)
            allowed = {b['agent_id'] for b in bindings}
            for profile in provider.list(include_disabled=True):
                if profile.id in allowed:
                    _add(getattr(profile, "workspace", None))
        except Exception as e:
            self._index_discovery_failed = True
            logger.debug("[PersonalMemory] agent index scan failed: %s", e)
        for path in getattr(self, '_extra_index_dbs', []):
            if path not in db_paths:
                db_paths.append(path)
        return db_paths

    def _publish_is_current(self, token: int) -> bool:
        """True when the operation that produced ``token`` is still the latest.

        A publisher that has been overtaken (a clear, or another writer's
        commit) must not write rows back: doing so would restore content the
        user just removed. The check runs before *every* index write, so a
        partially applied publish stops at the first database rather than
        spreading stale rows.
        """
        state = read_scope_state(self.user_root())
        return _coerce_int(state.get("op_version")) == token

    def _chunk_identity(self):
        """``(user_id, scope)`` the index rows of this domain carry.

        Personal memory is user-scoped. The Agent domain overrides this with
        ``(None, "shared")`` so its rows match what ``MemoryManager.sync``
        records for the same files — a mismatch does not fail loudly, it just
        publishes rows into a retrieval scope that never sees them.
        """
        return str(self._require_scope().user_id), "user"

    def _after_write(self, entry_id: str, content: str, token: int) -> str:
        """Refresh the edited entry in every known index; report the outcome."""
        label = self.label_for(entry_id)
        if self.entry_type(entry_id) in ('dream', 'evolution'):
            return self._after_remove([label], token)
        user_id, scope = self._chunk_identity()
        failures = []
        databases = self._index_dbs()
        if getattr(self, '_index_discovery_failed', False):
            failures.append(label)
        for db in databases:
            if not self._publish_is_current(token):
                # Defensive token check in addition to the process lock: an
                # obsolete operation cannot publish stale rows or claim success.
                _record_pending(self.user_root(), [label])
                return "obsolete"
            try:
                _index_label(db, label, content, user_id, scope)
            except Exception as e:
                logger.warning("[PersonalMemory] index refresh failed %s %s: %s",
                               db, label, e)
                failures.append(label)
        if failures:
            # The *content* is saved; the index is behind. The next sync would
            # repair it anyway, but recording it keeps the state honest and
            # makes it visible to the caller.
            _record_pending(self.user_root(), failures)
            return "pending"
        return "ok"

    def _after_remove(self, labels: List[str], token: int) -> str:
        """Purge labels from every known index; report the outcome."""
        failures: List[str] = []
        databases = self._index_dbs()
        if getattr(self, '_index_discovery_failed', False):
            failures.extend(labels)
        for db in databases:
            if not self._publish_is_current(token):
                _record_pending(self.user_root(), labels)
                return "obsolete"
            for label in labels:
                try:
                    _purge_label(db, label)
                except Exception as e:
                    logger.warning("[PersonalMemory] index purge failed %s %s: %s",
                                   db, label, e)
                    failures.append(label)
        if failures:
            # Fail closed: the rows may survive, so the labels stay in the
            # pending journal and the retrieval entry point keeps hiding them
            # until a retry succeeds. Reporting "ok" here would claim a
            # consistency the store does not have.
            _record_pending(self.user_root(), failures)
            return "pending"
        return "ok"

    def scope_status(self) -> Dict[str, Any]:
        """Recovery-visible scope state for the console (task 5.7)."""
        self._require_scope()
        root = self.user_root()
        recovered = recover_incomplete_publish(root)
        state = read_scope_state(root)
        return {
            "generation": _coerce_int(state.get("generation")),
            "op_version": _coerce_int(state.get("op_version")),
            "pending": list(state.get("pending_index") or []),
            "recovered_incomplete_publish": recovered,
        }

    def retry_pending_index(self) -> Dict[str, Any]:
        """Reconcile current files (or deletion), never just discard saved rows."""
        with scope_transaction(self.user_root()):
            return self._retry_pending_index_locked()

    def _retry_pending_index_locked(self):
        self._require_scope()
        root = self.user_root()
        recover_incomplete_publish(root)
        pending = list(read_scope_state(root).get("pending_index") or [])
        if not pending:
            return {"pending": [], "index_state": "ok"}
        done: List[str] = []
        still: List[str] = []
        entries = {self.label_for(e['id']): e['id'] for e in self.list_entries('all')}
        user_id, scope = self._chunk_identity()
        for label in pending:
            databases = self._index_dbs()
            ok = not getattr(self, '_index_discovery_failed', False)
            for db in databases:
                try:
                    entry = entries.get(label)
                    if label.endswith('/*'):
                        _purge_label(db, label)
                        # A pending clear may be followed by legitimate new
                        # writes. Rebuild their current bodies in the same lock.
                        for current_label, current_entry in entries.items():
                            if self.entry_type(current_entry) not in ('dream', 'evolution'):
                                _index_label(db, current_label, self._read_entry(current_entry), user_id, scope)
                    elif entry and self.entry_type(entry) not in ('dream', 'evolution'):
                        _index_label(db, label, self._read_entry(entry), user_id, scope)
                    else:
                        _purge_label(db, label)
                except Exception as e:
                    logger.warning("[PersonalMemory] retry purge failed %s %s: %s",
                                   db, label, e)
                    ok = False
            (done if ok else still).append(label)
        if still:
            return {"pending": still, "index_state": "pending"}
        _clear_pending(root, done)
        return {"pending": [], "index_state": "ok"}
