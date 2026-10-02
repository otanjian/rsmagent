// Manual isolated browser QA: node tests/support/session_history_preview.cjs
// Serves the production shell/assets with in-memory fixtures; never touches real sessions.
const fs=require('node:fs'), http=require('node:http'), path=require('node:path');
const {execFileSync}=require('node:child_process');
const repo = path.resolve(__dirname,'../..');
const webRoot = path.join(repo, 'channel/web');
const staticRoot = path.join(webRoot, 'static');
const baseHtml = execFileSync(path.join(repo,'.venv/bin/python'), ['-c',
    "from channel.web.core.template import render; print(render('chat.html'))"], {cwd:repo, encoding:'utf8'})
    .replaceAll('{{COW_DEFAULT_LANG}}','zh').replaceAll('{{COW_NAVIGATION_MODE}}','classic');
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

let sessionRows = sessions(63).map((s,i) => ({...s,
    title: i === 2 ? '跨部门团队讨论：财务与项目实施的长标题验收' : `历史验收会话 ${String(i+1).padStart(2,'0')}`,
    last_active: 1790900000-i*600,
    project: i%3 === 0 ? {path:'/fixture/project', name:'验收项目'} : null,
    participants: i === 2 ? [...normalAgents, {id:'four',name:'规划'}, {id:'five',name:'审校'}, {id:'six',name:'研究'}] : s.participants
}));
const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://fixture');
    const pathname = url.pathname;
    const flag = !url.searchParams.has('classic');
    const json = (data, status = 200) => {
        res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
        res.end(JSON.stringify(data));
    };
    if (pathname === '/chat' || pathname === '/' || pathname === '/admin') {
        res.writeHead(200, { 'Content-Type': mime['.html'], 'Cache-Control': 'no-store' });
        res.end(pageHtml(flag).replace('<head>', '<head>' + (url.searchParams.has('dark') ? `<script>localStorage.setItem('cow_theme','dark')</script>` : `<script>localStorage.setItem('cow_theme','light')</script>`)));
    } else if (pathname.startsWith('/assets/')) {
        const file = path.resolve(staticRoot, '.' + pathname.slice('/assets'.length));
        if (!file.startsWith(staticRoot + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
            json({ status: 'error', message: 'Missing fixture asset' }, 404);
        } else {
            res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream', 'Cache-Control':'no-store' });
            fs.createReadStream(file).pipe(res);
        }
    } else if (pathname === '/api/agents' && url.searchParams.get('view') === 'workbench') {
        json({ status: 'success', agents: workbenchAgents });
    } else if (pathname === '/api/agents') {
        json({ status: 'success', revision: 'fixture', agents: workbenchAgents, channel_instances: [],
            user_default: { agent_id: 'default' }, tenant_default_manageable: true });
    } else if (pathname === '/api/sessions') {
        const q = url.searchParams.get('q') || '';
        const archived = url.searchParams.get('archived') === '1';
        const rows = sessionRows.filter(s => !!s.archived === archived && s.title.includes(q));
        const page = Number(url.searchParams.get('page') || 1), size = Number(url.searchParams.get('page_size') || 50);
        json({status:'success', sessions:rows.slice((page-1)*size,page*size), has_more:page*size<rows.length, total:rows.length, group_mode:'project', query:q});
    } else if (pathname.startsWith('/api/sessions/') && req.method === 'PUT') {
        let body=''; req.on('data', chunk => body+=chunk); req.on('end',()=>{
            const row=sessionRows.find(s=>s.session_id===decodeURIComponent(pathname.split('/')[3]));
            if(row) Object.assign(row,JSON.parse(body||'{}'));
            json({status:'success'});
        });
    } else if (fixture[pathname]) json(fixture[pathname]);
    else json({ status: 'success' });
});


server.listen(0,"127.0.0.1",()=>console.log("History fixture port",server.address().port));
