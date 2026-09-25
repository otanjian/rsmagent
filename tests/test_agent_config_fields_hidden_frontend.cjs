// The console stopped offering three controls that had no management loop:
// the 分类 dropdown, the 关联场景 dropdown (概况 tab) and the SOP 流程 block
// (能力 tab). The persisted fields they wrote are untouched, so the contract
// this file locks down is twofold:
//
//   1. the detail pane no longer renders those controls, and
//   2. saving anything else still round-trips their stored values unchanged.
//
// (2) is checked end to end rather than by stubbing the DOM: the harness only
// exposes the elements the renderer actually produced, so a control that is
// still rendered is a control `saveAgentProfile` will read -- and clear -- the
// moment it has no value. That is what makes the assertion fail on the old
// markup and pass on the new one instead of passing either way.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');

function section(start, end) {
    const from = source.indexOf(start), to = source.indexOf(end, from);
    assert.ok(from >= 0 && to > from, `Missing console section ${start}`);
    return source.slice(from, to);
}

function node() {
    const classes = new Set();
    return {
        innerHTML: '', textContent: '', dataset: {}, value: '',
        classList: {
            add: (...x) => x.forEach(v => classes.add(v)),
            remove: (...x) => x.forEach(v => classes.delete(v)),
            contains: x => classes.has(x),
        },
        setAttribute() {}, removeAttribute() {}, focus() {},
        addEventListener() {},
        querySelector: () => null,
        querySelectorAll: () => [],
    };
}

/* A document holding only the elements the renderer declares. `seed` carries
   the containers the renderer reads before it writes (the two panes), and
   everything else appears because an id in the produced markup says so. */
