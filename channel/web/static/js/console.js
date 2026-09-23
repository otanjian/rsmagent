/* =====================================================================
   容大AI Console - Main Application Script
   ===================================================================== */

// =====================================================================
// Version — fetched from backend (single source: /VERSION file)
// =====================================================================
const PRODUCT_NAME = '容大AI';
let APP_VERSION = '';

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

function removeScopedPreference(key) {
    const scoped = _cowUserTenantKey(key);
    try { localStorage.removeItem(scoped); } catch (_) {}
}

// Sidebar account state
// UI-only identity: never retain the login response (which contains a token).
// Database identity is the only supported mode.
let _accountState = { phase: 'loading', mode: 'database', authRequired: null,
    authenticated: null, username: '', displayName: '', mustChangePassword: false };
let _authEpoch = 0;
let _accountCheckSeq = 0;
let _accountCheckRequest = null;
let _accountWritePending = null;
let _accountIdentityKey = null;
let _accountAppVisible = false;
let _accountEntryRequest = null;
let _pendingTenantPicker = false;
let _accountMenuOpen = false;
// Help is hosted on the current backend alongside the console.
const ACCOUNT_ABOUT_FALLBACK_URL = '/help/';
let _accountAboutUrl = ACCOUNT_ABOUT_FALLBACK_URL;
// The mobile account surface is a bottom sheet: below the sidebar breakpoint the
// one account panel DOM is hosted at the document root (outside the off-canvas
// sidebar's transform) and behaves as a modal dialog. Desktop keeps the anchored
// non-modal popover and its Tab-out behaviour.
let _accountMenuSheet = false;
// Current tenant's authoritative /auth/context capability summary, cached per
// account/epoch. Declared with the rest of the account state because identity
// invalidation has to drop it (and any late reply) in the same breath.
let _authContext = null;
let _authContextSeq = 0;
let _authContextRequest = null;
// 'unknown' until a tenant-scoped read is attempted, then 'checking' / 'ready' /
// 'failed'. It drives the account panel's 「我的资源」 checking and retry states,
// so an unconfirmed entry is never activatable and a failed read is not shown as
// an empty resource list.
let _authContextPhase = 'unknown';
let _forcedPassword = false;   // must_change_password: block tenant/business until set
// Bump on each successful self-avatar upload so the sidebar/menu/hero images
// refetch instead of serving the stale bytes from the same URL.
let _accountAvatarVersion = '';

function _accountText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}

// Grouped identity values (platform role, tenant roles) render as chips so a
// role reads as a distinct attribute instead of a line of plain text. Values
// are escaped; an empty list falls back to the shared "not set" copy so the
// empty-state contract matches a plain field.
function _accountChips(id, values) {
    const el = document.getElementById(id);
    if (!el) return;
    const list = (values || []).map(value => String(value == null ? '' : value).trim()).filter(Boolean);
    if (!list.length) { el.textContent = t('account_profile_empty'); return; }
    el.innerHTML = list.map(value =>
        '<span class="account-profile-chip">' + escapeHtml(value) + '</span>').join('');
}

function _accountHidden(id, hidden) {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('hidden', hidden);
}

// Avatar discs hold markup (an <img>), not text, so they need innerHTML. Any
// text fallback (an initial) is escaped by the caller.
function _accountAvatar(id, html) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html || '';
}

/* ---- User account avatars ------------------------------------------------
   Five bundled default faces stand in for an account that never uploaded one.
   The disc is picked from the account id alone, so the same account always
   wears the same default across sessions, tenants, list order and refreshes;
   the mapping is deliberately NOT keyed off the display name. An uploaded
   picture always wins over the default. */

/* How many bundled default account avatars exist (default-1 .. default-N). */
const USER_AVATAR_DEFAULTS = 5;

function userDefaultAvatarIndex(userId) {
    const key = String(userId || '');
    let hash = 0;
    for (let i = 0; i < key.length; i++) hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
    return (hash % USER_AVATAR_DEFAULTS) + 1;
}

function userDefaultAvatarURL(userId) {
    return `/assets/avatars/default-${userDefaultAvatarIndex(userId)}.svg`;
}

/* An account's face: the uploaded picture when it has one, otherwise the
   stable default. `self` routes the upload through the personal
   /auth/profile/avatar endpoint (the only one that serves the caller's own
   bytes); another account's upload is read through /api/users/<id>/avatar.
   A missing upload file degrades to the default rather than a broken image. */
function userAvatarHTML(opts) {
    const o = opts || {};
    const id = o.id || '';
    const fallback = userDefaultAvatarURL(id);
    let src = fallback;
    if (o.avatar === 'image' && id) {
        src = o.self
            ? '/auth/profile/avatar'
            : `/api/users/${encodeURIComponent(id)}/avatar`;
        if (o.self && o.version) src += `?v=${encodeURIComponent(o.version)}`;
    }
    return `<img class="user-avatar" src="${escapeHtml(src)}" alt=""`
        + ` onerror="this.onerror=null;this.src='${fallback}'">`;
}

function _emptyAccount(phase) {
    return { phase, mode: 'database', authRequired: null,
        authenticated: null, username: '', displayName: '', mustChangePassword: false };
}

function renderAccountVersion() {
    _accountText('sidebar-version', [effectiveBrandName(), APP_VERSION].filter(Boolean).join(' '));
}

function _renderSidebarAccount() {
    const active = document.activeElement;
    const state = _accountState;
    const hasUser = state.phase !== 'loading' && state.authenticated === true && !!state.username;
    const leaving = state.phase === 'logout_pending';
    const logoutError = state.phase === 'logout_error';
    const canLogout = (state.authRequired === true && state.authenticated === true) || logoutError || leaving;
    let name = t('account_loading'), subtitle = '';
    if (hasUser) {
        name = state.displayName || state.username;
        subtitle = '@' + state.username;
        // Prefer the *member* display name for the currently-selected tenant
        // (per the "edit member" field) over the account-level display name.
        if (_baseAccountSelf()) {
            const memberName = _currentMemberDisplayName();
            if (memberName) name = memberName;
        }
    } else if (leaving || logoutError) {
        name = t(leaving ? 'account_logging_out' : 'account_logout_unconfirmed');
        subtitle = logoutError ? t('account_retry_hint') : '';
    } else if (state.phase === 'error') {
        name = t('account_unavailable');
        subtitle = t('account_retry_hint');
    } else if (state.phase === 'unauthenticated') {
        name = t('account_login');
    }
    _accountText('sidebar-account-name', name);
    _accountText('sidebar-account-subtitle', subtitle);
    const trigger = document.getElementById('sidebar-account-toggle');
    // The trigger names the account *and* says what the entry offers
    // (「个人资源与设置」): the hint is supplementary, never a replacement for the
    // account name, and the compact row still shows avatar, name and chevron.
    const identityLabel = [name, subtitle].filter(Boolean).join('\n');
    const hint = t('account_menu_trigger_hint');
    if (trigger) {
        trigger.title = hasUser && hint ? [identityLabel, hint].join('\n') : identityLabel;
        if (hasUser && hint) trigger.setAttribute('aria-label', name + ' · ' + hint);
        else trigger.removeAttribute('aria-label');
    }
    const selfUser = ((_baseAccountSelf() || {}).user) || {};
    // The uploaded picture (or the id-stable default) once /auth/me has given us
    // the account id; until then keep the old initial so the disc does not flash
    // a default-1 face for an account that is actually on another index.
    const avatarHTML = hasUser
        ? (selfUser.id
            ? userAvatarHTML({ id: selfUser.id, avatar: selfUser.avatar, self: true, version: _accountAvatarVersion })
            : escapeHtml(Array.from(name.trim())[0] || ''))
        : '';
    _accountAvatar('sidebar-account-avatar', avatarHTML);
    _accountAvatar('account-menu-avatar', avatarHTML);
    _accountHidden('sidebar-account-avatar', !hasUser);
    _accountHidden('sidebar-account-avatar-icon', hasUser);
    _accountText('account-menu-name', hasUser ? name : '');
    _accountText('account-menu-username', hasUser ? '@' + state.username : '');
    _accountHidden('account-menu-identity', !hasUser);
    document.getElementById('account-menu-status')?.classList.remove('opacity-0');
    _accountText('account-menu-status', hasUser ? '' : name);
    _accountHidden('account-menu-status', hasUser || state.phase === 'unauthenticated');
    _accountHidden('account-menu-retry', !['error', 'logout_error', 'loading'].includes(state.phase));
    _accountHidden('account-menu-logout', !canLogout);
    _accountText('account-menu-logout-label', t(leaving ? 'account_logging_out' : logoutError ? 'account_retry_logout' : 'account_logout'));
    // Six-item menu for an authenticated database account.
    const dbUser = hasUser;
    _accountHidden('account-menu-settings', !dbUser);
    ['account-menu-profile', 'account-menu-password', 'account-menu-tenant'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = !!_accountWritePending;
        _accountHidden(id, !dbUser);
    });
    ['account-menu-prefs', 'account-menu-about'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = !!_accountWritePending;
        _accountHidden(id, !dbUser);
    });
    ['account-menu-logout'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = !!_accountWritePending;
    });
    ['account-menu-retry', 'auth-check-retry'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = state.phase === 'loading' || !!_accountWritePending;
    });
    if (!_accountAppVisible && state.phase !== 'unauthenticated' && !_pendingTenantPicker) {
        _accountText('login-subtitle', '');
        _accountText('auth-check-message', t(state.phase === 'error' ? 'account_unavailable' : 'account_loading'));
        _accountHidden('auth-check-retry', state.phase !== 'error');
    } else if (!_accountAppVisible) {
        _accountText('login-subtitle', t('account_login_hint'));
        _accountText('login-btn', t(_pendingTenantPicker ? 'login_enter_tenant' : 'account_login'));
    }
    renderAccountVersion();
    // A retry may disappear once data arrives. Keep focus inside the open
    // popover, instead of losing it to the page or focusing the chat input.
    if (_accountMenuOpen && active && ['account-menu-retry', 'account-menu-logout'].includes(active.id)
            && (active.disabled || active.classList.contains('hidden'))) {
        document.getElementById('sidebar-version')?.focus();
    }
}

function _accountMenuOutside(event) {
    // The panel may be hosted at the document root while the mobile sheet is
    // open, so "inside" has to cover the panel itself as well as the footer.
    const menu = document.getElementById('sidebar-account-menu');
    if (menu && menu.contains(event.target)) return;
    if (document.getElementById('sidebar-account-footer')?.contains(event.target)) return;
    closeAccountMenu();
}

// A menu item is reachable only when neither it nor any ancestor group inside the
// panel is hidden; a closed panel's content never joins the focus order.
function _accountMenuHiddenAncestor(el, root) {
    for (let node = el; node && node !== root; node = node.parentNode) {
        if (node.classList && node.classList.contains('hidden')) return true;
    }
    return false;
}

function _accountMenuFocusable(menu) {
    if (!menu || typeof menu.querySelectorAll !== 'function') return [];
    return Array.from(menu.querySelectorAll('button, a'))
        .filter(el => !el.disabled && !_accountMenuHiddenAncestor(el, menu));
}

function _accountMenuSheetMode() {
    // The same breakpoint the off-canvas sidebar uses (Tailwind ``lg``).
    return (window.innerWidth || 0) < 1024;
}

function _accountMenuSheetActive() {
    return _accountMenuOpen && _accountMenuSheet;
}

// Move the single account panel node between the two hosts and switch its
// semantics. On mobile it is mounted at the document root because the off-canvas
// sidebar carries a transform: a fixed-position sheet inside it would be clipped
// and dragged with the drawer. The panel content is never duplicated.
function _mountAccountMenu(isSheet) {
    const menu = document.getElementById('sidebar-account-menu');
    const footer = document.getElementById('sidebar-account-footer');
    if (!menu || !footer) return;
    _accountMenuSheet = !!isSheet;
    if (_accountMenuSheet) {
        if (menu.parentNode !== document.body) document.body.appendChild(menu);
        menu.classList.add('account-menu-sheet');
        menu.setAttribute('role', 'dialog');
        menu.setAttribute('aria-modal', 'true');
    } else {
        if (menu.parentNode !== footer) footer.appendChild(menu);
        menu.classList.remove('account-menu-sheet');
        menu.setAttribute('role', 'group');
        menu.removeAttribute('aria-modal');
    }
    _accountHidden('account-menu-sheet-head', !_accountMenuSheet);
    _accountHidden('account-menu-sheet-close', !_accountMenuSheet);
}

// Backdrop and background scroll lock belong to the open sheet only. Keeping the
// teardown in one place is what makes "close the drawer / change breakpoint /
// lose the session" leave no residue behind.
function _accountMenuSheetChrome() {
    const active = _accountMenuSheetActive();
    const backdrop = document.getElementById('account-menu-backdrop');
    if (backdrop) backdrop.classList.toggle('hidden', !active);
    document.body.classList.toggle('account-menu-sheet-open', active);
}

// Size the panel from the space actually available. The icon sidebar opens its
// panel to the side, so its limit is the viewport rather than the room above the
// card; the mobile sheet is bounded by CSS (80% of the visual viewport).
function _applyAccountMenuHeight() {
    const menu = document.getElementById('sidebar-account-menu');
    const footer = document.getElementById('sidebar-account-footer');
    if (!menu || !menu.style) return;
    if (_accountMenuSheet) { menu.style.maxHeight = ''; return; }
    const viewport = window.innerHeight || 0;
    const iconSidebar = (window.innerWidth || 0) >= 1024
        && !!document.getElementById('app')
        && document.getElementById('app').classList.contains('sidebar-collapsed');
    const top = footer && typeof footer.getBoundingClientRect === 'function'
        ? footer.getBoundingClientRect().top : viewport;
    const available = iconSidebar ? Math.max(160, viewport - 24) : Math.max(0, top - 12);
    menu.style.maxHeight = available + 'px';
}

// Re-opening the panel must land on the first actionable stop: the panel is a
// plain action list now, so there is no current resource item to scroll back to.
function _syncAccountMenuScroll() {
    const menu = document.getElementById('sidebar-account-menu');
    if (!menu || typeof menu.scrollTop !== 'number') return;
    menu.scrollTop = 0;
}

function _accountMenuKey(event) {
    if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        closeAccountMenu(true);
        return;
    }
    // The mobile sheet is a modal dialog: Tab cycles inside it instead of
    // reaching the page behind the backdrop. Desktop keeps the existing
    // non-modal Tab-out behaviour (the focusin listener closes the popover).
    if (event.key === 'Tab' && _accountMenuSheetActive()) {
        const menu = document.getElementById('sidebar-account-menu');
        const items = _accountMenuFocusable(menu);
        if (!items.length) return;
        const first = items[0];
        const last = items[items.length - 1];
        const active = document.activeElement;
        if (!menu.contains(active)) {
            event.preventDefault();
            first.focus();
            return;
        }
        if (event.shiftKey && active === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && active === last) {
            event.preventDefault();
            first.focus();
        }
    }
}

function closeAccountMenu(returnFocus = false) {
    if (!_accountMenuOpen) return;
    _accountMenuOpen = false;
    _accountHidden('sidebar-account-menu', true);
    document.getElementById('sidebar-account-toggle')?.setAttribute('aria-expanded', 'false');
    document.removeEventListener('pointerdown', _accountMenuOutside, true);
    document.removeEventListener('focusin', _accountMenuOutside);
    document.removeEventListener('keydown', _accountMenuKey, true);
    // Release the sheet (backdrop, scroll lock, dialog role) and put the single
    // panel node back where the desktop popover expects it.
    if (_accountMenuSheet) _mountAccountMenu(false);
    _accountMenuSheetChrome();
    if (returnFocus && _accountAppVisible) document.getElementById('sidebar-account-toggle')?.focus();
}

function toggleAccountMenu() {
    if (_accountMenuOpen) { closeAccountMenu(); return; }
    if (!_accountAppVisible) return;
    const menu = document.getElementById('sidebar-account-menu');
    if (!menu) return;
    ['tenant-menu'].forEach(id => _accountHidden(id, true));
    _mountAccountMenu(_accountMenuSheetMode());
    _renderSidebarAccount();
    _accountMenuOpen = true;
    _accountHidden('sidebar-account-menu', false);
    document.getElementById('sidebar-account-toggle')?.setAttribute('aria-expanded', 'true');
    _accountMenuSheetChrome();
    _applyAccountMenuHeight();
    _syncAccountMenuScroll();
    document.addEventListener('pointerdown', _accountMenuOutside, true);
    document.addEventListener('focusin', _accountMenuOutside);
    document.addEventListener('keydown', _accountMenuKey, true);
    const first = _accountMenuFocusable(menu)[0];
    if (first) first.focus();
}

function _clearTenantPicker() {
    _pendingTenantPicker = false;
    const btn = document.getElementById('login-btn');
    if (btn) { btn.onclick = null; btn.type = 'submit'; }
    const select = document.getElementById('login-tenant-select');
    if (select) select.replaceChildren();
    _accountHidden('login-tenant-group', true);
    const form = document.getElementById('login-form');
    if (form) form.onsubmit = _submitAccountLogin;
}

function _invalidateAccountIdentity(phase) {
    ++_authEpoch;
    if (typeof resetAgentWorkbenchFilters === 'function') resetAgentWorkbenchFilters(true);
    ++_accountCheckSeq;
    _accountCheckRequest = null;
    _accountIdentityKey = null;
    _accountEntryRequest = null;
    // Drop the cached capability summary and invalidate any in-flight reply from
    // the previous context (bumping the sequence is what discards the late one).
    ++_authContextSeq;
    _authContext = null;
    _authContextRequest = null;
    _authContextPhase = 'unknown';
    // The Agents and the default pointers belonged to the tenant that just went
    // away. The next catalogue read repopulates them; until then the detail pane
    // must not offer "设为我的默认" against the previous tenant's pointer.
    userDefault = { agent_id: '', revision: null, origin: null };
    tenantDefaultManageable = false;
    defaultResolution = { agent_id: '', source: null };
    _accountState = _emptyAccount(phase);
    if (_forcedPassword) _closeForcedPasswordModal();
    _clearTenantPicker();
    closeAccountMenu();
}

function _normalizeAccountCheck(data) {
    if (!data || typeof data !== 'object' || Array.isArray(data) || data.status !== 'success'
            || typeof data.auth_required !== 'boolean') throw new Error('Invalid authentication response');
    if (data.identity_mode !== 'database' || !data.auth_required
            || typeof data.authenticated !== 'boolean') {
        throw new Error('Invalid authentication mode');
    }
    const authenticated = data.authenticated;
    const user = authenticated && data.user;
    const username = user && typeof user.username === 'string' && user.username.trim() ? user.username : '';
    const displayName = user && typeof user.display_name === 'string' && user.display_name.trim() ? user.display_name : '';
    const mustChangePassword = authenticated ? Boolean(data.must_change_password) : false;
    return { mode: 'database', authRequired: true, authenticated, username, displayName,
        mustChangePassword,
        phase: !authenticated ? 'unauthenticated' : !username ? 'error' : 'ready' };
}

function _acceptAccountIdentity(next, newLogin = false) {
    const previous = _accountIdentityKey;
    if (!previous || newLogin || previous.mode !== next.mode || previous.authRequired !== next.authRequired
            || (previous.username && next.username && previous.username !== next.username)) ++_authEpoch;
    // A missing profile is not a new session: in-flight current-session 401s
    // must still take effect after a profile-only retry.
    _accountIdentityKey = { mode: next.mode, authRequired: next.authRequired,
        username: next.username || previous?.username || '' };
    _accountState = next;
    _renderSidebarAccount();
}

function _showAccountCheckGate() {
    _accountHidden('login-overlay', false);
    _accountHidden('app', true);
    _accountHidden('login-form', true);
    _accountHidden('auth-check-panel', false);
    _renderSidebarAccount();
}

function _enterAccountApp() {
    if (_accountAppVisible) return Promise.resolve();
    if (_accountEntryRequest) return _accountEntryRequest;
    // Entering the app is exactly when the *tenant* may have changed (the
    // picker and the one-shot `switch_tenant` link both land here). Drop the
    // previous tenant's capability summary and bump the revision before any
    // consumer starts, so a reply that was in flight when the tenant went away
    // cannot be written into the new one.
    _invalidateAuthContext();
    // The task list is the tenant's own ledger, so entering the app (exactly
    // where the tenant may have changed) drops the loaded flag: the next visit
    // reads the new tenant's rows instead of repainting the previous one's.
    tasksLoaded = false;
    // The context panel is session-scoped too; tear it down for the same reason
    // (a detached module would still hold the previous tenant's usage).
    if (typeof disposeContextModule === 'function') disposeContextModule();
    const epoch = _authEpoch;
    const current = () => epoch === _authEpoch && !_forcedPassword;
    _showAccountCheckGate();
    const request = Promise.resolve()
        // Authentication has established the mode before a tenant switch is
        // resolved. No business request may run until membership is confirmed.
        .then(() => current() ? _resolveOneShotTenantSwitch() : false)
        .then(() => current() ? _ensureTenantSelected() : false)
        .then(ready => {
            if (!current() || ready === false) return;
            // A platform administrator with no active tenant membership is a
            // pending-assignment account: per the member-tenant continuity rule
            // it must NOT enter the workbench or a normal management page, and
            // must NOT initialize any tenant consumer. Show the restricted
            // recovery gate ("待分配说明 + 重试") instead of the old no-tenant
            // platform direct branch. The administrator completes assignment
            // and retries; the server independently denies normal APIs.
            if (ready === 'platform') {
                _accountState = { ..._accountState, phase: 'error' };
                _showAccountCheckGate();
                _accountText('auth-check-message', t('account_assign_pending'));
                _accountHidden('auth-check-retry', false);
                _accountHidden('login-form', true);
                return;
            }
            // Platform administration does not require tenant membership.
            // Keep its navigation available without starting tenant consumers.
            return Promise.resolve(initApp()).then(() => {
                if (!current()) return;
                _accountHidden('login-overlay', true);
                _accountHidden('auth-check-panel', true);
                _accountHidden('app', false);
                // Reflect the validated layout-only navigation presentation
                // switch (classic|split) on the app root. This is a CSS/layout
                // hook only; it never changes authorization or consumer state.
                const appEl = document.getElementById('app');
                if (appEl) appEl.setAttribute('data-nav-mode', _navigationMode());
                _applyNavAreaAttribute();
                _accountAppVisible = true;
                _renderSidebarAccount();
                // Gate permission-sensitive sidebar entries (platform/audit)
                // once the self profile AND the current tenant's authoritative
                // capability projection are known. The tenant admin qualification
                // and per-page availability now come from /auth/context, not from
                // a client-side role array.
                fetchAccountSelf().then(function (self) {
                    if (!current()) return;
                    _applySidebarPermissions(self);
                    // The member-level display name arrives with /auth/me. Refresh
                    // the sidebar account label once so it can swap from the
                    // account-level name to the current tenant's member name.
                    _renderSidebarAccount();
                    return _fetchTenantAuthorization();
                }).then(function (ctx) {
                    if (!current()) return;
                    // Re-apply with the authoritative projection when it arrives.
                    _applySidebarPermissions(_baseAccountSelf());
                    if (ctx) _applySidebarPermissions(_baseAccountSelf());
                    // The knowledge write entry points depend on the selected
                    // Agent's projected capability (can_write_knowledge) and
                    // the admin qualification.
                    if (typeof renderKnowledgeWriteAffordances === 'function') {
                        renderKnowledgeWriteAffordances();
                    }
                    if (typeof _bootAreaDefaultView === 'function') _bootAreaDefaultView();
                    if (typeof loadSidebarRecentSessions === 'function') loadSidebarRecentSessions();
                    // The projection is known now: mount the context entry with
                    // the authoritative per-action availability (a no-op without
                    // the module, and hidden when both actions are closed).
                    if (typeof mountContextModule === 'function') mountContextModule();
                });
                if (_identityMode() === 'database') _setupHeaderTenantSelector();
                chatInput.focus();
            });
        })
        .catch(error => {
            if (!current()) return;
            _accountState = { ..._accountState, phase: 'error' };
            _showAccountCheckGate();
            if (error.code === 'no_tenants') {
                _accountText('auth-check-message', t('account_tenant_no_available'));
            }
        })
        .finally(() => {
            if (_accountEntryRequest === request) _accountEntryRequest = null;
        });
    _accountEntryRequest = request;
    return request;
}

// --- forced password change gate ---------------------------------------
// A database account flagged must_change_password must set a new password
// before tenant selection or any tenant-scoped business load. Show the change
// password modal as a full-screen gate; the restricted user may only complete
// the password change or log out. Closing/closing without setting a password is
// disallowed after a forced prompt.
function _enterForcedPassword() {
    _forcedPassword = true;
    _accountHidden('app', true);
    _accountHidden('auth-check-panel', true);
    // Keep the login overlay as a backdrop; the password modal is elevated so
    // it sits above the overlay (network gating remains until password is set).
    _accountHidden('login-overlay', false);
    _renderSidebarAccount();
    _openForcedPasswordModal();
}

function _openForcedPasswordModal() {
    _setAccountPanel('password');
    const modal = document.getElementById('account-password-modal');
    if (modal) {
        modal.classList.remove('hidden');
        // Elevate above the login overlay backdrop so the gate is interactable.
        modal.style.zIndex = '210';
    }
    // Repurpose the modal title/note for the forced flow; restore on close.
    const title = document.getElementById('account-password-title');
    if (title) title.textContent = t('account_password_forced_title');
    const note = document.querySelector('#account-password-modal .account-password-note');
    if (note) note.textContent = t('account_password_forced_note');
    // A restricted account cannot dismiss the gate: hide the close X and change
    // the cancel action to offer logout instead.
    const closeBtn = document.getElementById('account-password-close');
    if (closeBtn) closeBtn.classList.add('hidden');
    const cancelBtn = document.querySelector('#account-password-modal .agent-modal-foot button[type="button"]');
    if (cancelBtn) cancelBtn.textContent = t('account_logout');
    const status = document.getElementById('account-password-status');
    if (status) status.classList.add('hidden');
    // Clear any leftover password input.
    ['ap-old-password', 'ap-new-password', 'ap-confirm-password'].forEach(id => {
        const el = document.getElementById(id);
        if (el) { el.value = ''; delete el.dataset.dirty; }
    });
    focusAccountPanel('account-password-modal');
}

function _closeForcedPasswordModal() {
    _forcedPassword = false;
    // Restore normal modal semantics.
    const title = document.getElementById('account-password-title');
    if (title) title.textContent = t('account_password_title');
    const note = document.querySelector('#account-password-modal .account-password-note');
    if (note) note.textContent = t('account_password_note');
    const closeBtn = document.getElementById('account-password-close');
    if (closeBtn) closeBtn.classList.remove('hidden');
    const cancelBtn = document.querySelector('#account-password-modal .agent-modal-foot button[type="button"]');
    if (cancelBtn) cancelBtn.textContent = t('cancel');
    const modal = document.getElementById('account-password-modal');
    if (modal) modal.style.zIndex = '';
    _accountHidden('account-password-modal', true);
    _setAccountPanel(null);
}

function refreshAccountIdentity() {
    if (_accountWritePending || _pendingTenantPicker) return Promise.resolve();
    if (_accountCheckRequest) return _accountCheckRequest;
    const epoch = _authEpoch, seq = ++_accountCheckSeq;
    const current = () => epoch === _authEpoch && seq === _accountCheckSeq;
    _accountState = _emptyAccount('loading');
    if (!_accountAppVisible) _showAccountCheckGate();
    else _renderSidebarAccount();
    const request = Promise.resolve().then(async () => {
        try {
            // Global identity is independent of the selected tenant. Do not
            // use the tenant-admin request helper or cache a credentials body.
            const response = await fetch('/auth/check', { credentials: 'same-origin', cache: 'no-store' });
            if (!current()) return;
            if (response.status === 401) { showLoginScreen(); return; }
            if (!response.ok) throw new Error('Authentication check failed');
            const data = await response.json();
            if (!current()) return;
            const next = _normalizeAccountCheck(data);
            if (next.phase === 'unauthenticated') {
                showLoginScreen();
                return;
            }
            _acceptAccountIdentity(next);
            if (next.mustChangePassword) _enterForcedPassword();
            else if (!_accountAppVisible) _enterAccountApp();
        } catch (_) {
            if (!current()) return;
            _accountState = _emptyAccount('error');
            if (!_accountAppVisible) _showAccountCheckGate();
            else _renderSidebarAccount();
        } finally {
            if (_accountCheckRequest === request) _accountCheckRequest = null;
        }
    });
    _accountCheckRequest = request;
    return request;
}
// End sidebar account state

// Normalize the previous default while an existing backend is still running.
// Instance-specific titles remain intact.
function productTitle(title) {
    if (title && /^cowagent$/i.test(title.trim())) return 'RongAI';
    return !title || /^ai assistant$/i.test(title.trim()) ? PRODUCT_NAME : title;
}

function productTitleHTML(title) {
    const name = productTitle(title);
    return name === PRODUCT_NAME ? '容大<span class="brand-ai">AI</span>' : escapeHtml(name);
}

/* ---- Instance brand (branding) state --------------------------------------
   Single source for the console brand. Populated from /api/branding/public.
   Every painted brand position (sidebar, login, welcome, browser title,
   favicon, default agent avatar) consumes the SAME snapshot so a save updates
   them all without a reload. Falls back to the bundled default brand. */
const DEFAULT_BRAND = {
    enabled: false,
    revision: 0,
    brand_name: '容大AI',
    logo_description: '工作台',
    logo_url: '/assets/rongda-ai-mark.svg',
    favicon_url: '/assets/favicon.ico',
};
let brandState = { ...DEFAULT_BRAND };
let brandLoaded = false;
// Monotonic fetch generation + an explicit "save epoch" so a late read response
// can never overwrite a newer published version shown by a save.
let brandFetchSeq = 0;
let brandSaveEpoch = 0;
// Per-tab guard list used by navigateTo / beforeunload.
let brandingDirty = false;

function publicBrand() {
    return brandState;
}

function isBrandEnabled() {
    return !!brandState.enabled;
}

function effectiveBrandName() {
    return brandState.brand_name || DEFAULT_BRAND.brand_name;
}

function effectiveLogoUrl() {
    return brandState.logo_url || DEFAULT_BRAND.logo_url;
}

function effectiveFaviconUrl() {
    return brandState.favicon_url || DEFAULT_BRAND.favicon_url;
}

function effectiveLogoDescription() {
    return brandState.logo_description || '';
}

function _isDefaultLogoDescription(desc) {
    const value = String(desc || '').trim();
    if (!value) return true;
    // Treat the current default and the legacy caption as built-in defaults so
    // path-based sidebar captions (工作台 / 控制台) can replace them.
    return value === DEFAULT_BRAND.logo_description
        || value === '工作台'
        || value === '控制台'
        || value === 'Workbench'
        || value === 'Console';
}

function sidebarBrandCaption(desc) {
    if (!_isDefaultLogoDescription(desc)) return String(desc || '').trim();
    const area = (typeof _navAreaFromPath === 'function')
        ? _navAreaFromPath(location.pathname)
        : 'workbench';
    return area === 'admin' ? t('nav_admin_console') : t('nav_workbench');
}

/* Description allowed on the welcome hero. The built-in default ("工作台") is
   still styled into the sidebar caption and (historically) the login brand
   area, but on the welcome hero it's redundant with the eyebrow
   ("容大AI · 你的工作助手"), so we only surface a customized description. */
function welcomeHeroDescription() {
    const desc = effectiveLogoDescription();
    const isDefault = _isDefaultLogoDescription(desc);
    return (desc && desc.trim() && !isDefault) ? desc : '';
}

/* Render the brand name into a brand-styled wordmark. When the brand name is
   the built-in default we keep the special "容大<span>AI</span>" mark; any
   other name is escaped as plain text. */
function brandWordmarkHTML(name) {
    const value = productTitle(name || DEFAULT_BRAND.brand_name);
    return value === PRODUCT_NAME ? '容大<span class="brand-ai">AI</span>' : escapeHtml(value);
}

/* One-shot image fallback: if a brand logo/favicon fails to load, swap it for
   the built-in default ONCE so a transient asset error doesn't leave an empty
   slot and doesn't loop. Attached at set-src time; `{ once: true }` removes the
   listener after the first failure so a bad network blip is a single swap. */
function _brandArmFallback(img, url) {
    if (!img) return;
    img.dataset.brandFallbackArmed = '1';
    img.src = url;
    img.addEventListener('error', function onErr() {
        img.removeEventListener('error', onErr);
        if (img.dataset.brandFallbackArmed === '1') {
            img.dataset.brandFallbackArmed = '0';
            img.src = '/assets/rongda-ai-mark.svg';
        }
    }, { once: true });
}

/* Apply the brand snapshot to every static DOM position. Re-painted on save,
   on public revalidation and on view entry. idempotent w.r.t. brandState. */
function applyBrandToDocument() {
    renderAccountVersion();
    const name = effectiveBrandName();
    const logoUrl = effectiveLogoUrl();
    const desc = effectiveLogoDescription();
    const logoAlt = (desc || '').trim() || name;

    // Sidebar brand mark + wordmark + caption
    document.querySelectorAll('.sidebar-brand .brand-mark[src]').forEach(img => {
        _brandArmFallback(img, logoUrl);
    });
    const sidebarWordmark = document.getElementById('sidebar-brand-name');
    if (sidebarWordmark) { sidebarWordmark.innerHTML = brandWordmarkHTML(name); sidebarWordmark.title = name; }
    const sidebarCaption = document.getElementById('sidebar-brand-caption');
    if (sidebarCaption) {
        const captionHelper = (typeof window !== 'undefined' && window
            && typeof window.sidebarBrandCaption === 'function')
            ? window.sidebarBrandCaption
            : null;
        const caption = captionHelper
            ? captionHelper(desc)
            : ((desc && desc.trim()) || '');
        const hasDesc = !!(caption && String(caption).trim());
        sidebarCaption.textContent = hasDesc ? caption : '';
        sidebarCaption.classList.toggle('hidden', !hasDesc);
        sidebarCaption.title = hasDesc ? caption : '';
    }

    // Login brand area
    document.querySelectorAll('#login-overlay .brand-mark[src], .login-brand-mark').forEach(img => {
        _brandArmFallback(img, logoUrl);
    });
    const loginWordmark = document.getElementById('login-brand-name');
    if (loginWordmark) loginWordmark.innerHTML = brandWordmarkHTML(name);

    // Welcome screen (both the static initial hero and any rebuilt new-chat DOM)
    document.querySelectorAll('#welcome-screen .brand-mark[src]').forEach(img => {
        _brandArmFallback(img, logoUrl);
    });
    const welcomeTitle = document.getElementById('welcome-title');
    if (welcomeTitle) welcomeTitle.innerHTML = brandWordmarkHTML(name);
    // Welcome hero only surfaces a customized description; the built-in default
    // stays hidden here (redundant with the eyebrow) but still shows elsewhere.
    const heroDesc = welcomeHeroDescription();
    document.querySelectorAll('#welcome-screen [data-brand-desc]').forEach(el => {
        const hasDesc = !!(heroDesc && heroDesc.trim());
        el.textContent = hasDesc ? heroDesc : '';
        el.classList.toggle('hidden', !hasDesc);
    });
    // Rebuilt welcome DOM inside other containers uses these hooks.
    document.querySelectorAll('[data-brand-slot="name"]').forEach(el => {
        el.innerHTML = brandWordmarkHTML(name);
    });
    document.querySelectorAll('[data-brand-slot="logo"]').forEach(img => {
        _brandArmFallback(img, logoUrl);
    });
    document.querySelectorAll('[data-brand-slot="desc"]').forEach(el => {
        const isWelcomePreview = !!el.closest('[data-preview="welcome"]');
        const isDefault = (desc || '').trim() === DEFAULT_BRAND.logo_description;
        const hasDesc = isWelcomePreview
            ? !!(desc && desc.trim() && !isDefault)
            : !!(desc && desc.trim());
        el.textContent = hasDesc ? desc : '';
        el.classList.toggle('hidden', !hasDesc);
        el.title = hasDesc ? desc : '';
    });
    document.querySelectorAll('[data-brand-slot="caption"]').forEach(el => {
        const hasDesc = !!(desc && desc.trim());
        el.textContent = hasDesc ? desc : '';
        el.classList.toggle('hidden', !hasDesc);
        el.title = hasDesc ? desc : '';
    });

    // Browser title: "<brand_name> 控制台" (控制台 localized)
    const consoleLabel = t('branding_browser_title') || '控制台';
    document.title = `${name} ${consoleLabel}`.trim();

    // Favicon (derived PNG served by the backend; falls back to the built-in)
    const favUrl = effectiveFaviconUrl();
    document.querySelectorAll('link[rel="icon"]').forEach(link => {
        link.href = `${favUrl}?v=${brandFetchSeq}`;
    });
}

/* Refresh the default-agent avatar fallback (uses the product logo). */
function applyBrandToAgentAvatars() {
    document.querySelectorAll('.agent-avatar-brand').forEach(img => {
        _brandArmFallback(img, effectiveLogoUrl());
    });
}

// =====================================================================
// i18n
// =====================================================================
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

// Resolve language by priority: user choice (localStorage) -> backend-detected
// (cow_lang) -> browser language -> 'zh'. Shares __cowResolveLang__ defined in
// chat.html; falls back to a local resolver if loaded standalone.
let currentLang = (typeof window.__cowResolveLang__ === 'function')
    ? window.__cowResolveLang__()
    : (function () {
        const norm = (raw) => {
            if (!raw) return '';
            const v = String(raw).trim().toLowerCase();
            if (v === 'auto') return '';
            // Handle Traditional Chinese variants first (more specific)
            if (v === 'zh-hant' || v.startsWith('zh-hant-') || v === 'zh-tw' || v === 'zh-hk') return 'zh-Hant';
            // Then Simplified Chinese
            if (v.indexOf('zh') === 0) return 'zh';
            if (v.indexOf('en') === 0) return 'en';
            return '';
        };
        return norm(readStartupPreference('cow_lang'))
            || norm(window.__COW_DEFAULT_LANG__)
            || norm(navigator.language)
            || 'zh';
    })();

// Expose for sibling scripts (e.g. identity-admin.js) that use the same catalogs.
window.I18N = I18N;
window.__cowLang__ = currentLang;

function t(key) {
    return (I18N[currentLang] && I18N[currentLang][key]) || (I18N.en[key]) || key;
}

// Resolve a localized label that may be either a plain string or
// a {zh, en} object returned by the backend.
function localizedLabel(label) {
    if (label && typeof label === 'object') {
        return label[currentLang] || label.en || label.zh || '';
    }
    return label || '';
}

function applyI18n() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
        el.textContent = t(el.dataset.i18n);
    });
    document.querySelectorAll('[data-i18n-html]').forEach(el => {
        el.innerHTML = t(el.dataset.i18nHtml);
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        el.placeholder = t(el.dataset['i18nPlaceholder']);
    });
    document.querySelectorAll('[data-i18n-title]').forEach(el => {
        el.title = t(el.dataset['i18nTitle']);
    });
    document.querySelectorAll('[data-i18n-aria-label]').forEach(el => {
        el.setAttribute('aria-label', t(el.dataset['i18nAriaLabel']));
    });
    document.querySelectorAll('[data-i18n-tip]').forEach(el => {
        el.setAttribute('data-tip', t(el.dataset['i18nTip']));
    });
    document.querySelectorAll('[data-tip-key]').forEach(el => {
        el.setAttribute('data-tooltip', t(el.dataset.tipKey));
    });
    installCfgTipPortal();
    
    // Clear any status messages when language changes
    document.querySelectorAll('[id$="-status"]').forEach(el => {
        el.classList.add('opacity-0');
    });
    
    // Point the docs link to the locale-specific documentation site.
    const docsLink = document.getElementById('docs-link');
    if (docsLink) docsLink.href = currentLang === 'zh' ? 'https://www.rsm.global/china/zh-hans' : 'https://www.rsm.global/china/zh-hans';
    // Workspace panel content is rendered by JS, not data-i18n attributes.
    if (typeof relocalizeWorkspacePanel === 'function') relocalizeWorkspacePanel();
    _renderSidebarAccount();
    renderAppearancePreferences();
    if (typeof paintWelcomeAgentIntro === 'function') paintWelcomeAgentIntro();
}

// Single entry point for switching language.
//
// Two call paths share this:
//   * Personal change (the account Preferences modal): browser-local only
//     (``cow_lang``). MUST NOT write instance config.
//   * Config-page language picker (``cfg-lang-select``): preserves the original
//     permission and still persists the instance default ``cow_lang``.
// They are split so a personal switch never changes the instance (logs / CLI /
// agent replies) default.
function setLanguage(lang) {
    const next = (lang === 'en' || lang === 'zh' || lang === 'zh-Hant') ? lang : 'zh';
    if (next === currentLang) {
        // Still persist + sync in case storage/backend drifted from the UI.
        syncLanguageToBackend(next);
        return;
    }
    applyLanguage(next, /* writeToBackend */ true);
}

// Personal / browser-local switch (the account Preferences modal). Applies the
// language locally and NEVER writes the instance ``cow_lang`` config.
function setLanguageLocal(lang) {
    const next = (lang === 'en' || lang === 'zh' || lang === 'zh-Hant') ? lang : 'zh';
    applyLanguage(next, /* writeToBackend */ false);
}

let languageStorageFailed = false;

// Shared application of a new language. ``writeToBackend`` selects whether the
// instance config default is updated (config page) or left untouched (personal).
function applyLanguage(next, writeToBackend) {
    currentLang = next;
    window.__cowLang__ = currentLang;
    try {
        localStorage.setItem('cow_lang', currentLang);
        languageStorageFailed = localStorage.getItem('cow_lang') !== currentLang;
    } catch (_) { languageStorageFailed = true; }
    applyI18n();
    _applyInputTooltips();
    // The context panel owns its own labels, so re-mount it to repaint them.
    if (typeof mountContextModule === 'function') mountContextModule();
    // Keep the config-page language selector in sync (the personal preference
    // lives in the account menu's preferences dialog).
    try { updateLangControls(); } catch (e) {}

    if (writeToBackend) {
        // Sync language choice to backend first, then trigger dynamic views
        // reload to avoid race conditions on API endpoints.
        syncLanguageToBackend(currentLang, () => {
            try { rerenderDynamicViews(); } catch (e) {}
        });
    } else {
        try { rerenderDynamicViews(); } catch (e) {}
    }
}

// Persist the language to the backend `cow_lang` config (best-effort; the UI
// has already switched locally, so a network failure is non-blocking).
function syncLanguageToBackend(lang, callback) {
    try {
        fetch('/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ updates: { cow_lang: lang } })
        })
        .then(() => { if (callback) callback(); })
        .catch(() => { if (callback) callback(); });
    } catch (e) {
        if (callback) callback();
    }
}

// Reflect the current language on the config selector (if present), so the
// remaining entry points stay synchronized.
function updateLangControls() {
    // The config language picker is the custom .cfg-dropdown component. Only
    // sync it once it has been initialized (i.e. the config panel was opened).
    const sel = document.getElementById('cfg-lang-select');
    if (sel && sel._ddValue !== undefined && sel._ddValue !== currentLang) {
        sel._ddValue = currentLang;
        const textEl = sel.querySelector('.cfg-dropdown-text');
        if (textEl) {
            if (currentLang === 'zh-Hant') textEl.textContent = '繁體中文';
            else if (currentLang === 'zh') textEl.textContent = '简体中文';
            else textEl.textContent = 'English';
        }
        sel.querySelectorAll('.cfg-dropdown-item').forEach(i => {
            i.classList.toggle('active', i.dataset.value === currentLang);
        });
    }
}

// Refresh JS-rendered views after a language switch. Each branch uses the
// lightweight in-memory re-render path (no extra network round-trips).
function rerenderDynamicViews() {
    if (currentView === 'history') {
        _closeSessionActionMenu();
        _renderSessionList();
        _updateHistorySearchControls();
        _renderHistoryStatus();
    }
    if (currentView === 'agent-workbench') renderAgentWorkbench();
    // Models are a tab of the config view, not a view of their own.
    if (currentView === 'config' && typeof renderModelsView === 'function'
            && modelsState && (modelsState.providers || modelsState.capabilities)) {
        renderModelsView();
    }
    // The member catalog is the same view's other shape: re-render it from the
    // rows already in hand so a language switch does not silently drop back to
    // the loading skeleton while the request is repeated.
    if (currentView === 'config' && typeof renderMemberCatalog === 'function'
            && (memberCatalogState.items || []).length) {
        renderMemberCatalog();
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
    // Re-render the fork/identity views through the view registry (task 8.6).
    // Their `repaint` is the lightweight in-memory reload they used to get here.
    _repaintRegisteredView(currentView);
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
        if (!text) return;
        if (!_cfgTipPortalEl) {
            _cfgTipPortalEl = document.createElement('div');
            _cfgTipPortalEl.className = 'cfg-tip-floating';
            document.body.appendChild(_cfgTipPortalEl);
        }
        _cfgTipPortalEl.textContent = text;
        const rect = target.getBoundingClientRect();
        // Render once to measure, then position relative to the target.
        _cfgTipPortalEl.style.left = '0px';
        _cfgTipPortalEl.style.top = '0px';
        _cfgTipPortalEl.classList.add('show');
        const tipRect = _cfgTipPortalEl.getBoundingClientRect();
        let left = rect.left + rect.width / 2 - tipRect.width / 2;
        // Clamp horizontally to the viewport with an 8px gutter.
        left = Math.max(8, Math.min(left, window.innerWidth - tipRect.width - 8));
        // Default above the target; place below when data-tooltip-pos="bottom".
        const below = target.getAttribute('data-tooltip-pos') === 'bottom';
        const top = below ? rect.bottom + 6 : rect.top - tipRect.height - 6;
        _cfgTipPortalEl.style.left = left + 'px';
        _cfgTipPortalEl.style.top = top + 'px';
    };
    const hideTip = () => {
        if (_cfgTipPortalEl) _cfgTipPortalEl.classList.remove('show');
    };

    // Matches config keys and any element opting into the floating tooltip via
    // [data-tip-float] (used for dynamic tooltips like the workspace selector,
    // whose data-tooltip is set at runtime rather than from a translation key).
    const _tipSel = '[data-tip-key],[data-tip-float]';
    document.addEventListener('mouseover', (e) => {
        const target = e.target.closest(_tipSel);
        if (target) showTip(target);
    });
    document.addEventListener('mouseout', (e) => {
        const target = e.target.closest(_tipSel);
        if (target) hideTip();
    });
    // Hide on scroll/resize so the tooltip doesn't drift away from its anchor.
    window.addEventListener('scroll', hideTip, true);
    window.addEventListener('resize', hideTip);
}

// =====================================================================
// Theme
// =====================================================================
// The pre-paint controller owns state; this is a resolved-mode projection for
// existing console consumers, not a second persisted preference.
let currentTheme = (window.CowAppearance && window.CowAppearance.getState
    && window.CowAppearance.getState().resolved) || 'light';
let appearanceTrigger = null;

function renderAppearancePreferences() {
    const state = window.CowAppearance.getState();
    document.querySelectorAll('input[name="web-palette"]').forEach(input => {
        input.checked = input.value === state.palette;
    });
    document.querySelectorAll('input[name="web-mode"]').forEach(input => {
        input.checked = input.value === state.mode;
    });
    document.querySelectorAll('input[name="web-language"]').forEach(input => {
        input.checked = input.value === currentLang;
    });
    const warning = document.getElementById('appearance-storage-warning');
    if (warning) { warning.hidden = !state.storageFailed; warning.textContent = t('appearance_storage_failed'); }
    const languageWarning = document.getElementById('appearance-language-warning');
    if (languageWarning) {
        languageWarning.hidden = !languageStorageFailed;
        languageWarning.textContent = t('account_prefs_storage_fail');
    }
    const resolved = document.getElementById('appearance-resolved');
    if (resolved) {
        resolved.hidden = state.mode !== 'system';
        resolved.textContent = t('appearance_resolved_' + state.resolved);
    }
}

window.CowAppearance.subscribe(state => {
    currentTheme = state.resolved;
    const light = document.getElementById('hljs-light');
    const dark = document.getElementById('hljs-dark');
    if (light) light.disabled = state.resolved === 'dark';
    if (dark) dark.disabled = state.resolved !== 'dark';
    renderAppearancePreferences();
});

function applyTheme() { window.CowAppearance.apply(); }

// Compatibility for existing direct light/dark actions, without another store.
function toggleTheme() {
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
    });
    appearanceDialog.addEventListener('click', event => {
        if (event.target !== appearanceDialog) return;
        const rect = appearanceDialog.getBoundingClientRect();
        if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) {
            closeAppearancePreferences();
        }
    });
}

// =====================================================================
// Task completion notification (client-side preference)
// =====================================================================
const TASK_NOTIFY_KEY = 'cow_task_notify';
const TASK_NOTIFY_SOUND_KEY = 'cow_task_notify_sound';
let taskNotifyEnabled = readStartupPreference(TASK_NOTIFY_KEY) !== '0';
let taskNotifySound = readStartupPreference(TASK_NOTIFY_SOUND_KEY) !== '0';
let notifyAudioCtx = null;
let unreadCount = 0;
const baseDocTitle = document.title;

// Unlock audio on the first user gesture; browsers block autoplay otherwise.
document.addEventListener('pointerdown', function() {
    if (window.AudioContext) notifyAudioCtx = notifyAudioCtx || new AudioContext();
    if (notifyAudioCtx && notifyAudioCtx.state === 'suspended') {
        notifyAudioCtx.resume().catch(function() {});
    }
}, { once: true });

function playNotifyBeep() {
    if (!taskNotifySound) return;
    try {
        if (!notifyAudioCtx) {
            const Ctx = window.AudioContext || window.webkitAudioContext;
            if (!Ctx) return;
            notifyAudioCtx = new Ctx();
        }
        if (notifyAudioCtx.state === 'suspended') {
            notifyAudioCtx.resume().catch(function() {});
        }
        // Two short sine tones (A5 → D6); no audio asset needed.
        const t0 = notifyAudioCtx.currentTime;
        [880, 1174.66].forEach(function(freq, i) {
            const at = t0 + i * 0.09;
            const osc = notifyAudioCtx.createOscillator();
            const gain = notifyAudioCtx.createGain();
            osc.type = 'sine';
            osc.frequency.value = freq;
            gain.gain.setValueAtTime(0.001, at);
            gain.gain.exponentialRampToValueAtTime(0.12, at + 0.01);
            gain.gain.exponentialRampToValueAtTime(0.001, at + 0.09);
            osc.connect(gain).connect(notifyAudioCtx.destination);
            osc.start(at);
            osc.stop(at + 0.1);
        });
    } catch (_) {
        // Autoplay still blocked or AudioContext unavailable; stay silent.
    }
}

function firstLineSnippet(text) {
    return (text || '').split('\n')[0].trim().slice(0, 80);
}

function sessionTitleOf(sid) {
    const el = document.querySelector(`.session-item[data-session-id="${sid}"] .session-title`);
    return el ? el.textContent.trim() : '';
}

function popNotification(title, body, sid) {
    if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
    try {
        const n = new Notification(title, { body: body || title });
        n.onclick = function() {
            window.focus();
            if (sid && sid !== sessionId) switchSession(sid);
            n.close();
        };
    } catch (_) {
        // Notification API unavailable; beep + title badge still applied.
    }
}

function showTaskNotification(title, body, sid) {
    if (!taskNotifyEnabled) return;
    // Only notify when the window is not focused. If the user is actively
    // watching the tab, the reply is already on screen — a notification/beep
    // would just be noise (especially for short tasks).
    if (document.hasFocus()) return;
    playNotifyBeep();
    if (document.hidden) {
        unreadCount += 1;
        document.title = `(${unreadCount}) ${baseDocTitle}`;
    }
    if (typeof Notification === 'undefined') return;
    // First time we actually need to notify (window is in the background):
    // request permission now, then show this notification once granted. This
    // is more contextual than prompting on page load.
    if (Notification.permission === 'default') {
        Notification.requestPermission()
            .then(function(perm) {
                if (perm === 'granted') popNotification(title, body, sid);
                else refreshNotifyBlockedHint();
            })
            .catch(function() {});
        return;
    }
    if (Notification.permission === 'denied') {
        // Can't notify; surface the hint in settings so the user knows why.
        refreshNotifyBlockedHint();
        return;
    }
    popNotification(title, body, sid);
}

function notifyTaskFinished(sid, kind, text) {
    const label = t(kind === 'error' ? 'notify_task_error' : 'notify_task_done');
    const snippet = firstLineSnippet(text);
    showTaskNotification(sessionTitleOf(sid) || label, snippet ? `${label}: ${snippet}` : label, sid);
}

document.addEventListener('visibilitychange', function() {
    if (!document.hidden) {
        unreadCount = 0;
        document.title = baseDocTitle;
    }
});

// Request OS notification permission when notifications are enabled and the
// browser hasn't decided yet. Safe to call repeatedly.
function ensureNotifyPermission() {
    if (taskNotifyEnabled
        && typeof Notification !== 'undefined'
        && Notification.permission === 'default') {
        Notification.requestPermission().catch(function() {});
    }
}

// Show the "blocked by browser" hint only when notifications are enabled but
// the browser permission is denied (nothing the app can do about it in code).
function refreshNotifyBlockedHint() {
    const el = document.getElementById('cfg-task-notify-blocked');
    if (!el) return;
    const blocked = taskNotifyEnabled
        && typeof Notification !== 'undefined'
        && Notification.permission === 'denied';
    el.classList.toggle('hidden', !blocked);
}

function initTaskNotifyToggles() {
    const notifyEl = document.getElementById('cfg-task-notify');
    if (notifyEl) {
        notifyEl.checked = taskNotifyEnabled;
        notifyEl.addEventListener('change', function() {
            taskNotifyEnabled = notifyEl.checked;
            localStorage.setItem(TASK_NOTIFY_KEY, taskNotifyEnabled ? '1' : '0');
            ensureNotifyPermission();
            refreshNotifyBlockedHint();
        });
    }
    const soundEl = document.getElementById('cfg-task-notify-sound');
    if (soundEl) {
        soundEl.checked = taskNotifySound;
        soundEl.addEventListener('change', function() {
            taskNotifySound = soundEl.checked;
            localStorage.setItem(TASK_NOTIFY_SOUND_KEY, taskNotifySound ? '1' : '0');
        });
    }
    refreshNotifyBlockedHint();
}

document.addEventListener('DOMContentLoaded', initTaskNotifyToggles);

// =====================================================================
// Sidebar & Navigation
// =====================================================================
const VIEW_META = {
    chat:     { group: 'nav_workbench', page: 'menu_chat', console: 'workbench.chat' },
    history:  { group: 'nav_workbench', page: 'session_history', console: 'workbench.history' },
    'agent-workbench': { group: 'nav_workbench', page: 'menu_agents', console: 'workbench.agents' },
    todo:     { group: 'nav_workbench', page: 'menu_todo', console: 'workbench.todos' },
    tasks:    { group: 'nav_workbench', page: 'menu_tasks', console: 'workbench.schedules' },
    knowledge:{ group: 'nav_workbench', page: 'menu_knowledge', console: 'workbench.knowledge' },
    scenes:   { group: 'nav_workbench', page: 'menu_scenes', console: 'workbench.scenes' },
    // The retired member personal pages are kept here as **addresses only**
    // (change unify-console-by-data-scope, task 8.8): the five view ids no
    // longer have a host in the shell, no module registers them, and the backend
    // no longer signs their ``personal.*`` page ids. `navigateTo` rewrites each
    // one to the shared page that carries the same objects (task 8.1) — which is
    // why the entries must stay: the hash bootstrap only forwards an address it
    // can resolve through ``VIEW_META``, so deleting them would break an old
    // bookmark instead of redirecting it.
    'personal-agents':   { group: 'nav_group_personal', page: 'menu_personal_agents', console: 'personal.agents' },
    'personal-channels': { group: 'nav_group_personal', page: 'menu_personal_channels', console: 'personal.channels' },
    'personal-memory':   { group: 'nav_group_personal', page: 'menu_personal_memory', console: 'personal.memory' },
    'personal-tools':    { group: 'nav_group_personal', page: 'menu_personal_tools', console: 'personal.tools' },
    'personal-skills':   { group: 'nav_group_personal', page: 'menu_personal_skills', console: 'personal.skills' },
    agents:   { group: 'nav_group_agent_dev', page: 'menu_agent_config', console: 'admin.agents' },
    skills:   { group: 'nav_group_agent_dev', page: 'menu_skills', console: 'admin.skills' },
    memory:   { group: 'nav_group_agent_dev', page: 'menu_memory', console: 'admin.memory' },
    config:   { group: 'nav_group_model_access', page: 'menu_config', console: 'admin.models' },
    channels: { group: 'nav_group_model_access', page: 'menu_channels', console: 'admin.channels' },
    // 外部系统接入（change add-external-system-access, 任务 10.1）：与「消息渠道」同组并紧随
    // 其后，classic/split 共用同一地址与页面。module 由 `LAZY_VIEW_MODULES` 在首次进入时才
    // 注入，`chat.html` 不引用它，所以未进入本页不会加载连接管理代码。
    'external_connections': { group: 'nav_group_model_access', page: 'menu_external_connections', console: 'admin.external_connections' },
    system_user: { group: 'nav_group_org_perm', page: 'menu_system_user', console: 'admin.roles' },
    roles:       { group: 'nav_group_org_perm', page: 'menu_roles', console: 'admin.roles' },
    org:         { group: 'nav_group_org_perm', page: 'menu_org', console: 'admin.organization' },
    tenant:      { group: 'nav_group_platform_ops', page: 'menu_tenant', console: 'admin.tenants' },
    platform:    { group: 'nav_group_platform_ops', page: 'menu_platform', console: 'admin.tenants' },
    branding:    { group: 'nav_group_platform_ops', page: 'menu_branding', console: 'admin.branding' },
    logs:        { group: 'nav_group_platform_ops', page: 'menu_logs', console: 'admin.logs' },
    audit:       { group: 'nav_group_platform_ops', page: 'menu_audit', console: 'admin.settings' },
    'admin-home': { group: 'nav_admin_console', page: 'nav_admin_overview', console: null },
};

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

function _registeredConsoleView(viewId) {
    return CONSOLE_VIEW_REGISTRY.find(entry => entry.id === viewId) || null;
}

// Apply the shell's "exactly one view is active" rule to a view id. Split out
// because a registered view creates its container lazily inside its own loader:
// ``navigateTo`` runs the toggle before the container exists, so the loader has
// to be able to re-apply the state once the markup is there (otherwise a
// fork-owned view renders hidden).
function _activateViewContainer(viewId) {
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    const target = document.getElementById('view-' + viewId);
    if (target) target.classList.add('active');
}

// Load a registered view's data when it becomes the active view.
function _loadRegisteredView(viewId) {
    const entry = _registeredConsoleView(viewId);
    if (entry && typeof entry.load === 'function') {
        const pending = entry.load();
        // The loader may build its container asynchronously; re-apply the active
        // state afterwards, but only while this view is still the destination.
        if (pending && typeof pending.then === 'function') {
            pending.then(() => { if (currentView === viewId) _activateViewContainer(viewId); },
                         () => {});
        } else {
            _activateViewContainer(viewId);
        }
        return;
    }
    // A view whose module is not present yet is fetched on first navigation
    // (external connections, task 10.1). The module registers itself through
    // `registerConsoleView` when it evaluates, and this function is called again
    // then; `typeof` keeps an upstream build (or an extracted-source contract
    // test) that lacks the lazy table a silent no-op rather than a throw.
    if (typeof _ensureLazyViewModule === 'function') _ensureLazyViewModule(viewId);
}

// View id -> module injected on first navigation. A view not listed here must
// already be registered (or have no module at all), exactly as before.
const LAZY_VIEW_MODULES = {
    external_connections: 'assets/js/external-connections.js',
};
const _lazyViewModuleState = {};
const _lazyViewModulePromises = {};

function _loadViewModuleForId(viewId) {
    const src = LAZY_VIEW_MODULES[viewId];
    if (!src || _lazyViewModuleState[viewId]) return false;
    _lazyViewModuleState[viewId] = 'loading';
    _lazyViewModulePromises[viewId] = new Promise((resolve) => {
        const script = document.createElement('script');
        script.src = src;
        script.onload = () => resolve(true);
        script.onerror = () => resolve(false);
        document.head.appendChild(script);
    }).then((ok) => {
        _lazyViewModuleState[viewId] = ok ? 'loaded' : 'failed';
        if (ok) {
            // Re-enter dispatch through the registry now that the module has
            // registered itself; only while this view is still the target.
            if (currentView === viewId) _loadRegisteredView(viewId);
        } else if (typeof _lazyViewModuleFailed === 'function') {
            _lazyViewModuleFailed(viewId);
        }
        return ok;
    });
    return true;
}

function _ensureLazyViewModule(viewId) {
    return _loadViewModuleForId(viewId);
}

// A module that could not be fetched must not leave the page on a silent blank
// shell: render a terminal explanation into the view container itself.
function _lazyViewModuleFailed(viewId) {
    const container = document.getElementById('view-' + viewId);
    if (!container) return;
    const message = 'Failed to load assets/js/' + viewId + '.js';
    container.innerHTML = '<div class="p-6 text-sm text-red-500" role="alert">' +
        message.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])) +
        '</div>';
}

// Lightweight re-render of a registered view after a language switch.
function _repaintRegisteredView(viewId) {
    const entry = _registeredConsoleView(viewId);
    if (entry && typeof entry.repaint === 'function') entry.repaint();
}

function _viewTargetArea(viewId) {
    if (viewId === 'admin-home') return 'admin';
    const meta = VIEW_META[viewId];
    const key = meta && meta.console;
    if (key && String(key).indexOf('admin.') === 0) return 'admin';
    return 'workbench';
}

function _bootAreaDefaultView() {
    // A direct URL may name a view in the hash (``/chat#view-personal-memory``),
    // so a bookmark or a pasted link to a personal page opens that page instead
    // of the area default (task 8.4). Unknown hashes fall through to the existing
    // pending-view behaviour, so nothing else changes.
    const hashView = _normalizeViewId(String(location.hash || '').replace(/^#view-/, '').split(/[?/]/)[0]);
    if (hashView && VIEW_META[hashView]) {
        navigateTo(hashView);
        return;
    }
    const area = _navAreaFromPath(location.pathname);
    if (area === 'admin') {
        let pending = null;
        try {
            pending = sessionStorage.getItem('cow_admin_pending_view');
            sessionStorage.removeItem('cow_admin_pending_view');
        } catch (_) {}
        if (pending && VIEW_META[pending]) navigateTo(pending);
        else navigateTo('admin-home');
        return;
    }
    let pendingWb = null;
    try {
        pendingWb = sessionStorage.getItem('cow_workbench_pending_view');
        sessionStorage.removeItem('cow_workbench_pending_view');
        if (sessionStorage.getItem('cow_nav_admin_denied') === '1') {
            sessionStorage.removeItem('cow_nav_admin_denied');
            const status = document.getElementById('account-menu-status');
            if (status) {
                status.textContent = t('nav_admin_denied');
                status.classList.remove('hidden');
            }
        }
    } catch (_) {}
    if (pendingWb && VIEW_META[pendingWb]) navigateTo(pendingWb);
    else if (VIEW_META['chat']) navigateTo('chat');
}

const ADMIN_HOME_DESC_KEYS = {
    agents: 'admin_home_desc_agents',
    skills: 'admin_home_desc_skills',
    memory: 'admin_home_desc_memory',
    config: 'admin_home_desc_config',
    channels: 'admin_home_desc_channels',
    system_user: 'admin_home_desc_system_user',
    org: 'admin_home_desc_org',
    roles: 'admin_home_desc_roles',
    tenant: 'admin_home_desc_tenant',
    platform: 'admin_home_desc_platform',
    branding: 'admin_home_desc_branding',
    logs: 'admin_home_desc_logs',
    audit: 'admin_home_desc_audit',
};

const ADMIN_HOME_SHORTCUT_COLORS = {
    agents: '#3b82f6', skills: '#eab308', memory: '#8b5cf6',
    config: '#14b8a6', channels: '#ef4444', system_user: '#22c55e',
    org: '#f43f5e', roles: '#f97316', tenant: '#64748b',
    platform: '#3b82f6', branding: '#a855f7', logs: '#10b981',
    audit: '#64748b',
};

function _adminHomeFormatInt(n) {
    // ``null`` means "the server could not read this", which is NOT zero, and
    // ``Number(null)`` is ``0`` — so the absent case has to be caught *before*
    // the coercion. Rendering a failed read as 0 tells the operator their tenant
    // is empty when in fact the source broke (task 3.5: 不用零值冒充成功). A
    // real ``0`` still prints as 0, which is the distinction being kept.
    if (n === null || n === undefined) return t('admin_home_kpi_dash');
    const x = Number(n);
    if (!Number.isFinite(x)) return t('admin_home_kpi_dash');
    return x.toLocaleString();
}

function _adminHomeKpiUnavailable(meta) {
    return !!(meta && Array.isArray(meta.unavailable) && meta.unavailable.length);
}

function _renderAdminHomeKpis(kpis, meta) {
    const box = document.getElementById('admin-home-kpis');
    if (!box) return;
    const status = (kpis && kpis.system_status) || 'degraded';
    const member = kpis && kpis.member_count;
    // ``not_permitted`` is a member's normal state and not a failure, so it must
    // not raise the retry hint: retrying will never produce a member count.
    const memberScope = meta && meta.member_count_scope;
    const memberText = (member == null) ? t('admin_home_kpi_dash')
        : _adminHomeFormatInt(member);
    const statusOk = status === 'ok';
    box.innerHTML = `
      <div class="admin-home-kpi">
        <div>
          <div class="admin-home-kpi-label">${escapeHtml(t('admin_home_kpi_agents'))}</div>
          <div class="admin-home-kpi-value">${escapeHtml(_adminHomeFormatInt(kpis && kpis.agent_count))}</div>
        </div>
        <div class="admin-home-kpi-icon" style="background:#dbeafe;color:#2563eb"><i class="fas fa-cube"></i></div>
      </div>
      <div class="admin-home-kpi">
        <div>
          <div class="admin-home-kpi-label">${escapeHtml(t('admin_home_kpi_messages_today'))}</div>
          <div class="admin-home-kpi-value">${escapeHtml(_adminHomeFormatInt(kpis && kpis.messages_today))}</div>
        </div>
        <div class="admin-home-kpi-icon" style="background:#ccfbf1;color:#0d9488"><i class="fas fa-comment"></i></div>
      </div>
      <div class="admin-home-kpi">
        <div>
          <div class="admin-home-kpi-label">${escapeHtml(t('admin_home_kpi_members'))}</div>
          <div class="admin-home-kpi-value">${escapeHtml(memberText)}</div>
        </div>
        <div class="admin-home-kpi-icon" style="background:#ede9fe;color:#7c3aed"><i class="fas fa-user"></i></div>
      </div>
      <div class="admin-home-kpi">
        <div>
          <div class="admin-home-kpi-label">${escapeHtml(t('admin_home_kpi_system'))}</div>
          <div class="admin-home-kpi-value ${statusOk ? 'is-ok' : 'is-bad'}">${escapeHtml(statusOk ? t('admin_home_kpi_system_ok') : t('admin_home_kpi_system_bad'))}</div>
        </div>
        <div class="admin-home-kpi-icon" style="background:#dcfce7;color:#16a34a"><i class="fas fa-check"></i></div>
      </div>`;
    // A region the server could not read says so and offers a retry, while the
    // regions that did read stay usable — the requirement asks for exactly that
    // per-region behaviour rather than one failed load blanking the page.
    const hintId = 'admin-home-kpi-unavailable';
    let hint = document.getElementById(hintId);
    if (_adminHomeKpiUnavailable(meta)) {
        if (!hint) {
            hint = document.createElement('div');
            hint.id = hintId;
            hint.className = 'admin-home-kpi-note';
            box.parentNode.insertBefore(hint, box.nextSibling);
        }
        hint.dataset.unavailable = (meta.unavailable || []).join(',');
        hint.innerHTML = `${escapeHtml(t('admin_home_kpi_unavailable'))}`
            + ` <button type="button" class="btn btn-link" data-admin-home-retry>${escapeHtml(t('admin_home_kpi_retry'))}</button>`;
    } else if (hint) {
        hint.remove();
    }
}

function _renderAdminHomeShortcuts() {
    const box = document.getElementById('admin-home-shortcuts');
    if (!box) return;
    box.innerHTML = '';
    document.querySelectorAll('[data-nav-shell="admin"] .sidebar-item[data-view]').forEach(item => {
        if (item.classList.contains('hidden')) return;
        if (item.id === 'nav-admin-home') return;
        const viewId = item.dataset.view;
        if (!viewId || viewId === 'admin-home' || !VIEW_META[viewId]) return;
        const label = item.querySelector('[data-i18n]');
        const text = label ? label.textContent : t(VIEW_META[viewId].page);
        const icon = item.querySelector('i');
        const iconClass = icon ? icon.className : 'fas fa-circle';
        const descKey = ADMIN_HOME_DESC_KEYS[viewId];
        const desc = descKey ? t(descKey) : '';
        const color = ADMIN_HOME_SHORTCUT_COLORS[viewId] || '#64748b';
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'admin-home-shortcut';
        btn.innerHTML =
            `<span class="admin-home-shortcut-icon" style="background:${color}" aria-hidden="true"><i class="${iconClass}"></i></span>` +
            `<span class="admin-home-shortcut-copy">` +
            `<span class="admin-home-shortcut-title"></span>` +
            `<span class="admin-home-shortcut-desc"></span></span>`;
        btn.querySelector('.admin-home-shortcut-title').textContent = text;
        btn.querySelector('.admin-home-shortcut-desc').textContent = desc;
        btn.addEventListener('click', () => navigateTo(viewId));
        box.appendChild(btn);
    });
}

function initAdminHomeView() {
    const kpiBox = document.getElementById('admin-home-kpis');
    if (kpiBox) {
        kpiBox.innerHTML = `<div class="admin-home-kpi"><div class="admin-home-kpi-label">${escapeHtml(t('admin_home_kpi_loading'))}</div></div>`;
    }
    // The retry is delegated from the container so it survives the re-render
    // that follows a failed read (the button is created inside that render).
    if (kpiBox && !kpiBox.dataset.retryWired) {
        kpiBox.dataset.retryWired = '1';
        kpiBox.parentNode.addEventListener('click', (event) => {
            if (event.target && event.target.closest('[data-admin-home-retry]')) {
                initAdminHomeView();
            }
        });
    }
    _renderAdminHomeShortcuts();
    fetch('/api/admin/overview', { credentials: 'same-origin' })
        .then(r => r.json().then(body => ({ ok: r.ok, body })))
        .then(({ ok, body }) => {
            if (!ok || !body || body.status !== 'ok') {
                _renderAdminHomeKpis({ system_status: 'degraded' }, {});
                return;
            }
            _renderAdminHomeKpis(body.kpis || {}, body.meta || {});
        })
        .catch(() => _renderAdminHomeKpis({ system_status: 'degraded' }, {}));
}

// Known previously-visible targets whose feature is not yet enabled. These are
// removed from the normal sidebar, but old internal IDs and direct links must
// not silently no-op: route them to a clear "not available" view with a return.
// 场景应用（scenes）已由本变更转为真实入口，其旧占位 id「scenarios」在
// navigateTo 中重定向到 scenes，不再走「功能尚未开放」分支。
const UNAVAILABLE_VIEWS = new Set(['backup', 'open_api']);

function showUnavailableView(viewId, reason) {
    // reason: undefined/'' -> feature not yet open (nav_unavailable);
    // 'denied' -> the identity lacks read access (nav_denied, distinct copy).
    // A denied target does NOT silently switch scope: we render the denial
    // explanation and only offer a way back.
    const denied = reason === 'denied';
    currentView = viewId;
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    const target = document.getElementById('view-unavailable');
    if (target) target.classList.add('active');
    document.querySelectorAll('.sidebar-item').forEach(item => {
        item.classList.remove('active');
        item.removeAttribute('aria-current');
    });
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
const LEGACY_PERSONAL_FORWARD = {
    'personal-agents': 'agents',      // 智能体管理, filtered to the caller's range
    'personal-channels': 'channels',  // 消息渠道, member = their own connections
    'personal-memory': 'memory',      // 记忆管理, target set served by the backend
    'personal-tools': 'skills',       // 工具与技能
    'personal-skills': 'skills',      // 工具与技能
};

// The destination for a retired personal address, or '' to leave it alone.
// Only ``VIEW_META`` decides what is addressable, so a forward can never name a
// view the console does not have.
function legacyPersonalForward(viewId) {
    const target = LEGACY_PERSONAL_FORWARD[viewId];
    return (target && VIEW_META[target]) ? target : '';
}

// View-id aliases kept as addresses, like the retired personal pages above: the
// design route writes 外部系统接入 as `external-connections` while the view id
// (and `data-view`) uses an underscore like `system_user`. Normalising here, the
// single dispatch and the hash bootstrap agree, so a pasted deep link
// (`/chat#view-external-connections`) or an in-page click both reach the page.
function _normalizeViewId(viewId) {
    return viewId === 'external-connections' ? 'external_connections' : viewId;
}

function navigateTo(viewId) {
    // 旧「场景应用」占位 id 重定向到真实 scenes 视图（收藏/直链不失效）。
    if (viewId === 'scenarios') viewId = 'scenes';
    viewId = _normalizeViewId(viewId);
    // Retired personal addresses forward to the shared page carrying the same
    // objects (task 8.1). The address is rewritten so the URL shows the page
    // that is actually displayed rather than the one it replaced; the retired
    // independent implementation (``personal-console.js``) is gone, so there is
    // no in-flight personal load left to invalidate.
    //
    // The forward is *authorised*, not exempt: it runs BEFORE the availability
    // and leave gates below, so the destination is judged by exactly the
    // verdicts it would get on its own. A forward into a page this identity
    // cannot use renders that page's denial, and the leave check still runs
    // before anything is committed — an old bookmark is never a way around a
    // gate, and never a way to silently discard an unsaved draft.
    const legacyForward = legacyPersonalForward(viewId);
    if (legacyForward) {
        if (typeof window !== 'undefined' && window.history && window.history.replaceState
                && String(location.hash || '').indexOf('#view-') === 0) {
            window.history.replaceState(null, '', '#view-' + legacyForward);
        }
        viewId = legacyForward;
    }
    if (UNAVAILABLE_VIEWS.has(viewId)) {
        showUnavailableView(viewId);
        return;
    }
    if (!VIEW_META[viewId]) return;
    // Authoritative availability gate (database mode only). A target the
    // identity may not read and that is not open is rendered as a denial, NOT
    // silently switched to another scope. Works only once the /auth/context
    // projection is known; until then navigation is not spuriously blocked.
    const deny = _viewNavDenied(viewId);
    if (deny) {
        showUnavailableView(viewId, deny.reason);
        return;
    }
    // The leave decision comes BEFORE any commit of the target. A cross-area
    // target (console <-> workbench) must not push a new URL, re-render the area
    // shell or start the target's consumer while an unsaved form is still asking
    // to discard: the current region, address, draft and current item survive a
    // cancel. The single check runs once per navigation, and the nested
    // ``navigateTo`` that the area switch performs reuses the approval instead of
    // asking twice.
    if (!_viewLeaveApproved(viewId) && !_viewLeaveCheck(viewId)) return;
    // Cross-area: switch to the other area in the SAME window (no reload).
    const here = _navAreaFromPath(location.pathname);
    const want = _viewTargetArea(viewId);
    if (want !== here) {
        try {
            if (want === 'admin') sessionStorage.setItem('cow_admin_pending_view', viewId);
            else sessionStorage.setItem('cow_workbench_pending_view', viewId);
        } catch (_) {}
        _navApprovedTarget = viewId;
        _openNavArea(want);
        _navApprovedTarget = null;
        return;
    }
    if (viewId !== currentView) {
        agentNavigationVersion++;
        cancelAgentStart();
    }
    // Entering any functional view re-validates the public brand snapshot so
    // another tab's published change is picked up (unless the branding page
    // itself has a live draft, which is protected separately).
    if (viewId !== 'branding') fetchPublicBrand();

    // Leaving the history page: mark it dirty so a later re-entry re-reads the
    // newest list (titles/activity may have changed while we were elsewhere).
    if (currentView === 'history' && viewId !== 'history') {
        _historyDirty = true;
        _cancelHistoryRequest();
        _closeSessionActionMenu();
    }

    _activateViewContainer(viewId);
    document.querySelectorAll('.sidebar-item').forEach(item => {
        const selected = item.dataset.view === viewId;
        item.classList.toggle('active', selected);
        if (selected) item.setAttribute('aria-current', 'page');
        else item.removeAttribute('aria-current');
        if (selected) {
            const resources = item.closest('details');
            if (resources) resources.open = true;
            const group = item.closest('.menu-group');
            if (group) { group.classList.add('open'); group.querySelector('button')?.setAttribute('aria-expanded', 'true'); }
        }
    });
    // A personal page gets its single current marker in the account panel, and
    // the main navigation keeps none (see console-information-architecture). The
    // marker is synced right after ``currentView`` is committed below, so a
    // cancelled leave or a denial never moves it.
    const meta = VIEW_META[viewId];
    document.getElementById('breadcrumb-group').textContent = t(meta.group);
    document.getElementById('breadcrumb-group').dataset.i18n = meta.group;
    document.getElementById('breadcrumb-page').textContent = t(meta.page);
    document.getElementById('breadcrumb-page').dataset.i18n = meta.page;
    currentView = viewId;
    document.getElementById('chat-agent-identity')?.classList.toggle('hidden', viewId !== 'chat');
    document.getElementById('workspace-toggle-btn')?.classList.toggle('hidden', viewId !== 'chat');
    if (viewId === 'branding') initBrandingView();
    // Fork-owned views load through the registry (task 8.6); console.js holds
    // no fork-only loader branches.
    _loadRegisteredView(viewId);
    if (viewId === 'admin-home') initAdminHomeView();
    // The Agent detail is a fixed drawer, so it would otherwise hang over
    // whatever view you navigate to. It only belongs to the Agent Config page.
    if (viewId !== 'agents') closeAgentDetail();

    // Entering the history page: it is now the active consumer, so (re)load its
    // list. Re-reading only happens when dirty or the list is empty, so a plain
    // in-page refresh is not spuriously overwritten by a stale read.
    if (viewId === 'history') {
        _historyVisible = true;
        if (_historyDirty || !_sessionItems.length) {
            _historyDirty = false;
            loadSessionList();
        }
    } else {
        _historyVisible = false;
    }

    if (viewId === 'agents') {
        loadAgentCatalog();
    } else if (viewId === 'agent-workbench') {
        loadAgentWorkbench();
    } else if (viewId === 'scenes') {
        if (typeof window.loadScenesView === 'function') window.loadScenesView();
    }

    // Clear status messages when navigating away
    document.querySelectorAll('[id$="-status"]').forEach(el => {
        el.classList.add('opacity-0');
    });

    if (viewId === 'history') _renderHistoryStatus();

    if (window.innerWidth < 1024) closeSidebar();
}

// The leave check has already run for this target (the cross-area path commits
// the area first and re-enters navigateTo for the same view).
let _navApprovedTarget = null;

function _viewLeaveApproved(viewId) {
    return _navApprovedTarget === viewId;
}
// Ask the current view whether it may be left. Returns true when the caller may
// commit the target, false when the current view asked to stay (a cancelled
// discard) or will re-enter navigation itself after the confirmation.
function _viewLeaveCheck(viewId) {
    if (viewId === currentView) return true;
    const adminViews = ['tenant', 'system_user', 'roles', 'org', 'platform', 'audit'];
    if (currentView === 'branding' && brandingDirty) {
        brandingConfirmDiscard(() => {
            _brandingResetDraftToBaseline();
            navigateTo(viewId);
        });
        return false;
    }
    if (adminViews.indexOf(currentView) >= 0
        && typeof window.__identityAdminDirtyGuard__ === 'function'
        && !window.__identityAdminDirtyGuard__()) {
        return false;
    }
    // A registered view may own an in-page draft (external connections, task
    // 10.6). It asks before the target is committed, so a cancelled discard keeps
    // the page, route, scope and input untouched.
    const registered = _registeredConsoleView(currentView);
    if (registered && typeof registered.confirmLeave === 'function') {
        if (registered.confirmLeave(viewId) !== true) return false;
    }
    // The leave is committed now: let the outgoing view release its own
    // resources (timers, in-memory drafts) before the next one loads.
    if (registered && typeof registered.onLeave === 'function') registered.onLeave();
    return true;
}

// The account panel's own chrome. The five 「我的」 entries that this block used to
// wire (change move-personal-menu-to-account) are retired by task 3.3, so the panel
// now owns no business entry and no activation adapter.
function _initAccountMenuChrome() {
    if (typeof document.getElementById !== 'function') return;
    // Tapping the backdrop of the mobile sheet closes it, like the drawer behind
    // it; the sheet itself never closes on an inner click.
    document.getElementById('account-menu-backdrop')
        ?.addEventListener('click', () => closeAccountMenu(true));
}

_initAccountMenuChrome();
// === ACCOUNT_PERSONAL_NAV_END ===

function toggleSidebar() {
    if (window.innerWidth >= 1024) {
        closeAccountMenu();
        const collapsed = document.getElementById('app').classList.toggle('sidebar-collapsed');
        document.getElementById('menu-toggle')?.setAttribute('aria-expanded', String(!collapsed));
        return;
    }
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebar-overlay');
    const isOpen = !sidebar.classList.contains('-translate-x-full');
    if (isOpen) {
        closeSidebar();
    } else {
        sidebar.classList.remove('-translate-x-full');
        overlay.classList.remove('hidden');
        document.getElementById('menu-toggle')?.setAttribute('aria-expanded', 'true');
    }
}

function closeSidebar() {
    closeAccountMenu();
    document.getElementById('sidebar').classList.add('-translate-x-full');
    document.getElementById('sidebar-overlay').classList.add('hidden');
    if (window.innerWidth < 1024) document.getElementById('menu-toggle')?.setAttribute('aria-expanded', 'false');
}

/* The sidebar's launch control body: start a chat the way the session panel
   does, reached from the sidebar. Its caret is handled by the shared
   `onNewChatButton`, which opens the very same picker the history page uses —
   both are one flow with one candidate rule. */
function startSidebarNewChat() {
    if (typeof wsGuardUnsaved === 'function' && !wsGuardUnsaved(startSidebarNewChat)) return;
    if (currentView === 'branding' && brandingDirty) {
        brandingConfirmDiscard(() => { _brandingResetDraftToBaseline(); startSidebarNewChat(); });
        return;
    }
    navigateTo('chat');
    if (currentView !== 'chat') return;
    newChat();
    focusChatComposer();
}

/* =====================================================================
   Refined workbench sidebar (temporary presentation switch)
   =====================================================================
   `workbench_sidebar_launch_v2` gates *layout only*: the launch control's caret
   and picker, the navigation order and the five-row recent preview. It never
   gates a team rule — the candidate projection that keeps coding Agents out, the
   server-side roster rejection and the save-then-commit start all apply with
   the switch on or off (spec: 呈现回退不撤销团队类型边界). Turning it off
   restores the old sidebar exactly, because everything here is additive. */
function sidebarLaunchV2() {
    if (typeof window === 'undefined') return false;
    return String(window.__COW_WORKBENCH_SIDEBAR_LAUNCH_V2__ || '0').trim() === '1';
}

// The navigation order the refined sidebar promises, as view ids. The markup
// keeps its own order, so switching the flag back off needs no markup change.
const SIDEBAR_V2_VIEW_ORDER = ['chat', 'agent-workbench', 'scenes', 'knowledge', 'tasks'];

function applySidebarLaunchV2() {
    const on = sidebarLaunchV2();
    if (typeof document === 'undefined' || !document.body) return on;
    // The class sits on the sidebar it restyles, so the launch control's spacing
    // and the preview spacing need no rule that reaches outside it.
    const sidebar = document.getElementById('sidebar');
    if (sidebar) sidebar.classList.toggle('sidebar-launch-v2', on);
    // The caret belongs to this layout and is only offered when there is
    // something to choose between; `syncNewChatControls` owns that decision so
    // the session panel and the sidebar can never disagree about it.
    syncNewChatControls();
    if (!on) return on;
    const items = document.querySelector(
        '[data-nav-shell="workbench"] .menu-group[data-group="chat"] .menu-group-items');
    if (!items) return on;
    const byView = new Map();
    items.querySelectorAll('.sidebar-item[data-view]').forEach(el => {
        if (el.dataset.view) byView.set(el.dataset.view, el);
    });
    // Re-appending a node moves it. Known entries land in the promised order; an
    // entry this list does not know about keeps working, just before them.
    SIDEBAR_V2_VIEW_ORDER.forEach(view => {
        const el = byView.get(view);
        if (el) items.appendChild(el);
    });
    return on;
}

// Keep closed groups out of the keyboard focus order and move focus to the
// group trigger when a group that currently holds focus is collapsed. This
// fixes the "collapsed group still Tab-focusable" accessibility problem.
function _syncMenuGroupFocusability() {
    document.querySelectorAll('.menu-group').forEach(group => {
        const open = group.classList.contains('open');
        group.querySelectorAll('.sidebar-item').forEach(item => {
            item.tabIndex = open ? 0 : -1;
        });
    });
    // The resources <details> is a native disclosure; only its open children
    // should be focusable.
    const resources = document.getElementById('sidebar-resources');
    if (resources) {
        const open = resources.open;
        resources.querySelectorAll('.sidebar-item').forEach(item => {
            item.tabIndex = open ? 0 : -1;
        });
    }
}

function _collapseMenuGroup(group, trigger) {
    group.querySelectorAll('.sidebar-item').forEach(item => { item.tabIndex = -1; });
    const focusedInGroup = group.contains(document.activeElement);
    group.classList.remove('open');
    trigger.setAttribute('aria-expanded', 'false');
    if (focusedInGroup) trigger.focus();
}

document.querySelectorAll('.menu-group > button').forEach(btn => {
    const label = btn.querySelector('[data-i18n]');
    if (label) { btn.dataset.i18nTitle = label.dataset.i18n; btn.title = t(label.dataset.i18n); }
    btn.setAttribute('aria-expanded', String(btn.parentElement.classList.contains('open')));
    btn.addEventListener('click', () => {
        if (window.innerWidth >= 1024 && document.getElementById('app').classList.contains('sidebar-collapsed')) toggleSidebar();
        const group = btn.parentElement;
        const opening = !group.classList.contains('open');
        group.classList.toggle('open', opening);
        btn.setAttribute('aria-expanded', String(opening));
        if (opening) _syncMenuGroupFocusability();
        else _collapseMenuGroup(group, btn);
    });
});
document.querySelector('#sidebar-resources summary')?.addEventListener('click', () => {
    if (window.innerWidth >= 1024 && document.getElementById('app').classList.contains('sidebar-collapsed')) toggleSidebar();
    const resources = document.getElementById('sidebar-resources');
    // The browser toggles `open` on the native <details>; re-sync focusability.
    setTimeout(_syncMenuGroupFocusability, 0);
});

document.querySelectorAll('.sidebar-item').forEach(item => {
    item.setAttribute('role', 'link');
    item.tabIndex = 0;
    const label = item.querySelector('[data-i18n]');
    if (label) { item.dataset.i18nTitle = label.dataset.i18n; item.title = t(label.dataset.i18n); }
    if (item.classList.contains('active')) item.setAttribute('aria-current', 'page');
    item.addEventListener('click', (event) => {
        if (item.id === 'nav-open-admin') {
            event.preventDefault();
            _openNavArea('admin');
            return;
        }
        if (item.id === 'nav-return-workbench') {
            event.preventDefault();
            _openNavArea('workbench');
            return;
        }
        if (!item.dataset.view) return;
        navigateTo(item.dataset.view);
    });
    item.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            if (item.id === 'nav-open-admin') { _openNavArea('admin'); return; }
            if (item.id === 'nav-return-workbench') { _openNavArea('workbench'); return; }
            if (!item.dataset.view) return;
            navigateTo(item.dataset.view);
        }
    });
});

// Ensure closed groups are excluded from the tab order (run after the items
// get their default tabIndex so it is authoritative).
_syncMenuGroupFocusability();

// Return to an available page from the "not available" view (unreachable).
document.getElementById('nav-unavailable-back')?.addEventListener('click', () => {
    const next = ['chat', 'history', 'agent-workbench', 'todo', 'tasks', 'knowledge', 'agents']
        .find(v => VIEW_META[v] && document.getElementById('view-' + v));
    if (next) navigateTo(next);
});

function syncSidebarToggleState() {
    const expanded = window.innerWidth >= 1024
        ? !document.getElementById('app').classList.contains('sidebar-collapsed')
        : !document.getElementById('sidebar').classList.contains('-translate-x-full');
    document.getElementById('menu-toggle')?.setAttribute('aria-expanded', String(expanded));
}
syncSidebarToggleState();
window.addEventListener('resize', syncSidebarToggleState);

window.addEventListener('resize', () => {
    closeAccountMenu();
    if (window.innerWidth >= 1024) {
        document.getElementById('sidebar').classList.remove('-translate-x-full');
        document.getElementById('sidebar-overlay').classList.add('hidden');
    } else {
        if (!document.getElementById('sidebar').classList.contains('-translate-x-full')) {
            closeSidebar();
        }
    }
});

// =====================================================================
// Agents
// =====================================================================
let agentCatalog = [];
// The roster the chat pickers read: the Agents the caller may *chat with* (the
// use range — tenant-shared Agents plus their own private ones), which is not
// the same list as ``agentCatalog`` (the management range: what they may
// configure). ``null`` means "not read yet", and the pickers fall back to the
// management catalogue so a backend that cannot serve the use-range read
// behaves exactly as it did before this list existed.
let chatAgentCatalog = null;
let channelInstances = [];
let rosterRevision = '';
let defaultAgentId = readScopedPreference('cow_default_agent') || '';
// The caller's *own* registered default Agent, as the server reports it:
// `{agent_id, revision, origin}`. Distinct from `defaultAgentId` above, which is
// the resolved anchor (the member's own choice, else the tenant's, else a
// deterministic fallback) — a fallback is not a preference, and the detail pane
// must be able to tell them apart. `revision` is the optimistic lock the
// "set as my default" write round-trips; `origin` is 'user' or 'provisioned',
// and null means nothing was ever registered.
let userDefault = { agent_id: '', revision: null, origin: null };
// Whether the caller may appoint the *tenant* default Agent (a management act,
// task 4.5). Taken from the payload rather than guessed from a role name, so the
// button can never offer an action the request would refuse.
let tenantDefaultManageable = false;
// Where the anchor a *new session* would use came from (task 4.6):
// {agent_id, source} with source 'user' | 'tenant' | 'shared' | 'own' | null.
// Kept apart from `defaultAgentId` on purpose — `defaultAgentId` answers "which
// Agent leads the lists" and `defaultResolution` answers "will this actually be
// used, and why". Only the second can tell a user's own choice from a bare
// fallback, which is what the detail pane renders.
let defaultResolution = { agent_id: '', source: null };
let selectedAdminAgentId = '';
let selectedCoreRevision = '';
let installedSkills = [];
let installedTools = [];
function findAgent(agentId) {
    // The management catalogue is the richer record (it carries workspace and
    // configuration state), so it wins when both know the Agent. The use range
    // is the fallback that keeps a shared Agent resolvable for the chat
    // surfaces of a member who does not manage it — the composer face, the
    // message speakers and the session rows all read this.
    return agentCatalog.find(a => a.id === agentId)
        || (chatAgentCatalog || []).find(a => a.id === agentId) || null;
}

function normalizeAgentCatalogEntry(agent) {
    // Database mode returns only enabled, tenant-visible Agents, with can_chat
    // rather than the management snapshot's enabled flag. Keep runtime chat
    // readiness separate from configuration state and never infer permissions
    // from an unknown/missing capability.
    return {
        ...agent,
        name: /^cowagent$/i.test((agent.name || '').trim()) ? 'RongAI' : agent.name,
        enabled: typeof agent.enabled === 'boolean' ? agent.enabled : typeof agent.can_chat === 'boolean',
    };
}

function enabledAgents() {
    return agentCatalog.filter(a => a.enabled === true);
}

/* Which Agents the caller may *chat with* — the use range. This is a different
   question from the management grid's, and the two lists differ for every
   non-admin: their management range is the Agents they own, while their use
   range also holds the tenant-shared Agents they were granted. Drawing the
   chat pickers from the management catalogue therefore hid every shared Agent
   from a plain member. ``chatAgents`` reads the use-range roster and degrades
   to the management catalogue while that read is missing (an older backend),
   never the other way round: the management surfaces keep reading
   ``enabledAgents`` so the extra Agents never leak into configuration. */
function chatAgents() {
    const roster = chatAgentCatalog || agentCatalog;
    return roster.filter(a => a.enabled !== false && a.can_chat !== false);
}

function availableChatAgents() {
    return chatAgents();
}

/* Which Agents may join a *team*. This is a narrower question than "may chat",
   and the difference is the whole point of the projection (change
   refine-sidebar-team-chat-launch, task 2.1):

   - A coding Agent is excluded outright. Its conversation is served by the
     embedded Opencode app and it has no ordinary runtime to take a turn, so it
     can never be a teammate — not as the owner, not as a member.
   - An *unresolved* id is not a candidate either. Absent means "the roster row
     did not say", which a genuine pre-type profile may use, but an id that has
     no row at all is unverified and must never be offered.
   - The use-range read is the only authority. Unlike ``chatAgents()`` this does
     NOT fall back to the management catalogue when that read has not landed:
     the fallback would offer Agents whose ``agent.use`` grant was never
     verified, and team membership is exactly the surface where that matters
     (design D2).

   Every team surface reads this one projection — the picker, its search, the
   preselect, the default-responder candidates, the selected summary, the
   in-conversation invite list and the @ candidates — so a rule change cannot
   leave one of them offering a coding Agent. */
function teamCandidateAgents() {
    const roster = Array.isArray(chatAgentCatalog) ? chatAgentCatalog : [];
    return roster.filter(agent => agent && agent.enabled !== false && agent.can_chat !== false
        && !agentIsCoding(agent));
}

/* The declared type of a roster *row*: absent means normal, which is how a
   pre-type profile is tolerated without letting an unknown id through. */
function agentIsCoding(agent) {
    return !!agent && agent.agent_type === 'coding';
}

/* An uploaded avatar reuses the same URL every time, so the browser would keep
   serving the stale bytes. The roster revision only moves when the roster's
   *content* changes, and re-uploading over an existing image leaves the field as
   the same "image" token — so we stamp each successful upload with a fresh token
   here and prefer it, which forces the one re-fetch that shows the new picture. */
const avatarVersions = {};

/* How many muted discs the initials fallback cycles through. */
const AVATAR_TONES = 6;

/* Which disc an Agent gets. Keyed off the id alone, so a face never changes
   colour once the Agent exists, and so a draft in the create modal (no id yet)
   sits on the neutral tone instead of shifting as its name is typed. */
function avatarTone(agentId) {
    const key = String(agentId || '');
    let hash = 0;
    for (let i = 0; i < key.length; i++) hash = (hash * 31 + key.charCodeAt(i)) >>> 0;
    return hash % AVATAR_TONES;
}

/* The character an Agent is shown by when it has no picture. Array.from rather
   than [0] so an astral-plane character is taken whole instead of as half a
   surrogate pair; uppercased for latin, left alone for scripts without case. */
function avatarInitial(name) {
    return (Array.from(String(name || '').trim())[0] || '').toUpperCase();
}

/* Every Agent wears its own face: the image its owner uploaded, or a muted disc
   carrying the first character of its name. Initials rather than the product
   logo so a team is distinguishable at a glance, and low-saturation tones so a
   roster of them stays quiet.

   A null agent means the id no longer resolves - a conversation pinned to a
   since-deleted Agent. Fall back to the default Agent's face rather than an
   empty disc, so the deleted Agent visibly degrades to the default one. */
function agentAvatarHTML(agent, size) {
    const cls = `agent-avatar agent-avatar-${size || 32}`;
    if (!agent && defaultAgentId) {
        agent = findAgent(defaultAgentId);
    }
    if (agent && agent.avatar === 'image') {
        const v = avatarVersions[agent.id] || rosterRevision || agent.id;
        return `<img class="${cls}" src="/api/agents/${encodeURIComponent(agent.id)}/avatar?v=${encodeURIComponent(v)}" alt="">`;
    }
    // The default (first) Agent falls back to the product logo when it has no
    // uploaded picture, so the instance's own Agent wears the product mark.
    // Added Agents keep the initial-disc fallback so a team stays distinguishable.
    if (agent && agent.id && (agent.is_default || agent.id === defaultAgentId)) {
        return `<img class="${cls} agent-avatar-brand" src="${effectiveLogoUrl()}" alt="">`;
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
function randomAgentId() {
    return 'agent-' + Math.random().toString(36).slice(2, 8);
}

// Read the use-range roster the chat pickers work from, alongside (not instead
// of) the management catalogue. The workbench read is exactly that projection —
// the same call the 智能体 workbench cards are drawn from — so it is reused here
// rather than duplicated: its shape validation and its refusal to accept a
// management snapshot both apply to the pickers' roster too.
// A failure is not surfaced: the pickers keep the management catalogue they
// used before, which is the correct answer whenever the two lists agree (every
// admin) and only narrower for a member on a backend that cannot serve it.
function loadChatAgentCatalog() {
    return fetchAgentWorkbench().then(agents => {
        chatAgentCatalog = agents;
        // The composer face, both launch controls and any open picker are drawn
        // from this roster, so a list that arrives after the first paint repaints
        // them instead of leaving the earlier (narrower) reading on screen.
        renderComposerIdentity();
        syncNewChatControls();
        if (typeof consumeScenarioOpenLink === 'function') consumeScenarioOpenLink();
    }).catch(() => {});
}

function loadAgentCatalog() {
    const epoch = _authEpoch, tenant = sessionStorage.getItem('cow_tenant_id');
    const current = () => epoch === _authEpoch && tenant === sessionStorage.getItem('cow_tenant_id');
    // The pickers' own roster, read in parallel: it is a separate projection
    // (the use range) from the management catalogue this function returns, and
    // a failure of it must not fail the catalogue read.
    loadChatAgentCatalog();
    return fetch('/api/agents')
        .then(r => r.json())
        .then(data => {
            if (!current()) return;
            if (data.status !== 'success') throw new Error(data.message || 'Failed to load Agents');
            // Also handle a still-running backend or an older team file.
            agentCatalog = (data.agents || []).map(normalizeAgentCatalogEntry);
            channelInstances = data.channel_instances || [];
            rosterRevision = data.revision || '';
            // The caller's own preference and the tenant-level capability, taken
            // from the same payload as the roster so the detail pane's two
            // "default" actions always agree with what the server would allow.
            const pointer = data.user_default;
            userDefault = (pointer && typeof pointer === 'object')
                ? {
                    agent_id: pointer.agent_id || '',
                    revision: (typeof pointer.revision === 'number' ? pointer.revision : null),
                    origin: pointer.origin || null,
                }
                : { agent_id: '', revision: null, origin: null };
            tenantDefaultManageable = data.tenant_default_manageable === true;
            // Why the anchor is the anchor (task 4.6). Server-decided, like every
            // other authorization fact here: the pane must not re-derive the
            // resolution rule, or a fallback could be presented as a decision.
            const anchor = data.default_resolution;
            defaultResolution = (anchor && typeof anchor === 'object')
                ? { agent_id: anchor.agent_id || '', source: anchor.source || null }
                : { agent_id: '', source: null };
            // Never invent an Agent id. The global fetch wrapper copies the active
            // id onto every /message and /api call, so a made-up fallback becomes a
            // real request the server rejects ("agent not found") — a routing error
            // reported where the truth is "you may not see any Agent". An empty id
            // sends nothing and lets the server anchor the tenant's default Agent.
            defaultAgentId = data.default_agent_id || agentCatalog.find(agent => agent.is_default === true)?.id
                || (agentCatalog[0] && agentCatalog[0].id) || '';
            writeScopedPreference('cow_default_agent', defaultAgentId);
            // The default Agent leads every list it appears in — menus, the grid,
            // the memory picker — so its position never depends on load order.
            agentCatalog.sort((a, b) => (b.id === defaultAgentId) - (a.id === defaultAgentId));
            // A remembered Agent this catalogue no longer offers — deleted, unbound,
            // or a literal written by an older build — must stop riding the requests.
            // Reading configuration must not replace a bound session's owner, so an
            // id the catalogue still offers always survives as the explicit choice.
            if (activeAgentId && !agentCatalog.some(agent => agent.id === activeAgentId)) {
                activeAgentId = '';
            }
            if (!activeAgentId) {
                activeAgentId = defaultAgentId;
                writeScopedPreference('cow_active_agent', activeAgentId);
            }
            if (!selectedAdminAgentId || !agentCatalog.some(a => a.id === selectedAdminAgentId)) {
                selectedAdminAgentId = '';
            }
            // Two-pane workbench: on a wide screen, land on the first Agent so the
            // right pane is never a blank placeholder. On a phone the list shows
            // first (the detail is a sheet), so leave nothing selected there.
            if (!selectedAdminAgentId && currentView === 'agents'
                    && agentCatalog.length && window.innerWidth > 900) {
                openAgentDetail((enabledAgents()[0] || agentCatalog[0]).id);
                return data;
            }
            renderAgentsGrid();
            if (selectedAdminAgentId) renderAgentDetail();
            else closeAgentDetail();
            renderComposerIdentity();
            renderMemoryAgentSelect();
            // A launch control only sprouts a menu (and its caret) once there is
            // more than one Agent to choose between.
            syncNewChatControls();
            // A name or avatar may have changed; keep faces already on screen in
            // sync with the roster instead of only new bubbles.
            refreshBubbleAvatars();
            if (typeof consumeScenarioOpenLink === 'function') consumeScenarioOpenLink();
            return data;
        })
        .catch(err => {
            if (!current()) return;
            const status = document.getElementById('agent-editor-status');
            if (status) status.textContent = err.message;
        });
}

function renderAgentsGrid() {
    const grid = document.getElementById('agents-grid');
    if (!grid) return;
    if (!agentCatalog.length) {
        grid.innerHTML = `<div class="col-span-full text-sm text-slate-400 py-16 text-center">${escapeHtml(t('agents_empty'))}</div>`;
        return;
    }
    grid.innerHTML = agentCatalog.map(agent => {
        const selected = agent.id === selectedAdminAgentId;
        const desc = (agent.description || '').trim();
        // Status chips float in the top-right corner so a "default" or
        // "archived" card is exactly as tall as every other card.
        const corner = agent.id === defaultAgentId
            ? `<span class="agent-card-badge agent-chip-on">${escapeHtml(t('agents_default'))}</span>`
            : (!agent.enabled ? `<span class="agent-card-badge">${escapeHtml(t('agents_archived'))}</span>` : '');
        return `<div class="agent-card${selected ? ' selected' : ''}${agent.enabled ? '' : ' archived'}" onclick="openAgentDetail('${escapeHtml(agent.id)}')">
            ${corner}
            <div class="agent-card-top">
                ${agentAvatarHTML(agent, 32)}
                <div class="min-w-0 flex-1">
                    <div class="agent-card-name truncate">${escapeHtml(agent.name)}</div>
                    <div class="agent-card-desc">${desc ? escapeHtml(desc) : `<span class="agent-card-desc-empty">${escapeHtml(t('agents_no_desc'))}</span>`}</div>
                </div>
            </div>
        </div>`;
    }).join('');
}

// =====================================================================
// Agent Workbench (use agents)
// =====================================================================
// The usage page is a read-only card gallery. It deliberately does NOT reuse
// loadAgentCatalog(), which refreshes management data and the composer roster.
// A workbench load must be
// side-effect free: it only fetches /api/agents?view=workbench and paints the
// card grid from the whitelisted projection.
let agentWorkbench = [];
let agentWorkbenchLoading = false;
let agentWorkbenchSeq = 0;
let _wbNoticeKey = '';
// Why the last *successful* read came back empty, as the server's stable code
// (``no_agents`` / ``no_reachable_agents``). Kept apart from ``_wbErrorKey``
// because an empty list is a success: presenting it as a failure would report
// an authorization gap as a broken page and invite a pointless retry.
let _wbEmptyReason = '';
let _wbSearchQuery = '';
let _wbSelectedTag = null; // null = all; '' = untagged; otherwise an exact tag.
let _wbTagsExpanded = false;
let _wbSearchComposing = false;
let _wbFilterIdentity = null;
let _wbFilterOptions = [];
let _wbFilterObserver = null;

function _wbIdentityContext() {
    return JSON.stringify([
        typeof _authEpoch === 'undefined' ? 0 : _authEpoch,
        typeof _accountState === 'undefined' ? '' : _accountState.username || '',
        sessionStorage.getItem('cow_tenant_id') || '', _identityMode(),
    ]);
}

function resetAgentWorkbenchFilters(clearCatalog = false) {
    _wbSearchQuery = '';
    _wbSelectedTag = null;
    _wbTagsExpanded = false;
    _wbSearchComposing = false;
    _wbFilterOptions = [];
    _wbFilterIdentity = null;
    const input = document.getElementById('agent-workbench-search');
    if (input) input.value = '';
    if (clearCatalog) {
        ++agentWorkbenchSeq;
        agentWorkbench = [];
        chatAgentCatalog = [];
        agentWorkbenchLoading = false;
        _wbLoadedError = false;
        _wbEmptyReason = '';
        _wbNoticeKey = '';
        ['agent-workbench-grid', 'agent-workbench-tags'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.innerHTML = '';
        });
        ['agent-workbench-filters', 'agent-workbench-results'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.hidden = true;
        });
        setWbStatus('');
    }
}

function _wbSyncFilterIdentity() {
    const identity = _wbIdentityContext();
    if (_wbFilterIdentity !== null && _wbFilterIdentity !== identity) resetAgentWorkbenchFilters(true);
    _wbFilterIdentity = identity;
}

function agentWorkbenchTags(agent) {
    return [...new Set((Array.isArray(agent.tags) ? agent.tags : [])
        .filter(tag => typeof tag === 'string').map(tag => tag.trim()).filter(Boolean))];
}

// Derive the gallery without narrowing the shared chat/use catalogue.
function agentWorkbenchFilterResult(agents, query, selectedTag) {
    const keyword = String(query || '').trim().toLowerCase();
    const tags = new Map();
    const matched = [];
    let untaggedCount = 0;
    const seen = new Set();
    agents.forEach(agent => {
        if (seen.has(agent.id)) return;
        seen.add(agent.id);
        const values = agentWorkbenchTags(agent);
        values.forEach(tag => { if (!tags.has(tag)) tags.set(tag, 0); });
        const haystack = [agent.name, agent.id, agent.description, agent.position,
            agent.category, ...values].filter(Boolean).join(' ').toLowerCase();
        if (keyword && !haystack.includes(keyword)) return;
        values.forEach(tag => tags.set(tag, tags.get(tag) + 1));
        if (!values.length) untaggedCount++;
        matched.push({ agent, tags: values });
    });
    return {
        options: [{ tag: null, count: matched.length },
            ...Array.from(tags, ([tag, count]) => ({ tag, count })),
            { tag: '', count: untaggedCount }],
        agents: matched.filter(row => selectedTag === null
            || (selectedTag === '' ? !row.tags.length : row.tags.includes(selectedTag)))
            .map(row => row.agent),
    };
}

function onAgentWorkbenchSearch(event) {
    if (_wbSearchComposing || event.isComposing) return;
    _wbSearchQuery = event.target.value;
    paintAgentWorkbench();
    const grid = document.getElementById('agent-workbench-grid');
    if (grid) grid.scrollTop = 0;
}

function clearAgentWorkbenchSearch(all = false) {
    _wbSearchQuery = '';
    _wbSearchComposing = false;
    if (all) _wbSelectedTag = null;
    const input = document.getElementById('agent-workbench-search');
    if (input) { input.value = ''; input.focus(); }
    paintAgentWorkbench();
}

function selectAgentWorkbenchTag(index) {
    if (!Number.isInteger(index) || !_wbFilterOptions[index]) return;
    _wbSelectedTag = _wbFilterOptions[index].tag;
    paintAgentWorkbench();
    const tags = document.getElementById('agent-workbench-tags');
    const selected = tags && tags.querySelector('[aria-pressed="true"]');
    if (selected) selected.focus();
    const grid = document.getElementById('agent-workbench-grid');
    if (grid) grid.scrollTop = 0;
}

function toggleAgentWorkbenchTags() {
    _wbTagsExpanded = !_wbTagsExpanded;
    layoutAgentWorkbenchTags();
}

function layoutAgentWorkbenchTags() {
    const list = document.getElementById('agent-workbench-tags');
    const toggle = document.getElementById('agent-workbench-tags-toggle');
    if (!list || !toggle || !list.clientWidth) return;
    const buttons = Array.from(list.querySelectorAll('.agent-wb-filter-chip'));
    buttons.forEach(button => { button.hidden = false; });
    list.classList.remove('is-expanded');
    toggle.textContent = t(_wbTagsExpanded ? 'agent_workbench_tags_less' : 'agent_workbench_tags_more');
    // Measure the full row first, then reserve the toggle's width before clipping.
    toggle.hidden = true;
    list.style.setProperty('--agent-wb-all-width', `${buttons[0]?.offsetWidth || 0}px`);
    const firstTop = buttons[0]?.offsetTop;
    toggle.hidden = !buttons.some(button => button.offsetTop > firstTop);
    if (!_wbTagsExpanded) {
        const clipped = buttons.filter(button => button.offsetTop > buttons[0]?.offsetTop);
        clipped.forEach(button => { button.hidden = true; });
    }
    list.classList.toggle('is-expanded', _wbTagsExpanded);
    toggle.setAttribute('aria-expanded', String(_wbTagsExpanded));
}

function renderAgentWorkbenchFilters(result) {
    const toolbar = document.getElementById('agent-workbench-filters');
    const summary = document.getElementById('agent-workbench-results');
    const list = document.getElementById('agent-workbench-tags');
    const ready = !_wbLoadedError && agentWorkbench.length > 0;
    if (toolbar) toolbar.hidden = !ready;
    if (summary) summary.hidden = !ready;
    if (!ready || !list) return;
    _wbFilterOptions = result.options;
    // Pin the active tag after All so it stays visible when the list collapses.
    const indices = result.options.map((_, index) => index);
    if (_wbSelectedTag !== null) {
        const selected = result.options.findIndex(option => option.tag === _wbSelectedTag);
        if (selected > 0) indices.splice(1, 0, indices.splice(selected, 1)[0]);
    }
    list.innerHTML = indices.map(index => {
        const option = result.options[index];
        const active = option.tag === _wbSelectedTag;
        const label = option.tag === null ? t('agent_workbench_filter_all')
            : option.tag === '' ? t('agent_workbench_untagged') : option.tag;
        return `<button type="button" class="agent-wb-filter-chip${active ? ' is-active' : ''}"
            aria-pressed="${active}" onclick="selectAgentWorkbenchTag(${index})">
            <span class="agent-wb-filter-label">${escapeHtml(label)}</span>
            <span class="agent-wb-filter-count">${option.count}</span></button>`;
    }).join('');
    const count = document.getElementById('agent-workbench-result-count');
    if (count) count.textContent = t('agent_workbench_result_count').replace('{count}', result.agents.length);
    const reset = document.getElementById('agent-workbench-filter-reset');
    if (reset) reset.hidden = !_wbSearchQuery && _wbSelectedTag === null;
    const clear = document.getElementById('agent-workbench-search-clear');
    if (clear) clear.hidden = !_wbSearchQuery;
    list.setAttribute('aria-label', t('agent_workbench_filter_label'));
    const input = document.getElementById('agent-workbench-search');
    if (input) input.setAttribute('aria-label', t('agent_workbench_search_label'));
    if (clear) clear.setAttribute('aria-label', t('agent_workbench_search_clear'));
    if (!_wbFilterObserver && typeof ResizeObserver !== 'undefined') {
        let width = 0;
        _wbFilterObserver = new ResizeObserver(entries => {
            const next = entries[0].contentRect.width;
            if (next !== width) { width = next; layoutAgentWorkbenchTags(); }
        });
        _wbFilterObserver.observe(list.parentElement);
    }
    layoutAgentWorkbenchTags();
}

function _wbEmptyKey() {
    return _wbEmptyReason === 'no_reachable_agents'
        ? 'agent_workbench_empty_unreachable'
        : 'agent_workbench_empty';
}

function _wbContext() {
    return [activeAgentId, sessionId, currentView, agentNavigationVersion,
        _wbIdentityContext()].join('|');
}

// Stable, localizable names for the ways the read can end badly. A single
// «加载失败» stood in for all of them, which told the reader nothing they could
// act on: a permission denial and an unselected tenant are both recoverable,
// and one of them (nothing selected yet) is not even a fault.
function _wbFailureKey(status, code) {
    if (code === 'missing_tenant') return 'agent_workbench_no_tenant';
    if (status === 403 || code === 'forbidden') return 'agent_workbench_no_permission';
    if (status === 401 || code === 'unauthorized') return 'agent_workbench_signed_out';
    return 'agent_workbench_failed';
}

// A failed read carries its own evidence: `wbKey` is what the user is told,
// `wbTransient` says whether another attempt could plausibly differ, and
// `wbDetail` is the raw status/code kept for the console so a report like
//「加载失败」can be traced without reproducing it live.
function _wbFailure(wbKey, wbTransient, wbDetail) {
    const err = new Error(t(wbKey));
    err.wbKey = wbKey;
    err.wbTransient = !!wbTransient;
    err.wbDetail = wbDetail || wbKey;
    return err;
}

async function fetchAgentWorkbench() {
    let res;
    try {
        res = await fetch('/api/agents?view=workbench', { cache: 'no-store' });
    } catch (err) {
        // No response at all: the transport itself failed, so a retry can differ.
        throw _wbFailure('agent_workbench_failed', true,
            `transport: ${(err && err.message) || err}`);
    }
    let data = null;
    try {
        data = await res.json();
    } catch (_) {
        data = null;
    }
    const status = res.status || 0;
    const code = (data && data.code) || '';
    if (!res.ok || !data || data.status !== 'success' || !Array.isArray(data.agents)) {
        const message = (data && data.message) || '';
        throw _wbFailure(_wbFailureKey(status, code), !res.ok && status >= 500,
            `http ${status}${code ? ` ${code}` : ''}${message ? ` ${message}` : ''}`
                + (data ? '' : ' (non-JSON body)'));
    }
    // An old backend may ignore `view` and return the management snapshot.
    // Fail visibly instead of treating missing capability flags as an empty list.
    if ('channel_instances' in data || 'revision' in data
        || data.agents.some(a => typeof a.id !== 'string' || !a.id
            || typeof a.can_chat !== 'boolean' || typeof a.is_default !== 'boolean')) {
        // A wrong shape is deterministic: a second identical request cannot
        // heal it, so it is reported as the generic failure, never retried.
        throw _wbFailure('agent_workbench_failed', false,
            'workbench projection missing: management snapshot returned');
    }
    const agents = data.agents.map(a => ({
        id: a.id,
        name: /^cowagent$/i.test((a.name || '').trim()) ? 'RongAI' : (a.name || a.id),
        description: a.description || '', avatar: a.avatar || null,
        is_default: a.is_default, can_chat: a.can_chat,
        unavailable_reason: a.unavailable_reason || null,
        // The type decides which surface serves the Agent -- the card badge, the
        // new-chat row and ``agentTypeOf``'s fallback all read it -- and this
        // projection is the one place that could lose it: every other reader
        // spreads the server row, this one names its fields. Dropping it here
        // left a coding Agent indistinguishable from an ordinary one on the very
        // lists that route and label it, however well the card rendered a type
        // it was never handed. Absent still means normal.
        agent_type: a.agent_type,
        // Digital-employee projection fields (positioned to render on cards).
        position: a.position || '', category: a.category || '', tags: agentWorkbenchTags(a),
        greeting: typeof a.greeting === 'string' ? a.greeting : '',
    })).sort((a, b) => Number(b.is_default) - Number(a.is_default));
    // The server's diagnosis of an empty roster rides along on the array so the
    // load/apply contract stays a plain list. ``no_agents`` and ``null`` (an
    // older backend) both mean the plain empty message.
    agents.emptyReason = data.empty_reason === 'no_reachable_agents'
        ? 'no_reachable_agents' : '';
    return agents;
}

function applyAgentWorkbench(agents) {
    _wbSyncFilterIdentity();
    agentWorkbench = agents;
    // The workbench cards and the chat pickers read the same use-range
    // projection, so whichever read lands first fills both.
    chatAgentCatalog = agents;
    _wbEmptyReason = agents.emptyReason || '';
    if (_wbSelectedTag !== null && !agents.some(a => _wbSelectedTag === ''
        ? !agentWorkbenchTags(a).length : agentWorkbenchTags(a).includes(_wbSelectedTag))) {
        _wbSelectedTag = null;
    }
    // Avatars can be replaced without changing their URL or roster revision.
    const version = String(Date.now());
    agents.forEach(a => { if (a.avatar === 'image') avatarVersions[a.id] = version; });
}

function agentUnavailableLabel(reason) {
    if (reason === 'permission_denied') return t('agent_permission_denied');
    if (reason === 'agent_disabled') return t('agent_disabled');
    return t(reason === 'runtime_not_enabled' ? 'agent_runtime_not_enabled' : 'agent_cannot_run');
}

/* Why this Agent is what a new session uses (task 4.6).
   The source is not decoration: `user` and `tenant` are decisions somebody made,
   while `shared` and `own` are fallbacks nobody chose — the second pair is
   exactly what an operator needs to see before concluding "the tenant set this
   up on purpose". An unknown or absent source yields the neutral sentence rather
   than a guess, so a newer server can add a value without this lying about it. */
function agentAnchorHintText() {
    const source = defaultResolution.source;
    if (source === 'user') return t('agents_anchor_source_user');
    if (source === 'tenant') return t('agents_anchor_source_tenant');
    if (source === 'shared') return t('agents_anchor_source_shared');
    if (source === 'own') return t('agents_anchor_source_own');
    return t('agents_anchor_source_unknown');
}

/* The coding type hint is a glyph, not a word (change
   simplify-coding-agent-type-hint). As text it sat in the card's top-right
   corner next to the Agent's own name and read like a second name, while the
   one fact it carries -- this card's action opens the embedded OpenCode pane
   instead of the platform composer -- was buried in a brand string. The icon
   keeps that fact with less ink and moves the wording to the tooltip and the
   accessible name, where it costs no layout and is still readable.

   One implementation for the workbench card, the new-chat picker rows and the
   Agent management identity block: three literal copies would drift into three
   different hints, and nothing would fail when they did. ``extraClass`` is how
   the identity block keeps its ``hidden`` toggle. */
function codingAgentTypeHint(extraClass = '') {
    // escapeHtml() only handles &, < and >, so a translated string landing in a
    // double-quoted attribute needs the quote escaped on top of it.
    const label = escapeHtml(t('agents_type_coding')).replace(/"/g, '&quot;');
    return `<span class="coding-agent-badge${extraClass ? ' ' + extraClass : ''}"`
        + ` role="img" title="${label}" aria-label="${label}">`
        + '<i class="fas fa-terminal" aria-hidden="true"></i></span>';
}

function agentWorkbenchCardHTML(agent, canChat, unavailableReason) {
    const desc = (agent.description || '').trim();
    // The default/archived badge and the type badge share the card's top-right
    // corner; a coding Agent's action opens the embedded pane instead of the
    // platform composer, so which kind this card is has to be visible here too.
    const badges = [
        agent.is_default
            ? `<span class="agent-card-badge agent-chip-on">${escapeHtml(t('agents_default'))}</span>` : '',
        agent.agent_type === 'coding' ? codingAgentTypeHint() : '',
    ].filter(Boolean).join('');
    const badge = badges ? `<span class="agent-wb-card-badges">${badges}</span>` : '';
    const starting = _agentStartAgentId === agent.id;
    const disabled = !canChat || agentWorkbenchLoading || !!_agentStartInFlight;
    const actionLabel = starting ? t('agent_starting')
        : canChat ? t('start_chat') : agentUnavailableLabel(unavailableReason);
    // The card body and its button share one flow, so the whole card is a
    // keyboard-accessible button-ish element. Use a single <button> to avoid a
    // nested-button accessibility violation and to make one Tab stop per card.
    const actionIcon = starting ? 'fa-spinner fa-spin' : canChat ? 'fa-comment' : 'fa-circle-exclamation';
    // Digital-employee projection: position / category on one meta line, tags as
    // small chips under the description. Rendered only when present.
    const metaBits = [];
    if (agent.position) metaBits.push(escapeHtml(agent.position));
    else if (agent.category) metaBits.push(escapeHtml(agent.category));
    const metaLine = metaBits.length
        ? `<div class="agent-wb-card-meta truncate">${metaBits.join(' · ')}</div>`
        : '';
    const tagChips = (agent.tags || []).length
        ? `<div class="agent-wb-card-tags">${(agent.tags || []).map(tag => `<span class="agent-wb-tag">${escapeHtml(tag)}</span>`).join('')}</div>`
        : '';
    return `<button type="button" class="agent-wb-card${canChat ? '' : ' agent-wb-card-disabled'}"
            ${disabled ? 'disabled aria-disabled="true"' : `onclick="startChatWithAgent('${escapeHtml(agent.id)}')"`}
            aria-busy="${starting || agentWorkbenchLoading}"
            data-agent-id="${escapeHtml(agent.id)}">
        <div class="agent-wb-card-top">
            ${agentAvatarHTML(agent, 44)}
            <div class="min-w-0 flex-1">
                <div class="agent-wb-card-name truncate" title="${escapeHtml(agent.name)}">${escapeHtml(agent.name)}</div>
                <div class="agent-wb-card-id truncate font-mono">${escapeHtml(agent.id)}</div>
            </div>
            ${badge}
        </div>
        <div class="agent-wb-card-desc">${desc ? escapeHtml(desc) : `<span class="agent-card-desc-empty">${escapeHtml(t('agents_no_desc'))}</span>`}</div>
        ${metaLine}
        ${tagChips}
        <div class="agent-wb-card-foot">
            <span class="agent-wb-action${canChat ? '' : ' agent-wb-action-disabled'}">
                <i class="fas ${actionIcon} mr-1.5"></i>${escapeHtml(actionLabel)}
            </span>
        </div>
    </button>`;
}

function renderAgentWorkbench() {
    _wbSyncFilterIdentity();
    const grid = document.getElementById('agent-workbench-grid');
    const status = document.getElementById('agent-workbench-status');
    if (!grid) return;
    const filtered = agentWorkbenchFilterResult(agentWorkbench, _wbSearchQuery, _wbSelectedTag);
    renderAgentWorkbenchFilters(filtered);
    // State: loading / empty / error / cards. The status line carries the
    // non-card states (loading, empty, error), the grid carries the cards.
    if (agentWorkbenchLoading && !agentWorkbench.length) {
        setWbStatus(t('agent_workbench_loading'));
        grid.innerHTML = '';
        return;
    }
    if (_wbLoadedError) {
        setWbError(t(_wbErrorKey || 'agent_workbench_failed'));
        grid.innerHTML = `<div class="col-span-full text-sm text-slate-400 py-16 text-center">
            <p>${escapeHtml(t(_wbErrorKey || 'agent_workbench_failed'))}</p>
            <button type="button" class="agent-wb-retry"
                onclick="loadAgentWorkbench(true)">${escapeHtml(t('agent_workbench_retry'))}</button>
        </div>`;
        return;
    }
    if (!agentWorkbench.length) {
        // Success-but-empty: the reason code picks the wording, and the status
        // line stays the success one. A notice about an unavailable target still
        // wins (it is a response to a user action, not a read outcome).
        if (_wbNoticeKey) setWbError(t(_wbNoticeKey));
        else setWbStatus(t(_wbEmptyKey()));
        grid.innerHTML = '';
        return;
    }
    if (_wbNoticeKey) setWbError(t(_wbNoticeKey));
    else setWbStatus(agentWorkbenchLoading ? t('agent_workbench_loading') : '');
    if (!filtered.agents.length) {
        grid.innerHTML = `<div class="agent-wb-no-match">
            <i class="fas fa-magnifying-glass" aria-hidden="true"></i>
            <p>${escapeHtml(t('agent_workbench_no_match'))}</p>
            <button type="button" class="agent-wb-filter-link" onclick="clearAgentWorkbenchSearch(true)">
                ${escapeHtml(t('agent_workbench_clear_filters'))}</button></div>`;
        return;
    }
    grid.innerHTML = filtered.agents.map(a =>
        agentWorkbenchCardHTML(a, a.can_chat, a.unavailable_reason)
    ).join('');
}

function setWbStatus(text) {
    const status = document.getElementById('agent-workbench-status');
    if (!status) return;
    status.textContent = text || '';
    status.classList.remove('opacity-0');
    status.classList.toggle('agent-workbench-status-hidden', !text);
    status.classList.remove('agent-workbench-status-error');
}

function setWbError(text) {
    const status = document.getElementById('agent-workbench-status');
    if (!status) return;
    status.textContent = text || '';
    status.classList.remove('opacity-0');
    status.classList.remove('agent-workbench-status-hidden');
    status.classList.add('agent-workbench-status-error');
}

let _wbLoadedError = false;
// Which failure the last read ended on, as an i18n key rather than a boolean,
// so an actionable cause is reported as itself instead of as「加载失败」.
let _wbErrorKey = '';

// Paint the current state without letting a rendering fault masquerade as a
// failed read. Rendering used to run inside the load chain's `then`, so any UI
// exception was caught by the load handler and displayed as「加载失败」: a real
// defect reported as a different one is worse than no report at all.
function paintAgentWorkbench() {
    try {
        renderAgentWorkbench();
    } catch (err) {
        try { console.error('[agent-workbench] render failed:', err); } catch (_) {}
    }
}

function loadAgentWorkbench(manualRefresh = false) {
    const grid = document.getElementById('agent-workbench-grid');
    if (!grid) return Promise.resolve();
    _wbSyncFilterIdentity();
    // Suppress a spurious "loading" flash when returning to a filled list.
    agentWorkbenchLoading = true;
    _wbLoadedError = false;
    _wbErrorKey = '';
    _wbNoticeKey = '';
    _wbEmptyReason = '';
    // Even the first paint goes through the isolating wrapper: a fault raised
    // while drawing the loading state must not escape as a synchronous throw,
    // which would break the promise contract callers rely on.
    paintAgentWorkbench();
    // A request-seq + context guard so a late response from an earlier read
    // (or one started under a different Agent / view) is dropped instead of
    // repainting stale cards over a fresher result.
    const seq = ++agentWorkbenchSeq;
    const ctx = _wbContext();
    const current = () => seq === agentWorkbenchSeq && ctx === _wbContext();
    // One silent second attempt for a fault that another attempt could
    // plausibly differ on — a dropped transport, a restarting server, a 5xx.
    // A refusal or a wrong response shape is deterministic and is not retried:
    // a second identical request would only delay the report.
    const attempt = retriesLeft => fetchAgentWorkbench().catch(err => {
        if (retriesLeft > 0 && err && err.wbTransient && current()) return attempt(retriesLeft - 1);
        throw err;
    });
    return attempt(1)
        .then(agents => {
            if (!current()) return null;
            applyAgentWorkbench(agents);
            agentWorkbenchLoading = false;
            _wbLoadedError = false;
            _wbErrorKey = '';
            paintAgentWorkbench();
            return agents;
        })
        .catch(err => {
            if (!current()) return null;
            agentWorkbenchLoading = false;
            _wbLoadedError = true;
            _wbErrorKey = (err && err.wbKey) || 'agent_workbench_failed';
            // Keep the raw evidence in the console. The visible message is
            // deliberately short, so without this a report of「加载失败」cannot
            // be traced to a status, a code or a transport error afterwards.
            try {
                console.error('[agent-workbench] list read failed:', (err && err.wbDetail) || err);
            } catch (_) {}
            paintAgentWorkbench();
        });
}

function openAgentDetail(agentId) {
    selectedAdminAgentId = agentId;
    document.getElementById('agent-detail')?.classList.remove('hidden');
    renderAgentsGrid();
    renderAgentDetail();
    // Reset the core-file picker to a clean state per Agent, rather than
    // carrying over whichever file/view mode was left selected for the
    // previous one.
    const fileDd = document.getElementById('agent-core-file');
    if (fileDd) fileDd._ddValue = 'AGENT.md';
    setAgentCoreViewMode('edit');
    loadAgentCoreFile();
    // The model picker is drawn from the same catalog the composer uses, which
    // depends on which providers have keys. Re-render once it has arrived.
    if (!_sessCfg) refreshSessionSettings().then(() => {
        if (selectedAdminAgentId === agentId) renderAgentDetail();
    });
}

function closeAgentDetail() {
    selectedAdminAgentId = '';
    const detail = document.getElementById('agent-detail');
    if (detail) {
        detail.classList.add('hidden');
        // The empty pane's placeholder text (desktop two-pane layout).
        detail.setAttribute('data-empty-label', t('agents_select_hint'));
    }
    renderAgentsGrid();
}

function selectAgentDetailTab(tab) {
    document.querySelectorAll('.agent-detail-tab').forEach(el => {
        el.classList.toggle('active', el.dataset.tab === tab);
    });
    ['profile', 'skills', 'tasks', 'files'].forEach(name => {
        document.getElementById(`agent-detail-${name}`)?.classList.toggle('hidden', name !== tab);
    });
    if (tab === 'skills') renderAgentCapabilitiesPane();
    if (tab === 'tasks') renderAgentTasksPane();
    if (tab === 'files') loadAgentCoreFile();
}

// A field label followed by a small info icon whose help shows on hover, so a
// form stays compact instead of carrying a paragraph of hint under every field.
// The tip text may contain \n to force a line break (e.g. one line per option
// of a shared/own choice) — rendered via the popup's `white-space: pre-line`.
function fieldLabelWithTip(label, tip) {
    return `<div class="agent-field-label-row">
        <label class="agent-field-label">${escapeHtml(label)}</label>
        <span class="agent-field-tip" data-tip="${escapeHtml(tip)}"><i class="fas fa-circle-info"></i></span>
    </div>`;
}

// Single popup instance fixed to <body>, positioned relative to whichever
// .agent-field-tip is hovered. Living outside every drawer/modal means it is
// never clipped by an ancestor's `overflow: auto` (unlike a CSS ::after would
// be inside the scrolling Agent detail pane).
let _fieldTipEl = null;
let _fieldTipIcon = null;  // which icon the popup currently belongs to
function _ensureFieldTipEl() {
    if (!_fieldTipEl) {
        _fieldTipEl = document.createElement('div');
        _fieldTipEl.className = 'agent-tip-popup';
        document.body.appendChild(_fieldTipEl);
    }
    return _fieldTipEl;
}

function _showFieldTip(iconEl) {
    const tip = iconEl.dataset.tip;
    if (!tip) return;
    // Already showing for this icon: don't re-measure/re-animate. Moving the
    // cursor from the <span> onto its own <i> would otherwise re-trigger the
    // whole show sequence and make the tip visibly flicker.
    if (_fieldTipIcon === iconEl && _fieldTipEl && _fieldTipEl.classList.contains('show')) return;
    _fieldTipIcon = iconEl;
    const popup = _ensureFieldTipEl();
    popup.textContent = tip;
    popup.classList.remove('show');
    popup.style.left = '0px';
    popup.style.top = '0px';
    // Measure after layout so width/height reflect the actual (possibly
    // multi-line) content before we clamp it into the viewport.
    requestAnimationFrame(() => {
        const rect = iconEl.getBoundingClientRect();
        const pw = popup.offsetWidth, ph = popup.offsetHeight;
        let left = rect.left + rect.width / 2 - pw / 2;
        const margin = 8;
        left = Math.max(margin, Math.min(left, window.innerWidth - pw - margin));
        let top = rect.top - ph - 8;
        let arrowTop = false;
        if (top < margin) { top = rect.bottom + 8; arrowTop = true; } // flip below if clipped above
        popup.style.left = `${left}px`;
        popup.style.top = `${top}px`;
        popup.style.setProperty('--tip-arrow-x', `${rect.left + rect.width / 2 - left}px`);
        popup.classList.toggle('tip-arrow-top', arrowTop);
        popup.classList.add('show');
    });
}

function _hideFieldTip() {
    if (_fieldTipEl) _fieldTipEl.classList.remove('show');
    _fieldTipIcon = null;
}

document.addEventListener('mouseover', (e) => {
    const icon = e.target.closest ? e.target.closest('.agent-field-tip') : null;
    if (icon) _showFieldTip(icon);
});
document.addEventListener('mouseout', (e) => {
    const icon = e.target.closest ? e.target.closest('.agent-field-tip') : null;
    if (!icon) return;
    // mouseout fires when moving between the icon's own children (span -> <i>).
    // Only hide when the cursor actually leaves this icon's subtree, i.e. the
    // element it moved to isn't inside the same .agent-field-tip.
    const to = e.relatedTarget;
    if (to && icon.contains(to)) return;
    _hideFieldTip();
});
document.addEventListener('scroll', _hideFieldTip, true);

function renderAgentDetail() {
    const agent = findAgent(selectedAdminAgentId);
    const identity = document.getElementById('agent-detail-identity');
    const profile = document.getElementById('agent-detail-profile');
    if (!agent || !identity || !profile) return;
    const coding = agent.agent_type === 'coding';
    // The service line is read through the guarded seam accessor: this region of
    // the file runs on its own in the Agent tests, where the seam is absent.
    const seam = (typeof codingSeam === 'function') ? codingSeam() : null;
    const serviceLabel = (seam && seam.serviceLabel()) || t('agents_coding_service_unset');
    identity.innerHTML = `
        ${agentAvatarHTML(agent, 56)}
        <div class="min-w-0">
            <div class="flex items-center gap-2">
                <span class="text-lg font-semibold text-slate-800 dark:text-slate-100 truncate">${escapeHtml(agent.name)}</span>
                ${codingAgentTypeHint(coding ? '' : 'hidden')}
            </div>
            <div class="text-xs text-slate-400 font-mono truncate">${escapeHtml(agent.id)}</div>
        </div>`;
    const isDefault = agent.id === defaultAgentId;
    // *My* registered default, which is a different fact from `isDefault` above:
    // that one is the resolved anchor, and a fallback (the tenant's choice, or
    // the deterministic first shared Agent) is not a preference the user made.
    // Offering "设为我的默认" only against the registered pointer is what keeps
    // the button from claiming a choice nobody made.
    const isMyDefault = !!userDefault.agent_id && userDefault.agent_id === agent.id;
    // The Agent a *new session* would land on right now, which is what makes the
    // hint below worth showing. `defaultAgentId` is the same fact for the
    // catalogue ordering, but it is written with a defensive fallback chain, so
    // the resolution's own answer is the one to trust here.
    const isAnchor = !!defaultResolution.agent_id && defaultResolution.agent_id === agent.id;
    profile.innerHTML = `
        <div class="agent-field">
            <label class="agent-field-label">${escapeHtml(t('agents_avatar'))}</label>
            <div id="agent-edit-avatar" class="agent-avatar-picker"></div>
        </div>
        <div class="agent-field">
            <label class="agent-field-label">${escapeHtml(t('agents_name'))}</label>
            <input id="agent-edit-name" value="${escapeHtml(agent.name)}" class="agent-input">
        </div>
        <div class="agent-field">
            ${fieldLabelWithTip(t('agents_type'), t('agents_type_locked'))}
            <div id="agent-type-value" class="agent-field-hint">${escapeHtml(coding ? t('agents_type_coding') : t('agents_type_normal'))}</div>
        </div>
        ${coding ? `
        <div class="agent-field">
            ${fieldLabelWithTip(t('agents_coding_project'), t('agents_coding_project_hint'))}
            <!-- Read-only: the project is part of the Agent's definition and the
                 type cannot be changed after creation. -->
            <div id="agent-coding-project" class="agent-input font-mono bg-slate-50 dark:bg-white/5">${escapeHtml(agent.coding_project_dir || '')}</div>
        </div>
        <div class="agent-field">
            ${fieldLabelWithTip(t('agents_coding_service'), t('agents_coding_service_hint'))}
            <div id="agent-coding-service" class="agent-field-hint">${escapeHtml(serviceLabel)}</div>
        </div>` : ''}
        <div class="agent-field">
            ${fieldLabelWithTip(t('agents_description'), t('agents_description_hint'))}
            <textarea id="agent-edit-description" rows="4"
                   placeholder="${escapeHtml(t('agents_description_placeholder'))}"
                   class="agent-input agent-textarea">${escapeHtml(agent.description || '')}</textarea>
        </div>
        <div class="agent-field">
            <label class="agent-field-label">${escapeHtml(t('agents_position'))}</label>
            <input id="agent-edit-position" value="${escapeHtml(agent.position || '')}" class="agent-input"
                   placeholder="${escapeHtml(t('agents_position_placeholder'))}">
        </div>
        <div class="agent-field">
            <label class="agent-field-label">${escapeHtml(t('agents_category'))}</label>
            <div id="agent-edit-category" class="cfg-dropdown" tabindex="0">
                <div class="cfg-dropdown-selected">
                    <span class="cfg-dropdown-text">${escapeHtml(agent.category || t('agents_category_none'))}</span>
                    <i class="fas fa-chevron-down cfg-dropdown-arrow"></i>
                </div>
                <div class="cfg-dropdown-menu"></div>
            </div>
        </div>
        <div class="agent-field">
            <label class="agent-field-label">${escapeHtml(t('agents_tags'))}</label>
            <input id="agent-edit-tags" value="${escapeHtml((agent.tags || []).join(', '))}" class="agent-input"
                   placeholder="${escapeHtml(t('agents_tags_placeholder'))}">
        </div>
        <div class="agent-field">
            <label class="agent-field-label">${escapeHtml(t('agents_greeting'))}</label>
            <input id="agent-edit-greeting" value="${escapeHtml(agent.greeting || '')}" class="agent-input">
        </div>
        <div class="agent-field">
            ${fieldLabelWithTip(t('agents_persona'), t('agents_persona_hint'))}
            <textarea id="agent-edit-persona" rows="3"
                   class="agent-input agent-textarea">${escapeHtml(agent.persona_summary || '')}</textarea>
        </div>
        <div class="agent-field">
            <label class="agent-field-label">${escapeHtml(t('agents_scene'))}</label>
            <div id="agent-edit-scene" class="cfg-dropdown" tabindex="0">
                <div class="cfg-dropdown-selected">
                    <span class="cfg-dropdown-text">${escapeHtml(agent.scene_id || t('agents_scene_none'))}</span>
                    <i class="fas fa-chevron-down cfg-dropdown-arrow"></i>
                </div>
                <div class="cfg-dropdown-menu"></div>
            </div>
        </div>
        <div class="agent-field">
            <label class="agent-field-label">${escapeHtml(t('agents_model'))}</label>
            ${isDefault
                ? `<div class="agent-input-locked">${escapeHtml(t('agents_model_follows_global'))}</div>
                   <p class="agent-field-hint">${escapeHtml(t('agents_model_default_hint'))}</p>`
                : `<div id="agent-edit-model" class="cfg-dropdown" tabindex="0">
                       <div class="cfg-dropdown-selected">
                           <span class="cfg-dropdown-text">--</span>
                           <i class="fas fa-chevron-down cfg-dropdown-arrow"></i>
                       </div>
                       <div class="cfg-dropdown-menu"></div>
                   </div>`}
        </div>
        ${isDefault ? '' : `
        <div class="agent-field">
            ${fieldLabelWithTip(t('agents_knowledge'), t('agents_knowledge_hint'))}
            <div class="flex items-center gap-3">
                <div id="agent-knowledge-toggle" class="agent-seg" role="group">
                    <button type="button" class="agent-seg-btn ${agent.knowledge_mode !== 'own' ? 'active' : ''}" data-mode="shared" onclick="setAgentKnowledgeMode('${escapeHtml(agent.id)}','shared')">
                        <i class="fas fa-users mr-1"></i>${escapeHtml(t('agents_knowledge_shared'))}
                    </button>
                    <button type="button" class="agent-seg-btn ${agent.knowledge_mode === 'own' ? 'active' : ''}" data-mode="own" onclick="setAgentKnowledgeMode('${escapeHtml(agent.id)}','own')">
                        <i class="fas fa-box-archive mr-1"></i>${escapeHtml(t('agents_knowledge_own'))}
                    </button>
                </div>
                <span id="agent-knowledge-status" class="agent-field-hint" style="margin-top:0"></span>
            </div>
        </div>`}
        ${isAnchor ? `<p id="agent-anchor-hint" class="agent-field-hint agent-anchor-hint">${escapeHtml(agentAnchorHintText())}</p>` : ''}
        <div class="agent-detail-actions">
            <button type="button" onclick="saveAgentProfile()" class="agent-btn agent-btn-primary">${escapeHtml(t('save'))}</button>
            <button type="button" onclick="startChatWithAgent('${escapeHtml(agent.id)}')" class="agent-btn agent-btn-ghost">${escapeHtml(t('agents_chat'))}</button>
            ${isMyDefault || agent.enabled === false ? '' : `<button type="button" onclick="setAgentAsMyDefault('${escapeHtml(agent.id)}')" class="agent-btn agent-btn-ghost">${escapeHtml(t('agents_set_my_default'))}</button>`}
            ${tenantDefaultManageable && !isDefault ? `<button type="button" onclick="setAgentAsDefault('${escapeHtml(agent.id)}')" class="agent-btn agent-btn-ghost">${escapeHtml(t('agents_set_tenant_default'))}</button>` : ''}
            ${isDefault ? '' : `<button type="button" onclick="deleteAgent('${escapeHtml(agent.id)}')" class="agent-btn agent-btn-danger agent-detail-delete">${escapeHtml(t('agents_delete'))}</button>`}
        </div>
        <div id="agent-profile-status" class="agent-field-hint mt-3"></div>`;

    renderAvatarPicker('agent-edit-avatar', agent, (file) => uploadAgentAvatar(agent.id, file));

    // The service line is read through the same one-shot projection the create
    // form uses; only a coding Agent has one to show.
    if (coding && seam) seam.paintService('agent-coding-service');

    if (!isDefault) {
        const dd = document.getElementById('agent-edit-model');
        const opts = agentModelDropdownOptions(agent);
        const current = agent.model ? `${agent.bot_type || ''}|${agent.model}` : '';
        initDropdown(dd, opts, current, () => {}, { placeholder: t('agents_model_follows_global') });
    }
    // A save may re-render this pane several times; re-apply an in-flight
    // "saved" confirmation so it survives instead of being wiped.
    paintAgentSavedFlash();
    // Populate the scene + category dropdowns from the scene catalog, then
    // re-init so the current selection is preserved against the wide option set.
    refreshAgentCategoryDropdown();
    if (!isDefault) refreshAgentSceneDropdown();
}

/* A live preview beside an upload button, in the page's own styling rather than
   a raw file input. The default is the Agent's initial; uploading swaps it for
   the chosen image. `onUpload` may be null when the Agent does not exist yet
   (the create modal), leaving just the preview. */
function renderAvatarPicker(containerId, agent, onUpload) {
    const box = document.getElementById(containerId);
    if (!box) return;
    box.innerHTML = `
        <div class="agent-avatar-picker-preview">${agentAvatarHTML(agent, 56)}</div>
        <div class="agent-avatar-picker-body">
            ${onUpload ? `<button type="button" class="agent-avatar-upload">
                <i class="fas fa-arrow-up-from-bracket"></i><span>${escapeHtml(t('agents_avatar_upload'))}</span>
                <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" hidden>
            </button>` : ''}
        </div>`;
    const upload = box.querySelector('.agent-avatar-upload');
    if (upload && onUpload) {
        const input = upload.querySelector('input');
        upload.addEventListener('click', () => input.click());
        input.addEventListener('change', () => onUpload(input.files && input.files[0]));
    }
}

/* Flattened for the styled dropdown: one row per model, its provider carried in
   the value (a model asked of the wrong vendor is an error), its brand shown as
   a dim hint. The first row clears the choice back to the configured model. */
function agentModelDropdownOptions(agent) {
    const opts = [{ value: '', label: t('agents_model_follows_global') }];
    const providers = (_sessCfg && _sessCfg.model && _sessCfg.model.providers) || [];
    providers.forEach(p => {
        (p.models || []).forEach(m => {
            opts.push({ value: `${p.id}|${m}`, label: m, hint: localizedLabel(p.label) });
        });
    });
    // A pinned model whose provider is no longer in the catalog (its key was
    // removed, say) must still show as the selection. Otherwise the picker
    // falls back to the "follow global" row and the next save silently clears
    // the pin. Mirrors the desktop Agent editor.
    if (agent && agent.model) {
        const current = `${agent.bot_type || ''}|${agent.model}`;
        if (!opts.some(o => o.value === current)) {
            opts.push({ value: current, label: agent.model, hint: agent.bot_type || undefined });
        }
    }
    return opts;
}

// Scene catalog options for the Agent detail pane. Fetched lazily and cached;
// a missing scene module yields no options, so the selector simply offers "none".
let _sceneCatalogCache = null;
function sceneCatalog() {
    if (_sceneCatalogCache !== null) return Promise.resolve(_sceneCatalogCache);
    return fetch('/api/scenes').then(r => r.json()).then(d => {
        _sceneCatalogCache = d && d.scenes ? d.scenes : [];
        return _sceneCatalogCache;
    }).catch(() => { _sceneCatalogCache = []; return []; });
}
function sceneCatalogOptions() {
    return [{ value: '', label: t('agents_scene_none') }];
}
function refreshAgentSceneDropdown() {
    const dd = document.getElementById('agent-edit-scene');
    if (!dd) return;
    const agent = findAgent(selectedAdminAgentId);
    sceneCatalog().then(scenes => {
        const opts = [{ value: '', label: t('agents_scene_none') }].concat(
            scenes.map(s => ({ value: s.id, label: (s.name || s.id) }))
        );
        const current = (agent && agent.scene_id) || '';
        initDropdown(dd, opts, current, () => {}, { placeholder: t('agents_scene_none') });
    });
}
function sceneCategoryOptions() {
    // Category may be typed freely; the dropdown offers the scene categories
    // (empty allowed). The first row clears back to no category.
    return [{ value: '', label: t('agents_category_none') }];
}
function refreshAgentCategoryDropdown() {
    const dd = document.getElementById('agent-edit-category');
    if (!dd) return;
    const agent = findAgent(selectedAdminAgentId);
    sceneCatalog().then(scenes => {
        const seen = [];
        scenes.forEach(s => { const c = s.category; if (c && seen.indexOf(c) === -1) seen.push(c); });
        const opts = [{ value: '', label: t('agents_category_none') }].concat(
            seen.map(c => ({ value: c, label: c }))
        );
        const current = (agent && agent.category) || '';
        initDropdown(dd, opts, current, () => {}, { placeholder: t('agents_category_none') });
    });
}

// Persist an Agent's skill selection. Writes are serialized per Agent and
// coalesce to the latest desired state, so ticking several boxes quickly sends
// them in order (each with the revision the previous one returned) instead of
// racing and tripping the stale-roster guard. No catalog reload happens, so the
// grid, composer and avatars never flicker and the checkboxes never jump.
//   null  -> use every installed skill (the "use all" master toggle)
//   [...] -> exactly this subset ([] means none)
const _skillSaveState = {};  // agentId -> { inflight: bool, pending: skills|undefined }

function saveAgentSkills(agent, skills) {
    agent.skills = skills;  // optimistic; the pane already reflects it
    const st = _skillSaveState[agent.id] || (_skillSaveState[agent.id] = { inflight: false, pending: undefined });
    if (st.inflight) { st.pending = skills; return; }  // newest wins; drop stale intermediate
    st.inflight = true;
    fetch('/api/agents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'update', id: agent.id, revision: rosterRevision, skills }),
    }).then(r => r.json()).then(data => {
        if (data.status === 'success') {
            if (data.revision) rosterRevision = data.revision;
        } else {
            const status = document.getElementById('agent-editor-status');
            if (status) status.textContent = data.message || 'Update failed';
        }
    }).catch(() => {}).then(() => {
        st.inflight = false;
        if (st.pending !== undefined) {
            const next = st.pending;
            st.pending = undefined;
            saveAgentSkills(agent, next);  // flush the latest queued state
        }
    });
}

// Persist capabilities (skills / sops / tools allow+deny) in one update, so
// toggling related controls does not send several racing writes. The roster
// revision is carried on each call; on success we adopt the returned revision.
function saveAgentCapabilities(agent, fields) {
    // Apply optimistically so the pane reflects the new state immediately.
    if ('skills' in fields) agent.skills = fields.skills;
    if ('sops' in fields) agent.sops = fields.sops;
    if ('tools_allowlist' in fields) agent.tools_allowlist = fields.tools_allowlist;
    if ('tools_denylist' in fields) agent.tools_denylist = fields.tools_denylist;
    const body = Object.assign({ action: 'update', id: agent.id, revision: rosterRevision }, fields);
    fetch('/api/agents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    }).then(r => r.json()).then(data => {
        if (data.status === 'success') {
            if (data.revision) rosterRevision = data.revision;
        } else {
            const status = document.getElementById('agent-editor-status');
            if (status) status.textContent = data.message || 'Update failed';
        }
    }).catch(() => {});
}

// Switch an Agent between the shared knowledge base and its own. This is a
// filesystem toggle (symlink vs a real knowledge/ dir), so it applies at once
// rather than waiting for the profile "save".
async function setAgentKnowledgeMode(agentId, mode) {
    const agent = findAgent(agentId);
    if (!agent || agent.knowledge_mode === mode) return;
    const status = document.getElementById('agent-knowledge-status');
    const paintActive = (m) => document.querySelectorAll('#agent-knowledge-toggle .agent-seg-btn')
        .forEach(b => b.classList.toggle('active', b.dataset.mode === m));
    const prev = agent.knowledge_mode || 'shared';
    agent.knowledge_mode = mode;  // optimistic
    paintActive(mode);
    if (status) status.textContent = t('agents_knowledge_working') || '...';
    try {
        const res = await fetch('/api/agents', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'set_knowledge_mode', id: agentId, mode }),
        });
        const data = await res.json();
        if (data.status === 'success') {
            agent.knowledge_mode = (data.mode || mode);
            paintActive(agent.knowledge_mode);
            if (status) status.textContent = '';
        } else {
            agent.knowledge_mode = prev;  // roll back
            paintActive(prev);
            if (status) status.textContent = data.message || t('agents_knowledge_failed') || 'Failed';
        }
    } catch (e) {
        agent.knowledge_mode = prev;
        paintActive(prev);
        if (status) status.textContent = t('agents_knowledge_failed') || 'Failed';
    }
}

function renderAgentCapabilitiesPane() {
    const pane = document.getElementById('agent-detail-skills');
    const agent = findAgent(selectedAdminAgentId);
    if (!pane || !agent) return;
    // tools_denylist is optional on older agents; default to [] so we never
    // read .length on undefined. (tools_allowlist null means "no allowlist".)
    const allowlist = agent.tools_allowlist ?? null;
    const denylist = agent.tools_denylist ?? [];
    const render = () => {
        const all = agent.skills == null;
        const picked = new Set(all ? [] : agent.skills);
        const sops = agent.sops || [];
        const allowSet = new Set(allowlist || []);
        const denySet = new Set(denylist);
        pane.innerHTML = `
            <div class="agent-cap-section">
                <div class="agent-cap-title">${escapeHtml(t('agents_skills_label'))}</div>
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
                }).join('')}
            </div>
            <div class="agent-cap-section">
                <div class="agent-cap-title">${escapeHtml(t('agents_sops_label'))}</div>
                <p class="text-xs text-slate-400 mb-2">${escapeHtml(t('agents_sops_hint'))}</p>
                <div class="flex flex-wrap gap-2 mb-2" id="agent-sops-list">
                    ${sops.map(id => `<span class="agent-tag">${escapeHtml(id)}<button type="button" class="agent-tag-x" data-sop="${escapeHtml(id)}">&times;</button></span>`).join('')}
                </div>
                <div class="flex gap-2">
                    <input id="agent-sop-input" placeholder="${escapeHtml(t('agents_sops_placeholder'))}" class="agent-input" style="max-width: 240px;">
                    <button type="button" id="agent-sop-add" class="agent-btn agent-btn-ghost">${escapeHtml(t('agents_sops_add'))}</button>
                </div>
            </div>
            <div class="agent-cap-section">
                <div class="agent-cap-title">${escapeHtml(t('agents_tools_label'))}</div>
                <p class="text-xs text-slate-400 mb-2">${escapeHtml(t('agents_tools_hint'))}</p>
                <div class="agent-tool-grid" id="agent-tools-allow">
                    ${(installedTools || []).map(tool => {
                        const a = allowSet.has(tool.name);
                        const d = denySet.has(tool.name);
                        return `<div class="agent-tool-row">
                            <span class="agent-tool-chip"><input type="checkbox" class="agent-tool-allow" value="${escapeHtml(tool.name)}" ${a ? 'checked' : ''} ${d ? 'disabled' : ''}>${escapeHtml(t('agents_allow'))}</span>
                            <span class="agent-tool-chip"><input type="checkbox" class="agent-tool-deny" value="${escapeHtml(tool.name)}" ${d ? 'checked' : ''} ${a ? 'disabled' : ''}>${escapeHtml(t('agents_deny'))}</span>
                            <div class="min-w-0 flex-1">
                                <div class="text-sm text-slate-700 dark:text-slate-200 font-mono">${escapeHtml(tool.name)}</div>
                                <div class="text-xs text-slate-400 truncate">${escapeHtml((tool.description || '').split('\n')[0])}</div>
                            </div>
                        </div>`;
                    }).join('')}
                </div>
                <p class="text-xs text-slate-400 mt-2">${escapeHtml(t('agents_tools_none_hint'))}</p>
            </div>`;
        document.getElementById('agent-skills-all')?.addEventListener('change', (e) => {
            const next = e.target.checked ? null : [];
            saveAgentCapabilities(agent, { skills: next });
            render();
        });
        pane.querySelectorAll('.agent-skill-item').forEach(box => {
            box.addEventListener('change', () => {
                const names = Array.from(pane.querySelectorAll('.agent-skill-item:checked')).map(el => el.value);
                saveAgentCapabilities(agent, { skills: names });
            });
        });
        // SOP add/remove
        document.getElementById('agent-sop-add')?.addEventListener('click', () => {
            const input = document.getElementById('agent-sop-input');
            const val = (input && input.value || '').trim();
            if (!val) return;
            const next = Array.from(new Set([...sops, val]));
            saveAgentCapabilities(agent, { sops: next });
            if (input) input.value = '';
            render();
        });
        pane.querySelectorAll('.agent-tag-x[data-sop]').forEach(btn => {
            btn.addEventListener('click', () => {
                const id = btn.getAttribute('data-sop');
                const next = sops.filter(x => x !== id);
                saveAgentCapabilities(agent, { sops: next });
                render();
            });
        });
        // Tool allow/deny toggles
        pane.querySelectorAll('.agent-tool-allow').forEach(box => {
            box.addEventListener('change', () => {
                const name = box.value;
                const nextAllow = new Set(agent.tools_allowlist || []);
                if (box.checked) nextAllow.add(name);
                else if (agent.tools_allowlist != null) nextAllow.delete(name);
                saveAgentCapabilities(agent, { tools_allowlist: agent.tools_allowlist == null ? [name] : Array.from(nextAllow) });
                render();
            });
        });
        pane.querySelectorAll('.agent-tool-deny').forEach(box => {
            box.addEventListener('change', () => {
                const name = box.value;
                const nextDeny = new Set(agent.tools_denylist || []);
                if (box.checked) nextDeny.add(name); else nextDeny.delete(name);
                saveAgentCapabilities(agent, { tools_denylist: Array.from(nextDeny) });
                render();
            });
        });
    };
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
            return;
        }
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
                <p class="text-xs text-slate-500 dark:text-slate-400 mb-2 line-clamp-2">${escapeHtml(taskContent)}</p>
                <div class="flex items-center gap-4 text-xs text-slate-400 dark:text-slate-500">
                    <span><i class="fas fa-clock mr-1"></i>${escapeHtml(t('task_next_run'))}: ${nextRun}</span>
                    <div class="flex-1"></div>
                    ${caps.run ? `<button type="button" class="task-run-now px-2 py-1 rounded-md text-primary-500 hover:bg-primary-50 dark:hover:bg-primary-500/10 transition-colors">
                        <i class="fas fa-play mr-1"></i>${escapeHtml(t('task_run_now'))}
                    </button>` : ''}
                    ${caps.manage ? `<label class="relative inline-flex items-center cursor-pointer" for="${toggleId}">
                        <input type="checkbox" id="${toggleId}" class="sr-only peer" ${isEnabled ? 'checked' : ''}>
                        <div class="w-9 h-5 bg-slate-200 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-primary-500 dark:bg-slate-600 dark:peer-checked:bg-primary-500"></div>
                    </label>` : ''}
                </div>`;
            const runButton = card.querySelector('.task-run-now');
            if (runButton) runButton.addEventListener('click', (e) => {
                e.stopPropagation();
                // A manual run carries the client's "same request" key, so the
                // fork's run-now is the one installed by the Tasks page's patch
                // layer (assets/js/fork/tasks-console.js, loaded last), which
                // supersedes upstream's keyless version for every caller --
                // including this pane. Guarded so this pane cannot throw if a
                // page ever loads without that layer.
                if (typeof runTaskNow === 'function') runTaskNow(task, e.currentTarget);
            });
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
}

// Held between opening the create modal and a successful create: the chosen
// avatar has nowhere to live server-side until the Agent exists, so we keep the
// File and its preview URL client-side and upload once creation returns.
let _pendingCreateAvatar = null;
let _createKnowledgeMode = 'shared';

// What the Agent being filled in would look like: no id yet, so the disc is the
// neutral tone and only the initial follows the name.
function createAvatarDraft() {
    const name = document.getElementById('agent-create-name');
    return { id: '', name: (name && name.value) || '', avatar: '' };
}

// Repaint just the preview disc as the name is typed. The whole picker is not
// re-rendered because that would rebind the upload input on every keystroke.
function refreshCreateAvatarPreview() {
    if (_pendingCreateAvatar) return;
    const slot = document.querySelector('#agent-create-avatar .agent-avatar-picker-preview');
    if (slot) slot.innerHTML = agentAvatarHTML(createAvatarDraft(), 56);
}

// The create modal's avatar picker: same look as the edit one, but the upload
// is staged locally (preview from an object URL) instead of POSTed immediately.
function renderCreateAvatarPicker() {
    const box = document.getElementById('agent-create-avatar');
    if (!box) return;
    const preview = _pendingCreateAvatar
        ? `<img class="agent-avatar agent-avatar-56" src="${_pendingCreateAvatar.url}" alt="">`
        : agentAvatarHTML(createAvatarDraft(), 56);
    box.innerHTML = `
        <div class="agent-avatar-picker-preview">${preview}</div>
        <div class="agent-avatar-picker-body">
            <button type="button" class="agent-avatar-upload">
                <i class="fas fa-arrow-up-from-bracket"></i><span>${escapeHtml(t('agents_avatar_upload'))}</span>
                <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" hidden>
            </button>
        </div>`;
    const upload = box.querySelector('.agent-avatar-upload');
    const input = upload.querySelector('input');
    upload.addEventListener('click', () => input.click());
    input.addEventListener('change', () => {
        const file = input.files && input.files[0];
        if (!file) return;
        if (_pendingCreateAvatar && _pendingCreateAvatar.url) URL.revokeObjectURL(_pendingCreateAvatar.url);
        _pendingCreateAvatar = { file, url: URL.createObjectURL(file) };
        renderCreateAvatarPicker();
    });
}

function openAgentCreateForm() {
    const form = document.getElementById('agent-create-form');
    if (!form) return;
    form.classList.remove('hidden');
    const name = document.getElementById('agent-create-name');
    // The id is typed by hand or left blank on purpose; nothing writes to it
    // while the form is open. A blank one is filled in once, at submit.
    const id = document.getElementById('agent-create-id');
    const description = document.getElementById('agent-create-description');
    [name, id, description].forEach(el => { if (el) el.value = ''; });
    document.getElementById('agent-create-status').textContent = '';

    // The Agent has no home to store an avatar in yet, so the upload is held in
    // memory and previewed locally; it is POSTed the moment creation succeeds.
    _pendingCreateAvatar = null;
    renderCreateAvatarPicker();
    if (name && !name.dataset.avatarBound) {
        name.dataset.avatarBound = '1';
        // Without an upload the face is the name's first character, so the
        // preview has to follow what is being typed.
        name.addEventListener('input', refreshCreateAvatarPreview);
    }

    // Knowledge defaults to shared; reset the segmented control on every open.
    _createKnowledgeMode = 'shared';
    document.querySelectorAll('#agent-create-knowledge .agent-seg-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.mode === 'shared');
        if (!b.dataset.bound) {
            b.dataset.bound = '1';
            b.addEventListener('click', () => {
                _createKnowledgeMode = b.dataset.mode;
                document.querySelectorAll('#agent-create-knowledge .agent-seg-btn')
                    .forEach(x => x.classList.toggle('active', x === b));
            });
        }
    });

    const clone = document.getElementById('agent-create-clone');
    if (clone) {
        // Options carry the agent so both the row and the trigger show its
        // avatar + name; "blank" (no clone) has no face.
        const opts = [{ value: '', label: t('agents_clone_none') }].concat(
            enabledAgents().map(a => ({
                value: a.id,
                label: a.name || a.id,
                agent: a,
            }))
        );
        initDropdown(clone, opts, '', () => {});
    }

    // Type + project (change add-opencode-coding-agents, 4.3). A new Agent
    // starts normal, which is the state the server assumes for an absent type;
    // the project field only appears for the type that has one.
    if (typeof _createAgentType !== 'undefined') _createAgentType = 'normal';
    const project = document.getElementById('agent-create-project');
    if (project) project.value = '';
    if (typeof bindCreateAgentType === 'function') bindCreateAgentType();
    if (typeof renderCreateCodingFields === 'function') renderCreateCodingFields();
}

//: The type the open create form will submit. Reset on every open.
let _createAgentType = 'normal';

function bindCreateAgentType() {
    document.querySelectorAll('#agent-create-type .agent-seg-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.type === _createAgentType);
        if (btn.dataset.bound) return;
        btn.dataset.bound = '1';
        btn.addEventListener('click', () => {
            _createAgentType = btn.dataset.type === 'coding' ? 'coding' : 'normal';
            document.querySelectorAll('#agent-create-type .agent-seg-btn')
                .forEach(x => x.classList.toggle('active', x === btn));
            renderCreateCodingFields();
        });
    });
}

/* Show the project and service rows only for a coding Agent, and fill in the
   service the platform is configured with. The service is read-only: it is
   deployment configuration, so the form displays it and never submits it. */
function renderCreateCodingFields() {
    const coding = _createAgentType === 'coding';
    document.getElementById('agent-create-project-field')?.classList.toggle('hidden', !coding);
    document.getElementById('agent-create-service-field')?.classList.toggle('hidden', !coding);
    if (!coding) return;
    const seam = (typeof codingSeam === 'function') ? codingSeam() : null;
    if (seam) seam.paintService('agent-create-service');
}

/* Paint a read-only service line, reading the projection at most once per page.
   The first paint states what is known (usually nothing yet) and the read
   repaints when it settles, so the row never keeps a stale "unset" after the
   answer has arrived. The projection itself lives in the coding seam's section,
   which is why it is reached through the guarded accessor. */
function paintCodingService(elementId) {
    const seam = (typeof codingSeam === 'function') ? codingSeam() : null;
    if (!seam) return;
    const paint = () => {
        const el = document.getElementById(elementId);
        if (el) el.textContent = seam.serviceLabel() || t('agents_coding_service_unset');
    };
    paint();
    const pending = seam.settings();
    if (pending && typeof pending.then === 'function') pending.then(paint);
}

function closeAgentCreateForm() {
    document.getElementById('agent-create-form')?.classList.add('hidden');
    if (_pendingCreateAvatar && _pendingCreateAvatar.url) URL.revokeObjectURL(_pendingCreateAvatar.url);
    _pendingCreateAvatar = null;
}

document.addEventListener('click', (e) => {
    const menu = document.getElementById('composer-agent-menu');
    const btn = document.getElementById('composer-agent-btn');
    if (menu && !menu.classList.contains('hidden') && !menu.contains(e.target) && btn && !btn.contains(e.target)) {
        menu.classList.add('hidden');
    }
    const modal = document.getElementById('agent-create-form');
    if (modal && !modal.classList.contains('hidden') && e.target === modal) {
        closeAgentCreateForm();
    }
    const newMenu = document.getElementById('new-chat-menu');
    if (newMenu && !newMenu.classList.contains('hidden') && !newMenu.contains(e.target)) {
        newMenu.classList.add('hidden');
    }
    // The sidebar launch picker is the same control in another place, so a click
    // outside either one shuts them both.
    const sidebarNewMenu = document.getElementById('sidebar-new-chat-menu');
    if (sidebarNewMenu && !sidebarNewMenu.classList.contains('hidden')
            && !sidebarNewMenu.contains(e.target)) {
        sidebarNewMenu.classList.add('hidden');
    }
    const teamModal = document.getElementById('team-chat-modal');
    if (teamModal && !teamModal.classList.contains('hidden') && e.target === teamModal) {
        closeTeamChatModal();
    }
});

function createAgentWorkspace() {
    const name = document.getElementById('agent-create-name').value.trim();
    const status = document.getElementById('agent-create-status');
    if (!name) {
        status.textContent = t('agents_name_required');
        return;
    }
    // A hand-typed id is used as given; blank falls back to the name's slug,
    // and then to a random one when the name has no ascii to slug (e.g. it is
    // written in Chinese). Generated here rather than while typing so the field
    // stays exactly as the user left it.
    const typed = document.getElementById('agent-create-id').value.trim();
    const id = typed || slugAgentId(name) || randomAgentId();
    // Mirrors the server's rule, so a bad id is caught before the round trip.
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(id)) {
        status.textContent = t('agents_id_invalid');
        return;
    }
    // The form's type, read defensively: this region can run without the
    // declaration above it in the console's own section tests, where the type
    // is then simply the normal one.
    const createType = (typeof _createAgentType === 'undefined') ? 'normal' : _createAgentType;
    // The same rule the server enforces for a coding Agent: the session has to
    // have a directory to open, and the server would refuse the create anyway.
    const projectDir = document.getElementById('agent-create-project')?.value.trim() || '';
    if (createType === 'coding' && !projectDir) {
        status.textContent = t('agents_coding_project_required');
        return;
    }
    fetch('/api/agents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            action: 'create',
            id,
            name,
            description: document.getElementById('agent-create-description')?.value.trim() || '',
            clone_from: getDropdownValue(document.getElementById('agent-create-clone')) || null,
            knowledge_mode: _createKnowledgeMode,
            // Sent explicitly for every Agent so the type is never inferred from
            // an absent field; the project only when there is one.
            agent_type: createType,
            coding_project_dir: createType === 'coding' ? projectDir : null,
            revision: rosterRevision,
        }),
    }).then(r => r.json()).then(data => {
        if (data.status !== 'success') {
            throw new Error(data.code === 'stale_roster' ? t('agents_stale') : (data.message || 'Create failed'));
        }
        if (data.revision) rosterRevision = data.revision;
        // Now that the workspace exists, push the staged avatar (if any) before
        // reloading, so the roster arrives already carrying the new image.
        const avatarStep = _pendingCreateAvatar
            ? uploadAgentAvatar(id, _pendingCreateAvatar.file).catch(() => {})
            : Promise.resolve();
        closeAgentCreateForm();
        return avatarStep.then(() => loadAgentCatalog()).then(() => openAgentDetail(id));
    }).catch(err => { status.textContent = err.message; });
}

function saveAgentProfile() {
    const agent = findAgent(selectedAdminAgentId);
    if (!agent) return;
    const catEl = document.getElementById('agent-edit-category');
    const sceneEl = document.getElementById('agent-edit-scene');
    const payload = {
        name: document.getElementById('agent-edit-name')?.value.trim(),
        description: document.getElementById('agent-edit-description')?.value.trim() || '',
        position: document.getElementById('agent-edit-position')?.value.trim() || '',
        category: catEl ? (getDropdownValue(catEl) || '') : agent.category || '',
        tags: [...new Set((document.getElementById('agent-edit-tags')?.value || '').split(/[,，]/).map(s => s.trim()).filter(Boolean))],
        greeting: document.getElementById('agent-edit-greeting')?.value.trim() || '',
        persona_summary: document.getElementById('agent-edit-persona')?.value.trim() || '',
        scene_id: sceneEl ? (getDropdownValue(sceneEl) || '') : agent.scene_id || '',
    };
    // Absent for the default Agent, which follows the configured model.
    const picker = document.getElementById('agent-edit-model');
    if (picker) {
        const [provider, model] = (getDropdownValue(picker) || '').split('|');
        payload.model = model || '';
        payload.bot_type = provider || '';
    }
    // The write itself is quick; the follow-up catalog reload is what's slow
    // (the default Agent carries a large skill list). Confirm optimistically so
    // the feedback is instant, and only override it if the save actually fails.
    flashAgentProfileStatus();
    updateAgentWorkspace(agent.id, payload).then(ok => {
        if (!ok) {
            _agentSavedFlashUntil = 0;
            const status = document.getElementById('agent-profile-status');
            if (status) {
                status.textContent = t('agents_save_failed');
                status.classList.remove('agent-status-ok');
            }
        }
    });
}

/* A brief inline confirmation on the detail pane's status line. A save reloads
   the catalog and can re-render this pane more than once (the model catalog
   arrives async), so the confirmation is kept as a deadline that every render
   re-applies, rather than a one-shot write a later render would wipe. */
let _agentSavedFlashUntil = 0;

function paintAgentSavedFlash() {
    const status = document.getElementById('agent-profile-status');
    if (!status) return;
    if (Date.now() < _agentSavedFlashUntil) {
        status.textContent = t('agents_saved');
        status.classList.add('agent-status-ok');
    }
}

function flashAgentProfileStatus() {
    _agentSavedFlashUntil = Date.now() + 2200;
    paintAgentSavedFlash();
    clearTimeout(flashAgentProfileStatus._t);
    flashAgentProfileStatus._t = setTimeout(() => {
        _agentSavedFlashUntil = 0;
        const status = document.getElementById('agent-profile-status');
        if (!status) return;
        status.textContent = '';
        status.classList.remove('agent-status-ok');
    }, 2200);
}

function uploadAgentAvatar(agentId, file) {
    if (!file) return;
    const picker = document.getElementById('agent-edit-avatar');
    if (picker) picker.classList.add('is-uploading');
    const status = document.getElementById('agent-profile-status');
    if (status) { status.classList.remove('agent-status-ok'); status.textContent = ''; }
    const form = new FormData();
    form.append('avatar', file);
    return fetch(`/api/agents/${encodeURIComponent(agentId)}/avatar`, { method: 'POST', body: form })
        .then(r => r.json())
        .then(data => {
            if (data.status !== 'success') throw new Error(data.message || 'Upload failed');
            // The image already persisted server-side. Patch the local catalog in
            // place and repaint just the affected surfaces, rather than reloading
            // the whole roster (slow when the default Agent carries many skills).
            avatarVersions[agentId] = String(Date.now());
            if (data.revision) rosterRevision = data.revision;
            const agent = findAgent(agentId);
            if (agent) agent.avatar = 'image';
            renderAgentsGrid();
            if (selectedAdminAgentId === agentId) renderAgentDetail();
            renderComposerIdentity();
            refreshBubbleAvatars();
            flashAgentProfileStatus();
        })
        .catch(err => {
            const s = document.getElementById('agent-profile-status');
            if (s) { s.classList.remove('agent-status-ok'); s.textContent = err.message; }
        })
        .then(() => {
            const p = document.getElementById('agent-edit-avatar');
            if (p) p.classList.remove('is-uploading');
        });
}

/* Register this Agent as *my* default — the Agent my own conversation with no
   explicit target anchors to.

   A different act from `setAgentAsDefault` below: that one appoints the
   *tenant's* entry, which is a management decision and the same for everybody,
   while this writes only the caller's own preference and every user may set it.
   The server derives the subject from the session, so the body names the target
   and the revision that was read — never a user or a tenant.

   The revision is the pointer's own optimistic lock. Sending the one we read is
   what makes a stale form (two tabs, a double click, a slow network) fail loudly
   instead of quietly overwriting a newer choice; `null` is only meaningful while
   nothing has been registered. The marker moves after the server says so, and
   the catalogue is reloaded rather than patched in place so the resolved anchor,
   the grid order and the remembered preference all move together. */
function setAgentAsMyDefault(agentId) {
    if (!agentId) return Promise.resolve(false);
    const statusEl = () => document.getElementById('agent-profile-status')
        || document.getElementById('agent-editor-status');
    return fetch('/api/agents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            action: 'set_user_default',
            id: agentId,
            default_revision: userDefault.revision,
        }),
    }).then(r => r.json()).then(data => {
        if (data.status !== 'success') {
            throw new Error(data.code === 'version_conflict'
                ? t('agents_set_my_default_conflict')
                : (data.code === 'agent_not_usable'
                    ? t('agents_set_my_default_disabled')
                    : (data.code === 'forbidden'
                        ? t('agents_set_my_default_forbidden')
                        : (data.message || t('agents_set_my_default_failed')))));
        }
        return loadAgentCatalog().then(() => {
            renderAgentsGrid();
            if (selectedAdminAgentId) renderAgentDetail();
            const status = statusEl();
            if (status) {
                status.textContent = t('agents_set_my_default_done');
                status.classList.add('agent-status-ok');
            }
            return true;
        });
    }).catch(err => {
        const status = statusEl();
        if (status) {
            status.classList.remove('agent-status-ok');
            status.textContent = err.message;
        }
        return false;
    });
}

/* Make one Agent the tenant's default — the entry an Agent-less conversation
   anchors to for every member, and the one whose badge leads the config grid and
   the workbench.

   Tenant-scoped and administrator-only on the server, and *not* the same act as
   `setAgentAsMyDefault` above: this one changes what the tenant shares, so it is
   only offered when the server's payload says the caller may appoint it
   (`tenantDefaultManageable`) — never inferred from a role name on the client,
   which would offer an action the request then refuses.

   The default flag is derived server-side, so we reload the catalogue rather
   than patching one Agent in place: the badge, the grid order and the remembered
   default all have to move together. */
function setAgentAsDefault(agentId) {
    if (!agentId) return Promise.resolve(false);
    const statusEl = () => document.getElementById('agent-profile-status')
        || document.getElementById('agent-editor-status');
    return fetch('/api/agents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'set_default', id: agentId }),
    }).then(r => r.json()).then(data => {
        if (data.status !== 'success') {
            throw new Error(data.code === 'forbidden'
                ? t('agents_set_default_forbidden')
                : (data.code === 'private_agent_not_shareable'
                    ? t('agents_set_default_private')
                    : (data.message || t('agents_set_default_failed'))));
        }
        return loadAgentCatalog().then(() => {
            renderAgentsGrid();
            if (selectedAdminAgentId) renderAgentDetail();
            const status = statusEl();
            if (status) {
                status.textContent = t('agents_set_default_done');
                status.classList.add('agent-status-ok');
            }
            return true;
        });
    }).catch(err => {
        const status = statusEl();
        if (status) {
            status.classList.remove('agent-status-ok');
            status.textContent = err.message;
        }
        return false;
    });
}

function updateAgentWorkspace(agentId, updates, _retried) {
    return fetch('/api/agents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'update', id: agentId, revision: rosterRevision, ...updates }),
    }).then(r => r.json()).then(data => {
        if (data.status !== 'success') {
            // Two quick edits race: the second still carried the revision from
            // before the first landed. Re-sync and retry once, silently, so a
            // fast click just works instead of showing a lock error.
            if (data.code === 'stale_roster' && !_retried) {
                return loadAgentCatalog().then(() => updateAgentWorkspace(agentId, updates, true));
            }
            throw new Error(data.code === 'stale_roster' ? t('agents_stale') : (data.message || 'Update failed'));
        }
        return loadAgentCatalog().then(() => true);
    }).catch(err => {
        const status = document.getElementById('agent-profile-status') || document.getElementById('agent-editor-status');
        if (status) status.textContent = err.message;
        return false;
    });
}

function deleteAgent(agentId) {
    const agent = findAgent(agentId);
    if (!agent) return;
    if (agentId === defaultAgentId) return; // the default Agent is the instance
    showConfirmDialog({
        title: t('agents_delete_title'),
        message: t('agents_delete_confirm').replace('{name}', agent.name || agentId),
        okText: t('agents_delete'),
        cancelText: t('cancel'),
        onConfirm: () => _performAgentDelete(agentId),
    });
}

function _performAgentDelete(agentId, _retried) {
    return fetch('/api/agents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'delete', id: agentId, revision: rosterRevision }),
    }).then(r => r.json()).then(data => {
        if (data.status !== 'success') {
            if (data.code === 'stale_roster' && !_retried) {
                return loadAgentCatalog().then(() => _performAgentDelete(agentId, true));
            }
            throw new Error(data.code === 'stale_roster' ? t('agents_stale') : (data.message || 'Delete failed'));
        }
        // Leaving the detail open on a now-deleted Agent would show a ghost.
        if (selectedAdminAgentId === agentId) closeAgentDetail();
        // A conversation owned by the deleted Agent falls back to the default.
        if (activeAgentId === agentId) {
            activeAgentId = defaultAgentId;
            writeScopedPreference('cow_active_agent', activeAgentId);
        }
        // Drop the deleted Agent's remembered session id — its conversations
        // went with the workspace, so the pinned id would only re-pin a ghost.
        removeScopedPreference(`${SESSION_ID_KEY}:${agentId}`);
        return loadAgentCatalog().then(() => {
            renderComposerIdentity();
            // The Agent's sessions were removed server-side; refresh the open
            // list so its rows don't linger until the next unrelated reload.
            if (typeof _refreshHistoryList === 'function') _refreshHistoryList();
            return true;
        });
    }).catch(err => {
        const status = document.getElementById('agent-profile-status');
        if (status) status.textContent = err.message;
        else alert(err.message);
        return false;
    });
}

// The four core files an Agent can be edited through. BOOTSTRAP.md exists on
// disk for internal use but isn't meant for hand-editing, so it's left out of
// the picker entirely. Each option carries a short hint (rendered on the
// right of the dropdown row) so the raw filename isn't the only clue to what
// it holds.
function _agentCoreFileOptions() {
    return [
        { value: 'AGENT.md', label: 'AGENT.md', hint: t('agents_core_file_agent') },
        { value: 'USER.md', label: 'USER.md', hint: t('agents_core_file_user') },
        { value: 'RULE.md', label: 'RULE.md', hint: t('agents_core_file_rule') },
        { value: 'MEMORY.md', label: 'MEMORY.md', hint: t('agents_core_file_memory') },
    ];
}

let agentCoreViewMode = 'edit';

function initAgentCoreFileDropdown() {
    const el = document.getElementById('agent-core-file');
    if (!el) return;
    const current = el._ddValue || 'AGENT.md';
    initDropdown(el, _agentCoreFileOptions(), current, () => loadAgentCoreFile());
}

function currentAgentCoreFile() {
    const el = document.getElementById('agent-core-file');
    return (el && el._ddValue) || 'AGENT.md';
}

function setAgentCoreViewMode(mode) {
    agentCoreViewMode = mode;
    document.querySelectorAll('#agent-core-mode .agent-seg-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.mode === mode);
    });
    const editor = document.getElementById('agent-core-editor');
    const preview = document.getElementById('agent-core-preview');
    if (!editor || !preview) return;
    if (mode === 'preview') {
        preview.innerHTML = renderMarkdown(editor.value || '');
        // Same post-processing chat messages get: syntax highlighting plus the
        // language label + copy button on each code block (renderMarkdown only
        // produces the raw <pre>; the headers are added to the live DOM after).
        if (typeof applyHighlighting === 'function') applyHighlighting(preview);
        editor.classList.add('hidden');
        preview.classList.remove('hidden');
    } else {
        preview.classList.add('hidden');
        editor.classList.remove('hidden');
    }
}

function loadAgentCoreFile() {
    if (!selectedAdminAgentId) return;
    initAgentCoreFileDropdown();
    const filename = currentAgentCoreFile();
    if (!filename) return;
    _paintCoreFileStatus('pending', '…');
    fetch(`/api/agents/${encodeURIComponent(selectedAdminAgentId)}/files/${encodeURIComponent(filename)}`)
        .then(r => r.json()).then(data => {
            if (data.status !== 'success') throw new Error(data.message || t('agents_save_failed'));
            selectedCoreRevision = data.revision;
            document.getElementById('agent-core-editor').value = data.content || '';
            document.getElementById('agent-editor-label').textContent = `${selectedAdminAgentId} / ${filename}`;
            // The revision hash meant nothing to a human reader; a blank status
            // (nothing to report) reads better than a stray hex fragment.
            _paintCoreFileStatus('pending', '');
            // Refresh the preview in place if that's the active view, so
            // switching files while in preview mode doesn't show stale content.
            if (agentCoreViewMode === 'preview') setAgentCoreViewMode('preview');
        }).catch(err => { _paintCoreFileStatus('error', err.message); });
}

// Paint the save status with a colour + icon, not just bare text, so success
// and failure actually read differently at a glance. Success fades back to
// blank after a bit; failure stays until the next attempt so it isn't missed.
//
// Every other `*-status` element in this console is hidden via the shared
// `opacity-0` convention (see navigateTo/setLanguage, which blanket-fade any
// `[id$="-status"]` element on navigation). This one has the same id suffix
// so it gets caught by that same sweep — it must toggle `opacity-0` itself
// too, or a stray earlier sweep leaves it permanently invisible no matter
// what innerHTML is painted into it afterwards.
function _paintCoreFileStatus(kind, text) {
    const status = document.getElementById('agent-editor-status');
    if (!status) return;
    clearTimeout(_paintCoreFileStatus._t);
    status.classList.remove('agent-status-ok', 'agent-status-error');
    if (kind === 'ok') {
        status.innerHTML = `<i class="fas fa-check mr-1"></i>${escapeHtml(text)}`;
        status.classList.add('agent-status-ok');
        status.classList.remove('opacity-0');
        _paintCoreFileStatus._t = setTimeout(() => {
            status.textContent = '';
            status.classList.remove('agent-status-ok');
            status.classList.add('opacity-0');
        }, 2200);
    } else if (kind === 'error') {
        status.innerHTML = `<i class="fas fa-triangle-exclamation mr-1"></i>${escapeHtml(text)}`;
        status.classList.add('agent-status-error');
        status.classList.remove('opacity-0');
    } else {
        status.textContent = text || '';
        if (text) status.classList.remove('opacity-0');
    }
}

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
        activeAgentId = agentId;
        writeScopedPreference('cow_active_agent', activeAgentId);
        newChat(true, false);
        if (typeof resetWorkspaceToAgentRoot === 'function') resetWorkspaceToAgentRoot();
        navigateTo('chat');
        renderComposerIdentity();
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
        showConfirmDialog({
            title: t('agents_pick_tip'), message: t(key), hideCancel: true,
        });
    } else {
        renderAgentWorkbench();
    }
}

/** Reflect the selected Agent's identity in the chat header, so a solo chat
 *  with a single Agent still clearly names who the conversation belongs to. */
function paintChatAgentIdentity(agent) {
    if (!agent) return;
    const nameEl = document.getElementById('chat-agent-name');
    const faceEl = document.getElementById('chat-agent-avatar');
    if (!nameEl) return;
    nameEl.textContent = agent.name || agent.id;
    if (faceEl) faceEl.innerHTML = agentAvatarHTML(agent, 22);
    const head = document.getElementById('chat-agent-identity');
    if (head) {
        head.classList.toggle('hidden', currentView !== 'chat');
        head.removeAttribute('hidden');
    }
}

function conversationHasMessages() {
    return !!document.querySelector('#chat-messages .user-message-group, #chat-messages .bot-message-group');
}

/** A roster of one behaves exactly like the console did before Agents existed:
 *  no face on the composer, no faces in the session list, no @ mentions. */
function multiAgentMode() {
    return availableChatAgents().length > 1;
}

/** True once this conversation holds more than its owner. Until then it is an
 *  ordinary chat and is drawn like one. */
function sharedConversation() {
    return currentTeamIds().length > 0;
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
function botSpeakerAgent(msg, requestId) {
    if (!sharedConversation()) return null;
    const id = (msg && msg.extras && msg.extras.agent_id)
        || (requestId && _liveSpeakers[requestId])
        || activeAgentId;
    return findAgent(id) || null;
}

/** The Agent answering a live request, for the streaming bubble and the loading
 *  dots. Unlike botSpeakerAgent this also resolves in a solo chat, so a single
 *  Agent's own uploaded avatar shows while it streams instead of the logo. */
function liveSpeakerAgent(requestId) {
    const id = (requestId && _liveSpeakers[requestId]) || activeAgentId;
    return findAgent(id) || null;
}

/** Turn a written-out mention into a chip, so a name reads as a name instead
 *  of as an id someone pasted. Runs on the rendered bubble rather than on the
 *  markdown source, which keeps code spans untouched. */
function highlightMentions(root) {
    const roster = sessionRoster();
    if (!root || roster.length < 2) return;
    const byLabel = new Map();
    roster.forEach(agent => {
        [agent.name, agent.id].forEach(label => {
            if (label) byLabel.set(String(label).toLowerCase(), agent);
        });
    });
    const alternation = Array.from(byLabel.keys())
        .sort((a, b) => b.length - a.length)
        .map(label => label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
        .join('|');
    const re = new RegExp('@(' + alternation + ')(?=[\\s，,：:、]|$)', 'gi');

    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
        acceptNode: node => node.parentElement
            && node.parentElement.closest('code, pre, .mention-tag')
            ? NodeFilter.FILTER_REJECT
            : NodeFilter.FILTER_ACCEPT,
    });
    const targets = [];
    let node;
    while ((node = walker.nextNode())) {
        re.lastIndex = 0;
        if (re.test(node.nodeValue)) targets.push(node);
    }
    targets.forEach(text => {
        const value = text.nodeValue;
        const frag = document.createDocumentFragment();
        let cursor = 0;
        let match;
        re.lastIndex = 0;
        while ((match = re.exec(value))) {
            if (match.index > cursor) {
                frag.appendChild(document.createTextNode(value.slice(cursor, match.index)));
            }
            const agent = byLabel.get(match[1].toLowerCase());
            const tag = document.createElement('span');
            tag.className = 'mention-tag';
            if (agent) {
                // A chip that looks like the teammate it names: their face, then
                // their name. Falls back to plain text for an unknown label.
                tag.innerHTML = `<span class="mention-tag-face">${agentAvatarHTML(agent, 16)}</span><span class="mention-tag-name">${escapeHtml(agent.name || agent.id)}</span>`;
            } else {
                tag.textContent = '@' + match[1];
            }
            frag.appendChild(tag);
            cursor = match.index + match[0].length;
        }
        if (cursor < value.length) {
            frag.appendChild(document.createTextNode(value.slice(cursor)));
        }
        text.parentNode.replaceChild(frag, text);
    });
}

function renderComposerIdentity() {
    const agent = findAgent(activeAgentId) || { id: activeAgentId || defaultAgentId, name: activeAgentId || 'Agent' };
    paintChatAgentIdentity(agent);
    if (typeof paintWelcomeAgentIntro === 'function') paintWelcomeAgentIntro();
    const wrap = document.getElementById('composer-identity');
    const btn = document.getElementById('composer-agent-btn');
    if (!wrap || !btn) return;
    // A solo install has no choice to offer. Existing teammates still need
    // their menu even if the available catalog shrinks after this chat began.
    if (!multiAgentMode() && !currentTeamIds().length) {
        wrap.classList.add('hidden');
        document.getElementById('composer-agent-menu')?.classList.add('hidden');
        return;
    }
    wrap.classList.remove('hidden');
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
    const sections = [];

    // Once a conversation has teammates it is a group, and the only sensible
    // actions are adding and removing members - "switch the current Agent" would
    // silently abandon the group for a fresh solo chat. So the switch list only
    // appears in an ordinary (not-yet-shared) chat, where it opens a clean
    // conversation owned by the chosen Agent.
    if (!sharedConversation()) {
        sections.push(
            `<div class="composer-menu-title">${escapeHtml(t('agents_pick_tip'))}</div>`
            + availableChatAgents().map(agent => `
                <button type="button" class="composer-menu-item agent-row${agent.id === activeAgentId ? ' current' : ''}"
                        onclick="pickComposerAgent('${escapeHtml(agent.id)}')">
                    ${agentAvatarHTML(agent, 24)}
                    <span>${escapeHtml(agent.name)}</span>
                    ${agent.id === activeAgentId ? '<i class="fas fa-check ml-auto text-[11px]"></i>' : ''}
                </button>`).join('')
        );
    }

    // Inviting is team membership, so it reads the team projection: a coding
    // Agent is not merely disabled here, it is absent — no row, no face, no
    // "unavailable" label (task 2.2).
    const candidates = teamCandidateAgents().filter(a => a.id !== activeAgentId && !taken.has(a.id));

    // A group chat first lists the teammates already in the conversation (the
    // owner is implicit and not shown), then, in a separate section below, who
    // can still be pulled in. Splitting the two makes it obvious these rows are
    // members to remove, not options to pick.
    if (sharedConversation()) {
        // Only members that can actually take a turn get a row. A stored member
        // that no longer resolves, or that resolves to a coding/disabled Agent,
        // is reported by a generic warning below and never by its name, face or
        // type (task 2.5) — naming it would be exactly the "coding Agent appears
        // in a team surface" leak the boundary forbids.
        const joined = validTeamMemberRows().map(m => `
            <button type="button" class="composer-menu-item agent-row joined"
                    onclick="removeTeamMember('${escapeHtml(m.id)}')" title="${escapeHtml(t('team_remove'))}">
                ${agentAvatarHTML(m, 24)}
                <span>${escapeHtml(m.name || m.id)}</span>
                <i class="fas fa-check ml-auto text-[11px] joined-check"></i>
                <i class="fas fa-xmark ml-auto text-[11px] joined-remove"></i>
            </button>`).join('');
        if (joined) {
            sections.push(
                `<div class="composer-menu-title">${escapeHtml(t('team_members'))}</div>${joined}`
            );
        }
        if (invalidTeamMembers().length) {
            sections.push(
                `<div class="composer-menu-warning">${escapeHtml(t('team_invalid_members'))}</div>`
            );
        }
    }

    const invitable = candidates.map(agent => `
        <button type="button" class="composer-menu-item agent-row"
                onclick="inviteTeamMember('${escapeHtml(agent.id)}')">
            ${agentAvatarHTML(agent, 24)}
            <span>${escapeHtml(agent.name)}</span>
            <i class="fas fa-plus ml-auto text-[11px] text-slate-400"></i>
        </button>`).join('');
    if (invitable) {
        sections.push(
            `<div class="composer-menu-title">${escapeHtml(t('team_invite'))}</div>${invitable}`
        );
    }

    // Always offer a way to make a new Agent, so a single-Agent user discovers
    // the team feature straight from the composer.
    sections.push(
        `<button type="button" class="composer-menu-item agent-row composer-menu-create"
                onclick="openAgentCreateFromComposer()">
            <span class="composer-menu-create-icon"><i class="fas fa-plus"></i></span>
            <span>${escapeHtml(t('agents_create'))}</span>
        </button>`
    );

    menu.innerHTML = sections.join('<div class="composer-menu-sep"></div>');
}

/** Jump from the composer straight into agent creation: close the menu, land on
 *  the team tab, and open the create form. */
function openAgentCreateFromComposer() {
    document.getElementById('composer-agent-menu')?.classList.add('hidden');
    navigateTo('agents');
    if (typeof openAgentCreateForm === 'function') openAgentCreateForm();
}

function pickComposerAgent(agentId) {
    document.getElementById('composer-agent-menu')?.classList.add('hidden');
    if (!agentId || agentId === activeAgentId) return;
    // Use the same guarded, freshly validated start as the workbench. A cancel
    // must keep the current owner, and a switch must not inherit its project.
    return startChatWithAgent(agentId);
}

function inviteTeamMember(agentId) {
    // Keep the menu open so the invited Agent visibly moves from "+ add" to the
    // "× remove" list, and the user can invite several in a row without having
    // to reopen it each time. A refused write must say so instead of leaving the
    // menu looking as though the invite happened.
    addTeamMember(agentId).then(refreshComposerAgentMenuIfOpen)
        .catch(err => _wsToast((err && err.message) || t('session_settings_failed')));
}

/** The stored team roster exactly as the server reported it (identity included). */
function teamRosterRows() {
    return ((_sessCfg && _sessCfg.team && _sessCfg.team.members) || []);
}

/** The stored members that may actually take a turn: resolvable, ordinary,
    enabled and within the caller's use range. Everything else is an invalid
    member — it is *tolerated* (the messages stay, the roster is not rewritten
    behind the user's back) but it never runs and it is never named. */
function validTeamMemberRows() {
    const candidates = teamCandidateAgents();
    return teamRosterRows().filter(m => candidates.some(a => a.id === m.id));
}

/** The invalid members, reported only as a count by the selection surfaces. */
function invalidTeamMembers() {
    const valid = new Set(validTeamMemberRows().map(m => m.id));
    return teamRosterRows().filter(m => !valid.has(m.id));
}

/** The members a team write should submit: the valid ones only.

    The server refuses a roster that names a coding/unknown/unauthorized object
    *as a whole*, so re-submitting a legacy roster verbatim would make every
    later invite fail. Submitting the valid set is the explicit cleanup the spec
    asks for, and it is what makes the store agree with what the user can see. */
function validTeamMemberIds() {
    return validTeamMemberRows().map(m => m.id);
}

/** Everyone addressable in this conversation, owner first.

    Only participants that can take a turn are addressable: a legacy roster's
    coding or unresolvable member is neither shown as a mention chip nor
    offered as an @ target (tasks 2.2/2.5). */
function sessionRoster() {
    const owner = findAgent(activeAgentId);
    const roster = owner ? [owner] : [];
    validTeamMemberRows().forEach(m => {
        if (!roster.some(a => a.id === m.id)) roster.push(findAgent(m.id) || m);
    });
    return roster;
}

/** The teammate a message hands the turn to, or '' for nobody.
 *  Mirrors the server's rule: a leading mention only. */
function addressedAgentId(text) {
    const stripped = String(text || '').replace(/^\s+/, '');
    if (!stripped.startsWith('@')) return '';
    const labels = [];
    sessionRoster().forEach(agent => {
        [agent.name, agent.id].forEach(label => {
            if (label) labels.push([String(label), agent.id]);
        });
    });
    labels.sort((a, b) => b[0].length - a[0].length);
    for (const [label, id] of labels) {
        const re = new RegExp('^@' + label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(?=[\\s，,：:、]|$)', 'i');
        // The owner is addressable too; the server treats "@owner" as the owner
        // simply taking the turn, so no special-casing here.
        if (re.test(stripped)) return id;
    }
    return '';
}

function mentionedAgentIds(text) {
    const id = addressedAgentId(text);
    return id ? [id] : [];
}

function currentTeamIds() {
    return ((_sessCfg && _sessCfg.team && _sessCfg.team.members) || []).map(m => m.id);
}

/** Persist one conversation's roster, addressed explicitly.

    ``target`` carries ``{sessionId, agentId}`` so a caller can save a roster for
    a conversation that is *not* the one on screen — the prepare-then-commit team
    start needs exactly that, and addressing the write through the page's global
    ``sessionId`` / ``activeAgentId`` would write it onto whatever the user
    happens to be looking at (design D4).

    Rejects with the server's own message when the write is refused or fails:
    "no members saved" and "members saved" must not look alike to the caller
    (task 2.4). */
function setTeamMembers(ids, target) {
    const ownerId = (target && target.agentId) || activeAgentId || '';
    const sid = (target && target.sessionId) || sessionId;
    const unique = Array.from(new Set((ids || []).filter(id => id && id !== ownerId)));
    const body = { members: unique.length ? unique : null };
    if (ownerId) body.agent_id = ownerId;
    // Spell the owner in the query too: the console's global fetch wrapper
    // otherwise fills an absent ``agent_id`` from the page's current Agent.
    const url = `/api/sessions/${encodeURIComponent(sid)}/settings`
        + (ownerId ? `?agent_id=${encodeURIComponent(ownerId)}` : '');
    return fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    }).then(r => r.json().catch(() => ({})).then(data => {
        if (!r.ok || !data || data.status !== 'success') {
            throw new Error((data && data.message) || t('session_settings_failed'));
        }
        // Only a write that targeted the conversation on screen may repaint it.
        if (sid === sessionId && ownerId === activeAgentId) {
            _sessCfg = { model: data.model, team: data.team };
            renderComposerIdentity();
            // Inviting or removing someone changes whether one model can speak
            // for this conversation.
            _renderModelChip();
        }
        return data;
    }));
}

function addTeamMember(agentId) {
    if (!agentId || agentId === activeAgentId) return Promise.resolve();
    const ids = validTeamMemberIds();
    if (ids.includes(agentId)) return Promise.resolve();
    return setTeamMembers([...ids, agentId]);
}

function removeTeamMember(agentId) {
    return setTeamMembers(validTeamMemberIds().filter(id => id !== agentId))
        .then(refreshComposerAgentMenuIfOpen)
        .catch(err => {
            refreshComposerAgentMenuIfOpen();
            _wsToast((err && err.message) || t('session_settings_failed'));
        });
}

/** Repaint the agent menu if it is still open, so add/remove show immediately. */
function refreshComposerAgentMenuIfOpen() {
    const menu = document.getElementById('composer-agent-menu');
    if (menu && !menu.classList.contains('hidden')) renderComposerAgentMenu();
}

async function syncTeamFromText(text) {
    const extra = mentionedAgentIds(text);
    if (!extra.length) return;
    try {
        await setTeamMembers([...validTeamMemberIds(), ...extra]);
    } catch (err) {
        // A mention must not become a silent roster write that did not happen;
        // the message still goes out, addressed to whoever is on the team.
        _wsToast((err && err.message) || t('session_settings_failed'));
    }
}

// Point a channel instance at an Agent. Binding lives on the instance itself
// (channel_instances[].agent_id); an empty agentId means "follow the default
// Agent". instanceId defaults to the channel type for a single-instance channel.
function bindChannelAgent(channelType, agentId, instanceId, members) {
    const defaultId = defaultAgentId;
    const bound = (agentId && agentId !== defaultId) ? agentId : '';
    const iid = instanceId || channelType;
    const payload = {
        action: 'bind_channel_instance',
        channel_type: channelType,
        instance_id: iid,
        agent_id: bound,
    };
    // Only send members when we mean to set the team; omitting it leaves the
    // stored roster untouched (a plain owner-only rebind).
    if (Array.isArray(members)) payload.members = members;
    return fetch('/api/agents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    }).then(r => r.json()).then(data => {
        if (data.status !== 'success') throw new Error(data.message || 'Save failed');
        // Rebinding is a hot swap on the server (no channel restart), and the
        // dropdown already reflects the new value locally, so only the roster
        // catalog needs refreshing. Re-rendering the channels view here would
        // rebuild the cards and reset the scan/manual tab state for no reason.
        if (Array.isArray(channelInstancesView)) {
            const rec = channelInstancesView.find(i => i.instance_id === iid);
            if (rec) {
                rec.agent_id = bound;
                if (data.result && Array.isArray(data.result.members)) {
                    rec.members = data.result.members.slice();
                }
            }
        }
        return loadAgentCatalog();
    }).catch(err => _wsToast(err.message));
}

function channelBoundAgentId(channelType) {
    const inst = channelInstances.find(i =>
        (i.channel_type || '').toLowerCase() === channelType
    );
    return inst ? (inst.agent_id || '') : '';
}

// The target the memory page is addressing. `MEMORY_PERSONAL` is a *chosen*
// target (my own user memory), distinct from `''` which means "nothing chosen
// yet" — collapsing the two would make the personal domain unreachable, because
// `''` falls back to the Agent the console is working with (task 5.1).
const MEMORY_PERSONAL = 'personal';

let memoryAgentId = readScopedPreference('cow_memory_agent') || '';

// The legal target set, served with the list (task 5.1). Empty on an older
// backend, in which case the local catalogue is used as before.
let memoryTargets = [];

function viewingMemoryTarget() {
    return memoryAgentId || activeAgentId || defaultAgentId || '';
}

function memoryTargetQuery() {
    // Naming the personal domain explicitly rather than "no agent_id" keeps the
    // two meanings apart: an absent target is a refusal, the personal domain is
    // a choice.
    return viewingMemoryTarget() === MEMORY_PERSONAL
        ? 'scope=personal'
        : `agent_id=${encodeURIComponent(viewingMemoryTarget() || '')}`;
}

function memoryTargetOptions() {
    // The server's set is authoritative when present: it is derived from the
    // same predicate the read and the write are authorised by, so it cannot
    // offer the tenant's shared memory to a member the request would refuse.
    const fromServer = Array.isArray(memoryTargets) && memoryTargets.length > 0;
    if (fromServer) {
        return memoryTargets.map(row => ({
            value: row.value,
            label: row.kind === 'personal'
                ? t('memory_target_personal')
                : (row.name || row.agent_id),
            agent: row.kind === 'personal' ? null : { id: row.agent_id, name: row.name },
        }));
    }
    // Older backend: keep the previous behaviour rather than emptying the picker.
    const list = agentCatalog.length ? agentCatalog : enabledAgents();
    return [{ value: MEMORY_PERSONAL, label: t('memory_target_personal'), agent: null }]
        .concat(list.map(a => ({ value: a.id, label: a.name || a.id, agent: a })));
}

function renderMemoryAgentSelect() {
    const el = document.getElementById('memory-agent-select');
    if (!el) return;
    initDropdown(el, memoryTargetOptions(), viewingMemoryTarget(), (value) => selectMemoryAgent(value), { withAvatar: true });
}

function selectMemoryAgent(agentId) {
    memoryAgentId = agentId;
    writeScopedPreference('cow_memory_agent', agentId);
    closeMemoryViewer();
    loadMemoryView(1);
}

// =====================================================================
// Markdown Renderer
// =====================================================================
const FALLBACK_HLJS = {
    getLanguage() { return false; },
    highlight(str) { return { value: escapeHtml(str) }; },
    highlightAuto(str) { return { value: escapeHtml(str) }; },
    highlightElement() {},
};

function getHljs() {
    return window.hljs || FALLBACK_HLJS;
}

// CJK ideographs, kana, Hangul and full/halfwidth forms (BMP only).
const CJK_CHAR_RE = /[\u1100-\u11FF\u2E80-\u303F\u3040-\u33FF\u3400-\u4DBF\u4E00-\u9FFF\uA960-\uA97F\uAC00-\uD7FF\uF900-\uFAFF\uFE10-\uFE19\uFE30-\uFE6F\uFF00-\uFF60\uFFE0-\uFFE6]/;

// CommonMark's flanking rules treat every Unicode punctuation alike, so
// `是**"引号"**——` never opens emphasis: the quote after `**` is punctuation
// while 是 before it is neither punctuation nor space, and the run degrades to
// literal asterisks. Apply the CJK-friendly amendment
// (github.com/tats-u/markdown-cjk-friendly): a `*` run with a CJK neighbour and
// no adjacent whitespace both opens and closes. `_` keeps the stock rules,
// whose intraword behavior depends on the original classification.
function patchCjkEmphasis(md) {
    const State = md.inline && md.inline.State;
    if (!State || !State.prototype.scanDelims || State.prototype._cjkEmphasisPatched) return;
    const utils = md.utils;
    const scanDelims = State.prototype.scanDelims;
    State.prototype.scanDelims = function(start, canSplitWord) {
        const res = scanDelims.call(this, start, canSplitWord);
        if (!canSplitWord) return res;
        const lastCode = start > 0 ? this.src.charCodeAt(start - 1) : 0x20;
        const nextPos = start + res.length;
        const nextCode = nextPos < this.posMax ? this.src.charCodeAt(nextPos) : 0x20;
        if (utils.isWhiteSpace(lastCode) || utils.isWhiteSpace(nextCode)) return res;
        if (!CJK_CHAR_RE.test(String.fromCharCode(lastCode)) &&
            !CJK_CHAR_RE.test(String.fromCharCode(nextCode))) return res;
        res.can_open = true;
        res.can_close = true;
        return res;
    };
    State.prototype._cjkEmphasisPatched = true;
}

function createMd() {
    const hljsLib = getHljs();
    const mdFactory = window.markdownit;
    if (typeof mdFactory !== 'function') {
        return {
            render(text) {
                return `<p>${escapeHtml(text || '')}</p>`;
            }
        };
    }
    const md = mdFactory({
        html: false, breaks: true, linkify: true, typographer: true,
        highlight: function(str, lang) {
            if (lang && hljsLib.getLanguage(lang)) {
                try { return hljsLib.highlight(str, { language: lang }).value; } catch (_) {}
            }
            return hljsLib.highlightAuto(str).value;
        }
    });
    patchCjkEmphasis(md);
    // Fix greedy linkify: markdown-it's linkify swallows markdown emphasis (*)
    // and CJK full-width punctuation glued to a URL (common in LLM output like
    // "**https://x**，中文"), turning the whole tail into one broken link. Cut
    // the URL at the first such char and spill the remainder back as text.
    var GREEDY_LINK_CUT = /[*\u3000-\u303F\uFF00-\uFFEF]/;
    md.core.ruler.after('linkify', 'fix_greedy_linkify', function(state) {
        for (var b = 0; b < state.tokens.length; b++) {
            var blk = state.tokens[b];
            if (blk.type !== 'inline' || !blk.children) continue;
            var ch = blk.children;
            for (var i = 0; i < ch.length; i++) {
                var open = ch[i];
                if (open.type !== 'link_open' || open.markup !== 'linkify') continue;
                var textTok = ch[i + 1], close = ch[i + 2];
                if (!textTok || textTok.type !== 'text' || !close || close.type !== 'link_close') continue;
                var idx = textTok.content.search(GREEDY_LINK_CUT);
                if (idx < 0) continue;
                var keep = textTok.content.slice(0, idx);
                var spill = textTok.content.slice(idx);
                textTok.content = keep;
                open.attrSet('href', keep);
                var spillTok = new state.Token('text', '', 0);
                spillTok.content = spill;
                ch.splice(i + 3, 0, spillTok);
            }
        }
    });
    const defaultLinkOpen = md.renderer.rules.link_open || function(tokens, idx, options, env, self) {
        return self.renderToken(tokens, idx, options);
    };
    md.renderer.rules.link_open = function(tokens, idx, options, env, self) {
        const token = tokens[idx];
        // A workspace-relative href would resolve against the console URL and
        // 404 in a new tab. Tag it instead so the click handler in
        // workspace.js opens it in the preview panel.
        const wsPath = typeof wsWorkspaceHref === 'function'
            ? wsWorkspaceHref(token.attrGet('href') || '') : null;
        if (wsPath) {
            token.attrPush(['data-ws-path', wsPath]);
            token.attrJoin('class', 'ws-link');
        } else {
            token.attrPush(['target', '_blank']);
            token.attrPush(['rel', 'noopener noreferrer']);
        }
        return defaultLinkOpen(tokens, idx, options, env, self);
    };
    // A table can't shrink below its columns' minimum content width, so a wide
    // comparison table would run past the bubble. Wrap it in a scroller: it
    // still fills the bubble when it fits and scrolls sideways when it doesn't.
    const defaultTableOpen = md.renderer.rules.table_open || function(tokens, idx, options, env, self) {
        return self.renderToken(tokens, idx, options);
    };
    const defaultTableClose = md.renderer.rules.table_close || function(tokens, idx, options, env, self) {
        return self.renderToken(tokens, idx, options);
    };
    md.renderer.rules.table_open = function(tokens, idx, options, env, self) {
        return '<div class="table-wrap">' + defaultTableOpen(tokens, idx, options, env, self);
    };
    md.renderer.rules.table_close = function(tokens, idx, options, env, self) {
        return defaultTableClose(tokens, idx, options, env, self) + '</div>';
    };
    return md;
}

const md = createMd();

const VIDEO_EXT_RE = /\.(?:mp4|webm|mov|avi|mkv)$/i;  // tested against URL without query string
const IMAGE_EXT_RE = /\.(?:jpg|jpeg|png|gif|webp|bmp|svg)$/i;  // tested against URL without query string

// Windows absolute path (D:\x.png / D:/x.png).
const WIN_ABS_PATH_RE = /^[A-Za-z]:[\\/]/;

function _toWebUrl(url) {
    if ((/^\/[A-Za-z]/.test(url) || WIN_ABS_PATH_RE.test(url)) && !url.startsWith('/api/')) {
        return '/api/file?path=' + encodeURIComponent(url);
    }
    if (/^file:\/\/\//i.test(url)) {
        // file:///home/x → /home/x, but file:///D:/x stays drive-relative.
        const p = url.replace(/^file:\/\/\//i, '');
        return '/api/file?path=' + encodeURIComponent(WIN_ABS_PATH_RE.test(p) ? p : '/' + p);
    }
    return url;
}

function _buildVideoHtml(url) {
    const webUrl = _toWebUrl(url);
    const fileName = url.split('/').pop().split('?')[0];
    return `<div style="margin:10px 0;">` +
        `<video controls preload="metadata" ` +
        `style="max-width:100%;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,0.15);display:block;">` +
        `<source src="${webUrl}"></video>` +
        `<a href="${webUrl}" target="_blank" ` +
        `style="display:inline-flex;align-items:center;gap:4px;margin-top:4px;font-size:12px;color:#8b8fa8;text-decoration:none;">` +
        `<i class="fas fa-download"></i> ${escapeHtml(fileName)}</a></div>`;
}

function _openImageLightbox(src) {
    let overlay = document.getElementById('cow-lightbox');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'cow-lightbox';
        overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,0.85);display:flex;align-items:center;justify-content:center;cursor:zoom-out;opacity:0;transition:opacity .2s';
        overlay.onclick = () => { overlay.style.opacity = '0'; setTimeout(() => overlay.style.display = 'none', 200); };
        const img = document.createElement('img');
        img.id = 'cow-lightbox-img';
        img.style.cssText = 'max-width:92vw;max-height:92vh;border-radius:8px;box-shadow:0 4px 24px rgba(0,0,0,0.5);object-fit:contain;';
        img.onclick = (e) => e.stopPropagation();
        overlay.appendChild(img);
        document.body.appendChild(overlay);
    }
    overlay.querySelector('#cow-lightbox-img').src = src;
    overlay.style.display = 'flex';
    requestAnimationFrame(() => overlay.style.opacity = '1');
}

function _buildImageHtml(url) {
    const webUrl = _toWebUrl(url);
    const safeUrl = webUrl.replace(/"/g, '&quot;');
    return `<div style="margin:10px 0;">` +
        `<img src="${safeUrl}" alt="image" loading="lazy" ` +
        `onclick="_openImageLightbox(this.src)" ` +
        `style="max-width:520px;width:100%;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,0.15);display:block;cursor:zoom-in;">` +
        `</div>`;
}

function injectVideoPlayers(html) {
    // Step 1: replace markdown-it anchor tags whose href points to a video file.
    const step1 = html.replace(
        /<a\s+href="(https?:\/\/[^"]+)"[^>]*>[^<]*<\/a>/gi,
        (match, url) => VIDEO_EXT_RE.test(url.split('?')[0]) ? _buildVideoHtml(url) : match
    );
    // Step 2: replace any remaining bare video URLs in text nodes (not inside HTML tags).
    // Split on HTML tags to avoid touching src/href attributes already in markup.
    return step1.split(/(<[^>]+>)/).map((chunk, idx) => {
        // Even indices are text nodes; odd indices are HTML tags — leave them untouched.
        if (idx % 2 !== 0) return chunk;
        return chunk.replace(/https?:\/\/\S+/gi, (url) => {
            const bare = url.replace(/[),.\s]+$/, '');  // strip trailing punctuation
            return VIDEO_EXT_RE.test(bare.split('?')[0]) ? _buildVideoHtml(bare) : url;
        });
    }).join('');
}

// Convert image URLs into inline <img> previews. Mirrors injectVideoPlayers but for images.
// Handles three cases produced by markdown-it:
//   1. <a href="...image.jpg">...</a>  (bare URL or autolink that linkify turned into an anchor)
//   2. <img src="...">                  (markdown image syntax) — leave as-is, but normalize style
//   3. raw URL still present in a text node                    — only as a safety net
function injectImagePreviews(html) {
    // Step 1: anchor whose href points to an image file -> replace with <img> preview.
    const step1 = html.replace(
        /<a\s+href="(https?:\/\/[^"]+)"[^>]*>[^<]*<\/a>/gi,
        (match, url) => IMAGE_EXT_RE.test(url.split('?')[0]) ? _buildImageHtml(url) : match
    );
    // Step 2: bare image URLs left in text nodes (rare — markdown-it's linkify usually catches them).
    return step1.split(/(<[^>]+>)/).map((chunk, idx) => {
        if (idx % 2 !== 0) return chunk;
        return chunk.replace(/https?:\/\/\S+/gi, (url) => {
            const bare = url.replace(/[),.\s]+$/, '');
            return IMAGE_EXT_RE.test(bare.split('?')[0]) ? _buildImageHtml(bare) : url;
        });
    }).join('');
}

function _rewriteLocalImgSrc(html) {
    return html.replace(/<img\s([^>]*?)src="([^"]+)"([^>]*?)>/gi, (match, pre, src, post) => {
        const webSrc = _toWebUrl(src);
        const safeSrc = webSrc.replace(/"/g, '&quot;');
        const hasClick = /onclick/i.test(pre + post);
        const clickAttr = hasClick ? '' : ` onclick="_openImageLightbox(this.src)" style="cursor:zoom-in;"`;
        return `<img ${pre}src="${safeSrc}"${post}${clickAttr}>`;
    });
}

function renderMarkdown(text) {
    try {
        let html = md.render(text);
        html = _rewriteLocalImgSrc(html);
        // Order matters: video first (more specific), then image.
        html = injectImagePreviews(injectVideoPlayers(html));
        // Fallback for files the agent only mentions by path (workspace.js).
        if (typeof injectFileChips === 'function') html = injectFileChips(html);
        // Note: Code block headers are added via DOM manipulation after insertion
        // See addCodeBlockHeadersToElement()
        return html;
    }
    catch (e) { return text.replace(/\n/g, '<br>'); }
}

function _addCodeBlockHeaders(container) {
    // Add header with language label and copy button to each <pre> block using DOM manipulation
    const preBlocks = container.querySelectorAll('pre');
    preBlocks.forEach(pre => {
        if (pre.parentElement && pre.parentElement.classList.contains('code-block-wrapper')) return;
        
        const codeEl = pre.querySelector('code');
        if (!codeEl) return;
        
        const langClass = Array.from(codeEl.classList).find(c => c.startsWith('language-'));
        const language = langClass ? langClass.replace('language-', '') : '';
        // Hide label for unknown/empty languages (e.g. language-undefined)
        const showLang = language && language !== 'undefined' && language !== 'code';
        const langLabel = showLang ? language.charAt(0).toUpperCase() + language.slice(1) : '';
        
        const wrapper = document.createElement('div');
        wrapper.className = 'code-block-wrapper';
        
        const header = document.createElement('div');
        header.className = 'code-block-header';
        header.innerHTML = `
            <span class="code-block-lang">${langLabel}</span>
            <button class="code-copy-btn" title="Copy code">
                <i class="fas fa-copy"></i>
            </button>
        `;
        
        pre.parentNode.insertBefore(wrapper, pre);
        wrapper.appendChild(header);
        wrapper.appendChild(pre);
    });
}

// =====================================================================
// Chat Module
// =====================================================================
let isPolling = false;
let pollGeneration = 0;   // incremented on each restart to cancel stale poll loops
let loadingContainers = {};
let activeStreams = {};   // request_id -> EventSource
let sessionActiveRequest = {};   // agent_id + session_id -> request_id
const PENDING_VOICE_ATTACH_TTL_MS = 2 * 60 * 1000;
const PENDING_VOICE_ATTACH_MAX = 100;
const pendingVoiceAttachments = new Map(); // session_id:bot_seq -> pending audio

function runtimeSessionKey(sid, agentId = activeAgentId) {
    return `${agentId || defaultAgentId || 'default'}::${sid}`;
}

function isCurrentSessionConversationActive() {
    return !!sessionActiveRequest[runtimeSessionKey(sessionId)];
}

function updateEditButtonsState() {
    const active = isCurrentSessionConversationActive();
    document.querySelectorAll('.edit-msg-btn, .delete-msg-btn').forEach(btn => {
        btn.disabled = active;
        if (btn.classList.contains('edit-msg-btn')) {
            btn.title = active
                ? t('edit_disabled_reply_active')
                : t('edit_message');
        } else {
            btn.title = active
                ? t('delete_disabled_reply_active')
                : t('delete_message_title');
        }
    });
}
let streamBuffers = {};   // request_id -> { items: [event...], timestamp } for re-attach replay
let isComposing = false;
let appConfig = { use_agent: false, title: PRODUCT_NAME, subtitle: '', providers: {}, api_bases: {} };

let activeAgentId = readScopedPreference('cow_active_agent') || '';
const SESSION_ID_KEY = 'cow_session_id';

function activeSessionStorageKey() {
    return activeAgentId && activeAgentId !== defaultAgentId && activeAgentId !== 'default'
        ? `${SESSION_ID_KEY}:${activeAgentId}`
        : SESSION_ID_KEY;
}

// Carry the selected Agent through existing console requests without forcing
// every feature panel to implement its own routing glue.
const _nativeFetch = window.fetch.bind(window);

// Database-mode transports that are NOT under /api but still resolve their
// tenant from the X-Tenant-ID selection. Keep this list in step with the
// `tenant`-policy, non-/api routes in channel/web/route_registry.py: a route
// omitted here reaches the gate looking like "no tenant selected" (400
// missing_tenant) and the feature fails with no visible reason. `/uploads/...`
// is deliberately absent — the browser reads it as an <img>/<audio>
// subresource, which cannot carry a header at all; its tenant is derived from
// the addressed Agent instead (see _uploads_identity_scope).
const _TENANT_TRANSPORT_PATHS = ['/message', '/stream', '/poll', '/cancel', '/upload'];
const _TENANT_TRANSPORT_RE = new RegExp(
    `^/(?:${_TENANT_TRANSPORT_PATHS.map(p => p.slice(1)).join('|')})\\b`);

window.fetch = function(input, init) {
    init = init ? { ...init } : {};
    let url = typeof input === 'string' ? input : input.url;
    // In database identity mode the request context is tenant-scoped. The
    // selected tenant lives in sessionStorage (cow_tenant_id) but the core
    // console requests (agents / sessions / history / knowledge) do not
    // otherwise carry it, so the backend rejects them with a 400
    // "tenant selection required". Inject the header for same-origin requests
    // here, mirroring identity-admin.js / todos.js apiFetch. This is
    // a no-op in legacy mode (no tenant is ever stored).
    const tenantId = sessionStorage.getItem('cow_tenant_id');
    if (tenantId && typeof url === 'string' && url.startsWith('/')
            && (/^\/api\//.test(url) || _TENANT_TRANSPORT_RE.test(url))
            && !/^\/api\/auth\//.test(url)) {
        const headers = init.headers instanceof Headers
            ? new Headers(init.headers)
            : new Headers(init.headers || {});
        if (!headers.has('X-Tenant-ID')) headers.set('X-Tenant-ID', tenantId);
        init.headers = headers;
    }
    if (activeAgentId && typeof url === 'string' && url.startsWith('/')) {
        if (!/[?&]agent_id=/.test(url)) {
            const joiner = url.includes('?') ? '&' : '?';
            url = `${url}${joiner}agent_id=${encodeURIComponent(activeAgentId)}`;
        }
        if (typeof input !== 'string') input = new Request(url, input);
        else input = url;

        // JSON bodies read agent_id from the payload, so inject it there too.
        // Multipart (FormData) uploads must NOT get a body copy: the query
        // string above already carries it, and web.py merges query + body,
        // collapsing the duplicate into a list (agent_id=['x','x']). That list
        // then reaches handlers expecting a plain string and raises
        // "unhashable type: 'list'", silently killing every file upload.
        if (typeof init.body === 'string') {
            const contentType = new Headers(init.headers || {}).get('Content-Type') || '';
            if (contentType.includes('application/json')) {
                try {
                    const body = JSON.parse(init.body);
                    if (body && typeof body === 'object' && !Array.isArray(body) && !body.agent_id) {
                        body.agent_id = activeAgentId;
                        init.body = JSON.stringify(body);
                    }
                } catch (_) {}
            }
        }
    }
    return _nativeFetch(input, init);
};

function generateSessionId() {
    return 'session_' + ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g, c =>
        (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16)
    );
}

// Restore session_id from localStorage so conversation history survives page refresh.
// A new id is only generated when the user explicitly starts a new chat.
function loadOrCreateSessionId() {
    const stored = readScopedPreference(activeSessionStorageKey());
    if (stored) return stored;
    const fresh = generateSessionId();
    writeScopedPreference(activeSessionStorageKey(), fresh);
    return fresh;
}

let sessionId = loadOrCreateSessionId();

// ---- Conversation history state ----
let historyPage = 0;       // last page fetched (0 = nothing fetched yet)
let historyHasMore = false;
let historyLoading = false;
let _historyLoadSeq = 0;
let _historyLoadContext = '';

function restoreChatState() {
    const epoch = _authEpoch, owner = activeAgentId, sid = sessionId;
    const current = () => epoch === _authEpoch && owner === activeAgentId && sid === sessionId;
    return fetch('/config').then(r => r.json()).then(data => {
        if (!current()) return;
        if (data.status === 'success') {
            appConfig = data;
            appConfig.title = productTitle(data.title);
            const welcomeTitle = document.getElementById('welcome-title');
            if (welcomeTitle) welcomeTitle.innerHTML = productTitleHTML(appConfig.title);
            initConfigView(data);
        }
        loadHistory(1);
    }).catch(() => { if (current()) loadHistory(1); });
}

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

const chatInput = document.getElementById('chat-input');
const sendBtn = document.getElementById('send-btn');
const steerBtn = document.getElementById('steer-btn');
const messagesDiv = document.getElementById('chat-messages');
// Cache only welcome markup; the real composer remains a sibling for its entire
// lifetime. CSS orders it between the intro and suggestions in an empty chat.
const welcomeTemplateHTML = document.getElementById('welcome-screen')?.innerHTML || '';
function syncChatHomeLayout() {
    const main = document.getElementById('chat-main');
    if (!main) return;
    // ``chat-home`` means "this is an empty chat", and a mounted coding pane is
    // the opposite of that. Its arrival alone does not remove the welcome markup
    // -- only an ordinary message does, and a coding conversation renders none --
    // so presence of the markup is not enough to answer the question. This also
    // keeps a later re-render (the observer below) from putting the home layout
    // back on top of the frame the module just mounted.
    const home = !!document.getElementById('welcome-screen') && !codingPaneMounted();
    const changed = home !== main.classList.contains('chat-home');
    main.classList.toggle('chat-home', home);
    if (changed) main.scrollTop = 0;
    syncHomeHeroOffset();
    watchHomeHero(main);
}

/** Whether the coding module currently owns the conversation area. */
function codingPaneMounted() {
    const module = codingChatModule();
    try {
        return !!(module && typeof module.isActive === 'function' && module.isActive());
    } catch (err) {
        return false;
    }
}
// The empty-home hero is centred on the vertical axis, which needs its real
// height: the brand line and the narrow-screen type scale both move it, so
// nothing here is assumed. Re-reading is safe because the offset translates the
// hero rigidly instead of resizing it, so the measurement does not depend on
// the offset it produces.
function syncHomeHeroOffset() {
    const main = document.getElementById('chat-main');
    if (!main || !main.classList.contains('chat-home')) return;
    const intro = main.querySelector('.home-intro');
    const composer = main.querySelector('#chat-input-area');
    if (!intro || !composer) return;
    const span = Math.round(composer.getBoundingClientRect().bottom
        - intro.getBoundingClientRect().top);
    if (span > 0) main.style.setProperty('--home-hero-h', `${span}px`);
}
// Watch the hero itself so every cause of a height change is covered — the brand
// line appearing, a late font swap, or the copy rewrapping — instead of hooking
// each path that can repaint the brand. Only the intro is observed: the composer
// grows as the user types, and re-centring then would move the box under the
// caret. A rebind is needed because the welcome screen is rebuilt on re-render.
let homeHeroWatcher = null;
let homeHeroWatched = null;
function watchHomeHero(main) {
    if (typeof ResizeObserver === 'undefined') return;
    const intro = main.classList.contains('chat-home') ? main.querySelector('.home-intro') : null;
    if (!intro || intro === homeHeroWatched) return;
    if (homeHeroWatcher) homeHeroWatcher.disconnect();
    homeHeroWatched = intro;
    homeHeroWatcher = new ResizeObserver(syncHomeHeroOffset);
    homeHeroWatcher.observe(intro);
}
function bindWelcomeSuggestions(root) {
    root.querySelectorAll('.example-card').forEach(card => {
        card.addEventListener('click', () => {
            chatInput.value = t(card.dataset.promptKey);
            chatInput.dispatchEvent(new Event('input'));
            chatInput.focus();
        });
    });
}
// Introductory copy belongs to the empty welcome screen, not the transcript.
// Painting it never sends a message or starts an Agent run.
function paintWelcomeAgentIntro() {
    const welcome = document.getElementById('welcome-screen');
    if (!welcome || codingPaneMounted()) return;
    const heading = welcome.querySelector('.home-greeting');
    const description = welcome.querySelector('#welcome-subtitle');
    if (!heading || !description) return;
    // Prefer the use-range record refreshed by Start Chat over management data.
    const agent = (chatAgentCatalog || []).find(a => a.id === activeAgentId) || findAgent(activeAgentId);
    const ordinary = agent && agent.agent_type !== 'coding';
    const text = value => typeof value === 'string' ? value.trim() : '';
    heading.textContent = ordinary
        ? t('home_agent_greeting').replace('{name}', () => agent.name || agent.id)
        : t('home_greeting');
    description.textContent = ordinary
        ? text(agent.greeting) || text(agent.description) || t('home_agent_description')
        : t('home_description');
}

function renderWelcomeScreen() {
    const welcome = document.createElement('div');
    welcome.id = 'welcome-screen';
    welcome.className = 'workbench-welcome';
    welcome.innerHTML = welcomeTemplateHTML;
    welcome.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
    messagesDiv.appendChild(welcome);
    bindWelcomeSuggestions(welcome);
    applyBrandToDocument();
    paintWelcomeAgentIntro();
    syncChatHomeLayout();
    document.getElementById('chat-main').scrollTop = 0;
}
if (typeof MutationObserver !== 'undefined') new MutationObserver(syncChatHomeLayout).observe(messagesDiv, { childList: true });
syncChatHomeLayout();
// The console stays hidden behind the auth gate until the session resolves, so
// the first measurement lands while nothing is laid out and has to be skipped.
// The gate lifts by toggling #app's class, so watch that attribute: mutation
// callbacks are queued on the microtask queue and therefore still run when the
// page is not being painted, unlike requestAnimationFrame or a ResizeObserver.
// Resizing is measured synchronously for the same reason.
window.addEventListener('resize', syncHomeHeroOffset);
const homeHeroGateEl = document.getElementById('app');
if (homeHeroGateEl && typeof MutationObserver !== 'undefined') {
    new MutationObserver(syncHomeHeroOffset).observe(homeHeroGateEl, { attributes: true, attributeFilter: ['class'] });
}
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
function resetComposerHeight() {
    chatInput.style.height = COMPOSER_MIN_H + 'px';
    chatInput.style.overflowY = 'hidden';
}

// ---------------- Mic button: in-page voice input via the configured ASR provider ----------------
(function setupMicButton() {
    const micBtn = document.getElementById('mic-btn');
    if (!micBtn) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia ||
        typeof window.MediaRecorder === 'undefined') {
        micBtn.style.display = 'none';
        return;
    }

    let mediaRecorder = null;
    let stream = null;
    let chunks = [];
    let recording = false;

    // Use the custom CSS tooltip (data-tooltip) instead of the native title:
    // native title has a ~1.5s hover delay and is not i18n-aware.
    const setTip = (text) => {
        micBtn.setAttribute('data-tooltip', text);
        micBtn.removeAttribute('title');
    };

    const setIdle = () => {
        recording = false;
        micBtn.classList.remove('text-red-500', 'animate-pulse');
        micBtn.classList.add('text-slate-400');
        micBtn.querySelector('i').className = 'fas fa-microphone text-sm';
        setTip(t('mic_idle_title'));
    };
    const setRecording = () => {
        recording = true;
        micBtn.classList.remove('text-slate-400');
        micBtn.classList.add('text-red-500', 'animate-pulse');
        micBtn.querySelector('i').className = 'fas fa-stop text-sm';
        setTip(t('mic_recording_title'));
    };
    const setBusy = () => {
        micBtn.classList.remove('text-red-500', 'animate-pulse', 'text-slate-400');
        micBtn.classList.add('text-primary-500');
        micBtn.querySelector('i').className = 'fas fa-spinner fa-spin text-sm';
        setTip(t('mic_busy_title'));
    };

    const pickMimeType = () => {
        const candidates = [
            'audio/webm;codecs=opus',
            'audio/webm',
            'audio/ogg;codecs=opus',
            'audio/mp4',
        ];
        for (const m of candidates) {
            if (window.MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)) {
                return m;
            }
        }
        return '';
    };

    const stopStream = () => {
        if (stream) {
            stream.getTracks().forEach(t => t.stop());
            stream = null;
        }
    };

    let _micTipTimer = null;
    const flashError = (msg) => {
        console.warn('[mic]', msg);
        // Pop a small bubble above the mic so the user actually notices it.
        // The mic lives inside a relatively-positioned wrapper around the
        // textarea (see chat.html), so we hang the tip off that wrapper.
        const wrapper = micBtn.parentElement;
        if (!wrapper) return;
        let tip = wrapper.querySelector('.mic-tip');
        if (!tip) {
            tip = document.createElement('div');
            tip.className = 'mic-tip absolute right-1 bottom-full mb-2 px-2 py-1 rounded-md '
                + 'text-xs text-white bg-slate-800/90 dark:bg-slate-700/90 shadow-md '
                + 'pointer-events-none whitespace-nowrap z-10';
            wrapper.appendChild(tip);
        }
        tip.textContent = msg;
        tip.style.opacity = '1';
        if (_micTipTimer) clearTimeout(_micTipTimer);
        _micTipTimer = setTimeout(() => {
            tip.style.opacity = '0';
            tip.style.transition = 'opacity 200ms';
            setTimeout(() => tip.remove(), 250);
        }, 2000);
    };

    const upload = async (blob, ext) => {
        setBusy();
        const fd = new FormData();
        fd.append('file', blob, `recording.${ext}`);
        try {
            const resp = await fetch('/api/voice/asr', { method: 'POST', body: fd });
            const data = await resp.json();
            if (data.status === 'success' && data.text) {
                // Voice-message UX: drop the recording into the conversation
                // as a playable bubble with the caption underneath, then
                // dispatch the recognised text through the regular send path.
                sendVoiceMessage(data.text, data.audio_url);
            } else {
                flashError(data.message || t('mic_error'));
            }
        } catch (e) {
            flashError(t('mic_error') + ': ' + e.message);
        } finally {
            setIdle();
        }
    };

    const start = async () => {
        try {
            stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        } catch (e) {
            flashError(t('mic_permission_denied'));
            return;
        }
        chunks = [];
        const mimeType = pickMimeType();
        try {
            mediaRecorder = mimeType
                ? new MediaRecorder(stream, { mimeType })
                : new MediaRecorder(stream);
        } catch (e) {
            stopStream();
            flashError(t('mic_error') + ': ' + e.message);
            return;
        }
        mediaRecorder.ondataavailable = (ev) => {
            if (ev.data && ev.data.size > 0) chunks.push(ev.data);
        };
        mediaRecorder.onstop = () => {
            stopStream();
            const blob = new Blob(chunks, { type: mediaRecorder.mimeType || 'audio/webm' });
            // Map mime -> extension so the server picks the right file suffix.
            const mt = (mediaRecorder.mimeType || 'audio/webm').split(';')[0];
            const extMap = {
                'audio/webm': 'webm', 'audio/ogg': 'ogg',
                'audio/mp4': 'm4a',   'audio/mpeg': 'mp3',
            };
            const ext = extMap[mt] || 'webm';
            // 256 bytes ~ container header only, no actual audio. Anything
            // below that we treat as "tapped by mistake".
            if (blob.size < 256) {
                setIdle();
                flashError(t('mic_too_short'));
                return;
            }
            upload(blob, ext);
        };
        // timeslice=250ms: force the recorder to flush a chunk every 250ms.
        // Without it some browsers wait for stop() before producing any data,
        // which loses the audio on very short taps.
        mediaRecorder.start(250);
        recordStartedAt = Date.now();
        setRecording();
    };

    let recordStartedAt = 0;

    const stopWithMinDuration = () => {
        const elapsed = Date.now() - recordStartedAt;
        const minMs = 350;
        if (elapsed < minMs) {
            // Give the recorder a moment to capture at least one chunk
            // before we tell it to stop.
            setTimeout(() => stop(), minMs - elapsed);
        } else {
            stop();
        }
    };

    const stop = () => {
        if (mediaRecorder && mediaRecorder.state !== 'inactive') {
            mediaRecorder.stop();
        }
    };

    micBtn.addEventListener('click', () => {
        if (recording) {
            stopWithMinDuration();
        } else {
            start();
        }
    });

    setIdle();
})();

// ---------------- Optimize button: prompt optimization via AI ----------------
(function setupOptimizeButton() {
    const optBtn = document.getElementById('optimize-btn');
    if (!optBtn) return;

    let busy = false;

    // Use the custom CSS tooltip (data-tooltip) instead of the native title:
    // native title has a ~1.5s hover delay and is not i18n-aware.
    const setTip = (text) => {
        optBtn.setAttribute('data-tooltip', text);
        optBtn.removeAttribute('title');
    };

    const setIdle = () => {
        busy = false;
        optBtn.classList.remove('text-primary-500', 'animate-spin');
        optBtn.classList.add('text-slate-400');
        optBtn.querySelector('i').className = 'fas fa-magic text-[13px]';
        setTip(t('optimize_idle_title'));
        optBtn.style.pointerEvents = '';
    };
    const setBusy = () => {
        busy = true;
        optBtn.classList.remove('text-slate-400');
        optBtn.classList.add('text-primary-500');
        optBtn.querySelector('i').className = 'fas fa-spinner fa-spin text-[13px]';
        setTip(t('optimize_busy_title'));
        optBtn.style.pointerEvents = 'none';
    };

    // Shared flashError from mic setup — reuse its style by injecting into the same wrapper
    const flashError = (msg) => {
        console.warn('[optimize]', msg);
        const wrapper = optBtn.parentElement;
        if (!wrapper) return;
        let tip = wrapper.querySelector('.opt-tip');
        if (!tip) {
            tip = document.createElement('div');
            tip.className = 'opt-tip absolute right-9 bottom-full mb-2 px-2 py-1 rounded-md '
                + 'text-xs text-white bg-slate-800/90 dark:bg-slate-700/90 shadow-md '
                + 'pointer-events-none whitespace-nowrap z-10';
            wrapper.appendChild(tip);
        }
        tip.textContent = msg;
        tip.style.opacity = '1';
        tip.style.transition = '';
        clearTimeout(tip._timer);
        tip._timer = setTimeout(() => {
            tip.style.transition = 'opacity 200ms';
            tip.style.opacity = '0';
        }, 2500);
    };

    optBtn.addEventListener('click', async () => {
        if (busy) return;
        const raw = chatInput.value.trim();
        if (!raw) {
            flashError(t('optimize_empty'));
            return;
        }
        setBusy();
        try {
            // Gather optional context: last few message groups visible in the chat.
            // User and bot messages are distinguished by their group class.
            const contextMessages = [];
            const groups = messagesDiv.querySelectorAll('.user-message-group, .bot-message-group');
            const recentGroups = Array.from(groups).slice(-6);
            for (const g of recentGroups) {
                const role = g.classList.contains('user-message-group') ? 'user' : 'assistant';
                // Only read the main message content, not action buttons or timestamps.
                const contentEl = g.querySelector('.msg-content');
                const text = ((contentEl || g).textContent || '').trim().slice(0, 200);
                if (text) {
                    contextMessages.push({ role: role, content: text });
                }
            }

            const resp = await fetch('/api/prompt/optimize', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ input: raw, context_messages: contextMessages }),
            });
            const data = await resp.json();
            if (data.status === 'success' && data.optimized) {
                chatInput.value = data.optimized;
                chatInput.dispatchEvent(new Event('input', { bubbles: true }));
                chatInput.focus();
                // Place cursor at end
                chatInput.setSelectionRange(chatInput.value.length, chatInput.value.length);
            } else {
                flashError(data.message || t('optimize_error'));
            }
        } catch (e) {
            flashError(t('optimize_error') + ': ' + e.message);
        } finally {
            setIdle();
        }
    });

    setIdle();
})();


// Smart auto-scroll: pause when user scrolls up, resume when near bottom
let _autoScrollEnabled = true;
const _SCROLL_THRESHOLD = 80; // px from bottom to re-enable auto-scroll

messagesDiv.addEventListener('scroll', () => {
    const distFromBottom = messagesDiv.scrollHeight - messagesDiv.scrollTop - messagesDiv.clientHeight;
    _autoScrollEnabled = distFromBottom <= _SCROLL_THRESHOLD;
    _updateScrollToBottomBtn();
});

// Intercept internal navigation links in chat messages
messagesDiv.addEventListener('click', (e) => {
    // Code block copy button
    const codeCopyBtn = e.target.closest('.code-copy-btn');
    if (codeCopyBtn) {
        e.preventDefault();
        const wrapper = codeCopyBtn.closest('.code-block-wrapper');
        const codeEl = wrapper && wrapper.querySelector('pre code');
        if (codeEl) {
            const codeText = codeEl.textContent;
            copyToClipboard(codeText).then(() => {
                const icon = codeCopyBtn.querySelector('i');
                if (icon) { icon.className = 'fas fa-check'; setTimeout(() => { icon.className = 'fas fa-copy'; }, 1500); }
            });
        }
        return;
    }

    const copyBtn = e.target.closest('.copy-msg-btn');
    if (copyBtn) {
        e.preventDefault();
        const msgRoot = copyBtn.closest('.flex.gap-3');
        const answerEl = msgRoot && msgRoot.querySelector('.answer-content');
        const rawMd = answerEl && answerEl.dataset.rawMd;
        if (rawMd) {
            copyToClipboard(rawMd).then(() => {
                const icon = copyBtn.querySelector('i');
                if (icon) { icon.className = 'fas fa-check'; setTimeout(() => { icon.className = 'fas fa-copy'; }, 1500); }
            });
        }
        return;
    }

    // Edit user message
    const editBtn = e.target.closest('.edit-msg-btn');
    if (editBtn) {
        e.preventDefault();
        if (isCurrentSessionConversationActive()) return;
        const msgRoot = editBtn.closest('.user-message-group');
        if (msgRoot) editUserMessage(msgRoot);
        return;
    }

    // Regenerate bot response
    const regenerateBtn = e.target.closest('.regenerate-msg-btn');
    if (regenerateBtn) {
        e.preventDefault();
        const botMsgRoot = regenerateBtn.closest('.flex.gap-3');
        if (botMsgRoot) regenerateResponse(botMsgRoot);
        return;
    }

    // Delete message (user bubble only; bot bubbles intentionally lack a
    // delete button — removing only the bot reply would leave an orphan
    // user message that breaks LLM context alternation).
    const deleteBtn = e.target.closest('.delete-msg-btn');
    if (deleteBtn) {
        e.preventDefault();
        if (isCurrentSessionConversationActive()) return;
        const userMsgEl = deleteBtn.closest('.user-message-group');
        if (!userMsgEl) return;

        showConfirmModal(t('delete_message_title'), t('delete_message_confirm'), () => {
            // Find the next bot reply for this turn (skip non-message nodes).
            let botReplyEl = null;
            let sibling = userMsgEl.nextElementSibling;
            while (sibling) {
                if (sibling.classList && sibling.classList.contains('bot-message-group')) {
                    botReplyEl = sibling;
                    break;
                }
                sibling = sibling.nextElementSibling;
            }
            userMsgEl.remove();
            if (botReplyEl) botReplyEl.remove();

            const userSeq = userMsgEl.dataset.seq;
            if (userSeq) {
                fetch('/api/messages/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ session_id: sessionId, user_seq: parseInt(userSeq) })
                }).then(r => r.json()).then(data => {
                    if (data.status === 'success') console.log(`Deleted ${data.deleted} messages`);
                }).catch(err => console.error('Failed to delete:', err));
            }
        });
        return;
    }

    const a = e.target.closest('a');
    if (!a) return;
    const href = a.getAttribute('href') || '';
    if (href === '/memory/dreams') {
        e.preventDefault();
        navigateTo('memory');
        setTimeout(() => switchMemoryTab('dreams'), 50);
    } else if (href === '/memory/MEMORY.md') {
        e.preventDefault();
        navigateTo('memory');
        setTimeout(() => { switchMemoryTab('files'); openMemoryFile('MEMORY.md', 'memory'); }, 50);
    }
});
const attachmentPreview = document.getElementById('attachment-preview');

// Pending attachments: [{file_path, file_name, file_type, preview_url}]
// Items with _uploading=true are still in flight.
let pendingAttachments = [];
let uploadingCount = 0;

// Input history (like terminal arrow-key recall)
const inputHistory = [];
let historyIdx = -1;
let historySavedDraft = '';

// While an SSE stream is in flight, the send button morphs into a cancel
// button. Only one in-flight request is supported at a time.
let activeRequestId = null;
let sendBtnMode = 'send'; // 'send' | 'cancel'

function setSendBtnCancelMode(requestId) {
    activeRequestId = requestId;
    sendBtnMode = 'cancel';
    sendBtn.disabled = false;
    sendBtn.classList.add('send-btn-cancel');
    _setBtnTooltip(sendBtn, t('tip_cancel'));
    sendBtn.innerHTML = '<i class="fas fa-stop text-sm"></i>';
    updateSteerBtnState();
}

function resetSendBtnSendMode() {
    activeRequestId = null;
    sendBtnMode = 'send';
    sendBtn.classList.remove('send-btn-cancel');
    _setBtnTooltip(sendBtn, '');
    sendBtn.innerHTML = '<i class="fas fa-paper-plane text-sm"></i>';
    steerBtn.classList.add('hidden');
    steerBtn.classList.remove('flex');
    steerBtn.disabled = true;
    updateSendBtnState();
}

function updateSteerBtnState() {
    // Keep the steer button enabled whenever a task is running so users can
    // fire successive guidance. Empty-input is guarded in steerActiveTask,
    // avoiding a jarring disabled/not-allowed state right after each steer.
    const active = sendBtnMode === 'cancel' && !!activeRequestId;
    steerBtn.classList.toggle('hidden', !active);
    steerBtn.classList.toggle('flex', active);
    steerBtn.disabled = !active || uploadingCount > 0;
}

function steerActiveTask() {
    const instruction = chatInput.value.trim();
    if (!instruction || sendBtnMode !== 'cancel' || !activeRequestId) return;

    inputHistory.push(instruction);
    historyIdx = -1;
    historySavedDraft = '';
    addUserMessage(`↪ ${instruction}`, new Date());

    chatInput.value = '';
    resetComposerHeight();
    updateSteerBtnState();

    fetch('/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            session_id: sessionId,
            message: instruction,
            steer: true,
            stream: false,
            lang: currentLang,
        }),
    })
    .then(readMessageResponse)
    .then(data => {
        if (data.status === 'success' && data.inline_reply) {
            addBotMessage(data.inline_reply, new Date());
        } else {
            addMessageError(data);
        }
    })
    .catch(err => {
        console.warn('[steer] request failed', err);
        addBotMessage(t('error_send'), new Date());
    })
    .finally(updateSteerBtnState);
}

steerBtn.addEventListener('click', steerActiveTask);

function requestCancel() {
    const reqId = activeRequestId;
    if (!reqId) return;
    fetch('/cancel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ request_id: reqId, session_id: sessionId, lang: currentLang }),
    }).catch(err => {
        console.warn('[cancel] request failed', err);
    });
    // Optimistic UI lock so the click visibly registers before the SSE
    // "cancelled" event arrives.
    sendBtn.disabled = true;
    _setBtnTooltip(sendBtn, t('tip_cancelled'));
}

// Button click is the only path to Cancel. Pressing Enter still calls
// sendMessage() so users can submit "/cancel" as a regular slash command.
sendBtn.addEventListener('click', () => {
    if (sendBtnMode === 'cancel') {
        requestCancel();
    } else {
        sendMessage();
    }
});

function updateSendBtnState() {
    if (sendBtnMode === 'cancel') {
        // Self-heal a stuck Cancel button: if there's no live stream backing
        // the current request, the cancel state leaked (e.g. a stream ended
        // without resetting). Recover to Send so the input isn't blocked.
        if (!activeRequestId || !activeStreams[activeRequestId]) {
            resetSendBtnSendMode();
        } else {
            // Don't downgrade a genuinely active Cancel button on input edits.
            updateSteerBtnState();
            return;
        }
    }
    sendBtn.disabled = uploadingCount > 0 || (!chatInput.value.trim() && pendingAttachments.length === 0);
    updateSteerBtnState();
}

function renderAttachmentPreview() {
    if (pendingAttachments.length === 0) {
        attachmentPreview.classList.add('hidden');
        attachmentPreview.innerHTML = '';
        updateSendBtnState();
        return;
    }
    attachmentPreview.classList.remove('hidden');
    attachmentPreview.innerHTML = pendingAttachments.map((att, idx) => {
        if (att._uploading) {
            const suffix = att.file_type === 'directory' && att.file_count
                ? ` (${att.file_count})`
                : '';
            return `<div class="att-chip att-uploading" data-idx="${idx}">
                <i class="fas fa-spinner fa-spin"></i>
                <span class="att-name">${escapeHtml(att.file_name)}${suffix}</span>
            </div>`;
        }
        if (att.file_type === 'image') {
            return `<div class="att-thumb" data-idx="${idx}">
                <img src="${att.preview_url}" alt="${escapeHtml(att.file_name)}">
                <button class="att-remove" onclick="removeAttachment(${idx})">&times;</button>
            </div>`;
        }
        const icon = att.file_type === 'video'
            ? 'fa-film'
            : (att.file_type === 'directory' ? 'fa-folder-tree'
            : (att.is_dir ? 'fa-folder' : 'fa-file-alt'));
        const suffix = att.file_type === 'directory' && att.file_count
            ? ` (${att.file_count})`
            : '';
        return `<div class="att-chip" data-idx="${idx}">
            <i class="fas ${icon}"></i>
            <span class="att-name">${escapeHtml(att.file_name)}${suffix}</span>
            <button class="att-remove" onclick="removeAttachment(${idx})">&times;</button>
        </div>`;
    }).join('');
    updateSendBtnState();
}

function removeAttachment(idx) {
    if (pendingAttachments[idx]?._uploading) return;
    pendingAttachments.splice(idx, 1);
    renderAttachmentPreview();
}

function isAttachMenuVisible() {
    return attachMenu && !attachMenu.classList.contains('hidden');
}

function hideAttachMenu() {
    if (attachMenu) attachMenu.classList.add('hidden');
}

function toggleAttachMenu(event) {
    if (!attachMenu) return;
    if (event) {
        event.preventDefault();
        event.stopPropagation();
    }
    attachMenu.classList.toggle('hidden');
}

function triggerFileUpload() {
    hideAttachMenu();
    fileInput?.click();
}

function triggerFolderUpload() {
    if (!supportsDirectoryUpload) return;
    hideAttachMenu();
    folderInput?.click();
}

async function handleFileSelect(files) {
    if (!files || files.length === 0) return;
    const tasks = [];
    for (const file of files) {
        const placeholder = { file_name: file.name, file_type: 'file', _uploading: true };
        pendingAttachments.push(placeholder);
        uploadingCount++;
        renderAttachmentPreview();

        tasks.push((async () => {
            const formData = new FormData();
            formData.append('file', file);
            formData.append('session_id', sessionId);
            try {
                const resp = await fetch('/upload', { method: 'POST', body: formData });
                const data = await resp.json();
                if (data.status === 'success') {
                    placeholder.file_path = data.file_path;
                    placeholder.file_name = data.file_name;
                    placeholder.file_type = data.file_type;
                    placeholder.preview_url = data.preview_url;
                    delete placeholder._uploading;
                } else {
                    const i = pendingAttachments.indexOf(placeholder);
                    if (i !== -1) pendingAttachments.splice(i, 1);
                }
            } catch (e) {
                console.error('Upload failed:', e);
                const i = pendingAttachments.indexOf(placeholder);
                if (i !== -1) pendingAttachments.splice(i, 1);
            }
            uploadingCount--;
            renderAttachmentPreview();
        })());
    }
    await Promise.all(tasks);
}

function _makeUploadId() {
    return `dir_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
}

function _groupDirectoryFiles(files) {
    const groups = new Map();
    for (const file of Array.from(files || [])) {
        const relPath = file.webkitRelativePath || file.name;
        const parts = relPath.split('/').filter(Boolean);
        const rootName = parts[0] || file.name;
        if (!groups.has(rootName)) groups.set(rootName, []);
        groups.get(rootName).push({ file, relPath });
    }
    return groups;
}

async function handleFolderSelect(files) {
    if (!files || files.length === 0) return;
    const groups = _groupDirectoryFiles(files);
    const groupTasks = [];

    for (const [rootName, entries] of groups.entries()) {
        const placeholder = {
            file_name: rootName,
            file_type: 'directory',
            file_count: entries.length,
            _uploading: true,
        };
        pendingAttachments.push(placeholder);
        uploadingCount++;
        renderAttachmentPreview();

        const uploadId = _makeUploadId();
        groupTasks.push((async () => {
            try {
                const formData = new FormData();
                formData.append('session_id', sessionId);
                formData.append('upload_id', uploadId);
                for (const { file, relPath } of entries) {
                    formData.append('files', file);
                    formData.append('relative_paths', relPath);
                }

                const resp = await fetch('/upload', { method: 'POST', body: formData });
                const data = await resp.json();
                if (data.status !== 'success') {
                    throw new Error(data.message || 'Upload failed');
                }
                if (!data.root_path) {
                    throw new Error('Directory root path missing');
                }
                placeholder.file_path = data.root_path;
                placeholder.file_name = data.root_name || rootName;
                delete placeholder._uploading;
            } catch (e) {
                console.error('Directory upload failed:', e);
                const i = pendingAttachments.indexOf(placeholder);
                if (i !== -1) pendingAttachments.splice(i, 1);
            } finally {
                uploadingCount--;
            }
            renderAttachmentPreview();
        })());
    }

    await Promise.all(groupTasks);
}

fileInput.addEventListener('change', function() {
    handleFileSelect(this.files);
    this.value = '';
});

folderInput.addEventListener('change', function() {
    handleFolderSelect(this.files);
    this.value = '';
});

document.addEventListener('click', (e) => {
    if (!isAttachMenuVisible()) return;
    if (attachMenu.contains(e.target) || attachBtn.contains(e.target)) return;
    hideAttachMenu();
});

// =====================================================================
// Workspace selector (project picker above the input)
// =====================================================================
let _wsSelState = { current: null, recents: [], defaultWorkspace: '', projectsRoot: '' };

function _wsSelBtn() { return document.getElementById('workspace-selector-btn'); }
function _wsSelMenu() { return document.getElementById('workspace-selector-menu'); }

// Minimal self-dismissing toast for selector errors (no global toast exists).
function _wsToast(msg) {
    let el = document.getElementById('ws-sel-toast');
    if (!el) {
        el = document.createElement('div');
        el.id = 'ws-sel-toast';
        el.style.cssText = 'position:fixed;bottom:80px;left:50%;transform:translateX(-50%);' +
            'background:#1e293b;color:#fff;padding:8px 14px;border-radius:8px;font-size:13px;' +
            'z-index:9999;box-shadow:0 4px 16px rgba(0,0,0,0.2);opacity:0;transition:opacity .2s;';
        document.body.appendChild(el);
    }
    el.textContent = msg;
    el.style.opacity = '1';
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.style.opacity = '0'; }, 2600);
}

// Refresh the selector state + label for the current session.
async function refreshWorkspaceSelector() {
    const label = document.getElementById('workspace-selector-label');
    const requestSession = sessionId;
    const requestAgent = activeAgentId;
    try {
        // Scope the request to the active Agent so the default-workspace hint
        // matches the file panel's real root in multi-Agent setups.
        let url = `/api/projects?session=${encodeURIComponent(sessionId)}`;
        const aid = (typeof activeAgentId !== 'undefined') ? activeAgentId : '';
        if (aid) url += `&agent=${encodeURIComponent(aid)}`;
        const res = await fetch(url);
        const data = await res.json();
        if (sessionId !== requestSession || activeAgentId !== requestAgent) return;
        if (data.status !== 'success') return;
        _wsSelState = {
            current: data.current || null,
            recents: data.recents || [],
            defaultWorkspace: data.default_workspace || '',
            projectsRoot: data.projects_root || '',
        };
        _wsSelUpdateLabel();
    } catch (e) { /* keep last label */ }
}

// Sync the selector button's label and hover tooltip with the current state.
// Called after every selection so the tooltip always shows the live full path.
function _wsSelUpdateLabel() {
    const label = document.getElementById('workspace-selector-label');
    if (label) {
        label.textContent = _wsSelState.current
            ? _wsSelState.current.name
            : t('ws_default_workspace');
    }
    const btn = _wsSelBtn();
    if (btn) {
        // The default workspace is the resting state, so it collapses to just
        // the folder icon (matching the desktop composer); a picked workspace
        // shows its name so the user knows they've moved off the default.
        btn.classList.toggle('composer-chip-icon-only', !_wsSelState.current);
        const full = _wsSelState.current
            ? _wsSelState.current.path
            : _wsSelState.defaultWorkspace;
        btn.setAttribute('data-tooltip', full || t('ws_sel_title'));
        btn.setAttribute('data-tooltip-pos', 'top');
        // Route through the body-level floating tooltip so the full path isn't
        // clipped/covered by the chat history above the input bar.
        btn.setAttribute('data-tip-float', '');
    }
}

function toggleWorkspaceSelector(event) {
    if (event) { event.preventDefault(); event.stopPropagation(); }
    const menu = _wsSelMenu();
    if (!menu) return;
    if (!menu.classList.contains('hidden')) {
        _wsSelHide();
        return;
    }
    _closeComposerMenus(menu);
    refreshWorkspaceSelector().then(renderWorkspaceSelectorMenu);
    menu.classList.remove('hidden');
    _wsSelBtn()?.classList.add('open');
}

function _wsSelHide() {
    const menu = _wsSelMenu();
    if (menu) menu.classList.add('hidden');
    _wsSelBtn()?.classList.remove('open');
}

function renderWorkspaceSelectorMenu() {
    const menu = _wsSelMenu();
    if (!menu) return;

    const parts = [];
    const isDefault = !_wsSelState.current;
    parts.push(`<div class="ws-sel-section-title">${escapeHtml(t('ws_sel_title'))}</div>`);
    // Default workspace: hovering shows the full ~/cow absolute path.
    parts.push(`
        <button class="ws-sel-item ${isDefault ? 'active' : ''}" onclick="selectWorkspaceProject(null)"
                data-tip-float data-tooltip="${escapeHtml(_wsSelState.defaultWorkspace || '')}" data-tooltip-pos="bottom">
            <i class="fas fa-house"></i>
            <span class="ws-sel-name">${escapeHtml(t('ws_default_workspace'))}</span>
            ${isDefault ? '<i class="fas fa-check ws-sel-check"></i>' : ''}
        </button>`);

    if ((_wsSelState.recents || []).length) {
        parts.push(`<div class="ws-sel-divider"></div>`);
        parts.push(`<div class="ws-sel-section-title">${escapeHtml(t('ws_sel_recents'))}</div>`);
        _wsSelState.recents.forEach(r => {
            const active = _wsSelState.current && _wsSelState.current.path === r.path;
            parts.push(`
                <button class="ws-sel-item ${active ? 'active' : ''}" onclick="selectWorkspaceProject('${_wsAttr(r.path)}')"
                        data-tip-float data-tooltip="${escapeHtml(r.path)}" data-tooltip-pos="bottom">
                    <i class="fas fa-folder"></i>
                    <span class="ws-sel-name">${escapeHtml(r.name)}</span>
                    ${active ? '<i class="fas fa-check ws-sel-check"></i>' : ''}
                </button>`);
        });
    }

    parts.push(`<div class="ws-sel-divider"></div>`);
    // Host-filesystem folder picker stays closed — only new-project / recents /
    // default space remain.
    parts.push(`
        <button class="ws-sel-item" onclick="wsSelNewProjectDialog()">
            <i class="fas fa-folder-plus"></i>
            <span class="ws-sel-name">${escapeHtml(t('ws_sel_new'))}</span>
        </button>`);

    menu.innerHTML = parts.join('');
}

// Escape a path for safe embedding inside a single-quoted inline handler.
function _wsAttr(p) { return String(p || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'"); }

// ------- Folder picker modal (open an existing project) -------
let _fpCurrent = '';   // absolute path currently listed
let _fpBound = false;  // one-time listener binding guard

function wsSelOpenProjectDialog() {
    _wsSelHide();
    _fpBindOnce();
    const overlay = document.getElementById('folder-picker-overlay');
    document.getElementById('folder-picker-cancel').textContent = t('channels_cancel') || t('ws_sel_up');
    document.getElementById('folder-picker-open').textContent = t('ws_sel_open_here');
    document.getElementById('folder-picker-hint').textContent = t('ws_sel_dblclick_hint');
    overlay.classList.remove('hidden');
    _fpBrowse('');  // '' => backend starts at ~
}

function _fpBindOnce() {
    if (_fpBound) return;
    _fpBound = true;
    const overlay = document.getElementById('folder-picker-overlay');
    const close = () => overlay.classList.add('hidden');
    document.getElementById('folder-picker-cancel').addEventListener('click', close);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    document.getElementById('folder-picker-open').addEventListener('click', async () => {
        if (!_fpCurrent) return;
        const ok = await _wsSelApply('/api/projects/select', { session: sessionId, project_dir: _fpCurrent });
        if (ok) close();
    });
}

// Virtual path (Windows) that lists logical drives; not a real openable dir.
const _FP_DRIVES = '__DRIVES__';

async function _fpBrowse(path) {
    const list = document.getElementById('folder-picker-list');
    list.innerHTML = `<div class="fp-empty"><i class="fas fa-spinner fa-spin"></i></div>`;
    try {
        const res = await fetch(`/api/projects/browse?path=${encodeURIComponent(path || '')}`);
        const data = await res.json();
        if (data.status !== 'success') { list.innerHTML = `<div class="fp-empty">${escapeHtml(data.message || 'error')}</div>`; return; }
        const isDrives = data.path === _FP_DRIVES;
        _fpCurrent = isDrives ? null : data.path;
        // Drives view is a selector, not a real directory: show a label and
        // disable "Open here" so the sentinel can't be picked as a project.
        const label = isDrives ? (t('ws_sel_drives') || 'This PC') : data.path;
        document.getElementById('folder-picker-path').textContent = label;
        document.getElementById('folder-picker-path').setAttribute('title', label);
        document.getElementById('folder-picker-open').disabled = isDrives;
        _fpRenderToolbar(data);
        _fpRenderList(data);
    } catch (e) {
        list.innerHTML = `<div class="fp-empty">${escapeHtml(String(e.message || e))}</div>`;
    }
}

function _fpRenderToolbar(data) {
    const bar = document.getElementById('folder-picker-toolbar');
    const upDisabled = !data.parent;
    bar.innerHTML = `
        <button class="fp-btn" ${upDisabled ? 'disabled' : ''} onclick="_fpBrowse('${_wsAttr(data.parent || '')}')" data-tooltip="${escapeHtml(t('ws_sel_up'))}" data-tooltip-pos="bottom">
            <i class="fas fa-arrow-up"></i>
        </button>
        <button class="fp-btn" onclick="_fpBrowse('~')" data-tooltip="~" data-tooltip-pos="bottom">
            <i class="fas fa-house"></i>
        </button>`;
}

function _fpRenderList(data) {
    const list = document.getElementById('folder-picker-list');
    const dirs = data.dirs || [];
    if (!dirs.length) {
        list.innerHTML = `<div class="fp-empty"><i class="fas fa-folder-open"></i><span>${escapeHtml(t('ws_sel_no_subdirs'))}</span></div>`;
        return;
    }
    list.innerHTML = dirs.map(d => `
        <div class="fp-row" ondblclick="_fpBrowse('${_wsAttr(d.path)}')" onclick="_fpSelectRow(this,'${_wsAttr(d.path)}')" title="${escapeHtml(d.path)}">
            <i class="fas fa-folder"></i>
            <span class="fp-name">${escapeHtml(d.name)}</span>
            <i class="fas fa-chevron-right fp-into" onclick="event.stopPropagation();_fpBrowse('${_wsAttr(d.path)}')"></i>
        </div>`).join('');
}

// Single click selects a child folder as the target (so you can open a folder
// without navigating into it); double click / chevron navigates inside.
function _fpSelectRow(el, path) {
    document.querySelectorAll('#folder-picker-list .fp-row.selected').forEach(r => r.classList.remove('selected'));
    el.classList.add('selected');
    _fpCurrent = path;
    // Picking a row (e.g. a drive in the drives view) is a valid target again.
    document.getElementById('folder-picker-open').disabled = false;
    document.getElementById('folder-picker-path').textContent = path;
}

// Create a new project by name (lands under the projects root), then open it.
function wsSelNewProjectDialog() {
    _wsSelHide();
    openKnowledgeDialog({
        title: t('ws_sel_new'),
        subtitle: (t('ws_sel_new_subtitle') || '').replace('{root}', _wsSelState.projectsRoot || ''),
        label: t('ws_sel_new_placeholder'),
        hint: t('ws_sel_new_hint'),
        icon: 'fa-folder-plus',
        value: '',
        validate: (v) => {
            v = (v || '').trim();
            if (!v) return t('ws_sel_name_required');
            if (v.includes('/') || v.includes('\\')) return t('ws_sel_name_no_slash');
            return '';
        },
        onSubmit: async (name) => {
            const ok = await _wsSelApply('/api/projects/create', { session: sessionId, name: name.trim() });
            return ok ? true : null;
        },
    });
}

// Shared apply path for select/create: POST, update label, then reveal the
// project in the right-hand file panel so the user sees they are "inside" it.
async function _wsSelApply(url, body) {
    try {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.status !== 'success') { _wsToast(data.message || 'failed'); return false; }
        _wsSelState.current = data.current || null;
        if (Array.isArray(data.recents)) _wsSelState.recents = data.recents;
        if (data.default_workspace) _wsSelState.defaultWorkspace = data.default_workspace;
        _wsSelUpdateLabel();
        _wsSelRevealFiles();
        return true;
    } catch (e) { _wsToast(String(e.message || e)); return false; }
}

// Open (or refresh) the right-hand file panel on the Files tab so the newly
// selected project's directory is visible.
function _wsSelRevealFiles() {
    try {
        if (typeof openWorkspacePanel === 'function') {
            wsAutoOpenSuppressed = false;
            // Reset to the root of the new workspace before opening.
            if (typeof wsCurrentDir !== 'undefined') wsCurrentDir = '';
            openWorkspacePanel('files');
        }
        if (typeof refreshWorkspaceTree === 'function') refreshWorkspaceTree();
    } catch (e) { /* panel not present on this view */ }
}

// Kept for callers that select without a dialog (default / recents).
async function selectWorkspaceProject(projectDir) {
    _wsSelHide();
    await _wsSelApply('/api/projects/select', { session: sessionId, project_dir: projectDir });
}

document.addEventListener('click', (e) => {
    const menu = _wsSelMenu();
    const btn = _wsSelBtn();
    if (!menu || menu.classList.contains('hidden')) return;
    if (menu.contains(e.target) || (btn && btn.contains(e.target))) return;
    _wsSelHide();
});

// =====================================================================
// Per-session settings: permission mode and model
//
// Both live next to the workspace picker under the input, because all three
// answer the same question - what this conversation is allowed to do, and with
// what. Each falls back to the global setting until the user pins one here, so
// a session that was never touched keeps following Settings.
// =====================================================================

// Icons and i18n keys per mode. Ordered most-open first so the menu reads from
// "least restricted" downward, matching how the chip colours escalate.
const PERMISSION_META = {
    'full-access':     { icon: 'fa-lock-open',     key: 'perm_full_access' },
    'workspace-write': { icon: 'fa-shield-halved', key: 'perm_workspace_write' },
    'read-only':       { icon: 'fa-eye',           key: 'perm_read_only' },
};

// Last state from GET /api/sessions/<id>/settings; null until first fetch.
let _sessCfg = null;

function _modelBtn() { return document.getElementById('model-selector-btn'); }
function _modelMenu() { return document.getElementById('model-selector-menu'); }

function _permLabel(mode) { return t((PERMISSION_META[mode] || {}).key || 'perm_full_access'); }

/** Close every composer popover except `keep` (so one chip's menu replaces another's). */
function _closeComposerMenus(keep) {
    [[_wsSelMenu(), _wsSelBtn()], [_modelMenu(), _modelBtn()]]
        .forEach(([menu, btn]) => {
            if (!menu || menu === keep) return;
            menu.classList.add('hidden');
            if (btn) btn.classList.remove('open');
        });
    const agentMenu = document.getElementById('composer-agent-menu');
    if (agentMenu && agentMenu !== keep) agentMenu.classList.add('hidden');
}

// Fetch this session's effective model + permission and repaint both chips.
async function refreshSessionSettings() {
    const requestSession = sessionId;
    const requestAgent = activeAgentId;
    try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/settings`);
        const data = await res.json();
        if (sessionId !== requestSession || activeAgentId !== requestAgent) return;
        if (data.status !== 'success') return;
        _sessCfg = { model: data.model, team: data.team };
    } catch (e) {
        // Keep whatever the chips already show rather than blanking them.
        return;
    }
    _renderModelChip();
    renderComposerIdentity();
}

function _renderModelChip() {
    const btn = _modelBtn();
    if (!btn || !_sessCfg) return;
    // Once a conversation has more than one Agent there is no single model to
    // show: each answers on its own. Pinning one here would silently apply to
    // whoever happens to own the conversation.
    const shared = sharedConversation();
    btn.classList.toggle('hidden', shared);
    if (shared) {
        _modelMenu()?.classList.add('hidden');
        btn.classList.remove('open');
        return;
    }
    const state = _sessCfg.model || {};
    const model = state.model || '';

    const label = document.getElementById('model-selector-label');
    if (label) label.textContent = model || t('model_unset');

    const tip = t('model_tip').replace('{name}', model || t('model_unset'))
        + (state.source === 'global' ? ` · ${t('model_follow_global')}` : '')
        + (state.source === 'agent' ? ` · ${t('model_follow_agent')}` : '');
    btn.setAttribute('data-tooltip', tip);
    btn.setAttribute('data-tooltip-pos', 'top');
    btn.setAttribute('data-tip-float', '');
}

// Insert an explanatory hint after a tool card whose call was refused by the
// permission gate. The refusal reason already sits in the tool card; this is a
// short human sentence. There is deliberately no action button: execution is
// governed by the caller's role grants, not by a switch in the conversation.
function _appendPermissionDeniedHint(toolEl, mode, kind) {
    if (!toolEl || !toolEl.parentElement) return;
    // Avoid stacking duplicate hints if the model retries the same blocked call.
    if (toolEl.nextElementSibling
        && toolEl.nextElementSibling.classList
        && toolEl.nextElementSibling.classList.contains('perm-denied-hint')) {
        return;
    }
    // Only a legacy mode refusal names a mode; a role/isolation/quota refusal in
    // database mode must not blame a session mode the user cannot change. Name
    // the refusal's actual source where it is known: an isolation refusal is a
    // tenant boundary, not an authorization gap the caller's role can close.
    const text = (kind === 'mode' && mode)
        ? t('perm_denied_hint').replace('{name}', _permLabel(mode))
        : (kind === 'isolation'
            ? t('perm_denied_isolation_hint')
            : t('perm_denied_role_hint'));
    const hint = document.createElement('div');
    hint.className = 'perm-denied-hint';
    hint.innerHTML = `
        <i class="fas fa-shield-halved"></i>
        <span class="perm-denied-text">${escapeHtml(text)}</span>`;
    toolEl.parentElement.insertBefore(hint, toolEl.nextElementSibling);
}

function toggleModelSelector(event) {
    if (event) { event.preventDefault(); event.stopPropagation(); }
    const menu = _modelMenu();
    if (!menu) return;
    if (!menu.classList.contains('hidden')) {
        _closeComposerMenus();
        return;
    }
    _closeComposerMenus(menu);
    const open = () => { renderModelMenu(); menu.classList.remove('hidden'); _modelBtn()?.classList.add('open'); };
    // Always re-fetch: the catalog depends on which providers have keys, which
    // may have changed in Settings since this page loaded.
    refreshSessionSettings().then(() => { if (_sessCfg) open(); });
}

function renderModelMenu() {
    const menu = _modelMenu();
    if (!menu) return;
    const state = (_sessCfg && _sessCfg.model) || {};
    const providers = state.providers || [];
    const pinned = state.source === 'session';

    // Which model is currently effective (pinned or inherited from global), so
    // the check mark shows on it even when the session follows the global model.
    const activeModel = state.model || (state.global && state.global.model) || '';
    const activeProvider = state.provider || (state.global && state.global.provider) || '';

    const parts = [`<div class="composer-menu-title">${escapeHtml(t('model_menu_title'))}</div>`];
    providers.forEach((p, idx) => {
        if (idx > 0) parts.push('<div class="composer-menu-divider"></div>');
        parts.push(`<div class="composer-menu-title">${escapeHtml(localizedLabel(p.label))}</div>`);
        (p.models || []).forEach(m => {
            const active = m === activeModel && p.id === activeProvider;
            // Clicking the already-pinned model clears the pin (back to global);
            // "follow global" is no longer a separate row.
            const arg = (active && pinned)
                ? 'null, null'
                : `'${_wsAttr(p.id)}','${_wsAttr(m)}'`;
            parts.push(`
                <button class="composer-menu-item ${active ? 'active' : ''}"
                        onclick="selectSessionModel(${arg})">
                    <i class="fas fa-microchip"></i>
                    <span class="composer-menu-body">
                        <span class="composer-menu-name">${escapeHtml(m)}</span>
                    </span>
                    ${active ? '<i class="fas fa-check composer-menu-check"></i>' : ''}
                </button>`);
        });
    });

    menu.innerHTML = parts.join('');
}

/** Pin a model for this session; pass nulls to follow the global model again. */
async function selectSessionModel(provider, model) {
    _closeComposerMenus();
    await _applySessionSettings({ provider: provider, model: model });
}

// Single writer for both chips: POST the change, then repaint from the state the
// backend echoes back so the UI can never disagree with what was stored.
async function _applySessionSettings(body) {
    try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/settings`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.status !== 'success') { _wsToast(data.message || t('session_settings_failed')); return; }
        _sessCfg = { model: data.model };
        _renderModelChip();
    } catch (e) {
        _wsToast(t('session_settings_failed'));
    }
}

document.addEventListener('click', (e) => {
    [[_modelMenu(), _modelBtn()]].forEach(([menu, btn]) => {
        if (!menu || menu.classList.contains('hidden')) return;
        if (menu.contains(e.target) || (btn && btn.contains(e.target))) return;
        menu.classList.add('hidden');
        if (btn) btn.classList.remove('open');
    });
});

// Drag-and-drop support on entire chat view
const chatView = document.getElementById('view-chat');
const chatInputArea = document.getElementById('composer-card') || chatInput.closest('.flex-shrink-0');

// Create drag overlay for visual feedback
let dragOverlay = document.getElementById('drag-overlay');
if (!dragOverlay) {
    dragOverlay = document.createElement('div');
    dragOverlay.id = 'drag-overlay';
    dragOverlay.className = 'drag-overlay hidden';
    dragOverlay.innerHTML = `
        <div class="drag-overlay-content">
            <i class="fas fa-cloud-arrow-up"></i>
            <p>Drop files here to upload</p>
        </div>
    `;
    chatView.appendChild(dragOverlay);
}

let dragCounter = 0;

function showDragOverlay() {
    dragOverlay.classList.remove('hidden');
    dragOverlay.classList.add('active');
}

function hideDragOverlay() {
    dragOverlay.classList.remove('active');
    dragOverlay.classList.add('hidden');
}

/** Clear every drag affordance at once, whatever the drag's outcome was. */
function resetDragState() {
    dragCounter = 0;
    hideDragOverlay();
    chatInputArea.classList.remove('drag-over');
    document.getElementById('chat-main')?.classList.remove('ws-drop-active');
}

chatView.addEventListener('dragenter', (e) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter++;
    if (e.dataTransfer.types.includes('Files')) {
        showDragOverlay();
    }
});

chatView.addEventListener('dragover', (e) => {
    e.preventDefault();
    e.stopPropagation();
    // Only external file drags upload here; workspace drags have their own target.
    if (e.dataTransfer.types.includes('Files')) {
        chatInputArea.classList.add('drag-over');
    }
});

chatView.addEventListener('dragleave', (e) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter--;
    if (dragCounter <= 0) {
        resetDragState();
    }
});

chatView.addEventListener('drop', (e) => {
    e.preventDefault();
    e.stopPropagation();
    resetDragState();
    if (e.dataTransfer.files.length) {
        handleFileSelect(e.dataTransfer.files);
    }
});

// A drag can end without ever reaching a drop target (Esc, or released over
// another element). Clear the highlight unconditionally so it can't stay stuck
// until the next reload.
document.addEventListener('dragend', resetDragState);
window.addEventListener('drop', resetDragState);

document.body.addEventListener('dragover', (e) => {
    if (e.dataTransfer.types.includes('Files')) {
        e.preventDefault();
    }
});

document.body.addEventListener('drop', (e) => {
    if (e.dataTransfer.types.includes('Files')) {
        e.preventDefault();
    }
});

// Paste image support
chatInput.addEventListener('paste', (e) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const files = [];
    for (const item of items) {
        if (item.kind === 'file') {
            files.push(item.getAsFile());
        }
    }
    if (files.length) {
        e.preventDefault();
        handleFileSelect(files);
    }
});

chatInput.addEventListener('compositionstart', () => { isComposing = true; });
chatInput.addEventListener('compositionend', () => { setTimeout(() => { isComposing = false; }, 100); });

// ── Slash Command Menu ───────────────────────────────────────
// desc holds an i18n key, resolved via t() at render time so the menu follows
// the current UI language.
const SLASH_COMMANDS = [
    { cmd: '/help',                desc: 'slash_help' },
    { cmd: '/status',              desc: 'slash_status' },
    { cmd: '/context',             desc: 'slash_context' },
    { cmd: '/clear',               desc: 'slash_context_clear' },
    { cmd: '/compact',             desc: 'slash_compact' },
    { cmd: '/skill list',          desc: 'slash_skill_list' },
    { cmd: '/skill list --remote', desc: 'slash_skill_list_remote' },
    { cmd: '/skill search ',       desc: 'slash_skill_search' },
    { cmd: '/skill install ',      desc: 'slash_skill_install' },
    { cmd: '/skill uninstall ',    desc: 'slash_skill_uninstall' },
    { cmd: '/skill info ',         desc: 'slash_skill_info' },
    { cmd: '/skill enable ',       desc: 'slash_skill_enable' },
    { cmd: '/skill disable ',      desc: 'slash_skill_disable' },
    { cmd: '/memory dream ',       desc: 'slash_memory_dream' },
    { cmd: '/knowledge',           desc: 'slash_knowledge' },
    { cmd: '/knowledge list',      desc: 'slash_knowledge_list' },
    { cmd: '/knowledge on',        desc: 'slash_knowledge_on' },
    { cmd: '/knowledge off',       desc: 'slash_knowledge_off' },
    { cmd: '/场景',                 desc: 'slash_scenes' },
    { cmd: '/scenes',              desc: 'slash_scenes' },
    { cmd: '/config',              desc: 'slash_config' },
    { cmd: '/cancel',              desc: 'slash_cancel' },
    { cmd: '/steer ',              desc: 'slash_steer' },
    { cmd: '/logs',                desc: 'slash_logs' },
    { cmd: '/version',             desc: 'slash_version' },
];

const slashMenu = document.getElementById('slash-menu');
let slashActiveIdx = 0;
let slashFiltered = [];
let slashJustSelected = false;
let slashLastFilter = '';
let slashLastMouseX = -1;
let slashLastMouseY = -1;

function showSlashMenu(filter) {
    const q = filter.toLowerCase();
    if (q === slashLastFilter && !slashMenu.classList.contains('hidden')) return;
    slashLastFilter = q;

    const newFiltered = SLASH_COMMANDS.filter(c => c.cmd.toLowerCase().startsWith(q));
    if (newFiltered.length === 0) {
        hideSlashMenu();
        return;
    }

    const changed = newFiltered.length !== slashFiltered.length ||
        newFiltered.some((c, i) => c.cmd !== slashFiltered[i]?.cmd);
    slashFiltered = newFiltered;
    if (changed) slashActiveIdx = 0;
    slashActiveIdx = Math.min(slashActiveIdx, slashFiltered.length - 1);

    slashNavByKeyboard = true;
    renderSlashItems();
    slashMenu.classList.remove('hidden');
}

function hideSlashMenu() {
    slashMenu.classList.add('hidden');
    slashMenu.innerHTML = '';
    slashFiltered = [];
    slashActiveIdx = -1;
    slashLastFilter = '';
    slashNavByKeyboard = false;
    slashLastMouseX = -1;
    slashLastMouseY = -1;
}

function isSlashMenuVisible() {
    return !slashMenu.classList.contains('hidden') && slashFiltered.length > 0;
}

function renderSlashItems() {
    slashMenu.innerHTML =
        '<div class="slash-menu-header">Commands</div>' +
        slashFiltered.map((c, i) =>
            `<div class="slash-menu-item${i === slashActiveIdx ? ' active' : ''}" data-idx="${i}">` +
            `<span class="cmd">${escapeHtml(c.cmd)}</span>` +
            `<span class="desc">${escapeHtml(t(c.desc))}</span></div>`
        ).join('');

    const activeEl = slashMenu.querySelector('.slash-menu-item.active');
    if (activeEl) activeEl.scrollIntoView({ block: 'nearest' });
}

// Delegated events on the persistent slashMenu container (not destroyed by innerHTML)
// Use coordinate comparison to distinguish real mouse movement from DOM-rebuild phantom events.
slashMenu.addEventListener('mousemove', (e) => {
    if (e.clientX === slashLastMouseX && e.clientY === slashLastMouseY) return;
    slashLastMouseX = e.clientX;
    slashLastMouseY = e.clientY;
    if (!slashNavByKeyboard) return;
    slashNavByKeyboard = false;
    const item = e.target.closest('.slash-menu-item');
    if (!item) return;
    const idx = parseInt(item.dataset.idx);
    if (idx === slashActiveIdx) return;
    slashActiveIdx = idx;
    slashMenu.querySelectorAll('.slash-menu-item').forEach(el => {
        el.classList.toggle('active', parseInt(el.dataset.idx) === idx);
    });
});

slashMenu.addEventListener('mouseover', (e) => {
    if (slashNavByKeyboard) return;
    const item = e.target.closest('.slash-menu-item');
    if (!item) return;
    const idx = parseInt(item.dataset.idx);
    if (idx === slashActiveIdx) return;
    slashActiveIdx = idx;
    slashMenu.querySelectorAll('.slash-menu-item').forEach(el => {
        el.classList.toggle('active', parseInt(el.dataset.idx) === idx);
    });
});

slashMenu.addEventListener('mousedown', (e) => {
    const item = e.target.closest('.slash-menu-item');
    if (!item) return;
    e.preventDefault();
    selectSlashCommand(parseInt(item.dataset.idx));
});

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
            slashNavByKeyboard = true;
            slashActiveIdx = Math.max(slashActiveIdx - 1, 0);
            renderSlashItems();
            return;
        }
        if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey) {
            e.preventDefault();
            selectSlashCommand(slashActiveIdx);
            return;
        }
        if (e.key === 'Escape') {
            e.preventDefault();
            hideSlashMenu();
            return;
        }
        if (e.key === 'Tab') {
            e.preventDefault();
            selectSlashCommand(slashActiveIdx);
            return;
        }
    }

    // Arrow-key history recall (only when input is empty or already browsing history)
    if (e.key === 'ArrowUp' && inputHistory.length > 0 && !isSlashMenuVisible()) {
        const curVal = this.value.trim();
        const isSingleLine = !this.value.includes('\n');
        if (isSingleLine && (curVal === '' || historyIdx >= 0)) {
            e.preventDefault();
            if (historyIdx < 0) {
                historySavedDraft = this.value;
                historyIdx = inputHistory.length - 1;
            } else if (historyIdx > 0) {
                historyIdx--;
            }
            this.value = inputHistory[historyIdx];
            slashJustSelected = true;
            this.dispatchEvent(new Event('input'));
            hideSlashMenu();
            this.selectionStart = this.selectionEnd = this.value.length;
            return;
        }
    }
    if (e.key === 'ArrowDown' && historyIdx >= 0 && !isSlashMenuVisible()) {
        const isSingleLine = !this.value.includes('\n');
        if (isSingleLine) {
            e.preventDefault();
            if (historyIdx < inputHistory.length - 1) {
                historyIdx++;
                this.value = inputHistory[historyIdx];
            } else {
                historyIdx = -1;
                this.value = historySavedDraft;
                historySavedDraft = '';
            }
            slashJustSelected = true;
            this.dispatchEvent(new Event('input'));
            hideSlashMenu();
            this.selectionStart = this.selectionEnd = this.value.length;
            return;
        }
    }

    if ((e.ctrlKey || e.shiftKey) && e.key === 'Enter') {
        const start = this.selectionStart;
        const end = this.selectionEnd;
        this.value = this.value.substring(0, start) + '\n' + this.value.substring(end);
        this.selectionStart = this.selectionEnd = start + 1;
        this.dispatchEvent(new Event('input'));
        e.preventDefault();
    } else if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey) {
        sendMessage();
        e.preventDefault();
    }
});

chatInput.addEventListener('blur', () => {
    setTimeout(hideSlashMenu, 150);
});

bindWelcomeSuggestions(messagesDiv);

// Voice-message variant of sendMessage(): renders a playable audio bubble
// with the ASR caption, then dispatches the recognised text to /message
// through the same SSE/loading flow as a typed message.
function sendVoiceMessage(text, audioUrl) {
    text = (text || '').trim();
    if (!text) return;

    inputHistory.push(text);
    historyIdx = -1;
    historySavedDraft = '';

    const ws = document.getElementById('welcome-screen');
    const isFirstMessage = !!ws;
    if (ws) ws.remove();

    const titleInfo = isFirstMessage ? { sid: sessionId, userMsg: text } : null;
    const timestamp = new Date();
    addUserVoiceMessage(audioUrl, text, timestamp);
    const loadingEl = addLoadingIndicator();

    const body = {
        session_id: sessionId,
        message: text,
        stream: true,
        timestamp: timestamp.toISOString(),
        is_voice: true,
        lang: currentLang,
    };

    const MAX_RETRIES = 2;
    const RETRY_DELAY_MS = 1000;
    function postWithRetry(attempt) {
        fetch('/message', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        })
        .then(readMessageResponse)
        .then(data => {
            if (data.status === 'success') {
                rememberLiveSpeaker(data);
                setLoadingSpeaker(loadingEl, data.request_id);
                if (data.inline_reply) {
                    // Synchronous fast-path reply (e.g. /cancel); skip SSE.
                    loadingEl.remove();
                    addBotMessage(data.inline_reply, new Date());
                } else if (data.stream) {
                    setSendBtnCancelMode(data.request_id);
                    startSSE(data.request_id, loadingEl, timestamp, titleInfo);
                } else {
                    loadingContainers[data.request_id] = loadingEl;
                }
            } else {
                loadingEl.remove();
                addMessageError(data);
                resetSendBtnSendMode();
            }
        })
        .catch(err => {
            if (attempt < MAX_RETRIES) {
                setTimeout(() => postWithRetry(attempt + 1), RETRY_DELAY_MS * (attempt + 1));
                return;
            }
            loadingEl.remove();
            addBotMessage(t('error_send'), new Date());
        });
    }
    postWithRetry(0);
}

function addUserVoiceMessage(audioUrl, caption, timestamp) {
    const el = document.createElement('div');
    el.className = 'flex justify-end px-4 sm:px-6 py-3';
    // Voice-message bubble: compact voice pill on top, ASR caption beneath.
    // The bubble keeps the same primary tint as a normal user message so
    // it visually slots into the conversation flow.
    el.innerHTML = `
        <div class="max-w-[75%] sm:max-w-[60%]">
            <div class="bg-slate-100 dark:bg-white/10 text-slate-700 dark:text-slate-200 rounded-2xl px-3 py-2 msg-content user-bubble">
                <div class="user-voice-slot"></div>
                ${caption ? `<div class="text-xs mt-1.5 leading-snug text-slate-500 dark:text-slate-400 whitespace-pre-wrap break-words">${escapeHtml(caption)}</div>` : ''}
            </div>
            <div class="text-xs text-slate-400 dark:text-slate-500 mt-1.5 text-right">${formatTime(timestamp)}</div>
        </div>
    `;
    el.querySelector('.user-voice-slot').appendChild(renderVoicePill(audioUrl));
    messagesDiv.appendChild(el);
    _autoScrollEnabled = true;
    scrollChatToBottom(true);
}

// Clipboard helper with fallback for non-HTTPS environments
function copyToClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
        return navigator.clipboard.writeText(text);
    }
    // Fallback for HTTP environments
    return new Promise((resolve, reject) => {
        const textArea = document.createElement('textarea');
        textArea.value = text;
        textArea.style.position = 'fixed';
        textArea.style.left = '-999999px';
        textArea.style.top = '-999999px';
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        try {
            document.execCommand('copy') ? resolve() : reject(new Error('Copy failed'));
        } catch (err) {
            reject(err);
        } finally {
            textArea.remove();
        }
    });
}

// Edit user message: extract content, remove this and subsequent messages, fill input
async function editUserMessage(msgEl) {
    if (isCurrentSessionConversationActive()) return;
    const rawContent = msgEl.dataset.rawContent;
    if (!rawContent) return;

    // Delete this message and ALL subsequent messages from database (cascade)
    // Must await to ensure delete completes before user sends a new message
    const userSeq = msgEl.dataset.seq;
    if (userSeq) {
        try {
            const resp = await fetch('/api/messages/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    session_id: sessionId, 
                    user_seq: parseInt(userSeq),
                    delete_user: true,
                    cascade: true
                })
            });
            const data = await resp.json();
            if (data.status === 'success') console.log(`Deleted ${data.deleted} old messages`);
        } catch (err) {
            console.error('Failed to delete old messages:', err);
        }
    }

    // Remove this message bubble and every later bubble that belongs to
    // this or a subsequent turn. We mirror the backend cascade contract:
    // anything with a data-seq >= current seq, plus any live SSE bubble
    // that is still being streamed (no seq yet) after this point.
    const currentSeqNum = userSeq ? parseInt(userSeq) : null;
    const messagesToRemove = [];
    let current = msgEl;
    while (current) {
        if (current.classList && (current.classList.contains('user-message-group') || current.classList.contains('bot-message-group'))) {
            const seqAttr = current.dataset.seq;
            if (seqAttr === undefined || seqAttr === '') {
                // Live message without a persisted seq yet — treat as later.
                messagesToRemove.push(current);
            } else if (currentSeqNum === null || parseInt(seqAttr) >= currentSeqNum) {
                messagesToRemove.push(current);
            }
        }
        current = current.nextElementSibling;
    }
    messagesToRemove.forEach(el => {
        if (el && el.parentNode) el.parentNode.removeChild(el);
    });

    // Fill input with the original content
    chatInput.value = rawContent;
    chatInput.dispatchEvent(new Event("input", { bubbles: true }));
    chatInput.focus();
    chatInput.selectionStart = chatInput.selectionEnd = chatInput.value.length;
    scrollChatToBottom();
}

// Regenerate bot response: find the preceding user message and resend it
async function regenerateResponse(botMsgEl) {
    let prevEl = botMsgEl.previousElementSibling;
    while (prevEl && !prevEl.classList.contains('user-message-group')) {
        prevEl = prevEl.previousElementSibling;
    }

    if (!prevEl) {
        console.warn('No preceding user message found');
        return;
    }

    const userContent = prevEl.dataset.rawContent;
    if (!userContent) {
        console.warn('No content in preceding user message');
        return;
    }

    // Delete both the old user message AND bot reply from database
    // (because /message will create a fresh user message + new bot reply)
    // Must await to ensure delete completes before /message is sent
    const userSeq = prevEl.dataset.seq;
    if (userSeq) {
        try {
            const resp = await fetch('/api/messages/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    session_id: sessionId, 
                    user_seq: parseInt(userSeq),
                    delete_user: true
                })
            });
            const data = await resp.json();
            if (data.status === 'success') console.log(`Deleted ${data.deleted} old messages`);
        } catch (err) {
            console.error('Failed to delete old messages:', err);
        }
    }

    // Remove both the old user message and bot message from DOM
    if (prevEl.parentNode) prevEl.parentNode.removeChild(prevEl);
    if (botMsgEl.parentNode) botMsgEl.parentNode.removeChild(botMsgEl);

    // Re-add the user message to DOM (so it appears before the loading indicator)
    addUserMessage(userContent, new Date());

    // Show loading indicator
    const loadingEl = addLoadingIndicator();

    // Resend the message
    const timestamp = new Date();
    const body = { session_id: sessionId, message: userContent, stream: true, timestamp: timestamp.toISOString(), lang: currentLang };
    const regenAddressed = addressedAgentId(userContent);
    if (regenAddressed) body.speaker_agent_id = regenAddressed;

    const MAX_RETRIES = 2;
    const RETRY_DELAY_MS = 1000;

    function postWithRetry(attempt) {
        fetch('/message', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        })
        .then(readMessageResponse)
        .then(data => {
            if (data.status === 'success') {
                rememberLiveSpeaker(data);
                setLoadingSpeaker(loadingEl, data.request_id);
                if (data.inline_reply) {
                    loadingEl.remove();
                    addBotMessage(data.inline_reply, new Date());
                } else if (data.stream) {
                    setSendBtnCancelMode(data.request_id);
                    startSSE(data.request_id, loadingEl, timestamp, null);
                } else {
                    loadingContainers[data.request_id] = loadingEl;
                }
            } else {
                loadingEl.remove();
                addMessageError(data);
                resetSendBtnSendMode();
            }
        })
        .catch(err => {
            if (err.name === 'AbortError') {
                loadingEl.remove();
                addBotMessage(t('error_timeout'), new Date());
                resetSendBtnSendMode();
                return;
            }
            if (attempt < MAX_RETRIES) {
                console.warn(`[regenerateResponse] attempt ${attempt + 1} failed, retrying...`, err);
                setTimeout(() => postWithRetry(attempt + 1), RETRY_DELAY_MS * (attempt + 1));
                return;
            }
            loadingEl.remove();
            addBotMessage(t('error_send'), new Date());
            resetSendBtnSendMode();
        });
    }

    postWithRetry(0);
}

// Preserve actionable server failures without retrying an HTTP rejection.
async function readMessageResponse(response) {
    let data;
    try { data = await response.json(); } catch (_) { data = null; }
    if (!data || typeof data !== 'object' || Array.isArray(data)) data = { status: 'error' };
    return { ...data, status: response.ok ? data.status : 'error', http_status: response.status };
}

function messageFailureText(data) {
    const code = String(data?.code || '').toLowerCase();
    const detail = typeof data?.message === 'string' ? data.message.trim() : '';
    const normalized = detail.toLowerCase();
    if (data?.http_status === 401 || code === 'unauthorized' || normalized === 'unauthorized') {
        return t('error_login_required');
    }
    if (code === 'missing_tenant' || code === 'conflicting_tenant'
            || normalized === 'tenant selection required' || normalized === 'conflicting tenant selection') {
        return t('error_tenant_required');
    }
    if (code === 'password_change_required' || normalized === 'password change required') {
        return t('error_password_required');
    }
    if (data?.http_status === 403 || code === 'forbidden' || normalized === 'forbidden') {
        return t('error_chat_forbidden');
    }
    return detail ? `${t('error_send')} ${detail.slice(0, 500)}` : t('error_send');
}

function addMessageError(data) {
    const text = messageFailureText(data);
    const el = createBotMessageEl('', new Date());
    const content = el.querySelector('.answer-content');
    // Error details are plain text, even when the server returns markup or a
    // Markdown link. They must never become executable HTML or clickable UI.
    content.textContent = text;
    content.dataset.rawMd = text;
    messagesDiv.appendChild(el);
    scrollChatToBottom();
}

function sendMessage() {
    if (_identityMode() === 'database' && !sessionStorage.getItem('cow_tenant_id')) {
        addMessageError({ code: 'missing_tenant' });
        return;
    }
    // Do NOT branch on sendBtnMode here: Enter should always send (so
    // typing "/cancel" submits normally). Cancel is wired only to the
    // send button's pointer click — see send-btn listener above.

    const text = chatInput.value.trim();
    if (!text && pendingAttachments.length === 0) return;

    // Continue the original SAP workbench query context through its host adapter.
    if (text.startsWith('/sap ') && typeof window.continueSceneSapAnalysis === 'function') {
        chatInput.value = '';
        window.continueSceneSapAnalysis(text.slice(5).trim());
        return;
    }

    // `/场景`（或 `/scenes`）打开场景选择器，不发送给后端。
    if ((text === '/场景' || text === '/scenes') && typeof window.showScenePicker === 'function') {
        chatInput.value = '';
        window.showScenePicker();
        return;
    }

    if (text) {
        inputHistory.push(text);
        historyIdx = -1;
        historySavedDraft = '';
    }

    const ws = document.getElementById('welcome-screen');
    const isFirstMessage = !!ws;
    if (ws) ws.remove();

    const titleInfo = (isFirstMessage && text) ? { sid: sessionId, userMsg: text } : null;
    syncTeamFromText(text);
    renderComposerIdentity();

    const timestamp = new Date();
    const attachments = [...pendingAttachments];
    addUserMessage(text, timestamp, attachments);

    const loadingEl = addLoadingIndicator();

    chatInput.value = '';
    resetComposerHeight();
    pendingAttachments = [];
    renderAttachmentPreview();
    sendBtn.disabled = true;
    if (typeof resetTurnArtifacts === 'function') resetTurnArtifacts();

    const body = { session_id: sessionId, message: text, stream: true, timestamp: timestamp.toISOString(), lang: currentLang };
    // Naming somebody hands them the turn. Sent explicitly because the composer
    // already knows who it wrote, and the server re-checks it either way.
    const addressed = addressedAgentId(text);
    if (addressed) body.speaker_agent_id = addressed;
    if (attachments.length > 0) {
        body.attachments = attachments.map(a => ({
            file_path: a.file_path,
            file_name: a.file_name,
            file_type: a.file_type,
            file_count: a.file_count,
        }));
    }

    const MAX_RETRIES = 2;
    const RETRY_DELAY_MS = 1000;

    function postWithRetry(attempt) {
        fetch('/message', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        })
        .then(readMessageResponse)
        .then(data => {
            if (data.status === 'success') {
                rememberLiveSpeaker(data);
                // The turn has now persisted the session: the context entry can
                // read a real row instead of the quiet pre-persistence state.
                if (typeof _contextAfterSessionChange === 'function' && _contextNewSession) {
                    _contextAfterSessionChange(true);
                }
                setLoadingSpeaker(loadingEl, data.request_id);
                if (data.inline_reply) {
                    // Channel handled synchronously (e.g. /cancel fast-path);
                    // render as a bot bubble and skip SSE entirely.
                    loadingEl.remove();
                    addBotMessage(data.inline_reply, new Date());
                } else if (data.stream) {
                    setSendBtnCancelMode(data.request_id);
                    startSSE(data.request_id, loadingEl, timestamp, titleInfo);
                } else {
                    loadingContainers[data.request_id] = loadingEl;
                }
            } else {
                loadingEl.remove();
                addMessageError(data);
                resetSendBtnSendMode();
            }
        })
        .catch(err => {
            if (err.name === 'AbortError') {
                loadingEl.remove();
                addBotMessage(t('error_timeout'), new Date());
                resetSendBtnSendMode();
                return;
            }
            if (attempt < MAX_RETRIES) {
                console.warn(`[sendMessage] attempt ${attempt + 1} failed, retrying...`, err);
                setTimeout(() => postWithRetry(attempt + 1), RETRY_DELAY_MS * (attempt + 1));
                return;
            }
            loadingEl.remove();
            addBotMessage(t('error_send'), new Date());
            resetSendBtnSendMode();
        });
    }

    postWithRetry(0);
}

function startSSE(requestId, loadingEl, timestamp, titleInfo, replayItems) {
    let botEl = null;
    let stepsEl = null;    // .agent-steps  (thinking summaries + tool indicators)
    let contentEl = null;  // .answer-content (final streaming answer)
    let mediaEl = null;    // .media-content (images & file attachments)
    let accumulatedText = '';
    const toolElements = new Map();
    let currentReasoningEl = null;  // live reasoning bubble
    let reasoningText = '';
    let reasoningStartTime = 0;
    let done = false;
    let mainDone = false;
    let completedBotSeq = null;
    let cancelled = false;
    let lastSeq = 0;

    // A stream can end while tools are still marked in-flight (cancel, dropped
    // connection). Settle them so nothing spins forever.
    function settlePendingTools() {
        toolElements.forEach(el => {
            el.classList.remove('tool-streaming');
            const icon = el.querySelector('.tool-icon');
            if (icon) icon.className = 'fas fa-minus text-slate-400 flex-shrink-0 tool-icon';
        });
        toolElements.clear();
    }

    // The session this stream belongs to. Sessions run in parallel: the user
    // may switch to another session while this one is still streaming. The
    // stream keeps running in the background (so the reply still completes and
    // persists); when foreign it does not touch the view but still records
    // every event into a buffer, so returning to the session can rebuild the
    // bubble by replaying the buffer and then resume live rendering.
    const ownerSession = sessionId;
    const ownerAgent = activeAgentId;
    const ownerKey = runtimeSessionKey(ownerSession, ownerAgent);
    const isActive = () => ownerSession === sessionId && ownerAgent === activeAgentId;
    sessionActiveRequest[ownerKey] = requestId;
    updateEditButtonsState();
    // Per-request event buffer used to rebuild the bubble on re-attach.
    const buffer = streamBuffers[requestId] || { items: [], timestamp };
    streamBuffers[requestId] = buffer;
    const clearOwnerRequest = () => {
        if (sessionActiveRequest[ownerKey] === requestId) {
            delete sessionActiveRequest[ownerKey];
            updateEditButtonsState();
        }
        delete streamBuffers[requestId];
    };

    const MAX_RECONNECTS = 10;
    const RECONNECT_BASE_MS = 1000;
    let reconnectCount = 0;

    function ensureBotEl() {
        if (botEl) return;
        if (loadingEl) { loadingEl.remove(); loadingEl = null; }
        botEl = document.createElement('div');
        botEl.className = 'flex gap-3 px-4 sm:px-6 py-3 bot-message-group';
        botEl.dataset.requestId = requestId;
        // Regenerate button starts hidden; it's revealed in the "done"
        // event handler once seq metadata arrives from the backend.
        // The streaming face is whoever is answering this request: the addressed
        // teammate if one was named, else the conversation's own Agent. Wrapped
        // in .bot-face so a later avatar change repaints it like any bubble.
        const speaker = liveSpeakerAgent(requestId);
        if (speaker && speaker.id) botEl.dataset.speakerAgent = speaker.id;
        // In a group the bubble is labelled with its author while it streams,
        // exactly as the replayed history shows it — a solo chat stays unlabelled.
        const speakerName = (sharedConversation() && speaker)
            ? `<div class="bot-speaker">${escapeHtml(speaker.name || speaker.id)}</div>`
            : '';
        botEl.innerHTML = `
            <span class="bot-face">${agentAvatarHTML(speaker, 32)}</span>
            <div class="min-w-0 flex-1 max-w-[85%]">
                ${speakerName}
                <div class="bg-white dark:bg-[#1A1A1A] border border-slate-200 dark:border-white/10 rounded-2xl px-4 py-3 text-sm leading-relaxed msg-content text-slate-700 dark:text-slate-200">
                    <div class="agent-steps"></div>
                    <div class="answer-content sse-streaming"></div>
                    <div class="media-content"></div>
                    <div class="bot-audio-slot"></div>
                </div>
                <div class="flex items-center gap-2 mt-1.5">
                    <span class="text-xs text-slate-400 dark:text-slate-500">${formatTime(timestamp)}</span>
                    <button class="copy-msg-btn text-xs text-slate-300 dark:text-slate-600 hover:text-slate-500 dark:hover:text-slate-400 transition-colors cursor-pointer" title="${currentLang === 'zh' ? '复制' : 'Copy'}" style="display:none">
                        <i class="fas fa-copy"></i>
                    </button>
                    <button class="speak-msg-btn text-xs text-slate-300 dark:text-slate-600 hover:text-slate-500 dark:hover:text-slate-400 transition-colors cursor-pointer" title="${t('speak_msg')}" style="display:none;">
                        <i class="fas fa-volume-up"></i>
                    </button>
                    <button class="regenerate-msg-btn text-xs text-slate-300 dark:text-slate-600 hover:text-primary-400 dark:hover:text-primary-400 transition-colors cursor-pointer" title="${t('regenerate_response')}" style="display:none;">
                        <i class="fas fa-rotate-right"></i>
                    </button>
                </div>
            </div>
        `;
        messagesDiv.appendChild(botEl);
        stepsEl = botEl.querySelector('.agent-steps');
        contentEl = botEl.querySelector('.answer-content');
        mediaEl = botEl.querySelector('.media-content');
    }

    // Holds the live EventSource so terminal events (done/voice_attach/error)
    // can close it. During replay there is no live connection (null).
    let currentEs = null;

    // Render one SSE event into the bubble. Used by the live handler and by
    // re-attach replay alike, so both paths produce identical UI.
    function processSSEItem(item) {
            if (item.type === 'reasoning') {
                ensureBotEl();
                reasoningText += item.content;
                if (!currentReasoningEl) {
                    reasoningStartTime = Date.now();
                    currentReasoningEl = document.createElement('div');
                    currentReasoningEl.className = 'agent-step agent-thinking-step';
                    // During streaming, use a <pre> with a single text node and
                    // append-only updates. This avoids re-parsing markdown and
                    // re-setting innerHTML on every chunk, which is what causes
                    // the page to crash on long chains-of-thought.
                    currentReasoningEl.innerHTML = `
                        <div class="thinking-header" onclick="this.parentElement.classList.toggle('expanded')">
                            <i class="fas fa-lightbulb text-amber-400 flex-shrink-0"></i>
                            <span class="thinking-summary">${t('thinking_in_progress')}</span>
                            <i class="fas fa-chevron-right thinking-chevron"></i>
                        </div>
                        <div class="thinking-full"><pre class="thinking-stream-pre"></pre></div>`;
                    stepsEl.appendChild(currentReasoningEl);
                    const preEl = currentReasoningEl.querySelector('.thinking-stream-pre');
                    preEl.appendChild(document.createTextNode(''));
                    currentReasoningEl._streamTextNode = preEl.firstChild;
                    currentReasoningEl._streamPendingText = '';
                    currentReasoningEl._streamRafScheduled = false;
                    currentReasoningEl._streamCharsRendered = 0;
                    currentReasoningEl._streamCapped = false;
                }
                // Hard cap: once REASONING_RENDER_CAP chars are in the DOM, stop
                // appending further deltas. The full text is still kept in
                // `reasoningText` for finalize-time head+tail rendering.
                if (!currentReasoningEl._streamCapped) {
                    currentReasoningEl._streamPendingText += item.content;
                    if (!currentReasoningEl._streamRafScheduled) {
                        currentReasoningEl._streamRafScheduled = true;
                        const elRef = currentReasoningEl;
                        requestAnimationFrame(() => {
                            elRef._streamRafScheduled = false;
                            if (!elRef.isConnected || !elRef._streamTextNode) return;
                            let pending = elRef._streamPendingText;
                            elRef._streamPendingText = '';
                            if (!pending) return;
                            const remaining = REASONING_RENDER_CAP - elRef._streamCharsRendered;
                            if (remaining <= 0) {
                                elRef._streamCapped = true;
                            } else {
                                if (pending.length > remaining) {
                                    pending = pending.slice(0, remaining);
                                    elRef._streamCapped = true;
                                }
                                elRef._streamTextNode.appendData(pending);
                                elRef._streamCharsRendered += pending.length;
                                if (elRef._streamCapped) {
                                    elRef._streamTextNode.appendData(
                                        '\n\n... [reasoning truncated for display] ...'
                                    );
                                }
                            }
                            scrollChatToBottom();
                        });
                    }
                }

            } else if (item.type === 'delta') {
                ensureBotEl();
                if (currentReasoningEl) {
                    finalizeThinking(currentReasoningEl, reasoningStartTime, reasoningText);
                    currentReasoningEl = null;
                    reasoningText = '';
                }
                accumulatedText += item.content;
                contentEl.innerHTML = renderMarkdown(accumulatedText);
                scrollChatToBottom();

            } else if (item.type === 'message_end') {
                if (item.has_tool_calls && accumulatedText.trim()) {
                    ensureBotEl();
                    const frozenEl = document.createElement('div');
                    frozenEl.className = 'agent-step agent-content-step';
                    frozenEl.innerHTML = `<div class="agent-content-body">${renderMarkdown(accumulatedText.trim())}</div>`;
                    stepsEl.appendChild(frozenEl);
                    accumulatedText = '';
                    contentEl.innerHTML = '';
                    scrollChatToBottom();
                }

            } else if (item.type === 'tool_start') {
                ensureBotEl();
                if (currentReasoningEl) {
                    finalizeThinking(currentReasoningEl, reasoningStartTime, reasoningText);
                    currentReasoningEl = null;
                    reasoningText = '';
                }
                accumulatedText = '';
                contentEl.innerHTML = '';

                // Add tool execution indicator (collapsible)
                const toolEl = document.createElement('div');
                toolEl.className = 'agent-step agent-tool-step tool-streaming';
                toolEl.dataset.progressReceived = 'false';
                const argsStr = formatToolArgs(item.arguments || {});
                toolEl.innerHTML = `
                    <div class="tool-header" onclick="this.parentElement.classList.toggle('expanded')">
                        <i class="fas fa-cog fa-spin text-primary-400 flex-shrink-0 tool-icon"></i>
                        <span class="tool-name">${item.tool}</span>
                        <span class="tool-substep-count"></span>
                        <i class="fas fa-chevron-right tool-chevron"></i>
                    </div>
                    <div class="tool-detail">
                        <div class="tool-detail-section">
                            <div class="tool-detail-label">Input</div>
                            <pre class="tool-detail-content">${argsStr}</pre>
                        </div>
                        <div class="tool-detail-section tool-substeps-section hidden">
                            <div class="tool-detail-label">Steps</div>
                            <div class="tool-substeps"></div>
                        </div>
                        <div class="tool-detail-section tool-output-section">
                            <div class="tool-detail-label tool-output-label">Output</div>
                            <pre class="tool-detail-content tool-live-output"></pre>
                            <div class="tool-display-output"></div>
                        </div>
                    </div>`;
                stepsEl.appendChild(toolEl);
                toolElements.set(item.tool_call_id, toolEl);

                scrollChatToBottom();

            } else if (item.type === 'tool_progress') {
                const toolEl = toolElements.get(item.tool_call_id);
                if (toolEl) {
                    if (toolEl.dataset.progressReceived !== 'true') {
                        toolEl.classList.add('expanded');
                        toolEl.dataset.progressReceived = 'true';
                    }
                    toolEl.querySelector('.tool-live-output').textContent = String(item.content || '');
                    scrollChatToBottom();
                }

            } else if (item.type === 'tool_end') {
                const toolEl = toolElements.get(item.tool_call_id);
                if (toolEl) {
                    const isError = item.status !== 'success';
                    const icon = toolEl.querySelector('.tool-icon');
                    icon.className = isError
                        ? 'fas fa-times text-red-400 flex-shrink-0 tool-icon'
                        : 'fas fa-check text-primary-400 flex-shrink-0 tool-icon';

                    // Show execution time
                    const nameEl = toolEl.querySelector('.tool-name');
                    if (item.execution_time !== undefined) {
                        nameEl.innerHTML += ` <span class="tool-time">${item.execution_time}s</span>`;
                    }

                    // Fill output section. A tool that wrote its outcome for a
                    // person (item.display) gets rendered as markdown; the raw
                    // result is what the model reads and stays hidden then.
                    const outputLabel = toolEl.querySelector('.tool-output-label');
                    const outputEl = toolEl.querySelector('.tool-live-output');
                    const displayEl = toolEl.querySelector('.tool-display-output');
                    if (outputLabel) outputLabel.textContent = isError ? 'Error' : 'Output';
                    if (displayEl && item.display) {
                        displayEl.innerHTML = renderMarkdown(String(item.display));
                        displayEl.classList.add('has-content');
                        if (outputEl) outputEl.textContent = '';
                    } else if (outputEl) {
                        outputEl.textContent = item.result ? String(item.result) : '';
                        outputEl.classList.toggle('tool-error-text', isError);
                    }

                    toolEl.classList.remove('tool-streaming');
                    // Tools collapse once they are done; their output is a
                    // trace. A tool that wrote something for a person to read
                    // stays open — the reader just waited for it.
                    toolEl.classList.toggle('expanded', !!item.display);
                    if (!item.result && !item.display) {
                        const outputSection = toolEl.querySelector('.tool-output-section');
                        if (outputSection) outputSection.remove();
                    }
                    if (isError) toolEl.classList.add('tool-failed');
                    // A permission refusal is not an ordinary failure: add a short
                    // sentence explaining it instead of leaving the user to decode
                    // the model's error text. There is no action button — execution
                    // is decided by the caller's role, not by a chat control.
                    if (item.permission_denied) {
                        _appendPermissionDeniedHint(toolEl, item.permission_mode, item.permission_denial_kind);
                    }
                    toolElements.delete(item.tool_call_id);
                }

            } else if (item.type === 'subagent_step') {
                // A tool call made inside a sub agent, rendered under that sub
                // agent's card so its minutes of work are followable.
                renderSubagentStep(toolElements.get(item.card_id), item);
                scrollChatToBottom();

            } else if (item.type === 'image') {
                ensureBotEl();
                const imgEl = document.createElement('img');
                imgEl.src = item.content;
                imgEl.alt = 'screenshot';
                imgEl.style.cssText = 'max-width:600px;border-radius:8px;margin:8px 0;cursor:zoom-in;box-shadow:0 1px 4px rgba(0,0,0,0.1);';
                imgEl.onclick = () => _openImageLightbox(imgEl.src);
                mediaEl.appendChild(imgEl);
                scrollChatToBottom();

            } else if (item.type === 'text') {
                // Intermediate text sent before media items; display it but keep SSE open.
                ensureBotEl();
                contentEl.classList.remove('sse-streaming');
                const textContent = item.content || accumulatedText;
                if (textContent) contentEl.innerHTML = renderMarkdown(textContent);
                applyHighlighting(botEl);
                scrollChatToBottom();

            } else if (item.type === 'video') {
                ensureBotEl();
                const wrapper = document.createElement('div');
                wrapper.innerHTML = _buildVideoHtml(item.content);
                mediaEl.appendChild(wrapper.firstElementChild || wrapper);
                scrollChatToBottom();

            } else if (item.type === 'file') {
                ensureBotEl();
                const fileName = item.file_name || item.content.split('/').pop();
                const fileEl = document.createElement('a');
                fileEl.href = item.content;
                fileEl.download = fileName;
                fileEl.target = '_blank';
                fileEl.className = 'file-attachment';
                fileEl.style.cssText = 'display:inline-flex;align-items:center;gap:6px;padding:8px 14px;margin:8px 0;border-radius:8px;background:var(--bg-secondary,#f3f4f6);color:var(--text-primary,#374151);text-decoration:none;font-size:14px;border:1px solid var(--border-color,#e5e7eb);';
                fileEl.innerHTML = `<i class="fas fa-file-download" style="color:#6b7280;"></i> ${fileName}`;
                mediaEl.appendChild(fileEl);
                scrollChatToBottom();

            } else if (item.type === 'artifact') {
                // A user-facing file the agent wrote; render a card and let the
                // workspace panel decide whether to auto-open it (workspace.js).
                ensureBotEl();
                if (typeof appendArtifactCard === 'function') {
                    appendArtifactCard(mediaEl, item);
                }
                scrollChatToBottom();

            } else if (item.type === 'phase') {
                // Coarse progress (e.g. cow install-browser); must not close SSE (unlike "done")
                ensureBotEl();
                const wrap = document.createElement('div');
                wrap.className = 'text-xs sm:text-sm text-slate-600 dark:text-slate-400 border-l-2 border-primary-400 pl-2 py-1 my-0.5';
                wrap.textContent = String(item.content || '');
                stepsEl.appendChild(wrap);
                scrollChatToBottom();

            } else if (item.type === 'cancelled') {
                // Agent acknowledged the stop; mark the bubble. A trailing
                // "done" still arrives with the partial answer.
                cancelled = true;
                ensureBotEl();
                if (currentReasoningEl) {
                    finalizeThinking(currentReasoningEl, reasoningStartTime, reasoningText);
                    currentReasoningEl = null;
                    reasoningText = '';
                }
                if (!botEl.querySelector('.agent-cancelled-tag')) {
                    const tag = document.createElement('div');
                    tag.className = 'agent-cancelled-tag text-xs text-amber-600 dark:text-amber-400 mt-1';
                    tag.textContent = (currentLang === 'zh') ? '已中止' : 'Cancelled';
                    stepsEl.appendChild(tag);
                }
                resetSendBtnSendMode();

            } else if (item.type === 'done') {
                // The answer is persisted, but async attachments may still
                // follow. Only stream_end closes the request lifecycle.
                mainDone = true;
                if (item.bot_seq !== undefined && item.bot_seq !== null) {
                    completedBotSeq = item.bot_seq;
                }
                settlePendingTools();
                resetSendBtnSendMode();

                const finalTextRaw = item.content || accumulatedText;
                const finalText = localizeCancelMarker(finalTextRaw);

                if (!botEl && finalText) {
                    if (loadingEl) { loadingEl.remove(); loadingEl = null; }
                    addBotMessage(finalText, new Date((item.timestamp || Date.now() / 1000) * 1000), requestId);
                } else if (botEl) {
                    contentEl.classList.remove('sse-streaming');
                    if (finalText) contentEl.innerHTML = renderMarkdown(finalText);
                    contentEl.dataset.rawMd = finalTextRaw || '';
                    const copyBtn = botEl.querySelector('.copy-msg-btn');
                    if (copyBtn && finalText) copyBtn.style.display = '';
                    applyHighlighting(botEl);
                }

                // Backfill seq metadata so edit/regenerate buttons can call
                // the delete API without a page refresh. Backend includes
                // user_seq / bot_seq on the done event after persistence.
                const targetBotEl = botEl || (requestId ? messagesDiv.querySelector(`[data-request-id="${requestId}"]`) : null);
                if (targetBotEl) {
                    if (item.bot_seq !== undefined && item.bot_seq !== null) {
                        targetBotEl.dataset.seq = item.bot_seq;
                    }
                    // Reveal regenerate button now that the seq is wired up.
                    const regenBtn = targetBotEl.querySelector('.regenerate-msg-btn');
                    if (regenBtn) regenBtn.style.display = '';
                    if (item.user_seq !== undefined && item.user_seq !== null) {
                        // Locate the preceding user bubble for this turn.
                        let prev = targetBotEl.previousElementSibling;
                        while (prev && !prev.classList.contains('user-message-group')) {
                            prev = prev.previousElementSibling;
                        }
                        if (prev && !prev.dataset.seq) {
                            prev.dataset.seq = item.user_seq;
                        }
                    }
                }
                renderBotSpeakerButton(botEl, finalText);
                scrollChatToBottom();

                if (typeof maybeAutoOpenArtifact === 'function') maybeAutoOpenArtifact();

                if (titleInfo) {
                    generateSessionTitle(titleInfo.sid, titleInfo.userMsg, '');
                    titleInfo = null;
                } else {
                    // A session's title may have been regenerated/re-ordered or its
                    // activity updated. Refresh the visible history list, otherwise
                    // mark it dirty so the next visit re-reads the latest state.
                    if (_historyVisible) loadSessionList();
                    else _historyDirty = true;
                }

            } else if (item.type === 'voice_attach') {
                // TTS finished — attach a playable audio element to the
                // persisted bot bubble. If history is still loading after a
                // session switch, keep the attachment until that bubble exists.
                if (item.url && completedBotSeq !== null) {
                    rememberPendingVoiceAttachment(
                        ownerSession, completedBotSeq, item.url
                    );
                    flushPendingVoiceAttachments(ownerSession, true);
                }

            } else if (item.type === 'stream_end') {
                done = true;
                if (currentEs) { currentEs.close(); }
                delete activeStreams[requestId];
                clearOwnerRequest();

            } else if (item.type === 'resync_required') {
                done = true;
                settlePendingTools();
                if (currentEs) { currentEs.close(); }
                delete activeStreams[requestId];
                clearOwnerRequest();
                resetSendBtnSendMode();
                if (isActive()) {
                    messagesDiv.innerHTML = '';
                    historyPage = 0;
                    historyHasMore = false;
                    historyLoading = false;
                    loadHistory(1);
                }

            } else if (item.type === 'error') {
                done = true;
                settlePendingTools();
                if (currentEs) { currentEs.close(); }
                delete activeStreams[requestId];
                clearOwnerRequest();
                if (loadingEl) { loadingEl.remove(); loadingEl = null; }
                // After a stop the stream is expected to end; the bubble is
                // already tagged "已中止", so don't stack a failure on top.
                if (!cancelled) addBotMessage(t('error_send'), new Date());
                resetSendBtnSendMode();
            }
    }

    function connect() {
        const es = new EventSource(
            `/stream?request_id=${encodeURIComponent(requestId)}`
            + `&after_seq=${lastSeq}`
        );
        currentEs = es;
        activeStreams[requestId] = es;

        es.onmessage = function(e) {
            let item;
            try { item = JSON.parse(e.data); } catch (_) { return; }

            const seq = Number(item.seq || 0);
            if (seq && seq <= lastSeq) return;

            // Successful data received, reset reconnect counter
            reconnectCount = 0;

            // Record every event for re-attach replay (capped to avoid
            // unbounded growth on very long streams).
            if (item.type === 'tool_progress' && item.tool_call_id) {
                const previousIndex = buffer.items.findIndex(
                    buffered => buffered.type === 'tool_progress'
                        && buffered.tool_call_id === item.tool_call_id
                );
                if (previousIndex >= 0) buffer.items.splice(previousIndex, 1);
            }
            if (buffer.items.length < 5000) buffer.items.push(item);
            if (seq) lastSeq = seq;

            // done is persisted before it is published. Remember that state
            // even while this session is in the background, where rendering
            // is intentionally skipped. Notify for both foreground and
            // background sessions, before the render guard below.
            if (item.type === 'done') {
                mainDone = true;
                if (item.bot_seq !== undefined && item.bot_seq !== null) {
                    completedBotSeq = item.bot_seq;
                }
                notifyTaskFinished(ownerSession, 'done', item.content);
            } else if (item.type === 'error') {
                if (!cancelled) notifyTaskFinished(ownerSession, 'error', '');
            } else if (
                item.type === 'voice_attach'
                && item.url
                && completedBotSeq !== null
            ) {
                // Background sessions skip rendering below. Preserve their
                // attachment so loadHistory can mount it when the user returns.
                rememberPendingVoiceAttachment(
                    ownerSession, completedBotSeq, item.url
                );
            }

            // Background session: keep the stream alive so the reply finishes
            // and persists, but skip rendering into the now-foreign view. The
            // buffer above still grows so returning to the session can rebuild
            // the bubble and resume live rendering.
            if (ownerSession !== sessionId) {
                if (item.type === 'stream_end' || item.type === 'error' || item.type === 'resync_required') {
                    done = true;
                    es.close();
                    delete activeStreams[requestId];
                    clearOwnerRequest();
                }
                return;
            }

            processSSEItem(item);
        };

        es.onerror = function() {
            es.close();
            delete activeStreams[requestId];

            if (done) {
                // stream_end or an unrecoverable event already closed it.
                return;
            }

            if (cancelled && !mainDone) {
                // The user stopped the run, so the stream ending here is the
                // expected outcome. Reconnecting would only land on a queue
                // the backend has already reclaimed.
                settlePendingTools();
                clearOwnerRequest();
                if (loadingEl) { loadingEl.remove(); loadingEl = null; }
                if (contentEl) contentEl.classList.remove('sse-streaming');
                resetSendBtnSendMode();
                return;
            }

            if (currentReasoningEl) {
                finalizeThinking(currentReasoningEl, reasoningStartTime, reasoningText);
                currentReasoningEl = null;
                reasoningText = '';
            }

            if (reconnectCount < MAX_RECONNECTS) {
                reconnectCount++;
                const delay = Math.min(RECONNECT_BASE_MS * reconnectCount, 5000);
                console.warn(`[SSE] connection lost for ${requestId}, reconnecting in ${delay}ms (attempt ${reconnectCount}/${MAX_RECONNECTS})`);
                setTimeout(connect, delay);
                return;
            }

            // Exhausted retries. Only surface the failure in the owning view —
            // a background session must not mutate the currently shown chat.
            clearOwnerRequest();
            settlePendingTools();
            if (!isActive()) return;
            if (loadingEl) { loadingEl.remove(); loadingEl = null; }
            if (!botEl) {
                addBotMessage(t('error_send'), new Date());
            } else if (accumulatedText) {
                contentEl.classList.remove('sse-streaming');
                contentEl.innerHTML = renderMarkdown(accumulatedText);
                applyHighlighting(botEl);
            }
            resetSendBtnSendMode();
        };
    }

    // Re-attach replay: rebuild the bubble from buffered events (snapshot,
    // not animated) before connecting for the live tail. `processSSEItem`
    // is the same renderer used by the live onmessage handler, so the
    // snapshot matches exactly what live rendering would have produced.
    if (replayItems && replayItems.length) {
        for (const item of replayItems) {
            const seq = Number(item.seq || 0);
            if (seq > lastSeq) lastSeq = seq;
            try { processSSEItem(item); } catch (_) {}
            if (item.type === 'stream_end' || item.type === 'error' || item.type === 'resync_required') {
                done = true;
            }
        }
        // If the buffered stream already finished, don't reconnect — the
        // reply is complete and persisted; show its final state and stop.
        if (done) {
            clearOwnerRequest();
            resetSendBtnSendMode();
            scrollChatToBottom(true);
            return;
        }
    }

    connect();
}

function startPolling() {
    const gen = ++pollGeneration;
    isPolling = true;
    let pollInFlight = false;

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
                    // Pushed message (scheduler result, missed reply): show the
                    // content itself, matching the desktop push notification.
                    showTaskNotification(
                        sessionTitleOf(sessionId) || PRODUCT_NAME,
                        firstLineSnippet(data.content),
                        sessionId
                    );
                }
            }
            const delay = (data.status === 'success' && data.has_content) ? 5000 : 10000;
            setTimeout(poll, delay);
        })
        .catch(() => { pollInFlight = false; setTimeout(poll, 10000); });
    }
    poll();
}

// Attachment markers the backend appends to the prompt, keyed by the label it
// emitted (see the workspace_ref branch in web_channel.post_message). History
// only persists the prompt text, so this is the only way back to a chip.
const ATTACHMENT_MARKER_TYPES = {
    '工作空间文件': 'workspace_ref', '工作空间檔案': 'workspace_ref', 'Workspace file': 'workspace_ref',
    '工作空间目录': 'workspace_dir', '工作空间目錄': 'workspace_dir', 'Workspace directory': 'workspace_dir',
    '图片': 'image', '圖片': 'image', 'Image': 'image',
    '视频': 'video', '影片': 'video', 'Video': 'video',
    '目录': 'directory', '目錄': 'directory', 'Directory': 'directory',
    '文件': 'file', '檔案': 'file', 'File': 'file',
};

/**
 * Split trailing `[label: path]` lines off a persisted user message.
 * Returns the remaining text plus the attachments they describe.
 */
function parseAttachmentMarkers(content) {
    const lines = (content || '').split('\n');
    const found = [];
    while (lines.length) {
        const line = lines[lines.length - 1].trim();
        if (!line) { lines.pop(); continue; }
        const m = line.match(/^\[([^\]:]+):\s*(.+)\]$/);
        const type = m && ATTACHMENT_MARKER_TYPES[m[1].trim()];
        if (!type) break;
        found.unshift({ type, path: m[2].trim() });
        lines.pop();
    }
    if (!found.length) return { text: content, attachments: null };
    return {
        text: lines.join('\n').trimEnd(),
        attachments: found.map(f => ({
            file_path: f.path,
            file_name: f.path.split(/[\\/]/).filter(Boolean).pop() || f.path,
            file_type: f.type === 'workspace_dir' ? 'workspace_ref' : f.type,
            is_dir: f.type === 'workspace_dir' || f.type === 'directory',
        })),
    };
}

function createUserMessageEl(content, timestamp, attachments) {
    const el = document.createElement('div');
    el.className = 'flex justify-end px-4 sm:px-6 py-3 user-message-group';

    // Replaying history: recover the chips from the markers left in the text.
    if (!attachments) {
        const parsed = parseAttachmentMarkers(content);
        if (parsed.attachments) {
            attachments = parsed.attachments;
            content = parsed.text;
        }
    }

    let attachHtml = '';
    if (attachments && attachments.length > 0) {
        const items = attachments.map(a => {
            if (a.file_type === 'image') {
                // History replay recovers attachments from prompt markers, which
                // carry only the local file_path — route it through /api/file.
                const src = (a.preview_url || _toWebUrl(a.file_path || '')).replace(/"/g, '&quot;');
                return `<img src="${src}" alt="${escapeHtml(a.file_name)}" class="user-msg-image" onclick="_openImageLightbox(this.src)">`;
            }
            const icon = a.file_type === 'video'
                ? 'fa-film'
                : (a.file_type === 'directory' ? 'fa-folder-tree'
                : (a.is_dir ? 'fa-folder' : 'fa-file-alt'));
            const suffix = a.file_type === 'directory' && a.file_count
                ? ` (${a.file_count})`
                : '';
            // Workspace references stay openable in the preview panel.
            const openable = a.file_type === 'workspace_ref'
                ? ` data-ws-open="${escapeHtml(a.file_path)}" title="${escapeHtml(a.file_path)}"`
                : '';
            return `<div class="user-msg-file${openable ? ' is-openable' : ''}"${openable}>` +
                `<i class="fas ${icon}"></i> ${escapeHtml(a.file_name)}${suffix}</div>`;
        }).join('');
        attachHtml = `<div class="user-msg-attachments">${items}</div>`;
    }

    const textHtml = content ? renderMarkdown(content) : '';
    el.innerHTML = `
        <div class="max-w-[75%] sm:max-w-[60%]">
            <div class="bg-primary-400 text-white rounded-2xl px-4 py-2.5 text-sm leading-relaxed msg-content user-bubble">
                ${attachHtml}${textHtml}
            </div>
            <div class="flex items-center justify-end gap-2 mt-1.5">
                <button class="edit-msg-btn text-xs text-slate-300 dark:text-slate-600 hover:text-primary-400 dark:hover:text-primary-400 transition-colors cursor-pointer" title="${t('edit_message')}">
                    <i class="fas fa-pen-to-square"></i>
                </button>
                <button class="delete-msg-btn text-xs text-slate-300 dark:text-slate-600 hover:text-red-500 dark:hover:text-red-400 transition-colors cursor-pointer" title="${t('delete_message_title')}">
                    <i class="fas fa-trash"></i>
                </button>
                <span class="text-xs text-slate-400 dark:text-slate-500">${formatTime(timestamp)}</span>
            </div>
        </div>
    `;
    // Store raw content for editing
    el.dataset.rawContent = content || '';
    highlightMentions(el.querySelector('.msg-content'));
    return el;
}

function renderToolCallsHtml(toolCalls) {
    if (!toolCalls || toolCalls.length === 0) return '';
    return toolCalls.map(tc => {
        const argsStr = formatToolArgs(tc.arguments || {});
        const resultStr = tc.result ? escapeHtml(String(tc.result)) : '';
        const hasResult = !!resultStr;
        return `
<div class="agent-step agent-tool-step">
    <div class="tool-header" onclick="this.parentElement.classList.toggle('expanded')">
        <i class="fas fa-check text-primary-400 flex-shrink-0 tool-icon"></i>
        <span class="tool-name">${escapeHtml(tc.name || '')}</span>
        <i class="fas fa-chevron-right tool-chevron"></i>
    </div>
    <div class="tool-detail">
        <div class="tool-detail-section">
            <div class="tool-detail-label">Input</div>
            <pre class="tool-detail-content">${argsStr}</pre>
        </div>
        ${hasResult ? `
        <div class="tool-detail-section tool-output-section">
            <div class="tool-detail-label">Output</div>
            <pre class="tool-detail-content">${resultStr}</pre>
        </div>` : ''}
    </div>
</div>`;
    }).join('');
}

// Cap for rendering reasoning content in the bubble. Beyond this size,
// we skip markdown rendering entirely and show plain text head + tail to
// keep the page responsive (very long chains-of-thought can otherwise
// stall or crash the browser when re-parsed by marked.js).
// Keep this in sync with backend MAX_STORED_REASONING_CHARS and
// MAX_REASONING_STREAM_CHARS so storage / SSE / display stay aligned.
const REASONING_RENDER_CAP = 4 * 1024; // 4 KB

function _truncateReasoningForDisplay(text) {
    if (!text || text.length <= REASONING_RENDER_CAP) return { text, truncated: false, omitted: 0 };
    const half = Math.floor(REASONING_RENDER_CAP / 2);
    const head = text.slice(0, half);
    const tail = text.slice(-half);
    return {
        text: head + '\n\n... [' + (text.length - head.length - tail.length) + ' chars omitted] ...\n\n' + tail,
        truncated: true,
        omitted: text.length - head.length - tail.length,
    };
}

function _renderReasoningBody(text) {
    // For short reasoning, render as markdown. For long ones, fall back to
    // an escaped <pre> block to avoid expensive markdown parsing.
    const { text: shown, truncated } = _truncateReasoningForDisplay(text);
    if (truncated || shown.length > REASONING_RENDER_CAP) {
        return '<pre class="thinking-stream-pre">' + escapeHtml(shown) + '</pre>';
    }
    return renderMarkdown(shown);
}

function finalizeThinking(el, startTime, text) {
    const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
    el.querySelector('.thinking-summary').textContent = t('thinking_done');
    const fullDiv = el.querySelector('.thinking-full');
    fullDiv.innerHTML = `<div class="thinking-duration">${t('thinking_duration')} ${elapsed}s</div>` + _renderReasoningBody(text);
}

function renderThinkingHtml(text) {
    if (!text || !text.trim()) return '';
    const full = text.trim();
    return `
<div class="agent-step agent-thinking-step">
    <div class="thinking-header" onclick="this.parentElement.classList.toggle('expanded')">
        <i class="fas fa-lightbulb text-amber-400 flex-shrink-0"></i>
        <span class="thinking-summary">${t('thinking_done')}</span>
        <i class="fas fa-chevron-right thinking-chevron"></i>
    </div>
    <div class="thinking-full">${_renderReasoningBody(full)}</div>
</div>`;
}

function renderStepsHtml(steps) {
    if (!steps || steps.length === 0) return { stepsHtml: '', finalContent: '' };

    // Find the index of the last content step — it becomes the main answer, not a step
    let lastContentIdx = -1;
    for (let i = steps.length - 1; i >= 0; i--) {
        if (steps[i].type === 'content') { lastContentIdx = i; break; }
    }

    let html = '';
    let lastContentText = '';
    for (let i = 0; i < steps.length; i++) {
        const step = steps[i];
        if (step.type === 'thinking') {
            html += renderThinkingHtml(step.content);
        } else if (step.type === 'content') {
            if (i === lastContentIdx) {
                lastContentText = step.content;
            } else {
                html += `<div class="agent-step agent-content-step"><div class="agent-content-body">${renderMarkdown(step.content)}</div></div>`;
            }
        } else if (step.type === 'tool') {
            const argsStr = formatToolArgs(step.arguments || {});
            const resultStr = step.result ? escapeHtml(String(step.result)) : '';
            const isErr = step.is_error === true;
            const iconClass = isErr
                ? 'fas fa-times text-red-400 flex-shrink-0 tool-icon'
                : 'fas fa-check text-primary-400 flex-shrink-0 tool-icon';
            // Same rule as the live stream: a tool that wrote its outcome for
            // a person shows that, not the form the model was handed.
            const outputHtml = step.display
                ? `<div class="tool-display-output has-content">${renderMarkdown(String(step.display))}</div>`
                : (resultStr
                    ? `<pre class="tool-detail-content${isErr ? ' tool-error-text' : ''}">${resultStr}</pre>`
                    : '');
            html += `
<div class="agent-step agent-tool-step${isErr ? ' tool-failed' : ''}">
    <div class="tool-header" onclick="this.parentElement.classList.toggle('expanded')">
        <i class="${iconClass}"></i>
        <span class="tool-name">${escapeHtml(step.name || '')}</span>
        <i class="fas fa-chevron-right tool-chevron"></i>
    </div>
    <div class="tool-detail">
        <div class="tool-detail-section">
            <div class="tool-detail-label">Input</div>
            <pre class="tool-detail-content">${argsStr}</pre>
        </div>
        ${outputHtml ? `
        <div class="tool-detail-section tool-output-section">
            <div class="tool-detail-label">${isErr ? 'Error' : 'Output'}</div>
            ${outputHtml}
        </div>` : ''}
    </div>
</div>`;
            // If this tool sent a file (send/read tool), render the media inline
            // so it persists across page refreshes (SSE-only file events are not stored).
            const mediaHtml = _renderSentFileFromToolResult(step);
            if (mediaHtml) html += mediaHtml;
        }
    }
    return { stepsHtml: html, lastContentText };
}

// Extract file-to-send metadata from a tool's result and render an inline preview.
// Returns '' if the result isn't a file_to_send payload.
function _renderSentFileFromToolResult(step) {
    if (!step || !step.result) return '';
    let payload;
    try {
        payload = typeof step.result === 'string' ? JSON.parse(step.result) : step.result;
    } catch (_) { return ''; }
    if (!payload || payload.type !== 'file_to_send' || !payload.path) return '';
    const webUrl = _toWebUrl(payload.path);
    const fileType = payload.file_type || 'file';
    const fileName = payload.file_name || payload.path.split('/').pop();
    if (fileType === 'image') {
        return `<div class="agent-step">${_buildImageHtml(webUrl)}</div>`;
    }
    if (fileType === 'video') {
        return `<div class="agent-step">${_buildVideoHtml(webUrl)}</div>`;
    }
    return `<div class="agent-step"><a href="${webUrl}" download="${escapeHtml(fileName)}" target="_blank" ` +
        `style="display:inline-flex;align-items:center;gap:6px;padding:8px 14px;margin:8px 0;border-radius:8px;` +
        `background:var(--bg-secondary,#f3f4f6);color:var(--text-primary,#374151);text-decoration:none;font-size:14px;` +
        `border:1px solid var(--border-color,#e5e7eb);">` +
        `<i class="fas fa-file-download" style="color:#6b7280;"></i> ${escapeHtml(fileName)}</a></div>`;
}

// Cosmetic translator for cancel markers persisted in history.
// History keeps the English canonical form for the LLM; only display is localized.
function localizeCancelMarker(text) {
    if (!text) return text;
    if (currentLang !== 'zh') return text;
    return text
        .replace(/_\(Cancelled by user\)_/g, '_(用户已中止)_')
        .replace(/_\(Cancelled\)_/g, '_(已中止)_');
}

function createBotMessageEl(content, timestamp, requestId, msg) {
    const el = document.createElement('div');
    el.className = 'flex gap-3 px-4 sm:px-6 py-3 bot-message-group';
    if (requestId) el.dataset.requestId = requestId;

    let stepsHtml = '';
    let displayContent = localizeCancelMarker(content);

    if (msg && msg.steps && msg.steps.length > 0) {
        // New format: ordered steps with interleaved content
        const result = renderStepsHtml(msg.steps);
        stepsHtml = result.stepsHtml;
        // The final content (last text after all steps) is the main answer
        displayContent = content || result.lastContentText;
    } else {
        // Legacy format: separate tool_calls + optional reasoning
        const toolCalls = msg && msg.tool_calls;
        const reasoning = msg && msg.reasoning;
        stepsHtml = renderThinkingHtml(reasoning) + renderToolCallsHtml(toolCalls);
    }

    // Files written this turn, as computed by the history API (workspace.js).
    const artifactsHtml = typeof renderArtifactCards === 'function'
        ? renderArtifactCards(msg && msg.artifacts)
        : '';

    // Self-evolution bubbles get a small badge so the user can feel the agent
    // learned something on its own (text itself stays clean). History replay
    // carries msg.kind; live pushes are identified by the evolution_ request id.
    const isEvolution = (msg && msg.kind === 'evolution')
        || (typeof requestId === 'string' && requestId.startsWith('evolution_'));
    const evolutionBadge = isEvolution
        ? `<div class="flex items-center gap-1 mb-1.5 text-xs text-slate-400 dark:text-slate-500">
                <i class="fas fa-seedling text-[11px]"></i>
                <span>${t('evolution_badge')}</span>
           </div>`
        : '';

    // The reply's face is whichever Agent spoke: its uploaded image, or the
    // product logo by default. A shared conversation also labels the bubble,
    // since consecutive bubbles can come from different Agents; a solo chat
    // stays unlabelled but still reflects that Agent's own avatar.
    const speaker = botSpeakerAgent(msg, requestId) || findAgent(activeAgentId);
    // Remember who spoke, so a later avatar change can repaint this exact face
    // without re-rendering the whole bubble.
    if (speaker && speaker.id) el.dataset.speakerAgent = speaker.id;
    const faceHtml = `<span class="bot-face">${agentAvatarHTML(speaker, 32)}</span>`;
    const speakerName = (sharedConversation() && speaker)
        ? `<div class="bot-speaker">${escapeHtml(speaker.name || speaker.id)}</div>`
        : '';

    el.innerHTML = `
        ${faceHtml}
        <div class="min-w-0 flex-1 max-w-[85%]">
            ${speakerName}
            <div class="bg-white dark:bg-[#1A1A1A] border border-slate-200 dark:border-white/10 rounded-2xl px-4 py-3 text-sm leading-relaxed msg-content text-slate-700 dark:text-slate-200">
                ${evolutionBadge}
                ${stepsHtml ? `<div class="agent-steps">${stepsHtml}</div>` : ''}
                <div class="answer-content">${renderMarkdown(displayContent)}</div>
                <div class="media-content">${artifactsHtml}</div>
                <div class="bot-audio-slot"></div>
            </div>
            <div class="flex items-center gap-2 mt-1.5">
                <span class="text-xs text-slate-400 dark:text-slate-500">${formatTime(timestamp)}</span>
                <button class="copy-msg-btn text-xs text-slate-300 dark:text-slate-600 hover:text-slate-500 dark:hover:text-slate-400 transition-colors cursor-pointer" title="${currentLang === 'zh' ? '复制' : 'Copy'}">
                    <i class="fas fa-copy"></i>
                </button>
                <button class="speak-msg-btn text-xs text-slate-300 dark:text-slate-600 hover:text-slate-500 dark:hover:text-slate-400 transition-colors cursor-pointer" title="${t('speak_msg')}" style="display:none;">
                    <i class="fas fa-volume-up"></i>
                </button>
                <button class="regenerate-msg-btn text-xs text-slate-300 dark:text-slate-600 hover:text-primary-400 dark:hover:text-primary-400 transition-colors cursor-pointer" title="${t('regenerate_response')}">
                    <i class="fas fa-rotate-right"></i>
                </button>
            </div>
        </div>
    `;
    el.querySelector('.answer-content').dataset.rawMd = displayContent;
    // Existing TTS attachment (history replay): mount the player up-front.
    const existingAudio = msg && msg.extras && msg.extras.audio && msg.extras.audio.url;
    if (existingAudio) {
        attachAudioToBotBubble(el, existingAudio, { autoplay: false });
    }
    renderBotSpeakerButton(el, displayContent);
    applyHighlighting(el);
    return el;
}

// Append (or replace) a small audio player inside a bot bubble's
// dedicated `.bot-audio-slot`. Used by both live TTS pushes and history
// replay. Silent failures: never throws.
function attachAudioToBotBubble(botEl, audioUrl, opts) {
    try {
        if (!botEl || !audioUrl) return;
        const slot = botEl.querySelector('.bot-audio-slot');
        if (!slot) return;
        slot.innerHTML = '';
        slot.style.marginTop = '6px';
        const pill = renderVoicePill(audioUrl, { autoplay: !!(opts && opts.autoplay) });
        slot.appendChild(pill);
        const speakBtn = botEl.querySelector('.speak-msg-btn');
        if (speakBtn) speakBtn.style.display = 'none';
    } catch (_) { /* silent */ }
}

function pendingVoiceAttachmentKey(sid, botSeq) {
    return `${sid}:${botSeq}`;
}

function rememberPendingVoiceAttachment(sid, botSeq, audioUrl) {
    if (!sid || botSeq === undefined || botSeq === null || !audioUrl) return;
    const key = pendingVoiceAttachmentKey(sid, botSeq);
    const pending = {
        sid,
        botSeq: String(botSeq),
        audioUrl,
        expiresAt: Date.now() + PENDING_VOICE_ATTACH_TTL_MS,
    };
    pendingVoiceAttachments.delete(key);
    pendingVoiceAttachments.set(key, pending);

    while (pendingVoiceAttachments.size > PENDING_VOICE_ATTACH_MAX) {
        pendingVoiceAttachments.delete(pendingVoiceAttachments.keys().next().value);
    }
    setTimeout(() => {
        if (pendingVoiceAttachments.get(key) === pending) {
            pendingVoiceAttachments.delete(key);
        }
    }, PENDING_VOICE_ATTACH_TTL_MS);
}

function flushPendingVoiceAttachments(sid, autoplay) {
    if (!sid || sid !== sessionId) return 0;
    const now = Date.now();
    let attached = 0;
    pendingVoiceAttachments.forEach((pending, key) => {
        if (pending.expiresAt <= now) {
            pendingVoiceAttachments.delete(key);
            return;
        }
        if (pending.sid !== sid) return;
        const botEl = Array.from(
            messagesDiv.querySelectorAll('.bot-message-group[data-seq]')
        ).find(el => el.dataset.seq === pending.botSeq);
        if (!botEl) return;
        attachAudioToBotBubble(botEl, pending.audioUrl, { autoplay: !!autoplay });
        pendingVoiceAttachments.delete(key);
        attached++;
    });
    return attached;
}

// Build a compact play/pause + progress + duration pill that wraps a
// hidden <audio>. Returns the root element; safe to embed anywhere.
function renderVoicePill(audioUrl, opts) {
    opts = opts || {};
    const wrap = document.createElement('div');
    wrap.className = 'voice-pill';
    wrap.innerHTML = `
        <button type="button" class="voice-pill-btn" data-state="play" aria-label="play">
            <i class="fas fa-play"></i>
        </button>
        <div class="voice-pill-track"><div class="voice-pill-fill"></div></div>
        <span class="voice-pill-time">0:00</span>
        <audio preload="metadata" src="${audioUrl}"></audio>
    `;
    const btn = wrap.querySelector('.voice-pill-btn');
    const fill = wrap.querySelector('.voice-pill-fill');
    const timeEl = wrap.querySelector('.voice-pill-time');
    const audio = wrap.querySelector('audio');

    const fmt = (s) => {
        if (!isFinite(s) || s < 0) s = 0;
        const m = Math.floor(s / 60);
        const r = Math.floor(s % 60);
        return `${m}:${r < 10 ? '0' : ''}${r}`;
    };
    const setIcon = (state) => {
        btn.dataset.state = state;
        btn.querySelector('i').className = state === 'pause' ? 'fas fa-pause' : 'fas fa-play';
        btn.setAttribute('aria-label', state === 'pause' ? 'pause' : 'play');
    };

    audio.addEventListener('loadedmetadata', () => {
        if (audio.duration && isFinite(audio.duration)) timeEl.textContent = fmt(audio.duration);
    });
    audio.addEventListener('timeupdate', () => {
        const dur = audio.duration || 0;
        if (dur > 0) {
            fill.style.width = `${Math.min(100, (audio.currentTime / dur) * 100)}%`;
            timeEl.textContent = fmt(dur - audio.currentTime);
        }
    });
    audio.addEventListener('ended', () => {
        setIcon('play');
        fill.style.width = '0%';
        timeEl.textContent = fmt(audio.duration || 0);
    });
    audio.addEventListener('play',  () => setIcon('pause'));
    audio.addEventListener('pause', () => setIcon('play'));

    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (audio.paused) {
            audio.play().catch(() => {});
        } else {
            audio.pause();
        }
    });

    if (opts.autoplay) {
        // Autoplay may be blocked by the browser; fall back silently and
        // let the user tap the play button.
        const tryPlay = () => audio.play().catch(() => {});
        if (audio.readyState >= 2) tryPlay();
        else audio.addEventListener('canplay', tryPlay, { once: true });
    }
    return wrap;
}

// Show the manual "read aloud" button when TTS is configured but the
// bubble has no audio yet. Lazily probes capability via /api/models so
// we don't expose the button when nothing can synthesize speech.
function renderBotSpeakerButton(botEl, text) {
    if (!botEl || !text || !text.trim()) return;
    const btn = botEl.querySelector('.speak-msg-btn');
    if (!btn) return;
    if (botEl.querySelector('.bot-audio-slot audio')) return;
    _isTtsReady().then(ready => {
        if (!ready) return;
        btn.style.display = '';
        btn.onclick = () => _triggerManualTts(btn, botEl, text);
    });
}

let _ttsReadyPromise = null;
let _ttsReadyTs = 0;
function _isTtsReady() {
    // Cache for 30s to avoid hammering /api/models on every bubble.
    if (_ttsReadyPromise && Date.now() - _ttsReadyTs < 30000) {
        return _ttsReadyPromise;
    }
    _ttsReadyTs = Date.now();
    _ttsReadyPromise = fetch('/api/models')
        .then(r => r.json())
        .then(data => {
            const tts = data && data.capabilities && data.capabilities.tts;
            if (!tts) return false;
            return Boolean(tts.current_provider || tts.suggested_provider);
        })
        .catch(() => false);
    return _ttsReadyPromise;
}

function _triggerManualTts(btn, botEl, text) {
    if (btn.dataset.busy === '1') return;
    btn.dataset.busy = '1';
    const icon = btn.querySelector('i');
    const prev = icon ? icon.className : '';
    if (icon) icon.className = 'fas fa-spinner fa-spin';
    fetch('/api/voice/tts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, session_id: sessionId }),
    })
        .then(r => r.json())
        .then(data => {
            if (data && data.status === 'success' && data.audio_url) {
                attachAudioToBotBubble(botEl, data.audio_url, { autoplay: true });
            }
        })
        .catch(() => {})
        .finally(() => {
            btn.dataset.busy = '0';
            if (icon) icon.className = prev || 'fas fa-volume-up';
        });
}

function addUserMessage(content, timestamp, attachments) {
    const el = createUserMessageEl(content, timestamp, attachments);
    messagesDiv.appendChild(el);
    _autoScrollEnabled = true;
    scrollChatToBottom(true);
}

function addBotMessage(content, timestamp, requestId) {
    const el = createBotMessageEl(content, timestamp, requestId);
    messagesDiv.appendChild(el);
    scrollChatToBottom();
}

// Load conversation history from the server (page 1 = most recent messages).
// Subsequent pages prepend older messages when the user scrolls to the top.
function loadHistory(page) {
    const historySessionId = sessionId;
    const historyAgentId = activeAgentId;
    const historyEpoch = _authEpoch;
    const historyTenantId = sessionStorage.getItem('cow_tenant_id');
    const context = JSON.stringify([historyEpoch, historyTenantId, historyAgentId, historySessionId]);
    if (historyLoading && _historyLoadContext === context) return;
    historyLoading = true;
    _historyLoadContext = context;
    const requestSeq = ++_historyLoadSeq;
    const current = () => requestSeq === _historyLoadSeq && historySessionId === sessionId
        && historyAgentId === activeAgentId && historyEpoch === _authEpoch
        && historyTenantId === sessionStorage.getItem('cow_tenant_id');

    // A shared conversation labels each bubble with its author and paints the
    // right face. That resolution needs this session's team roster (_sessCfg),
    // which loads asynchronously; without it every replayed bubble falls back
    // to the owner's avatar and loses its name. Make sure the roster is in hand
    // before rendering so a reload looks exactly like the live conversation.
    const ready = _sessCfg ? Promise.resolve() : refreshSessionSettings().catch(() => {});

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
        .then(data => {
            // A response from a session we have since left must never render
            // into the new session's message list.
            if (!current() || data.messages.length === 0) return;

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
                if (ctxStartSeq > 0 && !dividerInserted && msg._seq !== undefined && msg._seq >= ctxStartSeq) {
                    dividerInserted = true;
                    const divider = document.createElement('div');
                    divider.className = 'context-divider';
                    divider.innerHTML = `<span>${t('context_cleared')}</span>`;
                    fragment.appendChild(divider);
                }

                const ts = new Date(msg.created_at * 1000);
                const el = msg.role === 'user'
                    ? createUserMessageEl(msg.content, ts)
                    : createBotMessageEl(msg.content || '', ts, null, msg);
                // Store seq for delete functionality
                if (msg._seq !== undefined) {
                    el.dataset.seq = msg._seq;
                }
                fragment.appendChild(el);
            });

            // If context was cleared but no new messages exist yet, append divider at the end
            if (ctxStartSeq > 0 && !dividerInserted) {
                const divider = document.createElement('div');
                divider.className = 'context-divider';
                divider.innerHTML = `<span>${t('context_cleared')}</span>`;
                fragment.appendChild(divider);
            }

            // Prepend history above any existing messages
            const sentinel = document.getElementById('history-load-more');
            const insertBefore = sentinel ? sentinel.nextSibling : messagesDiv.firstChild;
            messagesDiv.insertBefore(fragment, insertBefore);
            updateEditButtonsState();
            // A background voice_attach can arrive before this history
            // fragment creates its target bubble. Retry now that seq metadata
            // is present in the DOM; do not autoplay delayed attachments.
            if (isFirstLoad) {
                flushPendingVoiceAttachments(historySessionId, false);
            }

            // Manage the "load more" sentinel at the very top
            if (data.has_more) {
                if (!document.getElementById('history-load-more')) {
                    const btn = document.createElement('div');
                    btn.id = 'history-load-more';
                    btn.className = 'flex justify-center py-3';
                    btn.innerHTML = `<button class="text-xs text-slate-400 dark:text-slate-500 hover:text-primary-400 transition-colors" onclick="loadHistory(historyPage + 1)">Load earlier messages</button>`;
                    messagesDiv.insertBefore(btn, messagesDiv.firstChild);
                }
            } else {
                const sentinel = document.getElementById('history-load-more');
                if (sentinel) sentinel.remove();
            }

            historyHasMore = data.has_more;
            historyPage = page;

            if (isFirstLoad) {
                // Scroll to the very bottom after the DOM settles. A single
                // rAF isn't enough: markdown/code-highlight/images keep growing
                // scrollHeight after the first paint, leaving the last bubble's
                // timestamp clipped. Re-pin a few times to catch late layout.
                requestAnimationFrame(() => scrollChatToBottom(true));
                [120, 350, 700].forEach(d => setTimeout(() => scrollChatToBottom(true), d));
            } else {
                // Restore scroll position so loading older messages doesn't jump the view
                messagesDiv.scrollTop = messagesDiv.scrollHeight - prevScrollHeight;
            }
        });
    })
        .catch(error => {
            if (!current()) return;
            console.warn('[history] Failed to open conversation', error);
            _wsToast(t('session_history_failed'));
        })
        .finally(() => {
            if (!current()) return;
            historyLoading = false;
            renderComposerIdentity();
        });
}

function addLoadingIndicator() {
    const el = document.createElement('div');
    el.className = 'flex gap-3 px-4 sm:px-6 py-3 loading-indicator';
    // Starts on the conversation's own Agent; setLoadingSpeaker swaps the face
    // once the server says who actually took the turn (an addressed teammate).
    el.innerHTML = `
        <span class="bot-face">${agentAvatarHTML(findAgent(activeAgentId), 32)}</span>
        <div class="bg-white dark:bg-[#1A1A1A] border border-slate-200 dark:border-white/10 rounded-2xl px-4 py-3">
            <div class="flex items-center gap-1.5">
                <span class="w-2 h-2 rounded-full bg-primary-400 animate-pulse-dot" style="animation-delay: 0s"></span>
                <span class="w-2 h-2 rounded-full bg-primary-400 animate-pulse-dot" style="animation-delay: 0.2s"></span>
                <span class="w-2 h-2 rounded-full bg-primary-400 animate-pulse-dot" style="animation-delay: 0.4s"></span>
            </div>
        </div>
    `;
    messagesDiv.appendChild(el);
    scrollChatToBottom();
    return el;
}

/* =====================================================================
   New-chat launch controls (change refine-sidebar-team-chat-launch)
   =====================================================================
   The session panel's 「新对话」 and the workbench sidebar's 「新建对话」 are the
   same control in two places, so they are declared once here: the body starts a
   chat immediately (never gated on a choice) and the caret opens the one picker
   that offers a solo chat per Agent plus the team entry. Sharing the surface
   table is what keeps the two menus from drifting into two different rosters,
   orders or failure behaviours.

   ``available`` is the only difference between them: the sidebar's caret is part
   of the refined layout, so it appears with the presentation switch, while the
   panel's caret has always been part of the session header. */
const NEW_CHAT_SURFACES = {
    panel: {
        // The session panel's own button. It is *not* ``new-chat-btn``: that id
        // belongs to the composer's plus control, and pointing focus restore at
        // a duplicate id would park focus on the composer instead of the header.
        control: 'history-new-chat-btn',
        caret: 'new-chat-caret',
        menu: 'new-chat-menu',
        start: () => newChat(true),
        available: () => true,
    },
    sidebar: {
        control: 'sidebar-new-chat',
        caret: 'sidebar-new-chat-caret',
        menu: 'sidebar-new-chat-menu',
        // The sidebar's own entry point: it keeps the branding/unsaved guard
        // and hands focus to the composer, which a bare newChat() would skip.
        start: () => startSidebarNewChat(),
        available: () => sidebarLaunchV2(),
    },
};

function _newChatSurface(name) {
    return NEW_CHAT_SURFACES[name] || NEW_CHAT_SURFACES.panel;
}

/** The name of the surface whose picker contains ``node``, or ``''``. */
function _newChatSurfaceOfMenu(node) {
    if (!node || typeof node.closest !== 'function') return '';
    return Object.keys(NEW_CHAT_SURFACES).find(name =>
        node.closest('#' + NEW_CHAT_SURFACES[name].menu)) || '';
}

/* The session-panel and sidebar launch buttons. Starting a chat is never a
   decision: the button opens one with the default-anchored Agent straight away,
   so a tenant that owns several Agents does not gate the primary action on a
   picker. The caret is the *optional* "switch Agent / start a team chat" entry,
   and only exists once there is more than one Agent. */
function onNewChatButton(event, surfaceName = 'panel') {
    const surface = _newChatSurface(surfaceName);
    const onCaret = event && event.target && event.target.closest
        && event.target.closest('#' + surface.caret);
    if (!onCaret) { surface.start(); return; }
    event.stopPropagation();
    openNewChatMenu(surface.menu, surfaceName);
}

/* The optional picker: a solo chat per Agent, or a team chat. */
function openNewChatMenu(menuId = 'new-chat-menu', surfaceName = 'panel') {
    const menu = document.getElementById(menuId);
    if (!menu) { _newChatSurface(surfaceName).start(); return; }
    if (!menu.classList.contains('hidden')) { menu.classList.add('hidden'); return; }
    // One control in two places: leaving the other picker hanging open behind a
    // freshly opened one would show two rosters at once.
    closeNewChatMenus(menuId);
    paintNewChatMenu(menu);
    menu.classList.remove('hidden');
}

/** Shut every new-chat picker except ``keep`` (a menu id). */
function closeNewChatMenus(keep = '') {
    Object.keys(NEW_CHAT_SURFACES).forEach(name => {
        const surface = NEW_CHAT_SURFACES[name];
        if (surface.menu !== keep) document.getElementById(surface.menu)?.classList.add('hidden');
    });
}

/* Keep both launch controls truthful about the roster: a caret appears only when
   there is something to choose between, and a picker that is already open is
   repainted so a use-range roster arriving after the first paint is reflected
   instead of leaving the narrower earlier reading on screen. */
function syncNewChatControls() {
    const choosable = multiAgentMode();
    Object.keys(NEW_CHAT_SURFACES).forEach(name => {
        const surface = NEW_CHAT_SURFACES[name];
        document.getElementById(surface.caret)?.classList.toggle('hidden', !(choosable && surface.available()));
        const menu = document.getElementById(surface.menu);
        if (menu && !menu.classList.contains('hidden')) paintNewChatMenu(menu);
    });
}

/* The picker rows, split out so an already-open menu can be repainted when the
   use-range roster arrives without toggling itself shut. */
function paintNewChatMenu(menu) {
    // The solo rows are the full use range, coding Agents included: switching to
    // one is a legitimate single-Agent action and must keep working (task 2.2).
    const rows = availableChatAgents().map(agent => `
        <button type="button" class="new-chat-item" onclick="startSoloChat('${escapeHtml(agent.id)}')">
            ${agentAvatarHTML(agent, 22)}
            <span>${escapeHtml(agent.name)}</span>
            ${agent.agent_type === 'coding' ? codingAgentTypeHint() : ''}
        </button>`).join('');
    // The team entry is never withheld because the current conversation happens
    // to be a coding one: starting a team only *prepares* an ordinary selection,
    // and the existing coding session survives until the user confirms (task
    // 3.2). The modal itself keeps coding Agents out of its candidates.
    const teamRow = `
        <div class="new-chat-sep"></div>
        <button type="button" class="new-chat-item new-chat-team"
                onclick="openTeamChatModal()">
            <span class="new-chat-team-ico"><i class="fas fa-user-group"></i></span>
            <span>${escapeHtml(t('new_team_chat'))}</span>
        </button>`;
    menu.innerHTML = `
        <div class="new-chat-section">${rows}</div>${teamRow}`;
}

function startSoloChat(agentId) {
    closeNewChatMenus();
    if (!agentId) { newChat(true); return; }
    activeAgentId = agentId;
    writeScopedPreference('cow_active_agent', activeAgentId);
    newChat(true);
    if (typeof resetWorkspaceToAgentRoot === 'function') resetWorkspaceToAgentRoot();
    renderComposerIdentity();
}

/* Help-site「一键体验」深链：/?open_agent=<skill>&message=... */
let _scenarioOpenHandled = false;

function resolveScenarioAgent(key) {
    if (!key) return '';
    const lists = [chatAgentCatalog, agentCatalog];
    for (let i = 0; i < lists.length; i += 1) {
        const list = lists[i] || [];
        if (!list.length) continue;
        const exact = list.find(a => a && a.id === key);
        if (exact) return exact.id;
        const prefixed = list.find(a => a && typeof a.id === 'string' && a.id.startsWith(key + '-'));
        if (prefixed) return prefixed.id;
        const bySkill = list.find(a => a && Array.isArray(a.skills) && a.skills.indexOf(key) !== -1);
        if (bySkill) return bySkill.id;
    }
    return '';
}

function consumeScenarioOpenLink() {
    if (_scenarioOpenHandled) return;
    let url;
    try {
        url = new URL(window.location.href);
    } catch (_) {
        return;
    }
    const key = url.searchParams.get('open_agent');
    if (!key) return;
    const message = url.searchParams.get('message') || '';
    const catalogReady = (chatAgentCatalog && chatAgentCatalog.length)
        || (agentCatalog && agentCatalog.length);
    if (!catalogReady) return;

    const agentId = resolveScenarioAgent(key);
    _scenarioOpenHandled = true;
    url.searchParams.delete('open_agent');
    url.searchParams.delete('message');
    if (typeof window.history !== 'undefined' && window.history.replaceState) {
        window.history.replaceState(null, '', url.toString());
    }
    if (!agentId) {
        console.warn('[scenario-open] agent not found for', key);
        return;
    }
    if (typeof navigateTo === 'function' && currentView !== 'chat') {
        navigateTo('chat');
    }
    startSoloChat(agentId);
    if (!message) return;
    window.setTimeout(function () {
        const input = document.getElementById('chat-input');
        if (!input) return;
        input.value = message;
        if (typeof sendMessage === 'function') sendMessage();
    }, 0);
}

/* =====================================================================
   Starting a team conversation
   =====================================================================
   Building a group is a transaction, not two optimistic steps (change
   refine-sidebar-team-chat-launch, design D4):

     1. the modal only *collects* a roster; nothing on screen changes,
     2. "start" saves that roster onto a freshly generated session id the page
        has not switched to yet -- this is the *prepare* step,
     3. only a saved roster *commits*: the workbench moves onto that id.

   A refused or failed write therefore leaves the conversation, its unsent
   draft, the history and the coding view exactly as they were, and the modal
   stays open to retry. Nothing is ever sent before the roster is on the
   server, which is what makes the first message already a group message.

   `_teamChatDraft` is the modal's whole state; `epoch` invalidates a pending
   continuation when the modal is closed or reopened mid-save. */
let _teamChatDraft = null;
// The control that opened the picker, so closing it can hand focus back. The
// sheet is a dialog: without this a keyboard user would be dropped into the
// page behind it (spec: 弹层支持 Escape 关闭及焦点恢复).
let _teamChatTrigger = null;

function _elementIsFocusable(el) {
    return !!el && typeof el.focus === 'function'
        && (!el.getClientRects || el.getClientRects().length > 0);
}

/* Focus comes back to the action that opened the sheet, never to an off-screen
   control: on a phone the sidebar is a drawer that this flow has already
   collapsed, so the visible drawer toggle is the honest target there. */
function _restoreTeamChatFocus() {
    const trigger = _teamChatTrigger;
    _teamChatTrigger = null;
    let target = _elementIsFocusable(trigger) ? trigger : null;
    if (target && target.getBoundingClientRect && typeof window !== 'undefined') {
        const rect = target.getBoundingClientRect();
        if (rect.right <= 0 || (window.innerWidth && rect.left >= window.innerWidth)) target = null;
    }
    if (!target && typeof window !== 'undefined' && window.innerWidth < 1024) {
        target = document.getElementById('menu-toggle');
    }
    if (_elementIsFocusable(target)) target.focus();
}

function _teamChatEscape(event) {
    if (!_teamChatDraft || event.key !== 'Escape') return;
    event.preventDefault();
    if (event.stopPropagation) event.stopPropagation();
    closeTeamChatModal();
}

/** The roster the picker is currently collecting (test/debug visibility). */
function teamChatDraft() {
    return _teamChatDraft;
}

function openTeamChatModal() {
    // Read before the pickers close: hiding the menu row that was clicked blurs
    // it, and focus has to come back to the launch control the user actually
    // used rather than to a row that no longer exists.
    const active = document.activeElement;
    const fromMenu = _newChatSurfaceOfMenu(active);
    const trigger = fromMenu ? document.getElementById(_newChatSurface(fromMenu).control) : active;
    closeNewChatMenus();
    // A picker opened from the phone drawer is a sheet over the whole viewport:
    // leaving the drawer up behind it would stack two layers the user has to
    // dismiss and hide the control that focus returns to.
    const sidebar = document.getElementById('sidebar');
    if (window.innerWidth < 1024 && trigger && sidebar
            && typeof sidebar.contains === 'function' && sidebar.contains(trigger)) {
        closeSidebar();
    }
    // A coding conversation is *not* replaced here: this modal only prepares an
    // ordinary roster over a session id nobody has opened yet, and the coding
    // pane is left at commit time (task 3.2). Keeping the entry available also
    // means the user never has to leave the coding chat to look at the picker.
    const start = initialTeamChatSelection();
    _teamChatDraft = {
        epoch: ((_teamChatDraft && _teamChatDraft.epoch) || 0) + 1,
        selected: start.ids,
        owner: start.owner,
        query: '',
        phase: 'editing',
        error: '',
    };
    const search = document.getElementById('team-chat-search');
    if (search) search.value = '';
    renderTeamChatDraft();
    document.getElementById('team-chat-modal')?.classList.remove('hidden');
    _teamChatTrigger = (typeof trigger === 'object') ? trigger : null;
    document.addEventListener('keydown', _teamChatEscape);
    if (search) search.focus();
}

/* Who the picker starts with. The Agent in front of the user wins when it may
   join a team at all; otherwise the unified default resolution decides. An
   unresolvable roster starts *empty* -- inventing a fallback here would be the
   "hard-coded default" the change explicitly forbids. */
function initialTeamChatSelection() {
    const eligible = new Set(teamCandidateAgents().map(a => a.id));
    if (activeAgentId && eligible.has(activeAgentId)) {
        return { ids: [activeAgentId], owner: activeAgentId };
    }
    const resolved = (typeof defaultResolution !== 'undefined' && defaultResolution
        && defaultResolution.agent_id) || defaultAgentId || '';
    if (resolved && eligible.has(resolved)) return { ids: [resolved], owner: resolved };
    return { ids: [], owner: '' };
}

function closeTeamChatModal({ restoreFocus = true } = {}) {
    // Dropping the draft is what invalidates an in-flight save: its continuation
    // checks that it still owns `_teamChatDraft`, so closing during the write
    // cancels the commit without touching the conversation on screen.
    _teamChatDraft = null;
    document.getElementById('team-chat-modal')?.classList.add('hidden');
    document.removeEventListener('keydown', _teamChatEscape);
    if (restoreFocus) _restoreTeamChatFocus();
    else _teamChatTrigger = null;
}

/** From the group-chat picker, jump to creating a new Agent. */
function openAgentCreateFromModal() {
    // Focus belongs to the create form this hands off to, not to the launch
    // button the sheet came from.
    closeTeamChatModal({ restoreFocus: false });
    navigateTo('agents');
    if (typeof openAgentCreateForm === 'function') openAgentCreateForm();
}

function toggleTeamChatPick(agentId) {
    const draft = _teamChatDraft;
    if (!draft || draft.phase !== 'editing') return;
    const i = draft.selected.indexOf(agentId);
    if (i === -1) {
        draft.selected.push(agentId);
        // Selecting is the conscious act that may also answer "who responds by
        // default"; nobody is picked for the user otherwise.
        if (!draft.owner) draft.owner = agentId;
    } else {
        draft.selected.splice(i, 1);
        // The default responder is never re-assigned silently: dropping it
        // leaves the field empty until the user says who takes its place
        // (design D3).
        if (draft.owner === agentId) draft.owner = '';
    }
    draft.error = '';
    renderTeamChatDraft();
}

/** Make an already-picked member the conversation's default responder. */
function setTeamChatOwner(agentId) {
    const draft = _teamChatDraft;
    if (!draft || draft.phase !== 'editing') return;
    if (!draft.selected.includes(agentId)) draft.selected.push(agentId);
    draft.owner = agentId;
    draft.error = '';
    renderTeamChatDraft();
}

function onTeamChatSearch(event) {
    const draft = _teamChatDraft;
    if (!draft) return;
    draft.query = (event && event.target && event.target.value) || '';
    renderTeamChatDraft();
}

/* Search matches name and 职责. The candidate rows carry the digital-employee
   projection (position/category/description), so all three are searched. */
function teamChatMatches(agent, query) {
    const q = String(query || '').trim().toLowerCase();
    if (!q) return true;
    const hay = [agent.name, agent.id, agent.position, agent.category, agent.description]
        .filter(Boolean).join(' ').toLowerCase();
    return q.split(/\s+/).every(part => hay.includes(part));
}

function renderTeamChatDraft() {
    const draft = _teamChatDraft;
    if (!draft) return;
    const eligible = teamCandidateAgents();
    const byId = new Map(eligible.map(a => [a.id, a]));
    // Whatever the roster was built from, only live candidates may be sent: an
    // id the use-range read no longer covers is dropped rather than submitted
    // and refused by the server (task 3.5).
    draft.selected = draft.selected.filter(id => byId.has(id));
    // An owner that stopped being a candidate or left the selection is cleared
    // rather than swapped for someone else.
    if (draft.owner && (!byId.has(draft.owner) || !draft.selected.includes(draft.owner))) draft.owner = '';

    renderTeamChatSelected(draft, byId);
    renderTeamChatRows(draft, eligible, byId);
    renderTeamChatFooter(draft, eligible);
}

function renderTeamChatSelected(draft, byId) {
    const box = document.getElementById('team-chat-selected');
    if (!box) return;
    if (!draft.selected.length) {
        box.innerHTML = `<span class="team-chat-selected-empty">${escapeHtml(t('team_chat_selected_none'))}</span>`;
        return;
    }
    box.innerHTML = draft.selected.map(id => {
        const agent = byId.get(id);
        const owner = id === draft.owner;
        return `<span class="team-chat-chip${owner ? ' owner' : ''}">
            ${agentAvatarHTML(agent, 20)}
            <span class="team-chat-chip-name">${escapeHtml(agent.name)}</span>
            ${owner ? `<span class="team-chat-chip-owner">${escapeHtml(t('new_team_chat_owner'))}</span>` : ''}
            <button type="button" class="team-chat-chip-x" aria-label="${escapeHtml(t('delete'))}"
                    onclick="toggleTeamChatPick('${escapeHtml(id)}')"><i class="fas fa-xmark"></i></button>
        </span>`;
    }).join('');
}

function renderTeamChatRows(draft, eligible, byId) {
    const list = document.getElementById('team-chat-list');
    if (!list) return;
    const rows = eligible.filter(agent => teamChatMatches(agent, draft.query));
    if (!rows.length) {
        list.innerHTML = `<div class="team-chat-empty">${escapeHtml(t(eligible.length ? 'team_chat_no_match' : 'agents_empty'))}</div>`;
        return;
    }
    list.innerHTML = rows.map(agent => {
        const on = draft.selected.includes(agent.id);
        const owner = draft.owner === agent.id;
        const meta = agent.position || agent.category || agent.description || '';
        return `<div class="team-chat-row${on ? ' on' : ''}" role="option" aria-selected="${on ? 'true' : 'false'}">
            <button type="button" class="team-chat-pick" onclick="toggleTeamChatPick('${escapeHtml(agent.id)}')">
                ${agentAvatarHTML(agent, 28)}
                <span class="team-chat-row-text">
                    <span class="team-chat-name">${escapeHtml(agent.name)}</span>
                    ${meta ? `<span class="team-chat-meta">${escapeHtml(meta)}</span>` : ''}
                </span>
                <span class="team-chat-check"><i class="fas ${on ? 'fa-circle-check' : 'fa-circle'}"></i></span>
            </button>
            ${on ? (owner
                ? `<span class="team-chat-owner">${escapeHtml(t('new_team_chat_owner'))}</span>`
                : `<button type="button" class="team-chat-owner-btn" onclick="setTeamChatOwner('${escapeHtml(agent.id)}')">${escapeHtml(t('new_team_chat_set_owner'))}</button>`) : ''}
        </div>`;
    }).join('');
}

function renderTeamChatFooter(draft, eligible) {
    const count = document.getElementById('team-chat-count');
    if (count) {
        count.textContent = t('team_chat_count')
            .replace('{picked}', String(draft.selected.length))
            .replace('{total}', String(eligible.length));
    }
    const start = document.getElementById('team-chat-start');
    if (start) start.disabled = draft.phase !== 'editing';
    const status = document.getElementById('team-chat-status');
    if (status) {
        status.textContent = draft.error
            || (draft.phase === 'saving' ? t('team_chat_preparing') : '');
    }
}

/* The client's own preconditions: at least two members, and a default responder
   that is one of them. Returns the error key, or '' when the roster is ready. */
function teamChatRosterProblem(draft) {
    const eligible = new Set(teamCandidateAgents().map(a => a.id));
    if (draft.selected.some(id => !eligible.has(id))) return 'team_chat_stale';
    if (draft.selected.length < 2) return 'new_team_chat_min';
    if (!draft.owner || !draft.selected.includes(draft.owner)) return 'new_team_chat_min';
    return '';
}

/** Collect-then-prepare-then-commit: see the block comment above. */
function startTeamChat() {
    const draft = _teamChatDraft;
    if (!draft || draft.phase !== 'editing') return;   // one attempt at a time
    const problem = teamChatRosterProblem(draft);
    if (problem) {
        draft.error = t(problem);
        renderTeamChatDraft();
        return;
    }
    // The roster is saved before anything moves, so the ordinary leave-page
    // protection is asked up front -- cancelling it changes nothing at all
    // (task 3.4). The same guard is asked again at commit time.
    if (typeof wsGuardUnsaved === 'function' && !wsGuardUnsaved(() => startTeamChat())) return;
    prepareTeamChatSession(draft);
}

function prepareTeamChatSession(draft) {
    // The target snapshot the write is addressed with -- and, on a retry, reused
    // verbatim rather than regenerated (design D4 step 3).
    const prepared = { sessionId: generateSessionId(), agentId: draft.owner };
    // A tenant switch or sign-out while the write is in flight must not commit
    // onto the identity that replaced this one.
    const epoch = _authEpoch;
    const tenant = sessionStorage.getItem('cow_tenant_id');
    const stillCurrent = () => epoch === _authEpoch && tenant === sessionStorage.getItem('cow_tenant_id');
    draft.phase = 'saving';
    draft.error = '';
    renderTeamChatDraft();
    setTeamMembers(draft.selected.filter(id => id !== prepared.agentId), prepared)
        .then(data => {
            // Closed, reopened or replaced while saving: the prepared session is
            // simply abandoned, uncommitted and invisible.
            if (_teamChatDraft !== draft || draft.phase !== 'saving') return;
            if (!stillCurrent()) return;
            commitTeamChatSession(draft, prepared, data);
        })
        .catch(err => {
            if (_teamChatDraft !== draft || draft.phase !== 'saving') return;
            // Nothing was committed, so the fail-safe state is the conversation
            // the user already had, with the modal open and the refusal spelled
            // out (task 3.6).
            draft.phase = 'editing';
            draft.error = (err && err.message) || t('session_settings_failed');
            _wsToast(draft.error);
            renderTeamChatDraft();
        });
}

/* Commit a prepared session: adopt the owner, move the workbench onto the
   prepared id, then let the server's own roster answer be what the composer
   renders (it is the authority on who actually joined). */
function commitTeamChatSession(draft, prepared, data) {
    const commit = () => {
        if (_teamChatDraft !== draft) return;
        closeTeamChatModal();
        activeAgentId = prepared.agentId;
        writeScopedPreference('cow_active_agent', activeAgentId);
        // Moving onto a prepared session is the same render path an ordinary new
        // chat takes; only the id is decided elsewhere.
        if (!commitPreparedSession(prepared.sessionId, { optimistic: true, inherit: true })) return;
        if (data) _sessCfg = { model: data.model, team: data.team };
        renderComposerIdentity();
        if (typeof _renderModelChip === 'function') _renderModelChip();
        if (typeof resetWorkspaceToAgentRoot === 'function') resetWorkspaceToAgentRoot();
        const input = document.getElementById('user-input');
        if (input) input.focus();
    };
    if (typeof wsGuardUnsaved === 'function' && !wsGuardUnsaved(commit)) return;
    commit();
}

function newChat(optimistic = true, inherit = true) {
    const seam = (typeof codingSeam === 'function') ? codingSeam() : null;
    // A new conversation for a coding Agent is a new Opencode session; the
    // ordinary pane is not prepared for it at all.
    if (seam && seam.isCodingAgent(activeAgentId)) {
        if (typeof wsGuardUnsaved === 'function'
            && !wsGuardUnsaved(() => newChat(optimistic, inherit))) return;
        seam.open(activeAgentId, '');
        return;
    }
    // A fresh session resets the preview panel, discarding an open editor.
    if (typeof wsGuardUnsaved === 'function'
        && !wsGuardUnsaved(() => newChat(optimistic, inherit))) return;
    commitPreparedSession(generateSessionId(), { optimistic, inherit });
}

/**
 * Move the workbench onto a session id that has already been decided.
 *
 * Ordinary new chats generate their id here; the team start generates it
 * earlier and *saves the roster onto it* before calling this, so the id must be
 * an input rather than something this function invents (design D4). Everything
 * else -- leaving a coding pane, the empty transcript, the workspace/settings
 * refresh, polling generation and the history row -- is shared, so a prepared
 * session is indistinguishable from any other new chat once committed.
 *
 * Returns false when the move was declined (the coding view's own leave guard),
 * which callers must treat as "nothing happened".
 */
function newChatLeavesCodingPane() {
    const seam = (typeof codingSeam === 'function') ? codingSeam() : null;
    // Leaving a coding conversation is also this path, and it may refuse: the
    // embedded app can hold unsaved edits of its own.
    return !seam || seam.leave() !== false;
}

function commitPreparedSession(preparedSessionId, { optimistic = true, inherit = true } = {}) {
    if (!newChatLeavesCodingPane()) return false;
    if (window.SceneOriginal) window.SceneOriginal.resetChat();

    // Do NOT close active streams: other sessions keep streaming in the
    // background (each stream self-guards against the foreign view) and their
    // replies still complete and persist.

    // Persist the id so the next page load also starts clean.
    sessionId = preparedSessionId;
    writeScopedPreference(activeSessionStorageKey(), sessionId);
    _sessCfg = null;
    if (!inherit) {
        _wsSelState = { current: null, recents: [], defaultWorkspace: '', projectsRoot: '' };
        _wsSelUpdateLabel();
    }
    refreshWorkspaceSelector();  // a fresh session starts on the default workspace
    refreshSessionSettings();    // ... and on the global model / permission
    if (typeof wsOnSessionSwitch === 'function') wsOnSessionSwitch();
    resetSendBtnSendMode();  // fresh session has no in-flight reply
    startPolling();  // bump generation so old loop self-cancels, new loop uses fresh sessionId
    messagesDiv.innerHTML = '';
    renderWelcomeScreen();
    renderComposerIdentity();
    if (currentView !== 'chat') navigateTo('chat');

    // A fresh session may not have a backend record until its first message, so
    // only prepend an optimistic item for a real new-chat action. After deleting
    // the current session it is skipped: the fresh session has no row yet, and
    // inserting one would leave an empty, undeletable item behind (deleting it
    // would just spawn another).
    const newSid = sessionId;
    if (_historyVisible) {
        if (optimistic) {
            loadSessionList(() => _addOptimisticSessionItem(newSid));
        } else {
            loadSessionList();
        }
    } else {
        // The list is hidden; mark it dirty so the next visit re-reads it.
        _historyDirty = true;
    }
    // A fresh session has no server-side context row yet: keep the usage entry
    // quiet until the first turn persists it.
    if (typeof _contextAfterSessionChange === 'function') _contextAfterSessionChange(false);
    return true;
}

// =====================================================================
// Session History (workbench page)
// =====================================================================

// The history page is the sole consumer of the session list now that the old
// collapsible panel is gone. `_historyVisible` tracks whether the page is the
// active view; `_historyDirty` marks a pending reload (a session changed while
// the page was hidden, or the page was left and needs a fresh read).
let _historyVisible = false;
let _historyDirty = false;

function _isMobileView() {
    return window.innerWidth <= 768;
}

// Swap the native `title` for the CSS tooltip so hints appear instantly
// instead of waiting for the browser's built-in delay.
function _setBtnTooltip(el, text) {
    if (!el) return;
    el.setAttribute('data-tooltip', text);
    el.removeAttribute('title');
}

function _applyInputTooltips() {
    const set = (id, key, pos) => {
        const el = document.getElementById(id);
        if (!el) return;
        _setBtnTooltip(el, t(key));
        if (pos) el.setAttribute('data-tooltip-pos', pos);
    };
    set('new-chat-btn', 'tip_new_chat');
    set('clear-context-btn', 'tip_clear_context');
    set('attach-btn', 'tip_attach');
    set('steer-btn', 'steer_active');
    set('workspace-toggle-btn', 'ws_toggle', 'bottom');
    // The history page's inline refresh button carries a translated tooltip.
    const historyRefresh = document.querySelector('.history-refresh-btn');
    if (historyRefresh) _setBtnTooltip(historyRefresh, t('ws_refresh'));
    // Optimize / mic buttons carry state-dependent tooltips managed in their
    // own setup, but on language switch we reset them to the idle label so the
    // tooltip follows the current locale.
    set('optimize-btn', 'optimize_idle_title');
    set('mic-btn', 'mic_idle_title');
    // Send button only carries a tooltip while it acts as the cancel button.
    _setBtnTooltip(sendBtn, sendBtnMode === 'cancel' ? t('tip_cancel') : '');
    // The model chip carries translated labels and tooltips, so it is repainted
    // here too (this runs on every language switch).
    _renderModelChip();
}

// A session that exists in the browser but not yet in the database: the user
// pressed "new chat" and has not sent the first message. Rendered from the same
// path as real sessions so it lands in the right group.
function _addOptimisticSessionItem(sid) {
    if (_historyQuery) return;
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
            ? { path: _wsSelState.current.path, name: _wsSelState.current.name }
            : null,
    });
    _renderSessionList();
}

function _sessionTimeGroup(ts) {
    const now = new Date();
    const d = new Date(ts * 1000);
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const yesterday = new Date(today); yesterday.setDate(today.getDate() - 1);
    if (d >= today) return t('today');
    if (d >= yesterday) return t('yesterday');
    return t('earlier');
}

let _sessionPage = 1;
let _sessionHasMore = false;
let _sessionLoading = false;
const _SESSION_PAGE_SIZE = 50;

// Every session loaded so far, in backend order (pinned first, then recency).
// Kept as data rather than only as DOM because grouping by project reorders the
// whole list, which cannot be done by appending page by page.
let _sessionItems = [];
// 'time' (今天/昨天/更早, the behavior before projects existed) or 'project'.
// The backend decides, based on how many spaces are in use across all sessions.
let _sessionGroupMode = 'time';
// User-chosen order of project spaces (paths + '__default__'), from the backend.
let _projectOrder = [];
// Sentinel the backend uses for the default workspace in the ordering.
const DEFAULT_SPACE_KEY = '__default__';

// Which project groups are collapsed, persisted per-browser so the choice
// survives reloads. Keyed by space key (project path or the default sentinel).
const _COLLAPSED_KEY = 'cow_collapsed_projects';
function _loadCollapsed() {
    try { return new Set(JSON.parse(localStorage.getItem(_COLLAPSED_KEY) || '[]')); }
    catch (e) { return new Set(); }
}
function _saveCollapsed(set) {
    try { localStorage.setItem(_COLLAPSED_KEY, JSON.stringify([...set])); } catch (e) {}
}
let _collapsedProjects = _loadCollapsed();

// Request-generation + context guard for the session list, so a late response
// from an earlier read (or one started under a different Agent / after a
// re-entry) is dropped instead of overwriting a fresher list.
let _sessionReqSeq = 0;
let _sessionReqAgent = '';
let _historyQuery = '';
let _historySearchTimer = null;
let _historySearchComposing = false;
let _historyTotal = null;
let _historyRequestController = null;
let _historyAuthGeneration = 0;
let _historyStatus = { key: '', message: '', error: false, retry: null };
let _historyPageFailed = false;
function _sessionListContext() {
    return JSON.stringify([_historyAuthGeneration, sessionStorage.getItem('cow_tenant_id') || '', activeAgentId, _historyQuery]);
}

function _renderHistoryStatus() {
    const status = document.getElementById('history-status');
    if (!status) return;
    const text = _historyStatus.key ? t(_historyStatus.key) : _historyStatus.message;
    status.textContent = text || '';
    status.classList.remove('opacity-0');
    status.classList.toggle('history-status-hidden', !text);
    status.classList.toggle('history-status-error', _historyStatus.error);
    status.setAttribute('role', _historyStatus.error ? 'alert' : 'status');
    if (_historyStatus.retry) {
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'history-status-actions';
        retry.textContent = t('session_history_retry');
        retry.addEventListener('click', _historyStatus.retry);
        status.appendChild(retry);
    }
}

function _setHistoryState(key, error = false, retry = null, message = '') {
    _historyStatus = { key, error, retry, message };
    _renderHistoryStatus();
}

function _setHistoryStatus(text, error) {
    _setHistoryState('', !!error, null, text || '');
}

function _setHistoryErrorWithRetry(text, retry = () => loadSessionList()) {
    _setHistoryState('', true, retry, text);
}

function _updateHistorySearchControls() {
    const input = document.getElementById('history-search-input');
    const clear = document.getElementById('history-search-clear');
    const summary = document.getElementById('history-search-summary');
    const refresh = document.getElementById('history-refresh-btn');
    if (input) input.setAttribute('aria-label', t('history_search_placeholder'));
    if (clear) {
        clear.classList.toggle('hidden', !(input && input.value));
        clear.setAttribute('aria-label', t('history_search_clear'));
    }
    if (refresh) {
        refresh.setAttribute('aria-label', t('history_refresh'));
        refresh.setAttribute('data-tooltip', t('history_refresh'));
    }
    if (summary) summary.textContent = _historyQuery && _historyTotal !== null
        ? t('history_search_count').replace('{count}', String(_historyTotal)) : '';
    const list = document.getElementById('session-list');
    if (list) list.setAttribute('aria-busy', String(_sessionLoading));
}

function _cancelHistoryRequest() {
    clearTimeout(_historySearchTimer);
    _historySearchTimer = null;
    if (_historyRequestController) _historyRequestController.abort();
    _historyRequestController = null;
    _sessionReqSeq++;
    _sessionLoading = false;
}

function _resetHistorySearch() {
    _cancelHistoryRequest();
    _historyAuthGeneration++;
    _historySearchComposing = false;
    _historyQuery = '';
    _historyTotal = null;
    _sessionItems = [];
    _sessionHasMore = false;
    _historyPageFailed = false;
    _historyDirty = true;
    const input = document.getElementById('history-search-input');
    if (input) input.value = '';
    _closeSessionActionMenu();
    _setHistoryState('');
    _renderSessionList();
    _updateHistorySearchControls();
}

function onHistorySearchCompositionStart() {
    _historySearchComposing = true;
    _cancelHistoryRequest();
}

function onHistorySearchCompositionEnd(event) {
    _historySearchComposing = false;
    onHistorySearchInput(event);
}

function _readHistorySearchQuery() {
    const input = document.getElementById('history-search-input');
    return input ? input.value.trim() : _historyQuery;
}

function _syncHistorySearchQuery() {
    const query = _readHistorySearchQuery();
    if (query === _historyQuery) return false;
    // The displayed value is authoritative. Restored/autofilled values need not
    // have emitted input, so every explicit submit and refresh also comes here.
    _cancelHistoryRequest();
    _historyQuery = query;
    _sessionItems = [];
    _historyTotal = null;
    _sessionHasMore = false;
    _historyPageFailed = false;
    _closeSessionActionMenu();
    _renderSessionList();
    _updateHistorySearchControls();
    return true;
}

function onHistorySearchInput(event) {
    if (event && event.isComposing) return;
    // A committed InputEvent can recover from a missed compositionend. Keep
    // the composition flag as a fallback only for events without this signal.
    if (event && event.isComposing === false) _historySearchComposing = false;
    if (_historySearchComposing) return;
    const changed = _syncHistorySearchQuery();
    if (!changed && (_sessionLoading || _historyTotal !== null)) {
        _updateHistorySearchControls();
        return;
    }
    if (!changed) _cancelHistoryRequest();
    const query = _historyQuery;
    if (Array.from(query).length > 100) {
        _setHistoryState('history_search_limit', true);
        return;
    }
    if (!query) return _submitHistorySearch();
    _setHistoryState('history_search_loading');
    _historySearchTimer = setTimeout(_submitHistorySearch, 300);
}

function _submitHistorySearch() {
    clearTimeout(_historySearchTimer);
    _historySearchTimer = null;
    if (_historySearchComposing) return;
    return loadSessionList();
}

function onHistorySearchChange(event) {
    if (event && event.isComposing) return;
    _historySearchComposing = false;
    const changed = _syncHistorySearchQuery();
    if (!changed && !_historySearchTimer && (_sessionLoading || _historyTotal !== null)) return;
    return _submitHistorySearch();
}

function onHistorySearchKeydown(event) {
    if (event.key === 'Enter' && event.isComposing === false && event.keyCode !== 229) {
        _historySearchComposing = false;
    }
    if (event.key === 'Enter' && !_historySearchComposing && !event.isComposing && event.keyCode !== 229) {
        event.preventDefault();
        _syncHistorySearchQuery();
        // An immediate submit consumes the debounce; repeated Enter while that
        // same query is loading must not start a duplicate request.
        if (!_sessionLoading) return _submitHistorySearch();
    } else if (event.key === 'Escape' && !_historySearchComposing) {
        event.preventDefault();
        return clearHistorySearch();
    }
}

function clearHistorySearch() {
    const input = document.getElementById('history-search-input');
    if (input) { input.value = ''; input.focus(); }
    _historySearchComposing = false;
    _cancelHistoryRequest();
    _historyQuery = '';
    _historyTotal = null;
    _sessionItems = [];
    _sessionHasMore = false;
    _renderSessionList();
    _updateHistorySearchControls();
    return _submitHistorySearch();
}

function loadSessionList(onDone) {
    const container = document.getElementById('session-list');
    if (!container || _historySearchComposing) return;
    if (container.querySelector('.session-title-input') || _dragSpaceKey !== null) {
        _historyDirty = true;
        return;
    }
    _syncHistorySearchQuery();
    if (Array.from(_historyQuery).length > 100) {
        _setHistoryState('history_search_limit', true);
        return;
    }

    // A fresh (re)load supersedes any in-flight read: reset loading so the new
    // request starts, and bump the sequence so a stale response is dropped.
    _cancelHistoryRequest();
    _sessionPage = 1;
    _sessionHasMore = false;
    _historyDirty = false;
    _historyPageFailed = false;
    _historyTotal = null;
    _sessionReqAgent = _sessionListContext();
    const seq = _sessionReqSeq;
    container.scrollTop = 0;

    return _fetchSessionPage(1, true, onDone, seq);
}

// Refresh the list for session operations that happen while the user may not be
// on the history page: reload only if it is the active view, otherwise mark it
// dirty so the next visit re-reads.
function _refreshHistoryList() {
    if (_historyVisible) loadSessionList();
    else _historyDirty = true;
    if (typeof loadSidebarRecentSessions === 'function') loadSidebarRecentSessions();
}

// === SIDEBAR_RECENT_BEGIN ===
/* What a conversation announces itself as, derived from the persisted owner
   badge and roster the list already carries — never guessed from the title
   (spec: 会话类型标识与成员恢复一致). Both the history rows and the sidebar
   preview read this one helper, so the two surfaces cannot disagree about what
   a conversation is.

   - Several Agents: overlapping faces plus the remaining member summary,
     exactly like the group it is.
   - A coding conversation keeps its own identity: its turns are served by the
     embedded app rather than by an ordinary turn, so it must not read as a
     plain chat.
   - One Agent: a plain chat row, pinned or not. */
function sessionTypeMarker(s) {
    const roster = (s && s.participants) || [];
    if (roster.length > 1) {
        const names = roster.map(a => a.name || a.id).filter(Boolean).join('、');
        // Three faces keep the row tidy; "+N" still states the group's size. The
        // stack is one image to assistive tech, named by the member summary, so
        // a screen reader hears who took part instead of a row of empty faces.
        const crowd = roster.slice(0, 3);
        const overflow = roster.length - 3;
        return {
            html: `<span class="session-faces" role="img" aria-label="${escapeHtml(names)}"`
                + ` title="${escapeHtml(names)}">`
                + crowd.map(a => agentAvatarHTML(a, 20)).join('')
                + (overflow > 0 ? `<span class="session-face-more">+${overflow}</span>` : '')
                + `</span>`,
            summary: names,
        };
    }
    const coding = !!(s && s.agent && s.agent.agent_type === 'coding');
    const icon = coding ? 'fa-code' : (s && s.pinned ? 'fa-thumbtack' : 'fa-message');
    return {
        html: `<i class="fas ${icon} session-icon" aria-hidden="true"></i>`,
        summary: '',
    };
}

const SIDEBAR_RECENT_LIMIT = 10;
function _sidebarRecentLimit(items) {
    return Array.isArray(items) ? items.slice(0, sidebarRecentLimitCount()) : [];
}
// The refined sidebar shows a tighter preview. The limit is a presentation
// value only: the rows come from the same authorized, pinned-first, most-recent
// ordering either way, and 查看全部 reaches everything past it.
const SIDEBAR_RECENT_LIMIT_V2 = 5;
function sidebarRecentLimitCount() {
    return (typeof sidebarLaunchV2 === 'function' && sidebarLaunchV2())
        ? SIDEBAR_RECENT_LIMIT_V2 : SIDEBAR_RECENT_LIMIT;
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

/* The type marker drawn before a sidebar row's title. It is a child of the
   open button rather than a sibling so a click anywhere on the row's face still
   opens the conversation, and so the label and its marker stay on one line. */
function setSidebarRowType(btn, s) {
    const marker = sessionTypeMarker(s);
    const glyph = document.createElement('span');
    glyph.className = 'sidebar-recent-type';
    glyph.innerHTML = marker.html;
    if (marker.summary) glyph.title = marker.summary;
    btn.insertBefore(glyph, btn.children[0] || null);
}

/* Give a sidebar row its title. Assigning textContent drops every child, so the
   title and the marker beside it are always written through here — at build
   time and on an in-place rename alike. The tooltip is the bare conversation
   title; the marker keeps its own member summary. */
function setSidebarRowTitle(btn, title) {
    const marker = btn.querySelector('.sidebar-recent-type');
    btn.textContent = title;
    btn.title = title;
    if (marker) btn.insertBefore(marker, btn.children[0] || null);
}

function renderSidebarRecentSessions() {
    const list = document.getElementById('sidebar-recent-list');
    const more = document.getElementById('sidebar-recent-more');
    if (!list) return;
    list.innerHTML = '';
    const items = _sidebarRecentLimit(_sidebarRecentItems);
    if (!items.length) {
        const empty = document.createElement('div');
        empty.className = 'sidebar-recent-empty';
        empty.textContent = t('sidebar_history_empty');
        list.appendChild(empty);
        if (more) more.classList.toggle('hidden', !_sidebarRecentViewAllAlways());
        return;
    }
    items.forEach(s => {
        const ownerId = (s.agent && s.agent.id) || '';
        const title = s.title || t('untitled_session');
        const isActive = s.session_id === sessionId && (!ownerId || ownerId === activeAgentId);

        // The row is a container, not a single button: the archive control is a
        // sibling so it can be reached by keyboard and never triggers the open
        // click that the main button owns.
        const row = document.createElement('div');
        row.className = 'sidebar-recent-row' + (isActive ? ' active' : '');
        row.setAttribute('role', 'listitem');
        row.dataset.sessionId = s.session_id || '';
        if (ownerId) row.dataset.agentId = ownerId;

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'sidebar-recent-item' + (isActive ? ' active' : '');
        // The title stays the button's own text so in-place renaming keeps
        // working by text alone; the type marker rides in front of it and is
        // re-attached by `setSidebarRowTitle` when the title changes.
        setSidebarRowTitle(btn, title);
        setSidebarRowType(btn, s);
        btn.dataset.sessionId = s.session_id || '';
        if (ownerId) btn.dataset.agentId = ownerId;
        btn.addEventListener('click', () => {
            switchSession(s.session_id, ownerId || undefined);
        });
        // Double-click (or F2 on the focused row) renames in place. A single
        // click still opens the conversation, and `switchSession` never
        // re-renders this list, so the dblclick lands on the same node.
        btn.addEventListener('dblclick', (event) => {
            event.preventDefault();
            event.stopPropagation();
            renameSidebarSession(s.session_id, ownerId);
        });
        btn.addEventListener('keydown', (event) => {
            if (event.key !== 'F2') return;
            event.preventDefault();
            event.stopPropagation();
            renameSidebarSession(s.session_id, ownerId);
        });

        // A visible entry point for the same in-place rename that the
        // double-click and F2 gestures trigger, sitting beside the archive
        // control. Order is destructive-ness ascending: rename, then archive.
        const rename = document.createElement('button');
        rename.type = 'button';
        rename.className = 'sidebar-recent-rename-btn';
        rename.setAttribute('aria-label', t('rename_session') + ': ' + title);
        rename.title = t('rename_session');
        rename.innerHTML = '<i class="fas fa-pen" aria-hidden="true"></i>';
        rename.addEventListener('click', (event) => {
            event.stopPropagation();
            renameSidebarSession(s.session_id, ownerId);
        });

        const archive = document.createElement('button');
        archive.type = 'button';
        archive.className = 'sidebar-recent-archive-btn';
        archive.setAttribute('aria-label', t('archive_session') + ': ' + title);
        archive.title = t('archive_session');
        archive.innerHTML = '<i class="fas fa-box-archive" aria-hidden="true"></i>';
        archive.addEventListener('click', (event) => {
            event.stopPropagation();
            archiveSidebarSession(s.session_id, ownerId);
        });

        row.appendChild(btn);
        row.appendChild(rename);
        row.appendChild(archive);
        list.appendChild(row);
    });
    if (more) {
        more.classList.toggle('hidden',
            !_sidebarRecentViewAllAlways() && items.length < 1);
    }
}

/* Whether 查看全部 stays available for a short (or empty) preview.
 *
 * It is the fixed entry to the full history page, so in the refined sidebar it
 * is offered whenever the section itself is offered — a member with two
 * conversations still needs a way into search, archiving and rename. The old
 * behaviour is kept for the old layout, where the row only appears once there
 * is something to expand.
 */
function _sidebarRecentViewAllAlways() {
    return typeof sidebarLaunchV2 === 'function' && sidebarLaunchV2();
}

function loadSidebarRecentSessions() {
    const wrap = document.getElementById('sidebar-recent');
    if (!wrap) return;
    if (typeof _navAreaFromPath === 'function' && _navAreaFromPath(location.pathname) !== 'workbench') return;
    // A withheld menu grant hides the block and skips the request entirely: never
    // fetch history the identity is not allowed to see in the navigation.
    const denied = typeof _sidebarRecentDenied === 'function' && _sidebarRecentDenied();
    if (wrap.classList.contains('hidden') || denied) {
        wrap.classList.add('hidden');
        return;
    }
    const seq = ++_sidebarRecentSeq;
    fetch(`/api/sessions?page=1&page_size=${sidebarRecentLimitCount()}&scope=all`)
        .then(async r => {
            const data = await r.json().catch(() => ({}));
            return { ok: r.ok, data };
        })
        .then(({ ok, data }) => {
            if (seq !== _sidebarRecentSeq) return;
            if (!ok || !data || data.status !== 'success') {
                _sidebarRecentItems = [];
                const list = document.getElementById('sidebar-recent-list');
                if (list) {
                    list.innerHTML = '';
                    const err = document.createElement('div');
                    err.className = 'sidebar-recent-error';
                    err.textContent = t('session_history_failed');
                    list.appendChild(err);
                }
                return;
            }
            _sidebarRecentItems = _sidebarRecentLimit(data.sessions || []);
            renderSidebarRecentSessions();
        })
        .catch(() => {
            if (seq !== _sidebarRecentSeq) return;
            _sidebarRecentItems = [];
            const list = document.getElementById('sidebar-recent-list');
            if (!list) return;
            list.innerHTML = '';
            const err = document.createElement('div');
            err.className = 'sidebar-recent-error';
            err.textContent = t('session_history_failed');
            list.appendChild(err);
        });
}

function _initSidebarRecent() {
    const wrap = document.getElementById('sidebar-recent');
    const toggle = document.getElementById('sidebar-recent-toggle');
    const label = document.getElementById('sidebar-recent-label');
    const more = document.getElementById('sidebar-recent-more');
    if (!wrap || !toggle) return;
    const setOpen = (open) => {
        wrap.classList.toggle('open', open);
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    };
    toggle.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        setOpen(!wrap.classList.contains('open'));
    });
    // Double-click the label to open the full history page (search/filter),
    // same as the previous top-level「历史对话」entry.
    label?.addEventListener('dblclick', (event) => {
        event.preventDefault();
        navigateTo('history');
    });
    more?.addEventListener('click', () => navigateTo('history'));
    // The archived view is a compact dialog rather than another workbench page:
    // restoring is rare and should not compete with the history page entry.
    const archivedBtn = document.getElementById('sidebar-recent-archived');
    archivedBtn?.addEventListener('click', (event) => {
        event.preventDefault();
        openArchivedSessionsModal();
    });
    loadSidebarRecentSessions();
}

// Archive one conversation from the sidebar. It disappears from history but
// keeps every message, its project binding and its pin until restored. The row
// is removed optimistically; a failed write puts it back and explains why.
function archiveSidebarSession(sessionId, agentId) {
    if (!sessionId) return;
    const owner = agentId || activeAgentId || '';
    const index = _sidebarRecentItems.findIndex(
        s => s.session_id === sessionId && (!owner || (s.agent && s.agent.id) === owner));
    const removed = index >= 0 ? _sidebarRecentItems.splice(index, 1)[0] : null;
    if (removed) renderSidebarRecentSessions();
    const restoreRow = () => {
        if (!removed) return;
        _sidebarRecentItems.splice(Math.min(index, _sidebarRecentItems.length), 0, removed);
        renderSidebarRecentSessions();
    };
    fetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ archived: true, agent_id: owner }),
    })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') {
                _wsToast(t('session_archived'));
                loadSidebarRecentSessions();
                return;
            }
            restoreRow();
            _wsToast(data.message || t('session_archive_failed'));
        })
        .catch(() => {
            restoreRow();
            _wsToast(t('session_archive_failed'));
        });
}

// Rename one sidebar conversation in place. Mirrors the history page's
// `renameSession`: Enter saves, Escape cancels, blur saves; success is silent
// and a failed write rolls the title back with a reason. Single-click still
// opens the conversation, so this never has to steal the open click.
function renameSidebarSession(sessionId, agentId) {
    if (!sessionId) return;
    const owner = agentId || activeAgentId || '';
    const list = document.getElementById('sidebar-recent-list');
    if (!list) return;
    const row = [...list.querySelectorAll('.sidebar-recent-row')].find(el =>
        el.dataset.sessionId === sessionId
        && (!owner || el.dataset.agentId === owner));
    if (!row) return;
    const btn = row.querySelector('.sidebar-recent-item');
    if (!btn || row.querySelector('.sidebar-recent-rename-input')) return;

    const entry = _sidebarRecentItems.find(s => s.session_id === sessionId
        && (!owner || (s.agent && s.agent.id) === owner));
    // The tooltip holds the bare title; the button's text may carry the marker's
    // "+N" too, so it is the fallback of last resort.
    const oldTitle = (entry && entry.title) || btn.title || btn.textContent || '';

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'sidebar-recent-rename-input';
    input.value = oldTitle;
    input.maxLength = 100;
    input.setAttribute('aria-label', t('rename_session'));

    // The row's main button owns the open click; interacting with the editor
    // must never bubble into it.
    const stop = event => event.stopPropagation();
    input.addEventListener('click', stop);
    input.addEventListener('mousedown', stop);

    // An input cannot legally nest inside a button, so hide the button and put
    // the editor beside it in the row.
    btn.classList.add('hidden');
    row.insertBefore(input, btn);
    input.focus();
    input.select();

    let done = false;
    const restore = (title) => {
        done = true;
        if (title !== undefined) setSidebarRowTitle(btn, title);
        input.remove();
        btn.classList.remove('hidden');
    };
    const revert = (title) => {
        if (entry) entry.title = title;
        setSidebarRowTitle(btn, title);
    };
    const commit = () => {
        if (done) return;
        const newTitle = input.value.trim();
        if (!newTitle || newTitle === oldTitle) { restore(oldTitle); return; }
        // Optimistic: the row and the cached entry both move to the new title.
        if (entry) entry.title = newTitle;
        restore(newTitle);
        fetch(`/api/sessions/${encodeURIComponent(sessionId)}?agent_id=${encodeURIComponent(owner)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: newTitle, agent_id: owner }),
        })
            .then(r => r.json())
            .then(data => {
                if (data.status === 'success') return;
                revert(oldTitle);
                _wsToast(data.message || t('session_settings_failed'));
            })
            .catch(() => {
                revert(oldTitle);
                _wsToast(t('session_settings_failed'));
            });
    };

    input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.isComposing && event.keyCode !== 229) {
            event.preventDefault();
            commit();
        } else if (event.key === 'Escape') {
            event.preventDefault();
            restore(oldTitle);
        }
    });
    input.addEventListener('blur', commit);
}

// === ARCHIVED_SESSIONS_BEGIN ===
let _archivedSessionsSeq = 0;
let _archivedSessionsBody = null;

// Local, dependency-free time label: the archived dialog lives in its own
// section, so it does not reach for the history page's formatter. ``typeof``
// keeps it usable when ``currentLang`` is absent (unit harnesses).
function _archivedTimeLabel(timestamp) {
    const ts = Number(timestamp);
    if (!ts) return '';
    const date = new Date(ts * 1000);
    if (Number.isNaN(date.getTime())) return '';
    const lang = typeof currentLang === 'undefined' ? 'zh' : currentLang;
    const locale = lang === 'en' ? 'en-US' : lang === 'zh-Hant' ? 'zh-TW' : 'zh-CN';
    const ymd = date.toLocaleDateString(locale, { year: 'numeric', month: '2-digit', day: '2-digit' });
    const hm = date.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit', hour12: false });
    return `${ymd} ${hm}`;
}

function _renderArchivedStatus(body, key, retry) {
    if (!body) return;
    body.innerHTML = '';
    const status = document.createElement('div');
    status.className = 'archived-sessions-status';
    status.textContent = t(key);
    body.appendChild(status);
    if (retry) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'archived-session-retry';
        button.textContent = t('archived_retry');
        button.addEventListener('click', () => _loadArchivedSessions(body));
        body.appendChild(button);
    }
}

function _renderArchivedSessions(body, sessions) {
    if (!body) return;
    if (!sessions.length) {
        _renderArchivedStatus(body, 'archived_empty', false);
        return;
    }
    body.innerHTML = '';
    sessions.forEach(s => {
        const ownerId = (s.agent && s.agent.id) || '';
        const title = s.title || t('untitled_session');
        const meta = [
            (s.agent && (s.agent.name || s.agent.id)) || '',
            (s.project && s.project.name) || '',
        ].filter(Boolean).join(' · ');

        const row = document.createElement('div');
        row.className = 'archived-session-row';
        row.dataset.sessionId = s.session_id || '';
        if (ownerId) row.dataset.agentId = ownerId;

        const copy = document.createElement('span');
        copy.className = 'archived-session-copy';
        const titleEl = document.createElement('span');
        titleEl.className = 'archived-session-title';
        titleEl.textContent = title;
        titleEl.title = title;
        copy.appendChild(titleEl);
        if (meta) {
            const metaEl = document.createElement('span');
            metaEl.className = 'archived-session-meta';
            metaEl.textContent = meta;
            copy.appendChild(metaEl);
        }
        const time = _archivedTimeLabel(s.last_active);
        if (time) {
            const timeEl = document.createElement('span');
            timeEl.className = 'archived-session-time';
            timeEl.textContent = time;
            copy.appendChild(timeEl);
        }

        const restore = document.createElement('button');
        restore.type = 'button';
        restore.className = 'archived-session-restore';
        restore.textContent = t('session_restore');
        restore.addEventListener('click', () => restoreArchivedSession(s.session_id, ownerId));

        row.appendChild(copy);
        row.appendChild(restore);
        body.appendChild(row);
    });
}

function _loadArchivedSessions(body) {
    if (!body) return;
    const seq = ++_archivedSessionsSeq;
    body.innerHTML = '';
    const loading = document.createElement('div');
    loading.className = 'archived-sessions-status';
    loading.textContent = t('archived_loading');
    body.appendChild(loading);
    fetch('/api/sessions?scope=all&archived=1&page=1&page_size=50')
        .then(async r => ({ ok: r.ok, data: await r.json().catch(() => ({})) }))
        .then(({ ok, data }) => {
            if (seq !== _archivedSessionsSeq) return;
            if (!ok || !data || data.status !== 'success') {
                _renderArchivedStatus(body, 'archived_load_failed', true);
                return;
            }
            _renderArchivedSessions(body, data.sessions || []);
        })
        .catch(() => {
            if (seq !== _archivedSessionsSeq) return;
            _renderArchivedStatus(body, 'archived_load_failed', true);
        });
}

function openArchivedSessionsModal() {
    const existing = document.getElementById('confirm-modal-overlay');
    if (existing) existing.remove();
    const overlay = document.createElement('div');
    overlay.id = 'confirm-modal-overlay';
    overlay.className = 'confirm-overlay';

    const modal = document.createElement('div');
    modal.className = 'confirm-modal archived-sessions-modal';
    const title = document.createElement('div');
    title.className = 'confirm-title';
    title.textContent = t('archived_sessions');
    const body = document.createElement('div');
    body.className = 'archived-sessions-body';
    body.id = 'archived-sessions-body';
    const actions = document.createElement('div');
    actions.className = 'confirm-actions';
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'confirm-btn confirm-btn-cancel';
    close.textContent = t('archived_close');
    actions.appendChild(close);
    modal.appendChild(title);
    modal.appendChild(body);
    modal.appendChild(actions);
    overlay.appendChild(modal);
    document.body.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add('visible'));

    const dismiss = () => {
        _archivedSessionsSeq++;
        _archivedSessionsBody = null;
        overlay.classList.remove('visible');
        setTimeout(() => overlay.remove(), 200);
    };
    overlay.addEventListener('click', (event) => { if (event.target === overlay) dismiss(); });
    close.addEventListener('click', dismiss);

    _archivedSessionsBody = body;
    _loadArchivedSessions(body);
}

function restoreArchivedSession(sessionId, agentId) {
    if (!sessionId) return;
    const owner = agentId || activeAgentId || '';
    fetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ archived: false, agent_id: owner }),
    })
        .then(r => r.json())
        .then(data => {
            if (data.status !== 'success') {
                _wsToast(data.message || t('session_restore_failed'));
                return;
            }
            _wsToast(t('session_restored'));
            loadSidebarRecentSessions();
            _loadArchivedSessions(_archivedSessionsBody);
        })
        .catch(() => _wsToast(t('session_restore_failed')));
}
// === ARCHIVED_SESSIONS_END ===

// Never run sidebar init inline during console.js evaluation: deferred scripts
// can already be past `loading`, and sync init may call render paths that
// reference lets declared later in this file.
queueMicrotask(() => {
    try { _initSidebarRecent(); } catch (err) { console.error('[sidebar-recent]', err); }
});

function _fetchSessionPage(page, clear, onDone, seq) {
    if (_sessionLoading) return;
    const existingList = document.getElementById('session-list');
    if (existingList && (existingList.querySelector('.session-title-input') || _dragSpaceKey !== null)) {
        _historyDirty = true;
        return;
    }
    if (!seq) seq = ++_sessionReqSeq;
    // A re-entry or identity change invalidates prior reads: drop the request so
    // a stale result cannot repaint the current list.
    if (seq !== _sessionReqSeq) return;
    _sessionLoading = true;
    _historyPageFailed = false;
    _setHistoryState(_historyQuery ? 'history_search_loading' : 'session_history_loading');
    _updateHistorySearchControls();

    const container = document.getElementById('session-list');
    if (!container) { _sessionLoading = false; return; }
    const ctx = _sessionListContext();
    const query = _historyQuery;
    const controller = new AbortController();
    _historyRequestController = controller;
    const current = () => seq === _sessionReqSeq && ctx === _sessionListContext();
    const fail = (key, message) => {
        if (!current()) return;
        _sessionLoading = false;
        _historyRequestController = null;
        if (container.querySelector('.session-title-input') || _dragSpaceKey !== null) {
            _historyDirty = true;
            _setHistoryState('');
            _updateHistorySearchControls();
            return;
        }
        _historyPageFailed = true;
        if (clear) { _sessionItems = []; _historyTotal = null; }
        _setHistoryState(key, true, () => _fetchSessionPage(page, clear, onDone, seq), message);
        _renderSessionList();
        _updateHistorySearchControls();
    };
    const url = `/api/sessions?page=${page}&page_size=${_SESSION_PAGE_SIZE}&scope=all`
        + (query ? `&q=${encodeURIComponent(query)}` : '');

    return fetch(url, { signal: controller.signal })
        .then(async r => {
            const data = await r.json();
            if (r.status === 403 || r.status === 503) data._historyUnavailable = true;
            return data;
        })
        .then(data => {
            // Late / stale result: the page changed or the identity moved on.
            if (!current()) return;
            // Editing can begin after this request was sent. Defer its result
            // rather than replacing a focused editor or a dragged project.
            if (container.querySelector('.session-title-input') || _dragSpaceKey !== null) {
                _sessionLoading = false;
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

            if (clear) _sessionItems = [];

            const sessions = data.sessions || [];
            _sessionPage = page;
            _sessionHasMore = !!data.has_more;
            _historyTotal = Number.isFinite(data.total) ? data.total : null;
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

            // First-page (full) reloads paint the list state; subsequent-page
            // loads keep whatever is already confirmed on screen.
            _setHistoryState(_sessionItems.length ? '' : query ? 'history_search_empty' : 'session_history_empty');
            _renderSessionList();
            _updateHistorySearchControls();
            if (typeof onDone === 'function') onDone();
            // A tall screen may not produce a scroll event after the first page.
            // Fill until scrolling is possible or all matching sessions arrived.
            requestAnimationFrame(() => {
                if (current() && _historyVisible && _sessionHasMore && !_sessionLoading
                        && container.clientHeight > 0 && container.scrollHeight <= container.clientHeight + 60) {
                    _fetchSessionPage(_sessionPage + 1, false, undefined, seq);
                }
            });
        })
        .catch(error => {
            if (error.name !== 'AbortError') fail('session_history_failed');
        });
}

// Split the loaded sessions into ordered, labelled groups.
//
// Time mode keeps the original today/yesterday/earlier buckets, with one
// addition: pinned conversations move into a group of their own at the top,
// because a pin that stayed inside its date bucket would not be findable.
// Project mode groups by workspace instead, and pins float to the top of their
// own project - that is where the user filed them.
function _sessionGroups() {
    const groups = [];
    const bucket = (key, label, icon, hint, isProject) => {
        let g = groups.find(x => x.key === key);
        if (!g) { g = { key, label, icon, hint, isProject, items: [] }; groups.push(g); }
        return g;
    };

    if (_sessionGroupMode === 'project') {
        // `_sessionItems` is already pinned-first / newest-first, so appending in
        // order gives each project the same ordering for free.
        _sessionItems.forEach(s => {
            const key = s.project ? s.project.path : DEFAULT_SPACE_KEY;
            const name = s.project ? s.project.name : t('ws_default_workspace');
            const icon = s.project ? 'fa-folder' : 'fa-house';
            bucket(key, name, icon, s.project ? s.project.path : '', !!s.project).items.push(s);
        });
        // Sort groups by the user's chosen order; spaces without a saved
        // position keep their natural (recency) order after the ordered ones.
        if (_projectOrder.length) {
            const rank = new Map(_projectOrder.map((k, i) => [k, i]));
            groups.sort((a, b) => {
                const ra = rank.has(a.key) ? rank.get(a.key) : Infinity;
                const rb = rank.has(b.key) ? rank.get(b.key) : Infinity;
                return ra - rb;
            });
        }
        return groups;
    }

    const pinned = _sessionItems.filter(s => s.pinned);
    if (pinned.length) {
        bucket('__pinned__', t('session_pinned_group'), 'fa-thumbtack', '', false).items.push(...pinned);
    }
    _sessionItems.filter(s => !s.pinned).forEach(s => {
        const label = _sessionTimeGroup(s.last_active);
        bucket('time:' + label, label, '', '', false).items.push(s);
    });
    return groups;
}

function _renderSessionList() {
    const container = document.getElementById('session-list');
    if (!container) return;
    _closeSessionActionMenu();

    if (!_sessionItems.length) {
        // The state (empty / error / loading) is shown in the status line above;
        // the list itself is left blank.
        container.innerHTML = '';
        return;
    }

    container.innerHTML = '';
    if (_historyQuery) {
        _sessionItems.forEach(s => container.appendChild(_sessionItemEl(s, false)));
        return;
    }
    const projectMode = _sessionGroupMode === 'project';
    // Indent sessions under their project header when several projects are
    // shown, so the list reads as a tree aligned to the folder icon above.
    const indentItems = projectMode && _sessionGroups().length > 1;
    _sessionGroups().forEach(group => {
        const collapsed = projectMode && _collapsedProjects.has(group.key);
        const header = document.createElement('div');
        header.className = 'session-group-label' + (projectMode ? ' session-group-project' : '');
        if (group.hint) header.title = group.hint;

        if (projectMode) {
            // A collapsible, draggable project header. The default space has no
            // rename/delete actions (there is no record to edit) but still drags.
            header.draggable = true;
            header.dataset.spaceKey = group.key;
            const isDefault = group.key === DEFAULT_SPACE_KEY;
            const actions = isDefault ? '' : `
                <button class="session-group-action" title="${escapeHtml(t('project_rename'))}"
                        onclick="event.stopPropagation(); renameProject('${_wsAttr(group.key)}','${_wsAttr(group.label)}')">
                    <i class="fas fa-pen"></i>
                </button>
                <button class="session-group-action" title="${escapeHtml(t('project_delete'))}"
                        onclick="event.stopPropagation(); deleteProject('${_wsAttr(group.key)}','${_wsAttr(group.label)}')">
                    <i class="fas fa-trash-can"></i>
                </button>`;
            header.innerHTML = `
                <i class="fas fa-chevron-down session-group-caret ${collapsed ? 'collapsed' : ''}"></i>
                <i class="fas ${group.icon} session-group-icon"></i>
                <span class="session-group-name">${escapeHtml(group.label)}</span>
                <span class="session-group-count">${group.items.length}</span>
                <span class="session-group-actions">${actions}</span>`;
            header.addEventListener('click', () => _toggleProjectCollapse(group.key));
            _wireGroupDrag(header, group.key);
        } else if (group.icon) {
            header.innerHTML = `<i class="fas ${group.icon}"></i><span>${escapeHtml(group.label)}</span>`;
        } else {
            header.textContent = group.label;
        }
        container.appendChild(header);

        if (!collapsed) {
            group.items.forEach(s => container.appendChild(_sessionItemEl(s, indentItems)));
        }
    });
}

function _toggleProjectCollapse(key) {
    if (_collapsedProjects.has(key)) _collapsedProjects.delete(key);
    else _collapsedProjects.add(key);
    _saveCollapsed(_collapsedProjects);
    _renderSessionList();
}

// --- Project group drag-to-reorder -------------------------------------------
function _wireGroupDrag(header, key) {
    header.addEventListener('dragstart', (e) => {
        _dragSpaceKey = key;
        header.classList.add('dragging');
        try { e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', key); } catch (err) {}
    });
    header.addEventListener('dragend', () => {
        _dragSpaceKey = null;
        header.classList.remove('dragging');
        if (_historyDirty) { _historyDirty = false; _refreshHistoryList(); }
        document.querySelectorAll('.session-group-project.drop-target')
            .forEach(el => el.classList.remove('drop-target'));
    });
    header.addEventListener('dragover', (e) => {
        if (_dragSpaceKey === null || _dragSpaceKey === key) return;
        e.preventDefault();
        header.classList.add('drop-target');
    });
    header.addEventListener('dragleave', () => header.classList.remove('drop-target'));
    header.addEventListener('drop', (e) => {
        e.preventDefault();
        header.classList.remove('drop-target');
        if (_dragSpaceKey === null || _dragSpaceKey === key) return;
        _reorderSpace(_dragSpaceKey, key);
    });
}

// Move `fromKey` to sit just before `beforeKey`, then persist the new order.
function _reorderSpace(fromKey, beforeKey) {
    // Start from the currently displayed group order so dragging is stable even
    // when some spaces have no saved position yet.
    const current = _sessionGroups().map(g => g.key);
    const order = current.filter(k => k !== fromKey);
    const idx = order.indexOf(beforeKey);
    if (idx < 0) order.push(fromKey);
    else order.splice(idx, 0, fromKey);

    _projectOrder = order;
    _renderSessionList();

    fetch('/api/projects/order', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ order }),
    }).catch(() => {});
}

// Rename a project (display name only; the folder on disk is untouched).
function renameProject(path, currentName) {
    showPromptModal(t('project_rename_title'), currentName, (name) => {
        if (name === null) return;
        fetch('/api/projects/manage', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path, name }),
        })
            .then(r => r.json())
            .then(data => {
                if (data.status !== 'success') { _wsToast(data.message || t('session_settings_failed')); return; }
                _refreshHistoryList();
            })
            .catch(() => _wsToast(t('session_settings_failed')));
    });
}

// Delete a project record. Only the RongAI record is removed; files stay and
// bound sessions revert to the default workspace.
function deleteProject(path, name) {
    showConfirmModal(
        t('project_delete_title'),
        t('project_delete_confirm').replace('{name}', name || path),
        () => {
            fetch('/api/projects/manage', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path }),
            })
                .then(r => r.json())
                .then(data => {
                    if (data.status !== 'success') { _wsToast(data.message || t('session_settings_failed')); return; }
                    _refreshHistoryList();
                })
                .catch(() => _wsToast(t('session_settings_failed')));
        }
    );
}

function _historyTimeLabel(timestamp) {
    const date = new Date(Number(timestamp) * 1000);
    if (!timestamp || Number.isNaN(date.getTime())) return { text: '', full: '' };
    const now = new Date();
    const locale = currentLang === 'en' ? 'en-US' : currentLang === 'zh-Hant' ? 'zh-TW' : 'zh-CN';
    const today = date.toDateString() === now.toDateString();
    return {
        text: today ? date.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit', hour12: false })
            : date.toLocaleDateString(locale, { month: '2-digit', day: '2-digit', ...(date.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}) }),
        full: date.toLocaleString(locale),
    };
}

function _closeSessionActionMenu(restoreFocus = false) {
    if (_sessionMenuCleanup) _sessionMenuCleanup(restoreFocus);
    _sessionMenuCleanup = null;
    if (_sessionActionMenu) _sessionActionMenu.remove();
    _sessionActionMenu = null;
}

function _openSessionActionMenu(event, session, trigger) {
    event.stopPropagation();
    const wasOpen = trigger.getAttribute('aria-expanded') === 'true';
    _closeSessionActionMenu();
    if (wasOpen) return;
    const owner = (session.agent && session.agent.id) || activeAgentId;
    const menu = document.createElement('div');
    menu.className = 'session-action-menu';
    menu.setAttribute('role', 'menu');
    menu.setAttribute('aria-label', t('history_more'));
    const actions = [
        [session.pinned ? 'unpin_session' : 'pin_session', 'fa-thumbtack', () => toggleSessionPin(session.session_id, owner)],
        ['rename_session', 'fa-pen', () => renameSession(session.session_id, owner)],
        ['agents_delete', 'fa-trash-can', () => deleteSession(session.session_id, owner)],
    ];
    actions.forEach(([label, icon, action], index) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'session-action-menu-item' + (index === 2 ? ' danger' : '');
        button.setAttribute('role', 'menuitem');
        button.innerHTML = `<i class="fas ${icon}" aria-hidden="true"></i><span>${escapeHtml(t(label))}</span>`;
        button.addEventListener('click', e => {
            e.stopPropagation();
            _closeSessionActionMenu(index !== 1);
            action();
        });
        menu.appendChild(button);
    });
    document.body.appendChild(menu);
    _sessionActionMenu = menu;
    trigger.setAttribute('aria-expanded', 'true');
    const rect = trigger.getBoundingClientRect();
    const bounds = menu.getBoundingClientRect();
    menu.style.left = Math.max(8, Math.min(rect.right - bounds.width, window.innerWidth - bounds.width - 8)) + 'px';
    menu.style.top = Math.max(8, rect.bottom + bounds.height + 6 <= window.innerHeight
        ? rect.bottom + 6 : rect.top - bounds.height - 6) + 'px';
    const dismissOutside = e => { if (!menu.contains(e.target) && !trigger.contains(e.target)) _closeSessionActionMenu(); };
    const dismiss = () => _closeSessionActionMenu();
    const keydown = e => {
        const buttons = [...menu.querySelectorAll('button')];
        const index = buttons.indexOf(document.activeElement);
        if (e.key === 'Escape') { e.preventDefault(); _closeSessionActionMenu(true); }
        else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            buttons[(index + (e.key === 'ArrowDown' ? 1 : buttons.length - 1)) % buttons.length].focus();
        } else if (e.key === 'Tab') _closeSessionActionMenu(true);
    };
    document.addEventListener('pointerdown', dismissOutside);
    window.addEventListener('resize', dismiss);
    window.addEventListener('scroll', dismiss, true);
    menu.addEventListener('keydown', keydown);
    _sessionMenuCleanup = restore => {
        trigger.setAttribute('aria-expanded', 'false');
        document.removeEventListener('pointerdown', dismissOutside);
        window.removeEventListener('resize', dismiss);
        window.removeEventListener('scroll', dismiss, true);
        if (restore && trigger.isConnected) trigger.focus();
    };
    menu.querySelector('button').focus({ preventScroll: true });
}

function _sessionItemEl(s, indent) {
    const item = document.createElement('div');
    const ownerId = (s.agent && s.agent.id) || '';
    const isActive = s.session_id === sessionId && (!ownerId || ownerId === activeAgentId);
    item.className = 'session-item' + (isActive ? ' active' : '') + (s.pinned ? ' pinned' : '')
        + (indent ? ' session-item-indent' : '');
    item.dataset.sessionId = s.session_id;
    if (ownerId) item.dataset.agentId = ownerId;

    const title = s.title || t('untitled_session');
    // Faces mark a conversation that has several Agents in it, the way a group
    // chat is distinguishable from a direct one; a single-Agent conversation
    // stays a plain row. Shared with the sidebar preview so both agree.
    const face = sessionTypeMarker(s).html;
    const agentName = (s.agent && (s.agent.name || s.agent.id)) || t('agents_default');
    const projectName = s.project && s.project.name || t('ws_default_workspace');
    const time = _historyTimeLabel(s.last_active);
    item.innerHTML = `
        <button type="button" class="session-row-main">
            ${face}
            <span class="session-copy">
                <span class="session-title-line"><span class="session-title" title="${escapeHtml(title)}">${escapeHtml(title)}</span>
                    ${isActive ? `<span class="session-current-label">${escapeHtml(t('history_current'))}</span>` : ''}</span>
                <span class="session-meta">${escapeHtml(agentName)} · ${escapeHtml(projectName)}</span>
            </span>
        </button>
        <span class="session-time" title="${escapeHtml(time.full)}">${escapeHtml(time.text)}</span>
        <button type="button" class="session-more-btn" aria-haspopup="menu" aria-expanded="false"
                aria-label="${escapeHtml(t('history_more') + ': ' + title)}"><i class="fas fa-ellipsis" aria-hidden="true"></i></button>
    `;
    item.querySelector('.session-row-main').addEventListener('click', () => switchSession(s.session_id, ownerId || undefined));
    const more = item.querySelector('.session-more-btn');
    more.addEventListener('click', e => _openSessionActionMenu(e, s, more));
    return item;
}

// Pin / unpin, then re-render so the conversation moves to its new place.
// Reorder loaded sessions to match the backend's ordering (pinned first, then
// most-recently-active), so an optimistic pin/unpin lands in the right place
// without waiting for a reload. Stable within each bucket.
function _sortSessionItems() {
    _sessionItems.sort((a, b) => {
        const pa = a.pinned ? 1 : 0;
        const pb = b.pinned ? 1 : 0;
        if (pa !== pb) return pb - pa;
        return (b.last_active || 0) - (a.last_active || 0);
    });
}

function toggleSessionPin(sid, agentId) {
    const entry = _sessionItems.find(s => s.session_id === sid && (!agentId || (s.agent && s.agent.id) === agentId));
    if (!entry) return;
    const pinned = !entry.pinned;

    // Move it optimistically: the reorder is the whole point of the click, and
    // the list is re-rendered from this same data anyway. Pinning must also
    // reorder `_sessionItems` — the group renderer relies on the array already
    // being pinned-first, so flipping only the flag would leave a just-pinned
    // chat sitting in place (especially inside a project group).
    entry.pinned = pinned ? 1 : 0;
    _sortSessionItems();
    _renderSessionList();

    const owner = agentId || (entry.agent && entry.agent.id) || activeAgentId;
    fetch(`/api/sessions/${encodeURIComponent(sid)}?agent_id=${encodeURIComponent(owner || '')}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pinned, agent_id: owner }),
    })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') { _refreshHistoryList(); return; }
            // Most often an empty brand-new chat: it has no row to pin until the
            // first message is stored.
            _wsToast(data.message || t('session_settings_failed'));
            entry.pinned = pinned ? 0 : 1;
            _sortSessionItems();
            _renderSessionList();
        })
        .catch(() => {
            entry.pinned = pinned ? 0 : 1;
            _sortSessionItems();
            _renderSessionList();
        });
}

function _onSessionListScroll() {
    if (!_sessionHasMore || _sessionLoading || _historyPageFailed) return;
    const container = document.getElementById('session-list');
    if (!container) return;
    // Trigger when scrolled near the bottom (within 60px)
    if (container.scrollHeight - container.scrollTop - container.clientHeight < 60) {
        // Carry the current generation sequence so a page loading under a newer
        // read (or after re-entry) is dropped rather than appended to the wrong
        // list. A pagination failure is not fatal: keep what is confirmed and
        // allow the user to retry by scrolling again.
        _fetchSessionPage(_sessionPage + 1, false, undefined, _sessionReqSeq);
    }
}

// Attach scroll listener once DOM is ready
(function _initSessionScroll() {
    const el = document.getElementById('session-list');
    if (el) {
        el.addEventListener('scroll', _onSessionListScroll);
    } else {
        document.addEventListener('DOMContentLoaded', () => {
            const el2 = document.getElementById('session-list');
            if (el2) el2.addEventListener('scroll', _onSessionListScroll);
        });
    }
})();

// Returning to a session whose reply is still streaming in the background.
// Close the background EventSource, rebuild the bubble from the buffered
// events (snapshot), then resume live streaming via a fresh connection that
// reads the remaining tail from the backend replay log. Returns true if a stream
// was re-attached. The user's own bubble is already in history (persisted
// eagerly), so it was rendered by loadHistory before this runs.
function _reattachStream(sid) {
    const key = runtimeSessionKey(sid);
    const requestId = sessionActiveRequest[key];
    if (!requestId) return false;
    const buffer = streamBuffers[requestId];
    if (!buffer) return false;

    // If the buffered stream already finished, the assistant reply is already
    // persisted and rendered by loadHistory — re-attaching would duplicate it.
    // Just clean up the buffer/cursor and rely on history.
    const finished = buffer.items.some(
        it => it.type === 'stream_end' || it.type === 'error' || it.type === 'resync_required'
    );
    if (finished) {
        const oldEs = activeStreams[requestId];
        if (oldEs) { try { oldEs.close(); } catch (_) {} delete activeStreams[requestId]; }
        delete streamBuffers[requestId];
        delete sessionActiveRequest[key];
        resetSendBtnSendMode();
        return false;
    }

    // done already exists in persistent history. Keep the background tail
    // connected for voice_attach/stream_end, but do not replay the answer into
    // the freshly loaded history view or it would create a duplicate bubble.
    if (buffer.items.some(it => it.type === 'done')) {
        resetSendBtnSendMode();
        return false;
    }

    // Stop the background connection before rebuilding. Each new connection
    // resumes independently from its last accepted sequence number.
    const oldEs = activeStreams[requestId];
    if (oldEs) { try { oldEs.close(); } catch (_) {} delete activeStreams[requestId]; }

    // Snapshot the buffered events into the replay, then start a fresh stream
    // that replays them and reconnects for the live tail.
    const replay = buffer.items.slice();
    startSSE(requestId, null, buffer.timestamp || new Date(), null, replay);
    return true;
}

/* =====================================================================
 * Coding Agents (change add-opencode-coding-agents, task 4.3)
 *
 * A coding Agent's conversation is served by the embedded Opencode app, not by
 * the platform's message pane. The console keeps every one of its own
 * responsibilities -- the identity, the selected session, the sidebar row, the
 * history list -- and hands exactly one thing to `window.CodingChat`: the
 * conversation surface. That is why this is a routing decision (*which* surface
 * serves this Agent) rather than a second chat implementation.
 *
 * Two rules:
 *
 * 1. The type decides the surface, and the type comes from the roster rows the
 *    server already marks explicitly (`agent_type`). Absent is normal.
 * 2. Leaving a coding pane goes through the module's own teardown, which asks
 *    the page's existing unsaved-editor guard first: switching Agents must not
 *    throw away an open editor.
 * ===================================================================== */
function codingChatModule() {
    return (typeof window !== 'undefined' && window.CodingChat) || null;
}

/** The declared type of one Agent; absent means normal. */
function agentTypeOf(agentId) {
    const id = agentId || activeAgentId || defaultAgentId || '';
    if (!id) return 'normal';
    let found = null;
    if (typeof findAgent === 'function') found = findAgent(id);
    if (!found && typeof availableChatAgents === 'function') {
        found = availableChatAgents().find(a => a && a.id === id) || null;
    }
    return found && found.agent_type === 'coding' ? 'coding' : 'normal';
}

function isCodingAgent(agentId) {
    return agentTypeOf(agentId) === 'coding';
}

async function isCodingAgentAsync(agentId) {
    return isCodingAgent(agentId);
}

/** The project directory a coding Agent's sessions open, from the roster row. */
function codingProjectDirOf(agentId) {
    const id = agentId || activeAgentId || '';
    const found = (typeof findAgent === 'function' ? findAgent(id) : null)
        || (typeof availableChatAgents === 'function'
            ? availableChatAgents().find(a => a && a.id === id) : null);
    return (found && found.coding_project_dir) || '';
}

// The service projection is read once per page: it is deployment configuration,
// not something that changes while the console is open.
let _codingSettings = null;
let _codingSettingsRequest = null;

function loadCodingSettings() {
    if (_codingSettings || _codingSettingsRequest) return _codingSettingsRequest;
    // Nothing to read a projection with (and nothing to read it for) when the
    // host has no transport; the display simply stays unstated.
    if (typeof fetch !== 'function') return null;
    _codingSettingsRequest = fetch('/api/coding/settings')
        .then(r => r.json())
        .then(data => {
            // A member without `agent.read` gets a refusal here; that is not an
            // error to show, it just means the service line stays unstated.
            _codingSettings = (data && data.status === 'success') ? data : { unavailable: true };
            return _codingSettings;
        })
        .catch(() => { _codingSettings = { unavailable: true }; return _codingSettings; });
    return _codingSettingsRequest;
}

/** The service name and address for display, or '' when it cannot be stated. */
function codingServiceLabel() {
    const settings = _codingSettings || {};
    if (!settings.service_id && !settings.web_url) return '';
    return [settings.service_id, settings.web_url].filter(Boolean).join(' · ');
}

/* Whether a coding conversation may be started at all. The module is only
   loaded when the deployment has the capability, and the service projects
   `enabled`; with either absent the console says so instead of mounting a frame
   that cannot come up. */
function codingAvailability() {
    const module = codingChatModule();
    if (!module) return { available: false, reason: t('coding_disabled') };
    return { available: true, reason: '' };
}

/* Mount the embedded pane for one coding conversation, keeping the console's
   own bookkeeping in step. ``sessionId`` empty means "a new conversation". */
function openCodingSession(agentId, sessionId, options) {
    const opts = options || {};
    const module = codingChatModule();
    const availability = codingAvailability();
    if (!availability.available) {
        if (!opts.quiet) _wsToast(availability.reason);
        return false;
    }
    // A conversation of another type must not stay mounted behind this one.
    if (!leaveCodingSession()) return false;

    const targetAgent = agentId || activeAgentId || '';
    if (targetAgent && targetAgent !== activeAgentId) {
        activeAgentId = targetAgent;
        writeScopedPreference('cow_active_agent', activeAgentId);
    }
    if (currentView !== 'chat') navigateTo('chat');
    renderComposerIdentity();

    const mounted = sessionId
        ? module.open(targetAgent, sessionId)
        : module.launch(targetAgent, codingProjectDirOf(targetAgent));
    return Promise.resolve(mounted).then(described => {
        if (!described || !described.session_id) return false;
        // The platform session exists only once the service has answered, so the
        // selection and the history row are written from that answer rather than
        // from a client-side guess.
        sessionId = described.session_id;
        writeScopedPreference(activeSessionStorageKey(), sessionId);
        _sessCfg = null;
        markActiveSessionRow();
        if (typeof _historyVisible !== 'undefined' && _historyVisible) loadSessionList();
        return true;
    });
}

/** Tear the coding pane down, through the module's own leave confirmation. */
function leaveCodingSession() {
    const module = codingChatModule();
    if (!module || !module.isActive()) return true;
    return module.leave();
}

/** The conversation currently mounted as a coding session, if any. */
function activeCodingSession() {
    const module = codingChatModule();
    return (module && module.current && module.current()) || null;
}

/* The console's own view of the pane, installed once. The module owns the DOM
   it mounts, so these are the platform's answers to its questions rather than
   an attempt to keep two copies of the same state. */
function wireCodingModule() {
    const module = codingChatModule();
    if (!module || typeof module.setHooks !== 'function') return;
    module.setHooks({
        // The page's existing unsaved-editor guard; the module calls it before
        // it takes the pane down.
        confirmLeave: (proceed) => (typeof wsGuardUnsaved === 'function'
            ? wsGuardUnsaved(proceed)
            : (proceed(), true)),
        notify: (message) => _wsToast(message),
        redrawList: () => {
            if (typeof _historyVisible !== 'undefined' && _historyVisible) loadSessionList();
            loadSidebarRecentSessions();
        },
        onLinked: (described) => {
            // A session the user created inside Opencode is now a platform
            // session: select it, so the history row and the frame agree.
            if (!described || !described.session_id) return;
            const owner = described.agent_id || activeAgentId;
            if (owner && owner !== activeAgentId) return;
            sessionId = described.session_id;
            writeScopedPreference(activeSessionStorageKey(), sessionId);
            markActiveSessionRow();
        },
        onLeave: () => {
            // Back to the ordinary pane: nothing about the identity changes, so
            // the composer only has to be redrawn for the same conversation.
            renderComposerIdentity();
        },
    });
    if (typeof window.addEventListener === 'function') {
        window.addEventListener('message', (event) => {
            // The module validates origin, source and channel; an unclaimed
            // message is simply not ours.
            module.handleMessage(event);
        });
    }
}

/** The one row the sidebar/history marks as selected, in either surface. */
function markActiveSessionRow() {
    document.querySelectorAll('.session-item').forEach(el => {
        el.classList.toggle('active', el.dataset.sessionId === sessionId
            && (!el.dataset.agentId || el.dataset.agentId === activeAgentId));
    });
    document.querySelectorAll('.sidebar-recent-item').forEach(el => {
        el.classList.toggle('active', el.dataset.sessionId === sessionId
            && (!el.dataset.agentId || el.dataset.agentId === activeAgentId));
    });
}

/* The seam's own entry points, for the call sites that live in other sections
   of this file. console.js is split into sections by its own tests, and a
   helper defined in one section is absent when another runs alone, so every
   caller asks for the seam with `typeof codingSeam === 'function'` first and
   behaves exactly as it did before the coding feature when it is missing. */
function codingSeam() {
    return {
        isCodingAgent: isCodingAgent,
        activeSession: activeCodingSession,
        open: openCodingSession,
        leave: leaveCodingSession,
        markRows: markActiveSessionRow,
        projectDir: codingProjectDirOf,
        paintService: paintCodingService,
        serviceLabel: codingServiceLabel,
        settings: loadCodingSettings,
        wire: wireCodingModule,
    };
}

function switchSession(newSessionId, agentId) {
    const seam = (typeof codingSeam === 'function') ? codingSeam() : null;
    // A coding conversation is served by the embedded pane, so it is opened
    // through the module rather than by filling the message list. The identity
    // and the selection are committed by the same helper either way.
    if (seam && seam.isCodingAgent(agentId || activeAgentId)) {
        if (newSessionId === sessionId && (!agentId || agentId === activeAgentId)
            && seam.activeSession()) {
            // Already showing it: bring the view back without reloading the
            // frame (a reload would drop whatever the user has open in there).
            if (currentView !== 'chat') navigateTo('chat');
            renderComposerIdentity();
            return;
        }
        seam.open(agentId || activeAgentId, newSessionId);
        return;
    }
    // A normal conversation replaces a mounted coding pane, and only once the
    // page's leave confirmation has agreed.
    if (seam && !seam.leave()) return;
    // Carry the target across the guard: the identity/session flip must not
    // happen unless the navigation and the unsaved-editor check pass, so a
    // cancel keeps the current Agent and session untouched.
    if (newSessionId === sessionId && (!agentId || agentId === activeAgentId)) {
        if (currentView !== 'chat') navigateTo('chat');
        // Re-open a conversation whose previous history request did not load.
        if (!historyLoading && historyPage === 0) loadHistory(1);
        renderComposerIdentity();
        focusChatComposer();
        return;
    }

    // The preview panel is scoped to a session's workspace, so switching tears
    // down an open editor. Settle unsaved edits before committing to the switch.
    // Preserve the target agentId so a confirm re-runs with the same destination.
    if (typeof wsGuardUnsaved === 'function'
        && !wsGuardUnsaved(() => switchSession(newSessionId, agentId))) return;

    // Do NOT close active streams here: sessions run in parallel, so any
    // in-flight reply for another session must keep streaming in the
    // background (it self-guards against rendering into the foreign view).
    // Switching back re-attaches and resumes live streaming.

    // Commit the identity switch only after the guard passed.
    if (agentId && agentId !== activeAgentId) {
        activeAgentId = agentId;
        writeScopedPreference('cow_active_agent', activeAgentId);
    }

    sessionId = newSessionId;
    _sessCfg = null;
    _wsSelState = { current: null, recents: [], defaultWorkspace: '', projectsRoot: '' };
    _wsSelUpdateLabel();
    updateEditButtonsState();
    writeScopedPreference(activeSessionStorageKey(), sessionId);
    refreshWorkspaceSelector();
    refreshSessionSettings();
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
        _reattachStream(sessionId);
    } else {
        resetSendBtnSendMode();
    }

    document.querySelectorAll('.session-item').forEach(el => {
        el.classList.toggle('active', el.dataset.sessionId === sessionId
            && (!el.dataset.agentId || el.dataset.agentId === activeAgentId));
    });
    document.querySelectorAll('.sidebar-recent-item').forEach(el => {
        el.classList.toggle('active', el.dataset.sessionId === sessionId
            && (!el.dataset.agentId || el.dataset.agentId === activeAgentId));
    });

    if (currentView !== 'chat') navigateTo('chat');
    renderComposerIdentity();
    focusChatComposer();
    // A session switch moves the context panel to a row the server has written,
    // so re-mount it and let it re-read that session's usage.
    if (typeof _contextAfterSessionChange === 'function') _contextAfterSessionChange(true);
}

// In-place rename a session title: replace the title <span> with an <input>,
// commit on Enter/blur, cancel on Escape. Persists via PUT /api/sessions/<id>.
function renameSession(sid, agentId) {
    const owner = agentId || activeAgentId;
    const same = s => s.session_id === sid && (!owner || (s.agent && s.agent.id) === owner);
    const item = [...document.querySelectorAll('.session-item')].find(el =>
        el.dataset.sessionId === sid && (!owner || el.dataset.agentId === owner));
    if (!item) return;
    const titleEl = item.querySelector('.session-title');
    if (!titleEl || item.querySelector('.session-title-input')) return;

    const oldTitle = titleEl.textContent;

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'session-title-input';
    input.value = oldTitle;
    input.maxLength = 100;
    input.setAttribute('aria-label', t('rename_session'));

    // Keep the editor outside a button while retaining the original button's
    // click listener when the edit finishes.
    const mainButton = item.querySelector('.session-row-main');
    const editWrap = document.createElement('div');
    editWrap.className = 'session-row-main';
    while (mainButton.firstChild) editWrap.appendChild(mainButton.firstChild);
    mainButton.replaceWith(editWrap);

    // Avoid switching session while interacting with the input
    const stop = e => e.stopPropagation();
    input.addEventListener('click', stop);
    input.addEventListener('mousedown', stop);

    titleEl.replaceWith(input);
    input.focus();
    input.select();

    let done = false;

    const restore = (title, refreshDeferred = true) => {
        if (done) return;
        done = true;
        const span = document.createElement('span');
        span.className = 'session-title';
        span.title = title;
        span.textContent = title;
        input.replaceWith(span);
        while (editWrap.firstChild) mainButton.appendChild(editWrap.firstChild);
        editWrap.replaceWith(mainButton);
        if (_historyDirty && refreshDeferred) { _historyDirty = false; _refreshHistoryList(); }
    };

    // Undo the optimistic rename in both the DOM and the cached entry.
    const revert = () => {
        const cachedEntry = _sessionItems.find(same);
        if (cachedEntry) cachedEntry.title = oldTitle;
        const span = item.querySelector('.session-title');
        if (span) {
            span.title = oldTitle;
            span.textContent = oldTitle;
        }
        _refreshHistoryList();
    };

    const commit = () => {
        if (done) return;
        const newTitle = input.value.trim();
        if (!newTitle || newTitle === oldTitle) {
            restore(oldTitle);
            return;
        }
        // Optimistically show the new title, then persist. The cached entry is
        // updated too, or the next re-render (a pin, say) would revive the old one.
        restore(newTitle, false);
        const cached = _sessionItems.find(same);
        if (cached) cached.title = newTitle;
        fetch(`/api/sessions/${encodeURIComponent(sid)}?agent_id=${encodeURIComponent(owner || '')}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: newTitle, agent_id: owner })
        })
            .then(r => r.json())
            .then(data => {
                if (data.status !== 'success') { revert(); _wsToast(data.message || t('session_settings_failed')); }
                else _refreshHistoryList();
            })
            .catch(revert);
    };

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); commit(); }
        else if (e.key === 'Escape') { e.preventDefault(); restore(oldTitle); }
    });
    input.addEventListener('blur', commit);
}

function deleteSession(sid, agentId) {
    showConfirmModal(t('delete_session_title'), t('delete_session_confirm'), () => {
        const owner = agentId || activeAgentId;
        const deletingCurrent = sid === sessionId && (!owner || owner === activeAgentId);
        const next = deletingCurrent ? _findNextSession(sid, owner) : null;

        fetch(`/api/sessions/${encodeURIComponent(sid)}?agent_id=${encodeURIComponent(owner || '')}`, { method: 'DELETE' })
            .then(r => r.json())
            .then(data => {
                if (data.status !== 'success') { _wsToast(data.message || t('session_settings_failed')); return; }
                if (!deletingCurrent) {
                    _refreshHistoryList();
                    return;
                }
                if (next) {
                    switchSession(next.sessionId, next.agentId);
                    _refreshHistoryList();
                } else {
                    newChat(false);
                }
            })
            .catch(() => _wsToast(t('session_settings_failed')));
    });
}

// Pick the session to show after deleting `sid` (the current session): prefer
// the next item below it in the list, otherwise the previous one. Returns null
// if no other session exists.
function _findNextSession(sid, agentId) {
    const items = Array.from(document.querySelectorAll('.session-item[data-session-id]'));
    const same = el => el.dataset.sessionId === sid && (!agentId || el.dataset.agentId === agentId);
    const idx = items.findIndex(same);
    const pick = el => el ? { sessionId: el.dataset.sessionId, agentId: el.dataset.agentId || '' } : null;
    if (idx === -1) {
        return pick(items.find(el => !same(el)));
    }
    return pick(items[idx + 1] || items[idx - 1]);
}

function showConfirmModal(title, message, onConfirm) {
    let overlay = document.getElementById('confirm-modal-overlay');
    if (overlay) overlay.remove();

    overlay = document.createElement('div');
    overlay.id = 'confirm-modal-overlay';
    overlay.className = 'confirm-overlay';

    const modal = document.createElement('div');
    modal.className = 'confirm-modal';
    modal.innerHTML = `
        <div class="confirm-title">${escapeHtml(title)}</div>
        <div class="confirm-message">${escapeHtml(message)}</div>
        <div class="confirm-actions">
            <button class="confirm-btn confirm-btn-cancel">${t('confirm_cancel')}</button>
            <button class="confirm-btn confirm-btn-ok">${t('confirm_yes')}</button>
        </div>
    `;
    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    requestAnimationFrame(() => overlay.classList.add('visible'));

    const close = () => {
        overlay.classList.remove('visible');
        setTimeout(() => overlay.remove(), 200);
    };

    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    modal.querySelector('.confirm-btn-cancel').addEventListener('click', close);
    modal.querySelector('.confirm-btn-ok').addEventListener('click', () => {
        close();
        onConfirm();
    });
}

// A confirm modal with a single text input. Calls onSubmit(value) on OK, and
// does nothing on cancel. Mirrors showConfirmModal's look and lifecycle.
function showPromptModal(title, initialValue, onSubmit) {
    let overlay = document.getElementById('confirm-modal-overlay');
    if (overlay) overlay.remove();

    overlay = document.createElement('div');
    overlay.id = 'confirm-modal-overlay';
    overlay.className = 'confirm-overlay';

    const modal = document.createElement('div');
    modal.className = 'confirm-modal';
    modal.innerHTML = `
        <div class="confirm-title">${escapeHtml(title)}</div>
        <input type="text" class="prompt-modal-input" maxlength="100" />
        <div class="confirm-actions">
            <button class="confirm-btn confirm-btn-cancel">${t('confirm_cancel')}</button>
            <button class="confirm-btn confirm-btn-ok">${t('confirm_yes')}</button>
        </div>
    `;
    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    const input = modal.querySelector('.prompt-modal-input');
    input.value = initialValue || '';
    requestAnimationFrame(() => { overlay.classList.add('visible'); input.focus(); input.select(); });

    const close = () => {
        overlay.classList.remove('visible');
        setTimeout(() => overlay.remove(), 200);
    };
    const submit = () => { const v = input.value.trim(); close(); onSubmit(v); };

    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    modal.querySelector('.confirm-btn-cancel').addEventListener('click', close);
    modal.querySelector('.confirm-btn-ok').addEventListener('click', submit);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); submit(); }
        else if (e.key === 'Escape') { e.preventDefault(); close(); }
    });
}

function clearContext() {
    fetch(`/api/sessions/${encodeURIComponent(sessionId)}/clear_context`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.status !== 'success') return;
            // Insert a visual divider in the chat
            const divider = document.createElement('div');
            divider.className = 'context-divider';
            divider.innerHTML = `<span>${t('context_cleared')}</span>`;
            messagesDiv.appendChild(divider);
            scrollChatToBottom();
        })
        .catch(() => {});
}

function generateSessionTitle(sid, userMsg, assistantReply) {
    fetch(`/api/sessions/${encodeURIComponent(sid)}/generate_title`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_message: userMsg, assistant_reply: assistantReply }),
    })
        .then(r => r.json())
        .then(data => {
            if (data.status !== 'success') return;
            // The list only exists on the history page now; refresh it if it is
            // the active view, otherwise mark it dirty so the next visit re-reads
            // the freshly generated title.
            if (_historyVisible) loadSessionList();
            else _historyDirty = true;
        })
        .catch(() => {});
}

// =====================================================================
// Utilities
// =====================================================================
function formatTime(date) {
    const now = new Date();
    const sameDay = date.getFullYear() === now.getFullYear()
        && date.getMonth() === now.getMonth()
        && date.getDate() === now.getDate();
    const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (sameDay) return time;
    const m = String(date.getMonth() + 1).padStart(2, '0');
    const d = String(date.getDate()).padStart(2, '0');
    if (date.getFullYear() === now.getFullYear()) return `${m}-${d} ${time}`;
    return `${date.getFullYear()}-${m}-${d} ${time}`;
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
}

function ChannelsHandler_maskSecret(val) {
    if (!val || val.length <= 8) return val;
    return val.slice(0, 4) + '*'.repeat(val.length - 8) + val.slice(-4);
}

function formatToolArgs(args) {
    if (!args || Object.keys(args).length === 0) return '(none)';
    try {
        return escapeHtml(JSON.stringify(args, null, 2));
    } catch (_) {
        return escapeHtml(String(args));
    }
}

const SUBSTEP_ARGS_CHARS = 90;

/** Tool arguments on one line, for a step in a list of dozens. */
function summarizeToolArgs(args) {
    if (!args || typeof args !== 'object') return '';
    const parts = [];
    for (const [key, value] of Object.entries(args)) {
        const text = typeof value === 'object' ? JSON.stringify(value) : String(value);
        parts.push(`${key}=${text}`);
    }
    const joined = parts.join(', ');
    return joined.length > SUBSTEP_ARGS_CHARS
        ? joined.slice(0, SUBSTEP_ARGS_CHARS) + '…'
        : joined;
}

/**
 * Add or settle one step inside a sub agent's card.
 *
 * Silent when the card is gone: a sub agent cancelled on timeout keeps working
 * until its next checkpoint, and steps that arrive after its card closed
 * describe work nobody is waiting on any more.
 */
function renderSubagentStep(toolEl, item) {
    if (!toolEl || !item.step_id) return;
    const section = toolEl.querySelector('.tool-substeps-section');
    const list = toolEl.querySelector('.tool-substeps');
    if (!section || !list) return;

    let stepEl = list.querySelector(`[data-step-id="${CSS.escape(item.step_id)}"]`);
    if (!stepEl) {
        if (item.phase !== 'start') return;
        stepEl = document.createElement('div');
        stepEl.className = 'tool-substep';
        stepEl.dataset.stepId = item.step_id;
        stepEl.innerHTML = `
            <i class="fas fa-circle-notch fa-spin tool-substep-icon"></i>
            <span class="tool-substep-name">${escapeHtml(item.tool || 'tool')}</span>
            <span class="tool-substep-args">${escapeHtml(summarizeToolArgs(item.arguments))}</span>
            <span class="tool-substep-time"></span>`;
        list.appendChild(stepEl);
        section.classList.remove('hidden');
        // The first step is also the first sign of life from a sub agent that
        // runs for minutes, so it opens the card it belongs to.
        toolEl.classList.add('expanded');
        updateSubstepCount(toolEl, list.children.length);
        return;
    }

    if (item.phase !== 'end') return;
    const isError = item.status && item.status !== 'success';
    const icon = stepEl.querySelector('.tool-substep-icon');
    if (icon) {
        icon.className = isError
            ? 'fas fa-times tool-substep-icon tool-substep-failed'
            : 'fas fa-check tool-substep-icon';
    }
    const timeEl = stepEl.querySelector('.tool-substep-time');
    if (timeEl && item.execution_time) timeEl.textContent = `${item.execution_time}s`;
    if (item.error) {
        // A step that failed says so where it happened; the sub agent's report
        // covers what the successful ones found.
        const argsEl = stepEl.querySelector('.tool-substep-args');
        if (argsEl) {
            argsEl.textContent = String(item.error);
            argsEl.classList.add('tool-substep-failed');
        }
        stepEl.title = String(item.error);
    }
}

function updateSubstepCount(toolEl, count) {
    const countEl = toolEl.querySelector('.tool-substep-count');
    if (countEl) countEl.textContent = count === 1 ? '1 step' : `${count} steps`;
}

function scrollChatToBottom(force) {
    if (force || _autoScrollEnabled) {
        messagesDiv.scrollTop = messagesDiv.scrollHeight;
    }
}

function _updateScrollToBottomBtn() {
    const btn = document.getElementById('scroll-to-bottom-btn');
    if (!btn) return;
    const distFromBottom = messagesDiv.scrollHeight - messagesDiv.scrollTop - messagesDiv.clientHeight;
    btn.classList.toggle('hidden', distFromBottom <= _SCROLL_THRESHOLD);
}

function applyHighlighting(container) {
    const root = container || document;
    setTimeout(() => {
        const hljsLib = getHljs();
        root.querySelectorAll('pre code').forEach(block => {
            if (!block.classList.contains('hljs')) {
                hljsLib.highlightElement(block);
            }
        });
        // Add language labels and copy buttons to code blocks
        _addCodeBlockHeaders(root);
    }, 0);
}

// =====================================================================
// Config View
// =====================================================================
let configProviders = {};
let configApiBases = {};
let configApiKeys = {};
let configCurrentModel = '';
let cfgProviderValue = '';
let cfgModelValue = '';
let cfgReasoningEffortValue = 'high';
let configReasoningByModel = {};
// Remembers the custom model name the user typed per provider, so switching
// away from a provider (which rebuilds its model dropdown) and back does not
// lose an unsaved custom model. Keyed by provider id.
let configCustomModelByProvider = {};
// Same idea for the Models tab capability cards: remember the custom model the
// user typed per (capability, provider) and the provider active before the
// last switch, so switching vendors and back restores the custom model.
// Keyed by `${capabilityId}:${providerId}` -> custom model string.
let capabilityCustomModelMemory = {};
// Keyed by capabilityId -> provider id active before the current switch.
let capabilityLastProviderId = {};

// --- Custom dropdown helper ---
function initDropdown(el, options, selectedValue, onChange, opts) {
    // opts.placeholder: when set AND selectedValue is empty, render that text
    // in a dim style instead of auto-selecting options[0]. Useful for
    // "pick or empty" capabilities (asr / embedding) where we want the
    // user to make an explicit choice.
    opts = opts || {};
    // opts.readOnly: the control still renders the effective value but cannot
    // be opened or changed. Used for settings that are owned by another layer
    // (e.g. the default permission in database mode, which roles own).
    el._ddReadOnly = !!opts.readOnly;
    const textEl = el.querySelector('.cfg-dropdown-text');
    const menuEl = el.querySelector('.cfg-dropdown-menu');
    const selEl = el.querySelector('.cfg-dropdown-selected');
    // Optional avatar face in the trigger (opts.withAvatar). Each option then
    // carries an `agent` object so both the row and the trigger can paint it.
    const faceEl = el.querySelector('.cfg-dropdown-face');

    el._ddValue = selectedValue || '';
    el._ddOnChange = onChange;

    function paintFace(opt) {
        if (!faceEl) return;
        faceEl.innerHTML = (opt && opt.agent) ? agentAvatarHTML(opt.agent, 20) : '';
    }

    function render() {
        menuEl.innerHTML = '';
        options.forEach(opt => {
            const item = document.createElement('div');
            item.className = 'cfg-dropdown-item' + (opt.value === el._ddValue ? ' active' : '');
            item.dataset.value = opt.value;
            // Hint is an optional dim secondary label rendered on the right
            // side of the row (e.g. friendly brand name next to a technical
            // model id). When absent the row degrades to the original
            // single-string layout.
            if (opt.agent) {
                const face = document.createElement('span');
                face.className = 'cfg-dropdown-item-face';
                face.innerHTML = agentAvatarHTML(opt.agent, 20);
                const labelEl = document.createElement('span');
                labelEl.className = 'cfg-dropdown-label';
                labelEl.textContent = opt.label;
                item.appendChild(face);
                item.appendChild(labelEl);
                // Optional trailing pill (e.g. a "default" marker) rendered
                // dim after the name.
                if (opt.badge) {
                    const badgeEl = document.createElement('span');
                    badgeEl.className = 'cfg-dropdown-badge';
                    badgeEl.textContent = opt.badge;
                    item.appendChild(badgeEl);
                }
            } else if (opt.hint) {
                const labelEl = document.createElement('span');
                labelEl.className = 'cfg-dropdown-label';
                labelEl.textContent = opt.label;
                const hintEl = document.createElement('span');
                hintEl.className = 'cfg-dropdown-hint';
                hintEl.textContent = opt.hint;
                item.appendChild(labelEl);
                item.appendChild(hintEl);
            } else {
                item.textContent = opt.label;
            }
            item.addEventListener('click', (e) => {
                e.stopPropagation();
                if (el._ddReadOnly) return;
                el._ddValue = opt.value;
                textEl.textContent = opt.label;
                // Now that a real option is picked, drop the muted placeholder
                // style — otherwise the chosen label stays grey (visible on
                // dropdowns that start in a placeholder state, e.g. the chat
                // fallback pickers).
                textEl.classList.remove('text-slate-400', 'dark:text-slate-500');
                paintFace(opt);
                menuEl.querySelectorAll('.cfg-dropdown-item').forEach(i => i.classList.remove('active'));
                item.classList.add('active');
                el.classList.remove('open');
                if (el._ddOnChange) el._ddOnChange(opt.value);
            });
            menuEl.appendChild(item);
        });
        const sel = options.find(o => o.value === el._ddValue);
        if (sel) {
            textEl.textContent = sel.label;
            paintFace(sel);
            textEl.classList.remove('text-slate-400', 'dark:text-slate-500');
        } else if (opts.placeholder && !el._ddValue) {
            // No selection yet — show the placeholder in muted style.
            // Do NOT write a fallback value, so the dropdown stays
            // "unsaved" until the user explicitly picks.
            textEl.textContent = opts.placeholder;
            paintFace(null);
            textEl.classList.add('text-slate-400', 'dark:text-slate-500');
        } else {
            textEl.textContent = options[0] ? options[0].label : '--';
            paintFace(options[0]);
            textEl.classList.remove('text-slate-400', 'dark:text-slate-500');
            if (options[0]) el._ddValue = options[0].value;
        }
    }

    render();

    if (!el._ddBound) {
        selEl.addEventListener('click', (e) => {
            e.stopPropagation();
            if (el._ddReadOnly) return;
            document.querySelectorAll('.cfg-dropdown.open').forEach(d => { if (d !== el) d.classList.remove('open'); });
            const willOpen = !el.classList.contains('open');
            if (willOpen) {
                // Flip the menu above the control when it would otherwise be
                // clipped against the viewport bottom (e.g. the last channel's
                // config dropdown sitting near the window edge).
                const rect = el.getBoundingClientRect();
                const below = window.innerHeight - rect.bottom;
                const menuH = Math.min(menuEl.scrollHeight || 240, 280) + 8;
                el.classList.toggle('drop-up', below < menuH && rect.top > below);
            }
            el.classList.toggle('open');
        });
        el._ddBound = true;
    }
}

document.addEventListener('click', () => {
    document.querySelectorAll('.cfg-dropdown.open').forEach(d => d.classList.remove('open'));
});

function getDropdownValue(el) { return el._ddValue || ''; }

// --- Config init ---
function initConfigView(data) {
    configProviders = data.providers || {};
    configApiBases = data.api_bases || {};
    configApiKeys = data.api_keys || {};
    configCurrentModel = data.model || '';
    configReasoningByModel = data.reasoning_effort_by_model || {};
    cfgReasoningEffortValue = data.reasoning_effort || 'high';

    const providerEl = document.getElementById('cfg-provider');
    const providerOpts = Object.entries(configProviders).map(([pid, p]) => ({ value: pid, label: localizedLabel(p.label) }));

    // if use_linkai is enabled, always select linkai as the provider
    // Otherwise prefer bot_type from config, fall back to model-based detection
    const detected = data.use_linkai ? 'linkai'
        : (data.bot_type && configProviders[data.bot_type] ? data.bot_type : detectProvider(configCurrentModel));
    cfgProviderValue = detected || (providerOpts[0] ? providerOpts[0].value : '');

    initDropdown(providerEl, providerOpts, cfgProviderValue, onProviderChange);

    onProviderChange(cfgProviderValue);
    syncModelSelection(configCurrentModel);

    document.getElementById('cfg-max-tokens').value = data.agent_max_context_tokens || 50000;
    document.getElementById('cfg-max-turns').value = data.agent_max_context_turns || 20;
    document.getElementById('cfg-max-steps').value = data.agent_max_steps || 20;
    const thinkingEl = document.getElementById('cfg-enable-thinking');
    thinkingEl.checked = data.enable_thinking === true;
    if (!thinkingEl._cfgReasoningBound) {
        thinkingEl.addEventListener('change', syncReasoningEffortOptions);
        thinkingEl._cfgReasoningBound = true;
    }
    const customModelEl = document.getElementById('cfg-model-custom');
    if (customModelEl && !customModelEl._cfgReasoningBound) {
        customModelEl.addEventListener('input', () => {
            // Remember the typed custom model for the current provider so a
            // provider switch and switch-back doesn't lose it.
            if (cfgModelValue === '__custom__') {
                configCustomModelByProvider[cfgProviderValue] = customModelEl.value.trim();
            }
            syncReasoningEffortOptions();
        });
        customModelEl._cfgReasoningBound = true;
    }
    syncReasoningEffortOptions();
    document.getElementById('cfg-subagent').checked = data.subagent_enabled !== false;
    document.getElementById('cfg-self-evolution').checked = data.self_evolution_enabled === true;

    // Reflect the current UI language (already resolved, may include the user's
    // local choice) on the selector so it stays in sync with the top-right toggle.
    const langSel = document.getElementById('cfg-lang-select');
    if (langSel) {
        initDropdown(
            langSel,
            [{ value: 'zh', label: '简体中文' }, { value: 'zh-Hant', label: '繁體中文' }, { value: 'en', label: 'English' }],
            currentLang,
            (val) => setLanguage(val)
        );
    }

    // Default permission mode for new conversations. Owned by role resource
    // grants: rendered read-only so a tenant user cannot widen what their
    // session may run. Editable only when the server explicitly allows it.
    const permEl = document.getElementById('cfg-permission');
    if (permEl) {
        const editable = data.permission_mode_editable === true;
        const offered = data.permission_modes && data.permission_modes.length
            ? data.permission_modes
            : Object.keys(PERMISSION_META);
        const permOpts = Object.keys(PERMISSION_META)
            .filter(mode => offered.includes(mode))
            .map(mode => ({ value: mode, label: t(PERMISSION_META[mode].key) }));
        initDropdown(
            permEl,
            permOpts,
            data.agent_permission_mode || 'full-access',
            editable ? saveGlobalPermission : null,
            { readOnly: !editable }
        );
        permEl.classList.toggle('cfg-dropdown-readonly', !editable);
        if (editable) permEl.removeAttribute('aria-disabled');
        else permEl.setAttribute('aria-disabled', 'true');
        const descEl = document.getElementById('cfg-permission-desc');
        const roleDescEl = document.getElementById('cfg-permission-role-desc');
        if (descEl) descEl.classList.toggle('hidden', !editable);
        if (roleDescEl) roleDescEl.classList.toggle('hidden', editable);
    }
}

function detectProvider(model) {
    if (!model) return Object.keys(configProviders)[0] || '';
    for (const [pid, p] of Object.entries(configProviders)) {
        if (pid === 'linkai') continue;
        if (p.models && p.models.includes(model)) return pid;
    }
    return Object.keys(configProviders)[0] || '';
}

function onProviderChange(pid) {
    cfgProviderValue = pid || getDropdownValue(document.getElementById('cfg-provider'));
    const p = configProviders[cfgProviderValue];
    if (!p) return;

    const customTip = document.getElementById('cfg-custom-tip');
    if (customTip) customTip.classList.toggle('hidden', cfgProviderValue !== 'custom');

    const modelEl = document.getElementById('cfg-model-select');
    const modelOpts = (p.models || []).map(m => ({ value: m, label: m }));
    modelOpts.push({ value: '__custom__', label: t('config_custom_option') });

    // Restore a custom model the user typed for this provider earlier in the
    // session (kept in configCustomModelByProvider). Fall back to the first
    // preset. For a custom provider with no preset models the picker only has
    // the "__custom__" entry, so a remembered value is the only way its model
    // survives a provider switch.
    const rememberedCustom = configCustomModelByProvider[cfgProviderValue];
    const initialModelValue = rememberedCustom
        ? '__custom__'
        : (modelOpts[0] ? modelOpts[0].value : '');

    initDropdown(modelEl, modelOpts, initialModelValue, onModelSelectChange);

    // API Key
    const keyField = p.api_key_field;
    const keyWrap = document.getElementById('cfg-api-key-wrap');
    const keyInput = document.getElementById('cfg-api-key');

    // Only LinkAI (an aggregation platform) gets a link to its console for
    // managing the aggregated key; other providers manage keys on their sites.
    const cfgManageKey = document.getElementById('cfg-manage-key');
    if (cfgManageKey) cfgManageKey.classList.toggle('hidden', cfgProviderValue !== 'linkai');
    if (keyField) {
        keyWrap.classList.remove('hidden');
        keyInput.classList.add('cfg-key-masked');
        const maskedVal = configApiKeys[keyField] || '';
        keyInput.value = maskedVal;
        keyInput.dataset.field = keyField;
        keyInput.dataset.masked = maskedVal ? '1' : '';
        keyInput.dataset.maskedVal = maskedVal;
        const toggleIcon = document.querySelector('#cfg-api-key-toggle i');
        if (toggleIcon) toggleIcon.className = 'fas fa-eye text-xs';

        if (!keyInput._cfgBound) {
            keyInput.addEventListener('focus', function() {
                if (this.dataset.masked === '1') {
                    this.value = '';
                    this.dataset.masked = '';
                    this.classList.remove('cfg-key-masked');
                }
            });
            keyInput.addEventListener('blur', function() {
                if (!this.value.trim() && this.dataset.maskedVal) {
                    this.value = this.dataset.maskedVal;
                    this.dataset.masked = '1';
                    this.classList.add('cfg-key-masked');
                }
            });
            keyInput.addEventListener('input', function() {
                this.dataset.masked = '';
            });
            keyInput._cfgBound = true;
        }
    } else {
        keyWrap.classList.add('hidden');
        keyInput.value = '';
        keyInput.dataset.field = '';
    }

    // API Base
    const apiBaseInput = document.getElementById('cfg-api-base');
    if (p.api_base_key) {
        document.getElementById('cfg-api-base-wrap').classList.remove('hidden');
        apiBaseInput.value = configApiBases[p.api_base_key] || p.api_base_default || '';
        // Hint the version-path tail (e.g. /v1) so users are reminded to
        // include it themselves. We don't auto-rewrite anything server-side.
        apiBaseInput.placeholder = p.api_base_placeholder || 'https://...';
    } else {
        document.getElementById('cfg-api-base-wrap').classList.add('hidden');
        apiBaseInput.value = '';
        apiBaseInput.placeholder = 'https://...';
    }

    onModelSelectChange(initialModelValue, { restoredCustom: rememberedCustom });
    syncReasoningEffortOptions();
}

function onModelSelectChange(val, opts) {
    opts = opts || {};
    cfgModelValue = val || getDropdownValue(document.getElementById('cfg-model-select'));
    const customWrap = document.getElementById('cfg-model-custom-wrap');
    const customInput = document.getElementById('cfg-model-custom');
    if (cfgModelValue === '__custom__') {
        customWrap.classList.remove('hidden');
        // When switching back to a provider we restore the remembered value;
        // otherwise this is a fresh pick of "custom" and we focus for input.
        if (opts.restoredCustom) {
            customInput.value = opts.restoredCustom;
        } else {
            customInput.focus();
        }
    } else {
        customWrap.classList.add('hidden');
        customInput.value = '';
    }
    syncReasoningEffortOptions();
}

function syncModelSelection(model) {
    const p = configProviders[cfgProviderValue];
    if (!p) return;

    const modelEl = document.getElementById('cfg-model-select');
    if (p.models && p.models.includes(model)) {
        const modelOpts = (p.models || []).map(m => ({ value: m, label: m }));
        modelOpts.push({ value: '__custom__', label: t('config_custom_option') });
        initDropdown(modelEl, modelOpts, model, onModelSelectChange);
        cfgModelValue = model;
        document.getElementById('cfg-model-custom-wrap').classList.add('hidden');
    } else {
        cfgModelValue = '__custom__';
        const modelOpts = (p.models || []).map(m => ({ value: m, label: m }));
        modelOpts.push({ value: '__custom__', label: t('config_custom_option') });
        initDropdown(modelEl, modelOpts, '__custom__', onModelSelectChange);
        document.getElementById('cfg-model-custom-wrap').classList.remove('hidden');
        document.getElementById('cfg-model-custom').value = model;
        // Seed the per-provider memory so switching away and back keeps it.
        if (model) configCustomModelByProvider[cfgProviderValue] = model;
    }
    syncReasoningEffortOptions();
}

function syncReasoningEffortOptions() {
    const wrap = document.getElementById('cfg-reasoning-effort-wrap');
    const el = document.getElementById('cfg-reasoning-effort');
    if (!wrap || !el) return;

    const provider = configProviders[cfgProviderValue] || {};
    const selectedModel = getSelectedModel();
    const reasoningByModel = provider.reasoning_by_model || {};
    const reasoning = reasoningByModel[selectedModel] || provider.reasoning || {};
    const options = reasoning.supported ? (reasoning.options || []) : [];
    const thinkingEl = document.getElementById('cfg-enable-thinking');

    if (options.length) {
        const values = options.map(opt => opt.value);
        // Prefer this model's own saved effort (per-model config) so switching
        // vendors never reinterprets a value set for a different model. Key is
        // the lowercased model name, matching the backend resolve path.
        const savedForModel = configReasoningByModel[`${cfgProviderValue}:${selectedModel.trim().toLowerCase()}`]
            || configReasoningByModel[cfgProviderValue + ':' + selectedModel];
        const saved = savedForModel || cfgReasoningEffortValue;
        // Fall back to the active model's native enum when the saved value is
        // not valid here. Resolved even while hidden so a save never writes
        // another model's enum under this model's key.
        cfgReasoningEffortValue = values.includes(saved) ? saved : (reasoning.default || options[0].value);
    }

    // Effort only shapes a thinking pass, so the field follows the toggle.
    if (!thinkingEl || !thinkingEl.checked || !options.length) {
        wrap.classList.add('hidden');
        return;
    }

    wrap.classList.remove('hidden');
    initDropdown(
        el,
        options.map(opt => ({ value: opt.value, label: opt.label || opt.value })),
        cfgReasoningEffortValue,
        (val) => { cfgReasoningEffortValue = val; }
    );
}

function getSelectedModel() {
    if (cfgModelValue === '__custom__') {
        return document.getElementById('cfg-model-custom').value.trim();
    }
    return cfgModelValue;
}

function toggleApiKeyVisibility() {
    const input = document.getElementById('cfg-api-key');
    const icon = document.querySelector('#cfg-api-key-toggle i');
    if (input.classList.contains('cfg-key-masked')) {
        input.classList.remove('cfg-key-masked');
        icon.className = 'fas fa-eye-slash text-xs';
    } else {
        input.classList.add('cfg-key-masked');
        icon.className = 'fas fa-eye text-xs';
    }
}

function showStatus(elId, msgKey, isError) {
    const el = document.getElementById(elId);
    el.textContent = t(msgKey);
    el.classList.toggle('text-red-500', !!isError);
    el.classList.toggle('text-primary-500', !isError);
    el.classList.remove('opacity-0');
    // Warning messages (errors) should stay visible, success messages auto-hide
    if (!isError) {
        setTimeout(() => el.classList.add('opacity-0'), 2500);
    }
}

function saveModelConfig() {
    const model = getSelectedModel();
    if (!model) return;

    const updates = { model: model };
    const p = configProviders[cfgProviderValue];
    updates.use_linkai = (cfgProviderValue === 'linkai');
    if (cfgProviderValue === 'linkai') {
        updates.bot_type = '';
    } else {
        updates.bot_type = cfgProviderValue;
    }
    if (p && p.api_base_key) {
        const base = document.getElementById('cfg-api-base').value.trim();
        if (base) updates[p.api_base_key] = base;
    }
    if (p && p.api_key_field) {
        const keyInput = document.getElementById('cfg-api-key');
        const rawVal = keyInput.value.trim();
        if (rawVal && keyInput.dataset.masked !== '1') {
            updates[p.api_key_field] = rawVal;
        }
    }

    const btn = document.getElementById('cfg-model-save');
    btn.disabled = true;
    fetch('/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ updates })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            configCurrentModel = model;
            if (data.applied) {
                const keyInput = document.getElementById('cfg-api-key');
                Object.entries(data.applied).forEach(([k, v]) => {
                    if (k === 'model') return;
                    if (k.includes('api_key')) {
                        const masked = v.length > 8
                            ? v.substring(0, 4) + '*'.repeat(v.length - 8) + v.substring(v.length - 4)
                            : v;
                        configApiKeys[k] = masked;
                        if (keyInput.dataset.field === k) {
                            keyInput.value = masked;
                            keyInput.dataset.masked = '1';
                            keyInput.dataset.maskedVal = masked;
                            keyInput.classList.add('cfg-key-masked');
                            const toggleIcon = document.querySelector('#cfg-api-key-toggle i');
                            if (toggleIcon) toggleIcon.className = 'fas fa-eye text-xs';
                        }
                    } else {
                        configApiBases[k] = v;
                    }
                });
            }
            showStatus('cfg-model-status', 'config_saved', false);
        } else {
            showStatus('cfg-model-status', 'config_save_error', true);
        }
    })
    .catch(() => showStatus('cfg-model-status', 'config_save_error', true))
    .finally(() => { btn.disabled = false; });
}

function saveAgentConfig() {
    const effortKey = `${cfgProviderValue}:${getSelectedModel().trim().toLowerCase()}`;
    const mergedEffortByModel = Object.assign({}, configReasoningByModel, { [effortKey]: cfgReasoningEffortValue });
    const updates = {
        agent_max_context_tokens: parseInt(document.getElementById('cfg-max-tokens').value) || 50000,
        agent_max_context_turns: parseInt(document.getElementById('cfg-max-turns').value) || 20,
        agent_max_steps: parseInt(document.getElementById('cfg-max-steps').value) || 20,
        enable_thinking: document.getElementById('cfg-enable-thinking').checked,
        // Persist effort per model (merge with the existing map so other
        // models' saved efforts survive the flat config save).
        reasoning_effort_by_model: mergedEffortByModel,
        subagent_enabled: document.getElementById('cfg-subagent').checked,
        self_evolution_enabled: document.getElementById('cfg-self-evolution').checked,
    };

    const btn = document.getElementById('cfg-agent-save');
    btn.disabled = true;
    fetch('/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ updates })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            // Reflect the merged map so a later model switch shows/uses the
            // just-saved value instead of a stale in-memory one.
            configReasoningByModel = mergedEffortByModel;
            showStatus('cfg-agent-status', 'config_saved', false);
        } else {
            showStatus('cfg-agent-status', 'config_save_error', true);
        }
    })
    .catch(() => showStatus('cfg-agent-status', 'config_save_error', true))
    .finally(() => { btn.disabled = false; });
}

// Persist the instance-wide default permission mode. Sessions that never pinned
// their own follow it, so the composer chip is refreshed afterwards.
function saveGlobalPermission(mode) {
    fetch('/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ updates: { agent_permission_mode: mode } })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            showStatus('cfg-permission-status', 'config_saved', false);
            refreshSessionSettings();
        } else {
            showStatus('cfg-permission-status', 'config_save_error', true);
        }
    })
    .catch(() => showStatus('cfg-permission-status', 'config_save_error', true));
}

function loadConfigView() {
    fetch('/config').then(r => r.json()).then(data => {
        if (data.status !== 'success') return;
        appConfig = data;
        initConfigView(data);
    }).catch(() => {});
}

// The public model service (vendor addresses, keys, provider defaults) is the
// platform's to maintain; ``actions.manage`` on 模型与接入 is that qualification
// alone and is what /config and /api/models gate on server-side. Read from the
// authoritative projection rather than assuming: rendering those editors for a
// caller whose write would be refused is exactly the affordance the projection
// exists to prevent.
const CONFIG_VIEW_CONSOLE_PAGE = 'admin.models';

function _modelsManageAllowed() {
    // Legacy identity mode has a single, unrestricted surface: there is no page
    // projection to read and no member catalog to show.
    if (_identityMode() !== 'database') return true;
    const ctx = _baseAuthContext();
    const page = ctx && ctx.console_pages && typeof ctx.console_pages === 'object'
        ? ctx.console_pages[CONFIG_VIEW_CONSOLE_PAGE] : null;
    if (page && page.actions) return page.actions.manage === true;
    // Projection undecided (a deep link into /admin/config can enter the view
    // before /auth/context answers). The platform qualification is known from
    // /auth/me and is exactly what ``actions.manage`` is computed from, so this
    // cannot disagree with the server: a member is not platform admin, and an
    // unresolved projection never becomes a way to show them the editors.
    const self = _baseAccountSelf();
    return !!(self && self.user && self.user.is_platform_admin);
}

function switchConfigTab(tab) {
    ['basic', 'models', 'catalog'].forEach(name => {
        document.getElementById(`config-tab-${name}`)?.classList.toggle('active', name === tab);
        document.getElementById(`config-panel-${name}`)?.classList.toggle('hidden', name !== tab);
    });
    if (tab === 'models') loadModelsView();
    if (tab === 'catalog') loadMemberCatalog();
    // Re-pull /config when returning to Basic: a provider added on the Models
    // tab must show up in the basic main-model provider picker without a manual
    // page refresh. loadConfigView re-renders from the fresh provider list.
    if (tab === 'basic') loadConfigView();
}

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
    return brandingDraft.brand_name === brandingBaseline.brand_name
        && brandingDraft.logo_description === brandingBaseline.logo_description
        && brandingDraft.logo_action === 'keep'
        && brandingDraft.logoPreviewUrl === brandingBaseline.logoUrl;
}

function _brandingSetDirty(flag) {
    brandingDirty = flag;
    const badge = brandingEl('branding-state-badge');
    if (!badge) return;
    badge.classList.remove('hidden');
    if (flag) {
        badge.textContent = t('branding_unsaved');
        badge.classList.remove('bg-emerald-50', 'dark:bg-emerald-900/20', 'text-emerald-600', 'dark:text-emerald-300');
        badge.classList.add('bg-amber-50', 'dark:bg-amber-900/20', 'text-amber-600', 'dark:text-amber-300');
    } else {
        badge.textContent = t('branding_saved_state');
        badge.classList.remove('bg-amber-50', 'dark:bg-amber-900/20', 'text-amber-600', 'dark:text-amber-300');
        badge.classList.add('bg-emerald-50', 'dark:bg-emerald-900/20', 'text-emerald-600', 'dark:text-emerald-300');
    }
}

function _brandingRefreshDirty() {
    if (!brandingDraft) { _brandingSetDirty(false); return; }
    _brandingSetDirty(!_brandingInputsEqual());
    _brandingUpdateControls();
}

function _brandingShowBanner(msg, kind) {
    const banner = brandingEl('branding-banner');
    if (!banner) return;
    const styles = {
        warning: 'bg-amber-50 dark:bg-amber-900/20 border-amber-200 dark:border-amber-800 text-amber-700 dark:text-amber-200',
        error: 'bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-800 text-red-700 dark:text-red-200',
        info: 'bg-blue-50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-800 text-blue-700 dark:text-blue-200',
        success: 'bg-emerald-50 dark:bg-emerald-900/20 border-emerald-200 dark:border-emerald-800 text-emerald-700 dark:text-emerald-200',
    };
    banner.className = `hidden mb-4 rounded-xl border px-4 py-3 text-sm ${styles[kind] || styles.info}`;
    banner.innerHTML = `<div class="flex items-start gap-2"><span>${escapeHtml(msg)}</span></div>`;
    banner.classList.remove('hidden');
}

function _brandingHideBanner() {
    const banner = brandingEl('branding-banner');
    if (banner) banner.classList.add('hidden');
}

function _brandingShowReadonlyState() {
    _brandingUpdateControls();
    const messages = {
        branding_enterprise_unavailable: 'branding_enterprise_unavailable',
        branding_storage_corrupt: 'branding_storage_corrupt',
    };
    _brandingShowBanner(t(messages[brandingReadonlyReason] || 'branding_readonly_reason'), 'info');
    if (brandingReadonlyReason === 'branding_storage_corrupt') _brandingRenderConflictActions();
}

function _brandingRenderPreview() {
    if (!brandingDraft) return;
    const name = brandingDraft.brand_name || DEFAULT_BRAND.brand_name;
    const desc = brandingDraft.logo_description || '';
    const logoUrl = brandingDraft.logoPreviewUrl || effectiveLogoUrl();

    const canvas = brandingEl('branding-preview-canvas');
    if (!canvas) return;
    canvas.classList.toggle('dark', brandingPreviewDark);

    // sidebar slot
    canvas.querySelectorAll('[data-brand-slot="name"]').forEach(el => {
        el.innerHTML = brandWordmarkHTML(name);
        if (brandingPreviewDark) {
            el.classList.add('!text-[#f3f8fc]');
        } else {
            el.classList.remove('!text-[#f3f8fc]');
        }
    });
    canvas.querySelectorAll('[data-brand-slot="logo"]').forEach(el => {
        el.src = logoUrl;
        el.alt = '';
    });
    // Sidebar caption: default maps to 工作台 / 控制台 by path; custom
    // brand descriptions still paint as configured.
    canvas.querySelectorAll('[data-brand-slot="caption"]').forEach(el => {
        const captionHelper = (typeof window !== 'undefined' && window
            && typeof window.sidebarBrandCaption === 'function')
            ? window.sidebarBrandCaption
            : null;
        const caption = captionHelper
            ? captionHelper(desc)
            : ((desc && desc.trim()) || '');
        const hasDesc = !!String(caption || '').trim();
        el.textContent = hasDesc ? caption : '';
        el.classList.toggle('hidden', !hasDesc);
        el.title = hasDesc ? caption : '';
    });
    // Desc slot is used by both the login and welcome previews. The welcome
    // preview mirrors the real welcome hero and hides the built-in default
    // (redundant with the eyebrow); login preview keeps showing it.
    canvas.querySelectorAll('[data-brand-slot="desc"]').forEach(el => {
        const isWelcomePreview = !!el.closest('[data-preview="welcome"]');
        const isDefault = _isDefaultLogoDescription(desc);
        const visible = isWelcomePreview
            ? !!(desc && desc.trim() && !isDefault)
            : !!(desc && desc.trim());
        el.textContent = visible ? desc : '';
        el.classList.toggle('hidden', !visible);
        el.title = visible ? desc : '';
    });
}

function _brandingFillFormFromDraft() {
    if (!brandingDraft) return;
    const nameInput = brandingEl('branding-brand-name');
    const descInput = brandingEl('branding-logo-desc');
    if (nameInput) nameInput.value = brandingDraft.brand_name;
    if (descInput) descInput.value = brandingDraft.logo_description;
    _brandingRenderLogoThumb(brandingDraft.logoPreviewUrl, brandingDraft.logo_action === 'default');
}

function _brandingRenderLogoThumb(url, useDefault) {
    const thumb = brandingEl('branding-logo-thumb');
    if (!thumb) return;
    const src = (useDefault || !url) ? DEFAULT_BRAND.logo_url : url;
    thumb.innerHTML = `<img src="${escapeHtml(src)}" alt="" class="brand-mark w-12 h-12 object-contain" style="${brandingImageError ? 'opacity:.4' : ''}">`;
    brandingImageError = false;
}

function _brandingValidate() {
    const name = brandingEl('branding-brand-name').value.trim();
    const desc = brandingEl('branding-logo-desc').value.trim();
    const flowErr = brandingEl('branding-flow-error');
    if (!name) return '品牌名称不能为空';
    if (name.length > 32) return t('branding_save_failed');
    if (/\n|\r/.test(name) || /\n|\r/.test(desc)) return '不能包含换行字符';
    if (desc.length > 100) return 'Logo 描述不能超过 100 字';
    if (flowErr) flowErr.classList.add('hidden');
    return '';
}

function _brandingFormatFileError(err) {
    if (err && err.code === 'image_too_large') return t('branding_image_too_large');
    if (err && err.code === 'invalid_image_format') return '仅支持 PNG、JPG、WebP 图片';
    return t('branding_invalid_image');
}

function _brandingSetError(msg) {
    const flowErr = brandingEl('branding-flow-error');
    if (flowErr) {
        flowErr.textContent = msg;
        flowErr.classList.remove('hidden');
    }
}

function _brandingPublishSuccess(record) {
    brandingReadonly = record.can_manage === false;
    brandingCanReset = record.can_reset !== false;
    brandingReadonlyReason = record.readonly_reason || '';
    // Update the shared brand snapshot from the save response, bump the save
    // epoch so any in-flight (older) public read is ignored.
    brandSaveEpoch += 1;
    const logoUrl = record.logo_url || DEFAULT_BRAND.logo_url;
    brandState = {
        enabled: true,
        revision: record.revision || 0,
        brand_name: record.brand_name || DEFAULT_BRAND.brand_name,
        logo_description: (record.logo_description != null) ? record.logo_description : '',
        logo_url: logoUrl,
        favicon_url: record.favicon_url || DEFAULT_BRAND.favicon_url,
    };
    brandLoaded = true;
    if (appConfig) appConfig.title = productTitle(brandState.brand_name);
    applyBrandToDocument();
    applyBrandToAgentAvatars();

    // Update the baseline to the newly saved snapshot.
    brandingBaseline = {
        brand_name: brandState.brand_name,
        logo_description: brandState.logo_description,
        logo_action: 'keep',
        logoUrl: logoUrl,
        revision: brandState.revision,
    };
    brandingDraft = { ...brandingBaseline, logo_action: 'keep', logoPreviewUrl: logoUrl, logoFile: null };
    brandingDirty = false;
    brandingConflict = false;
    brandingSavePending = false;
    _brandingFillFormFromDraft();
    _brandingRefreshDirty();
    _brandingRenderPreview();
    _brandingShowBanner(t('branding_saved'), 'success');
    setTimeout(_brandingHideBanner, 3000);
}

function _brandingSetControlsState(saving) {
    brandingSaving = saving;
    _brandingUpdateControls();
}

function _brandingUpdateControls() {
    const busy = brandingLoading || brandingSaving;
    const editable = !busy && !brandingReadonly && !!brandingBaseline;
    ['branding-brand-name', 'branding-logo-desc', 'branding-logo-file', 'branding-upload-btn',
        'branding-default-logo-btn', 'branding-cancel'].forEach(id => {
        const el = brandingEl(id);
        if (el) el.disabled = !editable;
    });
    const saveBtn = brandingEl('branding-save');
    const resetBtn = brandingEl('branding-reset-all');
    if (saveBtn) saveBtn.disabled = !editable || brandingConflict || !brandingDirty || !!_brandingValidate();
    if (resetBtn) resetBtn.disabled = busy || !brandingCanReset || !brandingBaseline;
}

function _brandingSubmitSave() {
    if (brandingSaving || brandingLoading || brandingReadonly || brandingConflict || !brandingBaseline) return;
    const errMsg = _brandingValidate();
    if (errMsg) { _brandingSetError(errMsg); return; }
    const name = brandingEl('branding-brand-name').value.trim();
    const desc = brandingEl('branding-logo-desc').value.trim();
    // No-op save guard.
    if (_brandingInputsEqual()) { _brandingShowBanner(t('branding_unsaved_warn'), 'warning'); return; }

    _brandingSetControlsState(true);
    _brandingHideBanner();
    const fd = new FormData();
    fd.append('expected_revision', String(brandingBaseline ? brandingBaseline.revision : 0));
    fd.append('brand_name', name);
    fd.append('logo_description', desc);
    fd.append('logo_action', brandingDraft.logo_action || 'keep');
    if (brandingDraft.logo_action === 'replace' && brandingDraft.logoFile) fd.append('logo', brandingDraft.logoFile);

    return fetch('/api/branding', { method: 'POST', body: fd, credentials: 'same-origin' })
        .then(async (r) => {
            const data = await r.json().catch(() => ({}));
            if (r.status === 401) {
                // Session expired: re-prompt login; draft stays in memory only.
                if (typeof maybeShowLoginOverlay === 'function') maybeShowLoginOverlay();
                _brandingShowBanner(t('branding_save_failed') + ' 401', 'error');
                throw new Error('unauthorized');
            }
            if (r.status === 409) {
                brandingConflict = true;
                _brandingShowBanner(t('branding_conflict'), 'warning');
                _brandingRenderConflictActions();
                throw new Error('conflict');
            }
            if (!r.ok || data.status !== 'success') {
                _brandingShowBanner(data.message || t('branding_save_failed'), 'error');
                throw new Error('save-failed');
            }
            return data;
        })
        .then((data) => {
            _brandingPublishSuccess(data);
        })
        .catch((err) => {
            if (err && err.message === 'conflict') {
                // Keep the draft so the user can inspect / reload.
                brandingDirty = true;
                return;
            }
            if (err && err.message === 'unauthorized') return;
            _brandingSetDirty(true);
        })
        .finally(() => {
            _brandingSetControlsState(false);
            brandingSaving = false;
            _brandingRefreshDirty();
        });
}

function _brandingRenderConflictActions() {
    const banner = brandingEl('branding-banner');
    if (!banner) return;
    banner.innerHTML += `<button type="button" id="branding-reload-btn" class="ml-2 underline text-xs cursor-pointer">${escapeHtml(t('branding_reload'))}</button>`;
    const rb = brandingEl('branding-reload-btn');
    if (rb) rb.addEventListener('click', () => initBrandingView());
}

function brandingConfirmDiscard(onDiscard) {
    if (!brandingDirty) { onDiscard(); return; }
    showConfirmDialog({
        title: t('branding_dirty_leave_title'),
        message: t('branding_dirty_leave_body'),
        okText: t('branding_dirty_leave_ok'),
        cancelText: t('branding_dirty_leave_cancel'),
        onConfirm: onDiscard,
    });
}

function _brandingResetDraftToBaseline() {
    if (!brandingBaseline) return;
    brandingDraft = {
        brand_name: brandingBaseline.brand_name,
        logo_description: brandingBaseline.logo_description,
        logo_action: 'keep',
        logoFile: null,
        logoPreviewUrl: brandingBaseline.logoUrl,
        hasLogoChange: false,
    };
    _brandingFillFormFromDraft();
    _brandingRenderPreview();
    _brandingRefreshDirty();
    _brandingHideBanner();
}

function initBrandingView() {
    if (!brandingEl('view-branding')) return;
    if (brandingLoading || brandingSaving) return;
    // Re-entry and conflict reload both preserve drafts unless confirmed.
    brandingConfirmDiscard(_loadBrandingSetup);
}

function _loadBrandingSetup() {
    const view = brandingEl('view-branding');
    if (!view) return;
    brandingLoading = true;
    _brandingUpdateControls();
    const banner = brandingEl('branding-banner');
    if (banner) banner.classList.add('hidden');
    // Show a loading placeholder on the Save button.
    const saveBtn = brandingEl('branding-save');
    if (saveBtn) saveBtn.disabled = true;

    return fetch('/api/branding', { credentials: 'same-origin' })
        .then(async (r) => {
            if (r.status === 401) {
                if (typeof maybeShowLoginOverlay === 'function') maybeShowLoginOverlay();
                throw new Error('unauthorized');
            }
            const data = await r.json().catch(() => ({}));
            if (!r.ok || data.status !== 'success') throw new Error(data.message || 'load-failed');
            return data;
        })
        .then((data) => {
            brandingReadonly = !data.can_manage;
            brandingCanReset = !!data.can_reset;
            brandingReadonlyReason = data.readonly_reason || '';
            brandingBaseline = {
                brand_name: data.brand_name || DEFAULT_BRAND.brand_name,
                logo_description: (data.logo_description != null) ? data.logo_description : '',
                logo_action: 'keep',
                logoUrl: data.logo_url || DEFAULT_BRAND.logo_url,
                revision: data.revision || 0,
            };
            brandingDraft = { ...brandingBaseline, logo_action: 'keep', logoPreviewUrl: data.logo_url || DEFAULT_BRAND.logo_url, logoFile: null, hasLogoChange: false };
            brandingConflict = false;
            _brandingFillFormFromDraft();
            _brandingRenderPreview();
            _brandingRefreshDirty();
            brandingInitDone = true;
            brandingLoading = false;
            _brandingUpdateControls();
            if (brandingReadonly) _brandingShowReadonlyState();
        })
        .catch(() => {
            brandingLoading = false;
            brandingReadonly = true;
            brandingCanReset = false;
            _brandingUpdateControls();
            _brandingShowBanner(t('branding_load_failed'), 'error');
            _brandingRenderConflictActions();
        });
}

function _brandingBindEvents() {
    const view = brandingEl('view-branding');
    if (!view || brandingInitDone && brandingEl('view-branding').dataset.bound === '1') return;

    const name = brandingEl('branding-brand-name');
    const desc = brandingEl('branding-logo-desc');
    if (name) name.addEventListener('input', () => {
        if (!brandingDraft) return;
        brandingDraft.brand_name = name.value;
        _brandingRefreshDirty();
        _brandingRenderPreview();
    });
    if (desc) desc.addEventListener('input', () => {
        if (!brandingDraft) return;
        brandingDraft.logo_description = desc.value;
        _brandingRefreshDirty();
        _brandingRenderPreview();
    });

    const uploadBtn = brandingEl('branding-upload-btn');
    const fileInput = brandingEl('branding-logo-file');
    if (uploadBtn && fileInput) {
        uploadBtn.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', () => {
            if (brandingReadonly || brandingLoading || brandingSaving) return;
            const file = fileInput.files && fileInput.files[0];
            if (!file) return;
            if (file.size > 2 * 1024 * 1024) { _brandingSetError(t('branding_image_too_large')); return; }
            const ext = (file.name.split('.').pop() || '').toLowerCase();
            if (!['png', 'jpg', 'jpeg', 'webp'].includes(ext)) { _brandingSetError('仅支持 PNG、JPG、WebP 图片'); return; }
            _brandingSetError('');
            const objUrl = URL.createObjectURL(file);
            if (brandingDraft) { brandingDraft.logoFile = file; brandingDraft.logo_preview = objUrl; brandingDraft.logo_action = 'replace'; brandingDraft.logoPreviewUrl = objUrl; }
            _brandingRenderLogoThumb(objUrl, false);
            _brandingRenderPreview();
            _brandingRefreshDirty();
        });
    }

    const defaultLogoBtn = brandingEl('branding-default-logo-btn');
    if (defaultLogoBtn) defaultLogoBtn.addEventListener('click', () => {
        if (!brandingDraft) return;
        brandingDraft.logoFile = null;
        brandingDraft.logo_action = 'default';
        brandingDraft.logoPreviewUrl = DEFAULT_BRAND.logo_url;
        brandingDraft.hasLogoChange = true;
        // Keep a stable, non-emptied file state.
        const fileInputEl = brandingEl('branding-logo-file');
        if (fileInputEl) fileInputEl.value = '';
        _brandingRenderLogoThumb(DEFAULT_BRAND.logo_url, true);
        _brandingRenderPreview();
        _brandingRefreshDirty();
    });

    const saveBtn = brandingEl('branding-save');
    if (saveBtn) saveBtn.addEventListener('click', _brandingSubmitSave);

    const cancelBtn = brandingEl('branding-cancel');
    if (cancelBtn) cancelBtn.addEventListener('click', () => _brandingResetDraftToBaseline());

    const resetAllBtn = brandingEl('branding-reset-all');
    if (resetAllBtn) resetAllBtn.addEventListener('click', () => {
        const doReset = () => {
            if (brandingSaving || brandingLoading || !brandingCanReset || !brandingBaseline) return;
            _brandingSetControlsState(true);
            _brandingHideBanner();
            fetch('/api/branding/reset', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ expected_revision: brandingBaseline ? brandingBaseline.revision : 0 }),
            })
                .then(async (r) => {
                    if (r.status === 401) {
                        if (typeof maybeShowLoginOverlay === 'function') maybeShowLoginOverlay();
                        throw new Error('unauthorized');
                    }
                    const data = await r.json().catch(() => ({}));
                    if (r.status === 409) { brandingConflict = true; _brandingShowBanner(t('branding_conflict'), 'warning'); _brandingRenderConflictActions(); throw new Error('conflict'); }
                    if (!r.ok || data.status !== 'success') { _brandingShowBanner(data.message || t('branding_save_failed'), 'error'); throw new Error('reset-failed'); }
                    return data;
                })
                .then((data) => _brandingPublishSuccess(data))
                .catch((err) => {
                    if (err && err.message === 'conflict') { brandingDirty = true; return; }
                    if (err && err.message === 'unauthorized') return;
                    _brandingSetDirty(true);
                })
                .finally(() => _brandingSetControlsState(false));
        };
        if (typeof showConfirmDialog === 'function') {
            showConfirmDialog({
                title: t('branding_reset_confirm_title'),
                message: t('branding_reset_confirm_body'),
                okText: t('branding_reset_confirm_ok'),
                cancelText: t('branding_reset_confirm_cancel'),
                onConfirm: doReset,
            });
        } else if (window.confirm(t('branding_reset_confirm_body'))) {
            doReset();
        }
    });

    // Theme toggle for the preview
    const darkBtn = brandingEl('branding-theme-dark');
    const lightBtn = brandingEl('branding-theme-light');
    const setTheme = (dark) => {
        brandingPreviewDark = dark;
        if (darkBtn) darkBtn.classList.toggle('active', dark);
        if (lightBtn) lightBtn.classList.toggle('active', !dark);
        _brandingRenderPreview();
    };
    if (darkBtn) darkBtn.addEventListener('click', () => setTheme(true));
    if (lightBtn) lightBtn.addEventListener('click', () => setTheme(false));

    // Drag & drop onto the logo thumb.
    const thumb = brandingEl('branding-logo-thumb');
    if (thumb) {
        thumb.addEventListener('dragover', (e) => { e.preventDefault(); thumb.classList.add('border-primary-500'); });
        thumb.addEventListener('dragleave', () => thumb.classList.remove('border-primary-500'));
        thumb.addEventListener('drop', (e) => {
            e.preventDefault();
            if (brandingReadonly || brandingLoading || brandingSaving) return;
            thumb.classList.remove('border-primary-500');
            const file = e.dataTransfer.files && e.dataTransfer.files[0];
            if (file) {
                if (file.size > 2 * 1024 * 1024) { _brandingSetError(t('branding_image_too_large')); return; }
                const objUrl = URL.createObjectURL(file);
                if (brandingDraft) { brandingDraft.logoFile = file; brandingDraft.logo_action = 'replace'; brandingDraft.logoPreviewUrl = objUrl; }
                _brandingRenderLogoThumb(objUrl, false);
                _brandingRenderPreview();
                _brandingRefreshDirty();
            }
        });
    }

    brandingEl('view-branding').dataset.bound = '1';
}

document.addEventListener('DOMContentLoaded', function() {
    // Bind the branding page controls once. The page is populated lazily on
    // first entry via navigateTo -> initBrandingView.
    if (brandingEl('view-branding')) _brandingBindEvents();
});

// =====================================================================
// Skills View
// =====================================================================
let toolsLoaded = false;
//: The rows the tools section last received, so a card can hand its own row
//: (with its server-decided ``personal`` state) to the detail component instead
//: of re-reading a state the server already answered.
let toolsState = { rows: [] };
//: The skill rows the list last received, by name — the same reason as
//: ``toolsState``: a card hands the detail component the row the server decided,
//: not a reconstruction from data attributes.
let skillsState = { byName: {} };

const TOOL_ICONS = {
    bash: 'fa-terminal',
    edit: 'fa-pen-to-square',
    read: 'fa-file-lines',
    write: 'fa-file-pen',
    ls: 'fa-folder-open',
    send: 'fa-paper-plane',
    web_search: 'fa-magnifying-glass',
    browser: 'fa-globe',
    env_config: 'fa-key',
    scheduler: 'fa-clock',
    memory_get: 'fa-brain',
    memory_search: 'fa-brain',
};

function getToolIcon(name) {
    return TOOL_ICONS[name] || 'fa-wrench';
}

function loadSkillsView() {
    loadToolsSection();
    loadSkillsSection();
}

function loadToolsSection() {
    if (toolsLoaded) return;
    const emptyEl = document.getElementById('tools-empty');
    const listEl = document.getElementById('tools-list');
    const badge = document.getElementById('tools-count-badge');

    fetch('/api/tools').then(r => r.json()).then(data => {
        if (data.status !== 'success') return;
        const tools = data.tools || [];
        toolsState.rows = tools;
        emptyEl.classList.add('hidden');
        if (tools.length === 0) {
            emptyEl.classList.remove('hidden');
            emptyEl.innerHTML = `<span class="text-sm text-slate-400 dark:text-slate-500">${currentLang === 'zh' ? '暂无内置工具' : 'No built-in tools'}</span>`;
            return;
        }
        badge.textContent = tools.length;
        badge.classList.remove('hidden');
        listEl.innerHTML = '';
        tools.forEach(tool => {
            const card = document.createElement('div');
            // The card *is* the entry point to the resource's detail component
            // (task 5.5): the personal parameters a member may set for this tool
            // are reached from here, on the same page, instead of from a page of
            // their own. Rendered as a clickable row so the affordance is visible
            // rather than a surprise.
            card.className = 'bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10 p-4 flex items-start gap-3 cursor-pointer hover:border-slate-300 dark:hover:border-white/20';
            card.innerHTML = `
                <div class="w-9 h-9 rounded-lg bg-blue-50 dark:bg-blue-900/20 flex items-center justify-center flex-shrink-0">
                    <i class="fas ${getToolIcon(tool.name)} text-blue-500 dark:text-blue-400 text-sm"></i>
                </div>
                <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2">
                        <span class="font-medium text-sm text-slate-700 dark:text-slate-200 font-mono">${escapeHtml(tool.name)}</span>
                    </div>
                    <p class="text-xs text-slate-400 dark:text-slate-500 mt-1 line-clamp-2">${escapeHtml(tool.description || '--')}</p>
                </div>
                <i class="fas fa-chevron-right text-[11px] text-slate-300 dark:text-slate-600 mt-1"></i>`;
            card.onclick = () => openResourceDetail('tool', tool);
            listEl.appendChild(card);
        });
        listEl.classList.remove('hidden');
        toolsLoaded = true;
    }).catch(() => {
        emptyEl.classList.remove('hidden');
        emptyEl.innerHTML = `<span class="text-sm text-slate-400 dark:text-slate-500">${currentLang === 'zh' ? '加载失败' : 'Failed to load'}</span>`;
    });
}

function loadSkillsSection() {
    const emptyEl = document.getElementById('skills-empty');
    const listEl = document.getElementById('skills-list');
    const badge = document.getElementById('skills-count-badge');

    fetch('/api/skills').then(r => r.json()).then(data => {
        if (data.status !== 'success') return;
        const skills = data.skills || [];
        skillsState.byName = {};
        skills.forEach(sk => { if (sk && sk.name) skillsState.byName[sk.name] = sk; });
        if (skills.length === 0) {
            const p = emptyEl.querySelector('p');
            if (p) p.textContent = currentLang === 'zh' ? '暂无技能' : 'No skills found';
            return;
        }
        badge.textContent = skills.length;
        badge.classList.remove('hidden');
        emptyEl.classList.add('hidden');
        listEl.innerHTML = '';

        skills.forEach(sk => {
            const card = document.createElement('div');
            card.className = 'bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10 '
                + 'p-4 flex items-start gap-3 transition-opacity cursor-pointer '
                + 'hover:border-slate-300 dark:hover:border-white/20';
            card.dataset.skillName = sk.name;
            card.dataset.skillDesc = sk.description || '';
            card.dataset.skillDisplayName = sk.display_name || '';
            card.dataset.enabled = sk.enabled ? '1' : '0';
            renderSkillCard(card, sk);
            listEl.appendChild(card);
        });
    }).catch(() => {});
}

function renderSkillCard(card, sk) {
    const enabled = sk.enabled;
    // The global enable/disable decides the state every member and Agent reads,
    // so the server reports per row whether this caller may move it. Rendering
    // the switch regardless would advertise a request that is refused; the state
    // itself stays visible either way, because reading it is not the action.
    const canToggle = !sk.actions || sk.actions.enable !== false;
    const iconColor = enabled ? 'text-primary-400' : 'text-slate-300 dark:text-slate-600';
    const trackClass = enabled
        ? 'bg-primary-400'
        : 'bg-slate-200 dark:bg-slate-700';
    const thumbTranslate = enabled ? 'translate-x-3' : 'translate-x-0.5';
    const switchMarkup = canToggle
        ? `<button
                    role="switch"
                    data-skill-switch
                    aria-checked="${enabled}"
                    class="relative inline-flex h-4 w-7 flex-shrink-0 cursor-pointer rounded-full transition-colors duration-200 ease-in-out focus:outline-none ${trackClass}"
                    title="${enabled ? t('skill_disable') : t('skill_enable')}"
                >
                    <span class="inline-block h-3 w-3 mt-0.5 rounded-full bg-white shadow transform transition-transform duration-200 ease-in-out ${thumbTranslate}"></span>
                </button>`
        : `<span
                    data-skill-state
                    aria-disabled="true"
                    class="text-xs flex-shrink-0 ${enabled ? 'text-primary-400' : 'text-slate-400 dark:text-slate-500'}"
                    title="${t('skill_global_toggle_managed')}"
                >${enabled ? t('skill_enable') : t('skill_disable')}</span>`;
    card.innerHTML = `
        <div class="w-9 h-9 rounded-lg bg-amber-50 dark:bg-amber-900/20 flex items-center justify-center flex-shrink-0">
            <i class="fas fa-bolt ${iconColor} text-sm"></i>
        </div>
        <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2 mb-1">
                <span class="font-medium text-sm text-slate-700 dark:text-slate-200 truncate flex-1">${escapeHtml(sk.display_name || sk.name)}</span>
                ${switchMarkup}
            </div>
            <p class="text-xs text-slate-400 dark:text-slate-500 line-clamp-2">${escapeHtml(sk.description || '--')}</p>
        </div>`;

    // Bound here rather than written into the markup above: a skill name comes
    // from its own frontmatter, and one containing a quote would break out of
    // an inline onclick attribute.
    card.title = t('skill_open_hint');
    card.onclick = () => openResourceDetail('skill', sk);
    const sw = card.querySelector('[data-skill-switch]');
    if (sw) {
        sw.onclick = (e) => {
            e.stopPropagation();
            toggleSkill(sk.name, enabled);
        };
    }
}

function toggleSkill(name, currentlyEnabled) {
    const action = currentlyEnabled ? 'close' : 'open';
    const card = document.querySelector(`[data-skill-name="${CSS.escape(name)}"]`);
    if (card) card.style.opacity = '0.5';
    // The row the list received, kept so the card can be re-rendered *whole*:
    // the switch's answer only changes `enabled`, while `actions` and the
    // caller's `personal` state have to survive the repaint (a card rebuilt
    // without them would forget what the server said it may offer).
    const row = skillsState.byName[name];

    return fetch('/api/skills', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, name })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status !== 'success') {
            if (card) card.style.opacity = '1';
            alert(currentLang === 'zh' ? '操作失败，请稍后再试' : 'Operation failed, please try again');
            return false;
        }
        if (row) row.enabled = !currentlyEnabled;
        if (card) {
            card.dataset.enabled = currentlyEnabled ? '0' : '1';
            card.style.opacity = '1';
            renderSkillCard(card, row || {
                name: name,
                description: card.dataset.skillDesc || '',
                display_name: card.dataset.skillDisplayName || '',
                enabled: !currentlyEnabled,
            });
        }
        return true;
    })
    .catch(() => {
        if (card) card.style.opacity = '1';
        alert(currentLang === 'zh' ? '操作失败，请稍后再试' : 'Operation failed, please try again');
        return false;
    });
}

// ---------------------------------------------------------------------
// Resource detail (工具与技能, task 5.5)
// ---------------------------------------------------------------------
//
// One detail component for both kinds on this page, and the place a member's
// personal usage parameters live — the spec forbids a second, standalone
// personal resource page, because two surfaces for one configuration means two
// places to keep honest and one more way to advertise what the server refuses.
//
// Nothing here decides authorization. The row arrives with the server's own
// answer (``personal`` : the caller's saved parameters plus its ``configure`` /
// ``clear`` verbs, grant- and switch-aware), and the component renders exactly
// that: a row with no state gets no parameter section at all, a row that may not
// be configured but still holds saved parameters gets them read-only with the
// clear verb only.

let resourceDetailState = null;   // { kind: 'tool' | 'skill', row: {...} }

const RESOURCE_DETAIL_ENDPOINTS = { tool: '/api/tools', skill: '/api/skills' };

function openResourceDetail(kind, row) {
    const overlay = document.getElementById('resource-detail-overlay');
    if (!overlay || !row) return;
    resourceDetailState = { kind: kind, row: row };
    renderResourceDetail();
    overlay.classList.remove('hidden');
}

function closeResourceDetail() {
    document.getElementById('resource-detail-overlay')?.classList.add('hidden');
    resourceDetailState = null;
}

function renderResourceDetail() {
    if (!resourceDetailState) return;
    const kind = resourceDetailState.kind;
    const row = resourceDetailState.row;
    const title = document.getElementById('resource-detail-title');
    const chip = document.getElementById('resource-detail-kind');
    const body = document.getElementById('resource-detail-body');
    if (!body) return;
    if (title) title.textContent = row.display_name || row.name || '';
    if (chip) {
        chip.textContent = t(kind === 'skill' ? 'resource_detail_kind_skill'
                                              : 'resource_detail_kind_tool');
    }

    body.innerHTML = `
        <p class="text-sm text-slate-600 dark:text-slate-300 whitespace-pre-wrap">${escapeHtml(row.description || '--')}</p>
        ${kind === 'skill' ? skillDetailActions(row) : ''}
        ${personalParamsSection(row)}`;

    const defineBtn = body.querySelector('[data-resource-detail-define]');
    if (defineBtn) {
        defineBtn.onclick = () => {
            closeResourceDetail();
            openSkillFile(row.name);
        };
    }
    const saveBtn = body.querySelector('[data-resource-personal-save]');
    if (saveBtn) saveBtn.onclick = () => savePersonalParamsFromDetail();
    const clearBtn = body.querySelector('[data-resource-personal-clear]');
    if (clearBtn) clearBtn.onclick = () => clearPersonalParamsFromDetail();
    const toggleBtn = body.querySelector('[data-resource-detail-toggle]');
    if (toggleBtn) {
        toggleBtn.onclick = () => {
            // The switch's answer decides what the *list* shows too, and the
            // detail's own label must follow the server's outcome rather than a
            // predicted one: a refused toggle leaves both unchanged.
            toggleSkill(row.name, !!row.enabled).then(ok => {
                if (ok) renderResourceDetail();
            });
        };
    }
}

/** The skill-only half of the detail body: its definition and its global switch. */
function skillDetailActions(row) {
    // The 定义 entry stays a *viewer*: a builtin's file is not editable, and the
    // viewer already reports that from the server's own `editable` answer.
    const define = `
        <button type="button" data-resource-detail-define
                class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-200 dark:border-white/10 text-xs font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-white/5 cursor-pointer">
            <i class="fas fa-file-lines text-[11px]"></i>${t('resource_detail_skill_definition')}
        </button>`;
    // The switch is offered only where the server said the caller may move it
    // (``actions.enable``); otherwise the state is stated, never the control.
    const enabled = !!row.enabled;
    const toggle = row.actions && row.actions.enable !== false
        ? `<button type="button" data-resource-detail-toggle
                   class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-200 dark:border-white/10 text-xs font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-white/5 cursor-pointer">
               <i class="fas fa-power-off text-[11px]"></i>${t(enabled ? 'skill_disable' : 'skill_enable')}
           </button>`
        : `<span class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-50 dark:bg-white/5 text-xs text-slate-500 dark:text-slate-400"
                 title="${t('skill_global_toggle_managed')}"
                 data-resource-detail-state>${t('skill_global_toggle_managed')}</span>`;
    return `<div class="flex flex-wrap items-center gap-2 mt-4">${define}${toggle}</div>`;
}

/** The personal-parameter section, or '' when the server reported no state. */
function personalParamsSection(row) {
    const personal = row && row.personal;
    if (!personal) return '';
    const actions = personal.actions || {};
    const configured = !!personal.configured;
    const paramsText = JSON.stringify(personal.params || {}, null, 2);
    const header = `
        <div class="mt-5 pt-4 border-t border-slate-100 dark:border-white/5">
            <h4 class="text-sm font-medium text-slate-700 dark:text-slate-200">${t('resource_detail_personal_title')}</h4>
            <p class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">${t('resource_detail_personal_desc')}</p>
        </div>`;
    const status = `<p data-resource-personal-status class="hidden mt-2 text-xs"></p>`;
    const clear = actions.clear
        ? `<button type="button" data-resource-personal-clear
                   class="px-3 py-1.5 rounded-lg border border-slate-200 dark:border-white/10 text-xs text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-white/5 cursor-pointer">${t('personal_action_clear')}</button>`
        : '';

    if (!actions.configure) {
        // Saved parameters whose use grant has been withdrawn (or whose
        // capability was switched off): still readable and still removable —
        // the save is what would be refused — but never editable.
        return `${header}
            <pre class="mt-3 px-3 py-2 rounded-lg bg-slate-50 dark:bg-white/5 text-xs text-slate-600 dark:text-slate-300 overflow-x-auto">${escapeHtml(paramsText)}</pre>
            <p class="mt-2 text-xs text-amber-600 dark:text-amber-400">${t('resource_detail_personal_readonly')}</p>
            <div class="flex items-center gap-2 mt-3">${clear}</div>
            ${status}`;
    }

    const secretPlaceholder = personal.has_credential
        ? t('resource_detail_personal_secret_saved')
        : t('resource_detail_personal_secret');
    return `${header}
        <label class="block mt-3 text-xs text-slate-500 dark:text-slate-400">${t('resource_detail_personal_params')}</label>
        <textarea data-resource-personal-params rows="4" spellcheck="false"
                  class="mt-1 w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-600 bg-slate-50 dark:bg-white/5 text-xs font-mono text-slate-800 dark:text-slate-100 focus:outline-none focus:border-primary-500">${escapeHtml(paramsText)}</textarea>
        <label class="block mt-3 text-xs text-slate-500 dark:text-slate-400">${t('resource_detail_personal_secret_label')}</label>
        <input type="password" data-resource-personal-secret autocomplete="new-password"
               placeholder="${secretPlaceholder}"
               class="mt-1 w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-600 bg-slate-50 dark:bg-white/5 text-sm text-slate-800 dark:text-slate-100 focus:outline-none focus:border-primary-500">
        <div class="flex items-center gap-2 mt-3">
            <button type="button" data-resource-personal-save
                    class="px-3 py-1.5 rounded-lg bg-primary-500 hover:bg-primary-600 text-white text-xs font-medium cursor-pointer">${t('save')}</button>
            ${clear}
        </div>
        ${status}`;
}

function _personalDetailStatus(key, isError) {
    const el = document.querySelector('[data-resource-personal-status]');
    if (!el) return;
    el.dataset.i18n = key;
    el.textContent = t(key);
    el.classList.remove('hidden');
    el.classList.toggle('text-red-500', !!isError);
    el.classList.toggle('text-emerald-500', !isError);
}

function _personalDetailRequest(payload, okKey) {
    if (!resourceDetailState) return Promise.resolve();
    const kind = resourceDetailState.kind;
    const body = Object.assign({}, payload);
    if (kind === 'skill') {
        // The skill's *service* is the anchor Agent's library; without this the
        // object the server resolves could differ from the row the page listed.
        body.agent_id = activeAgentId;
    }
    return fetch(RESOURCE_DETAIL_ENDPOINTS[kind], {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    }).then(res => res.json().then(data => ({ ok: res.ok, data: data })))
    .then(({ ok, data }) => {
        if (!ok || data.status !== 'success') {
            // The refusal is the server's own message: a revoked grant, a
            // withdrawn capability and an invalid payload are different
            // situations and the page must not flatten them into one.
            _personalDetailStatus((data && data.message) || 'resource_detail_personal_error', true);
            return;
        }
        _applyPersonalConfig(kind, payload.action, data.config);
        renderResourceDetail();
        _personalDetailStatus(okKey, false);
        if (kind === 'skill') loadSkillsSection();
    }).catch(() => _personalDetailStatus('resource_detail_personal_error', true));
}

/** Fold a save/clear answer back into the row the component is showing. */
function _applyPersonalConfig(kind, action, config) {
    if (!resourceDetailState) return;
    const personal = resourceDetailState.row.personal || {};
    const mayConfigure = !!(personal.actions && personal.actions.configure);
    if (action === 'clear-personal') {
        resourceDetailState.row.personal = Object.assign({}, personal, {
            configured: false, params: {}, has_credential: false, version: 0,
            actions: { configure: mayConfigure, clear: false },
        });
        return;
    }
    resourceDetailState.row.personal = Object.assign({}, personal, {
        resource_kind: kind,
        resource_id: (config && config.resource_id) || personal.resource_id,
        configured: true,
        params: (config && config.params) || {},
        has_credential: !!(config && config.credential_id),
        version: (config && config.version) || personal.version,
        actions: { configure: mayConfigure, clear: true },
    });
}

function savePersonalParamsFromDetail() {
    if (!resourceDetailState) return;
    const row = resourceDetailState.row;
    const area = document.querySelector('[data-resource-personal-params]');
    const secretEl = document.querySelector('[data-resource-personal-secret]');
    let params;
    const raw = area ? area.value.trim() : '';
    try {
        params = raw ? JSON.parse(raw) : {};
    } catch (_) {
        return _personalDetailStatus('resource_detail_personal_invalid_json', true);
    }
    if (!params || typeof params !== 'object' || Array.isArray(params)) {
        return _personalDetailStatus('resource_detail_personal_invalid_json', true);
    }
    const payload = { action: 'save-personal', resource_id: row.resource_id, params: params };
    const secret = secretEl ? secretEl.value : '';
    // An empty field means "leave the saved secret alone": a save that silently
    // replaced a stored credential with nothing would be indistinguishable from
    // a rotation, and the server has no third state to express it.
    if (secret) payload.secret = secret;
    return _personalDetailRequest(payload, 'resource_detail_personal_saved');
}

function clearPersonalParamsFromDetail() {
    if (!resourceDetailState) return;
    return _personalDetailRequest(
        { action: 'clear-personal', resource_id: resourceDetailState.row.resource_id },
        'resource_detail_personal_cleared');
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
    const data = await res.json();
    if (data.status !== 'success') throw new Error(data.message || 'read failed');
    return data;
}

/** Save a skill's definition. Returns the raw response, a conflict included. */
async function skillWriteContent(name, content, expectedMtime) {
    const res = await fetch('/api/skills/content', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name, content: content, expected_mtime: expectedMtime }),
    });
    return res.json();
}

/** The i18n key explaining why a skill cannot be edited, or null if it can. */
function skillReadonlyReason(data) {
    if (data.editable) return null;
    // Not `source === 'builtin'`: the workspace copy of a builtin skill reads
    // back as `custom` and is refused all the same, so the server says so.
    if (data.ships_with_install) return 'skill_builtin_readonly';
    return docUneditableReason(data);
}

const skillEditor = createDocEditor({
    body: () => document.getElementById('skill-viewer-content'),
    buttons: () => ({
        edit: document.getElementById('skill-btn-edit'),
        save: document.getElementById('skill-btn-save'),
        cancel: document.getElementById('skill-btn-cancel'),
    }),
    read: (doc) => skillReadContent(doc.name),
    write: (doc, content, mtime) => skillWriteContent(doc.name, content, mtime),
    render: (doc) => docRenderBody('skill-viewer-content', doc.content),
    canEdit: (doc) => !doc.readonlyKey,
    refusal: skillReadonlyReason,
    onState: (state) => docRenderTitle('skill-viewer-title', skillEditor.current()?.name, state),
});

function openSkillFile(name) {
    skillReadContent(name).then(data => {
        const badge = document.getElementById('skill-viewer-readonly');
        const readonlyKey = skillReadonlyReason(data);
        if (badge) {
            badge.classList.toggle('hidden', !readonlyKey);
            if (readonlyKey) {
                // Keep data-i18n in step so a language switch re-translates it.
                badge.dataset.i18n = readonlyKey;
                badge.textContent = t(readonlyKey);
                badge.title = t(readonlyKey);
            }
        }
        document.getElementById('skills-panel-list').classList.add('hidden');
        document.getElementById('skills-panel-viewer').classList.remove('hidden');
        skillEditor.open({
            name: data.name || name,
            content: data.content || '',
            readonlyKey: readonlyKey,
        });
    }).catch(e => _wsToast(`${t('skill_load_failed')}: ${e.message}`));
}

function closeSkillViewer() {
    if (!skillEditor.guard(closeSkillViewer)) return;
    resetSkillViewer();
    // A saved edit can change the name and description in the frontmatter, so
    // the cards behind this panel may be out of date.
    loadSkillsSection();
}

/** Drop the viewer and show the list, without asking about unsaved edits. */
function resetSkillViewer() {
    skillEditor.forget();
    document.getElementById('skills-panel-viewer')?.classList.add('hidden');
    document.getElementById('skills-panel-list')?.classList.remove('hidden');
}

// =====================================================================
// Memory View
// =====================================================================
let memoryPage = 1;
let memoryCategory = 'memory';   // 'memory' | 'evolution'
const memoryPageSize = 10;

function switchMemoryTab(tab) {
    document.querySelectorAll('.memory-tab').forEach(el => el.classList.remove('active'));
    document.getElementById('memory-tab-' + tab).classList.add('active');
    // The "dreams" tab now surfaces self-evolution logs (merged with dream diaries).
    memoryCategory = tab === 'dreams' ? 'evolution' : 'memory';
    loadMemoryView(1);
}

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

function loadMemoryView(page) {
    page = page || 1;
    memoryPage = page;
    fetch(`/api/memory?page=${page}&page_size=${memoryPageSize}&category=${memoryCategory}&${memoryTargetQuery()}`).then(r => r.json()).then(data => {
        if (data.status !== 'success') return _memoryRefusal(data);
        if (Array.isArray(data.targets)) memoryTargets = data.targets;
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
    }).catch(() => _memoryRefusal(null));
}

// =====================================================================
// Document viewers (memory files, skill definitions)
// =====================================================================

/**
 * Read one file's text for an editor. Throws on an API error so the editor can
 * report it.
 *
 * No session is passed on purpose. Memory files are anchored to the agent's
 * state root, and a session with a project open would resolve the same relative
 * path against that project instead.
 */
async function docReadFile(relPath) {
    const res = await fetch(`/api/workspace/read?path=${encodeURIComponent(relPath)}`);
    const data = await res.json();
    if (data.status !== 'success') throw new Error(data.message || 'read failed');
    return data;
}

/** Save one file's text. Returns the raw response, a conflict included. */
async function docWriteFile(relPath, content, expectedMtime) {
    const res = await fetch('/api/workspace/write', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: relPath, content: content, expected_mtime: expectedMtime }),
    });
    return res.json();
}

/** Render Markdown into a viewer body. */
function docRenderBody(id, content) {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = renderMarkdown(content || '');
    applyHighlighting(el);
}

/** Put a document's name in a viewer title, with a dot while it is unsaved. */
function docRenderTitle(id, name, state) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = name || '';
    if (state && state.dirty) {
        el.insertAdjacentHTML('beforeend', ' <span class="doc-dirty-dot">•</span>');
    }
}

/**
 * Ask about any unsaved document edit before something tears its page down.
 *
 * @param {function} next - retried once the user agrees to lose the edits.
 * @returns {boolean} true when nothing is at stake and the caller may proceed.
 */
function docGuardUnsaved(next) {
    return memoryEditor.guard(next) && skillEditor.guard(next);
}

const memoryEditor = createDocEditor({
    body: () => document.getElementById('memory-viewer-content'),
    buttons: () => ({
        edit: document.getElementById('memory-btn-edit'),
        save: document.getElementById('memory-btn-save'),
        cancel: document.getElementById('memory-btn-cancel'),
    }),
    read: (doc) => memoryDocRead(doc),
    write: (doc, content, expectedRevision) => memoryDocWrite(doc, content, expectedRevision),
    // Read-only categories (the Agent's own dream/evolution diaries) report it in
    // the payload; hiding the button beats letting the click fail server-side.
    canEdit: (doc) => doc.canEdit === true,
    render: (doc) => docRenderBody('memory-viewer-content', doc.content),
    onState: (state) => {
        docRenderTitle('memory-viewer-title', memoryEditor.current()?.filename, state);
        memorySyncDocButtons(state);
    },
});

/**
 * The memory API body naming the target this page is showing.
 *
 * Mirrors {@link memoryTargetQuery} for POST bodies: the personal domain is a
 * choice and is named as one, and the memory *writes* refuse it anyway (the
 * member's own memory has its own endpoint) — so a personal target simply never
 * gets an edit or delete button.
 */
function memoryTargetBody() {
    return viewingMemoryTarget() === MEMORY_PERSONAL
        ? { scope: 'personal' }
        : { agent_id: viewingMemoryTarget() || '' };
}

/**
 * Whether the server says this entry may be edited.
 *
 * Two independent reasons say no, and both arrive in the read payload: the
 * category (the Agent writes its own dream and evolution diaries, so a manual
 * edit is overwritten by the next consolidation) and the caller's range on the
 * memory *root* (in database mode an Agent's memory root is the tenant's shared
 * root, so a member may read their private Agent's memory but not rewrite what
 * the shared Agent also reads). Stated once here so the editor never offers a
 * verb the write would refuse.
 */
function memoryEntryEditable(data) {
    return !data.read_only && !data.readOnly &&
        !(data.actions && data.actions.edit === false);
}

/**
 * Read one memory entry through the memory API rather than the workspace file
 * API this page used to use.
 *
 * Why it matters: a memory entry edited as a plain workspace file skipped the
 * version condition and the index publish, so a save could silently overwrite a
 * newer revision and a new body never reached retrieval. The memory surface
 * hands out the revision to send back and publishes the index in the same
 * operation. The editor treats the value opaquely and round-trips it, so the
 * content revision fills the slot an mtime filled for other pages.
 */
async function memoryDocRead(doc) {
    const url = `/api/memory/content?filename=${encodeURIComponent(doc.filename)}` +
        `&category=${encodeURIComponent(doc.category || 'memory')}&${memoryTargetQuery()}`;
    const res = await fetch(url);
    const data = await res.json();
    if (data.status !== 'success') throw new Error(data.message || 'load failed');
    return {
        content: data.content || '',
        mtime: data.revision,
        editable: memoryEntryEditable(data),
    };
}

/** Save one memory entry, version-conditioned, through the memory API. */
async function memoryDocWrite(doc, content, expectedRevision) {
    let revision = expectedRevision;
    if (revision == null) {
        // The user chose "overwrite" over a newer revision, so the *current*
        // revision is what we commit against: the server refuses a blind write
        // of an existing entry by design (that refusal is what protects the
        // other page's edit), so re-reading is how an intentional overwrite is
        // expressed here.
        revision = (await memoryDocRead(doc)).mtime;
    }
    const data = await memoryRequest('/api/memory/save', {
        filename: doc.filename,
        category: doc.category || 'memory',
        content: content,
        revision: revision,
    });
    if (!data) return { status: 'error', code: 'failed' };
    // The editor knows one conflict code; the memory API names the same
    // condition `stale_revision`. Translated here so the page's existing
    // "someone else changed it — overwrite?" flow runs instead of a bare error.
    if (data.code === 'stale_revision') {
        return { ...data, code: 'conflict' };
    }
    // A `pending` index is reported to the user by `memoryRequest`, but the
    // content operation did succeed — so the editor must see success, or it
    // would throw "save failed" over a saved body. The spread comes first so
    // the fields below, not the response's own `status`, decide.
    if (!memorySucceeded(data)) return data;
    return { ...data, status: 'success', mtime: (data.result || {}).revision };
}

/** True when a write response means the content operation is done.
 *
 * ``pending`` counts: the body is committed and only the index is behind, which
 * ``memoryRequest`` has already told the user about. Treating it as failure
 * would have the page claim a saved edit was lost.
 */
function memorySucceeded(data) {
    return !!data && (data.status === 'success' || data.status === 'pending');
}

/**
 * POST one memory write and hand the payload back to the caller.
 *
 * The response is returned for *every* parsed answer, including refusals: the
 * save path has to see the API's own code (``stale_revision``) to run the
 * editor's overwrite flow, so swallowing it here would silently turn a
 * resolvable conflict into a dead end. Only a transport failure yields ``null``.
 */
async function memoryRequest(path, body) {
    try {
        const res = await fetch(path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...body, ...memoryTargetBody() }),
        });
        const data = await res.json();
        if (data.status === 'pending') {
            // The content operation succeeded; the search index did not. Saying
            // nothing would let the user believe retrieval is already updated.
            _wsToast(t('memory_index_pending'));
        }
        return data;
    } catch (e) {
        _memoryRefusal(null);
        return null;
    }
}

/** Report a delete/clear outcome the way the page does; true when it applied. */
function memoryReportMutation(data, okMessage) {
    if (!memorySucceeded(data)) {
        if (data) _memoryRefusal(data);
        return false;
    }
    _wsToast(t(okMessage));
    return true;
}

/** Show delete/clear only where the server says the verb is available. */
function memorySyncDocButtons(state) {
    const doc = memoryEditor.current();
    const editing = !!(state && state.editing);
    const onAgent = viewingMemoryTarget() !== MEMORY_PERSONAL;
    const del = document.getElementById('memory-btn-delete');
    const clear = document.getElementById('memory-btn-clear');
    if (del) {
        del.classList.toggle('hidden',
            editing || !doc || !!doc.readOnly ||
            !!(doc.actions && doc.actions.delete === false));
    }
    if (clear) {
        // Clear is a delete-class verb on the same *root*, and the server
        // reports ``delete: false`` whenever a write to that root is refused
        // (read-only category, or a member on the tenant's shared memory root),
        // so it follows that signal rather than keeping a second rule that
        // could disagree with the one the write enforces.
        clear.classList.toggle('hidden',
            editing || !onAgent ||
            !doc || !!(doc.actions && doc.actions.delete === false));
    }
}

/** Delete the entry on screen, after confirming and after guarding edits. */
function memoryDocDelete() {
    if (!memoryEditor.guard(memoryDocDelete)) return;
    const doc = memoryEditor.current();
    if (!doc) return;
    showConfirmDialog({
        title: t('memory_delete_title'),
        message: t('memory_delete_msg').replace('{name}', doc.filename || ''),
        okText: t('memory_delete_ok'),
        onConfirm: () => {
            memoryRequest('/api/memory/delete',
                { filename: doc.filename, category: doc.category || 'memory' })
                .then((data) => {
                    if (!memoryReportMutation(data, 'memory_deleted')) return;
                    closeMemoryViewer();
                });
        },
    });
}

/** Clear the current category of an Agent's memory, after confirming. */
function memoryDocClear() {
    if (!memoryEditor.guard(memoryDocClear)) return;
    if (viewingMemoryTarget() === MEMORY_PERSONAL) return;
    showConfirmDialog({
        title: t('memory_clear_title'),
        message: t('memory_clear_msg'),
        okText: t('memory_clear_ok'),
        onConfirm: () => {
            memoryRequest('/api/memory/clear',
                { category: memoryCategory || 'memory' }).then((data) => {
                    if (!memoryReportMutation(data, 'memory_cleared')) return;
                    closeMemoryViewer();
                });
        },
    });
}

function openMemoryFile(filename, category) {
    category = category || 'memory';
    fetch(`/api/memory/content?filename=${encodeURIComponent(filename)}&category=${category}&${memoryTargetQuery()}`).then(r => r.json()).then(data => {
        if (data.status !== 'success') return _memoryRefusal(data, { keepList: true });
        document.getElementById('memory-panel-list').classList.add('hidden');
        document.getElementById('memory-panel-viewer').classList.remove('hidden');
        memoryEditor.open({
            filename: filename,
            category: category,
            // The memory API reports where the file sits under the workspace
            // root; kept for display and for the workspace-file fallbacks, but
            // the editor now addresses the entry through the memory API so a
            // save carries the revision and publishes the index.
            relPath: data.rel_path || filename,
            content: data.content || '',
            // Whether this entry may be edited/deleted at all, and the version
            // token a save has to carry back. Both come from the server so the
            // page never offers a verb the write would refuse.
            readOnly: !!data.read_only,
            actions: data.actions || {},
            // The editor asks the document, not the payload, so both reasons
            // above are folded into one flag here and read from one place.
            canEdit: memoryEntryEditable(data),
            revision: data.revision,
        });
    }).catch(() => _memoryRefusal(null, { keepList: true }));
}

function closeMemoryViewer() {
    if (!memoryEditor.guard(closeMemoryViewer)) return;
    memoryEditor.forget();
    document.getElementById('memory-panel-viewer').classList.add('hidden');
    document.getElementById('memory-panel-list').classList.remove('hidden');
    // A save changed the size and timestamp the list shows.
    loadMemoryView(memoryPage);
}

// Reloading or closing the tab drops an unsaved edit. All the browser allows
// here is its own generic prompt, which still beats losing the text in silence.
window.addEventListener('beforeunload', (e) => {
    // A registered view may hold a draft of its own (external connections, task
    // 10.6) — a tenant switch reloads the page, so this is the guard that covers
    // it, along with the in-page editors below.
    if (typeof CONSOLE_VIEW_REGISTRY !== 'undefined' && CONSOLE_VIEW_REGISTRY.some(
            entry => entry && typeof entry.isDirty === 'function' && entry.isDirty())) {
        e.preventDefault();
        e.returnValue = '';
        return;
    }
    if (!memoryEditor.isDirty() && !skillEditor.isDirty() && !brandingDirty) return;
    e.preventDefault();
    e.returnValue = '';
});

// =====================================================================
// Custom Confirm Dialog
// =====================================================================
function showConfirmDialog({ title, message, okText, cancelText, onConfirm, hideCancel }) {
    const overlay = document.getElementById('confirm-dialog-overlay');
    document.getElementById('confirm-dialog-title').textContent = title || '';
    document.getElementById('confirm-dialog-message').textContent = message || '';
    document.getElementById('confirm-dialog-ok').textContent = okText || 'OK';
    const cancelBtn = document.getElementById('confirm-dialog-cancel');
    cancelBtn.textContent = cancelText || t('channels_cancel');
    cancelBtn.classList.toggle('hidden', !!hideCancel);

    function cleanup() {
        overlay.classList.add('hidden');
        okBtn.removeEventListener('click', onOk);
        cancelBtn.removeEventListener('click', onCancel);
        overlay.removeEventListener('click', onOverlayClick);
    }
    function onOk() { cleanup(); if (onConfirm) onConfirm(); }
    function onCancel() { cleanup(); }
    function onOverlayClick(e) { if (e.target === overlay) cleanup(); }

    const okBtn = document.getElementById('confirm-dialog-ok');
    okBtn.addEventListener('click', onOk);
    cancelBtn.addEventListener('click', onCancel);
    overlay.addEventListener('click', onOverlayClick);
    overlay.classList.remove('hidden');
}

// =====================================================================
// Models View
// =====================================================================
// Capability cards rendered on the Models page. Order matters — main model
// comes first because it transitively decides defaults for vision and image.
// Icon palette is grouped by capability family:
//   - chat                       → primary (brand green; the "main" capability)
//   - vision + image             → blue    (everything visual)
//   - asr + tts                  → amber   (everything audio)
//   - embedding                  → purple  (vectors)
//   - search                     → orange  (retrieval)
// Each card uses an explicit `iconClass` string so Tailwind's CDN JIT can
// see the literal class names — dynamic `bg-${color}-50` strings would not
// be picked up reliably.
const MODELS_CAPABILITY_DEFS = [
    { id: 'chat',      icon: 'fa-microchip',        editable: true,  needsModel: true,  toggleable: false, titleKey: 'models_capability_chat',      descKey: 'models_capability_chat_desc',
      iconChip: 'bg-primary-50 dark:bg-primary-900/30',  iconGlyph: 'text-primary-500' },
    // NOTE: the chat fallback is deliberately NOT a top-level card. It is a
    // rarely-touched safety net, so it lives behind a small gear on the main
    // model card (see renderCapabilityHeaderTag / openChatFallbackModal) and
    // is edited in a modal that reuses the same picker machinery.
    { id: 'vision',    icon: 'fa-eye',              editable: true,  needsModel: true,  titleKey: 'models_capability_vision',    descKey: 'models_capability_vision_desc',
      iconChip: 'bg-blue-50 dark:bg-blue-900/30',        iconGlyph: 'text-blue-500' },
    { id: 'image',     icon: 'fa-image',            editable: true,  needsModel: true,  titleKey: 'models_capability_image',     descKey: 'models_capability_image_desc',
      iconChip: 'bg-blue-50 dark:bg-blue-900/30',        iconGlyph: 'text-blue-500' },
    { id: 'asr',       icon: 'fa-microphone',       editable: true,  needsModel: true,  titleKey: 'models_capability_asr',       descKey: 'models_capability_asr_desc',
      iconChip: 'bg-amber-50 dark:bg-amber-900/30',      iconGlyph: 'text-amber-500' },
    { id: 'tts',       icon: 'fa-volume-high',      editable: true,  needsModel: true,  titleKey: 'models_capability_tts',       descKey: 'models_capability_tts_desc',
      iconChip: 'bg-amber-50 dark:bg-amber-900/30',      iconGlyph: 'text-amber-500' },
    { id: 'embedding', icon: 'fa-vector-square',    editable: true,  needsModel: true,  titleKey: 'models_capability_embedding', descKey: 'models_capability_embedding_desc',
      iconChip: 'bg-purple-50 dark:bg-purple-900/30',    iconGlyph: 'text-purple-500' },
    { id: 'search',    icon: 'fa-magnifying-glass', editable: true,  needsModel: false, titleKey: 'models_capability_search',    descKey: 'models_capability_search_desc',
      iconChip: 'bg-orange-50 dark:bg-orange-900/30',    iconGlyph: 'text-orange-500' },
];

// Provider logos: when a real SVG exists under static/logos/<id>.svg we use
// it; otherwise we fall back to a neutral monogram chip. SVGs are fetched
// via <img> with a hidden onerror so layout stays stable when files are
// absent. Vendors whose mark is rendered in pure (or near-pure) black are
// listed in MODELS_PROVIDER_LOGO_DARK_INVERT — for those, we apply a CSS
// invert filter in dark mode so the glyph stays visible against #1A1A1A.
const MODELS_PROVIDER_LOGO_PATH = 'assets/logos';
const MODELS_PROVIDER_LOGO_DARK_INVERT = new Set([
    'openai',     // black wordmark
    'moonshot',   // dark monogram
    'zhipu',      // dark monogram
    'custom',     // single-color slider glyph
]);

let modelsState = { providers: [], capabilities: {} };

// The member's 模型与接入 content: the grant-filtered catalog
// (``GET /api/tenant/authorization/catalog?kind=model&purpose=use``), which is
// the *same* answer the page's ``read_allowed`` is computed from. Kept apart
// from ``modelsState`` on purpose — the vendor grid and the authorized catalog
// are different questions, and one state object would let a stale answer from
// one render into the other's panel.
let memberCatalogState = { items: [] };

// One-shot: { capabilityId, providerId } stashed before a Models reload,
// consumed by renderCapabilityBody to preselect a just-configured vendor.
let pendingCapabilitySelection = null;

// `opts.preserveScroll` keeps the page's vertical scroll position across the
// refresh. We capture it before unhiding the loading skeleton (which collapses
// content height to zero) and restore it after the new content is mounted.
// This matters when the user configures a vendor from inside a capability
// card's dropdown — without preservation, the post-save reload bounces them
// back to the top of the page, away from the card they were configuring.
function loadModelsView(opts) {
    const loading = document.getElementById('models-loading');
    const content = document.getElementById('models-content');
    if (!loading || !content) return;
    const preserveScroll = !!(opts && opts.preserveScroll);
    // The Models pane has its own scrollable container; capture its position
    // (not window.scrollY) so we can put the user back exactly where they were.
    const scroller = document.querySelector('#view-config .overflow-y-auto');
    const savedTop = preserveScroll && scroller ? scroller.scrollTop : null;

    loading.classList.remove('hidden');
    content.classList.add('hidden');

    fetch('/api/models').then(r => r.json()).then(data => {
        if (data.status !== 'success') {
            loading.innerHTML = `<span class="text-sm text-red-400">${escapeHtml(data.message || 'Failed to load')}</span>`;
            return;
        }
        modelsState.providers = data.providers || [];
        modelsState.capabilities = data.capabilities || {};
        renderModelsView();
        loading.classList.add('hidden');
        content.classList.remove('hidden');
        if (savedTop !== null && scroller) {
            // Wait one frame for the new layout to settle, otherwise the
            // restored scrollTop snaps to the previous (smaller) max.
            requestAnimationFrame(() => { scroller.scrollTop = savedTop; });
        }
    }).catch(err => {
        loading.innerHTML = `<span class="text-sm text-red-400">${escapeHtml(String(err))}</span>`;
    });
}

function renderModelsView() {
    const container = document.getElementById('models-content');
    container.innerHTML = '';
    container.appendChild(renderVendorsSection());
    MODELS_CAPABILITY_DEFS.forEach(def => container.appendChild(renderCapabilityCard(def)));
}

// ---------- Member model catalog (task 5.4) ----------------------------
//
// 模型与接入 for everyone who is not the platform's model-service maintainer:
// the models they are authorized for, and nothing about how the service behind
// them is configured. ``purpose=use`` is the endpoint's own name for "what the
// caller may use" — the same query the page projection is computed from — so a
// page that is open always has rows to render, and a member's catalog is
// whatever their grants say rather than whatever the deployment has installed.

function loadMemberCatalog() {
    const loading = document.getElementById('catalog-loading');
    const content = document.getElementById('catalog-content');
    if (!loading || !content) return;
    loading.classList.remove('hidden');
    content.classList.add('hidden');

    fetch('/api/tenant/authorization/catalog?kind=model&purpose=use&page_size=200')
        .then(r => r.json()).then(data => {
            if (data.status !== 'success') {
                loading.innerHTML = `<span class="text-sm text-red-400">${escapeHtml(data.message || 'Failed to load')}</span>`;
                return;
            }
            memberCatalogState.items = data.items || [];
            renderMemberCatalog();
            loading.classList.add('hidden');
            content.classList.remove('hidden');
        }).catch(err => {
            loading.innerHTML = `<span class="text-sm text-red-400">${escapeHtml(String(err))}</span>`;
        });
}

function renderMemberCatalog() {
    const container = document.getElementById('catalog-content');
    if (!container) return;
    container.innerHTML = '';

    const wrap = document.createElement('div');
    wrap.className = 'bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10 p-6';
    const header = `
        <div class="flex items-start gap-3 mb-5">
            <div class="w-9 h-9 rounded-lg bg-primary-50 dark:bg-primary-900/30 flex items-center justify-center flex-shrink-0">
                <i class="fas fa-microchip text-primary-500 text-sm"></i>
            </div>
            <div class="flex-1 min-w-0">
                <h3 class="font-semibold text-slate-800 dark:text-slate-100">${t('models_catalog_title')}</h3>
                <p class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">${t('models_catalog_desc')}</p>
            </div>
        </div>`;

    const items = memberCatalogState.items || [];
    let body;
    if (items.length === 0) {
        body = `
            <div class="flex flex-col items-center justify-center py-8 px-4 rounded-lg border border-dashed border-slate-200 dark:border-white/10">
                <p class="text-sm text-slate-500 dark:text-slate-400 text-center">${t('models_catalog_empty')}</p>
            </div>`;
    } else {
        body = `<div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            ${items.map(renderMemberCatalogRow).join('')}
        </div>`;
    }
    wrap.innerHTML = header + body;
    container.appendChild(wrap);
}

function renderMemberCatalogRow(item) {
    // ``resource_id`` is ``provider:<provider>:<model>``; the middleware name is
    // what a person recognises, so it is the title and the provider is the chip.
    const parts = String(item.resource_id || '').split(':');
    const provider = parts.length > 2 ? parts[1] : '';
    const model = item.name || parts[parts.length - 1] || '';
    return `
        <div class="flex items-center gap-3 px-3 py-2.5 rounded-lg border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-white/5 text-left">
            <i class="fas fa-cube text-[11px] text-slate-400 dark:text-slate-500"></i>
            <span class="flex-1 min-w-0">
                <span class="block text-sm font-medium text-slate-800 dark:text-slate-100 truncate">${escapeHtml(model)}</span>
                ${provider ? `<span class="block text-[11px] text-slate-500 dark:text-slate-400 truncate">${escapeHtml(provider)}</span>` : ''}
            </span>
        </div>`;
}

// True when a provider card is one of the expanded custom (OpenAI-compatible)
// providers (id "custom:<id>") — shown in the vendor grid alongside built-in
// vendors, but edited via the dedicated custom-provider modal.
function isCustomProviderCard(p) {
    return !!(p && p.is_custom && p.custom_name);
}

// ---------- Vendor section (Layer 1) -----------------------------------

function renderVendorsSection() {
    const wrap = document.createElement('div');
    wrap.className = 'bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10 p-6';

    // Custom providers always show once created (even without an api key,
    // e.g. a local vLLM/Ollama endpoint); built-in vendors show when configured.
    const configured = modelsState.providers.filter(p => p.configured || isCustomProviderCard(p));

    const header = `
        <div class="flex items-start gap-3 mb-5">
            <div class="w-9 h-9 rounded-lg bg-primary-50 dark:bg-primary-900/30 flex items-center justify-center flex-shrink-0">
                <i class="fas fa-key text-primary-500 text-sm"></i>
            </div>
            <div class="flex-1 min-w-0">
                <h3 class="font-semibold text-slate-800 dark:text-slate-100">${t('models_section_vendors')}</h3>
                <p class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">${t('models_section_vendors_desc')}</p>
            </div>
        </div>`;

    let body;
    if (configured.length === 0) {
        body = `
            <div class="flex flex-col items-center justify-center py-8 px-4 rounded-lg border border-dashed border-slate-200 dark:border-white/10">
                <p class="text-sm text-slate-500 dark:text-slate-400 text-center">${t('models_not_configured')}</p>
                <button onclick="openVendorModal('')"
                        class="mt-3 px-3 py-1.5 rounded-lg text-xs font-medium bg-primary-50 dark:bg-primary-900/30 text-primary-600 dark:text-primary-400 hover:bg-primary-100 dark:hover:bg-primary-900/50 cursor-pointer transition-colors">
                    <i class="fas fa-plus text-[10px] mr-1"></i>${t('models_add_vendor')}
                </button>
            </div>`;
    } else {
        // Existing vendors as chips, plus a trailing "add" tile so a new
        // built-in or custom provider can still be added once at least one is
        // already configured (otherwise the add entry only showed on the empty
        // state). openVendorModal('') opens the picker → built-in or custom.
        const addTile = `
            <button onclick="openVendorModal('')"
                    class="flex items-center justify-center gap-2 px-3 py-2.5 rounded-lg border border-dashed
                           border-slate-300 dark:border-white/15 text-slate-500 dark:text-slate-400
                           hover:border-primary-400 hover:text-primary-500 cursor-pointer transition-colors text-sm">
                <i class="fas fa-plus text-[11px]"></i>${t('models_add_vendor')}
            </button>`;
        body = `<div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
            ${configured.map(renderVendorChip).join('')}
            ${addTile}
        </div>`;
    }

    wrap.innerHTML = header + body;
    return wrap;
}

function renderVendorChip(p) {
    // The masked API key is intentionally not surfaced here; it is shown
    // inside the edit modal so the chip stays uncluttered and scannable.
    // Custom providers open their dedicated modal (name + base + key);
    // their ids are server-generated hex, safe to inline.
    const onclick = isCustomProviderCard(p)
        ? `openCustomProviderModal('${escapeHtml(p.custom_id)}')`
        : `openVendorModal('${escapeHtml(p.id)}')`;
    // The catalogue entry is a sibling of the edit button, not nested inside it:
    // a button inside a button is invalid markup and swallows the click. The
    // card id is a server-provided provider key, escaped before it reaches the
    // inline handler.
    const catalogId = escapeHtml(p.id);
    return `
        <div class="group flex items-center gap-1 px-3 py-2.5 rounded-lg border border-slate-200 dark:border-white/10
                    bg-slate-50 dark:bg-white/5 hover:border-primary-300 dark:hover:border-primary-500/50
                    transition-colors duration-150">
            <button onclick="${onclick}" class="flex items-center gap-3 flex-1 min-w-0 text-left cursor-pointer">
                ${renderProviderLogo(p, 28)}
                <span class="flex-1 min-w-0 text-sm font-medium text-slate-800 dark:text-slate-100 truncate">${escapeHtml(localizedLabel(p.label))}</span>
                <i class="fas fa-pen-to-square text-[11px] text-slate-400 dark:text-slate-500 group-hover:text-primary-500 transition-colors"></i>
            </button>
            <button type="button" onclick="openModelCatalogModal('${catalogId}')"
                    title="${escapeHtml(t('models_catalog_modal_title'))}"
                    class="flex-shrink-0 w-7 h-7 rounded-md flex items-center justify-center text-slate-400
                           dark:text-slate-500 hover:text-primary-500 dark:hover:text-primary-400
                           hover:bg-primary-50 dark:hover:bg-primary-900/30 cursor-pointer transition-colors">
                <i class="fas fa-list-ul text-[11px]"></i>
            </button>
        </div>`;
}

// Per-provider model catalogue editor (P3). The provider card already carries
// the editor's inputs -- `seed` (presets), `catalog` (the stored overrides) and
// `hidden` (tombstones) -- so the draft is diffed against those rather than
// against the effective list, which is what keeps un-edited presets following
// the server instead of being frozen into an override on every save.
function openModelCatalogModal(providerId) {
    const existing = document.getElementById('model-catalog-modal-overlay');
    if (existing) existing.remove();
    const provider = (modelsState.providers || []).find(p => p.id === providerId);
    if (!provider) return;

    const overlay = document.createElement('div');
    overlay.id = 'model-catalog-modal-overlay';
    overlay.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4';
    overlay.innerHTML = `
        <div class="w-full max-w-lg max-h-[80vh] flex flex-col rounded-2xl bg-white dark:bg-[#1A1A1A] border border-slate-200 dark:border-white/10 shadow-xl">
            <div class="flex items-start gap-3 px-6 pt-6 pb-4 border-b border-slate-100 dark:border-white/5">
                <div class="flex-1 min-w-0">
                    <h3 class="font-semibold text-slate-800 dark:text-slate-100">${escapeHtml(t('models_catalog_modal_title'))}</h3>
                    <p class="text-xs text-slate-500 dark:text-slate-400 mt-1 leading-relaxed">${escapeHtml(localizedLabel(provider.label))} · ${escapeHtml(t('models_catalog_context_window'))}</p>
                </div>
                <button type="button" onclick="closeModelCatalogModal()"
                        class="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 cursor-pointer transition-colors flex-shrink-0">
                    <i class="fas fa-xmark"></i>
                </button>
            </div>
            <div class="px-6 py-5 overflow-y-auto" data-catalog-body="1"></div>
        </div>`;
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModelCatalogModal(); });
    document.body.appendChild(overlay);

    const models = window.RdaiFunctionalModels;
    const body = overlay.querySelector('[data-catalog-body="1"]');
    if (!models || typeof models.mountCatalog !== 'function') {
        // The module is `defer`-loaded ahead of console.js; a page served from a
        // stale cache can still lack it, and a missing editor must say so rather
        // than silently show an empty dialog.
        body.innerHTML = `<p class="text-sm text-slate-500">${escapeHtml(t('models_save_failed'))}</p>`;
        return;
    }
    overlay._rdaiCatalog = models.mountCatalog({
        root: body,
        provider: provider,
        save: _postModelsPayload,
        reload: () => _fetchModelsPayload().then(data =>
            (data.providers || []).find(p => p.id === providerId) || provider),
        t: t,
    });
}

function closeModelCatalogModal() {
    const overlay = document.getElementById('model-catalog-modal-overlay');
    if (overlay) {
        if (overlay._rdaiCatalog && typeof overlay._rdaiCatalog.dispose === 'function') {
            overlay._rdaiCatalog.dispose();
            overlay._rdaiCatalog = null;
        }
        overlay.remove();
    }
}

// Render a uniformly-styled logo for a provider. Tries an SVG asset first; if
// it 404s the <img> swaps itself for a monogram fallback via onerror.
function renderProviderLogo(p, sizePx) {
    const initial = (localizedLabel(p.label) || p.id || '?').slice(0, 1).toUpperCase();
    const sz = sizePx || 32;
    const url = `${MODELS_PROVIDER_LOGO_PATH}/${encodeURIComponent(p.id)}.svg`;
    const fallbackId = `pl-${p.id}-${Math.random().toString(36).slice(2, 8)}`;
    const imgClass = MODELS_PROVIDER_LOGO_DARK_INVERT.has(p.id)
        ? 'absolute inset-0 m-auto provider-logo-img provider-logo-invert-dark'
        : 'absolute inset-0 m-auto provider-logo-img';
    return `
        <span class="relative flex items-center justify-center rounded-lg bg-slate-100 dark:bg-white/10
                     text-slate-600 dark:text-slate-300 flex-shrink-0 overflow-hidden"
              style="width:${sz}px;height:${sz}px;">
            <span id="${fallbackId}" class="text-xs font-bold">${escapeHtml(initial)}</span>
            <img src="${url}" alt="" aria-hidden="true"
                 class="${imgClass}"
                 style="width:${Math.round(sz * 0.65)}px;height:${Math.round(sz * 0.65)}px;"
                 onload="(function(el){var f=document.getElementById('${fallbackId}');if(f)f.style.display='none';})(this)"
                 onerror="this.remove();">
        </span>`;
}

function getCustomProviderCards() {
    return modelsState.providers.filter(isCustomProviderCard);
}

// ---------- Capability cards (Layer 2) ---------------------------------

function renderCapabilityCard(def) {
    const cap = modelsState.capabilities[def.id] || {};
    const wrap = document.createElement('div');
    wrap.className = 'bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10 p-6';
    wrap.id = `models-card-${def.id}`;

    const headerRight = renderCapabilityHeaderTag(def, cap);

    wrap.innerHTML = `
        <div class="flex items-start gap-3 mb-5">
            <div class="w-9 h-9 rounded-lg ${def.iconChip} flex items-center justify-center flex-shrink-0">
                <i class="fas ${def.icon} ${def.iconGlyph} text-sm"></i>
            </div>
            <div class="flex-1 min-w-0">
                <h3 class="font-semibold text-slate-800 dark:text-slate-100">${t(def.titleKey)}</h3>
                <p class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">${t(def.descKey)}</p>
            </div>
            ${headerRight}
        </div>
        <div class="space-y-4" data-cap-body="${def.id}"></div>`;

    const body = wrap.querySelector(`[data-cap-body="${def.id}"]`);
    renderCapabilityBody(def, cap, body);
    return wrap;
}

function renderCapabilityHeaderTag(def, cap) {
    // The main model card carries a small gear that opens the chat-fallback
    // modal. The fallback is a rarely-touched safety net, so it stays out of
    // the card body; a badge appears next to the gear only while it is on, so
    // an active fallback is still discoverable at a glance.
    if (def.id === 'chat') {
        const fb = modelsState.capabilities.chat_fallback || {};
        // A single entry point that also reflects state: green + "on" label
        // when the fallback is enabled, muted + "configure" label when off.
        const on = !!fb.enabled;
        const cls = on
            ? 'text-primary-600 dark:text-primary-300 bg-primary-50 dark:bg-primary-900/30 hover:bg-primary-100 dark:hover:bg-primary-900/50'
            : 'text-slate-500 dark:text-slate-400 hover:text-primary-600 dark:hover:text-primary-300 hover:bg-slate-100 dark:hover:bg-white/5';
        const label = on ? t('models_fallback_badge_on') : t('models_fallback_config');
        return `
            <button type="button" onclick="openChatFallbackModal()"
                    title="${escapeHtml(t('models_fallback_config_tip'))}"
                    class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs flex-shrink-0
                           cursor-pointer transition-colors ${cls}">
                <i class="fas fa-shield-halved text-[11px]"></i>${label}
            </button>`;
    }
    return '';
}

// The chat fallback is configured in a modal rather than as a top-level card
// (it is a rarely-touched safety net). The modal body reuses the exact same
// picker machinery as a capability card — `renderCapabilityBody` keys every
// element off `cap-chat_fallback-*`, so we hand it a def with that id and let
// the existing provider/model/toggle/save code run unchanged. No such card is
// registered in MODELS_CAPABILITY_DEFS, so the ids never collide.
const CHAT_FALLBACK_DEF = {
    id: 'chat_fallback', editable: true, needsModel: true, toggleable: true,
    titleKey: 'models_fallback_modal_title', descKey: 'models_capability_chat_fallback_desc',
};

// Resolve a capability def by id. The chat fallback is intentionally absent
// from MODELS_CAPABILITY_DEFS (it renders in a modal, not as a card), so the
// shared save/toggle handlers look it up here too.
function capabilityDefById(capId) {
    if (capId === 'chat_fallback') return CHAT_FALLBACK_DEF;
    return MODELS_CAPABILITY_DEFS.find(d => d.id === capId);
}

function openChatFallbackModal() {
    closeChatFallbackModal(); // never stack two

    const overlay = document.createElement('div');
    overlay.id = 'chat-fallback-modal-overlay';
    overlay.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4';
    overlay.innerHTML = `
        <div class="w-full max-w-md rounded-2xl bg-white dark:bg-[#1A1A1A] border border-slate-200 dark:border-white/10 shadow-xl">
            <div class="flex items-start gap-3 px-6 pt-6 pb-4 border-b border-slate-100 dark:border-white/5">
                <div class="w-9 h-9 rounded-lg bg-primary-50 dark:bg-primary-900/30 flex items-center justify-center flex-shrink-0">
                    <i class="fas fa-shield-halved text-primary-500 text-sm"></i>
                </div>
                <div class="flex-1 min-w-0">
                    <h3 class="font-semibold text-slate-800 dark:text-slate-100">${t('models_fallback_modal_title')}</h3>
                    <p class="text-xs text-slate-500 dark:text-slate-400 mt-1 leading-relaxed">${t('models_fallback_modal_desc')}</p>
                </div>
                <button type="button" onclick="closeChatFallbackModal()"
                        class="text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 cursor-pointer transition-colors flex-shrink-0">
                    <i class="fas fa-xmark"></i>
                </button>
            </div>
            <div class="px-6 py-5 space-y-4" data-cap-body="chat_fallback"></div>
        </div>`;

    // Close on backdrop click (but not when clicking inside the dialog).
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeChatFallbackModal(); });

    document.body.appendChild(overlay);

    const cap = modelsState.capabilities.chat_fallback || {};
    const body = overlay.querySelector('[data-cap-body="chat_fallback"]');
    // The ordered multi-node editor (change integrate-upstream-core-capabilities,
    // P3) replaces the single-provider picker: one node per row, order is
    // meaningful and the whole chain survives a disable. The legacy renderer
    // stays as the fallback so an older cached page keeps working.
    const models = window.RdaiFunctionalModels;
    if (models && typeof models.mountFallback === 'function') {
        try {
            overlay._rdaiFallback = models.mountFallback({
                root: body,
                capability: cap,
                save: _postModelsPayload,
                reload: () => _fetchModelsPayload().then(data => (data.capabilities || {}).chat_fallback || {}),
                t: t,
            });
            return;
        } catch (err) {
            console.warn('[Models] functional fallback editor unavailable', err);
        }
    }
    renderCapabilityBody(CHAT_FALLBACK_DEF, cap, body);
}

function closeChatFallbackModal() {
    const overlay = document.getElementById('chat-fallback-modal-overlay');
    if (overlay) {
        // Detach listeners/timers before the node goes away; a disposed editor
        // also stops applying a save result to a modal that no longer exists.
        if (overlay._rdaiFallback && typeof overlay._rdaiFallback.dispose === 'function') {
            overlay._rdaiFallback.dispose();
            overlay._rdaiFallback = null;
        }
        overlay.remove();
    }
}

// POST one /api/models payload and resolve with the response body only when
// both the HTTP status and body.status say success. A 200 carrying
// status:"error" is a real failure in this API (unlike the rest of the console
// surface), so callers must not treat "it did not throw" as saved.
function _postModelsPayload(payload) {
    return fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    }).then(resp => resp.json().then(data => {
        if (!resp.ok || data.status !== 'success') {
            throw new Error(data.message || data.code || ('HTTP ' + resp.status));
        }
        return data;
    }));
}

// Re-read the whole models payload; used to re-seed an editor from the server
// after a save so the surface shows what was actually stored, not what was
// submitted.
function _fetchModelsPayload() {
    return fetch('/api/models', { credentials: 'same-origin', cache: 'no-store' })
        .then(resp => resp.json())
        .then(data => {
            if (data.status !== 'success') throw new Error(data.message || 'load failed');
            modelsState.providers = data.providers || modelsState.providers;
            modelsState.capabilities = data.capabilities || modelsState.capabilities;
            return data;
        });
}

function _searchProviderLabel(cap, providerId) {
    const list = (cap && cap.providers) || [];
    const hit = list.find(p => p.id === providerId);
    return hit ? localizedLabel(hit.label) : providerId;
}

// Search card body: strategy picker + (when fixed) provider picker + a
// status row that surfaces which providers are ready and how to add the
// missing ones. Three of the four backends piggy-back on model-vendor
// credentials (zhipu / qianfan / linkai); bocha owns its own key under
// tools.web_search and gets its own minimal credential modal.
function renderSearchCapability(def, cap, body) {
    const providers = cap.providers || [];
    const configuredIds = cap.configured_providers || [];
    const hasAny = configuredIds.length > 0;
    const strategy = cap.strategy || 'auto';

    body.innerHTML = `
        <div>
            <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${t('models_search_strategy_label')}</label>
            <div id="cap-search-strategy" class="cfg-dropdown" tabindex="0">
                <div class="cfg-dropdown-selected">
                    <span class="cfg-dropdown-text">--</span>
                    <i class="fas fa-chevron-down cfg-dropdown-arrow"></i>
                </div>
                <div class="cfg-dropdown-menu"></div>
            </div>
        </div>
        <div id="cap-search-provider-wrap" class="hidden">
            <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${t('models_provider')}</label>
            <div id="cap-search-provider" class="cfg-dropdown" tabindex="0">
                <div class="cfg-dropdown-selected">
                    <span class="cfg-dropdown-text">--</span>
                    <i class="fas fa-chevron-down cfg-dropdown-arrow"></i>
                </div>
                <div class="cfg-dropdown-menu"></div>
            </div>
        </div>
        <div id="cap-search-summary"></div>
        <div class="flex items-center justify-end gap-3 pt-1">
            <span id="cap-search-status" class="text-xs text-primary-500 opacity-0 transition-opacity duration-300"></span>
            <button onclick="saveSearchCapability()"
                    class="px-4 py-2 rounded-lg bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium
                           cursor-pointer transition-colors duration-150 disabled:opacity-50 disabled:cursor-not-allowed">
                ${t('save')}
            </button>
        </div>
    `;

    // Strategy dropdown — when no provider is configured the strategy
    // value is meaningless, so we show a "待配置" placeholder instead of
    // a default selection. Once any provider gets configured the saved
    // strategy (or "auto") becomes the active value.
    initDropdown(
        body.querySelector('#cap-search-strategy'),
        [
            { value: 'auto',  label: t('models_strategy_auto'),         hint: t('models_search_strategy_auto_hint') },
            { value: 'fixed', label: t('models_search_strategy_fixed'), hint: t('models_search_strategy_fixed_hint') },
        ],
        hasAny ? strategy : '',
        (value) => _onSearchStrategyChange(cap, value, body),
        hasAny ? null : { placeholder: t('models_pending_config') },
    );

    // Provider dropdown — populated with configured providers only;
    // unconfigured ones cannot be pinned (they'd silently fall back).
    const provOpts = configuredIds.map(id => ({
        value: id,
        label: _searchProviderLabel(cap, id),
    }));
    if (provOpts.length === 0) provOpts.push({ value: '', label: '--' });
    initDropdown(
        body.querySelector('#cap-search-provider'),
        provOpts,
        cap.fixed_provider || configuredIds[0] || '',
        () => {},
    );

    _renderSearchSummary(body, cap);
    _setSearchProviderPickerVisible(body, strategy === 'fixed' && hasAny);
}

function _onSearchStrategyChange(cap, value, body) {
    const configuredIds = cap.configured_providers || [];
    _setSearchProviderPickerVisible(body, value === 'fixed' && configuredIds.length > 0);
}

function _setSearchProviderPickerVisible(body, visible) {
    const wrap = body.querySelector('#cap-search-provider-wrap');
    if (!wrap) return;
    if (visible) wrap.classList.remove('hidden');
    else wrap.classList.add('hidden');
}

// Search summary line: just lists configured providers + a trailing "+
// add" button. Unconfigured backends are hidden — the user picks one from
// a small chooser when they click add. Empty state surfaces the same add
// button as a primary CTA.
function _renderSearchSummary(body, cap) {
    const host = body.querySelector('#cap-search-summary');
    if (!host) return;
    const providers = cap.providers || [];
    const configured = providers.filter(p => p.configured);
    const missing = providers.filter(p => !p.configured);

    const addBtn = missing.length
        ? `<button type="button" id="cap-search-add-btn"
                  class="inline-flex items-center gap-1 px-2 py-0.5 text-[11px] rounded-md cursor-pointer
                         bg-slate-100 dark:bg-white/5 text-slate-500 dark:text-slate-400
                         hover:bg-slate-200 dark:hover:bg-white/10 transition-colors">
              <i class="fas fa-plus text-[10px]"></i>${t('models_search_add_provider')}
           </button>`
        : '';

    if (configured.length === 0) {
        host.innerHTML = `
            <div class="flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
                <i class="fas fa-circle-info text-[10px] text-amber-500"></i>
                <span>${t('models_search_none_configured')}</span>
                ${addBtn}
            </div>
        `;
    } else {
        const chips = configured.map(p => `
            <button type="button" data-search-edit-provider="${p.id}"
                    title="${t('models_search_edit_hint')}"
                    class="inline-flex items-center gap-1 px-2 py-0.5 text-[11px] rounded-md cursor-pointer
                           bg-emerald-50 dark:bg-emerald-900/30 text-emerald-600 dark:text-emerald-400
                           hover:bg-emerald-100 dark:hover:bg-emerald-900/50 transition-colors">
                <i class="fas fa-check text-[10px]"></i>${escapeHtml(localizedLabel(p.label))}
            </button>
        `).join('');
        host.innerHTML = `
            <div class="flex items-center flex-wrap gap-2 text-xs text-slate-500 dark:text-slate-400">
                <span>${t('models_search_available_label')}</span>
                ${chips}
                ${addBtn}
            </div>
        `;
    }

    const addBtnEl = host.querySelector('#cap-search-add-btn');
    if (addBtnEl) {
        addBtnEl.addEventListener('click', (ev) => {
            ev.preventDefault();
            openSearchAddProviderPicker(missing);
        });
    }
    host.querySelectorAll('[data-search-edit-provider]').forEach(el => {
        el.addEventListener('click', (ev) => {
            ev.preventDefault();
            const pid = el.getAttribute('data-search-edit-provider');
            const meta = (cap.providers || []).find(p => p.id === pid);
            _launchSearchProviderConfig(pid, meta);
        });
    });
}

// Two-step add flow: click "+ 添加厂商" -> chooser dialog -> per-provider
// credential editor. Bocha lands on the dedicated key modal; the others
// piggy-back on the existing vendor credential modal.
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
    });
}

// Search providers that own a dedicated credential — an API key, or SearXNG's
// instance URL. The rest (zhipu/qianfan/linkai) reuse a model-vendor
// credential and keep the vendor modal. Mirrors the backend's
// `needs_dedicated_key` / `needs_url` flags in ModelsHandler._search_capability,
// so a provider the runtime supports can always be configured here.
const DEDICATED_SEARCH_CREDENTIALS = ['bocha', 'anysearch', 'serply', 'tavily', 'searxng', 'keenable'];

function _launchSearchProviderConfig(providerId, providerMeta) {
    if (DEDICATED_SEARCH_CREDENTIALS.indexOf(providerId) !== -1) {
        openSearchKeyModal(providerId, providerMeta);
    } else {
        openVendorModal(providerId, () => loadModelsView({ preserveScroll: true }));
    }
}

function saveSearchCapability() {
    const strategyDd = document.getElementById('cap-search-strategy');
    const providerDd = document.getElementById('cap-search-provider');
    const strategy = strategyDd ? getDropdownValue(strategyDd) : 'auto';
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
    }
    const hasKey = !!masked;
    // anysearch/keenable can be on without a key (their keyless tier), and that
    // state still needs the clear button so it can be turned back off.
    const isAnonymous = (providerId === 'anysearch' || providerId === 'keenable')
        && !!((providerMeta && providerMeta.anonymous) || (provider && provider.anonymous));
    const clearBtnHtml = (hasKey || isAnonymous)
        ? `<button type="button" id="search-key-clear"
                  class="px-3 py-1.5 rounded-md text-xs text-red-500 dark:text-red-400
                         hover:bg-red-50 dark:hover:bg-red-900/20 cursor-pointer transition-colors">
              ${t('models_clear_credential')}
           </button>`
        : '';
    // Saving empty on anysearch turns its keyless tier on, so say so.
    const descText = providerId === 'anysearch'
        ? t('models_search_anysearch_desc') + ' ' + t('models_search_anysearch_anon_hint')
        : t('models_search_' + providerId + '_desc');

    const modal = document.createElement('div');
    modal.id = 'search-key-modal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm';
    modal.innerHTML = `
        <div id="search-key-modal-card"
             class="bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10
                    w-full max-w-md mx-4 p-6 shadow-xl">
            <h3 class="text-lg font-semibold text-slate-800 dark:text-slate-100 mb-1">${t('models_search_' + providerId + '_title')}</h3>
            <p class="text-xs text-slate-500 dark:text-slate-400 mb-4">${descText}</p>
            <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${isSearxng ? 'Instance URL' : 'API Key'}</label>
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
            </div>
        </div>
    `;
    document.body.appendChild(modal);

    // Reset masked sentinel as soon as the user starts editing so the save
    // handler can tell apart "kept the existing key" vs "typed a new one".
    const input = document.getElementById('search-key-input');
    if (input) {
        const unmask = () => {
            if (input.dataset.masked === '1') {
                input.value = '';
                input.dataset.masked = '';
                input.classList.remove('cfg-key-masked');
            }
        };
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Tab' || e.key === 'Escape') return;
            unmask();
        });
        input.addEventListener('paste', unmask);
        if (!hasKey) setTimeout(() => input.focus(), 50);
    }
    const clearBtn = document.getElementById('search-key-clear');
    if (clearBtn) clearBtn.addEventListener('click', () => _clearSearchKey(providerId));

    modal.addEventListener('mousedown', (e) => {
        if (e.target === modal) modal.remove();
    });
    const onKey = (e) => {
        if (e.key === 'Escape') {
            modal.remove();
            document.removeEventListener('keydown', onKey);
        }
    };
    document.addEventListener('keydown', onKey);
}

function _saveSearchKey(providerId) {
    const input = document.getElementById('search-key-input');
    if (!input) return;
    // Untouched masked value => no change requested; close silently.
    if (input.dataset.masked === '1') {
        const modal = document.getElementById('search-key-modal');
        if (modal) modal.remove();
        return;
    }
    const apiKey = input.value.trim();

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

    if (!apiKey) {
        input.focus();
        return;
    }
    _postSearchCredential({ action: 'set_search_credential', provider: providerId, api_key: apiKey });
}

// POST one credential change and close the dialog on success.
function _postSearchCredential(body) {
    fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    }).then(r => r.json()).then(data => {
        if (data.status === 'success') {
            const modal = document.getElementById('search-key-modal');
            if (modal) modal.remove();
            loadModelsView({ preserveScroll: true });
        }
    });
}

function _clearSearchKey(providerId) {
    // SearXNG is cleared by emptying its instance URL, not an API key. For
    // anysearch/keenable an empty key with `anonymous` absent also turns the
    // keyless tier back off, which is what the clear button means there.
    const body = providerId === 'searxng'
        ? { action: 'set_search_credential', provider: providerId, url: '' }
        : { action: 'set_search_credential', provider: providerId, api_key: '' };
    _postSearchCredential(body);
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
                <div class="cfg-dropdown-selected">
                    <span class="cfg-dropdown-text">--</span>
                    <i class="fas fa-chevron-down cfg-dropdown-arrow"></i>
                </div>
                <div class="cfg-dropdown-menu"></div>
            </div>
            <div id="cap-${def.id}-model-custom-wrap" class="mt-2 hidden">
                <input id="cap-${def.id}-model-custom" type="text"
                       class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-600
                              bg-slate-50 dark:bg-white/5 text-sm text-slate-800 dark:text-slate-100
                              focus:outline-none focus:border-primary-500 font-mono transition-colors"
                       placeholder="custom model name">
            </div>
        </div>` : '';

    const dimHtml = (def.id === 'embedding' && cap.current_dim) ? `
        <p class="text-xs text-slate-400 dark:text-slate-500">
            <i class="fas fa-cube text-[10px] mr-1"></i>${t('models_dim_label')}: <span class="font-mono">${cap.current_dim}</span>
        </p>` : '';

    // Opt-in capabilities get an on/off switch above the pickers. Everything
    // below it is hidden while off, so a disabled fallback never looks like an
    // unconfigured one — it is simply not part of the setup.
    const toggleHtml = def.toggleable ? `
        <div id="cap-${def.id}-toggle-wrap" class="flex items-center justify-between gap-3">
            <label class="text-sm font-medium text-slate-600 dark:text-slate-400">${t('models_fallback_enable')}</label>
            <button type="button" id="cap-${def.id}-toggle" role="switch"
                    aria-checked="${cap.enabled ? 'true' : 'false'}"
                    onclick="toggleCapabilityEnabled('${def.id}')"
                    class="relative inline-flex h-5 w-9 flex-shrink-0 items-center rounded-full transition-colors cursor-pointer ${cap.enabled ? 'bg-primary-500' : 'bg-slate-200 dark:bg-slate-700'}">
                <span class="inline-block h-3.5 w-3.5 rounded-full bg-white shadow transition-transform ${cap.enabled ? 'translate-x-[18px]' : 'translate-x-[3px]'}"></span>
            </button>
        </div>` : '';

    // Footer layout: a "hint slot" (filled later by renderCapabilityHints for
    // auto-mode cards) sits on the left while status + save stay anchored on
    // the right. Keeping them on the same row means the save button hugs the
    // inputs above instead of being pushed down by a separate hint line.
    const footer = `
        <div class="flex items-center justify-between gap-3 pt-1">
            <div data-cap-hint="${def.id}" class="flex-1 min-w-0"></div>
            <div class="flex items-center gap-3 flex-shrink-0">
                <span id="cap-${def.id}-status" class="text-xs text-primary-500 opacity-0 transition-opacity duration-300"></span>
                <button onclick="saveCapability('${def.id}')"
                        class="px-4 py-2 rounded-lg bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium
                               cursor-pointer transition-colors duration-150 disabled:opacity-50 disabled:cursor-not-allowed">
                    ${t('save')}
                </button>
            </div>
        </div>`;

    // Pickers live in their own wrapper so a disabled opt-in capability can
    // hide them as a group (the toggle itself stays visible above). The
    // wrapper carries its own `space-y-4` because the body's `space-y-4` only
    // applies to *direct* children: without it the provider/model rows would
    // collapse against each other (and against the label above them).
    const pickersHtml = `<div id="cap-${def.id}-pickers" class="space-y-4">${providerHtml + modelHtml + dimHtml}</div>`;
    body.innerHTML = toggleHtml + pickersHtml + footer;

    // TTS: mount reply-mode above provider; defer off-mode toggle to the end.
    if (def.id === 'tts') {
        renderVoiceReplyMode(body, cap.reply_mode || 'off', { skipVisibilityToggle: true });
        // Voice-timbre picker depends on provider+model; rebuilt by callbacks.
        const modelWrap = body.querySelector(`#cap-${def.id}-model-wrap`);
        if (modelWrap) {
            const voiceWrap = document.createElement('div');
            voiceWrap.id = `cap-${def.id}-voice-wrap`;
            voiceWrap.innerHTML = `
                <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${t('models_voice')}</label>
                <div id="cap-${def.id}-voice" class="cfg-dropdown" tabindex="0">
                    <div class="cfg-dropdown-selected">
                        <span class="cfg-dropdown-text">--</span>
                        <i class="fas fa-chevron-down cfg-dropdown-arrow"></i>
                    </div>
                    <div class="cfg-dropdown-menu"></div>
                </div>
                <div id="cap-${def.id}-voice-custom-wrap" class="hidden mt-2">
                    <input id="cap-${def.id}-voice-custom" type="text"
                           class="w-full px-3 py-2 text-sm rounded-md border border-slate-200 dark:border-slate-700
                                  bg-white dark:bg-slate-800 text-slate-700 dark:text-slate-200
                                  placeholder:text-slate-400 dark:placeholder:text-slate-500
                                  focus:outline-none focus:ring-2 focus:ring-primary-500"
                           placeholder="voice id" />
                </div>
            `;
            modelWrap.parentNode.insertBefore(voiceWrap, modelWrap.nextSibling);
        }
    }

    // `body` is still detached from `document`; scope lookups locally.
    const provDd = body.querySelector(`#cap-${def.id}-provider`);
    // Strip private fields before handing to the generic initDropdown helper.
    const ddOpts = providerOpts.map(o => ({ value: o.value, label: o.label }));

    let pendingProvider = null;
    if (pendingCapabilitySelection
            && pendingCapabilitySelection.capabilityId === def.id
            && providerOpts.some(o => o.value === pendingCapabilitySelection.providerId)) {
        pendingProvider = pendingCapabilitySelection.providerId;
        pendingCapabilitySelection = null;
    }

    // Auto strategy => leave empty sentinel selected. `suggested_provider`
    // is a UI-only preselect (not persisted until the user clicks Save).
    // No current + no suggestion => leave unselected with a placeholder.
    //
    // Pending-config takes priority over both "auto" and "pick provider":
    // when no real (non-sentinel) configured option exists, surfacing
    // "auto" or "pick" misleads the user — there's nothing to auto-route
    // to or pick from. Force a "待配置" placeholder instead so all
    // capabilities behave consistently on a fresh environment.
    const hasConfiguredOpt = providerOpts.some(o => !o._isAuto && o._configured);
    const noSelectionAndNoHint = !cap.current_provider && !cap.suggested_provider;
    let initialProviderValue;
    let dropdownPlaceholder = null;
    if (!hasConfiguredOpt) {
        initialProviderValue = '';
        dropdownPlaceholder = { placeholder: t('models_pending_config') };
    } else {
        initialProviderValue = pendingProvider
            ? pendingProvider
            : ((cap.strategy === 'auto' && capabilitySupportsAuto(def.id))
                ? ''
                : (cap.current_provider
                    || cap.suggested_provider
                    || (noSelectionAndNoHint ? '' : (ddOpts[0] && ddOpts[0].value))
                    || ''));
        if (noSelectionAndNoHint) {
            dropdownPlaceholder = { placeholder: t('models_pick_provider') };
        }
    }
    // Seed the "provider active before the last switch" tracker so the very
    // first vendor switch can still stash the initial provider's custom model.
    capabilityLastProviderId[def.id] = initialProviderValue;
    // If the initially selected model is a custom one, remember it against the
    // initial provider so a switch-away-and-back keeps it too.
    if (initialProviderValue && cap.current_model) {
        const provList = (cap.provider_models && cap.provider_models[initialProviderValue])
            || (initialProviderValue.startsWith('custom:') && cap.provider_models && cap.provider_models['custom'])
            || [];
        const presetValues = provList.map(e => (typeof e === 'string' ? e : e.value));
        if (!presetValues.includes(cap.current_model)) {
            capabilityCustomModelMemory[`${def.id}:${initialProviderValue}`] = cap.current_model;
        }
    }
    initDropdown(
        provDd,
        ddOpts,
        initialProviderValue,
        (value) => onCapabilityProviderChange(def, value, body),
        dropdownPlaceholder,
    );
    decorateCapabilityProviderDropdown(def, provDd, providerOpts);

    if (def.needsModel) {
        rebuildCapabilityModelDropdown(def, initialProviderValue, cap.current_model || '', body);
        // Embedding: hide model picker when no provider is selected.
        const showModel = def.id === 'embedding' ? initialProviderValue !== '' :
            (initialProviderValue !== '' || !capabilitySupportsAuto(def.id));
        setCapabilityModelPickerVisible(def, showModel, body);
    }

    if (def.id === 'tts') {
        rebuildCapabilityVoiceDropdown(
            initialProviderValue,
            cap.current_voice || '',
            body,
            cap.current_model || ''
        );
    }

    // Inject auto/router-pending hint banners before the action footer.
    renderCapabilityHints(def, cap, body, initialProviderValue);

    // Opt-in capabilities start collapsed when disabled, so an inactive
    // fallback reads as "off" rather than as a half-configured capability.
    if (def.toggleable) {
        _setCapabilityPickersVisible(def, body, !!cap.enabled);
    }

    if (def.id === 'tts') {
        _setTtsConfigVisible(body, (cap.reply_mode || 'off') !== 'off');
    }
}

// TTS reply-policy dropdown (off / voice_if_voice / always). Persists on
// change. When off, hides the rest of the TTS card.
function renderVoiceReplyMode(host, currentMode, options) {
    options = options || {};
    const opts = [
        { value: 'off',            label: t('voice_reply_off') },
        { value: 'voice_if_voice', label: t('voice_reply_if_voice') },
        { value: 'always',         label: t('voice_reply_always') },
    ];
    const wrap = document.createElement('div');
    wrap.id = 'voice-reply-mode-wrap';
    wrap.innerHTML = `
        <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${t('voice_reply_mode_label')}</label>
        <div id="voice-reply-mode-dd" class="cfg-dropdown" tabindex="0">
            <div class="cfg-dropdown-selected">
                <span class="cfg-dropdown-text">--</span>
                <i class="fas fa-chevron-down cfg-dropdown-arrow"></i>
            </div>
            <div class="cfg-dropdown-menu"></div>
        </div>
    `;
    host.prepend(wrap);

    const dd = wrap.querySelector('#voice-reply-mode-dd');
    const valid = ['off', 'voice_if_voice', 'always'];
    const initial = valid.includes(currentMode) ? currentMode : 'off';
    if (!options.skipVisibilityToggle) _setTtsConfigVisible(host, initial !== 'off');
    initDropdown(dd, opts, initial, (mode) => {
        if (!valid.includes(mode)) return;
        _setTtsConfigVisible(host, mode !== 'off');
        fetch('/api/models', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'set_voice_reply_mode', mode }),
        })
            .then(r => r.json())
            .then(data => {
                if (data && data.status === 'success') {
                    _ttsReadyPromise = null;  // force re-probe on next bubble
                }
            })
            .catch(() => {});
    });
}

// Show/hide everything in the TTS card below the reply-mode dropdown.
function _setTtsConfigVisible(host, visible) {
    if (!host) return;
    Array.from(host.children).forEach((child) => {
        if (child.id === 'voice-reply-mode-wrap') return;
        child.classList.toggle('hidden', !visible);
    });
}

// Toggle wrapper visibility instead of re-rendering so dropdown state survives.
function setCapabilityModelPickerVisible(def, visible, scope) {
    const root = scope || document;
    const wrap = root.querySelector(`#cap-${def.id}-model-wrap`);
    if (!wrap) return;
    wrap.classList.toggle('hidden', !visible);
}

function renderCapabilityHints(def, cap, body, currentProvider) {
    // Capabilities that can be in "auto" mode show a fallback hint right
    // under the inputs so users always know what'd actually be hit. The
    // image card additionally surfaces a "router pending" warning until the
    // standalone dispatcher lands.
    // The hint slot is co-located with the save button in the footer row
    // (see renderCapabilityBody) so the save button stays close to the
    // inputs above. We just rewrite the slot's innerHTML — emptying it
    // when the card leaves auto mode, or rendering a one-line hint when
    // it's in auto mode.
    const slot = body.querySelector(`[data-cap-hint="${def.id}"]`);
    if (!slot) return;
    slot.innerHTML = '';

    if (currentProvider !== '' || !capabilitySupportsAuto(def.id)) return;

    // The hint mirrors what the runtime would actually pick when in auto
    // mode. fallback_provider/model are pre-computed on the backend (see
    // _predict_vision_auto, _predict_image_auto) so we can trust them
    // here without re-implementing the provider chain.
    const fbProv = cap.fallback_provider || '';
    const fbModel = cap.fallback_model || '';
    if (!fbProv && !fbModel) return;
    // Show the vendor's display label (e.g. "LinkAI") instead of the raw
    // id ("linkai") when we know it. Falls back to the id when the
    // provider isn't in our vendor table (rare).
    const provMeta = modelsState.providers.find(p => p.id === fbProv);
    const fbProvLabel = (provMeta && localizedLabel(provMeta.label)) || fbProv;
    const fbText = fbModel ? `${fbProvLabel} / ${fbModel}` : fbProvLabel;
    slot.innerHTML = `
        <p class="flex items-center gap-1.5 text-xs text-slate-400 dark:text-slate-500 min-w-0">
            <i class="fas fa-circle-info text-[10px] flex-shrink-0"></i>
            <span class="flex-shrink-0">${t('models_auto_using')}</span>
            <span class="font-mono text-slate-500 dark:text-slate-400 truncate">${escapeHtml(fbText)}</span>
        </p>`;
}

function buildCapabilityProviderOptions(def, cap) {
    // Show ALL vendors in capability dropdowns so users can see at a glance
    // who's configured (green check) and who isn't (gray dot, click to set
    // up). The list order puts configured vendors first; clicking an
    // unconfigured row opens the vendor modal in-place. ASR/TTS engines that
    // aren't tracked by PROVIDER_MODELS (azure/baidu/google etc.) are treated
    // as "always available" — no credential gate.
    const knownProviderMap = {};
    modelsState.providers.forEach(p => { knownProviderMap[p.id] = p; });

    const explicitList = cap.providers && cap.providers.length ? cap.providers : null;
    let providerIds = explicitList ? explicitList.slice() : modelsState.providers.map(p => p.id);
    if (cap.current_provider && !providerIds.includes(cap.current_provider)) {
        providerIds = [cap.current_provider, ...providerIds];
    }

    const opts = providerIds.map(pid => {
        const meta = knownProviderMap[pid];
        const tracked = !!meta;
        const configured = !tracked || !!meta.configured;
        return {
            value: pid,
            label: (meta && localizedLabel(meta.label)) || pid,
            _tracked: tracked,
            _configured: configured,
        };
    });

    opts.sort((a, b) => {
        if (a._configured === b._configured) return 0;
        return a._configured ? -1 : 1;
    });

    // Capabilities with a fallback ("auto") strategy expose it as a sentinel
    // option pinned to the top of the list. We use empty-string as the auto
    // value so the existing save handler propagates it untouched to the
    // backend, which interprets "" as "fall back to the main model".
    // Skip the sentinel when no real vendor is configured — "auto" would
    // route to nothing useful and the renderer will show "待配置" instead.
    const hasAnyConfigured = opts.some(o => o._configured);
    if ((cap.strategy === 'auto' || cap.strategy === 'specified') && hasAnyConfigured) {
        if (capabilitySupportsAuto(def.id)) {
            opts.unshift({
                value: '',
                label: t('models_strategy_auto'),
                _tracked: false,
                _configured: true,
                _isAuto: true,
            });
        }
    }
    return opts;
}

function capabilitySupportsAuto(capId) {
    // Embedding is intentionally NOT here: runtime only auto-falls back to
    // OpenAI/LinkAI, so dressing it up as "auto" hides reality from users.
    return capId === 'image' || capId === 'vision';
}

// After initDropdown renders the capability provider menu, decorate each
// row with the right-aligned configuration cue:
//   - configured rows: nothing extra — the .active marker (a brand-green ✓)
//     already comes from initDropdown's selected-state CSS for the row the
//     user currently picked. Other configured rows show no chrome, mirroring
//     a plain "switch to this" selector.
//   - unconfigured rows: a subdued gear icon hints at "click to configure".
//     The row's whole click handler is swapped to launch the vendor modal
//     in place rather than selecting an unusable value.
function decorateCapabilityProviderDropdown(def, ddEl, opts) {
    if (!ddEl) return;
    const menu = ddEl.querySelector('.cfg-dropdown-menu');
    if (!menu) return;

    const optByValue = {};
    opts.forEach(o => { optByValue[o.value] = o; });

    menu.querySelectorAll('.cfg-dropdown-item').forEach(item => {
        const value = item.dataset.value;
        const opt = optByValue[value];
        if (!opt) return;
        item.classList.add('cap-provider-item');
        if (!opt._configured) item.classList.add('cap-provider-unconfigured');

        // Wrap the label so the trailing affordance lines up via flex:auto.
        const labelText = item.textContent;
        item.textContent = '';
        const labelEl = document.createElement('span');
        labelEl.className = 'cap-provider-label';
        labelEl.textContent = labelText;
        item.appendChild(labelEl);

        if (!opt._configured) {
            // Trailing gear icon as the "configure this vendor" affordance.
            const gear = document.createElement('i');
            gear.className = 'fas fa-gear cap-provider-gear';
            item.appendChild(gear);
        }

        if (!opt._configured && opt._tracked) {
            // Hijack the click: open the vendor modal instead of selecting
            // an unusable value, and remember which capability the user was
            // configuring so the post-save reload can preselect the vendor.
            const newItem = item.cloneNode(true);
            item.replaceWith(newItem);
            newItem.addEventListener('click', (e) => {
                e.stopPropagation();
                ddEl.classList.remove('open');
                openVendorModal(value, (savedProviderId) => {
                    pendingCapabilitySelection = {
                        capabilityId: def.id,
                        providerId: savedProviderId || value,
                    };
                    loadModelsView({ preserveScroll: true });
                });
            });
        }
    });
}

// Lightweight decorator for the "add vendor" modal's provider picker:
// every configured vendor row gets a trailing brand-green ✓ so the user can
// see at a glance who's already set up, without having to read each row.
// Unlike decorateCapabilityProviderDropdown we don't hijack clicks here —
// picking an unconfigured vendor in this modal *is* the intended action.
function decorateVendorModalPicker(ddEl, opts) {
    if (!ddEl) return;
    const menu = ddEl.querySelector('.cfg-dropdown-menu');
    if (!menu) return;

    const optByValue = {};
    opts.forEach(o => { optByValue[o.value] = o; });

    menu.querySelectorAll('.cfg-dropdown-item').forEach(item => {
        const opt = optByValue[item.dataset.value];
        if (!opt) return;
        // Tag the row so the global active-row ✓ rule is suppressed in CSS
        // (otherwise configured AND selected rows would render two checks).
        item.classList.add('vendor-picker-item');
        if (opt._isAddNew) {
            // "Custom" is an add-new action (multiple entries allowed),
            // so show a trailing + instead of the configured ✓.
            const plus = document.createElement('i');
            plus.className = 'fas fa-plus vendor-picker-add-mark';
            item.appendChild(plus);
            return;
        }
        if (!opt._configured) return;
        const check = document.createElement('i');
        check.className = 'fas fa-check vendor-picker-configured-mark';
        item.appendChild(check);
    });
}

function rebuildCapabilityModelDropdown(def, providerId, selectedModel, scope) {
    // `scope` lets the caller (renderCapabilityBody) target a still-detached
    // subtree. After the card is mounted, callers may pass `document` instead.
    const root = scope || document;
    const el = root.querySelector(`#cap-${def.id}-model`);
    if (!el) return;

    // Prefer the capability-scoped model list when the backend provides one
    // (vision / image). It reflects the models the runtime can actually
    // dispatch to for this capability, instead of the vendor's full chat-
    // model catalog. Fall back to the generic provider.models for chat /
    // embedding / tts where any vendor model is fair game.
    //
    // Entries may be plain strings or {value, hint} objects (image catalog
    // uses the latter to surface brand aliases like "Nano Banana 2" next to
    // the technical Gemini model id). We normalize to {value, label, hint}
    // before handing off to initDropdown.
    const cap = modelsState.capabilities[def.id] || {};
    const capModelMap = cap.provider_models || {};
    let rawList;
    if (capModelMap[providerId]) {
        rawList = capModelMap[providerId].slice();
    } else if (providerId.startsWith('custom:') && capModelMap['custom']) {
        // Expanded custom:<id> entries share the same preset model list
        rawList = capModelMap['custom'].slice();
    } else {
        const provider = modelsState.providers.find(p => p.id === providerId);
        rawList = (provider && provider.models) ? provider.models.slice() : [];
    }
    const modelValues = [];
    const opts = rawList.map(entry => {
        if (typeof entry === 'string') {
            modelValues.push(entry);
            return { value: entry, label: entry };
        }
        modelValues.push(entry.value);
        return { value: entry.value, label: entry.label || entry.value, hint: entry.hint || '' };
    });
    opts.push({ value: '__custom__', label: currentLang === 'zh' ? '自定义' : 'Custom' });

    let initialValue = selectedModel || '';
    if (initialValue && !modelValues.includes(initialValue)) {
        initialValue = '__custom__';
    }
    if (!initialValue && opts.length) initialValue = opts[0].value;

    initDropdown(el, opts, initialValue, (value) => {
        const customWrap = document.getElementById(`cap-${def.id}-model-custom-wrap`);
        if (customWrap) {
            if (value === '__custom__') {
                customWrap.classList.remove('hidden');
                const input = document.getElementById(`cap-${def.id}-model-custom`);
                if (input && !input.value) input.value = selectedModel || '';
            } else {
                customWrap.classList.add('hidden');
            }
        }
        // TTS voice catalog may be scoped per engine model (aggregating
        // gateways). Rebuild the voice picker whenever the model changes.
        if (def.id === 'tts') {
            const provDd = document.getElementById('cap-tts-provider');
            const provId = provDd ? getDropdownValue(provDd) : '';
            rebuildCapabilityVoiceDropdown(provId, '', null, value);
        }
    });

    const customWrap = root.querySelector(`#cap-${def.id}-model-custom-wrap`);
    if (customWrap) {
        if (initialValue === '__custom__') {
            customWrap.classList.remove('hidden');
            const input = root.querySelector(`#cap-${def.id}-model-custom`);
            if (input) input.value = selectedModel || '';
        } else {
            customWrap.classList.add('hidden');
        }
    }
}

// TTS-only: rebuild the voice timbre picker against the provider's
// curated voice list. Hidden when no provider is picked.
//
// Each voice entry may be:
//   - a bare string  (code = label)
//   - {value, label, hint?}   so we can show a friendly Chinese name
//     while persisting the raw API code that the runtime sends.
function rebuildCapabilityVoiceDropdown(providerId, selectedVoice, scope, modelId) {
    const root = scope || document;
    const wrap = root.querySelector(`#cap-tts-voice-wrap`);
    const el = root.querySelector(`#cap-tts-voice`);
    if (!wrap || !el) return;
    const cap = modelsState.capabilities.tts || {};
    const voicesByProvider = cap.provider_voices || {};
    let raw = (providerId && voicesByProvider[providerId]) || [];
    // Some providers (gateways) scope voices by engine model id.
    if (raw && !Array.isArray(raw) && typeof raw === 'object') {
        const activeModel = modelId
            || (root.querySelector(`#cap-tts-model`) ? getDropdownValue(root.querySelector(`#cap-tts-model`)) : '');
        raw = (activeModel && raw[activeModel]) || [];
    }
    if (!raw || raw.length === 0) {
        wrap.classList.add('hidden');
        return;
    }
    wrap.classList.remove('hidden');
    // Voice picker: friendly name on the left, raw API code as right-hand
    // hint. Persisted/sent value is always the raw code.
    const codes = [];
    const opts = raw.map(entry => {
        if (typeof entry === 'string') {
            codes.push(entry);
            return { value: entry, label: entry };
        }
        codes.push(entry.value);
        const code = entry.value;
        const desc = entry.hint || entry.label || code;
        return {
            value: code,
            label: desc,
            hint: desc === code ? '' : code,
        };
    });
    opts.push({ value: '__custom__', label: currentLang === 'zh' ? '自定义' : 'Custom' });

    // Off-catalog values route through the custom branch.
    let initial = selectedVoice || '';
    const isCustom = initial && !codes.includes(initial);
    if (isCustom) initial = '__custom__';
    if (!initial) initial = codes[0];

    initDropdown(el, opts, initial, (value) => {
        const customWrap = root.querySelector(`#cap-tts-voice-custom-wrap`);
        if (!customWrap) return;
        if (value === '__custom__') {
            customWrap.classList.remove('hidden');
            const input = root.querySelector(`#cap-tts-voice-custom`);
            if (input && !input.value) input.value = isCustom ? selectedVoice : '';
        } else {
            customWrap.classList.add('hidden');
        }
    });

    const customWrap = root.querySelector(`#cap-tts-voice-custom-wrap`);
    if (customWrap) {
        if (initial === '__custom__') {
            customWrap.classList.remove('hidden');
            const input = root.querySelector(`#cap-tts-voice-custom`);
            if (input) input.value = isCustom ? selectedVoice : '';
        } else {
            customWrap.classList.add('hidden');
        }
    }
}

function onCapabilityProviderChange(def, providerId, scope) {
    if (def.needsModel) {
        // Before rebuilding the model picker for the newly picked provider,
        // stash the custom model the user had typed under the *previous*
        // provider, so switching back to it later restores that value.
        const prevProvider = capabilityLastProviderId[def.id];
        if (prevProvider && prevProvider !== providerId) {
            const prevDd = document.getElementById(`cap-${def.id}-model`);
            const prevInput = document.getElementById(`cap-${def.id}-model-custom`);
            if (prevDd && prevInput && getDropdownValue(prevDd) === '__custom__') {
                const typed = prevInput.value.trim();
                if (typed) capabilityCustomModelMemory[`${def.id}:${prevProvider}`] = typed;
            }
        }
        capabilityLastProviderId[def.id] = providerId;

        // Embedding: hide model picker when no provider is selected.
        const showModel = def.id === 'embedding' ? providerId !== '' :
            !(providerId === '' && capabilitySupportsAuto(def.id));
        if (showModel) {
            // Restore a remembered custom model for this provider (if any) so
            // switching vendors and back does not drop it.
            const remembered = capabilityCustomModelMemory[`${def.id}:${providerId}`] || '';
            rebuildCapabilityModelDropdown(def, providerId, remembered, scope);
        }
        setCapabilityModelPickerVisible(def, showModel, scope);
    }
    if (def.id === 'tts') {
        rebuildCapabilityVoiceDropdown(providerId, '', scope);
    }
    const body = scope || document.querySelector(`[data-cap-body="${def.id}"]`);
    if (body) {
        const cap = modelsState.capabilities[def.id] || {};
        renderCapabilityHints(def, cap, body, providerId);
    }
}

function getCapabilityModelValue(def) {
    if (!def.needsModel) return '';
    const dd = document.getElementById(`cap-${def.id}-model`);
    if (!dd) return '';
    const v = getDropdownValue(dd);
    if (v === '__custom__') {
        const input = document.getElementById(`cap-${def.id}-model-custom`);
        return input ? input.value.trim() : '';
    }
    return v || '';
}

// Opt-in capabilities: show/hide the pickers under the toggle without
// touching config. Mirrors the TTS reply-mode pattern — the toggle itself is
// pure UI state until the user presses Save.
function _setCapabilityPickersVisible(def, body, visible) {
    const wrap = body.querySelector(`#cap-${def.id}-pickers`);
    if (wrap) wrap.classList.toggle('hidden', !visible);
}

// Clicking the toggle flips the local switch. Persisting is a separate act
// (Save), so a user can flip back without ever writing to config.
function toggleCapabilityEnabled(capId) {
    const def = capabilityDefById(capId);
    if (!def || !def.toggleable) return;
    const cap = modelsState.capabilities[capId] || {};
    cap.enabled = !cap.enabled;
    modelsState.capabilities[capId] = cap;
    const btn = document.getElementById(`cap-${capId}-toggle`);
    if (btn) {
        btn.setAttribute('aria-checked', cap.enabled ? 'true' : 'false');
        btn.classList.toggle('bg-primary-500', cap.enabled);
        btn.classList.toggle('bg-slate-200', !cap.enabled);
        btn.classList.toggle('dark:bg-slate-700', !cap.enabled);
        const knob = btn.querySelector('span');
        if (knob) {
            knob.classList.toggle('translate-x-[18px]', cap.enabled);
            knob.classList.toggle('translate-x-[3px]', !cap.enabled);
        }
    }
    // Same lookup the rest of the file uses for a capability body.
    const body = document.querySelector(`[data-cap-body="${capId}"]`);
    if (body) _setCapabilityPickersVisible(def, body, cap.enabled);
}

function saveCapability(capId) {
    const def = capabilityDefById(capId);
    if (!def || !def.editable) return;
    // Search has its own form (strategy + provider, no model picker).
    if (capId === 'search') { saveSearchCapability(); return; }
    const provDd = document.getElementById(`cap-${capId}-provider`);
    const provider = provDd ? getDropdownValue(provDd) : '';
    // When the user is in auto mode (provider == ""), the model picker is
    // hidden and any value left in it is stale; persist an empty model so
    // the backend treats this as "fall back to the runtime chain".
    const isAuto = provider === '' && capabilitySupportsAuto(capId);
    // Embedding without a provider similarly means "cleared" — don't leak
    // a stale model value into config.
    const model = (isAuto || (capId === 'embedding' && !provider)) ? '' : getCapabilityModelValue(def);
    // TTS carries an extra voice timbre (supports free-text custom ids).
    let voice = '';
    if (capId === 'tts' && !isAuto) {
        const voiceDd = document.getElementById(`cap-${capId}-voice`);
        voice = voiceDd ? getDropdownValue(voiceDd) : '';
        if (voice === '__custom__') {
            const input = document.getElementById(`cap-${capId}-voice-custom`);
            voice = input ? input.value.trim() : '';
        }
    }

    // Embedding changes invalidate any pre-existing vector index because
    // dimensions / vendor differ. Gate the save behind a confirm, and on
    // success surface a dedicated info dialog telling the user how to
    // rebuild — both via the in-app custom dialog, not the native alert.
    if (capId === 'embedding') {
        const cap = modelsState.capabilities[capId] || {};
        const before = (cap.current_provider || '').trim();
        const after = (provider || '').trim();
        if (before !== after) {
            showConfirmDialog({
                title: t('models_embedding_change_title'),
                message: t('models_embedding_change_msg'),
                okText: t('save'),
                cancelText: t('cancel'),
                onConfirm: () => _persistCapability(capId, provider, model, () => {
                    showConfirmDialog({
                        title: t('models_embedding_saved_title'),
                        message: t('models_embedding_saved_msg'),
                        okText: t('models_embedding_saved_ok'),
                        hideCancel: true,
                        onConfirm: () => {
                            navigateTo('chat');
                            // Defer focus + value set: navigateTo may
                            // re-render the chat panel; setting value before
                            // the input is mounted would be lost.
                            setTimeout(() => {
                                const input = document.getElementById('chat-input');
                                if (!input) return;
                                input.value = '/memory rebuild-index';
                                input.focus();
                                // Trigger any input listeners (autosize, send-button enable, etc.)
                                input.dispatchEvent(new Event('input', { bubbles: true }));
                            }, 60);
                        },
                    });
                }),
            });
            return;
        }
    }
    // Opt-in capabilities persist their on/off switch alongside the pickers.
    // It is sent even when turning off, so a broken entry can always be
    // cleared — and the backend refuses to enable a half-filled one.
    let enabled = undefined;
    if (def.toggleable) {
        const cap = modelsState.capabilities[capId] || {};
        enabled = !!cap.enabled;
    }
    // The chat fallback is edited inside a modal; close it once the save
    // lands so the user drops straight back to the models page (already
    // reloaded by _persistCapability, which refreshes the main-card badge).
    const onAfterSuccess = capId === 'chat_fallback' ? closeChatFallbackModal : undefined;
    _persistCapability(capId, provider, model, onAfterSuccess, { voice, enabled });
}

function _persistCapability(capId, provider, model, onAfterSuccess, extras) {
    const payload = { action: 'set_capability', capability: capId, provider_id: provider, model: model };
    if (extras && extras.voice !== undefined) payload.voice = extras.voice;
    // Opt-in capabilities (the chat fallback) carry their on/off switch.
    if (extras && extras.enabled !== undefined) payload.enabled = extras.enabled;
    fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    }).then(r => r.json()).then(data => {
        if (data.status === 'success') {
            // Flash "Saved" before reload so the status survives the rebuild.
            showStatus(`cap-${capId}-status`, 'models_save_success', false);
            setTimeout(() => {
                loadModelsView({ preserveScroll: true });
                if (onAfterSuccess) onAfterSuccess();
            }, 400);
        } else {
            showStatus(`cap-${capId}-status`, 'models_save_failed', true);
        }
    }).catch(() => showStatus(`cap-${capId}-status`, 'models_save_failed', true));
}

// ---------- Vendor credential modal ------------------------------------

let vendorModalState = { providerId: '', onSaved: null };

function openVendorModal(providerId, onSaved) {
    vendorModalState = { providerId: providerId || '', onSaved: onSaved || null };

    const overlay = document.getElementById('vendor-modal-overlay');
    const titleEl = document.getElementById('vendor-modal-title');
    const subEl = document.getElementById('vendor-modal-subtitle');
    const pickerWrap = document.getElementById('vendor-modal-picker-wrap');
    const baseWrap = document.getElementById('vendor-modal-base-wrap');
    const baseInput = document.getElementById('vendor-modal-base');
    const baseHint = document.getElementById('vendor-modal-base-hint');
    const keyInput = document.getElementById('vendor-modal-key');
    const clearBtn = document.getElementById('vendor-modal-clear');

    // Reset any leftover status (e.g. previous "Saved" message)
    const statusEl = document.getElementById('vendor-modal-status');
    if (statusEl) {
        statusEl.textContent = '';
        statusEl.classList.add('opacity-0');
    }

    if (!providerId) {
        // Add flow — show provider picker, default to the first unconfigured one.
        // We render every configured vendor with a trailing green ✓ via the
        // dropdown decorator, mirroring the visual language used by the
        // capability provider dropdowns. The .active row already shows the
        // currently selected vendor via its own background highlight, so we
        // intentionally suppress the global active-row ✓ for this picker
        // (see CSS) — otherwise configured + selected rows would show two.
        // Expanded custom provider cards ("custom:<id>") are edited via their
        // dedicated modal, so they are excluded from this picker. Picking the
        // "custom" entry creates a *new* custom provider via that modal —
        // this is how multiple OpenAI-compatible endpoints are added.
        const builtinProviders = modelsState.providers.filter(p => !isCustomProviderCard(p));
        const pickerOpts = builtinProviders.map(p => ({
            value: p.id,
            label: localizedLabel(p.label),
            _configured: !!p.configured,
        }));
        // In multi-provider mode the backend replaces the bare "custom" card
        // with the expanded ones; re-add it here so the entry stays available.
        if (!pickerOpts.some(o => o.value === 'custom')) {
            pickerOpts.push({ value: 'custom', label: t('models_custom_vendor_label'), _configured: false });
        }
        // "Custom" always behaves as an add-new action (multiple entries
        // allowed), so it shows a + mark instead of the configured ✓.
        pickerOpts.forEach(o => { if (o.value === 'custom') { o._isAddNew = true; o._configured = false; } });
        const unconfigured = builtinProviders.filter(p => !p.configured);
        const defaultId = (unconfigured[0] && unconfigured[0].id) || (builtinProviders[0] && builtinProviders[0].id) || 'custom';
        pickerWrap.classList.remove('hidden');
        const pickerEl = document.getElementById('vendor-modal-picker');
        const onPick = (val) => {
            if (val === 'custom') {
                // "Custom" in the add flow always creates a new
                // OpenAI-compatible provider entry via the dedicated modal
                // (name + base + key), supporting multiple custom endpoints.
                closeVendorModal();
                openCustomProviderModal('');
                return;
            }
            fillVendorModalForProvider(val);
        };
        initDropdown(pickerEl, pickerOpts, defaultId, onPick);
        decorateVendorModalPicker(pickerEl, pickerOpts);
        onPick(defaultId);
    } else {
        pickerWrap.classList.add('hidden');
        fillVendorModalForProvider(providerId);
    }

    overlay.classList.remove('hidden');

    document.getElementById('vendor-modal-cancel').onclick = closeVendorModal;
    document.getElementById('vendor-modal-save').onclick = saveVendorModal;
    clearBtn.onclick = clearVendorModal;

    // Once the user edits the masked value, drop the "masked sentinel" dataset
    // so the save handler treats their input as a real new key. We compare on
    // the next tick because keydown fires before the new char lands in .value.
    keyInput.oninput = function () {
        if (keyInput.dataset.masked === '1' && keyInput.value !== keyInput.dataset.maskedVal) {
            keyInput.dataset.masked = '';
        }
    };

    function onOverlayClick(e) {
        if (e.target === overlay) {
            closeVendorModal();
            overlay.removeEventListener('click', onOverlayClick);
        }
    }
    overlay.addEventListener('click', onOverlayClick);
    keyInput.focus();
}

function fillVendorModalForProvider(providerId) {
    const meta = modelsState.providers.find(p => p.id === providerId);
    if (!meta) return;
    document.getElementById('vendor-modal-title').textContent = localizedLabel(meta.label);
    document.getElementById('vendor-modal-subtitle').textContent = meta.id;

    // LinkAI aggregates many vendors, so only for it do we surface a link to its
    // console for creating/managing the aggregated key. Other providers manage
    // their keys on their own sites.
    const manageKey = document.getElementById('vendor-modal-manage-key');
    if (manageKey) manageKey.classList.toggle('hidden', meta.id !== 'linkai');

    // ----- API Base -----
    // Always reflect the *current effective* base as the input value so the
    // user can see (and edit) what's in use today. Placeholder is reserved
    // strictly for the "not yet typed anything" state and shows the official
    // default — never mixed with the actual value.
    const baseWrap = document.getElementById('vendor-modal-base-wrap');
    const baseInput = document.getElementById('vendor-modal-base');
    const baseHint = document.getElementById('vendor-modal-base-hint');
    if (meta.api_base_field) {
        baseWrap.classList.remove('hidden');
        baseInput.placeholder = meta.api_base_default || meta.api_base_placeholder || '';
        baseInput.value = meta.api_base || '';
        baseHint.classList.add('hidden');
    } else {
        baseWrap.classList.add('hidden');
        baseInput.value = '';
    }

    // ----- API Key -----
    // For configured vendors, surface the masked key as the input *value* so
    // it shows up in the same dark text as a real entry — making "configured"
    // visually unambiguous. The masked form (e.g. "sk-r***zRU") is also a
    // sentinel: the save handler treats untouched masked input as "no change".
    const keyInput = document.getElementById('vendor-modal-key');
    if (meta.configured && meta.api_key_masked) {
        keyInput.value = meta.api_key_masked;
        keyInput.dataset.masked = '1';
        keyInput.dataset.maskedVal = meta.api_key_masked;
        keyInput.placeholder = '';
    } else {
        keyInput.value = '';
        keyInput.dataset.masked = '';
        keyInput.dataset.maskedVal = '';
        keyInput.placeholder = 'sk-...';
    }

    const clearBtn = document.getElementById('vendor-modal-clear');
    clearBtn.classList.toggle('hidden', !meta.configured);

    vendorModalState.providerId = providerId;
}

function closeVendorModal() {
    document.getElementById('vendor-modal-overlay').classList.add('hidden');
}

function saveVendorModal() {
    const providerId = vendorModalState.providerId;
    if (!providerId) return;
    const keyInput = document.getElementById('vendor-modal-key');
    const apiBase = document.getElementById('vendor-modal-base').value.trim();

    // Treat "input still equals the masked value we surfaced on open" as "no
    // change" — the backend uses missing/empty api_key to skip the field.
    let apiKey = keyInput.value.trim();
    const masked = keyInput.dataset.masked === '1';
    const maskedVal = keyInput.dataset.maskedVal || '';
    if (masked && apiKey === maskedVal) {
        apiKey = '';
    }

    if (!apiKey && !masked) {
        // First-time setup with no key entered → nudge the user.
        keyInput.focus();
        return;
    }

    const btn = document.getElementById('vendor-modal-save');
    btn.disabled = true;
    const payload = { action: 'set_provider', provider_id: providerId, api_base: apiBase };
    if (apiKey) payload.api_key = apiKey;
    fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    }).then(r => r.json()).then(data => {
        btn.disabled = false;
        if (data.status === 'success') {
            closeVendorModal();
            const onSaved = vendorModalState.onSaved;
            if (onSaved) {
                try { onSaved(providerId); } catch (e) { /* noop */ }
            } else {
                loadModelsView();
            }
        } else {
            showStatus('vendor-modal-status', 'models_save_failed', true);
        }
    }).catch(() => {
        btn.disabled = false;
        showStatus('vendor-modal-status', 'models_save_failed', true);
    });
}

function clearVendorModal() {
    const providerId = vendorModalState.providerId;
    if (!providerId) return;
    showConfirmDialog({
        title: t('models_clear_confirm_title'),
        message: t('models_clear_confirm_msg'),
        okText: t('models_clear_credential'),
        cancelText: t('cancel'),
        onConfirm: () => {
            fetch('/api/models', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'delete_provider', provider_id: providerId }),
            }).then(r => r.json()).then(data => {
                if (data.status === 'success') {
                    closeVendorModal();
                    loadModelsView();
                } else {
                    showStatus('vendor-modal-status', 'models_clear_failed', true);
                }
            }).catch(() => showStatus('vendor-modal-status', 'models_clear_failed', true));
        }
    });
}

// =====================================================================
// Custom (OpenAI-compatible) provider modal — add / edit
// =====================================================================
// State for the dedicated custom-provider modal. `editId` is empty when
// adding and set to the provider id when editing.
let customProviderModalState = { editId: '' };

function openCustomProviderModal(providerId) {
    const editing = !!providerId;
    customProviderModalState = { editId: editing ? providerId : '' };

    const card = editing ? getCustomProviderCards().find(p => p.custom_id === providerId) : null;

    const overlay = document.getElementById('custom-provider-modal-overlay');
    if (!overlay) return;

    document.getElementById('custom-provider-modal-title').textContent =
        editing ? t('models_custom_edit_title') : t('models_custom_add_title');

    const nameInput = document.getElementById('custom-provider-name');
    const baseInput = document.getElementById('custom-provider-base');
    const keyInput = document.getElementById('custom-provider-key');

    nameInput.value = card ? (card.custom_name || '') : '';
    baseInput.value = card ? (card.api_base || '') : '';

    // Surface the masked key as the value for configured providers so the
    // "already set" state is unambiguous; an untouched masked value means
    // "keep the existing key" on save (mirrors the vendor modal contract).
    if (card && card.configured && card.api_key_masked) {
        keyInput.value = card.api_key_masked;
        keyInput.dataset.masked = '1';
        keyInput.dataset.maskedVal = card.api_key_masked;
    } else {
        keyInput.value = '';
        keyInput.dataset.masked = '';
        keyInput.dataset.maskedVal = '';
    }
    keyInput.oninput = function () {
        if (keyInput.dataset.masked === '1' && keyInput.value !== keyInput.dataset.maskedVal) {
            keyInput.dataset.masked = '';
        }
    };

    const statusEl = document.getElementById('custom-provider-modal-status');
    if (statusEl) { statusEl.textContent = ''; statusEl.classList.add('opacity-0'); }

    overlay.classList.remove('hidden');
    document.getElementById('custom-provider-modal-cancel').onclick = closeCustomProviderModal;
    document.getElementById('custom-provider-modal-save').onclick = saveCustomProviderModal;

    // Delete is only available when editing an existing provider.
    const deleteBtn = document.getElementById('custom-provider-modal-delete');
    if (deleteBtn) {
        deleteBtn.classList.toggle('hidden', !editing);
        deleteBtn.onclick = editing ? () => deleteCustomProvider(providerId) : null;
    }

    function onOverlayClick(e) {
        if (e.target === overlay) {
            closeCustomProviderModal();
            overlay.removeEventListener('click', onOverlayClick);
        }
    }
    overlay.addEventListener('click', onOverlayClick);
    nameInput.focus();
}

function closeCustomProviderModal() {
    const overlay = document.getElementById('custom-provider-modal-overlay');
    if (overlay) overlay.classList.add('hidden');
}

function saveCustomProviderModal() {
    const name = document.getElementById('custom-provider-name').value.trim();
    const apiBase = document.getElementById('custom-provider-base').value.trim();
    const keyInput = document.getElementById('custom-provider-key');

    if (!name) {
        showStatus('custom-provider-modal-status', 'models_custom_name_required', true);
        document.getElementById('custom-provider-name').focus();
        return;
    }
    const editing = !!customProviderModalState.editId;
    if (!editing && !apiBase) {
        showStatus('custom-provider-modal-status', 'models_custom_base_required', true);
        document.getElementById('custom-provider-base').focus();
        return;
    }

    // Key handling (the custom provider's key is optional):
    //  - masked + untouched  => keep existing, omit from payload
    //  - non-empty typed value => set it
    //  - explicitly cleared on edit => send "" so the backend clears it
    const untouchedMasked =
        keyInput.dataset.masked === '1' && keyInput.value.trim() === (keyInput.dataset.maskedVal || '');
    const apiKey = untouchedMasked ? '' : keyInput.value.trim();

    const payload = {
        action: 'set_custom_provider',
        name: name,
        api_base: apiBase,
    };
    if (untouchedMasked) {
        // omit api_key entirely => backend keeps the stored key
    } else {
        // Send the value (possibly "") so an explicit clear is honored.
        payload.api_key = apiKey;
    }
    if (editing) payload.id = customProviderModalState.editId;

    const btn = document.getElementById('custom-provider-modal-save');
    btn.disabled = true;
    fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    }).then(r => r.json()).then(data => {
        btn.disabled = false;
        if (data.status === 'success') {
            closeCustomProviderModal();
            loadModelsView();
        } else {
            showStatus('custom-provider-modal-status', 'models_save_failed', true);
        }
    }).catch(() => {
        btn.disabled = false;
        showStatus('custom-provider-modal-status', 'models_save_failed', true);
    });
}

function deleteCustomProvider(providerId) {
    showConfirmDialog({
        title: t('models_custom_delete_confirm_title'),
        message: t('models_custom_delete_confirm_msg'),
        okText: t('models_custom_delete'),
        cancelText: t('cancel'),
        onConfirm: () => {
            fetch('/api/models', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'delete_custom_provider', id: providerId }),
            }).then(r => r.json()).then(data => {
                if (data.status === 'success') {
                    closeCustomProviderModal();
                    loadModelsView();
                }
            }).catch(() => { /* noop */ });
        }
    });
}

// =====================================================================
// Channels View
// =====================================================================
let channelsData = [];
// Multi-Agent mode: the multi-instance-ready types (feishu) render one card per
// channel_instances record. These mirror the extra fields the API returns.
let channelInstancesView = [];
let multiInstanceTypes = [];
let channelsMultiAgent = false;

function isMultiInstanceType(name) {
    return channelsMultiAgent && multiInstanceTypes.indexOf(name) !== -1;
}

// --- Scope resolution and failure presentation ---------------------------
// The 消息渠道 page is ONE console page serving two scopes (server reports the
// relative scope in the projection). These helpers are pure so the contract is
// testable without a DOM.
function channelPageScope() {
    const ctx = (typeof _baseAuthContext === 'function') ? _baseAuthContext() : null;
    const pages = ctx && ctx.console_pages && typeof ctx.console_pages === 'object'
        ? ctx.console_pages : null;
    const page = pages && pages['admin.channels'];
    // ``self`` is the member's range on the *same* business surface (task 6.1):
    // the server answered them with their own connections, so they load the
    // tenant page rather than the platform instance page.
    if (page && page.scope === 'self') return 'self';
    if (page && page.scope === 'tenant') return 'tenant';
    if (page && page.scope === 'platform') return 'platform';
    // Unknown projection (legacy install / not yet loaded) keeps the historic
    // instance-level page rather than guessing the tenant scope.
    return 'platform';
}

function channelScope() {
    const scope = channelPageScope();
    return scope === 'self' ? 'tenant' : scope;
}

// The page header is static markup in chat.html and is shared by both scopes,
// so the tenant render must not paint a second copy. Only the description
// differs, and it moves with the scope (keeping data-i18n in step so a language
// switch re-translates it rather than reverting to the platform copy).
function syncChannelsHeader(scope) {
    const subtitle = document.getElementById('channels-subtitle');
    if (!subtitle) return;
    // Three scopes, three descriptions, one page: the platform instance list,
    // the tenant's public connections, and the member's own — the last one is
    // the same business surface as the tenant's but must say whose connections
    // these are (task 6.1).
    const key = scope === 'self' ? 'tenant_channel_self_desc'
        : (scope === 'tenant' ? 'tenant_channel_desc' : 'channels_desc');
    subtitle.dataset.i18n = key;
    subtitle.textContent = t(key);
}

// The header's single "接入通道" button is static markup, so it cannot bind one
// scope's handler directly in the HTML. Dispatch to the form that matches the
// scope the button is being shown in.
function openChannelsAddEntry() {
    if (channelScope() === 'tenant') return openTenantChannelForm();
    return openAddChannelPanel();
}

// Map a failed /api/channels response to the explanation the page shows. The
// distinction matters to the operator: "you may not" is actionable by asking an
// administrator, "not open yet" is not a permission problem at all.
function channelsFailureKey(status, code) {
    if (String(code || '') === 'database_unavailable') return 'channels_not_open';
    if (Number(status) === 403) return 'channels_no_permission';
    if (Number(status) === 405 || Number(status) === 503) return 'channels_not_open';
    return 'channels_load_failed';
}

// A scan start can fail before any register session exists: the route may be
// closed in this identity mode (405/503) or the caller may lack permission
// (403). Those deserve the same reason-specific explanation the page-level
// loader gives, not a generic "scan failed" that hides the cause.
function scanFailureText(status, code, message) {
    const key = channelsFailureKey(status, code);
    if (key !== 'channels_load_failed') return `${t(key)}：${t(key + '_desc')}`;
    return message || t('feishu_scan_fail');
}

// Replace the spinner with a terminal explanation. MUST always render something
// final: the page must never be left on "loading" for a request that has failed.
function renderChannelsUnavailable(container, status, code) {
    if (!container) return;
    const key = channelsFailureKey(status, code);
    container.classList.remove('hidden');
    container.innerHTML = `
        <div class="flex flex-col items-center justify-center py-16" id="channels-unavailable">
            <div class="w-14 h-14 rounded-2xl bg-slate-100 dark:bg-white/5 flex items-center justify-center mb-4">
                <i class="fas fa-circle-info text-slate-400 text-lg"></i>
            </div>
            <p class="text-slate-600 dark:text-slate-300 font-medium">${t(key)}</p>
            <p class="text-sm text-slate-400 dark:text-slate-500 mt-1">${t(key + '_desc')}</p>
        </div>`;
}

// --- Tenant-owned channel instances --------------------------------------
// Credentials are write-only: the server never returns a secret, so the list
// shows the instance metadata only and the form always starts empty for a
// secret field. `tenantChannelDraft` preserves what the operator typed when a
// write is rejected, so a conflict or a policy error does not clear the form.
let tenantChannelTypes = [];
let tenantChannelInstances = [];
// Whether the server answered this caller with their *own* range on the shared
// page (task 6.1). It decides what the form may offer — not which page is
// rendered, because there is only one page.
let tenantChannelSelfScope = false;
// The choose-a-target candidates the server offered, each with the ownership it
// produces (task 6.2). Empty until the page's first read; the picker falls back
// to the local catalogue meanwhile so an unread/older backend still works.
let tenantChannelTargets = [];
let tenantChannelDraft = null;

function tenantChannelType(type) {
    return tenantChannelTypes.find(t => t.channel_type === type) || null;
}

function channelTypeLabel(type) {
    // Label resolution lives in the shared module (task 3.1) so the public and
    // personal surfaces cannot disagree about what a type is called. The type
    // declaration is still looked up here: it is this controller's own state.
    return window.ChannelWorkbench.typeLabel(tenantChannelType(type), type, currentLang);
}

function channelFieldLabel(field) {
    return window.ChannelWorkbench.fieldLabel(field, currentLang);
}

// The form's type list is exactly what the server offered — never a local copy,
// so an unsupported type (e.g. the deferred 企微自建应用) cannot be submitted.
// On the caller's *own* surface (task 6.1) it is narrowed to the types the
// server reports as ready for personal onboarding. On the shared surface it is
// narrowed by the server's separate `inbound_admissible` verdict (task 7.7): a
// type whose adapter cannot stamp the sender would otherwise be created,
// started and reported connected while every message is refused at the inbound
// gate. The full list is kept for looking up the contract of an instance that
// already exists, and `!== false` means a payload from a deployment that does
// not send the verdict yet is not narrowed by it.
function tenantChannelTypeChoices() {
    return tenantChannelSelfScope
        ? tenantChannelTypes.filter(spec => spec.ready)
        : tenantChannelTypes.filter(spec => spec.inbound_admissible !== false);
}

function tenantChannelTypeOptions() {
    return tenantChannelTypes.map(spec => ({
        value: spec.channel_type,
        label: `${spec.label[currentLang] || spec.label.en} (${spec.channel_type})`,
    }));
}

function tenantChannelAgentOptions() {
    // The candidates come from the server (task 6.2): only it knows which
    // targets this caller may name and which ownership each produces.
    const fromServer = tenantChannelTargets.length > 0;
    const source = fromServer
        ? tenantChannelTargets.map(t => ({
            value: t.id,
            label: t.name ? `${t.name} (${t.id})` : t.id,
            scope: t.scope,
            is_tenant_default: !!t.is_tenant_default,
        }))
        // The local catalogue is the fallback for a backend that does not send
        // candidates yet, so an older deployment keeps working rather than
        // showing an empty picker. Its entries carry no derived ownership —
        // the catalogue *is* the caller's management range — so the own-surface
        // filter below is only applied to what the server derived. Filtering the
        // fallback by an ownership it never had would empty a member's picker
        // on exactly the deployments the fallback exists for.
        : (agentCatalog || [])
            .filter(a => a.enabled !== false)
            .map(a => ({
                value: a.id,
                label: a.name ? `${a.name} (${a.id})` : a.id,
                scope: null,
                is_tenant_default: false,
            }));
    // On the caller's own surface every legal target is one of their own, and
    // the target is **required**: selecting "none" would be refused on save, and
    // an option that cannot succeed is the "clickable but refused" shape this
    // change removes (task 6.1). The administrative surface keeps it — a tenant
    // connection may legitimately be unbound.
    const options = (tenantChannelSelfScope && fromServer)
        ? source.filter(o => o.scope === 'user')
        : source;
    if (tenantChannelSelfScope) return options;
    return [{ value: '', label: t('tenant_channel_agent_none') }, ...options];
}

// Channel types whose setup can be completed by scanning a QR code, and the
// function that starts each flow. Declared rather than inferred from the label
// so adding a scan flow is one line here and the contract test can pin the set.
const TENANT_CHANNEL_SCAN_TYPES = {
    feishu: 'startFeishuRegister',
    wecom_bot: 'startTenantWecomScan',
};

// The wording each scan flow uses. A scan flow creates one specific kind of app
// with its own credentials, so the copy belongs to the type and not to the
// shared panel: rendering a WeCom bot's entry with Feishu's "一键创建飞书应用"
// tells the operator the wrong thing about what the button will do and which
// app to expect afterwards. Declared beside the start function so a new type is
// added in one place, and pinned by the card contract test.
const TENANT_CHANNEL_SCAN_COPY = {
    feishu: {
        tab: 'feishu_mode_scan',
        manualTab: 'feishu_mode_manual',
        desc: 'feishu_scan_desc',
        btn: 'feishu_scan_btn',
    },
    wecom_bot: {
        tab: 'wecom_mode_scan',
        manualTab: 'wecom_mode_manual',
        desc: 'wecom_scan_desc',
        btn: 'wecom_scan_btn',
    },
};

// A type supports scanning only when both tables know it: the start function to
// run and the copy to show. Requiring both means the two cannot drift into a
// half-registered type — which is exactly how a WeCom card came to be rendered
// with Feishu's wording — and the card contract test fails if one is added
// without the other.
function tenantChannelSupportsScan(channelType) {
    // Both tables must know the type: the start function to run and the copy to
    // show. The module owns the pairing rule; the tables stay here because they
    // name page-global start functions (startFeishuRegister, …).
    return window.ChannelWorkbench.scanReady(
        TENANT_CHANNEL_SCAN_TYPES, TENANT_CHANNEL_SCAN_COPY, channelType);
}

// The scan copy for a type. Only called for types ``tenantChannelSupportsScan``
// accepted, so the null branch is unreachable by construction; it stays explicit
// rather than borrowing another type's wording.
function tenantChannelScanCopy(channelType) {
    return window.ChannelWorkbench.scanCopy(TENANT_CHANNEL_SCAN_COPY, channelType);
}

// Icon / colour come from the server's type description (the same declaration
// the platform page uses) with a neutral fallback for an unknown type.
function tenantChannelAppearance(channelType) {
    return window.ChannelWorkbench.appearance(tenantChannelType(channelType));
}

// The inline create/edit form for one tenant channel, in the same shape as the
// platform card: a Tab strip (扫码 / 手工填写) for scan-capable types and the
// credential form below it. Only the fields the server declared are rendered, so
// the form cannot submit a key the server would reject.
function buildTenantChannelForm(inst) {
    const draft = tenantChannelDraft || {};
    const channelType = (inst && inst.channel_type) || draft.channel_type || '';
    const spec = tenantChannelType(channelType);
    const fields = spec ? spec.credential_fields : [];
    const iid = (inst && inst.id) || 'new';
    const editing = !!(inst && inst.id);
    const supportsScan = tenantChannelSupportsScan(channelType);
    const scanStart = supportsScan ? TENANT_CHANNEL_SCAN_TYPES[channelType] : '';
    const scanCopy = supportsScan ? tenantChannelScanCopy(channelType) : null;
    const mode = draft.mode === 'scan' && supportsScan ? 'scan' : 'manual';
    const scanStatusId = `tenant-channel-scan-status-${iid}`;

    // Presentation is the shared module's (task 3.1). Everything it reads is
    // passed explicitly: the prefix namespaces the DOM so the personal form can
    // never collide with this one, the injected escape/t/lang keep the rendered
    // text identical, and the switch handler stays a page-global inline call so
    // the markup contract this page already publishes is unchanged.
    const wb = window.ChannelWorkbench;
    const view = {
        prefix: 'tenant-channel', iid, mode,
        escape: escapeHtml, t, lang: currentLang,
    };
    const tabs = wb.modeTabs(Object.assign({}, view, {
        supportsScan, copy: scanCopy,
        // Only a scan-capable type has two tabs to switch between; a manual-only
        // strip keeps the handler-free button it has always rendered.
        switchCall: supportsScan
            ? (id, target) => `switchTenantChannelMode('${escapeHtml(id)}', '${target}')`
            : undefined,
    }));

    // Both panes stay in the DOM and only their visibility toggles, so switching
    // to the scan tab and back cannot wipe credentials the operator already
    // typed. The scan pane never persists anything: it only fills the draft.
    const scanPane = wb.scanPane(Object.assign({}, view, {
        supportsScan, copy: scanCopy, statusId: scanStatusId, startCall: scanStart,
    }));

    const manualPane = wb.manualPane(Object.assign({}, view, {
        fields, values: draft.credentials || {},
    }));

    return `
        <div class="space-y-4" data-tenant-channel-form="${escapeHtml(iid)}">
            <div id="tenant-channel-error" class="hidden text-sm text-red-500"></div>
            ${editing ? '' : `
            <div>
                <label class="block text-xs text-slate-500 dark:text-slate-400 mb-1">${t('tenant_channel_type_label')}</label>
                <select id="tenant-channel-type" onchange="changeTenantChannelType(this.value)"
                    class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-white/10 bg-white dark:bg-[#141414] text-sm">
                    <option value="">${t('channels_select_placeholder')}</option>
                    ${tenantChannelTypeChoices().map(s =>
                        `<option value="${escapeHtml(s.channel_type)}" ${s.channel_type === channelType ? 'selected' : ''}>
                            ${escapeHtml(s.label[currentLang] || s.label.en)}</option>`).join('')}
                </select>
            </div>`}
            ${tabs}
            <div>
                <label class="block text-xs text-slate-500 dark:text-slate-400 mb-1">${t('tenant_channel_display_label')}${editing ? '' : ' <span class="text-red-500">*</span>'}</label>
                <input id="tenant-channel-display" type="text"
                    value="${escapeHtml(draft.display_name || (inst && inst.display_name) || '')}"${editing ? '' : ' data-tenant-channel-display-required="1"'}
                    class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-white/10 bg-white dark:bg-[#141414] text-sm">
            </div>
            <div>
                <label class="block text-xs text-slate-500 dark:text-slate-400 mb-1">${t('tenant_channel_agent_label')}</label>
                <select id="tenant-channel-agent"
                    class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-white/10 bg-white dark:bg-[#141414] text-sm">
                    ${tenantChannelAgentOptions().map(o => {
                        const selected = String(o.value) === String(
                            draft.agent_id !== undefined && draft.agent_id !== null
                                ? draft.agent_id : ((inst && inst.agent_id) || ''));
                        return `<option value="${escapeHtml(o.value)}"${selected ? ' selected' : ''}>${escapeHtml(o.label)}</option>`;
                    }).join('')}
                </select>
            </div>
            ${scanPane}
            ${manualPane}
            <div class="flex items-center justify-end gap-3 pt-1">
                <button type="button" onclick="closeTenantChannelForm()"
                    class="px-4 py-2 rounded-lg border border-slate-200 dark:border-white/10 text-slate-600 dark:text-slate-300 text-sm font-medium cursor-pointer">
                    ${t('channels_cancel')}</button>
                <button type="button" onclick="submitTenantChannel()" ${editing ? '' : ''}
                    class="px-4 py-2 rounded-lg bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium cursor-pointer">
                    ${t('tenant_channel_save')}</button>
            </div>
        </div>`;
}

// One tenant channel instance as a card, using the same shell as the platform
// page so the two lists are recognisably the same interface. Editing happens
// inline in the card rather than in a separate panel.
function renderTenantChannelCard(inst) {
    const appearance = tenantChannelAppearance(inst.channel_type);
    const editing = !!(tenantChannelDraft && tenantChannelDraft.instance_id === inst.id);
    const badge = inst.active
        ? `<span class="px-2 py-0.5 rounded-full text-[11px] bg-emerald-50 text-emerald-600 dark:bg-emerald-900/30 dark:text-emerald-400">${t('tenant_channel_active')}</span>`
        : `<span class="px-2 py-0.5 rounded-full text-[11px] bg-slate-100 text-slate-500 dark:bg-white/5 dark:text-slate-400">${t('tenant_channel_inactive')}</span>`;
    const statusText = `${badge}${inst.agent_id ? ` <span class="text-xs text-slate-400">· ${escapeHtml(inst.agent_id)}</span>` : ''}`;
    return `
        <div class="bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10 p-5"
             data-tenant-channel-row="${escapeHtml(inst.id)}">
            ${buildChannelCardShell({
                iid: inst.id,
                label: inst.display_name,
                icon: appearance.icon,
                color: appearance.color,
                statusDot: inst.active ? 'bg-primary-400' : 'bg-slate-300',
                statusText: statusText,
                subtitle: `${escapeHtml(channelTypeLabel(inst.channel_type))} · ${escapeHtml(inst.id)}`,
                headerMb: true,
                actionsHtml: `
                    <div class="flex items-center gap-2 shrink-0">
                        <button type="button" onclick="openTenantChannelForm('${escapeHtml(inst.id)}')"
                            class="text-xs px-3 py-1.5 rounded-lg border border-slate-200 dark:border-white/10
                                   text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-white/5 cursor-pointer">
                            ${t('tenant_channel_edit')}</button>
                        <button type="button" onclick="toggleTenantChannel('${escapeHtml(inst.id)}', ${inst.active ? 'false' : 'true'})"
                            class="text-xs px-3 py-1.5 rounded-lg border border-slate-200 dark:border-white/10
                                   text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-white/5 cursor-pointer">
                            ${inst.active ? t('tenant_channel_disable') : t('tenant_channel_enable')}</button>
                    </div>`,
                bodyHtml: editing ? buildTenantChannelForm(inst) : '',
            })}
        </div>`;
}

function tenantChannelFieldInput(field, value) {
    // Required-ness, the secret/blank rule and the "held" hint all come from the
    // shared module (task 3.1): one definition for both surfaces. The prefix is
    // what keeps the emitted attributes this page's own (data-tenant-channel-*).
    return window.ChannelWorkbench.fieldInput({
        field, value, prefix: 'tenant-channel',
        escape: escapeHtml, t, lang: currentLang,
    });
}

// Required credential fields the operator has left empty, for a create. An edit
// is deliberately exempt: a blank secret there means "keep the stored value",
// which the server honours by merging over the existing bundle.
function tenantChannelMissingRequiredFields(channelType, collected) {
    const entry = tenantChannelType(channelType);
    if (!entry) return [];  // unknown type: the server decides, not the console
    return window.ChannelWorkbench.missingRequiredFields(entry.credential_fields, collected);
}

function collectTenantChannelFields() {
    // Scoped to this page's prefix, so a personal form on the same document can
    // never contribute values to a public write.
    return window.ChannelWorkbench.collectFields(document, 'tenant-channel');
}

// Build the create/update body. Only the fields the caller actually supplied
// are sent, so editing a display name never blanks a stored secret.
function tenantChannelPayload(form) {
    return window.ChannelWorkbench.payload(form);
}

function tenantChannelWriteErrorKey(status, code) {
    const code_text = String(code || '');
    if (code_text === 'invalid_old') return 'tenant_channel_error_password';
    // A misconfigured deployment is not something "you got wrong": the
    // operator can retry forever and never succeed, so it must say so.
    if (code_text === 'credential_crypto') return 'tenant_channel_error_crypto';
    if (Number(status) === 409) return 'tenant_channel_error_conflict';
    if (code_text === 'forbidden' || Number(status) === 403) return 'tenant_channel_error_agent';
    if (Number(status) === 404) return 'tenant_channel_error_missing';
    return 'tenant_channel_error_invalid';
}

function tenantChannelTypeLabelForStatus(status) {
    return tenantChannelWriteErrorKey(status, '');
}

function loadTenantChannelsView() {
    const container = document.getElementById('channels-content');
    if (!container) return Promise.resolve();
    container.innerHTML = `<div class="flex items-center gap-2 py-8 justify-center text-slate-400 dark:text-slate-500 text-sm">
        <i class="fas fa-spinner fa-spin text-xs"></i><span>Loading...</span></div>`;

    const roster = agentCatalog.length ? Promise.resolve() : loadAgentCatalog();
    return roster.then(() => fetch('/api/tenant/channels')
        .then(r => r.json().then(data => ({ status: r.status, data })))
        .then(({ status, data }) => {
            if (!data || data.status !== 'success') {
                renderChannelsUnavailable(container, status, data && data.code);
                return;
            }
            tenantChannelTypes = data.channel_types || [];
            tenantChannelInstances = data.items || [];
            // ``self`` means the server answered with the caller's own range
            // (task 6.1). The page is the same one; what changes is that the
            // form must offer only what this caller's create would accept — a
            // type the member cannot configure, or an Agent that is not theirs,
            // is refused on save, and offering it would be the "clickable but
            // refused" shape this change removes.
            tenantChannelSelfScope = data.scope === 'self';
            tenantChannelTargets = data.targets || [];
            renderTenantChannels();
        })
        .catch(() => renderChannelsUnavailable(container, 0, 'network')));
}

// Whether the last tenant channel write is actually in service yet. A write
// commits to the identity store first and is brought up in the running process
// afterwards, so "success" only means the row was stored. Reporting that
// distinction is the point: an operator who is not told otherwise assumes the
// channel is live, and a channel that silently kept the previous credentials
// would be worse than one that is plainly down.
let tenantChannelRuntimeNotice = null;

function tenantChannelRuntimeNoticeFrom(data) {
    const runtime = (data && data.runtime) || null;
    if (!runtime) return null;
    return {
        applied: !!runtime.applied,
        pending: !!runtime.pending,
        reason: String(runtime.error || ''),
    };
}

function tenantChannelRuntimeNoticeHtml() {
    const notice = tenantChannelRuntimeNotice;
    if (!notice) return '';
    const tone = notice.applied
        ? 'bg-emerald-50 text-emerald-600 dark:bg-emerald-900/20 dark:text-emerald-300'
        : 'bg-amber-50 text-amber-700 dark:bg-amber-900/20 dark:text-amber-300';
    const text = notice.applied
        ? t('tenant_channel_applied')
        : (notice.reason
            ? `${t('tenant_channel_not_applied')} ${notice.reason}`
            : t('tenant_channel_not_applied'));
    return `<div class="mb-4 px-3 py-2 rounded-lg text-xs ${tone}" data-tenant-channel-runtime>${escapeHtml(text)}</div>`;
}

function renderTenantChannels() {
    const container = document.getElementById('channels-content');
    if (!container) return;
    const addPanel = document.getElementById('channels-add-panel');
    if (addPanel) { addPanel.classList.add('hidden'); addPanel.innerHTML = ''; }

    if (!tenantChannelInstances.length && !tenantChannelDraft) {
        container.innerHTML = `
            ${tenantChannelRuntimeNoticeHtml()}
            <div class="flex flex-col items-center justify-center py-20">
                <div class="w-16 h-16 rounded-2xl bg-blue-50 dark:bg-blue-900/20 flex items-center justify-center mb-4">
                    <i class="fas fa-tower-broadcast text-blue-400 text-xl"></i>
                </div>
                <p class="text-slate-500 dark:text-slate-400 font-medium">${t('channels_empty')}</p>
                <p class="text-sm text-slate-400 dark:text-slate-500 mt-1">${t(tenantChannelSelfScope ? 'tenant_channel_empty_desc_self' : 'tenant_channel_empty_desc')}</p>
                <button onclick="openTenantChannelForm()"
                    class="mt-4 px-4 py-2 rounded-lg bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium cursor-pointer">
                    ${t('channels_add')}</button>
            </div>`;
        return;
    }

    const rows = tenantChannelInstances.map(inst => renderTenantChannelCard(inst)).join('');
    // The "add" draft has no instance row yet; it renders as its own card so the
    // create form uses exactly the same Tab shape as an existing card.
    const draftCard = (tenantChannelDraft && !tenantChannelDraft.instance_id)
        ? `
        <div class="bg-white dark:bg-[#1A1A1A] rounded-xl border border-primary-200 dark:border-primary-800 p-5"
             data-tenant-channel-row="new">
            ${buildChannelCardShell({
                iid: 'new',
                label: tenantChannelDraft.display_name || t('channels_add'),
                icon: tenantChannelAppearance(tenantChannelDraft.channel_type).icon,
                color: tenantChannelAppearance(tenantChannelDraft.channel_type).color,
                statusDot: 'bg-amber-400 animate-pulse',
                statusText: `<span class="text-xs text-amber-500">${t('channels_connecting')}</span>`,
                subtitle: tenantChannelDraft.channel_type || '',
                headerMb: true,
                actionsHtml: '',
                bodyHtml: buildTenantChannelForm(null),
            })}
        </div>` : '';

    // The page title / description / "add" button live in the static header
    // (chat.html) and are settled by syncChannelsHeader, so this list renders
    // only the notice and the cards.
    container.innerHTML = `
        ${tenantChannelRuntimeNoticeHtml()}
        <div class="grid gap-4">${draftCard}${rows}</div>`;
}

// Inline create/edit. The form lives inside the card now, so this records the
// draft (which survives a rejected write) and repaints the list; it no longer
// builds a separate panel.
function openTenantChannelForm(instanceId) {
    const inst = instanceId
        ? tenantChannelInstances.find(i => i.id === instanceId) || null
        : null;
    const previous = tenantChannelDraft;
    const sameTarget = previous && previous.instance_id === (instanceId || '');
    const draft = sameTarget ? previous : null;
    const type = (inst && inst.channel_type) || (draft && draft.channel_type) || '';

    tenantChannelDraft = {
        instance_id: instanceId || '',
        channel_type: type,
        display_name: (inst && inst.display_name) || (draft && draft.display_name) || '',
        agent_id: (inst && inst.agent_id) || (draft && draft.agent_id) || '',
        active: inst ? !!inst.active : true,
        credentials: (draft && draft.credentials) || {},
        mode: (draft && draft.mode) || (tenantChannelSupportsScan(type) ? 'scan' : 'manual'),
        expected_version: inst ? inst.version : undefined,
    };
    renderTenantChannels();
}

// Toggle the scan / manual panes of one inline form. Both panes stay rendered;
// only their visibility changes, so moving between them keeps typed input.
function switchTenantChannelMode(iid, mode) {
    if (!tenantChannelDraft) return;
    tenantChannelDraft.mode = mode === 'scan' ? 'scan' : 'manual';
    // Pane visibility is the shared module's job: it knows the prefixed pane ids
    // it emitted, so a personal form's panes are never touched from here.
    window.ChannelWorkbench.applyMode(document, 'tenant-channel', iid, tenantChannelDraft.mode);
    const card = document.querySelector(`[data-tenant-channel-row="${iid}"]`);
    if (!card) return;
    const activeClasses = 'bg-white dark:bg-slate-700 text-slate-800 dark:text-slate-100 shadow-sm';
    const inactiveClasses = 'text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200';
    card.querySelectorAll('[data-tenant-channel-mode]').forEach(btn => {
        const isActive = btn.getAttribute('data-tenant-channel-mode') === tenantChannelDraft.mode;
        btn.className = `flex-1 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${isActive ? activeClasses : inactiveClasses}`;
    });
}

// Switching the type of a create form resets the credentials (the previous
// type's keys no longer apply) but keeps what the operator typed for the name.
function changeTenantChannelType(channelType) {
    if (!tenantChannelDraft) return;
    const display = document.getElementById('tenant-channel-display');
    if (display) tenantChannelDraft.display_name = display.value || '';
    tenantChannelDraft.channel_type = channelType || '';
    tenantChannelDraft.credentials = {};
    tenantChannelDraft.mode = tenantChannelSupportsScan(channelType) ? 'scan' : 'manual';
    renderTenantChannels();
}

// Pre-fill the inline create form from a scan result. The values live in the
// in-memory draft until the write lands, and both this draft and the secret
// field are dropped when the form is closed.
//
// ``scanTicket`` is the grant the server minted for this very scan. It is what
// lets the write that follows skip the password prompt, so a successful scan
// becomes a stored channel instead of a filled-in form nobody submits.
function applyScanToTenantForm(credentials, scanTicket) {
    const draft = tenantChannelDraft;
    if (!draft) return false;
    const display = document.getElementById('tenant-channel-display');
    if (display) draft.display_name = display.value || draft.display_name;
    // The re-render below rebuilds the form, so anything the operator chose
    // before scanning has to be read back into the draft first — otherwise the
    // agent selection, say, silently reverts to the default.
    const agent = document.getElementById('tenant-channel-agent');
    if (agent) draft.agent_id = agent.value || '';
    draft.credentials = Object.assign({}, draft.credentials, credentials || {});
    applyScanTicketToTenantForm(scanTicket);
    // Show the operator what the scan produced so they can review before saving.
    draft.mode = 'manual';
    renderTenantChannels();
    return true;
}

function applyFeishuScanToTenantForm(appId, appSecret, scanTicket) {
    return applyScanToTenantForm({
        feishu_app_id: appId || '',
        feishu_app_secret: appSecret || '',
    }, scanTicket);
}

// A scan's one-time grant is carried beside the credentials, never among them:
// it is not part of the bundle the server stores, it is the proof of presence
// that lets the create skip the password prompt.
function applyScanTicketToTenantForm(scanTicket) {
    const draft = tenantChannelDraft;
    if (!draft || !scanTicket) return false;
    draft.scan_ticket = scanTicket;
    return true;
}

// The name a scan-created channel gets without asking the operator for one. The
// app id is the only part of a scan result a person can tell apart in a list,
// so the name is the type plus its last four characters.
function tenantChannelAutoName(channelType, credentials) {
    return window.ChannelWorkbench.autoName(
        tenantChannelType(channelType), channelType, credentials, currentLang);
}

// A password must be collected in a real element. ``window.prompt`` is a native
// dialog the browser may suppress — after "prevent this page from creating
// additional dialogs" it returns null with no visible signal — and a null reads
// as an empty password, so the write is refused 401 while the operator has no
// idea a prompt was ever expected. That is exactly how a successful scan used
// to end up as no channel at all.
function recentPasswordDialogHtml() {
    return `
        <div class="bg-white dark:bg-[#1A1A1A] rounded-xl p-5 w-80 shadow-xl">
            <p class="text-sm font-medium text-slate-800 dark:text-slate-100 mb-1">${escapeHtml(t('admin_field_recent_password'))}</p>
            <p class="text-xs text-slate-400 dark:text-slate-500 mb-3">${escapeHtml(t('admin_field_recent_password_hint'))}</p>
            <input id="tenant-channel-password" type="password" autocomplete="current-password"
                class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-white/10 bg-white dark:bg-[#141414] text-sm">
            <div class="flex justify-end gap-2 mt-4">
                <button type="button" data-recent-password-cancel
                    class="px-3 py-1.5 rounded-lg border border-slate-200 dark:border-white/10 text-slate-600 dark:text-slate-300 text-xs font-medium cursor-pointer">${escapeHtml(t('channels_cancel'))}</button>
                <button type="button" data-recent-password-ok
                    class="px-3 py-1.5 rounded-lg bg-primary-500 hover:bg-primary-600 text-white text-xs font-medium cursor-pointer">${escapeHtml(t('tenant_channel_save'))}</button>
            </div>
        </div>`;
}

// Resolves the password, or ``null`` if the operator cancelled. Cancelling is
// distinguishable from an empty password, so the caller can stop instead of
// sending a write that is certain to be refused.
function askRecentPassword() {
    return new Promise((resolve) => {
        const host = document.createElement('div');
        host.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/40';
        host.setAttribute('data-recent-password-dialog', '1');
        host.innerHTML = recentPasswordDialogHtml();
        const done = (value) => {
            host.remove();
            resolve(value);
        };
        const field = host.querySelector('#tenant-channel-password');
        const confirm = host.querySelector('[data-recent-password-ok]');
        if (!field || !confirm) { done(null); return; }
        if (confirm.addEventListener) {
            confirm.addEventListener('click', () => done(field.value || ''));
            const cancel = host.querySelector('[data-recent-password-cancel]');
            if (cancel && cancel.addEventListener) {
                cancel.addEventListener('click', () => done(null));
            }
            field.addEventListener('keydown', (event) => {
                if (event && event.key === 'Enter') done(field.value || '');
            });
        }
        if (document.body && document.body.appendChild) document.body.appendChild(host);
        if (field.focus) field.focus();
    });
}

// WeCom Intelligent Bot scan. The flow is driven entirely by the vendor's
// browser SDK (``WECOM_BOT_SOURCE`` is a static constant, so nothing is held
// server-side and database mode needs no extra plumbing). If the SDK cannot
// load — offline or a blocked CDN — the manual credential pane is still
// rendered, so the operator always has a way to finish.
function startTenantWecomScan(statusId, iid) {
    const statusEl = document.getElementById(statusId || `tenant-channel-scan-status-${iid}`);
    const fail = (message) => {
        if (statusEl) {
            statusEl.innerHTML = `<p class="text-sm text-red-500 text-center">${escapeHtml(message)}</p>
                <p class="text-xs text-slate-400 dark:text-slate-500 text-center mt-1">${t('tenant_channel_scan_manual_hint')}</p>`;
        }
    };
    ensureWecomSdkLoaded().then(() => {
        WecomAIBotSDK.openBotInfoAuthWindow({
            source: WECOM_BOT_SOURCE,
            onCreated: function (bot) {
                if (statusEl) {
                    statusEl.innerHTML = `
                        <div class="flex flex-col items-center py-2">
                            <div class="w-10 h-10 rounded-full bg-emerald-50 dark:bg-emerald-900/30 flex items-center justify-center mb-2">
                                <i class="fas fa-check text-emerald-500 text-lg"></i>
                            </div>
                            <p class="text-sm font-medium text-emerald-600 dark:text-emerald-400">${t('wecom_scan_success')}</p>
                        </div>`;
                }
                applyScanToTenantForm({
                    wecom_bot_id: bot.botid || '',
                    wecom_bot_secret: bot.secret || '',
                });
            },
            onError: function (err) {
                fail(t('wecom_scan_fail') + ': ' + ((err && (err.message || err.code)) || ''));
            },
        });
    }).catch(err => fail('SDK load failed: ' + (err && err.message ? err.message : '')));
}

function closeTenantChannelForm() {
    tenantChannelDraft = null;
    const panel = document.getElementById('channels-add-panel');
    if (panel) { panel.classList.add('hidden'); panel.innerHTML = ''; }
    renderTenantChannels();
}

function tenantChannelFormError(key, detail) {
    const el = document.getElementById('tenant-channel-error');
    if (!el) return;
    el.textContent = detail ? `${t(key)} ${detail}` : t(key);
    el.classList.remove('hidden');
}

function submitTenantChannel() {
    const draft = tenantChannelDraft;
    if (!draft) return Promise.resolve();
    // Keep what was typed so a rejected write does not clear the form.
    draft.channel_type = draft.channel_type || '';
    draft.display_name = (document.getElementById('tenant-channel-display') || {}).value || '';
    draft.agent_id = (document.getElementById('tenant-channel-agent') || {}).value || '';
    // Merge over the draft rather than replace it. Secret inputs are rendered
    // blank by policy, so a secret that a scan just handed us lives only in the
    // draft; replacing would erase it, and the create would then be refused as
    // "missing a required field" — a successful scan that never becomes a
    // channel. Merging also keeps the documented "leave blank to keep the
    // stored value" behavior, because an untouched secret input contributes
    // nothing at all.
    draft.credentials = Object.assign({}, draft.credentials, collectTenantChannelFields());

    const editing = !!draft.instance_id;
    // The name is what the operator will see in the list and the duplicate
    // check keys on, and the server refuses an empty one. Rejection there reads
    // as a generic "save failed", so catch it here where the reason is obvious.
    if (!editing && !String(draft.display_name || '').trim()) {
        tenantChannelFormError('tenant_channel_error_display_required');
        return Promise.resolve();
    }
    // A create must carry the whole minimum set. The check happens before the
    // request so the operator gets the missing field names instead of a generic
    // rejection — and the draft survives, so nothing they typed is lost.
    if (!editing) {
        const missing = tenantChannelMissingRequiredFields(
            draft.channel_type, draft.credentials);
        if (missing.length) {
            tenantChannelFormError('tenant_channel_error_required',
                missing.map(f => channelFieldLabel(f)).join(' / '));
            return Promise.resolve();
        }
    }

    // Proof of presence. A scan already supplied it, and asking for a password
    // anyway would make every scan a two-step flow again — so a grant short
    // circuits the dialog entirely. Everything else collects a password in a
    // real element; see askRecentPassword for why a native prompt is not
    // acceptable here.
    const grant = String(draft.scan_ticket || '');
    let asking;
    if (grant) {
        asking = Promise.resolve(draft.recent_password || '');
    } else if (draft.recent_password) {
        asking = Promise.resolve(draft.recent_password);
    } else {
        asking = askRecentPassword();
    }
    return asking.then((password) => {
        // Cancelled: stop rather than send a write certain to be refused.
        if (password === null) return undefined;
        if (!grant && !String(password || '')) {
            tenantChannelFormError('tenant_channel_error_password_required');
            return undefined;
        }
        draft.recent_password = password || '';

        const body = tenantChannelPayload(editing
            ? {
                display_name: draft.display_name,
                agent_id: draft.agent_id,
                expected_version: draft.expected_version,
                credentials: Object.keys(draft.credentials).length ? draft.credentials : undefined,
                recent_password: draft.recent_password,
                scan_ticket: grant,
            }
            : {
                display_name: draft.display_name,
                agent_id: draft.agent_id,
                expected_version: undefined,
                credentials: draft.credentials,
                recent_password: draft.recent_password,
                scan_ticket: grant,
            });
        if (!editing) {
            body.channel_type = draft.channel_type;
            delete body.expected_version;
        }

        const url = editing
            ? `/api/tenant/channels/${encodeURIComponent(draft.instance_id)}`
            : '/api/tenant/channels';
        return fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        }).then(r => r.json().then(data => ({ status: r.status, data })).catch(() => ({ status: r.status, data: null })))
            .then(({ status, data }) => {
                if (!data || data.status !== 'success') {
                    // A refused grant is spent: drop it so a retry collects a
                    // password instead of failing the same way forever.
                    if (status === 401) draft.scan_ticket = '';
                    tenantChannelFormError(tenantChannelWriteErrorKey(status, data && data.code));
                    return; // draft (and the rendered inputs) survive
                }
                tenantChannelRuntimeNotice = tenantChannelRuntimeNoticeFrom(data);
                tenantChannelDraft = null;
                loadTenantChannelsView();
            })
            // A transport failure is not a rejected form: saying "the values
            // are not valid" sends the operator looking for a mistake that is
            // not there.
            .catch(() => tenantChannelFormError('tenant_channel_error_network'));
    });
}

// The write a scan owes the server once it reports success: no second click,
// and no password prompt, because the scan's grant is the proof of presence.
// The name is derived rather than asked for, so the operator's only act is the
// scan itself.
function autoPersistScannedTenantChannel() {
    const draft = tenantChannelDraft;
    if (!draft) return Promise.resolve();
    if (!draft.scan_ticket) {
        // Without a grant this would have to ask for a password, which is the
        // manual path; leave the form for the operator to submit.
        return Promise.resolve();
    }
    const display = document.getElementById('tenant-channel-display');
    if (display && !String(display.value || '').trim()) {
        display.value = tenantChannelAutoName(draft.channel_type, draft.credentials);
    }
    return submitTenantChannel();
}

function toggleTenantChannel(instanceId, active) {
    const inst = tenantChannelInstances.find(i => i.id === instanceId);
    if (!inst) return Promise.resolve();
    // The password goes through the same element the save path uses, so it
    // cannot be silently suppressed the way a native prompt can.
    return askRecentPassword().then((recent) => {
        if (recent === null) return undefined;
        return fetch(`/api/tenant/channels/${encodeURIComponent(instanceId)}/active`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ active: !!active, expected_version: inst.version, recent_password: recent || '' }),
        }).then(r => r.json().then(data => ({ status: r.status, data })).catch(() => ({ status: r.status, data: null })))
            .then(({ status, data }) => {
                if (!data || data.status !== 'success') {
                    const container = document.getElementById('channels-content');
                    renderChannelsUnavailable(container, status, data && data.code);
                    return;
                }
                tenantChannelRuntimeNotice = tenantChannelRuntimeNoticeFrom(data);
                loadTenantChannelsView();
            });
    });
}

function loadChannelsView() {
    // The header follows the *relative* scope; the loader follows the surface.
    syncChannelsHeader(channelPageScope());
    const scope = channelScope();
    if (scope === 'tenant') return loadTenantChannelsView();
    const container = document.getElementById('channels-content');
    if (!container) return Promise.resolve();
    container.innerHTML = `<div class="flex items-center gap-2 py-8 justify-center text-slate-400 dark:text-slate-500 text-sm">
        <i class="fas fa-spinner fa-spin text-xs"></i><span>Loading...</span></div>`;

    const roster = agentCatalog.length ? Promise.resolve() : loadAgentCatalog();
    return roster.then(() => fetch('/api/channels')
        .then(r => r.json().then(data => ({ status: r.status, data })).catch(() => ({ status: r.status, data: null })))
        .then(({ status, data }) => {
            // A failed request must resolve to a final explanation, never to a
            // page that keeps spinning because the payload had no channels.
            if (!data || data.status !== 'success') {
                renderChannelsUnavailable(container, status, data && data.code);
                return;
            }
            channelsData = data.channels || [];
            channelsMultiAgent = !!data.multi_agent;
            multiInstanceTypes = data.multi_instance_types || [];
            channelInstancesView = data.instances || [];
            renderActiveChannels();
        })
        .catch(() => renderChannelsUnavailable(container, 0, 'network')));
}

// Build the list of cards to render. In multi-Agent mode the multi-instance
// types (feishu) contribute one card per channel_instances record (from
// data.instances); everything else contributes its single per-type card. Each
// item carries an `iid` (instance id) that keys its DOM and actions: for legacy
// per-type cards it is just the channel name.
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
    return list;
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

        const fieldsHtml = buildChannelFieldsHtml(iid, ch.fields || []);
        const hasFields = (ch.fields || []).length > 0;

        const weixinWaiting = ch.name === 'weixin' && ch.login_status && ch.login_status !== 'logged_in';
        const wecomNeedsCreds = ch.name === 'wecom_bot' && !_wecomBotHasCreds(ch);
        // 飞书 active 卡片渲染带 Tab 的 panel：手动填写 + 扫码重建（覆盖现有配置）
        const isFeishu = ch.name === 'feishu';
        // An instance card (multi-Agent feishu) shows the bound agent inline and
        // uses the instance id as its subtitle instead of the bare type name.
        const isInstance = isMultiInstanceType(ch.name) && !!ch.instance_id;
        let statusDot, statusText;
        if (weixinWaiting) {
            statusDot = 'bg-amber-400 animate-pulse';
            statusText = ch.login_status === 'scanned'
                ? `<span class="text-xs text-primary-500">${t('weixin_scan_scanned')}</span>`
                : `<span class="text-xs text-amber-500">${t('weixin_scan_waiting')}</span>`;
        } else if (wecomNeedsCreds) {
            statusDot = 'bg-amber-400 animate-pulse';
            statusText = `<span class="text-xs text-amber-500">${t('channels_connecting')}</span>`;
        } else {
            statusDot = 'bg-primary-400';
            statusText = `<span class="text-xs text-primary-500">${t('channels_connected')}</span>`;
        }

        card.innerHTML = buildChannelCardShell({
            iid, label, icon: ch.icon, color: ch.color,
            statusDot, statusText, subtitle: iid,
            headerMb: !!(hasFields || weixinWaiting || wecomNeedsCreds || isFeishu || multiAgentMode()),
            actionsHtml: `
                <button onclick="disconnectChannel('${ch.name}', '${isInstance ? iid : ''}')"
                    class="px-3 py-1.5 rounded-lg text-xs font-medium
                           bg-red-50 dark:bg-red-900/20 text-red-500 dark:text-red-400
                           hover:bg-red-100 dark:hover:bg-red-900/40
                           cursor-pointer transition-colors flex-shrink-0">
                    ${t('channels_disconnect')}
                </button>`,
            bodyHtml: `
            ${multiAgentMode() ? `<div class="channel-agent-bind">
                <span class="text-xs text-slate-500 whitespace-nowrap" title="${escapeHtml(t('channel_bound_agent_hint'))}">${escapeHtml(t('channel_bound_agent'))}</span>
                <div id="ch-members-${iid}" class="cfg-dropdown cfg-dropdown-avatar cfg-dropdown-sm cfg-dropdown-multi" tabindex="0" style="width: 200px;">
                    <div class="cfg-dropdown-selected">
                        <span class="cfg-dropdown-faces"></span>
                        <span class="cfg-dropdown-text">--</span>
                        <i class="fas fa-chevron-down cfg-dropdown-arrow"></i>
                    </div>
                    <div class="cfg-dropdown-menu"></div>
                </div>
            </div>` : ''}
            ${weixinWaiting ? `<div id="weixin-active-qr" class="flex flex-col items-center py-2">
                <button onclick="showWeixinActiveQr()"
                    class="px-4 py-2 rounded-lg bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium
                           cursor-pointer transition-colors duration-150">
                    ${t('weixin_scan_title')}
                </button>
            </div>` : ''}
            ${wecomNeedsCreds ? `<div id="wecom-active-auth" class="flex flex-col items-center py-2">
                <p class="text-sm text-slate-500 dark:text-slate-400 mb-3">${t('wecom_scan_desc')}</p>
                <button onclick="startWecomBotAuthInCard()"
                    class="px-5 py-2 rounded-lg bg-emerald-500 hover:bg-emerald-600 text-white text-sm font-medium
                           cursor-pointer transition-colors duration-150">
                    <i class="fas fa-qrcode mr-2"></i>${t('wecom_scan_btn')}
                </button>
                <div id="wecom-card-scan-status" class="mt-3"></div>
            </div>` : ''}
            ${isFeishu ? buildFeishuPanel(ch, true) : (hasFields ? `<div class="space-y-4">
                ${fieldsHtml}
                <div class="flex items-center justify-end gap-3 pt-1">
                    <span id="ch-status-${iid}" class="text-xs text-primary-500 opacity-0 transition-opacity duration-300"></span>
                    <button onclick="saveChannelConfig('${ch.name}', '${isInstance ? iid : ''}')"
                        class="px-4 py-2 rounded-lg bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium
                               cursor-pointer transition-colors duration-150 disabled:opacity-50 disabled:cursor-not-allowed"
                        id="ch-save-${iid}">${t('channels_save')}</button>
                </div>
            </div>` : '')}`,
        });

        container.appendChild(card);
        bindSecretFieldEvents(card);
        initChannelTeam(ch);

        if (weixinWaiting) {
            startWeixinActiveStatusPoll();
        }
    });
}

// One multi-select per channel card, same idea as creating a team in the chat
// history: pick a set of Agents; the first pick is the owner (receives every
// message and can delegate), the rest are teammates. An ordered list, so the
// first checked stays the owner. Empty = follow the default Agent, solo.
let _channelTeam = {};  // iid -> ordered [ownerId, ...memberIds]

function initChannelTeam(ch) {
    const iid = ch.iid || ch.name;
    if (!multiAgentMode()) return;
    const box = document.getElementById(`ch-members-${iid}`);
    if (!box) return;
    // Seed the ordered team: owner first, then its members. A legacy per-type
    // card has no instance fields, so fall back to its channel-type binding.
    const owner = ch.instance_id ? (ch.agent_id || '') : (channelBoundAgentId(ch.name) || '');
    const members = Array.isArray(ch.members) ? ch.members : [];
    _channelTeam[iid] = [owner, ...members].filter((id, i, arr) => id && arr.indexOf(id) === i);
    box.dataset.channelName = ch.name;
    renderChannelTeam(iid);
    if (!box._ddBound) {
        box.querySelector('.cfg-dropdown-selected').addEventListener('click', (e) => {
            e.stopPropagation();
            document.querySelectorAll('.cfg-dropdown.open').forEach(d => { if (d !== box) d.classList.remove('open'); });
            box.classList.toggle('open');
        });
        box._ddBound = true;
    }
}

function renderChannelTeam(iid) {
    const box = document.getElementById(`ch-members-${iid}`);
    if (!box) return;
    const team = _channelTeam[iid] || [];
    const ownerId = team[0] || '';
    const agents = enabledAgents();
    const chosen = team.map(id => findAgent(id)).filter(Boolean);

    const faces = box.querySelector('.cfg-dropdown-faces');
    const textEl = box.querySelector('.cfg-dropdown-text');
    const MAX_FACES = 3;
    if (chosen.length) {
        // Trigger: up to MAX_FACES avatars; any beyond that become a "+N" pill
        // so the count always matches how many are hidden, never the total.
        const shown = chosen.slice(0, MAX_FACES);
        const extra = chosen.length - shown.length;
        faces.innerHTML = shown.map(a => agentAvatarHTML(a, 18)).join('')
            + (extra > 0 ? `<span class="cfg-dropdown-more">+${extra}</span>` : '');
        textEl.textContent = chosen[0].name || chosen[0].id;
        textEl.classList.remove('text-slate-400', 'dark:text-slate-500');
    } else {
        // Nothing picked: this channel follows the default Agent. Show it
        // (dim) rather than an empty "none", so the receiver is always clear.
        const def = findAgent(defaultAgentId);
        faces.innerHTML = def ? agentAvatarHTML(def, 18) : '';
        textEl.textContent = def ? (def.name || def.id) : t('channel_team_none');
        textEl.classList.add('text-slate-400', 'dark:text-slate-500');
    }

    // Menu: a checklist. The first-picked carries a small "default" badge so it
    // is clear which Agent receives and delegates. The selected tick is the
    // dropdown's global .active::after, so no per-row tick element is needed.
    const menu = box.querySelector('.cfg-dropdown-menu');
    if (!agents.length) {
        menu.innerHTML = `<div class="cfg-dropdown-item cfg-dropdown-empty">${escapeHtml(t('channel_team_no_candidates'))}</div>`;
        return;
    }
    menu.innerHTML = agents.map(a => {
        const on = team.includes(a.id);
        const isOwner = a.id === ownerId;
        return `<div class="cfg-dropdown-item cfg-dropdown-check${on ? ' active' : ''}"
            onclick="event.stopPropagation(); toggleChannelTeam('${iid}','${a.id}')">
            <span class="cfg-dropdown-item-face">${agentAvatarHTML(a, 20)}</span>
            <span class="cfg-dropdown-label">${escapeHtml(a.name || a.id)}</span>
            ${isOwner ? `<span class="cfg-dropdown-badge">${escapeHtml(t('channel_bound_default'))}</span>` : ''}
        </div>`;
    }).join('');
}

function toggleChannelTeam(iid, agentId) {
    const box = document.getElementById(`ch-members-${iid}`);
    const chName = box ? (box.dataset.channelName || '') : '';
    const team = _channelTeam[iid] || [];
    const i = team.indexOf(agentId);
    if (i === -1) team.push(agentId);       // append: order = pick order
    else team.splice(i, 1);                 // remove; if it was owner, next becomes owner
    _channelTeam[iid] = team;
    renderChannelTeam(iid);
    // Persist: first pick is the owner (empty -> default Agent), rest members.
    const ownerId = team[0] || '';
    const members = team.slice(1);
    bindChannelAgent(chName, ownerId, iid, members);
}

function buildChannelFieldsHtml(chName, fields) {
    let html = '';
    fields.forEach(f => {
        const inputId = `ch-${chName}-${f.key}`;
        let inputHtml = '';
        if (f.type === 'bool') {
            const checked = f.value ? 'checked' : '';
            inputHtml = `<label class="relative inline-flex items-center cursor-pointer">
                <input id="${inputId}" type="checkbox" ${checked} class="sr-only peer" data-field="${f.key}" data-ch="${chName}">
                <div class="w-9 h-5 bg-slate-200 dark:bg-slate-700 peer-checked:bg-primary-400 rounded-full
                            after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white
                            after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:after:translate-x-full"></div>
            </label>`;
        } else if (f.type === 'secret') {
            inputHtml = `<input id="${inputId}" type="text" value="${escapeHtml(String(f.value || ''))}"
                data-field="${f.key}" data-ch="${chName}" data-masked="${f.value ? '1' : ''}"
                class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-600
                       bg-slate-50 dark:bg-white/5 text-sm text-slate-800 dark:text-slate-100
                       focus:outline-none focus:border-primary-500 font-mono transition-colors
                       ${f.value ? 'cfg-key-masked' : ''}"
                placeholder="${escapeHtml(f.label)}">`;
        } else {
            const inputType = f.type === 'number' ? 'number' : 'text';
            inputHtml = `<input id="${inputId}" type="${inputType}" value="${escapeHtml(String(f.value ?? f.default ?? ''))}"
                data-field="${f.key}" data-ch="${chName}"
                class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-600
                       bg-slate-50 dark:bg-white/5 text-sm text-slate-800 dark:text-slate-100
                       focus:outline-none focus:border-primary-500 font-mono transition-colors"
                placeholder="${escapeHtml(f.label)}">`;
        }
        html += `<div>
            <label class="block text-sm font-medium text-slate-600 dark:text-slate-400 mb-1.5">${escapeHtml(f.label)}</label>
            ${inputHtml}
        </div>`;
    });
    return html;
}

function bindSecretFieldEvents(container) {
    container.querySelectorAll('input[data-masked="1"]').forEach(inp => {
        inp.addEventListener('focus', function() {
            if (this.dataset.masked === '1') {
                this.value = '';
                this.dataset.masked = '';
                this.classList.remove('cfg-key-masked');
            }
        });
    });
}

function showChannelStatus(chName, msgKey, isError) {
    const el = document.getElementById(`ch-status-${chName}`);
    if (!el) return;
    el.textContent = t(msgKey);
    el.classList.toggle('text-red-500', !!isError);
    el.classList.toggle('text-primary-500', !isError);
    el.classList.remove('opacity-0');
    setTimeout(() => el.classList.add('opacity-0'), 2500);
}

function saveChannelConfig(chName, instanceId) {
    // instanceId keys the DOM (per-instance cards); falls back to the channel
    // name for legacy single-instance cards.
    const iid = instanceId || chName;
    const card = document.getElementById(`channel-card-${iid}`);
    if (!card) return;

    const updates = {};
    card.querySelectorAll('input[data-ch="' + iid + '"]').forEach(inp => {
        const key = inp.dataset.field;
        if (inp.type === 'checkbox') {
            updates[key] = inp.checked;
        } else {
            if (inp.dataset.masked === '1') return;
            updates[key] = inp.value;
        }
    });

    const btn = document.getElementById(`ch-save-${iid}`);
    if (btn) btn.disabled = true;

    fetch('/api/channels', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'save', channel: chName, instance_id: instanceId || '', config: updates })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            showChannelStatus(iid, data.restarted ? 'channels_restarted' : 'channels_saved', false);
        } else {
            showChannelStatus(iid, 'channels_save_error', true);
        }
    })
    .catch(() => showChannelStatus(iid, 'channels_save_error', true))
    .finally(() => { if (btn) btn.disabled = false; });
}

function disconnectChannel(chName, instanceId) {
    const ch = channelsData.find(c => c.name === chName);
    const label = ch ? ((typeof ch.label === 'object') ? (ch.label[currentLang] || ch.label.en) : ch.label) : chName;

    showConfirmDialog({
        title: t('channels_disconnect'),
        message: t('channels_disconnect_confirm'),
        okText: t('channels_disconnect'),
        cancelText: t('channels_cancel'),
        onConfirm: () => {
            fetch('/api/channels', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'disconnect', channel: chName, instance_id: instanceId || '' })
            })
            .then(r => r.json())
            .then(data => {
                if (data.status === 'success') {
                    // An instance removal changes the instances list; reload from
                    // the server so the card set is authoritative. Legacy per-type
                    // disconnect can flip the flag locally.
                    if (instanceId) {
                        loadChannelsView();
                    } else {
                        if (ch) ch.active = false;
                        renderActiveChannels();
                    }
                }
            })
            .catch(() => {});
        }
    });
}

// --- Add channel panel ---
function openAddChannelPanel() {
    const panel = document.getElementById('channels-add-panel');
    // A multi-instance-ready type (feishu) can always be added again — each add
    // creates a new instance. Other types disappear once active.
    const activeNames = new Set(
        channelsData.filter(c => c.active && !isMultiInstanceType(c.name)).map(c => c.name)
    );
    const available = channelsData.filter(c => !activeNames.has(c.name));

    const anyCards = channelRenderList().length > 0;
    const content = document.getElementById('channels-content');
    if (!anyCards && content) content.classList.add('hidden');

    if (available.length === 0) {
        panel.innerHTML = `<div class="bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10 p-6 text-center">
            <p class="text-sm text-slate-500 dark:text-slate-400">${currentLang === 'zh' ? '所有通道均已接入' : 'All channels are already connected'}</p>
            <button onclick="closeAddChannelPanel()" class="mt-3 text-xs text-slate-400 hover:text-slate-600 dark:hover:text-slate-300 cursor-pointer">${t('channels_cancel')}</button>
        </div>`;
        panel.classList.remove('hidden');
        return;
    }

    const ddOptions = [
        { value: '', label: t('channels_select_placeholder') },
        ...available.map(ch => {
            const label = (typeof ch.label === 'object') ? (ch.label[currentLang] || ch.label.en) : ch.label;
            return { value: ch.name, label: `${label} (${ch.name})` };
        })
    ];

    panel.innerHTML = `
        <div class="bg-white dark:bg-[#1A1A1A] rounded-xl border border-primary-200 dark:border-primary-800 p-6">
            <div class="flex items-center gap-3 mb-5">
                <div class="w-9 h-9 rounded-lg bg-primary-50 dark:bg-primary-900/30 flex items-center justify-center">
                    <i class="fas fa-plus text-primary-500 text-sm"></i>
                </div>
                <h3 class="font-semibold text-slate-800 dark:text-slate-100">${t('channels_add')}</h3>
            </div>
            <div class="mb-4">
                <div id="add-channel-select" class="cfg-dropdown" tabindex="0">
                    <div class="cfg-dropdown-selected">
                        <span class="cfg-dropdown-text">--</span>
                        <i class="fas fa-chevron-down cfg-dropdown-arrow"></i>
                    </div>
                    <div class="cfg-dropdown-menu"></div>
                </div>
            </div>
            <div id="add-channel-fields" class="space-y-4"></div>
            <div id="add-channel-actions" class="hidden flex items-center justify-end gap-3 pt-4">
                <button onclick="closeAddChannelPanel()"
                    class="px-4 py-2 rounded-lg border border-slate-200 dark:border-white/10
                           text-slate-600 dark:text-slate-300 text-sm font-medium
                           hover:bg-slate-50 dark:hover:bg-white/5
                           cursor-pointer transition-colors duration-150">${t('channels_cancel')}</button>
                <button id="add-channel-submit" onclick="submitAddChannel()"
                    class="px-4 py-2 rounded-lg bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium
                           cursor-pointer transition-colors duration-150 disabled:opacity-50 disabled:cursor-not-allowed">${t('channels_connect_btn')}</button>
            </div>
        </div>`;
    panel.classList.remove('hidden');
    panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

    const ddEl = document.getElementById('add-channel-select');
    initDropdown(ddEl, ddOptions, '', onAddChannelSelect);
}

function closeAddChannelPanel() {
    stopWeixinQrPoll();
    stopFeishuRegisterPoll();
    const panel = document.getElementById('channels-add-panel');
    if (panel) {
        panel.classList.add('hidden');
        panel.innerHTML = '';
    }
    const content = document.getElementById('channels-content');
    if (content) content.classList.remove('hidden');
}

function onAddChannelSelect(chName) {
    stopWeixinQrPoll();
    stopFeishuRegisterPoll();
    const fieldsContainer = document.getElementById('add-channel-fields');
    const actions = document.getElementById('add-channel-actions');

    if (!chName) {
        fieldsContainer.innerHTML = '';
        actions.classList.add('hidden');
        return;
    }

    if (chName === 'weixin') {
        actions.classList.add('hidden');
        fieldsContainer.innerHTML = `
            <div id="weixin-qr-panel" class="flex flex-col items-center py-4">
                <p class="text-sm text-slate-500 dark:text-slate-400 mb-4">${t('weixin_scan_loading')}</p>
            </div>`;
        startWeixinQrLogin();
        return;
    }

    if (chName === 'wecom_bot') {
        actions.classList.add('hidden');
        const ch = channelsData.find(c => c.name === chName);
        fieldsContainer.innerHTML = buildWecomBotPanel(ch);
        return;
    }

    if (chName === 'feishu') {
        actions.classList.add('hidden');
        const ch = channelsData.find(c => c.name === chName);
        fieldsContainer.innerHTML = buildFeishuPanel(ch);
        return;
    }

    const ch = channelsData.find(c => c.name === chName);
    if (!ch) return;

    fieldsContainer.innerHTML = buildChannelFieldsHtml(chName, ch.fields || []);
    bindSecretFieldEvents(fieldsContainer);
    actions.classList.remove('hidden');
}

function submitAddChannel() {
    const ddEl = document.getElementById('add-channel-select');
    const chName = getDropdownValue(ddEl);
    if (!chName) return;

    const fieldsContainer = document.getElementById('add-channel-fields');
    const updates = {};
    fieldsContainer.querySelectorAll('input[data-ch="' + chName + '"]').forEach(inp => {
        const key = inp.dataset.field;
        if (inp.type === 'checkbox') {
            updates[key] = inp.checked;
        } else {
            if (inp.dataset.masked === '1') return;
            updates[key] = inp.value;
        }
    });

    const btn = document.getElementById('add-channel-submit');
    if (btn) { btn.disabled = true; btn.textContent = t('channels_connecting'); }

    fetch('/api/channels', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'connect', channel: chName, config: updates })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            // A new multi-instance record only shows up by reloading the
            // instances list from the server; legacy per-type add can patch
            // local state and re-render.
            if (isMultiInstanceType(chName) || data.instance_id) {
                loadChannelsView();
                return;
            }
            const ch = channelsData.find(c => c.name === chName);
            if (ch) {
                ch.active = true;
                (ch.fields || []).forEach(f => {
                    if (updates[f.key] !== undefined) {
                        f.value = f.type === 'secret' ? ChannelsHandler_maskSecret(updates[f.key]) : updates[f.key];
                    }
                });
            }
            renderActiveChannels();
        } else {
            if (btn) { btn.disabled = false; btn.textContent = t('channels_connect_btn'); }
        }
    })
    .catch(() => {
        if (btn) { btn.disabled = false; btn.textContent = t('channels_connect_btn'); }
    });
}

// =====================================================================
// WeChat QR Login
// =====================================================================
let _weixinQrPollTimer = null;
let _weixinStatusPollTimer = null;

function stopWeixinStatusPoll() {
    if (_weixinStatusPollTimer) {
        clearTimeout(_weixinStatusPollTimer);
        _weixinStatusPollTimer = null;
    }
}

function startWeixinActiveStatusPoll() {
    stopWeixinStatusPoll();
    _weixinStatusPollTimer = setTimeout(() => {
        fetch('/api/channels').then(r => r.json()).then(data => {
            if (data.status !== 'success') return;
            const wx = (data.channels || []).find(c => c.name === 'weixin');
            if (!wx || !wx.active) return;
            if (wx.login_status === 'logged_in') {
                channelsData = data.channels;
                renderActiveChannels();
            } else {
                const ch = channelsData.find(c => c.name === 'weixin');
                if (ch) ch.login_status = wx.login_status;
                startWeixinActiveStatusPoll();
            }
        }).catch(() => { startWeixinActiveStatusPoll(); });
    }, 3000);
}

function showWeixinActiveQr() {
    const container = document.getElementById('weixin-active-qr');
    if (!container) return;
    container.innerHTML = `
        <div id="weixin-qr-panel" class="flex flex-col items-center py-2">
            <p class="text-sm text-slate-500 dark:text-slate-400 mb-4">${t('weixin_scan_loading')}</p>
        </div>`;
    stopWeixinStatusPoll();
    startWeixinQrLogin();
}

function stopWeixinQrPoll() {
    if (_weixinQrPollTimer) {
        clearTimeout(_weixinQrPollTimer);
        _weixinQrPollTimer = null;
    }
}

function startWeixinQrLogin() {
    stopWeixinQrPoll();
    fetch('/api/weixin/qrlogin')
        .then(r => r.json())
        .then(data => {
            const panel = document.getElementById('weixin-qr-panel');
            if (!panel) return;
            if (data.status !== 'success') {
                panel.innerHTML = `<p class="text-sm text-red-500">${t('weixin_scan_fail')}: ${data.message || ''}</p>`;
                return;
            }
            renderWeixinQr(data.qr_image || data.qrcode_url, 'waiting');
            if (data.source === 'channel') {
                startWeixinActiveStatusPoll();
            } else {
                pollWeixinQrStatus();
            }
        })
        .catch(() => {
            const panel = document.getElementById('weixin-qr-panel');
            if (panel) panel.innerHTML = `<p class="text-sm text-red-500">${t('weixin_scan_fail')}</p>`;
        });
}

function renderWeixinQr(qrcodeUrl, status) {
    const panel = document.getElementById('weixin-qr-panel');
    if (!panel) return;

    let statusText = t('weixin_scan_waiting');
    let statusColor = 'text-slate-500 dark:text-slate-400';
    if (status === 'scanned') {
        statusText = t('weixin_scan_scanned');
        statusColor = 'text-primary-500';
    } else if (status === 'expired') {
        statusText = t('weixin_scan_expired');
        statusColor = 'text-amber-500';
    } else if (status === 'confirmed') {
        statusText = t('weixin_scan_success');
        statusColor = 'text-primary-500';
    }

    panel.innerHTML = `
        <div class="flex flex-col items-center">
            <p class="text-sm font-medium text-slate-700 dark:text-slate-200 mb-1">${t('weixin_scan_title')}</p>
            <p class="text-xs text-slate-400 dark:text-slate-500 mb-4">${t('weixin_scan_desc')}</p>
            <div class="bg-white p-3 rounded-xl shadow-sm border border-slate-100 dark:border-slate-700 mb-3">
                <img src="${escapeHtml(qrcodeUrl)}" alt="QR Code" class="w-52 h-52" style="image-rendering: pixelated;"/>
            </div>
            <p class="text-xs ${statusColor} mb-1">${statusText}</p>
            <p class="text-xs text-slate-400 dark:text-slate-500">${t('weixin_qr_tip')}</p>
        </div>`;
}

function pollWeixinQrStatus() {
    _weixinQrPollTimer = setTimeout(() => {
        fetch('/api/weixin/qrlogin', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'poll' })
        })
        .then(r => r.json())
        .then(data => {
            const panel = document.getElementById('weixin-qr-panel');
            if (!panel) { stopWeixinQrPoll(); return; }

            if (data.status !== 'success') {
                pollWeixinQrStatus();
                return;
            }

            const qrStatus = data.qr_status;
            if (qrStatus === 'confirmed') {
                renderWeixinQr('', 'confirmed');
                panel.innerHTML = `
                    <div class="flex flex-col items-center py-4">
                        <div class="w-12 h-12 rounded-full bg-primary-50 dark:bg-primary-900/30 flex items-center justify-center mb-3">
                            <i class="fas fa-check text-primary-500 text-lg"></i>
                        </div>
                        <p class="text-sm font-medium text-primary-600 dark:text-primary-400">${t('weixin_scan_success')}</p>
                    </div>`;
                connectWeixinAfterQr();
            } else if (qrStatus === 'expired' && (data.qr_image || data.qrcode_url)) {
                renderWeixinQr(data.qr_image || data.qrcode_url, 'waiting');
                pollWeixinQrStatus();
            } else if (qrStatus === 'scaned') {
                const img = panel.querySelector('img');
                const currentSrc = img ? img.src : '';
                renderWeixinQr(currentSrc, 'scanned');
                pollWeixinQrStatus();
            } else {
                pollWeixinQrStatus();
            }
        })
        .catch(() => {
            pollWeixinQrStatus();
        });
    }, 2000);
}

function connectWeixinAfterQr() {
    fetch('/api/channels', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'connect', channel: 'weixin', config: {} })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            // Multi-Agent: the new Weixin instance only lives in the server's
            // channel_instances yet, and its card is rendered from that list —
            // so reload the channels view to make it appear. Re-rendering from
            // the stale local state would drop the freshly scanned card until a
            // manual refresh. Legacy single-instance patches local state.
            if (isMultiInstanceType('weixin') || data.instance_id) {
                setTimeout(() => loadChannelsView(), 1500);
                return;
            }
            const ch = channelsData.find(c => c.name === 'weixin');
            if (ch) ch.active = true;
            setTimeout(() => renderActiveChannels(), 1500);
        }
    })
    .catch(() => {});
}

// =====================================================================
// WeCom Bot QR Auth
// =====================================================================
// NOTE: This is the only remaining external script in the Web Console.
// Tencent's WeCom Bot SDK must be loaded from their official CDN — it
// performs runtime origin/signature checks and will not work if
// self-hosted. The SDK is fetched lazily, only when the user opens the
// "WeCom Bot" channel QR-login flow, so the rest of the console works
// fully offline.
const WECOM_BOT_SDK_URL = 'https://wwcdn.weixin.qq.com/node/wework/js/wecom-aibot-sdk@0.1.0.min.js';
const WECOM_BOT_SOURCE = 'cowagent';
let _wecomSdkLoaded = false;

function ensureWecomSdkLoaded() {
    return new Promise((resolve, reject) => {
        if (_wecomSdkLoaded && window.WecomAIBotSDK) { resolve(); return; }
        if (document.querySelector(`script[src="${WECOM_BOT_SDK_URL}"]`)) {
            _wecomSdkLoaded = true; resolve(); return;
        }
        const s = document.createElement('script');
        s.src = WECOM_BOT_SDK_URL;
        s.onload = () => { _wecomSdkLoaded = true; resolve(); };
        s.onerror = () => reject(new Error('Failed to load WecomAIBotSDK'));
        document.head.appendChild(s);
    });
}

function _wecomBotHasCreds(ch) {
    if (!ch || !ch.fields) return false;
    const idField = ch.fields.find(f => f.key === 'wecom_bot_id');
    const secretField = ch.fields.find(f => f.key === 'wecom_bot_secret');
    return !!(idField && idField.value && secretField && secretField.value);
}

function buildWecomBotPanel(ch) {
    const scanLabel = t('wecom_mode_scan');
    const manualLabel = t('wecom_mode_manual');
    const hasCreds = _wecomBotHasCreds(ch);
    const defaultMode = hasCreds ? 'manual' : 'scan';
    return `
        <div id="wecom-bot-panel" data-default-mode="${defaultMode}">
            <div class="flex items-center justify-center gap-1 mb-5 bg-slate-100 dark:bg-white/5 rounded-lg p-1">
                <button id="wecom-tab-scan" onclick="switchWecomBotMode('scan')"
                    class="flex-1 px-3 py-1.5 rounded-md text-xs font-medium transition-colors
                           bg-white dark:bg-slate-700 text-slate-800 dark:text-slate-100 shadow-sm">
                    ${scanLabel}
                </button>
                <button id="wecom-tab-manual" onclick="switchWecomBotMode('manual')"
                    class="flex-1 px-3 py-1.5 rounded-md text-xs font-medium transition-colors
                           text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200">
                    ${manualLabel}
                </button>
            </div>
            <div id="wecom-mode-content"></div>
        </div>`;
}

function switchWecomBotMode(mode) {
    const scanTab = document.getElementById('wecom-tab-scan');
    const manualTab = document.getElementById('wecom-tab-manual');
    const content = document.getElementById('wecom-mode-content');
    const actions = document.getElementById('add-channel-actions');
    if (!scanTab || !manualTab || !content) return;

    const activeClasses = 'bg-white dark:bg-slate-700 text-slate-800 dark:text-slate-100 shadow-sm';
    const inactiveClasses = 'text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200';

    if (mode === 'scan') {
        scanTab.className = scanTab.className.replace(/text-slate-500[^\s]*/g, '').replace(/hover:\S+/g, '');
        scanTab.className = `flex-1 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${activeClasses}`;
        manualTab.className = `flex-1 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${inactiveClasses}`;
        actions.classList.add('hidden');
        content.innerHTML = `
            <div class="flex flex-col items-center py-4">
                <p class="text-sm text-slate-600 dark:text-slate-300 mb-2">${t('wecom_scan_desc')}</p>
                <button onclick="startWecomBotAuth()"
                    class="mt-3 px-6 py-2.5 rounded-lg bg-emerald-500 hover:bg-emerald-600 text-white text-sm font-medium
                           cursor-pointer transition-colors duration-150">
                    <i class="fas fa-qrcode mr-2"></i>${t('wecom_scan_btn')}
                </button>
                <div id="wecom-scan-status" class="mt-3"></div>
            </div>`;
    } else {
        manualTab.className = `flex-1 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${activeClasses}`;
        scanTab.className = `flex-1 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${inactiveClasses}`;
        const ch = channelsData.find(c => c.name === 'wecom_bot');
        content.innerHTML = `<div class="space-y-4">${buildChannelFieldsHtml('wecom_bot', ch ? ch.fields || [] : [])}</div>`;
        bindSecretFieldEvents(content);
        actions.classList.remove('hidden');
    }
}

function startWecomBotAuth() {
    const statusEl = document.getElementById('wecom-scan-status');
    ensureWecomSdkLoaded().then(() => {
        WecomAIBotSDK.openBotInfoAuthWindow({
            source: WECOM_BOT_SOURCE,
            onCreated: function(bot) {
                if (statusEl) {
                    statusEl.innerHTML = `
                        <div class="flex flex-col items-center py-2">
                            <div class="w-10 h-10 rounded-full bg-emerald-50 dark:bg-emerald-900/30 flex items-center justify-center mb-2">
                                <i class="fas fa-check text-emerald-500 text-lg"></i>
                            </div>
                            <p class="text-sm font-medium text-emerald-600 dark:text-emerald-400">${t('wecom_scan_success')}</p>
                        </div>`;
                }
                connectWecomBotAfterAuth(bot.botid, bot.secret);
            },
            onError: function(err) {
                if (statusEl) {
                    statusEl.innerHTML = `<p class="text-sm text-red-500">${t('wecom_scan_fail')}: ${err.message || err.code || ''}</p>`;
                }
            }
        });
    }).catch(err => {
        if (statusEl) {
            statusEl.innerHTML = `<p class="text-sm text-red-500">SDK load failed: ${err.message}</p>`;
        }
    });
}

function connectWecomBotAfterAuth(botId, secret) {
    fetch('/api/channels', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            action: 'connect',
            channel: 'wecom_bot',
            config: { wecom_bot_id: botId, wecom_bot_secret: secret }
        })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            const ch = channelsData.find(c => c.name === 'wecom_bot');
            if (ch) {
                ch.active = true;
                (ch.fields || []).forEach(f => {
                    if (f.key === 'wecom_bot_id') f.value = botId;
                    if (f.key === 'wecom_bot_secret') f.value = ChannelsHandler_maskSecret(secret);
                });
            }
            setTimeout(() => renderActiveChannels(), 1500);
        }
    })
    .catch(() => {});
}

function startWecomBotAuthInCard() {
    const statusEl = document.getElementById('wecom-card-scan-status');
    ensureWecomSdkLoaded().then(() => {
        WecomAIBotSDK.openBotInfoAuthWindow({
            source: WECOM_BOT_SOURCE,
            onCreated: function(bot) {
                if (statusEl) {
                    statusEl.innerHTML = `
                        <div class="flex flex-col items-center py-2">
                            <div class="w-10 h-10 rounded-full bg-emerald-50 dark:bg-emerald-900/30 flex items-center justify-center mb-2">
                                <i class="fas fa-check text-emerald-500 text-lg"></i>
                            </div>
                            <p class="text-sm font-medium text-emerald-600 dark:text-emerald-400">${t('wecom_scan_success')}</p>
                        </div>`;
                }
                connectWecomBotAfterAuth(bot.botid, bot.secret);
            },
            onError: function(err) {
                if (statusEl) {
                    statusEl.innerHTML = `<p class="text-sm text-red-500">${t('wecom_scan_fail')}: ${err.message || err.code || ''}</p>`;
                }
            }
        });
    }).catch(err => {
        if (statusEl) {
            statusEl.innerHTML = `<p class="text-sm text-red-500">SDK load failed: ${err.message}</p>`;
        }
    });
}

// Initialize wecom bot panel with correct default mode when inserted into DOM
document.addEventListener('DOMContentLoaded', function() {
    const observer = new MutationObserver(function() {
        const wecomPanel = document.getElementById('wecom-bot-panel');
        if (wecomPanel && !wecomPanel.dataset.initialized) {
            wecomPanel.dataset.initialized = '1';
            switchWecomBotMode(wecomPanel.dataset.defaultMode || 'scan');
        }
        // Init every feishu panel on screen, not just the first: multiple
        // instance cards can be present at once, each with its own id suffix.
        document.querySelectorAll('.feishu-panel').forEach(feishuPanel => {
            if (feishuPanel.dataset.initialized) return;
            feishuPanel.dataset.initialized = '1';
            switchFeishuMode(feishuPanel.dataset.iid || 'feishu', feishuPanel.dataset.defaultMode || 'scan');
        });
    });
    observer.observe(document.body, { childList: true, subtree: true });
});

// =====================================================================
// Feishu One-click App Registration (lark-oapi register_app)
// =====================================================================
let _feishuRegisterPollTimer = null;
// The server binds a register session to this browser's identity and hands back
// an opaque handle; every poll must present it. The server deliberately has no
// "whoever asks first" fallback, so a poll without the handle cannot address a
// session at all. Cleared as soon as the session reaches a terminal state or
// the user leaves the scan tab.
let _feishuRegisterHandle = '';
// Set when the currently-running scan was started from a tenant channel form
// (the instance id), empty when it was started from the platform page. Decides
// whether a "done" result pre-fills the tenant draft or connects the platform
// channel.
let _feishuScanTarget = '';

function _feishuHasCreds(ch) {
    if (!ch || !ch.fields) return false;
    const idField = ch.fields.find(f => f.key === 'feishu_app_id');
    const secretField = ch.fields.find(f => f.key === 'feishu_app_secret');
    return !!(idField && idField.value && secretField && secretField.value);
}

function buildFeishuPanel(ch, isActive) {
    const scanLabel = t('feishu_mode_scan');
    const manualLabel = t('feishu_mode_manual');
    // 已有凭据时默认进入手动 Tab，方便修改；否则推荐扫码
    const defaultMode = _feishuHasCreds(ch) ? 'manual' : 'scan';
    const activeAttr = isActive ? 'data-active="1"' : '';
    // Every DOM id in the panel is suffixed with the instance id so two feishu
    // cards on screen at once never collide: without this, getElementById()
    // always resolves to the first card, so the second card is dead and its tab
    // clicks drive the first one. The Add panel (no instance yet) uses the bare
    // "feishu" suffix; an active instance card uses its real instance id.
    const iid = (isActive && ch && ch.iid) ? ch.iid : 'feishu';
    return `
        <div id="feishu-panel-${iid}" class="feishu-panel" data-default-mode="${defaultMode}" data-iid="${escapeHtml(iid)}" ${activeAttr}>
            <div class="flex items-center justify-center gap-1 mb-5 bg-slate-100 dark:bg-white/5 rounded-lg p-1">
                <button id="feishu-tab-scan-${iid}" onclick="switchFeishuMode('${iid}', 'scan')"
                    class="flex-1 px-3 py-1.5 rounded-md text-xs font-medium transition-colors
                           bg-white dark:bg-slate-700 text-slate-800 dark:text-slate-100 shadow-sm">
                    ${scanLabel}
                </button>
                <button id="feishu-tab-manual-${iid}" onclick="switchFeishuMode('${iid}', 'manual')"
                    class="flex-1 px-3 py-1.5 rounded-md text-xs font-medium transition-colors
                           text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200">
                    ${manualLabel}
                </button>
            </div>
            <div id="feishu-mode-content-${iid}"></div>
        </div>`;
}

function switchFeishuMode(iid, mode) {
    // Back-compat: old call sites passed only the mode. Treat a bare mode as the
    // Add panel's "feishu" instance.
    if (mode === undefined && (iid === 'scan' || iid === 'manual')) {
        mode = iid;
        iid = 'feishu';
    }
    iid = iid || 'feishu';
    const panel = document.getElementById(`feishu-panel-${iid}`);
    const scanTab = document.getElementById(`feishu-tab-scan-${iid}`);
    const manualTab = document.getElementById(`feishu-tab-manual-${iid}`);
    const content = document.getElementById(`feishu-mode-content-${iid}`);
    if (!scanTab || !manualTab || !content) return;

    // 已激活通道卡片中嵌入此 panel 时，没有 add-channel-actions（保存按钮就近渲染）
    const isActive = panel && panel.dataset.active === '1';
    const actions = isActive ? null : document.getElementById('add-channel-actions');
    const scanStatusId = `feishu-scan-status-${iid}`;

    const activeClasses = 'bg-white dark:bg-slate-700 text-slate-800 dark:text-slate-100 shadow-sm';
    const inactiveClasses = 'text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200';

    stopFeishuRegisterPoll();
    // Leaving the scan tab abandons the session; drop the handle so a later
    // poll cannot address it (the server expires it on its own schedule).
    _feishuRegisterHandle = '';

    if (mode === 'scan') {
        scanTab.className = `flex-1 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${activeClasses}`;
        manualTab.className = `flex-1 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${inactiveClasses}`;
        if (actions) actions.classList.add('hidden');
        // active 卡片下扫码替换的提示文案，强调"创建新机器人会覆盖现有配置"
        const desc = isActive
            ? t('feishu_scan_replace_desc')
            : t('feishu_scan_desc');
        content.innerHTML = `
            <div class="flex flex-col items-center py-4">
                <p class="text-sm text-slate-600 dark:text-slate-300 mb-3 text-center">${desc}</p>
                <button onclick="startFeishuRegister('${scanStatusId}')"
                    class="mt-2 px-6 py-2.5 rounded-lg bg-emerald-500 hover:bg-emerald-600 text-white text-sm font-medium
                           cursor-pointer transition-colors duration-150">
                    <i class="fas fa-qrcode mr-2"></i>${t('feishu_scan_btn')}
                </button>
                <div id="${scanStatusId}" class="mt-4 w-full"></div>
            </div>`;
    } else {
        manualTab.className = `flex-1 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${activeClasses}`;
        scanTab.className = `flex-1 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${inactiveClasses}`;
        // An active instance card keys its fields by the instance id (so the
        // card's data-ch query and the save target line up); the Add panel keys
        // by the bare type since no instance exists yet.
        const ch = (isActive && iid !== 'feishu')
            ? channelInstancesView.find(c => c.instance_id === iid)
            : channelsData.find(c => c.name === 'feishu');
        const fieldsHtml = buildChannelFieldsHtml(iid, ch ? ch.fields || [] : []);
        if (isActive) {
            // 已接入卡片：内置保存按钮，复用 saveChannelConfig 走 update 流程
            content.innerHTML = `
                <div class="space-y-4">
                    ${fieldsHtml}
                    <div class="flex items-center justify-end gap-3 pt-1">
                        <span id="ch-status-${iid}" class="text-xs text-primary-500 opacity-0 transition-opacity duration-300"></span>
                        <button onclick="saveChannelConfig('feishu', '${iid === 'feishu' ? '' : iid}')"
                            class="px-4 py-2 rounded-lg bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium
                                   cursor-pointer transition-colors duration-150 disabled:opacity-50 disabled:cursor-not-allowed"
                            id="ch-save-${iid}">${t('channels_save')}</button>
                    </div>
                </div>`;
        } else {
            content.innerHTML = `<div class="space-y-4">${fieldsHtml}</div>`;
            if (actions) actions.classList.remove('hidden');
        }
        bindSecretFieldEvents(content);
    }
}

function stopFeishuRegisterPoll() {
    if (_feishuRegisterPollTimer) {
        clearTimeout(_feishuRegisterPollTimer);
        _feishuRegisterPollTimer = null;
    }
}

function startFeishuRegister(targetStatusId, forInstanceId) {
    const statusId = targetStatusId || 'feishu-scan-status';
    // When a tenant form started this scan, the result must pre-fill that form
    // rather than connect the platform-level channel.
    _feishuScanTarget = forInstanceId || '';
    const statusEl = document.getElementById(statusId);
    if (statusEl) {
        statusEl.innerHTML = `<p class="text-sm text-slate-500 dark:text-slate-400 text-center">${t('feishu_scan_loading')}</p>`;
    }
    stopFeishuRegisterPoll();
    _feishuRegisterHandle = '';
    fetch('/api/feishu/register')
        .then(r => r.json().then(data => ({ httpStatus: r.status, data })))
        .then(({ httpStatus, data }) => {
            if (!data || data.status !== 'success') {
                renderFeishuRegisterError(statusId, scanFailureText(
                    httpStatus, data && data.code, data && data.message));
                return;
            }
            _feishuRegisterHandle = data.handle || '';
            if (!_feishuRegisterHandle) {
                // A session we cannot address must not be polled: without the
                // handle every poll would read as "expired" forever.
                renderFeishuRegisterError(statusId, t('feishu_scan_fail'));
                return;
            }
            if (data.register_status === 'downloading') {
                // Desktop first run: the SDK bundle lands before the QR exists.
                renderFeishuSdkDownloading(statusId);
            } else {
                renderFeishuQr(statusId, data.qr_image, data.qrcode_url);
            }
            pollFeishuRegisterStatus(statusId);
        })
        .catch(err => {
            renderFeishuRegisterError(statusId, err.message || t('feishu_scan_fail'));
        });
}

function renderFeishuQr(statusId, qrImage, qrUrl) {
    const statusEl = document.getElementById(statusId);
    if (!statusEl) return;
    const imgHtml = qrImage
        ? `<img src="${qrImage}" alt="QR" class="w-44 h-44 rounded-lg border border-slate-200 dark:border-white/10 bg-white p-2"/>`
        : `<div class="w-44 h-44 rounded-lg border border-dashed border-slate-300 flex items-center justify-center text-xs text-slate-400">QR</div>`;
    statusEl.innerHTML = `
        <div class="flex flex-col items-center gap-3">
            ${imgHtml}
            <p class="text-xs text-amber-500">${t('feishu_scan_waiting')}</p>
            <p class="text-xs text-slate-400 dark:text-slate-500">${t('feishu_scan_tip')}</p>
            ${qrUrl ? `<a href="${qrUrl}" target="_blank" rel="noopener"
                class="text-xs text-blue-500 hover:text-blue-600 underline">${t('feishu_scan_open_link')}</a>` : ''}
        </div>`;
}

function renderFeishuSdkDownloading(statusId) {
    const statusEl = document.getElementById(statusId);
    if (!statusEl) return;
    statusEl.innerHTML = `
        <div class="flex flex-col items-center gap-2 py-6">
            <i class="fas fa-spinner fa-spin text-slate-400"></i>
            <p class="text-sm text-slate-500 dark:text-slate-400">${t('feishu_sdk_downloading')}</p>
            <p class="text-xs text-slate-400 dark:text-slate-500">${t('feishu_sdk_downloading_tip')}</p>
        </div>`;
}

function renderFeishuRegisterError(statusId, message) {
    const statusEl = document.getElementById(statusId);
    if (!statusEl) return;
    statusEl.innerHTML = `
        <div class="flex flex-col items-center gap-2 py-2">
            <p class="text-sm text-red-500 text-center">${message}</p>
            <button onclick="startFeishuRegister('${statusId}')"
                class="mt-1 px-4 py-1.5 rounded-md text-xs font-medium
                       bg-slate-100 dark:bg-white/10 text-slate-700 dark:text-slate-200
                       hover:bg-slate-200 dark:hover:bg-white/20 cursor-pointer">
                <i class="fas fa-rotate-right mr-1"></i>${t('feishu_scan_retry')}
            </button>
        </div>`;
}

function pollFeishuRegisterStatus(statusId) {
    stopFeishuRegisterPoll();
    if (!_feishuRegisterHandle) {
        // No live session to poll (never started, or already terminal).
        return;
    }
    const handle = _feishuRegisterHandle;
    _feishuRegisterPollTimer = setTimeout(() => {
        fetch('/api/feishu/register', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'poll', handle: handle })
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
                            ${_feishuScanTarget ? `<p class="text-xs text-slate-400 dark:text-slate-500 mt-1">${t('tenant_channel_scan_autosaved')}</p>` : ''}
                        </div>`;
                }
                if (_feishuScanTarget) {
                    // A tenant scan persists itself: the grant the server minted
                    // for this scan authorizes the create, so there is no second
                    // step and no password prompt to lose the channel to.
                    applyFeishuScanToTenantForm(data.app_id, data.app_secret, data.scan_ticket);
                    autoPersistScannedTenantChannel();
                } else {
                    connectFeishuAfterRegister(data.app_id, data.app_secret);
                }
                _feishuRegisterHandle = '';
                _feishuScanTarget = '';
            } else if (rs === 'expired') {
                _feishuRegisterHandle = '';
                renderFeishuRegisterError(statusId, t('feishu_scan_expired'));
            } else if (rs === 'denied') {
                _feishuRegisterHandle = '';
                renderFeishuRegisterError(statusId, t('feishu_scan_denied'));
            } else if (rs === 'error') {
                _feishuRegisterHandle = '';
                renderFeishuRegisterError(statusId, data.message || t('feishu_scan_fail'));
            } else {
                pollFeishuRegisterStatus(statusId);
            }
        })
        .catch(() => {
            pollFeishuRegisterStatus(statusId);
        });
    }, 2000);
}

function connectFeishuAfterRegister(appId, appSecret) {
    fetch('/api/channels', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            action: 'connect',
            channel: 'feishu',
            config: { feishu_app_id: appId, feishu_app_secret: appSecret }
        })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            // Multi-Agent mode created a new feishu instance server-side; reload
            // so its card appears. Legacy mode patches local state.
            if (isMultiInstanceType('feishu') || data.instance_id) {
                setTimeout(() => loadChannelsView(), 1500);
                return;
            }
            const ch = channelsData.find(c => c.name === 'feishu');
            if (ch) {
                ch.active = true;
                (ch.fields || []).forEach(f => {
                    if (f.key === 'feishu_app_id') f.value = appId;
                    if (f.key === 'feishu_app_secret') f.value = ChannelsHandler_maskSecret(appSecret);
                });
            }
            setTimeout(() => renderActiveChannels(), 1500);
        }
    })
    .catch(() => {});
}

// =====================================================================
// Context Usage View
// =====================================================================
// Session-scoped context usage + manual compaction (change
// integrate-upstream-core-capabilities, P2). The module owns the rendering and
// the request shape; this is only the seam: where it mounts, which identity it
// reads, and how its request travels the console's authorization path.
//
// `_contextNewSession` separates "a session the server has never written" from
// one that exists. A fresh chat has no context row to read, so the entry stays
// quiet until the first turn persists it (implementation.md §4: 首次落库前不轮询).
let _contextModuleHandle = null;
let _contextNewSession = false;

// The current session's durability, read at call time rather than captured:
// switching while a read is in flight is exactly what the module's
// capture/isCurrent check has to notice.
function _contextSession() {
    // The welcome screen is the console's own mark for "nothing persisted yet";
    // a switch to an existing session clears it before loadHistory paints.
    const fresh = _contextNewSession || !!document.getElementById('welcome-screen');
    return {
        agentId: activeAgentId || '',
        sessionId: sessionId || '',
        persisted: !!sessionId && !fresh,
    };
}

// The module's own request seam. It resolves the parsed body and *rejects* on a
// non-success envelope, because the context handlers answer a refusal as a real
// HTTP status carrying `{status:'error', code}` -- so "it resolved" must mean
// "it succeeded", and the server's code survives into the error the module maps.
function _contextRequest(path, options) {
    const opts = Object.assign({ credentials: 'same-origin', cache: 'no-store' },
                               options || {});
    if (opts.body !== undefined && typeof opts.body !== 'string') {
        opts.headers = Object.assign({ 'Content-Type': 'application/json' },
                                     opts.headers || {});
        opts.body = JSON.stringify(opts.body);
    }
    return fetch(path, opts).then(resp => resp.json().catch(() => ({})).then(data => {
        if (!resp.ok || data.status !== 'success') {
            const error = new Error(data.message || data.code || ('HTTP ' + resp.status));
            error.code = data.code || ('http_' + resp.status);
            error.status = resp.status;
            throw error;
        }
        return data;
    }));
}

// Mount (or re-mount) the entry into the composer's host. Re-mounting on every
// identity move is deliberate: it drops the previous session's in-flight
// request and open panel instead of letting one repaint the other.
function mountContextModule() {
    const host = document.getElementById('context-usage-host');
    if (!host) return;
    const module = window.RdaiFunctionalContext;
    if (!module || typeof module.mount !== 'function') return;
    if (_contextModuleHandle) {
        _contextModuleHandle.dispose();
        _contextModuleHandle = null;
    }
    host.innerHTML = '';
    // Either action opens the entry: the read and the write are separately
    // gated, so a deployment may offer usage without compaction. Both closed
    // hides the entry entirely and issues nothing.
    const anyOpen = _featureAvailable('session_context.usage')
        || _featureAvailable('session_context.compact');
    host.classList.toggle('hidden', !anyOpen);
    if (!anyOpen) return;
    try {
        _contextModuleHandle = module.mount({
            root: host,
            // Getters, not values: the projection is replaced (not mutated) when
            // the tenant changes, so a captured Object would keep answering from
            // the previous tenant's capabilities.
            getContext: _baseAuthContext,
            getContextRevision: () => _authContextSeq,
            getSession: _contextSession,
            request: _contextRequest,
            t: t,
        });
    } catch (err) {
        console.warn('[Context] console module unavailable', err);
        _contextModuleHandle = null;
    }
}

// Called after any identity/tenant/session/agent move. A previously mounted
// panel is disposed here so a late reply for the old screen cannot repaint the
// new one, and the fresh mount re-reads the current projection.
function _contextAfterSessionChange(persisted) {
    _contextNewSession = !persisted;
    mountContextModule();
}

function disposeContextModule() {
    if (_contextModuleHandle) {
        _contextModuleHandle.dispose();
        _contextModuleHandle = null;
    }
    _contextNewSession = false;
}

// =====================================================================
// Logs View
// =====================================================================
let logEventSource = null;

function logLevelClass(line) {
    if (/\[CRITICAL\]/.test(line)) return 'log-line-critical';
    if (/\[ERROR\]/.test(line))    return 'log-line-error';
    if (/\[WARNING\]/.test(line))  return 'log-line-warning';
    if (/\[INFO\]/.test(line))     return 'log-line-info';
    if (/\[DEBUG\]/.test(line))    return 'log-line-debug';
    return '';
}

function getHiddenLevels() {
    const hidden = new Set();
    document.querySelectorAll('.log-filter-cb').forEach(function(cb) {
        if (!cb.checked) hidden.add('log-line-' + cb.dataset.level);
    });
    return hidden;
}

function applyLogFilter() {
    const hidden = getHiddenLevels();
    document.querySelectorAll('#log-output .log-line').forEach(function(span) {
        const level = span.classList[1] || '';
        span.style.display = hidden.has(level) ? 'none' : '';
    });
}

function appendLogLines(output, text) {
    const hidden = getHiddenLevels();
    let lastLevelClass = '';
    const lines = text.split('\n');
    lines.forEach(function(line, i) {
        if (i === lines.length - 1 && line === '') return;
        const span = document.createElement('span');
        const levelClass = logLevelClass(line) || lastLevelClass;
        if (logLevelClass(line)) lastLevelClass = levelClass;
        span.className = 'log-line ' + levelClass;
        span.textContent = line + '\n';
        if (hidden.has(levelClass)) span.style.display = 'none';
        output.appendChild(span);
    });
}

document.addEventListener('change', function(e) {
    if (e.target.classList.contains('log-filter-cb')) applyLogFilter();
});

function startLogStream() {
    if (logEventSource) return;
    const output = document.getElementById('log-output');
    output.innerHTML = '';

    logEventSource = new EventSource('/api/logs');
    logEventSource.onmessage = function(e) {
        let item;
        try { item = JSON.parse(e.data); } catch (_) { return; }

        if (item.type === 'init') {
            output.innerHTML = '';
            appendLogLines(output, item.content || '');
            output.scrollTop = output.scrollHeight;
        } else if (item.type === 'line') {
            appendLogLines(output, item.content);
            output.scrollTop = output.scrollHeight;
        } else if (item.type === 'error') {
            output.textContent = item.message || 'Error loading logs';
        }
    };
    logEventSource.onerror = function() {
        logEventSource.close();
        logEventSource = null;
    };
}

function stopLogStream() {
    if (logEventSource) {
        logEventSource.close();
        logEventSource = null;
    }
}

// =====================================================================
// View Navigation Hook
// =====================================================================
const _origNavigateTo = navigateTo;
navigateTo = function(viewId) {
    // Previously-visible but not-yet-enabled targets (menu placeholders) are
    // routed by the base handler to a clear "not available" view instead of a
    // silent no-op. Do not early-return here.

    // An open document editor is about to be replaced by another view, which
    // would drop the edit with nothing on screen to say so.
    if (!docGuardUnsaved(() => navigateTo(viewId))) return;

    // Stop log stream when leaving logs view
    if (currentView === 'logs' && viewId !== 'logs') stopLogStream();

    _origNavigateTo(viewId);

    // Lazy-load view data
    if (viewId === 'config') { enterConfigView(); }
    else if (viewId === 'skills') { resetSkillViewer(); loadSkillsView(); }
    else if (viewId === 'memory') {
        memoryEditor.forget();
        document.getElementById('memory-panel-viewer').classList.add('hidden');
        document.getElementById('memory-panel-list').classList.remove('hidden');
        // Keep the last viewed Agent across refreshes, but drop it if that
        // Agent has since been deleted so we don't point at a ghost.
        if (memoryAgentId && memoryAgentId !== MEMORY_PERSONAL
                && agentCatalog.length && !agentCatalog.some(a => a.id === memoryAgentId)) {
            memoryAgentId = '';
            removeScopedPreference('cow_memory_agent');
        }
        if (!memoryAgentId) memoryAgentId = activeAgentId || defaultAgentId;
        renderMemoryAgentSelect();
        switchMemoryTab('files');
    }
    else if (viewId === 'knowledge') loadKnowledgeView();
    else if (viewId === 'channels') loadChannelsView();
    else if (viewId === 'tasks') loadTasksView();
    // `todo` is a fork view and loads through the registry in the base
    // navigateTo (task 8.6), so it is not hard-coded here anymore.
    else if (viewId === 'logs') startLogStream();
};

// =====================================================================
// Knowledge View
// =====================================================================
let _knowledgeTreeData = [];
let _knowledgeRootFiles = [];
let _knowledgeCurrentFile = null;
let _knowledgeGraphLoaded = false;
const KNOWLEDGE_IMPORT_MAX_FILES = 100;
const KNOWLEDGE_IMPORT_MAX_FILE_SIZE = 10 * 1024 * 1024;
const KNOWLEDGE_IMPORT_MAX_TOTAL_SIZE = 200 * 1024 * 1024;

// Which Agent's knowledge base the page is viewing. Persisted like the memory
// page's selector so a refresh keeps the last choice. An Agent on "shared" mode
// resolves to the shared base on the backend, so this simply scopes the view.
let knowledgeAgentId = readScopedPreference('cow_knowledge_agent') || '';

function viewingKnowledgeAgentId() {
    return knowledgeAgentId || activeAgentId || defaultAgentId;
}

// Append the viewed Agent to a knowledge URL. The global fetch wrapper only
// injects activeAgentId when no agent_id is present, so an explicit one wins.
function _kbUrl(path) {
    const joiner = path.includes('?') ? '&' : '?';
    return `${path}${joiner}agent_id=${encodeURIComponent(viewingKnowledgeAgentId())}`;
}

function renderKnowledgeAgentSelect() {
    const el = document.getElementById('knowledge-agent-select');
    if (!el) return;
    const current = viewingKnowledgeAgentId();
    const list = agentCatalog.length ? agentCatalog : enabledAgents();
    const options = list.map(a => ({ value: a.id, label: a.name || a.id, agent: a }));
    initDropdown(el, options, current, (value) => selectKnowledgeAgent(value), { withAvatar: true });
}

function selectKnowledgeAgent(agentId) {
    knowledgeAgentId = agentId;
    writeScopedPreference('cow_knowledge_agent', agentId);
    loadKnowledgeView();
}

// Knowledge writes (create/rename/delete/move/import) are authorized by the
// selected Agent's data root and the caller's relation to it, which the server
// projects as ``can_write_knowledge`` on every Agent (mirrors the server's
// ``_knowledge_write_authorized``). A missing projection means "capability
// unknown", NOT "denied": degrade to the admin qualification the write path
// always honours, so a slow /api/agents never hides a management entry. The
// server remains authoritative and still refuses a direct request.
function canWriteKnowledge() {
    const agent = findAgent(viewingKnowledgeAgentId());
    if (agent && typeof agent.can_write_knowledge === 'boolean') {
        return agent.can_write_knowledge;
    }
    const ctx = _baseAuthContext();
    if (!ctx) return true;
    if (ctx.authorization_mode === 'all') return true;
    return ctx.is_tenant_admin === true;
}

function renderKnowledgeWriteAffordances() {
    const menu = document.getElementById('knowledge-new-menu');
    if (menu) menu.classList.toggle('hidden', !canWriteKnowledge());
}

// Replace the hardcoded "加载知识库中..." placeholder when the read call answers
// anything but success. Before this, the handler-less closed consumer left the
// page spinning forever with no explanation (the same defect /api/channels had).
function renderKnowledgeUnavailable(data) {
    const emptyEl = document.getElementById('knowledge-empty');
    const docsPanel = document.getElementById('knowledge-panel-docs');
    const statsEl = document.getElementById('knowledge-stats');
    if (statsEl) statsEl.textContent = '';
    if (docsPanel) docsPanel.classList.add('hidden');
    if (!emptyEl) return;
    const code = String((data && (data.code || data.message)) || '');
    const key = (code === 'forbidden' || code === 'knowledge_write_required'
        || /forbidden/i.test(code)) ? 'knowledge_forbidden' : 'knowledge_unavailable';
    const lines = emptyEl.querySelectorAll('p');
    if (lines[0]) lines[0].textContent = t(key);
    if (lines[1]) lines[1].textContent = (data && data.message) ? String(data.message) : '';
    const guideEl = document.getElementById('knowledge-empty-guide');
    if (guideEl) guideEl.classList.add('hidden');
    emptyEl.classList.remove('hidden');
}

function loadKnowledgeView(targetPath) {
    // Reset to docs tab
    switchKnowledgeTab('docs');
    _knowledgeGraphLoaded = false;
    _knowledgeCurrentFile = null;

    // Drop a deleted Agent selection so we never point at a ghost.
    if (knowledgeAgentId && agentCatalog.length && !agentCatalog.some(a => a.id === knowledgeAgentId)) {
        knowledgeAgentId = '';
        removeScopedPreference('cow_knowledge_agent');
    }
    renderKnowledgeAgentSelect();
    renderKnowledgeWriteAffordances();

    fetch(_kbUrl('/api/knowledge/list')).then(r => r.json()).then(data => {
        if (data.status !== 'success') {
            renderKnowledgeUnavailable(data);
            return;
        }
        initKnowledgeImportDropZone();

        const emptyEl = document.getElementById('knowledge-empty');
        const docsPanel = document.getElementById('knowledge-panel-docs');
        const statsEl = document.getElementById('knowledge-stats');

        const tree = data.tree || [];
        const rootFiles = data.root_files || [];
        _knowledgeTreeData = tree;
        _knowledgeRootFiles = rootFiles;
        const stats = data.stats || {};
        const totalPages = stats.pages || 0;
        const sizeStr = stats.size < 1024 ? stats.size + ' B' : (stats.size / 1024).toFixed(1) + ' KB';

        statsEl.textContent = totalPages + ' pages · ' + sizeStr;

        if (totalPages === 0 && tree.length === 0 && rootFiles.length === 0) {
            emptyEl.querySelector('p').textContent = t('knowledge_empty_hint');
            const guideEl = document.getElementById('knowledge-empty-guide');
            if (guideEl) guideEl.classList.remove('hidden');
            emptyEl.classList.remove('hidden');
            docsPanel.classList.add('hidden');
            return;
        }
        emptyEl.classList.add('hidden');
        docsPanel.classList.remove('hidden');

        renderKnowledgeTree(tree, rootFiles);

        // Prefer opening the just created/imported file; ensure its group is
        // expanded so the active item is visible in the tree.
        const targetTitle = targetPath ? _findKnowledgeFileTitle(targetPath) : null;
        if (targetTitle !== null) {
            _expandKnowledgeGroupFor(targetPath);
            openKnowledgeFile(targetPath, targetTitle);
            return;
        }

        // Auto-select the first file (desktop only)
        if (window.innerWidth >= 768) {
            const firstFile = rootFiles.length > 0 ? rootFiles[0] : null;
            const firstGroup = !firstFile ? tree.find(g => g.files && g.files.length > 0) : null;
            if (firstFile) {
                openKnowledgeFile(firstFile.name, firstFile.title);
            } else if (firstGroup) {
                const gf = firstGroup.files[0];
                openKnowledgeFile(firstGroup.dir + '/' + gf.name, gf.title);
            }
        } else {
            document.getElementById('knowledge-content-placeholder').classList.add('hidden');
            document.getElementById('knowledge-content-viewer').classList.add('hidden');
        }
    }).catch(() => {});
}

// Find a file's display title by its relative path within the knowledge tree.
// Returns the title, or null when the path is not present.
function _findKnowledgeFileTitle(path) {
    if (!path) return null;
    const rootHit = (_knowledgeRootFiles || []).find(f => f.name === path);
    if (rootHit) return rootHit.title || rootHit.name;
    const walk = (groups, parentPath) => {
        for (const group of groups || []) {
            const groupPath = parentPath ? `${parentPath}/${group.dir}` : group.dir;
            const hit = (group.files || []).find(f => `${groupPath}/${f.name}` === path);
            if (hit) return hit.title || hit.name;
            const childHit = walk(group.children, groupPath);
            if (childHit !== null) return childHit;
        }
        return null;
    };
    return walk(_knowledgeTreeData, '');
}

// Open every ancestor group of the given file path so it is visible.
function _expandKnowledgeGroupFor(path) {
    if (!path || !path.includes('/')) return;
    const target = document.querySelector(`.knowledge-tree-file[data-path="${CSS.escape(path)}"]`);
    let node = target ? target.closest('.knowledge-tree-group') : null;
    while (node) {
        node.classList.add('open');
        node = node.parentElement ? node.parentElement.closest('.knowledge-tree-group') : null;
    }
}

function renderKnowledgeTree(tree, rootFilesOrFilter, filter) {
    const container = document.getElementById('knowledge-tree');
    container.innerHTML = '';
    let rootFiles, lowerFilter;
    if (typeof rootFilesOrFilter === 'string') {
        rootFiles = _knowledgeRootFiles;
        lowerFilter = (rootFilesOrFilter || '').toLowerCase();
    } else {
        rootFiles = rootFilesOrFilter || _knowledgeRootFiles;
        lowerFilter = (filter || '').toLowerCase();
    }
    (rootFiles || []).forEach(f => {
        if (lowerFilter && !f.title.toLowerCase().includes(lowerFilter) && !f.name.toLowerCase().includes(lowerFilter)) return;
        const fbtn = document.createElement('button');
        fbtn.className = 'knowledge-tree-file' + (_knowledgeCurrentFile === f.name ? ' active' : '');
        fbtn.dataset.path = f.name;
        fbtn.innerHTML = `<i class="fas fa-file-lines text-[10px] text-slate-400"></i><span class="truncate">${escapeHtml(f.title)}</span>${_knowledgeFileActions(f.name)}`;
        fbtn.onclick = () => openKnowledgeFile(f.name, f.title);
        container.appendChild(fbtn);
    });
    _renderKnowledgeGroups(container, tree, '', lowerFilter, 0);
}

function _renderKnowledgeGroups(container, groups, parentPath, lowerFilter, depth) {
    const indent = depth * 12;
    groups.forEach(group => {
        const groupPath = parentPath ? parentPath + '/' + group.dir : group.dir;
        const files = (group.files || []).filter(f =>
            !lowerFilter || f.title.toLowerCase().includes(lowerFilter) || f.name.toLowerCase().includes(lowerFilter)
        );
        const children = group.children || [];
        const hasMatchingChildren = lowerFilter ? _hasFilterMatch(children, lowerFilter) : children.length > 0;
        if (files.length === 0 && !hasMatchingChildren && lowerFilter) return;

        const div = document.createElement('div');
        div.className = 'knowledge-tree-group open';

        const fileCount = _countFiles(group);
        const btn = document.createElement('button');
        btn.className = 'knowledge-tree-group-btn';
        btn.style.paddingLeft = (8 + indent) + 'px';
        btn.innerHTML = `<i class="fas fa-chevron-right chevron"></i><i class="fas fa-folder text-amber-400 text-[11px]"></i><span>${escapeHtml(group.dir)}</span><span class="ml-auto text-[10px] text-slate-400">${fileCount}</span>${_knowledgeCategoryActions(groupPath)}`;
        btn.onclick = () => div.classList.toggle('open');
        div.appendChild(btn);

        const items = document.createElement('div');
        items.className = 'knowledge-tree-group-items';
        files.forEach(f => {
            const fbtn = document.createElement('button');
            const fpath = groupPath + '/' + f.name;
            fbtn.className = 'knowledge-tree-file' + (_knowledgeCurrentFile === fpath ? ' active' : '');
            fbtn.dataset.path = fpath;
            fbtn.style.paddingLeft = (24 + indent) + 'px';
            fbtn.innerHTML = `<i class="fas fa-file-lines text-[10px] text-slate-400"></i><span class="truncate">${escapeHtml(f.title)}</span>${_knowledgeFileActions(fpath)}`;
            fbtn.onclick = () => openKnowledgeFile(fpath, f.title);
            items.appendChild(fbtn);
        });
        if (children.length > 0) {
            _renderKnowledgeGroups(items, children, groupPath, lowerFilter, depth + 1);
        }
        div.appendChild(items);
        container.appendChild(div);
    });
}

function _knowledgeActionButton(icon, title, handler) {
    const danger = icon === 'fa-trash' ? ' danger' : '';
    return `<span role="button" tabindex="0" title="${escapeHtml(title)}" onclick="event.stopPropagation();${handler}" class="knowledge-action${danger}"><i class="fas ${icon}"></i></span>`;
}

function _knowledgeFileActions(path) {
    if (path === 'index.md' || path === 'log.md') return '';
    if (!canWriteKnowledge()) return '';
    const value = JSON.stringify(path).replace(/"/g, '&quot;');
    return `<span class="knowledge-actions">${_knowledgeActionButton('fa-arrow-right-arrow-left', '移动', `moveKnowledgeDocument(${value})`)}${_knowledgeActionButton('fa-trash', '删除', `deleteKnowledgeDocument(${value})`)}</span>`;
}

function _knowledgeCategoryActions(path) {
    if (!canWriteKnowledge()) return '';
    const value = JSON.stringify(path).replace(/"/g, '&quot;');
    return `<span class="knowledge-actions">${_knowledgeActionButton('fa-pen', '重命名', `renameKnowledgeCategory(${value})`)}${_knowledgeActionButton('fa-trash', '删除', `deleteKnowledgeCategory(${value})`)}</span>`;
}

async function dispatchKnowledgeAction(action, payload, openPathResolver) {
    _setKnowledgeStatus(currentLang === 'zh' ? '处理中...' : 'Working...', false, true);
    try {
        const response = await fetch('/api/knowledge/action', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({action, payload, agent_id: viewingKnowledgeAgentId()}),
        });
        const result = await response.json();
        if (result.status !== 'success') {
            _setKnowledgeStatus(result.message || (currentLang === 'zh' ? '操作失败' : 'Operation failed'), true);
            loadKnowledgeView();
            return null;
        }
        _setKnowledgeStatus(_knowledgeResultMessage(action, result.payload), false);
        // Optionally auto-open the affected file after the tree refreshes.
        const openPath = openPathResolver ? openPathResolver(result.payload) : null;
        loadKnowledgeView(openPath || undefined);
        return result.payload;
    } catch (error) {
        _setKnowledgeStatus(currentLang === 'zh' ? '请求失败，请稍后重试' : 'Request failed, please try again', true);
        return null;
    }
}

function _setKnowledgeStatus(message, isError, persistent) {
    const el = document.getElementById('knowledge-action-status');
    el.textContent = message;
    el.className = `text-xs transition-opacity duration-200 ${isError ? 'text-red-500' : 'text-primary-500'}`;
    el.classList.remove('opacity-0');
    clearTimeout(el._hideTimer);
    if (!persistent) el._hideTimer = setTimeout(() => el.classList.add('opacity-0'), 3500);
}

function _knowledgeResultMessage(action, payload) {
    if (currentLang !== 'zh') {
        return action === 'create_category' ? 'Category created' :
            action === 'create_document' ? 'Document created' :
            action === 'rename_category' ? 'Category renamed' :
            action === 'delete_category' ? 'Category deleted' :
            action === 'import_documents' ? `${payload?.imported || 0} imported · ${payload?.skipped || 0} skipped · ${payload?.failed || 0} failed` :
            action === 'move_documents' ? `${payload?.moved || 0} document moved` :
            `${payload?.deleted || 0} document deleted`;
    }
    return action === 'create_category' ? '分类已创建' :
        action === 'create_document' ? '文档已创建' :
        action === 'rename_category' ? '分类已重命名' :
        action === 'delete_category' ? '分类已删除' :
        action === 'import_documents' ? `导入 ${payload?.imported || 0} 个，跳过 ${payload?.skipped || 0} 个，失败 ${payload?.failed || 0} 个` :
        action === 'move_documents' ? `已移动 ${payload?.moved || 0} 个文档` :
        `已删除 ${payload?.deleted || 0} 个文档`;
}

function _knowledgeCategoryPaths(groups, parent = '') {
    const paths = [];
    for (const group of groups || []) {
        const path = parent ? `${parent}/${group.dir}` : group.dir;
        paths.push(path, ..._knowledgeCategoryPaths(group.children || [], path));
    }
    return paths;
}

function openKnowledgeDialog(options) {
    const overlay = document.getElementById('knowledge-dialog-overlay');
    const card = document.getElementById('knowledge-dialog-card');
    const input = document.getElementById('knowledge-dialog-input');
    const select = document.getElementById('knowledge-dialog-select');
    const textarea = document.getElementById('knowledge-dialog-textarea');
    const documentForm = document.getElementById('knowledge-document-form');
    const documentFilename = document.getElementById('knowledge-document-filename');
    const documentContent = document.getElementById('knowledge-document-content');
    const templateBtn = document.getElementById('knowledge-document-template');
    const documentPathPreview = document.getElementById('knowledge-document-path-preview');
    const submit = document.getElementById('knowledge-dialog-submit');
    const cancel = document.getElementById('knowledge-dialog-cancel');
    document.getElementById('knowledge-dialog-title').textContent = options.title;
    document.getElementById('knowledge-dialog-subtitle').textContent = options.subtitle || '';
    document.getElementById('knowledge-dialog-label').textContent = options.label;
    document.getElementById('knowledge-dialog-hint').textContent = options.hint || '';
    document.getElementById('knowledge-dialog-error').classList.add('hidden');
    document.getElementById('knowledge-dialog-icon').className = `fas ${options.icon || 'fa-folder'} text-emerald-500`;
    card.classList.toggle('knowledge-document-dialog', options.type === 'document');
    input.classList.toggle('hidden', options.type === 'select' || options.type === 'textarea' || options.type === 'document');
    select.classList.toggle('hidden', options.type !== 'select');
    textarea.classList.toggle('hidden', options.type !== 'textarea');
    documentForm.classList.toggle('hidden', options.type !== 'document');
    input.value = options.value || '';
    textarea.value = options.value || '';
    documentFilename.value = options.filename || '';
    documentContent.value = options.content || '';
    document.getElementById('knowledge-document-category-label').textContent = currentLang === 'zh' ? '目标分类' : 'Destination category';
    documentPathPreview.textContent = options.category
        ? `knowledge/${options.category}/`
        : 'knowledge/';
    documentFilename.oninput = null;
    document.getElementById('knowledge-document-filename-label').textContent = currentLang === 'zh' ? '文件名' : 'Filename';
    document.getElementById('knowledge-document-content-label').textContent = currentLang === 'zh' ? 'Markdown 内容' : 'Markdown content';
    templateBtn.textContent = currentLang === 'zh' ? '插入模板' : 'Insert template';
    templateBtn.onclick = () => {
        if (documentContent.value.trim()) return;
        const title = (documentFilename.value || 'untitled').replace(/\.md$/i, '');
        documentContent.value = currentLang === 'zh'
            ? `# ${title}\n\n## 摘要\n\n\n## 关键点\n\n- \n\n## 参考\n\n`
            : `# ${title}\n\n## Summary\n\n\n## Key points\n\n- \n\n## References\n\n`;
        documentContent.focus();
    };
    if (options.type === 'select') {
        // Use the shared custom dropdown component instead of a native
        // <select> so the arrow / menu match the rest of the console.
        const ddOptions = (options.choices || []).map(value => ({ value, label: value }));
        initDropdown(select, ddOptions, (options.choices || [])[0] || '', null);
    }
    submit.textContent = currentLang === 'zh' ? '确定' : 'Confirm';
    cancel.textContent = currentLang === 'zh' ? '取消' : 'Cancel';
    submit.disabled = options.type === 'select' && !(options.choices || []).length;

    const close = () => overlay.classList.add('hidden');
    const submitAction = async () => {
        const rawValue = options.type === 'select' ? getDropdownValue(select) :
            (options.type === 'textarea' ? textarea.value :
            (options.type === 'document' ? {
                filename: documentFilename.value.trim(),
                content: documentContent.value,
            } : input.value));
        const value = options.type === 'textarea' || options.type === 'document' ? rawValue : rawValue.trim();
        const error = options.validate ? options.validate(value) : (!value ? (currentLang === 'zh' ? '此项不能为空' : 'This field is required') : '');
        if (error) {
            const errorEl = document.getElementById('knowledge-dialog-error');
            errorEl.textContent = error;
            errorEl.classList.remove('hidden');
            return;
        }
        submit.disabled = true;
        const ok = await options.onSubmit(value);
        submit.disabled = false;
        if (ok !== null) close();
    };
    submit.onclick = submitAction;
    cancel.onclick = close;
    overlay.onclick = event => { if (event.target === overlay) close(); };
    input.onkeydown = event => { if (event.key === 'Enter') submitAction(); };
    overlay.classList.remove('hidden');
    setTimeout(() => (options.type === 'select' ? select : (options.type === 'textarea' ? textarea : (options.type === 'document' ? documentFilename : input))).focus(), 0);
}

function closeKnowledgeNewMenu() {
    const list = document.getElementById('knowledge-new-menu-list');
    if (list) list.classList.add('hidden');
    document.removeEventListener('click', _knowledgeNewMenuOutside, true);
}

function _knowledgeNewMenuOutside(event) {
    const menu = document.getElementById('knowledge-new-menu');
    if (menu && !menu.contains(event.target)) closeKnowledgeNewMenu();
}

function toggleKnowledgeNewMenu(event) {
    if (event) event.stopPropagation();
    const list = document.getElementById('knowledge-new-menu-list');
    if (!list) return;
    const willOpen = list.classList.contains('hidden');
    list.classList.toggle('hidden');
    if (willOpen) {
        document.addEventListener('click', _knowledgeNewMenuOutside, true);
    } else {
        document.removeEventListener('click', _knowledgeNewMenuOutside, true);
    }
}

function createKnowledgeCategory() {
    openKnowledgeDialog({
        title: currentLang === 'zh' ? '新建分类' : 'New category',
        subtitle: currentLang === 'zh' ? '分类会创建为 knowledge/ 下的目录' : 'Creates a directory under knowledge/',
        label: currentLang === 'zh' ? '分类路径' : 'Category path',
        hint: currentLang === 'zh' ? '支持嵌套路径，例如 research/ai' : 'Nested paths are supported, e.g. research/ai',
        icon: 'fa-folder-plus',
        onSubmit: path => dispatchKnowledgeAction('create_category', {path}),
    });
}

function createKnowledgeDocument() {
    const categories = _knowledgeCategoryPaths(_knowledgeTreeData);
    if (!categories.length) {
        _setKnowledgeStatus(currentLang === 'zh' ? '请先创建分类' : 'Create a category first', true);
        return;
    }
    openKnowledgeDialog({
        title: currentLang === 'zh' ? '新建文档' : 'New document',
        subtitle: currentLang === 'zh' ? '先选择分类，然后输入文件名' : 'Choose a category, then enter a filename',
        label: currentLang === 'zh' ? '目标分类' : 'Destination category',
        type: 'select',
        choices: categories,
        icon: 'fa-file-circle-plus',
        onSubmit: category => {
            openKnowledgeDocumentEditor(category);
            return null;
        },
    });
}

function openKnowledgeDocumentEditor(category) {
    openKnowledgeDialog({
        title: currentLang === 'zh' ? '新建文档' : 'New document',
        subtitle: currentLang === 'zh' ? `保存到 ${category}` : `Save to ${category}`,
        label: '',
        hint: currentLang === 'zh' ? '文件名可省略 .md 后缀；保存后会自动同步索引。' : 'The .md suffix is optional. Index sync runs after saving.',
        type: 'document',
        category,
        filename: '',
        content: '',
        icon: 'fa-file-circle-plus',
        validate: value => {
            if (!value.filename) return currentLang === 'zh' ? '文件名不能为空' : 'Filename is required';
            if (/\.[^.]+$/i.test(value.filename) && !/\.md$/i.test(value.filename)) {
                return currentLang === 'zh' ? '新建文档仅支持 .md 文件名' : 'New documents must be .md files';
            }
            if (!value.content.trim()) return currentLang === 'zh' ? '内容不能为空' : 'Content is required';
            if (new Blob([value.content]).size > KNOWLEDGE_IMPORT_MAX_FILE_SIZE) {
                return currentLang === 'zh' ? '内容不能超过 10MB' : 'Content cannot exceed 10MB';
            }
            return '';
        },
        onSubmit: value => {
            const safeName = value.filename.endsWith('.md') ? value.filename : `${value.filename}.md`;
            return dispatchKnowledgeAction('create_document', {
                path: `${category}/${safeName}`,
                content: value.content,
                overwrite: false,
            }, payload => payload?.path || `${category}/${safeName}`);
        },
    });
}

function selectKnowledgeImportFiles() {
    const input = document.getElementById('knowledge-import-input');
    input.value = '';
    input.onchange = () => {
        if (input.files && input.files.length) openKnowledgeImportDialog(Array.from(input.files));
    };
    input.click();
}

function openKnowledgeImportDialog(files) {
    if (!canWriteKnowledge()) return;
    const validationError = validateKnowledgeImportFiles(files);
    if (validationError) {
        _setKnowledgeStatus(validationError, true);
        return;
    }
    const choices = _knowledgeCategoryPaths(_knowledgeTreeData);
    openKnowledgeDialog({
        title: currentLang === 'zh' ? '导入文档' : 'Import documents',
        subtitle: currentLang === 'zh' ? `已选择 ${files.length} 个文件` : `${files.length} file(s) selected`,
        label: currentLang === 'zh' ? '目标分类' : 'Destination category',
        hint: choices.length ? (currentLang === 'zh' ? '支持 Markdown 和 TXT，TXT 会转成 Markdown 文档' : 'Markdown and TXT are supported. TXT is converted to Markdown.') :
            (currentLang === 'zh' ? '请先创建一个分类' : 'Create a category first'),
        type: 'select',
        choices,
        icon: 'fa-file-arrow-up',
        onSubmit: target => importKnowledgeDocuments(files, target),
    });
}

async function importKnowledgeDocuments(files, targetCategory) {
    const validationError = validateKnowledgeImportFiles(files);
    if (validationError) {
        _setKnowledgeStatus(validationError, true);
        return null;
    }
    const supported = files.filter(file => /\.(md|txt)$/i.test(file.name || ''));
    if (!supported.length) {
        _setKnowledgeStatus(currentLang === 'zh' ? '请选择 .md 或 .txt 文件' : 'Choose .md or .txt files', true);
        return null;
    }
    const formData = new FormData();
    formData.append('target_category', targetCategory);
    formData.append('conflict_strategy', 'rename');
    supported.forEach(file => formData.append('files', file, file.name));
    _setKnowledgeStatus(currentLang === 'zh' ? '正在导入...' : 'Importing...', false, true);
    try {
        const response = await fetch(_kbUrl('/api/knowledge/import'), { method: 'POST', body: formData });
        const result = await response.json();
        if (result.status !== 'success') {
            _setKnowledgeStatus(result.message || (currentLang === 'zh' ? '导入失败' : 'Import failed'), true);
            loadKnowledgeView();
            return null;
        }
        _setKnowledgeStatus(_knowledgeResultMessage('import_documents', result.payload), false);
        // Auto-open the first successfully imported document.
        const firstImported = (result.payload?.results || []).find(item => item.status === 'imported');
        loadKnowledgeView(firstImported ? firstImported.path : undefined);
        return result.payload;
    } catch (error) {
        _setKnowledgeStatus(currentLang === 'zh' ? '导入请求失败' : 'Import request failed', true);
        return null;
    }
}

function validateKnowledgeImportFiles(files) {
    if (!files || !files.length) return currentLang === 'zh' ? '请选择文件' : 'Choose files';
    if (files.length > KNOWLEDGE_IMPORT_MAX_FILES) {
        return currentLang === 'zh' ? `一次最多导入 ${KNOWLEDGE_IMPORT_MAX_FILES} 个文件` : `Import at most ${KNOWLEDGE_IMPORT_MAX_FILES} files at a time`;
    }
    let total = 0;
    for (const file of files) {
        total += file.size || 0;
        if ((file.size || 0) > KNOWLEDGE_IMPORT_MAX_FILE_SIZE) {
            return currentLang === 'zh' ? `${file.name} 超过 10MB` : `${file.name} exceeds 10MB`;
        }
    }
    if (total > KNOWLEDGE_IMPORT_MAX_TOTAL_SIZE) {
        return currentLang === 'zh' ? '单次导入总大小不能超过 200MB' : 'Total import size cannot exceed 200MB';
    }
    return '';
}

let _knowledgeImportDropReady = false;
function initKnowledgeImportDropZone() {
    if (_knowledgeImportDropReady) return;
    const panel = document.getElementById('knowledge-panel-docs');
    if (!panel) return;
    _knowledgeImportDropReady = true;
    ['dragenter', 'dragover'].forEach(name => {
        panel.addEventListener(name, event => {
            if (!event.dataTransfer || !event.dataTransfer.types.includes('Files')) return;
            event.preventDefault();
            panel.classList.add('knowledge-import-drag-over');
        });
    });
    ['dragleave', 'drop'].forEach(name => {
        panel.addEventListener(name, event => {
            if (event.type === 'drop') {
                event.preventDefault();
                const files = Array.from(event.dataTransfer?.files || []);
                if (files.length && canWriteKnowledge()) openKnowledgeImportDialog(files);
            }
            panel.classList.remove('knowledge-import-drag-over');
        });
    });
}

function renameKnowledgeCategory(path) {
    openKnowledgeDialog({
        title: currentLang === 'zh' ? '重命名分类' : 'Rename category',
        subtitle: path,
        label: currentLang === 'zh' ? '新的分类路径' : 'New category path',
        value: path,
        icon: 'fa-pen',
        validate: value => value === path ? (currentLang === 'zh' ? '请输入不同的分类路径' : 'Enter a different category path') : '',
        onSubmit: newPath => dispatchKnowledgeAction('rename_category', {path, new_path: newPath}),
    });
}

function deleteKnowledgeCategory(path) {
    showConfirmDialog({
        title: '删除分类',
        message: `确认删除“${path}”及其中全部文档？`,
        okText: t('confirm_yes'),
        cancelText: t('confirm_cancel'),
        onConfirm: () => dispatchKnowledgeAction('delete_category', {path, confirm: true}),
    });
}

function deleteKnowledgeDocument(path) {
    showConfirmDialog({
        title: '删除文档',
        message: `确认删除“${path}”？`,
        okText: t('confirm_yes'),
        cancelText: t('confirm_cancel'),
        onConfirm: () => dispatchKnowledgeAction('delete_documents', {paths: [path]}),
    });
}

function moveKnowledgeDocument(path) {
    const currentCategory = path.includes('/') ? path.split('/').slice(0, -1).join('/') : '';
    const choices = _knowledgeCategoryPaths(_knowledgeTreeData).filter(value => value !== currentCategory);
    openKnowledgeDialog({
        title: currentLang === 'zh' ? '移动文档' : 'Move document',
        subtitle: path,
        label: currentLang === 'zh' ? '目标分类' : 'Destination category',
        hint: choices.length ? '' : (currentLang === 'zh' ? '请先创建其他分类' : 'Create another category first'),
        type: 'select',
        choices,
        icon: 'fa-arrow-right-arrow-left',
        onSubmit: target => dispatchKnowledgeAction('move_documents', {paths: [path], target_category: target}),
    });
}

function _hasFilterMatch(groups, lowerFilter) {
    for (const g of groups) {
        for (const f of (g.files || [])) {
            if (f.title.toLowerCase().includes(lowerFilter) || f.name.toLowerCase().includes(lowerFilter)) return true;
        }
        if (_hasFilterMatch(g.children || [], lowerFilter)) return true;
    }
    return false;
}

function _countFiles(group) {
    let count = (group.files || []).length;
    for (const child of (group.children || [])) {
        count += _countFiles(child);
    }
    return count;
}

function filterKnowledgeTree(query) {
    renderKnowledgeTree(_knowledgeTreeData, _knowledgeRootFiles, query);
}

function resolveKnowledgePath(currentFilePath, relativeHref) {
    // currentFilePath: e.g. "concepts/mcp-protocol.md"
    // relativeHref: e.g. "../entities/openai.md"
    const parts = currentFilePath.split('/');
    parts.pop(); // remove filename, keep directory
    const segments = [...parts, ...relativeHref.split('/')];
    const resolved = [];
    for (const seg of segments) {
        if (seg === '..') resolved.pop();
        else if (seg !== '.' && seg !== '') resolved.push(seg);
    }
    return resolved.join('/');
}

function bindKnowledgeLinks(container, currentFilePath) {
    container.querySelectorAll('a').forEach(a => {
        const href = a.getAttribute('href');
        if (!href || !href.endsWith('.md')) return;
        // Skip absolute URLs
        if (/^https?:\/\//.test(href)) return;

        a.addEventListener('click', (e) => {
            e.preventDefault();
            const resolved = resolveKnowledgePath(currentFilePath, href);
            const linkTitle = a.textContent.trim() || resolved.replace(/\.md$/, '').split('/').pop();
            openKnowledgeFile(resolved, linkTitle);
        });
        a.style.cursor = 'pointer';
        a.classList.add('text-primary-500', 'hover:underline');
    });
}

// Rewrite <img> srcs that are relative to the knowledge doc's directory into
// /api/file URLs, mirroring bindKnowledgeLinks for links. Runs on rendered
// DOM, so markdown syntax quoted inside code blocks is never touched. The
// lightbox onclick that renderMarkdown attached reads this.src at click time,
// so rewriting src alone keeps zoom working.
function bindKnowledgeImages(container, baseDir) {
    if (!baseDir) return;
    container.querySelectorAll('img').forEach(img => {
        const src = img.getAttribute('src');
        // Remote / data / site-absolute srcs resolve on their own.
        if (!src || /^(?:[a-z][\w+.-]*:|\/)/i.test(src)) return;
        const combined = `${baseDir}/${src.split('?')[0]}`;
        const segments = [];
        for (const seg of combined.split('/')) {
            if (seg === '..') segments.pop();
            else if (seg !== '.' && seg !== '') segments.push(seg);
        }
        // baseDir is an absolute posix path, so restore the leading slash the
        // split() dropped — /api/file rejects non-absolute paths.
        const resolved = (combined.startsWith('/') ? '/' : '') + segments.join('/');
        img.src = '/api/file?path=' + encodeURIComponent(resolved);
    });
}

function openKnowledgeFile(path, title) {
    _knowledgeCurrentFile = path;
    // Update active state in tree via data-path
    document.querySelectorAll('.knowledge-tree-file').forEach(el => {
        el.classList.toggle('active', el.dataset.path === path);
    });

    // Immediately hide placeholder
    document.getElementById('knowledge-content-placeholder').classList.add('hidden');

    fetch(_kbUrl(`/api/knowledge/read?path=${encodeURIComponent(path)}`)).then(r => r.json()).then(data => {
        if (data.status !== 'success') return;
        const viewer = document.getElementById('knowledge-content-viewer');
        document.getElementById('knowledge-viewer-title').textContent = title;
        document.getElementById('knowledge-viewer-path').textContent = path;
        const bodyEl = document.getElementById('knowledge-viewer-body');
        bodyEl.innerHTML = renderMarkdown(data.content || '');
        viewer.classList.remove('hidden');
        applyHighlighting(viewer);
        bindKnowledgeLinks(bodyEl, path);
        bindKnowledgeImages(bodyEl, data.dir);

        // Mobile: hide sidebar, show content
        if (window.innerWidth < 768) {
            document.getElementById('knowledge-sidebar').classList.add('hidden');
        }
    }).catch(() => {});
}

function knowledgeMobileBack() {
    document.getElementById('knowledge-sidebar').classList.remove('hidden');
    document.getElementById('knowledge-content-viewer').classList.add('hidden');
}

function switchKnowledgeTab(tab) {
    document.querySelectorAll('.knowledge-tab').forEach(el => el.classList.remove('active'));
    document.getElementById('knowledge-tab-' + tab).classList.add('active');

    const docsPanel = document.getElementById('knowledge-panel-docs');
    const graphPanel = document.getElementById('knowledge-panel-graph');

    if (tab === 'docs') {
        docsPanel.classList.remove('hidden');
        graphPanel.classList.add('hidden');
    } else {
        docsPanel.classList.add('hidden');
        graphPanel.classList.remove('hidden');
        if (!_knowledgeGraphLoaded) {
            loadKnowledgeGraph();
        }
    }
}

let _d3LoadPromise = null;

function ensureD3Loaded() {
    if (window.d3) return Promise.resolve(window.d3);
    if (_d3LoadPromise) return _d3LoadPromise;
    _d3LoadPromise = new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = 'assets/vendor/d3/d3.min.js';
        script.async = true;
        script.onload = () => resolve(window.d3);
        script.onerror = () => reject(new Error('Failed to load d3'));
        document.head.appendChild(script);
    });
    return _d3LoadPromise;
}

function loadKnowledgeGraph() {
    _knowledgeGraphLoaded = true;
    const container = document.getElementById('knowledge-graph-container');
    container.innerHTML = '<div class="flex items-center justify-center h-full text-slate-400 text-sm"><i class="fas fa-spinner fa-spin mr-2"></i>Loading graph...</div>';

    Promise.all([
        ensureD3Loaded(),
        fetch(_kbUrl('/api/knowledge/graph')).then(r => r.json()),
    ]).then(([, data]) => {
        const nodes = data.nodes || [];
        const links = data.links || [];
        if (nodes.length === 0) {
            container.innerHTML = `<div class="flex flex-col items-center justify-center h-full text-slate-400"><i class="fas fa-diagram-project text-3xl mb-3 opacity-40"></i><p class="text-sm">${t('knowledge_empty_hint')}</p></div>`;
            return;
        }
        container.innerHTML = '';
        renderKnowledgeGraph(container, nodes, links);
    }).catch(() => {
        container.innerHTML = '<div class="flex items-center justify-center h-full text-slate-400 text-sm">Failed to load graph</div>';
    });
}

function renderKnowledgeGraph(container, nodes, links) {
    const width = container.clientWidth;
    const height = container.clientHeight || 600;

    // Order categories by node count so the dominant cluster gets the most
    // salient palette entry. Ties break by name to keep colors stable.
    const catCount = {};
    nodes.forEach(n => { catCount[n.category] = (catCount[n.category] || 0) + 1; });
    const categories = Object.keys(catCount).sort(
        (a, b) => catCount[b] - catCount[a] || a.localeCompare(b)
    );
    const colorScale = d3.scaleOrdinal(d3.schemeTableau10).domain(categories);

    // Connection count for sizing
    const connCount = {};
    nodes.forEach(n => connCount[n.id] = 0);
    links.forEach(l => {
        connCount[l.source] = (connCount[l.source] || 0) + 1;
        connCount[l.target] = (connCount[l.target] || 0) + 1;
    });

    const svg = d3.select(container)
        .append('svg')
        .attr('width', width)
        .attr('height', height);

    const g = svg.append('g');

    // Zoom with adaptive label visibility
    let currentZoomScale = 1;
    // Set once the graph is fitted to the viewport. Labels hide below it, so
    // zooming out past the default view still declutters.
    let fittedScale = 1;
    const zoom = d3.zoom()
        .scaleExtent([0.2, 5])
        .on('zoom', (event) => {
            g.attr('transform', event.transform);
            currentZoomScale = event.transform.k;
            updateLabelVisibility();
        });
    svg.call(zoom);

    function updateLabelVisibility() {
        if (!label) return;
        // Fitting a graph of any size into the panel lands well below scale 1,
        // so a fixed threshold would hide every label in the default view.
        // Compare against the fitted scale instead, and keep the text a
        // constant size on screen — inside the zoomed <g>, that means dividing
        // by the scale.
        if (currentZoomScale < fittedScale * 0.9) {
            label.attr('opacity', 0);
            return;
        }
        label.attr('opacity', 1)
            .attr('font-size', 10 / currentZoomScale)
            .attr('dx', d => getNodeRadius(d) + 4 / currentZoomScale)
            .attr('dy', 3 / currentZoomScale);
    }

    const simulation = d3.forceSimulation(nodes)
        .force('link', d3.forceLink(links).id(d => d.id).distance(90))
        .force('charge', d3.forceManyBody().strength(-180))
        .force('center', d3.forceCenter(width / 2, height / 2))
        .force('x', d3.forceX(width / 2).strength(0.06))
        .force('y', d3.forceY(height / 2).strength(0.06))
        .force('collision', d3.forceCollide().radius(d => getNodeRadius(d) + 30));

    function getNodeRadius(d) {
        return Math.max(5, Math.min(16, 5 + (connCount[d.id] || 0) * 2));
    }

    const link = g.append('g')
        .selectAll('line')
        .data(links)
        .join('line')
        .attr('stroke', '#94a3b8')
        .attr('stroke-opacity', 0.3)
        .attr('stroke-width', 1);

    const node = g.append('g')
        .selectAll('circle')
        .data(nodes)
        .join('circle')
        .attr('r', d => getNodeRadius(d))
        .attr('fill', d => colorScale(d.category))
        .attr('stroke', '#fff')
        .attr('stroke-width', 1.5)
        .style('cursor', 'pointer')
        .call(d3.drag()
            .on('start', (event, d) => { if (!event.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
            .on('drag', (event, d) => { d.fx = event.x; d.fy = event.y; })
            .on('end', (event, d) => { if (!event.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; })
        );

    const label = g.append('g')
        .selectAll('text')
        .data(nodes)
        .join('text')
        .text(d => d.label.length > 15 ? d.label.slice(0, 14) + '…' : d.label)
        .attr('font-size', 9)
        .attr('dx', d => getNodeRadius(d) + 4)
        .attr('dy', 3)
        .attr('fill', '#64748b')
        .style('pointer-events', 'none');

    // Tooltip
    const tooltip = document.createElement('div');
    tooltip.className = 'knowledge-graph-tooltip';
    container.style.position = 'relative';
    container.appendChild(tooltip);

    node.on('mouseover', (event, d) => {
        tooltip.textContent = d.label + ' (' + d.category + ')';
        tooltip.style.opacity = '1';
        tooltip.style.left = (event.offsetX + 12) + 'px';
        tooltip.style.top = (event.offsetY - 8) + 'px';
        // Highlight connections
        link.attr('stroke-opacity', l => (l.source.id === d.id || l.target.id === d.id) ? 0.8 : 0.1);
        node.attr('opacity', n => n.id === d.id || links.some(l => (l.source.id === d.id && l.target.id === n.id) || (l.target.id === d.id && l.source.id === n.id)) ? 1 : 0.2);
        label.attr('opacity', n => n.id === d.id || links.some(l => (l.source.id === d.id && l.target.id === n.id) || (l.target.id === d.id && l.source.id === n.id)) ? 1 : 0.1);
    }).on('mousemove', (event) => {
        tooltip.style.left = (event.offsetX + 12) + 'px';
        tooltip.style.top = (event.offsetY - 8) + 'px';
    }).on('mouseout', () => {
        tooltip.style.opacity = '0';
        link.attr('stroke-opacity', 0.3);
        node.attr('opacity', 1);
        label.attr('opacity', 1);
    }).on('click', (event, d) => {
        // Switch to docs tab and open the file
        switchKnowledgeTab('docs');
        openKnowledgeFile(d.id, d.label);
    });

    simulation.on('tick', () => {
        link.attr('x1', d => d.source.x).attr('y1', d => d.source.y)
            .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
        node.attr('cx', d => d.x).attr('cy', d => d.y);
        label.attr('x', d => d.x).attr('y', d => d.y);
    });

    // Auto fit-to-view when simulation settles
    simulation.on('end', () => {
        const pad = 16;
        let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
        nodes.forEach(n => {
            if (n.x < x0) x0 = n.x;
            if (n.y < y0) y0 = n.y;
            if (n.x > x1) x1 = n.x;
            if (n.y > y1) y1 = n.y;
        });
        const bw = x1 - x0 + pad * 2;
        const bh = y1 - y0 + pad * 2;
        if (bw > 0 && bh > 0) {
            const scale = Math.min(width / bw, height / bh, 4);
            fittedScale = scale;
            const tx = width / 2 - (x0 + x1) / 2 * scale;
            const ty = height / 2 - (y0 + y1) / 2 * scale;
            svg.transition().duration(500).call(
                zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale)
            );
        }
    });

    // Legend
    const legendDiv = document.createElement('div');
    legendDiv.className = 'knowledge-graph-legend';
    categories.forEach(cat => {
        const item = document.createElement('span');
        item.className = 'knowledge-graph-legend-item';
        item.innerHTML = `<span class="knowledge-graph-legend-dot" style="background:${colorScale(cat)}"></span>${escapeHtml(cat)}`;
        legendDiv.appendChild(item);
    });
    container.appendChild(legendDiv);
}

// =====================================================================
// Authentication
// =====================================================================
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
    // that fills the list never runs, so the 会话历史 section would render empty
    // after switching workbench <-> admin. Trigger the fetch here like the
    // full-page-load path does.
    if (typeof loadSidebarRecentSessions === 'function') loadSidebarRecentSessions();
}
// Whether the 控制台 (admin area) entry is offered to this identity.
//
// Admission is the trusted formal-page projection, not an administrative
// qualification: a member whose role reaches a business page opens the same
// console shell as an administrator, and the pages inside it are filtered per
// item by that same projection (change unify-console-by-data-scope, task 3.1).
// 组织与权限 and the platform surface keep their own checks — the former through
// the read gate on its page keys, the latter through the platform-scope shell
// flag — so dropping the tenant_admin requirement does not widen either.
function _qualifyAdminConsoleEntry(opts) {
    // opts: { identityMode, isPlatformAdmin, isTenantAdmin, mode, pages }
    if (!opts) return true;
    if (opts.identityMode !== 'database') return true; // legacy: no gate
    if (opts.isPlatformAdmin || opts.isTenantAdmin) return true;
    const pages = opts.pages;
    // Unknown projection: don't guess / don't block, the same rule _viewNavDenied
    // follows. The server still authorizes /admin and every API behind it.
    if (!pages || typeof pages !== 'object') return true;
    const allMode = opts.mode === 'all';
    return Object.keys(pages).some(pid => {
        // Only 管理区 (console shell) pages admit the entry. Workbench and
        // personal pages are not reachable from the console, so they must not
        // open a shell with nothing in it (console-information-architecture:
        // 没有管理区可访问页面时隐藏该区域入口). `admin.` is the same marker the
        // per-item gate below uses to tell shell pages from workbench pages.
        if (pid.indexOf('admin.') !== 0) return false;
        const info = pages[pid];
        if (!info || typeof info !== 'object') return false;
        // A withheld menu grant is not an admission.
        if (info.menu_denied === true) return false;
        // Neither is a capability the deployment withdrew: the per-item gate
        // hides that entry, so counting it would open an empty console.
        if (info.reason === 'capability_disabled') return false;
        // Platform-scope pages are not business entry points: a member cannot
        // reach the console through 平台运维.
        if (String(info.scope || '') === 'platform') return false;
        if (allMode) return true;
        return info.available === true || info.read_allowed === true;
    });
}
function _applyNavAreaAttribute() {
    const appEl = document.getElementById('app');
    const area = _navAreaFromPath(typeof location !== 'undefined' ? location.pathname : '');
    if (appEl) appEl.setAttribute('data-nav-area', area);
    const sidebarCaption = document.getElementById('sidebar-brand-caption');
    if (sidebarCaption && typeof sidebarBrandCaption === 'function') {
        const caption = sidebarBrandCaption(
            typeof effectiveLogoDescription === 'function' ? effectiveLogoDescription() : ''
        );
        const hasDesc = !!(caption && String(caption).trim());
        sidebarCaption.textContent = hasDesc ? caption : '';
        sidebarCaption.classList.toggle('hidden', !hasDesc);
        sidebarCaption.title = hasDesc ? caption : '';
    }
    return area;
}
// === NAV_AREA_END ===

// Restore the correct area shell when the user traverses history (back /
// forward). Because _openNavArea now navigates in-place with pushState, the
// browser never reloads, so we must re-apply the area attribute and re-render
// the target area's default view without a full page load.
if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    window.addEventListener('popstate', function () {
        _applyNavAreaAttribute();
        if (typeof _bootAreaDefaultView === 'function') _bootAreaDefaultView();
        // Refill the 会话历史 sidebar list after an in-place back/forward nav,
        // since no full page load runs the boot hook.
        if (typeof loadSidebarRecentSessions === 'function') loadSidebarRecentSessions();
    });
}

function _setupHeaderTenantSelector() {
    const sel = document.getElementById('tenant-selector');
    if (!sel) return;
    const label = document.getElementById('tenant-selector-label');
    const menu = document.getElementById('tenant-menu');
    if (!menu) return;
    // Only meaningful in database mode with a selected tenant.
    const tid = sessionStorage.getItem('cow_tenant_id');
    if (!tid) { sel.classList.add('hidden'); return; }
    sel.classList.remove('hidden');
    menu.innerHTML = '';
    // The picker lists the SELF's effective tenants (via /auth/me), not the
    // platform-admin tenant list — a platform admin is not given tenant members
    // for tenants they are not on. The list is not an authorization grant.
    fetch('/auth/me').then(r => r.json()).then(data => {
        const tenants = (data && data.status === 'success' && Array.isArray(data.tenants))
            ? data.tenants.map(tn => ({ id: tn.id, code: tn.code, name: tn.name })) : [];
        if (!tenants.length) return;
        const current = tenants.find(t => t.id === tid);
        if (current) label.textContent = current.name || current.code;
        tenants.forEach(tn => {
            const item = document.createElement('button');
            item.className = 'tenant-menu-item w-full text-left px-3 py-1.5 text-sm text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-white/10 cursor-pointer';
            item.dataset.tenant = tn.id;
            item.textContent = (tn.name || tn.code) + (tn.id === tid ? ' ✓' : '');
            item.addEventListener('click', () => {
                menu.classList.add('hidden');
                if (sessionStorage.getItem('cow_tenant_id') === tn.id) return;
                // Refresh-based switch (task 3.6): navigate with a one-shot
                // switch_tenant param; the new page validates before committing,
                // so the old page and its requests are never left half-switched.
                if (typeof bumpTenantGeneration === 'function') bumpTenantGeneration();
                const url = new URL(window.location.href);
                url.searchParams.set('switch_tenant', tn.id);
                window.location.assign(url.toString());
            });
            menu.appendChild(item);
        });
    }).catch(() => {});
}

// A platform administrator may see tenants they do not belong to. Resolve
// business context only from the authenticated account's effective memberships,
// including on reload when sessionStorage is empty or contains a stale tenant.
async function _ensureTenantSelected() {
    if (_identityMode() !== 'database') return true;
    const epoch = _authEpoch;
    const response = await fetch('/auth/me', { credentials: 'same-origin', cache: 'no-store' });
    if (epoch !== _authEpoch) return false;
    if (!response.ok) throw new Error('Tenant membership unavailable');
    const data = await response.json();
    if (epoch !== _authEpoch) return false;
    if (!data || data.status !== 'success' || !Array.isArray(data.tenants)) {
        throw new Error('Invalid tenant membership response');
    }
    const tenants = data.tenants.filter(tn => tn && typeof tn.id === 'string' && tn.id);
    const stored = sessionStorage.getItem('cow_tenant_id');
    if (tenants.some(tn => tn.id === stored)) return true;
    sessionStorage.removeItem('cow_tenant_id');
    if (tenants.length === 1) {
        sessionStorage.setItem('cow_tenant_id', tenants[0].id);
        if (typeof bumpTenantGeneration === 'function') bumpTenantGeneration();
        return true;
    }
    if (tenants.length > 1) {
        _showTenantPicker(tenants, null);
        return false;
    }
    if (data.user?.is_platform_admin === true) return 'platform';
    const error = new Error('No available tenant membership');
    error.code = 'no_tenants';
    throw error;
}

function toggleTenantMenu(event) {
    closeAccountMenu();
    event.stopPropagation();
    const menu = document.getElementById('tenant-menu');
    if (menu) menu.classList.toggle('hidden');
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
}

async function _submitAccountLogin(event) {
    event.preventDefault();
    if (_accountWritePending || _pendingTenantPicker) return false;
    const pwdInput = document.getElementById('login-password');
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
        const tenants = Array.isArray(data.tenants)
            ? data.tenants.filter(tn => tn && typeof tn.id === 'string' && tn.id) : [];
        sessionStorage.removeItem('cow_tenant_id');
        if (tenants.length > 1) {
            _showTenantPicker(tenants, null);
        } else {
            if (tenants.length === 1) sessionStorage.setItem('cow_tenant_id', tenants[0].id);
            _afterLogin();
        }
    } catch (_) {
        if (epoch === _authEpoch) {
            _accountText('login-error', t('account_login_failed'));
            _accountHidden('login-error', false);
        }
    } finally {
        _accountWritePending = null;
        btn.disabled = false;
        _renderSidebarAccount();
    }
    return false;
}

function _afterLogin() {
    _clearTenantPicker();
    _resetHistorySearch();
    _enterAccountApp();
}

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
    });
    const enter = event => {
        event.preventDefault();
        if (epoch !== _authEpoch || !_pendingTenantPicker || _accountWritePending) return false;
        const chosen = tenantSelect.value;
        if (!allowed.has(chosen)) return false;
        sessionStorage.setItem('cow_tenant_id', chosen);
        if (typeof bumpTenantGeneration === 'function') bumpTenantGeneration();
        _afterLogin(true);
        return false;
    };
    nextBtn.onclick = enter;
    document.getElementById('login-form').onsubmit = enter;
    _renderSidebarAccount();
    tenantSelect.focus();
}

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
        window.location.reload();
    } catch (_) {
        if (epoch === _authEpoch) _accountState = _emptyAccount('logout_error');
    } finally {
        _accountWritePending = null;
        _renderSidebarAccount();
    }
}
window.handleLogout = handleLogout;

// Only a 401 from the current identity can show the login screen. Preserve
// the earlier Agent-routing fetch wrapper and return the original response.
const _originalFetch = window.fetch;
window.fetch = function(...args) {
    const epoch = _authEpoch;
    return _originalFetch.apply(this, args).then(async response => {
        if (response.status === 401 && epoch === _authEpoch
                && !['unauthenticated', 'logout_pending'].includes(_accountState.phase)
                && _accountWritePending !== 'login') {
            const input = args[0];
            const raw = typeof input === 'string' ? input : input?.url;
            let url;
            try { url = new URL(raw, window.location.href); } catch (_) { return response; }
            if (url.origin === window.location.origin && !url.pathname.startsWith('/auth/')) {
                // A 401 carrying ``invalid_old`` is the recent-password factor
                // refusing one write, not a dead session: the caller is still
                // authenticated and reports the mistake on its own form. Sending
                // it to the login screen throws away a filled-in form that only
                // needs the password retyped — and, because the form reuses the
                // stored password, does it again on every retry. Read a clone so
                // the caller can still consume the original body.
                let code = '';
                try { code = ((await response.clone().json()) || {}).code || ''; }
                catch (_) { code = ''; }
                if (code !== 'invalid_old') showLoginScreen();
            }
        }
        return response;
    });
};

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
        memoryAgentId = readScopedPreference('cow_memory_agent') || '';
        knowledgeAgentId = readScopedPreference('cow_knowledge_agent') || '';
    }
    const chatReady = Promise.resolve(loadAgentCatalog()).then(() => {
        if (epoch !== _authEpoch) return;
        sessionId = loadOrCreateSessionId();
        refreshWorkspaceSelector();
        refreshSessionSettings();
        restoreChatState();
        startPolling();
        fetch('/api/knowledge/list').then(r => r.json()).then(data => {
            if (epoch === _authEpoch && data.status === 'success') {
                _knowledgeTreeData = data.tree || [];
                _knowledgeRootFiles = data.root_files || [];
            }
        }).catch(() => {});
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
        if (_activeAccountPanel === 'password') return false;
    }
    closeAppearancePreferences(returnFocus);
    _accountHidden('account-profile-drawer', true);
    _setAccountPanel(null);
    return true;
}

// --- profile ------------------------------------------------------------

const _ACCOUNT_PROFILE_SEL = {
    displayName: 'ap-display-name', username: 'ap-username', platform: 'ap-platform',
    tenant: 'ap-tenant', memberName: 'ap-member-name', role: 'ap-role',
    department: 'ap-department', position: 'ap-position',
};

async function fetchAccountSelf() {
    // Refreshes /auth/me once per sequence; dedupes concurrent calls.
    if (_accountSelfRequest) return _accountSelfRequest;
    const seq = ++_accountSelfSeq;
    const epoch = _authEpoch;
    const request = Promise.resolve().then(async () => {
        try {
            const resp = await fetch('/auth/me', { credentials: 'same-origin', cache: 'no-store' });
            const data = await resp.json();
            if (seq !== _accountSelfSeq) return null;
            if (resp.status === 401 || data.status !== 'success') return null;
            if (epoch !== _authEpoch) return null;
            _accountSelf = data;
            return data;
        } catch (_) {
            if (seq === _accountSelfSeq && epoch === _authEpoch) _accountSelf = null;
            return null;
        } finally {
            if (_accountSelfRequest === request) _accountSelfRequest = null;
        }
    });
    _accountSelfRequest = request;
    return request;
}

// Best-effort sync view of the last successful /auth/me. Returns null until the
// first fetch resolves; callers must treat null as "unknown" (leave menus as-is)
// rather than as a privilege denial.
function _baseAccountSelf() {
    return _accountSelf && _accountSelf.status === 'success' ? _accountSelf : null;
}

// Resolve the *member-level* display name for the currently-selected tenant from
// the cached /auth/me projection. This is the per-tenant "display name" edited on
// the member record (memberships.display_name), which may differ from the
// account-level display name. Returns '' when there is no self projection, no
// selected tenant, the account is not an active member of that tenant, or the
// member has no display name.
function _currentMemberDisplayName() {
    const self = _baseAccountSelf();
    if (!self) return '';
    const tid = sessionStorage.getItem('cow_tenant_id') || '';
    if (!tid) return '';
    const tenants = (self.tenants || []).filter(tn => tn && tn.id === tid);
    if (!tenants.length) return '';
    const membership = tenants[0].membership || {};
    const displayName = (typeof membership.display_name === 'string')
        ? membership.display_name.trim() : '';
    return displayName;
}

// Best-effort sync view of the last successful /auth/context. Returns null
// until the first fetch resolves; callers must treat null as "unknown", not as
// a privilege denial (menus stay as-is until the projection is known).
function _baseAuthContext() {
    return _authContext && _authContext.status === 'success' ? _authContext : null;
}

// Fetch the current tenant's authoritative capability summary (/auth/context)
// for the *display* projection only. Dedupes concurrent calls and bails on any
// epoch change (account switch/logout). No X-Tenant-ID present -> null (unknown);
// this function never throws.
async function _fetchTenantAuthorization() {
    if (_identityMode() !== 'database') { _authContextPhase = 'unknown'; return null; }
    if (_authContextRequest) return _authContextRequest;
    const tenantId = sessionStorage.getItem('cow_tenant_id') || '';
    if (!tenantId) { _authContextPhase = 'unknown'; return null; }
    const seq = ++_authContextSeq;
    const epoch = _authEpoch;
    // The tenant-scoped capability summary is in flight. The account panel owns no
    // resource entry any more (task 3.3), so this only marks the projection as
    // pending for the navigation gate below; it neither reports a personal
    // "checking" list nor offers an unconfirmed entry.
    _authContextPhase = 'checking';
    const request = Promise.resolve().then(async () => {
        try {
            const resp = await fetch('/auth/context', {
                credentials: 'same-origin', cache: 'no-store',
                headers: { 'X-Tenant-ID': tenantId },
            });
            const data = await resp.json();
            if (seq !== _authContextSeq) return null;
            if (epoch !== _authEpoch) return null;
            if (resp.status === 401 || resp.status === 403 || data.status !== 'success') {
                _authContext = null;
                _authContextPhase = 'failed';
                return null;
            }
            _authContext = data;
            _authContextPhase = 'ready';
            return data;
        } catch (_) {
            if (seq === _authContextSeq && epoch === _authEpoch) {
                _authContext = null;
                _authContextPhase = 'failed';
            }
            return null;
        } finally {
            if (_authContextRequest === request) _authContextRequest = null;
        }
    });
    _authContextRequest = request;
    return request;
}

// Reset the cached /auth/context when the tenant selection changes, so stale
// capability data from a previous tenant is never used to gate navigation. The
// phase goes back to "unknown" with it: no summary is cached, so the next read
// is a fresh check rather than a remembered verdict.
function _invalidateAuthContext() {
    // Bumping the sequence is what discards a late reply from the tenant that
    // just went away: `_fetchTenantAuthorization`'s own `seq !== _authContextSeq`
    // check then refuses to write it. Clearing the in-flight handle lets the
    // next read start at once instead of awaiting a stale promise -- and the old
    // request's `finally` only clears the handle while it still owns it
    // (`_authContextRequest === request`), so it cannot cancel the new request.
    ++_authContextSeq;
    _authContext = null;
    _authContextRequest = null;
    _authContextPhase = 'unknown';
}

// Per-action service availability (change integrate-upstream-core-capabilities,
// design D2). Delegates the strictness to functional-capabilities.js so Web and
// Desktop answer "available?" the same way; a missing module (an old cached
// page, or a build without the file) closes the feature instead of opening it.
// This is a *service* gate, never a permission: every request is still
// authorized server-side.
function _featureAvailable(key) {
    const api = window.RdaiFunctionalCapabilities;
    if (!api || typeof api.available !== 'function') return false;
    return api.available(_baseAuthContext(), key);
}

// Capture the identity a request starts under, and re-check it before applying
// the result: any logout / tenant switch / reconnect bumps _authContextSeq, so a
// captured revision that has moved on means the answer belongs to a screen that
// no longer exists (and must not repaint the new one).
function _featureCapture(agentId, sessionId) {
    const api = window.RdaiFunctionalCapabilities;
    if (!api || typeof api.capture !== 'function') return null;
    return api.capture(_authContextSeq, agentId, sessionId);
}

function _featureCurrent(captured, agentId, sessionId) {
    const api = window.RdaiFunctionalCapabilities;
    if (!api || typeof api.isCurrent !== 'function') return false;
    return api.isCurrent(captured, _authContextSeq, agentId, sessionId);
}

// Map a view id to its authoritative console_pages key (or '' if none). Server
// returns the projection; unknown views fall back to "available" so navigation
// is never spuriously blocked for pages the backend does not sign.
function _consolePageForView(viewId) {
    const meta = VIEW_META[viewId];
    return meta && meta.console ? meta.console : '';
}

// Return {reason:'denied'} when the target view is genuinely unavailable to the
// current identity (no read grant and page not open), in database mode with a
// known projection. Returns falsy to allow navigation. Platform "all" mode and
// any view the backend didn't sign are allowed. A denied read (but open) page is
// also a denial, since the identity cannot use it.
function _viewNavDenied(viewId) {
    if (_identityMode() !== 'database') return null;
    const ctx = _baseAuthContext();
    if (!ctx) return null; // projection unknown -> don't guess / don't block
    if (ctx.authorization_mode === 'all') return null; // platform all
    const key = _consolePageForView(viewId);
    if (!key) return null; // backend didn't sign this page -> leave as-is
    const pages = ctx.console_pages && typeof ctx.console_pages === 'object' ? ctx.console_pages : null;
    if (!pages || !pages[key]) return null; // unknown key -> don't guess
    // A page whose *menu* grant was withheld is denied for every area, including
    // workbench pages. This is the authoritative server signal; do not re-derive
    // it from the client.
    if (pages[key].menu_denied === true) return { reason: 'denied' };
    // Workbench pages are normal business entry points: their consumer
    // availability is reported on the page itself, so they are not denied by the
    // admin availability/read gate below.
    if (key.indexOf('admin.') !== 0) return null;
    if (pages[key].available || pages[key].read_allowed) return null;
    return { reason: 'denied' };
}

// Gate the permission-sensitive sidebar entries (task 5.7/5.8). The "platform
// accounts" entry is visible only to a platform admin. The "identity audit"
// entry is visible only to a platform admin OR the current tenant's tenant_admin
// (or anyone holding a tenant-scoped audit privilege). Rows that fail the check
// are hidden; the authorization decision always stays server-side.
function _applySidebarPermissions(self) {
    // is_platform_admin still comes from the verified /auth/me self profile
    // (it is NOT tenant-scoped, so /auth/context cannot report it). The current
    // tenant's admin qualification and page availability come from the
    // authoritative /auth/context projection — never from a client role array.
    const gotSelf = self || _baseAccountSelf();
    const user = gotSelf && gotSelf.user ? gotSelf.user : null;
    const isPlatformAdmin = !!(user && user.is_platform_admin);
    const ctx = _baseAuthContext();
    const isTenantAdmin = !!(ctx && (ctx.is_tenant_admin === true));
    const mode = (ctx && ctx.authorization_mode) || 'role';
    const pages = (ctx && ctx.console_pages && typeof ctx.console_pages === 'object')
        ? ctx.console_pages : null;

    const isDb = _identityMode() === 'database';
    // Entry to /admin: any identity the trusted projection gives a business page,
    // or a platform/tenant administrator. The gate is the formal page
    // projection, never a client role array and never the tenant_admin
    // qualification alone (change unify-console-by-data-scope, task 3.1).
    const showAdminEntry = _qualifyAdminConsoleEntry({
        identityMode: _identityMode(),
        isPlatformAdmin,
        isTenantAdmin,
        mode,
        pages,
    });
    const openAdminEl = document.getElementById('nav-open-admin');
    if (openAdminEl) openAdminEl.classList.toggle('hidden', !showAdminEntry);

    // Inside /admin, show admin groups when the entry is qualified; per-item
    // console_pages filtering below still applies.
    const canAdmin = showAdminEntry;

    if (isDb && _navAreaFromPath(location.pathname) === 'admin') {
        // Redirect only once qualification is known (platform from /auth/me,
        // otherwise wait for /auth/context).
        if (isPlatformAdmin || ctx) {
            if (!showAdminEntry) {
                try { sessionStorage.setItem('cow_nav_admin_denied', '1'); } catch (_) {}
                location.replace('/chat');
                return;
            }
        }
    }

    if (isDb) {
        document.querySelectorAll('#sidebar-nav .sidebar-hidden-admin-area')
            .forEach(el => el.classList.toggle('hidden', !canAdmin));
        document.querySelectorAll('#sidebar-nav .sidebar-hidden-platform-scope')
            .forEach(el => el.classList.toggle('hidden', !isPlatformAdmin));
    } else {
        document.querySelectorAll('#sidebar-nav .sidebar-hidden-admin-area')
            .forEach(el => el.classList.toggle('hidden', false));
        document.querySelectorAll('#sidebar-nav .sidebar-hidden-platform-scope')
            .forEach(el => el.classList.toggle('hidden', false));
    }

    // Per-item availability from the authoritative projection. In "all" mode a
    // page is available if the backend signed it (available flag) regardless of
    // a read grant. When the projection is unknown (not yet loaded / legacy),
    // leave items as-is rather than hiding a page on a guess.
    const allMode = (mode === 'all');
    if (isDb && ctx) {
        document.querySelectorAll('#sidebar-nav .sidebar-item[data-view]').forEach(item => {
            const viewId = item.getAttribute('data-view');
            const key = _consolePageForView(viewId);
            if (!key) return; // not a signed page -> leave as-is
            const pageInfo = pages && pages[key];
            if (!pageInfo) return; // unknown key -> don't guess
            // A withheld menu grant hides the entry regardless of area.
            if (pageInfo.menu_denied === true) {
                item.classList.add('hidden');
                return;
            }
            // A capability the deployment withdrew is not offered either (task
            // 9.1): the entry is hidden, while a direct URL still reaches the
            // page body, which names the capability instead of pretending the
            // member lacks a grant.
            if (pageInfo.reason === 'capability_disabled') {
                item.classList.add('hidden');
                return;
            }
            // Workbench pages have no admin availability gate here.
            if (key.indexOf('admin.') !== 0) return;
            const available = allMode ? true : !!(pageInfo.available);
            const readOk = allMode ? true : !!(pageInfo.read_allowed);
            // A page is shown when it is available; if the identity may read it
            // but the consumer is closed, still show it as read-only/explained
            // rather than hiding a granted page (spec: keep consumer states
            // separate). Hide only when it is genuinely unavailable/denied.
            item.classList.toggle('hidden', !(available || readOk));
        });
        // 会话历史 (the nested recent-sessions block) is a workbench menu entry
        // without a `data-view` item, so gate it explicitly by its page key.
        const recentEl = document.getElementById('sidebar-recent');
        if (recentEl && typeof _sidebarRecentDenied === 'function') {
            recentEl.classList.toggle('hidden', _sidebarRecentDenied());
        }
        // A group whose every page was withheld is hidden along with them. Now
        // that the console entry is no longer admin-only an ordinary member
        // reaches this area, and an empty 组织与权限 / 模型与接入 heading would
        // advertise a surface the identity cannot read
        // (console-information-architecture: 不展示空分组). Visibility is
        // recomputed from the items on every pass, never accumulated, so
        // withdrawing a grant hides the group again.
        document.querySelectorAll('#sidebar-nav .menu-group.sidebar-hidden-admin-area')
            .forEach(group => {
                const items = group.querySelectorAll('.sidebar-item[data-view]');
                let reachable = false;
                items.forEach(item => {
                    if (!item.classList.contains('hidden')) reachable = true;
                });
                group.classList.toggle('hidden', !reachable);
            });
    }

    // Per-item: platform entries only for a platform admin.
    const platformEl = document.querySelector('.sidebar-item[data-view="platform"]');
    if (platformEl) platformEl.classList.toggle('hidden', !isPlatformAdmin);

    // Permission passes only toggle visibility, but they run after entry and on
    // every projection refresh; re-asserting the refined layout here keeps the
    // navigation order and the second launch button stable across them (task
    // 4.2). Off-flag this restores the markup order and hides the button.
    if (typeof applySidebarLaunchV2 === 'function') applySidebarLaunchV2();
}

function openAccountProfile() {
    if (!closeAccountPanels(false)) return;
    closeAccountMenu();
    _setAccountPanel('profile');
    _accountHidden('account-profile-drawer', false);
    _accountHidden('account-profile-content', true);
    _accountHidden('account-profile-status', true);
    resetAccountProfileEditor();
    fetchAccountSelf().then(() => {
        renderAccountProfile();
        focusAccountPanel('account-profile-drawer');
    }).catch(() => {
        _accountText('account-profile-status', t('account_profile_error'));
        _accountHidden('account-profile-status', false);
    });
}

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
    }
    _accountHidden('account-profile-member-section', false);
    const m = entry.membership || {};
    _accountText(_ACCOUNT_PROFILE_SEL.tenant, entry.name || entry.code || entry.id);
    _accountText(_ACCOUNT_PROFILE_SEL.memberName, m.display_name || t('account_profile_empty'));
    _accountChips(_ACCOUNT_PROFILE_SEL.role, (m.roles || []).map(r => r.name || r.code));
    _accountText(_ACCOUNT_PROFILE_SEL.department, m.department ? (m.department.name || m.department.id) : t('account_profile_empty'));
    _accountText(_ACCOUNT_PROFILE_SEL.position, m.position_text || t('account_profile_empty'));
}

function closeAccountProfile() {
    resetAccountProfileEditor();
    _accountHidden('account-profile-drawer', true);
    if (_activeAccountPanel === 'profile') _setAccountPanel(null);
    if (_accountAppVisible) document.getElementById('sidebar-account-toggle')?.focus();
}

// --- profile edit (read -> edit state) -----------------------------------

let _accountProfileEditing = false;
let _accountProfileAvatarUploading = false;

// Render the caller's own avatar (an uploaded picture, or the id-stable
// default) in the hero + menu.
//
// NOTE: this repaints the disc's whole innerHTML. The file input and the edit
// badge therefore live outside `#account-profile-avatar` (as siblings in
// `.account-profile-avatar-wrap`); nesting them here would delete them on every
// load and silently break the avatar picker.
function renderAccountProfileAvatar(user) {
    const box = document.getElementById('account-profile-avatar');
    if (!box) return;
    const u = user || {};
    const v = _accountAvatarVersion || u.id || Date.now();
    box.innerHTML = userAvatarHTML({ id: u.id, avatar: u.avatar, self: true, version: v });
}

// Changing the avatar is deliberately a double-click gesture: the disc sits in
// a hero the user also clicks at casually, and a stray single click must not
// open a native file dialog. `MouseEvent.detail` carries the click count, so
// the first click (1) is ignored and the second (2) opens the picker. Keyboard
// activation (Enter/Space) reports 0, so the disc stays operable without a
// pointer.
function handleAccountProfileAvatarActivate(event) {
    const detail = event && typeof event.detail === 'number' ? event.detail : 0;
    if (detail === 1) return;
    openAccountProfileAvatarPicker();
}

// Open the hidden file input for a new avatar.
function openAccountProfileAvatarPicker() {
    if (_accountProfileAvatarUploading) return;
    const input = document.getElementById('account-profile-avatar-input');
    if (!input) return;
    // Clear first: re-picking the *same* file must still fire `change`.
    input.value = '';
    input.click();
}

// Reset to the read-only state (clean inputs, exit edit mode).
function resetAccountProfileEditor() {
    _accountProfileEditing = false;
    _accountProfileAvatarUploading = false;
    const rows = document.querySelectorAll('#account-profile-content .account-profile-row');
    rows.forEach(r => r.classList.remove('editing'));
    _accountHidden('account-profile-edit-btn', false);
    _accountHidden('account-profile-save-btn', true);
    _accountHidden('account-profile-cancel-btn', true);
    _accountHidden('account-profile-close-btn', false);
    const hint = document.getElementById('account-profile-action-status');
    if (hint) { hint.classList.add('hidden'); hint.textContent = ''; }
    ['ap-display-name-input', 'ap-member-name-input', 'ap-position-input'].forEach(id => {
        const el = document.getElementById(id);
        if (el) { el.value = ''; el.classList.remove('invalid'); el.removeAttribute('aria-invalid'); }
        const err = document.getElementById(id + '-error');
        if (err) err.classList.add('hidden');
    });
}

// Enter edit mode: swap read rows for editable inputs and show save/cancel.
function startAccountProfileEdit() {
    const ctx = _accountSelf;
    if (!ctx || !ctx.user) return;
    if (_accountProfileEditing) return;
    _accountProfileEditing = true;

    // Populate inputs from the current projection.
    const user = ctx.user;
    const tid = sessionStorage.getItem('cow_tenant_id');
    const entry = (ctx.tenants || []).find(tn => tn.id === tid) || (ctx.tenants || [])[0];
    const m = (entry && entry.membership) || {};
    setInputValue('ap-display-name-input', user.display_name || '');
    setInputValue('ap-member-name-input', m.display_name || '');
    setInputValue('ap-position-input', m.position_text || '');

    // Only the editable rows are toggled; role/dept/tenant/username stay read.
    document.querySelectorAll('#account-profile-content .account-profile-row[data-field]')
        .forEach(r => r.classList.add('editing'));

    _accountHidden('account-profile-edit-btn', true);
    _accountHidden('account-profile-save-btn', false);
    _accountHidden('account-profile-cancel-btn', false);
    _accountHidden('account-profile-close-btn', true);
    const hint = document.getElementById('account-profile-action-status');
    if (hint) hint.classList.add('hidden');
    // Clear a field's inline error the moment the user starts typing in it.
    ['ap-display-name-input', 'ap-member-name-input', 'ap-position-input'].forEach(id => {
        const input = document.getElementById(id);
        if (!input || input.dataset.profileErrBound) return;
        input.dataset.profileErrBound = '1';
        input.addEventListener('input', () => {
            input.classList.remove('invalid');
            input.removeAttribute('aria-invalid');
            const err = document.getElementById(id + '-error');
            if (err) err.classList.add('hidden');
        });
    });
    // Move focus to the first editable input so the user can type immediately
    // (focusAccountPanel would land on the modal close/avatar button instead).
    const firstInput = document.getElementById('ap-display-name-input');
    if (firstInput) window.setTimeout(() => { firstInput.focus(); firstInput.select?.(); }, 0);
}

function setInputValue(id, value) {
    const el = document.getElementById(id);
    if (el) el.value = value || '';
}

// Leave edit mode without saving (same as cancel).
function cancelAccountProfileEdit() {
    if (_accountProfileAvatarUploading) return;
    resetAccountProfileEditor();
    renderAccountProfile();
}

// Highlight a profile input as invalid and show its inline error message.
function markFieldError(inputId, message) {
    const input = document.getElementById(inputId);
    if (input) {
        input.classList.add('invalid');
        input.setAttribute('aria-invalid', 'true');
        input.focus();
    }
    const err = document.getElementById(inputId + '-error');
    if (err) {
        err.textContent = message;
        err.classList.remove('hidden');
    }
}

// Persist the edited fields (display_name + member name/position).
function submitAccountProfile() {
    if (_accountProfileAvatarUploading || _accountWritePending) return;
    const ctx = _accountSelf;
    if (!ctx || !ctx.user) return;
    const displayName = document.getElementById('ap-display-name-input')?.value || '';
    const memberName = document.getElementById('ap-member-name-input')?.value || '';
    const position = document.getElementById('ap-position-input')?.value || '';
    const hint = document.getElementById('account-profile-action-status');
    const saveBtn = document.getElementById('account-profile-save-btn');

    // Client-side validation mirroring the backend guards.
    const displayNameVal = displayName.trim();
    const memberNameVal = memberName.trim();
    if (!displayNameVal) {
        markFieldError('ap-display-name-input', t('account_profile_error_required'));
        return;
    }
    // member name is optional only when no tenant; when editing a tenant member it
    // is required just like display name (mirrors backend guard).
    const tid = sessionStorage.getItem('cow_tenant_id');
    const hasTenant = (ctx.tenants || []).some(tn => tn.id === tid);
    if (hasTenant && !memberNameVal) {
        markFieldError('ap-member-name-input', t('account_profile_error_required'));
        return;
    }

    _accountWritePending = 'profile';
    if (saveBtn) saveBtn.disabled = true;
    if (hint) { hint.textContent = ''; hint.classList.add('hidden'); }
    const body = { display_name: displayNameVal };
    // Send member fields only when the caller belongs to a tenant.
    if (hasTenant) {
        body.member_display_name = memberNameVal;
        body.position_text = position.trim();
    }
    fetch('/auth/profile', {
        method: 'PATCH',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', ...(tid ? { 'X-Tenant-ID': tid } : {}) },
        body: JSON.stringify(body),
    }).then(r => r.json()).then(data => {
        if (data && data.status === 'success') {
            _accountSelf = data;
            resetAccountProfileEditor();
            renderAccountProfile();
            if (hint) { hint.textContent = t('saved'); hint.classList.remove('hidden'); }
        } else if (data && data.code === 'password_change_required') {
            if (hint) { hint.textContent = t('account_password_note'); hint.classList.remove('hidden'); }
        } else {
            if (hint) { hint.textContent = (data && data.message) || t('account_profile_error'); hint.classList.remove('hidden'); }
        }
    }).catch(() => {
        if (hint) { hint.textContent = t('account_profile_error'); hint.classList.remove('hidden'); }
    }).finally(() => {
        _accountWritePending = null;
        if (saveBtn) saveBtn.disabled = false;
        // Refresh the sidebar identity now that the write is no longer pending,
        // so the account button picks up the new display name.
        refreshAccountIdentity();
    });
}

// Upload a new avatar for the caller.
function uploadAccountProfileAvatar(file) {
    if (!file) return;
    if (_accountProfileAvatarUploading) return;
    _accountProfileAvatarUploading = true;
    const hint = document.getElementById('account-profile-action-status');
    const loader = document.getElementById('account-profile-avatar');
    if (loader) loader.classList.add('is-uploading');
    const form = new FormData();
    form.append('avatar', file);
    fetch('/auth/profile/avatar', { method: 'POST', credentials: 'same-origin', body: form })
        .then(r => r.json())
        .then(data => {
            if (data && data.status === 'success') {
                _accountAvatarVersion = String(Date.now());
                _accountSelf = data;
                renderAccountProfileAvatar(data.user || {});
                refreshAccountIdentity();
                if (hint) { hint.textContent = t('saved'); hint.classList.remove('hidden'); }
            } else {
                if (hint) { hint.textContent = (data && data.message) || t('account_profile_error'); hint.classList.remove('hidden'); }
            }
        })
        .catch(() => {
            if (hint) { hint.textContent = t('account_profile_error'); hint.classList.remove('hidden'); }
        })
        .then(() => {
            _accountProfileAvatarUploading = false;
            if (loader) loader.classList.remove('is-uploading');
        });
}


// --- change password ----------------------------------------------------

function openAccountPassword() {
    if (!closeAccountPanels(false)) return;
    closeAccountMenu();
    _setAccountPanel('password');
    _accountHidden('account-password-modal', false);
    _accountHidden('account-password-status', true);
    ['ap-old-password', 'ap-new-password', 'ap-confirm-password'].forEach(id => {
        const el = document.getElementById(id);
        if (el) { el.value = ''; delete el.dataset.dirty; }
    });
    focusAccountPanel('account-password-modal');
}

function cancelAccountPassword() {
    if (_forcedPassword) {
        // No dismissal: offer logout as the only way out. If confirmed, log out.
        if (window.confirm(t('account_password_forced_logout'))) handleLogout();
        return;
    }
    const dirty = ['ap-old-password', 'ap-new-password', 'ap-confirm-password']
        .some(id => document.getElementById(id)?.dataset.dirty);
    if (dirty && !window.confirm(t('unsaved_changes_warning'))) return;
    _clearPasswordInputs();
    closeAccountPassword();
}

function closeAccountPassword() {
    // Forced change must not be bypassed by closing the modal: the restricted
    // account can only set a new password or log out. Keep the gate open.
    if (_forcedPassword) return;
    _accountHidden('account-password-modal', true);
    _setAccountPanel(null);
    if (_accountAppVisible) document.getElementById('sidebar-account-toggle')?.focus();
}

function _clearPasswordInputs() {
    ['ap-old-password', 'ap-new-password', 'ap-confirm-password'].forEach(id => {
        const el = document.getElementById(id);
        if (el) { el.value = ''; delete el.dataset.dirty; }
    });
}

async function submitAccountPassword(event) {
    if (event) event.preventDefault();
    const oldPw = document.getElementById('ap-old-password')?.value || '';
    const newPw = document.getElementById('ap-new-password')?.value || '';
    const confirmPw = document.getElementById('ap-confirm-password')?.value || '';
    const status = document.getElementById('account-password-status');
    const submitBtn = document.getElementById('ap-submit-btn');
    if (!oldPw || !newPw || !confirmPw) {
        _accountPasswordStatus(t('account_password_unknown'), true);
        return;
    }
    if (newPw !== confirmPw) {
        _accountPasswordStatus(t('account_password_mismatch'), true);
        return;
    }
    if (_accountWritePending) return;
    _accountWritePending = 'password';
    if (submitBtn) submitBtn.disabled = true;
    _accountHidden('account-password-status', true);
    try {
        const resp = await fetch('/auth/password', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ old_password: oldPw, new_password: newPw }),
        });
        let data = null;
        try { data = await resp.json(); } catch (_) {}
        if (resp.status === 401 && data && data.code === 'invalid_old') {
            _accountPasswordStatus(t('account_password_invalid_old'), true);
            return;
        }
        if (resp.status === 400 && data && data.code === 'weak_password') {
            _accountPasswordStatus(t('account_password_weak'), true);
            return;
        }
        if (resp.status === 401) {
            // unauthorized -> session invalid; return to login
            showLoginScreen();
            return;
        }
        if (!resp.ok || !data || data.status !== 'success') {
            _accountPasswordStatus(t('account_password_unknown'), true);
            return;
        }
        // Success: session revoked & cookie cleared; force re-login.
        _accountPasswordStatus(t('account_password_done'), false);
        _clearPasswordInputs();
        _accountSelf = null;
        const wasForced = _forcedPassword;
        if (wasForced) _closeForcedPasswordModal();
        window.setTimeout(() => { showLoginScreen(); }, wasForced ? 700 : 900);
    } catch (_) {
        _accountPasswordStatus(t('account_password_unknown'), true);
    } finally {
        _accountWritePending = null;
        if (submitBtn) submitBtn.disabled = false;
    }
}

function _accountPasswordStatus(text, isError) {
    const status = document.getElementById('account-password-status');
    if (!status) return;
    status.textContent = text || '';
    status.classList.toggle('error', !!isError);
    status.classList.remove('hidden');
}

// --- preferences (theme + language, browser-local) -----------------------

function openAccountPrefs() {
    openAppearancePreferences(document.getElementById('sidebar-account-toggle'));
}

function closeAccountPrefs() {
    closeAppearancePreferences();
}

function _syncAccountPrefButtons() {
    renderAppearancePreferences();
}

function setAccountTheme(theme) {
    window.CowAppearance.setMode(theme);
}

function setAccountLang(lang) {
    if (lang !== 'zh' && lang !== 'zh-Hant' && lang !== 'en') return;
    // Personal preference: browser-local only, MUST NOT write instance config.
    setLanguageLocal(lang);
    _syncAccountPrefButtons();
    if (typeof window.__cowLang__ !== 'undefined') window.__cowLang__ = lang;
}

function _accountPrefStorageWarn() {
    languageStorageFailed = true;
    renderAppearancePreferences();
}

// --- about ---------------------------------------------------------------

// An openable external target: absolute http/https only. A relative value would
// silently become a same-origin page, and a non-http scheme (javascript:, data:)
// must never reach window.open.
function _externalUrlOrEmpty(value) {
    const raw = String(value == null ? '' : value).trim();
    if (!raw) return '';
    try {
        const url = new URL(raw);
        return (url.protocol === 'http:' || url.protocol === 'https:') ? url.href : '';
    } catch (_) {
        return '';
    }
}

// Adopt the address the public brand snapshot carried. An unusable or absent
// value keeps the current target rather than clearing it, so a failed refresh
// cannot leave the entry without a destination.
function _applyAccountAboutUrl(value) {
    // Only the integrated help path is accepted by this menu entry.
    _accountAboutUrl = ACCOUNT_ABOUT_FALLBACK_URL;
}

function openAccountAbout() {
    closeAccountMenu();
    // Same-origin help works before and after the public brand request.
    if (_accountAboutUrl) window.open(_accountAboutUrl, '_blank', 'noopener');
    _setAccountPanel(null);
}

// --- tenant switching (refresh-based) ------------------------------------

async function openAccountTenant() {
    closeAccountMenu();
    const epoch = _authEpoch;
    const self = await fetchAccountSelf();
    if (epoch !== _authEpoch) return;
    if (!self) {
        window.alert(t('account_profile_error'));
        return;
    }
    const tenants = Array.isArray(self.tenants) ? self.tenants : [];
    if (tenants.length === 0) {
        window.alert(t('account_tenant_no_available'));
        return;
    }
    if (tenants.length === 1) {
        window.alert(t('account_tenant_single'));
        return;
    }
    const current = sessionStorage.getItem('cow_tenant_id');
    // Lists available tenants; clicking a non-current tenant navigates via a
    // one-shot switch_tenant query param so the old page never pre-changes the
    // stored tenant. The new page validates before committing.
    const label = (tn) => (tn.name || tn.code || tn.id) + (tn.id === current ? ' (' + t('account_tenant_current') + ')' : '');
    const choice = window.prompt(t('account_tenant_title') + '\n' + tenants.map((tn, i) => (i + 1) + '. ' + label(tn)).join('\n'));
    if (!choice) return;
    const idx = parseInt(choice, 10) - 1;
    if (!Number.isInteger(idx) || idx < 0 || idx >= tenants.length) return;
    const target = tenants[idx];
    if (target.id === current) return;
    _applyTenantSwitch(target.id);
}

function _applyTenantSwitch(targetId) {
    if (!targetId || _accountWritePending) return;
    _accountWritePending = 'tenant';
    try {
        const url = new URL(window.location.href);
        url.searchParams.set('switch_tenant', targetId);
        // The old page does NOT change cow_tenant_id; the new page validates.
        window.location.assign(url.toString());
    } catch (_) {
        _accountWritePending = null;
    }
}

// --- shared focus helper -------------------------------------------------

function focusAccountPanel(panelId) {
    const panel = document.getElementById(panelId);
    if (!panel) return;
    const focusable = panel.querySelector('input, select, button, a');
    if (focusable) window.setTimeout(() => focusable.focus(), 0);
}

// Prevent accidental navigation/close when the password form has input.
function accountBeforeUnload(e) {
    if (_forcedPassword) return;  // forced gate may be exited via logout reload
    const dirty = ['ap-old-password', 'ap-new-password', 'ap-confirm-password']
        .some(id => document.getElementById(id)?.dataset.dirty);
    if (dirty || _activeAccountPanel === 'password') {
        e.preventDefault();
        e.returnValue = true;
    }
}
window.addEventListener('beforeunload', accountBeforeUnload);

// One-shot tenant switch validation on load: read the switch_tenant query
// param, validate it against /auth/me, then commit and strip the param.
function _resolveOneShotTenantSwitch() {
    try {
        const url = new URL(window.location.href);
        const target = url.searchParams.get('switch_tenant');
        const epoch = _authEpoch;
        if (!target || _identityMode() !== 'database') return Promise.resolve(false);
        return fetch('/auth/me', { credentials: 'same-origin', cache: 'no-store' })
            .then(r => r.json())
            .then(data => {
                if (epoch !== _authEpoch) return false;
                const valid = data && data.status === 'success'
                    && Array.isArray(data.tenants)
                    && data.tenants.some(tn => tn.id === target);
                if (valid) {
                    sessionStorage.setItem('cow_tenant_id', target);
                    if (typeof bumpTenantGeneration === 'function') bumpTenantGeneration();
                    // strip the one-shot param
                    url.searchParams.delete('switch_tenant');
                    if (typeof window.history !== 'undefined' && window.history.replaceState) {
                        window.history.replaceState(null, '', url.toString());
                    }
                    return true;
                }
                // invalid target: keep global login, do not silently enter another
                window.alert(t('account_tenant_invalid'));
                if (typeof bumpTenantGeneration === 'function') bumpTenantGeneration();
                url.searchParams.delete('switch_tenant');
                if (typeof window.history !== 'undefined' && window.history.replaceState) {
                    window.history.replaceState(null, '', url.toString());
                }
                return false;
            })
            .catch(() => false);
    } catch (_) {
        return Promise.resolve(false);
    }
}

// Mark password inputs dirty on change (for the beforeunload confirmation).
document.addEventListener('input', (e) => {
    if (e.target && ['ap-old-password', 'ap-new-password', 'ap-confirm-password'].includes(e.target.id)) {
        e.target.dataset.dirty = '1';
    }
});

window.openAccountProfile = openAccountProfile;
window.openAccountPassword = openAccountPassword;
window.closeAccountProfile = closeAccountProfile;
window.startAccountProfileEdit = startAccountProfileEdit;
window.cancelAccountProfileEdit = cancelAccountProfileEdit;
window.submitAccountProfile = submitAccountProfile;
window.uploadAccountProfileAvatar = uploadAccountProfileAvatar;
window.handleAccountProfileAvatarActivate = handleAccountProfileAvatarActivate;
window.openAccountProfileAvatarPicker = openAccountProfileAvatarPicker;
window.closeAccountPassword = closeAccountPassword;
window.cancelAccountPassword = cancelAccountPassword;
window.openAccountPrefs = openAccountPrefs;
window.closeAccountPrefs = closeAccountPrefs;
window.setAccountTheme = setAccountTheme;
window.setAccountLang = setAccountLang;
window.openAccountAbout = openAccountAbout;
window.openAccountTenant = openAccountTenant;
window.submitAccountPassword = submitAccountPassword;

// =====================================================================
// Initialization
// =====================================================================
applyTheme();
applyI18n();

// Refined workbench sidebar (change refine-sidebar-team-chat-launch, task 4.2):
// apply the presentation switch as soon as the sidebar markup exists, so the
// dual launch buttons and the navigation order are right on first paint. The
// sidebar is hidden behind the account gate until entry, but its layout is
// resolved here and re-applied after every permission pass.
if (typeof applySidebarLaunchV2 === 'function') applySidebarLaunchV2();

// Wire the change-password form submit (single submission).
(function () {
    const form = document.getElementById('account-password-form');
    if (form) form.addEventListener('submit', submitAccountPassword);
})();

refreshAccountIdentity();

// Coding Agents (change add-opencode-coding-agents, task 4.3): install the
// console's answers to the module's questions once. Inert when the module is
// absent (a deployment without the capability loads no coding.js at all), and
// the read-only service projection is fetched lazily by the two surfaces that
// display it -- a console that never opens them makes no request. Guarded
// because console.js runs in sections here and in its own tests.
if (typeof codingSeam === 'function') codingSeam().wire();

requestAnimationFrame(() => {
    document.body.classList.add('transition-colors', 'duration-200');
});
