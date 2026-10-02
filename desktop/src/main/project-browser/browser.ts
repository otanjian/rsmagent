/**
 * The local project, as a *source* the file panel can read and edit (task 9.2).
 *
 * The console's file panel has always been server-backed: `/api/workspace/*`
 * resolves a path against the backend's own working directory. That is the right
 * answer when the files are the backend's, and the wrong one when the session is
 * bound to a project on the user's own machine -- the backend has no such path,
 * and asking it to `stat` one either 404s or, on a single-machine deployment,
 * quietly answers about a *different* file with the same name.
 *
 * So there are two sources, and this module is the local one. It is deliberately
 * not a second filesystem implementation:
 *
 * * reads go through {@link DeviceCommandRunner}, i.e. the same
 *   ``list`` / ``stat`` / ``search`` / ``read_text`` handler the v1 device
 *   channel uses, with that handler's own clamps and its read-only helper
 *   (``fs-guard`` refuses ``write``/``unlink``/``rename`` as a pinned test);
 * * edits go through the *project-execution* channel as a ``write`` frame, so a
 *   panel save is authorized, serialised per project and journaled exactly like
 *   the model's own ``write`` call. A panel edit is not a special case of
 *   writing; it is the same operation with a human as the caller.
 *
 * Two honesty rules shape every reply:
 *
 * * the page never learns a directory. Results carry the workspace id, a label
 *   the user already chose, the grant version and *relative* paths;
 * * an unanswered question is refused by name, never answered with a plausible
 *   default. A revoked binding is ``stale_context`` (not "empty directory"), an
 *   offline device is ``device_offline`` (not a silent local write), and a
 *   build without the execution channel refuses the edit rather than falling
 *   back to the server's path rules.
 *
 * Pure Node (no Electron import), so the whole surface is driven in
 * ``node --test`` against real files.
 */

import { randomBytes } from 'node:crypto'

import type { DeviceCommand, DeviceResult } from '../remote/device-ops'
import { PROJECT_EXECUTION_PROTOCOL, paramsDigest } from '../project-execution/contract'

/** The v2 tool a panel save travels as. Always ``write``: it replaces the file. */
export const EDIT_TOOL = 'write'

/** How long a locally-issued edit frame stays valid. Short, like a start permit. */
export const EDIT_FRAME_TTL_SECONDS = 10

/** One page of a tree listing may not exceed the device command bound. */
export const TREE_PAGE_MAX = 200

/** One preview page may not exceed the device command bound. */
export const PREVIEW_BYTES_MAX = 16384

export interface ProjectBinding {
  /** The server's workspace id this binding answers for. */
  workspaceId: string
  /** The opaque local grant behind it; re-checked on every call, never sent. */
  grantId: string
  /** What the user chose, for display only. */
  label: string
  /** Bumped by a re-pick; a frame naming an older one is a miss. */
  grantVersion: number
  /** The server binding this workspace belongs to, as the frame must name it. */
  bindingId: string
  deviceId: string
  selectionGeneration: number
  /** The live connection epoch, or ``''`` while the device is not attached. */
  connectionEpoch: string
  /** True only for a ``project-execution`` grant; a read grant is not widened. */
  executable: boolean
  /** True while the connection is usable; ``false`` refuses locally-issued work. */
  connected: boolean
}

export interface DeviceCommandRunner {
  runCommand: (command: DeviceCommand) => Promise<DeviceResult>
}

export interface ProjectBrowserOptions {
  /** Reads, through the device command handler (never a server path lookup). */
  commands: DeviceCommandRunner
  /** What this device knows about a bound workspace *right now*, or ``null``. */
  binding: (workspaceId: string) => ProjectBinding | null
  /**
   * Run one locally-issued execution frame. Absent in a build without project
   * execution, in which case an edit is refused by name instead of being
   * routed somewhere it does not belong.
   */
  executeTool?: (frame: Record<string, unknown>) => Promise<Record<string, unknown>>
  now?: () => number
  /** Unique id source for the edit frame's identifiers. */
  newId?: () => string
}

export interface ProjectBrowserRefusal {
  ok: false
  code: string
  message: string
}

export interface ProjectSourceDescription {
  ok: true
  source: 'desktop'
  workspace_id: string
  label: string
  grant_version: number
  /** Reading is always available for a live binding; editing needs more. */
  readable: true
  editable: boolean
  /** Why editing is unavailable, when it is. */
  edit_refusal: string
}

