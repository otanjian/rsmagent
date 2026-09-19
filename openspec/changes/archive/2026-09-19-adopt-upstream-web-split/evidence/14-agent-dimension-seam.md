# 14 — Deep seam: the Agent dimension (conversation store)

Resolution of `agent/memory/conversation_store.py` (16 hunks), plus the two fork
test files whose premise upstream's global store obsoletes.

## What the two sides each did

| side | model | how a row is addressed |
| --- | --- | --- |
| upstream | **one global file**; `agent_id` tags every row; composite keys `(agent_id, session_id[, seq])`; a store *handle* is bound to one Agent (`''` = default) | the handle's binding |
| fork | tenancy filter columns (`owner`, `tenant_id`), never keys; a single handle multiplexed by the **ambient runtime identity** | the ambient identity at call time |

The fork had already built `agent/memory/conversation_schema.py` for exactly this
merge: the composed schema is `compose(historical_tables, (UPSTREAM_AGENT_DIMENSION,
FORK_TENANCY_DIMENSION))`. That seam held up — the schema hunks needed no new logic.

## Dispositions

* **hunks 1–15 → fork side.** Every query in the fork is parameterised by
  `dimension_clause(...)`, and `DIMENSION_COLUMNS` already carries `agent_id`; the
  fork's INSERTs already list the `agent_id` column. Upstream's literal
  `WHERE agent_id = ? AND session_id = ?` is therefore *subsumed, not dropped*.
  Hunk 2's ALTER constants are likewise unnecessary: `_init_db` runs
  `build_table_ddl()` → `_migrate()` (`schema_seam.plan_column_migrations`, which
  adds every composed column) → `build_index_ddl()`.
* **hunk 2 → keep only `_META_DDL`.** Upstream's global-merge bookkeeping table is
  *not* a schema concern the seam covers, and upstream's newly-merged
  `migrate_conversations_to_global` reads/writes it. The unused ALTER constants
  went with the block below.
* **hunk 16 → union.** `_resolve_global_binding` (the global model, called by the
  shared `get_conversation_store`) *and* the fork's public `conversation_store_path`
  (its migration asks whether a workspace has a store before opening one) are both live.

## Two defects the merge introduced silently

1. **Upstream's `agent_id` + `_INDEX_DDL` block landed after a `return`** — inside
   the fork's `rollback_key_constraints`, referencing `cols`/`conn` that are not in
   scope. It merged clean (no conflict marker), so nothing pointed at it. Deleted:
   it duplicates the seam's `plan_column_migrations` + `build_index_ddl`, and the
   only other reader of `_INDEX_DDL` was itself. A note was added to `_migrate`'s
   docstring so the next sync does not re-add it. `_ensure_composite_key` (upstream's,
   genuinely called by the global migration) is kept — it rebuilds its own indexes.
2. **Fork code was relocated into `channel/web/api/`.** Upstream moved
   `channel/web/openai_api.py` → `channel/web/api/openai_compat.py`; git's rename
   detection carried the fork's +183 lines into the new path, i.e. fork logic inside
   the directory this change exists to keep upstream-verbatim. Restored: the fork's
   module back at `channel/web/openai_api.py`, and `api/openai_compat.py` verbatim
   from upstream. The fork's own tests import the former, which is the tell.

## The semantic seam: the handle's binding must win

The only genuine behavioural collision. `dimension_clause` treated an empty value as
"not in scope", and `ConversationStore` took `agent_id` from `ambient_dimensions()`.
Both are wrong once one file holds several Agents:

* the default Agent (`''`) skipped the predicate entirely and could read *every*
  Agent's transcripts;
* `LEGACY_EMPTY_DIMENSIONS` widened a scoped read to also match `''` rows, so a
  secondary Agent could read the default Agent's untagged rows.

Upstream's `tests/test_conversation_global_migration.py` (which arrived with this
merge and is the spec for the global model) pins the strict behaviour: two handles on
one file, sharing a `session_id`, must each read only their own row.

Changes:

* `ALWAYS_SCOPED_DIMENSIONS = {"agent_id"}` — emitted even when empty, because `''`
  is the default Agent, not "unattributed".
* `LEGACY_EMPTY_DIMENSIONS` retired to `frozenset()` (kept, documented, so the
  mechanism and its reasoning are not rediscovered by the next sync).
* `ConversationStore._dimensions()` — ambient dimensions with `agent_id` overridden
  by the handle's binding; all 7 in-class scope reads now use it. `owner`/`tenant_id`
  stay ambient: per-turn filters, not properties of the handle.

## Test retargets (the premise, not the assertion)

* `tests/test_conversation_tenant_isolation.py::test_the_agent_dimension_also_scopes_a_read`
  — one handle + ambient switching → two handles bound to two Agents over one file.
  Same claim, now also exercising the composite key.
* `tests/test_session_store_resolution.py` — `_seed` now stamps `store._agent_id`
  (a row without one belongs to the default Agent, so the addressed Agent could not
  find it), and the registry stub's `get` accepts the **no-argument** call.

## Incident: the merge leaked test rows into a real database

`_default_db_path()` calls `registry.get(require_enabled=False)`. The fork fixture's
stub was `get=lambda agent_id, **kwargs: ...`, requiring a positional argument, so the
call raised `TypeError` — which `_default_db_path` swallows via
`except Exception: return ~/cow/...`. The tests therefore seeded
`~/cow/memory/long-term/index.db`, and the resulting `('', 's-b')` row made the other
three runs fail on the composite key.

* Fixture fixed to `get=lambda agent_id=None, **kwargs: ...` (with a comment naming the
  failure mode).
* The leaked row was deleted from the real database; row count verified back to its
  prior value and no other test literal present.
* Worth a follow-up: that `except Exception` turns a caller-contract error into silent
  redirection of writes into the user's live database. Out of scope here, but it is the
  reason a test-fixture typo reached real data.

## Verification

```
tests/test_conversation_global_migration.py     (upstream's global model)  6 passed
tests/test_conversation_schema_seam.py                                    8 passed
tests/test_conversation_tenant_isolation.py                              12 passed
tests/test_conversation_store_owner.py / runs / message_counts            22 passed
tests/test_session_store_resolution.py                                    4 passed
                                                                     52 passed total
```
