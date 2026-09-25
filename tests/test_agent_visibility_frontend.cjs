// 可见性徽标与私有↔共享转换的前端行为（change
// ``show-and-toggle-agent-visibility``）。
//
// 关注三件界面必须自己保证的事，而不是重复服务端的判定：
//   1. 徽标是「私有 / 租户共享」这条独立轴，与「默认 / 已归档」并列，而不是二选一；
//   2. 转换入口只按服务端给的 can_share / can_unshare 渲染，界面不自己推断资格；
//   3. 两个方向都先说清后果或先选归属人，且拒绝必须读起来像拒绝。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');
const slice = (from, to) => {
    const start = source.indexOf(from);
    const end = source.indexOf(to);
    assert.ok(start >= 0, `console.js must still contain ${from}`);
    assert.ok(end > start, `console.js must still contain ${to} after ${from}`);
    return source.slice(start, end);
};
// The two renderers live in the detail-pane section, between ``renderAgentDetail``
// and ``renderAvatarPicker``; ``renderAgentsGrid`` is sliced in separately. The
// boundaries follow the file's own section split, so reordering one section does
// not silently drop the code under test.
const badgeCode = slice('function agentVisibilityBadgeHTML(', 'function renderAvatarPicker(');
const gridCode = badgeCode + slice('function renderAgentsGrid() {', '// Agent Workbench (use agents)');
const actionCode = slice('/* 私有 → 租户共享。先确认', 'function renderAgentCapabilitiesPane() {');

// The real zh copy, so the wording the user is shown is what gets asserted —
// asserting on a stubbed key would pass even if the disclosure said nothing.
function realT(key) {
    const ctx = { window: {} };
    vm.createContext(ctx);
    vm.runInContext(
        fs.readFileSync(path.join(__dirname, '../channel/web/static/js/i18n/agents.js'), 'utf8'),
        ctx);
    const dict = ctx.window.__cowI18N__.agents.zh;
    return dict[key] || key;
}

function makeElement() {
    const el = {
        innerHTML: '', textContent: '', value: '',
        classes: new Set(['hidden']),
    };
    el.classList = {
        add: c => el.classes.add(c),
        remove: c => el.classes.delete(c),
        toggle: (c, on) => { if (on) el.classes.add(c); else el.classes.delete(c); },
    };
    return el;
}

function badgeSetup() {
    const ctx = vm.createContext({ t: realT, escapeHtml: v => String(v) });
    vm.runInContext(badgeCode, ctx);
    return ctx;
}

test('the badge names which of the two states the object is in', () => {
    const ctx = badgeSetup();
    const priv = ctx.agentVisibilityBadgeHTML({ id: 'a', visibility: 'private' });
    const shared = ctx.agentVisibilityBadgeHTML({ id: 'a', visibility: 'tenant' });

    assert.match(priv, />私有</);
    assert.match(shared, />租户共享</);
    assert.match(priv, /agent-visibility-private/);
    assert.match(shared, /agent-visibility-tenant/);
});

test('a row the server did not describe renders no badge at all', () => {
    const ctx = badgeSetup();
    // The workbench and personal projections do not carry the field; guessing a
    // state for them would label an object the caller may not manage.
    assert.equal(ctx.agentVisibilityBadgeHTML({ id: 'a' }), '');
    assert.equal(ctx.agentVisibilityBadgeHTML(null), '');
});

test('the detail header takes the same pill without the corner positioning', () => {
    const ctx = badgeSetup();
    const inline = ctx.agentVisibilityBadgeHTML({ id: 'a', visibility: 'tenant' }, { inline: true });
    assert.match(inline, /agent-visibility-chip/);
    assert.ok(!inline.includes('agent-card-badge'),
        'the corner chip would be absolutely positioned on top of the name');
});

