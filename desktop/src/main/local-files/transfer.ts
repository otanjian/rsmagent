/**
 * Single-chunk-window upload of a granted local file (task 10.4).
 *
 * Pure Node on purpose: identity / size / mtime checks, one in-flight chunk,
 * one auto-retry on source change, and the final hash are unit-tested without
 * Electron. Production opens the source through the fs-guard helper (held
 * descriptor) and posts chunks to the HTTPS transfer API; this module owns the
 * windowing and version checks, not the transport.
 *
 * Absolute paths never leave this process in request bodies — only the opaque
 * source_ref / source_version the server already knows.
 */

import { createHash } from 'crypto'
import { openSync, readSync, fstatSync, closeSync } from 'fs'
import type { Stats } from 'fs'

export const DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024

export interface SourceIdentity {
  /** Opaque server-side source_ref (never an absolute path). */
  sourceRef: string
  /** size + mtimeMs fingerprint used as source_version. */
  sourceVersion: string
  size: number
  mtimeMs: number
}

export interface SourceHandle {
  identity: SourceIdentity
  /** Read up to `length` bytes at `offset`. */
  read(offset: number, length: number): Promise<Buffer>
  /** Re-stat the held object; detect replacement / truncation. */
  restat(): Promise<SourceIdentity>
  close(): Promise<void>
}

export interface ChunkPoster {
  (
    offset: number,
    body: Buffer,
    sha256: string,
  ): Promise<{ acknowledged_offset: number; state: string }>
}

export interface TransferCreate {
  (args: {
    sourceRef: string
    sourceVersion: string
    totalBytes: number
    filename: string
  }): Promise<{
    id: string
    chunk_size: number
    acknowledged_offset: number
  }>
}

export interface TransferCommit {
  (args: {
    transferId: string
    totalBytes: number
    sha256: string
    sourceVersionAfter: string
  }): Promise<{ state: string; artifact_ref?: string }>
}

export class TransferError extends Error {
  readonly code: string
  constructor(code: string, message: string) {
    super(message)
    this.code = code
  }
}

/** Build the contract source_version from size + mtime. */
export function sourceVersionOf(size: number, mtimeMs: number): string {
  return `${size}:${Math.trunc(mtimeMs)}`
}

/**
 * Open a local file under a known grant root and hold the fd.
 *
 * Callers must already have authorised `absolutePath` through the grant
 * registry / fs-guard; this helper only holds the descriptor and fingerprints.
 */
export function openSourceHandle(
  absolutePath: string,
  sourceRef: string,
): SourceHandle {
  if (!absolutePath || absolutePath.includes('\0')) {
    throw new TransferError('invalid_path', 'invalid absolute path')
  }
  const fd = openSync(absolutePath, 'r')
  const first = fstatSync(fd)
  if (!first.isFile()) {
    closeSync(fd)
    throw new TransferError('invalid_path', 'source is not a regular file')
  }
  const identity: SourceIdentity = {
    sourceRef,
    sourceVersion: sourceVersionOf(first.size, first.mtimeMs),
    size: first.size,
    mtimeMs: first.mtimeMs,
  }
  let closed = false
  return {
    identity,
    async read(offset: number, length: number): Promise<Buffer> {
      if (closed) throw new TransferError('invalid_request', 'handle closed')
      const buf = Buffer.alloc(length)
      const n = readSync(fd, buf, 0, length, offset)
      return n === length ? buf : buf.subarray(0, n)
    },
    async restat(): Promise<SourceIdentity> {
      if (closed) throw new TransferError('invalid_request', 'handle closed')
      const st: Stats = fstatSync(fd)
      return {
        sourceRef,
        sourceVersion: sourceVersionOf(st.size, st.mtimeMs),
        size: st.size,
        mtimeMs: st.mtimeMs,
      }
    },
    async close(): Promise<void> {
      if (closed) return
      closed = true
      closeSync(fd)
    },
  }
}

function sha256Hex(buf: Buffer): string {
  return createHash('sha256').update(buf).digest('hex')
}

/**
 * Upload one file with a single in-flight chunk window.
 *
 * On source identity change mid-transfer, retries from offset 0 at most once
 * (task 10.4); a second change raises `file_changed`.
 */
export async function uploadWithSingleChunkWindow(args: {
  handle: SourceHandle
  filename: string
  create: TransferCreate
  putChunk: ChunkPoster
  commit: TransferCommit
  chunkSize?: number
}): Promise<{ transferId: string; sha256: string; artifact_ref?: string }> {
  const chunkSize = args.chunkSize ?? DEFAULT_CHUNK_SIZE
  let attempt = 0
  let lastError: TransferError | null = null

  while (attempt < 2) {
    attempt += 1
    const before = await args.handle.restat()
    const created = await args.create({
      sourceRef: before.sourceRef,
      sourceVersion: before.sourceVersion,
      totalBytes: before.size,
      filename: args.filename,
    })
    const hasher = createHash('sha256')
    let offset = created.acknowledged_offset || 0
    try {
      while (offset < before.size) {
        const want = Math.min(chunkSize, before.size - offset)
        const body = await args.handle.read(offset, want)
        if (body.length !== want) {
          throw new TransferError('file_changed', 'short read from source')
        }
        hasher.update(body)
        const digest = sha256Hex(body)
        const ack = await args.putChunk(offset, body, digest)
        if (ack.acknowledged_offset < offset + body.length) {
          throw new TransferError(
            'invalid_request',
            'server acknowledged fewer bytes than sent',
          )
        }
        offset = ack.acknowledged_offset
      }
      const after = await args.handle.restat()
      if (after.sourceVersion !== before.sourceVersion) {
        throw new TransferError(
          'file_changed',
          'source identity changed during upload',
        )
      }
      const sha256 = hasher.digest('hex')
      const committed = await args.commit({
        transferId: created.id,
        totalBytes: before.size,
        sha256,
        sourceVersionAfter: after.sourceVersion,
      })
      return {
        transferId: created.id,
        sha256,
        artifact_ref: committed.artifact_ref,
      }
    } catch (err) {
      if (err instanceof TransferError && err.code === 'file_changed') {
        lastError = err
        continue
      }
      throw err
    }
  }
  throw lastError || new TransferError('file_changed', 'source changed twice')
}
