// Starting a team conversation is a *transaction*, not a hop (change
// refine-sidebar-team-chat-launch, tasks 3.3-3.7).
//
// The load-bearing promise is about ordering: the roster is saved onto a
// session id the page has NOT switched to yet, and only a saved roster moves
// the workbench. Everything that could break that -- a refused write, a lost
// response, a closed picker, a tenant switch, a second click -- is asserted
// against the shipped functions with a controlled transport rather than
// restated as a description.
//
// The visual layout and the real server refusal are owned by
// tests/test_session_team_runtime.py and browser acceptance; this file owns the
// client's half of the contract.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(
    path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');

function section(start, end) {
    const from = source.indexOf(start);
    const to = source.indexOf(end, from);
    assert.ok(from >= 0 && to > from, `section ${start} .. ${end}`);
    return source.slice(from, to);
}

function node(core) {
    const classes = new Set(core ? [] : ['hidden']);
    return {
        innerHTML: '', textContent: '', value: '', disabled: false, dataset: {},
        children: [], parentNode: null,
        classList: {
            add: (...xs) => xs.forEach(x => classes.add(x)),
            remove: (...xs) => xs.forEach(x => classes.delete(x)),
            contains: x => classes.has(x),
            toggle: (x, on) => { if (on) classes.add(x); else classes.delete(x); return on; },
        },
        // Enough of an ancestry answer for the shipped "is this control inside
        // the sidebar?" question; tests append the launch control themselves.
        contains(other) {
            for (let item = other; item; item = item.parentNode) if (item === this) return true;
            return false;
        },
        appendChild(child) { this.children.push(child); child.parentNode = this; return child; },
        focus() { this.focused = true; },
    };
}

const agent = (id, extra = {}) =>
    ({ id, name: id, enabled: true, can_chat: true, is_default: false, ...extra });

