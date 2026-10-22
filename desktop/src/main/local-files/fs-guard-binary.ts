/**
 * Where the two file helpers live, and how a child is spawned for each.
 *
 * Change ``add-windows-local-project-access`` (task 3.3, design D1/D3).
 *
 * ## Two binaries, because one of them cannot write
 *
 * D1 splits the help in two, and the split is enforced by the build rather than
 * by a runtime check:
 *
 * * ``fs-guard``         -- read-only: ``list``/``stat``/``read``/``search``.
 * * ``project-fs-guard`` -- the same frames plus ``write``/``edit``/``mkdir``.
 *   It is a separate Cargo binary built with the ``write-ops`` feature, so the
 *   read-only helper has no write operation *in it at all*.
 *
 * Resolution is therefore per **role**: the reader gets the read-only binary, and
 * only a caller that has already established a write-capable grant asks for the
 * write one. Asking for the write binary is a deliberate act, not a default, so a
 * read path cannot acquire write capability by resolving a path.
 *
 * ## The ``.exe`` suffix
 *
 * The previous version joined ``fs-guard`` onto candidate directories with no
 * suffix, which cannot resolve anything on Windows: the built artifact is
 * ``fs-guard.exe``. The design records this exactly --
 *
 * > 开发态构建候选未带.exe，打包候选为resources/fs-guard且先做accessSync检查，
 * > 不能假设自动补后缀，须统一产物名后真包验证
 *
 * -- so the suffix is applied explicitly and per candidate rather than assumed
 * away. The packaged form stays a bare name because that is what
 * ``electron-builder``'s ``extraResources`` produces: ``from`` names the built
 * artifact and ``to: 'fs-guard'`` renames it, so the macOS ``Resources/fs-guard``
 * and the Windows ``Resources/fs-guard.exe`` are both found by the same entry.
 *
 * Resolution is explicit and ordered, and a build that has no helper says so by
 * returning ``null`` rather than failing at the first read: "this build cannot
 * read local files" is a fact the UI can show, and an empty directory would be a
 * lie.
 */

import * as fs from 'fs'
import * as path from 'path'

import type { GuardChild } from './fs-guard'

/**
 * Which helper a caller needs.
 *
 * ``read`` is the read-only whitelist; ``write`` adds ``write``/``edit``/``mkdir``.
 * There is no ``both``: a caller knows which one it is doing, and a single answer
 * would let a read path hold a write-capable handle.
 */
export type GuardRole = 'read' | 'write' | 'launcher'

/** The binary's base name, before any platform suffix. */
const BASE_NAME: Record<GuardRole, string> = {
  read: 'fs-guard',
  write: 'project-fs-guard',
  // Not a file helper: the native launcher that confines a script's process tree.
  // Resolved here anyway because 'which binary of ours lives where, in a build
  // tree and in an install' is one question with one answer, and answering it in
  // two places is how a packaged build ends up missing exactly one of them --
  // which is what happened to the file helpers.
  launcher: 'script-launcher',
}

/**
 * The platform's executable suffix for a helper.
 *
 * Exported because the dev-tree candidates and the tests both need to agree with
 * it, and "which name does this build produce" is exactly the question that was
 * answered wrongly before.
 */
export function guardExecutableName(role: GuardRole, platform: string): string {
  const base = BASE_NAME[role]
  return platform === 'win32' ? `${base}.exe` : base
}

/** Candidate absolute paths for one role's helper, most-packaged first. */
export function guardBinaryCandidates(options: {
  role?: GuardRole
  resourcesPath?: string
  appPath?: string
  env?: NodeJS.ProcessEnv
  /**
   * Injected so the Windows candidate list can be asserted on any host. A test
   * that could only check ``.exe`` handling on Windows would leave the bug it
   * exists to catch untested on the machines most likely to introduce it.
   */
  platform?: string
}): string[] {
  const env = options.env ?? process.env
  const role = options.role ?? 'read'
  const platform = options.platform ?? process.platform
  const name = guardExecutableName(role, platform)
  const candidates: string[] = []

  const override = (env.COW_FS_GUARD || '').trim()
  if (override && role === 'read') candidates.push(override)
  // The write helper gets its own override so a diagnostics run can point one at
  // a build tree without also redirecting the reader.
  const writeOverride = (env.COW_PROJECT_FS_GUARD || '').trim()
  if (writeOverride && role === 'write') candidates.push(writeOverride)

  // A build tree can be redirected wholesale, which is how the launcher tests drive
  // a binary they just built without pretending to be an installed app.
  const launcherOverride = (env.COW_SCRIPT_LAUNCHER || '').trim()
  if (launcherOverride && role === 'launcher') candidates.push(launcherOverride)

  // The packaged location, as ``extraResources`` leaves it: a bare name, because
  // ``to: 'fs-guard'`` renames the artifact. Checked without a suffix on both
  // platforms, and with the platform suffix as a second chance, so a packaged
  // layout that kept the built name also resolves.
  if (options.resourcesPath) {
    candidates.push(path.join(options.resourcesPath, BASE_NAME[role]))
    candidates.push(path.join(options.resourcesPath, name))
  }

  if (options.appPath) {
    // The build tree. Here the artifact keeps the name cargo produced, so the
    // platform suffix is required rather than optional. The crate directory differs
    // per role, so it is named rather than assumed.
    const crate = role === 'launcher' ? 'script-launcher' : 'fs-guard'
    for (const build of ['release', 'debug']) {
      candidates.push(
        path.join(options.appPath, 'native', crate, 'target', build, name),
        path.join(
          options.appPath, '..', 'desktop', 'native', crate, 'target', build, name,
        ),
      )
    }
    // A plain ``native/<crate>`` checkout that was built in place.
    candidates.push(path.join(options.appPath, 'native', crate, name))
  }
  return candidates
}

/** The first candidate that exists and is executable, or null. */
export function resolveGuardBinary(options: {
  role?: GuardRole
  resourcesPath?: string
  appPath?: string
  env?: NodeJS.ProcessEnv
  platform?: string
  exists?: (candidate: string) => boolean
}): string | null {
  const exists = options.exists ?? ((candidate: string) => {
    try {
      fs.accessSync(candidate, fs.constants.X_OK)
      return true
    } catch {
      return false
    }
  })
  for (const candidate of guardBinaryCandidates(options)) {
    if (exists(candidate)) return candidate
  }
  return null
}

/**
 * Spawn a helper on its private stdio channel.
 *
 * ``stdio: ['pipe','pipe','pipe']`` is the whole interface: no shell, no
 * arguments, no environment inherited beyond what the OS gives every child --
 * a helper whose job is to be untrusted cannot be steerable by its command line.
 * The role is expressed by *which binary* is spawned, never by a flag, because
 * the read-only build has no write operation to switch on.
 */
export function spawnGuardProcess(binaryPath: string): GuardChild {
  const { spawn } = require('node:child_process') as typeof import('node:child_process')
  const child = spawn(binaryPath, [], { stdio: ['pipe', 'pipe', 'pipe'] })
  return child as unknown as GuardChild
}
