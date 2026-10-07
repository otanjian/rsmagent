// Real shipped React login screen; private browser with a deterministic IPC stub.
// Run after npm run build: NODE_PATH=$(npm root -g) node tests/test_desktop_login_gate_browser.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.join(__dirname, '../desktop/dist/renderer');
const output = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-login-gate-'));
const server = http.createServer((req, res) => {
    const file = path.join(root, new URL(req.url, 'http://localhost').pathname.replace(/^\/$/, '/index.html'));
    if (!fs.existsSync(file) || !fs.statSync(file).isFile()) return res.writeHead(404).end();
    const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };
    res.setHeader('Content-Type', mime[path.extname(file)] || 'application/octet-stream');
    res.end(fs.readFileSync(file));
});

(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch({ channel: 'chrome', headless: true });
    try {
        const page = await browser.newPage({ viewport: { width: 1340, height: 850 } });
        await page.route('**/api/health', route => route.fulfill({ json: { status: 'ok' } }));
        await page.addInitScript(() => {
            localStorage.setItem('cow_theme', 'light');
            const saved = localStorage.getItem('fixture-server');
            window.fixture = { selected: saved || 'http://localhost:9876', calls: [], pending: [], mode: saved ? 'remote' : 'local' };
            window.electronAPI = {
                platform: 'darwin', systemLocale: 'zh-CN',
                getBackendPort: async () => 9876,
                onBackendStatus: () => () => {},
                desktopAuthProbe: async () => ({ ok: true, authRequired: true, identityMode: 'database' }),
                desktopAuthStatus: async () => ({ ok: true, session: null, blockedReason: '' }),
                desktopModeGet: async () => ({ ok: true, mode: window.fixture.mode,
                    serverOrigin: window.fixture.selected, profiles: [], activeProfileId: null }),
                desktopLoginServerSet: async origin => {
                    window.fixture.calls.push(['select', origin]);
                    if (!origin.startsWith('https://') && origin !== 'http://localhost:9876') {
                        return { ok: false, reason: 'scheme_not_https' };
                    }
                    window.fixture.selected = new URL(origin).origin;
                    localStorage.setItem('fixture-server', window.fixture.selected);
                    return { ok: true, origin: window.fixture.selected };
                },
                desktopAuthBegin: () => {
                    window.fixture.calls.push(['begin', window.fixture.selected]);
                    return new Promise(resolve => window.fixture.pending.push(resolve));
                },
                // Delay the old authorization answer until a new one has started.
                desktopAuthCancel: async () => { window.fixture.calls.push(['cancel']); return { ok: true }; },
            };
        });
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.goto(`http://127.0.0.1:${server.address().port}/`);
        const input = page.getByLabel('服务器地址');
        const start = page.getByRole('button', { name: '在浏览器中登录', exact: true });
        await input.waitFor();
        assert.equal(await input.inputValue(), 'http://localhost:9876');
        await input.fill('http://other.example');
        await start.click();
        await page.getByRole('alert').waitFor();
        assert.equal(await page.evaluate(() => window.fixture.pending.length), 0);
        await input.fill('https://ai.example.com');
        await input.press('Enter');
        const cancel = page.getByRole('button', { name: '取消', exact: true });
        await cancel.waitFor();
        assert.equal(await input.isDisabled(), true);
        assert.equal(await start.isDisabled(), true);
        assert.deepEqual(await page.evaluate(() => window.fixture.calls.slice(-2)),
            [['select', 'https://ai.example.com'], ['begin', 'https://ai.example.com']]);
        await cancel.click();
        await input.waitFor({ state: 'visible' });
        await start.click();
        await cancel.waitFor();
        await page.evaluate(() => window.fixture.pending[0]({ ok: false, code: 'authorization_cancelled',
            message: 'the server or sign-in attempt changed' }));
        assert.equal(await input.isDisabled(), true, 'old cancellation cannot unlock the new attempt');
        assert.equal(await page.getByRole('alert').count(), 0);
        await page.evaluate(() => window.fixture.pending[1]({ ok: false, code: 'authorization_timeout' }));
        await page.getByRole('alert').waitFor();
        assert.match(await page.getByRole('alert').innerText(), /超时/);
        assert.equal(await page.getByRole('button', { name: '重新打开浏览器', exact: true }).count(), 0);
        await page.reload();
        await input.waitFor();
        assert.equal(await input.inputValue(), 'https://ai.example.com', 'remote restart retains the editable login entry');
        await page.screenshot({ path: path.join(output, 'login.png'), fullPage: true });
        assert.deepEqual(errors, []);
        console.log(`PASS: server selection before sign-in, validation, cancellation race, retry, remembered remote entry. Screenshot: ${output}/login.png`);
    } finally {
        await browser.close();
        await new Promise(resolve => server.close(resolve));
    }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
