// Harness for the ported scheduled-task page.
//
// The page is three layers sharing one global scope, in chat.html's load order:
// the upstream fragments' scripts (static/js/views/tasks.js and
// tasks-modal.js), console.js's helpers, and the fork patch module
// (static/js/fork/tasks-console.js) that overrides some of both. A test that
// inspected one layer's source could not see the seams between them -- which is
// where this page's bugs live (a `let`-vs-`let` collision is a page-killing
// SyntaxError, a `function`-vs-`function` one is a silent shadow, and the patch
// only works because it loads last).
//
// So this harness evaluates the real files in that order inside one VM context
// against a small DOM, with the leaf helpers console.js would provide. The
// pieces that carry the rules under test -- initDropdown, getDropdownValue,
// _featureAvailable -- are extracted from console.js itself rather than stubbed,
// so a change to the seam shows up here instead of in a stub that agrees with
// whatever the test expects.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const WEB = path.join(__dirname, '..', 'channel', 'web');
const read = relative => fs.readFileSync(path.join(WEB, relative), 'utf8');

const SOURCES = {
    upstreamTasks: 'static/js/views/tasks.js',
    upstreamModal: 'static/js/views/tasks-modal.js',
    patch: 'static/js/fork/tasks-console.js',
    consoleJs: 'static/js/console.js',
};

const source = Object.fromEntries(
    Object.entries(SOURCES).map(([key, relative]) => [key, read(relative)]));

// Extract one top-level `function name(...) { ... }` by brace matching: console.js
// is ~19k lines and pulling a neighbouring section in would drag half the console
// into the sandbox.
function fnSource(name, from = source.consoleJs) {
    const head = `function ${name}(`;
    const start = from.indexOf(head);
    if (start < 0) throw new Error(`Missing ${name}`);
    const isAsync = from.slice(Math.max(0, start - 6), start) === 'async ';
    let depth = 0;
    for (let i = from.indexOf('{', start); i < from.length; i++) {
        if (from[i] === '{') depth++;
        else if (from[i] === '}') {
            depth--;
            if (depth === 0) return from.slice(isAsync ? start - 6 : start, i + 1);
        }
    }
    throw new Error(`Unbalanced ${name}`);
}

// A stand-in element: only what the page's two halves actually touch. Selector
// lookups are memoized per selector so a control's `.cfg-dropdown-text` keeps
// being the same node, which is what initDropdown paints into.
function element(classes = ['hidden']) {
    const owned = new Set(classes);
    const found = new Map();
    const many = new Map();
    const el = {
        innerHTML: '', textContent: '', value: '', checked: false,
        disabled: false, style: {}, dataset: {}, children: [], className: '',
        classList: {
            add: (...names) => names.forEach(n => owned.add(n)),
            remove: (...names) => names.forEach(n => owned.delete(n)),
            contains: name => owned.has(name),
            toggle: (name, on) => (on === undefined
                ? (owned.has(name) ? owned.delete(name) : owned.add(name))
                : (on ? owned.add(name) : owned.delete(name))),
        },
        listeners: {},
        addEventListener(type, handler) { (el.listeners[type] ||= []).push(handler); },
        removeEventListener() {},
        setAttribute(name, value) { el.dataset['attr_' + name] = value; },
        getAttribute(name) { return el.dataset['attr_' + name]; },
        removeAttribute() {},
        focus() {},
        closest() { return null; },
        // One node per selector, created on first ask: initDropdown looks up the
        // trigger's text/menu/selected/face nodes and paints into them.
        querySelector(sel) {
            if (!found.has(sel)) found.set(sel, element([]));
            return found.get(sel);
        },
        // The records pane's empty state has two paragraphs (the message and the
        // "records show up here" guide) that the refusal path replaces
        // separately, so those exist as real siblings.
        querySelectorAll(sel) {
            if (!many.has(sel)) many.set(sel, sel === 'p' ? [element([]), element([])] : []);
            return many.get(sel);
        },
        appendChild(child) { el.children.push(child); return child; },
        remove() { el.removed = true; },
        replaceChildren() { el.children = []; },
        getBoundingClientRect: () => ({ top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }),
    };
    return el;
}

