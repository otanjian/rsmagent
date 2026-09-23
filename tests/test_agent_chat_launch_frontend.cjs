// Starting a chat must never be gated on choosing an Agent (tasks 1.8 / 2.4).
// A tenant that owns several Agents anchors the conversation to the default
// one; the caret is the optional "switch / team" entry. Browser acceptance owns
// the visual layout — these run the shipped functions.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(
    path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');

function section(start, end) {
    const from = source.indexOf(start), to = source.indexOf(end, from);
    assert.ok(from >= 0 && to > from, `section ${start} .. ${end}`);
    return source.slice(from, to);
}

function node() {
    const classes = new Set(['hidden']);
    return {
        innerHTML: '', textContent: '', dataset: {},
        classList: {
            add: (...xs) => xs.forEach(x => classes.add(x)),
            remove: (...xs) => xs.forEach(x => classes.delete(x)),
            contains: x => classes.has(x),
            toggle: (x, on) => { if (on) classes.add(x); else classes.delete(x); return on; },
        },
    };
}

/** The slice that decides chat-launch behaviour, with a recorded newChat().
 *
 *  ``agents`` is the management catalogue (``/api/agents``); ``useAgents`` is
 *  the use-range roster the pickers read (``/api/agents?view=workbench``),
 *  which defaults to the catalogue for the many cases where the two agree. A
 *  plain member's management catalogue holds only their own private Agents, so
 *  the two lists only differ for the members whose picker must still show the
 *  tenant-shared Agents they may chat with. */
function launchHarness(agents, payload, useAgents) {
    const nodes = new Map();
    const calls = { newChat: 0, team: 0 };
    const ctx = {
        agentCatalog: agents, chatAgentCatalog: useAgents || agents,
        activeAgentId: '', defaultAgentId: '',
        _authEpoch: 1, tenant: 'tenant-a', selectedAdminAgentId: '',
        channelInstances: [], rosterRevision: '', currentView: 'chat',
        sessionStorage: { getItem: () => ctx.tenant },
        fetch: async () => ({
            json: async () => payload
                || { status: 'success', agents, default_agent_id: 'owner' },
        }),
        writeScopedPreference() {}, renderAgentsGrid() {}, closeAgentDetail() {},
        renderComposerIdentity() {}, renderMemoryAgentSelect() {}, refreshBubbleAvatars() {},
        renderAgentDetail() {}, openAgentDetail() {},
        agentAvatarHTML: a => `<avatar>${a.name}</avatar>`,
        escapeHtml: x => String(x), t: x => x,
        newChat: () => { calls.newChat += 1; },
        openTeamChatModal: () => { calls.team += 1; },
        document: { getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); } },
    };
    vm.createContext(ctx);
    for (const [start, end] of [
        ['function normalizeAgentCatalogEntry(', 'function loadAgentCatalog()'],
        ['function loadAgentCatalog()', 'function renderAgentsGrid()'],
        ['function findAgent(', 'function enabledAgents()'],
        ['function enabledAgents()', 'function availableChatAgents()'],
        ['function availableChatAgents()', '/* An uploaded avatar'],
        ['function multiAgentMode()', '// Who is answering'],
        ['function sidebarLaunchV2()', 'const SIDEBAR_V2_VIEW_ORDER'],
        // The launch-control section: the panel and the sidebar are the same
        // control in two places, and these cases run the shipped table. The type
        // hint rides along because the picker rows draw it, and the point of one
        // shared implementation is that a row and a card cannot drift apart.
        ['function codingAgentTypeHint(', 'function agentWorkbenchCardHTML('],
        ['const NEW_CHAT_SURFACES = {', 'function startSoloChat('],
    ]) {
        vm.runInContext(section(start, end), ctx);
    }
    // The shipped avatar helper pulls in branding lookups this harness has no
    // reason to model; the menu's *structure* is what these tests pin.
    ctx.agentAvatarHTML = a => `<avatar>${a.name}</avatar>`;
    // Filling the use-range roster is a read of its own (the workbench
    // projection), so the harness supplies the result and exercises the
    // pickers against it instead of re-testing the fetch.
    ctx.loadChatAgentCatalog = () => Promise.resolve();
    return { ctx, calls, get: id => ctx.document.getElementById(id) };
}

