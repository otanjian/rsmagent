# 21 — Adjudicating the remaining backend-increment rows (task 3.5 / 3.6 / 3.10)

Follow-on to `evidence/18` (the measurement) and `evidence/20` (the first two
ports). This round closes the rest of the measurement's rows: every method the
script still lists has been read against upstream's side and classified as
**ported**, **fork-hardened** (deliberately not upstream's), **equivalent under
another name**, or **deferred with the split console**. Nothing was dropped
silently, which is what task 3.5 asks for.

Tool: `scripts/migration/measure_backend_increment_gap.py --upstream
origin/master`. Its similarity heuristic still reports ~35 rows after this
round; that is expected and is not the counter — the tables below are.

## A. Test drift re-pointed at the fork's served stack (task 3.10)

`build_app()` is upstream's assembly over `channel/web/api/**`; the console
actually serves `build_web_app()` over `channel/web/fork/handlers/**`
(evidence/11, D8). Five test files were still naming upstream's modules, so they
asserted against code no request ever reaches. All five now import/patch
`channel.web.web_channel` (or `channel.web.fork.handlers.<mod>`):

| file | was | now |
| --- | --- | --- |
| `tests/test_web_channel_disconnect.py` | `channel.web.api.channels` | `channel.web.web_channel` |
| `tests/test_upload_agent_scope.py` | `channel.web.api.files._require_auth` | fork identity chain (`_db_scope` + `_require_*`) |
| `tests/test_web_multipart_agent_scope.py` | `channel.web.api.{files,knowledge}._require_auth` | same |
| `tests/test_console_channel_manager_resolution.py` | `channel.web.api.channels` | `channel.web.web_channel` |
| `tests/test_web_console_update.py` | `channel.web.api.update` | `channel.web.fork.handlers.update` |
| `tests/test_web_console_assets.py` | `channel.web.api.pages` | `channel.web.fork.handlers.pages` (file is skipped until Phase 3) |

**This is what surfaced the gaps.** Re-pointed at the fork, the disconnect test's
five routing cases failed (the fork rejected a legacy card's disconnect with
`instance_id is required`), and the version test's `update_supported` assertion
failed. Both are now closed below. Task 3.10 is done: `grep -rl
'channel\.web\.api' tests/` returns only docstrings.

Note on the two skipped cases in `test_web_channel_disconnect.py`: upstream's
resurrection test needs `bootstrap_legacy_instances` to synthesize records from
flat credentials. The fork's version is a no-op passthrough (evidence/15), so
the sanity precondition cannot hold and the test stays skipped with that reason
recorded. The *prune* half is still ported and now has fork-native coverage
(the keep-branch and the prune-branch, both driving explicit records).

## B. Real increments, ported

1. **Instance rename** (`_handle_instance_rename`, `POST action=rename`) —
   `channel/web/fork/handlers/channels.py`. Upstream added a friendly label per
   instance; the fork's handler had no rename path at all.
2. **Legacy-card routing** (`is_instance_op`). A multi-instance-ready type
   enabled the legacy way still renders a card with no `instance_id`; without
   the fallthrough its disconnect is rejected and the channel can never be
   removed. Ported verbatim into the fork's `POST`.
3. **`_prune_legacy_channel_type`** — the disconnect path prunes the type from
   `channel_type` when the last instance of it is gone. Adapted to the fork's
   model: the survivor set is built from the fork's explicit records (there are
   no bootstrapped ones), so the keep-branch keys off a surviving record rather
   than a synthesized one. Tested both ways.
4. **`_channel_instances_view`** — blank credentials now fall back to the global
   `config.json` value (mirroring `channel.cfg`), so a secret that lives only in
   the global config renders masked instead of blank; and the card carries
   `instance_name`.
5. **`VersionHandler.GET`** — now `version_payload()` (local metadata only:
   `version`, `install_kind`, `update_supported`, `unsupported_reason`,
   `platform`) instead of the fork's one-field stub. `/api/version` is routed
   with `"upstream"` provenance, so upstream's payload is the contract.
