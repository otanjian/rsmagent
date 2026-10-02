const {test} = require('node:test');
const assert = require('node:assert/strict');
const {boot} = require('./support/account_memory.cjs');
const read = {status:'success',content:'BODY',revision:'r1',actions:{edit:true,delete:true},read_only:false};
test('read revision round-trips to save through the formal owner API', async () => {
    const {sandbox, calls, doc} = boot([read,{status:'success',result:{revision:'r2'}}]);
    const entry = doc();
    const loaded = await sandbox.memoryDocRead(entry);
    assert.equal(loaded.mtime, 'r1');
    assert.equal(loaded.editable, true);
    const saved = await sandbox.memoryDocWrite(entry, 'NEW', loaded.mtime);
    assert.equal(saved.mtime, 'r2');
    assert.deepEqual(calls[1].body, {scope:'personal',filename:'notes.md',category:'memory',content:'NEW',revision:'r1'});
});
test('server edit permission is respected for every category', async () => {
    for (const category of ['memory','dream','evolution']) {
        const {sandbox,doc} = boot([{...read,actions:{edit:false,delete:true}}]);
        assert.equal((await sandbox.memoryDocRead(doc({category}))).editable,false);
    }
});
test('conflict is preserved and intentional overwrite re-reads current version', async () => {
    const {sandbox, doc, calls} = boot([{status:'error',code:'stale_revision'},read,{status:'success',result:{revision:'r3'}}]);
    assert.equal((await sandbox.memoryDocWrite(doc(),'NEW','old')).code,'conflict');
    assert.equal((await sandbox.memoryDocWrite(doc(),'NEW',null)).mtime,'r3');
    assert.equal(calls[2].body.revision,'r1');
});
test('saved body with pending index is reported to the user', async () => {
    const {sandbox,doc,toasts} = boot([{status:'pending',result:{revision:'r2'}}]);
    assert.equal((await sandbox.memoryDocWrite(doc(),'NEW','r1')).status,'success');
    assert.deepEqual(toasts,['memory_index_pending']);
});
test('delete uses the displayed revision after confirmation', async () => {
    const {sandbox,doc,editor,calls,confirms} = boot([{status:'success'}, {status:'success',total:0}]);
    editor.open(doc({revision:'r1',actions:{delete:true}}));
    sandbox.memoryDocDelete();
    assert.equal(calls.length,0);
    await confirms[0].onConfirm();
    assert.deepEqual(calls[0].body,{scope:'personal',filename:'notes.md',category:'memory',revision:'r1'});
});
test('full clear uses collection version and explicitly names all categories', async () => {
    const {sandbox,run,calls,confirms} = boot([{status:'success'},{status:'success',total:0}]);
    run("memoryMetadata = {actions:{clear:true},collection_revision:'collection',counts:{global:1,daily:2,dream:3,evolution:4}}");
    sandbox.memoryDocClear();
    assert.match(confirms[0].message,/tenant-a/);
    await confirms[0].onConfirm();
    assert.deepEqual(calls[0].body,{scope:'personal',clear_scope:'all_personal',revision:'collection'});
});
test('previous-account draft and delayed confirmation cannot write to a new owner', async () => {
    const {sandbox,doc,editor,calls,confirms} = boot([]);
    const old = doc({revision:'r1',actions:{delete:true}});
    editor.open(old); sandbox.memoryDocDelete(); sandbox._authEpoch++;
    await assert.rejects(sandbox.memoryDocWrite(old,'SECRET','r1'),/memory_account_changed/);
    await confirms[0].onConfirm();
    assert.equal(calls.length,0);
});
test('viewer routes all reads and writes to memory APIs', () => {
    const {editor,sandbox} = boot();
    assert.equal(editor.config.read,sandbox.memoryDocRead);
    assert.equal(editor.config.write,sandbox.memoryDocWrite);
});
