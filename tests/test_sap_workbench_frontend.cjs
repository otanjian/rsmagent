// Run the shipped dispatcher; a missing SAP bundle must never activate chat.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = p => fs.readFileSync(path.join(__dirname, '..', p), 'utf8');
const scene = JSON.parse(read('Scene/sap_workbench/scene.json'));

function setup({original = true, bundleFailure = false, sceneAvailable = true} = {}) {
    const nodes = new Map(), requests = [], calls = [], opened = [];
    const element = () => ({style:{}, remove(){}, appendChild(child){nodes.set(child.id, child);}});
    const document = {createElement:element, getElementById:id=>nodes.get(id),
        body:element(), head:{appendChild(script){
            if (script.src) queueMicrotask(() => {
                if (bundleFailure && script.src === '/scene-assets/runtime.js') script.onerror();
                else script.onload();
            });
        }}};
    const forbidden = () => {throw new Error('normal chat must not run');};
    const ctx = {document, console, queueMicrotask, setTimeout:()=>1, clearTimeout(){},
        currentLang:'zh', I18N:{zh:{}},
        fetch:async url => {requests.push(url); assert.equal(url,'/api/scenes');
            return {ok:true,json:async()=>({status:'success',categories:[],scenes:[scene]})};},
        newChat:forbidden, sendMessage:forbidden, switchSession:forbidden,
        generateSessionId:forbidden, addBotMessage:forbidden, navigateTo:forbidden,
        SapWorkbench:sceneAvailable ? {open:(options)=>{calls.push('open'); opened.push(options===undefined?null:options);}} : undefined};
    ctx.window=ctx; vm.createContext(ctx);
    const run=code=>vm.runInContext(code,ctx);
    run(read('channel/web/static/js/scenes/registry.js'));
    ctx.ScenesRegistry.registerRenderer('base',forbidden);
    run(read('channel/web/static/js/scenes/index.js'));
    if(original) {
        // The production runtime wraps the adapter in a closure too: its
        // fetch shim must not replace the host fetch it captures.
        run('(function(){' + read('Scene/_shared/frontend/adapter.js') +
            '_hostScene = {id:"original-draft"}; activeSceneContext = {id:"original-context"};' +
            'window.previousScene = () => [_hostScene.id, activeSceneContext.id, mounted];})();');
    }
    return {ctx,run,requests,calls,opened,nodes};
}

test('SAP entry opens only its scene, preserving the previous scene context',async()=>{
    const h=setup();
    await h.ctx.openSceneById('sap_workbench');
    assert.deepEqual(h.calls,['open']);
    // The card body opens a new session, while its nested button configures.
    assert.equal(h.opened.length,1);
    assert.equal(h.opened[0].view,'new-session');
    assert.deepEqual(h.requests,['/api/scenes']);
    assert.deepEqual(Array.from(h.ctx.previousScene()),['original-draft','original-context',null]);
});

test('the card configuration entry opens the connection form directly',async()=>{
    const h=setup();
    // The card declares this entry through `card_action: "configure"`.
    assert.equal(scene.card_action,'configure');
    await h.ctx.SceneOriginal.configure(scene);
    assert.deepEqual(h.calls,['open']);
    // The options object is built inside the vm realm, so compare its fields.
    assert.equal(h.opened.length,1);
    assert.equal(h.opened[0].view,'settings');
});

test('a scene without a configuration surface still opens through the same entry',async()=>{
    const h=setup();
    // The generic workbench bundle is not loaded in this suite, so the plain
    // open path may fail here. What matters is that it never touched SAP.
    await h.ctx.SceneOriginal.configure({id:'sap_data_analysis',name:'SAP 数据分析'}).catch(()=>{});
    assert.deepEqual(h.calls,[]);
    assert.deepEqual(h.opened,[]);
});

for(const options of [{original:false},{sceneAvailable:false},{bundleFailure:true}]) {
    test('unavailable SAP entry refuses normal fallback '+JSON.stringify(options),async()=>{
        const h=setup(options);
        await h.ctx.openSceneById('sap_workbench');
        assert.deepEqual(h.calls,[]);
        assert.deepEqual(h.requests,['/api/scenes']);
        assert.ok(h.nodes.get('scenes-notice').textContent);
    });
}

test('SAP respects the host unsaved-work guard before touching DOM or fetching',async()=>{
    const forbidden=()=>{throw new Error('guarded open touched runtime');};
    const ctx={document:{},fetch:forbidden,wsGuardUnsaved:()=>false,addEventListener(){}};
    ctx.window=ctx;vm.createContext(ctx);
    vm.runInContext(read('Scene/sap_workbench/frontend/workbench.js'),ctx);
    assert.equal(await ctx.SapWorkbench.open(),false);
    assert.doesNotThrow(()=>ctx.SapWorkbench.close());
});

// The platform's own coding entry, not the scene, serves the conversation pane.
const CODING_ORIGIN = 'https://code.example.com';

function setupWorkbench({dirty = false, hostGuard = true, canManage = false, sessionPorts = [4321], browserRef = '', visual = true, nativeSap = false, notes = []} = {}) {    const requests = [], dialogs = [], confirmations = [], sockets = [], drawn = [], frames = [], submittedForms = [];
    const timers = new Map(), events = {};
    let timerId = 0, sessionCount = 0;
    const element = tag => {
        const queries = new Map();
        return {
            tagName: tag, children: [], dataset: {}, style: {setProperty(name, value) {this[name] = value;}}, open: false, showCount: 0,
            hidden: false, className: '', textContent: '', type: '', value: '', tabIndex: 0,
            width: 0, height: 0, readyState: 1, listeners: {}, contentWindow: {}, attributes: {},
            append(...children) {children.forEach(child => {child.parentNode = this; this.children.push(child);});},
            appendChild(child) {this.append(child);},
            prepend(...children) {this.children.unshift(...children);},
            replaceChildren(...children) {this.children = children;},
            setAttribute(name, value) {this.attributes[name] = String(value);},
            getAttribute(name) {return this.attributes[name] ?? null;},
            removeAttribute(name) {delete this.attributes[name];},
            setPointerCapture(id) {this.capture = id;},
            hasPointerCapture(id) {return this.capture === id;},
            releasePointerCapture() {this.capture = null;},
            focus() {this.focused = true; document.activeElement = this;},
            remove() {if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(child => child !== this);},
            submit() {submittedForms.push(this);},
            addEventListener(type, handler) {(this.listeners[type] = this.listeners[type] || []).push(handler);},
            dispatch(type, event) {(this.listeners[type] || []).forEach(handler => handler(event));},
            getBoundingClientRect() {return this.rect || {left: 0, top: 0, width: 640, height: 400};},
            getContext() {return {drawImage: image => drawn.push(image)};},
            querySelector(selector) {
                if (!queries.has(selector)) queries.set(selector, element('div'));
                return queries.get(selector);
            },
            querySelectorAll(selector) {
                const matches = node => selector.startsWith('.')
                    ? node.className === selector.slice(1) : node.tagName === selector;
                return this.children.filter(matches);
            },
            showModal() {this.open = true; this.showCount++;},
            close() {this.open = false;},
        };
    };
    const blankDocument = () => {
        const head = element('head');
        return {head, createElement: tag => element(tag),
            getElementById: id => head.children.find(child => child.id === id) || null};
    };
    const document = {
        head: element('head'), body: element('body'), activeElement: element('button'),
        createElement(tag) {
            const node = element(tag);
            if (tag === 'dialog') dialogs.push(node);
            if (tag === 'iframe') {node.contentDocument = blankDocument(); frames.push(node);}
            return node;
        },
    };
    class FakeSocket {
        constructor(url, protocols) {
            this.url = url; this.protocols = protocols; this.readyState = 1;
            this.sent = []; this.listeners = {}; this.closed = false; sockets.push(this);
        }
        addEventListener(type, handler) {(this.listeners[type] = this.listeners[type] || []).push(handler);}
        emit(type, event) {(this.listeners[type] || []).forEach(handler => handler(event));}
        send(payload) {this.sent.push(payload);}
        close() {this.closed = true; this.readyState = 3;}
    }
    const ctx = {document, URL, location: {origin: 'https://console.test', href: 'https://console.test/'},
        currentLang: 'zh', t: key => key,
        addEventListener(type, handler) {(events[type] = events[type] || []).push(handler);},
        setTimeout(fn) {timers.set(++timerId, fn); return timerId;},
        clearTimeout(id) {timers.delete(id);},
        wsEditorDirty: () => dirty,
        wsDiscardEditState: () => {dirty = false;},
        showConfirmDialog: options => confirmations.push(options),
        WebSocket: FakeSocket, crypto: {randomUUID: () => 'test-request'},
        createImageBitmap: async () => ({width: 1280, height: 800, close() {}}),
        fetch: async (url, options) => {
            requests.push(url);
            // The conversation is the platform's own coding session. The console
            // contract is mirrored here: create or reopen, then mount the embed
            // address the entry returns.
            if (url.startsWith('/api/coding/sessions')) {
                if (url.endsWith('/attach')) {
                    const sent = JSON.parse(options.body);
                    return {ok: true, json: async () => ({status: 'success', session_id: 'coding-test',
                        external_session_id: sent.external_session_id})};
                }
                return {ok: true, json: async () => ({status: 'success', session_id: 'coding-test',
                    agent_id: 'sap', external_session_id: 'ses_test',
                    iframe_url: CODING_ORIGIN + '/L3RtcC9wcm9qZWN0/session/ses_test?rsm_embed=1'})};
            }
            if (url.endsWith('/sessions')) {
                const port = sessionPorts[Math.min(sessionCount++, sessionPorts.length - 1)];
                return {ok: true, json: async () => ({status: 'success', port,
                    binding_id: 'binding-test', remote_session_id: 'ses_test',
                    origin: `http://localhost:${port}`, bootstrap_token: 'test-boot',
                    ...(nativeSap ? {display_mode: 'iframe', sap_url: 'https://sap.example/sap/bc/gui?sap-client=200',
                        gui_automation: false, coding_session_id: 'coding-test', agent_id: 'sap'} : {}),
                    token: 'view-token', target: 'https://sap.example/sap/bc/gui', readonly: false})};
            }
            return {ok: true, json: async () => ({status: 'success', version: 1, can_manage: canManage,
                capabilities: {visual, blockers: [], notes}, checks: [], coding: null, coding_options: [],
                opencode: {configured: false, web_url: ''},
                // The real projection carries the editable config to a
                // controller; the connection form is rendered from it.
                config: {enabled: false, automation_enabled: false, commit_enabled: false,
                    coding_agent_id: '', browser_service_ref: browserRef,
                    sap: {system_id: '', web_gui_url: '', client: '', language: 'ZH',
                        allowed_origins: [], login_mode: 'password'},
                    mcp: {connections: [
                        {id: 'sap-abap', url: 'http://127.0.0.1:8110/mcp', enabled: true},
                        {id: 'sap-pyrfc', url: 'http://127.0.0.1:8200/mcp', enabled: true},
                    ]}, max_sessions: 4, idle_seconds: 900}})};
        },
    };
    ctx.window = ctx; vm.createContext(ctx);
    if (hostGuard) {
        // Use the real host contract: true means the caller must proceed;
        // the callback is invoked only after confirming discarded edits.
        const guard = read('channel/web/static/js/workspace.js')
            .match(/function wsGuardUnsaved\(next\) \{[\s\S]*?\n\}/)[0];
        vm.runInContext(guard, ctx);
    }
    vm.runInContext(read('Scene/sap_workbench/frontend/workbench.js'), ctx);
    return {ctx, requests, dialogs, confirmations, sockets, drawn, timers, frames, submittedForms,
        emit: (type, event) => (events[type] || []).forEach(handler => handler(event))};
}

