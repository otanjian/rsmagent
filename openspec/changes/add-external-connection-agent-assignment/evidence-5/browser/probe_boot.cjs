// Probe 2: boot the assembled shell with the console fixture responses.
//
// Same question as probe_boot.cjs, one step further along: the shell's own
// identity/branding/agents answers, everything else logged, so the first request
// that stalls the boot is visible instead of guessed at.
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

const { chromium } = require('playwright');

const repo = path.resolve(__dirname, '..', '..', '..', '..', '..');
const staticRoot = path.join(repo, 'channel/web/static');
const WEB_DIR = path.join(repo, 'channel/web');
const INCLUDE_RE = /<!--#include\s+([^\s>]+?)\s*-->/g;

function assembleShell() {
    const expand = (relPath, depth) => {
        if (depth > 8) throw new Error('include cycle at ' + relPath);
        const text = fs.readFileSync(path.join(WEB_DIR, relPath), 'utf8');
        return text.replace(INCLUDE_RE, (_, fragment) => {
            const expanded = expand(fragment, depth + 1);
            return expanded.endsWith('\n') ? expanded.slice(0, -1) : expanded;
        });
    };
    return expand('chat.html', 0)
        .replaceAll('{{COW_DEFAULT_LANG}}', 'zh')
        .replaceAll('{{COW_NAVIGATION_MODE}}', 'classic')
        .replaceAll('{{COW_WORKBENCH_SIDEBAR_LAUNCH_V2}}', '0');
}

const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png',
    '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf', '.ico': 'image/x-icon' };

const TENANT = { id: 'fixture-tenant', name: '测试租户', code: 'fixture' };
const IDENTITY = { status: 'success', identity_mode: 'database', auth_required: true,
    authenticated: true, user: { id: 'fixture-user', username: 'tenant-admin',
        display_name: '租户管理员', roles: ['admin'], is_admin: true,
        is_platform_admin: false } };
const AGENT = { id: 'agent-1', name: '税法知识库助手', enabled: true };

const seen = [];

const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://fixture');
    const pathname = url.pathname;
    if (!pathname.startsWith('/assets/')) seen.push(req.method + ' ' + req.url);
    const json = (data, status = 200) => {
        res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8',
            'Cache-Control': 'no-store' });
        res.end(JSON.stringify(data));
    };
    if (pathname === '/chat' || pathname === '/' || pathname === '/admin') {
        res.writeHead(200, { 'Content-Type': mime['.html'], 'Cache-Control': 'no-store' });
        res.end(assembleShell());
    } else if (pathname.startsWith('/assets/')) {
        const file = path.resolve(staticRoot, '.' + pathname.slice('/assets'.length));
        if (!file.startsWith(staticRoot + path.sep) || !fs.existsSync(file)
            || !fs.statSync(file).isFile()) {
            json({ status: 'error', message: 'Missing fixture asset' }, 404);
        } else {
            res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream' });
            fs.createReadStream(file).pipe(res);
        }
    } else if (pathname === '/auth/check') json(IDENTITY);
    else if (pathname === '/auth/login') json({ ...IDENTITY, tenants: [TENANT] });
    else if (pathname === '/auth/logout') json({ status: 'success' });
    else if (pathname === '/auth/me') json({ ...IDENTITY, tenants: [TENANT], current_tenant: TENANT });
    else if (pathname === '/auth/context') json({ status: 'success', authorization_mode: 'role',
        is_tenant_admin: true, is_platform_admin: false, tenant: TENANT,
        console_pages: { 'admin.external_connections': { available: true, read_allowed: true, scope: 'tenant' } } });
    else if (pathname === '/api/version') json({ status: 'success', version: 'external-fixture' });
    else if (pathname === '/api/branding/public') json({ enabled: false, revision: 1, logo_url: '', favicon_url: '' });
    else if (pathname === '/config') json({ status: 'success', title: '容大AI', model: 'fixture-model',
        providers: {}, agent_permission_mode: 'workspace-write',
        permission_modes: ['read-only', 'workspace-write', 'full-access'] });
    else if (pathname === '/api/agents') json({ status: 'success', default_agent_id: AGENT.id,
        agents: [AGENT], revision: 'fixture', channel_instances: [] });
    else if (pathname === '/poll') json({ status: 'success', has_content: false });
    else if (pathname.startsWith('/api/')) json({ status: 'success', items: [], sessions: [],
        has_more: false, total: 0, messages: [], recents: [], tree: [], root_files: [],
        providers: [], agents: [] });
    else json({ status: 'success' });
});

(async () => {
    const browser = await chromium.launch().catch(() => chromium.launch({ channel: 'chrome' }));
    const origin = await new Promise(resolve => {
        server.listen(0, '127.0.0.1', () => resolve('http://127.0.0.1:' + server.address().port));
    });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'zh-CN' });
    await context.addInitScript(() => {
        if (!location.href.startsWith('http:')) return;
        sessionStorage.setItem('cow_tenant_id', 'fixture-tenant');
        localStorage.setItem('cow_lang', 'zh');
        // Every rejection reason that a handler sees, so a swallowed failure in
        // the boot chain names itself instead of only showing the gate.
        const originalThen = Promise.prototype.then;
        Promise.prototype.then = function (onFulfilled, onRejected) {
            if (typeof onRejected === 'function') {
                const wrapped = function (error) {
                    console.log('REJECTED-REASON ' + (error && error.stack ? error.stack : error));
                    return onRejected(error);
                };
                return originalThen.call(this, onFulfilled, wrapped);
            }
            return originalThen.call(this, onFulfilled, onRejected);
        };
    });
    const page = await context.newPage();
    page.on('console', message => console.log('CONSOLE', message.type(), message.text().slice(0, 300)));
    page.on('pageerror', error => console.log('PAGEERROR', error.message));
    page.on('dialog', async dialog => { console.log('DIALOG', dialog.message()); await dialog.dismiss(); });
    await page.goto(origin + '/admin#view-external-connections', { waitUntil: 'networkidle' });
    await page.waitForTimeout(5000);
    console.log('--- #app class:', await page.locator('#app').getAttribute('class'));
    console.log('--- login overlay hidden:', await page.locator('#login-overlay').getAttribute('class'));
    console.log('--- view visible:', await page.locator('#view-external_connections').isVisible());
    console.log('--- visible text:', (await page.locator('body').innerText()).slice(0, 600));
    console.log('--- hash:', await page.evaluate(() => location.hash));
    console.log('--- requests:');
    seen.forEach(line => console.log('   ', line));
    await browser.close();
    server.close();
})();
