# Implementation and acceptance status

Fixed source: `ee2dc0521714fc144e0c9923f363147fe865ad2f`. Fixed delivery target: `c9bd4942261156f56c02ffd65e4d81c6153e1594`. Candidate branch: `codex/sync-master-to-rdai-20261021-web`, in the independent clone `/Users/jiantan/ai_assistant/rsmagent-sync-master-20261021`.

## Implemented

All 876 upstream changed paths are classified. The 175 actual merge conflicts retain per-hunk decisions and both sides' hashes. The runtime uses master's split Web shell/templates, with one authoritative route registry and the existing fork identity/resource boundaries. No unresolved Git conflict remains. The Web acceptance scope is complete, including real-provider paths.

Shared pages and interactions follow master. The rdai default is 业务工作台 / 業務工作台 / Business Workspace; master conversation (`chat`), rdai Agent directory (`agent-workbench`) and SAP retain their own routes/content. User branding is preserved. Master owns the sidebar/menu arrangement and session panel; historical sidebar launch/recent-preview layout is not restored over it. The session-panel launch control has its own unique ID and team creation returns focus to the actual composer.

Artifacts extend the existing conversation store with exact tenant/owner/file-source boundaries and an index retry watermark. Messages are committed once; retrying an index does not duplicate them. Personal menu JSON uses the existing private application directory and atomic replacement. Unknown legitimate menu entries survive as unavailable entries. IAM service-account keys retain one-time plaintext issuance and scoped API defaults/locks. Skills carry resource identity through creation, file listing and editing. Context usage reuses the source card/snapshot and disables compaction without a live authorized session. Direct TLS is applied before serving.

Additional integration repairs include one database scheduler observer (no duplicate upstream polling), stale identity/session guards, model fallback grants bound to the resolved provider, delayed runtime config publication after atomic disk save, preservation of Agent visibility in the use-range catalog so the file panel opens the member's own writable directory, and the master header language control reflecting the browser-local preference without writing instance configuration.

## Evidence and limits

`results/` contains stable summaries, named-case XML/TAP reports, browser scenario results, build provenance and baseline comparison bindings. It contains no operator credentials or real business data. Browser fixtures use fresh IAM/conversation stores, synthetic users in two tenants, real HTTP routes, actual files and the rendered shipped shell. Notification tests include 205 rows, disconnect/reconnect, two windows and revoked-grant clicks. Synthetic model fixtures are not counted as a real-provider success.

The broad backend run isolates test files in separate processes, with real `web` imported before collection. Some historical test modules install a fake `web` module at import time or replace global config; combining those modules in one process contaminates unrelated tests. File isolation retains every assertion and test ID. Native Chrome browser tests are enabled. The 12 Windows-only integration cases remain explicitly deferred under task 12, with their original skip reasons.

Known baseline findings are retained, not disguised as a green suite:

- `test_capability_matrix.py::DeclarationInvariantTests::test_every_declared_capability_has_a_spec` fails because the target already declares `desktop-remote-web-workbench` without a corresponding spec. The unchanged invariant and target source were verified in an isolated archive. This sync does not fabricate that spec or qualify the desktop slice.
- Five old launch/sidebar frontend cases fail identically against the fixed target: two launch-caret cases in `test_agent_chat_launch_frontend.cjs` and three removed recent-sidebar cases in `test_workbench_menu_grant_frontend.cjs`. Those tests still read the historical monolith. Target blobs for the test inputs are hash-bound in `baseline-bindings.json`; source-to-candidate comparison adds no failure. Current master-layout launch, team persistence, history/navigation, permissions and file flows are covered by the actual loaded modules and browser gate. The historical failures remain visible for maintainer review; they are not reported as passing.

- Three historical i18n snapshot/loader cases also fail on the fixed target. The current runtime coverage test now executes the actual loaded master table and rdai namespaces; 33 translation/appearance cases pass, and the browser checks Simplified Chinese, Traditional Chinese and English without global config writes. The legacy snapshot failures remain recorded in `results/legacy-i18n-baseline.json`.

## Final Web acceptance and review

The operator supplied a working provider credential. Real DeepSeek Web chat completed successful write/read tool calls, an assistant response and the normal SSE end sequence. The physical generated file, automatic artifact card, preview, source conversation, workspace preview and downloaded bytes match. Two separately authorized service accounts completed real streaming/non-streaming tool turns with token usage, isolated same-named conversations, continued their own context, and made a stateless request without inheriting the legacy `user` history. Only synthetic data and redacted summaries are retained.

Acceptance found and repaired three integration defects: relative artifact paths now reuse `personal_default_dir`; the compatible chat route delegates authentication to the existing SAK/database handler instead of the browser-only tenant gateway; and the API emits the intended 403/409 responses. Real HTTP regressions preserve anonymous, conflicting-tenant, ungranted-model and revoked-key denial. Tenant model ceilings remain enforced. The isolated real-model fixture also confines the initializer's legacy environment file to a temporary directory.

The final full synchronization gate passed on runtime digest `391d9c49ac4f6489f8607b6f87c9349f66ac125f90dc420840cd50471132de18`, with matching beginning/end hashes, 39 browser scenarios and zero page errors. The final affected run passed 376 cases across 20 backend/core files with no skips. Earlier broad backend, frontend, i18n and build results apply to unchanged implementation and retain their original provenance. No Desktop qualification or remote CI pass is inferred.

The staged review checks the minimal final fixes, preserved merge parents/history, source classifications, actual screenshots and bundles, schema/recovery notes, and credential exclusion. Review was performed by this coding agent under the user's instruction to execute the recommended path; no independent human approval is claimed. Gitea has no branch protection and its production jobs are queued. The change's approved Web/manual-equivalent scope permits delivery while task 9.4 / inherited GAP-007 and four Desktop tasks remain open. Production rollout and desktop packages are not part of this branch merge.

The initial merge is `6396213e5cf396ad85d895aed65403820865986c` with first parent `c9f80a8ae51ae7e327be18930eb63a3facd119d9` and second parent `ee2dc0521714fc144e0c9923f363147fe865ad2f`. Follow-up fixes preserve that history. Final PR/delivery details are recorded after merge in `results/delivery-results.json`. The change remains active for its explicitly deferred tasks.

## Delivered

PR #2 merged as `c7b981b0068e817cd0cef4fab28f19f0385e2bf1`; its tree equals the accepted head and retains both fixed source and target ancestry. Post-merge Web smoke restarted the isolated server and repeated real model write/read, artifact preview/jump, workspace preview and byte-exact download. All passed. See `results/delivery-results.json`. The checklist is 53/58: only inherited CI enforcement and four explicitly deferred Desktop tasks remain.
