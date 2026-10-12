/* =====================================================================
 * MCP editor (change align-mcp-editor-with-master)
 *
 * The centered MCP editor of the fixed master baseline, extracted from
 * `static/js/views/skills.js` and wired to the *current* System Access page
 * instead of the global `/api/mcp/servers` file store.
 *
 * Why a separate module rather than loading `views/skills.js`:
 *   `skills.js` declares globals (e.g. `renderMcpCard`) that collide with the
 *   console's own symbols, and the page never used the rest of that view. Only
 *   the MCP functions are extracted here, keeping the upstream parsing,
 *   form/JSON switching and result rendering, and attaching exactly one window
 *   namespace (`window.EcMcpEditor`).
 *
 * Boundary: this module owns the modal's markup and interaction only. It never
 * calls the connection API. The page passes `test`/`save` callbacks and this
 * module drives them, so scope authorization, credential slots and the
 * connection contract stay in `external-connections.js` where they already
 * live. Secrets typed here never leave the open modal except through `save`,
 * and are cleared on close.
 * ===================================================================== */
(function () {
    'use strict';

    if (typeof window === 'undefined') return;

    function t(key) {
        var text = (typeof window.t === 'function') ? window.t(key) : key;
        return (text === undefined || text === null || text === '') ? key : text;
    }

    function esc(value) {
        if (typeof window.escapeHtml === 'function') return window.escapeHtml(value);
        return String(value === null || value === undefined ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function el(id) { return document.getElementById(id); }

    function newIdempotencyKey() {
        try {
            if (window.crypto && typeof window.crypto.randomUUID === 'function') {
                return window.crypto.randomUUID();
            }
        } catch (_) { /* fall through */ }
        return 'mcp-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
    }

    // ---- master key/value helpers -------------------------------------
    function kvToObject(text) {
        var out = {};
        String(text || '').split(/\r?\n/).forEach(function (line) {
            var trimmed = line.trim();
            if (!trimmed) return;
            var idx = trimmed.indexOf('=');
            if (idx <= 0) return;
            out[trimmed.slice(0, idx).trim()] = trimmed.slice(idx + 1);
        });
        return out;
    }

    function objectToKv(obj) {
        if (!obj || typeof obj !== 'object') return '';
        return Object.keys(obj).map(function (k) { return k + '=' + obj[k]; }).join('\n');
    }

    // ---- transport vocabulary -----------------------------------------
    var TRANSPORT_LABELS = { stdio: 'stdio', sse: 'SSE', 'streamable-http': 'HTTP' };

    function transportOptions() {
        return [
            { value: 'stdio', label: 'stdio', hint: t('mcp_transport_stdio_hint') },
            { value: 'sse', label: 'SSE', hint: t('mcp_transport_remote_hint') },
            { value: 'streamable-http', label: 'Streamable HTTP', hint: t('mcp_transport_remote_hint') },
        ];
    }

    function currentTransport() {
        var node = el('mcp-field-type');
        if (window.getDropdownValue) return window.getDropdownValue(node) || 'stdio';
        return 'stdio';
    }

    // The state the modal is driven by. `options` is replaced on every open;
    // `items` is the per-item working set (one entry for an edit/override).
    var state = {
        open: false,
        bound: false,
        tab: 'form',
        saveAnyway: false,
        busy: false,
        advancedOpen: false,
        options: null,
        items: [],
        // Names already created in this open editor, so a retry after a partial
        // batch does not resubmit a server that succeeded. Reset on every open.
        saved: {},
        epoch: 0,
        // Which secret groups the user has actually edited in this session. A
        // group that was never touched must be left alone on save (the stored
        // credential stays); a touched-and-emptied group is an explicit clear.
        touched: { env: false, headers: false },
    };

    function context() {
        var o = state.options || {};
        return {
            mode: o.mode || 'create',
            scope: o.scope || '',
            connectionId: o.connectionId || null,
            version: o.version === undefined ? null : o.version,
            baseConnectionId: o.baseConnectionId || null,
            name: o.name || '',
            touched: { env: !!state.touched.env, headers: !!state.touched.headers },
        };
    }

    function isCurrent(epoch) {
        if (!state.open || epoch !== state.epoch) return false;
        var o = state.options;
        if (o && typeof o.isCurrent === 'function' && !o.isCurrent()) return false;
        return true;
    }

    // ---- form fields --------------------------------------------------
    function syncTransport() {
        setSaveAnyway(false);
        var stdio = currentTransport() === 'stdio';
        var stdioFields = el('mcp-stdio-fields');
        var urlFields = el('mcp-url-fields');
        var scopeField = el('mcp-scope-field');
        if (stdioFields) stdioFields.classList.toggle('hidden', !stdio);
        if (urlFields) urlFields.classList.toggle('hidden', stdio);
        if (scopeField) scopeField.classList.toggle('hidden', stdio);
    }

    function setAdvancedOpen(open) {
        state.advancedOpen = !!open;
        var fields = el('mcp-advanced-fields');
        if (fields) fields.classList.toggle('hidden', !open);
        var icon = el('mcp-advanced-icon');
        if (icon) icon.style.transform = open ? 'rotate(90deg)' : '';
    }

    function fillForm(server) {
        var s = server || {};
        var nameEl = el('mcp-field-name');
        if (nameEl) {
            nameEl.value = s.name || '';
            nameEl.disabled = !!s.name && (state.options && state.options.lockName !== false);
        }
        var type = s.type || (s.url ? 'sse' : 'stdio');
        if (window.initDropdown) {
            window.initDropdown(el('mcp-field-type'), transportOptions(), type, syncTransport);
        }
        if (el('mcp-field-command')) el('mcp-field-command').value = s.command || '';
        if (el('mcp-field-args')) el('mcp-field-args').value = (s.args || []).join('\n');
        if (el('mcp-field-env')) el('mcp-field-env').value = objectToKv(s.env);
        if (el('mcp-field-url')) el('mcp-field-url').value = s.url || '';
        if (el('mcp-field-headers')) el('mcp-field-headers').value = objectToKv(s.headers);
        if (el('mcp-field-scope')) el('mcp-field-scope').value = s.scope || '';
        if (el('mcp-field-prefix')) el('mcp-field-prefix').value = s.tool_name_prefix || '';
        if (el('mcp-field-timeout')) el('mcp-field-timeout').value = s.timeout || '';
        if (el('mcp-field-disabled')) el('mcp-field-disabled').checked = !!s.disabled;
        setAdvancedOpen(!!(s.tool_name_prefix || s.timeout || s.scope));
        syncTransport();
    }

    function readForm() {
        var type = currentTransport();
        var cfg = { name: el('mcp-field-name') ? el('mcp-field-name').value.trim() : '', type: type };
        var prefix = el('mcp-field-prefix') ? el('mcp-field-prefix').value : '';
        if (prefix) cfg.tool_name_prefix = prefix;
        if (el('mcp-field-disabled') && el('mcp-field-disabled').checked) cfg.disabled = true;
        var timeout = el('mcp-field-timeout') ? el('mcp-field-timeout').value.trim() : '';
        if (timeout) cfg.timeout = Number(timeout);
        if (type === 'stdio') {
            cfg.command = el('mcp-field-command') ? el('mcp-field-command').value.trim() : '';
            var args = (el('mcp-field-args') ? el('mcp-field-args').value : '')
                .split(/\r?\n/).map(function (s) { return s.trim(); }).filter(Boolean);
            if (args.length) cfg.args = args;
            var env = kvToObject(el('mcp-field-env') ? el('mcp-field-env').value : '');
            if (Object.keys(env).length) cfg.env = env;
        } else {
            cfg.url = el('mcp-field-url') ? el('mcp-field-url').value.trim() : '';
            var headers = kvToObject(el('mcp-field-headers') ? el('mcp-field-headers').value : '');
            if (Object.keys(headers).length) cfg.headers = headers;
            var scope = el('mcp-field-scope') ? el('mcp-field-scope').value.trim() : '';
            if (scope) cfg.scope = scope;
        }
        return cfg;
    }

    // ---- form <-> JSON -------------------------------------------------
    /** A server as it is written in mcp.json: no name, no runtime fields, no empty values. */
    function configForJson(cfg) {
        var out = {};
        Object.keys(cfg || {}).forEach(function (key) {
            var value = cfg[key];
            if (key === 'name' || key === 'status') return;
            if (key === 'type' && value === 'stdio') return;
            if (value === '' || value === null || value === undefined || value === false) return;
            if (Array.isArray(value) && !value.length) return;
            if (typeof value === 'object' && !Array.isArray(value) && !Object.keys(value).length) return;
            out[key] = value;
        });
        return out;
    }

    function serversToJson(servers) {
        var map = {};
        (servers || []).forEach(function (s) { map[s.name || 'my-server'] = configForJson(s); });
        return JSON.stringify({ mcpServers: map }, null, 2);
    }

    //: The fields this editor carries end to end, from the JSON pane to the
    //: connection store. Master's parser ignores what it does not recognise,
    //: which would submit a config silently missing a field the user pasted;
    //: this page names the offending field instead, per the batch/JSON spec.
    var SERVER_FIELDS = ['name', 'type', 'transport', 'command', 'args', 'env', 'url',
        'serverUrl', 'headers', 'scope', 'oauth_scope', 'tool_name_prefix', 'timeout',
        'disabled'];

    function refuseUnknownFields(cfg, label) {
        Object.keys(cfg).forEach(function (key) {
            if (SERVER_FIELDS.indexOf(key) !== -1) return;
            throw new Error(t('mcp_json_unknown_field')
                .replace('{name}', label || '')
                .replace('{field}', key));
        });
    }

    /**
     * Read the JSON pane. Accepts the {"mcpServers": {...}} file format that MCP
     * directories publish, a bare {name: config} map, a list, or one config that
     * carries its own "name".
     */
    function parseJson(text) {
        var raw = String(text || '').trim();
        if (!raw) throw new Error(t('mcp_json_empty'));
        var obj;
        try {
            obj = JSON.parse(raw);
        } catch (err) {
            throw new Error(t('mcp_json_invalid') + ': ' + err.message);
        }
        var fromMap = function (map) {
            return Object.keys(map).map(function (name) {
                var cfg = map[name];
                if (!cfg || typeof cfg !== 'object' || Array.isArray(cfg)) {
                    throw new Error(t('mcp_json_invalid') + ': ' + name);
                }
                var copy = {};
                Object.keys(cfg).forEach(function (k) { copy[k] = cfg[k]; });
                copy.name = name;
                return copy;
            });
        };
        var servers;
        if (Array.isArray(obj)) {
            servers = obj.map(function (s) { return s; });
        } else if (obj && typeof obj === 'object') {
            if (obj.mcpServers && typeof obj.mcpServers === 'object') {
                servers = fromMap(obj.mcpServers);
            } else if ('command' in obj || 'url' in obj || 'serverUrl' in obj) {
                if (!obj.name) throw new Error(t('mcp_json_need_name'));
                servers = [obj];
            } else {
                servers = fromMap(obj);
            }
        } else {
            throw new Error(t('mcp_json_invalid'));
        }
        servers = servers.map(function (s, index) {
            var cfg = {};
            Object.keys(s).forEach(function (k) { cfg[k] = s[k]; });
            refuseUnknownFields(cfg, cfg.name || ('#' + (index + 1)));
            if (!cfg.url && cfg.serverUrl) cfg.url = cfg.serverUrl;
            delete cfg.serverUrl;
            // The deployment's own vocabulary calls the transport `transport`,
            // master's calls it `type`; either is honoured. A config that names
            // neither is stdio unless it carries an address -- the same
            // inference the form makes, so a pasted `mcp.json` entry is never
            // submitted as a remote transport it did not ask for.
            var transport = cfg.type || cfg.transport;
            delete cfg.transport;
            // `streamable_http` is the registry's spelling of the transport the
            // dropdown, the form and the JSON pane all call `streamable-http`;
            // the boundary maps them, so the editor keeps one vocabulary.
            if (transport === 'streamable_http') transport = 'streamable-http';
            cfg.type = transport || (cfg.url ? 'sse' : 'stdio');
            if (!cfg.scope && cfg.oauth_scope) cfg.scope = cfg.oauth_scope;
            delete cfg.oauth_scope;
            return cfg;
        });
        if (!servers.length) throw new Error(t('mcp_json_empty'));
        return servers;
    }

    /** The servers the editor currently describes, whichever tab is showing. */
    function readServers() {
        if (state.tab === 'json') {
            var servers = parseJson(el('mcp-field-json').value);
            if (state.options && state.options.mode !== 'create' && servers.length > 1) {
                throw new Error(t('mcp_json_edit_single'));
            }
            return servers;
        }
        var cfg = readForm();
        if (!cfg.name) throw new Error(t('mcp_name_required'));
        return [cfg];
    }

    function showTab(tab) {
        state.tab = tab;
        Array.prototype.slice.call(document.querySelectorAll('[data-mcp-tab]')).forEach(function (btn) {
            btn.classList.toggle('active', btn.getAttribute('data-mcp-tab') === tab);
        });
        if (el('mcp-pane-form')) el('mcp-pane-form').classList.toggle('hidden', tab !== 'form');
        if (el('mcp-pane-json')) el('mcp-pane-json').classList.toggle('hidden', tab !== 'json');
    }

    function switchTab(tab) {
        if (tab === state.tab) return;
        var jsonEl = el('mcp-field-json');
        if (tab === 'json') {
            var cfg = readForm();
            if (cfg.name || cfg.command || cfg.url) jsonEl.value = serversToJson([cfg]);
        } else if (jsonEl.value.trim()) {
            var servers;
            try {
                servers = parseJson(jsonEl.value);
            } catch (err) {
                showError(err.message);
                return;
            }
            if (servers.length > 1) {
                showError(t('mcp_json_multi_to_form'));
                return;
            }
            var next = servers[0];
            if (state.options && state.options.name) next.name = state.options.name;
            fillForm(next);
        }
        clearResult();
        showTab(tab);
        if (tab === 'json') jsonEl.focus();
    }

    // ---- result box ----------------------------------------------------
    function clearResult() {
        setSaveAnyway(false);
        var result = el('mcp-test-result');
        if (!result) return;
        result.className = 'cap-result hidden mt-4';
        result.innerHTML = '';
    }

    function showResult(kind, html) {
        var result = el('mcp-test-result');
        if (!result) return;
        result.className = 'cap-result ' + kind + ' mt-4';
        result.innerHTML = html;
        if (result.scrollIntoView) result.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }

    function showError(message) {
        showResult('fail', '<div class="flex items-start gap-2">'
            + '<i class="fas fa-circle-exclamation mt-0.5"></i>'
            + '<span class="flex-1 min-w-0 break-words">' + esc(message) + '</span></div>');
    }

    function renderToolList(tools) {
        if (!tools || !tools.length) {
            return '<p class="mt-2 text-xs opacity-80">' + esc(t('mcp_test_no_tools')) + '</p>';
        }
        var chips = tools.map(function (tool) {
            return '<span class="cap-chip" title="' + esc(tool.description || tool.name) + '">'
                + esc(tool.name) + '</span>';
        }).join('');
        return '<p class="mt-2.5 mb-1.5 text-xs font-medium opacity-80">'
            + esc(t('mcp_test_tools')) + ' (' + tools.length + ')</p>'
            + '<div class="flex flex-wrap gap-1.5 max-h-40 overflow-y-auto">' + chips + '</div>';
    }

    /** One probe's outcome. In a mixed batch the box is neutral, each icon carries its own color. */
    function renderProbe(probe, withName, mixed) {
        var tools = (probe.tools || []).filter(function (x) { return x && x.name; });
        var name = withName
            ? '<span class="font-mono">' + esc(probe.name) + '</span><span class="opacity-50">·</span>'
            : '';
        if (probe.ok) {
            var color = mixed ? 'text-emerald-500' : '';
            return '<div class="flex items-center gap-2 font-medium">'
                + '<i class="fas fa-circle-check ' + color + '"></i>' + name
                + '<span>' + esc(t('mcp_test_ok')) + '</span></div>' + renderToolList(tools);
        }
        var failColor = mixed ? 'text-red-500' : '';
        var detail = probe.needs_auth ? t('mcp_test_needs_auth') : (probe.error || '');
        return '<div class="flex items-center gap-2 font-medium">'
            + '<i class="fas fa-circle-xmark ' + failColor + '"></i>' + name
            + '<span>' + esc(t('mcp_test_fail')) + '</span></div>'
            + (detail ? '<p class="mt-1.5 text-xs break-words opacity-90">' + esc(detail) + '</p>' : '');
    }

    function renderResultList(items) {
        var multi = items.length > 1;
        var okCount = items.filter(function (i) { return i.probe && i.probe.ok; }).length;
        var mixed = okCount > 0 && okCount < items.length;
        var html = items.map(function (item) {
            var cls = multi ? 'py-2 first:pt-0 last:pb-0' : '';
            return '<div class="' + cls + '">' + renderProbe(item.probe || {}, multi, mixed) + '</div>';
        }).join(multi ? '<div class="border-t border-current opacity-10"></div>' : '');
        showResult(mixed ? 'info' : (okCount ? 'ok' : 'fail'), html);
    }

    function appendNote(message) {
        var result = el('mcp-test-result');
        if (!result) return;
        result.insertAdjacentHTML('beforeend',
            '<div class="mt-2.5 mb-2 border-t border-current opacity-10"></div>'
            + '<p class="text-xs font-medium">' + esc(message) + '</p>');
        if (result.scrollIntoView) result.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }

    // ---- save-anyway state --------------------------------------------
    /** After a failed check, the next save skips it: the user saw why and chose to keep it. */
    function setSaveAnyway(on) {
        if (state.saveAnyway === on) return;
        state.saveAnyway = on;
        var btn = el('mcp-editor-save');
        if (!btn) return;
        var key = on ? 'mcp_save_anyway' : 'mcp_save';
        btn.dataset.i18n = key;
        btn.textContent = t(key);
    }

    function setBusy(busy) {
        state.busy = !!busy;
        var btn = el('mcp-editor-save');
        var testBtn = el('mcp-editor-test');
        if (typeof window.setButtonBusy === 'function') {
            if (btn) window.setButtonBusy(btn, busy);
            if (testBtn) window.setButtonBusy(testBtn, busy);
        } else {
            if (btn) btn.disabled = busy;
            if (testBtn) testBtn.disabled = busy;
        }
    }

    // ---- test / save ---------------------------------------------------
    function callTest(server) {
        var cb = state.options && state.options.callbacks;
        if (!cb || typeof cb.test !== 'function') {
            return Promise.resolve({ name: server.name, ok: false, error: 'no tester' });
        }
        var epoch = state.epoch;
        return Promise.resolve(cb.test(server, context())).then(function (result) {
            if (!isCurrent(epoch)) return null;
            return result || { name: server.name, ok: false };
        }, function (err) {
            return { name: server.name, ok: false, error: (err && err.message) || '' };
        });
    }

    function runCheck(servers) {
        showResult('info', '<div class="flex items-center gap-2">'
            + '<i class="fas fa-spinner fa-spin text-xs"></i>'
            + '<span>' + esc(t('mcp_testing')) + '</span></div>');
        // Sequential probing keeps the results in submission order and never
        // opens more outbound attempts than the deployment budget expects.
        var items = [];
        var chain = servers.reduce(function (promise, server) {
            return promise.then(function () {
                return callTest(server).then(function (probe) {
                    if (probe) items.push({ server: server, probe: probe });
                });
            });
        }, Promise.resolve());
        return chain.then(function () {
            if (!isCurrent(state.epoch)) return [];
            renderResultList(items);
            return items.map(function (i) { return i.probe; });
        });
    }

    function bindItem(server, key) {
        return { server: server, key: key, status: 'pending', error: '', unknown: false };
    }

    function buildItems(servers) {
        var seen = {};
        servers.forEach(function (server) {
            var name = server.name || '';
            if (seen[name]) throw new Error(t('mcp_name_exists').replace('{name}', name));
            seen[name] = true;
        });
        return servers.map(function (server) {
            // A "result unknown" item has to be retried with the *same* key:
            // that key is what lets the server answer the retry with the row the
            // first attempt may have committed. A definite failure keeps its key
            // too until the input changes (`onEdit` re-keys it).
            var retained = null;
            for (var i = 0; i < state.items.length; i += 1) {
                if (state.items[i].server.name === server.name
                        && state.items[i].status !== 'ok') {
                    retained = state.items[i];
                    break;
                }
            }
            return bindItem(server, retained ? retained.key : newIdempotencyKey());
        });
    }

    function saveItem(item) {
        var cb = state.options && state.options.callbacks;
        var epoch = state.epoch;
        item.status = 'saving';
        return Promise.resolve(cb.save(item.server, context(), { idempotencyKey: item.key }))
            .then(function (result) {
                if (!isCurrent(epoch)) return;
                result = result || {};
                if (result.ok) {
                    item.status = 'ok';
                    item.error = '';
                    item.unknown = false;
                    if (result.id) item.id = result.id;
                    // Recorded by name so a later retry of the batch submits
                    // only what is still outstanding.
                    state.saved[item.server.name] = result.id || true;
                } else if (result.unknown) {
                    // The request may have committed: keep the payload and the
                    // key so a retry is answered with the same connection.
                    item.status = 'unknown';
                    item.unknown = true;
                    item.error = result.error || '';
                } else {
                    item.status = 'fail';
                    item.error = result.error || '';
                }
            }, function (err) {
                if (!isCurrent(epoch)) return;
                item.status = 'fail';
                item.error = (err && err.message) || '';
            });
    }

    function renderItemResults() {
        var rows = state.items.map(function (item) {
            var tone = { ok: 'ok', fail: 'fail', unknown: 'info', pending: 'info', saving: 'info' }[item.status] || 'info';
            var icon = item.status === 'ok' ? 'fa-circle-check'
                : (item.status === 'fail' ? 'fa-circle-xmark'
                    : (item.status === 'unknown' ? 'fa-circle-question' : 'fa-circle-dot'));
            var label = item.status === 'ok' ? t('mcp_test_ok')
                : (item.status === 'fail' ? t('mcp_test_fail')
                    : (item.status === 'unknown' ? t('mcp_batch_unknown') : t('mcp_batch_pending')));
            var detail = item.error
                ? '<p class="mt-1.5 text-xs break-words opacity-90">' + esc(item.error) + '</p>' : '';
            return '<div class="py-2 first:pt-0 last:pb-0">'
                + '<div class="flex items-center gap-2 font-medium">'
                + '<i class="fas ' + icon + '"></i>'
                + '<span class="font-mono">' + esc(item.server.name) + '</span>'
                + '<span class="opacity-50">·</span><span>' + esc(label) + '</span></div>'
                + detail + '</div>';
        }).join('<div class="border-t border-current opacity-10"></div>');
        showResult('info', rows);
    }

    function pendingItems() {
        return state.items.filter(function (i) {
            return i.status !== 'ok' && i.status !== 'saving';
        });
    }

    function saveBatch() {
        if (state.busy) return Promise.resolve();
        setBusy(true);
        renderItemResults();
        var queue = pendingItems().slice();
        function step() {
            if (!state.open || !queue.length) return Promise.resolve();
            var item = queue.shift();
            return saveItem(item).then(function () {
                if (!isCurrent(state.epoch)) return;
                if (item.status === 'fail' || item.status === 'unknown') {
                    // First failure or unknown stops the run: later items stay
                    // untouched for the user to handle.
                    return;
                }
                return step();
            });
        }
        return step().then(function () {
            if (!state.open) return;
            setBusy(false);
            renderItemResults();
            var outstanding = state.items.filter(function (i) { return i.status !== 'ok'; });
            // Everything created in this editor counts, including an earlier
            // attempt's successes that were dropped from this run.
            var ok = Object.keys(state.saved).length;
            var total = ok + outstanding.length;
            var allOk = outstanding.length === 0 && ok > 0;
            if (typeof state.options.callbacks.onSaved === 'function') {
                state.options.callbacks.onSaved({ saved: ok, total: total, allOk: allOk });
            }
            if (allOk) {
                close(true);
            } else {
                // Drop the confirmed items so a retry only touches the rest; a
                // successful item is never resubmitted or rolled back.
                state.items = outstanding;
            }
        });
    }

    function saveSingle() {
        if (state.busy) return Promise.resolve();
        setBusy(true);
        // Same rule as a batch: an item whose outcome is unknown is retried
        // with the key (and payload) it already used, so the server can answer
        // with the row the first attempt may have committed. A refused item is
        // re-keyed by `onEdit` as soon as its input changes.
        state.items = buildItems([state.servers[0]]);
        var item = state.items[0];
        return saveItem(item).then(function () {
            setBusy(false);
            if (!state.open) return;
            if (item.status === 'ok') {
                if (typeof state.options.callbacks.onSaved === 'function') {
                    state.options.callbacks.onSaved({ saved: 1, total: 1, allOk: true });
                }
                // A committed save is not an abandoned draft: the editor
                // closes on its own, like master's, and never asks the user to
                // confirm discarding what was just written.
                close(true);
            } else {
                renderItemResults();
                if (typeof state.options.callbacks.onSaved === 'function') {
                    state.options.callbacks.onSaved({ saved: 0, total: 1, allOk: false });
                }
            }
        });
    }

    function checkThenSave(servers) {
        var active = servers.filter(function (s) { return !s.disabled; });
        if (state.saveAnyway || !active.length) return Promise.resolve(true);
        return runCheck(active).then(function (probes) {
            if (!isCurrent(state.epoch)) return false;
            probes = probes || [];
            var allPass = probes.length === active.length
                && probes.every(function (p) { return p.ok || p.needs_auth; });
            if (!allPass) {
                appendNote(t('mcp_check_failed'));
                setSaveAnyway(true);
                return false;
            }
            return true;
        });
    }

    function onSave() {
        if (state.busy) return;
        var servers;
        try {
            servers = readServers();
        } catch (err) {
            showError(err.message);
            return;
        }
        var isBatch = state.options && state.options.mode === 'create' && servers.length > 1;
        checkThenSave(servers).then(function (proceed) {
            if (!proceed || !isCurrent(state.epoch)) return;
            state.servers = servers;
            if (isBatch) {
                // A server this editor already created is never submitted
                // again: the JSON pane still holds it, and a second create
                // would either duplicate the row or be refused by the server.
                var outstanding = servers.filter(function (s) {
                    return !Object.prototype.hasOwnProperty.call(state.saved, s.name || '');
                });
                try {
                    state.items = buildItems(outstanding);
                } catch (err) {
                    showError(err.message);
                    return;
                }
                if (outstanding.length === 0) {
                    // Everything in the pane is already created; report that
                    // rather than doing nothing silently.
                    var savedCount = Object.keys(state.saved).length;
                    if (typeof state.options.callbacks.onSaved === 'function') {
                        state.options.callbacks.onSaved(
                            { saved: savedCount, total: savedCount, allOk: true });
                    }
                    close(true);
                    return;
                }
                return saveBatch();
            }
            return saveSingle();
        });
    }

    function onTest() {
        if (state.busy) return;
        var servers;
        try {
            servers = readServers();
        } catch (err) {
            showError(err.message);
            return;
        }
        setBusy(true);
        var icon = el('mcp-editor-test-icon');
        if (icon) icon.className = 'fas fa-spinner fa-spin text-xs';
        runCheck(servers).then(function () {
            setBusy(false);
            if (icon) icon.className = 'fas fa-plug-circle-check text-xs';
        });
    }

    // ---- open / close --------------------------------------------------
    function titleKey() {
        if (!state.options) return 'mcp_add_title';
        if (state.options.mode === 'edit') return 'mcp_edit';
        return 'mcp_add_title';
    }

    function open(options) {
        if (!ensureBound()) return;
        state.options = options || {};
        state.epoch += 1;
        state.saveAnyway = false;
        state.items = [];
        state.saved = {};
        state.busy = false;
        state.touched = { env: false, headers: false };
        showTab('form');
        var initial = state.options.initial || [];
        fillForm(initial[0] || null);
        if (state.options.mode !== 'create') {
            // Editing locks the name; the stable id is what is updated.
            if (el('mcp-field-name')) el('mcp-field-name').disabled = true;
        }
        var jsonEl = el('mcp-field-json');
        if (jsonEl) jsonEl.value = initial.length ? serversToJson(initial) : '';
        clearResult();
        var title = el('mcp-editor-title');
        if (title) {
            var key = titleKey();
            title.dataset.i18n = key;
            title.textContent = t(key);
        }
        var saveBtn = el('mcp-editor-save');
        if (saveBtn) { saveBtn.dataset.i18n = 'mcp_save'; saveBtn.textContent = t('mcp_save'); }
        state.open = true;
        el('mcp-editor-overlay').classList.remove('hidden');
        if (state.options.mode === 'create' && el('mcp-field-name')) {
            setTimeout(function () { el('mcp-field-name').focus(); }, 30);
        }
    }

    function isDirty() {
        if (el('mcp-field-json') && el('mcp-field-json').value.trim() !== '') return true;
        if (el('mcp-field-url') && !!el('mcp-field-url').value.trim()) return true;
        if (el('mcp-field-command') && !!el('mcp-field-command').value.trim()) return true;
        if (el('mcp-field-name') && !!el('mcp-field-name').value.trim()) return true;
        return !!(state.touched.env || state.touched.headers);
    }

    /** `true`, `false`, or a Promise<boolean> from the page's own guard. */
    function wantsClose() {
        var cb = state.options && state.options.callbacks;
        if (!cb || typeof cb.confirmClose !== 'function') return true;
        if (!isDirty()) return true;
        return cb.confirmClose();
    }

    function close(force) {
        if (!state.open) return;
        if (!force) {
            var decision = wantsClose();
            if (decision === false) return;
            if (decision && typeof decision.then === 'function') {
                // The page shows its own unsaved-changes dialog; close only if
                // the user chose to discard.
                decision.then(function (ok) { if (ok) close(true); });
                return;
            }
        }
        var overlay = el('mcp-editor-overlay');
        if (overlay) overlay.classList.add('hidden');
        var typeEl = el('mcp-field-type');
        if (typeEl) typeEl.classList.remove('open');
        // Clear every secret typed here: a closed editor must not keep one.
        ['mcp-field-env', 'mcp-field-headers', 'mcp-field-json'].forEach(function (id) {
            if (el(id)) el(id).value = '';
        });
        if (el('mcp-field-name')) { el('mcp-field-name').value = ''; el('mcp-field-name').disabled = false; }
        if (el('mcp-field-command')) el('mcp-field-command').value = '';
        if (el('mcp-field-url')) el('mcp-field-url').value = '';
        if (el('mcp-field-scope')) el('mcp-field-scope').value = '';
        if (el('mcp-field-prefix')) el('mcp-field-prefix').value = '';
        if (el('mcp-field-timeout')) el('mcp-field-timeout').value = '';
        if (el('mcp-field-disabled')) el('mcp-field-disabled').checked = false;
        state.open = false;
        state.epoch += 1;
        state.saveAnyway = false;
        state.items = [];
        state.saved = {};
        state.touched = { env: false, headers: false };
        var opts = state.options || {};
        if (typeof opts.onClose === 'function') opts.onClose();
        state.options = opts;
        if (opts.focus && typeof opts.focus.focus === 'function') {
            try { opts.focus.focus(); } catch (_) { /* detached */ }
        }
    }

    // ---- binding -------------------------------------------------------
    function bindOnce() {
        var overlay = el('mcp-editor-overlay');
        if (!overlay) return false;
        var on = function (id, event, handler) {
            var node = el(id);
            if (node) node.addEventListener(event, handler);
        };
        on('mcp-editor-cancel', 'click', function () { close(); });
        on('mcp-editor-save', 'click', onSave);
        on('mcp-editor-test', 'click', onTest);
        on('mcp-advanced-toggle', 'click', function () {
            setAdvancedOpen(!(el('mcp-advanced-fields') && !el('mcp-advanced-fields').classList.contains('hidden')));
        });
        overlay.addEventListener('click', function (e) { if (e.target.id === 'mcp-editor-overlay') close(); });
        // Any edit invalidates a "save anyway" decision and moves a failed item
        // back to "not yet submitted".
        overlay.addEventListener('input', function (e) { setSaveAnyway(false); markTouched(e.target); onEdit(); });
        overlay.addEventListener('change', function (e) { setSaveAnyway(false); markTouched(e.target); onEdit(); });
        Array.prototype.slice.call(document.querySelectorAll('[data-mcp-tab]')).forEach(function (btn) {
            btn.addEventListener('click', function () { switchTab(btn.getAttribute('data-mcp-tab')); });
        });
        var jsonEl = el('mcp-field-json');
        if (jsonEl) jsonEl.placeholder = JSON.stringify(
            { mcpServers: { fetch: { command: 'uvx', args: ['mcp-server-fetch'] } } }, null, 2);
        state.bound = true;
        return true;
    }

    function onEdit() {
        // A failed item whose input changed may be retried with a fresh key: the
        // server refused it, so a new attempt is a new request. An unknown item
        // keeps its key until its outcome is confirmed.
        state.items.forEach(function (item) {
            if (item.status === 'fail') item.key = newIdempotencyKey();
        });
    }

    function markTouched(target) {
        if (!target || !target.id) return;
        if (target.id === 'mcp-field-env') state.touched.env = true;
        if (target.id === 'mcp-field-headers') state.touched.headers = true;
    }

    function ensureBound() {
        if (state.bound) return true;
        return bindOnce();
    }

    // ---- public surface ------------------------------------------------
    var api = {
        open: open,
        close: close,
        isOpen: function () { return state.open; },
        state: state,
        // What the two footer buttons are bound to, exposed so a test drives
        // the same entry point a click does instead of a second code path.
        save: onSave,
        test: onTest,
        // Whether the open editor holds anything a navigation would destroy.
        isDirty: isDirty,
        // Exposed for tests (parsing/round-trip are pure).
        _kvToObject: kvToObject,
        _objectToKv: objectToKv,
        _configForJson: configForJson,
        _serversToJson: serversToJson,
        _parseJson: parseJson,
        _readForm: readForm,
        _fillForm: fillForm,
        _readServers: readServers,
        _switchTab: switchTab,
        _showTab: showTab,
    };

    window.EcMcpEditor = api;
})();
