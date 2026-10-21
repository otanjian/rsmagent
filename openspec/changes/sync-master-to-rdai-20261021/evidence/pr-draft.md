# Merge master into rdai with upstream Web UI

This merge brings `master@ee2dc0521714fc144e0c9923f363147fe865ad2f` into `rdai@c9bd4942261156f56c02ffd65e4d81c6153e1594`, preserving both histories. Shared Web pages use master's modules, templates and interactions; rdai keeps database identity, tenant/owner/resource boundaries, branding and its renamed **Business Workspace**. SAP and master pages retain their own routes and content.

Artifacts use the existing conversation index and personal workspace resolver. Menus use atomic private JSON. The compatible API retains IAM service-account keys, scoped sessions, model authorization and proper 403/409 responses. Skills, model discovery, context snapshots, TLS and upstream Agent/tool/channel fixes are wired to actual fork entry points. No second router, menu table or context panel was introduced.

Web acceptance passed: full fixed-source synchronization gate, 39 real browser scenarios with zero script errors, 376 final core/affected backend cases, real DeepSeek write/read and file preview/download, and two real service-account streaming/non-streaming conversations with usage and isolation. Runtime digest: `391d9c49ac4f6489f8607b6f87c9349f66ac125f90dc420840cd50471132de18`. Persistent evidence: `openspec/changes/sync-master-to-rdai-20261021/evidence/README.md`.

Earlier broad regression evidence remains valid for unchanged code: 3,083 cases/subcases passed, one reproduced target-baseline missing Desktop spec failure, and 12 deferred Windows-native cases. Five historical sidebar/launch and three historical i18n snapshot failures reproduce on the target and are retained rather than reported as passing.

Desktop types/build/native D1–D6/package validation is explicitly deferred. Gitea production jobs are queued and branch protection is absent; no remote CI success or blocking enforcement is claimed. Task 9.4 / inherited GAP-007 and task 12 remain open. Equivalent local Web checks are used as permitted by the approved change; no protections or required tests were removed. Review was performed by the coding agent under the user's autonomous-delivery instruction; no independent human review is claimed.

Preserve merge history; do not squash or rebase. Deployment/migration/recovery notes: `evidence/api-and-deployment.md`. Clients that relied on `user` for continuation must supply `conversation_id`. Revert the eventual PR merge's first parent on a separate rollback branch if necessary; code rollback does not restore database or external effects. No production rollout or desktop release is included.
