"""Fixed, bounded SAP DOM observations. No model-provided selectors or script."""

SNAPSHOT = r"""(() => {
 const docs=[];
 const walk=d=>{if(docs.length>=24)return;docs.push(d);for(const f of d.querySelectorAll('iframe,frame')){
   try{if(f.contentDocument)walk(f.contentDocument)}catch(_){}}};walk(document);
 const visible=e=>Boolean(e.getClientRects().length)&&e.ownerDocument.defaultView.getComputedStyle(e).visibility!=='hidden';
 const text=e=>String(e.getAttribute('aria-label')||e.title||e.innerText||e.value||'').trim().slice(0,240);
 // WAI-ARIA invalid tokens are not a business validation. Missing attributes
 // remain unavailable; explicit errors on either a grid cell or editor win.
 const validation=(e,cell)=>{
   const state=n=>{const raw=n?.getAttribute('aria-invalid');
     if(raw===null||raw===undefined)return 'unavailable';if(raw===''||raw==='false')return 'false';
     return ['true','grammar','spelling'].includes(raw)?raw:'unknown';};
   const aria_invalid=state(e),cell_aria_invalid=state(cell),states=[aria_invalid,cell_aria_invalid];
   return {invalid:states.some(s=>!['unavailable','false'].includes(s))?true:states.includes('false')?false:null,
     aria_invalid,...(cell?{cell_aria_invalid}:{})};
 };
 const login=docs.some(d=>[...d.querySelectorAll('input[type=password]')].some(visible));
 if(login)return {origin:location.origin,title:document.title.slice(0,300),login:true,text:'SAP sign-in required',fields:[],controls:[],dialogs:[],messages:[],tables:[]};
 const fields=[],controls=[],dialogs=[],messages=[],tables=[];
 docs.forEach((d,di)=>{
   // Hidden/replaced controls must not retain an ordinal from an older read.
   // Clear only our observation markers, then keep the unique-target guards.
   for(const e of d.querySelectorAll('[data-rsm-sap-field],[data-rsm-sap-control],[data-rsm-sap-table]')){
     for(const marker of ['data-rsm-sap-field','data-rsm-sap-control','data-rsm-sap-table'])e.removeAttribute(marker);
   }
   // ARIA relationships are observations, never proof of SAP's local/server
   // paging effect. Resolve references in this document without selectors
   // assembled from page data; duplicate IDs cannot establish an association.
   let identities;
   const native=e=>{
     if(!e||typeof e.id!=='string'||!e.id||e.id.length>200||/[\s\x00-\x1f\x7f]/.test(e.id))return '';
     if(!identities){identities=new Map();for(const n of d.querySelectorAll('[id]')){
       const id=n.id;if(!identities.has(id))identities.set(id,n);else identities.set(id,null);
     }}return identities.get(e.id)===e?e.id:'';
   };
   const refs=raw=>{
     if(raw===null)return [];if(typeof raw!=='string'||raw.length>800)return null;
     const ids=raw.trim().split(/\s+/).filter(Boolean);return ids.length<=4&&ids.every(id=>id.length<=200)?ids:null;
   };
   const displayed=e=>{
     if(!e||!visible(e)||e.hidden||e.getAttribute('aria-hidden')==='true')return false;
     for(let n=e.parentElement;n;n=n.parentElement)if(n.hidden||n.getAttribute('aria-hidden')==='true')return false;
     let w=e.ownerDocument.defaultView,depth=0;
     while(w!==document.defaultView){if(++depth>24)return false;const f=w.frameElement;
       if(!f||!visible(f))return false;
       for(let n=f;n;n=n.parentElement)if(n.hidden||n.getAttribute('aria-hidden')==='true')return false;
       w=f.ownerDocument.defaultView;
     }return true;
   };
   const tabObservation=e=>{
     const native_id=native(e),tablist_id=native(e.closest('[role=tablist]'));
     const result={native_id,tablist_id,panel_id:'',panel_visible:null,association:'unresolved',paging_mode:'unknown',automatic:false};
     if(!native_id)return result;
     const panels=[...d.querySelectorAll('[role=tabpanel]')].filter(p=>Boolean(native(p)));
     const linked=panels.filter(p=>refs(p.getAttribute('aria-labelledby'))?.includes(native_id));
     const raw=e.getAttribute('aria-controls'),controlled=refs(raw);
     if(raw!==null&&raw!==''){
       if(!controlled||controlled.length!==1)return result;
       const panel=identities.get(controlled[0]);
       if(!panel||panel.getAttribute('role')!=='tabpanel'||!native(panel))return result;
       const backwards=refs(panel.getAttribute('aria-labelledby'));
       if(!backwards||(backwards.length&&!backwards.includes(native_id))||(linked.length&&(linked.length!==1||linked[0]!==panel)))return result;
       result.panel_id=panel.id;result.panel_visible=displayed(panel);
       result.association=backwards.includes(native_id)?'reciprocal':'aria_controls';
     }else if(linked.length===1){
       result.panel_id=linked[0].id;result.panel_visible=displayed(linked[0]);result.association='aria_labelledby';
     }return result;
   };
   // Web GUI table editors start as CBS spans and become inputs on focus.
   // Keep a cell's identity stable across that replacement and label it from
   // its actual column header, not SAP's otherwise anonymous InputField name.
   const gridEditors=[...d.querySelectorAll('[ct=CBS]')].filter(e=>visible(e)&&e.closest('[role=gridcell]'));
   [...d.querySelectorAll('input,textarea,select')].filter(visible).forEach((e,i)=>{
     if(gridEditors.includes(e))return;
     const name=[e.id,e.name,e.getAttribute('aria-label'),e.getAttribute('autocomplete')].join(' ');
     if(/password|passwd|sap-user|sap-client|token|secret|username|logon|credential/i.test(name))return;
     if(['password','hidden','submit','button','file'].includes(e.type))return;
     const label=e.getAttribute('aria-label')||e.title||[...(e.labels||[])].map(l=>l.innerText).join(' ')||e.id||e.name;
     const id=di+':'+i;e.setAttribute('data-rsm-sap-field',id);
     fields.push({id,label:label.slice(0,200),value:String(e.value||'').slice(0,300),editable:!e.disabled&&!e.readOnly,
       command:/okcd|okcode/i.test(name),type:e.tagName.toLowerCase(),input_type:e.type||'',...validation(e),
       ...(e.tagName==='SELECT'?{options:[...e.options].slice(0,80).map(o=>({value:o.value.slice(0,300),label:o.text.slice(0,240),disabled:o.disabled}))}:{})});
   });
   gridEditors.forEach(e=>{
     const cell=e.closest('[role=gridcell]'),grid=cell.closest('[role=grid],[role=treegrid],[role=table]');
     let meta,editor;try{meta=JSON.parse(cell.getAttribute('lsdata')||'{}');editor=JSON.parse(e.getAttribute('lsdata')||'{}')}catch(_){return}
     if(!grid||!e.id||editor['1']!=='FREETEXT'||meta['2']!=='EDIT'||e.type==='password')return;
     const row=cell.getAttribute('lsmatrixrowindex'),column=cell.getAttribute('lsmatrixcolindex');
     const header=[...grid.querySelectorAll('[role=columnheader]')].find(h=>h.getAttribute('lsmatrixcolindex')===column);
     if(!header||!/^\d+$/.test(row||''))return;
     const label=text(header),id=di+':grid:'+e.id;
     if(/password|passwd|token|secret|credential|密码|密碼/i.test(label+' '+e.id))return;
     e.setAttribute('data-rsm-sap-field',id);
     fields.push({id,label:(label+' [row '+row+']').slice(0,200),value:String(e.value??e.innerText??'').trim().slice(0,300),
       editable:!e.disabled&&!e.readOnly&&e.getAttribute('aria-readonly')!=='true'&&e.getAttribute('aria-disabled')!=='true',
       command:false,type:'sap_grid',input_type:'text',row:Number(row),column:label.slice(0,200),...validation(e,cell)});
   });
   [...d.querySelectorAll('button,input[type=button],input[type=submit],[role=button],[role=tab],[role=treeitem],[role=option],[role=row],[aria-expanded]')].filter(visible).forEach((e,i)=>{
     const expanded=e.getAttribute('aria-expanded');
     const role=expanded!==null&&e.closest('[role=tree],[role=treegrid]')?'treeitem':e.getAttribute('role')||'button',label=text(e);
     if(!label)return;
     const id=di+':control:'+i;e.setAttribute('data-rsm-sap-control',id);
     const popup=Boolean(e.closest('[role=dialog],[aria-modal=true]'));
     controls.push({id,label,role,enabled:!e.disabled&&e.getAttribute('aria-disabled')!=='true',
       expanded,selected:e.getAttribute('aria-selected'),popup,
       ...(role==='tab'?{tab:tabObservation(e)}:{}),
       ...(role==='option'&&e.getAttribute('value')!==null?{value:e.getAttribute('value').slice(0,300)}:{})});
   });
   [...d.querySelectorAll('[role=dialog],[aria-modal=true]')].filter(visible).slice(0,8).forEach(e=>dialogs.push({label:text(e),text:String(e.innerText||'').slice(0,2000)}));
   [...d.querySelectorAll('[role=status],[role=alert],[aria-live=assertive],.lsMessageBar')].filter(visible).slice(0,15).forEach(e=>{const value=text(e);if(value)messages.push({role:e.getAttribute('role')||'message',text:value})});
   [...d.querySelectorAll('table,[role=grid],[role=treegrid]')].filter(visible).slice(0,8).forEach((e,i)=>{
     const id=di+':table:'+i,role=e.getAttribute('role')||'table';e.setAttribute?.('data-rsm-sap-table',id);
     const numeric=n=>Number.isFinite(n)?Math.max(-10000000,Math.min(10000000,Math.round(n))):0;
     const rect=e.getBoundingClientRect?.(),inside=r=>{if(!rect||!r.getBoundingClientRect)return true;const a=r.getBoundingClientRect();return a.right>rect.left&&a.left<rect.right&&a.bottom>rect.top&&a.top<rect.bottom};
     const shown=[...e.querySelectorAll('tr,[role=row]')].filter(r=>visible(r)&&inside(r)).slice(0,20);
     const rows=shown.map(r=>[...r.querySelectorAll('th,td,[role=gridcell],[role=columnheader]')].filter(c=>visible(c)&&inside(c)).slice(0,16).map(c=>String(c.innerText||'').trim().slice(0,160)));
     const index=(e,row)=>{const raw=e.getAttribute(row?'aria-rowindex':'aria-colindex')??e.getAttribute(row?'lsmatrixrowindex':'lsmatrixcolindex');return /^\d{1,7}$/.test(raw||'')&&Number(raw)<=1000000?Number(raw):null};
     const row_indices=shown.map(r=>index(r,true)).filter(v=>v!==null);
     const column_indices=[...e.querySelectorAll('[role=columnheader]')].filter(c=>visible(c)&&inside(c)).slice(0,16).map(c=>index(c,false)).filter(v=>v!==null);
     const count=axis=>{
       const raw=e.getAttribute('aria-'+axis+'count');
       if(raw===null)return {value:null,state:'unavailable'};
       if(raw==='-1')return {value:null,state:'unknown'};
       if(!/^\d{1,7}$/.test(raw)||Number(raw)>1000000)return {value:null,state:'invalid'};
       return {value:Number(raw),state:'declared'};
     };
     const provenance=(items,row)=>{
       const aria=row?'aria-rowindex':'aria-colindex',sap=row?'lsmatrixrowindex':'lsmatrixcolindex';
       const sources=items.map(c=>{const a=c.getAttribute(aria),s=c.getAttribute(sap);
         if(a!==null)return /^\d{1,7}$/.test(a)&&Number(a)>=1&&Number(a)<=1000000?'aria':'invalid';
         return /^\d{1,7}$/.test(s||'')&&Number(s)<=1000000?'sap_lsmatrix':'unavailable';
       });
       const source=!sources.length?'unavailable':sources.every(s=>s===sources[0])?sources[0]:'mixed';
       return {source,base:source==='aria'?1:null};
     };
     const row_count=count('row'),column_count=count('col');
     const structure={count_source:'aria',row_count:row_count.value,row_count_state:row_count.state,
       column_count:column_count.value,column_count_state:column_count.state,row_index:provenance(shown,true),
       column_index:provenance([...e.querySelectorAll('[role=columnheader]')].filter(c=>visible(c)&&inside(c)).slice(0,16),false),
       complete:false,pagination_supported:false};
     let hash=2166136261;for(const c of JSON.stringify([row_indices,column_indices,rows]))hash=Math.imul(hash^c.charCodeAt(0),16777619)>>>0;
     const valid=['scrollTop','scrollLeft','clientWidth','clientHeight','scrollWidth','scrollHeight'].every(k=>Number.isFinite(e[k])&&Math.abs(e[k])<=10000000);
     const viewport={top:numeric(e.scrollTop),left:numeric(e.scrollLeft),width:numeric(e.clientWidth),height:numeric(e.clientHeight),
       scroll_width:numeric(e.scrollWidth),scroll_height:numeric(e.scrollHeight),row_indices,column_indices,row_signature:hash.toString(16).padStart(8,'0'),valid};
     const native_axes=[],directions=[];
     if(viewport.scroll_height>viewport.height)native_axes.push('vertical');
     if(viewport.scroll_width>viewport.width)native_axes.push('horizontal');
     const native_id=String(e.id||'').slice(0,200),ready=valid&&viewport.width>0&&viewport.height>0&&['grid','treegrid'].includes(role)&&native_id&&String(e.id).length<=200&&!e.disabled&&e.getAttribute('aria-disabled')!=='true';
     if(ready){
       if(native_axes.includes('vertical')){if(viewport.top>0)directions.push('up');if(viewport.top<viewport.scroll_height-viewport.height)directions.push('down')}
       else if(shown.length)directions.push('up','down');
       if(native_axes.includes('horizontal')){if(viewport.left>0)directions.push('left');if(viewport.left<viewport.scroll_width-viewport.width)directions.push('right')}
       else if(shown.length)directions.push('left','right');
     }
     tables.push({id,native_id,role,label:String(e.getAttribute('aria-label')||e.title||'').trim().slice(0,100),rows,viewport,structure,
       scroll:{mode:'bounded_wheel',directions,native_axes,step_pixels:{vertical:320,horizontal:240},requires_observation:true}});
   });
 });
 tables.sort((a,b)=>Number(Boolean(b.scroll.directions.length))-Number(Boolean(a.scroll.directions.length)));
 const result={origin:location.origin,title:document.title.slice(0,300),login:false,fields:fields.slice(0,160),controls:controls.slice(0,200),
   dialogs:dialogs.slice(0,8),messages:messages.slice(0,15),tables:tables.slice(0,8),text:docs.map(d=>d.body?.innerText||'').join('\n').slice(0,12000)};
 if(docs.length>=24||fields.length>160||controls.length>200||dialogs.length>8||messages.length>15||tables.length>8)result.truncated=true;
 while(JSON.stringify(result).length>48000){result.truncated=true;
   const verboseTable=result.tables.find(t=>t.rows.length);
   if(verboseTable)verboseTable.rows=[];
   else if(result.text.length>4000)result.text=result.text.slice(0,4000);
   else if(result.tables.length>4)result.tables.pop();
   else if(result.fields.length&&JSON.stringify(result.fields).length>=JSON.stringify(result.controls).length)result.fields.pop();
   else if(result.controls.length)result.controls.pop();
   else if(result.text.length)result.text='';
   else if(result.messages.length)result.messages.pop();
   else if(result.dialogs.length)result.dialogs.pop();
   else if(result.tables.length)result.tables.pop();else break;}
 return result;
})()"""

