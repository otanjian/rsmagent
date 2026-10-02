/**
 * Where the ``fs-guard`` helper lives, and how a child is spawned for it.
 *
 * Change ``fix-desktop-local-context-and-tool-calls`` (task 2.4). Resolution is
 * explicit and ordered, and a build that has no helper says so by returning
 * ``null`` rather than failing at the first read: "this build cannot read local
 * files" is a fact the UI can show, and an empty directory would be a lie.
 *
 * The search order is the packaged location first (``extraResources`` puts the
 * signed binary next to the app), then the build-tree location for a dev run,
 * then an override for diagnostics.
 */

import * as fs from 'fs'
import * as path from 'path'

import type { GuardChild } from './fs-guard'

/** Candidate absolute paths for the helper, most-packaged first. */
export function guardBinaryCandidates(options: {
  resourcesPath?: string
  appPath?: string
  env?: NodeJS.ProcessEnv
}): string[] {
  const env = options.env ?? process.env
  const candidates: string[] = []
  const override = (env.COW_FS_GUARD || '').trim()
  if (override) candidates.push(override)
  if (options.resourcesPath) candidates.push(path.join(options.resourcesPath, 'fs-guard'))
  if (options.appPath) {
    candidates.push(
      path.join(options.appPath, 'native', 'fs-guard', 'target', 'release', 'fs-guard'),
      path.join(options.appPath, 'native', 'fs-guard', 'target', 'debug', 'fs-guard'),
      path.join(options.appPath, '..', 'desktop', 'native', 'fs-guard', 'target', 'release', 'fs-guard'),
      path.join(options.appPath, '..', 'desktop', 'native', 'fs-guard', 'target', 'debug', 'fs-guard'),
    )
  }
  return candidates
}

/** The first candidate that exists and is executable, or null. */
export function resolveGuardBinary(options: {
  resourcesPath?: string
  appPath?: string
  env?: NodeJS.ProcessEnv
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
 * Spawn the helper on its private stdio channel.
 *
 * ``stdio: ['pipe','pipe','pipe']`` is the whole interface: no shell, no
 * arguments, no environment inherited beyond what the OS gives every child --
 * a helper whose job is to be untrusted cannot be steerable by its command line.
 */
export function spawnGuardProcess(binaryPath: string): GuardChild {
  const { spawn } = require('node:child_process') as typeof import('node:child_process')
  const child = spawn(binaryPath, [], { stdio: ['pipe', 'pipe', 'pipe'] })
  return child as unknown as GuardChild
}
