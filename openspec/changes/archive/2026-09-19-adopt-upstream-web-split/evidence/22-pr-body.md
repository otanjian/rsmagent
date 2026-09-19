# merge: sync master into rdai

**这一节已不再是待办**：集成方式后来改为本地 `git merge --ff-only` 直接合入 `rdai`
（见 `tasks.md` 5.9），PR 从未创建。下面保留当时的开 PR 入口以说明原计划；仓库已由
`otanjian/CowAgent` 改名为 `otanjian/rsmagent`，故该 URL 现走 GitHub 301 重定向。

Open this PR at
`https://github.com/otanjian/rsmagent/compare/rdai...codex/adopt-upstream-web-split`
(`gh` is unauthenticated in the authoring environment, so the body is prepared
here and pasted verbatim.)

---

## Summary

Brings `origin/master` (`8f1b19f1`) into `rdai` (`b5c5090f`). The upstream
change this sync carries is a **whole-console refactor**: `console.js`
(17,315 lines) and `console.css` (4,039 lines) were deleted and replaced by a
module tree of ~47 files under `channel/web/static/js/{core,chat,views}/`,
`channel/web/static/css/`, plus a new backend split into
`channel/web/api/**` + `channel/web/core/**`.

That refactor collided head-on with the fork's console: the fork keeps its URL
table, HTTP authorization policy and business handlers in one place
(`route_registry.py` + `web_channel.py`), so 46 files conflicted and 25 of them
were new drift beyond the existing seams. This PR resolves all 46 on the
recorded baseline and adopts the upstream module split **without** putting fork
logic back into upstream modules.

Two scope decisions are worth reading before the diff:

1. **The fork's console keeps serving its own monolith.** The merge brings the
   upstream front-end files into the tree but does not switch to them; the
   served console is byte-identical to the pre-merge one. Migrating the fork's
   front-end customization onto the split modules is a separate, independently
   reviewable change, delivered as `adopt-upstream-web-frontend-split`.
2. **The section 6.4 database acceptance was run after the merge, and its
   result is partial by design.** The Web/backend slices produced all three
   required kinds of evidence on a two-tenant, multi-user fixture over the real
   app; the slices that need external conditions (one-click update — routed
   off by decision, console front-end, a real packaged desktop client, real
   personal-channel providers, real model inference) stay **not passed** and
   are listed as such rather than rounded up.

## Merge evidence

| Item | Value |
| --- | --- |
| Target `rdai` | `b5c5090fc9a3c23695925ba129b3e252460ba982` |
| Source `origin/master` | `8f1b19f1e72db0b46772f78f9c760b04b1836428` |
| Merge base | `e5e2a52d3f309130ebabcb69ee0fb12adf754fbd` |
| Merge commit | `163951b5` (`merge: sync master into rdai`) |
| Parents | `65596a99` (rdai line + web-split work) × `8f1b19f1` |
| Tree | `07244685012289d58d5d541b4c6fff632ab4ad21` |

The merge commit's tree is **byte-identical to the candidate tree resolved and
verified in an isolated clone**: the conflicts were handled with
`git merge --no-commit`, then the tree was converged with
`git read-tree -u --reset <candidate tree>` — so the commit is the tree that was
tested, not a re-merge that approximates it.

Delivery updates were then applied on top (`4cd86956`, `762c301b`, `ad6f7666`,
`bd44bf1f`): freezing the post-merge baseline, closing the increment
adjudication and test retargeting, adding the structural invariant gate,
splitting the front-end unit out, and recording the delivery state. **All
numbers below are for the PR head.**

Both refs were re-checked at delivery with `git fetch` and `git ls-remote`:
`master` is `8f1b19f1` and `rdai` is **still** `b5c5090f` — `rdai` has not moved,
so the verified candidate remains valid and no re-verification against a new
target was needed.

### Verification (PR head)

