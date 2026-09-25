// Browser acceptance: shipped HTML/CSS/JS, in-memory backend fixtures only.
// Run: node openspec/changes/add-agent-workbench-tag-filter/evidence/serve-fixture.cjs
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
const baseHtml = expandIncludes(fs.readFileSync(path.join(webRoot, 'chat.html'), 'utf8'))
    .replaceAll('{{COW_DEFAULT_LANG}}', 'zh')
    .replaceAll('{{COW_NAVIGATION_MODE}}', 'classic');
const pageHtml = (flag) => baseHtml.replaceAll('{{COW_WORKBENCH_SIDEBAR_LAUNCH_V2}}', flag ? '1' : '');

// --- backend fixture --------------------------------------------------------
const workbenchAgents = [
    { id: 'default', name: '智能办公助理', description: '负责日常办公与业务问答', is_default: true, tags: [] },
    { id: 'business-analysis', name: '经营分析参谋', description: '先给结论，再给归因与建议，辅助经营决策。', tags: ['经营分析', '财务', '归因', '指标体系', '经营决策'] },
    { id: 'bank-reconciliation', name: '银行余额调节表自动编制', description: '对账与未达账项核对，输出可复核的结果。', tags: ['财务', '对账', '银行'] },
    { id: 'ar-reconciliation', name: '客户对账差异逐笔排查', description: '定位客户与我方台账的差异。', tags: ['销售', '对账', '经营分析'] },
    { id: 'sap', name: 'SAP 智能助手', description: 'SAP 业务协作助手', agent_type: 'coding', tags: ['ERP', '采购', '销售'] },
    ...['合同审查', '招聘管理', '薪酬核对', '生产排程', '研发协作', '质量检查', '知识问答', '渠道返利', '税务检查', '物流跟踪', '项目复盘', '超长标签兼容性测试ABCDEFGHIJKLMNOPQRSTUVWXYZ'].map((name, i) => ({id: 'sample-'+i, name, description: '按业务规则核对资料并整理结果', tags: [name, '场景智能体']})),
].map(a => ({avatar: null, can_chat: true, unavailable_reason: null, is_default: false, agent_type: 'normal', position: '', category: '', ...a}));

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
    const flag = true;
    const json = (data, status = 200) => {
        res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
        res.end(JSON.stringify(data));
    };
    if (pathname === '/chat' || pathname === '/') {
        res.writeHead(200, { 'Content-Type': mime['.html'], 'Cache-Control': 'no-store' });
        res.end(pageHtml(flag));
    } else if (pathname === '/auth/context') {
        json({ status: 'success', is_tenant_admin: true, authorization_mode: 'all', permissions: ['agent.read', 'agent.use', 'chat.use'], console_pages: {} });
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

server.listen(9907, '127.0.0.1', () => console.log('Workbench fixture: http://127.0.0.1:9907/chat'));
