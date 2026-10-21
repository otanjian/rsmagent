/* Bind rdai lifecycle after shared master modules are initialized. */
const _nativeFetch = window.fetch.bind(window);

window.fetch = function(input, init) {
    init = init ? { ...init } : {};
    let url = typeof input === 'string' ? input : (input instanceof URL ? input.href : input.url);
    const requestUrl = new URL(url, window.location.href);
    const sameOrigin = requestUrl.origin === window.location.origin;
    if (sameOrigin) url = requestUrl.pathname + requestUrl.search + requestUrl.hash;
    const accountMemory = sameOrigin && /^\/api\/memory(?:\/|$)/.test(requestUrl.pathname);
    const instanceUpdate = sameOrigin && /^\/api\/update\//.test(requestUrl.pathname);
    // A scan registration is not an agent-scoped call: it either creates a new
    // application (tenant/platform scan) or names its target explicitly. The
    // appended active agent turned "shared scan" into "personal target" and the
    // request was refused before any QR appeared.
    const scanOnboarding = sameOrigin
        && /^\/api\/feishu\/register(?:\/|$)/.test(requestUrl.pathname);
    // In database identity mode the request context is tenant-scoped. The
    // selected tenant lives in sessionStorage (cow_tenant_id) but the core
    // console requests (agents / sessions / history / knowledge) do not
    // otherwise carry it, so the backend rejects them with a 400
    // "tenant selection required". Inject the header for same-origin requests
    // here, mirroring identity-admin.js / todos.js apiFetch. This is
    // a no-op in legacy mode (no tenant is ever stored).
    const tenantId = tenantSelectionHeader(url);
    if (tenantId) {
        const headers = init.headers instanceof Headers
            ? new Headers(init.headers)
            : new Headers(init.headers || (input instanceof Request ? input.headers : {}));
        if (!headers.has('X-Tenant-ID')) headers.set('X-Tenant-ID', tenantId);
        init.headers = headers;
    }
    if (!accountMemory && !instanceUpdate && !scanOnboarding && activeAgentId
            && sameOrigin && url.startsWith('/')) {
        if (!/[?&]agent_id=/.test(url)) {
            const joiner = url.includes('?') ? '&' : '?';
            url = `${url}${joiner}agent_id=${encodeURIComponent(activeAgentId)}`;
        }
        if (input instanceof Request) input = new Request(new URL(url, window.location.href), input);
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
    // A turn sent while a local directory is the committed selection carries the
    // non-secret reference for it, so the server reads *that* directory instead
    // of the server workspace (task 2.3). Attached at the one transport seam
    // every /message caller already goes through -- send, regenerate and steer
    // -- and outside the agent guard, because a single-Agent install has no
    // `activeAgentId` and still needs its local directory read.
    if (/^\/message\b/.test(url) && typeof init.body === 'string') {
        const context = _desktopContextForRequest();
        if (context) {
            try {
                const body = JSON.parse(init.body);
                if (body && typeof body === 'object' && !Array.isArray(body)) {
                    body.desktop_context = context;
                    init.body = JSON.stringify(body);
                }
            } catch (_) {}
        }
    }
    return _nativeFetch(input, init);
};

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

window.I18N = I18N;

window.__cowLang__ = currentLang;

window.CowAppearance.subscribe(state => {
    currentTheme = state.resolved;
    const light = document.getElementById('hljs-light');
    const dark = document.getElementById('hljs-dark');
    if (light) light.disabled = state.resolved === 'dark';
    if (dark) dark.disabled = state.resolved !== 'dark';
    renderAppearancePreferences();
});

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

window.registerConsoleView = registerConsoleView;

fetchPublicBrand();

document.addEventListener('DOMContentLoaded', applyBrandToDocument);

document.addEventListener('visibilitychange', () => {
    if (!document.hidden) fetchPublicBrand();
});

NEW_CHAT_SURFACES = {
    panel: {
        // The full history page's button (legacy surface key: panel). It is *not* ``new-chat-btn``: that id
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

document.addEventListener('DOMContentLoaded', function() {
    // Bind the branding page controls once. The page is populated lazily on
    // first entry via navigateTo -> initBrandingView.
    if (brandingEl('view-branding')) _brandingBindEvents();
});

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
    if (!memoryEditor.isDirty() && !skillEditor.isDirty() && !knowledgeEditor.isDirty() && !brandingDirty) return;
    e.preventDefault();
    e.returnValue = '';
});

window.desktopRelogin = desktopRelogin;

window.addEventListener('beforeunload', accountBeforeUnload);

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
