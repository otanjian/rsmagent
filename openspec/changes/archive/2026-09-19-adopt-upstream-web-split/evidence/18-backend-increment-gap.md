# 18 — Upstream's web-handler increments the fork's parallel layer has not absorbed

Task 3.5/3.6 asks whether upstream's *no-conflict* increments were silently
dropped. For the web layer the answer cannot be "the merge carried them": since
Phase 1 the fork implements 64 upstream handlers in
`channel/web/fork/handlers/**` (evidence 03 explains why parallelism was the only
route that does not edit upstream files), so upstream's changes to
`channel/web/api/**` do **not** reach the fork's implementation through a merge.

Tool: `scripts/migration/measure_backend_increment_gap.py` (`ast` on
`upstream/master` = `8f1b19f1` vs the fork point `e5e2a52d`, exact method source
compared, upstream's post-fork-point lines looked up in the fork's parallel
body).

## Measured on this merge

| | methods |
| --- | --- |
| upstream handler methods | 67 |
| **new** since the fork point | **22** |
| changed since the fork point | 45 |
| of those changed, absent from the fork's parallel body | 45 → **39** after `evidence/20` |

Upstream lines with no counterpart in the fork's body: **≈450** → **≈390** after
`evidence/20`, still concentrated in `channel/web/api/models.py`
(search/chat-fallback/provider/capability cards, ~240), `scheduler.py`,
`pages.py` (asset serving), `config.py`, `sessions.py`, `channels.py` and
`update.py`.

Caveat on the number: `measure_backend_increment_gap.py` judges by text
similarity, so a port written in the fork's idioms (`cls._is_real_key` instead of
upstream's `is_real_key`, a lazy `from channel.web.web_channel import conf`
instead of a module-level import) still counts as absent. The search-provider and
chat-fallback rows below are the worked example: both are closed and both still
appear in the script's output. **A row is a question to adjudicate, not a
verdict** — the adjudication is per method, and the retargeted test is the proof.

The 22 brand-new methods are features with no fork equivalent at all (search
credentials, chat-fallback chain, capability predictions, the update check
endpoint surface).

## Consequence, stated plainly

This is a **capability gap, not a test failure**: the fork's console keeps
working, but it does not expose what upstream added. Closing it is task 3.5/3.6,
one method at a time, into the fork's parallel handlers and through the fork's
authorization (`_require_*` / `_db_scope`) rather than by copying upstream's
authentication model. The measurement is repeatable, so progress is countable
rather than asserted.

Ported so far (this merge, where the gap was already visible in a failing test):

- the `tool_retrieval` SSE event → `channel/web/fork/runtime.py`;
- channel manager resolution via `common.channel_registry`
  (`_live_channel_manager()`), instead of a `__main__` module lookup →
  `channel/web/fork/handlers/channels.py`, `fork/runtime.py`;
- the global scheduler store model (`AgentScopedTaskStore`) →
  `channel/web/fork/handlers/scheduler.py`;
- the agent dimension of the conversation store →
  `agent/memory/conversation_store.py` (evidence 14).

## Two concrete instances, found by auditing test drift — both now **closed**

An audit of every test file the merge touched
(`scripts/migration/find_retargeted_tests.py`) turned two measurement rows into
named gaps in the fork's console. They were the first members ported:
`evidence/20-backend-increment-port.md` records the ports, and
`tests/test_web_search.py` / `tests/test_chat_model_fallback.py` — repointed at
the fork's served handler — are the proof (11 failures before, 89 passes after).

1. **Search providers.** ~~The tool module (shared, merged) now carries
   `tavily`, `searxng`, `keenable`, while the fork's
   `channel/web/fork/handlers/models.py` still lists the six original ids.~~
   **Closed**: the fork's `_SEARCH_PROVIDERS` now equals the runtime's
   `PROVIDER_ORDER` (nine ids), and `_search_provider_key` /
   `_search_capability` / `_handle_set_search_credential` carry the three
   providers' shapes — searxng's instance URL (`needs_url`, `url_masked`), and
   the keyless tier for anysearch/keenable (`anonymous` in the payload,
   `<provider>_anonymous` in the config).
2. **Chat-fallback chain.** ~~The fork's console handler still reads and writes
   `max_switches` … so the cap the user sets there is dropped by the merged
   config normaliser, and the console cannot express a chain of more than one
   link.~~ **Closed**: the fork's console now persists `{enabled, chain}` and
   reads the chain back, because the runtime is upstream's
   (`config.py::_migrate_chat_fallback`, `bridge/agent_bridge.py` walking
   `chain`). `max_switches` is dropped, matching the migration's own note;
   `current_provider`/`current_model` are kept as chain link 1 for a console
   that has not caught up to the chain UI (front-end work: `evidence/17`).

The fork test files that lost coverage to the same refactors (fork's
`test_chat_model_fallback.py` assertions about `max_switches` clamping, and the
search-sync assertion above) are upstream's versions now; the fork's behaviour
is unchanged and untested rather than deleted. `tests/test_direct_addressing.py`
and `tests/test_conversation…`-style imports that the merge silently re-pointed
at `channel.web.api/core` were re-pointed back at the fork's stack — that is
task 3.10, and the audit keeps finding them (3 files this round: search,
direct addressing, and the fallback file above).
