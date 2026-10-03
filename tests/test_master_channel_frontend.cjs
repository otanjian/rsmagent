const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const wb = require('../channel/web/static/js/channel-workbench.js');
const source = fs.readFileSync(require('node:path').join(__dirname, '../channel/web/static/js/console.js'), 'utf8');
function fn(name) {
    const start = source.indexOf(`function ${name}(`);
    assert.ok(start >= 0, name);
    let depth = 0;
    for (let i = source.indexOf('{', start); i < source.length; i++) {
        if (source[i] === '{') depth++;
        if (source[i] === '}' && --depth === 0) return source.slice(start, i + 1);
    }
    throw new Error(name);
}
function boot(responses) {
    const nodes = new Map();
    const node = id => {
        if (!nodes.has(id)) nodes.set(id, { value: '', innerHTML: '', textContent: '' });
        return nodes.get(id);
    };
    node('tenant-channel-agent').value = 'agent-a';
    node('tenant-channel-display').value = 'WeChat support';
    const calls = [];
    let loads = 0;
    const sandbox = {
        tenantChannelDraft: { instance_id: '', channel_type: 'weixin' },
        tenantChannelSelfScope: false,
        tenantChannelTargets: [{ id: 'agent-a', scope: 'tenant' }],
        tenantChannelRuntimeNotice: null,
        document: { getElementById: node },
        t: key => key, escapeHtml: text => String(text).replace(/</g, '&lt;').replace(/"/g, '&quot;'),
        setTimeout: callback => callback, clearTimeout: () => {},
        loadTenantChannelsView: () => { loads++; return Promise.resolve(); },
        fetch: (url, options) => {
            calls.push({ url, body: JSON.parse(options.body) });
            const response = responses.shift();
            return Promise.resolve({ json: () => Promise.resolve(response || { status: 'success' }) });
        },
    };
    vm.runInNewContext('let tenantWeixinScan = null;\n' + [
        'stopTenantWeixinScan', 'startTenantWeixinScan', 'renderTenantWeixinScan',
        'tenantWeixinScanFailed', 'pollTenantWeixinScan',
    ].map(fn).join('\n') + '\nfunction scanState() { return tenantWeixinScan; }', sandbox);
    return { sandbox, calls, node, loads: () => loads };
}

test('WeChat scan carries its handle and the selected agent through automatic save', async () => {
    const { sandbox: s, calls, node, loads } = boot([
        { status: 'success', handle: 'scan-a', qr_image: 'data:image/png;base64,test' },
        { status: 'success', saved: true, instance_id: 'channel-a', connected: false, connection_reason: 'pending' },
    ]);
    await s.startTenantWeixinScan('status', 'new');
    assert.equal(calls[0].body.scope, 'tenant');
    assert.match(node('status').innerHTML, /data:image\/png/);
    assert.match(node('status').innerHTML, /tenant_channel_scan_will_save/);
    node('tenant-channel-agent').value = 'different-agent';
    await s.pollTenantWeixinScan(s.scanState());
    assert.equal(calls[1].body.handle, 'scan-a');
    assert.equal(calls[1].body.agent_id, 'agent-a');
    assert.equal(calls[1].body.display_name, 'WeChat support');
    assert.equal(s.tenantChannelDraft, null);
    assert.equal(s.tenantChannelRuntimeNotice.reason, 'pending');
    assert.equal(loads(), 1);
    assert.equal(calls.length, 2); // the confirmed endpoint already saved it
});

test('a personal target requests personal scope', async () => {
    const { sandbox: s, calls } = boot([{ status: 'success', handle: 'scan-personal' }]);
    s.tenantChannelTargets[0].scope = 'user';
    await s.startTenantWeixinScan('status', 'new');
    assert.equal(calls[0].body.scope, 'user');
});

test('closing or changing a form cancels only its scan and stops polling', async () => {
    const { sandbox: s, calls } = boot([{ status: 'success', handle: 'scan-a' }]);
    await s.startTenantWeixinScan('status', 'new');
    const scan = s.scanState();
    s.stopTenantWeixinScan();
    await s.pollTenantWeixinScan(scan);
    assert.equal(calls.length, 2);
    assert.equal(calls[1].body.action, 'cancel');
    assert.equal(calls[1].body.handle, 'scan-a');
    assert.equal(s.scanState(), null);
});

test('a scan result from a replaced draft cannot mutate or submit the new form', async () => {
    const { sandbox: s, calls, loads } = boot([{ status: 'success', handle: 'late-scan' }]);
    const pending = s.startTenantWeixinScan('status', 'new');
    s.tenantChannelDraft = { instance_id: '', channel_type: 'qq' };
    await pending;
    assert.equal(calls[1].body.action, 'cancel');
    assert.equal(s.tenantChannelDraft.channel_type, 'qq');
    assert.equal(loads(), 0);
});

test('an existing WeChat instance never opens a create scan', async () => {
    const { sandbox: s, calls } = boot([]);
    s.tenantChannelDraft.instance_id = 'existing';
    await s.startTenantWeixinScan('status', 'existing');
    assert.equal(calls.length, 0);
});

test('callback hint supplies an instance-specific path only after saving', () => {
    const opts = { spec: { webhook: { path: '/wxcomapp' } }, t: key => key, escape: String };
    assert.match(wb.webhookHint({ ...opts, iid: '' }), /tenant_channel_webhook_after_save/);
    assert.match(wb.webhookHint({ ...opts, iid: 'bot-a' }), /<code>\/wxcomapp\/bot-a<\/code>/);
    assert.notEqual(wb.webhookHint({ ...opts, iid: 'bot-a' }), wb.webhookHint({ ...opts, iid: 'bot-b' }));
    assert.equal(wb.webhookHint({ spec: {} }), '');
    assert.match(wb.fieldInput({ field: { key: 'wechatmp_port', type: 'number' } }), /type="number".*min="1" max="65535"/);
});