const agent = (id, extra = {}) =>
    ({ id, name: id, enabled: true, can_chat: true, is_default: false, ...extra });
/** The coding-type badge markup inside ``html``, or null when there is none. */
function codingHint(html) {
    const found = html.match(/<span class="coding-agent-badge[^"]*"[^>]*>[\s\S]*?<\/span>/);
    return found ? found[0] : null;
}

test('entering chat with several Agents anchors the default, no picker', async () => {
    const { ctx, get } = launchHarness([
        agent('research'), agent('owner', { is_default: true }), agent('coder'),
    ]);
    await ctx.loadAgentCatalog();
    assert.equal(ctx.activeAgentId, 'owner',
        'a multi-Agent tenant did not anchor the default Agent');
    // The menu exists but stays shut until the user asks for it.
    assert.ok(get('new-chat-menu').classList.contains('hidden'));
    // The caret is offered precisely when there is something to choose between.
    assert.equal(get('new-chat-caret').classList.contains('hidden'), false);
});

test('the new-chat button starts a chat instead of forcing a choice', () => {
    const { ctx, calls, get } = launchHarness([
        agent('research'), agent('owner', { is_default: true }), agent('coder'),
    ]);
    ctx.activeAgentId = 'owner';
    // Click on the button body: no caret in the event path.
    ctx.onNewChatButton({ target: { closest: () => null }, stopPropagation() {} });
    assert.equal(calls.newChat, 1, 'the primary action did not start a chat');
    assert.ok(get('new-chat-menu').classList.contains('hidden'),
        'the picker was forced open');
});

test('the caret opens the optional switch / team picker', () => {
    const { ctx, calls, get } = launchHarness([
        agent('research'), agent('owner', { is_default: true }), agent('coder'),
    ]);
    let stopped = false;
    ctx.onNewChatButton({
        target: { closest: sel => (sel === '#new-chat-caret' ? {} : null) },
        stopPropagation() { stopped = true; },
    });
    assert.equal(calls.newChat, 0, 'the caret started a chat instead of opening the picker');
    assert.ok(stopped);
    assert.equal(get('new-chat-menu').classList.contains('hidden'), false);
    // It lists a solo chat per Agent plus the team entry.
    assert.match(get('new-chat-menu').innerHTML, /startSoloChat\('owner'\)/);
    assert.match(get('new-chat-menu').innerHTML, /openTeamChatModal/);
});

test('a single Agent behaves like the console before Agents existed', async () => {
    const { ctx, calls, get } = launchHarness([agent('owner', { is_default: true })]);
    await ctx.loadAgentCatalog();
    assert.equal(ctx.activeAgentId, 'owner');
    // Nothing to choose between -> no caret at all.
    assert.ok(get('new-chat-caret').classList.contains('hidden'));
    ctx.onNewChatButton({ target: { closest: () => null }, stopPropagation() {} });
    assert.equal(calls.newChat, 1);
});

test('re-clicking the caret closes the picker again', () => {
    const { ctx, get } = launchHarness([agent('a'), agent('b')]);
    const caretEvent = {
        target: { closest: sel => (sel === '#new-chat-caret' ? {} : null) },
        stopPropagation() {},
    };
    ctx.onNewChatButton(caretEvent);
    assert.equal(get('new-chat-menu').classList.contains('hidden'), false);
    ctx.onNewChatButton(caretEvent);
    assert.ok(get('new-chat-menu').classList.contains('hidden'),
        'a second caret click did not close the picker');
});

// The console must never *invent* an Agent id. A literal fallback on an empty
// catalogue becomes a real request: the global fetch wrapper copies the active
// id onto every /message and /api call, so a made-up id is sent to the server,
// which rejects it as "agent not found" — reporting a routing failure when the
// member simply may not see any Agent. Sending no id lets the server anchor the
// session to the tenant default instead.

