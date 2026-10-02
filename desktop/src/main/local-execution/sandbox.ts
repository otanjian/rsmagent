/**
 * The OS-level boundary a local script runs inside (tasks 4.4 / 4.5 / 4.6).
 *
 * Design D5 is explicit that a substring check on a command line is not
 * isolation, and the probes in
 * `openspec/changes/align-desktop-project-execution-with-master/evidence/platform-probes.md`
 * are what decided this file. Two consequences shape it:
 *
 * 1. **The profile is generated from the verified grant, not from a path
 *    check.** Only the project root (writable), the skill cache (read-only) and
 *    the run's temp directory (writable) are named; everything else is denied by
 *    `(deny default)`. A symlink or `..` escape is stopped by the resolver, which
 *    is why this works where a string filter does not.
 * 2. **A platform that has not been probed does not get to claim support.**
 *    Windows has no probed boundary yet, so `resolveScriptSupport` refuses there
 *    rather than launching the worker unconfined. Refusing is the honest answer;
 *    "we will add the sandbox later" is not a sandbox.
 *
 * The profile's exact contents are load-bearing, and every clause in it was
 * found by probing rather than by reading documentation:
 *
 * - without `(allow process-fork)` a script that spawns anything fails with
 *   `fork: Operation not permitted`;
 * - without a readable `/` the process dies in dyld (SIGABRT 134);
 * - without `(allow file-read-metadata (subpath "/"))` the interpreter cannot be
 *   `exec`'d at all (`execvp() ... failed: Operation not permitted`), because
 *   path resolution needs a directory lookup on every ancestor;
 * - without a `(literal ...)` allowance on those ancestors the same `execvp`
 *   fails, because `(subpath ...)` grants the subtree but not the traversal;
 * - without the `/dev/null` literal Python's `subprocess.DEVNULL` fails.
 *
 * Together those give a boundary that is real in both directions: reads are
 * confined to the granted roots plus directory *metadata*, and writes are
 * confined to the project and the run's temp directory.
 */

import path from 'node:path';

/** How scripts are confined on this platform. */
export type SandboxKind = 'seatbelt' | 'none';

export interface ScriptCapabilities {
  platform: string;
  shell: string;
  /** The interpreter that will run scripts, as it will appear to the user. */
  python: string;
  /** What "isolated" means here, in words the model may repeat. */
  isolation: string;
  /** Whether the sandbox permits network access (always false for now). */
  network: boolean;
  /**
   * Whether reads are actually confined to the grant.
   *
   * `true`, with one stated exception: path *resolution* requires reading
   * directory metadata everywhere, so a script can learn that a file outside the
   * project exists and how big it is. It cannot read its contents. That
   * exception is `(allow file-read-metadata (subpath "/"))` below, and it is not
   * optional -- without it the interpreter cannot be exec'd at all (probe:
   * `execvp() ... failed: Operation not permitted`).
   */
  readsConfined: boolean;
  /** Files the run may write, in the order they are granted. */
  writable: readonly string[];
  /** Files the run may read, beyond the system's own runtime. */
  readable: readonly string[];
}

export type ScriptSupport =
  | { supported: true; kind: SandboxKind; launcher: string; capabilities: ScriptCapabilities }
  | { supported: false; code: ScriptSupportRefusal; reason: string };

export type ScriptSupportRefusal =
  | 'unsupported_platform'
  | 'sandbox_unavailable'
  | 'python_not_found';

export interface GrantPaths {
  /** The local project root the user opened. */
  projectRoot: string;
  /** The run's private temp directory. */
  tempRoot: string;
  /** The resolved skill cache for this run, if one was prepared. */
  skillRoots?: readonly string[];
  /**
   * The interpreter's own installation (its base directory and site-packages).
   *
   * Not a convenience: the sandbox denies by default, so without these the
   * interpreter cannot load its own standard library and dies before the
   * handshake -- which looks exactly like "the worker crashed". They are read
   * allowances only; nothing here becomes writable.
   */
  runtimeReadPaths?: readonly string[];
}

export interface ResolveSupportOptions {
  platform: NodeJS.Platform;
  paths: GrantPaths;
  /**
   * The executable that will run the worker.
   *
   * In a source checkout that is a Python interpreter; in an installed app it is
   * the bundle's own binary, which is why the argv is a separate option rather
   * than being hardcoded as `<executable> -m <module>` at the call site.
   */
  pythonPath?: string;
  /**
   * The argv that turns that executable into the worker.
   *
   * Defaults to the source-checkout form (`-m agent.desktop_local.worker`). A
   * frozen bundle passes `['--desktop-local-worker']` instead: `-m` would be
   * read as the program's own argument.
   */
  workerArgs?: readonly string[];
  /** Whether `/usr/bin/sandbox-exec` was found. */
  seatbeltAvailable?: boolean;
}

