//! Wiring the local executor endpoint into the app's start-up.
//!
//! ## Why this module exists at all
//!
//! `startLocalExecutor` had no caller: the endpoint, the Python client
//! (`agent/desktop_local/script_executor.py`) and the contract were all written,
//! but nothing ever put the endpoint's origin and token into the backend's
//! environment. The result was silent and total -- `run_script` asked
//! `executor_endpoint()`, got `None`, and refused every script with "this machine
//! has no platform launcher". Nothing logged a start-up failure, because nothing
//! tried to start.
//!
//! ## Why it runs *before* the backend
//!
//! The backend caches the endpoint on first use and reads it from its own
//! environment, which is fixed when the process is spawned. So the order is not a
//! preference: start the endpoint, hand its address to the backend, *then* start
//! the backend. Doing it the other way round would leave the backend permanently
//! believing there is no launcher.
//!
//! ## Why the grant source needs no ordering care
//!
//! `remoteGrantRegistry` is a module-level singleton, so the endpoint queries the
//! same grants the panel and the read path use rather than a copy that could drift
//! from them. A grant activated after start-up is therefore visible to a script run
//! immediately -- which is what "the user just picked this project" has to mean.

import { app } from 'electron'

import { resolveGuardBinary } from '../local-files/fs-guard-binary'
import { remoteGrantRegistry } from '../remote/local-files-bridge'
import { startLocalExecutor, type RunningLocalExecutor } from './executor-endpoint'
import { InterpreterRegistry, findWorkerRuntime } from './interpreter'

/** The backend reads these; they are part of the backend's own documented input. */
export const EXECUTOR_URL_ENV = 'COW_DESKTOP_EXECUTOR_URL'
export const EXECUTOR_TOKEN_ENV = 'COW_DESKTOP_EXECUTOR_TOKEN'

let running: RunningLocalExecutor | null = null

/**
 * The environment the backend needs to reach the endpoint.
 *
 * Empty when no endpoint is running, which is the honest answer: the backend then
 * has no script surface rather than one it cannot reach.
 */
export function executorEnv(): Record<string, string> {
  if (!running) return {}
  return {
    [EXECUTOR_URL_ENV]: running.origin,
    [EXECUTOR_TOKEN_ENV]: running.token,
  }
}

/**
 * Start the endpoint, or explain why it could not be started.
 *
 * Returns `null` on failure instead of throwing: a build whose executor cannot bind
 * a loopback port should still open, and every script run will refuse with a real
 * reason. Crashing the shell here would take the file features down with the script
 * ones, and the file path does not depend on this at all.
 */
export async function startExecutorEndpoint(options: {
  backendPath: string
  callTimeoutMs?: number
}): Promise<RunningLocalExecutor | null> {
  if (running) return running
  const launcherBinary = resolveGuardBinary({
    role: 'launcher',
    resourcesPath: (process as { resourcesPath?: string }).resourcesPath,
    appPath: app.getAppPath(),
  })
  const interpreters = new InterpreterRegistry()
  const runtime = findWorkerRuntime(options.backendPath)
  try {
    running = await startLocalExecutor({
      grants: remoteGrantRegistry,
      backendPath: options.backendPath,
      runtime,
      probe: (candidate) => interpreters.get(candidate),
      platform: process.platform,
      seatbeltAvailable: false,
      baseEnv: process.env,
      ...(options.callTimeoutMs !== undefined ? { callTimeoutMs: options.callTimeoutMs } : {}),
      // The Job Object. Present only when this build shipped the launcher, so a
      // build without it keeps the historical spawn rather than refusing scripts.
      ...(launcherBinary
        ? { wrapper: { executable: launcherBinary, maxProcesses: 64, maxMemoryBytes: 4 * 1024 * 1024 * 1024 } }
        : {}),
    })
    console.log(
      `[local-executor] listening on ${running.origin}` +
      `${launcherBinary ? '' : ' (no launcher: script trees are not Job-confined)'}` +
      `${runtime ? '' : ' (no runtime: script runs will refuse)'}`,
    )
    return running
  } catch (err) {
    // Logged, not swallowed: a silently absent endpoint is exactly the state this
    // module was written to end.
    console.warn(`[local-executor] could not start: ${(err as Error)?.message || String(err)}`)
    running = null
    return null
  }
}

/** Close the endpoint. Safe to call when nothing was started. */
export async function stopExecutorEndpoint(): Promise<void> {
  const current = running
  running = null
  if (!current) return
  try {
    await current.close()
  } catch {
    /* a close failure must not block quitting */
  }
}

/** For diagnostics: what the endpoint says this platform supports. */
export function executorSupport(): ReturnType<RunningLocalExecutor['support']> | null {
  return running ? running.support() : null
}
