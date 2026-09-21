/* =====================================================================
 * Console page: coding sessions (coding agents)
 *
 * Change add-opencode-coding-agents, task groups 4.3 and 4.4. This is the
 * single platform module that owns the embedded OpenCode pane, exposed as
 * `window.CodingChat` and called from console.js at the agent form, the
 * workbench/new-chat entry and every place a conversation is opened.
 *
 * What it owns
 * ------------
 *   * the iframe that renders the coding session, and the chrome swap that
 *     hides the ordinary composer while it is up (and restores it exactly on
 *     the way out);
 *   * the parent half of the embed protocol: it accepts a notification only
 *     from the mounted frame's own origin *and* window object, under the
 *     channel that iframe was given, and registers a session the user created
 *     inside OpenCode through the platform's attach endpoint -- the client
 *     never claims a session, retitles it or grants anything on the strength
 *     of a message;
 *   * the refresh cadence: a 5s tick that walks the cached list by its stable
 *     cursor, paused while the page is hidden, deduplicated while a round is
 *     still in flight, and abandoned by generation when the page, Agent or
 *     identity changed underneath it.
 *
 * Deliberately not here
 * ---------------------
 * There is no general-purpose front-end message bus. Notifications are two
 * fixed shapes from one iframe, validated in `handleMessage`, and every
 * cross-module call is a plain function on the hooks object below (which is
 * what makes the whole thing testable without a browser).
 * ===================================================================== */
