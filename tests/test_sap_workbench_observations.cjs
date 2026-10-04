// Standards-based observation fixtures, not captured SAP markup or acceptance.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../Scene/sap_workbench/browser_service/dom.py'),'utf8');
const snapshotScript=source.match(/SNAPSHOT = r"""([\s\S]*?)"""/)[1];

function fixture() {
    const doc={title:'创建采购订单',body:{innerText:'Displayed document'},
        defaultView:{getComputedStyle:e=>({visibility:e.visibility||'visible'})}};
    const writes=[];
    const element=(id, attrs={})=>({id,ownerDocument:doc,tagName:'DIV',innerText:'',parentElement:null,
        getAttribute:k=>attrs[k]??null,
        setAttribute:(k,v)=>{writes.push([id,k,v]);attrs[k]=v},
        getClientRects:()=>[{}],getBoundingClientRect:()=>({left:0,top:0,right:600,bottom:400}),
        closest:()=>null,querySelectorAll:()=>[],
        focus:()=>{throw new Error('observations cannot focus')},
        scrollIntoView:()=>{throw new Error('observations cannot scroll')}});
    const group=element('group',{role:'tablist'});
    const tab=element('tab-a',{role:'tab','aria-label':'项目','aria-selected':'true','aria-controls':'panel-a'});
    tab.closest=s=>s==='[role=tablist]'?group:null;
    const panel=element('panel-a',{role:'tabpanel','aria-labelledby':'tab-a'});
    const nodes=[group,tab,panel],controls=[tab],panels=[panel],tables=[];
    doc.querySelectorAll=s=>s==='[id]'?nodes:s==='[role=tabpanel]'?panels:
        s.startsWith('button,input')?controls:s==='table,[role=grid],[role=treegrid]'?tables:[];
    const snapshot=()=>JSON.parse(JSON.stringify(vm.runInNewContext(snapshotScript,
        {document:doc,location:{origin:'https://sap.example.test'}})));
    return {doc,element,group,tab,panel,nodes,controls,panels,tables,writes,snapshot};
}

test('tab observations preserve unique reciprocal panel/group links without claiming a paging effect',()=>{
    const f=fixture(), control=f.snapshot().controls[0];
    assert.equal(control.selected,'true');
    assert.deepEqual(control.tab,{native_id:'tab-a',tablist_id:'group',panel_id:'panel-a',panel_visible:true,
        association:'reciprocal',paging_mode:'unknown',automatic:false});
    assert.ok(f.writes.every(([,key])=>key==='data-rsm-sap-control'));
    f.panel.setAttribute('aria-hidden','true');assert.equal(f.snapshot().controls[0].tab.panel_visible,false);
    f.panel.setAttribute('aria-hidden','false');
    f.panel.parentElement=f.element('hidden-parent',{'aria-hidden':'true'});
    assert.equal(f.snapshot().controls[0].tab.panel_visible,false);
});

test('one-way ARIA references are reported distinctly without inferring reciprocal links',()=>{
    const forward=fixture();forward.panel.setAttribute('aria-labelledby','');
    assert.equal(forward.snapshot().controls[0].tab.association,'aria_controls');
    const reverse=fixture();reverse.tab.setAttribute('aria-controls','');
    reverse.panel.setAttribute('aria-labelledby','panel-heading tab-a');
    assert.equal(reverse.snapshot().controls[0].tab.association,'aria_labelledby');
    assert.equal(reverse.snapshot().controls[0].tab.panel_id,'panel-a');
});

test('conflicting or nonunique native associations cannot resolve a tab panel',()=>{
    for(const change of ['wrong-backlink','additional-backlink','duplicate-panel','duplicate-tab','multiple-targets','absent-target','wrong-role']) {
        const f=fixture();
        if(change==='wrong-backlink')f.panel.setAttribute('aria-labelledby','tab-b');
        if(change==='additional-backlink'){
            const extra=f.element('panel-b',{role:'tabpanel','aria-labelledby':'tab-a'});f.panels.push(extra);f.nodes.push(extra);
        }
        if(change==='duplicate-panel')f.nodes.push(f.element('panel-a',{role:'tabpanel'}));
        if(change==='duplicate-tab')f.nodes.push(f.element('tab-a',{role:'tab'}));
        if(change==='multiple-targets')f.tab.setAttribute('aria-controls','panel-a panel-b');
        if(change==='absent-target')f.tab.setAttribute('aria-controls','absent');
        if(change==='wrong-role')f.panel.setAttribute('role','button');
        const result=f.snapshot().controls[0].tab;
        assert.equal(result.association,'unresolved',change);assert.equal(result.panel_id,'',change);
        assert.equal(result.panel_visible,null,change);assert.equal(result.automatic,false,change);
    }
    const f=fixture();f.nodes.push(f.element('group',{role:'tablist'}));
    assert.equal(f.snapshot().controls[0].tab.tablist_id,'');
});

