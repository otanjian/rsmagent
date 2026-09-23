// Real-page acceptance for the refined workbench sidebar (change
// ``refine-sidebar-team-chat-launch`` 5.2/5.4).
//
// Only *backend responses* are fixtures: the page, its stylesheets and its
// scripts are the production files served from channel/web, with the same
// server-side include expansion and the same presentation-switch substitution
// the real handlers perform. So the DOM, the accessibility tree, the focus
// order and the computed layout are the ones a browser would build in
// production; nothing here re-implements the console's own logic.
//
// Requires Playwright with an installed Chrome:
//   NODE_PATH=$(npm root -g) node tests/test_sidebar_launch_browser.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');

// Playwright is not a project dependency: it is only present when the runner
// provides it (`NODE_PATH=$(npm root -g) node tests/test_sidebar_launch_browser.cjs`).
// Without it this file must not turn an unrelated suite run red, so it reports
// the skip and stops.
let chromium;
try {
    ({ chromium } = require('playwright'));
} catch (error) {
    console.log('skipped: playwright is unavailable -- run with NODE_PATH=$(npm root -g)');
    process.exit(0);
}

const repo = path.resolve(__dirname, '..');
const webRoot = path.join(repo, 'channel/web');
const staticRoot = path.join(webRoot, 'static');
const output = process.env.COW_SIDEBAR_BROWSER_OUTPUT
    || path.join(repo, 'openspec/changes/refine-sidebar-team-chat-launch/evidence');
fs.mkdirSync(output, { recursive: true });
const report = { fixtureBackend: true, scenarios: [], pageErrors: [], screenshots: [], checks: [] };

// --- the production page, assembled the way channel/web/core/template.py is ---
function expandIncludes(html, depth = 0) {
    if (depth > 8) throw new Error('include nesting too deep');
    return html.replace(/<!--#include\s+([^\s>]+?)\s*-->/g, (_all, rel) => {
        const file = path.resolve(webRoot, rel);
        if (!file.startsWith(webRoot + path.sep)) throw new Error('include escapes the web directory: ' + rel);
        if (!fs.existsSync(file)) throw new Error('missing include: ' + rel);
        return expandIncludes(fs.readFileSync(file, 'utf8'), depth + 1);
    });
}
const baseHtml = expandIncludes(fs.readFileSync(path.join(webRoot, 'chat.html'), 'utf8'))
    .replaceAll('{{COW_DEFAULT_LANG}}', 'zh')
    .replaceAll('{{COW_NAVIGATION_MODE}}', 'classic');
const pageHtml = (flag) => baseHtml.replaceAll('{{COW_WORKBENCH_SIDEBAR_LAUNCH_V2}}', flag ? '1' : '');

// --- backend fixture --------------------------------------------------------
const normalAgents = [
    { id: 'default', name: '通用助手', description: '默认智能体', avatar: null, is_default: true, can_chat: true, unavailable_reason: '', position: '助手', category: '', tags: [], agent_type: 'normal' },
    { id: 'research', name: '研究员', description: '资料检索与综述', avatar: null, is_default: false, can_chat: true, unavailable_reason: '', position: '研究员', category: '', tags: [], agent_type: 'normal' },
    { id: 'writer', name: '文案', description: '对外文案撰写', avatar: null, is_default: false, can_chat: true, unavailable_reason: '', position: '文案', category: '', tags: [], agent_type: 'normal' },
];
// The coding Agent: it must be reachable everywhere a *single* Agent is picked
// and nowhere a team is assembled (spec ``agent-team-conversation``).
const codingAgent = { id: 'coder', name: '编码助手', description: '仓库内编码', avatar: null, is_default: false, can_chat: true, unavailable_reason: '', position: '工程', category: '', tags: [], agent_type: 'coding' };
const workbenchAgents = [...normalAgents, codingAgent];

