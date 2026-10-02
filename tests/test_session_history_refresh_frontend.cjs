// Exercise the shipped lifecycle handlers with delayed HTTP responses and SSE
// events. Rendering is stubbed; ownership, buffering and refreshes are real.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');
function section(from, to) {
    const start = source.indexOf(from), end = source.indexOf(to, start);
    assert.ok(start >= 0 && end > start, from);
    return source.slice(start, end);
}
const response = data => ({ json: async () => data });
const settle = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}
function setup(transport = () => response({ status: 'success', stream: true, request_id: 'r1' })) {
    const calls = [], streams = [], rendered = [], timers = [], refreshes = [];
    const storage = new Map([['cow_tenant_id', 'tenant-one']]);
    let welcome = { remove() { welcome = null; } };
    const ctx = vm.createContext({
        console, Date, Map, currentLang: 'zh', activeAgentId: 'agent-a', sessionId: 's1', _authEpoch: 1,
        sessionStorage: { getItem: key => storage.get(key) || null },
        _identityMode: () => 'database', _historyVisible: false, _historyDirty: false,
        loadSessionList: () => refreshes.push('full'), loadSidebarRecentSessions: () => refreshes.push('sidebar'),
        chatInput: { value: 'First question' }, pendingAttachments: [], inputHistory: [],
        historyIdx: -1, historySavedDraft: '', sendBtn: { disabled: false },
        document: { getElementById: id => id === 'welcome-screen' ? welcome : null },
        messagesDiv: { querySelector: () => null },
        activeStreams: {}, streamBuffers: {}, sessionActiveRequest: {}, loadingContainers: {},
        pollGeneration: 0, isPolling: false,
        syncTeamFromText() {}, renderComposerIdentity() {}, resetComposerHeight() {},
        renderAttachmentPreview() {}, addressedAgentId: () => '',
        addUserMessage() {}, addUserVoiceMessage() {}, addLoadingIndicator: () => ({ remove() {} }),
        readMessageResponse: r => r.json(), rememberLiveSpeaker() {}, setLoadingSpeaker() {},
        setSendBtnCancelMode() {}, resetSendBtnSendMode() {}, updateEditButtonsState() {},
        addBotMessage: text => rendered.push(text), addMessageError() {},
        localizeCancelMarker: text => text, renderBotSpeakerButton() {}, scrollChatToBottom() {},
        notifyTaskFinished() {}, showTaskNotification() {}, sessionTitleOf: () => '',
        firstLineSnippet: text => text, PRODUCT_NAME: 'Test', t: key => key,
        setTimeout: fn => timers.push(fn),
        fetch(url, options) { calls.push({ url, options }); return Promise.resolve(transport(url, options)); },
        EventSource: class {
            constructor(url) { this.url = url; streams.push(this); }
            close() { this.closed = true; }
            emit(item) { this.onmessage({ data: JSON.stringify(item) }); }
        },
    });
    for (const [from, to] of [
        ['function runtimeSessionKey(', 'function isCurrentSessionConversationActive('],
        ['function _refreshHistoryList(', '// === SIDEBAR_RECENT_BEGIN ==='],
        ['function sendVoiceMessage(', 'function addUserVoiceMessage('],
        ['function sendMessage(', '// Attachment markers the backend'],
        ['function generateSessionTitle(', '// ====================================================================='],
        ['function _reattachStream(', '/* ====================================================================='],
    ]) vm.runInContext(section(from, to), ctx);
    return { ctx, calls, streams, rendered, timers, refreshes, storage };
}

test('text and voice save acknowledgements refresh history before any model event', async () => {
    for (const voice of [false, true]) {
        const pending = deferred(), h = setup(() => pending.promise);
        if (voice) h.ctx.sendVoiceMessage('First question', '/audio.wav'); else h.ctx.sendMessage();
        assert.equal(h.refreshes.length, 0);
        pending.resolve(response({ status: 'success', stream: true, request_id: 'r1' }));
        await settle();
        assert.deepEqual(h.refreshes, []);
        assert.equal(h.ctx._historyDirty, true);
        assert.equal(h.streams.length, 1);
        assert.equal(h.rendered.length, 0);
        assert.equal(JSON.parse(h.calls[0].options.body).agent_id, 'agent-a');
    }
});

