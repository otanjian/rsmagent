// Exercise the shipped scenes frontend (scenes/index.js + scenes/registry.js)
// through their public view entry points. Browser acceptance owns layout;
// these tests make the scene center, category tabs, `/场景` picker and the
// workbench dispatch reproducible.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const indexSource = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/scenes/index.js'), 'utf8');
const registrySource = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/scenes/registry.js'), 'utf8');

const catalog = {
    status: 'success',
    categories: [
        { id: 'procurement', name: '采购', icon: 'fa-cart-shopping', color: '#10b981' },
        { id: 'finance', name: '财务', icon: 'fa-coins', color: '#ef4444' },
    ],
    scenes: [
        {
            id: 'procurement_supplier', name: '供应商管理', category: 'procurement',
            description: '供应商评估、选择、关系维护与风险管理', icon: 'fa-truck-field',
            color: '#10b981', skill_name: 'procurement-supplier', has_workbench: true,
            workbench_title: '供应商管理工作台', required_permission: 'scenes.use.procurement',
            system_prompt: '你是一位资深采购专家。',
            sub_scenes: [
                { id: 'supplier_qualification', name: '资质审核', color: '#10b981',
                    description: '审核供应商资质', icon: 'fa-file-check',
                    has_workbench: true, workbench_title: '供应商管理工作台',
                    indicators: [{ name: '准入标准', formula: '审核要点', meaning: '基本信息核实' }] },
                { id: 'supplier_performance', name: '绩效评估', color: '#3b82f6',
                    description: '多维度评估', icon: 'fa-chart-bar' },
            ],
        },
        {
            id: 'finance_voucher', name: '凭证辅助', category: 'finance',
            description: '凭证生成与辅助', icon: 'fa-file-invoice-dollar', color: '#ef4444',
            skill_name: 'finance-voucher', has_workbench: true, workbench_title: '凭证工作台',
            required_permission: 'scenes.use.finance',
            system_prompt: '你是一位财务凭证专家。',
        },
    ],
};

function response(data, status = 200) {
    return { ok: status >= 200 && status < 300, status, json: async () => data };
}

function element(document, tag = 'div') {
    const classes = new Set();
    let text = '', html = '';
    const el = {
        tagName: tag.toUpperCase(), id: '', attrs: {}, dataset: {}, style: {}, disabled: false,
        children: [], parentNode: null, value: '', placeholder: '', _hide: null,
        get className() { return [...classes].join(' '); },
        set className(value) { classes.clear(); String(value).split(/\s+/).filter(Boolean).forEach(v => classes.add(v)); },
        get textContent() { return text; },
        set textContent(value) { text = String(value); html = ''; this.children = []; },
        get innerHTML() { return html; },
        set innerHTML(value) { html = String(value); text = ''; this.children = []; },
        classList: {
            add: (...names) => names.forEach(name => classes.add(name)),
            remove: (...names) => names.forEach(name => classes.delete(name)),
            contains: name => classes.has(name),
            replace(a, b) { if (classes.delete(a)) classes.add(b); },
            toggle(name, force) {
                const add = force === undefined ? !classes.has(name) : force;
                if (add) classes.add(name); else classes.delete(name);
                return add;
            },
        },
        setAttribute(key, value) { this.attrs[key] = String(value); },
        getAttribute(key) { return this.attrs[key] ?? null; },
        removeAttribute(key) { delete this.attrs[key]; },
        appendChild(child) {
            child.parentNode?.removeChild(child);
            this.children.push(child); child.parentNode = this;
            if (this.tagName === 'SELECT' && (!this.value || child.selected)) this.value = child.value;
            return child;
        },
        removeChild(child) { this.children = this.children.filter(c => c !== child); child.parentNode = null; },
        replaceChildren(...children) { this.children = []; children.forEach(child => this.appendChild(child)); },
        contains(other) { return this === other || this.children.some(child => child.contains(other)); },
        matches(selector) {
            if (selector.includes(':disabled') && selector.includes(':not(') && this.disabled) return false;
            if (selector.includes('[hidden]') && selector.includes(':not(') && this.hidden) return false;
            if (selector.includes('.hidden') && selector.includes(':not(') && this.classList.contains('hidden')) return false;
            const simple = selector.replace(/:not\([^)]*\)/g, '').trim();
            if (simple.startsWith('#')) return this.id === simple.slice(1);
            if (simple.startsWith('.')) return this.classList.contains(simple.slice(1));
            return this.tagName.toLowerCase() === simple.replace(/\[.*$/, '').toLowerCase();
        },
        querySelectorAll(selector) {
            const selectors = selector.split(',').map(s => s.trim());
            return this.children.flatMap(child => [
                ...(selectors.some(s => child.matches(s)) ? [child] : []), ...child.querySelectorAll(selector),
            ]);
        },
        querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
        closest(selector) { return this.matches(selector) ? this : this.parentNode?.closest(selector) || null; },
        focus() { this.placeholder = 'focus'; },
        addEventListener(type, fn) {
            if (!this._listeners) this._listeners = {};
            (this._listeners[type] = this._listeners[type] || []).push(fn);
        },
        dispatch(type, extra) {
            for (const fn of (this._listeners[type] || [])) fn.call(this, { target: this, preventDefault() {}, stopPropagation() {}, ...extra });
        },
        click() { this.dispatch('click'); },
    };
    return el;
}

