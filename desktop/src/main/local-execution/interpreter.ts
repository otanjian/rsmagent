/**
 * Which interpreter runs a local script, and what it needs to be readable.
 *
 * This exists because of a failure that looks like something else entirely. The
 * seatbelt profile denies by default, so an interpreter whose own standard
 * library or `site-packages` is not named in the read list cannot start: it dies
 * before the handshake, and the desktop reports "the worker crashed". The
 * allowances therefore have to be **derived from the interpreter that will
 * actually run**, not guessed from a path prefix -- and a virtualenv makes the
 * two different, because `.venv/bin/python` is a symlink into a completely
 * separate installation.
 *
 * There are two runtimes, not one. A source checkout runs the worker as
 * `python -m agent.desktop_local.worker`; an **installed app** ships a single
 * frozen PyInstaller executable, where there is no Python on the PATH, no
 * `.venv` beside the backend, and no `-m` to give: the worker is that binary
 * with a flag (see `app.py`). Both are described by {@link WorkerRuntime} so the
 * launcher has one thing to consult, and the read list is still derived from a
 * probe of whatever is actually going to run.
 *
 * Deriving them is a production concern, not a test concern: a test that works
 * out the right paths proves nothing about the app.
 */

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

/** The module a source checkout runs. */
export const WORKER_MODULE = 'agent.desktop_local.worker';
/** The flag a frozen bundle is started with to become the worker. */
export const WORKER_FLAG = '--desktop-local-worker';
/** The flag that asks either form to describe its own runtime, then exit. */
export const WORKER_PROBE_FLAG = '--probe';

/**
 * What the sandbox starts, and how.
 *
 * `args` is the whole difference between the two shapes: `['-m', module]` cannot
 * be given to a frozen binary (it would be read as the program's own argv), and
 * the flag cannot be given to a plain interpreter (it would be a file name).
 * Keeping that decision in one place means the launcher cannot emit a command
 * that only works in a checkout.
 */
export interface WorkerRuntime {
  /** The executable the sandbox starts. */
  path: string;
  /** The argv after `path` that turns it into the worker. */
  args: readonly string[];
  /** One frozen executable (an installed app) rather than a Python module run. */
  frozen: boolean;
}

/** The source-checkout form: an interpreter running the worker module. */
export function sourceRuntime(pythonPath: string): WorkerRuntime {
  return { path: pythonPath, args: ['-m', WORKER_MODULE], frozen: false };
}

/** What the interpreter reports about itself, in one round trip. */
export interface InterpreterProbe {
  /** The path as it was given (may be a symlink). */
  pythonPath: string;
  /** The resolved binary. */
  realPath: string;
  /** `sys.base_prefix` -- the installation, not the virtualenv. */
  basePrefix: string;
  /** `sysconfig` stdlib directory. */
  stdlib: string;
  /** `sysconfig` purelib, i.e. where this environment's packages live. */
  purelib: string;
  /** How this runtime is started. Absent is read as the source form. */
  runtime?: WorkerRuntime;
  /** Whether the probe reported a frozen build. */
  frozen?: boolean;
  /** PyInstaller's `sys._MEIPASS`: where a bundle keeps its Python runtime. */
  bundleRoot?: string;
}

export type ProbeRunner = (command: readonly string[]) => string;

const PROBE_SCRIPT = [
  'import json, sys, sysconfig',
  'paths = sysconfig.get_paths()',
  'print(json.dumps({',
  '  "realPath": sys.executable,',
  '  "basePrefix": sys.base_prefix,',
  '  "stdlib": paths.get("stdlib", ""),',
  '  "purelib": paths.get("purelib", ""),',
  '}))',
].join('\n');

