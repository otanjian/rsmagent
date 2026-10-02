// The shipped account view composed with the actual console fetch wrapper.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {boot, ROOT, consoleSource, memorySource} = require('./support/account_memory.cjs');

test('shipped HTML loads exactly one master-derived memory module before console boot', () => {
    const html = fs.readFileSync(path.join(ROOT, 'channel/web/chat.html'), 'utf8');
    const scripts = [...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map(m => m[1]);
    assert.equal(scripts.filter(s => s.includes('fork/views/memory.js')).length, 1);
    assert.ok(scripts.findIndex(s => s.includes('doc-editor.js')) < scripts.findIndex(s => s.includes('fork/views/memory.js')));
    assert.ok(scripts.findIndex(s => s.includes('fork/views/memory.js')) < scripts.findIndex(s => /\/console.js/.test(s)));
    assert.ok(!consoleSource.includes('function loadMemoryView('));
    assert.ok(!html.includes('id="memory-agent-select"'));
    assert.ok(memorySource.includes('48c0d79c36146950667f8d1884ab823deae62305'));
});
for (const form of ['string', 'URL', 'Request', 'JSON POST']) test(`${form} memory requests retain tenant and never inherit active Agent`, async () => {
    const {sandbox, calls} = boot([{}]);
    let input = '/api/memory?scope=personal';
    let init;
    if (form === 'URL') input = new URL(input, 'http://localhost');
    if (form === 'Request') input = new Request(new URL(input, 'http://localhost'), {headers: {'X-Test': 'keep'}});
    if (form === 'JSON POST') {input = '/api/memory/save'; init = {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({scope: 'personal', content: 'X'})};}
    await sandbox.fetch(input, init);
    assert.ok(!calls[0].url.includes('agent_id'));
    assert.equal(calls[0].body?.agent_id, undefined);
    assert.equal(calls[0].init.headers.get('X-Tenant-ID'), 'tenant-a');
    if (form === 'Request') assert.equal(calls[0].init.headers.get('X-Test'), 'keep');
});
test('explicit legacy Agent memory and other Agent APIs keep their targets', async () => {
    const {sandbox, calls} = boot([{}, {}]);
    await sandbox.fetch('/api/memory?agent_id=legacy');
    await sandbox.fetch('/api/sessions');
    assert.equal(calls[0].url, '/api/memory?agent_id=legacy');
    assert.match(calls[1].url, /agent_id=agent-x/);
});
test('late previous-account list response is discarded', async () => {
    let release;
    const {sandbox, run, node} = boot([() => new Promise(resolve => {release = resolve;})]);
    const pending = sandbox.loadMemoryView(1);
    await new Promise(setImmediate);
    sandbox._authEpoch++;
    sandbox.resetMemoryView();
    release({status: 'success', list: [{filename: 'PRIVATE', type: 'daily', size: 1, updated_at: ''}], total: 1});
    await pending;
    assert.equal(run('memoryMetadata'), null);
    assert.equal(node('memory-table-body').children.length, 0);
});
test('network error and true empty result have different states', async () => {
    const {sandbox, node} = boot([new Error('network-down'), {status:'success', list:[],total:0}]);
    await sandbox.loadMemoryView();
    assert.equal(node('memory-emptyp').textContent, 'network-down');
    await sandbox.loadMemoryView();
    assert.equal(node('memory-emptyp2').textContent, 'memory_account_empty_hint');
});