| Check | Result |
| --- | --- |
| Full python suite (`pytest tests -q -p no:randomly --ignore=tests/e2e`) | **30 failed / 5775 passed / 30 skipped / 423 subtests passed** |
| Same suite on the merge commit's tree | 30 failed / 5735 passed — the +40 passes are exactly the cases this round adds (`tests/test_web_module_seams.py` 11, `tests/test_change_delta_check.py` +3, `tests/test_web_database_capability_acceptance.py` 26), and the failing set is identical item for item |
| Pre-merge baseline (fork `65596a99`) | 32 failures → **zero merge-introduced failures**, and the merge fixes two baseline failures |
| Route coverage (`scripts/check-route-coverage.py`) | `176 routes (68 upstream, 108 fork), 221 method entries, OK` |
| Structural invariant (`scripts/check-web-module-seams.py`) | `OK: 22 upstream module(s), 213 fork-only symbol(s), 0 findings` |
| Baseline coverage (`scripts/check_change_deltas.py`) | `OK (proposed)` — all 46 rows covered |
| Database acceptance §6.4 (`tests/test_web_database_capability_acceptance.py`) | **26 passed** (ordered and random); with the §6.4 starter + isolation/recovery suites: **184 passed** |
| `node --test tests/*.cjs` | Failing set identical to baseline (verified in the same directory), **plus 7 new passing cases** in `tests/test_console_search_providers.cjs` |
| `openspec validate --strict` | Both changes valid |

The 30 remaining failures are unchanged from the merge candidate and all sit in
six files this change does not touch: `test_weixin_qr_flow.py` (22 —
`create_tenant_channel_instance` refuses `weixin` because its inbound cannot
stamp a sender; pre-existing fork state), the in-flight
`add-external-system-access` tests (6), `test_personal_console_frontend.py` (1 —
reads the monolith shell, i.e. the deferred front-end unit), and
`test_console_migration_drill.py` (1 — hardcodes a role version the fork's own
migration has since moved). Per-file attribution is in
`openspec/changes/adopt-upstream-web-split/evidence/21-increment-adjudication.md`.
**No test was deleted or weakened to reach this state.**

## Capability comparison

| Upstream capability | Fork entry point | Status in this PR |
| --- | --- | --- |
| Chat, streaming, polling | `/chat`, `/stream`, `/poll`, `/message` | Served by the fork stack; session/tenant from the trusted context. **Behaviour unchanged** — the served console is the pre-merge one |
| Files / workspace | `/upload`, `/api/workspace/*`, `/api/file` | Fork stack. Multipart `agent_id` scoping fixed and regression-tested (`_scoped_agent_id`) |
| Models & providers | `/config`, `/api/models` | Fork stack. Upstream's increments **ported**: search providers (Tavily / SearXNG / Keenable), ordered chat-fallback chain, per-provider model catalog overlay, ASR model from config, `agent_max_context_tokens` budget. The served console's search-credential dialog was wired to the three new providers in the same round (SearXNG posts an instance URL, anysearch/keenable an empty key that turns their keyless tier on), with upstream's copy added to the loaded i18n namespace — no ported capability is left without an entry point the user can reach |
| Skills / MCP | `/api/skills`, `/api/skills/content` | Fork stack; per-tenant resource and user-permission checks |
| Knowledge / memory | `/api/knowledge/*`, `/api/memory*` | Fork stack; personal-memory version protocol kept |
| Scheduler | `/api/scheduler/*` | Fork stack on the one global task store; upstream's `list_tasks(agent_id, enabled_only)` + created-at-descending sort ported; upstream's per-Agent store shape deliberately not taken |
| Channels | `/api/channels`, `/api/weixin/qrlogin`, `/api/feishu/register` | Fork stack. Upstream's disconnect fix, legacy-card routing and legacy-type pruning ported; Weixin credential resolution **kept fork-hardened** (no fallback to shared credential files) |
| Desktop | `desktop/src/main/preload.ts`, renderer client | Merged on the fork's broker protocol; upstream's upload retry kept; **no** renderer-held desktop token |
| One-click update | — | **Not routed** in the fork console. `/api/version` stays and reports the local build; `/api/update/{check,start,status}` are deferred with the split front-end |
| Console front-end | served monolith | **Deferred** to `adopt-upstream-web-frontend-split` |
| Agent runtime isolation | `agent/registry.py`, `config.py` | Merged as both sides' additions: upstream's injectable builtin id + the fork's brand/binding keys |

