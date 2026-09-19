/* todos.js - Personal todo (待办事项) workbench view.
 *
 * Loaded alongside console.js. Provides the one-page todo list plus a shared
 * create/edit form and a detail drawer (with per-status actions and paginated
 * processing history). All authorization happens server-side; this module
 * only improves UX and handles the documented frontend failure modes
 * (create-key idempotency, 409 stale/draft compare, unknown-result GET,
 * 503 retry, and distinct empty states).
 */
(function () {
    'use strict';

    // ---- shared helpers (kept local; console.js does not export these) ----
    function liveLang() {
        // Prefer the live global (updated by console.js setLanguage); fall back
        // to the one-time snapshot set at load.
        if (typeof currentLang === 'string' && currentLang) return currentLang;
        return window.__cowLang__ || 'zh';
    }
    function t(key) {
        const lang = liveLang();
        const i18n = window.I18N || {};
        return (i18n[lang] && i18n[lang][key]) || (i18n.en && i18n.en[key]) || key;
    }

    function escapeHtml(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function formatDate(sec) {
        if (!sec) return '';
        const d = new Date(sec * 1000);
        const p = function (n) { return String(n).padStart(2, '0'); };
        return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
    }

    function makeCreateKey() {
        if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
        return 'todo-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
    }

    // ---- request helper ---------------------------------------------------
    let _generation = 0;

    async function apiFetch(path, options) {
        const opts = options || {};
        const headers = Object.assign({}, opts.headers || {});
        if (opts.body) headers['Content-Type'] = 'application/json';
        const resp = await fetch(path, {
            credentials: 'same-origin',
            method: opts.method || 'GET',
            headers: headers,
            body: opts.body ? JSON.stringify(opts.body) : undefined,
        });
        const data = await resp.json().catch(function () { return {}; });
        // Treat any non-JSON/network failure as a retryable 503 semantics.
        if (resp.status === 503) {
            const err = new Error(data.message || t('todo_load_error'));
            err.status = 503;
            err.code = data.code;
            throw err;
        }
        if (!resp.ok || data.status !== 'success') {
            const err = new Error(data.message || (data.code === 'todo_disabled' ? 'disabled' : 'load-failed'));
            err.status = resp.status;
            err.code = data.code;
            err.field = data.field;
            err.data = data;
            throw err;
        }
        return data;
    }

    // ---- toast (minimal, self-dismissing) ---------------------------------
    let _toastEl = null;
    let _toastTimer = null;
    function toast(message, type) {
        if (!_toastEl) {
            _toastEl = document.createElement('div');
            _toastEl.className = 'fixed bottom-6 right-6 z-[300] px-4 py-3 rounded-xl shadow-lg text-sm font-medium ' +
                'bg-white dark:bg-[#1A1A1A] border border-slate-200 dark:border-white/10 ' +
                'text-slate-800 dark:text-slate-100 transition-opacity duration-300 opacity-0 pointer-events-none';
            document.body.appendChild(_toastEl);
        }
        _toastEl.classList.remove('opacity-0');
        _toastEl.textContent = message || '';
        if (_toastTimer) clearTimeout(_toastTimer);
        _toastTimer = setTimeout(function () { _toastEl.classList.add('opacity-0'); }, 2500);
    }

    // ---- banner -----------------------------------------------------------
    function showBanner(kind, text) {
        const el = document.getElementById('todo-banner');
        if (!el) return;
        if (!text) { el.classList.add('hidden'); return; }
        el.classList.remove('hidden');
        el.textContent = text;
        el.className = 'mb-4 rounded-xl border px-4 py-3 text-sm ' + (kind === 'ok'
            ? 'bg-emerald-50 dark:bg-emerald-900/20 border-emerald-200 dark:border-emerald-500/30 text-emerald-700 dark:text-emerald-300'
            : kind === 'warn'
                ? 'bg-amber-50 dark:bg-amber-900/20 border-amber-200 dark:border-amber-500/30 text-amber-700 dark:text-amber-300'
                : 'bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-500/30 text-red-700 dark:text-red-300');
    }

    // ---- state ------------------------------------------------------------
    let _loaded = false;
    let _loading = false;
    let _currentStatus = 'open';
    let _q = '';
    let _overdue = false;
    let _page = 1;
    const _pageSize = 20;
    let _total = 0;
    let _items = [];
    let _editingItem = null;   // null = create mode
    let _dirty = false;        // unsaved edit modal changes

    // ---- summary badge ----------------------------------------------------
    // The top-bar bell is the todo's only entry point, so one summary render
    // paints one badge. Keeping it a named pass holds the 0 / value / 99+
    // decision in a single place (todo-workbench: 本人角标).
    const BADGE_OVERFLOW = 99;
    const BELL_BTN_ID = 'todo-bell-btn';
    const BELL_BADGE_ID = 'todo-bell-badge';

    function formatBadgeCount(count) {
        const n = Math.floor(Number(count) || 0);
        if (n <= 0) return '';
        return n > BADGE_OVERFLOW ? BADGE_OVERFLOW + '+' : String(n);
    }

    function paintBadge(badge, text, overdue, overdueClass) {
        if (!badge) return;
        if (!text) {
            badge.classList.add('hidden');
            badge.textContent = '';
            return;
        }
        badge.textContent = text;
        badge.classList.remove('hidden');
        // The caller names the overdue class so the red state does not depend on
        // which utility class the stylesheet resolves last.
        if (overdueClass) {
            if (overdue) badge.classList.add(overdueClass);
            else badge.classList.remove(overdueClass);
        }
    }

    function setBellOffered(offered) {
        const btn = document.getElementById(BELL_BTN_ID);
        if (!btn) return;
        if (offered) btn.classList.remove('hidden');
        else btn.classList.add('hidden');
    }

    function applySummaryBadge(summary) {
        const usable = !!(summary && summary.enabled && summary.bound);
        const text = usable ? formatBadgeCount(summary.open) : '';
        const overdue = usable && !!summary.overdue;
        paintBadge(document.getElementById(BELL_BADGE_ID), text, overdue, 'todo-bell-badge-overdue');
        // The entry is withheld only on a server fact: capability withdrawn or
        // no bound subject. No sidebar entry is left to withhold with it.
        setBellOffered(!(summary && (summary.enabled === false || summary.bound === false)));
    }

    // A status that means the server judged this identity, as opposed to a read
    // that merely failed. 401/403 say the identity may not read todos at all;
    // the feature-off 404 says the deployment withdrew the capability.
    function summaryDenied(err) {
        if (!err) return false;
        if (err.status === 401 || err.status === 403) return true;
        return err.code === 'todo_disabled';
    }

    async function refreshSummary() {
        try {
            const data = await apiFetch('/api/todos/summary');
            applySummaryBadge(data);
            return data;
        } catch (e) {
            // Unknown, not "off": clear the count but keep the entry, so one
            // transient failure cannot erase the way in. An authoritative
            // denial still withholds the entry.
            applySummaryBadge(null);
            if (summaryDenied(e)) setBellOffered(false);
            return null;
        }
    }

    // ---- top-bar bell entry -----------------------------------------------
    // The header is upstream-shared markup, so the fork injects its entry from
    // this module at a stable anchor rather than editing chat.html
    // (fork-upstream-decoupling: fork frontend ships as its own module).
    function mountTodoBell() {
        const header = document.querySelector('.workbench-header');
        if (!header) return;                              // no workbench shell here
        if (document.getElementById(BELL_BTN_ID)) return; // idempotent
        const label = t('menu_todo');
        const btn = document.createElement('button');
        btn.id = BELL_BTN_ID;
        btn.type = 'button';
        btn.className = 'todo-bell-btn p-2 rounded-lg hover:bg-slate-100 ' +
            'dark:hover:bg-white/10 cursor-pointer transition-colors duration-150';
        // console.js's applyI18n() ran before this module loaded, so seed the
        // tooltip and the accessible name here; the data-* keys keep both
        // localized on later language switches.
        btn.setAttribute('data-tip-key', 'menu_todo');
        btn.setAttribute('data-tooltip', label);
        btn.setAttribute('data-tooltip-pos', 'bottom');
        btn.setAttribute('data-i18n-aria-label', 'menu_todo');
        btn.setAttribute('aria-label', label);
        btn.innerHTML = '<i class="fas fa-bell text-slate-500 dark:text-slate-400" aria-hidden="true"></i>';
        const badge = document.createElement('span');
        badge.id = BELL_BADGE_ID;
        badge.className = 'hidden';
        badge.setAttribute('aria-hidden', 'true');
        btn.appendChild(badge);
        btn.addEventListener('click', openTodoFromBell);
        // Between the page title and the tenant selector. Falling back to the
        // last header action and then to the header itself means a different
        // header shape never drops the entry silently.
        const anchor = document.getElementById('tenant-selector') ||
            document.getElementById('workspace-toggle-btn');
        if (anchor && anchor.parentNode === header) header.insertBefore(btn, anchor);
        else header.appendChild(btn);
    }

    function openTodoFromBell() {
        // The todo page's address: reuse the console's availability gate, leave
        // check and cross-area switch rather than reaching the view directly.
        if (typeof window.navigateTo === 'function') window.navigateTo('todo');
    }

    // ---- list rendering ---------------------------------------------------
    function emptyText() {
        if (_q) return t('todo_empty_search');
        if (_overdue) return t('todo_empty_overdue');
        if (_currentStatus === 'all') return t('todo_empty_all');
        if (_currentStatus === 'open') return t('todo_empty_open');
        if (_currentStatus === 'pending') return t('todo_empty_pending');
        if (_currentStatus === 'in_progress') return t('todo_empty_progress');
        if (_currentStatus === 'completed') return t('todo_empty_done');
        if (_currentStatus === 'cancelled') return t('todo_empty_cancel');
        return t('todo_empty_all');
    }

    function statusClass(status) {
        switch (status) {
            case 'in_progress': return 'bg-blue-50 dark:bg-blue-900/20 text-blue-600 dark:text-blue-300';
            case 'completed': return 'bg-emerald-50 dark:bg-emerald-900/20 text-emerald-600 dark:text-emerald-300';
            case 'cancelled': return 'bg-slate-100 dark:bg-white/10 text-slate-500 dark:text-slate-400';
            default: return 'bg-amber-50 dark:bg-amber-900/20 text-amber-600 dark:text-amber-300';
        }
    }

    function priorityClass(p) {
        if (p === 'high') return 'bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-300';
        if (p === 'low') return 'bg-slate-100 dark:bg-white/10 text-slate-500 dark:text-slate-400';
        return 'bg-slate-100 dark:bg-white/10 text-slate-600 dark:text-slate-300';
    }

    function kindIcon(kind) {
        switch (kind) {
            case 'input_required': return 'fa-file-import';
            case 'confirmation': return 'fa-circle-check';
            case 'review': return 'fa-square-check';
            default: return 'fa-list-check';
        }
    }

    function renderList() {
        const listEl = document.getElementById('todo-list');
        const emptyEl = document.getElementById('todo-empty');
        const emptyTextEl = document.getElementById('todo-empty-text');
        if (!listEl) return;

        if (!_items.length) {
            listEl.classList.add('hidden');
            listEl.innerHTML = '';
            emptyEl.classList.remove('hidden');
            emptyTextEl.textContent = emptyText();
            renderPagination();
            return;
        }

        emptyEl.classList.add('hidden');
        listEl.classList.remove('hidden');
        listEl.innerHTML = _items.map(function (item) {
            const overdue = item.overdue;
            const dueStr = item.due_at
                ? (overdue ? '<span class="text-red-500">' + escapeHtml(t('todo_due_overdue')) + ' · ' + escapeHtml(formatDate(item.due_at)) + '</span>'
                            : escapeHtml(formatDate(item.due_at)))
                : t('todo_due_none');
            const titleCls = item.status === 'completed' || item.status === 'cancelled' ? ' line-through text-slate-400' : '';
            return '<div class="bg-white dark:bg-[#1A1A1A] rounded-xl border ' + (overdue ? 'border-red-200 dark:border-red-500/30 ' : 'border-slate-200 dark:border-white/10 ') +
                'p-4 hover:shadow-sm cursor-pointer transition-colors duration-150" data-id="' + escapeHtml(item.id) + '" onclick="openTodoDetail(\'' + escapeHtml(item.id) + '\')">' +
                '<div class="flex items-start gap-3">' +
                '<div class="w-9 h-9 rounded-lg bg-primary-50 dark:bg-primary-900/30 flex items-center justify-center flex-shrink-0">' +
                '<i class="fas ' + kindIcon(item.kind) + ' text-primary-500"></i></div>' +
                '<div class="min-w-0 flex-1">' +
                '<div class="flex items-center justify-between gap-2">' +
                '<div class="font-medium text-slate-800 dark:text-slate-100 text-sm' + titleCls + '">' + escapeHtml(item.title) + '</div>' +
                '<span class="text-[10px] font-medium px-2 py-0.5 rounded-full flex-shrink-0 ' + statusClass(item.status) + '">' + escapeHtml(item.status_label) + '</span>' +
                '</div>' +
                (item.description ? '<p class="text-xs text-slate-500 dark:text-slate-400 mt-1 line-clamp-2">' + escapeHtml(item.description) + '</p>' : '') +
                '<div class="flex items-center gap-3 mt-2 text-xs text-slate-400 dark:text-slate-500">' +
                '<span class="inline-flex items-center gap-1"><i class="fas fa-clock"></i>' + dueStr + '</span>' +
                '<span class="inline-flex items-center gap-1"><i class="fas fa-flag"></i>' + escapeHtml(item.priority_label) + '</span>' +
                '<span class="inline-flex items-center gap-1"><i class="fas fa-tag"></i>' + escapeHtml(item.kind_label) + '</span>' +
                '</div></div></div></div>';
        }).join('');

        renderPagination();
    }

    function renderPagination() {
        const el = document.getElementById('todo-pagination');
        if (!el) return;
        const pages = Math.ceil(_total / _pageSize);
        if (_total <= _pageSize) { el.classList.add('hidden'); el.innerHTML = ''; return; }
        el.classList.remove('hidden');
        el.innerHTML =
            '<button class="todo-page-btn px-3 py-1.5 rounded-lg border border-slate-200 dark:border-white/10 text-xs text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-white/5 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer" data-page="' + (_page - 1) + '" ' + (_page <= 1 ? 'disabled' : '') + '>' + t('todo_pagination_prev') + '</button>' +
            '<span class="text-xs text-slate-400 dark:text-slate-500">' + _page + ' / ' + pages + '</span>' +
            '<button class="todo-page-btn px-3 py-1.5 rounded-lg border border-slate-200 dark:border-white/10 text-xs text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-white/5 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer" data-page="' + (_page + 1) + '" ' + (_page >= pages ? 'disabled' : '') + '>' + t('todo_pagination_next') + '</button>';
    }

    // ---- load -------------------------------------------------------------
    async function loadTodosView(force) {
        if (_loading) return;
        if (_loaded && !force) return;
        _loading = true;
        _loaded = false;
        const gen = ++_generation;
        showBanner('', '');
        const listEl = document.getElementById('todo-list');
        const emptyEl = document.getElementById('todo-empty');
        const emptyTextEl = document.getElementById('todo-empty-text');
        const paginationEl = document.getElementById('todo-pagination');
        emptyEl.classList.remove('hidden');
        emptyTextEl.textContent = 'Loading...';
        if (listEl) listEl.classList.add('hidden');
        if (paginationEl) paginationEl.classList.add('hidden');

        try {
            const params = new URLSearchParams();
            params.set('status', _currentStatus);
            params.set('page', String(_page));
            params.set('page_size', String(_pageSize));
            if (_q) params.set('q', _q);
            if (_overdue) params.set('overdue', 'true');
            const data = await apiFetch('/api/todos?' + params.toString());
            if (gen !== _generation) return; // superseded by a newer load
            _total = data.total || 0;
            _items = data.items || [];
            renderList();
            _loaded = true;
            refreshSummary();
        } catch (err) {
            if (gen !== _generation) return;
            if (err.code === 'todo_disabled') {
                showBanner('warn', t('todo_disabled_banner'));
            } else if (err.status === 401 || err.code === 'unauthorized') {
                showBanner('warn', t('todo_unauthorized_banner'));
            } else {
                showBanner('error', t('todo_load_failed'));
            }
            _items = [];
            _total = 0;
            // A failed request has no known result; only a successful empty
            // response may show the empty state. Keep failed loads retryable.
            if (listEl) { listEl.classList.add('hidden'); listEl.innerHTML = ''; }
            if (emptyEl) emptyEl.classList.add('hidden');
            if (paginationEl) { paginationEl.classList.add('hidden'); paginationEl.innerHTML = ''; }
        } finally {
            if (gen === _generation) _loading = false;
        }
    }

    function refreshTodosView() {
        _loaded = false;
        _page = 1;
        loadTodosView(true);
    }

    async function loadTodosViewNow() {
        await loadTodosView(true);
    }

    // ---- filter interactions ---------------------------------------------
    function setActiveFilter(status) {
        _currentStatus = status;
        _page = 1;
        _loaded = false;
        const tabs = document.querySelectorAll('.todo-status-tab');
        tabs.forEach(function (tab) {
            tab.classList.toggle('active', tab.dataset.status === status);
        });
        loadTodosView(true);
    }

    function setOverdue(on) {
        _overdue = on;
        _page = 1;
        _loaded = false;
        loadTodosView(true);
    }

    // ---- create / edit modal ---------------------------------------------
    let _lastCreateKey = null; // persisted create key for idempotent retry

    function openTodoCreate() {
        _editingItem = null;
        _dirty = false;
        document.getElementById('todo-edit-title').textContent = t('todo_create_title');
        document.getElementById('todo-edit-subtitle').textContent = '';
        document.getElementById('todo-edit-icon').className = 'fas fa-plus text-primary-500';
        document.getElementById('todo-edit-field-title').value = '';
        document.getElementById('todo-edit-field-desc').value = '';
        document.getElementById('todo-edit-field-due').value = '';
        fillKindSelect('general');
        fillPrioritySelect('normal');
        closeHiddenError();
        document.getElementById('todo-edit-overlay').classList.remove('hidden');
        _lastCreateKey = makeCreateKey();
        const titleInput = document.getElementById('todo-edit-field-title');
        titleInput.focus();
    }

    function openTodoEdit(id) {
        const item = _items.find(function (x) { return x.id === id; });
        if (!item) { openTodoDetail(id); return; }
        _editingItem = item;
        _dirty = false;
        document.getElementById('todo-edit-title').textContent = t('todo_edit_title');
        document.getElementById('todo-edit-subtitle').textContent = item.status_label || '';
        document.getElementById('todo-edit-icon').className = 'fas fa-pen text-primary-500';
        document.getElementById('todo-edit-field-title').value = item.title || '';
        document.getElementById('todo-edit-field-desc').value = item.description || '';
        document.getElementById('todo-edit-field-due').value = toLocalInput(item.due_at);
        fillKindSelect(item.kind);
        fillPrioritySelect(item.priority);
        closeHiddenError();
        document.getElementById('todo-edit-overlay').classList.remove('hidden');
        document.getElementById('todo-edit-field-title').focus();
    }

    function toLocalInput(sec) {
        if (!sec) return '';
        const d = new Date(sec * 1000);
        const p = function (n) { return String(n).padStart(2, '0'); };
        return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes());
    }

    function fromLocalInput(val) {
        if (!val) return null;
        // datetime-local gives local wall time; pass as-is; server treats as wall time in the given zone.
        return val;
    }

    function fillKindSelect(value) {
        const sel = document.getElementById('todo-edit-field-kind');
        const options = [
            { v: 'general', k: 'todo_kind_general' },
            { v: 'input_required', k: 'todo_kind_input_required' },
            { v: 'confirmation', k: 'todo_kind_confirmation' },
            { v: 'review', k: 'todo_kind_review' },
        ];
        sel.innerHTML = options.map(function (o) {
            return '<option value="' + o.v + '"' + (o.v === value ? ' selected' : '') + '>' + escapeHtml(t(o.k)) + '</option>';
        }).join('');
    }

    function fillPrioritySelect(value) {
        const sel = document.getElementById('todo-edit-field-priority');
        const options = [
            { v: 'high', k: 'todo_priority_high' },
            { v: 'normal', k: 'todo_priority_normal' },
            { v: 'low', k: 'todo_priority_low' },
        ];
        sel.innerHTML = options.map(function (o) {
            return '<option value="' + o.v + '"' + (o.v === value ? ' selected' : '') + '>' + escapeHtml(t(o.k)) + '</option>';
        }).join('');
    }

    function closeHiddenError() {
        const el = document.getElementById('todo-edit-error');
        if (el) { el.classList.add('hidden'); el.textContent = ''; }
    }

    function showErr(msg) {
        const el = document.getElementById('todo-edit-error');
        if (el) { el.textContent = msg || ''; el.classList.remove('hidden'); }
    }

    function closeTodoEdit() {
        if (_dirty && !window.confirm(t('todo_confirm_discard'))) return false;
        document.getElementById('todo-edit-overlay').classList.add('hidden');
        _editingItem = null;
        _dirty = false;
        return true;
    }

    async function submitTodoEdit() {
        const title = document.getElementById('todo-edit-field-title').value.trim();
        const desc = document.getElementById('todo-edit-field-desc').value;
        const kind = document.getElementById('todo-edit-field-kind').value;
        const priority = document.getElementById('todo-edit-field-priority').value;
        const dueLocal = document.getElementById('todo-edit-field-due').value;
        const submitBtn = document.getElementById('todo-edit-submit');
        submitBtn.disabled = true;
        closeHiddenError();
        try {
            if (_editingItem) {
                const fields = { title: title, description: desc, kind: kind, priority: priority };
                if (dueLocal) { fields.due_at = fromLocalInput(dueLocal); fields.timezone = localTimezone(); }
                else { fields.due_at = null; fields.timezone = ''; }
                const item = await updateTodoFields(_editingItem.id, _editingItem.version, fields);
                toast(t('todo_saved'));
                closeTodoEdit();
                refreshTodosView();
                if (window.openTodoDetail) window.openTodoDetail(item.id);
            } else {
                const createKey = _lastCreateKey || makeCreateKey();
                const body = {
                    title: title, description: desc, kind: kind, priority: priority,
                    source: 'manual', create_key: createKey,
                };
                if (dueLocal) { body.due_at = fromLocalInput(dueLocal); body.timezone = localTimezone(); }
                try {
                    const res = await apiFetch('/api/todos', { method: 'POST', body: body });
                    toast(t('todo_created'));
                    closeTodoEdit();
                    refreshTodosView();
                    return;
                } catch (err) {
                    // Retry once after a transient 503 (e.g. storage busy).
                    if (err.status === 503) {
                        await new Promise(function (r) { setTimeout(r, 500); });
                        const res = await apiFetch('/api/todos', { method: 'POST', body: body });
                        toast(t('todo_created'));
                        closeTodoEdit();
                        refreshTodosView();
                        return;
                    }
                    if (err.status === 409) {
                        // Same create_key with a different initial payload: the
                        // key was already consumed by another draft. GET the
                        // current item so the user sees the real state rather
                        // than a stale form, and surface the conflict clearly.
                        const cur = await apiFetch('/api/todos?q=' + encodeURIComponent(title) + '&page_size=1');
                        const existing = (cur.items && cur.items[0]);
                        if (existing) {
                            toast((err.data && err.data.message) || err.message || t('todo_save_failed'));
                            showErr((err.data && err.data.message) || err.message || t('todo_save_failed'));
                            window.openTodoDetail ? window.openTodoDetail(existing.id) : null;
                            closeTodoEdit();
                            return;
                        }
                    }
                    // Unknown result: don't assume failure — GET a fresh read.
                    const cur = await apiFetch('/api/todos?q=' + encodeURIComponent(title) + '&page_size=1');
                    if (cur.items && cur.items.length) {
                        toast(t('todo_created'));
                        closeTodoEdit();
                        refreshTodosView();
                        return;
                    }
                    throw err;
                }
            }
        } catch (err) {
            if (err.field === 'title' || err.code === 'invalid_field') {
                showErr(err.message || t('todo_save_failed'));
            } else {
                showErr((err.data && err.data.message) || err.message || t('todo_save_failed'));
            }
        } finally {
            submitBtn.disabled = false;
        }
    }

    function localTimezone() {
        const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
        return tz || '';
    }

    async function updateTodoFields(id, expectedVersion, fields) {
        return apiFetch('/api/todos/' + encodeURIComponent(id), {
            method: 'PATCH',
            body: { expected_version: expectedVersion, fields: fields },
        });
    }

    // ---- detail drawer ----------------------------------------------------
    let _detailItem = null;
    let _detailHistoryPage = 1;
    let _detailHistoryLoaded = false;

    async function openTodoDetail(id) {
        _detailItem = null;
        _detailHistoryPage = 1;
        _detailHistoryLoaded = false;
        renderDetailLoading();
        document.getElementById('todo-detail-overlay').classList.remove('hidden');
        try {
            const data = await apiFetch('/api/todos/' + encodeURIComponent(id));
            _detailItem = data.item;
            renderDetail();
            loadDetailEvents();
        } catch (err) {
            showBanner('error', (err.data && err.data.message) || err.message || t('todo_load_error'));
            closeTodoDetail();
        }
    }

    function renderDetailLoading() {
        const body = document.getElementById('todo-detail-body');
        document.getElementById('todo-detail-title').textContent = t('todo_detail_title');
        document.getElementById('todo-detail-meta').textContent = '';
        document.getElementById('todo-detail-actions').innerHTML = '';
        if (body) body.innerHTML = '<div class="text-sm text-slate-400 dark:text-slate-500">Loading...</div>';
    }

    function renderDetail() {
        const item = _detailItem;
        if (!item) return;
        document.getElementById('todo-detail-title').textContent = item.title || '';
        const metaParts = [];
        metaParts.push('<span class="' + statusClass(item.status) + ' px-2 py-0.5 rounded-full text-[10px] font-medium">' + escapeHtml(item.status_label) + '</span>');
        metaParts.push(escapeHtml(item.kind_label) + ' · ' + escapeHtml(item.priority_label));
        document.getElementById('todo-detail-meta').innerHTML = metaParts.join(' &nbsp; ');

        const body = document.getElementById('todo-detail-body');
        let html = '';
        if (item.description) {
            html += '<div><div class="text-xs font-medium text-slate-500 dark:text-slate-400 mb-1">' + escapeHtml(t('todo_field_desc')) + '</div>' +
                '<p class="text-sm text-slate-700 dark:text-slate-200 whitespace-pre-wrap break-words">' + escapeHtml(item.description) + '</p></div>';
        }
        if (item.due_at) {
            html += '<div><div class="text-xs font-medium text-slate-500 dark:text-slate-400 mb-1">' + escapeHtml(t('todo_field_due')) + '</div>' +
                '<p class="text-sm ' + (item.overdue ? 'text-red-500 font-medium' : 'text-slate-700 dark:text-slate-200') + '">' +
                (item.overdue ? escapeHtml(t('todo_due_overdue')) + ' · ' : '') + escapeHtml(formatDate(item.due_at)) + '</p></div>';
        }
        html += '<div><div class="text-xs font-medium text-slate-500 dark:text-slate-400 mb-1">' + escapeHtml(t('todo_created_by')) + '</div>' +
            '<p class="text-sm text-slate-700 dark:text-slate-200">' + (item.source === 'conversation' ? escapeHtml(t('todo_source_conversation')) : escapeHtml(t('todo_source_manual'))) +
            (item.created_by === 'agent' ? ' (Agent)' : '') + '</p></div>';
        html += '<div class="grid grid-cols-2 gap-3 text-xs text-slate-400 dark:text-slate-500">' +
            '<div><span class="text-slate-500 dark:text-slate-400">' + escapeHtml(t('todo_by')) + '</span><br>' + escapeHtml(formatDate(item.created_at)) + '</div>' +
            '<div><span class="text-slate-500 dark:text-slate-400">' + escapeHtml(t('todo_updated')) + '</span><br>' + escapeHtml(formatDate(item.updated_at)) + '</div>' +
            '</div>';
        body.innerHTML = html;
        renderDetailActions();
    }

    function renderDetailActions() {
        const el = document.getElementById('todo-detail-actions');
        const item = _detailItem;
        if (!item) { el.innerHTML = ''; return; }
        const opts = item.can_operate || {};
        const buttons = [];
        if (opts.start) buttons.push(detailActionBtn(item, 'start', 'todo_action_start', 'fa-play', 'in_progress'));
        if (opts.complete) buttons.push(detailActionBtn(item, 'complete', 'todo_action_complete', 'fa-check', 'completed'));
        if (opts.cancel) buttons.push(detailActionBtn(item, 'cancel', 'todo_action_cancel', 'fa-xmark', 'cancelled'));
        if (opts.reopen) buttons.push(detailActionBtn(item, 'reopen', 'todo_action_reopen', 'fa-rotate-left', 'pending'));
        if (item.can_edit) buttons.push('<button class="px-3 py-1.5 rounded-lg border border-slate-200 dark:border-white/10 text-xs text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-white/5 cursor-pointer" onclick="openTodoEditFromDetail()">' + escapeHtml(t('todo_action_edit')) + '</button>');
        el.innerHTML = buttons.join(' ');
    }

    function detailActionBtn(item, action, labelKey, icon, targetStatus) {
        return '<button class="px-3 py-1.5 rounded-lg border border-slate-200 dark:border-white/10 text-xs text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-white/5 cursor-pointer"' +
            ' onclick="operateTodo(\'' + escapeHtml(item.id) + '\',' + item.version + ',\'' + targetStatus + '\')">' +
            '<i class="fas ' + icon + ' mr-1"></i>' + escapeHtml(t(labelKey)) + '</button>';
    }

    async function openTodoEditFromDetail() {
        if (!_detailItem) return;
        openTodoDetailAttach();
        const item = _detailItem;
        _editingItem = item;
        _dirty = false;
        document.getElementById('todo-edit-title').textContent = t('todo_edit_title');
        document.getElementById('todo-edit-subtitle').textContent = item.status_label || '';
        document.getElementById('todo-edit-field-title').value = item.title || '';
        document.getElementById('todo-edit-field-desc').value = item.description || '';
        document.getElementById('todo-edit-field-due').value = toLocalInput(item.due_at);
        fillKindSelect(item.kind);
        fillPrioritySelect(item.priority);
        closeHiddenError();
        document.getElementById('todo-edit-overlay').classList.remove('hidden');
        document.getElementById('todo-edit-field-title').focus();
    }

    function openTodoDetailAttach() {
        // Keep the drawer; the edit modal sits above it.
        document.getElementById('todo-edit-overlay').classList.remove('hidden');
    }

    function closeTodoDetail() {
        document.getElementById('todo-detail-overlay').classList.add('hidden');
        _detailItem = null;
        _detailHistoryLoaded = false;
    }

    // ---- status operations -----------------------------------------------
    async function operateTodo(id, expectedVersion, targetStatus) {
        const labels = { completed: t('todo_confirm_complete'), cancelled: t('todo_confirm_cancel'), pending: t('todo_confirm_reopen') };
        const confirmMsg = labels[targetStatus];
        if (confirmMsg && !window.confirm(confirmMsg)) return;
        try {
            const res = await apiFetch('/api/todos/' + encodeURIComponent(id), {
                method: 'PATCH',
                body: { expected_version: expectedVersion, status: targetStatus },
            });
            toast(t('todo_saved'));
            if (_detailItem && _detailItem.id === id) {
                _detailItem = res.item;
                renderDetail();
                loadDetailEvents();
            }
            refreshTodosView();
            refreshSummary();
        } catch (err) {
            onUnknownUpdate(id, err);
        }
    }

    // 409 / unknown result: GET current to confirm, then surface the real state.
    async function onUnknownUpdate(id, err) {
        // 409 stale -> re-fetch current and refresh UI so the user sees reality.
        try {
            const data = await apiFetch('/api/todos/' + encodeURIComponent(id));
            toast((err.data && err.data.message) || err.message || t('todo_load_error'));
            if (window.confirm((err.data && err.data.message) || err.message || t('todo_load_error'))) {
                // Refreshing after a conflict: just reload list.
            }
            if (_detailItem && _detailItem.id === id) {
                _detailItem = data.item;
                renderDetail();
                loadDetailEvents();
            }
            refreshTodosView();
            return;
        } catch (e) {
            refreshTodosView();
        }
    }

    // ---- events / history ------------------------------------------------
    async function loadDetailEvents() {
        const id = _detailItem && _detailItem.id;
        if (!id) return;
        try {
            const data = await apiFetch('/api/todos/' + encodeURIComponent(id) + '/events?page=' + _detailHistoryPage + '&page_size=20');
            renderHistory(data);
        } catch (err) {
            // Silent: history is auxiliary.
        }
    }

    function renderHistory(data) {
        const body = document.getElementById('todo-detail-body');
        if (!body) return;
        const items = data.items || [];
        let html = '<div class="pt-1"><div class="text-xs font-medium text-slate-500 dark:text-slate-400 mb-2">' + escapeHtml(t('todo_detail_history')) + '</div>';
        // The action buttons were already rendered by renderDetail(); do not
        // clobber them here.
        if (!items.length) {
            html += '<p class="text-sm text-slate-400 dark:text-slate-500">' + escapeHtml(t('todo_detail_history_empty')) + '</p>';
        } else {
            html += '<div class="space-y-3">' + items.map(function (ev) {
                const icon = ev.action === 'create' ? 'fa-plus' : ev.action === 'complete' ? 'fa-check' : ev.action === 'cancel' ? 'fa-xmark' : ev.action === 'start' ? 'fa-play' : 'fa-pen';
                const label = ev.action === 'create' ? t('todo_created') : ev.action === 'complete' ? t('todo_action_complete') : ev.action === 'cancel' ? t('todo_action_cancel') : ev.action === 'start' ? t('todo_action_start') : t('todo_action_edit');
                return '<div class="flex gap-3">' +
                    '<div class="w-6 h-6 rounded-full bg-slate-100 dark:bg-white/10 flex items-center justify-center flex-shrink-0"><i class="fas ' + icon + ' text-xs text-slate-500"></i></div>' +
                    '<div class="min-w-0 flex-1"><div class="text-xs text-slate-600 dark:text-slate-300">' + escapeHtml(label) + (ev.operator_kind === 'agent' ? ' · Agent' : '') + '</div>' +
                    '<div class="text-[11px] text-slate-400">' + escapeHtml(formatDate(ev.created_at)) + '</div>' +
                    (ev.note ? '<div class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">' + escapeHtml(ev.note) + '</div>' : '') +
                    '</div></div>';
            }).join('') + '</div>';
        }
        html += '</div>';
        body.innerHTML = html;
    }

    // ---- wire up events ---------------------------------------------------
    function initTodosView() {
        // status tabs
        document.querySelectorAll('.todo-status-tab').forEach(function (tab) {
            tab.addEventListener('click', function () {
                setActiveFilter(tab.dataset.status);
            });
        });
        // overdue toggle
        const overdueBtn = document.getElementById('todo-overdue-toggle');
        if (overdueBtn) {
            overdueBtn.addEventListener('click', function () {
                const on = !_overdue;
                overdueBtn.classList.toggle('active', on);
                if (on) overdueBtn.classList.add('bg-amber-100', 'dark:bg-amber-900/20');
                else overdueBtn.classList.remove('bg-amber-100', 'dark:bg-amber-900/20');
                setOverdue(on);
            });
        }
        // search debounce
        const searchInput = document.getElementById('todo-search-input');
        if (searchInput) {
            let debounce = null;
            searchInput.addEventListener('input', function () {
                if (debounce) clearTimeout(debounce);
                debounce = setTimeout(function () {
                    _q = searchInput.value.trim();
                    _page = 1;
                    _loaded = false;
                    loadTodosView(true);
                }, 300);
            });
        }
        // pagination
        document.addEventListener('click', function (e) {
            const btn = e.target.closest('.todo-page-btn');
            if (!btn || btn.disabled) return;
            _page = parseInt(btn.dataset.page, 10) || 1;
            _loaded = false;
            loadTodosView(true);
        });
        // edit modal close buttons
        document.querySelectorAll('#todo-edit-overlay .todo-modal-close').forEach(function (btn) {
            btn.addEventListener('click', function () { closeTodoEdit(); });
        });
        document.getElementById('todo-edit-overlay').addEventListener('click', function (e) {
            if (e.target === e.currentTarget) closeTodoEdit();
        });
        const submitBtn = document.getElementById('todo-edit-submit');
        if (submitBtn) submitBtn.addEventListener('click', submitTodoEdit);
        // edit modal dirty tracking
        ['todo-edit-field-title', 'todo-edit-field-desc', 'todo-edit-field-kind', 'todo-edit-field-priority', 'todo-edit-field-due'].forEach(function (id) {
            const el = document.getElementById(id);
            if (el) el.addEventListener('input', function () { _dirty = true; });
        });
        document.getElementById('todo-edit-clear-due').addEventListener('click', function () {
            document.getElementById('todo-edit-field-due').value = '';
            _dirty = true;
        });
        // detail drawer close
        document.querySelectorAll('#todo-detail-overlay .todo-detail-close').forEach(function (btn) {
            btn.addEventListener('click', closeTodoDetail);
        });
        document.getElementById('todo-detail-overlay').addEventListener('click', function (e) {
            if (e.target === e.currentTarget) closeTodoDetail();
        });

        // The bell is the cross-view entry, so it is mounted once here, gets a
        // count without waiting for a visit to the todo page, and refreshes on
        // window focus. No interval/SSE/push: these are the refresh points
        // todo-workbench allows.
        mountTodoBell();
        refreshSummary();
        if (typeof window.addEventListener === 'function') {
            window.addEventListener('focus', function () { refreshSummary(); });
        }
    }

    // ---- expose to global scope for onclick/fetch --------------------------
    window.loadTodosView = loadTodosView;
    window.refreshTodosView = refreshTodosView;
    // Exposed like the other view entry points so the shipped module can be
    // exercised directly (see tests/test_todo_frontend.cjs).
    window.mountTodoBell = mountTodoBell;
    window.refreshSummary = refreshSummary;
    // Register the fork TODO view with console.js (change
    // fork-decoupling-and-tenant-hardening, task 8.6). No `repaint`: a language
    // switch left this view untouched before, and it still does.
    if (typeof window.registerConsoleView === 'function') {
        window.registerConsoleView({ id: 'todo', label: 'menu_todo', load: loadTodosView });
    }
    window.openTodoCreate = openTodoCreate;
    window.openTodoEdit = openTodoEdit;
    window.openTodoDetail = openTodoDetail;
    window.openTodoEditFromDetail = openTodoEditFromDetail;
    window.operateTodo = operateTodo;
    window.closeTodoDetail = closeTodoDetail;
    window.closeTodoEdit = closeTodoEdit;

    // init once DOM is ready (script is deferred, so DOM is parsed).
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initTodosView);
    } else {
        initTodosView();
    }
})();