test('oversized native identities and malformed ID references are not truncated into valid links',()=>{
    for(const change of ['long-tab','nul-tab','long-group','long-controls','long-backlink','many-backlinks','selector-text']) {
        const f=fixture();
        if(change==='long-tab')f.tab.id='t'.repeat(201);
        if(change==='nul-tab')f.tab.id='tab\x00a';
        if(change==='long-group')f.group.id='g'.repeat(201);
        if(change==='long-controls')f.tab.setAttribute('aria-controls','panel-a'+' '.repeat(801));
        if(change==='long-backlink')f.panel.setAttribute('aria-labelledby','tab-a'+' '.repeat(801));
        if(change==='many-backlinks')f.panel.setAttribute('aria-labelledby','tab-a b c d e');
        if(change==='selector-text')f.tab.setAttribute('aria-controls','panel-a"] [role="button');
        const result=f.snapshot().controls[0].tab;
        if(change==='long-group')assert.equal(result.tablist_id,'');
        else {assert.equal(result.association,'unresolved',change);assert.equal(result.panel_id,'',change);}
        assert.equal(result.automatic,false);
    }
});

test('tab references resolve only in the owning document and panel visibility includes containing frames',()=>{
    const f=fixture(), top={title:f.doc.title,body:{innerText:'Outer'},defaultView:{getComputedStyle:()=>({visibility:'visible'})}};
    const frame={ownerDocument:top,getClientRects:()=>[],getAttribute:()=>null};
    f.doc.defaultView.frameElement=frame;
    top.querySelectorAll=s=>s==='iframe,frame'?[{contentDocument:f.doc}]:[];
    const result=JSON.parse(JSON.stringify(vm.runInNewContext(snapshotScript,{document:top,location:{origin:'https://sap.example.test'}})));
    assert.equal(result.controls[0].id,'1:control:0');assert.equal(result.controls[0].tab.panel_visible,false);
    f.nodes.splice(f.nodes.indexOf(f.panel),1);f.panels.length=0;
    top.querySelectorAll=s=>s==='iframe,frame'?[{contentDocument:f.doc}]:s==='[id]'?[f.panel]:[];
    const detached=JSON.parse(JSON.stringify(vm.runInNewContext(snapshotScript,{document:top,location:{origin:'https://sap.example.test'}})));
    assert.equal(detached.controls[0].tab.association,'unresolved');
});

function tableFixture() {
    const f=fixture(), table=f.element('items',{role:'grid','aria-label':'采购订单项目','aria-rowcount':'120','aria-colcount':'8'});
    Object.assign(table,{scrollTop:0,scrollLeft:0,clientWidth:600,clientHeight:400,scrollWidth:600,scrollHeight:400});
    const rows=[2,3].map(n=>f.element('row-'+n,{'aria-rowindex':String(n)}));
    const columns=[2,3].map(n=>f.element('column-'+n,{role:'columnheader','aria-colindex':String(n)}));
    table.querySelectorAll=s=>s==='tr,[role=row]'?rows:s==='[role=columnheader]'?columns:[];
    f.tables.push(table);f.nodes.push(table,...rows,...columns);
    return {...f,table,rows,columns};
}

test('declared ARIA counts and 1-based indices are separate from bounded rows and pagination support',()=>{
    const f=tableFixture(), result=f.snapshot().tables[0];
    assert.deepEqual(result.structure,{count_source:'aria',row_count:120,row_count_state:'declared',
        column_count:8,column_count_state:'declared',row_index:{source:'aria',base:1},
        column_index:{source:'aria',base:1},complete:false,pagination_supported:false});
    assert.deepEqual(result.viewport.row_indices,[2,3]);assert.equal(result.rows.length,2);
    assert.equal(result.scroll.mode,'bounded_wheel');
    assert.ok(f.writes.every(([,key])=>['data-rsm-sap-control','data-rsm-sap-table'].includes(key)));
});

test('unknown, unavailable and malformed ARIA totals do not become visible-window counts',()=>{
    const states=[['-1','unknown'],['','invalid'],['1.5','invalid'],['-2','invalid'],['1000001','invalid'],['Infinity','invalid']];
    for(const [raw,state] of states){
        const f=tableFixture();f.table.setAttribute('aria-rowcount',raw);f.table.setAttribute('aria-colcount',raw);
        const s=f.snapshot().tables[0].structure;
        assert.equal(s.row_count,null,raw);assert.equal(s.row_count_state,state,raw);
        assert.equal(s.column_count,null,raw);assert.equal(s.column_count_state,state,raw);
    }
    const f=tableFixture();f.table.setAttribute('aria-rowcount',null);
    assert.equal(f.snapshot().tables[0].structure.row_count_state,'unavailable');
});

test('SAP matrix, mixed and malformed ARIA indices retain an unknown base instead of claiming global row positions',()=>{
    const f=tableFixture();
    for(const r of f.rows){r.setAttribute('aria-rowindex',null);r.setAttribute('lsmatrixrowindex','0');}
    let s=f.snapshot().tables[0].structure;
    assert.deepEqual(s.row_index,{source:'sap_lsmatrix',base:null});
    f.rows[0].setAttribute('aria-rowindex','2');s=f.snapshot().tables[0].structure;
    assert.deepEqual(s.row_index,{source:'mixed',base:null});
    for(const r of f.rows)r.setAttribute('aria-rowindex','0');s=f.snapshot().tables[0].structure;
    assert.deepEqual(s.row_index,{source:'invalid',base:null});
    assert.equal(s.complete,false);assert.equal(s.pagination_supported,false);
});