test('the conversion entry follows the server-derived eligibility', () => {
    const ctx = badgeSetup();
    const share = ctx.agentVisibilityActionsHTML(
        { id: 'a', visibility: 'private', can_share: true, can_unshare: false });
    assert.match(share, /shareAgent\('a'\)/);
    assert.match(share, />转为租户共享</);

    const unshare = ctx.agentVisibilityActionsHTML(
        { id: 'a', visibility: 'tenant', can_share: false, can_unshare: true });
    assert.match(unshare, /startAgentUnshare\('a'\)/);
    assert.match(unshare, />恢复为私有</);
});

test('neither eligible direction means no entry, not a disabled one', () => {
    const ctx = badgeSetup();
    assert.equal(ctx.agentVisibilityActionsHTML(
        { id: 'a', visibility: 'tenant', can_share: false, can_unshare: false }), '');
    // A shared object an ordinary member may use: use is not a conversion.
    assert.equal(ctx.agentVisibilityActionsHTML({ id: 'a', visibility: 'tenant' }), '');
});

function gridSetup(agents, defaultAgentId) {
    const grid = makeElement();
    const ctx = vm.createContext({
        agentCatalog: agents, selectedAdminAgentId: '', defaultAgentId,
        t: realT, escapeHtml: v => String(v),
        agentAvatarHTML: () => '<i class="agent-avatar"></i>',
        document: { getElementById: id => (id === 'agents-grid' ? grid : null) },
    });
    vm.runInContext(gridCode, ctx);
    ctx.renderAgentsGrid();
    return grid.innerHTML;
}

test('the list row carries the visibility chip beside the default chip', () => {
    const html = gridSetup([
        { id: 'a', name: 'Private one', enabled: true, visibility: 'private', description: '' },
    ], 'a');

    // Both facts about one object, stacked — "the default" and "private" are not
    // branches of each other.
    assert.match(html, /agent-card-badges/);
    assert.match(html, />默认</);
    assert.match(html, />私有</);
    // The name has to clear two lines of chips, so the card says so.
    assert.match(html, /agent-card has-visibility/);
});

test('a shared row is labelled shared and an archived one keeps its own chip', () => {
    const html = gridSetup([
        { id: 'a', name: 'Shared', enabled: true, visibility: 'tenant', description: '' },
        { id: 'b', name: 'Stopped', enabled: false, visibility: 'private', description: '' },
    ], '');
    assert.match(html, />租户共享</);
    assert.match(html, />已归档</);
});

function actionSetup({ agent, members = [], response = { status: 'success' } } = {}) {
    const nodes = {
        'agent-profile-status': makeElement(),
        'agent-visibility-owner': makeElement(),
    };
    const requests = [];
    const dialogs = [];
    let reloads = 0;
    const ctx = vm.createContext({
        t: realT, escapeHtml: v => String(v),
        findAgent: id => (agent && id === agent.id ? agent : null),
        showConfirmDialog: opts => dialogs.push(opts),
        document: { getElementById: id => nodes[id] || null },
        loadAgentCatalog: async () => { reloads += 1; },
        fetch: async (url, options) => {
            if (String(url).startsWith('/api/tenant/members')) {
                return { json: async () => ({ items: members }) };
            }
            requests.push(JSON.parse(options.body));
            return { json: async () => response };
        },
    });
    vm.runInContext(actionCode, ctx);
    return { ctx, nodes, requests, dialogs, reloads: () => reloads };
}

test('sharing first asks, and the question spells out what actually changes', () => {
    const ui = actionSetup({ agent: { id: 'a', name: 'A', visibility: 'private', can_share: true } });
    ui.ctx.shareAgent('a');

    assert.equal(ui.requests.length, 0, 'nothing may be written before the confirmation');
    assert.equal(ui.dialogs.length, 1);
    const body = ui.dialogs[0].message;
    // Two facts a confirmation has to carry: it publishes, and it hands over.
    assert.match(body, /所有成员/);
    assert.match(body, /不再独占/);
    assert.equal(ui.dialogs[0].okText, '转为租户共享');
});

