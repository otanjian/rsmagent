// Verify the console "Knowledge base" view does not hang on the hardcoded
// "加载知识库中..." placeholder when the read consumer answers anything but
// success (before this change the handler-less `closed` consumer returned
// `503 database_unavailable` and the page spun forever with no explanation).
// Also pins that the write affordances follow the *selected Agent's*
// `can_write_knowledge` projection (data root + ownership), not a
// `knowledge.write` grant.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');

function element() {
    const classes = new Set(['hidden']);
    const el = {
        innerHTML: '', textContent: '', dataset: {}, children: [],
        classList: {
            add: (...x) => x.forEach(c => classes.add(c)),
            remove: (...x) => x.forEach(c => classes.delete(c)),
            contains: c => classes.has(c),
            toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
        },
        setAttribute() {}, removeAttribute() {},
        querySelector() { return null; },
        appendChild(child) { el.children.push(child); return child; },
        addEventListener() {},
        style: {}, closest() { return null; }, focus() {},
    };
    return el;
}

// knowledge-empty owns two <p> lines (title + description) which
// renderKnowledgeUnavailable rewrites in place.
function seededEl() {
    const el = element();
    const lines = [element(), element()];
    el.querySelector = sel => (sel === 'p' ? lines[0] : null);
    el.querySelectorAll = sel => (sel === 'p' ? lines : []);
    el._lines = lines;
    return el;
}

function setup(payload, authCtx) {
    const nodes = new Map();
    const ctx = {
        currentLang: 'zh',
        t: key => key, // identity; assert on the raw key so it is locale-agnostic
        escapeHtml: x => String(x),
        activeAgentId: '', defaultAgentId: '',
        agentCatalog: [],
        enabledAgents: () => [],
        readScopedPreference: () => '',
        writeScopedPreference: () => {},
        removeScopedPreference: () => {},
        switchKnowledgeTab: () => {},
        initKnowledgeImportDropZone: () => {},
        // The sources tab's state reset lives past this slice's end; selecting an
        // Agent calls it before the view reload, and the write-gate behaviour
        // under test here does not depend on it.
        knowledgeSourcesOnLibraryChange: () => {},
        _baseAuthContext: () => authCtx || null,
        document: {
            getElementById(id) {
                // The Agent selector returns null so renderKnowledgeAgentSelect
                // short-circuits (dropdown init is not under test here).
                if (id === 'knowledge-agent-select') return null;
                if (!nodes.has(id)) nodes.set(id, id === 'knowledge-empty' ? seededEl() : element());
                return nodes.get(id);
            },
        },
        fetch: async () => ({ json: async () => payload }),
    };
    // ``findAgent`` lives outside the sliced knowledge section; the page's
    // canWriteKnowledge() consults the selected Agent's projection through it.
    ctx.findAgent = id => (ctx.agentCatalog || []).find(a => a.id === id) || null;
    vm.createContext(ctx);
    const from = source.indexOf('// Knowledge View');
    const to = source.indexOf('function _hasFilterMatch');
    assert.ok(from >= 0 && to > from, 'knowledge section not found');
    vm.runInContext(source.slice(from, to), ctx);
    return { ctx, get: id => ctx.document.getElementById(id) };
}

const flush = () => new Promise(resolve => setImmediate(resolve));

test('a closed/database-unavailable consumer surfaces a readable reason instead of hanging', async () => {
    const { ctx, get } = setup({
        status: 'error', message: 'unavailable in database identity mode',
        code: 'database_unavailable',
    });
    ctx.loadKnowledgeView();
    await flush();
    const empty = get('knowledge-empty');
    assert.equal(empty.classList.contains('hidden'), false, 'empty state becomes visible');
    assert.equal(empty._lines[0].textContent, 'knowledge_unavailable',
        'placeholder replaced with the unavailable label');
    assert.equal(get('knowledge-panel-docs').classList.contains('hidden'), true,
        'docs panel stays hidden');
});

test('a forbidden response surfaces the permission message', async () => {
    const { ctx, get } = setup({ status: 'error', message: 'forbidden', code: 'forbidden' });
    ctx.loadKnowledgeView();
    await flush();
    const empty = get('knowledge-empty');
    assert.equal(empty.classList.contains('hidden'), false);
    assert.equal(empty._lines[0].textContent, 'knowledge_forbidden');
});

test('a member without a writable Agent does not see the new/import entry', () => {
    const { ctx, get } = setup({ status: 'success' }, {
        status: 'success', authorization_mode: 'role', is_tenant_admin: false,
        effective_permissions: ['knowledge.read'],
    });
    ctx.renderKnowledgeWriteAffordances();
    assert.equal(get('knowledge-new-menu').classList.contains('hidden'), true);
    assert.equal(ctx.canWriteKnowledge(), false);
});