test('inline control replies, failed saves and empty sends never claim saved history', async () => {
    for (const data of [{ status: 'success', inline_reply: 'Cancelled' }, { status: 'error' }]) {
        const h = setup(() => response(data));
        h.ctx.sendMessage();
        await settle();
        assert.equal(h.refreshes.length, 0);
        assert.equal(h.streams.length, 0);
    }
    const h = setup(); h.ctx.chatInput.value = '';
    h.ctx.sendMessage();
    assert.equal(h.calls.length, 0);
});

test('preparing an unsent new chat does not write an empty record or prepend a sidebar row', () => {
    const h = setup();
    Object.assign(h.ctx, {
        window: {}, currentView: 'chat', generateSessionId: () => 'unsent',
        activeSessionStorageKey: () => 'session-key', writeScopedPreference() {},
        refreshWorkspaceSelector() {}, refreshSessionSettings() {}, startPolling() {}, renderWelcomeScreen() {},
    });
    vm.runInContext(section('function newChat(', '// =====================================================================\n// Session History'), h.ctx);
    h.ctx.newChat();
    assert.equal(h.ctx.sessionId, 'unsent'); assert.equal(h.calls.length, 0);
    assert.deepEqual(h.refreshes, []);
});

test('later replies refresh history without generating another first title', () => {
    const h = setup(); h.ctx.startSSE('r1', null, new Date(), null);
    h.streams[0].emit({ type: 'done', content: 'Next reply', seq: 1 });
    assert.deepEqual(h.refreshes, []); assert.equal(h.calls.length, 0);
});

test('foreground completion and title success refresh the single visible history list', async () => {
    const title = deferred(), h = setup(url => url.includes('generate_title') ? title.promise : response({ status: 'success' }));
    h.ctx._historyVisible = true;
    h.ctx.startSSE('r1', null, new Date(), { sid: 's1', agentId: 'agent-a', userMsg: 'Question' });
    h.streams[0].emit({ type: 'done', content: 'Answer', seq: 1 });
    assert.deepEqual(h.refreshes, ['full']);
    assert.deepEqual(h.rendered, ['Answer']);
    title.resolve(response({ status: 'success' })); await settle();
    assert.deepEqual(h.refreshes, ['full', 'full']);
});

test('background completion uses original owner and leaves the visible chat and draft alone', async () => {
    const h = setup(() => response({ status: 'success' }));
    h.ctx.startSSE('r1', null, new Date(), { sid: 's1', agentId: 'agent-a', userMsg: 'Question' });
    h.ctx.sessionId = 's2'; h.ctx.activeAgentId = 'agent-b'; h.ctx.chatInput.value = 'Keep draft';
    h.streams[0].emit({ type: 'done', content: 'Background answer', seq: 1 }); await settle();
    assert.equal(h.calls.length, 1);
    assert.equal(new URL(h.calls[0].url, 'http://test').searchParams.get('agent_id'), 'agent-a');
    assert.equal(JSON.parse(h.calls[0].options.body).agent_id, 'agent-a');
    assert.ok(h.calls[0].url.startsWith('/api/sessions/s1/generate_title'));
    assert.deepEqual(h.rendered, []);
    assert.equal(h.ctx.sessionId, 's2'); assert.equal(h.ctx.chatInput.value, 'Keep draft');
    assert.deepEqual(h.refreshes, []);
});

test('switch back rebuilds the real stream, retaining the first title and consuming it once', async () => {
    const h = setup(() => response({ status: 'success' }));
    h.ctx.startSSE('r1', null, new Date(), { sid: 's1', agentId: 'agent-a', userMsg: 'Question' });
    h.ctx.sessionId = 's2';
    h.streams[0].emit({ type: 'heartbeat', seq: 1 });
    h.ctx.sessionId = 's1'; assert.equal(h.ctx._reattachStream('s1'), true);
    assert.equal(h.streams[0].closed, true);
    assert.match(h.streams[1].url, /after_seq=1/);
    const event = { type: 'done', content: 'Answer', seq: 2 };
    h.streams[1].emit(event); h.streams[1].emit(event);
    h.streams[1].emit({ ...event, seq: 3 });
    // A completed buffer can also be replayed without submitting a new title.
    h.ctx.startSSE('r1', null, new Date(), null, h.ctx.streamBuffers.r1.items.slice());
    await settle();
    assert.equal(h.calls.length, 1);
    assert.equal(h.ctx.streamBuffers.r1.titleInfo, null);
});

