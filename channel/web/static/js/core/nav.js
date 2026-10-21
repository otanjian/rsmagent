/* Sidebar routing: navigateTo plus the per-view lazy loading hook.
   Split out of console.js. These are classic scripts sharing one global
   scope; see channel/web/README.md before changing the load order. */

// =====================================================================
// Sidebar & Navigation
// =====================================================================
// `nav` names the sidebar entry to light when the view has none of its own.
const VIEW_META = {
    "chat": {
        "group": "nav_chat",
        "page": "menu_chat",
        "console": "workbench.chat"
    },
    "artifacts": {
        "group": "nav_chat",
        "page": "menu_artifacts",
        "nav": "chat",
        "console": "workbench.artifacts"
    },
    "agents": {
        "group": "nav_manage",
        "page": "menu_agent_config",
        "console": "admin.agents"
    },
    "config": {
        "group": "nav_manage",
        "page": "menu_config",
        "console": "admin.models"
    },
    "skills": {
        "group": "nav_manage",
        "page": "menu_skills",
        "console": "admin.skills"
    },
    "memory": {
        "group": "nav_manage",
        "page": "menu_memory",
        "console": "admin.memory"
    },
    "knowledge": {
        "group": "nav_manage",
        "page": "menu_knowledge",
        "console": "workbench.knowledge"
    },
    "channels": {
        "group": "nav_manage",
        "page": "menu_channels",
        "console": "admin.channels"
    },
    "tasks": {
        "group": "nav_manage",
        "page": "menu_tasks",
        "console": "workbench.schedules"
    },
    "logs": {
        "group": "nav_monitor",
        "page": "menu_logs",
        "console": "admin.logs"
    },
    "custom": {
        "group": "nav_chat",
        "page": "menu_chat"
    },
    "history": {
        "group": "nav_workbench",
        "page": "session_history",
        "console": "workbench.history"
    },
    "agent-workbench": {
        "group": "nav_workbench",
        "page": "menu_agents",
        "console": "workbench.agents"
    },
    "todo": {
        "group": "nav_workbench",
        "page": "menu_todo",
        "console": "workbench.todos"
    },
    "scenes": {
        "group": "nav_workbench",
        "page": "menu_scenes",
        "console": "workbench.scenes"
    },
    "external_connections": {
        "group": "nav_group_model_access",
        "page": "menu_external_connections",
        "console": "admin.external_connections"
    },
    "system_user": {
        "group": "nav_group_org_perm",
        "page": "menu_system_user",
        "console": "admin.roles"
    },
    "roles": {
        "group": "nav_group_org_perm",
        "page": "menu_roles",
        "console": "admin.roles"
    },
    "org": {
        "group": "nav_group_org_perm",
        "page": "menu_org",
        "console": "admin.organization"
    },
    "tenant": {
        "group": "nav_group_platform_ops",
        "page": "menu_tenant",
        "console": "admin.tenants"
    },
    "platform": {
        "group": "nav_group_platform_ops",
        "page": "menu_platform",
        "console": "admin.tenants"
    },
    "branding": {
        "group": "nav_group_platform_ops",
        "page": "menu_branding",
        "console": "admin.branding"
    },
    "audit": {
        "group": "nav_group_platform_ops",
        "page": "menu_audit",
        "console": "admin.audit"
    },
    "token_usage": {
        "group": "nav_group_platform_ops",
        "page": "menu_token_usage",
        "console": "admin.token_usage"
    },
    "admin-home": {
        "group": "nav_admin_console",
        "page": "nav_admin_overview",
        "console": null
    }
};

let currentView = 'chat';

