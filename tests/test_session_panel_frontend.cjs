// Presentation-only adapter: same nodes, one request owner, scoped preference.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/fork/views/sessions.js'), 'utf8');
function setup({mobile=false, preference=null}={}) {
    const nodes = new Map(), storage = new Map();
    if(preference !== null) storage.set('user-a:cow_session_panel_open', preference);
    const node = id => {
        if(!nodes.has(id)) {
            const classes = new Set();
            nodes.set(id,{id, children:[], attrs:{}, parentNode:null, classList:{
                add: c=>classes.add(c), remove:c=>classes.delete(c), contains:c=>classes.has(c),
                toggle(c,on){if(on) classes.add(c);else classes.delete(c);}},
                setAttribute(k,v){this.attrs[k]=v},
                appendChild(child){
                    if(child.parentNode) child.parentNode.children=child.parentNode.children.filter(e=>e!==child);
                    this.children.push(child);child.parentNode=this;
                }});
        }
        return nodes.get(id);
    };
    const events={}, calls=[];
    const ctx = vm.createContext({
        _accountAppVisible:true, currentView:'chat', scope:'user-a:', denied:false,
        _historyVisible:false, _historyDirty:false, _sessionItems:[{}], _sessionLoading:false,
        _isMobileView:()=>mobile, _sidebarRecentDenied:()=>ctx.denied,
        _cowUserTenantKey:k=>ctx.scope+k,
        readScopedPreference:k=>storage.get(ctx.scope+k) ?? null,
        writeScopedPreference:(k,v)=>storage.set(ctx.scope+k,v),
        _cancelHistoryRequest:()=>calls.push('cancel'), _closeSessionActionMenu(){},closeNewChatMenus(){},
        closeSidebar(){calls.push('close-sidebar');node('sidebar').classList.add('-translate-x-full');ctx.syncSessionHistorySurface?.();},
        toggleSidebar(){
            calls.push('open-sidebar');
            if(mobile) node('sidebar').classList.remove('-translate-x-full');
            else node('app').classList.remove('sidebar-collapsed');
            ctx.syncSessionHistorySurface?.();
        },
        loadSessionList(){calls.push('load'); ctx._historyDirty=false;},
        navigateTo(view, onArrive){if(!ctx.leaveDenied) {ctx.currentView=view;onArrive?.();}},
        document:{getElementById:node, addEventListener:(key,fn)=>events[key]=fn},
        window:{innerWidth:mobile?390:1280,addEventListener:(key,fn)=>events[key]=fn},
    });
    if(mobile) node('sidebar').classList.add('-translate-x-full');
    node('history-page-new-mount').appendChild(node('history-new-control'));
    vm.runInContext(source,ctx);
    return {ctx,node,calls,storage,events};
}
test('master default is closed and does not read sessions before opening',()=>{
    const h=setup(); assert.equal(h.ctx.sessionHistorySurface(),''); assert.deepEqual(h.calls,[]);
    h.ctx.openSessionPanel();
    assert.equal(h.ctx.sessionHistorySurface(),'panel');
    assert.equal(h.node('session-list').parentNode,h.node('history-panel-list-mount'));
    assert.equal(h.node('history-status').parentNode,h.node('history-panel-list-mount'));
    assert.equal(h.node('history-new-control').parentNode,h.node('history-page-new-mount'));
    assert.equal(h.storage.get('user-a:cow_session_panel_open'),'true');
});
test('page and panel share list and status, leaving the chooser on the full page',()=>{
    const h=setup(); h.ctx.openSessionPanel();const list=h.node('session-list');
    h.ctx.currentView='history'; h.ctx.syncSessionHistorySurface();
    assert.equal(list.parentNode,h.node('history-page-list-mount'));
    assert.equal(h.node('history-status').parentNode,list.parentNode);
    assert.equal(h.node('history-new-control').parentNode,h.node('history-page-new-mount'));
    h.ctx.currentView='agents';h.ctx.syncSessionHistorySurface();
    assert.equal(h.ctx._historyVisible,false);
    h.ctx.currentView='chat';h.ctx.syncSessionHistorySurface();
    assert.equal(list.parentNode,h.node('history-panel-list-mount'));
    assert.deepEqual(h.calls,['load','load','load'], 'moving the list must not abort reusable reads');
});
test('cancelled leave, denied history and hidden account cannot open or fetch',()=>{
    const h=setup();h.ctx.currentView='memory';h.ctx.leaveDenied=true;h.ctx.openSessionPanel();
    assert.equal(h.ctx.currentView,'memory');assert.deepEqual(h.calls,[]);
    h.ctx.leaveDenied=false;h.ctx.denied=true;h.ctx.openSessionPanel();assert.deepEqual(h.calls,[]);
    h.ctx.denied=false;h.ctx._accountAppVisible=false;h.ctx.openSessionPanel();assert.deepEqual(h.calls,[]);
});
test('identity changes cannot inherit another account panel preference',()=>{
    const h=setup({preference:'true'});assert.equal(h.ctx.sessionHistorySurface(),'panel');
    h.ctx.scope='user-b:';h.ctx.syncSessionHistorySurface();assert.equal(h.ctx.sessionHistorySurface(),'');
    assert.equal(h.ctx._historyVisible,false);assert.equal(h.node('session-panel').classList.contains('hidden'),true);
});
test('mobile history uses the main sidebar drawer and selection closes it without losing the preference',()=>{
    const h=setup({mobile:true,preference:'true'});assert.equal(h.ctx.sessionHistorySurface(),'');
    h.ctx.openSessionPanel();assert.ok(h.calls.includes('open-sidebar'));
    assert.equal(h.node('sidebar').classList.contains('-translate-x-full'),false);
    assert.equal(h.node('sidebar-recent').classList.contains('history-expanded'),true);
    h.ctx.finishSessionPanelSelection();assert.equal(h.ctx.sessionHistorySurface(),'');
    assert.equal(h.node('sidebar').classList.contains('-translate-x-full'),true);
    assert.equal(h.storage.get('user-a:cow_session_panel_open'),'true');
    h.ctx.toggleSidebar();assert.equal(h.ctx.sessionHistorySurface(),'panel');
});
test('opening history from the collapsed desktop rail reveals the same sidebar list',()=>{
    const h=setup();h.node('app').classList.add('sidebar-collapsed');
    h.ctx.openSessionPanel();
    assert.equal(h.node('app').classList.contains('sidebar-collapsed'),false);
    assert.equal(h.ctx.sessionHistorySurface(),'panel');
    h.node('app').classList.add('sidebar-collapsed');h.ctx.syncSessionHistorySurface();
    assert.equal(h.ctx.sessionHistorySurface(),'');
    assert.equal(h.storage.get('user-a:cow_session_panel_open'),'true');
});
test('opening after the asynchronous branding leave confirmation preserves the pending action',()=>{
    const h=setup(); const consoleSource=fs.readFileSync(path.join(__dirname,'../channel/web/static/js/console.js'),'utf8');
    const start=consoleSource.indexOf('function _viewLeaveCheck('), end=consoleSource.indexOf('// The account panel',start);
    h.ctx.currentView='branding';h.ctx.brandingDirty=true;
    h.ctx.brandingConfirmDiscard=done=>{h.confirm=done;};
    h.ctx._brandingResetDraftToBaseline=()=>{h.ctx.brandingDirty=false;};
    h.ctx._registeredConsoleView=()=>null;
    vm.runInContext(consoleSource.slice(start,end),h.ctx);
    h.ctx.navigateTo=(view,done)=>{
        if(!h.ctx._viewLeaveCheck(view,done)) return;
        h.ctx.currentView=view;done?.();
    };
    h.ctx.openSessionPanel();
    assert.equal(h.ctx.currentView,'branding');assert.deepEqual(h.calls,[]);
    h.confirm();
    assert.equal(h.ctx.currentView,'chat');assert.equal(h.ctx.sessionHistorySurface(),'panel');
});
