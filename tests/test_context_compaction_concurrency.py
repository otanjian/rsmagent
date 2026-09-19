# encoding:utf-8
"""Manual compaction must not lose a turn appended while it summarizes (P2).

``Agent.compact_context`` deep-copies the history under ``messages_lock``, then
computes the summary *outside* the lock because the summary may be a slow LLM
call. That opens a window in which a new turn can be appended; the commit must
therefore re-check ``self.messages`` against the snapshot it summarized and
abandon the stale result rather than overwrite the newer history.

The first test is verbatim from ``implementation.md`` §4 (the phase's designated
failing test before the method was rewritten). The rest pin the two threads that
race for the same snapshot and the single-thread happy path, whose long-term
memory write must happen only after a successful commit.
"""

from __future__ import annotations

import copy
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from agent.protocol.agent import Agent


def test_compaction_does_not_replace_messages_added_during_summary():
    agent = Agent.__new__(Agent)
    agent.messages_lock = threading.Lock()
    agent.last_usage = {"input_tokens": 123}
    agent.messages = [
        {"role": role, "content": [{"type": "text", "text": f"{role}-{n}"}]}
        for n in range(4) for role in ("user", "assistant")
    ]
    before = copy.deepcopy(agent.messages)
    arrived = {"role": "user", "content": [{"type": "text", "text": "new"}]}
    flush = Mock()

    def summarize(messages, max_messages=0):
        with agent.messages_lock:
            agent.messages.append(copy.deepcopy(arrived))
        return "summary"

    flush._summarize_messages.side_effect = summarize
    flush._clean_summary_output.side_effect = lambda value: value
    agent.memory_manager = SimpleNamespace(flush_manager=flush)
    result = agent.compact_context()
    assert result["ok"] is False
    assert result["reason"] == "context_changed"
    assert agent.messages == before + [arrived]
    flush.write_daily_summary.assert_not_called()


def _agent_with_turns(turns: int = 4) -> Agent:
    """A bare Agent holding *turns* complete user/assistant pairs.

    Built with ``__new__`` so the test needs neither a model nor a workspace:
    ``compact_context`` only touches ``messages``, ``messages_lock``,
    ``last_usage`` and ``memory_manager``.
    """
    agent = Agent.__new__(Agent)
    agent.messages_lock = threading.Lock()
    agent.last_usage = {"input_tokens": 123}
    agent.messages = [
        {"role": role, "content": [{"type": "text", "text": f"{role}-{n}"}]}
        for n in range(turns) for role in ("user", "assistant")
    ]
    return agent


class _BarrierFlush:
    """A flush manager whose summarization holds both compactors together.

    The summarization barrier makes the race deterministic: whichever thread
    wins the commit must do so only after both have produced a summary from the
    same snapshot, so exactly one can match ``self.messages`` at commit time.
    """

    def __init__(self, barrier: threading.Barrier, summary: str = "summary") -> None:
        self.barrier = barrier
        self.summary = summary
        self.daily_writes = []

    def _summarize_messages(self, messages, max_messages=0):
        # A timeout keeps a broken implementation from hanging the suite: the
        # wait raises instead of blocking forever, and the join below reports it.
        self.barrier.wait(timeout=5)
        return self.summary

    def _clean_summary_output(self, value):
        return value

    def write_daily_summary(self, summary, user_id=None, reason=""):
        self.daily_writes.append(summary)


def test_two_concurrent_compactions_commit_at_most_once():
    barrier = threading.Barrier(2)
    flush = _BarrierFlush(barrier)
    agent = _agent_with_turns()
    snapshot = copy.deepcopy(agent.messages)
    agent.memory_manager = SimpleNamespace(flush_manager=flush)

    results = []
    errors = []

    def run():
        try:
            results.append(agent.compact_context())
        except BaseException as exc:  # noqa: BLE001 - asserted below, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        # A plain Lock around a deep copy is easy to get wrong (nested acquire
        # deadlocks); joining with a timeout is what turns that bug into a
        # failure instead of a hung suite.
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads), "compaction deadlocked"
    assert not errors, errors

    assert len(results) == 2
    committed = [result for result in results if result["ok"]]
    changed = [result for result in results if result["reason"] == "context_changed"]
    assert len(committed) == 1, results
    assert len(changed) == 1, results
    # The loser assigned nothing, so the live history is exactly the winner's
    # replacement of the shared snapshot (fewer messages than the original).
    assert agent.messages != snapshot
    assert len(agent.messages) < len(snapshot)
    # Long-term memory is written once, by the commit that actually happened.
    assert len(flush.daily_writes) == 1


def test_single_compaction_commits_then_persists_to_daily_memory():
    flush = Mock()
    flush._summarize_messages.return_value = "summary"
    flush._clean_summary_output.side_effect = lambda value: value
    agent = _agent_with_turns()
    before = len(agent.messages)
    agent.memory_manager = SimpleNamespace(flush_manager=flush)

    result = agent.compact_context(keep_recent_turns=2)

    assert result["ok"] is True
    assert result["reason"] == "compacted"
    assert result["compacted_turns"] == 2
    assert result["before"] == before
    assert result["after"] < before
    # The stale provider usage described the pre-compaction history.
    assert agent.last_usage is None
    flush.write_daily_summary.assert_called_once()


def test_nothing_to_compact_keeps_the_history_untouched():
    flush = Mock()
    flush._clean_summary_output.side_effect = lambda value: value
    agent = _agent_with_turns(turns=2)
    before = copy.deepcopy(agent.messages)
    agent.memory_manager = SimpleNamespace(flush_manager=flush)

    result = agent.compact_context(keep_recent_turns=2)

    assert result["ok"] is False
    assert result["reason"] == "nothing_to_compact"
    assert agent.messages == before
    assert agent.last_usage == {"input_tokens": 123}
    flush._summarize_messages.assert_not_called()
    flush.write_daily_summary.assert_not_called()