for (const hostGuard of [true, false]) {
    test('SAP opens and loads configuration with a clean editor, host guard=' + hostGuard, async () => {
        const h = setupWorkbench({hostGuard});
        await h.ctx.SapWorkbench.open();
        assert.equal(h.dialogs.length, 1);
        assert.equal(h.dialogs[0].open, true);
        assert.equal(h.dialogs[0].showCount, 1);
        assert.deepEqual(h.requests, ['/api/scenes/sap-workbench/config']);
        assert.equal(h.confirmations.length, 0);
        h.ctx.SapWorkbench.close();
        assert.equal(h.dialogs[0].open, false);
        await h.ctx.SapWorkbench.open();
        assert.equal(h.dialogs.length, 1);
        assert.equal(h.dialogs[0].showCount, 2);
        assert.equal(h.requests.length, 2);
    });
}

test('SAP waits for dirty host editor confirmation, then opens exactly once', async () => {
    const h = setupWorkbench({dirty: true});
    assert.equal(await h.ctx.SapWorkbench.open(), false);
    assert.equal(h.dialogs.length, 0);
    assert.deepEqual(h.requests, []);
    assert.equal(h.confirmations.length, 1);
    h.confirmations[0].onConfirm();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(h.ctx.wsEditorDirty(), false);
    assert.equal(h.dialogs.length, 1);
    assert.equal(h.dialogs[0].open, true);
    assert.equal(h.dialogs[0].showCount, 1);
    assert.deepEqual(h.requests, ['/api/scenes/sap-workbench/config']);
});

const flush = () => new Promise(resolve => setImmediate(resolve));

test('native SAP fills an iframe without a screen socket and preserves it while chat toggles', async () => {
    const h = setupWorkbench({nativeSap:true});
    await h.ctx.SapWorkbench.open({view:'new-session'});
    await flush();
    assert.equal(h.sockets.length,0);
    const host = h.dialogs[0].querySelector('[data-sap="view"]');
    const sap = host.children[0];
    assert.equal(sap.tagName,'iframe');
    assert.equal(sap.src,'https://sap.example/sap/bc/gui?sap-client=200');
    assert.equal(sap.className,'sap-native-frame');
    // The conversation is mounted from the coding entry's own embed address, and
    // carries nothing but the parent origin and this mount's channel: there is no
    // grant to post into a frame the scene does not serve.
    const pane = h.dialogs[0].querySelector('[data-sap="code-pane"]');
    const frame = pane.children.find(child => child.tagName === 'iframe');
    const url = new URL(frame.src);
    assert.equal(url.origin, CODING_ORIGIN);
    assert.equal(url.pathname, '/L3RtcC9wcm9qZWN0/session/ses_test');
    assert.equal(url.searchParams.get('rsm_embed'), '1');
    assert.equal(url.searchParams.get('rsm_parent_origin'), 'https://console.test');
    assert.ok(url.searchParams.get('rsm_channel'));
    assert.equal(h.submittedForms.length,0);
    const toggle = h.dialogs[0].querySelector('[data-sap="chat-toggle"]');
    toggle.dispatch('click'); toggle.dispatch('click');
    assert.equal(host.children[0],sap);
    assert.equal(h.submittedForms.length,0);
    h.ctx.SapWorkbench.close();
    assert.equal(host.children.length,0);
    assert.equal(h.timers.size,0);
});

