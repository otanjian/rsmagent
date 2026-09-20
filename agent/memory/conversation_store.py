"""
Conversation history persistence using SQLite.

Design:
- sessions table: per-session metadata (channel_type, last_active, msg_count)
- messages table: individual messages stored as JSON, append-only
- Pruning: age-based only (sessions not updated within N days are deleted)
- Thread-safe via a single in-process lock

Storage path: <agent workspace>/memory/long-term/index.db (shared with the
memory index)
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from common.log import logger
from agent.memory import conversation_schema as schema_seam


def normalize_session_search_query(q: Optional[str]) -> str:
    """Validate the optional, literal session-title search query."""
    if q is None:
        return ""
    if not isinstance(q, str):
        raise ValueError("q must be a string")
    q = q.strip()
    if len(q) > 100:
        raise ValueError("q must be at most 100 characters")
    return q


#: The dimensions a conversation row can be scoped by. ``agent_id`` is
#: upstream's multi-agent dimension; ``owner`` and ``tenant_id`` are this
#: fork's tenancy dimension. The composed schema defines them
#: (``agent/memory/conversation_schema.py``); this is how a query applies them.
DIMENSION_COLUMNS: tuple = ("agent_id", "owner", "tenant_id")

#: Dimensions a bound store always filters on, even when the value is empty.
#:
#: ``agent_id`` is upstream's multi-agent dimension, and its empty value is the
#: **default Agent** -- ``''`` is not "unattributed", it is who owns the row
#: (upstream's migration adds ``agent_id TEXT NOT NULL DEFAULT ''`` so a
#: single-Agent install keeps its data). A ``ConversationStore`` handle is bound
#: to exactly one Agent, so it must scope to that id *including when it is
#: empty*: skipping the predicate on ``''`` would let the default Agent read
#: every other Agent's transcripts out of the shared global file -- the exact
#: collision the composite key exists to prevent.
ALWAYS_SCOPED_DIMENSIONS: frozenset = frozenset({"agent_id"})

#: Retired rule: dimensions whose empty value used to be *widened* to also match
#: ``''`` rows. It held while the fork was the only model in play ("a scoped read
#: also sees the empty-agent rows"). Upstream's global store makes that widening
#: a cross-Agent leak instead -- a secondary Agent reading its own scope must not
#: see the default Agent's untagged rows -- and upstream's
#: ``tests/test_conversation_global_migration.py`` pins the strict behaviour.
#: Kept as an (empty) extension point so the mechanism and its reasoning stay
#: visible rather than being rediscovered by the next sync.
LEGACY_EMPTY_DIMENSIONS: frozenset = frozenset()


def ambient_dimensions() -> Dict[str, str]:
    """The dimension values carried by the ambient runtime identity.

    Empty string means "this dimension is not in scope" for ``owner`` and
    ``tenant_id``: legacy callers and machine-initiated work carry no user or
    tenant, and an empty value skips that predicate instead of matching only the
    empty column, which is what keeps pre-dimension rows readable.

    ``agent_id`` does not follow that rule -- see
    ``ALWAYS_SCOPED_DIMENSIONS``. A store also overrides it with its own
    binding (:meth:`ConversationStore._dimensions`), because upstream's global
    model makes the handle, not the ambient identity, decide which Agent's rows
    it may touch.
    """
    from common.runtime_identity import current_identity

    ident = current_identity()
    return {
        "agent_id": str(getattr(ident, "agent_id", "") or ""),
        "owner": str(getattr(ident, "user_id", "") or ""),
        "tenant_id": str(getattr(ident, "tenant_id", "") or ""),
    }


def dimension_clause(
    alias: str = "",
    values: Optional[Dict[str, str]] = None,
    keys: tuple = DIMENSION_COLUMNS,
) -> tuple:
    """Build ``(sql_fragment, params)`` restricting a query to set dimensions.

    Returns ``("", ())`` when no dimension is in scope, so an unscoped read
    keeps the historical "see everything" semantics. A dimension in
    ``ALWAYS_SCOPED_DIMENSIONS`` is emitted even when its value is empty.
    """
    values = ambient_dimensions() if values is None else values
    clauses: List[str] = []
    params: List[str] = []
    for key in keys:
        if key not in values:
            # Absent is not the same as empty: a caller that does not carry a
            # dimension has no opinion about it (no filter), while ``''`` is a
            # value in its own right -- for ``agent_id`` that is the default
            # Agent's scope, which is exactly why it is always scoped below.
            continue
        value = values.get(key) or ""
        if not value and key not in ALWAYS_SCOPED_DIMENSIONS:
            continue
        if key in LEGACY_EMPTY_DIMENSIONS:
            # ``''`` is the pre-multi-Agent/default domain (see
            # ``LEGACY_EMPTY_DIMENSIONS``), so a scoped read includes it.
            clauses.append(f"({alias}{key} = ? OR {alias}{key} = '')")
        else:
            clauses.append(f"{alias}{key} = ?")
        params.append(value)
    if not clauses:
        return "", ()
    return " AND " + " AND ".join(clauses), tuple(params)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# Core conversation schema. Sessions and messages are the irreplaceable part
# of this file, so their creation must always succeed; nothing optional belongs
# in this script.
#
# The definition itself is composed, not literal: see
# ``agent/memory/conversation_schema.py`` for the seam that lets upstream's
# agent dimension (composite keys) and this fork's tenancy dimension (owner,
# tenant_id) be registered independently instead of being edited into one
# literal that every merge rewrites.
def _core_ddl() -> str:
    return schema_seam.build_ddl()


def _core_schema():
    return schema_seam.conversation_schema()


# Runs are an auxiliary table in the same file. Kept out of the core script so
# that any problem here -- a legacy table of the same name, a partial upgrade --
# degrades run tracking rather than taking conversation history down with it.
#
# A run is one addressable, persisted unit of an agent's work: the thing a
# delegated task, a subagent spawn or a scheduled job can be looked up by,
# resumed from and reported on after the call that started it has returned.
# Distinct from a session (who the conversation is with) and an agent (who is
# doing the work).
_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT    PRIMARY KEY,
    agent_id     TEXT    NOT NULL DEFAULT '',
    -- Reserved for the tenancy dimension; empty until per-user isolation lands,
    -- so filtering by owner is an additive query change rather than a schema one.
    user_id      TEXT    NOT NULL DEFAULT '',
    session_id   TEXT    NOT NULL DEFAULT '',
    -- The run that spawned this one (a delegation or subagent). Empty for a
    -- top-level run. Lets a whole delegation tree be walked from any node.
    parent_run_id TEXT   NOT NULL DEFAULT '',
    -- Free-form external work handle and where it came from. task_source is
    -- empty for a native RongAI run; a non-empty value names the external
    -- system and task_id then addresses a work item within it. TEXT on purpose:
    -- it must hold external ids, never a foreign key into a table we own.
    task_id      TEXT    NOT NULL DEFAULT '',
    task_source  TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL DEFAULT 'running',
    started_at   INTEGER NOT NULL,
    ended_at     INTEGER,
    error        TEXT    NOT NULL DEFAULT '',
    extras       TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_runs_session
    ON runs (session_id, started_at);

CREATE INDEX IF NOT EXISTS idx_runs_parent
    ON runs (parent_run_id);

CREATE INDEX IF NOT EXISTS idx_runs_task
    ON runs (task_source, task_id);
"""

# Bookkeeping for the one-time, idempotent global migration. Lives in the
# global file itself so "have we already absorbed source X?" survives restarts
# without a side-car file. Mirrors the scheduler's ``_migrate_legacy_task_stores``
# marker discipline.
_META_DDL = """
CREATE TABLE IF NOT EXISTS _migration_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL DEFAULT '',
    done_at INTEGER NOT NULL DEFAULT 0
);
"""
DEFAULT_MAX_AGE_DAYS: int = 30


def _is_visible_user_message(content: Any) -> bool:
    """
    Return True when a user-role message represents actual user input
    (not an internal tool_result injected by the agent loop).
    """
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("type") == "text"
            for b in content
        )
    return False


def _extract_display_text(content: Any) -> str:
    """
    Extract the human-readable text portion from a message content value.
    Returns an empty string for tool_use / tool_result blocks.
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p).strip()
    return ""


# Internal markers written into the session for the agent's own bookkeeping
# (scheduler injection / self-evolution undo). They must stay in the stored
# content (the LLM reads them, e.g. to find a backup_id for undo) but should
# never be shown verbatim to the user in the chat history UI.
_SCHEDULED_DISPLAY_MARKERS = ("[SCHEDULED]", "Scheduled task")
_EVOLUTION_DISPLAY_MARKER = "[EVOLUTION]"


def _is_internal_user_marker(text: str) -> bool:
    """True if a user-turn text is an internal injection marker (hide from UI)."""
    t = (text or "").lstrip()
    return any(t.startswith(m) for m in _SCHEDULED_DISPLAY_MARKERS)


def _is_evolution_text(text: str) -> bool:
    """True if assistant text is a self-evolution summary (before cleaning)."""
    return (text or "").lstrip().startswith(_EVOLUTION_DISPLAY_MARKER)


def _message_plain_text(raw_content: Any) -> str:
    """Human-readable text of a stored message, whose content is a JSON string.

    Decodes the DB-stored JSON (a string or a list of content blocks) and reuses
    :func:`_extract_display_text`. Returns '' for tool-only messages.
    """
    try:
        content = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
    except Exception:
        content = raw_content
    return _extract_display_text(content)


def _message_is_scheduled_marker(raw_content: Any) -> bool:
    """True when a stored user message is the ``[SCHEDULED]`` injection marker
    that precedes a scheduled task's delivered assistant reply."""
    return _is_internal_user_marker(_message_plain_text(raw_content))


def _clean_display_text(text: str) -> str:
    """Strip internal markers from assistant text for user-facing display.

    Removes a leading ``[EVOLUTION]`` tag and a trailing ``(backup_id: ...)``
    undo hint. The raw stored message is untouched, so undo + LLM context still
    work; only the rendered chat bubble is cleaned.
    """
    if not text:
        return text
    cleaned = text
    stripped = cleaned.lstrip()
    if stripped.startswith(_EVOLUTION_DISPLAY_MARKER):
        cleaned = stripped[len(_EVOLUTION_DISPLAY_MARKER):].lstrip()
    # Drop a trailing backup_id undo hint line, e.g.
    #   "(backup_id: 20260607-...; to undo, restore this backup)"
    cleaned = re.sub(
        r"\n*\(backup_id:[^\)]*\)\s*$",
        "",
        cleaned,
    ).rstrip()
    return cleaned


def _extract_tool_calls(content: Any) -> List[Dict[str, Any]]:
    """
    Extract tool_use blocks from an assistant message content.
    Returns a list of {name, arguments} dicts (result filled in later).
    """
    if not isinstance(content, list):
        return []
    return [
        {"id": b.get("id", ""), "name": b.get("name", ""), "arguments": b.get("input", {})}
        for b in content
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]


