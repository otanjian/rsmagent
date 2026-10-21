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
const output = process.env.COW_SYNC_BROWSER_OUTPUT || fs.mkdtempSync(path.join(os.tmpdir(), 'sync-web-browser-'));
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
        const readMenu=p=>p.evaluate(async()=>{const r=await fetch('/api/menu');return r.json();});
        async function editTitle(page,title,success=true) {
            currentPage=page;
            await page.locator('[onclick="menuEditorOpen()"]:visible').last().click();
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
            await alice.locator('#lang-selector > button').click();
            await alice.locator('#lang-menu [data-lang="en"]').click();
            await alice.waitForFunction(()=>document.getElementById('lang-label').textContent==='EN');
            assert.equal(await alice.locator('#sidebar-brand-caption').innerText(),'Business Workspace');
            await alice.locator('#lang-selector > button').click();
            await alice.locator('#lang-menu [data-lang="zh-Hant"]').click();
            await alice.waitForFunction(()=>document.getElementById('lang-label').textContent==='繁');
            await alice.locator('#lang-selector > button').click();
            await alice.locator('#lang-menu [data-lang="zh"]').click();
            await alice.waitForFunction(()=>document.getElementById('lang-label').textContent==='简');
            assert.equal(globalWrites,0);alice.off('request',observe);
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
