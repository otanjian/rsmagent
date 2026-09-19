/* Scheduler console module: target picker, authoring and run history
 * (change integrate-upstream-core-capabilities, P6).
 *
 * The module is a plain IIFE, `defer`-loaded before console.js, so it must not
 * touch the DOM at load time. The pure half is tested directly; the mounted
 * half is driven against a minimal fake document so the request shapes and the
 * "remove only after the server confirms" rule are exercised for real.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const MODULE_PATH = 'channel/web/static/js/functional-scheduler.js';

function loadModule() {
    const sandbox = { window: {} };
    vm.runInNewContext(fs.readFileSync(MODULE_PATH, 'utf8'), sandbox, {
        filename: MODULE_PATH
    });
    // The sandbox's own `window` is the one the module closes over, so features
    // it reads at mount time (the capability projection, the console's confirm
    // dialog) have to be installed there -- not on Node's `global`.
    return { api: sandbox.window.RdaiFunctionalScheduler, sandbox };
}

const loaded = loadModule();
const api = loaded.api;
const sandboxWindow = loaded.sandbox.window;

// The page always loads `functional-capabilities.js` first, so its projection
// reader is part of the environment the scheduler module is written against.
// This is that module's own contract, reduced to what `available` does.
const CAPABILITIES = {
    available: (context, key) => Boolean(
        context && context.feature_actions && context.feature_actions[key]
        && context.feature_actions[key].available === true),
};

function withSandboxWindow(extras) {
    for (const key of Object.keys(sandboxWindow)) delete sandboxWindow[key];
    Object.assign(sandboxWindow,
                  { RdaiFunctionalCapabilities: CAPABILITIES }, extras || {});
}

withSandboxWindow({});

// ---------------------------------------------------------------------------
// A minimal DOM: only what `mount` actually uses.
// ---------------------------------------------------------------------------

function makeNode(doc, tag) {
    const upper = String(tag).toUpperCase();
    const node = {
        ownerDocument: doc,
        tagName: upper,
        className: '',
        type: '',
        rows: 0,
        disabled: false,
        placeholder: '',
        children: [],
        listeners: {},
        attributes: {},
        removed: false,
        // Setting `textContent` in a real DOM replaces every child; the module
        // uses that to empty a <select> before repopulating it, so the fake has
        // to match or the options would accumulate across renders.
        get textContent() {
            if (this.children.length) {
                return this.children.map((child) => child.textContent).join('');
            }
            return this._text || '';
        },
        set textContent(value) {
            this._text = value === null || value === undefined ? '' : String(value);
            for (const child of this.children) child.parentNode = null;
            this.children = [];
        },
        appendChild(child) { this.children.push(child); child.parentNode = this; return child; },
        removeChild(child) {
            this.children = this.children.filter((item) => item !== child);
            return child;
        },
        remove() {
            this.removed = true;
            if (this.parentNode) this.parentNode.removeChild(this);
        },
        addEventListener(name, handler) {
            (this.listeners[name] = this.listeners[name] || []).push(handler);
        },
        setAttribute(name, value) { this.attributes[name] = value; },
        querySelector() { return null; },
        get firstChild() { return this.children[0] || null; },
    };
    // A real <select> reports the first option's value until the caller picks
    // one, which is how the pickers start usable without an explicit preselect.
    if (upper === 'SELECT') {
        let chosen = '';
        Object.defineProperty(node, 'value', {
            get() {
                if (chosen) return chosen;
                const first = this.children.find((child) => child.tagName === 'OPTION');
                return first ? first.value : '';
            },
            set(next) { chosen = next; },
            enumerable: true,
        });
    } else {
        node.value = '';
    }
    return node;
}

function makeDocument() {
    const doc = { body: null };
    doc.createElement = (tag) => makeNode(doc, tag);
    doc.body = makeNode(doc, 'body');
    return doc;
}

function find(root, predicate) {
    if (predicate(root)) return root;
    for (const child of root.children) {
        const hit = find(child, predicate);
        if (hit) return hit;
    }
    return null;
}

function byClass(root, token) {
    const needle = ' ' + token + ' ';
    return find(root, (node) => (' ' + node.className + ' ').indexOf(needle) !== -1);
}

function click(node) {
    for (const handler of node.listeners.click || []) handler({ target: node });
}

function mountScheduler(responses, context) {
    const doc = makeDocument();
    const root = makeNode(doc, 'div');
    const calls = [];
    const handle = api.mount({
        root,
        t: (key) => key,
        getContext: () => context || ALL_OPEN,
        request: (path, options) => {
            calls.push({ path, options });
            const matched = Object.keys(responses).find((key) => path.indexOf(key) === 0);
            const answer = matched === undefined ? null : responses[matched];
            if (answer instanceof Error) return Promise.reject(answer);
            return Promise.resolve(answer);
        },
    });
    return { handle, root, calls };
}

const INSTANCES = { status: 'success', instances: [{ id: 'i-1', name: 'Bot' }] };
const RECIPIENTS = {
    status: 'success',
    recipients: [{ receiver: 'user-1', name: 'Alice' }],
};
const EMPTY_RUNS = { status: 'success', runs: [], history_scope: 'attributed_only' };
// The projection the console hands the module. Every key it consults has to be
// declared: an action the server did not report open is closed, and a closed
// action renders nothing rather than a control that can only fail.
const ALL_OPEN = { feature_actions: {
    'scheduler.create': { available: true },
    'scheduler.runs.list': { available: true },
    'scheduler.runs.detail': { available: true },
    'scheduler.runs.delete': { available: true },
} };

// ---------------------------------------------------------------------------
// Loading contract
// ---------------------------------------------------------------------------

test('loading the module does not touch the document', () => {
    // The sandbox has no `document` at all: a load-time DOM access would throw
    // before the assertion, which is exactly the failure this guards.
    assert.equal(typeof api.mount, 'function');
});

// ---------------------------------------------------------------------------
// buildCreatePayload
// ---------------------------------------------------------------------------

test('a create payload carries only the field of the chosen action type', () => {
    const message = api.buildCreatePayload({
        name: 'Daily',
        instanceId: 'i-1',
        receiver: 'user-1',
        actionType: 'send_message',
        content: 'hi',
        schedule: { type: 'interval', interval: '1h' },
    });
    assert.deepEqual(JSON.parse(JSON.stringify(message)), {
        name: 'Daily',
        enabled: true,
        schedule: { type: 'interval', interval: '1h' },
        action: { type: 'send_message', instance_id: 'i-1', receiver: 'user-1', content: 'hi' },
    });
    assert.equal('task_description' in message.action, false);

    const task = api.buildCreatePayload({
        name: 'Daily',
        instanceId: 'i-1',
        receiver: 'user-1',
        actionType: 'agent_task',
        content: 'summarize',
        schedule: { type: 'cron', expression: '0 9 * * *' },
    });
    assert.deepEqual(JSON.parse(JSON.stringify(task.action)), {
        type: 'agent_task',
        instance_id: 'i-1',
        receiver: 'user-1',
        task_description: 'summarize',
    });
    assert.equal('content' in task.action, false);
});

test('a payload never carries the owner or tenant the client happens to know', () => {
    // Ownership is filled in server-side from the verified session. Sending it
    // would invite the server to trust it; not sending it keeps the trust seam
    // in one place.
    const payload = api.buildCreatePayload({
        name: 'Daily',
        instanceId: 'i-1',
        receiver: 'user-1',
        actionType: 'send_message',
        content: 'hi',
        schedule: { type: 'interval', interval: '1h' },
        owner: { user_id: 'attacker' },
        tenant_id: 'other-tenant',
        agent_id: 'other-agent',
    });
    const keys = Object.keys(payload);
    assert.deepEqual(keys.sort(), ['action', 'enabled', 'name', 'schedule']);
});

test('an incomplete form is refused before any request is made', () => {
    const base = {
        name: 'Daily',
        instanceId: 'i-1',
        receiver: 'user-1',
        actionType: 'send_message',
        content: 'hi',
        schedule: { type: 'interval', interval: '1h' },
    };
    const cases = [
        [{ ...base, name: '  ' }, 'invalid_name'],
        [{ ...base, instanceId: '' }, 'invalid_instance'],
        [{ ...base, receiver: '' }, 'invalid_receiver'],
        [{ ...base, content: '' }, 'invalid_content'],
        [{ ...base, actionType: 'drop_table' }, 'invalid_action_type'],
        [{ ...base, schedule: { type: 'nope' } }, 'invalid_schedule_type'],
        [{ ...base, schedule: { type: 'cron' } }, 'invalid_schedule_expression'],
        [{ ...base, schedule: { type: 'interval' } }, 'invalid_schedule_interval'],
    ];
    for (const [form, code] of cases) {
        assert.throws(() => api.buildCreatePayload(form), (error) => error.code === code, code);
    }
});

// ---------------------------------------------------------------------------
// History paging and merging
// ---------------------------------------------------------------------------

test('a repeated run_id updates its row instead of duplicating it', () => {
    const state = { runs: [{ run_id: 'r1', status: 'running' }, { run_id: 'r2' }] };
    const merged = api.mergeRunPage(state, {
        status: 'success',
        runs: [{ run_id: 'r2', status: 'done' }, { run_id: 'r3' }],
    });
    assert.deepEqual(JSON.parse(JSON.stringify(merged.map((run) => run.run_id))),
                     ['r1', 'r2', 'r3']);
    // The later payload wins for the row it repeats: a run that finished since
    // the first page must not keep rendering as running.
    assert.equal(merged[1].status, 'done');
});

test('a failed page is never merged as an empty list', () => {
    // A refusal or an outage must not be indistinguishable from "no runs".
    const kept = api.mergeRunPage({ runs: [{ run_id: 'r1' }] }, null);
    assert.deepEqual(JSON.parse(JSON.stringify(kept)), [{ run_id: 'r1' }]);
    assert.deepEqual(JSON.parse(JSON.stringify(api.runsFrom({ status: 'error' }))), []);
});

test('load more advances the offset past the rows already held', () => {
    const query = api.nextRunsQuery({ offset: 40, limit: 20, agentId: 'a-1', since: 1700 });
    assert.deepEqual(JSON.parse(JSON.stringify(query)), {
        limit: 20, offset: 40, agent_id: 'a-1', since: 1700,
    });
    assert.equal(api.serializeQuery({ limit: 20, offset: 0 }),
                 '?limit=20&offset=0');
    // The aggregate view omits agent_id entirely; it must never be sent empty,
    // because an empty string is a *global* read on the server's side only when
    // it is absent -- the client does not get to ask for the whole ledger.
    assert.equal('agent_id' in api.nextRunsQuery({ offset: 0 }), false);
});

test('the since watermark only advances over a drained page set', () => {
    const page = { status: 'success', runs: [{ run_id: 'r1', started_at: 1700 },
                                            { run_id: 'r2', started_at: 1705 }] };
    assert.equal(api.advanceSince({ since: 1600 }, page, true), 1705);
    // Not drained: a later page of the same second may still be unfetched, so
    // the watermark stays put or those runs are silently skipped forever.
    assert.equal(api.advanceSince({ since: 1600 }, page, false), 1600);
    assert.equal(api.advanceSince({ since: 1600 }, { runs: [] }, true), 1600);
});

// ---------------------------------------------------------------------------
// Detail rendering
// ---------------------------------------------------------------------------

test('a withheld body renders the preview and says it is a preview', () => {
    const view = api.runDetailView({ output_preview: 'hello', full_output: null });
    assert.deepEqual(JSON.parse(JSON.stringify(view)), { text: 'hello', isPreview: true });
    const full = api.runDetailView({ output_preview: 'hello', full_output: 'hello world' });
    assert.deepEqual(JSON.parse(JSON.stringify(full)), {
        text: 'hello world', isPreview: false,
    });
    // An empty string is not a body: the server sends null when withheld, and
    // an empty delivered message still counts as "no full text available".
    assert.equal(api.runDetailView({ output_preview: 'p', full_output: '' }).isPreview, true);
});

test('run statuses map to the three states the row can render', () => {
    assert.equal(api.runStatusLabel({ status: 'done' }), 'scheduler_run_status_done');
    assert.equal(api.runStatusLabel({ status: 'failed' }), 'scheduler_run_status_failed');
    assert.equal(api.runStatusLabel({ status: 'running' }), 'scheduler_run_status_running');
    // An unrecognised status is not shown as success.
    assert.equal(api.runStatusLabel({ status: 'weird' }), 'scheduler_run_status_unknown');
});

// ---------------------------------------------------------------------------
// Mounted behaviour
// ---------------------------------------------------------------------------

test('refresh loads instances, their recipients and the first history page', async () => {
    const { handle, root, calls } = mountScheduler({
        '/api/scheduler/instances': INSTANCES,
        '/api/scheduler/recipients': RECIPIENTS,
        '/api/scheduler/runs': { status: 'success', history_scope: 'attributed_only', runs: [] },
    }, ALL_OPEN);
    await handle.refresh();

    assert.deepEqual(calls.map((call) => call.path.split('?')[0]), [
        '/api/scheduler/instances',
        '/api/scheduler/recipients',
        '/api/scheduler/runs',
    ]);
    // The recipient query names the instance explicitly: the server narrows by
    // it, and the caller's granted range still applies on top.
    assert.equal(calls[1].path, '/api/scheduler/recipients?instance_id=i-1');
    const note = byClass(root, 'rdai-scheduler-scope');
    assert.equal(note.textContent, 'scheduler_history_scope');
});

test('a create posts once and refreshes without retrying', async () => {
    const { handle, root, calls } = mountScheduler({
        '/api/scheduler/instances': INSTANCES,
        '/api/scheduler/recipients': RECIPIENTS,
        '/api/scheduler/runs': EMPTY_RUNS,
        '/api/scheduler/create': { status: 'success', task: { id: 't-1' } },
    }, ALL_OPEN);
    await handle.refresh();

    byClass(root, 'rdai-scheduler-name').value = 'Daily';
    byClass(root, 'rdai-scheduler-content').value = 'hello';
    byClass(root, 'rdai-scheduler-schedule-value').value = '1h';
    click(byClass(root, 'rdai-scheduler-create'));
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));

    const creates = calls.filter((call) => call.path === '/api/scheduler/create');
    assert.equal(creates.length, 1);
    // `body` is the payload object, not a pre-serialized string: the console's
    // shared request wrapper owns the JSON encoding, so the module never
    // hand-builds one.
    const body = creates[0].options.body;
    assert.equal(creates[0].options.method, 'POST');
    assert.equal(body.name, 'Daily');
    assert.equal(body.action.content, 'hello');
    assert.equal(body.action.instance_id, 'i-1');
    assert.equal(body.action.receiver, 'user-1');
});

test('a refused create surfaces the reason and posts no retry', async () => {
    const failing = () => Promise.reject(Object.assign(new Error('quota exhausted'), { code: 'quota_exceeded' }));
    const doc = makeDocument();
    const root = makeNode(doc, 'div');
    let createCalls = 0;
    // A real bundle answers the key; the identity function below is what an
    // *unmapped* code looks like, so this asserts the mapped branch.
    const bundle = { scheduler_error_quota_exceeded: '超出配额' };
    const handle = api.mount({
        root,
        t: (key) => (key in bundle ? bundle[key] : key),
        getContext: () => ALL_OPEN,
        request: (path) => {
            if (path.indexOf('/api/scheduler/instances') === 0) return Promise.resolve(INSTANCES);
            if (path.indexOf('/api/scheduler/recipients') === 0) return Promise.resolve(RECIPIENTS);
            if (path.indexOf('/api/scheduler/runs') === 0) return Promise.resolve(EMPTY_RUNS);
            createCalls += 1;
            return failing();
        },
    });
    await handle.refresh();
    byClass(root, 'rdai-scheduler-name').value = 'Daily';
    byClass(root, 'rdai-scheduler-content').value = 'hello';
    byClass(root, 'rdai-scheduler-schedule-value').value = '1h';
    click(byClass(root, 'rdai-scheduler-create'));
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(createCalls, 1);
    const error = byClass(root, 'rdai-scheduler-error');
    assert.equal(error.textContent, '超出配额');
});

test('an unmapped refusal code falls back to the server message', async () => {
    const doc = makeDocument();
    const root = makeNode(doc, 'div');
    const handle = api.mount({
        root,
        t: (key) => key,
        getContext: () => ALL_OPEN,
        request: (path) => {
            if (path.indexOf('/api/scheduler/instances') === 0) return Promise.resolve(INSTANCES);
            if (path.indexOf('/api/scheduler/recipients') === 0) return Promise.resolve(RECIPIENTS);
            if (path.indexOf('/api/scheduler/runs') === 0) return Promise.resolve(EMPTY_RUNS);
            return Promise.reject(Object.assign(
                new Error('channel execution is closed'), { code: 'brand_new_code' }));
        },
    });
    await handle.refresh();
    byClass(root, 'rdai-scheduler-name').value = 'Daily';
    byClass(root, 'rdai-scheduler-content').value = 'hello';
    byClass(root, 'rdai-scheduler-schedule-value').value = '1h';
    click(byClass(root, 'rdai-scheduler-create'));
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));

    // Better a readable server sentence than a bare key the bundle never knew.
    assert.equal(byClass(root, 'rdai-scheduler-error').textContent,
                 'channel execution is closed');
});

test('delete asks first and removes the row only after the server confirms', async () => {
    const doc = makeDocument();
    const root = makeNode(doc, 'div');
    const confirmations = [];
    withSandboxWindow({
        RdaiFunctionalCapabilities: { available: () => true },
        showConfirmDialog: (options) => { confirmations.push(options); },
    });
    try {
        const handle = api.mount({
            root,
            t: (key) => key,
            getContext: () => ALL_OPEN,
            request: (path) => {
                if (path.indexOf('/api/scheduler/instances') === 0) return Promise.resolve(INSTANCES);
                if (path.indexOf('/api/scheduler/recipients') === 0) return Promise.resolve(RECIPIENTS);
                if (path.indexOf('/api/scheduler/runs/detail') === 0) {
                    return Promise.resolve({ status: 'success', run: { run_id: 'r1' } });
                }
                if (path.indexOf('/api/scheduler/runs') === 0) {
                    return Promise.resolve({
                        status: 'success',
                        history_scope: 'attributed_only',
                        runs: [{ run_id: 'r1', task_id: 't-1', status: 'done' }],
                    });
                }
                return Promise.resolve({ status: 'success' });
            },
        });
        await handle.refresh();
        const row = byClass(root, 'rdai-scheduler-run');
        assert.equal(row.attributes['data-run-id'], 'r1');

        click(byClass(root, 'rdai-scheduler-run-delete'));
        // Nothing happens until the user confirms.
        assert.equal(row.removed, false);
        assert.equal(confirmations.length, 1);
    } finally {
        withSandboxWindow({});
    }
});

test('a failed delete keeps the row visible', async () => {
    const doc = makeDocument();
    const root = makeNode(doc, 'div');
    withSandboxWindow({ RdaiFunctionalCapabilities: { available: () => true } });
    const bundle = { scheduler_error_run_not_found: '运行记录不存在' };
    try {
        const handle = api.mount({
            root,
            t: (key) => (key in bundle ? bundle[key] : key),
            getContext: () => ALL_OPEN,
            request: (path) => {
                if (path.indexOf('/api/scheduler/instances') === 0) return Promise.resolve(INSTANCES);
                if (path.indexOf('/api/scheduler/recipients') === 0) return Promise.resolve(RECIPIENTS);
                if (path === '/api/scheduler/runs/delete') {
                    return Promise.resolve({ status: 'error', code: 'run_not_found' });
                }
                if (path.indexOf('/api/scheduler/runs') === 0) {
                    return Promise.resolve({
                        status: 'success',
                        history_scope: 'attributed_only',
                        runs: [{ run_id: 'r1', status: 'done' }],
                    });
                }
                return Promise.resolve({ status: 'success' });
            },
        });
        await handle.refresh();
        // No confirm dialog is available in this sandbox, so the delete runs
        // straight through -- and still refuses to drop the row on failure.
        click(byClass(root, 'rdai-scheduler-run-delete'));
        await new Promise((resolve) => setImmediate(resolve));
        await new Promise((resolve) => setImmediate(resolve));

        assert.ok(byClass(root, 'rdai-scheduler-run'));
        const error = byClass(root, 'rdai-scheduler-error');
        // The server's code survives the round trip and maps to a real label,
        // rather than collapsing into a generic "delete failed".
        assert.equal(error.textContent, '运行记录不存在');
    } finally {
        withSandboxWindow({});
    }
});

test('a closed projection renders neither half and issues no request', async () => {
    const doc = makeDocument();
    const root = makeNode(doc, 'div');
    const calls = [];
    const handle = api.mount({
        root,
        t: (key) => key,
        // An old server, or a deployment that has not opened the batch: the
        // module must render nothing rather than two empty panels that read as
        // "you have no instances and no runs".
        getContext: () => ({ status: 'success', consumers: {} }),
        request: (path) => { calls.push(path); return Promise.resolve(EMPTY_RUNS); },
    });
    const root2 = makeNode(doc, 'div');
    void root2;
    await handle.refresh();

    assert.deepEqual(calls, []);
    assert.equal(byClass(root, 'rdai-scheduler-authoring'), null);
    assert.equal(byClass(root, 'rdai-scheduler-history'), null);
});

test('dispose stops a refresh that lands afterwards', async () => {
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    const doc = makeDocument();
    const root = makeNode(doc, 'div');
    const handle = api.mount({
        root,
        t: (key) => key,
        getContext: () => ALL_OPEN,
        request: () => gate,
    });
    const pending = handle.refresh();
    handle.dispose();
    release({ status: 'success', instances: [], runs: [] });
    await pending;

    // The root is empty and nothing was rendered into the detached tree: a
    // stale response must not repaint a view the user closed.
    assert.equal(root.removed, false);
    assert.equal(root.children.length, 0);
});
