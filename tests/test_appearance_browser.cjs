// Full production-page integration: only backend responses are fixtures.
// Requires Playwright with installed Chrome; no requests reach a live service.
// NODE_PATH=.../node_modules node tests/test_appearance_browser.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require('playwright');

const repo = path.resolve(__dirname, '..');
const staticRoot = path.join(repo, 'channel/web/static');
const output = process.env.COW_APPEARANCE_BROWSER_OUTPUT || fs.mkdtempSync(path.join(os.tmpdir(), 'cow-appearance-browser-'));
const report = { fixtureOnly: true, scenarios: [], pageErrors: [], unexpectedRoutes: [], requests: [], visual: [] };
const streams = new Map();
const browserSessions = new Map();
const browserBrands = new Map();
const browserRosters = new Map();
const sessionTeams = new Map();
let contextSequence = 0;
const tenants = [{ id: 'fixture-tenant', name: '测试租户 A', code: 'fixture-a' },
    { id: 'fixture-tenant-b', name: '测试租户 B', code: 'fixture-b' }];
fs.mkdirSync(output, { recursive: true });

const identities = {
    database: { status: 'success', identity_mode: 'database', auth_required: true, authenticated: true,
        user: { username: 'appearance-member', display_name: '普通成员', roles: ['member'], is_admin: false } },
    // Unauthenticated database shell (login overlay visible); not a shared-password mode.
    public: { status: 'success', identity_mode: 'database', auth_required: true, authenticated: false },
};

const fixture = {
    '/config': { status: 'success', title: '容大AI', model: 'fixture-model', providers: {},
        agent_permission_mode: 'workspace-write', permission_modes: ['read-only', 'workspace-write', 'full-access'] },
    '/api/branding/public': { enabled: true, revision: 1, brand_name: '容大AI', logo_description: '控制台',
        logo_url: '/assets/rongda-ai-mark.svg', favicon_url: '/assets/favicon.ico' },
    '/api/version': { status: 'success', version: 'appearance-fixture' },
    '/api/agents': { status: 'success', default_agent_id: 'default', revision: 'fixture',
        agents: [{ id: 'default', name: 'default', enabled: true, is_default: true }], channel_instances: [] },
    '/api/platform/tenants': { status: 'success', items: [{ id: 'fixture-tenant', name: '测试租户', code: 'fixture' }] },
    '/api/knowledge/list': { status: 'success', tree: [], root_files: [] },
    '/api/projects': { status: 'success', current: null, recents: [], default_workspace: '/fixture/workspace', projects_root: '/fixture/projects' },
    '/api/history': { status: 'success', messages: [], has_more: false },
    '/api/sessions': { status: 'success', sessions: [], has_more: false, total: 0, group_mode: 'time' },
    '/api/models': { status: 'success', providers: [], models: [] },
    '/api/channels': { status: 'success', channels: [] },
    '/poll': { status: 'success', has_content: false },
};
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
    '.png': 'image/png', '.jpg': 'image/jpeg', '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf' };
// Render shared fragments just as the production template assembler does.
function expandIncludes(html, depth = 0) {
    if (depth > 8) throw new Error('Template include cycle');
    return html.replace(/<!--#include\s+([^\s>]+?)\s*-->/g, (_, fragment) =>
        expandIncludes(fs.readFileSync(path.join(repo, 'channel/web', fragment), 'utf8'), depth + 1).trimEnd());
}
const server = http.createServer((req, res) => {
    const pathname = new URL(req.url, 'http://fixture').pathname;
    const identity = req.headers['x-test-identity'] || 'public';
    const sessionKey = req.headers['x-test-browser-session'] || 'default';
    if (!browserSessions.has(sessionKey)) browserSessions.set(sessionKey, JSON.parse(JSON.stringify(identities[identity])));
    const login = browserSessions.get(sessionKey);
    if (!pathname.startsWith('/assets/')) report.requests.push({ pathname, method: req.method, identity });
    const json = (data, status = 200) => {
        res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
        res.end(JSON.stringify(data));
    };
    if (pathname === '/chat' || pathname === '/') {
        res.writeHead(200, { 'Content-Type': mime['.html'], 'Cache-Control': 'no-store' });
        res.end(expandIncludes(fs.readFileSync(path.join(repo, 'channel/web/chat.html'), 'utf8'))
            .replaceAll('{{COW_DEFAULT_LANG}}', 'zh')
            .replaceAll('{{COW_NAVIGATION_MODE}}', 'classic'));
    } else if (pathname.startsWith('/assets/')) {
        const file = path.resolve(staticRoot, '.' + pathname.slice('/assets'.length));
        if (!file.startsWith(staticRoot + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
            report.unexpectedRoutes.push({ pathname, method: req.method });
            json({ status: 'error', message: 'Missing fixture asset' }, 404);
        } else {
            res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream' });
            fs.createReadStream(file).pipe(res);
        }
    } else if (pathname === '/auth/check') json(login);
    else if (pathname === '/auth/logout') { login.authenticated = false; delete login.user; json({ status: 'success' }); }
    else if (pathname === '/auth/login') {
        let body = ''; req.on('data', chunk => { body += chunk; });
        req.on('end', () => {
            const input = JSON.parse(body);
            login.authenticated = true;
            if (identity === 'database') login.user = { username: input.username, display_name: '用户 ' + input.username, roles: ['member'], is_admin: false };
            json({ ...login, tenants: [tenants[0]] });
        });
    }
    else if (pathname === '/auth/me') json({ ...login, tenants, current_tenant: tenants[0] });
    else if (pathname === '/api/branding/public') json(browserBrands.get(sessionKey) || fixture[pathname]);
    else if (pathname === '/api/agents' && identity === 'database') {
        json({ status: 'success', revision: 'safe-projection-fixture', agents: browserRosters.get(sessionKey)
            || [{ id: 'default', name: 'default', description: '', avatar: '', is_default: true, can_chat: true }] });
    }
    else if (pathname === '/upload') {
        req.resume(); req.on('end', () => json({ status: 'success', file_path: '/fixture/appearance.txt', file_name: 'appearance-fixture.txt', file_type: 'file' }));
    }
    else if (pathname === '/message') {
        let body = ''; req.on('data', chunk => { body += chunk; });
        req.on('end', () => {
            report.sentMessages = [...(report.sentMessages || []), JSON.parse(body)];
            json({ status: 'success', stream: true, request_id: 'appearance-send-fixture' });
        });
    }
    else if (pathname === '/stream') {
        const requestId = new URL(req.url, 'http://fixture').searchParams.get('request_id');
        res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });
        res.flushHeaders(); streams.set(requestId, res);
        res.write('data: ' + JSON.stringify({ type: 'delta', content: 'First streamed segment. ', seq: 1 }) + '\n\n');
        req.on('close', () => streams.delete(requestId));
    }
    else if (pathname === '/api/appearance-fixture-expire') json({ status: 'error', message: 'Fixture session expired' }, 401);
    else if (/^\/api\/sessions\/[^/]+\/settings$/.test(pathname)) {
        const teamKey = sessionKey + ':' + pathname;
        const settings = () => ({ status: 'success', model: { model: 'fixture-model', source: 'global' },
            permission: { mode: 'workspace-write', source: 'global' }, team: { members: sessionTeams.get(teamKey) || [], source: 'session' } });
        if (req.method === 'POST') {
            let body = ''; req.on('data', chunk => { body += chunk; });
            req.on('end', () => {
                const data = JSON.parse(body);
                if (Object.hasOwn(data, 'members')) {
                    const roster = browserRosters.get(sessionKey) || fixture['/api/agents'].agents;
                    const members = data.members || [];
                    sessionTeams.set(teamKey, members.map(id => {
                        const agent = roster.find(item => item.id === id);
                        assert.ok(agent, 'Team fixture only accepts known agent IDs');
                        return { id: agent.id, name: agent.name, avatar: agent.avatar || '' };
                    }));
                    report.teamSettingsWrites = [...(report.teamSettingsWrites || []), { pathname, members }];
                }
                json(settings());
            });
        } else json(settings());
    } else if (fixture[pathname]) json(fixture[pathname]);
    else {
        report.unexpectedRoutes.push({ pathname, method: req.method });
        json({ status: 'error', message: 'Unconfigured fixture route' }, 404);
    }
});

