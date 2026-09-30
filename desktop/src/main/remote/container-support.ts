// Remote-container platform support (change add-desktop-remote-web-workbench,
// task 1.5).
//
// The remote workbench is carried in an Electron ``WebContentsView``, which
// arrived in Electron 30. This repository also publishes a Windows 7 legacy
// line pinned to Electron 22 (``.github/workflows/release-win7.yml`` installs
// ``electron@22.3.27``), and that line must never load a remote module it
// cannot run. Rather than hoping no one selects remote mode there, the main
// process asks this module before it honours the stored mode: on an
// unsupported runtime the launch is forced to local and the reason is reported,
// so the legacy build degrades to exactly the app it shipped as.
//
// Pure Node (no Electron import) so the decision is directly testable.

/** The first Electron major that provides ``WebContentsView``. */
export const MIN_ELECTRON_MAJOR_FOR_CONTAINER = 30

/** Parse an Electron version string ("33.4.11", "22.3.27", "v30.0.0"). */
export function electronMajor(version: string | null | undefined): number | null {
  if (typeof version !== 'string') return null
  const match = /^v?(\d+)/.exec(version.trim())
  if (!match) return null
  const major = Number(match[1])
  return Number.isFinite(major) ? major : null
}

/** Whether this runtime can host the remote container at all. */
export function supportsWebContentsView(version: string | null | undefined): boolean {
  const major = electronMajor(version)
  if (major === null) return false
  return major >= MIN_ELECTRON_MAJOR_FOR_CONTAINER
}

/**
 * Decide whether a stored ``remote`` mode may actually be honoured.
 *
 * ``version`` is ``process.versions.electron`` at the call site; it is a
 * parameter rather than read here so a test can drive both branches.
 */
export function canHonourRemoteMode(version: string | null | undefined):
  { ok: true } | { ok: false; reason: string } {
  if (!supportsWebContentsView(version)) {
    return { ok: false, reason: 'electron_without_web_contents_view' }
  }
  return { ok: true }
}
