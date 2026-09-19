"""Domain service for personal todos.

This is the single entry point both the Web API and the minimal ``todo`` agent
tool call into. It owns:

* validation of the minimum field set (title 1..120, description <= 8000,
  note <= 2000, known kind / priority, explicit timezone on due_at),
* the four-state state machine and the rule that terminal items must be
  reopened before editing,
* the overdue derivation (active + due_at before server now),
* persistent create-dedup (``create_key`` + immutable initial payload hash),
* version-casual updates combined with an append-only event in one transaction,
* authorization: every call resolves ``scope``/``owner`` from a trusted request
  context (passed in by the caller) and enforces ``todo.read`` / ``todo.write``
  plus the ownership guard. It never accepts a client-supplied owner/tenant.

The module intentionally does not know about HTTP or web.py; the routes (and the
tool wrapper) translate errors below into HTTP semantics / tool output. Errors
carry a stable ``code`` and an ``http_status`` so the translation layer stays
thin.
"""

from __future__ import annotations

import datetime
import os
import re
from typing import Any, Dict, Optional, Tuple

from agent.todo.store import (
    KIND_VALUES,
    NOTE_MAX,
    PRIORITY_VALUES,
    SOURCE_VALUES,
    STATUS_VALUES,
    TITLE_MAX,
    TITLE_MIN,
    DESCRIPTION_MAX,
    TodoConflict,
    TodoFieldError,
    TodoNotFound,
    TodoStore,
    TodoStoreError,
    compute_create_key,
    hash_create_payload,
    is_active_status,
)

try:
    from zoneinfo import ZoneInfo
    _HAS_ZONEINFO = True
except ImportError:  # pragma: no cover - Python < 3.9
    _HAS_ZONEINFO = False


# --------------------------------------------------------------------------- #
# Identity helpers (maps an auth credential to a stable owner/scope)
# --------------------------------------------------------------------------- #

LEGACY_OWNER = "local-owner"
LEGACY_SCOPE = "default"


def reassign_empty_owner_todos(
    *,
    scope_id: str,
    owner_id: str,
    app_data_root: Optional[str] = None,
) -> int:
    """Reassign rows with empty/legacy owner to ``owner_id`` (idempotent).

    Used by first-run bootstrap so historical empty-owner todos land under the
    initial platform admin instead of remaining unreachable in database mode.
    """
    from agent.todo.store import TodoStore

    path = memory_db_path(scope_id, LEGACY_OWNER, app_data_root=app_data_root)
    if not os.path.isfile(path):
        # Also try the default-scope legacy path under app_data_root.
        path = memory_db_path(LEGACY_SCOPE, LEGACY_OWNER, app_data_root=app_data_root)
    if not os.path.isfile(path):
        return 0
    store = TodoStore(path)
    updated = 0
    con = store._connect()  # noqa: SLF001 — migration helper
    try:
        # The handler moves with the owner, but only when it still points at the
        # legacy value: after ``assignee_id`` exists a row could in principle be
        # delegated, and that decision must survive this bootstrap.
        cur = con.execute(
            "UPDATE todo_items SET owner_id=?, scope_id=?,"
            " assignee_id = CASE WHEN assignee_id IN ('', ?) OR assignee_id IS NULL"
            "                    THEN ? ELSE assignee_id END"
            " WHERE owner_id IN ('', ?) OR owner_id IS NULL",
            (owner_id, scope_id, LEGACY_OWNER, owner_id, LEGACY_OWNER),
        )
        updated += cur.rowcount or 0
        con.execute(
            "UPDATE todo_events SET owner_id=?, scope_id=?"
            " WHERE owner_id IN ('', ?) OR owner_id IS NULL",
            (owner_id, scope_id, LEGACY_OWNER),
        )
        con.commit()
    finally:
        con.close()
    return updated


