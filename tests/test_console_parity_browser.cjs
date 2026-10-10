// Required real-browser regressions against the assembled, shipped shell.
// Reuse the knowledge fixture's authenticated HTTP responses; never a live account.
const assert = require('node:assert/strict');

// Every 管理区 page id the console signs. An identity whose projection withholds
// all of them, with no platform/tenant bypass, is the "all admin pages
// unreadable" variant: the entry itself must be refused, not just one page.
const ADMIN_PAGE_KEYS = ['admin.agents', 'admin.skills', 'admin.memory', 'admin.models',
    'admin.channels', 'admin.external_connections', 'admin.roles', 'admin.organization',
    'admin.tenants', 'admin.branding', 'admin.logs', 'admin.audit', 'admin.token_usage'];

// Keep the granted workbench page; withhold every admin page as unavailable,
// unreadable and menu-denied, as an ordinary member sees it.
function denyEveryAdminPage(data) {
    data.authorization_mode = 'role';
    data.is_tenant_admin = false;
    data.is_platform_admin = false;
    data.console_pages = data.console_pages || {};
    data.console_pages['workbench.knowledge'] = { available: true, read_allowed: true, scope: 'self', actions: {} };
    for (const key of ADMIN_PAGE_KEYS) {
        data.console_pages[key] = { available: false, read_allowed: false, menu_denied: true,
            scope: 'tenant', reason: 'no_permission', actions: {} };
    }
    return data;
}

