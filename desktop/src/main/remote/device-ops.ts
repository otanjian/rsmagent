/**
 * Turn one gateway ``command`` frame into a real, bounded local read.
 *
 * Change ``fix-desktop-local-context-and-tool-calls`` (task 2.4). This is the
 * step the old flow was missing end to end: a command reached the *server*
 * queue and nothing on the machine ever ran it. The mapping is therefore
 * narrow and total:
 *
 * * only the ops this fix claims (``list`` / ``stat`` / ``search`` /
 *   ``read_text``) are executed; ``materialize`` and ``inspect`` answer
 *   ``feature_unavailable`` rather than a fabricated success;
 * * every parameter is re-clamped to the contract bound here, because the
 *   device must not trust the server (or a page upstream of it) for its own
 *   resource limits;
 * * a refusal from the helper becomes a *failed* command with the helper's own
 *   code -- never an empty but successful result, which would read to the model
 *   as "that directory is empty".
 *
 * Pure logic over an injected helper client, so it is tested without Electron.
 */

import type { FsGuard } from '../local-files/fs-guard'

/** Contract bounds (``contracts/desktop/v1.json``; D5). */
export const LIST_LIMIT_MAX = 200
export const READ_TEXT_LIMIT_MAX = 16384
export const SEARCH_RESULTS_MAX = 100
export const SEARCH_SECONDS = 30

/** Encodings ``read_text`` may declare. Anything else is refused, not guessed. */
const TEXT_ENCODINGS = new Set(['utf-8', 'utf8', 'utf-16le', 'utf16le', 'latin1', 'ascii'])

export interface DeviceCommand {
  request_id: string
  connection_epoch: string
  workspace_id: string
  op: string
  params: Record<string, unknown>
  deadline?: number
}

export interface DeviceResult {
  /** ``succeeded`` or ``failed``: the only two this side ever reports. */
  state: 'succeeded' | 'failed'
  result?: Record<string, unknown>
  errorCode?: string
  errorMessage?: string
}

/** The ops this fix executes against a picked directory. */
export const SUPPORTED_OPS = ['list', 'stat', 'search', 'read_text'] as const

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function asInt(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return Math.trunc(value)
  if (typeof value === 'string' && value.trim() !== '' && /^-?\d+$/.test(value.trim())) {
    return Number(value.trim())
  }
  return null
}

/** Clamp a caller's page size into the contract bound, or the bound itself. */
export function clampPageSize(raw: unknown): number {
  const value = asInt(raw)
  if (value == null || value <= 0) return LIST_LIMIT_MAX
  return Math.min(value, LIST_LIMIT_MAX)
}

/** Clamp a read length into the contract bound. */
export function clampReadLength(raw: unknown): number {
  const value = asInt(raw)
  if (value == null || value <= 0) return READ_TEXT_LIMIT_MAX
  return Math.min(value, READ_TEXT_LIMIT_MAX)
}

/**
 * Decode the helper's bytes into text for the declared encoding.
 *
 * Invalid UTF-8 is *replaced*, not thrown: a directory can legitimately hold a
 * file whose bytes are not valid text, and the honest answer is the text with
 * the replacement characters plus ``encoding`` saying what was assumed.
 */
export function decodeText(data: Buffer, encoding: string): string {
  const name = encoding.toLowerCase()
  if (name === 'utf-16le' || name === 'utf16le') return data.toString('utf16le')
  if (name === 'latin1' || name === 'ascii') return data.toString('latin1')
  return data.toString('utf8')
}

/**
 * Run one command.
 *
 * ``fsGrant`` is the helper's own grant for the workspace the server named;
 * resolving *which* workspace that is (and whether it is still live) is the
 * caller's job and is re-checked on every command.
 */