def _extract_tool_results(content: Any) -> Dict[str, dict]:
    """
    Extract tool_result blocks from a user message, keyed by tool_use_id.
    Values are {"result": str, "is_error": bool}.
    """
    if not isinstance(content, list):
        return {}
    results = {}
    for b in content:
        if not isinstance(b, dict) or b.get("type") != "tool_result":
            continue
        tool_id = b.get("tool_use_id", "")
        result_content = b.get("content", "")
        if isinstance(result_content, list):
            result_content = "\n".join(
                rb.get("text", "") for rb in result_content
                if isinstance(rb, dict) and rb.get("type") == "text"
            )
        results[tool_id] = {"result": str(result_content), "is_error": bool(b.get("is_error", False))}
    return results


def _group_into_display_turns(
    rows: List[tuple],
    include_thinking: bool = True,
) -> List[Dict[str, Any]]:
    """
    Convert raw DB rows into display turns. Rows loaded for the web history
    include ``seq`` as their first field; older callers may still pass the
    legacy ``(role, content_json, created_at, extras)`` shape.

    One display turn = one visible user message  +  one merged assistant reply.
    All intermediate assistant messages (those carrying tool_use) and the final
    assistant text reply produced for the same user query are collapsed into a
    single assistant turn, exactly matching the live SSE rendering where tools
    and the final answer appear inside the same bubble.

    Grouping rules:
    - A visible user message starts a new group.
    - tool_result user messages are internal; their content is attached to the
      matching tool_use entry via tool_use_id and they never become own turns.
    - All assistant messages within a group are merged:
        * tool_use blocks → tool_calls list (result filled from tool_results)
        * text blocks → last non-empty text becomes the display content
    """
    # ------------------------------------------------------------------ #
    # Pass 1: split rows into groups, each starting with a visible user msg
    # ------------------------------------------------------------------ #
    # group = (user_row | None, [subsequent_rows])
    # user_row: (content, created_at)
    groups: List[tuple] = []
    cur_user: Optional[tuple] = None
    cur_rest: List[tuple] = []
    started = False

    for row in rows:
        if len(row) == 5:
            seq, role, raw_content, created_at, raw_extras = row
        else:
            seq = None
            role, raw_content, created_at, raw_extras = row
        try:
            content = json.loads(raw_content)
        except Exception:
            content = raw_content
        try:
            extras = json.loads(raw_extras) if raw_extras else {}
            if not isinstance(extras, dict):
                extras = {}
        except Exception:
            extras = {}

        if role == "user" and _is_visible_user_message(content):
            if started:
                groups.append((cur_user, cur_rest))
            cur_user = (content, created_at, extras, seq)
            cur_rest = []
            started = True
        else:
            cur_rest.append((role, content, created_at, extras, seq))

    if started:
        groups.append((cur_user, cur_rest))

    # ------------------------------------------------------------------ #
    # Pass 2: build display turns from each group
    # ------------------------------------------------------------------ #
    turns: List[Dict[str, Any]] = []

    for user_row, rest in groups:
        # User turn
        if user_row:
            content, created_at, _u_extras, user_seq = user_row
            text = _extract_display_text(content)
            # Hide internal injection markers (scheduler / self-evolution) so the
            # user never sees a synthetic "[SCHEDULED] self-evolution" bubble;
            # the assistant reply that follows is still rendered.
            if text and not _is_internal_user_marker(text):
                turn = {"role": "user", "content": text, "created_at": created_at}
                if user_seq is not None:
                    turn["_seq"] = user_seq
                turns.append(turn)

        # Build an ordered list of steps preserving the original sequence:
        #   thinking → content → tool_call → content → ...
        steps: List[Dict[str, Any]] = []
        tool_results: Dict[str, str] = {}
        final_text = ""
        final_ts: Optional[int] = None
        final_seq: Optional[int] = None
        merged_extras: Dict[str, Any] = {}

        for role, content, created_at, extras, seq in rest:
            if role == "assistant" and isinstance(extras, dict):
                merged_extras.update(extras)
            if role == "user":
                tool_results.update(_extract_tool_results(content))
            elif role == "assistant":
                # Walk content blocks in order to preserve interleaving
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "thinking":
                            if not include_thinking:
                                continue
                            txt = block.get("thinking", "").strip()
                            if txt:
                                steps.append({"type": "thinking", "content": txt})
                        elif btype == "text":
                            txt = block.get("text", "").strip()
                            if txt:
                                steps.append({"type": "content", "content": txt})
                                final_text = txt
                        elif btype == "tool_use":
                            steps.append({
                                "type": "tool",
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "arguments": block.get("input", {}),
                            })
                elif isinstance(content, str) and content.strip():
                    steps.append({"type": "content", "content": content.strip()})
                    final_text = content.strip()
                final_ts = created_at
                if seq is not None:
                    final_seq = seq

        # Attach tool results to tool steps
        for step in steps:
            if step["type"] == "tool":
                tr = tool_results.get(step.get("id", ""), {})
                if not isinstance(tr, dict):
                    tr = {"result": tr}
                step["result"] = tr.get("result", "")
                step["is_error"] = tr.get("is_error", False)

        # Detect a self-evolution bubble BEFORE cleaning the marker away, so the
        # UI can flag it even though the visible text stays clean.
        is_evolution = _is_evolution_text(final_text)

        # Clean internal markers from the user-facing assistant text. Applies to
        # both the final content and the mirrored content step so the rendered
        # bubble shows clean text while the stored message keeps the markers.
        final_text = _clean_display_text(final_text)
        for step in steps:
            if step.get("type") == "content":
                step["content"] = _clean_display_text(step.get("content", ""))

        if steps or final_text:
            turn = {
                "role": "assistant",
                "content": final_text,
                "steps": steps,
                "created_at": final_ts or (user_row[1] if user_row else 0),
            }
            if is_evolution:
                turn["kind"] = "evolution"
            if merged_extras:
                turn["extras"] = merged_extras
            if final_seq is not None:
                turn["_seq"] = final_seq
            turns.append(turn)

    return turns


