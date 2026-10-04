// Bound observations across nested SAP frames, independently of live SAP.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../Scene/sap_workbench/browser_service/dom.py'),'utf8');
const script=source.match(/SNAPSHOT = r"""([\s\S]*?)"""/)[1];

function observation({login=false}={}) {
    let traversed=0;
    const docs=[];
    for(let index=0;index<100;index++) {
        const doc={body:{innerText:'Page text '.repeat(5000)},defaultView:{getComputedStyle:()=>({visibility:'visible'})}};
        const item={ownerDocument:doc,innerText:'Long metadata '.repeat(1000),getClientRects:()=>[{}],getAttribute:()=>null,querySelectorAll:()=>[]};
        doc.querySelectorAll=selector=>{
            if(selector==='iframe,frame') {traversed++;return docs[index+1]?[{contentDocument:docs[index+1]}]:[];}
            if(selector==='input[type=password]') return login?[item]:[];
            if(selector==='[role=dialog],[aria-modal=true]') return Array(8).fill(item);
            if(selector==='[role=status],[role=alert],[aria-live=assertive],.lsMessageBar') return Array(15).fill(item);
            if(selector==='table,[role=grid],[role=treegrid]') return Array(8).fill(item);
            return [];
        };
        docs.push(doc);
    }
    docs[0].title='T'.repeat(10000);
    const value=vm.runInNewContext(script,{document:docs[0],location:{origin:'https://sap.example.test'}});
    return {value,traversed};
}

test('nested frame metadata has global counts and a strict serialized size bound',()=>{
    const {value,traversed}=observation();
    assert.equal(traversed,24);
    assert.equal(value.title.length,300);
    assert.equal(value.dialogs.length,8);
    assert.equal(value.messages.length,15);
    assert.ok(value.tables.length<=8);
    assert.ok(JSON.stringify(value).length<=48000);
});

test('a visible login field suppresses all page content and controls',()=>{
    const {value}=observation({login:true});
    assert.equal(value.login,true);
    assert.equal(value.text,'SAP sign-in required');
    for(const key of ['fields','controls','dialogs','messages','tables']) assert.equal(value[key].length,0);
    assert.ok(!JSON.stringify(value).includes('Page text'));
});

function gridObservation({active=false, mode='EDIT', count=1}={}) {
    const doc={title:'创建采购订单',body:{innerText:'短文本 采购订单数量'},defaultView:{getComputedStyle:()=>({visibility:'visible'})}};
    const base=attrs=>({ownerDocument:doc,getClientRects:()=>[{}],getAttribute:k=>attrs[k]??null,setAttribute:(k,v)=>{attrs[k]=v}});
    const header={...base({lsmatrixcolindex:'7'}),title:'短文本'};
    const grid={querySelectorAll:()=>[header]};
    const editors=Array.from({length:count},(_,i)=>{
        const cell={...base({lsdata:JSON.stringify({'2':mode}),lsmatrixrowindex:String(i+1),lsmatrixcolindex:'7'}),closest:()=>grid};
        return {...base({lsdata:JSON.stringify({'1':'FREETEXT'})}),id:`table[${i+1},7]_c`,name:'InputField',
            tagName:active?'INPUT':'SPAN',type:active?'text':undefined,value:active?'测试文本':undefined,
            innerText:active?undefined:'测试文本',closest:()=>cell};
    });
    const check={...base({role:'button'}),innerText:'检查 (Cmd Shift F3)',closest:()=>null};
    doc.querySelectorAll=selector=>{
        if(selector==='[ct=CBS]')return editors;
        if(selector==='input,textarea,select')return active?editors:[];
        if(selector.startsWith('button,input'))return [check];
        return [];
    };
    return vm.runInNewContext(script,{document:doc,location:{origin:'https://sap.example.test'}});
}

test('SAP lazy table editor keeps its row, label and identity after becoming an input',()=>{
    const before=gridObservation(),after=gridObservation({active:true});
    assert.equal(before.fields.length,1);
    assert.deepEqual(JSON.parse(JSON.stringify(before.fields)),JSON.parse(JSON.stringify(after.fields)));
    assert.equal(after.fields[0].label,'短文本 [row 1]');
    assert.equal(after.fields[0].value,'测试文本');
    assert.equal(after.fields[0].editable,true);
    assert.equal(after.fields[0].type,'sap_grid');
    assert.equal(gridObservation({mode:'READONLY'}).fields.length,0);
});

test('many SAP rows do not evict the check control or the first editable row',()=>{
    const result=gridObservation({count:300});
    assert.ok(result.truncated);
    assert.ok(JSON.stringify(result).length<=48000);
    assert.equal(result.controls[0].label,'检查 (Cmd Shift F3)');
    assert.equal(result.fields[0].row,1);
});

function guard(name,changes={}) {
    const doc={defaultView:{getComputedStyle:()=>({visibility:'visible'})}};
    const field={ownerDocument:doc,tagName:'INPUT',id:'sap-okcode',name:'okcode',type:'text',value:'/nSPRO',
        getClientRects:()=>[{}],getAttribute:k=>k==='data-rsm-sap-field'?'command':null,...changes.field};
    doc.activeElement=changes.focusChanged?{}:field;
    doc.querySelectorAll=selector=>selector==='iframe,frame'?[]:selector==='[data-rsm-sap-field]'?[field]:
        changes.modal&&selector.includes('role=dialog')?[field]:[];
    const expression=source.match(new RegExp(name+' = r"""([\\s\\S]*?)"""'))[1];
    return vm.runInNewContext(expression,{document:doc,window:doc.defaultView})({id:'command',value:'/nSPRO',field:true});
}

