/* Address-bar routing: mirrors the current view and tab into the URL path, so
   a reload lands where the user left off and Back/Forward move between views.
   These are classic scripts sharing one global scope; see
   channel/web/README.md before changing the load order. */

// =====================================================================
// Routing
// =====================================================================
// A route names a view and, for the views that have tabs, one of its tabs:
// /settings, /settings/models. Only a tab that is not the view's default gets
// a segment, so the path carries what distinguishes it and nothing else.
// Deeper state -- which session is open, which file the editor holds --
// deliberately stays out. It is already restored from localStorage, and
// putting it in the URL would rewrite the address bar on every click in the
// session list.
//
// The view a route names is not always the view's internal id. /settings is
// the config view, because /config is the backend's config API, which this
// console and the desktop client both call. /scheduler is the tasks view: it
// is what the backend calls this feature already (/api/scheduler/...), and it
// leaves /tasks free for the separate notion of a task. web_channel.py serves
// these paths from the same shell; the table there has to stay in step.
const ROUTE_PATHS = {
    chat:      '',
    artifacts: 'artifacts',
    agents:    'agents',
    config:    'settings',
    skills:    'skills',
    memory:    'memory',
    knowledge: 'knowledge',
    channels:  'channels',
    tasks:     'scheduler',
    logs:      'logs',
};

// An artifact or link the user put in the menu is at /m/<item id>, served by
// its own pattern in web_channel.py. The id travels as the view's "tab".
const ROUTE_MENU_SEGMENT = 'm';