/**
 * Whether local scripts may run at all, and what the boundary actually is.
 *
 * Returning a refusal instead of a plan is the point: the caller must not have a
 * "just spawn it" fallback available, and the user must be told which capability
 * is missing rather than getting a permission error from inside a tool.
 */
export function resolveScriptSupport(options: ResolveSupportOptions): ScriptSupport {
  const { platform, paths } = options;
  if (platform === 'darwin') {
    if (!options.seatbeltAvailable) {
      return {
        supported: false,
        code: 'sandbox_unavailable',
        reason:
          'sandbox-exec is not available on this machine, so a local script ' +
          'cannot be confined. Local script execution is disabled; file tools ' +
          'still work.',
      };
    }
    if (!options.pythonPath) {
      return {
        supported: false,
        code: 'python_not_found',
        reason:
          'the bundled Python interpreter was not found, so local script ' +
          'execution is disabled.',
      };
    }
    return {
      supported: true,
      kind: 'seatbelt',
      launcher: 'seatbelt',
      capabilities: {
        platform: 'macOS',
        shell: '/bin/sh (the system shell; zsh and bash are not guaranteed)',
        python: 'the interpreter bundled with this application',
        isolation:
          'kernel-level sandbox: the process may read the system runtime, the ' +
          'project and the skill caches, and may write only inside the project ' +
          'and the run\'s temporary directory. The network is unreachable. ' +
          'Reading a file outside those roots is refused by the kernel; only ' +
          'its metadata (existence, size, timestamps) is visible, because the ' +
          'loader needs that to start the interpreter at all.',
        network: false,
        readsConfined: true,
        writable: [paths.projectRoot, paths.tempRoot],
        readable: [
          paths.projectRoot,
          ...(paths.skillRoots ?? []),
          ...(paths.runtimeReadPaths ?? []),
        ],
      },
    };
  }
  if (platform === 'win32') {
    return {
      supported: false,
      code: 'unsupported_platform',
      reason:
        'Windows has no probed execution boundary in this build, so local ' +
        'script execution is disabled. It is not a permissions problem: the ' +
        'isolation this app relies on has not been verified on Windows yet.',
    };
  }
  return {
    supported: false,
    code: 'unsupported_platform',
    reason:
      `local script execution is not supported on this platform (${platform}); ` +
      'file tools inside the local project still work.',
  };
}

/**
 * Escape one path for inclusion as an SBPL string literal.
 *
 * A path that contains a quote or a backslash would otherwise end the literal
 * early and let the rest of the path be read as policy -- i.e. a directory name
 * could widen the sandbox. `sandbox-exec` has no separate argv channel for
 * these, so escaping is the only defence and it must be exact.
 */
