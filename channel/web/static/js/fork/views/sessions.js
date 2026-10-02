/* Master history list, embedded below the sidebar's history disclosure.
 * Querying, pagination and mutations remain owned by console.js. */
const SESSION_PANEL_KEY = 'cow_session_panel_open';
let _sessionPanelWanted = false;
let _sessionPanelScope = null;
let _sessionSurface = '';

function sessionHistorySurface() { return _sessionSurface; }

function resetSessionPanelIdentity() {
    _sessionPanelScope = null;
    _sessionPanelWanted = false;
    _sessionSurface = '';
    _historyVisible = false;
    document.getElementById('session-panel')?.classList.add('hidden');
    document.getElementById('sidebar-recent')?.classList.remove('history-expanded');
}

function sessionSidebarVisible() {
    return window.innerWidth >= 1024
        ? !document.getElementById('app')?.classList.contains('sidebar-collapsed')
        : !document.getElementById('sidebar')?.classList.contains('-translate-x-full');
}

function syncSessionHistorySurface() {
    const panel = document.getElementById('session-panel');
    if (!panel) return;
    const allowed = _accountAppVisible && !_sidebarRecentDenied();
    if (allowed) {
        const scope = _cowUserTenantKey(SESSION_PANEL_KEY);
        if (_sessionPanelScope !== scope) {
            _sessionPanelScope = scope;
            _sessionPanelWanted = readScopedPreference(SESSION_PANEL_KEY) === 'true';
        }
    }
    const next = !allowed ? '' : currentView === 'history' ? 'page'
        : currentView === 'chat' && _sessionPanelWanted && sessionSidebarVisible() ? 'panel' : '';
    const open = next === 'panel';
    panel.classList.toggle('hidden', !open);
    document.getElementById('sidebar-recent')?.classList.toggle('history-expanded', open);
    ['sidebar-recent-label', 'session-panel-toggle-btn'].forEach(id => {
        const button = document.getElementById(id);
        button?.setAttribute('aria-expanded', String(open));
        button?.classList.toggle('hidden', !allowed || (id === 'session-panel-toggle-btn' && currentView !== 'chat'));
    });
    const changed = next !== _sessionSurface;
    _historyVisible = !!next;
    if (changed) {
        _cancelHistoryRequest();
        _closeSessionActionMenu();
        closeNewChatMenus();
        _sessionSurface = next;
        _historyDirty = true;
        _historySearchComposing = false;
        if (next) {
            const mount = document.getElementById(`history-${next === 'panel' ? 'panel' : 'page'}-list-mount`);
            mount.appendChild(document.getElementById('history-status'));
            mount.appendChild(document.getElementById('session-list'));
        }
    }
    if (next && (_historyDirty || !_sessionItems.length) && !_sessionLoading) loadSessionList();
}

function openSessionPanel() {
    if (!_accountAppVisible || _sidebarRecentDenied()) return;
    // Navigation's existing leave guard settles any draft before we open.
    if (currentView !== 'chat') {
        navigateTo('chat', openSessionPanel);
        return;
    }
    _sessionPanelScope = _cowUserTenantKey(SESSION_PANEL_KEY);
    _sessionPanelWanted = true;
    writeScopedPreference(SESSION_PANEL_KEY, 'true');
    if (!sessionSidebarVisible()) toggleSidebar();
    syncSessionHistorySurface();
}

function closeSessionPanel(skipPersist = false) {
    _sessionPanelWanted = false;
    if (!skipPersist) writeScopedPreference(SESSION_PANEL_KEY, 'false');
    syncSessionHistorySurface();
}

function toggleSessionPanel() {
    if (sessionHistorySurface() === 'panel') closeSessionPanel();
    else openSessionPanel();
}

function finishSessionPanelSelection() {
    // The only mobile drawer is the main sidebar; retain its history preference.
    if (window.innerWidth < 1024) closeSidebar();
}

window.addEventListener('resize', syncSessionHistorySurface);
document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !event.defaultPrevented && _sessionSurface === 'panel'
            && window.innerWidth < 1024) closeSidebar();
});
syncSessionHistorySurface();
