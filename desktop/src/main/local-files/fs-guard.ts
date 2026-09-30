/**
 * The main process side of the ``fs-guard`` stdio protocol.
 *
 * Change ``fix-desktop-local-context-and-tool-calls`` (task 2.4). A local read
 * has to happen *somewhere*, and design decision D6 puts it in a separate,
 * signed helper: the app's own process never walks a user directory, so the
 * only thing holding the authorised root descriptor is a binary whose entire op
 * list is ``list`` / ``stat`` / ``read`` / ``search`` (no write, no exec, no
 * network).
 *
 * Framing is a 4-byte big-endian length plus that many bytes of UTF-8 JSON, in
 * both directions. The length is read *before* the body, and the same 64 KiB
 * ceiling is enforced on the way out, so this side cannot ask the helper for
 * something the helper would have refused.
 *
 * Node-only (an injected ``spawn``), so ``tests/test_desktop_fs_guard.cjs``
 * drives the real binary -- when it is built -- and a stub child otherwise.
 */

/** Ceiling on one frame in either direction (mirrors ``MAX_FRAME_BYTES``). */
export const MAX_FRAME_BYTES = 64 * 1024

/** Largest single ``read`` the helper will answer. */
export const MAX_READ_BYTES = 1024 * 1024

/** Largest ``list`` page the helper will answer. */
export const MAX_PAGE = 1000

/** A refusal, with the helper's own stable code. */
export class FsGuardError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message)
  }
}

export interface FsGuardResult {
  grant: string
  grantVersion: number
  label: string
}

export interface ListEntry {
  name: string
  kind: string
  size: number | null
  modified: number
}

export interface ListResult {
  path: string
  grantVersion: number
  entries: ListEntry[]
  skipped: number
  truncated: boolean
  nextCursor: string | null
}

export interface StatResult {
  path: string
  grantVersion: number
  kind: string
  size: number | null
  modified: number
}

export interface ReadResult {
  path: string
  grantVersion: number
  offset: number
  bytes: number
  total: number
  truncated: boolean
  /** Always ``base64``: the helper never guesses at an encoding. */
  encoding: string
  data: string
}

export interface SearchHit {
  /** Path relative to the authorised root; this is what a follow-up read uses. */
  path: string
  kind: string
  size: number | null
  modified: number
}

export interface SearchResult {
  path: string
  grantVersion: number
  hits: SearchHit[]
  /** Entries examined before the walk stopped. */
  scanned: number
  skipped: number
  truncated: boolean
}

/** The child process surface this module uses. */
export interface GuardChild {
  stdin: { write(data: Buffer): void }
  stdout: { on(event: 'data', listener: (chunk: Buffer) => void): void }
  stderr?: { on(event: 'data', listener: (chunk: Buffer) => void): void }
  on(event: 'exit', listener: (code: number | null) => void): void
  on(event: 'error', listener: (err: Error) => void): void
  kill(): void
}

export type SpawnGuard = () => GuardChild

/** Frame one request body (4-byte big-endian length + JSON). */
export function encodeFrame(value: unknown): Buffer {
  const body = Buffer.from(JSON.stringify(value), 'utf8')
  if (body.byteLength > MAX_FRAME_BYTES) {
    throw new FsGuardError('frame_too_large', 'the request frame is too large')
  }
  const header = Buffer.alloc(4)
  header.writeUInt32BE(body.byteLength, 0)
  return Buffer.concat([header, body])
}

/**
 * A decoder for the helper's response stream.
 *
 * A body shorter than its prefix claims is a *desynchronised* stream, not a
 * short read: the helper never writes partial frames, so the only honest answer
 * is to stop using the connection.
 */
export class FrameReader {
  private buffer = Buffer.alloc(0)