const consolePages = JSON.parse(document.getElementById('console-page-routes')?.textContent || '[]');
for (const page of consolePages) ROUTE_PATHS[page.view] = page.path.replace(/^\//, '');
const ROUTE_VIEWS = {};
for (const view in ROUTE_PATHS) ROUTE_VIEWS[ROUTE_PATHS[view]] = view;

const ROUTE_TABS = {
    config:    ['basic', 'models'],
    memory:    ['files', 'dreams'],
    tasks:     ['tasks', 'records'],
    knowledge: ['docs', 'graph'],
};

// The tab a view opens on when the route does not name one. It is left out of
// the path: /tasks, not /tasks/tasks. A path segment is there to say which of
// several tabs is showing, so the one you get by default has nothing to say.
const ROUTE_DEFAULT_TABS = {
    config:    'basic',
    memory:    'files',
    tasks:     'tasks',
    knowledge: 'docs',
};

// URL segment for a tab whose element id reads as an internal name. The path
// should say what the UI says, and the "dreams" tab is labelled Self-Evolution
// everywhere the user can see it. Only the segment differs -- the element id,
// the switcher argument and ROUTE_TABS above all stay as they are.
const ROUTE_TAB_PATHS = {
    memory: { dreams: 'evolution' },
};

for (const page of consolePages) {
    ROUTE_TABS[page.view] = page.tabs;
    ROUTE_DEFAULT_TABS[page.view] = page.tabs[0] || '';
    ROUTE_TAB_PATHS[page.view] = page.tab_aliases;
}
function _tabPath(view, tab) {
    return (ROUTE_TAB_PATHS[view] || {})[tab] || tab;
}

function _tabId(view, segment) {
    const aliases = ROUTE_TAB_PATHS[view] || {};
    for (const tab in aliases) if (aliases[tab] === segment) return tab;
    // Unaliased segments are the tab id itself. An alias's own id also lands
    // here, so a hand-written /memory/dreams still opens the tab and is
    // rewritten to /memory/evolution by the settle at the end of routeApply.
    return segment;
}

// The tab showing in the current view, '' for a view that has none. Tracked so
// a navigation the unsaved-edit guard refuses can put the address bar back
// exactly where it was.
let routeTab = '';

// Set while a route is being applied to the page. The handlers that normally
// write the address bar then leave it alone: it already says what is being
// applied, and writing would stack a duplicate entry onto the history.
let _routeApplying = false;

function _routePath(view, tab) {
    if (view === 'custom') return '/' + ROUTE_MENU_SEGMENT + '/' + encodeURIComponent(tab || '');
    const path = ROUTE_PATHS[view] || '';
    if (!path || !tab || tab === ROUTE_DEFAULT_TABS[view]) return '/' + path;
    return '/' + path + '/' + _tabPath(view, tab);
}

function _routeParse(pathname) {
    const parts = String(pathname || '/').replace(/^\/+|\/+$/g, '').split('/');
    if (parts[0] === ROUTE_MENU_SEGMENT && parts[1]) {
        let id = parts[1];
        try { id = decodeURIComponent(id); } catch (_) { /* keep it as typed */ }
        return { view: 'custom', tab: id };
    }
    const clean = String(pathname || '/').replace(/^\/+|\/+$/g, '');
    const routePath = Object.keys(ROUTE_VIEWS).sort((a, b) => b.length - a.length)
        .find(path => clean === path || (path && clean.startsWith(path + '/')));
    const view = ROUTE_VIEWS[routePath];
    if (!view) return { view: 'chat', tab: '' };
    const allowed = ROUTE_TABS[view] || [];
    const tab = _tabId(view, clean.slice(routePath.length).replace(/^\//, ''));
    return { view, tab: allowed.includes(tab) ? tab : '' };
}

function _routeWrite(view, tab, replace) {
    const path = _routePath(view, tab);
    if (location.pathname === path && !location.hash && !location.search) return;
    // pushState/replaceState do not fire popstate, so writing the address bar
    // cannot loop back in as a navigation.
    history[replace ? 'replaceState' : 'pushState'](null, '', path);
}

function _routeApplyTab(view, tab) {
    if (view === 'config') switchConfigTab(tab);
    else if (view === 'memory') switchMemoryTab(tab);
    else if (view === 'tasks') switchTasksTab(tab);
    else if (view === 'knowledge') switchKnowledgeTab(tab);
    else if (view === 'custom') navigateTo('custom', tab);
}

// Called by navigateTo() once it has committed to a view: the history gains an
// entry, so Back returns to where the user came from.
function routeEnterView(view, tab) {
    if (!VIEW_META[view]) return;
    // The custom view is one view for every entry the user added, so which
    // one it shows is part of the destination, not a refinement of it.
    routeTab = tab || '';
    if (_routeApplying) return;
    // Re-entering the view already on screen -- clicking its sidebar item
    // again -- is not a new destination. Refine the entry instead of stacking
    // another, or Back would have to undo a run of no-op navigations before it
    // appeared to do anything.
    const here = _routeParse(location.pathname);
    const reentry = here.view === view && (view !== 'custom' || here.tab === routeTab);
    _routeWrite(view, routeTab, reentry);
}

// Called by the tab switchers. A tab is a refinement of the view already on
// screen rather than a new destination, so it replaces the entry navigateTo
// just pushed instead of stacking another one -- Back then leaves the view,
// instead of stepping back through every tab visited inside it.
function routeNoteTab(view, tab) {
    if (view !== currentView) return;
    routeTab = tab;
    if (_routeApplying) return;
    _routeWrite(view, tab, true);
}

// Apply what the address bar says: on Back/Forward, and once at startup.
function routeApply() {
    if (typeof _accountAppVisible !== 'undefined' && !_accountAppVisible) return false;
    let parsed = _routeParse(location.pathname);
    if (['/', '/chat', '/admin'].includes(location.pathname) && location.hash) {
        let id = location.hash.replace(/^#view-/, '').split(/[?\/]/)[0];
        const old = /^#\/(workbench|admin)\/([^?]+)/.exec(location.hash);
        if (old) id = old[1] === 'workbench' && old[2] === 'agents' ? 'agent-workbench' : old[2];
        id = legacyPersonalForward(id) || _normalizeViewId(id);
        if (VIEW_META[id]) {
            const requested = new URLSearchParams(location.hash.split('?')[1] || '').get('tab');
            parsed = {view: id, tab: (ROUTE_TABS[id] || []).includes(requested) ? requested : ''};
        }
    }

    if (parsed.view === currentView) {
        // No tab segment names the view's default tab, rather than meaning
        // "leave the tab alone". Back out of /tasks/records lands on /tasks,
        // which has to put the tasks tab up again -- otherwise the page would
        // keep showing records and the settle below would undo the Back.
        const wanted = parsed.tab || ROUTE_DEFAULT_TABS[parsed.view] || '';
        if (wanted && wanted !== routeTab) {
            _routeApplying = true;
            try { _routeApplyTab(parsed.view, wanted); }
            finally { _routeApplying = false; }
        }
    } else {
        _routeApplying = true;
        try { navigateTo(parsed.view, parsed.tab); }
        finally { _routeApplying = false; }
    }

    // Settle the address bar on whatever ended up on screen. Usually it
    // already says that and this writes nothing. It earns its keep in the two
    // cases where the path and the page disagree: a path the router does not
    // write itself (/settings/bogus lands on the default tab, /memory/dreams
    // normalises to /memory/evolution) is rewritten to its canonical form; and
    // a navigation the unsaved-edit guard refused leaves the page where it
    // was, so the address bar -- which Back has already moved -- goes back too.
    _routeWrite(currentView, routeTab, true);
}

window.addEventListener('popstate', routeApply);

// The fork's business pages share this router; no second history implementation.
window.ConsoleRoutes = {
    pages: consolePages,
    parse: path => {
        const route = _routeParse(path);
        const page = consolePages.find(page => page.view === route.view);
        const clean = String(path).replace(/\/$/, '') || '/';
        return page && (clean === page.path || (page.path !== '/' && clean.startsWith(page.path + '/') && route.tab)) ? route : null;
    },
    pathFor: _routePath,
    note: routeNoteTab, apply: routeApply,
    area: path => { const route = window.ConsoleRoutes.parse(path); return route ? consolePages.find(page => page.view === route.view)?.area || null : null; },
};
window.addEventListener('hashchange', routeApply);
window.addEventListener('account-invalidated', () => { routeTab = ''; _routeApplying = false; });