# Write only the same field semantics that were observed. SAP focus handlers
# may replace editors or change the screen, so repeat every check after focus
# before using a native setter. No caller-controlled selector or script.
FIELD_WRITE = r"""((p)=>{
 try{
   if(!p||typeof p.title!=='string'||!p.field||typeof p.field.id!=='string'||typeof p.value!=='string'||p.value.length>300||/[\r\n\x00]/.test(p.value)||typeof p.command!=='boolean')return false;
   const visible=e=>Boolean(e.getClientRects().length)&&e.ownerDocument.defaultView.getComputedStyle(e).visibility!=='hidden';
   const text=e=>String(e.getAttribute('aria-label')||e.title||e.innerText||e.value||'').trim().slice(0,240);
   const frames=(e,focused)=>{
     if(focused&&e.ownerDocument.activeElement!==e)return false;
     let w=e.ownerDocument.defaultView,depth=0;
     while(w!==window){if(++depth>24)return false;const f=w.frameElement;
       if(!f||!visible(f)||(focused&&f.ownerDocument.activeElement!==f))return false;
       w=f.ownerDocument.defaultView;
     }return true;
   };
   const resolve=()=>{
     const docs=[];const walk=d=>{if(docs.length>=24)return;docs.push(d);for(const f of d.querySelectorAll('iframe,frame')){try{if(f.contentDocument)walk(f.contentDocument)}catch(_){}}};walk(document);
     if(document.title!==p.title||docs.some(d=>[...d.querySelectorAll('[role=dialog],[aria-modal=true],input[type=password]')].some(visible)))return null;
     const matches=docs.flatMap(d=>[...d.querySelectorAll('[data-rsm-sap-field]')]).filter(e=>e.getAttribute('data-rsm-sap-field')===p.field.id);
     if(matches.length!==1)return null;const e=matches[0],di=docs.indexOf(e.ownerDocument),field=p.field;
     if(!['INPUT','TEXTAREA','SELECT'].includes(e.tagName)||e.disabled||e.readOnly||e.getAttribute('aria-readonly')==='true'||e.getAttribute('aria-disabled')==='true'||!visible(e)||!frames(e,false)||field.editable!==true||field.command!==p.command)return null;
     if(['password','hidden','submit','button','file','checkbox','radio'].includes(e.type))return null;
     if(field.type==='sap_grid'){
       if(p.command||e.tagName!=='INPUT'||e.type!=='text'||field.input_type!=='text'||e.getAttribute('ct')!=='CBS'||field.id!==di+':grid:'+e.id)return null;
       const cell=e.closest('[role=gridcell]'),grid=cell?.closest('[role=grid],[role=treegrid],[role=table]');
       if(!cell||!grid||[e,cell,grid].some(n=>!visible(n)||n.disabled||n.readOnly||n.getAttribute('aria-readonly')==='true'||n.getAttribute('aria-disabled')==='true'))return null;
       const meta=JSON.parse(cell.getAttribute('lsdata')||'{}'),editor=JSON.parse(e.getAttribute('lsdata')||'{}');
       if(editor['1']!=='FREETEXT'||meta['2']!=='EDIT')return null;
       const row=cell.getAttribute('lsmatrixrowindex'),column=cell.getAttribute('lsmatrixcolindex');
       const header=[...grid.querySelectorAll('[role=columnheader]')].find(h=>h.getAttribute('lsmatrixcolindex')===column);
       if(!header||!visible(header)||!/^\d+$/.test(row||'')||!Number.isSafeInteger(Number(row))||Number(row)!==field.row)return null;
       const label=text(header);
       if(/password|passwd|token|secret|credential|密码|密碼/i.test(label+' '+e.id)||(label+' [row '+row+']').slice(0,200)!==field.label||label.slice(0,200)!==field.column||String(e.value??e.innerText??'').trim().slice(0,300)!==field.value)return null;
     }else{
       if(e.getAttribute('ct')==='CBS'&&e.closest('[role=gridcell]'))return null;
       const name=[e.id,e.name,e.getAttribute('aria-label'),e.getAttribute('autocomplete')].join(' ');
       const index=[...e.ownerDocument.querySelectorAll('input,textarea,select')].filter(visible).indexOf(e);
       const label=e.getAttribute('aria-label')||e.title||[...(e.labels||[])].map(l=>l.innerText).join(' ')||e.id||e.name;
       if(field.id!==di+':'+index||/password|passwd|sap-user|sap-client|token|secret|username|logon|credential/i.test(name)||String(label||'').slice(0,200)!==field.label||e.tagName.toLowerCase()!==field.type||(e.type||'')!==field.input_type||(/okcd|okcode/i).test(name)!==field.command||String(e.value||'').slice(0,300)!==field.value)return null;
       if(p.command&&(e.tagName!=='INPUT'||!(/okcd|okcode/i).test(e.id+' '+e.name)))return null;
     }
     if(e.tagName==='SELECT'){
       if(!Array.isArray(field.options)||!field.options.some(o=>o.value===p.value&&!o.disabled)||![...e.options].some(o=>o.value===p.value&&!o.disabled&&!o.closest?.('optgroup')?.disabled))return null;
     }
     return e;
   };
   const e=resolve();if(!e)return false;const ownerDocument=e.ownerDocument,owner=ownerDocument.defaultView;
   const proto=e.tagName==='TEXTAREA'?owner.HTMLTextAreaElement.prototype:e.tagName==='SELECT'?owner.HTMLSelectElement.prototype:owner.HTMLInputElement.prototype;
   const setter=Object.getOwnPropertyDescriptor(proto,'value')?.set;if(!setter)return false;
   // A detached input verifies that the native type accepts the exact value
   // (for example, invalid dates and numbers otherwise silently become '').
   if(e.tagName==='INPUT'){
     const probe=e.ownerDocument.createElement('input');probe.type=e.type;
     setter.call(probe,p.value);if(probe.value!==p.value)return false;
   }
   e.focus();if(resolve()!==e||e.ownerDocument!==ownerDocument||!frames(e,true))return false;
   setter.call(e,p.value);if(e.value!==p.value)return false;
   e.dispatchEvent(new owner.Event('input',{bubbles:true}));e.dispatchEvent(new owner.Event('change',{bubbles:true}));
   if(!p.command)e.blur();return true;
 }catch(_){return false;}
})"""

