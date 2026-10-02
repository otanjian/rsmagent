/**
 * The device side of the desktop gateway: connect, answer, execute.
 *
 * Change ``fix-desktop-local-context-and-tool-calls`` (task 2.4). Until now
 * this side existed as helper functions only (URL derivation, backoff, frame
 * bound) and no process ever held a socket, so a command the server queued was
 * never run: the tool waited out its whole deadline against a queue nobody was
 * draining. This module is the missing consumer.
 *
 * What it is responsible for, and what it is not:
 *
 * * it connects only to the configured origin's ``/api/desktop/connect``,
 *   derived by :func:`deviceConnectUrl` -- never a URL from a page;
 * * it authenticates with the native bearer, sent as a header (the gateway
 *   refuses cookie-only and query-token handshakes outright);
 * * it hello's, heartbeats, reconnects with jitter, and answers ``command``
 *   frames by running them against the local root it was given;
 * * it does *not* decide authorization: the workspace it may read is the one
 *   the server named and the main process confirmed, and the helper re-checks
 *   the root descriptor on every operation.
 *
 * Injected socket + injected command runner, so the protocol behaviour (hello
 * before commands, bounded execution, reconnect, no resend after close) is
 * unit-tested without a live gateway.
 */

import {
  deviceConnectUrl,
  nativeConnectHeaders,
  nextReconnectDelayMs,
} from './device-connection'
import { resultFrame, type DeviceCommand, type DeviceResult } from './device-ops'
import { PROJECT_EXECUTION_PROTOCOL } from '../project-execution/contract'
import { openWebSocket, type OpenSocketOptions, type WebSocketLike } from './ws-client'

/** Heartbeat cadence (contracts ``limits.heartbeat_seconds``). */
export const HEARTBEAT_SECONDS = 20

/** How long silence may last before the connection is treated as lost. */
export const IDLE_TIMEOUT_SECONDS = 90

/**
 * The reason to record for a failed attempt: the symbolic ``code`` when the
 * failure carries one (the gateway's own vocabulary, e.g. ``handshake_failed``;
 * or a system error's ``ECONNREFUSED``), otherwise the message.
 */
function reasonOf(err: unknown, fallback: string): string {
  const code = (err as { code?: unknown } | null)?.code
  if (typeof code === 'string' && code) return code
  if (err instanceof Error && err.message) return err.message
  return fallback
}

export interface DeviceCapabilities {
  /** Whether this build can execute file commands at all. */
  files: boolean
}

/**
 * Where the connection is.
 *
 * ``connecting`` means an attempt is live (a socket is being opened or is
 * waiting for its hello); ``reconnecting`` means we are *between* attempts,
 * waiting out the backoff. A retry that has fired is ``connecting`` again --
 * the distinction tells an operator whether anything is happening right now.
 */
export type DeviceState = 'idle' | 'connecting' | 'ready' | 'reconnecting' | 'stopped'

export interface DeviceClientOptions {
  /** The configured origin. The gateway URL is derived from it, never passed in. */
  origin: string
  /** Native bearer, read per attempt so a reconnect uses the current session. */
  token: () => Promise<string | null>
  /** This install's *server* device id, read per attempt. */
  deviceId: () => Promise<string | null>
  /** Permit ``ws://`` for the registered loopback backend only. */
  allowInsecureLoopback?: boolean
  /** Trusted bundled-backend discovery only; never supplied by the Web page. */
  localGatewayPort?: () => Promise<number>
  /** Run one command against the local root. Injected for tests. */
  runCommand: (command: DeviceCommand) => Promise<DeviceResult>
  /**
   * Run one v2 ``execute_tool`` frame and return the ``execution_result`` frame
   * (task 7.1 - 7.7). Optional: a device without project execution simply does
   * not offer the capability, and the server's negotiation decides that -- this
   * client never fabricates an answer for a frame it cannot run.
   */
  runExecution?: (frame: Record<string, unknown>) => Promise<Record<string, unknown>>
  /**
   * Answer a v2 ``execution_status`` query from the durable journal (task 7.2).
   *
   * Optional for the same reason: without a journal there is no honest answer,
   * and "queued" is the only safe default -- the server re-drives a queued
   * command and the journal refuses a double effect on the device.
   */
  runExecutionStatus?: (frame: Record<string, unknown>) => Record<string, unknown>
  capabilities?: DeviceCapabilities
  heartbeatSeconds?: number
  idleTimeoutSeconds?: number
  /** Socket factory; defaults to the verified-TLS client. */
  openSocket?: (url: string, options: OpenSocketOptions) => WebSocketLike
  /** Deterministic jitter source for the reconnect schedule. */
  random?: () => number
  onState?: (state: DeviceState, detail?: string) => void
}

