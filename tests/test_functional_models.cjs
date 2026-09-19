// Web model-catalog + ordered chat-fallback chain editors
// (change integrate-upstream-core-capabilities, P3; implementation.md §5).
//
// The module is a plain IIFE that exposes exactly one global
// (window.RdaiFunctionalModels) and must load without a DOM. Pure data
// functions are unit-tested in a bare VM context; the mount* DOM code is
// driven through a tiny in-test DOM that records textContent/innerHTML so
// the XSS guarantee (data never becomes markup) is verifiable.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const { loadDictionaries } = require('./support/i18n_namespaces.cjs');

const ROOT = path.join(__dirname, '..');
const MODULE = path.join(ROOT, 'channel/web/static/js/functional-models.js');
const LANGS = ['zh', 'zh-Hant', 'en'];

function loadApi() {
    const loaded = { window: {} };
    vm.runInNewContext(fs.readFileSync(MODULE, 'utf8'), loaded);
    return loaded.window.RdaiFunctionalModels;
}

// Values produced inside the VM realm have a different Array/Object
// prototype, which deepStrictEqual rejects. Copy them into this realm first.
function plain(value) {
    return JSON.parse(JSON.stringify(value));
}

// ---------------------------------------------------------------------------
// the verbatim starting example from implementation.md §5
// ---------------------------------------------------------------------------
const sandbox = {window: {}};
vm.runInNewContext(
  fs.readFileSync('channel/web/static/js/functional-models.js', 'utf8'),
  sandbox
);
const api = sandbox.window.RdaiFunctionalModels;
test('catalog saves overrides without freezing untouched presets', () => {
  const result = api.buildCatalogPayload('zhipu', {
    seed: [{name: 'preset', capabilities: ['text']}],
    overrides: [{name: 'added', capabilities: ['text']}],
    hidden: []
  });
  assert.deepEqual(JSON.parse(JSON.stringify(result.models)), [
    {name: 'added', capabilities: ['text']}
  ]);
});

// ---------------------------------------------------------------------------
// tiny DOM
// ---------------------------------------------------------------------------

function makeFakeDom() {
    const created = [];

    function makeNode(tag) {
        const node = {
            tagName: String(tag).toUpperCase(),
            ownerDocument: doc,
            children: [],
            parentNode: null,
            className: '',
            style: {},
            dataset: {},
            disabled: false,
            checked: false,
            innerHTMLUsed: false,
            _text: '',
            _value: '',
            _html: '',
            _attrs: {},
            _handlers: {},
        };
        node.classList = {
            add() {},
            remove() {},
            toggle() {},
            contains() { return false; },
        };
        node.appendChild = function (child) {
            if (node._text) node._text = '';
            child.parentNode = node;
            node.children.push(child);
            return child;
        };
        node.removeChild = function (child) {
            const at = node.children.indexOf(child);
            if (at >= 0) {
                node.children.splice(at, 1);
                child.parentNode = null;
            }
            return child;
        };
        node.remove = function () {
            if (node.parentNode) node.parentNode.removeChild(node);
        };
        node.setAttribute = function (key, value) {
            node._attrs[key] = value;
            if (key === 'class') node.className = value;
        };
        node.getAttribute = function (key) {
            return Object.prototype.hasOwnProperty.call(node._attrs, key)
                ? node._attrs[key] : null;
        };
        node.removeAttribute = function (key) { delete node._attrs[key]; };
        node.addEventListener = function (type, handler) {
            (node._handlers[type] = node._handlers[type] || []).push(handler);
        };
        node.removeEventListener = function (type, handler) {
            const list = node._handlers[type] || [];
            const at = list.indexOf(handler);
            if (at >= 0) list.splice(at, 1);
        };
        node.dispatchEvent = function (event) {
            const evt = event || {};
            (node._handlers[evt.type] || []).slice().forEach(fn => fn.call(node, evt));
        };
        node.querySelector = function () { return null; };
        Object.defineProperty(node, 'textContent', {
            get() {
                if (node.children.length) {
                    return node.children.map(child => child.textContent).join('');
                }
                return node._text;
            },
            set(value) {
                node._text = value === null || value === undefined ? '' : String(value);
                node.children.length = 0;
            },
        });
        Object.defineProperty(node, 'value', {
            get() { return node._value; },
            set(value) { node._value = value === null || value === undefined ? '' : String(value); },
        });
        Object.defineProperty(node, 'innerHTML', {
            get() { return node._html; },
            set(value) {
                node._html = String(value);
                node.innerHTMLUsed = true;
            },
        });
        created.push(node);
        return node;
    }

    const doc = { createElement: makeNode };
    return { document: doc, created, create: tag => makeNode(tag) };
}