/** The team-launch slice, with a scripted transport and a recorded commit. */
function teamHarness(agents, { responder = acceptance() } = {}) {
    const nodes = new Map();
    const calls = { writes: [], commits: 0, leaves: 0, newChat: 0, toasts: [], drawerClosed: 0 };
    const ctx = {
        agentCatalog: agents, chatAgentCatalog: agents,
        // No conversation on screen: every case below picks its own roster, so
        // nothing is preselected behind the assertions.
        activeAgentId: '', defaultAgentId: '', defaultResolution: null,
        sessionId: 'session-on-screen', activeAgent: 'session-on-screen',
        _sessCfg: null, _authEpoch: 7,
        sessionStorage: { getItem: k => (k === 'cow_tenant_id' ? 'tenant-a' : null) },
        writable: [], rosterRevision: '',
        writeScopedPreference(k, v) { ctx.writable.push([k, v]); },
        _wsToast(m) { calls.toasts.push(m); },
        escapeHtml: x => String(x),
        t: x => x,
        generateSessionId: () => 'prepared-session',
        // Models the shipped leave-page guard: `true` means "clean, go ahead"
        // and the callback is NOT run; a dirty editor parks the callback until
        // the user confirms (`ctx.confirmGuard()`).
        wsGuardUnsaved(next) {
            calls.guard = (calls.guard || 0) + 1;
            if (!ctx.editorDirty) return true;
            ctx.pendingGuard = () => { ctx.editorDirty = false; ctx.pendingGuard = null; next(); };
            return false;
        },
        editorDirty: false,
        confirmGuard() { if (ctx.pendingGuard) ctx.pendingGuard(); },
        codingSeam: () => ({
            isCodingAgent: () => false,
            leave: () => { calls.leaves += 1; return true; },
        }),
        renderComposerIdentity() { calls.identity = (calls.identity || 0) + 1; },
        _renderModelChip() {},
        resetWorkspaceToAgentRoot() {},
        closeSidebar() { calls.drawerClosed += 1; },
        commitPreparedSession(id, opts) {
            // The rest of the shipped function repaints the whole workbench, so
            // the harness stops at the one precondition that can *decline* the
            // move -- and calls the shipped gate rather than restating it.
            if (!ctx.newChatLeavesCodingPane()) return false;
            calls.commits += 1;
            calls.committed = { id, opts };
            ctx.sessionId = id;
            return true;
        },
        setTeamMembers(ids, target) {
            // The real write addresses a session explicitly; recording the target
            // is how these tests see that the *prepared* id was used.
            calls.writes.push({ ids: [...ids], target: { ...target } });
            return responder(calls.writes.length, { ids: [...ids], target: { ...target } });
        },
        document: {
            activeElement: null,
            listeners: {},
            getElementById(id) {
                if (!nodes.has(id)) nodes.set(id, node(id === 'user-input'));
                return nodes.get(id);
            },
            addEventListener(type, handler) {
                (this.listeners[type] = this.listeners[type] || []).push(handler);
            },
            removeEventListener(type, handler) {
                this.listeners[type] = (this.listeners[type] || []).filter(item => item !== handler);
            },
            dispatch(type, event) {
                (this.listeners[type] || []).forEach(handler => handler(event));
            },
        },
        window: { innerWidth: 1440 },
    };
    vm.createContext(ctx);
    for (const [start, end] of [
        ['function normalizeAgentCatalogEntry(', 'function loadAgentCatalog()'],
        ['function findAgent(', 'function enabledAgents()'],
        ['function availableChatAgents()', '/* An uploaded avatar'],
        // The launch-control section: `openTeamChatModal` shuts both pickers
        // through it, so the picker cases need the shipped implementation.
        // The type hint rides along: the picker rows draw it, and the point of
        // sharing one implementation is that these rows and the card cannot
        // drift into two different hints.
        ['function codingAgentTypeHint(', 'function agentWorkbenchCardHTML('],
        ['const NEW_CHAT_SURFACES = {', 'function startSoloChat('],
        ['let _teamChatDraft = null;', 'function newChat('],
        ['function newChatLeavesCodingPane()', 'function commitPreparedSession('],
    ]) vm.runInContext(section(start, end), ctx);
    // The shipped avatar helper pulls in branding lookups this harness has no
    // reason to model, and it was redefined by the section above.
    ctx.agentAvatarHTML = a => `<avatar>${a.name}</avatar>`;
    return { ctx, calls, get: id => ctx.document.getElementById(id) };
}

const refusal = (message = 'team refused') => () =>
    Promise.reject(new Error(message));
const acceptance = () => () => Promise.resolve({
    status: 'success', model: { name: 'm' },
    team: { members: [{ id: 'guest', name: 'guest' }] },
});

// ---------------------------------------------------------------- picking

test('the picker offers team candidates, never a coding Agent', () => {
    const { ctx, get } = teamHarness([
        agent('owner', { is_default: true }), agent('guest'),
        agent('coder', { agent_type: 'coding' }),
    ]);
    ctx.openTeamChatModal();
    assert.equal(get('team-chat-modal').classList.contains('hidden'), false);
    const html = get('team-chat-list').innerHTML;
    assert.match(html, /toggleTeamChatPick\('owner'\)/);
    assert.match(html, /toggleTeamChatPick\('guest'\)/);
    assert.doesNotMatch(html, /coder/,
        'a coding Agent was rendered as a team candidate');
});

test('a coding conversation can still open the picker and keeps its session', () => {
    const { ctx, calls } = teamHarness([agent('owner', { is_default: true }), agent('guest')]);
    ctx.codingSeam = () => ({
        isCodingAgent: () => true,
        leave: () => { calls.leaves += 1; return true; },
    });
    ctx.openTeamChatModal();
    assert.ok(ctx.teamChatDraft(), 'a coding session could not open the team picker at all');
    assert.equal(calls.leaves, 0,
        'opening the picker left the coding conversation before anything was saved');
});