Route inventory after the merge: 176 routes (68 upstream-origin, 108 fork),
221 method entries, all registered with a policy. The 12 upstream routes that
were unregistered at rehearsal time were each classified. Deferred with the
split console (their UI belongs to it, so routing them now would ship endpoints
nothing reads): `/api/update/{check,start,status}`, the scheduler authoring and
run-history endpoints (`/api/scheduler/runs*`, `create`, `recipients`,
`instances`), and `/api/sessions/(.*)/{context_usage,compact_context}`.
Upstream's SPA deep-link catch-all is not routed either — the fork serves the
shell at `/chat` and `/admin` and switches views client-side, so there is
nothing to catch. `/api/version` stays and now returns the full payload.

## Database acceptance (norm section 6.4)

**Performed on the delivered candidate: the Web/backend slices pass, the
external-condition slices do not.** Section 6.4 requires, per capability, three
kinds of evidence — positive business success in database mode, authorization
isolation, and reachability through a real entry point. The acceptance was run
over an independent identity database with **two tenants** (`acme`, `globex`),
each with an administrator and an ordinary member, driving the **real
`build_web_app()`** WSGI application. Full per-capability judgement and the
commands are in
`openspec/changes/adopt-upstream-web-split/evidence/23-database-capability-acceptance.md`.

| Evidence class | Result |
| --- | --- |
| Positive business success | Platform plane (models/config/channels/logs) reads and writes round-trip for the platform admin; the three ported search providers, the ordered chat-fallback chain and the per-provider catalog overlay all round-trip through `POST /api/models`; tenant plane answers the member with a real `status=success` payload on ten entries |
| Authorization isolation | Tenant admin and plain member 403 on every platform entry, platform write 403, anonymous 401, foreign-tenant member 403, missing tenant selection 400 |
| Link completeness | Registry has **zero `closed` policies**; the routes this console keeps unrouted answer 404, and the two context-budget paths are swallowed by the session catch-all as 405 rather than being silently opened |

New cases: `tests/test_web_database_capability_acceptance.py` — **26 passed**
(in both ordered and random runs). The section 6.4 starter suites plus the
related isolation/recovery suites run together: **184 passed**.

**Not passed, and not claimed:** one-click update (the three `/api/update/*`
routes stay unrouted by decision; 404 at runtime), console front-end
modularization (deferred to `adopt-upstream-web-frontend-split`), Desktop (its
slice stays `accepted=false`; the eight renderer-consumed routes are unrouted),
personal-channel **execution** (no real provider round-trip), and real model
inference / voice specials. So this PR must not be read as "master's
capabilities are now available in database mode" or "all capabilities pass".
The narrower claims it does support:

- the merge adds **no** new externally reachable ability to the fork: the 12
  upstream routes were each classified, and the fork's authorization decisions
  were not loosened;
- the fork's identity model is unchanged — no resurrection of `web_password`,
  `cow_auth_token`, shared-password HMAC login or `_require_auth` pass-through
  (`tests/test_no_resurrection_legacy_identity.py` passes; the guard reads the
  whole fork web layer, so a helper reappearing inside `channel/web/fork/`
  cannot slip past it);
- the new web-layer invariant gate fails if a fork symbol is ever written back
  into an upstream module.

`openspec/changes/adopt-upstream-web-split/tasks.md` section 5 records the
performed and the still-open parts explicitly rather than as a silent omission.

## Conflict decisions

