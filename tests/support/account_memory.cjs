const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ROOT = path.join(__dirname, '../..');
const memorySource = fs.readFileSync(path.join(ROOT, 'channel/web/static/js/fork/views/memory.js'), 'utf8');
const consoleSource = fs.readFileSync(path.join(ROOT, 'channel/web/static/js/console.js'), 'utf8');
function boot(responses = []) {
    const nodes = new Map(), calls = [], toasts = [], confirms = [];
    function node(id) {
        if (!nodes.has(id)) nodes.set(id, {
            textContent: '', innerHTML: '', className: '', children: [], classes: new Set(),
            classList: {add(c) {node(id).classes.add(c);}, remove(c) {node(id).classes.delete(c);},
                toggle(c, on) {on ? this.add(c) : this.remove(c);}},
            replaceChildren() {this.children = []; this.innerHTML = '';},
            appendChild(child) {this.children.push(child);},
            querySelector: selector => node(id + selector),
            querySelectorAll: selector => [node(id + selector), node(id + selector + '2')],
        });
        return nodes.get(id);
    }
    let doc = null;
    const editor = {current: () => doc, forget: () => {doc = null;},
        guard: () => true, open: value => {doc = value;}};
    const sandbox = {
        console, URL, Request, Headers, AbortController, _authEpoch: 1,
        _accountState: {username: 'alice', displayName: 'Alice'}, currentLang: 'zh',
        activeAgentId: 'agent-x', sessionStorage: {getItem: () => 'tenant-a'},
        document: {getElementById: node, querySelectorAll: () => [], createElement: () => node('row' + Math.random())},
        window: {location: {href: 'http://localhost/admin', origin: 'http://localhost'}},
        t: key => key, escapeHtml: value => value, createDocEditor: cfg => {editor.config = cfg; return editor;},
        docRenderTitle() {}, docRenderBody() {}, _wsToast: message => toasts.push(message),
        showConfirmDialog: options => confirms.push(options),
        tenantSelectionHeader: url => url.startsWith('/api/') ? 'tenant-a' : '',
        _desktopContextForRequest: () => null,
        _nativeFetch: async (input, init) => {
            calls.push({url: input instanceof Request ? input.url : String(input), init,
                body: init?.body ? JSON.parse(init.body) : null});
            const next = responses.shift();
            if (next instanceof Error) throw next;
            return {json: async () => typeof next === 'function' ? next() : next};
        },
    };
    vm.createContext(sandbox);
    const from = consoleSource.indexOf('window.fetch = function(input, init)');
    const to = consoleSource.indexOf('\nfunction generateSessionId', from);
    vm.runInContext(consoleSource.slice(from, to), sandbox);
    sandbox.fetch = sandbox.window.fetch;
    vm.runInContext(memorySource, sandbox);
    return {sandbox, node, calls, toasts, confirms, editor,
        run: code => vm.runInContext(code, sandbox),
        doc: (extra = {}) => ({filename: 'notes.md', category: 'memory',
            owner: sandbox.memoryOwner(), ...extra})};
}
module.exports = {boot, ROOT, memorySource, consoleSource};
