import React, { useState, useCallback, useEffect } from 'react'
import { Routes, Route, useLocation, useNavigate } from 'react-router-dom'
import { History, FolderTree } from 'lucide-react'
import { useSyncExternalStore } from 'react'
import NavRail from './layout/NavRail'
import SessionList from './layout/SessionList'
import WindowControls from './layout/WindowControls'
import StatusScreen from './components/StatusScreen'
import LoginGate, { BlockedGate, PasswordChangeGate, TenantSelectGate } from './components/LoginGate'
import { useBackend } from './hooks/useBackend'
import { usePlatform } from './hooks/usePlatform'
import { usePushPoll } from './hooks/usePushPoll'
import {
  useSchedulerNotifyPoll,
  getSchedulerNotifyState,
  subscribeSchedulerNotify,
} from './hooks/useSchedulerNotifyPoll'
import { useUIStore } from './store/uiStore'
import { useSessionStore } from './store/sessionStore'
import { useWorkspaceStore } from './store/workspaceStore'
import { guardDocEditors } from './store/docEditorStore'
import WorkspacePanel from './components/WorkspacePanel'
import Lightbox from './components/Lightbox'
import ConfirmDialog from './components/ConfirmDialog'
import { initUpdateListener } from './store/updateStore'
import { useOnboardingStore } from './store/onboardingStore'
import OnboardingWizard from './components/OnboardingWizard'
import apiClient from './api/client'
import desktopContext from './api/context'
import { t } from './i18n'
import ChatPage from './pages/ChatPage'
import SettingsPage from './pages/SettingsPage'
import KnowledgePage from './pages/KnowledgePage'
import SkillsPage from './pages/SkillsPage'
import MemoryPage from './pages/MemoryPage'
import ChannelsPage from './pages/ChannelsPage'
import TasksPage from './pages/TasksPage'
import LogsPage from './pages/LogsPage'
import AgentsPage from './pages/AgentsPage'
import { useAgentStore } from './store/agentStore'
import { product } from '@product'

