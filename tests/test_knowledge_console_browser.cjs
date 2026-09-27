// The knowledge page in a real browser (change add-traceable-knowledge-ingestion,
// task 3.4: "保存指定搜索位置和资料闭环的界面证据").
//
// The DOM unit tests next door drive one module with a hand-built document. What
// they cannot prove is that the *production* page — the shell, the real i18n
// table, the real CSS and the knowledge module running together — puts the two
// surfaces where this change says they are:
//
//   * the Agent menu's search box is a filter on an existing menu, it does not
//     move the library the page is viewing, and filtering there leaves the
//     document search alone;
//   * the 原始资料 tab is a second, self-contained panel: list → detail → upload
//     panel, with "saved" clearly separated from "searchable".
//
// Requires Playwright with Chromium or a Chrome channel; no request reaches a
// live service. The artifact is written even when a scenario fails, so a human
// can read what the page actually rendered:
//   NODE_PATH=$(npm root -g) node tests/test_knowledge_console_browser.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');

let chromium;
try {
    ({ chromium } = require('playwright'));
} catch (error) {
    // Without Playwright the file still loads, so a wrapper can skip it.
    console.log('SKIP knowledge console browser contract: playwright is not installed');
    process.exit(0);
}

const repo = path.resolve(__dirname, '..');
const staticRoot = path.join(repo, 'channel/web/static');
const output = process.env.COW_KNOWLEDGE_BROWSER_OUTPUT
    || fs.mkdtempSync(path.join(os.tmpdir(), 'cow-knowledge-browser-'));
const report = { artifact: output, scenarios: [], pageErrors: [], unexpectedRoutes: [], requests: [] };
fs.mkdirSync(output, { recursive: true });

const TENANT = { id: 'fixture-tenant', name: '测试租户', code: 'fixture' };
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
    '.png': 'image/png', '.jpg': 'image/jpeg', '.woff2': 'font/woff2', '.woff': 'font/woff',
    '.ttf': 'font/ttf' };

// Two libraries that differ in an obvious, case-insensitive, CJK-able way, so a
// query can prove it filtered the *visible* names without switching the library.
const AGENTS = [
    { id: 'agent-a', name: '客服助手', enabled: true, can_write_knowledge: true },
    { id: 'agent-b', name: '法务知识库', enabled: true, can_write_knowledge: true },
    { id: 'agent-c', name: 'Finance Bot', enabled: true, can_write_knowledge: true },
];

const KNOWLEDGE_LIST = {
    status: 'success',
    tree: [],
    root_files: [{ name: 'MEMORY.md', title: 'MEMORY.md' }],
    stats: { pages: 1, size: 1024 },
};

// src-1 is saved *and* searchable; src-2 is stored for audit only. The list has
// to say both without the second borrowing the first's status.
const SOURCES = {
    status: 'success',
    registered: true,
    sources: [
        { source_id: 'src-1', name: '合同.pdf', category: 'legal', lifecycle: 'active',
          latest_version: 2, active_version: 1, active_task_id: 'task-9',
          target_task_id: 'task-9', converted: true, searchable: true, size: 2048,
          created_at: '2026-09-24T03:00:00Z', updated_at: '2026-09-26T02:00:00Z',
          latest_task: { task_id: 'task-9', task_type: 'convert', status: 'complete',
              stage: 'indexed', error: null } },
        { source_id: 'src-2', name: '报价.xlsx', category: '',
          lifecycle: 'active', latest_version: 1, active_version: 0,
          active_task_id: null, target_task_id: null, converted: false,
          searchable: false, size: 512, latest_task: null },
    ],
    capabilities: {
        knowledge_enabled: true, can_write: true,
        source_upload: { configured: true, dependency_ready: true, available: true, reason: '' },
        conversion: { configured: true, dependency_ready: true, available: true, reason: '' },
    },
    limits: { max_files: 100, max_file_size: 10485760, max_batch_size: 209715200, storage_quota: 0 },
};

const SOURCE_DETAIL = {
    status: 'success',
    source: SOURCES.sources[1],
    versions: [{ version: 1, original_name: '报价.xlsx', size: 512, ext: '.xlsx',
        content_hash: 'a'.repeat(64), commit_state: 'committed',
        created_at: '2026-09-26T01:00:00Z' }],
    tasks: [],
    limits: SOURCES.limits,
};