46 conflicted files: all 21 pre-existing baseline rows reproduced (none
disappeared) plus 25 new drifts. Counts are computed from
`scripts/conflict-baseline.txt`, not transcribed:

| Disposition | n | Basis |
| --- | --- | --- |
| `merge` | 17 | Both sides' changes carried, in the recorded order |
| `merge-docs` | 8 | Upstream prose + fork branding re-applied |
| `keep-deletion` | 7 | The README decision (fork ships no READMEs); this round added `channel/web/README.md` and `static/vendor/README.md` to it, since the first describes *upstream's* entry module |
| `keep-fork` | 5 | `.gitignore`; `channel/web/web_channel.py` (the D8 entry module); `chat.html`, `console.js`, `console.css` (the deferred front-end unit) |
| `retarget` | 5 | Fork tests the merge silently re-pointed at upstream's split modules — re-pointed back at the fork's served stack |
| `seam:` | 3 | `conversation_store.py` (tenant composite key), `scheduler/integration.py` + `test_scheduler_web_update.py` (one global store, tasks carry their Agent) |
| `take-deletion` | 1 | `desktop/build/notarize-dmg.sh` — upstream retired it; the fork's only edit was a branding comment |

Status mix: `UU` 36, `DU` 7, `UD` 3.

Three decisions are worth a reviewer's attention:

**1. Two namespaces in the entry module (`channel/web/web_channel.py`).**
Upstream's `api/` and the fork have 76 / 79 handler classes with **64 names in
common**. Importing both into one `globals()` would make the later import
silently win, and one of the two URL tables would resolve to the other stack's
handler — not a crash, but **silent wrong authorization**. The entry module
therefore exposes two factories, each resolving handlers in its own namespace:
`URLS` + `build_app()` (upstream stack, which upstream's own
`core/channel.py` calls) and `_WEB_URLS` + `build_web_app()` (fork stack).
`WebChannel` / `SERVING` stay the fork's, because only the fork's
`WebChannel` sets the event the fork waits on.

**2. The front-end unit is deferred, explicitly.** The fork's front-end
customization is *edits inside* upstream's modules (84% of added lines fall in
7 upstream modules of the split tree), not attachable units, so it cannot be
re-mounted after the fact — and the upstream scripts are classic scripts sharing
one global scope, where a duplicate top-level `const`/`let` is a `SyntaxError`
that blanks the page. Migrating it is therefore a standalone structural change.
It is deferred the way the design required: `keep-fork` recorded in the baseline
with "not to be taken as a deletion while fork rules remain in it", and every
upstream front-end increment this leaves unserved itemised in
`adopt-upstream-web-frontend-split/evidence/deferred-upstream-frontend.md`
(structural items, plus 7 that only lack UI and ~15 pure front-end fixes), with
the 98 hand-decision regions as its bound. Nothing was dropped silently, and
un-deferring is a single `revert` away.

**3. Fork-handened logic that was deliberately *not* moved to upstream's.**
Scheduler authorization (the fork keeps its revision/lease guards on the one
global store), Desktop token handling (no renderer-held token), Weixin
credential resolution (no fallback to shared credential files), and agent avatar
authorization. Each is recorded with its reason in `evidence/21`.

## Baseline drift

`scripts/conflict-baseline.txt` is re-frozen for this pair of tips:
`origin/master@8f1b19f1 × codex/adopt-upstream-web-split@163951b5`, 46 rows — the
merge commit, matching the header frozen in the file.
Replaying the real conflict set against it reports 46 known conflicts, an empty
drift section, and `DELIBERATE_REMOVALS` unchanged at five names — the
`take-deletion` row for `notarize-dmg.sh` is deliberately *not* a
`keep-deletion`, so upstream retiring a file never silently joins the fork's
standing removals.

Two gates changed with the baseline:

