let APP_VERSION = "";
/* rdai identity, brand and business extensions to the master console. */
const PRODUCT_NAME = '容大AI';

function readStartupPreference(key) {
    try { return localStorage.getItem(key); } catch (_) { return null; }
}

function _cowUserTenantKey(key) {
    const uid = (_accountState && _accountState.username) ? _accountState.username : '';
    const tid = sessionStorage.getItem('cow_tenant_id') || '';
    if (!uid && !tid) return key;      // not confirmed yet -> unpartitioned key
    return `${key}::u=${encodeURIComponent(uid)}::t=${encodeURIComponent(tid)}`;
}

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

const ACCOUNT_ABOUT_FALLBACK_URL = '/help/';

let _accountAboutUrl = ACCOUNT_ABOUT_FALLBACK_URL;

let _accountMenuSheet = false;

let _authContext = null;

let _authContextSeq = 0;

let _authContextRequest = null;

let _authContextPhase = 'unknown';

let _forcedPassword = false;

let _accountAvatarVersion = '';

function _accountText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}

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

function _accountAvatar(id, html) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html || '';
}

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
    } else if (state.phase === 'timeout') {
        name = t('account_check_timeout');
        subtitle = t('account_retry_hint');
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
    _accountHidden('account-menu-retry', !['error', 'logout_error', 'loading', 'timeout'].includes(state.phase));
    _accountHidden('account-menu-logout', !canLogout);
    _accountText('account-menu-logout-label', t(leaving ? 'account_logging_out' : logoutError ? 'account_retry_logout' : 'account_logout'));
    // Personal settings for an authenticated database account.
    const dbUser = hasUser;
    _accountHidden('account-menu-settings', !dbUser);
    ['account-menu-profile', 'account-menu-password', 'account-menu-tenant'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = !!_accountWritePending;
        _accountHidden(id, !dbUser);
    });
    ['account-menu-prefs', 'account-menu-customize', 'account-menu-about'].forEach(id => {
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
        _accountText('auth-check-message', t(
            state.phase === 'timeout' ? 'account_check_timeout'
                : state.phase === 'error' ? 'account_unavailable' : 'account_loading'));
        _accountHidden('auth-check-retry', !['error', 'timeout'].includes(state.phase));
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

function _accountMenuSheetChrome() {
    const active = _accountMenuSheetActive();
    const backdrop = document.getElementById('account-menu-backdrop');
    if (backdrop) backdrop.classList.toggle('hidden', !active);
    document.body.classList.toggle('account-menu-sheet-open', active);
}

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
    if (typeof Event === 'function' && typeof window.dispatchEvent === 'function') {
        window.dispatchEvent(new Event('account-invalidated'));
    }
    if (typeof resetMemoryView === 'function') resetMemoryView();
    resumedRequests.clear();
    if (typeof resetTimeline === "function") resetTimeline();
    if (typeof resetAgentWorkbenchFilters === 'function') resetAgentWorkbenchFilters(true);
    ++_accountCheckSeq;
    _accountCheckRequest = null;
    // Drop an unconsumed startup check: it belongs to the identity that just
    // changed and a late answer must not restore the old account. An already
    // consumed result keeps flowing through the ordinary epoch/sequence guard.
    const checkPrimitive = _accountCheckPrimitive();
    if (checkPrimitive && typeof checkPrimitive.discardStartup === 'function') {
        checkPrimitive.discardStartup();
    }
    _accountIdentityKey = null;
    _accountEntryRequest = null;
    ++_accountSelfSeq;
    _accountSelf = null;
    _accountSelfRequest = null;
    agentCatalog = [];
    chatAgentCatalog = null;
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
            || (previous.username && next.username && previous.username !== next.username)) {
        ++_authEpoch;
        if (typeof resetMemoryView === 'function') resetMemoryView();
    }
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
    let entrySelf = null;
    const request = Promise.resolve()
        // Authentication has established the mode before a tenant switch is
        // resolved. No business request may run until membership is confirmed.
        .then(() => current() ? fetchAccountSelf() : null)
        .then(self => {
            entrySelf = self;
            return current() ? _resolveOneShotTenantSwitch(self) : false;
        })
        .then(() => current() ? _ensureTenantSelected(entrySelf) : false)
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
                Promise.resolve(entrySelf).then(function (self) {
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
                    menuLoad().then(() => { if (current()) _bootAreaDefaultView(); });
                    openAuthGate();
                    if (typeof syncSessionHistorySurface === 'function') syncSessionHistorySurface();
                    // The projection is known now: mount the context entry with
                    // the authoritative per-action availability (a no-op without
                    // the module, and hidden when both actions are closed).
                    if (typeof mountContextModule === 'function') mountContextModule();
                    if (typeof Event === 'function' && typeof window.dispatchEvent === 'function') {
                        window.dispatchEvent(new Event('account-ready'));
                    }
                });
                if (_identityMode() === 'database') _setupHeaderTenantSelector(entrySelf);
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

function _accountCheckPrimitive() {
    return typeof window !== 'undefined' ? window.__cowAccountCheck__ : null;
}

function _startupCheckFresh(startup, primitive) {
    if (!startup || typeof startup.issuedAt !== 'number') return false;
    const maxAge = Number(primitive && primitive.STARTUP_MAX_AGE_MS) || 5000;
    return Date.now() - startup.issuedAt <= maxAge;
}

function _accountCheckOutcome() {
    const primitive = _accountCheckPrimitive();
    if (!primitive || typeof primitive.request !== 'function') {
        return Promise.resolve({ kind: 'error', error: new Error('Account check unavailable') });
    }
    // `request()` hands back a cancellable handle whose `outcome` is the
    // captured result; an injected test double may return the promise directly.
    const startFresh = () => {
        const handle = primitive.request();
        return handle && handle.outcome ? handle.outcome : handle;
    };
    const startup = typeof primitive.takeStartup === 'function' ? primitive.takeStartup() : null;
    if (!startup) return startFresh();
    return Promise.resolve(startup.outcome).then(outcome => {
        if (outcome && outcome.kind === 'response' && !_startupCheckFresh(startup, primitive)) {
            return startFresh();
        }
        return outcome;
    });
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
            // Global identity is independent of the selected tenant. The
            // primitive issues the fixed endpoint without a tenant header and
            // captures its own errors, so no startup failure rejects here.
            const outcome = await _accountCheckOutcome();
            if (!current()) return;
            // A bounded wait that expired is recoverable, not an anonymous or
            // authenticated conclusion: show the timeout copy and the existing
            // retry entry instead of guessing a login mode.
            if (outcome.kind === 'timeout') {
                _accountState = _emptyAccount('timeout');
                if (!_accountAppVisible) _showAccountCheckGate();
                else _renderSidebarAccount();
                return;
            }
            if (outcome.kind !== 'response' || !outcome.validBody) {
                throw new Error('Authentication check failed');
            }
            if (outcome.status === 401) { showLoginScreen(); return; }
            if (outcome.status < 200 || outcome.status >= 300) {
                throw new Error('Authentication check failed');
            }
            const next = _normalizeAccountCheck(outcome.data);
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

function productTitle(title) {
    if (title && /^cowagent$/i.test(title.trim())) return 'RongAI';
    return !title || /^ai assistant$/i.test(title.trim()) ? PRODUCT_NAME : title;
}

function productTitleHTML(title) {
    const name = productTitle(title);
    return name === PRODUCT_NAME ? '容大<span class="brand-ai">AI</span>' : escapeHtml(name);
}

const DEFAULT_BRAND = {
    enabled: false,
    revision: 0,
    brand_name: '容大AI',
    logo_description: '业务工作台',
    logo_url: '/assets/rongda-ai-mark.svg',
    favicon_url: '/assets/favicon.ico',
};

let brandState = { ...DEFAULT_BRAND };

let brandLoaded = false;

let brandFetchSeq = 0;

let brandSaveEpoch = 0;

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

function welcomeHeroDescription() {
    const desc = effectiveLogoDescription();
    const isDefault = _isDefaultLogoDescription(desc);
    return (desc && desc.trim() && !isDefault) ? desc : '';
}

function brandWordmarkHTML(name) {
    const value = productTitle(name || DEFAULT_BRAND.brand_name);
    return value === PRODUCT_NAME ? '容大<span class="brand-ai">AI</span>' : escapeHtml(value);
}

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

    // Preserve the brand while reflecting the active navigation area.
    const consoleLabel = t(_navAreaFromPath(location.pathname) === 'admin' ? 'nav_admin_console' : 'nav_workbench');
    document.title = `${name} ${consoleLabel}`.trim();

    // Favicon (derived PNG served by the backend; falls back to the built-in)
    const favUrl = effectiveFaviconUrl();
    document.querySelectorAll('link[rel="icon"]').forEach(link => {
        link.href = `${favUrl}?v=${brandFetchSeq}`;
    });
}

function applyBrandToAgentAvatars() {
    document.querySelectorAll('.agent-avatar-brand').forEach(img => {
        _brandArmFallback(img, effectiveLogoUrl());
    });
}

function setLanguageLocal(lang) {
    const next = (lang === 'en' || lang === 'zh' || lang === 'zh-Hant') ? lang : 'zh';
    applyLanguage(next, /* writeToBackend */ false);
}

let languageStorageFailed = false;

function applyLanguage(next, writeToBackend) {
    currentLang = next;
    window.__cowLang__ = currentLang;
    try {
        localStorage.setItem('cow_lang', currentLang);
        languageStorageFailed = localStorage.getItem('cow_lang') !== currentLang;
    } catch (_) { languageStorageFailed = true; }
    applyI18n();
    _applyNavAreaAttribute();
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

const CONSOLE_VIEW_REGISTRY = [];

function registerConsoleView(spec) {
    if (!spec || !spec.id) return;
    const index = CONSOLE_VIEW_REGISTRY.findIndex(entry => entry.id === spec.id);
    if (index >= 0) CONSOLE_VIEW_REGISTRY[index] = spec;
    else CONSOLE_VIEW_REGISTRY.push(spec);
}

function _registeredConsoleView(viewId) {
    return CONSOLE_VIEW_REGISTRY.find(entry => entry.id === viewId) || null;
}

function _activateViewContainer(viewId) {
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    const target = document.getElementById('view-' + viewId);
    if (target) target.classList.add('active');
}

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

const LAZY_VIEW_MODULES = {
    external_connections: '/assets/js/external-connections.js',
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

function _lazyViewModuleFailed(viewId) {
    const container = document.getElementById('view-' + viewId);
    if (!container) return;
    const message = 'Failed to load assets/js/' + viewId + '.js';
    container.innerHTML = '<div class="p-6 text-sm text-red-500" role="alert">' +
        message.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])) +
        '</div>';
}

function _repaintRegisteredView(viewId) {
    const entry = _registeredConsoleView(viewId);
    if (entry && typeof entry.repaint === 'function') entry.repaint();
}

function _viewTargetArea(viewId) {
    const registered = window.ConsoleRoutes?.pages.find(page => page.view === viewId);
    if (registered) return registered.area;
    if (viewId === 'admin-home') return 'admin';
    const meta = VIEW_META[viewId];
    const key = meta && meta.console;
    if (key && String(key).indexOf('admin.') === 0) return 'admin';
    return 'workbench';
}

function _bootAreaDefaultView() { routeApply(); }

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

const UNAVAILABLE_VIEWS = new Set(['backup', 'open_api']);