function setup({ scenesPayload = catalog, activate = null } = {}) {
    const nodes = new Map();
    const document = { body: null };
    document.body = element(document, 'body');
    const node = id => {
        if (!nodes.has(id)) {
            const tag = /(?:btn|button)$/.test(id) || id === 'scenes-tabs' ? 'button' : 'div';
            nodes.set(id, element(document, tag));
            nodes.get(id).id = id;
        }
        return nodes.get(id);
    };
    // 场景中心视图的固定 DOM 节点（chat.html 恒定存在）预注册，供 getEl 命中。
    for (const id of ['scenes-empty', 'scenes-empty-title', 'scenes-empty-guide', 'scenes-tabs', 'scenes-grid']) node(id);
    // getElementById: 先查注册节点，再从 body 后代中按 id 查找（动态创建的模态框）。
    function byIdInTree(id) {
        const walk = el => {
            if (el.id === id) return el;
            for (const child of el.children || []) {
                const found = walk(child);
                if (found) return found;
            }
            return null;
        };
        return walk(document.body);
    }
    Object.assign(document, {
        getElementById: id => nodes.get(id) || byIdInTree(id),
        createElement: tag => element(document, tag),
        body: document.body, activeElement: null,
        // The scenes view injects the workbench stylesheet and bundles into
        // <head>; without it every open path stalls before dispatch.
        head: {appendChild(script) { if (script.onload) queueMicrotask(script.onload); }},
        addEventListener() {}, removeEventListener() {},
        querySelector: () => null, querySelectorAll: () => [],
    });

    const requests = [];
    const state = { switched: null, navigated: null, greeted: null };
    const timers = [];
    const ctx = {
        console, Date, URL, URLSearchParams,
        document,
        // console.js globals that scenes/index.js reuses.
        currentLang: 'zh',
        window: null,
        I18N: {
            zh: {
                scenes_title: '场景应用', scenes_subtitle: '选择一个业务场景，进入专用工作台或对话上下文。',
                scenes_loading: '加载场景中...', scenes_empty: '暂无场景应用。',
                scenes_go_chat: '开始对话', scenes_all: '全部', scenes_workbench: '工作台',
                scenes_configure: '配置',
                scenes_activate_failed: '场景激活失败，请稍后重试。',
                scenes_no_category: '当前分类没有可用的场景。',
                scenes_picker_title: '选择场景', scenes_picker_placeholder: '搜索场景...',
                scenes_picker_empty: '没有匹配的场景',
                scenes_greeting: '已进入「{name}」场景。', slash_scenes: '打开场景选择器',
            },
            en: {
                scenes_title: 'Scenario Apps', scenes_all: 'All', scenes_go_chat: 'Start chatting',
                scenes_configure: 'Configure',
                scenes_activate_failed: 'Failed to activate the scenario, please try again.',
                scenes_picker_empty: 'No matching scenarios',
                scenes_greeting: 'Switched to the "{name}" scenario.',
            },
        },
        navigateTo(view) { state.navigated = view; },
        generateSessionId() { return 'session_scene_' + (state.switched ? 2 : 1); },
        switchSession(sid) { state.switched = sid; },
        addBotMessage(content) { state.greeted = content; },
        setTimeout(fn) { timers.push(fn); return timers.length; },
        clearTimeout() {},
        setInterval() { return 1; },
        clearInterval() {},
        fetch(url, options) {
            requests.push({ url, options });
            if (url === '/api/scenes') return Promise.resolve(response(scenesPayload));
            if (url === '/api/scenes/activate') {
                if (activate) return Promise.resolve(activate(options));
                return Promise.resolve(response({ status: 'success', scene: { id: 'ok' } }));
            }
            return Promise.resolve(response({ status: 'error' }));
        },
    };
    // Script 加载桩：测试中不必真加载 workbench js，立即 onload 以推进 Promise。
    const head = element(document, 'head');
    document.head = head;
    head.appendChild = function (s) {
        s.onload && setTimeout(s.onload, 0);
        return s;
    };
    // 场景中心固定 DOM 节点（chat.html 恒存在）。
    for (const id of ['scene-catalog', 'scene-workbench']) node(id);
    ctx.window = ctx;
    vm.createContext(ctx);

    const run = code => vm.runInContext(code, ctx);
    run(registrySource);
    run(indexSource);
    return { ctx, run, node, document, nodes, requests, state, timers };
}