/**
 * One device connection, with its reconnect loop.
 *
 * ``start``/``stop`` are idempotent: the container attach/detach path calls
 * them on every transition, and a second start must not open a second socket
 * (two live leases would fence each other and the device would appear flaky).
 */
export class DeviceClient {
  private socket: WebSocketLike | null = null
  private running = false
  private attempt = 0
  private epoch = ''
  private heartbeat: ReturnType<typeof setInterval> | null = null
  private idleTimer: ReturnType<typeof setTimeout> | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private readonly inFlight = new Set<string>()
  private state: DeviceState = 'idle'

  constructor(private readonly options: DeviceClientOptions) {}

  get currentState(): DeviceState {
    return this.state
  }

  /** The epoch the server handed out for this connection, once hello landed. */
  get currentEpoch(): string {
    return this.epoch
  }

  private setState(state: DeviceState, detail?: string): void {
    this.state = state
    try {
      this.options.onState?.(state, detail)
    } catch {
      /* a listener must not break the connection */
    }
  }

  start(): void {
    if (this.running) return
    this.running = true
    this.attempt = 0
    void this.connect()
  }

  stop(): void {
    this.running = false
    this.clearTimers()
    const socket = this.socket
    this.socket = null
    this.epoch = ''
    if (socket) {
      try {
        socket.close(1000)
      } catch {
        /* already closed */
      }
    }
    this.setState('stopped')
  }

