// The knowledge page's Agent dropdown gets an opt-in filter box (change
// add-traceable-knowledge-ingestion, task 3.1).
//
// The rule that makes this safe to add to `initDropdown` -- which twenty other
// controls call -- is that the box is opt-in: a call site that does not ask for
// it must render exactly the rows it rendered before. The filter itself only
// narrows the options the caller already handed over (the authorised
// `agentCatalog`), never asks the server for more, and never changes the
// selection: typing is a way to find an Agent, not a way to switch to one.
//
// The other half of the contract is what the box must NOT break: the input node
// survives a re-render (so the caret and an IME composition are not destroyed),
// and keyboard/IME behaviour matches what a Chinese input method needs.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const { createDocument } = require('./_console_dom.cjs');

const source = fs.readFileSync(
    path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');

function slice(from, to) {
    const start = source.indexOf(from);
    const end = source.indexOf(to, start);
    assert.ok(start >= 0 && end > start, `slice not found: ${from} .. ${to}`);
    return source.slice(start, end);
}

/** The dropdown builder, plus a stubbed avatar painter it calls for Agent rows. */
function loadDropdown() {
    const document = createDocument();
    const changes = [];
    const ctx = {
        t: key => key, // identity: assert on the raw key, so this is locale-agnostic
        agentAvatarHTML: (agent, size) => `<avatar data-id="${agent && agent.id}" data-size="${size}">`,
        window: { innerHeight: 900 },
        document,
    };
    vm.createContext(ctx);
    vm.runInContext(
        slice('// --- Opt-in dropdown filter ---', 'function getDropdownValue'), ctx);
    return { ctx, document, changes };
}

function dropdown(document) {
    const el = document.createElement('div');
    el.classList.add('cfg-dropdown');
    const selected = document.createElement('div');
    selected.classList.add('cfg-dropdown-selected');
    const face = document.createElement('div');
    face.classList.add('cfg-dropdown-face');
    const text = document.createElement('div');
    text.classList.add('cfg-dropdown-text');
    const menu = document.createElement('div');
    menu.classList.add('cfg-dropdown-menu');
    selected.appendChild(face);
    selected.appendChild(text);
    el.appendChild(selected);
    el.appendChild(menu);
    document._root.appendChild(el); // so document.querySelectorAll('.cfg-dropdown') sees it
    return { el, menu, text };
}

const AGENTS = [
    { value: 'agent-knowledge', label: '知识库助手', agent: { id: 'agent-knowledge', name: '知识库助手' } },
    { value: 'agent-sales', label: 'Sales Copilot', agent: { id: 'agent-sales', name: 'Sales Copilot' } },
    { value: 'agent-hr', label: 'HR Bot', agent: { id: 'agent-hr', name: 'HR Bot' } },
];

function open(opts = {}) {
    const harness = loadDropdown();
    const parts = dropdown(harness.document);
    const picked = [];
    harness.ctx.initDropdown(
        parts.el, AGENTS, 'agent-sales', value => picked.push(value),
        { withAvatar: true, ...opts });
    return { ...harness, ...parts, picked };
}

const rowsOf = menu => menu.querySelectorAll('.cfg-dropdown-item');
const labelsOf = menu => rowsOf(menu).map(row => row.querySelector('.cfg-dropdown-label').textContent);

// `fire` only runs the listeners on one node; the browser keeps going up the
// tree and then runs the document-level handlers. Model that here, because the
// bug this guards is about propagation: a click inside an open dropdown must
// not reach the document handler that dismisses every open dropdown.
function clickAndBubble(harness, node) {
    let current = node;
    while (current) {
        const event = current.fire('click');
        if (event.propagated === false) return event;
        current = current.parentNode;
    }
    (harness.document.listeners.click || []).forEach(handler => handler({ target: node }));
    return null;
}

test('a searchable dropdown shows a filter box above the rows', () => {
    const { menu, el } = open({ searchable: true });
    const box = el.querySelector('.cfg-dropdown-search-input');
    assert.ok(box, 'the opt-in filter box is rendered inside the dropdown');
    assert.ok(box.closest('.cfg-dropdown-menu') === menu, 'it lives inside the menu, above the rows');
    assert.equal(menu.children[0].querySelector('.cfg-dropdown-search-input'), box,
        'and it is the first thing in the menu');
    assert.equal(labelsOf(menu).length, 3, 'all options are still listed');
});

test('a dropdown that does not opt in renders exactly what it did before', () => {
    const { menu, el } = open();
    assert.equal(el.querySelector('.cfg-dropdown-search-input'), null, 'no filter box');
    assert.equal(menu.children.length, 3, 'the menu holds the rows and nothing else');
    assert.deepEqual(labelsOf(menu), ['知识库助手', 'Sales Copilot', 'HR Bot']);
});

test('the filter matches a full name or id, ignoring case and surrounding space', () => {
    const { el, menu } = open({ searchable: true });
    const box = el.querySelector('.cfg-dropdown-search-input');

    box.value = '  sales ';
    box.fire('input');
    assert.deepEqual(labelsOf(menu), ['Sales Copilot'], 'trim + case-insensitive on the name');

    box.value = 'AGENT-HR';
    box.fire('input');
    assert.deepEqual(labelsOf(menu), ['HR Bot'], 'the id is part of the candidate set too');
});

test('a CJK name matches by substring', () => {
    const { el, menu } = open({ searchable: true });
    const box = el.querySelector('.cfg-dropdown-search-input');
    box.value = '知识';
    box.fire('input');
    assert.deepEqual(labelsOf(menu), ['知识库助手']);
});

test('no match shows the empty label instead of an empty menu', () => {
    const { el, menu } = open({ searchable: true });
    const box = el.querySelector('.cfg-dropdown-search-input');
    box.value = 'nothing-here';
    box.fire('input');
    assert.deepEqual(labelsOf(menu), [], 'no rows');
    const empty = el.querySelector('.cfg-dropdown-empty');
    assert.ok(empty, 'a placeholder row is rendered');
    assert.equal(empty.classList.contains('hidden'), false);
    assert.equal(empty.textContent, 'knowledge_agent_search_empty');
});

test('a filter to nothing hides no option from the server and keeps the selection', () => {
    const { el, picked } = open({ searchable: true });
    const box = el.querySelector('.cfg-dropdown-search-input');
    box.value = 'nothing-here';
    box.fire('input');
    assert.deepEqual(picked, [], 'filtering is not a selection');
    assert.equal(el._ddValue, 'agent-sales', 'the current Agent is untouched');
});

test('clearing the query restores every option and hides the empty label', () => {
    const { el, menu } = open({ searchable: true });
    const box = el.querySelector('.cfg-dropdown-search-input');
    box.value = 'sales';
    box.fire('input');
    assert.deepEqual(labelsOf(menu), ['Sales Copilot']);

    box.value = '';
    box.fire('input');
    assert.deepEqual(labelsOf(menu), ['知识库助手', 'Sales Copilot', 'HR Bot']);
    assert.equal(el.querySelector('.cfg-dropdown-empty').classList.contains('hidden'), true);
});

test('picking a filtered row still selects it', () => {
    const { el, menu, picked, text } = open({ searchable: true });
    const box = el.querySelector('.cfg-dropdown-search-input');
    box.value = 'hr';
    box.fire('input');
    rowsOf(menu)[0].fire('click', { stopPropagation() {} });
    assert.deepEqual(picked, ['agent-hr']);
    assert.equal(text.textContent, 'HR Bot', 'the trigger paints the new choice');
});

test('the input node survives filtering, so the caret and query are not destroyed', () => {
    const { el, menu } = open({ searchable: true });
    const box = el.querySelector('.cfg-dropdown-search-input');
    // One keystroke at a time is the case that breaks a re-render-everything
    // implementation: rebuilding the input would drop the caret on each one.
    for (const value of ['s', 'sa', 'sal', 'sale', 'sales']) {
        box.value = value;
        box.fire('input');
    }
    assert.equal(el.querySelector('.cfg-dropdown-search-input'), box,
        'the same node is reused rather than rebuilt');
    assert.equal(box.value, 'sales');
    assert.deepEqual(labelsOf(menu), ['Sales Copilot'], 'and the query still applies');
});

test('clicking into the filter box does not dismiss the menu it lives in', () => {
    const harness = open({ searchable: true });
    const { el } = harness;
    el.classList.add('open');
    const box = el.querySelector('.cfg-dropdown-search-input');

    // Clicking the box is how typing starts. The click bubbles to a
    // document-level handler that closes every open dropdown, and unlike the
    // rows and the trigger, the box did not stop it -- so the menu (and the
    // caret) vanished before the user could type a single character.
    clickAndBubble(harness, box);

    assert.equal(el.classList.contains('open'), true,
        'the click that places the caret leaves the menu open');
    assert.equal(el.querySelector('.cfg-dropdown-search-input'), box,
        'the box is still there to type into');
});

test('an in-progress IME composition is not treated as a query', () => {
    const { el, menu } = open({ searchable: true });
    const box = el.querySelector('.cfg-dropdown-search-input');
    box.value = 'zhi';
    box.fire('input', { isComposing: true });
    assert.equal(labelsOf(menu).length, 3, 'composition updates do not filter mid-word');

    box.value = '知识';
    box.fire('compositionend');
    assert.deepEqual(labelsOf(menu), ['知识库助手'], 'the committed text filters');
});

test('arrow keys move a highlight and Enter picks the highlighted row', () => {
    const { el, menu, picked } = open({ searchable: true });
    const box = el.querySelector('.cfg-dropdown-search-input');
    box.value = 'agent';
    box.fire('input');
    assert.equal(rowsOf(menu).length, 3);

    box.fire('keydown', { key: 'ArrowDown', preventDefault() {} });
    assert.equal(rowsOf(menu)[0].classList.contains('cfg-dropdown-item-hl'), true);
    box.fire('keydown', { key: 'ArrowDown', preventDefault() {} });
    assert.equal(rowsOf(menu)[0].classList.contains('cfg-dropdown-item-hl'), false);
    assert.equal(rowsOf(menu)[1].classList.contains('cfg-dropdown-item-hl'), true);

    box.fire('keydown', { key: 'Enter', preventDefault() {} });
    assert.deepEqual(picked, ['agent-sales'], 'Enter commits the highlighted row');
});

test('Escape clears the query first, and closes only when it is already empty', () => {
    const { el } = open({ searchable: true });
    const box = el.querySelector('.cfg-dropdown-search-input');
    el.classList.add('open');
    box.value = 'sales';
    box.fire('input');
    box.fire('keydown', { key: 'Escape', preventDefault() {} });
    assert.equal(box.value, '', 'first Escape clears the query');
    assert.equal(el.classList.contains('open'), true, 'and leaves the menu open');

    box.fire('keydown', { key: 'Escape', preventDefault() {} });
    assert.equal(el.classList.contains('open'), false, 'second Escape closes it');
});

test('the knowledge Agent menu opts in and the page switches nothing by filtering', () => {
    const knowledge = slice('function renderKnowledgeAgentSelect', 'function selectKnowledgeAgent');
    assert.match(knowledge, /searchable:\s*true/,
        'renderKnowledgeAgentSelect asks for the filter box');
    assert.match(knowledge, /withAvatar:\s*true/, 'and keeps the avatar rows');
    const select = slice('function selectKnowledgeAgent', 'function canWriteKnowledge');
    assert.match(select, /loadKnowledgeView\(\)/, 'picking an Agent is still what reloads the view');
});
