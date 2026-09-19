// Model catalog + ordered chat-fallback chain editors (change
// integrate-upstream-core-capabilities, P3; design.md D4).
//
// The fork's ModelsHandler already implements `set_capability` /
// `chat_fallback` / `chain` and `save_catalog` / `models` / `hidden`
// server-side. This module only adds the Web editor for them; it never
// re-implements persistence. It is loaded as a plain IIFE (the console is
// not transpiled) and exposes exactly one global:
//
//     window.RdaiFunctionalModels
//
// Pure data functions are deliberately separated from DOM code so they can
// be unit-tested in a bare VM context (tests/test_functional_models.cjs):
//
//   - buildFallbackPayload(enabled, rows) -> set_capability JSON
//   - buildCatalogPayload(providerId, draft) -> save_catalog JSON
//   - draftFromProvider / catalogView / editModel / deleteModel /
//     restoreModel / renameModel / resetDefault / moveChainRow / escapeHtml
//
// `mountFallback` / `mountCatalog` render into a caller-provided root and
// return a handle. All user-visible strings go through the injected `t`,
// and every dynamic value is written with `textContent` / `value` (never
// raw innerHTML), so malicious model names cannot become markup.

(function () {
    'use strict';

    // Mirrors models/model_catalog.py::VALID_CAPABILITIES.
    var VALID_CAPABILITIES = [
        'text', 'vision', 'video', 'image', 'embedding', 'asr', 'tts'
    ];
    var DEFAULT_CAPABILITIES = ['text'];

    // ------------------------------------------------------------------
    // small helpers
    // ------------------------------------------------------------------

    function isArray(value) {
        return Object.prototype.toString.call(value) === '[object Array]';
    }

    function isObject(value) {
        return value !== null && typeof value === 'object' && !isArray(value);
    }

    function trim(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/^\s+/, '').replace(/\s+$/, '');
    }

    function clone(value) {
        if (value === null || value === undefined) return value;
        return JSON.parse(JSON.stringify(value));
    }

    function inputError(code, message) {
        var error = new Error(message || code);
        error.code = code;
        return error;
    }

    function escapeHtml(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // ------------------------------------------------------------------
    // catalog validation (mirrors models/model_catalog.py::normalize_entry)
    // ------------------------------------------------------------------

    function normalizeCapabilities(raw) {
        var list = raw;
        if (list === null || list === undefined || list === '') {
            list = DEFAULT_CAPABILITIES.slice();
        }
        if (typeof list === 'string') list = [list];
        if (!isArray(list)) {
            throw inputError('unknown_capability', 'capabilities must be a list');
        }
        var out = [];
        var seen = {};
        for (var i = 0; i < list.length; i++) {
            var tag = trim(list[i]).toLowerCase();
            if (!tag) continue;
            if (VALID_CAPABILITIES.indexOf(tag) < 0) {
                throw inputError('unknown_capability', 'unknown capability: ' + tag);
            }
            if (!seen[tag]) {
                seen[tag] = true;
                out.push(tag);
            }
        }
        if (!out.length) out = DEFAULT_CAPABILITIES.slice();
        return out;
    }

    function normalizeLimit(name, raw) {
        var value = Number(raw);
        if (!isFinite(value) || value % 1 !== 0 || value <= 0) {
            throw inputError('invalid_limit', name + ' must be a positive integer');
        }
        return value;
    }

    function normalizeCatalogEntry(raw) {
        if (!isObject(raw)) {
            throw inputError('missing_name', 'model entry must be an object');
        }
        var name = trim(raw.name);
        if (!name) {
            throw inputError('missing_name', 'model name is required');
        }
        var entry = { name: name, capabilities: normalizeCapabilities(raw.capabilities) };
        if (raw.context_window !== undefined && raw.context_window !== null
                && raw.context_window !== '') {
            entry.context_window = normalizeLimit('context_window', raw.context_window);
        }
        if (raw.max_output_tokens !== undefined && raw.max_output_tokens !== null
                && raw.max_output_tokens !== '') {
            entry.max_output_tokens = normalizeLimit('max_output_tokens', raw.max_output_tokens);
        }
        return entry;
    }

    // ------------------------------------------------------------------
    // pure payload builders
    // ------------------------------------------------------------------

    // set_capability payload for the ordered fallback chain. Each row keeps
    // its original position and its provider id verbatim (custom:<id>
    // included). Incomplete rows are rejected in the UI; an enabled chain
    // with no rows is an input error. Disabling never drops configured rows.
    function buildFallbackPayload(enabled, rows) {
        if (rows !== undefined && rows !== null && !isArray(rows)) {
            throw inputError('invalid_chain', 'rows must be a list');
        }
        var list = isArray(rows) ? rows : [];
        var chain = [];
        for (var i = 0; i < list.length; i++) {
            var row = list[i] || {};
            var provider = trim(row.provider);
            var model = trim(row.model);
            if (!provider || !model) {
                throw inputError('incomplete_link',
                    'every fallback node needs a provider and a model');
            }
            chain.push({ provider: provider, model: model });
        }
        if (enabled && chain.length === 0) {
            throw inputError('empty_chain',
                'at least one fallback node is required to enable the fallback');
        }
        return {
            action: 'set_capability',
            capability: 'chat_fallback',
            enabled: !!enabled,
            chain: chain
        };
    }

    // save_catalog payload. `models` comes ONLY from draft.overrides — never
    // from seed/effective — so untouched presets are never frozen into the
    // overlay (the "do not freeze untouched presets" invariant).
    function buildCatalogPayload(providerId, draft) {
        var pid = trim(providerId);
        if (!pid) throw inputError('missing_provider', 'provider id is required');
        draft = draft || {};
        var overrides = isArray(draft.overrides) ? draft.overrides : [];
        var models = [];
        var names = {};
        for (var i = 0; i < overrides.length; i++) {
            var entry = normalizeCatalogEntry(overrides[i]);
            if (names[entry.name]) {
                throw inputError('duplicate_name', 'duplicate model name: ' + entry.name);
            }
            names[entry.name] = true;
            models.push(entry);
        }
        var hidden = [];
        var hiddenSeen = {};
        var rawHidden = isArray(draft.hidden) ? draft.hidden : [];
        for (var h = 0; h < rawHidden.length; h++) {
            var name = trim(rawHidden[h]);
            if (!name || hiddenSeen[name] || names[name]) continue;
            hiddenSeen[name] = true;
            hidden.push(name);
        }
        return {
            action: 'save_catalog',
            provider_id: pid,
            models: models,
            hidden: hidden
        };
    }

    // ------------------------------------------------------------------
    // draft model
    // ------------------------------------------------------------------

    function draftFromProvider(provider) {
        provider = provider || {};
        return {
            seed: clone(isArray(provider.seed) ? provider.seed : []),
            overrides: clone(isArray(provider.catalog) ? provider.catalog : []),
            hidden: clone(isArray(provider.hidden) ? provider.hidden : [])
        };
    }

    function seedEntry(draft, name) {
        var seed = isArray(draft && draft.seed) ? draft.seed : [];
        for (var i = 0; i < seed.length; i++) {
            if (trim(seed[i] && seed[i].name) === name) return seed[i];
        }
        return null;
    }

    function overrideEntry(draft, name) {
        var overrides = isArray(draft && draft.overrides) ? draft.overrides : [];
        for (var i = 0; i < overrides.length; i++) {
            if (trim(overrides[i] && overrides[i].name) === name) return overrides[i];
        }
        return null;
    }

    // Effective list = preset base, minus tombstones, plus overrides layered
    // by name (same algorithm as ModelsHandler._merged_catalog).
    function effectiveEntries(draft) {
        draft = draft || {};
        var seed = isArray(draft.seed) ? draft.seed : [];
        var overrides = isArray(draft.overrides) ? draft.overrides : [];
        var hidden = isArray(draft.hidden) ? draft.hidden : [];
        var hiddenSet = {};
        var i;
        for (i = 0; i < hidden.length; i++) {
            var hiddenName = trim(hidden[i]);
            if (hiddenName) hiddenSet[hiddenName] = true;
        }
        var overrideByName = {};
        for (i = 0; i < overrides.length; i++) {
            var overrideName = trim(overrides[i] && overrides[i].name);
            if (overrideName) overrideByName[overrideName] = overrides[i];
        }
        var out = [];
        var used = {};
        for (i = 0; i < seed.length; i++) {
            var seedName = trim(seed[i] && seed[i].name);
            if (!seedName || hiddenSet[seedName]) continue;
            out.push(clone(overrideByName[seedName] || seed[i]));
            used[seedName] = true;
        }
        for (i = 0; i < overrides.length; i++) {
            var addedName = trim(overrides[i] && overrides[i].name);
            if (addedName && !used[addedName] && !hiddenSet[addedName]) {
                out.push(clone(overrides[i]));
                used[addedName] = true;
            }
        }
        return out;
    }

    function catalogView(draft) {
        draft = draft || {};
        var seedNames = {};
        var overrideNames = {};
        var i;
        for (i = 0; i < (draft.seed || []).length; i++) {
            seedNames[trim(draft.seed[i] && draft.seed[i].name)] = true;
        }
        for (i = 0; i < (draft.overrides || []).length; i++) {
            overrideNames[trim(draft.overrides[i] && draft.overrides[i].name)] = true;
        }
        var visible = [];
        var effective = effectiveEntries(draft);
        for (i = 0; i < effective.length; i++) {
            var row = effective[i];
            var name = trim(row.name);
            row.origin = overrideNames[name]
                ? (seedNames[name] ? 'override' : 'added')
                : 'preset';
            row.hidden = false;
            visible.push(row);
        }
        var hidden = [];
        var rawHidden = isArray(draft.hidden) ? draft.hidden : [];
        for (i = 0; i < rawHidden.length; i++) {
            var hiddenName = trim(rawHidden[i]);
            if (!hiddenName) continue;
            var base = seedEntry(draft, hiddenName);
            var hiddenRow = base ? clone(base) : { name: hiddenName, capabilities: ['text'] };
            hiddenRow.origin = 'preset';
            hiddenRow.originPreset = true;
            hiddenRow.hidden = true;
            hidden.push(hiddenRow);
        }
        return { visible: visible, hidden: hidden };
    }

    function upsert(overrides, entry) {
        var out = [];
        var found = false;
        for (var i = 0; i < overrides.length; i++) {
            if (trim(overrides[i] && overrides[i].name) === entry.name) {
                out.push(entry);
                found = true;
            } else {
                out.push(clone(overrides[i]));
            }
        }
        if (!found) out.push(entry);
        return out;
    }

    function mergeEntry(base, current, patch) {
        var out = {};
        var sources = [base, current];
        for (var s = 0; s < sources.length; s++) {
            var source = sources[s];
            if (!source) continue;
            var keys = Object.keys(source);
            for (var k = 0; k < keys.length; k++) {
                if (source[keys[k]] !== undefined) out[keys[k]] = source[keys[k]];
            }
        }
        if (patch) {
            var patchKeys = Object.keys(patch);
            for (var p = 0; p < patchKeys.length; p++) {
                var key = patchKeys[p];
                var value = patch[key];
                if (value === undefined) continue;
                if (value === null || value === '') {
                    delete out[key];
                } else {
                    out[key] = value;
                }
            }
        }
        return out;
    }

    // Edit (or first-time override) a model. Untouched fields of the base
    // entry are preserved; the name is removed from `hidden` if present.
    function editModel(draft, name, patch) {
        name = trim(name);
        if (!name) throw inputError('missing_name', 'model name is required');
        var merged = mergeEntry(seedEntry(draft, name), overrideEntry(draft, name), patch);
        merged.name = name;
        var entry = normalizeCatalogEntry(merged);
        var hidden = [];
        var rawHidden = isArray(draft && draft.hidden) ? draft.hidden : [];
        for (var i = 0; i < rawHidden.length; i++) {
            if (trim(rawHidden[i]) !== name) hidden.push(trim(rawHidden[i]));
        }
        return {
            seed: clone(isArray(draft && draft.seed) ? draft.seed : []),
            overrides: upsert(isArray(draft && draft.overrides) ? draft.overrides : [], entry),
            hidden: hidden
        };
    }

    function isVisible(draft, name) {
        var visible = catalogView(draft).visible;
        for (var i = 0; i < visible.length; i++) {
            if (visible[i].name === name) return true;
        }
        return false;
    }

    // Add a brand-new model. Duplicate names are rejected here; the backend
    // still performs its own final validation.
    function addModel(draft, entry) {
        var name = trim(entry && entry.name);
        if (!name) throw inputError('missing_name', 'model name is required');
        if (isVisible(draft, name)) {
            throw inputError('duplicate_name', 'duplicate model name: ' + name);
        }
        return editModel(draft, name, entry);
    }

    // Delete a model. A preset (present in seed) keeps a tombstone so it is
    // not restored on the next read; an added model only drops its override.
    function deleteModel(draft, name) {
        name = trim(name);
        var overrides = [];
        var rawOverrides = isArray(draft && draft.overrides) ? draft.overrides : [];
        for (var i = 0; i < rawOverrides.length; i++) {
            if (trim(rawOverrides[i] && rawOverrides[i].name) !== name) {
                overrides.push(clone(rawOverrides[i]));
            }
        }
        var hidden = clone(isArray(draft && draft.hidden) ? draft.hidden : []);
        if (seedEntry(draft, name) && hidden.indexOf(name) < 0) hidden.push(name);
        return {
            seed: clone(isArray(draft && draft.seed) ? draft.seed : []),
            overrides: overrides,
            hidden: hidden
        };
    }

    // Restore a hidden preset: remove its tombstone.
    function restoreModel(draft, name) {
        name = trim(name);
        var hidden = [];
        var rawHidden = isArray(draft && draft.hidden) ? draft.hidden : [];
        for (var i = 0; i < rawHidden.length; i++) {
            if (trim(rawHidden[i]) !== name) hidden.push(trim(rawHidden[i]));
        }
        return {
            seed: clone(isArray(draft && draft.seed) ? draft.seed : []),
            overrides: clone(isArray(draft && draft.overrides) ? draft.overrides : []),
            hidden: hidden
        };
    }

    // Revert the whole provider to its code-side presets.
    function resetDefault(draft) {
        return {
            seed: clone(isArray(draft && draft.seed) ? draft.seed : []),
            overrides: [],
            hidden: []
        };
    }

    // Rename = delete the old name (tombstoning a preset) + add the new one.
    function renameModel(draft, oldName, newName) {
        oldName = trim(oldName);
        newName = trim(newName);
        if (!newName) throw inputError('missing_name', 'model name is required');
        if (oldName === newName) return draft;
        if (isVisible(draft, newName)) {
            throw inputError('duplicate_name', 'duplicate model name: ' + newName);
        }
        var visible = catalogView(draft).visible;
        var source = null;
        for (var i = 0; i < visible.length; i++) {
            if (visible[i].name === oldName) { source = visible[i]; break; }
        }
        if (!source) throw inputError('missing_model', 'unknown model: ' + oldName);
        var entry = { name: newName, capabilities: normalizeCapabilities(source.capabilities) };
        if (source.context_window !== undefined) entry.context_window = source.context_window;
        if (source.max_output_tokens !== undefined) {
            entry.max_output_tokens = source.max_output_tokens;
        }
        return editModel(deleteModel(draft, oldName), newName, entry);
    }

    // ------------------------------------------------------------------
    // fallback-chain reads / row moves
    // ------------------------------------------------------------------

    function readFallbackCapability(capability) {
        capability = capability || {};
        var chain = [];
        var raw = isArray(capability.chain) ? capability.chain : [];
        for (var i = 0; i < raw.length; i++) {
            if (!isObject(raw[i])) continue;
            chain.push({
                provider: trim(raw[i].provider),
                model: trim(raw[i].model)
            });
        }
        return { enabled: !!capability.enabled, chain: chain };
    }

    function moveChainRow(rows, from, to) {
        var out = isArray(rows) ? rows.slice() : [];
        if (from < 0 || from >= out.length) return out;
        if (to < 0) to = 0;
        if (to > out.length - 1) to = out.length - 1;
        var moved = out.splice(from, 1)[0];
        out.splice(to, 0, moved);
        return out;
    }

    // ------------------------------------------------------------------
    // error -> locale keys
    // ------------------------------------------------------------------

    function fallbackErrorText(t, error) {
        var code = error && error.code;
        if (code === 'empty_chain') return t('models_fallback_chain_empty');
        if (code === 'incomplete_link' || code === 'invalid_chain') {
            return t('models_fallback_incomplete');
        }
        return (error && error.message) || t('models_save_failed');
    }

    function catalogErrorText(t, error) {
        var code = error && error.code;
        if (code === 'duplicate_name') return t('models_catalog_duplicate');
        if (code === 'missing_name') return t('models_catalog_name_required');
        if (code === 'invalid_limit') return t('models_catalog_invalid_limit');
        if (code === 'unknown_capability') return t('models_catalog_unknown_capability');
        return (error && error.message) || t('models_save_failed');
    }

    function saveErrorText(t, error) {
        var detail = error && (error.message || error.error || error.code);
        if (detail && detail !== 'models_save_failed') {
            return t('models_save_failed') + ': ' + detail;
        }
        return t('models_save_failed');
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
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function button(doc, className, label, onClick) {
        var node = el(doc, 'button', className, label);
        node.setAttribute('type', 'button');
        if (onClick) node.addEventListener('click', onClick);
        return node;
    }

    function clear(node) {
        if (!node) return;
        node.textContent = '';
    }

    function appendOption(doc, select, value, label) {
        var option = el(doc, 'option', '', label === undefined ? value : label);
        option.value = value;
        select.appendChild(option);
        return option;
    }

    function hasOption(select, value) {
        var children = select.children || [];
        for (var i = 0; i < children.length; i++) {
            if (children[i].value === value) return true;
        }
        return false;
    }

    function providerLabel(provider) {
        if (typeof provider === 'string') return provider;
        var label = provider && provider.label;
        if (label && typeof label === 'object') {
            label = label.zh || label.en || provider.id;
        }
        return String(label || (provider && provider.id) || '');
    }

    function normalizeProviders(list) {
        var out = [];
        var raw = isArray(list) ? list : [];
        for (var i = 0; i < raw.length; i++) {
            var provider = raw[i];
            if (provider === null || provider === undefined) continue;
            if (typeof provider === 'string') {
                out.push({ id: trim(provider), label: trim(provider) });
                continue;
            }
            var id = trim(provider.id || provider.value);
            if (!id) continue;
            out.push({ id: id, label: providerLabel(provider) });
        }
        return out;
    }

    function modelValues(map, providerId) {
        var list = map && providerId ? map[providerId] : null;
        var out = [];
        var raw = isArray(list) ? list : [];
        for (var i = 0; i < raw.length; i++) {
            var item = raw[i];
            var value = (typeof item === 'string') ? item
                : (item && item.value !== undefined ? item.value : (item && item.name));
            value = trim(value);
            if (value && out.indexOf(value) < 0) out.push(value);
        }
        return out;
    }

    // ------------------------------------------------------------------
    // mountFallback
    // ------------------------------------------------------------------
    //
    // Contract settled with the orchestrator:
    //   save(payload) -> Promise, performs the POST itself and resolves on
    //                    HTTP 200 + body.status === 'success' (usually with
    //                    the server body, e.g. {status, applied:{enabled,
    //                    chain}}), rejecting otherwise.
    //   reload()      -> optional Promise resolving the fresh chat_fallback
    //                    capability object (from GET /api/models). After a
    //                    successful save the mount re-seeds from reload()
    //                    when provided, otherwise from the save resolution
    //                    value when it carries {enabled|chain}; otherwise it
    //                    keeps the local draft.
    // The returned handle adds dispose(), save(), refresh(), getState() and
    // the row mutators used by the DOM buttons so tests and the orchestrator
    // can drive it without synthesising clicks.

    function mountFallback(options) {
        options = options || {};
        var root = options.root;
        if (!root) throw inputError('missing_root', 'mountFallback requires a root element');
        var doc = docOf(root);
        var saveFn = typeof options.save === 'function' ? options.save : function () {};
        var reloadFn = typeof options.reload === 'function' ? options.reload : null;
        var t = typeof options.t === 'function' ? options.t : function (key) { return key; };
        var capability = options.capability || {};
        var providers = normalizeProviders(options.providers || capability.providers);
        var providerModels = capability.provider_models || {};

        var seeded = readFallbackCapability(capability);
        var state = {
            enabled: seeded.enabled,
            chain: seeded.chain,
            error: '',
            saving: false,
            saved: false
        };
        var disposed = false;

        var container = el(doc, 'div', 'rdai-fallback space-y-3');
        var toggleBtn = button(doc, 'rdai-fallback-toggle', t('models_fallback_enable'), function () {
            setEnabled(!state.enabled);
        });
        toggleBtn.setAttribute('role', 'switch');
        var listWrap = el(doc, 'div', 'rdai-fallback-list space-y-2');
        var addBtn = button(doc, 'rdai-fallback-add', t('models_fallback_add'), function () {
            addLink({ provider: '', model: '' });
        });
        var errorEl = el(doc, 'div', 'rdai-fallback-error text-xs text-red-500');
        var statusEl = el(doc, 'span', 'rdai-fallback-status text-xs text-primary-500');
        var saveBtn = button(doc, 'rdai-fallback-save', t('save'), function () { saveNow(); });

        container.appendChild(toggleBtn);
        container.appendChild(listWrap);
        container.appendChild(addBtn);
        var footer = el(doc, 'div', 'flex items-center gap-3');
        footer.appendChild(statusEl);
        footer.appendChild(errorEl);
        footer.appendChild(saveBtn);
        container.appendChild(footer);
        clear(root);
        root.appendChild(container);

        function buildRow(link, index) {
            var row = el(doc, 'div', 'rdai-fallback-row flex items-center gap-2');
            var choose = el(doc, 'select', 'rdai-fallback-provider');
            appendOption(doc, choose, '', '--');
            for (var i = 0; i < providers.length; i++) {
                appendOption(doc, choose, providers[i].id, providers[i].label);
            }
            if (link.provider && !hasOption(choose, link.provider)) {
                appendOption(doc, choose, link.provider, link.provider);
            }
            choose.value = link.provider || '';
            choose.addEventListener('change', function () {
                setLink(index, { provider: choose.value, model: '' });
            });
            row.appendChild(choose);

            var values = modelValues(providerModels, link.provider);
            var modelNode;
            if (values.length) {
                modelNode = el(doc, 'select', 'rdai-fallback-model');
                appendOption(doc, modelNode, '', '--');
                for (var m = 0; m < values.length; m++) {
                    appendOption(doc, modelNode, values[m], values[m]);
                }
                if (link.model && !hasOption(modelNode, link.model)) {
                    appendOption(doc, modelNode, link.model, link.model);
                }
                modelNode.value = link.model || '';
                modelNode.addEventListener('change', function () {
                    setLink(index, { model: modelNode.value });
                });
            } else {
                modelNode = el(doc, 'input', 'rdai-fallback-model');
                modelNode.setAttribute('type', 'text');
                modelNode.value = link.model || '';
                modelNode.addEventListener('change', function () {
                    setLink(index, { model: modelNode.value });
                });
            }
            row.appendChild(modelNode);

            row.appendChild(button(doc, 'rdai-fallback-up', t('models_fallback_move_up'), function () {
                moveLink(index, index - 1);
            }));
            row.appendChild(button(doc, 'rdai-fallback-down', t('models_fallback_move_down'), function () {
                moveLink(index, index + 1);
            }));
            row.appendChild(button(doc, 'rdai-fallback-remove', t('models_fallback_remove'), function () {
                removeLink(index);
            }));
            return row;
        }

        function renderRows() {
            clear(listWrap);
            for (var i = 0; i < state.chain.length; i++) {
                listWrap.appendChild(buildRow(state.chain[i], i));
            }
        }

        function sync() {
            toggleBtn.setAttribute('aria-checked', state.enabled ? 'true' : 'false');
            toggleBtn.className = 'rdai-fallback-toggle '
                + (state.enabled ? 'rdai-on' : 'rdai-off');
            saveBtn.disabled = !!state.saving;
            statusEl.textContent = state.saved ? t('models_save_success') : '';
            errorEl.textContent = state.error || '';
        }

        function render() {
            renderRows();
            sync();
        }

        function setEnabled(value) {
            state.enabled = !!value;
            sync();
        }

        function addLink(link) {
            state.chain.push({
                provider: trim(link && link.provider),
                model: trim(link && link.model)
            });
            state.saved = false;
            render();
        }

        function removeLink(index) {
            if (index < 0 || index >= state.chain.length) return;
            state.chain.splice(index, 1);
            state.saved = false;
            render();
        }

        function moveLink(from, to) {
            state.chain = moveChainRow(state.chain, from, to);
            state.saved = false;
            render();
        }

        function setLink(index, patch) {
            if (index < 0 || index >= state.chain.length) return;
            var link = state.chain[index];
            if (patch && patch.provider !== undefined) link.provider = trim(patch.provider);
            if (patch && patch.model !== undefined) link.model = trim(patch.model);
            state.saved = false;
            render();
        }

        function reseed(fresh) {
            var data = fresh || {};
            if (isObject(data.applied)) data = data.applied;
            if (typeof data.enabled === 'boolean' || isArray(data.chain)) {
                var read = readFallbackCapability(data);
                state.enabled = read.enabled;
                state.chain = read.chain;
            }
        }

        function refresh(nextCapability) {
            capability = nextCapability || {};
            providers = normalizeProviders(
                capability.providers || options.providers || providers);
            providerModels = capability.provider_models || {};
            reseed(capability);
            state.error = '';
            state.saved = false;
            render();
        }

        function saveNow() {
            if (state.saving || disposed) return Promise.resolve(false);
            var payload;
            try {
                payload = buildFallbackPayload(state.enabled, state.chain);
            } catch (error) {
                state.error = fallbackErrorText(t, error);
                state.saved = false;
                sync();
                return Promise.resolve(false);
            }
            state.saving = true;
            state.error = '';
            state.saved = false;
            sync();
            return Promise.resolve().then(function () {
                return saveFn(payload);
            }).then(function (fresh) {
                if (disposed) return true;
                state.saving = false;
                var reseedFresh = function (data) {
                    reseed(data);
                    if (!disposed) {
                        state.saved = true;
                        state.error = '';
                    }
                };
                if (reloadFn) {
                    return Promise.resolve().then(reloadFn).then(reseedFresh, function () {
                        reseedFresh(fresh);
                    }).then(function () {
                        if (!disposed) render();
                        return true;
                    });
                }
                reseedFresh(fresh);
                render();
                return true;
            }, function (error) {
                if (disposed) return false;
                state.saving = false;
                state.saved = false;
                state.error = saveErrorText(t, error);
                sync();
                return false;
            });
        }

        function getState() {
            return {
                enabled: state.enabled,
                chain: clone(state.chain),
                error: state.error,
                saving: state.saving,
                saved: state.saved
            };
        }

        function dispose() {
            disposed = true;
            clear(root);
        }

        render();

        return {
            dispose: dispose,
            save: saveNow,
            refresh: refresh,
            getState: getState,
            setEnabled: setEnabled,
            addLink: addLink,
            removeLink: removeLink,
            moveLink: moveLink,
            setLink: setLink,
            element: container
        };
    }

    // ------------------------------------------------------------------
    // mountCatalog
    // ------------------------------------------------------------------
    //
    // Same save/reload contract as mountFallback. The save resolution value
    // may be the save_catalog response ({status, provider_id, models}) or a
    // full provider object ({seed, catalog, hidden, ...}); reload() is
    // preferred when supplied. On any failure the local draft is kept.

    function mountCatalog(options) {
        options = options || {};
        var root = options.root;
        if (!root) throw inputError('missing_root', 'mountCatalog requires a root element');
        var doc = docOf(root);
        var saveFn = typeof options.save === 'function' ? options.save : function () {};
        var reloadFn = typeof options.reload === 'function' ? options.reload : null;
        var t = typeof options.t === 'function' ? options.t : function (key) { return key; };
        var provider = options.provider || {};
        var providerId = trim(provider.id);
        var state = {
            draft: draftFromProvider(provider),
            error: '',
            saving: false,
            saved: false
        };
        var disposed = false;

        var container = el(doc, 'div', 'rdai-catalog space-y-3');
        var visibleWrap = el(doc, 'div', 'rdai-catalog-list space-y-2');
        var hiddenSection = el(doc, 'div', 'rdai-catalog-hidden space-y-2');
        hiddenSection.appendChild(el(doc, 'div', 'rdai-catalog-hidden-title text-xs', t('models_catalog_hidden_title')));
        var hiddenWrap = el(doc, 'div', 'rdai-catalog-hidden-list space-y-2');
        hiddenSection.appendChild(hiddenWrap);

        var addName = el(doc, 'input', 'rdai-catalog-add-name');
        addName.setAttribute('type', 'text');
        addName.setAttribute('placeholder', t('models_catalog_add_placeholder'));
        var addBtn = button(doc, 'rdai-catalog-add', t('models_catalog_add'), function () {
            run(function () {
                return addModel(state.draft, {
                    name: addName.value,
                    capabilities: DEFAULT_CAPABILITIES.slice()
                });
            });
            if (!state.error) addName.value = '';
        });
        var resetBtn = button(doc, 'rdai-catalog-reset', t('models_catalog_reset'), function () {
            run(function () { return resetDefault(state.draft); });
        });
        var errorEl = el(doc, 'div', 'rdai-catalog-error text-xs text-red-500');
        var statusEl = el(doc, 'span', 'rdai-catalog-status text-xs text-primary-500');
        var saveBtn = button(doc, 'rdai-catalog-save', t('save'), function () { saveNow(); });

        container.appendChild(visibleWrap);
        container.appendChild(hiddenSection);
        var addRow = el(doc, 'div', 'rdai-catalog-add-row flex items-center gap-2');
        addRow.appendChild(addName);
        addRow.appendChild(addBtn);
        addRow.appendChild(resetBtn);
        container.appendChild(addRow);
        var footer = el(doc, 'div', 'flex items-center gap-3');
        footer.appendChild(statusEl);
        footer.appendChild(errorEl);
        footer.appendChild(saveBtn);
        container.appendChild(footer);
        clear(root);
        root.appendChild(container);

        function makeNumber(value, labelText, onChange) {
            var label = el(doc, 'label', 'rdai-catalog-limit');
            label.appendChild(el(doc, 'span', 'rdai-catalog-limit-label', labelText));
            var input = el(doc, 'input', 'rdai-catalog-limit-input');
            input.setAttribute('type', 'number');
            input.setAttribute('min', '1');
            input.value = (value === undefined || value === null) ? '' : String(value);
            input.addEventListener('change', function () {
                var raw = trim(input.value);
                onChange(raw === '' ? '' : raw);
            });
            label.appendChild(input);
            return label;
        }

        function buildRow(row) {
            var wrap = el(doc, 'div', 'rdai-catalog-row');
            var nameInput = el(doc, 'input', 'rdai-catalog-name');
            nameInput.setAttribute('type', 'text');
            nameInput.value = row.name;
            nameInput.addEventListener('change', function () {
                var next = trim(nameInput.value);
                if (next === row.name) return;
                run(function () { return renameModel(state.draft, row.name, next); });
            });
            wrap.appendChild(nameInput);
            wrap.appendChild(el(doc, 'span', 'rdai-catalog-origin',
                t(row.origin === 'added'
                    ? 'models_catalog_origin_added'
                    : 'models_catalog_origin_preset')));

            var capsWrap = el(doc, 'div', 'rdai-catalog-caps');
            for (var i = 0; i < VALID_CAPABILITIES.length; i++) {
                (function (cap) {
                    var label = el(doc, 'label', 'rdai-catalog-cap');
                    var box = el(doc, 'input', 'rdai-catalog-cap-box');
                    box.setAttribute('type', 'checkbox');
                    box.checked = (row.capabilities || []).indexOf(cap) >= 0;
                    box.addEventListener('change', function () {
                        var caps = (row.capabilities || []).slice();
                        var at = caps.indexOf(cap);
                        if (box.checked && at < 0) caps.push(cap);
                        if (!box.checked && at >= 0) caps.splice(at, 1);
                        if (!caps.length) return; // a model always keeps one tag
                        run(function () {
                            return editModel(state.draft, row.name, { capabilities: caps });
                        });
                    });
                    label.appendChild(box);
                    label.appendChild(el(doc, 'span', 'rdai-catalog-cap-label',
                        t('models_cap_tag_' + cap)));
                    capsWrap.appendChild(label);
                })(VALID_CAPABILITIES[i]);
            }
            wrap.appendChild(capsWrap);

            wrap.appendChild(makeNumber(row.context_window, t('models_catalog_context_window'),
                function (value) {
                    run(function () {
                        return editModel(state.draft, row.name, { context_window: value });
                    });
                }));
            wrap.appendChild(makeNumber(row.max_output_tokens, t('models_catalog_max_output'),
                function (value) {
                    run(function () {
                        return editModel(state.draft, row.name, { max_output_tokens: value });
                    });
                }));
            wrap.appendChild(button(doc, 'rdai-catalog-delete', t('models_custom_delete'),
                function () {
                    run(function () { return deleteModel(state.draft, row.name); });
                }));
            return wrap;
        }

        function buildHiddenRow(row) {
            var wrap = el(doc, 'div', 'rdai-catalog-hidden-row');
            wrap.appendChild(el(doc, 'span', 'rdai-catalog-hidden-name', row.name));
            wrap.appendChild(button(doc, 'rdai-catalog-restore', t('models_catalog_restore'),
                function () {
                    run(function () { return restoreModel(state.draft, row.name); });
                }));
            return wrap;
        }

        function renderRows() {
            clear(visibleWrap);
            clear(hiddenWrap);
            var view = catalogView(state.draft);
            for (var i = 0; i < view.visible.length; i++) {
                visibleWrap.appendChild(buildRow(view.visible[i]));
            }
            if (!view.visible.length) {
                visibleWrap.appendChild(el(doc, 'div', 'rdai-catalog-empty',
                    t('models_catalog_editor_empty')));
            }
            for (var h = 0; h < view.hidden.length; h++) {
                hiddenWrap.appendChild(buildHiddenRow(view.hidden[h]));
            }
            hiddenSection.style.display = view.hidden.length ? '' : 'none';
        }

        function sync() {
            saveBtn.disabled = !!state.saving;
            statusEl.textContent = state.saved ? t('models_save_success') : '';
            errorEl.textContent = state.error || '';
        }

        function render() {
            renderRows();
            sync();
        }

        function run(mutate) {
            try {
                state.draft = mutate();
                state.error = '';
                state.saved = false;
            } catch (error) {
                state.error = catalogErrorText(t, error);
            }
            render();
        }

        function reseed(fresh) {
            if (!fresh) return;
            if (typeof fresh.catalog !== 'undefined' || typeof fresh.seed !== 'undefined'
                    || typeof fresh.effective !== 'undefined') {
                state.draft = draftFromProvider(fresh);
                if (fresh.id) providerId = trim(fresh.id);
                return;
            }
            if (isArray(fresh.models)) {
                state.draft = {
                    seed: clone(state.draft.seed),
                    overrides: clone(fresh.models),
                    hidden: clone(state.draft.hidden)
                };
            }
        }

        function refresh(nextProvider) {
            provider = nextProvider || {};
            if (provider.id) providerId = trim(provider.id);
            state.draft = draftFromProvider(provider);
            state.error = '';
            state.saved = false;
            render();
        }

        function saveNow() {
            if (state.saving || disposed) return Promise.resolve(false);
            var payload;
            try {
                payload = buildCatalogPayload(providerId, state.draft);
            } catch (error) {
                state.error = catalogErrorText(t, error);
                state.saved = false;
                sync();
                return Promise.resolve(false);
            }
            state.saving = true;
            state.error = '';
            state.saved = false;
            sync();
            return Promise.resolve().then(function () {
                return saveFn(payload);
            }).then(function (fresh) {
                if (disposed) return true;
                state.saving = false;
                var reseedFresh = function (data) {
                    reseed(data);
                    if (!disposed) {
                        state.saved = true;
                        state.error = '';
                    }
                };
                if (reloadFn) {
                    return Promise.resolve().then(reloadFn).then(reseedFresh, function () {
                        reseedFresh(fresh);
                    }).then(function () {
                        if (!disposed) render();
                        return true;
                    });
                }
                reseedFresh(fresh);
                render();
                return true;
            }, function (error) {
                if (disposed) return false;
                state.saving = false;
                state.saved = false;
                state.error = saveErrorText(t, error);
                sync();
                return false;
            });
        }

        function getState() {
            return {
                draft: clone(state.draft),
                rows: catalogView(state.draft),
                error: state.error,
                saving: state.saving,
                saved: state.saved
            };
        }

        function dispose() {
            disposed = true;
            clear(root);
        }

        render();

        return {
            dispose: dispose,
            save: saveNow,
            refresh: refresh,
            getState: getState,
            addModel: function (entry) {
                run(function () { return addModel(state.draft, entry); });
            },
            editModel: function (name, patch) {
                run(function () { return editModel(state.draft, name, patch); });
            },
            deleteModel: function (name) {
                run(function () { return deleteModel(state.draft, name); });
            },
            restoreModel: function (name) {
                run(function () { return restoreModel(state.draft, name); });
            },
            renameModel: function (oldName, newName) {
                run(function () { return renameModel(state.draft, oldName, newName); });
            },
            resetDefault: function () {
                run(function () { return resetDefault(state.draft); });
            },
            element: container
        };
    }

    window.RdaiFunctionalModels = {
        // required exports
        buildFallbackPayload: buildFallbackPayload,
        buildCatalogPayload: buildCatalogPayload,
        mountFallback: mountFallback,
        mountCatalog: mountCatalog,
        // pure helpers used by the mounters and by tests
        escapeHtml: escapeHtml,
        draftFromProvider: draftFromProvider,
        catalogView: catalogView,
        effectiveEntries: effectiveEntries,
        editModel: editModel,
        addModel: addModel,
        deleteModel: deleteModel,
        restoreModel: restoreModel,
        renameModel: renameModel,
        resetDefault: resetDefault,
        readFallbackCapability: readFallbackCapability,
        moveChainRow: moveChainRow
    };
})();
