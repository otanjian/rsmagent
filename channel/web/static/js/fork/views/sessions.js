/* Shared master's floating history panel plus rdai's full history page. */
function sessionHistorySurface() { return currentView === 'history' ? 'page' : sessionPanelOpen ? 'panel' : ''; }
function resetSessionPanelIdentity() {
    if (typeof closeReadOnlyHistory === 'function') closeReadOnlyHistory();
    closeSessionPanel(true);
    _historyVisible = false;
}
function syncSessionHistorySurface() {
    if (!_accountAppVisible) return;
    const surface = sessionHistorySurface();
    const mount = document.getElementById(surface === 'page' ? 'history-page-list-mount' : 'history-panel-list-mount');
    if (mount) {
        ['history-status', 'session-list'].forEach(id => {
            const el = document.getElementById(id); if (el) mount.appendChild(el);
        });
    }
    const newControl = document.getElementById('history-new-control');
    const newMount = surface === 'page' ? document.getElementById('history-page-new-mount')
        : document.getElementById('session-panel');
    if (newControl && newMount) {
        if (surface === 'page') newMount.appendChild(newControl);
        else newMount.insertBefore(newControl, document.getElementById('history-panel-list-mount'));
    }
    const wasVisible = _historyVisible;
    _historyVisible = !!surface;
    if (surface === 'page' && typeof loadHistoryReadTargets === 'function') {
        loadHistoryReadTargets();
    }
    if (surface !== 'page' && typeof closeReadOnlyHistory === 'function') {
        closeReadOnlyHistory();
    }
    if (surface && (!wasVisible || _historyDirty || !_sessionItems.length)) loadSessionList(undefined, {revalidate:true});
}
function finishSessionPanelSelection() { if (_isMobileView()) closeSessionPanel(); }
