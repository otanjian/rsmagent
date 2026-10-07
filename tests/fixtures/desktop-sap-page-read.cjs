// Run with desktop/node_modules/.bin/electron, without touching the user's app/profile.
const {app, BrowserWindow} = require('electron');
const http = require('node:http');
const assert = require('node:assert/strict');
const {readSapPage} = require('../../desktop/dist/main/remote/sap-page-read');
const server = handler => new Promise(resolve => {
  const s = http.createServer(handler).listen(0, '127.0.0.1', () => resolve(s));
});
let shell, sap, window;
app.whenReady().then(async () => {
  try {
    assert.equal(process.platform, 'darwin');
    sap = await server((_req, res) => res.end(`<title>SAP fixture</title>
      <label for="quantity">Quantity</label><input id="quantity" value="2">
      <div role="grid"><div role="row"><span role="columnheader">Material</span></div>
      <div role="row"><span role="gridcell">EWMS4-01</span></div></div>
      <div id="statusbar"><span>User: SAP_TEST</span><span>Client: 200</span><span>System: S4H</span></div>
      <script>document.querySelector('#quantity').value='7'</script>`));
    const sapURL = `http://127.0.0.1:${sap.address().port}`;
    shell = await server((_req, res) => res.end(`<iframe name="sap-gui-binding-123" src="${sapURL}"></iframe>
      <iframe name="other-pane" srcdoc="<p>DO-NOT-READ</p>"></iframe>`));
    window = new BrowserWindow({show:false, webPreferences:{partition:'sap-read-fixture', sandbox:true, contextIsolation:true, nodeIntegration:false}});
    await window.loadURL(`http://127.0.0.1:${shell.address().port}`);
    const request = {binding_id:'binding-123', read_id:'reading-123', view_id:'view-12345'};
    let checks = 0;
    const context = async () => {checks++; return {...request, id:request.read_id, expires_at:Date.now()+15000,
      sap_url:sapURL, allowed_origins:[]};};
    const root = window.webContents.mainFrame.frames.find(frame => frame.name === 'sap-gui-binding-123');
    assert.ok(root.framesInSubtree.includes(root), 'Electron subtree must include the SAP root');
    const before = await root.executeJavaScript('JSON.stringify([document.body.innerHTML,document.activeElement.id,scrollX,scrollY])');
    const result = await readSapPage(window.webContents, request, context);
    assert.equal(result.fields.find(field => field.label === 'Quantity').value, '7');
    assert.equal(result.tables[0].rows[1].cells[0].value, 'EWMS4-01');
    assert.deepEqual(result.currentUser,{account:'SAP_TEST',client:'200',systemId:'S4H',source:'sap_session_ui',sourceDocument:1});
    assert.doesNotMatch(JSON.stringify(result), /DO-NOT-READ/);
    assert.equal(await root.executeJavaScript('JSON.stringify([document.body.innerHTML,document.activeElement.id,scrollX,scrollY])'), before);
    assert.equal(checks, 2);
    console.log(JSON.stringify({passed:true, platform:process.platform, electron:process.versions.electron,
      crossOriginFrame:true, currentUnsavedValue:true, currentUser:true, readonly:true, source:result.source}));
    app.exit(0);
  } catch (error) {console.error(error); app.exit(1);}
}).finally(() => {window?.destroy(); shell?.close(); sap?.close();});