# A target is found only by an opaque ID emitted by the current observation.
# Focus + CDP input produces the SAP keyboard/mouse events, including frames.
TARGET = r"""((p)=>{
 const docs=[];const walk=d=>{if(docs.length>=24)return;docs.push(d);for(const f of d.querySelectorAll('iframe,frame')){try{if(f.contentDocument)walk(f.contentDocument)}catch(_){}}};walk(document);
 const attr=p.field?'data-rsm-sap-field':'data-rsm-sap-control';
 const e=docs.flatMap(d=>[...d.querySelectorAll('['+attr+']')]).find(e=>e.getAttribute(attr)===p.id);
 if(!e||e.disabled||e.getAttribute('aria-disabled')==='true'||!e.getClientRects().length||e.type==='password')return null;
 const label=()=>String(e.getAttribute('aria-label')||e.title||e.innerText||e.value||'').trim().slice(0,240);
 if(p.label!==undefined&&label()!==p.label)return null;
 e.scrollIntoView({block:'nearest',inline:'nearest'});if(p.focus!==false)e.focus();
 if(p.label!==undefined&&label()!==p.label)return null;
 let r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2,w=e.ownerDocument.defaultView;
 while(w!==window){const f=w.frameElement;if(!f)return null;const a=f.getBoundingClientRect();x+=a.left+f.clientLeft;y+=a.top+f.clientTop;w=f.ownerDocument.defaultView;}
 return {x,y,nativeId:e.id};
})"""

