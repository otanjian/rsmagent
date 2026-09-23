// Refined workbench sidebar (change refine-sidebar-team-chat-launch, tasks
// 4.1-4.4): the temporary presentation switch `workbench_sidebar_launch_v2`
// gates *layout only* — the second launch action, the navigation order and the
// five-row recent preview. These cases run the shipped functions against a
// small DOM and pin:
//
//   * the switch defaults to off and only an explicit `1` turns it on;
//   * off keeps the old single launch action, the markup order and the old
//     preview limit, so turning the switch back off is a real rollback;
//   * on reveals the second action as the primary's sibling, reorders the
//     workbench navigation without dropping unknown entries, and tightens the
//     preview to five rows while 查看全部 stays available for a short or empty
//     list;
//   * the recent row's type marker is derived from the persisted roster and the
//     owner badge — never from the title — and survives an in-place rename;
//   * the full history row reads the same marker, so the two surfaces agree.
//
// The switch never gates a team rule; the candidate projection and the
// server-side roster rejection are covered by test_team_chat_launch_frontend.cjs
// and the Python suites, and are asserted absent from the switch here.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');
const chatHtml = fs.readFileSync(path.join(__dirname, '../channel/web/chat.html'), 'utf8');

function section(from, to) {
    const start = source.indexOf(from);
    const end = source.indexOf(to, start);
    assert.ok(start >= 0 && end > start, `Missing section ${from} .. ${to} in console.js`);
    return source.slice(start, end);
}

// The switch itself, the promised view order and the two entry points.
const launchSource = section('function sidebarLaunchV2() {', '// Keep closed groups out of the keyboard focus order');
// The one launch control the sidebar shares with the session panel: its surface
// table, its picker painter and the visibility rule for both carets.
const launchControlSource = section('const NEW_CHAT_SURFACES = {', 'function startSoloChat(');
// The sidebar preview: the type marker, the title writer, the limit and the
// renderer that draws the rows.
const sidebarSource = section('// === SIDEBAR_RECENT_BEGIN ===', '// Never run sidebar init inline');

/* --- a very small DOM -------------------------------------------------------
   Enough of the real thing for the shipped code: children with parent links,
   class names, attributes mirrored into `dataset` the way the browser does
   (`data-view` <-> `dataset.view`), and a descendant selector engine that
   handles the compound selectors the sidebar actually uses. */
function element(tagName = 'div') {
    const classes = new Set();
    let text = '', html = '';
    const el = {
        tagName: tagName.toUpperCase(), id: '', value: '', dataset: {}, attrs: {},
        handlers: {}, children: [], parentNode: null, disabled: false, hidden: false, type: '',
        get firstChild() { return this.children[0] || null; },
        get lastChild() { return this.children[this.children.length - 1] || null; },
        get textContent() { return text; },
        set textContent(value) { text = String(value); html = ''; this.children.forEach(c => { c.parentNode = null; }); this.children = []; },
        get innerHTML() { return html; },
        set innerHTML(value) { html = String(value); text = ''; this.children.forEach(c => { c.parentNode = null; }); this.children = []; },
        get className() { return [...classes].join(' '); },
        set className(value) { classes.clear(); String(value).split(/\s+/).filter(Boolean).forEach(v => classes.add(v)); },
        classList: {
            add: (...names) => names.forEach(name => classes.add(name)),
            remove: (...names) => names.forEach(name => classes.delete(name)),
            contains: name => classes.has(name),
            toggle(name, force) {
                const add = force === undefined ? !classes.has(name) : force;
                if (add) classes.add(name); else classes.delete(name);
                return add;
            },
        },
        setAttribute(key, value) {
            this.attrs[key] = String(value);
            if (key.startsWith('data-')) {
                const camel = key.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
                this.dataset[camel] = String(value);
            }
        },
        getAttribute(key) { return this.attrs[key] ?? null; },
        removeAttribute(key) { delete this.attrs[key]; },
        addEventListener(name, fn) { this.handlers[name] = fn; },
        appendChild(child) {
            if (child.parentNode) child.parentNode.children = child.parentNode.children.filter(el => el !== child);
            this.children.push(child); child.parentNode = this; return child;
        },
        insertBefore(child, ref) {
            if (child.parentNode) child.parentNode.children = child.parentNode.children.filter(el => el !== child);
            const idx = ref ? this.children.indexOf(ref) : -1;
            if (idx < 0) this.children.push(child); else this.children.splice(idx, 0, child);
            child.parentNode = this; return child;
        },
        removeChild(child) {
            this.children = this.children.filter(el => el !== child); child.parentNode = null; return child;
        },
        remove() { if (this.parentNode) this.parentNode.removeChild(this); },
        focus() { this.focused = true; },
        querySelector(selector) { return querySelectorAll(this, selector)[0] || null; },
        querySelectorAll(selector) { return querySelectorAll(this, selector); },
    };
    return el;
}