function fire(node, type) {
    node.dispatchEvent({ type });
}

function findCreated(dom, tagName) {
    return dom.created.filter(node => node.tagName === String(tagName).toUpperCase());
}

// ---------------------------------------------------------------------------
// fallback payload + chain round-trip
// ---------------------------------------------------------------------------

test('fallback payload keeps order, exact provider ids and rejects bad input', () => {
    const api = loadApi();
    const payload = api.buildFallbackPayload(true, [
        { provider: 'zhipu', model: 'model-a' },
        { provider: 'custom:fixture', model: 'model-b' },
    ]);
    assert.deepEqual(plain(payload), {
        action: 'set_capability',
        capability: 'chat_fallback',
        enabled: true,
        chain: [
            { provider: 'zhipu', model: 'model-a' },
            { provider: 'custom:fixture', model: 'model-b' },
        ],
    });

    assert.throws(() => api.buildFallbackPayload(true, []), /fallback/i,
        'an enabled empty chain is an input error');
    assert.throws(() => api.buildFallbackPayload(false, [{ provider: 'zhipu', model: '' }]),
        /provider and a model/i, 'incomplete nodes are rejected even while disabled');
    assert.throws(() => api.buildFallbackPayload(false, [{ provider: '', model: 'x' }]),
        /provider and a model/i);

    // Disabling never collapses a configured chain to one node.
    const disabled = api.buildFallbackPayload(false, [
        { provider: 'a', model: '1' },
        { provider: 'b', model: '2' },
    ]);
    assert.equal(disabled.enabled, false);
    assert.equal(disabled.chain.length, 2);
});

test('chain reorder survives a save/reload round-trip through mountFallback', async () => {
    const api = loadApi();
    const dom = makeFakeDom();
    const root = dom.create('div');
    const providers = [
        { id: 'zhipu', label: 'Zhipu' },
        { id: 'openai', label: 'OpenAI' },
        { id: 'custom:fixture', label: 'Fixture' },
    ];
    const capability = {
        enabled: true,
        chain: [
            { provider: 'zhipu', model: 'model-a' },
            { provider: 'openai', model: 'model-b' },
            { provider: 'custom:fixture', model: 'model-c' },
        ],
        providers,
    };
    let posted = null;
    let stored = capability.chain.slice();
    const handle = api.mountFallback({
        root,
        capability,
        providers,
        t: key => key,
        save: payload => {
            posted = payload;
            stored = payload.chain.slice(); // "server" accepts the new order
            return Promise.resolve({ status: 'success', applied: payload });
        },
        // GET /api/models re-read after the successful POST
        reload: () => Promise.resolve({ enabled: true, chain: stored }),
    });

    handle.moveLink(2, 0); // custom:fixture to the front
    const ok = await handle.save();

    assert.equal(ok, true);
    assert.deepEqual(plain(posted.chain.map(link => link.provider)),
        ['custom:fixture', 'zhipu', 'openai']);
    assert.deepEqual(plain(handle.getState().chain.map(link => link.provider)),
        ['custom:fixture', 'zhipu', 'openai'],
        'the reloaded order is what the editor keeps');
    handle.dispose();
});

test('disabling the fallback keeps every node', async () => {
    const api = loadApi();
    const dom = makeFakeDom();
    const root = dom.create('div');
    let posted = null;
    const handle = api.mountFallback({
        root,
        capability: {
            enabled: true,
            chain: [{ provider: 'a', model: '1' }, { provider: 'b', model: '2' }],
            providers: [{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }],
        },
        t: key => key,
        save: payload => { posted = payload; return Promise.resolve(payload); },
    });
    handle.setEnabled(false);
    await handle.save();
    assert.equal(posted.enabled, false);
    assert.equal(posted.chain.length, 2, 'disabled chains keep their node config');
    handle.dispose();
});

// ---------------------------------------------------------------------------
// catalog draft semantics
// ---------------------------------------------------------------------------

function fixtureDraft() {
    return {
        seed: [
            { name: 'preset', capabilities: ['text', 'vision'], context_window: 1000, max_output_tokens: 100 },
            { name: 'other', capabilities: ['text'] },
        ],
        overrides: [],
        hidden: [],
    };
}

test('editing a preset writes an override that preserves un-edited fields', () => {
    const api = loadApi();
    const draft = api.editModel(fixtureDraft(), 'preset', { capabilities: ['text'] });
    assert.deepEqual(plain(draft.overrides), [{
        name: 'preset',
        capabilities: ['text'],
        context_window: 1000,
        max_output_tokens: 100,
    }]);
});