- `scripts/check_change_deltas.py` had a stale disposition vocabulary
  (`{keep-deletion, keep-fork, merge-docs}`) and accepted only task-number seam
  references. It rejected the *correct* baseline with 26 findings. The checker
  was fixed, not the baseline: `merge`, `retarget` and `take-deletion` are now
  recognised, seam references accept module names, and
  `tests/test_change_delta_check.py` covers all three cases. A gate that passes
  by ignoring input is worse than no gate.
- `scripts/check-web-module-seams.py` is new (tasks 4.6/4.7). It declares the
  upstream module set explicitly, derives the fork-only symbol set from the
  registry's `fork:*` rows plus the fork package's top-level names, and fails
  when any of them appears in an upstream module. The basis is the fork's
  symbols, **never a keyword**: upstream uses `tenant` as ordinary domain
  vocabulary, so a keyword criterion would fire on upstream's own code and be
  disabled rather than fixed. Names shared by both trees are subtracted —
  `_live_channel_manager` is upstream's, `ChatHandler` is one of the 64
  collisions — because sharing a name says nothing about who owns a body. The
  entry module may import fork symbols but not define them. Eleven injected
  cases in `tests/test_web_module_seams.py`, including the standalone-upstream
  form passing with no fork package present.

## Rollback

- **Before merge:** close this PR; `rdai` is untouched at `b5c5090f`.
- **After merge:** `git revert -m 1 <merge commit>`. Note the reverted source
  commits remain in the ancestry, so re-landing this sync needs a fresh merge
  rather than a fast-forward.
- **No data migration** is involved, so there is no database rollback step.
- **The deferred front-end unit** is recovered by reverting the split commit, or
  by simply completing `adopt-upstream-web-frontend-split`; the served console is
  unaffected either way.

## Not closed by this PR

1. **Front-end modularization** — `adopt-upstream-web-frontend-split`: 98
   adjudications, the override map, the `manifest.json` drift gate, deleting
   `console.js` / `console.css`, the `.cjs` and browser acceptance, and
   un-skipping the three front-end tests.
2. **Database capability acceptance (norm §6.4)** — the Web/backend slices were
   accepted (see above); the external-condition slices (one-click update, the
   console front-end, a real packaged desktop client, real personal-channel
   execution, real inference) remain not passed and are listed per capability in
   `evidence/23` §2/§5.
3. **The 30 pre-existing test failures** — attributed per file in `evidence/21`;
   none is caused by this merge.
4. **The desktop renderer's new screens against deferred endpoints** — the merged
   `desktop/src/renderer/**` (upstream's, so its next build ships) calls eight
   routes this backend does not register: `/api/scheduler/{runs,runs/detail,
   runs/delete,create,recipients,instances}` and
   `/api/sessions/<id>/{context_usage,compact_context}`. The fork's desktop
   called none of them before this merge, so this PR leaves a front-end/backend
   gap: those screens come up empty (most call sites swallow the failure) and
   "delete run" surfaces an error. The acceptance run records the exact runtime
   shape: the nine `/api/update/*` and scheduler-authoring/run-history paths are
   404, while the two context-budget paths are captured by the session
   catch-all and answer 405. It is recorded in `evidence/21` §E and
   assigned to task 0.6 of `adopt-upstream-web-frontend-split` — route the
   endpoints with the fork's authorization, or degrade the desktop entries. It
   is **not** a missing fork capability and not a security loosening; nothing
   was routed to satisfy a UI.

## Evidence index

- `openspec/changes/adopt-upstream-web-split/` — design (D1–D9), tasks,
  `evidence/01`–`evidence/21` (baseline, fork symbol map, handler divergence,
  per-round merge dispositions, seams, increment gap, port, adjudication)
- `openspec/changes/adopt-upstream-web-frontend-split/` — the deferred unit:
  design, tasks, and `evidence/deferred-upstream-frontend.md`
- `docs/design/web-layer-module-layout.md` — module layout, the zero-fork-branch
  criterion and its gate entry points
- `doc/master合并到rdai-同步报告-2026-09-18.md` — the sync report (local record
  set; `doc/` is gitignored)