// The console shell is assembled on the server: `<!--#include path-->` pulls in
// the view and modal fragments, then the `{{COW_*}}` placeholders are injected.
// Serving the raw chat.html would drop the task modal markup and make an
// unrelated lazy view throw, so the fixture assembles the page the same way.
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

const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://fixture');
    const pathname = url.pathname;
    if (!pathname.startsWith('/assets/')) {
        report.requests.push({ pathname, query: url.search, method: req.method });
    }
    const json = (data, status = 200) => {
        res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8',
            'Cache-Control': 'no-store' });
        res.end(JSON.stringify(data));
    };
    const identity = { status: 'success', identity_mode: 'database', auth_required: true,
        authenticated: true, user: { username: 'knowledge-member', display_name: '知识成员',
            roles: ['member'], is_admin: false } };

    if (pathname === '/chat' || pathname === '/' || pathname === '/admin') {
        res.writeHead(200, { 'Content-Type': mime['.html'], 'Cache-Control': 'no-store' });
        res.end(assembleShell());
    } else if (pathname.startsWith('/assets/')) {
        const file = path.resolve(staticRoot, '.' + pathname.slice('/assets'.length));
        if (!file.startsWith(staticRoot + path.sep) || !fs.existsSync(file)
            || !fs.statSync(file).isFile()) {
            report.unexpectedRoutes.push({ pathname, method: req.method });
            json({ status: 'error', message: 'Missing fixture asset' }, 404);
        } else {
            res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream' });
            fs.createReadStream(file).pipe(res);
        }
    } else if (pathname === '/auth/check') json(identity);
    else if (pathname === '/auth/login') json({ ...identity, tenants: [TENANT] });
    else if (pathname === '/auth/logout') json({ status: 'success' });
    else if (pathname === '/auth/me') json({ ...identity, tenants: [TENANT], current_tenant: TENANT });
    else if (pathname === '/auth/context') json({ status: 'success',
        authorization_mode: 'role', is_tenant_admin: false, is_platform_admin: false,
        tenant: TENANT,
        console_pages: {
            'workbench.chat': { available: true, read_allowed: true, scope: 'self' },
            'workbench.history': { available: true, read_allowed: true, scope: 'self' },
            'workbench.knowledge': { available: true, read_allowed: true, scope: 'self' },
        } });
    else if (pathname === '/api/version') json({ status: 'success', version: 'knowledge-fixture' });
    else if (pathname === '/api/branding/public') {
        json({ enabled: false, revision: 1, logo_url: '', favicon_url: '' });
    } else if (pathname === '/config') json({ status: 'success', title: '容大AI',
        model: 'fixture-model', providers: {}, agent_permission_mode: 'workspace-write',
        permission_modes: ['read-only', 'workspace-write', 'full-access'] });
    else if (pathname === '/api/agents') json({ status: 'success', default_agent_id: 'agent-a',
        revision: 'fixture', agents: AGENTS.map(a => ({ ...a, is_default: a.id === 'agent-a' })),
        channel_instances: [] });
    else if (pathname === '/api/knowledge/list') json(KNOWLEDGE_LIST);
    else if (pathname === '/api/knowledge/sources') json(SOURCES);
    else if (pathname === '/api/knowledge/sources/detail') json(SOURCE_DETAIL);
    else if (pathname === '/api/knowledge/graph') json({ status: 'success', nodes: [], links: [] });
    else if (['/api/sessions', '/api/history', '/api/projects', '/api/models', '/api/tools',
              '/api/skills'].includes(pathname)) {
        json({ status: 'success', sessions: [], has_more: false, total: 0, messages: [],
            recents: [], tree: [], root_files: [], providers: [], items: [] });
    } else if (pathname === '/poll') json({ status: 'success', has_content: false });
    else if (pathname.startsWith('/api/')) json({ status: 'success', items: [] });
    else {
        report.unexpectedRoutes.push({ pathname, method: req.method });
        json({ status: 'error', message: 'Unconfigured fixture route' }, 404);
    }
});

let browser, origin;

// Prefer the bundled browser; fall back to the system Chrome channel, which is
// what a developer machine without `playwright install` has.
async function launchBrowser() {
    try {
        return await chromium.launch();
    } catch (error) {
        return await chromium.launch({ channel: 'chrome' });
    }
}