const App: React.FC = () => {
  const backend = useBackend()
  const location = useLocation()
  const navigate = useNavigate()
  const { isWin, isMac } = usePlatform()
  const { sessionsCollapsed, toggleSessions, navCollapsed } = useUIStore()
  const toggleWorkspace = useWorkspaceStore((s) => s.togglePanel)
  const workspaceOpen = useWorkspaceStore((s) => s.open)
  const onboardingOpen = useOnboardingStore((s) => s.open)
  const maybeOpenOnboarding = useOnboardingStore((s) => s.maybeOpen)
  const [, forceUpdate] = useState(0)
  // The single authoritative desktop context (design D8, task 8.1): gate,
  // account projection, selected tenant and epoch all come from one place, so
  // every surface -- chat, upload, stream, preview, settings -- sees the same
  // identity and the same tenant. The renderer holds no credential.
  const context = useSyncExternalStore(desktopContext.subscribe, desktopContext.getSnapshot)
  // Why the scheduler notification poll is stopped (permission/service error).
  // The hook clears it once a fresh capability projection reopens the action.
  const notifyState = useSyncExternalStore(subscribeSchedulerNotify, getSchedulerNotifyState)
  const authState: 'checking' | 'need_login' | 'ok' =
    context.gate === 'checking'
      ? 'checking'
      : context.gate === 'ready'
        ? 'ok'
        : 'need_login'
  const [productAuthed, setProductAuthed] = useState(false)
  // Optional gate provided by '@product'. `product.auth` is constant for the
  // whole build, so calling its hook conditionally is stable across renders.
  // eslint-disable-next-line react-hooks/rules-of-hooks
  const productRequiresAuth = product.auth ? product.auth.useRequiresAuth() : false

  useEffect(() => {
    if (backend.status === 'ready') apiClient.setBaseUrl(backend.baseUrl)
  }, [backend.status, backend.baseUrl])

  // A file dropped where no drop zone handles it makes Chromium navigate to
  // that file, replacing the app. Swallow those at the document level; pages
  // that accept files (chat input, knowledge import) still get the event first.
  useEffect(() => {
    const swallow = (e: DragEvent) => {
      if (e.dataTransfer?.types.includes('Files')) e.preventDefault()
    }
    document.addEventListener('dragover', swallow)
    document.addEventListener('drop', swallow)
    return () => {
      document.removeEventListener('dragover', swallow)
      document.removeEventListener('drop', swallow)
    }
  }, [])

  // Resolve the authoritative identity context once the backend is ready. The
  // main process decides the gate (signed out / password change / tenant choice
  // / ready); this window never inspects a credential.
  useEffect(() => {
    if (backend.status !== 'ready') return
    void desktopContext.probe()
  }, [backend.status, backend.baseUrl])

  // First-run check: once the backend is ready, decide whether to show the
  // onboarding wizard. It's config-driven — shown whenever the chat model isn't
  // configured (and not dismissed earlier this session); no persisted flag.
  useEffect(() => {
    if (backend.status !== 'ready' || authState !== 'ok') return
    // An extension may opt out of the built-in setup wizard.
    if (product.onboarding?.enabled === false) return
    let cancelled = false
    apiClient
      .getModels()
      .then((data) => {
        if (cancelled) return
        const chat = data.capabilities?.chat
        // "Configured" needs a chat provider+model AND that provider's API key
        // set. A default config can ship a model name with no key, which
        // shouldn't count as ready — otherwise we'd skip onboarding for users
        // who still need to enter a key.
        const providerId = chat?.current_provider
        const provider = data.providers?.find((p) => p.id === providerId)
        const keyReady = !!provider && (provider.configured || (provider.is_custom && !!provider.custom_name))
        const configured = !!providerId && !!chat?.current_model && keyReady
        maybeOpenOnboarding(configured)
      })
      .catch(() => {
        // If models can't be loaded, fall back to the flag-only decision.
        if (!cancelled) maybeOpenOnboarding(false)
      })
    return () => {
      cancelled = true
    }
  }, [backend.status, authState, maybeOpenOnboarding])

  // Load the team roster once the backend and auth are settled. This is
  // best-effort: the store swallows every error and degrades to single-Agent,
  // so a legacy backend (no /api/agents) or a transient failure never blocks
  // the app — the team affordances simply stay hidden.
  const refreshRoster = useAgentStore((s) => s.refresh)
  useEffect(() => {
    if (backend.status === 'ready' && authState === 'ok') void refreshRoster()
  }, [backend.status, authState, backend.baseUrl, refreshRoster])

  // Poll for scheduler/push messages once the backend and auth are settled.
  usePushPoll(backend.status === 'ready' && authState === 'ok')
  // Independently watch the global runs ledger so a scheduled task firing into a
  // non-active session still notifies (usePushPoll only sees the open session).
  useSchedulerNotifyPoll(backend.status === 'ready' && authState === 'ok')

  // A clicked OS notification asks us to open its session.
  useEffect(() => {
    const off = window.electronAPI?.onOpenSession?.((sessionId) => {
      useSessionStore.getState().setActive(sessionId)
      navigate('/')
    })
    return off
  }, [navigate])

  // Subscribe to auto-update status from the main process (no-op in dev).
  useEffect(() => initUpdateListener(), [])

  // Handle app-menu / shortcut actions forwarded from the main process.
  useEffect(() => {
    const off = window.electronAPI?.onMenuAction?.(async (action) => {
      // Each of these leaves the current page, taking any open editor with it.
      if (!(await guardDocEditors())) return
      if (action === 'new-chat') {
        if (!(await useWorkspaceStore.getState().guardUnsavedEdit())) return
        useSessionStore.getState().newSession()
        navigate('/')
      } else if (action === 'open-settings') {
        navigate('/settings')
      } else if (action === 'view-logs') {
        navigate('/logs')
      }
    })
    return off
  }, [navigate])

  const handleLangChange = useCallback(() => forceUpdate((n) => n + 1), [])

  if (backend.status !== 'ready') {
    return (
      <StatusScreen
        status={backend.status}
        error={backend.error}
        code={backend.code}
        path={backend.path}
        slow={backend.slow}
        reconnecting={backend.reconnecting}
        onRetry={backend.restart}
      />
    )
  }

  // Backend is up but we're still resolving auth — keep the loading screen.
  if (authState === 'checking' || context.gate === 'checking') {
    return <StatusScreen status="connecting" onRetry={backend.restart} />
  }

  // Each gate renders its own concrete state (design D8). None of them turns a
  // refusal into a usable app: a forced password change has only the change
  // form, an unconfirmed sign-out stops traffic.
  if (context.gate === 'password_change') {
    return <PasswordChangeGate onAuthenticated={() => undefined} />
  }
  if (context.gate === 'blocked') {
    return <BlockedGate onAuthenticated={() => undefined} />
  }
  if (context.gate === 'need_login') {
    return <LoginGate onAuthenticated={() => undefined} />
  }
  if (context.gate === 'tenant_select') {
    return <TenantSelectGate onAuthenticated={() => undefined} />
  }

  // A signed-in account with no active tenant membership keeps its account
  // domain: settings, logs and (for a platform admin) platform entry points.
  // Tenant business -- chat, sessions, memory, channels, knowledge, agents and
  // tasks -- is not initialized, and no forged tenant/Membership is sent.
  const zeroTenant = !!context.session && context.session.tenants.length === 0
  const ACCOUNT_ONLY_PATHS = ['/settings', '/models', '/logs']
  if (zeroTenant && !ACCOUNT_ONLY_PATHS.includes(location.pathname) && !(product.routes || []).some((r) => r.path === location.pathname)) {
    return (
      <div className="flex h-screen overflow-hidden bg-base text-content">
        <NavRail onLangChange={handleLangChange} />
        <div className="flex-1 flex items-center justify-center">
          <div className="max-w-md text-center space-y-4 px-8">
            <h1 className="text-lg font-bold text-content">{t('account_only_title')}</h1>
            <p className="text-sm text-content-tertiary">{t('account_only_desc')}</p>
            <button
              type="button"
              onClick={() => void apiClient.authLogout().catch(() => undefined)}
              className="px-4 py-2.5 bg-primary-500 hover:bg-primary-600 text-white rounded-lg text-sm font-medium cursor-pointer"
            >
              {t('login_browser_cancel')}
            </button>
          </div>
        </div>
      </div>
    )
  }

  // Optional gate from '@product', shown after the local auth check passes.
  // Rendered inside the layout (nav rail stays visible) so the app's features
  // are on display while the login card sits in the content area.
  const ProductGate = product.auth?.Gate
  const showProductGate = !!(ProductGate && productRequiresAuth && !productAuthed)

  const isChat = location.pathname === '/'
  const showSessions = isChat && !sessionsCollapsed && !showProductGate

  return (
    <div className="flex h-screen overflow-hidden bg-base text-content">
      {onboardingOpen && <OnboardingWizard onDone={handleLangChange} />}
      <Lightbox />
      <ConfirmDialog />
      <NavRail onLangChange={handleLangChange} />

      {showSessions && <SessionList />}

      <div className="flex-1 flex flex-col min-w-0 h-screen">
        {/* Top titlebar strip — drag region + Windows controls */}
        <header className="h-[44px] flex items-center gap-1 px-2 flex-shrink-0 titlebar-drag bg-base border-b border-default">
          {isChat && sessionsCollapsed && (
            <button
              onClick={toggleSessions}
              title={t('session_history')}
              // Keep aligned with the SessionList history button: only nudge
              // right of the macOS traffic lights when the nav rail is collapsed
              // (otherwise the lights stay within the rail and don't overlap).
              className={`titlebar-no-drag inline-flex items-center justify-center w-7 h-7 rounded-btn text-content-tertiary hover:text-content hover:bg-surface-2 cursor-pointer transition-colors ${isMac ? 'mt-1' : ''} ${isMac && navCollapsed ? 'ml-2' : ''}`}
            >
              <History size={16} />
            </button>
          )}
          <div className="flex-1 min-w-0" />
          {isChat && !showProductGate && (
            <button
              onClick={toggleWorkspace}
              title={t('ws_toggle')}
              className={`titlebar-no-drag inline-flex items-center justify-center w-7 h-7 rounded-btn cursor-pointer transition-colors ${
                workspaceOpen
                  ? 'text-accent bg-accent-soft'
                  : 'text-content-tertiary hover:text-content hover:bg-surface-2'
              } ${isMac ? 'mt-1' : ''}`}
            >
              <FolderTree size={16} />
            </button>
          )}
          {product.slots?.HeaderRight && (
            <div className="titlebar-no-drag flex items-center">
              <product.slots.HeaderRight />
            </div>
          )}
          {isWin && <WindowControls />}
        </header>

        {notifyState.paused && (
          // The scheduler notification poll stopped on a permission/service
          // error (design D2/P6). It is never silently swallowed: the banner
          // states why, and the hook clears it only after the capability
          // projection is re-fetched and the action is open again.
          <div
            role="status"
            title={notifyState.reason}
            className="flex-shrink-0 px-3 py-1.5 text-[11px] bg-danger-soft text-danger border-b border-danger-border"
          >
            {t('scheduler_notify_paused')}
          </div>
        )}

        {/* Content */}
        <div className="flex-1 flex min-h-0 overflow-hidden bg-base">
          <div className="flex-1 flex flex-col min-w-0 min-h-0 overflow-hidden">
          {showProductGate && ProductGate ? (
            <ProductGate onAuthenticated={() => setProductAuthed(true)} />
          ) : (
          <Routes>
            <Route path="/" element={<ChatPage baseUrl={backend.baseUrl} />} />
            <Route path="/knowledge" element={<KnowledgePage baseUrl={backend.baseUrl} />} />
            <Route path="/memory" element={<MemoryPage baseUrl={backend.baseUrl} />} />
            <Route path="/skills" element={<SkillsPage baseUrl={backend.baseUrl} />} />
            <Route path="/channels" element={<ChannelsPage baseUrl={backend.baseUrl} />} />
            <Route path="/agents" element={<AgentsPage baseUrl={backend.baseUrl} />} />
            <Route path="/tasks" element={<TasksPage baseUrl={backend.baseUrl} />} />
            <Route path="/settings" element={<SettingsPage baseUrl={backend.baseUrl} onLangChange={handleLangChange} />} />
            {/* Legacy /models route now lives as a tab inside settings */}
            <Route path="/models" element={<SettingsPage baseUrl={backend.baseUrl} onLangChange={handleLangChange} />} />
            <Route path="/logs" element={<LogsPage baseUrl={backend.baseUrl} />} />
            {product.routes?.map((r) => (
              <Route key={r.path} path={r.path} element={r.element} />
            ))}
          </Routes>
          )}
          </div>
          {isChat && !showProductGate && <WorkspacePanel />}
        </div>
      </div>
    </div>
  )
}

export default App
