const {test, before, after} = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {chromium} = require('playwright');
const {SAP_PAGE_DOM} = require('../desktop/dist/main/remote/sap-page-dom.js');
const {readSapPage} = require('../desktop/dist/main/remote/sap-page-read.js');
const {checkBridgeCall} = require('../desktop/dist/main/remote/host-bridge.js');
let browser;
before(async () => {browser = await chromium.launch({channel:'chrome', headless:true});});
after(async () => {await browser?.close();});

test('DOM reads current input, SAP grid and tabs without changing the page', async () => {
  const page = await browser.newPage();
  await page.setContent(`<title>标准采购订单</title><label for="order">采购订单</label><input id="order" value="4500000127">
    <label for="quantity">采购订单数量</label><input id="quantity" value="2">
    <div role="grid" aria-label="行项目"><table><tr role="row"><th role="columnheader" lsmatrixcolindex="1">物料</th><th role="columnheader" lsmatrixcolindex="2">数量</th></tr>
    <tr role="row" lsmatrixrowindex="10" aria-selected="true"><td role="gridcell" lsmatrixcolindex="1"><span ct="CBS">EWMS4-01</span></td><td role="gridcell" lsmatrixcolindex="2">2</td></tr></table></div>
    <div role="tab" aria-selected="true">交货计划</div><div role="status">已读取</div>
    <div hidden>HIDDEN-VALUE</div><div id="access-token"><span>TOKEN-SENTINEL</span></div>
    <div role="dialog">提示 <span data-secret>SECRET-SENTINEL</span></div>
    <div style="height:2000px"></div><label for="offscreen">屏外已渲染</label><input id="offscreen" value="rendered">
    <script>window.events=[];for(const type of ['change','blur','input'])document.addEventListener(type,e=>events.push(type),true)</script>`);
  await page.locator('#quantity').fill('7');
  const before = await page.evaluate(() => ({html:document.body.innerHTML, focus:document.activeElement.id, y:scrollY, events:[...events]}));
  const result = await page.evaluate(SAP_PAGE_DOM);
  assert.equal(result.fields.find(f => f.label === '采购订单数量').value, '7');
  assert.equal(result.fields.find(f => f.label === '屏外已渲染').value, 'rendered');
  assert.equal(result.tables[0].headers[0].label, '物料');
  assert.equal(result.tables[0].rows.find(r => r.index === '10').cells[0].value, 'EWMS4-01');
  assert.deepEqual(result.activeTabs, ['交货计划']);
  assert.equal(result.scope, 'rendered_dom');
  assert.doesNotMatch(JSON.stringify(result), /HIDDEN-VALUE|TOKEN-SENTINEL|SECRET-SENTINEL/);
  assert.deepEqual(await page.evaluate(() => ({html:document.body.innerHTML, focus:document.activeElement.id, y:scrollY, events:[...events]})), before);
  await page.close();
});

test('login returns no fields, and oversized DOM is bounded with limitations', async () => {
  const page = await browser.newPage();
  await page.setContent('<input type="password" value="PASSWORD-SENTINEL"><p>PRIVATE-LOGIN-PAGE</p>');
  assert.deepEqual(await page.evaluate(SAP_PAGE_DOM), {error:'login_required'});
  await page.setContent('<title>Large</title>' + '<input value="same">'.repeat(250) + '<p>文字</p>'.repeat(13000));
  const result = await page.evaluate(SAP_PAGE_DOM);
  assert.equal(result.fields.length, 200);
  assert.ok(result.text.length <= 12000);
  assert.ok(result.limitations.length > 1);
  await page.close();
});

