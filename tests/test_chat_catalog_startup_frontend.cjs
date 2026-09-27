const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../channel/web/static/js/console.js'), 'utf8');
const from = source.indexOf('let _chatCatalogRequest =');
const to = source.indexOf('function renderAgentsGrid()', from);
const deferred = () => {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
};

function setup(fetchAgentWorkbench) {
    const ctx = { _authEpoch: 1, tenant: 'A', chatAgentCatalog: null, agentCatalog: [],
        fetchAgentWorkbench, sessionStorage: { getItem: () => ctx.tenant },
        renderComposerIdentity() {}, syncNewChatControls() {} };
    vm.createContext(ctx);
    vm.runInContext(source.slice(from, to), ctx);
    return ctx;
}

test('concurrent chat roster reads share a request but later refreshes are fresh', async () => {
    const pending = deferred(); let calls = 0;
    const ctx = setup(() => { calls++; return pending.promise; });
    const first = ctx.loadChatAgentCatalog(), second = ctx.loadChatAgentCatalog();
    assert.equal(first, second);
    assert.equal(calls, 1);
    pending.resolve([{ id: 'shared', is_default: true }]);
    await first;
    assert.equal(ctx.chatAgentCatalog[0].id, 'shared');
    await ctx.loadChatAgentCatalog();
    assert.equal(calls, 2);
});

test('an old tenant reply cannot repaint a new tenant or clear its in-flight read', async () => {
    const old = deferred(), fresh = deferred(); let calls = 0;
    const ctx = setup(() => (++calls === 1 ? old : fresh).promise);
    const first = ctx.loadChatAgentCatalog();
    ctx.tenant = 'B';
    const second = ctx.loadChatAgentCatalog();
    old.resolve([{ id: 'tenant-A' }]);
    await first;
    assert.equal(ctx.chatAgentCatalog, null);
    assert.equal(ctx.loadChatAgentCatalog(), second);
    fresh.resolve([{ id: 'tenant-B' }]);
    await second;
    assert.equal(ctx.chatAgentCatalog[0].id, 'tenant-B');
});

test('a failed chat roster read rejects startup and can be retried', async () => {
    let fail = true;
    const ctx = setup(async () => {
        if (fail) throw Error('permission denied');
        return [];
    });
    await assert.rejects(ctx.loadChatAgentCatalog(), /permission denied/);
    assert.equal(ctx.chatAgentCatalog, null);
    fail = false;
    await ctx.loadChatAgentCatalog();
    assert.deepEqual(ctx.chatAgentCatalog, []);
});

test('management data loads on demand without painting a view the user already left', async () => {
    const pending = deferred(); let renders = 0, reads = 0;
    const ctx = setup(async () => []);
    ctx.currentView = 'knowledge';
    ctx.loadAgentCatalog = () => { reads++; return pending.promise; };
    const loaded = ctx.withManagementCatalog('knowledge', () => renders++);
    assert.equal(reads, 1);
    ctx.currentView = 'chat';
    pending.resolve();
    await loaded;
    assert.equal(renders, 0);
});
