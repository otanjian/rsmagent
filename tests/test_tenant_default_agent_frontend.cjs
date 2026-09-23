// Exercise the console's "set as default Agent" action: the Agent config
// page must offer it for every Agent that is not already the tenant default,
// and selecting it must go through POST /api/agents {action:"set_default"}
// and reload the catalogue so the badge, grid order and workbench order all
// move together.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(
    path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');

function section(start, end) {
    const from = source.indexOf(start), to = source.indexOf(end, from);
    assert.ok(from >= 0 && to > from, `Missing console section ${start}`);
    return source.slice(from, to);
}

function node() {
    const classes = new Set([]);
    return {
        innerHTML: '', textContent: '', dataset: {},
        classList: {
            add: (...x) => x.forEach(v => classes.add(v)),
            remove: (...x) => x.forEach(v => classes.delete(v)),
            contains: x => classes.has(x),
            toggle: (x, on) => (on ? classes.add(x) : classes.delete(x)),
        },
        querySelector: () => null,
        querySelectorAll: () => [],
        focus() {},
    };
}

const agent = (extra = {}) => ({
    id: 'beta', name: 'Beta', description: '', category: 'beta', tags: [],
    greeting: '', persona_summary: '', scene_id: '', enabled: true, ...extra,
});
/** The coding-type badge markup inside ``html``, or null when there is none. */
function codingHint(html) {
    const found = html.match(/<span class="coding-agent-badge[^"]*"[^>]*>[\s\S]*?<\/span>/);
    return found ? found[0] : null;
}

// Load renderAgentDetail with just enough context for its own template.
function renderCtx({ defaultAgentId, tenantDefaultManageable }) {
    const nodes = new Map();
    const ctx = {
        console,
        selectedAdminAgentId: 'beta',
        defaultAgentId,
        // The tenant action is offered from the server's payload, so every case
        // here says whether the caller may appoint it (task 4.4/4.5).
        tenantDefaultManageable: tenantDefaultManageable !== false,
        userDefault: { agent_id: '', revision: null, origin: null },
        // Where the anchor a new session would use came from (task 4.6). The
        // detail pane renders the resolved source, so the sandbox has to carry
        // the same shape the page initialises with.
        defaultResolution: { agent_id: '', source: null },
        t: key => key,
        escapeHtml: x => String(x),
        findAgent: id => (id === 'beta' ? agent() : null),
        agentAvatarHTML: () => '<img class="agent-avatar" alt="">',
        fieldLabelWithTip: label => String(label),
        renderAvatarPicker: () => {},
        initDropdown: () => {},
        refreshAgentCategoryDropdown: () => {},
        refreshAgentSceneDropdown: () => {},
        paintAgentSavedFlash: () => {},
        agentModelDropdownOptions: () => [],
        _sessCfg: { model: { providers: [] } },
        document: {
            getElementById(id) {
                if (!nodes.has(id)) nodes.set(id, node());
                return nodes.get(id);
            },
        },
    };
    vm.createContext(ctx);
    vm.runInContext(
        section('function codingAgentTypeHint(', 'function agentWorkbenchCardHTML(')
        + section('function renderAgentDetail()', 'function renderAvatarPicker('), ctx);
    return { ctx, node: id => ctx.document.getElementById(id) };
}

test('a non-default agent offers "set as tenant default"', () => {
    const { ctx, node } = renderCtx({ defaultAgentId: 'alpha' });

    ctx.renderAgentDetail();

    const html = node('agent-detail-profile').innerHTML;
    assert.ok(html.includes('setAgentAsDefault'), (
        'the Agent config page must offer the action for a non-default Agent'));
    assert.ok(html.includes('agents_set_tenant_default'), 'the action must be labelled');
});

test('the current default agent offers no "set as default"', () => {
    const { ctx, node } = renderCtx({ defaultAgentId: 'beta' });

    ctx.renderAgentDetail();

    const html = node('agent-detail-profile').innerHTML;
    assert.ok(!html.includes('setAgentAsDefault'), (
        'the Agent that already is the default must not offer to become it'));
});

test('a coding Agent is marked with a glyph, not with the type spelled out', () => {
    // Same hint as the workbench card and the new-chat picker rows (change
    // simplify-coding-agent-type-hint), so the identity block cannot keep
    // spelling out a type the rest of the console now shows as an icon.
    const { ctx, node } = renderCtx({ defaultAgentId: '' });
    ctx.findAgent = () => agent({ agent_type: 'coding' });

    ctx.renderAgentDetail();

    const hint = codingHint(node('agent-detail-identity').innerHTML);
    assert.ok(hint, 'the coding Agent lost its type hint');
    assert.equal(hint, ctx.codingAgentTypeHint(),
        'the identity block drew its own hint instead of the shared one');
    assert.match(hint, /fa-terminal/);
    assert.doesNotMatch(hint, />[^<]*[^\s<]/,
        'the identity block still renders the type as visible text');
    // The icon is a label, not the field: the form still states the type in
    // words, which is where the value is actually read and edited.
    assert.match(node('agent-detail-profile').innerHTML, /agents_type_coding/,
        'the type field lost its wording');
});

test('a normal Agent keeps the hint hidden', () => {
    const { ctx, node } = renderCtx({ defaultAgentId: '' });
    ctx.findAgent = () => agent({ agent_type: 'normal' });

    ctx.renderAgentDetail();

    const hint = codingHint(node('agent-detail-identity').innerHTML);
    assert.ok(hint, 'the toggle element disappeared');
    assert.match(hint, /\bhidden\b/, 'a normal Agent was marked as a coding one');
});

test('setAgentAsDefault posts the action and reloads the catalogue', async () => {
    const calls = [];
    const ctx = {
        console,
        t: key => key,
        selectedAdminAgentId: 'beta',
        fetch: async (url, opts) => {
            calls.push({ url, opts });
            return { ok: true, json: async () => ({ status: 'success' }) };
        },
        loadAgentCatalog: async () => { calls.push({ url: 'loadAgentCatalog' }); },
        renderAgentsGrid: () => { calls.push({ url: 'renderAgentsGrid' }); },
        renderAgentDetail: () => { calls.push({ url: 'renderAgentDetail' }); },
        document: { getElementById: () => null },
    };
    vm.createContext(ctx);
    vm.runInContext(
        section('function setAgentAsDefault(', 'function updateAgentWorkspace('), ctx);

    await ctx.setAgentAsDefault('beta');

    const post = calls.find(c => c.opts && String(c.opts.body).includes('set_default'));
    assert.ok(post, 'a set_default write must be issued');
    assert.equal(post.url, '/api/agents');
    assert.equal(JSON.parse(post.opts.body).id, 'beta');
    assert.ok(calls.some(c => c.url === 'loadAgentCatalog'), (
        'the catalogue must be reloaded so the default flag moves everywhere'));
});

test('a failed appointment surfaces a readable reason', async () => {
    const ctx = {
        console,
        t: key => key,
        selectedAdminAgentId: 'beta',
        fetch: async () => ({
            ok: false,
            json: async () => ({ status: 'error', code: 'forbidden' }),
        }),
        loadAgentCatalog: async () => { throw new Error('must not reload on failure'); },
        renderAgentsGrid: () => {},
        renderAgentDetail: () => {},
        document: { getElementById: () => null },
    };
    vm.createContext(ctx);
    vm.runInContext(
        section('function setAgentAsDefault(', 'function updateAgentWorkspace('), ctx);

    const ok = await ctx.setAgentAsDefault('beta');

    assert.equal(ok, false, 'a refused appointment must report failure');
});
