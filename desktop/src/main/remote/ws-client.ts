/**
 * The smallest WebSocket client the device gateway needs.
 *
 * Change ``fix-desktop-local-context-and-tool-calls`` (task 2.4). The device
 * connection is the one place the app has to *speak* to the server outside
 * ``fetch``: the gateway pushes commands down a WSS, and no request/response
 * call can stand in for it.
 *
 * Why not a package: the packaged app's Node (Electron 33 / Node 20) exposes
 * ``WebSocket`` only behind ``--experimental-websocket``, and adding a locked
 * dependency is a packaging decision that belongs to the parent change's build
 * work. The surface used here is four things -- upgrade, masked text frame,
 * ping answer, close -- and pinning *that* is smaller than pinning a client.
 *
 * It is deliberately not a general client: text frames only, one endpoint,
 * 64 KiB payloads (contracts ``limits.ws_frame_max_bytes``), no extensions, no
 * redirects, and TLS that is never relaxed. Anything outside that is a protocol
 * error, not a feature to guess at.
 *
 * Pure Node (``net`` / ``tls`` / ``crypto``), with the socket injected, so
 * ``tests/test_desktop_ws_client.cjs`` drives framing and the handshake against
 * a real socket server.
 */

import { createHash, randomBytes } from 'node:crypto'

/** Payload ceiling for one frame, in either direction (contracts §5). */
export const FRAME_MAX_BYTES = 64 * 1024

/** The magic GUID from RFC 6455 §4.2.2. */
const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

export interface SocketLike {
  write(data: Buffer | string): boolean
  end(): void
  destroy(): void
  on(event: 'data', listener: (chunk: Buffer) => void): void
  on(event: 'error', listener: (err: Error) => void): void
  on(event: 'close', listener: () => void): void
}

export interface ConnectOptions {
  host: string
  port: number
  tls: boolean
  serverName?: string
}

/** Open a TCP/TLS socket. Injected so tests can drive a local server. */
export type SocketConnector = (options: ConnectOptions) => SocketLike

export interface WebSocketLike {
  send(text: string): void
  close(code?: number): void
  /** Resolves once the upgrade has been accepted, rejects otherwise. */
  readonly opened: Promise<void>
  onText(listener: (text: string) => void): void
  onClose(listener: (info: { code: number; reason: string }) => void): void
}

export interface OpenSocketOptions {
  headers: Record<string, string>
  /**
   * Permit ``ws://``. Only ever true for the *registered* loopback backend: a
   * remote device connection must be ``wss://`` with a verified certificate.
   */
  allowInsecureLoopback?: boolean
}

export class WebSocketError extends Error {
  constructor(
    readonly code: string,
    message: string,
  ) {
    super(message)
  }
}

/** The ``Sec-WebSocket-Accept`` value for a client key (RFC 6455 §4.2.2). */
export function acceptFor(key: string): string {
  return createHash('sha1').update(key + WS_GUID).digest('base64')
}

/** Build a frame with the FIN bit, an opcode, and a client mask. */
function encodeFrame(opcode: number, payload: Buffer, mask?: Buffer): Buffer {
  const length = payload.byteLength
  if (length > FRAME_MAX_BYTES) {
    throw new WebSocketError('frame_too_large', 'frame exceeds the contract bound')
  }
  const extended = length < 126
    ? Buffer.alloc(0)
    : length < 65536
      ? (() => {
        const b = Buffer.alloc(2)
        b.writeUInt16BE(length)
        return b
      })()
      : (() => {
        const b = Buffer.alloc(8)
        b.writeBigUInt64BE(BigInt(length))
        return b
      })()
  const lengthByte = length < 126 ? length : length < 65536 ? 126 : 127
  const header = Buffer.concat([Buffer.from([0x80 | opcode, 0x80 | lengthByte]), extended])
  const masked = Buffer.alloc(length)
  const maskingKey = mask ?? randomBytes(4)
  for (let i = 0; i < length; i += 1) masked[i] = payload[i] ^ maskingKey[i % 4]
  return Buffer.concat([header, maskingKey, masked])
}

/** A masked text frame: what every client-to-gateway message is. */
export function encodeTextFrame(text: string, mask?: Buffer): Buffer {
  return encodeFrame(0x1, Buffer.from(text, 'utf8'), mask)
}

/** A pong echoing the ping payload verbatim (RFC 6455 §5.5.3). */
export function encodePongFrame(payload: Buffer): Buffer {
  return encodeFrame(0xa, payload)
}

