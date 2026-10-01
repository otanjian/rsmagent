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

// --------------------------------------------------------------------------
// 退出：状态不足以判断，必须问会话本身（change fix-desktop-relogin-session-sync）
// --------------------------------------------------------------------------

test('a sign-out the status does not confirm asks the session, not the status', () => {
    const broker = read('desktop/src/main/auth-broker.ts');
    const section = broker.slice(
        broker.indexOf('async function sessionIsGone'),
        broker.indexOf('export async function changePassword'),
    );
    const body = section.slice(section.indexOf('export async function logout'));
    // 容器内的退出是串行的：网页先结束 Web 会话，而配对状态下这一步已经把原生父
    // 会话一并撤销。宿主随后的 ``POST /auth/logout`` 因此带着一个身份库已不认识
    // 的 Bearer 到达，写路由的 CSRF 门把它当作来源问题，回 ``403 cross_origin``
    // ——一个「会话已不在」的事实被报成了「来源不被信任」。只看状态码无法与「服务
    // 端拒绝撤销一个仍然有效的会话」区分，所以确认不了时必须回读会话本身；把 403
    // 直接当成成功会放过一个仍然有效的会话，把 403 当成失败则会把已完成的退出报成
    // 未完成（真机验收观察到的就是后者）。
    assert.match(body, /revoked = reply\.status === 200 \|\| reply\.status === 401/,
        'only 200/401 confirm the revocation by status');
    assert.match(body, /if \(!revoked\) revoked = await sessionIsGone\(session\.token\)/,
        'an unconfirmed status must be resolved by asking the session');
    assert.match(body, /state\.blockedReason = 'logout_incomplete'/,
        'a session that is not proven gone must still block');

    // 回读必须是一个只认凭据的 GET，并且只把明确的「已失效」当作证据：把非 200
    // 一律当成「已结束」会在服务端出错时误报退出成功。
    const probe = section.slice(0, section.indexOf('export async function logout'));
    assert.match(probe, /fetchWithToken\('\/auth\/me', token\)/,
        '/auth/me resolves the caller from the credential alone');
    assert.match(probe, /return reply\.status === 401 \|\| reply\.status === 403/,
        'only a 401/403 is proof the session is gone');
    assert.doesNotMatch(probe, /status !== 200/, 'a server error is never proof of a sign-out');
    assert.doesNotMatch(body, /status === 403\)\s*\{?\s*revoked = true/,
        'a 403 must never by itself be read as a completed sign-out');
});