test('Enter guard only accepts the same focused visible command and value',()=>{
    assert.equal(guard('COMMAND_READY'),true);
    for(const changes of [{focusChanged:true},{modal:true},{field:{value:'/nME21N'}},{field:{id:'save',name:'save'}},
        {field:{type:'password'}},{field:{readOnly:true}},{field:{disabled:true}},{field:{getClientRects:()=>[]}}]) {
        assert.equal(guard('COMMAND_READY',changes),false);
    }
});

test('F4 guard refuses command fields, lost focus, modal and lazy spans',()=>{
    const field={id:'company',name:'company',value:'2000'};
    assert.equal(guard('KEY_TARGET_READY',{field}),true);
    assert.equal(guard('KEY_TARGET_READY'),false);
    for(const changes of [{field,focusChanged:true},{field,modal:true},{field:{...field,tagName:'SPAN'}},{field:{...field,readOnly:true}}]) {
        assert.equal(guard('KEY_TARGET_READY',changes),false);
    }
});

test('Enter guard verifies the complete active frame chain',()=>{
    const top={defaultView:{getComputedStyle:()=>({visibility:'visible'})}},child={defaultView:{getComputedStyle:()=>({visibility:'visible'})}};
    const frame={ownerDocument:top,contentDocument:child};child.defaultView.frameElement=frame;
    const field={ownerDocument:child,tagName:'INPUT',id:'okcode',name:'okcode',type:'text',value:'/nSPRO',
        getClientRects:()=>[{}],getAttribute:k=>k==='data-rsm-sap-field'?'command':null};
    child.activeElement=field;top.activeElement=frame;
    top.querySelectorAll=selector=>selector==='iframe,frame'?[frame]:[];
    child.querySelectorAll=selector=>selector==='[data-rsm-sap-field]'?[field]:[];
    const check=()=>vm.runInNewContext(source.match(/COMMAND_READY = r"""([\s\S]*?)"""/)[1],{document:top,window:top.defaultView})({id:'command',value:'/nSPRO'});
    assert.equal(check(),true);top.activeElement={};assert.equal(check(),false);
});

const writeScript=source.match(/FIELD_WRITE = r"""([\s\S]*?)"""/)[1];

function writeFixture({grid=false,tag='INPUT',type='text',value='2000',command=false,nested=false}={}) {
    const writes=[],events=[];
    function Input() {}
    function Textarea() {}
    function Select() {}
    for(const ctor of [Input,Textarea,Select])Object.defineProperty(ctor.prototype,'value',{
        get(){return this._value||'';},set(value){
            this._value=this.type==='number'&&value!==''&&!/^-?\d+(?:\.\d+)?$/.test(value)?'':value;
            if(!this.probe)writes.push(value);
        }});
    const view={getComputedStyle:e=>({visibility:e.visibility||'visible'}),
        HTMLInputElement:Input,HTMLTextAreaElement:Textarea,HTMLSelectElement:Select,
        Event:class {constructor(type,options){this.type=type;Object.assign(this,options);}}};
    const doc={title:'创建采购订单',body:{innerText:'Order draft'},defaultView:view,activeElement:null};
    const element=(attrs={},properties={})=>({ownerDocument:doc,tagName:'DIV',
        getClientRects:()=>[{}],getAttribute:k=>attrs[k]??null,setAttribute:(k,v)=>{attrs[k]=v;},
        removeAttribute:k=>{delete attrs[k];},...properties});
    const attrs={'aria-label':command?'命令':'公司代码',...(grid?{ct:'CBS',lsdata:'{"1":"FREETEXT"}'}:{})};
    const field=Object.assign(Object.create((tag==='SELECT'?Select:tag==='TEXTAREA'?Textarea:Input).prototype),element(attrs),{
        tagName:tag,type,id:command?'sap-okcode':grid?'table[1,7]_c':'company',name:command?'okcode':'company',_value:value,
        focus(){doc.activeElement=this;if(frame)top.activeElement=frame;if(f.onFocus)f.onFocus();},
        blur(){events.push('blur');},dispatchEvent(event){events.push(event.type);return true;},
        options:[{value:'2000',text:'Company 2000',disabled:false},{value:'3000',text:'Company 3000',disabled:false}]});
    const header=element({role:'columnheader',lsmatrixcolindex:'7'},{title:'短文本'});
    const table=element({role:'grid'},{querySelectorAll:s=>s==='[role=columnheader]'?[header]:[]});
    const cell=element({role:'gridcell',lsdata:'{"2":"EDIT"}',lsmatrixrowindex:'1',lsmatrixcolindex:'7'},{closest:()=>table});
    field.closest=()=>grid?cell:null;
    const f={field,doc,header,table,cell,writes,events,inputs:[field],overlays:[],extraNodes:[],onFocus:null};
    doc.createElement=()=>Object.assign(Object.create(Input.prototype),{probe:true,_value:''});
    const markers=['data-rsm-sap-field','data-rsm-sap-control','data-rsm-sap-table'];
    doc.querySelectorAll=s=>s===markers.map(k=>'['+k+']').join(',')?[...f.inputs,...f.extraNodes].filter(e=>markers.some(k=>e.getAttribute(k)!==null)):
        s==='input,textarea,select'?f.inputs:s==='[data-rsm-sap-field]'?f.inputs.filter(e=>e.getAttribute('data-rsm-sap-field')!==null):
        s==='[ct=CBS]'?(grid?f.inputs:[]):s==='[role=dialog],[aria-modal=true],input[type=password]'?f.overlays:
        s==='input[type=password]'?f.overlays.filter(e=>e.type==='password'):s==='[role=dialog],[aria-modal=true]'?f.overlays.filter(e=>e.type!=='password'):[];
    let top=doc,frame=null;
    if(nested){
        top={title:doc.title,body:{innerText:''},defaultView:{...view},querySelectorAll:s=>s==='iframe,frame'?[frame]:[]};
        frame=element({}, {ownerDocument:top,contentDocument:doc});view.frameElement=frame;
    }
    f.top=top;f.frame=frame;f.element=element;
    const snapshot=vm.runInNewContext(script,{document:top,location:{origin:'https://sap.example.test'}});
    f.payload={title:top.title,field:JSON.parse(JSON.stringify(snapshot.fields[0])),value:'3000',command};
    f.check=changes=>vm.runInNewContext(writeScript,{document:top,window:top.defaultView})({...f.payload,...changes});
    return f;
}

