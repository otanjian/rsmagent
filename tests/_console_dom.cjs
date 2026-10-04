// A minimal DOM for console frontend tests.
//
// The console's view modules are classic scripts that build their markup with
// `document.createElement`, so a test that wants to drive them needs more than
// the four-method stub the earlier suites used. Hand-rolling that per test file
// drifted: each copy answered `querySelector` for the one selector its own test
// needed, so a helper that started asking for a different selector silently got
// `null` and the assertion passed or failed for the wrong reason.
//
// This is deliberately not a DOM implementation. It supports exactly what the
// helpers under test use -- class selectors, `innerHTML = ''`, `dataset`,
// `classList`, `style`, appended children, and recorded event listeners -- and
// throws on anything else so a new usage is loud instead of silently inert.
//
// A bare-class selector ('.cfg-dropdown-item', '.a.b') is what every helper
// here queries; `matches()` is the one place that rule lives, and any other
// selector shape raises rather than quietly matching nothing.

function classListOf(el) {
    const classes = new Set();
    return {
        add: (...names) => names.forEach(n => classes.add(n)),
        remove: (...names) => names.forEach(n => classes.delete(n)),
        contains: name => classes.has(name),
        toggle: (name, force) => {
            const on = force === undefined ? !classes.has(name) : !!force;
            if (on) classes.add(name);
            else classes.delete(name);
            return on;
        },
        _set: classes,
    };
}

function isClassSelector(selector) {
    return typeof selector === 'string' && /^(\.[A-Za-z0-9_-]+)+$/.test(selector);
}

//: `#ws-file-list .ws-file-row.ws-checked` — one id scope, then classes. The
//: workspace panel scopes its row queries to the list element it owns, so the
//: helper has to understand that one shape or every selection query would throw.
function isScopedClassSelector(selector) {
    return typeof selector === 'string' && /^#[A-Za-z0-9_-]+\s+(\.[A-Za-z0-9_-]+)+$/.test(selector);
}

function matches(el, selector) {
    if (!isClassSelector(selector)) {
        throw new Error(`_console_dom supports class selectors only, got ${selector}`);
    }
    return selector.split('.').filter(Boolean).every(name => el.classList.contains(name));
}

/** Resolve a `#id .a.b` selector against `root`'s subtree, scoped by the id. */
function selectScoped(root, selector) {
    const [scope, classes] = selector.split(/\s+/);
    const id = scope.slice(1);
    const owners = [root, ...root.descendants()].filter(node => node.id === id);
    return owners.flatMap(owner => owner.descendants().filter(node => matches(node, classes)));
}

function createElement(document, tag = 'div') {
    const el = {
        tagName: String(tag).toUpperCase(),
        parentNode: null,
        children: [],
        dataset: {},
        style: {},
        attributes: {},
        listeners: {},
        textContent: '',
        value: '',
        disabled: false,
        _html: '',
        classList: classListOf(),
    };
    // `className` and `classList` are two views of one set, exactly as in the
    // DOM: code that assigns `el.className = 'a b'` (which the console does when
    // it builds rows) must be visible to a later `querySelector('.a')`.
    Object.defineProperty(el, 'className', {
        get: () => [...el.classList._set].join(' '),
        set: value => {
            el.classList._set.clear();
            String(value).split(/\s+/).filter(Boolean).forEach(name => el.classList._set.add(name));
        },
    });
    Object.defineProperty(el, 'innerHTML', {
        get: () => el._html,
        set: (html) => {
            el._html = String(html);
            // The real parser replaces the subtree; `children` staying stale
            // would let a test see rows that the page no longer has.
            el.children.forEach(child => { child.parentNode = null; });
            el.children = [];
        },
    });
    el.appendChild = child => {
        if (child.parentNode) child.parentNode.removeChild(child);
        child.parentNode = el;
        el.children.push(child);
        return child;
    };
    el.insertBefore = (child, before) => {
        const at = el.children.indexOf(before);
        if (at < 0) return el.appendChild(child);
        child.parentNode = el;
        el.children.splice(at, 0, child);
        return child;
    };
    el.removeChild = child => {
        const at = el.children.indexOf(child);
        if (at >= 0) el.children.splice(at, 1);
        child.parentNode = null;
        return child;
    };
    el.remove = () => { if (el.parentNode) el.parentNode.removeChild(el); };
    el.descendants = () => el.children.flatMap(child => [child, ...child.descendants()]);
    el.querySelector = selector => el.querySelectorAll(selector)[0] || null;
    el.querySelectorAll = selector => (isScopedClassSelector(selector)
        ? selectScoped(el, selector)
        : el.descendants().filter(node => matches(node, selector)));
    el.closest = selector => {
        let node = el;
        while (node) {
            if (matches(node, selector)) return node;
            node = node.parentNode;
        }
        return null;
    };
    el.setAttribute = (key, value) => { el.attributes[key] = String(value); };
    el.removeAttribute = key => { delete el.attributes[key]; };
    el.getAttribute = key => (key in el.attributes ? el.attributes[key] : null);
    el.addEventListener = (type, handler) => {
        (el.listeners[type] || (el.listeners[type] = [])).push(handler);
    };
    el.removeEventListener = (type, handler) => {
        const list = el.listeners[type] || [];
        const at = list.indexOf(handler);
        if (at >= 0) list.splice(at, 1);
    };
    /** Fire every listener registered for `type` with a synthesized event. */
    el.fire = (type, event = {}) => {
        const payload = {
            type, target: el, currentTarget: el, defaultPrevented: false,
            stopPropagation() { payload.propagated = false; },
            preventDefault() { payload.defaultPrevented = true; },
            ...event,
        };
        (el.listeners[type] || []).forEach(handler => handler(payload));
        return payload;
    };
    el.getBoundingClientRect = () => ({ top: 0, bottom: 300, left: 0, right: 200, width: 200, height: 24 });
    el.focus = () => { document.activeElement = el; };
    el.blur = () => {};
    // HTMLElement.click(): dispatches a click through the same listeners, so a
    // helper that commits a keyboard-highlighted row by calling `.click()`
    // exercises the row's real handler instead of a test-only shortcut.
    el.click = () => el.fire('click');
    return el;
}

/** Build a detached DOM root. `document` is what the sliced console sees. */
function createDocument() {
    const document = {
        activeElement: null,
        listeners: {},
        createElement: tag => createElement(document, tag),
        getElementById: id => document._byId.get(id) || null,
        querySelector: selector => document._root.querySelector(selector),
        querySelectorAll: selector => document._root.querySelectorAll(selector),
        addEventListener: (type, handler) => {
            (document.listeners[type] || (document.listeners[type] = [])).push(handler);
        },
        removeEventListener: () => {},
        _byId: new Map(),
        _root: null,
        /** Register an element under an id so `getElementById` finds it. */
        register(id, el) {
            document._byId.set(id, el);
            el.id = id;
            return el;
        },
    };
    document._root = createElement(document, 'body');
    document.body = document._root;
    document.documentElement = createElement(document, 'html');
    return document;
}

module.exports = { createDocument, createElement };