class ConversationStore:
    """
    SQLite-backed store for per-session conversation history.

    Usage:
        store = ConversationStore(db_path)
        store.append_messages("user_123", new_messages, channel_type="feishu")
        msgs = store.load_messages("user_123", max_turns=30)
    """

    def __init__(self, db_path: Path, agent_id: Optional[str] = None):
        self._db_path = db_path
        # The Agent every row read/written through this handle belongs to. The
        # default Agent keeps ``""`` so a single-Agent install never backfills a
        # value and its historical rows stay valid; every other Agent scopes to
        # its own id. All queries are scoped by this so one global file can hold
        # every Agent's transcripts without one Agent seeing another's.
        #
        # ``agent_id=None`` means *unbound*: a handle built directly (tests, or
        # a caller that does not go through ``get_conversation_store``) has no
        # opinion and follows the ambient identity. ``""`` is a binding like any
        # other -- the default Agent -- and must win over the ambient Agent, or
        # a handle for the default Agent would read another Agent's rows the
        # moment one is in scope.
        self._agent_id = agent_id or ""
        self._agent_bound = agent_id is not None
        self._lock = threading.RLock()  # Use RLock to allow reentrant locking
        self._schema_identity: tuple = ()
        # True once the runs table is confirmed present. When it is not, run
        # bookkeeping degrades to a no-op so it can never break a turn or a
        # history query -- runs are auxiliary to conversation storage.
        self._runs_ready = False
        self._init_db()

    def _dimensions(self) -> Dict[str, str]:
        """Ambient identity dimensions with this handle's Agent binding applied.

        Upstream's global model binds one store handle to one Agent: every
        Agent's transcripts share a file and are told apart by ``agent_id``, so
        a *bound* handle -- not the ambient identity -- decides whose rows it
        reads and writes. ``''`` is the default Agent, not "unset", which is why
        a bound value is applied even when empty (``ALWAYS_SCOPED_DIMENSIONS``).

        A handle with no binding at all keeps the fork's ambient rule: it serves
        whichever Agent the runtime identity names (and the default Agent when
        nothing names one). Overwriting that with ``''`` would silently point
        every unbound store -- the fixtures, the migration tools, a single-Agent
        deployment -- at the default Agent's rows only.

        ``owner`` and ``tenant_id`` stay ambient either way: they are per-turn
        filters on the caller's identity rather than properties of the handle.
        """
        dimensions = ambient_dimensions()
        if self._agent_bound:
            dimensions["agent_id"] = self._agent_id
        elif not dimensions.get("agent_id"):
            # No binding and no Agent in scope: drop the key rather than let the
            # always-scoped rule narrow the query to the default Agent's rows.
            # An unbound handle is what migrations and fixtures use to sweep
            # every Agent's rows, and ``''`` there would silently hide them.
            dimensions.pop("agent_id", None)
        return dimensions

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_messages(
        self,
        session_id: str,
        max_turns: int = 30,
        with_authors: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Load the most recent messages for a session, for injection into the LLM.

        ALL message types (user text, assistant tool_use, tool_result) are returned
        in their original JSON form so the LLM can reconstruct the full context.

        max_turns is a *visible-turn* count: we count only user messages whose
        content is actual user text (not tool_result blocks).  This prevents
        tool-heavy sessions from exhausting the turn budget prematurely.

        Args:
            session_id: Unique session identifier.
            max_turns: Maximum number of visible user-assistant turns to keep.
            with_authors: Also report which Agent wrote each message, as an
                ``agent_id`` key. Off by default because the answer is only
                ever "the one Agent in this conversation"; a shared transcript
                is where it matters, and where an Agent reading back its own
                history would otherwise take a colleague's work for its own.

        Returns:
            Chronologically ordered list of message dicts (role, content).
        """
        # Dimension scope: when the caller runs as a verified user/tenant (or as
        # a specific Agent), restore only content in that scope — the same rule
        # ``list_sessions`` applies, so a restored LLM context can never contain
        # another tenant's or user's turns. A dimension that is not in scope
        # (legacy, machine work) is not filtered, preserving the historical
        # behaviour.
        values = self._dimensions()
        with self._lock:
            conn = self._connect()
            try:
                aid = self._agent_id
                # Respect context_start_seq: only load messages at or after the boundary
                ctx_row = conn.execute(
                    "SELECT context_start_seq FROM sessions WHERE agent_id = ? AND session_id = ?",
                    (aid, session_id),
                ).fetchone()
                ctx_start = ctx_row[0] if ctx_row else 0

                columns = "seq, role, content" + (", extras" if with_authors else "")
                scope_sql, scope_params = dimension_clause(values=values)
                rows = conn.execute(
                    f"""
                    SELECT {columns}
                    FROM messages
                    WHERE session_id = ? AND seq >= ?{scope_sql}
                    ORDER BY seq DESC
                    """,
                    (session_id, ctx_start) + scope_params,
                ).fetchall()
            finally:
                conn.close()

        if not rows:
            return []

        authors = {row[0]: self._author_of(row[3]) for row in rows} if with_authors else {}

        visible_turn_seqs: List[int] = []
        for seq, role, raw_content, *_ in rows:
            if role != "user":
                continue
            try:
                content = json.loads(raw_content)
            except Exception:
                content = raw_content
            if _is_visible_user_message(content):
                visible_turn_seqs.append(seq)

        if len(visible_turn_seqs) <= max_turns:
            cutoff_seq = None
        else:
            cutoff_seq = visible_turn_seqs[max_turns - 1]

        result = []
        for seq, role, raw_content, *_ in reversed(rows):
            if cutoff_seq is not None and seq < cutoff_seq:
                continue
            try:
                content = json.loads(raw_content)
            except Exception:
                content = raw_content
            # Strip thinking blocks — they are stored for UI display only
            if role == "assistant" and isinstance(content, list):
                content = [b for b in content if b.get("type") != "thinking"]
            message = {"role": role, "content": content}
            if authors.get(seq):
                message["agent_id"] = authors[seq]
            result.append(message)
        return result

    @staticmethod
    def _author_of(raw_extras: Any) -> str:
        """The Agent stamped on a stored message, if any.

        Absent on every message written by a conversation's own Agent, which is
        all of them until somebody else is invited in.
        """
        if not raw_extras:
            return ""
        try:
            extras = json.loads(raw_extras) if isinstance(raw_extras, str) else raw_extras
        except ValueError:
            return ""
        if not isinstance(extras, dict):
            return ""
        agent_id = extras.get("agent_id")
        return agent_id if isinstance(agent_id, str) else ""

    def append_messages(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        channel_type: str = "",
        create_if_missing: bool = True,
        run_id: Optional[str] = None,
    ) -> bool:
        """
        Append new messages to a session's history.

        Seq numbers continue from the session's current maximum, so
        concurrent callers on distinct sessions never collide.

        Args:
            session_id: Unique session identifier.
            messages: List of message dicts to append.
            channel_type: Source channel (e.g. "feishu", "web", "wechat").
                          Only written on session creation; ignored on update.
            create_if_missing: When False, do nothing if the session row is
                          gone. Callers that already stored the user turn use
                          this so a session deleted mid-run is not recreated
                          from the reply alone.
            run_id: The run these messages belong to. Falls back to the ambient
                          RuntimeIdentity's run id, so callers inside a run do
                          not have to thread it through. A per-message ``run_id``
                          key overrides it for that one message.

        Returns:
            True when the messages were written, False when the session was
            missing and ``create_if_missing`` is False.
        """
        if not messages:
            return False

        if run_id is None:
            from common.utils import current_agent_run_id
            run_id = current_agent_run_id() or ""

        # Dimension scope (task 6.6): stamp new content with every dimension the
        # ambient identity carries, so the composed schema's columns are filled
        # at the write and the read can filter on them. Empty values keep the
        # legacy rows readable (see ``ambient_dimensions``).
        dimensions = self._dimensions()
        owner = dimensions.get("owner", "")
        scope_sql, scope_params = dimension_clause(values=dimensions)
        # The columns are NOT NULL with a ``''`` default, and an unbound handle
        # may carry no ``agent_id`` at all (see ``_dimensions``), so normalize
        # for the INSERTs without widening ``scope_sql`` built above.
        agent_dim = dimensions.get("agent_id", "")
        tenant_dim = dimensions.get("tenant_id", "")

        now = int(time.time())
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    aid = self._agent_id
                    if not create_if_missing:
                        exists = conn.execute(
                            f"SELECT 1 FROM sessions WHERE session_id = ?{scope_sql}",
                            (session_id,) + scope_params,
                        ).fetchone()
                        if not exists:
                            return False
                    # INSERT OR IGNORE creates the row on first visit;
                    # the UPDATE always refreshes last_active.
                    # Avoids ON CONFLICT...DO UPDATE (requires SQLite >= 3.24).
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO sessions
                            (agent_id, session_id, channel_type, owner, tenant_id,
                             created_at, last_active, msg_count)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                        """,
                        (agent_dim, session_id, channel_type, owner,
                         tenant_dim, now, now),
                    )
                    conn.execute(
                        "UPDATE sessions SET last_active = ?"
                        f" WHERE session_id = ?{scope_sql}",
                        (now, session_id) + scope_params,
                    )

                    # Determine starting seq for the new batch.
                    row = conn.execute(
                        "SELECT COALESCE(MAX(seq), -1) FROM messages"
                        f" WHERE session_id = ?{scope_sql}",
                        (session_id,) + scope_params,
                    ).fetchone()
                    next_seq = row[0] + 1

                    for msg in messages:
                        role = msg.get("role", "")
                        content = json.dumps(
                            msg.get("content", ""), ensure_ascii=False
                        )
                        extras_obj = msg.get("extras") or {}
                        extras = json.dumps(extras_obj, ensure_ascii=False) if extras_obj else ""
                        msg_run_id = str(msg.get("run_id") or run_id or "")
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO messages
                                (agent_id, session_id, seq, role, content, created_at,
                                 extras, run_id, owner, tenant_id)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (agent_dim, session_id, next_seq, role,
                             content, now, extras, msg_run_id, owner,
                             tenant_dim),
                        )
                        next_seq += 1

                    conn.execute(
                        """
                        UPDATE sessions
                        SET msg_count = (
                            SELECT COUNT(*) FROM messages
                            WHERE session_id = ?{scope_sql}
                        )
                        WHERE session_id = ?{scope_sql}
                        """.format(scope_sql=scope_sql),
                        (session_id,) + scope_params + (session_id,) + scope_params,
                    )

                    # Auto-generate title from the first visible user message
                    cur_title = conn.execute(
                        "SELECT title FROM sessions"
                        f" WHERE session_id = ?{scope_sql}",
                        (session_id,) + scope_params,
                    ).fetchone()
                    if cur_title and not cur_title[0]:
                        for msg in messages:
                            if msg.get("role") == "user":
                                content = msg.get("content", "")
                                text = _extract_display_text(content)
                                if text:
                                    title = text[:50].split("\n")[0]
                                    conn.execute(
                                        "UPDATE sessions SET title = ?"
                                        f" WHERE session_id = ?{scope_sql}",
                                        (title, session_id) + scope_params,
                                    )
                                    break
                    return True
            finally:
                conn.close()

    def clear_context(self, session_id: str) -> int:
        """
        Set the context boundary to after the current last message.
        Messages before this boundary are still stored but excluded from LLM context.

        Returns the new context_start_seq value.
        """
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    aid = self._agent_id
                    row = conn.execute(
                        "SELECT COALESCE(MAX(seq), -1) FROM messages WHERE agent_id = ? AND session_id = ?",
                        (aid, session_id),
                    ).fetchone()
                    new_start = row[0] + 1
                    conn.execute(
                        "UPDATE sessions SET context_start_seq = ? WHERE agent_id = ? AND session_id = ?",
                        (new_start, aid, session_id),
                    )
                    return new_start
            finally:
                conn.close()

    def get_context_start_seq(self, session_id: str) -> int:
        """Return the context_start_seq for a session (0 if not set)."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT context_start_seq FROM sessions WHERE agent_id = ? AND session_id = ?",
                    (self._agent_id, session_id),
                ).fetchone()
                return row[0] if row else 0
            finally:
                conn.close()

    def get_latest_pair_seqs(self, session_id: str) -> Dict[str, Optional[int]]:
        """Return the seq numbers of the latest visible user message and the
        latest assistant message in a session.

        A "visible" user message is one whose content is real user text
        (not just a tool_result block), so tool-execution turns do not
        shadow the actual user query.

        Returns:
            Dict with keys ``user_seq`` and ``bot_seq``; either may be None
            when no matching message exists.
        """
        result: Dict[str, Optional[int]] = {"user_seq": None, "bot_seq": None}
        with self._lock:
            conn = self._connect()
            try:
                aid = self._agent_id
                # Latest assistant message (cheap: single row by seq DESC).
                row = conn.execute(
                    "SELECT seq FROM messages "
                    "WHERE agent_id = ? AND session_id = ? AND role = 'assistant' "
                    "ORDER BY seq DESC LIMIT 1",
                    (aid, session_id),
                ).fetchone()
                if row:
                    result["bot_seq"] = int(row[0])

                # Latest visible user message: scan recent user rows and
                # skip pure tool_result entries.
                rows = conn.execute(
                    "SELECT seq, content FROM messages "
                    "WHERE agent_id = ? AND session_id = ? AND role = 'user' "
                    "ORDER BY seq DESC LIMIT 20",
                    (aid, session_id),
                ).fetchall()
                for seq, content_raw in rows:
                    try:
                        content = json.loads(content_raw)
                    except Exception:
                        result["user_seq"] = int(seq)
                        break
                    if isinstance(content, list):
                        has_text = any(
                            isinstance(b, dict) and b.get("type") == "text"
                            for b in content
                        )
                        has_tool_result = any(
                            isinstance(b, dict) and b.get("type") == "tool_result"
                            for b in content
                        )
                        if has_text and not has_tool_result:
                            result["user_seq"] = int(seq)
                            break
                    else:
                        result["user_seq"] = int(seq)
                        break
            finally:
                conn.close()
        return result

    def clear_session(self, session_id: str) -> None:
        """Delete all messages and the session record for a given session_id."""
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    aid = self._agent_id
                    conn.execute(
                        "DELETE FROM messages WHERE agent_id = ? AND session_id = ?",
                        (aid, session_id),
                    )
                    conn.execute(
                        "DELETE FROM sessions WHERE agent_id = ? AND session_id = ?",
                        (aid, session_id),
                    )
            finally:
                conn.close()

    def delete_message_pair(self, session_id: str, user_seq: int, delete_user: bool = True, cascade: bool = False) -> int:
        """Delete a user message and/or its corresponding assistant reply.

        The assistant reply is identified as all messages between user_seq
        and the next visible user message (or end of session).

        Args:
            session_id: Session identifier.
            user_seq: The seq number of the user message.
            delete_user: If True (default), delete the user message too.
                        If False, only delete assistant reply (for regenerate scenarios).
            cascade: If True, also delete all subsequent turns after this one.
                    Used by edit-message which removes this turn and everything after.

        Returns:
            Number of message rows deleted.
        """
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    aid = self._agent_id
                    # Verify this is a user message
                    row = conn.execute(
                        "SELECT role FROM messages WHERE agent_id = ? AND session_id = ? AND seq = ?",
                        (aid, session_id, user_seq),
                    ).fetchone()
                    if not row or row[0] != "user":
                        return 0

                    if cascade:
                        # Delete from this message to end of session
                        start_seq = user_seq if delete_user else user_seq + 1
                        end_seq_row = conn.execute(
                            "SELECT MAX(seq) FROM messages WHERE agent_id = ? AND session_id = ?",
                            (aid, session_id),
                        ).fetchone()
                        end_seq = (end_seq_row[0] or user_seq) + 1
                    else:
                        # Find the next visible user message seq (exclude tool_result)
                        # Use batched query to avoid loading too many rows at once
                        next_user_seq = None
                        batch_size = 100
                        offset = 0
                        while True:
                            batch = conn.execute(
                                """
                                SELECT seq, content FROM messages
                                WHERE agent_id = ? AND session_id = ? AND seq > ? AND role = 'user'
                                ORDER BY seq ASC
                                LIMIT ? OFFSET ?
                                """,
                                (aid, session_id, user_seq, batch_size, offset),
                            ).fetchall()
                            if not batch:
                                break
                            for seq, content in batch:
                                try:
                                    content_obj = json.loads(content)
                                except Exception:
                                    content_obj = content
                                if _is_visible_user_message(content_obj):
                                    next_user_seq = seq
                                    break
                            if next_user_seq is not None:
                                break
                            offset += batch_size

                        # Determine the end boundary for deletion
                        if next_user_seq is not None:
                            end_seq = next_user_seq
                        else:
                            end_seq_row = conn.execute(
                                "SELECT MAX(seq) FROM messages WHERE agent_id = ? AND session_id = ?",
                                (aid, session_id),
                            ).fetchone()
                            end_seq = (end_seq_row[0] or user_seq) + 1

                        # Determine the start boundary for deletion
                        start_seq = user_seq if delete_user else user_seq + 1

                    # Delete messages from start_seq to end_seq (exclusive)
                    cur = conn.execute(
                        "DELETE FROM messages WHERE agent_id = ? AND session_id = ? AND seq >= ? AND seq < ?",
                        (aid, session_id, start_seq, end_seq),
                    )
                    deleted = cur.rowcount

                    # Update session msg_count
                    conn.execute(
                        """
                        UPDATE sessions
                        SET msg_count = (
                            SELECT COUNT(*) FROM messages
                            WHERE agent_id = ? AND session_id = ?
                        )
                        WHERE agent_id = ? AND session_id = ?
                        """,
                        (aid, session_id, aid, session_id),
                    )

                    return deleted
            finally:
                conn.close()

    def prune_scheduled_messages(
        self,
        session_id: str,
        keep_last_n: int,
        markers: Optional[List[str]] = None,
    ) -> int:
        """
        Keep at most ``keep_last_n`` scheduler-injected user/assistant pairs in
        the session, deleting the older ones.

        A scheduler-injected pair is identified by a user message whose first
        text block starts with one of ``markers``; the immediately following
        assistant message (next seq) is treated as its paired output.

        Only scheduler-tagged messages are touched; regular user turns are
        never deleted. Safe to call repeatedly; no-op if nothing to prune.

        Args:
            session_id: Session to prune.
            keep_last_n: Maximum scheduler pairs to retain (must be >= 0).
            markers: Text prefixes that identify scheduler user messages.
                Defaults to ``["[SCHEDULED]", "Scheduled task"]`` so that
                pairs written by older versions are also recognised.

        Returns:
            Number of message rows deleted.
        """
        if keep_last_n < 0:
            keep_last_n = 0
        if markers is None:
            markers = ["[SCHEDULED]", "Scheduled task"]

        def _matches_marker(raw_content: str) -> bool:
            try:
                parsed = json.loads(raw_content)
            except Exception:
                parsed = raw_content
            text = _extract_display_text(parsed) if not isinstance(parsed, str) else parsed
            if not text:
                return False
            return any(text.startswith(m) for m in markers)

        with self._lock:
            conn = self._connect()
            try:
                aid = self._agent_id
                rows = conn.execute(
                    """
                    SELECT seq, role, content
                    FROM messages
                    WHERE agent_id = ? AND session_id = ?
                    ORDER BY seq ASC
                    """,
                    (aid, session_id),
                ).fetchall()

                # Find scheduler pairs: each is (user_seq, assistant_seq?)
                pairs: List[tuple] = []  # list of (user_seq, assistant_seq_or_None)
                for idx, (seq, role, raw_content) in enumerate(rows):
                    if role != "user" or not _matches_marker(raw_content):
                        continue
                    assistant_seq = None
                    # Pair with the very next message if it's an assistant turn.
                    if idx + 1 < len(rows):
                        next_seq, next_role, _ = rows[idx + 1]
                        if next_role == "assistant":
                            assistant_seq = next_seq
                    pairs.append((seq, assistant_seq))

                if len(pairs) <= keep_last_n:
                    return 0

                to_delete_pairs = pairs[: len(pairs) - keep_last_n]
                seqs_to_delete: List[int] = []
                for user_seq, assistant_seq in to_delete_pairs:
                    seqs_to_delete.append(user_seq)
                    if assistant_seq is not None:
                        seqs_to_delete.append(assistant_seq)

                if not seqs_to_delete:
                    return 0

                placeholders = ",".join("?" * len(seqs_to_delete))
                with conn:
                    conn.execute(
                        f"DELETE FROM messages WHERE agent_id = ? AND session_id = ? AND seq IN ({placeholders})",
                        (aid, session_id, *seqs_to_delete),
                    )
                    conn.execute(
                        """
                        UPDATE sessions
                        SET msg_count = (
                            SELECT COUNT(*) FROM messages
                            WHERE agent_id = ? AND session_id = ?
                        )
                        WHERE agent_id = ? AND session_id = ?
                        """,
                        (aid, session_id, aid, session_id),
                    )
                return len(seqs_to_delete)
            finally:
                conn.close()

    def cleanup_old_sessions(self, max_age_days: Optional[int] = None) -> int:
        """
        Delete sessions that have not been active within max_age_days.
        Web channel sessions are excluded — they are meant to be permanent.

        Args:
            max_age_days: Override the default retention period.

        Returns:
            Number of sessions deleted.
        """
        try:
            from config import conf
            max_age = max_age_days or conf().get(
                "conversation_max_age_days", DEFAULT_MAX_AGE_DAYS
            )
        except Exception:
            max_age = max_age_days or DEFAULT_MAX_AGE_DAYS

        cutoff = int(time.time()) - max_age * 86400
        deleted = 0

        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    aid = self._agent_id
                    stale = conn.execute(
                        "SELECT session_id FROM sessions "
                        "WHERE agent_id = ? AND last_active < ? AND channel_type != 'web'",
                        (aid, cutoff),
                    ).fetchall()
                    for (sid,) in stale:
                        conn.execute(
                            "DELETE FROM messages WHERE agent_id = ? AND session_id = ?",
                            (aid, sid),
                        )
                        conn.execute(
                            "DELETE FROM sessions WHERE agent_id = ? AND session_id = ?",
                            (aid, sid),
                        )
                        deleted += 1
            finally:
                conn.close()

        if deleted:
            logger.info(f"[ConversationStore] Pruned {deleted} expired sessions")
        return deleted

    def attach_extras_to_last_assistant(
        self,
        session_id: str,
        extras: Dict[str, Any],
    ) -> Optional[int]:
        """
        Merge ``extras`` into the latest assistant message of a session.

        Used by post-processing (e.g. TTS) that needs to annotate an already
        persisted bot reply with attachments such as audio URLs.

        Returns the message seq that was updated, or ``None`` if no assistant
        message exists or the update could not be applied.
        """
        if not extras:
            return None
        with self._lock:
            conn = self._connect()
            try:
                aid = self._agent_id
                row = conn.execute(
                    """
                    SELECT seq, extras FROM messages
                    WHERE agent_id = ? AND session_id = ? AND role = 'assistant'
                    ORDER BY seq DESC LIMIT 1
                    """,
                    (aid, session_id),
                ).fetchone()
                if not row:
                    return None
                seq, raw = row
                try:
                    cur = json.loads(raw) if raw else {}
                    if not isinstance(cur, dict):
                        cur = {}
                except Exception:
                    cur = {}
                cur.update(extras)
                conn.execute(
                    "UPDATE messages SET extras = ? WHERE agent_id = ? AND session_id = ? AND seq = ?",
                    (json.dumps(cur, ensure_ascii=False), aid, session_id, seq),
                )
                conn.commit()
                return seq
            except Exception as e:
                logger.warning(f"[ConversationStore] attach_extras failed: {e}")
                return None
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Runs: addressable, persisted units of work
    # ------------------------------------------------------------------

    @staticmethod
    def _run_row_to_dict(row: tuple) -> Dict[str, Any]:
        (
            run_id, agent_id, user_id, session_id, parent_run_id,
            task_id, task_source, status, started_at, ended_at, error, raw_extras,
        ) = row
        try:
            extras = json.loads(raw_extras) if raw_extras else {}
            if not isinstance(extras, dict):
                extras = {}
        except Exception:
            extras = {}
        return {
            "run_id": run_id,
            "agent_id": agent_id,
            "user_id": user_id,
            "session_id": session_id,
            "parent_run_id": parent_run_id,
            "task_id": task_id,
            "task_source": task_source,
            "status": status,
            "started_at": started_at,
            "ended_at": ended_at,
            "error": error,
            "extras": extras,
        }

    _RUN_COLUMNS = (
        "run_id, agent_id, user_id, session_id, parent_run_id, "
        "task_id, task_source, status, started_at, ended_at, error, extras"
    )

    def create_run(
        self,
        run_id: str,
        *,
        agent_id: str = "",
        user_id: str = "",
        session_id: str = "",
        parent_run_id: str = "",
        task_id: str = "",
        task_source: str = "",
        status: str = "running",
        extras: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Record the start of a run. Idempotent: a second call with the same
        run_id is ignored so a retried entry point does not duplicate the row.

        Returns True when a new row was written, False when it already existed.
        """
        if not run_id:
            raise ValueError("run_id is required")
        if not self._runs_ready:
            return False
        now = int(time.time())
        extras_json = (
            json.dumps(extras, ensure_ascii=False) if extras else ""
        )
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO runs
                            (run_id, agent_id, user_id, session_id, parent_run_id,
                             task_id, task_source, status, started_at, ended_at,
                             error, extras)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '', ?)
                        """,
                        (
                            run_id, agent_id, user_id, session_id, parent_run_id,
                            task_id, task_source, status, now, extras_json,
                        ),
                    )
                    return cur.rowcount > 0
            finally:
                conn.close()

    def finish_run(
        self,
        run_id: str,
        status: str = "done",
        error: str = "",
        extras: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Mark a run finished (or failed). Sets ended_at and, when given,
        merges ``extras`` into the stored sidecar. Returns True if the run
        existed.
        """
        if not run_id or not self._runs_ready:
            return False
        now = int(time.time())
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    if extras:
                        row = conn.execute(
                            "SELECT extras FROM runs WHERE run_id = ?", (run_id,)
                        ).fetchone()
                        if row is None:
                            return False
                        try:
                            cur_extras = json.loads(row[0]) if row[0] else {}
                            if not isinstance(cur_extras, dict):
                                cur_extras = {}
                        except Exception:
                            cur_extras = {}
                        cur_extras.update(extras)
                        extras_json = json.dumps(cur_extras, ensure_ascii=False)
                        cur = conn.execute(
                            """
                            UPDATE runs
                            SET status = ?, error = ?, ended_at = ?, extras = ?
                            WHERE run_id = ?
                            """,
                            (status, error, now, extras_json, run_id),
                        )
                    else:
                        cur = conn.execute(
                            """
                            UPDATE runs
                            SET status = ?, error = ?, ended_at = ?
                            WHERE run_id = ?
                            """,
                            (status, error, now, run_id),
                        )
                    return cur.rowcount > 0
            finally:
                conn.close()

    def update_run_extras(self, run_id: str, extras: Dict[str, Any]) -> bool:
        """Merge keys into a run's sidecar without touching its lifecycle.

        Lets an observer attach a payload to a run it did not execute, so the
        status stays owned by whoever actually ran the work. Returns True if
        the run existed.
        """
        if not run_id or not extras or not self._runs_ready:
            return False
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    row = conn.execute(
                        "SELECT extras FROM runs WHERE run_id = ?", (run_id,)
                    ).fetchone()
                    if row is None:
                        return False
                    try:
                        merged = json.loads(row[0]) if row[0] else {}
                        if not isinstance(merged, dict):
                            merged = {}
                    except Exception:
                        merged = {}
                    merged.update(extras)
                    conn.execute(
                        "UPDATE runs SET extras = ? WHERE run_id = ?",
                        (json.dumps(merged, ensure_ascii=False), run_id),
                    )
                    return True
            finally:
                conn.close()

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Return a single run by id, or None."""
        if not run_id or not self._runs_ready:
            return None
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    f"SELECT {self._RUN_COLUMNS} FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            finally:
                conn.close()
        return self._run_row_to_dict(row) if row else None

    def list_runs(
        self,
        session_id: Optional[str] = None,
        parent_run_id: Optional[str] = None,
        task_source: Optional[str] = None,
        task_id: Optional[str] = None,
        status: Optional[str] = None,
        agent_id: Optional[str] = None,
        since: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List runs, newest first, filtered by any combination of the given
        dimensions. ``parent_run_id=''`` selects top-level runs only.

        ``since`` keeps only runs that *started strictly after* the given epoch
        seconds; it powers the client's cross-session scheduler poll, which asks
        "any new executions since I last checked?" without re-fetching history.

        ``offset`` skips the first N rows for paging the history list ("load
        more"). Combined with the stable ``started_at DESC, run_id DESC`` order
        it gives deterministic pages.

        Unlike the conversation methods this is intentionally *not* scoped to the
        store's bound ``self._agent_id``: the runs table is one global ledger and
        the console shows the whole team's activity by default. Pass an explicit
        ``agent_id`` to scope to a single Agent (``''`` selects the default
        Agent's own rows).
        """
        if not self._runs_ready:
            return []
        clauses: List[str] = []
        params: List[Any] = []
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if parent_run_id is not None:
            clauses.append("parent_run_id = ?")
            params.append(parent_run_id)
        if task_source is not None:
            clauses.append("task_source = ?")
            params.append(task_source)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if since is not None:
            clauses.append("started_at > ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, limit))
        params.append(max(0, offset))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT {self._RUN_COLUMNS} FROM runs {where} "
                    "ORDER BY started_at DESC, run_id DESC LIMIT ? OFFSET ?",
                    tuple(params),
                ).fetchall()
            finally:
                conn.close()
        return [self._run_row_to_dict(r) for r in rows]

    def delete_run(self, run_id: str, agent_id: Optional[str] = None) -> bool:
        """Delete a single run row. Returns True if a row was removed.

        Only touches the ``runs`` ledger entry (the history-list item); the
        delivered message persisted in the session history is left intact. When
        ``agent_id`` is given the delete is scoped to that Agent so one Agent
        cannot remove another's run by id alone.
        """
        if not self._runs_ready or not run_id:
            return False
        clauses = ["run_id = ?"]
        params: List[Any] = [run_id]
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        where = " AND ".join(clauses)
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(f"DELETE FROM runs WHERE {where}", tuple(params))
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def get_run_detail(self, run_id: str) -> Optional[Dict[str, Any]]:
        """A run plus the full text it delivered, for a history detail view.

        The runs row only keeps a short ``output_preview`` (a list-view index).
        The complete delivered message lives in the *receiver's session*, which
        the scheduler threads the push into (``_remember_delivered_output``), so
        this joins back to that session to recover the full body.

        Correlation is by ``(session_id, agent_id)`` from the run, then the
        scheduler-injected assistant message whose ``created_at`` is closest at
        or after the run's ``started_at`` (the pair this run wrote). It is
        best-effort: the session copy is itself capped (~2000 chars) and old
        scheduler pairs are pruned, so ``full_output`` may be ``None`` — the
        caller falls back to the preview. Messages are scoped to the *run's*
        ``agent_id``, not this store's bound id, since a run may belong to any
        Agent in the global ledger.
        """
        run = self.get_run(run_id)
        if run is None:
            return None

        detail = dict(run)
        extras = detail.pop("extras", {}) or {}
        detail["task_name"] = extras.get("task_name", "")
        detail["action_type"] = extras.get("action_type", "")
        detail["channel_type"] = extras.get("channel_type", "")
        detail["instance_id"] = extras.get("instance_id", "")
        detail["trigger"] = extras.get("trigger", "")
        detail["output_preview"] = extras.get("output_preview", "")
        detail["full_output"] = None

        session_id = run.get("session_id") or ""
        if not session_id:
            return detail

        run_agent_id = run.get("agent_id") or ""
        started_at = run.get("started_at") or 0
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT seq, role, content, created_at
                    FROM messages
                    WHERE agent_id = ? AND session_id = ?
                    ORDER BY seq ASC
                    """,
                    (run_agent_id, session_id),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            finally:
                conn.close()

        # Assistant messages that answer a [SCHEDULED] user turn are the delivered
        # bodies. Pick the one written closest at/after this run started; fall
        # back to the newest scheduler body if none lands after the timestamp
        # (clock skew, or the pair persisted a hair before the run row).
        candidates: List[tuple] = []  # (created_at, text)
        for i, (seq, role, raw, created_at) in enumerate(rows):
            if role != "user":
                continue
            if not _message_is_scheduled_marker(raw):
                continue
            if i + 1 < len(rows) and rows[i + 1][1] == "assistant":
                text = _message_plain_text(rows[i + 1][2])
                if text:
                    candidates.append((rows[i + 1][3] or 0, text))

        if candidates:
            after = [c for c in candidates if c[0] >= started_at]
            chosen = min(after, key=lambda c: c[0]) if after else max(
                candidates, key=lambda c: c[0]
            )
            detail["full_output"] = chosen[1]
        return detail

    def load_history_page(
        self,
        session_id: str,
        page: int = 1,
        page_size: int = 20,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Load a page of conversation history for UI display, grouped into turns.

        Each "turn" maps to one of:
          - A user message (role="user", content=str)
          - An assistant message (role="assistant", content=str,
            tool_calls=[{name, arguments, result}] when tools were used)

        Internal tool_result user messages are merged into the preceding
        assistant entry's tool_calls list and never appear as standalone items.

        Pages are numbered from 1 (most recent).  Messages within a page are
        returned in chronological order.

        Returns:
            {
                "messages": [
                    {
                        "role": "user" | "assistant",
                        "content": str,
                        "tool_calls": [...],   # assistant only, may be []
                        "created_at": int,
                    },
                    ...
                ],
                "total": <visible turn count>,
                "page": <current page>,
                "page_size": <page_size>,
                "has_more": bool,
            }
        """
        page = max(1, page)
        with self._lock:
            conn = self._connect()
            try:
                aid = self._agent_id
                ctx_row = conn.execute(
                    "SELECT context_start_seq FROM sessions WHERE agent_id = ? AND session_id = ?",
                    (aid, session_id),
                ).fetchone()
                ctx_start = ctx_row[0] if ctx_row else 0

                # extras column is added by migration; tolerate older DBs that
                # might miss it by falling back to a NULL literal.
                # The Agent comes from this handle's binding and the tenant
                # from the ambient request scope; the explicit ``user_id`` keeps
                # its historical meaning of "this is the owner filter the UI
                # asked for", so an unscoped internal read is not silently
                # narrowed to the empty owner.
                scope_sql, scope_params = dimension_clause(
                    values=self._dimensions(), keys=("agent_id", "tenant_id"))
                if user_id:
                    msg_where = f"WHERE session_id = ? AND owner = ?{scope_sql}"
                    msg_args = (session_id, user_id) + scope_params
                else:
                    msg_where = f"WHERE session_id = ?{scope_sql}"
                    msg_args = (session_id,) + scope_params
                try:
                    rows = conn.execute(
                        f"""
                        SELECT seq, role, content, created_at, extras
                        FROM messages
                        {msg_where}
                        ORDER BY seq ASC
                        """,
                        msg_args,
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = [
                        (seq, role, content, created_at, "")
                        for (seq, role, content, created_at) in conn.execute(
                            f"""
                            SELECT seq, role, content, created_at
                            FROM messages
                            {msg_where}
                            ORDER BY seq ASC
                            """,
                            msg_args,
                        ).fetchall()
                    ]
            finally:
                conn.close()

        # Honour the current enable_thinking switch when building display turns
        # so that toggling it off hides previously-saved thinking blocks too.
        try:
            from config import conf
            include_thinking = bool(conf().get("enable_thinking", False))
        except Exception:
            include_thinking = False

        visible = _group_into_display_turns(rows, include_thinking=include_thinking)

        total = len(visible)
        offset = (page - 1) * page_size
        page_items = list(reversed(visible))[offset: offset + page_size]
        page_items = list(reversed(page_items))

        return {
            "messages": page_items,
            "context_start_seq": ctx_start,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": offset + page_size < total,
        }

    def get_session_owner(self, session_id: str) -> Optional[str]:
        """The verified user a session belongs to, or None when unknown.

        ``None`` covers both "no such session yet" (a brand-new conversation
        before its first message) and "legacy, owner-less" — a caller must
        distinguish them only if it is safe to assume ownership in either case.
        Used to decide whether a per-user segment may enter a session's prompt.
        """
        with self._lock:
            conn = self._connect()
            try:
                # Scoped to this handle's Agent: every Agent's conversations now
                # share one file, so the same session_id can exist for more than
                # one of them and an unscoped read would answer for the wrong
                # one. ``owner`` is deliberately *not* a filter here -- this
                # method is what callers use to establish ownership, so scoping
                # by it would be circular.
                clause, params = dimension_clause(
                    values=self._dimensions(), keys=("agent_id",))
                row = conn.execute(
                    f"SELECT owner FROM sessions WHERE session_id=?{clause}",
                    (session_id, *params),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        return row[0] or None

    def list_sessions(
        self,
        channel_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
        user_id: Optional[str] = None,
        q: str = "",
        archived: bool = False,
    ) -> Dict[str, Any]:
        """
        List sessions with pinned ones first, then last_active DESC, with an
        optional channel_type filter and literal title substring search.

        Archived sessions are soft-hidden: the default read (``archived=False``)
        excludes them, and ``archived=True`` is the archived view used to
        restore them. The filter is applied before counting and pagination.

        Title matching ignores ASCII letter case. Search uses ``instr`` rather
        than LIKE so percent signs and underscores remain literal characters.
        Ownership and search apply before both counting and pagination.

        Pinned sessions sort ahead of everything else rather than only ahead of
        the rows on the same page, so a pin still reaches the top of the list
        when the conversation is old enough to sit several pages down.

        Returns:
            {
                "sessions": [{session_id, title, created_at, last_active,
                              msg_count, pinned}, ...],
                "total": int,
                "page": int,
                "page_size": int,
                "has_more": bool,
            }
        """
        page = max(1, page)
        q = normalize_session_search_query(q)
        clauses = ["owner = ?"]
        params: List[Any] = [user_id or ""]
        scope_sql, scope_params = dimension_clause(
            values=self._dimensions(), keys=("agent_id", "tenant_id"))
        if scope_sql:
            # strip the leading " AND " so it joins the clause list
            clauses.append(scope_sql[len(" AND "):])
            params.extend(scope_params)
        if channel_type:
            clauses.append("channel_type = ?")
            params.append(channel_type)
        clauses.append("archived = ?")
        params.append(1 if archived else 0)
        if q:
            clauses.append("instr(lower(title), lower(?)) > 0")
            params.append(q)
        where = " AND ".join(clauses)
        with self._lock:
            conn = self._connect()
            try:
                total = conn.execute(
                    f"SELECT COUNT(*) FROM sessions WHERE {where}", params,
                ).fetchone()[0]
                rows = conn.execute(
                    f"""
                    SELECT session_id, title, created_at, last_active, msg_count, pinned
                    FROM sessions
                    WHERE {where}
                    ORDER BY pinned DESC, last_active DESC
                    LIMIT ? OFFSET ?
                    """,
                    [*params, page_size, (page - 1) * page_size],
                ).fetchall()
            finally:
                conn.close()

        sessions = [
            {
                "session_id": r[0],
                "title": r[1],
                "created_at": r[2],
                "last_active": r[3],
                "msg_count": r[4],
                "pinned": bool(r[5]),
            }
            for r in rows
        ]
        return {
            "sessions": sessions,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": (page - 1) * page_size + page_size < total,
        }

    def rename_session(self, session_id: str, title: str) -> bool:
        """Update the title of a session. Returns True if the session existed."""
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "UPDATE sessions SET title = ? WHERE agent_id = ? AND session_id = ?",
                        (title, self._agent_id, session_id),
                    )
                    return cur.rowcount > 0
            finally:
                conn.close()

    def backfill_owner(self, owner: str) -> int:
        """Assign ``owner`` to every owner-less session/message row (task 4.1).

        Runs once during the maintenance-window migration on an existing store:
        pre-isolation content is in-place registered to the default tenant's
        initial admin. Idempotent — rows that already have a non-empty owner are
        left untouched, so re-running does not overwrite later splits.
        Returns the number of sessions updated.
        """
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "UPDATE sessions SET owner = ? WHERE owner = ''",
                        (owner,),
                    )
                    conn.execute(
                        "UPDATE messages SET owner = ? WHERE owner = ''",
                        (owner,),
                    )
                    return cur.rowcount
            finally:
                conn.close()

    def backfill_tenant(self, tenants_for_owner) -> Dict[str, int]:
        """Attribute owner-known rows to a tenant, from the owner's membership.

        Task 6.8. The tenant of a pre-isolation conversation is *derived* from
        the person it belongs to, never invented:

        * only rows with an empty ``tenant_id`` and a non-empty ``owner`` are
          candidates (a row with no owner has no derivable tenant);
        * ``tenants_for_owner(owner)`` must return exactly one tenant id, or
          ``None``/empty when the owner has no membership or several. An
          ambiguous answer leaves the row empty rather than picking a tenant,
          so the row stays unreadable to every tenant scope instead of leaking
          into an arbitrary one;
        * idempotent: a second pass finds only the still-unattributed rows.

        Returns ``{"sessions": n, "messages": n, "unresolved": n}`` where
        ``unresolved`` counts the *owners* (not rows) that could not be mapped.
        """
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    owners = [
                        r[0] for r in conn.execute(
                            "SELECT DISTINCT owner FROM sessions"
                            " WHERE tenant_id = '' AND owner != ''"
                        ).fetchall()
                    ]
                    resolved: Dict[str, str] = {}
                    unresolved = 0
                    for owner in owners:
                        tenant = tenants_for_owner(owner)
                        if isinstance(tenant, str) and tenant:
                            resolved[owner] = tenant
                        else:
                            unresolved += 1

                    sessions = messages = 0
                    for owner, tenant in resolved.items():
                        sessions += conn.execute(
                            "UPDATE sessions SET tenant_id = ?"
                            " WHERE tenant_id = '' AND owner = ?",
                            (tenant, owner),
                        ).rowcount
                        messages += conn.execute(
                            "UPDATE messages SET tenant_id = ?"
                            " WHERE tenant_id = '' AND owner = ?",
                            (tenant, owner),
                        ).rowcount
                    return {
                        "sessions": sessions,
                        "messages": messages,
                        "unresolved": unresolved,
                    }
            finally:
                conn.close()

    def set_pinned(self, session_id: str, pinned: bool) -> bool:
        """Pin or unpin a session. Returns True if the session existed."""
        scope_sql, scope_params = dimension_clause(
            values=self._dimensions(), keys=("agent_id", "tenant_id"))
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "UPDATE sessions SET pinned = ?"
                        f" WHERE session_id = ?{scope_sql}",
                        (1 if pinned else 0, session_id) + scope_params,
                    )
                    return cur.rowcount > 0
            finally:
                conn.close()

    def set_archived(self, session_id: str, archived: bool) -> bool:
        """Soft-hide or restore a session. Returns True if it existed.

        Archiving only flips the flag: messages, title, owner, project binding
        and the pin are left untouched, so restoring returns the conversation
        exactly as it was. Agent/tenant scoping mirrors :meth:`set_pinned`, so
        a caller cannot address another tenant's session.
        """
        scope_sql, scope_params = dimension_clause(
            values=self._dimensions(), keys=("agent_id", "tenant_id"))
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "UPDATE sessions SET archived = ?"
                        f" WHERE session_id = ?{scope_sql}",
                        (1 if archived else 0, session_id) + scope_params,
                    )
                    return cur.rowcount > 0
            finally:
                conn.close()

    def list_session_ids(self, channel_type: Optional[str] = None,
                         user_id: Optional[str] = None,
                         archived: bool = False) -> List[str]:
        """Every session id, optionally filtered by channel and/or owner.

        One cheap single-column scan, used to work out how many distinct project
        spaces are actually in play without paging through full session rows.
        Archived sessions are excluded by default so a hidden conversation does
        not keep inflating the space count that decides how the list groups.
        """
        scope_sql, scope_params = dimension_clause(
            values=self._dimensions(), keys=("agent_id", "tenant_id"))
        archived_param = 1 if archived else 0
        with self._lock:
            conn = self._connect()
            try:
                if channel_type:
                    rows = conn.execute(
                        "SELECT session_id FROM sessions"
                        f" WHERE channel_type = ? AND owner = ? AND archived = ?{scope_sql}",
                        (channel_type, user_id or "", archived_param) + scope_params,
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"SELECT session_id FROM sessions WHERE owner = ? AND archived = ?{scope_sql}",
                        (user_id or "", archived_param) + scope_params,
                    ).fetchall()
            finally:
                conn.close()
        return [r[0] for r in rows]

    def get_stats(self) -> Dict[str, Any]:
        """Return basic stats keyed by channel_type, for monitoring."""
        with self._lock:
            conn = self._connect()
            try:
                aid = self._agent_id
                total_sessions = conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE agent_id = ?", (aid,)
                ).fetchone()[0]
                total_messages = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE agent_id = ?", (aid,)
                ).fetchone()[0]
                by_channel = conn.execute(
                    """
                    SELECT channel_type, COUNT(*) as cnt
                    FROM sessions
                    WHERE agent_id = ?
                    GROUP BY channel_type
                    ORDER BY cnt DESC
                    """,
                    (aid,),
                ).fetchall()
                return {
                    "total_sessions": total_sessions,
                    "total_messages": total_messages,
                    "by_channel": {row[0] or "unknown": row[1] for row in by_channel},
                }
            finally:
                conn.close()

    def count_messages_between(self, start_ts: int, end_ts: int) -> int:
        """Count messages with created_at in [start_ts, end_ts)."""
        start_ts = int(start_ts)
        end_ts = int(end_ts)
        if end_ts <= start_ts:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE created_at >= ? AND created_at < ?",
                    (start_ts, end_ts),
                ).fetchone()
                return int(row[0] or 0)
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._raw_connect()
        try:
            # Core tables first and unguarded: if these cannot be created the
            # store is genuinely unusable and the error should surface.
            # Tables, then the migration, then the indexes: a database that
            # predates a dimension cannot have that dimension's indexes created
            # until the migration has added its columns.
            conn.executescript(schema_seam.build_table_ddl())
            conn.commit()
            self._migrate(conn)
            conn.executescript(schema_seam.build_index_ddl())
            conn.commit()
            # Runs are auxiliary. Their setup is isolated so a legacy table, a
            # half-applied upgrade or any other surprise degrades run tracking
            # instead of taking conversation history offline.
            self._init_runs(conn)
        finally:
            conn.close()
        self._schema_identity = self._db_identity()

    def _init_runs(self, conn: sqlite3.Connection) -> None:
        """Create the runs table without ever risking the core schema."""
        try:
            self._retire_incompatible_runs_table(conn)
            conn.executescript(_RUNS_DDL)
            conn.commit()
            self._runs_ready = True
        except Exception as e:
            self._runs_ready = False
            logger.warning(
                f"[ConversationStore] Run tracking unavailable ({e}); "
                "conversation history is unaffected"
            )
            try:
                conn.rollback()
            except Exception:
                pass

    def _retire_incompatible_runs_table(self, conn: sqlite3.Connection) -> None:
        """Move aside a pre-existing ``runs`` table that predates this schema.

        An earlier, since-removed feature shipped a differently shaped ``runs``
        table in the same file. Its columns do not match the one the current
        code owns, so ``CREATE TABLE IF NOT EXISTS`` leaves the old table in
        place and a later ``CREATE INDEX`` on a column it lacks aborts the whole
        init script -- which takes every conversation query down with it. The
        old table is renamed rather than dropped so its rows stay recoverable,
        and the marker column check makes this a no-op on both the current
        schema and a database that never had the legacy table.
        """
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'"
            ).fetchone()
            if not exists:
                return
            cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
            if "task_source" in cols:
                return
            backup = "runs_legacy_backup"
            # Never clobber an earlier backup; keep the current file untouched
            # if one is already parked there.
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (backup,),
            ).fetchone():
                backup = f"runs_legacy_backup_{int(time.time())}"
            # Indexes on the old table would otherwise collide with the new
            # ones the DDL is about to create.
            for (idx_name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='runs' AND name NOT LIKE 'sqlite_%'"
            ).fetchall():
                conn.execute(f'DROP INDEX IF EXISTS "{idx_name}"')
            conn.execute(f"ALTER TABLE runs RENAME TO {backup}")
            conn.commit()
            logger.warning(
                "[ConversationStore] Renamed a legacy runs table to "
                f"{backup}; its rows are preserved there"
            )
        except Exception as e:
            logger.warning(
                f"[ConversationStore] Could not retire legacy runs table: {e}"
            )

    def _db_identity(self) -> tuple:
        """Identify the physical file behind _db_path, or () when it is missing."""
        try:
            st = self._db_path.stat()
        except OSError:
            return ()
        return (st.st_dev, st.st_ino)

    def _ensure_schema(self) -> None:
        """Recreate the conversation tables when the shared DB file was swapped.

        The long-term memory index lives in the same file and may quarantine and
        replace it on corruption. Without this check, every later query would
        keep failing with "no such table: sessions" for the whole process
        lifetime, so new messages would silently stop being persisted.
        """
        if self._db_identity() == self._schema_identity:
            return
        logger.warning(
            "[ConversationStore] Shared DB file was replaced; recreating conversation schema"
        )
        self._init_db()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Apply the composed schema to an existing database.

        Column work comes from the seam's column specs (any composed column the
        table lacks is added with its declared DDL), and the key work is the
        table rebuild the seam plans when a stored primary key no longer
        matches the composed one. Keeping both derived from
        ``conversation_schema`` is what stops an upstream merge from silently
        re-creating the single-column key: the comparison is on the live schema,
        not on which literal is in the file.
        Upstream's own migration tail (an ``agent_id`` ALTER plus an
        ``agent_id``-scoped index script) is intentionally absent: this
        method already adds every composed column and rebuilds every
        composed key from the seam, so re-running upstream's literal would
        be a second, unreviewed source of schema truth. ``_ensure_composite_key``
        below is upstream's and is kept, because its caller needs it.
        """
        schema = _core_schema()
        for table_name, statement in schema_seam.plan_column_migrations(conn, schema):
            try:
                conn.execute(statement)
                conn.commit()
                logger.info(
                    f"[ConversationStore] Migrated: {statement.split(' ADD COLUMN ')[-1]}"
                    f" on {table_name}"
                )
            except Exception as e:
                logger.warning(
                    f"[ConversationStore] Migration ({table_name}) failed: {e}"
                )

        for table in schema.tables.values():
            try:
                if schema_seam.rebuild_key_constraints(conn, table):
                    conn.commit()
                    logger.warning(
                        "[ConversationStore] Rebuilt %s to the composed primary key"
                        " %s; the pre-rebuild table is kept as %s%s and can be"
                        " restored with rollback_key_constraints()"
                        % (table.name, table.key, table.name,
                           schema_seam.BACKUP_SUFFIX)
                    )
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.error(
                    f"[ConversationStore] Key rebuild for {table.name} failed: {e}"
                )

    def rollback_key_constraints(self) -> bool:
        """Restore the pre-rebuild tables (documented rollback for task 6.5).

        Returns True when at least one table was restored. Rows written after
        the rebuild are not carried back: this is a restore-point rollback.
        """
        schema = _core_schema()
        restored = False
        with self._lock:
            conn = self._raw_connect()
            try:
                with conn:
                    for table in schema.tables.values():
                        restored = (
                            schema_seam.rollback_key_constraints(conn, table)
                            or restored
                        )
            finally:
                conn.close()
        self._schema_identity = self._db_identity()
        return restored

    def _ensure_composite_key(self, conn: sqlite3.Connection) -> None:
        """Rebuild sessions/messages under composite ``(agent_id, session_id)`` keys.

        A pre-global database keyed ``sessions`` by ``session_id`` alone and
        ``messages`` by ``(session_id, seq)``. Those keys cannot hold two Agents
        that share a ``session_id`` (the same IM user in both), so before any
        second Agent's rows can join this file the tables must be rebuilt under
        the composite keys the current ``_DDL`` declares.

        Detected by inspecting the primary key of ``sessions``: a single-column
        PK means the old shape. The rebuild is the classic SQLite
        create-new / copy / drop / rename, wrapped in one transaction so an
        interruption rolls back to the old shape rather than a half-migrated one.
        Restricted by the caller to installs that actually need it (more than
        one Agent), so the vast single-Agent majority never rebuilds.
        """
        pk_cols = [
            row[1]
            for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
            if row[5]  # pk position, 0 when not part of the primary key
        ]
        if pk_cols == ["agent_id", "session_id"]:
            return  # already composite
        logger.info(
            "[ConversationStore] Rebuilding sessions/messages under composite "
            "(agent_id, session_id) keys for multi-Agent merge"
        )
        with conn:
            conn.execute("DROP INDEX IF EXISTS idx_messages_session")
            conn.execute("DROP INDEX IF EXISTS idx_sessions_last_active")
            conn.executescript(
                """
                CREATE TABLE sessions_new (
                    agent_id          TEXT    NOT NULL DEFAULT '',
                    session_id        TEXT    NOT NULL,
                    channel_type      TEXT    NOT NULL DEFAULT '',
                    title             TEXT    NOT NULL DEFAULT '',
                    context_start_seq INTEGER NOT NULL DEFAULT 0,
                    created_at        INTEGER NOT NULL,
                    last_active       INTEGER NOT NULL,
                    msg_count         INTEGER NOT NULL DEFAULT 0,
                    pinned            INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (agent_id, session_id)
                );
                INSERT INTO sessions_new
                    (agent_id, session_id, channel_type, title, context_start_seq,
                     created_at, last_active, msg_count, pinned)
                SELECT agent_id, session_id, channel_type, title, context_start_seq,
                       created_at, last_active, msg_count, pinned
                FROM sessions;
                DROP TABLE sessions;
                ALTER TABLE sessions_new RENAME TO sessions;

                CREATE TABLE messages_new (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id     TEXT    NOT NULL DEFAULT '',
                    session_id   TEXT    NOT NULL,
                    seq          INTEGER NOT NULL,
                    role         TEXT    NOT NULL,
                    content      TEXT    NOT NULL,
                    created_at   INTEGER NOT NULL,
                    extras       TEXT    NOT NULL DEFAULT '',
                    run_id       TEXT    NOT NULL DEFAULT '',
                    UNIQUE (agent_id, session_id, seq)
                );
                INSERT INTO messages_new
                    (id, agent_id, session_id, seq, role, content, created_at, extras, run_id)
                SELECT id, agent_id, session_id, seq, role, content, created_at, extras, run_id
                FROM messages;
                DROP TABLE messages;
                ALTER TABLE messages_new RENAME TO messages;

                CREATE INDEX IF NOT EXISTS idx_messages_session
                    ON messages (agent_id, session_id, seq);
                CREATE INDEX IF NOT EXISTS idx_sessions_last_active
                    ON sessions (agent_id, last_active);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        with self._lock:
            self._ensure_schema()
        return self._raw_connect()

    def _raw_connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_store_instance: Optional[ConversationStore] = None
