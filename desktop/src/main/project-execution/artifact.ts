/**
 * Local artifact references produced on the device (task 9.1).
 *
 * The server cannot verify a file on someone else's machine: it has no such
 * path, and `contracts/desktop/v2.json` says so explicitly -- "the client's
 * absolute paths never enter the server artifact". So the reference is built
 * *here*, where the bytes are, and it is built only after this process has
 * really looked at the file:
 *
 *   * the path must resolve inside the workspace that ran the command, with the
 *     containment judged on the *real* path so a symlinked component cannot
 *     point the reference at something outside the project;
 *   * the final component must be a regular file, not a symlink and not a
 *     directory -- a link is a reference to a path this run never wrote;
 *   * `source_version` is derived from the file's content (digest) or, for a
 *     file too large to hash without stalling the click that produced it, from
 *     its size and nanosecond mtime. Never from the display name: two different
 *     reports are both called `报告.xlsx`, and treating them as one version is
 *     how a card ends up showing stale bytes.
 *
 * A candidate that fails any of those is dropped. "No card" is an honest
 * outcome; a card the user cannot open, or that opens the wrong file, is not.
 */

import * as crypto from 'node:crypto'
import * as fs from 'node:fs'
import * as path from 'node:path'

import { ARTIFACT_PROTOCOL, type ExecutionArtifact } from './contract'

/** The one source value the contract accepts. */
export const ARTIFACT_SOURCE = 'desktop'

export { ARTIFACT_PROTOCOL }

/**
 * Files at or below this size get a content digest as their version.
 *
 * Above it the version falls back to a `stat:` token, because hashing a
 * multi-gigabyte artifact on the request that just produced it would stall the
 * very click it is meant to enable (contract §6: "大文件摘要计算不得阻塞 UI").
 */
export const SOURCE_VERSION_DIGEST_MAX_BYTES = 8 * 1024 * 1024

const DIGEST_CHUNK_BYTES = 1024 * 1024

/** The contract's six buckets, in the contract's own spelling. */
const KIND_BY_EXT: Record<string, string> = {
    '.txt': 'text', '.md': 'text', '.markdown': 'text', '.html': 'text',
    '.htm': 'text', '.csv': 'text', '.tsv': 'text', '.log': 'text',
    '.json': 'text', '.yaml': 'text', '.yml': 'text', '.xml': 'text',
    '.toml': 'text', '.ini': 'text', '.css': 'text', '.scss': 'text',
    '.py': 'text', '.js': 'text', '.ts': 'text', '.tsx': 'text', '.jsx': 'text',
    '.java': 'text', '.c': 'text', '.cpp': 'text', '.h': 'text', '.go': 'text',
    '.rs': 'text', '.rb': 'text', '.php': 'text', '.sh': 'text', '.sql': 'text',
    '.doc': 'office', '.docx': 'office', '.xls': 'office', '.xlsx': 'office',
    '.ppt': 'office', '.pptx': 'office', '.ods': 'office', '.odt': 'office',
    '.jpg': 'image', '.jpeg': 'image', '.png': 'image', '.gif': 'image',
    '.webp': 'image', '.bmp': 'image', '.svg': 'image', '.ico': 'image',
    '.pdf': 'pdf',
    '.zip': 'archive', '.tar': 'archive', '.gz': 'archive', '.tgz': 'archive',
    '.bz2': 'archive', '.xz': 'archive', '.7z': 'archive', '.rar': 'archive',
    '.zst': 'archive',
}

/** The contract's kind for one path, from its extension. */
export function artifactKind(filePath: string): string {
    const ext = path.extname(filePath).toLowerCase()
    return KIND_BY_EXT[ext] ?? 'binary'
}

/**
 * An opaque version token for a file that exists right now.
 *
 * Opaque on purpose: callers only compare two of these for equality, so the
 * shape may change without a protocol break.
 */