test('a fresh snapshot clears hidden field/control/table ordinals before assigning new targets',()=>{
    const f=writeFixture();
    const hidden=f.element({'data-rsm-sap-field':'0:0'},{tagName:'INPUT',type:'text',id:'previous',name:'previous',value:'old',getClientRects:()=>[]});
    const control=f.element({'data-rsm-sap-control':'0:control:0'});
    const table=f.element({'data-rsm-sap-table':'0:table:0'});
    f.inputs.unshift(hidden);f.extraNodes.push(control,table);
    const observed=vm.runInNewContext(script,{document:f.doc,location:{origin:'https://sap.example.test'}});
    assert.equal(hidden.getAttribute('data-rsm-sap-field'),null);
    assert.equal(control.getAttribute('data-rsm-sap-control'),null);
    assert.equal(table.getAttribute('data-rsm-sap-table'),null);
    assert.equal(observed.fields.length,1);assert.equal(observed.fields[0].id,'0:0');
    f.payload.field=JSON.parse(JSON.stringify(observed.fields[0]));
    assert.equal(f.check(),true,'the freshly observed field remains writable after a preceding field hides');
    assert.deepEqual(f.writes,['3000']);
});

test('snapshot marker cleanup applies independently to nested documents without reusing an old frame ordinal',()=>{
    const f=writeFixture({nested:true});
    const old=f.element({'data-rsm-sap-field':'1:0','data-rsm-sap-control':'1:control:0','data-rsm-sap-table':'1:table:0'},
        {getClientRects:()=>[]});f.extraNodes.push(old);
    const observed=vm.runInNewContext(script,{document:f.top,location:{origin:'https://sap.example.test'}});
    for(const marker of ['data-rsm-sap-field','data-rsm-sap-control','data-rsm-sap-table'])assert.equal(old.getAttribute(marker),null);
    assert.equal(observed.fields[0].id,'1:0');f.payload.field=JSON.parse(JSON.stringify(observed.fields[0]));
    assert.equal(f.check(),true);
});

const readyScript=source.match(/GRID_READY = r"""([\s\S]*?)"""/)[1];
function gridReady(f) {
    return vm.runInNewContext(readyScript,{document:f.top,window:f.top.defaultView})({id:f.payload.field.id,nativeId:f.field.id});
}
function restrict(node,state) {
    if(state==='hidden')node.getClientRects=()=>[];
    else if(state==='visibility')node.visibility='hidden';
    else if(state==='readonly')node.readOnly=true;
    else if(state==='disabled')node.disabled=true;
    else node.setAttribute(state,'true');
}

test('grid readiness and final write both refuse restrictions on the editor, cell or grid',()=>{
    for(const target of ['field','cell','table'])for(const state of ['hidden','visibility','readonly','disabled','aria-readonly','aria-disabled']){
        const f=writeFixture({grid:true});assert.equal(gridReady(f),true);
        restrict(f[target],state);
        assert.equal(gridReady(f),false,target+' '+state);
        assert.equal(f.check(),false,target+' '+state);assert.deepEqual(f.writes,[]);assert.deepEqual(f.events,[]);
    }
});

test('final grid write rechecks cell and grid restrictions introduced by focus handlers',()=>{
    for(const target of ['cell','table'])for(const state of ['hidden','visibility','readonly','disabled','aria-readonly','aria-disabled']){
        const f=writeFixture({grid:true});f.onFocus=()=>restrict(f[target],state);
        assert.equal(f.check(),false,target+' '+state);assert.deepEqual(f.writes,[]);assert.deepEqual(f.events,[]);
    }
});