let browser, origin;
async function context(options = {}) {
    const contextKey = String(++contextSequence);
    if (options.brand) browserBrands.set(contextKey, options.brand);
    if (options.roster) browserRosters.set(contextKey, options.roster);
    const ctx = await browser.newContext({ viewport: options.viewport || { width: 1440, height: 900 },
        locale: 'zh-CN', colorScheme: options.system || 'light',
        extraHTTPHeaders: { 'X-Test-Identity': options.identity || 'public', 'X-Test-Browser-Session': contextKey } });
    await ctx.addInitScript(({ initial, denyRead, denyWrite }) => {
        if (!location.href.startsWith('http:')) return;
        if (!localStorage.getItem('__appearance_fixture_seeded')) {
            for (const [key, value] of Object.entries(initial)) localStorage.setItem(key, value);
            localStorage.setItem('__appearance_fixture_seeded', '1');
        }
        const get = Storage.prototype.getItem, set = Storage.prototype.setItem;
        if (denyRead) Storage.prototype.getItem = function (key) {
            if (this === localStorage) throw new DOMException('Fixture denied all localStorage reads', 'SecurityError');
            return get.call(this, key);
        };
        if (denyWrite) Storage.prototype.setItem = function (key, value) {
            if (this === localStorage && key === denyWrite) throw new DOMException('Fixture partial save denial', 'QuotaExceededError');
            return set.call(this, key, value);
        };
        window.__appearanceFixtureRootStates = [];
        new MutationObserver(() => {
            const root = document.documentElement;
            if (root?.dataset.webPalette) window.__appearanceFixtureRootStates.push({
                palette: root.dataset.webPalette, dark: root.classList.contains('dark'), phase: document.readyState,
            });
        }).observe(document, { subtree: true, attributes: true, attributeFilter: ['data-web-palette', 'class'] });
    }, { initial: options.initial || {}, denyRead: !!options.denyRead, denyWrite: options.denyWrite || '' });
    ctx.on('page', page => page.on('pageerror', error => {
        report.pageErrors.push({ scenario: report.currentScenario, url: page.url(), message: error.message, stack: error.stack });
    }));
    return ctx;
}

async function open(ctx) {
    const page = await ctx.newPage();
    page.setDefaultTimeout(10000);
    await page.goto(origin + '/chat', { waitUntil: 'networkidle' });
    // Credentials must be submitted before the tenant group appears (product
    // gates that step on a successful password login).
    if (await page.locator('#login-form').isVisible()) {
        if (await page.locator('#login-username').isVisible()) {
            await page.locator('#login-username').fill('appearance-member');
            await page.locator('#login-password').fill('fixture-only-password');
        }
        if (await page.locator('#login-tenant-group').isVisible()) {
            await page.locator('#login-tenant-select').selectOption('fixture-tenant');
        }
        await page.locator('#login-btn').click();
    }
    if (await page.locator('#login-tenant-group').isVisible()) {
        await page.locator('#login-tenant-select').selectOption('fixture-tenant');
        await page.locator('#login-btn').click();
    }
    await page.locator('#app').waitFor({ state: 'visible' });
    await page.locator('#chat-agent-identity').waitFor({ state: 'visible' });
    await page.waitForFunction(() => typeof window.CowAppearance?.setPalette === 'function'
        && typeof openAppearancePreferences === 'function' && typeof initWorkspacePanel === 'function');
    const boot = await page.evaluate(() => ({ state: CowAppearance.getState(), snapshots: window.__appearanceFixtureRootStates }));
    assert.ok(boot.snapshots.some(s => s.phase === 'loading'), 'Appearance must resolve during document parsing');
    assert.ok(boot.snapshots.every(s => s.palette === boot.state.palette && s.dark === (boot.state.resolved === 'dark')),
        'Pre-paint and runtime must keep the same initial appearance');
    return page;
}

async function state(page, expected) {
    await page.waitForFunction(value => {
        const s = window.CowAppearance.getState();
        return Object.entries(value).every(([key, field]) => s[key] === field);
    }, expected);
    const actual = await page.evaluate(() => ({ ...CowAppearance.getState(),
        attribute: document.documentElement.dataset.webPalette, dark: document.documentElement.classList.contains('dark'),
        lightHighlightDisabled: document.getElementById('hljs-light').disabled,
        darkHighlightDisabled: document.getElementById('hljs-dark').disabled }));
    for (const [key, value] of Object.entries(expected)) assert.equal(actual[key], value, key);
    assert.equal(actual.attribute, actual.palette);
    assert.equal(actual.dark, actual.resolved === 'dark');
    assert.equal(actual.lightHighlightDisabled, actual.dark);
    assert.equal(actual.darkHighlightDisabled, !actual.dark);
}
async function panel(page) {
    // Language, appearance and logout live in the account menu only, so the
    // preference panel opens from there instead of a top-bar entry.
    if (!await page.locator('#appearance-dialog').evaluate(el => el.open)) await accountPanel(page);
    await page.locator('#appearance-dialog').waitFor({ state: 'visible' });
}
async function choose(page, field, value) {
    await page.locator(`label:has(input[name="web-${field}"][value="${value}"])`).click();
    assert.equal(await page.locator(`input[name="web-${field}"]:checked`).inputValue(), value);
}
async function closePanel(page) {
    await page.keyboard.press('Escape');
    await page.locator('#appearance-dialog').waitFor({ state: 'hidden' });
}
async function expectFocus(page, id) {
    await page.waitForFunction(expected => document.activeElement.id === expected, id);
}
async function accountPanel(page) {
    await page.locator('#sidebar-account-toggle').click();
    await page.locator('#account-menu-prefs').click();
    await page.locator('#appearance-dialog').waitFor({ state: 'visible' });
}
async function sidebarNavigate(page, view) {
    const target = page.locator(`#sidebar [data-view="${view}"]`);
    const insideGroup = await target.evaluate(el => !!el.closest('.menu-group'));
    if (insideGroup && !await target.evaluate(el => el.closest('.menu-group').classList.contains('open'))) {
        const group = target.locator('xpath=ancestor::div[contains(@class,"menu-group")]');
        await group.locator('button').click();
    }
    await target.click();
}
async function settleColors(page) {
    // Inherited color transitions can start descendant transitions. Await the
    // real animation timeline, including descendants, instead of sampling an
    // intermediate frame or disabling production transitions in the test.
    await page.evaluate(async () => {
        for (let round = 0; round < 20; round++) {
            const active = document.getAnimations().filter(a => a instanceof CSSTransition && a.playState === 'running');
            if (active.length) await Promise.all(active.map(a => a.finished.catch(() => {})));
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            if (!document.getAnimations().some(a => a instanceof CSSTransition && a.playState === 'running')) return;
        }
        throw Error('Appearance color transitions did not settle');
    });
}
async function scenario(name, run) {
    if (process.env.COW_APPEARANCE_BROWSER_FILTER && !name.includes(process.env.COW_APPEARANCE_BROWSER_FILTER)) return;
    report.currentScenario = name;
    const start = Date.now();
    try { await run(); report.scenarios.push({ name, passed: true, durationMs: Date.now() - start }); }
    catch (error) { report.scenarios.push({ name, passed: false, message: error.message, stack: error.stack }); throw error; }
    finally { fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(report, null, 2)); }
}