/** The closing handshake frame. */
export function encodeCloseFrame(code = 1000): Buffer {
  const payload = Buffer.alloc(2)
  payload.writeUInt16BE(code, 0)
  return encodeFrame(0x8, payload)
}

export interface DecodedFrame {
  opcode: number
  payload: Buffer
}

/**
 * Decode one frame from a buffer, or ``null`` when more bytes are needed.
 *
 * A server frame that carries the mask bit is a protocol error (RFC 6455 §5.1),
 * and so is a fragmented message: the gateway sends one text frame per message,
 * so a continuation frame means the stream is not the protocol we agreed on.
 */
export function decodeFrame(buffer: Buffer): { frame: DecodedFrame; rest: Buffer } | null {
  if (buffer.length < 2) return null
  const first = buffer[0]
  const second = buffer[1]
  if ((first & 0x80) === 0) {
    throw new WebSocketError('invalid_frame', 'fragmented frames are not expected')
  }
  if ((second & 0x80) !== 0) {
    throw new WebSocketError('invalid_frame', 'server frames must not be masked')
  }
  let length = second & 0x7f
  let offset = 2
  if (length === 126) {
    if (buffer.length < 4) return null
    length = buffer.readUInt16BE(2)
    offset = 4
  } else if (length === 127) {
    if (buffer.length < 10) return null
    const big = buffer.readBigUInt64BE(2)
    if (big > BigInt(FRAME_MAX_BYTES)) {
      throw new WebSocketError('frame_too_large', 'the server sent an oversized frame')
    }
    length = Number(big)
    offset = 10
  }
  if (length > FRAME_MAX_BYTES) {
    throw new WebSocketError('frame_too_large', 'the server sent an oversized frame')
  }
  if (buffer.length < offset + length) return null
  return {
    frame: { opcode: first & 0x0f, payload: Buffer.from(buffer.subarray(offset, offset + length)) },
    rest: buffer.subarray(offset + length),
  }
}

/** True for the loopback hosts the registered local backend may use. */
export function isLoopbackHost(host: string): boolean {
  const name = (host || '').toLowerCase()
  return name === '127.0.0.1' || name === 'localhost' || name === '::1' || name === '[::1]'
}

/** Parse a gateway URL into socket options, refusing anything weaker. */
export function socketOptionsFor(url: string, allowInsecureLoopback: boolean): ConnectOptions {
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    throw new WebSocketError('invalid_url', 'the gateway URL could not be parsed')
  }
  const tls = parsed.protocol === 'wss:'
  if (!tls && parsed.protocol !== 'ws:') {
    throw new WebSocketError('invalid_url', 'the gateway must be ws(s)')
  }
  if (!tls && !(allowInsecureLoopback && isLoopbackHost(parsed.hostname))) {
    throw new WebSocketError('downgrade', 'an unencrypted gateway is only allowed on loopback')
  }
  const port = parsed.port ? Number(parsed.port) : (tls ? 443 : 80)
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new WebSocketError('invalid_url', 'the gateway port is not usable')
  }
  return { host: parsed.hostname, port, tls, serverName: parsed.hostname }
}

/** The production socket factory: verified TLS, or plain TCP on loopback. */
export const nodeSocketConnector: SocketConnector = (options) => {
  const net = require('node:net') as typeof import('node:net')
  if (!options.tls) return net.connect({ host: options.host, port: options.port }) as unknown as SocketLike
  const tls = require('node:tls') as typeof import('node:tls')
  return tls.connect({
    host: options.host,
    port: options.port,
    // Never relaxed, never environment-dependent: a device connection that
    // cannot prove the server is the server is refused.
    rejectUnauthorized: true,
    servername: options.serverName,
    ALPNProtocols: ['http/1.1'],
  }) as unknown as SocketLike
}

/**
 * Open one gateway socket: upgrade handshake, then frame plumbing.
 *
 * The result is deliberately tiny -- ``send`` / ``close`` / listeners -- because
 * the protocol above it (hello, heartbeat, command, result) is the part with
 * meaning and it is tested separately.
 */
