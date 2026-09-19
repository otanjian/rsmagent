# 16 — Phase-2 resolution, second round: the seams the first pass left

Context: after conflicts reached zero, the full suite ran `5691 passed /
83 failed`. 26 were pre-existing on the fork's HEAD; 56 were merge-caused. The
first round fixed the scheduler-authorization cluster (15) and the
conversation/tenant cluster. This file records the second round: each remaining
cluster, what the merge had actually broken, and the fix.

## 1. Session anchor leak — `channel/web/fork/handlers/chat.py`

Symptom: `test_default_change_session_anchor.py` saw a session created for one
Agent visible to another.

Cause: the fork's "claim this session" write predates the global conversation
store. It did `INSERT OR IGNORE INTO sessions (session_id, …)` with **no
`agent_id`**, so the row landed under the default Agent (`''`) while the
*poller* stored messages under the target Agent — two rows for one session, and
an unscoped `SELECT` that could see the other Agent's same-id session.

Fix: scope both the ownership `SELECT`s and the `INSERT` to the store handle's
Agent (`store._agent_id`), mirroring `ConversationStore.create_session`'s
canonical insert. 19 tests green (`test_default_change_session_anchor.py`,
`test_session_archive.py`, `test_web_chat_content_type.py`).

## 2. The scheduler: two stores, one runtime

Symptom: `test_plan_3_1_joint_acceptance.py` failed on
`get_scheduler_service(agent_id=ASSISTANT) is None`, then on tasks missing from
an Agent's list.

Root cause (the real find): after the merge there were **two** schedulers.

- the runtime (upstream): one global `tasks.json`,
  `get_task_store()`, every task carrying its own `agent_id`, with the fork's
  boot hook folding the legacy per-Agent files into it;
- the fork's console (`channel/web/fork/handlers/scheduler.py`): resolved a
  **per-Agent** `TaskStore` through `state_dir.scheduler_file(identity)` and
  handed it to `TaskAccessService` as its `store_resolver`.

So a task created through the console was filed where the scheduler loop never
looked, and the console was blind to tasks the loop had created. The
authorization service calls `store.list_tasks()` / `store.get_task()` with no
Agent filter (it is given "the store for agent X" and trusts it), so simply
swapping in the global store would have leaked every Agent's tasks into every
console view.

Fix — converge on upstream's storage, keep the per-Agent call shape:

- `agent/tools/scheduler/integration.py`: new `AgentScopedTaskStore` (filters
  `list_tasks` by its Agent, stamps `agent_id` on `add_task`, delegates the
  rest) and `get_scoped_task_store(agent_id)`;
- `channel/web/fork/handlers/scheduler.py::_scheduler_task_store` now returns
  that view instead of a per-Agent path.

Paths that **stay** per-Agent, deliberately: `common/startup_hooks.py`'s
tenancy migration and its backup pass read the *legacy* files (that is their
input, and the boot order is migration → fold, so nothing is skipped), and
`_migrate_legacy_task_stores` in the integration module.

Service identity: `get_scheduler_service()` is one global service now; the
per-Agent argument is ignored by design. `test_plan_3_1`'s assertion that no
service exists for a disabled Agent was rewritten to the model the merge
actually implements — the disabled Agent's task is never *serviced* (the run is
refused, no `last_run_at`), which is the property the test is about.

Test infrastructure: `tests/_helpers.py` gained `global_scheduler_store()`
(Agent-scoped view, tenant identity **pinned** — a helper runs outside a request,
where `shared_root()` would resolve the instance root rather than the tenant's,
i.e. a different file from the one the handler reads) and
`legacy_scheduler_store()` for the migration tests; `seed_task` stamps
`agent_id`; the harness now drops the process-global scheduler store/service on
build and teardown (same class of cache leak as the agent registry it already
unpins). One test asserted the pre-merge per-Agent file layout
(`test_the_store_layout_is_per_agent_not_per_tenant`) and was rewritten to the
merged contract: one file, per-Agent *views*.

## 3. Smaller merge-caused breaks

- **`channel/dingtalk/dingtalk_channel.py`** — the fork's identity stamping read
  `cmsg.sender_staff_id`, which only exists on the DingTalk message subclass.
  Upstream's new test drives the same code path with a bare `ChatMessage`, so
  any falsy `actual_user_id` crashed. Now `getattr(cmsg, "sender_staff_id", "")`.
- **`channel/web/fork/runtime.py`** — upstream's new additive
  `tool_retrieval` SSE event (allowlisted, sanitized fields only) was missing
  from the fork's `_make_sse_callback`; ported verbatim.
- **`channel/web/fork/handlers/channels.py`, `runtime.py`** — the fork's copies
  still resolved the live ChannelManager with
  `getattr(sys.modules['__main__'], '_channel_mgr', None)`, the lookup upstream
  fixed (#3120) because `python app.py` makes `__main__` a *different* module
  object from a later `import app`: it always returned `None`, so the console
  silently refused to start a newly configured channel. Both now use upstream's
  `_live_channel_manager()` (registry-published) through a module-level wrapper,
  so the source-level guard test holds for the whole web layer.
- **`.gitignore` / `agent/subagent/assets/README.md`** — the fork's blanket
  `README*.md` ignore meant upstream's new sub-Agent guide never entered the
  tree, failing `test_subagent.py`. The guide is a *shipped asset*, not
  repository documentation, so it is un-ignored explicitly.
- **`tests/test_tool_display.py`** — silently retargeted by the merge to
  upstream's `console_js()` helper, which reads the split tree and returns
  nothing for the fork's still-monolithic console. Reads the monolith directly
  again, with a note that Phase 3's split moves it to `console_js()`.
- **Legacy channel bootstrap** — see `15-legacy-bootstrap-divergence.md`.

## Still open after this round

- `tests/test_chat_model_fallback.py` was **replaced wholesale by upstream's
  version**, losing the fork's `max_switches` coverage (no conflict was raised).
  Fork-only tests to be restored.
- The frontend/console cluster (~11 tests: assets, routing, console update,
  knowledge console, tool display parity) belongs to Phase 3 — the fork's
  monolith is still the served console while upstream's split tree already
  exists alongside it. They are expected to go green when the overlay map is
  wired and the monoliths are deleted.
