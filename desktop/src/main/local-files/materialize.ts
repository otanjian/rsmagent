/**
 * Deliver one granted local file to the server, explicitly and only on request.
 *
 * Change ``align-desktop-project-execution-with-master`` (task 9.7). Two claims
 * meet here, and they are not the same claim:
 *
 * * *the local file stays local* -- no card, preview, refresh or run ever turns
 *   a reference into an upload by itself;
 * * *the user may hand one file over* -- and when they do, the copy is made
 *   through the existing chunked transfer path (quota reservation, ownership,
 *   integrity, audit) and the local reference is left exactly as it was.
 *
 * So this module is only ever entered from an explicit ``materialize`` command,
 * and it is deliberately the *server's* command that drives it: the server
 * already queued the request against a live binding, and the device re-checks
 * the file it was asked about here.
 *
 * Bytes are read through the fs-guard helper rather than through a fresh
 * descriptor, which keeps two properties this change already paid for:
 *
 * * no absolute path is ever constructed in this path (the helper resolves
 *   relative paths against the root descriptor it re-checks on every read);
 * * a revoked grant stops the upload mid-flight instead of at the next command.
 *
 * The helper clamps one read to 32 KiB (``native/fs-guard/src/ops.rs``), so the
 * window this module hands to the uploader is assembled from bounded reads --
 * the bound is the helper's, restated here rather than guessed.
 */

import type { FsGuard } from './fs-guard'
import {
  TransferError,
  uploadWithSingleChunkWindow,
  type ChunkPoster,
  type SourceHandle,
  type TransferCommit,
  type TransferCreate,
} from './transfer'

/** The helper's own per-read bound (``MAX_READ_CHUNK``), which we never exceed. */
export const GUARD_READ_CHUNK = 32 * 1024

/** Server-side ``limits.file_max_bytes``: refuse here, before anything moves. */
export const MATERIALIZE_MAX_BYTES = 512 * 1024 * 1024

/** The upload window; one in flight, matching the transfer contract. */
export const MATERIALIZE_CHUNK_BYTES = 4 * 1024 * 1024

/** The three transfer calls, as :mod:`./transfer` already defines them. */
export interface MaterializeTransport {
  create: TransferCreate
  putChunk: ChunkPoster
  commit: TransferCommit
}

export interface MaterializeInput {
  guard: FsGuard
  /** The helper's grant for the root this file lives under. */
  fsGrant: string
  /** Path relative to that root; never absolute. */
  relativePath: string
  /**
   * The version the caller approved, when it had one.
   *
   * Format is what this device's own ``stat`` reported: ``<size>:<modified>``
   * (modified in whole seconds, as the helper reports it). An empty value means
   * "no version was approved" and the file's current version travels instead --
   * it is a convenience for the caller, never a bypass of the check.
   */
  expectedVersion?: string
  /** The server command id; the transfer is keyed on it and so is idempotent. */
  commandId: string
  /** The bound workspace the server named; part of the source_ref only. */
  workspaceId: string
  transport: MaterializeTransport
  chunkBytes?: number
}

export interface MaterializeOutcome {
  transfer_id: string
  artifact_ref: string
  sha256: string
  filename: string
  total_bytes: number
  /** Provenance, carried onto the server copy: which local file, which version. */
  source_ref: string
  source_version: string
}

/**
 * The device's own version vocabulary for a local file.
 *
 * Deliberately the same string a ``stat`` result carries, so a caller that
 * approved a version has something it can pass back without inventing a format.
 */
export function localVersionOf(size: number, modified: number): string {
  return `${Math.trunc(size)}:${Math.trunc(modified)}`
}

/**
 * An opaque provenance token for the server copy.
 *
 * A workspace id plus the project-relative path: enough for a human to tell
 * which local file a server copy came from, and structurally unable to carry an
 * absolute path or a home directory.
 */
export function sourceRefFor(workspaceId: string, relativePath: string): string {
  const relative = relativePath.replace(/\\/g, '/').replace(/^\/+/, '')
  return `desktop-file:${workspaceId || 'unbound'}:${relative}`
}

/** The published name: the last path segment, with control characters removed. */
export function safeLocalName(relativePath: string): string {
  const text = relativePath.replace(/\\/g, '/').split('/').filter(Boolean).pop() || ''
  const cleaned = Array.from(text).filter((ch) => ch.charCodeAt(0) >= 32).join('').trim()
  return cleaned && cleaned !== '.' && cleaned !== '..' ? cleaned : 'file'
}

/**
 * A :class:`SourceHandle` over the helper, so windowed upload reuses one code
 * path for every transfer this app makes.
 */
