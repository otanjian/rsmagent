/**
 * The per-launch secret the desktop shell gives the backend it starts.
 *
 * Change ``align-desktop-project-execution-with-master`` (task 5.1/5.2). Two
 * things in this app are reachable only over loopback *and* only when the caller
 * proves it is the shell that started the backend:
 *
 * * registering the absolute root a native picker returned
 *   (``integrations/desktop/local_root.py`` checks ``X-COW-DESKTOP-TOKEN``),
 * * asking the platform launcher to run a script (the executor endpoint below).
 *
 * A browser tab on the same port can read neither the backend's environment nor
 * this process's, so a token minted here and passed only through the spawn
 * environment distinguishes the shell from a page -- which is the whole reason
 * it exists. It is **not** an identity and not a session: it grants nothing on
 * its own, and every request that carries it is still authorized against the
 * caller's own device / workspace / binding.
 *
 * Held in a module rather than on a class because there is exactly one backend
 * per app run: a second token would only create two answers to one question.
 */

import { randomBytes } from 'node:crypto'

/** Header the backend reads the launch token from (see `_desktop_token_matches`). */
export const DESKTOP_TOKEN_HEADER = 'x-cow-desktop-token'

/** Env var names the backend reads. Kept here so both sides cannot drift. */
export const DESKTOP_TOKEN_ENV = 'COW_DESKTOP_TOKEN'
export const DESKTOP_EXECUTOR_URL_ENV = 'COW_DESKTOP_EXECUTOR_URL'
export const DESKTOP_EXECUTOR_TOKEN_ENV = 'COW_DESKTOP_EXECUTOR_TOKEN'
/**
 * Where the shell tells itself the backend's skill cache lives (task 8.8).
 *
 * *Only* the shell reads this: it is not a permission and grants nothing. It is
 * the anchor the executor endpoint validates a run's requested read-only skill
 * directories against, so that "the backend names the exact version directories"
 * cannot become "the backend names any directory it likes". The value is
 * computed by the shell from the same state root it launched the backend with,
 * never taken from a request or from the backend's environment.
 */
export const DESKTOP_SKILL_CACHE_ROOT_ENV = 'COW_DESKTOP_SKILL_CACHE_ROOT'

let currentToken: string | null = null

/** 32 bytes of CSPRNG entropy, url-safe. */
export function newLaunchToken(bytes = 32): string {
  return randomBytes(bytes).toString('base64url')
}

/**
 * The token for this app run, minted on first use, or `null` when unset.
 *
 * ``null`` is a state tests use deliberately (a source run has no shell), and
 * every caller treats it as "this feature is unavailable" rather than inventing
 * a token -- a default would be the same mistake as a default password.
 */
export function desktopLaunchToken(): string | null {
  return currentToken
}

/** Set the token this run uses. ``null`` clears it (tests, shutdown). */
export function setDesktopLaunchToken(token: string | null): void {
  currentToken = token && token.trim() ? token.trim() : null
}

/**
 * Mint the token for this run, reusing it if one already exists.
 *
 * Reuse matters: the backend is spawned with whatever is current, and a restart
 * of the backend (dev reload, crash recovery) must not leave the shell holding
 * a different secret from the process it just started.
 */
export function ensureDesktopLaunchToken(bytes = 32): string {
  if (!currentToken) currentToken = newLaunchToken(bytes)
  return currentToken
}

/** The headers a request to the same-machine backend must carry. */
export function desktopTokenHeaders(token: string | null = currentToken): Record<string, string> {
  return token ? { [DESKTOP_TOKEN_HEADER]: token } : {}
}