function renderedDom() {
    const nodes = new Map();
    return {
        seed: (id) => { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
        adopt(html) {
            for (const match of html.matchAll(/id="([^"]+)"/g)) this.seed(match[1]);
        },
        getElementById: (id) => nodes.get(id) || null,
        has: (id) => nodes.has(id),
    };
}

const agent = (extra = {}) => ({
    id: 'proc', name: '采购专员', description: '负责采购', position: '采购专员',
    category: '', tags: [], greeting: '', persona_summary: '', scene_id: '',
    skills: null, sops: [], tools_allowlist: null, tools_denylist: [],
    ...extra,
});

/** The detail pane + save path, sharing one document, as the page has them. */
function profileCtx(a) {
    const dom = renderedDom();
    const writes = [];
    dom.seed('agent-detail-identity');
    const profile = dom.seed('agent-detail-profile');
    const ctx = {
        console, selectedAdminAgentId: a.id, rosterRevision: 'r1', _agentSavedFlashUntil: 0,
        defaultAgentId: 'other', userDefault: { agent_id: '' }, defaultResolution: { agent_id: 'other' },
        tenantDefaultManageable: false, _sessCfg: { model: { providers: [] } },
        document: {
            getElementById: (id) => dom.getElementById(id),
        },
        dom, profile,
        t: key => key,
        localizedLabel: (x) => x,
        escapeHtml: x => String(x),
        findAgent: id => (id === a.id ? a : null),
        getDropdownValue: el => (el ? el._ddValue || '' : ''),
        agentAvatarHTML: () => '<img class="agent-avatar" alt="">',
        codingAgentTypeHint: () => '',
        fieldLabelWithTip: (label) => `<label class="agent-field-label">${label}</label>`,
        agentAnchorHintText: () => 'anchor',
        renderAvatarPicker() {},
        paintAgentSavedFlash() {},
        agentModelDropdownOptions: () => [],
        initDropdown() {},
        // Only the pre-change renderer calls these; kept as no-ops so the
        // removed-control assertions fail on their own message rather than on
        // a missing binding. They become unused once the calls go.
        refreshAgentCategoryDropdown() {},
        refreshAgentSceneDropdown() {},
        flashAgentProfileStatus() {},
        updateAgentWorkspace: (id, payload) => { writes.push(payload); return Promise.resolve(true); },
    };
    vm.createContext(ctx);
    vm.runInContext(section('function renderAgentDetail()', 'function renderAvatarPicker('), ctx);
    vm.runInContext(section('function saveAgentProfile()', 'function paintAgentSavedFlash('), ctx);
    ctx.writes = writes;
    return ctx;
}

/** The 能力 tab, which reads its own two panes out of the document. */
function capabilitiesCtx(a) {
    const dom = renderedDom();
    const pane = dom.seed('agent-detail-skills');
    // The two catalogue containers the pane paints its rows into.
    const skillsList = dom.seed('agent-skills-list');
    const toolsList = dom.seed('agent-tools-list');
    const ctx = {
        console, selectedAdminAgentId: a.id,
        installedSkills: [{ name: 'skill-a', display_name: 'Skill A', description: 'does a' }],
        installedTools: [{ name: 'read', description: 'reads a file' }],
        document: { getElementById: (id) => dom.getElementById(id) },
        t: key => key,
        escapeHtml: x => String(x),
        findAgent: id => (id === a.id ? a : null),
        saveAgentCapabilities() {},
    };
    vm.createContext(ctx);
    vm.runInContext(section('function renderAgentCapabilitiesPane(', 'function renderAgentTasksPane('), ctx);
    ctx.pane = pane;
    return ctx;
}

test('概况 pane hides 分类 and 关联场景 and keeps the other profile fields', () => {
    const ctx = profileCtx(agent({ category: 'procurement', scene_id: 'rfq' }));
    ctx.renderAgentDetail();
    const html = ctx.profile.innerHTML;
    ctx.dom.adopt(html);

    // The two withdrawn controls, label and element alike.
    assert.ok(!html.includes('agents_category'), '分类 must not be rendered');
    assert.ok(!html.includes('agents_category_none'), 'the 分类 placeholder must not be rendered');
    assert.ok(!html.includes('agent-edit-category'), 'no 分类 control element');
    assert.ok(!html.includes('agents_scene'), '关联场景 must not be rendered');
    assert.ok(!html.includes('agents_scene_none'), 'the 关联场景 placeholder must not be rendered');
    assert.ok(!html.includes('agent-edit-scene'), 'no 关联场景 control element');

    // The rest of the pane is untouched, including its neighbours.
    for (const kept of ['agents_position', 'agent-edit-position', 'agents_tags', 'agent-edit-tags',
                        'agents_greeting', 'agent-edit-greeting', 'agents_persona', 'agent-edit-persona',
                        'agents_name', 'agent-edit-name']) {
        assert.ok(html.includes(kept), `${kept} must still be rendered`);
    }
});

test('能力 pane hides the SOP block and keeps the skills and tools blocks', () => {
    const ctx = capabilitiesCtx(agent({ sops: ['check-prd'] }));
    ctx.renderAgentCapabilitiesPane();
    const html = ctx.pane.innerHTML;

    assert.ok(!html.includes('agents_sops_label'), 'the SOP heading must not be rendered');
    assert.ok(!html.includes('agents_sops_hint'), 'the SOP hint must not be rendered');
    assert.ok(!html.includes('agent-sops-list'), 'the bound-SOP list must not be rendered');
    assert.ok(!html.includes('agent-sop-input'), 'the SOP input must not be rendered');
    assert.ok(!html.includes('agent-sop-add'), 'the SOP add button must not be rendered');
    // The bound id itself is what the removed list used to print.
    assert.ok(!html.includes('check-prd'), 'bound SOP ids must not be listed');

    assert.ok(html.includes('agents_skills_label'), 'the skills block must still be rendered');
    assert.ok(html.includes('agents_tools_label'), 'the tools block must still be rendered');
    assert.ok(ctx.document.getElementById('agent-tools-list').innerHTML.includes('read'),
        'the tool catalogue must still be listed');
});

test('saving other profile fields round-trips the hidden fields unchanged', async () => {
    const a = agent({ category: 'procurement', scene_id: 'rfq' });
    const ctx = profileCtx(a);
    ctx.renderAgentDetail();
    ctx.dom.adopt(ctx.profile.innerHTML);
    // The user rewrites the fields that are still offered.
    ctx.dom.seed('agent-edit-position').value = '高级采购专员';
    ctx.dom.seed('agent-edit-greeting').value = '你好';

    await ctx.saveAgentProfile();

    assert.equal(ctx.writes.length, 1, 'one write must be issued');
    const payload = ctx.writes[0];
    assert.equal(payload.position, '高级采购专员', 'the edited field is sent');
    assert.equal(payload.greeting, '你好', 'the edited field is sent');
    assert.equal(payload.category, 'procurement', 'the hidden 分类 keeps its stored value');
    assert.equal(payload.scene_id, 'rfq', 'the hidden 关联场景 keeps its stored value');
});
