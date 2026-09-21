/* Fork patch layer for the upstream scheduled-task page.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * The Tasks page is assembled from the upstream fragments and scripts, served
 * as-is (channel/web/templates/views/tasks.html, templates/modals/task-edit.html,
 * templates/modals/run-detail.html, static/js/views/tasks.js and
 * static/js/views/tasks-modal.js). Upstream has no notion of this fork's
 * per-action capability projection, of the per-task `capabilities` the server
 * computes for each row, of the personal/shared scope marker, of a closed
 * consumer, or of a delivery target that must not move on an edit. Those cannot
 * be added by wrapping a call or shaping a payload, because they decide what the
 * renderer paints in the middle of its own loop -- so this module carries the
 * fork's version of that render (see the "定制逻辑位于稳定接缝而非上游核心文件内"
 * requirement in openspec/specs/fork-upstream-decoupling/spec.md, which allows a
 * parallel implementation when it lives in a fork namespace, keeps its upstream
 * sources in upstream form, and registers where it came from).
 *
 * UPSTREAM SOURCES (registered, and guarded by
 * tests/test_upstream_drift_guards.py::TasksPagePortDriftTests):
 *   markup  templates/views/tasks.html
 *           templates/modals/task-edit.html
 *           templates/modals/run-detail.html
 *   scripts static/js/views/tasks.js
 *           static/js/views/tasks-modal.js
 * A change to any of them is a change to the contract this file re-implements:
 * re-port the affected override by hand rather than editing the upstream file.
 *
 * Loaded last, after the upstream modules it overrides and after console.js,
 * whose `initDropdown` and `_featureAvailable` it calls. It declares nothing at
 * the top level (the whole file is one IIFE) so it cannot collide with the
 * upstream page's own top-level names.
 */
