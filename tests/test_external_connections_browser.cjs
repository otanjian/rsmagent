// The 系统接入 drawer's conditional field groups, in a real browser.
//
// ``test_external_connections_frontend.cjs`` drives the module with a hand-built
// document whose ``addEventListener`` only records the handler: it can prove the
// form *renders* the 请求头名称 input and the 认证请求头值 credential input, but not
// that choosing 「请求头认证」 makes them appear. That reveal is a `change` event
// which has to travel from the drawer to whatever element carries the listener —
// and the drawer is a child of `document.body`, not of the page root. When the
// listener sat on the root alone the event never arrived, the two fields stayed
// hidden and there was nowhere to type the MCP token.
//
// Requires Playwright with Chromium or a Chrome channel; no request reaches a
// live service:
//   NODE_PATH=$(npm root -g) node tests/test_external_connections_browser.cjs
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
    console.log('SKIP external connections browser contract: playwright is not installed');
    process.exit(0);
}

const repo = path.resolve(__dirname, '..');
const staticRoot = path.join(repo, 'channel/web/static');
const output = process.env.COW_EXTERNAL_BROWSER_OUTPUT
    || fs.mkdtempSync(path.join(os.tmpdir(), 'cow-external-browser-'));
const report = { artifact: output, scenarios: [], pageErrors: [], unexpectedRoutes: [],
    requests: [], writes: [] };
fs.mkdirSync(output, { recursive: true });

const TENANT = { id: 'fixture-tenant', name: '测试租户', code: 'fixture' };
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
    '.png': 'image/png', '.jpg': 'image/jpeg', '.woff2': 'font/woff2', '.woff': 'font/woff',
    '.ttf': 'font/ttf' };

// A tenant MCP connection is the one card the page already has. The tenant range
// is the only scope this identity may read, so the page reads exactly one
// catalogue and stays on the branch that matters.
const MCP_TYPE = {
    kind: 'mcp', label: 'MCP', label_key: 'ext_conn_type_mcp',
    config_keys: ['transport', 'url'],
    secret_slots: ['header', 'env', 'oauth'],
    scopes: [{ scope: 'tenant', available: true, reason: '' }],
    capabilities: {
        open: ['configure'], test_available: false, execute_available: false,
        unavailable_reason: 'awaiting_mcp_test_environment', missing_secret_slots: ['header'],
    },
};

const MCP_CARD = {
    id: 'mcp-1', effective_id: 'mcp-1', kind: 'mcp', scope: 'tenant', name: 'OneAgent HTTP MCP',
    enabled: true, source: 'tenant', version: 1, test_status: 'untested', tested_at: null,
    actions: ['read', 'manage'], base_connection_id: null,
    config: { transport: 'streamable_http', url: 'http://127.0.0.1:8080/mcp', auth: 'none' },
    agent_assignment: { configured: true, revision: 3, visible_count: 2, can_assign: true },
};

// -- Agent assignment fixture ------------------------------------------------
//
// The relation is *server* state, not a static response: a save has to move the
// page from one state to the next (a draft that vanishes into a canned reply
// would prove nothing about what the modal does with the answer), and the 409
// path needs a revision the server refuses once.
const AGENT_ROSTER = [
    { id: 'agent-1', name: '税法知识库助手', category: 'knowledge', enabled: true,
        visibility: 'tenant', manageable: true },
    { id: 'agent-2', name: 'SAP 对账助手', category: 'erp', enabled: true,
        visibility: 'tenant', manageable: true },
    { id: 'agent-3', name: '合同审阅助手', category: 'legal', enabled: true,
        visibility: 'tenant', manageable: true },
    { id: 'agent-4', name: '他人分享的助手', category: 'shared', enabled: true,
        visibility: 'tenant', manageable: false },
];

const assignment = {
    configured: true, revision: 3, canAssign: true,
    assigned: ['agent-1', 'agent-2'], conflictOnce: false, saves: [],
};

function resetAssignments(options) {
    const next = options || {};
    assignment.configured = next.configured === undefined ? true : !!next.configured;
    assignment.assigned = (next.assigned || ['agent-1', 'agent-2']).slice();
    assignment.revision = next.revision === undefined ? 3 : next.revision;
    assignment.canAssign = next.canAssign === undefined ? true : !!next.canAssign;
    assignment.conflictOnce = false;
    assignment.saves = [];
    MCP_CARD.agent_assignment = {
        configured: assignment.configured, revision: assignment.revision,
        visible_count: assignment.assigned.length, can_assign: assignment.canAssign,
    };
}

/** The last save body, minus the ambient `agent_id` the console shell copies
 *  onto every `/api` write (it names the page's active Agent, not a member of
 *  this relation). What is left is the whole delta contract. */
function lastSaveBody() {
    const body = Object.assign({}, assignment.saves[assignment.saves.length - 1]);
    delete body.agent_id;
    return body;
}

function agentById(id) {
    return AGENT_ROSTER.filter(item => item.id === id)[0] || null;
}

