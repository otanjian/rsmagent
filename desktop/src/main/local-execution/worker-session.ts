/**
 * The lifecycle of one confined local worker (tasks 4.3 / 4.8).
 *
 * One worker per project per desktop session, started only from a
 * `buildLaunchPlan` result -- there is no path here that spawns an unconfined
 * process, and no path that spawns one for a platform whose boundary has not
 * been probed. That absence is the design: a fallback would be reached sooner or
 * later, and it would be reached exactly when the sandbox failed.
 *
 * Framing and lifecycle are separated so the framing rules (what counts as a
 * valid frame, how a refusal is reported, how a reply is matched to its request)
 * are testable without a process, and so a broken frame cannot be mistaken for a
 * dead worker. The reader keeps reading while a call runs -- that is what lets
 * `cancel` reach a running shell.
 */

import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import type { LaunchPlan } from './sandbox';

/** Must match `agent/desktop_local/protocol.py`. */
export const PROTOCOL_VERSION = 1;
export const MAX_FRAME_BYTES = 1 << 20;
export const DEFAULT_CALL_TIMEOUT_MS = 300_000;

export type WorkerErrorCode =
  | 'invalid_request'
  | 'not_hello'
  | 'unknown_tool'
  | 'unknown_op'
  | 'frame_too_large'
  | 'result_too_large'
  | 'path_outside_project'
  | 'cancelled'
  | 'timeout'
  | 'tool_failed'
  | 'internal'
  | 'worker_exited'
  | 'launch_failed';

export interface WorkerError {
  code: WorkerErrorCode | string;
  message: string;
}

export type DecodeResult =
  | { ok: true; frame: Record<string, unknown> }
  | { ok: false; error: WorkerError };

export function encodeFrame(frame: Record<string, unknown>): string {
  return JSON.stringify(frame) + '\n';
}

/**
 * Whether a line is one usable frame.
 *
 * A frame is a JSON object with a non-empty string `id` and `op`. Anything else
 * is refused with a code rather than partially used, so a truncated or mixed-up
 * line can never be read as a command.
 */
export function decodeFrame(line: string | Buffer): DecodeResult {
  const text = Buffer.isBuffer(line) ? line.toString('utf8') : line;
  if (Buffer.byteLength(text, 'utf8') > MAX_FRAME_BYTES) {
    return { ok: false, error: { code: 'frame_too_large', message: 'frame exceeds the limit' } };
  }
  const trimmed = text.trim();
  if (!trimmed) {
    return { ok: false, error: { code: 'invalid_request', message: 'empty frame' } };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return { ok: false, error: { code: 'invalid_request', message: 'frame is not JSON' } };
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return { ok: false, error: { code: 'invalid_request', message: 'frame must be a JSON object' } };
  }
  const frame = parsed as Record<string, unknown>;
  if (typeof frame.id !== 'string' || frame.id.length === 0) {
    return { ok: false, error: { code: 'invalid_request', message: 'id is required' } };
  }
  if (typeof frame.op !== 'string' || frame.op.length === 0) {
    return { ok: false, error: { code: 'invalid_request', message: 'op is required' } };
  }
  return { ok: true, frame };
}

export interface WorkerReply {
  id: string;
  status: 'success' | 'error';
  /**
   * Present **only** for a protocol-level failure (a bad frame, an unknown
   * tool, a timeout). A tool that ran and reported failure answers with
   * `status: 'error'` too, but carries `tool` + `result` and **no** `error` --
   * see `call` in `agent/desktop_local/worker.py`, which copies the tool's own
   * status into the frame.
   */
  error?: WorkerError;
  tool?: string;
  result?: unknown;
  [key: string]: unknown;
}

/** What a `call` frame actually answered, with the two failures kept apart. */
export type CallOutcome =
  | { kind: 'protocol_error'; code: string; message: string }
  | { kind: 'tool_error'; tool: string; result: unknown }
  | { kind: 'tool_result'; tool: string; result: unknown };

/**
 * Tell "the tool failed" apart from "the worker is broken".
 *
 * They are different events with different responses -- one is the model's
 * problem, the other is the runtime's -- and both arrive as `status: 'error'`.
 * Collapsing them loses the distinction at exactly the moment it matters.
 */