test('grid readiness retains the FREETEXT/EDIT and unique native editor requirements',()=>{
    for(const mutation of ['readonly-mode','different-editor','duplicate','span']){
        const f=writeFixture({grid:true});
        if(mutation==='readonly-mode')f.cell.setAttribute('lsdata','{"2":"READONLY"}');
        if(mutation==='different-editor')f.field.setAttribute('lsdata','{"1":"VALUEHELP"}');
        if(mutation==='duplicate')f.inputs.push(f.field);
        if(mutation==='span')f.field.tagName='SPAN';
        assert.equal(gridReady(f),false,mutation);assert.deepEqual(f.writes,[]);assert.deepEqual(f.events,[]);
    }
});

test('field write keeps native input/change/blur semantics and exact observed value checks',()=>{
    for(const tag of ['INPUT','TEXTAREA','SELECT']){
        const f=writeFixture({tag,type:tag==='TEXTAREA'?'textarea':tag==='SELECT'?'select-one':'text'});
        assert.equal(f.check(),true,tag);assert.deepEqual(f.writes,['3000']);
        assert.deepEqual(f.events,['input','change','blur']);assert.equal(f.field.value,'3000');
    }
    const command=writeFixture({command:true,value:''});
    assert.equal(command.check({value:'/nSPRO'}),true);
    assert.deepEqual(command.events,['input','change'],'the command retains focus for the separate Enter guard');
});

for(const mutation of ['title','label','value','type','input-type','hidden','visibility','readonly','disabled','aria-readonly','aria-disabled','ordinal','command','duplicate','password','modal','grid-editor']){
    test('field write rejects changed '+mutation+' before focusing or writing',()=>{
        const f=writeFixture();let focused=false;const focus=f.field.focus;
        f.field.focus=function(){focused=true;focus.call(this);};
        if(mutation==='title')f.top.title='Different page';
        if(mutation==='label')f.field.setAttribute('aria-label','Other field');
        if(mutation==='value')f.field._value='new value';
        if(mutation==='type')f.field.tagName='TEXTAREA';
        if(mutation==='input-type')f.field.type='email';
        if(mutation==='hidden')f.field.getClientRects=()=>[];
        if(mutation==='visibility')f.field.visibility='hidden';
        if(mutation==='readonly')f.field.readOnly=true;
        if(mutation==='disabled')f.field.disabled=true;
        if(mutation==='aria-readonly')f.field.setAttribute('aria-readonly','true');
        if(mutation==='aria-disabled')f.field.setAttribute('aria-disabled','true');
        if(mutation==='ordinal')f.inputs.unshift(f.element());
        if(mutation==='command')f.field.name='okcode';
        if(mutation==='duplicate')f.inputs.push(f.field);
        if(mutation==='grid-editor'){f.field.setAttribute('ct','CBS');f.field.closest=()=>f.cell;}
        if(['password','modal'].includes(mutation))f.overlays.push(f.element({}, {type:mutation==='password'?'password':undefined}));
        assert.equal(f.check(),false);assert.equal(focused,false);assert.deepEqual(f.writes,[]);assert.deepEqual(f.events,[]);
    });
}

for(const mutation of ['title','value','label','readonly','hidden','modal','password','focus','replacement']){
    test('field write revalidates '+mutation+' after the focus handler',()=>{
        const f=writeFixture();
        f.onFocus=()=>{
            if(mutation==='title')f.top.title='Other screen';
            if(mutation==='value')f.field._value='focus default';
            if(mutation==='label')f.field.setAttribute('aria-label','Another field');
            if(mutation==='readonly')f.field.readOnly=true;
            if(mutation==='hidden')f.field.getClientRects=()=>[];
            if(['password','modal'].includes(mutation))f.overlays.push(f.element({}, {type:mutation==='password'?'password':undefined}));
            if(mutation==='focus')f.doc.activeElement={};
            if(mutation==='replacement')f.inputs=[Object.assign(Object.create(Object.getPrototypeOf(f.field)),f.field)];
        };
        assert.equal(f.check(),false);assert.deepEqual(f.writes,[]);assert.deepEqual(f.events,[]);
    });
}

test('field writes require the visible complete frame focus chain after focus',()=>{
    const f=writeFixture({nested:true});assert.equal(f.check(),true);
    const lost=writeFixture({nested:true});lost.onFocus=()=>{lost.top.activeElement={};};
    assert.equal(lost.check(),false);assert.deepEqual(lost.writes,[]);
    const hidden=writeFixture({nested:true});hidden.frame.getClientRects=()=>[];
    assert.equal(hidden.check(),false);assert.deepEqual(hidden.writes,[]);
});

test('grid write preserves row/column/FREETEXT/EDIT semantics and trimmed snapshot values',()=>{
    const f=writeFixture({grid:true,value:'  原始短文本  '});
    assert.equal(f.payload.field.value,'原始短文本');assert.equal(f.check({value:'新的短文本'}),true);
    for(const mutation of ['row','column','header','mode','editor','native-id','cell-hidden','header-hidden','grid-hidden','ct']){
        const f=writeFixture({grid:true});
        if(mutation==='row')f.cell.setAttribute('lsmatrixrowindex','2');
        if(mutation==='column')f.cell.setAttribute('lsmatrixcolindex','8');
        if(mutation==='header')f.header.title='数量';
        if(mutation==='mode')f.cell.setAttribute('lsdata','{"2":"READONLY"}');
        if(mutation==='editor')f.field.setAttribute('lsdata','{"1":"VALUEHELP"}');
        if(mutation==='native-id')f.field.id='table[2,7]_c';
        if(mutation==='cell-hidden')f.cell.getClientRects=()=>[];
        if(mutation==='header-hidden')f.header.getClientRects=()=>[];
        if(mutation==='grid-hidden')f.table.getClientRects=()=>[];
        if(mutation==='ct')f.field.setAttribute('ct','OTHER');
        assert.equal(f.check(),false,mutation);assert.deepEqual(f.writes,[]);
    }
    const focusChange=writeFixture({grid:true});
    focusChange.onFocus=()=>focusChange.cell.setAttribute('lsmatrixrowindex','2');
    assert.equal(focusChange.check(),false);assert.deepEqual(focusChange.writes,[]);
});