test('an empty Agent catalogue anchors nothing instead of inventing "default"', async () => {
    const { ctx } = launchHarness([], { status: 'success', agents: [] });
    await ctx.loadAgentCatalog();
    assert.equal(ctx.defaultAgentId, '',
        'the console invented a default Agent id out of an empty catalogue');
    assert.equal(ctx.activeAgentId, '',
        'an invented id would ride every request and be rejected as "agent not found"');
});

test('a remembered Agent the catalogue no longer offers is dropped', async () => {
    const { ctx } = launchHarness([agent('owner', { is_default: true })]);
    // A stale value survives in scoped storage: an older build, a deleted
    // Agent, or a tenant the account no longer belongs to.
    ctx.activeAgentId = 'default';
    await ctx.loadAgentCatalog();
    assert.equal(ctx.activeAgentId, 'owner',
        'a stale Agent id kept riding the requests instead of the real default');
});

test('a remembered Agent the catalogue still offers is kept', async () => {
    const { ctx } = launchHarness([
        agent('owner', { is_default: true }), agent('research'),
    ]);
    ctx.activeAgentId = 'research';
    await ctx.loadAgentCatalog();
    assert.equal(ctx.activeAgentId, 'research',
        'an explicit choice was reset to the default while it was still available');
});

// The picker asks which Agents the caller may *chat with*. A plain member's
// management catalogue holds only the Agents they own, so a picker drawn from
// it hid every tenant-shared Agent from that member: the Agent was reachable
// from the console's own chat surface (the use range), yet absent from the
// list that offers it.

test('the picker offers the shared Agents the caller may use', () => {
    const mine = agent('mine', { is_default: true });
    const shared = agent('team-bot', { description: 'tenant shared' });
    const { ctx, get } = launchHarness([mine], null, [shared, mine]);
    ctx.activeAgentId = 'mine';
    ctx.onNewChatButton({
        target: { closest: sel => (sel === '#new-chat-caret' ? {} : null) },
        stopPropagation() {},
    });
    assert.match(get('new-chat-menu').innerHTML, /startSoloChat\('team-bot'\)/,
        'a shared Agent the member may use was missing from the picker');
    assert.match(get('new-chat-menu').innerHTML, /startSoloChat\('mine'\)/,
        'the member\'s own Agent was missing from the picker');
    assert.equal(ctx.multiAgentMode(), true,
        'a member with two usable Agents was treated as having nothing to choose between');
});

test('a shared Agent resolves for the chat identity it names', () => {
    const { ctx } = launchHarness(
        [agent('mine', { is_default: true })], null,
        [agent('team-bot', { name: 'Team Bot' }), agent('mine', { is_default: true })]);
    // The conversation is owned by a shared Agent this member does not manage:
    // without a use-range fallback the composer face and the message speakers
    // would degrade to the raw id.
    assert.equal(ctx.findAgent('team-bot')?.name, 'Team Bot',
        'a shared conversation owner was unresolvable from the management catalogue alone');
});

test('the management grid is not widened by the use-range roster', () => {
    const mine = agent('mine', { is_default: true });
    const { ctx } = launchHarness([mine], null, [agent('team-bot'), mine]);
    assert.deepEqual(ctx.enabledAgents().map(a => a.id), ['mine'],
        'a shared Agent leaked into the management surfaces (grid, clone-from list)');
});

test('an unusable Agent in the use range is not offered for chat', () => {
    const mine = agent('mine', { is_default: true });
    const broken = agent('broken', { can_chat: false, unavailable_reason: 'runtime_not_enabled' });
    const { ctx, get } = launchHarness([mine], null, [broken, mine]);
    ctx.activeAgentId = 'mine';
    ctx.onNewChatButton({
        target: { closest: sel => (sel === '#new-chat-caret' ? {} : null) },
        stopPropagation() {},
    });
    assert.doesNotMatch(get('new-chat-menu').innerHTML, /startSoloChat\('broken'\)/,
        'an Agent that cannot chat was offered as a chat target');
    assert.deepEqual(ctx.availableChatAgents().map(a => a.id), ['mine']);
});

// The sidebar's 新建对话 is the *same* control as the session panel's 新对话:
// its body starts a chat through the sidebar's own entry point (which keeps the
// branding/unsaved guard and the composer focus), and its caret opens the same
// picker. Two menus drawn from two sources is exactly what this pins against.