test('current user comes from visible session status, not business users or stored login', async () => {
  const page = await browser.newPage();
  await page.setContent(`<h1>采购订单 已被 CREATOR 创建</h1><label for="business">用户</label><input id="business" value="BUSINESS_USER" readonly>
    <div id="statusbar"><span>用户: SAP_TEST</span><span>客户端: 200</span><span>系统: S4H</span>
    <span hidden>用户: HIDDEN_USER</span><span data-secret>用户: SECRET_USER</span></div>`);
  await page.evaluate(() => {window.rememberedSAPUser='OLD_USER'});
  const result = await page.evaluate(SAP_PAGE_DOM);
  assert.deepEqual(result.currentUser,{account:'SAP_TEST',client:'200',systemId:'S4H',source:'sap_session_ui'});
  await page.locator('#statusbar').evaluate(e=>e.remove());
  const unknown = await page.evaluate(SAP_PAGE_DOM);
  assert.equal(unknown.currentUser,null);assert.ok(unknown.limitations.some(s=>s.includes('当前账号未知')));
  await page.close();
});

test('system status supports readonly labelled fields and refuses editable, hidden or conflicting identities', async () => {
  const page=await browser.newPage();
  await page.setContent(`<section role="dialog" id="status-dialog"><h2>System: Status</h2><section aria-label="Usage Data">
    <label for="u">User</label><input id="u" readonly value="SAP_TEST">
    <table><tr><th>Client</th><td>200</td></tr></table><dl><dt>System ID</dt><dd>S4H</dd></dl>
    <div>Token: hidden-token</div></section><section aria-label="Database Data"><label for="db">User</label><input id="db" readonly value="DATABASE_USER"></section></section>`);
  assert.deepEqual((await page.evaluate(SAP_PAGE_DOM)).currentUser,{account:'SAP_TEST',client:'200',systemId:'S4H',source:'sap_session_ui'});
  await page.locator('#u').evaluate(e=>e.readOnly=false);
  assert.equal((await page.evaluate(SAP_PAGE_DOM)).currentUser,null);
  await page.locator('#u').evaluate(e=>e.readOnly=true);
  await page.evaluate(()=>{const n=document.createElement('span');n.textContent='User: ANOTHER';document.querySelector('[aria-label="Usage Data"]').append(n)});
  const conflict=await page.evaluate(SAP_PAGE_DOM);assert.equal(conflict.currentUser,null);assert.ok(conflict.limitations.some(s=>s.includes('冲突')));
  await page.setContent('<div id="statusbar">User: OLD</div><input name="sap-user" value="NEW"><input type="password" value="SECRET">');
  assert.deepEqual(await page.evaluate(SAP_PAGE_DOM),{error:'login_required'});
  await page.close();
});

test('real WebGUI current-user tooltips distinguish logon user from database user', async () => {
  const page=await browser.newPage();
  await page.setContent(`<label for="u">用户</label><input id="u" readonly title="ABAP 系统字段：当前用户的名称" value="SAP_TEST">
    <label for="c">集团</label><input id="c" readonly title="ABAP 系统字段：当前用户的客户端标识" value="200">
    <label for="db">用户</label><input id="db" readonly value="SAPHANADB">`);
  assert.deepEqual((await page.evaluate(SAP_PAGE_DOM)).currentUser,{account:'SAP_TEST',client:'200',systemId:null,source:'sap_session_ui'});
  await page.close();
});

function fixture() {
  const request = {binding_id:'binding-123', read_id:'reading-123', view_id:'view-12345'};
  const sample = {capturedAt:'now', source:'sap_page_dom', scope:'rendered_dom', title:'SAP', fields:[], tables:[], selection:[], activeTabs:[], messages:[], text:'', limitations:[]};
  const root = {name:'sap-gui-binding-123', url:'https://sap.example/webgui', processId:2, routingId:4, detached:false,
    executeJavaScript:async script => {assert.equal(script, SAP_PAGE_DOM);return structuredClone(sample);}};
  root.framesInSubtree = [root];
  const wc = new EventEmitter(); wc.mainFrame = {frames:[root]}; wc.isDestroyed = () => false;
  const context = async () => ({...request,id:request.read_id,expires_at:Date.now()+15000,sap_url:root.url,allowed_origins:[]});
  return {request,root,wc,context};
}