test('search filters on name or role without clearing the selection', () => {
    const { ctx, get } = teamHarness([
        agent('owner', { is_default: true }),
        agent('guest', { name: '采购助手', position: '采购比价' }),
    ]);
    ctx.openTeamChatModal();
    ctx.toggleTeamChatPick('owner');
    ctx.toggleTeamChatPick('guest');
    ctx.onTeamChatSearch({ target: { value: '采购' } });
    const html = get('team-chat-list').innerHTML;
    assert.match(html, /toggleTeamChatPick\('guest'\)/);
    assert.doesNotMatch(html, /toggleTeamChatPick\('owner'\)/,
        'the search did not narrow the candidate rows');
    assert.deepEqual([...ctx.teamChatDraft().selected], ['owner', 'guest'],
        'searching dropped a valid selection');
});

test('dropping the default responder leaves the field empty rather than reassigning it', () => {
    const { ctx } = teamHarness([agent('owner', { is_default: true }), agent('guest')]);
    ctx.openTeamChatModal();
    ctx.toggleTeamChatPick('owner');
    ctx.toggleTeamChatPick('guest');
    ctx.setTeamChatOwner('guest');
    ctx.toggleTeamChatPick('guest');
    const draft = ctx.teamChatDraft();
    assert.deepEqual([...draft.selected], ['owner']);
    assert.equal(draft.owner, '',
        'removing the default responder silently promoted someone else');
});

test('two members and a default responder are required before anything is written', () => {
    const { ctx, calls, get } = teamHarness([agent('owner', { is_default: true }), agent('guest')]);
    ctx.openTeamChatModal();
    // Nothing picked at all.
    ctx.startTeamChat();
    assert.equal(calls.writes.length, 0);
    assert.equal(get('team-chat-status').textContent, 'new_team_chat_min');
    // One member and nobody to respond by default.
    ctx.toggleTeamChatPick('owner');
    ctx.toggleTeamChatPick('owner');
    assert.equal(ctx.teamChatDraft().owner, '');
    ctx.startTeamChat();
    assert.equal(calls.writes.length, 0, 'a roster without a responder was submitted');
    assert.equal(get('team-chat-status').textContent, 'new_team_chat_min');
    // Two members, but the responder was removed and never replaced.
    ctx.toggleTeamChatPick('owner');
    ctx.toggleTeamChatPick('guest');
    ctx.toggleTeamChatPick('owner');
    assert.equal(ctx.teamChatDraft().owner, '');
    assert.deepEqual([...ctx.teamChatDraft().selected], ['guest']);
    ctx.startTeamChat();
    assert.equal(calls.writes.length, 0);
});

// ------------------------------------------------------------- closing

test('Escape closes the picker and hands focus back to the launch action', () => {
    const { ctx, get } = teamHarness([agent('owner', { is_default: true }), agent('guest')]);
    const launch = get('sidebar-new-chat');
    ctx.document.activeElement = launch;
    ctx.openTeamChatModal();
    assert.equal(get('team-chat-modal').classList.contains('hidden'), false);

    ctx.document.dispatch('keydown', { key: 'Escape', preventDefault() {}, stopPropagation() {} });

    assert.equal(get('team-chat-modal').classList.contains('hidden'), true,
        'Escape did not close the picker');
    assert.equal(launch.focused, true, 'focus was not handed back to the launch action');
    assert.equal(ctx.teamChatDraft(), null, 'the draft outlived the Escape');
    assert.equal((ctx.document.listeners.keydown || []).length, 0,
        'the Escape listener outlived the picker');
});

test('focus comes back to the launch control, not to the row that closed', () => {
    const { ctx, get } = teamHarness([agent('owner', { is_default: true }), agent('guest')]);
    const launch = get('sidebar-new-chat');
    // The user clicked the team row inside the sidebar's picker. That row is
    // hidden with the menu, so it cannot be the focus target afterwards.
    const row = get('sidebar-new-chat-picked-team');
    row.closest = sel => (sel === '#sidebar-new-chat-menu' ? get('sidebar-new-chat-menu') : null);
    row.getClientRects = () => [];
    ctx.document.activeElement = row;

    ctx.openTeamChatModal();
    assert.equal(get('sidebar-new-chat-menu').classList.contains('hidden'), true,
        'the sidebar picker stayed open behind the modal');

    ctx.document.dispatch('keydown', { key: 'Escape', preventDefault() {}, stopPropagation() {} });
    assert.equal(launch.focused, true,
        'focus went back to a row inside the picker that just closed');
});

