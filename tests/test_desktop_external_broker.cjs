// Desktop external-connection transport (change add-external-system-access, task 11.1).
//
// 系统接入页与 ERP 连接列表都是普通 ``/api/**`` 业务路径：Desktop 主进程 broker
// 必须像对待其它业务 API 一样附加 Bearer 与 X-Tenant-ID，渲染层不得持有 token、
// 不得用绝对 URL 直连后端，也不得为此发明第二条 IPC 通道。
//
// 本文件固定四件事（静态断言，与其它 desktop 合同测试同一风格）：
//   1. ``wantsTenantHeader`` 对 ``/api/**``（除 ``/api/auth/**``）返回 true；
//   2. ``requestBusiness`` 先做同源路径守卫，再 ``withTenant`` + ``withToken``；
//   3. ``external-connections.js`` 的 ``ecRequest`` 只用相对路径 + ``same-origin``；
//   4. preload/broker 仍走 ``desktop-request``，没有 ``desktop-external`` 旁路。
//
// 运行：node --test tests/test_desktop_external_broker.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const read = (p) => fs.readFileSync(path.join(root, p), 'utf8');

function extractFunction(source, name) {
    const marker = `function ${name}`;
    const start = source.indexOf(marker);
    assert.ok(start >= 0, `${name} must exist`);
    const nextFn = source.indexOf('\n    function ', start + marker.length);
    const nextSection = source.indexOf('\n    // ===', start + marker.length);
    let end = source.length;
    for (const candidate of [nextFn, nextSection]) {
        if (candidate > start && candidate < end) end = candidate;
    }
    return source.slice(start, end);
}

// --------------------------------------------------------------------------
// 路径守卫：外部连接路由与其它 /api/** 一样
// --------------------------------------------------------------------------

test('requestBusiness rejects absolute and protocol-relative paths', () => {
    const broker = read('desktop/src/main/auth-broker.ts');
    const body = broker.slice(
        broker.indexOf('export async function requestBusiness'),
        broker.indexOf('export async function requestUpload'),
    );
    // 只接受同源路径：必须以 / 开头，且不是 //host 或 scheme: —— 这三条一起
    // 挡掉了绝对 URL 与协议相对 URL，浏览器无法把它们变成对第三方后端的请求。
    assert.match(
        body,
        /if \(!path\.startsWith\('\/'\) \|\| path\.startsWith\('\/\/'\) \|\| \/\^\[a-z\]\+:\/i\.test\(path\)\) \{/,
        'requestBusiness must require a same-origin path',
    );
    assert.match(body, /throw new BrokerError\('invalid_path'/);
});

// --------------------------------------------------------------------------
// 主进程 broker：租户头 + 凭据装配（静态契约）
// --------------------------------------------------------------------------

test('wantsTenantHeader attaches X-Tenant-ID to /api/** business paths', () => {
    const broker = read('desktop/src/main/auth-broker.ts');
    const fn = broker.slice(
        broker.indexOf('function wantsTenantHeader'),
        broker.indexOf('function b64url'),
    );
    assert.match(fn, /path\.startsWith\('\/api\/auth\/'\).*return false/s);
    assert.match(fn, /path\.startsWith\('\/api\/'\).*return true/s);
    // 注释与实现一致：外部连接与 ERP 连接都是 /api/**。
    assert.match(broker, /\/api\/\*\*/);
});

test('requestBusiness guards paths then attaches tenant and token', () => {
    const broker = read('desktop/src/main/auth-broker.ts');
    const body = broker.slice(
        broker.indexOf('export async function requestBusiness'),
        broker.indexOf('export async function requestUpload'),
    );
    assert.match(body, /if \(!path\.startsWith\('\/'\) \|\| path\.startsWith\('\/\/'\) \|\| \/\^\[a-z\]\+:\/i\.test\(path\)\) \{/);
    assert.match(body, /withTenant: true/);
    assert.match(body, /withToken: true/);
});

test('the broker IPC surface stays on desktop-request without a second external channel', () => {
    const broker = read('desktop/src/main/auth-broker.ts');
    const preload = read('desktop/src/main/preload.ts');
    assert.match(broker, /ipcMain\.handle\('desktop-request'/);
    assert.doesNotMatch(broker, /ipcMain\.handle\('desktop-external'/);
    assert.doesNotMatch(preload, /desktop-external/);
    assert.match(preload, /ipcRenderer\.invoke\('desktop-request'/);
});

// --------------------------------------------------------------------------
// 控制台页：相对 fetch，渲染层不持凭据
// --------------------------------------------------------------------------

test('external-connections ecRequest uses relative same-origin fetch only', () => {
    const page = read('channel/web/static/js/external-connections.js');
    const ecRequest = extractFunction(page, 'ecRequest');
    assert.match(ecRequest, /credentials:\s*'same-origin'/);
    assert.match(ecRequest, /window\.fetch\(path,\s*init\)/);
    assert.doesNotMatch(ecRequest, /Authorization/);
    assert.doesNotMatch(ecRequest, /Bearer\s/);
    assert.doesNotMatch(ecRequest, /localStorage/);
    assert.doesNotMatch(ecRequest, /sessionStorage/);
    assert.doesNotMatch(ecRequest, /https?:\/\//);
    // 所有 ecRequest 调用点必须是相对 /api/ 路径（不得硬编码绝对后端 origin）。
    const calls = page.match(/ecRequest\([^)]+\)/g) || [];
    assert.ok(calls.length >= 5, 'expected several ecRequest call sites');
    for (const call of calls) {
        assert.doesNotMatch(call, /https?:\/\//, call);
    }
});

test('ecPathFor builds relative external-connection URLs under /api/', () => {
    const page = read('channel/web/static/js/external-connections.js');
    const ecPathFor = extractFunction(page, 'ecPathFor');
    assert.match(ecPathFor, /\/api\/external-connections\//);
    assert.doesNotMatch(ecPathFor, /https?:\/\//);
});