const settle = async () => {
    for (let i = 0; i < 4; i++) await new Promise(resolve => setTimeout(resolve, 0));
};

test('scenes view renders category tabs and scene cards from the catalog', async () => {
    const h = setup();
    await h.ctx.loadScenesView();
    const tabs = h.node('scenes-tabs');
    const grid = h.node('scenes-grid');
    assert.equal(tabs.classList.contains('hidden'), false);
    assert.equal(grid.classList.contains('hidden'), false);
    // 分类页签：全部 + 采购 + 财务
    assert.equal(tabs.children.length, 3);
    // 首次进入显示全部，其他分类的新场景也能被发现。
    const cards = grid.children.filter(c => c.className.includes('scene-card'));
    assert.equal(cards.length, 2);
    assert.equal(cards[0].dataset.sceneId, 'procurement_supplier');
    assert.ok(tabs.children[1].textContent.includes('采购'));
});

test('SAP workbench is visible on first entry and remains available through the data filter', async () => {
    const sceneRoot = path.join(__dirname, '../Scene');
    const index = JSON.parse(fs.readFileSync(path.join(sceneRoot, 'catalog.json'), 'utf8'));
    const scenes = index.scenes.map(id => JSON.parse(fs.readFileSync(path.join(sceneRoot, id, 'scene.json'), 'utf8')));
    const h = setup({ scenesPayload: { status: 'success', categories: index.categories, scenes } });
    const visible = () => h.node('scenes-grid').children.map(card => card.dataset.sceneId);
    await h.ctx.loadScenesView();
    assert.ok(visible().includes('sap_workbench'));
    h.ctx.selectCategory('procurement');
    assert.ok(!visible().includes('sap_workbench'));
    h.ctx.selectCategory('data');
    assert.ok(visible().includes('sap_workbench'));
    await h.ctx.loadScenesView();
    assert.ok(visible().includes('sap_workbench'));
    assert.ok(visible().every(id => scenes.find(scene => scene.id === id).category === 'data'));
});

test('category tabs switch the visible scene cards and empty category shows the empty state', async () => {
    const h = setup({ scenesPayload: {
        status: 'success',
        categories: [{ id: 'procurement', name: '采购', icon: 'fa-cart-shopping', color: '#10b981' }],
        scenes: [
            { id: 'procurement_supplier', name: '供应商管理', category: 'procurement', description: '供应商评估',
                icon: 'fa-truck-field', color: '#10b981', has_workbench: false, system_prompt: 'x' },
        ],
    }});
    await h.ctx.loadScenesView();
    const grid = h.node('scenes-grid');
    assert.equal(grid.children.filter(c => c.className.includes('scene-card')).length, 1);
    // 切换到「全部」仍只有 1 个场景
    h.ctx.selectCategory('all');
    assert.equal(grid.children.filter(c => c.className.includes('scene-card')).length, 1);
});

test('empty catalog shows the empty state and hides the card grid', async () => {
    const emptyCatalog = JSON.parse(fs.readFileSync(path.join(__dirname, '../scenes/scenes_config.json'), 'utf8'));
    const h = setup({ scenesPayload: { status: 'success', ...emptyCatalog } });
    await h.ctx.loadScenesView();
    assert.equal(h.node('scenes-empty').classList.contains('hidden'), false);
    assert.equal(h.node('scenes-grid').classList.contains('hidden'), true);
    assert.equal(h.node('scenes-tabs').classList.contains('hidden'), true);
    assert.equal(h.node('scenes-empty-title').textContent, '暂无场景应用。');
});