_store_instances: Dict[str, ConversationStore] = {}
_store_lock = threading.RLock()


def _default_db_path() -> Path:
    """The one global conversation file: the DEFAULT Agent's ``index.db``.

    Resolved explicitly from the registry's default Agent workspace, never via
    the routing-aware ``get_default_memory_config()`` — that one follows the
    ambient identity, so under ``identity_scope(agent_id=X)`` it would hand back
    Agent X's own file and split X's writes off into its (soon-archived) source
    DB instead of the shared global file. This must be identity-independent.
    """
    from common.utils import expand_path

    try:
        from agent.registry import get_agent_registry
        registry = get_agent_registry()
        default_ws = registry.get(require_enabled=False).workspace
        from agent.memory.config import MemoryConfig
        return MemoryConfig(workspace_root=str(default_ws)).get_db_path().resolve()
    except Exception:
        return (
            Path(expand_path("~/cow")) / "memory" / "long-term" / "index.db"
        ).resolve()


def _resolve_store_path(workspace_root=None) -> Path:
    """Legacy per-workspace file path (kept for the registry-less fallback)."""
    if workspace_root is None:
        return _default_db_path()
    from agent.memory.config import MemoryConfig
    from common.utils import expand_path
    workspace = Path(expand_path(str(workspace_root))).resolve()
    return MemoryConfig(workspace_root=str(workspace)).get_db_path().resolve()


