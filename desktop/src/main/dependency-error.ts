/**
 * The dependency a backend crash named, when it named one.
 *
 * Change ``harden-windows-desktop-reliability``, task 2.2: a missing *core*
 * dependency has to stop readiness, and the actionable part of that failure is
 * the module's name -- "ModuleNotFoundError: No module named 'xlsxwriter'" says
 * which installation is incomplete, while "the backend crashed" does not. The
 * optional tools are the other half of the same task: they are checked at first
 * use (``agent/tools/browser/browser_tool.py``), so their absence never blocks
 * chat, and this file is deliberately *not* a dependency registry -- it only
 * reads what Python already said.
 *
 * Kept out of ``python-manager.ts`` so the classification can be tested against
 * real stderr text without an Electron runtime (the same reason
 * ``backend-port.ts`` exists).
 */

/** The failure code the shell shows for a missing module. */
export const DEPENDENCY_MISSING = 'dependency_missing'

/** The two shapes CPython uses when an import cannot be satisfied. */
const MODULE_PATTERNS = [
    /ModuleNotFoundError: No module named '([^']+)'/,
    /ImportError: cannot import name '([^']+)'/,
]

/**
 * The first module named by ``lines``, or ``null`` when none was.
 *
 * Only the *first* one is reported: the traceback that follows a missing core
 * dependency is usually a cascade, and the first line is the cause the user can
 * act on.
 */
export function missingModuleFrom(lines: readonly string[]): string | null {
    for (const line of lines) {
        for (const pattern of MODULE_PATTERNS) {
            const match = pattern.exec(line)
            if (match) return match[1]
        }
    }
    return null
}
