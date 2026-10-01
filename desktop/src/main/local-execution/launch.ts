/**
 * The launch seam: a verified grant plus an interpreter in, a confined worker
 * out (task 4.4).
 *
 * The sandbox module knows how to write a profile; this module knows *what to
 * put in it*. The separation matters because the read allowances cannot be
 * guessed from the grant: they come from probing the interpreter that will
 * actually run (see `interpreter.ts`). Wiring that here means the production
 * path and the test path build the same plan, rather than the test quietly
 * doing the work the app forgot to do.
 */

import type { LaunchPlan, LaunchRefusal } from './sandbox';
import { buildLaunchPlan } from './sandbox';
import { scrubWorkerEnv } from './env';
import { type InterpreterProbe, interpreterReadPaths } from './interpreter';

/** What a verified local-project grant names. Paths are already resolved. */
export interface LocalGrant {
  /** The project root the user opened. The only writable user location. */
  projectRoot: string;
  /** The run's private temp directory. */
  tempRoot: string;
  /** Read-only skill caches prepared for this run. */
  skillRoots?: readonly string[];
}

export interface GrantLaunchOptions {
  platform: NodeJS.Platform;
  grant: LocalGrant;
  /** The probed interpreter, or `null` when none could be found. */
  interpreter: InterpreterProbe | null;
  /** Whether `/usr/bin/sandbox-exec` exists; only consulted on darwin. */
  seatbeltAvailable?: boolean;
  /** The working directory for the worker: where the Python package lives. */
  backendPath: string;
  /**
   * The parent environment, which this function scrubs.
   *
   * Deliberately not a pre-scrubbed `env`: the temp variables must point at the
   * *grant's* temp root, and a caller who hands in an environment has already
   * had the chance to get that wrong. Scrubbing here means the worker's `TMPDIR`
   * is correct by construction.
   */
  baseEnv: NodeJS.ProcessEnv;
}

/**
 * The plan, or the refusal that must be shown instead of one.
 *
 * There is no third outcome. A caller that cannot handle a refusal cannot fall
 * back to spawning the worker directly, because there is nothing else here that
 * spawns anything.
 */
export function buildGrantLaunchPlan(
  options: GrantLaunchOptions,
): LaunchPlan | LaunchRefusal {
  const { grant, interpreter } = options;
  // A source checkout runs the worker as a module, so the directory that Python
  // package lives in has to be readable -- and it sits outside both the
  // virtualenv and the project. An installed app has no such directory: its code
  // is inside the bundle, which the probe already named as a read path. Naming
  // the backend path in both cases is harmless and omitting it stops the worker
  // before the handshake in the checkout case.
  const runtimeReadPaths = [
    ...(interpreter ? interpreterReadPaths(interpreter) : []),
    ...(interpreter?.frozen ? [] : [options.backendPath]),
  ];
  return buildLaunchPlan({
    platform: options.platform,
    pythonPath: interpreter?.pythonPath,
    workerArgs: interpreter?.runtime?.args,
    seatbeltAvailable: options.seatbeltAvailable,
    paths: {
      projectRoot: grant.projectRoot,
      tempRoot: grant.tempRoot,
      skillRoots: grant.skillRoots,
      // Without these the interpreter dies before the handshake, which is
      // indistinguishable from "the worker crashed".
      runtimeReadPaths,
    },
    workingDirectory: options.backendPath,
    env: scrubWorkerEnv(options.baseEnv, {
      tempRoot: grant.tempRoot,
      extra: {
        // The worker refuses a handshake naming a different root than the one
        // confined here, so this must be the grant's root and nothing else.
        COW_DESKTOP_WORKER_ROOT: grant.projectRoot,
        PYTHONPATH: options.backendPath,
      },
    }),
  });
}
