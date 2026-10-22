const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const {script}=require('./support/console_sources.cjs');
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function setup(allowed=true) {
    let resolve;const pending=new Promise(done=>{resolve=done;});const calls=[],toasts=[];
    const ctx=vm.createContext({
        _authEpoch:1,sessionId:'owned',activeAgentId:'a',_identityMode:()=> 'database',
        _featureAvailable:()=>allowed,document:{getElementById:()=>null},
        chatInput:{disabled:false,classList:{toggle(){}},setAttribute(){},getAttribute:()=>null},
        sendBtn:{disabled:false},_wsToast:message=>toasts.push(message),t:key=>key,
        fetch:(url,options)=>{calls.push({url,options});return pending;},
    });
    vm.runInContext(script('js/chat/context-usage.js'),ctx);
    vm.runInContext('_ctxLastUsage={available:true};',ctx);
    return {ctx,calls,toasts,resolve};
}
test('master context card refuses compaction without the authoritative capability',()=>{
    const h=setup(false);h.ctx._ctxDoCompact();assert.equal(h.calls.length,0);
    assert.match(h.ctx._ctxActionsBar({available:true}),/data-ctx-action="compact" disabled/);
});
test('late compaction completion cannot repaint or unlock a different session',async()=>{
    const h=setup();h.ctx._ctxDoCompact();assert.match(h.calls[0].url,/owned\/compact_context\?agent_id=a/);
    h.ctx.sessionId='other';h.ctx.resetContextUsage();
    h.ctx.chatInput.disabled=true;
    h.resolve({json:async()=>({status:'success',ok:false})});await settle();await settle();
    assert.equal(h.ctx.chatInput.disabled,true);assert.deepEqual(h.toasts,[]);
});
test('reset invalidates a pending operation even when returning to the same session',async()=>{
    const h=setup();h.ctx._ctxDoCompact();h.ctx.resetContextUsage();
    h.resolve({json:async()=>({status:'error'})});await settle();await settle();
    assert.deepEqual(h.toasts,[]);
});
test('restored context renders its snapshot while live-only compaction stays disabled',()=>{
    const h=setup();const card=h.ctx._ctxRenderCard({available:false,restored:true,limit:100,used:30,breakdown:{history:30,free:70}});
    assert.match(card,/ctx_restored/);assert.match(card,/30%/);
    assert.match(h.ctx._ctxActionsBar({available:false,restored:true}),/data-ctx-action="compact" disabled/);
});