test('host reads only verified frame, rechecks request, and cleans navigation listener', async () => {
  const f = fixture(); let reads = 0;
  assert.equal((await readSapPage(f.wc, f.request, async () => {reads++;return f.context();}, 'darwin')).source,'sap_page_dom');
  assert.equal(reads,2); assert.equal(f.wc.listenerCount('did-frame-navigate'),0);
  await assert.rejects(readSapPage(f.wc,f.request,f.context,'win32'), /page_read_unsupported/);
  await assert.rejects(readSapPage(f.wc,f.request,async()=>({...await f.context(),binding_id:'another'}),'darwin'),/page_changed/);
  f.wc.mainFrame.frames.push({...f.root});
  await assert.rejects(readSapPage(f.wc,f.request,f.context,'darwin'),/page_read_unavailable/);
});

test('navigation and late revocation prevent delivery', async () => {
  const f = fixture();
  f.root.executeJavaScript = async () => {f.wc.emit('did-frame-navigate',{},f.root.url,200,'OK',false,2,4);return {source:'sap_page_dom'};};
  await assert.rejects(readSapPage(f.wc,f.request,f.context,'darwin'),/page_changed/);
  const next = fixture(); let calls=0;
  await assert.rejects(readSapPage(next.wc,next.request,async()=>{if(++calls===2)throw new Error('revoked');return next.context();},'darwin'),/revoked/);
  assert.equal(next.wc.listenerCount('did-frame-navigate'),0);
  const replaced = fixture(); let checks=0;
  await assert.rejects(readSapPage(replaced.wc,replaced.request,async()=>{
    if (++checks===2) replaced.wc.mainFrame.frames.push({...replaced.root});
    return replaced.context();
  },'darwin'),/page_changed/);
});

test('bridge rejects arbitrary scripts and URLs before reaching the host', () => {
  const {request} = fixture();
  assert.equal(checkBridgeCall({method:'readSapPage',params:request}).ok,true);
  for(const params of [{...request,url:'https://other'}, {...request,script:'danger()'}, {view_id:'short'}])
    assert.equal(checkBridgeCall({method:'readSapPage',params}).ok,false);
});

test('nested SAP document results retain provenance and truncation warnings', async () => {
  const f=fixture();
  const child={...f.root, name:'sap-content', routingId:8, executeJavaScript:async script=>({
    ...await f.root.executeJavaScript(script), fields:[{label:'Supplier',value:'fixture'}],
    limitations:['子文档文本已截断。'], activeTabs:['交货计划']})};
  f.root.framesInSubtree=[f.root,child];
  const result=await readSapPage(f.wc,f.request,f.context,'darwin');
  assert.equal(result.fields[0].sourceDocument,2);
  assert.ok(result.limitations.includes('子文档文本已截断。'));
  assert.match(result.activeTabs[0], /SAP 文档 2/);
});

test('current user has document provenance; conflicting SAP frames return unknown', async () => {
  const f=fixture(), original=f.root.executeJavaScript;
  const user={account:'SAP_TEST',client:'200',systemId:'S4H',source:'sap_session_ui'};
  const child={...f.root,name:'sap-content',routingId:8,executeJavaScript:async script=>({...await original(script),currentUser:user})};
  f.root.framesInSubtree=[f.root,child];
  assert.deepEqual((await readSapPage(f.wc,f.request,f.context,'darwin')).currentUser,{...user,sourceDocument:2});
  f.root.executeJavaScript=async script=>({...await original(script),currentUser:{...user,account:'OTHER'}});
  const conflict=await readSapPage(f.wc,f.request,f.context,'darwin');assert.equal(conflict.currentUser,null);
  assert.ok(conflict.limitations.some(s=>s.includes('文档间')));
  f.root.executeJavaScript=original;child.url='https://approved-content.example/';
  assert.equal((await readSapPage(f.wc,f.request,async()=>({...await f.context(),allowed_origins:['https://approved-content.example']}),'darwin')).currentUser,null);
});
