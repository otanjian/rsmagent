/* audit-console.js - 平台管理: 审计日志 (audit) + Token 消耗 (token_usage)
 *
 * Ported from the source deployment's console (change
 * add-audit-and-token-console). Two deliberate differences from the source
 * module layout:
 *
 *   1. It is a fork-owned view module registered through
 *      `window.registerConsoleView` rather than a branch inside console.js's
 *      navigation dispatch, so the core file keeps no fork-only code
 *      (change fork-decoupling-and-tenant-hardening, task 8.6).
 *   2. It talks to this fork's own endpoints. The identity audit trail and the
 *      token counts live in `identity.db` here, not in the source's per-tenant
 *      conversation databases, and the read scope (all tenants / one tenant) is
 *      decided by the server from the caller's qualification. The page never
 *      sends a scope of its own — it only *displays* the `scope` the server
 *      answered with, so a tenant administrator can never widen their own view
 *      by editing the request.
 *
 * Globals are attached (window.loadAuditLog / window.loadTokenUsage / …) because
 * the ported markup calls them from inline `onclick`, exactly as the source does.
 */
(function () {
    'use strict';

    var AUDIT_PAGE_SIZE = 50;
    var TOKEN_PAGE_SIZE = 200;

    var _auditPage = 1;
    var _auditTotal = 0;
    var _auditScope = '';
    var _tokenTab = 'details';
    // Monotonic request markers. A filter change fires several requests at once
    // (summary + one panel); without this, a slow earlier response would paint
    // over a newer one and the page would show results for a filter the user has
    // already moved on from.
    var _auditSeq = 0;
    var _tokenSeq = 0;

    // ------------------------------------------------------------------
    // Small local helpers
    // ------------------------------------------------------------------
    // The console's own helpers of the same name (qs / status / apiFetch) are
    // private to identity-admin.js's IIFE, so this module carries its own rather
    // than depending on load order between two sibling scripts.
    function el(id) { return document.getElementById(id); }

    function esc(value) {
        if (value == null) return '';
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    function num(value) { return Number(value || 0).toLocaleString(); }

    function t(key) {
        return (typeof window.t === 'function') ? window.t(key) : key;
    }

    function qs(params) {
        var parts = [];
        Object.keys(params || {}).forEach(function (k) {
            var v = params[k];
            if (v === null || v === undefined || v === '') return;
            parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
        });
        return parts.join('&');
    }

    async function apiGet(path) {
        var resp = await fetch(path, { credentials: 'same-origin' });
        var data = null;
        try { data = await resp.json(); } catch (e) { data = null; }
        if (!resp.ok) {
            var message = (data && (data.message || data.error)) || ('HTTP ' + resp.status);
            var err = new Error(message);
            err.status = resp.status;
            throw err;
        }
        return data || {};
    }

    function fmtDay(d) {
        return d.getFullYear() + '-' +
            String(d.getMonth() + 1).padStart(2, '0') + '-' +
            String(d.getDate()).padStart(2, '0');
    }

    function fmtTime(seconds) {
        if (!seconds) return '-';
        return new Date(Number(seconds) * 1000).toLocaleString();
    }

    function setStatus(id, text, ok) {
        var node = el(id);
        if (!node) return;
        node.textContent = text || '';
        node.style.color = ok ? '' : '#ef4444';
    }

    function emptyRow(colspan, text, danger) {
        return '<tr><td colspan="' + colspan + '" class="px-4 py-8 text-center '
            + (danger ? 'text-red-500' : 'text-slate-500 dark:text-slate-400') + '">'
            + esc(text) + '</td></tr>';
    }

    function renderPager(containerId, page, pageSize, total, onPage) {
        var box = el(containerId);
        if (!box) return;
        var pages = Math.max(1, Math.ceil((total || 0) / pageSize));
        if (!total || total <= pageSize) {
            box.innerHTML = '';
            return;
        }
        var prev = '<button type="button" data-pg="' + (page - 1) + '"'
            + (page <= 1 ? ' disabled' : '')
            + ' class="px-3 py-1.5 rounded-lg border border-slate-200 dark:border-white/10 text-xs text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-white/5 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer">'
            + esc(t('admin_prev')) + '</button>';
        var next = '<button type="button" data-pg="' + (page + 1) + '"'
            + (page >= pages ? ' disabled' : '')
            + ' class="px-3 py-1.5 rounded-lg border border-slate-200 dark:border-white/10 text-xs text-slate-600 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-white/5 disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer">'
            + esc(t('admin_next')) + '</button>';
        box.innerHTML = prev
            + '<span class="text-xs text-slate-500 dark:text-slate-400 px-2">'
            + page + ' / ' + pages + '</span>' + next;
        box.querySelectorAll('button[data-pg]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var target = parseInt(btn.getAttribute('data-pg'), 10);
                if (!target || target < 1 || target > pages || target === page) return;
                onPage(target);
            });
        });
    }

    // ------------------------------------------------------------------
    // 审计日志
    // ------------------------------------------------------------------

    function initAuditDates() {
        var startEl = el('audit-filter-start');
        var endEl = el('audit-filter-end');
        if (!startEl || !endEl) return;
        var now = new Date();
        if (!startEl.value) {
            // Start of the current month, as in the source page: the question an
            // operator asks first is "what happened this month", and a window
            // that began 30 days ago would straddle two months and answer it
            // with a number they then have to interpret.
            startEl.value = fmtDay(new Date(now.getFullYear(), now.getMonth(), 1));
        }
        if (!endEl.value) endEl.value = fmtDay(now);
    }

    function _auditResultBadge(event) {
        var result = String(event.result || '');
        if (result === 'success') {
            return '<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-emerald-50 dark:bg-emerald-500/10 text-emerald-600 dark:text-emerald-400">'
                + '<i class="fas fa-circle-check"></i>' + esc(t('audit_success')) + '</span>';
        }
        var denied = result === 'denied';
        var label = denied ? t('audit_result_denied')
            : (result === 'error' ? t('audit_result_error') : (result || t('audit_failure')));
        var tone = denied
            ? 'bg-amber-50 dark:bg-amber-500/10 text-amber-600 dark:text-amber-400'
            : 'bg-red-50 dark:bg-red-500/10 text-red-600 dark:text-red-400';
        var icon = denied ? 'fa-ban' : 'fa-circle-xmark';
        return '<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs ' + tone + '">'
            + '<i class="fas ' + icon + '"></i>' + esc(label) + '</span>';
    }

    function _changesText(changes) {
        if (!changes) return '';
        var keys = Object.keys(changes);
        if (!keys.length) return '';
        return keys.map(function (k) {
            var value = changes[k];
            if (value && typeof value === 'object') {
                try { value = JSON.stringify(value); } catch (e) { value = String(value); }
            }
            return k + '=' + String(value);
        }).join(', ');
    }

    function _auditRow(event) {
        var resource = event.resource_type
            ? (event.resource_id ? event.resource_type + ':' + event.resource_id
                                 : event.resource_type)
            : '-';
        var changes = _changesText(event.changes);
        var tenantCell = _auditScope === 'tenant' ? ''
            : '<td class="px-4 py-3 text-xs text-slate-500 dark:text-slate-400">'
              + esc(event.tenant || '-') + '</td>';
        return '<tr class="hover:bg-slate-50 dark:hover:bg-white/5 transition-colors">'
            + '<td class="px-4 py-3 font-mono text-xs text-slate-500 dark:text-slate-400 whitespace-nowrap">'
            + esc(fmtTime(event.timestamp)) + '</td>'
            + tenantCell
            + '<td class="px-4 py-3 text-slate-700 dark:text-slate-200">'
            + esc(event.actor || '-') + '</td>'
            + '<td class="px-4 py-3 font-mono text-xs text-slate-700 dark:text-slate-200">'
            + esc(event.action || '-') + '</td>'
            + '<td class="px-4 py-3 text-xs text-slate-500 dark:text-slate-400">'
            + esc(resource) + '</td>'
            + '<td class="px-4 py-3 text-xs font-medium">' + _auditResultBadge(event) + '</td>'
            + '<td class="px-4 py-3 text-xs text-slate-500 dark:text-slate-400 max-w-xs truncate" title="'
            + esc(changes) + '">' + esc(changes || '-') + '</td>'
            + '</tr>';
    }

    async function loadAuditLog() {
        var body = el('audit-table-body');
        if (!body) return;
        var seq = ++_auditSeq;
        body.innerHTML = emptyRow(7, t('tenant_loading'));

        var actorEl = el('audit-filter-actor');
        var actionEl = el('audit-filter-action');
        var statusEl = el('audit-filter-status');
        var startEl = el('audit-filter-start');
        var endEl = el('audit-filter-end');

        var query = qs({
            actor: actorEl ? actorEl.value.trim() : '',
            action: actionEl ? actionEl.value.trim() : '',
            status: statusEl ? statusEl.value : '',
            start_time: startEl ? startEl.value : '',
            end_time: endEl ? endEl.value : '',
            limit: AUDIT_PAGE_SIZE,
            offset: (_auditPage - 1) * AUDIT_PAGE_SIZE,
        });

        try {
            var data = await apiGet('/api/admin/audit/events?' + query);
            if (seq !== _auditSeq) return;
            _auditScope = data.scope || '';
            _auditTotal = data.total || 0;
            var events = data.events || [];
            if (!events.length) {
                body.innerHTML = emptyRow(7, t('audit_empty'));
            } else {
                body.innerHTML = events.map(_auditRow).join('');
            }
            renderPager('audit-pagination', _auditPage, AUDIT_PAGE_SIZE,
                _auditTotal, function (p) { _auditPage = p; loadAuditLog(); });
            setStatus('audit-status', t('audit_count').replace('{n}', num(_auditTotal)), true);
        } catch (err) {
            if (seq !== _auditSeq) return;
            body.innerHTML = emptyRow(7, err.message, true);
            setStatus('audit-status', err.message, false);
        }
    }

    // ------------------------------------------------------------------
    // Token 消耗
    // ------------------------------------------------------------------

    function initTokenUsageDates() {
        var startEl = el('token-usage-start');
        var endEl = el('token-usage-end');
        if (!startEl || !endEl) return;
        var now = new Date();
        if (!startEl.value) {
            var back = new Date();
            back.setDate(back.getDate() - 30);
            startEl.value = fmtDay(back);
        }
        if (!endEl.value) endEl.value = fmtDay(now);
    }

    function _tokenRange() {
        var startEl = el('token-usage-start');
        var endEl = el('token-usage-end');
        return {
            start_date: startEl ? startEl.value : '',
            end_date: endEl ? endEl.value : '',
        };
    }

    function switchTokenUsageTab(tabId) {
        _tokenTab = tabId;
        ['details', 'by-user', 'call-logs'].forEach(function (id) {
            var panel = el('token-usage-' + id + '-panel');
            if (panel) panel.classList.toggle('hidden', id !== tabId);
        });
        var tabs = document.querySelectorAll('#token-usage-tabs .token-usage-tab-btn');
        tabs.forEach(function (btn) {
            var active = btn.getAttribute('data-token-usage-tab') === tabId;
            btn.classList.toggle('bg-white', active);
            btn.classList.toggle('dark:bg-slate-700', active);
            btn.classList.toggle('text-slate-800', active);
            btn.classList.toggle('dark:text-slate-100', active);
            btn.classList.toggle('shadow-sm', active);
            btn.classList.toggle('text-slate-500', !active);
            btn.classList.toggle('dark:text-slate-400', !active);
            btn.classList.toggle('hover:text-slate-700', !active);
            btn.classList.toggle('dark:hover:text-slate-200', !active);
        });
        loadTokenUsage();
    }

    function _tokenFilters(extra) {
        var modelEl = el('token-usage-model');
        var actorEl = el('token-usage-actor');
        var params = Object.assign(_tokenRange(), {
            model: modelEl ? modelEl.value.trim() : '',
            actor: actorEl ? actorEl.value.trim() : '',
        });
        return Object.assign(params, extra || {});
    }

    async function loadTokenUsageActors() {
        var select = el('token-usage-actor');
        if (!select) return;
        try {
            var data = await apiGet('/api/admin/token-usage?'
                + qs(_tokenRange()) + '&type=actors');
            var actors = data.actors || [];
            var current = select.value;
            select.innerHTML = '<option value="">' + esc(t('token_usage_all_users')) + '</option>'
                + actors.map(function (a) {
                    return '<option value="' + esc(a.name) + '">' + esc(a.name) + '</option>';
                }).join('');
            select.value = current || '';
        } catch (err) {
            // A missing option list is not worth blocking the page over: the
            // tables below still load, and the name field simply stays empty
            // ("all users").
            console.warn('loadTokenUsageActors failed:', err);
        }
    }

    async function _loadTokenSummary(seq) {
        var data = await apiGet('/api/admin/token-usage?'
            + qs(_tokenFilters({ type: 'summary' })));
        if (seq !== _tokenSeq) return;
        var s = data.summary || {};
        var promptEl = el('token-usage-prompt');
        var completionEl = el('token-usage-completion');
        var callsEl = el('token-usage-calls');
        if (promptEl) promptEl.textContent = num(s.total_prompt_tokens);
        if (completionEl) completionEl.textContent = num(s.total_completion_tokens);
        if (callsEl) callsEl.textContent = num(s.total_calls);
    }

    async function _loadTokenDetails(seq) {
        var data = await apiGet('/api/admin/token-usage?'
            + qs(_tokenFilters({ type: 'details' })));
        if (seq !== _tokenSeq) return;
        var body = el('token-usage-table-body');
        if (!body) return;
        var rows = data.details || [];
        if (!rows.length) {
            body.innerHTML = emptyRow(7, t('token_usage_empty'));
            return;
        }
        body.innerHTML = rows.map(function (row) {
            return '<tr class="hover:bg-slate-50 dark:hover:bg-white/5">'
                + '<td class="px-4 py-3 font-mono text-xs">' + esc(row.date || '-') + '</td>'
                + '<td class="px-4 py-3 text-xs text-slate-500 dark:text-slate-400">' + esc(row.tenant || '-') + '</td>'
                + '<td class="px-4 py-3">' + esc(row.model || '-') + '</td>'
                + '<td class="px-4 py-3 text-slate-500 dark:text-slate-400">' + esc(row.provider || '-') + '</td>'
                + '<td class="px-4 py-3 text-right tabular-nums">' + num(row.prompt_tokens) + '</td>'
                + '<td class="px-4 py-3 text-right tabular-nums">' + num(row.completion_tokens) + '</td>'
                + '<td class="px-4 py-3 text-right tabular-nums">' + num(row.call_count) + '</td>'
                + '</tr>';
        }).join('');
    }

    async function _loadTokenByUser(seq) {
        var data = await apiGet('/api/admin/token-usage?'
            + qs(_tokenFilters({ type: 'by-user' })));
        if (seq !== _tokenSeq) return;
        var body = el('token-usage-by-user-body');
        if (!body) return;
        var rows = data.users || [];
        if (!rows.length) {
            body.innerHTML = emptyRow(4, t('token_usage_empty'));
            return;
        }
        body.innerHTML = rows.map(function (row) {
            return '<tr class="hover:bg-slate-50 dark:hover:bg-white/5">'
                + '<td class="px-4 py-3">' + esc(row.actor || '-') + '</td>'
                + '<td class="px-4 py-3 text-right tabular-nums">' + num(row.prompt_tokens) + '</td>'
                + '<td class="px-4 py-3 text-right tabular-nums">' + num(row.completion_tokens) + '</td>'
                + '<td class="px-4 py-3 text-right tabular-nums">' + num(row.call_count) + '</td>'
                + '</tr>';
        }).join('');
    }

    async function _loadTokenCallLogs(seq) {
        var data = await apiGet('/api/admin/token-usage?'
            + qs(_tokenFilters({ type: 'call_logs', limit: TOKEN_PAGE_SIZE })));
        if (seq !== _tokenSeq) return;
        var body = el('token-usage-call-logs-body');
        if (!body) return;
        var rows = data.logs || [];
        if (!rows.length) {
            body.innerHTML = emptyRow(11, t('token_usage_empty'));
            return;
        }
        body.innerHTML = rows.map(function (row) {
            // The asterisk marks a value the server derived from the summaries
            // because the provider reported no usage; the hint travels in the
            // column's title so the page does not need a footnote.
            var mark = row.estimated ? '*' : '';
            var statusClass = String(row.status) === 'success'
                ? 'text-emerald-600 dark:text-emerald-400'
                : 'text-red-600 dark:text-red-400';
            return '<tr class="hover:bg-slate-50 dark:hover:bg-white/5">'
                + '<td class="px-4 py-3 font-mono text-xs whitespace-nowrap">' + esc(fmtTime(row.created_at)) + '</td>'
                + '<td class="px-4 py-3 text-xs text-slate-500 dark:text-slate-400">' + esc(row.tenant || '-') + '</td>'
                + '<td class="px-4 py-3">' + esc(row.actor || '-') + '</td>'
                + '<td class="px-4 py-3">' + esc(row.model || '-') + '</td>'
                + '<td class="px-4 py-3 text-slate-500 dark:text-slate-400 max-w-xs truncate" title="'
                + esc(row.input_summary || '') + '">' + esc(row.input_summary || '-') + '</td>'
                + '<td class="px-4 py-3 text-slate-500 dark:text-slate-400 max-w-xs truncate" title="'
                + esc(row.output_summary || '') + '">' + esc(row.output_summary || '-') + '</td>'
                + '<td class="px-4 py-3 text-right tabular-nums">' + num(row.prompt_tokens) + mark + '</td>'
                + '<td class="px-4 py-3 text-right tabular-nums">' + num(row.completion_tokens) + mark + '</td>'
                + '<td class="px-4 py-3 text-center text-xs ' + statusClass + '">' + esc(row.status || '-') + '</td>'
                + '<td class="px-4 py-3 text-right tabular-nums">' + num(row.duration_ms) + '</td>'
                + '<td class="px-4 py-3 text-right tabular-nums">' + num(row.call_count) + '</td>'
                + '</tr>';
        }).join('');
    }

    async function loadTokenUsage() {
        var seq = ++_tokenSeq;
        setStatus('token-usage-status', '', true);
        var jobs = [_loadTokenSummary(seq)];
        if (_tokenTab === 'by-user') jobs.push(_loadTokenByUser(seq));
        else if (_tokenTab === 'call-logs') jobs.push(_loadTokenCallLogs(seq));
        else jobs.push(_loadTokenDetails(seq));
        try {
            await Promise.all(jobs);
        } catch (err) {
            if (seq !== _tokenSeq) return;
            var panel = _tokenTab === 'by-user' ? 'token-usage-by-user-body'
                : (_tokenTab === 'call-logs' ? 'token-usage-call-logs-body'
                    : 'token-usage-table-body');
            var colspan = _tokenTab === 'by-user' ? 4 : (_tokenTab === 'call-logs' ? 11 : 7);
            var body = el(panel);
            if (body) body.innerHTML = emptyRow(colspan, err.message, true);
            setStatus('token-usage-status', err.message, false);
        }
    }

    // ------------------------------------------------------------------
    // Wiring
    // ------------------------------------------------------------------

    function _resetAuditPage() { _auditPage = 1; loadAuditLog(); }

    function wireAudit() {
        ['audit-filter-status', 'audit-filter-start', 'audit-filter-end']
            .forEach(function (id) {
                var node = el(id);
                if (node) node.addEventListener('change', _resetAuditPage);
            });
        // The actor/action boxes are free text, so they commit on Enter or on
        // leaving the field — not on every keystroke, which would fire one query
        // per character and (worse) re-page the table mid-typing.
        ['audit-filter-actor', 'audit-filter-action'].forEach(function (id) {
            var node = el(id);
            if (!node) return;
            node.addEventListener('keydown', function (e) {
                if (e.key === 'Enter') _resetAuditPage();
            });
            node.addEventListener('change', _resetAuditPage);
        });
        var refresh = el('audit-refresh');
        if (refresh) refresh.addEventListener('click', _resetAuditPage);
    }

    function _resetTokenPage() {
        loadTokenUsageActors();
        loadTokenUsage();
    }

    function wireTokenUsage() {
        ['token-usage-actor', 'token-usage-model', 'token-usage-start', 'token-usage-end']
            .forEach(function (id) {
                var node = el(id);
                if (!node) return;
                node.addEventListener('change', _resetTokenPage);
            });
        var modelInput = el('token-usage-model');
        if (modelInput) {
            modelInput.addEventListener('keydown', function (e) {
                if (e.key === 'Enter') _resetTokenPage();
            });
        }
        var refresh = el('token-usage-refresh');
        if (refresh) refresh.addEventListener('click', _resetTokenPage);
    }

    function onEnter() {
        initAuditDates();
        wireAudit();
        initTokenUsageDates();
        wireTokenUsage();
        loadAuditLog();
    }

    function onEnterToken() {
        initTokenUsageDates();
        loadTokenUsageActors();
        loadTokenUsage();
    }

    // Globals the ported markup's inline handlers call.
    window.initAuditDates = initAuditDates;
    window.loadAuditLog = loadAuditLog;
    window.initTokenUsageDates = initTokenUsageDates;
    window.loadTokenUsage = loadTokenUsage;
    window.loadTokenUsageActors = loadTokenUsageActors;
    window.switchTokenUsageTab = switchTokenUsageTab;

    if (typeof window.registerConsoleView === 'function') {
        window.registerConsoleView({
            id: 'audit',
            label: 'menu_audit',
            load: onEnter,
            repaint: function () { loadAuditLog(); },
        });
        window.registerConsoleView({
            id: 'token_usage',
            label: 'menu_token_usage',
            load: onEnterToken,
            repaint: function () { loadTokenUsage(); },
        });
    }
})();