  private clearTimers(): void {
    if (this.heartbeat) clearInterval(this.heartbeat)
    if (this.idleTimer) clearTimeout(this.idleTimer)
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer)
    this.heartbeat = null
    this.idleTimer = null
    this.reconnectTimer = null
  }

  private async connect(): Promise<void> {
    if (!this.running) return
    this.setState('connecting')
    let url: string
    try {
      url = deviceConnectUrl(this.options.origin)
    } catch (err) {
      this.failPermanently(err instanceof Error ? err.message : 'invalid origin')
      return
    }
    try {
      if (this.options.allowInsecureLoopback && this.options.localGatewayPort) {
        const port = await this.options.localGatewayPort()
        if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('invalid local gateway port')
        url = deviceConnectUrl(`http://127.0.0.1:${port}`)
      }
    } catch (err) {
      // Local metadata may briefly disappear while the bundled backend restarts.
      this.scheduleReconnect(reasonOf(err, 'gateway_unavailable'))
      return
    }
    let token: string | null = null
    let deviceId: string | null = null
    try {
      token = await this.options.token()
      deviceId = await this.options.deviceId()
    } catch {
      // The broker is unavailable; retry on the normal schedule rather than
      // pretending the device is gone.
      this.scheduleReconnect('session_unavailable')
      return
    }
    if (!this.running) return
    if (!token) {
      this.failPermanently('no native session for the device connection')
      return
    }
    if (!deviceId) {
      this.failPermanently('this install has no registered device id yet')
      return
    }

    const open = this.options.openSocket ?? ((target, opts) => openWebSocket(target, opts))
    let socket: WebSocketLike
    try {
      socket = open(url, {
        headers: nativeConnectHeaders(token),
        allowInsecureLoopback: !!this.options.allowInsecureLoopback,
      })
    } catch (err) {
      this.scheduleReconnect(reasonOf(err, 'connect_failed'))
      return
    }
    this.socket = socket
    socket.onText((text) => this.onFrame(text))
    socket.onClose((info) => this.onClose(socket, info.code, info.reason))
    try {
      await socket.opened
    } catch (err) {
      // ``onClose`` already scheduled the retry when the transport reported the
      // failure; only a handshake that never produced a close event needs the
      // rejection turned into one. Either way the reason is the symbolic code
      // (``handshake_failed``), not the prose, so the log line is greppable.
      if (this.socket === socket) this.scheduleReconnect(reasonOf(err, 'handshake_failed'))
      return
    }
    if (!this.running || this.socket !== socket) return

    // The hello must land before any command can be answered, because a result
    // is fenced by the epoch the hello returned.
    try {
      socket.send(JSON.stringify({
        v: 1,
        type: 'hello',
        device_id: deviceId,
        protocol_major: 1,
        capabilities: this.options.capabilities ?? { files: true },
      }))
    } catch (err) {
      this.scheduleReconnect(reasonOf(err, 'hello_failed'))
      return
    }
    this.armIdleTimer(socket)
  }

  private failPermanently(reason: string): void {
    this.running = false
    this.clearTimers()
    this.setState('idle', reason)
  }

  private armIdleTimer(socket: WebSocketLike): void {
    if (this.idleTimer) clearTimeout(this.idleTimer)
    const idleMs = (this.options.idleTimeoutSeconds ?? IDLE_TIMEOUT_SECONDS) * 1000
    this.idleTimer = setTimeout(() => {
      // Silence past the contract's idle bound: the socket may be half-open.
      // Closing it is not enough on its own -- a socket we closed ourselves
      // reports no close event, so the retry has to be requested here or the
      // device would sit silently dead until the next container attach.
      if (this.socket === socket) this.closeForRetry('idle_timeout')
    }, idleMs)
  }

  private startHeartbeat(socket: WebSocketLike, epoch: string): void {
    if (this.heartbeat) clearInterval(this.heartbeat)
    const everyMs = (this.options.heartbeatSeconds ?? HEARTBEAT_SECONDS) * 1000
    this.heartbeat = setInterval(() => {
      if (!this.running || this.socket !== socket) return
      try {
        socket.send(JSON.stringify({
          v: 1,
          type: 'heartbeat',
          connection_epoch: epoch,
        }))
      } catch {
        /* the reconnect path owns recovery */
      }
    }, everyMs)
  }

  private onFrame(text: string): void {
    // Frames from a socket we already detached (a close we started, or a
    // reconnect) carry nothing to act on, and re-arming the idle timer for one
    // would leave a timer with no socket to guard.
    const socket = this.socket
    if (!socket) return
    this.armIdleTimer(socket)
    let frame: Record<string, unknown>
    try {
      frame = JSON.parse(text) as Record<string, unknown>
    } catch {
      return
    }
    if (frame.protocol_major === PROJECT_EXECUTION_PROTOCOL.major) {
      // The v2 execution channel. Recognised by its own major version rather
      // than by the frame type, so a v2 frame this build does not know is
      // ignored instead of being mistaken for a v1 ``command``.
      void this.handleExecutionFrame(frame)
      return
    }
    if (frame.v !== 1) return
    const type = frame.type
    if (type === 'hello') {
      this.epoch = String(frame.epoch || '')
      // A hello without an epoch cannot fence a result, so it is not a
      // successful connection: close and retry rather than run commands.
      if (!this.epoch) {
        this.closeForRetry('hello_without_epoch')
        return
      }
      this.attempt = 0
      this.startHeartbeat(socket, this.epoch)
      this.setState('ready')
      return
    }
    if (type === 'heartbeat' || type === 'ack' || type === 'error') {
      // Heartbeat acknowledgements and result acks carry no work. An ``error``
      // frame here is the server's answer to something *we* sent; the durable
      // command row is the truth for what happened to a command, so nothing is
      // inferred from it.
      return
    }
    if (type === 'command') {
      // Handled off the read loop: a slow read must not stop heartbeats.
      void this.handleCommand(frame)
    }
  }

  private async handleCommand(frame: Record<string, unknown>): Promise<void> {
    const requestId = String(frame.request_id || '')
    const socket = this.socket
    if (!requestId || !socket) return
    const command: DeviceCommand = {
      request_id: requestId,
      connection_epoch: String(frame.connection_epoch || ''),
      workspace_id: String(frame.workspace_id || ''),
      op: String(frame.op || ''),
      params: (frame.params as Record<string, unknown>) || {},
      deadline: typeof frame.deadline === 'number' ? frame.deadline : undefined,
    }
    // The epoch has to be the *live* one: a command that arrives under a stale
    // epoch would be fenced by the server anyway, and completing it would look
    // like a device fault.
    if (command.connection_epoch && command.connection_epoch !== this.epoch) return
    if (this.inFlight.has(requestId)) return
    this.inFlight.add(requestId)
    try {
      const result = await this.options.runCommand(command)
      if (!this.running || this.socket !== socket) return
      socket.send(JSON.stringify(resultFrame(command, this.epoch, result)))
    } catch (err) {
      if (!this.running || this.socket !== socket) return
      try {
        socket.send(JSON.stringify(resultFrame(command, this.epoch, {
          state: 'failed',
          errorCode: 'device_error',
          errorMessage: err instanceof Error ? err.message : 'the local read failed',
        })))
      } catch {
        /* the reconnect path owns recovery */
      }
    } finally {
      this.inFlight.delete(requestId)
    }
  }

  /**
   * Answer one v2 execution frame (task 7.1 - 7.7).
   *
   * Same fencing rules as the v1 path, for the same reason: a frame under a
   * stale epoch is dropped rather than answered (the server would treat the
   * answer as a device fault), and a command id already being run here is not
   * started twice. What differs is that the reply *is* the result frame the
   * device executor built -- this method never invents a phase or an effect.
   */
  private async handleExecutionFrame(frame: Record<string, unknown>): Promise<void> {
    const socket = this.socket
    if (!socket) return
    const type = String(frame.type || '')
    const epoch = String(frame.connection_epoch || '')
    if (epoch && epoch !== this.epoch) return

    if (type === 'execution_status') {
      const answer = this.options.runExecutionStatus
      if (!answer) return
      const commandId = String(frame.command_id || '')
      if (!commandId || this.inFlight.has(`status:${commandId}`)) return
      this.inFlight.add(`status:${commandId}`)
      try {
        socket.send(JSON.stringify(answer(frame)))
      } catch {
        /* the reconnect path owns recovery */
      } finally {
        this.inFlight.delete(`status:${commandId}`)
      }
      return
    }

    if (type !== 'execute_tool') return
    const run = this.options.runExecution
    const commandId = String(frame.command_id || '')
    if (!run || !commandId || this.inFlight.has(`exec:${commandId}`)) return
    this.inFlight.add(`exec:${commandId}`)
    try {
      const reply = await run(frame)
      if (!this.running || this.socket !== socket) return
      socket.send(JSON.stringify(reply))
    } catch (err) {
      if (!this.running || this.socket !== socket) return
      try {
        socket.send(JSON.stringify({
          type: 'execution_result',
          protocol_major: PROJECT_EXECUTION_PROTOCOL.major,
          command_id: commandId,
          run_id: String(frame.run_id || ''),
          tool_call_id: String(frame.tool_call_id || ''),
          // A transport failure before the journal was reached: nothing about
          // the project changed here, and claiming otherwise would fabricate an
          // effect the device never had.
          state: 'failed',
          execution_phase: 'failed',
          effects: 'none',
          started_at: 0,
          finished_at: 0,
          error_code: 'device_error',
          error_message: err instanceof Error ? err.message : 'the local execution failed',
        }))
      } catch {
        /* the reconnect path owns recovery */
      }
    } finally {
      this.inFlight.delete(`exec:${commandId}`)
    }
  }

  private closeForRetry(reason: string): void {
    const socket = this.socket
    this.socket = null
    this.epoch = ''
    this.clearTimers()
    if (socket) {
      try {
        socket.close(1002)
      } catch {
        /* already closed */
      }
    }
    this.scheduleReconnect(reason)
  }

  private onClose(socket: WebSocketLike, code: number, reason: string): void {
    // Only the *current* socket may drive the connection: ``stop`` and
    // ``closeForRetry`` detach the socket before closing it, and a close event
    // arriving after that would otherwise overwrite the state (and the reason)
    // they just decided on.
    if (!this.running || this.socket !== socket) return
    this.socket = null
    this.epoch = ''
    this.clearTimers()
    this.scheduleReconnect(`${code}:${reason}`)
  }

  private scheduleReconnect(reason: string): void {
    if (!this.running) return
    if (this.reconnectTimer) return
    const delay = nextReconnectDelayMs(this.attempt, this.options.random ?? Math.random)
    this.attempt += 1
    this.setState('reconnecting', reason)
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      void this.connect()
    }, delay)
  }
}
