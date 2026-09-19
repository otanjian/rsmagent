# Phase 1 verification — fork web layer moved out of the monolith

Date: 2026-09-19
Branch: `codex/adopt-upstream-web-split`
Base (pre-migration) HEAD: `b5c5090f` (`rdai`)

## What phase 1 claims

The fork's web implementation — 290 module-level symbols (79 handlers + 134
private helpers + the rest pipeline, constants and runtime state) — lives in
`channel/web/fork/**`, sliced **verbatim** from
`channel/web/web_channel.py` by AST line range. `channel/web/web_channel.py` is
reduced to upstream's shape: the URL table, `build_web_app()`, the module-level
imports other code reaches for, and the handler imports `web.py` resolves
against its globals.

The claim to verify is **behaviour preservation**: for the same request the
authorization outcome is the same as before the move. The strongest available
evidence of that is the existing suite — written against the monolith and
unmodified in intent — continuing to pass.

## Method

The pre-migration tree was kept as an isolated clone at `/tmp/rsmagent-baseline`
so both runs use the same interpreter (`.venv`, CPython 3.14.3), the same test
order (`-p no:randomly`) and the same commands.

| Run | Command |
|-----|---------|
| Python baseline | `.venv/bin/python -m pytest tests/ -q -p no:randomly --tb=no -rf` |
| Python after | same |
| Node baseline | `node --test tests/*.cjs tests/support/*.cjs` |
| Node after | same |

## Result

### Python suite — no regressions

| | failed | passed | skipped | subtests |
|---|---|---|---|---|
| baseline | 33 | 5414 | 7 | 396 |
| after | **27** | **5420** | 6 | 402 |

Comparing the unique failing test ids (`32` → `27`):

- **new failures: 0**
- resolved: 5 (`tests/test_external_channel_propagation.py` ×4,
  `tests/test_subagent.py::test_the_repo_ships_a_guide_that_documents_the_real_format`)

Every one of the 27 remaining failures also fails on the pre-migration tree.
19 of them are `tests/test_weixin_qr_flow.py`, which is red in the baseline for
reasons unrelated to this change.

### Node suite — no regressions

| | failed | passed |
|---|---|---|
| baseline | 54 | 626 |
| after | **46** | **635** |

- **new failures: 0**
- resolved: 8

### Phase-1 gates (task 2.9)

| Gate | Result |
|---|---|
| `scripts/check-route-coverage.py` | `176 routes (68 upstream, 108 fork), 221 method entries` → **OK** |
| `tests/test_route_registry.py` | pass |
| `tests/test_upstream_core_seams.py` | pass |
| `tests/test_no_resurrection_legacy_identity.py` | pass |
| `tests/test_identity_resource_authorization.py` | pass |
| `tests/test_http_policy.py` | pass |

Widened re-run over the 17 files touched by this change (the six gates plus the
files that failed at any point during migration): **410 passed, 2 skipped**.

### Emitter determinism

Re-running `scripts/migration/emit_fork_web.py` + `emit_entry_module.py` against
an unchanged monolith and ref leaves `channel/web/fork/**` and
`channel/web/web_channel.py` **byte-identical** (checked by per-file md5 across
consecutive runs). A re-run is therefore diffable, which is what makes the
"verbatim slice" argument checkable rather than asserted.

## Regressions found during verification, and their root causes

The first post-migration full-suite run had **27 new failures**. They were not
accepted as "expected fallout": each was traced to a cause and fixed.

### 1. `patch.object(web_channel, "x")` stopped intercepting (22 failures)

The emitter kept references to the entry module for names it could see resolved
through it, but detected those by scanning for `web_channel.<name>` and
`from channel.web.web_channel import <name>`. Most of the suite stubs console
helpers as **string arguments**:

```python
with patch.object(web_channel, "_db_scope", _fake_db_scope), \
     patch.object(web_channel, "_require_read_permission"), ...
```

A name reached only that way was not recognised as seam surface, so the call site
bound to the sibling fork module and the patch silently stopped applying. This is
the failure mode the design set out to prevent, and it is invisible to a passing
test — the test only fails when it asserts on the stubbed behaviour, which is why
it surfaced as unrelated-looking behaviour regressions
(`test_agent_workbench`, `test_private_agent_file_scope`,
`test_personal_channel_console`, …).

Fixed in `emit_fork_web.py::compute_hub_surface`: the string-argument form is now
recognised, plus a deliberately conservative rule treating any name passed to
`patch.object(<obj>, "x")` / `setattr(<obj>, "x", …)` as seam surface. A false
positive can only route a call through the entry module — which is what the
monolith did — so it costs a lookup and never changes which implementation wins.
Same-module references stay local, so no shadowing is introduced.

### 2. `__file__`-relative asset paths (5 failures)

Moving a symbol changes what `__file__` resolves to, so `chat.html` and
`static/` were looked up under `channel/web/fork/handlers/`. Fixed by rewriting
those six expressions to a `_WEB_ROOT` constant anchored at `channel/web` —
the migration's only non-verbatim edit, documented in the emitter and in
`scripts/migration/README.md`.

### 3. Guardrails reading the monolith's source text (9 failures)

These assert on source text rather than behaviour, so they must follow the code.
They were retargeted to the **web layer as a whole**
(`tests/_helpers.web_layer_source()`, `tests/_web_layer.cjs`) rather than pointed
at one fork module, so a future move between fork modules cannot turn a
guardrail into a no-op.

Two of them were worse than failing: they were **passing vacuously**.
`test_channel_signature_seam.py::test_no_database_only_keyword_survives_on_the_channel`
read `web_channel.__file__` — now the thin entry module, which contains no
`WebChannel` method bodies — and
`test_no_resurrection_legacy_identity.py::test_legacy_auth_helpers_are_absent`
searched the entry module for retired helpers. Both would have accepted a
resurrected helper inside `channel/web/fork/`. Both now read the whole layer, as
does `tests/test_route_registry.py::test_core_files_no_longer_carry_route_literals`.

## Not covered here

Phase 1 is backend-only and behaviour-preserving. Still open, and tracked in
`tasks.md`:

- the frontend split (`console.js` 932 KB / `console.css` 197 KB are untouched
  and still conflict with upstream) — phase 3;
- the drift guard that detects upstream edits to handlers the fork now owns;
- the actual `master` → `rdai` merge commit (phase 2), which phase 1 unblocks by
  removing the file-level conflict on `web_channel.py`.
