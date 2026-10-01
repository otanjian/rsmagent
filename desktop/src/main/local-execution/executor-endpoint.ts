/**
 * The loopback endpoint that lets the *same-machine* backend run a local script
 * under the platform launcher (change task 5.1).
 *
 * Why this exists at all: the local backend is the process that decides "this
 * turn acts in the user's project", but it is not the process that can confine
 * a script. The confinement (seatbelt profile, scrubbed environment, per-run
 * temp directory, process-tree budget) is group 4's launcher, which lives here.
 * So the backend asks this endpoint, and this endpoint is the *only* new
 * surface -- deliberately narrow:
 *
 * * **Loopback only.** The listener binds ``127.0.0.1`` and refuses a request
 *   whose peer is not loopback or that carries an ``Origin`` header, so a page
 *   cannot reach it even if it learns the port.
 * * **The launch token only.** Every request must carry the per-launch secret
 *   the shell put in the backend's own environment. A page cannot read it, so a
 *   compromised console cannot turn this into an execution service.
 * * **A closed op set.** One operation, ``bash``, and nothing that names an
 *   executable: this is "run the project's script tool", not a remote shell.
 * * **A named authorization, re-checked here.** The request carries identifiers
 *   (never a path); the directory comes from the live grant table. The grant
 *   must be a ``project-execution`` grant, its version must match, and the named
 *   cwd must be inside the granted root. Re-picking a directory bumps the
 *   version, so a stale request is a miss rather than a wrong directory.
 * * **The real result.** What the worker's own tool produced (status, output,
 *   exit code, truncation) is passed back unchanged, so the model sees a failing
 *   command fail.
 *
 * Kept as plain Node (no Electron import) so it runs under `node --test` with an
 * injected spawn, which is how its protocol is verified without an app build.
 */

import fs from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'
import type { AddressInfo } from 'node:net'
import type { spawn } from 'node:child_process'

import { buildGrantLaunchPlan } from './launch'
import type { InterpreterProbe, WorkerRuntime } from './interpreter'
import { runtimeKey } from './interpreter'
import { describeScriptCapabilities, resolveScriptSupport } from './sandbox'
import type { ScriptSupport } from './sandbox'
import { WorkerSession } from './worker-session'
import { newLaunchToken } from './launch-token'

/** The one operation this endpoint performs. Anything else is refused by name. */
export const EXECUTOR_TOOL = 'bash'

/** The paths the endpoint answers on. A closed set, checked before the body. */
export const EXECUTOR_CAPABILITIES_PATH = '/desktop-exec/capabilities'
export const EXECUTOR_SCRIPT_PATH = '/desktop-exec/script'

const MAX_BODY_BYTES = 1 << 20
const DEFAULT_CALL_TIMEOUT_MS = 300_000
/** Shown in the capability text when no grant has resolved yet. */
const PROJECT_PLACEHOLDER = '(the project directory)'
const TEMP_PLACEHOLDER = "(the run's temporary directory)"

/** The live grants, as this module needs to read them. */
export interface ExecutorGrantSource {
  list(): Array<{
    id: string
    userId: string
    tenantId: string
    deviceId: string
    grantVersion: number
    purpose: 'readonly-input' | 'project-execution'
  }>
  absolutePathFor(grantId: string): string | null
}

export interface LocalExecutorConfig {
  grants: ExecutorGrantSource
  /** The directory the Python package lives in (the worker's cwd). */
  backendPath: string
  /**
   * The runtime to run the worker with, or null when none was found.
   *
   * A source checkout passes an interpreter plus `-m`; an installed app passes
   * its own bundle binary plus a flag. The launcher never invents either, so a
   * build shape it does not understand is a refusal rather than a command that
   * only works in a checkout.
   */
  runtime: WorkerRuntime | null
  /** Probe for the runtime (its read allowances are derived from it). */
  probe: (runtime: WorkerRuntime) => InterpreterProbe | null
  platform?: NodeJS.Platform
  /** Whether `/usr/bin/sandbox-exec` exists; only consulted on darwin. */
  seatbeltAvailable?: boolean
  /** The parent environment, scrubbed per run by the launcher. */
  baseEnv?: NodeJS.ProcessEnv
  /** Where per-run temp directories are created. Defaults to the system temp. */
  tempBase?: string
  /**
   * The root the local skill cache lives under (task 8.8).
   *
   * The anchor a run's pinned skill directories are validated against. The
   * backend names the exact version directories -- it owns the cache, so the
   * shell cannot know them -- but naming a path is not the same as being trusted
   * with one: a root outside this anchor is refused rather than granted. Left
   * unset, no skill directory is ever granted, which is the honest answer for a
   * shell that has not told us where its cache is.
   */
  skillCacheRoot?: string
  callTimeoutMs?: number
  /** Only for tests; production uses `node:child_process.spawn`. */
  spawnFn?: typeof spawn
  /** Only for tests; production mints one per app run. */
  token?: string
}

