import fs from 'fs'
import path from 'path'

/**
 * The desktop shell's own diagnostic log: one file, one writer, no console,
 * no buffer.
 *
 * Why this module exists
 * ----------------------
 * The shell used to mirror everything into the *backend's* ``run.log`` and its
 * console wrapper wrote to stdout without any protection. On Windows a GUI app
 * started from a console that then exits gets ``EPIPE`` on every stdout write;
 * the ``uncaughtException`` handler called ``console.error``, which wrote to the
 * same broken pipe, which raised ``EPIPE`` again -- a self-feeding loop that grew
 * ``run.log`` to 1.1 GB in minutes (see
 * ``docs/design/windows-desktop-verification-2026-10-13.md`` section 4.1).
 *
 * Four rules keep that from coming back:
 *
 * * nothing here ever calls ``console.*`` -- this file, its rotation and its
 *   failure reporting use their own channel, so a broken terminal cannot feed
 *   itself;
 * * a terminal write happens once per console call, inside a ``try``/``catch``,
 *   and the terminal is muted on either the synchronous throw or the
 *   asynchronous ``'error'`` event (``process.on('EPIPE')`` does not exist, and
 *   the ``writable`` flag alone is not enough);
 * * the crash handlers write straight to the file behind a reentrancy guard;
 * * records are written **synchronously** to a descriptor this module owns.
 *
 * Why synchronous
 * ---------------
 * A stream would buffer: in a burst (the incident wrote thousands of lines a
 * second) it reports backpressure long before the disk has caught up, and any
 * "skip while behind" policy then drops records -- including the crash record
 * that explains the burst. Writing each line with ``fs.writeSync`` means there
 * is no queue, no high-water mark and no drop policy to get wrong: memory is
 * O(one line) by construction, every record that is accepted is on disk when the
 * call returns, and rotation can rename the live file because the only writer is
 * this process and it is not holding a pending asynchronous write.
 *
 * It deliberately has **no Electron dependency**, so ``node --test`` can drive
 * the compiled module directly (``tests/test_desktop_host_log.cjs``).
 *
 * The shell and the backend never rotate the same file (on Windows two writers
 * cannot rotate one file safely); this is the shell's file only, and Python
 * keeps its own ``run.log`` rotation.
 */

/** The shell's own log file, written beside the backend's ``run.log``. */
export const HOST_LOG_NAME = 'desktop-main.log'

/** At most this many bytes per file before it is rotated. */
export const HOST_LOG_MAX_BYTES = 10 * 1024 * 1024

/** How many rotated generations are kept (``.1``, ``.2``, ...). */
export const HOST_LOG_BACKUPS = 2

/**
 * How many failed opens or writes are tolerated before the log goes quiet. The
 * count is what keeps a wedged disk (or a directory that is not writable) from
 * turning every later line into another failing syscall.
 */
export const HOST_LOG_FAILURE_LIMIT = 3

export type TerminalName = 'stdout' | 'stderr'

/**
 * Render one log argument. Never throws: a circular object, a value with a
 * throwing ``toString`` and an unserializable input all have to reach the file
 * as *something*, because the alternative is a formatter that itself becomes the
 * crash the log was supposed to explain.
 */
export function describeValue(value: unknown): string {
  try {
    if (typeof value === 'string') return value
    if (value instanceof Error) return value.stack || value.message || String(value)
    if (typeof value === 'object' && value !== null) {
      try {
        const json = JSON.stringify(value)
        return json === undefined ? String(value) : json
      } catch {
        // Circular, a BigInt, or a throwing toJSON.
        return '[unserializable object]'
      }
    }
    return String(value)
  } catch {
    return '[unprintable value]'
  }
}

/** One line describing a thrown value, without touching a terminal. */
export function describeError(error: unknown): string {
  return describeValue(error)
}

/** Collapse anything to a single line, for records that must stay countable. */
export function oneLine(text: string): string {
  const first = String(text).split('\n')[0].trim()
  return first || '(no detail)'
}