test('delete preset tombstones it; restore un-hides; delete added drops only the override', () => {
    const api = loadApi();
    let draft = fixtureDraft();

    draft = api.editModel(draft, 'preset', { context_window: 2000 });
    assert.equal(draft.overrides.length, 1);

    draft = api.deleteModel(draft, 'preset');
    assert.deepEqual(plain(draft.hidden), ['preset']);
    assert.equal(draft.overrides.length, 0, 'deleting a preset clears its override');
    assert.ok(!api.catalogView(draft).visible.some(row => row.name === 'preset'));

    draft = api.restoreModel(draft, 'preset');
    assert.deepEqual(plain(draft.hidden), []);
    assert.ok(api.catalogView(draft).visible.some(row => row.name === 'preset'));

    draft = api.addModel(draft, { name: 'added', capabilities: ['vision'] });
    assert.ok(api.catalogView(draft).visible.some(row => row.name === 'added'));
    draft = api.deleteModel(draft, 'added');
    assert.deepEqual(plain(draft.hidden), [], 'an added model leaves no tombstone');
    assert.ok(!api.catalogView(draft).visible.some(row => row.name === 'added'));
});

test('restore default clears both overrides and hidden', () => {
    const api = loadApi();
    let draft = api.editModel(fixtureDraft(), 'preset', { context_window: 2000 });
    draft = api.deleteModel(draft, 'other');
    assert.ok(draft.overrides.length > 0 && draft.hidden.length > 0);
    draft = api.resetDefault(draft);
    assert.deepEqual(plain(draft.overrides), []);
    assert.deepEqual(plain(draft.hidden), []);
    assert.equal(api.buildCatalogPayload('zhipu', draft).models.length, 0);
});

test('rename is delete-old plus add-new', () => {
    const api = loadApi();
    const draft = api.renameModel(fixtureDraft(), 'preset', 'renamed');
    assert.deepEqual(plain(draft.hidden), ['preset']);
    assert.ok(draft.overrides.some(entry => entry.name === 'renamed'));
    assert.ok(!api.catalogView(draft).visible.some(row => row.name === 'preset'));
    assert.ok(api.catalogView(draft).visible.some(row => row.name === 'renamed'));
    assert.throws(() => api.renameModel(draft, 'other', 'renamed'), /duplicate/i);
});

test('catalog rejects duplicate names, bad limits and unknown capabilities', () => {
    const api = loadApi();
    const draft = { seed: [], overrides: [], hidden: [] };
    assert.throws(() => api.buildCatalogPayload('zhipu', {
        seed: [], overrides: [{ name: 'a' }, { name: 'a' }], hidden: [],
    }), /duplicate/i);
    assert.throws(() => api.buildCatalogPayload('zhipu', {
        seed: [], overrides: [{ name: 'a', context_window: 0 }], hidden: [],
    }), /positive integer/i);
    assert.throws(() => api.buildCatalogPayload('zhipu', {
        seed: [], overrides: [{ name: 'a', max_output_tokens: -5 }], hidden: [],
    }), /positive integer/i);
    assert.throws(() => api.buildCatalogPayload('zhipu', {
        seed: [], overrides: [{ name: 'a', capabilities: ['quantum'] }], hidden: [],
    }), /unknown capability/i);
    assert.throws(() => api.buildCatalogPayload('', draft), /provider id/i);
});

test('catalog payload keeps custom provider ids and hidden tombstones', () => {
    const api = loadApi();
    const payload = api.buildCatalogPayload('custom:fixture', {
        seed: [{ name: 'preset', capabilities: ['text'] }],
        overrides: [{ name: 'mine', capabilities: ['text'], context_window: 32000 }],
        hidden: ['preset', 'preset'],
    });
    assert.equal(payload.provider_id, 'custom:fixture');
    assert.deepEqual(plain(payload.models), [{
        name: 'mine', capabilities: ['text'], context_window: 32000,
    }]);
    assert.deepEqual(plain(payload.hidden), ['preset']);
    assert.equal(payload.action, 'save_catalog');
});

// ---------------------------------------------------------------------------
// failure keeps the draft
// ---------------------------------------------------------------------------