# Semantic key dispatch (F4/tree arrows) must retain focus on the observed
# target after focus handlers, through every containing frame.
KEY_TARGET_READY = r"""((p)=>{
 const docs=[];const walk=d=>{if(docs.length>=24)return;docs.push(d);for(const f of d.querySelectorAll('iframe,frame')){try{if(f.contentDocument)walk(f.contentDocument)}catch(_){}}};walk(document);
 const visible=e=>Boolean(e.getClientRects().length)&&e.ownerDocument.defaultView.getComputedStyle(e).visibility!=='hidden';
 if(docs.some(d=>[...d.querySelectorAll('[role=dialog],[aria-modal=true],input[type=password]')].some(visible)))return false;
 const attr=p.field?'data-rsm-sap-field':'data-rsm-sap-control';
 const e=docs.flatMap(d=>[...d.querySelectorAll('['+attr+']')]).find(e=>e.getAttribute(attr)===p.id);
 if(!e||e.disabled||e.getAttribute('aria-disabled')==='true'||!visible(e)||e.type==='password'||e.ownerDocument.activeElement!==e)return false;
 if(p.field&&(!['INPUT','TEXTAREA','SELECT'].includes(e.tagName)||e.readOnly||(/okcd|okcode/i).test(e.id+' '+e.name)))return false;
 if(!p.field&&String(e.getAttribute('aria-label')||e.title||e.innerText||e.value||'').trim().slice(0,240)!==p.label)return false;
 let w=e.ownerDocument.defaultView;while(w!==window){const f=w.frameElement;if(!f||f.ownerDocument.activeElement!==f)return false;w=f.ownerDocument.defaultView;}
 return true;
})"""