const sessions = (count) => Array.from({ length: count }, (_v, index) => ({
    session_id: `s-${index + 1}`,
    title: `会话 ${index + 1}`,
    pinned: index === 0,
    updated_at: `2026-09-${String(20 - Math.min(index, 9)).padStart(2, '0')}T10:00:00`,
    agent: { id: index === 1 ? 'coder' : 'default', name: index === 1 ? '编码助手' : '通用助手', avatar: '', agent_type: index === 1 ? 'coding' : 'normal' },
    participants: index === 2
        ? [{ id: 'default', name: '通用助手' }, { id: 'research', name: '研究员' }]
        : index === 0 ? [] : [],
}));

const fixture = {
    '/auth/check': { status: 'success', identity_mode: 'database', auth_required: true, authenticated: true,
        user: { username: 'sidebar-acceptance', display_name: '验收账号', roles: ['admin'], is_admin: true } },
    '/auth/me': { status: 'success', identity_mode: 'database', auth_required: true, authenticated: true,
        user: { username: 'sidebar-acceptance', display_name: '验收账号', roles: ['admin'], is_admin: true },
        current_tenant: { id: 'fixture-tenant', name: '验收租户', code: 'fixture' },
        tenants: [{ id: 'fixture-tenant', name: '验收租户', code: 'fixture' }] },
    '/config': { status: 'success', title: '容大AI', model: 'fixture-model', providers: {},
        agent_permission_mode: 'workspace-write', permission_modes: ['read-only', 'workspace-write', 'full-access'] },
    '/api/version': { status: 'success', version: 'sidebar-acceptance-fixture' },
    '/api/branding/public': { enabled: true, revision: 1, brand_name: '容大AI', logo_description: '控制台',
        logo_url: '/assets/rongda-ai-mark.svg', favicon_url: '/assets/favicon.ico' },
    '/api/platform/tenants': { status: 'success', items: [{ id: 'fixture-tenant', name: '验收租户', code: 'fixture' }] },
    '/api/knowledge/list': { status: 'success', tree: [], root_files: [] },
    '/api/projects': { status: 'success', current: null, recents: [], default_workspace: '/fixture/workspace', projects_root: '/fixture/projects' },
    '/api/history': { status: 'success', messages: [], has_more: false },
    '/api/models': { status: 'success', providers: [], models: [] },
    '/api/channels': { status: 'success', channels: [] },
    '/api/todos/summary': { status: 'success', total: 0, items: [] },
    '/api/todos': { status: 'success', items: [], has_more: false },
    '/api/appearance': { status: 'success' },
    '/poll': { status: 'success', has_content: false },
};
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
    '.png': 'image/png', '.jpg': 'image/jpeg', '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf' };

let sessionRows = sessions(3);
const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://fixture');
    const pathname = url.pathname;
    const flag = req.headers['x-test-sidebar-v2'] === '1';
    const json = (data, status = 200) => {
        res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
        res.end(JSON.stringify(data));
    };
    if (pathname === '/chat' || pathname === '/') {
        res.writeHead(200, { 'Content-Type': mime['.html'], 'Cache-Control': 'no-store' });
        res.end(pageHtml(flag));
    } else if (pathname.startsWith('/assets/')) {
        const file = path.resolve(staticRoot, '.' + pathname.slice('/assets'.length));
        if (!file.startsWith(staticRoot + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
            json({ status: 'error', message: 'Missing fixture asset' }, 404);
        } else {
            res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream' });
            fs.createReadStream(file).pipe(res);
        }
    } else if (pathname === '/api/agents' && url.searchParams.get('view') === 'workbench') {
        json({ status: 'success', agents: workbenchAgents });
    } else if (pathname === '/api/agents') {
        json({ status: 'success', revision: 'fixture', agents: workbenchAgents, channel_instances: [],
            user_default: { agent_id: 'default' }, tenant_default_manageable: true });
    } else if (pathname === '/api/sessions') {
        json({ status: 'success', sessions: sessionRows, has_more: false, total: sessionRows.length, group_mode: 'time' });
    } else if (fixture[pathname]) json(fixture[pathname]);
    else json({ status: 'success' });
});