test('a late send acknowledgement retains original session ownership after switching Agent', async () => {
    const pending = deferred(), h = setup(url => url === '/message' ? pending.promise : response({ status: 'success' }));
    h.ctx.sendMessage(); h.ctx.sessionId = 's2'; h.ctx.activeAgentId = 'agent-b';
    h.ctx.setSendBtnCancelMode = () => assert.fail('A late acknowledgement must not change the visible composer');
    h.ctx._contextAfterSessionChange = () => assert.fail('The new unsent session must stay unpersisted');
    h.ctx._contextNewSession = true;
    pending.resolve(response({ status: 'success', stream: true, request_id: 'r1' })); await settle();
    assert.equal(h.ctx.streamBuffers.r1.ownerContext.sid, 's1');
    assert.match(h.streams[0].url, /agent_id=agent-a/);
    h.streams[0].emit({ type: 'done', content: 'Answer', seq: 1 }); await settle();
    assert.equal(JSON.parse(h.calls[1].options.body).agent_id, 'agent-a');
    assert.deepEqual(h.rendered, []);
});

test('old identity callbacks cannot refresh or submit titles under the new identity', async () => {
    for (const change of [h => h.ctx._authEpoch++, h => h.storage.set('cow_tenant_id', 'tenant-two')]) {
        const pending = deferred(), h = setup(() => pending.promise);
        h.ctx.sendMessage(); change(h);
        pending.resolve(response({ status: 'success', stream: true, request_id: 'r1' })); await settle();
        assert.deepEqual(h.refreshes, []); assert.equal(h.streams.length, 0);
        const stream = setup();
        stream.ctx.startSSE('r1', null, new Date(), { sid: 's1', agentId: 'agent-a', userMsg: 'Question' });
        change(stream); stream.streams[0].emit({ type: 'done', seq: 1 });
        assert.equal(stream.calls.length, 0); assert.deepEqual(stream.refreshes, []);
        assert.equal(stream.streams[0].closed, true);
        const title = deferred(), titled = setup(() => title.promise);
        titled.ctx.generateSessionTitle('s1', 'Question', '', 'agent-a'); change(titled);
        title.resolve(response({ status: 'success' })); await settle();
        assert.deepEqual(titled.refreshes, []);
    }
});

test('poll replies refresh persisted history, while empty and stale polls do not', async () => {
    const h = setup(() => response({ status: 'success', has_content: true, content: 'Pushed reply', timestamp: 1 }));
    h.ctx.startPolling(); await settle();
    assert.deepEqual(h.refreshes, []); assert.deepEqual(h.rendered, ['Pushed reply']);
    const empty = setup(() => response({ status: 'success', has_content: false }));
    empty.ctx.startPolling(); await settle(); assert.deepEqual(empty.refreshes, []);
    const pending = deferred(), stale = setup(() => pending.promise);
    stale.ctx.startPolling(); stale.ctx._authEpoch++;
    pending.resolve(response({ status: 'success', has_content: true })); await settle();
    assert.deepEqual(stale.refreshes, []);
});

test('explicit new chat opens the shared panel and adds a temporary row; automatic fallback does not',()=>{
    const h=setup(), added=[];
    Object.assign(h.ctx,{
        window:{}, currentView:'chat', _sessionLoading:false, generateSessionId:()=> 'unsent',
        activeSessionStorageKey:()=> 'session-key', writeScopedPreference(){},
        refreshWorkspaceSelector(){}, refreshSessionSettings(){}, startPolling(){}, renderWelcomeScreen(){},
        openSessionPanel(){h.ctx._historyVisible=true;},
        _addOptimisticSessionItem:sid=>added.push(sid),
    });
    vm.runInContext(section('function newChat(', '// =====================================================================\n// Session History'),h.ctx);
    h.ctx.newChat();
    assert.equal(h.ctx._historyVisible,true);assert.deepEqual(added,['unsent']);assert.equal(h.calls.length,0);
    h.ctx.newChat(false);
    assert.deepEqual(added,['unsent']);assert.equal(h.calls.length,0);
});