def _resolve_global_binding(workspace_root) -> tuple:
    """Map a caller's ``workspace_root`` to ``(db_path, agent_id)`` on the
    global model.

    Every Agent's conversations live in one file — the default Agent's
    ``index.db`` — and are told apart by an ``agent_id`` column. The default
    Agent binds to ``""`` so its historical, un-tagged rows read back
    unchanged (zero migration for the single-Agent majority); every other
    Agent binds to its own id.

    Falls back to the legacy per-workspace file (and ``""`` id) when the
    registry is unavailable — early startup or a test that never built one —
    so nothing depends on registry readiness just to open a store.
    """
    try:
        from agent.registry import get_agent_registry
        from common.utils import expand_path

        registry = get_agent_registry()
        default_id = registry.default_agent_id
        global_path = _default_db_path()

        # No explicit workspace: follow the ambient RuntimeIdentity so the
        # no-arg call is routing-aware (a scheduler tick or channel turn scoped
        # to Agent X sees X's conversations), exactly as it did when the file
        # itself was routed. Absent an identity this resolves to the default.
        if workspace_root is None:
            try:
                from common.runtime_identity import current_identity
                current_agent_id = current_identity().agent_id or ""
            except Exception:
                current_agent_id = ""
            agent_id = "" if (not current_agent_id or current_agent_id == default_id) else current_agent_id
            return global_path, agent_id

        want = str(Path(expand_path(str(workspace_root))).resolve())
        agents = registry.list(include_disabled=True)
        for profile in agents:
            if str(Path(expand_path(str(profile.workspace))).resolve()) == want:
                agent_id = "" if profile.id == default_id else profile.id
                return global_path, agent_id

        # A workspace that no registered Agent claims. On a multi-Agent install
        # we must NOT fall back to that workspace's own file: once the global
        # migration renames its conversation tables aside, a handle still bound
        # to that file would silently recreate empty tables and split new writes
        # off into the archived source (the pm-agent split bug). Bind to the
        # global file instead, deriving the id from the ``agents/<id>/`` layout
        # so the rows still land under the right Agent; unknown -> default ("").
        if len(agents) > 1:
            derived_id = ""
            try:
                parts = Path(want).parts
                if "agents" in parts:
                    idx = parts.index("agents")
                    if idx + 1 < len(parts):
                        candidate = parts[idx + 1]
                        derived_id = "" if candidate == default_id else candidate
            except Exception:
                derived_id = ""
            return global_path, derived_id

        # Genuinely single-Agent and unmatched (e.g. a bespoke test workspace):
        # its own file is safe, nothing will ever archive it.
        return _resolve_store_path(workspace_root), ""
    except Exception:
        return _resolve_store_path(workspace_root), ""


