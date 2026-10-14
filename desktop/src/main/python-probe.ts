import { spawn, ChildProcess } from 'child_process'
import fs from 'fs'
import path from 'path'

/**
 * Choosing the Python interpreter that runs the local backend.
 *
 * The old ``findPython()`` returned the first path that *existed* and otherwise
 * the string ``'python'``. On Windows that failed in the most confusing way
 * available: a checkout whose ``.venv`` was created on macOS has no
 * ``Scripts/python.exe`` at all, and PATH's ``python`` is the Microsoft Store
 * placeholder -- which exists, launches, and refuses to be an interpreter. The
 * user saw "The app exited during startup (code 9009)" and nothing else
 * (docs/design/windows-desktop-verification-2026-10-13.md section 4.2).
 *
 * So the rule here is: a candidate is only chosen after it has *answered*. The
 * probe asks the interpreter for its own version and ``sys.executable``, which
 * is also what makes a shim, an alias and a wrapper distinguishable from the
 * real thing: the path is a clue, the answer is the evidence.
 *
 * Deliberately narrow, per design D2:
 *
 * * explicit path first -- ``COW_PYTHON_PATH``, then the dev config
 *   ``python_path``. An explicit setting is an instruction: if it fails we stop
 *   instead of quietly running some other interpreter;
 * * the project's own venv, in the layout that belongs to *this* platform;
 * * PATH last.
 *
 * No conda/micromamba environment hunting, no py-launcher discovery, no scan of
 * every environment on the machine: those guess, and a wrong guess is worse than
 * a clear failure. Users of those tools point at their interpreter explicitly.
 *
 * No Electron import: ``node --test`` drives this directly
 * (tests/test_desktop_python_probe.cjs).
 */

/**
 * Supported interpreter window, mirroring ``agent/skills/dependencies.py``
 * (``MIN_PYTHON`` / ``MAX_PYTHON_EXCLUSIVE``), which is also what the bundle's
 * build check enforces. A test keeps the two from drifting.
 */
export const MIN_PYTHON: readonly [number, number] = [3, 8]
export const MAX_PYTHON_EXCLUSIVE: readonly [number, number] = [3, 14]

/** One candidate gets this long to answer before it is killed. */
export const PROBE_TIMEOUT_MS = 3000

/** The whole round of candidates gets this long. */
export const PROBE_BUDGET_MS = 15000

/** Cap on what we read from a probe, so a chatty wrapper cannot flood us. */
const MAX_PROBE_OUTPUT = 4096

/** Asks for the version and the real executable path, as one JSON line. */
const PROBE_SCRIPT =
  'import json,sys;print(json.dumps({"v":list(sys.version_info[:3]),"e":sys.executable}))'

/**
 * Where a candidate came from. ``env`` and ``config`` are explicit choices and
 * end the search when they fail; ``venv`` and ``path`` are conveniences.
 */
export type PythonSource = 'env' | 'config' | 'venv' | 'path'

export interface PythonCandidate {
  command: string
  source: PythonSource
}

export type PythonProbeFailure =
  /** The command could not be run at all (ENOENT, EACCES, non-zero exit). */
  | 'not_found'
  /** It ran, but it is the Microsoft Store placeholder rather than Python. */
  | 'store_alias'
  /** It ran and answered, but the version is outside the supported window. */
  | 'unsupported_version'
  /** It never answered inside the per-candidate limit. */
  | 'timeout'
  /** It answered with something we cannot read as a version. */
  | 'unreadable'

export interface PythonProbe {
  ok: boolean
  candidate: PythonCandidate
  /** e.g. ``3.12.13``, when the probe answered. */
  version?: string
  /** ``sys.executable`` -- the interpreter behind a shim or symlink. */
  executable?: string
  failure?: PythonProbeFailure
  /** One line of evidence for the failure, shown in the diagnostic summary. */
  detail?: string
}

/** Injectable for tests; matches the shape of ``child_process.spawn``. */
export type SpawnLike = (
  command: string,
  args: readonly string[],
  options: Record<string, unknown>,
) => ChildProcess

export interface CandidateOptions {
  backendPath: string
  platform?: NodeJS.Platform
  /** ``COW_PYTHON_PATH`` -- highest priority. */
  envPython?: string
  /** Development config ``python_path``. */
  configuredPython?: string
  exists?: (candidate: string) => boolean
}

/**
 * The candidates in priority order.
 *
 * The venv list is platform-specific on purpose. On Windows a POSIX checkout's
 * ``.venv/bin/python`` cannot exist (the report's checkout had one because it
 * was created on macOS), and listing it made the search look broader than it
 * was while the layout that *would* work (``.venv-win``) was never created.
 */
