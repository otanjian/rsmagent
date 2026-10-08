// Adapted from upstream core/router.js. Paths come from the server registry;
// navigation, permissions and leave guards remain owned by console.js.
(function () {
    'use strict';
    const node = document.getElementById('console-page-routes');
    const pages = JSON.parse(node ? node.textContent : '[]');
    const byView = new Map(pages.map(page => [page.view, page]));
    let sequence = 0, applying = false, ready = false, active = null;

    function tabId(page, value) {
        const alias = Object.keys(page.tab_aliases).find(key => page.tab_aliases[key] === value);
        const tab = alias || value || page.tabs[0] || '';
        return !tab || page.tabs.includes(tab) ? tab : null;
    }
    function pathFor(view, tab) {
        const page = byView.get(view);
        if (!page) return null;
        tab = tabId(page, tab);
        if (tab === null) return null;
        return page.path + (tab && tab !== page.tabs[0] ? '/' + (page.tab_aliases[tab] || tab) : '');
    }
    function parse(pathname, hash = '') {
        if (!pathname.startsWith('/') || pathname.startsWith('//')) return null;
        const path = pathname === '/' ? '/' : pathname.replace(/\/$/, '');
        if (hash && ['/', '/chat', '/admin'].includes(path)) {
            let view, tab, area;
            if (hash.startsWith('#view-')) {
                const [id, query = ''] = hash.slice(6).split('?');
                view = id; tab = new URLSearchParams(query).get('tab');
            } else {
                const match = /^#\/(workbench|admin)\/([^/?]+)(?:\?([^#]*))?$/.exec(hash);
                if (!match) return null;
                area = match[1]; view = match[2]; tab = new URLSearchParams(match[3] || '').get('tab');
                if (area === 'workbench' && view === 'agents') view = 'agent-workbench';
                const aliases = { models: 'config', schedules: 'tasks', todos: 'todo', organization: 'org', tenants: 'tenant' };
                view = aliases[view] || view;
            }
            if (typeof legacyPersonalForward === 'function') view = legacyPersonalForward(view) || view;
            if (view === 'scenarios') view = 'scenes';
            if (view === 'external-connections') view = 'external_connections';
            const page = byView.get(view);
            if (!page || (area && page.area !== area)) return null;
            tab = tabId(page, tab);
            return tab === null ? null : { view, tab };
        }
        if (path === '/chat') return { view: 'chat', tab: '' };
        for (const page of pages) {
            if (path === page.path) return { view: page.view, tab: page.tabs[0] || '' };
            if (page.path !== '/' && path.startsWith(page.path + '/')) {
                const tab = tabId(page, path.slice(page.path.length + 1));
                if (tab !== null) return { view: page.view, tab };
            }
        }
        return null;
    }
    function write(replace) {
        if (!active) return;
        const path = pathFor(active.view, active.tab);
        if (!path) return;
        if (location.pathname + location.search + location.hash !== path) {
            history[replace ? 'replaceState' : 'pushState']({ cowView: active.view }, '', path);
        }
    }
    function prepare(options) {
        if (options && options.id) return options.id === sequence ? options : null;
        return { ...(options || {}), id: ++sequence };
    }
    function enter(view, options) {
        const page = byView.get(view);
        if (!page) return;
        const same = active && active.view === view;
        const tab = options && options.tab !== undefined ? tabId(page, options.tab)
            : (same ? active.tab : page.tabs[0] || '');
        active = { view, tab: tab || '' };
        applying = true;
        write(!!(options && options.replace) || !!same);
    }
    function finish(view, options) {
        if (!active || active.view !== view || (options && options.id !== sequence)) return;
        try {
            const tab = active.tab;
            if (view === 'config') {
                if (!_modelsManageAllowed() && tab !== 'catalog') active.tab = 'catalog';
                switchConfigTab(active.tab);
            } else if (view === 'memory') switchMemoryTab(tab);
            else if (view === 'tasks') switchTasksTab(tab);
            else if (view === 'knowledge') switchKnowledgeTab(tab);
        } finally {
            applying = false;
            write(true);
        }
    }
    function note(view, tab) {
        if (applying || !active || active.view !== view || view !== currentView) return;
        const page = byView.get(view);
        const selected = page && tabId(page, tab);
        if (selected === null || selected === undefined) return;
        active.tab = selected;
        write(true);
    }
    function apply() {
        if (typeof _accountAppVisible !== 'undefined' && !_accountAppVisible) return false;
        ready = true;
        const desired = parse(location.pathname, location.hash);
        if (!desired) return false;
        // History moves before the guard runs. Restore the committed address
        // until the approved continuation (which retains its tab) commits.
        const previous = active;
        if (previous) write(true);
        const result = navigateTo(desired.view, null, { tab: desired.tab, replace: true });
        if (result === false && previous) write(true);
        return result;
    }
    window.ConsoleRoutes = {
        pages, parse, pathFor, prepare, enter, finish, note, apply,
        area: path => { const route = parse(path); return route ? byView.get(route.view).area : null; },
        cancel: () => { applying = false; },
    };
    window.routeNoteTab = note;
    window.addEventListener('account-invalidated', () => { sequence++; active = null; ready = false; applying = false; });
    window.addEventListener('popstate', () => { if (ready) apply(); });
    window.addEventListener('hashchange', () => { if (ready) apply(); });
})();