def conversation_store_path(workspace_root=None) -> Path:
    """The SQLite file a workspace's conversations live in (task 6.11).

    Public so a migration can ask whether a workspace *has* a store before
    opening one: opening creates the file, and a backfill must not manufacture
    an empty database for every workspace that never had a conversation.
    """
    return _resolve_store_path(workspace_root)


def get_conversation_store(workspace_root=None) -> ConversationStore:
    """
    Return the ConversationStore for one Agent, backed by the one global file.

    All Agents share a single SQLite file (the default Agent's
    ``memory/long-term/index.db``); each returned handle is scoped to its
    Agent by an ``agent_id`` column, so callers keep passing a workspace and
    still see only that Agent's conversations. The default Agent scopes to the
    empty string, so a single-Agent install behaves exactly as before.

    The conversation tables (sessions / messages) share the file with the
    memory tables (memory_chunks / file_metadata) as before.
    """
    global _store_instance
    db_path, agent_id = _resolve_global_binding(workspace_root)
    # Key by (file, agent_id): one handle per Agent even though several share
    # the same physical file.
    key = f"{db_path}::{agent_id}"
    store = _store_instances.get(key)
    if store is not None:
        if workspace_root is None:
            _store_instance = store
        return store

    with _store_lock:
        store = _store_instances.get(key)
        if store is None:
            store = ConversationStore(db_path, agent_id=agent_id)
            _store_instances[key] = store
            logger.debug(
                f"[ConversationStore] Using global DB at: {db_path} (agent_id={agent_id or 'default'})"
            )
        if workspace_root is None:
            _store_instance = store
        return store