test('a knowledge.write grant no longer forces the entry', () => {
    // The grant is retired: authorization is the Agent's data root + ownership,
    // so a lingering id in the auth context must not reveal the entry.
    const { ctx, get } = setup({ status: 'success' }, {
        status: 'success', authorization_mode: 'role', is_tenant_admin: false,
        effective_permissions: ['knowledge.read', 'knowledge.write'],
    });
    ctx.agentCatalog = [{ id: 'agent-shared', can_write_knowledge: false }];
    ctx.selectKnowledgeAgent('agent-shared');
    ctx.renderKnowledgeWriteAffordances();
    assert.equal(ctx.canWriteKnowledge(), false);
    assert.equal(get('knowledge-new-menu').classList.contains('hidden'), true);
});

test('a tenant admin does not write an Agent it cannot write', () => {
    const { ctx, get } = setup({ status: 'success' }, {
        status: 'success', authorization_mode: 'role', is_tenant_admin: true,
        effective_permissions: ['knowledge.read'],
    });
    ctx.agentCatalog = [{ id: 'agent-shared', can_write_knowledge: false }];
    ctx.selectKnowledgeAgent('agent-shared');
    ctx.renderKnowledgeWriteAffordances();
    assert.equal(ctx.canWriteKnowledge(), false);
    assert.equal(get('knowledge-new-menu').classList.contains('hidden'), true);
});

test('a writable private Agent shows the entry to its owner', () => {
    const { ctx, get } = setup({ status: 'success' }, {
        status: 'success', authorization_mode: 'role', is_tenant_admin: false,
        effective_permissions: ['knowledge.read'],
    });
    ctx.agentCatalog = [{ id: 'agent-own', can_write_knowledge: true }];
    ctx.selectKnowledgeAgent('agent-own');
    ctx.renderKnowledgeWriteAffordances();
    assert.equal(ctx.canWriteKnowledge(), true);
    assert.equal(get('knowledge-new-menu').classList.contains('hidden'), false);
});

test('switching the viewed Agent updates the write entry', () => {
    const { ctx, get } = setup({ status: 'success' }, {
        status: 'success', authorization_mode: 'role', is_tenant_admin: false,
        effective_permissions: ['knowledge.read'],
    });
    ctx.agentCatalog = [
        { id: 'agent-own', can_write_knowledge: true },
        { id: 'agent-shared', can_write_knowledge: false },
    ];
    ctx.selectKnowledgeAgent('agent-own');
    assert.equal(get('knowledge-new-menu').classList.contains('hidden'), false, 'own base is writable');
    ctx.selectKnowledgeAgent('agent-shared');
    assert.equal(get('knowledge-new-menu').classList.contains('hidden'), true, 'shared base is read-only');
});

test('file-level write actions follow the same gate', () => {
    const { ctx } = setup({ status: 'success' }, {
        status: 'success', authorization_mode: 'role', is_tenant_admin: false,
        effective_permissions: ['knowledge.read'],
    });
    ctx.agentCatalog = [
        { id: 'agent-own', can_write_knowledge: true },
        { id: 'agent-shared', can_write_knowledge: false },
    ];
    ctx.selectKnowledgeAgent('agent-own');
    assert.match(ctx._knowledgeFileActions('note.md'), /knowledge-actions/);
    assert.match(ctx._knowledgeCategoryActions('research'), /knowledge-actions/);
    ctx.selectKnowledgeAgent('agent-shared');
    assert.equal(ctx._knowledgeFileActions('note.md'), '');
    assert.equal(ctx._knowledgeCategoryActions('research'), '');
});

test('a tenant admin and a platform-all identity keep the entry', () => {
    const admin = setup({ status: 'success' }, {
        status: 'success', authorization_mode: 'role', is_tenant_admin: true,
        effective_permissions: ['knowledge.read'],
    });
    assert.equal(admin.ctx.canWriteKnowledge(), true);

    const all = setup({ status: 'success' }, {
        status: 'success', authorization_mode: 'all', is_tenant_admin: false,
        effective_permissions: [],
    });
    assert.equal(all.ctx.canWriteKnowledge(), true);
});

test('an unknown capability projection does not hide the entry', () => {
    // null means "not fetched yet", not "denied": a slow /auth/context must not
    // strip a management affordance. The server still authorizes the write.
    const { ctx, get } = setup({ status: 'success' }, null);
    ctx.renderKnowledgeWriteAffordances();
    assert.equal(get('knowledge-new-menu').classList.contains('hidden'), false);
});
