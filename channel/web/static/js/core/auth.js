/* Login screen, logout, and the 401 handling around fetch.
   Split out of console.js. These are classic scripts sharing one global
   scope; see channel/web/README.md before changing the load order. */

// =====================================================================
// Authentication
// =====================================================================
// Auth gate for background pollers. When a web_password is set, the /poll and
// /api/scheduler/runs loops must NOT run before the user logs in — otherwise
// they fire every few seconds with no cookie and spam the server log with
// "401 Unauthorized (credentials offered: none)". Both loops call
// requestAuthGatedStart(); the actual start is deferred until the login/auth
// check opens the gate via openAuthGate(). When no password is set the gate is
// opened immediately at startup so behavior is unchanged.
// This file loads after the pollers define their loops but before boot.js, the
// only caller that runs at load time, so the declarations are always in scope.
let authGateOpen = false;
const _pendingAuthGatedStarts = [];
function requestAuthGatedStart(fn) {
    if (authGateOpen) { fn(); return; }
    _pendingAuthGatedStarts.push(fn);
}
function openAuthGate() {
    if (authGateOpen) return;
    authGateOpen = true;
    while (_pendingAuthGatedStarts.length) {
        const fn = _pendingAuthGatedStarts.shift();
        try { fn(); } catch (_) {}
    }
}

function toggleLoginPassword() {
    const input = document.getElementById('login-password');
    const icon = document.querySelector('#login-toggle-pwd i');
    if (input.type === 'password') {
        input.type = 'text';
        icon.classList.replace('fa-eye', 'fa-eye-slash');
    } else {
        input.type = 'password';
        icon.classList.replace('fa-eye-slash', 'fa-eye');
    }
}
window.toggleLoginPassword = toggleLoginPassword;

function showLoginScreen() {
    if (typeof closeAppearancePreferences === 'function') closeAppearancePreferences(false);
    _invalidateAccountIdentity('unauthenticated');
    _accountAppVisible = false;
    // Leaving the account app: the context panel belongs to a signed-in tenant.
    if (typeof disposeContextModule === 'function') disposeContextModule();
    _resetHistorySearch();
    _accountState.authRequired = true;
    _accountState.authenticated = false;
    _accountHidden('login-overlay', false);
    _accountHidden('app', true);
    _accountHidden('auth-check-panel', true);
    _desktopLoginMode();
    _accountHidden('login-error', true);
    _accountHidden('login-username-wrap', false);
    const password = document.getElementById('login-password');
    if (password) { password.value = ''; password.type = 'password'; }
    const icon = document.querySelector('#login-toggle-pwd i');
    if (icon) icon.classList.replace('fa-eye-slash', 'fa-eye');
    const btn = document.getElementById('login-btn');
    if (btn) btn.disabled = !!_accountWritePending;
    const recoveryBtn = document.getElementById('login-recovery-btn');
    if (recoveryBtn) recoveryBtn.disabled = !!_accountWritePending;
    _renderSidebarAccount();
    // A container's only way in is the shell's own login, so the password field
    // must not steal the focus it cannot use (task 2.4).
    if (_desktopLoginRecovery()) return;
    document.getElementById('login-username')?.focus();
}

async function handleLogout() {
    if (_accountWritePending || !((_accountState.authRequired && _accountState.authenticated)
            || _accountState.phase === 'logout_error')) return;
    _accountWritePending = 'logout';
    _invalidateHistoryCache();
    _invalidateAccountIdentity('logout_pending');
    _resetHistorySearch();
    const epoch = _authEpoch;
    _renderSidebarAccount();
    // Suspend the host's local context *before* the session ends: after the
    // logout call the page may still be alive for a moment, and a host holding
    // the previous tenant's context must not be used (task 5.2).
    if (window.CowDesktopHost) window.CowDesktopHost.suspendLocalContext('logout');
    // The reference belongs to the tenant that minted it; a later turn must not
    // carry it into whatever session comes next (task 2.6).
    _desktopContextClear();
    const onDesktop = _desktopLoginRecovery();
    // The Web end, sequenced by the adapter (task 2.3). The container may retry
    // an already-revoked session; a browser keeps 401 as the failure it was.
    const endWebSession = async (alreadyGoneOk) => {
        const response = await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin' });
        const data = await response.json().catch(() => ({}));
        if (epoch !== _authEpoch) return { ok: false };
        const ended = (response.ok && data && data.status === 'success')
            || Boolean(alreadyGoneOk && response.status === 401);
        return { ok: ended, status: response.status };
    };
    try {
        if (onDesktop) {
            // Serial sign-out, and the reason there is no reload here: a page that
            // reloads into the ordinary password form looks signed in while the
            // native host holds no session, and the next directory bind says
            // "not signed in". The adapter ends the account; the shell draws login.
            const reply = await CowDesktopAccount.logout({ webLogout: () => endWebSession(true) });
            if (epoch !== _authEpoch) return;
            // Not confirmed: keep everything as it is and let the account menu
            // offer the retry, rather than pretending the session ended.
            if (!reply.ok) _accountState = _emptyAccount('logout_error');
            return;
        }
        const ended = await endWebSession(false);
        if (epoch !== _authEpoch) return;
        if (!ended.ok) throw new Error('Logout unconfirmed');
        window.location.reload();
    } catch (_) {
        if (epoch === _authEpoch) _accountState = _emptyAccount('logout_error');
    } finally {
        _accountWritePending = null;
        _renderSidebarAccount();
    }
}
window.handleLogout = handleLogout;

// Intercept 401 responses globally to show login screen on session expiry



function initApp() {
    applyI18n();
    _applyInputTooltips();
    const epoch = _authEpoch;
    // Top-level variables were read before authentication. Restore the actual
    // account/tenant selections only now, then resolve the Agent before any
    // session/history request can capture an old owner or default.
    if (_identityMode() === 'database') {
        activeAgentId = readScopedPreference('cow_active_agent') || '';
        defaultAgentId = readScopedPreference('cow_default_agent') || '';
        resetMemoryView();
        removeScopedPreference('cow_memory_agent');
        knowledgeAgentId = readScopedPreference('cow_knowledge_agent') || '';
    }
    const chatReady = loadChatAgentCatalog().then(agents => {
        if (epoch !== _authEpoch) return;
        // The chat owner is selected from the use range, not the narrower
        // management range. An empty roster must never invent an Agent id.
        defaultAgentId = agents.find(agent => agent.is_default)?.id || '';
        writeScopedPreference('cow_default_agent', defaultAgentId);
        if (!agents.some(agent => agent.id === activeAgentId)) activeAgentId = defaultAgentId;
        writeScopedPreference('cow_active_agent', activeAgentId);
        renderComposerIdentity();
        sessionId = loadOrCreateSessionId();
        refreshWorkspaceSelector();
        // Then resume whatever local project this chat already has open on this
        // machine (task 9.3). After the selector read, so the restored entry is
        // not overwritten by the server's answer, and after the session id, so
        // the host is asked about the chat that is actually being shown.
        _desktopRestoreContext();
        // History waits for these settings before rendering team authors.
        // Let that path issue the single initial read.
        _sessCfg = null;
        restoreChatState();
        startPolling();
    });

    fetch('/api/version').then(async response => {
        if (!response.ok) throw new Error('Version unavailable');
        const data = await response.json();
        if (!data || (data.status && data.status !== 'success')
                || typeof data.version !== 'string' || !data.version.trim()) throw new Error('Invalid version');
        APP_VERSION = 'v' + data.version.trim().replace(/^v/i, '');
        renderAccountVersion();
    }).catch(() => renderAccountVersion());
    return chatReady;
}