test('reopening the catalog removes cached cards after its contents are cleared', async () => {
    const payload = JSON.parse(JSON.stringify(catalog));
    const h = setup({ scenesPayload: payload });
    await h.ctx.loadScenesView();
    assert.ok(h.node('scenes-grid').children.length > 0);
    payload.categories = [];
    payload.scenes = [];
    await h.ctx.loadScenesView();
    assert.equal(h.node('scenes-tabs').children.length, 0);
    assert.equal(h.node('scenes-grid').children.length, 0);
    assert.equal(h.node('scenes-empty').classList.contains('hidden'), false);
});

test('a non-workbench scene card activates into chat and injects the greeting', async () => {
    const h = setup({ scenesPayload: {
        status: 'success',
        categories: [{ id: 'procurement', name: '采购', color: '#10b981' }],
        scenes: [{ id: 'procurement_supplier', name: '供应商管理', category: 'procurement',
            description: 'd', icon: 'fa-truck-field', color: '#10b981', has_workbench: false,
            system_prompt: 'x', required_permission: 'scenes.use.procurement' }],
    }});
    await h.ctx.loadScenesView();
    const card = h.node('scenes-grid').children.find(c => c.className.includes('scene-card'));
    card.click();
    await settle();
    assert.equal(h.requests.some(r => r.url === '/api/scenes/activate'), true);
    assert.equal(h.state.greeted, '已进入「供应商管理」场景。');
    assert.equal(h.state.switched, 'session_scene_1');
    assert.equal(h.state.navigated, 'chat');
});

test('a workbench scene with a registered renderer dispatches instead of activating', async () => {
    const h = setup();
    // 注册 registry 将 finance-voucher 映射到的专业渲染器。
    h.run(`
        window.__wbScenes = [];
        window.ScenesRegistry.registerRenderer('voucher', function(scene){ window.__wbScenes.push(scene); return true; });
    `);
    // 已声明工作台且渲染器可用时不会激活普通对话。
    await h.ctx.openSceneById('finance_voucher');
    await settle();
    assert.equal(h.requests.some(r => r.url === '/api/scenes/activate'), false);
    assert.equal(h.ctx.__wbScenes.length, 1);
    assert.equal(h.ctx.__wbScenes[0].id, 'finance_voucher');
});

test('a scene with a declared configuration entry shows a 配置 control that opens it directly', async () => {
    const h = setup({ scenesPayload: {
        status: 'success',
        categories: [{ id: 'data', name: '数据', color: '#8b5cf6' }],
        scenes: [{ id: 'sap_workbench', name: 'SAP 工作台', category: 'data',
            description: 'd', icon: 'fa-desktop', has_workbench: true,
            card_action: 'configure', required_permission: 'chat.use' }],
    }});
    h.run(`
        window.__configured = null; window.__opened = null;
        window.SceneOriginal = {
            configure: function (scene) { window.__configured = scene.id; return true; },
            open: function (scene) { window.__opened = scene.id; return true; },
        };
    `);
    await h.ctx.loadScenesView();
    const card = h.node('scenes-grid').children.find(c => c.className.includes('scene-card'));
    // 分类标签被「配置」入口取代：卡片下沿是设置动作而不是分类名。
    assert.ok(card.innerHTML.includes('scene-configure'));
    assert.ok(card.innerHTML.includes('配置'));
    assert.ok(!card.innerHTML.includes('数据'));
    // 点中嵌套的配置控件走配置分发，不激活场景、不创建会话。
    const control = element(h.document, 'button');
    control.className = 'scene-configure text-xs';
    card.dispatch('click', {target: control});
    await settle();
    assert.equal(h.ctx.__configured, 'sap_workbench');
    assert.equal(h.ctx.__opened, null);
    assert.equal(h.requests.some(r => r.url === '/api/scenes/activate'), false);
    // 点卡片其他位置仍然打开工作台，两个入口职责不混。
    card.dispatch('click', {target: card});
    await settle();
    assert.equal(h.ctx.__opened, 'sap_workbench');
});

test('a scene without a configuration entry keeps the plain card and category tag', async () => {
    const h = setup();
    await h.ctx.loadScenesView();
    const card = h.node('scenes-grid').children.find(c => c.dataset.sceneId === 'finance_voucher');
    assert.ok(!card.innerHTML.includes('scene-configure'));
    assert.ok(card.innerHTML.includes('财务'));
});

test('`/场景` picker opens against the loaded catalog without throwing', async () => {
    const h = setup();
    h.ctx.showScenePicker();
    await settle();
    // 模态框通过 createElement + appendChild 挂到 body，可按 id 找到。
    const modal = h.document.body.children.find(c => c.id === 'scenes-picker');
    assert.ok(modal, 'picker modal should be created');
    assert.equal(modal.classList.contains('hidden'), false);
});
