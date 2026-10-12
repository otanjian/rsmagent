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
    requests: [], writes: [], draftTests: [] };
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
    enabled: true, source: 'tenant', version: 6, test_status: 'untested', tested_at: null,
    actions: ['read', 'manage'], base_connection_id: null,
    // The stored shape of an already-saved connection: the credential itself is
    // never sent to the browser, only the fact that a header group exists.
    config: { transport: 'streamable_http', url: 'http://127.0.0.1:8080/mcp',
        header_names: ['Authorization'], tool_name_prefix: 'kb_', timeout: 30 },
    agent_assignment: { configured: true, revision: 3, visible_count: 2, can_assign: true },
};

// -- The MCP editor's own fixtures (change align-mcp-editor-with-master) ------
//
// Three things the editor cannot be checked without:
// * `draftOutcome` — the draft-test route's verdict, so a probe failure can be
//   followed by a save ("仍然保存");
// * `createRefusals` — which names the create route refuses, so a batch stops
//   part-way and the retry has a real "already confirmed" prefix to leave alone;
// * the recorded `Idempotency-Key` per create, so key retention across a retry
//   is asserted from the wire rather than from the module's own state.
let draftOutcome = 'ok';
let createRefusals = {};

function resetEditorFixtures() {
    draftOutcome = 'ok';
    createRefusals = {};
}


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
// The route table is one of them, and it is not optional: `console.js` asks
// `window.ConsoleRoutes` for the view it was opened on before it falls back to
// its own hash handling, so a placeholder left in the page throws in `router.js`
// and strands the console on whatever the view loader guessed.
//
// Rendering through the real template keeps the fixture on the same bytes the
// server sends, exactly as `test_knowledge_console_browser.cjs` does. The entry
// paths come from the same registry: once `ConsoleRoutes` is live, entering a
// view rewrites the address to the view's own path, and the next reload asks for
// that path instead of `/admin#view-...`.
let shellHtml = '';
const shellPaths = new Set(['/']);
function assembleShell() {
    const python = process.env.PYTHON_BIN || path.join(repo, '.venv/bin/python');
    const raw = require('node:child_process').execFileSync(python, ['-B', '-c',
        'import json; from channel.web.core import template; from channel.web.route_registry import console_page_metadata, shell_entry_paths; '
        + 'print(json.dumps({"html": template.render("chat.html").replace("{{COW_CONSOLE_ROUTES}}", json.dumps(console_page_metadata())), "paths": shell_entry_paths()}))'],
        { cwd: repo, encoding: 'utf8', maxBuffer: 8 * 1024 * 1024 });
    const shell = JSON.parse(raw);
    shellHtml = shell.html
        .replaceAll('{{COW_DEFAULT_LANG}}', 'zh')
        .replaceAll('{{COW_NAVIGATION_MODE}}', 'classic')
        .replaceAll('{{COW_WORKBENCH_SIDEBAR_LAUNCH_V2}}', '0');
    shell.paths.forEach(entry => shellPaths.add(entry));
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
        if (shellPaths.has(pathname)) {
            res.writeHead(200, { 'Content-Type': mime['.html'], 'Cache-Control': 'no-store' });
            res.end(shellHtml);
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
        } else if (pathname === '/api/external-connections/draft-test') {
            const raw = await readBody(req);
            let body = null;
            try { body = JSON.parse(raw); } catch (_) { body = null; }
            report.draftTests.push({ body });
            if (draftOutcome === 'ok') {
                json({ status: 'success', outcome: 'ok', stage: 'connect',
                    metadata: { tools: ['echo'] } });
            } else {
                json({ status: 'success', outcome: 'fail', stage: 'connect',
                    code: 'connection_refused' });
            }
        } else if (/^\/api\/external-connections\/tenant\/[^/]+\/update$/.test(pathname)) {
            const raw = await readBody(req);
            let body = null;
            try { body = JSON.parse(raw); } catch (_) { body = null; }
            report.writes.push({ pathname, body,
                idempotencyKey: req.headers['idempotency-key'] || '' });
            json({ status: 'success', id: pathname.split('/')[4] });
        } else if (pathname === '/api/external-connections/tenant' && req.method === 'POST') {
            const raw = await readBody(req);
            let body = null;
            try { body = JSON.parse(raw); } catch (_) { body = null; }
            report.writes.push({ pathname, body,
                idempotencyKey: req.headers['idempotency-key'] || '' });
            // A definite refusal: the editor has to stop the run here, keep the
            // confirmed items out of the retry, and re-key only after the input
            // changes.
            if (body && createRefusals[body.name]) {
                json({ status: 'error', code: createRefusals[body.name],
                    message: body.name + ' refused by the fixture' }, 409);
                return;
            }
            json({ status: 'success', item: { ...MCP_CARD, id: 'mcp-' + (body && body.name) } });
        } else if (/^\/api\/external-connections\/tenant\/[^/]+$/.test(pathname)) {
            // The detail the editor opens an editing draft from. It carries the
            // version the draft is bound to and never the stored credential.
            json({ status: 'success', ...MCP_CARD });
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
    const theme = size.theme || 'light';
    const context = await browser.newContext({
        viewport: { width: size.width || 1440, height: size.height || 900 },
        locale: 'zh-CN', colorScheme: theme,
    });
    context.on('page', page => page.on('pageerror', error => {
        report.pageErrors.push({ scenario: report.currentScenario, message: error.message,
            stack: error.stack });
    }));
    await context.addInitScript(theme => {
        if (!location.href.startsWith('http:')) return;
        sessionStorage.setItem('cow_tenant_id', 'fixture-tenant');
        localStorage.setItem('cow_lang', 'zh');
        // The appearance module reads its own keys before first paint.
        localStorage.setItem('cow_theme', theme);
    }, theme);
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
    // The shell the server would send, rendered before the fixture answers.
    assembleShell();
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    origin = 'http://127.0.0.1:' + server.address().port;

    // -- The master MCP editor (change align-mcp-editor-with-master) ----------
    //
    // 新增/编辑 an MCP connection happens in the centred editor extracted from
    // master, not in the drawer: these scenarios hold that down from the real
    // page — the transport groups, the form/JSON round trip, a batch that stops
    // part-way and retries with the keys it already used, an editing draft
    // bound to its row version, and the page's own unsaved-changes question
    // raised above the editor.

    /** Type picker → MCP, leaving the centred editor open on the create form. */
    async function openMcpEditor(page) {
        await page.locator('[data-ec-action="add"]').click();
        await page.locator('#ec-modal').waitFor({ state: 'visible' });
        await page.locator('[data-ec-action="pick-type"][data-kind="mcp"]').click();
        await page.locator('#mcp-editor-overlay').waitFor({ state: 'visible' });
    }

    /** The editor's transport control is the console's own dropdown. */
    async function chooseTransport(page, value) {
        await page.locator('#mcp-field-type .cfg-dropdown-selected').click();
        await page.locator('#mcp-field-type .cfg-dropdown-item[data-value="' + value + '"]').click();
    }

    function createWrites() {
        return report.writes.filter(entry => entry.pathname === '/api/external-connections/tenant');
    }

    await scenario('新增 MCP 打开居中编辑器：默认 stdio，改远程传输才出现地址与请求头', async page => {
        await openMcpEditor(page);

        // The drawer is the *other* editor's home: MCP must not fall back to it.
        assert.equal(await page.locator('#ec-drawer').isVisible(), false,
            'MCP 新增不得再走旧抽屉');
        assert.equal(await page.locator('#mcp-stdio-fields').isVisible(), true,
            '默认传输是 stdio，启动命令与参数就在首屏');
        assert.equal(await page.locator('#mcp-url-fields').isVisible(), false,
            'stdio 不显示服务地址');
        assert.equal(await page.locator('#mcp-advanced-fields').isVisible(), false,
            '高级设置默认收起');
        await page.screenshot({ path: path.join(output, 'mcp-editor-create-stdio.png') });

        await chooseTransport(page, 'streamable-http');
        assert.equal(await page.locator('#mcp-url-fields').isVisible(), true,
            '改到远程传输后必须出现地址与请求头');
        assert.equal(await page.locator('#mcp-stdio-fields').isVisible(), false,
            '远程传输不再要求启动命令');
        // The OAuth scope is a remote-only field: it lives in 高级设置, and the
        // transport switch is what makes it appear there.
        await page.locator('#mcp-advanced-toggle').click();
        assert.equal(await page.locator('#mcp-scope-field').isVisible(), true,
            '远程传输才有 OAuth scope');
        await page.screenshot({ path: path.join(output, 'mcp-editor-create-http.png') });

        await chooseTransport(page, 'stdio');
        // Back to stdio: the remote-only groups are gone again, so the two
        // transports really are two different forms rather than a growing one.
        assert.equal(await page.locator('#mcp-url-fields').isVisible(), false);
        assert.equal(await page.locator('#mcp-stdio-fields').isVisible(), true);
        assert.equal(await page.locator('#mcp-scope-field').isVisible(), false,
            'stdio 不提供 OAuth scope');
    });

    await scenario('表单与 JSON 双向同步，未知字段在提交前被点名', async page => {
        await openMcpEditor(page);
        await page.fill('#mcp-field-name', '知识库 MCP');
        await page.fill('#mcp-field-command', 'npx');
        await page.fill('#mcp-field-args', '-y\n@modelcontextprotocol/server-github');
        await page.fill('#mcp-field-env', 'API_KEY=fixture-key');

        await page.locator('[data-mcp-tab="json"]').click();
        const json = await page.locator('#mcp-field-json').inputValue();
        assert.match(json, /"mcpServers"/, 'JSON 面板给出 mcpServers 形状');
        assert.match(json, /"知识库 MCP"/, '表单里的名字进入 JSON');
        assert.match(json, /"@modelcontextprotocol\/server-github"/, '参数按行进入 JSON');
        assert.match(json, /API_KEY=fixture-key|"API_KEY": ?"fixture-key"/, '环境变量进入 JSON');

        // Back to the form: what the JSON box holds repaints the fields, so the
        // user can paste a config and keep editing it by hand.
        await page.fill('#mcp-field-json',
            JSON.stringify({ mcpServers: { 对话助手: { url: 'http://127.0.0.1:9100/mcp',
                type: 'streamable_http', timeout: 25 } } }, null, 2));
        await page.locator('[data-mcp-tab="form"]').click();
        assert.equal(await page.locator('#mcp-field-name').inputValue(), '对话助手',
            'JSON 里的名字回填到表单');
        assert.equal(await page.locator('#mcp-field-url').inputValue(), 'http://127.0.0.1:9100/mcp',
            'serverUrl/url 回填到地址');
        assert.equal(await page.locator('#mcp-field-timeout').inputValue(), '25',
            '高级字段也回填，并自动展开高级设置');

        // A field this editor cannot carry is named *before* anything is sent:
        // silently dropping it would submit a config the user did not write.
        await page.locator('[data-mcp-tab="json"]').click();
        await page.fill('#mcp-field-json',
            JSON.stringify({ mcpServers: { 坏配置: { url: 'http://127.0.0.1:9100/mcp', 工具: 1 } } }));
        const before = createWrites().length;
        await page.locator('#mcp-editor-save').click();
        await page.locator('#mcp-test-result.fail').waitFor({ state: 'visible' });
        assert.match(await page.locator('#mcp-test-result').innerText(), /工具/,
            '未知字段要被点名，而不是被丢掉或静默提交');
        assert.equal(createWrites().length, before, '校验失败不得发出创建请求');
        await page.screenshot({ path: path.join(output, 'mcp-editor-unknown-field.png') });

        await page.locator('#mcp-editor-cancel').click();
        await page.locator('[data-ec-action="modal-confirm"]').click();
        await page.locator('#mcp-editor-overlay').waitFor({ state: 'hidden' });
    });

    await scenario('Escape 关闭编辑器：先问未保存的输入，再按一次收回问题', async page => {
        resetEditorFixtures();
        await openMcpEditor(page);
        await page.fill('#mcp-field-name', '键盘关闭的 MCP');

        // 已输入内容，所以 Escape 必须先问，而不是直接丢弃。
        await page.keyboard.press('Escape');
        await page.locator('#ec-modal').waitFor({ state: 'visible' });
        assert.match(await page.locator('#ec-modal-panel').innerText(), /不会保存|未保存/,
            'Escape 不得直接丢弃草稿');
        assert.equal(await page.locator('#mcp-editor-overlay').isVisible(), true,
            '提问期间编辑器还在，输入也还在');

        // 第二下 Escape 关上的是问题本身（离焦点最近的那层），不是编辑器。
        await page.keyboard.press('Escape');
        await page.locator('#ec-modal').waitFor({ state: 'hidden' });
        assert.equal(await page.locator('#mcp-editor-overlay').isVisible(), true,
            '收回问题后编辑器仍然打开');
        assert.equal(await page.locator('#mcp-field-name').inputValue(), '键盘关闭的 MCP',
            '收回问题不得动已经写下的内容');
        await page.screenshot({ path: path.join(output, 'mcp-editor-escape.png') });

        // 确认丢弃才是真的关掉它，并且连带清掉草稿。
        await page.keyboard.press('Escape');
        await page.locator('[data-ec-action="modal-confirm"]').click();
        await page.locator('#mcp-editor-overlay').waitFor({ state: 'hidden' });
        assert.equal(await page.locator('#mcp-field-name').inputValue(), '',
            '关闭要清掉未保存的输入');

        // 空编辑器上的 Escape 就是 master 的行为：直接关，不提问。
        await openMcpEditor(page);
        await page.keyboard.press('Escape');
        await page.locator('#mcp-editor-overlay').waitFor({ state: 'hidden' });
        assert.equal(await page.locator('#ec-modal').isVisible(), false,
            '没有输入就不该提问');
    });

    await scenario('批量 JSON 逐项创建：失败即停，重试只补未完成项并复用原幂等键', async page => {
        resetEditorFixtures();
        const baseline = createWrites().length;
        createRefusals.beta = 'name_conflict';
        await openMcpEditor(page);
        await page.locator('[data-mcp-tab="json"]').click();
        await page.fill('#mcp-field-json', JSON.stringify({ mcpServers: {
            alpha: { command: 'uvx', args: ['alpha-server'] },
            beta: { command: 'uvx', args: ['beta-server'] },
            gamma: { command: 'uvx', args: ['gamma-server'] },
        } }, null, 2));

        await page.locator('#mcp-editor-save').click();
        // 逐项提交：alpha 提交，beta 被拒，gamma 从未提交 —— 只有两条创建请求。
        await page.locator('#mcp-test-result .fa-circle-xmark').waitFor({ state: 'visible' });

        const first = createWrites().slice(baseline);
        assert.deepEqual(first.map(entry => entry.body.name), ['alpha', 'beta'],
            '失败即停：被拒项之后的内容不得继续提交');
        assert.equal(first.every(entry => entry.idempotencyKey), true,
            '每条创建都必须带自己的幂等键');
        assert.equal(new Set(first.map(entry => entry.idempotencyKey)).size, 2,
            '两个条目不得共用一个幂等键');
        assert.deepEqual(Object.keys(first[0].body.config).sort(),
            ['args', 'command', 'transport'], 'stdio 只提交命令、参数与传输方式');
        assert.equal(first[0].body.secrets, undefined,
            '没有输入凭据就不提交任何秘密组');
        assert.equal(await page.locator('#mcp-editor-overlay').isVisible(), true,
            '部分成功不得关闭编辑器，未完成项还在里面');
        assert.match(await page.locator('#mcp-test-result').innerText(), /beta/,
            '逐项结果要指出哪一项没成功');
        await page.screenshot({ path: path.join(output, 'mcp-editor-batch-partial.png') });

        // 部分保存刷新了目录；编辑器必须仍是同一个会话，否则重试会被静默拒绝。
        assert.ok(report.requests.filter(entry =>
            entry.pathname === '/api/external-connections/catalog').length >= 2,
        '部分保存后目录要重新读取');

        // 重试：已确认的 alpha 不再提交；被拒项与后续项各自带键重发。
        delete createRefusals.beta;
        await page.locator('#mcp-editor-save').click();
        await page.locator('#mcp-editor-overlay').waitFor({ state: 'hidden' });

        const all = createWrites().slice(baseline);
        assert.deepEqual(all.map(entry => entry.body.name),
            ['alpha', 'beta', 'beta', 'gamma'],
            '重试只补未完成项，不重发已确认的 alpha');
        // 输入未变：被拒项沿用原键，服务端才能把重试对上同一次提交。
        assert.equal(all[2].idempotencyKey, all[1].idempotencyKey,
            '未修改的被拒项要沿用原幂等键');
        assert.equal(new Set(all.map(entry => entry.idempotencyKey)).size, 3,
            '三个条目各自一个键，且 gamma 不是借来的');
    });

    await scenario('编辑草稿测试引用连接 ID 与版本，没重填的请求头不外发', async page => {
        resetEditorFixtures();
        const drafts = report.draftTests.length;
        await page.locator('[data-ec-action="open"][data-mode="edit"]').first().click();
        await page.locator('#mcp-editor-overlay').waitFor({ state: 'visible' });

        // The name is the row's identity: an edit does not rename it.
        assert.equal(await page.locator('#mcp-field-name').isDisabled(), true,
            '编辑草稿的稳定 ID 来自行本身，名称不可改');
        assert.equal(await page.locator('#mcp-field-name').inputValue(), 'OneAgent HTTP MCP');
        assert.equal(await page.locator('#mcp-advanced-fields').isVisible(), true,
            '已保存的超时与前缀要展开可见');

        // 只改超时，认证请求头不重新输入。
        await page.fill('#mcp-field-timeout', '45');
        await page.locator('#mcp-editor-test').click();
        await page.locator('#mcp-test-result.ok').waitFor({ state: 'visible' });

        const probe = report.draftTests[drafts];
        assert.ok(probe, '测试必须走草稿测试路由');
        assert.equal(probe.body.connection_id, 'mcp-1', '编辑草稿要引用稳定连接 ID');
        assert.equal(probe.body.expected_version, 6, '并且绑定到读取时的版本');
        assert.equal(probe.body.config.timeout, 45, '测试用的是本次改动的配置');
        assert.equal(probe.body.secrets, undefined,
            '没有重新输入凭据就不得发送 secrets：服务端按整组保留解析');
        assert.match(await page.locator('#mcp-test-result').innerText(), /连接正常|工具/,
            '测试结果来自服务端的探测结论');

        await page.locator('#mcp-editor-save').click();
        await page.locator('#mcp-editor-overlay').waitFor({ state: 'hidden' });
        const write = report.writes[report.writes.length - 1];
        assert.match(write.pathname, /\/api\/external-connections\/tenant\/mcp-1\/update$/);
        assert.equal(write.body.config.timeout, 45, '保存提交新的超时');
        assert.equal(write.body.expected_version, 6, '保存也带版本，避免覆盖并发改动');
        assert.equal(write.body.secrets, undefined, '未触碰的凭据组不得被空组覆盖');
        assert.deepEqual(write.body.config.header_names, ['Authorization'],
            '未重填的请求头声明要随配置保留，否则保留下来的凭据就再也用不上');
        await page.screenshot({ path: path.join(output, 'mcp-editor-edit-save.png') });
    });

    await scenario('测试失败后仍可保存：仍然保存只跳过探测，不绕过校验', async page => {
        resetEditorFixtures();
        draftOutcome = 'fail';
        const baseline = createWrites().length;
        await openMcpEditor(page);
        await page.fill('#mcp-field-name', '探测失败的 MCP');
        await chooseTransport(page, 'streamable-http');
        await page.fill('#mcp-field-url', 'http://127.0.0.1:9100/mcp');

        await page.locator('#mcp-editor-save').click();
        await page.locator('#mcp-test-result .fa-circle-xmark').waitFor({ state: 'visible' });
        assert.equal(createWrites().length, baseline, '探测失败不得提交创建');
        assert.match(await page.locator('#mcp-test-result').innerText(), /连接正常|失败|连接被拒绝|connection_refused/,
            '失败原因要照实呈现');
        assert.equal(await page.locator('#mcp-editor-save').innerText(), '仍然保存',
            '失败后保存按钮要变成明确的“仍然保存”');
        await page.screenshot({ path: path.join(output, 'mcp-editor-save-anyway.png') });

        draftOutcome = 'ok';
        await page.locator('#mcp-editor-save').click();
        await page.locator('#mcp-editor-overlay').waitFor({ state: 'hidden' });
        const write = createWrites().slice(baseline).pop();
        assert.equal(write.body.name, '探测失败的 MCP', '仍然保存把合法配置照常提交');
        assert.equal(write.body.config.url, 'http://127.0.0.1:9100/mcp');
        assert.equal(write.body.secrets, undefined, '没有输入凭据就不提交任何秘密组');

        // 字段错误仍然被拒：跳过探测不等于绕过校验。
        await openMcpEditor(page);
        await page.locator('#mcp-editor-save').click();
        await page.locator('#mcp-test-result.fail').waitFor({ state: 'visible' });
        assert.equal(createWrites().length, baseline + 1, '没有名字的草稿不得提交');
        await page.locator('#mcp-editor-cancel').click();
        await page.locator('#mcp-editor-overlay').waitFor({ state: 'hidden' });
    });

    await scenario('脏编辑器取消时先问，问题要盖在编辑器之上', async page => {
        resetEditorFixtures();
        await openMcpEditor(page);
        await page.fill('#mcp-field-name', '还没保存的名字');

        await page.locator('#mcp-editor-cancel').click();
        await page.locator('#ec-modal').waitFor({ state: 'visible' });
        assert.match(await page.locator('#ec-modal-panel').innerText(), /不会保存|未保存/,
            '取消要先问，不能直接丢弃输入');
        // The master overlay sits at z-index 200: a question asked over it has
        // to be lifted, or the user is asked something they cannot see.
        const raised = await page.evaluate(() => {
            const modal = document.getElementById('ec-modal');
            const overlay = document.getElementById('mcp-editor-overlay');
            return { cls: modal.className,
                modalZ: getComputedStyle(modal.closest('.ec-modal') || modal).zIndex,
                overlayZ: getComputedStyle(overlay).zIndex };
        });
        assert.match(raised.cls, /ec-modal-raised/, '未保存提示要抬到编辑器之上');
        assert.ok(Number(raised.modalZ) > Number(raised.overlayZ),
            '提示的层要高于编辑器：' + JSON.stringify(raised));

        await page.locator('[data-ec-action="modal-close"]').click();
        assert.equal(await page.locator('#mcp-editor-overlay').isVisible(), true,
            '选择继续编辑时编辑器必须还在');
        assert.equal(await page.locator('#mcp-field-name').inputValue(), '还没保存的名字',
            '输入不得被丢弃');

        await page.locator('#mcp-editor-cancel').click();
        await page.locator('[data-ec-action="modal-confirm"]').click();
        await page.locator('#mcp-editor-overlay').waitFor({ state: 'hidden' });
    });

    await scenario('窄屏（360px）新增 MCP 仍完整可用', async page => {
        resetEditorFixtures();
        await openMcpEditor(page);
        const panel = await page.locator('#mcp-editor-overlay > div').boundingBox();
        assert.ok(panel.width <= 360, '编辑器不得溢出 360px 视口：' + panel.width);
        const save = await page.locator('#mcp-editor-save').boundingBox();
        assert.ok(save.x >= 0 && save.x + save.width <= 361, '保存按钮必须在视口内');
        await page.fill('#mcp-field-name', '窄屏 MCP');
        await page.fill('#mcp-field-command', 'uvx');
        await page.screenshot({ path: path.join(output, 'mcp-editor-narrow-360.png') });
        await page.locator('#mcp-editor-cancel').click();
        await page.locator('[data-ec-action="modal-confirm"]').click();
        await page.locator('#mcp-editor-overlay').waitFor({ state: 'hidden' });
    }, { width: 360, height: 780 });

    await scenario('深色主题下新增 MCP 仍完整可用', async page => {
        resetEditorFixtures();
        assert.equal(await page.evaluate(
            () => document.documentElement.classList.contains('dark')), true,
            '深色偏好要在首次绘制前落到根元素上');
        await openMcpEditor(page);
        await page.fill('#mcp-field-name', '深色 MCP');
        await chooseTransport(page, 'streamable-http');
        await page.fill('#mcp-field-url', 'http://127.0.0.1:9100/mcp');
        // The credentials field is the one group whose visibility depends on the
        // transport, so it is the useful thing to check under the other theme.
        assert.equal(await page.locator('#mcp-url-fields').isVisible(), true);
        assert.equal(await page.locator('#mcp-stdio-fields').isVisible(), false);
        await page.screenshot({ path: path.join(output, 'mcp-editor-dark.png') });
        await page.locator('#mcp-editor-cancel').click();
        await page.locator('[data-ec-action="modal-confirm"]').click();
        await page.locator('#mcp-editor-overlay').waitFor({ state: 'hidden' });
    }, { theme: 'dark' });

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