test('confirming the share writes exactly one tenant conversion', async () => {
    const ui = actionSetup({ agent: { id: 'a', name: 'A', visibility: 'private', can_share: true } });
    ui.ctx.shareAgent('a');
    await ui.dialogs[0].onConfirm();

    assert.deepEqual(ui.requests, [{ action: 'set_visibility', id: 'a', visibility: 'tenant' }]);
    assert.equal(ui.reloads(), 1, 'the list decides the badge, so it is re-read');
});

test('a row the server did not authorize offers no action and posts nothing', () => {
    const ui = actionSetup({ agent: { id: 'a', name: 'A', visibility: 'tenant' } });
    ui.ctx.shareAgent('a');
    ui.ctx.startAgentUnshare('a');

    assert.equal(ui.dialogs.length, 0);
    assert.equal(ui.requests.length, 0);
});

test('restoring refuses to submit until an owner is chosen', async () => {
    const ui = actionSetup({
        agent: { id: 'a', name: 'A', visibility: 'tenant', can_unshare: true },
        members: [{ user_id: 'usr_1', display_name: 'Rock', active: 1 }],
    });
    await ui.ctx.startAgentUnshare('a');

    // The picker is filled from the existing member list, not a new pipeline.
    assert.ok(!ui.nodes['agent-visibility-owner'].classes.has('hidden'));
    assert.match(ui.nodes['agent-visibility-owner'].innerHTML, /usr_1/);
    assert.match(ui.nodes['agent-visibility-owner'].innerHTML, /Rock/);

    ui.ctx.confirmAgentUnshare('a');   // value is '' — nobody chosen
    assert.equal(ui.requests.length, 0);
    assert.equal(ui.nodes['agent-profile-status'].textContent, '请先选择归属人');
});

test('choosing an owner posts it and re-reads the list', async () => {
    const ui = actionSetup({
        agent: { id: 'a', name: 'A', visibility: 'tenant', can_unshare: true },
        members: [{ user_id: 'usr_1', display_name: 'Rock', active: 1 }],
    });
    await ui.ctx.startAgentUnshare('a');
    const picker = makeElement();
    picker.value = 'usr_1';
    ui.nodes['agent-unshare-owner'] = picker;

    ui.ctx.confirmAgentUnshare('a');
    await new Promise(resolve => setImmediate(resolve));

    assert.deepEqual(ui.requests, [
        { action: 'set_visibility', id: 'a', visibility: 'private', owner_user_id: 'usr_1' },
    ]);
    assert.equal(ui.reloads(), 1);
});

test('a refusal is reported by its own reason and never reads as success', async () => {
    const ui = actionSetup({
        agent: { id: 'a', name: 'A', visibility: 'tenant', can_unshare: true },
        response: { status: 'error', code: 'agent_is_tenant_default', message: 'nope' },
    });
    await ui.ctx.startAgentUnshare('a');
    const picker = makeElement();
    picker.value = 'usr_1';
    ui.nodes['agent-unshare-owner'] = picker;

    ui.ctx.confirmAgentUnshare('a');
    await new Promise(resolve => setImmediate(resolve));

    assert.equal(ui.reloads(), 0, 'a refused conversion must not re-read as if it landed');
    assert.match(ui.nodes['agent-profile-status'].textContent, /租户默认/);
});

test('every refusal code the route can send has its own wording', () => {
    const ui = actionSetup({ agent: { id: 'a', name: 'A' } });
    const text = ui.ctx.agentVisibilityErrorText;
    assert.match(text({ code: 'agent_is_tenant_default' }), /租户默认/);
    assert.match(text({ code: 'forbidden' }), /可管理范围/);
    assert.match(text({ code: 'not_found' }), /不存在/);
    // An unexpected shape still says something actionable rather than "undefined".
    assert.ok(text(null).length > 0);
    assert.ok(text({ status: 'error', code: 'something_new', message: 'raw' }).length > 0);
});
