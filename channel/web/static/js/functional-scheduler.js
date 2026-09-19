// Scheduler console: target picker, task authoring and run history (change
// integrate-upstream-core-capabilities, P6).
//
// The fork's scheduler handlers already own the authorization: instances come
// from the caller's granted range, recipients from the trusted directory of
// those instances, and run history from the attribution snapshot written at
// execution time. This module never re-derives any of that -- passing a
// `receiver` the server did not offer, or an `instance_id` the caller does not
// hold, is refused server-side, and the client must not pretend otherwise.
//
// Loaded as a plain IIFE (the console is not transpiled) and exposes exactly
// one global:
//
//     window.RdaiFunctionalScheduler
//
// Pure data functions are separated from DOM code so they can be unit-tested in
// a bare VM context (tests/test_functional_scheduler.cjs):
//
//   - buildCreatePayload(form)  -> the POST body, or a validation error
//   - recipientsFor(state, id)  -> the cached recipient list for one instance
//   - mergeRunPage(state, page) -> dedupe-by-run_id page merge
//   - nextRunsQuery(state)      -> the load-more / polling query
//   - advanceSince(state, page) -> the polling watermark
//   - runDetailView(run)        -> {text, isPreview}
//   - runStatusLabel(run)       -> the status key the row renders
//
// `mount` renders into a caller-provided root and returns a handle. Every
// dynamic value is written with `textContent`/`value` (never raw innerHTML), so
// a task name or a receiver label cannot become markup.

