# CI status — required Web checks enforced

Task 9.4 completed on 2026-10-21. The repository-scoped runner `rsmagent-web-sync-macos-arm64` executes `.gitea/workflows/web-readiness.yml` on pull requests to rdai. Its required context is `Web readiness / Web required behavior (pull_request)`.

- [Run 745](https://rd.rsmxm.com.cn/Gitea/rsmai/rsmagent/actions/runs/745) passed on `479face2b340f440b1a3a59d2ff38b7b95b65296`: all required cases, 39 real browser cases, zero page errors, stable digest `a36b9c157b8d455119ee7a69cbd346fe38432e47bf049d88a7953312465b80aa`. The uploaded artifact was downloaded from the Gitea UI and its report verified. Gitea's v1 artifact list returns an empty list for this V3 artifact; the UI exposes the retained artifact correctly.
- [Run 746](https://rd.rsmxm.com.cn/Gitea/rsmai/rsmagent/actions/runs/746) deliberately skipped three required startup cases on disposable PR #5. The gate failed specifically on those three cases. Under the protected validation branch, a real merge request returned HTTP 405, `Not all required status checks successful`.
- The skip was reverted in `c29ce0d5583960f86b82caa6444b8064b7b8ff88`. [Run 747](https://rd.rsmxm.com.cn/Gitea/rsmai/rsmagent/actions/runs/747) passed, and PR #5 merged normally under the **unchanged** protection policy. No forced merge or temporary check removal was used.
- The verified policy was applied to `rdai`: required context enabled; direct pushes, force pushes, bypass allowlists and administrator merge override disabled; outdated branches blocked. The selected enforcement fields were compared with the validation policy after creation.

Evidence: [results/ci-enforcement](results/ci-enforcement). The runner uses an isolated HOME, cache and working directory, with a persistent per-user LaunchAgent. Its repository registration credential is stored outside Git in a mode-0600 file. Official runner and action versions are pinned; the V3 artifact uploader retains the Gitea deployment subpath.

Older `production-readiness.yml` jobs still request the independent `macos-production` / `linux-production` labels. Their queued status is not presented as a pass. This closes the inherited Web CI enforcement gap referenced by GAP-007 / task 7.3; it does not establish unrelated full production or image acceptance.

## Prior delivery observations (historical)

# CI status (task 9.4 remains open)

The candidate's `.gitea/workflows/production-readiness.yml` declares push/PR triggers for `main` and `rdai`, with `macos-production` and `linux-production` runners. It invokes the existing production checks; those include Desktop/image work beyond this turn's agreed Web validation. `.github/workflows/test.yml` includes the upstream-sync job and lists `master`, `rdai` and `main` for the configured branch trigger.

Repository YAML alone does not prove runner availability, execution, protected-branch required checks, or actual failed-PR blocking and subsequent recovery in Gitea. No such remote acceptance is claimed. The existing GAP-007 / restore-upstream-web-parity-and-sync-gates task 7.3 continues to own this evidence gap. Task 9.4 is intentionally unchecked; no branch protection was removed and no global production test list was emptied.

Read-only Gitea API verification at 2026-10-21 11:48 UTC is recorded in [remote-ci-status.json](results/remote-ci-status.json). The server reports version 1.27.3, Actions enabled and **no branch protection rules**. The repository-level runner list is empty; this does not establish the absence of inherited runners. Target push run 728 for `c9bd4942261156f56c02ffd65e4d81c6153e1594` and its `local-regression` / `final-linux-image` jobs are queued, with `runner_id=0`; their requested labels are `macos-production` and `linux-production`. These results establish the current enforcement gap, not a successful run or failed-PR blocking/recovery. No runner or repository setting was changed. A fresh fetch confirmed both fixed source and target are unchanged.

The local Web equivalent is recorded in `results/required-gate/report.json`, the core/backend/frontend results, and `web-acceptance.md`. The real-model portion now passes; see results/real-provider-status.json. Final review uses the exact candidate and equivalent local Web evidence. Desktop remains separately deferred.

The blocked draft PR #2 triggered actual `pull_request` workflow run 729 for candidate merge `6396213e5cf396ad85d895aed65403820865986c`. At 11:58 UTC it was queued; this verifies PR triggering, not runner execution or required-check enforcement. Gitea reported the PR as draft and not mergeable, and the target branch remained at the fixed target SHA. See [draft-remote-check.json](results/draft-remote-check.json). The post-commit structural check passed with the same runtime digest as the previously completed full behavior gate; [draft-postcommit-structure.json](results/draft-postcommit-structure.json) is explicitly structural-only and does not replace the full gate or real-provider acceptance.

After PR #2 merged, target push workflow 732 was triggered on `c7b981b0068e817cd0cef4fab28f19f0385e2bf1` and remained queued at observation. Branch protections remain absent. See `results/postmerge-ci-status.json`; task 9.4 remains open. The final local full gate and real-provider acceptance passed, and the merged tree is byte-identical to the accepted PR head.