/** One row as the service's projection builds it (visible fields only). */
function agentRow(id, assigned) {
    const agent = agentById(id) || { id: id, name: id, enabled: true, manageable: true };
    return { id: agent.id, name: agent.name, category: agent.category,
        enabled: agent.enabled, visibility: agent.visibility,
        assigned: !!assigned, manageable: agent.manageable };
}

/** A page, sorted the way the server sorts: display name, then id. */
function agentPage(rows, url) {
    const page = Math.max(1, parseInt(url.searchParams.get('page'), 10) || 1);
    const size = Math.max(1, parseInt(url.searchParams.get('page_size'), 10) || 20);
    rows.sort((a, b) => (a.name.toLowerCase() < b.name.toLowerCase() ? -1
        : a.name.toLowerCase() > b.name.toLowerCase() ? 1 : (a.id < b.id ? -1 : 1)));
    const start = (page - 1) * size;
    return {
        status: 'success', items: rows.slice(start, start + size), total: rows.length,
        page: page, page_size: size, has_more: start + size < rows.length,
        configured: assignment.configured, revision: assignment.revision,
        can_assign: assignment.canAssign,
    };
}

// The console shell is assembled on the server: `<!--#include path-->` pulls in
// the view and modal fragments, then the `{{COW_*}}` placeholders are injected.
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

function readBody(req) {
    return new Promise(resolve => {
        let raw = '';
        req.on('data', chunk => { raw += chunk; });
        req.on('end', () => resolve(raw));
    });
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
        authenticated: true, user: { username: 'tenant-admin', display_name: '租户管理员',
            roles: ['admin'], is_admin: true, is_platform_admin: false } };

    const handle = async () => {
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
                res.writeHead(200,
                    { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream' });
                fs.createReadStream(file).pipe(res);
            }
        } else if (pathname === '/auth/check') json(identity);
        else if (pathname === '/auth/login') json({ ...identity, tenants: [TENANT] });
        else if (pathname === '/auth/logout') json({ status: 'success' });
        else if (pathname === '/auth/me') json({ ...identity, tenants: [TENANT], current_tenant: TENANT });
        else if (pathname === '/auth/context') json({ status: 'success',
            authorization_mode: 'role', is_tenant_admin: true, is_platform_admin: false,
            tenant: TENANT,
            console_pages: {
                'admin.external_connections': { available: true, read_allowed: true, scope: 'tenant' },
            } });
        else if (pathname === '/api/version') json({ status: 'success', version: 'external-fixture' });
        else if (pathname === '/api/branding/public') {
            json({ enabled: false, revision: 1, logo_url: '', favicon_url: '' });
        } else if (pathname === '/config') json({ status: 'success', title: '容大AI',
            model: 'fixture-model', providers: {}, agent_permission_mode: 'workspace-write',
            permission_modes: ['read-only', 'workspace-write', 'full-access'] });
        else if (pathname === '/api/agents') {
            // The console reads two projections off one address: the management
            // snapshot (default id, roster revision, channel instances) and the
            // workbench one, which the boot chain fetches and which must carry
            // `can_chat` / `is_default` per Agent — answering the management
            // shape there is reported as "management snapshot returned" and
            // stops the app from ever becoming visible.
            if (url.searchParams.get('view') === 'workbench') {
                json({ status: 'success', empty_reason: '', agents: [
                    { id: 'agent-1', name: '税法知识库助手', description: '',
                        avatar: null, is_default: true, can_chat: true,
                        unavailable_reason: null, agent_type: 'single',
                        position: '', category: '', tags: [],
                        greeting: '', usage_hint: '', suggested_questions: [] },
                ] });
                return;
            }
            json({ status: 'success', default_agent_id: null,
                revision: 'fixture', agents: [], channel_instances: [] });
        }
        else if (pathname === '/api/external-connections/types') {
            json({ status: 'success', types: [MCP_TYPE], form_version: 1 });
        } else if (pathname === '/api/external-connections/catalog') {
            json({ status: 'success', items: [MCP_CARD], total: 1,
                scope: url.searchParams.get('scope') || 'tenant' });
        }         else if (pathname === '/api/external-connections/tenant/erp-default') {
            json({ status: 'success', connection_id: null });
        } else if (/\/tenant\/[^/]+\/agent-assignments$/.test(pathname)) {
            if (req.method === 'POST') {
                const raw = await readBody(req);
                let body = null;
                try { body = JSON.parse(raw); } catch (_) { body = null; }
                report.writes.push({ pathname, body });
                assignment.saves.push(body);
                if (assignment.conflictOnce) {
                    // The revision moved on between open and save; the page has
                    // to keep the draft and ask for a re-read, never re-send.
                    assignment.conflictOnce = false;
                    json({ status: 'error', code: 'assignment_version_conflict',
                        message: 'assignments changed; reload before saving' }, 409);
                    return;
                }
                if (body && body.expected_revision !== assignment.revision) {
                    json({ status: 'error', code: 'assignment_version_conflict',
                        message: 'assignments changed; reload before saving' }, 409);
                    return;
                }
                const added = (body && body.add_agent_ids) || [];
                const removed = (body && body.remove_agent_ids) || [];
                assignment.assigned = assignment.assigned
                    .filter(id => removed.indexOf(id) < 0)
                    .concat(added.filter(id => assignment.assigned.indexOf(id) < 0));
                assignment.configured = true;
                assignment.revision += 1;
                MCP_CARD.agent_assignment = {
                    configured: true, revision: assignment.revision,
                    visible_count: assignment.assigned.length,
                    can_assign: assignment.canAssign,
                };
                json({ status: 'success', configured: true, revision: assignment.revision,
                    added: added.length, removed: removed.length, unchanged: false });
            } else {
                json(agentPage(assignment.assigned.map(id => agentRow(id, true)), url));
            }
        } else if (/\/tenant\/[^/]+\/agent-candidates$/.test(pathname)) {
            const needle = (url.searchParams.get('q') || '').trim().toLowerCase();
            if (!needle) {
                json({ status: 'error', code: 'field_required', message: 'a search term is required' }, 400);
                return;
            }
            const rows = AGENT_ROSTER
                .filter(agent => agent.id.toLowerCase().indexOf(needle) >= 0
                    || agent.name.toLowerCase().indexOf(needle) >= 0)
                .map(agent => agentRow(agent.id, assignment.assigned.indexOf(agent.id) >= 0));
            json(agentPage(rows, url));
        } else if (pathname === '/api/external-connections/tenant' && req.method === 'POST') {
            const raw = await readBody(req);
            let body = null;
            try { body = JSON.parse(raw); } catch (_) { body = null; }
            report.writes.push({ pathname, body });
            json({ status: 'success', item: { ...MCP_CARD, id: 'mcp-new' } });
        } else if (['/api/sessions', '/api/history', '/api/projects', '/api/models', '/api/tools',
                   '/api/skills'].includes(pathname)) {
            json({ status: 'success', sessions: [], has_more: false, total: 0, messages: [],
                recents: [], tree: [], root_files: [], providers: [], items: [] });
        } else if (pathname === '/poll') json({ status: 'success', has_content: false });
        else if (pathname.startsWith('/api/')) json({ status: 'success', items: [] });
        else {
            report.unexpectedRoutes.push({ pathname, method: req.method });
            json({ status: 'error', message: 'Unconfigured fixture route' }, 404);
        }
    };
    handle().catch(error => {
        report.pageErrors.push({ scenario: report.currentScenario, message: String(error) });
        json({ status: 'error', message: 'fixture handler failed' }, 500);
    });
});