/**
 * The local half of the file panel's source.
 *
 * One instance per process; every method is stateless apart from the injected
 * lookups, so a binding that is revoked between two calls is refused by the
 * second one rather than served from a path cached by the first.
 */
export class ProjectBrowser {
  constructor(private readonly options: ProjectBrowserOptions) {}

  /** What the panel should say about a workspace before it lists anything. */
  describe(workspaceId: string): ProjectSourceDescription | ProjectBrowserRefusal {
    const binding = this.options.binding(String(workspaceId || ''))
    if (!binding) return staleContext()
    const editRefusal = this.editRefusal(binding)
    return {
      ok: true,
      source: 'desktop',
      workspace_id: binding.workspaceId,
      label: binding.label,
      grant_version: binding.grantVersion,
      readable: true,
      editable: editRefusal === '',
      edit_refusal: editRefusal,
    }
  }

  /** One page of a directory listing, relative to the project root. */
  async tree(params: Record<string, unknown>): Promise<unknown> {
    const command = this.command(params, 'list', {
      relative_path: asPath(params.path),
      cursor: asString(params.cursor) || null,
      limit: asInt(params.limit),
    })
    if ('ok' in command) return command
    const result = await this.options.commands.runCommand(command)
    if (result.state !== 'succeeded') return failed(result)
    const page = result.result || {}
    const base = asString(page.path)
    return {
      ok: true,
      source: 'desktop',
      type: 'tree',
      path: base,
      // Each entry carries its own project-relative path. The helper reports a
      // name (it is the one that opened the root and can resolve safely), so the
      // join happens once here rather than at every caller -- and a caller never
      // has to guess how a relative path is spelled.
      entries: (Array.isArray(page.entries) ? page.entries : []).map((entry) => {
        const row = (entry || {}) as Record<string, unknown>
        return { ...row, path: joinRelative(base, asString(row.name)) }
      }),
      // A partial listing must never read as a complete directory, so the
      // helper's own paging answers travel with it.
      truncated: !!page.truncated,
      next_cursor: page.next_cursor ?? null,
      skipped: Number(page.skipped || 0),
      grant_version: Number(page.grant_version || 0),
    }
  }

  /** Name or text search inside the project. */
  async search(params: Record<string, unknown>): Promise<unknown> {
    const query = asString(params.query).trim()
    if (!query) return refuse('invalid_request', 'a search needs a query')
    const mode = asString(params.mode) || 'name'
    if (mode !== 'name' && mode !== 'text') {
      return refuse('invalid_request', 'the search mode must be name or text')
    }
    const command = this.command(params, 'search', {
      relative_path: asPath(params.path),
      mode,
      query,
    })
    if ('ok' in command) return command
    const result = await this.options.commands.runCommand(command)
    if (result.state !== 'succeeded') return failed(result)
    const found = result.result || {}
    return {
      ok: true,
      source: 'desktop',
      type: 'search',
      mode,
      query,
      path: asString(found.path),
      hits: Array.isArray(found.hits) ? found.hits : [],
      scanned: Number(found.scanned || 0),
      skipped: Number(found.skipped || 0),
      truncated: !!found.truncated,
      grant_version: Number(found.grant_version || 0),
    }
  }

  /** Metadata for one project-relative path. */
  async resolve(params: Record<string, unknown>): Promise<unknown> {
    const path = asPath(params.path)
    if (!path) return refuse('invalid_request', 'resolve needs a path')
    const command = this.command(params, 'stat', { relative_path: path })
    if ('ok' in command) return command
    const result = await this.options.commands.runCommand(command)
    if (result.state !== 'succeeded') return failed(result)
    const info = result.result || {}
    return {
      ok: true,
      source: 'desktop',
      type: 'entry',
      path: asString(info.path),
      kind: asString(info.kind),
      size: info.size === null || info.size === undefined ? null : Number(info.size),
      modified: Number(info.modified || 0),
      grant_version: Number(info.grant_version || 0),
    }
  }

