const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const read = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const source = read('channel/web/static/js/audit-console.js');
const translations = read('channel/web/static/js/i18n/audit-console.js');
const markup = read('channel/web/chat.html').split('id="view-audit"')[1].split('</table>')[0];

function boot() {
    const nodes = new Map();
    const element = () => ({ innerHTML: '', textContent: '', value: '', style: {},
                            querySelectorAll: () => [] });
    const headers = [...markup.matchAll(/<th\b([^>]*)>([^<]*)<\/th>/g)].map(([, attrs, label]) => {
        const node = { ...element(), label };
        const id = attrs.match(/\bid="([^"]+)"/);
        if (id) nodes.set(id[1], node);
        return node;
    });
    ['audit-table-body', 'audit-status', 'audit-pagination'].forEach(id => nodes.set(id, element()));
    let response;
    const context = { window: {}, document: { getElementById: id => nodes.get(id) || null },
        fetch: async () => {
            if (response instanceof Error) throw response;
            return { ok: true, json: async () => response };
        } };
    vm.createContext(context);
    vm.runInContext(translations, context);
    context.window.t = key => context.window.__cowI18N__['audit-console'].zh[key] || key;
    vm.runInContext(source, context);
    return {
        headers: () => headers.filter(h => h.style.display !== 'none').map(h => h.label),
        body: () => nodes.get('audit-table-body').innerHTML,
        load(data) { response = data; return context.window.loadAuditLog(); },
    };
}

const event = { timestamp: 1790910000, tenant: '租户一', actor_user_id: 'usr_1',
    actor: 'alice', actor_display_name: '张三', action: 'agent.update',
    resource_type: 'agent', resource_id: 'demo', result: 'success', changes: { action: 'update' } };
const page = (scope, events = [event]) => ({ scope, events, total: events.length });
const cells = html => [...html.matchAll(/<td\b[^>]*>([\s\S]*?)<\/td>/g)]
    .map(([, value]) => value.replace(/<[^>]*>/g, ''));

test('tenant and platform views keep each value under its actual table header', async () => {
    const app = boot();
    for (const scope of ['tenant', 'all', 'tenant']) {
        await app.load(page(scope));
        const headers = app.headers();
        const values = cells(app.body());
        assert.equal(values.length, headers.length);
        assert.deepEqual(headers, scope === 'tenant'
            ? ['时间', '用户', '动作', '资源', '状态', '详情']
            : ['时间', '租户', '用户', '动作', '资源', '状态', '详情']);
        const row = Object.fromEntries(headers.map((header, index) => [header, values[index]]));
        assert.equal(row.用户, '张三');
        assert.equal(row.动作, 'agent.update');
        assert.equal(row.资源, 'agent:demo');
        assert.equal(row.状态, '成功');
        assert.equal(row.详情, 'action=update');
        if (scope === 'all') assert.equal(row.租户, '租户一');
    }
});

test('loading, empty and failure rows span the visible columns after scope changes', async () => {
    const app = boot();
    for (const scope of ['tenant', 'all']) {
        await app.load(page(scope));
        const colspan = new RegExp(`colspan="${app.headers().length}"`);
        const loading = app.load(page(scope, []));
        assert.match(app.body(), colspan);
        await loading;
        assert.match(app.body(), colspan);
        assert.match(app.body(), /暂无审计记录/);
        await app.load(new Error('network failed'));
        assert.match(app.body(), colspan);
        assert.match(app.body(), /network failed/);
    }
});

test('user names are escaped and missing names do not display internal user IDs', async () => {
    const app = boot();
    await app.load(page('tenant', [{ ...event, actor_display_name: '<img src=x>' }]));
    assert.match(app.body(), /&lt;img src=x&gt;/);
    assert.doesNotMatch(app.body(), /<img/);
    await app.load(page('tenant', [{ ...event, actor: '', actor_display_name: '' }]));
    assert.equal(cells(app.body())[1], '未知用户');
    assert.doesNotMatch(app.body(), /usr_1/);
});
