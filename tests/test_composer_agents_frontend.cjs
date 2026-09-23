// Run the shipped catalog adapter and composer functions against both API
// contracts. The browser suite exercises the actual menu and team requests.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');
function section(start, end) {
    const from = source.indexOf(start), to = source.indexOf(end, from);
    assert.ok(from >= 0 && to > from);
    return source.slice(from, to);
}
function node() {
    const classes = new Set(['hidden']);
    return { innerHTML: '', textContent: '', dataset: {},
        classList: { add: x => classes.add(x), remove: x => classes.delete(x),
            contains: x => classes.has(x), toggle: (x, on) => on ? classes.add(x) : classes.delete(x) },
        setAttribute() {}, removeAttribute() {}, querySelector() { return null; } };
}
function setup(payload) {
    const nodes = new Map();
    const ctx = { agentCatalog: [], activeAgentId: 'owner', defaultAgentId: 'old-default',
        // Filled by ``loadChatAgentCatalog`` below, which mirrors the app's
        // separate use-range read.
        chatAgentCatalog: null,
        sessionId: 'existing-session', currentView: 'chat', _authEpoch: 1, tenant: 'tenant-a',
        selectedAdminAgentId: '', _sessCfg: null, channelInstances: [], rosterRevision: '',
        sessionStorage: { getItem: () => ctx.tenant },
        fetch: async () => ({ json: async () => payload }),
        writeScopedPreference() {}, renderAgentsGrid() {}, closeAgentDetail() {}, renderMemoryAgentSelect() {},
        refreshBubbleAvatars() {}, conversationHasMessages: () => true,
        escapeHtml: x => String(x), t: x => x,
        agentAvatarHTML: a => `<avatar>${a.name}</avatar>`,
        document: { getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); } },
    };
    vm.createContext(ctx);
    for (const [start, end] of [
        ['function findAgent(', '/* An uploaded avatar'],
        ['function loadAgentCatalog()', 'function renderAgentsGrid()'],
        ['function paintChatAgentIdentity(', 'function conversationHasMessages()'],
        ['function multiAgentMode()', '// Who is answering'],
        ['function renderComposerIdentity()', 'function toggleComposerAgentMenu('],
        ['function renderComposerAgentMenu()', '/** Jump from the composer'],
        ['function teamRosterRows()', 'function addressedAgentId('],
        ['function currentTeamIds()', 'function setTeamMembers('],
    ]) vm.runInContext(section(start, end), ctx);
    // The roster the pickers read is a read of its own (the workbench
    // projection), so the harness performs it against the same payload the
    // management catalogue came from. The team surfaces deliberately do NOT
    // fall back to the management catalogue, so a stub that left
    // ``chatAgentCatalog`` empty would make every invite row vanish.
    ctx.loadChatAgentCatalog = () => {
        const rows = (payload && payload.agents) || [];
        // Same shape the real read returns: normalized rows, default first.
        ctx.chatAgentCatalog = rows.map(ctx.normalizeAgentCatalogEntry)
            .sort((a, b) => Number(b.is_default) - Number(a.is_default));
        return Promise.resolve();
    };
    return { ctx, get: id => ctx.document.getElementById(id) };
}
const projected = (id, extra = {}) => ({ id, name: id, can_chat: true, is_default: false, ...extra });

test('tenant projection restores all selectable Agents without enabled or a global default', async () => {
    const { ctx, get } = setup({ status: 'success', agents: [projected('research'), projected('owner', { is_default: true }), projected('coder')] });
    await ctx.loadAgentCatalog();
    assert.deepEqual(Array.from(ctx.availableChatAgents(), a => a.id), ['owner', 'research', 'coder']);
    assert.equal(ctx.defaultAgentId, 'owner');
    assert.equal(ctx.activeAgentId, 'owner');
    assert.equal(ctx.sessionId, 'existing-session');
    assert.equal(get('composer-identity').classList.contains('hidden'), false);
    ctx.renderComposerAgentMenu();
    assert.match(get('composer-agent-menu').innerHTML, /inviteTeamMember\('research'\)/);
    assert.match(get('composer-agent-menu').innerHTML, /inviteTeamMember\('coder'\)/);
});