def clear_conversation_store_cache() -> None:
    """Forget cached store objects. Intended for config reloads and tests."""
    global _store_instance
    with _store_lock:
        _store_instances.clear()
        _store_instance = None


# ---------------------------------------------------------------------------
# Global migration: fold every Agent's conversations into the one global file
# ---------------------------------------------------------------------------

_CONV_TABLES = ("sessions", "messages", "runs")


def _mark_migration_done(conn: sqlite3.Connection, key: str, value: str = "") -> None:
    conn.execute(_META_DDL)
    conn.execute(
        "INSERT OR REPLACE INTO _migration_meta (key, value, done_at) VALUES (?, ?, ?)",
        (key, value, int(time.time())),
    )


def _migration_done(conn: sqlite3.Connection, key: str) -> bool:
    try:
        conn.execute(_META_DDL)
        row = conn.execute(
            "SELECT 1 FROM _migration_meta WHERE key = ?", (key,)
        ).fetchone()
        return row is not None
    except Exception:
        return False


def migrate_conversations_to_global(kickoff_async: bool = True) -> None:
    """Bring every Agent's conversations into the one global file.

    Two levels, matching what each user actually needs:

    * **Level 1 (all users, synchronous, here):** ensure the global file's
      ``sessions``/``messages`` carry an ``agent_id`` column (done by
      ``_init_db``'s ``_migrate``) and — only when more than one Agent exists —
      that both tables use the composite ``(agent_id, session_id)`` key. These
      touch the file the default Agent is about to read/write, so they must be
      finished before traffic flows. They are cheap (metadata add + at most one
      rebuild) and idempotent.

    * **Level 2 (multi-Agent only, asynchronous):** copy each *non-default*
      Agent's conversation rows into the global file, tagged with that Agent's
      id, then rename the source tables aside (never drop). This reads other
      Agents' files and only affects when their history becomes visible in the
      aggregate view, so it runs on a background thread and resumes on the next
      start if interrupted.

    Safe to call any number of times; both levels are guarded by their own
    idempotency checks.
    """
    try:
        from agent.registry import get_agent_registry
        registry = get_agent_registry()
    except Exception as e:  # pragma: no cover - registry always present in prod
        logger.debug(f"[ConvMigrate] registry unavailable, skipping: {e}")
        return

    default_store = get_conversation_store()  # binds default file + agent_id=""

    agents = registry.list(include_disabled=True)
    multi_agent = len(agents) > 1

    # Level 1: composite key, only where a second Agent could ever collide.
    if multi_agent:
        with default_store._lock:
            conn = default_store._raw_connect()
            try:
                default_store._ensure_composite_key(conn)
            except Exception as e:
                logger.warning(f"[ConvMigrate] composite-key upgrade failed: {e}")
            finally:
                conn.close()
        # The rebuild swapped the physical tables; drop the cached schema
        # identity so the next query re-validates against the new shape.
        default_store._schema_identity = ()

    if not multi_agent:
        return

    # Drop every cached handle before the merge renames any source tables.
    # A handle that a warmup/first-request opened against a secondary Agent's
    # own file (e.g. a workspace the registry hadn't matched yet) would, once
    # its tables are archived, silently recreate empty tables and split new
    # writes off into the source — the pm-agent split we just diagnosed.
    # Clearing forces every subsequent open to re-resolve to the global file.
    clear_conversation_store_cache()

    if kickoff_async:
        threading.Thread(
            target=_merge_secondary_agents,
            args=(),
            name="conv-merge",
            daemon=True,
        ).start()
    else:
        _merge_secondary_agents()