// Resolve actual painted backgrounds from the element through its ancestors.
// Alpha is composed over the parent; text opacity is included where present.
async function contrastAudit(page, selectors) {
    return page.evaluate(selectors => {
        const rgba = value => {
            const m = value.match(/^rgba?\(([^)]+)\)$/);
            if (!m) throw Error('Unsupported computed color: ' + value);
            const v = m[1].split(/[ ,/]+/).map(Number);
            return [v[0], v[1], v[2], v.length > 3 ? v[3] : 1];
        };
        const over = (a, b) => [0, 1, 2].map(i => a[i] * a[3] + b[i] * (1 - a[3])).concat(1);
        const bg = el => {
            const parent = el.parentElement ? bg(el.parentElement) : [255, 255, 255, 1];
            const style = getComputedStyle(el);
            if (style.backgroundImage !== 'none') throw Error('Audit cannot resolve background image on ' + el.id);
            return over(rgba(style.backgroundColor), parent);
        };
        const lum = color => color.slice(0, 3).map(c => {
            c /= 255; return c <= .04045 ? c / 12.92 : Math.pow((c + .055) / 1.055, 2.4);
        }).reduce((total, c, i) => total + c * [.2126, .7152, .0722][i], 0);
        const result = [];
        for (const item of selectors) {
            const el = document.querySelector(item.selector);
            if (!el) throw Error('Missing audit selector ' + item.selector);
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el, item.pseudo || null);
            const foreground = rgba(style.color), background = bg(el);
            let opacity = 1;
            for (let current = el; current; current = current.parentElement) opacity *= Number(getComputedStyle(current).opacity);
            foreground[3] *= opacity;
            const fgLum = lum(over(foreground, background)), bgLum = lum(background);
            result.push({ ...item, text: el.textContent.trim().slice(0, 60), color: style.color,
                background, opacity, ratio: (Math.max(fgLum, bgLum) + .05) / (Math.min(fgLum, bgLum) + .05),
                onScreen: rect.width > 0 && rect.height > 0 && rect.right > 0 && rect.left < innerWidth
                    && rect.bottom > 0 && rect.top < innerHeight });
        }
        return result;
    }, selectors);
}

const contentAuditSelectors = [
    { selector: '#sidebar .sidebar-item.active span', area: 'sidebar active' },
    { selector: '#sidebar .sidebar-item:not(.active) span', area: 'sidebar default' },
    { selector: '#sidebar-brand-caption', area: 'sidebar brand caption' },
    { selector: '#sidebar-brand-name .brand-ai', area: 'sidebar brand AI' },
    { selector: '#sidebar .menu-group > button span', area: 'sidebar group label' },
    { selector: '#sidebar-account-name', area: 'sidebar account' },
    { selector: '#breadcrumb-page', area: 'header' },
    { selector: '#chat-agent-name', area: 'agent header' },
    { selector: '#welcome-subtitle', area: 'hero' },
    { selector: '.example-card span[data-i18n]', area: 'card title' },
    { selector: '.example-card p', area: 'card body' },
    { selector: '#chat-input', area: 'composer text' },
    { selector: '#chat-input', pseudo: '::placeholder', area: 'composer placeholder' },
    { selector: '#model-selector-label', area: 'composer model' },
];
const panelAuditSelectors = [
    { selector: '#appearance-title', area: 'panel title' },
    { selector: '#appearance-scope', area: 'panel scope' },
    { selector: '.appearance-preset-label', area: 'panel palette' },
    { selector: '.appearance-preset-hint', area: 'panel recommended' },
    { selector: '.appearance-modes label span', area: 'panel mode' },
    { selector: '.appearance-reset', area: 'panel reset' },
    { selector: '.appearance-footer .appearance-description', area: 'panel footer' },
];