# A grid activation may replace its span. Resolve only that same native ID,
# never an arbitrary active input (which could be a login or another cell).
# The initial click is also bounded to the observed grid cell, before SAP has
# replaced its span. It cannot focus/scroll the page or hit another control.
GRID_TARGET = r"""((p)=>{
 try{
   if(!p||typeof p.title!=='string'||!p.field||p.field.type!=='sap_grid'||p.field.input_type!=='text'||p.field.command!==false||p.field.editable!==true||typeof p.field.id!=='string')return null;
   const docs=[];const walk=d=>{if(docs.length>=24)return;docs.push(d);for(const f of d.querySelectorAll('iframe,frame')){try{if(f.contentDocument)walk(f.contentDocument)}catch(_){}}};walk(document);
   const visible=e=>Boolean(e.getClientRects().length)&&e.ownerDocument.defaultView.getComputedStyle(e).visibility!=='hidden';
   if(document.title!==p.title||docs.some(d=>[...d.querySelectorAll('[role=dialog],[aria-modal=true],input[type=password]')].some(visible)))return null;
   const matches=docs.flatMap(d=>[...d.querySelectorAll('[data-rsm-sap-field]')]).filter(e=>e.getAttribute('data-rsm-sap-field')===p.field.id);
   if(matches.length!==1)return null;const e=matches[0],field=p.field;
   if(!['SPAN','INPUT'].includes(e.tagName)||(e.tagName==='INPUT'&&e.type!=='text')||!e.id||field.id!==docs.indexOf(e.ownerDocument)+':grid:'+e.id||e.getAttribute('ct')!=='CBS'||!visible(e))return null;
   const cell=e.closest('[role=gridcell]'),grid=cell?.closest('[role=grid],[role=treegrid],[role=table]');
   if(!cell||!grid||[e,cell,grid].some(n=>!visible(n)||n.disabled||n.readOnly||n.getAttribute('aria-readonly')==='true'||n.getAttribute('aria-disabled')==='true'))return null;
   const meta=JSON.parse(cell.getAttribute('lsdata')||'{}'),editor=JSON.parse(e.getAttribute('lsdata')||'{}');
   if(meta['2']!=='EDIT'||editor['1']!=='FREETEXT')return null;
   const row=cell.getAttribute('lsmatrixrowindex'),column=cell.getAttribute('lsmatrixcolindex');
   const header=[...grid.querySelectorAll('[role=columnheader]')].find(h=>h.getAttribute('lsmatrixcolindex')===column);
   if(!header||!visible(header)||!/^\d+$/.test(row||'')||!Number.isSafeInteger(Number(row))||Number(row)!==field.row)return null;
   const label=String(header.getAttribute('aria-label')||header.title||header.innerText||header.value||'').trim().slice(0,240);
   if(/password|passwd|token|secret|credential|密码|密碼/i.test(label+' '+e.id)||(label+' [row '+row+']').slice(0,200)!==field.label||label.slice(0,200)!==field.column||String(e.value??e.innerText??'').trim().slice(0,300)!==field.value)return null;
   const rect=e.getBoundingClientRect();let x=(rect.left+rect.right)/2,y=(rect.top+rect.bottom)/2,w=e.ownerDocument.defaultView;
   if(![rect.left,rect.top,rect.right,rect.bottom,x,y,w.innerWidth,w.innerHeight].every(Number.isFinite)||rect.right<=rect.left||rect.bottom<=rect.top||x<0||y<0||x>=w.innerWidth||y>=w.innerHeight)return null;
   const hit=e.ownerDocument.elementFromPoint(x,y);let inside=false;
   for(let n=hit;n;n=n.parentElement){
     if(n===cell){inside=true;break;}
     if(n!==e&&(['INPUT','TEXTAREA','SELECT','BUTTON','A'].includes(n.tagName)||['button','link','combobox','textbox','listbox','option','tab','slider','checkbox','radio','switch','spinbutton'].includes(n.getAttribute('role'))||n.getAttribute('ct')==='CBS'||n.getAttribute('data-rsm-sap-field')!==null||n.isContentEditable||n.getAttribute('contenteditable')==='true'))return null;
   }
   if(!inside)return null;
   let depth=0;while(w!==window){
     if(++depth>24)return null;const f=w.frameElement;if(!f||!visible(f))return null;
     const a=f.getBoundingClientRect(),parent=f.ownerDocument.defaultView,style=parent.getComputedStyle(f);
     if((style.transform&&style.transform!=='none')||(Number.isFinite(f.offsetWidth)&&Math.abs(a.right-a.left-f.offsetWidth)>1)||(Number.isFinite(f.offsetHeight)&&Math.abs(a.bottom-a.top-f.offsetHeight)>1))return null;
     x+=a.left+f.clientLeft;y+=a.top+f.clientTop;
     if(![x,y,parent.innerWidth,parent.innerHeight].every(Number.isFinite)||x<0||y<0||x>=parent.innerWidth||y>=parent.innerHeight||f.ownerDocument.elementFromPoint(x,y)!==f)return null;
     w=parent;
   }
   return {x,y,nativeId:e.id};
 }catch(_){return null;}
})"""