export async function runDeviceCommand(
  guard: FsGuard,
  fsGrant: string,
  command: DeviceCommand,
): Promise<DeviceResult> {
  const params = command.params || {}
  const relativePath = asString(params.relative_path)

  try {
    switch (command.op) {
      case 'list': {
        const page = await guard.list(fsGrant, {
          path: relativePath,
          cursor: asString(params.cursor) || null,
          pageSize: clampPageSize(params.limit),
        })
        return {
          state: 'succeeded',
          result: {
            op: 'list',
            path: page.path,
            grant_version: page.grantVersion,
            entries: page.entries,
            skipped: page.skipped,
            // Paging and truncation travel as the helper reported them: a
            // partial listing must not read as a complete directory.
            truncated: page.truncated,
            next_cursor: page.nextCursor,
          },
        }
      }
      case 'stat': {
        const info = await guard.stat(fsGrant, relativePath)
        return {
          state: 'succeeded',
          result: {
            op: 'stat',
            path: info.path,
            grant_version: info.grantVersion,
            kind: info.kind,
            size: info.size,
            modified: info.modified,
          },
        }
      }
      case 'search': {
        const mode = asString(params.mode)
        const query = asString(params.query)
        if (!query) {
          return { state: 'failed', errorCode: 'invalid_request', errorMessage: 'search needs a query' }
        }
        if (mode !== 'name' && mode !== 'text') {
          return { state: 'failed', errorCode: 'invalid_request', errorMessage: 'search mode must be name or text' }
        }
        const found = await guard.search(fsGrant, {
          path: relativePath,
          ...(mode === 'name' ? { nameContains: query } : { textContains: query }),
          maxResults: SEARCH_RESULTS_MAX,
          deadlineMs: SEARCH_SECONDS * 1000,
        })
        return {
          state: 'succeeded',
          result: {
            op: 'search',
            mode,
            query,
            path: found.path,
            grant_version: found.grantVersion,
            // One entry per hit: a path relative to the authorised root, so the
            // model's follow-up read needs no rewriting.
            hits: found.hits,
            scanned: found.scanned,
            skipped: found.skipped,
            truncated: found.truncated,
          },
        }
      }
      case 'read_text': {
        const encoding = (asString(params.encoding) || 'utf-8').toLowerCase()
        if (!TEXT_ENCODINGS.has(encoding)) {
          return {
            state: 'failed',
            errorCode: 'invalid_request',
            errorMessage: 'unsupported text encoding',
          }
        }
        const offset = Math.max(0, asInt(params.offset) ?? 0)
        const length = clampReadLength(params.limit)
        const chunk = await guard.read(fsGrant, { path: relativePath, offset, length })
        const bytes = Buffer.from(chunk.data, 'base64')
        return {
          state: 'succeeded',
          result: {
            op: 'read_text',
            path: chunk.path,
            grant_version: chunk.grantVersion,
            offset: chunk.offset,
            bytes: chunk.bytes,
            total: chunk.total,
            truncated: chunk.truncated,
            encoding,
            text: decodeText(bytes, encoding),
          },
        }
      }
      default:
        // Not a "no files" answer: the tool must be able to say the device
        // cannot do this yet instead of showing an empty result.
        return {
          state: 'failed',
          errorCode: 'feature_unavailable',
          errorMessage: `the desktop device does not implement ${command.op} yet`,
        }
    }
  } catch (err) {
    const code = (err as { code?: unknown } | null)?.code
    const message = (err as { message?: unknown } | null)?.message
    return {
      state: 'failed',
      errorCode: typeof code === 'string' && code ? code : 'device_error',
      errorMessage: typeof message === 'string' && message ? message : 'the local read failed',
    }
  }
}

/** The ``result`` frame the gateway expects for one command (contracts §5). */
export function resultFrame(
  command: DeviceCommand,
  epoch: string,
  result: DeviceResult,
): Record<string, unknown> {
  return {
    v: 1,
    type: 'result',
    request_id: command.request_id,
    connection_epoch: epoch,
    state: result.state,
    ...(result.state === 'succeeded' ? { result: result.result ?? {} } : {}),
    ...(result.errorCode ? { error_code: result.errorCode } : {}),
    ...(result.errorMessage ? { error_message: result.errorMessage } : {}),
  }
}