6. **Custom-provider delete drops its catalog** —
   `model_catalog.remove_catalog("custom:<id>")` after
   `_persist_custom_providers`, or a deleted provider's overrides/hidden set
   lingers and re-attaches to a provider created later with the same id.
7. **Workspace-relative media is rewritten on every display path.** Upstream
   absolutizes `![](img.png)`-style refs in a reply to `/api/file?path=…` so
   images render for non-default Agents. The fork had the helper (carried
   verbatim in `channel/web/core/_common.py`) but called it nowhere:
   - SSE `done` event → `channel/web/fork/runtime.py::WebChannel._send`
     (display copy only — auto-TTS still reads the original text);
   - the polling branch of the same method;
   - `HistoryHandler.GET`, so a reopened conversation shows the same URLs;
   - re-exported through `channel.web.web_channel`, which is the seam the fork
     imports such helpers through.
   Covered by `tests/test_history_agent_workspace.py`
   (`test_history_media_refs_are_rewritten_to_a_servable_url`, and a direct test
   that a ref escaping the workspace root is left literal, since the rewrite
   turns refs into servable URLs).

8. **The search-credential UI was wired to the three new providers.**
   `_search_capability` advertises nine providers, but the *served* console only
   knew six: `tavily`/`keenable` fell through to the model-vendor modal and
   `searxng` had no instance-URL field, so three providers the backend reported
   as configurable could not be configured from the console.
   `channel/web/static/js/console.js` now routes the dedicated-credential set
   (`bocha`, `anysearch`, `serply`, `tavily`, `searxng`, `keenable`) to the
   credential dialog, posts `url` for `searxng` (pre-filled from `url_masked`,
   and not treated as a masked sentinel), posts `anonymous: !api_key` for
   `anysearch`/`keenable` so an empty save reaches their keyless tier, and shows
   the clear button while that tier is on. Upstream's copy for the three
   providers was added to the loaded namespace
   (`static/js/i18n/models-config.js`, all three languages; previously only in
   the unloaded `core/i18n.js`). Covered by
   `tests/test_console_search_providers.cjs` (7 cases), with
   `tests/test_console_i18n_parity.cjs` kept green through the fixture update.
   The field label follows upstream's `views/models.js` (and the fork's existing
   `API Key`) in being a literal — `Instance URL` vs `API Key` — rather than a new
   key, so the Phase 3 port of this dialog stays a no-op.

## C. Adjudicated: fork-hardened, keep the fork's side

These rows are upstream changes the fork deliberately does **not** take. They
are recorded here so "absent" stops meaning "maybe dropped".

| method | why the fork keeps its own |
| --- | --- |
| `SchedulerToggleHandler.POST`, `SchedulerDeleteHandler.POST`, `SchedulerUpdateHandler.POST` | upstream authenticates with `_require_auth()` and writes through `_global_task_store()`. The fork authorizes first — `_db_scope()` → `_scheduler_access(ctx)` / `_scheduler_actor(ctx)` / `_request_agent_id(body)`, `TaskAuthorizationError` → HTTP, and revision-checked updates. Taking upstream's body would remove the authorization and the optimistic-concurrency check. |
| `AgentAvatarHandler.GET/POST`, `AgentsHandler.GET` | fork resolves the Agent through `_db_scope` + `_require_tenant_agent_binding` + `_require_agent_action` and serves only tenant-bound Agents; upstream's is `_require_auth()` + registry lookup. |
| `WeixinQrHandler._qr_state` / `_poll_status`, `FeishuRegisterHandler._state` / `_reset_state` | process-global QR/session state upstream still keeps; the fork replaced it with per-identity scan-onboarding sessions (the fork docstring on `WeixinQrHandler` records this). Re-adding the globals would reopen cross-identity takeover. |
| `RootHandler.GET` | `/` redirects to `/chat`, the fork's console path, not upstream's `/`. |
| `ChatHandler.GET` | the fork assembles the monolithic shell and cache-busts an explicit asset list plus discovered i18n namespaces and fork fragments; upstream's `template.render('chat.html')` serves the split shell, which is not wired yet (Phase 3). |

