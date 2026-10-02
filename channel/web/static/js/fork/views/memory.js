// Based on master 48c0d79c36146950667f8d1884ab823deae62305; account adaptation, see change fix-account-memory-management.
/* Memory file list.
   Split out of console.js. These are classic scripts sharing one global
   scope; see channel/web/README.md before changing the load order. */

// =====================================================================
// Memory View
// =====================================================================
let memoryPage = 1;
let memoryCategory = 'memory';   // 'memory' | 'evolution'
const memoryPageSize = 10;

function switchMemoryTab(tab) {
    if (!memoryEditor.guard(() => switchMemoryTab(tab))) return;
    memoryEditor.forget();
    document.getElementById('memory-panel-viewer').classList.add('hidden');
    document.getElementById('memory-panel-list').classList.remove('hidden');
    document.querySelectorAll('.memory-tab').forEach(el => el.classList.remove('active'));
    document.getElementById('memory-tab-' + tab).classList.add('active');
    // The "dreams" tab now surfaces self-evolution logs (merged with dream diaries).
    memoryCategory = tab === 'dreams' ? 'evolution' : 'memory';
    loadMemoryView(1);
    if (typeof routeNoteTab === 'function') routeNoteTab('memory', tab);
}

function loadMemoryView(page) {
    page = page || 1;
    memoryPage = page;
    const owner = memoryOwner();
    const sequence = ++memoryLoadSequence;
    memoryShowState(t('memory_loading'), t('memory_loading_desc'), false);
    memoryAbort?.abort();
    memoryAbort = new AbortController();
    return fetch(`/api/memory?page=${page}&page_size=${memoryPageSize}&category=${memoryCategory}&scope=personal`, {signal: memoryAbort.signal}).then(r => r.json()).then(data => {
        if (!memoryCurrent(owner) || sequence !== memoryLoadSequence) return;
        if (data.status !== 'success') throw new Error(data.message || t('memory_load_failed'));
        memoryMetadata = data;
        memoryUpdateStatus();
        const emptyEl = document.getElementById('memory-empty');
        const listEl = document.getElementById('memory-list');
        const files = data.list || [];
        const total = data.total || 0;

        if (total === 0) {
            emptyEl.querySelectorAll('p')[1].textContent = t('memory_account_empty_hint');
            const emptyIcon = emptyEl.querySelector('i');
            const emptyTitle = emptyEl.querySelector('p');
            if (memoryCategory === 'evolution') {
                emptyIcon.className = 'fas fa-seedling text-emerald-400 text-xl';
                emptyTitle.textContent = t('memory_account_no_evolution');
            } else {
                emptyIcon.className = 'fas fa-brain text-purple-400 text-xl';
                emptyTitle.textContent = t('memory_account_no_files');
            }
            emptyEl.classList.remove('hidden');
            listEl.classList.add('hidden');
            return;
        }
        emptyEl.classList.add('hidden');
        listEl.classList.remove('hidden');

        const tbody = document.getElementById('memory-table-body');
        tbody.innerHTML = '';
        files.forEach(f => {
            const tr = document.createElement('tr');
            tr.className = 'border-b border-slate-100 dark:border-white/5 hover:bg-slate-50 dark:hover:bg-white/5 cursor-pointer transition-colors';
            // In the merged evolution tab, resolve each file by its own origin
            // (evolution logs vs dream diaries live in different dirs).
            const fileCategory = (f.type === 'dream' || f.type === 'evolution') ? f.type : memoryCategory;
            tr.onclick = () => openMemoryFile(f.filename, fileCategory);
            let typeLabel;
            if (f.type === 'global') {
                typeLabel = `<span class="px-2 py-0.5 rounded-full text-xs bg-primary-50 dark:bg-primary-900/30 text-primary-600 dark:text-primary-400">${t('memory_type_long_term')}</span>`;
            } else if (f.type === 'evolution') {
                typeLabel = `<span class="px-2 py-0.5 rounded-full text-xs bg-emerald-50 dark:bg-emerald-900/30 text-emerald-600 dark:text-emerald-400">${t('memory_account_type_evolution')}</span>`;
            } else if (f.type === 'dream') {
                typeLabel = `<span class="px-2 py-0.5 rounded-full text-xs bg-violet-50 dark:bg-violet-900/30 text-violet-600 dark:text-violet-400">${t('memory_account_type_dream')}</span>`;
            } else {
                typeLabel = `<span class="px-2 py-0.5 rounded-full text-xs bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400">${t('memory_account_type_daily')}</span>`;
            }
            const sizeStr = f.size < 1024 ? f.size + ' B' : (f.size / 1024).toFixed(1) + ' KB';
            tr.innerHTML = `
                <td class="px-4 py-3 text-sm font-mono text-slate-700 dark:text-slate-200">${escapeHtml(f.filename)}</td>
                <td class="px-4 py-3 text-sm">${typeLabel}</td>
                <td class="px-4 py-3 text-sm text-slate-500 dark:text-slate-400">${sizeStr}</td>
                <td class="px-4 py-3 text-sm text-slate-500 dark:text-slate-400">${escapeHtml(f.updated_at)}</td>`;
            tbody.appendChild(tr);
        });

        // Pagination
        const totalPages = Math.ceil(total / memoryPageSize);
        const pagEl = document.getElementById('memory-pagination');
        if (totalPages <= 1) { pagEl.innerHTML = ''; return; }
        let pagHtml = `<span>${page} / ${totalPages}</span><div class="flex gap-2">`;
        if (page > 1) pagHtml += `<button onclick="loadMemoryView(${page - 1})" class="px-3 py-1 rounded-lg border border-slate-200 dark:border-white/10 hover:bg-slate-100 dark:hover:bg-white/10 text-xs">Prev</button>`;
        if (page < totalPages) pagHtml += `<button onclick="loadMemoryView(${page + 1})" class="px-3 py-1 rounded-lg border border-slate-200 dark:border-white/10 hover:bg-slate-100 dark:hover:bg-white/10 text-xs">Next</button>`;
        pagHtml += '</div>';
        pagEl.innerHTML = pagHtml;
    }).catch(error => {
        if (error.name !== 'AbortError' && memoryCurrent(owner) && sequence === memoryLoadSequence)
            memoryShowState(error.message || t('memory_load_failed'), t('memory_retry_hint'), true);
    });
}


