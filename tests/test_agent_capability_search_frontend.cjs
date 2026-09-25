// The 能力 tab's keyword box narrows both catalogues (skills and tools) with a
// fuzzy, case-insensitive match, without changing what is bound.
//
// The contract this locks down:
//   1. one box, above both sections, matching freely over skills (name/display
//      name/description) and tools (name/description);
//   2. a keyword hides rows but never the selection behind them -- toggling a
//      visible row must still submit the skills the keyword is hiding;
//   3. "use every installed skill" and the tool master checkbox keep their
//      semantics: both describe the full catalogue, not the visible rows;
//   4. the keyword is per Agent (a switch clears it) but survives a repaint of
//      the same Agent (a tool toggle).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');
const code = source.slice(source.indexOf('const _capabilitySaveState ='),
    source.indexOf('function renderAgentTasksPane('));

const INPUT_RE = /<input\b([^>]*)>/g;

function fakeNode(extra = {}) {
    return Object.assign({
        textContent: '', value: '', checked: false, hidden: false, disabled: false,
        dataset: {}, _handlers: {},
        addEventListener(event, handler) { this._handlers[event] = handler; },
        removeEventListener() {},
        focus() {}, blur() {}, click() {},
        querySelector: () => null,
        querySelectorAll: () => [],
    }, extra);
}

/** Inputs declared by a markup string, as the renderer's own rows. */
function parseInputs(html) {
    return [...html.matchAll(INPUT_RE)].map(([, attrs]) => fakeNode({
        id: /id="([^"]*)"/.exec(attrs)?.[1] || '',
        className: /class="([^"]*)"/.exec(attrs)?.[1] || '',
        value: /value="([^"]*)"/.exec(attrs)?.[1],
        checked: /\bchecked\b/.test(attrs),
        disabled: /\bdisabled\b/.test(attrs),
    }));
}

function rowQuery(rows, selector) {
    const name = selector.slice(1).split(':')[0];
    if (name !== 'agent-skill-item' && name !== 'agent-tool-item') return [];
    return rows().filter(row => row.className === name
        && (!selector.endsWith(':checked') || row.checked));
}

/** A catalogue container the renderer paints rows into. */
function listNode() {
    const el = fakeNode();
    let html = '';
    el._rows = [];
    Object.defineProperty(el, 'innerHTML', {
        get() { return html; },
        set(value) { html = value; el._rows = parseInputs(value); },
        configurable: true,
    });
    el.querySelectorAll = (selector) => rowQuery(() => el._rows, selector);
    return el;
}

/** The pane: registers an id for every element its markup declares. */
function paneNode(nodes) {
    const el = fakeNode();
    Object.defineProperty(el, 'innerHTML', {
        get() { return el._html; },
        set(value) {
            el._html = value;
            el._rows = parseInputs(value);
            // Inputs keep their declared value/checked/disabled state; anything
            // else is a plain placeholder for the renderer's bindings.
            for (const row of el._rows) if (row.id && !nodes.has(row.id)) nodes.set(row.id, row);
            for (const match of value.matchAll(/id="([^"]+)"/g)) {
                if (!nodes.has(match[1])) nodes.set(match[1], fakeNode({ id: match[1] }));
            }
        },
        configurable: true,
    });
    el.querySelectorAll = (selector) => rowQuery(() => el._rows, selector);
    return el;
}

const CATALOG = [
    { name: 'knowledge-wiki', display_name: 'Knowledge Wiki', description: 'Manage the personal knowledge wiki.' },
    { name: 'image-generation', display_name: 'Image Generation', description: 'Generate or edit images from text prompts.' },
    { name: 'rfq-quote', display_name: 'RFQ Quote', description: '图纸询价智能报价，处理钣金与机柜。' },
];
const TOOLS = [
    { name: 'read', description: 'Read or inspect file contents.' },
    { name: 'write', description: 'Write content to a file.' },
    { name: 'scheduler', description: 'Create and manage scheduled tasks.' },
];