(function () {
    'use strict';

    // The upstream implementations this module wraps. Captured at load, before
    // any of the assignments below, so a wrapper can still reach the original.
    const upstream = {
        loadTasksView: window.loadTasksView,
        refreshTasksView: window.refreshTasksView,
        renderRunCard: window.renderRunCard,
        initDropdown: window.initDropdown,
        openTaskEditModal: window.openTaskEditModal,
        openTaskCreateModal: window.openTaskCreateModal,
    };
    if (typeof upstream.loadTasksView !== 'function'
            || typeof upstream.initDropdown !== 'function') {
        // The upstream page is not on this document (a page that does not load
        // the fragments above); leave the console exactly as it is rather than
        // installing overrides that would have nothing to override.
        return;
    }

    // ------------------------------------------------------------------
    // Per-action availability. One source of truth: the server projection
    // merged into /auth/context, read through console.js's own accessor so this
    // module cannot end up with a second opinion about what is open.
    // ------------------------------------------------------------------
    function open(key) {
        return typeof _featureAvailable === 'function' && _featureAvailable(key);
    }

    const createOpen = () => open('scheduler.create');
    const recordsOpen = () => open('scheduler.runs.list');
    const recordDeleteOpen = () => open('scheduler.runs.delete');

    // Hide by inline style, not by the `hidden` class: upstream's
    // switchTasksTab() toggles that class on the add button itself, and an
    // inline `display:none` is what survives that toggle.
    function setShown(id, shown) {
        const el = document.getElementById(id);
        if (el) el.style.display = shown ? '' : 'none';
    }

    // The header's controls follow the projection, per action. The per-task
    // verbs do NOT belong here: those come from each task's own
    // `capabilities` and are applied while its card is built.
    function applyActionGating() {
        if (!recordsOpen() && tasksActiveTab === 'records') {
            // A capability closed under a live page: the records tab is about to
            // disappear, so the pane it was showing must not stay selected.
            switchTasksTab('tasks');
        }
        setShown('task-add-btn', createOpen());
        setShown('tasks-tab-records', recordsOpen());
    }

    // ------------------------------------------------------------------
    // Tab selection. `console-route-lifecycle` wants a registered `tab`
    // parameter in the address; the fork's router does not reflect sub-tabs for
    // any page (config/memory/knowledge behave the same way) and its hash model
    // is `#view-<id>`, so mirroring one page's tabs into it would invent an
    // address scheme the router cannot restore. What this fork does instead is
    // remember the tab for the session, so the state survives a refresh and a
    // return to the page; the address half is registered as an open item with
    // the router reconciliation rather than half-implemented here.
    // ------------------------------------------------------------------
    const TAB_STORE = 'cow_tasks_active_tab';
    let rememberedTab = '';

    function routeNoteTab(viewId, tab) {
        if (viewId !== 'tasks' || !tab) return;
        rememberedTab = tab;
        try {
            sessionStorage.setItem(TAB_STORE, tab);
        } catch (_) {
            // A browser that refuses sessionStorage still gets the in-page
            // behaviour; only the across-reload memory is lost.
        }
    }

    function restoreRememberedTab() {
        if (!recordsOpen()) return;
        let tab = rememberedTab;
        if (!tab) {
            try {
                tab = sessionStorage.getItem(TAB_STORE) || '';
            } catch (_) { tab = ''; }
        }
        if (tab === 'records' && tasksActiveTab !== 'records') switchTasksTab('records');
    }

    // ------------------------------------------------------------------
    // Late-answer guard. The console bumps `_authEpoch` when the signed-in
    // identity changes and `_authContextSeq` whenever the authorization context
    // is invalidated, which is what entering the app (a tenant switch) does.
    // This page reads a per-tenant ledger, so a response that started under the
    // previous identity must not paint its rows into the new tenant's page
    // (database-runtime-consumers: "迟到响应 MUST NOT 恢复旧身份内容").
    // ------------------------------------------------------------------
    function identityMark() {
        const epoch = typeof _authEpoch === 'number' ? _authEpoch : null;
        const seq = typeof _authContextSeq === 'number' ? _authContextSeq : null;
        // A page without console.js in scope has no identity to compare against;
        // `null` disables the check rather than dropping every answer.
        return (epoch === null || seq === null) ? null : epoch + ':' + seq;
    }

    function dropped(mark) {
        return mark !== null && mark !== identityMark();
    }

    // ------------------------------------------------------------------
    // The task list. Upstream's loader returns early on a non-success answer,
    // which leaves the hardcoded "Loading..." placeholder on screen forever --
    // the exact hang tests/test_scheduler_frontend.cjs was written for -- and it
    // paints a run/enable control for every row regardless of the per-task
    // decision the server sent with that row.
    // ------------------------------------------------------------------
    // ------------------------------------------------------------------
    // Reading a refusal. One rule for both panes: the closed consumer names
    // itself (a code, and in older answers only a message), a refusal carries
    // the server's own reason, and neither is "nothing here yet" -- which is a
    // success with an empty list.
    // ------------------------------------------------------------------
    function closedConsumer(data) {
        const code = (data && data.code) || '';
        return code === 'database_unavailable'
            || /unavailable in database identity mode/i.test(String((data && data.message) || ''));
    }

    function failureText(data, fallbackKey) {
        if (!data || !data.message || closedConsumer(data)) return t(fallbackKey);
        return data.message;
    }

    function emptyStateText(data) {
        return failureText(data, 'tasks_unavailable');
    }

    function finishTaskList(text) {
        const emptyEl = document.getElementById('tasks-empty');
        const listEl = document.getElementById('tasks-list');
        if (emptyEl) {
            const line = emptyEl.querySelector('p');
            if (line) line.textContent = text;
            emptyEl.classList.remove('hidden');
        }
        if (listEl) listEl.classList.add('hidden');
        tasksLoaded = true;
    }

    function loadTasksView() {
        applyActionGating();
        restoreRememberedTab();
        if (tasksLoaded) return;
        const mark = identityMark();
        // The list tags each task with an owning Agent; make sure the roster is
        // in hand first so findAgent()/multiAgentMode() resolve the avatar.
        const rosterReady = agentCatalog.length ? Promise.resolve() : loadAgentCatalog();
        return rosterReady.then(() => fetch('/api/scheduler?agent_id=')
            .then(r => r.json())
            .then(data => {
                if (dropped(mark)) return;
                const listEl = document.getElementById('tasks-list');
                if (!data || data.status !== 'success') {
                    finishTaskList(emptyStateText(data));
                    return;
                }
                const allTasks = data.tasks || [];
                if (allTasks.length === 0) {
                    finishTaskList(currentLang === 'zh' ? '暂无定时任务' : 'No scheduled tasks');
                    return;
                }
                const emptyEl = document.getElementById('tasks-empty');
                if (emptyEl) emptyEl.classList.add('hidden');
                if (!listEl) return;
                listEl.classList.remove('hidden');
                listEl.innerHTML = '';
                allTasks.forEach(task => listEl.appendChild(renderTaskCard(task)));
                tasksLoaded = true;
            })
            .catch(() => {
                if (dropped(mark)) return;
                finishTaskList(t('tasks_unavailable'));
            }));
    }

    // A card the server has already ruled on: the verbs are the row's own
    // `capabilities`, so the page cannot offer an action the five scheduler
    // handlers would refuse.
    function renderTaskCard(task) {
        const isEnabled = task.enabled !== false;
        const caps = task.capabilities || { run: false, manage: false, view: true };
        const isMine = (task.scope || 'public') === 'personal';
        const card = document.createElement('div');
        card.className = 'bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10 p-4';
        card.dataset.taskId = task.id;
        if (!isEnabled) card.classList.add('opacity-50');

        const schedule = task.schedule || {};
        let typeLabel = '';
        if (schedule.type === 'cron') {
            typeLabel = `<span class="text-xs font-mono text-slate-400">${escapeHtml(schedule.expression || '')}</span>`;
        } else if (schedule.type === 'interval') {
            const seconds = schedule.seconds || 0;
            const hours = Math.floor(seconds / 3600);
            const mins = Math.floor((seconds % 3600) / 60);
            const secs = seconds % 60;
            const parts = [];
            if (hours > 0) parts.push(`${hours}h`);
            if (mins > 0) parts.push(`${mins}m`);
            if (secs > 0 || parts.length === 0) parts.push(`${secs}s`);
            typeLabel = `<span class="text-xs text-slate-400">${parts.join(' ')}</span>`;
        } else {
            typeLabel = `<span class="text-xs text-slate-400">${escapeHtml(schedule.type || 'once')}</span>`;
        }

        let nextRun = '--';
        if (task.next_run_at) {
            const d = new Date(task.next_run_at);
            if (!isNaN(d.getTime())) nextRun = d.toLocaleString();
        }
        const action = task.action || {};
        const taskContent = action.content || action.task_description || '';
        const toggleId = 'toggle-' + task.id;

        // 本人 / 公共: what the owner rules turn on, so the page has to show it.
        const scopeChip = `<span class="text-[10px] leading-none px-1.5 py-0.5 rounded-full ${isMine
            ? 'bg-primary-50 text-primary-500 dark:bg-primary-500/10'
            : 'bg-slate-100 text-slate-400 dark:bg-white/10'}">${escapeHtml(isMine
                ? (currentLang === 'zh' ? '本人' : 'Mine')
                : (currentLang === 'zh' ? '公共' : 'Shared'))}</span>`;
        // Owner face: only when several Agents exist, or every card would carry
        // the same one.
        const owner = (multiAgentMode() && task.agent_id) ? findAgent(task.agent_id) : null;
        const ownerChip = owner
            ? `<span class="inline-flex items-center gap-1 ml-2 pl-1 pr-1.5 py-0.5 rounded-full bg-slate-100 dark:bg-white/10 text-[10px] leading-none text-slate-400 dark:text-slate-500">
                    ${agentAvatarHTML(owner, 15)}<span class="truncate max-w-[80px]">${escapeHtml(owner.name || owner.id)}</span>
               </span>`
            : '';
        card.innerHTML = `
            <div class="flex items-center gap-2 mb-2">
                <span class="w-2 h-2 rounded-full ${isEnabled ? 'bg-primary-400' : 'bg-slate-300 dark:bg-slate-600'}"></span>
                <span class="font-medium text-sm text-slate-700 dark:text-slate-200">${escapeHtml(task.name || task.id || '--')}</span>
                ${scopeChip}
                ${ownerChip}
                <div class="flex-1"></div>
                ${typeLabel}
            </div>
            <p class="text-xs text-slate-500 dark:text-slate-400 mb-2 line-clamp-2">${escapeHtml(taskContent)}</p>
            <div class="flex items-center gap-4 text-xs text-slate-400 dark:text-slate-500">
                <span><i class="fas fa-clock mr-1"></i>${currentLang === 'zh' ? '下次执行' : 'Next run'}: ${nextRun}</span>
                <div class="flex-1"></div>
                ${caps.run ? `<button type="button" class="task-run-now px-2 py-1 rounded-md text-primary-500 hover:bg-primary-50 dark:hover:bg-primary-500/10 transition-colors">
                    <i class="fas fa-play mr-1"></i>${t('task_run_now')}
                </button>` : ''}
                ${caps.manage ? `<label class="relative inline-flex items-center cursor-pointer" for="${toggleId}">
                    <input type="checkbox" id="${toggleId}" class="sr-only peer" ${isEnabled ? 'checked' : ''}>
                    <div class="w-9 h-5 bg-slate-200 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-primary-500 dark:bg-slate-600 dark:peer-checked:bg-primary-500"></div>
                </label>` : ''}
            </div>`;

        const runButton = card.querySelector('.task-run-now');
        if (runButton) {
            runButton.addEventListener('click', function (e) {
                e.stopPropagation();
                runTaskNow(task, runButton);
            });
        }
        const checkbox = card.querySelector('#' + toggleId);
        if (checkbox) {
            checkbox.addEventListener('change', function () {
                const newEnabled = this.checked;
                fetch('/api/scheduler/toggle', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ task_id: task.id, enabled: newEnabled, agent_id: task.agent_id || '' }),
                }).then(r => r.json()).then(res => {
                    if (res.status !== 'success') throw new Error(res.message || '');
                    const dot = card.querySelector('.rounded-full.w-2');
                    card.classList.toggle('opacity-50', !newEnabled);
                    if (dot) {
                        dot.classList.toggle('bg-primary-400', newEnabled);
                        dot.classList.toggle('bg-slate-300', !newEnabled);
                        dot.classList.toggle('dark:bg-slate-600', !newEnabled);
                    }
                }).catch(() => { this.checked = !newEnabled; });
            });
        }
        // Clicking the card opens the same edit surface upstream opens, minus
        // the move-a-task-to-another-target affordance (see lockDeliveryTarget).
        card.addEventListener('click', function (e) {
            if (!e.target.closest('label') && !e.target.closest('input[type="checkbox"]')) {
                openTaskEditModal(task);
            }
        });
        card.style.cursor = 'pointer';
        return card;
    }

    // A manual run carries the client's "same request" key: a retry after a lost
    // response is answered from the accepted outcome instead of queueing a
    // second fire. Upstream's version sends none, so the fork keeps its own.
    function runTaskNow(task, button) {
        showConfirmDialog({
            title: t('task_run_confirm_title'),
            message: `${task.name || task.id}: ${t('task_run_confirm_msg')}`,
            okText: t('task_run_now'),
            onConfirm: () => {
                const originalHtml = button.innerHTML;
                button.disabled = true;
                button.innerHTML = `<i class="fas fa-spinner fa-spin mr-1"></i>${t('task_run_now')}`;
                const runKey = (window.crypto && window.crypto.randomUUID)
                    ? window.crypto.randomUUID()
                    : String(Date.now()) + '-' + Math.random().toString(36).slice(2);
                fetch('/api/scheduler/run', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ task_id: task.id, run_key: runKey, agent_id: task.agent_id || '' }),
                }).then(r => r.json()).then(res => {
                    if (res.status !== 'success') throw new Error(res.message || t('task_run_failed'));
                    button.innerHTML = `<i class="fas fa-check mr-1"></i>${t('task_run_started')}`;
                    setTimeout(() => {
                        button.innerHTML = originalHtml;
                        button.disabled = false;
                    }, 1500);
                }).catch(() => {
                    button.innerHTML = `<i class="fas fa-triangle-exclamation mr-1"></i>${t('task_run_failed')}`;
                    setTimeout(() => {
                        button.innerHTML = originalHtml;
                        button.disabled = false;
                    }, 2000);
                });
            },
        });
    }

    // ------------------------------------------------------------------
    // Execution records. Upstream folds a refusal into an empty list, which
    // reads as "nothing ran yet" for a closed consumer, a denial and a genuine
    // empty history alike -- the client is required to keep those apart.
    // ------------------------------------------------------------------
    const RUNS_PAGE_SIZE = 30;

    function showRunsMessage(text) {
        const loadingEl = document.getElementById('runs-loading');
        const emptyEl = document.getElementById('runs-empty');
        const listEl = document.getElementById('runs-list');
        if (loadingEl) {
            loadingEl.classList.add('hidden');
            loadingEl.classList.remove('flex');
        }
        if (listEl) listEl.classList.add('hidden');
        if (!emptyEl) return;
        const lines = emptyEl.querySelectorAll('p');
        if (lines[0]) lines[0].textContent = text;
        // The "records will show up here once a task runs" guide is only true for
        // an empty history, not for a refusal.
        if (lines[1]) lines[1].classList.add('hidden');
        emptyEl.classList.remove('hidden');
        emptyEl.classList.add('flex');
    }

    function runsFailureText(data) {
        return failureText(data, 'scheduler_records_unavailable');
    }

    function loadRunsView() {
        if (runsLoaded) return;
        if (!recordsOpen()) {
            runsLoaded = true;
            showRunsMessage(t('scheduler_records_unavailable'));
            return;
        }
        const mark = identityMark();
        const loadingEl = document.getElementById('runs-loading');
        const emptyEl = document.getElementById('runs-empty');
        const listEl = document.getElementById('runs-list');
        if (loadingEl) {
            loadingEl.classList.remove('hidden');
            loadingEl.classList.add('flex');
        }
        if (emptyEl) emptyEl.classList.add('hidden');
        if (listEl) listEl.classList.add('hidden');
        runsOffset = 0;
        runsHasMore = false;

        const rosterReady = agentCatalog.length ? Promise.resolve() : loadAgentCatalog();
        return rosterReady.then(() => Promise.all([
            fetch(`/api/scheduler/runs?agent_id=&limit=${RUNS_PAGE_SIZE}&offset=0`)
                .then(r => r.json()).catch(() => null),
            (taskInstances && taskInstances.length)
                ? Promise.resolve({ status: 'success', instances: taskInstances })
                : fetch('/api/scheduler/instances').then(r => r.json()).catch(() => null),
        ])).then(([runData, instData]) => {
            if (dropped(mark)) return;
            runsLoaded = true;
            if (instData && instData.status === 'success') taskInstances = instData.instances || [];
            if (!runData || runData.status !== 'success') {
                showRunsMessage(runsFailureText(runData));
                return;
            }
            if (loadingEl) {
                loadingEl.classList.add('hidden');
                loadingEl.classList.remove('flex');
            }
            const runs = runData.runs || [];
            if (runs.length === 0) {
                // A real empty history: restore the copy the refusal replaced.
                const lines = emptyEl ? emptyEl.querySelectorAll('p') : [];
                if (lines[0]) lines[0].textContent = t('records_empty');
                if (lines[1]) {
                    lines[1].textContent = t('records_empty_guide');
                    lines[1].classList.remove('hidden');
                }
                if (emptyEl) {
                    emptyEl.classList.remove('hidden');
                    emptyEl.classList.add('flex');
                }
                if (listEl) listEl.classList.add('hidden');
                return;
            }
            if (emptyEl) {
                emptyEl.classList.add('hidden');
                emptyEl.classList.remove('flex');
            }
            if (!listEl) return;
            listEl.classList.remove('hidden');
            listEl.innerHTML = '';
            runs.forEach(run => listEl.appendChild(renderRunCard(run)));
            runsOffset = runs.length;
            runsHasMore = runs.length >= RUNS_PAGE_SIZE;
            renderRunsLoadMore();
        }).catch(() => {
            if (dropped(mark)) return;
            runsLoaded = true;
            showRunsMessage(t('scheduler_records_unavailable'));
        });
    }

    // Upstream's card always carries a delete control. The server owns that
    // decision (`scheduler.runs.delete`), so a closed verb must not be painted.
    function renderRunCard(run) {
        const card = upstream.renderRunCard(run);
        if (!recordDeleteOpen() && card && card.querySelector) {
            const del = card.querySelector('.run-delete-btn');
            if (del && del.remove) del.remove();
        }
        return card;
    }

    function refreshTasksView() {
        applyActionGating();
        return upstream.refreshTasksView();
    }

    // ------------------------------------------------------------------
    // The edit surface may not move a task to another delivery target.
    // TaskAccessService.update_task refuses a changed receiver/channel_type
    // (`forged_field`), and an existing task has one target for life, so the
    // pickers that upstream leaves switchable are shown as fields of record:
    // read-only, and when the trusted directory is closed they still show the
    // target the task actually delivers to instead of an empty picker.
    // ------------------------------------------------------------------
    const LOCKED_TARGET_IDS = ['task-edit-instance', 'task-edit-recipient'];

    function editingTask() {
        return typeof currentEditingTask !== 'undefined' ? currentEditingTask : null;
    }

    function storedAction() {
        return (editingTask() && editingTask().action) || {};
    }

    // The single option the picker shows in edit mode: the target the task
    // already delivers to, named the way the directory names it when that entry
    // is still around, and by its raw id when it is not.
    function storedTargetOption(el) {
        const action = storedAction();
        if (el.id === 'task-edit-instance') {
            const value = action.instance_id || action.channel_type || '';
            if (!value) return [];
            const known = (taskInstances || []).find(i => i.instance_id === value);
            return [{ value: value, label: (known && (known.name || known.instance_id)) || value }];
        }
        const receiver = action.receiver || '';
        if (!receiver) return [];
        return [{
            value: `${action.instance_id || action.channel_type || ''}:${receiver}`,
            label: action.receiver_name || receiver,
            hint: typeof truncateRecipientId === 'function' ? truncateRecipientId(receiver) : '',
        }];
    }

    // Upstream rebuilds the edited action from the recipient entry behind the
    // picker, so the entry has to carry the stored target's own fields: with the
    // directory closed, the map is empty, and reading nothing back would post a
    // changed (or blank) target that TaskAccessService.update_task refuses as
    // `forged_field`.
    function seedRecipientMap(key) {
        const action = storedAction();
        if (!key || !action.receiver) return;
        taskRecipientMap[key] = {
            channel_type: action.channel_type || '',
            instance_id: action.instance_id || action.channel_type || '',
            receiver: action.receiver,
            name: action.receiver_name || action.receiver,
            is_group: action.is_group || false,
            session_id: action.notify_session_id || action.receiver,
        };
    }

    function lockDeliveryTarget() {
        const tip = document.getElementById('task-instance-tip');
        if (tip) {
            tip.setAttribute('data-tip-key', 'task_target_locked');
            tip.setAttribute('data-tooltip', t('task_target_locked'));
        }
    }

    function unlockDeliveryTarget() {
        const tip = document.getElementById('task-instance-tip');
        if (tip) {
            tip.setAttribute('data-tip-key', 'task_instance_tip');
            tip.setAttribute('data-tooltip', t('task_instance_tip'));
        }
        // The lock is a class on the control, not a one-shot style, so create
        // mode has to take it off again: these are the same two elements the
        // edit modal just showed as fields of record, and a create modal that
        // inherits `pointer-events: none` could not choose a target at all.
        LOCKED_TARGET_IDS.forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.classList.remove('cfg-dropdown-disabled');
        });
    }

    function openTaskEditModal(task) {
        const result = upstream.openTaskEditModal(task);
        lockDeliveryTarget();
        return result;
    }

    function openTaskCreateModal() {
        const result = upstream.openTaskCreateModal();
        unlockDeliveryTarget();
        return result;
    }

    // Every picker inside the modal is built through console.js's initDropdown,
    // including the ones rebuilt asynchronously when the directory arrives, so
    // wrapping that call is what keeps the lock on the re-render too.
    function blockingTargetPicker(el) {
        return !!el && LOCKED_TARGET_IDS.indexOf(el.id) !== -1 && taskModalMode === 'edit';
    }

    window.initDropdown = function (el, options, selectedValue, onChange, opts) {
        if (!blockingTargetPicker(el)) return upstream.initDropdown(el, options, selectedValue, onChange, opts);
        // Show the stored target and nothing else. Upstream falls back to
        // `options[0]` when the passed value is not among them, and on save it
        // reads the selection back -- so offering the directory instead of the
        // stored target would rewrite the task's delivery target on the next
        // save, which is exactly what the edit surface must not allow.
        const stored = storedTargetOption(el);
        const shown = stored.length ? stored : (options || []);
        const value = stored.length ? stored[0].value : selectedValue;
        if (el.id === 'task-edit-recipient') seedRecipientMap(value);
        // Upstream's save path validates the instance variable, not the DOM.
        if (el.id === 'task-edit-instance') selectedTaskInstanceId = value;
        el.classList.add('cfg-dropdown-disabled');
        return upstream.initDropdown(el, shown, value, () => {},
                                     Object.assign({}, opts || {}, { readOnly: true }));
    };

    // ------------------------------------------------------------------
    // Installation. Assignments on the global object: the upstream page calls
    // these by bare name from its own event handlers and inline attributes, and
    // a global function binding is writable, so the property is what those
    // lookups resolve to.
    // ------------------------------------------------------------------
    window.routeNoteTab = routeNoteTab;
    window.loadTasksView = loadTasksView;
    window.refreshTasksView = refreshTasksView;
    window.loadRunsView = loadRunsView;
    window.renderRunCard = renderRunCard;
    window.runTaskNow = runTaskNow;
    window.openTaskEditModal = openTaskEditModal;
    window.openTaskCreateModal = openTaskCreateModal;
})();