(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    origin = 'http://127.0.0.1:' + server.address().port;
    browser = await chromium.launch({ channel: 'chrome', headless: true });
    await scenario('new page, live choices, persistence, keyboard, reset and system highlighting', async () => {
        const ctx = await context({ initial: { cow_lang: 'en', cow_theme_id: 'desktop-untouched' } });
        const page = await open(ctx);
        await state(page, { palette: 'business', mode: 'system', resolved: 'light', storageFailed: false });
        assert.deepEqual(await page.evaluate(() => [localStorage.getItem('cow_theme'), localStorage.getItem('cow_web_palette')]), [null, null]);
        assert.equal(await page.locator('button.example-card').count(), 6);
        assert.equal(await page.locator('#app header #chat-agent-identity').count(), 1);
        assert.equal(await page.locator('#app header').count(), 1);
        await panel(page);
        assert.equal(await page.locator('#appearance-title').textContent(), 'Appearance');
        await choose(page, 'palette', 'slate');
        await choose(page, 'mode', 'dark');
        await state(page, { palette: 'slate', mode: 'dark', resolved: 'dark' });
        await closePanel(page);
        await expectFocus(page, 'sidebar-account-toggle');
        await page.reload({ waitUntil: 'networkidle' });
        await state(page, { palette: 'slate', mode: 'dark', resolved: 'dark' });
        await page.locator('#chat-input').fill('Draft survives appearance choices');
        const sessionBefore = await page.evaluate(() => ({ id: sessionId, agent: activeAgentId,
            brand: document.getElementById('sidebar-brand-name').innerHTML }));
        await panel(page);
        await page.locator('.appearance-reset').click();
        await state(page, { palette: 'business', mode: 'system', resolved: 'light' });
        assert.equal(await page.locator('#chat-input').inputValue(), 'Draft survives appearance choices');
        assert.deepEqual(await page.evaluate(() => ({ id: sessionId, agent: activeAgentId,
            brand: document.getElementById('sidebar-brand-name').innerHTML })), sessionBefore);
        assert.deepEqual(await page.evaluate(() => [localStorage.getItem('cow_lang'), localStorage.getItem('cow_theme_id')]), ['en', 'desktop-untouched']);
        await page.emulateMedia({ colorScheme: 'dark' });
        await state(page, { palette: 'business', mode: 'system', resolved: 'dark' });
        assert.equal(await page.locator('input[name="web-mode"]:checked').inputValue(), 'system');
        await choose(page, 'mode', 'light');
        await page.emulateMedia({ colorScheme: 'light' });
        await page.emulateMedia({ colorScheme: 'dark' });
        await state(page, { palette: 'business', mode: 'light', resolved: 'light' });
        // Native radio keyboard navigation changes the same real controller.
        await page.locator('input[name="web-palette"][value="business"]').focus();
        await page.keyboard.press('ArrowRight');
        await state(page, { palette: 'slate' });
        await closePanel(page);
        await ctx.close();
    });

    await scenario('old light/dark browser migration persists classic after a mode-only change', async () => {
        for (const mode of ['light', 'dark']) {
            const ctx = await context({ initial: { cow_theme: mode } });
            const page = await open(ctx);
            await state(page, { palette: 'classic', mode, resolved: mode });
            await panel(page); await choose(page, 'mode', 'system');
            await page.reload({ waitUntil: 'networkidle' });
            await state(page, { palette: 'classic', mode: 'system', resolved: 'light' });
            await ctx.close();
        }
    });

    await scenario('one preferences dialog serves both entries with local language, keyboard and all close actions', async () => {
        const ctx = await context({ identity: 'database' });
        const page = await open(ctx);
        const start = report.requests.length;
        await accountPanel(page);
        assert.equal(await page.locator('#account-prefs-modal').count(), 0);
        assert.equal(await page.locator('dialog[open]').count(), 1);
        await choose(page, 'palette', 'slate');
        assert.deepEqual(await page.evaluate(() => [localStorage.getItem('cow_web_palette'), localStorage.getItem('cow_theme')]), ['slate', 'system']);
        await choose(page, 'language', 'en');
        assert.equal(await page.locator('#appearance-title').textContent(), 'Appearance');
        assert.equal(await page.evaluate(() => localStorage.getItem('cow_lang')), 'en');
        await page.locator('.appearance-close').click();
        await page.locator('#appearance-dialog').waitFor({ state: 'hidden' });
        await expectFocus(page, 'sidebar-account-toggle');
        await panel(page);
        assert.equal(await page.locator('input[name="web-palette"]:checked').inputValue(), 'slate');
        assert.equal(await page.locator('input[name="web-mode"]:checked').inputValue(), 'system');
        assert.equal(await page.locator('input[name="web-language"]:checked').inputValue(), 'en');
        await page.locator('input[name="web-mode"][value="system"]').focus();
        await page.keyboard.press('ArrowLeft');
        await state(page, { mode: 'dark', resolved: 'dark' });
        report.preferenceTabSequence = [];
        for (let i = 0; i < 12; i++) {
            await page.keyboard.press('Tab');
            const focus = await page.evaluate(() => ({ inside: document.getElementById('appearance-dialog').contains(document.activeElement),
                tag: document.activeElement.tagName, id: document.activeElement.id,
                name: document.activeElement.getAttribute('name'), value: document.activeElement.value || '' }));
            report.preferenceTabSequence.push(focus);
            // Chromium may traverse browser chrome at the cycle boundary,
            // reported as BODY. No background application control may focus.
            assert.ok(focus.inside || focus.tag === 'BODY', JSON.stringify(focus));
        }
        assert.ok(report.preferenceTabSequence.some(item => item.name === 'web-language'));
        assert.ok(report.preferenceTabSequence.some(item => item.name === 'web-palette'));
        await page.mouse.click(3, 3);
        await page.locator('#appearance-dialog').waitFor({ state: 'hidden' });
        await expectFocus(page, 'sidebar-account-toggle');
        await accountPanel(page); await choose(page, 'mode', 'system');
        await page.emulateMedia({ colorScheme: 'dark' });
        await state(page, { palette: 'slate', mode: 'system', resolved: 'dark' });
        await closePanel(page);
        await expectFocus(page, 'sidebar-account-toggle');
        assert.deepEqual(report.requests.slice(start).filter(r => !['GET', 'HEAD'].includes(r.method) && r.pathname !== '/poll'), []);
        await ctx.close();

        const blocked = await context({ denyWrite: 'cow_web_palette' });
        const failurePage = await open(blocked); await accountPanel(failurePage);
        await choose(failurePage, 'palette', 'classic');
        assert.equal(await failurePage.locator('#appearance-storage-warning').isVisible(), true);
        await closePanel(failurePage); await panel(failurePage);
        assert.equal(await failurePage.locator('#appearance-storage-warning').isVisible(), true);
        assert.equal(await failurePage.locator('input[name="web-palette"]:checked').inputValue(), 'classic');
        await blocked.close();
    });

    await scenario('same browser logout, login another account and production tenant switching retain appearance', async () => {
        const ctx = await context({ identity: 'database', system: 'dark' });
        const page = await open(ctx);
        await panel(page); await choose(page, 'palette', 'slate'); await closePanel(page);
        const before = await page.evaluate(() => [localStorage.getItem('cow_web_palette'), localStorage.getItem('cow_theme')]);
        await page.locator('#sidebar-account-toggle').click();
        await page.locator('#account-menu-logout').click();
        await page.locator('#login-form').waitFor({ state: 'visible' });
        await state(page, { palette: 'slate', mode: 'system', resolved: 'dark' });
        await page.locator('#login-username').fill('appearance-member-b');
        await page.locator('#login-password').fill('fixture-only-password');
        await page.locator('#login-btn').click();
        await page.locator('#app').waitFor({ state: 'visible' });
        await page.waitForFunction(() => _accountState.username === 'appearance-member-b');
        await state(page, { palette: 'slate', mode: 'system', resolved: 'dark' });
        await page.locator('#sidebar-account-toggle').click();
        const nativePrompt = page.waitForEvent('dialog');
        const tenantClick = page.locator('#account-menu-tenant').click();
        const prompt = await nativePrompt;
        assert.equal(prompt.type(), 'prompt', prompt.message());
        assert.match(prompt.message(), /测试租户 B/);
        await prompt.accept('2');
        await tenantClick;
        await page.waitForFunction(() => sessionStorage.getItem('cow_tenant_id') === 'fixture-tenant-b');
        await page.locator('#app').waitFor({ state: 'visible' });
        assert.equal(new URL(page.url()).searchParams.has('switch_tenant'), false);
        assert.equal(await page.evaluate(() => _accountState.username), 'appearance-member-b');
        assert.deepEqual(await page.evaluate(() => [localStorage.getItem('cow_web_palette'), localStorage.getItem('cow_theme')]), before);
        await state(page, { palette: 'slate', mode: 'system', resolved: 'dark' });
        report.identityTransitions = { fixtureBackend: true, logout: true, loginAccountB: true, tenantB: true, appearancePreserved: true };
        await ctx.close();
    });

    await scenario('database safe agent projection keeps composer team entry and continuous session invitations', async () => {
        // Match the backend's non-sensitive database projection: no enabled,
        // workspace, model credentials, or default_agent_id fields are added.
        const roster = [
            { id: 'research', name: '资料助手', description: '资料整理', avatar: '', is_default: false, can_chat: true },
            { id: 'default', name: '默认助手', description: '当前会话主智能体', avatar: '', is_default: true, can_chat: true },
            { id: 'coder', name: '编程助手', description: '代码协作', avatar: '', is_default: false, can_chat: true },
        ];
        const ctx = await context({ identity: 'database', roster });
        const page = await open(ctx);
        await page.locator('#composer-agent-btn').waitFor({ state: 'visible' });
        assert.deepEqual(await page.evaluate(() => [defaultAgentId, activeAgentId]), ['default', 'default'],
            'is_default wins over the first projected Agent');
        assert.equal(await page.locator('.composer-group-end > #composer-identity').count(), 1);
        assert.equal(await page.evaluate(() => document.getElementById('model-selector-btn').compareDocumentPosition(
            document.getElementById('composer-agent-btn')) & Node.DOCUMENT_POSITION_FOLLOWING), 4);
        const projected = await (await ctx.request.get(origin + '/api/agents')).json();
        assert.equal(projected.agents.length, 3);
        assert.ok(projected.agents.every(agent => !Object.hasOwn(agent, 'enabled')));
        assert.equal(Object.hasOwn(projected, 'default_agent_id'), false);
        await page.locator('#composer-agent-btn').click();
        await page.locator('#composer-agent-menu').waitFor({ state: 'visible' });
        assert.equal(await page.locator('#composer-agent-menu [onclick^="inviteTeamMember("]').count(), 2);
        await settleColors(page);
        await page.screenshot({ path: path.join(output, '1440x900-multi-agent-home.png') });
        await page.locator('#composer-agent-btn').click();

        await page.setViewportSize({ width: 375, height: 812 });
        await settleColors(page);
        await page.locator('#composer-agent-btn').click();
        await page.locator('#composer-agent-menu').waitFor({ state: 'visible' });
        const mobileHomeMenu = await page.locator('#composer-agent-menu').boundingBox();
        report.multiAgentMobileHomeMenu = mobileHomeMenu;
        assert.ok(mobileHomeMenu.x >= 0 && mobileHomeMenu.x + mobileHomeMenu.width <= 376);
        await page.locator('#composer-agent-menu [onclick="pickComposerAgent(\'default\')"]').click();
        assert.equal(await page.locator('#composer-agent-menu').isVisible(), false);
        await page.locator('#composer-agent-btn').click();
        await page.locator('#composer-agent-menu [onclick="inviteTeamMember(\'coder\')"]').scrollIntoViewIfNeeded();
        await settleColors(page);
        await page.screenshot({ path: path.join(output, '375x812-multi-agent-home.png') });
        await page.locator('#composer-agent-menu [onclick="inviteTeamMember(\'coder\')"]').click();
        await page.waitForFunction(() => currentTeamIds().length === 1 && currentTeamIds()[0] === 'coder');
        assert.equal(await page.locator('#chat-main.chat-home').count(), 1);
        await page.locator('#composer-agent-menu [onclick="removeTeamMember(\'coder\')"]').click();
        await page.waitForFunction(() => currentTeamIds().length === 0);
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
        await page.locator('#composer-agent-btn').click();
        await page.setViewportSize({ width: 1440, height: 900 });
        await settleColors(page);

        await page.locator('#chat-input').fill('Create an existing fixture conversation before inviting teammates');
        await page.locator('#send-btn').click();
        await page.locator('[data-request-id="appearance-send-fixture"] .answer-content').waitFor();
        streams.get('appearance-send-fixture').write('data: ' + JSON.stringify({ type: 'stream_end', seq: 2 }) + '\n\n');
        await page.waitForFunction(() => !activeStreams['appearance-send-fixture']);
        await page.locator('#chat-input').fill('Keep this draft while inviting multiple agents');
        const before = await page.evaluate(() => ({ session: sessionId, agent: activeAgentId, draft: chatInput.value }));
        const writeStart = report.teamSettingsWrites?.length || 0;
        await page.locator('#composer-agent-btn').click();
        await page.locator('#composer-agent-menu [onclick="inviteTeamMember(\'research\')"]').click();
        await page.waitForFunction(() => currentTeamIds().length === 1 && currentTeamIds()[0] === 'research');
        assert.equal(await page.locator('#composer-agent-menu').isVisible(), true);
        assert.equal(await page.locator('.composer-agent-count').textContent(), '2');
        await page.locator('#composer-agent-menu [onclick="inviteTeamMember(\'coder\')"]').click();
        await page.waitForFunction(() => currentTeamIds().length === 2 && currentTeamIds().includes('coder'));
        assert.equal(await page.locator('#composer-agent-menu').isVisible(), true);
        assert.equal(await page.locator('.composer-agent-count').textContent(), '3');
        await page.locator('#composer-agent-menu [onclick="removeTeamMember(\'research\')"]').click();
        await page.waitForFunction(() => currentTeamIds().length === 1 && currentTeamIds()[0] === 'coder');
        assert.equal(await page.locator('#composer-agent-menu').isVisible(), true);
        assert.equal(await page.locator('.composer-agent-count').textContent(), '2');
        assert.deepEqual(await page.evaluate(() => ({ session: sessionId, agent: activeAgentId, draft: chatInput.value })), before);
        const writes = report.teamSettingsWrites.slice(writeStart);
        assert.equal(writes.length, 3);
        assert.deepEqual(writes.map(item => item.members), [['research'], ['research', 'coder'], ['coder']]);
        assert.ok(writes.every(item => item.pathname === `/api/sessions/${encodeURIComponent(before.session)}/settings`));
        await settleColors(page);
        await page.screenshot({ path: path.join(output, '1440x900-multi-agent-conversation.png') });
        await page.locator('#composer-agent-btn').click();
        await page.setViewportSize({ width: 375, height: 812 });
        await settleColors(page);
        await page.locator('#composer-agent-btn').click();
        await page.locator('#composer-agent-menu').waitFor({ state: 'visible' });
        const menu = await page.locator('#composer-agent-menu').boundingBox();
        report.multiAgentMobileMenu = menu;
        await page.screenshot({ path: path.join(output, '375x812-multi-agent-menu-check.png') });
        assert.ok(menu.x >= 0 && menu.x + menu.width <= 376);
        await page.locator('#composer-agent-menu [onclick="inviteTeamMember(\'research\')"]').click();
        await page.waitForFunction(() => currentTeamIds().length === 2);
        assert.equal(await page.locator('#chat-input').inputValue(), before.draft);
        assert.equal(await page.evaluate(() => sessionId), before.session);
        await settleColors(page);
        await page.screenshot({ path: path.join(output, '375x812-multi-agent-conversation.png') });
        report.multiAgent = { safeProjection: projected, owner: before.agent, session: before.session,
            consecutiveMembers: writes.map(item => item.members), mobileClickable: true,
            mobileHomeUpperAndLowerItemsClickable: true, draftPreserved: true };
        await ctx.close();
    });

    await scenario('homepage layout, real upload and send, new chat and collapsed navigation preserve composer', async () => {
        const ctx = await context();
        const page = await open(ctx);
        await page.evaluate(() => { window.__originalComposer = document.getElementById('chat-input'); });
        report.homeLayout = [];
        for (const viewport of [{ width: 1440, height: 900 }, { width: 1280, height: 720 }, { width: 375, height: 812 }]) {
            await page.setViewportSize(viewport); await settleColors(page);
            const geometry = await page.evaluate(() => {
                const rect = selector => { const r = document.querySelector(selector).getBoundingClientRect(); return { top: r.top, bottom: r.bottom, left: r.left, right: r.right }; };
                return { intro: rect('.home-intro'), composer: rect('#chat-input-area'), suggestions: rect('.home-suggestions'),
                    sameInput: window.__originalComposer === document.getElementById('chat-input'),
                    mainScrolls: getComputedStyle(document.getElementById('chat-main')).overflowY,
                    documentWidth: document.documentElement.scrollWidth, viewportWidth: innerWidth };
            });
            assert.ok(geometry.intro.bottom <= geometry.composer.top + 1);
            assert.ok(geometry.composer.bottom <= geometry.suggestions.top + 1);
            assert.equal(geometry.sameInput, true);
            assert.ok(geometry.documentWidth <= viewport.width);
            assert.ok(['auto', 'scroll'].includes(geometry.mainScrolls));
            await page.locator('button.example-card').last().scrollIntoViewIfNeeded();
            report.homeLayout.push({ ...viewport, ...geometry });
            if (viewport.width === 1440) {
                await page.locator('.home-intro').scrollIntoViewIfNeeded();
                await page.screenshot({ path: path.join(output, '1440x900-business-light-home.png') });
            }
            await page.locator('#chat-input').fill('/');
            await page.locator('#slash-menu').waitFor({ state: 'visible' });
            await page.locator('#slash-menu .slash-menu-item').last().scrollIntoViewIfNeeded();
            const popup = await page.locator('#slash-menu').boundingBox();
            assert.ok(popup.x >= 0 && popup.x + popup.width <= viewport.width + 1);
            await page.keyboard.press('Escape');
            await page.locator('#slash-menu').waitFor({ state: 'hidden' });
            await page.locator('#chat-input').fill('');
            await page.locator('#workspace-selector-btn').click();
            await page.locator('#workspace-selector-menu').waitFor({ state: 'visible' });
            const workspacePopup = await page.locator('#workspace-selector-menu').boundingBox();
            assert.ok(workspacePopup.x >= 0 && workspacePopup.x + workspacePopup.width <= viewport.width + 1);
            assert.ok(workspacePopup.y >= 0, 'workspace menu must not clip above the viewport');
            assert.ok(
                workspacePopup.y + workspacePopup.height <= viewport.height + 1,
                'workspace menu must not clip below the viewport',
            );
            const composerBox = await page.locator('#composer-card').boundingBox();
            assert.ok(
                workspacePopup.y + workspacePopup.height <= composerBox.y + 1,
                'home workspace menu opens above the composer',
            );
            await page.locator('#workspace-selector-btn').click();
        }
        await page.setViewportSize({ width: 1440, height: 900 });
        await page.locator('#app header button[onclick="toggleSidebar()"]').click();
        await page.waitForFunction(() => document.getElementById('app').classList.contains('sidebar-collapsed'));
        await page.locator('#sidebar-account-toggle').click();
        const collapsedAccount = await page.locator('#sidebar-account-menu').boundingBox();
        assert.ok(collapsedAccount.width >= 220 && collapsedAccount.x >= 0 && collapsedAccount.x + collapsedAccount.width <= 1440);
        await page.keyboard.press('Escape');
        await page.locator('#app header button[onclick="toggleSidebar()"]').click();
        await page.waitForFunction(() => !document.getElementById('app').classList.contains('sidebar-collapsed'));
        const sendsBefore = report.sentMessages?.length || 0;
        await page.locator('button.example-card').first().click();
        assert.ok(await page.locator('#chat-input').inputValue());
        assert.equal(report.sentMessages?.length || 0, sendsBefore, 'Task suggestions only prepare a draft');
        await page.locator('#chat-input').fill('Send this fixture-only task');
        await page.locator('#file-input').setInputFiles({ name: 'appearance-fixture.txt', mimeType: 'text/plain', buffer: Buffer.from('Browser fixture content') });
        await page.waitForFunction(() => pendingAttachments.length === 1 && !pendingAttachments[0]._uploading);
        await page.evaluate(() => { window.__pendingAttachment = document.querySelector('#attachment-preview .att-chip'); });
        await panel(page); await choose(page, 'palette', 'classic'); await choose(page, 'mode', 'dark'); await closePanel(page);
        assert.equal(await page.locator('#chat-input').inputValue(), 'Send this fixture-only task');
        assert.equal(await page.evaluate(() => window.__pendingAttachment === document.querySelector('#attachment-preview .att-chip')), true);
        const firstSession = await page.evaluate(() => sessionId);
        await page.locator('#send-btn').click();
        await page.locator('[data-request-id="appearance-send-fixture"] .answer-content').waitFor();
        assert.equal(await page.locator('#welcome-screen').count(), 0);
        assert.equal(await page.locator('#chat-main').evaluate(el => el.classList.contains('chat-home')), false);
        assert.equal(await page.evaluate(() => window.__originalComposer === document.getElementById('chat-input')), true);
        assert.equal(report.sentMessages.at(-1).message, 'Send this fixture-only task');
        assert.equal(report.sentMessages.at(-1).attachments[0].file_name, 'appearance-fixture.txt');
        await page.locator('#chat-input').fill('Draft carried into new chat');
        await page.locator('#file-input').setInputFiles({ name: 'appearance-fixture.txt', mimeType: 'text/plain', buffer: Buffer.from('Next task attachment') });
        await page.waitForFunction(() => pendingAttachments.length === 1 && !pendingAttachments[0]._uploading);
        const activeBeforeNewChat = await page.evaluate(() => ({ agent: activeAgentId, attachments: JSON.stringify(pendingAttachments) }));
        await page.locator('#sidebar-new-chat').click();
        await page.locator('#welcome-screen').waitFor({ state: 'attached' });
        assert.equal(await page.locator('button.example-card').count(), 6);
        assert.notEqual(await page.evaluate(() => sessionId), firstSession);
        assert.equal(await page.evaluate(() => window.__originalComposer === document.getElementById('chat-input')), true);
        assert.equal(await page.locator('#chat-input').inputValue(), 'Draft carried into new chat');
        assert.deepEqual(await page.evaluate(() => ({ agent: activeAgentId, attachments: JSON.stringify(pendingAttachments) })), activeBeforeNewChat);
        assert.equal(await page.evaluate(() => !!activeStreams['appearance-send-fixture']), true);
        streams.get('appearance-send-fixture').write('data: ' + JSON.stringify({ type: 'delta', content: 'Background reply continues.', seq: 2 }) + '\n\n');
        await page.waitForFunction(() => streamBuffers['appearance-send-fixture']?.items.some(item => item.seq === 2));
        assert.equal(await page.locator('[data-request-id="appearance-send-fixture"]').count(), 0);
        streams.get('appearance-send-fixture').write('data: ' + JSON.stringify({ type: 'stream_end', seq: 3 }) + '\n\n');
        await page.waitForFunction(() => !activeStreams['appearance-send-fixture']);
        await sidebarNavigate(page, 'config');
        await sidebarNavigate(page, 'chat');
        assert.equal(await page.locator('#chat-input').inputValue(), 'Draft carried into new chat');
        await sidebarNavigate(page, 'config');
        const guardBefore = await page.evaluate(() => {
            const editor = document.createElement('textarea'); editor.id = 'ws-editor';
            editor.value = 'Unsaved workspace fixture'; document.getElementById('view-config').appendChild(editor);
            wsEditing = true; wsEditBaseline = 'Original workspace fixture';
            return { view: currentView, session: sessionId, draft: chatInput.value };
        });
        await page.locator('#sidebar-new-chat').click();
        await page.locator('#confirm-dialog-overlay').waitFor({ state: 'visible' });
        assert.deepEqual(await page.evaluate(() => ({ view: currentView, session: sessionId, draft: chatInput.value })), guardBefore);
        await page.locator('#confirm-dialog-cancel').click();
        assert.deepEqual(await page.evaluate(() => ({ view: currentView, session: sessionId, draft: chatInput.value })), guardBefore);
        assert.equal(await page.evaluate(() => document.activeElement === chatInput), false);
        assert.equal(await page.locator('#ws-editor').inputValue(), 'Unsaved workspace fixture');
        await page.evaluate(() => { document.getElementById('ws-editor').remove(); wsDiscardEditState(); });
        await ctx.close();

        const customBrand = { enabled: true, revision: 19,
            brand_name: 'Fixture已发布企业品牌 VeryLongProductNameWithoutSpacesForResponsiveBrandVerification',
            logo_description: 'Published fixture description', logo_url: '/assets/github.png', favicon_url: '/assets/favicon.ico' };
        const brandedContext = await context({ brand: customBrand });
        const brandedPage = await open(brandedContext);
        await brandedPage.locator('#sidebar-new-chat').click();
        await brandedPage.setViewportSize({ width: 375, height: 812 });
        await settleColors(brandedPage);
        const published = await brandedPage.evaluate(() => ({ name: brandState.brand_name, logo: effectiveLogoUrl(),
            sidebarName: document.getElementById('sidebar-brand-name').textContent,
            welcomeName: document.getElementById('welcome-title').textContent,
            sidebarLogo: document.querySelector('.sidebar-brand-mark').getAttribute('src'),
            overflow: document.documentElement.scrollWidth > innerWidth }));
        assert.equal(published.name, customBrand.brand_name); assert.equal(published.logo, customBrand.logo_url);
        assert.equal(published.sidebarName, customBrand.brand_name); assert.equal(published.welcomeName, customBrand.brand_name);
        assert.equal(published.sidebarLogo, customBrand.logo_url); assert.equal(published.overflow, false);
        report.publishedBrand = published;
        await brandedPage.screenshot({ path: path.join(output, '375x812-published-custom-brand.png') });
        await brandedContext.close();
    });

    await scenario('same-origin tabs synchronize and remote clear restores defaults', async () => {
        const ctx = await context();
        const a = await open(ctx), b = await open(ctx);
        await panel(a); await choose(a, 'palette', 'slate'); await choose(a, 'mode', 'dark');
        await state(b, { palette: 'slate', mode: 'dark', resolved: 'dark' });
        await panel(b);
        assert.equal(await b.locator('input[name="web-palette"]:checked').inputValue(), 'slate');
        await choose(b, 'palette', 'classic');
        await state(a, { palette: 'classic', mode: 'dark' });
        await b.evaluate(() => { localStorage.removeItem('cow_theme'); localStorage.removeItem('cow_web_palette'); });
        await state(a, { palette: 'business', mode: 'system', resolved: 'light' });
        await ctx.close();
    });

    await scenario('whole-page localStorage read failure still reaches and operates appearance', async () => {
        const ctx = await context({ denyRead: true });
        const page = await open(ctx);
        await state(page, { palette: 'business', mode: 'system', storageFailed: true });
        await panel(page);
        assert.equal(await page.locator('#appearance-storage-warning').isVisible(), true);
        await choose(page, 'palette', 'slate'); await choose(page, 'mode', 'dark');
        await state(page, { palette: 'slate', mode: 'dark', resolved: 'dark', storageFailed: true });
        assert.equal(await page.locator('#appearance-storage-warning').isVisible(), true);
        await page.screenshot({ path: path.join(output, 'storage-read-denied.png') });
        await ctx.close();
    });

    await scenario('a partial appearance write reports in-page-only persistence', async () => {
        const ctx = await context({ initial: { cow_web_palette: 'classic', cow_theme: 'light' }, denyWrite: 'cow_web_palette' });
        const page = await open(ctx);
        await panel(page); await choose(page, 'palette', 'slate'); await choose(page, 'mode', 'dark');
        await state(page, { palette: 'slate', mode: 'dark', storageFailed: true });
        assert.equal(await page.locator('#appearance-storage-warning').isVisible(), true);
        assert.match(await page.locator('#appearance-storage-warning').textContent(), /仅本页生效/);
        await page.reload({ waitUntil: 'networkidle' });
        await state(page, { palette: 'classic', mode: 'dark' });
        await ctx.close();
    });

    await scenario('already-authorized database member and public login shell can choose locally', async () => {
        for (const identity of ['database', 'public']) {
            const ctx = await context({ identity });
            const page = await open(ctx);
            const start = report.requests.length;
            await panel(page); await choose(page, 'palette', 'slate'); await choose(page, 'mode', 'dark');
            await state(page, { palette: 'slate', mode: 'dark' });
            await closePanel(page);
            const writes = report.requests.slice(start).filter(r => !['GET', 'HEAD'].includes(r.method) && r.pathname !== '/poll');
            assert.deepEqual(writes, [], identity + ': appearance must not write server data');
            await ctx.close();
        }
    });

    await scenario('session expiry dismisses appearance and keeps login controls reachable', async () => {
        const ctx = await context({ identity: 'database' });
        const page = await open(ctx);
        await page.locator('#attach-btn').click();
        assert.equal(await page.locator('#attach-menu').isVisible(), true);
        await accountPanel(page);
        assert.equal(await page.locator('#attach-menu').isVisible(), false);
        await page.evaluate(() => fetch('/api/appearance-fixture-expire'));
        await page.locator('#login-form').waitFor({ state: 'visible' });
        await page.locator('#appearance-dialog').waitFor({ state: 'hidden' });
        assert.equal(await page.locator('#sidebar-account-toggle').evaluate(el => el === document.activeElement), false);
        await page.locator('#login-username').fill('member');
        await page.locator('#login-password').fill('fixture-password');
        assert.equal(await page.locator('#login-password').inputValue(), 'fixture-password');
        await page.screenshot({ path: path.join(output, 'expired-session-login.png') });
        await ctx.close();
    });

    await scenario('stream, attachment and independent brand preview survive appearance changes', async () => {
        const ctx = await context();
        const page = await open(ctx);
        const requestStart = report.requests.length;
        await page.evaluate(() => {
            pendingAttachments.push({ file_name: 'appearance-fixture.txt', file_path: '/fixture/appearance.txt', file_type: 'file' });
            renderAttachmentPreview();
            chatInput.value = 'Next draft while streaming';
            brandingDraft = { brand_name: 'Custom fixture brand', logo_description: 'Local preview only', logoPreviewUrl: '/assets/rongda-ai-mark.svg' };
            brandingPreviewDark = true; _brandingBindEvents(); _brandingRenderPreview();
            document.getElementById('welcome-screen')?.remove();
            const loading = document.createElement('div'); messagesDiv.appendChild(loading);
            startSSE('appearance-stream-fixture', loading, new Date(), null);
            window.__streamPreservation = { session: sessionId, agent: activeAgentId,
                attachments: JSON.stringify(pendingAttachments), attachmentNode: document.querySelector('#attachment-preview .att-chip'),
                brand: JSON.stringify(brandState), draft: JSON.stringify(brandingDraft) };
        });
        await page.getByText('First streamed segment.', { exact: false }).waitFor();
        await page.evaluate(() => { window.__streamPreservation.node = document.querySelector('[data-request-id="appearance-stream-fixture"]'); });
        await panel(page); await choose(page, 'palette', 'slate'); await choose(page, 'mode', 'dark');
        assert.ok(streams.has('appearance-stream-fixture'), 'The real EventSource connection must remain open');
        streams.get('appearance-stream-fixture').write('data: ' + JSON.stringify({ type: 'delta', content: 'Second segment during appearance selection.', seq: 2 }) + '\n\n');
        await page.waitForFunction(() => document.querySelector('[data-request-id="appearance-stream-fixture"] .answer-content')?.textContent.includes('Second segment'));
        await closePanel(page);
        await page.locator('#branding-theme-light').evaluate(el => el.click());
        const preserved = await page.evaluate(() => {
            const before = window.__streamPreservation;
            return { node: before.node === document.querySelector('[data-request-id="appearance-stream-fixture"]'),
                attachmentNode: before.attachmentNode === document.querySelector('#attachment-preview .att-chip'),
                session: before.session === sessionId, agent: before.agent === activeAgentId,
                attachments: before.attachments === JSON.stringify(pendingAttachments), brand: before.brand === JSON.stringify(brandState),
                draft: before.draft === JSON.stringify(brandingDraft), composer: chatInput.value,
                previewDark: brandingPreviewDark, previewClass: document.getElementById('branding-preview-canvas').classList.contains('dark'),
                rootDark: document.documentElement.classList.contains('dark'),
                content: before.node.querySelector('.answer-content').textContent };
        });
        for (const key of ['node', 'attachmentNode', 'session', 'agent', 'attachments', 'brand', 'draft', 'rootDark']) assert.equal(preserved[key], true, key);
        assert.equal(preserved.composer, 'Next draft while streaming');
        assert.equal(preserved.previewDark, false); assert.equal(preserved.previewClass, false);
        assert.match(preserved.content, /First streamed segment\. Second segment during appearance selection\./);
        streams.get('appearance-stream-fixture').write('data: ' + JSON.stringify({ type: 'stream_end', seq: 3 }) + '\n\n');
        await page.waitForFunction(() => !activeStreams['appearance-stream-fixture']);
        await settleColors(page);
        await page.screenshot({ path: path.join(output, 'stream-attachment-preserved.png') });
        await sidebarNavigate(page, 'config');
        await page.locator('#view-config').waitFor({ state: 'visible' });
        report.configReadability = [];
        for (const mode of ['light', 'dark']) {
            await panel(page); await choose(page, 'mode', mode); await closePanel(page); await settleColors(page);
            const audit = await contrastAudit(page, [
                { selector: '#view-config h2', area: 'config page title' },
                { selector: '#view-config [data-i18n="config_desc"]', area: 'config description' },
                { selector: '#view-config h3', area: 'config card title' },
            ]);
            assert.ok(audit.every(item => item.ratio >= 4.5 && item.onScreen), 'Configuration sample remains readable in ' + mode);
            report.configReadability.push({ mode, audit });
            await page.screenshot({ path: path.join(output, 'configuration-' + mode + '.png') });
        }
        await sidebarNavigate(page, 'history');
        await page.locator('#view-history').waitFor({ state: 'visible' });
        await page.waitForFunction(() => document.getElementById('history-status').textContent.includes('暂无历史会话'));
        report.historyReadability = [];
        for (const mode of ['light', 'dark']) {
            await panel(page); await choose(page, 'mode', mode); await closePanel(page); await settleColors(page);
            const selectors = [
                { selector: '#view-history h2', area: 'history page title' },
                { selector: '#view-history [data-i18n="history_desc"]', area: 'history description' },
                { selector: '#history-status', area: 'history rendered empty state' },
            ];
            const audit = await contrastAudit(page, selectors);
            await page.screenshot({ path: path.join(output, 'history-' + mode + '.png') });
            await page.locator('link[href="assets/css/appearance.css"]').evaluate(el => { el.disabled = true; });
            await settleColors(page);
            const baseline = await contrastAudit(page, selectors);
            await page.locator('link[href="assets/css/appearance.css"]').evaluate(el => { el.disabled = false; });
            await settleColors(page);
            const comparison = audit.map((item, index) => ({ area: item.area, ratio: item.ratio,
                baselineRatio: baseline[index].ratio, textColorUnchanged: item.color === baseline[index].color,
                ratioChangePercent: (item.ratio / baseline[index].ratio - 1) * 100,
                backgroundUnchanged: JSON.stringify(item.background) === JSON.stringify(baseline[index].background) }));
            report.historyReadability.push({ mode, audit, baseline, comparison, existingBelowTarget: baseline.filter(item => item.ratio < 4.5) });
            assert.ok(audit.every(item => item.onScreen), 'History title and rendered empty state must remain on screen');
            assert.ok(comparison.every(item => item.textColorUnchanged), 'History text colors must match the existing same-mode baseline');
            for (const item of comparison) {
                const existingLightSecondary = mode === 'light' && item.baselineRatio < 4.5
                    && ['history description', 'history rendered empty state'].includes(item.area);
                if (existingLightSecondary) {
                    // These existing auxiliary labels are outside the palette
                    // styling scope. Record their exact shortfall and require
                    // less than 1% change from the original same-mode baseline.
                    assert.ok(Math.abs(item.ratioChangePercent) < 1, 'Existing secondary text must not materially regress');
                } else assert.ok(item.ratio >= 4.5, 'History normal text must retain 4.5:1 contrast');
            }
        }
        assert.deepEqual(report.requests.slice(requestStart).filter(r => !['GET', 'HEAD'].includes(r.method) && r.pathname !== '/poll'), []);
        report.streamPreservation = preserved;
        await ctx.close();
    });

    await scenario('all localized labels and palette/mode/viewport visual matrix', async () => {
        for (const [lang, title, prefs] of [['zh', '外观', '个人偏好'], ['zh-Hant', '外觀', '個人偏好'], ['en', 'Appearance', 'Preferences']]) {
            const ctx = await context({ initial: { cow_lang: lang }, viewport: { width: 375, height: 812 } });
            const page = await open(ctx); await panel(page);
            assert.equal(await page.locator('#appearance-title').textContent(), title);
            // Appearance is reached from the account menu only, so that entry's
            // label must follow the same language as the panel it opens.
            assert.equal(await page.locator('#account-menu-prefs span[data-i18n="account_menu_prefs"]').textContent(), prefs);
            await page.screenshot({ path: path.join(output, `mobile-${lang}-panel.png`) });
            await ctx.close();
        }
        const ctx = await context();
        const page = await open(ctx);
        for (const viewport of [{ width: 1440, height: 900 }, { width: 1280, height: 720 }, { width: 375, height: 812 }]) {
            await page.setViewportSize(viewport);
            for (const palette of ['business', 'slate', 'classic']) {
                for (const mode of ['light', 'dark']) {
                    await panel(page); await choose(page, 'palette', palette); await choose(page, 'mode', mode);
                    await closePanel(page);
                    await settleColors(page);
                    const stem = `${viewport.width}x${viewport.height}-${palette}-${mode}`;
                    const entry = { ...viewport, palette, mode, screenshots: [], contrast: [] };
                    const contentImage = path.join(output, stem + '-home.png');
                    await page.screenshot({ path: contentImage }); entry.screenshots.push(contentImage);
                    entry.contrast.push(...await contrastAudit(page, contentAuditSelectors));
                    if (viewport.width === 1440) {
                        await page.locator('#sidebar-account-toggle').click();
                        await settleColors(page);
                        entry.contrast.push(...await contrastAudit(page, [{ selector: '#sidebar-version', area: 'account menu version' }]));
                        const menuImage = path.join(output, stem + '-account.png');
                        await page.screenshot({ path: menuImage }); entry.screenshots.push(menuImage);
                        await page.keyboard.press('Escape');
                    }
                    await panel(page);
                    const panelImage = path.join(output, stem + '-panel.png');
                    await page.screenshot({ path: panelImage }); entry.screenshots.push(panelImage);
                    entry.contrast.push(...await contrastAudit(page, panelAuditSelectors));
                    entry.geometry = await page.evaluate(() => {
                        const dialog = document.getElementById('appearance-dialog'), rect = dialog.getBoundingClientRect();
                        return { documentWidth: document.documentElement.scrollWidth, viewport: innerWidth,
                            dialogWidth: dialog.scrollWidth, dialogClientWidth: dialog.clientWidth,
                            dialogRect: { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom },
                            resetVisible: !!document.querySelector('.appearance-reset').getClientRects().length,
                            closeVisible: !!document.querySelector('.appearance-close').getClientRects().length,
                            cardCount: document.querySelectorAll('.example-card').length,
                            topHeaderHeight: document.querySelector('#app header').getBoundingClientRect().height,
                            headerCount: document.querySelectorAll('#app header').length,
                            agentInsideHeader: !!document.querySelector('#app header #chat-agent-identity') };
                    });
                    report.visual.push(entry);
                    await closePanel(page);
                }
            }
        }
        await ctx.close();
        const badContrast = report.visual.flatMap(v => v.contrast.filter(c => c.ratio < 4.5)
            .map(c => ({ viewport: v.width, palette: v.palette, mode: v.mode, ...c })));
        const badGeometry = report.visual.filter(v => v.geometry.documentWidth > v.width
            || v.geometry.dialogWidth > v.geometry.dialogClientWidth + 1
            || v.geometry.dialogRect.left < 0 || v.geometry.dialogRect.right > v.width + 1
            || v.geometry.dialogRect.top < 0 || v.geometry.dialogRect.bottom > v.height + 1
            || !v.geometry.resetVisible || !v.geometry.closeVisible
            || v.geometry.cardCount !== 6 || v.geometry.topHeaderHeight <= 0 || v.geometry.headerCount !== 1 || !v.geometry.agentInsideHeader);
        report.contrastFailures = badContrast;
        report.geometryFailures = badGeometry;
        assert.deepEqual(badGeometry, [], 'Visual geometry must fit the viewport and use one header and six task buttons');
        assert.deepEqual(badContrast, [], 'In-scope normal text must have at least 4.5:1 contrast');
    });

    assert.deepEqual(report.pageErrors, [], 'Unexpected production page errors');
    assert.deepEqual(report.unexpectedRoutes, [], 'Every production request/asset should be covered');
    console.log(JSON.stringify({ passed: report.scenarios.length, visualCombinations: report.visual.length,
        pageErrors: report.pageErrors.length, output }, null, 2));
})().catch(error => {
    report.error = { message: error.message, stack: error.stack };
    console.error(error.message);
    process.exitCode = 1;
}).finally(async () => {
    fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(report, null, 2));
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
});
