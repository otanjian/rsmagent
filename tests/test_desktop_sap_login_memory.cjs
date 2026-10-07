const {test, before, after} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const {EventEmitter} = require('node:events');
const {chromium} = require('playwright');
const {SapLoginVault, SapLoginMemory, loginScopeKey, loginSubmission, sapCookie, sapLoginFormScript} = require('../desktop/dist/main/remote/sap-login-memory');
const {checkBridgeCall} = require('../desktop/dist/main/remote/host-bridge');
const scope = {server:'https://platform.example',user:'member',tenant:'team',epoch:1};
const target = {binding_id:'binding-123',sap_url:'https://sap.example/webgui/',client:'200'};
const credential = {user:'fixture-user',password:'fixture-password',client:'200',language:'ZH'};
const secret = crypto.randomBytes(32);
const cipher = {isEncryptionAvailable:()=>true,
 encryptString(value){const iv=crypto.randomBytes(12), c=crypto.createCipheriv('aes-256-gcm',secret,iv);const encrypted=Buffer.concat([c.update(value,'utf8'),c.final()]);return Buffer.concat([iv,c.getAuthTag(),encrypted]);},
 decryptString(value){const c=crypto.createDecipheriv('aes-256-gcm',secret,value.subarray(0,12));c.setAuthTag(value.subarray(12,28));return Buffer.concat([c.update(value.subarray(28)),c.final()]).toString('utf8');}};
let browser, dir;
before(async()=>{dir=fs.mkdtempSync(path.join(os.tmpdir(),'sap-login-test-'));browser=await chromium.launch({channel:'chrome',headless:true});});
after(async()=>{await browser?.close();fs.rmSync(dir,{recursive:true,force:true});});