let browser, origin;

async function launchBrowser() {
    try {
        return await chromium.launch();
    } catch (error) {
        return await chromium.launch({ channel: 'chrome' });
    }
}

async function open(options) {
    const size = options || {};
    const context = await browser.newContext({
        viewport: { width: size.width || 1440, height: size.height || 900 },
        locale: 'zh-CN', colorScheme: 'light',
    });
    context.on('page', page => page.on('pageerror', error => {
        report.pageErrors.push({ scenario: report.currentScenario, message: error.message,
            stack: error.stack });
    }));
    await context.addInitScript(() => {
        if (!location.href.startsWith('http:')) return;
        sessionStorage.setItem('cow_tenant_id', 'fixture-tenant');
        localStorage.setItem('cow_lang', 'zh');
    });
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    // 系统接入 is a console-shell page (`admin.external_connections`), so it is
    // reached from `/admin`; `/chat` would keep it out of the navigation.
    await page.goto(origin + '/admin#view-external-connections', { waitUntil: 'networkidle' });
    await page.locator('#app').waitFor({ state: 'visible' });
    await page.locator('#view-external_connections').waitFor({ state: 'visible' });
    return page;
}

async function scenario(name, run, options) {
    report.currentScenario = name;
    report.scenarioBase = report.requests.length;
    const start = Date.now();
    let page = null;
    try {
        page = await open(options);
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

/** 新增连接 → pick MCP, leaving the drawer open on the create form. */
async function openMcpDrawer(page) {
    await page.locator('[data-ec-action="add"]').click();
    await page.locator('#ec-modal').waitFor({ state: 'visible' });
    await page.locator('[data-ec-action="pick-type"][data-kind="mcp"]').click();
    await page.locator('#ec-drawer').waitFor({ state: 'visible' });
}

/** Type into a box through a real input method.
 *
 * Chromium's own IME simulation (`Input.imeSetComposition` for the pre-edit
 * 拼音, `Input.insertText` to commit) puts the box through the same events a
 * Chinese input method sends: compositionstart, an `input` whose `isComposing`
 * is true and whose value is the pinyin, and finally the committed characters
 * followed by compositionend.
 *
 * The node is marked before typing, and `survived` is read while the pre-edit
 * text is still uncommitted: that is the question these scenarios exist for —
 * did the box the input method was writing into still exist 600ms later, when
 * the search debounce has certainly expired? `observe` runs at that same
 * checkpoint, so a caller can look at the page mid-composition too.
 */
async function imeType(page, selector, pinyin, committed, observe) {
    const cdp = await page.context().newCDPSession(page);
    await page.evaluate(sel => {
        document.querySelector(sel).__ime = true;
        window.__imeLog = [];
        ['compositionstart', 'compositionupdate', 'compositionend'].forEach(type => {
            document.addEventListener(type, () => window.__imeLog.push(type), true);
        });
    }, selector);
    await page.locator(selector).click();
    await cdp.send('Input.imeSetComposition',
        { text: pinyin, selectionStart: pinyin.length, selectionEnd: pinyin.length });
    await page.waitForTimeout(600);
    const survived = await page.evaluate(sel => {
        const node = document.querySelector(sel);
        return !!(node && node.__ime);
    }, selector);
    const seen = observe ? await observe() : null;
    await cdp.send('Input.insertText', { text: committed });
    await page.waitForTimeout(100);
    const log = await page.evaluate(() => window.__imeLog.slice());
    await cdp.detach();
    return { log, survived, seen };
}

async function main() {
    browser = await launchBrowser();
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    origin = 'http://127.0.0.1:' + server.address().port;

    await scenario('选择「请求头认证」后要出现填写令牌的字段', async page => {
        await openMcpDrawer(page);

        // Without authentication the credential field is not offered at all.
        assert.equal(await page.locator('#ec-f-header_name').isVisible(), false,
            '无需认证时不应出现请求头名称');
        assert.equal(await page.locator('#ec-f-secret-header').isVisible(), false,
            '无需认证时不应出现可以填令牌的输入框');

        await page.selectOption('#ec-f-auth', 'header');

        // The regression: the change has to reach the drawer's own listener, or
        // these two groups stay hidden and the token has nowhere to go.
        assert.equal(await page.locator('#ec-f-header_name').isVisible(), true,
            '选择「请求头认证」后必须出现请求头名称');
        assert.equal(await page.locator('#ec-f-secret-header').isVisible(), true,
            '选择「请求头认证」后必须出现认证请求头值（令牌填这里）');

        // Typing has to register too: the save state reporting unsaved input is
        // the same wiring that marks the draft dirty.
        await page.fill('#ec-f-name', '税法知识库');
        await page.fill('#ec-f-url', 'http://127.0.0.1:8080/mcp');
        await page.fill('#ec-f-header_name', 'Authorization');
        await page.fill('#ec-f-secret-header', 'Bearer TOKEN-123');
        assert.match(await page.locator('.ec-save-state').innerText(), /未保存/,
            '输入后底部必须提示有未保存的修改');
        await page.screenshot({ path: path.join(output, 'header-auth-fields.png') }).catch(() => {});

        await page.locator('[data-ec-action="drawer-save"]').click();
        await page.locator('#ec-drawer').waitFor({ state: 'hidden' });

        const write = report.writes[report.writes.length - 1];
        assert.ok(write, '保存必须发出创建请求');
        assert.equal(write.body.name, '税法知识库');
        assert.equal(write.body.config.auth, 'header');
        assert.equal(write.body.config.header_name, 'Authorization',
            '头名进入 config，不是秘密');
        assert.deepEqual(write.body.secrets, { header: 'Bearer TOKEN-123' },
            '令牌只进入 secrets，且正是认证请求头值里的内容');
    });

    await scenario('切到 stdio 后要出现启动命令', async page => {
        await openMcpDrawer(page);
        assert.equal(await page.locator('#ec-f-command').isVisible(), false,
            '远程传输时不出现启动命令');
        await page.selectOption('#ec-f-transport', 'stdio');
        assert.equal(await page.locator('#ec-f-command').isVisible(), true,
            '切到 stdio 后必须出现启动命令');
        assert.equal(await page.locator('#ec-f-url').isVisible(), false,
            'stdio 不再要求服务地址');
    });

    // -- Published tools, no per-tool declaration (change add-external-mcp-readonly-tool-execution)

    await scenario('MCP 表单不再出现逐条工具声明，只提交连接级配置', async page => {
        await openMcpDrawer(page);
        assert.equal(await page.locator('#ec-f-read_only_tools').count(), 0,
            '表单不得再提供逐条工具声明');
        assert.equal(await page.locator('[data-ec-field-wrap="read_only_tools"]').count(), 0,
            '也不得留下该字段的外壳或错误位');

        // 可调用性来自服务器的发布，与传输方式无关：切到 stdio 也不该冒出声明字段。
        await page.selectOption('#ec-f-transport', 'stdio');
        assert.equal(await page.locator('#ec-f-read_only_tools').count(), 0,
            '切到 stdio 后同样没有声明字段');
        await page.selectOption('#ec-f-transport', 'streamable_http');

        await page.fill('#ec-f-name', '知识库 MCP');
        await page.fill('#ec-f-url', 'http://127.0.0.1:8080/mcp');
        await page.screenshot({ path: path.join(output, 'mcp-connection-level-only.png') });

        await page.locator('[data-ec-action="drawer-save"]').click();
        await page.locator('#ec-drawer').waitFor({ state: 'hidden' });

        const write = report.writes[report.writes.length - 1];
        assert.ok(write, '保存必须发出创建请求');
        assert.deepEqual(Object.keys(write.body.config).sort(), ['auth', 'transport', 'url'],
            '只提交连接级配置，不枚举远端工具');
        assert.deepEqual(write.body.secrets, undefined, '没有工具名冒充凭据');
    });

    // -- Agent assignment (change add-external-connection-agent-assignment) ---

    /** Close the type picker and open the assignment modal from the card. */
    async function openAssignments(page) {
        await page.locator('[data-ec-action="agents"]').first().click();
        await page.locator('#ec-modal').waitFor({ state: 'visible' });
        await page.locator('#ec-modal-panel [data-ec-agents-save]').waitFor({ state: 'visible' });
        await page.locator('#ec-modal-panel .ec-assign-list').first().waitFor({ state: 'visible' });
    }

    /** Set the server-side relation, then reload so the page reads it again. */
    async function withRelation(page, options) {
        resetAssignments(options);
        await page.reload({ waitUntil: 'networkidle' });
        await page.locator('#view-external_connections').waitFor({ state: 'visible' });
    }

    await scenario('分配弹窗首屏显示已分配列表与卡片摘要', async page => {
        await withRelation(page, {});
        await page.locator('[data-ec-assign-summary]')
            .filter({ hasText: '已配置 · 2 个智能体可用' }).first().waitFor({ state: 'visible' });
        await openAssignments(page);

        const rows = page.locator('#ec-modal-panel .ec-assign-list .ec-assign-row');
        assert.equal(await rows.count(), 2, '首屏必须列出已分配的两个智能体');
        assert.equal(await page.locator('#ec-modal-panel .ec-assign-section-title').first().innerText(),
            '已分配（2）', '已分配区标题要给出可见数量');
        // The roster is never downloaded: opening the modal sends no search.
        assert.equal(report.requests.filter(
            entry => entry.pathname.indexOf('/agent-candidates') >= 0).length, 0,
            '未输入关键词时不得加载候选');
        assert.match(await page.locator('#ec-modal-panel .ec-assign-pending').innerText(),
            /没有待保存的变更/);
        await page.screenshot({ path: path.join(output, 'assign-desktop-assigned.png') });
    });

    await scenario('搜索添加智能体并保存差量', async page => {
        await withRelation(page, {});
        await openAssignments(page);

        await page.fill('#ec-agents-search', '合同');
        // Debounced: the request only leaves after the pause, and it carries the
        // term rather than the whole roster.
        await page.locator('#ec-modal-panel [data-ec-agents-add="agent-3"]')
            .waitFor({ state: 'visible' });
        const searched = report.requests.filter(
            entry => entry.pathname.indexOf('/agent-candidates') >= 0);
        assert.equal(searched.length, 1, '输入关键词只发一次候选请求');
        assert.match(searched[0].query, /(^|[?&])q=%E5%90%88%E5%90%8C(&|$)/,
            '候选请求要带上关键词，而不是下载全部智能体');
        assert.match(searched[0].query, /(^|[?&])page=1(&|$)/);
        assert.match(searched[0].query, /page_size=20/);

        await page.locator('#ec-modal-panel [data-ec-agents-add="agent-3"]').click();
        assert.match(await page.locator('#ec-modal-panel .ec-assign-pending').innerText(),
            /待保存：新增 1/);
        await page.screenshot({ path: path.join(output, 'assign-desktop-draft-add.png') });

        await page.locator('[data-ec-agents-save]').click();
        await page.locator('#ec-modal-panel .ec-assign-list .ec-assign-row')
            .filter({ hasText: '合同审阅助手' }).first().waitFor({ state: 'visible' });

        // The delta is the whole request that matters: the two named lists and
        // the revision they were read at — never the page, never the visible set.
        assert.deepEqual(lastSaveBody(), {
            expected_revision: 3, add_agent_ids: ['agent-3'], remove_agent_ids: [] },
        '保存必须只发差量与版本号，不整体替换');
        assert.equal(assignment.assigned.indexOf('agent-3') >= 0, true);
        await page.locator('[data-ec-agents-cancel]').click();
        // The card is updated from the server's own number, not a local count.
        await page.locator('[data-ec-assign-summary]')
            .filter({ hasText: '已配置 · 3 个智能体可用' }).first().waitFor({ state: 'visible' });
    });

    await scenario('行内移除可撤销，取消不落库', async page => {
        await withRelation(page, {});
        await openAssignments(page);
        await page.locator('#ec-modal-panel [data-ec-agents-remove="agent-1"]').click();
        assert.match(await page.locator('#ec-modal-panel .ec-assign-pending').innerText(),
            /待保存：移除 1/);
        await page.screenshot({ path: path.join(output, 'assign-desktop-draft-remove.png') });

        // 撤销移除: the same control toggles back and the change is gone.
        await page.locator('#ec-modal-panel [data-ec-agents-remove="agent-1"]').click();
        assert.match(await page.locator('#ec-modal-panel .ec-assign-pending').innerText(),
            /没有待保存的变更/);

        await page.locator('#ec-modal-panel [data-ec-agents-remove="agent-2"]').click();
        // 未保存处理 is reused rather than re-invented: cancelling with a draft
        // asks first, and only the caller's answer discards the change.
        const prompts = [];
        page.on('dialog', async dialog => {
            prompts.push(dialog.message());
            await dialog.accept();
        });
        await page.locator('[data-ec-agents-cancel]').click();
        await page.locator('#ec-modal').waitFor({ state: 'hidden' });
        assert.equal(prompts.length, 1, '取消时草稿必须确认后才丢弃');
        assert.match(prompts[0], /不会保存/);
        assert.equal(assignment.assigned.join(','), 'agent-1,agent-2',
            '取消必须丢弃草稿，不写任何关联');
        assert.equal(assignment.saves.length, 0, '取消不得发出保存请求');
    });

    await scenario('首次保存提示影响并允许空配置保存', async page => {
        await withRelation(page, { configured: false, assigned: [] });
        await page.locator('[data-ec-assign-summary]')
            .filter({ hasText: '未配置 · 沿用原权限' }).first().waitFor({ state: 'visible' });

        await page.locator('[data-ec-action="agents"]').first().click();
        await page.locator('#ec-modal-panel .ec-banner-warn').waitFor({ state: 'visible' });
        assert.match(await page.locator('#ec-modal-panel .ec-banner-warn').innerText(),
            /首次保存将启用限制/);
        assert.equal(await page.locator('#ec-modal-panel .ec-assign-empty').innerText(),
            '该连接尚未配置，当前所有智能体沿用原权限。');
        // Nothing is assigned yet, and 空保存 is still explicitly offered.
        assert.match(await page.locator('#ec-modal-panel .ec-assign-pending').innerText(),
            /首次保存将配置为空/);
        assert.equal(await page.locator('[data-ec-agents-save]').isEnabled(), true,
            '首次空保存必须是可点的明确动作');

        await page.locator('[data-ec-agents-save]').click();
        await page.locator('#ec-modal-panel .ec-assign-empty')
            .filter({ hasText: '没有可见的已分配智能体' }).first().waitFor({ state: 'visible' });
        assert.deepEqual(lastSaveBody(),
            { expected_revision: 3, add_agent_ids: [], remove_agent_ids: [] },
            '首次保存即使差量为空也必须原子提交 configured');
        assert.equal(assignment.configured, true);
        await page.locator('[data-ec-agents-cancel]').click();
        await page.locator('[data-ec-assign-summary]')
            .filter({ hasText: '已配置 · 0 个智能体可用' }).first().waitFor({ state: 'visible' });
    });

    await scenario('版本冲突保留草稿并要求重读', async page => {
        await withRelation(page, {});
        await openAssignments(page);
        await page.locator('#ec-modal-panel [data-ec-agents-remove="agent-1"]').click();
        assignment.conflictOnce = true;

        await page.locator('[data-ec-agents-save]').click();
        await page.locator('#ec-modal-panel .ec-banner-error').waitFor({ state: 'visible' });
        assert.match(await page.locator('#ec-modal-panel .ec-banner-error').innerText(),
            /草稿已保留/);
        assert.equal(await page.locator('#ec-modal-panel [data-ec-agents-reload]').isVisible(), true,
            '冲突后必须提供重新读取入口');
        assert.match(await page.locator('#ec-modal-panel .ec-assign-pending').innerText(),
            /待保存：移除 1/, '冲突不得丢弃草稿');
        assert.equal(await page.locator('#ec-modal-panel [data-ec-agents-save]').isDisabled(), true,
            '冲突未解决前不自动重发，保存保持不可用');
        assert.equal(assignment.assigned.join(','), 'agent-1,agent-2',
            '冲突时服务端关系不得被改动');
        await page.screenshot({ path: path.join(output, 'assign-desktop-conflict.png') });

        await page.locator('#ec-modal-panel [data-ec-agents-reload]').click();
        await page.locator('#ec-modal-panel .ec-banner-error').waitFor({ state: 'hidden' });
        await page.locator('[data-ec-agents-save]').click();
        await page.locator('#ec-modal-panel .ec-assign-list .ec-assign-row')
            .filter({ hasText: '税法知识库助手' }).first()
            .waitFor({ state: 'detached' });
        assert.equal(assignment.assigned.join(','), 'agent-2', '重读后再保存才生效');
    });

    await scenario('窄屏（360px）首屏与草稿仍完整可用', async page => {
        await withRelation(page, {});
        await page.locator('[data-ec-action="agents"]').first().click();
        await page.locator('#ec-modal-panel .ec-assign-list').first().waitFor({ state: 'visible' });
        // 390px 断点内的布局：列表、按钮与底部动作都要留在视口内可点。
        const box = await page.locator('#ec-modal-panel').boundingBox();
        assert.ok(box.width <= 360, '面板不得溢出 360px 视口：' + box.width);
        const save = await page.locator('[data-ec-agents-save]').boundingBox();
        assert.ok(save.x >= 0 && save.x + save.width <= 361, '保存按钮必须在视口内');
        assert.equal(await page.locator('#ec-modal-panel .ec-assign-row').first().isVisible(), true);
        await page.screenshot({ path: path.join(output, 'assign-narrow-360.png') });

        await page.fill('#ec-agents-search', '合同');
        await page.locator('#ec-modal-panel [data-ec-agents-add="agent-3"]')
            .waitFor({ state: 'visible' });
        await page.locator('#ec-modal-panel [data-ec-agents-add="agent-3"]').click();
        await page.screenshot({ path: path.join(output, 'assign-narrow-360-draft.png') });
        assert.match(await page.locator('#ec-modal-panel .ec-assign-pending').innerText(),
            /待保存：新增 1/);
    }, { width: 360, height: 780 });

    await scenario('中文输入法组合提交前不重建分配搜索框', async page => {
        await withRelation(page, {});
        await openAssignments(page);
        const before = report.requests.length;

        const typed = await imeType(page, '#ec-agents-search', 'hetong', '合同');
        assert.ok(typed.log.indexOf('compositionstart') >= 0
            && typed.log.indexOf('compositionend') >= 0,
        '浏览器必须真的送出输入法组合事件，否则这条用例什么也没验证：' + typed.log.join(','));
        // 拼音在屏幕上停留了 600ms，远超 300ms 防抖：这一步以前会把输入框连同
        // 未提交的拼音一起重绘掉，中文因此永远提交不上。
        assert.equal(typed.survived, true,
            '组合提交前输入框不得被重建，否则输入法会丢掉未提交的拼音');

        // 提交后才搜索，且用的是提交后的中文。
        await page.locator('#ec-modal-panel [data-ec-agents-add="agent-3"]')
            .waitFor({ state: 'visible' });
        const asked = report.requests.slice(before).filter(
            entry => entry.pathname.indexOf('/agent-candidates') >= 0);
        assert.equal(asked.length, 1, '一次提交只发一次候选请求：' + JSON.stringify(asked));
        assert.match(asked[0].query, /(^|[?&])q=%E5%90%88%E5%90%8C(&|$)/,
            '请求带的是提交后的中文，而不是拼音');
        assert.equal(asked.filter(entry => /hetong/i.test(entry.query)).length, 0,
            '拼音不得成为查询');
        assert.equal(await page.locator('#ec-modal-panel [data-ec-agents-search]').inputValue(),
            '合同', '提交后的文本留在输入框里');
        await page.screenshot({ path: path.join(output, 'assign-ime-composition.png') });
    });

    await scenario('中文输入法组合提交前不重建目录搜索框', async page => {
        await page.locator('[data-ec-card]').first().waitFor({ state: 'visible' });

        const typed = await imeType(page, '#ec-search', 'hetong', '合同',
            () => page.locator('[data-ec-card]').count().then(cards => ({ cards })));
        assert.ok(typed.log.indexOf('compositionend') >= 0,
            '浏览器必须真的送出输入法组合事件：' + typed.log.join(','));
        assert.equal(typed.survived, true,
            '组合提交前目录搜索框不得被重建');
        // 目录的筛选是靠重绘生效的：组合期间没有重绘，也就没有按拼音筛选。
        assert.equal(typed.seen.cards, 1, '组合期间不得按拼音筛选连接卡片');

        await page.locator('.ec-panel-title')
            .filter({ hasText: '没有找到匹配的连接' }).first().waitFor({ state: 'visible' });
        assert.equal(await page.locator('#ec-search').inputValue(), '合同',
            '提交后的文本留在输入框里，并据此筛选');
    });

    await scenario('输入法取消候选的 Escape 不当作关闭弹窗', async page => {
        await withRelation(page, {});
        await openAssignments(page);

        // 组合中的 Escape 是输入法取消候选：浏览器把它标成 keyCode 229 /
        // isComposing，页面若把它当成「关闭弹窗」就会丢掉正在输入的词。
        const cdp = await page.context().newCDPSession(page);
        await page.locator('#ec-agents-search').click();
        await cdp.send('Input.imeSetComposition',
            { text: 'hetong', selectionStart: 6, selectionEnd: 6 });
        await cdp.send('Input.dispatchKeyEvent',
            { type: 'keyDown', key: 'Escape', code: 'Escape',
                windowsVirtualKeyCode: 229, nativeVirtualKeyCode: 229 });
        await page.waitForTimeout(100);
        assert.equal(await page.locator('#ec-modal').isVisible(), true,
            '输入法取消候选不得关闭分配弹窗');
        assert.ok(await page.locator('#ec-agents-search').isVisible(),
            '输入法正在书写的搜索框还在');
        await cdp.detach();

        // 不是输入法的 Escape 仍然关闭弹窗：这条用例区分的是两者，不是「Escape 失效」。
        await page.keyboard.press('Escape');
        await page.locator('#ec-modal').waitFor({ state: 'hidden' });
    });

    await scenario('只读调用方看得见关系但操作全部不可用', async page => {
        await withRelation(page, { canAssign: false });
        await openAssignments(page);
        assert.match(await page.locator('#ec-modal-panel .ec-banner-info').innerText(),
            /添加和移除需要连接管理权限/);
        assert.equal(await page.locator('#ec-modal-panel [data-ec-agents-remove="agent-1"]').isDisabled(),
            true, '只读时行内移除不可用');
        assert.equal(await page.locator('[data-ec-agents-save]').isDisabled(), true);
        await page.screenshot({ path: path.join(output, 'assign-desktop-readonly.png') });
    });

    // -- Execution availability (change add-external-connection-agent-assignment)
    //
    // The deployment this was reported against: MCP testing is open while every
    // execution class is closed, and an Agent had been assigned to the
    // connection. The card reported the assignment and the connection test and
    // said nothing about execution, so the two read as "the tools work" — while
    // every tool call was still refused. `classes` below is the server's own
    // per-class shape (integrations/external/registry.py#capability_projection).
    await scenario('分配成功但业务执行关闭时，卡片分别呈现两件事', async page => {
        const cap = MCP_TYPE.capabilities;
        const saved = { classes: cap.classes, open: cap.open,
            test: cap.test_available, exec: cap.execute_available,
            reason: cap.unavailable_reason,
            status: MCP_CARD.test_status, testedAt: MCP_CARD.tested_at };
        cap.classes = {
            configure: { available: true, reason: '' },
            test: { available: true, reason: '' },
            read_execute: { available: false, reason: 'awaiting_mcp_test_environment' },
            write_execute: { available: false, reason: 'not_supported_by_type' },
        };
        cap.open = ['configure', 'test'];
        cap.test_available = true;
        cap.execute_available = false;
        cap.unavailable_reason = 'awaiting_mcp_test_environment';
        MCP_CARD.test_status = 'ok';
        MCP_CARD.tested_at = 1790478000;
        try {
            await withRelation(page, {});
            const card = page.locator('[data-ec-card]').first();
            await card.locator('[data-ec-exec-state]').waitFor({ state: 'visible' });

            // All three facts are on the card at once, each on its own line.
            assert.match(await card.locator('[data-ec-assign-summary]').innerText(),
                /已配置 · 2 个智能体可用/, '分配成功要照实报告');
            assert.match(await card.locator('.ec-test-state').innerText(), /连接正常/,
                '连接测试成功也要照实报告');
            assert.match(await card.locator('[data-ec-exec-state]').innerText(),
                /工具执行未开放/, '执行未开放必须单独说明');

            // The reason is the server's own sentence and it explains the
            // execution class only. On this very card the test class is *open*,
            // so a reason that concluded 「测试保持关闭」 would be false — and it
            // is the button below that proves the test class really is usable.
            assert.match(await card.locator('.ec-card-exec-reason').innerText(),
                /尚未取得 MCP 测试环境的验收证据。/, '原因要来自服务端投影');
            assert.equal(await card.locator('[data-ec-action="test"]').isEnabled(), true,
                '测试在这张卡上是开放的，原因句不能替它下结论');

            // 分别表达 is the whole point: the execution line is its own fact,
            // not a decoration appended to the connection-test row.
            assert.doesNotMatch(await card.locator('.ec-test-state').innerText(),
                /工具执行未开放/, '执行状态不能混进连接测试那一行');
            assert.equal(await card.locator('.ec-card-exec-reason').count(), 1);
            await page.screenshot({ path: path.join(output, 'execution-closed-on-card.png') });

            // And with both execution classes open the line says so instead of
            // going quiet, so its absence still means "no projection to read".
            cap.classes.read_execute = { available: true, reason: '' };
            cap.execute_available = true;
            await page.reload({ waitUntil: 'networkidle' });
            await page.locator('[data-ec-exec-state]').first().waitFor({ state: 'visible' });
            assert.match(await page.locator('[data-ec-exec-state]').first().innerText(),
                /工具执行仅开放读取/, '读取开放要写成仅读取');
            // Half-open still owes the reader a reason: it is the write class
            // that stays shut, and the sentence has to be that one's.
            assert.match(await page.locator('.ec-card-exec-reason').first().innerText(),
                /该类型不支持此能力。/, '未开放的那一半的原因要跟着说明');
            assert.doesNotMatch(await page.locator('[data-ec-exec-state]').first().innerText(),
                /尚未取得 MCP 测试环境的验收证据/, '不能借用已开放那一半的原因');
        } finally {
            cap.classes = saved.classes;
            cap.open = saved.open;
            cap.test_available = saved.test;
            cap.execute_available = saved.exec;
            cap.unavailable_reason = saved.reason;
            MCP_CARD.test_status = saved.status;
            MCP_CARD.tested_at = saved.testedAt;
        }
    });

    const failed = report.scenarios.filter(entry => !entry.passed);
    if (report.pageErrors.length) {
        throw new Error('page errors: ' + JSON.stringify(report.pageErrors, null, 2));
    }
    if (report.unexpectedRoutes.length) {
        throw new Error('unexpected routes: ' + JSON.stringify(report.unexpectedRoutes, null, 2));
    }
    console.log('external connections browser: ' + report.scenarios.length + ' scenarios, '
        + failed.length + ' failed');
    console.log('artifact: ' + path.join(output, 'results.json'));
}

main()
    .catch(error => { console.error(error); process.exitCode = 1; })
    .finally(() => { if (browser) return browser.close(); })
    .finally(() => { server.close(); });