test('field writes reject oversized UTF-16 text, invalid native values and unavailable options without mutation',()=>{
    for(const value of ['x'.repeat(301),'😀'.repeat(200),'line\nbreak','zero\0byte']){
        const f=writeFixture();assert.equal(f.check({value}),false);assert.deepEqual(f.writes,[]);
    }
    const emoji=writeFixture();assert.equal(emoji.check({value:'😀'.repeat(150)}),true);
    const number=writeFixture({type:'number'});assert.equal(number.check({value:'not a number'}),false);
    assert.deepEqual(number.writes,[],'the native type probe never writes to the live field');
    const select=writeFixture({tag:'SELECT',type:'select-one'});
    select.field.options[1].disabled=true;assert.equal(select.check(),false);assert.deepEqual(select.writes,[]);
    const group=writeFixture({tag:'SELECT',type:'select-one'});
    group.field.options[1].closest=()=>({disabled:true});assert.equal(group.check(),false);
    const removed=writeFixture({tag:'SELECT',type:'select-one'});
    removed.field.options.pop();assert.equal(removed.check(),false);
    const unobserved=writeFixture({tag:'SELECT',type:'select-one'});
    unobserved.field.options.push({value:'4000',text:'New option',disabled:false});
    assert.equal(unobserved.check({value:'4000'}),false);
});

const gridTargetScript=source.match(/GRID_TARGET = r"""([\s\S]*?)"""/)[1];
function activationFixture({active=true,nested=false}={}) {
    const f=writeFixture({grid:true,tag:active?'INPUT':'SPAN',type:active?'text':undefined,nested});
    f.field.parentElement=f.cell;f.cell.parentElement=f.table;
    f.field.getBoundingClientRect=()=>({left:100,top:50,right:500,bottom:90});
    f.doc.defaultView.innerWidth=800;f.doc.defaultView.innerHeight=600;
    f.doc.elementFromPoint=()=>f.field;
    f.field.focus=()=>{throw new Error('activation guard must not focus');};
    f.field.scrollIntoView=()=>{throw new Error('activation guard must not scroll');};
    if(nested){
        f.top.defaultView.innerWidth=1200;f.top.defaultView.innerHeight=900;
        Object.assign(f.frame,{clientLeft:2,clientTop:3,offsetWidth:804,offsetHeight:606,
            getBoundingClientRect:()=>({left:60,top:50,right:864,bottom:656})});
        f.top.elementFromPoint=()=>f.frame;
    }
    f.check=changes=>vm.runInNewContext(gridTargetScript,{document:f.top,window:f.top.defaultView})({...f.payload,...changes});
    return f;
}

test('grid activation accepts only the observed lazy span or active input without focus or scroll',()=>{
    for(const active of [false,true]){
        const f=activationFixture({active});
        assert.deepEqual(JSON.parse(JSON.stringify(f.check())),{x:300,y:70,nativeId:'table[1,7]_c'});
        assert.deepEqual(f.writes,[]);assert.deepEqual(f.events,[]);assert.equal(f.doc.activeElement,null);
        f.doc.elementFromPoint=()=>f.cell;assert.ok(f.check(),'the same editor cell is an activation target');
        const text=f.element({}, {parentElement:f.field});f.doc.elementFromPoint=()=>text;
        assert.ok(f.check(),'a non-interactive child remains in the same editor');
    }
});

for(const mutation of ['title','marker','duplicate','native-id','row','column','header','value','mode','editor','ct','type','hidden','readonly','disabled','grid-disabled','cell-readonly','modal','password','ordinary-field']){
    test('grid activation rejects changed '+mutation+' without input',()=>{
        const f=activationFixture();
        if(mutation==='title')f.top.title='Other page';
        if(mutation==='marker')f.field.setAttribute('data-rsm-sap-field','old-field');
        if(mutation==='duplicate')f.inputs.push(f.field);
        if(mutation==='native-id')f.field.id='table[2,7]_c';
        if(mutation==='row')f.cell.setAttribute('lsmatrixrowindex','2');
        if(mutation==='column')f.cell.setAttribute('lsmatrixcolindex','8');
        if(mutation==='header')f.header.title='数量';
        if(mutation==='value')f.field._value='New value';
        if(mutation==='mode')f.cell.setAttribute('lsdata','{"2":"READONLY"}');
        if(mutation==='editor')f.field.setAttribute('lsdata','{"1":"VALUEHELP"}');
        if(mutation==='ct')f.field.setAttribute('ct','OTHER');
        if(mutation==='type')f.field.type='password';
        if(mutation==='hidden')f.field.getClientRects=()=>[];
        if(mutation==='readonly')f.field.readOnly=true;
        if(mutation==='disabled')f.field.disabled=true;
        if(mutation==='grid-disabled')f.table.setAttribute('aria-disabled','true');
        if(mutation==='cell-readonly')f.cell.setAttribute('aria-readonly','true');
        if(['password','modal'].includes(mutation))f.overlays.push(f.element({}, {type:mutation==='password'?'password':undefined}));
        if(mutation==='ordinary-field')f.payload.field.type='input';
        assert.equal(f.check(),null);assert.deepEqual(f.writes,[]);assert.deepEqual(f.events,[]);
    });
}