def _merge_secondary_agents() -> None:
    """Copy each non-default Agent's conversation tables into the global file.

    Per Agent: one transaction, ``INSERT OR IGNORE`` for idempotency, a
    ``_migration_meta`` marker so a restart never repeats it, and a rename of
    the source tables to ``*_migrated_<ts>`` so the rows stay recoverable and a
    lost marker still cannot double-import (the source table is simply gone).
    A failure is logged and isolated to that Agent; the source file keeps
    serving until the next attempt.
    """
    try:
        from agent.registry import get_agent_registry
        from common.utils import expand_path
        registry = get_agent_registry()
    except Exception:
        return

    default_id = registry.default_agent_id
    global_store = get_conversation_store()
    global_path = str(global_store._db_path)

    for profile in registry.list(include_disabled=True):
        if profile.id == default_id:
            continue
        agent_id = profile.id
        try:
            src_path = str(
                Path(expand_path(str(profile.workspace))).resolve()
                / "memory" / "long-term" / "index.db"
            )
        except Exception:
            continue
        if src_path == global_path or not Path(src_path).exists():
            continue

        marker = f"merged_agent:{agent_id}"
        with global_store._lock:
            conn = global_store._raw_connect()
            try:
                if _migration_done(conn, marker):
                    continue
                _merge_one_agent(conn, src_path, agent_id)
                _mark_migration_done(conn, marker, src_path)
                conn.commit()
                logger.info(
                    f"[ConvMigrate] merged conversations for agent '{agent_id}' "
                    f"from {src_path}"
                )
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.warning(
                    f"[ConvMigrate] merge for agent '{agent_id}' failed "
                    f"({e}); source left intact, will retry next start"
                )
            finally:
                conn.close()


def _attached_columns(conn: sqlite3.Connection, table: str) -> set:
    """The columns ``table`` has in the attached source database (``src``).

    The fork's tenancy dimension rides along with the merge, so the copy reads
    the source's column list instead of assuming it. The dimension is a
    *filter* on every read, which makes omitting it here not a cosmetic loss
    but silent data loss: the merged rows are present yet unreadable to the
    tenant that owns them.
    """
    try:
        return {row[1] for row in conn.execute(f"PRAGMA src.table_info({table})")}
    except sqlite3.Error:
        return set()


def _merge_one_agent(conn: sqlite3.Connection, src_path: str, agent_id: str) -> None:
    """Attach a source Agent DB and copy its conversation rows in, tagged."""
    conn.execute("ATTACH DATABASE ? AS src", (src_path,))
    try:
        src_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM src.sqlite_master WHERE type='table'"
            ).fetchall()
        }
        # Older sources predate the fork's tenancy dimension; absent, the
        # column copies as the empty string the schema defaults to.
        src_session_cols = _attached_columns(conn, "sessions")
        src_message_cols = _attached_columns(conn, "messages")
        session_owner = "COALESCE(owner, '')" if "owner" in src_session_cols else "''"
        session_tenant = (
            "COALESCE(tenant_id, '')" if "tenant_id" in src_session_cols else "''"
        )
        message_owner = "COALESCE(owner, '')" if "owner" in src_message_cols else "''"
        message_tenant = (
            "COALESCE(tenant_id, '')" if "tenant_id" in src_message_cols else "''"
        )
        with conn:
            if "sessions" in src_tables:
                conn.execute(
                    f"""
                    INSERT OR IGNORE INTO sessions
                        (agent_id, session_id, channel_type, title, context_start_seq,
                         created_at, last_active, msg_count, pinned, owner, tenant_id)
                    SELECT ?, session_id, channel_type, title, context_start_seq,
                           created_at, last_active, msg_count, pinned,
                           {session_owner}, {session_tenant}
                    FROM src.sessions
                    """,
                    (agent_id,),
                )
            if "messages" in src_tables:
                # id -> NULL so the global file re-issues AUTOINCREMENT ids and
                # cross-file ids never collide; dedupe is on (agent_id, session_id, seq).
                conn.execute(
                    f"""
                    INSERT OR IGNORE INTO messages
                        (agent_id, session_id, seq, role, content, created_at, extras,
                         run_id, owner, tenant_id)
                    SELECT ?, session_id, seq, role, content, created_at,
                           COALESCE(extras, ''), COALESCE(run_id, ''),
                           {message_owner}, {message_tenant}
                    FROM src.messages
                    """,
                    (agent_id,),
                )
            if "runs" in src_tables:
                # runs already carry agent_id (uuid run_id never collides);
                # backfill only the empties to this source's owner.
                conn.execute(
                    """
                    INSERT OR IGNORE INTO runs
                        (run_id, agent_id, user_id, session_id, parent_run_id,
                         task_id, task_source, status, started_at, ended_at, error, extras)
                    SELECT run_id,
                           CASE WHEN COALESCE(agent_id, '') = '' THEN ? ELSE agent_id END,
                           user_id, session_id, parent_run_id,
                           task_id, task_source, status, started_at, ended_at,
                           COALESCE(error, ''), COALESCE(extras, '')
                    FROM src.runs
                    """,
                    (agent_id,),
                )
    finally:
        conn.execute("DETACH DATABASE src")

    # Rename the source tables aside so the rows survive but can never be
    # re-imported. Best-effort and outside the copy transaction.
    ts = int(time.time())
    src_conn = sqlite3.connect(src_path, timeout=10)
    try:
        existing = {
            row[0]
            for row in src_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        with src_conn:
            for table in _CONV_TABLES:
                if table in existing:
                    src_conn.execute(
                        f"ALTER TABLE {table} RENAME TO {table}_migrated_{ts}"
                    )
    except Exception as e:
        logger.warning(
            f"[ConvMigrate] could not archive source tables in {src_path}: {e}"
        )
    finally:
        src_conn.close()