  /**
   * One *page* of a file's text.
   *
   * Paged on purpose (contract §6, "单次至多 16 MiB，文本分页更小"): a preview
   * must not pull a multi-gigabyte log through the bridge, and the page size is
   * clamped here rather than trusted from the page. ``total`` travels with the
   * page so the editor can tell "this is the whole file" from "there is more".
   */
  async read(params: Record<string, unknown>): Promise<unknown> {
    const path = asPath(params.path)
    if (!path) return refuse('invalid_request', 'a preview needs a path')
    const command = this.command(params, 'read_text', {
      relative_path: path,
      offset: Math.max(0, asInt(params.offset) ?? 0),
      limit: Math.min(Math.max(1, asInt(params.bytes) ?? PREVIEW_BYTES_MAX),
                      PREVIEW_BYTES_MAX),
      encoding: asString(params.encoding) || 'utf-8',
    })
    if ('ok' in command) return command
    const result = await this.options.commands.runCommand(command)
    if (result.state !== 'succeeded') return failed(result)
    const page = result.result || {}
    // The helper's read answers bytes, not a timestamp, and the panel's editor
    // needs one to tell "the file changed under me" from "it did not". Rather
    // than widening the read op, one stat follows a successful read -- the same
    // freshness the editor's baseline depends on, and still entirely local.
    const meta = await this.stat(params, path)
    return {
      ok: true,
      source: 'desktop',
      type: 'page',
      path: asString(page.path),
      text: asString(page.text),
      encoding: asString(page.encoding),
      offset: Number(page.offset || 0),
      bytes: Number(page.bytes || 0),
      total: Number(page.total || 0),
      truncated: !!page.truncated,
      grant_version: Number(page.grant_version || 0),
      mtime: meta,
    }
  }

  /** The file's modification time through the same handler, or ``0``. */
  private async stat(params: Record<string, unknown>, path: string): Promise<number> {
    const command = this.command(params, 'stat', { relative_path: path })
    if ('ok' in command) return 0
    const result = await this.options.commands.runCommand(command)
    if (result.state !== 'succeeded') return 0
    return Number((result.result || {}).modified || 0)
  }

  /**
   * Write one file the user edited in the panel.
   *
   * Travels as a v2 ``write`` frame, not as a panel-specific file write: the
   * frame is validated by the same rules a server-dispatched one is, serialised
   * against the same per-project key, and journaled under an id that cannot be
   * replayed. It is issued *here* rather than by the server because the caller
   * is the user sitting in front of this machine, and the authorization it needs
   * is the local grant, which only this process can read.
   *
   * Everything the frame must name (`grant_version`, `binding_id`,
   * `selection_generation`, `connection_epoch`) comes from the live binding, so
   * a re-picked directory or a re-attached connection makes an older edit a miss
   * instead of a write into a directory the user no longer authorized.
   */
  async write(params: Record<string, unknown>): Promise<unknown> {
    const workspaceId = asString(params.workspace_id)
    const binding = this.options.binding(workspaceId)
    if (!binding) return staleContext()
    const refusal = this.editRefusal(binding)
    if (refusal) return refuse(refusal, this.editRefusalMessage(refusal))

    const path = asPath(params.path)
    if (!path) return refuse('invalid_request', 'an edit needs a project-relative path')
    const content = asString(params.content)

    const execute = this.options.executeTool
    if (!execute) {
      return refuse('feature_unavailable', 'this build does not offer project execution')
    }

    const frame = this.editFrame(binding, path, content)
    let reply: Record<string, unknown>
    try {
      reply = await execute(frame)
    } catch (err) {
      return failed({
        state: 'failed',
        errorCode: (err as { code?: string })?.code || 'device_error',
        errorMessage: (err as Error)?.message || 'the local write did not complete',
      })
    }
    const phase = String(reply.execution_phase || '')
    if (reply.state !== 'succeeded' || phase !== 'succeeded') {
      return failed({
        state: 'failed',
        errorCode: String(reply.error_code || '') || 'device_error',
        errorMessage: String(reply.error_message || '') || 'the local write did not complete',
      })
    }
    return {
      ok: true,
      source: 'desktop',
      type: 'saved',
      path,
      grant_version: binding.grantVersion,
      command_id: String(frame.command_id),
      // The frame's own verdict, so the panel can say "written, effects
      // completed" rather than inferring success from the absence of an error.
      execution_phase: phase,
      effects: String(reply.effects || ''),
    }
  }

  // -- internals -----------------------------------------------------------

  /** The refusal an edit would hit right now, or ``''`` when it would proceed. */
  private editRefusal(binding: ProjectBinding): string {
    if (!binding.executable) return 'source_read_only'
    if (!binding.connected) return 'device_offline'
    if (!binding.connectionEpoch) return 'device_offline'
    if (!this.options.executeTool) return 'feature_unavailable'
    return ''
  }