async function open(options = {}) {
    const context = await browser.newContext({
        viewport: options.viewport || { width: 1440, height: 900 },
        locale: 'zh-CN',
        colorScheme: options.colorScheme || 'light',
        reducedMotion: options.reducedMotion || 'no-preference',
    });
    context.on('page', page => page.on('pageerror', error => {
        report.pageErrors.push({ scenario: report.currentScenario, message: error.message,
            stack: error.stack });
    }));
    await context.addInitScript((lang) => {
        if (!location.href.startsWith('http:')) return;
        sessionStorage.setItem('cow_tenant_id', 'fixture-tenant');
        localStorage.setItem('cow_lang', lang);
    }, options.lang || 'zh');
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    // The knowledge view belongs to the workbench area (`workbench.knowledge`),
    // so it is reached from `/chat`; `/admin` would keep it hidden.
    await page.goto(origin + (options.path || '/chat') + (options.hash || '#view-knowledge'),
        { waitUntil: 'networkidle' });
    await page.locator('#app').waitFor({ state: 'visible' });
    await page.locator('#view-knowledge').waitFor({ state: 'visible' });
    return page;
}

async function scenario(name, run, options) {
    report.currentScenario = name;
    report.scenarioBase = report.requests.length;
    const start = Date.now();
    let page = null;
    try {
        page = await open(options || {});
        await run(page);
        report.scenarios.push({ name, passed: true, durationMs: Date.now() - start });
    } catch (error) {
        report.scenarios.push({ name, passed: false, message: error.message, stack: error.stack });
        throw error;
    } finally {
        if (page) await page.context().close().catch(() => {});
        fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(report, null, 2));
    }
}

function requestsTo(pathname) {
    return report.requests.slice(report.scenarioBase || 0)
        .filter(entry => entry.pathname === pathname);
}

/** The knowledge Agent menu, opened, with its search box clicked like a user. */
async function openAgentMenu(page) {
    await page.locator('#knowledge-agent-select .cfg-dropdown-selected').click();
    await page.locator('#knowledge-agent-select.open').waitFor({ state: 'visible' });
    const input = page.locator('#knowledge-agent-select .cfg-dropdown-search-input');
    await input.waitFor({ state: 'visible' });
    // Clicking to place the caret is the first thing anyone does with a search
    // box, and it is the one interaction Playwright's `fill` skips: fill focuses
    // the field without a click, so the bubbled click that used to close the
    // menu was never exercised and the box could vanish for real users only.
    await input.click();
    assert.equal(await page.locator('#knowledge-agent-select.open').count(), 1,
        'clicking into the box leaves the menu -- and the box -- open');
    return input;
}

