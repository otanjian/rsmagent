// Actual Web app, authorization and terminal ledger with a real Chromium client.
// OS notification display is captured; no model call, external channel or live account.
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {createInterface} = require('node:readline');
const {chromium} = require('playwright');
const repo = path.resolve(__dirname, '..');
const output = process.env.COW_NOTIFICATION_BROWSER_OUTPUT || fs.mkdtempSync(path.join(os.tmpdir(), 'cow-notifications-http-'));
const report = {scenarios:[], pageErrors:[], scope:'real WSGI, IAM, scheduler dispatch, SQLite ledger and browser; captured OS notification'};
async function main() {
    const child = spawn(process.env.PYTHON_BIN || path.join(repo,'.venv/bin/python'),
        ['-u', 'tests/fixtures/console-parity-server.py'], {cwd:repo, stdio:['pipe','pipe','pipe']});
    let stderr='', browser, counter=0, readyResolve, readyReject;
    const pending = new Map();
    const ready = new Promise((resolve,reject)=>{readyResolve=resolve;readyReject=reject;});
    child.stderr.on('data', data=>{stderr+=data;});
    child.on('exit', code=>{if(code) {const error=new Error(stderr);readyReject(error);pending.forEach(p=>p.reject(error));}});
    createInterface({input:child.stdout}).on('line', line=>{
        if(!line.startsWith('PARITY ')){stderr+=line+'\n';return;}
        const value=JSON.parse(line.slice(7));
        if(value.ready)readyResolve(value);
        else {const request=pending.get(value.id);if(request){pending.delete(value.id);request.resolve(value.result);}}
    });
    const command=(action, fields={})=>new Promise((resolve,reject)=>{
        const id=fields.id || String(++counter); pending.set(id,{resolve,reject});
        child.stdin.write(JSON.stringify({action,...fields,id})+'\n');
    });
    async function scenario(name, fn){
        try{await fn();report.scenarios.push({name,passed:true});}
        catch(error){report.scenarios.push({name,passed:false,message:error.message});throw error;}
    }
    try {
        const instance=await ready;
        try {browser=await chromium.launch();} catch (_) {browser=await chromium.launch({channel:'chrome'});}
        const context=await browser.newContext();
        await context.addCookies([{name:'cow_session',value:instance.token,url:instance.origin}]);
        await context.addInitScript(tenant=>{
            sessionStorage.setItem('cow_tenant_id',tenant);
            localStorage.setItem('cow_lang','zh');
            window.capturedNotifications=[];
            window.Notification=class {
                static permission='granted';
                constructor(title,options){this.title=title;this.options=options;capturedNotifications.push(this);}
                close(){this.onclose?.();}
            };
        },instance.tenant);
        const page=await context.newPage();
        const pages=[page];
        const prepare=async p=>{
            p.setDefaultTimeout(25000);p.on('pageerror',e=>report.pageErrors.push(e.message));
            await p.goto(instance.origin+'/scheduler/records');
            await p.locator('#app').waitFor({state:'visible'});
            await p.waitForFunction(()=>!!window.SchedulerNotifications?.identity());
            await p.evaluate(()=>{taskNotifyEnabled=true;window.parityWho=SchedulerNotifications.identity();});
        };
        const poll=async()=>Promise.all(pages.map(p=>p.evaluate(()=>SchedulerNotifications.start(window.parityWho))));
        const captures=async()=> (await Promise.all(pages.map(p=>p.evaluate(()=>capturedNotifications.map(n=>n.options.tag))))).flat();
        await prepare(page);
        await scenario('http: console-created tasks dispatch into two owned sessions',async()=>{
            for(const session of ['session-a','session-b']){
                const result=await page.evaluate(async receiver=>{
                    const response=await fetch('/api/scheduler/create',{method:'POST',headers:{'Content-Type':'application/json'},
                        body:JSON.stringify({name:receiver,enabled:true,schedule:{type:'interval',seconds:3600},
                            action:{type:'send_message',instance_id:'web',receiver,content:'isolated reminder'}})});
                    return {status:response.status,body:await response.json()};
                },session);
                assert.equal(result.status,200,JSON.stringify(result));
                assert.equal(result.body.status,'success',JSON.stringify(result));
            }
            const tick=await command('tick');
            assert.equal(tick.runs.length,2,JSON.stringify(tick));
            assert.ok(tick.runs.every(r=>r.status==='done'),JSON.stringify(tick));
            await poll();
            assert.equal((await captures()).length,2);
        });
        await scenario('http: long completion, 205 rows, reconnect and two windows',async()=>{
            const second=await context.newPage();pages.push(second);await prepare(second);
            await poll();
            await command('begin-long',{id:'long'});
            await Promise.all(pages.map(p=>p.evaluate(()=>SchedulerNotifications.stop())));
            await context.setOffline(true);
            await command('batch');
            await poll();
            assert.equal((await captures()).length,2);
            await context.setOffline(false);await poll();
            let all=await captures();
            assert.equal(all.length,207);assert.equal(new Set(all).size,207);
            assert.ok(!all.includes('bob-private')&&!all.includes('foreign-tenant'));
            await command('finish-long',{id:'long'});await poll();
            all=await captures();assert.equal(all.length,208);assert.ok(all.includes('long'));
        });
        await scenario('http: preference off consumes event and revoked grant rejects notification click',async()=>{
            await Promise.all(pages.map(p=>p.evaluate(()=>{taskNotifyEnabled=false;})));
            await command('begin-long',{id:'muted'});await command('finish-long',{id:'muted'});await poll();
            assert.ok(!(await captures()).includes('muted'));
            await Promise.all(pages.map(p=>p.evaluate(()=>{taskNotifyEnabled=true;})));await poll();
            assert.ok(!(await captures()).includes('muted'));
            await command('revoke');
            const denied=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/history'&&r.status()===403);
            await page.evaluate(()=>capturedNotifications[0].onclick());await denied;
            assert.equal(new URL(page.url()).pathname,'/scheduler/records');
            await poll();assert.equal((await captures()).length,208);
        });
        assert.deepEqual(report.pageErrors,[]);
        await page.screenshot({path:path.join(output,'notifications-http.png')});
    } finally {
        if(browser)await browser.close();
        child.stdin.end();
        await new Promise(resolve=>{if(child.exitCode!==null)return resolve();child.once('exit',resolve);});
        fs.mkdirSync(output,{recursive:true});
        fs.writeFileSync(path.join(output,'results.json'),JSON.stringify(report,null,2));
        fs.writeFileSync(path.join(output,'server.log'),stderr);
        console.log('Notification HTTP browser artifact: '+path.join(output,'results.json'));
    }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