function descendants(node) {
    return node.children.flatMap(child => [child, ...descendants(child)]);
}

function attributeValue(el, name) {
    if (Object.prototype.hasOwnProperty.call(el.attrs, name)) return el.attrs[name];
    if (name.startsWith('data-')) {
        const camel = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
        if (el.dataset[camel] !== undefined) return String(el.dataset[camel]);
    }
    return null;
}

function matchesPart(el, part) {
    const tokens = part.match(/\[[^\]]+\]|[.#]?[A-Za-z0-9_-]+/g) || [];
    return tokens.every(token => {
        if (token.startsWith('[')) {
            const body = token.slice(1, -1);
            const eq = body.indexOf('=');
            if (eq < 0) return attributeValue(el, body) !== null;
            const name = body.slice(0, eq).trim();
            const want = body.slice(eq + 1).trim().replace(/^["']|["']$/g, '');
            return attributeValue(el, name) === want;
        }
        if (token.startsWith('#')) return el.id === token.slice(1);
        if (token.startsWith('.')) return el.classList.contains(token.slice(1));
        return el.tagName.toLowerCase() === token.toLowerCase();
    });
}

// Descendant-combinator only, which is all the sidebar selectors use.
function querySelectorAll(root, selector) {
    let current = [root];
    for (const part of selector.trim().split(/\s+/)) {
        const next = [];
        current.forEach(node => descendants(node).forEach(child => {
            if (matchesPart(child, part)) next.push(child);
        }));
        current = next;
    }
    return current;
}

/* --- the fixture -----------------------------------------------------------
   The real sidebar skeleton: brand, launch actions, one scrolling navigation
   region that also hosts the recent preview, and the account card outside it.
   `navItems` holds the workbench entries in their markup order. */
const VIEW_LABELS = ['chat', 'tasks', 'knowledge', 'scenes', 'agent-workbench'];

function buildFixture() {
    const root = element('html');
    const body = element('body');
    const sidebar = element('aside');
    sidebar.id = 'sidebar';
    const brand = element('div');
    brand.className = 'sidebar-brand';
    const primary = element('button');
    primary.id = 'sidebar-new-chat';
    primary.className = 'sidebar-new-chat';
    const caret = element('i');
    caret.id = 'sidebar-new-chat-caret';
    caret.className = 'new-chat-caret hidden';
    const menu = element('div');
    menu.id = 'sidebar-new-chat-menu';
    menu.className = 'new-chat-menu new-chat-menu-sidebar hidden';
    const wrap = element('div');
    wrap.className = 'sidebar-new-chat-wrap';
    wrap.appendChild(primary);
    wrap.appendChild(caret);
    wrap.appendChild(menu);
    const nav = element('nav');
    nav.id = 'sidebar-nav';
    const workbench = element('div');
    workbench.setAttribute('data-nav-shell', 'workbench');
    const group = element('div');
    group.className = 'menu-group open';
    group.setAttribute('data-group', 'chat');
    const items = element('div');
    items.className = 'menu-group-items';
    const navRows = VIEW_LABELS.map(view => {
        const item = element('a');
        item.className = 'sidebar-item';
        item.setAttribute('data-view', view);
        items.appendChild(item);
        return item;
    });
    // An entry the promised order does not know about: it must keep working.
    const extra = element('a');
    extra.className = 'sidebar-item';
    extra.setAttribute('data-view', 'daily-digest');
    items.appendChild(extra);
    group.appendChild(items);
    workbench.appendChild(group);
    nav.appendChild(workbench);
    const recent = element('div');
    recent.id = 'sidebar-recent';
    recent.className = 'sidebar-recent open';
    const recentList = element('div');
    recentList.id = 'sidebar-recent-list';
    const more = element('button');
    more.id = 'sidebar-recent-more';
    more.className = 'sidebar-recent-more hidden';
    recent.appendChild(recentList);
    recent.appendChild(more);
    nav.appendChild(recent);
    const footer = element('div');
    footer.id = 'sidebar-account-footer';
    sidebar.appendChild(brand);
    sidebar.appendChild(wrap);
    sidebar.appendChild(nav);
    sidebar.appendChild(footer);
    body.appendChild(sidebar);
    root.appendChild(body);
    return { root, body, sidebar, brand, primary, caret, menu, wrap, nav, items, navRows, extra, recent, recentList, more, footer };
}

function setup(options = {}) {
    const fixture = buildFixture();
    const ctx = vm.createContext({
        console, Date, Intl, URLSearchParams, AbortController,
        activeAgentId: 'owner', sessionId: 'current',
        location: { pathname: '/chat' },
        _navAreaFromPath: () => 'workbench',
        window: { innerWidth: options.innerWidth || 1440, __COW_WORKBENCH_SIDEBAR_LAUNCH_V2__: options.flag },
        document: {
            body: fixture.body,
            getElementById: id => querySelectorAll(fixture.root, '#' + id)[0] || null,
            querySelector: sel => querySelectorAll(fixture.root, sel)[0] || null,
            querySelectorAll: sel => querySelectorAll(fixture.root, sel),
            createElement: element,
        },
        localStorage: { getItem: () => null, setItem() {} },
        t: key => key,
        escapeHtml: value => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
        agentAvatarHTML: (agent, size) =>
            `<img class="agent-avatar agent-avatar-${size || 32}" alt="" data-agent="${agent && agent.id}">`,
        // The launch control's picker is drawn from the use range; the sidebar
        // cases only need the rows to exist.
        availableChatAgents: () => options.agents || [
            { id: 'owner', name: '经营助手' }, { id: 'ops', name: '运营助手' },
        ],
        // Whether there is anything to choose between is the roster's answer,
        // owned by the shipped `multiAgentMode`; the switch decides separately.
        multiAgentMode: () => options.multiAgents !== false,
        startSidebarNewChat: () => { ctx.__sidebarStarted = true; },
        _historyTimeLabel: () => ({ text: 'now', full: 'now' }),
        switchSession() {}, _openSessionActionMenu() {}, renameSidebarSession() {}, archiveSidebarSession() {},
        _wsToast() {}, navigateTo() {}, openArchivedSessionsModal() {},
        _viewNavDenied: () => false,
        requestAnimationFrame: fn => fn(),
        setTimeout: fn => { fn(); return 0; }, clearTimeout() {},
        fetch: async () => ({ ok: true, status: 200, json: async () => ({ status: 'success', sessions: [], total: 0, has_more: false }) }),
        closeSidebar: () => { ctx.__drawerClosed = true; },
        openTeamChatModal: () => { ctx.__pickerOpened = true; },
    });
    ctx.__pickerOpened = false;
    ctx.__drawerClosed = false;
    const run = code => vm.runInContext(code, ctx);
    run(launchSource);
    run(launchControlSource);
    run(sidebarSource);
    // Script-level `let`s are invisible to property assignment from Node.
    const setItems = items => run(`_sidebarRecentItems = ${JSON.stringify(items)};`);
    const render = () => run('renderSidebarRecentSessions();');
    const rowFor = id => fixture.recentList.children
        .find(row => row.dataset.sessionId === id);
    return { ctx, run, fixture, setItems, render, rowFor,
        order: () => fixture.items.children.map(el => el.dataset.view) };
}

const teamSession = (extra = {}) => ({
    session_id: 'group-1', title: '月度经营分析', last_active: 1700000000, pinned: 0,
    agent: { id: 'owner', name: '经营助手' },
    participants: [{ id: 'owner', name: '经营助手' }, { id: 'ops', name: '运营助手' },
        { id: 'finance', name: '财务助手' }, { id: 'legal', name: '法务助手' }],
    project: null, ...extra,
});

/* --- the switch ---------------------------------------------------------- */

test('the refined sidebar is off unless the flag is exactly 1', () => {
    const values = [undefined, '', '0', 'off', 'true', 'yes', '1.0', '2', '  '];
    values.forEach(value => {
        const h = setup({ flag: value });
        assert.equal(h.run('sidebarLaunchV2()'), false, `flag=${JSON.stringify(value)} must stay off`);
    });
    ['1', ' 1 '].forEach(value => {
        const h = setup({ flag: value });
        assert.equal(h.run('sidebarLaunchV2()'), true, `flag=${JSON.stringify(value)} must be on`);
    });
});

test('offscreen rendering cannot throw: the switch is inert without a document', () => {
    const ctx = vm.createContext({ window: { __COW_WORKBENCH_SIDEBAR_LAUNCH_V2__: '1' } });
    vm.runInContext(launchSource, ctx);
    assert.equal(vm.runInContext('applySidebarLaunchV2()', ctx), true);
});

test('the switch gates layout only, never a team rule', () => {
    // The switch must not be consulted by the candidate projection or by the
    // roster writer: a rollback cannot re-open a coding Agent to a team.
    ['teamCandidateAgents', 'agentIsCoding', 'setTeamMembers', 'sessionRoster',
        'prepareTeamChatSession', 'teamChatRosterProblem'].forEach(name => {
        const start = source.indexOf(`function ${name}(`);
        assert.ok(start >= 0, `missing ${name}`);
        const body = source.slice(start, source.indexOf('\n}', start));
        assert.ok(!/sidebarLaunchV2/.test(body), `${name} must not depend on the sidebar switch`);
    });
});

/* --- quick launch + navigation order ------------------------------------ */

test('the launch control carries no picker while the switch is off', () => {
    const h = setup({ flag: '0' });
    assert.equal(h.fixture.sidebar.classList.contains('sidebar-launch-v2'), false);
    h.run('applySidebarLaunchV2()');
    assert.equal(h.fixture.caret.classList.contains('hidden'), true,
        'the old sidebar offered a picker it does not have');
    assert.equal(h.fixture.menu.classList.contains('hidden'), true);
    assert.deepEqual(h.order(), [...VIEW_LABELS, 'daily-digest'], 'markup order is untouched');
});

test('the one launch control sits above the navigation when on', () => {
    const h = setup({ flag: '1' });
    h.run('applySidebarLaunchV2()');
    assert.equal(h.fixture.sidebar.classList.contains('sidebar-launch-v2'), true);
    assert.equal(h.fixture.caret.classList.contains('hidden'), false);
    const zone = h.fixture.sidebar.children;
    assert.deepEqual(zone.slice(0, 3).map(el => el.id || el.className),
        ['sidebar-brand', 'sidebar-new-chat-wrap', 'sidebar-nav'],
        'brand and the launch control stay above the single scroll region');
    assert.equal(zone[zone.length - 1].id, 'sidebar-account-footer',
        'the account card stays last, outside the scroll region');
});

test('there is exactly one launch control: no separate team button', () => {
    // The team entry lives in the picker, not in a second sidebar button. A
    // second button is what drifted from the history page's menu.
    const h = setup({ flag: '1' });
    h.run('applySidebarLaunchV2()');
    assert.equal(h.fixture.wrap.children.length, 3,
        'the launch block holds a button, its caret and one picker');
    assert.equal(h.fixture.primary.children.includes(h.fixture.caret), false,
        'the fixture is malformed');
    assert.equal(chatHtml.includes('sidebar-new-team-chat'), false,
        'a separate team launch button came back');
});

test('the launch controls keep distinct ids on the page', () => {
    // The panel's control id used to be borrowed from the composer's plus
    // button, so focus restore sent the user to the composer instead of the
    // header. Every id on the page stays unique.
    const ids = [...chatHtml.matchAll(/\sid="([^"]+)"/g)].map(m => m[1]);
    const seen = new Set();
    const duplicates = ids.filter(id => (seen.has(id) ? true : (seen.add(id), false)));
    assert.deepEqual(duplicates, [], 'chat.html has a duplicate id');
    assert.ok(ids.includes('history-new-chat-btn'),
        'the panel launch control has no id of its own');
    assert.equal(ids.filter(id => id === 'new-chat-btn').length, 1,
        'the composer plus button no longer owns new-chat-btn alone');
});

test('the workbench navigation takes the promised order and keeps unknown entries', () => {
    const h = setup({ flag: '1' });
    h.run('applySidebarLaunchV2()');
    const order = h.order();
    assert.deepEqual(order.filter(view => view !== 'daily-digest'),
        ['chat', 'agent-workbench', 'scenes', 'knowledge', 'tasks']);
    assert.ok(order.includes('daily-digest'),
        'an entry the promised order does not know keeps working rather than disappearing');
});

test('the body starts a chat and the caret opens the shared picker', () => {
    const h = setup({ flag: '1' });
    h.run('applySidebarLaunchV2()');

    // Body click: the sidebar's own entry point (guard, focus, navigation).
    h.run('onNewChatButton({ target: { closest: () => null }, stopPropagation() {} }, "sidebar")');
    assert.equal(h.ctx.__sidebarStarted, true, 'the sidebar body did not start a chat');
    assert.equal(h.fixture.menu.classList.contains('hidden'), true,
        'the body click forced the picker open');

    // Caret click: the same options the session panel draws.
    const caretEvent = '{ target: { closest: sel => (sel === "#sidebar-new-chat-caret" ? {} : null) },'
        + ' stopPropagation() {} }';
    h.run(`onNewChatButton(${caretEvent}, "sidebar")`);
    assert.equal(h.fixture.menu.classList.contains('hidden'), false,
        'the caret did not open the picker');
    assert.match(h.fixture.menu.innerHTML, /startSoloChat\('owner'\)/);
    assert.match(h.fixture.menu.innerHTML, /openTeamChatModal/,
        'the picker lost the multi-Agent entry the history page offers');
    h.run(`onNewChatButton(${caretEvent}, "sidebar")`);
    assert.equal(h.fixture.menu.classList.contains('hidden'), true,
        'a second caret click did not close the picker');
});

test('a roster of one leaves neither launch control with a caret', () => {
    const h = setup({ flag: '1', multiAgents: false });
    h.run('applySidebarLaunchV2()');
    assert.equal(h.fixture.caret.classList.contains('hidden'), true,
        'the sidebar offered something to choose between that does not exist');
});

/* --- the recent preview -------------------------------------------------- */

test('the preview holds five rows when on and ten when off', () => {
    const many = Array.from({ length: 12 }, (_, i) => ({
        session_id: 's' + i, title: 'S' + i, last_active: 1700000000 - i, pinned: 0,
        agent: { id: 'owner', name: '经营助手' }, project: null,
    }));
    const on = setup({ flag: '1' });
    assert.equal(on.run('sidebarRecentLimitCount()'), 5);
    on.setItems(many);
    on.render();
    assert.equal(on.fixture.recentList.children.length, 5, 'only the first five of the ordering');

    const off = setup({ flag: '0' });
    assert.equal(off.run('sidebarRecentLimitCount()'), 10);
    off.setItems(many);
    off.render();
    assert.equal(off.fixture.recentList.children.length, 10, 'the old limit is preserved');
});

test('查看全部 is always reachable in the refined preview, including short and empty lists', () => {
    [0, 2].forEach(count => {
        const h = setup({ flag: '1' });
        h.setItems(Array.from({ length: count }, (_, i) => ({
            session_id: 's' + i, title: 'S' + i, last_active: 1700000000, pinned: 0,
            agent: { id: 'owner', name: '助手' }, project: null,
        })));
        h.render();
        assert.equal(h.fixture.more.classList.contains('hidden'), false,
            `view all must stay available with ${count} rows`);
    });
});

test('the old layout keeps its original short-list behaviour', () => {
    const h = setup({ flag: '0' });
    h.setItems([]);
    h.render();
    assert.equal(h.fixture.more.classList.contains('hidden'), true,
        'off is a real rollback: nothing to expand means no entry');
});

/* --- the type marker, on both surfaces ---------------------------------- */

test('a recent row marks a team from the persisted roster, not from its title', () => {
    const h = setup({ flag: '1' });
    h.setItems([
        teamSession(),
        // A solo conversation whose title reads like a team: it must stay plain.
        { session_id: 'solo-1', title: '月度经营分析 多人协作', last_active: 1, pinned: 0,
            agent: { id: 'owner', name: '经营助手' }, project: null },
    ]);
    h.render();
    const row = h.rowFor('group-1');
    const marker = row.querySelectorAll('.sidebar-recent-type')[0];
    assert.ok(marker, 'the team row carries a marker');
    assert.match(marker.innerHTML, /session-faces/, 'a team reads as faces');
    assert.equal(marker.title, '经营助手、运营助手、财务助手、法务助手');
    const title = row.querySelectorAll('.sidebar-recent-item')[0];
    assert.equal(title.textContent, '月度经营分析', 'the title text is untouched');
    assert.equal(title.children[0], marker, 'the marker rides before the title');

    const solo = h.rowFor('solo-1').querySelectorAll('.sidebar-recent-type')[0];
    assert.match(solo.innerHTML, /fa-message/, 'a single-Agent conversation stays a plain chat row');
    assert.doesNotMatch(solo.innerHTML, /session-faces/, 'the title never decides the type');
    assert.equal(solo.title, undefined, 'a solo row has no member summary to announce');
});

test('a coding conversation keeps its own identity in the preview', () => {
    const h = setup({ flag: '1' });
    h.setItems([{ session_id: 'code-1', title: 'SAP 数据查询', last_active: 2, pinned: 0,
        agent: { id: 'coder', name: '编码助手', agent_type: 'coding' }, project: null }]);
    h.render();
    const marker = h.rowFor('code-1').querySelectorAll('.sidebar-recent-type')[0];
    assert.match(marker.innerHTML, /fa-code/, 'coding keeps its coding identity');
});

test('a pinned solo conversation keeps the pin marker', () => {
    const h = setup({ flag: '1' });
    h.setItems([{ session_id: 'pin-1', title: '置顶会话', last_active: 3, pinned: 1,
        agent: { id: 'owner', name: '助手' }, project: null }]);
    h.render();
    const marker = h.rowFor('pin-1').querySelectorAll('.sidebar-recent-type')[0];
    assert.match(marker.innerHTML, /fa-thumbtack/);
});

test('an in-place rename rewrites the title and keeps the marker', () => {
    const h = setup({ flag: '1' });
    h.setItems([teamSession()]);
    h.render();
    const button = h.rowFor('group-1').querySelectorAll('.sidebar-recent-item')[0];
    h.run('setSidebarRowTitle(document.getElementById("sidebar-recent-list")'
        + '.querySelectorAll(".sidebar-recent-item")[0], "新标题");');
    assert.equal(button.textContent, '新标题');
    assert.equal(button.title, '新标题', 'the tooltip follows the new title');
    const marker = button.querySelectorAll('.sidebar-recent-type')[0];
    assert.ok(marker, 'the marker survives the edit');
    assert.match(marker.innerHTML, /session-faces/);
    assert.equal(button.children[0], marker);
});

test('the marker helper states what a conversation is, from the roster', () => {
    const h = setup({ flag: '1' });
    const team = h.run('sessionTypeMarker(' + JSON.stringify(teamSession()) + ')');
    assert.match(team.html, /session-faces/);
    assert.match(team.html, /role="img"/, 'the stack is one image to assistive tech');
    assert.match(team.html, /aria-label="经营助手、运营助手、财务助手、法务助手"/,
        'the member summary is announced, not only hovered');
    assert.equal(team.summary, '经营助手、运营助手、财务助手、法务助手');

    const solo = h.run('sessionTypeMarker(' + JSON.stringify({ session_id: 'solo', title: '月度经营分析',
        agent: { id: 'owner', name: '助手' }, project: null }) + ')');
    assert.match(solo.html, /fa-message/);
    assert.doesNotMatch(solo.html, /session-faces/, 'a solo chat is never marked as a team');

    const coding = h.run('sessionTypeMarker(' + JSON.stringify({ session_id: 'code', title: 'T',
        agent: { id: 'coder', name: '编码助手', agent_type: 'coding' }, project: null }) + ')');
    assert.match(coding.html, /fa-code/, 'coding keeps its coding identity');

    const pinned = h.run('sessionTypeMarker(' + JSON.stringify({ session_id: 'pin', title: 'T', pinned: 1,
        agent: { id: 'owner', name: '助手' }, project: null }) + ')');
    assert.match(pinned.html, /fa-thumbtack/);
});

test('both surfaces derive their marker from the one shared helper', () => {
    // The preview renders the helper into the row button, and the full history
    // row interpolates the same helper, so the two cannot disagree about what a
    // conversation is (spec: 会话类型标识与成员恢复一致).
    const previewRow = source.slice(source.indexOf('function setSidebarRowType('),
        source.indexOf('function setSidebarRowTitle('));
    assert.match(previewRow, /sessionTypeMarker\(s\)/);
    const historyRow = source.slice(source.indexOf('function _sessionItemEl('),
        source.indexOf('function _sortSessionItems('));
    assert.match(historyRow, /sessionTypeMarker\(s\)\.html/);
    assert.doesNotMatch(historyRow, /s\.title[^\n]*session-faces/,
        'the title must never be read to decide the type');
});

/* --- structure (task 4.2) ------------------------------------------------ */

test('the sidebar keeps one middle scroll region with the zones in order', () => {
    const brand = chatHtml.indexOf('class="sidebar-brand"');
    const launch = chatHtml.indexOf('class="sidebar-new-chat-wrap"');
    const primary = chatHtml.indexOf('id="sidebar-new-chat"');
    const caret = chatHtml.indexOf('id="sidebar-new-chat-caret"');
    const menu = chatHtml.indexOf('id="sidebar-new-chat-menu"');
    const nav = chatHtml.indexOf('id="sidebar-nav"');
    const recent = chatHtml.indexOf('id="sidebar-recent"');
    const footer = chatHtml.indexOf('id="sidebar-account-footer"');
    assert.ok(brand < launch && launch < primary && primary < caret && caret < menu && menu < nav
        && nav < recent && recent < footer,
        'brand, launch control (button, caret, picker), navigation, recent preview and account card keep their order');

    // The recent preview lives inside the navigation, which is the only
    // scrolling container: the sidebar itself must not scroll a second region.
    const navBlock = chatHtml.slice(nav, chatHtml.indexOf('</nav>', nav));
    assert.ok(navBlock.includes('id="sidebar-recent"'), 'the preview shares the scrolling region');
    assert.match(navBlock, /class="[^"]*flex-1 min-h-0 overflow-y-auto/);

    const sidebarBlock = chatHtml.slice(chatHtml.indexOf('<aside id="sidebar"'), chatHtml.indexOf('</aside>'));
    const scrollers = sidebarBlock.match(/overflow-y-auto/g) || [];
    assert.equal(scrollers.length, 1, 'the refined sidebar scrolls in exactly one place');
    const footerBlock = sidebarBlock.slice(sidebarBlock.indexOf('id="sidebar-account-footer"'));
    assert.doesNotMatch(footerBlock, /overflow-y-auto/, 'the account card is not inside the scroller');
});

test('the launch control and its picker carry their own styling hooks', () => {
    const appearance = fs.readFileSync(path.join(__dirname, '../channel/web/static/css/appearance.css'), 'utf8');
    const consoleCss = fs.readFileSync(path.join(__dirname, '../channel/web/static/css/console.css'), 'utf8');

    // One control: the section spacing moved to the wrapper so the picker can
    // anchor to the button instead of to the button's margin box.
    assert.match(appearance, /#sidebar \.sidebar-new-chat-wrap \{ margin: 8px 18px 18px; \}/,
        'the launch block spacing rides the palette hook');
    assert.doesNotMatch(appearance, /sidebar-new-chat-team/, 'a rule for the removed button survived');
    assert.match(consoleCss, /\.sidebar-new-chat-wrap \{ position: relative; display: flex; flex-direction: column; \}/,
        'the picker has no positioning context, or the button lost the sidebar-width stretch it had as a direct flex item');
    assert.match(consoleCss, /\.sidebar-new-chat-wrap \{ [^}]*flex-direction: column/,
        'the launch block is not a column, so the button will not fill its width');
    assert.match(consoleCss, /#sidebar \.sidebar-new-chat \.new-chat-caret \{/,
        'the caret is in the flow, which would push the centred label aside');
    assert.match(consoleCss, /\.new-chat-menu-sidebar \{/,
        'the sidebar picker has no anchor rule of its own');

    // The 72px rail cannot host the picker under the button: it opens beside it,
    // the way the account menu does when collapsed.
    assert.match(appearance, /#app\.sidebar-collapsed #sidebar \.new-chat-menu \{[\s\S]*?left: calc\(100% \+ 12px\)/,
        'the collapsed rail clips its picker');
    // /admin has no workbench navigation to back a launch control.
    assert.match(consoleCss, /#app\[data-nav-area="admin"\] #sidebar \.sidebar-new-chat-wrap \{ display: none !important; \}/);
});