  private editRefusalMessage(code: string): string {
    switch (code) {
      case 'source_read_only':
        // The directory was opened for reference. Editing it would be a
        // capability the user never granted, so the panel offers none.
        return 'this project was opened for reference; open it with execution to edit files'
      case 'device_offline':
        return 'the device is not connected; edits are journaled and need a live grant'
      default:
        return 'this build does not offer project execution'
    }
  }

  /** Resolve the workspace and shape one read command, or a refusal. */
  private command(
    params: Record<string, unknown>,
    op: string,
    extra: Record<string, unknown>,
  ): DeviceCommand | ProjectBrowserRefusal {
    const workspaceId = asString(params.workspace_id)
    const binding = this.options.binding(workspaceId)
    if (!binding) return staleContext()
    return {
      // A panel request is not a server command and has no request id of its
      // own; a fresh one keeps the device handler's own bookkeeping honest.
      request_id: this.id(),
      connection_epoch: binding.connectionEpoch,
      workspace_id: workspaceId,
      op,
      params: extra,
    }
  }

  private editFrame(
    binding: ProjectBinding,
    path: string,
    content: string,
  ): Record<string, unknown> {
    const now = this.options.now?.() ?? Date.now()
    const id = this.id()
    const frame: Record<string, unknown> = {
      type: 'execute_tool',
      protocol_major: PROJECT_EXECUTION_PROTOCOL.major,
      command_id: `edit_${id}`,
      // One run id per edit: the panel does not model conversation runs, and
      // borrowing a real one would put a user's save inside a model's turn.
      run_id: `user_edit_${id}`,
      tool_call_id: `edit_call_${id}`,
      binding_id: binding.bindingId,
      workspace_id: binding.workspaceId,
      device_id: binding.deviceId,
      grant_version: binding.grantVersion,
      selection_generation: binding.selectionGeneration,
      connection_epoch: binding.connectionEpoch,
      tool: EDIT_TOOL,
      tool_schema_version: 1,
      arguments: { path, content },
      expires_at: new Date((now + EDIT_FRAME_TTL_SECONDS * 1000)).toISOString(),
    }
    // The digest must be the contract's own: a receiver recomputes it from the
    // canonical envelope, so any other recipe would be refused (or worse, would
    // journal one edit under two identities).
    frame.params_digest = paramsDigest(frame)
    return frame
  }

  private id(): string {
    if (this.options.newId) return this.options.newId()
    return randomBytes(9).toString('base64url')
  }
}

function staleContext(): ProjectBrowserRefusal {
  return refuse('stale_context', 'this session has no live local project on this device')
}

function refuse(code: string, message: string): ProjectBrowserRefusal {
  return { ok: false, code, message }
}

/** A device failure becomes the same ``{ok:false}`` a local refusal uses. */
function failed(result: DeviceResult): ProjectBrowserRefusal {
  return refuse(result.errorCode || 'device_error', result.errorMessage || 'the local read failed')
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/**
 * Join a listing path with one entry name.
 *
 * ``name`` comes from the helper's own directory scan, so it is a single
 * component with no separator; anything else (a name that somehow contains a
 * separator or a traversal) is *not* joined -- an entry whose path cannot be
 * stated honestly gets its bare name, and the containment check on the
 * follow-up read is what refuses it.
 */
function joinRelative(base: string, name: string): string {
  if (!name || /[\\/]/.test(name) || name === '.' || name === '..') return name
  const trimmed = base.replace(/\/+$/, '')
  return trimmed ? `${trimmed}/${name}` : name
}

function asInt(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return Math.trunc(value)
  if (typeof value === 'string' && /^-?\d+$/.test(value.trim())) return Number(value.trim())
  return null
}

/**
 * A project-relative path, or ``''``.
 *
 * The containment itself is the helper's (it re-opens the root and resolves each
 * component against it); what this rejects is the *shape* a project-relative
 * path must not have, so an absolute path or a traversal never reaches a file
 * API at all.
 */
function asPath(value: unknown): string {
  const raw = asString(value).trim()
  if (!raw || raw.includes('\u0000')) return ''
  if (raw.startsWith('/') || /^[a-zA-Z]:/.test(raw)) return ''
  const parts = raw.split(/[\\/]+/)
  if (parts.some((part) => part === '..')) return ''
  return raw.replace(/^\.\//, '')
}
