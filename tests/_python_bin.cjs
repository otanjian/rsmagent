// Find a Python interpreter the way this repository's own tools do.
//
// Change ``harden-windows-desktop-reliability``, task 2.1 taught the desktop to
// resolve an interpreter honestly: explicit path, then the platform venv, then
// PATH -- and to refuse the Microsoft Store placeholder aliases, which exist on
// PATH but are not interpreters. The test helpers had the same bug in miniature:
// they hardcoded ``.venv/bin/python`` (a path that cannot exist on Windows --
// this checkout's ``.venv`` was created on macOS), and two of them fell back to
// it even after every candidate had failed, so the failure a reader saw was
// ``spawnSync ... ENOENT`` instead of "this host has no interpreter".
//
// A test that needs the real Python must therefore *skip* when there is none,
// and never mistake a placeholder for one. Candidates are located before they
// are run, so the Store alias is never launched.
//
// Usage:
//   const { resolvePython, PYTHON_SKIP_REASON } = require('./_python_bin')
//   const PY = resolvePython()
//   test('...', { skip: PY ? false : PYTHON_SKIP_REASON }, () => { ... PY.path ... })

const { spawnSync } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')

const ROOT = path.join(__dirname, '..')
const PROBE = 'import sys; sys.stdout.write(sys.executable or "")'
const LAUNCHER_RE = /[\\/]WindowsApps[\\/]/i

/** The real path of a bare command name, or null (never runs it). */
function located(candidate) {
    if (!candidate) return null // an unset environment variable is not a candidate
    if (path.isAbsolute(candidate)) {
        return fs.existsSync(candidate) ? candidate : null
    }
    const which = process.platform === 'win32' ? 'where' : 'which'
    const found = spawnSync(which, [candidate], { encoding: 'utf8' })
    if (found.status !== 0) return null
    const first = String(found.stdout || '')
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean)[0]
    if (!first) return null
    // The Store alias is a launcher, not an interpreter: never start it.
    return LAUNCHER_RE.test(first) ? null : first
}

/** Run the candidate and accept it only if it really is an interpreter. */
function usable(candidate) {
    const locatedPath = located(candidate)
    if (!locatedPath) return null
    const probed = spawnSync(locatedPath, ['-c', PROBE], { encoding: 'utf8', timeout: 20000 })
    if (probed.status !== 0) return null
    const executable = String(probed.stdout || '').trim()
    if (!executable || LAUNCHER_RE.test(executable)) return null
    return { path: locatedPath, executable }
}

const PYTHON_SKIP_REASON = 'no usable Python interpreter on this host'
let cached

/** ``{ path, executable }`` for the first usable interpreter, else ``null``. */
function resolvePython() {
    if (cached !== undefined) return cached
    const candidates = [
        process.env.COW_PYTHON_PATH,
        process.env.PYTHON_BIN,
        process.env.SAP_WORKBENCH_PYTHON,
        process.env.PYTHON,
        path.join(ROOT, '.venv-win', 'Scripts', 'python.exe'),
        path.join(ROOT, '.venv', 'Scripts', 'python.exe'),
        path.join(ROOT, '.venv', 'bin', 'python'),
        'python3',
        'python',
    ]
    cached = null
    for (const candidate of candidates) {
        const found = usable(candidate)
        if (found) {
            cached = found
            break
        }
    }
    return cached
}

/**
 * A ``node:test`` ``test`` that skips itself when no interpreter is available.
 *
 * Every test in a Python-driven file depends on the same thing, so the option is
 * injected once instead of repeated at each call site.
 */
function pythonTest(runner) {
    const resolve = resolvePython()
    const options = resolve ? {} : { skip: PYTHON_SKIP_REASON }
    const wrapped = (name, fn) => runner(name, options, fn)
    // ``require('node:test')`` (and its ``test`` member) is callable *and* carries
    // before/after/describe; a Python-driven file often uses both. Carry them
    // over so replacing ``test`` with this wrapper changes nothing else.
    for (const key of Object.keys(runner)) {
        const value = runner[key]
        wrapped[key] = typeof value === 'function' ? value.bind(runner) : value
    }
    return wrapped
}

module.exports = { resolvePython, pythonTest, PYTHON_SKIP_REASON }