function defaultRunner(command: readonly string[]): string {
  return execFileSync(command[0], command.slice(1), {
    encoding: 'utf8',
    timeout: 15_000,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
}

/** Virtualenv layouts, in the order the backend's own discovery uses. */
export function interpreterCandidates(backendPath: string): string[] {
  return [
    path.join(backendPath, '.venv', 'bin', 'python'),
    path.join(backendPath, '.venv', 'Scripts', 'python.exe'),
    path.join(backendPath, 'venv', 'bin', 'python'),
    path.join(backendPath, 'venv', 'Scripts', 'python.exe'),
  ];
}

/**
 * Where an installed app's backend binary lives.
 *
 * The same rule `python-manager.ts` uses to start the HTTP backend: the packaged
 * onedir bundle sits at `<resources>/backend/cowagent-backend/`, with a flat
 * `<resources>/backend/` as the fallback layout. A second, different rule here
 * would produce "the app runs but cannot run scripts", which is exactly the
 * failure this is for.
 */
export function bundleExecutableCandidates(
  backendPath: string,
  platform: NodeJS.Platform = process.platform,
): string[] {
  const exeName = platform === 'win32' ? 'cowagent-backend.exe' : 'cowagent-backend';
  return [
    path.join(backendPath, 'cowagent-backend', exeName),
    path.join(backendPath, exeName),
  ];
}

/**
 * The runtime to run local scripts with, or `null`.
 *
 * The bundle is checked before the virtualenvs because a shipped app is the case
 * that has no other candidate; a checkout has no bundle, so the two never
 * overlap in practice.
 *
 * `null` rather than a bare `python3` fallback: a PATH lookup would pick up
 * whatever the user happens to have, which is neither the interpreter this app
 * ships nor one whose dependencies it validated. The caller turns `null` into a
 * refusal.
 */
export function findWorkerRuntime(
  backendPath: string,
  platform: NodeJS.Platform = process.platform,
  exists: (candidate: string) => boolean = fs.existsSync,
): WorkerRuntime | null {
  for (const candidate of bundleExecutableCandidates(backendPath, platform)) {
    if (exists(candidate)) {
      return { path: candidate, args: [WORKER_FLAG], frozen: true };
    }
  }
  const interpreter = findInterpreter(backendPath, exists);
  return interpreter ? sourceRuntime(interpreter) : null;
}

/**
 * The interpreter to run local scripts with, or `null`.
 *
 * The source-checkout half of {@link findWorkerRuntime}, kept because the v1
 * read path only ever wants a venv interpreter.
 */
export function findInterpreter(
  backendPath: string,
  exists: (candidate: string) => boolean = fs.existsSync,
): string | null {
  for (const candidate of interpreterCandidates(backendPath)) {
    if (exists(candidate)) return candidate;
  }
  return null;
}

/** A stable key for caching a probe, per runtime shape. */
export function runtimeKey(runtime: WorkerRuntime): string {
  return [runtime.path, ...runtime.args].join(' ');
}

/**
 * Ask the runtime where it lives. `null` when it cannot answer.
 *
 * The failure is a `null` and not a throw because the caller's answer is the
 * same either way -- local script execution is unavailable -- and an exception
 * here would travel up through a spawn path.
 */
export function probeInterpreter(
  target: string | WorkerRuntime,
  run: ProbeRunner = defaultRunner,
): InterpreterProbe | null {
  const runtime = typeof target === 'string' ? sourceRuntime(target) : target;
  // A frozen binary has no `-c`; it answers the probe through its own entry
  // point, which is the same dispatch that makes it the worker.
  const command = runtime.frozen
    ? [runtime.path, ...runtime.args, WORKER_PROBE_FLAG]
    : [runtime.path, '-c', PROBE_SCRIPT];
  let parsed: unknown;
  try {
    const text = run(command);
    // A venv's interpreter may print a warning line before the JSON.
    const line = text.split('\n').map((l) => l.trim()).filter((l) => l.startsWith('{')).pop();
    if (!line) return null;
    parsed = JSON.parse(line);
  } catch {
    return null;
  }
  if (typeof parsed !== 'object' || parsed === null) return null;
  const record = parsed as Record<string, unknown>;
  const asString = (value: unknown): string => (typeof value === 'string' ? value : '');
  // `realPath` is the one field without which the read list would be wrong for a
  // symlinked virtualenv, so its absence is a failed probe, not a default.
  const realPath = asString(record.realPath);
  if (!realPath) return null;
  return {
    pythonPath: runtime.path,
    realPath,
    basePrefix: asString(record.basePrefix),
    stdlib: asString(record.stdlib),
    purelib: asString(record.purelib),
    runtime,
    frozen: record.frozen === true || runtime.frozen,
    bundleRoot: asString(record.bundleRoot),
  };
}

/**
 * The directories a sandboxed run must be able to read to start at all.
 *
 * Deliberately not "the project and the runtime": a symlinked virtualenv means
 * the binary's own directory is *not* under the base prefix, and a venv's
 * `site-packages` is not under either. All of them are read-only allowances;
 * nothing here becomes writable (asserted in the test suite).
 *
 * A frozen run is narrower on purpose: `base_prefix`/`stdlib`/`purelib` in a
 * PyInstaller build can still name the machine the bundle was *built* on, and
 * granting those would widen the sandbox on a developer's machine (where such a
 * path exists) while doing nothing for a user (where it does not). A bundle
 * reports where its own runtime is, so only that is used.
 */
export function interpreterReadPaths(probe: InterpreterProbe): string[] {
  const candidates = probe.frozen
    ? [probe.bundleRoot ?? '', path.dirname(probe.realPath), probe.pythonPath]
    : [
      probe.basePrefix,
      probe.stdlib,
      probe.purelib,
      path.dirname(probe.realPath),
      path.dirname(path.dirname(probe.realPath)),
    ];
  const seen = new Set<string>();
  for (const candidate of candidates) {
    // "/" is already granted wholesale and naming it again adds nothing.
    if (candidate && candidate !== '/' && path.isAbsolute(candidate)) {
      seen.add(candidate);
    }
  }
  return [...seen];
}

/**
 * Probes, remembered per runtime.
 *
 * A failed probe is remembered as a failure: re-running a broken interpreter on
 * every call would turn one refusal into a stall.
 */
export class InterpreterRegistry {
  private readonly probes = new Map<string, InterpreterProbe | null>();

  constructor(private readonly run: ProbeRunner = defaultRunner) {}

  get(target: string | WorkerRuntime): InterpreterProbe | null {
    const runtime = typeof target === 'string' ? sourceRuntime(target) : target;
    const key = runtimeKey(runtime);
    const cached = this.probes.get(key);
    if (cached !== undefined) return cached;
    const probe = probeInterpreter(runtime, this.run);
    this.probes.set(key, probe);
    return probe;
  }

  forget(target: string | WorkerRuntime): void {
    this.probes.delete(runtimeKey(typeof target === 'string' ? sourceRuntime(target) : target));
  }
}
