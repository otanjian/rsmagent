// Desktop Web child session: real module behaviour for the bootstrap checks.
//
// Change ``add-desktop-remote-web-workbench``, task 3.7. The Electron half of
// the bootstrap (installing the cookie into an in-memory partition) is a thin
// wrapper; everything that can silently go wrong -- a cookie the page could
// read, one shared across hosts, the native Bearer handed back as the "child",
// a secret echoed in the JSON body, a redirect followed elsewhere -- is a pure
// data check in ``src/main/remote/web-session.ts``. This file drives those
// checks against compiled output, and pins the client's constants to the same
// ``contracts/desktop/v1.json`` the server reads.
//
// Run: node --test tests/test_desktop_web_session_bridge.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const contractPath = path.join(root, 'contracts', 'desktop', 'v1.json');
const compiled = path.join(desktop, 'dist', 'main', 'remote', 'web-session.js');
const compiledConnection = path.join(desktop, 'dist', 'main', 'remote', 'connection.js');

let webSession;
let contract;

function needsBuild() {
    const sources = ['web-session.ts', 'connection.ts']
        .map((name) => fs.statSync(path.join(desktop, 'src', 'main', 'remote', name)).mtimeMs);
    const newestSource = Math.max(...sources);
    for (const out of [compiled, compiledConnection]) {
        if (!fs.existsSync(out)) return true;
        if (newestSource > fs.statSync(out).mtimeMs) return true;
    }
    return false;
}

before(() => {
    if (needsBuild()) {
        execFileSync('npm', ['run', 'build:main'], { cwd: desktop, stdio: 'inherit' });
    }
    webSession = require(compiled);
    contract = JSON.parse(fs.readFileSync(contractPath, 'utf8'));
});

const NATIVE = 'native-token-value-aaaaaaaaaaaaaaaaaaaa';
const CHILD = 'child-token-value-bbbbbbbbbbbbbbbbbbbb';

function setCookie(value, extra = '') {
    return `cow_session=${value}; Path=/; HttpOnly; Secure; SameSite=Lax${extra}`;
}

function accepted(setCookies = [setCookie(CHILD)], body = defaultBody()) {
    return webSession.checkBootstrapReply({
        status: 200,
        contentType: 'application/json',
        body,
        setCookies,
    }, NATIVE);
}

function defaultBody(overrides = {}) {
    return JSON.stringify({
        status: 'success',
        data: { link_id: 'link_1', expires_at: Math.floor(Date.now() / 1000) + 3600, web_protocol: 1, ...overrides },
    });
}

// ---------------------------------------------------------------------------
// Contract alignment
// ---------------------------------------------------------------------------

test('the client constants are the ones the contract declares', () => {
    assert.equal(webSession.WEB_SESSION_COOKIE, contract.web_session.cookie_name);
    assert.equal(webSession.WEB_SESSION_MAJOR, contract.protocols.web_session.major);
    // And the negotiation table the connection module uses agrees with it.
    const connection = require(compiledConnection);
    assert.equal(connection.CLIENT_PROTOCOLS.web_session.major, webSession.WEB_SESSION_MAJOR);
});

test('the partition name is memory-only, never a persisted one', () => {
    const name = webSession.partitionName('ABCDEFGHIJKLMNOPQRSTUV');
    assert.ok(name.includes('ABCDEFGHIJKLMNOPQRSTUV'));
    assert.ok(!name.startsWith('persist:'), 'a persist: partition would write the cookie to disk');
});

test('an instance id must carry at least as much entropy as the contract requires', () => {
    const minimum = contract.web_session.instance_id_min_chars;
    assert.equal(minimum, 22);
    assert.equal(webSession.isValidInstanceId('x'.repeat(minimum - 1)), false);
    assert.equal(webSession.isValidInstanceId('x'.repeat(minimum)), true);
    assert.equal(webSession.isValidInstanceId('x'.repeat(128)), true);
    assert.equal(webSession.isValidInstanceId('x'.repeat(129)), false);
    for (const bad of ['', 'short', 'has space here 1234567890', 'semi;colon1234567890123', '../escape1234567890123456']) {
        assert.equal(webSession.isValidInstanceId(bad), false, bad);
    }
});

// ---------------------------------------------------------------------------
// Set-Cookie parsing and acceptance
// ---------------------------------------------------------------------------

test('a Set-Cookie header is parsed with case-insensitive attributes', () => {
    const parsed = webSession.parseSetCookie(
        `cow_session=${CHILD}; path=/; httponly; secure; samesite=lax; max-age=3600`);
    assert.equal(parsed.name, 'cow_session');
    assert.equal(parsed.value, CHILD);
    assert.equal(parsed.path, '/');
    assert.equal(parsed.httpOnly, true);
    assert.equal(parsed.secure, true);
    assert.equal(parsed.sameSite.toLowerCase(), 'lax');
    assert.equal(parsed.maxAge, 3600);
    assert.equal(webSession.parseSetCookie(''), null);
    assert.equal(webSession.parseSetCookie('novalue'), null);
});

