# encoding:utf-8
"""Token-usage orchestration: buffered writes, scoped reads, display helpers.

Ported from the source deployment (``oneagent-multi-rc``). The buffering shape
is kept — a queue plus a background flush thread, so an LLM call never waits on
SQLite — but the read side is much smaller: because every tenant's rows live in
one database (see ``store.py``), "current tenant" is a filter rather than a
fan-out over per-tenant stores, and the source's ``_resolve_stores`` merge loops
disappear with it.
"""

from __future__ import annotations

import atexit
import queue
import re
import threading
import time
from collections import namedtuple
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from agent.token_usage.store import TokenUsageStore
from common.log import logger

_UsageEvent = namedtuple(
    "_UsageEvent",
    [
        "date_str",
        "tenant_id",
        "actor_user_id",
        "actor_username",
        "session_id",
        "provider",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    ],
)

#: Default window when a caller names no dates, matching the console's own
#: initial range (start-of-month is applied by the page, not here).
DEFAULT_WINDOW_DAYS = 30


def _identity_db_path() -> str:
    """Resolve ``identity.db`` through the same helper the rest of the app uses."""
    from auth.service import identity_db_path

    return identity_db_path()


class TokenUsageManager:
    """Process-wide singleton for recording and querying token usage."""

    _instance: Optional["TokenUsageManager"] = None
    _lock = threading.Lock()

    def __init__(self, flush_interval: int = 10, db_path: Optional[str] = None):
        self._flush_interval = flush_interval
        self._db_path = db_path
        self._store: Optional[TokenUsageStore] = None
        self._store_lock = threading.Lock()
        self._queue: "queue.Queue[_UsageEvent]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._stopped = False
        self._names: Dict[str, str] = {}
        self._names_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def _get_store(self) -> TokenUsageStore:
        if self._store is None:
            with self._store_lock:
                if self._store is None:
                    self._store = TokenUsageStore(self._db_path or _identity_db_path())
        return self._store

    def start(self) -> None:
        """Start the background flush worker (idempotent)."""
        if self._worker is not None and self._worker.is_alive():
            return
        self._stopped = False
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.name = "token-usage-flush"
        self._worker.start()
        logger.info("[TokenUsageManager] Background flush worker started")

    def stop(self, quiet: bool = False) -> None:
        """Flush what is buffered, then stop the worker."""
        self._stopped = True
        if self._worker is not None:
            self._worker.join(timeout=5)
        self._flush(force=True)
        if not quiet:
            logger.info("[TokenUsageManager] Stopped")

    def enqueue(self, event: _UsageEvent) -> None:
        """Buffer one usage event. Never blocks the caller on SQLite."""
        if self._worker is None or not self._worker.is_alive():
            self.start()
        try:
            self._queue.put_nowait(event)
        except Exception as e:  # pragma: no cover - queue is unbounded
            logger.warning(f"[TokenUsageManager] Failed to enqueue event: {e}")

    def _worker_loop(self) -> None:
        while not self._stopped:
            time.sleep(self._flush_interval)
            try:
                self._flush()
            except Exception as e:
                logger.warning(f"[TokenUsageManager] Flush error: {e}")

    def _flush(self, force: bool = False) -> None:
        """Drain the queue, collapsing identical keys before touching the DB."""
        events: List[_UsageEvent] = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except Exception:
                break

        if not events and not force:
            return

        # Collapse in memory first: a burst of calls in the same minute is one
        # upsert rather than one per call.
        key_map: Dict[tuple, Dict[str, Any]] = {}
        for ev in events:
            key = (ev.date_str, ev.tenant_id, ev.actor_user_id, ev.provider, ev.model)
            entry = key_map.setdefault(
                key,
                {
                    "date_str": ev.date_str,
                    "tenant_id": ev.tenant_id,
                    "actor_user_id": ev.actor_user_id,
                    "actor_username": ev.actor_username,
                    "session_id": ev.session_id,
                    "provider": ev.provider,
                    "model": ev.model,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "call_count": 0,
                },
            )
            entry["prompt_tokens"] += max(0, ev.prompt_tokens)
            entry["completion_tokens"] += max(0, ev.completion_tokens)
            entry["total_tokens"] += max(0, ev.total_tokens)
            entry["call_count"] += 1

        store = self._get_store()
        for entry in key_map.values():
            try:
                store.record(**entry)
            except Exception as e:
                logger.warning(f"[TokenUsageManager] Insert failed: {e}")

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def _window(
        self, start_date: Optional[date], end_date: Optional[date]
    ) -> "tuple[date, date]":
        if end_date is None:
            end_date = date.today()
        if start_date is None:
            start_date = end_date - timedelta(days=DEFAULT_WINDOW_DAYS)
        return start_date, end_date

    def summary(
        self,
        *,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        tenant_id: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        actor_username: Optional[str] = None,
    ) -> Dict[str, Any]:
        start_date, end_date = self._window(start_date, end_date)
        self._flush()
        return self._get_store().summary(
            start_date=start_date, end_date=end_date, tenant_id=tenant_id,
            actor_user_id=actor_user_id, model=model, provider=provider,
            actor_username=actor_username,
        )

    def details(
        self,
        *,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        tenant_id: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        actor_username: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        start_date, end_date = self._window(start_date, end_date)
        self._flush()
        rows = self._get_store().query_details(
            start_date=start_date, end_date=end_date, tenant_id=tenant_id,
            actor_user_id=actor_user_id, model=model, provider=provider,
            actor_username=actor_username,
        )
        self._fill_usernames(rows)
        return rows

    def by_user(
        self,
        *,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        tenant_id: Optional[str] = None,
        actor_username: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        start_date, end_date = self._window(start_date, end_date)
        self._flush()
        rows = self._get_store().by_user(
            start_date=start_date, end_date=end_date, tenant_id=tenant_id,
            actor_username=actor_username,
        )
        self._fill_usernames(rows)
        return rows

    def call_logs(
        self,
        *,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        tenant_id: Optional[str] = None,
        actor_user_id: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        status: Optional[str] = None,
        session_id: Optional[str] = None,
        actor_username: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Call-log rows, prepared for display.

        Three presentation rules are inherited from the source console, because
        the page it renders is the deliverable being matched:

        1. rows whose ``input_summary`` is identical are merged (tokens summed,
           ``call_count`` incremented). This is a display-level grouping, not a
           claim that one call happened — the token totals are preserved.
        2. rows carrying no counters at all get a rough estimate derived from
           the summaries and are flagged ``estimated``, so a provider that
           reports no usage still shows something instead of a row of zeros.
        3. secrets in either summary are masked before the row is returned.
        """
        start_date, end_date = self._window(start_date, end_date)
        self._flush()
        raw = self._get_store().query_call_logs(
            start_date=start_date, end_date=end_date, tenant_id=tenant_id,
            actor_user_id=actor_user_id, model=model, provider=provider,
            status=status, session_id=session_id, actor_username=actor_username,
            limit=limit, offset=offset,
        )
        for row in raw:
            row["call_count"] = 1

        raw.sort(key=lambda r: (r.get("created_at") or 0, r.get("id") or 0), reverse=True)
        merged: List[Dict[str, Any]] = []
        index_by_input: Dict[str, int] = {}
        for row in raw:
            key = (row.get("input_summary") or "").strip()
            if not key:
                _estimate_call_row(row)
                _mask_call_row(row)
                merged.append(row)
                continue
            if key in index_by_input:
                base = merged[index_by_input[key]]
                base["call_count"] = base.get("call_count", 1) + 1
                base["prompt_tokens"] = (base.get("prompt_tokens") or 0) + (row.get("prompt_tokens") or 0)
                base["completion_tokens"] = (base.get("completion_tokens") or 0) + (row.get("completion_tokens") or 0)
                base["total_tokens"] = (base.get("total_tokens") or 0) + (row.get("total_tokens") or 0)
                base["estimated"] = bool(base.get("estimated") or row.get("estimated"))
                continue
            _estimate_call_row(row)
            _mask_call_row(row)
            merged.append(row)
            index_by_input[key] = len(merged) - 1

        merged.sort(key=lambda r: (r.get("created_at") or 0, r.get("id") or 0), reverse=True)
        self._fill_usernames(merged)
        return merged[:limit]

    def distinct_values(self, column: str) -> List[str]:
        return self._get_store().distinct_values(column)

    # ------------------------------------------------------------------
    # Actor display names
    # ------------------------------------------------------------------

    def _fill_usernames(self, rows: List[Dict[str, Any]]) -> None:
        """Fill blank ``actor_username`` values for display.

        Resolved on read rather than on write: the write path runs on the flush
        worker and must stay free of lookups, and the console only ever renders
        a bounded page. Unknown ids are left blank (the console falls back to
        showing the id) rather than dropped.
        """
        pending = {
            row.get("actor_user_id") or ""
            for row in rows
            if not row.get("actor_username") and row.get("actor_user_id")
        }
        if not pending:
            return
        names = self._usernames_for(pending)
        for row in rows:
            if not row.get("actor_username"):
                row["actor_username"] = names.get(row.get("actor_user_id") or "", "")

    def _usernames_for(self, user_ids: "set[str]") -> Dict[str, str]:
        unresolved = []
        with self._names_lock:
            for uid in user_ids:
                if uid in self._names:
                    continue
                unresolved.append(uid)

        if unresolved:
            found: Dict[str, str] = {}
            try:
                store = self._get_store()
                placeholders = ",".join("?" for _ in unresolved)
                for row in store._store.execute(
                    f"SELECT id, username FROM users WHERE id IN ({placeholders})",
                    tuple(unresolved),
                ):
                    found[row[0]] = row[1] or ""
            except Exception as e:
                # A display name is not worth failing a query over.
                logger.debug(f"[TokenUsageManager] Username lookup failed: {e}")
            with self._names_lock:
                for uid in unresolved:
                    # Cache the miss too, so a deleted user is not re-queried on
                    # every page load.
                    self._names[uid] = found.get(uid, "")

        with self._names_lock:
            return {uid: self._names.get(uid, "") for uid in user_ids}

    @classmethod
    def get_instance(cls) -> "TokenUsageManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = cls()
                    atexit.register(lambda: instance.stop(quiet=True))
                    cls._instance = instance
        return cls._instance


def get_token_usage_manager() -> TokenUsageManager:
    return TokenUsageManager.get_instance()


# ----------------------------------------------------------------------
# Display-layer helpers for LLM call logs
# ----------------------------------------------------------------------

# 常见敏感字段名：命中后仅把其值打码为 *（字段名保留，便于辨认）
_SECRET_FIELD_RE = re.compile(
    r"(api[_-]?key|apikey|secret|password|passwd|access[_-]?token|auth[_-]?token|"
    r"token|authorization|bearer|credential|app[_-]?secret|app[_-]?key)"
    r"\s*[=:]\s*(?:[\"']?)([A-Za-z0-9_\-./+=]{4,})",
    re.IGNORECASE,
)
# 明显的密钥前缀 token（sk-/glpat-/ghp- 等），整体以 * 隐藏
_SK_TOKEN_RE = re.compile(r"\b(?:sk|sk-|ghp|glpat|ak)[-_][A-Za-z0-9_\-]{6,}\b")


def _mask_secrets(text: str) -> str:
    """只对输入/输出摘要里明确的密钥/密码值打码（值显示为 *），其它内容不做脱敏。"""
    if not text:
        return text

    def _field_mask(match: "re.Match[str]") -> str:
        name = match.group(1)
        return f"{name}=*"

    text = _SECRET_FIELD_RE.sub(_field_mask, str(text))
    text = _SK_TOKEN_RE.sub("*", text)
    return text


def _estimate_text_tokens(text: str) -> int:
    """粗略估算一段文本的 token 数（中文字符 ~0.6 token/字，其它 ~4 字符/token）。"""
    if not text:
        return 0
    text = str(text)
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", text))
    other = len(text) - cjk
    return max(0, int(cjk * 0.6 + other / 4.0))


def _estimate_call_row(row: Dict[str, Any]) -> None:
    """对全 0 的调用日志行按摘要给一个展示用估算值，并打 estimated 标记。"""
    p = row.get("prompt_tokens") or 0
    c = row.get("completion_tokens") or 0
    t = row.get("total_tokens") or 0
    if p or c or t:
        row["estimated"] = False
        return
    est_p = _estimate_text_tokens(row.get("input_summary") or "")
    est_c = _estimate_text_tokens(row.get("output_summary") or "")
    # 至少给 1 个 token，避免整行为 0 显示
    est_p = max(est_p, 1 if (row.get("input_summary") or "").strip() else 0)
    est_c = max(est_c, 1 if (row.get("output_summary") or "").strip() else 0)
    row["prompt_tokens"] = est_p
    row["completion_tokens"] = est_c
    row["total_tokens"] = est_p + est_c
    row["estimated"] = bool(est_p or est_c)


def _mask_call_row(row: Dict[str, Any]) -> None:
    """展示前对输入/输出摘要做敏感信息打码。"""
    if row.get("input_summary"):
        row["input_summary"] = _mask_secrets(row["input_summary"])
    if row.get("output_summary"):
        row["output_summary"] = _mask_secrets(row["output_summary"])