  push(chunk: Buffer): unknown[] {
    this.buffer = Buffer.concat([this.buffer, chunk])
    const out: unknown[] = []
    for (;;) {
      if (this.buffer.length < 4) return out
      const length = this.buffer.readUInt32BE(0)
      if (length > MAX_FRAME_BYTES) {
        throw new FsGuardError('frame_too_large', 'the helper sent an oversized frame')
      }
      if (this.buffer.length < 4 + length) return out
      const body = this.buffer.subarray(4, 4 + length)
      this.buffer = this.buffer.subarray(4 + length)
      try {
        out.push(JSON.parse(body.toString('utf8')))
      } catch {
        throw new FsGuardError('invalid_frame', 'the helper sent an unreadable frame')
      }
    }
  }
}

interface Pending {
  resolve: (value: unknown) => void
  reject: (err: Error) => void
}

/**
 * One helper process, one request at a time.
 *
 * Serialised on purpose: the helper answers in order, so pipelining would only
 * make it harder to say which request a refusal belongs to. Cancellation is the
 * one exception -- ``cancel`` is written immediately, because the helper needs
 * to see it *while* the operation it targets is running.
 */
export class FsGuard {
  private child: GuardChild
  private reader = new FrameReader()
  private pending: Pending[] = []
  private queue: Promise<unknown> = Promise.resolve()
  private nextId = 1
  private closed = false

  constructor(private readonly spawn: SpawnGuard) {
    this.child = spawn()
    this.child.stdout.on('data', (chunk) => this.onData(chunk))
    this.child.on('error', (err) => this.failAll(new FsGuardError('io_error', err.message)))
    this.child.on('exit', () => {
      this.closed = true
      this.failAll(new FsGuardError('helper_exited', 'the file helper stopped'))
    })
  }

  get isClosed(): boolean {
    return this.closed
  }

  private onData(chunk: Buffer): void {
    let frames: unknown[]
    try {
      frames = this.reader.push(chunk)
    } catch (err) {
      this.failAll(err instanceof FsGuardError ? err : new FsGuardError('invalid_frame', String(err)))
      return
    }
    for (const frame of frames) {
      const pending = this.pending.shift()
      if (!pending) continue
      const record = (frame || {}) as { ok?: boolean; result?: unknown; error?: { code?: string; message?: string } }
      if (record.ok) {
        pending.resolve(record.result)
      } else {
        const code = record.error?.code || 'internal'
        pending.reject(new FsGuardError(code, record.error?.message || 'the file helper refused the request'))
      }
    }
  }

  private failAll(err: Error): void {
    const waiting = this.pending
    this.pending = []
    for (const pending of waiting) pending.reject(err)
    try {
      this.child.kill()
    } catch {
      /* already gone */
    }
  }

  /** Send one request and wait for its answer. */
  request(op: string, params: Record<string, unknown>, grant?: string): Promise<unknown> {
    if (this.closed) {
      return Promise.reject(new FsGuardError('helper_exited', 'the file helper is not running'))
    }
    const id = `rq_${this.nextId++}`
    const body = encodeFrame(grant ? { id, op, grant, params } : { id, op, params })
    const result = this.queue.then(
      () => new Promise<unknown>((resolve, reject) => {
        this.pending.push({ resolve, reject })
        try {
          this.child.stdin.write(body)
        } catch (err) {
          this.pending.pop()
          reject(new FsGuardError('io_error', (err as Error).message))
        }
      }),
    )
    // Keep the chain alive after a refusal: one failed request must not stop
    // the next one from being sent.
    this.queue = result.then(
      () => undefined,
      () => undefined,
    )
    return result
  }

  /** Open a root from the native picker's path. This is the only absolute path. */
  async openRoot(absolutePath: string): Promise<FsGuardResult> {
    const raw = (await this.request('open_root', { path: absolutePath })) as Record<string, unknown>
    return {
      grant: String(raw.grant || ''),
      grantVersion: Number(raw.grant_version || 0),
      label: String(raw.label || ''),
    }
  }

  async closeRoot(grant: string): Promise<void> {
    this.closeRootBestEffort(grant)
    await Promise.resolve()
  }