// Account-only adaptation of master's document viewer callbacks.
let memoryLoadSequence = 0;
let memoryAbort = null;
let memoryMetadata = null;
function memoryOwner() {
    return JSON.stringify([_authEpoch, sessionStorage.getItem('cow_tenant_id') || '']);
}
function memoryCurrent(owner) { return owner === memoryOwner(); }
function renderMemoryOwner() {
    const el = document.getElementById('memory-owner');
    const user = typeof _accountState !== 'undefined' ? _accountState : null;
    if (el) el.textContent = user?.displayName || user?.username || t('memory_target_personal');
}
function resetMemoryView() {
    ++memoryLoadSequence;
    memoryAbort?.abort();
    memoryMetadata = null;
    memoryEditor.forget();
    document.getElementById('memory-viewer-content')?.replaceChildren();
    document.getElementById('memory-table-body')?.replaceChildren();
    document.getElementById('memory-pagination')?.replaceChildren();
    document.getElementById('memory-panel-viewer')?.classList.add('hidden');
    document.getElementById('memory-panel-list')?.classList.remove('hidden');
    memoryUpdateStatus();
}
function memoryShowState(title, hint, retry) {
    const el = document.getElementById('memory-empty');
    if (!el) return;
    el.querySelector('p').textContent = title;
    el.querySelectorAll('p')[1].textContent = hint;
    el.querySelector('i').className = retry
        ? 'fas fa-triangle-exclamation text-amber-500 text-xl' : 'fas fa-brain text-purple-400 text-xl';
    document.getElementById('memory-list').classList.add('hidden');
    document.getElementById('memory-table-body').replaceChildren();
    document.getElementById('memory-pagination').replaceChildren();
    el.classList.remove('hidden');
}
function memoryUpdateStatus() {
    renderMemoryOwner();
    const pending = memoryMetadata?.index_state === 'pending';
    document.getElementById('memory-retry-index')?.classList.toggle('hidden', !pending);
    const status = document.getElementById('memory-status');
    if (status) status.textContent = pending ? t('memory_index_pending') : '';
    document.getElementById('memory-btn-clear')?.classList.toggle('hidden', !memoryMetadata?.actions?.clear);
}
function memoryAssertOwner(owner) {
    if (!memoryCurrent(owner)) throw new Error(t('memory_account_changed'));
}
async function memoryDocRead(doc) {
    memoryAssertOwner(doc.owner);
    const res = await fetch(`/api/memory/content?scope=personal&filename=${encodeURIComponent(doc.filename)}&category=${doc.category}`);
    const data = await res.json();
    memoryAssertOwner(doc.owner);
    if (data.status !== 'success') throw new Error(data.message || t('memory_load_failed'));
    if (!data.revision) throw new Error(t('memory_entry_missing'));
    doc.revision = data.revision;
    doc.actions = data.actions;
    return {content: data.content, mtime: data.revision, editable: !data.read_only && data.actions?.edit};
}
async function memoryRequest(path, body, owner = memoryOwner()) {
    memoryAssertOwner(owner);
    const response = await fetch(path, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({...body, scope: 'personal'}),
    });
    const data = await response.json();
    memoryAssertOwner(owner);
    if (data.status === 'pending') {
        memoryMetadata = {...memoryMetadata, index_state: 'pending'};
        memoryUpdateStatus();
        _wsToast(t('memory_index_pending'));
    }
    return data;
}
async function memoryDocWrite(doc, content, revision) {
    memoryAssertOwner(doc.owner);
    if (revision == null) revision = (await memoryDocRead(doc)).mtime;
    const data = await memoryRequest('/api/memory/save', {
        filename: doc.filename, category: doc.category, content, revision,
    }, doc.owner);
    if (data.code === 'stale_revision') return {...data, code: 'conflict'};
    if (!['success', 'pending'].includes(data.status)) return data;
    doc.revision = data.result.revision;
    return {...data, status: 'success', mtime: doc.revision};
}
const memoryEditor = createDocEditor({
    body: () => document.getElementById('memory-viewer-content'),
    buttons: () => ({edit: document.getElementById('memory-btn-edit'),
        save: document.getElementById('memory-btn-save'), cancel: document.getElementById('memory-btn-cancel')}),
    read: memoryDocRead,
    write: memoryDocWrite,
    canEdit: doc => doc.actions?.edit === true && memoryCurrent(doc.owner),
    render: doc => docRenderBody('memory-viewer-content', doc.content),
    onState: state => {
        docRenderTitle('memory-viewer-title', memoryEditor.current()?.filename, state);
        document.getElementById('memory-btn-delete')?.classList.toggle('hidden',
            !!state?.editing || !memoryEditor.current()?.actions?.delete);
    },
});
async function openMemoryFile(filename, category = 'memory') {
    if (!memoryEditor.guard(() => openMemoryFile(filename, category))) return;
    const doc = {filename, category, owner: memoryOwner()};
    const sequence = ++memoryLoadSequence;
    try {
        const data = await memoryDocRead(doc);
        if (!memoryCurrent(doc.owner) || sequence !== memoryLoadSequence) return;
        document.getElementById('memory-panel-list').classList.add('hidden');
        document.getElementById('memory-panel-viewer').classList.remove('hidden');
        memoryEditor.open({...doc, content: data.content});
    } catch (error) {
        if (memoryCurrent(doc.owner)) _wsToast(error.message);
    }
}
function closeMemoryViewer() {
    if (!memoryEditor.guard(closeMemoryViewer)) return;
    memoryEditor.forget();
    document.getElementById('memory-panel-viewer').classList.add('hidden');
    document.getElementById('memory-panel-list').classList.remove('hidden');
    loadMemoryView(memoryPage);
}
function memoryDocDelete() {
    if (!memoryEditor.guard(memoryDocDelete)) return;
    const doc = memoryEditor.current();
    if (!doc?.actions?.delete) return;
    showConfirmDialog({title: t('memory_delete_title'),
        message: t('memory_delete_msg').replace('{name}', doc.filename), okText: t('memory_delete_ok'),
        onConfirm: async () => {
            try {
                const data = await memoryRequest('/api/memory/delete', {
                    filename: doc.filename, category: doc.category, revision: doc.revision,
                }, doc.owner);
                if (!['success', 'pending'].includes(data.status)) throw new Error(data.message);
                closeMemoryViewer();
            } catch (error) { if (memoryCurrent(doc.owner)) _wsToast(error.message); }
        }});
}
function memoryDocClear() {
    if (!memoryEditor.guard(memoryDocClear) || !memoryMetadata?.actions?.clear) return;
    const owner = memoryOwner(), revision = memoryMetadata.collection_revision;
    const counts = memoryMetadata.counts || {};
    const total = Object.values(counts).reduce((sum, value) => sum + value, 0);
    const breakdown = t('memory_account_counts').replace('{global}', counts.global || 0)
        .replace('{daily}', counts.daily || 0).replace('{evolution}', counts.evolution || 0).replace('{dream}', counts.dream || 0);
    const label = document.getElementById('memory-owner')?.textContent || t('memory_target_personal');
    const tenant = sessionStorage.getItem('cow_tenant_id') || '';
    showConfirmDialog({title: t('memory_clear_all'),
        message: `${label} · ${tenant}\n${breakdown}\n${t('memory_clear_all_message').replace('{count}', total)}`,
        okText: t('memory_clear_ok'), onConfirm: async () => {
            try {
                const data = await memoryRequest('/api/memory/clear', {clear_scope: 'all_personal', revision}, owner);
                if (!['success', 'pending'].includes(data.status)) throw new Error(data.message);
                closeMemoryViewer();
            } catch (error) { if (memoryCurrent(owner)) _wsToast(error.message); }
        }});
}
async function memoryRetryIndex() {
    const owner = memoryOwner();
    try {
        const data = await memoryRequest('/api/memory/personal', {action: 'retry_index'}, owner);
        if (!['success', 'pending'].includes(data.status)) throw new Error(data.message);
        loadMemoryView(memoryPage);
    } catch (error) { if (memoryCurrent(owner)) _wsToast(error.message); }
}
