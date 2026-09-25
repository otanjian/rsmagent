// Tool selection must round-trip permissions, including no tools and rapid edits.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');
const code = source.slice(source.indexOf('const _capabilitySaveState ='), source.indexOf('function renderAgentTasksPane('));
const flush = () => new Promise(resolve => setImmediate(resolve));

function setup(fields = {}, { deferred = false, tools = ['read', 'write', 'bash'] } = {}) {
    const agent = { id: 'test-agent', ...fields };
    const saved = { ...agent };
    const requests = [];
    const pending = [];
    let inputs = [];
    let html = '';
    const collect = (value) => {
        for (const [, attrs] of value.matchAll(/<input\b([^>]*)>/g)) {
            inputs.push({
                id: /id="([^"]*)"/.exec(attrs)?.[1],
                className: /class="([^"]*)"/.exec(attrs)?.[1] || '',
                value: /value="([^"]*)"/.exec(attrs)?.[1],
                checked: /\bchecked\b/.test(attrs),
                disabled: /\bdisabled\b/.test(attrs),
                indeterminate: false,
                addEventListener(event, handler) { this[event] = handler; },
            });
        }
    };
    const matching = (selector) => {
        if (!selector.startsWith('.agent-tool-item') && !selector.startsWith('.agent-skill-item')) return [];
        const name = selector.slice(1).split(':')[0];
        return inputs.filter(input => input.className === name && (!selector.endsWith(':checked') || input.checked));
    };
    const status = { textContent: '' };
    const pane = {
        get innerHTML() { return html; },
        set innerHTML(value) { html = value; inputs = []; collect(value); },
        querySelectorAll: matching,
    };
    // The two catalogue containers the pane paints its rows into.
    let toolsHtml = '';
    const toolsList = {
        get innerHTML() { return toolsHtml; },
        set innerHTML(value) { toolsHtml = value; collect(value); },
        querySelectorAll: matching,
    };
    const ctx = vm.createContext({
        selectedAdminAgentId: agent.id, rosterRevision: 'r1',
        installedSkills: [{ name: 'example' }],
        installedTools: tools.map(tool => typeof tool === 'string' ? { name: tool, description: tool + ' description' } : tool),
        findAgent: () => agent, t: key => key, escapeHtml: value => String(value),
        document: { getElementById(id) {
            if (id === 'agent-detail-skills') return pane;
            if (id === 'agent-tools-list') return toolsList;
            if (id === 'agent-editor-status') return status;
            return inputs.find(input => input.id === id);
        } },
        fetch: async (url, options) => {
            if (!options) return { json: async () => ({ tools: [] }) };
            assert.equal(url, '/api/agents');
            const body = JSON.parse(options.body);
            requests.push(body);
            if (deferred) await new Promise(resolve => pending.push(resolve));
            Object.assign(saved, body);
            return { json: async () => ({ status: 'success', revision: 'r' + (requests.length + 1) }) };
        },
    });
    vm.runInContext(code, ctx);
    ctx.renderAgentCapabilitiesPane();
    const master = () => inputs.find(input => input.id === 'agent-tools-all');
    const items = () => pane.querySelectorAll('.agent-tool-item');
    const toggle = (name, checked) => {
        const input = name === '*' ? master() : items().find(input => input.value === name);
        assert.ok(input && !input.disabled, `${name} must be selectable`);
        input.checked = checked;
        input.change({ target: input });
    };
    return { agent, saved, requests, pending, pane, ctx, master, items, toggle };
}

const selected = ui => ui.items().filter(input => input.checked).map(input => input.value);
const deliveryTools = ['read', 'write', 'bash',
    { name: 'requirements_delivery', requires_explicit_binding: true },
    { name: 'another_opt_in', requires_explicit_binding: true }];

test('inherited defaults do not display explicit opt-in tools as selected', () => {
    for (const fields of [{}, { tools_allowlist: [] }]) {
        const ui = setup(fields, { tools: deliveryTools });
        assert.deepEqual(selected(ui), ['read', 'write', 'bash']);
        assert.equal(ui.master().checked, false);
        assert.equal(ui.master().indeterminate, true);
    }
});

test('an older catalog still saves delivery explicitly before the backend restarts', async () => {
    const tools = ['read', 'write', 'requirements_delivery'];
    const ui = setup({}, { tools });
    assert.deepEqual(selected(ui), ['read', 'write']);
    ui.toggle('requirements_delivery', true);
    await flush();
    assert.deepEqual(ui.saved.tools_allowlist, tools);
    assert.deepEqual(selected(setup(ui.saved, { tools })), tools);
});

test('checking delivery persists an explicit binding and retains other selections', async () => {
    const ui = setup({ tools_denylist: ['bash', 'hidden'] }, { tools: deliveryTools });
    ui.toggle('requirements_delivery', true);
    await flush();
    assert.deepEqual(ui.saved.tools_allowlist, ['read', 'write', 'requirements_delivery']);
    assert.deepEqual(ui.saved.tools_denylist, ['bash', 'hidden']);
    assert.deepEqual(selected(setup(ui.saved, { tools: deliveryTools })),
        ['read', 'write', 'requirements_delivery']);
    ui.toggle('requirements_delivery', false);
    await flush();
    assert.deepEqual(selected(setup(ui.saved, { tools: deliveryTools })), ['read', 'write']);
    ui.toggle('requirements_delivery', true);
    await flush();
    assert.deepEqual(selected(setup(ui.saved, { tools: deliveryTools })),
        ['read', 'write', 'requirements_delivery']);
});

