# Contributing to RongAI

Thanks for taking the time to contribute! 🎉 RongAI is built by a global
community, and contributions of all sizes are welcome — from typo fixes to new
features.

## Language policy

To keep the project accessible to a global community, **please write issues,
pull requests, code comments, and commit messages in English.**

> 为方便全球开发者协作，请尽量使用**英文**提交 issue、PR、代码注释与
> commit message。不必担心英文不完美——表达清楚即可，工具翻译也完全没问题。感谢理解 ❤️

## Reporting issues

Found a bug or have an idea? [Open an issue](https://github.com/zhayujie/CowAgent/issues/new/choose).

Before opening one, please search existing issues (including closed ones) to
avoid duplicates, and make sure you're on the latest version.

## Submitting a pull request

1. **Fork** the repo and create a branch from `master`
   (e.g. `feat/web-search`, `fix/telegram-reconnect`).
2. Make your change. Keep it focused — one logical change per PR.
3. Follow the existing code style. Write comments and docstrings in English.
4. Run the app locally to confirm your change works.
5. Open a PR with a clear title and a short description of **what** and **why**.

We keep the bar friendly: clear, focused, and working is enough. Maintainers are
happy to help polish details during review.

### Commit & PR titles

Use a short, imperative summary. The [Conventional Commits](https://www.conventionalcommits.org/)
style is preferred but not required:

```
feat: add web search tool
fix: reconnect Telegram websocket on timeout
docs: clarify Docker setup
```

## Development setup

See the [Install from Source](https://www.rsm.global/china/zh-hans)
guide. In short:

```bash
git clone https://github.com/zhayujie/CowAgent.git
cd CowAgent
pip install -r requirements.txt
pip install -e .
cow start
```

## Code of conduct

Be respectful and constructive. We want RongAI to be a welcoming place for
everyone.

<!-- =======================================================================
     FORK-ONLY SECTION (RongAI fork of CowAgent). Owned by this fork's
     maintainers, not upstream; keep it at the end of the file so an upstream
     merge that edits the sections above cannot collide with it,
     and vice versa. Tasks 9.1/9.5.
     ======================================================================= -->

## Upstream sync (fork maintainers)

This repository is a long-lived fork of CowAgent's `master`. Upstream keeps
moving, so syncing is a routine operation, not a one-off migration.

**Cadence.** Sync after every upstream release, and at least weekly. A sync is
always reviewed by a human before it is committed: the fork carries a tenancy
and authorization model upstream does not have, so a clean textual merge is not
evidence that the *semantics* still line up (see
`openspec/changes/fork-decoupling-and-tenant-hardening/` for the seams that make
this cheap, and `scripts/conflict-baseline.txt` for the conflicts we expect).

```bash
# Rehearsal: fetches, attempts the merge, reports conflicts and baseline drift,
# then aborts the merge. It never commits and never pushes.
scripts/sync-from-master.sh                # defaults to origin/master
scripts/sync-from-master.sh upstream master # or name the remote/branch
# exit 0 -> merged cleanly: review the diff, run the tests, commit yourself
# exit 1 -> conflicts: resolve against the seams, then commit yourself
# exit 2 -> could not run (dirty tree, fetch or baseline failure)
```

**Rerere.** Enable git's conflict-reuse cache once per clone; recorded
resolutions are replayed automatically on the next sync, which is what makes
repeatedly re-resolving the same seam unnecessary:

```bash
git config --local rerere.enabled true   # per-clone; the script never edits
git config --local rerere.autoupdate true
```

(`scripts/sync-from-master.sh` deliberately does not set this for you: a script
that silently writes your git configuration is a surprise, not a convenience.)

**Human review checklist.** After a rehearsal, before committing:

1. `scripts/sync-from-master.sh` reported no *new* conflict file. A new one means
   an upstream edit crossed a seam — fix the seam, then update
   `scripts/conflict-baseline.txt`.
2. Any baseline conflict that disappeared is explained (the seam worked, or
   upstream moved). Do not silently drop a baseline entry.
3. The five deliberate removals are re-confirmed, not re-applied blindly
   (`scripts/conflict-baseline.txt`, "Deliberate removals").
4. `scripts/check-route-coverage.py`, `scripts/check-web-module-seams.py` and the
   test suite pass (see §10 of the change's `evidence.md` for the exact
   commands). The seam gate is the one that matters after an upstream merge:
   it fails when a fork symbol has been written into an upstream module, which
   is how the next sync turns back into a whole-file conflict.


### Keeping `master` and `rdai` independently mergeable

Keep generic changes and tenant-specific changes in separate commits when
preparing a PR. For the simplification work, the boundary is:

- `channel/web/static/js/views/tasks-modal.js` adds a generic, optional
  `registerTaskModalAdapter({ dropdown })` hook. Without an adapter it uses
  the original `initDropdown`. This small change can be cherry-picked to
  `master` independently; task permissions and delivery rules stay in
  `static/js/fork/tasks-console.js`. Do not replace the global dropdown.
- `channel/web/api/` and `channel/web/core/` retain upstream implementations.
  The rdai model handler reuses five explicit, pure value transforms from the
  upstream handler; it does not inherit HTTP methods or authorization. Review
  these named transforms when upstream changes their contract.
- Ordinary fork helpers import their owner modules directly. The remaining
  `web_channel` imports are compatibility/injection seams; preserve their
  existing callers while migrating a seam deliberately. New helpers should
  not route dependencies back through the HTTP entry module.
- `auth/`, `integrations/external/`, `Scene/` and personal memory contain rdai
  policy. Console display projection lives in `auth/console_projection.py`;
  authorization and database transactions remain in the identity service.
  Merge upstream algorithms into the scene modules using their recorded
  source hashes and explicit host/resource imports.

The personal-maintenance upgrade appends migration 46. Existing windows are
qualified by their opening member, without changing connection IDs or the
migration ledger's aggregate scope keys. Append future migrations; do not
rewrite the recorded upgrade. Resolve migration-number conflicts before
shipping branches that independently appended migrations.

A master merge must retain rdai route policy and owner filtering even when an
upstream handler has the same name. The task-modal drift guard removes only the
small adapter hook before comparing the original upstream digest, so unrelated
upstream changes still require review. Run the route and module seam checks,
then the affected behavior tests, including `tests/test_rdai_simplification.py`.
