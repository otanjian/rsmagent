import BrandMark from './BrandMark'
import React, { useCallback, useEffect, useRef, useState } from 'react'
import apiClient from '../api/client'
import desktopContext from '../api/context'
import { t } from '../i18n'

/**
 * The Desktop identity gates (design D8, tasks 8.2/8.3/8.6).
 *
 * There are four of them, in the design's order, and each renders a concrete
 * state rather than hiding a failure:
 *
 * * ``LoginGate``      -- start the browser authorization (no password here);
 * * ``PasswordChangeGate`` -- the only thing a restricted account may do;
 * * ``TenantSelectGate``   -- pick the business tenant; nothing tenant-scoped
 *   starts before this, so no request is issued against an unconfirmed tenant;
 * * ``BlockedGate``    -- sign-out was not confirmed server-side, so business
 *   requests are stopped instead of quietly continuing on a live session.
 */

const cardClass =
  'text-center space-y-6 max-w-md px-8 w-full'
const inputClass =
  'w-full px-4 py-2.5 rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-[#1a1a1a] text-slate-800 dark:text-slate-100 text-sm outline-none focus:border-primary-500 transition-colors'
const buttonClass =
  'w-full inline-flex items-center justify-center gap-2 px-4 py-2.5 bg-primary-500 hover:bg-primary-600 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg transition-colors text-sm font-medium cursor-pointer'

const Shell: React.FC<{ title: string; desc: string; children: React.ReactNode }> = ({ title, desc, children }) => (
  <div className="h-screen w-screen flex items-center justify-center bg-gray-50 dark:bg-[#111111]">
    <div className={cardClass}>
      <BrandMark className="w-16 h-16 p-3 rounded-2xl mx-auto bg-white dark:bg-[#1c1c1f] border border-slate-200 dark:border-white/10" />
      <div className="space-y-2">
        <h1 className="text-xl font-bold text-slate-800 dark:text-slate-100">{title}</h1>
        <p className="text-sm text-slate-500 dark:text-slate-400">{desc}</p>
      </div>
      {children}
    </div>
  </div>
)

interface GateProps {
  onAuthenticated: () => void
}

/**
 * Start the native authorization in the system browser.
 *
 * The password is typed into the browser, on the backend's own origin, so this
 * window never sees a credential and never stores one. While the browser
 * round trip is open the gate reports that it is waiting, and cancelling closes
 * the loopback listener and drops the pending verifier/state.
 */
export const LoginGate: React.FC<GateProps> = ({ onAuthenticated }) => {
  const [phase, setPhase] = useState<'idle' | 'starting' | 'waiting' | 'cancelling'>('idle')
  const [serverOrigin, setServerOrigin] = useState('')
  const [loadingServer, setLoadingServer] = useState(true)
  const [error, setError] = useState('')
  const attempt = useRef(0)
  const inFlight = useRef(false)
  const busy = phase !== 'idle'

  useEffect(() => {
    let mounted = true
    const api = window.electronAPI
    void api?.desktopModeGet?.().then(reply => {
      if (!mounted) return
      setServerOrigin(reply.serverOrigin
        || reply.profiles?.find(profile => profile.id === reply.activeProfileId)?.origin || '')
    }).catch(() => {
      if (mounted) setError(t('login_server_load_failed'))
    }).finally(() => {
      if (mounted) setLoadingServer(false)
    })
    if (!api?.desktopModeGet) setLoadingServer(false)
    return () => { mounted = false; attempt.current++ }
  }, [])

  const start = useCallback(async () => {
    if (inFlight.current || loadingServer) return
    inFlight.current = true
    const current = ++attempt.current
    setPhase('starting')
    setError('')
    try {
      const api = window.electronAPI
      if (!api?.desktopLoginServerSet) throw new Error(t('login_bridge_missing'))
      const selected = await api.desktopLoginServerSet(serverOrigin.trim())
      if (current !== attempt.current) return
      if (!selected.ok) {
        const reason = selected.reason || selected.code
        throw new Error(t(reason === 'sign_out_required' ? 'identity_blocked_desc'
          : reason === 'newer_version' ? 'login_bridge_missing' : 'login_server_invalid'))
      }
      if (selected.origin) setServerOrigin(selected.origin)
      setPhase('waiting')
      await apiClient.beginDesktopSignIn()
      if (current === attempt.current) onAuthenticated()
    } catch (e) {
      if (current !== attempt.current) return
      const code = (e as { code?: string })?.code
      if (code !== 'authorization_cancelled') {
        const message = e instanceof Error ? e.message : ''
        setError(code === 'authorization_timeout' ? t('login_browser_timeout')
          : code === 'authorization_server_changed' ? t('login_browser_changed')
          : code ? t('login_browser_failed') : message || t('login_browser_failed'))
      }
    } finally {
      if (current === attempt.current) {
        inFlight.current = false
        setPhase('idle')
      }
    }
  }, [loadingServer, serverOrigin, onAuthenticated])

  const cancel = useCallback(async () => {
    const current = ++attempt.current
    setPhase('cancelling')
    setError('')
    try {
      await desktopContext.cancelAuthorization()
    } catch {
      if (current === attempt.current) setError(t('login_browser_failed'))
    } finally {
      if (current === attempt.current) {
        inFlight.current = false
        setPhase('idle')
      }
    }
  }, [])

  if (!desktopContext.isBrokerAvailable) {
    return (
      <Shell title={t('login_title')} desc={t('login_desc')}>
        <p className="text-sm text-amber-600 dark:text-amber-400">{t('login_bridge_missing')}</p>
      </Shell>
    )
  }

  return (
    <Shell title={t('login_title')} desc={t('login_browser_desc')}>
      <form className="space-y-4" onSubmit={event => { event.preventDefault(); void start() }}>
        <div className="space-y-2 text-left">
          <label htmlFor="login-server-origin" className="block text-sm font-medium text-slate-700 dark:text-slate-200">
            {t('login_server_label')}
          </label>
          <input id="login-server-origin" type="url" required maxLength={2048}
            value={serverOrigin} onChange={event => { setServerOrigin(event.target.value); setError('') }}
            disabled={busy || loadingServer} placeholder="https://ai.example.com" spellCheck={false}
            autoComplete="url" aria-describedby="login-server-hint" className={inputClass} />
          <p id="login-server-hint" className="text-xs text-slate-500 dark:text-slate-400">{t('login_server_hint')}</p>
        </div>
        {error && <p role="alert" className="text-sm text-red-500">{error}</p>}
        {phase === 'waiting' && <p role="status" className="text-sm text-slate-500 dark:text-slate-400">{t('login_browser_waiting')}</p>}
        <button type="submit" disabled={busy || loadingServer || !serverOrigin.trim()} className={buttonClass}>
          {phase === 'starting' ? t('login_checking') : t('login_browser_submit')}
        </button>
        {(phase === 'waiting' || phase === 'cancelling') && (
          <button type="button" onClick={() => void cancel()} disabled={phase === 'cancelling'}
            className="w-full text-sm text-slate-500 hover:text-slate-700 dark:text-slate-400 disabled:opacity-50">
            {t('login_browser_cancel')}
          </button>
        )}
      </form>
    </Shell>
  )
}

