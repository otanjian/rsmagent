// The deployment has to keep the Web app and the device gateway apart, in the
// proxy and in the operator's hands.
//
// Change ``harden-windows-desktop-reliability``, task 6.3, against
// openspec/changes/.../specs/production-deployment/spec.md:
//
//   * "使用桌面设备通道的部署 SHALL 将同源 `/api/desktop/connect` 正确转发至实际
//     网关，支持 WebSocket Upgrade、心跳及重连，并提供网关进程管理与就绪诊断";
//   * "Web 应用和网关 SHALL 使用同一权威身份及命令存储配置";
//   * "部署说明 SHALL 列出客户端版本、应用与网关启动方式、实际代理端口、
//     HTTPS/出站网络条件、模型及工具依赖、持久化范围和升级回退步骤".
//
// The example proxy forwarded *everything* to the Web app, which is WSGI and
// cannot upgrade a WebSocket at all -- so every device connection would have
// failed its handshake while the server business looked healthy.
//
// Run: node --test tests/test_desktop_gateway_deployment.cjs

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const nginx = fs.readFileSync(path.join(root, 'docker', 'nginx.conf.example'), 'utf8');
const gateway = fs.readFileSync(path.join(root, 'integrations', 'desktop', 'gateway.py'), 'utf8');
const runtime = fs.readFileSync(path.join(root, 'channel', 'web', 'fork', 'runtime.py'), 'utf8');
const doc = fs.readFileSync(path.join(root, 'docs', 'desktop-remote-deployment.md'), 'utf8');

/** The nginx location block for an exact path, as written. */
function locationBlock(matcher) {
    const start = nginx.indexOf(matcher);
    assert.ok(start >= 0, `nginx example is missing ${matcher}`);
    const end = nginx.indexOf('\n    }', start);
    return nginx.slice(start, end > start ? end : start + 800);
}

test('the proxy sends the device channel to the gateway, with an Upgrade', () => {
    const block = locationBlock('location = /api/desktop/connect');
    assert.match(block, /proxy_pass http:\/\/127\.0\.0\.1:9877;/,
        'the device channel belongs to the gateway port, not the Web app');
    assert.match(block, /proxy_set_header Upgrade \$http_upgrade;/);
    assert.match(block, /proxy_set_header Connection "upgrade";/,
        'the generic Connection "" would break the handshake');
    assert.match(block, /proxy_read_timeout 3600s;/);
    assert.match(block, /proxy_send_timeout 3600s;/, 'heartbeats need the send timeout too');
    assert.match(block, /proxy_buffering off;/);
});

test('everything else still goes to the Web app, without an accidental upgrade', () => {
    const block = locationBlock('location / {');
    assert.match(block, /proxy_pass http:\/\/127\.0\.0\.1:9899;/);
    assert.match(block, /proxy_set_header Connection "";/, 'business traffic is not upgraded');
    assert.match(nginx, /gzip_types [^;]*text\/html/);
    // Anchored to a line start: the comment above the directive also contains
    // the word `gzip_types`, and matching from there would pull the comment's
    // own prose (which mentions application/json) into the assertion.
    const gzipLine = /^[ \t]*gzip_types[^;]*;/m.exec(nginx)[0];
    assert.ok(!/application\/json/.test(gzipLine), 'API and user data stay uncompressed');
    assert.ok(!/text\/event-stream/.test(gzipLine), 'SSE stays uncompressed');
});

test('the gateway readiness endpoint is not published on the public origin', () => {
    assert.ok(!/location[^\n]*healthz/.test(nginx),
        'exposing /healthz would advertise the internal gateway port');
    assert.match(doc, /healthz/, 'the doc still tells the operator how to check it');
    assert.match(doc, /curl[^\n]*127\.0\.0\.1:9877\/healthz/);
});

test('the gateway is a standalone process bound to loopback by default', () => {
    assert.match(gateway, /parser\.add_argument\("--port", type=int, default=9877\)/);
    assert.match(gateway, /parser\.add_argument\("--bind", default="127\.0\.0\.1"\)/);
    assert.match(gateway, /DESKTOP_GATEWAY_ALLOW_NONLOCAL/,
        'a non-loopback bind has to be deliberate');
    assert.match(gateway, /app\.router\.add_get\("\/api\/desktop\/connect", connect\)/);
    assert.match(gateway, /app\.router\.add_get\("\/healthz", health\)/);
    assert.match(runtime, /WSGI cannot upgrade \/api\/desktop\/connect/,
        'the Web app cannot serve this route: the proxy has to split them');
});

test('both processes name the same identity and command store', () => {
    assert.match(gateway, /identity_db_path/);
    assert.match(gateway, /identity\.db/);
    assert.match(doc, /identity_db_path/, 'the doc has to name the shared setting');
    assert.match(doc, /同一权威身份库与命令存储配置/);
    assert.match(doc, /不是让用户重新登录|不以重新登录/,
        'an identity mismatch is a configuration fault, not a re-login');
});

test('the operator notes cover what the contract asks for', () => {
    for (const [what, pattern] of [
        ['client version', /客户端/],
        ['app and gateway start', /python -m integrations\.desktop\.gateway/],
        ['real proxy ports', /9899[\s\S]*9877|9877[\s\S]*9899/],
        ['HTTPS / outbound network', /证书链/],
        ['model and tool dependencies', /模型\/工具|模型及工具|模型/],
        ['persistence scope', /持久化/],
        ['upgrade and rollback', /升级与回退/],
    ]) {
        assert.match(doc, pattern, `the deployment notes must cover ${what}`);
    }
    assert.match(doc, /Web 就绪不等于网关就绪/,
        'the two readiness states must not be conflated');
    assert.match(doc, /绝不上传服务器|不上传.*授权根/,
        'the local authorization root stays on the device');
    assert.match(doc, /不关闭证书校验/);
});