export interface HostLogOptions {
  /** Directory holding the log; created if missing (and possible). */
  dir: string
  fileName?: string
  maxBytes?: number
  backups?: number
}

export class HostLog {
  /** Absolute path of the live file, so entry points can show it. */
  readonly file: string

  private readonly dir: string
  private readonly maxBytes: number
  private readonly backups: number
  private fd: number | null = null
  private size = 0
  private failures = 0
  private muted: TerminalName[] = []
  private recording = false
  private captured = false

  constructor(options: HostLogOptions) {
    this.dir = options.dir
    this.file = path.join(options.dir, options.fileName || HOST_LOG_NAME)
    this.maxBytes = options.maxBytes && options.maxBytes > 0 ? options.maxBytes : HOST_LOG_MAX_BYTES
    this.backups = options.backups === undefined ? HOST_LOG_BACKUPS : Math.max(0, options.backups)
    this.open()
  }

  /** Terminals whose writes failed; used by tests and by the disable record. */
  get mutedTerminals(): readonly TerminalName[] {
    return this.muted
  }

  /** True once the log has given up on its file. */
  get disabled(): boolean {
    return this.fd === null && this.failures >= HOST_LOG_FAILURE_LIMIT
  }

  /** One record. Never throws, never writes to a terminal. */
  write(level: string, args: unknown[]): void {
    let text: string
    try {
      text = args.map(describeValue).join(' ')
    } catch {
      text = '[unprintable log input]'
    }
    this.line(level, text)
  }

  /** One already-formatted record. Never throws. */
  line(level: string, text: string): void {
    const fd = this.ensureFd()
    if (fd === null) return
    const entry = `[MAIN][${new Date().toISOString()}] [${level}] ${text}\n`
    const bytes = Buffer.byteLength(entry)
    // Rotate *before* writing, so the live file never exceeds the cap by more
    // than the one record that crossed it.
    if (this.size + bytes > this.maxBytes) {
      this.rotate()
    }
    const target = this.fd
    if (target === null) return
    try {
      fs.writeSync(target, entry)
      this.size += bytes
      this.failures = 0
    } catch {
      // ENOSPC, a descriptor closed underneath us, a disk that went away: the
      // record is lost, the app is not. Bounded retries keep a permanently
      // broken device from paying a failing syscall for every later line.
      this.failures += 1
      this.closeFd()
    }
  }

  /**
   * Mirror ``console.*`` into this file while keeping terminal writes guarded.
   * The very first ``EPIPE`` mutes the affected terminal for the rest of the
   * process: the shell keeps working, its diagnostics keep landing in the file,
   * and nothing writes to a pipe that is gone.
   */
  captureConsole(): void {
    if (this.captured) return
    this.captured = true
    this.guardTerminal('stdout')
    this.guardTerminal('stderr')
    const patch = (name: 'log' | 'warn' | 'error') => {
      const original = console[name].bind(console)
      const terminal: TerminalName = name === 'log' ? 'stdout' : 'stderr'
      console[name] = (...args: unknown[]) => {
        // File first: it has to survive a terminal that is already broken.
        this.write(name.toUpperCase(), args)
        if (this.muted.includes(terminal)) return
        const stream = terminal === 'stderr' ? process.stderr : process.stdout
        if (!stream) return
        if (stream.destroyed || (stream as { writable?: boolean }).writable === false) {
          this.mute(terminal, 'stream is not writable')
          return
        }
        try {
          original(...args)
        } catch (error) {
          this.mute(terminal, describeError(error))
        }
      }
    }
    patch('log')
    patch('warn')
    patch('error')
  }

  /**
   * Record main-process crashes straight to the file.
   *
   * The reentrancy guard is the point: the old handler logged *through console*,
   * so an EPIPE exception reported itself forever. A crash record can never
   * re-enter this path, and a non-I/O exception is still recorded -- a broken
   * pipe is not treated as a reason to swallow every other failure.
   */
  captureCrashes(): void {
    const record = (kind: string, detail: unknown) => {
      if (this.recording) return
      this.recording = true
      try {
        this.line('ERROR', `[crash] ${kind}: ${describeError(detail)}`)
      } finally {
        this.recording = false
      }
    }
    process.on('uncaughtException', (error) => record('uncaughtException', error))
    process.on('unhandledRejection', (reason) => record('unhandledRejection', reason))
  }

