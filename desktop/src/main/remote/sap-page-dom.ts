// Fixed script only. No selectors or JavaScript supplied by the caller.
export const SAP_PAGE_DOM = String.raw`(() => {
  const limits = new Set(['当前已渲染 DOM；不包含未加载的行、隐藏页签或完整业务单据。']);
  const clip = (s, n = 1000) => { s = String(s ?? '').trim(); if (s.length > n) limits.add('部分文本已截断。'); return s.slice(0, n); };
  const nodes = [], walk = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_ELEMENT);
  let node;
  while ((node = walk.nextNode()) && nodes.length < 12000) nodes.push(node);
  if (node) limits.add('页面节点超过读取上限。');
  const shown = e => e.getClientRects().length > 0 && !e.closest('[hidden],[aria-hidden="true"],script,style,template') &&
    !['hidden','collapse'].includes(getComputedStyle(e).visibility);
  const secret = e => /password|passwd|token|secret|credential|sap-user|sap-client|username|logon|密码|密碼|口令/i.test(
    [e.id, e.name, e.getAttribute('autocomplete'), e.getAttribute('aria-label')].join(' '));
  if (nodes.some(e => shown(e) && (e.matches('input[type=password]') ||
      (e.matches('input') && /sap-user|logonuser|j_username/i.test(e.id + ' ' + e.name))))) return {error: 'login_required'};
  const sensitive = new WeakMap();
  const secretTree = e => {
    if (!e) return false;
    if (!sensitive.has(e)) sensitive.set(e, secret(e) || e.matches('[data-secret],input[type=password]') || secretTree(e.parentElement));
    return sensitive.get(e);
  };
  const safe = e => shown(e) && !secretTree(e);
  const text = e => {
    const parts = [], walker = document.createTreeWalker(e, NodeFilter.SHOW_TEXT);
    let item, count = 0, length = 0;
    while ((item = walker.nextNode()) && ++count < 1000 && length < 1000) {
      if (!item.parentElement || !safe(item.parentElement)) continue;
      const s = String(item.textContent || '').trim(); if (s) {parts.push(s);length += s.length + 1;}
    }
    if (item) limits.add('部分文本已截断。');
    return clip(parts.join(' '));
  };
  const label = e => {
    const refs = (e.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean).slice(0, 4);
    return clip(e.getAttribute('aria-label') || refs.map(id => {const n=document.getElementById(id);return n && safe(n) ? text(n) : ''}).join(' ') ||
      [...(e.labels || [])].filter(safe).map(text).join(' ') || e.title || e.id || e.name || '', 200);
  };
  const value = e => clip(e.matches('input,textarea,select') ? (e.type === 'checkbox' || e.type === 'radio' ? String(e.checked) : e.value) : text(e));
  // Session identity is narrower than general page text. A document creator,
  // SU01 user field or remembered login must never become the current user.
  const identityKey = name => ({
    'user':'account', 'username':'account', 'currentuser':'account', 'loggedonuser':'account',
    '用户':'account', '用户名':'account', '当前用户':'account', '登录用户':'account',
    '用戶':'account', '用戶名稱':'account', '使用者':'account',
    'client':'client', '客户端':'client', '集团':'client', '集團':'client', '用戶端':'client', 'mandant':'client',
    'system':'systemId', 'systemid':'systemId', '系统':'systemId', '系统标识':'systemId', '系統':'systemId',
    'syst-uname':'account', 'sy-uname':'account', 'syst-mandt':'client', 'sy-mandt':'client',
    'syst-sysid':'systemId', 'sy-sysid':'systemId'
  })[String(name).toLowerCase().replace(/[\s:：]/g, '')];
  const identityScopes = nodes.filter(e => safe(e) && (
    /^(?:sap[-_:]?)?(?:statusbar|systeminfo|sessioninfo)(?:[-_:].*)?$/i.test(e.id) ||
    /\/sbar$/.test(e.id) ||
    (e.matches('[role=dialog],[role=group],fieldset,section,[role=status]') &&
      /^(?:system\s*:?\s*(?:status|information)|session information|系统[：:]?\s*(?:状态|信息)|系統[：:]?\s*(?:狀態|資訊))$/i.test(
        e.getAttribute('aria-label') || (e.hasAttribute('aria-labelledby') ? label(e) : '') || e.title ||
        text(e.querySelector('legend,h1,h2,[role=heading]') || e).slice(0, 100)))
  ));
  const identities = [];
  let identityConflict = false;
  const readonly = e => e.matches('input,textarea') && (e.readOnly || e.disabled || e.getAttribute('aria-readonly') === 'true');
  const technicalKey = e => ({
    'ABAP 系统字段：当前用户的名称':'account', 'ABAP System Field: Name of Current User':'account',
    'ABAP 系统字段：当前用户的客户端标识':'client', 'ABAP System Field: Client ID of Current User':'client',
    'ABAP 系统字段：SAP 系统标识':'systemId', 'ABAP System Field: SAP System ID':'systemId'
  })[String(e.title || e.getAttribute('aria-description') || '').trim()];
  // SAP System: Status also contains a *database* user. Only its Usage Data
  // section or explicit ABAP current-user tooltips identify the logon user.
  const usageSections = nodes.filter(e => safe(e) && e.matches('[role=group],fieldset,section') &&
    /^(?:usage data|使用数据|使用資料|使用者資料)$/i.test(e.getAttribute('aria-label') || text(e.querySelector('legend,h1,h2,[role=heading]') || e)));
  for (const scope of [...identityScopes.slice(0, 8), null]) {
    const found = {account:new Set(), client:new Set(), systemId:new Set()};
    const add = (name, raw) => {
      const key = identityKey(name) || (['account','client','systemId'].includes(name) ? name : null), v = String(raw || '').trim();
      if (!key || !v || v.length > 128 || /[\s<>\x00-\x1f\x7f]/.test(v)) return;
      if (key === 'client' && !/^\d{3}$/.test(v)) return;
      if (key === 'systemId' && !/^[A-Za-z0-9]{3}$/.test(v)) return;
      found[key].add(v);
    };
    const systemStatus = scope && /^(?:system\s*:?\s*status|系统[：:]?\s*状态|系統[：:]?\s*狀態)$/i.test(
      scope.getAttribute('aria-label') || (scope.hasAttribute('aria-labelledby') ? label(scope) : '') || scope.title || text(scope.querySelector('h1,h2,[role=heading]') || scope));
    const inside = nodes.filter(e => safe(e) && (scope ? scope.contains(e) && (!systemStatus || usageSections.some(section => scope.contains(section) && section.contains(e))) : readonly(e) && technicalKey(e)));
    for (const e of inside) {
      if (readonly(e)) add(technicalKey(e) || label(e), value(e));
      if (e.matches('tr,[role=row]')) {
        const cells = [...e.children].filter(safe);
        if (cells.length === 2) {
          const editor = cells[1].querySelector('input,textarea,select');
          if (!editor) add(text(cells[0]), text(cells[1]));
        }
      }
      if (e.matches('dt') && e.nextElementSibling?.matches('dd') && safe(e.nextElementSibling)) add(text(e), text(e.nextElementSibling));
      // Only complete, explicitly labelled status items; never mine prose.
      if (!e.children.length && !e.matches('input,textarea,select')) {
        for (const item of (e.getAttribute('aria-label') || text(e)).split(/[\n|;]/).slice(0, 12)) {
          const pair = item.match(/^\s*([^:：]{1,40})[:：]\s*([^\s:：]{1,128})\s*$/);
          if (pair) add(pair[1], pair[2]);
        }
      }
    }
    if (Object.values(found).some(values => values.size > 1)) {identityConflict = true; continue;}
    if (found.account.size) identities.push({account:[...found.account][0], client:[...found.client][0] || null,
      systemId:[...found.systemId][0] || null, source:'sap_session_ui'});
  }
  if (['account','client','systemId'].some(key => new Set(identities.map(i => i[key]).filter(Boolean)).size > 1)) identityConflict = true;
  const currentUser = identityConflict ? null : identities.sort((a,b) => Number(!!b.client)+Number(!!b.systemId)-Number(!!a.client)-Number(!!a.systemId))[0] || null;
  if (!currentUser) limits.add(identityConflict ? '页面会话用户信息冲突，无法确认当前 SAP 登录账号。' : '页面未显示可确认的 SAP 会话用户信息；当前账号未知，不使用已保存账号或业务字段推断。');
  const take = (items, count) => { if (items.length > count) limits.add('部分控件或表格超过数量上限。'); return items.slice(0, count); };
  const fields = take(nodes.filter(e => safe(e) && e.matches('input,textarea,select,[ct=CBS]') &&
    !['hidden','password','button','submit','file'].includes(e.type)), 200).map(e => ({
      label: label(e), value: value(e), readonly: Boolean(e.readOnly || e.disabled),
      ...(e.type === 'checkbox' || e.type === 'radio' ? {checked: e.checked} : {})
    }));
  const grids = take(nodes.filter(e => safe(e) && (e.matches('[role=grid],[role=treegrid],[role=table]') ||
    (e.matches('table') && !e.closest('[role=grid],[role=treegrid],[role=table]') && e.querySelector('th')))), 5);
  const tables = grids.map(grid => {
    const inside = e => grid.matches('[role=grid],[role=treegrid],[role=table]')
      ? e.closest('[role=grid],[role=treegrid],[role=table]') === grid : e.closest('table') === grid;
    const headers = take(nodes.filter(e => safe(e) && grid.contains(e) && inside(e) && e.matches('th,[role=columnheader]')), 40)
      .map(e => ({label: text(e), column: e.getAttribute('aria-colindex') || e.getAttribute('lsmatrixcolindex') || null}));
    const rows = take(nodes.filter(e => safe(e) && grid.contains(e) && inside(e) && e.matches('tr,[role=row]')), 100).map(row => ({
      index: row.getAttribute('aria-rowindex') || row.getAttribute('lsmatrixrowindex') || null,
      selected: row.getAttribute('aria-selected') === 'true',
      cells: take([...row.querySelectorAll('td,th,[role=gridcell],[role=cell],[role=columnheader]')].filter(e => safe(e) && inside(e)), 40).map(cell => {
        const editor = cell.querySelector('input,textarea,select,[ct=CBS]');
        return {column: cell.getAttribute('aria-colindex') || cell.getAttribute('lsmatrixcolindex') || null,
          value: editor && safe(editor) ? value(editor) : text(cell)};
      })
    }));
    return {label: label(grid), headers, rows, complete: false};
  });
  const selection = take(nodes.filter(e => safe(e) && e.matches('[aria-selected=true],input:checked')), 40).map(e => label(e) || text(e));
  const activeTabs = take(nodes.filter(e => safe(e) && e.matches('[role=tab][aria-selected=true]')), 20).map(text);
  const messages = take(nodes.filter(e => safe(e) && e.matches('[role=status],[role=alert],[role=dialog],[aria-modal=true],.lsMessageBar')), 20).map(text);
  const lines = [];
  const tw = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  let t, total = 0, visited = 0;
  while ((t = tw.nextNode()) && ++visited <= 16000 && total < 12000) {
    const parent = t.parentElement;
    if (!parent || !safe(parent) || parent.closest('input,textarea,select,script,style,template,[data-secret]')) continue;
    const part = clip(t.textContent, 500);
    if (part) { lines.push(part); total += part.length + 1; }
  }
  if (t) limits.add('可读文本已截断。');
  return {capturedAt: new Date().toISOString(), source: 'sap_page_dom', scope: 'rendered_dom',
    title: clip(document.title, 300), currentUser, fields, tables, selection, activeTabs, messages,
    text: lines.join('\n').slice(0, 12000), limitations: [...limits]};
})()`