export function classifyReply(reply: WorkerReply): CallOutcome {
  if (reply.error) {
    return { kind: 'protocol_error', code: reply.error.code, message: reply.error.message };
  }
  const tool = typeof reply.tool === 'string' ? reply.tool : '';
  if (reply.status === 'error') {
    return { kind: 'tool_error', tool, result: reply.result };
  }
  return { kind: 'tool_result', tool, result: reply.result };
}

/**
 * Whether a line is one usable *reply*.
 *
 * Deliberately not `decodeFrame`: a request carries `op`, and a reply carries
 * `status` instead (see `encode_reply` in `agent/desktop_local/protocol.py` --
 * there is no `op` on the way back). Validating replies with the request rule
 * silently discarded every answer, which presents as "the worker never
 * replied" -- a hang that looks like a dead process. The reply rule is the one
 * that matches what the worker actually writes.
 */
export function decodeReply(line: string | Buffer): DecodeResult {
  const text = Buffer.isBuffer(line) ? line.toString('utf8') : line;
  if (Buffer.byteLength(text, 'utf8') > MAX_FRAME_BYTES) {
    return { ok: false, error: { code: 'frame_too_large', message: 'frame exceeds the limit' } };
  }
  const trimmed = text.trim();
  if (!trimmed) {
    return { ok: false, error: { code: 'invalid_request', message: 'empty frame' } };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    // A non-JSON line is the worker's *log* output, not a reply; dropping it is
    // correct, so this must not be an error the caller sees.
    return { ok: false, error: { code: 'invalid_request', message: 'frame is not JSON' } };
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return { ok: false, error: { code: 'invalid_request', message: 'frame must be a JSON object' } };
  }
  const frame = parsed as Record<string, unknown>;
  if (typeof frame.id !== 'string' || frame.id.length === 0) {
    return { ok: false, error: { code: 'invalid_request', message: 'id is required' } };
  }
  if (frame.status !== 'success' && frame.status !== 'error') {
    return { ok: false, error: { code: 'invalid_request', message: 'status is required' } };
  }
  return { ok: true, frame };
}

export interface WorkerSessionOptions {
  callTimeoutMs?: number;
  /** Injected in tests; the only place a process is created. */
  spawnFn?: typeof spawn;
}

export interface WorkerSessionEvents {
  onExit?: (code: number | null, signal: NodeJS.Signals | null) => void;
  onStderr?: (chunk: string) => void;
}

/**
 * A running worker, addressed by request id.
 *
 * A call that times out is cancelled *and* the tree is killed: a tool that ran
 * past its budget may be mid-write, and leaving it alive would mean the next
 * call's answer could arrive after a "failed" one.
 */
export class WorkerSession {
  private readonly child: ChildProcessWithoutNullStreams;
  private readonly pending = new Map<
    string,
    { resolve: (reply: WorkerReply) => void; timer: NodeJS.Timeout | null }
  >();
  private buffer = '';
  private exited = false;
  private readonly callTimeoutMs: number;

  constructor(plan: LaunchPlan, events: WorkerSessionEvents = {}, options: WorkerSessionOptions = {}) {
    this.callTimeoutMs = options.callTimeoutMs ?? DEFAULT_CALL_TIMEOUT_MS;
    const spawnFn = options.spawnFn ?? spawn;
    this.child = spawnFn(plan.command, plan.args, {
      env: plan.env,
      cwd: plan.cwd,
      // Own process group, so a script's children die with it (Windows uses
      // taskkill /T; see killWorkerTree).
      detached: process.platform !== 'win32',
      stdio: ['pipe', 'pipe', 'pipe'],
    }) as ChildProcessWithoutNullStreams;

    this.child.stdout.setEncoding('utf8');
    this.child.stderr.setEncoding('utf8');
    // A write to a worker that has just exited raises EPIPE on the stream. That
    // is the *symptom* of an exit we already handle via `child.on('exit')`, so
    // it must not crash the process with an unhandled 'error' event.
    this.child.stdin.on('error', () => {
      /* handled by the exit/error path */
    });
    this.child.stdout.on('data', (chunk: string) => this.onStdout(chunk));
    this.child.stderr.on('data', (chunk: string) => events.onStderr?.(chunk));
    this.child.on('exit', (code, signal) => {
      this.exited = true;
      for (const [, entry] of this.pending) {
        if (entry.timer) clearTimeout(entry.timer);
        entry.resolve({
          id: '',
          status: 'error',
          error: { code: 'worker_exited', message: 'the worker exited before answering' },
        });
      }
      this.pending.clear();
      events.onExit?.(code, signal);
    });
    this.child.on('error', () => {
      this.exited = true;
      for (const [, entry] of this.pending) {
        if (entry.timer) clearTimeout(entry.timer);
        entry.resolve({
          id: '',
          status: 'error',
          error: { code: 'launch_failed', message: 'the worker could not be started' },
        });
      }
      this.pending.clear();
    });
  }

