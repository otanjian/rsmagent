const {test} = require('node:test');
const assert = require('node:assert/strict');
const {create} = require('../channel/web/static/js/fork/scheduler-notifications.js');
const who = {server:'http://fixture', user:'alice', tenant:'t1', epoch:1};
const run = id => ({run_id:id, status:'done', channel_type:'web', session_id:'other', agent_id:'b'});
function setup(overrides = {}) {
    const values = new Map(), seen = [], timers = new Map();
    let seq = 0, valid = true;
    const observer = create({
        storage: {read: async key => values.get(key), advance: async (key, n) => values.set(key, n),
            claim: async (key, r) => {const id = key + r.run_id; if(values.has(id)) return false; values.set(id, true); return true;}},
        current: () => valid, setTimer: fn => {timers.set(++seq, fn); return seq;}, clearTimer: id => timers.delete(id),
        fetchPage: async () => ({status:'success', runs:[], cursor:0, has_more:false}), notify: async r => seen.push(r.run_id),
        ...overrides,
    });
    return {observer, values, seen, timers, invalidate:()=>{valid=false;}};
}
test('notification polling stops across identities', async () => {
    let release, started, signal;
    const called = new Promise(resolve => started = resolve);
    const env = setup({fetchPage: (_cursor, s) => {signal=s; started(); return new Promise(resolve=>release=resolve);}});
    const pending = env.observer.start(who); await called;
    env.observer.stop(); env.invalidate();
    assert.equal(signal.aborted, true);
    release({status:'success', runs:[run('old')], cursor:1}); await pending;
    assert.deepEqual(env.seen, []); assert.equal(env.timers.size, 0);
});
test('terminal cursor pages are fully consumed', async () => {
    const asked=[];
    const env=setup({fetchPage: async cursor => {
        asked.push(cursor);
        if(cursor === undefined) return {status:'success', runs:[], cursor:100, has_more:false};
        return {status:'success', runs:[run('run-'+(cursor+1))], cursor:cursor+1, has_more:cursor < 103};
    }});
    await env.observer.start(who); assert.deepEqual(env.seen, []);
    const next=[...env.timers.values()][0]; env.timers.clear(); await next();
    assert.deepEqual(asked,[undefined,100,101,102,103]);
    assert.deepEqual(env.seen,['run-101','run-102','run-103','run-104']);
    env.observer.stop();
});
test('concurrent windows claim a run once', async () => {
    // Observer-level coordination; the actual IndexedDB transaction is also
    // exercised with two real browser pages in the browser contract.
    const claims=new Set(), seen=[];
    const storage={read:async()=>0,advance:async()=>{},claim:async(k,r)=>{
        const key=k+r.run_id;if(claims.has(key))return false;claims.add(key);return true;
    }};
    const opts={storage, fetchPage:async()=>({status:'success',runs:[run('one')],cursor:1}), notify:async r=>seen.push(r.run_id)};
    const a=setup(opts),b=setup(opts);
    await Promise.all([a.observer.start(who),b.observer.start(who)]);
    assert.deepEqual(seen,['one']);a.observer.stop();b.observer.stop();
});
test('a temporary failure preserves progress and an authorization refusal stops the loop', async () => {
    let calls=0;
    const env=setup({fetchPage:async()=>{calls++;const e=new Error('fault');if(calls>1)e.status=403;throw e;}});
    await env.observer.start(who); assert.equal(env.values.size,0); assert.equal(env.timers.size,1);
    const next=[...env.timers.values()][0];env.timers.clear();await next();assert.equal(env.timers.size,0);
});
