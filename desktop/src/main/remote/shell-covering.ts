/**
 * When a WebContentsView covers the frameless shell at TOP_INSET=0, the main
 * renderer's ``-webkit-app-region: drag`` strips still steal pointer events in
 * that band (Electron composes drag regions from the parent webContents even
 * under a child view). Toggle this class on ``document.documentElement`` so the
 * covered shell stops claiming clicks; the guest page then owns drag via its
 * own ``no-drag`` / ``drag`` CSS on interactive chrome.
 */
export const REMOTE_COVERING_CLASS = 'cow-remote-covering'

export function remoteCoveringScript(enabled: boolean): string {
  const flag = enabled ? 'true' : 'false'
  return `document.documentElement.classList.toggle('${REMOTE_COVERING_CLASS}', ${flag});`
}
