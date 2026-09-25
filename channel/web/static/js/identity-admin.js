/* identity-admin.js - Admin views (Tenant / Users / Roles / Org /
 * Platform accounts / Audit).
 *
 * Loaded unconditionally from chat.html alongside console.js. Provides the admin
 * views, a shared request helper, per-view loading and rendering, plus full
 * create/edit/delete forms (previously read-only). All authorization happens
 * server-side; this only improves UX and wires the frontend to the existing
 * CRUD endpoints in admin_handlers.py.
 *
 * Create/Edit use a single reusable modal built from a field spec. Every write
 * includes an `expected_version` so a concurrent change surfaces as a 409
 * conflict, which we surface and reload the list (the refresh-the-list-on-409
 * behaviour is the acceptance focus of this change).
 */
(function () {
    'use strict';

    // ---- shared request helper -------------------------------------------
    function t(key) {
        const lang = window.__cowLang__ || 'zh';
        const i18n = window.I18N || {};
        return (i18n[lang] && i18n[lang][key]) || (i18n.en && i18n.en[key]) || key;
    }

    let _generation = 0;

    // Bump on tenant switch or page cleanup so in-flight responses from the
    // previous tenant are discarded (task 3.7 late-response guard).
    function bumpTenantGeneration() {
        _generation += 1;
    }

    async function apiFetch(path, options) {
        const gen = _generation;
        const headers = Object.assign({}, (options && options.headers) || {});
        // A per-call tenant override lets multi-tenant member writes target each
        // tenant explicitly; the server still re-validates the actor's
        // tenant_admin qualification for that tenant on every request.
        const tenant = (options && options.tenantId) || sessionStorage.getItem('cow_tenant_id');
        if (tenant) headers['X-Tenant-ID'] = tenant;
        if (options && options.body) headers['Content-Type'] = 'application/json';
        const resp = await fetch(path, {
            credentials: 'same-origin',
            method: (options && options.method) || 'GET',
            headers: headers,
            body: (options && options.body) ? JSON.stringify(options.body) : undefined,
        });
        if (gen !== _generation) throw new Error('stale-response');
        if (resp.status === 401) {
            // A rejected current-password is an expected answer for the write
            // flows that collect it, so those callers can opt out of the
            // "your session expired" overlay instead of being told to re-login.
            if (!(options && options.suppressAuthOverlay) &&
                typeof maybeShowLoginOverlay === 'function') {
                maybeShowLoginOverlay();
            }
            const authError = new Error('unauthorized');
            authError.status = 401;
            authError.code = 'unauthorized';
            throw authError;
        }
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.status !== 'success') {
            const err = new Error(data.message || 'load-failed');
            err.status = resp.status;
            err.code = data.code;
            err.data = data;
            throw err;
        }
        return data;
    }

    function status(el, text, ok) {
        if (!el) return;
        el.textContent = text || '';
        el.classList.remove('opacity-0');
        el.style.color = ok ? '' : '#ef4444';
        if (text) setTimeout(() => el.classList.add('opacity-0'), 5000);
    }

    function confirmDiscard(dirty) {
        if (dirty && !window.confirm(t('unsaved_changes_warning'))) return false;
        return true;
    }

    function escapeHtml(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function fmtActive(active) {
        return active ? t('active') : t('inactive');
    }

    function qs(params) {
        // Build a query string from {key: value}, dropping null/undefined/''.
        const parts = [];
        Object.keys(params || {}).forEach(function (k) {
            const v = params[k];
            if (v == null || v === '') return;
            parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
        });
        return parts.join('&');
    }

    // Session-stored items so inline row buttons can look a row up by id.
    let _tenantById = {};
    let _memberById = {};
    let _roleById = {};
    let _deptById = {};
    let _roles = [];
    let _depts = [];
    let _permCatalog = null;
    // Pagination state (kept in-view so a filter change resets to page 1).
    let _memberPage = 1;
    let _memberPageSize = 20;
    let _memberFilters = { q: '', status: '', role: '', department_id: '' };
    let _platformUserPage = 1;
    let _platformUserPageSize = 20;
    let _platformUserFilters = { q: '', status: '' };
    let _auditFilters = { actor: '', action: '', result: '', since: '', until: '' };
    let _auditPage = 1;
    let _auditPageSize = 25;

    // Platform-admin target-tenant role editing scope (task 3.2). When a
    // platform admin edits another tenant's roles, this holds that tenant id;
    // when null, the role UI targets the *current* tenant via /api/tenant/roles.
    // role base helpers route role CRUD + the assign catalog accordingly.
    let _rolePlatformTarget = null;

    function _roleApiBase() {
        return _rolePlatformTarget
            ? '/api/platform/tenants/' + encodeURIComponent(_rolePlatformTarget) + '/roles'
            : '/api/tenant/roles';
    }

    function _roleCatalogBase() {
        return _rolePlatformTarget
            ? '/api/platform/tenants/' + encodeURIComponent(_rolePlatformTarget) + '/authorization/catalog'
            : '/api/tenant/authorization/catalog';
    }

    // Resource-authorization selection state (task 3.1). Selections are kept
    // here (not just in checked DOM boxes) so search/pagination across pages does
    // not drop an already-checked resource. Each kind keeps its own q/page and
    // a Set of selected resource_id. `_resourceActions` maps kind -> the grant
    // actions emitted when a resource is selected (defaults to the kind's full
    // set, mirroring the backend RESOURCE_ACTIONS).
    let _resourceState = {};
    let _resourceActions = {
        menu: ['view'],
        skill: ['read', 'use', 'edit', 'enable'],
        tool: ['read', 'execute', 'configure'],
        model: ['read', 'use'],
        agent: ['read', 'use', 'edit', 'enable'],
    };
    let _resourceKinds = ['menu', 'skill', 'tool', 'model', 'agent'];
    function _resourceKindLabel(k) {
        return t('admin_resource_kind_' + k) || k;
    }
    let _modelCapabilities = ['chat', 'chat_fallback', 'vision', 'asr', 'tts', 'embedding', 'image', 'search'];
    let _modelDefaultSel = {}; // capability -> model resource_id

    // Tenant-model grant picker (edit-tenant modal): the platform admin picks
    // models to allocate to a tenant. Kept apart from _resourceState (which is
    // the role-assign picker) because the catalog source (platform all-mode)
    // and the produced grants (tenant_resource_grants) differ.
    // Tenant-level resource grants are tracked per kind inside the grant picker
    // (see `_tenantGrantState`), so there is no module-level singleton here.

    function _resetResourceState(initialGrants, modelDefaults) {
        _resourceState = {};
        _resourceKinds.forEach(function (k) {
            _resourceState[k] = { q: '', page: 1, pageSize: 12, selected: new Set(), loaded: false };
        });
        _modelDefaultSel = {};
        (initialGrants || []).forEach(function (g) {
            if (!g || !_resourceKinds.includes(g.resource_kind)) return;
            if (!_resourceState[g.resource_kind]) return;
            _resourceState[g.resource_kind].selected.add(g.resource_id);
        });
        Object.keys(modelDefaults || {}).forEach(function (k) {
            const v = modelDefaults[k];
            if (v) _modelDefaultSel[k] = v;
        });
    }

    // ---- reusable create/edit modal --------------------------------------
    let _adminModal = { open: false, dirty: false, fields: [], submit: null, onConflictReload: null, statusEl: null };

    function ensureAdminModal() {
        let el = document.getElementById('admin-modal');
        if (el) return el;
        el = document.createElement('div');
        el.id = 'admin-modal';
        el.className = 'fixed inset-0 bg-black/50 z-[200] hidden flex items-center justify-center';
        el.innerHTML =
            '<div class="bg-white dark:bg-[#1A1A1A] rounded-2xl border border-slate-200 dark:border-white/10 shadow-2xl w-full max-w-4xl max-h-[calc(100dvh-4rem)] m-2 overflow-hidden flex flex-col">' +
            // Header: icon + title + subtitle + close (bordered bottom)
            '<div class="flex items-center justify-between px-6 py-4 border-b border-slate-200 dark:border-white/10 flex-shrink-0">' +
            '<div class="flex items-center gap-3 min-w-0">' +
            '<div class="w-9 h-9 rounded-lg bg-primary-500/10 dark:bg-primary-400/15 text-primary-600 dark:text-primary-400 flex items-center justify-center flex-shrink-0">' +
            '<i id="admin-modal-icon" class="fas fa-plus text-sm"></i>' +
            '</div>' +
            '<div class="min-w-0">' +
            '<h3 id="admin-modal-title" class="text-base font-semibold text-slate-800 dark:text-slate-100 truncate"></h3>' +
            '<p id="admin-modal-subtitle" class="text-xs text-slate-500 dark:text-slate-400 mt-0.5 truncate"></p>' +
            '</div>' +
            '</div>' +
            '<button class="admin-modal-close p-2 -mr-1 rounded-lg text-slate-400 hover:text-slate-600 hover:bg-slate-100 dark:hover:text-slate-300 dark:hover:bg-white/10 transition-colors cursor-pointer"><i class="fas fa-times text-sm"></i></button>' +
            '</div>' +
            // Body (scrollable) + inline error
            '<div id="admin-modal-body" class="flex-1 overflow-y-auto px-6 py-6 space-y-6"></div>' +
            '<p id="admin-modal-error" class="hidden px-6 -mt-2 text-xs text-red-500"></p>' +
            // Footer: bottom bar
            '<div class="flex items-center justify-between px-6 py-4 border-t border-slate-200 dark:border-white/10 bg-slate-50/60 dark:bg-white/[0.02] flex-shrink-0">' +
            '<div></div>' +
            '<div class="flex items-center gap-3">' +
            '<button id="admin-modal-cancel" class="admin-modal-close px-4 py-2 rounded-lg text-sm font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-white/10 active:scale-[0.98] cursor-pointer transition-all"></button>' +
            '<button id="admin-modal-submit" class="px-5 py-2 rounded-lg bg-primary-500 hover:bg-primary-600 active:scale-[0.98] text-white text-sm font-medium shadow-sm shadow-primary-500/20 disabled:opacity-50 cursor-pointer transition-all inline-flex items-center gap-1.5"><i class="fas fa-check text-xs"></i><span class="admin-modal-submit-label"></span></button>' +
            '</div>' +
            '</div>' +
            '</div>';
        document.body.appendChild(el);
        el.querySelectorAll('.admin-modal-close').forEach(function (btn) {
            btn.addEventListener('click', function () { closeAdminModal(); });
        });
        el.addEventListener('click', function (e) { if (e.target === el) closeAdminModal(); });
        document.getElementById('admin-modal-submit').addEventListener('click', submitAdminModal);
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && _adminModal.open) closeAdminModal();
        });
        return el;
    }

    function fieldHtml(f) {
        const id = 'adm-fld-' + f.name;
        const val = (f.value == null ? '' : f.value);
        const req = f.required ? '<span class="text-red-500 ml-0.5">*</span>' : '';
        let control = '';
        let label = '';
        if (f.type === 'checkbox') {
            control = '<div class="flex items-center gap-2 py-1"><input type="checkbox" id="' + id + '"' + (val ? ' checked' : '') + ' class="agent-checkbox">' +
                '<span class="text-sm text-slate-600 dark:text-slate-300">' + escapeHtml(f.label) + '</span></div>';
        } else if (f.type === 'textarea') {
            label = '<label class="agent-field-label" for="' + id + '">' + escapeHtml(f.label) + req + '</label>';
            control = '<textarea id="' + id + '" class="agent-input agent-textarea" placeholder="' + escapeHtml(f.placeholder || '') + '">' + escapeHtml(val) + '</textarea>';
        } else if (f.type === 'select') {
            label = '<label class="agent-field-label" for="' + id + '">' + escapeHtml(f.label) + req + '</label>';
            control = '<div class="agent-input-wrap relative"><span class="agent-icon-abs">' + (f.icon ? '<i class="fas fa-' + escapeHtml(f.icon) + '"></i>' : '') + '</span>' +
                '<select id="' + id + '" class="agent-input"' + (f.icon ? ' data-with-icon="1"' : '') + '>' + (f.options || []).map(function (o) {
                    return '<option value="' + escapeHtml(o.value) + '"' + (String(o.value) === String(val) ? ' selected' : '') + '>' + escapeHtml(o.label) + '</option>';
                }).join('') + '</select></div>';
        } else if (f.type === 'multi') {
            label = '<label class="agent-field-label">' + escapeHtml(f.label) + req + '</label>';
            control = '<div id="' + id + '" class="flex flex-wrap gap-2">' + (f.options || []).map(function (o) {
                const checked = (f.value || []).indexOf(o.value) !== -1;
                return '<label class="inline-flex items-center gap-1.5 text-xs text-slate-600 dark:text-slate-300 px-2 py-1.5 rounded-lg border border-slate-200 dark:border-white/10 cursor-pointer bg-slate-50 dark:bg-white/5">' +
                    '<input type="checkbox" value="' + escapeHtml(o.value) + '"' + (checked ? ' checked' : '') + '>' +
                    escapeHtml(o.label) + '</label>';
            }).join('') + '</div>';
        } else if (f.type === 'locked') {
            label = '<label class="agent-field-label">' + escapeHtml(f.label) + '</label>';
            control = '<div class="agent-input-locked">' + escapeHtml(val) + '</div>';
        } else if (f.type === 'resourcegroup') {
            control = resourceGroupHtml(f);
        } else if (f.type === 'modeldefaults') {
            control = modelDefaultsHtml(f);
        } else if (f.type === 'userpicker') {
            // An account picker: the value is a stable User id chosen from the
            // currently valid accounts, never text typed into an input. The
            // container keeps the `adm-fld-<name>` id so the shared dirty-marking
            // and required-field paths keep working unchanged.
            label = '<label class="agent-field-label">' + escapeHtml(f.label) + req + '</label>';
            control = _userPickerHtml(id, f);
        } else {
            const type = f.type || 'text';
            label = '<label class="agent-field-label" for="' + id + '">' + escapeHtml(f.label) + req + '</label>';
            control = '<div class="agent-input-wrap relative"><span class="agent-icon-abs">' + (f.icon ? '<i class="fas fa-' + escapeHtml(f.icon) + '"></i>' : '') + '</span>' +
                '<input type="' + type + '" id="' + id + '" class="agent-input"' + (f.icon ? ' data-with-icon="1"' : '') + ' value="' + escapeHtml(val) + '" placeholder="' + escapeHtml(f.placeholder || '') + '"></div>';
        }
        const isFullWidth = f.type === 'resourcegroup' || f.type === 'modeldefaults' || f.type === 'userpicker' || f.type === 'textarea' || f.type === 'select' && f.full;
        const wrapClass = isFullWidth ? 'agent-field w-full md:col-span-2' : (f.inline ? 'agent-field agent-field-inline flex-1 min-w-[220px]' : 'agent-field w-full');
        return '<div class="' + wrapClass + '">' + label + control +
            (f.hint ? '<div class="agent-field-hint">' + escapeHtml(f.hint) + '</div>' : '') +
            '</div>';
    }

    // Group fields into optional "section" blocks, each with an icon + heading +
    // divider line. Within a section, fields lay out in a two-column grid.
    // A new section begins only at a field that carries an explicit
    // `sectionTitle`; fields without one continue in the current section, so
    // callers may mark just the first field of a group. Fields that precede any
    // titled section are rendered as a single implicit (untitled) section.
    function renderModalBody(fields) {
        const sections = [];
        let current = null;
        fields.forEach(function (f) {
            if (f.sectionTitle) {
                current = { key: f.sectionTitle, title: f.sectionTitle, icon: f.sectionIcon, fields: [] };
                sections.push(current);
            } else if (!current) {
                current = { key: '', title: '', icon: '', fields: [] };
                sections.push(current);
            }
            current.fields.push(f);
        });
        return sections.map(function (s) {
            const head = s.title
                ? '<div class="flex items-center gap-2 mb-3">' +
                  '<i class="fas fa-' + escapeHtml(s.icon || 'circle') + ' text-xs text-slate-400 dark:text-slate-500 w-4 text-center"></i>' +
                  '<h4 class="text-xs font-semibold text-slate-500 dark:text-slate-400">' + escapeHtml(s.title) + '</h4>' +
                  '<div class="flex-1 h-px bg-slate-100 dark:bg-white/5"></div>' +
                  '</div>'
                : '';
            const grid = '<div class="grid grid-cols-1 md:grid-cols-2 gap-4">' +
                s.fields.map(fieldHtml).join('') +
                '</div>';
            return '<section>' + head + grid + '</section>';
        }).join('');
    }

    // ---- resource-authorization pickers (task 3.1) -------------------------
    function _resCount(kind) {
        const st = _resourceState[kind];
        return st ? st.selected.size : 0;
    }

    function resourceGroupHtml(f) {
        // Renders a compact block listing the five resource kinds, each with a
        // live selection count and an expandable management area (search + paged
        // checkbox list). The whole block is a single field; collectField reads
        // the selections from _resourceState.
        const rows = _resourceKinds.map(function (k) {
            const n = _resCount(k);
            const summary = n ? t('admin_resources_selected').replace('{n}', n) : t('admin_resources_none');
            return '<div class="resource-kind-row" data-kind="' + escapeHtml(k) + '">' +
                '<div class="flex items-center justify-between py-1.5 cursor-pointer resource-kind-toggle">' +
                '<span class="text-sm font-medium text-slate-700 dark:text-slate-200">' + escapeHtml(_resourceKindLabel(k)) + '</span>' +
                '<span class="text-xs text-slate-400 resource-kind-summary">' + escapeHtml(summary) + '</span>' +
                '</div>' +
                '<div class="resource-kind-manage hidden pl-3 border-l border-slate-200 dark:border-white/10"></div>' +
                '</div>';
        }).join('');
        return '<div id="adm-fld-' + escapeHtml(f.name) + '" class="space-y-1">' + rows + '</div>';
    }

    async function _loadResourceCatalog(kind, q, page, pageSize) {
        const query = qs({ purpose: 'assign', kind: kind, q: q || '', page: page, page_size: pageSize });
        const data = await apiFetch(_roleCatalogBase() + '?' + query);
        return data;
    }

    function _resourceKindRowFind(kind) {
        const container = document.getElementById('adm-fld-resource_grants');
        if (!container) return null;
        // kind is one of a fixed small set (menu/skill/tool/model/agent), safe to
        // embed directly.
        return container.querySelector('.resource-kind-row[data-kind="' + kind + '"]');
    }

    async function _openResourceManage(kind) {
        const row = _resourceKindRowFind(kind);
        if (!row) return;
        const manage = row.querySelector('.resource-kind-manage');
        manage.classList.remove('hidden');
        const st = _resourceState[kind];
        if (!st) return;
        st.kind = kind;
        await _renderResourceList(kind);
    }

    function _closeResourceManage(kind) {
        const row = _resourceKindRowFind(kind);
        if (!row) return;
        const manage = row.querySelector('.resource-kind-manage');
        manage.classList.add('hidden');
    }

    async function _renderResourceList(kind) {
        const row = _resourceKindRowFind(kind);
        if (!row) return;
        const manage = row.querySelector('.resource-kind-manage');
        const st = _resourceState[kind];
        if (!st) return;
        manage.innerHTML = '<div class="py-1 text-xs text-slate-400">' + escapeHtml(t('admin_loading')) + '</div>';
        try {
            const data = await _loadResourceCatalog(kind, st.q, st.page, st.pageSize);
            const items = data.items || [];
            const total = data.total || 0;
            const actions = data.resource_actions || _resourceActions[kind] || [];
            st.actions = actions;
            const fn = function (selected) {
                return function (r) {
                    const checked = selected.has(r.resource_id);
                    const rid = r.resource_id;
                    const idSuffix = rid && rid.indexOf('nav:') === 0 ? rid.slice(4) : rid;
                    return '<label class="inline-flex items-start gap-2 text-xs text-slate-600 dark:text-slate-300 py-1 px-1.5 rounded hover:bg-slate-50 dark:hover:bg-white/5 cursor-pointer">' +
                        '<input type="checkbox" value="' + escapeHtml(rid) + '"' + (checked ? ' checked' : '') + '>' +
                        '<span class="flex flex-col leading-tight"><span class="truncate">' + escapeHtml(r.name) + '</span>' +
                        (idSuffix && idSuffix !== r.name ? '<span class="text-[10px] text-slate-400 dark:text-slate-500 truncate">' + escapeHtml(idSuffix) + '</span>' : '') +
                        '</span></label>';
                };
            };
            const listHtml = items.length
                ? items.map(fn(st.selected)).join('')
                : '<div class="text-xs text-slate-400 py-1">' + escapeHtml(t('admin_resources_none')) + '</div>';
            manage.innerHTML =
                '<div class="pt-1 pb-2">' +
                '<div class="flex gap-2 items-center pb-2">' +
                '<input type="text" class="agent-input resource-kind-search" autocomplete="off"' +
                ' data-1p-ignore data-lpignore="true" spellcheck="false"' +
                ' placeholder="' + escapeHtml(t('admin_resource_search_placeholder')) + '" value="' + escapeHtml(st.q || '') + '">' +
                '<button type="button" class="admin-row-btn resource-kind-clear">' + escapeHtml(t('admin_resource_clear')) + '</button>' +
                '</div>' +
                '<div class="grid grid-cols-2 gap-x-2 resource-kind-list">' + listHtml + '</div>' +
                '<div class="flex items-center justify-between pt-2">' +
                '<button type="button" class="admin-row-btn resource-kind-selectall">' + escapeHtml(t('admin_resource_selectall')) + '</button>' +
                '<span class="text-xs text-slate-400">' + escapeHtml(t('admin_total_label')) +
                ' <span class="font-medium">' + total + '</span></span>' +
                '</div>' +
                '<div class="resource-kind-pagination pt-1"></div>' +
                '</div>';
            // Wire search (reset to page 1).
            const search = manage.querySelector('.resource-kind-search');
            search.addEventListener('input', function () {
                st.q = search.value;
                st.page = 1;
                markModalDirty();
                clearTimeout(this._deb);
                this._deb = setTimeout(function () { _renderResourceList(kind); }, 250);
            });
            search.addEventListener('keyup', function (e) { if (e.key === 'Enter') { st.page = 1; _renderResourceList(kind); } });
            // Wire select-all (this page).
            manage.querySelector('.resource-kind-selectall').addEventListener('click', function () {
                items.forEach(function (r) { st.selected.add(r.resource_id); });
                _afterResourceSelectChanged(kind);
            });
            // Wire clear (deselect all for this kind).
            manage.querySelector('.resource-kind-clear').addEventListener('click', function () {
                st.selected.clear();
                _afterResourceSelectChanged(kind);
            });
            // Wire each checkbox.
            manage.querySelectorAll('.resource-kind-list input[type=checkbox]').forEach(function (cb) {
                cb.addEventListener('change', function () {
                    if (cb.checked) st.selected.add(cb.value);
                    else st.selected.delete(cb.value);
                    _afterResourceSelectChanged(kind);
                });
            });
            // Pagination.
            renderPagination(manage.querySelector('.resource-kind-pagination'), st.page, st.pageSize, total, function (p) {
                st.page = p;
                _renderResourceList(kind);
            });
        } catch (e) {
            manage.innerHTML = '<div class="py-1 text-xs text-red-500">' + escapeHtml(e.message || t('load_error')) + '</div>';
        }
    }

    function _updateResourceSummary(kind) {
        const row = _resourceKindRowFind(kind);
        if (!row) return;
        const n = _resCount(kind);
        const summaryEl = row.querySelector('.resource-kind-summary');
        if (summaryEl) {
            summaryEl.textContent = n ? t('admin_resources_selected').replace('{n}', n) : t('admin_resources_none');
        }
    }

    function _afterResourceSelectChanged(kind) {
        _updateResourceSummary(kind);
        markModalDirty();
        if (kind === 'model') {
            // Rebuild the model-default capability options from the new set.
            _refreshModelDefaultOptions();
        }
        _renderResourceList(kind);
    }

    function _collectResourceGrants() {
        const grants = [];
        _resourceKinds.forEach(function (k) {
            const st = _resourceState[k];
            if (!st || !st.selected.size) return;
            const actions = st.actions || _resourceActions[k] || [];
            st.selected.forEach(function (rid) {
                actions.forEach(function (a) {
                    grants.push({ resource_kind: k, resource_id: rid, action: a });
                });
            });
        });
        return grants;
    }

    function modelDefaultsHtml(f) {
        const id = 'adm-fld-modeldefaults';
        // Build model options from the currently selected model resources.
        const modelSel = (_resourceState && _resourceState.model && _resourceState.model.selected) ? _resourceState.model.selected : new Set();
        const options = Array.from(modelSel).map(function (rid) { return { value: rid, label: rid }; });
        const rows = _modelCapabilities.map(function (cap) {
            const val = _modelDefaultSel[cap] || '';
            const opts = '<option value="">' + escapeHtml(t('admin_resources_none')) + '</option>' +
                options.map(function (o) {
                    return '<option value="' + escapeHtml(o.value) + '"' + (String(o.value) === val ? ' selected' : '') + '>' +
                        escapeHtml(o.label) + '</option>';
                }).join('');
            return '<div class="flex items-center gap-2 py-1">' +
                '<span class="w-32 text-xs text-slate-500 dark:text-slate-400">' + escapeHtml(cap) + '</span>' +
                '<select class="agent-input model-default-select" data-cap="' + escapeHtml(cap) + '">' + opts + '</select>' +
                '</div>';
        }).join('');
        return '<div id="' + id + '" class="space-y-1">' +
            '<div class="text-xs text-slate-400 mb-1">' + escapeHtml(t('admin_resource_model_capabilities')) + '</div>' +
            rows + '</div>';
    }

    function _collectModelDefaults() {
        const out = {};
        _modelCapabilities.forEach(function (cap) {
            const v = _modelDefaultSel[cap];
            if (v) out[cap] = v;
        });
        return out;
    }

    // ---- tenant-level resource-grant picker (model / tool tabs) -----------
    //
    // One parameterized picker serves both the model tab and the tool tab: the
    // platform all-mode catalog is fetched by `resource_kind` and every checked
    // id is emitted with that kind's full allowed action set. The whole set is
    // replaced wholesale by the PUT, so a caller saving one kind must merge in
    // the other kinds' current grants or they would be dropped.
    const _tenantGrantState = {};

    function _tenantGrantStateFor(kind) {
        if (!_tenantGrantState[kind]) {
            _tenantGrantState[kind] = {
                sel: new Set(), catalog: [], apiBase: '', version: 0, actions: [],
            };
        }
        return _tenantGrantState[kind];
    }

    function tenantGrantHtml(f) {
        const kind = f.resourceKind;
        const st = _tenantGrantStateFor(kind);
        st.apiBase = f.apiBase || '';
        st.version = f.version || 0;
        st.actions = (f.actions || []).slice();
        // Seed selection from the field value (this kind's current grants only).
        st.sel = new Set();
        st.catalog = [];
        (f.value || []).forEach(function (g) {
            if (g && g.resource_kind === kind && g.resource_id) st.sel.add(g.resource_id);
        });
        const id = 'tenant-res-' + kind;
        const label = f.label
            ? '<div class="text-sm font-medium text-slate-700 dark:text-slate-200 mb-1">' + escapeHtml(f.label) + '</div>'
            : '';
        return '<div id="' + id + '" class="tenant-model-grant grant-picker" data-res-kind="' + escapeHtml(kind) + '">' +
            label +
            '<div class="flex gap-2 items-center pb-2">' +
            // Not a credential field, but it sits in a panel that also renders
            // password inputs, so browser password managers have autofilled it
            // (a stray "admin" here filtered every model out of the list).
            // Mirror the cfg key field's opt-out attributes.
            '<input type="text" class="agent-input resource-kind-search" autocomplete="off"' +
            ' data-1p-ignore data-lpignore="true" spellcheck="false"' +
            ' placeholder="' + escapeHtml(t('admin_resource_search_placeholder')) + '">' +
            '<button type="button" class="admin-row-btn resource-kind-clear">' + escapeHtml(t('admin_resource_clear')) + '</button>' +
            '</div>' +
            '<div class="grid grid-cols-2 gap-x-2 resource-kind-list">' +
            '<div class="py-1 text-xs text-slate-400 col-span-2">' + escapeHtml(t('admin_loading')) + '</div>' +
            '</div>' +
            '<div class="flex items-center justify-between pt-2">' +
            '<button type="button" class="admin-row-btn resource-kind-selectall">' + escapeHtml(t('admin_resource_selectall')) + '</button>' +
            '<span class="text-xs text-slate-400"><span class="resource-kind-selcount font-medium"></span></span>' +
            '</div>' +
            '<div class="resource-kind-pagination pt-1"></div>' +
            '</div>';
    }

    function tenantGrantSelection(kind) {
        return new Set(_tenantGrantStateFor(kind).sel);
    }

    function collectTenantGrants(kind) {
        const st = _tenantGrantStateFor(kind);
        const actions = st.actions.length ? st.actions : ['read'];
        const grants = [];
        st.sel.forEach(function (rid) {
            actions.forEach(function (action) {
                grants.push({ resource_kind: kind, resource_id: rid, action: action });
            });
        });
        return grants;
    }

    async function initTenantGrant(node, kind, onSelect) {
        const st = _tenantGrantStateFor(kind);
        if (!st.apiBase) return;
        const dirty = typeof onSelect === 'function' ? onSelect : markModalDirty;
        const list = node.querySelector('.resource-kind-list');
        const search = node.querySelector('.resource-kind-search');
        const selCount = node.querySelector('.resource-kind-selcount');
        if (!list || !search) return;
        // Second line of defence against the password-manager autofill above: a
        // readonly field is not autofilled, and it is released on the first
        // interaction so typing still works and it never looks disabled
        // (``.agent-input`` has explicit colours, so ``readonly`` does not grey it).
        search.setAttribute('readonly', 'readonly');
        ['focus', 'click', 'keydown'].forEach(function (evt) {
            search.addEventListener(evt, function () { search.removeAttribute('readonly'); },
                { once: true });
        });
        let q = '';
        let page = 1;
        const pageSize = 12;
        function updateCount() {
            if (selCount) selCount.textContent = t('admin_resources_selected').replace('{n}', st.sel.size);
        }
        async function render() {
            const query = qs({ kind: kind, q: q, page: page, page_size: pageSize });
            try {
                const data = await apiFetch(st.apiBase + '?' + query);
                st.catalog = data.items || [];
                const total = data.total || 0;
                if (!st.catalog.length) {
                    // An empty *search result* must not read as "you have
                    // selected nothing". Reusing ``admin_resources_none`` here
                    // hid the real cause: a filter was on, so the tab looked
                    // broken with no hint that clearing the box would fix it.
                    const msg = q
                        ? t('admin_resources_no_match').replace('{q}', q)
                        : t('admin_resources_none');
                    list.innerHTML = '<div class="py-1 text-xs text-slate-400 col-span-2">' + escapeHtml(msg) + '</div>';
                } else {
                    list.innerHTML = st.catalog.map(function (r) {
                        const checked = st.sel.has(r.resource_id);
                        const idSuffix = r.resource_id.indexOf('provider:') === 0 ? r.resource_id : '';
                        return '<label class="inline-flex items-start gap-2 text-xs text-slate-600 dark:text-slate-300 py-1 px-1.5 rounded hover:bg-slate-50 dark:hover:bg-white/5 cursor-pointer">' +
                            '<input type="checkbox" value="' + escapeHtml(r.resource_id) + '"' + (checked ? ' checked' : '') + '>' +
                            '<span class="flex flex-col leading-tight"><span class="truncate">' + escapeHtml(r.name) + '</span>' +
                            (idSuffix && idSuffix !== r.name ? '<span class="text-[10px] text-slate-400 dark:text-slate-500 truncate">' + escapeHtml(idSuffix) + '</span>' : '') +
                            '</span></label>';
                    }).join('');
                }
                list.querySelectorAll('input[type=checkbox]').forEach(function (cb) {
                    cb.addEventListener('change', function () {
                        if (cb.checked) st.sel.add(cb.value);
                        else st.sel.delete(cb.value);
                        dirty();
                        updateCount();
                    });
                });
                const pag = node.querySelector('.resource-kind-pagination');
                renderPagination(pag, page, pageSize, total, function (p) { page = p; render(); });
            } catch (e) {
                list.innerHTML = '<div class="py-1 text-xs text-red-500 col-span-2">' + escapeHtml(e.message || t('load_error')) + '</div>';
            }
        }
        function setSearch(v) {
            q = v; page = 1;
        }
        search.addEventListener('input', function () {
            setSearch(search.value);
            clearTimeout(this._deb);
            this._deb = setTimeout(render, 250);
        });
        search.addEventListener('keyup', function (e) { if (e.key === 'Enter') { setSearch(search.value); render(); } });
        node.querySelector('.resource-kind-clear').addEventListener('click', function () {
            // "清空" must return the picker to a clean, fully visible state.
            // Deselecting alone left any search term in place, so the list
            // stayed empty and the button looked like it did nothing.
            st.sel.clear();
            setSearch('');
            search.value = '';
            dirty();
            updateCount();
            render();
        });
        node.querySelector('.resource-kind-selectall').addEventListener('click', function () {
            (st.catalog || []).forEach(function (r) { st.sel.add(r.resource_id); });
            dirty();
            updateCount();
            render();
        });
        updateCount();
        await render();
    }


    // ---- account picker field (userpicker) --------------------------------
    //
    // `_tenantSaveAdmin` (the tenant editor's management tab) needs the operator
    // to choose an existing valid account by its stable `usr_…` id. A free-text
    // field made that impossible: the server
    // resolves the target with `SELECT * FROM users WHERE id=?` and answers one
    // opaque `user not found or disabled` for both a bad id and a disabled
    // account, so a mistyped tenant code or login name looked like a missing
    // account. The picker reads the same candidate set the platform user list
    // already exposes to the same role (platform admin), so no new visibility.
    const _userPickerState = {};
    // The picker's node+state are kept so an already-rendered picker can be
    // re-queried without losing what the operator selected.
    const _userPickerRegistry = {};
    let _userPickerSeq = 0;

    // Shared markup so the tenant editor's admin tab and the create-admin modal
    // render an identical picker (same classes, same collection contract).
    function _userPickerHtml(id, f) {
        return '<div id="' + id + '" class="user-picker" data-name="' + escapeHtml(f.name) + '">' +
            '<div class="relative">' +
            '<input type="text" class="agent-input user-picker-search" placeholder="' +
            escapeHtml(t('admin_user_picker_search_placeholder')) + '">' +
            '</div>' +
            '<div class="user-picker-selected"></div>' +
            '<div class="user-picker-list mt-2 max-h-56 overflow-y-auto rounded-lg border border-slate-200 dark:border-white/10"></div>' +
            '<div class="user-picker-status agent-field-hint"></div>' +
            '</div>';
    }

    async function _loadUserPickerCandidates(node, state) {
        // Only the newest query may render. `apiFetch`'s own stale-response guard
        // is keyed on the tenant generation, so it does not cover two searches
        // racing inside the same tenant.
        const seq = ++_userPickerSeq;
        const list = node.querySelector('.user-picker-list');
        const statusEl = node.querySelector('.user-picker-status');
        list.innerHTML = '<div class="px-3 py-2 text-xs text-slate-400">' + escapeHtml(t('admin_user_picker_loading')) + '</div>';
        try {
            const query = qs({ status: 'active', page: 1, page_size: 100, q: state.q || '' });
            const data = await apiFetch('/api/platform/users?' + query);
            if (seq !== _userPickerSeq) return;
            const items = data.items || [];
            const total = data.total || 0;
            if (!items.length) {
                list.innerHTML = '';
                statusEl.textContent = t('admin_user_picker_empty');
                return;
            }
            list.innerHTML = items.map(function (u) {
                return '<button type="button" class="user-picker-option w-full text-left px-3 py-2 rounded-md hover:bg-slate-50 dark:hover:bg-white/5" data-user-id="' +
                    escapeHtml(u.id) + '" data-user-name="' +
                    escapeHtml(u.display_name || u.username) + '">' +
                    '<span class="block text-sm text-slate-700 dark:text-slate-200">' + escapeHtml(u.display_name || u.username) + '</span>' +
                    '<span class="block text-[10px] text-slate-400 dark:text-slate-500">' + escapeHtml(u.username) +
                    (u.is_platform_admin ? ' · ' + escapeHtml(t('platform_admin_badge')) : '') + '</span>' +
                    '</button>';
            }).join('');
            // Never present a silently truncated candidate set as complete.
            statusEl.textContent = total > items.length ? t('admin_user_picker_truncated') : '';
            list.querySelectorAll('.user-picker-option').forEach(function (btn) {
                btn.addEventListener('click', function () {
                    state.id = btn.getAttribute('data-user-id');
                    state.label = (btn.textContent || '').trim();
                    // The option's text also carries the username line, so the
                    // clean display name comes from its own attribute.
                    state.displayName = btn.getAttribute('data-user-name') || '';
                    _userPickerState[state.name] = state.id;
                    _renderUserPickerSelected(node, state);
                    // The modal and the tenant editor's admin tab both host this
                    // picker; only the modal marks itself dirty by default.
                    if (typeof state.onSelect === 'function') state.onSelect(state);
                    else markModalDirty();
                });
            });
        } catch (e) {
            if (seq !== _userPickerSeq || e.message === 'stale-response') return;
            // A failed load must stay distinguishable from an empty candidate
            // set, and must leave the picked id empty so submit is blocked.
            list.innerHTML = '';
            state.id = '';
            _userPickerState[state.name] = '';
            statusEl.textContent = t('admin_user_picker_load_failed');
        }
    }

    function _renderUserPickerSelected(node, state) {
        const el = node.querySelector('.user-picker-selected');
        if (!el) return;
        el.innerHTML = state.id
            ? '<div class="mt-2 text-xs text-slate-600 dark:text-slate-300">' + escapeHtml(t('admin_user_picker_selected')) +
              ' <span class="font-medium">' + escapeHtml(state.label || state.id) + '</span></div>'
            : '';
    }

    function _initUserPicker(node, f) {
        const state = { name: f.name, id: '', label: '', displayName: '',
                        q: '', onSelect: f.onSelect || null };
        _userPickerRegistry[f.name] = { node: node, state: state };
        // Seed from any provided value so an edit dialog could prefill later.
        const preset = (node.getAttribute('data-value') || '').trim();
        if (preset) { state.id = preset; _userPickerState[f.name] = preset; }
        _renderUserPickerSelected(node, state);
        const search = node.querySelector('.user-picker-search');
        if (search) {
            let deb;
            search.addEventListener('input', function () {
                state.q = search.value;
                clearTimeout(deb);
                deb = setTimeout(function () { _loadUserPickerCandidates(node, state); }, 300);
            });
        }
        return _loadUserPickerCandidates(node, state);
    }

    // Re-query an already-rendered picker's candidates while keeping whatever
    // the operator has selected (a create makes the earlier list stale).
    function _refreshUserPicker(name) {
        const entry = _userPickerRegistry[name];
        if (!entry) return Promise.resolve();
        return _loadUserPickerCandidates(entry.node, entry.state);
    }

    function _initResourceGroup(node) {
        // Bind the kind toggle (expand/collapse the management area).
        node.querySelectorAll('.resource-kind-toggle').forEach(function (toggle) {
            toggle.addEventListener('click', function () {
                const row = toggle.closest('.resource-kind-row');
                const kind = row && row.getAttribute('data-kind');
                const manage = row && row.querySelector('.resource-kind-manage');
                if (!kind) return;
                if (manage.classList.contains('hidden')) {
                    _openResourceManage(kind);
                } else {
                    _closeResourceManage(kind);
                }
            });
        });
    }

    function _refreshModelDefaultOptions() {
        const node = document.getElementById('adm-fld-modeldefaults');
        if (!node) return;
        const modelSel = (_resourceState.model && _resourceState.model.selected) ? _resourceState.model.selected : new Set();
        const options = Array.from(modelSel).map(function (rid) { return { value: rid, label: rid }; });
        node.querySelectorAll('.model-default-select').forEach(function (sel) {
            const cur = sel.value;
            sel.innerHTML = '<option value="">' + escapeHtml(t('admin_resources_none')) + '</option>' +
                options.map(function (o) {
                    return '<option value="' + escapeHtml(o.value) + '"' + (String(o.value) === cur ? ' selected' : '') + '>' +
                        escapeHtml(o.label) + '</option>';
                }).join('');
        });
    }

    function _initModelDefaults(node) {
        node.querySelectorAll('.model-default-select').forEach(function (sel) {
            sel.addEventListener('change', function () {
                const cap = sel.getAttribute('data-cap');
                const v = sel.value;
                if (v) _modelDefaultSel[cap] = v;
                else delete _modelDefaultSel[cap];
                markModalDirty();
            });
        });
    }

    function collectField(f) {
        if (f.type === 'resourcegroup') {
            return _collectResourceGrants();
        }
        if (f.type === 'modeldefaults') {
            return _collectModelDefaults();
        }
        if (f.type === 'userpicker') {
            // Never fall back to reading an input's value: the picked id is the
            // only acceptable source, and "nothing picked" must stay empty so the
            // shared required-field check rejects the submit.
            return _userPickerState[f.name] || '';
        }
        const el = document.getElementById('adm-fld-' + f.name);
        if (!el) return undefined;
        if (f.type === 'checkbox') return !!el.checked;
        if (f.type === 'multi') {
            return Array.prototype.slice.call(el.querySelectorAll('input:checked')).map(function (i) { return i.value; });
        }
        return el.value;
    }

    function openAdminModal(cfg) {
        const el = ensureAdminModal();
        document.getElementById('admin-modal-title').textContent = cfg.title || '';
        document.getElementById('admin-modal-subtitle').textContent = cfg.subtitle || '';
        document.getElementById('admin-modal-icon').className = 'fas ' + (cfg.icon || 'fa-plus') + ' text-sm';
        const submitBtn = document.getElementById('admin-modal-submit');
        const submitLabel = submitBtn.querySelector('.admin-modal-submit-label');
        if (submitLabel) submitLabel.textContent = cfg.submitLabel || t('save');
        submitBtn.disabled = false;
        document.getElementById('admin-modal-cancel').textContent = t('cancel');
        _adminModal = {
            open: true, dirty: false,
            fields: cfg.fields || [],
            submit: cfg.submit || null,
            onConflictReload: cfg.onConflictReload || null,
            afterSuccess: cfg.afterSuccess || null,
            statusEl: cfg.statusEl || null,
            successMsg: cfg.successMsg || t('admin_saved'),
        };
        const body = document.getElementById('admin-modal-body');
        body.innerHTML = renderModalBody(cfg.fields || []);
        closeAdminErr();
        (cfg.fields || []).forEach(function (f) {
            // The modal-control container id is stamped by the field renderer.
            // resourcegroup uses `adm-fld-resource_grants` (matches f.name), while
            // modeldefaults renders `adm-fld-modeldefaults`; resolve the id the
            // same way the renderer did so _initModelDefaults can bind.
            const nodeId = f.type === 'modeldefaults' ? 'adm-fld-modeldefaults' : ('adm-fld-' + f.name);
            const node = document.getElementById(nodeId);
            if (!node) return;
            node.addEventListener('input', markModalDirty);
            if (f.type === 'multi') {
                node.querySelectorAll('input[type=checkbox]').forEach(function (cb) { cb.addEventListener('change', markModalDirty); });
            } else {
                node.addEventListener('change', markModalDirty);
            }
            if (f.type === 'resourcegroup') {
                _initResourceGroup(node);
            } else if (f.type === 'modeldefaults') {
                _initModelDefaults(node);
            } else if (f.type === 'userpicker') {
                _initUserPicker(node, f);
            }
        });
        el.classList.remove('hidden');
        const first = body.querySelector('input, textarea, select');
        if (first) { try { first.focus(); } catch (e) {} }
    }

    function markModalDirty() { _adminModal.dirty = true; }

    function closeAdminErr() {
        const el = document.getElementById('admin-modal-error');
        if (el) { el.classList.add('hidden'); el.textContent = ''; }
    }

    function showAdminErr(msg) {
        const el = document.getElementById('admin-modal-error');
        if (el) { el.textContent = msg || ''; el.classList.remove('hidden'); }
    }

    function closeAdminModal() {
        if (_adminModal.dirty && !confirmDiscard(true)) return;
        closeAdminModalNoPrompt();
    }

    function closeAdminModalNoPrompt() {
        const el = document.getElementById('admin-modal');
        if (el) el.classList.add('hidden');
        _adminModal = { open: false, dirty: false, fields: [], submit: null, onConflictReload: null, afterSuccess: null, statusEl: null, successMsg: '' };
    }

    async function submitAdminModal() {
        const cfg = _adminModal;
        if (!cfg.submit) return;
        const body = {};
        cfg.fields.forEach(function (f) {
            if (f.type === 'locked') return;
            body[f.name] = collectField(f);
        });
        for (let i = 0; i < cfg.fields.length; i++) {
            const f = cfg.fields[i];
            if (!f.required) continue;
            const v = body[f.name];
            if (v == null || v === '' || (Array.isArray(v) && v.length === 0)) {
                showAdminErr(f.type === 'userpicker'
                    ? t('admin_user_picker_required')
                    : t('admin_required_field'));
                return;
            }
        }
        const btn = document.getElementById('admin-modal-submit');
        btn.disabled = true;
        closeAdminErr();
        try {
            await cfg.submit(body);
            closeAdminModalNoPrompt();
            if (cfg.statusEl) status(cfg.statusEl, cfg.successMsg, true);
            // `afterSuccess` runs AFTER the modal is dismissed and `_adminModal`
            // is reset, so a follow-up dialog opened here is not immediately
            // closed and does not inherit this modal's dirty/field state. Its
            // failures must not turn a committed write back into an error.
            if (cfg.afterSuccess) {
                try { await cfg.afterSuccess(body); } catch (e) { /* follow-up only */ }
            }
        } catch (err) {
            if (err.status === 409 || err.code === 'conflict') {
                showAdminErr(err.message || t('admin_conflict'));
                if (cfg.onConflictReload) cfg.onConflictReload();
                closeAdminModalNoPrompt();
                if (cfg.statusEl) status(cfg.statusEl, err.message || t('admin_conflict'), false);
            } else if (err.status === 401) {
                showAdminErr(err.message || t('account_credentials_error'));
            } else {
                // A rejected write may carry a server code the operator can act
                // on (e.g. `weak_password`). Map it to the actionable reason
                // before falling back to the raw server message, so a
                // correctable input never reads as an unexplained failure. The
                // underlying evidence is logged for later diagnosis.
                try {
                    console.error('[admin-write] failed:', 'http ' + (err.status || '?'),
                        err.code || '', err.message || '');
                } catch (e) { /* logging must never mask the failure */ }
                const reason = _tenantAdminNewReason(err);
                showAdminErr(reason || (err.data && err.data.message) || err.message || t('admin_save_failed'));
            }
        } finally {
            btn.disabled = false;
        }
    }

    // ---- option builders -------------------------------------------------
    function roleOptions(roles) {
        return (roles || []).map(function (r) {
            return { value: r.code, label: r.name + ' (' + r.code + ')' };
        });
    }

    function deptOptions(depts, excludeId) {
        const opts = [];
        let rootId = '';
        (depts || []).forEach(function (d) {
            if (d.code === '__root__') { rootId = d.id; return; }
            if (excludeId && d.id === excludeId) return;
        });
        opts.push({ value: rootId, label: t('admin_field_parent_none') });
        (depts || []).forEach(function (d) {
            if (d.code === '__root__') return;
            if (excludeId && d.id === excludeId) return;
            opts.push({ value: d.id, label: d.name });
        });
        return opts;
    }

    function memberDeptOptions(depts) {
        const opts = [{ value: '', label: t('admin_field_department_none') }];
        (depts || []).forEach(function (d) {
            if (d.code === '__root__') return;
            opts.push({ value: d.id, label: d.name });
        });
        return opts;
    }

    function permOptions(perms) {
        return (perms || []).map(function (p) {
            if (typeof p === 'string') return { value: p, label: p };
            return { value: p.id, label: p.label || p.id };
        });
    }

    // Group the permission catalog by its `group` field (task 5.5).
    function permGroups(catalog) {
        const groups = {};
        (catalog || []).forEach(function (p) {
            const g = p.group || '';
            (groups[g] = groups[g] || []).push(p);
        });
        return groups;
    }

    async function fetchRoles() {
        try {
            const d = await apiFetch('/api/tenant/roles');
            _roles = d.items || [];
            _roleById = {};
            _roles.forEach(function (r) { _roleById[r.id] = r; });
            return _roles;
        } catch (e) { _roles = []; _roleById = {}; return []; }
    }

    async function fetchDepts() {
        try {
            const d = await apiFetch('/api/tenant/departments');
            _depts = d.items || [];
            _deptById = {};
            _depts.forEach(function (x) { _deptById[x.id] = x; });
            return _depts;
        } catch (e) { _depts = []; _deptById = {}; return []; }
    }

    // Tenants the actor administers (active tenant_admin). This is the candidate
    // set for the member tenant multi-select; a platform admin is NOT broadened
    // to all tenants (see `administered_tenants` in auth/service.py). Pass a
    // target `user_id` to also learn that user's membership status within these
    // administered tenants (never other tenants).
    let _adminTenants = null;
    async function administeredTenants(targetUserId) {
        if (!targetUserId && _adminTenants) return _adminTenants;
        const path = '/api/identity/administered-tenants' +
            (targetUserId ? '?' + qs({ user_id: targetUserId }) : '');
        const data = await apiFetch(path);
        const items = data.items || [];
        if (!targetUserId) _adminTenants = items;
        return items;
    }

    function tenantOptions(tenants) {
        return (tenants || []).map(function (t) {
            return { value: t.id, label: t.name + ' (' + t.code + ')' };
        });
    }

    async function ensurePermCatalog() {
        if (_permCatalog) return _permCatalog;
        try {
            const d = await apiFetch('/api/tenant/permissions');
            if (!Array.isArray(d.permissions) || !d.permissions.length) {
                throw new Error('invalid-permission-catalog');
            }
            // Build into a local first; only cache a fully-valid catalog so a
            // transient/invalid response never becomes a retryable empty state
            // that would let a modal open without real permission choices.
            const parsed = d.permissions.map(function (p) {
                if (typeof p === 'string') return { id: p, label: p, group: '' };
                return {
                    id: p.id || '',
                    label: p.label || p.id,
                    group: p.group || '',
                    description: p.description || '',
                    scope: p.scope || '',
                    assignable: p.assignable !== false,
                };
            });
            if (parsed.some(function (p) { return !p.id || !p.id.trim(); })) {
                throw new Error('invalid-permission-catalog');
            }
            _permCatalog = parsed;
        } catch (e) {
            if (e.message !== 'stale-response') {
                status(document.getElementById('role-status'), t('admin_permissions_load_failed'), false);
            }
            return null;
        }
        return _permCatalog;
    }

    // ---- pagination control ----------------------------------------------
    function renderPagination(container, page, pageSize, total, onPage) {
        if (!container) return;
        if (!total || total <= pageSize) {
            container.innerHTML = '';
            return;
        }
        const pages = Math.ceil(total / pageSize);
        const prev = '<button class="admin-row-btn" data-pg="' + (page - 1) + '"' + (page <= 1 ? ' disabled style="opacity:.4"' : '') + '><i class="fas fa-chevron-left mr-1"></i>' + escapeHtml(t('admin_prev')) + '</button>';
        const next = '<button class="admin-row-btn" data-pg="' + (page + 1) + '"' + (page >= pages ? ' disabled style="opacity:.4"' : '') + '>' + escapeHtml(t('admin_next')) + '<i class="fas fa-chevron-right ml-1"></i></button>';
        container.innerHTML = '<div class="text-xs text-slate-400">' +
            escapeHtml(t('admin_total_label')) + ' <span class="font-semibold text-slate-600 dark:text-slate-300">' + total + '</span>' +
            (pages > 1 ? ' · ' + escapeHtml(t('admin_page_label')) + ' ' + page + '/' + pages : '') +
            '</div><div class="flex gap-2">' + prev + next + '</div>';
        container.querySelectorAll('[data-pg]').forEach(function (btn) {
            btn.addEventListener('click', function () { onPage(parseInt(btn.dataset.pg, 10)); });
        });
    }

    // ---- helper to know if the account is a platform admin ----------------
    // The self profile (/auth/me) is authoritative. We cache it once so the
    // admin views can decide whether to render platform-only controls; the
    // backend still independently enforces every authorization.
    let _selfProfile = null;
    async function isPlatformAdmin() {
        if (_selfProfile) return !!_selfProfile.user?.is_platform_admin;
        try {
            const resp = await fetch('/auth/me', { credentials: 'same-origin', cache: 'no-store' });
            const data = await resp.json().catch(() => ({}));
            if (data && data.status === 'success' && data.user) {
                _selfProfile = data;
                return !!data.user.is_platform_admin;
            }
            return false;
        } catch (e) { return false; }
    }

    // ---- Tenant view ------------------------------------------------------
    async function loadTenantView() {
        const list = document.getElementById('tenant-list');
        const btn = document.getElementById('tenant-create-btn');
        const empty = document.getElementById('tenant-empty');
        if (!list) return;
        // Ordinary members with only tenant.info.read must NOT request the
        // platform tenant list (server rejects 403); route them to the read-only
        // current-tenant profile instead.
        if (!(await isPlatformAdmin())) {
            await loadTenantReadOnly();
            return;
        }
        list.innerHTML = '<div class="text-sm text-slate-400 dark:text-slate-500">' + t('tenant_loading') + '</div>';
        try {
            const searchEl = document.getElementById('tenant-search');
            const q = searchEl ? searchEl.value : '';
            const filterEl = document.getElementById('tenant-status-filter');
            const status = (filterEl && filterEl.value) || '';
            const query = qs({ q: q, status: status });
            const data = await apiFetch('/api/platform/tenants' + (query ? '?' + query : ''));
            if (!data.items || !data.items.length) {
                list.innerHTML = '';
                empty.classList.remove('hidden');
                btn.classList.add('hidden');
                return;
            }
            empty.classList.add('hidden');
            btn.classList.remove('hidden');
            _tenantById = {};
            const currentTenantId = sessionStorage.getItem('cow_tenant_id') || '';
            const rows = data.items.map(function (tn) {
                _tenantById[tn.id] = tn;
                const archived = !!tn.archived;
                const actions = [];
                if (archived) {
                    actions.push('<span class="text-xs px-2 py-0.5 rounded bg-slate-100 dark:bg-white/10 text-slate-500 dark:text-slate-400">' + escapeHtml(t('tenant_archived_tag')) + '</span>');
                    actions.push('<button class="admin-row-btn" onclick="adminRowAction(\'tenant\',\'restore\',\'' + escapeHtml(tn.id) + '\')"><i class="fas fa-rotate-left mr-1"></i>' + escapeHtml(t('tenant_restore')) + '</button>');
                } else {
                    actions.push('<button class="admin-row-btn" onclick="adminRowAction(\'tenant\',\'edit\',\'' + escapeHtml(tn.id) + '\')"><i class="fas fa-pen mr-1"></i>' + escapeHtml(t('admin_edit')) + '</button>');
                    // The default tenant and the operator's own current tenant are
                    // protected server-side; hide the entry point so the UI never
                    // offers an action that is guaranteed to fail.
                    if (tn.code !== 'default' && tn.id !== currentTenantId) {
                        actions.push('<button class="admin-row-btn danger" onclick="adminRowAction(\'tenant\',\'delete\',\'' + escapeHtml(tn.id) + '\')"><i class="fas fa-trash mr-1"></i>' + escapeHtml(t('tenant_delete')) + '</button>');
                    }
                }
                const subtitle = archived
                    ? escapeHtml(tn.code) + ' · ' + escapeHtml(t('tenant_archived_tag'))
                    : escapeHtml(tn.code) + ' · ' + fmtActive(tn.active);
                return '<div class="flex items-center justify-between px-4 py-3 rounded-lg border border-slate-200 dark:border-white/10">'
                    + '<div class="flex items-center gap-3"><div class="w-9 h-9 rounded-lg bg-primary-50 dark:bg-primary-900/30 flex items-center justify-center">'
                    + '<i class="fas fa-building text-primary-500"></i></div>'
                    + '<div><div class="text-sm font-medium text-slate-800 dark:text-slate-100">' + escapeHtml(tn.name) + '</div>'
                    + '<div class="text-xs text-slate-400">' + subtitle + '</div></div></div>'
                    + '<div class="flex items-center gap-2"><div class="text-xs text-slate-400">' + t('tenant_version_label') + ' ' + tn.version + '</div>'
                    + actions.join('')
                    + '</div></div>';
            }).join('');
            list.innerHTML = rows;
        } catch (err) {
            if (err.message === 'stale-response') return;
            list.innerHTML = '<div class="text-sm text-red-500">' + t('load_error') + ': ' + escapeHtml(err.message) + '</div>';
            status(document.getElementById('tenant-status'), err.message, false);
        }
    }

    // Read-only current-tenant profile for members with tenant.info.read.
    async function loadTenantReadOnly() {
        const list = document.getElementById('tenant-list');
        const btn = document.getElementById('tenant-create-btn');
        const empty = document.getElementById('tenant-empty');
        const searchEl = document.getElementById('tenant-search');
        if (searchEl) searchEl.classList.add('hidden');
        if (btn) btn.classList.add('hidden');
        if (!list) return;
        list.innerHTML = '<div class="text-sm text-slate-400 dark:text-slate-500">' + t('tenant_loading') + '</div>';
        try {
            const data = await apiFetch('/api/tenant');
            const tn = data.tenant || {};
            if (empty) empty.classList.add('hidden');
            list.innerHTML = '<div class="px-4 py-4 rounded-lg border border-slate-200 dark:border-white/10">'
                + '<div class="flex items-center gap-3"><div class="w-10 h-10 rounded-lg bg-primary-50 dark:bg-primary-900/30 flex items-center justify-center">'
                + '<i class="fas fa-building text-primary-500"></i></div>'
                + '<div><div class="text-sm font-medium text-slate-800 dark:text-slate-100">' + escapeHtml(tn.name) + '</div>'
                + '<div class="text-xs text-slate-400">' + escapeHtml(tn.code) + ' · ' + fmtActive(tn.active) + '</div></div></div>'
                + '</div>';
        } catch (err) {
            if (err.message === 'stale-response') return;
            list.innerHTML = '<div class="text-sm text-red-500">' + t('load_error') + ': ' + escapeHtml(err.message) + '</div>';
            status(document.getElementById('tenant-status'), err.message, false);
        }
    }

    // Soft-delete (archive) a tenant. Deleting requires only the platform
    // admin's current password: the password prompt is the confirmation for a
    // destructive-but-reversible action (the tenant and all its data are kept
    // and can be restored). The server re-validates the password, the released
    // version and the protected-tenant rules in ``archive_tenant``.
    function archiveTenant(id) {
        const tn = _tenantById[id];
        if (!tn) return;
        openAdminModal({
            title: t('tenant_archive_title'),
            subtitle: t('tenant_archive_subtitle'),
            icon: 'fa-trash',
            submitLabel: t('tenant_delete'),
            statusEl: document.getElementById('tenant-status'),
            successMsg: t('tenant_archived'),
            fields: [
                { type: 'password', name: 'recent_password',
                  label: t('admin_field_recent_password'),
                  required: true, hint: t('admin_field_recent_password_hint') },
            ],
            submit: async function (body) {
                try {
                    await apiFetch('/api/platform/tenants/' + encodeURIComponent(id), {
                        method: 'DELETE',
                        body: {
                            expected_version: tn.version,
                            recent_password: body.recent_password,
                        },
                        suppressAuthOverlay: true,
                    });
                } catch (e) {
                    if (e && e.status === 401) {
                        // A rejected password keeps the dialog open for a retry;
                        // clear the field and say what was wrong instead of the
                        // generic "unauthorized" the request helper throws.
                        const input = document.getElementById('adm-fld-recent_password');
                        if (input) input.value = '';
                        const retry = new Error(t('tenant_password_wrong'));
                        retry.status = 401;
                        throw retry;
                    }
                    throw e;
                }
                await loadTenantView();
            },
            onConflictReload: function () { loadTenantView(); },
        });
    }

    // Restore an archived tenant. Reuses the shared password prompt (which
    // reopens on a wrong password so a typo costs a retry, not the whole flow).
    async function restoreTenant(id) {
        const tn = _tenantById[id];
        if (!tn) return;
        const outcome = await withTenantPassword(async function (pw) {
            await apiFetch('/api/platform/tenants/' + encodeURIComponent(id), {
                method: 'POST',
                body: {
                    operation: 'restore',
                    expected_version: tn.version,
                    recent_password: pw,
                },
                suppressAuthOverlay: true,
            });
        });
        if (outcome.cancelled) return;
        if (!outcome.ok) {
            const err = outcome.error || {};
            status(document.getElementById('tenant-status'), err.message || t('admin_save_failed'), false);
            if (err.status === 409 || err.code === 'conflict') loadTenantView();
            return;
        }
        status(document.getElementById('tenant-status'), t('tenant_restored'), true);
        await loadTenantView();
    }

    // ---- Tenant create/edit page (tabbed editor) --------------------------
    //
    // Replaces the old tenant modal. Five tabs: basic information, model
    // authorization, tool authorization, agent provisioning, and tenant
    // management (admin + space). Each tab saves independently, so a guard
    // rejected on one tab cannot lose another tab's edits.
    //
    // The editor reuses the role editor's structural CSS vocabulary
    // (`role-editor-*` classes) and also carries `tenant-editor-*` class names,
    // so the shared tabbed-editor rules keep a single definition while the
    // tenant editor stays addressable on its own.
    const TENANT_TABS = ['basic', 'model', 'tool', 'agent', 'admin'];
    // A granted resource keeps the kind's FULL allowed action set: a tenant admin
    // must be able to read/execute/configure what the platform allocated.
    const TENANT_GRANT_ACTIONS = {
        model: ['read', 'use'],
        tool: ['read', 'execute', 'configure'],
    };
    const TENANT_ADMIN_PICKER_NAME = 'tenant_admin_user_id';

    let _tenantEditor = _tenantEditorIdle();

    function _tenantEditorIdle() {
        return {
            open: false, mode: 'create', id: null, tab: 'basic',
            code: '', name: '', active: true, version: 0,
            // Which way the admin tab appoints an admin; "existing" is the
            // pre-existing behaviour and stays the default.
            adminMode: 'existing',
            space: null, grants: [], dirty: {}, tabLoaded: {},
            // Read-only projection of the tenant's current valid tenant_admin
            // (earliest bound first), plus whether the read has landed and
            // whether it failed. `adminLoaded`/`adminLoadFailed` are what let
            // "no admin" stay distinguishable from "could not read".
            admins: [], adminLoaded: false, adminLoadFailed: false,
            // The Agent tab: the tenant's own bound agents (read-only) and the
            // resolved source tenant's copyable candidates. `agentLoadFailed`
            // keeps "could not read" distinct from "no agents"; `agentPending`
            // remembers which candidates were checked so a partial copy can be
            // retried without re-picking them.
            agents: [], agentLoaded: false, agentLoadFailed: false,
            copySource: null, agentSourceError: '', agentResult: null,
            agentPending: {},
        };
    }

    function _tenantResourcesBase(id) {
        return '/api/platform/tenants/' + encodeURIComponent(id) + '/resources';
    }

    function _tenantCatalogBase(id) {
        return '/api/platform/tenants/' + encodeURIComponent(id) + '/authorization/catalog';
    }

    function _tenantGrantActions(kind) {
        return TENANT_GRANT_ACTIONS[kind] || ['read'];
    }

    function tenantEditorIsDirty() {
        return TENANT_TABS.some(function (tab) { return !!_tenantEditor.dirty[tab]; });
    }

    function markTenantDirty(tab) {
        _tenantEditor.dirty[tab || _tenantEditor.tab] = true;
        const pill = document.getElementById('tenant-dirty-pill');
        if (pill) pill.classList.add('show');
    }

    function _tenantEditorClearDirty(tab) {
        _tenantEditor.dirty[tab] = false;
        const pill = document.getElementById('tenant-dirty-pill');
        if (pill) pill.classList.toggle('show', tenantEditorIsDirty());
    }

    function _tenantEditorSetError(msg) {
        const el = document.getElementById('tenant-editor-error');
        if (!el) return;
        el.textContent = msg || '';
        el.classList.toggle('hidden', !msg);
    }

    // ---- unified save: password prompt ------------------------------------

    function _setTenantPasswordError(msg) {
        const el = document.getElementById('tenant-password-error');
        if (!el) return;
        el.textContent = msg || '';
        el.classList.toggle('hidden', !msg);
    }

    function _openTenantPasswordModal() {
        let el = document.getElementById('tenant-password-modal');
        if (!el) {
            el = document.createElement('div');
            el.id = 'tenant-password-modal';
            el.className = 'agent-modal tenant-password-modal hidden';
            el.innerHTML =
                '<div class="agent-modal-card">' +
                '<h3 class="agent-modal-title" id="tenant-password-title"></h3>' +
                '<div class="agent-modal-body">' +
                '<p id="tenant-password-hint" class="text-sm text-slate-500 dark:text-slate-400 mb-3"></p>' +
                '<input type="password" id="tenant-password-input" class="agent-input w-full" value="">' +
                '<p id="tenant-password-error" class="hidden text-xs text-red-500 mt-2"></p>' +
                '</div>' +
                '<div class="agent-modal-foot justify-end">' +
                '<button type="button" class="px-4 py-2 rounded-lg text-sm font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-white/10" id="tenant-password-cancel"></button>' +
                '<button type="button" class="px-5 py-2 rounded-lg bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium" id="tenant-password-confirm"></button>' +
                '</div></div>';
            document.body.appendChild(el);
        }
        el.classList.remove('hidden');
        const input = document.getElementById('tenant-password-input');
        if (input) {
            input.value = '';
            if (typeof input.focus === 'function') input.focus();
        }
        _setTenantPasswordError('');
        const title = document.getElementById('tenant-password-title');
        if (title) title.textContent = t('tenant_password_title');
        const hint = document.getElementById('tenant-password-hint');
        if (hint) hint.textContent = t('tenant_password_hint');
        const cancel = document.getElementById('tenant-password-cancel');
        if (cancel) cancel.textContent = t('cancel');
        const confirm = document.getElementById('tenant-password-confirm');
        if (confirm) confirm.textContent = t('admin_save');
        return el;
    }

    function _closeTenantPasswordModal() {
        const input = document.getElementById('tenant-password-input');
        // Drop the secret as soon as it is no longer needed.
        if (input) input.value = '';
        _setTenantPasswordError('');
        const el = document.getElementById('tenant-password-modal');
        if (!el) return;
        if (el.parentNode && typeof el.parentNode.removeChild === 'function') {
            el.parentNode.removeChild(el);
        } else {
            el.classList.add('hidden');
        }
    }

    // Run `work(password)` behind a password prompt, re-running it in place when
    // the password is rejected so a typo costs a retry instead of the whole
    // form. `work` is expected to resume from the first unfinished step.
    function withTenantPassword(work) {
        return new Promise(function (resolve) {
            _openTenantPasswordModal();
            let busy = false;
            let settled = false;
            // Escape must mean the same thing as the cancel button, so the
            // dialog cannot be dismissed "for free" by a keystroke that leaves
            // the operator unsure whether the save ran.
            function onKey(ev) {
                if (ev && ev.key === 'Escape') finish({ cancelled: true });
            }
            function finish(result) {
                if (settled) return;
                settled = true;
                if (typeof document.removeEventListener === 'function') {
                    document.removeEventListener('keydown', onKey);
                }
                _closeTenantPasswordModal();
                resolve(result);
            }
            async function attempt() {
                if (busy || settled) return;
                const input = document.getElementById('tenant-password-input');
                const pw = (input && input.value) || '';
                if (!pw) {
                    _setTenantPasswordError(t('tenant_password_required'));
                    return;
                }
                busy = true;
                try {
                    await work(pw);
                    finish({ ok: true });
                } catch (e) {
                    if (e && e.message === 'stale-response') {
                        finish({ cancelled: true });
                        return;
                    }
                    if (e && e.status === 401) {
                        // Wrong password: keep the prompt open and the drafts.
                        _setTenantPasswordError(t('tenant_password_wrong'));
                        if (input) input.value = '';
                    } else {
                        finish({ ok: false, error: e });
                    }
                } finally {
                    busy = false;
                }
            }
            const confirm = document.getElementById('tenant-password-confirm');
            const cancel = document.getElementById('tenant-password-cancel');
            const input = document.getElementById('tenant-password-input');
            if (confirm) confirm.addEventListener('click', attempt);
            if (cancel) cancel.addEventListener('click', function () {
                finish({ cancelled: true });
            });
            if (input) input.addEventListener('keydown', function (ev) {
                if (ev && ev.key === 'Enter') attempt();
            });
            if (typeof document.addEventListener === 'function') {
                document.addEventListener('keydown', onKey);
            }
        });
    }

    function ensureTenantEditor() {
        let el = document.getElementById('tenant-editor');
        if (el) return el;
        el = document.createElement('div');
        el.id = 'tenant-editor';
        el.className = 'role-editor tenant-editor hidden';
        const tabHtml = TENANT_TABS.map(function (tab) {
            return '<button type="button" class="role-editor-tab tenant-editor-tab"' +
                ' id="tenant-editor-tab-' + tab + '" data-tab="' + tab + '">' +
                '<span data-tab-label="' + tab + '"></span></button>';
        }).join('');
        el.innerHTML =
            '<div class="role-editor-head">' +
            '<button type="button" class="role-editor-back" id="tenant-editor-back"></button>' +
            '<div class="role-editor-title-row">' +
            '<div class="min-w-0">' +
            '<h2 id="tenant-editor-title" class="text-xl font-bold text-slate-800 dark:text-slate-100 m-0"></h2>' +
            '<p id="tenant-editor-sub" class="text-xs text-slate-400 mt-1 truncate"></p>' +
            '</div>' +
            '<div class="flex items-center gap-2">' +
            '<span id="tenant-editor-active-badge" class="text-xs px-2 py-1 rounded-full"></span>' +
            '<span id="tenant-editor-version" class="text-xs text-slate-400"></span>' +
            '<span class="role-dirty-pill" id="tenant-dirty-pill"></span>' +
            '</div></div>' +
            '<nav class="role-editor-tabs" role="tablist">' + tabHtml + '</nav>' +
            '</div>' +
            '<div class="role-editor-body">' +
            // --- basic information ---
            '<div class="role-editor-panel tenant-editor-panel active" id="tenant-panel-basic">' +
            '<div class="role-editor-block-title" data-block-title="basic"></div>' +
            '<p class="text-sm text-slate-500 dark:text-slate-400 mb-4" data-block-hint="basic"></p>' +
            '<div class="grid grid-cols-1 md:grid-cols-2 gap-4">' +
            '<div class="agent-field"><label class="agent-field-label" id="tenant-label-code"></label>' +
            '<input type="text" id="tenant-fld-code" class="agent-input" value=""></div>' +
            '<div class="agent-field"><label class="agent-field-label" id="tenant-label-name"></label>' +
            '<input type="text" id="tenant-fld-name" class="agent-input" value=""></div>' +
            '</div>' +
            '<div class="agent-field"><label class="inline-flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300">' +
            '<input type="checkbox" id="tenant-fld-active">' +
            '<span id="tenant-label-active"></span></label></div>' +
            '</div>' +
            // --- model authorization ---
            '<div class="role-editor-panel tenant-editor-panel" id="tenant-panel-model">' +
            '<div class="role-editor-block-title" data-block-title="model"></div>' +
            '<p class="text-sm text-slate-500 dark:text-slate-400 mb-4" data-block-hint="model"></p>' +
            '<div id="tenant-grant-model" class="role-res-picker"></div>' +
            '</div>' +
            // --- tool authorization ---
            '<div class="role-editor-panel tenant-editor-panel" id="tenant-panel-tool">' +
            '<div class="role-editor-block-title" data-block-title="tool"></div>' +
            '<p class="text-sm text-slate-500 dark:text-slate-400 mb-4" data-block-hint="tool"></p>' +
            '<div id="tenant-grant-tool" class="role-res-picker"></div>' +
            '</div>' +
            // --- agent provisioning ---
            '<div class="role-editor-panel tenant-editor-panel" id="tenant-panel-agent">' +
            '<div class="role-editor-block-title" data-block-title="agent"></div>' +
            '<p class="text-sm text-slate-500 dark:text-slate-400 mb-3" data-block-hint="agent"></p>' +
            // Read-only "which agents this tenant has now". Not a form control:
            // it never dirties the tab and never preselects the picker.
            '<div id="tenant-agent-current" class="tenant-agent-current mb-6"></div>' +
            '<div class="role-editor-block-title" data-block-title="agent_copy"></div>' +
            '<p class="text-sm text-slate-500 dark:text-slate-400 mb-2" data-block-hint="agent_copy"></p>' +
            '<div id="tenant-agent-source-note" class="tenant-agent-source-note mb-2"></div>' +
            '<div id="tenant-agent-candidates" class="tenant-agent-candidates"></div>' +
            '<div id="tenant-agent-selected" class="tenant-agent-selected mt-2"></div>' +
            '<div id="tenant-agent-result" class="tenant-agent-result mt-3"></div>' +
            '</div>' +
            // --- tenant management (space + admin) ---
            '<div class="role-editor-panel tenant-editor-panel" id="tenant-panel-admin">' +
            '<div class="role-editor-block-title" data-block-title="space"></div>' +
            '<p class="text-sm text-slate-500 dark:text-slate-400 mb-3" data-block-hint="space"></p>' +
            '<div id="tenant-space-card" class="role-res-picker mb-6"></div>' +
            '<div class="role-editor-block-title" data-block-title="admin"></div>' +
            '<p class="text-sm text-slate-500 dark:text-slate-400 mb-3" data-block-hint="admin"></p>' +
            // Read-only "who administers this tenant now", rendered from a
            // dedicated read. It is not a form control: it never preselects the
            // picker nor dirties the tab.
            '<div id="tenant-current-admin" class="tenant-current-admin mb-3"></div>' +
            '<div class="tenant-admin-modes" role="group">' +
            '<button type="button" class="tenant-admin-mode active" id="tenant-admin-mode-existing" data-mode="existing"></button>' +
            '<button type="button" class="tenant-admin-mode" id="tenant-admin-mode-new" data-mode="new"></button>' +
            '</div>' +
            '<div id="tenant-admin-body-existing" class="tenant-admin-body">' +
            '<div id="tenant-admin-picker"></div>' +
            '<div class="grid grid-cols-1 md:grid-cols-2 gap-4 mt-4">' +
            '<div class="agent-field"><label class="agent-field-label" id="tenant-label-admin_display"></label>' +
            '<input type="text" id="tenant-fld-admin_display" class="agent-input" value=""></div>' +
            '</div>' +
            '</div>' +
            '<div id="tenant-admin-body-new" class="tenant-admin-body hidden">' +
            '<div class="grid grid-cols-1 md:grid-cols-2 gap-4">' +
            '<div class="agent-field"><label class="agent-field-label" id="tenant-label-admin_new_username"></label>' +
            '<input type="text" id="tenant-fld-admin_new_username" class="agent-input" value=""></div>' +
            '<div class="agent-field"><label class="agent-field-label" id="tenant-label-admin_new_display"></label>' +
            '<input type="text" id="tenant-fld-admin_new_display" class="agent-input" value=""></div>' +
            '<div class="agent-field"><label class="agent-field-label" id="tenant-label-admin_new_password"></label>' +
            '<input type="password" id="tenant-fld-admin_new_password" class="agent-input" value="">' +
            '<div class="agent-field-hint" id="tenant-hint-admin_new_password"></div></div>' +
            '</div>' +
            '</div>' +
            '</div>' +
            '</div>' +
            '<p id="tenant-editor-error" class="hidden px-7 text-xs text-red-500"></p>' +
            '<div class="role-editor-foot">' +
            '<div class="text-xs text-slate-400" id="tenant-editor-foot-hint"></div>' +
            '<div class="flex items-center gap-2">' +
            '<button type="button" class="px-4 py-2 rounded-lg text-sm font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-white/10" id="tenant-editor-cancel"></button>' +
            '<button type="button" class="px-5 py-2 rounded-lg bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium" id="tenant-editor-submit"></button>' +
            '</div></div>';

        const view = document.getElementById('view-tenant');
        if (view) {
            if (!view.style.position) view.style.position = 'relative';
            el.style.position = 'absolute';
            el.style.inset = '0';
            el.style.zIndex = '20';
            view.appendChild(el);
        } else {
            document.body.appendChild(el);
        }

        el.querySelectorAll('.tenant-editor-tab').forEach(function (btn) {
            btn.addEventListener('click', function () {
                if (btn.disabled) return;
                switchTenantTab(btn.getAttribute('data-tab'));
            });
        });
        document.getElementById('tenant-editor-back').addEventListener('click', function () { closeTenantEditor(); });
        document.getElementById('tenant-editor-cancel').addEventListener('click', function () { closeTenantEditor(); });
        document.getElementById('tenant-editor-submit').addEventListener('click', submitTenantEditor);
        document.getElementById('tenant-fld-name').addEventListener('input', function () { markTenantDirty('basic'); });
        document.getElementById('tenant-fld-active').addEventListener('change', function () { markTenantDirty('basic'); });
        document.getElementById('tenant-fld-code').addEventListener('input', function () { markTenantDirty('basic'); });
        document.getElementById('tenant-fld-admin_display').addEventListener('input', function () { markTenantDirty('admin'); });
        document.getElementById('tenant-admin-mode-existing').addEventListener('click', function () { setTenantAdminMode('existing'); });
        document.getElementById('tenant-admin-mode-new').addEventListener('click', function () { setTenantAdminMode('new'); });
        ['tenant-fld-admin_new_username', 'tenant-fld-admin_new_display',
         'tenant-fld-admin_new_password'].forEach(function (fid) {
            document.getElementById(fid).addEventListener('input', function () { markTenantDirty('admin'); });
        });
        _localizeTenantEditorChrome();
        return el;
    }

    function _localizeTenantEditorChrome() {
        const map = {
            basic: 'tenant_tab_basic', model: 'tenant_tab_model',
            tool: 'tenant_tab_tool', agent: 'tenant_tab_agent', admin: 'tenant_tab_admin',
        };
        Object.keys(map).forEach(function (tab) {
            const el = document.getElementById('tenant-editor') &&
                document.getElementById('tenant-editor')
                    .querySelector('[data-tab-label="' + tab + '"]');
            if (el) el.textContent = t(map[tab]);
        });
        const hints = {
            basic: 'tenant_tab_basic_hint', model: 'tenant_tab_model_hint',
            tool: 'tenant_tab_tool_hint', space: 'tenant_space_hint', admin: 'tenant_tab_admin_hint',
            agent: 'tenant_tab_agent_hint', agent_copy: 'tenant_agent_copy_hint',
        };
        Object.keys(hints).forEach(function (key) {
            const el = document.getElementById('tenant-editor') &&
                document.getElementById('tenant-editor')
                    .querySelector('[data-block-hint="' + key + '"]');
            if (el) el.textContent = t(hints[key]);
        });
        const titles = { basic: 'tenant_tab_basic', model: 'tenant_tab_model', tool: 'tenant_tab_tool',
            agent: 'tenant_tab_agent' };
        Object.keys(titles).forEach(function (key) {
            const el = document.getElementById('tenant-editor') &&
                document.getElementById('tenant-editor')
                    .querySelector('[data-block-title="' + key + '"]');
            if (el) el.textContent = t(titles[key]);
        });
        const agentCopyTitle = document.getElementById('tenant-editor') &&
            document.getElementById('tenant-editor').querySelector('[data-block-title="agent_copy"]');
        if (agentCopyTitle) agentCopyTitle.textContent = t('tenant_agent_copy_title');
        const spaceTitle = document.getElementById('tenant-editor') &&
            document.getElementById('tenant-editor').querySelector('[data-block-title="space"]');
        if (spaceTitle) spaceTitle.textContent = t('tenant_space_title');
        const adminTitle = document.getElementById('tenant-editor') &&
            document.getElementById('tenant-editor').querySelector('[data-block-title="admin"]');
        if (adminTitle) adminTitle.textContent = t('tenant_tab_admin');
        const labels = {
            'tenant-label-code': 'admin_field_code', 'tenant-label-name': 'admin_field_name',
            'tenant-label-active': 'admin_field_active',
            'tenant-label-admin_display': 'admin_field_admin_display',
            'tenant-label-admin_new_username': 'tenant_admin_new_username',
            'tenant-label-admin_new_display': 'tenant_admin_new_display',
            'tenant-label-admin_new_password': 'tenant_admin_new_password',
        };
        Object.keys(labels).forEach(function (id) {
            const el = document.getElementById(id);
            if (el) el.textContent = t(labels[id]);
        });
        const modes = {
            'tenant-admin-mode-existing': 'tenant_admin_mode_existing',
            'tenant-admin-mode-new': 'tenant_admin_mode_new',
        };
        Object.keys(modes).forEach(function (id) {
            const el = document.getElementById(id);
            if (el) el.textContent = t(modes[id]);
        });
        // Field-level hints reuse the account-creation wording so the rule is
        // stated once, in the same words as the member form.
        const fieldHints = { 'tenant-hint-admin_new_password': 'admin_field_password_hint' };
        Object.keys(fieldHints).forEach(function (id) {
            const el = document.getElementById(id);
            if (el) el.textContent = t(fieldHints[id]);
        });
        const cancel = document.getElementById('tenant-editor-cancel');
        if (cancel) cancel.textContent = t('cancel');
        const back = document.getElementById('tenant-editor-back');
        if (back) back.textContent = t('admin_back_to_list');
    }

    function _tenantEditorRefreshBadges() {
        const badge = document.getElementById('tenant-editor-active-badge');
        if (badge) {
            badge.textContent = fmtActive(_tenantEditor.active);
            badge.className = 'text-xs px-2 py-1 rounded-full ' + (_tenantEditor.active
                ? 'bg-primary-50 text-primary-600 dark:bg-primary-900/30 dark:text-primary-300'
                : 'bg-slate-100 text-slate-500 dark:bg-white/10 dark:text-slate-400');
        }
        const version = document.getElementById('tenant-editor-version');
        if (version) version.textContent = t('tenant_version_label') + ' ' + _tenantEditor.version;
    }

    function _renderTenantSpace() {
        const el = document.getElementById('tenant-space-card');
        if (!el) return;
        const space = _tenantEditor.space;
        if (!space) {
            el.innerHTML = '<div class="text-xs text-slate-400">' + escapeHtml(t('tenant_space_unavailable')) + '</div>';
            return;
        }
        function cell(label, value) {
            return '<div><div class="text-[10px] uppercase tracking-wide text-slate-400">' +
                escapeHtml(label) + '</div>' +
                '<div class="text-sm text-slate-700 dark:text-slate-200">' +
                escapeHtml(value) + '</div></div>';
        }
        el.innerHTML = '<div class="grid grid-cols-1 md:grid-cols-3 gap-3">' +
            cell(t('tenant_space_id'), space.id || _tenantEditor.code || '') +
            cell(t('tenant_space_status'), space.status === 'ready'
                ? t('tenant_space_ready') : t('tenant_space_not_ready')) +
            cell(t('tenant_space_isolation'), space.isolation || '') +
            '</div>';
    }

    // Read-only: who administers this tenant right now. This is deliberately
    // NOT the picker's selection. Preselecting would make a read look like a
    // pending write, so the operator could not tell "already administered"
    // from "about to change"; the tab must stay clean until they act.
    function _renderTenantCurrentAdmin() {
        const el = document.getElementById('tenant-current-admin');
        if (!el) return;
        const label = escapeHtml(t('tenant_current_admin_label'));
        if (_tenantEditor.adminLoadFailed) {
            // "Could not read" must never masquerade as "no admin", or the
            // operator would designate someone they did not need to.
            el.innerHTML = '<div class="text-xs text-amber-600 dark:text-amber-400">' +
                label + ': ' + escapeHtml(t('tenant_current_admin_unavailable')) + '</div>';
            return;
        }
        if (!_tenantEditor.adminLoaded) { el.innerHTML = ''; return; }
        const admin = (_tenantEditor.admins || [])[0];
        if (!admin) {
            el.innerHTML = '<div class="text-xs text-slate-400">' +
                label + ': ' + escapeHtml(t('tenant_current_admin_none')) + '</div>';
            return;
        }
        el.innerHTML = '<div class="tenant-current-admin-card">' +
            '<div class="text-[10px] uppercase tracking-wide text-slate-400">' + label + '</div>' +
            '<div class="text-sm text-slate-700 dark:text-slate-200">' +
            escapeHtml(admin.display_name || admin.username) + '</div>' +
            '<div class="text-[10px] text-slate-400">' + escapeHtml(admin.username) + '</div>' +
            '</div>';
    }

    // Best-effort read: a failure is surfaced (and stays distinct from "no
    // admin") but never blocks the operator from designating one.
    async function _loadTenantCurrentAdmin() {
        if (_tenantEditor.mode === 'create' || !_tenantEditor.id) {
            _tenantEditor.admins = [];
            _tenantEditor.adminLoaded = false;
            _tenantEditor.adminLoadFailed = false;
            _renderTenantCurrentAdmin();
            return;
        }
        _tenantEditor.adminLoaded = false;
        _tenantEditor.adminLoadFailed = false;
        try {
            const res = await apiFetch('/api/platform/tenants/' +
                encodeURIComponent(_tenantEditor.id) + '/admins');
            _tenantEditor.admins = (res && res.items) || [];
            _tenantEditor.adminLoaded = true;
        } catch (e) {
            if (e && e.message === 'stale-response') return;
            _tenantEditor.admins = [];
            _tenantEditor.adminLoadFailed = true;
        }
        _renderTenantCurrentAdmin();
    }

    // The admin tab has two ways to appoint an admin. Both bodies stay in the
    // DOM so switching modes never discards what the other mode already holds.
    function setTenantAdminMode(mode) {
        const want = mode === 'new' ? 'new' : 'existing';
        _tenantEditor.adminMode = want;
        [['existing', 'tenant-admin-mode-existing'], ['new', 'tenant-admin-mode-new']]
            .forEach(function (pair) {
                const btn = document.getElementById(pair[1]);
                if (btn) btn.classList.toggle('active', pair[0] === want);
            });
        const bodies = { existing: 'tenant-admin-body-existing', new: 'tenant-admin-body-new' };
        Object.keys(bodies).forEach(function (key) {
            const el = document.getElementById(bodies[key]);
            if (el) el.classList.toggle('hidden', key !== want);
        });
    }

    function _tenantAdminField(id) {
        const el = document.getElementById(id);
        return el ? String(el.value == null ? '' : el.value) : '';
    }

    // Clear the admin tab's controls so neither a half-filled create form nor a
    // previously picked account leaks from the tenant that was open before.
    function _resetTenantAdminFields() {
        ['tenant-fld-admin_display', 'tenant-fld-admin_new_username',
         'tenant-fld-admin_new_display', 'tenant-fld-admin_new_password']
            .forEach(function (id) {
                const el = document.getElementById(id);
                if (el) el.value = '';
            });
        _userPickerState[TENANT_ADMIN_PICKER_NAME] = '';
        // Clear the previous tenant's current-admin read so it cannot linger
        // while the new tenant's read is still in flight.
        _tenantEditor.admins = [];
        _tenantEditor.adminLoaded = false;
        _tenantEditor.adminLoadFailed = false;
        _renderTenantCurrentAdmin();
        setTenantAdminMode('existing');
    }

    function _tenantAdminBody(pw) {
        if (_tenantEditor.adminMode === 'new') {
            return {
                mode: 'new',
                username: _tenantAdminField('tenant-fld-admin_new_username').trim(),
                display_name: _tenantAdminField('tenant-fld-admin_new_display').trim(),
                temporary_password: _tenantAdminField('tenant-fld-admin_new_password'),
                recent_password: pw,
            };
        }
        return {
            mode: 'existing',
            user_id: _userPickerState[TENANT_ADMIN_PICKER_NAME] || '',
            display_name: _tenantAdminField('tenant-fld-admin_display'),
            recent_password: pw,
        };
    }

    // ---- Agent tab: read the tenant's agents, copy from the source ----------
    //
    // Two reads happen in one GET: the tenant's own bound agents (read-only) and
    // the copyable candidates of the resolved source tenant, each flagged with
    // whether it already has a clone here. The write copies the checked subset;
    // it is a cross-tenant write, so it needs the same recent password as the
    // other sensitive platform writes.
    function _tenantAgentsBase(id) {
        return '/api/platform/tenants/' + encodeURIComponent(id) + '/agents';
    }

    function tenantAgentSelection() {
        const host = document.getElementById('tenant-agent-candidates');
        if (!host) return [];
        // querySelectorAll yields a NodeList, which has forEach but no filter or
        // map. Reaching for them here threw inside both the checkbox handler and
        // the save handler, so Save failed before it could even report an error.
        return Array.from(host.querySelectorAll('input[type=checkbox]'))
            .filter(function (box) { return box.checked && !box.disabled; })
            .map(function (box) { return box.value; });
    }

    function _updateTenantAgentSelected() {
        const el = document.getElementById('tenant-agent-selected');
        if (!el) return;
        if (_tenantEditor.mode === 'create') { el.textContent = ''; return; }
        el.textContent = t('tenant_agent_selected_count')
            .replace('{n}', String(tenantAgentSelection().length));
    }

    function _resetTenantAgentFields() {
        // Clear the previous tenant's agent read (and any result) so it cannot
        // linger while the new tenant is still loading.
        ['tenant-agent-current', 'tenant-agent-candidates', 'tenant-agent-source-note',
         'tenant-agent-selected', 'tenant-agent-result'].forEach(function (id) {
            const el = document.getElementById(id);
            if (el) el.innerHTML = '';
        });
    }

    function _renderTenantAgentCurrent() {
        const el = document.getElementById('tenant-agent-current');
        if (!el) return;
        const label = '<div class="tenant-agent-block-label">' +
            escapeHtml(t('tenant_agent_current_title')) + '</div>';
        if (_tenantEditor.mode === 'create') {
            el.innerHTML = label + '<div class="text-xs text-slate-400">' +
                escapeHtml(t('tenant_agent_create_hint')) + '</div>';
            return;
        }
        if (_tenantEditor.agentLoadFailed) {
            // "Could not read" must never masquerade as "no agents", or the
            // operator cannot tell whether this tenant is really empty.
            el.innerHTML = label + '<div class="text-xs text-amber-600 dark:text-amber-400">' +
                escapeHtml(t('tenant_agent_current_unavailable')) + '</div>';
            return;
        }
        if (!_tenantEditor.agentLoaded) { el.innerHTML = label; return; }
        if (!_tenantEditor.agents.length) {
            el.innerHTML = label + '<div class="text-xs text-slate-400">' +
                escapeHtml(t('tenant_agent_current_empty')) + '</div>';
            return;
        }
        el.innerHTML = label + _tenantEditor.agents.map(function (agent) {
            return '<div class="tenant-agent-row">' +
                '<div class="tenant-agent-row-main">' +
                '<div class="text-sm text-slate-700 dark:text-slate-200">' +
                escapeHtml(agent.name || agent.id) + '</div>' +
                '<div class="tenant-agent-row-id">' + escapeHtml(agent.id) + '</div>' +
                '</div>' +
                '<div class="tenant-agent-row-badges">' +
                '<span class="tenant-agent-badge">' +
                escapeHtml(t(agent.enabled ? 'tenant_agent_enabled' : 'tenant_agent_disabled')) +
                '</span>' +
                (agent.is_default ? '<span class="tenant-agent-badge is-default">' +
                    escapeHtml(t('tenant_agent_default_badge')) + '</span>' : '') +
                '</div></div>';
        }).join('');
    }

    function _renderTenantAgentCandidates() {
        const host = document.getElementById('tenant-agent-candidates');
        const note = document.getElementById('tenant-agent-source-note');
        if (!host) return;
        if (_tenantEditor.mode === 'create') {
            host.innerHTML = '';
            if (note) note.innerHTML = '';
            return;
        }
        const source = _tenantEditor.copySource;
        if (note) {
            if (source) {
                note.innerHTML = escapeHtml(t('tenant_agent_source_label')) + ': ' +
                    escapeHtml(source.name || source.code || source.tenant_id);
            } else {
                note.innerHTML = escapeHtml(_tenantEditor.agentSourceError ||
                    t('tenant_agent_source_unavailable'));
            }
        }
        if (!source) {
            host.innerHTML = '<div class="text-xs text-slate-400">' +
                escapeHtml(t('tenant_agent_source_unavailable')) + '</div>';
            return;
        }
        const candidates = source.candidates || [];
        if (!candidates.length) {
            host.innerHTML = '<div class="text-xs text-slate-400">' +
                escapeHtml(t('tenant_agent_source_empty')) + '</div>';
            return;
        }
        host.innerHTML = candidates.map(function (c) {
            const badges =
                (c.is_default ? '<span class="tenant-agent-badge is-default">' +
                    escapeHtml(t('tenant_agent_default_badge')) + '</span>' : '') +
                (c.already_copied ? '<span class="tenant-agent-badge synced">' +
                    escapeHtml(t('tenant_agent_copied_badge')) + '</span>' : '') +
                (c.enabled ? '' : '<span class="tenant-agent-badge">' +
                    escapeHtml(t('tenant_agent_disabled')) + '</span>');
            return '<label class="tenant-agent-candidate-row' +
                (c.already_copied ? ' is-synced' : '') + '">' +
                '<input type="checkbox" class="tenant-agent-candidate" value="' +
                escapeHtml(c.source_agent_id) + '"' + (c.already_copied ? ' disabled' : '') + '>' +
                '<span class="tenant-agent-candidate-main">' +
                '<span class="text-sm text-slate-700 dark:text-slate-200">' +
                escapeHtml(c.name || c.source_agent_id) + '</span>' +
                '<span class="tenant-agent-row-id">' + escapeHtml(c.source_agent_id) + '</span>' +
                '</span>' +
                '<span class="tenant-agent-row-badges">' + badges + '</span>' +
                '</label>';
        }).join('');
        host.querySelectorAll('input[type=checkbox]').forEach(function (box) {
            if (box.disabled) return;
            // A re-render (after a copy) must not throw away a draft the operator
            // already picked, nor the selection they are retrying.
            if (_tenantEditor.agentPending[box.value]) box.checked = true;
            box.addEventListener('change', function () {
                if (box.checked) _tenantEditor.agentPending[box.value] = true;
                else delete _tenantEditor.agentPending[box.value];
                markTenantDirty('agent');
                _updateTenantAgentSelected();
            });
        });
    }

    function _renderTenantAgentResult() {
        const el = document.getElementById('tenant-agent-result');
        if (!el) return;
        const res = _tenantEditor.agentResult;
        if (!res) { el.innerHTML = ''; return; }
        const copied = res.copied_agent_ids || [];
        const skipped = res.skipped_agent_ids || [];
        const failed = res.failed || [];
        let html = '<div class="text-xs text-slate-600 dark:text-slate-300">' +
            escapeHtml(t('tenant_agent_result_copied')) + ' ' + copied.length +
            ' · ' + escapeHtml(t('tenant_agent_result_skipped')) + ' ' + skipped.length +
            '</div>';
        if (copied.length) {
            html += '<div class="tenant-agent-row-id">' +
                copied.map(function (item) { return escapeHtml(item.agent_id); }).join(', ') +
                '</div>';
        }
        if (res.default_agent_id) {
            html += '<div class="text-xs text-slate-500 dark:text-slate-400">' +
                escapeHtml(t('tenant_agent_result_default')) + ': ' +
                escapeHtml(res.default_agent_id) + '</div>';
        }
        if (failed.length) {
            html += '<div class="text-xs text-red-500">' +
                escapeHtml(t('tenant_agent_result_failed')) + ': ' +
                failed.map(function (item) {
                    return escapeHtml(item.source_agent_id) +
                        (item.error ? ' (' + escapeHtml(item.error) + ')' : '');
                }).join(', ') + '</div>';
        }
        el.innerHTML = html;
    }

    function _renderTenantAgents() {
        _renderTenantAgentCurrent();
        _renderTenantAgentCandidates();
        _updateTenantAgentSelected();
        _renderTenantAgentResult();
    }

    async function _loadTenantAgents() {
        if (_tenantEditor.mode === 'create' || !_tenantEditor.id) {
            _tenantEditor.agents = [];
            _tenantEditor.agentLoaded = false;
            _tenantEditor.agentLoadFailed = false;
            _tenantEditor.copySource = null;
            _tenantEditor.agentSourceError = '';
            _renderTenantAgents();
            return;
        }
        _tenantEditor.agentLoaded = false;
        _tenantEditor.agentLoadFailed = false;
        try {
            const res = await apiFetch(_tenantAgentsBase(_tenantEditor.id));
            _tenantEditor.agents = (res && res.agents) || [];
            _tenantEditor.copySource = (res && res.copy_source) || null;
            _tenantEditor.agentSourceError = (res && res.source_error) || '';
            _tenantEditor.agentLoaded = true;
        } catch (e) {
            if (e && e.message === 'stale-response') return;
            _tenantEditor.agents = [];
            _tenantEditor.copySource = null;
            _tenantEditor.agentLoadFailed = true;
        }
        _renderTenantAgents();
    }

    async function _ensureTenantAgentTab() {
        // A failed read is retried on re-entry; a successful one is not re-read
        // away, which would discard a draft the operator is still editing.
        if (_tenantEditor.tabLoaded.agent && !_tenantEditor.agentLoadFailed) return;
        _tenantEditor.tabLoaded.agent = true;
        await _loadTenantAgents();
    }

    async function _tenantSaveAgents(pw) {
        const selected = tenantAgentSelection();
        const res = await apiFetch(_tenantAgentsBase(_tenantEditor.id), {
            method: 'POST',
            body: { action: 'copy', source_agent_ids: selected, recent_password: pw },
            suppressAuthOverlay: true,
        });
        _tenantEditor.agentResult = res;
        // Only the agents that still owe a copy stay picked, so "try again" is one
        // click rather than a re-pick of everything.
        const pending = {};
        ((res && res.failed) || []).forEach(function (item) {
            pending[item.source_agent_id] = true;
        });
        _tenantEditor.agentPending = pending;
        await _loadTenantAgents();
        if (((res && res.failed) || []).length) {
            // The endpoint answers 200 with a per-agent failure list: some agents
            // may well have landed, so the batch must stop here rather than claim
            // overall success.
            const err = new Error('partial');
            err.code = 'partial';
            throw err;
        }
        return res;
    }

    function _tenantEditorApplyCreateMode() {
        const isCreate = _tenantEditor.mode === 'create';
        ['model', 'tool', 'agent', 'admin'].forEach(function (tab) {
            const btn = document.getElementById('tenant-editor-tab-' + tab);
            if (btn) btn.disabled = isCreate;
        });
        const hint = document.getElementById('tenant-editor-foot-hint');
        if (hint) {
            hint.textContent = isCreate ? t('tenant_editor_create_hint') : '';
        }
        const codeEl = document.getElementById('tenant-fld-code');
        if (codeEl) codeEl.readOnly = !isCreate;
    }

    async function _tenantEditorRender(tn, grants) {
        _tenantEditor.code = tn.code || '';
        _tenantEditor.name = tn.name || '';
        _tenantEditor.active = !!tn.active;
        _tenantEditor.version = tn.version || 0;
        _tenantEditor.space = tn.space || null;
        _tenantEditor.grants = grants || [];
        const codeEl = document.getElementById('tenant-fld-code');
        const nameEl = document.getElementById('tenant-fld-name');
        const activeEl = document.getElementById('tenant-fld-active');
        if (codeEl) codeEl.value = _tenantEditor.code;
        if (nameEl) nameEl.value = _tenantEditor.name;
        if (activeEl) activeEl.checked = _tenantEditor.active;
        document.getElementById('tenant-editor-sub').textContent = _tenantEditor.code;
        _tenantEditorRefreshBadges();
        _renderTenantSpace();
        _tenantEditorApplyCreateMode();
    }

    async function switchTenantTab(tab) {
        if (TENANT_TABS.indexOf(tab) === -1) tab = 'basic';
        // A tenant that does not exist yet cannot hold grants or an admin.
        if (_tenantEditor.mode === 'create' && tab !== 'basic') {
            _tenantEditorSetError(t('tenant_editor_create_hint'));
            tab = 'basic';
        }
        _tenantEditor.tab = tab;
        const root = document.getElementById('tenant-editor');
        if (root) {
            root.querySelectorAll('.tenant-editor-tab').forEach(function (btn) {
                btn.classList.toggle('active', btn.getAttribute('data-tab') === tab);
            });
            root.querySelectorAll('.tenant-editor-panel').forEach(function (panel) {
                panel.classList.toggle('active', panel.id === 'tenant-panel-' + tab);
            });
        }
        const submit = document.getElementById('tenant-editor-submit');
        if (submit) submit.textContent = t('admin_save');
        if (tab === 'model' || tab === 'tool') await _ensureTenantGrantTab(tab);
        else if (tab === 'agent') await _ensureTenantAgentTab();
        else if (tab === 'admin') await _ensureTenantAdminTab();
    }

    async function _ensureTenantGrantTab(kind) {
        const host = document.getElementById('tenant-grant-' + kind);
        if (!host) return;
        if (_tenantEditor.tabLoaded[kind]) return;
        _tenantEditor.tabLoaded[kind] = true;
        host.innerHTML = tenantGrantHtml({
            resourceKind: kind,
            value: _tenantEditor.grants,
            apiBase: _tenantCatalogBase(_tenantEditor.id),
            version: _tenantEditor.version,
            actions: _tenantGrantActions(kind),
        });
        await initTenantGrant(host, kind, function () { markTenantDirty(kind); });
    }

    async function _ensureTenantAdminTab() {
        _renderTenantSpace();
        if (_tenantEditor.mode === 'create') return;
        const host = document.getElementById('tenant-admin-picker');
        if (!host || _tenantEditor.tabLoaded.admin) return;
        _tenantEditor.tabLoaded.admin = true;
        host.innerHTML = _userPickerHtml('tenant-admin-picker-inner', { name: TENANT_ADMIN_PICKER_NAME });
        const node = document.getElementById('tenant-admin-picker-inner');
        if (node) {
            await _initUserPicker(node, {
                name: TENANT_ADMIN_PICKER_NAME,
                onSelect: function (picked) {
                    // Prefill from the chosen account so the operator edits a real
                    // value instead of retyping it; the field stays editable.
                    const nameEl = document.getElementById('tenant-fld-admin_display');
                    if (nameEl && picked && picked.displayName) nameEl.value = picked.displayName;
                    markTenantDirty('admin');
                },
            });
        }
        // Show who administers the tenant today, separately from the picker.
        await _loadTenantCurrentAdmin();
    }

    async function openTenantEditor(mode, id) {
        const el = ensureTenantEditor();
        _localizeTenantEditorChrome();
        _tenantEditor = _tenantEditorIdle();
        _tenantEditor.open = true;
        _tenantEditor.mode = mode;
        _tenantEditor.id = id || null;
        // A write disables the save button while it is in flight; opening an
        // editor always starts from a usable button so a form can never look
        // interactive while silently swallowing every click.
        const submitBtn = document.getElementById('tenant-editor-submit');
        if (submitBtn) submitBtn.disabled = false;
        _resetTenantAdminFields();
        _resetTenantAgentFields();
        _tenantEditorSetError('');
        const pill = document.getElementById('tenant-dirty-pill');
        if (pill) pill.classList.remove('show');
        // Show the shell before any fetch so the operator sees the page open.
        el.classList.remove('hidden');

        if (mode === 'create') {
            document.getElementById('tenant-editor-title').textContent = t('tenant_create');
            document.getElementById('tenant-editor-sub').textContent = '';
            await _tenantEditorRender({ code: '', name: '', active: true, version: 0 }, []);
            await switchTenantTab('basic');
            return;
        }

        document.getElementById('tenant-editor-title').textContent = t('tenant_edit_title');
        // The platform single read is the only source of the space projection;
        // the list row does not carry it.
        let tn = _tenantById[id] || {};
        try {
            const res = await apiFetch('/api/platform/tenants/' + encodeURIComponent(id));
            tn = (res && res.tenant) || tn;
        } catch (e) {
            if (e && e.message === 'stale-response') return;
        }
        if (tn && tn.id) _tenantById[id] = tn;
        let grants = [];
        try {
            const res = await apiFetch(_tenantResourcesBase(id));
            grants = (res && res.grants) || [];
        } catch (e) {
            // Best-effort: an unreadable grant set shows an empty picker rather
            // than blocking the basic tab.
            grants = [];
        }
        await _tenantEditorRender(tn, grants);
        await switchTenantTab('basic');
    }

    function closeTenantEditor() {
        if (tenantEditorIsDirty() && !confirmDiscard(true)) return;
        closeTenantEditorNoPrompt();
    }

    function closeTenantEditorNoPrompt() {
        const el = document.getElementById('tenant-editor');
        if (el) el.classList.add('hidden');
        _tenantEditor = _tenantEditorIdle();
    }

    // Reveal the unsaved-draft prompt without closing (navigation guard path).
    function discardTenantEditorDraft() {
        closeTenantEditorNoPrompt();
    }

    // The three write endpoints are separate transactions, so a batch cannot be
    // atomic. Steps therefore run in a fixed order and stop at the first
    // failure, keeping "what already committed" a prefix that is easy to reason
    // about and to retry.
    //
    // Tenant admin runs before basics on purpose: the profile write refuses to
    // enable a tenant with no valid tenant_admin, so binding the admin first is
    // what makes "appoint an admin and enable" succeed in a single save.
    function _tenantBatchSteps(dirty) {
        const steps = [];
        if (dirty.indexOf('admin') >= 0) {
            steps.push({
                key: 'admin', label: t('tenant_tab_admin'), tabs: ['admin'],
                // Remembered per step so a failure can be explained in terms of
                // the mode that produced it, even if the tab is switched after.
                adminMode: _tenantEditor.adminMode, run: _tenantSaveAdmin,
            });
        }
        const grantKinds = ['model', 'tool'].filter(function (k) { return dirty.indexOf(k) >= 0; });
        if (grantKinds.length) {
            steps.push({
                key: 'grants', label: t('tenant_tab_' + grantKinds[0]),
                tabs: grantKinds,
                run: function () { return _tenantSaveGrants(grantKinds); },
            });
        }
        if (dirty.indexOf('agent') >= 0) {
            // Copying takes no part in the tenant version chain: the endpoint
            // writes bindings and rosters, not the tenant row.
            steps.push({
                key: 'agent', label: t('tenant_tab_agent'), tabs: ['agent'],
                run: _tenantSaveAgents,
            });
        }
        if (dirty.indexOf('basic') >= 0) {
            steps.push({ key: 'basic', label: t('tenant_tab_basic'), tabs: ['basic'], run: _tenantSaveBasic });
        }
        return steps;
    }

    // Validate everything a step needs before any write (and before asking for
    // the password), so an incomplete form cannot trigger a partial save.
    function _tenantBatchValidationError(dirty) {
        if (dirty.indexOf('admin') >= 0) {
            if (_tenantEditor.adminMode === 'new') {
                if (!_tenantAdminField('tenant-fld-admin_new_username').trim()) {
                    return t('tenant_admin_new_username_required');
                }
                if (!_tenantAdminField('tenant-fld-admin_new_display').trim()) {
                    return t('tenant_admin_new_display_required');
                }
                if (!_tenantAdminField('tenant-fld-admin_new_password')) {
                    return t('tenant_admin_new_password_required');
                }
                return '';
            }
            const userId = _userPickerState[TENANT_ADMIN_PICKER_NAME] || '';
            if (!userId) return t('admin_user_picker_required');
        }
        if (dirty.indexOf('agent') >= 0 && !tenantAgentSelection().length) {
            // Never let an empty selection degrade into "copy everything".
            return t('tenant_agent_pick_required');
        }
        return '';
    }

    function _clearTenantStepDirty(tabs) {
        tabs.forEach(function (tab) { _tenantEditorClearDirty(tab); });
    }

    // A partial batch must still name the step that failed, and a version
    // conflict additionally tells the operator to reload before retrying.
    function _reportTenantBatchError(error) {
        const label = error && error.stepLabel;
        if (label) {
            // A copy that landed some agents and failed others comes back as a
            // 200 with a failure list; it must name the reason without pretending
            // the whole step succeeded.
            if (error.stepKey === 'agent' && error.code === 'partial') {
                _tenantEditorSetError(t('tenant_agent_partial_failed'));
                return;
            }
            // The create-an-account path has a small, known set of rejections.
            // Naming the reason is the difference between "try again" and
            // "lengthen the password", so map them before falling back.
            if (error.stepKey === 'admin' && error.stepAdminMode === 'new') {
                const reason = _tenantAdminNewReason(error);
                if (reason) {
                    _tenantEditorSetError(reason);
                    return;
                }
            }
            let msg = t('tenant_editor_save_failed_step') + ': ' + label;
            if (error.status === 409) msg += ' (' + t('tenant_editor_conflict') + ')';
            _tenantEditorSetError(msg);
            return;
        }
        _tenantEditorSetError((error && error.message) || t('load_error'));
    }

    // Server codes from create_tenant_admin_account, in the operator's terms.
    function _tenantAdminNewReason(error) {
        const code = error && error.code;
        if (code === 'weak_password') return t('tenant_admin_weak_password');
        if (code === 'invalid_username') return t('tenant_admin_invalid_username');
        if (code === 'conflict') return t('tenant_admin_username_taken');
        return '';
    }

    async function submitTenantEditor() {
        const btn = document.getElementById('tenant-editor-submit');
        // Just the set of tabs to save; the order they are written in is owned
        // by _tenantBatchSteps, not by this list.
        const dirty = ['admin', 'model', 'tool', 'agent', 'basic'].filter(function (tab) {
            return !!_tenantEditor.dirty[tab];
        });
        if (!dirty.length) return;
        _tenantEditorSetError('');

        const invalid = _tenantBatchValidationError(dirty);
        if (invalid) {
            _tenantEditorSetError(invalid);
            markTenantDirty(dirty.indexOf('admin') >= 0 ? 'admin' : dirty[0]);
            return;
        }

        const pending = _tenantBatchSteps(dirty);
        async function runPending(pw) {
            for (const step of pending) {
                if (step.done) continue;
                try {
                    await step.run(pw);
                } catch (e) {
                    // Remember where it stopped so the message can name the step
                    // and a retry can resume from here instead of re-running
                    // steps whose version has already moved on.
                    if (e) {
                        e.stepLabel = step.label;
                        e.stepKey = step.key;
                        e.stepAdminMode = step.adminMode;
                    }
                    throw e;
                }
                step.done = true;
                _clearTenantStepDirty(step.tabs);
            }
        }

        if (btn) btn.disabled = true;
        let failure = null;
        try {
            if (dirty.indexOf('basic') >= 0 || dirty.indexOf('admin') >= 0 ||
                dirty.indexOf('agent') >= 0) {
                // Only the endpoints that actually verify the password ask for
                // it; a grants-only save must not prompt for a secret the
                // server would ignore.
                const outcome = await withTenantPassword(runPending);
                if (outcome.cancelled) return;
                if (!outcome.ok) failure = outcome.error;
            } else {
                await runPending('');
            }
        } catch (e) {
            if (e && e.message === 'stale-response') return;
            failure = e;
        } finally {
            if (btn) btn.disabled = false;
        }
        if (failure) {
            _reportTenantBatchError(failure);
            return;
        }
        status(document.getElementById('tenant-status'), t('admin_saved'), true);
    }

    async function _tenantSaveBasic(pw) {
        const code = (document.getElementById('tenant-fld-code') || {}).value || '';
        const name = (document.getElementById('tenant-fld-name') || {}).value || '';
        const active = !!(document.getElementById('tenant-fld-active') || {}).checked;
        if (_tenantEditor.mode === 'create') {
            const res = await apiFetch('/api/platform/tenants', {
                method: 'POST',
                body: { code: code, name: name, recent_password: pw },
                suppressAuthOverlay: true,
            });
            const tn = (res && res.tenant) || {};
            if (tn && tn.id) _tenantById[tn.id] = tn;
            await loadTenantView();
            // Stay on the page, now editing the tenant just created, so the
            // operator can continue with the authorization tabs.
            await openTenantEditor('edit', tn.id);
            const hint = document.getElementById('tenant-editor-foot-hint');
            if (hint) hint.textContent = t('tenant_editor_created_hint');
            return;
        }
        const res = await apiFetch('/api/platform/tenants/' + encodeURIComponent(_tenantEditor.id), {
            method: 'POST',
            body: {
                operation: 'profile',
                name: name,
                active: active,
                expected_version: _tenantEditor.version,
                recent_password: pw,
            },
            suppressAuthOverlay: true,
        });
        const tn = (res && res.tenant) || {};
        _tenantEditor.name = tn.name || name;
        _tenantEditor.active = (tn.active != null) ? !!tn.active : active;
        _tenantEditor.version = tn.version || (_tenantEditor.version + 1);
        if (_tenantById[_tenantEditor.id]) {
            Object.assign(_tenantById[_tenantEditor.id], {
                name: _tenantEditor.name, active: _tenantEditor.active,
                version: _tenantEditor.version,
            });
        }
        _tenantEditorRefreshBadges();
        await loadTenantView();
    }

    // The grant PUT replaces the whole set of a kind, so every kind travels in
    // one request: the dirty kinds carry the operator's current selection and
    // the untouched kinds are re-sent from the loaded grants so they are not
    // silently dropped.
    function _tenantGrantsForSave(kinds) {
        const known = ['model', 'tool'];
        const out = [];
        known.forEach(function (kind) {
            if (kinds.indexOf(kind) >= 0) {
                out.push.apply(out, collectTenantGrants(kind));
            } else {
                out.push.apply(out, (_tenantEditor.grants || []).filter(function (g) {
                    return g.resource_kind === kind;
                }));
            }
        });
        // Future resource kinds are passed through untouched.
        out.push.apply(out, (_tenantEditor.grants || []).filter(function (g) {
            return known.indexOf(g.resource_kind) < 0;
        }));
        return out;
    }

    async function _tenantSaveGrants(kinds) {
        const res = await apiFetch(_tenantResourcesBase(_tenantEditor.id), {
            method: 'PUT',
            body: { grants: _tenantGrantsForSave(kinds), expected_version: _tenantEditor.version },
        });
        _tenantEditor.grants = (res && res.grants) || _tenantEditor.grants;
        // The PUT bumps the tenant version by exactly one.
        _tenantEditor.version = _tenantEditor.version + 1;
        _tenantEditorRefreshBadges();
        await loadTenantView();
    }

    async function _tenantSaveAdmin(pw) {
        const body = _tenantAdminBody(pw);
        const res = await apiFetch('/api/platform/tenants/' + encodeURIComponent(_tenantEditor.id) + '/admins', {
            method: 'POST',
            body: body,
            suppressAuthOverlay: true,
        });
        if (body.mode === 'new') {
            // The account only exists after this write, so the candidate list the
            // picker loaded earlier cannot contain it. Re-query instead of
            // making the operator reload the page to find what they just made.
            await _refreshUserPicker(TENANT_ADMIN_PICKER_NAME);
        }
        // The read-only block reports committed state, so it must reflect the
        // admin this save just installed, not the one that was there before.
        await _loadTenantCurrentAdmin();
        return res;
    }

    // ---- Members (Users) view --------------------------------------------
    async function loadMembersView() {
        const list = document.getElementById('member-list');
        const btn = document.getElementById('member-create-btn');
        if (!list) return;
        // Refresh filter dropdowns (roles + depts) once.
        await refreshMemberFilters();
        list.innerHTML = '<div class="text-sm text-slate-400 dark:text-slate-500">' + t('tenant_loading') + '</div>';
        const f = _memberFilters;
        f.q = (document.getElementById('member-search') || {}).value || '';
        f.status = (document.getElementById('member-status-filter') || {}).value || '';
        f.role = (document.getElementById('member-role-filter') || {}).value || '';
        f.department_id = (document.getElementById('member-dept-filter') || {}).value || '';
        const query = qs({
            q: f.q, status: f.status, role: f.role,
            department_id: f.department_id,
            page: _memberPage, page_size: _memberPageSize,
        });
        try {
            const data = await apiFetch('/api/tenant/members?' + query);
            if (!data.items || !data.items.length) {
                list.innerHTML = '<div class="text-sm text-slate-400">' + t('member_empty') + '</div>';
                btn.classList.add('hidden');
                renderPagination(document.getElementById('member-pagination'), _memberPage, _memberPageSize, data.total || 0, function (p) { _memberPage = p; loadMembersView(); });
                return;
            }
            btn.classList.remove('hidden');
            _memberById = {};
            const rolesByCode = {};
            _roles.forEach(function (r) { rolesByCode[r.code] = r; });
            const platformAdmin = await isPlatformAdmin();
            const rows = data.items.map(function (m) {
                _memberById[m.id] = m;
                const roleNames = (m.role_codes || []).map(function (c) {
                    return (rolesByCode[c] && rolesByCode[c].name) || c;
                }).join(', ');
                return '<div class="flex items-center justify-between px-4 py-3 rounded-lg border border-slate-200 dark:border-white/10">'
                    + '<div class="flex items-center gap-3"><div class="w-9 h-9 rounded-lg overflow-hidden shrink-0 bg-slate-100 dark:bg-white/10">'
                    + userAvatarHTML({ id: m.user_id, avatar: m.avatar }) + '</div>'
                    + '<div><div class="text-sm font-medium text-slate-800 dark:text-slate-100">' + escapeHtml(m.display_name) + '</div>'
                    + '<div class="text-xs text-slate-400">' + escapeHtml(m.username) + ' · ' + fmtActive(m.active) + (roleNames ? ' · ' + escapeHtml(roleNames) : '') + '</div></div></div>'
                    + '<div class="flex items-center gap-2"><div class="text-xs text-slate-400">' + escapeHtml(m.position_text || '') + '</div>'
                    + '<button class="admin-row-btn" onclick="adminRowAction(\'member\',\'edit\',\'' + escapeHtml(m.id) + '\')"><i class="fas fa-pen mr-1"></i>' + escapeHtml(t('admin_edit')) + '</button>'
                    + '<button class="admin-row-btn" onclick="openExternalIdentities(\'member\',\'' + escapeHtml(m.id) + '\')"><i class="fas fa-link mr-1"></i>' + escapeHtml(t('extid_title')) + '</button>'
                    + '<button class="admin-row-btn" onclick="adminRowAction(\'member\',\'tenants\',\'' + escapeHtml(m.id) + '\')"><i class="fas fa-building mr-1"></i>' + escapeHtml(t('member_tenants')) + '</button>'
                    + (platformAdmin && m.user_id
                        ? '<button class="admin-row-btn" onclick="adminRowAction(\'member\',\'reset\',\'' + escapeHtml(m.id) + '\')"><i class="fas fa-key mr-1"></i>' + escapeHtml(t('admin_reset')) + '</button>'
                        : '')
                    + '</div></div>';
            }).join('');
            list.innerHTML = rows;
            renderPagination(document.getElementById('member-pagination'), _memberPage, _memberPageSize, data.total || 0, function (p) { _memberPage = p; loadMembersView(); });
        } catch (err) {
            if (err.message === 'stale-response') return;
            list.innerHTML = '<div class="text-sm text-red-500">' + t('load_error') + ': ' + escapeHtml(err.message) + '</div>';
            if (err.status === 403) status(document.getElementById('member-status'), err.message, false);
        }
    }

    async function refreshMemberFilters() {
        const roles = await fetchRoles();
        const depts = await fetchDepts();
        const roleSel = document.getElementById('member-role-filter');
        if (roleSel) {
            const cur = roleSel.value;
            const all = '<option value="" data-i18n="filter_all">' + escapeHtml(t('filter_all')) + '</option>';
            roleSel.innerHTML = all + roles.map(function (r) {
                return '<option value="' + escapeHtml(r.code) + '"' + (r.code === cur ? ' selected' : '') + '>' + escapeHtml(r.name) + '</option>';
            }).join('');
        }
        const deptSel = document.getElementById('member-dept-filter');
        if (deptSel) {
            const cur = deptSel.value;
            const all = '<option value="" data-i18n="filter_all">' + escapeHtml(t('filter_all')) + '</option>';
            deptSel.innerHTML = all + depts.filter(function (d) { return d.code !== '__root__'; }).map(function (d) {
                return '<option value="' + escapeHtml(d.id) + '"' + (d.id === cur ? ' selected' : '') + '>' + escapeHtml(d.name) + '</option>';
            }).join('');
        }
    }

    async function openMemberCreate() {
        const roles = await fetchRoles();
        const depts = await fetchDepts();
        const tenants = await administeredTenants();
        const currentTenant = sessionStorage.getItem('cow_tenant_id');
        openAdminModal({
            title: t('member_create'),
            icon: 'fa-plus',
            fields: [
                // Section 0: 所属租户
                { name: 'tenants', label: t('admin_field_tenants'), type: 'multi', options: tenantOptions(tenants), value: [currentTenant], required: true, hint: t('admin_field_tenants_hint'), sectionTitle: t('member_section_tenants'), sectionIcon: 'building' },
                // Section 1: 账号信息
                { name: 'username', label: t('admin_field_username'), type: 'text', required: true, hint: t('admin_field_username_hint'), inline: true, icon: 'user', sectionTitle: t('member_section_account'), sectionIcon: 'address-card' },
                { name: 'display_name', label: t('admin_field_display_name'), type: 'text', required: true, inline: true, icon: 'id-card' },
                { name: 'temporary_password', label: t('admin_field_temp_password'), type: 'password', required: true, hint: t('admin_field_password_hint'), icon: 'lock' },
                { name: 'department_id', label: t('admin_field_department'), type: 'select', options: memberDeptOptions(depts), hint: t('admin_field_department_hint'), icon: 'building' },
                { name: 'position_text', label: t('admin_field_position'), type: 'text', icon: 'briefcase' },
                // Section 2: 角色与状态
                { name: 'roles', label: t('admin_field_roles'), type: 'multi', options: roleOptions(roles), value: ['member'], hint: t('admin_field_roles_hint'), sectionTitle: t('member_section_role'), sectionIcon: 'user-shield' },
            ],
            submitLabel: t('admin_create'),
            statusEl: document.getElementById('member-status'),
            submit: async function (body) {
                const tenantIds = body.tenants || [];
                for (let i = 0; i < tenantIds.length; i++) {
                    const tid = tenantIds[i];
                    const op = (i === 0) ? 'create-new' : 'bind-existing';
                    await apiFetch('/api/tenant/members', {
                        method: 'POST',
                        tenantId: tid,
                        body: {
                            operation: op,
                            username: body.username,
                            display_name: body.display_name,
                            temporary_password: op === 'create-new' ? (body.temporary_password || '') : '',
                            roles: body.roles,
                            department_id: body.department_id || null,
                            position_text: body.position_text,
                        },
                    });
                }
                await loadMembersView();
            },
            onConflictReload: function () { loadMembersView(); },
        });
    }

    async function openMemberEdit(id) {
        const m = _memberById[id];
        if (!m) return;
        const roles = await fetchRoles();
        const depts = await fetchDepts();
        openAdminModal({
            title: t('member_edit_title'),
            subtitle: m.username || '',
            icon: 'fa-pen',
            fields: [
                // Section 1: 账号信息
                { name: 'username', label: t('admin_field_username'), type: 'locked', value: m.username, sectionTitle: t('member_section_account'), sectionIcon: 'address-card' },
                { name: 'display_name', label: t('admin_field_display_name'), type: 'text', value: m.display_name, required: true, icon: 'id-card' },
                { name: 'department_id', label: t('admin_field_department'), type: 'select', options: memberDeptOptions(depts), value: m.department_id || '', icon: 'building' },
                { name: 'position_text', label: t('admin_field_position'), type: 'text', value: m.position_text || '', icon: 'briefcase' },
                // Section 2: 角色与状态
                { name: 'active', label: t('admin_field_active'), type: 'checkbox', value: !!m.active, sectionTitle: t('member_section_role'), sectionIcon: 'user-shield' },
                { name: 'roles', label: t('admin_field_roles'), type: 'multi', options: roleOptions(roles), value: m.role_codes || [], hint: t('admin_field_roles_edit_hint') },
            ],
            submitLabel: t('admin_save'),
            statusEl: document.getElementById('member-status'),
            submit: async function (body) {
                body.expected_version = m.version;
                body.department_id = body.department_id || null;
                // Task 3.2: only submit `roles` when the selection actually
                // differs from the current role_codes; omitting preserves.
                const current = (m.role_codes || []).slice().sort().join(',');
                const next = (body.roles || []).slice().sort().join(',');
                if (current === next) delete body.roles;
                await apiFetch('/api/tenant/members/' + encodeURIComponent(id), { method: 'POST', body: body });
                await loadMembersView();
            },
            onConflictReload: function () { loadMembersView(); },
        });
    }

    // Adjust which (administered) tenants a member belongs to — the tenant side
    // of member editing, decoupled from the per-tenant profile edit. Adding a
    // tenant binds the existing account there (default member role); removing
    // one deactivates that membership (continuity is enforced server-side).
    async function openMemberTenants(id) {
        const m = _memberById[id];
        if (!m) return;
        const tenants = await administeredTenants(m.user_id);
        const current = tenants.filter(function (t) { return t.member; }).map(function (t) { return t.id; });
        openAdminModal({
            title: t('member_tenants_title'),
            subtitle: m.username || '',
            icon: 'fa-building',
            fields: [
                { name: 'tenants', label: t('admin_field_tenants'), type: 'multi', options: tenantOptions(tenants), value: current, required: true, hint: t('admin_field_tenants_edit_hint') },
            ],
            submitLabel: t('admin_save'),
            statusEl: document.getElementById('member-status'),
            submit: async function (body) {
                const selected = new Set(body.tenants || []);
                for (let i = 0; i < tenants.length; i++) {
                    const t = tenants[i];
                    if (!t.member && selected.has(t.id)) {
                        await apiFetch('/api/tenant/members', {
                            method: 'POST',
                            tenantId: t.id,
                            body: {
                                operation: 'bind-existing',
                                username: m.username,
                                display_name: m.display_name,
                                temporary_password: '',
                                roles: ['member'],
                                department_id: null,
                                position_text: '',
                            },
                        });
                    }
                }
                for (let i = 0; i < tenants.length; i++) {
                    const t = tenants[i];
                    if (t.member && !selected.has(t.id)) {
                        await apiFetch('/api/tenant/members/' + encodeURIComponent(t.member_id), {
                            method: 'POST',
                            tenantId: t.id,
                            body: {
                                display_name: m.display_name,
                                active: false,
                                department_id: null,
                                position_text: '',
                                expected_version: t.member_version,
                            },
                        });
                    }
                }
                await loadMembersView();
            },
            onConflictReload: function () { loadMembersView(); },
        });
    }

    async function resetMemberPassword(id) {
        const m = _memberById[id];
        if (!m) return;
        if (!(await isPlatformAdmin())) return;
        if (!window.confirm(t('admin_reset_confirm').replace('{name}', m.username || m.display_name))) return;
        openAdminModal({
            title: t('admin_reset'),
            subtitle: m.username || '',
            icon: 'fa-key',
            fields: [
                { name: 'recent_password', label: t('admin_field_recent_password'), type: 'password', required: true, hint: t('admin_field_recent_password_hint') },
            ],
            submitLabel: t('admin_reset_do'),
            statusEl: document.getElementById('member-status'),
            submit: async function (body) {
                body.expected_version = m.version;
                const r = await apiFetch('/api/platform/users/' + encodeURIComponent(m.user_id) + '/password', { method: 'POST', body: body });
                // Show the one-time temp password once, then clear on close.
                const td = document.getElementById('admin-modal-body');
                const d = document.createElement('div');
                d.className = 'text-sm text-emerald-600 dark:text-emerald-400 mt-3 font-semibold break-all';
                d.textContent = t('admin_reset_temp_result') + ': ' + (r.temporary_password || '');
                td.appendChild(d);
                await loadMembersView();
            },
            onConflictReload: function () { loadMembersView(); },
        });
    }

    // ---- Platform users (task 5.3) ----------------------------------------
    async function loadPlatformUsersView() {
        const list = document.getElementById('platform-user-list');
        if (!list) return;
        if (!(await isPlatformAdmin())) {
            list.innerHTML = '<div class="text-sm text-red-500">' + escapeHtml(t('admin_forbidden')) + '</div>';
            return;
        }
        list.innerHTML = '<div class="text-sm text-slate-400 dark:text-slate-500">' + t('tenant_loading') + '</div>';
        const f = _platformUserFilters;
        f.q = (document.getElementById('platform-user-search') || {}).value || '';
        f.status = (document.getElementById('platform-user-status') || {}).value || '';
        const query = qs({ q: f.q, status: f.status, page: _platformUserPage, page_size: _platformUserPageSize });
        try {
            const data = await apiFetch('/api/platform/users?' + query);
            if (!data.items || !data.items.length) {
                list.innerHTML = '<div class="text-sm text-slate-400">' + t('platform_user_empty') + '</div>';
                return;
            }
            _platformUsers = {};
            const rows = data.items.map(function (u) {
                _platformUsers[u.id] = u;
                const badges = [];
                if (u.is_platform_admin) badges.push('<span class="px-1.5 py-0.5 rounded bg-amber-50 dark:bg-amber-900/20 text-amber-600 dark:text-amber-300 text-[10px]">' + escapeHtml(t('platform_admin_badge')) + '</span>');
                if (u.must_change_password) badges.push('<span class="px-1.5 py-0.5 rounded bg-red-50 dark:bg-red-900/20 text-red-600 dark:text-red-300 text-[10px]">' + escapeHtml(t('filter_restricted')) + '</span>');
                return '<div class="flex items-center justify-between px-4 py-3 rounded-lg border border-slate-200 dark:border-white/10">'
                    + '<div class="flex items-center gap-3"><div class="w-9 h-9 rounded-lg overflow-hidden shrink-0 bg-slate-100 dark:bg-white/10">'
                    + userAvatarHTML({ id: u.id, avatar: u.avatar }) + '</div>'
                    + '<div><div class="text-sm font-medium text-slate-800 dark:text-slate-100">' + escapeHtml(u.display_name) + '</div>'
                    + '<div class="text-xs text-slate-400">' + escapeHtml(u.username) + ' · ' + fmtActive(u.active) + ' ' + badges.join(' ') + '</div></div></div>'
                    + '<div class="flex items-center gap-2">'
                    + '<button class="admin-row-btn" onclick="adminRowAction(\'platform_user\',\'reset\',\'' + escapeHtml(u.id) + '\')"><i class="fas fa-key mr-1"></i>' + escapeHtml(t('admin_reset')) + '</button>'
                    + '<button class="admin-row-btn" onclick="openExternalIdentities(\'platform_user\',\'' + escapeHtml(u.id) + '\')"><i class="fas fa-link mr-1"></i>' + escapeHtml(t('extid_title')) + '</button>'
                    + '<button class="admin-row-btn" onclick="adminRowAction(\'platform_user\',\'edit\',\'' + escapeHtml(u.id) + '\')"><i class="fas fa-pen mr-1"></i>' + escapeHtml(t('admin_edit')) + '</button>'
                    + '</div></div>';
            }).join('');
            list.innerHTML = rows;
            renderPagination(document.getElementById('platform-user-pagination'), _platformUserPage, _platformUserPageSize, data.total || 0, function (p) { _platformUserPage = p; loadPlatformUsersView(); });
        } catch (err) {
            if (err.message === 'stale-response') return;
            list.innerHTML = '<div class="text-sm text-red-500">' + t('load_error') + ': ' + escapeHtml(err.message) + '</div>';
        }
    }
    let _platformUsers = {};

    function openPlatformUserEdit(id) {
        const u = _platformUsers[id];
        if (!u) return;
        openAdminModal({
            title: t('platform_user_edit_title'),
            subtitle: u.username || '',
            icon: 'fa-pen',
            fields: [
                { name: 'username', label: t('admin_field_username'), type: 'locked', value: u.username },
                { name: 'active', label: t('admin_field_active'), type: 'checkbox', value: !!u.active },
                { name: 'is_platform_admin', label: t('admin_field_platform_admin'), type: 'checkbox', value: !!u.is_platform_admin },
                { name: 'recent_password', label: t('admin_field_recent_password'), type: 'password', required: true, hint: t('admin_field_recent_password_hint') },
            ],
            submitLabel: t('admin_save'),
            statusEl: document.getElementById('platform-user-status-msg'),
            submit: async function (body) {
                body.expected_version = u.version;
                await apiFetch('/api/platform/users/' + encodeURIComponent(id), { method: 'PATCH', body: body });
                await loadPlatformUsersView();
            },
            onConflictReload: function () { loadPlatformUsersView(); },
        });
    }

    function resetPlatformUser(id) {
        const u = _platformUsers[id];
        if (!u) return;
        if (!window.confirm(t('admin_reset_confirm').replace('{name}', u.username || u.display_name))) return;
        openAdminModal({
            title: t('admin_reset'),
            subtitle: u.username || '',
            icon: 'fa-key',
            fields: [
                { name: 'recent_password', label: t('admin_field_recent_password'), type: 'password', required: true, hint: t('admin_field_recent_password_hint') },
            ],
            submitLabel: t('admin_reset_do'),
            statusEl: document.getElementById('platform-user-status-msg'),
            submit: async function (body) {
                body.expected_version = u.version;
                const r = await apiFetch('/api/platform/users/' + encodeURIComponent(id) + '/password', { method: 'POST', body: body });
                const td = document.getElementById('admin-modal-body');
                const d = document.createElement('div');
                d.className = 'text-sm text-emerald-600 dark:text-emerald-400 mt-3 font-semibold break-all';
                d.textContent = t('admin_reset_temp_result') + ': ' + (r.temporary_password || '');
                td.appendChild(d);
                await loadPlatformUsersView();
            },
            onConflictReload: function () { loadPlatformUsersView(); },
        });
    }

    // ---- External identity bindings ---------------------------------------
    //
    // Both administrators maintain IM bindings, so this is *one* dialog opened
    // from two places: a platform account row (platform accounts view) and a
    // member row (members view). The dialog always answers the same two
    // questions — "who is this" (the header names the account) and "which
    // channel / which open_id" (one line per binding) — because an
    // administrator who cannot see whose account it is cannot tell whether the
    // binding is right.
    //
    // Which HTTP surface it uses follows the *entry point*, not the viewer:
    //  * a member row is a tenant-scoped thing, so it uses the tenant routes
    //    (the server confines a tenant admin to their own memberships);
    //  * a platform account row uses the platform routes.
    // A platform admin who is also a tenant admin therefore gets a working
    // path from either view.

    let _extId = null;

    const EXTID_KNOWN_PROVIDERS = ['feishu', 'wecom_bot', 'weixin', 'dingtalk', 'wechatcom_app'];

    function externalIdentityProviderLabel(code) {
        const raw = String(code || '').trim();
        if (!raw) return t('extid_provider_unknown');
        const key = 'extid_provider_' + raw.replace(/[^a-z0-9_]/gi, '_');
        const label = t(key);
        return label === key ? raw : label;
    }

    function externalIdentitySurface(kind, platformAdmin) {
        // Pure decision: which HTTP surface may this viewer use for this row?
        //
        // A platform account row is platform-domain by construction, so it
        // needs the platform role. A *member* row is tenant-scoped, and a tenant
        // admin must go through the tenant surface so the server can confine
        // them to their own memberships. But a platform admin may be looking at
        // a member row of a tenant whose admin role they do not hold — the
        // tenant surface would refuse them — so they use the platform surface,
        // which can reach any account. Both administrators therefore have a
        // working path from the same dialog.
        if (kind === 'platform_user') return 'platform';
        return platformAdmin ? 'platform' : 'tenant';
    }

    function externalIdentityRoutes(st) {
        // Pure so the "which administrator hits which surface" rule is testable
        // without a DOM: this is the part that must never silently flip.
        if (st.platform) {
            return {
                base: '/api/platform/users/' + encodeURIComponent(st.userId) + '/external-identities',
                attempts: '/api/platform/external-identity-attempts',
            };
        }
        return {
            base: '/api/tenant/members/' + encodeURIComponent(st.memberId) + '/external-identities',
            attempts: '/api/tenant/external-identity-attempts',
        };
    }

    function renderExternalIdentityBindingRow(st, b) {
        return '<div class="flex items-center justify-between gap-3 px-3 py-2 rounded-lg border border-slate-200 dark:border-white/10">'
            + '<div class="min-w-0">'
            + '<div class="text-sm text-slate-800 dark:text-slate-100 truncate"><i class="fas fa-comment-dots mr-1 text-indigo-400"></i>'
            + escapeHtml(externalIdentityProviderLabel(b.provider))
            + '<span class="text-slate-400"> · </span>' + escapeHtml(b.issuer || '—') + '</div>'
            + '<div class="text-xs text-slate-400 break-all">open_id: ' + escapeHtml(b.subject) + '</div>'
            + '</div>'
            + '<button class="admin-row-btn" onclick="extidDelete(\'' + escapeHtml(b.id) + '\')">'
            + '<i class="fas fa-unlink mr-1"></i>' + escapeHtml(t('admin_delete')) + '</button>'
            + '</div>';
    }

    function renderExternalIdentityAttemptRow(index, a) {
        // A pending attempt is the only place an administrator can learn an
        // open_id without reading the server log, so the row is a button that
        // fills the form rather than a passive hint.
        //
        // An open_id identifies nobody, though: the row leads with the sender's
        // name and a preview of what they actually said, which is what lets an
        // administrator recognise the person. Both are best-effort, so a channel
        // that cannot resolve a name still shows the message, and vice versa.
        const groupTag = a.is_group
            ? '<span class="extid_group_tag ml-1 px-1.5 py-0.5 rounded text-[10px] bg-amber-100 text-amber-700 dark:bg-amber-500/20 dark:text-amber-300">'
                + escapeHtml(t('extid_group_tag')) + '</span>'
            : '';
        const name = String(a.sender_name || '').trim()
            || ('<span class="text-slate-400">' + escapeHtml(t('extid_unnamed_sender')) + '</span>');
        const source = escapeHtml(externalIdentityProviderLabel(a.provider))
            + (a.issuer ? '<span class="text-slate-400"> · </span>' + escapeHtml(a.issuer) : '');
        const preview = String(a.message_preview || '').trim();
        const previewLine = preview
            ? '<div class="text-xs text-slate-600 dark:text-slate-300 mt-1 line-clamp-2">'
                + escapeHtml(preview) + '</div>'
            : '';
        const tries = Number(a.attempts || 0) > 1
            ? '<span class="text-[10px] text-slate-400 shrink-0">×' + Number(a.attempts) + '</span>'
            : '';
        return '<button type="button" class="w-full text-left px-3 py-2 rounded-lg border border-amber-200 dark:border-amber-500/30 hover:bg-amber-50 dark:hover:bg-amber-900/10"'
            + ' onclick="extidUseAttempt(' + index + ')">'
            + '<div class="flex items-center gap-2 text-sm text-slate-800 dark:text-slate-100">'
            + '<i class="fas fa-clock text-amber-500 shrink-0"></i>'
            + '<span class="truncate">' + name + '</span>'
            + groupTag
            + '<span class="ml-auto">' + tries + '</span></div>'
            + '<div class="text-xs text-slate-400 truncate mt-0.5">' + source + '</div>'
            + previewLine
            + '<div class="text-[10px] text-slate-400 break-all">open_id: ' + escapeHtml(a.subject) + '</div>'
            + '</button>';
    }

    function externalIdentitiesModalHtml(st) {
        const bindings = (st.bindings || []).map(function (b) {
            return renderExternalIdentityBindingRow(st, b);
        }).join('') || '<div class="text-xs text-slate-400">' + escapeHtml(t('extid_no_bindings')) + '</div>';
        const attempts = (st.attempts || []).map(function (a, i) {
            return renderExternalIdentityAttemptRow(i, a);
        }).join('') || '<div class="text-xs text-slate-400">' + escapeHtml(t('extid_no_attempts')) + '</div>';
        const providerOptions = EXTID_KNOWN_PROVIDERS.map(function (code) {
            const selected = (st.form && st.form.provider) === code ? ' selected' : '';
            return '<option value="' + escapeHtml(code) + '"' + selected + '>'
                + escapeHtml(externalIdentityProviderLabel(code)) + '</option>';
        }).join('');
        return '<div class="space-y-4">'
            + (st.error ? '<div class="text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-900/20 rounded-lg px-3 py-2 break-all">' + escapeHtml(st.error) + '</div>' : '')
            + (st.status ? '<div class="text-sm text-emerald-600 dark:text-emerald-400 bg-emerald-50 dark:bg-emerald-900/20 rounded-lg px-3 py-2">' + escapeHtml(st.status) + '</div>' : '')
            + '<div><div class="text-xs font-semibold text-slate-500 dark:text-slate-400 mb-2">' + escapeHtml(t('extid_bound_title')) + '</div>'
            + '<div class="space-y-2">' + bindings + '</div></div>'
            + '<div class="border-t border-slate-200 dark:border-white/10 pt-3">'
            + '<div class="text-xs font-semibold text-slate-500 dark:text-slate-400 mb-2">' + escapeHtml(t('extid_add_title')) + '</div>'
            + '<div class="grid grid-cols-1 sm:grid-cols-3 gap-2">'
            + '<select id="extid-provider" class="admin-input">' + providerOptions + '</select>'
            + '<input id="extid-issuer" class="admin-input" placeholder="' + escapeHtml(t('extid_field_issuer')) + '" value="' + escapeHtml((st.form && st.form.issuer) || '') + '">'
            + '<input id="extid-subject" class="admin-input" placeholder="' + escapeHtml(t('extid_field_subject')) + '" value="' + escapeHtml((st.form && st.form.subject) || '') + '">'
            + '</div>'
            + '<div class="text-xs text-slate-400 mt-1">' + escapeHtml(t('extid_field_hint')) + '</div>'
            + '<div class="flex justify-end mt-3"><button type="button" class="admin-row-btn" onclick="extidSubmit()"'
            + (st.busy ? ' disabled' : '') + '><i class="fas fa-link mr-1"></i>' + escapeHtml(t('extid_bind')) + '</button></div>'
            + '</div>'
            + '<div class="border-t border-slate-200 dark:border-white/10 pt-3">'
            + '<div class="text-xs font-semibold text-slate-500 dark:text-slate-400 mb-1">' + escapeHtml(t('extid_attempts_title')) + '</div>'
            + '<div class="text-xs text-slate-400 mb-2">' + escapeHtml(t('extid_attempts_hint')) + '</div>'
            + '<div class="space-y-2">' + attempts + '</div>'
            + '</div>'
            + '</div>';
    }

    function externalIdentityAccountLabel(st) {
        // "Who" is the first question the dialog must answer, so the label is a
        // pure function of the account rather than a DOM detail. The username is
        // only appended when it actually adds something: an account with no
        // display name would otherwise read "acmemember · acmemember".
        const name = st.name || st.username || '';
        if (!st.username || st.username === name) return name;
        return name ? name + ' · ' + st.username : st.username;
    }

    function ensureExtIdModal() {
        let el = document.getElementById('extid-modal');
        if (el) return el;
        el = document.createElement('div');
        el.id = 'extid-modal';
        el.className = 'hidden fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4';
        el.innerHTML = '<div class="bg-white dark:bg-slate-800 rounded-xl shadow-xl w-full max-w-2xl max-h-[85vh] overflow-y-auto">'
            + '<div class="flex items-start justify-between px-5 py-4 border-b border-slate-200 dark:border-white/10">'
            + '<div><div id="extid-title" class="text-base font-semibold text-slate-800 dark:text-slate-100"></div>'
            + '<div id="extid-subtitle" class="text-xs text-slate-400"></div></div>'
            + '<button type="button" class="admin-row-btn" onclick="extidClose()"><i class="fas fa-times"></i></button>'
            + '</div>'
            + '<div id="extid-body" class="px-5 py-4"></div>'
            + '</div>';
        document.body.appendChild(el);
        return el;
    }

    function renderExtIdModal() {
        if (!_extId) return;
        const el = ensureExtIdModal();
        document.getElementById('extid-title').textContent = t('extid_title');
        // "Who" comes first: a binding is meaningless without its account.
        document.getElementById('extid-subtitle').textContent = externalIdentityAccountLabel(_extId);
        document.getElementById('extid-body').innerHTML = externalIdentitiesModalHtml(_extId);
        el.classList.remove('hidden');
    }

    async function extidLoadAttempts() {
        try {
            const routes = externalIdentityRoutes(_extId);
            const data = await apiFetch(routes.attempts);
            _extId.attempts = data.items || [];
        } catch (err) {
            // The picker is a convenience; the form still works without it.
            _extId.attempts = [];
        }
    }

    async function extidRefresh() {
        const routes = externalIdentityRoutes(_extId);
        const data = await apiFetch(routes.base);
        _extId.bindings = data.items || [];
        await extidLoadAttempts();
        renderExtIdModal();
    }

    async function openExternalIdentities(kind, id) {
        // Look the account up from the list the row was rendered from, then hand
        // the *account* and the permitted surface to extidOpen. Splitting the
        // two keeps the dialog independent of how the caller found the row.
        const account = kind === 'platform_user' ? (_platformUsers[id] || {}) : (_memberById[id] || {});
        const surface = externalIdentitySurface(kind, await isPlatformAdmin());
        await extidOpen(kind, account, { platform: surface === 'platform' });
    }

    async function extidOpen(kind, account, opts) {
        const platform = opts && 'platform' in opts
            ? !!opts.platform
            : kind === 'platform_user';
        // The account's identifiers come from *which list the row came from*,
        // not from which surface we may use: a member row carries a membership
        // id and a separate user id, while a platform account row's id already
        // is the user id. Conflating the two sends a membership id to the
        // platform route, which addresses the wrong account entirely.
        const fromPlatformList = kind === 'platform_user';
        const userId = fromPlatformList ? account.id : account.user_id;
        const memberId = fromPlatformList ? '' : account.id;
        if (!userId) return;
        _extId = {
            kind: kind,
            platform: platform,
            memberId: memberId,
            userId: userId,
            name: account.display_name || '',
            username: account.username || '',
            bindings: [], attempts: [], error: '', status: '', busy: false,
            form: { provider: EXTID_KNOWN_PROVIDERS[0], issuer: '', subject: '' },
        };
        renderExtIdModal();
        try {
            await extidRefresh();
        } catch (err) {
            _extId.error = err && err.message ? err.message : String(err);
            renderExtIdModal();
        }
    }

    function extidCollectForm() {
        const provider = document.getElementById('extid-provider');
        const issuer = document.getElementById('extid-issuer');
        const subject = document.getElementById('extid-subject');
        return {
            provider: provider ? provider.value : EXTID_KNOWN_PROVIDERS[0],
            issuer: issuer ? issuer.value : '',
            subject: subject ? subject.value : '',
        };
    }

    function extidUseAttempt(index) {
        if (!_extId) return;
        const attempt = (_extId.attempts || [])[index];
        if (!attempt) return;
        _extId.form = {
            provider: attempt.provider || EXTID_KNOWN_PROVIDERS[0],
            issuer: attempt.issuer || '',
            subject: attempt.subject || '',
        };
        _extId.error = '';
        renderExtIdModal();
    }

    async function extidSubmit() {
        if (!_extId) return;
        const form = extidCollectForm();
        _extId.form = form;
        if (!form.subject) {
            _extId.error = t('extid_error_subject_required');
            _extId.status = '';
            renderExtIdModal();
            return;
        }
        _extId.busy = true;
        _extId.error = '';
        _extId.status = '';
        renderExtIdModal();
        try {
            const routes = externalIdentityRoutes(_extId);
            await apiFetch(routes.base, { method: 'POST', body: form });
            _extId.busy = false;
            _extId.status = t('extid_bound_ok');
            _extId.form = { provider: form.provider, issuer: form.issuer, subject: '' };
            await extidRefresh();
        } catch (err) {
            _extId.busy = false;
            _extId.error = extidErrorMessage(err);
            renderExtIdModal();
        }
    }

    async function extidDelete(bindingId) {
        if (!_extId || !bindingId) return;
        try {
            const routes = externalIdentityRoutes(_extId);
            await apiFetch(routes.base + '/' + encodeURIComponent(bindingId), { method: 'DELETE' });
            _extId.status = t('extid_unbound_ok');
            await extidRefresh();
        } catch (err) {
            _extId.error = extidErrorMessage(err);
            renderExtIdModal();
        }
    }

    function extidErrorMessage(err) {
        const code = (err && err.code) || '';
        if (code === 'conflict') return t('extid_error_conflict');
        if (code === 'forbidden') return t('extid_error_forbidden');
        if (code === 'not_found') return t('extid_error_not_found');
        if (code === 'bad_request') return t('extid_error_bad_request');
        return (err && err.message) ? err.message : t('extid_error_generic');
    }

    function extidClose() {
        const el = document.getElementById('extid-modal');
        if (el) el.classList.add('hidden');
        _extId = null;
    }

    // ---- Roles view -------------------------------------------------------
    async function loadRolesView() {
        const list = document.getElementById('role-list');
        const btn = document.getElementById('role-create-btn');
        if (!list) return;
        list.innerHTML = '<div class="text-sm text-slate-400 dark:text-slate-500">' + t('tenant_loading') + '</div>';
        try {
            const data = await apiFetch(_roleApiBase());
            if (!data.items || !data.items.length) {
                list.innerHTML = '<div class="text-sm text-slate-400">' + t('role_empty') + '</div>';
                btn.classList.add('hidden');
                return;
            }
            btn.classList.remove('hidden');
            _roleById = {};
            // Reuse the already-fetched permission catalog (if any) to render
            // grouped labels; do NOT force a catalog fetch just to list roles.
            const perms = _permCatalog;
            const rows = data.items.map(function (r) {
                _roleById[r.id] = r;
                const builtin = !!r.builtin;
                const permSet = r.permissions || [];
                // Group the role's permissions by the catalog group (task 5.5).
                const grouped = renderPermGrouped(permSet, perms);
                const memberCount = countMembersForRole(r.code);
                return '<div class="px-4 py-3 rounded-lg border border-slate-200 dark:border-white/10">'
                    + '<div class="flex items-center justify-between"><div class="text-sm font-medium text-slate-800 dark:text-slate-100">'
                    + escapeHtml(r.name) + ' <span class="text-xs text-slate-400">(' + escapeHtml(r.code) + ')</span>'
                    + (builtin ? ' <span class="px-1.5 py-0.5 rounded bg-slate-100 dark:bg-white/10 text-[10px] text-slate-500">' + escapeHtml(t('role_builtin')) + '</span>' : '')
                    + (memberCount != null ? ' <span class="text-xs text-slate-400">' + escapeHtml(t('role_members_label')) + ' ' + memberCount + '</span>' : '')
                    + '</div>'
                    + '<div class="flex items-center gap-2">'
                    + '<button class="admin-row-btn" onclick="adminRowAction(\'role\',\'members\',\'' + escapeHtml(r.code) + '\')"><i class="fas fa-users mr-1"></i>' + escapeHtml(t('role_view_members')) + '</button>'
                    + '<button class="admin-row-btn" onclick="adminRowAction(\'role\',\'copy\',\'' + escapeHtml(r.id) + '\')"><i class="fas fa-copy mr-1"></i>' + escapeHtml(t('role_copy')) + '</button>'
                    + '<button class="admin-row-btn" onclick="adminRowAction(\'role\',\'edit\',\'' + escapeHtml(r.id) + '\')"><i class="fas fa-pen mr-1"></i>' + escapeHtml(t('admin_edit')) + '</button>'
                    + (builtin ? '' : '<button class="admin-row-btn danger" onclick="adminRowAction(\'role\',\'delete\',\'' + escapeHtml(r.id) + '\')"><i class="fas fa-trash mr-1"></i>' + escapeHtml(t('admin_delete')) + '</button>')
                    + '</div></div>'
                    + '<div class="mt-1 text-xs text-slate-400">' + t('role_permissions_label') + ': ' + grouped + '</div>'
                    + '</div>';
            }).join('');
            list.innerHTML = rows;
        } catch (err) {
            if (err.message === 'stale-response') return;
            list.innerHTML = '<div class="text-sm text-red-500">' + t('load_error') + ': ' + escapeHtml(err.message) + '</div>';
        }
    }

    function renderPermGrouped(permSet, catalog) {
        if (!permSet || !permSet.length) return '-';
        const idToMeta = {};
        (catalog || []).forEach(function (p) { idToMeta[p.id] = p; });
        const labels = permSet.map(function (pid) {
            const meta = idToMeta[pid];
            return meta ? (meta.group ? '[' + meta.group + '] ' : '') + meta.label : pid;
        });
        return labels.join(', ');
    }

    function countMembersForRole(code) {
        // Count members holding this role using the member list (capped).
        // A block count would be a second request; keep a light in-page count
        // only for roles we have already loaded member data for. Return null
        // when unavailable so the row omits it.
        return null;
    }

    // ---- Role page editor (create / edit / copy; replaces admin-modal for roles) --
    let _roleEditor = {
        open: false, dirty: false, mode: 'create', role: null,
        submit: null, onConflictReload: null, statusEl: null,
        successMsg: '', activeTab: 'basic', perms: null,
    };

    function markRoleEditorDirty() {
        _roleEditor.dirty = true;
        const pill = document.getElementById('role-dirty-pill');
        if (pill) pill.classList.add('show');
    }

    function _roleEditorTabLabel(tab) {
        if (tab === 'basic') return t('role_tab_basic') || '基本信息';
        if (tab === 'model') return t('admin_resource_kind_model') || '模型';
        return _resourceKindLabel(tab);
    }

    function ensureRoleEditor() {
        let el = document.getElementById('role-editor');
        if (el) return el;
        el = document.createElement('div');
        el.id = 'role-editor';
        el.className = 'role-editor hidden';
        const tabs = ['basic', 'menu', 'skill', 'tool', 'agent', 'model'];
        const tabHtml = tabs.map(function (tab) {
            const badgeId = tab === 'basic' ? 'role-badge-perms' : ('role-badge-' + tab);
            return '<button type="button" class="role-editor-tab" data-tab="' + tab + '">' +
                '<span class="role-editor-tab-label" data-tab-label="' + tab + '"></span>' +
                '<span class="role-editor-badge" id="' + badgeId + '">0</span></button>';
        }).join('');
        const kindPanels = ['menu', 'skill', 'tool', 'agent'].map(function (kind) {
            return '<div class="role-editor-panel" id="role-panel-' + kind + '">' +
                '<p class="text-sm text-slate-500 dark:text-slate-400 mb-4 role-panel-hint" data-kind-hint="' + kind + '"></p>' +
                '<div class="role-res-picker" id="role-res-manage-' + kind + '">' +
                '<div class="flex flex-wrap gap-2 items-center mb-3">' +
                '<input type="text" class="agent-input flex-1 min-w-[180px] role-res-search" data-kind="' + kind + '" placeholder="">' +
                '<button type="button" class="admin-row-btn role-res-selectall" data-kind="' + kind + '"></button>' +
                '<button type="button" class="admin-row-btn role-res-clear" data-kind="' + kind + '"></button>' +
                '</div>' +
                '<div class="role-res-list" id="role-res-list-' + kind + '"></div>' +
                '<div class="flex items-center justify-between pt-2 text-xs text-slate-400">' +
                '<span class="role-res-total" data-kind="' + kind + '"></span>' +
                '<div class="role-res-pagination" data-kind="' + kind + '"></div>' +
                '</div></div></div>';
        }).join('');
        el.innerHTML =
            '<div class="role-editor-head">' +
            '<button type="button" class="role-editor-back" id="role-editor-back"></button>' +
            '<div class="role-editor-title-row">' +
            '<div class="min-w-0">' +
            '<h2 id="role-editor-title" class="text-xl font-bold text-slate-800 dark:text-slate-100 m-0"></h2>' +
            '<p id="role-editor-sub" class="text-xs text-slate-400 mt-1 truncate"></p>' +
            '</div>' +
            '<span class="role-dirty-pill" id="role-dirty-pill"></span>' +
            '</div>' +
            '<nav class="role-editor-tabs" role="tablist">' + tabHtml + '</nav>' +
            '</div>' +
            '<div class="role-editor-body">' +
            '<div class="role-editor-panel active" id="role-panel-basic">' +
            '<div class="role-editor-block-title" id="role-block-basic-title"></div>' +
            '<p class="text-sm text-slate-500 dark:text-slate-400 mb-4" id="role-basic-hint"></p>' +
            '<div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-2">' +
            '<div class="agent-field"><label class="agent-field-label" id="role-label-code"></label>' +
            '<div id="role-code-wrap"></div></div>' +
            '<div class="agent-field"><label class="agent-field-label" id="role-label-name"></label>' +
            '<input type="text" id="adm-fld-name" class="agent-input" value=""></div>' +
            '</div>' +
            '<div class="role-editor-block-title" id="role-block-perms-title"></div>' +
            '<p class="text-sm text-slate-500 dark:text-slate-400 mb-3" id="role-perms-hint"></p>' +
            '<div class="flex flex-wrap gap-2 items-center mb-3">' +
            '<input type="text" id="role-perm-search" class="agent-input flex-1 min-w-[180px]" placeholder="">' +
            '<button type="button" class="admin-row-btn" id="role-perm-clear-all"></button>' +
            '</div>' +
            '<div id="adm-fld-permissions"></div>' +
            '</div>' +
            kindPanels +
            '<div class="role-editor-panel" id="role-panel-model">' +
            '<div class="role-editor-block-title" id="role-block-model-assign-title"></div>' +
            '<p class="text-sm text-slate-500 dark:text-slate-400 mb-4" id="role-model-assign-hint"></p>' +
            '<div class="role-res-picker mb-4" id="role-res-manage-model">' +
            '<div class="flex flex-wrap gap-2 items-center mb-3">' +
            '<input type="text" class="agent-input flex-1 min-w-[180px] role-res-search" data-kind="model" placeholder="">' +
            '<button type="button" class="admin-row-btn role-res-selectall" data-kind="model"></button>' +
            '<button type="button" class="admin-row-btn role-res-clear" data-kind="model"></button>' +
            '</div>' +
            '<div class="role-res-list" id="role-res-list-model"></div>' +
            '<div class="flex items-center justify-between pt-2 text-xs text-slate-400">' +
            '<span class="role-res-total" data-kind="model"></span>' +
            '<div class="role-res-pagination" data-kind="model"></div>' +
            '</div></div>' +
            '<div class="role-editor-block-title" id="role-block-model-defaults-title"></div>' +
            '<p class="text-sm text-slate-500 dark:text-slate-400 mb-3" id="role-model-defaults-hint"></p>' +
            '<div id="adm-fld-modeldefaults" class="role-res-picker space-y-2"></div>' +
            '</div>' +
            '</div>' +
            '<p id="role-editor-error" class="hidden px-7 text-xs text-red-500"></p>' +
            '<div class="role-editor-foot">' +
            '<div class="text-xs text-slate-400" id="role-editor-foot-hint"></div>' +
            '<div class="flex items-center gap-2">' +
            '<button type="button" class="px-4 py-2 rounded-lg text-sm font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-white/10" id="role-editor-cancel"></button>' +
            '<button type="button" class="px-5 py-2 rounded-lg bg-primary-500 hover:bg-primary-600 text-white text-sm font-medium" id="role-editor-submit"></button>' +
            '</div></div>';

        const view = document.getElementById('view-roles');
        if (view) {
            if (!view.style.position) view.style.position = 'relative';
            el.style.position = 'absolute';
            el.style.inset = '0';
            el.style.zIndex = '20';
            view.appendChild(el);
        } else {
            document.body.appendChild(el);
        }

        el.querySelectorAll('.role-editor-tab').forEach(function (btn) {
            btn.addEventListener('click', function () {
                switchRoleEditorTab(btn.getAttribute('data-tab'));
            });
        });
        document.getElementById('role-editor-back').addEventListener('click', function () { closeRoleEditor(); });
        document.getElementById('role-editor-cancel').addEventListener('click', function () { closeRoleEditor(); });
        document.getElementById('role-editor-submit').addEventListener('click', submitRoleEditor);
        document.getElementById('adm-fld-name').addEventListener('input', markRoleEditorDirty);
        document.getElementById('role-perm-search').addEventListener('input', function () {
            renderRolePermissions(_roleEditor.perms || [], _collectRolePermissionIds());
        });
        document.getElementById('role-perm-clear-all').addEventListener('click', function () {
            renderRolePermissions(_roleEditor.perms || [], []);
            markRoleEditorDirty();
            updateRoleEditorBadges();
        });
        el.querySelectorAll('.role-res-search').forEach(function (inp) {
            inp.addEventListener('input', function () {
                const kind = inp.getAttribute('data-kind');
                const st = _resourceState[kind];
                if (!st) return;
                st.q = inp.value || '';
                st.page = 1;
                _renderRoleKindList(kind);
            });
        });
        el.querySelectorAll('.role-res-selectall').forEach(function (btn) {
            btn.addEventListener('click', function () {
                const kind = btn.getAttribute('data-kind');
                const list = document.getElementById('role-res-list-' + kind);
                if (!list) return;
                const st = _resourceState[kind];
                list.querySelectorAll('input[type=checkbox]').forEach(function (cb) {
                    st.selected.add(cb.value);
                    cb.checked = true;
                    if (cb.parentElement) cb.parentElement.classList.add('checked');
                });
                markRoleEditorDirty();
                updateRoleEditorBadges();
                if (kind === 'model') _refreshRoleModelDefaults();
            });
        });
        el.querySelectorAll('.role-res-clear').forEach(function (btn) {
            btn.addEventListener('click', function () {
                const kind = btn.getAttribute('data-kind');
                const st = _resourceState[kind];
                if (st) st.selected.clear();
                _renderRoleKindList(kind);
                markRoleEditorDirty();
                updateRoleEditorBadges();
                if (kind === 'model') _refreshRoleModelDefaults();
            });
        });
        return el;
    }

    function _localizeRoleEditorChrome() {
        const tabs = ['basic', 'menu', 'skill', 'tool', 'agent', 'model'];
        tabs.forEach(function (tab) {
            const label = document.querySelector('.role-editor-tab-label[data-tab-label="' + tab + '"]');
            if (label) label.textContent = _roleEditorTabLabel(tab);
        });
        const setText = function (id, value) {
            const el = document.getElementById(id);
            if (el) el.textContent = value;
        };
        setText('role-editor-back', '← ' + (t('role_editor_back') || t('roles_title') || '返回角色列表'));
        setText('role-dirty-pill', t('role_dirty_pill') || '有未保存更改');
        setText('role-block-basic-title', t('role_section_basic') || '基本信息');
        setText('role-basic-hint', t('role_basic_hint') || '填写角色标识；下方配置功能权限。');
        setText('role-label-code', t('admin_field_code'));
        setText('role-label-name', t('admin_field_name') + ' *');
        setText('role-block-perms-title', t('admin_field_permissions'));
        setText('role-perms-hint', t('admin_field_permissions_hint'));
        setText('role-perm-clear-all', t('admin_resource_clear'));
        const permSearch = document.getElementById('role-perm-search');
        if (permSearch) permSearch.placeholder = t('role_perm_search_placeholder') || '搜索权限…';
        setText('role-block-model-assign-title', t('role_section_model_assign') || '可分配模型');
        setText('role-model-assign-hint', t('role_model_assign_hint') || '勾选该角色可使用的模型；默认模型只能从已选项中选择。');
        setText('role-block-model-defaults-title', t('admin_field_model_defaults') || '默认模型');
        setText('role-model-defaults-hint', t('admin_field_model_defaults_hint') || '');
        setText('role-editor-foot-hint', t('role_editor_foot_hint') || '切换 Tab 不丢草稿 · 离开前若有改动会确认');
        setText('role-editor-cancel', t('cancel'));
        ['menu', 'skill', 'tool', 'agent', 'model'].forEach(function (kind) {
            const search = document.querySelector('.role-res-search[data-kind="' + kind + '"]');
            if (search) search.placeholder = t('admin_resource_search_placeholder');
            const allBtn = document.querySelector('.role-res-selectall[data-kind="' + kind + '"]');
            if (allBtn) allBtn.textContent = t('admin_resource_selectall');
            const clearBtn = document.querySelector('.role-res-clear[data-kind="' + kind + '"]');
            if (clearBtn) clearBtn.textContent = t('admin_resource_clear');
            const hint = document.querySelector('.role-panel-hint[data-kind-hint="' + kind + '"]');
            if (hint) hint.textContent = (t('admin_field_resource_grants_hint') || '');
        });
    }

    function switchRoleEditorTab(tab) {
        _roleEditor.activeTab = tab || 'basic';
        document.querySelectorAll('.role-editor-tab').forEach(function (btn) {
            btn.classList.toggle('active', btn.getAttribute('data-tab') === _roleEditor.activeTab);
        });
        document.querySelectorAll('.role-editor-panel').forEach(function (panel) {
            panel.classList.toggle('active', panel.id === 'role-panel-' + _roleEditor.activeTab);
        });
        if (_roleEditor.activeTab !== 'basic') {
            _renderRoleKindList(_roleEditor.activeTab === 'model' ? 'model' : _roleEditor.activeTab);
        }
        if (_roleEditor.activeTab === 'model') _refreshRoleModelDefaults();
    }

    function updateRoleEditorBadges() {
        const permEl = document.getElementById('role-badge-perms');
        if (permEl) permEl.textContent = String(_collectRolePermissionIds().length);
        ['menu', 'skill', 'tool', 'agent', 'model'].forEach(function (kind) {
            const el = document.getElementById('role-badge-' + kind);
            if (el) el.textContent = String(_resCount(kind));
        });
    }

    function _collectRolePermissionIds() {
        const root = document.getElementById('adm-fld-permissions');
        if (!root) return [];
        return Array.prototype.slice.call(root.querySelectorAll('input:checked')).map(function (i) { return i.value; });
    }

    function renderRolePermissions(catalog, selected) {
        const root = document.getElementById('adm-fld-permissions');
        if (!root) return;
        const selectedSet = {};
        (selected || []).forEach(function (id) { selectedSet[id] = true; });
        const q = ((document.getElementById('role-perm-search') || {}).value || '').trim().toLowerCase();
        const groups = permGroups(catalog);
        const keys = Object.keys(groups);
        // Stable-ish order: nonempty groups first alphabetically, then blank.
        keys.sort(function (a, b) {
            if (!a) return 1;
            if (!b) return -1;
            return a.localeCompare(b);
        });
        let html = '';
        keys.forEach(function (g) {
            const items = (groups[g] || []).filter(function (p) {
                if (!q) return true;
                const hay = ((p.group || '') + ' ' + (p.label || '') + ' ' + (p.id || '')).toLowerCase();
                return hay.indexOf(q) !== -1;
            });
            if (!items.length) return;
            const selectedCount = items.filter(function (p) { return selectedSet[p.id]; }).length;
            const allOn = selectedCount === items.length;
            const title = g || (t('admin_field_permissions') || '权限');
            html += '<div class="role-perm-group" data-group="' + escapeHtml(g) + '">' +
                '<div class="role-perm-group-head">' +
                '<div class="text-sm font-semibold text-slate-700 dark:text-slate-200">' + escapeHtml(title) +
                ' <span class="text-xs font-normal text-slate-400">' + selectedCount + ' / ' + items.length + '</span></div>' +
                '<button type="button" class="admin-row-btn role-perm-group-toggle" data-group="' + escapeHtml(g) + '" data-all="' + (allOn ? '1' : '0') + '">' +
                (allOn ? (t('role_perm_clear_group') || '清空本组') : (t('role_perm_select_group') || '全选本组')) +
                '</button></div><div class="role-perm-group-body">';
            items.forEach(function (p) {
                const checked = !!selectedSet[p.id];
                html += '<label class="role-perm-item' + (checked ? ' checked' : '') + '">' +
                    '<input type="checkbox" value="' + escapeHtml(p.id) + '"' + (checked ? ' checked' : '') + '>' +
                    escapeHtml(p.label || p.id) + '</label>';
            });
            html += '</div></div>';
        });
        if (!html) {
            html = '<div class="text-sm text-slate-400 py-6 text-center">' + escapeHtml(t('admin_resources_none') || '无匹配') + '</div>';
        }
        root.innerHTML = html;
        root.querySelectorAll('input[type=checkbox]').forEach(function (cb) {
            cb.addEventListener('change', function () {
                if (cb.parentElement) cb.parentElement.classList.toggle('checked', !!cb.checked);
                markRoleEditorDirty();
                updateRoleEditorBadges();
            });
        });
        root.querySelectorAll('.role-perm-group-toggle').forEach(function (btn) {
            btn.addEventListener('click', function () {
                const group = btn.getAttribute('data-group') || '';
                const turnOn = btn.getAttribute('data-all') !== '1';
                const current = _collectRolePermissionIds();
                const set = {};
                current.forEach(function (id) { set[id] = true; });
                (groups[group] || []).forEach(function (p) {
                    if (turnOn) set[p.id] = true;
                    else delete set[p.id];
                });
                renderRolePermissions(catalog, Object.keys(set));
                markRoleEditorDirty();
                updateRoleEditorBadges();
            });
        });
    }

    //: Kinds whose assignable set is the platform's tenant limit rather than a
    //: tenant-owned scope (see auth/service.py `_project_owned_ids`: model and
    //: tool are "global, controlled by tenant grants"). Only these can go stale
    //: *after* a role was saved, because only the platform can shrink the limit
    //: underneath it. menu/skill/agent resolve from page scope or tenant-owned
    //: objects, which the tenant admin maintains itself.
    const _LIMIT_SCOPED_KINDS = ['model', 'tool'];

    // A saved role keeps the grants it was written with, but the platform can
    // narrow a tenant's model/tool limit afterwards. A dropped id then never
    // appears in the assignable catalog, so the picker can neither render nor
    // uncheck it — yet `_resCount` still counted it in the tab badge and
    // `_collectResourceGrants` wrote it straight back on the next save. That is
    // the "只有一个模型，数量却是 3" mismatch. Reconcile the selections against
    // the assignable catalog so the badge, the list and the saved grants agree.
    // Nothing is persisted here (no dirty flag) and nothing is re-rendered: the
    // leftovers stop being counted and stop being re-saved.
    function _reconcileRoleSelections(kind, data) {
        const st = _resourceState[kind];
        if (!st || st.reconciled) return;
        if (_LIMIT_SCOPED_KINDS.indexOf(kind) < 0) {
            st.reconciled = true;
            return;
        }
        // A filtered or truncated page proves nothing about the ids it omits.
        if (st.q) return;
        const items = data.items || [];
        if ((data.total || 0) > items.length) {
            // Only a slice of the catalog is on screen: ask for all of it once,
            // without holding up the render that is already using this page.
            _loadResourceCatalog(kind, '', 1, Math.max(data.total || 0, 1))
                .then(function (full) { _applyRoleSelectionReconcile(kind, full.items || []); })
                .catch(function () {});
            return;
        }
        _applyRoleSelectionReconcile(kind, items);
    }

    function _applyRoleSelectionReconcile(kind, items) {
        const st = _resourceState[kind];
        if (!st) return;
        st.reconciled = true;
        if (!st.selected.size) return;
        const assignable = new Set(items.map(function (it) { return it.resource_id; }));
        let dropped = false;
        Array.from(st.selected).forEach(function (rid) {
            if (assignable.has(rid)) return;
            st.selected.delete(rid);
            dropped = true;
        });
        if (!dropped) return;
        updateRoleEditorBadges();
        // A default may only point at a still-granted model (spec 5.1), so a
        // dropped model must not survive as a default either.
        if (kind === 'model') _refreshRoleModelDefaults();
    }

    async function _renderRoleKindList(kind) {
        const manage = document.getElementById('role-res-manage-' + kind);
        const list = document.getElementById('role-res-list-' + kind);
        if (!manage || !list) return;
        const st = _resourceState[kind];
        if (!st) return;
        list.innerHTML = '<div class="text-xs text-slate-400 py-2">' + escapeHtml(t('admin_loading') || '…') + '</div>';
        try {
            const data = await _loadResourceCatalog(kind, st.q, st.page, st.pageSize);
            const items = data.items || [];
            const total = data.total || 0;
            const actions = data.resource_actions || _resourceActions[kind] || [];
            st.actions = actions;
            st.loaded = true;
            _reconcileRoleSelections(kind, data);
            if (!items.length) {
                list.innerHTML = '<div class="text-xs text-slate-400 py-2">' + escapeHtml(t('admin_resources_none')) + '</div>';
            } else {
                list.innerHTML = items.map(function (r) {
                    const rid = r.resource_id;
                    const checked = st.selected.has(rid);
                    const idSuffix = rid && rid.indexOf('nav:') === 0 ? rid.slice(4) : rid;
                    return '<label class="role-res-item' + (checked ? ' checked' : '') + '">' +
                        '<input type="checkbox" value="' + escapeHtml(rid) + '"' + (checked ? ' checked' : '') + '>' +
                        '<span class="flex flex-col leading-tight min-w-0"><span class="truncate">' + escapeHtml(r.name) + '</span>' +
                        (idSuffix && idSuffix !== r.name ? '<span class="text-[10px] text-slate-400 truncate">' + escapeHtml(idSuffix) + '</span>' : '') +
                        '</span></label>';
                }).join('');
            }
            list.querySelectorAll('input[type=checkbox]').forEach(function (cb) {
                cb.addEventListener('change', function () {
                    if (cb.checked) st.selected.add(cb.value);
                    else st.selected.delete(cb.value);
                    if (cb.parentElement) cb.parentElement.classList.toggle('checked', !!cb.checked);
                    markRoleEditorDirty();
                    updateRoleEditorBadges();
                    if (kind === 'model') _refreshRoleModelDefaults();
                });
            });
            const totalEl = manage.querySelector('.role-res-total[data-kind="' + kind + '"]');
            if (totalEl) totalEl.textContent = (t('admin_total_label') || '共') + ' ' + total;
            const pag = manage.querySelector('.role-res-pagination[data-kind="' + kind + '"]');
            if (pag && typeof renderPagination === 'function') {
                renderPagination(pag, st.page, st.pageSize, total, function (p) {
                    st.page = p;
                    _renderRoleKindList(kind);
                });
            } else if (pag) {
                pag.innerHTML = '';
            }
        } catch (err) {
            list.innerHTML = '<div class="text-xs text-red-500 py-2">' + escapeHtml(err.message || t('load_error')) + '</div>';
        }
    }

    function _refreshRoleModelDefaults() {
        const node = document.getElementById('adm-fld-modeldefaults');
        if (!node) return;
        const modelSel = (_resourceState.model && _resourceState.model.selected) ? _resourceState.model.selected : new Set();
        const options = Array.from(modelSel);
        node.innerHTML = _modelCapabilities.map(function (cap) {
            const val = _modelDefaultSel[cap] || '';
            const opts = '<option value="">' + escapeHtml(t('admin_resources_none')) + '</option>' +
                options.map(function (rid) {
                    return '<option value="' + escapeHtml(rid) + '"' + (rid === val ? ' selected' : '') + '>' +
                        escapeHtml(rid) + '</option>';
                }).join('');
            return '<div class="flex items-center gap-2 py-1">' +
                '<span class="w-32 text-xs text-slate-500 dark:text-slate-400">' + escapeHtml(cap) + '</span>' +
                '<select class="agent-input model-default-select" data-cap="' + escapeHtml(cap) + '">' + opts + '</select>' +
                '</div>';
        }).join('');
        // Drop defaults that are no longer granted.
        Object.keys(_modelDefaultSel).forEach(function (cap) {
            if (_modelDefaultSel[cap] && !modelSel.has(_modelDefaultSel[cap])) delete _modelDefaultSel[cap];
        });
        node.querySelectorAll('.model-default-select').forEach(function (sel) {
            sel.addEventListener('change', function () {
                const cap = sel.getAttribute('data-cap');
                const v = sel.value;
                if (v) _modelDefaultSel[cap] = v;
                else delete _modelDefaultSel[cap];
                markRoleEditorDirty();
            });
        });
    }

    function closeRoleEditorErr() {
        const el = document.getElementById('role-editor-error');
        if (el) { el.classList.add('hidden'); el.textContent = ''; }
    }

    function showRoleEditorErr(msg) {
        const el = document.getElementById('role-editor-error');
        if (el) { el.textContent = msg || ''; el.classList.remove('hidden'); }
    }

    function closeRoleEditor() {
        if (_roleEditor.dirty && !confirmDiscard(true)) return;
        closeRoleEditorNoPrompt();
    }

    function closeRoleEditorNoPrompt() {
        const el = document.getElementById('role-editor');
        if (el) el.classList.add('hidden');
        _roleEditor = {
            open: false, dirty: false, mode: 'create', role: null,
            submit: null, onConflictReload: null, statusEl: null,
            successMsg: '', activeTab: 'basic', perms: null,
        };
    }

    async function openRoleEditorPage(cfg) {
        const el = ensureRoleEditor();
        _localizeRoleEditorChrome();
        _roleEditor = {
            open: true,
            dirty: false,
            mode: cfg.mode || 'create',
            role: cfg.role || null,
            submit: cfg.submit || null,
            onConflictReload: cfg.onConflictReload || null,
            statusEl: cfg.statusEl || null,
            successMsg: cfg.successMsg || t('admin_saved'),
            activeTab: 'basic',
            perms: cfg.perms || [],
        };
        document.getElementById('role-editor-title').textContent = cfg.title || '';
        document.getElementById('role-editor-sub').innerHTML = cfg.subtitleHtml || '';
        const submitBtn = document.getElementById('role-editor-submit');
        submitBtn.textContent = cfg.submitLabel || t('admin_save');
        // Start every open from an enabled button: a request that never settled
        // (or an older build that forgot to clear the in-flight flag) must not
        // leave this form looking interactive while it swallows every click.
        submitBtn.disabled = false;
        document.getElementById('role-dirty-pill').classList.remove('show');
        closeRoleEditorErr();

        const codeWrap = document.getElementById('role-code-wrap');
        if (cfg.codeLocked) {
            codeWrap.innerHTML = '<div class="agent-input-locked" id="adm-fld-code">' + escapeHtml(cfg.code || '') + '</div>';
        } else {
            codeWrap.innerHTML = '<input type="text" id="adm-fld-code" class="agent-input" value="' + escapeHtml(cfg.code || '') + '" placeholder="' + escapeHtml(t('admin_field_code_hint') || '') + '">';
            const codeInput = document.getElementById('adm-fld-code');
            if (codeInput) codeInput.addEventListener('input', markRoleEditorDirty);
        }
        const nameInput = document.getElementById('adm-fld-name');
        nameInput.value = cfg.name || '';

        renderRolePermissions(cfg.perms || [], cfg.selectedPermissions || []);
        updateRoleEditorBadges();
        switchRoleEditorTab('basic');
        // Prefetch model list so defaults can resolve immediately on the model tab.
        _renderRoleKindList('model');
        el.classList.remove('hidden');
        try { nameInput.focus(); } catch (e) {}
    }

    async function submitRoleEditor() {
        if (!_roleEditor.submit) return;
        closeRoleEditorErr();
        const nameEl = document.getElementById('adm-fld-name');
        const codeEl = document.getElementById('adm-fld-code');
        const name = (nameEl && nameEl.value || '').trim();
        if (!name) {
            showRoleEditorErr(t('admin_field_name') + ' *');
            switchRoleEditorTab('basic');
            return;
        }
        const body = {
            name: name,
            permissions: _collectRolePermissionIds(),
            resource_grants: _collectResourceGrants(),
            model_defaults: _collectModelDefaults(),
        };
        if (_roleEditor.mode !== 'edit') {
            const code = (codeEl && codeEl.value || '').trim();
            if (!code) {
                showRoleEditorErr(t('admin_field_code') + ' *');
                switchRoleEditorTab('basic');
                return;
            }
            body.code = code;
        }
        const btn = document.getElementById('role-editor-submit');
        if (btn) btn.disabled = true;
        try {
            await _roleEditor.submit(body);
            const statusEl = _roleEditor.statusEl;
            const msg = _roleEditor.successMsg;
            closeRoleEditorNoPrompt();
            if (statusEl) status(statusEl, msg, true);
            await loadRolesView();
        } catch (err) {
            if (err && (err.status === 409 || err.code === 'conflict')) {
                showRoleEditorErr(err.message || t('admin_conflict'));
                if (_roleEditor.onConflictReload) _roleEditor.onConflictReload();
                return;
            }
            showRoleEditorErr((err && err.message) || t('admin_save_failed'));
        } finally {
            // The button is only disabled while a write is in flight. Clearing it
            // on every outcome matters because the editor node outlives a single
            // open: a form reopened after a successful save would otherwise render
            // a disabled button, so the user fills it in, clicks, and nothing
            // happens and no request is sent.
            if (btn) btn.disabled = false;
        }
    }

    async function openRoleCreate() {
        const perms = await ensurePermCatalog();
        if (!perms) return;
        _resetResourceState([], {});
        await openRoleEditorPage({
            mode: 'create',
            title: t('role_create'),
            subtitleHtml: escapeHtml(t('role_create_sub') || '创建后编码不可修改'),
            codeLocked: false,
            code: '',
            name: '',
            perms: perms,
            selectedPermissions: [],
            submitLabel: t('admin_create'),
            statusEl: document.getElementById('role-status'),
            submit: async function (body) {
                await apiFetch(_roleApiBase(), { method: 'POST', body: body });
            },
            onConflictReload: function () { loadRolesView(); },
        });
    }

    async function openRoleEdit(id) {
        const r = _roleById[id];
        if (!r) return;
        const perms = await ensurePermCatalog();
        if (!perms) return;
        _resetResourceState(r.resource_grants || [], r.model_defaults || {});
        await openRoleEditorPage({
            mode: 'edit',
            role: r,
            title: t('role_edit_title'),
            subtitleHtml: (t('admin_field_code') || '编码') + ' <code>' + escapeHtml(r.code || '') + '</code>',
            codeLocked: true,
            code: r.code || '',
            name: r.name || '',
            perms: perms,
            selectedPermissions: r.permissions || [],
            submitLabel: t('admin_save'),
            statusEl: document.getElementById('role-status'),
            submit: async function (body) {
                body.expected_version = r.version;
                await apiFetch(_roleApiBase() + '/' + encodeURIComponent(id), { method: 'POST', body: body });
            },
            onConflictReload: function () { loadRolesView(); },
        });
    }

    async function copyRole(id) {
        const r = _roleById[id];
        if (!r) return;
        const perms = await ensurePermCatalog();
        if (!perms) return;
        // Read then create (task 5.5); never copy admin qualification or bindings.
        const assignableSet = {};
        perms.forEach(function (p) { assignableSet[p.id] = p; });
        const copyablePermissions = (r.permissions || []).filter(function (pid) {
            return assignableSet[pid] && assignableSet[pid].assignable !== false;
        });
        _resetResourceState(r.resource_grants || [], r.model_defaults || {});
        await openRoleEditorPage({
            mode: 'copy',
            role: r,
            title: t('role_copy_title'),
            subtitleHtml: escapeHtml((t('role_copy_from') || '从 {name} 复制').replace('{name}', r.name || r.code || '')),
            codeLocked: false,
            code: '',
            name: (r.name || '') + (t('role_copy_suffix') || '（副本）'),
            perms: perms,
            selectedPermissions: copyablePermissions,
            submitLabel: t('admin_create'),
            statusEl: document.getElementById('role-status'),
            submit: async function (body) {
                await apiFetch(_roleApiBase(), { method: 'POST', body: body });
            },
            onConflictReload: function () { loadRolesView(); },
        });
    }

    async function deleteRole(id) {
        const r = _roleById[id];
        if (!r) return;
        if (!window.confirm(t('admin_delete_confirm_role').replace('{name}', r.name))) return;
        try {
            await apiFetch(_roleApiBase() + '/' + encodeURIComponent(id), { method: 'DELETE' });
            status(document.getElementById('role-status'), t('admin_deleted'), true);
            await loadRolesView();
        } catch (err) {
            status(document.getElementById('role-status'), err.message || t('admin_save_failed'), false);
            if (err.status === 409 || err.code === 'conflict' || err.code === 'in_use') await loadRolesView();
        }
    }

    function openTenantRoles(id) {
        // Platform-admin target-tenant role editing (task 3.2). Set the platform
        // target so role CRUD and the assign catalog route to the platform
        // endpoints for this tenant, then show the roles view. If an admin form
        // has unsaved changes, confirm discard FIRST so the target is not
        // switched away while a stale draft still references the previous target.
        const tn = _tenantById[id];
        if (!tn) return;
        if (typeof window.__identityAdminDirtyGuard__ === 'function'
            && !window.__identityAdminDirtyGuard__()) return;
        _rolePlatformTarget = id;
        try {
            if (typeof window.navigateTo === 'function') window.navigateTo('roles');
            else if (typeof navigateTo === 'function') navigateTo('roles');
        } catch (e) {}
        setTimeout(function () { loadRolesView(); }, 50);
    }

    function viewRoleMembers(code) {
        // Reuse the member list's role filter (task 5.5): set the filter and
        // navigate to the Users page.
        const sel = document.getElementById('member-role-filter');
        if (sel && code) sel.value = code;
        // jump to the member view
        try {
            if (typeof window.navigateTo === 'function') window.navigateTo('system_user');
            else if (typeof navigateTo === 'function') navigateTo('system_user');
        } catch (e) {}
        if (sel) { _memberFilters.role = code; _memberPage = 1; }
        // Wait for the view switch to render, then load.
        setTimeout(function () { loadMembersView(); }, 50);
    }

    // ---- Organization view (task 5.6) -------------------------------------
    async function loadOrgView() {
        const org = document.getElementById('org-tree');
        const btn = document.getElementById('dept-create-btn');
        if (!org) return;
        org.innerHTML = '<div class="text-sm text-slate-400 dark:text-slate-500">' + t('tenant_loading') + '</div>';
        try {
            const data = await apiFetch('/api/tenant/departments');
            _deptById = {};
            _depts = data.items || [];
            _depts.forEach(function (d) { _deptById[d.id] = d; });
            // The virtual `__root__` department always exists and is never drawn
            // as a node, so an empty tree is decided by the visible departments
            // rather than the raw item count (an untouched tenant still returns
            // __root__). Without this the area rendered blank and the page gave
            // no hint that the tree was simply empty.
            const hasDepts = _depts.some(function (d) { return d.code !== '__root__'; });
            const tree = hasDepts ? buildOrgTree() : '';
            org.innerHTML = tree || '<div class="text-sm text-slate-400">' + t('org_empty') + '</div>';
            // chat.html ships the control hidden; reveal it with the view so the
            // page actually offers 新增 instead of a tree nobody can extend
            // (the server still re-authorizes every tenant_admin write).
            if (btn) btn.classList.remove('hidden');
        } catch (err) {
            if (err.message === 'stale-response') return;
            org.innerHTML = '<div class="text-sm text-red-500">' + t('load_error') + ': ' + escapeHtml(err.message) + '</div>';
        }
    }

    function buildOrgTree() {
        // Build a nested tree by parent_id, ordered by sort_order then name.
        const byParent = {};
        // Resolve the synthetic root id FIRST. The __root__ record may appear
        // anywhere in the list (bootstrap typically appends it last), so a
        // single-pass assign-to-rootId groups top-level departments (empty
        // parent_id) under a blank key before rootId is known, leaving the tree
        // blank. Two passes keep top-level nodes attached to the real root.
        let rootId = '';
        for (let i = 0; i < _depts.length; i++) {
            if (_depts[i].code === '__root__') { rootId = _depts[i].id; break; }
        }
        _depts.forEach(function (d) {
            if (d.code === '__root__') { return; }
            const pid = d.parent_id || rootId;
            (byParent[pid] = byParent[pid] || []).push(d);
        });
        Object.keys(byParent).forEach(function (k) {
            byParent[k].sort(function (a, b) {
                return (a.sort_order || 0) - (b.sort_order || 0) || a.name.localeCompare(b.name);
            });
        });
        const walk = function (parentId, depth) {
            const children = byParent[parentId] || [];
            if (!children.length) return '';
            return children.map(function (d) {
                const kids = walk(d.id, depth + 1);
                const hasKids = kids !== '';
                const indent = depth > 0 ? ' style="margin-left:' + (depth * 16) + 'px"' : '';
                const chevron = hasKids
                    ? '<i class="fas fa-chevron-right text-[10px] text-slate-400 mr-1 org-chevron transition-transform duration-150"></i>'
                    : '<i class="fas fa-folder text-amber-500 mr-1"></i>';
                return '<div class="org-node" data-id="' + escapeHtml(d.id) + '"' + indent + '>'
                    + '<div class="flex items-center justify-between px-3 py-2 rounded-lg border border-slate-200 dark:border-white/10 org-row cursor-pointer">'
                    + '<div class="flex items-center gap-2">' + chevron
                    + '<span class="text-sm text-slate-800 dark:text-slate-100">' + escapeHtml(d.name) + '</span>'
                    + '<span class="text-xs text-slate-400">(' + escapeHtml(d.code) + ')</span>'
                    + '<span class="text-xs text-slate-400">' + fmtActive(d.active) + '</span>'
                    + '</div>'
                    + '<div class="flex items-center gap-2">'
                    + '<button class="admin-row-btn" onclick="adminRowAction(\'dept\',\'members\',\'' + escapeHtml(d.id) + '\')"><i class="fas fa-users mr-1"></i>' + escapeHtml(t('role_view_members')) + '</button>'
                    + '<button class="admin-row-btn" onclick="adminRowAction(\'dept\',\'edit\',\'' + escapeHtml(d.id) + '\')"><i class="fas fa-pen mr-1"></i>' + escapeHtml(t('admin_edit')) + '</button>'
                    + '<button class="admin-row-btn danger" onclick="adminRowAction(\'dept\',\'delete\',\'' + escapeHtml(d.id) + '\')"><i class="fas fa-trash mr-1"></i>' + escapeHtml(t('admin_delete')) + '</button>'
                    + '</div></div>'
                    + (hasKids ? '<div class="org-children hidden pl-2">' + kids + '</div>' : '')
                    + '</div>';
            }).join('');
        };
        return walk(rootId, 0);
    }

    function openDeptCreate(parentId) {
        const depts = _depts || [];
        openAdminModal({
            title: t('dept_create'),
            icon: 'fa-plus',
            fields: [
                { name: 'code', label: t('admin_field_code'), type: 'text', required: true, hint: t('admin_field_code_hint') },
                { name: 'name', label: t('admin_field_name'), type: 'text', required: true },
                { name: 'parent_id', label: t('admin_field_parent'), type: 'select', options: deptOptions(depts), value: parentId || '' },
                { name: 'sort_order', label: t('admin_field_sort_order'), type: 'number', value: 0 },
            ],
            submitLabel: t('admin_create'),
            statusEl: document.getElementById('org-status'),
            submit: async function (body) {
                body.parent_id = body.parent_id || null;
                await apiFetch('/api/tenant/departments', { method: 'POST', body: body });
                await loadOrgView();
            },
            onConflictReload: function () { loadOrgView(); },
        });
    }

    function rootDeptId() {
        for (let i = 0; i < _depts.length; i++) {
            if (_depts[i].code === '__root__') return _depts[i].id;
        }
        return '';
    }

    function openDeptEdit(id) {
        const d = _deptById[id];
        if (!d) return;
        const parentValue = d.parent_id || rootDeptId();
        openAdminModal({
            title: t('dept_edit_title'),
            subtitle: d.code || '',
            icon: 'fa-pen',
            fields: [
                { name: 'code', label: t('admin_field_code'), type: 'locked', value: d.code },
                { name: 'name', label: t('admin_field_name'), type: 'text', value: d.name, required: true },
                { name: 'sort_order', label: t('admin_field_sort_order'), type: 'number', value: d.sort_order },
                { name: 'active', label: t('admin_field_active'), type: 'checkbox', value: !!d.active },
                { name: 'parent_id', label: t('admin_field_parent'), type: 'select', options: deptOptions(_depts, d.id), value: parentValue },
            ],
            submitLabel: t('admin_save'),
            statusEl: document.getElementById('org-status'),
            submit: async function (body) {
                body.expected_version = d.version;
                body.parent_id = body.parent_id || null;
                await apiFetch('/api/tenant/departments/' + encodeURIComponent(id), { method: 'PUT', body: body });
                await loadOrgView();
            },
            onConflictReload: function () { loadOrgView(); },
        });
    }

    async function deleteDept(id) {
        const d = _deptById[id];
        if (!d) return;
        if (!window.confirm(t('admin_delete_confirm_dept').replace('{name}', d.name))) return;
        try {
            await apiFetch('/api/tenant/departments/' + encodeURIComponent(id), { method: 'DELETE' });
            status(document.getElementById('org-status'), t('admin_deleted'), true);
            await loadOrgView();
        } catch (err) {
            status(document.getElementById('org-status'), err.message || t('admin_save_failed'), false);
            if (err.status === 409 || err.code === 'conflict' || err.code === 'in_use' || err.code === 'cycle') await loadOrgView();
            if (err.code === 'cycle') status(document.getElementById('org-status'), t('org_cycle_rejected'), false);
        }
    }

    function viewDeptMembers(id) {
        const sel = document.getElementById('member-dept-filter');
        if (sel && id) sel.value = id;
        try {
            if (typeof window.navigateTo === 'function') window.navigateTo('system_user');
            else if (typeof navigateTo === 'function') navigateTo('system_user');
        } catch (e) {}
        if (sel) { _memberFilters.department_id = id; _memberPage = 1; }
        setTimeout(function () { loadMembersView(); }, 50);
    }

    // ---- Audit view (task 5.7) --------------------------------------------
    async function loadAuditView() {
        const list = document.getElementById('audit-list');
        if (!list) return;
        list.innerHTML = '<div class="text-sm text-slate-400 dark:text-slate-500">' + t('tenant_loading') + '</div>';
        const f = _auditFilters;
        f.actor = (document.getElementById('audit-actor') || {}).value || '';
        f.action = (document.getElementById('audit-action') || {}).value || '';
        f.result = (document.getElementById('audit-result') || {}).value || '';
        const query = qs({
            actor: f.actor, action: f.action, result: f.result,
            since: f.since, until: f.until,
            page: _auditPage, page_size: _auditPageSize,
        });
        try {
            const data = await apiFetch('/api/identity/audit?' + query);
            const items = data.items || [];
            if (!items.length) {
                list.innerHTML = '<div class="text-sm text-slate-400">' + t('audit_empty') + '</div>';
                renderPagination(document.getElementById('audit-pagination'), _auditPage, _auditPageSize, data.total || 0, function (p) { _auditPage = p; loadAuditView(); });
                return;
            }
            const rows = items.map(function (ev) {
                let changes = '';
                try { const ch = JSON.parse(ev.redacted_changes || '{}'); changes = Object.keys(ch).map(function (k) { return k + '=' + String(ch[k]); }).join(', '); } catch (e) {}
                const actionHex = ev.action && ev.action.indexOf('.') > 0 ? '' : '';
                return '<div class="px-4 py-3 rounded-lg border border-slate-200 dark:border-white/10">'
                    + '<div class="flex items-center justify-between"><div class="text-sm font-medium text-slate-800 dark:text-slate-100">' + escapeHtml(ev.action) + '</div>'
                    + '<div class="text-xs ' + (ev.result === 'denied' ? 'text-red-500' : 'text-slate-400') + '">' + escapeHtml(ev.result) + '</div></div>'
                    + '<div class="mt-1 text-xs text-slate-400">' + escapeHtml(ev.actor_username || '-') + ' · ' + new Date((ev.time || 0) * 1000).toLocaleString() + '</div>'
                    + (changes ? '<div class="mt-1 text-xs text-slate-400 break-all">' + escapeHtml(changes) + '</div>' : '')
                    + '</div>';
            }).join('');
            list.innerHTML = rows;
            renderPagination(document.getElementById('audit-pagination'), _auditPage, _auditPageSize, data.total || 0, function (p) { _auditPage = p; loadAuditView(); });
        } catch (err) {
            if (err.message === 'stale-response') return;
            list.innerHTML = '<div class="text-sm text-red-500">' + t('load_error') + ': ' + escapeHtml(err.message) + '</div>';
            status(document.getElementById('audit-status'), err.message, false);
        }
    }

    // ---- row action dispatcher (used by inline onclick) -------------------
    function adminRowAction(kind, action, id) {
        if (action === 'edit') {
            if (kind === 'tenant') openTenantEditor('edit', id);
            else if (kind === 'member') openMemberEdit(id);
            else if (kind === 'role') openRoleEdit(id);
            else if (kind === 'dept') openDeptEdit(id);
            else if (kind === 'platform_user') openPlatformUserEdit(id);
        } else if (action === 'delete') {
            if (kind === 'role') deleteRole(id);
            else if (kind === 'dept') deleteDept(id);
            else if (kind === 'tenant') archiveTenant(id);
        } else if (action === 'restore') {
            if (kind === 'tenant') restoreTenant(id);
        } else if (action === 'admin') {
            // Tenant admin configuration now lives in the editor's tenant
            // management tab; no row-level entry point remains for it.
            if (kind === 'tenant') openTenantEditor('edit', id);
        } else if (action === 'roles') {
            if (kind === 'tenant') openTenantRoles(id);
        } else if (action === 'copy') {
            if (kind === 'role') copyRole(id);
        } else if (action === 'members') {
            if (kind === 'role') viewRoleMembers(id);
            else if (kind === 'dept') viewDeptMembers(id);
        } else if (action === 'reset') {
            if (kind === 'member') resetMemberPassword(id);
            else if (kind === 'platform_user') resetPlatformUser(id);
        } else if (action === 'tenants') {
            if (kind === 'member') openMemberTenants(id);
        }
    }

    // Let console.js navigation ask whether it's safe to leave an admin view.
    // Returns true when there is no unsaved form (or the user confirms discard).
    function identityAdminDirtyGuard() {
        if (_tenantEditor && _tenantEditor.open && tenantEditorIsDirty()) {
            const ok = confirmDiscard(true);
            if (ok) closeTenantEditorNoPrompt();
            return ok;
        }
        if (_roleEditor && _roleEditor.open && _roleEditor.dirty) {
            const ok = confirmDiscard(true);
            if (ok) closeRoleEditorNoPrompt();
            return ok;
        }
        const state = _adminModal;
        if (state && state.open && state.dirty) {
            const ok = confirmDiscard(true);
            if (ok) closeAdminModalNoPrompt();
            return ok;
        }
        return true;
    }

    // ---- wire up create buttons / filters ---------------------------------
    document.addEventListener('DOMContentLoaded', function () {
        const tenantBtn = document.getElementById('tenant-create-btn');
        if (tenantBtn) tenantBtn.addEventListener('click', function () { openTenantEditor('create', null); });
        const memberBtn = document.getElementById('member-create-btn');
        if (memberBtn) memberBtn.addEventListener('click', function () { openMemberCreate(); });
        const roleBtn = document.getElementById('role-create-btn');
        if (roleBtn) roleBtn.addEventListener('click', function () { openRoleCreate(); });
        const deptBtn = document.getElementById('dept-create-btn');
        if (deptBtn) deptBtn.addEventListener('click', function () { openDeptCreate(); });

        // Member filters: debounce search and re-load on filter/status change.
        const msearch = document.getElementById('member-search');
        if (msearch) { let deb; msearch.addEventListener('input', function () { clearTimeout(deb); deb = setTimeout(function () { _memberPage = 1; loadMembersView(); }, 300); }); }
        ['member-status-filter', 'member-role-filter', 'member-dept-filter'].forEach(function (id) {
            const el = document.getElementById(id);
            if (el) el.addEventListener('change', function () { _memberPage = 1; loadMembersView(); });
        });

        // Platform user filters.
        const psearch = document.getElementById('platform-user-search');
        if (psearch) { let deb; psearch.addEventListener('input', function () { clearTimeout(deb); deb = setTimeout(function () { _platformUserPage = 1; loadPlatformUsersView(); }, 300); }); }
        const pstatus = document.getElementById('platform-user-status');
        if (pstatus) pstatus.addEventListener('change', function () { _platformUserPage = 1; loadPlatformUsersView(); });

        // Tenant search.
        const tsearch = document.getElementById('tenant-search');
        if (tsearch) { let deb; tsearch.addEventListener('input', function () { clearTimeout(deb); deb = setTimeout(function () { loadTenantView(); }, 300); }); }
        const tfilter = document.getElementById('tenant-status-filter');
        if (tfilter) tfilter.addEventListener('change', function () { loadTenantView(); });

        // Audit apply button + inputs.
        const applyBtn = document.getElementById('audit-apply');
        if (applyBtn) applyBtn.addEventListener('click', function () { _auditPage = 1; loadAuditView(); });
        ['audit-actor', 'audit-action', 'audit-result'].forEach(function (id) {
            const el = document.getElementById(id);
            if (el) el.addEventListener('change', function () { _auditPage = 1; loadAuditView(); });
        });

        // Org tree expand/collapse (delegated).
        const org = document.getElementById('org-tree');
        if (org) org.addEventListener('click', function (e) {
            const row = e.target.closest('.org-row');
            if (!row) return;
            const chevron = row.querySelector('.org-chevron');
            const node = row.parentElement;
            const children = node.querySelector(':scope > .org-children');
            if (children && chevron) {
                children.classList.toggle('hidden');
                chevron.classList.toggle('-rotate-90');
            }
        });
    });

    // expose for console.js navigation / inline onclick
    window.loadTenantView = loadTenantView;
    window.loadMembersView = loadMembersView;
    window.loadRolesView = loadRolesView;
    window.loadOrgView = loadOrgView;
    window.loadPlatformUsersView = loadPlatformUsersView;
    window.loadAuditView = loadAuditView;
    // Register the fork admin views with console.js (change
    // fork-decoupling-and-tenant-hardening, task 8.6) so navigation iterates a
    // view registry instead of the core file hard-coding fork-only branches.
    // `repaint` keeps the old language-switch re-render; absent when console.js
    // is loaded standalone (contract tests) and never registered.
    if (typeof window.registerConsoleView === 'function') {
        window.registerConsoleView({ id: 'tenant', label: 'menu_tenant', load: loadTenantView, repaint: loadTenantView });
        window.registerConsoleView({ id: 'system_user', label: 'menu_system_user', load: loadMembersView, repaint: loadMembersView });
        window.registerConsoleView({ id: 'roles', label: 'menu_roles', load: loadRolesView, repaint: loadRolesView });
        window.registerConsoleView({ id: 'org', label: 'menu_org', load: loadOrgView, repaint: loadOrgView });
        window.registerConsoleView({ id: 'platform', label: 'menu_platform', load: loadPlatformUsersView, repaint: loadPlatformUsersView });
        window.registerConsoleView({ id: 'audit', label: 'menu_audit', load: loadAuditView, repaint: loadAuditView });
    }
    window.bumpTenantGeneration = bumpTenantGeneration;
    window.adminRowAction = adminRowAction;
    window.openExternalIdentities = openExternalIdentities;
    window.extidSubmit = extidSubmit;
    window.extidDelete = extidDelete;
    window.extidUseAttempt = extidUseAttempt;
    window.extidClose = extidClose;
    // Exposed for the frontend contract tests: these encode the rule this
    // feature turns on (which administrator hits which HTTP surface) and the
    // "who / which channel / which open_id" rendering.
    window.externalIdentityRoutes = externalIdentityRoutes;
    window.externalIdentitySurface = externalIdentitySurface;
    window.externalIdentitiesModalHtml = externalIdentitiesModalHtml;
    window.externalIdentityAccountLabel = externalIdentityAccountLabel;
    window.externalIdentityProviderLabel = externalIdentityProviderLabel;
    window.extidOpen = extidOpen;
    window.__identityAdminDirtyGuard__ = identityAdminDirtyGuard;
})();
