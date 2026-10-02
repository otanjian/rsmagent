/**
 * The environment a local worker is started with (task 4.7).
 *
 * Three independent reasons this exists:
 *
 * 1. **The worker must not inherit authority it was not granted.** It is a
 *    separate process precisely so the OS can confine it, and an OS boundary
 *    does nothing about inherited secrets: a bearer token, a session cookie or
 *    the identity database path sitting in the parent's environment would be
 *    readable by any script the worker runs. The sandbox grants no network, but
 *    a token is still a credential in a place it has no business being.
 * 2. **It must still be able to run a tool.** An over-stripped environment
 *    breaks `PATH` lookups, `HOME`-relative config and (on Windows) process
 *    creation entirely, so this is an allowlist with a named deny pass, not a
 *    blanket "drop everything with the word key in it".
 * 3. **The temp directory must be one the run may actually write.** This is not
 *    hygiene, it is correctness: `TMPDIR` is inherited from the parent, whose
 *    value (`/var/folders/.../T`) is *outside* the sandbox's writable grant, so
 *    `tempfile.gettempdir()` finds nothing usable and every tool that touches a
 *    temp file fails with `[Errno 2] No usable temporary directory found`. The
 *    bash tool alone hits this the moment output exceeds its inline limit,
 *    because it spills the full output to a temp file.
 *
 * The deny pass runs last and wins: `COW_IDENTITY_DB` is dropped even though
 * `COW_` prefixes are otherwise unremarkable, and a deployment that adds a new
 * secret variable does not have to remember this file.
 *
 * This mirrors `agent/desktop_local/worker.py::build_worker_env`, deliberately:
 * the launcher scrubs and the worker would scrub again, so neither is the single
 * point of failure.
 */

/** Variables a tool genuinely needs in order to start and run. */
export const ENV_KEEP: readonly string[] = [
  'PATH',
  'HOME',
  'USER',
  'LOGNAME',
  'SHELL',
  'LANG',
  'LC_ALL',
  'LC_CTYPE',
  'TERM',
  'PWD',
  // Windows cannot create a process or resolve DLLs without these.
  'SYSTEMROOT',
  'SYSTEMDRIVE',
  'WINDIR',
  'PATHEXT',
  'COMSPEC',
  'PROCESSOR_ARCHITECTURE',
  'NUMBER_OF_PROCESSORS',
  'OS',
  // The master build already keys desktop-specific tool wording off this.
  'COW_DESKTOP',
];

/**
 * Temp variables, which are *set* rather than kept.
 *
 * Deliberately absent from `ENV_KEEP`: inheriting the parent's value is the bug
 * described above, so these are always overwritten with the granted temp root.
 */
export const ENV_TEMP_VARS: readonly string[] = ['TMPDIR', 'TEMP', 'TMP'];

/** Name fragments that are never allowed through, whatever else says otherwise. */
export const ENV_DROP_MARKERS: readonly string[] = [
  'TOKEN',
  'SECRET',
  'PASSWORD',
  'PASSWD',
  'API_KEY',
  'APIKEY',
  'CREDENTIAL',
  'PRIVATE_KEY',
  'SESSION',
  'COOKIE',
  'AUTH',
];

export interface WorkerEnvOptions {
  /**
   * The run's granted temp directory, which becomes `TMPDIR`/`TEMP`/`TMP`.
   *
   * Absent means the temp variables are not set at all, which is only correct
   * for a worker that will not touch a temp file. Every real launch passes it.
   */
  tempRoot?: string;
  /** Additions the launcher itself makes. Still subject to the deny pass. */
  extra?: NodeJS.ProcessEnv;
}

/** The scrubbed environment, suitable for `child_process.spawn({ env })`. */
export function scrubWorkerEnv(
  base: NodeJS.ProcessEnv,
  options: WorkerEnvOptions = {},
): NodeJS.ProcessEnv {
  const clean: NodeJS.ProcessEnv = {};
  for (const key of ENV_KEEP) {
    const value = base[key];
    if (value !== undefined) clean[key] = value;
  }
  for (const key of Object.keys(clean)) {
    if (isSecretName(key)) delete clean[key];
  }
  // Set, never inherited: the parent's temp directory is outside the writable
  // grant, and a worker that cannot create a temp file cannot run a tool.
  if (options.tempRoot) {
    for (const key of ENV_TEMP_VARS) clean[key] = options.tempRoot;
  }
  // Caller-supplied additions are the launcher's own, so they are applied after
  // the allowlist but still subject to the secret-name deny pass.
  for (const [key, value] of Object.entries(options.extra ?? {})) {
    if (value === undefined || isSecretName(key)) continue;
    clean[key] = value;
  }
  return clean;
}

function isSecretName(key: string): boolean {
  const upper = key.toUpperCase();
  return ENV_DROP_MARKERS.some((marker) => upper.includes(marker));
}