test('legacy enabled flags remain authoritative and archived Agents are not offered', async () => {
    const { ctx } = setup({ status: 'success', default_agent_id: 'owner', agents: [
        { id: 'owner', name: 'Owner', enabled: true },
        { id: 'legacy', name: 'Legacy', enabled: true },
        { id: 'archived', name: 'Archived', enabled: false, can_chat: true },
    ] });
    await ctx.loadAgentCatalog();
    assert.deepEqual(Array.from(ctx.availableChatAgents(), a => a.id), ['owner', 'legacy']);
});

test('runtime-unavailable and malformed capabilities do not become chat choices', async () => {
    const { ctx, get } = setup({ status: 'success', agents: [projected('owner', { is_default: true }),
        projected('blocked', { can_chat: false }), { id: 'unknown', name: 'Unknown' },
        projected('bad', { can_chat: 'true' })] });
    await ctx.loadAgentCatalog();
    assert.deepEqual(Array.from(ctx.availableChatAgents(), a => a.id), ['owner']);
    assert.equal(ctx.findAgent('blocked').enabled, true, 'Runtime readiness must not rewrite the management enabled state');
    assert.equal(get('composer-identity').classList.contains('hidden'), true);
    ctx.renderComposerAgentMenu();
    assert.doesNotMatch(get('composer-agent-menu').innerHTML, /inviteTeamMember/);
});

test('an existing group keeps the member controls of members it can still run', async () => {
    const { ctx, get } = setup({ status: 'success', agents: [projected('owner', { is_default: true }), projected('guest')] });
    ctx._sessCfg = { team: { members: [{ id: 'guest', name: 'Guest' }] } };
    await ctx.loadAgentCatalog();
    assert.equal(ctx.sharedConversation(), true);
    assert.equal(get('composer-identity').classList.contains('hidden'), false);
    assert.match(get('composer-agent-btn').innerHTML, /composer-agent-count/);
    ctx.renderComposerAgentMenu();
    assert.match(get('composer-agent-menu').innerHTML, /removeTeamMember\('guest'\)/);
    assert.doesNotMatch(get('composer-agent-menu').innerHTML, /pickComposerAgent/);
});

test('a member that can no longer take part is a generic warning, never a named row', async () => {
    // The roster the server echoed still names 'gone', but it is outside the
    // caller's eligible team candidates. Showing its name, face or type would be
    // exactly the leak the team boundary forbids, so the menu reports an invalid
    // member and offers no row for it (change refine-sidebar-team-chat-launch,
    // task 2.5).
    const { ctx, get } = setup({ status: 'success', agents: [projected('owner', { is_default: true })] });
    ctx._sessCfg = { team: { members: [{ id: 'gone', name: 'Gone', agent_type: 'coding' }] } };
    await ctx.loadAgentCatalog();
    assert.equal(ctx.sharedConversation(), true);
    ctx.renderComposerAgentMenu();
    const html = get('composer-agent-menu').innerHTML;
    assert.doesNotMatch(html, /removeTeamMember\('gone'\)/);
    assert.doesNotMatch(html, /Gone/);
    assert.match(html, /team_invalid_members/);
});

test('a late catalog from the previous tenant cannot restore its Agents', async () => {
    const { ctx, get } = setup(null);
    let resolve;
    ctx.fetch = () => new Promise(done => { resolve = done; });
    const pending = ctx.loadAgentCatalog();
    ctx.tenant = 'tenant-b';
    resolve({ json: async () => ({ status: 'success', agents: [projected('foreign')] }) });
    await pending;
    assert.equal(ctx.agentCatalog.length, 0);
    assert.equal(get('composer-identity').classList.contains('hidden'), true);
});
