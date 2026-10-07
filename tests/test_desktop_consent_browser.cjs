// Real browser coverage of editing the consent server. Uses a private page and
// intercepts the form POST locally; no user session or external service is used.
// Run: NODE_PATH=$(npm root -g) node tests/test_desktop_consent_browser.cjs
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright');

const repo = path.resolve(__dirname, '..');
const output = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-consent-browser-'));
const htmlFile = path.join(output, 'consent.html');
execFileSync(path.join(repo, '.venv/bin/python'), ['-c', [
    'import pathlib, sys',
    'from auth.desktop_auth import render_consent_page',
    'page = render_consent_page(backend_origin="http://localhost:9876", username="admin",',
    '    display_name="系统管理员", redirect_uri="http://127.0.0.1:60947/callback",',
    '    request_id="fixture-request", csrf="fixture-csrf")',
    'pathlib.Path(sys.argv[1]).write_text(page, encoding="utf-8")',
].join('\n'), htmlFile], { cwd: repo });

const posts = [];
const server = http.createServer((req, res) => {
    const target = new URL(req.url, 'http://localhost');
    if (req.method === 'POST') {
        let body = '';
        req.on('data', chunk => { body += chunk; });
        req.on('end', () => {
            posts.push({ path: target.pathname, fields: Object.fromEntries(new URLSearchParams(body)) });
            res.end('saved');
        });
        return;
    }
    if (target.pathname === '/auth/desktop/authorize') {
        res.setHeader('Content-Type', 'text/html; charset=utf-8');
        res.end(fs.readFileSync(htmlFile));
        return;
    }
    const file = path.join(repo, 'channel/web/static', target.pathname.replace(/^\/assets\//, ''));
    if (target.pathname.startsWith('/assets/') && fs.existsSync(file) && fs.statSync(file).isFile()) {
        const types = { '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };
        res.setHeader('Content-Type', types[path.extname(file)] || 'application/octet-stream');
        res.end(fs.readFileSync(file));
        return;
    }
    res.writeHead(404).end();
});

(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const browser = await chromium.launch({ headless: true, channel: 'chrome' });
    try {
        const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.goto(`http://127.0.0.1:${server.address().port}/auth/desktop/authorize`);
        const editor = page.locator('#desktop-server-form');
        const input = page.getByLabel('服务器地址', { exact: true });
        const allow = page.getByRole('button', { name: '确认授权并继续' });
        await page.getByRole('button', { name: '修改', exact: true }).click();
        assert.equal(await editor.isVisible(), true);
        assert.equal(await allow.isDisabled(), true);
        assert.equal(await input.inputValue(), 'http://localhost:9876');
        await input.fill('http://another.example');
        await page.getByRole('button', { name: '保存并连接' }).click();
        assert.equal(await page.locator('#desktop-server-error').isVisible(), true);
        assert.equal(posts.length, 0);
        await page.getByRole('button', { name: '取消修改' }).click();
        assert.equal(await editor.isVisible(), false);
        assert.equal(await allow.isEnabled(), true);
        await page.getByRole('button', { name: '修改', exact: true }).click();
        assert.equal(await input.inputValue(), 'http://localhost:9876');
        // Saving an unchanged local origin simply closes the editor.
        await page.getByRole('button', { name: '保存并连接' }).click();
        assert.equal(await editor.isVisible(), false);
        assert.equal(posts.length, 0);
        await page.getByRole('button', { name: '修改', exact: true }).click();
        await input.fill('https://AI.example.com:8443/');
        await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });
        await page.setViewportSize({ width: 390, height: 844 });
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
        await Promise.all([
            page.waitForResponse(response => response.request().method() === 'POST'),
            page.getByRole('button', { name: '保存并连接' }).click(),
        ]);
        assert.deepEqual(posts, [{ path: '/auth/desktop/authorize', fields: {
            request_id: 'fixture-request', csrf: 'fixture-csrf',
            decision: 'change_server', server_origin: 'https://ai.example.com:8443',
        } }]);
        assert.deepEqual(errors, []);
        console.log(`PASS: edit, validation, cancel, unchanged origin, responsive layout, form submission. Screenshots: ${output}`);
    } finally {
        await browser.close();
        await new Promise(resolve => server.close(resolve));
    }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
