# 19 — The desktop token seam: upstream's per-launch secret is retired in the fork

## What upstream brought

Upstream hands the Electron shell a per-launch secret and lets the **renderer**
present it back:

| file | upstream addition |
| --- | --- |
| `desktop/src/main/python-manager.ts` | `desktopToken` field, `COW_DESKTOP_TOKEN` in the child env, `getDesktopToken()` |
| `desktop/src/main/index.ts` | `ipcMain.handle('get-desktop-token', …)` |
| `desktop/src/main/preload.ts` | `getDesktopToken: () => ipcRenderer.invoke('get-desktop-token')`, plus `getPathForFile` and the runtime `webUtils` lookup |
| `desktop/src/renderer/src/types.ts` | `getDesktopToken?: () => Promise<string>` |
| `desktop/src/renderer/src/api/client.ts` | `desktopTokenPromise` / `getDesktopToken()`, `isLoopbackBackend()`, `importLocalFile()` — a JSON `{"local_path": …}` POST carrying `X-Cow-Desktop-Token` |

The mechanism exists for one feature: importing an attachment by local path
instead of shipping its bytes over HTTP.

## Why the fork cannot take it as-is

The fork's desktop identity is the opposite arrangement (design **D8**: the
session credential lives in the **main** process, the renderer sees only the
broker's desensitized projection). Its own regression suite pins that:

- `tests/test_desktop_context_frontend.cjs` → *"the preload exposes only
  narrowed broker channels"* asserts `preload.ts` does **not** match
  `/desktopToken|desktop-token|getToken|authToken/`.
- The same file asserts `client.ts` carries no credential, no `withToken`,
  no `Authorization` header, and that every request path goes through
  `desktopContext`.

Measured on the merge tree: the fork's `HEAD` has **zero** occurrences of this
seam, while the merge re-introduced it in all five files, and
`tests/test_desktop_context_frontend.cjs` went from 0 to 9 failures.

The feature is also not servable in the fork: the path-import branch lives in
upstream's `channel/web/core/channel.py` (`local_path` handling), and the fork's
parallel `UploadHandler` (`channel/web/fork/handlers/files.py`) has no
`local_path` route. Keeping the renderer side would post a body the served
handler does not understand.

## Resolution

`keep-fork` for the seam, `keep-upstream` for everything around it:

- removed the five renderer/main-process sites above (83 lines, pure deletion);
- kept upstream's unrelated increments in the same files — the runtime
  `webUtils` lookup, `notify(… force?: boolean)`, the named `UploadResult`
  interface, the upload retry loop now living on the fork's
  `desktopContext.sendForm`, and `types.ts`'s `tool_retrieval` fields;
- left `getPathForFile` in the preload/types as upstream's additive enabler: it
  exposes a local path, not a credential. Wiring it to the fork's own
  local-import route (`channel/web/project_import.py`, which authenticates by
  loopback **and** its own `local_import.token`, published `0600` next to the
  data root) is a feature port, not a merge resolution: the main process would
  have to read that file and carry the token, since the renderer holds none.
  Recorded with the handler-increment gap in `18-backend-increment-gap.md`.

## Verification

| check | result |
| --- | --- |
| `tests/test_desktop_context_frontend.cjs` | 14 passed, 0 failed (was 9 failed) |
| `node --test tests/*.cjs tests/support/*.cjs` | 47 unique failures — byte-identical to the fork `HEAD` baseline (0 merged-only, 0 base-only) |
| `git grep 'getDesktopToken\|desktopToken\|COW_DESKTOP_TOKEN\|X-Cow-Desktop-Token' -- desktop/src` | no matches |

Note: the node suite is a *frontend* suite. Its behavioural half transpiles the
real `.ts` sources and needs `desktop/node_modules/typescript`, so the comparison
was run with the workspace's `desktop/node_modules` temporarily linked in (the
clone ships no dependencies); the link was removed before staging anything.

## Post-merge audit of the seam (2 assertions that the resolution is *safe*)

Removing a renderer-visible secret is only safe if nothing on the served path
still demands it. Both halves were re-checked against the committed merge tree:

| question | finding |
| --- | --- |
| does the fork edit upstream's Python side? | no: `channel/web/core/channel.py` and `channel/web/core/_common.py` are **byte-identical** to `origin/master` (`git diff origin/master:<path> <path>` is empty). The `_import_local_file` route and `_desktop_token_matches()` are upstream's, unmodified — no fork branch lives in an upstream module (task 4.7's criterion) |
| is the removed token still required by anything the fork serves? | no: `_desktop_token_matches()` reads `COW_DESKTOP_TOKEN` and **fails closed** when it is unset, so the route it guards can only answer inside upstream's namespace (`build_app()` + `channel/web/api/**`). The fork's live application is `build_web_app()`, and its `/upload` resolves to `channel/web/fork/handlers/files.py::UploadHandler`, which has a `POST` and no `local_path` branch at all — so the desktop sending no token is correct, not a silent 403 |

Consequence for the runtime: the fork's desktop falls back to the multipart
upload it has always used. Upstream's import-by-path stays a **feature port**
(evidence/18), and its two ends are now explicit — the renderer needs a path
(`getPathForFile`, kept) and the main process needs a token it can read
(`local_import.token`, published `0600`; the fork never reads it yet).