function setup({ agent, skills = CATALOG, tools = TOOLS }) {
    const nodes = new Map();
    const skillsList = listNode();
    const toolsList = listNode();
    const search = fakeNode({ id: 'agent-cap-search' });
    const clear = fakeNode({ id: 'agent-cap-search-clear', hidden: true });
    nodes.set('agent-skills-list', skillsList);
    nodes.set('agent-tools-list', toolsList);
    nodes.set('agent-cap-search', search);
    nodes.set('agent-cap-search-clear', clear);
    const pane = paneNode(nodes);
    nodes.set('agent-detail-skills', pane);
    const state = { agent };
    const ctx = {
        selectedAdminAgentId: agent.id, rosterRevision: 'r1',
        installedSkills: skills,
        installedTools: tools,
        findAgent: () => state.agent,
        t: key => key,
        escapeHtml: value => String(value),
        document: { getElementById: id => nodes.get(id) || null },
        fetch: async () => ({ json: async () => ({ status: 'success', revision: 'r2' }) }),
    };
    vm.createContext(ctx);
    vm.runInContext(code, ctx);

    const rows = (list) => list._rows.filter(row => row.className === 'agent-skill-item'
        || row.className === 'agent-tool-item');
    const ui = {
        ctx, state, pane, skillsList, toolsList, search, clear, nodes,
        render: () => ctx.renderAgentCapabilitiesPane(),
        skills: () => rows(skillsList).map(row => row.value),
        tools: () => rows(toolsList).map(row => row.value),
        checkedSkills: () => rows(skillsList).filter(row => row.checked).map(row => row.value),
        checkedTools: () => rows(toolsList).filter(row => row.checked).map(row => row.value),
        type: (value) => { search.value = value; search._handlers.input(); },
        clearBox: () => {
            assert.ok(typeof clear._handlers.click === 'function', 'the clear control must be wired');
            clear._handlers.click();
            assert.equal(search.value, '', 'clearing must empty the keyword box itself');
        },
        toggleSkill: (name) => {
            const row = rows(skillsList).find(r => r.value === name);
            assert.ok(row && !row.disabled, `${name} must be toggleable`);
            row.checked = !row.checked;
            row._handlers.change({ target: row });
        },
        toggleTool: (name) => {
            const row = rows(toolsList).find(r => r.value === name);
            assert.ok(row, `tool ${name} must be listed`);
            row.checked = !row.checked;
            row._handlers.change({ target: row });
        },
        master: (id) => nodes.get(id),
        setMaster: (id, checked) => {
            const box = nodes.get(id);
            box.checked = checked;
            box._handlers.change({ target: box });
        },
    };
    return ui;
}

const agent = (extra = {}) => ({ id: 'proc', name: '采购专员', skills: null, tools_allowlist: null, tools_denylist: [], ...extra });

/** Arrays produced inside the vm realm need lifting before a strict compare. */
const local = (value) => Array.from(value == null ? [] : value);

test('one keyword box sits above both catalogues', () => {
    const ui = setup({ agent: agent() });
    ui.render();
    const html = ui.pane.innerHTML;
    assert.ok(html.includes('id="agent-cap-search"'), 'the keyword box must be rendered');
    assert.ok(html.includes('agents_cap_search_placeholder'), 'the box must carry its i18n placeholder');
    const boxAt = html.indexOf('agent-cap-search"');
    assert.ok(boxAt < html.indexOf('agents_skills_label'), 'the box sits above the 技能 heading');
    assert.ok(boxAt < html.indexOf('agents_tools_label'), 'the box sits above the 工具目录 heading');
    assert.ok(boxAt < html.indexOf('agent-skills-all'), 'and above the use-all toggle');
    assert.ok(!html.includes('agent-skills-search'), 'no leftover second box');
    assert.deepEqual(ui.skills(), CATALOG.map(skill => skill.name), 'an empty keyword shows every skill');
    assert.deepEqual(ui.tools(), TOOLS.map(tool => tool.name), 'and every tool');
});