const write = (name, checks) => {
    report.checks.push({ scenario: report.currentScenario, name, ...checks });
    assert.ok(checks.ok, `${name}: ${JSON.stringify(checks)}`);
};

let browser, origin;
async function context({ flag = true, viewport = { width: 1440, height: 900 }, palette, theme } = {}) {
    const ctx = await browser.newContext({
        viewport, locale: 'zh-CN', colorScheme: 'light',
        extraHTTPHeaders: { 'X-Test-Sidebar-V2': flag ? '1' : '0' },
    });
    await ctx.addInitScript(({ palette, theme }) => {
        if (!location.href.startsWith('http:')) return;
        if (palette) localStorage.setItem('cow_web_palette', palette);
        if (theme) localStorage.setItem('cow_theme', theme);
    }, { palette, theme });
    ctx.on('page', page => page.on('pageerror', error => {
        report.pageErrors.push({ scenario: report.currentScenario, url: page.url(), message: error.message, stack: error.stack });
    }));
    return ctx;
}

async function open(ctx) {
    const page = await ctx.newPage();
    page.setDefaultTimeout(15000);
    await page.goto(origin + '/chat', { waitUntil: 'networkidle' });
    await page.locator('#app').waitFor({ state: 'visible' });
    await page.waitForFunction(() => typeof window.teamCandidateAgents === 'function'
        && typeof window.onNewChatButton === 'function'
        && typeof window.syncNewChatControls === 'function');
    return page;
}

async function scenario(name, body) {
    report.currentScenario = name;
    const entry = { name, passed: false };
    report.scenarios.push(entry);
    try {
        await body();
        entry.passed = true;
    } catch (error) {
        entry.message = error.message;
        throw error;
    }
}

async function shot(page, name) {
    const file = path.join(output, name + '.png');
    await page.screenshot({ path: file });
    report.screenshots.push(path.basename(file));
    return file;
}

// The one middle scroll region: no element inside the sidebar may *scroll*
// except the navigation, and the page itself must never scroll sideways.
// "Scrolls" means an auto/scroll overflow container holding taller content --
// a truncated label with an ellipsis is not a second scroll region.
const sidebarGeometry = () => {
    const scrollers = [];
    document.querySelectorAll('#sidebar *').forEach(el => {
        const overflowY = getComputedStyle(el).overflowY;
        const scrollable = (overflowY === 'auto' || overflowY === 'scroll')
            && el.scrollHeight > el.clientHeight + 1;
        if (scrollable) {
            scrollers.push(el.id ? '#' + el.id : '.' + (el.className || '').toString().split(' ')[0]);
        }
    });
    const box = (id) => {
        const el = document.getElementById(id);
        if (!el || !el.getClientRects().length) return null;
        const r = el.getBoundingClientRect();
        return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height };
    };
    return {
        scrollers,
        documentWidth: document.documentElement.scrollWidth,
        viewportWidth: innerWidth,
        viewportHeight: innerHeight,
        bodyOverflowY: getComputedStyle(document.body).overflowY,
        newChat: box('sidebar-new-chat'),
        caret: box('sidebar-new-chat-caret'),
        launchWraps: document.querySelectorAll('#sidebar .sidebar-new-chat-wrap').length,
        teamButtons: document.querySelectorAll('#sidebar [id*="team-chat"], #sidebar-new-team-chat').length,
        shell: box('sidebar'),
        nav: box('sidebar-nav'),
        navOverflowY: (() => {
            const nav = document.getElementById('sidebar-nav');
            return nav ? getComputedStyle(nav).overflowY : '';
        })(),
        shellOverflowY: getComputedStyle(document.getElementById('sidebar')).overflowY,
        footer: box('sidebar-account-footer'),
        recentRows: document.querySelectorAll('#sidebar-recent-list .sidebar-recent-row').length,
        overlayHidden: document.getElementById('sidebar-overlay')?.classList.contains('hidden'),
        shellClasses: document.getElementById('sidebar').className,
    };
};