GRID_READY = r"""((p)=>{
 try{
 const docs=[];const walk=d=>{if(docs.length>=24)return;docs.push(d);for(const f of d.querySelectorAll('iframe,frame')){try{if(f.contentDocument)walk(f.contentDocument)}catch(_){}}};walk(document);
 const matches=docs.flatMap(d=>[...d.querySelectorAll('[ct=CBS]')]).filter(e=>e.id===p.nativeId);
 if(matches.length!==1)return false;const e=matches[0];
 const visible=n=>Boolean(n.getClientRects().length)&&n.ownerDocument.defaultView.getComputedStyle(n).visibility!=='hidden';
 if(e.tagName!=='INPUT'||e.type!=='text'||e.getAttribute('ct')!=='CBS')return false;
 const cell=e.closest('[role=gridcell]'),grid=cell?.closest('[role=grid],[role=treegrid],[role=table]');
 if(!cell||!grid||[e,cell,grid].some(n=>!visible(n)||n.disabled||n.readOnly||n.getAttribute('aria-readonly')==='true'||n.getAttribute('aria-disabled')==='true'))return false;
 if(JSON.parse(cell.getAttribute('lsdata')||'{}')['2']!=='EDIT'||JSON.parse(e.getAttribute('lsdata')||'{}')['1']!=='FREETEXT')return false;
 e.setAttribute('data-rsm-sap-field',p.id);return true;
 }catch(_){return false;}
})"""