function showUnavailableView(viewId, reason) {
    // reason: undefined/'' -> feature not yet open (nav_unavailable);
    // 'denied' -> the identity lacks read access (nav_denied, distinct copy).
    // A denied target does NOT silently switch scope: we render the denial
    // explanation and only offer a way back.
    const denied = reason === 'denied';
    currentView = viewId;
    if (typeof syncSessionHistorySurface === 'function') syncSessionHistorySurface();
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

let agentNavigationVersion = 0;

const LEGACY_PERSONAL_FORWARD = {
    'personal-agents': 'agents',      // 智能体管理, filtered to the caller's range
    'personal-channels': 'channels',  // 消息渠道, member = their own connections
    'personal-memory': 'memory',      // 记忆管理, target set served by the backend
    'personal-tools': 'skills',       // 工具与技能
    'personal-skills': 'skills',      // 工具与技能
};

function legacyPersonalForward(viewId) {
    const target = LEGACY_PERSONAL_FORWARD[viewId];
    return (target && VIEW_META[target]) ? target : '';
}

function _normalizeViewId(viewId) {
    return viewId === 'external-connections' ? 'external_connections' : viewId;
}

let _navApprovedTarget = null;

function _viewLeaveApproved(viewId) {
    return _navApprovedTarget === viewId;
}

function _viewLeaveCheck(viewId, onArrive, routeOptions) {
    if (viewId === currentView) return true;
    const adminViews = ['tenant', 'system_user', 'roles', 'org', 'platform', 'audit'];
    if (currentView === 'branding' && brandingDirty) {
        brandingConfirmDiscard(() => {
            _brandingResetDraftToBaseline();
            navigateTo(viewId, onArrive, routeOptions);
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

function _initAccountMenuChrome() {
    if (typeof document.getElementById !== 'function') return;
    // Tapping the backdrop of the mobile sheet closes it, like the drawer behind
    // it; the sheet itself never closes on an inner click.
    document.getElementById('account-menu-backdrop')
        ?.addEventListener('click', () => closeAccountMenu(true));
}

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

function sidebarLaunchV2() {
    if (typeof window === 'undefined') return false;
    return String(window.__COW_WORKBENCH_SIDEBAR_LAUNCH_V2__ || '0').trim() === '1';
}

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

function syncSidebarToggleState() {
    const expanded = window.innerWidth >= 1024
        ? !document.getElementById('app').classList.contains('sidebar-collapsed')
        : !document.getElementById('sidebar').classList.contains('-translate-x-full');
    document.getElementById('menu-toggle')?.setAttribute('aria-expanded', String(expanded));
}

let chatAgentCatalog = null;

let userDefault = { agent_id: '', revision: null, origin: null };

let tenantDefaultManageable = false;

let defaultResolution = { agent_id: '', source: null };

let installedTools = [];

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

function chatAgents() {
    const roster = chatAgentCatalog || agentCatalog;
    return roster.filter(a => a.enabled !== false && a.can_chat !== false);
}

function availableChatAgents() {
    return chatAgents();
}

function teamCandidateAgents() {
    const roster = Array.isArray(chatAgentCatalog) ? chatAgentCatalog : [];
    return roster.filter(agent => agent && agent.enabled !== false && agent.can_chat !== false
        && !agentIsCoding(agent));
}

function agentIsCoding(agent) {
    return !!agent && agent.agent_type === 'coding';
}

let _chatCatalogRequest = null;

function loadChatAgentCatalog() {
    const epoch = _authEpoch, tenant = sessionStorage.getItem('cow_tenant_id');
    const key = epoch + ':' + tenant;
    if (_chatCatalogRequest?.key === key) return _chatCatalogRequest.promise;
    const request = fetchAgentWorkbench().then(agents => {
        if (epoch !== _authEpoch || tenant !== sessionStorage.getItem('cow_tenant_id')) return;
        chatAgentCatalog = agents;
        // The composer face, both launch controls and any open picker are drawn
        // from this roster, so a list that arrives after the first paint repaints
        // them instead of leaving the earlier (narrower) reading on screen.
        renderComposerIdentity();
        syncNewChatControls();
        if (typeof consumeScenarioOpenLink === 'function') consumeScenarioOpenLink();
        return agents;
    }).finally(() => {
        if (_chatCatalogRequest?.promise === request) _chatCatalogRequest = null;
    });
    _chatCatalogRequest = { key, promise: request };
    return request;
}

function withManagementCatalog(viewId, render) {
    if (agentCatalog.length) return render();
    const epoch = _authEpoch, tenant = sessionStorage.getItem('cow_tenant_id');
    return loadAgentCatalog().then(() => {
        if (currentView === viewId && epoch === _authEpoch
                && tenant === sessionStorage.getItem('cow_tenant_id')) return render();
    });
}

let agentWorkbench = [];

let agentWorkbenchLoading = false;

let agentWorkbenchSeq = 0;

let _wbNoticeKey = '';

let _wbEmptyReason = '';

let _wbSearchQuery = '';

let _wbSelectedTag = null;

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

function _wbSearchValue(event) {
    const input = document.getElementById('agent-workbench-search');
    if (input && typeof input.value === 'string') return input.value;
    const target = event && event.target;
    return target && typeof target.value === 'string' ? target.value : '';
}

function onAgentWorkbenchSearch(event) {
    // A keystroke inside a composition is pre-edit text: the pinyin, not the
    // word being written, so it must not narrow the list.
    if (event && event.isComposing) return;
    // A committed input event proves the composition ended. Recovering the flag
    // here keeps a dropped ``compositionend`` from freezing the search: every
    // later plain keystroke would otherwise be ignored as well.
    if (event && event.isComposing === false) _wbSearchComposing = false;
    if (_wbSearchComposing) return;
    _wbSearchQuery = _wbSearchValue(event);
    paintAgentWorkbench();
    const grid = document.getElementById('agent-workbench-grid');
    if (grid) grid.scrollTop = 0;
}

function onAgentWorkbenchSearchKeydown(event) {
    if (event.key === 'Escape' && !event.isComposing && event.keyCode !== 229) {
        event.preventDefault();
        clearAgentWorkbenchSearch();
        return;
    }
    if (event.key !== 'Enter' || event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    _wbSearchComposing = false;
    onAgentWorkbenchSearch(event);
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

function _wbFailureKey(status, code) {
    if (code === 'missing_tenant') return 'agent_workbench_no_tenant';
    if (status === 403 || code === 'forbidden') return 'agent_workbench_no_permission';
    if (status === 401 || code === 'unauthorized') return 'agent_workbench_signed_out';
    return 'agent_workbench_failed';
}

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
        // The file panel needs the server's ownership shape to open this user's directory.
        visibility: a.visibility,
        // Digital-employee projection fields (positioned to render on cards).
        position: a.position || '', category: a.category || '', tags: agentWorkbenchTags(a),
        greeting: typeof a.greeting === 'string' ? a.greeting : '',
        usage_hint: typeof a.usage_hint === 'string' ? a.usage_hint : '',
        suggested_questions: Array.isArray(a.suggested_questions)
            ? a.suggested_questions.filter(q => typeof q === 'string' && q.trim()).slice(0, 4)
            : [],
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

function agentAnchorHintText() {
    const source = defaultResolution.source;
    if (source === 'user') return t('agents_anchor_source_user');
    if (source === 'tenant') return t('agents_anchor_source_tenant');
    if (source === 'shared') return t('agents_anchor_source_shared');
    if (source === 'own') return t('agents_anchor_source_own');
    return t('agents_anchor_source_unknown');
}

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

let _wbErrorKey = '';

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

function agentVisibilityBadgeHTML(agent, options) {
    if (!agent || !agent.visibility) return '';
    const shared = agent.visibility === 'tenant';
    // The list corner is a stacked chip (see ``agent-card-badges``); the detail
    // header needs the same pill inline, where the corner positioning would put
    // it on top of the name.
    const base = (options && options.inline) ? 'agent-visibility-chip' : 'agent-card-badge';
    const label = t(shared ? 'agents_visibility_tenant' : 'agents_visibility_private');
    return `<span class="${base} agent-visibility-${shared ? 'tenant' : 'private'}">${escapeHtml(label)}</span>`;
}

function agentVisibilityActionsHTML(agent) {
    if (!agent) return '';
    const id = escapeHtml(agent.id);
    if (agent.can_share) {
        return `<button type="button" onclick="shareAgent('${id}')" class="agent-btn agent-btn-ghost">${escapeHtml(t('agents_share'))}</button>`;
    }
    if (agent.can_unshare) {
        return `<button type="button" onclick="startAgentUnshare('${id}')" class="agent-btn agent-btn-ghost">${escapeHtml(t('agents_unshare'))}</button>`;
    }
    return '';
}

const _capabilitySaveState = {};

function saveAgentCapabilities(agent, fields) {
    // Apply optimistically so the pane reflects the new state immediately.
    if ('skills' in fields) agent.skills = fields.skills;
    if ('sops' in fields) agent.sops = fields.sops;
    if ('tools_allowlist' in fields) agent.tools_allowlist = fields.tools_allowlist;
    if ('tools_denylist' in fields) agent.tools_denylist = fields.tools_denylist;
    const st = _capabilitySaveState[agent.id] || (_capabilitySaveState[agent.id] = { inflight: false, pending: null });
    if (st.inflight) {
        st.pending = Object.assign(st.pending || {}, fields);
        return;
    }
    st.inflight = true;
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
    }).catch(() => {
        const status = document.getElementById('agent-editor-status');
        if (status) status.textContent = t('agents_save_failed');
    }).then(() => {
        st.inflight = false;
        if (st.pending) {
            const next = st.pending;
            st.pending = null;
            saveAgentCapabilities(agent, next);
        }
    });
}

function shareAgent(agentId) {
    const agent = findAgent(agentId);
    if (!agent || !agent.can_share) return;
    showConfirmDialog({
        title: t('agents_share_confirm_title'),
        message: t('agents_share_confirm_body'),
        okText: t('agents_share'),
        // Returned so a caller (and the tests) can await the write; the dialog
        // itself only ignores the value.
        onConfirm: () => postAgentVisibility(agentId, 'tenant', ''),
    });
}

async function startAgentUnshare(agentId) {
    const agent = findAgent(agentId);
    const row = document.getElementById('agent-visibility-owner');
    if (!agent || !agent.can_unshare || !row) return;
    row.classList.remove('hidden');
    row.innerHTML = `<div class="agent-field-hint">${escapeHtml(t('agents_unshare_loading'))}</div>`;
    let members = [];
    try {
        const res = await fetch('/api/tenant/members?page_size=200');
        const data = await res.json();
        members = (data && data.items) || [];
    } catch (e) {
        members = [];
    }
    const options = members
        .filter(m => m && m.user_id && m.active !== 0)
        .map(m => `<option value="${escapeHtml(m.user_id)}">${escapeHtml(m.display_name || m.username || m.user_id)}</option>`)
        .join('');
    row.innerHTML = `
        <div class="agent-field">
            <label class="agent-field-label">${escapeHtml(t('agents_unshare_owner_label'))}</label>
            <div class="flex items-center gap-2">
                <select id="agent-unshare-owner" class="agent-input">
                    <option value="">${escapeHtml(t('agents_unshare_owner_placeholder'))}</option>
                    ${options}
                </select>
                <button type="button" class="agent-btn agent-btn-primary" onclick="confirmAgentUnshare('${escapeHtml(agent.id)}')">${escapeHtml(t('save'))}</button>
                <button type="button" class="agent-btn agent-btn-ghost" onclick="cancelAgentUnshare()">${escapeHtml(t('channels_cancel'))}</button>
            </div>
            <p class="agent-field-hint">${escapeHtml(t('agents_unshare_owner_hint'))}</p>
        </div>`;
}

function cancelAgentUnshare() {
    const row = document.getElementById('agent-visibility-owner');
    if (!row) return;
    row.classList.add('hidden');
    row.innerHTML = '';
}

function confirmAgentUnshare(agentId) {
    const picker = document.getElementById('agent-unshare-owner');
    const ownerUserId = picker ? picker.value : '';
    // 没选归属人就不提交。服务端也会拒绝（bad_request），但没必要先发一个注定失败的
    // 请求，再让用户从错误里反推「原来要选人」。
    if (!ownerUserId) {
        const status = document.getElementById('agent-profile-status');
        if (status) status.textContent = t('agents_unshare_owner_required');
        return;
    }
    postAgentVisibility(agentId, 'private', ownerUserId);
}

async function postAgentVisibility(agentId, visibility, ownerUserId) {
    const status = document.getElementById('agent-profile-status');
    if (status) status.textContent = t('agents_visibility_working');
    const body = { action: 'set_visibility', id: agentId, visibility };
    if (ownerUserId) body.owner_user_id = ownerUserId;
    let data = null;
    try {
        const res = await fetch('/api/agents', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        data = await res.json();
    } catch (e) {
        data = null;
    }
    // A refusal must never read as success, and must not leave the pane's
    // optimistic state claiming the object moved: the list is re-read only on
    // success, so a failure keeps the rows exactly as the server last described
    // them.
    if (!data || data.status !== 'success') {
        if (status) status.textContent = agentVisibilityErrorText(data);
        return false;
    }
    cancelAgentUnshare();
    await loadAgentCatalog();
    // Both directions usually take the object out of *this* caller's management
    // range — sharing hands it to the whole tenant, restoring hands it to one
    // member — so the pane is often already gone here. The row leaving the list
    // is then the confirmation, and it is exactly what the dialog promised.
    const statusNow = document.getElementById('agent-profile-status');
    if (statusNow) {
        statusNow.textContent = t(visibility === 'tenant' ? 'agents_share_done' : 'agents_unshare_done');
    }
    return true;
}

function agentVisibilityErrorText(data) {
    const code = data && data.code;
    if (code === 'agent_is_tenant_default') return t('agents_unshare_tenant_default');
    if (code === 'forbidden') return t('agents_visibility_forbidden');
    if (code === 'not_found') return t('agents_visibility_not_found');
    if (code === 'bad_request' || code === 'invalid_agent_id') return t('agents_visibility_failed');
    return (data && data.message) || t('agents_visibility_failed');
}

function renderAgentCapabilitiesPane() {
    const pane = document.getElementById('agent-detail-skills');
    const agent = findAgent(selectedAdminAgentId);
    if (!pane || !agent) return;
    // The keyword is per Agent: switching Agents starts from the full catalog
    // again, while repainting the same Agent (a tool toggle) keeps what was
    // typed. `_capabilitySearch` lives below this function so the pane's own
    // unit tests, which load a slice of console.js, keep it in scope.
    if (_capabilitySearch.agentId !== agent.id) {
        _capabilitySearch.agentId = agent.id;
        _capabilitySearch.query = '';
    }
    // The tool selection state is derived from the Agent on each pass, because
    // the row painter is also reached from the keyword handler (after a save
    // moved the allow/deny lists) and not only from a full repaint.
    const toolState = () => {
        const tools = installedTools || [];
        const allowSet = new Set(agent.tools_allowlist || []);
        const denySet = new Set(agent.tools_denylist || []);
        // Static assets can update before the server restarts. Older catalogs
        // omit this flag, but delivery already enforced explicit binding then.
        const requiresExplicitBinding = tool => tool.requires_explicit_binding
            ?? (tool.name === 'requirements_delivery');
        const toolSelected = tool => ((!allowSet.size && !requiresExplicitBinding(tool))
            || allowSet.has(tool.name)) && !denySet.has(tool.name);
        return { tools, requiresExplicitBinding, toolSelected };
    };
    const render = () => {
        const all = agent.skills == null;
        const { tools, requiresExplicitBinding, toolSelected } = toolState();
        const selectedToolCount = tools.filter(toolSelected).length;
        const allTools = tools.length > 0 && selectedToolCount === tools.length;
        pane.innerHTML = `
            <div class="agent-cap-search">
                <i class="fas fa-magnifying-glass agent-cap-search-icon"></i>
                <input type="text" id="agent-cap-search" class="agent-cap-search-input"
                    autocomplete="off" spellcheck="false"
                    placeholder="${escapeHtml(t('agents_cap_search_placeholder'))}"
                    value="${escapeHtml(_capabilitySearch.query)}">
                <button type="button" id="agent-cap-search-clear" class="agent-cap-search-clear"
                    aria-label="${escapeHtml(t('agents_cap_search_clear'))}"
                    title="${escapeHtml(t('agents_cap_search_clear'))}"${_capabilitySearch.query ? '' : ' hidden'}>
                    <i class="fas fa-xmark"></i>
                </button>
            </div>
            <div class="agent-cap-section">
                <div class="agent-cap-title">${escapeHtml(t('agents_skills_label'))}</div>
                <label class="flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300 mb-3">
                    <input type="checkbox" id="agent-skills-all" ${all ? 'checked' : ''}>
                    <span>${escapeHtml(t('agents_skills_all'))}</span>
                </label>
                <p class="text-xs text-slate-400 mb-3">${escapeHtml(t('agents_skills_pick'))}</p>
                <div id="agent-skills-list"></div>
            </div>
            <div class="agent-cap-section">
                <div class="agent-cap-title">${escapeHtml(t('agents_tools_label'))}</div>
                <label class="flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300 mb-3">
                    <input type="checkbox" id="agent-tools-all" ${allTools ? 'checked' : ''} ${tools.length ? '' : 'disabled'}>
                    <span>${escapeHtml(t('agents_tools_all'))}</span>
                </label>
                <p class="text-xs text-slate-400 mb-2">${escapeHtml(t('agents_tools_hint'))}</p>
                <div class="agent-tool-grid" id="agent-tools-list"></div>
            </div>`;
        // The keyword box lives outside both lists so typing never rebuilds it:
        // a rebuilt input would drop focus, the caret and any IME composition.
        const searchInput = document.getElementById('agent-cap-search');
        if (searchInput) {
            searchInput.addEventListener('input', () => {
                _capabilitySearch.query = searchInput.value || '';
                paintCapabilityLists();
            });
        }
        const searchClear = document.getElementById('agent-cap-search-clear');
        if (searchClear) {
            searchClear.addEventListener('click', () => {
                _capabilitySearch.query = '';
                if (searchInput) { searchInput.value = ''; searchInput.focus(); }
                paintCapabilityLists();
            });
        }
        document.getElementById('agent-skills-all')?.addEventListener('change', (e) => {
            const next = e.target.checked ? null : [];
            saveAgentCapabilities(agent, { skills: next });
            paintSkillList();  // repaint the rows in place — no reload, no flicker
        });
        const toolsAllBox = document.getElementById('agent-tools-all');
        if (toolsAllBox) {
            toolsAllBox.indeterminate = selectedToolCount > 0 && !allTools;
            toolsAllBox.addEventListener('change', () => {
                const nextDeny = new Set(agent.tools_denylist || []);
                tools.forEach(tool => {
                    if (toolsAllBox.checked) nextDeny.delete(tool.name);
                    else nextDeny.add(tool.name);
                });
                // Selecting all must also opt in to tools that cannot inherit
                // defaults. Keep allowlist entries outside the visible catalog.
                const nextAllow = toolsAllBox.checked
                    ? (tools.some(requiresExplicitBinding)
                        ? Array.from(new Set([...(agent.tools_allowlist || []), ...tools.map(tool => tool.name)]))
                        : null)
                    : (agent.tools_allowlist ?? null);
                saveAgentCapabilities(agent, {
                    tools_allowlist: nextAllow,
                    tools_denylist: Array.from(nextDeny),
                });
                render();
            });
        }
        paintCapabilityLists();
    };
    // Rows are drawn on their own so a keystroke never rebuilds the keyword
    // box. The working set is the Agent's *full* selection, not the visible
    // rows: a keyword hides rows, and a hidden row must still be submitted when
    // a visible one is toggled (otherwise filtering silently unbinds skills).
    function paintSkillList() {
        const list = document.getElementById('agent-skills-list');
        if (!list) return;
        const all = agent.skills == null;
        const picked = new Set(all ? [] : (agent.skills || []));
        const matches = (installedSkills || []).filter(skill => skillMatchesQuery(skill, _capabilitySearch.query));
        if (!matches.length) {
            list.innerHTML = _capabilitySearch.query
                ? `<p class="agent-skill-no-match">${escapeHtml(t('agents_skills_search_empty'))}</p>`
                : '';
        } else {
            list.innerHTML = matches.map(skill => {
                const name = skill.name || skill.id;
                const checked = all || picked.has(name);
                return `<label class="agent-skill-row">
                    <input type="checkbox" class="agent-skill-item" value="${escapeHtml(name)}" ${checked ? 'checked' : ''} ${all ? 'disabled' : ''}>
                    <div>
                        <div class="text-sm text-slate-700 dark:text-slate-200">${escapeHtml(skill.display_name || name)}</div>
                        <div class="text-xs text-slate-400">${escapeHtml(skill.description || '')}</div>
                    </div>
                </label>`;
            }).join('');
            list.querySelectorAll('.agent-skill-item').forEach(box => {
                box.addEventListener('change', () => {
                    if (box.checked) picked.add(box.value); else picked.delete(box.value);
                    saveAgentCapabilities(agent, { skills: orderAgentSkillNames(picked) });
                });
            });
        }
        const clear = document.getElementById('agent-cap-search-clear');
        if (clear) clear.hidden = !_capabilitySearch.query;
    }
    // The tool catalogue is repainted the same way, so one keyword narrows both
    // lists without either repaint touching the box the user is typing in.
    function paintToolList() {
        const list = document.getElementById('agent-tools-list');
        if (!list) return;
        const { tools, requiresExplicitBinding, toolSelected } = toolState();
        const matches = tools.filter(tool => toolMatchesQuery(tool, _capabilitySearch.query));
        if (!matches.length) {
            list.innerHTML = _capabilitySearch.query
                ? `<p class="agent-skill-no-match">${escapeHtml(t('agents_tools_search_empty'))}</p>`
                : '';
            return;
        }
        list.innerHTML = matches.map(tool => {
            return `<label class="agent-skill-row agent-tool-row">
                <input type="checkbox" class="agent-tool-item" value="${escapeHtml(tool.name)}" ${toolSelected(tool) ? 'checked' : ''}>
                <div class="min-w-0 flex-1">
                    <div class="text-sm text-slate-700 dark:text-slate-200 font-mono">${escapeHtml(tool.name)}</div>
                    <div class="text-xs text-slate-400 truncate">${escapeHtml((tool.description || '').split('\n')[0])}</div>
                </div>
            </label>`;
        }).join('');
        list.querySelectorAll('.agent-tool-item').forEach(box => {
            box.addEventListener('change', () => {
                const name = box.value;
                const nextAllow = new Set(agent.tools_allowlist || []);
                const nextDeny = new Set(agent.tools_denylist || []);
                if (box.checked) {
                    nextDeny.delete(name);
                    const tool = tools.find(tool => tool.name === name);
                    if (!nextAllow.size && tool && requiresExplicitBinding(tool)) {
                        // Switching from inherited defaults to an explicit
                        // allowlist must retain the other selected tools.
                        tools.filter(toolSelected).forEach(tool => nextAllow.add(tool.name));
                        nextAllow.add(name);
                    }
                    if (nextAllow.size) nextAllow.add(name);
                } else {
                    // Empty allowlists inherit defaults, so exclusions must be
                    // saved as denials even when the last tool is unchecked.
                    nextDeny.add(name);
                }
                saveAgentCapabilities(agent, {
                    tools_allowlist: nextAllow.size ? Array.from(nextAllow) : null,
                    tools_denylist: Array.from(nextDeny),
                });
                render();
            });
        });
    }
    function paintCapabilityLists() {
        paintSkillList();
        paintToolList();
    }
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

let _capabilitySearch = { agentId: '', query: '' };

function _fuzzyIdentifierHit(text, needle, compactNeedle) {
    const hay = String(text == null ? '' : text).toLowerCase();
    if (!hay) return false;
    if (hay.includes(needle)) return true;
    const compact = hay.replace(/[\s\-_]+/g, '');
    if (!compactNeedle) return false;
    if (compact.includes(compactNeedle)) return true;
    let i = 0;
    for (let c = 0; c < compact.length && i < compactNeedle.length; c++) {
        if (compact[c] === compactNeedle[i]) i++;
    }
    return i === compactNeedle.length;
}

function _fuzzyTextHit(text, needle) {
    return String(text == null ? '' : text).toLowerCase().includes(needle);
}

function skillMatchesQuery(skill, query) {
    const needle = String(query == null ? '' : query).trim().toLowerCase();
    if (!needle) return true;
    const compactNeedle = needle.replace(/[\s\-_]+/g, '');
    return _fuzzyIdentifierHit(skill.name || skill.id, needle, compactNeedle)
        || _fuzzyIdentifierHit(skill.display_name, needle, compactNeedle)
        || _fuzzyTextHit(skill.description, needle);
}

function toolMatchesQuery(tool, query) {
    const needle = String(query == null ? '' : query).trim().toLowerCase();
    if (!needle) return true;
    const compactNeedle = needle.replace(/[\s\-_]+/g, '');
    return _fuzzyIdentifierHit(tool.name || tool.id, needle, compactNeedle)
        || _fuzzyTextHit(tool.description, needle);
}

function orderAgentSkillNames(picked) {
    const names = [];
    const seen = new Set();
    (installedSkills || []).forEach(skill => {
        const name = skill.name || skill.id;
        if (picked.has(name) && !seen.has(name)) { seen.add(name); names.push(name); }
    });
    picked.forEach(name => { if (!seen.has(name)) { seen.add(name); names.push(name); } });
    return names;
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

function renderCreateCodingFields() {
    const coding = _createAgentType === 'coding';
    document.getElementById('agent-create-project-field')?.classList.toggle('hidden', !coding);
    document.getElementById('agent-create-service-field')?.classList.toggle('hidden', !coding);
    if (!coding) return;
    const seam = (typeof codingSeam === 'function') ? codingSeam() : null;
    if (seam) seam.paintService('agent-create-service');
}

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

let _agentStartInFlight = null;

let _agentStartAgentId = null;

function cancelAgentStart() {
    _agentStartInFlight = null;
    _agentStartAgentId = null;
}

function focusChatComposer() {
    const input = document.getElementById('chat-input');
    if (input) {
        requestAnimationFrame(() => { input.focus(); });
    }
}

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

function teamRosterRows() {
    return ((_sessCfg && _sessCfg.team && _sessCfg.team.members) || []);
}

function validTeamMemberRows() {
    const candidates = teamCandidateAgents();
    return teamRosterRows().filter(m => candidates.some(a => a.id === m.id));
}

function invalidTeamMembers() {
    const valid = new Set(validTeamMemberRows().map(m => m.id));
    return teamRosterRows().filter(m => !valid.has(m.id));
}

function validTeamMemberIds() {
    return validTeamMemberRows().map(m => m.id);
}

const _TENANT_TRANSPORT_PATHS = ['/message', '/stream', '/poll', '/cancel', '/upload'];

const _TENANT_TRANSPORT_RE = new RegExp(
    `^/(?:${_TENANT_TRANSPORT_PATHS.map(p => p.slice(1)).join('|')})\\b`);

function tenantSelectionHeader(url) {
    let tenantId = '';
    try {
        tenantId = sessionStorage.getItem('cow_tenant_id') || '';
    } catch (exc) {
        // A page without sessionStorage (sandboxed frame) has no selection.
        return '';
    }
    if (!tenantId || typeof url !== 'string' || !url.startsWith('/')) return '';
    if (/^\/api\/auth\//.test(url)) return '';
    return (/^\/api\//.test(url) || _TENANT_TRANSPORT_RE.test(url)) ? tenantId : '';
}

let _historyLoadSeq = 0;

let _historyLoadContext = '';

function restoreChatState() {
    // Branding is public and session settings have their own scoped endpoint.
    // Platform configuration belongs to enterConfigView(), not chat startup.
    return loadHistory(1);
}

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

const welcomeTemplate = document.getElementById('welcome-screen').cloneNode(true);

function syncChatHomeLayout() {
    const main = document.getElementById('chat-main');
    if (!main) return;
    // ``chat-home`` means "this is an empty chat", and a mounted coding pane is
    // the opposite of that. Its arrival alone does not remove the welcome markup
    // -- only an ordinary message does, and a coding conversation renders none --
    // so presence of the markup is not enough to answer the question. This also
    // keeps a later re-render (the observer below) from putting the home layout
    // back on top of the frame the module just mounted.
    // The legacy hero rules require its own wrapper. Master's welcome keeps
    // its flex container and the composer at the bottom of the chat pane.
    const home = !!document.querySelector('#welcome-screen .home-intro') && !codingPaneMounted();
    const changed = home !== main.classList.contains('chat-home');
    main.classList.toggle('chat-home', home);
    if (changed) main.scrollTop = 0;
    syncHomeHeroOffset();
    watchHomeHero(main);
}

function codingPaneMounted() {
    const module = codingChatModule();
    try {
        return !!(module && typeof module.isActive === 'function' && module.isActive());
    } catch (err) {
        return false;
    }
}

function syncHomeHeroOffset() {
    const main = document.getElementById('chat-main');
    if (!main || !main.classList.contains('chat-home')) return;
    const intro = main.querySelector('.home-intro');
    const composer = main.querySelector('#chat-composer');
    if (!intro || !composer) return;
    const span = Math.round(composer.getBoundingClientRect().bottom
        - intro.getBoundingClientRect().top);
    if (span > 0) main.style.setProperty('--home-hero-h', `${span}px`);
}

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
            const text = card.dataset.send || (card.dataset.promptKey
                ? t(card.dataset.promptKey) : card.querySelector('[data-i18n*="text"]')?.textContent);
            if (!text) return;
            chatInput.value = text;
            chatInput.dispatchEvent(new Event('input'));
            chatInput.focus();
        });
    });
}

const AGENT_QUESTION_COUNT = 4;

function agentOnboardingAgent() {
    return (chatAgentCatalog || []).find(a => a.id === activeAgentId) || findAgent(activeAgentId);
}

function agentOnboardingActive(agent) {
    if (!agent || agent.agent_type === 'coding') return false;
    // A conversation that has invited others is a group chat, not the ordinary
    // single-Agent home the onboarding copy belongs to.
    if (typeof sharedConversation === 'function' && sharedConversation()) return false;
    return true;
}

function agentQuestionTexts(agent) {
    const configured = (Array.isArray(agent.suggested_questions) ? agent.suggested_questions : [])
        .map(q => (typeof q === 'string' ? q.trim() : ''))
        .filter(Boolean)
        .slice(0, AGENT_QUESTION_COUNT);
    if (configured.length) return configured;
    const defaults = [];
    for (let i = 1; i <= AGENT_QUESTION_COUNT; i += 1) {
        const key = `home_agent_questions_default_${i}`;
        const copy = t(key);
        // An unresolved key comes back as itself; never render it on the page.
        if (copy && copy !== key) defaults.push(copy);
    }
    return defaults;
}

let _agentQuestionBusy = false;

function homeAgentQuestionBlocks() {
    return [...document.querySelectorAll('#home-agent-questions .home-agent-question')];
}

function homeAgentQuestionAvailability() {
    if (!document.getElementById('welcome-screen')) return { available: false, reason: '' };
    if (_agentQuestionBusy) return { available: false, reason: '' };
    if (chatInput.value.trim()) return { available: false, reason: t('home_agent_questions_draft') };
    if (pendingAttachments.length > 0 || uploadingCount > 0) {
        return { available: false, reason: t('home_agent_questions_draft') };
    }
    // A live turn in this conversation (Cancel showing) must finish first.
    if (sendBtnMode === 'cancel') return { available: false, reason: '' };
    return { available: true, reason: '' };
}

function syncAgentQuestionState() {
    const host = document.getElementById('home-agent-questions');
    if (!host) return;
    const { available, reason } = homeAgentQuestionAvailability();
    host.classList.toggle('is-disabled', !available);
    homeAgentQuestionBlocks().forEach(btn => { btn.disabled = !available; });
    const state = document.getElementById('home-agent-questions-state');
    if (state) {
        const show = !available && !!reason;
        state.textContent = show ? reason : '';
        state.classList.toggle('hidden', !show);
    }
}

function homeAgentQuestionClick(button) {
    if (!button || button.disabled || _agentQuestionBusy) return;
    const text = (button.dataset.question || '').trim();
    if (!text) return;
    if (!agentOnboardingActive(agentOnboardingAgent())) return;
    if (!homeAgentQuestionAvailability().available) { syncAgentQuestionState(); return; }
    _agentQuestionBusy = true;
    syncAgentQuestionState();
    try {
        chatInput.value = text;
        chatInput.dispatchEvent(new Event('input'));
        sendMessage();
    } finally {
        _agentQuestionBusy = false;
    }
    // sendMessage() removes the welcome synchronously when it accepts the turn.
    // If it refused, restore the entry and leave the question in the composer.
    if (document.getElementById('welcome-screen')) syncAgentQuestionState();
}

function renderAgentQuestions(agent) {
    const host = document.getElementById('home-agent-questions');
    if (!host) return;
    host.textContent = '';
    agentQuestionTexts(agent).forEach(question => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'home-agent-question';
        btn.dataset.question = question;
        btn.textContent = question;
        btn.addEventListener('click', () => homeAgentQuestionClick(btn));
        host.appendChild(btn);
    });
}

function paintAgentOnboarding(welcome, agent) {
    const avatar = welcome.querySelector('#home-agent-avatar');
    const usage = welcome.querySelector('#home-agent-usage');
    const onboarding = welcome.querySelector('#home-agent-onboarding');
    const label = welcome.querySelector('#home-agent-questions-label');
    const suggestions = welcome.querySelector('.home-suggestions');
    const active = agentOnboardingActive(agent);
    const main = document.getElementById('chat-main');
    if (main && main.classList) main.classList.toggle('agent-onboarding', active);
    if (suggestions) suggestions.classList.toggle('hidden', active);
    if (onboarding) onboarding.classList.toggle('hidden', !active);
    if (avatar) {
        avatar.classList.toggle('hidden', !active);
        avatar.textContent = '';
        if (active && typeof agentAvatarHTML === 'function') {
            avatar.innerHTML = agentAvatarHTML(agent, 56);
        }
    }
    if (usage) {
        usage.classList.toggle('hidden', !active);
        usage.textContent = active
            ? ((typeof agent.usage_hint === 'string' && agent.usage_hint.trim())
                || t('home_agent_usage_default'))
            : '';
    }
    if (label) label.textContent = active ? t('home_agent_questions_label') : '';
    const host = welcome.querySelector('#home-agent-questions');
    if (active) {
        renderAgentQuestions(agent);
        syncAgentQuestionState();
    } else {
        if (host) host.textContent = '';
        const state = welcome.querySelector('#home-agent-questions-state');
        if (state) { state.textContent = ''; state.classList.add('hidden'); }
    }
}

function paintWelcomeAgentIntro() {
    const welcome = document.getElementById('welcome-screen');
    if (!welcome || codingPaneMounted()) return;
    const heading = welcome.querySelector('.home-greeting');
    const description = welcome.querySelector('#welcome-subtitle');
    if (!heading || !description) return;
    const agent = agentOnboardingAgent();
    const ordinary = agent && agent.agent_type !== 'coding';
    const text = value => typeof value === 'string' ? value.trim() : '';
    heading.textContent = ordinary
        ? t('home_agent_greeting').replace('{name}', () => agent.name || agent.id)
        : t('home_greeting');
    description.textContent = ordinary
        ? text(agent.greeting) || text(agent.description) || t('home_agent_description')
        : t('home_description');
    paintAgentOnboarding(welcome, ordinary ? agent : null);
}

function renderWelcomeScreen() {
    const welcome = welcomeTemplate.cloneNode(true);
    welcome.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
    welcome.querySelectorAll('[data-i18n-html]').forEach(el => { el.innerHTML = t(el.dataset.i18nHtml); });
    messagesDiv.appendChild(welcome);
    bindWelcomeSuggestions(welcome);
    applyBrandToDocument();
    paintWelcomeAgentIntro();
    syncChatHomeLayout();
    document.getElementById('chat-main').scrollTop = 0;
}

const homeHeroGateEl = document.getElementById('app');

function _makeAttachmentId() {
    return `att_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}

async function _sendSingleAttachment(placeholder) {
    const formData = new FormData();
    formData.append('file', placeholder._file);
    formData.append('session_id', sessionId);
    formData.append('attachment_id', placeholder._attachment_id);
    const resp = await fetch('/upload', { method: 'POST', body: formData });
    const data = await resp.json();
    if (!data || data.status !== 'success') {
        throw new Error((data && data.message) || 'Upload failed');
    }
    placeholder.file_path = data.file_path;
    if (data.file_name) placeholder.file_name = data.file_name;
    if (data.file_type) placeholder.file_type = data.file_type;
    if (data.preview_url) placeholder.preview_url = data.preview_url;
    if (data.remote) placeholder.remote = true;
}

async function _sendDirectoryAttachment(placeholder) {
    const formData = new FormData();
    formData.append('session_id', sessionId);
    formData.append('upload_id', placeholder._attachment_id);
    formData.append('attachment_id', placeholder._attachment_id);
    for (const { file, relPath } of placeholder._files) {
        formData.append('files', file);
        formData.append('relative_paths', relPath);
    }
    const resp = await fetch('/upload', { method: 'POST', body: formData });
    const data = await resp.json();
    if (!data || data.status !== 'success') {
        throw new Error((data && data.message) || 'Upload failed');
    }
    if (!data.root_path) throw new Error('Directory root path missing');
    placeholder.file_path = data.root_path;
    placeholder.file_name = data.root_name || placeholder.file_name;
    if (data.remote) placeholder.remote = true;
}

async function _runAttachmentUpload(placeholder) {
    uploadingCount++;
    renderAttachmentPreview();
    try {
        if (placeholder._files) await _sendDirectoryAttachment(placeholder);
        else await _sendSingleAttachment(placeholder);
        delete placeholder._failed;
        delete placeholder._error;
    } catch (e) {
        // The draft is kept: the chip stays with a retry affordance and
        // sendMessage refuses to submit while it is unresolved.
        console.error('Upload failed:', e);
        placeholder._failed = true;
        placeholder._error = (e && e.message) || 'Upload failed';
    } finally {
        delete placeholder._uploading;
        uploadingCount--;
        renderAttachmentPreview();
    }
}

function retryAttachment(idx) {
    const att = pendingAttachments[idx];
    if (!att || !att._failed || att._uploading) return;
    delete att._failed;
    delete att._error;
    att._uploading = true;
    _runAttachmentUpload(att);
}

let _desktopContext = null;

let _desktopContextKey = '';

function _desktopSelectionKey() {
    return `${activeAgentId || ''}\u0000${sessionId || ''}`;
}

function _desktopContextClear() {
    const had = !!_desktopContext;
    _desktopContext = null;
    _desktopContextKey = '';
    // The panel follows the source: once no local project is in effect here, its
    // landing is the Agent's own folder again, and keeping the local listing on
    // screen would show files the session no longer reads.
    if (had && typeof wsSourceChanged === 'function') wsSourceChanged();
}

function _desktopContextForRequest() {
    if (!_desktopContext) return null;
    if (_desktopContextKey !== _desktopSelectionKey()) return null;
    if (!_wsSelState.current
            || String(_wsSelState.current.path || '').indexOf('desktop:') !== 0) return null;
    return _desktopContext;
}

async function _desktopRestoreContext() {
    if (typeof CowDesktopHost === 'undefined'
            || typeof CowDesktopHost.localContext !== 'function') return;
    const requestKey = _desktopSelectionKey();
    let reply = null;
    try {
        reply = await CowDesktopHost.localContext({
            agent_id: activeAgentId || '',
            business_session_id: sessionId || '',
        });
    } catch (_) {
        return;  // no host, or the host refused: the page simply has no local project
    }
    if (requestKey !== _desktopSelectionKey()) return;
    if (!reply || reply.state !== 'live') {
        // `stale` is a real answer: this chat had a local project and its
        // authorization no longer holds. Saying so is the point -- the user must
        // not be left thinking their files are on this machine when they are not.
        if (reply && reply.state === 'stale') _wsToast(t('ws_sel_local_lost'));
        return;
    }
    _desktopContext = {
        binding_id: String(reply.binding_id || ''),
        workspace_id: String(reply.workspace_id || ''),
        grant_version: parseInt(reply.grant_version, 10) || 0,
    };
    _desktopContextKey = requestKey;
    _wsSelState.current = {
        path: 'desktop:' + String(reply.grant_id || ''),
        name: String(reply.label || '') || t('ws_sel_local_dir'),
    };
    _wsSelUpdateLabel();
    if (typeof wsSourceChanged === 'function') wsSourceChanged();
}

function _desktopLocalLost(reason) {
    const had = !!_desktopContext;
    _desktopContextClear();
    if (!had) return;
    try { refreshWorkspaceSelector(); } catch (_) { /* the selector is not on this page */ }
    void reason;
    _wsToast(t('ws_sel_local_lost'));
}

function _wsSelBuildMenuParts(includeLocalDir) {
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
    // Desktop container: native 「选择本机目录」 when local-files is open. The
    // server-disk folder picker stays closed in database mode.
    if (includeLocalDir) {
        parts.push(`
            <button class="ws-sel-item" onclick="wsSelChooseLocalDir()" data-ws-sel-local-dir>
                <i class="fas fa-laptop-file"></i>
                <span class="ws-sel-name">${escapeHtml(t('ws_sel_local_dir'))}</span>
            </button>`);
    }
    parts.push(`
        <button class="ws-sel-item" onclick="wsSelNewProjectDialog()">
            <i class="fas fa-folder-plus"></i>
            <span class="ws-sel-name">${escapeHtml(t('ws_sel_new'))}</span>
        </button>`);
    return parts;
}

function _desktopRandomBytes(len) {
    const c = (typeof crypto !== 'undefined' && crypto) ? crypto : null;
    if (!c || typeof c.getRandomValues !== 'function') {
        throw new Error('crypto unavailable');
    }
    const bytes = new Uint8Array(len);
    c.getRandomValues(bytes);   // keep the receiver: crypto.getRandomValues(bytes)
    return bytes;
}

function _desktopRandomToken(len) {
    return btoa(String.fromCharCode.apply(null, _desktopRandomBytes(len)))
        .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function _desktopInstallationId() {
    const key = 'cow_desktop_installation_id';
    let id = null;
    try { id = localStorage.getItem(key); } catch (_) { id = null; }
    if (id && /^[A-Za-z0-9_-]{22,128}$/.test(id)) return id;
    const fresh = _desktopRandomToken(24);
    // Throws when it cannot persist (private frame, quota): report unavailable
    // rather than hand out an id the next page load will not recognise.
    localStorage.setItem(key, fresh);
    return fresh;
}

async function _desktopLocalGrantScope() {
    const tenantId = sessionStorage.getItem('cow_tenant_id') || '';
    if (!tenantId) return null;
    let self = _baseAccountSelf && _baseAccountSelf();
    if (!self || !self.user || !self.user.id) {
        try { await fetchAccountSelf(); } catch (_) { /* fall through */ }
        self = _baseAccountSelf && _baseAccountSelf();
    }
    const userId = self && self.user && self.user.id ? String(self.user.id) : '';
    if (!userId) return null;
    let serverId = '';
    try {
        serverId = 'srv_' + btoa(String(location.origin || 'local'))
            .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '').slice(0, 48);
    } catch (_) {
        serverId = 'srv_local';
    }
    return {
        serverId,
        userId,
        tenantId,
        deviceId: 'dev_' + _desktopInstallationId().slice(0, 40),
    };
}

async function wsSelChooseLocalDir() {
    _wsSelHide();
    if (typeof CowDesktopHost === 'undefined'
            || typeof CowDesktopHost.chooseWorkspace !== 'function') return;
    // The Agent + session this pick belongs to. Anything that lands after the
    // user moved on is dropped rather than adopted (task 2.6).
    const requestKey = _desktopSelectionKey();
    try {
        const scope = await _desktopLocalGrantScope();
        if (!scope) {
            _wsToast(t('ws_sel_local_dir_unavailable') || t('ws_sel_select_failed'));
            return;
        }
        const result = await CowDesktopHost.chooseWorkspace(scope);
        if (!result || !result.activated || !result.grant) return;  // cancelled: leave the old selection
        const grant = result.grant;
        if (requestKey !== _desktopSelectionKey()) return;
        // A picked directory is not usable until the server confirmed it: the
        // binding, the workspace and the grant version all have to exist, or a
        // turn would fall back to the server workspace while the chip claims a
        // local one (task 2.2).
        if (typeof CowDesktopHost.bindContext !== 'function') {
            throw new Error(t('ws_sel_select_failed'));
        }
        const confirmed = await CowDesktopHost.bindContext({
            scope,
            // Legacy migration hint only (task 2.2): the main process owns the
            // installation id now and adopts this value at most once, on the
            // first run after the upgrade, so the device registered from it is
            // not orphaned. It is not an authorization either way.
            installationId: _desktopInstallationId(),
            label: String(grant.label || ''),
            agentId: activeAgentId || '',
            businessSessionId: sessionId || '',
            contextNonce: _desktopRandomToken(24),
        });
        if (!confirmed || confirmed.ok === false || !confirmed.bindingId || !confirmed.workspaceId) {
            // A binding the server refused *because the session is gone* is the
            // re-login entry, not a toast: the previous selection is dropped so
            // the directory cannot be shown as if it were bound (task 2.4).
            if (_desktopSessionInvalid(confirmed && confirmed.code)) {
                _desktopRequireSignIn();
                return;
            }
            throw new Error((confirmed && confirmed.message) || t('ws_sel_select_failed'));
        }
        if (requestKey !== _desktopSelectionKey()) return;
        _desktopContext = {
            binding_id: String(confirmed.bindingId),
            workspace_id: String(confirmed.workspaceId),
            grant_version: parseInt(confirmed.grantVersion, 10) || 0,
        };
        _desktopContextKey = requestKey;
        _wsSelState.current = {
            path: 'desktop:' + String(grant.id || ''),
            name: String(grant.label || t('ws_sel_local_dir')),
        };
        _wsSelUpdateLabel();
        // The panel must follow the source it just committed to (task 9.2): the
        // files it shows from here are the ones in the directory the user picked,
        // read locally, not the server's copy of a folder with the same name.
        if (typeof wsSourceChanged === 'function') wsSourceChanged({ reveal: true });
    } catch (err) {
        // A pick that could not be confirmed must not be published: keep the
        // previous selection and say why, rather than showing a directory the
        // tools cannot read.
        _desktopContextClear();
        // The one refusal that is not a dead end (task 2.4): the native login is
        // gone, so the page offers the desktop re-login instead of a toast that
        // invites the user to repeat an action that cannot succeed. Everything
        // else -- a tool permission, a tenant rule, a device or network problem --
        // keeps its own message and is never misreported as a missing login.
        if (_desktopSessionInvalid(err && err.code)) {
            _desktopRequireSignIn();
            return;
        }
        _wsToast((err && err.message) || t('ws_sel_select_failed'));
    }
}

const _DESKTOP_SESSION_INVALID_CODES = [
    'auth_required', 'session_revoked', 'unauthorized', 'invalid_session',
];

function _desktopSessionInvalid(code) {
    return _DESKTOP_SESSION_INVALID_CODES.indexOf(String(code || '')) >= 0;
}

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
    if (code === 'attachment_upload_failed' || code === 'attachment_missing'
            || code === 'invalid_reference') {
        return t('attachment_upload_failed');
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

const PLATFORM_NOTE_LABELS = new Set([
    '个人目录输出位置', '個人目錄輸出位置', 'Personal directory output location',
]);

let NEW_CHAT_SURFACES;

function _newChatSurface(name) {
    return NEW_CHAT_SURFACES[name] || NEW_CHAT_SURFACES.panel;
}

function _newChatSurfaceOfMenu(node) {
    if (!node || typeof node.closest !== 'function') return '';
    return Object.keys(NEW_CHAT_SURFACES).find(name =>
        node.closest('#' + NEW_CHAT_SURFACES[name].menu)) || '';
}

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

function closeNewChatMenus(keep = '') {
    Object.keys(NEW_CHAT_SURFACES).forEach(name => {
        const surface = NEW_CHAT_SURFACES[name];
        if (surface.menu !== keep) document.getElementById(surface.menu)?.classList.add('hidden');
    });
}

function syncNewChatControls() {
    const choosable = multiAgentMode();
    Object.keys(NEW_CHAT_SURFACES).forEach(name => {
        const surface = NEW_CHAT_SURFACES[name];
        document.getElementById(surface.caret)?.classList.toggle('hidden', !(choosable && surface.available()));
        const menu = document.getElementById(surface.menu);
        if (menu && !menu.classList.contains('hidden')) paintNewChatMenu(menu);
    });
}

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
        <button type="button" class="new-chat-item new-chat-team"
                onclick="openTeamChatModal()">
            <span class="new-chat-team-ico"><i class="fas fa-user-group"></i></span>
            <span>${escapeHtml(t('new_team_chat'))}</span>
        </button>
        <div class="new-chat-sep"></div>`;
    // The team entry is the menu's first item (change
    // promote-team-chat-launch-option): it is the one cross-Agent action here,
    // and burying it under the roster made it look absent. The separator now
    // sits *below* it, still dividing it from the solo list. Both entry points
    // read this one painter, so the order cannot drift between them.
    menu.innerHTML = `
        ${teamRow}<div class="new-chat-section">${rows}</div>`;
}

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

let _teamChatDraft = null;

let _teamChatTrigger = null;

function _elementIsFocusable(el) {
    return !!el && typeof el.focus === 'function'
        && (!el.getClientRects || el.getClientRects().length > 0);
}

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

function teamChatDraft() {
    return _teamChatDraft;
}

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

function teamChatRosterProblem(draft) {
    const eligible = new Set(teamCandidateAgents().map(a => a.id));
    if (draft.selected.some(id => !eligible.has(id))) return 'team_chat_stale';
    if (draft.selected.length < 2) return 'new_team_chat_min';
    if (!draft.owner || !draft.selected.includes(draft.owner)) return 'new_team_chat_min';
    return '';
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

function commitTeamChatSession(draft, prepared, data) {
    const commit = () => {
        if (_teamChatDraft !== draft) return;
        closeTeamChatModal();
        activeAgentId = prepared.agentId;
        writeScopedPreference('cow_active_agent', activeAgentId);
        // Moving onto a prepared session is the same render path an ordinary new
        // chat takes; only the id is decided elsewhere.
        if (!commitPreparedSession(prepared.sessionId, { optimistic: true, inherit: true })) return;
        if (data) {
            _sessCfg = { model: data.model, team: data.team };
            setSessionParticipants(prepared.sessionId, data.team);
        }
        renderComposerIdentity();
        if (typeof _renderModelChip === 'function') _renderModelChip();
        if (typeof resetWorkspaceToAgentRoot === 'function') resetWorkspaceToAgentRoot();
        const input = document.getElementById('chat-input');
        if (input) input.focus();
    };
    if (typeof wsGuardUnsaved === 'function' && !wsGuardUnsaved(commit)) return;
    commit();
}

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
    _autoScrollEnabled = true;
    _lastScrollTop = 0;
    _programmaticScrollTop = null;
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
    _historyDirty = true;
    if (optimistic && typeof openSessionPanel === 'function') openSessionPanel();
    if (_historyVisible && optimistic) _addOptimisticSessionItem(sessionId);
    if (typeof finishSessionPanelSelection === 'function') finishSessionPanelSelection();
    // A fresh session has no server-side context row yet: keep the usage entry
    // quiet until the first turn persists it.
    if (typeof _contextAfterSessionChange === 'function') _contextAfterSessionChange(false);
    if (window.innerWidth < 1024) closeSidebar();
    focusChatComposer();
    return true;
}

let _historyVisible = false;

let _historyDirty = false;

let _historyRefreshQueued = false;

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

let _historyRequestPromise = null;

let _historyRequestTimer = null;

let _historyLastLoadedAt = 0;

let _historyLastLoadedContext = '';

const _HISTORY_CACHE_MAX_AGE = 24 * 60 * 60 * 1000;

const _HISTORY_REQUEST_TIMEOUT = 15000;

function _historyCacheKey() {
    // Never restore business data before the account and tenant are confirmed.
    // Origin also separates servers when embedded in a desktop browser session.
    try {
        const tenant = sessionStorage.getItem('cow_tenant_id');
        if (typeof _accountState === 'undefined' || !_accountState.authenticated
                || typeof _accountAppVisible === 'undefined' || !_accountAppVisible
                || !_accountState.username || !tenant || _sidebarRecentDenied()) return null;
        return 'cow_history_v1:' + JSON.stringify([
            window.location.origin, _accountState.username, tenant,
        ]);
    } catch (_) { return null; }
}

function _readHistoryCache() {
    const key = _historyCacheKey();
    if (!key) return null;
    try {
        const raw = localStorage.getItem(key);
        if (!raw || raw.length > 256000) return null;
        const cached = JSON.parse(raw);
        if (!cached || !Number.isFinite(cached.savedAt) || cached.savedAt > Date.now()
                || Date.now() - cached.savedAt > _HISTORY_CACHE_MAX_AGE
                || !Array.isArray(cached.sessions) || cached.sessions.length > _SESSION_PAGE_SIZE
                || !cached.sessions.every(s => s && typeof s.session_id === 'string'
                    && typeof s.title === 'string' && s.agent && typeof s.agent.id === 'string')
                || !Number.isFinite(cached.total) || !Array.isArray(cached.project_order)) return null;
        return cached;
    } catch (_) { return null; }
}

function _writeHistoryCache(data) {
    const key = _historyCacheKey();
    if (!key || _historyQuery) return;
    // Only list summaries belong here, never transcripts, tokens or settings.
    const badge = a => ({ id: a.id, name: a.name, avatar: a.avatar, agent_type: a.agent_type });
    try {
        const cached = {
            savedAt: Date.now(), total: data.total, has_more: !!data.has_more,
            group_mode: data.group_mode, project_order: data.project_order || [],
            sessions: (data.sessions || []).slice(0, _SESSION_PAGE_SIZE).map(s => ({
                session_id: s.session_id, title: s.title, created_at: s.created_at,
                last_active: s.last_active, msg_count: s.msg_count, pinned: s.pinned,
                agent: badge(s.agent), participants: (s.participants || []).map(badge),
                project: s.project ? { path: s.project.path, name: s.project.name } : null,
                sync_state: s.sync_state,
            })),
        };
        const raw = JSON.stringify(cached);
        if (raw.length <= 256000) localStorage.setItem(key, raw);
    } catch (_) { /* Storage may be disabled or full; the live list still works. */ }
}

function _invalidateHistoryCache() {
    try {
        const key = _historyCacheKey();
        if (key) localStorage.removeItem(key);
    } catch (_) {}
    _historyLastLoadedAt = 0;
}

function _patchHistoryCache(sid, agentId, patch) {
    const cached = _readHistoryCache();
    if (!cached) return;
    const row = cached.sessions.find(s => s.session_id === sid && s.agent.id === agentId);
    if (!row) return;
    ['title', 'pinned', 'last_active', 'msg_count'].forEach(key => {
        if (patch[key] !== undefined) row[key] = patch[key];
    });
    cached.sessions.sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned)
        || (b.last_active || 0) - (a.last_active || 0));
    _writeHistoryCache(cached);
}

function _restoreHistoryCache() {
    if (_historyQuery || _sessionItems.length) return;
    const cached = _readHistoryCache();
    if (!cached) return;
    _sessionItems = cached.sessions;
    _sessionPage = 1;
    _sessionHasMore = cached.has_more;
    _historyTotal = cached.total;
    _sessionGroupMode = cached.group_mode === 'project' ? 'project' : 'time';
    _projectOrder = cached.project_order;
    _setHistoryState(_sessionItems.length ? '' : 'session_history_empty');
    _renderSessionList();
}

function _sessionListContext() {
    return JSON.stringify([_historyAuthGeneration, sessionStorage.getItem('cow_tenant_id') || '', activeAgentId, _historyQuery, _historyCacheKey()]);
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
    clearTimeout(_historyRequestTimer);
    _historyRequestTimer = null;
    _historyRequestPromise = null;
    _sessionReqSeq++;
    _sessionLoading = false;
    _historyRefreshQueued = false;
}

function _historyLoadSettled() {
    _sessionLoading = false;
    _updateHistorySearchControls();
    if (!_historyRefreshQueued) return;
    _historyRefreshQueued = false;
    _refreshHistoryList();
}

function _resetHistorySearch() {
    _cancelHistoryRequest();
    _historyAuthGeneration++;
    _historyLastLoadedAt = 0;
    _historyLastLoadedContext = '';
    if (typeof resetSessionPanelIdentity === 'function') resetSessionPanelIdentity();
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
    if (typeof sessionHistorySurface === 'function' && sessionHistorySurface() === 'panel') return '';
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

function _findSessionItem(sid, agentId) {
    if (typeof _sessionItems === 'undefined' || !Array.isArray(_sessionItems)) return null;
    return _sessionItems.find(s => s.session_id === sid
        && (!agentId || (s.agent && s.agent.id) === agentId)) || null;
}

function _touchHistorySession(sid, agentId, patch) {
    if (!sid) return;
    if (typeof _sessionItems === 'undefined' || !Array.isArray(_sessionItems)) {
        _historyDirty = true;
        return;
    }
    const owner = agentId || (typeof activeAgentId !== 'undefined' ? activeAgentId : '');
    const entry = _findSessionItem(sid, owner) || _findSessionItem(sid);
    if (!entry) {
        if (_historyQuery) return;
        if (typeof _historyVisible !== 'undefined' && _historyVisible) _refreshHistoryList();
        else _historyDirty = true;
        return;
    }
    patch = patch || {};
    if (Object.prototype.hasOwnProperty.call(patch, 'optimistic') && !patch.optimistic) {
        delete entry.optimistic;
    }
    Object.keys(patch).forEach(key => {
        if (key === 'optimistic' || patch[key] === undefined) return;
        entry[key] = patch[key];
    });
    _patchHistoryCache(sid, owner, patch);
    if (patch.last_active != null || patch.pinned != null) {
        if (typeof _sortSessionItems === 'function') _sortSessionItems();
    }
    if (typeof _historyVisible !== 'undefined' && _historyVisible
            && typeof _renderSessionList === 'function') _renderSessionList();
}

function _refreshHistoryList() {
    _invalidateHistoryCache();
    if (typeof _historyVisible === 'undefined' || !_historyVisible) {
        _historyDirty = true;
        return;
    }
    if (typeof _sessionLoading !== 'undefined' && _sessionLoading) {
        _historyRefreshQueued = true;
        return;
    }
    loadSessionList(undefined, { silent: true, fill: false });
}

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

function _sidebarRecentDenied() {
    return typeof _viewNavDenied === 'function' && !!_viewNavDenied('history');
}

let _sessionActionMenu = null;

let _sessionMenuCleanup = null;

const _sessionActionsInFlight = new Set();

function _sessionActionKey(action, sid, agentId) {
    return `${action}:${agentId || ''}:${sid}`;
}

function _sessionMarkBusy(sid, agentId, busy) {
    document.querySelectorAll('.session-item[data-session-id], .archived-session-row[data-session-id]')
        .forEach(el => {
            if (el.dataset.sessionId === sid && (!agentId || el.dataset.agentId === agentId)) {
                el.classList.toggle('busy', busy);
            }
        });
}

function _isOptimisticSession(sid, owner) {
    return _sessionItems.some(s => s.optimistic && s.session_id === sid
        && (!owner || s.agent?.id === owner));
}

function archiveSidebarSession(sid, agentId) {
    if (!sid || _isOptimisticSession(sid, agentId)) return;
    const owner = agentId || activeAgentId || '';
    const key = _sessionActionKey('archive', sid, owner);
    // A second click while the first request is unanswered would archive an
    // already-archived session; the answer marks the row idle again.
    if (_sessionActionsInFlight.has(key)) return;
    _sessionActionsInFlight.add(key);
    const context = _sessionListContext();
    _sessionMarkBusy(sid, owner, true);
    return fetch(`/api/sessions/${encodeURIComponent(sid)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ archived: true, agent_id: owner }),
    }).then(r => r.json().catch(() => ({}))).then(data => {
        if (context !== _sessionListContext()) return;
        const ok = data && data.status === 'success';
        _wsToast(ok ? t('session_archived') : (data && data.message) || t('session_archive_failed'));
        if (ok) _refreshHistoryList();
    }).catch(() => {
        if (context === _sessionListContext()) _wsToast(t('session_archive_failed'));
    }).finally(() => {
        _sessionActionsInFlight.delete(key);
        _sessionMarkBusy(sid, owner, false);
    });
}

let _archivedSessionsSeq = 0;

let _archivedSessionsBody = null;

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
    const key = _sessionActionKey('restore', sessionId, owner);
    if (_sessionActionsInFlight.has(key)) return;
    _sessionActionsInFlight.add(key);
    // Remember which dialog asked, so a delayed answer does not repaint a
    // dialog that was dismissed and reopened on another set of records.
    const body = _archivedSessionsBody;
    const context = _sessionListContext();
    _sessionMarkBusy(sessionId, owner, true);
    return fetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ archived: false, agent_id: owner }),
    })
        .then(r => r.json().catch(() => ({})))
        .then(data => {
            if (!data || data.status !== 'success') {
                _wsToast((data && data.message) || t('session_restore_failed'));
                return;
            }
            _wsToast(t('session_restored'));
            _refreshHistoryList();
            if (context === _sessionListContext() && body && body === _archivedSessionsBody) {
                _loadArchivedSessions(body);
            }
        })
        .catch(() => _wsToast(t('session_restore_failed')))
        .finally(() => {
            _sessionActionsInFlight.delete(key);
            _sessionMarkBusy(sessionId, owner, false);
        });
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
    if (session.optimistic) return;
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
        ['archive_session', 'fa-box-archive', () => archiveSidebarSession(session.session_id, owner)],
        ['agents_delete', 'fa-trash-can', () => deleteSession(session.session_id, owner)],
    ];
    actions.forEach(([label, icon, action], index) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'session-action-menu-item' + (label === 'agents_delete' ? ' danger' : '');
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

function codingChatModule() {
    return (typeof window !== 'undefined' && window.CodingChat) || null;
}

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

function guardCodingMessage() {
    if (!isCodingAgent(activeAgentId)) return false;
    _wsToast(t('coding_send_in_pane'));
    return true;
}

async function isCodingAgentAsync(agentId) {
    return isCodingAgent(agentId);
}

function codingProjectDirOf(agentId) {
    const id = agentId || activeAgentId || '';
    const found = (typeof findAgent === 'function' ? findAgent(id) : null)
        || (typeof availableChatAgents === 'function'
            ? availableChatAgents().find(a => a && a.id === id) : null);
    return (found && found.coding_project_dir) || '';
}

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

function codingServiceLabel() {
    const settings = _codingSettings || {};
    if (!settings.service_id && !settings.web_url) return '';
    return [settings.service_id, settings.web_url].filter(Boolean).join(' · ');
}

function codingAvailability() {
    const module = codingChatModule();
    if (!module) return { available: false, reason: t('coding_disabled') };
    return { available: true, reason: '' };
}

function openCodingSession(agentId, requestedSessionId, options) {
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

    const mounted = requestedSessionId
        ? module.open(targetAgent, requestedSessionId)
        : module.launch(targetAgent, codingProjectDirOf(targetAgent));
    return Promise.resolve(mounted).then(described => !!(described && described.session_id));
}

function leaveCodingSession(options) {
    const module = codingChatModule();
    if (!module || !module.isActive()) return true;
    return module.leave(options);
}

function activeCodingSession() {
    const module = codingChatModule();
    return (module && module.current && module.current()) || null;
}

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
        onOpened: (described, options) => {
            const current = module.current();
            if (!described?.session_id || current?.agent_id !== activeAgentId) return;
            // This must update the page's sessionId, including when a failed
            // initial launch succeeds through the pane's own retry button.
            sessionId = described.session_id;
            writeScopedPreference(activeSessionStorageKey(), sessionId);
            _sessCfg = null;
            markActiveSessionRow();
            if (options?.isNew && typeof openSessionPanel === 'function') openSessionPanel();
            if (typeof finishSessionPanelSelection === 'function') finishSessionPanelSelection();
            if (typeof _refreshHistoryList === 'function') _refreshHistoryList();
        },
        redrawList: () => {
            _refreshHistoryList();
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

function markActiveSessionRow() {
    document.querySelectorAll('.session-item').forEach(el => {
        el.classList.toggle('active', el.dataset.sessionId === sessionId
            && (!el.dataset.agentId || el.dataset.agentId === activeAgentId));
    });
}

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

function submitSessionDelete(sid, owner) {
    const key = _sessionActionKey('delete', sid, owner);
    if (_sessionActionsInFlight.has(key)) return;
    _sessionActionsInFlight.add(key);
    const context = _sessionListContext();
    const deletingCurrent = sid === sessionId && (!owner || owner === activeAgentId);
    const next = deletingCurrent ? _findNextSession(sid, owner) : null;
    _sessionMarkBusy(sid, owner, true);
    return fetch(`/api/sessions/${encodeURIComponent(sid)}?agent_id=${encodeURIComponent(owner || '')}`, { method: 'DELETE' })
        .then(r => r.json())
        .then(data => {
            _invalidateHistoryCache();
            if (!data || data.status !== 'success') {
                _wsToast((data && data.message) || t('session_settings_failed'));
                return;
            }
            // The identity, the surface or the selection may have moved on
            // while the request was in flight. Then the list is refreshed and
            // nothing here navigates or closes what the user opened.
            if (context !== _sessionListContext() || sid !== sessionId) {
                _refreshHistoryList();
                return;
            }
            if (next) {
                // The unsaved guard already ran; do not ask again on the way out.
                if (typeof codingSeam === 'function' && codingSeam().isCodingAgent(activeAgentId)
                    && codingSeam().activeSession()) {
                    codingSeam().leave({ skipConfirm: true });
                }
                switchSession(next.sessionId, next.agentId);
                _refreshHistoryList();
            } else if (typeof codingSeam === 'function'
                && codingSeam().isCodingAgent(owner || activeAgentId)) {
                enterCodingEmptyState(owner || activeAgentId);
                _refreshHistoryList();
            } else {
                newChat(false);
            }
        })
        .catch(() => _wsToast(t('session_settings_failed')))
        .finally(() => {
            _sessionActionsInFlight.delete(key);
            _sessionMarkBusy(sid, owner, false);
        });
}

function enterCodingEmptyState(agentId) {
    const owner = agentId || activeAgentId || '';
    const seam = (typeof codingSeam === 'function') ? codingSeam() : null;
    // The leave confirmation already ran before the delete request.
    if (seam) seam.leave({ skipConfirm: true });
    if (owner) {
        activeAgentId = owner;
        writeScopedPreference('cow_active_agent', activeAgentId);
    }
    // No conversation: drop the selection and its remembered id, so a reload
    // does not try to reopen the session that was just removed.
    sessionId = '';
    if (typeof removeScopedPreference === 'function') removeScopedPreference(activeSessionStorageKey());
    _sessCfg = null;
    if (typeof resetTimeline === 'function') resetTimeline();
    messagesDiv.innerHTML = '';
    renderCodingEmptyState(owner);
    renderComposerIdentity();
    if (currentView !== 'chat') navigateTo('chat');
    const composerArea = document.getElementById('chat-composer');
    if (composerArea) composerArea.classList.add('hidden');
    if (seam) seam.markRows();
}

function renderCodingEmptyState(agentId) {
    const agent = (typeof findAgent === 'function' && findAgent(agentId)) || null;
    const wrap = document.createElement('div');
    wrap.id = 'coding-empty-state';
    wrap.className = 'coding-empty-state';
    const title = document.createElement('div');
    title.className = 'coding-empty-title';
    title.textContent = t('coding_empty_title');
    const hint = document.createElement('div');
    hint.className = 'coding-empty-hint';
    hint.textContent = t('coding_empty_hint');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'coding-empty-new';
    button.textContent = t('coding_empty_new');
    button.addEventListener('click', () => { newChat(true); });
    wrap.appendChild(title);
    wrap.appendChild(hint);
    wrap.appendChild(button);
    messagesDiv.appendChild(wrap);
    if (typeof syncChatHomeLayout === 'function') syncChatHomeLayout();
}

function _mountDropdownSearch(menuEl) {
    const wrap = document.createElement('div');
    wrap.className = 'cfg-dropdown-search';
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'cfg-dropdown-search-input';
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('data-i18n-placeholder', 'knowledge_agent_search_placeholder');
    input.setAttribute('aria-label', 'knowledge_agent_search_placeholder');
    input.placeholder = t('knowledge_agent_search_placeholder');
    wrap.appendChild(input);

    // Rendered only while a query is active and matched nothing: an unfiltered
    // menu with no options is the caller's empty state to explain, not ours.
    const emptyEl = document.createElement('div');
    emptyEl.className = 'cfg-dropdown-empty hidden';
    emptyEl.textContent = t('knowledge_agent_search_empty');

    const listEl = document.createElement('div');
    listEl.className = 'cfg-dropdown-list';

    menuEl.innerHTML = '';
    menuEl.classList.add('cfg-dropdown-searchable');
    menuEl.appendChild(wrap);
    menuEl.appendChild(emptyEl);
    menuEl.appendChild(listEl);

    return {
        input,
        listEl,
        showEmpty(visible) { emptyEl.classList.toggle('hidden', !visible); },
    };
}

function _dropdownOptionMatches(opt, needle) {
    const label = String(opt && opt.label != null ? opt.label : '').toLowerCase();
    const value = String(opt && opt.value != null ? opt.value : '').toLowerCase();
    return label.includes(needle) || value.includes(needle);
}

function _moveDropdownHighlight(listEl, step) {
    const rows = Array.from(listEl.querySelectorAll('.cfg-dropdown-item'));
    if (!rows.length) return;
    const current = rows.findIndex(row => row.classList.contains('cfg-dropdown-item-hl'));
    const next = current < 0
        ? (step > 0 ? 0 : rows.length - 1)
        : (current + step + rows.length) % rows.length;
    rows.forEach((row, index) => row.classList.toggle('cfg-dropdown-item-hl', index === next));
}

function _dropdownSearchKeydown(event, el, search, rerender) {
    const key = event.key;
    if (key === 'Escape') {
        // Clearing is the first intent: someone who typed a filter usually
        // wants their list back, not the menu dismissed.
        event.preventDefault();
        if (search.input.value) {
            search.input.value = '';
            el._ddQuery = '';
            rerender();
        } else {
            el.classList.remove('open');
        }
        return;
    }
    if (key === 'ArrowDown' || key === 'ArrowUp') {
        event.preventDefault();
        _moveDropdownHighlight(search.listEl, key === 'ArrowDown' ? 1 : -1);
        return;
    }
    if (key === 'Enter') {
        const row = search.listEl.querySelector('.cfg-dropdown-item-hl');
        if (row && typeof row.click === 'function') {
            event.preventDefault();
            row.click();
        }
    }
}

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

let brandingDraft = null;

let brandingBaseline = null;

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

let toolsState = { rows: [] };

let skillsState = { byName: {} };

let resourceDetailState = null;

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

let memberCatalogState = { items: [] };

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

const DEDICATED_SEARCH_CREDENTIALS = ['bocha', 'anysearch', 'serply', 'tavily', 'searxng', 'keenable'];

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

function openChannelsAddEntry() {
    if (channelScope() === 'tenant') return openTenantChannelForm();
    return openAddChannelPanel();
}

function channelsFailureKey(status, code) {
    if (String(code || '') === 'database_unavailable') return 'channels_not_open';
    if (Number(status) === 403) return 'channels_no_permission';
    if (Number(status) === 405 || Number(status) === 503) return 'channels_not_open';
    return 'channels_load_failed';
}

function scanFailureText(status, code, message) {
    const key = channelsFailureKey(status, code);
    if (key !== 'channels_load_failed') return `${t(key)}：${t(key + '_desc')}`;
    return message || t('feishu_scan_fail');
}

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

let tenantChannelTypes = [];

let tenantChannelInstances = [];

let tenantGuestRoles = [];

let tenantChannelSelfScope = false;

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

const TENANT_CHANNEL_SCAN_TYPES = {
    weixin: 'startTenantWeixinScan',
    feishu: 'startFeishuRegister',
    wecom_bot: 'startTenantWecomScan',
};

const TENANT_CHANNEL_SCAN_COPY = {
    weixin: {
        tab: 'wecom_mode_scan', manualTab: 'wecom_mode_manual',
        desc: 'weixin_scan_desc', btn: 'weixin_scan_title',
    },
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

function tenantChannelSupportsScan(channelType) {
    // Both tables must know the type: the start function to run and the copy to
    // show. The module owns the pairing rule; the tables stay here because they
    // name page-global start functions (startFeishuRegister, …).
    return window.ChannelWorkbench.scanReady(
        TENANT_CHANNEL_SCAN_TYPES, TENANT_CHANNEL_SCAN_COPY, channelType);
}

function tenantChannelScanCopy(channelType) {
    return window.ChannelWorkbench.scanCopy(TENANT_CHANNEL_SCAN_COPY, channelType);
}

function tenantChannelAppearance(channelType) {
    return window.ChannelWorkbench.appearance(tenantChannelType(channelType));
}

function tenantGuestSection(inst, editing) {
    if (!editing || !inst || inst.scope !== 'tenant') return '';
    const policy = inst.unbound_user_policy || 'require_binding';
    const roleId = inst.guest_role_id || '';
    const groups = (inst.guest_group_ids || []).join('\n');
    const roleOptions = tenantGuestRoles
        .filter(r => r && r.code !== 'tenant_admin')
        .map(r => `<option value="${escapeHtml(r.id)}"${r.id === roleId ? ' selected' : ''}>${escapeHtml(r.name)}</option>`)
        .join('');
    return `
        <div class="rounded-lg border border-slate-200 dark:border-white/10 p-3 space-y-3"
             data-tenant-channel-guest="${escapeHtml((inst && inst.id) || '')}">
            <div class="flex items-center gap-2">
                <div class="text-xs font-semibold text-slate-600 dark:text-slate-300">${t('tenant_channel_guest_title')}</div>
                <span id="tenant-channel-guest-state" class="text-[11px] px-1.5 py-0.5 rounded ${policy === 'auto_guest' ? 'bg-emerald-50 text-emerald-600 dark:bg-emerald-900/30 dark:text-emerald-400' : 'bg-slate-100 text-slate-500 dark:bg-white/5 dark:text-slate-400'}">${policy === 'auto_guest' ? t('tenant_channel_guest_state_on') : t('tenant_channel_guest_state_off')}</span>
            </div>
            <div class="text-[11px] text-slate-400">${t('tenant_channel_guest_hint')}</div>
            <div>
                <label class="block text-xs text-slate-500 dark:text-slate-400 mb-1">${t('tenant_channel_guest_policy')}</label>
                <select id="tenant-channel-guest-policy" onchange="syncTenantGuestState(this.value)"
                    class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-white/10 bg-white dark:bg-[#141414] text-sm">
                    <option value="require_binding"${policy === 'require_binding' ? ' selected' : ''}>${t('tenant_channel_guest_policy_bind')}</option>
                    <option value="auto_guest"${policy === 'auto_guest' ? ' selected' : ''}>${t('tenant_channel_guest_policy_auto')}</option>
                </select>
            </div>
            <div>
                <label class="block text-xs text-slate-500 dark:text-slate-400 mb-1">${t('tenant_channel_guest_role')}</label>
                <select id="tenant-channel-guest-role"
                    class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-white/10 bg-white dark:bg-[#141414] text-sm">
                    ${roleOptions}
                </select>
            </div>
            <div>
                <label class="block text-xs text-slate-500 dark:text-slate-400 mb-1">${t('tenant_channel_guest_groups')}</label>
                <textarea id="tenant-channel-guest-groups" rows="2"
                    class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-white/10 bg-white dark:bg-[#141414] text-sm font-mono text-xs"
                    placeholder="oc_xxxxxxxx">${escapeHtml(groups)}</textarea>
                <div class="text-[11px] text-slate-400 mt-1">${t('tenant_channel_guest_groups_hint')}</div>
            </div>
        </div>`;
}

function syncTenantGuestState(value) {
    const badge = document.getElementById('tenant-channel-guest-state');
    if (!badge) return;
    const on = value === 'auto_guest';
    badge.textContent = on ? t('tenant_channel_guest_state_on')
        : t('tenant_channel_guest_state_off');
    badge.className = 'text-[11px] px-1.5 py-0.5 rounded ' + (on
        ? 'bg-emerald-50 text-emerald-600 dark:bg-emerald-900/30 dark:text-emerald-400'
        : 'bg-slate-100 text-slate-500 dark:bg-white/5 dark:text-slate-400');
}

function buildTenantChannelForm(inst) {
    const draft = tenantChannelDraft || {};
    const channelType = (inst && inst.channel_type) || draft.channel_type || '';
    const spec = tenantChannelType(channelType);
    const fields = spec ? spec.credential_fields : [];
    const iid = (inst && inst.id) || 'new';
    const editing = !!(inst && inst.id);
    const supportsScan = tenantChannelSupportsScan(channelType) && !(editing && channelType === 'weixin');
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
            ${tenantGuestSection(inst, editing)}
            ${scanPane}
            ${manualPane}
            ${wb.webhookHint({ spec, iid: editing ? iid : '', escape: escapeHtml, t })}
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
                bodyHtml: editing ? buildTenantChannelForm(inst) : window.ChannelWorkbench.webhookHint({
                    spec: tenantChannelType(inst.channel_type), iid: inst.id, escape: escapeHtml, t,
                }),
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
    // The guest-role picker needs the tenant's roles. A failure only empties the
    // picker -- the server still validates the saved role -- so it must not turn
    // the whole channel list into an error page.
    const roles = fetch('/api/tenant/roles')
        .then(r => (r.ok ? r.json() : null))
        .then(d => { tenantGuestRoles = (d && d.items) || []; })
        .catch(() => {});
    return Promise.all([roster, roles]).then(() => fetch('/api/tenant/channels')
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

function openTenantChannelForm(instanceId) {
    stopTenantWeixinScan();
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
        // Read back by submitTenantChannel only when the guest section rendered;
        // otherwise they stay undefined and the server keeps the stored policy.
        unbound_user_policy: inst ? inst.unbound_user_policy : undefined,
        guest_role_id: inst ? inst.guest_role_id : undefined,
        guest_group_ids: inst ? inst.guest_group_ids : undefined,
    };
    renderTenantChannels();
}

function switchTenantChannelMode(iid, mode) {
    if (!tenantChannelDraft) return;
    if (mode !== 'scan') stopTenantWeixinScan();
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

function changeTenantChannelType(channelType) {
    stopTenantWeixinScan();
    if (!tenantChannelDraft) return;
    const display = document.getElementById('tenant-channel-display');
    if (display) tenantChannelDraft.display_name = display.value || '';
    tenantChannelDraft.channel_type = channelType || '';
    tenantChannelDraft.credentials = {};
    tenantChannelDraft.mode = tenantChannelSupportsScan(channelType) ? 'scan' : 'manual';
    renderTenantChannels();
}

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

function applyScanTicketToTenantForm(scanTicket) {
    const draft = tenantChannelDraft;
    if (!draft || !scanTicket) return false;
    draft.scan_ticket = scanTicket;
    return true;
}

function tenantChannelAutoName(channelType, credentials) {
    return window.ChannelWorkbench.autoName(
        tenantChannelType(channelType), channelType, credentials, currentLang);
}

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

let tenantWeixinScan = null;

function stopTenantWeixinScan() {
    const scan = tenantWeixinScan;
    tenantWeixinScan = null;
    if (!scan) return;
    clearTimeout(scan.timer);
    if (scan.handle) {
        fetch('/api/weixin/qrlogin', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'cancel', handle: scan.handle }),
        }).catch(() => {});
    }
}

function startTenantWeixinScan(statusId, iid) {
    stopTenantWeixinScan();
    const draft = tenantChannelDraft;
    if (!draft || draft.instance_id || draft.channel_type !== 'weixin') return Promise.resolve();
    const agent = (document.getElementById('tenant-channel-agent') || {}).value || '';
    const display = (document.getElementById('tenant-channel-display') || {}).value || '';
    const target = tenantChannelTargets.find(item => item.id === agent);
    const scope = tenantChannelSelfScope || (target && target.scope === 'user') ? 'user' : 'tenant';
    const scan = { draft, statusId, handle: '', timer: null, agent_id: agent, display_name: display };
    tenantWeixinScan = scan;
    const panel = document.getElementById(statusId);
    if (panel) panel.textContent = t('weixin_scan_loading');
    return fetch('/api/weixin/qrlogin', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'refresh', scope }),
    }).then(r => r.json()).then(data => {
        scan.handle = data.handle || '';
        if (tenantWeixinScan !== scan || tenantChannelDraft !== draft) {
            if (scan.handle) fetch('/api/weixin/qrlogin', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'cancel', handle: scan.handle }),
            }).catch(() => {});
            return;
        }
        if (data.status !== 'success' || !scan.handle) throw new Error(data.message || t('weixin_scan_fail'));
        renderTenantWeixinScan(scan, data);
        scan.timer = setTimeout(() => pollTenantWeixinScan(scan), 2000);
    }).catch(error => tenantWeixinScanFailed(scan, error));
}

function renderTenantWeixinScan(scan, data) {
    const panel = document.getElementById(scan.statusId);
    if (!panel) return;
    const src = data.qr_image || data.qrcode_url || '';
    panel.innerHTML = `<div class="flex flex-col items-center gap-3 py-3">
        ${src ? `<img src="${escapeHtml(src)}" alt="${escapeHtml(t('weixin_scan_title'))}" class="w-48 h-48 rounded-lg">` : ''}
        <p class="text-sm text-slate-500">${t(data.qr_status === 'scaned' ? 'weixin_scan_scanned' : 'weixin_scan_waiting')}</p>
        <p class="text-xs text-slate-400">${t('tenant_channel_scan_will_save')}</p>
    </div>`;
}

function tenantWeixinScanFailed(scan, error) {
    if (tenantWeixinScan !== scan) return;
    clearTimeout(scan.timer);
    const panel = document.getElementById(scan.statusId);
    if (panel) panel.innerHTML = `<p class="text-sm text-red-500">${escapeHtml(error.message || t('weixin_scan_fail'))}</p>`;
}

function pollTenantWeixinScan(scan) {
    if (tenantWeixinScan !== scan || tenantChannelDraft !== scan.draft) return Promise.resolve();
    return fetch('/api/weixin/qrlogin', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'poll', handle: scan.handle,
            agent_id: scan.agent_id, display_name: scan.display_name }),
    }).then(r => r.json()).then(data => {
        if (tenantWeixinScan !== scan || tenantChannelDraft !== scan.draft) return;
        if (data.status !== 'success') throw new Error(data.message || t('weixin_scan_fail'));
        if (data.saved && data.instance_id) {
            scan.handle = '';
            stopTenantWeixinScan();
            tenantChannelDraft = null;
            tenantChannelRuntimeNotice = data.connected
                ? { applied: true } : { applied: false, reason: data.connection_reason || data.connection };
            return loadTenantChannelsView();
        }
        if (data.terminal) throw new Error(data.error || t('weixin_scan_fail'));
        renderTenantWeixinScan(scan, data);
        scan.timer = setTimeout(() => pollTenantWeixinScan(scan), 2000);
    }).catch(error => tenantWeixinScanFailed(scan, error));
}

function closeTenantChannelForm() {
    stopTenantWeixinScan();
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
    // Guest access: read only when the section is present, so an ordinary edit
    // (or a member's personal connection) omits the fields entirely and keeps
    // whatever policy is stored.
    const guestPolicy = document.getElementById('tenant-channel-guest-policy');
    if (guestPolicy) {
        draft.unbound_user_policy = guestPolicy.value;
        const guestRole = document.getElementById('tenant-channel-guest-role');
        draft.guest_role_id = guestRole ? guestRole.value : '';
        const guestGroups = document.getElementById('tenant-channel-guest-groups');
        draft.guest_group_ids = guestGroups
            ? guestGroups.value.split('\n').map(s => s.trim()).filter(Boolean)
            : [];
    }

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
                unbound_user_policy: draft.unbound_user_policy,
                guest_role_id: draft.guest_role_id,
                guest_group_ids: draft.guest_group_ids,
                expected_version: draft.expected_version,
                credentials: Object.keys(draft.credentials).length ? draft.credentials : undefined,
                recent_password: draft.recent_password,
                scan_ticket: grant,
            }
            : {
                display_name: draft.display_name,
                agent_id: draft.agent_id,
                unbound_user_policy: draft.unbound_user_policy,
                guest_role_id: draft.guest_role_id,
                guest_group_ids: draft.guest_group_ids,
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

function buildChannelCardShell(opts) {
    return window.ChannelWorkbench.cardShell(opts);
}

let _feishuRegisterHandle = '';

let _feishuScanTarget = '';

function renderFeishuPreparing(statusId) {
    const el = document.getElementById(statusId);
    if (!el) return;
    el.innerHTML = `<p class="text-sm text-slate-500 dark:text-slate-400 text-center">`
        + `${t('feishu_scan_loading')}</p>`;
}

// The master usage card is the single context UI; rdai supplies its projection.
let _contextNewSession = false;
function mountContextModule() {
    const button = document.getElementById('clear-context-btn');
    if (button) button.classList.toggle('hidden', !_ctxCan('usage'));
}
function _contextAfterSessionChange(persisted) {
    _contextNewSession = !persisted;
    resetContextUsage();
    mountContextModule();
}
function disposeContextModule() {
    _contextNewSession = false;
    resetContextUsage();
}

let _knowledgeLoadSeq = 0;

let _knowledgeListState = 'loading';

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

function renderKnowledgeUnavailable(data) {
    _knowledgeHasPages = false;
    _knowledgeListState = 'error';
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
    _applyKnowledgePanels();
}

function _knowledgeReadContext() {
    return JSON.stringify([_authEpoch, sessionStorage.getItem('cow_tenant_id'), viewingKnowledgeAgentId()]);
}

const KNOWLEDGE_SOURCE_POLL_MS = 4000;

var _knowledgeSourceSelectedId = '';

let _knowledgeSourceEpoch = 0;

let _knowledgeSourcePollTimer = null;

let _knowledgeSourceLimits = null;

let _knowledgeSourceCapabilities = null;

let _knowledgeSourceItems = [];

let _knowledgeSourceDetailData = null;

let _knowledgeUploadAttempt = null;

let _knowledgeUploadTarget = null;

const _knowledgeUploadQueue = [];

function _knowledgeSourceSize(bytes) {
    const n = Number(bytes) || 0;
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function knowledgeSourcesOnLibraryChange() {
    _knowledgeSourceSelectedId = '';
    _knowledgeSourceDetailData = null;
    _knowledgeSourceItems = [];
    _knowledgeSourceCapabilities = null;
    _knowledgeSourceUploadReset();
    _knowledgeSourceEpoch += 1;
    stopKnowledgeSourcePolling();
    // The rows of the library just left must not stay on screen: paint the
    // loading state until the new library answers, so the panel never reads as
    // "this Agent has these sources".
    const list = document.getElementById('knowledge-sources-list');
    const empty = document.getElementById('knowledge-sources-empty');
    const errorEl = document.getElementById('knowledge-sources-error');
    const loading = document.getElementById('knowledge-sources-loading');
    const noMatch = document.getElementById('knowledge-sources-no-match');
    const filtersBar = document.getElementById('knowledge-sources-filters');
    const usage = document.getElementById('knowledge-sources-usage');
    if (list) { list.innerHTML = ''; list.classList.add('hidden'); }
    if (empty) empty.classList.add('hidden');
    if (noMatch) noMatch.classList.add('hidden');
    if (filtersBar) filtersBar.classList.add('hidden');
    if (errorEl) { errorEl.classList.add('hidden'); errorEl.textContent = ''; }
    if (loading) loading.classList.remove('hidden');
    if (usage) usage.textContent = '';
    const detail = document.getElementById('knowledge-source-detail');
    const placeholder = document.getElementById('knowledge-source-detail-placeholder');
    if (detail) detail.classList.add('hidden');
    if (placeholder) placeholder.classList.remove('hidden');
}

function stopKnowledgeSourcePolling() {
    if (_knowledgeSourcePollTimer === null) return;
    clearInterval(_knowledgeSourcePollTimer);
    _knowledgeSourcePollTimer = null;
}

function _knowledgeSourcesBusy(items) {
    return (items || []).some(item => {
        const task = item.latest_task;
        return !!task && (task.status === 'queued' || task.status === 'running');
    });
}

function _maybeStartKnowledgeSourcePolling() {
    if (!_knowledgeSourcesBusy(_knowledgeSourceItems)) {
        stopKnowledgeSourcePolling();
        return;
    }
    if (_knowledgeSourcePollTimer !== null) return;
    _knowledgeSourcePollTimer = setInterval(() => {
        if (currentView !== 'knowledge' || _knowledgeTab !== 'sources') {
            stopKnowledgeSourcePolling();
            return;
        }
        loadKnowledgeSources({ quiet: true });
    }, KNOWLEDGE_SOURCE_POLL_MS);
}

function _knowledgeSourceStateOf(item) {
    const task = item.latest_task;
    if (task && task.status === 'failed') {
        return { key: 'knowledge_sources_state_failed', cls: 'text-rose-500 dark:text-rose-400' };
    }
    if (task && (task.status === 'queued' || task.status === 'running')) {
        return { key: 'knowledge_sources_state_converting', cls: 'text-amber-500 dark:text-amber-400' };
    }
    if (item.lifecycle === 'disabled') {
        return { key: 'knowledge_sources_state_disabled', cls: 'text-slate-400 dark:text-slate-500' };
    }
    if (item.searchable) {
        return { key: 'knowledge_sources_state_searchable', cls: 'text-emerald-600 dark:text-emerald-400' };
    }
    return { key: 'knowledge_sources_state_saved', cls: 'text-slate-400 dark:text-slate-500' };
}

function _knowledgeSourceById(sourceId) {
    return _knowledgeSourceItems.find(item => item.source_id === sourceId) || null;
}

function _knowledgeSourceTime(value) {
    if (!value) return '';
    const at = new Date(value);
    if (Number.isNaN(at.getTime())) return String(value);
    return at.toLocaleString();
}

function _knowledgeSourceFilters() {
    const read = (id, fallback) => {
        const el = document.getElementById(id);
        return el && typeof el.value === 'string' ? el.value : fallback;
    };
    return {
        query: read('knowledge-sources-filter', '').trim().toLowerCase(),
        category: read('knowledge-sources-category', ''),
        ext: read('knowledge-sources-ext', ''),
        status: read('knowledge-sources-status', ''),
    };
}

function _knowledgeSourceExt(item) {
    const name = String(item.name || '');
    const at = name.lastIndexOf('.');
    return at > 0 ? name.slice(at).toLowerCase() : '';
}

function _knowledgeSourceStatusOf(item) {
    if (item.lifecycle === 'disabled') return 'disabled';
    const task = item.latest_task;
    if (task && task.status === 'failed') return 'failed';
    if (task && (task.status === 'queued' || task.status === 'running')) return 'converting';
    return item.searchable ? 'searchable' : 'saved';
}

function _knowledgeSourceMatches(item, filters) {
    if (filters.query && !String(item.name || '').toLowerCase().includes(filters.query)) return false;
    if (filters.category && (item.category || '') !== filters.category) return false;
    if (filters.ext && _knowledgeSourceExt(item) !== filters.ext) return false;
    if (filters.status && _knowledgeSourceStatusOf(item) !== filters.status) return false;
    return true;
}

function _knowledgeSourceOption(value, label, selected) {
    return `<option value="${escapeHtml(value)}"${value === selected ? ' selected' : ''}>${escapeHtml(label)}</option>`;
}

function _knowledgeSourceFillSelect(id, options) {
    const el = document.getElementById(id);
    if (!el) return;
    const current = typeof el.value === 'string' ? el.value : '';
    el.innerHTML = options.join('');
    if (current && options.some(option => option.startsWith(`<option value="${escapeHtml(current)}"`))) {
        el.value = current;
    } else {
        el.value = '';
    }
}

function _knowledgeSourceRenderFilters(items) {
    const bar = document.getElementById('knowledge-sources-filters');
    if (bar) bar.classList.remove('hidden');
    const categories = [...new Set(items.map(item => item.category || ''))].sort();
    const exts = [...new Set(items.map(_knowledgeSourceExt).filter(Boolean))].sort();
    _knowledgeSourceFillSelect('knowledge-sources-category',
        [_knowledgeSourceOption('', t('knowledge_sources_filter_all_categories'), '')]
            .concat(categories.map(value => _knowledgeSourceOption(
                value, value || t('knowledge_sources_uncategorized'), ''))));
    _knowledgeSourceFillSelect('knowledge-sources-ext',
        [_knowledgeSourceOption('', t('knowledge_sources_filter_all_exts'), '')]
            .concat(exts.map(value => _knowledgeSourceOption(value, value, ''))));
    _knowledgeSourceFillSelect('knowledge-sources-status',
        [_knowledgeSourceOption('', t('knowledge_sources_filter_all_status'), '')]
                .concat(['searchable', 'saved', 'converting', 'failed', 'disabled']
                .map(value => _knowledgeSourceOption(
                    value, t(`knowledge_sources_state_${value}`), ''))));
}

function _knowledgeSourceRowHtml(item) {
    const state = _knowledgeSourceStateOf(item);
    const task = item.latest_task;
    const taskError = (task && task.status === 'failed' && task.error)
        ? `<p class="mt-1 text-[11px] text-rose-500 dark:text-rose-400 truncate">${escapeHtml(task.error)}</p>`
        : '';
    const selected = item.source_id === _knowledgeSourceSelectedId
        ? ' bg-slate-50 dark:bg-white/5' : '';
    // The version whose body is in effect is a different fact from the newest
    // original: v2 can be stored while v1 is still the searchable one.
    const versions = t('knowledge_sources_latest_version').replace('{version}', item.latest_version)
        + (item.active_version
            ? ` · ${t('knowledge_sources_active_version').replace('{version}', item.active_version)}`
            : '');
    return `
        <button type="button" data-source-id="${escapeHtml(item.source_id)}"
                data-status="${_knowledgeSourceStatusOf(item)}"
                data-latest-version="${item.latest_version}"
                data-active-version="${item.active_version || 0}"
                onclick="openKnowledgeSourceDetail('${item.source_id}')"
                class="knowledge-source-row w-full text-left px-4 py-2.5 border-b border-slate-100 dark:border-white/5 hover:bg-slate-50 dark:hover:bg-white/5 cursor-pointer transition-colors${selected}">
            <div class="flex items-center gap-2">
                <i class="fas ${item.searchable ? 'fa-circle-check text-emerald-500' : 'fa-file-shield text-slate-400'} text-xs"></i>
                <span class="flex-1 truncate text-xs font-medium text-slate-700 dark:text-slate-200">${escapeHtml(item.name)}</span>
                <span class="text-[10px] flex-shrink-0 ${state.cls}">${t(state.key)}</span>
            </div>
            <div class="mt-1 flex flex-wrap items-center gap-x-2 text-[11px] text-slate-400 dark:text-slate-500">
                ${item.category ? `<span class="truncate">${escapeHtml(item.category)}</span>` : `<span>${t('knowledge_sources_uncategorized')}</span>`}
                <span>${escapeHtml(_knowledgeSourceExt(item) || '—')}</span>
                <span>${_knowledgeSourceSize(item.size)}</span>
                <span>${versions}</span>
                <span>${escapeHtml(_knowledgeSourceTime(item.updated_at))}</span>
            </div>${taskError}
        </button>`;
}

function renderKnowledgeSourceCapabilities() {
    const upload = _knowledgeUploadCapability();
    const button = document.getElementById('knowledge-upload-btn');
    if (!button) return;
    button.classList.toggle('hidden', !upload.available);
    // The reason lives on the control even while it is hidden: revealing it is
    // how the page explains "why is there no upload button here".
    button.setAttribute('title', upload.reason || '');
}

function renderKnowledgeSources() {
    const list = document.getElementById('knowledge-sources-list');
    const empty = document.getElementById('knowledge-sources-empty');
    const noMatch = document.getElementById('knowledge-sources-no-match');
    const filtersBar = document.getElementById('knowledge-sources-filters');
    const loading = document.getElementById('knowledge-sources-loading');
    const errorEl = document.getElementById('knowledge-sources-error');
    const usage = document.getElementById('knowledge-sources-usage');
    const note = document.getElementById('knowledge-sources-note');
    const items = _knowledgeSourceItems;

    if (loading) loading.classList.add('hidden');
    if (errorEl) { errorEl.classList.add('hidden'); errorEl.textContent = ''; }
    if (note) note.textContent = t('knowledge_sources_note');

    const total = items.reduce((sum, item) => sum + (Number(item.size) || 0), 0);
    if (usage) {
        usage.textContent = t('knowledge_sources_usage')
            .replace('{count}', String(items.length))
            .replace('{size}', _knowledgeSourceSize(total));
    }

    // Nothing stored at all: the filters have nothing to filter and the empty
    // state is the honest answer, not "no match".
    if (!items.length) {
        if (filtersBar) filtersBar.classList.add('hidden');
        if (list) { list.innerHTML = ''; list.classList.add('hidden'); }
        if (noMatch) noMatch.classList.add('hidden');
        if (empty) empty.classList.remove('hidden');
        renderKnowledgeSourceCapabilities();
        return;
    }
    if (empty) empty.classList.add('hidden');
    _knowledgeSourceRenderFilters(items);

    const filters = _knowledgeSourceFilters();
    const shown = items.filter(item => _knowledgeSourceMatches(item, filters));
    if (list) {
        list.classList.toggle('hidden', !shown.length);
        list.innerHTML = shown.map(_knowledgeSourceRowHtml).join('');
    }
    // "Stored, but filtered out" is not "nothing stored": say which one it is.
    if (noMatch) noMatch.classList.toggle('hidden', !!shown.length);
    renderKnowledgeSourceCapabilities();
}

function renderKnowledgeSourcesError(message) {
    const loading = document.getElementById('knowledge-sources-loading');
    const errorEl = document.getElementById('knowledge-sources-error');
    const list = document.getElementById('knowledge-sources-list');
    const empty = document.getElementById('knowledge-sources-empty');
    const noMatch = document.getElementById('knowledge-sources-no-match');
    const filtersBar = document.getElementById('knowledge-sources-filters');
    if (loading) loading.classList.add('hidden');
    if (empty) empty.classList.add('hidden');
    if (noMatch) noMatch.classList.add('hidden');
    if (filtersBar) filtersBar.classList.add('hidden');
    if (list) { list.innerHTML = ''; list.classList.add('hidden'); }
    if (errorEl) {
        errorEl.classList.remove('hidden');
        errorEl.textContent = String(message == null ? '' : message);
    }
}

async function loadKnowledgeSources(opts) {
    const quiet = !!(opts && opts.quiet);
    const epoch = ++_knowledgeSourceEpoch;
    const loading = document.getElementById('knowledge-sources-loading');
    const errorEl = document.getElementById('knowledge-sources-error');
    if (loading && !quiet) loading.classList.remove('hidden');
    if (errorEl && !quiet) { errorEl.classList.add('hidden'); errorEl.textContent = ''; }

    let data;
    try {
        const response = await fetch(_kbUrl('/api/knowledge/sources'));
        data = await response.json();
    } catch (exc) {
        if (epoch !== _knowledgeSourceEpoch) return null;
        renderKnowledgeSourcesError((exc && exc.message) || exc);
        return null;
    }
    // A newer read (or a library switch) owns the panel now; this answer is
    // about a page state the user has already left.
    if (epoch !== _knowledgeSourceEpoch) return null;

    if (!data || data.status !== 'success') {
        renderKnowledgeSourcesError((data && data.message) || t('knowledge_unavailable'));
        return null;
    }
    _knowledgeSourceItems = data.sources || [];
    _knowledgeSourceLimits = data.limits || _knowledgeSourceLimits;
    _knowledgeSourceCapabilities = data.capabilities || _knowledgeSourceCapabilities;
    renderKnowledgeSources();
    _maybeStartKnowledgeSourcePolling();
    // A reload must not keep a row that the server no longer lists.
    if (_knowledgeSourceSelectedId && !_knowledgeSourceById(_knowledgeSourceSelectedId)) {
        _knowledgeSourceSelectedId = '';
        _knowledgeSourceDetailData = null;
        const detail = document.getElementById('knowledge-source-detail');
        const placeholder = document.getElementById('knowledge-source-detail-placeholder');
        if (detail) detail.classList.add('hidden');
        if (placeholder) placeholder.classList.remove('hidden');
    }
    return data;
}

function _knowledgeSourceAction(key, handler, extra) {
    return `<button type="button" onclick="${handler}"
                    class="px-2.5 py-1 rounded-md border border-slate-200 dark:border-white/10
                           text-[11px] text-slate-600 dark:text-slate-300 hover:bg-slate-50
                           dark:hover:bg-white/5 cursor-pointer transition-colors${extra || ''}">${t(key)}</button>`;
}

function _knowledgeSourceDetailHtml(data) {
    const item = data.source || {};
    const versions = data.versions || [];
    const tasks = data.tasks || [];
    const caps = _knowledgeSourceCapabilities || {};
    const conversion = caps.conversion || { available: false, reason: '' };

    const versionRows = versions.map(v => `
        <li class="flex items-center gap-2 py-1.5">
            <span class="text-[11px] text-slate-400 dark:text-slate-500 flex-shrink-0">v${v.version}</span>
            <span class="flex-1 truncate text-xs text-slate-700 dark:text-slate-200">${escapeHtml(v.original_name)}</span>
            <span class="text-[11px] text-slate-400 dark:text-slate-500 flex-shrink-0">${_knowledgeSourceSize(v.size)}</span>
            <span class="text-[10px] flex-shrink-0 ${v.commit_state === 'committed' ? 'text-slate-400' : 'text-amber-500'}">${escapeHtml(v.commit_state)}</span>
            <button type="button" onclick="downloadKnowledgeSource('${item.source_id}', ${v.version})"
                    class="flex-shrink-0 text-[11px] text-primary-500 hover:underline cursor-pointer">${t('knowledge_sources_download')}</button>
        </li>`).join('');

    const taskRows = tasks.map(task => {
        const pending = task.status === 'queued' || task.status === 'running';
        const cancel = pending && task.task_type === 'convert'
            ? _knowledgeSourceAction('knowledge_sources_cancel',
                `cancelKnowledgeSourceTask('${task.task_id}')`) : '';
        const retry = task.status === 'failed'
            ? _knowledgeSourceAction('knowledge_sources_retry',
                `retryKnowledgeSourceTask('${task.task_id}')`) : '';
        return `
        <li class="flex items-center gap-2 py-1.5">
            <span class="text-[11px] text-slate-500 dark:text-slate-400 flex-shrink-0">${escapeHtml(task.task_type)}</span>
            <span class="text-[11px] flex-shrink-0 ${task.status === 'failed' ? 'text-rose-500' : 'text-slate-400'}">${escapeHtml(task.status)}</span>
            <span class="flex-1 truncate text-[11px] text-slate-400 dark:text-slate-500">${escapeHtml(task.error || task.stage || '')}</span>
            ${retry}${cancel}
        </li>`;
    }).join('');

    const conversionAction = conversion.available
        ? _knowledgeSourceAction('knowledge_sources_convert',
            `requestKnowledgeSourceConversion('${item.source_id}')`)
        // No button at all while the deployment cannot convert: a control that
        // always refuses looks like a bug, the reason looks like an answer.
        : `<p class="text-[11px] text-slate-400 dark:text-slate-500">${escapeHtml(conversion.reason || '')}</p>`;

    // The body in effect is a separate fact from the newest original, and when
    // there is none the reason has to be stated: "saved" must never read as
    // "searchable".
    const bodyTone = item.searchable ? 'text-emerald-600 dark:text-emerald-400' : 'text-slate-400 dark:text-slate-500';
    const bodyText = item.searchable
        ? t('knowledge_sources_body_published').replace('{version}', item.active_version)
        : t('knowledge_sources_body_missing');

    const lifecycle = item.lifecycle === 'active'
        ? _knowledgeSourceAction('knowledge_sources_disable',
            `setKnowledgeSourceLifecycle('${item.source_id}', 'disable')`)
        : _knowledgeSourceAction('knowledge_sources_enable',
            `setKnowledgeSourceLifecycle('${item.source_id}', 'enable')`);

    const infoRow = (key, value) => `<div class="flex items-baseline gap-2 py-0.5">
            <span class="text-[11px] text-slate-400 dark:text-slate-500 w-20 flex-shrink-0">${t(key)}</span>
            <span class="text-[11px] text-slate-600 dark:text-slate-300 break-all">${escapeHtml(value)}</span>
        </div>`;

    return `
        <p class="text-[11px] text-slate-500 dark:text-slate-400">
            ${t(item.searchable ? 'knowledge_sources_searchable_hint' : 'knowledge_sources_saved_hint')}
        </p>
        <div class="mt-4">
            <span class="text-xs font-medium text-slate-600 dark:text-slate-300">${t('knowledge_sources_info')}</span>
            <div class="mt-1">
                ${infoRow('knowledge_sources_field_category', item.category || t('knowledge_sources_uncategorized'))}
                ${infoRow('knowledge_sources_field_type', _knowledgeSourceExt(item) || '—')}
                ${infoRow('knowledge_sources_field_size', _knowledgeSourceSize(item.size))}
                ${infoRow('knowledge_sources_field_latest_version', `v${item.latest_version}`)}
                ${infoRow('knowledge_sources_field_active_version',
                    item.active_version ? `v${item.active_version}` : t('knowledge_sources_active_version_none'))}
                ${infoRow('knowledge_sources_field_created', _knowledgeSourceTime(item.created_at))}
                ${infoRow('knowledge_sources_field_updated', _knowledgeSourceTime(item.updated_at))}
            </div>
        </div>
        <div class="mt-4">
            <span class="text-xs font-medium text-slate-600 dark:text-slate-300">${t('knowledge_sources_versions')}</span>
            <ul class="mt-1 divide-y divide-slate-100 dark:divide-white/5">${versionRows || ''}</ul>
            <div class="mt-2">
                ${_knowledgeSourceAction('knowledge_sources_add_version',
                    `openKnowledgeUploadPanel('${item.source_id}')`)}
            </div>
        </div>
        <div class="mt-4">
            <span class="text-xs font-medium text-slate-600 dark:text-slate-300">${t('knowledge_sources_body')}</span>
            <p class="mt-1 text-[11px] ${bodyTone}">${bodyText}</p>
        </div>
        <div class="mt-4">
            <span class="text-xs font-medium text-slate-600 dark:text-slate-300">${t('knowledge_sources_tasks')}</span>
            <ul class="mt-1 divide-y divide-slate-100 dark:divide-white/5">${taskRows || `<li class="py-1.5 text-[11px] text-slate-400 dark:text-slate-500">${t('knowledge_sources_no_tasks')}</li>`}</ul>
        </div>
        <div class="mt-4 flex flex-wrap items-center gap-2">
            <span class="text-xs font-medium text-slate-600 dark:text-slate-300">${t('knowledge_sources_convert')}</span>
            ${conversionAction}
        </div>
        <div class="mt-3 flex flex-wrap items-center gap-2">
            <span class="text-xs font-medium text-slate-600 dark:text-slate-300">${t('knowledge_sources_lifecycle')}</span>
            ${lifecycle}
            ${_knowledgeSourceAction('knowledge_sources_delete',
                `setKnowledgeSourceLifecycle('${item.source_id}', 'delete')`)}
        </div>`;
}

function renderKnowledgeSourceDetail() {
    const data = _knowledgeSourceDetailData;
    const panel = document.getElementById('knowledge-source-detail');
    const placeholder = document.getElementById('knowledge-source-detail-placeholder');
    const title = document.getElementById('knowledge-source-detail-title');
    const stateEl = document.getElementById('knowledge-source-detail-state');
    const body = document.getElementById('knowledge-source-detail-body');
    if (!data || !data.source) return;
    if (title) title.textContent = data.source.name || '';
    if (stateEl) {
        const state = _knowledgeSourceStateOf(data.source);
        stateEl.className = `text-[11px] ml-auto flex-shrink-0 ${state.cls}`;
        stateEl.textContent = t(state.key);
    }
    if (body) body.innerHTML = _knowledgeSourceDetailHtml(data);
    if (placeholder) placeholder.classList.add('hidden');
    if (panel) panel.classList.remove('hidden');
}

async function openKnowledgeSourceDetail(sourceId) {
    _knowledgeSourceSelectedId = sourceId;
    _knowledgeSourceDetailData = null;
    renderKnowledgeSources();
    const epoch = _knowledgeSourceEpoch;
    let data;
    try {
        const response = await fetch(_kbUrl(
            `/api/knowledge/sources/detail?source_id=${encodeURIComponent(sourceId)}`));
        data = await response.json();
    } catch (exc) {
        if (epoch !== _knowledgeSourceEpoch || _knowledgeSourceSelectedId !== sourceId) return null;
        _setKnowledgeStatus((exc && exc.message) || exc, true, true);
        return null;
    }
    // Another row (or another library) was opened while this was in flight.
    if (epoch !== _knowledgeSourceEpoch || _knowledgeSourceSelectedId !== sourceId) return null;
    if (!data || data.status !== 'success') {
        _setKnowledgeStatus((data && data.message) || t('knowledge_unavailable'), true, true);
        return null;
    }
    _knowledgeSourceDetailData = data;
    renderKnowledgeSourceDetail();
    return data;
}

function knowledgeSourceDetailBack() {
    _knowledgeSourceSelectedId = '';
    _knowledgeSourceDetailData = null;
    const panel = document.getElementById('knowledge-source-detail');
    const placeholder = document.getElementById('knowledge-source-detail-placeholder');
    if (panel) panel.classList.add('hidden');
    if (placeholder) placeholder.classList.remove('hidden');
    renderKnowledgeSources();
}

function downloadKnowledgeSource(sourceId, version) {
    let url = `/api/knowledge/sources/download?source_id=${encodeURIComponent(sourceId)}`;
    if (version) url += `&version=${encodeURIComponent(version)}`;
    window.location.href = _kbUrl(url);
}

function _knowledgeSourcePostJson(path, payload, onDone) {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', path);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText || '{}'); } catch (exc) { data = {}; }
        if (onDone) onDone(data, xhr.status);
    };
    xhr.onerror = () => {
        if (onDone) onDone({ status: 'error', message: t('knowledge_sources_request_failed') }, 0);
    };
    xhr.send(JSON.stringify(payload));
    return xhr;
}

async function setKnowledgeSourceLifecycle(sourceId, action) {
    const known = _knowledgeSourceById(sourceId) || {};
    if (action === 'delete') {
        // Deleting is not one row: it removes every original version and any
        // published body that consumers are searching right now. State what it
        // takes away before asking, instead of a generic "are you sure".
        const body = known.searchable
            ? t('knowledge_sources_delete_body_published').replace('{version}', known.active_version)
            : t('knowledge_sources_delete_body_none');
        const message = t('knowledge_sources_delete_confirm')
            .replace('{versions}', String(known.latest_version || 0))
            .replace('{body}', body);
        if (!window.confirm(message)) return null;
    }
    _knowledgeSourcePostJson('/api/knowledge/sources/lifecycle',
        { source_id: sourceId, action: action, agent_id: viewingKnowledgeAgentId() },
        (data) => {
            if (!data || data.status !== 'success') {
                _setKnowledgeStatus((data && data.message) || t('knowledge_sources_request_failed'), true, true);
                return;
            }
            // A delete takes the row with it; keeping a selection would leave the
            // detail pointing at something the list no longer has.
            if (action === 'delete' && _knowledgeSourceSelectedId === sourceId) {
                knowledgeSourceDetailBack();
            }
            _setKnowledgeStatus(t(`knowledge_sources_lifecycle_done_${action}`), false, true);
            loadKnowledgeSources({ quiet: true });
        });
    return known ? known.source_id : sourceId;
}

async function retryKnowledgeSourceTask(taskId) {
    _knowledgeSourcePostJson('/api/knowledge/sources/task',
        { task_id: taskId, action: 'retry', agent_id: viewingKnowledgeAgentId() },
        (data) => {
            if (!data || data.status !== 'success') {
                _setKnowledgeStatus((data && data.message) || t('knowledge_sources_request_failed'), true, true);
                return;
            }
            _setKnowledgeStatus(t('knowledge_sources_retry_started'), false, true);
            loadKnowledgeSources({ quiet: true });
            if (_knowledgeSourceSelectedId) openKnowledgeSourceDetail(_knowledgeSourceSelectedId);
        });
    return taskId;
}

async function cancelKnowledgeSourceTask(taskId) {
    _knowledgeSourcePostJson('/api/knowledge/sources/task',
        { task_id: taskId, action: 'cancel', agent_id: viewingKnowledgeAgentId() },
        (data) => {
            if (!data || data.status !== 'success') {
                _setKnowledgeStatus((data && data.message) || t('knowledge_sources_request_failed'), true, true);
                return;
            }
            _setKnowledgeStatus(t('knowledge_sources_cancel_done'), false, true);
            loadKnowledgeSources({ quiet: true });
            if (_knowledgeSourceSelectedId) openKnowledgeSourceDetail(_knowledgeSourceSelectedId);
        });
    return taskId;
}

async function requestKnowledgeSourceConversion(sourceId) {
    _knowledgeSourcePostJson('/api/knowledge/sources/task',
        { source_id: sourceId, action: 'convert', agent_id: viewingKnowledgeAgentId() },
        (data) => {
            // Phase C owns the executor; until it lands the server refuses with
            // the projected reason, and the reason is the useful answer.
            if (!data || data.status !== 'success') {
                _setKnowledgeStatus((data && data.message) || t('knowledge_sources_request_failed'), true, true);
                return;
            }
            _setKnowledgeStatus(t('knowledge_sources_convert_started'), false, true);
            loadKnowledgeSources({ quiet: true });
            if (_knowledgeSourceSelectedId) openKnowledgeSourceDetail(_knowledgeSourceSelectedId);
        });
    return sourceId;
}

function _knowledgeUploadRequestId() {
    return `src-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function _knowledgeUploadLimits() {
    return _knowledgeSourceLimits || {};
}

function _knowledgeSourceUploadReset() {
    _knowledgeUploadQueue.length = 0;
    _knowledgeUploadAttempt = null;
    _knowledgeUploadTarget = null;
    _renderKnowledgeUploadQueue();
}

function _knowledgeUploadCapability() {
    return (_knowledgeSourceCapabilities || {}).source_upload
        || { available: false, reason: '' };
}

function _knowledgeUploadRefused() {
    const upload = _knowledgeUploadCapability();
    if (upload.available) return false;
    _setKnowledgeStatus(upload.reason || t('knowledge_sources_request_failed'), true, true);
    return true;
}

function openKnowledgeUploadPanel(sourceId) {
    const panel = document.getElementById('knowledge-upload-panel');
    if (!panel) return;
    if (_knowledgeUploadRefused()) return;
    _knowledgeUploadTarget = sourceId || null;
    const limits = _knowledgeUploadLimits();
    const target = document.getElementById('knowledge-upload-target');
    const known = sourceId ? _knowledgeSourceById(sourceId) : null;
    if (target) {
        target.textContent = known
            ? t('knowledge_upload_target_version').replace('{name}', known.name)
            : `${t('knowledge_upload_target')}${viewingKnowledgeAgentId()}`;
    }
    const limitsEl = document.getElementById('knowledge-upload-limits');
    if (limitsEl) {
        limitsEl.textContent = [
            t('knowledge_upload_limits'),
            `${limits.max_files || 0}`,
            _knowledgeSourceSize(limits.max_file_size),
            _knowledgeSourceSize(limits.max_batch_size),
        ].join(' · ');
    }
    const conversion = (_knowledgeSourceCapabilities || {}).conversion || { available: false, reason: '' };
    const convert = document.getElementById('knowledge-upload-convert');
    const hint = document.getElementById('knowledge-upload-convert-hint');
    if (convert) {
        convert.disabled = !conversion.available;
        if (!conversion.available) convert.checked = false;
    }
    if (hint) hint.textContent = conversion.available ? '' : (conversion.reason || '');
    // A new version extends one named source: the same-name question does not
    // apply, and answering it here would only confuse the choice.
    const conflict = document.getElementById('knowledge-upload-conflict');
    if (conflict) conflict.disabled = !!sourceId;
    panel.classList.remove('hidden');
    _renderKnowledgeUploadQueue();
}

function closeKnowledgeUploadPanel() {
    const panel = document.getElementById('knowledge-upload-panel');
    if (panel) panel.classList.add('hidden');
    // Files do not survive the panel: a stale queue would let the next open
    // submit bytes the user picked for a different attempt.
    _knowledgeSourceUploadReset();
}

function pickKnowledgeUploadFiles() {
    const input = document.getElementById('knowledge-upload-input');
    if (input) input.click();
}

function addKnowledgeUploadFiles(files) {
    // A drop reaches this function directly from markup, without going through
    // the panel first: the refusal has to be repeated here or a drop on a
    // disabled library would queue bytes that the next save is refused for.
    if (_knowledgeUploadRefused()) return;
    const limits = _knowledgeUploadLimits();
    const max = Number(limits.max_file_size) || 0;
    for (const file of Array.from(files || [])) {
        const name = String((file && file.name) || '');
        const size = Number((file && file.size) || 0);
        if (!name) continue;
        // Refused here rather than after a round trip: the server's limit is the
        // same number, and a local failure keeps the batch from aborting.
        if (max && size > max) {
            _knowledgeUploadQueue.push({
                file, name, size, status: 'failed', reason: 'knowledge_upload_item_too_large',
            });
            continue;
        }
        _knowledgeUploadQueue.push({ file, name, size, status: 'queued', reason: '' });
    }
    _renderKnowledgeUploadQueue();
}

function removeKnowledgeUploadFile(index) {
    const entry = _knowledgeUploadQueue[index];
    if (!entry) return;
    if (entry.status === 'uploading') return;
    _knowledgeUploadQueue.splice(index, 1);
    _renderKnowledgeUploadQueue();
}

function _knowledgeUploadConflictChoice(index, strategy, key) {
    return `<button type="button" onclick="resolveKnowledgeUploadConflict(${index}, '${strategy}')"
                    class="px-2 py-1 rounded-md border border-slate-200 dark:border-white/10
                           text-[11px] text-slate-600 dark:text-slate-300 hover:bg-slate-50
                           dark:hover:bg-white/5 cursor-pointer transition-colors">${t(key)}</button>`;
}

function _knowledgeUploadItemHtml(entry, index) {
    const icon = {
        queued: 'fa-clock text-slate-400',
        uploading: 'fa-spinner fa-spin text-primary-500',
        saved: 'fa-circle-check text-emerald-500',
        reused: 'fa-recycle text-sky-500',
        conflict: 'fa-triangle-exclamation text-amber-500',
        failed: 'fa-circle-xmark text-rose-500',
    }[entry.status] || 'fa-clock text-slate-400';
    // A conflict is never resolved by overwriting: the two outcomes are named
    // and picked, and the choice travels as its own request.
    const choice = entry.status === 'conflict'
        ? `<span class="flex items-center gap-1.5">
                ${_knowledgeUploadConflictChoice(index, 'new', 'knowledge_upload_conflict_new')}
                ${_knowledgeUploadConflictChoice(index, 'update', 'knowledge_upload_conflict_update')}
           </span>`
        : `<span class="text-[11px] text-slate-500 dark:text-slate-400">${t('knowledge_upload_item_' + entry.status)}</span>`;
    const reason = entry.reason && entry.status !== 'conflict'
        ? `<span class="text-[11px] text-slate-400 dark:text-slate-500 truncate">${escapeHtml(t(entry.reason))}</span>`
        : '';
    return `
        <div class="knowledge-upload-item flex items-center gap-2 rounded-lg px-2.5 py-2 bg-slate-50 dark:bg-white/5" data-status="${entry.status}">
            <i class="fas ${icon} text-xs"></i>
            <span class="flex-1 truncate text-xs text-slate-700 dark:text-slate-200">${escapeHtml(entry.name)}</span>
            <span class="text-[11px] text-slate-400 dark:text-slate-500 flex-shrink-0">${_knowledgeSourceSize(entry.size)}</span>
            ${reason}${choice}
            <button type="button" onclick="removeKnowledgeUploadFile(${index})"
                    class="p-0.5 text-slate-300 hover:text-slate-500 dark:hover:text-slate-300 cursor-pointer">
                <i class="fas fa-xmark text-[11px]"></i>
            </button>
        </div>`;
}

function _renderKnowledgeUploadQueue() {
    const queue = document.getElementById('knowledge-upload-queue');
    const summary = document.getElementById('knowledge-upload-summary');
    const submit = document.getElementById('knowledge-upload-submit');
    if (queue) {
        const progress = _knowledgeUploadAttempt && _knowledgeUploadAttempt.progress !== undefined
            ? `<div class="text-[11px] text-slate-400 dark:text-slate-500">${t('knowledge_upload_progress')} ${_knowledgeUploadAttempt.progress}%</div>`
            : '';
        queue.innerHTML = progress + _knowledgeUploadQueue.map(_knowledgeUploadItemHtml).join('');
    }
    if (summary) {
        const counts = {};
        _knowledgeUploadQueue.forEach(entry => { counts[entry.status] = (counts[entry.status] || 0) + 1; });
        summary.textContent = Object.keys(counts)
            .map(status => `${t('knowledge_upload_item_' + status)} ${counts[status]}`).join(' · ');
    }
    if (submit) {
        submit.disabled = !_knowledgeUploadQueue.some(entry => entry.status === 'queued');
    }
}

function _knowledgeUploadSetProgress(percent) {
    if (!_knowledgeUploadAttempt) return;
    _knowledgeUploadAttempt.progress = percent;
    _renderKnowledgeUploadQueue();
}

function _knowledgeUploadApply(results) {
    const list = results || [];
    const attempt = _knowledgeUploadAttempt;
    const entries = (attempt && attempt.entries) || [];
    entries.forEach((entry, index) => {
        // Results arrive in request order, with server-side per-file failures
        // appended; a filename lookup covers the appended tail.
        let result = list[index];
        if (!result || result.filename !== entry.name) {
            result = list.find(item => item.filename === entry.name) || result;
        }
        if (result) {
            entry.status = result.status || 'failed';
            entry.reason = result.status === 'conflict' ? '' : (result.reason || result.code || '');
            entry.existing = result.existing || null;
            entry.sourceId = result.source_id || null;
            return;
        }
        if (entry.status === 'uploading') { entry.status = 'failed'; entry.reason = ''; }
    });
    if (attempt) attempt.progress = undefined;
    _renderKnowledgeUploadQueue();
}

function _knowledgeUploadSend(entries, conflict, target) {
    const form = new FormData();
    form.append('agent_id', viewingKnowledgeAgentId());
    form.append('conflict', conflict);
    form.append('request_id', _knowledgeUploadAttempt.requestId);
    const category = document.getElementById('knowledge-upload-category');
    if (category && category.value.trim()) form.append('category', category.value.trim());
    if (target && target.sourceId) {
        // Only an explicit "add a version" names the source it extends, and it
        // pins the version the choice was made against so a concurrent upload
        // is reported instead of silently overwritten.
        form.append('target_source_id', target.sourceId);
        if (target.version !== undefined && target.version !== null) {
            form.append('expected_version', String(target.version));
        }
    }
    entries.forEach(entry => form.append('files', entry.file));

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/knowledge/sources/upload');
    // This request is an XHR rather than a `fetch` (progress reporting needs
    // one), so the console's wrapper never sees it: the tenant selection has to
    // be attached here. Without it the identity gate answers 400
    // missing_tenant before the handler reads the body, and the batch dies as
    // "保存失败" with the progress bar stuck mid-way.
    const tenantId = tenantSelectionHeader('/api/knowledge/sources/upload');
    if (tenantId) xhr.setRequestHeader('X-Tenant-ID', tenantId);
    xhr.upload.onprogress = (event) => {
        if (!event || !event.lengthComputable || !event.total) return;
        _knowledgeUploadSetProgress(Math.round((event.loaded / event.total) * 100));
    };
    xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText || '{}'); } catch (exc) { data = {}; }
        if (!data || data.status !== 'success') {
            entries.forEach(entry => {
                if (entry.status === 'uploading') {
                    entry.status = 'failed';
                    entry.reason = (data && data.message) || '';
                }
            });
            if (_knowledgeUploadAttempt) _knowledgeUploadAttempt.progress = undefined;
            _renderKnowledgeUploadQueue();
            _setKnowledgeStatus((data && data.message) || t('knowledge_sources_request_failed'), true, true);
            return;
        }
        _knowledgeUploadApply(data.results || []);
        const counts = { saved: data.saved || 0, reused: data.reused || 0, conflict: data.conflict || 0, failed: data.failed || 0 };
        _setKnowledgeStatus(
            Object.keys(counts).map(key => `${t('knowledge_upload_item_' + key)} ${counts[key]}`).join(' · '),
            counts.failed > 0, true);
        // "Save then convert" is a second, explicit request per source: the
        // upload route stores bytes only (the server has no convert flag), and
        // the conversion route is where a refusal carries its reason.
        const convert = document.getElementById('knowledge-upload-convert');
        const conversion = (_knowledgeSourceCapabilities || {}).conversion || {};
        if (convert && convert.checked && conversion.available) {
            (data.results || [])
                .filter(result => result.status === 'saved' && result.source_id)
                .forEach(result => requestKnowledgeSourceConversion(result.source_id));
        }
        // A finished batch can change the list (new rows, new tasks to watch),
        // and it can change the versions of the source the detail is showing.
        loadKnowledgeSources({ quiet: true });
        if (_knowledgeSourceSelectedId) openKnowledgeSourceDetail(_knowledgeSourceSelectedId);
    };
    xhr.onerror = () => {
        entries.forEach(entry => {
            if (entry.status === 'uploading') {
                entry.status = 'failed';
                entry.reason = 'knowledge_sources_request_failed';
            }
        });
        _renderKnowledgeUploadQueue();
    };
    entries.forEach(entry => { entry.status = 'uploading'; });
    _renderKnowledgeUploadQueue();
    xhr.send(form);
}

function submitKnowledgeUpload() {
    const entries = _knowledgeUploadQueue.filter(entry => entry.status === 'queued');
    if (!entries.length) {
        _setKnowledgeStatus(t('knowledge_upload_nothing'), true, true);
        return;
    }
    const select = document.getElementById('knowledge-upload-conflict');
    const strategy = (select && select.value) || 'ask';
    // "Upload a new version" pins the version the user is looking at, so a
    // concurrent upload is reported as stale instead of silently replaced.
    const known = _knowledgeUploadTarget ? _knowledgeSourceById(_knowledgeUploadTarget) : null;
    const target = known
        ? { sourceId: known.source_id, version: known.latest_version }
        : null;
    _knowledgeUploadAttempt = { requestId: _knowledgeUploadRequestId(), entries };
    _knowledgeUploadSend(entries, target ? 'update' : strategy, target);
}

function resolveKnowledgeUploadConflict(index, strategy) {
    const entry = _knowledgeUploadQueue[index];
    if (!entry || entry.status !== 'conflict') return;
    const attempt = _knowledgeUploadAttempt
        || (_knowledgeUploadAttempt = { requestId: _knowledgeUploadRequestId(), entries: [entry] });
    if (!attempt.entries.includes(entry)) attempt.entries = attempt.entries.concat([entry]);
    const existing = entry.existing || {};
    entry.status = 'queued';
    entry.reason = '';
    _renderKnowledgeUploadQueue();
    // 'new' creates a distinct source; only 'update' extends the existing one.
    _knowledgeUploadSend([entry], strategy,
        strategy === 'update' ? { sourceId: existing.source_id, version: existing.latest_version } : null);
}

function _setKnowledgePanelHidden(el, hidden) {
    // `hidden` may be absent in a partially assembled page (and in the DOM
    // harness); the panel is optional, the switch is not.
    if (el) el.classList.toggle('hidden', hidden);
}

function _identityMode() {
    return 'database';
}

const _NAVIGATION_MODES = ['classic', 'split'];

function _navigationMode() {
    const raw = String(window.__COW_NAVIGATION_MODE__ || 'classic').trim().toLowerCase();
    return _NAVIGATION_MODES.indexOf(raw) >= 0 ? raw : 'classic';
}

function _navAreaFromPath(pathname) {
    if (window.ConsoleRoutes) return window.ConsoleRoutes.area(String(pathname || '/')) || 'workbench';
    const p = String(pathname || '');
    return p === '/admin' || p.startsWith('/admin/') ? 'admin' : 'workbench';
}

function _openNavArea(area, path) {
    if (window.ConsoleRoutes) {
        const target = path && window.ConsoleRoutes.parse(path);
        return navigateTo(target ? target.view : (area === 'admin' ? 'admin-home' : 'chat'), null,
            target ? { tab: target.tab } : undefined);
    }
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
    // (see the guard inside syncSessionHistorySurface). Because navigation now
    // happens in-place via pushState (no full page load), the normal boot hook
    // that fills the list never runs, so the 会话历史 section would render empty
    // after switching workbench <-> admin. Trigger the fetch here like the
    // full-page-load path does.
    if (typeof syncSessionHistorySurface === 'function') syncSessionHistorySurface();
}

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
        // reach the console through 平台管理.
        if (String(info.scope || '') === 'platform') return false;
        if (allMode) return true;
        return info.available === true || info.read_allowed === true;
    });
}

