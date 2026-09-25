# encoding:utf-8
"""What an LLM caller uses to report usage: two fire-and-forget functions.

Both resolve the ambient identity themselves (``common.runtime_identity``)
rather than taking a user id as an argument. That is deliberate and matches how
the rest of this fork treats ownership: whoever *supplies* a user id decides
whose row is written, so an argument here would be a way for one tenant's work
to be billed to another's. Callers with genuinely no identity in scope get a
``''`` tenant and an empty actor, never someone else's.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from agent.token_usage.manager import TokenUsageManager, _UsageEvent
from common.log import logger


def _ambient() -> "tuple[str, str, str]":
    """``(tenant_id, user_id, session_id)`` from the runtime identity."""
    try:
        from common.runtime_identity import current_identity

        identity = current_identity()
        return (
            identity.tenant_id or "",
            identity.user_id or "",
            identity.session_id or "",
        )
    except Exception:
        return "", "", ""


def record_token_usage(
    *,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: Optional[int] = None,
    actor_user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    session_id: str = "",
    at_date: Optional[date] = None,
) -> None:
    """Buffer one usage event. Never raises into the calling LLM path.

    ``actor_user_id``/``tenant_id`` are overrides for the rare caller that knows
    better than the ambient context (a scheduled run replaying for a specific
    tenant); passing them is the exception, not the norm.
    """
    try:
        ambient_tenant, ambient_user, ambient_session = _ambient()
        if at_date is None:
            at_date = date.today()

        prompt_tokens = max(0, int(prompt_tokens or 0))
        completion_tokens = max(0, int(completion_tokens or 0))
        total = (
            total_tokens if total_tokens is not None
            else prompt_tokens + completion_tokens
        )

        TokenUsageManager.get_instance().enqueue(
            _UsageEvent(
                date_str=at_date.isoformat(),
                tenant_id=tenant_id if tenant_id is not None else ambient_tenant,
                actor_user_id=(
                    actor_user_id if actor_user_id is not None else ambient_user
                ),
                # Left blank on purpose: names are resolved when a page renders,
                # so the flush worker never does a lookup (see manager.py).
                actor_username="",
                session_id=session_id or ambient_session,
                provider=provider or "",
                model=model or "",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=max(0, int(total)),
            )
        )
    except Exception as e:
        logger.warning(
            f"[TokenUsage] Failed to record usage for {provider}/{model}: {e}"
        )


def record_llm_call(
    *,
    provider: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: Optional[int] = None,
    input_summary: str = "",
    output_summary: str = "",
    status: str = "success",
    duration_ms: int = 0,
    actor_user_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    session_id: str = "",
) -> None:
    """Record the daily aggregate and the per-call row together.

    This is the preferred entry point for LLM callers: one call site produces
    both the «明细»/«按用户汇总» numbers and the «调用日志» row, so the two tabs
    of the console cannot disagree about how many calls happened.
    """
    record_token_usage(
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        session_id=session_id,
    )

    try:
        ambient_tenant, ambient_user, ambient_session = _ambient()
        total = (
            total_tokens if total_tokens is not None
            else int(prompt_tokens or 0) + int(completion_tokens or 0)
        )
        TokenUsageManager.get_instance()._get_store().insert_call_log(
            tenant_id=tenant_id if tenant_id is not None else ambient_tenant,
            actor_user_id=(
                actor_user_id if actor_user_id is not None else ambient_user
            ),
            actor_username="",
            session_id=session_id or ambient_session,
            provider=provider or "",
            model=model or "",
            prompt_tokens=int(prompt_tokens or 0),
            completion_tokens=int(completion_tokens or 0),
            total_tokens=max(0, int(total)),
            input_summary=_truncate_summary(input_summary),
            output_summary=_truncate_summary(output_summary),
            status=status or "success",
            duration_ms=int(duration_ms or 0),
        )
    except Exception as e:
        logger.debug(
            f"[TokenUsage] Failed to record call log for {provider}/{model}: {e}"
        )


def _truncate_summary(text: str, max_len: int = 100) -> str:
    """Keep a summary to one short line: it is a hint, not the conversation."""
    if not text:
        return ""
    text = str(text).replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."
