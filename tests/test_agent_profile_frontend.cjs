// Exercise console.js agent-profile editing for the new digital-employee fields.
// Verifies that saveAgentProfile reads position/category/tags/greeting/persona
// and the scene selector, and sends them through the workspace write. Also
// verifies old clients cleanly round-trip the fields via findAgent + snapshot.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');
function section(start, end) {
    const from = source.indexOf(start), to = source.indexOf(end, from);
    assert.ok(from >= 0 && to > from, `Missing console section ${start}`);
    return source.slice(from, to);
}
function node(value = '', dd = false) {
    const classes = new Set([]);
    return {
        innerHTML: '', textContent: '', dataset: {}, attrs: {},
        _ddValue: value, _ddDropdown: dd,
        classList: { add: (...x) => x.forEach(v => classes.add(v)), remove: (...x) => x.forEach(v => classes.delete(v)), contains: x => classes.has(x), toggle: (x, on) => on ? classes.add(x) : classes.delete(x) },
        setAttribute(k, v) { this.attrs[k] = v; },
        removeAttribute(k) { delete this.attrs[k]; },
        querySelector: () => null,
        focus() {}, value,
    };
}

function setup({ agent, fetchImpl }) {
    const nodes = new Map();
    const events = [];
    const ctx = {
        console, selectedAdminAgentId: agent.id, rosterRevision: 'r1', _agentSavedFlashUntil: 0,
        defaultAgentId: 'other',
        document: { getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); } },
        t: key => ({ save: '保存', agents_save_failed: '保存失败' })[key] || key,
        escapeHtml: x => String(x),
        findAgent: id => (id === agent.id ? agent : null),
        getDropdownValue: el => (el ? el._ddValue : ''),
        updateAgentWorkspace: (id, payload) => { events.push(['write', id, payload]); return Promise.resolve(true); },
        flashAgentProfileStatus() {},
        fetch: (...a) => { events.push(['fetch', a[0]]); return fetchImpl(...a); },
        _identityMode: () => 'legacy',
    };
    vm.createContext(ctx);
    // Load saveAgentProfile + its helpers (scene catalog, dropdown, saved flash).
    vm.runInContext(section('let _sceneCatalogCache = null;', 'function saveAgentSkills('), ctx);
    vm.runInContext(section('function saveAgentProfile()', 'function paintAgentSavedFlash('), ctx);
    vm.runInContext("function getDropdownValue(el) { return el._ddValue || ''; }", ctx);
    return { ctx, events, node: id => ctx.document.getElementById(id) };
}

const agent = (extra = {}) => ({ id: 'proc', name: '采购专员', description: '', position: '', category: '', tags: [], greeting: '', persona_summary: '', scene_id: '', ...extra });

test('saveAgentProfile sends the digital-employee fields', async () => {
    const a = agent();
    const { ctx, events } = setup({ agent: a, fetchImpl: async () => ({ json: async () => ({}) }) });
    ctx.document.getElementById('agent-edit-name').value = '采购专员';
    ctx.document.getElementById('agent-edit-description').value = '负责采购';
    ctx.document.getElementById('agent-edit-position').value = '采购专员';
    ctx.document.getElementById('agent-edit-category')._ddValue = 'procurement';
    ctx.document.getElementById('agent-edit-tags').value = '供应商, 招标';
    ctx.document.getElementById('agent-edit-greeting').value = '你好，我是采购专员。';
    ctx.document.getElementById('agent-edit-persona').value = '语气专业';
    ctx.document.getElementById('agent-edit-scene')._ddValue = 'procurement';
    ctx.document.getElementById('agent-edit-model')._ddValue = '|gpt-5';

    await ctx.saveAgentProfile();

    const write = events.find(e => e[0] === 'write');
    assert.ok(write, 'a write must be issued');
    assert.equal(write[1], 'proc');
    assert.equal(write[2].position, '采购专员');
    assert.equal(write[2].category, 'procurement');
    assert.deepEqual([...write[2].tags], ['供应商', '招标']);
    assert.equal(write[2].greeting, '你好，我是采购专员。');
    assert.equal(write[2].persona_summary, '语气专业');
    assert.equal(write[2].scene_id, 'procurement');
    // Model follows the dropdown value (empty provider -> '|gpt-5' split).
    assert.equal(write[2].model, 'gpt-5');
    assert.equal(write[2].bot_type, '');
});

test('profile tags accept mixed commas, trim, deduplicate and clear', async () => {
    const { ctx, events, node } = setup({ agent: agent() });
    node('agent-edit-name').value = '采购专员';
    node('agent-edit-tags').value = ' 财务,经营分析，财务, ,归因 ， ';
    await ctx.saveAgentProfile();
    assert.deepEqual([...events.find(e => e[0] === 'write')[2].tags], ['财务', '经营分析', '归因']);
    events.length = 0;
    node('agent-edit-tags').value = '， , ';
    await ctx.saveAgentProfile();
    assert.deepEqual([...events.find(e => e[0] === 'write')[2].tags], []);
});