export function openWebSocket(
  url: string,
  options: OpenSocketOptions,
  connect: SocketConnector = nodeSocketConnector,
): WebSocketLike {
  const authorization = options.headers.Authorization
  const textListeners: ((text: string) => void)[] = []
  const closeListeners: ((info: { code: number; reason: string }) => void)[] = []
  let buffer: Buffer = Buffer.alloc(0)
  let handshakeDone = false
  let closed = false
  let settleOpen: () => void = () => undefined
  let settleFail: (err: Error) => void = () => undefined
  const opened = new Promise<void>((resolve, reject) => {
    settleOpen = resolve
    settleFail = reject
  })
  // An unobserved rejection would crash the process on a refused handshake.
  opened.catch(() => undefined)

  let socket: SocketLike | null = null
  const finish = (code: number, reason: string): void => {
    if (closed) return
    closed = true
    for (const listener of closeListeners) listener({ code, reason })
  }
  const fail = (err: WebSocketError): void => {
    if (!handshakeDone) settleFail(err)
    try {
      socket?.destroy()
    } catch {
      /* already gone */
    }
    finish(1006, err.code)
  }

  if (!authorization) {
    settleFail(new WebSocketError('invalid_request', 'the gateway handshake needs a bearer'))
    return {
      opened,
      send: () => undefined,
      close: () => undefined,
      onText: () => undefined,
      onClose: () => undefined,
    }
  }

  let target: ConnectOptions
  try {
    target = socketOptionsFor(url, !!options.allowInsecureLoopback)
  } catch (err) {
    settleFail(err instanceof WebSocketError ? err : new WebSocketError('invalid_url', String(err)))
    return {
      opened,
      send: () => undefined,
      close: () => undefined,
      onText: () => undefined,
      onClose: () => undefined,
    }
  }

  socket = connect(target)
  const key = randomBytes(16).toString('base64')
  const expected = acceptFor(key)
  const parsed = new URL(url)

  socket.on('error', (err: Error) => fail(new WebSocketError('network_error', err.message)))

  socket.on('data', (chunk: Buffer) => {
    buffer = Buffer.concat([buffer, chunk])
    if (!handshakeDone) {
      const end = buffer.indexOf('\r\n\r\n')
      if (end < 0) return
      const head = buffer.subarray(0, end).toString('latin1')
      buffer = buffer.subarray(end + 4)
      const lines = head.split('\r\n')
      const status = /^HTTP\/1\.1\s+(\d{3})/.exec(lines[0] || '')
      if (!status || status[1] !== '101') {
        fail(new WebSocketError('handshake_failed', 'the gateway refused the upgrade'))
        return
      }
      const accept = lines
        .map((line) => /^sec-websocket-accept:\s*(.+)$/i.exec(line))
        .find((match): match is RegExpExecArray => !!match)
      if (!accept || accept[1].trim() !== expected) {
        fail(new WebSocketError('handshake_failed', 'the gateway answered with the wrong accept key'))
        return
      }
      handshakeDone = true
      settleOpen()
    }
    try {
      let decoded = decodeFrame(buffer)
      while (decoded) {
        buffer = decoded.rest
        const { opcode, payload } = decoded.frame
        if (opcode === 0x1) {
          for (const listener of textListeners) listener(payload.toString('utf8'))
        } else if (opcode === 0x9) {
          socket?.write(encodePongFrame(payload))
        } else if (opcode === 0x8) {
          const code = payload.length >= 2 ? payload.readUInt16BE(0) : 1005
          closed = true
          try {
            socket?.end()
          } catch {
            /* the peer already closed */
          }
          for (const listener of closeListeners) listener({ code, reason: 'closed' })
          return
        }
        decoded = decodeFrame(buffer)
      }
    } catch (err) {
      fail(err instanceof WebSocketError ? err : new WebSocketError('invalid_frame', String(err)))
    }
  })

  socket.on('close', () => finish(1006, 'transport_closed'))

  const request = [
    `GET ${parsed.pathname || '/api/desktop/connect'} HTTP/1.1`,
    `Host: ${parsed.host}`,
    'Upgrade: websocket',
    'Connection: Upgrade',
    `Sec-WebSocket-Key: ${key}`,
    'Sec-WebSocket-Version: 13',
    `Authorization: ${authorization}`,
    ...Object.entries(options.headers)
      .filter(([name]) => name.toLowerCase() !== 'authorization')
      .map(([name, value]) => `${name}: ${value}`),
    '',
    '',
  ].join('\r\n')
  socket.write(request)

  return {
    opened,
    send: (text: string) => {
      if (closed) throw new WebSocketError('closed', 'the gateway connection is closed')
      socket?.write(encodeTextFrame(text))
    },
    close: (code = 1000) => {
      if (closed) return
      closed = true
      try {
        socket?.write(encodeCloseFrame(code))
        socket?.end()
      } catch {
        /* the write raced the close */
      }
    },
    onText: (listener) => {
      textListeners.push(listener)
    },
    onClose: (listener) => {
      closeListeners.push(listener)
    },
  }
}
