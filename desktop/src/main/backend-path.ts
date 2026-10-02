/**
 * Where the bundled Python backend lives, in one place.
 *
 * Two callers need the same answer for different reasons: the backend manager
 * starts the HTTP process from it, and the project-execution worker runs
 * ``python -m agent.desktop_local.worker`` with it as the module directory. A
 * second copy of this rule would eventually disagree with the first, and the
 * failure would look like "the sandbox cannot find the worker" -- a runtime
 * problem with a packaging cause.
 */

import path from 'node:path'

export interface BackendPathOptions {
  /** Whether this is an unpackaged source checkout (``!app.isPackaged``). */
  dev: boolean
  /** ``process.resourcesPath`` when packaged; unused in dev. */
  resourcesPath?: string
  /** The compiled module's own directory (``__dirname`` at the call site). */
  moduleDir: string
}

/** The repo root in dev, ``resources/backend`` in an installed app. */
export function resolveBackendPath(options: BackendPathOptions): string {
  if (options.dev) {
    // dist/main -> dist -> desktop -> repo root, which is what a checkout's
    // `python -m agent...` needs on PYTHONPATH.
    return path.resolve(options.moduleDir, '../../..')
  }
  return path.join(options.resourcesPath ?? process.resourcesPath, 'backend')
}