export function pythonCandidates(options: CandidateOptions): PythonCandidate[] {
  const platform = options.platform || process.platform
  const exists = options.exists || defaultExists
  const candidates: PythonCandidate[] = []

  const explicit = (value: string | undefined, source: PythonSource): void => {
    const trimmed = (value || '').trim()
    if (trimmed) candidates.push({ command: trimmed, source })
  }
  explicit(options.envPython, 'env')
  explicit(options.configuredPython, 'config')

  const venvs = platform === 'win32'
    ? ['.venv-win/Scripts/python.exe', '.venv/Scripts/python.exe', 'venv/Scripts/python.exe']
    : ['.venv-win/bin/python', '.venv/bin/python', 'venv/bin/python']
  for (const relative of venvs) {
    const full = path.isAbsolute(relative) ? relative : path.join(options.backendPath, relative)
    if (exists(full)) candidates.push({ command: full, source: 'venv' })
  }

  candidates.push({ command: platform === 'win32' ? 'python' : 'python3', source: 'path' })
  return candidates
}

function defaultExists(candidate: string): boolean {
  try {
    return fs.existsSync(candidate)
  } catch {
    return false
  }
}

export interface ProbeOptions {
  timeoutMs?: number
  spawnImpl?: SpawnLike
}

/** Run the interpreter once and decide whether it is usable. Never throws. */
export function probeInterpreter(
  candidate: PythonCandidate,
  options: ProbeOptions = {},
): Promise<PythonProbe> {
  const timeoutMs = options.timeoutMs && options.timeoutMs > 0 ? options.timeoutMs : PROBE_TIMEOUT_MS
  const run: SpawnLike = options.spawnImpl || (spawn as unknown as SpawnLike)

  return new Promise<PythonProbe>((resolve) => {
    let settled = false
    let output = ''
    let child: ChildProcess | null = null
    let timer: ReturnType<typeof setTimeout> | null = null

    const finish = (result: PythonProbe): void => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      try {
        child?.removeAllListeners()
      } catch {
        // A child we cannot detach from is still a child we stop waiting on.
      }
      resolve(result)
    }

    try {
      child = run(candidate.command, ['-c', PROBE_SCRIPT], {
        // No shell: a candidate is a command, not a line of shell to interpret.
        // The probe must work when PATH contains spaces, quotes or a `&`.
        shell: false,
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      })
    } catch (error) {
      resolve({ ok: false, candidate, failure: 'not_found', detail: describe(error) })
      return
    }

    timer = setTimeout(() => {
      // Settle first, then kill: killing makes the child emit 'close', and that
      // close would otherwise be classified as "the interpreter failed" instead
      // of "we stopped waiting for it".
      finish({
        ok: false,
        candidate,
        failure: 'timeout',
        detail: `no answer within ${timeoutMs} ms`,
      })
      // A probe that hangs is cleaned up, not left running: an orphaned
      // interpreter would hold the port or a lock the next attempt needs.
      try {
        child?.kill()
      } catch {
        // Already gone.
      }
    }, timeoutMs)
    if (typeof timer.unref === 'function') timer.unref()

    const collect = (data: Buffer | string): void => {
      if (output.length >= MAX_PROBE_OUTPUT) return
      output += String(data).slice(0, MAX_PROBE_OUTPUT - output.length)
    }
    child.stdout?.on('data', collect)
    child.stderr?.on('data', collect)
    child.on('error', (error: Error) => {
      finish({ ok: false, candidate, failure: 'not_found', detail: describe(error) })
    })
    child.on('close', (code: number | null) => {
      finish(classify(candidate, code, output))
    })
  })
}

function classify(candidate: PythonCandidate, code: number | null, output: string): PythonProbe {
  const text = output.trim()
  const first = firstLine(text)

  if (code !== 0) {
    if (looksLikeStoreAlias(candidate.command, text)) {
      return {
        ok: false,
        candidate,
        failure: 'store_alias',
        detail: first || 'the Microsoft Store placeholder was launched',
      }
    }
    return {
      ok: false,
      candidate,
      failure: 'not_found',
      detail: first || `exit code ${code}`,
    }
  }

  const parsed = parseProbe(text)
  if (!parsed) {
    return { ok: false, candidate, failure: 'unreadable', detail: first || 'no version in the answer' }
  }
  const [major, minor, patch] = parsed.v
  const version = `${major}.${minor}.${patch}`
  if (!inWindow([major, minor])) {
    return {
      ok: false,
      candidate,
      version,
      failure: 'unsupported_version',
      detail: `Python ${version} is outside >=${MIN_PYTHON.join('.')},<${MAX_PYTHON_EXCLUSIVE.join('.')}`,
    }
  }
  return { ok: true, candidate, version, executable: parsed.e }
}