test('select-all explicitly enables opt-in tools and preserves hidden policy entries', async () => {
    const ui = setup({ tools_allowlist: ['read', 'offline'], tools_denylist: ['hidden'] }, { tools: deliveryTools });
    ui.toggle('*', true);
    await flush();
    assert.ok(ui.saved.tools_allowlist.includes('requirements_delivery'));
    assert.ok(ui.saved.tools_allowlist.includes('offline'));
    assert.deepEqual(ui.saved.tools_denylist, ['hidden']);
    assert.equal(setup(ui.saved, { tools: deliveryTools }).master().checked, true);
    ui.toggle('*', false);
    await flush();
    assert.deepEqual(selected(setup(ui.saved, { tools: deliveryTools })), []);
});

test('delivery can be the first selected tool after clearing inherited defaults', async () => {
    const ui = setup({}, { tools: deliveryTools });
    ui.toggle('*', false);
    await flush();
    ui.toggle('requirements_delivery', true);
    await flush();
    assert.deepEqual(ui.saved.tools_allowlist, ['requirements_delivery']);
    assert.deepEqual(selected(setup(ui.saved, { tools: deliveryTools })), ['requirements_delivery']);
});

test('default permissions select all tools and allow individual cancellation', async () => {
    for (const fields of [{}, { tools_allowlist: [] }, { tools_allowlist: null, tools_denylist: [] }]) {
        const ui = setup(fields);
        assert.equal(ui.master().checked, true);
        assert.deepEqual(selected(ui), ['read', 'write', 'bash']);
        assert.ok(!ui.pane.innerHTML.includes('agent-tool-allow'));
        assert.ok(!ui.pane.innerHTML.includes('agent-tool-deny'));
        ui.toggle('write', false);
        assert.deepEqual(selected(ui), ['read', 'bash']);
        assert.equal(ui.master().checked, false);
        assert.equal(ui.master().indeterminate, true);
        await flush();
        assert.deepEqual(ui.saved.tools_denylist, ['write']);
        assert.deepEqual(selected(setup(ui.saved)), ['read', 'bash']);
    }
});

test('legacy allow/deny selections round-trip without enabling other tools', async () => {
    const ui = setup({ tools_allowlist: ['read', 'write', 'offline'], tools_denylist: ['write', 'hidden'] });
    assert.deepEqual(selected(ui), ['read']);
    ui.toggle('bash', true);
    await flush();
    assert.deepEqual(selected(setup(ui.saved)), ['read', 'bash']);
    assert.ok(ui.saved.tools_allowlist.includes('offline'));
    assert.deepEqual(ui.saved.tools_denylist, ['write', 'hidden']);
    ui.toggle('write', true);
    await flush();
    assert.equal(ui.master().checked, true);
    assert.equal(ui.master().indeterminate, false);
    assert.deepEqual(ui.saved.tools_denylist, ['hidden']);
});

test('unchecking the last allowed tool stays empty after saving and reopening', async () => {
    const ui = setup({ tools_allowlist: ['read'] });
    ui.toggle('read', false);
    await flush();
    assert.deepEqual(selected(setup(ui.saved)), []);
    assert.deepEqual(ui.saved.tools_allowlist, ['read']);
    assert.deepEqual(ui.saved.tools_denylist, ['read']);
    assert.equal(ui.master().indeterminate, false);
});

test('select-all and clear-all update every listed tool, preserving hidden exclusions', async () => {
    const ui = setup({ tools_allowlist: ['read'], tools_denylist: ['hidden'] });
    ui.toggle('*', true);
    await flush();
    assert.equal(ui.saved.tools_allowlist, null);
    assert.deepEqual(ui.saved.tools_denylist, ['hidden']);
    assert.deepEqual(selected(setup(ui.saved)), ['read', 'write', 'bash']);
    ui.toggle('*', false);
    await flush();
    assert.deepEqual(selected(setup(ui.saved)), []);
    assert.deepEqual(ui.saved.tools_denylist, ['hidden', 'read', 'write', 'bash']);
    ui.toggle('write', true);
    await flush();
    assert.deepEqual(selected(setup(ui.saved)), ['write']);
});

test('rapid capability changes serialize writes with the latest revision and selections', async () => {
    const ui = setup({}, { deferred: true });
    ui.toggle('read', false);
    ui.toggle('write', false);
    ui.ctx.saveAgentCapabilities(ui.agent, { skills: ['example'] });
    ui.toggle('bash', false);
    assert.equal(ui.requests.length, 1);
    assert.deepEqual(selected(ui), []);
    ui.pending.shift()();
    await flush();
    assert.equal(ui.requests.length, 2);
    assert.equal(ui.requests[1].revision, 'r2');
    assert.deepEqual(ui.requests[1].tools_denylist, ['read', 'write', 'bash']);
    assert.deepEqual(ui.requests[1].skills, ['example']);
    ui.pending.shift()();
    await flush();
    assert.deepEqual(selected(setup(ui.saved)), []);
});

test('an empty tool catalog disables the master checkbox', async () => {
    const ui = setup({}, { tools: [] });
    await flush();
    assert.equal(ui.master().disabled, true);
    assert.equal(ui.master().checked, false);
    assert.equal(ui.master().indeterminate, false);
});