test('grid activation rejects offscreen, invalid or obstructed target points and interactive siblings',()=>{
    for(const rect of [{left:-800,top:50,right:-400,bottom:90},{left:100,top:700,right:500,bottom:740},
        {left:100,top:50,right:100,bottom:90},{left:100,top:50,right:NaN,bottom:90}]){
        const f=activationFixture();f.field.getBoundingClientRect=()=>rect;assert.equal(f.check(),null);
    }
    const overlay=activationFixture();overlay.doc.elementFromPoint=()=>overlay.element();assert.equal(overlay.check(),null);
    for(const attrs of [{tagName:'BUTTON'},{tagName:'INPUT'},{role:'button'},{role:'combobox'},{ct:'CBS'},
        {'data-rsm-sap-field':'other-field'},{contenteditable:'true'}]){
        const f=activationFixture(),hit=f.element(attrs,{tagName:attrs.tagName||'SPAN',parentElement:f.cell});
        f.doc.elementFromPoint=()=>hit;assert.equal(f.check(),null,JSON.stringify(attrs));
        const child=f.element({}, {parentElement:hit});f.doc.elementFromPoint=()=>child;assert.equal(f.check(),null);
    }
});

test('grid activation verifies each visible untransformed unobscured frame during coordinate translation',()=>{
    const f=activationFixture({nested:true});
    assert.deepEqual(JSON.parse(JSON.stringify(f.check())),{x:362,y:123,nativeId:'table[1,7]_c'});
    f.top.elementFromPoint=()=>({});assert.equal(f.check(),null);
    f.top.elementFromPoint=()=>f.frame;f.frame.getClientRects=()=>[];assert.equal(f.check(),null);
    f.frame.getClientRects=()=>[{}];f.frame.offsetWidth=1000;assert.equal(f.check(),null);
    f.frame.offsetWidth=804;f.top.defaultView.getComputedStyle=()=>({visibility:'visible',transform:'scale(2)'});
    assert.equal(f.check(),null);assert.deepEqual(f.writes,[]);assert.deepEqual(f.events,[]);
});

test('Escape guard requires the one matching dialog and focus inside it',()=>{
    const doc={defaultView:{getComputedStyle:()=>({visibility:'visible'})},activeElement:{}};
    const dialog={ownerDocument:doc,title:'值帮助',innerText:'选择公司',getClientRects:()=>[{}],getAttribute:()=>null,contains:()=>true};
    let dialogs=[dialog];doc.querySelectorAll=selector=>selector==='iframe,frame'?[]:dialogs;
    const check=()=>vm.runInNewContext(source.match(/VALUE_HELP_READY = r"""([\s\S]*?)"""/)[1],{document:doc,window:doc.defaultView})({label:'值帮助',text:'选择公司'});
    assert.equal(check(),true);dialogs=[dialog,dialog];assert.equal(check(),false);
    dialogs=[dialog];dialog.contains=()=>false;assert.equal(check(),false);
    dialog.contains=()=>true;dialog.title='保存更改？';assert.equal(check(),false);
});

const scrollScript=source.match(/SCROLL_TARGET = r"""([\s\S]*?)"""/)[1];

function scrollFixture() {
    const doc={title:'创建采购订单',body:{innerText:'Purchase-order page'},
        defaultView:{innerWidth:1000,innerHeight:800,getComputedStyle:()=>({visibility:'visible',transform:'none'})}};
    const element=(attrs={},changes={})=>({ownerDocument:doc,tagName:'DIV',
        getClientRects:()=>[{}],getAttribute:k=>attrs[k]??null,setAttribute:(k,v)=>{attrs[k]=v},
        getBoundingClientRect:()=>({left:100,top:100,right:700,bottom:400}),...changes});
    const grid=element({role:'grid','aria-label':'采购订单项目','data-rsm-sap-table':'0:table:0'},
        {id:'ME21N-items',scrollTop:320,scrollLeft:240,clientWidth:600,clientHeight:300,scrollWidth:1800,scrollHeight:1500});
    const rows=[2,3,4].map((n,i)=>element({'aria-rowindex':String(n)},{parentElement:grid,
        getBoundingClientRect:()=>({left:100,top:150+i*60,right:700,bottom:200+i*60})}));
    const headers=[2,3,4].map((n,i)=>element({role:'columnheader','aria-colindex':String(n)},{parentElement:grid,
        getBoundingClientRect:()=>({left:150+i*100,top:100,right:230+i*100,bottom:140}),innerText:'Column '+n}));
    const readonly=element({role:'gridcell','aria-readonly':'true'},{parentElement:grid,innerText:'Visible business data'});
    for(const row of rows)row.querySelectorAll=()=>[readonly];
    grid.querySelectorAll=s=>s==='tr,[role=row]'?rows:s==='[role=columnheader]'?headers:[];
    grid.contains=hit=>{for(let n=hit;n;n=n.parentElement)if(n===grid)return true;return false};
    let overlays=[];
    doc.querySelectorAll=s=>s==='[data-rsm-sap-table]'||s==='table,[role=grid],[role=treegrid]'?[grid]:
        s==='[role=dialog],[aria-modal=true],input[type=password]'?overlays:[];
    doc.elementFromPoint=()=>readonly;
    const payload={id:'0:table:0',native_id:'ME21N-items',role:'grid',label:'采购订单项目',title:doc.title,
        direction:'down',viewport:{top:320,left:240,width:600,height:300,scroll_width:1800,scroll_height:1500},
        row_indices:[2,3,4],column_indices:[2,3,4]};
    const check=changes=>vm.runInNewContext(scrollScript,{document:doc,window:doc.defaultView})({...payload,...changes});
    const snapshot=()=>vm.runInNewContext(script,{document:doc,location:{origin:'https://sap.example.test'}});
    return {doc,grid,rows,headers,readonly,payload,check,snapshot,element,setOverlays:v=>{overlays=v}};
}