async function main() {
    browser = await launchBrowser();
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    origin = 'http://127.0.0.1:' + server.address().port;

    await scenario('desktop: the Agent search filters its own menu, not the document search', async page => {
        // The search box belongs to the Agent menu (task 3.1): typing narrows the
        // rows below it, the closed trigger keeps showing the viewed library, and
        // the document search box on the docs panel is not touched.
        const input = await openAgentMenu(page);
        await input.fill('法务');
        await page.locator('#knowledge-agent-select .cfg-dropdown-item').first().waitFor();
        // The row's visible label only; the leading avatar glyph is not a name.
        const rows = await page.locator('#knowledge-agent-select .cfg-dropdown-label')
            .allTextContents();
        assert.deepEqual(rows, ['法务知识库'], 'only the matching library stays in the menu');
        assert.equal(await page.locator('#knowledge-agent-select .cfg-dropdown-empty.hidden').count(), 1,
            'a match means the empty note stays hidden');

        // No library switch: the page still addresses its original Agent.
        assert.equal(await requestsTo('/api/knowledge/list').length >= 1, true);
        const before = requestsTo('/api/knowledge/list').length;
        assert.equal(await page.locator('#knowledge-search').inputValue(), '',
            'the document search box is a different control and stays empty');
        assert.equal(requestsTo('/api/knowledge/list').length, before,
            'filtering the menu starts no knowledge read');

        // Clearing brings every library back and hides the empty note.
        await input.fill('');
        const all = await page.locator('#knowledge-agent-select .cfg-dropdown-label')
            .allTextContents();
        assert.deepEqual(all.sort(), ['Finance Bot', '客服助手', '法务知识库'].sort(),
            'clearing restores every visible option');
        assert.equal(await page.locator('#knowledge-agent-select .cfg-dropdown-empty.hidden').count(), 1,
            'and hides the empty note again');
    });

    await scenario('desktop, dark: a CJK query with no match says so instead of an empty menu',
        async page => {
            const input = await openAgentMenu(page);
            await input.fill('不存在的库');
            await page.locator('#knowledge-agent-select .cfg-dropdown-empty').waitFor({ state: 'visible' });
            assert.equal(await page.locator('#knowledge-agent-select .cfg-dropdown-item').count(), 0,
                'no row survives the query');
            assert.equal(await page.locator('#knowledge-agent-select .cfg-dropdown-empty.hidden').count(), 0,
                'the empty note is the answer, not a blank menu');
        }, { colorScheme: 'dark' });

    await scenario('desktop: the sources tab is a closed loop of list, detail and upload', async page => {
        // Task 3.2/3.3: the tab loads its own panel, a row reports saved vs
        // searchable, the detail carries versions and lifecycle, and the upload
        // panel opens against the deployment's own limits.
        await page.locator('#knowledge-tab-sources').click();
        await page.locator('#knowledge-panel-sources').waitFor({ state: 'visible' });
        await page.locator('.knowledge-source-row').first().waitFor();

        const rows = page.locator('.knowledge-source-row');
        assert.equal(await rows.count(), 2, 'both stored originals are listed');
        assert.equal(await rows.nth(0).getAttribute('data-status'), 'searchable',
            'the converted source is the searchable one');
        assert.equal(await rows.nth(1).getAttribute('data-status'), 'saved',
            'the stored-only source does not borrow that status');
        assert.equal(await rows.nth(0).getAttribute('data-active-version'), '1');
        assert.equal(await rows.nth(0).getAttribute('data-latest-version'), '2');

        // The header button is a real control when the deployment can store bytes.
        assert.equal(await page.locator('#knowledge-upload-btn').isVisible(), true,
            'upload is offered when source_upload.available is true');

        // The detail is the second half of the loop: pick the stored-only source
        // and it must say there is no searchable body yet.
        await rows.nth(1).click();
        await page.locator('#knowledge-source-detail').waitFor({ state: 'visible' });
        await page.locator('#knowledge-source-detail-body').waitFor();
        const detail = await page.locator('#knowledge-source-detail-body').innerText();
        assert.match(detail, /报价\.xlsx/, 'the detail names the picked original');
        assert.match(detail, /还没有可检索正文/, 'and states that nothing is searchable yet');

        // The upload panel opens from the header and shows the source limits.
        await page.locator('#knowledge-upload-btn').click();
        await page.locator('#knowledge-upload-panel').waitFor({ state: 'visible' });
        const panel = await page.locator('#knowledge-upload-panel').innerText();
        assert.match(panel, /10(\.0)?\s*MB|10 MiB/, 'the panel states the per-file ceiling');
        await page.screenshot({ path: path.join(output, 'sources-upload-panel.png') }).catch(() => {});
    });

    await scenario('narrow: the sources loop still works on a phone-width viewport', async page => {
        await page.locator('#knowledge-tab-sources').click();
        await page.locator('.knowledge-source-row').first().waitFor();
        assert.equal(await page.locator('.knowledge-source-row').count(), 2);
        await page.locator('.knowledge-source-row').nth(0).click();
        await page.locator('#knowledge-source-detail').waitFor({ state: 'visible' });
        await page.screenshot({ path: path.join(output, 'sources-narrow.png') }).catch(() => {});
    }, { viewport: { width: 390, height: 844 } });

    const failed = report.scenarios.filter(entry => !entry.passed);
    if (report.pageErrors.length) {
        throw new Error('page errors: ' + JSON.stringify(report.pageErrors, null, 2));
    }
    if (report.unexpectedRoutes.length) {
        throw new Error('unexpected routes: ' + JSON.stringify(report.unexpectedRoutes, null, 2));
    }
    console.log('knowledge console browser: ' + report.scenarios.length + ' scenarios, '
        + failed.length + ' failed');
    console.log('artifact: ' + path.join(output, 'results.json'));
}

main()
    .catch(error => { console.error(error); process.exitCode = 1; })
    .finally(() => { if (browser) return browser.close(); })
    .finally(() => { server.close(); });
