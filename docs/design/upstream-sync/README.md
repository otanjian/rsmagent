# Upstream synchronization checks

`capabilities.json` records behavior decisions and evidence. It is not a list of
files that happened to merge. Keep stable IDs, including resolved or explicitly
excluded history. `history.json` preserves the initial unresolved ledger; on
later PRs the gate also compares the ledger in the actual PR base commit.

`source-census.json` pins the upstream Web file tree. `runtime-contract.json`
pins the implementations, dependencies and lifecycle hooks used by the rdai
assembly. `required-tests.json` names mandatory individual cases. A passing
upstream reference module is not evidence for an unloaded fork runtime.

## Run

```sh
npm --prefix tests/browser ci
tests/browser/node_modules/.bin/playwright install chromium
export NODE_PATH="$PWD/tests/browser/node_modules"
# Use the candidate's configured Python interpreter.
export PYTHON_BIN="$PWD/.venv/bin/python"
"$PYTHON_BIN" scripts/check-upstream-sync.py --output artifacts/upstream-sync
```

For a new sync, pass the reviewed full `--source SHA` and the last accepted
`--baseline SHA`. CI supplies the PR base through `UPSTREAM_SYNC_BASE_SHA`.
Changing upstream requires a new census and a per-file mapping: capability
`source.paths`, or `source_reviews` entries with `path`, `classification`
(`behavior`, `dependency`, `reference`), `owner` and repository `evidence` paths.
A changed source without new capability evidence is rejected. Reference modules
must match the declared source or an unchanged pre-existing delta in the fixed
target; changing only the source/hash metadata cannot bless an unknown module.

The gate executes the actual `build_web_app()` assembly, existing route/seam
checks, pytest with JUnit, Node with TAP, and Playwright with a JSON report. It
rejects absent, skipped, xfailed, cancelled, uncollected or failed required cases;
missing browser dependencies fail rather than silently skip. Browser debug
filters are removed from a mandatory run. Candidate hashes include tracked,
unstaged and new nonignored runtime packages, dependency locks, CI files and gate
inputs; a change during execution invalidates the report. Planning/evidence in
`openspec/`, `doc/` and documentation outside this contract directory is excluded,
as are ignored installed dependencies and generated outputs.

`--structure-only` is an investigation command. Its report explicitly excludes
behavior and production acceptance. Even a complete local run does not prove
remote CI branch protection, an installed Desktop/SAP integration, or a deployed
source updater's recovery. Reports list unaccepted capabilities separately.

## Current implementation boundary

The gate is connected to `scripts/check-production.sh`, the existing Gitea
`local-regression` job, and GitHub's `upstream-sync` job. Remote runners and
branch-protection requirements have **not** been verified on this candidate.
A YAML change alone is not delivery acceptance.

The controlled source updater is implemented and its positive, concurrency,
preparation-failure and data-recovery tests are mandatory. An operator must
configure the trusted source and an independently supervised process manager;
unconfigured, Docker, Windows and packaged installs cannot run this updater.
The manager protocol and acceptance boundary are in
`docs/operations/source-update.md`. The isolated Git/venv/HTTP process fixture
proves the update algorithm; it does not certify an arbitrary deployment adapter.

The required browser batch includes actual WSGI/IAM/scheduler dispatch/SQLite
notification integration as well as controlled HTTP UI scenarios. The common
production script rebuilds Desktop, then runs the production Electron container,
preload and IPC against isolated identities. SAP iframe navigation uses the real
scene UI and a controlled SAP origin; live SAP transactions and final installed
packages remain outside that scope.

Read the current candidate report together with capability verification scopes.
Local gate success is not remote merge blocking: repository and organization
runner inventories are empty and branch protections are absent as inspected in
`openspec/changes/restore-upstream-web-parity-and-sync-gates/evidence/implementation/ci-inventory.json`.