(function () {
    'use strict';

    if (typeof window === 'undefined') return;

    // =================================================================
    // Constants
    // =================================================================
    // The refresh cadence. 5s is a scheduling interval, not a latency SLA: the
    // promise is "visible by the next successful round on a healthy service".
    var REFRESH_INTERVAL_MS = 5000;

    // How long the pane waits for the child's ready notice before offering a
    // retry. The frame may well still come up after that; the retry affordance
    // is additive and never auto-clicks.
    var READY_TIMEOUT_MS = 15000;

    // Safety stop for the cursor walk. The server pages at 50; this only bounds
    // a pathological `next_cursor` that never terminates.
    var MAX_SYNC_BATCHES = 200;

    var EMBED_PARAM = 'rsm_embed';
    var PARENT_ORIGIN_PARAM = 'rsm_parent_origin';
    var CHANNEL_PARAM = 'rsm_channel';

    var READY = 'rsm.opencode.ready';
    var SESSION = 'rsm.opencode.session';

    // The controls that make no sense for a coding session: the model, the
    // workspace and the permission mode all belong to the normal runtime the
    // coding Agent does not use, and the team entry would start a conversation
    // this Agent cannot serve. The composer itself is hidden as a whole.
    var HIDDEN_SELECTORS = [
        '#chat-messages',
        '#chat-input-area',
        '#scroll-to-bottom-btn',
        '[data-coding-hide]',
    ];

    // =================================================================
    // i18n + escaping (same shape as the other console modules)
    // =================================================================
    function codingT(key, vars) {
        var text = (typeof window.t === 'function') ? window.t(key) : key;
        if (text === undefined || text === null || text === '') text = key;
        if (vars) {
            Object.keys(vars).forEach(function (name) {
                text = text.split('{' + name + '}').join(String(vars[name]));
            });
        }
        return text;
    }

    // =================================================================
    // Hooks: everything the module needs from its host. Defaults are the
    // production behaviour; tests replace them and read the calls back.
    // =================================================================
    function defaultRequest(path, options) {
        var opts = options || {};
        var init = { method: opts.method || 'GET', headers: {} };
        if (opts.body !== undefined) {
            init.headers['Content-Type'] = 'application/json';
            init.body = JSON.stringify(opts.body);
        }
        return fetch(path, init).then(function (response) {
            return response.json().then(function (data) {
                return { ok: response.ok, status: response.status, data: data };
            }, function () {
                return { ok: response.ok, status: response.status, data: {} };
            });
        });
    }

    function defaultNotify(message) {
        if (typeof window.showNotification === 'function') {
            window.showNotification(message);
            return;
        }
        if (typeof window._wsToast === 'function') window._wsToast(message);
    }

    function defaultRedrawList() {
        // The platform already redraws its own list; a refresh only has to ask
        // it to re-read, so sorting, search and paging keep their behaviour.
        if (typeof window.loadSidebarRecentSessions === 'function') {
            window.loadSidebarRecentSessions();
        }
        if (typeof window.loadHistory === 'function') window.loadHistory(1);
    }

    function defaultConfirmLeave(proceed) {
        if (typeof window.wsGuardUnsaved === 'function') {
            return window.wsGuardUnsaved(proceed);
        }
        proceed();
        return true;
    }

    var hooks = {
        request: defaultRequest,
        notify: defaultNotify,
        redrawList: defaultRedrawList,
        confirmLeave: defaultConfirmLeave,
        // Console.js points this at the code that brings the ordinary chat pane
        // back (it owns which view is showing); the module only says whether
        // coding chrome is on.
        onLeave: null,
        // Console.js points this at its own "select this conversation" so a
        // newly attached session becomes the selected history row.
        onLinked: null,
        // Injected for tests; see `_test`.
        now: function () { return Date.now(); },
        setTimer: function (fn, ms) { return window.setTimeout(fn, ms); },
        clearTimer: function (id) { window.clearTimeout(id); },
        randomId: null,
    };

    function setHooks(next) {
        if (!next) return;
        Object.keys(next).forEach(function (name) {
            if (next[name] !== undefined) hooks[name] = next[name];
        });
    }

    // =================================================================
    // State
    // =================================================================
    // The mounted coding pane, or null when the ordinary chat pane is showing.
    var mount = null;

    // The refresh loop. ``generation`` is bumped whenever the thing being
    // refreshed changes (a different session, a different Agent, leaving the
    // pane, a reload), so a late response can tell it is no longer wanted.
    var refresh = { timer: null, inFlight: null, generation: 0, paused: false };

    function newRequestId() {
        if (typeof hooks.randomId === 'function') return hooks.randomId();
        try {
            if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID();
        } catch (error) {
            // Fall through to the counter below.
        }
        newRequestId.counter = (newRequestId.counter || 0) + 1;
        return 'coding-' + Date.now().toString(36) + '-' + newRequestId.counter;
    }

    function originOf(url) {
        try {
            return new URL(url, window.location && window.location.href).origin;
        } catch (error) {
            return '';
        }
    }

    function isActive() {
        return !!(mount && mount.frame);
    }

    function current() {
        if (!mount) return null;
        return {
            agent_id: mount.agentId,
            session_id: mount.sessionId,
            external_session_id: mount.externalId,
            channel: mount.channel,
            ready: mount.ready,
        };
    }

    // =================================================================
    // The embed protocol: notifications from the mounted frame
    // =================================================================
    // Only the two fixed shapes are recognised, and only with a channel: a
    // message the parent cannot attribute to the frame it mounted is dropped.
    function parseNotification(data) {
        if (!data || typeof data !== 'object') return null;
        if (data.type !== READY && data.type !== SESSION) return null;
        var channel = typeof data.channel === 'string' ? data.channel : '';
        if (!channel) return null;
        return {
            type: data.type,
            channel: channel,
            sessionID: typeof data.session_id === 'string' ? data.session_id : '',
        };
    }

    // A message is trusted only when all four agree: this pane is mounted, the
    // sender is the frame's own origin, the sender *is* the frame (an origin is
    // shared by everything on that host), and the channel is this mount's. A
    // stale iframe -- the previous session's, still unloading -- therefore
    // cannot register anything.
    function handleMessage(event) {
        if (!mount || !mount.frame) return false;
        if (!event || event.origin !== mount.origin) return false;
        if (event.source && mount.frame.contentWindow && event.source !== mount.frame.contentWindow) return false;
        var note = parseNotification(event.data);
        if (!note || note.channel !== mount.channel) return false;

        if (note.type === READY) {
            markReady();
            return true;
        }
        // The session the user is looking at changed inside OpenCode: a
        // navigation, a fork, or a session started in its own UI.
        if (!note.sessionID || note.sessionID === mount.externalId) return true;
        void attachSession(note.sessionID);
        return true;
    }

    // Register a session first seen upstream. Duplicate deliveries of the same
    // notification are ignored: the id is remembered while its request is in
    // flight and once it succeeded, and forgotten only on failure so a genuine
    // retry is still possible.
    function attachSession(externalSessionID) {
        if (!mount) return Promise.resolve(false);
        if (mount.attachSent[externalSessionID]) return Promise.resolve(false);
        mount.attachSent[externalSessionID] = true;
        var generation = mount.generation;
        hooks.notify(codingT('coding_linked'));
        return Promise.resolve(hooks.request(
            '/api/coding/sessions/attach', {
                method: 'POST',
                body: {
                    agent_id: mount.agentId,
                    source_session_id: mount.sessionId,
                    external_session_id: externalSessionID,
                },
            },
        )).then(function (result) {
            if (generation !== mount.generation) return false;
            if (!result || !result.ok) {
                delete mount.attachSent[externalSessionID];
                hooks.notify(codingT('coding_attach_failed', {
                    reason: reasonOf(result),
                }));
                return false;
            }
            var data = result.data || {};
            // The registration only makes the new session selectable: the frame
            // is already showing it, so it is never reloaded here (that would
            // loop), and nothing about title or grants is taken from it.
            mount.externalId = data.external_session_id || externalSessionID;
            mount.sessionId = data.session_id || mount.sessionId;
            hooks.notify(codingT('coding_linked_ok'));
            if (typeof hooks.onLinked === 'function') hooks.onLinked(data);
            hooks.redrawList();
            return true;
        });
    }

    function reasonOf(result) {
        if (result && result.data && typeof result.data.message === 'string' && result.data.message) {
            return result.data.message;
        }
        return result && result.status ? String(result.status) : '';
    }

    // =================================================================
    // Mounting the frame and swapping the chrome
    // =================================================================
    function containers() {
        return {
            host: document.getElementById('chat-main'),
            messages: document.getElementById('chat-messages'),
            composer: document.getElementById('chat-input-area'),
        };
    }

    function frameUrl(payload, channel) {
        var url = payload.iframe_url;
        if (!url) return '';
        // The service's own ``session_url`` already marks the session as an
        // embed, and its docstring is explicit that only the page's own
        // properties belong here. Add the marker only when it is missing, so a
        // URL that arrives marked is not handed to the child twice.
        var marker = EMBED_PARAM + '=1';
        var head = url;
        var params = [PARENT_ORIGIN_PARAM + '=' + encodeURIComponent(window.location.origin),
            CHANNEL_PARAM + '=' + encodeURIComponent(channel)];
        if (url.indexOf(EMBED_PARAM + '=') < 0) {
            params.unshift(marker);
        }
        var separator = head.indexOf('?') >= 0 ? '&' : '?';
        return head + separator + params.join('&');
    }

    function applyChrome(active) {
        var nodes = document.querySelectorAll(HIDDEN_SELECTORS.join(','));
        for (var index = 0; index < nodes.length; index += 1) {
            var node = nodes[index];
            if (active) {
                if (!node.hasAttribute('data-coding-was-hidden')) {
                    node.setAttribute('data-coding-was-hidden',
                        node.classList.contains('hidden') ? 'hidden' : '');
                }
                node.classList.add('hidden');
            } else {
                var was = node.getAttribute('data-coding-was-hidden');
                node.removeAttribute('data-coding-was-hidden');
                if (was === 'hidden') continue;
                node.classList.remove('hidden');
            }
        }
        var main = document.getElementById('chat-main');
        if (main) {
            main.classList.toggle('coding-active', !!active);
            // A mounted pane owns the conversation area, but ``chat-home`` says
            // the console is showing an empty chat: it centres a hero, pads the
            // pane's top and makes the message list ``display: contents``. The
            // welcome markup that switches it on is only removed when an
            // ordinary message renders, and a coding conversation has none, so
            // reopening one from history left both classes on -- the frame then
            // shared the column with the welcome markup and collapsed to a strip
            // behind a loading notice that could never clear. Dropped here, and
            // restored only if it was this module that dropped it, so a
            // conversation that was already open stays open.
            if (active) {
                if (main.classList.contains('chat-home')) {
                    main.setAttribute('data-coding-was-home', '');
                    main.classList.remove('chat-home');
                }
            } else if (main.hasAttribute('data-coding-was-home')) {
                main.removeAttribute('data-coding-was-home');
                main.classList.add('chat-home');
            }
        }
    }

    function markReady() {
        if (!mount) return;
        mount.ready = true;
        if (mount.readyTimer !== null) {
            hooks.clearTimer(mount.readyTimer);
            mount.readyTimer = null;
        }
        removeRetry();
    }

    // The load state, and the retry offered only after the child stayed silent:
    // a frame that is merely slow is indistinguishable from a broken one, so
    // the pane says what it knows and offers the retry *without* auto-clicking.
    function showLoading() {
        if (!mount || !mount.host) return;
        if (!mount.loadingEl) {
            mount.loadingEl = document.createElement('div');
            mount.loadingEl.className = 'coding-loading';
            mount.loadingEl.setAttribute('role', 'status');
            mount.loadingEl.textContent = codingT('coding_loading');
            mount.host.appendChild(mount.loadingEl);
        }
        mount.readyTimer = hooks.setTimer(function () {
            mount.readyTimer = null;
            showRetry();
        }, READY_TIMEOUT_MS);
    }

    function showRetry() {
        if (!mount || !mount.host || mount.retryEl) return;
        var box = document.createElement('div');
        box.className = 'coding-retry';
        var text = document.createElement('span');
        // Not "the service is down" -- nothing said that. It says what is true:
        // the pane is waiting, and here is the one safe action.
        text.textContent = codingT('coding_not_ready') + ' ';
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'coding-retry-btn';
        button.textContent = codingT('coding_retry');
        button.setAttribute('title', codingT('coding_retry_hint'));
        button.addEventListener('click', function () { void retry(); });
        box.appendChild(text);
        box.appendChild(button);
        mount.host.appendChild(box);
        mount.retryEl = box;
    }

    function removeRetry() {
        if (!mount) return;
        if (mount.loadingEl) {
            mount.loadingEl.remove();
            mount.loadingEl = null;
        }
        if (mount.retryEl) {
            mount.retryEl.remove();
            mount.retryEl = null;
        }
    }

    function unmountFrame() {
        if (!mount) return;
        if (mount.readyTimer !== null) {
            hooks.clearTimer(mount.readyTimer);
            mount.readyTimer = null;
        }
        if (mount.frame) {
            // Stop the app; it is not asked to abort any running work (the
            // design keeps cancellation on the OpenCode side).
            mount.frame.remove();
        }
        removeRetry();
        if (mount.host) mount.host.classList.remove('coding-active');
    }

    function installFrame(payload, agentId, requestId) {
        var host = document.getElementById('chat-main');
        if (!host) return false;
        if (!payload.iframe_url) {
            hooks.notify(codingT('coding_open_failed', { reason: reasonOf({}) }));
            return false;
        }
        // Replace whatever was mounted: opening another session must not leave
        // the previous frame alive behind the new one.
        var generation = (mount ? mount.generation : 0) + 1;
        if (mount) unmountFrame();
        var channel = 'ch-' + Date.now().toString(36) + '-'
            + Math.random().toString(36).slice(2, 10);
        mount = {
            agentId: agentId,
            sessionId: payload.session_id,
            externalId: payload.external_session_id || '',
            channel: channel,
            origin: originOf(payload.iframe_url),
            frame: null,
            host: host,
            ready: false,
            readyTimer: null,
            loadingEl: null,
            retryEl: null,
            requestId: requestId || '',
            attachSent: {},
            generation: generation,
        };
        var frame = document.createElement('iframe');
        frame.className = 'coding-frame';
        frame.setAttribute('title', codingT('coding_type_coding'));
        // The platform's own origin is the only allowed ancestor; the child
        // asserts the same on its side.
        frame.setAttribute('allow', 'clipboard-write');
        frame.src = frameUrl(payload, channel);
        mount.frame = frame;
        applyChrome(true);
        host.appendChild(frame);
        showLoading();
        startRefresh();
        // The described session, not a bare `true`: the console needs the
        // platform session id the service just confirmed.
        return payload;
    }

    // =================================================================
    // Public actions
    // =================================================================
    // Open an existing coding session. A GET never creates remote state, so a
    // reservation the server reports as retryable is *not* silently completed
    // here; the caller resumes it through `launch` with the same request id.
    function open(agentId, sessionId) {
        if (!agentId || !sessionId) return Promise.resolve(false);
        return Promise.resolve(hooks.request(
            '/api/coding/sessions/' + encodeURIComponent(sessionId)
                + '/open?agent_id=' + encodeURIComponent(agentId),
            { method: 'GET' },
        )).then(function (result) {
            if (!result || !result.ok) {
                hooks.notify(codingT('coding_open_failed', { reason: reasonOf(result) }));
                return false;
            }
            return installFrame(result.data || {}, agentId, '');
        });
    }

    // Create the session for a click, or resume the one that click already
    // created. The request id is what makes the retry safe: the platform
    // derives the session id from it, so the same id always maps to the same
    // session and a retry can never start a second conversation.
    function launch(agentId, projectDir, requestId) {
        if (!agentId) return Promise.resolve(false);
        var rid = requestId || newRequestId();
        return Promise.resolve(hooks.request('/api/coding/sessions', {
            method: 'POST',
            body: { agent_id: agentId, request_id: rid },
        })).then(function (result) {
            if (!result || !result.ok) {
                hooks.notify(codingT('coding_open_failed', { reason: reasonOf(result) }));
                return false;
            }
            return installFrame(result.data || {}, agentId, rid);
        });
    }

    // The retry affordance: only ever reopens *this* session. With the stored
    // request id it resumes the same reservation; without one (a session opened
    // from history) it is a plain re-open.
    function retry() {
        if (!mount) return Promise.resolve(false);
        var agentId = mount.agentId;
        var requestId = mount.requestId;
        var sessionId = mount.sessionId;
        var generation = mount.generation;
        if (requestId) {
            unmountFrame();
            return launch(agentId, '', requestId).then(function (ok) {
                if (!ok && generation === (mount ? mount.generation : -1)) {
                    hooks.notify(codingT('coding_unavailable'));
                }
                return ok;
            });
        }
        return open(agentId, sessionId);
    }

    // One refresh round: walk the cached list by its stable cursor, then let
    // the platform redraw its own list. Concurrent calls collapse into the one
    // in flight, and a round whose generation moved on is discarded.
    function refreshNow() {
        if (!mount) return Promise.resolve(false);
        if (refresh.inFlight) return refresh.inFlight;
        var generation = refresh.generation;
        var agentId = mount.agentId;
        refresh.inFlight = Promise.resolve().then(function () {
            var cursor = null;
            var batches = 0;
            function step() {
                return Promise.resolve(hooks.request('/api/coding/sessions/sync', {
                    method: 'POST',
                    body: { agent_id: agentId, cursor: cursor },
                })).then(function (result) {
                    if (generation !== refresh.generation) return false;
                    // Unreachable is not an error the user must see: the cached
                    // rows stay exactly as they are and the next round retries.
                    if (!result || !result.ok) return false;
                    var data = result.data || {};
                    cursor = data.next_cursor || null;
                    batches += 1;
                    if (cursor && batches < MAX_SYNC_BATCHES) return step();
                    hooks.redrawList();
                    return true;
                });
            }
            return step();
        }).then(function (outcome) {
            refresh.inFlight = null;
            return outcome;
        }, function () {
            refresh.inFlight = null;
            return false;
        });
        return refresh.inFlight;
    }

    function startRefresh() {
        stopRefresh();
        refresh.generation += 1;
        refresh.timer = hooks.setTimer(function tick() {
            refresh.timer = hooks.setTimer(tick, REFRESH_INTERVAL_MS);
            if (refresh.paused) return;
            void refreshNow();
        }, REFRESH_INTERVAL_MS);
    }

    function stopRefresh() {
        if (refresh.timer !== null) {
            hooks.clearTimer(refresh.timer);
            refresh.timer = null;
        }
    }

    // Hidden means "stop asking": the timer stays but does no work, and the
    // first visible moment refreshes once immediately rather than waiting out
    // the interval.
    function setVisibility(visible) {
        refresh.paused = !visible;
        if (!visible || !mount) return;
        void refreshNow();
    }

    // Leave coding mode: tear the frame down and put the ordinary pane back.
    // Switching between a coding and a normal conversation goes through the
    // console's own leave confirmation first, so this never discards an open
    // editor behind the user's back.
    function leave(options) {
        var opts = options || {};
        if (!mount) return false;
        if (!opts.skipConfirm && typeof hooks.confirmLeave === 'function') {
            if (hooks.confirmLeave(function () {}) === false) return false;
        }
        unmountFrame();
        mount = null;
        stopRefresh();
        refresh.generation += 1;
        applyChrome(false);
        if (typeof hooks.onLeave === 'function') hooks.onLeave();
        return true;
    }

    function _reset() {
        unmountFrame();
        mount = null;
        stopRefresh();
        refresh.inFlight = null;
        refresh.generation += 1;
    }

    // =================================================================
    // Export
    // =================================================================
    window.CodingChat = {
        launch: launch,
        open: open,
        refresh: refreshNow,
        leave: leave,
        retry: retry,
        isActive: isActive,
        current: current,
        setVisibility: setVisibility,
        // The message handler is a plain function so the parent-side rules can
        // be driven directly, and so console.js can install it once.
        handleMessage: handleMessage,
        parseNotification: parseNotification,
        // Wiring for console.js and for tests.
        setHooks: setHooks,
        _reset: _reset,
        _state: function () {
            return {
                mounted: isActive(),
                agentId: mount ? mount.agentId : '',
                sessionId: mount ? mount.sessionId : '',
                channel: mount ? mount.channel : '',
                ready: mount ? mount.ready : false,
                paused: refresh.paused,
                requests: refresh.inFlight !== null,
            };
        },
        constants: {
            REFRESH_INTERVAL_MS: REFRESH_INTERVAL_MS,
            READY_TIMEOUT_MS: READY_TIMEOUT_MS,
            HIDDEN_SELECTORS: HIDDEN_SELECTORS.slice(),
        },
        source: 'channel/web/static/js/coding.js',
    };
}());