test('the session panel hands focus back to its own button, not the composer', () => {
    // Both surfaces are the same control, so the panel needs the same
    // guarantee. It must land on the header button that owns the picker: the
    // composer's plus button carries its own id and is a different control.
    const { ctx, get } = teamHarness([agent('owner', { is_default: true }), agent('guest')]);
    const launch = get('history-new-chat-btn');
    const row = get('new-chat-menu-picked-team');
    row.closest = sel => (sel === '#new-chat-menu' ? get('new-chat-menu') : null);
    row.getClientRects = () => [];
    ctx.document.activeElement = row;

    ctx.openTeamChatModal();
    ctx.document.dispatch('keydown', { key: 'Escape', preventDefault() {}, stopPropagation() {} });
    assert.equal(launch.focused, true, 'focus did not come back to the panel launch control');
    assert.equal(get('new-chat-btn').focused, undefined,
        'focus landed on the composer plus button, which is a different control');
});

test('a picker opened from the phone drawer closes the drawer behind it', () => {
    const { ctx, calls, get } = teamHarness([agent('owner', { is_default: true }), agent('guest')]);
    ctx.window.innerWidth = 390;
    const launch = get('sidebar-new-chat');
    // The control really lives in the drawer, which is what the shipped code
    // asks the DOM. Leaving it up would stack the sheet over the drawer.
    get('sidebar').appendChild(launch);
    ctx.document.activeElement = launch;

    ctx.openTeamChatModal();
    assert.equal(calls.drawerClosed, 1, 'the drawer stayed up behind the picker sheet');
    assert.ok(ctx.teamChatDraft(), 'the picker never opened while the drawer was closing');

    // A picker opened from the main content leaves the drawer alone.
    calls.drawerClosed = 0;
    ctx.closeTeamChatModal({ restoreFocus: false });
    ctx.document.activeElement = get('composer-agent-btn');
    ctx.openTeamChatModal();
    assert.equal(calls.drawerClosed, 0, 'a content-side picker closed the sidebar drawer');
});

test('the Escape key is inert while no picker is open, and cannot eat another dialog', () => {
    const { ctx, get } = teamHarness([agent('owner', { is_default: true }), agent('guest')]);
    let prevented = 0;
    ctx.document.dispatch('keydown', { key: 'Escape', preventDefault() { prevented += 1; } });
    assert.equal(prevented, 0, 'a closed picker swallowed an Escape meant for something else');
    assert.equal(get('team-chat-modal').classList.contains('hidden'), true);
});

test('on a phone the focus returns to the visible drawer toggle, not into the collapsed drawer', () => {
    const { ctx, get } = teamHarness([agent('owner', { is_default: true }), agent('guest')]);
    ctx.window.innerWidth = 390;
    const launch = get('sidebar-new-chat');
    // The control's drawer was already collapsed off-screen before the picker
    // opened (a phone drawer that was shut, or one the picker closed itself).
    launch.getClientRects = () => [{}];
    launch.getBoundingClientRect = () => ({ left: -208, right: -8, top: 60, bottom: 96 });
    ctx.document.activeElement = launch;
    ctx.openTeamChatModal();

    ctx.document.dispatch('keydown', { key: 'Escape', preventDefault() {}, stopPropagation() {} });

    assert.equal(get('menu-toggle').focused, true,
        'focus went to a control the user cannot see');
    assert.equal(launch.focused, undefined, 'focus was parked inside the collapsed drawer');
});

// ------------------------------------------------------------- the write