/** Forced password change: the only business action a restricted account has. */
export const PasswordChangeGate: React.FC<GateProps> = ({ onAuthenticated }) => {
  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!oldPassword || !newPassword || busy) return
    setBusy(true)
    setError('')
    try {
      await desktopContext.changePassword(oldPassword, newPassword)
      // The server revoked the session on success: authorize again.
      setDone(true)
      onAuthenticated()
    } catch (err) {
      setError(err instanceof Error && err.message ? err.message : t('password_change_failed'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Shell title={t('password_change_title')} desc={t('password_change_desc')}>
      {done ? (
        <p className="text-sm text-slate-500 dark:text-slate-400">{t('password_change_relogin')}</p>
      ) : (
        <form onSubmit={submit} className="space-y-4">
          <input
            type="password"
            autoFocus
            autoComplete="current-password"
            value={oldPassword}
            onChange={(e) => setOldPassword(e.target.value)}
            placeholder={t('password_change_old')}
            className={inputClass}
          />
          <input
            type="password"
            autoComplete="new-password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            placeholder={t('password_change_new')}
            className={inputClass}
          />
          {error && <p className="text-sm text-red-500">{error}</p>}
          <button type="submit" disabled={busy || !oldPassword || !newPassword} className={buttonClass}>
            {t('password_change_submit')}
          </button>
          <button
            type="button"
            onClick={() => {
              void apiClient.authLogout().catch(() => setError(t('identity_blocked_desc')))
            }}
            className="w-full text-sm text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200"
          >
            {t('login_browser_cancel')}
          </button>
        </form>
      )}
    </Shell>
  )
}

/**
 * Tenant selection.
 *
 * The list is the account's own authoritative membership list -- it is never
 * inferred from a username, an Agent id or a default. Until one is chosen no
 * tenant-scoped request is issued at all, so no data is loaded under an
 * unconfirmed tenant.
 */
export const TenantSelectGate: React.FC<GateProps> = ({ onAuthenticated }) => {
  const session = desktopContext.getSnapshot().session
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (desktopContext.getSnapshot().gate === 'ready') onAuthenticated()
  }, [onAuthenticated])

  if (!session) return null

  const choose = async (tenantId: string) => {
    setBusy(tenantId)
    setError('')
    try {
      await desktopContext.selectTenant(tenantId)
      onAuthenticated()
    } catch (e) {
      setError(t('tenant_invalid'))
    } finally {
      setBusy('')
    }
  }

  return (
    <Shell title={t('tenant_select_title')} desc={t('tenant_select_desc')}>
      <div className="space-y-2">
        {session.tenants.map((tenant) => (
          <button
            key={tenant.id}
            type="button"
            disabled={!!busy}
            onClick={() => void choose(tenant.id)}
            className="w-full px-4 py-2.5 rounded-lg border border-slate-300 dark:border-slate-700 bg-white dark:bg-[#1a1a1a] text-slate-800 dark:text-slate-100 text-sm hover:border-primary-500 transition-colors"
          >
            {tenant.name} <span className="text-slate-400">({tenant.code})</span>
          </button>
        ))}
      </div>
      {error && <p className="text-sm text-red-500">{error}</p>}
    </Shell>
  )
}

/** Sign-out was not confirmed by the server: stop, say so, offer a retry. */
export const BlockedGate: React.FC<GateProps> = () => {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const retry = async () => {
    setBusy(true)
    setError('')
    try {
      await apiClient.authLogout()
    } catch {
      setError(t('identity_blocked_desc'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Shell title={t('identity_blocked_title')} desc={t('identity_blocked_desc')}>
      {error && <p className="text-sm text-red-500">{error}</p>}
      <button type="button" disabled={busy} onClick={() => void retry()} className={buttonClass}>
        {t('identity_blocked_retry')}
      </button>
    </Shell>
  )
}

export default LoginGate
