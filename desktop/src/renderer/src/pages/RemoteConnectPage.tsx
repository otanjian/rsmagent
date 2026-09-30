import React, { useCallback, useEffect, useState } from 'react'
import { Server, ShieldCheck, ShieldAlert, Trash2, Plus, ArrowLeft, RefreshCw } from 'lucide-react'
import { t } from '../i18n'
import type { DesktopModeProjection, RemoteFailure, RemoteProbeReply } from '../types'

// The trusted local connection shell (change add-desktop-remote-web-workbench,
// task 2.3).
//
// In remote mode the *business* UI comes from the server; this page is the only
// thing the local React app renders, and it deliberately does the minimum: add
// and validate a server, probe it, pick the one active server, and switch back
// to local. It holds no credential -- the native session and the remote
// container are the main process's job -- so nothing here can leak a token.
//
// Every action goes through `window.electronAPI`, which is the *local* preload;
// the remote Web container gets a different, narrower bridge and never reaches
// these channels.

interface Props {
  onLangChange?: () => void
}

const failureText = (failure: RemoteFailure): string => {
  // Stable mapping from the main process's failure kind to a sentence. The
  // server address and any certificate detail are never echoed: a TLS error is
  // reported as a refusal, never as a hint to "try HTTP".
  switch (failure.kind) {
    case 'tls':
      return t('remote_err_tls')
    case 'downgrade':
      return t('remote_err_https_required')
    case 'protocol':
      return t('remote_err_protocol')
    case 'http':
      return t('remote_err_http')
    case 'identity':
      return t('remote_err_identity')
    default:
      return t('remote_err_network')
  }
}