export function guardSourceHandle(args: {
  guard: FsGuard
  fsGrant: string
  relativePath: string
  sourceRef: string
  size: number
  modified: number
  guardChunk?: number
}): SourceHandle {
  const chunk = args.guardChunk ?? GUARD_READ_CHUNK
  const identity = () => ({
    sourceRef: args.sourceRef,
    sourceVersion: localVersionOf(args.size, args.modified),
    size: args.size,
    mtimeMs: Math.trunc(args.modified) * 1000,
  })
  return {
    identity: identity(),
    async read(offset: number, length: number): Promise<Buffer> {
      const parts: Buffer[] = []
      let got = 0
      while (got < length) {
        const want = Math.min(chunk, length - got)
        const page = await args.guard.read(args.fsGrant, {
          path: args.relativePath,
          offset: offset + got,
          length: want,
        })
        const bytes = Buffer.from(page.data, 'base64')
        if (bytes.length === 0) break
        parts.push(bytes)
        got += bytes.length
      }
      return Buffer.concat(parts)
    },
    async restat() {
      const info = await args.guard.stat(args.fsGrant, args.relativePath)
      if (info.size === null || info.size === undefined) {
        throw new TransferError('io_error', 'the local file size could not be read')
      }
      return {
        sourceRef: args.sourceRef,
        sourceVersion: localVersionOf(info.size, info.modified),
        size: info.size,
        mtimeMs: Math.trunc(info.modified) * 1000,
      }
    },
    // The root descriptor belongs to the guard, which closes it with the app.
    async close(): Promise<void> {},
  }
}

/**
 * Read one granted file and publish an independent server copy of it.
 *
 * Refusals are :class:`TransferError` codes, and each one means something the
 * caller can act on: ``not_a_file`` (a directory has no bytes to send),
 * ``limit_exceeded`` (the server would refuse it anyway), ``file_changed`` (the
 * file is not the one that was approved), plus the guard's own ``io_error`` /
 * ``path_outside_root`` when the reference no longer resolves.
 */
export async function materializeLocalFile(
  input: MaterializeInput,
): Promise<MaterializeOutcome> {
  const relative = (input.relativePath || '').trim()
  if (!relative || relative.includes('\0')) {
    throw new TransferError('invalid_request', 'a relative path is required')
  }

  const info = await input.guard.stat(input.fsGrant, relative)
  if (info.kind !== 'file') {
    throw new TransferError('not_a_file', 'only a regular file can be delivered')
  }
  if (info.size === null || info.size === undefined || info.size < 0) {
    throw new TransferError('io_error', 'the local file size could not be read')
  }
  const size = Number(info.size)
  if (size > MATERIALIZE_MAX_BYTES) {
    throw new TransferError(
      'limit_exceeded',
      `the file exceeds the ${MATERIALIZE_MAX_BYTES} byte limit`,
    )
  }

  const current = localVersionOf(size, info.modified)
  const expected = (input.expectedVersion || '').trim()
  if (expected && expected !== current) {
    throw new TransferError(
      'file_changed',
      'the local file changed since it was approved',
    )
  }
  // Once approved (or observed), the version is *pinned*: a copy that silently
  // picked up a later edit would be a different file than the one the caller
  // agreed to send. The server compares the version it was given with the one
  // the upload declares, so a change mid-flight is refused there too.
  const approved = expected || current

  const sourceRef = sourceRefFor(input.workspaceId, relative)
  const filename = safeLocalName(relative)
  const handle = guardSourceHandle({
    guard: input.guard,
    fsGrant: input.fsGrant,
    relativePath: relative,
    sourceRef,
    size,
    modified: info.modified,
  })

  const uploaded = await uploadWithSingleChunkWindow({
    handle,
    filename,
    commandId: input.commandId,
    create: (args) => input.transport.create({
      commandId: args.commandId,
      sourceRef: args.sourceRef,
      // The server treats both as opaque and compares them to each other, so
      // the approved version and the delivered one are the same string by
      // construction here -- not two vocabularies kept in step by hand.
      sourceVersion: approved,
      totalBytes: args.totalBytes,
      filename: args.filename,
    }),
    putChunk: (transferId, offset, body, sha256) =>
      input.transport.putChunk(transferId, offset, body, sha256),
    commit: (args) => input.transport.commit(args),
    chunkSize: input.chunkBytes ?? MATERIALIZE_CHUNK_BYTES,
  })

  const artifactRef = (uploaded.artifact_ref || '').trim()
  if (!artifactRef) {
    throw new TransferError(
      'publish_failed',
      'the server committed the transfer without an artifact reference',
    )
  }
  return {
    transfer_id: uploaded.transferId,
    artifact_ref: artifactRef,
    sha256: uploaded.sha256,
    filename,
    total_bytes: size,
    source_ref: sourceRef,
    source_version: approved,
  }
}
