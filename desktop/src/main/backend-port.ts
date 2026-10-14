/**
 * Ports, and who is allowed to hold them.
 *
 * Split out of ``python-manager.ts`` so the rules can be tested against real
 * sockets (the manager itself imports electron). Both rules below are
 * Windows-specific and were learned the hard way -- change
 * ``harden-windows-desktop-reliability``, task 5.1, which went looking for the
 * source of a second backend spawn and found the port guard had been lying:
 *
 *  - **A bind is not proof that a port is free.** A plain winsock bind takes
 *    SO_REUSEADDR, so binding ``127.0.0.1:p`` succeeds *while another process
 *    holds ``0.0.0.0:p``* -- which is how the backend serves. The old probe
 *    believed the bind, ``pickPort()`` handed the backend a port it could not
 *    use, and the backend died with ``WinError 10048``. Reproduced by
 *    ``scripts/repro/probe-port-free-windows.cjs``; a connection that is
 *    answered is the evidence a bind cannot give.
 *  - **A listener is not necessarily ours.** The old code killed whatever held
 *    the port. A source-run ``python app.py`` is the user's own process and must
 *    be left alone; the caller tries the next candidate port instead of
 *    deciding that process is disposable.
 */

import net from 'net'
import { spawn } from 'child_process'

/** The backend executable a packaged app ships. Never a bare interpreter. */
export const PACKAGED_BACKEND_IMAGE = 'cowagent-backend.exe'

/** True if we can bind ``host:port`` right now. Says nothing about listeners. */
export function canBind(port: number, host = '127.0.0.1'): Promise<boolean> {
    return new Promise((resolve) => {
        const tester = net.createServer()
        tester.once('error', () => resolve(false))
        tester.once('listening', () => tester.close(() => resolve(true)))
        tester.listen(port, host)
    })
}

/** True if something accepts a connection on ``host:port``. */
export function portAnswers(port: number, host = '127.0.0.1', timeoutMs = 750): Promise<boolean> {
    return new Promise((resolve) => {
        const socket = net.connect({ port, host })
        const settle = (answers: boolean) => {
            socket.destroy()
            resolve(answers)
        }
        socket.setTimeout(timeoutMs)
        socket.once('connect', () => settle(true))
        socket.once('timeout', () => settle(false))
        socket.once('error', () => settle(false))
    })
}

/**
 * True if this port can really be used -- nothing is serving it.
 *
 * The bind is necessary and not sufficient (see the module comment), so a
 * successful bind is followed by an actual connection attempt.
 */
export async function isPortFree(port: number, host = '127.0.0.1'): Promise<boolean> {
    if (!(await canBind(port, host))) return false
    return !(await portAnswers(port, host))
}

/** PIDs listening on ``host:port``, via lsof (POSIX) / netstat (Windows). */
export function listenerPids(port: number): Promise<number[]> {
    return new Promise((resolve) => {
        const isWin = process.platform === 'win32'
        const cmd = isWin ? 'netstat' : 'lsof'
        const args = isWin
            ? ['-ano', '-p', 'tcp']
            : ['-nP', `-iTCP:${port}`, '-sTCP:LISTEN', '-t']
        let out = ''
        try {
            const child = spawn(cmd, args)
            child.stdout?.on('data', (d: Buffer) => (out += d.toString()))
            child.on('error', () => resolve([]))
            child.on('close', () => {
                const pids = new Set<number>()
                if (isWin) {
                    // Match lines like: TCP 127.0.0.1:9876 ... LISTENING  12345
                    for (const line of out.split('\n')) {
                        if (!/LISTENING/i.test(line)) continue
                        if (!new RegExp(`[:.]${port}\\b`).test(line)) continue
                        const pid = Number(line.trim().split(/\s+/).pop())
                        if (Number.isInteger(pid) && pid > 0) pids.add(pid)
                    }
                } else {
                    for (const tok of out.split(/\s+/)) {
                        const pid = Number(tok)
                        if (Number.isInteger(pid) && pid > 0) pids.add(pid)
                    }
                }
                resolve([...pids])
            })
        } catch {
            resolve([])
        }
    })
}

/**
 * The image (executable) name behind a PID, lowercased, or ``''`` if unknown.
 *
 * Used to decide whether a listener is the backend we ship. An empty answer
 * means "not provably ours", which is treated as "leave it alone".
 */
export function listenerImage(pid: number): Promise<string> {
    return new Promise((resolve) => {
        const isWin = process.platform === 'win32'
        const cmd = isWin ? 'tasklist' : 'ps'
        const args = isWin
            ? ['/FI', `PID eq ${pid}`, '/FO', 'CSV', '/NH']
            : ['-o', 'comm=', '-p', String(pid)]
        let out = ''
        try {
            const child = spawn(cmd, args)
            child.stdout?.on('data', (d: Buffer) => (out += d.toString()))
            child.on('error', () => resolve(''))
            child.on('close', () => {
                const text = out.trim()
                if (!text) return resolve('')
                if (isWin) {
                    const match = /^"([^"]+)"/.exec(text)
                    return resolve(match ? match[1].toLowerCase() : '')
                }
                resolve(text.split('\n')[0].trim().toLowerCase())
            })
        } catch {
            resolve('')
        }
    })
}

/**
 * Whether a listener may be cleared before launch.
 *
 * Two provable cases only: the child this instance spawned, or the backend
 * executable a packaged app ships. Everything else -- a source-run
 * ``python app.py``, another application, an unidentifiable process -- is left
 * running on purpose.
 */
export function isOurBackend(pid: number, image: string, childPid?: number): boolean {
    if (childPid !== undefined && pid === childPid) return true
    return image === PACKAGED_BACKEND_IMAGE
}