exports.run = async function (scenario) {
    await scenario('parity: SAP iframe navigation preserves deep shell and identity teardown', async page => {
        await page.addScriptTag({path: require('node:path').resolve(__dirname, '../Scene/sap_workbench/frontend/workbench.js')});
        await page.evaluate(() => SapWorkbench.open({view:'new-session'}));
        const dialog = page.locator('#sap-workbench-dialog');
        await dialog.waitFor({state:'visible'});
        const frame = page.frameLocator('.sap-native-frame');
        await frame.locator('#sap-next').click();
        await frame.locator('#sap-details').waitFor().catch(async error => { console.error('SAP state', await page.evaluate(() => ({phase:_accountState.phase, open:document.getElementById('sap-workbench-dialog').open, text:document.querySelector('[data-sap=notice]').textContent, frames:Array.from(document.querySelectorAll('iframe')).map(f=>f.src)})), page.frames().map(f=>f.url())); throw error; });
        assert.equal(new URL(page.url()).pathname, '/knowledge/graph');
        await page.evaluate(() => _invalidateAccountIdentity('unauthenticated'));
        assert.equal(await dialog.isVisible(), false);
        assert.equal(await page.locator('.sap-native-frame').count(), 0);
        await page.reload({waitUntil:'networkidle'});
        await page.locator('#knowledge-panel-graph').waitFor();
        assert.equal(new URL(page.url()).pathname, '/knowledge/graph');
        await page.addScriptTag({path: require('node:path').resolve(__dirname, '../Scene/sap_workbench/frontend/workbench.js')});
        await page.evaluate(() => SapWorkbench.open({view:'new-session'}));
        await page.frameLocator('.sap-native-frame').locator('#sap-next').waitFor();
        await page.locator('[data-sap="home"]').click();
        await page.waitForURL(new URL('/', page.url()).href);
        assert.equal(await page.locator('#sap-workbench-dialog').isVisible(), false);
    }, {path:'/knowledge/graph',hash:'',prepare:async page=>{
        await page.route('**/scene-assets/sap_workbench/frontend/workbench.css*', route=>route.fulfill({
            contentType:'text/css',path:require('node:path').resolve(__dirname,'../Scene/sap_workbench/frontend/workbench.css')}));
        await page.route('**/api/scenes/sap-workbench/**', route=>{
            const endpoint=new URL(route.request().url()).pathname;
            return route.fulfill({json:endpoint.endsWith('/config')
                ? {status:'success',can_manage:false,capabilities:{visual:true},config:{},checks:[],blockers:[]}
                : {status:'success',binding_id:'parity-sap',display_mode:'iframe',sap_url:'http://sap.parity.test/start',agent_id:'agent-a'}});
        });
        // Controlled SAP origin exercises the real scene iframe without accessing SAP accounts.
        await page.route('http://sap.parity.test/**', route=>route.fulfill({contentType:'text/html',body:
            new URL(route.request().url()).pathname === '/start'
                ? '<a id="sap-next" href="/details">查看详情</a>' : '<h1 id="sap-details">隔离 SAP 页面</h1>'}));
        await page.route('**/api/coding/sessions', route=>route.fulfill({status:503,json:{status:'error',message:'Fixture coding service is unavailable'}}));
    }});

    let authenticated = false, beforeLoginReads = 0;
    await scenario('parity: deep route survives login and explicit tenant selection', async page => {
        assert.equal(new URL(page.url()).pathname, '/knowledge/graph');
        await page.locator('#knowledge-panel-graph').waitFor();
        assert.equal(await page.locator('#knowledge-panel-graph').isVisible(), true);
        assert.equal(beforeLoginReads, 0, 'Anonymous shell cannot load tenant content');
        await page.reload({waitUntil:'networkidle'});
        await page.locator('#knowledge-panel-graph').waitFor();
        assert.equal(new URL(page.url()).pathname, '/knowledge/graph');
    }, {path:'/knowledge/graph',hash:'',prepare:async page=>{
        page.on('request', request=>{
            if (!authenticated && /^\/api\/(knowledge|agents|history)/.test(new URL(request.url()).pathname)) beforeLoginReads++;
        });
        await page.route('**/auth/check**', async route=>{
            const response=await route.fetch(),data=await response.json();
            if (!authenticated) {data.authenticated=false;data.user=null;}
            await route.fulfill({json:data});
        });
        await page.route('**/auth/login**', async route=>{
            const response=await route.fetch(),data=await response.json();
            authenticated=true;
            data.tenants.push({id:'second-tenant',name:'另一个租户',code:'second'});
            await route.fulfill({json:data});
        });
    },beforeReady:async page=>{
        await page.locator('#login-username').fill('knowledge-member');
        await page.locator('#login-password').fill('Fixture-only-password');
        await page.locator('#login-btn').click();
        await page.locator('#login-tenant-select').selectOption('fixture-tenant');
        assert.equal(await page.locator('#app').isVisible(), false);
        await page.locator('#login-btn').click();
    }});

    let updateStarts = [], updateChecks = 0;
    await scenario('parity: source update binds confirmation and clears it across identities', async page => {
        assert.equal(updateChecks, 0, 'Opening the page must not contact the release source');
        await page.evaluate(() => toggleAccountMenu());
        await page.locator('#source-update-open').click();
        await page.locator('#update-action-item').click();
        await page.waitForFunction(() => document.getElementById('source-update-target').textContent.includes('bbbbbbbbbbbb'));
        assert.equal(updateChecks, 1);
        await page.locator('#update-action-item').click();
        await page.locator('.confirm-btn-ok').click();
        await page.waitForFunction(() => document.getElementById('source-update-status').textContent.includes('更新完成'));
        assert.deepEqual(updateStarts, [{check_id:'a'.repeat(32)}]);
        await page.evaluate(() => checkForConsoleUpdate());
        await page.evaluate(() => startConsoleUpdate());
        await page.locator('.confirm-btn-ok').waitFor();
        await page.evaluate(() => _invalidateAccountIdentity('unauthenticated'));
        await page.locator('.confirm-btn-ok').click();
        assert.equal(updateStarts.length, 1, 'Old identity confirmation cannot start an update');
        assert.equal(await page.locator('#source-update-open').isVisible(), false);
        assert.equal(await page.locator('#source-update-target').innerText(), '');
    }, {prepare: async page => {
        for (const endpoint of ['/auth/check', '/auth/me']) {
            await page.route('**' + endpoint + '**', async route => {
                const response=await route.fetch(), data=await response.json();
                data.user.is_platform_admin=true;
                await route.fulfill({json:data});
            });
        }
        await page.route('**/api/version**', route=>route.fulfill({json:{version:'fixture',update_supported:true,install_kind:'git'}}));
        await page.route('**/api/update/check**', route=>{
            updateChecks++;
            return route.fulfill({json:{check_id:'a'.repeat(32),current_sha:'c'.repeat(40),target_sha:'b'.repeat(40),
                up_to_date:false,source:'Fixture release',update_supported:true}});
        });
        await page.route('**/api/update/start**', route=>{
            updateStarts.push(route.request().postDataJSON());
            return route.fulfill({json:{status:'success',update:{state:'queued'}}});
        });
        await page.route('**/api/update/status**', route=>route.fulfill({json:{state:'completed'}}));
    }});

    for (const mode of ['classic', 'split']) {
        let modelReads = 0;
        await scenario('parity: denied deep route preserves address (' + mode + ')', async page => {
            assert.equal(new URL(page.url()).pathname, '/settings/models');
            assert.equal(await page.locator('#nav-unavailable-title').innerText(), '无权访问');
            assert.equal(await page.locator('#app').getAttribute('data-nav-mode'), mode);
            assert.equal(await page.locator('#app').getAttribute('data-nav-area'), 'admin');
            assert.equal(modelReads, 0);
            await page.evaluate(() => navigateTo('knowledge'));
            assert.equal(new URL(page.url()).pathname, '/knowledge');
            await page.goBack(); await page.locator('#view-unavailable').waitFor();
            assert.equal(new URL(page.url()).pathname, '/settings/models');
            assert.equal(modelReads, 0);
        }, {mode, path:'/settings/models',hash:'',view:'unavailable',prepare:async page=>{
            await page.route('**/auth/context**', async route=>{
                const response=await route.fetch(),data=await response.json();
                data.console_pages['admin.models']={available:false,read_allowed:false,menu_denied:true,scope:'self'};
                await route.fulfill({json:data});
            });
            page.on('request', request=>{if(new URL(request.url()).pathname==='/api/models')modelReads++;});
        }});
    }

    // The entry itself can be refused while a registered deep path keeps its
    // address and renders the denial. Keep the single-page case above; this
    // variant withholds every 管理区 page from an ordinary member.
    for (const mode of ['classic', 'split']) {
        let modelReads = 0;
        await scenario('parity: denied admin entry keeps registered deep routes (' + mode + ')', async page => {
            assert.equal(new URL(page.url()).pathname, '/settings/models');
            assert.equal(await page.locator('#view-unavailable').isVisible(), true);
            assert.equal(await page.locator('#nav-unavailable-title').innerText(), '无权访问');
            assert.equal(await page.locator('#app').getAttribute('data-nav-mode'), mode);
            assert.equal(await page.locator('#app').getAttribute('data-nav-area'), 'admin');
            assert.equal(await page.locator('#nav-open-admin').isVisible(), false,
                'a refused entry must not be offered with every admin page unreadable');
            assert.equal(modelReads, 0);
            await page.reload({waitUntil:'networkidle'});
            await page.locator('#view-unavailable').waitFor();
            assert.equal(new URL(page.url()).pathname, '/settings/models');
            assert.equal(await page.locator('#nav-open-admin').isVisible(), false);
            assert.equal(modelReads, 0);
            await page.evaluate(() => navigateTo('knowledge'));
            await page.locator('#view-knowledge').waitFor();
            await page.goBack(); await page.locator('#view-unavailable').waitFor();
            assert.equal(new URL(page.url()).pathname, '/settings/models');
            assert.equal(await page.locator('#nav-open-admin').isVisible(), false);
            assert.equal(modelReads, 0);
        }, {mode, path:'/settings/models', hash:'', view:'unavailable', prepare:async page=>{
            await page.route('**/auth/context**', async route=>{
                const response=await route.fetch();
                await route.fulfill({json:denyEveryAdminPage(await response.json())});
            });
            page.on('request', request=>{if(new URL(request.url()).pathname==='/api/models')modelReads++;});
        }});

        let landingReads = 0;
        await scenario('parity: denied admin landing returns to the conversation (' + mode + ')', async page => {
            const landed = new URL(page.url()).pathname;
            assert.ok(landed === '/' || landed === '/chat', 'landed at the conversation entry: ' + landed);
            assert.equal(await page.locator('#view-chat').isVisible(), true);
            assert.equal(await page.locator('#nav-open-admin').isVisible(), false);
            assert.equal(landingReads, 0);
        }, {mode, path:'/admin', hash:'', view:'chat', prepare:async page=>{
            await page.route('**/auth/context**', async route=>{
                const response=await route.fetch();
                await route.fulfill({json:denyEveryAdminPage(await response.json())});
            });
            page.on('request', request=>{if(new URL(request.url()).pathname==='/api/models')landingReads++;});
        }});
    }

    let notificationPage = {status:'success',runs:[],cursor:10,has_more:false};
    await scenario('parity: scheduler observer honors identity preferences and authorized clicks', async page => {
        await page.waitForFunction(() => SchedulerNotifications.identity() !== null);
        const identityKeys = await page.evaluate(() => {
            const before = SchedulerNotifications.identity().user;
            const name = _accountState.username;
            _accountState.username = 'renamed-account';
            const renamed = SchedulerNotifications.identity().user;
            _accountState.username = name;
            return [before, renamed];
        });
        assert.deepEqual(identityKeys, ['fixture-user', 'fixture-user'], 'Partition uses stable user ID');
        const run = {run_id:'browser-web-run', status:'done', channel_type:'web', session_id:'other-session',
            agent_id:'agent-b', task_name:'Fixture scheduled task', output_preview:'Synthetic completion'};
        notificationPage = {status:'success',runs:[run, {...run,run_id:'failed',status:'error'},
            {...run,run_id:'im',channel_type:'feishu'}],cursor:13,has_more:false};
        await page.evaluate(() => SchedulerNotifications.start(SchedulerNotifications.identity()));
        await page.waitForFunction(() => window.parityNotices.length === 1);
        assert.equal(await page.evaluate(() => parityNotices[0].title), 'Fixture scheduled task');
        let deniedReads = 0;
        await page.route('**/api/history?*', async route => {
            deniedReads++; await route.fulfill({status:403,json:{status:'error',message:'Forbidden'}});
        });
        await page.evaluate(() => parityNotices[0].onclick());
        await page.waitForFunction(() => document.body.innerText.includes('无权访问'));
        assert.equal(deniedReads, 1);
        assert.equal(await page.evaluate(() => currentView), 'knowledge');
        await page.unroute('**/api/history?*');
        await page.evaluate(() => parityNotices[0].onclick());
        await page.waitForFunction(() => sessionId === 'other-session' && activeAgentId === 'agent-b');
        assert.equal(new URL(page.url()).pathname, '/');
        notificationPage = {status:'success',runs:[{...run,run_id:'pref-off'}],cursor:14,has_more:false};
        await page.evaluate(() => { taskNotifyEnabled = false; });
        await page.evaluate(() => SchedulerNotifications.start(SchedulerNotifications.identity()));
        assert.equal(await page.evaluate(() => parityNotices.length), 1);
        const readsBefore = deniedReads;
        await page.evaluate(() => { _invalidateAccountIdentity('unauthenticated'); parityNotices[0].onclick(); });
        assert.equal(await page.evaluate(() => SchedulerNotifications.identity()), null);
        assert.equal(deniedReads, readsBefore);
        assert.equal(await page.evaluate(() => parityNotices.every(n => n.closed)), true);
    }, {prepare: async page => {
        await page.addInitScript(() => {
            window.parityNotices = [];
            window.Notification = class {
                static permission = 'granted';
                static requestPermission() { return Promise.resolve('granted'); }
                constructor(title, options) { this.title=title; this.options=options; parityNotices.push(this); }
                close() { this.closed=true; if(this.onclose)this.onclose(); }
            };
        });
        await page.route('**/auth/context**', async route => {
            const response=await route.fetch(), data=await response.json();
            data.feature_actions={'scheduler.runs.list':{available:true}};
            await route.fulfill({json:data});
        });
        await page.route('**/api/scheduler/runs?*', route => route.fulfill({json:notificationPage}));
    }});

    await scenario('parity: two browser windows atomically claim one terminal run', async page => {
        const other = await page.context().newPage();
        await other.goto(new URL('/knowledge', page.url()).href, {waitUntil:'networkidle'});
        const claim = p => p.evaluate(async () => {
            const storage = SchedulerNotificationInternals.database(indexedDB);
            return storage.claim('fixture:alice:t1', {run_id:'concurrent-run'});
        });
        const results = await Promise.all([claim(page), claim(other)]);
        assert.equal(results.filter(Boolean).length, 1);
        assert.equal(await claim(page), false);
        const isolated = await other.evaluate(() => SchedulerNotificationInternals.database(indexedDB)
            .claim('fixture:bob:t2', {run_id:'concurrent-run'}));
        assert.equal(isolated, true);
        const preferred = await page.evaluate(async () => {
            const db = SchedulerNotificationInternals.database(indexedDB);
            await db.manualOrigin('manual-fixture', 'agent-a:task', 'owner-window');
            const run = {run_id:'manual-run', trigger:'manual', agent_id:'agent-a', task_id:'task'};
            const other = await db.claim('manual-fixture', run, 'other-window');
            const own = await db.claim('manual-fixture', run, 'owner-window');
            const consumed = await db.claim('manual-fixture', run, 'other-window');
            const originalNow = Date.now;
            let expired;
            try {
                Date.now = () => 1;
                await db.manualOrigin('manual-fixture', 'agent-a:abandoned', 'closed-window');
                Date.now = () => 31002;
                expired = await db.claim('manual-fixture', {...run,run_id:'abandoned',task_id:'abandoned'}, 'live-window');
            } finally { Date.now = originalNow; }
            return [other, own, consumed, expired];
        });
        assert.deepEqual(preferred, [null, true, false, true]);
        await other.close();
    });
    await scenario('parity: member model route renders the granted catalog', async page => {
        await page.locator('#catalog-content').waitFor();
        assert.equal(new URL(page.url()).pathname, '/settings/catalog');
        assert.equal(await page.locator('#config-panel-basic').isVisible(), false);
        assert.equal(await page.locator('#config-panel-models').isVisible(), false);
        assert.equal(await page.locator('#config-tabs').isVisible(), false);
        assert.equal(await page.locator('#catalog-content').innerText().then(text => text.includes('我的模型目录')), true);
        assert.equal(await page.locator('#source-update-open').isVisible(), false);
    }, {path:'/settings/models', hash:'', view:'config', prepare: async page => {
        await page.route('**/auth/context**', async route => {
            const response = await route.fetch(); const data = await response.json();
            data.console_pages['admin.models'] = {available:true,read_allowed:true,scope:'self',actions:{manage:false}};
            await route.fulfill({json:data});
        });
    }});

    let revoked = false, modelReads = 0;
    await scenario('parity: revoked model management restores only the member catalog', async page => {
        await page.locator('#config-panel-models').waitFor();
        revoked = true;
        const before = modelReads;
        await page.reload({waitUntil:'networkidle'});
        await page.locator('#catalog-content').waitFor();
        assert.equal(new URL(page.url()).pathname, '/settings/catalog');
        assert.equal(modelReads, before, 'Revocation must prevent management API loads');
        assert.equal(await page.locator('#config-panel-models').isVisible(), false);
    }, {path:'/settings/models',hash:'',view:'config',prepare:async page=>{
        for (const endpoint of ['/auth/check', '/auth/me']) await page.route('**'+endpoint+'**',async route=>{
            const response=await route.fetch(),data=await response.json();
            data.user.is_platform_admin=!revoked;
            await route.fulfill({json:data});
        });
        await page.route('**/auth/context**',async route=>{
            const response=await route.fetch(),data=await response.json();
            data.is_platform_admin=!revoked;
            data.console_pages['admin.models']={available:true,read_allowed:true,scope:revoked?'self':'platform',actions:{manage:!revoked}};
            await route.fulfill({json:data});
        });
        await page.route('**/api/models**',route=>{
            modelReads++;
            return route.fulfill({status:revoked?403:200,json:revoked?{status:'error'}:{status:'success',providers:{},models:[]}});
        });
    }});

    await scenario('parity: deep paths survive refresh and back', async page => {
        assert.equal(new URL(page.url()).pathname, '/knowledge/graph');
        assert.equal(await page.locator('#knowledge-panel-graph').isVisible(), true);
        await page.reload({waitUntil: 'networkidle'});
        await page.locator('#knowledge-panel-graph').waitFor();
        await page.evaluate(() => navigateTo('chat'));
        assert.equal(new URL(page.url()).pathname, '/');
        await page.goBack();
        await page.locator('#knowledge-panel-graph').waitFor();
        assert.equal(new URL(page.url()).pathname, '/knowledge/graph');
        await page.goForward();
        await page.locator('#view-chat').waitFor();
        assert.equal(new URL(page.url()).pathname, '/');
        assert.equal(await page.evaluate(() => ConsoleRoutes.parse('//evil.invalid')), null);
        assert.equal(await page.evaluate(() => ConsoleRoutes.parse('/settings/unknown')), null);
    }, {path: '/knowledge/graph', hash: ''});

    await scenario('parity: legacy links and cancelled leave preserve the target', async page => {
        assert.equal(new URL(page.url()).pathname, '/knowledge/sources');
        await page.locator('#knowledge-panel-sources').waitFor();
        // Drive the actual registered-view leave seam, preserving a real input
        // and focus. Production views register this same synchronous contract.
        await page.evaluate(() => {
            window.parityAllowLeave = false;
            registerConsoleView({id: 'knowledge', confirmLeave: () => window.parityAllowLeave});
            const input = document.createElement('input'); input.id = 'parity-draft'; input.value = 'unsaved';
            document.getElementById('knowledge-panel-sources').appendChild(input); input.focus();
            window.parityCallback = 0;
            navigateTo('chat', () => { window.parityCallback++; });
        });
        assert.equal(new URL(page.url()).pathname, '/knowledge/sources');
        assert.equal(await page.locator('#parity-draft').inputValue(), 'unsaved');
        assert.equal(await page.evaluate(() => document.activeElement.id), 'parity-draft');
        assert.equal(await page.evaluate(() => window.parityCallback), 0);
        await page.evaluate(() => { window.parityAllowLeave = true; navigateTo('chat', () => {window.parityCallback++;}); });
        assert.equal(new URL(page.url()).pathname, '/');
        assert.equal(await page.evaluate(() => window.parityCallback), 1);
        // Exercise the branding guard's asynchronous continuation contract;
        // only the dialog answer/reset are controlled by the fixture.
        const arrived = await page.evaluate(() => {
            const discard = brandingConfirmDiscard, reset = _brandingResetDraftToBaseline;
            let pending, calls = 0;
            try {
                brandingConfirmDiscard = next => { pending = next; };
                _brandingResetDraftToBaseline = () => { brandingDirty = false; };
                currentView = 'branding'; brandingDirty = true;
                navigateTo('knowledge', () => { calls++; }, {tab:'sources'});
                if (calls !== 0) throw new Error('Arrival ran before discard');
                pending();
                const first = {calls, path:location.pathname};
                currentView = 'branding'; brandingDirty = true;
                navigateTo('knowledge', () => { calls++; }, {tab:'graph'});
                const stale = pending;
                brandingDirty = false;
                navigateTo('chat');
                stale();
                return {first, calls, path:location.pathname};
            } finally { brandingDirty=false; brandingConfirmDiscard=discard; _brandingResetDraftToBaseline=reset; }
        });
        assert.deepEqual(arrived, {first:{calls:1,path:'/knowledge/sources'},calls:1,path:'/'});
    }, {path: '/chat', hash: '#/workbench/knowledge?tab=sources'});

    await scenario('parity: empty docs/graph and independent sources', async page => {
        await page.route('**/api/knowledge/list?*', route => route.fulfill({ json: {
            status: 'success', tree: [], root_files: [], stats: { pages: 0, size: 0 },
        } }));
        await page.evaluate(() => loadKnowledgeView());
        await page.waitForFunction(() => _knowledgeListState === 'ready');
        await page.locator('#knowledge-tab-graph').click();
        assert.equal(await page.locator('#knowledge-panel-docs').isVisible(), false);
        assert.equal(await page.locator('#knowledge-panel-graph').isVisible(), false);
        assert.equal(await page.locator('#knowledge-empty').isVisible(), true);
        await page.locator('#knowledge-tab-sources').click();
        await page.locator('.knowledge-source-row').first().waitFor();
        assert.equal(await page.locator('#knowledge-empty').isVisible(), false);
    });

    await scenario('parity: delayed lists preserve graph and sources selection', async page => {
        for (const tab of ['graph', 'sources']) {
            let release, started;
            const held = new Promise(resolve => { release = resolve; });
            const seen = new Promise(resolve => { started = resolve; });
            const handler = async route => {
                started(); await held;
                await route.fulfill({ json: { status: 'success', tree: [], root_files: [], stats: { pages: 1, size: 1 } } });
            };
            await page.route('**/api/knowledge/list?*', handler);
            await page.evaluate(() => loadKnowledgeView()); await seen;
            await page.locator('#knowledge-tab-' + tab).click();
            release(); await page.waitForFunction(() => _knowledgeListState === 'ready');
            assert.equal(await page.locator('#knowledge-panel-docs').isVisible(), false);
            assert.equal(await page.locator('#knowledge-panel-' + tab).isVisible(), true);
            await page.unroute('**/api/knowledge/list?*', handler);
        }
    });

    await scenario('parity: knowledge failures offer a working retry', async page => {
        const handler = route => route.fulfill({ status: 503, json: { status: 'error', message: 'Synthetic unavailable' } });
        await page.route('**/api/knowledge/list?*', handler);
        await page.evaluate(() => loadKnowledgeView());
        await page.locator('#knowledge-retry').waitFor();
        await page.unroute('**/api/knowledge/list?*', handler);
        await page.locator('#knowledge-retry').click();
        await page.locator('#knowledge-panel-docs').waitFor();
        assert.equal(await page.locator('#knowledge-retry').isVisible(), false);
    });

    await scenario('parity: slight upward scroll pauses streaming and explicit bottom resumes', async page => {
        await page.evaluate(() => navigateTo('chat'));
        await page.locator('#view-chat').waitFor();
        await page.evaluate(() => {
            messagesDiv.innerHTML = '';
            messagesDiv.style.height = '300px'; messagesDiv.style.maxHeight = '300px';
            messagesDiv.style.minHeight = '0'; messagesDiv.style.overflow = 'auto'; messagesDiv.style.flex = 'none';
            messagesDiv.style.scrollBehavior = 'auto';
            const content = document.createElement('div'); content.id = 'parity-scroll-content';
            content.style.height = '2000px'; messagesDiv.appendChild(content); scrollChatToBottom(true);
        });
        await page.waitForFunction(() => messagesDiv.clientHeight === 300 && messagesDiv.scrollHeight > 1000);
        await page.evaluate(() => scrollChatToBottom(true));
        await page.waitForFunction(() => messagesDiv.scrollTop > 500);
        await page.evaluate(() => messagesDiv.dispatchEvent(new Event('scroll')));
        const top = await page.evaluate(() => { messagesDiv.scrollTop -= 10; return messagesDiv.scrollTop; });
        await page.waitForFunction(() => !_autoScrollEnabled);
        await page.evaluate(() => {
            document.getElementById('parity-scroll-content').style.height = '2200px'; scrollChatToBottom();
        });
        assert.equal(await page.evaluate(() => messagesDiv.scrollTop), top);
        await page.evaluate(() => scrollChatToBottom(true));
        await page.waitForFunction(() => _autoScrollEnabled && messagesDiv.scrollHeight - messagesDiv.scrollTop - messagesDiv.clientHeight < 2);
        // Writing the same position emits no event. It must not swallow the next user scroll.
        await page.evaluate(() => { scrollChatToBottom(true); messagesDiv.scrollTop -= 10; });
        await page.waitForFunction(() => !_autoScrollEnabled);
        await page.evaluate(() => newChat(false));
        assert.equal(await page.evaluate(() => _autoScrollEnabled), true);
    });

    for (const colorScheme of ['light', 'dark']) {
        await scenario('parity: message wrapping and local overflow (' + colorScheme + ')', async page => {
            await page.evaluate(() => {
                navigateTo('chat'); messagesDiv.innerHTML = '';
                const long = 'x'.repeat(200);
                const bubble = document.createElement('div'); bubble.id = 'parity-bubble';
                bubble.className = 'msg-content'; bubble.style.width = '260px';
                bubble.innerHTML = '<p><a href="#">https://example.invalid/' + long + '</a></p><p><code>' + long
                    + '</code></p><pre><code>' + long + '</code></pre><div class="table-wrap"><table><tr><th>' + long + '</th></tr></table></div>';
                messagesDiv.appendChild(bubble);
            });
            const size = await page.locator('#parity-bubble').evaluate(el => ({
                width: el.clientWidth, scroll: el.scrollWidth,
                preWidth: el.querySelector('pre').clientWidth, preScroll: el.querySelector('pre').scrollWidth,
                tableWidth: el.querySelector('.table-wrap').clientWidth, tableScroll: el.querySelector('.table-wrap').scrollWidth,
            }));
            assert.ok(size.scroll <= size.width + 1, JSON.stringify(size));
            assert.ok(size.preScroll > size.preWidth, JSON.stringify(size));
            assert.ok(size.tableScroll > size.tableWidth, JSON.stringify(size));
        }, { colorScheme, viewport: { width: 390, height: 844 } });
    }
};

if (require.main === module) {
    const result = require('node:child_process').spawnSync(process.execPath,
        [require.resolve('./test_knowledge_console_browser.cjs')], {
            stdio: 'inherit', env: { ...process.env, COW_PARITY_BROWSER: '1' },
        });
    if (result.error) throw result.error;
    process.exitCode = result.status === null ? 1 : result.status;
}