test('table observations expose bounded labels, visible index windows and fixed scroll capability',()=>{
    const f=scrollFixture();f.grid.title='Body fallback must not be used';
    f.grid.innerText='Secret business body '.repeat(20000);
    f.grid.setAttribute('aria-label','采购订单项目'.repeat(100));
    f.rows.push(f.element({'aria-rowindex':'99'},{getBoundingClientRect:()=>({left:100,top:600,right:700,bottom:650})}));
    f.rows[3].querySelectorAll=()=>[f.readonly];
    f.headers.push(f.element({role:'columnheader','aria-colindex':'99'},{getBoundingClientRect:()=>({left:800,top:100,right:900,bottom:140})}));
    const table=f.snapshot().tables[0];
    assert.equal(table.id,'0:table:0');assert.equal(table.native_id,'ME21N-items');
    assert.equal(table.label.length,100);assert.ok(!table.label.includes('Secret'));
    assert.deepEqual(Array.from(table.viewport.row_indices),[2,3,4]);
    assert.deepEqual(Array.from(table.viewport.column_indices),[2,3,4]);
    assert.match(table.viewport.row_signature,/^[0-9a-f]{8}$/);
    assert.deepEqual(Array.from(table.scroll.directions),['up','down','left','right']);
    assert.equal(table.scroll.step_pixels.vertical,320);assert.equal(table.scroll.step_pixels.horizontal,240);
});

test('table capability rejects unknown role, invalid metrics, missing native identity and native boundaries',()=>{
    for(const change of ['table','missing','nan','oversize','disabled']) {
        const f=scrollFixture();
        if(change==='table')f.grid.setAttribute('role','table');
        if(change==='missing')f.grid.id='';
        if(change==='nan')f.grid.scrollTop=NaN;
        if(change==='oversize')f.grid.scrollHeight=10000001;
        if(change==='disabled')f.grid.disabled=true;
        assert.equal(f.snapshot().tables[0].scroll.directions.length,0,change);
    }
    const f=scrollFixture();f.grid.scrollTop=0;f.grid.scrollLeft=0;
    assert.deepEqual(Array.from(f.snapshot().tables[0].scroll.directions),['down','right']);
});

test('large page budget trims business rows before evicting actionable table identity',()=>{
    const f=scrollFixture();
    const inputs=Array.from({length:160},(_,i)=>f.element({'aria-label':'L'.repeat(200)},
        {tagName:'INPUT',type:'text',id:'field-'+i,value:'V'.repeat(300)}));
    const controls=Array.from({length:200},()=>f.element({role:'button'},{innerText:'B'.repeat(240),closest:()=>null}));
    const moreGrids=Array.from({length:7},(_,i)=>f.element({role:i<3?'table':'grid','aria-label':'Table '+i},
        {id:'extra-'+i,scrollTop:0,scrollLeft:0,clientWidth:600,clientHeight:300,scrollWidth:1800,scrollHeight:1500,
            querySelectorAll:f.grid.querySelectorAll}));
    f.doc.body.innerText='Long body '.repeat(20000);
    f.readonly.innerText='Long cell '.repeat(20000);
    for(const row of f.rows)row.querySelectorAll=()=>Array(16).fill(f.readonly);
    const query=f.doc.querySelectorAll;
    f.doc.querySelectorAll=s=>s==='input,textarea,select'?inputs:s.startsWith('button,input')?controls:
        s==='table,[role=grid],[role=treegrid]'?[...moreGrids.slice(0,3),f.grid,...moreGrids.slice(3)]:query(s);
    const result=f.snapshot();
    assert.ok(result.truncated);assert.ok(JSON.stringify(result).length<=48000);
    assert.equal(result.tables.length,4);
    assert.ok(result.tables.every(t=>t.role==='grid'&&t.scroll.directions.length>0&&t.viewport.valid));
    assert.ok(result.tables.some(t=>t.native_id==='ME21N-items'));
    assert.ok(result.tables.every(t=>t.rows.length===0));
});