export interface RunningLocalExecutor {
  /** e.g. ``http://127.0.0.1:53111``; what the backend is told. */
  origin: string
  token: string
  /** The platform's own answer, for logging. */
  support: () => ScriptSupport
  close: () => Promise<void>
}

interface GrantWorker {
  session: WorkerSession
  root: string
  tempRoot: string
  /** Serialises calls per grant: the worker refuses a concurrent call by design. */
  tail: Promise<unknown>
}

interface RequestOutcome {
  status: number
  body: Record<string, unknown>
}

function ok(body: Record<string, unknown>): RequestOutcome {
  return { status: 200, body }
}

function refuse(status: number, code: string, message: string): RequestOutcome {
  return { status, body: { code, message } }
}

/** Length-safe comparison; the token is short-lived and loopback-bound. */
function tokensMatch(expected: string, provided: string): boolean {
  if (!provided || provided.length !== expected.length) return false
  let diff = 0
  for (let i = 0; i < expected.length; i += 1) {
    diff |= expected.charCodeAt(i) ^ provided.charCodeAt(i)
  }
  return diff === 0
}

function isLoopbackPeer(address: string | undefined): boolean {
  if (!address) return false
  const value = address.replace(/^::ffff:/, '')
  return value === '::1' || value.startsWith('127.')
}

/** Whether ``candidate`` is the root itself or lives inside it. */
export function isInsideRoot(candidate: string, root: string): boolean {
  let realCandidate: string
  let realRoot: string
  try {
    realCandidate = fs.realpathSync(candidate)
  } catch {
    realCandidate = path.resolve(candidate)
  }
  try {
    realRoot = fs.realpathSync(root)
  } catch {
    return false
  }
  if (realCandidate === realRoot) return true
  const withSep = realRoot.endsWith(path.sep) ? realRoot : realRoot + path.sep
  return realCandidate.startsWith(withSep)
}