test('a failed save keeps the catalog draft and surfaces the error', async () => {
    const api = loadApi();
    const dom = makeFakeDom();
    const root = dom.create('div');
    const handle = api.mountCatalog({
        root,
        provider: { id: 'zhipu', seed: fixtureDraft().seed, catalog: [], hidden: [] },
        t: key => key,
        save: () => Promise.reject(new Error('boom')),
    });

    handle.editModel('preset', { context_window: 12345 });
    handle.addModel({ name: 'added', capabilities: ['vision'] });
    const ok = await handle.save();

    assert.equal(ok, false);
    const state = handle.getState();
    assert.ok(state.error, 'the failure is surfaced');
    const preset = state.draft.overrides.find(entry => entry.name === 'preset');
    assert.equal(preset.context_window, 12345, 'the edited draft survives the failure');
    assert.ok(state.draft.overrides.some(entry => entry.name === 'added'));
    handle.dispose();
});

test('a failed fallback save keeps the chain and surfaces the error', async () => {
    const api = loadApi();
    const dom = makeFakeDom();
    const root = dom.create('div');
    const handle = api.mountFallback({
        root,
        capability: {
            enabled: true,
            chain: [{ provider: 'a', model: '1' }],
            providers: [{ id: 'a', label: 'A' }],
        },
        t: key => key,
        save: () => Promise.reject(new Error('nope')),
    });
    handle.addLink({ provider: 'b', model: '2' });
    const ok = await handle.save();
    assert.equal(ok, false);
    assert.ok(handle.getState().error);
    assert.equal(handle.getState().chain.length, 2);
    handle.dispose();
});

// ---------------------------------------------------------------------------
// XSS
// ---------------------------------------------------------------------------

test('escapeHtml neutralizes markup', () => {
    const api = loadApi();
    const escaped = api.escapeHtml('<img src=x onerror="alert(1)">');
    assert.ok(!escaped.includes('<'), 'no raw angle bracket survives');
    assert.ok(escaped.includes('&lt;img'), 'the tag is escaped');
});

test('mount renderers never turn model/provider data into markup', () => {
    const api = loadApi();
    const evil = '<img src=x onerror="window.__xss=1">';

    const domF = makeFakeDom();
    const rootF = domF.create('div');
    const handleF = api.mountFallback({
        root: rootF,
        capability: {
            enabled: true,
            chain: [{ provider: 'custom:fixture', model: evil }],
            providers: [{ id: 'custom:fixture', label: evil }],
        },
        t: key => key,
        save: () => Promise.resolve({}),
    });
    assert.ok(domF.created.some(node => node.textContent.includes(evil)),
        'the label is rendered as text');
    assert.equal(findCreated(domF, 'img').length, 0, 'no <img> element was created');
    assert.ok(domF.created.every(node => !node.innerHTMLUsed), 'innerHTML is never used');
    handleF.dispose();

    const domC = makeFakeDom();
    const rootC = domC.create('div');
    const handleC = api.mountCatalog({
        root: rootC,
        provider: {
            id: 'zhipu',
            seed: [{ name: evil, capabilities: ['text'] }],
            catalog: [],
            hidden: [],
        },
        t: key => key,
        save: () => Promise.resolve({}),
    });
    const names = findCreated(domC, 'input').map(node => node.value);
    assert.ok(names.includes(evil), 'the malicious name is a plain input value');
    assert.equal(findCreated(domC, 'img').length, 0);
    assert.ok(domC.created.every(node => !node.innerHTMLUsed));
    handleC.dispose();
});

// ---------------------------------------------------------------------------
// i18n
// ---------------------------------------------------------------------------

test('every new editor string exists in zh, zh-Hant and en', () => {
    const dict = loadDictionaries();
    const keys = [
        'models_fallback_add', 'models_fallback_remove', 'models_fallback_move_up',
        'models_fallback_move_down', 'models_fallback_chain_empty',
        'models_fallback_incomplete', 'models_catalog_modal_title', 'models_catalog_add',
        'models_catalog_add_placeholder', 'models_catalog_name', 'models_catalog_capabilities',
        'models_catalog_context_window', 'models_catalog_max_output',
        'models_catalog_origin_preset', 'models_catalog_origin_added',
        'models_catalog_hidden_title', 'models_catalog_restore', 'models_catalog_reset',
        'models_catalog_editor_empty', 'models_catalog_duplicate', 'models_catalog_name_required',
        'models_catalog_invalid_limit', 'models_catalog_unknown_capability',
        'models_cap_tag_text', 'models_cap_tag_vision', 'models_cap_tag_video',
        'models_cap_tag_image', 'models_cap_tag_embedding', 'models_cap_tag_asr',
        'models_cap_tag_tts',
    ];
    for (const lang of LANGS) {
        assert.ok(dict[lang], `${lang} dictionary is loaded`);
        for (const key of keys) {
            assert.equal(typeof dict[lang][key], 'string', `${lang}/${key} exists`);
            assert.ok(dict[lang][key].trim().length > 0, `${lang}/${key} is non-empty`);
        }
    }
});