test('scroll guard accepts only a point inside the same current visible grid',()=>{
    const f=scrollFixture();assert.deepEqual(JSON.parse(JSON.stringify(f.check())),{x:400,y:250});
    assert.equal(f.check({id:'unknown'}),null);assert.equal(f.check({native_id:'new-grid'}),null);
    assert.equal(f.check({label:'Other table'}),null);assert.equal(f.check({direction:'Enter'}),null);
    f.doc.elementFromPoint=()=>f.element();assert.equal(f.check(),null);
    f.doc.elementFromPoint=()=>f.element({}, {parentElement:f.grid});assert.equal(f.check(),null);
    f.doc.elementFromPoint=()=>f.grid;assert.ok(f.check());
    f.grid.getClientRects=()=>[];assert.equal(f.check(),null);
});

test('scroll guard checks modal, login and exact ME21N screen context again before input',()=>{
    const f=scrollFixture();
    for(const item of [f.element({role:'dialog'}),f.element({}, {tagName:'INPUT',type:'password'})]) {
        f.setOverlays([item]);assert.equal(f.check(),null);
    }
    f.setOverlays([]);f.doc.title='Unknown SAP Screen';assert.equal(f.check({title:f.doc.title}),null);
    f.doc.title='Create Purchase Order';assert.ok(f.check({title:f.doc.title}));
});

test('scroll guard refuses changed geometry, native positions and index windows',()=>{
    for(const change of ['top','width','nan','offscreen','row','column','role','disabled','contenteditable']) {
        const f=scrollFixture();
        if(change==='top')f.grid.scrollTop++;
        if(change==='width')f.grid.clientWidth++;
        if(change==='nan')f.grid.scrollTop=NaN;
        if(change==='offscreen')f.grid.getBoundingClientRect=()=>({left:1100,top:100,right:1600,bottom:400});
        if(change==='row')f.rows[0].setAttribute('aria-rowindex','1');
        if(change==='column')f.headers[0].setAttribute('aria-colindex','1');
        if(change==='role')f.grid.setAttribute('role','table');
        if(change==='disabled')f.grid.disabled=true;
        if(change==='contenteditable')f.grid.isContentEditable=true;
        assert.equal(f.check(),null,change);
    }
    const f=scrollFixture();f.grid.scrollTop=1200;
    assert.equal(f.check({viewport:{...f.payload.viewport,top:1200}}),null);
});

test('scroll guard never targets inputs, lazy editors, controls or unclassified editable cells',()=>{
    for(const attrs of [{tagName:'INPUT'},{tagName:'SELECT'},{tagName:'TEXTAREA'},{tagName:'BUTTON'},
        {role:'spinbutton'},{role:'combobox'},{role:'button'},{role:'gridcell'},{ct:'CBS'},
        {'data-rsm-sap-field':'field'},{contenteditable:'true'}]) {
        const f=scrollFixture();
        const item=f.element(attrs,{tagName:attrs.tagName||'DIV',parentElement:f.grid});
        const child=f.element({}, {parentElement:item});
        f.doc.elementFromPoint=()=>child;
        assert.equal(f.check(),null,JSON.stringify(attrs));
    }
    const f=scrollFixture();f.readonly.setAttribute('aria-readonly',null);f.readonly.setAttribute('lsdata','{"2":"READONLY"}');
    assert.ok(f.check());
});

test('scroll guard searches at most nine safe points without focusing or scrolling the page',()=>{
    const f=scrollFixture();let hits=0;
    const input=f.element({}, {tagName:'INPUT',parentElement:f.grid});
    f.doc.elementFromPoint=(x,y)=>{hits++;return y<140?f.headers[0]:input};
    const point=f.check();assert.deepEqual(JSON.parse(JSON.stringify(point)),{x:400,y:130});
    assert.equal(hits,2);
    hits=0;f.doc.elementFromPoint=()=>{hits++;return input};
    assert.equal(f.check(),null);assert.equal(hits,9);
});

test('scroll frame point translation verifies every ancestor is visible and unobscured',()=>{
    const f=scrollFixture(),child=f.doc;
    const top={title:child.title,defaultView:{innerWidth:2000,innerHeight:1600,
        getComputedStyle:()=>({visibility:'visible',transform:'none'})}};
    const frame={ownerDocument:top,contentDocument:child,clientLeft:2,clientTop:3,offsetWidth:1004,offsetHeight:806,
        getClientRects:()=>[{}],getBoundingClientRect:()=>({left:60,top:50,right:1064,bottom:856})};
    child.defaultView.frameElement=frame;
    top.querySelectorAll=s=>s==='iframe,frame'?[frame]:[];
    top.elementFromPoint=()=>frame;
    const check=()=>vm.runInNewContext(scrollScript,{document:top,window:top.defaultView})(f.payload);
    assert.deepEqual(JSON.parse(JSON.stringify(check())),{x:462,y:303});
    top.elementFromPoint=()=>({});assert.equal(check(),null);
    top.elementFromPoint=()=>frame;frame.getClientRects=()=>[];assert.equal(check(),null);
    frame.getClientRects=()=>[{}];frame.offsetWidth=1200;assert.equal(check(),null);
    frame.offsetWidth=1004;top.defaultView.getComputedStyle=()=>({visibility:'visible',transform:'scale(2)'});
    assert.equal(check(),null);
});
