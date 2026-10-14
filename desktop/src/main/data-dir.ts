import os from 'os'
import path from 'path'

/**
 * Where this app keeps writable data: ``config.json``, the backend's own
 * ``run.log`` and the shell's ``desktop-main.log``.
 *
 * Single source of truth on purpose. The shell used to compute this twice --
 * ``index.ts`` always wrote to ``~/.cow`` while ``python-manager.ts`` used the
 * checkout in a source run -- so in development the two log files landed in
 * *different* directories, and the "open log folder" button only ever revealed
 * one of them. The backend reads the same ``COW_DATA_DIR`` variable
 * (``config.py`` ``get_data_root``), so one function keeps all three agreed.
 */
export const COW_DATA_DIR = path.join(os.homedir(), '.cow')

/**
 * The data directory for this build.
 *
 * An explicit ``COW_DATA_DIR`` wins in *every* build: the bundled app needs it
 * (its own directory is replaced on update), and a source run accepts it too so
 * a run can be pointed at a private directory instead of the checkout.
 */
export function resolveDataDir(backendPath: string, packaged: boolean): string {
  const override = process.env.COW_DATA_DIR
  if (override) {
    return override.replace(/^~(?=$|[/\\])/, os.homedir())
  }
  return packaged ? COW_DATA_DIR : backendPath
}