function _applyNavAreaAttribute() {
    const appEl = document.getElementById('app');
    const area = _navAreaFromPath(typeof location !== 'undefined' ? location.pathname : '');
    if (appEl) appEl.setAttribute('data-nav-area', area);
    document.title = `${productTitle(effectiveBrandName())} ${t(area === 'admin' ? 'nav_admin_console' : 'nav_workbench')}`.trim();
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

function _setupHeaderTenantSelector(self) {
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
    const epoch = _authEpoch;
    (self === undefined ? fetchAccountSelf() : Promise.resolve(self)).then(data => {
        if (epoch !== _authEpoch || tid !== sessionStorage.getItem('cow_tenant_id')) return;
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

async function _ensureTenantSelected(self) {
    if (_identityMode() !== 'database') return true;
    const epoch = _authEpoch;
    const data = self === undefined ? await fetchAccountSelf() : self;
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

function _desktopLoginRecovery() {
    return typeof CowDesktopAccount !== 'undefined' && CowDesktopAccount.isDesktop();
}

function _desktopLoginMode() {
    const recovery = _desktopLoginRecovery();
    _accountHidden('login-form', recovery);
    _accountHidden('login-recovery', !recovery);
    _accountHidden('login-recovery-error', true);
}

function _desktopRequireSignIn(message) {
    _desktopContextClear();
    showLoginScreen();
    _accountText('login-recovery-error', message || t('account_desktop_session_lost'));
    _accountHidden('login-recovery-error', false);
}

async function _submitAccountLogin(event) {
    event.preventDefault();
    if (_accountWritePending || _pendingTenantPicker) return false;
    // A password login is not a desktop recovery (task 2.4). It would create the
    // Web-only session this change exists to remove, so the form is refused and
    // the re-login entry is drawn in its place -- the guard the spec asks for,
    // not a path a user is meant to reach.
    if (_desktopLoginRecovery()) { _desktopRequireSignIn(); return false; }
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

function _desktopAccountMessage(reply) {
    const code = (reply && reply.code) || '';
    if (code === 'host_outdated') return t('account_desktop_upgrade_required');
    if (code === 'no_host') return t('account_desktop_no_host');
    return t('account_logout_unconfirmed');
}

async function desktopRelogin() {
    if (_accountWritePending) return;
    // The entry is desktop-only, and this is the guard that keeps it so: a plain
    // browser has nothing to end natively and must not be sent here.
    if (typeof CowDesktopAccount === 'undefined' || !CowDesktopAccount.isDesktop()) return;
    _accountWritePending = 'logout';
    resetMemoryView();
    ++_authEpoch;   // late answers from the old session must not reopen the form
    _invalidateAccountIdentity('logout_pending');
    _resetHistorySearch();
    const epoch = _authEpoch;
    if (window.CowDesktopHost) window.CowDesktopHost.suspendLocalContext('logout');
    _desktopContextClear();
    _accountHidden('login-recovery-error', true);
    const button = document.getElementById('login-recovery-btn');
    if (button) button.disabled = true;
    try {
        const reply = await CowDesktopAccount.logout({
            webLogout: async () => {
                const response = await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin' });
                const data = await response.json().catch(() => ({}));
                // Already ended counts as ended: this is a recovery, so a session
                // that is gone must not block the native half from finishing.
                const ended = (response.ok && data && data.status === 'success') || response.status === 401;
                return { ok: ended, status: response.status };
            }
        });
        if (epoch !== _authEpoch || reply.ok) return;
        _accountText('login-recovery-error', _desktopAccountMessage(reply));
        _accountHidden('login-recovery-error', false);
    } finally {
        _accountWritePending = null;
        if (button) button.disabled = false;
    }
}

let _accountSelf = null;

let _accountSelfSeq = 0;

let _accountSelfRequest = null;

let _activeAccountPanel = null;

function _db() { return _identityMode() === 'database'; }

function _setAccountPanel(panel) {
    _activeAccountPanel = panel;
    ['account-password-form', 'account-password-status', 'ap-old-password',
     'ap-new-password', 'ap-confirm-password'].forEach(id => {
        const el = document.getElementById(id);
        if (el && panel !== 'password') delete el.dataset.dirty;
    });
}

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
            if (!resp.ok || !data || data.status !== 'success') {
                _accountSelf = null;
                return null;
            }
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

function _baseAccountSelf() {
    return _accountSelf && _accountSelf.status === 'success' ? _accountSelf : null;
}

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

function _baseAuthContext() {
    return _authContext && _authContext.status === 'success' ? _authContext : null;
}

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

function _featureAvailable(key) {
    const api = window.RdaiFunctionalCapabilities;
    if (!api || typeof api.available !== 'function') return false;
    return api.available(_baseAuthContext(), key);
}

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

function _consolePageForView(viewId) {
    const meta = VIEW_META[viewId];
    return meta && meta.console ? meta.console : '';
}

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

function _isPlatformOnlyEntry(item) {
    if (!item) return false;
    if (item.getAttribute('data-view') === 'platform') return true;
    return item.classList.contains('platform-scope-only');
}

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
                // Deep paths keep their address and render the page's denial.
                // Only the historical admin landing retains its old redirect.
                if (!window.ConsoleRoutes || location.pathname.replace(/\/$/, '') === '/admin') {
                    location.replace('/chat');
                    return;
                }
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
                    if (item.classList.contains('hidden')) return;
                    // A platform-only entry is not offered below platform scope
                    // (the platform pass below hides it). Counting it here would
                    // keep an otherwise-empty 平台管理 heading alive: for a tenant
                    // administrator, whose group now holds only 审计日志 / Token
                    // 消耗, and for a member, whose group holds nothing at all.
                    if (!isPlatformAdmin && _isPlatformOnlyEntry(item)) return;
                    reachable = true;
                });
                group.classList.toggle('hidden', !reachable);
            });
    }

    // Per-item: platform-only entries for a platform admin only (see
    // _isPlatformOnlyEntry). Runs after the group pass so it decides the final
    // state of these rows regardless of what the projection reported for them.
    document.querySelectorAll('#sidebar-nav .sidebar-item').forEach(el => {
        if (_isPlatformOnlyEntry(el)) el.classList.toggle('hidden', !isPlatformAdmin);
    });

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

let _accountProfileEditing = false;

let _accountProfileAvatarUploading = false;

function renderAccountProfileAvatar(user) {
    const box = document.getElementById('account-profile-avatar');
    if (!box) return;
    const u = user || {};
    const v = _accountAvatarVersion || u.id || Date.now();
    box.innerHTML = userAvatarHTML({ id: u.id, avatar: u.avatar, self: true, version: v });
}

function handleAccountProfileAvatarActivate(event) {
    const detail = event && typeof event.detail === 'number' ? event.detail : 0;
    if (detail === 1) return;
    openAccountProfileAvatarPicker();
}

function openAccountProfileAvatarPicker() {
    if (_accountProfileAvatarUploading) return;
    const input = document.getElementById('account-profile-avatar-input');
    if (!input) return;
    // Clear first: re-picking the *same* file must still fire `change`.
    input.value = '';
    input.click();
}

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

function cancelAccountProfileEdit() {
    if (_accountProfileAvatarUploading) return;
    resetAccountProfileEditor();
    renderAccountProfile();
}

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

function focusAccountPanel(panelId) {
    const panel = document.getElementById(panelId);
    if (!panel) return;
    const focusable = panel.querySelector('input, select, button, a');
    if (focusable) window.setTimeout(() => focusable.focus(), 0);
}

function accountBeforeUnload(e) {
    if (_forcedPassword) return;  // forced gate may be exited via logout reload
    const dirty = ['ap-old-password', 'ap-new-password', 'ap-confirm-password']
        .some(id => document.getElementById(id)?.dataset.dirty);
    if (dirty || _activeAccountPanel === 'password') {
        e.preventDefault();
        e.returnValue = true;
    }
}

function _resolveOneShotTenantSwitch(self) {
    try {
        const url = new URL(window.location.href);
        const target = url.searchParams.get('switch_tenant');
        const epoch = _authEpoch;
        if (!target || _identityMode() !== 'database') return Promise.resolve(false);
        return (self === undefined ? fetchAccountSelf() : Promise.resolve(self))
            .then(data => {
                if (epoch !== _authEpoch) return false;
                const valid = data && data.status === 'success'
                    && Array.isArray(data.tenants)
                    && data.tenants.some(tn => tn.id === target);
                if (valid) {
                    sessionStorage.setItem('cow_tenant_id', target);
                    // The previous tenant's context must not stay live in the
                    // desktop host (change add-desktop-remote-web-workbench,
                    // task 5.2). A browser has no host and this resolves as a
                    // no-op, so there is no branch here.
                    if (window.CowDesktopHost) window.CowDesktopHost.suspendLocalContext('tenant-switch');
                    // The reference names a binding in the *old* tenant; drop it
                    // so the first turn in the new tenant cannot carry it.
                    _desktopContextClear();
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