test('the roster is written to the prepared session, not the one on screen', async () => {
    const { ctx, calls } = teamHarness(
        [agent('owner', { is_default: true }), agent('guest')], { responder: acceptance() });
    ctx.openTeamChatModal();
    ctx.toggleTeamChatPick('owner');
    ctx.toggleTeamChatPick('guest');
    ctx.startTeamChat();
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(calls.writes.length, 1);
    assert.deepEqual(calls.writes[0].ids, ['guest'], 'the owner was submitted as a member');
    assert.equal(calls.writes[0].target.sessionId, 'prepared-session');
    assert.equal(calls.writes[0].target.agentId, 'owner');
    assert.notEqual(calls.writes[0].target.sessionId, 'session-on-screen',
        'the prepared write addressed the conversation the user was looking at');
    assert.equal(calls.commits, 1, 'a committed roster did not move the workbench');
    assert.equal(calls.committed.id, 'prepared-session',
        'the commit generated a second session id instead of reusing the prepared one');
});

test('a refused write leaves the conversation, the draft and the picker alone', async () => {
    const { ctx, calls, get } = teamHarness(
        [agent('owner', { is_default: true }), agent('guest')], { responder: refusal('no such member') });
    ctx.openTeamChatModal();
    ctx.toggleTeamChatPick('owner');
    ctx.toggleTeamChatPick('guest');
    ctx.startTeamChat();
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(calls.commits, 0, 'a refused roster still moved the workbench');
    assert.equal(ctx.sessionId, 'session-on-screen');
    assert.equal(calls.leaves, 0, 'a refused roster still left the coding conversation');
    assert.equal(get('team-chat-modal').classList.contains('hidden'), false,
        'the picker closed on a refusal, taking the user\'s choice with it');
    assert.deepEqual([...ctx.teamChatDraft().selected], ['owner', 'guest'],
        'a refusal discarded the roster the user picked');
    assert.equal(ctx.teamChatDraft().phase, 'editing', 'the picker was left un-retryable');
    assert.equal(get('team-chat-status').textContent, 'no such member',
        'the refusal was not reported');
});

test('a lost response after the user cancels never commits', async () => {
    let settle;
    const { ctx, calls } = teamHarness(
        [agent('owner', { is_default: true }), agent('guest')],
        { responder: () => new Promise(resolve => { settle = resolve; }) });
    ctx.openTeamChatModal();
    ctx.toggleTeamChatPick('owner');
    ctx.toggleTeamChatPick('guest');
    ctx.startTeamChat();
    assert.equal(calls.writes.length, 1);
    // The user gives up while the write is in flight.
    ctx.closeTeamChatModal();
    settle({ status: 'success', team: { members: [] } });
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(calls.commits, 0, 'a cancelled attempt committed a session anyway');
    assert.equal(ctx.sessionId, 'session-on-screen');
    assert.ok(ctx.teamChatDraft() === null, 'the draft outlived the closed picker');
});

test('a second click while saving cannot write a second roster', async () => {
    let settle;
    const { ctx, calls } = teamHarness(
        [agent('owner', { is_default: true }), agent('guest')],
        { responder: () => new Promise(resolve => { settle = resolve; }) });
    ctx.openTeamChatModal();
    ctx.toggleTeamChatPick('owner');
    ctx.toggleTeamChatPick('guest');
    ctx.startTeamChat();
    ctx.startTeamChat();
    ctx.startTeamChat();
    assert.equal(calls.writes.length, 1, 'a double click produced more than one team');
    settle({ status: 'success', team: { members: [] } });
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(calls.commits, 1);
});

test('a tenant switch while saving drops the stale commit', async () => {
    let settle;
    const { ctx, calls } = teamHarness(
        [agent('owner', { is_default: true }), agent('guest')],
        { responder: () => new Promise(resolve => { settle = resolve; }) });
    ctx.openTeamChatModal();
    ctx.toggleTeamChatPick('owner');
    ctx.toggleTeamChatPick('guest');
    ctx.startTeamChat();
    // The account moved to another tenant while the write was in flight.
    ctx.sessionStorage = { getItem: k => (k === 'cow_tenant_id' ? 'tenant-b' : null) };
    settle({ status: 'success', team: { members: [] } });
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(calls.commits, 0, 'a roster saved under one tenant opened in another');
    assert.equal(ctx.sessionId, 'session-on-screen');
});