test('the keyword filters both catalogues fuzzily', () => {
    const ui = setup({ agent: agent() });
    ui.render();

    ui.type('kw');
    assert.deepEqual(ui.skills(), ['knowledge-wiki'], 'a subsequence must find knowledge-wiki');

    ui.type('knowledgewiki');
    assert.deepEqual(ui.skills(), ['knowledge-wiki'], 'separators must not defeat the match');

    ui.type('IMAGE');
    assert.deepEqual(ui.skills(), ['image-generation'], 'matching ignores case');

    ui.type('image gen');
    assert.deepEqual(ui.skills(), ['image-generation'], 'display-name substrings match');

    ui.type('text prompts');
    assert.deepEqual(ui.skills(), ['image-generation'], 'skill descriptions match as substrings');

    ui.type('询价');
    assert.deepEqual(ui.skills(), ['rfq-quote'], 'descriptions are matched in any language');

    ui.type(' iMgEn ');  // padded keyword: trimmed then matched
    assert.deepEqual(ui.skills(), ['image-generation'], 'the keyword is trimmed before matching');
});

test('the same keyword narrows the tool catalogue', () => {
    const ui = setup({ agent: agent() });
    ui.render();

    ui.type('rd');
    assert.deepEqual(ui.tools(), ['read'], 'a tool name matches by subsequence');
    assert.deepEqual(ui.skills(), [], 'the skill list is narrowed by the same keyword');

    ui.type('scheduled tasks');
    assert.deepEqual(ui.tools(), ['scheduler'], 'tool descriptions match as substrings');

    ui.type('WRITE');
    assert.deepEqual(ui.tools(), ['write'], 'tool matching ignores case');

    ui.type('');
    assert.deepEqual(ui.tools(), TOOLS.map(tool => tool.name), 'clearing restores every tool');
    assert.deepEqual(ui.skills(), CATALOG.map(skill => skill.name), 'and every skill');
});

test('each catalogue names its own empty state instead of a load failure', () => {
    const ui = setup({ agent: agent() });
    ui.render();
    ui.type('zzz-nope');
    assert.deepEqual(ui.skills(), []);
    assert.deepEqual(ui.tools(), []);
    assert.ok(ui.skillsList.innerHTML.includes('agents_skills_search_empty'),
        'a no-match hint is rendered for the skills');
    assert.ok(ui.toolsList.innerHTML.includes('agents_tools_search_empty'),
        'a no-match hint is rendered for the tools');
    assert.ok(!ui.skillsList.innerHTML.includes('agents_skills_all'),
        'no match must not be reported as an empty or unreadable catalogue');
    assert.equal(ui.clear.hidden, false, 'the clear control is offered while a keyword is set');

    ui.clearBox();
    assert.deepEqual(ui.skills(), CATALOG.map(skill => skill.name), 'clearing restores the skills');
    assert.deepEqual(ui.tools(), TOOLS.map(tool => tool.name), 'and the tools');
    assert.equal(ui.clear.hidden, true, 'and hides the clear control again');
});

test('filtering never drops skill selections the keyword is hiding', () => {
    const ui = setup({ agent: agent({ skills: ['knowledge-wiki', 'image-generation', 'rfq-quote'] }) });
    ui.render();
    assert.deepEqual(ui.checkedSkills(), ['knowledge-wiki', 'image-generation', 'rfq-quote']);

    ui.type('image');
    assert.deepEqual(ui.skills(), ['image-generation'], 'only the matching row stays visible');
    ui.toggleSkill('image-generation');

    assert.deepEqual(local(ui.state.agent.skills), ['knowledge-wiki', 'rfq-quote'],
        'the hidden selections must survive the toggle');

    ui.type('');
    assert.deepEqual(ui.checkedSkills(), ['knowledge-wiki', 'rfq-quote']);
});

test('a skill checked out of the filtered result joins the subset', () => {
    const ui = setup({ agent: agent({ skills: [] }) });
    ui.render();
    assert.deepEqual(ui.skills(), CATALOG.map(skill => skill.name), 'an explicit empty subset still lists the catalogue');
    assert.deepEqual(ui.checkedSkills(), []);

    ui.type('rfq');
    assert.deepEqual(ui.skills(), ['rfq-quote']);
    ui.toggleSkill('rfq-quote');
    assert.deepEqual(local(ui.state.agent.skills), ['rfq-quote']);

    ui.type('');
    assert.deepEqual(ui.checkedSkills(), ['rfq-quote']);
});