// The view switch itself. Callers want navigateTo() below, which wraps this
// with the unsaved-edit guard and the per-view lazy loading.
function _switchToView(viewId) {
    if (!VIEW_META[viewId]) return;
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    const target = document.getElementById('view-' + viewId);
    if (target) target.classList.add('active');
    const meta = VIEW_META[viewId];
    const artifactsBtn = document.getElementById('artifacts-toggle-btn');
    if (artifactsBtn) {
        artifactsBtn.classList.toggle('hidden', viewId !== 'chat' && viewId !== 'artifacts');
        artifactsBtn.classList.toggle('is-active', viewId === 'artifacts');
    }
    document.getElementById('breadcrumb-group').textContent = t(meta.group);
    document.getElementById('breadcrumb-group').dataset.i18n = meta.group;
    document.getElementById('breadcrumb-page').textContent = t(meta.page);
    document.getElementById('breadcrumb-page').dataset.i18n = meta.page;
    const breadcrumb = document.getElementById('header-breadcrumb');
    if (breadcrumb) breadcrumb.style.display = viewId === 'chat' ? 'none' : '';
    const leavingAgents = currentView === 'agents' && viewId !== 'agents';
    currentView = viewId;
    menuRenderSidebar();
    // The Agent detail is a fixed drawer, so it would otherwise hang over
    // whatever view you navigate to. It only belongs to the Agent Team page.
    if (viewId !== 'agents') closeAgentDetail();
    if (viewId === 'agents') {
        // The team page is a wide two-pane workbench; the history panel on top
        // of it would leave the detail cramped. Tuck it away on entry and put it
        // back the way it was when the user leaves (only if they hadn't already
        // toggled it themselves in the meantime).
        _sessionPanelWasOpen = sessionPanelOpen;
        if (sessionPanelOpen) closeSessionPanel(true);
        loadAgentCatalog();
    } else if (leavingAgents && _sessionPanelWasOpen) {
        _sessionPanelWasOpen = false;
        openSessionPanel();
    }
    
    // Clear status messages when navigating away
    document.querySelectorAll('[id$="-status"]').forEach(el => {
        el.classList.add('opacity-0');
    });
    
    if (window.innerWidth < 1024) closeSidebar();
}

function toggleSidebar() {
    if (window.innerWidth >= 1024) {
        closeAccountMenu();
        const collapsed = document.getElementById('app').classList.toggle('sidebar-collapsed');
        document.getElementById('menu-toggle')?.setAttribute('aria-expanded', String(!collapsed));
        if (typeof syncSessionHistorySurface === 'function') syncSessionHistorySurface();
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
        if (typeof syncSessionHistorySurface === 'function') syncSessionHistorySurface();
    }
}

function closeSidebar() {
    closeAccountMenu();
    document.getElementById('sidebar').classList.add('-translate-x-full');
    document.getElementById('sidebar-overlay').classList.add('hidden');
    if (window.innerWidth < 1024) document.getElementById('menu-toggle')?.setAttribute('aria-expanded', 'false');
    if (typeof syncSessionHistorySurface === 'function') syncSessionHistorySurface();
}

// Group open/closed state and the collapsed rail are remembered per browser.
// The first paint already honours both: the <head> script sets
// html.sidebar-collapsed and sidebar.html closes the stored groups inline.
const SIDEBAR_COLLAPSED_KEY = 'cow_sidebar_collapsed';
const SIDEBAR_GROUPS_KEY = 'cow_sidebar_groups';

function _saveSidebarGroups() {
    // Merged into what is stored: a group the menu does not draw right now
    // keeps the state it had.
    let state = {};
    try { state = JSON.parse(localStorage.getItem(SIDEBAR_GROUPS_KEY) || '{}'); } catch (_) { /* start over */ }
    document.querySelectorAll('#sidebar .menu-group[data-group]').forEach(g => {
        state[g.dataset.group] = g.classList.contains('open');
    });
    try { localStorage.setItem(SIDEBAR_GROUPS_KEY, JSON.stringify(state)); } catch (_) { /* private mode */ }
}

// Delegated: menu.js redraws the entries whenever the menu changes.
document.getElementById('sidebar-nav').addEventListener('click', e => {
    const header = e.target.closest('.menu-group-head');
    if (header) {
        header.parentElement.classList.toggle('open');
        _saveSidebarGroups();
        return;
    }
    const item = e.target.closest('.sidebar-item');
    if (!item) return;
    if (item.dataset.menuId) menuOpenItem(item.dataset.menuId);
    else navigateTo(item.dataset.view);
});

function isSidebarCollapsed() {
    return document.documentElement.classList.contains('sidebar-collapsed');
}

// The rail hides every label, so each entry carries its name as a hover tip
// instead. Re-run on language switch (applyI18n) and on every toggle.
function syncSidebarTips() {
    const collapsed = isSidebarCollapsed();
    document.querySelectorAll('#sidebar .sidebar-item').forEach(item => {
        const label = item.querySelector(':scope > span');
        if (collapsed && label) {
            item.setAttribute('data-tooltip', label.textContent.trim());
            item.setAttribute('data-tooltip-pos', 'right');
            item.setAttribute('data-tip-float', '');
        } else {
            item.removeAttribute('data-tooltip');
            item.removeAttribute('data-tooltip-pos');
            item.removeAttribute('data-tip-float');
        }
    });
    const toggle = document.getElementById('sidebar-collapse-btn');
    if (toggle) {
        toggle.setAttribute('data-tooltip', t(collapsed ? 'sidebar_expand' : 'sidebar_collapse'));
        toggle.setAttribute('data-tooltip-pos', collapsed ? 'right' : 'top');
        toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    }
}

