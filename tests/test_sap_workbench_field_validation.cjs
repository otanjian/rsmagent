// Execute the fixed observation code against isolated DOM attributes, then
// project its actual JSON through Python. These are not live SAP fixtures.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {spawnSync}=require('node:child_process');
const root=path.resolve(__dirname,'..');
// The project ships a POSIX venv, but these assertions must also run where
// that path exists yet is not executable (e.g. a Windows host reading a WSL
// venv). Probe candidates and use the first interpreter that actually runs.
const python=(()=>{
    const candidates=[process.env.SAP_WORKBENCH_PYTHON,
        path.join(root,'.venv/bin/python'),path.join(root,'.venv/Scripts/python.exe'),
        process.env.PYTHON,'python3','python'];
    for(const candidate of candidates){
        if(!candidate) continue;
        if(spawnSync(candidate,['-c','print(1)'],{encoding:'utf8'}).status===0) return candidate;
    }
    return path.join(root,'.venv/bin/python');
})();
const source=fs.readFileSync(path.join(root,'Scene/sap_workbench/browser_service/dom.py'),'utf8');
const snapshotScript=source.match(/SNAPSHOT = r"""([\s\S]*?)"""/)[1];

function observe({normal=null, editor=null, cell=null, active=false}={}) {
    const doc={title:'创建采购订单',body:{innerText:'交货日期 数量'},
        defaultView:{getComputedStyle:()=>({visibility:'visible'})}};
    const element=attrs=>({ownerDocument:doc,getAttribute:k=>attrs[k]??null,
        setAttribute:(k,v)=>{attrs[k]=v},getClientRects:()=>[{}]});
    const header={...element({lsmatrixcolindex:'7'}),title:'数量'};
    const grid={querySelectorAll:()=>[header]};
    const gridCell={...element({lsdata:'{"2":"EDIT"}',lsmatrixrowindex:'1',lsmatrixcolindex:'7','aria-invalid':cell}),
        closest:()=>grid};
    const quantity={...element({ct:'CBS',lsdata:'{"1":"FREETEXT"}','aria-invalid':editor}),
        id:'quantity-cell',name:'InputField',tagName:active?'INPUT':'SPAN',type:active?'text':undefined,
        value:active?'bad quantity':undefined,innerText:active?undefined:'bad quantity',closest:()=>gridCell};
    const date={...element({'aria-label':'交货日期','aria-invalid':normal}),id:'delivery-date',name:'delivery-date',
        tagName:'INPUT',type:'text',value:'2026.99.99'};
    doc.querySelectorAll=s=>s==='[ct=CBS]'?[quantity]:s==='input,textarea,select'?(active?[date,quantity]:[date]):[];
    return JSON.parse(JSON.stringify(vm.runInNewContext(snapshotScript,{document:doc,location:{origin:'https://sap.example.test'}})));
}

function project(observed) {
    const script='import json,sys\nfrom Scene.sap_workbench.browser_service.page import model_observation\nprint(json.dumps(model_observation(json.load(sys.stdin), {})))';
    const child=spawnSync(python,['-B','-c',script],
        {cwd:root,input:JSON.stringify(observed),encoding:'utf8',timeout:10000});
    assert.equal(child.status,0,child.stderr||String(child.error));return JSON.parse(child.stdout);
}

test('missing ARIA validity remains unavailable across actual snapshot and model projection',()=>{
    const observed=observe(), result=project(observed);
    assert.equal(observed.fields.length,2);
    for(const field of observed.fields){assert.equal(field.invalid,null);assert.equal(field.aria_invalid,'unavailable');}
    for(const field of result.fields){assert.equal(field.invalid,null);assert.equal(field.aria_invalid,'unavailable');}
    assert.equal(result.fields[1].cell_aria_invalid,'unavailable');
    assert.ok(!('business_validated' in result),'missing validity never synthesizes a business acceptance');
});

test('normal date and grid quantity observations retain standard and unknown invalid tokens',()=>{
    const samples=[...['true','grammar','spelling'].map(token=>({token,state:token})),
        {token:'unexpected',state:'unknown'},{token:' false ',state:'unknown'}];
    const all=[];
    for(const {token,state} of samples){
        const observed=observe({normal:token,editor:token});
        for(const field of observed.fields){assert.equal(field.invalid,true);assert.equal(field.aria_invalid,state);}
        all.push(...observed.fields);
    }
    const projected=project({...observe(),fields:all});
    assert.equal(projected.fields.length,10);
    assert.ok(projected.fields.every(field=>field.invalid===true));
    assert.equal(projected.fields[8].aria_invalid,'unknown');
});

test('an explicit cell error wins over an editor false flag and remains consistent after activation',()=>{
    const lazy=observe({normal:'false',editor:'false',cell:'true'});
    const active=observe({normal:'false',editor:'false',cell:'true',active:true});
    assert.deepEqual(lazy.fields,active.fields);
    const quantity=project(lazy).fields.find(field=>field.row===1);
    assert.equal(quantity.invalid,true);assert.equal(quantity.aria_invalid,'false');assert.equal(quantity.cell_aria_invalid,'true');
    const inverse=observe({editor:'grammar',cell:'false'}).fields.find(field=>field.row===1);
    assert.equal(inverse.invalid,true);assert.equal(inverse.aria_invalid,'grammar');
});

test('false and empty ARIA markers retain a neutral observation without inventing business validation',()=>{
    const observed=observe({normal:'',editor:'false',cell:''}), projected=project(observed);
    for(const field of projected.fields){assert.equal(field.invalid,false);assert.equal(field.aria_invalid,'false');}
    assert.ok(!('business_validated' in projected));
});