# Input/change handlers may move focus after the command field is written.
# Enter is permitted only in that same visible SAP command field, with no
# modal dialog and with its complete frame focus chain still active.
COMMAND_READY = r"""((p)=>{
 const docs=[];const walk=d=>{if(docs.length>=24)return;docs.push(d);for(const f of d.querySelectorAll('iframe,frame')){try{if(f.contentDocument)walk(f.contentDocument)}catch(_){}}};walk(document);
 const visible=e=>Boolean(e.getClientRects().length)&&e.ownerDocument.defaultView.getComputedStyle(e).visibility!=='hidden';
 if(docs.some(d=>[...d.querySelectorAll('[role=dialog],[aria-modal=true],input[type=password]')].some(visible)))return false;
 const e=docs.flatMap(d=>[...d.querySelectorAll('[data-rsm-sap-field]')]).find(e=>e.getAttribute('data-rsm-sap-field')===p.id);
 if(!e||e.tagName!=='INPUT'||e.type==='password'||e.disabled||e.readOnly||!visible(e)||!(/okcd|okcode/i).test(e.id+' '+e.name)||e.value!==p.value)return false;
 if(e.ownerDocument.activeElement!==e)return false;
 let w=e.ownerDocument.defaultView;while(w!==window){const f=w.frameElement;if(!f||f.ownerDocument.activeElement!==f)return false;w=f.ownerDocument.defaultView;}
 return true;
})"""

# Escape only closes the unique value-help dialog opened by this controller.
# A different/extra dialog or focus outside it requires manual handling.
VALUE_HELP_READY = r"""((p)=>{
 const docs=[];const walk=d=>{if(docs.length>=24)return;docs.push(d);for(const f of d.querySelectorAll('iframe,frame')){try{if(f.contentDocument)walk(f.contentDocument)}catch(_){}}};walk(document);
 const visible=e=>Boolean(e.getClientRects().length)&&e.ownerDocument.defaultView.getComputedStyle(e).visibility!=='hidden';
 const all=docs.flatMap(d=>[...d.querySelectorAll('[role=dialog],[aria-modal=true]')]).filter(visible);
 if(all.length!==1)return false;const d=all[0];
 const label=String(d.getAttribute('aria-label')||d.title||d.innerText||d.value||'').trim().slice(0,240);
 if(label!==p.label||String(d.innerText||'').slice(0,2000)!==p.text||!d.contains(d.ownerDocument.activeElement))return false;
 let w=d.ownerDocument.defaultView;while(w!==window){const f=w.frameElement;if(!f||f.ownerDocument.activeElement!==f)return false;w=f.ownerDocument.defaultView;}
 return true;
})"""

