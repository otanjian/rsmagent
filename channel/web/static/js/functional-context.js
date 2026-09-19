// Session context controls: live usage read and synchronous compaction
// (change integrate-upstream-core-capabilities, P2; design D2/D3).
//
// The fork handlers already own the authorization: the session's durable owner,
// tenant and per-Agent grants are resolved server-side before any runtime
// instance is touched, and the write passes the shared Origin/CSRF gate. This
// module only renders what the server returned and never re-derives ownership.
//
// Loaded as a plain IIFE (the console is not transpiled) and exposes exactly one
// global:
//
//     window.RdaiFunctionalContext
//
// Pure data functions are separated from DOM code so they can be unit-tested in
// a bare VM context (tests/test_functional_context.cjs):
//
//   - buildUsageView(payload)         -> renderable usage model, or null
//   - panelState(input)               -> closed / unavailable / conflict /
//                                        refused / error / ready (plus the
//                                        transient idle/loading)
//   - buildCompactRequest(sid, agent) -> {path, body}
//   - usagePath(sid, agent)           -> the GET path
//   - compactOutcome(payload)         -> {ok, noop, reason, key}
//   - errorKey(code, status)          -> the locale key for a server code
//
// `mount` renders into a caller-provided root and returns a handle. Every
// dynamic value is written with `textContent` (never raw innerHTML), so a model
// name or a server sentence cannot become markup. Every response is discarded
// unless the (contextRevision, agentId, sessionId) triple it started under is
// still current, so a late answer for a previous tenant or session can never
// repaint the one on screen.
(function () {
    'use strict';

    // The two per-action keys from auth/capability_matrix.py::FEATURE_ACTIONS.
    var KEYS = {
        USAGE: 'session_context.usage',
        COMPACT: 'session_context.compact',
    };

    // Codes that mean "the history moved under you": a real, retryable answer,
    // not a generic failure.
    var CONFLICT_CODES = ['session_busy', 'context_changed'];
    var REFUSED_CODES = ['forbidden', 'agent.use'];

    // ------------------------------------------------------------------
    // small helpers
    // ------------------------------------------------------------------

    function trim(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/^\s+/, '').replace(/\s+$/, '');
    }

    function num(value) {
        var parsed = Number(value);
        return isFinite(parsed) ? parsed : 0;
    }

    function inputError(code, message, status) {
        var error = new Error(message || code);
        error.code = code;
        if (status) error.status = status;
        return error;
    }

    // A refusal can arrive as an HTTP error or as a 200 carrying
    // ``{status:'error', code}``. The server's code is the diagnosable part, so
    // it is carried through instead of being replaced by a generic label.
    function failureFrom(payload, fallbackCode) {
        var code = trim(payload && payload.code) || fallbackCode;
        var message = trim(payload && payload.message);
        return inputError(code, message || code);
    }

    function errorShape(error) {
        return {
            code: trim(error && error.code) || 'internal_error',
            status: num(error && error.status),
            message: trim(error && error.message),
        };
    }

    // ------------------------------------------------------------------
    // pure: usage view model
    // ------------------------------------------------------------------

    // A successful read of a session with no live instance carries no usage
    // fields at all. That is a *state*, not an error and not a zeroed figure:
    // the model keeps `available:false` and nothing else, so the renderer can
    // never draw "0 / 0" over it.
    function buildUsageView(payload) {
        if (!payload || payload.status !== 'success') return null;
        if (payload.available !== true) return { available: false };
        var limit = num(payload.limit);
        var used = num(payload.used);
        var raw = payload.breakdown && typeof payload.breakdown === 'object'
            ? payload.breakdown : null;
        return {
            available: true,
            estimated: payload.estimated === true,
            model: trim(payload.model),
            used: used,
            limit: limit,
            window: num(payload.window),
            messages: num(payload.messages),
            // `used` may exceed `limit` (the trimmer budgets tools separately),
            // so the ring saturates instead of running past 100.
            percent: limit > 0 ? Math.max(0, Math.min(100, Math.round((used / limit) * 100))) : 0,
            breakdown: raw ? {
                system: num(raw.system),
                tools: num(raw.tools),
                history: num(raw.history),
                free: num(raw.free),
            } : null,
        };
    }

    // ------------------------------------------------------------------
    // pure: state machine
    // ------------------------------------------------------------------

    // One predicate for the panel. Closed is checked first: a feature the
    // deployment has not opened renders its own state and issues no request,
    // regardless of what was cached from an earlier identity.
    function panelState(input) {
        input = input || {};
        if (!input.actionAvailable) return 'closed';
        if (input.error) {
            var code = trim(input.error.code);
            if (CONFLICT_CODES.indexOf(code) >= 0) return 'conflict';
            if (REFUSED_CODES.indexOf(code) >= 0 || num(input.error.status) === 403) return 'refused';
            return 'error';
        }
        if (input.loading) return 'loading';
        if (!input.usage) return 'idle';
        if (input.usage.available !== true) return 'unavailable';
        return 'ready';
    }

    // Map a server error code onto a message key. Unknown codes fall back to the
    // generic key (and the server's own sentence), never to a fabricated code.
    function errorKey(code, status) {
        code = trim(code);
        status = num(status);
        if (code === 'store_unavailable') return 'context_error_store_unavailable';
        if (code === 'internal_error') return 'context_error_internal_error';
        if (code === 'invalid_request') return 'context_error_invalid';
        if (code === 'session_not_found') return 'context_error_session_missing';
        if (code === 'compact_failed') return 'context_compact_failed';
        if (code === 'session_busy') return 'context_compact_busy';
        if (code === 'context_changed') return 'context_compact_changed';
        if (REFUSED_CODES.indexOf(code) >= 0 || status === 403) return 'context_denied';
        if (status === 503) return 'context_unavailable';
        return 'context_error';
    }

    // The state line's copy. A ready panel draws figures instead; an error that
    // the vocabulary knows uses its own key, otherwise the server sentence.
    function stateText(t, panel, error) {
        if (panel === 'closed') return t('context_not_open');
        if (panel === 'unavailable') return t('context_empty');
        if (panel === 'conflict') return t('context_conflict');
        if (panel === 'refused') return t('context_denied');
        if (panel === 'loading') return t('context_loading');
        if (panel === 'error') {
            var key = errorKey(error && error.code, error && error.status);
            if (key !== 'context_error') return t(key);
            var message = trim(error && error.message);
            return message || t('context_error');
        }
        return '';
    }

    // ------------------------------------------------------------------
    // pure: compaction request + outcome
    // ------------------------------------------------------------------

    function buildCompactRequest(sessionId, agentId) {
        var sid = trim(sessionId);
        if (!sid) throw inputError('missing_session', 'a session id is required');
        var body = {};
        var agent = trim(agentId);
        // The Agent is optional on the wire; an omitted/empty one resolves
        // through the server's tenant-default path, so it is never sent empty.
        if (agent) body.agent_id = agent;
        return {
            path: '/api/sessions/' + encodeURIComponent(sid) + '/compact_context',
            body: body,
        };
    }

    function usagePath(sessionId, agentId) {
        var sid = trim(sessionId);
        if (!sid) throw inputError('missing_session', 'a session id is required');
        var path = '/api/sessions/' + encodeURIComponent(sid) + '/context_usage';
        var agent = trim(agentId);
        if (agent) path += '?agent_id=' + encodeURIComponent(agent);
        return path;
    }

    // A compaction that found nothing to do is a *successful* no-op: `ok:false`
    // with `no_live_context` / `nothing_to_compact` must never be shown as an
    // error (session-context-controls "无可压缩内容 SHALL 返回成功的无操作结果").
    function compactOutcome(payload) {
        if (!payload || payload.status !== 'success') {
            return { ok: false, noop: false, reason: '', key: 'context_compact_failed' };
        }
        var reason = trim(payload.reason);
        if (payload.ok === true) {
            return { ok: true, noop: false, reason: reason || 'compacted',
                     key: 'context_compact_done' };
        }
        if (reason === 'no_live_context' || reason === 'nothing_to_compact') {
            return { ok: false, noop: true, reason: reason, key: 'context_compact_noop' };
        }
        return { ok: false, noop: false, reason: reason, key: 'context_compact_failed' };
    }

    // ------------------------------------------------------------------
    // DOM helpers
    // ------------------------------------------------------------------

    function docOf(root) {
        if (root && root.ownerDocument) return root.ownerDocument;
        if (typeof document !== 'undefined' && document) return document;
        return null;
    }

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

    function clear(node) {
        if (node) node.textContent = '';
    }

    // `classList` is available in a real document and in the test DOM; the
    // string fallback keeps the module usable where a node only carries a
    // `className`.
    function setHidden(node, hidden) {
        if (!node) return;
        if (node.classList && typeof node.classList.toggle === 'function') {
            node.classList.toggle('hidden', !!hidden);
            return;
        }
        var parts = String(node.className || '').split(/\s+/).filter(function (part) {
            return part && part !== 'hidden';
        });
        if (hidden) parts.push('hidden');
        node.className = parts.join(' ');
    }

    // ------------------------------------------------------------------
    // mount
    // ------------------------------------------------------------------

    // Dependencies are injected so the module is drivable from a bare sandbox:
    //
    //   root              the host element (required)
    //   request(path,opt) the console's shared fetch/authorization seam
    //   t(key)            the current i18n bundle
    //   getContext()      the /auth/context projection for the feature gate
    //   getContextRevision() the console's monotonic auth revision
    //   getSession()      {agentId, sessionId, persisted}
    //   document          optional; defaults to the root's ownerDocument
    //   confirm           optional; when provided the write waits for it
    function mount(options) {
        options = options || {};
        var root = options.root;
        if (!root) throw inputError('missing_root', 'mount requires a root element');
        var doc = options.document || docOf(root);
        if (!doc) throw inputError('missing_document', 'mount requires a document');
        var request = typeof options.request === 'function' ? options.request : null;
        if (!request) throw inputError('missing_request', 'mount requires a request function');
        var t = typeof options.t === 'function' ? options.t : function (key) { return key; };
        var getContext = typeof options.getContext === 'function'
            ? options.getContext : function () { return null; };
        var getSession = typeof options.getSession === 'function'
            ? options.getSession : function () {
                return { agentId: '', sessionId: '', persisted: false };
            };
        var getContextRevision = typeof options.getContextRevision === 'function'
            ? options.getContextRevision : function () { return 0; };
        // Compaction is the explicit result of a click, so no confirmation is
        // asked for by default (matching the desktop console). A caller may
        // still inject one; the write then waits until it is accepted.
        var confirmFn = typeof options.confirm === 'function' ? options.confirm : null;

        var state = {
            usage: null,
            error: null,
            loading: false,
            usageSeq: 0,
            compacting: false,
            compactError: null,
            compactState: 'idle',
            noticeKey: '',
            open: false,
            panel: 'idle',
            disposed: false,
        };

        var container = el(doc, 'div', 'rdai-context');
        var trigger = button(doc, 'rdai-context-trigger', t('context_usage_title'), function () {
            togglePanel();
        });
        var panel = el(doc, 'div', 'rdai-context-panel hidden');
        var stateEl = el(doc, 'div', 'rdai-context-state');
        var figure = el(doc, 'div', 'rdai-context-figure hidden');
        var figureMain = el(doc, 'div', 'rdai-context-figure-main');
        var figureMeta = el(doc, 'div', 'rdai-context-figure-meta');
        figure.appendChild(figureMain);
        figure.appendChild(figureMeta);
        var noticeEl = el(doc, 'div', 'rdai-context-notice');
        var errorEl = el(doc, 'div', 'rdai-context-error');
        var actions = el(doc, 'div', 'rdai-context-actions');
        panel.appendChild(stateEl);
        panel.appendChild(figure);
        panel.appendChild(noticeEl);
        panel.appendChild(errorEl);
        panel.appendChild(actions);
        container.appendChild(trigger);
        container.appendChild(panel);
        clear(root);
        root.appendChild(container);

        function capabilitiesApi() {
            return (typeof window !== 'undefined' && window.RdaiFunctionalCapabilities) || null;
        }

        function featureAvailable(key) {
            var capability = capabilitiesApi();
            if (!capability || typeof capability.available !== 'function') return false;
            return capability.available(getContext(), key);
        }

        function sessionOf() {
            var session = getSession() || {};
            return {
                agentId: trim(session.agentId),
                sessionId: trim(session.sessionId),
                persisted: session.persisted === true,
            };
        }

        // Record the identity a request starts under; `isCurrent` below refuses
        // to apply a result whose triple has moved on. When the shared
        // capability module is absent the same comparison is done locally so a
        // response is dropped, not applied blindly.
        function captureFor(session) {
            var revision = getContextRevision();
            var capability = capabilitiesApi();
            if (capability && typeof capability.capture === 'function') {
                return capability.capture(revision, session.agentId, session.sessionId);
            }
            return {
                contextRevision: revision == null ? 0 : revision,
                agentId: String(session.agentId || ''),
                sessionId: String(session.sessionId || ''),
            };
        }

        // Reads the *current* session: the request's own session is deliberately
        // not passed in, because that is the screen the answer would have to
        // repaint. If the user switched away, the capture no longer matches.
        function stillCurrent(captured) {
            if (!captured) return false;
            var session = sessionOf();
            var revision = getContextRevision();
            var capability = capabilitiesApi();
            if (capability && typeof capability.isCurrent === 'function') {
                return capability.isCurrent(captured, revision, session.agentId, session.sessionId);
            }
            return captured.contextRevision === (revision == null ? 0 : revision)
                && captured.agentId === String(session.agentId || '')
                && captured.sessionId === String(session.sessionId || '');
        }

        function renderState(panelValue) {
            stateEl.textContent = stateText(t, panelValue, state.error);
        }

        function renderFigure(panelValue) {
            var ready = panelValue === 'ready' && state.usage && state.usage.available === true;
            setHidden(figure, !ready);
            if (!ready) {
                figureMain.textContent = '';
                figureMeta.textContent = '';
                return;
            }
            var usage = state.usage;
            figureMain.textContent = t('context_used_of')
                .replace('{used}', String(usage.used))
                .replace('{limit}', String(usage.limit));
            var meta = t('context_messages').replace('{count}', String(usage.messages));
            if (usage.estimated) meta += ' · ' + t('context_estimated');
            figureMeta.textContent = meta;
        }

        function renderNotice() {
            noticeEl.textContent = state.noticeKey ? t(state.noticeKey) : '';
        }

        function renderError() {
            errorEl.textContent = '';
            if (!state.compactError) return;
            var key = errorKey(state.compactError.code, state.compactError.status);
            if (key !== 'context_error') {
                errorEl.textContent = t(key);
                return;
            }
            errorEl.textContent = trim(state.compactError.message)
                || t('context_compact_failed');
        }

        // The write entry is offered only while its action is open; whether the
        // read found a context is the server's answer, not a client guess.
        function renderActions(compactAvailable) {
            clear(actions);
            setHidden(actions, !compactAvailable);
            if (!compactAvailable) return;
            var compactBtn = button(doc, 'rdai-context-compact', t('context_act_compact'),
                                    function () { compactNow(); });
            compactBtn.disabled = !!state.compacting;
            actions.appendChild(compactBtn);
            if (state.compacting) {
                actions.appendChild(el(doc, 'span', 'rdai-context-compacting',
                                       t('context_compacting')));
            }
        }

        function render() {
            if (state.disposed) return;
            var session = sessionOf();
            var compactAvailable = featureAvailable(KEYS.COMPACT);
            var panelValue;
            if (!session.persisted) {
                // A brand-new session has no server record yet: there is no
                // context to read, so the panel says so and reads nothing.
                panelValue = 'unavailable';
            } else {
                panelValue = panelState({
                    actionAvailable: featureAvailable(KEYS.USAGE),
                    error: state.error,
                    usage: state.usage,
                    loading: state.loading,
                });
            }
            state.panel = panelValue;
            renderState(panelValue);
            renderFigure(panelValue);
            renderNotice();
            renderError();
            renderActions(compactAvailable);
        }

        function togglePanel() {
            state.open = !state.open;
            setHidden(panel, !state.open);
            if (state.open) loadUsage();
        }

        function loadUsage() {
            if (state.disposed) return Promise.resolve(null);
            var session = sessionOf();
            if (!session.persisted || !featureAvailable(KEYS.USAGE)) {
                // Closed, or no server-side session: render the state and issue
                // NO request (session-context-controls "关闭时不发轮询").
                state.loading = false;
                state.error = null;
                state.usage = null;
                render();
                return Promise.resolve(null);
            }
            var captured = captureFor(session);
            var seq = ++state.usageSeq;
            state.loading = true;
            state.error = null;
            render();
            return Promise.resolve()
                .then(function () {
                    return request(usagePath(session.sessionId, session.agentId));
                })
                .then(function (payload) {
                    if (state.disposed || seq !== state.usageSeq) return null;
                    if (!payload || payload.status !== 'success') {
                        throw failureFrom(payload, 'internal_error');
                    }
                    // The identity may have moved on while this read was in
                    // flight; leave the newer screen untouched.
                    if (!stillCurrent(captured)) return null;
                    state.usage = buildUsageView(payload);
                    state.loading = false;
                    state.error = null;
                    render();
                    return state.usage;
                })
                .catch(function (error) {
                    if (state.disposed || seq !== state.usageSeq) return null;
                    if (!stillCurrent(captured)) return null;
                    state.loading = false;
                    state.usage = null;
                    state.error = errorShape(error);
                    render();
                    return null;
                });
        }

        function compactOutcomeRender(outcome) {
            state.compacting = false;
            state.compactError = null;
            if (outcome.ok) {
                state.compactState = 'ready';
                state.noticeKey = outcome.key;
            } else if (outcome.noop) {
                state.compactState = 'noop';
                state.noticeKey = outcome.key;
            } else {
                state.compactState = 'error';
                state.noticeKey = '';
                state.compactError = { code: 'compact_failed', status: 0, message: '' };
            }
        }

        function doCompact(session) {
            var captured = captureFor(session);
            var req;
            try {
                req = buildCompactRequest(session.sessionId, session.agentId);
            } catch (error) {
                state.compacting = false;
                state.compactError = errorShape(error);
                state.compactState = 'error';
                state.noticeKey = '';
                render();
                return Promise.resolve(null);
            }
            state.compacting = true;
            state.compactError = null;
            state.compactState = 'loading';
            state.noticeKey = '';
            state.error = null;
            render();
            return Promise.resolve()
                .then(function () {
                    return request(req.path, { method: 'POST', body: req.body });
                })
                .then(function (payload) {
                    if (state.disposed) return null;
                    if (!payload || payload.status !== 'success') {
                        throw failureFrom(payload, 'internal_error');
                    }
                    if (!stillCurrent(captured)) {
                        state.compacting = false;
                        render();
                        return null;
                    }
                    var outcome = compactOutcome(payload);
                    compactOutcomeRender(outcome);
                    if (outcome.ok) {
                        // The server returns the refreshed usage with the
                        // commit; fall back to a fresh read when it does not.
                        if (payload.usage) state.usage = buildUsageView(payload.usage);
                        else loadUsage();
                    }
                    render();
                    return outcome;
                })
                .catch(function (error) {
                    if (state.disposed) return null;
                    if (!stillCurrent(captured)) {
                        state.compacting = false;
                        render();
                        return null;
                    }
                    state.compacting = false;
                    state.noticeKey = '';
                    state.compactError = errorShape(error);
                    // A 409 is its own state (retryable), never a generic
                    // failure; the panel state machine already knows that.
                    state.compactState = panelState({
                        actionAvailable: true, error: state.compactError,
                    });
                    render();
                    return null;
                });
        }

        function compactNow() {
            if (state.compacting || state.disposed) return Promise.resolve(null);
            var session = sessionOf();
            if (!session.persisted || !featureAvailable(KEYS.COMPACT)) {
                // The action is closed here, or there is no server-side session
                // to compact: do not fire a write that can only be refused.
                return Promise.resolve(null);
            }
            if (!confirmFn) return doCompact(session);
            return new Promise(function (resolve) {
                state.compacting = true;
                render();
                confirmFn({
                    title: t('context_compact_confirm_title'),
                    message: t('context_compact_confirm_msg'),
                    okText: t('context_act_compact'),
                    cancelText: t('cancel'),
                    onConfirm: function () { resolve(doCompact(session)); },
                    onCancel: function () {
                        state.compacting = false;
                        render();
                        resolve(null);
                    },
                });
            });
        }

        render();

        return {
            refresh: loadUsage,
            compact: compactNow,
            open: togglePanel,
            getState: function () {
                return {
                    usage: state.usage,
                    error: state.error,
                    loading: state.loading,
                    panel: state.panel,
                    compacting: state.compacting,
                    compactState: state.compactState,
                    compactError: state.compactError,
                    noticeKey: state.noticeKey,
                    open: state.open,
                };
            },
            dispose: function () {
                state.disposed = true;
                state.usageSeq += 1;
                clear(root);
            },
        };
    }

    var api = {
        KEYS: KEYS,
        buildUsageView: buildUsageView,
        panelState: panelState,
        errorKey: errorKey,
        buildCompactRequest: buildCompactRequest,
        usagePath: usagePath,
        compactOutcome: compactOutcome,
        mount: mount,
    };

    if (typeof window !== 'undefined') {
        window.RdaiFunctionalContext = api;
    }
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }
})();