test('a bound skill that left the catalog is kept when another row is toggled', () => {
    const ui = setup({ agent: agent({ skills: ['knowledge-wiki', 'gone-skill'] }) });
    ui.render();
    ui.toggleSkill('knowledge-wiki');
    assert.deepEqual(local(ui.state.agent.skills), ['gone-skill'],
        'uninstalled but still bound names must not be silently dropped');
});

test('use-every-skill keeps its null semantics under a keyword', () => {
    const ui = setup({ agent: agent({ skills: null }) });
    ui.render();
    assert.equal(ui.master('agent-skills-all').checked, true, 'the use-all toggle starts checked');
    assert.ok(ui.skillsList._rows.every(row => row.disabled && row.checked), 'the whole catalogue shows as selected');

    ui.type('image');
    assert.deepEqual(ui.skills(), ['image-generation']);
    assert.equal(ui.state.agent.skills, null, 'filtering must not write an explicit subset');

    ui.setMaster('agent-skills-all', false);
    assert.deepEqual(local(ui.state.agent.skills), [], 'turning use-all off starts from an empty subset');
    assert.deepEqual(ui.skills(), ['image-generation'], 'the keyword still filters after the toggle');
    assert.deepEqual(ui.checkedSkills(), [], 'the rows are no longer force-checked');
    assert.ok(ui.skillsList._rows.every(row => !row.disabled), 'the rows become editable');

    ui.type('');
    assert.deepEqual(ui.skills(), CATALOG.map(skill => skill.name));
    ui.toggleSkill('rfq-quote');
    assert.deepEqual(local(ui.state.agent.skills), ['rfq-quote']);
});

test('the tool master checkbox describes the catalogue, not the visible rows', () => {
    const ui = setup({ agent: agent() });
    ui.render();
    assert.equal(ui.master('agent-tools-all').checked, true, 'inherited defaults select every tool');
    assert.deepEqual(ui.checkedTools(), TOOLS.map(tool => tool.name));

    ui.type('rd');
    assert.deepEqual(ui.tools(), ['read']);
    assert.equal(ui.master('agent-tools-all').checked, true,
        'filtering must not make the tool master look unchecked');
    assert.deepEqual(ui.checkedTools(), ['read'], 'only the visible row is checked');

    ui.toggleTool('read');
    assert.deepEqual(local(ui.state.agent.tools_denylist), ['read'], 'the visible row still saves on its own');
});

test('the keyword is per Agent and survives a repaint of the same Agent', () => {
    const second = agent({ id: 'sales', name: '销售', skills: null });
    const ui = setup({ agent: agent() });
    ui.render();
    ui.type('read');
    assert.deepEqual(ui.tools(), ['read']);

    // A tool toggle repaints the whole pane for the same Agent.
    ui.toggleTool('read');
    assert.deepEqual(ui.tools(), ['read'], 'the keyword survives a same-Agent repaint');
    assert.ok(/id="agent-cap-search"[^>]*value="read"/.test(ui.pane.innerHTML),
        'the repainted box carries the keyword back');
    assert.deepEqual(local(ui.state.agent.tools_denylist), ['read'], 'the toggle itself still saved');

    // Switching Agents starts from the full catalogue again.
    ui.state.agent = second;
    ui.ctx.selectedAdminAgentId = second.id;
    ui.render();
    assert.deepEqual(ui.skills(), CATALOG.map(skill => skill.name), 'a switch clears the keyword');
    assert.ok(/id="agent-cap-search"[^>]*value=""/.test(ui.pane.innerHTML), 'the box is empty again');
});

test('the box is a plain text input rendered outside both row containers', () => {
    const ui = setup({ agent: agent() });
    ui.render();
    const box = /<input[^>]*id="agent-cap-search"[^>]*>/.exec(ui.pane.innerHTML);
    assert.ok(box, 'the keyword box is a single input');
    assert.match(box[0], /type="text"/, 'a text box, not a browser search/retype control');
    // Repainting rows must not rebuild the box: it lives outside both lists.
    ui.type('kw');
    assert.ok(!ui.skillsList.innerHTML.includes('agent-cap-search'),
        'the skill list container never holds the keyword box');
    assert.ok(!ui.toolsList.innerHTML.includes('agent-cap-search'),
        'the tool list container never holds the keyword box');
});