  get pid(): number | undefined {
    return this.child.pid;
  }

  get running(): boolean {
    return !this.exited;
  }

  /** Send a frame and wait for its reply. */
  request(
    op: string,
    fields: Record<string, unknown> = {},
    timeoutMs?: number,
  ): Promise<WorkerReply> {
    const id = randomUUID();
    return this.sendWithId(id, op, fields, timeoutMs);
  }

  sendWithId(
    id: string,
    op: string,
    fields: Record<string, unknown>,
    timeoutMs?: number,
  ): Promise<WorkerReply> {
    if (this.exited) {
      return Promise.resolve({
        id,
        status: 'error',
        error: { code: 'worker_exited', message: 'the worker is not running' },
      });
    }
    return new Promise<WorkerReply>((resolve) => {
      const budget = timeoutMs ?? this.callTimeoutMs;
      const timer = setTimeout(() => {
        this.pending.delete(id);
        // Resolve first so the caller is not kept waiting for cleanup, then ask
        // the worker to take its command trees down and finish anything it is not
        // allowed to signal itself.
        resolve({
          id,
          status: 'error',
          error: { code: 'timeout', message: 'the tool exceeded its time budget' },
        });
        void this.cancelAndReap();
      }, budget);
      this.pending.set(id, { resolve, timer });
      this.writeFrame({ id, op, ...fields });
    });
  }

  /** Write one frame, tolerating a worker that died between check and write. */
  private writeFrame(frame: Record<string, unknown>): void {
    try {
      this.child.stdin.write(encodeFrame(frame));
    } catch {
      /* the exit handler resolves the pending call */
    }
  }

  hello(root: string, temp: string): Promise<WorkerReply> {
    return this.request('hello', { root, temp });
  }

  describe(): Promise<WorkerReply> {
    return this.request('describe');
  }

  call(
    tool: string,
    args: Record<string, unknown>,
    timeoutMs?: number,
  ): Promise<WorkerReply> {
    return this.request('call', { tool, arguments: args }, timeoutMs);
  }

  /** Best-effort cancel: the worker sets its own cancel event. */
  cancel(): Promise<WorkerReply | null> {
    if (this.exited) return Promise.resolve(null);
    try {
      return this.request('cancel');
    } catch {
      return Promise.resolve(null);
    }
  }

  /**
   * Cancel, then stop the tree -- in that order, and waiting for the answer.
   *
   * The order is the point. The worker is sandboxed and may only signal its own
   * live children, so a command that backgrounded something and exited leaves a
   * process the worker *cannot* kill; it names those groups in the cancel reply
   * instead, and `onLine` reaps them. Killing the worker first would destroy that
   * answer, which is how a timed-out command ends up running on the user's
   * machine.
   */
  /**
   * Cancel the current call, then stop the tree -- the public form of the
   * timeout path, for a caller that owns the cancellation (task 7.5).
   *
   * The device's execution endpoint must resolve a cancel only once the tree has
   * really ended, so it awaits this rather than firing a kill and hoping.
   */
  async interruptAndReap(): Promise<void> {
    await this.cancelAndReap();
  }

  private async cancelAndReap(): Promise<void> {
    await this.cancel().catch(() => null);
    killWorkerTree(this.child.pid, process.platform);
  }