test('the shared build pane is trimmed in its own document without a proxy', async () => {
    const h = setupWorkbench({nativeSap:true});
    await h.ctx.SapWorkbench.open({view:'new-session'});
    await flush();
    const pane = h.dialogs[0].querySelector('[data-sap="code-pane"]');
    const frame = pane.children.find(child => child.tagName === 'iframe');
    // The scene serves no copy of the Web build; it trims the frame's own
    // document instead, so the compact tabs and the empty title strip stop
    // sitting above the header this dialog already renders.
    const style = frame.contentDocument.getElementById('sap-code-trim');
    assert.ok(style, 'the frame document must carry the scene trim');
    assert.match(style.textContent, /header:has\(#opencode-titlebar-right\)/);
    assert.match(style.textContent, /data-value="session"/);
    assert.match(style.textContent, /data-value="changes"/);
    assert.match(style.textContent, /display:none!important/);
    // A reload of the same frame must not stack a second copy.
    frame.dispatch('load');
    assert.equal(frame.contentDocument.head.children.filter(child => child.id === 'sap-code-trim').length, 1);
    // A frame this page cannot read (cross-origin) is left exactly as OpenCode
    // ships it rather than failing the mount.
    frame.contentDocument = null;
    assert.doesNotThrow(() => frame.dispatch('load'));
});

test('native old session is removed after replacement and cannot recreate itself through heartbeat', async () => {
    const h = setupWorkbench({nativeSap:true});
    await h.ctx.SapWorkbench.open({view:'new-session'});
    const requests = [];
    h.ctx.fetch = async (url,options) => {
        requests.push(JSON.parse(options.body));
        return {ok:false,status:410,json:async()=>({code:'session_closed'})};
    };
    for(const [id,run] of [...h.timers]) {h.timers.delete(id); run();}
    await flush();
    assert.deepEqual(requests,[{binding_id:'binding-test',action:'heartbeat'}]);
    assert.equal(h.dialogs[0].dataset.live,'false');
    assert.equal(h.dialogs[0].querySelector('[data-sap="view"]').children.length,0);
    assert.equal(h.dialogs[0].querySelector('[data-sap="chat-toggle"]').hidden,true);
    assert.equal(h.timers.size,0);
    assert.equal(h.sockets.length,0);
});

test('the conversation pane names its coding state, offers one retry and clears on close', async () => {
    const h = setupWorkbench({nativeSap:true});
    const original = h.ctx.fetch;
    let release;
    h.ctx.fetch = async (url, options) => {
        // Hold the coding open so the pane can only say what it actually knows:
        // the platform has not answered yet, so it must not claim progress.
        if (url.startsWith('/api/coding/sessions'))
            return new Promise(resolve => {release = () => resolve(original(url, options));});
        return original(url, options);
    };
    const opening = h.ctx.SapWorkbench.open({view:'new-session'});
    for (let i = 0; i < 4; i++) await flush();
    const pane = h.dialogs[0].querySelector('[data-sap="code-pane"]');
    const state = () => pane.children.find(child => child.className === 'sap-code-state');
    assert.equal(state().children[0].textContent, '正在打开智能体会话…');
    release();
    await opening;
    await flush();
    assert.equal(state().children[0].textContent, '正在打开对话…');
    assert.equal(state().getAttribute('role'), 'status');
    // An embed that never reports itself ready is not narrated forever: the pane
    // says so and offers the one action that is safe, reopening this session.
    for (const [id, run] of [...h.timers]) {h.timers.delete(id); run();}
    assert.equal(state().children[0].textContent, '智能体会话尚未就绪。');
    assert.equal(state().getAttribute('role'), 'alert');
    assert.equal(state().children[1].textContent, '重试');
    h.ctx.SapWorkbench.close();
    assert.equal(h.timers.size, 0);
    assert.equal(state(), undefined, 'closing removes the state from the released pane');
});

test('resuming a history entry reopens its own coding session instead of creating one', async () => {
    const h = setupWorkbench({nativeSap:true});
    await h.ctx.SapWorkbench.open();
    const original = h.ctx.fetch;
    h.ctx.fetch = async (url, options) => {
        if (url.endsWith('/sessions') && !options?.method)
            return {ok:true, json:async()=>({status:'success', sessions:[{id:'binding-test', state:'paused'}]})};
        return original(url, options);
    };
    h.dialogs[0].querySelector('[data-sap="actions"]').children[1].dispatch('click');
    await flush(); await flush();
    // A GET never creates remote state, so resuming cannot start a second
    // conversation; the create endpoint is not called at all.
    assert.equal(h.requests.filter(url => url === '/api/coding/sessions').length, 0);
    assert.deepEqual(h.requests.filter(url => url.startsWith('/api/coding/sessions/')),
        ['/api/coding/sessions/coding-test/open?agent_id=sap']);
    const pane = h.dialogs[0].querySelector('[data-sap="code-pane"]');
    assert.ok(pane.children.some(child => child.tagName === 'iframe'));
    h.ctx.SapWorkbench.close();
});

test('a conversation started inside the embed is registered once, for this mount only', async () => {
    const h = setupWorkbench({nativeSap:true});
    await h.ctx.SapWorkbench.open({view:'new-session'});
    await flush();
    const dialog = h.dialogs[0];
    const frame = dialog.querySelector('[data-sap="code-pane"]').children.find(child => child.tagName === 'iframe');
    const channel = new URL(frame.src).searchParams.get('rsm_channel');
    const note = {origin:CODING_ORIGIN, source:frame.contentWindow,
        data:{channel, type:'rsm.opencode.session', session_id:'ses_fork'}};
    // A message the parent cannot attribute to the frame it mounted is dropped
    // unread: not this origin, not this frame, not this channel, or already bound.
    for (const spoof of [{...note, origin:'https://other.test'}, {...note, source:{}},
        {...note, data:{...note.data, channel:'other-channel'}},
        {...note, data:{...note.data, session_id:'ses_test'}}]) h.emit('message', spoof);
    await flush();
    assert.equal(h.requests.filter(url => url === '/api/coding/sessions/attach').length, 0);
    h.emit('message', note);
    await flush();
    h.emit('message', note);
    await flush();
    assert.equal(h.requests.filter(url => url === '/api/coding/sessions/attach').length, 1);
    assert.equal(dialog.querySelector('[data-sap="notice"]').textContent, '新会话已加入历史。');
    h.ctx.SapWorkbench.close();
});

test('a refused coding open states the reason and the retry resumes the same request', async () => {
    const h = setupWorkbench({nativeSap:true});
    const original = h.ctx.fetch, attempts = [];
    h.ctx.fetch = async (url, options) => {
        if (url.startsWith('/api/coding/sessions')) {
            attempts.push({url, body: options?.body ? JSON.parse(options.body) : null});
            return {ok:false, status:503, json:async()=>({status:'error', code:'coding_upstream_unavailable'})};
        }
        return original(url, options);
    };
    await h.ctx.SapWorkbench.open({view:'new-session'});
    await flush();
    const dialog = h.dialogs[0], notice = dialog.querySelector('[data-sap="notice"]');
    const pane = dialog.querySelector('[data-sap="code-pane"]');
    const state = () => pane.children.find(child => child.className === 'sap-code-state');
    const reason = '无法打开智能体会话：平台智能体服务暂时不可达，请稍后重试。';
    assert.equal(state().children[0].textContent, reason);
    assert.equal(notice.textContent, reason);
    assert.equal(pane.children.some(child => child.tagName === 'iframe'), false);
    state().children[1].dispatch('click');
    await flush();
    assert.deepEqual(attempts, [{url: '/api/coding/sessions', body: {agent_id:'sap', request_id:'test-request', permission_profile:'sap_workbench'}},
        {url: '/api/coding/sessions', body: {agent_id:'sap', request_id:'test-request', permission_profile:'sap_workbench'}}]);
    h.ctx.SapWorkbench.close();
});

test('two retry clicks that overlap mount one frame and arm one readiness wait', async () => {
    // The retry button is bound before the pane re-renders, so a double click
    // delivers two clicks in the same tick. Both attempts name the same
    // conversation: the superseded one must not mount its own frame, or the
    // pane would carry two frames and two readiness waits for one session.
    const h = setupWorkbench({nativeSap:true});
    const original = h.ctx.fetch, releases = [];
    let refuse = true;
    h.ctx.fetch = async (url, options) => {
        if (url.startsWith('/api/coding/sessions') && !url.endsWith('/attach')) {
            if (refuse) return {ok:false, status:503, json:async()=>({status:'error',
                code:'coding_upstream_unavailable'})};
            await new Promise(resolve => releases.push(resolve));
        }
        return original(url, options);
    };
    await h.ctx.SapWorkbench.open({view:'new-session'});
    await flush();
    const dialog = h.dialogs[0], pane = dialog.querySelector('[data-sap="code-pane"]');
    const button = pane.children.find(child => child.className === 'sap-code-state').children[1];
    refuse = false;
    button.dispatch('click');
    button.dispatch('click');
    await flush();
    assert.equal(releases.length, 2, 'both clicks must reach the platform entry');
    assert.equal(pane.children.some(child => child.tagName === 'iframe'), false,
        'nothing may be mounted before the entry answers');
    for (const release of releases) release();
    await flush();
    assert.equal(pane.children.filter(child => child.tagName === 'iframe').length, 1,
        'only the newest attempt may mount a frame');
    h.ctx.SapWorkbench.close();
});

test('native navigation updates only the existing left iframe and duplicate delivery only re-acks', async () => {
    const h = setupWorkbench({nativeSap:true}); await h.ctx.SapWorkbench.open({view:'new-session'}); await flush();
    const sap = h.frames.find(frame => frame.className === 'sap-native-frame');
    const originalFrames = [...h.frames], forms = h.submittedForms.length, requests = [];
    const command = {id:'navigate-one',transaction:'ME21N',expires_at:Date.now()+60000,
        url:'https://sap.example/sap/bc/gui?sap-client=200&~transaction=ME21N'};
    h.ctx.fetch = async (url,options) => {
        const body = JSON.parse(options.body); requests.push(body);
        return {ok:true,json:async()=>({status:'success',binding_id:'binding-test',navigation:body.action === 'heartbeat' ? command : null})};
    };
    const tick = async () => {for(const [id,run] of [...h.timers]) {h.timers.delete(id); run();} await flush();};
    await tick(); assert.equal(sap.src,command.url);
    // If the URL were assigned again, this sentinel would be lost.
    sap.src = 'already applied'; await tick(); assert.equal(sap.src,'already applied');
    assert.equal(requests.filter(r=>r.action==='navigation_ack').length,2);
    assert.deepEqual(h.frames,originalFrames); assert.equal(h.submittedForms.length,forms);
    assert.equal(h.sockets.length,0); h.ctx.SapWorkbench.close();
});

test('expired and late navigation responses cannot modify the current SAP iframe', async () => {
    const h = setupWorkbench({nativeSap:true}); await h.ctx.SapWorkbench.open({view:'new-session'}); await flush();
    let sap = h.frames.find(frame => frame.className === 'sap-native-frame'), release;
    const original = sap.src;
    h.ctx.fetch = async () => ({ok:true,json:async()=>({status:'success',binding_id:'binding-test',navigation:{
        id:'expired',transaction:'ME21N',expires_at:Date.now()-1,url:original+'&~transaction=ME21N'}})});
    for(const [id,run] of [...h.timers]) {h.timers.delete(id); run();} await flush();
    assert.equal(sap.src,original);
    h.ctx.fetch = async () => new Promise(resolve=>{release=resolve;});
    for(const [id,run] of [...h.timers]) {h.timers.delete(id); run();} await flush();
    h.ctx.SapWorkbench.close();
    release({ok:true,json:async()=>({status:'success',binding_id:'binding-test',navigation:{
        id:'late',transaction:'ME21N',expires_at:Date.now()+60000,url:original+'&~transaction=ME21N'}})});
    await flush(); assert.equal(sap.src,original); assert.equal(h.timers.size,0);
});

test('full screen restores layout without replacing SAP or conversation and Escape restores before collapsing', async () => {
    const h = setupWorkbench({nativeSap:true});
    await h.ctx.SapWorkbench.open({view:'new-session'});
    await flush();
    const dialog = h.dialogs[0], pane = dialog.querySelector('[data-sap="code-pane"]');
    const sap = dialog.querySelector('[data-sap="view"]').children[0];
    const frame = pane.children.find(child => child.tagName === 'iframe');
    const channel = new URL(frame.src).searchParams.get('rsm_channel');
    dialog.querySelector('[data-sap="chat-toggle"]').dispatch('click');
    const count = h.requests.length, forms = h.submittedForms.length;
    const fullscreen = dialog.querySelector('[data-sap="chat-fullscreen"]');
    fullscreen.dispatch('click');
    assert.equal(dialog.dataset.chatFullscreen, 'true');
    assert.equal(fullscreen.textContent, '还原');
    assert.equal(fullscreen.getAttribute('aria-pressed'), 'true');
    dialog.dispatch('cancel', {preventDefault(){}});
    assert.equal(dialog.dataset.chatFullscreen, 'false');
    assert.equal(pane.hidden, false);
    assert.equal(dialog.open, true);
    assert.equal(dialog.querySelector('[data-sap="view"]').children[0], sap);
    assert.equal(pane.children.find(child => child.tagName === 'iframe'), frame);
    assert.equal(frame.title, 'SAP智能助手');
    assert.equal(h.requests.length, count); assert.equal(h.submittedForms.length, forms);
    // Native navigation/fork notifications retain the owner-scoped instance: the
    // conversation pane stays mounted because this tab's channel matches.
    h.emit('message', {origin:CODING_ORIGIN, source:frame.contentWindow,
        data:{channel,type:'rsm.opencode.session',session_id:'ses_native_fork'}});
    await flush();
    assert.equal(dialog.dataset.live, 'true'); assert.equal(pane.hidden, false);
    dialog.dispatch('cancel', {preventDefault(){}});
    assert.equal(pane.hidden, true); assert.equal(dialog.open, true);
    h.ctx.SapWorkbench.close();
});

test('divider drag and keyboard clamp assistant width without remounting frames or making requests', async () => {
    const h = setupWorkbench({nativeSap:true}); await h.ctx.SapWorkbench.open({view:'new-session'});
    const dialog = h.dialogs[0], resizer = dialog.querySelector('[data-sap="chat-resizer"]');
    dialog.querySelector('[data-sap="panes"]').rect = {width:1200,height:700};
    dialog.querySelector('[data-sap="code-pane"]').rect = {width:400,height:700};
    dialog.querySelector('[data-sap="chat-toggle"]').dispatch('click');
    const frames = [...h.frames], requests = h.requests.length;
    assert.equal(resizer.hidden,false);
    resizer.dispatch('pointerdown',{button:0,pointerId:4,clientX:800,preventDefault(){}});
    assert.equal(resizer.capture,4); assert.equal(dialog.dataset.chatResizing,'true');
    resizer.dispatch('pointermove',{pointerId:4,clientX:700});
    assert.equal(dialog.style['--sap-chat-width'],'500px');
    resizer.dispatch('pointermove',{pointerId:4,clientX:-2000});
    assert.equal(dialog.style['--sap-chat-width'],'874px');
    resizer.dispatch('pointermove',{pointerId:4,clientX:3000});
    assert.equal(dialog.style['--sap-chat-width'],'320px');
    resizer.dispatch('pointercancel');
    assert.equal(dialog.dataset.chatResizing,'false'); assert.equal(resizer.capture,null);
    resizer.dispatch('pointermove',{pointerId:4,clientX:700});
    assert.equal(dialog.style['--sap-chat-width'],'320px');
    resizer.dispatch('keydown',{key:'ArrowLeft',preventDefault(){}});
    assert.equal(dialog.style['--sap-chat-width'],'424px');
    resizer.dispatch('keydown',{key:'Home',preventDefault(){}});
    assert.equal(dialog.style['--sap-chat-width'],'320px');
    dialog.querySelector('[data-sap="chat-fullscreen"]').dispatch('click');
    assert.equal(resizer.hidden,true);
    dialog.querySelector('[data-sap="chat-fullscreen"]').dispatch('click');
    assert.equal(resizer.hidden,false);
    assert.deepEqual(h.frames,frames); assert.equal(h.requests.length,requests);
    h.ctx.SapWorkbench.close(); assert.equal(resizer.hidden,true);
});

test('resume ignores terminal replaced history and keeps the latest resumable binding', async () => {
    const h = setupWorkbench();
    await h.ctx.SapWorkbench.open();
    const original = h.ctx.fetch, dispatched = [];
    h.ctx.fetch = async (url,options) => {
        if(url.endsWith('/sessions') && !options?.method) return {ok:true,json:async()=>({status:'success',sessions:[{id:'replaced',state:'closed'},{id:'active',state:'paused'}]})};
        if(options) dispatched.push(JSON.parse(options.body));
        return original(url,options);
    };
    const resume = h.dialogs[0].querySelector('[data-sap="actions"]').children[1];
    resume.dispatch('click'); await flush();
    assert.equal(dispatched[0].binding_id,'active');
    h.ctx.SapWorkbench.close();
});

test('SAP viewport follows coalesced container resizes and releases the observer on close', async () => {
    const h = setupWorkbench({canManage:true}), observers = [];
    h.ctx.ResizeObserver = class {
        constructor(callback) {this.callback = callback; observers.push(this);}
        observe(host) {this.host = host;}
        disconnect() {this.disconnected = true;}
    };
    await h.ctx.SapWorkbench.open({view:'new-session'}); await flush();
    const observer = observers[0], socket = h.sockets[0];
    const drain = () => {for (const [id, run] of [...h.timers]) {h.timers.delete(id); run();}};
    observer.host.rect = {width:1470,height:698}; socket.emit('open');
    observer.callback(); observer.host.rect = {width:1600,height:900}; observer.callback();
    drain();
    assert.deepEqual(socket.sent.map(JSON.parse), [{t:'resize',width:1600,height:900}]);
    observer.callback(); drain(); assert.equal(socket.sent.length, 1, 'unchanged size is not resent');
    observer.host.rect = {width:800,height:600}; observer.callback(); drain();
    assert.deepEqual(JSON.parse(socket.sent[1]), {t:'resize',width:800,height:600});
    observer.host.rect = {width:0,height:0}; observer.callback(); drain();
    assert.equal(socket.sent.length, 2, 'hidden containers never shrink SAP to zero');
    observer.host.rect = {width:1000,height:500}; observer.callback();
    h.ctx.SapWorkbench.close();
    assert.equal(observer.disconnected, true); drain();
    assert.equal(socket.sent.length, 2, 'closing cancels a pending resize');
});

test('floating chat toggles retain the iframe, SAP target and current session without requests', async () => {
    const h = setupWorkbench({canManage: true});
    await h.ctx.SapWorkbench.open();
    const dialog = h.dialogs[0], toggle = dialog.querySelector('[data-sap="chat-toggle"]');
    const pane = dialog.querySelector('[data-sap="code-pane"]');
    assert.equal(toggle.hidden, true, 'no chat is offered before a session exists');
    dialog.querySelector('[data-sap="actions"]').children[0].dispatch('click'); await flush();
    assert.equal(toggle.hidden, false);
    assert.equal(pane.hidden, true);
    assert.equal(pane.inert, true);
    assert.equal(toggle.getAttribute('aria-expanded'), 'false');
    const frame = h.frames[0], canvas = dialog.querySelector('[data-sap="view"]').children[0];
    const requests = h.requests.length;
    for (let attempt = 0; attempt < 3; attempt++) {
        toggle.dispatch('click');
        assert.equal(pane.hidden, false); assert.equal(pane.inert, false);
        assert.equal(toggle.getAttribute('aria-expanded'), 'true');
        assert.equal(h.ctx.document.activeElement, dialog.querySelector('[data-sap="chat-close"]'));
        dialog.querySelector('[data-sap="chat-close"]').dispatch('click');
        assert.equal(pane.hidden, true); assert.equal(pane.inert, true);
        assert.equal(h.ctx.document.activeElement, toggle);
        assert.equal(pane.children.find(child => child.tagName === 'iframe'), frame);
        assert.equal(dialog.querySelector('[data-sap="view"]').children[0], canvas);
    }
    assert.equal(h.frames.length, 1, 'collapsing does not reload the conversation');
    assert.equal(h.sockets.length, 1); assert.equal(h.sockets[0].closed, false);
    assert.equal(h.requests.length, requests, 'layout changes do not call a model or alter control');
});

test('Escape collapses chat first and only then closes the workbench', async () => {
    const h = setupWorkbench({canManage: true}); await h.ctx.SapWorkbench.open();
    const dialog = h.dialogs[0];
    dialog.querySelector('[data-sap="actions"]').children[0].dispatch('click'); await flush();
    dialog.querySelector('[data-sap="chat-toggle"]').dispatch('click');
    dialog.dispatch('cancel', {preventDefault() {}});
    assert.equal(dialog.open, true); assert.equal(h.sockets[0].closed, false);
    assert.equal(dialog.querySelector('[data-sap="code-pane"]').hidden, true);
    dialog.dispatch('cancel', {preventDefault() {}});
    assert.equal(dialog.open, false); assert.equal(h.sockets[0].closed, true);
    assert.equal(dialog.querySelector('[data-sap="chat-toggle"]').hidden, true);
    assert.equal(dialog.querySelector('[data-sap="code-pane"]').children.some(child => child.tagName === 'iframe'), false);
});

test('refresh preserves expanded chat and host recovery restores it against the new origin', async () => {
    const h = setupWorkbench({canManage: true, sessionPorts: [4321, 5432]}); await h.ctx.SapWorkbench.open();
    const dialog = h.dialogs[0];
    dialog.querySelector('[data-sap="actions"]').children[0].dispatch('click'); await flush();
    dialog.querySelector('[data-sap="chat-toggle"]').dispatch('click');
    const frame = h.frames[0];
    dialog.querySelector('[data-sap="toolbar"]').children.find(child => child.textContent === '刷新状态').dispatch('click');
    await flush();
    assert.equal(h.frames.length, 1); assert.equal(dialog.querySelector('[data-sap="code-pane"]').hidden, false);
    h.sockets[0].emit('close');
    for (const retry of h.timers.values()) retry(); h.timers.clear(); await flush();
    const pane = dialog.querySelector('[data-sap="code-pane"]');
    assert.equal(pane.hidden, false); assert.equal(pane.inert, false);
    assert.equal(h.frames.length, 2); assert.notEqual(h.frames[1], frame);
    assert.equal(pane.children.find(child => child.tagName === 'iframe'), h.frames[1]);
    assert.equal(pane.children.includes(frame), false);
    assert.equal(h.sockets[1].url, 'ws://127.0.0.1:5432/screen');
});

test('ending and starting a session resets expanded chat and releases the previous iframe', async () => {
    const h = setupWorkbench({canManage: true}); await h.ctx.SapWorkbench.open();
    const dialog = h.dialogs[0], actions = dialog.querySelector('[data-sap="actions"]');
    actions.children[0].dispatch('click'); await flush();
    dialog.querySelector('[data-sap="chat-toggle"]').dispatch('click');
    const oldFrame = h.frames[0];
    actions.children[2].dispatch('click'); await flush();
    assert.equal(dialog.querySelector('[data-sap="chat-toggle"]').hidden, true);
    assert.equal(dialog.querySelector('[data-sap="code-pane"]').children.includes(oldFrame), false);
    actions.children[0].dispatch('click'); await flush();
    assert.equal(dialog.querySelector('[data-sap="chat-toggle"]').hidden, false);
    assert.equal(dialog.querySelector('[data-sap="code-pane"]').hidden, true);
    assert.equal(dialog.querySelector('[data-sap="code-pane"]').inert, true);
});

for (const [language, label, collapse] of [['zh', 'AI 对话', '收起对话'], ['en', 'AI chat', 'Collapse chat'], ['zh-Hant', 'AI 對話', '收起對話']]) {
    test('floating chat controls follow the current language ' + language, async () => {
        const h = setupWorkbench({canManage: true}); h.ctx.currentLang = language;
        await h.ctx.SapWorkbench.open();
        const dialog = h.dialogs[0];
        dialog.querySelector('[data-sap="actions"]').children[0].dispatch('click'); await flush();
        assert.equal(dialog.querySelector('[data-sap="chat-toggle"]').textContent, label);
        assert.equal(dialog.querySelector('[data-sap="chat-close"]').textContent, collapse);
    });
}

test('server pause and login expiry replace the obsolete automatic-control notice', async () => {
    const h = setupWorkbench({canManage: true});
    await h.ctx.SapWorkbench.open();
    h.dialogs[0].querySelector('[data-sap="actions"]').children[0].dispatch('click');
    await flush();
    const notice = h.dialogs[0].querySelector('[data-sap="notice"]');
    h.sockets[0].emit('message', {data: JSON.stringify({t:'control',control:'automatic',epoch:1,login_required:false})});
    assert.equal(notice.textContent, '已允许对话操作当前 SAP 页面。');
    h.sockets[0].emit('message', {data: JSON.stringify({t:'control',control:'manual',epoch:2,login_required:false})});
    assert.equal(notice.textContent, '会话已连接，当前由人工控制。');
    h.sockets[0].emit('message', {data: JSON.stringify({t:'control',control:'manual',epoch:3,login_required:true})});
    assert.equal(notice.textContent, '请先在 SAP 页面完成登录。');
});

test('returning from live-session settings restores usable new/resume controls', async () => {
    const h = setupWorkbench({canManage:true});
    await h.ctx.SapWorkbench.open();
    const dialog=h.dialogs[0], actions=dialog.querySelector('[data-sap="actions"]');
    actions.children[0].dispatch('click'); await flush();
    assert.equal(dialog.dataset.live, 'true');
    dialog.querySelector('[data-sap="toolbar"]').children.find(x=>x.textContent==='连接配置').dispatch('click');
    assert.equal(dialog.dataset.live, 'false');
    const form=dialog.querySelector('[data-sap="form"]');
    form.children[0].children.find(x=>x.textContent==='返回工作台').dispatch('click');
    assert.equal(form.hidden,true);
    assert.equal(actions.children[0].textContent,'新建工作台会话');
    assert.equal(actions.children[0].disabled,false);
    assert.equal(actions.children[1].textContent,'恢复已有会话');
});

for (const transition of ['settings', 'refresh', 'close']) {
    test('a pending start cannot attach panes or unlock a newer start after ' + transition, async () => {
        const h=setupWorkbench({canManage:true});await h.ctx.SapWorkbench.open();
        const dialog=h.dialogs[0],actions=dialog.querySelector('[data-sap="actions"]');
        const original=h.ctx.fetch,finishes=[];
        h.ctx.fetch=async (url,options)=>{
            const response=await original(url,options);
            return url.endsWith('/sessions')?new Promise(resolve=>finishes.push(()=>resolve(response))):response;
        };
        actions.children[0].dispatch('click');await flush();
        assert.equal(actions.children[0].disabled,true);
        if(transition==='settings'){
            dialog.querySelector('[data-sap="toolbar"]').children.find(x=>x.textContent==='连接配置').dispatch('click');
            const form=dialog.querySelector('[data-sap="form"]');
            form.children[0].children.find(x=>x.textContent==='返回工作台').dispatch('click');
        }else if(transition==='refresh'){
            dialog.querySelector('[data-sap="toolbar"]').children.find(x=>x.textContent==='刷新状态').dispatch('click');
            await flush();
        }else{
            h.ctx.SapWorkbench.close();await h.ctx.SapWorkbench.open();
        }
        assert.equal(actions.children[0].disabled,false,'leaving the old request permits a new or resumed session');
        actions.children[0].dispatch('click');await flush();
        assert.equal(finishes.length,2);
        const notice=dialog.querySelector('[data-sap="notice"]'),before=notice.textContent;
        finishes[0]();await flush();
        assert.equal(h.sockets.length,0,'the superseded start does not mount either pane');
        assert.equal(dialog.dataset.live,'false');
        assert.equal(actions.children[0].disabled,true,'its finally cannot clear the newer pending request');
        assert.equal(notice.textContent,before);
        finishes[1]();await flush();
        assert.equal(h.sockets.length,1);assert.equal(dialog.dataset.live,'true');
    });
}

test('leaving settings does not accept a delayed session start into the connection form',async()=>{
    const h=setupWorkbench({canManage:true});await h.ctx.SapWorkbench.open();
    const dialog=h.dialogs[0],original=h.ctx.fetch;let finish;
    h.ctx.fetch=async(url,options)=>{
        const response=await original(url,options);
        return url.endsWith('/sessions')?new Promise(resolve=>{finish=()=>resolve(response);}):response;
    };
    dialog.querySelector('[data-sap="actions"]').children[0].dispatch('click');await flush();
    dialog.querySelector('[data-sap="toolbar"]').children.find(x=>x.textContent==='连接配置').dispatch('click');
    const before=dialog.querySelector('[data-sap="notice"]').textContent;
    finish();await flush();
    assert.equal(dialog.querySelector('[data-sap="form"]').hidden,false);
    assert.equal(dialog.dataset.live,'false');assert.equal(h.sockets.length,0);
    assert.equal(dialog.querySelector('[data-sap="notice"]').textContent,before);
});

test('an abandoned resume list does not allocate a session after the dialog closes',async()=>{
    const h=setupWorkbench({canManage:true});await h.ctx.SapWorkbench.open();
    let finish,posts=0;
    h.ctx.fetch=(url,options)=>{
        if(options?.method==='POST')posts++;
        return new Promise(resolve=>{finish=()=>resolve({ok:true,json:async()=>({status:'success',sessions:[{id:'saved-binding'}]})});});
    };
    h.dialogs[0].querySelector('[data-sap="actions"]').children[1].dispatch('click');await flush();
    h.ctx.SapWorkbench.close();finish();await flush();
    assert.equal(posts,0);assert.equal(h.sockets.length,0);
});

test('a late close error cannot replace the notice for a newer workbench session',async()=>{
    const h=setupWorkbench({canManage:true});await h.ctx.SapWorkbench.open();
    const dialog=h.dialogs[0],actions=dialog.querySelector('[data-sap="actions"]');
    actions.children[0].dispatch('click');await flush();
    const original=h.ctx.fetch;let finish;
    h.ctx.fetch=(url,options)=>{
        if(options?.body&&JSON.parse(options.body).action==='close')return new Promise(resolve=>{finish=resolve;});
        return original(url,options);
    };
    actions.children[2].dispatch('click');await flush();
    actions.children[0].dispatch('click');await flush();
    const notice=dialog.querySelector('[data-sap="notice"]'),before=notice.textContent;
    finish({ok:false,json:async()=>({status:'error',code:'session_closed'})});await flush();
    assert.equal(notice.textContent,before);assert.equal(dialog.dataset.live,'true');
});

test('refreshing config keeps a pending reconnect attached to the same retained binding',async()=>{
    const h=setupWorkbench({canManage:true,sessionPorts:[4321,5432]});await h.ctx.SapWorkbench.open();
    const dialog=h.dialogs[0];dialog.querySelector('[data-sap="actions"]').children[0].dispatch('click');await flush();
    const original=h.ctx.fetch;let finish;
    h.ctx.fetch=async(url,options)=>{
        const response=await original(url,options);
        return url.endsWith('/sessions')?new Promise(resolve=>{finish=()=>resolve(response);}):response;
    };
    h.sockets[0].emit('close');for(const retry of h.timers.values())retry();h.timers.clear();await flush();
    dialog.querySelector('[data-sap="toolbar"]').children.find(x=>x.textContent==='刷新状态').dispatch('click');await flush();
    finish();await flush();
    assert.equal(h.sockets.length,2);assert.equal(h.sockets[1].url,'ws://127.0.0.1:5432/screen');
    const forms=h.submittedForms;
    assert.equal(forms.at(-1).action,'http://localhost:5432/bootstrap?binding=binding-test');
});

test('reconnecting after host reclamation restores both SAP and OpenCode to the new origin', async () => {
    const h = setupWorkbench({canManage: true, sessionPorts: [4321, 5432]});
    await h.ctx.SapWorkbench.open();
    h.dialogs[0].querySelector('[data-sap="actions"]').children[0].dispatch('click');
    await flush();
    const forms = () => h.submittedForms;
    assert.equal(forms().at(-1).action, 'http://localhost:4321/bootstrap?binding=binding-test');
    h.sockets[0].emit('close');
    for (const retry of h.timers.values()) retry();
    h.timers.clear();
    await flush();
    assert.equal(h.sockets[1].url, 'ws://127.0.0.1:5432/screen');
    assert.equal(forms().at(-1).action, 'http://localhost:5432/bootstrap?binding=binding-test');
    assert.equal(forms().length, 2);
    h.sockets[1].emit('message', {data: JSON.stringify({t: 'ready', viewport: {width: 1280, height: 800}})});
    assert.equal(h.dialogs[0].querySelector('[data-sap="notice"]').textContent, '会话已连接，当前由人工控制。');
});

for (const [code, message] of [
    ['unauthorized', '平台登录已过期，请重新登录后恢复工作台。'],
    ['session_forbidden', '当前账号已无权访问此工作台，请检查账号与租户。'],
    ['config_conflict', '连接配置已更新。请新建会话使用新配置；历史会话保持原目标。'],
]) {
    test('reconnection stops and clears both panes after ' + code, async () => {
        const h = setupWorkbench({canManage:true});
        await h.ctx.SapWorkbench.open();
        const dialog = h.dialogs[0];
        dialog.querySelector('[data-sap="actions"]').children[0].dispatch('click');
        await flush();
        h.ctx.fetch = async () => ({ok:false, json:async()=>({status:'error', code})});
        h.sockets[0].emit('close');
        for (const retry of h.timers.values()) retry();
        h.timers.clear();
        await flush();
        assert.equal(h.sockets.length, 1, 'no socket retry after revoked access');
        assert.equal(h.sockets[0].closed, true);
        assert.equal(dialog.dataset.live, 'false');
        assert.equal(dialog.querySelector('[data-sap="actions"]').children[0].disabled, false);
        assert.equal(dialog.querySelector('[data-sap="notice"]').textContent, message);
        assert.equal(h.timers.size, 0);
    });
}

test('SAP pane opens a token-gated live view, draws frames and forwards input', async () => {
    const h = setupWorkbench({canManage: true});
    await h.ctx.SapWorkbench.open();
    assert.deepEqual(h.requests, ['/api/scenes/sap-workbench/config']);

    // Nothing connects until the controller asks for the page.
    const actions = h.dialogs[0].querySelector('[data-sap="actions"]');
    actions.children[0].dispatch('click');
    await flush();
    assert.equal(h.requests[1], '/api/scenes/sap-workbench/sessions');

    const socket = h.sockets[0];
    assert.equal(socket.url, 'ws://127.0.0.1:4321/screen');
    assert.equal(h.dialogs[0].querySelector('[data-sap="notice"]').textContent, '对话已就绪，正在连接 SAP 页面…');
    // The view token travels as the sub-protocol, never in the URL.
    assert.deepEqual(Array.from(socket.protocols), ['view-token']);

    const host = h.dialogs[0].querySelector('[data-sap="view"]');
    const canvas = host.children[0];
    assert.equal(canvas.tagName, 'canvas');

    socket.emit('message', {data: JSON.stringify({t: 'ready', viewport: {width: 1280, height: 800}})});
    assert.equal(h.dialogs[0].querySelector('[data-sap="notice"]').textContent, '会话已连接，当前由人工控制。');
    assert.equal(canvas.width, 1280);
    socket.emit('message', {data: {jpeg: true}});
    await flush();
    assert.equal(h.drawn.length, 1, 'the frame reaches the canvas');

    canvas.dispatch('mousedown', {preventDefault() {}, clientX: 320, clientY: 200});
    assert.deepEqual(JSON.parse(socket.sent[0]), {t: 'mouse', event: 'down', x: 320, y: 200,
        button: 'left', canvas: {w: 640, h: 400}});
    canvas.dispatch('keydown', {preventDefault() {}, code: 'Enter', key: 'Enter', ctrlKey: false,
        altKey: false, shiftKey: false, metaKey: false});
    assert.deepEqual(JSON.parse(socket.sent[1]), {t: 'key', code: 'Enter', key: 'Enter',
        ctrl: false, alt: false, shift: false, meta: false});

    // Closing the dialog must not leave a socket streaming in the background.
    h.ctx.SapWorkbench.close();
    assert.equal(socket.closed, true);
});

test('late frames from a closed pane cannot change a newly opened session', async () => {
    const h=setupWorkbench({canManage:true});
    await h.ctx.SapWorkbench.open();
    const dialog=h.dialogs[0];
    dialog.querySelector('[data-sap="actions"]').children[0].dispatch('click');
    await flush();
    let finishDecode;
    h.ctx.createImageBitmap=()=>new Promise(resolve=>{finishDecode=resolve;});
    const old=h.sockets[0];
    old.emit('message',{data:{jpeg:true}});
    h.ctx.SapWorkbench.close();
    await h.ctx.SapWorkbench.open();
    dialog.querySelector('[data-sap="actions"]').children[0].dispatch('click');
    await flush();
    let disposed=false;
    finishDecode({width:1280,height:800,close(){disposed=true;}});
    await flush();
    assert.equal(disposed,true);
    assert.equal(h.drawn.length,0);
    const notice=dialog.querySelector('[data-sap="notice"]');
    const before=notice.textContent;
    old.emit('message',{data:JSON.stringify({t:'control',control:'automatic'})});
    old.emit('close');
    assert.equal(notice.textContent,before);
    assert.equal(h.timers.size,0);
});

test('only the mounted OpenCode channel can signal a session change, which revokes both panes', async () => {
    const h=setupWorkbench({canManage:true});
    await h.ctx.SapWorkbench.open();
    const dialog=h.dialogs[0];
    dialog.querySelector('[data-sap="actions"]').children[0].dispatch('click');
    await flush();
    const frame=dialog.querySelector('[data-sap="code-pane"]').children.find(n=>n.tagName==='iframe');
    const event={source:frame.contentWindow,origin:'http://localhost:4321',
        data:{type:'rsm.opencode.session',channel:'binding-test',session_id:'other-session'}};
    for(const spoof of [{...event,source:{}},{...event,origin:'http://other.test'},
        {...event,data:{...event.data,channel:'other-channel'}},
        {...event,data:{...event.data,session_id:'ses_test'}}]) h.emit('message',spoof);
    assert.equal(dialog.dataset.live,'true');
    assert.equal(h.sockets[0].closed,false);
    h.emit('message',event);
    await flush();
    assert.equal(dialog.dataset.live,'false');
    assert.equal(h.sockets[0].closed,true);
    assert.equal(dialog.querySelector('[data-sap="notice"]').textContent,'会话未运行，请恢复已有会话。');
    assert.equal(dialog.querySelector('[data-sap="actions"]').children[1].disabled,false);
});

test('a late control response cannot overwrite the notice for a new session', async () => {
    const h=setupWorkbench({canManage:true});
    await h.ctx.SapWorkbench.open();
    const dialog=h.dialogs[0];
    dialog.querySelector('[data-sap="actions"]').children[0].dispatch('click');
    await flush();
    const original=h.ctx.fetch;
    let finish;
    h.ctx.fetch=()=>new Promise(resolve=>{finish=resolve;});
    dialog.querySelector('[data-sap="actions"]').children[1].dispatch('click');
    await flush();
    h.ctx.fetch=original;
    h.ctx.SapWorkbench.close();
    await h.ctx.SapWorkbench.open();
    dialog.querySelector('[data-sap="actions"]').children[0].dispatch('click');
    await flush();
    const notice=dialog.querySelector('[data-sap="notice"]');
    const previous=notice.textContent;
    finish({ok:true,json:async()=>({status:'success'})});
    await flush();
    assert.equal(notice.textContent,previous);
});

test('opening the SAP pane does not allocate a member browser until session creation', async () => {
    const h = setupWorkbench({canManage: false});
    await h.ctx.SapWorkbench.open();
    const controls = h.dialogs[0].querySelector('[data-sap="view-controls"]');
    assert.deepEqual(controls.children, []);
    assert.equal(h.sockets.length, 0);
    assert.deepEqual(h.requests, ['/api/scenes/sap-workbench/config']);
});

test('SAP input commits CJK once, clears local values and pastes without a remote clipboard shortcut', async () => {
    const h = setupWorkbench({canManage: true});
    await h.ctx.SapWorkbench.open();
    h.dialogs[0].querySelector('[data-sap="actions"]').children[0].dispatch('click');
    await flush();
    const [canvas, input] = h.dialogs[0].querySelector('[data-sap="view"]').children;
    const socket = h.sockets[0];
    canvas.dispatch('mousedown', {preventDefault() {}, clientX: 100, clientY: 100});
    assert.equal(input.focused, true);
    socket.sent.length = 0;
    input.dispatch('compositionstart', {});
    input.value = 'ni';
    input.dispatch('input', {isComposing: true});
    input.dispatch('keydown', {key: 'Enter', code: 'Enter', isComposing: true});
    assert.equal(socket.sent.length, 0, 'IME candidates and confirmation Enter stay local');
    input.value = '你好';
    input.dispatch('compositionend', {});
    input.dispatch('input', {isComposing: false});
    assert.deepEqual(socket.sent.map(JSON.parse), [{t: 'text', text: '你好'}]);
    assert.equal(input.value, '');
    input.dispatch('keydown', {key: 'v', code: 'KeyV', metaKey: true});
    input.dispatch('paste', {preventDefault() {}, clipboardData: {getData: () => '采购测试'}});
    assert.deepEqual(JSON.parse(socket.sent[1]), {t: 'text', text: '采购测试'});
    input.value = 'unfinished';
    input.dispatch('blur');
    assert.equal(input.value, '', 'partial input does not survive focus changes');
    h.ctx.SapWorkbench.close();
    assert.equal(h.dialogs[0].dataset.live, 'false', 'closed dialog must not retain flex display');
    input.value = 'late';
    input.dispatch('input', {});
    assert.equal(socket.sent.length, 2, 'detached inputs cannot reach an old SAP target');
});

test('card body starts a new bound session without a second button or history lookup', async () => {
    const h = setupWorkbench({canManage: true}), posts = [];
    const fetch = h.ctx.fetch;
    h.ctx.fetch = (url, options) => {
        if (options?.method === 'POST') posts.push(JSON.parse(options.body));
        return fetch(url, options);
    };
    await h.ctx.SapWorkbench.open({view: 'new-session'});
    assert.deepEqual(h.requests, ['/api/scenes/sap-workbench/config', '/api/scenes/sap-workbench/sessions']);
    assert.deepEqual(posts, [{request_id: 'test-request'}]);
    assert.equal(h.sockets.length, 1);
    assert.equal(h.frames.length, 1);
    assert.equal(h.dialogs[0].dataset.live, 'true');
    assert.equal(h.dialogs[0].querySelector('[data-sap="code-pane"]').hidden, true);
    assert.equal(h.dialogs[0].querySelector('[data-sap="chat-toggle"]').hidden, false);
});

test('card body stays on unavailable overview without allocating a session', async () => {
    const h = setupWorkbench({visual: false});
    await h.ctx.SapWorkbench.open({view: 'new-session'});
    assert.deepEqual(h.requests, ['/api/scenes/sap-workbench/config']);
    assert.equal(h.sockets.length, 0);
    assert.equal(h.frames.length, 0);
    assert.equal(h.dialogs[0].dataset.live, 'false');
});

test('card body config failure reports sign-in and never starts a session', async () => {
    const h = setupWorkbench();
    h.ctx.fetch = async url => {
        h.requests.push(url);
        return {ok: false, status: 401, json: async () => ({status: 'error'})};
    };
    await h.ctx.SapWorkbench.open({view: 'new-session'});
    assert.deepEqual(h.requests, ['/api/scenes/sap-workbench/config']);
    assert.equal(h.sockets.length, 0);
    assert.match(h.dialogs[0].querySelector('[data-sap="notice"]').textContent, /重新登录/);
});

test('two card opens during config loading discard the stale load and start once', async () => {
    const h = setupWorkbench(), pending = [], fetch = h.ctx.fetch;
    h.ctx.fetch = (url, options) => {
        const response = fetch(url, options);
        if (url.endsWith('/config')) return new Promise(resolve => pending.push(() => resolve(response)));
        return response;
    };
    const first = h.ctx.SapWorkbench.open({view: 'new-session'});
    const second = h.ctx.SapWorkbench.open({view: 'new-session'});
    assert.equal(pending.length, 2);
    pending[1](); await second;
    pending[0](); await first;
    assert.equal(h.requests.filter(url => url.endsWith('/sessions')).length, 1);
    assert.equal(h.sockets.length, 1);
    assert.equal(h.frames.length, 1);
});

test('repeated card clicks during session allocation or after binding do not reload or duplicate', async () => {
    const h = setupWorkbench(), fetch = h.ctx.fetch;
    let finish;
    h.ctx.fetch = (url, options) => {
        const response = fetch(url, options);
        if (url.endsWith('/sessions')) return new Promise(resolve => {finish = () => resolve(response);});
        return response;
    };
    const first = h.ctx.SapWorkbench.open({view: 'new-session'});
    await flush();
    await h.ctx.SapWorkbench.open({view: 'new-session'});
    assert.deepEqual(h.requests, ['/api/scenes/sap-workbench/config', '/api/scenes/sap-workbench/sessions']);
    finish(); await first;
    const frame = h.frames[0], socket = h.sockets[0];
    await h.ctx.SapWorkbench.open({view: 'new-session'});
    assert.equal(h.requests.length, 2);
    assert.equal(h.frames.length, 1); assert.equal(h.frames[0], frame);
    assert.equal(h.sockets.length, 1); assert.equal(h.sockets[0], socket);
});

test('closing while card configuration loads prevents late automatic session creation', async () => {
    const h = setupWorkbench(), fetch = h.ctx.fetch;
    let finish;
    h.ctx.fetch = (url, options) => {
        const response = fetch(url, options);
        return new Promise(resolve => {finish = () => resolve(response);});
    };
    const opening = h.ctx.SapWorkbench.open({view: 'new-session'});
    h.ctx.SapWorkbench.close(); finish(); await opening;
    assert.deepEqual(h.requests, ['/api/scenes/sap-workbench/config']);
    assert.equal(h.sockets.length, 0);
    assert.equal(h.frames.length, 0);
    assert.equal(h.dialogs[0].open, false);
});

test('the configuration entry lands on the connection form, not the workbench', async () => {
    const h = setupWorkbench({canManage: true});
    await h.ctx.SapWorkbench.open({view: 'settings'});
    const main = h.dialogs[0].querySelector('[data-sap="main"]');
    const form = h.dialogs[0].querySelector('[data-sap="form"]');
    assert.equal(main.hidden, true, 'the workbench view is not the landing surface');
    assert.equal(form.hidden, false, 'the connection form is shown');
    // Opening settings must not start a browser anyone has to clean up.
    assert.equal(h.sockets.length, 0);
    assert.deepEqual(h.requests, ['/api/scenes/sap-workbench/config']);
    // The plain open still lands on the workbench.
    h.ctx.SapWorkbench.close();
    await h.ctx.SapWorkbench.open();
    assert.equal(main.hidden, false);
    assert.equal(form.hidden, true);
});

test('a member is never dropped into a configuration form they cannot use', async () => {
    const h = setupWorkbench({canManage: false});
    await h.ctx.SapWorkbench.open({view: 'settings'});
    const main = h.dialogs[0].querySelector('[data-sap="main"]');
    const form = h.dialogs[0].querySelector('[data-sap="form"]');
    assert.equal(main.hidden, false);
    assert.equal(form.hidden, true);
});

test('MCP settings show fixed read-only URLs and allow only enablement', async () => {
    const h = setupWorkbench({canManage: true});
    await h.ctx.SapWorkbench.open({view: 'settings'});
    const form = h.dialogs[0].querySelector('[data-sap="form"]');
    const descendants = node => [node, ...node.children.flatMap(descendants)];
    const nodes = descendants(form);
    const endpoints = nodes.filter(node => node.name === 'url');
    assert.deepEqual(endpoints.map(node => node.value), [
        'http://127.0.0.1:8110/mcp', 'http://127.0.0.1:8200/mcp',
    ]);
    assert.ok(endpoints.every(node => node.readOnly));
    assert.equal(nodes.filter(node => node.name === 'mcp_enabled').length, 2);
    assert.equal(nodes.filter(node => node.name === 'transport_auth' || node.name === 'profile_ref').length, 0);
    assert.ok(!nodes.some(node => node.tagName === 'button' && ['添加 MCP', '移除'].includes(node.textContent)));
    assert.ok(nodes.some(node => node.textContent.includes('仅连接测试 SAP https://sap.goodsap.cn:44300')));
    const password = nodes.find(node => node.name === 'mcp_password');
    assert.equal(password.type, 'password');
    assert.equal(password.value, '');
    assert.ok(nodes.some(node => node.name === 'mcp_username'));
    assert.ok(nodes.some(node => node.textContent === '密码尚未设置。'));
});

for (const browserRef of ['', 'sap-browser-worker', 'local', 'retired-node']) {
    test('browser node options preserve saved selection and identify retired nodes: '+browserRef, async () => {
        const h = setupWorkbench({canManage: true, browserRef});
        await h.ctx.SapWorkbench.open({view: 'settings'});
        const descendants = node => [node, ...node.children.flatMap(descendants)];
        const form = h.dialogs[0].querySelector('[data-sap="form"]');
        const node = descendants(form).find(item => item.name === 'browser_service_ref');
        assert.equal(node.tagName, 'select');
        assert.equal(node.value, browserRef);
        assert.equal(node.children[1].value, 'sap-browser-worker');
        assert.equal(node.children[1].textContent, '本机专属浏览器');
        if (browserRef === 'retired-node') assert.equal(node.children[2].textContent, '当前节点不可用');
        if (browserRef === 'local') assert.equal(node.children[2].textContent, '本机专属浏览器');
        assert.equal(h.sockets.length, 0);
    });
}

test('capability notes state the delivered limits instead of implying page control', async () => {
    const h = setupWorkbench({notes: ['navigation_limited', 'mcp_business_available', 'page_readwrite_unavailable']});
    await h.ctx.SapWorkbench.open();
    const dialog = h.dialogs[0];
    const notes = dialog.querySelector('[data-sap="notes"]').children.map(node => node.textContent);
    assert.deepEqual(notes, [
        '画面导航：有限可用。只确认导航指令送达，不代表已完成登录或拿到业务结果。',
        'SAP 业务数据：可用。对话按会话经服务端已登记的连接读写业务数据；SAP 凭据与连接标识不下发给模型。',
        '页面读写与业务提交：不可用。阅读、填写左侧 SAP 页面字段和提交业务单据均不开放。',
    ]);
    assert.equal(dialog.querySelector('[data-sap="blockers"]').children.length, 0);
});

test('the business row is withheld while no MCP account is saved', async () => {
    const h = setupWorkbench({notes: ['navigation_limited', 'mcp_credentials_missing', 'page_readwrite_unavailable']});
    await h.ctx.SapWorkbench.open();
    const notes = h.dialogs[0].querySelector('[data-sap="notes"]').children.map(node => node.textContent);
    assert.deepEqual(notes, [
        '画面导航：有限可用。只确认导航指令送达，不代表已完成登录或拿到业务结果。',
        'SAP 业务数据：未就绪。尚未保存 MCP 账号口令，服务端无法建立连接，数据调用会以 mcp_login_failed 失败。',
        '页面读写与业务提交：不可用。阅读、填写左侧 SAP 页面字段和提交业务单据均不开放。',
    ]);
});