/** True when the version is inside the window the bundle is built for. */
export function inWindow(version: readonly [number, number]): boolean {
  const [major, minor] = version
  const above = major > MIN_PYTHON[0] || (major === MIN_PYTHON[0] && minor >= MIN_PYTHON[1])
  const below =
    major < MAX_PYTHON_EXCLUSIVE[0] ||
    (major === MAX_PYTHON_EXCLUSIVE[0] && minor < MAX_PYTHON_EXCLUSIVE[1])
  return above && below
}

/**
 * The Store placeholder is the one failure worth naming, because the user's fix
 * ("install Python") is different from "your environment is broken".
 *
 * The path is only a clue and the printed text is only a clue: an interpreter
 * that answers correctly from ``WindowsApps`` would still be accepted, and an
 * alias installed elsewhere is still caught by what it prints.
 */
function looksLikeStoreAlias(command: string, output: string): boolean {
  if (/[\\/]WindowsApps[\\/]/i.test(command)) return true
  return /microsoft store|python was not found|WindowsApps/i.test(output)
}

function parseProbe(text: string): { v: [number, number, number]; e?: string } | null {
  const lines = text.split('\n').map((line) => line.trim()).filter(Boolean)
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const line = lines[index]
    if (!line.startsWith('{')) continue
    try {
      const parsed = JSON.parse(line) as { v?: unknown; e?: unknown }
      if (Array.isArray(parsed.v) && parsed.v.length >= 2) {
        const [major, minor, patch] = parsed.v.map((value) => Number(value))
        if (Number.isFinite(major) && Number.isFinite(minor)) {
          return {
            v: [major, minor, Number.isFinite(patch) ? patch : 0],
            e: typeof parsed.e === 'string' ? parsed.e : undefined,
          }
        }
      }
    } catch {
      // Not the answer line; a wrapper may have printed something else first.
    }
  }
  return null
}

function firstLine(text: string): string {
  const line = text.split('\n').map((value) => value.trim()).find(Boolean)
  return line ? line.slice(0, 200) : ''
}

function describe(error: unknown): string {
  if (error instanceof Error) return error.message
  return String(error)
}

export interface SelectionOptions extends ProbeOptions {
  /** Total time for the whole round, so a slow disk cannot stall startup. */
  budgetMs?: number
  now?: () => number
}

export interface Selection {
  selected?: PythonProbe
  attempts: PythonProbe[]
}

/**
 * Probe candidates in order until one answers.
 *
 * An explicit candidate (env or config) that fails ends the search: running a
 * different interpreter than the one the developer named produces a backend that
 * works on one machine and not on the next, which is exactly the class of bug
 * this replaces.
 */
export async function selectInterpreter(
  candidates: readonly PythonCandidate[],
  options: SelectionOptions = {},
): Promise<Selection> {
  const budgetMs = options.budgetMs && options.budgetMs > 0 ? options.budgetMs : PROBE_BUDGET_MS
  const now = options.now || (() => Date.now())
  const started = now()
  const attempts: PythonProbe[] = []

  for (const candidate of candidates) {
    const remaining = budgetMs - (now() - started)
    if (remaining <= 0) {
      attempts.push({
        ok: false,
        candidate,
        failure: 'timeout',
        detail: `the probe round exceeded ${budgetMs} ms before this candidate was tried`,
      })
      break
    }
    const attempt = await probeInterpreter(candidate, {
      ...options,
      timeoutMs: Math.min(options.timeoutMs || PROBE_TIMEOUT_MS, remaining),
    })
    attempts.push(attempt)
    if (attempt.ok) return { selected: attempt, attempts }
    if (candidate.source === 'env' || candidate.source === 'config') break
  }

  return { selected: undefined, attempts }
}

/** One line per attempt, for the failure screen and the log. */
export function summariseAttempts(attempts: readonly PythonProbe[]): string {
  return attempts
    .map((attempt) => {
      const version = attempt.version ? ` ${attempt.version}` : ''
      const detail = attempt.detail ? ` (${attempt.detail})` : ''
      return `${attempt.candidate.command}${version} → ${attempt.failure || 'usable'}${detail}`
    })
    .join('; ')
}