export function escapeSeatbeltPath(value: string): string {
  return value.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

/**
 * Every ancestor directory of the given paths, excluding `/`.
 *
 * A `(subpath ...)` allowance does not imply that the path can be *reached*:
 * resolving `/a/b/c` needs a lookup on `/a` and `/a/b`, and the kernel refuses
 * the whole `exec` without it (probe: `execvp() ... failed: Operation not
 * permitted`). Ancestors are granted with `literal`, which permits the lookup
 * without granting anything inside them -- the difference between "may traverse
 * `/Users/me`" and "may read `/Users/me`".
 */
export function ancestorDirectories(paths: readonly string[]): string[] {
  const ancestors = new Set<string>();
  for (const value of paths) {
    let current = path.dirname(value);
    while (current && current !== '/' && current !== '.') {
      ancestors.add(current);
      current = path.dirname(current);
    }
  }
  return [...ancestors];
}

/** System paths that must stay readable or the process cannot start at all. */
const SYSTEM_READ_PATHS: readonly string[] = [
  // The root is granted as a *literal*: naming the exact directory permits the
  // lookup that dyld needs, while `(subpath "/")` would make the entire
  // filesystem readable. Without any root allowance even /usr/bin/true dies in
  // dyld (probe: SIGABRT 134).
  '/',
  // The shared dyld cache lives here on current macOS releases.
  '/System/Volumes/Preboot',
  '/usr',
  '/System',
  '/bin',
  '/sbin',
  '/Library/Apple',
  '/dev',
  '/etc',
  '/private/etc',
  '/private/var/db',
  // `python3` via the Xcode shim reads the selected developer directory; the
  // bundled interpreter may not, but the cost of the allowance is nil.
  '/private/var/select',
];

/**
 * The macOS seatbelt profile for one project.
 *
 * `(deny default)` first, then the minimum that makes a process start and a
 * script usable. Only project + temp are writable; skill caches are read-only by
 * virtue of not appearing in the write list, and are likewise readable only
 * because they are named.
 */
export function buildSeatbeltProfile(paths: GrantPaths): string {
  const readable = [
    ...SYSTEM_READ_PATHS,
    paths.projectRoot,
    ...(paths.skillRoots ?? []),
    ...(paths.runtimeReadPaths ?? []),
  ];
  const writable = [paths.projectRoot, paths.tempRoot];
  const subpaths = readable.filter((p) => p !== '/');
  const literals = readable.filter((p) => p === '/');
  const lines = [
    '(version 1)',
    '(deny default)',
    '(allow process-exec)',
    // Without this, anything a script spawns fails with
    // "fork: Operation not permitted" (probe).
    '(allow process-fork)',
    '(allow sysctl-read)',
    '(allow mach-lookup)',
    '(allow signal (target self))',
    // `(target self)` is not enough, and that is not obvious: it denies the
    // worker killing *its own children*, so every cancellation path fails with
    // "Operation not permitted" and a cancelled command keeps running. Probed:
    // `(target children)` allows descendants and still denies signalling
    // anything else; `(target others)` is the complement and does not help.
    '(allow signal (target children))',
    // Path resolution needs directory metadata everywhere, or the interpreter
    // cannot be exec'd at all. This reveals existence and size, not contents.
    '(allow file-read-metadata (subpath "/"))',
    '(allow file-read*',
    ...literals.map((p) => `  (literal "${escapeSeatbeltPath(p)}")`),
    ...subpaths.map((p) => `  (subpath "${escapeSeatbeltPath(p)}")`),
    ...ancestorDirectories(subpaths).map(
      (p) => `  (literal "${escapeSeatbeltPath(p)}")`,
    ),
    ')',
    '(allow file-write*',
    ...writable.map((p) => `  (subpath "${escapeSeatbeltPath(p)}")`),
    // Python's own subprocess helpers open /dev/null for a discarded stream
    // (`subprocess.DEVNULL`), so a bash tool that pipes anything dies with
    // "Operation not permitted: '/dev/null'" without this. It is a single
    // character device, so the literal -- not a subpath -- is what is granted.
    '  (literal "/dev/null")',
    '  (literal "/dev/stdout")',
    '  (literal "/dev/stderr")',
    ')',
  ];
  return lines.join('\n') + '\n';
}

export interface LaunchPlan {
  ok: true;
  /** The executable to start. */
  command: string;
  /** Its arguments; for seatbelt this is `-p <profile> <python> -m ...`. */
  args: string[];
  /** The working directory for the child. */
  cwd: string;
  /** The environment, already scrubbed (see `env.ts`). */
  env: NodeJS.ProcessEnv;
  /** How the process is confined, and with what. */
  sandbox: { kind: SandboxKind; profile: string; description: string };
  capabilities: ScriptCapabilities;
}

export type LaunchRefusal = {
  ok: false;
  code: ScriptSupportRefusal;
  reason: string;
};

/**
 * The exact argv/env a confined worker is started with.
 *
 * The worker is launched either as `python -m agent.desktop_local.worker` (a
 * source checkout) or as the bundle's own binary with a flag (an installed app):
 * the project root travels in the handshake frame, and
 * `COW_DESKTOP_WORKER_ROOT` lets the worker refuse a frame that names a
 * different directory than the one confined here.
 */
export function buildLaunchPlan(
  options: ResolveSupportOptions & {
    env: NodeJS.ProcessEnv;
    pythonPath?: string;
    workingDirectory: string;
    moduleName?: string;
  },
): LaunchPlan | LaunchRefusal {
  const support = resolveScriptSupport(options);
  if (!support.supported) {
    return { ok: false, code: support.code, reason: support.reason };
  }
  const moduleName = options.moduleName ?? 'agent.desktop_local.worker';
  const pythonPath = options.pythonPath as string;
  const workerArgs = options.workerArgs ?? ['-m', moduleName];
  const profile = buildSeatbeltProfile(options.paths);
  return {
    ok: true,
    command: '/usr/bin/sandbox-exec',
    args: ['-p', profile, pythonPath, ...workerArgs],
    cwd: options.workingDirectory,
    env: options.env,
    sandbox: {
      kind: 'seatbelt',
      profile,
      description: support.capabilities.isolation,
    },
    capabilities: support.capabilities,
  };
}

/** The description a model or a user may be shown for a script capability. */
export function describeScriptCapabilities(
  support: ScriptSupport,
): string {
  if (!support.supported) {
    return `Local script execution is unavailable: ${support.reason}`;
  }
  const c = support.capabilities;
  return [
    `Platform: ${c.platform}`,
    `Shell: ${c.shell}`,
    `Python: ${c.python}`,
    `Isolation: ${c.isolation}`,
    `Network: ${c.network ? 'allowed' : 'not allowed'}`,
    // Stated explicitly rather than left to be inferred from `Isolation`: the
    // model must not tell a user their files are safe from being read.
    `Reads: ${c.readsConfined ? 'confined to the project' : 'not confined'}`,
    `Writable: ${c.writable.join(', ')}`,
  ].join('\n');
}