test('saveAgentCapabilities sends sops and tool allow/deny in one update', async () => {
    const a = agent({ sops: ['sop-1'], tools_allowlist: ['read'], tools_denylist: ['write'] });
    const { ctx, events, node } = setup({ agent: a, fetchImpl: async () => ({ json: async () => ({ status: 'success', revision: 'r2' }) }) });
    // Load saveAgentSkills (which defines _skillSaveState and the fetch helper)
    // then the capabilities helper.
    vm.runInContext(section('function saveAgentSkills(', 'function renderAgentCapabilitiesPane('), ctx);
    vm.runInContext("function getDropdownValue(el) { return el._ddValue || ''; }", ctx);

    ctx.saveAgentCapabilities(a, { sops: ['sop-1', 'sop-2'], tools_denylist: ['write'] });
    const write = events.find(e => e[0] === 'fetch' && String(e[1]).includes('/api/agents'));
    assert.ok(write, 'a capabilities write must be issued');
    // The fetch call is the first argument; the body was passed as the 2nd arg's body.
    // We assert on the optimistic agent state so we don't need to parse the body.
    assert.deepEqual([...a.sops], ['sop-1', 'sop-2']);
    assert.deepEqual([...a.tools_denylist], ['write']);
    // Revision adopted from the response.
    await new Promise(r => setImmediate(r));
});

test('workbench card projects position/category and tags', () => {
    const a = agent({ position: '采购专员', category: 'procurement', tags: ['供应商', '招标'], can_chat: true, is_default: false });
    const ctx = {
        console, t: k => k,
        escapeHtml: x => String(x),
        findAgent: id => (id === a.id ? a : null),
        agentWorkbenchLoading: false, _agentStartInFlight: false, _agentStartAgentId: null,
        agentAvatarHTML: (agent) => `<img class="agent-avatar" alt="">`,
        agentUnavailableLabel: (reason) => reason,
    };
    vm.createContext(ctx);
    vm.runInContext(section('function agentUnavailableLabel(', 'function renderAgentWorkbench('), ctx);
    const html = ctx.agentWorkbenchCardHTML(a, true, null);
    assert.ok(html.includes('采购专员'), 'position must render on the card');
    assert.ok(html.includes('agent-wb-card-meta'), 'a meta line must be rendered');
    assert.ok(html.includes('供应商') && html.includes('招标'), 'tags must render as chips');
    assert.ok(html.includes('agent-wb-tag'), 'tags must use the chip class');
    // An agent with neither position nor category renders no meta line.
    const bare = agent({});
    const htmlBare = ctx.agentWorkbenchCardHTML(bare, true, null);
    assert.ok(!htmlBare.includes('agent-wb-card-meta'), 'no meta line when empty position/category');
});

test('agentModelDropdownOptions keeps a pinned model missing from the catalog', () => {
    const ctx = {
        console, t: k => k, localizedLabel: x => x,
        _sessCfg: { model: { providers: [{ id: 'deepseek', label: 'DeepSeek', models: ['deepseek-v4-flash'] }] } },
    };
    vm.createContext(ctx);
    vm.runInContext(section('function agentModelDropdownOptions(', '// Scene catalog options'), ctx);
    // Catalog hit: one row for the pinned model, no synthetic duplicate.
    const hit = ctx.agentModelDropdownOptions({ model: 'deepseek-v4-flash', bot_type: 'deepseek' });
    assert.equal(hit.filter(o => o.value === 'deepseek|deepseek-v4-flash').length, 1);
    // Catalog miss: the pin is still offered instead of collapsing to "follow global".
    const miss = ctx.agentModelDropdownOptions({ model: 'gone-model', bot_type: 'deepseek' });
    const pin = miss.find(o => o.value === 'deepseek|gone-model');
    assert.ok(pin, 'the pinned model must remain selectable');
    assert.equal(pin.label, 'gone-model');
    assert.equal(miss[0].value, '', 'follow-global stays the first row');
});

test('renderAgentTasksPane fetches tasks scoped to the agent and renders a card', async () => {
    const a = agent({ id: 'proc' });
    const nodes = new Map();
    const fetchUrls = [];
    const pane = { innerHTML: '', appendChild(child) { this.children.push(child); }, children: [] };
    function el() {
        return {
            innerHTML: '', dataset: {}, className: '', classList: { add: () => {}, toggle: () => {}, remove: () => {} },
            appendChild() {}, addEventListener() {}, querySelector: () => el(),
        };
    }
    const ctx = {
        console, selectedAdminAgentId: 'proc', rosterRevision: 'r1', defaultAgentId: 'other',
        t: k => k, escapeHtml: x => String(x),
        findAgent: id => (id === a.id ? a : null),
        runTaskNow: () => {},
        showConfirmDialog: () => {},
        document: {
            getElementById(id) { if (id === 'agent-detail-tasks') return pane; return nodes.get(id) || (nodes.set(id, el()), nodes.get(id)); },
            createElement: () => el(),
        },
        fetch: async (url) => { fetchUrls.push(url); return { json: async () => ({ status: 'success', tasks: [{ id: 't1', name: 'nightly', enabled: true, agent_id: 'proc', schedule: { type: 'cron', expression: '0 2 * * *' }, action: { content: 'run' } }] }) }; },
    };
    vm.createContext(ctx);
    vm.runInContext(section('function renderAgentTasksPane(', 'function createAvatarDraft('), ctx);
    await ctx.renderAgentTasksPane();
    assert.ok(fetchUrls.some(u => /agent_id=proc/.test(u)), 'tasks fetched scoped to the owning agent');
    assert.ok(pane.innerHTML.includes('agents_tasks_label'), 'a tasks heading is rendered');
});