function toggleSidebarCollapsed() {
    const collapsed = !isSidebarCollapsed();
    document.documentElement.classList.toggle('sidebar-collapsed', collapsed);
    try { localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? '1' : '0'); } catch (_) { /* private mode */ }
    if (typeof closeUpdateMenu === 'function') closeUpdateMenu();
    syncSidebarTips();
}

syncSidebarTips();

// The logo goes home, same as the sidebar items do: through navigateTo, so the
// address bar, the unsaved-edit guard and the mobile drawer all behave as they
// do for any other destination. It is not a .sidebar-item because it must not
// pick up the active highlight the chat entry already carries.
const sidebarHome = document.getElementById('sidebar-home');
if (sidebarHome) {
    sidebarHome.addEventListener('click', () => navigateTo('chat'));
    // role="button" without this is a lie to anyone not using a mouse.
    sidebarHome.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            navigateTo('chat');
        }
    });
}

window.addEventListener('resize', () => {
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
// View Navigation
// =====================================================================
// Everything routes through here: the sidebar, the breadcrumb and the
// navigateTo() calls in generated onclick handlers.
// `tab` is optional and comes from the address bar; a caller that does not
// name one gets the view's usual landing tab. Returns false when the guard
// below refused to leave the current view, which is what lets the router put
// the address bar back after a Back it could not honour.
let navigationSequence = 0;
window.addEventListener('account-invalidated', () => { navigationSequence++; });
function navigateTo(viewId, tab, routeOptions) {
    if (routeOptions?.sequence && routeOptions.sequence !== navigationSequence) return false;
    routeOptions = routeOptions || { sequence: ++navigationSequence };
    if (!routeOptions.sequence) routeOptions = { ...routeOptions, sequence: ++navigationSequence };
    const onArrive = typeof tab === 'function' ? tab : null;
    if (onArrive) tab = routeOptions.tab || '';
    else if (routeOptions.tab !== undefined) tab = routeOptions.tab;
    viewId = _normalizeViewId(viewId === 'scenarios' ? 'scenes' : viewId);
    viewId = legacyPersonalForward(viewId) || viewId;
    if (!VIEW_META[viewId]) return false;
    if (!_viewLeaveCheck(viewId, onArrive || tab, routeOptions)) return false;
    const denied = _viewNavDenied(viewId);
    if (denied) {
        routeEnterView(viewId, tab);
        showUnavailableView(viewId, denied.reason);
        _applyNavAreaAttribute();
        menuRenderSidebar();
        return false;
    }
    if (viewId !== currentView) { agentNavigationVersion++; cancelAgentStart(); }

    // An open document editor is about to be replaced by another view, which
    // would drop the edit with nothing on screen to say so.
    if (!docGuardUnsaved(() => navigateTo(viewId, onArrive || tab, routeOptions))) return false;

    // Stop log stream when leaving logs view
    if (currentView === 'logs' && viewId !== 'logs') stopLogStream();

    _switchToView(viewId);
    // The address bar follows the view, so a reload lands back here.
    routeEnterView(viewId, tab);
    _applyNavAreaAttribute();

    // Lazy-load view data
    if (viewId === 'config') { loadConfigView(); switchConfigTab(tab || 'basic'); }
    else if (viewId === 'skills') { resetSkillViewer(); loadSkillsView(); }
    else if (viewId === 'memory') {
        memoryEditor.forget();
        document.getElementById('memory-panel-viewer').classList.add('hidden');
        document.getElementById('memory-panel-list').classList.remove('hidden');
        // Memory ownership comes from the signed-in account, never an Agent fallback.
        switchMemoryTab(tab || 'files');
    }
    // loadKnowledgeView lands on the docs tab itself, so unlike the views
    // above there is no default to pass -- only a route-named tab to override
    // it with.
    else if (viewId === 'knowledge') {
        withManagementCatalog(viewId, () => { loadKnowledgeView(); if (tab) switchKnowledgeTab(tab); });
    }
    else if (viewId === 'channels') loadChannelsView();
    else if (viewId === 'tasks') { switchTasksTab(tab || 'tasks'); loadTasksView(); }
    else if (viewId === 'logs') startLogStream();
    else if (viewId === 'artifacts') loadArtifactsView();
    else if (viewId === 'custom') menuShowPage(tab);
    else if (viewId === 'branding') initBrandingView();
    else if (viewId === 'admin-home') initAdminHomeView();
    else if (viewId === 'agent-workbench') loadAgentWorkbench();
    else if (viewId === 'scenes' && typeof window.loadScenesView === 'function') window.loadScenesView();
    _loadRegisteredView(viewId);
    syncSessionHistorySurface();
    if (onArrive) onArrive();
    return true;
}