  /** Release the file. Called on quit; later writes are not reopened. */
  close(): void {
    this.closeFd()
    this.failures = HOST_LOG_FAILURE_LIMIT
  }

  // --- file plumbing --------------------------------------------------------

  private ensureFd(): number | null {
    if (this.fd !== null) return this.fd
    if (this.failures >= HOST_LOG_FAILURE_LIMIT) return null
    this.open()
    return this.fd
  }

  private open(): void {
    try {
      fs.mkdirSync(this.dir, { recursive: true })
      // Opened synchronously and kept for the life of the log: the path is
      // never handed to an asynchronous opener, so a rotation can rename the
      // live file without racing a pending open (on Windows that race fails with
      // ENOENT and silently loses the whole generation).
      this.fd = fs.openSync(this.file, 'a')
      this.size = this.measure()
      // Note: a successful open does *not* clear the failure count. The counter
      // is cleared by a successful write, so a disk that opens fine and then
      // refuses every write still stops after HOST_LOG_FAILURE_LIMIT attempts.
    } catch {
      this.fd = null
      this.failures += 1
    }
  }

  private measure(): number {
    try {
      return fs.statSync(this.file).size
    } catch {
      return 0
    }
  }

  private closeFd(): void {
    const fd = this.fd
    this.fd = null
    if (fd === null) return
    try {
      fs.closeSync(fd)
    } catch {
      // Already gone.
    }
  }

  /**
   * Rotate the live file: ``desktop-main.log`` -> ``.1`` -> ``.2``, with the
   * oldest generation dropped first so the total stays bounded.
   *
   * Renaming a file this process still holds open is safe on every platform we
   * ship: libuv opens with ``FILE_SHARE_DELETE``, so Windows allows the rename
   * (verified on Windows x64). The descriptor is closed first anyway, which
   * makes the ordering obvious rather than platform-dependent.
   */
  private rotate(): void {
    this.closeFd()
    if (this.backups < 1) {
      try {
        fs.unlinkSync(this.file)
      } catch {
        // Not there: nothing to drop.
      }
    } else {
      for (let index = this.backups; index >= 1; index -= 1) {
        const target = `${this.file}.${index}`
        const source = index === 1 ? this.file : `${this.file}.${index - 1}`
        if (index === this.backups) {
          try {
            fs.unlinkSync(target)
          } catch {
            // The oldest generation may not exist yet.
          }
        }
        try {
          fs.renameSync(source, target)
        } catch {
          // A missing generation is not fatal; keep shifting the others.
        }
      }
    }
    this.size = 0
    this.open()
  }

  // --- terminal protection --------------------------------------------------

  private guardTerminal(name: TerminalName): void {
    const stream = name === 'stderr' ? process.stderr : process.stdout
    if (!stream || typeof stream.on !== 'function') return
    try {
      stream.on('error', (error: unknown) => this.mute(name, describeError(error)))
    } catch {
      // A stream we cannot listen to is one we cannot protect; keep going.
    }
  }

  private mute(name: TerminalName, reason: string): void {
    if (this.muted.includes(name)) return
    this.muted.push(name)
    // Recorded on the file channel, which is independent of the terminal that
    // just failed -- this is the "terminal output disabled" evidence. Kept to a
    // single line: an error stack here would turn one record into several.
    this.line('ERROR', `[log] ${name} output disabled: ${oneLine(reason)}`)
  }
}

let active: HostLog | null = null

/** Create (and install) the shell's log. Safe to call once at startup. */
export function initHostLog(dir: string, options?: Omit<HostLogOptions, 'dir'>): HostLog {
  active?.close()
  active = new HostLog({ dir, ...(options || {}) })
  return active
}

/** The installed shell log, or null before startup / in a plain node process. */
export function getHostLog(): HostLog | null {
  return active
}
