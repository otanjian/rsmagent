// Browser acceptance for the 能力 tab's keyword box, which narrows both the
// skill list and the tool catalogue. Shipped HTML/CSS/JS with in-memory backend
// fixtures only (no real Agent, skill or session is touched).
// Run: node openspec/changes/add-agent-capability-search/evidence/serve-fixture.cjs
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const repo = path.resolve(__dirname, '../../../..');
const webRoot = path.join(repo, 'channel/web');
const staticRoot = path.join(webRoot, 'static');

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
const pageHtml = expandIncludes(fs.readFileSync(path.join(webRoot, 'chat.html'), 'utf8'))
    .replaceAll('{{COW_DEFAULT_LANG}}', 'zh')
    .replaceAll('{{COW_NAVIGATION_MODE}}', 'classic');

// --- backend fixture --------------------------------------------------------
const skills = [
    { name: 'knowledge-wiki', display_name: 'Knowledge Wiki', description: 'Manage the personal knowledge wiki: file articles and keep them organised.' },
    { name: 'image-generation', display_name: 'Image Generation', description: 'Generate or edit images from text prompts.' },
    { name: 'rfq-quote', display_name: '图纸询价智能报价', description: '处理钣金、机柜、焊接结构件的客户询价，提取图纸参数并核算成本。' },
    { name: 'data-analysis', display_name: '数据分析', description: '对业务数据做统计、归因与可视化。' },
    { name: 'pdf-tools', display_name: 'PDF 工具', description: '合并、拆分、压缩 PDF 文件。' },
    { name: 'web-search', display_name: '联网搜索', description: '检索公开网页并总结要点。' },
    { name: 'excel-report', display_name: '财务报表', description: '生成财务报表 Excel 并做同比环比分析。' },
    { name: 'translation', display_name: '翻译助手', description: '中文、英文、日文互译。' },
    { name: 'code-review', display_name: '代码审查', description: '审查代码变更并给出修改建议。' },
    { name: 'meeting-notes', display_name: '会议纪要', description: '整理会议纪要并抽取待办事项。' },
    { name: 'email-draft', display_name: '邮件起草', description: '起草商务邮件与回复。' },
    { name: 'sql-helper', display_name: 'SQL 助手', description: '编写与优化 SQL 查询。' },
];

const tools = [
    { name: 'read', description: 'Read or inspect file contents.' },
    { name: 'write', description: 'Write content to a file, overwriting the existing file.' },
    { name: 'edit', description: 'Replace an exact string inside a file.' },
    { name: 'bash', description: 'Run a shell command in the workspace.' },
    { name: 'web_search', description: 'Search the public web and summarise the results.' },
    { name: 'scheduler', description: 'Create and manage scheduled tasks.' },
    { name: 'requirements_delivery', description: 'Deliver the requirement document.', requires_explicit_binding: true },
];

const agents = [
    {
        id: 'cap-search-demo', name: '智能办公助理', description: '负责日常办公与业务问答',
        avatar: null, can_chat: true, unavailable_reason: null, is_default: true, agent_type: 'normal',
        is_tenant_default: true, position: 'Rock的专属办公助理', category: '', tags: [], revision: 'fixture',
        skills: ['knowledge-wiki', 'image-generation', 'rfq-quote', 'data-analysis'],
        sops: [], tools_allowlist: null, tools_denylist: [],
    },
    {
        id: 'all-skills-demo', name: '企业知识官', description: '只依据企业知识库回答',
        avatar: null, can_chat: true, unavailable_reason: null, is_default: false, agent_type: 'normal',
        position: '', category: '', tags: [], revision: 'fixture',
        skills: null, sops: [], tools_allowlist: null, tools_denylist: [],
    },
];

const fixture = {
    '/auth/check': { status: 'success', identity_mode: 'database', auth_required: true, authenticated: true,
        user: { username: 'cap-search-acceptance', display_name: '验收账号', roles: ['admin'], is_admin: true } },
    '/auth/me': { status: 'success', identity_mode: 'database', auth_required: true, authenticated: true,
        user: { username: 'cap-search-acceptance', display_name: '验收账号', roles: ['admin'], is_admin: true },
        current_tenant: { id: 'fixture-tenant', name: '验收租户', code: 'fixture' },
        tenants: [{ id: 'fixture-tenant', name: '验收租户', code: 'fixture' }] },
    '/auth/context': { status: 'success', is_tenant_admin: true, authorization_mode: 'all',
        permissions: ['agent.read', 'agent.write', 'agent.use', 'chat.use'], console_pages: {} },
    '/config': { status: 'success', title: '容大AI', model: 'fixture-model', providers: {},
        agent_permission_mode: 'workspace-write', permission_modes: ['read-only', 'workspace-write', 'full-access'] },
    '/api/version': { status: 'success', version: 'cap-search-acceptance-fixture' },
    '/api/branding/public': { enabled: false },
    '/api/platform/tenants': { status: 'success', items: [{ id: 'fixture-tenant', name: '验收租户', code: 'fixture' }] },
    '/api/knowledge/list': { status: 'success', tree: [], root_files: [] },
    '/api/projects': { status: 'success', current: null, recents: [], default_workspace: '/fixture/workspace', projects_root: '/fixture/projects' },
    '/api/history': { status: 'success', messages: [], has_more: false },
    '/api/models': { status: 'success', providers: [], models: [] },
    '/api/channels': { status: 'success', channels: [] },
    '/api/todos/summary': { status: 'success', total: 0, items: [] },
    '/api/todos': { status: 'success', items: [], has_more: false },
    '/api/appearance': { status: 'success' },
    '/api/skills': { status: 'success', skills },
    '/api/tools': { status: 'success', tools },
    '/api/scheduler': { status: 'success', tasks: [] },
    '/poll': { status: 'success', has_content: false },
};

const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
    '.png': 'image/png', '.jpg': 'image/jpeg', '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf' };

const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://fixture');
    const pathname = url.pathname;
    const json = (data, status = 200) => {
        res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
        res.end(JSON.stringify(data));
    };
    if (pathname === '/chat' || pathname === '/' || pathname === '/admin') {
        res.writeHead(200, { 'Content-Type': mime['.html'], 'Cache-Control': 'no-store' });
        res.end(pageHtml);
    } else if (pathname.startsWith('/assets/')) {
        const file = path.resolve(staticRoot, '.' + pathname.slice('/assets'.length));
        if (!file.startsWith(staticRoot + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
            json({ status: 'error', message: 'Missing fixture asset' }, 404);
        } else {
            res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream' });
            fs.createReadStream(file).pipe(res);
        }
    } else if (pathname === '/api/agents') {
        json({ status: 'success', revision: 'fixture', agents, channel_instances: [],
            user_default: { agent_id: 'cap-search-demo' }, tenant_default_manageable: true,
            default_agent_id: 'cap-search-demo',
            default_resolution: { agent_id: 'cap-search-demo', source: 'user_default' } });
    } else if (pathname === '/api/sessions') {
        json({ status: 'success', sessions: [], has_more: false, total: 0, group_mode: 'time' });
    } else if (fixture[pathname]) json(fixture[pathname]);
    else json({ status: 'success' });
});

server.listen(9908, '127.0.0.1', () => console.log('Capability-search fixture: http://127.0.0.1:9908/chat'));