## D. Adjudicated: equivalent under another name

- `ModelsHandler._chat_preset_models` → the fork's `_chat_provider_models()`
  (same `{provider_id: [models]}` reduction of `PROVIDER_MODELS`; the fork
  reaches the mapping through `ConfigHandler.PROVIDER_MODELS`, which is where
  the fork's module layout keeps it). Every other `models.py` row
  (`_search_provider_key`, `_handle_set_capability`, `_image_capability`,
  `_set_chat`, `_handle_set_provider`, `_handle_delete_provider`,
  `_chat_capability`, …) is byte-identical to upstream apart from that same
  `ConfigHandler.` indirection and the catalog calls ported in `evidence/20`.

## E. Adjudicated: not routed by this console (deferred with the split console)

`channel/web/api/**` defines classes the fork does not route. Asserted by
`tests/test_web_console_update.py::test_the_fork_console_does_not_route_the_one_click_update_api`
and by the registry diff below:

| upstream route | handler | why absent |
| --- | --- | --- |
| `/api/update/check`, `/api/update/start`, `/api/update/status` | `UpdateCheck/Start/StatusHandler` | the one-click update menu ships with upstream's split console; `/api/version` (the version row, shown everywhere) *is* routed and now returns the full payload (§B5). |
| `/api/scheduler/runs`, `/api/scheduler/runs/detail`, `/api/scheduler/runs/delete`, `/api/scheduler/create`, `/api/scheduler/recipients`, `/api/scheduler/instances` | `Scheduler*Handler` | the fork's scheduler console exposes the five legacy verbs; the newer task-authoring/run-history UI is part of the split frontend. |
| `/api/sessions/(.*)/context_usage`, `/api/sessions/(.*)/compact_context` | `SessionContextUsage/CompactContextHandler` | same: the context-budget UI is the split frontend's. |
| `/(?:agents\|settings\|skills\|memory\|knowledge\|channels\|scheduler\|logs)(?:/[a-z]+)?/?` | `ChatHandler` | upstream's SPA deep-link catch-all. The fork serves the shell at `/chat` and `/admin` and does its view switching client-side, so there is nothing to catch. |

Two further upstream-only additions are display fields for the split frontend
and are deferred with it, not dropped: `ConfigHandler.GET`'s
`web_password_masked` (+ the raw `web_password` in `COW_DESKTOP` mode), read by
`static/js/views/config.js`, and `_annotate_avatar_revs` /
`AgentsHandler.GET`'s `avatar_rev`, used for avatar cache-busting in the split
views. The fork's live console loads neither file (its shell lists
`js/console.js` and friends), and the fork reaches the same behaviour through
its own paths: `/auth/*` for account secrets and `AgentAvatarHandler` +
`_store_avatar` for avatars. They land with tasks 4.4-4.9, together with the
routes that consume them; taking them earlier would ship fields nothing reads
and, for `web_password`, a second way to read the console password.

**One deferred row does have a live consumer: the desktop renderer.** The
`console.js` monolith is the only *web* consumer of these endpoints, but
`desktop/src/renderer/**` is an independent Electron frontend that the merge
carried over byte-identically from upstream (its conflicts were `merge`, not
`keep-fork`), so its next build calls `/api/scheduler/runs`, `/runs/detail`,
`/runs/delete`, `/scheduler/create`, `/recipients`, `/instances` and
`/api/sessions/<id>/{context_usage,compact_context}` — eight routes the fork
backend does not register. The fork's desktop called **none** of them before
this merge, so this is a front-end/backend gap introduced here, not a missing
fork feature. The impact is degraded rather than broken: most call sites swallow
the failure (`.catch(() => [])`), so the new task/run-history and context-usage
surfaces come up empty instead of erroring, while "delete run" surfaces the
error. Closing it — either by routing the endpoints under the fork's
authorization or by degrading the desktop entries — is task 0.6 of
`adopt-upstream-web-frontend-split`, and the item is listed in that change's
`evidence/deferred-upstream-frontend.md` §D.

## F. Route-table diff (task 3.5, "routes")