# A fixed wheel targets only the observed grid's visible, unobstructed point.
# Never scroll/focus the document to manufacture a target, or accept selectors.
SCROLL_TARGET = r"""((p)=>{
 const docs=[];const walk=d=>{if(docs.length>=24)return;docs.push(d);for(const f of d.querySelectorAll('iframe,frame')){try{if(f.contentDocument)walk(f.contentDocument)}catch(_){}}};walk(document);
 const visible=e=>Boolean(e.getClientRects().length)&&e.ownerDocument.defaultView.getComputedStyle(e).visibility!=='hidden';
 if(document.title!==p.title||!['创建采购订单','Create Purchase Order'].includes(document.title.trim()))return null;
 if(docs.some(d=>[...d.querySelectorAll('[role=dialog],[aria-modal=true],input[type=password]')].some(visible)))return null;
 const e=docs.flatMap(d=>[...d.querySelectorAll('[data-rsm-sap-table]')]).find(e=>e.getAttribute('data-rsm-sap-table')===p.id);
 if(!e||!p.native_id||e.id!==p.native_id||e.getAttribute('role')!==p.role||!['grid','treegrid'].includes(p.role)||!visible(e)||e.disabled||e.getAttribute('aria-disabled')==='true'||e.isContentEditable||e.getAttribute('contenteditable')==='true'||['INPUT','TEXTAREA','SELECT'].includes(e.tagName))return null;
 if(String(e.getAttribute('aria-label')||e.title||'').trim().slice(0,100)!==p.label)return null;
 if(!['scrollTop','scrollLeft','clientWidth','clientHeight','scrollWidth','scrollHeight'].every(k=>Number.isFinite(e[k])&&Math.abs(e[k])<=10000000)||e.clientWidth<=0||e.clientHeight<=0||e.scrollWidth<0||e.scrollHeight<0)return null;
 if(!p.viewport||[['scrollTop','top'],['scrollLeft','left'],['clientWidth','width'],['clientHeight','height'],['scrollWidth','scroll_width'],['scrollHeight','scroll_height']].some(([native,key])=>Math.round(e[native])!==p.viewport[key]))return null;
 if(!['up','down','left','right'].includes(p.direction))return null;
 if(e.scrollHeight>e.clientHeight&&((p.direction==='up'&&e.scrollTop<=0)||(p.direction==='down'&&e.scrollTop>=e.scrollHeight-e.clientHeight)))return null;
 if(e.scrollWidth>e.clientWidth&&((p.direction==='left'&&e.scrollLeft<=0)||(p.direction==='right'&&e.scrollLeft>=e.scrollWidth-e.clientWidth)))return null;
 const r=e.getBoundingClientRect(),v=e.ownerDocument.defaultView;
 if(![r.left,r.top,r.right,r.bottom,v.innerWidth,v.innerHeight].every(Number.isFinite))return null;
 const left=Math.max(0,r.left),top=Math.max(0,r.top),right=Math.min(v.innerWidth,r.right),bottom=Math.min(v.innerHeight,r.bottom);
 if(right-left<2||bottom-top<2)return null;
 const inside=c=>{const a=c.getBoundingClientRect();return a.right>r.left&&a.left<r.right&&a.bottom>r.top&&a.top<r.bottom};
 const index=(c,row)=>{const raw=c.getAttribute(row?'aria-rowindex':'aria-colindex')??c.getAttribute(row?'lsmatrixrowindex':'lsmatrixcolindex');return /^\d{1,7}$/.test(raw||'')&&Number(raw)<=1000000?Number(raw):null};
 const rows=[...e.querySelectorAll('tr,[role=row]')].filter(c=>visible(c)&&inside(c)).slice(0,20).map(c=>index(c,true)).filter(n=>n!==null);
 const columns=[...e.querySelectorAll('[role=columnheader]')].filter(c=>visible(c)&&inside(c)).slice(0,16).map(c=>index(c,false)).filter(n=>n!==null);
 if(JSON.stringify(rows)!==JSON.stringify(p.row_indices)||JSON.stringify(columns)!==JSON.stringify(p.column_indices))return null;
 const unsafe=hit=>{let eligible=hit===e;for(let n=hit;n&&n!==e;n=n.parentElement){
   if(['INPUT','SELECT','TEXTAREA','BUTTON','A'].includes(n.tagName)||['button','spinbutton','combobox','textbox','listbox','option','tab','link','slider','checkbox','radio','switch'].includes(n.getAttribute('role'))||n.isContentEditable||n.getAttribute('contenteditable')==='true'||n.getAttribute('ct')==='CBS'||n.getAttribute('data-rsm-sap-field')!==null)return true;
   if(n.getAttribute('role')==='gridcell'){let meta;try{meta=JSON.parse(n.getAttribute('lsdata')||'{}')}catch(_){return true}
     if(n.getAttribute('aria-readonly')!=='true'&&meta['2']!=='READONLY')return true;
     eligible=true;
   }
   if(n.getAttribute('role')==='columnheader'||['TH','TD'].includes(n.tagName))eligible=true;
 }return !eligible};
 // Nine fixed candidates; never focus an editor or scroll the enclosing page.
 for(const [fx,fy] of [[.5,.5],[.5,.1],[.1,.1],[.9,.1],[.1,.5],[.9,.5],[.1,.9],[.5,.9],[.9,.9]]){
   let x=left+(right-left)*fx,y=top+(bottom-top)*fy;
   const hit=e.ownerDocument.elementFromPoint(x,y);if(!hit||(hit!==e&&!e.contains(hit))||unsafe(hit))continue;
   let w=v,valid=true,depth=0;while(w!==window){if(++depth>24){valid=false;break}const f=w.frameElement;if(!f||!visible(f)){valid=false;break}const a=f.getBoundingClientRect(),style=f.ownerDocument.defaultView.getComputedStyle(f);
     if((style.transform&&style.transform!=='none')||(Number.isFinite(f.offsetWidth)&&Math.abs(a.right-a.left-f.offsetWidth)>1)||(Number.isFinite(f.offsetHeight)&&Math.abs(a.bottom-a.top-f.offsetHeight)>1)){valid=false;break}
     x+=a.left+f.clientLeft;y+=a.top+f.clientTop;const parent=f.ownerDocument.defaultView;
     if(![x,y,parent.innerWidth,parent.innerHeight].every(Number.isFinite)||x<0||y<0||x>=parent.innerWidth||y>=parent.innerHeight||f.ownerDocument.elementFromPoint(x,y)!==f){valid=false;break}
     w=parent;
   }
   if(valid)return {x,y};
 }
 return null;
})"""
