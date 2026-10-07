// Local HTTPS fixture only; no real SAP password or user profile is accessed.
const {app, BrowserWindow, session} = require('electron');
const assert = require('node:assert/strict');
const https = require('node:https');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {randomUUID, randomBytes, createCipheriv, createDecipheriv, X509Certificate} = require('node:crypto');
const {SapLoginMemory, SapLoginVault, loginScopeKey} = require('../../desktop/dist/main/remote/sap-login-memory');
const directory=fs.mkdtempSync(path.join(os.tmpdir(),'sap-login-electron-'));
app.setPath('userData',directory);
app.on('window-all-closed',()=>{});
const secret=randomBytes(32), cipher={isEncryptionAvailable:()=>true,
  encryptString(text){const iv=randomBytes(12),c=createCipheriv('aes-256-gcm',secret,iv);const bytes=Buffer.concat([c.update(text),c.final()]);return Buffer.concat([iv,c.getAuthTag(),bytes]);},
  decryptString(bytes){const c=createDecipheriv('aes-256-gcm',secret,bytes.subarray(0,12));c.setAuthTag(bytes.subarray(12,28));return Buffer.concat([c.update(bytes.subarray(28)),c.final()]).toString();}};
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const until=async check=>{for(let n=0;n<60;n++){if(await check())return;await pause(100)}throw Error('fixture timed out');};
let server, manager, window;
app.whenReady().then(async()=>{
  try {
    const cert=fs.readFileSync(process.env.SAP_FIXTURE_CERT),key=fs.readFileSync(process.env.SAP_FIXTURE_KEY);
    let sessions=0;
    server=https.createServer({cert,key},(req,res)=>{
      if(req.method==='POST') {
        let body='';req.on('data',b=>body+=b);req.on('end',()=>{
          const form=new URLSearchParams(body);
          assert.equal(form.get('sap-password'),'fixture-only-password');sessions++;
          res.writeHead(303,{'Location':'/webgui/','Set-Cookie':'SAP_SESSIONID_FIX_200=fixture-session; Path=/; Secure; HttpOnly; SameSite=None'});res.end();
        });return;
      }
      if(sessions && req.headers.cookie?.includes('SAP_SESSIONID_FIX_200=fixture-session')) {
        res.end('<title>SAP fixture</title><div ct="TXT" id="signed-in">Order fixture</div>');return;
      }
      res.end('<title>SAP fixture login</title><form name="loginForm" method="POST"><input name="sap-client" value="200"><input name="sap-user"><input name="sap-password" type="password"><input name="sap-language" value="ZH"></form>');
    });
    await new Promise(r=>server.listen(0,'127.0.0.1',r));
    const target={binding_id:'binding-fixture',sap_url:`https://127.0.0.1:${server.address().port}/webgui/`,client:'200'};
    const scope={server:'https://platform.fixture',user:'fixture',tenant:'team',epoch:1};
    const vault=new SapLoginVault(path.join(directory,'vault'),cipher);
    const create=async()=>{
      const partition='sap-fixture-'+randomUUID(),ses=session.fromPartition(partition);
      const expected=new X509Certificate(cert).fingerprint256;
      ses.setCertificateVerifyProc((request,done)=>done(request.hostname==='127.0.0.1' && new X509Certificate(request.certificate.data).fingerprint256===expected ? 0 : -3));
      window=new BrowserWindow({show:false,webPreferences:{partition,sandbox:true,contextIsolation:true,nodeIntegration:false}});
      manager=new SapLoginMemory(window.webContents,vault,()=>scope);
      await manager.manage(target,scope,'prepare');
      await window.loadURL('data:text/html,'+encodeURIComponent(`<iframe name="sap-gui-binding-fixture" src="${target.sap_url}"></iframe>`));
      return window.webContents.mainFrame.frames[0];
    };
    let frame=await create();await manager.manage(target,scope,'enable');
    await frame.executeJavaScript(`document.querySelector('[name="sap-user"]').value='fixture-user';document.querySelector('[name="sap-password"]').value='fixture-only-password';document.forms[0].submit()`);
    await until(()=>vault.read(loginScopeKey(scope,target))?.credentials?.password==='fixture-only-password');
    manager.dispose();window.destroy();
    frame=await create();
    assert.equal(await frame.executeJavaScript('!!document.querySelector("#signed-in")'),true,'session cookie must restore in a fresh partition');
    await window.webContents.session.clearStorageData({storages:['cookies']});
    await frame.executeJavaScript('location.reload()');
    await until(async()=>{
      try{return await frame.executeJavaScript('document.querySelector("[name=sap-password]")?.value === "fixture-only-password"')}catch{return false}
    });
    assert.equal(sessions,1,'autofill must not submit');
    await manager.manage(target,scope,'forget');
    assert.equal(vault.read(loginScopeKey(scope,target)),undefined);
    console.log(JSON.stringify({passed:true,electron:process.versions.electron,postCaptured:true,freshPartitionCookieRestored:true,expiredSessionAutofilled:true,noAutoSubmit:true,forgotten:true}));
  } finally {manager?.dispose();window?.destroy();server?.close();fs.rmSync(directory,{recursive:true,force:true})}
}).then(()=>app.exit(0)).catch(error=>{console.error(error);app.exit(1)});