test('the sidebar launch control is the panel control in another place', () => {
    const { ctx, calls, get } = launchHarness([
        agent('research'), agent('owner', { is_default: true }), agent('coder'),
    ]);
    ctx.activeAgentId = 'owner';
    let sidebarStarts = 0;
    ctx.startSidebarNewChat = () => { sidebarStarts += 1; };
    ctx.sidebarLaunchV2 = () => true;

    // Body click: the sidebar's own entry point, not a bare newChat().
    ctx.onNewChatButton({ target: { closest: () => null }, stopPropagation() {} }, 'sidebar');
    assert.equal(sidebarStarts, 1, 'the sidebar body did not start a chat');
    assert.equal(calls.newChat, 0, 'the sidebar body bypassed the sidebar entry point');

    // Caret click: the sidebar's own menu node, shut until asked for.
    assert.ok(get('sidebar-new-chat-menu').classList.contains('hidden'));
    ctx.onNewChatButton({
        target: { closest: sel => (sel === '#sidebar-new-chat-caret' ? {} : null) },
        stopPropagation() {},
    }, 'sidebar');
    const sidebarMenu = get('sidebar-new-chat-menu');
    assert.equal(sidebarMenu.classList.contains('hidden'), false,
        'the sidebar caret did not open a picker');
    assert.ok(get('new-chat-menu').classList.contains('hidden'),
        'opening the sidebar picker left the panel picker open too');

    // Both menus are one painter's output, so their options cannot drift.
    ctx.paintNewChatMenu(get('new-chat-menu'));
    assert.equal(sidebarMenu.innerHTML, get('new-chat-menu').innerHTML,
        'the two launch controls offer different options');
    assert.match(sidebarMenu.innerHTML, /startSoloChat\('research'\)/);
    assert.match(sidebarMenu.innerHTML, /openTeamChatModal/);
});

test('the sidebar caret rides the presentation switch, the panel caret does not', async () => {
    const { ctx, get } = launchHarness([agent('owner', { is_default: true }), agent('research')]);
    ctx.sidebarLaunchV2 = () => false;
    await ctx.loadAgentCatalog();
    assert.equal(get('new-chat-caret').classList.contains('hidden'), false);
    assert.ok(get('sidebar-new-chat-caret').classList.contains('hidden'),
        'the sidebar caret appeared with the refined layout switched off');

    ctx.sidebarLaunchV2 = () => true;
    await ctx.loadAgentCatalog();
    assert.equal(get('sidebar-new-chat-caret').classList.contains('hidden'), false,
        'the sidebar caret stayed hidden with the refined layout on');
});

test('one usable Agent leaves both launch controls without a caret', async () => {
    const { ctx, get } = launchHarness([agent('owner', { is_default: true })]);
    ctx.sidebarLaunchV2 = () => true;
    await ctx.loadAgentCatalog();
    assert.ok(get('new-chat-caret').classList.contains('hidden'));
    assert.ok(get('sidebar-new-chat-caret').classList.contains('hidden'),
        'the sidebar offered something to choose between that does not exist');
});

test('a coding Agent is marked in the picker with the card’s glyph', () => {
    // The picker rows and the workbench card draw one hint from one function
    // (change simplify-coding-agent-type-hint). A row that spelled the type out
    // while the card showed a glyph would be exactly the drift this pins.
    const { ctx, get } = launchHarness([
        agent('owner', { is_default: true }),
        agent('coder', { agent_type: 'coding' }),
    ]);

    ctx.paintNewChatMenu(get('new-chat-menu'));

    const menu = get('new-chat-menu').innerHTML;
    assert.match(menu, /startSoloChat\('coder'\)/, 'the coding Agent left the picker');
    const hint = codingHint(menu);
    assert.ok(hint, 'the coding row carries no type hint');
    assert.equal(hint, ctx.codingAgentTypeHint(),
        'the picker drew its own hint instead of the shared one');
    assert.match(hint, /fa-terminal/);
    assert.doesNotMatch(hint, />[^<]*[^\s<]/,
        'the picker row still renders the type as visible text');
});