test('a member that stopped being eligible is refused before the write', () => {
    const { ctx, calls, get } = teamHarness(
        [agent('owner', { is_default: true }), agent('guest')], { responder: acceptance() });
    ctx.openTeamChatModal();
    ctx.toggleTeamChatPick('owner');
    ctx.toggleTeamChatPick('guest');
    // The use-range read lands (or is revoked) after the user picked.
    ctx.chatAgentCatalog = [agent('owner', { is_default: true })];
    ctx.startTeamChat();
    assert.equal(calls.writes.length, 0, 'an ineligible member was submitted anyway');
    assert.equal(get('team-chat-status').textContent, 'team_chat_stale');
});

test('the saved roster is what the composer renders', async () => {
    const { ctx, calls } = teamHarness(
        [agent('owner', { is_default: true }), agent('guest')], { responder: acceptance() });
    ctx.openTeamChatModal();
    ctx.toggleTeamChatPick('owner');
    ctx.toggleTeamChatPick('guest');
    ctx.startTeamChat();
    await Promise.resolve();
    await Promise.resolve();
    assert.deepEqual(ctx._sessCfg.team.members.map(m => m.id), ['guest'],
        'the composer kept the locally guessed roster instead of the saved one');
    assert.ok(ctx.writable.some(([k]) => k === 'cow_active_agent'),
        'the committed conversation did not record its owner');
});

test('an arbitrary member set cannot start a team without a default responder', () => {
    const { ctx, calls } = teamHarness([agent('a'), agent('b'), agent('c')]);
    ctx.defaultAgentId = '';           // no unified default resolution either
    ctx.activeAgentId = '';
    ctx.defaultResolution = null;
    ctx.openTeamChatModal();
    assert.deepEqual([...ctx.teamChatDraft().selected], [],
        'an unresolvable roster still preselected something');
    ctx.toggleTeamChatPick('a');
    ctx.toggleTeamChatPick('b');
    assert.equal(ctx.teamChatDraft().owner, 'a', 'the first pick did not answer "who responds"');
    ctx.startTeamChat();
    assert.equal(calls.writes.length, 1);
});

// ------------------------------------------------------- leave protection

test('edit made while the roster saves gets its say before the view moves', async () => {
    let settle;
    const { ctx, calls } = teamHarness(
        [agent('owner', { is_default: true }), agent('guest')],
        { responder: () => new Promise(resolve => { settle = resolve; }) });
    ctx.openTeamChatModal();
    ctx.toggleTeamChatPick('owner');
    ctx.toggleTeamChatPick('guest');
    ctx.startTeamChat();
    const askedBefore = calls.guard;
    // The user typed into the workspace editor while the write was in flight.
    ctx.editorDirty = true;
    settle({ status: 'success', team: { members: [] } });
    await Promise.resolve();
    await Promise.resolve();
    assert.ok(calls.guard > askedBefore, 'the committed view skipped the leave-page guard');
    assert.equal(calls.commits, 0, 'the unsaved editor was discarded without asking');
    assert.equal(ctx.sessionId, 'session-on-screen');
    // Confirming the dialog completes the move instead of dropping the work.
    ctx.confirmGuard();
    assert.equal(calls.commits, 1);
    assert.equal(ctx.sessionId, 'prepared-session');
});

test('declining the coding leave guard keeps the coding conversation', async () => {
    const { ctx, calls } = teamHarness(
        [agent('owner', { is_default: true }), agent('guest')], { responder: acceptance() });
    // The coding pane has its own "unsaved" prompt and the user says no.
    ctx.codingSeam = () => ({
        isCodingAgent: () => true,
        leave: () => { calls.leaves += 1; return false; },
    });
    ctx.openTeamChatModal();
    ctx.toggleTeamChatPick('owner');
    ctx.toggleTeamChatPick('guest');
    ctx.startTeamChat();
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(calls.leaves, 1, 'the coding surface was never asked before leaving');
    assert.equal(calls.commits, 0,
        'declining the coding leave prompt still opened the team conversation');
    assert.equal(ctx.sessionId, 'session-on-screen');
});