`URLS` = upstream's 77 patterns, `_WEB_URLS` = the fork's registry (176).
Upstream-only: the twelve rows in §E. Fork-only: the fork's own consoles
(platform/tenant/identity/external-connections/todos/branding/scenes/help/
project-import) plus the RBAC auth surface. Every shared pattern's declared
HTTP methods still match the served handler's implemented methods —
`tests/test_route_registry.py`'s coverage invariant passes — so no method was
dropped by the merge.

## H. Outside `channel/web`: the other rows tasks 3.5 / 3.6 name

Tasks 3.5 and 3.6 are not web-only. The list in 3.6 names channel behaviours and
the scheduler's task fields, so this round audited the non-web dual-changed
files the same way — for each file both sides touched since `e5e2a52d`, diff
`HEAD` against `origin/master` and classify every upstream-only line.

Method: for all 348 dual-changed files, `git diff --numstat HEAD origin/master`
counts the lines upstream has that `HEAD` lacks. Files with a non-zero count are
the only candidates for a dropped increment (a file where upstream's change
merged cleanly has zero). 100 files report one; the table below covers every
one that is not a doc, a test, a desktop-renderer file, or the fork's own
frontend rewrites.

| area | upstream lines absent from HEAD | adjudication |
| --- | --- | --- |
| `channel/feishu/feishu_channel.py` | 0 (only fork-side additions are "missing" upstream) | **present.** The mention gate is intact: `_is_mention_bot` (:549) and the group gate that covers `text` **and** `post` with a non-empty-but-not-us check (:740-751), matching `a3592a9a` + `408e9844`. |
| `channel/qq/qq_channel.py` | 0 — `HEAD` is byte-identical to upstream | **present** (`c82e5b52`, `8e6d813f`): file receive, the `msg_type=2` Markdown body with plain-text fallback, and the preserved filename. |
| `channel/dingtalk/dingtalk_channel.py` | 0 | **present** (`0bd619f0`): the `ContextType.FILE` branch caches the attachment (`file_cache.add(..., file_type="file")`) in both the private and group handlers, plus the `ctype is None` guard. |
| `channel/weixin/weixin_channel.py` | 23 | **fork-hardened, keep the fork's side.** All 23 are one region: upstream's credential resolution. Upstream keeps a fallback from an instance's own credential file to the id-less `get_weixin_credentials_path()` file that the pre-instance QR flow wrote, so the first instance to start inherits whatever token that shared file holds. The fork deleted that fallback on purpose (`resolve_instance_credentials`, a fork-only function with the task-7.3 rationale in its docstring: shared file as cross-instance truth is exactly what the scan rule forbids). Upstream's *other* weixin increments merged and are present: `_cloud_channel_id()` on the QR/status notifications, the `raise` instead of a silent `return` when a scheduled push has no `context_token`, and the `file_type == "video"` send branch. The context-token restore survives (it loads the instance's own file after resolution). |
| `agent/tools/scheduler/task_store.py`, `integration.py`, `scheduler_tool.py` | 5 / 13 / 33 | **present.** `list_tasks(enabled_only, agent_id)` with the `effective_task_agent_id` filter and the `_DescStr` created_at-descending sort (`87706bee`) are in `HEAD`; the residual count is the fork's hardened store (revision conflict, `TaskWriteLease` multi-writer refusal) which upstream has no counterpart of. |
| `app.py`, `config.py`, `common/state_dir.py` | 6 / 5 / 3 | **fork-hardened.** The absent lines are upstream's pre-hardening text and defaults; the fork's boot seam (`_verify_required_seams`, identity-mode guard, database bootstrap, tenancy + task migrations, external-store guard) and its config keys are additions upstream never had. No upstream behaviour is missing. |

Two rows in this area are **deferred with the split frontend**, not ported, and
are recorded here so they are not mistaken for drops:

- **Knowledge empty state** (`cbe14fd1` / `d081f65d`, "keep the knowledge panels
  hidden behind the empty state") is a change to
  `channel/web/static/js/views/knowledge.js` — an upstream split-frontend module
  the fork's shell does not load yet. It lands with task 4.3/4.4, together with
  the module that consumes it.
- The same is true of the other `static/js/{core,views,chat}/*` diffs in the
  100-file list.

## I. Regression evidence for this round

Targeted run over the touched and adjacent files: **255 passed, 2 skipped**
(the two documented skips above), 37 subtests passed.

Full suite, this tree, `pytest tests/ -q -p no:randomly --ignore=tests/e2e`
(13:34): **30 failed, 5735 passed, 30 skipped, 423 subtests passed**.

Re-run at delivery (2026-09-19, after this round's gates and the §6.4 acceptance
were added): **30 failed, 5775 passed, 30 skipped, 423 subtests passed** — same
30 failures, and the +40 passes are exactly the cases this round added
(`tests/test_web_module_seams.py` 11, `tests/test_change_delta_check.py` +3,
`tests/test_web_database_capability_acceptance.py` 26), so the gate and
acceptance work is covered rather than assumed.

Set-level comparison against the stored pre-merge baseline
(`/tmp/base_full.fails`, the fork's `65596a99`, 32 failures) — the 30 are exactly
that set minus two, and neither of the two move in the wrong direction:

    - `tests/test_subagent.py` and `tests/test_knowledge_console_database.py`
      failed before the merge and pass after it (upstream's shipped guide and
      the knowledge tenant-admin case);
    - every remaining failure is one of the six files below, and the merge
      candidate at `163951b5` reported the same 30.

So this round introduces **no** new failure, and the passing count rose from
5717 (merge candidate) to 5735 — the ported behaviours now have their tests
pointing at the code that actually serves them.

All 30 sit in six files, none of which this change touches, and each belongs to
a divergence already recorded elsewhere:

| file | n | why it fails | recorded |
| --- | --- | --- | --- |
| `tests/test_weixin_qr_flow.py` | 22 | `create_tenant_channel_instance` refuses `weixin`: its inbound cannot stamp a sender (`INBOUND_IDENTITY_STAMPING_TYPES = {feishu, dingtalk, wecom_bot}`, `channel/channel_instances.py:135-156`). The scan flow therefore cannot create the instance the tests drive. | pre-existing fork state; the weixin scan surface's adjudication is in the archived `unify-console-by-data-scope` (7.7/7.10) |
| `tests/test_external_channel_propagation.py` (4), `test_external_connection_service.py` (1), `test_external_connections_api.py` (1) | 6 | the in-flight `add-external-system-access` change's own tests (its console entry / catalogue ids are still fail-closed) | not this change |
| `tests/test_personal_console_frontend.py` | 1 | reads the monolith shell, i.e. the Phase 3 divergence | `evidence/17` |
| `tests/test_console_migration_drill.py` | 1 | hardcodes `r_member`'s role version as `8`; the fork's own `_migration_30` backfills the external-connections menu and moves it to `9` (the assertion predates that migration) | not this change (`add-external-system-access`) |

No test was deleted or weakened to reach this state; the count of *new* skips is
one, and it is upstream's legacy-resurrection case whose precondition the fork's
no-op bootstrap cannot satisfy (§A).

### I.1 §6.4 database acceptance (added after this round)

`evidence/23-database-capability-acceptance.md` runs the norm section 6.4
two-tenant acceptance over the same tree and records the per-capability result:
the Web/backend slices pass (26 new cases; 184 passed with the §6.4 starter and
isolation/recovery suites; the full suite above moves 5749 → 5775), the
external-condition slices stay not passed
(one-click update by decision, the console front-end, a real desktop client,
real personal-channel execution, real inference). Its ten tenant-plane routes
include `/api/memory`, `/api/scheduler`, `/api/history`, `/api/skills`,
`/api/knowledge/list`, `/api/agents`, `/api/sessions`, `/api/projects/browse`,
`/api/workspace/tree` and `/api/tenant/channels`, i.e. the entries this section
adjudicated as ported or kept. Its `KnownGapAcceptance` case pins §E's unrouted
rows at runtime: 404 for the update and scheduler-authoring/run-history paths,
405 for the two context-budget paths the session catch-all swallows.