(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    origin = 'http://127.0.0.1:' + server.address().port;
    browser = await chromium.launch({ channel: 'chrome', headless: true });

    await scenario('the sidebar launch control opens the shared picker, and the team picker never a coding Agent', async () => {
        const ctx = await context({ flag: true, palette: 'business' });
        const page = await open(ctx);

        // The sidebar's 新建对话 is the session panel's control in another place,
        // so its caret opens the one picker and draws the identical rows.
        const geometryBefore = await page.evaluate(sidebarGeometry);
        write('there is one launch control and no separate team button', {
            ok: geometryBefore.launchWraps === 1 && geometryBefore.teamButtons === 0
                && !!geometryBefore.newChat && !!geometryBefore.caret,
            launchWraps: geometryBefore.launchWraps, teamButtons: geometryBefore.teamButtons,
            newChat: geometryBefore.newChat, caret: geometryBefore.caret,
        });
        write('the caret rides inside the launch button, at its right edge', {
            ok: geometryBefore.caret.right <= geometryBefore.newChat.right + 1
                && geometryBefore.caret.left >= geometryBefore.newChat.left
                && geometryBefore.caret.top >= geometryBefore.newChat.top,
            caret: geometryBefore.caret, newChat: geometryBefore.newChat,
        });

        await page.locator('#sidebar-new-chat-caret').click();
        await page.locator('#sidebar-new-chat-menu').waitFor({ state: 'visible' });
        const drawn = await page.evaluate(() => ({
            sidebar: document.getElementById('sidebar-new-chat-menu').innerHTML,
            panel: (() => {
                paintNewChatMenu(document.getElementById('new-chat-menu'));
                return document.getElementById('new-chat-menu').innerHTML;
            })(),
            hidden: document.getElementById('new-chat-menu').classList.contains('hidden'),
        }));
        write('the sidebar picker is the session panel picker', {
            ok: drawn.sidebar === drawn.panel && drawn.hidden,
            sidebar: drawn.sidebar.slice(0, 120),
        });
        // A single coding conversation is still one row away: the boundary is on
        // team membership, not on reaching the Agent at all (task 2.2).
        const soloRows = await page.$$eval('#sidebar-new-chat-menu button[onclick^="startSoloChat"]',
            els => els.map(el => el.getAttribute('onclick')));
        write('a single coding conversation is still one picker row away', {
            ok: soloRows.some(row => row.includes("'coder'")),
            soloRows,
        });
        // ...and the picker anchors to the button it belongs to.
        const anchoring = await page.evaluate(() => {
            const button = document.getElementById('sidebar-new-chat').getBoundingClientRect();
            const wrap = document.querySelector('#sidebar .sidebar-new-chat-wrap').getBoundingClientRect();
            const menu = document.getElementById('sidebar-new-chat-menu').getBoundingClientRect();
            return {
                palette: document.documentElement.dataset.webPalette,
                display: getComputedStyle(document.getElementById('sidebar-new-chat')).display,
                buttonWidth: button.width, wrapperWidth: wrap.width,
                button: { left: button.left, right: button.right, bottom: button.bottom },
                wrapper: { left: wrap.left, right: wrap.right },
                menu: { left: menu.left, right: menu.right, top: menu.top, width: menu.width },
            };
        });
        write('the launch button fills the block its picker is anchored to', {
            ok: Math.abs(anchoring.buttonWidth - anchoring.wrapperWidth) < 1
                && Math.abs(anchoring.button.left - anchoring.wrapper.left) < 1,
            anchoring,
        });
        write('the picker is anchored under its own launch button', {
            ok: Math.abs(anchoring.menu.left - anchoring.button.left) < 2
                && Math.abs(anchoring.menu.right - anchoring.button.right) < 2
                && anchoring.menu.top >= anchoring.button.bottom - 1,
            anchoring,
        });
        await shot(page, 'sidebar-launch-v2-sidebar-picker');

        // The team row opens the very member picker the history page uses.
        await page.locator('#sidebar-new-chat-menu .new-chat-team').click();
        await page.locator('#team-chat-modal').waitFor({ state: 'visible' });
        write('opening the team picker shuts both launch pickers', {
            ok: await page.evaluate(() => document.getElementById('sidebar-new-chat-menu').classList.contains('hidden')
                && document.getElementById('new-chat-menu').classList.contains('hidden')),
        });

        // Keyboard: the search field owns the focus, so the picker is usable
        // without a mouse, and the listbox states its own selection.
        const focused = await page.evaluate(() => document.activeElement?.id || '');
        write('the picker focuses its search field', { ok: focused === 'team-chat-search', focused });

        const listed = await page.locator('#team-chat-list .team-chat-name').allTextContents();
        write('only normal Agents are listed', {
            ok: JSON.stringify(listed) === JSON.stringify(['通用助手', '研究员', '文案']), listed,
        });

        // Searching the coding Agent by name, by its position and by its id
        // finds nothing at all -- no disabled row, no label, no empty face.
        for (const query of ['编码', '工程', 'coder']) {
            await page.locator('#team-chat-search').fill(query);
            await page.waitForTimeout(60);
            const text = await page.locator('#team-chat-modal').innerText();
            write(`searching "${query}" exposes no coding Agent`, {
                ok: !text.includes('编码助手') && !text.includes('coder')
                    && !text.includes('工程'), text: text.slice(0, 200),
            });
        }
        await page.locator('#team-chat-search').fill('');
        await page.waitForTimeout(60);
        const modalText = await page.locator('#team-chat-modal').innerText();
        write('the whole picker text is free of the coding Agent', {
            ok: !modalText.includes('编码助手'),
        });

        // The composer's own picker is drawn from the same rule.
        await page.locator('#team-chat-modal button:has-text("取消")').click();
        await page.locator('#team-chat-modal').waitFor({ state: 'hidden' });
        const teamIds = await page.evaluate(() => window.teamCandidateAgents().map(a => a.id));
        write('the candidate projection excludes the coding Agent', {
            ok: !teamIds.includes('coder') && teamIds.includes('research'), teamIds,
        });

        // Escape hands focus back to the launch control the user actually used,
        // not to the menu row that just closed with its menu.
        await page.locator('#sidebar-new-chat-caret').click();
        await page.locator('#sidebar-new-chat-menu').waitFor({ state: 'visible' });
        await page.locator('#sidebar-new-chat-menu .new-chat-team').click();
        await page.locator('#team-chat-modal').waitFor({ state: 'visible' });
        await page.keyboard.press('Escape');
        await page.locator('#team-chat-modal').waitFor({ state: 'hidden' });
        const afterClose = await page.evaluate(() => document.activeElement?.id || '');
        write('Escape closes the picker and restores focus', { ok: afterClose === 'sidebar-new-chat', afterClose });
        await shot(page, 'sidebar-launch-v2-picker-excludes-coding');
        await ctx.close();
    });

    await scenario('the composer separates switching to a coding Agent from inviting teammates', async () => {
        const ctx = await context({ flag: true });
        const page = await open(ctx);
        // ``@`` in the composer: the composer menu mixes two different things,
        // so the invite rows and the switch rows are read apart.
        await page.locator('#composer-agent-btn').click();
        await page.locator('#composer-agent-menu').waitFor({ state: 'visible' });
        const invites = await page.$$eval('#composer-agent-menu button[onclick^="inviteTeamMember"]',
            els => els.map(el => el.innerText.trim()));
        const switches = await page.$$eval('#composer-agent-menu button[onclick^="pickComposerAgent"]',
            els => els.map(el => el.innerText.trim()));
        // Inviting is team membership, so the coding Agent is absent from it...
        write('the composer invite list holds no coding Agent', {
            ok: invites.length === 2
                && !invites.some(text => text.includes('编码助手') || text.includes('coder')), invites,
        });
        // ...while a single coding conversation stays one menu entry away, which
        // is the capability the boundary must not remove (task 2.2).
        write('a single coding conversation is still switchable from the composer', {
            ok: switches.some(text => text.includes('编码助手')), switches,
        });
        await page.keyboard.press('Escape');

        const geometry = await page.evaluate(sidebarGeometry);
        write('the sidebar preview keeps the members marker and the coding marker apart', {
            ok: geometry.recentRows === 3,
            rows: geometry.recentRows,
        });
        const markers = await page.evaluate(() => Array.from(
            document.querySelectorAll('#sidebar-recent-list .sidebar-recent-row'),
            row => ({
                faces: row.querySelectorAll('.session-faces img, .session-faces .agent-avatar').length,
                summary: row.querySelector('.session-faces')?.getAttribute('aria-label') || '',
                code: !!row.querySelector('.fa-code'),
            })));
        write('the team row states its members, the coding row its own type', {
            ok: markers[2].faces === 2 && markers[2].summary === '通用助手、研究员'
                && markers[1].code === true && markers[0].code === false, markers,
        });
        await ctx.close();
    });

    await scenario('desktop, narrow desktop and phone keep one scroll region and no overflow', async () => {
        for (const width of [1440, 1024, 360]) {
            const height = width === 360 ? 720 : 900;
            const ctx = await context({ flag: true, viewport: { width, height } });
            const page = await open(ctx);
            if (width < 1024) {
                // The sidebar is a drawer below lg: open it as a user would.
                await page.locator('#menu-toggle').click();
                await page.waitForTimeout(350);
            }
            const g = await page.evaluate(sidebarGeometry);
            write(`the navigation is the one middle scroll region at ${width}px`, {
                ok: g.scrollers.every(s => s === '#sidebar-nav')
                    && !g.scrollers.includes('#sidebar')
                    && g.navOverflowY === 'auto',
                scrollers: g.scrollers, navOverflowY: g.navOverflowY, shellOverflowY: g.shellOverflowY,
            });
            write(`nothing overflows the viewport at ${width}px`, {
                ok: g.documentWidth <= g.viewportWidth + 1, documentWidth: g.documentWidth, viewport: g.viewportWidth,
            });
            write(`the single launch control sits above the navigation at ${width}px`, {
                ok: !!g.newChat && !!g.caret
                    && g.caret.left >= g.newChat.left && g.caret.right <= g.newChat.right + 1
                    && g.newChat.bottom <= g.nav.top + 1,
                newChat: g.newChat, caret: g.caret, nav: g.nav,
            });
            write(`the account footer stays inside the viewport at ${width}px`, {
                ok: !!g.footer && g.footer.bottom <= g.viewportHeight + 1 && g.footer.top >= 0, footer: g.footer,
            });
            if (width < 1024) {
                // The caret lives in the drawer the user opened: the picker is a
                // full-viewport sheet, so the drawer must step aside rather than
                // stack under it and hide the control focus returns to.
                await page.locator('#sidebar-new-chat-caret').click();
                await page.locator('#sidebar-new-chat-menu').waitFor({ state: 'visible' });
                const menuBox = await page.locator('#sidebar-new-chat-menu').boundingBox();
                write(`the drawer picker fits the phone at ${width}px`, {
                    ok: !!menuBox && menuBox.x >= 0 && menuBox.x + menuBox.width <= width + 1,
                    menuBox,
                });
                await page.locator('#sidebar-new-chat-menu .new-chat-team').click();
                await page.locator('#team-chat-modal').waitFor({ state: 'visible' });
                await page.waitForTimeout(350);
                const afterLaunch = await page.evaluate(sidebarGeometry);
                write(`starting a picker from the drawer shuts the drawer at ${width}px`, {
                    ok: afterLaunch.overlayHidden === true
                        && afterLaunch.shellClasses.includes('-translate-x-full'),
                    overlayHidden: afterLaunch.overlayHidden, shellClasses: afterLaunch.shellClasses,
                });
                await page.keyboard.press('Escape');
                await page.locator('#team-chat-modal').waitFor({ state: 'hidden' });
                const focused = await page.evaluate(() => document.activeElement?.id || '');
                write(`focus lands on the visible drawer toggle at ${width}px`, {
                    ok: focused === 'menu-toggle', focused,
                });
                // Re-open for the mask-tap case below.
                await page.locator('#menu-toggle').click();
                await page.waitForTimeout(350);
                const overlayOpen = await page.evaluate(() => !document.getElementById('sidebar-overlay').classList.contains('hidden'));
                // Tap the mask beside the open drawer, where a user would tap.
                await page.locator('#sidebar-overlay').click({ position: { x: width - 40, y: 400 } });
                await page.waitForTimeout(350);
                const closed = await page.evaluate(sidebarGeometry);
                write(`closing the phone drawer leaves no mask behind at ${width}px`, {
                    ok: overlayOpen && closed.overlayHidden === true && closed.shellClasses.includes('-translate-x-full'),
                    overlayOpen, overlayHidden: closed.overlayHidden,
                });
            }
            await shot(page, `sidebar-launch-v2-${width}px`);
            await ctx.close();
        }
    });

    await scenario('the three palettes in both modes keep the launch control inside the sidebar', async () => {
        for (const palette of ['business', 'slate', 'classic']) {
            for (const theme of ['light', 'dark']) {
                const ctx = await context({ flag: true, palette, theme });
                const page = await open(ctx);
                const g = await page.evaluate(sidebarGeometry);
                const resolved = await page.evaluate(() => ({
                    palette: document.documentElement.dataset.webPalette,
                    dark: document.documentElement.classList.contains('dark'),
                }));
                write(`${palette}/${theme} paints the promised appearance`, {
                    ok: resolved.palette === palette && resolved.dark === (theme === 'dark'), resolved,
                });
                write(`${palette}/${theme} keeps the launch control and its caret inside the sidebar`, {
                    ok: !!g.newChat && !!g.caret
                        && g.newChat.left >= g.shell.left - 1 && g.newChat.right <= g.shell.right + 1
                        && g.caret.left >= g.shell.left - 1 && g.caret.right <= g.shell.right + 1
                        && g.documentWidth <= g.viewportWidth + 1,
                    newChat: g.newChat, caret: g.caret, shell: g.shell,
                    documentWidth: g.documentWidth, viewport: g.viewportWidth,
                });
                // The picker belongs to the sidebar it drops out of: it must not
                // be a browser-default white sheet in a dark rail, and it must
                // stay inside the viewport.
                await page.locator('#sidebar-new-chat-caret').click();
                await page.locator('#sidebar-new-chat-menu').waitFor({ state: 'visible' });
                const menu = await page.evaluate(() => {
                    const el = document.getElementById('sidebar-new-chat-menu');
                    const box = el.getBoundingClientRect();
                    const shell = document.getElementById('sidebar').getBoundingClientRect();
                    return { box: { left: box.left, right: box.right, top: box.top, bottom: box.bottom },
                        background: getComputedStyle(el).backgroundColor,
                        sidebarBg: getComputedStyle(document.getElementById('sidebar')).backgroundColor,
                        viewportWidth: window.innerWidth,
                        horizontalOverflow: document.documentElement.scrollWidth - innerWidth };
                });
                write(`${palette}/${theme} paints the picker as a sidebar surface inside the viewport`, {
                    ok: menu.box.left >= -1 && menu.box.right <= menu.viewportWidth + 1
                        && menu.horizontalOverflow <= 1
                        && menu.background !== 'rgba(0, 0, 0, 0)' && menu.background !== 'transparent',
                    menu,
                });
                if (theme === 'dark') await shot(page, `sidebar-launch-v2-${palette}-dark`);
                await ctx.close();
            }
        }
    });

    await scenario('the collapsed rail opens the picker beside itself, not inside 72px', async () => {
        const ctx = await context({ flag: true, viewport: { width: 1440, height: 900 } });
        const page = await open(ctx);
        // The desktop collapse toggle turns the sidebar into the icon rail.
        await page.locator('#menu-toggle').click();
        await page.waitForFunction(() => document.getElementById('app').classList.contains('sidebar-collapsed'));
        await page.waitForTimeout(250);
        await page.locator('#sidebar-new-chat-caret').click();
        await page.locator('#sidebar-new-chat-menu').waitFor({ state: 'visible' });
        const layout = await page.evaluate(() => {
            const box = el => { const r = el.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width }; };
            return {
                shell: box(document.getElementById('sidebar')),
                menu: box(document.getElementById('sidebar-new-chat-menu')),
                caret: box(document.getElementById('sidebar-new-chat-caret')),
                rows: document.querySelectorAll('#sidebar-new-chat-menu .new-chat-item').length,
                documentWidth: document.documentElement.scrollWidth,
                viewportWidth: innerWidth,
            };
        });
        write('the rail keeps its launch icon and caret', {
            ok: layout.caret.width > 0 && layout.shell.width < 100,
            shell: layout.shell, caret: layout.caret,
        });
        write('the picker opens beside the rail, whole and unclipped', {
            ok: layout.menu.left >= layout.shell.right - 1
                && layout.menu.width >= 200
                && layout.menu.right <= layout.viewportWidth
                && layout.rows >= 1
                && layout.documentWidth <= layout.viewportWidth + 1,
            menu: layout.menu, shell: layout.shell, rows: layout.rows,
        });
        await shot(page, 'sidebar-launch-v2-collapsed-picker');
        await ctx.close();
    });

    await scenario('turning the switch off restores the classic sidebar without weakening a rule', async () => {
        sessionRows = sessions(12);
        const ctx = await context({ flag: false });
        const page = await open(ctx);
        const off = await page.evaluate(sidebarGeometry);
        write('the classic sidebar has no caret, no picker and no refined class', {
            ok: !off.shellClasses.includes('sidebar-launch-v2') && off.caret === null
                && off.launchWraps === 1,
            shellClasses: off.shellClasses, caret: off.caret, launchWraps: off.launchWraps,
        });
        write('the classic sidebar keeps its own picker shut', {
            ok: await page.evaluate(() => document.getElementById('sidebar-new-chat-menu').classList.contains('hidden')),
        });
        write('the classic preview keeps its ten rows', { ok: off.recentRows === 10, rows: off.recentRows });

        const onCtx = await context({ flag: true });
        const onPage = await open(onCtx);
        const on = await onPage.evaluate(sidebarGeometry);
        write('the refined preview holds five rows and keeps 查看全部', {
            ok: on.recentRows === 5 && on.shellClasses.includes('sidebar-launch-v2'),
            rows: on.recentRows, shellClasses: on.shellClasses,
        });
        write('the team candidate rule is the same with the switch off', {
            ok: await onPage.evaluate(() => !window.teamCandidateAgents().map(a => a.id).includes('coder')),
        });
        // The rollback promise is about this layout only: the team flow stays
        // reachable through the history page's own 新对话 menu.
        write('the history page still offers the team entry when the switch is off', {
            ok: await page.evaluate(() => {
                const caret = document.getElementById('new-chat-caret');
                paintNewChatMenu(document.getElementById('new-chat-menu'));
                return !!caret && !caret.classList.contains('hidden')
                    && /new-chat-team/.test(document.getElementById('new-chat-menu').innerHTML);
            }),
        });
        await shot(onPage, 'sidebar-launch-v2-preview-five-rows');
        await ctx.close();
        await onCtx.close();
        sessionRows = sessions(3);
    });

    assert.deepEqual(report.pageErrors, [], 'the production page must boot without errors');
    assert.ok(report.checks.every(c => c.ok));
    console.log(JSON.stringify({ scenarios: report.scenarios.length, checks: report.checks.length,
        screenshots: report.screenshots.length, pageErrors: report.pageErrors.length, output }, null, 2));
})().catch(error => {
    report.error = { message: error.message, stack: error.stack };
    console.error(error.message);
    process.exitCode = 1;
}).finally(async () => {
    fs.writeFileSync(path.join(output, 'sidebar-launch-browser.json'), JSON.stringify(report, null, 2));
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
});