// One VM context = one page load. `boot()` returns the sandbox, a node getter,
// and the request log so a test can assert what the page asked the server for.
function boot(options = {}) {
    const nodes = new Map();
    const requests = [];
    const answers = options.answers || {};
    const features = options.features || {};
    const context = options.context || {
        feature_actions: Object.fromEntries(
            Object.entries(features).map(([key, available]) => [key, { available: !!available }])),
    };

    const node = id => {
        if (!nodes.has(id)) {
            const created = element();
            created.id = id;
            nodes.set(id, created);
        }
        return nodes.get(id);
    };

    const sandbox = {
        console,
        // Leaf helpers console.js would provide. The rules under test live in the
        // extracted functions below, not here.
        t: key => key,
        currentLang: options.lang || 'zh',
        escapeHtml: x => String(x),
        renderMarkdown: x => String(x),
        agentAvatarHTML: () => '',
        findAgent: () => null,
        multiAgentMode: () => false,
        agentCatalog: [],
        loadAgentCatalog: async () => [],
        defaultAgentId: '',
        withAuthenticatedFetch: () => {},
        // Auto-confirming: the confirm dialog is not what these cases are about,
        // and the flows below continue from what its onConfirm does.
        showConfirmDialog: ({ onConfirm } = {}) => { if (onConfirm) onConfirm(); },
        _baseAuthContext: () => context,
        document: {
            getElementById: node,
            querySelector: () => element([]),
            querySelectorAll: () => [],
            createElement: () => element([]),
            addEventListener() {},
            body: element([]),
        },
        fetch: (url, opts) => {
            const body = opts && opts.body ? JSON.parse(opts.body) : null;
            requests.push({ url: String(url), body });
            const match = Object.keys(answers).find(prefix => String(url).startsWith(prefix));
            return Promise.resolve({
                ok: true,
                status: 200,
                json: async () => (match ? answers[match] : { status: 'success', tasks: [], runs: [] }),
            });
        },
    };
    sandbox.window = sandbox;
    // The capability projection module is what _featureAvailable delegates to.
    sandbox.RdaiFunctionalCapabilities = {
        available: (ctx, key) => Boolean(ctx && ctx.feature_actions && ctx.feature_actions[key]
            && ctx.feature_actions[key].available === true),
    };
    vm.createContext(sandbox);
    // The identity markers other console loaders capture before a request; kept
    // as declarations so a test can move them the way a tenant switch does.
    vm.runInContext('let _authEpoch = 0, _authContextSeq = 0;', sandbox);
    vm.runInContext([
        fnSource('initDropdown'), fnSource('getDropdownValue'), fnSource('_featureAvailable'),
    ].join('\n'), sandbox);
    // Load order from chat.html: upstream scripts, console.js, then the patch.
    vm.runInContext(source.upstreamTasks, sandbox, { filename: SOURCES.upstreamTasks });
    vm.runInContext(source.upstreamModal, sandbox, { filename: SOURCES.upstreamModal });
    vm.runInContext(source.patch, sandbox, { filename: SOURCES.patch });

    return {
        sandbox,
        get: node,
        requests,
        // A tenant switch / logout moves these; the page must drop what it has
        // in flight rather than paint it into the new identity's page.
        moveIdentity() { vm.runInContext('++_authContextSeq; ++_authEpoch;', sandbox); },
        // The capability projection the page reads per action.
        setFeatures(next) {
            Object.assign(context.feature_actions ||= {}, Object.fromEntries(
                Object.entries(next).map(([key, available]) => [key, { available: !!available }])));
        },
    };
}

const flush = () => new Promise(resolve => setImmediate(resolve));

module.exports = { boot, element, fnSource, flush, source, SOURCES };
