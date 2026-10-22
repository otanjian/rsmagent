// Actual WSGI/IAM/storage and rendered master components; no HTTP mocks.
// Model/tool acceptance is recorded separately and is never replaced by this fixture.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('playwright');
const repo = path.resolve(__dirname, '..');
const output = path.resolve(process.env.COW_SYNC_BROWSER_OUTPUT || fs.mkdtempSync(path.join(os.tmpdir(), 'sync-web-browser-')));
fs.mkdirSync(output, {recursive:true});
const report = {scenarios:[], pageErrors:[], scope:'actual Web browser, IAM, files and persistence; no model substitute'};

async function main() {
    const child = spawn(process.env.PYTHON_BIN || path.join(repo,'.venv/bin/python'),
        ['-u','tests/fixtures/sync-master-web-server.py'], {cwd:repo, stdio:['pipe','pipe','pipe']});
    let stderr='', browser, sequence=0, resolveReady, rejectReady;
    const ready = new Promise((resolve,reject)=>{resolveReady=resolve;rejectReady=reject;});
    const pending = new Map();
    child.stderr.on('data',data=>{stderr+=data;});
    child.on('exit',code=>{if(code){const error=new Error(stderr);rejectReady(error);pending.forEach(p=>p.reject(error));}});
    createInterface({input:child.stdout}).on('line',line=>{
        if(!line.startsWith('SYNC ')){stderr+=line+'\n';return;}
        const data=JSON.parse(line.slice(5));
        if(data.ready) resolveReady(data);
        else {const p=pending.get(data.id);if(p){pending.delete(data.id);data.error?p.reject(new Error(data.error)):p.resolve(data.result);}}
    });
    const command=(action,fields={})=>new Promise((resolve,reject)=>{
        const id=String(++sequence);pending.set(id,{resolve,reject});
        child.stdin.write(JSON.stringify({id,action,...fields})+'\n');
    });
    let currentPage;
    async function scenario(name,fn) {
        try{await fn();report.scenarios.push({name,passed:true});}
        catch(error){report.scenarios.push({name,passed:false,message:error.message});
            if(currentPage) await currentPage.screenshot({path:path.join(output,'failure.png')});throw error;}
    }
    try {
        const app=await ready;
        try{browser=await chromium.launch();}catch(_){browser=await chromium.launch({channel:'chrome'});}
        async function login(name) {
            const context=await browser.newContext({viewport:{width:1440,height:1000},locale:'zh-CN'});
            await context.addInitScript(()=>{if(window.top===window) localStorage.setItem('cow_lang','zh');});
            const page=await context.newPage();page.setDefaultTimeout(20000);
            page.on('pageerror',e=>report.pageErrors.push(e.message));
            currentPage=page;await page.goto(app.origin+'/chat');
            await page.locator('#login-username').fill(name);
            await page.locator('#login-password').fill(app.password);
            await page.locator('#login-btn').click();
            await page.locator('#app').waitFor({state:'visible'});
            await page.waitForFunction(()=>typeof menuLoaded !== 'undefined' && menuLoaded && !!activeAgentId);
            return page;
        }
        const alice=await login('alice'), second=await login('alice'), bob=await login('bob');
        await scenario('sync: todos work by default with persistent personal creation and completion',async()=>{
            const owner=await login('todoalice'), other=await login('todobob');
            currentPage=owner;
            await owner.locator('#todo-bell-btn').waitFor({state:'visible'});
            assert.equal(await owner.locator('#todo-bell-btn').count(),1);
            assert.equal(await owner.locator('#todo-bell-btn').evaluate(el=>el.nextElementSibling.id),'theme-toggle');
            await owner.locator('#todo-bell-btn').click();
            await owner.locator('#todo-empty').waitFor({state:'visible'});
            assert.equal(await owner.locator('#todo-bell-badge').isVisible(),false);
            assert.equal(await owner.locator('#todo-banner').isVisible(),false);
            await owner.locator('#todo-create-btn').click();
            await owner.locator('#todo-edit-field-title').fill('Default-enabled personal todo');
            const created=owner.waitForResponse(r=>new URL(r.url()).pathname==='/api/todos'&&r.request().method()==='POST');
            await owner.locator('#todo-edit-submit').click();
            const response=await created;assert.equal(response.status(),200);
            const {item}=await response.json();
            const row=owner.locator('#todo-list [data-id="'+item.id+'"]');
            await row.waitFor({state:'visible'});
            await owner.waitForFunction(()=>document.getElementById('todo-bell-badge')?.textContent==='1');
            await owner.goto(app.origin+'/history');
            await owner.locator('#view-history').waitFor({state:'visible'});
            await owner.locator('#todo-bell-badge').waitFor({state:'visible'});
            assert.equal(await owner.locator('#todo-bell-badge').innerText(),'1');
            await owner.screenshot({path:path.join(output,'todo-bell-history.png')});
            await owner.locator('#todo-bell-btn').click();
            await row.waitFor({state:'visible'});
            await owner.reload();await row.waitFor({state:'visible'});
            assert.equal(await owner.locator('#todo-banner').isVisible(),false);
            await other.goto(app.origin+'/todos');
            await other.locator('#todo-empty').waitFor({state:'visible'});
            assert.equal(await other.locator('#todo-list [data-id="'+item.id+'"]').count(),0);
            assert.equal(await other.evaluate(async id=>(await fetch('/api/todos/'+id)).status,item.id),404);
            await row.click();
            const completed=owner.waitForResponse(r=>new URL(r.url()).pathname==='/api/todos/'+item.id&&r.request().method()==='PATCH');
            owner.once('dialog',dialog=>dialog.accept());
            await owner.locator('#todo-detail-actions button[onclick*="completed"]').click();
            const result=await completed;assert.equal(result.status(),200);
            assert.equal((await result.json()).item.status,'completed');
            await owner.locator('#todo-bell-badge').waitFor({state:'hidden'});
            assert.equal(await owner.locator('#todo-bell-btn').isVisible(),true);
            await owner.locator('#todo-detail-overlay .todo-detail-close').click();
            await owner.reload();
            await owner.locator('.todo-status-tab[data-status="completed"]').click();
            await row.waitFor({state:'visible'});
            await owner.screenshot({path:path.join(output,'todo-enabled-default.png')});
            await owner.context().close();await other.context().close();
            currentPage=alice;
        });
        const readMenu=p=>p.evaluate(async()=>{const r=await fetch('/api/menu');return r.json();});
        async function assertHeaderShortcutsHidden(page) {
            for (const selector of ['#docs-link', 'a[title="Website"]', 'a[title="GitHub"]']) {
                assert.equal(await page.locator(selector).isVisible(),false);
            }
            assert.equal(await page.locator('#lang-selector').isVisible(),true);
            assert.equal(await page.locator('#theme-toggle').isVisible(),true);
        }
        async function openPersonalMenu(page) {
            await page.locator('#sidebar-account-toggle').click();
            assert.equal(await page.locator('#source-update-open').isVisible(),false);
            await page.locator('#account-menu-customize').click();
            await page.locator('#menu-editor-overlay').waitFor({state:'visible'});
            assert.equal(await page.locator('#sidebar-account-menu').isVisible(),false);
            assert.equal(await page.locator('body').evaluate(el=>el.classList.contains('account-menu-sheet-open')),false);
            assert.equal(await page.locator('#menu-editor-body input').first().evaluate(el=>el===document.activeElement),true);
            assert.equal(await page.locator('#sidebar-nav .menu-group-edit, #sidebar .sidebar-footer [onclick*="menuEditorOpen"]').count(),0);
        }
        async function editTitle(page,title,success=true) {
            currentPage=page;
            await openPersonalMenu(page);
            await page.locator('.me-group[data-gid="chat"] [data-field="group-title"]').fill(title);
            const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/menu'&&r.request().method()==='POST');
            await page.locator('#menu-editor-save').click();const result=await response;
            assert.equal(result.ok(),success);
            if(success)await page.locator('#menu-editor-overlay').waitFor({state:'hidden'});
            else assert.ok(await page.locator('#menu-editor-error').innerText());
        }
        await scenario('sync: header language reflects the personal choice without changing instance configuration',async()=>{
            currentPage=alice;let globalWrites=0;
            const observe=r=>{if(new URL(r.url()).pathname==='/config'&&r.method()==='POST')globalWrites++;};
            alice.on('request',observe);
            assert.equal(await alice.locator('#lang-label').innerText(),'简');
            await assertHeaderShortcutsHidden(alice);
            await alice.locator('#lang-selector > button').click();
            await alice.locator('#lang-menu [data-lang="en"]').click();
            await alice.waitForFunction(()=>document.getElementById('lang-label').textContent==='EN');
            await assertHeaderShortcutsHidden(alice);
            assert.equal(await alice.locator('#sidebar-brand-caption').innerText(),'Business Workspace');
            await alice.locator('#lang-selector > button').click();
            await alice.locator('#lang-menu [data-lang="zh-Hant"]').click();
            await alice.waitForFunction(()=>document.getElementById('lang-label').textContent==='繁');
            await assertHeaderShortcutsHidden(alice);
            await alice.locator('#lang-selector > button').click();
            await alice.locator('#lang-menu [data-lang="zh"]').click();
            await alice.waitForFunction(()=>document.getElementById('lang-label').textContent==='简');
            await assertHeaderShortcutsHidden(alice);
            assert.equal(globalWrites,0);alice.off('request',observe);
        });
        await scenario('sync: agent conversation entry preserves centered welcome and visible composer',async()=>{
            const entry=await login('alice');currentPage=entry;
            async function checkLayout(screenshot) {
                await entry.locator('#welcome-screen').waitFor({state:'visible'});
                await entry.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
                if(screenshot)await entry.screenshot({path:path.join(output,screenshot+'.png')});
                const main=await entry.locator('#chat-main').boundingBox();
                for(const selector of ['#welcome-screen > img','#welcome-title','#welcome-screen > .grid']) {
                    const box=await entry.locator(selector).boundingBox();
                    assert.ok(Math.abs(box.x+box.width/2-main.x-main.width/2)<2,selector+' stays centered in the conversation pane');
                }
                const composer=await entry.locator('#chat-composer').boundingBox();
                assert.ok(composer.y+composer.height<=entry.viewportSize().height+1,'composer stays inside the viewport');
                assert.ok(await entry.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'page has no horizontal overflow');
            }
            await checkLayout();
            const brandBox=await entry.locator('#sidebar .sidebar-brand').boundingBox();
            const launchBox=await entry.locator('#sidebar-new-chat').boundingBox();
            const menuBox=await entry.locator('#sidebar-nav').boundingBox();
            assert.ok(launchBox.y>=brandBox.y+brandBox.height && launchBox.y+launchBox.height<=menuBox.y,'new chat stays between brand and navigation');
            const initialSession=await entry.evaluate(()=>sessionId);
            await entry.locator('#sidebar-new-chat').click();
            await entry.waitForFunction(previous=>sessionId!==previous,initialSession);
            assert.equal(await entry.locator('#chat-input').evaluate(el=>el===document.activeElement),true);
            await entry.locator('#sidebar-new-chat-caret').click();
            await entry.locator('#sidebar-new-chat-menu').waitFor({state:'visible'});
            await entry.screenshot({path:path.join(output,'sidebar-agent-picker.png')});
            const pickedSession=await entry.evaluate(()=>sessionId);
            await entry.locator('#sidebar-new-chat-menu [onclick="startSoloChat(\'secondary\')"]').click();
            await entry.waitForFunction(previous=>sessionId!==previous && activeAgentId==='secondary',pickedSession);
            assert.equal(await entry.locator('#sidebar-new-chat-menu').isVisible(),false);
            await entry.locator('#sidebar-nav [data-view="agent-workbench"]').click();
            await entry.locator('#agent-workbench-grid [data-agent-id="secondary"]').click();
            await entry.waitForFunction(()=>currentView==='chat' && activeAgentId==='secondary');
            await entry.locator('#session-panel').waitFor({state:'visible'});
            await checkLayout('agent-conversation-history');
            await entry.locator('#welcome-screen .example-card[data-send="/help"]').click();
            assert.equal(await entry.locator('#chat-input').inputValue(),'/help');
            await entry.locator('#chat-input').fill('');
            await entry.locator('#welcome-screen .example-card').first().click();
            assert.equal(await entry.locator('#chat-input').inputValue(),await entry.locator('#welcome-screen .example-card').first().locator('p').innerText());
            await entry.locator('#chat-input').fill('');
            await entry.locator('#history-new-chat-btn').click();
            await checkLayout();
            await entry.locator('#session-toggle-btn').click();
            await checkLayout('agent-conversation-no-history');
            await entry.reload();
            await entry.locator('#sidebar-nav [data-view="chat"]').waitFor({state:'visible'});
            await checkLayout();
            await entry.setViewportSize({width:1024,height:768});
            await entry.locator('#new-chat-btn').click();
            await checkLayout();
            await entry.setViewportSize({width:390,height:844});
            await entry.locator('#session-panel-close, .session-panel-close').click();
            await entry.locator('#new-chat-btn').click();
            await checkLayout('agent-conversation-mobile');
            await entry.locator('#menu-toggle').click();
            const mobileSession=await entry.evaluate(()=>sessionId);
            await entry.locator('#sidebar-new-chat').click();
            await entry.waitForFunction(previous=>sessionId!==previous,mobileSession);
            assert.equal(await entry.locator('#menu-toggle').getAttribute('aria-expanded'),'false');
            await checkLayout();
            await entry.locator('#menu-toggle').click();
            await entry.locator('#sidebar-new-chat-caret').click();
            const mobilePick=await entry.evaluate(()=>sessionId);
            await entry.locator('#sidebar-new-chat-menu [onclick="startSoloChat(\'primary\')"]').click();
            await entry.waitForFunction(previous=>sessionId!==previous && activeAgentId==='primary',mobilePick);
            assert.equal(await entry.locator('#menu-toggle').getAttribute('aria-expanded'),'false');
            await checkLayout();
            await entry.context().close();
        });
        await scenario('sync: personal menu saves, reloads and isolates two browser identities',async()=>{
            await editTitle(alice,'Alice navigation');await second.reload();
            await second.waitForFunction(()=>typeof menuLoaded !== 'undefined' && menuLoaded);
            assert.match(await second.locator('#sidebar-nav').innerText(),/Alice navigation/i);
            assert.doesNotMatch(await bob.locator('#sidebar-nav').innerText(),/Alice navigation/i);
            await editTitle(second,'Last successful layout');await alice.reload();
            await alice.waitForFunction(()=>typeof menuLoaded !== 'undefined' && menuLoaded);
            assert.match(await alice.locator('#sidebar-nav').innerText(),/Last successful layout/i);
            assert.equal((await readMenu(bob)).menu,null);
        });
        await scenario('sync: actual menu write failure retains the committed layout and reports error',async()=>{
            await command('menu-write-failure',{enabled:true});
            try{await editTitle(alice,'Must not persist',false);}
            finally{await command('menu-write-failure',{enabled:false});}
            assert.equal((await readMenu(alice)).menu.groups.find(g=>g.id==='chat').title,'Last successful layout');
            await alice.locator('#menu-editor-overlay [onclick="menuEditorClose()"]:visible').first().click();
        });
        await scenario('sync: artifacts read real files, rename without rewriting and isolate owners',async()=>{
            currentPage=alice;await alice.goto(app.origin+'/artifacts');
            await alice.locator('.art-card').first().waitFor();
            const own=await alice.evaluate(()=>artItems.map(i=>({id:i.id,name:i.file_name})));
            const foreign=await bob.evaluate(async()=>{const r=await fetch('/api/artifacts?scope=all');return (await r.json()).items.map(i=>i.id);});
            assert.ok(own.every(item=>!foreign.includes(item.id)));
            await alice.locator('.art-card').filter({hasText:'report.md'}).click();
            await alice.locator('#art-preview-body').getByText('Persistent file body.').waitFor();
            await alice.locator('.art-name-btn').click();await alice.locator('.art-name-input').fill('Renamed report');
            await alice.locator('.art-name-input').press('Enter');
            await alice.locator('.art-name-text').filter({hasText:'Renamed report'}).waitFor();
            assert.equal((await command('file',{name:'report.md'})).body,'# Browser report\n\nPersistent file body.');
            await alice.screenshot({path:path.join(output,'artifacts.png')});
        });
        await scenario('sync: HTML preview cannot escape its sandbox and missing files stay unavailable',async()=>{
            await alice.locator('.art-card').filter({hasText:'hostile.html'}).click();
            const iframe=alice.locator('#art-preview-body iframe');await iframe.waitFor();
            assert.ok(!(await iframe.getAttribute('sandbox')).includes('allow-same-origin'));
            await alice.frameLocator('#art-preview-body iframe').locator('html[data-blocked="true"]').waitFor();
            assert.equal(await alice.evaluate(()=>window.__artifactEscaped),undefined);
            assert.equal(new URL(alice.url()).pathname,'/artifacts');
            await command('file',{name:'report.md',remove:true});await alice.reload();
            await alice.locator('.art-card').filter({hasText:'Renamed report'}).click();
            await alice.waitForFunction(()=>artSelected && artSelected.exists===false);
            assert.equal(await alice.locator('#art-btn-download').isVisible(),false);
        });
        const manager=await login('root');
        await scenario('sync: workbench and console keep separate menus through links, reload and history',async()=>{
            currentPage=manager;
            assert.equal(await manager.locator('#app').getAttribute('data-nav-area'),'workbench');
            assert.equal(await manager.locator('#sidebar-nav [data-view="config"]').count(),0);
            assert.equal(await manager.locator('#sidebar-nav [data-view="agent-workbench"]').count(),1);
            assert.deepEqual(await manager.locator('#sidebar-nav .sidebar-item').evaluateAll(items=>items.map(item=>item.dataset.view)),
                ['chat','agent-workbench','tasks','knowledge','scenes','history']);
            assert.equal(await manager.locator('#sidebar-nav .menu-group-head').count(),0);
            assert.equal(await manager.locator('#nav-return-workbench').isVisible(),false);
            async function assertBottomSwitch(id) {
                const geometry=await manager.locator(id).evaluate(button=>{
                    const nav=document.getElementById('sidebar-nav'), account=document.getElementById('sidebar-account-footer');
                    return {outside:!nav.contains(button), top:button.getBoundingClientRect().top,
                        bottom:button.getBoundingClientRect().bottom, navBottom:nav.getBoundingClientRect().bottom,
                        accountTop:account.getBoundingClientRect().top};
                });
                assert.ok(geometry.outside && geometry.top>=geometry.navBottom && geometry.bottom<=geometry.accountTop);
            }
            await assertBottomSwitch('#nav-open-admin');
            await manager.screenshot({path:path.join(output,'navigation-workbench.png')});
            await manager.locator('#nav-open-admin').click();
            await manager.waitForURL(app.origin+'/admin');
            assert.equal(await manager.locator('#app').getAttribute('data-nav-area'),'admin');
            assert.equal(await manager.locator('#sidebar-nav [data-view="chat"]').count(),0);
            await manager.locator('#admin-home-shortcuts .admin-home-shortcut').first().waitFor();
            assert.deepEqual(await manager.locator('#sidebar-nav .menu-group').evaluateAll(groups=>groups.map(g=>({
                id:g.dataset.group, views:Array.from(g.querySelectorAll('.sidebar-item'),item=>item.dataset.view)
            }))), [
                {id:'admin-overview',views:['admin-home']},
                {id:'agent-dev',views:['agents','skills','memory']},
                {id:'model-access',views:['config','channels','external_connections']},
                {id:'org-perm',views:['system_user','org','roles']},
                {id:'platform-ops',views:['tenant','branding','logs','audit','token_usage']},
            ]);
            assert.deepEqual(await manager.locator('#sidebar-nav .menu-group-head [data-i18n]').allTextContents(),
                ['智能体开发','模型与接入','组织与权限','平台管理']);
            assert.equal(await manager.locator('#nav-open-admin').isVisible(),false);
            await assertBottomSwitch('#nav-return-workbench');
            await manager.screenshot({path:path.join(output,'navigation-console.png')});
            await manager.locator('#sidebar-nav [data-view="config"]').click();
            await manager.waitForURL(app.origin+'/settings');await manager.reload();
            await manager.waitForFunction(()=>typeof menuLoaded!=='undefined'&&menuLoaded);
            assert.equal(await manager.locator('#app').getAttribute('data-nav-area'),'admin');
            await manager.locator('#nav-return-workbench').click();
            await manager.goBack();await manager.waitForURL(app.origin+'/settings');
            assert.equal(await manager.locator('#app').getAttribute('data-nav-area'),'admin');
            await manager.goForward();
            assert.equal(await manager.locator('#app').getAttribute('data-nav-area'),'workbench');
        });
        await scenario('sync: mixed personal layouts keep both areas and unknown items after edit and reset',async()=>{
            currentPage=manager;
            const saved=await manager.evaluate(async()=>{
                const response=await fetch('/api/menu',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({menu:{groups:[{id:'chat',title:'Mixed layout',items:[
                    {id:'chat',type:'builtin',view:'chat'},
                    {id:'config',type:'builtin',view:'config',title:'My configuration'},
                    {id:'logs',type:'builtin',view:'logs',hidden:true},
                    {id:'future',type:'builtin',view:'future-page',title:'Keep future page'},
                    {id:'external',type:'url',url:'https://example.invalid/',open:'tab',title:'My external link'}
                ]}]}})});return response.ok;
            });assert.equal(saved,true);
            await manager.reload();await manager.waitForFunction(()=>typeof menuLoaded!=='undefined'&&menuLoaded);
            await manager.locator('#sidebar-nav [data-menu-id="external"]').waitFor();
            await manager.locator('#nav-open-admin').click();
            assert.equal(await manager.locator('#sidebar-nav [data-menu-id="external"]').count(),0);
            assert.equal(await manager.locator('#sidebar-nav [data-view="config"]').innerText(),'My configuration');
            assert.equal(await manager.locator('#sidebar-nav [data-view="logs"]').count(),0);
            await editTitle(manager,'Kept across areas');
            const items=(await readMenu(manager)).menu.groups.flatMap(g=>g.items);
            assert.equal(items.find(i=>i.id==='future').title,'Keep future page');
            assert.equal(items.find(i=>i.id==='logs').hidden,true);
            assert.equal(items.find(i=>i.id==='external').url,'https://example.invalid/');
            await manager.locator('#nav-return-workbench').click();
            assert.match(await manager.locator('#sidebar-nav').innerText(),/Kept across areas/i);
            await openPersonalMenu(manager);
            await manager.locator('[onclick="menuEditorReset()"]:visible').click();
            await manager.locator('#confirm-dialog-ok').click();
            await manager.locator('#menu-editor-overlay').waitFor({state:'hidden'});
            assert.equal((await readMenu(manager)).menu,null);
            assert.equal(await manager.locator('#app').getAttribute('data-nav-area'),'workbench');
        });
        await scenario('sync: main menu labels retain rdai names in three languages',async()=>{
            currentPage=manager;await manager.locator('#nav-open-admin').click();
            const views=['agents','config','skills','memory','channels','logs'];
            for (const [lang,labels,area] of [
                ['zh',['智能体管理','模型服务','能力中心','记忆管理','消息渠道','运行日志'],'控制台'],
                ['zh-Hant',['智慧體管理','模型服務','能力中心','記憶管理','訊息管道','執行日誌'],'控制台'],
                ['en',['Agent Management','Model Services','Capability Center','Memory Management','Channels','Runtime Logs'],'Console'],
            ]) {
                await manager.locator('#lang-selector > button').click();
                await manager.locator('#lang-menu [data-lang="'+lang+'"]').click();
                for (let i=0;i<views.length;i++) {
                    assert.equal(await manager.locator('#sidebar-nav [data-view="'+views[i]+'"] [data-i18n]').innerText(),labels[i]);
                }
                assert.equal(await manager.locator('#nav-open-admin').getAttribute('aria-label'),area);
                assert.ok((await manager.title()).endsWith(area));
            }
            await manager.locator('#lang-selector > button').click();
            await manager.locator('#lang-menu [data-lang="zh"]').click();
            await manager.locator('#sidebar-nav [data-view="agents"]').click();
            assert.equal(await manager.locator('#breadcrumb-page').innerText(),'智能体管理');
            await manager.screenshot({path:path.join(output,'navigation-rdai-labels.png')});
            await manager.locator('#nav-return-workbench').click();
            for (const [view,label] of [['knowledge','知识库'],['tasks','定时任务'],['agent-workbench','智能体']]) {
                assert.equal(await manager.locator('#sidebar-nav [data-view="'+view+'"] [data-i18n]').innerText(),label);
            }
            assert.ok((await manager.title()).endsWith('业务工作台'));
        });
        await scenario('sync: workbench-only member has no console entry and denied deep links keep a way back',async()=>{
            const limited=await login('limited');currentPage=limited;
            assert.equal(await limited.locator('#nav-open-admin').isVisible(),false);
            await limited.goto(app.origin+'/settings');
            await limited.locator('#view-unavailable').waitFor({state:'visible'});
            assert.equal(new URL(limited.url()).pathname,'/settings');
            assert.equal(await limited.locator('#nav-open-admin').isVisible(),false);
            await limited.locator('#nav-return-workbench').click();
            await limited.locator('#view-chat').waitFor({state:'visible'});
            assert.equal(await limited.locator('#app').getAttribute('data-nav-area'),'workbench');
            await limited.context().close();
        });
        await scenario('sync: both navigation areas stay reachable on mobile and the collapsed sidebar',async()=>{
            currentPage=manager;
            await manager.setViewportSize({width:390,height:844});
            await manager.locator('#menu-toggle').click();
            await manager.locator('#nav-open-admin').click();
            await manager.waitForURL(app.origin+'/admin');
            assert.equal(await manager.locator('#menu-toggle').getAttribute('aria-expanded'),'false');
            await manager.locator('#menu-toggle').click();
            await manager.waitForFunction(()=>document.getElementById('sidebar').getBoundingClientRect().left>=0);
            assert.equal(await manager.locator('#sidebar-collapse-btn').isVisible(),false);
            await assertHeaderShortcutsHidden(manager);
            await manager.screenshot({path:path.join(output,'navigation-mobile.png')});
            await openPersonalMenu(manager);
            assert.equal(await manager.locator('.menu-editor-foot > button').evaluateAll(buttons => buttons.every(button => {
                const box = button.getBoundingClientRect();
                return box.left >= 0 && box.right <= innerWidth && button.scrollWidth <= button.clientWidth;
            })), true);
            await manager.screenshot({path:path.join(output,'account-menu-editor-mobile.png')});
            await manager.keyboard.press('Escape');
            assert.equal(await manager.locator('#sidebar-account-toggle').evaluate(el=>el===document.activeElement),true);
            await manager.locator('#nav-return-workbench').click();
            await manager.locator('#view-chat').waitFor({state:'visible'});
            await manager.setViewportSize({width:1440,height:1000});
            assert.equal(await manager.locator('#sidebar > .sidebar-footer').count(),0);
            const accountBox=await manager.locator('#sidebar-account-toggle').boundingBox();
            const collapseBox=await manager.locator('#sidebar-collapse-btn').boundingBox();
            assert.ok(collapseBox.x>=accountBox.x+accountBox.width);
            assert.ok(Math.abs((collapseBox.y+collapseBox.height/2)-(accountBox.y+accountBox.height/2))<1);
            await manager.screenshot({path:path.join(output,'sidebar-account-expanded.png')});
            await manager.locator('#sidebar-account-toggle').click();
            assert.equal(await manager.locator('#sidebar-account-menu').isVisible(),true);
            await manager.locator('#sidebar-collapse-btn').click();
            assert.equal(await manager.locator('#sidebar-account-menu').isVisible(),false);
            await manager.waitForFunction(()=>document.getElementById('sidebar').getBoundingClientRect().width<=72);
            const avatarBox=await manager.locator('#sidebar-account-toggle').boundingBox();
            const expandBox=await manager.locator('#sidebar-collapse-btn').boundingBox();
            assert.ok(expandBox.y>=avatarBox.y+avatarBox.height);
            assert.equal(await manager.locator('#sidebar-collapse-btn').getAttribute('aria-expanded'),'false');
            assert.match(await manager.locator('#sidebar-collapse-btn').getAttribute('aria-label'),/展开/);
            await openPersonalMenu(manager);
            await manager.keyboard.press('Escape');
            assert.equal(await manager.locator('#sidebar-account-toggle').evaluate(el=>el===document.activeElement),true);
            await manager.reload();
            await manager.locator('#sidebar-nav [data-view="chat"]').waitFor({state:'visible'});
            await assertHeaderShortcutsHidden(manager);
            await manager.waitForFunction(()=>document.getElementById('sidebar').getBoundingClientRect().width<=72);
            await manager.screenshot({path:path.join(output,'sidebar-account-collapsed.png')});
            await manager.locator('#sidebar-account-toggle').click();
            assert.equal(await manager.locator('#account-menu-customize').evaluate(el=>{
                const b=el.getBoundingClientRect();
                return el.contains(document.elementFromPoint(b.x+b.width/2,b.y+b.height/2));
            }),true);
            await manager.screenshot({path:path.join(output,'sidebar-account-collapsed-menu.png')});
            await manager.keyboard.press('Escape');
            await manager.locator('#nav-open-admin').click();
            await manager.waitForURL(app.origin+'/admin');
            await manager.locator('#nav-return-workbench').click();
            await manager.locator('#sidebar-collapse-btn').press('Enter');
            await manager.waitForFunction(()=>document.getElementById('sidebar').getBoundingClientRect().width>200);
            assert.equal(await manager.locator('#sidebar-collapse-btn').getAttribute('aria-expanded'),'true');
            await manager.locator('#sidebar-collapse-btn').press('Space');
            await manager.waitForFunction(()=>document.getElementById('sidebar').getBoundingClientRect().width<=72);
            const collapsedSession=await manager.evaluate(()=>sessionId);
            await manager.locator('#sidebar-new-chat').press('Enter');
            await manager.waitForFunction(previous=>sessionId!==previous,collapsedSession);
            await manager.locator('#sidebar-new-chat-caret').click();
            await manager.screenshot({path:path.join(output,'sidebar-agent-picker-collapsed.png')});
            const collapsedPick=await manager.evaluate(()=>sessionId);
            await manager.locator('#sidebar-new-chat-menu [onclick="startSoloChat(\'secondary\')"]').click();
            await manager.waitForFunction(previous=>sessionId!==previous && activeAgentId==='secondary',collapsedPick);
            await manager.locator('#sidebar-collapse-btn').press('Enter');
        });
        await scenario('sync: skill creation uses the rendered form and persists an authorized resource',async()=>{
            currentPage=manager;await manager.goto(app.origin+'/skills');
            await manager.locator('#skill-add-btn').click();
            await manager.locator('[data-skill-tab="create"]').click();
            await manager.locator('#skill-create-name').fill('Browser acceptance skill');
            await manager.locator('#skill-create-desc').fill('Synthetic skill for real Web acceptance');
            await manager.locator('#skill-create-body').fill('Return the requested fixture text.');
            const response=manager.waitForResponse(r=>new URL(r.url()).pathname==='/api/skills/create');
            await manager.locator('#skill-add-primary').click();
            const result=await response;const data=await result.json();
            assert.equal(data.status,'success',JSON.stringify(data));
            await manager.locator('[data-skill-name="'+data.name+'"]').waitFor();
            const catalog=await manager.evaluate(async()=>{const r=await fetch('/api/skills');return r.json();});
            const created=catalog.skills.find(s=>s.name===data.name);assert.ok(created?.resource_id);
            const read=await manager.evaluate(async skill=>{
                const r=await fetch('/api/skills/content?name='+encodeURIComponent(skill.name)+'&resource_id='+encodeURIComponent(skill.resource_id));
                return {status:r.status,body:await r.json()};
            },created);
            assert.equal(read.status,200,JSON.stringify(read));assert.match(read.body.content,/requested fixture text/);
            await manager.locator('[data-skill-name="'+data.name+'"] [data-skill-edit]').click();
            const editor=manager.locator('#skill-viewer-content textarea.doc-editor');
            await editor.waitFor();await editor.fill((await editor.inputValue())+'\n\nEdited through the Web form.\n');
            // The new area switch goes through the existing unsaved-edit guard.
            const draft=await editor.inputValue();
            await manager.locator('#nav-return-workbench').click();
            await manager.locator('#confirm-dialog-overlay').waitFor({state:'visible'});
            await manager.locator('#confirm-dialog-cancel').click();
            assert.equal(new URL(manager.url()).pathname,'/skills');
            assert.equal(await manager.locator('#app').getAttribute('data-nav-area'),'admin');
            assert.equal(await editor.inputValue(),draft);
            const saved=manager.waitForResponse(r=>new URL(r.url()).pathname==='/api/skills/content'&&r.request().method()==='POST');
            await manager.locator('#skill-btn-save').click();
            assert.equal((await (await saved).json()).status,'success');
            await manager.locator('#skill-viewer-content').getByText('Edited through the Web form.').waitFor();
            await manager.screenshot({path:path.join(output,'skills.png')});
        });
        await scenario('sync: knowledge category and document create, edit and reload through the master UI',async()=>{
            currentPage=manager;await manager.goto(app.origin+'/knowledge');
            await manager.locator('#knowledge-new-menu > button').click();
            await manager.locator('[onclick="createKnowledgeCategory(); closeKnowledgeNewMenu()"]:visible').click();
            await manager.locator('#knowledge-dialog-input').fill('browser-evidence');
            const category=manager.waitForResponse(r=>new URL(r.url()).pathname==='/api/knowledge/action');
            await manager.locator('#knowledge-dialog-submit').click();
            assert.equal((await (await category).json()).status,'success');
            await manager.locator('#knowledge-dialog-overlay').waitFor({state:'hidden'});
            await manager.locator('#knowledge-new-menu > button').click();
            await manager.locator('[onclick="createKnowledgeDocument(); closeKnowledgeNewMenu()"]:visible').click();
            await manager.locator('#knowledge-dialog-submit').click();
            await manager.locator('#knowledge-document-filename').fill('record.md');
            await manager.locator('#knowledge-document-content').fill('# Acceptance document\n\nBrowser persistence marker.');
            const created=manager.waitForResponse(r=>new URL(r.url()).pathname==='/api/knowledge/action');
            await manager.locator('#knowledge-dialog-submit').click();
            assert.equal((await (await created).json()).status,'success');
            await manager.locator('#knowledge-viewer-body').getByText('Browser persistence marker.').waitFor();
            await manager.locator('#knowledge-btn-edit').click();
            const editor=manager.locator('#knowledge-viewer-body textarea.doc-editor');
            await editor.fill('# Acceptance document\n\nEdited knowledge marker.');
            const saved=manager.waitForResponse(r=>new URL(r.url()).pathname==='/api/knowledge/action');
            await manager.locator('#knowledge-btn-save').click();assert.equal((await (await saved).json()).status,'success');
            await manager.reload();
            await manager.locator('#knowledge-tree .knowledge-tree-file[data-path="browser-evidence/record.md"]').click();
            await manager.locator('#knowledge-viewer-body').getByText('Edited knowledge marker.').waitFor();
            await manager.screenshot({path:path.join(output,'knowledge.png')});
        });
        await scenario('sync: Web service keys are one-shot, authorized and revoked on rotation',async()=>{
            currentPage=manager;await manager.goto(app.origin+'/settings');
            await manager.locator('#cfg-api-user').fill(app.users.alice);
            const created=manager.waitForResponse(r=>new URL(r.url()).pathname==='/api/external_api'&&r.request().method()==='POST');
            await manager.locator('[onclick="createServiceApiKey()"]:visible').click();
            assert.equal((await (await created).json()).status,'success');
            const key=await manager.locator('#cfg-api-token').inputValue();assert.ok(key.startsWith('sak_'));
            const directory=await manager.request.get(app.origin+'/v1/models',{headers:{Authorization:'Bearer '+key}});
            assert.equal(directory.status(),200);assert.deepEqual((await directory.json()).data,[]);
            await manager.locator('[onclick="loadExternalApi()"]:visible').click();
            await manager.waitForFunction(()=>document.getElementById('cfg-api-token').value==='');
            const rotated=manager.waitForResponse(r=>new URL(r.url()).pathname==='/api/external_api'&&r.request().method()==='POST');
            await manager.locator('#cfg-api-keys button').first().click();
            await manager.locator('#confirm-dialog-ok').click();
            assert.equal((await (await rotated).json()).status,'success');
            const revoked=await manager.request.get(app.origin+'/v1/models',{headers:{Authorization:'Bearer '+key}});
            assert.equal(revoked.status(),401);
            await manager.reload();await manager.locator('#cfg-api-user').waitFor();
            assert.equal(await manager.locator('#cfg-api-token').inputValue(),'');
        });
        await scenario('sync: master history launcher creates a scoped team before switching conversation',async()=>{
            currentPage=alice;await alice.goto(app.origin+'/chat');
            await alice.waitForFunction(()=>chatAgentCatalog?.length===2);
            await alice.locator('#session-toggle-btn').click();
            await alice.locator('#new-chat-caret').click();
            await alice.locator('#new-chat-menu [onclick="openTeamChatModal()"]:visible').click();
            for (const id of ['primary','secondary']) {
                if (!(await alice.evaluate(id=>_teamChatDraft.selected.includes(id),id))) {
                    await alice.locator('#team-chat-list .team-chat-pick[onclick="toggleTeamChatPick(\''+id+'\')"]').click();
                }
            }
            const previous=await alice.evaluate(()=>sessionId);
            await alice.locator('#team-chat-start').click();
            await alice.locator('#team-chat-modal').waitFor({state:'hidden'});
            await alice.waitForFunction(old=>sessionId!==old,previous);
            assert.deepEqual(await alice.evaluate(()=>[activeAgentId,..._sessCfg.team.members.map(m=>m.id)].sort()),['primary','secondary']);
            await alice.reload();await alice.waitForFunction(()=>_sessCfg?.team?.members?.length===1);
        });
        await scenario('sync: workspace upload, search, edit and download persist real file bytes',async()=>{
            currentPage=alice;await alice.goto(app.origin+'/chat');
            await alice.locator('#workspace-toggle-btn').click();
            await alice.locator('[data-ws-tab="files"]').click();
            await alice.waitForFunction(()=>wsCanWriteHere());
            const uploadPath=path.join(output,'browser-workspace.txt');
            fs.writeFileSync(uploadPath,'Original browser workspace bytes.');
            const box=await alice.locator('#ws-body-files').boundingBox();
            const cdp=await alice.context().newCDPSession(alice);
            const drag={x:box.x+box.width/2,y:box.y+box.height/2,data:{items:[],files:[uploadPath],dragOperationsMask:1}};
            await cdp.send('Input.dispatchDragEvent',{type:'dragEnter',...drag});
            await cdp.send('Input.dispatchDragEvent',{type:'dragOver',...drag});
            await cdp.send('Input.dispatchDragEvent',{type:'drop',...drag});
            await cdp.detach();
            await alice.waitForFunction(()=>wsUpload?.phase==='done');
            assert.deepEqual(await alice.evaluate(()=>wsUpload.failures),[]);
            await alice.locator('#ws-search-input').fill('browser-workspace');
            await alice.locator('#ws-file-list .ws-file-row').filter({hasText:'browser-workspace.txt'}).first().click();
            await alice.locator('#ws-preview-content').getByText('Original browser workspace bytes.').waitFor();
            await alice.locator('#ws-btn-edit').click();
            await alice.locator('#ws-editor').fill('Updated browser workspace bytes.');
            const saved=alice.waitForResponse(r=>new URL(r.url()).pathname==='/api/workspace/write'&&r.request().method()==='POST');
            await alice.locator('#ws-btn-save').click();assert.equal((await (await saved).json()).status,'success');
            await alice.locator('#ws-preview-content').getByText('Updated browser workspace bytes.').waitFor();
            const download=alice.waitForEvent('download');await alice.locator('#ws-btn-download').click();
            const file=await download;assert.equal(fs.readFileSync(await file.path(),'utf8'),'Updated browser workspace bytes.');
            await alice.reload();await alice.locator('#workspace-toggle-btn').click();
            await alice.locator('[data-ws-tab="files"]').click();await alice.locator('#ws-search-input').fill('browser-workspace');
            await alice.locator('#ws-file-list .ws-file-row').filter({hasText:'browser-workspace.txt'}).first().click();
            await alice.locator('#ws-preview-content').getByText('Updated browser workspace bytes.').waitFor();
        });
        await scenario('sync: tenant switches, logout and re-login restore only the selected personal layout',async()=>{
            currentPage=alice;const other=await command('second-tenant');await alice.goto(app.origin+'/chat');
            await alice.locator('#tenant-selector-btn').click();
            await alice.locator('#tenant-menu [data-tenant="'+other.tenant+'"]').click();
            await alice.waitForFunction(tenant=>typeof menuLoaded !== 'undefined' && menuLoaded && sessionStorage.getItem('cow_tenant_id')===tenant,other.tenant);
            assert.equal((await readMenu(alice)).menu,null);
            assert.doesNotMatch(await alice.locator('#sidebar-nav').innerText(),/Last successful layout/i);
            const artifacts=await alice.evaluate(async()=>{const r=await fetch('/api/artifacts?scope=all');return {status:r.status,data:await r.json()};});
            assert.equal(artifacts.status,200);assert.deepEqual(artifacts.data.items,[]);
            await alice.locator('#tenant-selector-btn').click();
            await alice.locator('#tenant-menu [data-tenant="'+app.tenant+'"]').click();
            await alice.waitForFunction(tenant=>typeof menuLoaded !== 'undefined' && menuLoaded && sessionStorage.getItem('cow_tenant_id')===tenant,app.tenant);
            assert.match(await alice.locator('#sidebar-nav').innerText(),/Last successful layout/i);
            await alice.locator('#sidebar-account-toggle').click();await alice.locator('#account-menu-logout').click();
            await alice.locator('#login-username').fill('alice');await alice.locator('#login-password').fill(app.password);
            await alice.locator('#login-btn').click();
            await alice.locator('#login-tenant-select').waitFor();
            await alice.locator('#login-tenant-select').selectOption(app.tenant);await alice.locator('#login-btn').click();
            await alice.waitForFunction(()=>typeof menuLoaded !== 'undefined' && menuLoaded && _accountAppVisible);
            assert.match(await alice.locator('#sidebar-nav').innerText(),/Last successful layout/i);
            await command('corrupt-menu');await alice.reload();await alice.waitForFunction(()=>typeof menuLoaded !== 'undefined' && menuLoaded);
            assert.equal((await readMenu(alice)).menu,null);
            assert.doesNotMatch(await alice.locator('#sidebar-nav').innerText(),/Last successful layout/i);
        });
        assert.deepEqual(report.pageErrors,[]);
    } finally {
        if(browser)await browser.close();child.stdin.end();
        await new Promise(resolve=>{if(child.exitCode!==null)return resolve();child.once('exit',resolve);});
        fs.writeFileSync(path.join(output,'results.json'),JSON.stringify(report,null,2));
        fs.writeFileSync(path.join(output,'server.log'),stderr);
        console.log('Web acceptance evidence:',output);
    }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