  /**
   * Kill process groups the worker was not permitted to signal.
   *
   * They were created by this session's own tool calls, so they are ours by
   * construction; this process is not sandboxed, so it can reach them.
   */
  private reapGroups(groups: unknown): void {
    if (!Array.isArray(groups)) return;
    for (const value of groups) {
      const gid = typeof value === 'number' ? value : Number(value);
      // Never 0 or 1: those name "every process in my group" and "init".
      if (!Number.isInteger(gid) || gid <= 1) continue;
      try {
        process.kill(-gid, 'SIGKILL');
      } catch {
        /* already gone */
      }
    }
  }

  /** Stop the worker and its children. Idempotent. */
  async stop(graceMs = 1000): Promise<void> {
    const pid = this.child.pid;
    if (!this.exited) {
      try {
        // The reply names any group the worker could not signal itself, so it is
        // awaited rather than raced away: the whole point is to receive it.
        const reply = await Promise.race([
          this.request('shutdown'),
          new Promise<null>((resolve) => setTimeout(() => resolve(null), graceMs)),
        ]);
        if (reply) this.reapGroups(reply.unreaped_groups);
      } catch {
        /* falling through to the kill is the point */
      }
      try {
        this.child.stdin.end();
      } catch {
        /* already gone */
      }
    }
    // Deliberately unconditional, and deliberately *after* the graceful
    // shutdown. A worker that exits cleanly still leaves whatever it started:
    // `bash` runs `sh -c 'sleep 137 &'`, the worker answers and exits, and the
    // grandchild is orphaned -- a clean shutdown that leaks processes onto the
    // user's machine. `killWorkerTree` is a no-op when the group is already
    // gone, so sweeping always is both safe and the only correct option.
    killWorkerTree(pid, process.platform);
  }

  private onStdout(chunk: string): void {
    this.buffer += chunk;
    let index = this.buffer.indexOf('\n');
    while (index >= 0) {
      const line = this.buffer.slice(0, index);
      this.buffer = this.buffer.slice(index + 1);
      this.onLine(line);
      index = this.buffer.indexOf('\n');
    }
  }

  private onLine(line: string): void {
    // Replies, not requests: see `decodeReply` for why this is a separate rule.
    const decoded = decodeReply(line);
    if (!decoded.ok) {
      // A frame we cannot parse cannot be matched to a caller. Dropping it is
      // safer than resolving an arbitrary pending call with it.
      return;
    }
    const reply = decoded.frame as WorkerReply;
    // Reaped here rather than by the caller, because the caller may already have
    // given up on this request (the timeout path deletes the pending entry before
    // the answer lands), and an answer nobody is waiting for still names groups
    // that must die.
    this.reapGroups(reply.unreaped_groups);
    const entry = this.pending.get(reply.id);
    if (!entry) return;
    this.pending.delete(reply.id);
    if (entry.timer) clearTimeout(entry.timer);
    entry.resolve(reply);
  }
}

/** Kill the worker and everything it started. */
export function killWorkerTree(
  pid: number | undefined,
  platform: NodeJS.Platform,
  spawnSyncFn: typeof import('node:child_process').spawnSync = require('node:child_process')
    .spawnSync,
): void {
  if (!pid) return;
  try {
    if (platform === 'win32') {
      // A Job Object would be the real answer; without one, /T is what reaches
      // the grandchildren a shell script started.
      spawnSyncFn('taskkill', ['/pid', String(pid), '/T', '/F'], { stdio: 'ignore' });
      return;
    }
    // Negative pid = the process group, because the child was spawned detached.
    process.kill(-pid, 'SIGTERM');
    const deadline = Date.now() + 1500;
    while (Date.now() < deadline) {
      try {
        process.kill(-pid, 0);
      } catch {
        return;
      }
      // Busy-wait briefly; this runs on a shutdown/timeout path, not per call.
      const until = Date.now() + 25;
      while (Date.now() < until) {
        /* spin */
      }
    }
    process.kill(-pid, 'SIGKILL');
  } catch {
    try {
      process.kill(pid, 'SIGKILL');
    } catch {
      /* already gone */
    }
  }
}