export async function startLocalExecutor(
  config: LocalExecutorConfig,
): Promise<RunningLocalExecutor> {
  const platform = config.platform ?? process.platform
  const token = config.token ?? newLaunchToken()
  const baseEnv = config.baseEnv ?? process.env
  const tempBase = config.tempBase ?? os.tmpdir()
  const workers = new Map<string, GrantWorker>()
  /** Probes are expensive (they spawn the interpreter); remember the answer. */
  const probes = new Map<string, InterpreterProbe | null>()
  let closing = false

  const probeOf = (): InterpreterProbe | null => {
    if (!config.runtime) return null
    const key = runtimeKey(config.runtime)
    if (!probes.has(key)) {
      probes.set(key, config.probe(config.runtime))
    }
    return probes.get(key) ?? null
  }

  const supportFor = (root: string, tempRoot: string): ScriptSupport => resolveScriptSupport({
    platform,
    pythonPath: config.runtime?.path,
    workerArgs: config.runtime?.args,
    seatbeltAvailable: config.seatbeltAvailable,
    paths: { projectRoot: root, tempRoot },
  })

  const support = (): ScriptSupport => supportFor(PROJECT_PLACEHOLDER, TEMP_PLACEHOLDER)

  const capabilitiesPayload = (root: string, tempRoot: string): Record<string, unknown> => {
    const answer = supportFor(root, tempRoot)
    if (!answer.supported) {
      return { supported: false, code: answer.code, reason: answer.reason }
    }
    return {
      supported: true,
      kind: answer.kind,
      platform: answer.capabilities.platform,
      description: describeScriptCapabilities(answer),
    }
  }

  const resolveGrant = (
    scope: Record<string, unknown>,
  ): { ok: true; grantId: string; root: string } | RequestOutcome => {
    const userId = String(scope.user_id ?? '')
    const tenantId = String(scope.tenant_id ?? '')
    const deviceId = String(scope.device_id ?? '')
    const claimedVersion = Number(scope.grant_version ?? 0)
    if (!userId || !deviceId) {
      return refuse(400, 'invalid_request', 'the request must name its user and device')
    }
    const matches = config.grants.list().filter((grant) => (
      grant.userId === userId
      && grant.tenantId === tenantId
      && grant.deviceId === deviceId
      && grant.purpose === 'project-execution'
      && grant.grantVersion === claimedVersion
    ))
    if (matches.length === 0) {
      // No guess and no nearest match: a re-picked directory bumps the version,
      // and a read-only grant is not an execution grant.
      return refuse(403, 'grant_revoked', 'no active project-execution grant matches this run')
    }
    if (matches.length > 1) {
      // Two live grants in one scope should be impossible; picking one would be
      // a coin flip over which directory gets written.
      return refuse(403, 'ambiguous_grant', 'more than one live grant matches this run')
    }
    const absolute = config.grants.absolutePathFor(matches[0].id)
    if (!absolute) {
      return refuse(403, 'grant_revoked', 'the grant no longer has a directory')
    }
    return { ok: true, grantId: matches[0].id, root: absolute }
  }

  /**
   * The run's pinned skill directories, or a refusal (task 8.8).
   *
   * Only directories that resolve inside the shell's own skill-cache root are
   * accepted. The backend has to name them -- they are the versions *it* pinned,
   * and the shell has no way to know which -- so the trust is placed in the
   * anchor instead of in the request: what the request may do is point at a
   * subdirectory of a directory the shell already owns, never at an arbitrary
   * path. A root that leaves the anchor is refused outright rather than dropped,
   * because a silently partial grant produces a sandboxed run that fails for a
   * reason nobody can see.
   */
  const skillRootsFor = (scope: Record<string, unknown>): string[] | RequestOutcome => {
    const claimed = scope.skill_roots
    if (claimed === undefined || claimed === null) return []
    if (!Array.isArray(claimed)) {
      return refuse(400, 'invalid_request', 'skill_roots must be a list of directories')
    }
    if (claimed.length === 0) return []
    const anchor = config.skillCacheRoot
    if (!anchor) {      return refuse(403, 'skill_cache_unavailable',
        'this backend asked for read-only skill directories, but the shell was not '
        + 'told where the skill cache lives, so no directory can be granted')
    }
    const roots: string[] = []
    for (const entry of claimed) {
      const candidate = typeof entry === 'string' ? entry.trim() : ''
      if (!candidate || !path.isAbsolute(candidate)) {
        return refuse(400, 'invalid_request', 'a skill root must be an absolute directory')
      }
      if (!isInsideRoot(candidate, anchor)) {
        return refuse(403, 'skill_root_outside_cache',
          'a requested skill directory is outside the skill cache')
      }
      roots.push(fs.realpathSync(candidate))
    }
    return Array.from(new Set(roots)).sort()
  }

  const workerFor = async (
    grantId: string, root: string, skillRoots: readonly string[],
  ): Promise<GrantWorker | RequestOutcome> => {
    // Keyed by the grant *and* its read-only set: the sandbox profile is built
    // once per worker, so a run that pins a different set of skill versions needs
    // its own worker rather than inheriting the previous run's grants. Keying on
    // the grant alone would also keep a wider worker alive after a narrower run
    // started, which is the same leak in the other direction.
    const key = [grantId, ...skillRoots].join('\u0000')
    const existing = workers.get(key)
    if (existing) return existing
    const tempRoot = fs.mkdtempSync(path.join(tempBase, 'cow-local-run-'))
    fs.chmodSync(tempRoot, 0o700)
    const plan = buildGrantLaunchPlan({
      platform,
      grant: { projectRoot: root, tempRoot, skillRoots },
      interpreter: probeOf(),
      seatbeltAvailable: config.seatbeltAvailable,
      backendPath: config.backendPath,
      baseEnv,
    })
    if (!plan.ok) {
      fs.rmSync(tempRoot, { recursive: true, force: true })
      return refuse(503, plan.code, plan.reason)
    }
    const session = new WorkerSession(
      plan,
      { onStderr: (chunk) => process.stderr.write(`[local-executor] ${chunk}`) },
      { spawnFn: config.spawnFn, callTimeoutMs: config.callTimeoutMs ?? DEFAULT_CALL_TIMEOUT_MS },
    )
    const reply = await session.hello(root, tempRoot)
    if (reply.status !== 'success') {
      await session.stop()
      fs.rmSync(tempRoot, { recursive: true, force: true })
      return refuse(503, 'worker_failed', reply.error?.message || 'the local worker did not start')
    }
    const worker: GrantWorker = { session, root, tempRoot, tail: Promise.resolve() }
    workers.set(key, worker)
    return worker
  }

  const runScript = async (
    scope: Record<string, unknown>,
    cwd: string,
    tool: string,
    args: Record<string, unknown>,
    timeoutMs: number | undefined,
  ): Promise<RequestOutcome> => {
    if (tool !== EXECUTOR_TOOL) {
      // A closed set, checked by name: this endpoint is not a remote shell.
      return refuse(400, 'unknown_tool', `this endpoint runs ${EXECUTOR_TOOL} only`)
    }
    if (!cwd) return refuse(400, 'invalid_request', 'the request must name its working directory')
    const resolved = resolveGrant(scope)
    if (!('ok' in resolved)) return resolved
    if (!isInsideRoot(cwd, resolved.root)) {
      return refuse(400, 'path_outside_project',
        'the working directory is outside the granted project')
    }
    const skills = skillRootsFor(scope)
    if (!Array.isArray(skills)) return skills
    const worker = await workerFor(resolved.grantId, resolved.root, skills)
    if (!('session' in worker)) return worker
    // One call at a time per grant: the worker refuses a concurrent call, and a
    // queue keeps a second message from being answered with "another call is
    // already running" while the first is still doing real work.
    const run = worker.tail.then(async (): Promise<RequestOutcome> => {
      const started = Date.now()
      const reply = await worker.session.call(EXECUTOR_TOOL, args, timeoutMs)
      if (reply.error) {
        const code = reply.error.code
        // A tool that ran and failed, or was stopped, is the model's outcome to
        // read -- not a transport refusal. Only the runtime breaking is a 5xx.
        if (code === 'timeout' || code === 'cancelled' || code === 'unknown_tool'
            || code === 'invalid_request' || code === 'path_outside_project') {
          return ok({
            status: 'error',
            tool: EXECUTOR_TOOL,
            result: reply.error.message,
            ext_data: { worker_code: code },
          })
        }
        return refuse(503, code, reply.error.message)
      }
      return ok({
        status: reply.status,
        tool: EXECUTOR_TOOL,
        result: reply.result ?? null,
        display: reply.display ?? null,
        ext_data: reply.ext_data ?? null,
        duration_ms: typeof reply.duration_ms === 'number' ? reply.duration_ms : Date.now() - started,
      })
    })
    // Keep the chain alive after a failure so one bad call does not wedge the grant.
    worker.tail = run.catch(() => undefined)
    return run
  }

  const server = http.createServer((req, res) => {
    const finish = (outcome: RequestOutcome) => {
      if (res.headersSent) return
      const data = Buffer.from(JSON.stringify(outcome.body), 'utf8')
      res.writeHead(outcome.status, {
        'Content-Type': 'application/json',
        'Content-Length': String(data.length),
        // No caching: this surface exists for a process on this machine only.
        'Cache-Control': 'no-store',
      })
      res.end(data)
    }
    void (async () => {
      if (closing) return finish(refuse(503, 'shutting_down', 'the executor is stopping'))
      if (req.method !== 'POST') return finish(refuse(405, 'invalid_request', 'POST is required'))
      if (!isLoopbackPeer(req.socket.remoteAddress)) {
        return finish(refuse(403, 'permission_denied', 'the local executor is loopback-only'))
      }
      if (req.headers.origin) {
        // A document origin means a page reached this listener; the shell's own
        // requests are process-to-process and never carry one.
        return finish(refuse(403, 'permission_denied', 'a document origin is not accepted'))
      }
      const authorization = String(req.headers.authorization || '')
      const provided = authorization.startsWith('Bearer ') ? authorization.slice(7) : ''
      if (!tokensMatch(token, provided)) {
        return finish(refuse(403, 'desktop_token_required', 'the desktop launch token is required'))
      }
      const url = req.url || ''
      const queryAt = url.indexOf('?')
      const pathname = queryAt >= 0 ? url.slice(0, queryAt) : url
      if (pathname !== EXECUTOR_SCRIPT_PATH && pathname !== EXECUTOR_CAPABILITIES_PATH) {
        return finish(refuse(404, 'not_found', 'no such operation'))
      }
      const chunks: Buffer[] = []
      let size = 0
      for await (const chunk of req) {
        const buffer = chunk as Buffer
        size += buffer.length
        if (size > MAX_BODY_BYTES) {
          return finish(refuse(413, 'invalid_request', 'the request body is too large'))
        }
        chunks.push(buffer)
      }
      let body: Record<string, unknown> = {}
      const raw = Buffer.concat(chunks).toString('utf8')
      if (raw.trim()) {
        try {
          const parsed = JSON.parse(raw)
          if (typeof parsed !== 'object' || parsed === null) {
            return finish(refuse(400, 'invalid_request', 'the body must be a JSON object'))
          }
          body = parsed as Record<string, unknown>
        } catch {
          return finish(refuse(400, 'invalid_request', 'the body must be JSON'))
        }
      }
      const scope = (body.scope && typeof body.scope === 'object')
        ? body.scope as Record<string, unknown>
        : {}
      if (pathname === EXECUTOR_CAPABILITIES_PATH) {
        if (!scope.device_id) {
          // Platform facts only: no grant was named, so no directory is claimed.
          return finish(ok(capabilitiesPayload(PROJECT_PLACEHOLDER, TEMP_PLACEHOLDER)))
        }
        const resolved = resolveGrant(scope)
        if (!('ok' in resolved)) {
          // A named authorization that no longer resolves: report *its* reason,
          // so the caller can say "grant revoked" instead of "no launcher".
          return finish(ok({
            supported: false,
            code: String(resolved.body.code ?? 'grant_revoked'),
            reason: String(resolved.body.message ?? 'this run has no active project grant'),
          }))
        }
        const skills = skillRootsFor(scope)
        if (!Array.isArray(skills)) {
          return finish(ok({
            supported: false,
            code: String(skills.body.code ?? 'skill_root_refused'),
            reason: String(skills.body.message ?? 'this run may not read its skills'),
          }))
        }
        const worker = await workerFor(resolved.grantId, resolved.root, skills)
        if (!('session' in worker)) {
          return finish(ok({
            supported: false,
            code: String(worker.body.code ?? 'worker_failed'),
            reason: String(worker.body.message ?? 'the local worker could not be started'),
          }))
        }
        return finish(ok(capabilitiesPayload(resolved.root, worker.tempRoot)))
      }
      return finish(await runScript(
        scope,
        String(body.cwd ?? ''),
        String(body.tool ?? ''),
        (body.arguments && typeof body.arguments === 'object')
          ? body.arguments as Record<string, unknown>
          : {},
        typeof body.timeout_ms === 'number' ? body.timeout_ms : undefined,
      ))
    })().catch((err) => {
      finish(refuse(500, 'internal_error', String((err as Error)?.message || err)))
    })
  })

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => resolve())
  })
  const address = server.address() as AddressInfo
  const origin = `http://127.0.0.1:${address.port}`

  const close = async (): Promise<void> => {
    closing = true
    await new Promise<void>((resolve) => server.close(() => resolve()))
    for (const worker of workers.values()) {
      try {
        await worker.session.stop()
      } catch {
        /* the worker may already be gone */
      }
      try {
        fs.rmSync(worker.tempRoot, { recursive: true, force: true })
      } catch {
        /* best effort: a temp dir left behind is reported, not fatal */
      }
    }
    workers.clear()
  }

  return { origin, token, support, close }
}