(function () {
    'use strict';

    // Mirrors the server's `_CREATE_ACTION_TYPES` whitelist.
    var ACTION_TYPES = ['send_message', 'agent_task'];
    var SCHEDULE_TYPES = ['cron', 'interval'];
    var PAGE_SIZE = 20;

    // ------------------------------------------------------------------
    // small helpers
    // ------------------------------------------------------------------

    function isArray(value) {
        return Object.prototype.toString.call(value) === '[object Array]';
    }

    function trim(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/^\s+/, '').replace(/\s+$/, '');
    }

    function inputError(code, message) {
        var error = new Error(message || code);
        error.code = code;
        return error;
    }

    // A refusal can arrive as an HTTP error *or* as a 200 carrying
    // ``{status:'error', code}`` -- the scheduler handlers use both shapes. The
    // server's code is the diagnosable part, so it is carried through instead of
    // being replaced by the caller's generic "it failed" label.
    function failureFrom(payload, fallbackCode) {
        var code = trim(payload && payload.code) || fallbackCode;
        var message = trim(payload && payload.message);
        return inputError(code, message || code);
    }

    function errorText(t, error, fallbackKey) {
        var code = (error && error.code) || '';
        var key = code ? 'scheduler_error_' + code : '';
        var text = key ? t(key) : '';
        if (text && text !== key) return text;
        var message = trim(error && error.message);
        if (message) return message;
        return t(fallbackKey || 'scheduler_action_failed');
    }

    // ------------------------------------------------------------------
    // pure: target selection
    // ------------------------------------------------------------------

    function instancesFrom(payload) {
        if (!payload || payload.status !== 'success') return [];
        return isArray(payload.instances) ? payload.instances : [];
    }

    function recipientsFrom(payload) {
        if (!payload || payload.status !== 'success') return [];
        return isArray(payload.recipients) ? payload.recipients : [];
    }

    function recipientsFor(state, instanceId) {
        if (!state || !state.recipients) return [];
        return state.recipients[instanceId] || [];
    }

    // ------------------------------------------------------------------
    // pure: task authoring
    // ------------------------------------------------------------------

    // The server resolves the receiver identity itself (channel type, instance,
    // group flag, notify session) and only accepts the text that belongs to the
    // chosen action type. Sending the other type's field is refused, so the
    // payload is built from the form's *type* rather than from whatever happens
    // to still be in the other input.
    function buildCreatePayload(form) {
        form = form || {};
        var name = trim(form.name);
        if (!name) throw inputError('invalid_name');
        var instanceId = trim(form.instanceId);
        if (!instanceId) throw inputError('invalid_instance');
        var receiver = trim(form.receiver);
        if (!receiver) throw inputError('invalid_receiver');
        var type = trim(form.actionType) || ACTION_TYPES[0];
        if (ACTION_TYPES.indexOf(type) === -1) throw inputError('invalid_action_type');
        var content = trim(form.content);
        if (!content) throw inputError('invalid_content');

        var schedule = buildSchedule(form.schedule);
        var action = { type: type, instance_id: instanceId, receiver: receiver };
        if (type === 'send_message') {
            action.content = content;
        } else {
            action.task_description = content;
        }
        return {
            name: name,
            enabled: form.enabled !== false,
            schedule: schedule,
            action: action,
        };
    }

    function buildSchedule(raw) {
        raw = raw || {};
        var type = trim(raw.type) || 'interval';
        if (SCHEDULE_TYPES.indexOf(type) === -1) throw inputError('invalid_schedule_type');
        if (type === 'cron') {
            var expression = trim(raw.expression);
            if (!expression) throw inputError('invalid_schedule_expression');
            return { type: 'cron', expression: expression };
        }
        var interval = trim(raw.interval);
        if (!interval) throw inputError('invalid_schedule_interval');
        return { type: 'interval', interval: interval };
    }

    // ------------------------------------------------------------------
    // pure: history
    // ------------------------------------------------------------------

    function runsFrom(payload) {
        if (!payload || payload.status !== 'success') return [];
        return isArray(payload.runs) ? payload.runs : [];
    }

    // A run can legitimately appear twice across two pages -- a tick that lands
    // between the page request and the poll, or the same second's boundary. The
    // list is keyed by run_id, so a repeat updates the existing row instead of
    // duplicating it, and the first (newest) occurrence keeps its position.
    function mergeRunPage(state, payload) {
        var incoming = runsFrom(payload);
        var seen = {};
        var merged = [];
        var existing = (state && state.runs) || [];
        var i;
        for (i = 0; i < existing.length; i++) {
            var key = String(existing[i].run_id);
            if (seen[key]) continue;
            seen[key] = true;
            merged.push(existing[i]);
        }
        for (i = 0; i < incoming.length; i++) {
            var next = incoming[i];
            var nextKey = String(next.run_id);
            if (seen[nextKey]) {
                for (var j = 0; j < merged.length; j++) {
                    if (String(merged[j].run_id) === nextKey) {
                        merged[j] = next;
                        break;
                    }
                }
                continue;
            }
            seen[nextKey] = true;
            merged.push(next);
        }
        return merged;
    }

    function nextRunsQuery(state) {
        state = state || {};
        var query = { limit: state.limit || PAGE_SIZE, offset: 0 };
        if (trim(state.agentId)) query.agent_id = trim(state.agentId);
        if (trim(state.taskId)) query.task_id = trim(state.taskId);
        if (state.offset) query.offset = state.offset;
        if (state.since !== null && state.since !== undefined) {
            query.since = state.since;
        }
        return query;
    }

    function serializeQuery(query) {
        var parts = [];
        for (var key in query) {
            if (!Object.prototype.hasOwnProperty.call(query, key)) continue;
            var value = query[key];
            if (value === null || value === undefined || value === '') continue;
            parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(value));
        }
        return parts.length ? '?' + parts.join('&') : '';
    }

    // The polling watermark advances only over a fully drained page set: moving
    // `since` past a second whose runs were not all fetched would drop the rest
    // of that second's notifications permanently.
    function advanceSince(state, payload, drained) {
        if (!drained) return state && state.since !== undefined ? state.since : null;
        var rows = runsFrom(payload);
        var highest = null;
        for (var i = 0; i < rows.length; i++) {
            var started = rows[i].started_at;
            if (started === null || started === undefined || started === '') continue;
            var value = Number(started);
            if (isNaN(value)) continue;
            if (highest === null || value > highest) highest = value;
        }
        if (highest === null) {
            return state && state.since !== undefined ? state.since : null;
        }
        return highest;
    }

    // `full_output` is null whenever the viewer may see the run but not the
    // conversation it delivered into. The preview is then the whole answer, and
    // the UI must say so -- silently showing the preview as if it were the full
    // text would misrepresent a partial read as a complete one.
    function runDetailView(run) {
        run = run || {};
        var full = run.full_output;
        if (typeof full === 'string' && full !== '') {
            return { text: full, isPreview: false };
        }
        return { text: trim(run.output_preview), isPreview: true };
    }

    function runStatusLabel(run) {
        var status = trim(run && run.status);
        if (status === 'done' || status === 'success') return 'scheduler_run_status_done';
        if (status === 'failed' || status === 'error') return 'scheduler_run_status_failed';
        if (status === 'running') return 'scheduler_run_status_running';
        return 'scheduler_run_status_unknown';
    }

    // ------------------------------------------------------------------
    // DOM
    // ------------------------------------------------------------------

    function el(doc, tag, className, text) {
        var node = doc.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function button(doc, className, label, onClick) {
        var node = el(doc, 'button', className, label);
        node.type = 'button';
        node.addEventListener('click', onClick);
        return node;
    }

    function docOf(root) {
        return root.ownerDocument || (typeof document !== 'undefined' ? document : null);
    }

    // The run ledger is read-only except for delete, and delete is destructive
    // to a record the user can see, so it is gated on the capability projection
    // the server sent rather than on the button simply being rendered.
    function mount(options) {
        options = options || {};
        var root = options.root;
        if (!root) throw inputError('missing_root');
        var doc = docOf(root);
        var request = typeof options.request === 'function' ? options.request : null;
        var getContext = typeof options.getContext === 'function'
            ? options.getContext : function () { return null; };
        var t = typeof options.t === 'function' ? options.t : function (key) { return key; };
        if (!request) throw inputError('missing_request');

        var state = {
            instances: [],
            recipients: {},
            runs: [],
            since: null,
            offset: 0,
            limit: PAGE_SIZE,
            agentId: '',
            taskId: '',
            detail: null,
            busy: false,
            disposed: false,
            error: '',
        };

        // Both halves are separately gated, and the gate is the server's own
        // projection: rendering the authoring form where `scheduler.create` is
        // closed would offer a submit that can only answer 503, and rendering an
        // empty history where `scheduler.runs.list` is closed would read as
        // "you have no runs" rather than "this view is not open here".
        var canCreate = featureAvailable('scheduler.create');
        var canListRuns = featureAvailable('scheduler.runs.list');

        var container = el(doc, 'div', 'rdai-scheduler space-y-4');
        var errorEl = el(doc, 'div', 'rdai-scheduler-error text-xs text-red-500');
        container.appendChild(errorEl);
        var authoring = el(doc, 'div', 'rdai-scheduler-authoring space-y-3');
        var history = el(doc, 'div', 'rdai-scheduler-history space-y-3');
        if (canCreate) container.appendChild(authoring);
        if (canListRuns) container.appendChild(history);
        root.appendChild(container);

        function setError(message) {
            state.error = message || '';
            errorEl.textContent = state.error;
        }

        function featureAvailable(key) {
            var capabilities = (typeof window !== 'undefined') && window.RdaiFunctionalCapabilities;
            if (!capabilities || typeof capabilities.available !== 'function') return false;
            return capabilities.available(getContext(), key);
        }

        // ---- authoring --------------------------------------------------

        var nameInput = el(doc, 'input', 'rdai-scheduler-name');
        nameInput.type = 'text';
        nameInput.placeholder = t('scheduler_field_name');

        var instanceSelect = el(doc, 'select', 'rdai-scheduler-instance');
        instanceSelect.addEventListener('change', function () {
            state.recipients[instanceSelect.value] = state.recipients[instanceSelect.value] || [];
            renderAuthoring();
            loadRecipients(instanceSelect.value);
        });

        var receiverSelect = el(doc, 'select', 'rdai-scheduler-receiver');

        var typeSelect = el(doc, 'select', 'rdai-scheduler-action-type');
        for (var a = 0; a < ACTION_TYPES.length; a++) {
            var option = el(doc, 'option', '', t('scheduler_action_' + ACTION_TYPES[a]));
            option.value = ACTION_TYPES[a];
            typeSelect.appendChild(option);
        }

        var contentInput = el(doc, 'textarea', 'rdai-scheduler-content');
        contentInput.rows = 3;

        var scheduleTypeSelect = el(doc, 'select', 'rdai-scheduler-schedule-type');
        for (var s = 0; s < SCHEDULE_TYPES.length; s++) {
            var scheduleOption = el(doc, 'option', '', t('scheduler_schedule_' + SCHEDULE_TYPES[s]));
            scheduleOption.value = SCHEDULE_TYPES[s];
            scheduleTypeSelect.appendChild(scheduleOption);
        }
        var scheduleValue = el(doc, 'input', 'rdai-scheduler-schedule-value');
        scheduleValue.type = 'text';
        scheduleValue.placeholder = t('scheduler_schedule_placeholder');

        var submitBtn = button(doc, 'rdai-scheduler-create', t('scheduler_create'), function () {
            createTask();
        });

        // The form is assembled once, then only its *values* change on render:
        // rebuilding the nodes on every refresh would drop focus mid-typing and
        // detach the listeners a second time.
        function field(labelKey, control) {
            var row = el(doc, 'div', 'rdai-scheduler-field');
            row.appendChild(el(doc, 'label', 'rdai-scheduler-label', t(labelKey)));
            row.appendChild(control);
            return row;
        }

        authoring.appendChild(el(doc, 'h3', 'rdai-scheduler-authoring-title',
                                 t('scheduler_authoring_title')));
        authoring.appendChild(field('scheduler_field_name', nameInput));
        authoring.appendChild(field('scheduler_field_instance', instanceSelect));
        authoring.appendChild(field('scheduler_field_receiver', receiverSelect));
        authoring.appendChild(field('scheduler_field_action_type', typeSelect));
        authoring.appendChild(field('scheduler_field_content', contentInput));
        authoring.appendChild(field('scheduler_field_schedule_type', scheduleTypeSelect));
        authoring.appendChild(field('scheduler_field_schedule_value', scheduleValue));
        authoring.appendChild(submitBtn);

        function renderAuthoring() {
            var current = instanceSelect.value;
            instanceSelect.textContent = '';
            if (!state.instances.length) {
                var none = el(doc, 'option', '', t('scheduler_no_instances'));
                none.value = '';
                instanceSelect.appendChild(none);
            }
            for (var i = 0; i < state.instances.length; i++) {
                var instance = state.instances[i];
                var opt = el(doc, 'option', '', instance.name || instance.id);
                opt.value = instance.id;
                instanceSelect.appendChild(opt);
            }
            if (current) instanceSelect.value = current;

            var list = recipientsFor(state, instanceSelect.value);
            var previous = receiverSelect.value;
            receiverSelect.textContent = '';
            if (!list.length) {
                var empty = el(doc, 'option', '', t('scheduler_no_recipients'));
                empty.value = '';
                receiverSelect.appendChild(empty);
            }
            for (var r = 0; r < list.length; r++) {
                var recipient = list[r];
                var recipientOpt = el(doc, 'option', '',
                                      recipient.name || recipient.receiver);
                recipientOpt.value = recipient.receiver;
                receiverSelect.appendChild(recipientOpt);
            }
            if (previous) receiverSelect.value = previous;

            scheduleValue.placeholder = scheduleTypeSelect.value === 'cron'
                ? t('scheduler_cron_placeholder') : t('scheduler_interval_placeholder');
            submitBtn.disabled = state.busy;
        }

        scheduleTypeSelect.addEventListener('change', function () {
            renderAuthoring();
        });

        function loadRecipients(instanceId) {
            if (!instanceId) return Promise.resolve();
            return Promise.resolve()
                .then(function () {
                    return request('/api/scheduler/recipients?instance_id='
                                   + encodeURIComponent(instanceId));
                })
                .then(function (payload) {
                    if (state.disposed) return;
                    state.recipients[instanceId] = recipientsFrom(payload);
                    renderAuthoring();
                })
                .catch(function (error) {
                    if (state.disposed) return;
                    // A failed recipient load is left visible: the list is then
                    // empty, and creating against an empty list is impossible
                    // rather than silently targeting something else.
                    setError(errorText(t, error, 'scheduler_recipients_failed'));
                });
        }

        function createTask() {
            if (state.busy) return Promise.resolve(false);
            var payload;
            try {
                payload = buildCreatePayload({
                    name: nameInput.value,
                    enabled: true,
                    instanceId: instanceSelect.value,
                    receiver: receiverSelect.value,
                    actionType: typeSelect.value,
                    content: contentInput.value,
                    schedule: {
                        type: scheduleTypeSelect.value,
                        expression: scheduleValue.value,
                        interval: scheduleValue.value,
                    },
                });
            } catch (error) {
                setError(errorText(t, error, 'scheduler_create_failed'));
                return Promise.resolve(false);
            }
            // One attempt only. A create is not idempotent from the client's
            // side (the server assigns the id), so an automatic retry could file
            // two tasks for one click; the user retries deliberately instead.
            state.busy = true;
            setError('');
            renderAuthoring();
            return Promise.resolve()
                .then(function () {
                    return request('/api/scheduler/create', { method: 'POST', body: payload });
                })
                .then(function () {
                    if (state.disposed) return true;
                    nameInput.value = '';
                    contentInput.value = '';
                    return refresh();
                })
                .catch(function (error) {
                    if (state.disposed) return false;
                    setError(errorText(t, error, 'scheduler_create_failed'));
                    return false;
                })
                .then(function (result) {
                    state.busy = false;
                    if (!state.disposed) renderAuthoring();
                    return result;
                });
        }

        // ---- history ----------------------------------------------------

        var historyList = el(doc, 'div', 'rdai-scheduler-runs space-y-2');
        var historyNote = el(doc, 'p', 'rdai-scheduler-scope text-xs text-slate-400',
                             t('scheduler_history_scope'));
        var loadMoreBtn = button(doc, 'rdai-scheduler-load-more',
                                 t('scheduler_load_more'), function () {
            loadRuns({ append: true });
        });
        var detailBox = el(doc, 'div', 'rdai-scheduler-detail');
        history.appendChild(historyNote);
        history.appendChild(historyList);
        history.appendChild(loadMoreBtn);
        history.appendChild(detailBox);

        function renderRuns() {
            historyList.textContent = '';
            for (var i = 0; i < state.runs.length; i++) {
                historyList.appendChild(renderRunRow(state.runs[i]));
            }
            loadMoreBtn.disabled = state.runs.length === 0
                || state.runs.length % state.limit !== 0;
        }

        function renderRunRow(run) {
            var row = el(doc, 'div', 'rdai-scheduler-run');
            row.setAttribute('data-run-id', String(run.run_id));
            row.appendChild(el(doc, 'span', 'rdai-scheduler-run-name',
                               run.task_name || run.task_id || run.run_id));
            row.appendChild(el(doc, 'span', 'rdai-scheduler-run-status',
                               t(runStatusLabel(run))));
            row.appendChild(button(doc, 'rdai-scheduler-run-open', t('scheduler_detail'),
                                   function () { openDetail(run.run_id); }));
            if (featureAvailable('scheduler.runs.delete')) {
                row.appendChild(button(doc, 'rdai-scheduler-run-delete', t('delete'),
                                       function () { deleteRun(run.run_id); }));
            }
            return row;
        }

        function openDetail(runId) {
            return Promise.resolve()
                .then(function () {
                    return request('/api/scheduler/runs/detail?run_id='
                                   + encodeURIComponent(runId));
                })
                .then(function (payload) {
                    if (state.disposed) return null;
                    if (!payload || payload.status !== 'success' || !payload.run) {
                        throw failureFrom(payload, 'detail_failed');
                    }
                    state.detail = payload.run;
                    renderDetail();
                    return payload.run;
                })
                .catch(function (error) {
                    if (state.disposed) return null;
                    // A failed detail is surfaced, never replaced by an empty
                    // panel: "no output" and "could not load" are different
                    // answers and the user acts differently on each.
                    state.detail = null;
                    setError(errorText(t, error, 'scheduler_detail_failed'));
                    return null;
                });
        }

        function renderDetail() {
            detailBox.textContent = '';
            var run = state.detail;
            if (!run) return;
            var view = runDetailView(run);
            detailBox.appendChild(el(doc, 'h4', 'rdai-scheduler-detail-title',
                                     run.task_name || run.run_id));
            if (view.isPreview) {
                detailBox.appendChild(el(doc, 'span', 'rdai-scheduler-preview-badge',
                                         t('scheduler_preview_only')));
            }
            detailBox.appendChild(el(doc, 'pre', 'rdai-scheduler-detail-body', view.text));
            if (run.error) {
                detailBox.appendChild(el(doc, 'p', 'rdai-scheduler-detail-error',
                                         String(run.error)));
            }
        }

        function deleteRun(runId) {
            var confirmFn = typeof window !== 'undefined' && window.showConfirmDialog;
            if (typeof confirmFn === 'function') {
                return new Promise(function (resolve) {
                    confirmFn({
                        title: t('scheduler_delete_confirm_title'),
                        message: t('scheduler_delete_confirm_msg'),
                        okText: t('delete'),
                        cancelText: t('cancel'),
                        onConfirm: function () { resolve(deleteRunNow(runId)); },
                    });
                });
            }
            return deleteRunNow(runId);
        }

        function deleteRunNow(runId) {
            return Promise.resolve()
                .then(function () {
                    return request('/api/scheduler/runs/delete',
                                   { method: 'POST', body: { run_id: runId } });
                })
                .then(function (payload) {
                    if (state.disposed) return false;
                    if (!payload || payload.status !== 'success') {
                        throw failureFrom(payload, 'delete_failed');
                    }
                    // Removed only after the server confirms: dropping the row
                    // optimistically would show a record as gone that is still
                    // there, and a refresh would bring it back.
                    state.runs = state.runs.filter(function (run) {
                        return String(run.run_id) !== String(runId);
                    });
                    if (state.detail && String(state.detail.run_id) === String(runId)) {
                        state.detail = null;
                        renderDetail();
                    }
                    renderRuns();
                    return true;
                })
                .catch(function (error) {
                    if (state.disposed) return false;
                    setError(errorText(t, error, 'scheduler_delete_failed'));
                    return false;
                });
        }

        function loadRuns(opts) {
            opts = opts || {};
            if (opts.append) {
                state.offset = state.runs.length;
            } else {
                state.offset = 0;
            }
            var query = nextRunsQuery(state);
            return Promise.resolve()
                .then(function () {
                    return request('/api/scheduler/runs' + serializeQuery(query));
                })
                .then(function (payload) {
                    if (state.disposed) return [];
                    if (!payload || payload.status !== 'success') {
                        throw failureFrom(payload, 'history_failed');
                    }
                    state.runs = mergeRunPage(state, payload);
                    state.since = advanceSince(state, payload, true);
                    renderRuns();
                    return state.runs;
                })
                .catch(function (error) {
                    if (state.disposed) return [];
                    setError(errorText(t, error, 'scheduler_history_failed'));
                    return [];
                });
        }

        function refresh() {
            setError('');
            var sequence = Promise.resolve();
            if (canCreate) {
                sequence = sequence
                    .then(function () {
                        return request('/api/scheduler/instances');
                    })
                    .then(function (payload) {
                        if (state.disposed) return [];
                        if (!payload || payload.status !== 'success') {
                            throw failureFrom(payload, 'instances_failed');
                        }
                        state.instances = instancesFrom(payload);
                        renderAuthoring();
                        var first = state.instances.length ? state.instances[0].id : '';
                        if (first) return loadRecipients(first);
                        return [];
                    })
                    .catch(function (error) {
                        if (state.disposed) return [];
                        setError(errorText(t, error, 'scheduler_instances_failed'));
                        return [];
                    });
            }
            if (!canListRuns) return sequence;
            return sequence.then(function () { return loadRuns({ append: false }); });
        }

        renderAuthoring();
        renderRuns();

        return {
            refresh: refresh,
            loadMore: function () { return loadRuns({ append: true }); },
            openDetail: openDetail,
            dispose: function () {
                state.disposed = true;
                state.recipients = {};
                state.runs = [];
                state.detail = null;
                container.remove();
            },
        };
    }

    var api = {
        ACTION_TYPES: ACTION_TYPES,
        SCHEDULE_TYPES: SCHEDULE_TYPES,
        PAGE_SIZE: PAGE_SIZE,
        buildCreatePayload: buildCreatePayload,
        instancesFrom: instancesFrom,
        recipientsFrom: recipientsFrom,
        recipientsFor: recipientsFor,
        runsFrom: runsFrom,
        mergeRunPage: mergeRunPage,
        nextRunsQuery: nextRunsQuery,
        serializeQuery: serializeQuery,
        advanceSince: advanceSince,
        runDetailView: runDetailView,
        runStatusLabel: runStatusLabel,
        mount: mount,
    };

    if (typeof window !== 'undefined') {
        window.RdaiFunctionalScheduler = api;
    }
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }
})();