export function sourceVersion(absolutePath: string): string {
    const stat = fs.statSync(absolutePath)
    if (stat.size > SOURCE_VERSION_DIGEST_MAX_BYTES) {
        return `stat:${stat.size}-${Math.trunc(stat.mtimeMs * 1e6)}`
    }
    const hash = crypto.createHash('sha256')
    const fd = fs.openSync(absolutePath, 'r')
    try {
        const buffer = Buffer.allocUnsafe(DIGEST_CHUNK_BYTES)
        for (;;) {
            const read = fs.readSync(fd, buffer, 0, buffer.length, null)
            if (read <= 0) break
            hash.update(buffer.subarray(0, read))
        }
    } finally {
        fs.closeSync(fd)
    }
    return 'sha256:' + hash.digest('hex')
}

/** A stable id for one produced file, with no path baked into the value. */
export function artifactId(runId: string, toolCallId: string,
                           relativePath: string): string {
    return 'art_' + crypto.createHash('sha256')
        .update(`${runId}\u0000${toolCallId}\u0000${relativePath}`, 'utf8')
        .digest('hex')
        .slice(0, 24)
}

export interface ArtifactInput {
    /** The workspace's *real* root, already collapsed by the caller. */
    realRoot: string
    workspaceId: string
    deviceId: string
    runId: string
    toolCallId: string
    /** The path the tool reported, relative or (in-project) absolute. */
    candidate: string
}

/**
 * One verified local artifact reference, or `null` when nothing may be claimed.
 *
 * Every rejection here is a *silent* one at the protocol level and a logged one
 * at the call site: a tool that reports a path outside the project, a symlink, a
 * missing file or a directory is not an error the model should see (it may well
 * have succeeded at something else), but it is also not something a card may be
 * published for.
 */
export function buildArtifact(input: ArtifactInput): ExecutionArtifact | null {
    const root = String(input.realRoot || '')
    const candidate = String(input.candidate || '').trim()
    if (!root || !candidate) return null
    if (candidate.includes('\u0000')) return null

    // The containment comparison is real-to-real. Collapsing the root here as
    // well as the file is what keeps a workspace reached through a symlinked
    // directory (macOS ``/var``, a mapped drive on Windows) from silently
    // disabling every reference this device would otherwise publish.
    const realRoot = canonicalRoot(root)
    if (!realRoot) return null

    // The candidate is looked at on the path the tool *named*, before symlinks
    // are followed: a symlinked final component is a path this run did not
    // write, so it must be refused rather than resolved.
    const named = path.isAbsolute(candidate)
        ? path.normalize(candidate)
        : path.resolve(root, candidate)

    let stat: fs.Stats
    try {
        stat = fs.lstatSync(named)
    } catch {
        return null
    }
    if (!stat.isFile()) return null

    // ...and the *real* path must still be inside the project: a symlinked
    // directory component above the file would otherwise escape it.
    let real: string
    try {
        real = fs.realpathSync(named)
    } catch {
        return null
    }
    if (!isInside(realRoot, real)) return null

    const relativePath = path.relative(realRoot, real).split(path.sep).join('/')
    if (!relativePath || relativePath.startsWith('..') || path.isAbsolute(relativePath)) {
        return null
    }
    try {
        return {
            source: ARTIFACT_SOURCE,
            artifact_id: artifactId(input.runId, input.toolCallId, relativePath),
            device_id: input.deviceId,
            workspace_id: input.workspaceId,
            run_id: input.runId,
            tool_call_id: input.toolCallId,
            relative_path: relativePath,
            file_name: path.basename(real),
            kind: artifactKind(real),
            size: stat.size,
            source_version: sourceVersion(real),
        }
    } catch {
        return null
    }
}

/** The root with symlinks collapsed, or the normalized string if it is gone. */
function canonicalRoot(root: string): string {
    const normalized = path.normalize(root)
    if (!normalized) return ''
    try {
        return fs.realpathSync(normalized)
    } catch {
        return normalized
    }
}

/** Whether `child` is `root` itself or inside it (both normalized). */
function isInside(root: string, child: string): boolean {
    const withSep = root.endsWith(path.sep) ? root : root + path.sep
    return child === root || child.startsWith(withSep)
}