const RemoteConnectPage: React.FC<Props> = () => {
  const [projection, setProjection] = useState<DesktopModeProjection | null>(null)
  const [origin, setOrigin] = useState('')
  const [busy, setBusy] = useState(false)
  const [attached, setAttached] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const api = typeof window !== 'undefined' ? window.electronAPI : undefined

  const refresh = useCallback(async () => {
    if (!api?.desktopModeGet) return
    const reply = await api.desktopModeGet()
    if (reply?.ok) {
      setProjection({
        mode: reply.mode,
        profiles: reply.profiles || [],
        activeProfileId: reply.activeProfileId,
        refused: reply.refused || '',
        containerSupported: reply.containerSupported !== false,
        containerUnsupportedReason: reply.containerUnsupportedReason || '',
      })
    }
  }, [api])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const probeAndAdd = async () => {
    if (!api?.desktopRemoteAddServer || !api?.desktopRemoteProbe) return
    setError('')
    setNotice('')
    setBusy(true)
    try {
      // Probe first so an incompatible or unreachable server is refused before
      // it is ever stored -- a saved profile should be one that answered.
      const result: RemoteProbeReply = await api.desktopRemoteProbe(origin)
      if (!result?.ok) {
        setError(result?.failure ? failureText(result.failure) : t('remote_err_network'))
        return
      }
      if (!result.meta?.remote_web?.available) {
        // The server answered, but does not offer the remote workbench. Say so
        // rather than saving a server that can never be used.
        setError(t('remote_err_not_offered'))
        return
      }
      const added = await api.desktopRemoteAddServer({ origin })
      if (!added?.ok) {
        setError(t('remote_err_invalid'))
        return
      }
      setOrigin('')
      setNotice(t('remote_server_added'))
      await refresh()
    } finally {
      setBusy(false)
    }
  }

  const selectActive = async (id: string | null) => {
    if (!api?.desktopRemoteSetActive) return
    await api.desktopRemoteSetActive(id)
    await refresh()
  }

  const removeServer = async (id: string) => {
    if (!api?.desktopRemoteRemoveServer) return
    await api.desktopRemoteRemoveServer(id)
    await refresh()
  }

  const switchMode = async (mode: 'local' | 'remote') => {
    if (!api?.desktopRemoteSetMode) return
    setError('')
    setNotice('')
    const reply = await api.desktopRemoteSetMode(mode)
    await refresh()
    if (mode === 'remote' && reply && reply.applied === false) {
      // The runtime refused the mode (e.g. the legacy Electron line has no
      // WebContentsView). Say so instead of leaving a mode that will not boot.
      setError(t('remote_container_unsupported'))
      return
    }
    if (mode === 'local') {
      // Leaving remote mode must end the session, not just stop rendering it.
      await api.desktopRemoteDisconnect?.()
      setAttached(false)
      // The mode decides whether the local backend starts, so it takes effect on
      // the next launch. Saying that plainly beats a silent no-op.
      setNotice(t('local_mode_restart'))
      return
    }
    await connect()
  }

  const connect = async () => {
    if (!api?.desktopRemoteConnect) return
    setError('')
    setNotice('')
    setBusy(true)
    try {
      setNotice(t('remote_connecting'))
      const reply = await api.desktopRemoteConnect()
      if (!reply?.ok) {
        setError(reply?.message || t('remote_err_network'))
        return
      }
      setAttached(true)
      setNotice(t('remote_attached'))
    } finally {
      setBusy(false)
    }
  }

  const disconnect = async () => {
    if (!api?.desktopRemoteDisconnect) return
    setError('')
    setNotice('')
    setBusy(true)
    try {
      const reply = await api.desktopRemoteDisconnect()
      setAttached(false)
      if (!reply?.ok) {
        // The server did not confirm the revocation. Say that instead of
        // pretending the session ended; further requests are blocked locally.
        setError(t('remote_err_network'))
        return
      }
      setNotice(t('remote_disconnected'))
    } finally {
      setBusy(false)
    }
  }

  const profiles = projection?.profiles || []
  const active = projection?.activeProfileId || null

  return (
    <div className="flex h-screen overflow-hidden bg-base text-content">
      <div className="flex-1 flex items-start justify-center overflow-auto py-10">
        <div className="w-full max-w-xl px-6 space-y-6">
          <header className="flex items-center gap-2">
            <Server size={18} className="text-primary-500" />
            <h1 className="text-lg font-bold">{t('remote_shell_title')}</h1>
          </header>
          <p className="text-sm text-content-tertiary">{t('remote_shell_desc')}</p>

          {projection?.refused ? (
            <div className="rounded-lg border border-warning-border bg-warning-soft px-3 py-2 text-xs text-warning">
              {t('remote_config_refused')}
            </div>
          ) : null}
          {projection && projection.containerSupported === false ? (
            <div className="rounded-lg border border-warning-border bg-warning-soft px-3 py-2 text-xs text-warning">
              {t('remote_container_unsupported')}
            </div>
          ) : null}

          <section className="space-y-3">
            <h2 className="text-sm font-semibold">{t('remote_servers')}</h2>
            {profiles.length === 0 ? (
              <p className="text-xs text-content-tertiary">{t('remote_no_servers')}</p>
            ) : (
              <ul className="space-y-2">
                {profiles.map((profile) => (
                  <li
                    key={profile.id}
                    className="flex items-center gap-2 rounded-lg border border-default px-3 py-2"
                  >
                    <button
                      type="button"
                      onClick={() => void selectActive(profile.id)}
                      className="flex-1 min-w-0 text-left cursor-pointer"
                    >
                      <div className="flex items-center gap-1.5">
                        {active === profile.id ? (
                          <ShieldCheck size={14} className="text-success" />
                        ) : (
                          <ShieldAlert size={14} className="text-content-tertiary" />
                        )}
                        <span className="text-sm truncate">{profile.displayName}</span>
                      </div>
                      <span className="text-[11px] text-content-tertiary truncate block">{profile.origin}</span>
                    </button>
                    <button
                      type="button"
                      title={t('remote_remove')}
                      onClick={() => void removeServer(profile.id)}
                      className="p-1.5 rounded-btn text-content-tertiary hover:text-danger hover:bg-surface-2 cursor-pointer"
                    >
                      <Trash2 size={14} />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="space-y-2">
            <h2 className="text-sm font-semibold">{t('remote_add_server')}</h2>
            <div className="flex items-center gap-2">
              <input
                value={origin}
                onChange={(e) => setOrigin(e.target.value)}
                placeholder="https://console.example.com"
                spellCheck={false}
                className="flex-1 rounded-lg border border-default bg-surface-1 px-3 py-2 text-sm outline-none focus:border-primary-500"
              />
              <button
                type="button"
                disabled={busy || !origin.trim()}
                onClick={() => void probeAndAdd()}
                className="inline-flex items-center gap-1 rounded-lg bg-primary-500 px-3 py-2 text-sm font-medium text-white hover:bg-primary-600 disabled:opacity-50 cursor-pointer"
              >
                {busy ? <RefreshCw size={14} className="animate-spin" /> : <Plus size={14} />}
                {t('remote_check_and_add')}
              </button>
            </div>
            <p className="text-[11px] text-content-tertiary">{t('remote_https_only')}</p>
          </section>

          {error ? (
            <div className="rounded-lg border border-danger-border bg-danger-soft px-3 py-2 text-xs text-danger">{error}</div>
          ) : null}
          {notice ? (
            <div className="rounded-lg border border-success-border bg-success-soft px-3 py-2 text-xs text-success">{notice}</div>
          ) : null}

          <section className="flex items-center gap-2 pt-2 border-t border-default">
            <button
              type="button"
              disabled={busy || !active || projection?.containerSupported === false}
              onClick={() => void switchMode('remote')}
              className="rounded-lg bg-primary-500 px-3 py-2 text-sm font-medium text-white hover:bg-primary-600 disabled:opacity-50 cursor-pointer"
            >
              {t('remote_enter')}
            </button>
            {attached ? (
              <button
                type="button"
                disabled={busy}
                onClick={() => void disconnect()}
                className="inline-flex items-center gap-1 rounded-lg border border-default px-3 py-2 text-sm hover:bg-surface-2 disabled:opacity-50 cursor-pointer"
              >
                {t('remote_disconnect')}
              </button>
            ) : null}
            <button
              type="button"
              disabled={busy}
              onClick={() => void switchMode('local')}
              className="inline-flex items-center gap-1 rounded-lg border border-default px-3 py-2 text-sm hover:bg-surface-2 disabled:opacity-50 cursor-pointer"
            >
              <ArrowLeft size={14} />
              {t('remote_back_local')}
            </button>
          </section>
        </div>
      </div>
    </div>
  )
}

export default RemoteConnectPage
