# 10 — Phase 2 merge: conflict classification and dispositions

First real merge attempt (not a rehearsal). Isolated clone, branch HEAD extends
`rdai`:

    runner   /tmp/merge-20260919-020204/repo   (git clone --no-hardlinks of the workspace)
    source   origin/master @ 8f1b19f1
    target   origin/rdai   @ b5c5090f  (is an ancestor of HEAD)
    base     e5e2a52d
    branch   c47aa3c8 (rdai + 7 change commits)

46 conflicted paths, matching the rehearsal in `06-rehearsal-after-phase1.md`.
`git ls-files -u` shows 46 paths in conflict; all 21 rows of
`scripts/conflict-baseline.txt` are present, so there is no baseline drift in the
"file disappeared" sense — only new files.

## Resolved in this attempt (9)

| path | status | disposition | rationale |
|---|---|---|---|
| `.gitignore` | UU | `keep-fork` | Fork-only ignore section kept, and upstream's new `.obsidian/` rule placed **above** the fork banner. The banner's own text requires being last, so upstream's rule cannot go inside it. |
| `README.md` | DU | `keep-deletion` | Standing decision (baseline). |
| `docs/ja/README.md` | DU | `keep-deletion` | Standing decision. |
| `docs/zh/README.md` | DU | `keep-deletion` | Standing decision. |
| `docs/zh/README-Hant.md` | DU | `keep-deletion` | Standing decision. |
| `desktop/src/renderer/src/components/PermissionSelector.tsx` | DU | `keep-deletion` | Standing decision; `test_execution_permission_ui.cjs` asserts its absence. |
| `channel/web/static/js/console.js` | UD | `keep-fork` (deferred) | Upstream deleted it; deleting now would discard the fork's console. Task 4.4h forbids deletion until the 98 frontend adjudications land, so the fork's side is kept and this deferral is registered. |
| `channel/web/static/css/console.css` | UD | `keep-fork` (deferred) | Same. |
| `channel/web/chat.html` | UU | `keep-fork` (deferred) | The fork's shell loads `console.js`; adopting upstream's shell now would load upstream's modules while the fork's customization is still unported. Deferred with the two above as one unit (design D5/D6). |

The three frontend deferrals are a **single decision**, not three: the fork's
shell and monoliths are one coupled unit. Deferring it means upstream's split
frontend (`static/js/{core,chat,views}/*`, `static/css/*`, `templates/**`,
`core/template.py`) lands in the tree as new files that nothing loads yet.

## Remaining (37), grouped by what the disposition requires

### A. Docs — `merge-docs`, and not mechanical (8)

`docs/intro/{architecture,index}.mdx`, `docs/intro/features.mdx` (drift),
`docs/ja/intro/{architecture,index}.mdx`, `docs/ja/intro/features.mdx` (drift),
`docs/zh/intro/{architecture,index}.mdx`.

Baseline says "both-sides doc edit, no code impact". That is true of the *code*,
but the fork's doc edits are **not** renamable-by-substitution: taking upstream's
text and swapping `CowAgent` → `容大AI` would silently drop real fork content —

- `docs/intro/architecture.mdx` — the fork rewrote the permission paragraph for
  database mode: on a multi-tenant install `agent_permission_mode` is read-only
  and what a session may run is decided by the caller's role resource
  authorization. Upstream's text still describes the single-tenant global
  default. Rebranding upstream's paragraph would delete the fork's capability
  statement.
- `docs/intro/index.mdx` — the fork replaced the "Try Online" card with "Try the
  Upstream Service Online" and swapped the logo/video assets. The fork does not
  operate that hosted service, so upstream's card is wrong for the fork.

So these need the same intent-level treatment as code: upstream's new content,
with the fork's database-identity and fork-service edits re-applied on top.

### B. Backend behavioural — baseline `seam:` rows (7)

| path | baseline disposition |
|---|---|
| `agent/memory/conversation_store.py` | `seam:6.1-6.11` — constraint-level seam; composite PK |
| `agent/tools/scheduler/integration.py` | `seam:8.15-8.16,3.4` — converge to upstream global service + identity seam |
| `app.py` | `seam:6.11,8.9,2.3,4.1-4.3` — keep upstream migration; fork guard via extension hook |
| `channel/channel_instances.py` | `seam:8.13,7.3` — partitioned symbol registration |
| `channel/web/web_channel.py` | `seam:4.1-4.12,3.1-3.7,5.1-5.4,6.1-6.4,7.1-7.7` — retire shared-password/HMAC; keep upstream `_import_local_file` |
| `desktop/src/renderer/src/api/client.ts` | `seam:2.11-2.12,4.19,8.1,8.4` — database session Bearer; refuse `cow_auth_token` |
| `tests/test_scheduler_web_update.py` | `seam:8.17,3.4,3.5` — revise with upstream behaviour change |

`web_channel.py` is the one phase 1 reshaped: 25,397 of 25,659 bytes conflict, but
both sides are now small enough to resolve by composition (upstream's URL table +
the fork's `channel/web/fork/**` imports) rather than by merging text — see
`06-rehearsal-after-phase1.md`.

### C. Drift needing a disposition (19)

Backend / desktop: `agent/admin.py`, `agent/protocol/agent_stream.py`,
`agent/registry.py`, `agent/tools/scheduler/task_store.py`, `config.py`,
`desktop/build/notarize-dmg.sh` (UD — upstream deleted, fork modified),
`desktop/package.json`, `desktop/src/main/preload.ts`,
`desktop/src/renderer/src/types.ts`.

Web: `channel/web/README.md` (DU), `channel/web/static/vendor/README.md` (DU).

Per the design D7 extension, `channel/web/README.md` is **not** resolved as a
blanket README `keep-deletion`. The standing "fork ships no READMEs" decision
rests on upstream's READMEs describing a different product; this file is the
developer documentation for the module split the fork is now adopting. Deleting
it discards upstream's explanation of the layout. Recorded as an open decision
rather than resolved either way.

Tests (11): `test_agent_web_management.py`, `test_claude_thinking.py`,
`test_config_subagent_toggle.py`, `test_dashscope_provider.py`,
`test_doc_edit.py`, `test_knowledge_web.py`, `test_models_handler.py`,
`test_openai_chat_api.py`, `test_qianfan_provider.py`,
`test_web_chat_content_type.py`, `test_workspace_edit.py`. Task 3.10 requires
each to be confirmed as "the capability it tests entered the target version" and
retargeted to the new module locations — not deleted, and not weakened to pass.

## Gates not yet run

Nothing is committed. The merge is left in progress in the runner so the
remaining 37 can be resolved against this state. Task 3.9's regression suite and
route-coverage check cannot run meaningfully until the conflict set is closed,
and the `web_channel.py` composition above all else.