class TodoServiceError(Exception):
    """Base error, carrying a stable code and an HTTP-ish status."""

    def __init__(self, message: str, code: str = "todo_error", http_status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status


class TodoDisabled(TodoServiceError):
    def __init__(self, message: str = "todo 功能未开启", code: str = "todo_disabled", http_status: int = 404):
        super().__init__(message, code=code, http_status=http_status)


class TodoUnauthorized(TodoServiceError):
    def __init__(self, message: str = "未认证或身份不可用", code: str = "unauthorized", http_status: int = 401):
        super().__init__(message, code=code, http_status=http_status)


class TodoPermissionDenied(TodoServiceError):
    def __init__(self, message: str = "无待办权限", code: str = "forbidden", http_status: int = 403):
        super().__init__(message, code=code, http_status=http_status)


class TodoNotFoundError(TodoServiceError):
    def __init__(self, message: str = "事项不存在", code: str = "not_found", http_status: int = 404):
        super().__init__(message, code=code, http_status=http_status)


class TodoConflictError(TodoServiceError):
    def __init__(self, message: str = "版本或状态冲突", code: str = "conflict", http_status: int = 409):
        super().__init__(message, code=code, http_status=http_status)


class TodoFieldValidationError(TodoServiceError):
    def __init__(self, message: str, field: str = "", code: str = "invalid_field", http_status: int = 422):
        super().__init__(message, code=code, http_status=http_status)
        self.field = field


class TodoUnavailable(TodoServiceError):
    def __init__(self, message: str = "依赖或存储不可用", code: str = "unavailable", http_status: int = 503):
        super().__init__(message, code=code, http_status=http_status)


# --------------------------------------------------------------------------- #
# Storage path resolution (private todo/todos.db, not the file-serving workspace)
# --------------------------------------------------------------------------- #

def default_enabled() -> bool:
    """Read the single ``todo_enabled`` switch (default off)."""
    try:
        from config import conf
        return bool(conf().get("todo_enabled", False))
    except Exception:
        return False


def resolve_todo_database_path(scope_id: str = "default", owner_id: str = LEGACY_OWNER) -> str:
    """Resolve the private per-scope ``todo/todos.db`` path.

    The DB lives under a *private* data root, never under the file-serving /
    download-able agent workspace. For legacy single-owner this is the instance
    appdata root. For database mode the caller passes the tenant's confirmed app
    data root; this function only appends the ``todo`` segment and returns the
    path (it does not create directories).
    """
    from config import get_data_root
    root = get_data_root()
    return os_path_join(root, "todo", "todos.db")


def os_path_join(*parts: str) -> str:
    import os
    return os.path.join(*parts)


def memory_db_path(scope_id: str = "default", owner_id: str = LEGACY_OWNER, app_data_root: Optional[str] = None) -> str:
    """Resolve the todo database path under a trusted, private app-data root.

    ``app_data_root`` overrides the default data root for database mode where a
    tenant's data root is confirmed by the identity slice. Never falls back to a
    shared global workspace or a file-servable directory.
    """
    import os
    root = app_data_root
    if not root:
        from config import get_data_root
        root = get_data_root()
    return os.path.join(root, "todo", "todos.db")


# --------------------------------------------------------------------------- #
# Request context
# --------------------------------------------------------------------------- #

class TodoActor:
    """A trusted, resolved actor for one todo request.

    ``bound`` is False when no valid identity could be established — the service
    must then refuse (no silent fallback). Actor always carries a real
    tenant/user subject; shared-password / local-owner paths are retired.
    """

    __slots__ = ("bound", "scope_id", "owner_id", "username", "permissions")

    def __init__(
        self,
        *,
        bound: bool,
        scope_id: str = "",
        owner_id: str = "",
        username: str = "",
        permissions: Optional[set] = None,
    ):
        self.bound = bound
        self.scope_id = scope_id or ""
        self.owner_id = owner_id or ""
        self.username = username
        self.permissions = permissions or set()

    def has(self, permission: str) -> bool:
        return permission in self.permissions


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

_KIND_DISPLAY = {
    "general": "普通事项",
    "input_required": "补充资料",
    "confirmation": "方案确认",
    "review": "结果验收",
}
_PRIORITY_DISPLAY = {"low": "低", "normal": "普通", "high": "高"}
_STATUS_DISPLAY = {
    "pending": "待处理",
    "in_progress": "处理中",
    "completed": "已完成",
    "cancelled": "已取消",
}


def _valid_timezone(tz: str) -> bool:
    if not tz:
        return False
    if not _HAS_ZONEINFO:
        return True  # defer to server on old Python
    try:
        ZoneInfo(tz)
        return True
    except Exception:
        return False


def _validate_title(title: Any) -> str:
    if title is None:
        raise TodoFieldValidationError("标题不能为空", field="title")
    if not isinstance(title, str):
        raise TodoFieldValidationError("标题必须是文本", field="title")
    trimmed = title.strip()
    length = len(trimmed)
    if length < TITLE_MIN or length > TITLE_MAX:
        raise TodoFieldValidationError(
            f"标题需要 {TITLE_MIN}～{TITLE_MAX} 个字符", field="title"
        )
    return trimmed


def _validate_description(description: Any) -> str:
    if description is None:
        return ""
    if not isinstance(description, str):
        raise TodoFieldValidationError("说明必须是纯文本", field="description")
    if len(description) > DESCRIPTION_MAX:
        raise TodoFieldValidationError(
            f"说明不能超过 {DESCRIPTION_MAX} 个字符", field="description"
        )
    return description


def _validate_note(note: Any) -> str:
    if note is None:
        return ""
    if not isinstance(note, str):
        raise TodoFieldValidationError("说明必须是纯文本", field="note")
    if len(note) > NOTE_MAX:
        raise TodoFieldValidationError(
            f"处理说明不能超过 {NOTE_MAX} 个字符", field="note"
        )
    return note


def _validate_kind(kind: Any) -> str:
    if kind is None or kind == "":
        return "general"
    if kind not in KIND_VALUES:
        raise TodoFieldValidationError("未知的分类", field="kind")
    return kind


def _validate_priority(priority: Any) -> str:
    if priority is None or priority == "":
        return "normal"
    if priority not in PRIORITY_VALUES:
        raise TodoFieldValidationError("未知的优先级", field="priority")
    return priority


def _validate_source(source: Any) -> str:
    if source is None or source == "":
        return "manual"
    if source not in SOURCE_VALUES:
        raise TodoFieldValidationError("未知的来源类型", field="source")
    return source


def _parse_due(due_at: Any, timezone: Any, validate_tz: bool = True) -> Tuple[Optional[int], str]:
    """Normalize a due_at / timezone pair into (epoch_seconds, iana_tz).

    The API only accepts an explicit timezone. A date-only value is treated as
    end-of-day 23:59:59 in that zone by the frontend before submission; here we
    only require a parseable timestamp alongside a valid IANA zone.
    """
    if due_at is None or due_at == "":
        return None, ""
    tz = (timezone or "").strip()
    if not tz:
        raise TodoFieldValidationError("截止时间必须带明确时区", field="timezone")
    if validate_tz and not _valid_timezone(tz):
        raise TodoFieldValidationError("无效的时区", field="timezone")

    # Accept an ISO-8601 string OR a numeric epoch.
    if isinstance(due_at, (int, float)):
        return int(due_at), tz
    if isinstance(due_at, str):
        raw = due_at.strip()
        if not raw:
            return None, ""
        try:
            dt = datetime.datetime.fromisoformat(raw)
        except ValueError:
            try:
                return int(raw), tz
            except ValueError:
                raise TodoFieldValidationError("无效的截止时间", field="due_at") from None
        if dt.tzinfo is None:
            # Treated as wall time in the given zone.
            if _HAS_ZONEINFO:
                dt = dt.replace(tzinfo=ZoneInfo(tz))
            else:
                raise TodoFieldValidationError("无法解释无时区截止时间", field="due_at")
        return int(dt.timestamp()), tz
    raise TodoFieldValidationError("无效的截止时间", field="due_at")


def _is_overdue(item: Dict[str, Any], now: Optional[int] = None) -> bool:
    if not is_active_status(item.get("status", "pending")):
        return False
    due = item.get("due_at")
    if not due:
        return False
    return int(due) < (now if now is not None else int(datetime.datetime.now().timestamp()))


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #

class TodoService:
    """Per-request service. One instance per request; no shared mutable state."""

    def __init__(
        self,
        actor: TodoActor,
        *,
        enabled_fn=None,
        db_path: Optional[str] = None,
        app_data_root: Optional[str] = None,
    ):
        self.actor = actor
        self._enabled_fn = enabled_fn or default_enabled
        self._app_data_root = app_data_root
        # The DB is only opened once we know the actor is bound and the feature
        # is on; opening it lazily avoids creating files for disabled reads.
        self._store_instance: Optional[TodoStore] = None
        if db_path:
            self._db_path = db_path
        else:
            self._db_path = self._resolve_db_path()

    # -- storage path -------------------------------------------------------- #
    def _resolve_db_path(self) -> str:
        # Caller must supply a confirmed tenant app-data root.
        if not self._app_data_root:
            raise TodoUnavailable("缺少可信租户数据根")
        path = memory_db_path(
            scope_id=self.actor.scope_id,
            owner_id=self.actor.owner_id,
            app_data_root=self._app_data_root,
        )
        from pathlib import Path
        # The tenant root is trusted, but an existing todo directory/database
        # must not redirect SQLite into another tenant or a public workspace.
        if Path(path).parent.is_symlink() or Path(path).is_symlink():
            raise TodoUnavailable("待办存储路径不可用")
        return path

    # -- guards -------------------------------------------------------------- #
    def _require_enabled(self) -> None:
        if not self._enabled_fn():
            raise TodoDisabled()

    def _require_bound(self) -> None:
        if not self.actor.bound:
            raise TodoUnauthorized("未认证或身份不可用")

    def _require_read(self) -> None:
        self._require_enabled()
        self._require_bound()
        if not self.actor.has("todo.read"):
            raise TodoPermissionDenied("无待办读取权限")

    def _require_write(self) -> None:
        self._require_enabled()
        self._require_bound()
        if not self.actor.has("todo.write"):
            raise TodoPermissionDenied("无待办写入权限")

    @property
    def disabled_reason(self) -> str:
        """Human-readable reason for a disabled/blocked feature (for summary)."""
        if not self._enabled_fn():
            return "todo_enabled"
        if not self.actor.bound:
            return "unauthorized"
        return ""

    # -- db ------------------------------------------------------------------- #
    def _db(self) -> TodoStore:
        if self._store_instance is None:
            try:
                from pathlib import Path
                self._store_instance = TodoStore(Path(self._db_path))
            except Exception as e:
                from common.log import logger
                logger.error(f"[TodoService] open db failed: {e}")
                raise TodoUnavailable("存储不可用") from e
        return self._store_instance

    # -- projection ----------------------------------------------------------- #
    def _project(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Project a stored item for the API, including derived fields."""
        now = int(datetime.datetime.now().timestamp())
        due = item.get("due_at")
        return {
            "id": item["id"],
            "scope_id": item["scope_id"],
            "owner_id": item["owner_id"],
            "title": item["title"],
            "description": item.get("description", ""),
            "kind": item.get("kind", "general"),
            "kind_label": _KIND_DISPLAY.get(item.get("kind", "general"), item.get("kind", "")),
            "priority": item.get("priority", "normal"),
            "priority_label": _PRIORITY_DISPLAY.get(item.get("priority", "normal"), ""),
            "status": item.get("status", "pending"),
            "status_label": _STATUS_DISPLAY.get(item.get("status", "pending"), item.get("status", "")),
            "due_at": due,
            "timezone": item.get("timezone", ""),
            "source": item.get("source", "manual"),
            "agent_id": item.get("agent_id", ""),
            "session_id": item.get("session_id", ""),
            "message_seq": item.get("message_seq"),
            "created_by": item.get("created_by", "human"),
            "overdue": _is_overdue(item, now),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "completed_at": item.get("completed_at"),
            "version": item.get("version", 1),
            "can_edit": item.get("status") in ("pending", "in_progress"),
            "can_operate": self._available_status_actions(item.get("status", "pending")),
        }

    @staticmethod
    def _available_status_actions(status: str) -> Dict[str, bool]:
        return {
            "start": status == "pending",
            "complete": status in ("pending", "in_progress"),
            "cancel": status in ("pending", "in_progress"),
            "reopen": status in ("completed", "cancelled"),
        }

    # -- read: list ----------------------------------------------------------- #
    def list(self, *, status: str = "open", q: Optional[str] = None,
             overdue: bool = False, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        self._require_read()
        store = self._db()
        items, total = store.list_items(
            self.actor.scope_id, self.actor.owner_id,
            status=status, q=q, overdue=overdue, page=page, page_size=page_size,
        )
        projected = [self._project(i) for i in items]
        return {
            "items": projected,
            "total": total,
            "page": page,
            "page_size": len(projected),
            "has_more": (page - 1) * page_size + len(projected) < total,
        }

    def summary(self) -> Dict[str, Any]:
        """Return the personal badge counts plus the current capability state."""
        feature_on = self._enabled_fn()
        if not feature_on or not self.actor.bound:
            return {
                "enabled": feature_on,
                "bound": self.actor.bound,
                "disabled_reason": self.disabled_reason,
                "open": 0,
                "overdue": 0,
            }
        if not self.actor.has("todo.read"):
            raise TodoPermissionDenied("无待办读取权限")
        store = self._db()
        return {
            "enabled": True,
            "bound": True,
            "open": store.count_open(self.actor.scope_id, self.actor.owner_id),
            "overdue": store.count_overdue(self.actor.scope_id, self.actor.owner_id),
        }

    def get(self, item_id: str) -> Dict[str, Any]:
        self._require_read()
        store = self._db()
        item = store.get_item(self.actor.scope_id, self.actor.owner_id, item_id)
        if item is None:
            raise TodoNotFoundError()
        return self._project(item)

    def events(self, item_id: str, *, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
        self._require_read()
        store = self._db()
        # Guard: the item must exist and belong to this actor.
        item = store.get_item(self.actor.scope_id, self.actor.owner_id, item_id)
        if item is None:
            raise TodoNotFoundError()
        events, total = store.list_events(
            self.actor.scope_id, self.actor.owner_id, item_id, page=page, page_size=page_size,
        )
        return {
            "items": events,
            "total": total,
            "page": page,
            "page_size": len(events),
            "has_more": (page - 1) * page_size + len(events) < total,
        }

    def source(self, item_id: str) -> Dict[str, Any]:
        """Re-resolve and authorize the source link for an item.

        Only returns the locator (agent/session/message) after re-verifying the
        source is allowed. It never fabricates a jump URL; the caller resolves
        the actual navigation.
        """
        self._require_read()
        store = self._db()
        item = store.get_item(self.actor.scope_id, self.actor.owner_id, item_id)
        if item is None:
            raise TodoNotFoundError()
        if item.get("source") != "conversation":
            return {"available": False, "reason": "no_source", "source": "manual"}
        if not item.get("agent_id") or not item.get("session_id"):
            return {"available": False, "reason": "missing_locator", "source": "conversation"}
        return {
            "available": True,
            "source": "conversation",
            "agent_id": item.get("agent_id"),
            "session_id": item.get("session_id"),
            "message_seq": item.get("message_seq"),
        }

    # -- write: create --------------------------------------------------------- #
    def create(
        self,
        *,
        title: str,
        description: str = "",
        kind: str = "general",
        priority: str = "normal",
        due_at: Any = None,
        timezone: str = "",
        source: str = "manual",
        agent_id: str = "",
        session_id: str = "",
        message_seq: Optional[int] = None,
        create_key: Optional[str] = None,
        operator_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate + persist a new todo with a stable create key.

        Returns the projected new item. On a repeated ``create_key`` with the
        same initial payload (idempotent retry), returns the existing item's
        current view; on a different initial payload, raises a conflict.
        """
        self._require_write()

        # Source references are server-validated: a conversation source must
        # carry a locator, and never trusts a client-supplied owner/tenant.
        if source == "conversation" and (not agent_id or not session_id):
            raise TodoFieldValidationError("会话来源缺少 Agent/会话定位", field="source")

        store = self._db()
        ck = create_key or compute_create_key()
        effective_owner = self.actor.owner_id
        operator = operator_id or effective_owner

        payload = {
            "title": _validate_title(title),
            "description": _validate_description(description),
            "kind": _validate_kind(kind),
            "priority": _validate_priority(priority),
            "timezone": timezone,
            "source": _validate_source(source),
            "agent_id": agent_id or "",
            "session_id": session_id or "",
            "message_seq": message_seq,
        }
        due_ts, tz = _parse_due(due_at, timezone)
        payload["due_at"] = due_ts
        payload["timezone"] = tz
        create_hash = hash_create_payload(payload)

        # Idempotent retry: same key + same initial payload -> return current.
        existing = store.get_item_by_create_key(self.actor.scope_id, effective_owner, ck)
        if existing is not None:
            if existing.get("create_payload_hash") != create_hash:
                raise TodoConflictError("创建标识已被使用，但初始内容不同")
            # Re-authorize on the return path too.
            self._require_read()
            return self._project(existing)

        try:
            item = store.create_item(
                scope_id=self.actor.scope_id,
                owner_id=effective_owner,
                title=payload["title"],
                description=payload["description"],
                kind=payload["kind"],
                priority=payload["priority"],
                due_at=payload["due_at"],
                timezone=payload["timezone"],
                source=payload["source"],
                agent_id=payload["agent_id"],
                session_id=payload["session_id"],
                message_seq=payload["message_seq"],
                created_by="human" if operator == effective_owner else "agent",
                operator_id=operator,
                create_key=ck,
                create_payload_hash=create_hash,
            )
        except TodoConflict as e:
            raise TodoConflictError(str(e)) from e
        except TodoStoreError as e:
            raise TodoUnavailable(str(e)) from e
        return self._project(item)

    # -- write: update ---------------------------------------------------------- #
    def update(
        self,
        item_id: str,
        *,
        expected_version: int,
        fields: Optional[Dict[str, Any]] = None,
        status: Optional[str] = None,
        note: str = "",
        operator_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Apply a field edit OR a target state transition (never both in one call).

        ``fields`` edits content/due/kind/priority; ``status`` performs a direct
        state change. The update advances ``version`` and appends an event in a
        single transaction, refusing stale versions (409).
        """
        self._require_write()
        store = self._db()

        current = store.get_item(self.actor.scope_id, self.actor.owner_id, item_id)
        if current is None:
            raise TodoNotFoundError()
        if current.get("version") != expected_version:
            raise TodoConflictError("旧版本重试被拒绝")

        operator = operator_id or self.actor.owner_id

        if status is not None and fields:
            raise TodoFieldValidationError("一次请求只能选择字段编辑或状态更新", code="invalid_field")

        try:
            if fields is not None:
                # Editing only allowed on active rows; terminal must reopen first.
                if current.get("status") in ("completed", "cancelled"):
                    raise TodoConflictError("终态事项需先重新打开再编辑")
                clean: Dict[str, Any] = {}
                if "title" in fields:
                    clean["title"] = _validate_title(fields["title"])
                if "description" in fields:
                    clean["description"] = _validate_description(fields["description"])
                if "kind" in fields:
                    clean["kind"] = _validate_kind(fields["kind"])
                if "priority" in fields:
                    clean["priority"] = _validate_priority(fields["priority"])
                if "due_at" in fields or "timezone" in fields:
                    # due_at is normalized together with timezone.
                    due_ts, tz = _parse_due(
                        fields.get("due_at"), fields.get("timezone", current.get("timezone", "")),
                        validate_tz=not current.get("timezone"),
                    )
                    clean["due_at"] = due_ts
                    clean["timezone"] = tz
                if not clean:
                    raise TodoFieldValidationError("没有可编辑字段", code="invalid_field")
                updated = store.update_item(
                    self.actor.scope_id, self.actor.owner_id, item_id,
                    expected_version=expected_version,
                    fields=clean,
                    operator_id=operator,
                    operator_kind="human" if operator == self.actor.owner_id else "agent",
                    action="edit",
                    note=_validate_note(note),
                    changed={k: clean[k] for k in clean},
                )
            else:
                target = status or current.get("status")
                if target not in STATUS_VALUES:
                    raise TodoFieldValidationError("未知的状态", field="status")
                if target == current.get("status"):
                    raise TodoFieldValidationError("状态未变化", field="status")
                action_map = {
                    "pending": "reopen" if current.get("status") in ("completed", "cancelled") else "edit",
                    "in_progress": "start",
                    "completed": "complete",
                    "cancelled": "cancel",
                }
                updated = store.update_item(
                    self.actor.scope_id, self.actor.owner_id, item_id,
                    expected_version=expected_version,
                    fields={"status": target},
                    operator_id=operator,
                    operator_kind="human" if operator == self.actor.owner_id else "agent",
                    action=action_map.get(target, "edit"),
                    note=_validate_note(note),
                    changed={"status": target},
                )
        except TodoConflict as e:
            raise TodoConflictError(str(e)) from e
        except TodoFieldError as e:
            raise TodoFieldValidationError(str(e)) from e
        except TodoStoreError as e:
            raise TodoUnavailable(str(e)) from e
        return self._project(updated)


SOURCES = ("manual", "conversation")


def _log():
    from common.log import logger
    return logger