test('scope separates platform accounts, tenants, SAP clients and origins, but survives restart',()=>{
 const key=loginScopeKey(scope,target);
 assert.equal(key,loginScopeKey({...scope,epoch:2},target));
 for(const s of [{...scope,user:'other'},{...scope,tenant:'other'},{...scope,server:'https://other.example'}]) assert.notEqual(key,loginScopeKey(s,target));
 for(const t of [{...target,client:'100'},{...target,sap_url:'https://other.example/webgui/'}]) assert.notEqual(key,loginScopeKey(scope,t));
 assert.throws(()=>loginScopeKey(scope,{...target,sap_url:scope.server}));
 assert.throws(()=>loginScopeKey(scope,{...target,sap_url:'http://sap.example/webgui/'}));
});
test('vault stores ciphertext, restores credentials after restart, forgets and fails closed',()=>{
 const key=loginScopeKey(scope,target),vault=new SapLoginVault(dir,cipher),record={version:1,updated:Date.now(),cookies:[],credentials:credential};
 vault.write(key,record);
 assert.ok(!fs.readFileSync(path.join(dir,key+'.bin')).includes(Buffer.from(credential.password)));
 assert.deepEqual(new SapLoginVault(dir,cipher).read(key),record);
 assert.throws(()=>new SapLoginVault(dir,{...cipher,isEncryptionAvailable:()=>false}).read(key),/sap_keychain_unavailable/);
 vault.forget(key);assert.equal(vault.read(key),undefined);
});
test('submission requires exact known fields, matching SAP client and bounded password',()=>{
 const body=new URLSearchParams({'sap-user':credential.user,'sap-password':credential.password,'sap-client':'200','sap-language':'ZH'}).toString();
 assert.deepEqual(loginSubmission(body,'200'),credential);
 assert.equal(loginSubmission(body,'100'),undefined);
 assert.equal(loginSubmission(body+'&sap-password=another','200'),undefined);
 assert.equal(loginSubmission('sap-password=only','200'),undefined);
 assert.equal(sapCookie({domain:'platform.example'},target.sap_url),false);
 assert.equal(sapCookie({domain:'.example'},target.sap_url),false);
 assert.equal(sapCookie({domain:'.sap.example'},target.sap_url),true);
});
test('autofill matches the SAP login form and client, preserves edits, never submits or fills another origin',async()=>{
 const page=await browser.newPage();
 await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:`<form name="loginForm"><input name="sap-user"><input name="sap-password" type="password"><input name="sap-client" value="200"><input name="sap-language" value="ZH"></form><script>window.submits=0;document.querySelector('form').onsubmit=e=>{e.preventDefault();submits++}</script>`}));
 await page.goto(target.sap_url);
 assert.equal((await page.evaluate(sapLoginFormScript(target.sap_url,credential))).filled,true);
 assert.equal(await page.locator('[name=sap-password]').inputValue(),credential.password);
 assert.equal(await page.evaluate(()=>submits),0);
 await page.locator('[name=sap-password]').fill('user-edit');
 await page.evaluate(sapLoginFormScript(target.sap_url,credential));
 assert.equal(await page.locator('[name=sap-password]').inputValue(),'user-edit');
 await page.goto('https://other.example/webgui/');
 await page.evaluate(sapLoginFormScript(target.sap_url,credential));
 assert.equal(await page.locator('[name=sap-password]').inputValue(),'');
 await page.goto(target.sap_url);await page.locator('[name=sap-client]').fill('100');
 await page.evaluate(sapLoginFormScript(target.sap_url,credential));
 assert.equal(await page.locator('[name=sap-password]').inputValue(),'');
 await page.close();
});
function fixture(){
 let current={...scope};const cookies=new EventEmitter(),jar=[];
 cookies.get=async()=>jar.slice();cookies.set=async c=>{jar.push(c)};cookies.remove=async(_url,name)=>{const i=jar.findIndex(c=>c.name===name);if(i>=0)jar.splice(i,1)};
 const wc=new EventEmitter(),frame={name:'sap-gui-'+target.binding_id,url:target.sap_url,processId:2,routingId:3,executeJavaScript:async()=>({login:true})};
 let request;
 Object.assign(wc,{id:7,mainFrame:{frames:[frame]},isDestroyed:()=>false,session:{cookies,webRequest:{onBeforeRequest:listener=>{request=listener}}}});
 const vault=new SapLoginVault(fs.mkdtempSync(path.join(dir,'manager-')),cipher);
 const manager=new SapLoginMemory(wc,vault,()=>current);
 return {manager,vault,wc,frame,jar,cookies,request:()=>request,setScope:s=>{current=s}};
}
test('native manager saves only successful login, restores cookies, forgets, and never returns secrets',async()=>{
 const f=fixture();try {
  f.jar.push({name:'SAP_SESSIONID_TEST_200',value:'cookie-fixture',domain:'sap.example',path:'/',sameSite:'lax'});
  assert.deepEqual(await f.manager.manage(target,scope,'prepare'),{enabled:false,credentialsSaved:false,error:''});
  const enabled=await f.manager.manage(target,scope,'enable');assert.equal(enabled.enabled,true);
  const post={url:target.sap_url,method:'POST',webContentsId:7,frame:f.frame,uploadData:[{bytes:Buffer.from(new URLSearchParams({'sap-user':credential.user,'sap-password':credential.password,'sap-client':'200','sap-language':'ZH'}).toString())}]};
  f.request()(post,answer=>assert.equal(answer.cancel,false));
  await f.manager.tick();assert.equal(f.vault.read(loginScopeKey(scope,target)).credentials,undefined);
  f.frame.executeJavaScript=async()=>({sap:true,login:false});f.wc.emit('did-frame-finish-load',{},false,2,3);
  await f.manager.tick();assert.deepEqual(f.vault.read(loginScopeKey(scope,target)).credentials,credential);
  const state=await f.manager.manage(target,scope,'prepare');assert.equal(state.credentialsSaved,true);assert.doesNotMatch(JSON.stringify(state),/cookie-fixture|fixture-password|fixture-user/);
  const forgotten=await f.manager.manage(target,scope,'forget');assert.equal(forgotten.enabled,false);assert.equal(f.vault.read(loginScopeKey(scope,target)),undefined);
 }finally{f.manager.dispose()}
});
test('another frame cannot submit a password and a revoked scope cannot persist cookies',async()=>{
 const f=fixture();try{
  await f.manager.manage(target,scope,'enable');
  f.request()({url:target.sap_url,method:'POST',webContentsId:7,frame:{...f.frame},uploadData:[{bytes:Buffer.from('sap-user=u&sap-password=p&sap-client=200')}]},()=>{});
  f.frame.executeJavaScript=async()=>({sap:true});f.wc.emit('did-frame-finish-load',{},false,2,3);await f.manager.tick();
  assert.equal(f.vault.read(loginScopeKey(scope,target)).credentials,undefined);
  f.setScope({...scope,tenant:'other',epoch:2});
  f.cookies.emit('changed',{}, {name:'x',value:'must-not-persist',domain:'sap.example',path:'/'},'explicit',false);
  assert.equal(f.vault.read(loginScopeKey(scope,target)).cookies.length,0);
 }finally{f.manager.dispose()}
});
test('bridge accepts no renderer passwords, URLs, scripts or unknown actions',()=>{
 assert.equal(checkBridgeCall({method:'manageSapLogin',params:{binding_id:target.binding_id,tenant_id:'team',action:'prepare'}}).ok,true);
 for(const params of [{binding_id:target.binding_id,action:'prepare'}, {binding_id:target.binding_id,tenant_id:'',action:'prepare'}, {binding_id:target.binding_id,tenant_id:'team',action:'prepare',password:'p'},{binding_id:target.binding_id,tenant_id:'team',action:'execute'},{binding_id:target.binding_id,tenant_id:'team',action:'enable',url:target.sap_url}]) assert.equal(checkBridgeCall({method:'manageSapLogin',params}).ok,false);
});
test('web team suspension immediately stops reads and persistence until an authorized prepare',async()=>{
 const f=fixture();try{
  await f.manager.manage(target,scope,'enable');let reads=0;
  f.frame.executeJavaScript=async()=>{reads++;return {login:true}};
  f.manager.suspend();await f.manager.tick();
  f.cookies.emit('changed',{}, {name:'x',value:'suspended',domain:'sap.example',path:'/'},'explicit',false);
  assert.equal(reads,0);assert.equal(f.vault.read(loginScopeKey(scope,target)).cookies.length,0);
  await f.manager.manage(target,scope,'prepare');await f.manager.tick();assert.equal(reads,1);
 }finally{f.manager.dispose()}
});