test('the contract cookie is accepted and its secret is never the native token', () => {
    const result = webSession.selectChildCookie([setCookie(CHILD)], NATIVE);
    assert.equal(result.ok, true);
    assert.equal(result.cookie.value, CHILD);
    assert.equal(result.cookie.name, contract.web_session.cookie_name);
});

test('a missing or duplicated child cookie is refused', () => {
    assert.equal(webSession.selectChildCookie([], NATIVE).ok, false);
    assert.equal(webSession.selectChildCookie([setCookie(CHILD), setCookie(CHILD)], NATIVE).ok, false);
    // A cookie for something else is not a child session cookie.
    assert.equal(webSession.selectChildCookie(['other=1; Path=/; HttpOnly'], NATIVE).ok, false);
});

test('the native credential can never be handed back as the child', () => {
    const result = webSession.selectChildCookie([setCookie(NATIVE)], NATIVE);
    assert.equal(result.ok, false);
    assert.match(result.message, /native session credential/);
});

test('every attribute the contract fixes is enforced', () => {
    const refusals = [
        [`cow_session=${CHILD}; Path=/; Secure; SameSite=Lax`, /page script/],
        [`cow_session=${CHILD}; Path=/; HttpOnly; SameSite=Lax`, /plain HTTP/],
        [`cow_session=${CHILD}; Path=/; HttpOnly; Secure; SameSite=Lax; Domain=example.com`, /host-only/],
        [`cow_session=${CHILD}; Path=/app; HttpOnly; Secure; SameSite=Lax`, /application path/],
        [`cow_session=${CHILD}; Path=/; HttpOnly; Secure`, /SameSite=Lax/],
        [`cow_session=${CHILD}; Path=/; HttpOnly; Secure; SameSite=Strict`, /SameSite=Lax/],
    ];
    for (const [header, expected] of refusals) {
        const result = webSession.selectChildCookie([header], NATIVE);
        assert.equal(result.ok, false, header);
        assert.match(result.message, expected, header);
    }
});

test('a value that is not a session token is refused', () => {
    for (const value of ['', 'short', '<html>', 'a b c d e f g h i j k l m n o p']) {
        const result = webSession.selectChildCookie([setCookie(value)], NATIVE);
        assert.equal(result.ok, false, value);
    }
});

// ---------------------------------------------------------------------------
// The whole reply
// ---------------------------------------------------------------------------

test('a well-formed answer is accepted with its non-secret facts', () => {
    const result = accepted();
    assert.equal(result.ok, true);
    assert.equal(result.linkId, 'link_1');
    assert.equal(result.webProtocol, contract.protocols.web_session.major);
    assert.ok(result.expiresAt > 0);
});

test('the secret is refused anywhere in the body', () => {
    const echo = accepted([setCookie(CHILD)], defaultBody({ note: CHILD }));
    assert.equal(echo.ok, false);
    assert.match(echo.message, /echoed/);
    const token = accepted([setCookie(CHILD)], defaultBody({ web_token: CHILD }));
    assert.equal(token.ok, false);
    assert.match(token.message, /session secret/);
});

test('a missing cookie, link id or expiry is refused', () => {
    assert.equal(accepted([]).ok, false);
    assert.equal(accepted([setCookie(CHILD)], defaultBody({ link_id: '' })).ok, false);
    assert.equal(accepted([setCookie(CHILD)], defaultBody({ expires_at: 0 })).ok, false);
    assert.equal(accepted([setCookie(CHILD)], JSON.stringify({ status: 'success' })).ok, false);
});

test('another Web session protocol major is a protocol refusal', () => {
    const result = accepted([setCookie(CHILD)], defaultBody({ web_protocol: 2 }));
    assert.equal(result.ok, false);
    assert.equal(result.code, 'protocol_mismatch');
    assert.equal(contract.error_codes.protocol_mismatch, result.status);
});

test('a non-2xx answer keeps the server code and status', () => {
    const body = JSON.stringify({ status: 'error', code: 'bootstrap_consumed', message: 'already used' });
    const result = webSession.checkBootstrapReply(
        { status: 409, contentType: 'application/json', body, setCookies: [] }, NATIVE);
    assert.equal(result.ok, false);
    assert.equal(result.code, 'bootstrap_consumed');
    assert.equal(result.status, 409);
    assert.equal(contract.error_codes.bootstrap_consumed, 409);
});

test('a non-JSON answer (an HTML page, a redirect body) is refused', () => {
    const result = webSession.checkBootstrapReply(
        { status: 200, contentType: 'text/html', body: '<html>moved</html>', setCookies: [setCookie(CHILD)] },
        NATIVE);
    assert.equal(result.ok, false);
    assert.notEqual(result.status, 200);
});

test('every refusal is a real error, never a 2xx', () => {
    const bodies = ['', 'not json', JSON.stringify({ status: 'success' }), defaultBody({ web_protocol: 9 })];
    for (const body of bodies) {
        for (const status of [200, 400, 500]) {
            const result = webSession.checkBootstrapReply(
                { status, contentType: 'application/json', body, setCookies: [] }, NATIVE);
            if (result.ok) {
                assert.equal(status, 200, 'only a 200 may be accepted');
                continue;
            }
            assert.ok(result.status >= 400, `${status} ${body}`);
        }
    }
});
