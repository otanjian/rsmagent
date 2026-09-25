# encoding:utf-8
"""Token-usage storage, in ``identity.db`` next to the audit trail.

Ported from the source deployment (``oneagent-multi-rc``) with two deliberate
changes, both forced by this fork's data model:

* **One database, not one per tenant.** The source keeps a
  ``.one/conversations.db`` inside every tenant workspace and merges the
  per-tenant stores on read (``manager._resolve_stores``). Here the rows live in
  ``identity.db`` beside ``audit_events``, which is where tenancy itself lives —
  so a cross-tenant read is one query with one filter instead of a fan-out, and
  a tenant-scoped read cannot silently miss a tenant whose workspace directory
  happens to be missing.
* **Identity is stored twice, like ``audit_events``.** ``actor_user_id`` is the
  stable key; ``actor_username`` is the display value captured at write time so
  the console does not resolve every row through ``users``.

Tenant filtering is a parameter, and ``None`` means "do not filter" — that is
the platform-admin read. Callers must therefore never pass ``None`` on behalf of
a tenant-scoped request: resolve the caller's scope first (see
``agent/token_usage/manager.py``), because the failure mode here is a
cross-tenant leak, not an error.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from auth.store import IdentityStore


class TokenUsageStore:
    """Read/write access to ``token_usage`` and ``llm_call_logs``."""

    def __init__(self, db_path: str):
        self._store = IdentityStore(db_path)

    @property
    def db_path(self) -> str:
        return self._store.db_path

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        date_str: str,
        tenant_id: str = "",
        actor_user_id: str = "",
        actor_username: str = "",
        session_id: str = "",
        provider: str = "",
        model: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        call_count: int = 1,
    ) -> None:
        """Accumulate one day's usage for ``tenant/user/provider/model``.

        The conflict target is the ``idx_token_usage_key`` unique index, so this
        is a single atomic statement. The source instead read the row, decided,
        and then wrote — which loses counts when two flush workers overlap.
        """
        sql = """
            INSERT INTO token_usage
                (date, tenant_id, actor_user_id, actor_username, session_id,
                 provider, model, prompt_tokens, completion_tokens, total_tokens,
                 call_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, unixepoch())
            ON CONFLICT(date, tenant_id, actor_user_id, provider, model) DO UPDATE SET
                prompt_tokens     = prompt_tokens + excluded.prompt_tokens,
                completion_tokens = completion_tokens + excluded.completion_tokens,
                total_tokens      = total_tokens + excluded.total_tokens,
                call_count        = call_count + excluded.call_count,
                -- Late-arriving display values only ever fill a blank, so a
                -- rename does not rewrite the recorded history backwards.
                actor_username = CASE WHEN actor_username = ''
                                       AND excluded.actor_username <> ''
                                      THEN excluded.actor_username
                                      ELSE actor_username END,
                session_id     = CASE WHEN excluded.session_id <> ''
                                      THEN excluded.session_id
                                      ELSE session_id END
        """
        params = (
            date_str,
            tenant_id or "",
            actor_user_id or "",
            actor_username or "",
            session_id or "",
            provider or "",
            model or "",
            max(0, int(prompt_tokens or 0)),
            max(0, int(completion_tokens or 0)),
            max(0, int(total_tokens or 0)),
            max(1, int(call_count or 1)),
        )
        with self._store.connect() as con:
            con.execute(sql, params)
            con.commit()

    def insert_call_log(
        self,
        *,
        tenant_id: str = "",
        actor_user_id: str = "",
        actor_username: str = "",
        session_id: str = "",
        provider: str = "",
        model: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        input_summary: str = "",
        output_summary: str = "",
        status: str = "success",
        duration_ms: int = 0,
        created_at: Optional[int] = None,
    ) -> None:
        """Append one coarse-grained LLM call row."""
        sql = """
            INSERT INTO llm_call_logs
                (created_at, tenant_id, actor_user_id, actor_username, session_id,
                 provider, model, prompt_tokens, completion_tokens, total_tokens,
                 input_summary, output_summary, status, duration_ms)
            VALUES (COALESCE(?, unixepoch()), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            None if created_at is None else int(created_at),
            tenant_id or "",
            actor_user_id or "",
            actor_username or "",
            session_id or "",
            provider or "",
            model or "",
            max(0, int(prompt_tokens or 0)),
            max(0, int(completion_tokens or 0)),
            max(0, int(total_tokens or 0)),
            input_summary or "",
            output_summary or "",
            status or "success",
            max(0, int(duration_ms or 0)),
        )
        with self._store.connect() as con:
            con.execute(sql, params)
            con.commit()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _usage_filters(
        self,
        *,
        start_date: Optional[date],
        end_date: Optional[date],
        tenant_id: Optional[str],
        actor_user_id: Optional[str],
        model: Optional[str],
        provider: Optional[str],
        actor_username: Optional[str] = None,
    ) -> "tuple[List[str], List[Any]]":
        clauses: List[str] = ["1=1"]
        params: List[Any] = []
        if start_date is not None:
            clauses.append("date >= ?")
            params.append(start_date.isoformat())
        if end_date is not None:
            clauses.append("date <= ?")
            params.append(end_date.isoformat())
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if actor_user_id:
            clauses.append("actor_user_id = ?")
            params.append(actor_user_id)
        if actor_username:
            # The console filters by the name it *shows*, which is the name
            # recorded with the row. Escaped, so a stray ``%`` is a literal.
            pattern = str(actor_username).replace("\\", "\\\\") \
                .replace("%", "\\%").replace("_", "\\_")
            clauses.append("actor_username LIKE ? ESCAPE '\\'")
            params.append(f"%{pattern}%")
        if model:
            clauses.append("model = ?")
            params.append(model)
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        return clauses, params

    def query_details(
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
        """Per-day, per-model rows ("明细" tab), newest day first."""
        clauses, params = self._usage_filters(
            start_date=start_date, end_date=end_date, tenant_id=tenant_id,
            actor_user_id=actor_user_id, model=model, provider=provider,
            actor_username=actor_username,
        )
        sql = (
            "SELECT date, tenant_id, actor_user_id, MAX(actor_username), model, provider,"
            " SUM(prompt_tokens), SUM(completion_tokens), SUM(total_tokens),"
            " SUM(call_count)"
            " FROM token_usage WHERE " + " AND ".join(clauses) +
            " GROUP BY date, tenant_id, actor_user_id, model, provider"
            " ORDER BY date DESC, model"
        )
        rows = self._store.execute(sql, params)
        return [
            {
                "date": r[0],
                "tenant_id": r[1] or "",
                "actor_user_id": r[2] or "",
                "actor_username": r[3] or "",
                "model": r[4] or "",
                "provider": r[5] or "",
                "prompt_tokens": r[6] or 0,
                "completion_tokens": r[7] or 0,
                "total_tokens": r[8] or 0,
                "call_count": r[9] or 0,
            }
            for r in rows
        ]

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
        """Totals plus ``by_model`` / ``by_date`` breakdowns for the cards."""
        clauses, params = self._usage_filters(
            start_date=start_date, end_date=end_date, tenant_id=tenant_id,
            actor_user_id=actor_user_id, model=model, provider=provider,
            actor_username=actor_username,
        )
        where = " AND ".join(clauses)

        totals = self._store.execute(
            "SELECT COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0),"
            " COALESCE(SUM(total_tokens),0), COALESCE(SUM(call_count),0)"
            " FROM token_usage WHERE " + where,
            params,
        )[0]

        by_model: Dict[str, Dict[str, Any]] = {}
        for r in self._store.execute(
            "SELECT provider, model, COALESCE(SUM(prompt_tokens),0),"
            " COALESCE(SUM(completion_tokens),0), COALESCE(SUM(call_count),0)"
            " FROM token_usage WHERE " + where +
            " GROUP BY provider, model ORDER BY SUM(total_tokens) DESC",
            params,
        ):
            key = f"{r[0]}:{r[1]}" if r[0] else r[1]
            by_model[key] = {
                "provider": r[0] or "",
                "model": r[1] or "",
                "prompt_tokens": r[2] or 0,
                "completion_tokens": r[3] or 0,
                "call_count": r[4] or 0,
            }

        by_date: Dict[str, Dict[str, Any]] = {}
        for r in self._store.execute(
            "SELECT date, COALESCE(SUM(prompt_tokens),0),"
            " COALESCE(SUM(completion_tokens),0), COALESCE(SUM(call_count),0)"
            " FROM token_usage WHERE " + where + " GROUP BY date ORDER BY date DESC",
            params,
        ):
            by_date[r[0]] = {
                "prompt_tokens": r[1] or 0,
                "completion_tokens": r[2] or 0,
                "call_count": r[3] or 0,
            }

        return {
            "total_prompt_tokens": totals[0] or 0,
            "total_completion_tokens": totals[1] or 0,
            "total_tokens": totals[2] or 0,
            "total_calls": totals[3] or 0,
            "by_model": by_model,
            "by_date": by_date,
        }

    def by_user(
        self,
        *,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        tenant_id: Optional[str] = None,
        actor_username: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Per-user aggregate ("按用户汇总" tab), heaviest user first."""
        clauses, params = self._usage_filters(
            start_date=start_date, end_date=end_date, tenant_id=tenant_id,
            actor_user_id=None, model=None, provider=None,
            actor_username=actor_username,
        )
        sql = (
            "SELECT actor_user_id,"
            " MAX(actor_username) AS actor_username,"
            " COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0),"
            " COALESCE(SUM(total_tokens),0), COALESCE(SUM(call_count),0)"
            " FROM token_usage WHERE " + " AND ".join(clauses) +
            " GROUP BY actor_user_id ORDER BY SUM(total_tokens) DESC"
        )
        return [
            {
                "actor_user_id": r[0] or "",
                "actor_username": r[1] or "",
                "prompt_tokens": r[2] or 0,
                "completion_tokens": r[3] or 0,
                "total_tokens": r[4] or 0,
                "call_count": r[5] or 0,
            }
            for r in self._store.execute(sql, params)
        ]

    def query_call_logs(
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
        """Newest-first page of LLM call rows ("调用日志" tab)."""
        clauses: List[str] = ["1=1"]
        params: List[Any] = []
        if start_date is not None:
            clauses.append("DATE(created_at, 'unixepoch') >= ?")
            params.append(start_date.isoformat())
        if end_date is not None:
            clauses.append("DATE(created_at, 'unixepoch') <= ?")
            params.append(end_date.isoformat())
        if tenant_id is not None:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if actor_user_id:
            clauses.append("actor_user_id = ?")
            params.append(actor_user_id)
        if actor_username:
            pattern = str(actor_username).replace("\\", "\\\\") \
                .replace("%", "\\%").replace("_", "\\_")
            clauses.append("actor_username LIKE ? ESCAPE '\\'")
            params.append(f"%{pattern}%")
        if model:
            clauses.append("model = ?")
            params.append(model)
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)

        sql = (
            "SELECT id, created_at, tenant_id, actor_user_id, actor_username,"
            " session_id, provider, model, prompt_tokens, completion_tokens,"
            " total_tokens, input_summary, output_summary, status, duration_ms"
            " FROM llm_call_logs WHERE " + " AND ".join(clauses) +
            " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        )
        params.extend([max(1, min(int(limit), 1000)), max(0, int(offset))])
        return [
            {
                "id": r[0],
                "created_at": r[1],
                "tenant_id": r[2] or "",
                "actor_user_id": r[3] or "",
                "actor_username": r[4] or "",
                "session_id": r[5] or "",
                "provider": r[6] or "",
                "model": r[7] or "",
                "prompt_tokens": r[8] or 0,
                "completion_tokens": r[9] or 0,
                "total_tokens": r[10] or 0,
                "input_summary": r[11] or "",
                "output_summary": r[12] or "",
                "status": r[13] or "",
                "duration_ms": r[14] or 0,
            }
            for r in self._store.execute(sql, params)
        ]

    def distinct_values(self, column: str) -> List[str]:
        """Distinct providers or models, for the console's filter dropdowns."""
        if column not in ("provider", "model"):
            raise ValueError("unsupported column")
        rows = self._store.execute(
            f"SELECT DISTINCT {column} FROM token_usage WHERE {column} <> ''"
            f" ORDER BY {column}"
        )
        return [r[0] for r in rows]
