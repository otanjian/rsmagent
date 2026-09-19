# Frontend adjudication worklist

109 regions where the fork's console edit and upstream's refactor
collide. For each: choose `fork`, `upstream`, or `merged`, and record why.
Auto-transplanting the fork's function is not used here on purpose: it would
silently drop upstream's change to the same code.

## `channel/web/static/js/core/auth.js` — 16 region(s)

### symbol `_identityMode` (console lines 18445–18642, base lines 13854–13853)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
function _identityMode() {
    return 'database';
}

// Console navigation presentation switch, injected by the backend as a validated
// value in { "classic", "split" }. It is layout-only and never changes
// authorization, the identity mode, or any consumer open/closed state. Invalid
// or missing values fall back to "classic".
const _NAVIGATION_MODES = ['classic', 'split'];
function _navigationMode() {
    const raw = String(window.__COW_NAVIGATION_MODE__ || 'classic').trim().toLowerCase();
    return _NAVIGATION_MODES.indexOf(raw) >= 0 ? raw : 'classic';
}

// === NAV_AREA_BEGIN ===
function _navAreaFromPath(pathname) {
    const p = String(pathname || '');
    return p === '/admin' || p.startsWith('/admin/') ? 'admin' : 'workbench';
}
function _openNavArea(area, path) {
    const target = path || (area === 'admin' ? '/admin' : '/chat');
    // Switch in the SAME window so workbench <-> admin never triggers a full
    // page load. A full load flashes the login overlay and keeps #app hidden
    // until /auth/check resolves (the "闪到登录页又好了" symptom). Instead we
    // update the URL via pushState and re-render the area shell; the CSS
    // keyed on #app[data-nav-area] toggles which sidebar shell applies.
    const viaHistory = typeof window !== 'undefined' && window.history
        && typeof window.history.pushState === 'function';
    if (viaHistory) {
        try { window.history.pushState({ cowArea: area }, '', target); }
        catch (_) { window.location.assign(target); return; }
    } else {
        window.location.assign(target);
        return;
    }
    _applyNavAreaAttribute();
    if (typeof _bootAreaDefaultView === 'function') _bootAreaDefaultView();
    // The sidebar recent-sessions list is only fetched for the workbench area
    // (see the guard inside loadSidebarRecentSessions). Because navigation now
    // happens in-place via pushState (no full page load), the normal boot hook
```
</details>

### symbol `showLoginScreen` (console lines 18657–18676, base lines 13868–13883)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    if (typeof closeAppearancePreferences === 'function') closeAppearancePreferences(false);
    _invalidateAccountIdentity('unauthenticated');
    _accountAppVisible = false;
    _resetHistorySearch();
    _accountState.authRequired = true;
    _accountState.authenticated = false;
    _accountHidden('login-overlay', false);
    _accountHidden('app', true);
    _accountHidden('auth-check-panel', true);
    _accountHidden('login-form', false);
    _accountHidden('login-error', true);
    _accountHidden('login-username-wrap', false);
    const password = document.getElementById('login-password');
    if (password) { password.value = ''; password.type = 'password'; }
    const icon = document.querySelector('#login-toggle-pwd i');
    if (icon) icon.classList.replace('fa-eye-slash', 'fa-eye');
    const btn = document.getElementById('login-btn');
    if (btn) btn.disabled = !!_accountWritePending;
    _renderSidebarAccount();
    document.getElementById('login-username')?.focus();
```
</details>

<details><summary>upstream's version of `showLoginScreen`</summary>

```javascript
function showLoginScreen() {
    const overlay = document.getElementById('login-overlay');
    if (!overlay) return;
    overlay.classList.remove('hidden');
    document.getElementById('app').classList.add('hidden');

    const subtitle = document.getElementById('login-subtitle');
    const loginBtn = document.getElementById('login-btn');
    if (currentLang === 'en') {
        subtitle.textContent = 'Enter password to access the console';
        loginBtn.textContent = 'Login';
    } else if (currentLang === 'zh-Hant') {
        subtitle.textContent = '請輸入密碼以存取控制台';
        loginBtn.textContent = '登入';
    } else {
        subtitle.textContent = '请输入密码以访问控制台';
        loginBtn.textContent = '登录';
    }

    const form = document.getElementById('login-form');
    const pwdInput = document.getElementById('login-password');
    pwdInput.focus();

    form.onsubmit = function(e) {
        e.preventDefault();
        const pwd = pwdInput.value;
        if (!pwd) return;
        const btn = document.getElementById('login-btn');
        const errEl = document.getElementById('login-error');
        btn.disabled = true;
        errEl.classList.add('hidden');

        fetch('/auth/login', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({password: pwd})
        }).then(r => r.json()).then(data => {
            if (data.status === 'success') {
                overlay.classList.add('hidden');
                document.getElementById('app').classList.remove('hidden');
                const logoutBtn = document.getElementById('logout-btn-header');
                if (logoutBtn) logoutBtn.classList.remove('hidden');
                // Now that the auth cookie is set, release the parked pollers.
                openAuthGate();
                initApp();
```
</details>

### symbol `_submitAccountLogin` (console lines 18679–18681, base lines 13886–13886)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
async function _submitAccountLogin(event) {
    event.preventDefault();
    if (_accountWritePending || _pendingTenantPicker) return false;
```
</details>

### symbol `_submitAccountLogin` (console lines 18683–18743, base lines 13888–13888)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    const userInput = document.getElementById('login-username');
    if (!pwdInput?.value) return false;
    const epoch = _authEpoch;
    const btn = document.getElementById('login-btn');
    const body = { username: userInput?.value || '', password: pwdInput.value };
    _accountWritePending = 'login';
    ++_accountCheckSeq;
    _accountCheckRequest = null;
    btn.disabled = true;
    _accountHidden('login-error', true);
    try {
        const response = await fetch('/auth/login', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
        });
        const data = await response.json();
        if (epoch !== _authEpoch) return false;
        if (!response.ok || !data || data.status !== 'success') {
            _accountText('login-error', t('account_credentials_error'));
            _accountHidden('login-error', false);
            pwdInput.value = '';
            userInput?.focus();
            return false;
        }
        if (data.identity_mode !== 'database') throw new Error('Invalid login mode');
        const loginNext = _normalizeAccountCheck({
            status: 'success', identity_mode: 'database',
            auth_required: true, authenticated: true, user: data.user,
            must_change_password: Boolean(data.must_change_password)
        });
        _acceptAccountIdentity(loginNext, true);
        pwdInput.value = '';
        // A forced change blocks tenant selection and business load: show the
        // change-password gate and stay there until set.
        if (loginNext.mustChangePassword) {
            if (typeof bumpTenantGeneration === 'function') bumpTenantGeneration();
            _enterForcedPassword();
            return false;
        }
        // Keep only the sanitized user above, before entering the tenant step.
```
</details>

### symbol `_afterLogin` (console lines 18745–18749, base lines 13890–13897)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
function _afterLogin() {
    _clearTenantPicker();
    _resetHistorySearch();
    _enterAccountApp();
}
```
</details>

### symbol `_showTenantPicker` (console lines 18751–18770, base lines 13899–13932)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
function _showTenantPicker(tenants, currentId) {
    _clearTenantPicker();
    const tenantGroup = document.getElementById('login-tenant-group');
    const tenantSelect = document.getElementById('login-tenant-select');
    const nextBtn = document.getElementById('login-btn');
    if (!tenantGroup || !tenantSelect || !nextBtn) throw new Error('Tenant picker unavailable');
    _pendingTenantPicker = true;
    _accountHidden('login-overlay', false);
    _accountHidden('app', true);
    _accountHidden('auth-check-panel', true);
    _accountHidden('login-form', false);
    const epoch = _authEpoch;
    const allowed = new Set(tenants.map(tn => tn.id));
    tenantGroup.classList.remove('hidden');
    tenants.forEach(tn => {
        const opt = document.createElement('option');
        opt.value = tn.id;
        opt.textContent = tn.name || tn.code || tn.id;
        if (tn.id === currentId) opt.selected = true;
        tenantSelect.appendChild(opt);
```
</details>

### symbol `_showTenantPicker` (console lines 18772–18779, base lines 13934–13933)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    const enter = event => {
        event.preventDefault();
        if (epoch !== _authEpoch || !_pendingTenantPicker || _accountWritePending) return false;
        const chosen = tenantSelect.value;
        if (!allowed.has(chosen)) return false;
        sessionStorage.setItem('cow_tenant_id', chosen);
        if (typeof bumpTenantGeneration === 'function') bumpTenantGeneration();
        _afterLogin(true);
```
</details>

### symbol `handleLogout` (console lines 18788–18800, base lines 13938–13942)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
async function handleLogout() {
    if (_accountWritePending || !((_accountState.authRequired && _accountState.authenticated)
            || _accountState.phase === 'logout_error')) return;
    _accountWritePending = 'logout';
    _invalidateAccountIdentity('logout_pending');
    _resetHistorySearch();
    const epoch = _authEpoch;
    _renderSidebarAccount();
    try {
        const response = await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin' });
        const data = await response.json();
        if (epoch !== _authEpoch) return;
        if (!response.ok || !data || data.status !== 'success') throw new Error('Logout unconfirmed');
```
</details>

<details><summary>upstream's version of `handleLogout`</summary>

```javascript
function handleLogout() {
    fetch('/auth/logout', {
        method: 'POST'
    }).then(r => r.json()).then(data => {
        if (data.status === 'success') {
            window.location.reload();
        }
    }).catch(() => {
        window.location.reload();
    });
}
window.handleLogout = handleLogout;

// Intercept 401 responses globally to show login screen on session expiry
const _originalFetch = window.fetch;
window.fetch = function(...args) {
    return _originalFetch.apply(this, args).then(response => {
        if (response.status === 401) {
            const url = typeof args[0] === 'string' ? args[0] : (args[0]?.url || '');
            if (!url.startsWith('/auth/')) {
                showLoginScreen();
            }
        }
        return response;
    });
};

function initApp() {
    applyI18n();
    _applyInputTooltips();
    _restoreSessionPanel();
    refreshWorkspaceSelector();
    refreshSessionSettings();

    fetch('/api/knowledge/list').then(r => r.json()).then(data => {
        if (data.status === 'success') {
            _knowledgeTreeData = data.tree || [];
            _knowledgeRootFiles = data.root_files || [];
        }
    }).catch(() => {});

    fetch('/api/version').then(r => r.json()).then(data => {
        APP_VERSION = `v${data.version}`;
        UPDATE_META = {
            version: data.version || '',
```
</details>

### symbol `handleLogout` (console lines 18802–18806, base lines 13944–13943)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    } catch (_) {
        if (epoch === _authEpoch) _accountState = _emptyAccount('logout_error');
    } finally {
        _accountWritePending = null;
        _renderSidebarAccount();
```
</details>

<details><summary>upstream's version of `handleLogout`</summary>

```javascript
function handleLogout() {
    fetch('/auth/logout', {
        method: 'POST'
    }).then(r => r.json()).then(data => {
        if (data.status === 'success') {
            window.location.reload();
        }
    }).catch(() => {
        window.location.reload();
    });
}
window.handleLogout = handleLogout;

// Intercept 401 responses globally to show login screen on session expiry
const _originalFetch = window.fetch;
window.fetch = function(...args) {
    return _originalFetch.apply(this, args).then(response => {
        if (response.status === 401) {
            const url = typeof args[0] === 'string' ? args[0] : (args[0]?.url || '');
            if (!url.startsWith('/auth/')) {
                showLoginScreen();
            }
        }
        return response;
    });
};

function initApp() {
    applyI18n();
    _applyInputTooltips();
    _restoreSessionPanel();
    refreshWorkspaceSelector();
    refreshSessionSettings();

    fetch('/api/knowledge/list').then(r => r.json()).then(data => {
        if (data.status === 'success') {
            _knowledgeTreeData = data.tree || [];
            _knowledgeRootFiles = data.root_files || [];
        }
    }).catch(() => {});

    fetch('/api/version').then(r => r.json()).then(data => {
        APP_VERSION = `v${data.version}`;
        UPDATE_META = {
            version: data.version || '',
```
</details>

### symbol `handleLogout` (console lines 18808–18807, base lines 13945–13947)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
```
</details>

<details><summary>upstream's version of `handleLogout`</summary>

```javascript
function handleLogout() {
    fetch('/auth/logout', {
        method: 'POST'
    }).then(r => r.json()).then(data => {
        if (data.status === 'success') {
            window.location.reload();
        }
    }).catch(() => {
        window.location.reload();
    });
}
window.handleLogout = handleLogout;

// Intercept 401 responses globally to show login screen on session expiry
const _originalFetch = window.fetch;
window.fetch = function(...args) {
    return _originalFetch.apply(this, args).then(response => {
        if (response.status === 401) {
            const url = typeof args[0] === 'string' ? args[0] : (args[0]?.url || '');
            if (!url.startsWith('/auth/')) {
                showLoginScreen();
            }
        }
        return response;
    });
};

function initApp() {
    applyI18n();
    _applyInputTooltips();
    _restoreSessionPanel();
    refreshWorkspaceSelector();
    refreshSessionSettings();

    fetch('/api/knowledge/list').then(r => r.json()).then(data => {
        if (data.status === 'success') {
            _knowledgeTreeData = data.tree || [];
            _knowledgeRootFiles = data.root_files || [];
        }
    }).catch(() => {});

    fetch('/api/version').then(r => r.json()).then(data => {
        APP_VERSION = `v${data.version}`;
        UPDATE_META = {
            version: data.version || '',
```
</details>

### symbol `initApp` (console lines 18845–18857, base lines 13968–13968)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    const epoch = _authEpoch;
    // Top-level variables were read before authentication. Restore the actual
    // account/tenant selections only now, then resolve the Agent before any
    // session/history request can capture an old owner or default.
    if (_identityMode() === 'database') {
        activeAgentId = readScopedPreference('cow_active_agent') || '';
        defaultAgentId = readScopedPreference('cow_default_agent') || '';
        memoryAgentId = readScopedPreference('cow_memory_agent') || '';
        knowledgeAgentId = readScopedPreference('cow_knowledge_agent') || '';
    }
    const chatReady = Promise.resolve(loadAgentCatalog()).then(() => {
        if (epoch !== _authEpoch) return;
        sessionId = loadOrCreateSessionId();
```
</details>

<details><summary>upstream's version of `initApp`</summary>

```javascript
function initApp() {
    applyI18n();
    _applyInputTooltips();
    _restoreSessionPanel();
    refreshWorkspaceSelector();
    refreshSessionSettings();

    fetch('/api/knowledge/list').then(r => r.json()).then(data => {
        if (data.status === 'success') {
            _knowledgeTreeData = data.tree || [];
            _knowledgeRootFiles = data.root_files || [];
        }
    }).catch(() => {});

    fetch('/api/version').then(r => r.json()).then(data => {
        APP_VERSION = `v${data.version}`;
        UPDATE_META = {
            version: data.version || '',
            install_kind: data.install_kind || 'unknown',
            update_supported: !!data.update_supported,
            unsupported_reason: data.unsupported_reason || ''
        };
        _setSidebarVersionLabel(`CowAgent ${APP_VERSION}`);
    }).catch(() => {
        _setSidebarVersionLabel('CowAgent');
    });
    chatInput.focus();
    // Last, and only from here: initApp() runs once auth has settled, on all
    // three paths into the app. Opening the routed view any earlier would
    // switch views behind the login overlay.
    routeApply();
}

```
</details>

### symbol `initApp` (console lines 18868–18868, base lines 13978–13977)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    });
```
</details>

<details><summary>upstream's version of `initApp`</summary>

```javascript
function initApp() {
    applyI18n();
    _applyInputTooltips();
    _restoreSessionPanel();
    refreshWorkspaceSelector();
    refreshSessionSettings();

    fetch('/api/knowledge/list').then(r => r.json()).then(data => {
        if (data.status === 'success') {
            _knowledgeTreeData = data.tree || [];
            _knowledgeRootFiles = data.root_files || [];
        }
    }).catch(() => {});

    fetch('/api/version').then(r => r.json()).then(data => {
        APP_VERSION = `v${data.version}`;
        UPDATE_META = {
            version: data.version || '',
            install_kind: data.install_kind || 'unknown',
            update_supported: !!data.update_supported,
            unsupported_reason: data.unsupported_reason || ''
        };
        _setSidebarVersionLabel(`CowAgent ${APP_VERSION}`);
    }).catch(() => {
        _setSidebarVersionLabel('CowAgent');
    });
    chatInput.focus();
    // Last, and only from here: initApp() runs once auth has settled, on all
    // three paths into the app. Opening the routed view any earlier would
    // switch views behind the login overlay.
    routeApply();
}

```
</details>

### symbol `initApp` (console lines 18870–19210, base lines 13979–13981)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
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

// =====================================================================
// Account self-context: /auth/me, six-item menu, password, prefs, tenant switch
// =====================================================================
// Read-only self profile cached for the current account/epoch. Cleared on
// account switch/logout so a previous account cannot restore into a new one.
let _accountSelf = null;
let _accountSelfSeq = 0;
let _accountSelfRequest = null;
// Current tenant's authoritative /auth/context capability summary is declared
// with the sidebar account state above (identity invalidation drops it there).
let _activeAccountPanel = null;  // 'profile' | 'password' | 'prefs' | 'tenant' | 'about'

function _db() { return _identityMode() === 'database'; }

function _setAccountPanel(panel) {
    _activeAccountPanel = panel;
    ['account-password-form', 'account-password-status', 'ap-old-password',
     'ap-new-password', 'ap-confirm-password'].forEach(id => {
        const el = document.getElementById(id);
        if (el && panel !== 'password') delete el.dataset.dirty;
    });
}

// Close account surfaces before opening another, preserving the existing
// password form's dismissal guard. The native preferences dialog owns focus.
function closeAccountPanels(returnFocus = false) {
    if (_forcedPassword) return false;
    if (_activeAccountPanel === 'password') {
        cancelAccountPassword();
```
</details>

<details><summary>upstream's version of `initApp`</summary>

```javascript
function initApp() {
    applyI18n();
    _applyInputTooltips();
    _restoreSessionPanel();
    refreshWorkspaceSelector();
    refreshSessionSettings();

    fetch('/api/knowledge/list').then(r => r.json()).then(data => {
        if (data.status === 'success') {
            _knowledgeTreeData = data.tree || [];
            _knowledgeRootFiles = data.root_files || [];
        }
    }).catch(() => {});

    fetch('/api/version').then(r => r.json()).then(data => {
        APP_VERSION = `v${data.version}`;
        UPDATE_META = {
            version: data.version || '',
            install_kind: data.install_kind || 'unknown',
            update_supported: !!data.update_supported,
            unsupported_reason: data.unsupported_reason || ''
        };
        _setSidebarVersionLabel(`CowAgent ${APP_VERSION}`);
    }).catch(() => {
        _setSidebarVersionLabel('CowAgent');
    });
    chatInput.focus();
    // Last, and only from here: initApp() runs once auth has settled, on all
    // three paths into the app. Opening the routed view any earlier would
    // switch views behind the login overlay.
    routeApply();
}

```
</details>

### symbol `openAccountProfile` (console lines 19212–19213, base lines 13983–13983)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
        _accountText('account-profile-status', t('account_profile_error'));
        _accountHidden('account-profile-status', false);
```
</details>

### symbol `openAccountProfile` (console lines 19215–19214, base lines 13985–13985)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
```
</details>

### symbol `openAccountProfile` (console lines 19216–19808, base lines 13987–13986)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript

function renderAccountProfile() {
    const box = document.getElementById('account-profile-content');
    if (!box) return;
    const ctx = _accountSelf;
    box.classList.remove('hidden');
    _accountHidden('account-profile-status', true);
    if (!ctx || !ctx.user) {
        _accountText('account-profile-status', t('account_profile_error'));
        _accountHidden('account-profile-status', false);
        return;
    }
    const user = ctx.user;
    const displayName = user.display_name || user.username || '—';

    // Hero header (avatar + name + username)
    renderAccountProfileAvatar(user);
    _accountText('account-profile-hero-name', displayName);
    _accountText('account-profile-hero-username', '@' + (user.username || ''));

    // Read mode values
    _accountText(_ACCOUNT_PROFILE_SEL.displayName, displayName);
    _accountText(_ACCOUNT_PROFILE_SEL.username, user.username || '—');
    if (user.is_platform_admin) {
        _accountChips(_ACCOUNT_PROFILE_SEL.platform, [t('platform_admin_badge')]);
    } else {
        _accountText(_ACCOUNT_PROFILE_SEL.platform, t('account_public_mode'));
    }

    // Resolve the selected tenant's membership from the self list.
    const tid = sessionStorage.getItem('cow_tenant_id');
    const entry = (ctx.tenants || []).find(tn => tn.id === tid) || (ctx.tenants || [])[0];
    if (!entry) {
        _accountText(_ACCOUNT_PROFILE_SEL.tenant, t('account_profile_no_tenant'));
        _accountText(_ACCOUNT_PROFILE_SEL.memberName, t('account_profile_empty'));
        _accountText(_ACCOUNT_PROFILE_SEL.role, t('account_profile_empty'));
        _accountText(_ACCOUNT_PROFILE_SEL.department, t('account_profile_empty'));
        _accountText(_ACCOUNT_PROFILE_SEL.position, t('account_profile_empty'));
        _accountHidden('account-profile-member-section', true);
        return;
```
</details>

## `channel/web/static/js/views/agents.js` — 12 region(s)

### symbol `agentAvatarHTML` (console lines 2362–2362, base lines 1860–1860)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    // uploaded picture, so the instance's own Agent wears the product mark.
```
</details>

<details><summary>upstream's version of `agentAvatarHTML`</summary>

```javascript
function agentAvatarHTML(agent, size) {
    const cls = `agent-avatar agent-avatar-${size || 32}`;
    if (!agent && defaultAgentId) {
        agent = findAgent(defaultAgentId);
    }
    if (agent && agent.avatar === 'image') {
        // Prefer the server's per-file token (avatar mtime): it changes on every
        // upload, so replacing a picture busts the cache even after a hard reload
        // when in-memory hints are gone and the roster revision hasn't moved.
        const v = avatarVersions[agent.id] || agent.avatar_rev || rosterRevision || agent.id;
        return `<img class="${cls}" src="/api/agents/${encodeURIComponent(agent.id)}/avatar?v=${encodeURIComponent(v)}" alt="">`;
    }
    // The default (first) Agent falls back to the product logo when it has no
    // uploaded picture, so the instance's own Agent wears the CowAgent face.
    // Added Agents keep the initial-disc fallback so a team stays distinguishable.
    if (agent && agent.id && agent.id === defaultAgentId) {
        return `<img class="${cls}" src="/assets/logo.jpg" alt="">`;
    }
    const initial = avatarInitial(agent && (agent.name || agent.id));
    return `<span class="${cls} agent-avatar-tone-${avatarTone(agent && agent.id)}">${escapeHtml(initial)}</span>`;
}

/* Repaint the faces on bubbles already on screen. Bubbles are rendered once and
   left alone, so an avatar changed in Settings would otherwise keep showing the
   old picture in the open conversation until reload. Each bot bubble remembers
   its speaker; the loading indicator follows the active Agent. */
function refreshBubbleAvatars() {
    const container = document.getElementById('chat-messages');
    if (!container) return;
    container.querySelectorAll('.bot-face').forEach(face => {
        const group = face.closest('.bot-message-group');
        // A bubble knows its speaker; the loading indicator (no group) tracks the
        // active Agent, the only one that can be mid-reply in a solo chat.
        const id = (group && group.dataset.speakerAgent) || activeAgentId;
        face.innerHTML = agentAvatarHTML(findAgent(id), 32);
    });
}

// Derive the ascii slug from a name, or '' when there is no ascii to work with
// (e.g. a name written in Chinese). Callers fall back to randomAgentId().
function slugAgentId(name) {
    return String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32);
}

// An id for a name that yields no slug.
```
</details>

### symbol `agentAvatarHTML` (console lines 2364–2365, base lines 1862–1863)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    if (agent && agent.id && (agent.is_default || agent.id === defaultAgentId)) {
        return `<img class="${cls} agent-avatar-brand" src="${effectiveLogoUrl()}" alt="">`;
```
</details>

<details><summary>upstream's version of `agentAvatarHTML`</summary>

```javascript
function agentAvatarHTML(agent, size) {
    const cls = `agent-avatar agent-avatar-${size || 32}`;
    if (!agent && defaultAgentId) {
        agent = findAgent(defaultAgentId);
    }
    if (agent && agent.avatar === 'image') {
        // Prefer the server's per-file token (avatar mtime): it changes on every
        // upload, so replacing a picture busts the cache even after a hard reload
        // when in-memory hints are gone and the roster revision hasn't moved.
        const v = avatarVersions[agent.id] || agent.avatar_rev || rosterRevision || agent.id;
        return `<img class="${cls}" src="/api/agents/${encodeURIComponent(agent.id)}/avatar?v=${encodeURIComponent(v)}" alt="">`;
    }
    // The default (first) Agent falls back to the product logo when it has no
    // uploaded picture, so the instance's own Agent wears the CowAgent face.
    // Added Agents keep the initial-disc fallback so a team stays distinguishable.
    if (agent && agent.id && agent.id === defaultAgentId) {
        return `<img class="${cls}" src="/assets/logo.jpg" alt="">`;
    }
    const initial = avatarInitial(agent && (agent.name || agent.id));
    return `<span class="${cls} agent-avatar-tone-${avatarTone(agent && agent.id)}">${escapeHtml(initial)}</span>`;
}

/* Repaint the faces on bubbles already on screen. Bubbles are rendered once and
   left alone, so an avatar changed in Settings would otherwise keep showing the
   old picture in the open conversation until reload. Each bot bubble remembers
   its speaker; the loading indicator follows the active Agent. */
function refreshBubbleAvatars() {
    const container = document.getElementById('chat-messages');
    if (!container) return;
    container.querySelectorAll('.bot-face').forEach(face => {
        const group = face.closest('.bot-message-group');
        // A bubble knows its speaker; the loading indicator (no group) tracks the
        // active Agent, the only one that can be mid-reply in a solo chat.
        const id = (group && group.dataset.speakerAgent) || activeAgentId;
        face.innerHTML = agentAvatarHTML(findAgent(id), 32);
    });
}

// Derive the ascii slug from a name, or '' when there is no ascii to work with
// (e.g. a name written in Chinese). Callers fall back to randomAgentId().
function slugAgentId(name) {
    return String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32);
}

// An id for a name that yields no slug.
```
</details>

### symbol `ready` (console lines 3393–3411, base lines 2314–2315)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    const ready = () => {
        if (installedSkills.length && installedTools.length) { render(); return; }
        Promise.all([
            installedSkills.length ? Promise.resolve() : fetch('/api/skills').then(r => r.json()).then(d => { installedSkills = d.skills || []; }),
            installedTools.length ? Promise.resolve() : fetch('/api/tools').then(r => r.json()).then(d => { installedTools = d.tools || []; }),
        ]).then(() => render()).catch(() => {
            pane.innerHTML = `<p class="text-sm text-slate-400">${escapeHtml(t('agents_skills_all'))}</p>`;
        });
    };
    ready();
}

function renderAgentTasksPane() {
    const pane = document.getElementById('agent-detail-tasks');
    const agent = findAgent(selectedAdminAgentId);
    if (!pane || !agent) return;
    const render = (tasks) => {
        if (!tasks.length) {
            pane.innerHTML = `<p class="text-sm text-slate-400">${escapeHtml(t('tasks_empty_agent'))}</p>`;
```
</details>

### symbol `render` (console lines 3414–3469, base lines 2318–2322)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
        pane.innerHTML = `<div class="agent-cap-title">${escapeHtml(t('agents_tasks_label'))}</div>`;
        const wrap = document.createElement('div');
        wrap.className = 'agent-task-list';
        tasks.forEach(task => {
            const isEnabled = task.enabled !== false;
            const schedule = task.schedule || {};
            let typeLabel = '';
            if (schedule.type === 'cron') {
                typeLabel = `<span class="text-xs font-mono text-slate-400">${escapeHtml(schedule.expression || '')}</span>`;
            } else if (schedule.type === 'interval') {
                const seconds = schedule.seconds || 0;
                const hours = Math.floor(seconds / 3600);
                const mins = Math.floor((seconds % 3600) / 60);
                typeLabel = hours ? `${hours}h${mins ? ` ${mins}m` : ''}` : `${mins}m`;
                typeLabel = `<span class="text-xs text-slate-400">${escapeHtml(typeLabel)}</span>`;
            } else {
                typeLabel = `<span class="text-xs text-slate-400">${escapeHtml(schedule.type || 'once')}</span>`;
            }
            let nextRun = '--';
            if (task.next_run_at) {
                const d = new Date(task.next_run_at);
                if (!isNaN(d.getTime())) nextRun = d.toLocaleString();
            }
            const action = task.action || {};
            const taskContent = action.content || action.task_description || '';
            const toggleId = 'agent-task-toggle-' + task.id;
            // Same rule as the tasks page: the server decides which verbs exist
            // for this caller, the page only draws them. An admin looking at a
            // member's personal task sees the row without controls.
            const caps = task.capabilities || {run: false, manage: false, view: true};
            const card = document.createElement('div');
            card.className = 'agent-task-card' + (isEnabled ? '' : ' agent-task-card-disabled');
            card.dataset.taskId = task.id;
            card.innerHTML = `
                <div class="flex items-center gap-2 mb-1">
                    <span class="w-2 h-2 rounded-full ${isEnabled ? 'bg-primary-400' : 'bg-slate-300 dark:bg-slate-600'}"></span>
                    <span class="font-medium text-sm text-slate-700 dark:text-slate-200">${escapeHtml(task.name || task.id || '--')}</span>
                    <div class="flex-1"></div>
                    ${typeLabel}
                </div>
```
</details>

<details><summary>upstream's version of `render`</summary>

```javascript
    const render = () => {
        const all = agent.skills == null;
        const picked = new Set(all ? [] : agent.skills);
        pane.innerHTML = `
            <label class="flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300 mb-3">
                <input type="checkbox" id="agent-skills-all" ${all ? 'checked' : ''}>
                <span>${escapeHtml(t('agents_skills_all'))}</span>
            </label>
            <p class="text-xs text-slate-400 mb-3">${escapeHtml(t('agents_skills_pick'))}</p>
            ${(installedSkills || []).map(skill => {
                const name = skill.name || skill.id;
                const checked = all || picked.has(name);
                return `<label class="agent-skill-row">
                    <input type="checkbox" class="agent-skill-item" value="${escapeHtml(name)}" ${checked ? 'checked' : ''} ${all ? 'disabled' : ''}>
                    <div>
                        <div class="text-sm text-slate-700 dark:text-slate-200">${escapeHtml(skill.display_name || name)}</div>
                        <div class="text-xs text-slate-400">${escapeHtml(skill.description || '')}</div>
                    </div>
                </label>`;
            }).join('')}`;
        document.getElementById('agent-skills-all')?.addEventListener('change', (e) => {
            // Toggle only flips ALL <-> empty subset. Turning it off starts from
            // an empty list so the user picks up exactly what they want, and the
            // stored value is [] rather than a full enumeration.
            const next = e.target.checked ? null : [];
            saveAgentSkills(agent, next);
            render();  // repaint in place — no page-wide reload, no flicker
        });
        pane.querySelectorAll('.agent-skill-item').forEach(box => {
            box.addEventListener('change', () => {
                const names = Array.from(pane.querySelectorAll('.agent-skill-item:checked')).map(el => el.value);
                saveAgentSkills(agent, names);
            });
        });
    };
    if (installedSkills.length) {
        render();
        return;
    }
    fetch('/api/skills').then(r => r.json()).then(data => {
        installedSkills = data.skills || [];
        render();
    }).catch(() => {
        pane.innerHTML = `<p class="text-sm text-slate-400">${escapeHtml(t('agents_skills_all'))}</p>`;
    });
```
</details>

### symbol `render` (console lines 3471–3487, base lines 2324–2323)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
            const checkbox = card.querySelector('#' + toggleId);
            if (checkbox) checkbox.addEventListener('change', function() {
                const newEnabled = this.checked;
                fetch('/api/scheduler/toggle', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ task_id: task.id, enabled: newEnabled, agent_id: task.agent_id || agent.id })
                }).then(r => r.json()).then(res => { if (res.status === 'success') renderAgentTasksPane(); });
            });
            wrap.appendChild(card);
        });
        pane.appendChild(wrap);
    };
    return fetch('/api/scheduler?agent_id=' + encodeURIComponent(agent.id))
        .then(r => r.json())
        .then(data => { render(data.tasks || []); })
        .catch(() => { pane.innerHTML = `<p class="text-sm text-slate-400">${escapeHtml(t('tasks_unavailable'))}</p>`; });
```
</details>

<details><summary>upstream's version of `render`</summary>

```javascript
    const render = () => {
        const all = agent.skills == null;
        const picked = new Set(all ? [] : agent.skills);
        pane.innerHTML = `
            <label class="flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300 mb-3">
                <input type="checkbox" id="agent-skills-all" ${all ? 'checked' : ''}>
                <span>${escapeHtml(t('agents_skills_all'))}</span>
            </label>
            <p class="text-xs text-slate-400 mb-3">${escapeHtml(t('agents_skills_pick'))}</p>
            ${(installedSkills || []).map(skill => {
                const name = skill.name || skill.id;
                const checked = all || picked.has(name);
                return `<label class="agent-skill-row">
                    <input type="checkbox" class="agent-skill-item" value="${escapeHtml(name)}" ${checked ? 'checked' : ''} ${all ? 'disabled' : ''}>
                    <div>
                        <div class="text-sm text-slate-700 dark:text-slate-200">${escapeHtml(skill.display_name || name)}</div>
                        <div class="text-xs text-slate-400">${escapeHtml(skill.description || '')}</div>
                    </div>
                </label>`;
            }).join('')}`;
        document.getElementById('agent-skills-all')?.addEventListener('change', (e) => {
            // Toggle only flips ALL <-> empty subset. Turning it off starts from
            // an empty list so the user picks up exactly what they want, and the
            // stored value is [] rather than a full enumeration.
            const next = e.target.checked ? null : [];
            saveAgentSkills(agent, next);
            render();  // repaint in place — no page-wide reload, no flicker
        });
        pane.querySelectorAll('.agent-skill-item').forEach(box => {
            box.addEventListener('change', () => {
                const names = Array.from(pane.querySelectorAll('.agent-skill-item:checked')).map(el => el.value);
                saveAgentSkills(agent, names);
            });
        });
    };
    if (installedSkills.length) {
        render();
        return;
    }
    fetch('/api/skills').then(r => r.json()).then(data => {
        installedSkills = data.skills || [];
        render();
    }).catch(() => {
        pane.innerHTML = `<p class="text-sm text-slate-400">${escapeHtml(t('agents_skills_all'))}</p>`;
    });
```
</details>

### symbol `saveAgentCoreFile` (console lines 4068–4105, base lines 2791–2792)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
// In-flight guard so a double-click on a card cannot spawn two session
// switches. Reset on every completed start / cancellation / error.
let _agentStartInFlight = null;
let _agentStartAgentId = null;

function cancelAgentStart() {
    _agentStartInFlight = null;
    _agentStartAgentId = null;
}

async function startChatWithAgent(agentId) {
    if (!agentId || _agentStartInFlight) return;
    if (typeof wsGuardUnsaved === 'function'
        && !wsGuardUnsaved(() => startChatWithAgent(agentId))) return;
    const attempt = { context: _wbContext() };
    _agentStartInFlight = attempt;
    _agentStartAgentId = agentId;
    _wbNoticeKey = '';
    renderAgentWorkbench();
    try {
        // The use-page roster is independent of the management cache. A card
        // created in another tab must work, and a removed target must not start.
        const agents = await fetchAgentWorkbench();
        if (_agentStartInFlight !== attempt || attempt.context !== _wbContext()) return;
        const agent = agents.find(a => a.id === agentId);
        applyAgentWorkbench(agents);
        if (!agent || !agent.can_chat) {
            refreshWorkbenchAfterUnavailable(agentId, agent?.unavailable_reason);
            return;
        }
        // The user could edit a file while validation was in flight. Settle it
        // once more before committing; no await occurs between this and newChat.
        if (typeof wsGuardUnsaved === 'function'
            && !wsGuardUnsaved(() => startChatWithAgent(agentId))) return;
        const cached = findAgent(agentId);
        if (cached) Object.assign(cached, agent, { enabled: true });
        else agentCatalog.push({ ...agent, enabled: true });
        if (agent.is_default) defaultAgentId = agent.id;
```
</details>

<details><summary>upstream's version of `saveAgentCoreFile`</summary>

```javascript
function saveAgentCoreFile() {
    if (!selectedAdminAgentId) return;
    const filename = currentAgentCoreFile();
    const btn = document.querySelector('#agent-detail-files button[onclick="saveAgentCoreFile()"]');
    _paintCoreFileStatus('pending', '…');
    if (btn) btn.disabled = true;
    fetch(`/api/agents/${encodeURIComponent(selectedAdminAgentId)}/files/${encodeURIComponent(filename)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: document.getElementById('agent-core-editor').value, revision: selectedCoreRevision }),
    }).then(async r => ({ ok: r.ok, data: await r.json() })).then(({ ok, data }) => {
        if (!ok || data.status !== 'success') throw new Error(data.message || t('agents_save_failed'));
        selectedCoreRevision = data.revision;
        _paintCoreFileStatus('ok', t('agents_saved'));
    }).catch(err => {
        _paintCoreFileStatus('error', err.message);
    }).finally(() => {
        if (btn) btn.disabled = false;
    });
}

function startChatWithAgent(agentId) {
    if (!agentId) return;
    activeAgentId = agentId;
    localStorage.setItem('cow_active_agent', activeAgentId);
    newChat(true);
    navigateTo('chat');
    renderComposerIdentity();
}

function conversationHasMessages() {
    return !!document.querySelector('#chat-messages .user-message-group, #chat-messages .bot-message-group');
}

/** A roster of one behaves exactly like the console did before Agents existed:
 *  no face on the composer, no faces in the session list, no @ mentions. */
function multiAgentMode() {
    return enabledAgents().length > 1;
}

/** True once this conversation holds more than its owner. Until then it is an
 *  ordinary chat and is drawn like one. */
function sharedConversation() {
    return multiAgentMode() && currentTeamIds().length > 0;
}
```
</details>

### symbol `startChatWithAgent` (console lines 4112–4173, base lines 2798–2797)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
        focusChatComposer();
    } catch (err) {
        if (_agentStartInFlight === attempt && attempt.context === _wbContext()) {
            showAgentStartNotice('agent_start_failed');
        }
    } finally {
        if (_agentStartInFlight === attempt) cancelAgentStart();
        if (currentView === 'agent-workbench') renderAgentWorkbench();
    }
}

// Focus the chat input once the fresh conversation is on screen, so the user
// can start typing immediately without an extra click.
function focusChatComposer() {
    const input = document.getElementById('chat-input');
    if (input) {
        requestAnimationFrame(() => { input.focus(); });
    }
}

// When a card's target is no longer usable (archived/disabled/removed), refresh
// the workbench list and surface a short notice instead of silently reusing a
// default Agent or a stale card.
function refreshWorkbenchAfterUnavailable(agentId, reason) {
    showAgentStartNotice(reason === 'permission_denied'
        ? 'agent_permission_denied'
        : reason === 'runtime_not_enabled'
            ? 'agent_runtime_not_enabled'
            : 'agent_target_unavailable');
}

function showAgentStartNotice(key) {
    _wbNoticeKey = key;
    if (currentView === 'agents') {
        const status = document.getElementById('agent-profile-status');
        if (status) {
            status.textContent = t(key);
            status.classList.remove('opacity-0', 'agent-status-ok');
        }
    } else if (currentView === 'chat') {
```
</details>

<details><summary>upstream's version of `startChatWithAgent`</summary>

```javascript
function startChatWithAgent(agentId) {
    if (!agentId) return;
    activeAgentId = agentId;
    localStorage.setItem('cow_active_agent', activeAgentId);
    newChat(true);
    navigateTo('chat');
    renderComposerIdentity();
}

function conversationHasMessages() {
    return !!document.querySelector('#chat-messages .user-message-group, #chat-messages .bot-message-group');
}

/** A roster of one behaves exactly like the console did before Agents existed:
 *  no face on the composer, no faces in the session list, no @ mentions. */
function multiAgentMode() {
    return enabledAgents().length > 1;
}

/** True once this conversation holds more than its owner. Until then it is an
 *  ordinary chat and is drawn like one. */
function sharedConversation() {
    return multiAgentMode() && currentTeamIds().length > 0;
}

// Who is answering each in-flight request, as reported when it was accepted.
// Lets a streaming bubble carry the right name before anything is persisted.
const _liveSpeakers = {};

function rememberLiveSpeaker(data) {
    if (data && data.request_id && data.speaker) {
        _liveSpeakers[data.request_id] = data.speaker;
    }
}

/** Repaint a still-visible loading indicator with the resolved speaker's face,
 *  once /message has said who took the turn. No-op if streaming already
 *  replaced the dots with a bubble. */
function setLoadingSpeaker(loadingEl, requestId) {
    if (!loadingEl || !loadingEl.isConnected) return;
    const face = loadingEl.querySelector('.bot-face');
    if (face) face.innerHTML = agentAvatarHTML(liveSpeakerAgent(requestId), 32);
}

/** The Agent to draw on a reply, or null to keep the product's own face. */
```
</details>

### symbol `renderComposerIdentity` (console lines 4289–4290, base lines 2913–2912)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    const agent = findAgent(activeAgentId) || { id: activeAgentId || defaultAgentId, name: activeAgentId || 'Agent' };
    paintChatAgentIdentity(agent);
```
</details>

<details><summary>upstream's version of `renderComposerIdentity`</summary>

```javascript
function renderComposerIdentity() {
    const wrap = document.getElementById('composer-identity');
    const btn = document.getElementById('composer-agent-btn');
    if (!wrap || !btn) return;
    // A single-Agent install keeps the composer exactly as it always was: no
    // avatar, no menu. The identity chip only appears once there is more than
    // one Agent and thus an actual choice to make.
    if (!multiAgentMode()) {
        wrap.classList.add('hidden');
        document.getElementById('composer-agent-menu')?.classList.add('hidden');
        return;
    }
    wrap.classList.remove('hidden');
    const agent = findAgent(activeAgentId) || { id: activeAgentId || defaultAgentId, name: activeAgentId || 'Agent' };
    const others = currentTeamIds().length;
    btn.innerHTML = agentAvatarHTML(agent, 22)
        + (others ? `<span class="composer-agent-count">${others + 1}</span>` : '');
    const face = btn.querySelector('.agent-avatar');
    if (face) face.id = 'composer-agent-avatar';
    // The owner can only be swapped before the first turn, but joining is
    // allowed at any point, so the button itself never goes dead.
    btn.classList.toggle('locked', conversationHasMessages());
    btn.dataset.tooltip = agent.name || agent.id;
}

function toggleComposerAgentMenu(event) {
    event.stopPropagation();
    const menu = document.getElementById('composer-agent-menu');
    if (!menu) return;
    if (!menu.classList.contains('hidden')) {
        menu.classList.add('hidden');
        return;
    }
    _closeComposerMenus(menu);
    renderComposerAgentMenu();
    menu.classList.remove('hidden');
}

/** Paint the agent menu's body from the current roster / team. Kept separate
 *  from the open/close toggle so an invite or removal can refresh the list in
 *  place — the menu stays open, the +/× flips, and the user can keep going. */
function renderComposerAgentMenu() {
    const menu = document.getElementById('composer-agent-menu');
    if (!menu) return;
    const taken = new Set(currentTeamIds());
```
</details>

### symbol `members` (console lines 4344–4344, base lines 2968–2968)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
            + availableChatAgents().map(agent => `
```
</details>

<details><summary>upstream's version of `members`</summary>

```javascript
    const members = (_sessCfg && _sessCfg.team && _sessCfg.team.members) || [];
    const sections = [];

    // A solo chat only shows who it is talking to right now — the current
    // Agent, and just that one. Switching to a different Agent (which would
    // silently start a fresh conversation) was more confusing than useful, so
    // the roster is gone; adding teammates below is how you bring others in.
    if (!sharedConversation()) {
        const current = findAgent(activeAgentId)
            || { id: activeAgentId, name: activeAgentId };
        sections.push(
            `<div class="composer-menu-title">${escapeHtml(t('composer_current_agent'))}</div>`
            + `<div class="composer-menu-item agent-row current">
                    ${agentAvatarHTML(current, 24)}
                    <span>${escapeHtml(current.name || current.id)}</span>
                    <i class="fas fa-check ml-auto text-[11px]"></i>
                </div>`
        );
    }

    const candidates = enabledAgents().filter(a => a.id !== activeAgentId && !taken.has(a.id));

    // A group chat lists everyone in the conversation, host first. The host is
    // the main Agent (owner) and is shown with a "main Agent" badge and no
    // remove control — it cannot be dropped from its own conversation. The
    // teammates below it are removable. A separate section further down offers
    // who can still be pulled in.
    if (sharedConversation()) {
        const owner = findAgent(activeAgentId)
            || { id: activeAgentId, name: activeAgentId };
        const ownerRow = `
            <div class="composer-menu-item agent-row joined is-owner">
                ${agentAvatarHTML(owner, 24)}
                <span>${escapeHtml(owner.name || owner.id)}</span>
                <span class="composer-menu-badge owner-badge ml-auto">${escapeHtml(t('composer_agent_owner'))}</span>
            </div>`;
        const joined = members.filter(m => m.id !== activeAgentId).map(m => `
            <button type="button" class="composer-menu-item agent-row joined"
                    onclick="removeTeamMember('${escapeHtml(m.id)}')" title="${escapeHtml(t('team_remove'))}">
                ${agentAvatarHTML(m, 24)}
                <span>${escapeHtml(m.name || m.id)}</span>
                <i class="fas fa-check ml-auto text-[11px] joined-check"></i>
                <i class="fas fa-xmark ml-auto text-[11px] joined-remove"></i>
            </button>`).join('');
        sections.push(
```
</details>

### symbol `members` (console lines 4354–4354, base lines 2978–2978)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    const candidates = availableChatAgents().filter(a => a.id !== activeAgentId && !taken.has(a.id));
```
</details>

<details><summary>upstream's version of `members`</summary>

```javascript
    const members = (_sessCfg && _sessCfg.team && _sessCfg.team.members) || [];
    const sections = [];

    // A solo chat only shows who it is talking to right now — the current
    // Agent, and just that one. Switching to a different Agent (which would
    // silently start a fresh conversation) was more confusing than useful, so
    // the roster is gone; adding teammates below is how you bring others in.
    if (!sharedConversation()) {
        const current = findAgent(activeAgentId)
            || { id: activeAgentId, name: activeAgentId };
        sections.push(
            `<div class="composer-menu-title">${escapeHtml(t('composer_current_agent'))}</div>`
            + `<div class="composer-menu-item agent-row current">
                    ${agentAvatarHTML(current, 24)}
                    <span>${escapeHtml(current.name || current.id)}</span>
                    <i class="fas fa-check ml-auto text-[11px]"></i>
                </div>`
        );
    }

    const candidates = enabledAgents().filter(a => a.id !== activeAgentId && !taken.has(a.id));

    // A group chat lists everyone in the conversation, host first. The host is
    // the main Agent (owner) and is shown with a "main Agent" badge and no
    // remove control — it cannot be dropped from its own conversation. The
    // teammates below it are removable. A separate section further down offers
    // who can still be pulled in.
    if (sharedConversation()) {
        const owner = findAgent(activeAgentId)
            || { id: activeAgentId, name: activeAgentId };
        const ownerRow = `
            <div class="composer-menu-item agent-row joined is-owner">
                ${agentAvatarHTML(owner, 24)}
                <span>${escapeHtml(owner.name || owner.id)}</span>
                <span class="composer-menu-badge owner-badge ml-auto">${escapeHtml(t('composer_agent_owner'))}</span>
            </div>`;
        const joined = members.filter(m => m.id !== activeAgentId).map(m => `
            <button type="button" class="composer-menu-item agent-row joined"
                    onclick="removeTeamMember('${escapeHtml(m.id)}')" title="${escapeHtml(t('team_remove'))}">
                ${agentAvatarHTML(m, 24)}
                <span>${escapeHtml(m.name || m.id)}</span>
                <i class="fas fa-check ml-auto text-[11px] joined-check"></i>
                <i class="fas fa-xmark ml-auto text-[11px] joined-remove"></i>
            </button>`).join('');
        sections.push(
```
</details>

### symbol `setTeamMembers` (console lines 4474–4474, base lines 3102–3102)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
            _sessCfg = { model: data.model, team: data.team };
```
</details>

<details><summary>upstream's version of `setTeamMembers`</summary>

```javascript
function setTeamMembers(ids) {
    const unique = Array.from(new Set(ids.filter(id => id && id !== activeAgentId)));
    return fetch(`/api/sessions/${encodeURIComponent(sessionId)}/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ members: unique.length ? unique : null }),
    }).then(r => r.json()).then(data => {
        if (data.status === 'success') {
            _sessCfg = { model: data.model, permission: data.permission, team: data.team };
            renderComposerIdentity();
            // Inviting or removing someone changes whether one model can speak
            // for this conversation, and whether @ can address an Agent.
            _renderModelChip();
            _renderInputPlaceholder();
        }
    });
}

function addTeamMember(agentId) {
    if (!agentId || agentId === activeAgentId) return Promise.resolve();
    const ids = currentTeamIds();
    if (ids.includes(agentId)) return Promise.resolve();
    return setTeamMembers([...ids, agentId]);
}

function removeTeamMember(agentId) {
    return setTeamMembers(currentTeamIds().filter(id => id !== agentId))
        .then(refreshComposerAgentMenuIfOpen);
}

/** Repaint the agent menu if it is still open, so add/remove show immediately. */
function refreshComposerAgentMenuIfOpen() {
    const menu = document.getElementById('composer-agent-menu');
    if (menu && !menu.classList.contains('hidden')) renderComposerAgentMenu();
}

async function syncTeamFromText(text) {
    const extra = mentionedAgentIds(text);
    if (!extra.length) return;
    await setTeamMembers([...currentTeamIds(), ...extra]);
}

// Point a channel instance at an Agent. Binding lives on the instance itself
// (channel_instances[].agent_id); an empty agentId means "follow the default
// Agent". instanceId defaults to the channel type for a single-instance channel.
```
</details>

### symbol `selectMemoryAgent` (console lines 4610–4609, base lines 3202–3203)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
```
</details>

<details><summary>upstream's version of `selectMemoryAgent`</summary>

```javascript
function selectMemoryAgent(agentId) {
    memoryAgentId = agentId;
    localStorage.setItem('cow_memory_agent', agentId);
    closeMemoryViewer();
    loadMemoryView(1);
}

loadAgentCatalog();

```
</details>

## `channel/web/static/js/views/models.js` — 11 region(s)

### symbol `openSearchAddProviderPicker` (console lines 13512–13518, base lines 10015–10014)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
// Search providers that own a dedicated credential — an API key, or SearXNG's
// instance URL. The rest (zhipu/qianfan/linkai) reuse a model-vendor
// credential and keep the vendor modal. Mirrors the backend's
// `needs_dedicated_key` / `needs_url` flags in ModelsHandler._search_capability,
// so a provider the runtime supports can always be configured here.
const DEDICATED_SEARCH_CREDENTIALS = ['bocha', 'anysearch', 'serply', 'tavily', 'searxng', 'keenable'];

```
</details>

<details><summary>upstream's version of `openSearchAddProviderPicker`</summary>

```javascript
function openSearchAddProviderPicker(missingProviders) {
    if (!missingProviders || missingProviders.length === 0) return;
    if (missingProviders.length === 1) {
        _launchSearchProviderConfig(missingProviders[0].id);
        return;
    }

    const existing = document.getElementById('search-add-modal');
    if (existing) existing.remove();

    const rows = missingProviders.map(p => `
        <button type="button" data-pid="${p.id}"
                class="w-full flex items-center justify-between px-3 py-2.5 rounded-lg cursor-pointer
                       bg-slate-50 dark:bg-white/5 hover:bg-slate-100 dark:hover:bg-white/10
                       text-sm text-slate-700 dark:text-slate-200 transition-colors">
            <span>${escapeHtml(localizedLabel(p.label))}</span>
            <i class="fas fa-chevron-right text-[10px] text-slate-400"></i>
        </button>
    `).join('');

    const modal = document.createElement('div');
    modal.id = 'search-add-modal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm';
    modal.innerHTML = `
        <div class="bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10
                    w-full max-w-md mx-4 p-6 shadow-xl">
            <h3 class="text-lg font-semibold text-slate-800 dark:text-slate-100 mb-1">${t('models_search_add_provider')}</h3>
            <p class="text-xs text-slate-500 dark:text-slate-400 mb-4">${t('models_search_add_desc')}</p>
            <div class="space-y-2">${rows}</div>
            <div class="flex items-center justify-end mt-5">
                <button type="button" onclick="document.getElementById('search-add-modal').remove()"
                        class="px-3 py-1.5 rounded-md text-sm text-slate-600 dark:text-slate-300
                               hover:bg-slate-100 dark:hover:bg-white/5 transition-colors">
                    ${t('cancel')}
                </button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    modal.querySelectorAll('[data-pid]').forEach(el => {
        el.addEventListener('click', () => {
            const pid = el.getAttribute('data-pid');
            modal.remove();
            _launchSearchProviderConfig(pid);
        });
```
</details>

### symbol `_launchSearchProviderConfig` (console lines 13520–13520, base lines 10016–10016)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    if (DEDICATED_SEARCH_CREDENTIALS.indexOf(providerId) !== -1) {
```
</details>

<details><summary>upstream's version of `_launchSearchProviderConfig`</summary>

```javascript
function _launchSearchProviderConfig(providerId, providerMeta) {
    // Providers that hold their own credential (dedicated key or, for SearXNG,
    // an instance URL) use the bespoke search-key modal. zhipu/qianfan/linkai
    // reuse a model-vendor key and go through the vendor modal instead.
    if (['bocha', 'anysearch', 'serply', 'tavily', 'searxng', 'keenable'].includes(providerId)) {
        openSearchKeyModal(providerId, providerMeta);
    } else {
        openVendorModal(providerId, () => loadModelsView({ preserveScroll: true }));
    }
}


function saveSearchCapability() {
    const strategyDd = document.getElementById('cap-search-strategy');
    const providerDd = document.getElementById('cap-search-provider');
    // 如果策略下拉框的值是空（待配置），默认使用 'auto'
    const strategy = strategyDd ? (getDropdownValue(strategyDd) || 'auto') : 'auto';
    const provider = (strategy === 'fixed' && providerDd) ? getDropdownValue(providerDd) : '';

    fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            action: 'set_capability',
            capability: 'search',
            strategy,
            provider,
        }),
    }).then(r => r.json()).then(data => {
        if (data.status === 'success') {
            showStatus('cap-search-status', 'models_save_success', false);
            setTimeout(() => loadModelsView({ preserveScroll: true }), 400);
        } else {
            console.log('[saveSearchCapability] Error:', data.message);
            showStatus('cap-search-status', 'models_save_failed', true);
        }
    }).catch(() => showStatus('cap-search-status', 'models_save_failed', true));
}


// Minimal bocha API-key modal. Reuses the existing vendor-modal markup
// helpers would be nice, but bocha isn't in PROVIDER_MODELS (it's not a
// model vendor), so we render a tiny dedicated dialog.
// For search vendors that hold their own keys.

```
</details>

### symbol `searchCap` (console lines 13561–13560, base lines 10057–10058)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
```
</details>

<details><summary>upstream's version of `searchCap`</summary>

```javascript
        const searchCap = (modelsState && modelsState.capabilities && modelsState.capabilities.search) || {};
    const prov = (searchCap.providers || []).find(p => p.id === providerId);
    const isSearxng = providerId === 'searxng';
    // SearXNG holds an instance URL (echoed back verbatim in url_masked); the
    // rest hold a masked API key. Resolve whichever applies as the field value.
    let masked;
    if (isSearxng) {
        masked = (providerMeta && providerMeta.url_masked) || (prov && prov.url_masked) || '';
    } else {
        masked = (providerMeta && providerMeta.api_key_masked) || '';
        if (!masked && prov && prov.api_key_masked) masked = prov.api_key_masked;
    }
    // SearXNG URL is not masked, so it's safe to keep editable (not a sentinel).
    const hasKey = !!masked;
    const isAnonymous = (providerId === 'anysearch' || providerId === 'keenable')
        && !!((providerMeta && providerMeta.anonymous) || (prov && prov.anonymous));
    const clearBtnHtml = (hasKey || isAnonymous)
        ? `<button type="button" id="search-key-clear"
                  class="px-3 py-1.5 rounded-md text-xs text-red-500 dark:text-red-400
                         hover:bg-red-50 dark:hover:bg-red-900/20 cursor-pointer transition-colors">
              ${t('models_clear_credential')}
           </button>`
        : '';
    let descText = t('models_search_' + providerId + '_desc');
    if (providerId === 'anysearch') {
        const hint = currentLang === 'zh'
            ? '（留空可启用匿名模式，每日有免费额度）'
            : '(Leave blank to enable anonymous mode with daily free quota)';
        descText = descText + ' ' + hint;
    }
    const modal = document.createElement('div');
    modal.id = 'search-key-modal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm';
    modal.innerHTML = `
        <div id="search-key-modal-card"
             class="bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10
                    w-full max-w-md mx-4 p-6 shadow-xl">
            <h3 class="text-lg font-semibold text-slate-800 dark:text-slate-100 mb-1">${t('models_search_' + providerId + '_title')}</h3>
            <p class="text-xs text-slate-500 dark:text-slate-400 mb-4">${descText}</p>
            <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${providerId === 'searxng' ? 'Instance URL' : 'API Key'}</label>
            <input id="search-key-input" type="text" autocomplete="off" data-1p-ignore data-lpignore="true"
                   class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-600
                          bg-slate-50 dark:bg-white/5 text-sm text-slate-800 dark:text-slate-100
                          focus:outline-none focus:border-primary-500 ${isSearxng ? '' : 'font-mono'} ${(hasKey && !isSearxng) ? 'cfg-key-masked' : ''}"
                   value="${escapeHtml(masked)}"
```
</details>

### symbol `provider` (console lines 13562–13571, base lines 10060–10061)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    const provider = (searchCap.providers || []).find(p => p.id === providerId);
    const isSearxng = providerId === 'searxng';
    // SearXNG holds an instance URL (echoed back verbatim in url_masked); the
    // rest hold a masked API key. Resolve whichever applies as the field value.
    let masked;
    if (isSearxng) {
        masked = (providerMeta && providerMeta.url_masked) || (provider && provider.url_masked) || '';
    } else {
        masked = (providerMeta && providerMeta.api_key_masked) || '';
        if (!masked && provider && provider.api_key_masked) masked = provider.api_key_masked;
```
</details>

<details><summary>upstream's version of `provider`</summary>

```javascript
    const provider = (strategy === 'fixed' && providerDd) ? getDropdownValue(providerDd) : '';

    fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            action: 'set_capability',
            capability: 'search',
            strategy,
            provider,
        }),
    }).then(r => r.json()).then(data => {
        if (data.status === 'success') {
            showStatus('cap-search-status', 'models_save_success', false);
            setTimeout(() => loadModelsView({ preserveScroll: true }), 400);
        } else {
            console.log('[saveSearchCapability] Error:', data.message);
            showStatus('cap-search-status', 'models_save_failed', true);
        }
    }).catch(() => showStatus('cap-search-status', 'models_save_failed', true));
}


// Minimal bocha API-key modal. Reuses the existing vendor-modal markup
// helpers would be nice, but bocha isn't in PROVIDER_MODELS (it's not a
// model vendor), so we render a tiny dedicated dialog.
// For search vendors that hold their own keys.


function openSearchKeyModal(providerId, providerMeta) {
    const existing = document.getElementById('search-key-modal');
    if (existing) existing.remove();

        const searchCap = (modelsState && modelsState.capabilities && modelsState.capabilities.search) || {};
    const prov = (searchCap.providers || []).find(p => p.id === providerId);
    const isSearxng = providerId === 'searxng';
    // SearXNG holds an instance URL (echoed back verbatim in url_masked); the
    // rest hold a masked API key. Resolve whichever applies as the field value.
    let masked;
    if (isSearxng) {
        masked = (providerMeta && providerMeta.url_masked) || (prov && prov.url_masked) || '';
    } else {
        masked = (providerMeta && providerMeta.api_key_masked) || '';
        if (!masked && prov && prov.api_key_masked) masked = prov.api_key_masked;
    }
```
</details>

### symbol `masked` (console lines 13574–13578, base lines 10064–10064)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    // anysearch/keenable can be on without a key (their keyless tier), and that
    // state still needs the clear button so it can be turned back off.
    const isAnonymous = (providerId === 'anysearch' || providerId === 'keenable')
        && !!((providerMeta && providerMeta.anonymous) || (provider && provider.anonymous));
    const clearBtnHtml = (hasKey || isAnonymous)
```
</details>

<details><summary>upstream's version of `masked`</summary>

```javascript
        masked = (providerMeta && providerMeta.url_masked) || (prov && prov.url_masked) || '';
    } else {
        masked = (providerMeta && providerMeta.api_key_masked) || '';
        if (!masked && prov && prov.api_key_masked) masked = prov.api_key_masked;
    }
    // SearXNG URL is not masked, so it's safe to keep editable (not a sentinel).
    const hasKey = !!masked;
    const isAnonymous = (providerId === 'anysearch' || providerId === 'keenable')
        && !!((providerMeta && providerMeta.anonymous) || (prov && prov.anonymous));
    const clearBtnHtml = (hasKey || isAnonymous)
        ? `<button type="button" id="search-key-clear"
                  class="px-3 py-1.5 rounded-md text-xs text-red-500 dark:text-red-400
                         hover:bg-red-50 dark:hover:bg-red-900/20 cursor-pointer transition-colors">
              ${t('models_clear_credential')}
           </button>`
        : '';
    let descText = t('models_search_' + providerId + '_desc');
    if (providerId === 'anysearch') {
        const hint = currentLang === 'zh'
            ? '（留空可启用匿名模式，每日有免费额度）'
            : '(Leave blank to enable anonymous mode with daily free quota)';
        descText = descText + ' ' + hint;
    }
    const modal = document.createElement('div');
    modal.id = 'search-key-modal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm';
    modal.innerHTML = `
        <div id="search-key-modal-card"
             class="bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10
                    w-full max-w-md mx-4 p-6 shadow-xl">
            <h3 class="text-lg font-semibold text-slate-800 dark:text-slate-100 mb-1">${t('models_search_' + providerId + '_title')}</h3>
            <p class="text-xs text-slate-500 dark:text-slate-400 mb-4">${descText}</p>
            <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${providerId === 'searxng' ? 'Instance URL' : 'API Key'}</label>
            <input id="search-key-input" type="text" autocomplete="off" data-1p-ignore data-lpignore="true"
                   class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-600
                          bg-slate-50 dark:bg-white/5 text-sm text-slate-800 dark:text-slate-100
                          focus:outline-none focus:border-primary-500 ${isSearxng ? '' : 'font-mono'} ${(hasKey && !isSearxng) ? 'cfg-key-masked' : ''}"
                   value="${escapeHtml(masked)}"
                   data-masked="${(hasKey && !isSearxng) ? '1' : ''}"
                   placeholder="${isSearxng ? 'https://searxng.example.com' : 'sk-...'}" />
            <div class="flex items-center justify-between gap-3 mt-5">
                <div>${clearBtnHtml}</div>
                <div class="flex items-center gap-3">
                    <button type="button" onclick="document.getElementById('search-key-modal').remove()"
                            class="px-3 py-1.5 rounded-md text-sm text-slate-600 dark:text-slate-300
```
</details>

### symbol `clearBtnHtml` (console lines 13585–13588, base lines 10071–10070)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    // Saving empty on anysearch turns its keyless tier on, so say so.
    const descText = providerId === 'anysearch'
        ? t('models_search_anysearch_desc') + ' ' + t('models_search_anysearch_anon_hint')
        : t('models_search_' + providerId + '_desc');
```
</details>

<details><summary>upstream's version of `clearBtnHtml`</summary>

```javascript
    const clearBtnHtml = (hasKey || isAnonymous)
        ? `<button type="button" id="search-key-clear"
                  class="px-3 py-1.5 rounded-md text-xs text-red-500 dark:text-red-400
                         hover:bg-red-50 dark:hover:bg-red-900/20 cursor-pointer transition-colors">
              ${t('models_clear_credential')}
           </button>`
        : '';
    let descText = t('models_search_' + providerId + '_desc');
    if (providerId === 'anysearch') {
        const hint = currentLang === 'zh'
            ? '（留空可启用匿名模式，每日有免费额度）'
            : '(Leave blank to enable anonymous mode with daily free quota)';
        descText = descText + ' ' + hint;
    }
    const modal = document.createElement('div');
    modal.id = 'search-key-modal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm';
    modal.innerHTML = `
        <div id="search-key-modal-card"
             class="bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10
                    w-full max-w-md mx-4 p-6 shadow-xl">
            <h3 class="text-lg font-semibold text-slate-800 dark:text-slate-100 mb-1">${t('models_search_' + providerId + '_title')}</h3>
            <p class="text-xs text-slate-500 dark:text-slate-400 mb-4">${descText}</p>
            <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${providerId === 'searxng' ? 'Instance URL' : 'API Key'}</label>
            <input id="search-key-input" type="text" autocomplete="off" data-1p-ignore data-lpignore="true"
                   class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-600
                          bg-slate-50 dark:bg-white/5 text-sm text-slate-800 dark:text-slate-100
                          focus:outline-none focus:border-primary-500 ${isSearxng ? '' : 'font-mono'} ${(hasKey && !isSearxng) ? 'cfg-key-masked' : ''}"
                   value="${escapeHtml(masked)}"
                   data-masked="${(hasKey && !isSearxng) ? '1' : ''}"
                   placeholder="${isSearxng ? 'https://searxng.example.com' : 'sk-...'}" />
            <div class="flex items-center justify-between gap-3 mt-5">
                <div>${clearBtnHtml}</div>
                <div class="flex items-center gap-3">
                    <button type="button" onclick="document.getElementById('search-key-modal').remove()"
                            class="px-3 py-1.5 rounded-md text-sm text-slate-600 dark:text-slate-300
                                   hover:bg-slate-100 dark:hover:bg-white/5 transition-colors">
                        ${t('cancel')}
                    </button>
                    <button type="button" onclick="_saveSearchKey('${providerId}')"
                            class="px-4 py-1.5 rounded-md bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium
                                   cursor-pointer transition-colors">
                        ${t('save')}
                    </button>
                </div>
```
</details>

### symbol `clearBtnHtml` (console lines 13598–13599, base lines 10080–10081)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
            <p class="text-xs text-slate-500 dark:text-slate-400 mb-4">${descText}</p>
            <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${isSearxng ? 'Instance URL' : 'API Key'}</label>
```
</details>

<details><summary>upstream's version of `clearBtnHtml`</summary>

```javascript
    const clearBtnHtml = (hasKey || isAnonymous)
        ? `<button type="button" id="search-key-clear"
                  class="px-3 py-1.5 rounded-md text-xs text-red-500 dark:text-red-400
                         hover:bg-red-50 dark:hover:bg-red-900/20 cursor-pointer transition-colors">
              ${t('models_clear_credential')}
           </button>`
        : '';
    let descText = t('models_search_' + providerId + '_desc');
    if (providerId === 'anysearch') {
        const hint = currentLang === 'zh'
            ? '（留空可启用匿名模式，每日有免费额度）'
            : '(Leave blank to enable anonymous mode with daily free quota)';
        descText = descText + ' ' + hint;
    }
    const modal = document.createElement('div');
    modal.id = 'search-key-modal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm';
    modal.innerHTML = `
        <div id="search-key-modal-card"
             class="bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10
                    w-full max-w-md mx-4 p-6 shadow-xl">
            <h3 class="text-lg font-semibold text-slate-800 dark:text-slate-100 mb-1">${t('models_search_' + providerId + '_title')}</h3>
            <p class="text-xs text-slate-500 dark:text-slate-400 mb-4">${descText}</p>
            <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${providerId === 'searxng' ? 'Instance URL' : 'API Key'}</label>
            <input id="search-key-input" type="text" autocomplete="off" data-1p-ignore data-lpignore="true"
                   class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-600
                          bg-slate-50 dark:bg-white/5 text-sm text-slate-800 dark:text-slate-100
                          focus:outline-none focus:border-primary-500 ${isSearxng ? '' : 'font-mono'} ${(hasKey && !isSearxng) ? 'cfg-key-masked' : ''}"
                   value="${escapeHtml(masked)}"
                   data-masked="${(hasKey && !isSearxng) ? '1' : ''}"
                   placeholder="${isSearxng ? 'https://searxng.example.com' : 'sk-...'}" />
            <div class="flex items-center justify-between gap-3 mt-5">
                <div>${clearBtnHtml}</div>
                <div class="flex items-center gap-3">
                    <button type="button" onclick="document.getElementById('search-key-modal').remove()"
                            class="px-3 py-1.5 rounded-md text-sm text-slate-600 dark:text-slate-300
                                   hover:bg-slate-100 dark:hover:bg-white/5 transition-colors">
                        ${t('cancel')}
                    </button>
                    <button type="button" onclick="_saveSearchKey('${providerId}')"
                            class="px-4 py-1.5 rounded-md bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium
                                   cursor-pointer transition-colors">
                        ${t('save')}
                    </button>
                </div>
```
</details>

### symbol `clearBtnHtml` (console lines 13603–13603, base lines 10085–10085)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
                          focus:outline-none focus:border-primary-500 ${isSearxng ? '' : 'font-mono'} ${(hasKey && !isSearxng) ? 'cfg-key-masked' : ''}"
```
</details>

<details><summary>upstream's version of `clearBtnHtml`</summary>

```javascript
    const clearBtnHtml = (hasKey || isAnonymous)
        ? `<button type="button" id="search-key-clear"
                  class="px-3 py-1.5 rounded-md text-xs text-red-500 dark:text-red-400
                         hover:bg-red-50 dark:hover:bg-red-900/20 cursor-pointer transition-colors">
              ${t('models_clear_credential')}
           </button>`
        : '';
    let descText = t('models_search_' + providerId + '_desc');
    if (providerId === 'anysearch') {
        const hint = currentLang === 'zh'
            ? '（留空可启用匿名模式，每日有免费额度）'
            : '(Leave blank to enable anonymous mode with daily free quota)';
        descText = descText + ' ' + hint;
    }
    const modal = document.createElement('div');
    modal.id = 'search-key-modal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm';
    modal.innerHTML = `
        <div id="search-key-modal-card"
             class="bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10
                    w-full max-w-md mx-4 p-6 shadow-xl">
            <h3 class="text-lg font-semibold text-slate-800 dark:text-slate-100 mb-1">${t('models_search_' + providerId + '_title')}</h3>
            <p class="text-xs text-slate-500 dark:text-slate-400 mb-4">${descText}</p>
            <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${providerId === 'searxng' ? 'Instance URL' : 'API Key'}</label>
            <input id="search-key-input" type="text" autocomplete="off" data-1p-ignore data-lpignore="true"
                   class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-600
                          bg-slate-50 dark:bg-white/5 text-sm text-slate-800 dark:text-slate-100
                          focus:outline-none focus:border-primary-500 ${isSearxng ? '' : 'font-mono'} ${(hasKey && !isSearxng) ? 'cfg-key-masked' : ''}"
                   value="${escapeHtml(masked)}"
                   data-masked="${(hasKey && !isSearxng) ? '1' : ''}"
                   placeholder="${isSearxng ? 'https://searxng.example.com' : 'sk-...'}" />
            <div class="flex items-center justify-between gap-3 mt-5">
                <div>${clearBtnHtml}</div>
                <div class="flex items-center gap-3">
                    <button type="button" onclick="document.getElementById('search-key-modal').remove()"
                            class="px-3 py-1.5 rounded-md text-sm text-slate-600 dark:text-slate-300
                                   hover:bg-slate-100 dark:hover:bg-white/5 transition-colors">
                        ${t('cancel')}
                    </button>
                    <button type="button" onclick="_saveSearchKey('${providerId}')"
                            class="px-4 py-1.5 rounded-md bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium
                                   cursor-pointer transition-colors">
                        ${t('save')}
                    </button>
                </div>
```
</details>

### symbol `clearBtnHtml` (console lines 13605–13606, base lines 10087–10088)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
                   data-masked="${(hasKey && !isSearxng) ? '1' : ''}"
                   placeholder="${isSearxng ? 'https://searxng.example.com' : 'sk-...'}" />
```
</details>

<details><summary>upstream's version of `clearBtnHtml`</summary>

```javascript
    const clearBtnHtml = (hasKey || isAnonymous)
        ? `<button type="button" id="search-key-clear"
                  class="px-3 py-1.5 rounded-md text-xs text-red-500 dark:text-red-400
                         hover:bg-red-50 dark:hover:bg-red-900/20 cursor-pointer transition-colors">
              ${t('models_clear_credential')}
           </button>`
        : '';
    let descText = t('models_search_' + providerId + '_desc');
    if (providerId === 'anysearch') {
        const hint = currentLang === 'zh'
            ? '（留空可启用匿名模式，每日有免费额度）'
            : '(Leave blank to enable anonymous mode with daily free quota)';
        descText = descText + ' ' + hint;
    }
    const modal = document.createElement('div');
    modal.id = 'search-key-modal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm';
    modal.innerHTML = `
        <div id="search-key-modal-card"
             class="bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10
                    w-full max-w-md mx-4 p-6 shadow-xl">
            <h3 class="text-lg font-semibold text-slate-800 dark:text-slate-100 mb-1">${t('models_search_' + providerId + '_title')}</h3>
            <p class="text-xs text-slate-500 dark:text-slate-400 mb-4">${descText}</p>
            <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${providerId === 'searxng' ? 'Instance URL' : 'API Key'}</label>
            <input id="search-key-input" type="text" autocomplete="off" data-1p-ignore data-lpignore="true"
                   class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-600
                          bg-slate-50 dark:bg-white/5 text-sm text-slate-800 dark:text-slate-100
                          focus:outline-none focus:border-primary-500 ${isSearxng ? '' : 'font-mono'} ${(hasKey && !isSearxng) ? 'cfg-key-masked' : ''}"
                   value="${escapeHtml(masked)}"
                   data-masked="${(hasKey && !isSearxng) ? '1' : ''}"
                   placeholder="${isSearxng ? 'https://searxng.example.com' : 'sk-...'}" />
            <div class="flex items-center justify-between gap-3 mt-5">
                <div>${clearBtnHtml}</div>
                <div class="flex items-center gap-3">
                    <button type="button" onclick="document.getElementById('search-key-modal').remove()"
                            class="px-3 py-1.5 rounded-md text-sm text-slate-600 dark:text-slate-300
                                   hover:bg-slate-100 dark:hover:bg-white/5 transition-colors">
                        ${t('cancel')}
                    </button>
                    <button type="button" onclick="_saveSearchKey('${providerId}')"
                            class="px-4 py-1.5 rounded-md bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium
                                   cursor-pointer transition-colors">
                        ${t('save')}
                    </button>
                </div>
```
</details>

### symbol `_saveSearchKey` (console lines 13669–13691, base lines 10151–10150)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript

    // anysearch and keenable hold a key *or* run on their keyless tier: saving
    // with an empty key is what turns the anonymous mode on.
    if (providerId === 'anysearch' || providerId === 'keenable') {
        _postSearchCredential({
            action: 'set_search_credential',
            provider: providerId,
            api_key: apiKey,
            anonymous: !apiKey,
        });
        return;
    }

    // SearXNG is addressed by an instance URL rather than authenticated by a key.
    if (providerId === 'searxng') {
        if (!apiKey) {
            input.focus();  // empty is a no-op; the clear button empties the URL
            return;
        }
        _postSearchCredential({ action: 'set_search_credential', provider: providerId, url: apiKey });
        return;
    }

```
</details>

<details><summary>upstream's version of `_saveSearchKey`</summary>

```javascript
function _saveSearchKey(providerId) {
    const input = document.getElementById('search-key-input');
    if (!input) return;
    if (input.dataset.masked === '1') {
        const modal = document.getElementById('search-key-modal');
        if (modal) modal.remove();
        return;
    }
    const apiKey = input.value.trim();

    // anysearch and keenable: saving with an empty key enables the anonymous tier.
    if (providerId === 'anysearch' || providerId === 'keenable') {
    fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            action: 'set_search_credential',
            provider: providerId,
            api_key: apiKey,
            anonymous: !apiKey, // ← 字段名必须是 anonymous；留空保存 = 启用匿名（表第 2 行）
        }),
    }).then(r => r.json()).then(data => {
        if (data.status === 'success') {
            const modal = document.getElementById('search-key-modal');
            if (modal) modal.remove();
            loadModelsView({ preserveScroll: true });
        }
    });
    return;
}

    if (providerId === 'searxng') {
        // SearXNG uses an instance URL, not an API key. Empty input is a no-op
        // here (use the clear button to remove it).
    if (!apiKey) {
        input.focus();
        return;
    }
    fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                action: 'set_search_credential',
                provider: providerId,
                url: apiKey, // reuse the input value as the URL
```
</details>

### symbol `_clearSearchKey` (console lines 13715–13721, base lines 10169–10179)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    // SearXNG is cleared by emptying its instance URL, not an API key. For
    // anysearch/keenable an empty key with `anonymous` absent also turns the
    // keyless tier back off, which is what the clear button means there.
    const body = providerId === 'searxng'
        ? { action: 'set_search_credential', provider: providerId, url: '' }
        : { action: 'set_search_credential', provider: providerId, api_key: '' };
    _postSearchCredential(body);
```
</details>

<details><summary>upstream's version of `_clearSearchKey`</summary>

```javascript
function _clearSearchKey(providerId) {
    // SearXNG is cleared by emptying its instance URL, not an API key.
    const payload = (providerId === 'searxng')
        ? { action: 'set_search_credential', provider: providerId, url: '' }
        : { action: 'set_search_credential', provider: providerId, api_key: '' };
    fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    }).then(r => r.json()).then(data => {
        if (data.status === 'success') {
            const modal = document.getElementById('search-key-modal');
            if (modal) modal.remove();
            loadModelsView({ preserveScroll: true });
        }
    });
}

function renderCapabilityBody(def, cap, body) {
    if (def.id === 'search') {
        renderSearchCapability(def, cap, body);
        return;
    }

    // Editable cards: provider dropdown + (optional) model dropdown + save row
    const providerOpts = buildCapabilityProviderOptions(def, cap);
    const providerHtml = `
        <div>
            <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${t('models_provider')}</label>
            <div id="cap-${def.id}-provider" class="cfg-dropdown" tabindex="0">
                <div class="cfg-dropdown-selected">
                    <span class="cfg-dropdown-text">--</span>
                    <i class="fas fa-chevron-down cfg-dropdown-arrow"></i>
                </div>
                <div class="cfg-dropdown-menu"></div>
            </div>
        </div>`;

    // The model-picker container is always emitted so the provider-change
    // handler can show/hide it; for `auto` capabilities it starts hidden and
    // gets toggled by setCapabilityModelPickerVisible.
    const modelHtml = def.needsModel ? `
        <div id="cap-${def.id}-model-wrap">
            <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${t('models_model')}</label>
            <div id="cap-${def.id}-model" class="cfg-dropdown" tabindex="0">
```
</details>

## `channel/web/static/js/core/nav.js` — 9 region(s)

### symbol `initTaskNotifyToggles` (console lines 1494–1508, base lines 1717–1717)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
// =====================================================================
// Console view registry (change fork-decoupling-and-tenant-hardening, task 8.6)
// =====================================================================
// Fork-owned view modules (identity-admin.js, todos.js) register
// { id, label, load, repaint } here instead of console.js hard-coding their
// loaders in navigation dispatch. console.js iterates this registry, so the
// upstream core file no longer carries fork-only view branches.
const CONSOLE_VIEW_REGISTRY = [];
function registerConsoleView(spec) {
    if (!spec || !spec.id) return;
    const index = CONSOLE_VIEW_REGISTRY.findIndex(entry => entry.id === spec.id);
    if (index >= 0) CONSOLE_VIEW_REGISTRY[index] = spec;
    else CONSOLE_VIEW_REGISTRY.push(spec);
}
window.registerConsoleView = registerConsoleView;
```
</details>

### symbol `_registeredConsoleView` (console lines 1510–1519, base lines 1719–1720)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
function _registeredConsoleView(viewId) {
    return CONSOLE_VIEW_REGISTRY.find(entry => entry.id === viewId) || null;
}

// Apply the shell's "exactly one view is active" rule to a view id. Split out
// because a registered view creates its container lazily inside its own loader:
// ``navigateTo`` runs the toggle before the container exists, so the loader has
// to be able to re-apply the state once the markup is there (otherwise a
// fork-owned view renders hidden).
function _activateViewContainer(viewId) {
```
</details>

### symbol `showUnavailableView` (console lines 1827–1984, base lines 1727–1726)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    // A denied or unavailable target keeps no current marker on the account panel
    // either: the account panel no longer claims a current item.
    document.getElementById('breadcrumb-group').textContent = t('nav_system');
    document.getElementById('breadcrumb-group').dataset.i18n = 'nav_system';
    const pageKey = denied ? 'nav_denied' : 'nav_unavailable';
    document.getElementById('breadcrumb-page').textContent = t(pageKey);
    document.getElementById('breadcrumb-page').dataset.i18n = pageKey;
    // Swap the title/hint copy and icon for the denied case.
    const title = document.getElementById('nav-unavailable-title');
    const hint = document.getElementById('nav-unavailable-hint');
    const icon = target.querySelector('i.fas');
    if (denied) {
        if (title) { title.textContent = t('nav_denied'); title.dataset.i18n = 'nav_denied'; }
        if (hint) { hint.textContent = t('nav_denied_hint'); hint.dataset.i18n = 'nav_denied_hint'; }
        if (icon) icon.classList.replace('fa-hourglass-half', 'fa-lock');
    } else {
        if (title) { title.textContent = t('nav_unavailable'); title.dataset.i18n = 'nav_unavailable'; }
        if (hint) { hint.textContent = t('nav_unavailable_hint'); hint.dataset.i18n = 'nav_unavailable_hint'; }
        if (icon) icon.classList.replace('fa-lock', 'fa-hourglass-half');
    }
    document.getElementById('chat-agent-identity')?.classList.toggle('hidden', true);
    document.getElementById('workspace-toggle-btn')?.classList.toggle('hidden', true);
    if (window.innerWidth < 1024) closeSidebar();
}

// === ACCOUNT_PERSONAL_NAV_BEGIN ===
// The shared protected navigation: every entry (main navigation, page-internal
// links, deep addresses) commits through ``navigateTo``, so the deny gate, the
// unsaved checks and the current-item update cannot drift apart. The marked block
// is executed on its own by tests/test_sidebar_account_frontend.cjs, so the
// ordering contract (leave decision before any commit) is asserted against the
// shipped code rather than a paraphrase of it.
let currentView = 'chat';
let agentNavigationVersion = 0;

// Retired personal page ids and the shared page that carries the same objects
// now (change unify-console-by-data-scope, task 8.1). A member's own objects are
// managed on the formal pages — the same ones an administrator uses, with the
// range decided by ``auth.object_scope`` — so an old bookmark or a pasted
// ``#view-personal-*`` link forwards instead of painting a retired page.
```
</details>

### symbol `_collapseMenuGroup` (console lines 2204–2212, base lines 1781–1782)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
// Ensure closed groups are excluded from the tab order (run after the items
// get their default tabIndex so it is authoritative).
_syncMenuGroupFocusability();

// Return to an available page from the "not available" view (unreachable).
document.getElementById('nav-unavailable-back')?.addEventListener('click', () => {
    const next = ['chat', 'history', 'agent-workbench', 'todo', 'tasks', 'knowledge', 'agents']
        .find(v => VIEW_META[v] && document.getElementById('view-' + v));
    if (next) navigateTo(next);
```
</details>

### symbol `syncSidebarToggleState` (console lines 2215–2223, base lines 1785–1784)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
function syncSidebarToggleState() {
    const expanded = window.innerWidth >= 1024
        ? !document.getElementById('app').classList.contains('sidebar-collapsed')
        : !document.getElementById('sidebar').classList.contains('-translate-x-full');
    document.getElementById('menu-toggle')?.setAttribute('aria-expanded', String(expanded));
}
syncSidebarToggleState();
window.addEventListener('resize', syncSidebarToggleState);

```
</details>

### symbol `syncSidebarToggleState` (console lines 2225–2225, base lines 1786–1785)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    closeAccountMenu();
```
</details>

### symbol `navigateTo` (console lines 17397–17400, base lines 12864–12863)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    // Previously-visible but not-yet-enabled targets (menu placeholders) are
    // routed by the base handler to a clear "not available" view instead of a
    // silent no-op. Do not early-return here.

```
</details>

<details><summary>upstream's version of `navigateTo`</summary>

```javascript
function navigateTo(viewId, tab) {
    // An open document editor is about to be replaced by another view, which
    // would drop the edit with nothing on screen to say so.
    if (!docGuardUnsaved(() => navigateTo(viewId, tab))) return false;

    // Stop log stream when leaving logs view
    if (currentView === 'logs' && viewId !== 'logs') stopLogStream();

    _switchToView(viewId);
    // The address bar follows the view, so a reload lands back here.
    routeEnterView(viewId);

    // Lazy-load view data
    if (viewId === 'config') { loadConfigView(); switchConfigTab(tab || 'basic'); }
    else if (viewId === 'skills') { resetSkillViewer(); loadSkillsView(); }
    else if (viewId === 'memory') {
        memoryEditor.forget();
        document.getElementById('memory-panel-viewer').classList.add('hidden');
        document.getElementById('memory-panel-list').classList.remove('hidden');
        // Keep the last viewed Agent across refreshes, but drop it if that
        // Agent has since been deleted so we don't point at a ghost.
        if (memoryAgentId && agentCatalog.length && !agentCatalog.some(a => a.id === memoryAgentId)) {
            memoryAgentId = '';
            localStorage.removeItem('cow_memory_agent');
        }
        if (!memoryAgentId) memoryAgentId = activeAgentId || defaultAgentId;
        renderMemoryAgentSelect();
        switchMemoryTab(tab || 'files');
    }
    // loadKnowledgeView lands on the docs tab itself, so unlike the views
    // above there is no default to pass -- only a route-named tab to override
    // it with.
    else if (viewId === 'knowledge') { loadKnowledgeView(); if (tab) switchKnowledgeTab(tab); }
    else if (viewId === 'channels') loadChannelsView();
    else if (viewId === 'tasks') { switchTasksTab(tab || 'tasks'); loadTasksView(); }
    else if (viewId === 'logs') startLogStream();
    return true;
}

```
</details>

### symbol `navigateTo` (console lines 17411–17411, base lines 12874–12874)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    if (viewId === 'config') { enterConfigView(); }
```
</details>

<details><summary>upstream's version of `navigateTo`</summary>

```javascript
function navigateTo(viewId, tab) {
    // An open document editor is about to be replaced by another view, which
    // would drop the edit with nothing on screen to say so.
    if (!docGuardUnsaved(() => navigateTo(viewId, tab))) return false;

    // Stop log stream when leaving logs view
    if (currentView === 'logs' && viewId !== 'logs') stopLogStream();

    _switchToView(viewId);
    // The address bar follows the view, so a reload lands back here.
    routeEnterView(viewId);

    // Lazy-load view data
    if (viewId === 'config') { loadConfigView(); switchConfigTab(tab || 'basic'); }
    else if (viewId === 'skills') { resetSkillViewer(); loadSkillsView(); }
    else if (viewId === 'memory') {
        memoryEditor.forget();
        document.getElementById('memory-panel-viewer').classList.add('hidden');
        document.getElementById('memory-panel-list').classList.remove('hidden');
        // Keep the last viewed Agent across refreshes, but drop it if that
        // Agent has since been deleted so we don't point at a ghost.
        if (memoryAgentId && agentCatalog.length && !agentCatalog.some(a => a.id === memoryAgentId)) {
            memoryAgentId = '';
            localStorage.removeItem('cow_memory_agent');
        }
        if (!memoryAgentId) memoryAgentId = activeAgentId || defaultAgentId;
        renderMemoryAgentSelect();
        switchMemoryTab(tab || 'files');
    }
    // loadKnowledgeView lands on the docs tab itself, so unlike the views
    // above there is no default to pass -- only a route-named tab to override
    // it with.
    else if (viewId === 'knowledge') { loadKnowledgeView(); if (tab) switchKnowledgeTab(tab); }
    else if (viewId === 'channels') loadChannelsView();
    else if (viewId === 'tasks') { switchTasksTab(tab || 'tasks'); loadTasksView(); }
    else if (viewId === 'logs') startLogStream();
    return true;
}

```
</details>

### symbol `navigateTo` (console lines 17431–17432, base lines 12893–12892)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    // `todo` is a fork view and loads through the registry in the base
    // navigateTo (task 8.6), so it is not hard-coded here anymore.
```
</details>

<details><summary>upstream's version of `navigateTo`</summary>

```javascript
function navigateTo(viewId, tab) {
    // An open document editor is about to be replaced by another view, which
    // would drop the edit with nothing on screen to say so.
    if (!docGuardUnsaved(() => navigateTo(viewId, tab))) return false;

    // Stop log stream when leaving logs view
    if (currentView === 'logs' && viewId !== 'logs') stopLogStream();

    _switchToView(viewId);
    // The address bar follows the view, so a reload lands back here.
    routeEnterView(viewId);

    // Lazy-load view data
    if (viewId === 'config') { loadConfigView(); switchConfigTab(tab || 'basic'); }
    else if (viewId === 'skills') { resetSkillViewer(); loadSkillsView(); }
    else if (viewId === 'memory') {
        memoryEditor.forget();
        document.getElementById('memory-panel-viewer').classList.add('hidden');
        document.getElementById('memory-panel-list').classList.remove('hidden');
        // Keep the last viewed Agent across refreshes, but drop it if that
        // Agent has since been deleted so we don't point at a ghost.
        if (memoryAgentId && agentCatalog.length && !agentCatalog.some(a => a.id === memoryAgentId)) {
            memoryAgentId = '';
            localStorage.removeItem('cow_memory_agent');
        }
        if (!memoryAgentId) memoryAgentId = activeAgentId || defaultAgentId;
        renderMemoryAgentSelect();
        switchMemoryTab(tab || 'files');
    }
    // loadKnowledgeView lands on the docs tab itself, so unlike the views
    // above there is no default to pass -- only a route-named tab to override
    // it with.
    else if (viewId === 'knowledge') { loadKnowledgeView(); if (tab) switchKnowledgeTab(tab); }
    else if (viewId === 'channels') loadChannelsView();
    else if (viewId === 'tasks') { switchTasksTab(tab || 'tasks'); loadTasksView(); }
    else if (viewId === 'logs') startLogStream();
    return true;
}

```
</details>

## `channel/web/static/js/views/sessions.js` — 7 region(s)

### symbol `newChat` (console lines 8833–8833, base lines 7342–7342)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
// Session History (workbench page)
```
</details>

### symbol `set` (console lines 8866–8865, base lines 7437–7437)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
```
</details>

<details><summary>upstream's version of `set`</summary>

```javascript
    const set = (id, key, pos) => {
        const el = document.getElementById(id);
        if (!el) return;
        _setBtnTooltip(el, t(key));
        if (pos) el.setAttribute('data-tooltip-pos', pos);
    };
    set('new-chat-btn', 'tip_new_chat');
    // #clear-context-btn gets a rich hover popover (context-usage chart) instead
    // of the plain text tooltip, so it must not carry a data-tooltip here —
    // otherwise both would pop at once.
    set('attach-btn', 'tip_attach');
    set('steer-btn', 'steer_active');
    set('session-toggle-btn', 'session_history', 'bottom');
    set('workspace-toggle-btn', 'ws_toggle', 'bottom');
    // Optimize / mic buttons carry state-dependent tooltips managed in their
    // own setup, but on language switch we reset them to the idle label so the
    // tooltip follows the current locale.
    set('optimize-btn', 'optimize_idle_title');
    set('mic-btn', 'mic_idle_title');
    // Send button only carries a tooltip while it acts as the cancel button.
    _setBtnTooltip(sendBtn, sendBtnMode === 'cancel' ? t('tip_cancel') : '');
    // The permission / model chips carry translated labels and tooltips, so they
    // are repainted here too (this runs on every language switch).
    _renderPermissionChip();
    _renderModelChip();
    // applyI18n resets the placeholder to the solo hint via data-i18n-placeholder,
    // so re-apply the team-aware variant for group conversations.
    _renderInputPlaceholder();
}

// A session that exists in the browser but not yet in the database: the user
// pressed "new chat" and has not sent the first message. Rendered from the same
// path as real sessions so it lands in the right group.
function _addOptimisticSessionItem(sid) {
    const container = document.getElementById('session-list');
    if (!container) return;
    if (_sessionItems.some(s => s.session_id === sid)) return;

    _sessionItems.unshift({
        session_id: sid,
        title: t('new_chat'),
        last_active: Math.floor(Date.now() / 1000),
        pinned: 0,
        // The fresh session inherits the workspace the selector currently shows.
        project: _wsSelState.current
```
</details>

### symbol `set` (console lines 8877–8878, base lines 7446–7448)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    // The model chip carries translated labels and tooltips, so it is repainted
    // here too (this runs on every language switch).
```
</details>

<details><summary>upstream's version of `set`</summary>

```javascript
    const set = (id, key, pos) => {
        const el = document.getElementById(id);
        if (!el) return;
        _setBtnTooltip(el, t(key));
        if (pos) el.setAttribute('data-tooltip-pos', pos);
    };
    set('new-chat-btn', 'tip_new_chat');
    // #clear-context-btn gets a rich hover popover (context-usage chart) instead
    // of the plain text tooltip, so it must not carry a data-tooltip here —
    // otherwise both would pop at once.
    set('attach-btn', 'tip_attach');
    set('steer-btn', 'steer_active');
    set('session-toggle-btn', 'session_history', 'bottom');
    set('workspace-toggle-btn', 'ws_toggle', 'bottom');
    // Optimize / mic buttons carry state-dependent tooltips managed in their
    // own setup, but on language switch we reset them to the idle label so the
    // tooltip follows the current locale.
    set('optimize-btn', 'optimize_idle_title');
    set('mic-btn', 'mic_idle_title');
    // Send button only carries a tooltip while it acts as the cancel button.
    _setBtnTooltip(sendBtn, sendBtnMode === 'cancel' ? t('tip_cancel') : '');
    // The permission / model chips carry translated labels and tooltips, so they
    // are repainted here too (this runs on every language switch).
    _renderPermissionChip();
    _renderModelChip();
    // applyI18n resets the placeholder to the solo hint via data-i18n-placeholder,
    // so re-apply the team-aware variant for group conversations.
    _renderInputPlaceholder();
}

// A session that exists in the browser but not yet in the database: the user
// pressed "new chat" and has not sent the first message. Rendered from the same
// path as real sessions so it lands in the right group.
function _addOptimisticSessionItem(sid) {
    const container = document.getElementById('session-list');
    if (!container) return;
    if (_sessionItems.some(s => s.session_id === sid)) return;

    _sessionItems.unshift({
        session_id: sid,
        title: t('new_chat'),
        last_active: Math.floor(Date.now() / 1000),
        pinned: 0,
        // The fresh session inherits the workspace the selector currently shows.
        project: _wsSelState.current
```
</details>

### symbol `loadSessionList` (console lines 9166–9669, base lines 7522–7522)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
// Refresh the list for session operations that happen while the user may not be
// on the history page: reload only if it is the active view, otherwise mark it
// dirty so the next visit re-reads.
function _refreshHistoryList() {
    if (_historyVisible) loadSessionList();
    else _historyDirty = true;
    if (typeof loadSidebarRecentSessions === 'function') loadSidebarRecentSessions();
}

// === SIDEBAR_RECENT_BEGIN ===
const SIDEBAR_RECENT_LIMIT = 10;
function _sidebarRecentLimit(items) {
    return Array.isArray(items) ? items.slice(0, SIDEBAR_RECENT_LIMIT) : [];
}
// The 会话历史 block is the `history` workbench menu entry. It is denied when the
// authoritative projection withholds its menu grant; an unknown projection (or
// legacy mode) never denies.
function _sidebarRecentDenied() {
    if (typeof _viewNavDenied !== 'function') return false;
    return !!_viewNavDenied('history');
}
// === SIDEBAR_RECENT_END ===

let _sidebarRecentItems = [];
let _sidebarRecentSeq = 0;
// Declared before sidebar/history init so mid-script DOMContentLoaded or
// deferred callbacks cannot hit temporal-dead-zone on these lets.
let _dragSpaceKey = null;
let _sessionActionMenu = null;
let _sessionMenuCleanup = null;

function renderSidebarRecentSessions() {
    const list = document.getElementById('sidebar-recent-list');
    const more = document.getElementById('sidebar-recent-more');
    if (!list) return;
    list.innerHTML = '';
    const items = _sidebarRecentLimit(_sidebarRecentItems);
    if (!items.length) {
        const empty = document.createElement('div');
        empty.className = 'sidebar-recent-empty';
```
</details>

<details><summary>upstream's version of `loadSessionList`</summary>

```javascript
function loadSessionList(onDone) {
    const container = document.getElementById('session-list');
    if (!container) return;

    _sessionPage = 1;
    _sessionHasMore = false;

    _fetchSessionPage(1, true, onDone);
}

function _fetchSessionPage(page, clear, onDone) {
    if (_sessionLoading) return;
    _sessionLoading = true;

    const container = document.getElementById('session-list');
    if (!container) { _sessionLoading = false; return; }

    fetch(`/api/sessions?page=${page}&page_size=${_SESSION_PAGE_SIZE}&scope=all`)
        .then(r => r.json())
        .then(data => {
            _sessionLoading = false;
            if (data.status !== 'success') return;

            if (clear) _sessionItems = [];

            const sessions = data.sessions || [];
            _sessionPage = page;
            _sessionHasMore = !!data.has_more;
            _sessionGroupMode = data.group_mode === 'project' ? 'project' : 'time';
            if (Array.isArray(data.project_order)) _projectOrder = data.project_order;

            const sessionKey = s => `${(s.agent && s.agent.id) || ''}::${s.session_id}`;
            const seen = new Set(_sessionItems.map(sessionKey));
            sessions.forEach(s => {
                const key = sessionKey(s);
                if (seen.has(key)) return;
                seen.add(key);
                _sessionItems.push(s);
            });

            _renderSessionList();
            if (typeof onDone === 'function') onDone();
        })
        .catch(() => { _sessionLoading = false; });
}
```
</details>

### symbol `fail` (console lines 9718–9722, base lines 7532–7531)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
            // Late / stale result: the page changed or the identity moved on.
            if (!current()) return;
            // Editing can begin after this request was sent. Defer its result
            // rather than replacing a focused editor or a dragged project.
            if (container.querySelector('.session-title-input') || _dragSpaceKey !== null) {
```
</details>

### symbol `fail` (console lines 9724–9740, base lines 7533–7533)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
                _historyRequestController = null;
                _historyDirty = true;
                _setHistoryState('');
                _updateHistorySearchControls();
                return;
            }

            if (data.status !== 'success') {
                fail(data._historyUnavailable ? 'session_history_not_enabled' : 'session_history_failed');
                return;
            }
            if (query && data.query !== query) {
                fail('history_search_unsupported');
                return;
            }
            _sessionLoading = false;
            _historyRequestController = null;
```
</details>

### symbol `switchSession` (console lines 10272–10272, base lines 7945–7945)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    writeScopedPreference(activeSessionStorageKey(), sessionId);
```
</details>

<details><summary>upstream's version of `switchSession`</summary>

```javascript
function switchSession(newSessionId, agentId) {
    if (agentId && agentId !== activeAgentId) {
        activeAgentId = agentId;
        localStorage.setItem('cow_active_agent', activeAgentId);
    }
    if (newSessionId === sessionId) {
        if (currentView !== 'chat') navigateTo('chat');
        renderComposerIdentity();
        return;
    }

    // The preview panel is scoped to a session's workspace, so switching tears
    // down an open editor. Settle unsaved edits before committing to the switch.
    if (typeof wsGuardUnsaved === 'function'
        && !wsGuardUnsaved(() => switchSession(newSessionId))) return;

    // Do NOT close active streams here: sessions run in parallel, so any
    // in-flight reply for another session must keep streaming in the
    // background (it self-guards against rendering into the foreign view).
    // Switching back re-attaches and resumes live streaming.

    sessionId = newSessionId;
    updateEditButtonsState();
    localStorage.setItem(activeSessionStorageKey(), sessionId);
    refreshWorkspaceSelector();
    refreshSessionSettings();
    // Reflect the new session's context in the mini pie right away.
    if (typeof _ctxRefresh === 'function') { try { _ctxRefresh({}); } catch (_) {} }
    // Reset the file/preview panel so it reflects the new session's root.
    if (typeof wsOnSessionSwitch === 'function') wsOnSessionSwitch();

    historyPage = 0;
    historyHasMore = false;
    historyLoading = false;

    messagesDiv.innerHTML = '';
    loadHistory(1);
    startPolling();

    // Restore the send button to match this session's stream state, and if a
    // reply is still streaming in the background, re-attach to resume showing
    // it live (the user turn itself comes from history above).
    const pendingReq = sessionActiveRequest[runtimeSessionKey(sessionId)];
    if (pendingReq) {
        setSendBtnCancelMode(pendingReq);
```
</details>

## `channel/web/static/js/core/i18n.js` — 6 region(s)

### symbol `applyBrandToAgentAvatars` (console lines 906–921, base lines 13–1253)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
// The language dictionaries live in per-domain namespace files under
// static/js/i18n/ (change fork-decoupling-and-tenant-hardening, task 8.5) so
// fork and upstream key edits no longer land in the same literal. Each file
// registers itself on window.__cowI18N__; merge them into one lookup table.
const I18N = (function mergeI18nNamespaces() {
    const merged = {};
    const registry = (typeof window !== 'undefined' && window.__cowI18N__) || {};
    Object.keys(registry).forEach(function (domain) {
        const namespace = registry[domain] || {};
        Object.keys(namespace).forEach(function (lang) {
            if (!merged[lang]) merged[lang] = {};
            Object.assign(merged[lang], namespace[lang] || {});
        });
    });
    return merged;
})();
```
</details>

### symbol `applyLanguage` (console lines 1033–1034, base lines 1339–1338)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    window.__cowLang__ = currentLang;
    try {
```
</details>

### symbol `applyLanguage` (console lines 1036–1037, base lines 1340–1339)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
        languageStorageFailed = localStorage.getItem('cow_lang') !== currentLang;
    } catch (_) { languageStorageFailed = true; }
```
</details>

### symbol `applyLanguage` (console lines 1044–1046, base lines 1345–1346)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    if (writeToBackend) {
        // Sync language choice to backend first, then trigger dynamic views
        // reload to avoid race conditions on API endpoints.
```
</details>

### symbol `applyLanguage` (console lines 1050–1052, base lines 1350–1349)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    } else {
        try { rerenderDynamicViews(); } catch (e) {}
    }
```
</details>

### symbol `rerenderDynamicViews` (console lines 1131–1133, base lines 1457–1456)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    // Re-render the fork/identity views through the view registry (task 8.6).
    // Their `repaint` is the lightweight in-memory reload they used to get here.
    _repaintRegisteredView(currentView);
```
</details>

<details><summary>upstream's version of `rerenderDynamicViews`</summary>

```javascript
function rerenderDynamicViews() {
    // Models are a tab of the config view, not a view of their own.
    if (currentView === 'config' && typeof renderModelsView === 'function'
            && modelsState && (modelsState.providers || modelsState.capabilities)) {
        renderModelsView();
    }
    // Reload task list after language switch
    if (currentView === 'tasks') {
        tasksLoaded = false;
        loadTasksView();
    }
    // Reload skills and tools after language switch
    if (currentView === 'skills') {
        toolsLoaded = false;
        loadSkillsView();
    }
    // Reload channels after language switch
    if (currentView === 'channels') {
        loadChannelsView();
    }
    // Reload config after language switch
    if (currentView === 'config') {
        loadConfigView();
    }
    // Repaint the Agents workbench after a language switch. The grid, detail
    // pane and avatar picker are built from t() into innerHTML, so applyI18n()
    // (which only touches data-i18n nodes) can't relocalize them; re-render from
    // the in-memory catalog instead of refetching.
    if (currentView === 'agents') {
        renderAgentsGrid();
        if (selectedAdminAgentId) renderAgentDetail();
    }
}

// Floating tooltip portal for [data-tip-key] elements. Tooltip nodes are
// appended to <body> so they aren't clipped by overflow:hidden ancestors
// (e.g. the config panel's scroll container).
let _cfgTipPortalEl = null;
let _cfgTipPortalInstalled = false;
function installCfgTipPortal() {
    if (_cfgTipPortalInstalled) return;
    _cfgTipPortalInstalled = true;

    const showTip = (target) => {
        const text = target.getAttribute('data-tooltip');
```
</details>

## `channel/web/static/js/chat/new-chat.js` — 4 region(s)

### symbol `addLoadingIndicator` (console lines 8673–8677, base lines 7119–7121)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
/* The session-panel "新对话" button. Starting a chat is never a decision: the
   button opens one with the default-anchored Agent straight away, so a tenant
   that owns several Agents does not gate the primary action on a picker.
   The caret is the *optional* "switch Agent / start a team chat" entry, and
   only exists once there is more than one Agent. */
```
</details>

### symbol `newChat` (console lines 8810–8810, base lines 7235–7300)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    renderWelcomeScreen();
```
</details>

<details><summary>upstream's version of `newChat`</summary>

```javascript
function newChat(optimistic = true, inherit = true) {
    // A fresh session resets the preview panel, discarding an open editor.
    if (typeof wsGuardUnsaved === 'function'
        && !wsGuardUnsaved(() => newChat(optimistic, inherit))) return;

    // Do NOT close active streams: other sessions keep streaming in the
    // background (each stream self-guards against the foreign view) and their
    // replies still complete and persist.

    // Generate a fresh session and persist it so the next page load also starts clean
    sessionId = generateSessionId();
    localStorage.setItem(activeSessionStorageKey(), sessionId);
    refreshWorkspaceSelector();  // a fresh session starts on the default workspace
    refreshSessionSettings();    // ... and on the global model / permission
    if (typeof wsOnSessionSwitch === 'function') wsOnSessionSwitch();
    resetSendBtnSendMode();  // fresh session has no in-flight reply
    startPolling();  // bump generation so old loop self-cancels, new loop uses fresh sessionId
    messagesDiv.innerHTML = '';
    const ws = document.createElement('div');
    ws.id = 'welcome-screen';
    ws.className = 'flex flex-col items-center justify-center h-full px-6 pb-16';
    ws.style.paddingTop = '6vh';
    ws.innerHTML = `
        <img src="/assets/logo.jpg" alt="CowAgent" class="w-16 h-16 rounded-2xl mb-6 shadow-lg shadow-primary-500/20">
        <h1 class="text-2xl font-bold text-slate-800 dark:text-slate-100 mb-3">${appConfig.title || 'CowAgent'}</h1>
        <p class="text-slate-500 dark:text-slate-400 text-center max-w-lg mb-10 leading-relaxed" data-i18n="welcome_subtitle">${t('welcome_subtitle')}</p>
        <div class="grid grid-cols-2 sm:grid-cols-3 gap-3 w-full max-w-2xl">
            <div class="example-card group bg-white dark:bg-[#1A1A1A] border border-slate-200 dark:border-white/10 rounded-xl p-4 cursor-pointer hover:border-primary-300 dark:hover:border-primary-600 hover:shadow-md transition-all duration-200">
                <div class="flex items-center gap-2 mb-2">
                    <div class="w-7 h-7 rounded-lg bg-blue-50 dark:bg-blue-900/30 flex items-center justify-center">
                        <i class="fas fa-folder-open text-blue-500 text-xs"></i>
                    </div>
                    <span class="font-medium text-sm text-slate-700 dark:text-slate-200" data-i18n="example_sys_title">${t('example_sys_title')}</span>
                </div>
                <p class="text-sm text-slate-500 dark:text-slate-400 leading-relaxed" data-i18n="example_sys_text">${t('example_sys_text')}</p>
            </div>
            <div class="example-card group bg-white dark:bg-[#1A1A1A] border border-slate-200 dark:border-white/10 rounded-xl p-4 cursor-pointer hover:border-primary-300 dark:hover:border-primary-600 hover:shadow-md transition-all duration-200">
                <div class="flex items-center gap-2 mb-2">
                    <div class="w-7 h-7 rounded-lg bg-amber-50 dark:bg-amber-900/30 flex items-center justify-center">
                        <i class="fas fa-clock text-amber-500 text-xs"></i>
                    </div>
                    <span class="font-medium text-sm text-slate-700 dark:text-slate-200" data-i18n="example_task_title">${t('example_task_title')}</span>
                </div>
                <p class="text-sm text-slate-500 dark:text-slate-400 leading-relaxed" data-i18n="example_task_text">${t('example_task_text')}</p>
            </div>
```
</details>

### symbol `newChat` (console lines 8820–8820, base lines 7334–7333)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    if (_historyVisible) {
```
</details>

<details><summary>upstream's version of `newChat`</summary>

```javascript
function newChat(optimistic = true, inherit = true) {
    // A fresh session resets the preview panel, discarding an open editor.
    if (typeof wsGuardUnsaved === 'function'
        && !wsGuardUnsaved(() => newChat(optimistic, inherit))) return;

    // Do NOT close active streams: other sessions keep streaming in the
    // background (each stream self-guards against the foreign view) and their
    // replies still complete and persist.

    // Generate a fresh session and persist it so the next page load also starts clean
    sessionId = generateSessionId();
    localStorage.setItem(activeSessionStorageKey(), sessionId);
    refreshWorkspaceSelector();  // a fresh session starts on the default workspace
    refreshSessionSettings();    // ... and on the global model / permission
    if (typeof wsOnSessionSwitch === 'function') wsOnSessionSwitch();
    resetSendBtnSendMode();  // fresh session has no in-flight reply
    startPolling();  // bump generation so old loop self-cancels, new loop uses fresh sessionId
    messagesDiv.innerHTML = '';
    const ws = document.createElement('div');
    ws.id = 'welcome-screen';
    ws.className = 'flex flex-col items-center justify-center h-full px-6 pb-16';
    ws.style.paddingTop = '6vh';
    ws.innerHTML = `
        <img src="/assets/logo.jpg" alt="CowAgent" class="w-16 h-16 rounded-2xl mb-6 shadow-lg shadow-primary-500/20">
        <h1 class="text-2xl font-bold text-slate-800 dark:text-slate-100 mb-3">${appConfig.title || 'CowAgent'}</h1>
        <p class="text-slate-500 dark:text-slate-400 text-center max-w-lg mb-10 leading-relaxed" data-i18n="welcome_subtitle">${t('welcome_subtitle')}</p>
        <div class="grid grid-cols-2 sm:grid-cols-3 gap-3 w-full max-w-2xl">
            <div class="example-card group bg-white dark:bg-[#1A1A1A] border border-slate-200 dark:border-white/10 rounded-xl p-4 cursor-pointer hover:border-primary-300 dark:hover:border-primary-600 hover:shadow-md transition-all duration-200">
                <div class="flex items-center gap-2 mb-2">
                    <div class="w-7 h-7 rounded-lg bg-blue-50 dark:bg-blue-900/30 flex items-center justify-center">
                        <i class="fas fa-folder-open text-blue-500 text-xs"></i>
                    </div>
                    <span class="font-medium text-sm text-slate-700 dark:text-slate-200" data-i18n="example_sys_title">${t('example_sys_title')}</span>
                </div>
                <p class="text-sm text-slate-500 dark:text-slate-400 leading-relaxed" data-i18n="example_sys_text">${t('example_sys_text')}</p>
            </div>
            <div class="example-card group bg-white dark:bg-[#1A1A1A] border border-slate-200 dark:border-white/10 rounded-xl p-4 cursor-pointer hover:border-primary-300 dark:hover:border-primary-600 hover:shadow-md transition-all duration-200">
                <div class="flex items-center gap-2 mb-2">
                    <div class="w-7 h-7 rounded-lg bg-amber-50 dark:bg-amber-900/30 flex items-center justify-center">
                        <i class="fas fa-clock text-amber-500 text-xs"></i>
                    </div>
                    <span class="font-medium text-sm text-slate-700 dark:text-slate-200" data-i18n="example_task_title">${t('example_task_title')}</span>
                </div>
                <p class="text-sm text-slate-500 dark:text-slate-400 leading-relaxed" data-i18n="example_task_text">${t('example_task_text')}</p>
            </div>
```
</details>

### symbol `newChat` (console lines 8826–8829, base lines 7339–7338)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    } else {
        // The list is hidden; mark it dirty so the next visit re-reads it.
        _historyDirty = true;
    }
```
</details>

<details><summary>upstream's version of `newChat`</summary>

```javascript
function newChat(optimistic = true, inherit = true) {
    // A fresh session resets the preview panel, discarding an open editor.
    if (typeof wsGuardUnsaved === 'function'
        && !wsGuardUnsaved(() => newChat(optimistic, inherit))) return;

    // Do NOT close active streams: other sessions keep streaming in the
    // background (each stream self-guards against the foreign view) and their
    // replies still complete and persist.

    // Generate a fresh session and persist it so the next page load also starts clean
    sessionId = generateSessionId();
    localStorage.setItem(activeSessionStorageKey(), sessionId);
    refreshWorkspaceSelector();  // a fresh session starts on the default workspace
    refreshSessionSettings();    // ... and on the global model / permission
    if (typeof wsOnSessionSwitch === 'function') wsOnSessionSwitch();
    resetSendBtnSendMode();  // fresh session has no in-flight reply
    startPolling();  // bump generation so old loop self-cancels, new loop uses fresh sessionId
    messagesDiv.innerHTML = '';
    const ws = document.createElement('div');
    ws.id = 'welcome-screen';
    ws.className = 'flex flex-col items-center justify-center h-full px-6 pb-16';
    ws.style.paddingTop = '6vh';
    ws.innerHTML = `
        <img src="/assets/logo.jpg" alt="CowAgent" class="w-16 h-16 rounded-2xl mb-6 shadow-lg shadow-primary-500/20">
        <h1 class="text-2xl font-bold text-slate-800 dark:text-slate-100 mb-3">${appConfig.title || 'CowAgent'}</h1>
        <p class="text-slate-500 dark:text-slate-400 text-center max-w-lg mb-10 leading-relaxed" data-i18n="welcome_subtitle">${t('welcome_subtitle')}</p>
        <div class="grid grid-cols-2 sm:grid-cols-3 gap-3 w-full max-w-2xl">
            <div class="example-card group bg-white dark:bg-[#1A1A1A] border border-slate-200 dark:border-white/10 rounded-xl p-4 cursor-pointer hover:border-primary-300 dark:hover:border-primary-600 hover:shadow-md transition-all duration-200">
                <div class="flex items-center gap-2 mb-2">
                    <div class="w-7 h-7 rounded-lg bg-blue-50 dark:bg-blue-900/30 flex items-center justify-center">
                        <i class="fas fa-folder-open text-blue-500 text-xs"></i>
                    </div>
                    <span class="font-medium text-sm text-slate-700 dark:text-slate-200" data-i18n="example_sys_title">${t('example_sys_title')}</span>
                </div>
                <p class="text-sm text-slate-500 dark:text-slate-400 leading-relaxed" data-i18n="example_sys_text">${t('example_sys_text')}</p>
            </div>
            <div class="example-card group bg-white dark:bg-[#1A1A1A] border border-slate-200 dark:border-white/10 rounded-xl p-4 cursor-pointer hover:border-primary-300 dark:hover:border-primary-600 hover:shadow-md transition-all duration-200">
                <div class="flex items-center gap-2 mb-2">
                    <div class="w-7 h-7 rounded-lg bg-amber-50 dark:bg-amber-900/30 flex items-center justify-center">
                        <i class="fas fa-clock text-amber-500 text-xs"></i>
                    </div>
                    <span class="font-medium text-sm text-slate-700 dark:text-slate-200" data-i18n="example_task_title">${t('example_task_title')}</span>
                </div>
                <p class="text-sm text-slate-500 dark:text-slate-400 leading-relaxed" data-i18n="example_task_text">${t('example_task_text')}</p>
            </div>
```
</details>

## `channel/web/static/js/chat/render.js` — 4 region(s)

### symbol `loadHistory` (console lines 8512–8517, base lines 6980–6980)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    const historySessionId = sessionId;
    const historyAgentId = activeAgentId;
    const historyEpoch = _authEpoch;
    const historyTenantId = sessionStorage.getItem('cow_tenant_id');
    const context = JSON.stringify([historyEpoch, historyTenantId, historyAgentId, historySessionId]);
    if (historyLoading && _historyLoadContext === context) return;
```
</details>

<details><summary>upstream's version of `loadHistory`</summary>

```javascript
function loadHistory(page) {
    if (historyLoading) return;
    historyLoading = true;
    const historySessionId = sessionId;

    // A shared conversation labels each bubble with its author and paints the
    // right face. That resolution needs this session's team roster (_sessCfg),
    // which loads asynchronously; without it every replayed bubble falls back
    // to the owner's avatar and loses its name. Make sure the roster is in hand
    // before rendering so a reload looks exactly like the live conversation.
    const ready = _sessCfg ? Promise.resolve() : refreshSessionSettings().catch(() => {});

    ready.then(() => fetch(`/api/history?session_id=${encodeURIComponent(historySessionId)}&page=${page}&page_size=20`)
        .then(r => r.json())
        .then(data => {
            // A response from a session we have since left must never render
            // into the new session's message list.
            if (historySessionId !== sessionId) return;
            if (data.status !== 'success' || data.messages.length === 0) return;

            const prevScrollHeight = messagesDiv.scrollHeight;
            const isFirstLoad = page === 1;

            // On first load, remove the welcome screen if history exists
            if (isFirstLoad) {
                const ws = document.getElementById('welcome-screen');
                if (ws) ws.remove();
            }

            // Build a fragment of history message elements in chronological order
            const fragment = document.createDocumentFragment();

            if (data.has_more && page > 1) {
                // Keep the "load more" sentinel in place (inserted below)
            }

            const ctxStartSeq = data.context_start_seq || 0;
            let dividerInserted = false;

            data.messages.forEach(msg => {
                const hasContent = msg.content && msg.content.trim();
                const hasToolCalls = msg.role === 'assistant' && msg.tool_calls && msg.tool_calls.length > 0;
                if (!hasContent && !hasToolCalls) return;

                // Insert context divider when transitioning from above to below boundary
```
</details>

### symbol `current` (console lines 8532–8541, base lines 6991–6992)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    return ready.then(() => {
        if (!current()) return;
        return fetch(`/api/history?session_id=${encodeURIComponent(historySessionId)}&agent_id=${encodeURIComponent(historyAgentId)}&page=${page}&page_size=20`)
        .then(async r => {
            const data = await r.json();
            if (!r.ok || data.status !== 'success' || !Array.isArray(data.messages)) {
                throw new Error('Conversation history request failed');
            }
            return data;
        })
```
</details>

### symbol `current` (console lines 8639–8639, base lines 7091–7090)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
        });
```
</details>

### symbol `current` (console lines 8650–8650, base lines 7096–7096)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
        });
```
</details>

## `channel/web/static/js/views/channels.js` — 4 region(s)

### symbol `channelRenderList` (console lines 15854–15865, base lines 11416–11415)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
}

// Shared channel-card shell for the platform (instance) page and the tenant
// channel page. Both render the same kinds of channels, so the icon / status
// dot / label / subtitle / action-slot markup lives in the shared module
// (task 3.1): two copies would drift and the two pages would slowly stop looking
// like each other. ``bodyHtml`` is whatever the caller needs below the header
// (Tabs, credential form, QR flow), and ``actionsHtml`` replaces the right-hand
// slot (the platform page passes its disconnect button, the tenant page its own
// actions).
function buildChannelCardShell(opts) {
    return window.ChannelWorkbench.cardShell(opts);
```
</details>

<details><summary>upstream's version of `channelRenderList`</summary>

```javascript
function channelRenderList() {
    const list = [];
    channelsData.forEach(ch => {
        if (isMultiInstanceType(ch.name)) return;  // rendered from instances
        if (ch.active) list.push(Object.assign({}, ch, { iid: ch.name }));
    });
    if (channelsMultiAgent) {
        channelInstancesView.forEach(inst => {
            list.push(Object.assign({}, inst, { iid: inst.instance_id }));
        });
    }
    // Show WeChat cards first; keep every other card in its existing relative
    // order (stable sort: weixin -> 0, everything else -> 1).
    list.sort((a, b) => (a.name === 'weixin' ? 0 : 1) - (b.name === 'weixin' ? 0 : 1));
    return list;
}

function renderActiveChannels() {
    stopWeixinQrPoll();
    stopWeixinStatusPoll();
    const container = document.getElementById('channels-content');
    container.innerHTML = '';
    closeAddChannelPanel();

    const activeChannels = channelRenderList();

    if (activeChannels.length === 0) {
        container.innerHTML = `
            <div class="flex flex-col items-center justify-center py-20">
                <div class="w-16 h-16 rounded-2xl bg-blue-50 dark:bg-blue-900/20 flex items-center justify-center mb-4">
                    <i class="fas fa-tower-broadcast text-blue-400 text-xl"></i>
                </div>
                <p class="text-slate-500 dark:text-slate-400 font-medium">${t('channels_empty')}</p>
                <p class="text-sm text-slate-400 dark:text-slate-500 mt-1">${t('channels_empty_desc')}</p>
            </div>`;
        return;
    }

    activeChannels.forEach(ch => {
        const iid = ch.iid;
        const label = (typeof ch.label === 'object') ? (ch.label[currentLang] || ch.label.en) : ch.label;
        const card = document.createElement('div');
        card.className = 'bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10 p-6';
        card.id = `channel-card-${iid}`;

```
</details>

### symbol `hasFields` (console lines 15920–15924, base lines 11470–11482)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
        card.innerHTML = buildChannelCardShell({
            iid, label, icon: ch.icon, color: ch.color,
            statusDot, statusText, subtitle: iid,
            headerMb: !!(hasFields || weixinWaiting || wecomNeedsCreds || isFeishu || multiAgentMode()),
            actionsHtml: `
```
</details>

<details><summary>upstream's version of `hasFields`</summary>

```javascript
        const hasFields = (ch.fields || []).length > 0;

        const weixinWaiting = ch.name === 'weixin' && ch.login_status && ch.login_status !== 'logged_in';
        // 飞书 / 企微机器人 active 卡片渲染带 Tab 的 panel：手动填写 + 扫码重建（覆盖现有配置）
        const isFeishu = ch.name === 'feishu';
        const isWecomBot = ch.name === 'wecom_bot';
        // An instance card (multi-Agent feishu) shows the bound agent inline and
        // uses the instance id as its subtitle instead of the bare type name.
        const isInstance = isMultiInstanceType(ch.name) && !!ch.instance_id;
        let statusDot, statusText;
        if (weixinWaiting) {
            statusDot = 'bg-amber-400 animate-pulse';
            statusText = ch.login_status === 'scanned'
                ? `<span class="text-xs text-primary-500">${t('weixin_scan_scanned')}</span>`
                : `<span class="text-xs text-amber-500">${t('weixin_scan_waiting')}</span>`;
        } else {
            statusDot = 'bg-primary-400';
            statusText = `<span class="text-xs text-primary-500">${t('channels_connected')}</span>`;
        }

        card.innerHTML = `
            <div class="flex items-center gap-4${hasFields || weixinWaiting || isFeishu || isWecomBot || multiAgentMode() ? ' mb-5' : ''}">
                <div class="w-10 h-10 rounded-xl bg-${ch.color}-50 dark:bg-${ch.color}-900/20 flex items-center justify-center flex-shrink-0">
                    <i class="fas ${ch.icon} text-${ch.color}-500 text-base"></i>
                </div>
                <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2">
                        <span class="font-semibold text-slate-800 dark:text-slate-100">${escapeHtml(isInstance ? (ch.instance_name || label) : label)}</span>
                        ${isInstance ? `<button onclick="renameChannelInstance('${ch.name}', '${escapeHtml(iid)}')" title="${escapeHtml(t('channel_rename'))}"
                            class="text-slate-400 hover:text-primary-500 cursor-pointer transition-colors flex-shrink-0">
                            <i class="fas fa-pen text-xs"></i>
                        </button>` : ''}
                        <span class="w-2 h-2 rounded-full ${statusDot}"></span>
                        ${statusText}
                    </div>
                    <p class="text-xs text-slate-500 dark:text-slate-400 mt-0.5 font-mono">${escapeHtml(isInstance ? `${label} · ${iid}` : iid)}</p>
                </div>
                <button onclick="disconnectChannel('${ch.name}', '${isInstance ? iid : ''}')"
                    class="px-3 py-1.5 rounded-lg text-xs font-medium
                           bg-red-50 dark:bg-red-900/20 text-red-500 dark:text-red-400
                           hover:bg-red-100 dark:hover:bg-red-900/40
                           cursor-pointer transition-colors flex-shrink-0">
                    ${t('channels_disconnect')}
                </button>
            </div>
```
</details>

### symbol `hasFields` (console lines 15931–15932, base lines 11489–11490)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
                </button>`,
            bodyHtml: `
```
</details>

<details><summary>upstream's version of `hasFields`</summary>

```javascript
        const hasFields = (ch.fields || []).length > 0;

        const weixinWaiting = ch.name === 'weixin' && ch.login_status && ch.login_status !== 'logged_in';
        // 飞书 / 企微机器人 active 卡片渲染带 Tab 的 panel：手动填写 + 扫码重建（覆盖现有配置）
        const isFeishu = ch.name === 'feishu';
        const isWecomBot = ch.name === 'wecom_bot';
        // An instance card (multi-Agent feishu) shows the bound agent inline and
        // uses the instance id as its subtitle instead of the bare type name.
        const isInstance = isMultiInstanceType(ch.name) && !!ch.instance_id;
        let statusDot, statusText;
        if (weixinWaiting) {
            statusDot = 'bg-amber-400 animate-pulse';
            statusText = ch.login_status === 'scanned'
                ? `<span class="text-xs text-primary-500">${t('weixin_scan_scanned')}</span>`
                : `<span class="text-xs text-amber-500">${t('weixin_scan_waiting')}</span>`;
        } else {
            statusDot = 'bg-primary-400';
            statusText = `<span class="text-xs text-primary-500">${t('channels_connected')}</span>`;
        }

        card.innerHTML = `
            <div class="flex items-center gap-4${hasFields || weixinWaiting || isFeishu || isWecomBot || multiAgentMode() ? ' mb-5' : ''}">
                <div class="w-10 h-10 rounded-xl bg-${ch.color}-50 dark:bg-${ch.color}-900/20 flex items-center justify-center flex-shrink-0">
                    <i class="fas ${ch.icon} text-${ch.color}-500 text-base"></i>
                </div>
                <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2">
                        <span class="font-semibold text-slate-800 dark:text-slate-100">${escapeHtml(isInstance ? (ch.instance_name || label) : label)}</span>
                        ${isInstance ? `<button onclick="renameChannelInstance('${ch.name}', '${escapeHtml(iid)}')" title="${escapeHtml(t('channel_rename'))}"
                            class="text-slate-400 hover:text-primary-500 cursor-pointer transition-colors flex-shrink-0">
                            <i class="fas fa-pen text-xs"></i>
                        </button>` : ''}
                        <span class="w-2 h-2 rounded-full ${statusDot}"></span>
                        ${statusText}
                    </div>
                    <p class="text-xs text-slate-500 dark:text-slate-400 mt-0.5 font-mono">${escapeHtml(isInstance ? `${label} · ${iid}` : iid)}</p>
                </div>
                <button onclick="disconnectChannel('${ch.name}', '${isInstance ? iid : ''}')"
                    class="px-3 py-1.5 rounded-lg text-xs font-medium
                           bg-red-50 dark:bg-red-900/20 text-red-500 dark:text-red-400
                           hover:bg-red-100 dark:hover:bg-red-900/40
                           cursor-pointer transition-colors flex-shrink-0">
                    ${t('channels_disconnect')}
                </button>
            </div>
```
</details>

### symbol `hasFields` (console lines 15969–15970, base lines 11527–11527)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
            </div>` : '')}`,
        });
```
</details>

<details><summary>upstream's version of `hasFields`</summary>

```javascript
        const hasFields = (ch.fields || []).length > 0;

        const weixinWaiting = ch.name === 'weixin' && ch.login_status && ch.login_status !== 'logged_in';
        // 飞书 / 企微机器人 active 卡片渲染带 Tab 的 panel：手动填写 + 扫码重建（覆盖现有配置）
        const isFeishu = ch.name === 'feishu';
        const isWecomBot = ch.name === 'wecom_bot';
        // An instance card (multi-Agent feishu) shows the bound agent inline and
        // uses the instance id as its subtitle instead of the bare type name.
        const isInstance = isMultiInstanceType(ch.name) && !!ch.instance_id;
        let statusDot, statusText;
        if (weixinWaiting) {
            statusDot = 'bg-amber-400 animate-pulse';
            statusText = ch.login_status === 'scanned'
                ? `<span class="text-xs text-primary-500">${t('weixin_scan_scanned')}</span>`
                : `<span class="text-xs text-amber-500">${t('weixin_scan_waiting')}</span>`;
        } else {
            statusDot = 'bg-primary-400';
            statusText = `<span class="text-xs text-primary-500">${t('channels_connected')}</span>`;
        }

        card.innerHTML = `
            <div class="flex items-center gap-4${hasFields || weixinWaiting || isFeishu || isWecomBot || multiAgentMode() ? ' mb-5' : ''}">
                <div class="w-10 h-10 rounded-xl bg-${ch.color}-50 dark:bg-${ch.color}-900/20 flex items-center justify-center flex-shrink-0">
                    <i class="fas ${ch.icon} text-${ch.color}-500 text-base"></i>
                </div>
                <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2">
                        <span class="font-semibold text-slate-800 dark:text-slate-100">${escapeHtml(isInstance ? (ch.instance_name || label) : label)}</span>
                        ${isInstance ? `<button onclick="renameChannelInstance('${ch.name}', '${escapeHtml(iid)}')" title="${escapeHtml(t('channel_rename'))}"
                            class="text-slate-400 hover:text-primary-500 cursor-pointer transition-colors flex-shrink-0">
                            <i class="fas fa-pen text-xs"></i>
                        </button>` : ''}
                        <span class="w-2 h-2 rounded-full ${statusDot}"></span>
                        ${statusText}
                    </div>
                    <p class="text-xs text-slate-500 dark:text-slate-400 mt-0.5 font-mono">${escapeHtml(isInstance ? `${label} · ${iid}` : iid)}</p>
                </div>
                <button onclick="disconnectChannel('${ch.name}', '${isInstance ? iid : ''}')"
                    class="px-3 py-1.5 rounded-lg text-xs font-medium
                           bg-red-50 dark:bg-red-900/20 text-red-500 dark:text-red-400
                           hover:bg-red-100 dark:hover:bg-red-900/40
                           cursor-pointer transition-colors flex-shrink-0">
                    ${t('channels_disconnect')}
                </button>
            </div>
```
</details>

## `channel/web/static/js/views/config.js` — 4 region(s)

### symbol `restoreChatState` (console lines 5037–5041, base lines 3599–3599)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
function restoreChatState() {
    const epoch = _authEpoch, owner = activeAgentId, sid = sessionId;
    const current = () => epoch === _authEpoch && owner === activeAgentId && sid === sessionId;
    return fetch('/config').then(r => r.json()).then(data => {
        if (!current()) return;
```
</details>

### symbol `current` (console lines 5044–5046, base lines 3602–3603)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
            appConfig.title = productTitle(data.title);
            const welcomeTitle = document.getElementById('welcome-title');
            if (welcomeTitle) welcomeTitle.innerHTML = productTitleHTML(appConfig.title);
```
</details>

### symbol `current` (console lines 5050–5051, base lines 3607–3607)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    }).catch(() => { if (current()) loadHistory(1); });
}
```
</details>

### symbol `switchConfigTab` (console lines 11323–11871, base lines 9005–9004)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
// Enter 模型与接入 in whichever shape the caller is entitled to. A platform
// admin gets the tabbed page (基础配置 / 模型配置) exactly as before; anyone
// else gets the authorized-model catalog and *no* tab strip, because both tabs
// are management surfaces — an empty tab row is not a way to say "you have a
// catalog, but not this". The catalog is the page's own content, not a
// read-only copy of the vendor grid.
function enterConfigView() {
    const manage = _modelsManageAllowed();
    const tabs = document.getElementById('config-tabs');
    if (tabs) tabs.classList.toggle('hidden', !manage);
    if (manage) {
        loadConfigView();
        switchConfigTab('basic');
        return;
    }
    switchConfigTab('catalog');
}

// =====================================================================
// Branding View (系统设置 → 品牌设置)
// =====================================================================
let brandingDraft = null;       // { brand_name, logo_description, logo_action, logoFile, logoPreviewUrl, hasLogoChange }
let brandingBaseline = null;    // last successful published snapshot (the form baseline)
let brandingLoading = false;
let brandingSaving = false;
let brandingReadonly = false;
let brandingReadonlyReason = '';
let brandingCanReset = false;
let brandingConflict = false;
let brandingSavePending = false;
let brandingPreviewDark = true;
let brandingImageError = false;
let brandingInitDone = false;

function brandingEl(id) {
    return document.getElementById(id);
}

function _brandingInputsEqual() {
    if (!brandingDraft || !brandingBaseline) return false;
```
</details>

<details><summary>upstream's version of `switchConfigTab`</summary>

```javascript
function switchConfigTab(tab) {
    ['basic', 'models'].forEach(name => {
        document.getElementById(`config-tab-${name}`)?.classList.toggle('active', name === tab);
        document.getElementById(`config-panel-${name}`)?.classList.toggle('hidden', name !== tab);
    });
    if (tab === 'models') loadModelsView();
    // Re-pull /config when returning to Basic: a provider added on the Models
    // tab must show up in the basic main-model provider picker without a manual
    // page refresh. loadConfigView re-renders from the fresh provider list.
    if (tab === 'basic') loadConfigView();
    routeNoteTab('config', tab);
}

```
</details>

## `channel/web/static/js/views/skills.js` — 4 region(s)

### symbol `renderSkillCard` (console lines 12028–12028, base lines 9122–9130)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
                ${switchMarkup}
```
</details>

<details><summary>upstream's version of `renderSkillCard`</summary>

```javascript
function renderSkillCard(card, sk) {
    const enabled = sk.enabled;
    const iconColor = enabled ? 'text-primary-400' : 'text-slate-300 dark:text-slate-600';
    const trackClass = enabled
        ? 'bg-primary-400'
        : 'bg-slate-200 dark:bg-slate-700';
    const thumbTranslate = enabled ? 'translate-x-3' : 'translate-x-0.5';
    card.innerHTML = `
        <div class="w-9 h-9 rounded-lg bg-amber-50 dark:bg-amber-900/20 flex items-center justify-center flex-shrink-0">
            <i class="fas fa-bolt ${iconColor} text-sm"></i>
        </div>
        <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2 mb-1">
                <span class="font-medium text-sm text-slate-700 dark:text-slate-200 truncate flex-1">${escapeHtml(sk.display_name || sk.name)}</span>
                <button
                    data-skill-edit
                    class="flex-shrink-0 p-1 -mx-1 -mt-1.5 -mb-1 rounded text-slate-300 dark:text-slate-600 hover:text-slate-500 dark:hover:text-slate-300 transition-colors"
                    title="${t('skill_edit_hint')}"
                >
                    <i class="fas fa-pen text-[10px]"></i>
                </button>
                <button
                    role="switch"
                    data-skill-switch
                    aria-checked="${enabled}"
                    class="relative inline-flex h-4 w-7 flex-shrink-0 cursor-pointer rounded-full transition-colors duration-200 ease-in-out focus:outline-none ${trackClass}"
                    title="${enabled ? (currentLang === 'zh' ? '点击禁用' : 'Click to disable') : (currentLang === 'zh' ? '点击启用' : 'Click to enable')}"
                >
                    <span class="inline-block h-3 w-3 mt-0.5 rounded-full bg-white shadow transform transition-transform duration-200 ease-in-out ${thumbTranslate}"></span>
                </button>
            </div>
            <p class="text-xs text-slate-400 dark:text-slate-500 line-clamp-2">${escapeHtml(sk.description || '--')}</p>
        </div>`;

    // Bound here rather than written into the markup above: a skill name comes
    // from its own frontmatter, and one containing a quote would break out of
    // an inline onclick attribute.
    card.title = t('skill_open_hint');
    card.onclick = () => openSkillFile(sk.name);
    const editBtn = card.querySelector('[data-skill-edit]');
    if (editBtn) {
        editBtn.onclick = (e) => {
            e.stopPropagation();
            openSkillFile(sk.name, { edit: true });
        };
```
</details>

### symbol `renderSkillCard` (console lines 12037–12037, base lines 9139–9139)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    card.onclick = () => openResourceDetail('skill', sk);
```
</details>

<details><summary>upstream's version of `renderSkillCard`</summary>

```javascript
function renderSkillCard(card, sk) {
    const enabled = sk.enabled;
    const iconColor = enabled ? 'text-primary-400' : 'text-slate-300 dark:text-slate-600';
    const trackClass = enabled
        ? 'bg-primary-400'
        : 'bg-slate-200 dark:bg-slate-700';
    const thumbTranslate = enabled ? 'translate-x-3' : 'translate-x-0.5';
    card.innerHTML = `
        <div class="w-9 h-9 rounded-lg bg-amber-50 dark:bg-amber-900/20 flex items-center justify-center flex-shrink-0">
            <i class="fas fa-bolt ${iconColor} text-sm"></i>
        </div>
        <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2 mb-1">
                <span class="font-medium text-sm text-slate-700 dark:text-slate-200 truncate flex-1">${escapeHtml(sk.display_name || sk.name)}</span>
                <button
                    data-skill-edit
                    class="flex-shrink-0 p-1 -mx-1 -mt-1.5 -mb-1 rounded text-slate-300 dark:text-slate-600 hover:text-slate-500 dark:hover:text-slate-300 transition-colors"
                    title="${t('skill_edit_hint')}"
                >
                    <i class="fas fa-pen text-[10px]"></i>
                </button>
                <button
                    role="switch"
                    data-skill-switch
                    aria-checked="${enabled}"
                    class="relative inline-flex h-4 w-7 flex-shrink-0 cursor-pointer rounded-full transition-colors duration-200 ease-in-out focus:outline-none ${trackClass}"
                    title="${enabled ? (currentLang === 'zh' ? '点击禁用' : 'Click to disable') : (currentLang === 'zh' ? '点击启用' : 'Click to enable')}"
                >
                    <span class="inline-block h-3 w-3 mt-0.5 rounded-full bg-white shadow transform transition-transform duration-200 ease-in-out ${thumbTranslate}"></span>
                </button>
            </div>
            <p class="text-xs text-slate-400 dark:text-slate-500 line-clamp-2">${escapeHtml(sk.description || '--')}</p>
        </div>`;

    // Bound here rather than written into the markup above: a skill name comes
    // from its own frontmatter, and one containing a quote would break out of
    // an inline onclick attribute.
    card.title = t('skill_open_hint');
    card.onclick = () => openSkillFile(sk.name);
    const editBtn = card.querySelector('[data-skill-edit]');
    if (editBtn) {
        editBtn.onclick = (e) => {
            e.stopPropagation();
            openSkillFile(sk.name, { edit: true });
        };
```
</details>

### symbol `toggleSkill` (console lines 12064–12069, base lines 9161–9161)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
        if (data.status !== 'success') {
            if (card) card.style.opacity = '1';
            alert(currentLang === 'zh' ? '操作失败，请稍后再试' : 'Operation failed, please try again');
            return false;
        }
        if (row) row.enabled = !currentlyEnabled;
```
</details>

<details><summary>upstream's version of `toggleSkill`</summary>

```javascript
function toggleSkill(name, currentlyEnabled) {
    const action = currentlyEnabled ? 'close' : 'open';
    const card = document.querySelector(`[data-skill-name="${CSS.escape(name)}"]`);
    if (card) card.style.opacity = '0.5';

    fetch('/api/skills', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, name })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            if (card) {
                card.dataset.enabled = currentlyEnabled ? '0' : '1';
                card.style.opacity = '1';
                renderSkillCard(card, {
                    name: name,
                    description: card.dataset.skillDesc || '',
                    display_name: card.dataset.skillDisplayName || '',
                    enabled: !currentlyEnabled,
                });
            }
        } else {
            if (card) card.style.opacity = '1';
            alert(currentLang === 'zh' ? '操作失败，请稍后再试' : 'Operation failed, please try again');
        }
    })
    .catch(() => {
        if (card) card.style.opacity = '1';
        alert(currentLang === 'zh' ? '操作失败，请稍后再试' : 'Operation failed, please try again');
    });
}

// ---------------------------------------------------------------------
// Skill viewer / editor
// ---------------------------------------------------------------------

/**
 * Skills are addressed by name, not by path: which file a name resolves to is
 * the loader's business, and a builtin skill lives outside the workspace that
 * the file APIs are confined to.
 */
async function skillReadContent(name) {
    const res = await fetch(`/api/skills/content?name=${encodeURIComponent(name)}`);
```
</details>

### symbol `toggleSkill` (console lines 12080–12080, base lines 9172–9175)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
        return true;
```
</details>

<details><summary>upstream's version of `toggleSkill`</summary>

```javascript
function toggleSkill(name, currentlyEnabled) {
    const action = currentlyEnabled ? 'close' : 'open';
    const card = document.querySelector(`[data-skill-name="${CSS.escape(name)}"]`);
    if (card) card.style.opacity = '0.5';

    fetch('/api/skills', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, name })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            if (card) {
                card.dataset.enabled = currentlyEnabled ? '0' : '1';
                card.style.opacity = '1';
                renderSkillCard(card, {
                    name: name,
                    description: card.dataset.skillDesc || '',
                    display_name: card.dataset.skillDisplayName || '',
                    enabled: !currentlyEnabled,
                });
            }
        } else {
            if (card) card.style.opacity = '1';
            alert(currentLang === 'zh' ? '操作失败，请稍后再试' : 'Operation failed, please try again');
        }
    })
    .catch(() => {
        if (card) card.style.opacity = '1';
        alert(currentLang === 'zh' ? '操作失败，请稍后再试' : 'Operation failed, please try again');
    });
}

// ---------------------------------------------------------------------
// Skill viewer / editor
// ---------------------------------------------------------------------

/**
 * Skills are addressed by name, not by path: which file a name resolves to is
 * the loader's business, and a builtin skill lives outside the workspace that
 * the file APIs are confined to.
 */
async function skillReadContent(name) {
    const res = await fetch(`/api/skills/content?name=${encodeURIComponent(name)}`);
```
</details>

## `channel/web/static/js/views/tasks.js` — 4 region(s)

### symbol `runKey` (console lines 17123–17123, base lines 12629–12629)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
                body: JSON.stringify({task_id: task.id, run_key: runKey, agent_id: task.agent_id || ''})
```
</details>

### symbol `owner` (console lines 17250–17250, base lines 12726–12726)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
                    ${caps.run ? `<button type="button" class="task-run-now px-2 py-1 rounded-md text-primary-500 hover:bg-primary-50 dark:hover:bg-primary-500/10 transition-colors">
```
</details>

<details><summary>upstream's version of `owner`</summary>

```javascript
    const owner = (multiAgentMode() && run.agent_id) ? findAgent(run.agent_id) : null;
    const ownerChip = owner
        ? `<span class="inline-flex items-center gap-1 pl-1 pr-1.5 py-0.5 rounded-full bg-slate-100 dark:bg-white/10 text-[10px] leading-none text-slate-400 dark:text-slate-500">${agentAvatarHTML(owner, 15)}<span class="truncate max-w-[80px]">${escapeHtml(owner.name || owner.id)}</span></span>`
        : '';
    const trigger = run.trigger === 'manual' ? t('records_trigger_manual') : t('records_trigger_scheduled');
    const duration = formatRunDuration(run.started_at, run.ended_at);
    const bodyLine = (run.status === 'error' && run.error)
        ? `<p class="text-xs text-red-500 mb-2 line-clamp-2 break-words">${escapeHtml(run.error)}</p>`
        : `<p class="text-xs text-slate-500 dark:text-slate-400 mb-2 line-clamp-2 break-words">${run.output_preview ? escapeHtml(run.output_preview) : `<span class="italic text-slate-400">${t('records_no_output')}</span>`}</p>`;
    card.innerHTML = `
        <div class="flex items-center gap-2 mb-1.5">
            ${runStatusBadge(run.status)}
            <span class="font-medium text-sm text-slate-700 dark:text-slate-200 truncate">${escapeHtml(run.task_name || run.task_id || t('tasks_tab_records'))}</span>
            ${ownerChip}
            <div class="flex-1"></div>
            <span class="text-[10px] px-1.5 py-0.5 rounded-full bg-slate-100 dark:bg-white/10 text-slate-400 dark:text-slate-500">${escapeHtml(trigger)}</span>
            <button class="run-delete-btn text-slate-300 hover:text-red-500 dark:text-slate-600 dark:hover:text-red-400 transition-colors px-1" title="${t('records_delete')}"><i class="fas fa-trash-can text-xs"></i></button>
        </div>
        ${bodyLine}
        <div class="flex items-center gap-2 text-xs text-slate-400 dark:text-slate-500">
            <i class="fas fa-clock"></i><span>${formatRunTime(run.started_at)}</span>
            ${duration ? `<span class="opacity-50">·</span><span>${t('records_duration')} ${duration}</span>` : ''}
        </div>`;
    card.addEventListener('click', () => showRunDetailModal(run));
    const delBtn = card.querySelector('.run-delete-btn');
    if (delBtn) {
        delBtn.addEventListener('click', (e) => {
            e.stopPropagation();   // don't open the detail modal
            deleteRunRecord(run, card);
        });
    }
    return card;
}

// Confirm, then delete one execution record and drop its card from the list.
function deleteRunRecord(run, card) {
    showConfirmDialog({
        title: t('records_delete_confirm_title'),
        message: t('records_delete_confirm_msg'),
        okText: t('records_delete'),
        onConfirm: () => {
            fetch('/api/scheduler/runs/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ run_id: run.run_id })
```
</details>

### symbol `owner` (console lines 17252–17253, base lines 12728–12729)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
                    </button>` : ''}
                    ${caps.manage ? `<label class="relative inline-flex items-center cursor-pointer" for="${toggleId}">
```
</details>

<details><summary>upstream's version of `owner`</summary>

```javascript
    const owner = (multiAgentMode() && run.agent_id) ? findAgent(run.agent_id) : null;
    const ownerChip = owner
        ? `<span class="inline-flex items-center gap-1 pl-1 pr-1.5 py-0.5 rounded-full bg-slate-100 dark:bg-white/10 text-[10px] leading-none text-slate-400 dark:text-slate-500">${agentAvatarHTML(owner, 15)}<span class="truncate max-w-[80px]">${escapeHtml(owner.name || owner.id)}</span></span>`
        : '';
    const trigger = run.trigger === 'manual' ? t('records_trigger_manual') : t('records_trigger_scheduled');
    const duration = formatRunDuration(run.started_at, run.ended_at);
    const bodyLine = (run.status === 'error' && run.error)
        ? `<p class="text-xs text-red-500 mb-2 line-clamp-2 break-words">${escapeHtml(run.error)}</p>`
        : `<p class="text-xs text-slate-500 dark:text-slate-400 mb-2 line-clamp-2 break-words">${run.output_preview ? escapeHtml(run.output_preview) : `<span class="italic text-slate-400">${t('records_no_output')}</span>`}</p>`;
    card.innerHTML = `
        <div class="flex items-center gap-2 mb-1.5">
            ${runStatusBadge(run.status)}
            <span class="font-medium text-sm text-slate-700 dark:text-slate-200 truncate">${escapeHtml(run.task_name || run.task_id || t('tasks_tab_records'))}</span>
            ${ownerChip}
            <div class="flex-1"></div>
            <span class="text-[10px] px-1.5 py-0.5 rounded-full bg-slate-100 dark:bg-white/10 text-slate-400 dark:text-slate-500">${escapeHtml(trigger)}</span>
            <button class="run-delete-btn text-slate-300 hover:text-red-500 dark:text-slate-600 dark:hover:text-red-400 transition-colors px-1" title="${t('records_delete')}"><i class="fas fa-trash-can text-xs"></i></button>
        </div>
        ${bodyLine}
        <div class="flex items-center gap-2 text-xs text-slate-400 dark:text-slate-500">
            <i class="fas fa-clock"></i><span>${formatRunTime(run.started_at)}</span>
            ${duration ? `<span class="opacity-50">·</span><span>${t('records_duration')} ${duration}</span>` : ''}
        </div>`;
    card.addEventListener('click', () => showRunDetailModal(run));
    const delBtn = card.querySelector('.run-delete-btn');
    if (delBtn) {
        delBtn.addEventListener('click', (e) => {
            e.stopPropagation();   // don't open the detail modal
            deleteRunRecord(run, card);
        });
    }
    return card;
}

// Confirm, then delete one execution record and drop its card from the list.
function deleteRunRecord(run, card) {
    showConfirmDialog({
        title: t('records_delete_confirm_title'),
        message: t('records_delete_confirm_msg'),
        okText: t('records_delete'),
        onConfirm: () => {
            fetch('/api/scheduler/runs/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ run_id: run.run_id })
```
</details>

### symbol `owner` (console lines 17256–17256, base lines 12732–12732)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
                    </label>` : ''}
```
</details>

<details><summary>upstream's version of `owner`</summary>

```javascript
    const owner = (multiAgentMode() && run.agent_id) ? findAgent(run.agent_id) : null;
    const ownerChip = owner
        ? `<span class="inline-flex items-center gap-1 pl-1 pr-1.5 py-0.5 rounded-full bg-slate-100 dark:bg-white/10 text-[10px] leading-none text-slate-400 dark:text-slate-500">${agentAvatarHTML(owner, 15)}<span class="truncate max-w-[80px]">${escapeHtml(owner.name || owner.id)}</span></span>`
        : '';
    const trigger = run.trigger === 'manual' ? t('records_trigger_manual') : t('records_trigger_scheduled');
    const duration = formatRunDuration(run.started_at, run.ended_at);
    const bodyLine = (run.status === 'error' && run.error)
        ? `<p class="text-xs text-red-500 mb-2 line-clamp-2 break-words">${escapeHtml(run.error)}</p>`
        : `<p class="text-xs text-slate-500 dark:text-slate-400 mb-2 line-clamp-2 break-words">${run.output_preview ? escapeHtml(run.output_preview) : `<span class="italic text-slate-400">${t('records_no_output')}</span>`}</p>`;
    card.innerHTML = `
        <div class="flex items-center gap-2 mb-1.5">
            ${runStatusBadge(run.status)}
            <span class="font-medium text-sm text-slate-700 dark:text-slate-200 truncate">${escapeHtml(run.task_name || run.task_id || t('tasks_tab_records'))}</span>
            ${ownerChip}
            <div class="flex-1"></div>
            <span class="text-[10px] px-1.5 py-0.5 rounded-full bg-slate-100 dark:bg-white/10 text-slate-400 dark:text-slate-500">${escapeHtml(trigger)}</span>
            <button class="run-delete-btn text-slate-300 hover:text-red-500 dark:text-slate-600 dark:hover:text-red-400 transition-colors px-1" title="${t('records_delete')}"><i class="fas fa-trash-can text-xs"></i></button>
        </div>
        ${bodyLine}
        <div class="flex items-center gap-2 text-xs text-slate-400 dark:text-slate-500">
            <i class="fas fa-clock"></i><span>${formatRunTime(run.started_at)}</span>
            ${duration ? `<span class="opacity-50">·</span><span>${t('records_duration')} ${duration}</span>` : ''}
        </div>`;
    card.addEventListener('click', () => showRunDetailModal(run));
    const delBtn = card.querySelector('.run-delete-btn');
    if (delBtn) {
        delBtn.addEventListener('click', (e) => {
            e.stopPropagation();   // don't open the detail modal
            deleteRunRecord(run, card);
        });
    }
    return card;
}

// Confirm, then delete one execution record and drop its card from the list.
function deleteRunRecord(run, card) {
    showConfirmDialog({
        title: t('records_delete_confirm_title'),
        message: t('records_delete_confirm_msg'),
        okText: t('records_delete'),
        onConfirm: () => {
            fetch('/api/scheduler/runs/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ run_id: run.run_id })
```
</details>

## `channel/web/static/css/sessions.css` — 4 region(s)

### symbol `?` (console lines 66–97, base lines 66–65)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
.sidebar-brand {
    display: flex;
    align-items: center;
    gap: 11px;
    height: 56px;
    padding: 0 20px;
    flex-shrink: 0;
    border-bottom: 1px solid rgba(255, 255, 255, .1);
}
.brand-mark {
    display: block;
    flex-shrink: 0;
    object-fit: contain;
}
.sidebar-brand-mark { width: 34px; height: 34px; }
.sidebar-brand-copy { display: flex; flex-direction: column; min-width: 0; gap: 3px; }
.brand-wordmark {
    color: #172b3a;
    font-family: 'Inter', 'PingFang SC', 'Microsoft YaHei', system-ui, sans-serif;
    font-size: 19px;
    font-weight: 700;
    letter-spacing: .025em;
    line-height: 1.1;
    white-space: nowrap;
}
.brand-ai { color: #0788b5; margin-left: 3px; letter-spacing: -.025em; }
.dark .brand-wordmark, .sidebar-brand .brand-wordmark { color: #f3f8fc; }
.dark .brand-ai, .sidebar-brand .brand-ai { color: #5cd7ff; }
.sidebar-brand-caption { color: #8b959f; font-size: 10px; line-height: 1.1; letter-spacing: .12em; }
.brand-mark-hero { width: 64px; height: 64px; }
.brand-wordmark-hero { font-size: 28px; line-height: 1.25; }

```
</details>

### symbol `?` (console lines 1900–1903, base lines 603–605)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
/* Archived sessions dialog (reuses the confirm overlay shell) */
.archived-sessions-modal {
    width: 520px;
    max-width: 92vw;
```
</details>

### symbol `?` (console lines 1937–1943, base lines 633–632)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
.archived-session-row:hover { background: rgba(0, 0, 0, .03); }
.dark .archived-session-row:hover { background: rgba(255, 255, 255, .05); }
.archived-session-copy {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
```
</details>

### symbol `?` (console lines 1945–1985, base lines 634–633)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
.archived-session-title {
    font-size: 13px;
    color: #1f2937;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.dark .archived-session-title { color: #e5e7eb; }
.archived-session-meta {
    font-size: 11px;
    color: #9ca3af;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.archived-session-time {
    font-size: 11px;
    color: #9ca3af;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.dark .archived-session-time { color: #6b7280; }
.archived-session-restore {
    flex-shrink: 0;
    padding: 6px 14px;
    border: 0;
    border-radius: 8px;
    background: #f3f4f6;
    color: #374151;
    font-size: 13px;
    font-weight: 500;
    cursor: pointer;
    transition: background .15s ease;
}
.archived-session-restore:hover { background: #e5e7eb; }
.dark .archived-session-restore {
    background: rgba(255, 255, 255, .08);
    color: #d1d5db;
}
```
</details>

## `(no owning module)` — 3 region(s)

### symbol `pickComposerAgent` (console lines 4413–4415, base lines 3037–3043)

- reason deferred: fork edit crosses an upstream module boundary
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    // Use the same guarded, freshly validated start as the workbench. A cancel
    // must keep the current owner, and a switch must not inherit its project.
    return startChatWithAgent(agentId);
```
</details>

### symbol `current` (console lines 5053–5092, base lines 3609–3610)

- reason deferred: fork edit crosses an upstream module boundary
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
// Load the public brand snapshot and apply it once the DOM is ready. This
// drives the sidebar / login / welcome / favicon / title from the SAME source
// the published brand uses, independent of the legacy /config.title projection.
function fetchPublicBrand(seq) {
    const requestSeq = (seq != null ? seq : ++brandFetchSeq);
    return fetch('/api/branding/public').then(r => r.json()).then(data => {
        // A later public read must not clobber a version published by THIS tab
        // after it was issued.
        if (requestSeq < brandSaveEpoch) return;
        if (data && data.brand_name) {
            if (requestSeq < brandFetchSeq) return; // a newer read already landed
            brandState = {
                enabled: !!data.enabled,
                revision: data.revision || 0,
                brand_name: data.brand_name || DEFAULT_BRAND.brand_name,
                logo_description: (data.logo_description != null) ? data.logo_description : '',
                logo_url: data.logo_url || DEFAULT_BRAND.logo_url,
                favicon_url: data.favicon_url || DEFAULT_BRAND.favicon_url,
            };
            brandLoaded = true;
            // Keep the legacy config title in sync so any code reading it stays
            // consistent without a second source of truth.
            if (appConfig) appConfig.title = productTitle(brandState.brand_name);
        }
        // The 「帮助与关于」 target travels in this projection. It is an
        // instance-level fact independent of the brand record, so it is adopted
        // even when the payload carries no usable brand name; an unusable or
        // absent value keeps the local-development default.
        if (data) _applyAccountAboutUrl(data.help_url);
        applyBrandToDocument();
        applyBrandToAgentAvatars();
    }).catch(() => { /* keep last known brand; never break the console */ });
}

// Fetch immediately and re-validate on visibility / view entry.
fetchPublicBrand();
document.addEventListener('DOMContentLoaded', applyBrandToDocument);
document.addEventListener('visibilitychange', () => {
    if (!document.hidden) fetchPublicBrand();
});
```
</details>

### symbol `_resolveOneShotTenantSwitch` (console lines 19816–19822, base lines 13994–14006)

- reason deferred: fork edit crosses an upstream module boundary
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
// Wire the change-password form submit (single submission).
(function () {
    const form = document.getElementById('account-password-form');
    if (form) form.addEventListener('submit', submitAccountPassword);
})();

refreshAccountIdentity();
```
</details>

## `channel/web/static/js/chat/session-settings.js` — 2 region(s)

### symbol `refreshSessionSettings` (console lines 6300–6299, base lines 4739–4739)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
```
</details>

<details><summary>upstream's version of `refreshSessionSettings`</summary>

```javascript
async function refreshSessionSettings() {
    try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/settings`);
        const data = await res.json();
        if (data.status !== 'success') return;
        _sessCfg = { model: data.model, permission: data.permission, team: data.team };
    } catch (e) {
        // Keep whatever the chips already show rather than blanking them.
        return;
    }
    _renderPermissionChip();
    _renderModelChip();
    _renderInputPlaceholder();
    renderComposerIdentity();
}

function _renderPermissionChip() {
    const btn = _permBtn();
    if (!btn || !_sessCfg) return;
    const state = _sessCfg.permission || {};
    const mode = state.mode || 'full-access';
    const meta = PERMISSION_META[mode] || PERMISSION_META['full-access'];

    const label = document.getElementById('permission-selector-label');
    if (label) label.textContent = _permLabel(mode);
    const icon = document.getElementById('permission-selector-icon');
    if (icon) icon.className = `fas ${meta.icon}`;

    // One colour per mode, so an unrestricted session is visibly different from
    // a read-only one without having to read the label.
    btn.classList.remove('perm-read-only', 'perm-workspace-write', 'perm-full-access');
    btn.classList.add(`perm-${mode}`);

    const tip = t('perm_tip').replace('{name}', _permLabel(mode))
        + (state.source === 'global' ? ` · ${t('perm_follow_global')}` : '');
    btn.setAttribute('data-tooltip', tip);
    btn.setAttribute('data-tooltip-pos', 'top');
    btn.setAttribute('data-tip-float', '');
}

// The composer placeholder only advertises "@ an Agent" when the conversation
// actually has other members to address. A solo chat can only @ files, so it
// falls back to the file-only hint. Runs whenever the session's team changes.
function _renderInputPlaceholder() {
    const input = document.getElementById('chat-input');
```
</details>

### symbol `refreshSessionSettings` (console lines 6302–6301, base lines 4742–4765)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
```
</details>

<details><summary>upstream's version of `refreshSessionSettings`</summary>

```javascript
async function refreshSessionSettings() {
    try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/settings`);
        const data = await res.json();
        if (data.status !== 'success') return;
        _sessCfg = { model: data.model, permission: data.permission, team: data.team };
    } catch (e) {
        // Keep whatever the chips already show rather than blanking them.
        return;
    }
    _renderPermissionChip();
    _renderModelChip();
    _renderInputPlaceholder();
    renderComposerIdentity();
}

function _renderPermissionChip() {
    const btn = _permBtn();
    if (!btn || !_sessCfg) return;
    const state = _sessCfg.permission || {};
    const mode = state.mode || 'full-access';
    const meta = PERMISSION_META[mode] || PERMISSION_META['full-access'];

    const label = document.getElementById('permission-selector-label');
    if (label) label.textContent = _permLabel(mode);
    const icon = document.getElementById('permission-selector-icon');
    if (icon) icon.className = `fas ${meta.icon}`;

    // One colour per mode, so an unrestricted session is visibly different from
    // a read-only one without having to read the label.
    btn.classList.remove('perm-read-only', 'perm-workspace-write', 'perm-full-access');
    btn.classList.add(`perm-${mode}`);

    const tip = t('perm_tip').replace('{name}', _permLabel(mode))
        + (state.source === 'global' ? ` · ${t('perm_follow_global')}` : '');
    btn.setAttribute('data-tooltip', tip);
    btn.setAttribute('data-tooltip-pos', 'top');
    btn.setAttribute('data-tip-float', '');
}

// The composer placeholder only advertises "@ an Agent" when the conversation
// actually has other members to address. A solo chat can only @ files, so it
// falls back to the file-only hint. Runs whenever the session's team changes.
function _renderInputPlaceholder() {
    const input = document.getElementById('chat-input');
```
</details>

## `channel/web/static/js/core/theme.js` — 2 region(s)

### symbol `hideTip` (console lines 1193–1197, base lines 1516–1516)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
// The pre-paint controller owns state; this is a resolved-mode projection for
// existing console consumers, not a second persisted preference.
let currentTheme = (window.CowAppearance && window.CowAppearance.getState
    && window.CowAppearance.getState().resolved) || 'light';
let appearanceTrigger = null;
```
</details>

### symbol `toggleTheme` (console lines 1237–1284, base lines 1534–1536)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    window.CowAppearance.setMode(currentTheme === 'dark' ? 'light' : 'dark');
}

function openAppearancePreferences(trigger) {
    const dialog = document.getElementById('appearance-dialog');
    if (!dialog || dialog.open) return;
    if (!closeAccountPanels(false)) return;
    appearanceTrigger = trigger || document.activeElement;
    closeAccountMenu();
    ['tenant-menu'].forEach(id => _accountHidden(id, true));
    renderAppearancePreferences();
    dialog.showModal();
    _setAccountPanel('prefs');
    dialog.querySelector('input[name="web-palette"]:checked')?.focus();
}

function closeAppearancePreferences(returnFocus = true) {
    const dialog = document.getElementById('appearance-dialog');
    if (dialog?.open) {
        if (!returnFocus) appearanceTrigger = null;
        if (_activeAccountPanel === 'prefs') _setAccountPanel(null);
        dialog.close();
    }
}

const appearanceDialog = document.getElementById('appearance-dialog');
if (appearanceDialog) {
    appearanceDialog.addEventListener('close', () => {
        if (_activeAccountPanel === 'prefs') _setAccountPanel(null);
        // A login transition has its own focus target; do not focus the
        // now-hidden workbench after its modal is dismissed.
        if (!appearanceTrigger) return;
        if (appearanceTrigger?.isConnected && appearanceTrigger.getClientRects().length) appearanceTrigger.focus();
        else {
            // The trigger itself can be gone; return focus to the account entry
            // that owns the preference panel, but never to a hidden workbench.
            const fallback = document.getElementById('sidebar-account-toggle');
            if (fallback && fallback.getClientRects().length) fallback.focus();
        }
        appearanceTrigger = null;
```
</details>

<details><summary>upstream's version of `toggleTheme`</summary>

```javascript
function toggleTheme() {
    currentTheme = currentTheme === 'dark' ? 'light' : 'dark';
    localStorage.setItem('cow_theme', currentTheme);
    applyTheme();
}

```
</details>

## `channel/web/static/js/core/version.js` — 2 region(s)

### symbol `?` (console lines 2–2, base lines 2–2)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
   容大AI Console - Main Application Script
```
</details>

### symbol `?` (console lines 10–901, base lines 9–8)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript

// Startup reads must not stop the UI when browser storage is unavailable.
// Business writes and authentication storage keep their existing behavior.
function readStartupPreference(key) {
    try { return localStorage.getItem(key); } catch (_) { return null; }
}

// Task 3.7 — user/tenant-scoped storage partition for the Agent/session
// selection keys. Keys are namespaced by the confirmed user and tenant so a
// different account or tenant on the same browser never restores the wrong
// context. The namespace is only computable once the identity and the effective
// tenant are known, so callers that restore these identifiers must wait for
// authentication + tenant select.
function _cowUserTenantKey(key) {
    const uid = (_accountState && _accountState.username) ? _accountState.username : '';
    const tid = sessionStorage.getItem('cow_tenant_id') || '';
    if (!uid && !tid) return key;      // not confirmed yet -> unpartitioned key
    return `${key}::u=${encodeURIComponent(uid)}::t=${encodeURIComponent(tid)}`;
}

// Read a user/tenant-scoped selection key. Once the account is authenticated,
// do not carry an old unpartitioned value into the new context. Before the
// context is confirmed, the unpartitioned key is allowed as the initial value.
function readScopedPreference(key) {
    try {
        const scoped = _cowUserTenantKey(key);
        const v = localStorage.getItem(scoped);
        if (v !== null) return v;
        if (_accountState && _accountState.authenticated) {
            return null;
        }
        return localStorage.getItem(key);
    } catch (_) { return null; }
}

function writeScopedPreference(key, value) {
    const scoped = _cowUserTenantKey(key);
    try { localStorage.setItem(scoped, value); } catch (_) {}
}

```
</details>

## `channel/web/static/js/views/channels-feishu.js` — 2 region(s)

### symbol `pollFeishuRegisterStatus` (console lines 17017–17023, base lines 12542–12541)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
                if (_feishuScanTarget) {
                    // A tenant scan persists itself: the grant the server minted
                    // for this scan authorizes the create, so there is no second
                    // step and no password prompt to lose the channel to.
                    applyFeishuScanToTenantForm(data.app_id, data.app_secret, data.scan_ticket);
                    autoPersistScannedTenantChannel();
                } else {
```
</details>

<details><summary>upstream's version of `pollFeishuRegisterStatus`</summary>

```javascript
function pollFeishuRegisterStatus(statusId) {
    stopFeishuRegisterPoll();
    _feishuRegisterPollTimer = setTimeout(() => {
        fetch('/api/feishu/register', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'poll' })
        })
        .then(r => r.json())
        .then(data => {
            if (data.status !== 'success') {
                renderFeishuRegisterError(statusId, data.message || t('feishu_scan_fail'));
                return;
            }
            const rs = data.register_status;
            if (rs === 'downloading') {
                renderFeishuSdkDownloading(statusId);
                pollFeishuRegisterStatus(statusId);
                return;
            }
            // The QR may only be generated after the bundle downloaded, in
            // which case the initial GET could not carry it. Render it once;
            // repainting on every poll would make it flicker.
            const shown = document.getElementById(statusId);
            if ((data.qr_image || data.qrcode_url) && shown && !shown.querySelector('img')) {
                renderFeishuQr(statusId, data.qr_image, data.qrcode_url);
            }
            if (rs === 'done') {
                const statusEl = document.getElementById(statusId);
                if (statusEl) {
                    statusEl.innerHTML = `
                        <div class="flex flex-col items-center py-2">
                            <div class="w-10 h-10 rounded-full bg-emerald-50 dark:bg-emerald-900/30 flex items-center justify-center mb-2">
                                <i class="fas fa-check text-emerald-500 text-lg"></i>
                            </div>
                            <p class="text-sm font-medium text-emerald-600 dark:text-emerald-400">${t('feishu_scan_success')}</p>
                        </div>`;
                }
                connectFeishuAfterRegister(data.app_id, data.app_secret);
            } else if (rs === 'expired') {
                renderFeishuRegisterError(statusId, t('feishu_scan_expired'));
            } else if (rs === 'denied') {
                renderFeishuRegisterError(statusId, t('feishu_scan_denied'));
            } else if (rs === 'error') {
                renderFeishuRegisterError(statusId, data.message || t('feishu_scan_fail'));
```
</details>

### symbol `pollFeishuRegisterStatus` (console lines 17025–17027, base lines 12543–12542)

- reason deferred: splice left the module unparseable
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
                }
                _feishuRegisterHandle = '';
                _feishuScanTarget = '';
```
</details>

<details><summary>upstream's version of `pollFeishuRegisterStatus`</summary>

```javascript
function pollFeishuRegisterStatus(statusId) {
    stopFeishuRegisterPoll();
    _feishuRegisterPollTimer = setTimeout(() => {
        fetch('/api/feishu/register', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'poll' })
        })
        .then(r => r.json())
        .then(data => {
            if (data.status !== 'success') {
                renderFeishuRegisterError(statusId, data.message || t('feishu_scan_fail'));
                return;
            }
            const rs = data.register_status;
            if (rs === 'downloading') {
                renderFeishuSdkDownloading(statusId);
                pollFeishuRegisterStatus(statusId);
                return;
            }
            // The QR may only be generated after the bundle downloaded, in
            // which case the initial GET could not carry it. Render it once;
            // repainting on every poll would make it flicker.
            const shown = document.getElementById(statusId);
            if ((data.qr_image || data.qrcode_url) && shown && !shown.querySelector('img')) {
                renderFeishuQr(statusId, data.qr_image, data.qrcode_url);
            }
            if (rs === 'done') {
                const statusEl = document.getElementById(statusId);
                if (statusEl) {
                    statusEl.innerHTML = `
                        <div class="flex flex-col items-center py-2">
                            <div class="w-10 h-10 rounded-full bg-emerald-50 dark:bg-emerald-900/30 flex items-center justify-center mb-2">
                                <i class="fas fa-check text-emerald-500 text-lg"></i>
                            </div>
                            <p class="text-sm font-medium text-emerald-600 dark:text-emerald-400">${t('feishu_scan_success')}</p>
                        </div>`;
                }
                connectFeishuAfterRegister(data.app_id, data.app_secret);
            } else if (rs === 'expired') {
                renderFeishuRegisterError(statusId, t('feishu_scan_expired'));
            } else if (rs === 'denied') {
                renderFeishuRegisterError(statusId, t('feishu_scan_denied'));
            } else if (rs === 'error') {
                renderFeishuRegisterError(statusId, data.message || t('feishu_scan_fail'));
```
</details>

## `channel/web/static/js/views/memory.js` — 2 region(s)

### symbol `switchMemoryTab` (console lines 12432–12472, base lines 9286–9285)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
/**
 * Render a refused memory read as a terminal state instead of an empty folder.
 *
 * Both memory reads used to `return` on `status !== 'success'`, which left the
 * previous rows (or the "loading" copy) on screen and swallowed the catch: a
 * 403, a 503 and "this Agent has no memory files yet" all ended up looking the
 * same, and the console could not say which one happened. The reason now comes
 * from the server payload and the stale rows are cleared, so what is displayed
 * is the answer to the request that was actually made.
 *
 * `keepList` is for one *file* failing to open: the list is still the correct
 * answer for the list request and must not be wiped over a single read.
 */
function _memoryRefusal(data, opts) {
    const keepList = !!(opts && opts.keepList);
    const message = (data && typeof data.message === 'string' && data.message.trim())
        ? data.message.trim()
        : (currentLang === 'zh' ? '读取记忆失败' : 'Failed to read memory');
    const emptyEl = document.getElementById('memory-empty');
    const listEl = document.getElementById('memory-list');
    const pagEl = document.getElementById('memory-pagination');
    const tbody = document.getElementById('memory-table-body');
    if (tbody) tbody.innerHTML = '';
    if (pagEl) pagEl.innerHTML = '';
    if (!keepList && listEl) listEl.classList.add('hidden');
    if (emptyEl) {
        const icon = emptyEl.querySelector('i');
        const title = emptyEl.querySelector('p');
        const hint = emptyEl.querySelectorAll('p')[1];
        if (icon) icon.className = 'fas fa-triangle-exclamation text-amber-500 text-xl';
        if (title) title.textContent = message;
        if (hint) {
            hint.textContent = currentLang === 'zh'
                ? '这不是「暂无记忆」；原因来自服务端。' : 'This is not "no memory files"; the reason comes from the server.';
        }
        if (!keepList) emptyEl.classList.remove('hidden');
    }
    _wsToast(message);
    return message;
}
```
</details>

<details><summary>upstream's version of `switchMemoryTab`</summary>

```javascript
function switchMemoryTab(tab) {
    document.querySelectorAll('.memory-tab').forEach(el => el.classList.remove('active'));
    document.getElementById('memory-tab-' + tab).classList.add('active');
    // The "dreams" tab now surfaces self-evolution logs (merged with dream diaries).
    memoryCategory = tab === 'dreams' ? 'evolution' : 'memory';
    loadMemoryView(1);
    routeNoteTab('memory', tab);
}

function loadMemoryView(page) {
    page = page || 1;
    memoryPage = page;
    const agent = viewingMemoryAgentId();
    fetch(`/api/memory?page=${page}&page_size=${memoryPageSize}&category=${memoryCategory}&agent_id=${encodeURIComponent(agent || '')}`).then(r => r.json()).then(data => {
        if (data.status !== 'success') return;
        const emptyEl = document.getElementById('memory-empty');
        const listEl = document.getElementById('memory-list');
        const files = data.list || [];
        const total = data.total || 0;

        if (total === 0) {
            const emptyIcon = emptyEl.querySelector('i');
            const emptyTitle = emptyEl.querySelector('p');
            if (memoryCategory === 'evolution') {
                emptyIcon.className = 'fas fa-seedling text-emerald-400 text-xl';
                emptyTitle.textContent = currentLang === 'zh' ? '暂无进化记录' : 'No evolution records yet';
            } else {
                emptyIcon.className = 'fas fa-brain text-purple-400 text-xl';
                emptyTitle.textContent = currentLang === 'zh' ? '暂无记忆文件' : 'No memory files';
            }
            emptyEl.classList.remove('hidden');
            listEl.classList.add('hidden');
            return;
        }
        emptyEl.classList.add('hidden');
        listEl.classList.remove('hidden');

        const tbody = document.getElementById('memory-table-body');
        tbody.innerHTML = '';
        files.forEach(f => {
            const tr = document.createElement('tr');
            tr.className = 'border-b border-slate-100 dark:border-white/5 hover:bg-slate-50 dark:hover:bg-white/5 cursor-pointer transition-colors';
            // In the merged evolution tab, resolve each file by its own origin
            // (evolution logs vs dream diaries live in different dirs).
            const fileCategory = (f.type === 'dream' || f.type === 'evolution') ? f.type : memoryCategory;
```
</details>

### symbol `fileCategory` (console lines 12538–12538, base lines 9351–9351)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
    }).catch(() => _memoryRefusal(null));
```
</details>

<details><summary>upstream's version of `fileCategory`</summary>

```javascript
            const fileCategory = (f.type === 'dream' || f.type === 'evolution') ? f.type : memoryCategory;
            tr.onclick = () => openMemoryFile(f.filename, fileCategory);
            let typeLabel;
            if (f.type === 'global') {
                typeLabel = '<span class="px-2 py-0.5 rounded-full text-xs bg-primary-50 dark:bg-primary-900/30 text-primary-600 dark:text-primary-400">Global</span>';
            } else if (f.type === 'evolution') {
                typeLabel = '<span class="px-2 py-0.5 rounded-full text-xs bg-emerald-50 dark:bg-emerald-900/30 text-emerald-600 dark:text-emerald-400">Evolution</span>';
            } else if (f.type === 'dream') {
                typeLabel = '<span class="px-2 py-0.5 rounded-full text-xs bg-violet-50 dark:bg-violet-900/30 text-violet-600 dark:text-violet-400">Dream</span>';
            } else {
                typeLabel = '<span class="px-2 py-0.5 rounded-full text-xs bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400">Daily</span>';
            }
            const sizeStr = f.size < 1024 ? f.size + ' B' : (f.size / 1024).toFixed(1) + ' KB';
            tr.innerHTML = `
                <td class="px-4 py-3 text-sm font-mono text-slate-700 dark:text-slate-200">${escapeHtml(f.filename)}</td>
                <td class="px-4 py-3 text-sm">${typeLabel}</td>
                <td class="px-4 py-3 text-sm text-slate-500 dark:text-slate-400">${sizeStr}</td>
                <td class="px-4 py-3 text-sm text-slate-500 dark:text-slate-400">${escapeHtml(f.updated_at)}</td>`;
            tbody.appendChild(tr);
        });

        // Pagination
        const totalPages = Math.ceil(total / memoryPageSize);
        const pagEl = document.getElementById('memory-pagination');
        if (totalPages <= 1) { pagEl.innerHTML = ''; return; }
        let pagHtml = `<span>${page} / ${totalPages}</span><div class="flex gap-2">`;
        if (page > 1) pagHtml += `<button onclick="loadMemoryView(${page - 1})" class="px-3 py-1 rounded-lg border border-slate-200 dark:border-white/10 hover:bg-slate-100 dark:hover:bg-white/10 text-xs">Prev</button>`;
        if (page < totalPages) pagHtml += `<button onclick="loadMemoryView(${page + 1})" class="px-3 py-1 rounded-lg border border-slate-200 dark:border-white/10 hover:bg-slate-100 dark:hover:bg-white/10 text-xs">Next</button>`;
        pagHtml += '</div>';
        pagEl.innerHTML = pagHtml;
    }).catch(() => {});
}

```
</details>

## `channel/web/static/css/agents.css` — 2 region(s)

### symbol `?` (console lines 4782–5116, base lines 3378–3377)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript

/* ---- Role create/edit page (replaces modal for roles) ---- */
.role-editor {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
    background: #f8fafc;
}
.dark .role-editor { background: #121212; }
.role-editor.hidden { display: none; }
.role-editor-head {
    flex-shrink: 0;
    background: #fff;
    border-bottom: 1px solid #e2e8f0;
    padding: 16px 28px 0;
}
.dark .role-editor-head {
    background: #1A1A1A;
    border-bottom-color: rgba(255,255,255,.08);
}
.role-editor-back {
    border: 0;
    background: none;
    color: #64748b;
    font-size: 12px;
    padding: 0;
    margin-bottom: 10px;
    cursor: pointer;
}
.role-editor-back:hover { color: #0f172a; }
.dark .role-editor-back:hover { color: #f1f5f9; }
.role-editor-title-row {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 14px;
}
.role-editor-tabs {
```
</details>

### symbol `?` (console lines 5477–6048, base lines 3672–3671)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript

/* =================================================================== */
/* Agent Workbench (工作台 → 智能体)                                   */
/* =================================================================== */
.agent-workbench-view.view.active {
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
}
.agent-workbench-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    padding: 20px 22px 8px;
}
.agent-workbench-head h2 { font-size: 20px; font-weight: 700; }
.agent-workbench-head p { margin-top: 4px; font-size: 13px; }
.agent-workbench-status {
    padding: 6px 22px;
    font-size: 13px;
    color: #64748b;
}
.agent-workbench-status:not(.agent-workbench-status-hidden) { opacity: 1; }
.dark .agent-workbench-status { color: #94a3b8; }
.agent-workbench-status-hidden { display: none; }
.agent-workbench-status-error { color: #d9534f; }
.dark .agent-workbench-status-error { color: #f3a8a4; }
.agent-workbench-grid {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 6px 22px 22px;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
    gap: 14px;
    align-content: start;
}
.agent-wb-card {
```
</details>

## `channel/web/static/css/base.css` — 2 region(s)

### symbol `?` (console lines 2–2, base lines 2–2)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
   容大AI Console Styles
```
</details>

### symbol `?` (console lines 1988–1989, base lines 636–637)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
.menu-group-items { max-height: 0; overflow: hidden; transition: max-height 0.25s ease-out; visibility: hidden; }
.menu-group.open .menu-group-items { max-height: 2000px; transition: max-height 0.35s ease-in; visibility: visible; }
```
</details>

## `channel/web/static/js/chat/composer-input.js` — 1 region(s)

### symbol `selectSlashCommand` (console lines 6806–6806, base lines 5320–5337)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
bindWelcomeSuggestions(messagesDiv);
```
</details>

<details><summary>upstream's version of `selectSlashCommand`</summary>

```javascript
function selectSlashCommand(idx) {
    if (idx < 0 || idx >= slashFiltered.length) return;
    const chosen = slashFiltered[idx].cmd;
    slashJustSelected = true;
    chatInput.value = chosen;
    chatInput.dispatchEvent(new Event('input'));
    hideSlashMenu();
    chatInput.focus();
    chatInput.selectionStart = chatInput.selectionEnd = chosen.length;
}

chatInput.addEventListener('input', function() {
    autoResizeComposer();
    updateSendBtnState();
    // Reveal/hide the steer button as the user types during a running turn.
    updateSteerBtnState();

    const val = this.value;
    if (slashJustSelected) {
        slashJustSelected = false;
    } else if (val.startsWith('/')) {
        showSlashMenu(val);
    } else {
        hideSlashMenu();
    }
});

chatInput.addEventListener('keydown', function(e) {
    if (e.keyCode === 229 || e.isComposing || isComposing) return;

    if (e.key === 'Escape' && isAttachMenuVisible()) {
        hideAttachMenu();
        return;
    }

    if (isSlashMenuVisible()) {
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            slashNavByKeyboard = true;
            slashActiveIdx = Math.min(slashActiveIdx + 1, slashFiltered.length - 1);
            renderSlashItems();
            return;
        }
        if (e.key === 'ArrowUp') {
            e.preventDefault();
```
</details>

## `channel/web/static/js/chat/send.js` — 1 region(s)

### symbol `poll` (console lines 7926–7926, base lines 6394–6394)

- reason deferred: no verified context: upstream rewrote the surrounding code
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
                        sessionTitleOf(sessionId) || PRODUCT_NAME,
```
</details>

<details><summary>upstream's version of `poll`</summary>

```javascript
    function poll() {
        if (gen !== pollGeneration) return;
        if (pollInFlight) return;
        // Keep polling while hidden: push messages are exactly what the
        // notification below should deliver to a background tab.
        pollInFlight = true;
        fetch('/poll', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sessionId })
        })
        .then(r => r.json())
        .then(data => {
            pollInFlight = false;
            if (gen !== pollGeneration) return;
            if (data.status === 'success' && data.has_content) {
                const rid = data.request_id;
                if (loadingContainers[rid]) {
                    loadingContainers[rid].remove();
                    delete loadingContainers[rid];
                }
                // Skip if this reply is already on screen. Happens when a reply
                // arrives via both the SSE stream and the poll queue (e.g. the
                // user switched away mid-run, leaving the queued reply to be
                // re-fetched on return) — render it only once.
                const already = rid && messagesDiv.querySelector(
                    `[data-request-id="${rid}"]`
                );
                if (!already) {
                    const welcomeScreen = document.getElementById('welcome-screen');
                    if (welcomeScreen) welcomeScreen.remove();
                    addBotMessage(data.content, new Date(data.timestamp * 1000), rid);
                    scrollChatToBottom();
                    // Scheduler executions are notified by the global runs poll
                    // (maybeNotifyScheduledRun) — it forces a notice across ALL
                    // sessions, including this one and manual "run now", and
                    // dedupes by run id. Notifying here too would double-pop, so
                    // only notify for an ordinary missed reply.
                    if (!isSchedulerRequest(rid)) {
                    showTaskNotification(
                        sessionTitleOf(sessionId) || 'CowAgent',
                        firstLineSnippet(data.content),
                            sessionId,
                            activeAgentId
                    );
```
</details>

## `channel/web/static/js/chat/state.js` — 1 region(s)

### symbol `loadOrCreateSessionId` (console lines 5034–5035, base lines 3598–3597)

- reason deferred: no verified context around the insertion point
- decision: `TBD`

<details><summary>fork's version</summary>

```javascript
let _historyLoadSeq = 0;
let _historyLoadContext = '';
```
</details>

<details><summary>upstream's version of `loadOrCreateSessionId`</summary>

```javascript
function loadOrCreateSessionId() {
    const stored = localStorage.getItem(activeSessionStorageKey());
    if (stored) return stored;
    const fresh = generateSessionId();
    localStorage.setItem(activeSessionStorageKey(), fresh);
    return fresh;
}

let sessionId = loadOrCreateSessionId();

// ---- Conversation history state ----
let historyPage = 0;       // last page fetched (0 = nothing fetched yet)
let historyHasMore = false;
let historyLoading = false;


const chatInput = document.getElementById('chat-input');
const sendBtn = document.getElementById('send-btn');
const steerBtn = document.getElementById('steer-btn');
const messagesDiv = document.getElementById('chat-messages');
const fileInput = document.getElementById('file-input');
const folderInput = document.getElementById('folder-input');
const attachBtn = document.getElementById('attach-btn');
const attachMenu = document.getElementById('attach-menu');
const attachFolderOption = document.getElementById('attach-folder-option');
const supportsDirectoryUpload = !!folderInput && 'webkitdirectory' in folderInput;

if (!supportsDirectoryUpload && attachFolderOption) {
    attachFolderOption.classList.add('hidden');
}

// Composer textarea sizing. The empty box is deliberately tall (a few lines of
// room, like other coding agents) and grows with the text up to a cap, after
// which it scrolls.
const COMPOSER_MIN_H = 52;
const COMPOSER_MAX_H = 220;

function autoResizeComposer() {
    chatInput.style.height = COMPOSER_MIN_H + 'px';
    const scrollH = chatInput.scrollHeight;
    chatInput.style.height = Math.max(COMPOSER_MIN_H, Math.min(scrollH, COMPOSER_MAX_H)) + 'px';
    chatInput.style.overflowY = scrollH > COMPOSER_MAX_H ? 'auto' : 'hidden';
}

/** Shrink the composer back to its resting height after the text is consumed. */
```
</details>