  /** Fire-and-forget close, for the revoke/teardown paths. */
  closeRootBestEffort(grant: string): void {
    if (!grant || this.closed) return
    try {
      this.child.stdin.write(encodeFrame({ id: `rq_${this.nextId++}`, op: 'close_root', grant, params: {} }))
      // The reply is deliberately not awaited: the caller is revoking and has
      // nothing to do with an acknowledgement.
      this.pending.push({ resolve: () => undefined, reject: () => undefined })
    } catch {
      /* the helper is already gone */
    }
  }

  /** Ask the helper to stop the named in-flight request. */
  cancel(requestId: string): void {
    if (this.closed) return
    try {
      this.child.stdin.write(encodeFrame({
        id: `rq_${this.nextId++}`,
        op: 'cancel',
        params: { request_id: requestId },
      }))
      this.pending.push({ resolve: () => undefined, reject: () => undefined })
    } catch {
      /* the helper is already gone */
    }
  }

  async list(
    grant: string,
    params: { path?: string; cursor?: string | null; pageSize?: number },
  ): Promise<ListResult> {
    const raw = (await this.request('list', {
      ...(params.path ? { path: params.path } : {}),
      ...(params.cursor ? { cursor: params.cursor } : {}),
      ...(params.pageSize ? { page_size: params.pageSize } : {}),
    }, grant)) as Record<string, unknown>
    return {
      path: String(raw.path || ''),
      grantVersion: Number(raw.grant_version || 0),
      entries: Array.isArray(raw.entries) ? (raw.entries as ListEntry[]) : [],
      skipped: Number(raw.skipped || 0),
      truncated: !!raw.truncated,
      nextCursor: typeof raw.next_cursor === 'string' ? raw.next_cursor : null,
    }
  }

  async stat(grant: string, path: string): Promise<StatResult> {
    const raw = (await this.request('stat', { path }, grant)) as Record<string, unknown>
    return {
      path: String(raw.path || ''),
      grantVersion: Number(raw.grant_version || 0),
      kind: String(raw.kind || ''),
      size: raw.size == null ? null : Number(raw.size),
      modified: Number(raw.modified || 0),
    }
  }

  async read(
    grant: string,
    params: { path: string; offset?: number; length?: number },
  ): Promise<ReadResult> {
    const raw = (await this.request('read', {
      path: params.path,
      ...(params.offset ? { offset: params.offset } : {}),
      ...(params.length ? { length: params.length } : {}),
    }, grant)) as Record<string, unknown>
    return {
      path: String(raw.path || ''),
      grantVersion: Number(raw.grant_version || 0),
      offset: Number(raw.offset || 0),
      bytes: Number(raw.bytes || 0),
      total: Number(raw.total || 0),
      truncated: !!raw.truncated,
      encoding: String(raw.encoding || 'base64'),
      data: String(raw.data || ''),
    }
  }

  async search(
    grant: string,
    params: {
      path?: string
      nameContains?: string
      textContains?: string
      maxResults?: number
      deadlineMs?: number
    },
  ): Promise<SearchResult> {
    const raw = (await this.request('search', {
      ...(params.path ? { path: params.path } : {}),
      ...(params.nameContains ? { name_contains: params.nameContains } : {}),
      ...(params.textContains ? { text_contains: params.textContains } : {}),
      ...(params.maxResults ? { max_results: params.maxResults } : {}),
      ...(params.deadlineMs ? { deadline_ms: params.deadlineMs } : {}),
    }, grant)) as Record<string, unknown>
    return {
      path: String(raw.path || ''),
      grantVersion: Number(raw.grant_version || 0),
      // The helper calls them ``results``; each is a path relative to the root.
      hits: Array.isArray(raw.results) ? (raw.results as SearchHit[]) : [],
      scanned: Number(raw.candidates || 0),
      skipped: Number(raw.skipped || 0),
      truncated: !!raw.truncated,
    }
  }

  /** Close stdin and stop the helper; every root descriptor dies with it. */
  dispose(): void {
    if (this.closed) return
    this.closed = true
    this.failAll(new FsGuardError('closed', 'the file helper was stopped'))
  }
}
