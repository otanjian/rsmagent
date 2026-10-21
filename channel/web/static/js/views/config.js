/* Basic settings tab.
   Split out of console.js. These are classic scripts sharing one global
   scope; see channel/web/README.md before changing the load order. */

// =====================================================================
// Config View
// =====================================================================
let configProviders = {};
let configApiBases = {};
let configApiKeys = {};
let configCurrentModel = '';
let cfgProviderValue = '';
let cfgModelValue = '';
let cfgReasoningEffortValue = 'high';
let configReasoningByModel = {};
// Remembers the custom model name the user typed per provider, so switching
// away from a provider (which rebuilds its model dropdown) and back does not
// lose an unsaved custom model. Keyed by provider id.
let configCustomModelByProvider = {};
// Same idea for the Models tab capability cards: remember the custom model the
// user typed per (capability, provider) and the provider active before the
// last switch, so switching vendors and back restores the custom model.
// Keyed by `${capabilityId}:${providerId}` -> custom model string.
let capabilityCustomModelMemory = {};
// Keyed by capabilityId -> provider id active before the current switch.
let capabilityLastProviderId = {};

// --- Custom dropdown helper ---
function initDropdown(el, options, selectedValue, onChange, opts) {
    // opts.placeholder: when set AND selectedValue is empty, render that text
    // in a dim style instead of auto-selecting options[0]. Useful for
    // "pick or empty" capabilities (asr / embedding) where we want the
    // user to make an explicit choice.
    opts = opts || {};
    // opts.readOnly: the control still renders the effective value but cannot
    // be opened or changed. Used for settings that are owned by another layer
    // (e.g. the default permission in database mode, which roles own).
    el._ddReadOnly = !!opts.readOnly;
    const textEl = el.querySelector('.cfg-dropdown-text');
    const menuEl = el.querySelector('.cfg-dropdown-menu');
    const selEl = el.querySelector('.cfg-dropdown-selected');
    // Optional avatar face in the trigger (opts.withAvatar). Each option then
    // carries an `agent` object so both the row and the trigger can paint it.
    const faceEl = el.querySelector('.cfg-dropdown-face');

    el._ddValue = selectedValue || '';
    el._ddOnChange = onChange;

    // The filter box is opt-in (opts.searchable). Without it `listEl` is the
    // menu itself, so an opting-out call site keeps byte-identical markup and
    // the rows are still direct children of `.cfg-dropdown-menu`.
    const search = opts.searchable ? _mountDropdownSearch(menuEl) : null;
    const listEl = search ? search.listEl : menuEl;
    el._ddQuery = '';
    if (search) {
        const applyQuery = () => {
            el._ddQuery = String(search.input.value || '').trim().toLowerCase();
            render();
        };
        search.input.addEventListener('input', (event) => {
            // A composition update is a syllable in progress, not the query yet;
            // filtering on it would rebuild the row list mid-character.
            if (event && event.isComposing) return;
            applyQuery();
        });
        search.input.addEventListener('compositionend', applyQuery);
        search.input.addEventListener('keydown',
            (event) => _dropdownSearchKeydown(event, el, search, render));
    }

    function paintFace(opt) {
        if (!faceEl) return;
        faceEl.innerHTML = (opt && opt.agent) ? agentAvatarHTML(opt.agent, 20) : '';
    }

    function buildItem(opt) {
        const item = document.createElement('div');
        item.className = 'cfg-dropdown-item' + (opt.value === el._ddValue ? ' active' : '');
        item.dataset.value = opt.value;
        // Hint is an optional dim secondary label rendered on the right
        // side of the row (e.g. friendly brand name next to a technical
        // model id). When absent the row degrades to the original
        // single-string layout.
        if (opt.agent) {
            const face = document.createElement('span');
            face.className = 'cfg-dropdown-item-face';
            face.innerHTML = agentAvatarHTML(opt.agent, 20);
            const labelEl = document.createElement('span');
            labelEl.className = 'cfg-dropdown-label';
            labelEl.textContent = opt.label;
            item.appendChild(face);
            item.appendChild(labelEl);
            // Optional trailing pill (e.g. a "default" marker) rendered
            // dim after the name.
            if (opt.badge) {
                const badgeEl = document.createElement('span');
                badgeEl.className = 'cfg-dropdown-badge';
                badgeEl.textContent = opt.badge;
                item.appendChild(badgeEl);
            }
        } else if (opt.hint) {
            const labelEl = document.createElement('span');
            labelEl.className = 'cfg-dropdown-label';
            labelEl.textContent = opt.label;
            const hintEl = document.createElement('span');
            hintEl.className = 'cfg-dropdown-hint';
            hintEl.textContent = opt.hint;
            item.appendChild(labelEl);
            item.appendChild(hintEl);
        } else {
            item.textContent = opt.label;
        }
        item.addEventListener('click', (e) => {
            e.stopPropagation();
            if (el._ddReadOnly) return;
            el._ddValue = opt.value;
            textEl.textContent = opt.label;
            // Now that a real option is picked, drop the muted placeholder
            // style — otherwise the chosen label stays grey (visible on
            // dropdowns that start in a placeholder state, e.g. the chat
            // fallback pickers).
            textEl.classList.remove('text-slate-400', 'dark:text-slate-500');
            paintFace(opt);
            listEl.querySelectorAll('.cfg-dropdown-item').forEach(i => i.classList.remove('active'));
            item.classList.add('active');
            el.classList.remove('open');
            if (el._ddOnChange) el._ddOnChange(opt.value);
        });
        return item;
    }

    // Paint the closed control (the trigger) from the full option list, so a
    // filtered-out but still-selected Agent keeps showing in the trigger: the
    // filter narrows the menu, never the current choice.
    function paintTrigger() {
        const sel = options.find(o => o.value === el._ddValue);
        if (sel) {
            textEl.textContent = sel.label;
            paintFace(sel);
            textEl.classList.remove('text-slate-400', 'dark:text-slate-500');
        } else if (opts.placeholder && !el._ddValue) {
            // No selection yet — show the placeholder in muted style.
            // Do NOT write a fallback value, so the dropdown stays
            // "unsaved" until the user explicitly picks.
            textEl.textContent = opts.placeholder;
            paintFace(null);
            textEl.classList.add('text-slate-400', 'dark:text-slate-500');
        } else {
            textEl.textContent = options[0] ? options[0].label : '--';
            paintFace(options[0]);
            textEl.classList.remove('text-slate-400', 'dark:text-slate-500');
            if (options[0]) el._ddValue = options[0].value;
        }
    }

    function render() {
        const needle = el._ddQuery;
        const shown = needle
            ? options.filter(opt => _dropdownOptionMatches(opt, needle))
            : options;
        listEl.innerHTML = '';
        shown.forEach(opt => listEl.appendChild(buildItem(opt)));
        // The keyboard highlight is positional, so it cannot outlive the rows it
        // pointed at; a re-filter starts from "nothing highlighted".
        if (search) search.showEmpty(shown.length === 0 && !!needle);
        paintTrigger();
    }

    render();

    if (!el._ddBound) {
        // A click inside the menu belongs to the menu. The document-level
        // handler below closes every open dropdown whose click it hears, and
        // the trigger and the rows already stop their clicks from reaching it;
        // the opt-in filter box did not, so the click that put the caret in it
        // closed the menu (and took the box) before a character could be typed.
        menuEl.addEventListener('click', (e) => e.stopPropagation());
        selEl.addEventListener('click', (e) => {
            e.stopPropagation();
            if (el._ddReadOnly) return;
            document.querySelectorAll('.cfg-dropdown.open').forEach(d => { if (d !== el) d.classList.remove('open'); });
            const willOpen = !el.classList.contains('open');
            if (willOpen) {
                // Flip the menu above the control when it would otherwise be
                // clipped against the viewport bottom (e.g. the last channel's
                // config dropdown sitting near the window edge).
                const rect = el.getBoundingClientRect();
                const below = window.innerHeight - rect.bottom;
                const menuH = Math.min(menuEl.scrollHeight || 240, 280) + 8;
                el.classList.toggle('drop-up', below < menuH && rect.top > below);
            }
            el.classList.toggle('open');
        });
        el._ddBound = true;
    }
}

document.addEventListener('click', () => {
    document.querySelectorAll('.cfg-dropdown.open').forEach(d => d.classList.remove('open'));
});

function getDropdownValue(el) { return el._ddValue || ''; }

// --- Config init ---
function initConfigView(data) {
    configProviders = data.providers || {};
    configApiBases = data.api_bases || {};
    configApiKeys = data.api_keys || {};
    configCurrentModel = data.model || '';
    configReasoningByModel = data.reasoning_effort_by_model || {};
    cfgReasoningEffortValue = data.reasoning_effort || 'high';

    const providerEl = document.getElementById('cfg-provider');
    const providerOpts = Object.entries(configProviders).map(([pid, p]) => ({ value: pid, label: localizedLabel(p.label) }));

    // if use_linkai is enabled, always select linkai as the provider
    // Otherwise prefer bot_type from config, fall back to model-based detection
    const detected = data.use_linkai ? 'linkai'
        : (data.bot_type && configProviders[data.bot_type] ? data.bot_type : detectProvider(configCurrentModel));
    cfgProviderValue = detected || (providerOpts[0] ? providerOpts[0].value : '');

    initDropdown(providerEl, providerOpts, cfgProviderValue, onProviderChange);

    onProviderChange(cfgProviderValue);
    syncModelSelection(configCurrentModel);

    document.getElementById('cfg-max-tokens').value = data.agent_max_context_tokens ?? 200000;
    document.getElementById('cfg-max-turns').value = data.agent_max_context_turns || 30;
    document.getElementById('cfg-max-steps').value = data.agent_max_steps || 100;
    const thinkingEl = document.getElementById('cfg-enable-thinking');
    thinkingEl.checked = data.enable_thinking === true;
    if (!thinkingEl._cfgReasoningBound) {
        thinkingEl.addEventListener('change', syncReasoningEffortOptions);
        thinkingEl._cfgReasoningBound = true;
    }
    const customModelEl = document.getElementById('cfg-model-custom');
    if (customModelEl && !customModelEl._cfgReasoningBound) {
        customModelEl.addEventListener('input', () => {
            // Remember the typed custom model for the current provider so a
            // provider switch and switch-back doesn't lose it.
            if (cfgModelValue === '__custom__') {
                configCustomModelByProvider[cfgProviderValue] = customModelEl.value.trim();
            }
            syncReasoningEffortOptions();
        });
        customModelEl._cfgReasoningBound = true;
    }
    syncReasoningEffortOptions();
    document.getElementById('cfg-subagent').checked = data.subagent_enabled !== false;
    document.getElementById('cfg-self-evolution').checked = data.self_evolution_enabled === true;

    // Reflect the current UI language (already resolved, may include the user's
    // local choice) on the selector so it stays in sync with the top-right toggle.
    const langSel = document.getElementById('cfg-lang-select');
    if (langSel) {
        initDropdown(
            langSel,
            [{ value: 'zh', label: '简体中文' }, { value: 'zh-Hant', label: '繁體中文' }, { value: 'en', label: 'English' }],
            currentLang,
            (val) => setLanguage(val)
        );
    }

    // Default permission mode for new conversations. Owned by role resource
    // grants: rendered read-only so a tenant user cannot widen what their
    // session may run. Editable only when the server explicitly allows it.
    const permEl = document.getElementById('cfg-permission');
    if (permEl) {
        const editable = data.permission_mode_editable === true;
        const offered = data.permission_modes && data.permission_modes.length
            ? data.permission_modes
            : Object.keys(PERMISSION_META);
        const permOpts = Object.keys(PERMISSION_META)
            .filter(mode => offered.includes(mode))
            .map(mode => ({ value: mode, label: t(PERMISSION_META[mode].key) }));
        initDropdown(
            permEl,
            permOpts,
            data.agent_permission_mode || 'full-access',
            editable ? saveGlobalPermission : null,
            { readOnly: !editable }
        );
        permEl.classList.toggle('cfg-dropdown-readonly', !editable);
        if (editable) permEl.removeAttribute('aria-disabled');
        else permEl.setAttribute('aria-disabled', 'true');
        const descEl = document.getElementById('cfg-permission-desc');
        const roleDescEl = document.getElementById('cfg-permission-role-desc');
        if (descEl) descEl.classList.toggle('hidden', !editable);
        if (roleDescEl) roleDescEl.classList.toggle('hidden', editable);
    }
}

function detectProvider(model) {
    if (!model) return Object.keys(configProviders)[0] || '';
    for (const [pid, p] of Object.entries(configProviders)) {
        if (pid === 'linkai') continue;
        if (p.models && p.models.includes(model)) return pid;
    }
    return Object.keys(configProviders)[0] || '';
}

function onProviderChange(pid) {
    cfgProviderValue = pid || getDropdownValue(document.getElementById('cfg-provider'));
    const p = configProviders[cfgProviderValue];
    if (!p) return;

    const customTip = document.getElementById('cfg-custom-tip');
    if (customTip) customTip.classList.toggle('hidden', cfgProviderValue !== 'custom');

    const modelEl = document.getElementById('cfg-model-select');
    const modelOpts = (p.models || []).map(m => ({ value: m, label: m }));
    modelOpts.push({ value: '__custom__', label: t('config_custom_option') });

    // Restore a custom model the user typed for this provider earlier in the
    // session (kept in configCustomModelByProvider). Fall back to the first
    // preset. For a custom provider with no preset models the picker only has
    // the "__custom__" entry, so a remembered value is the only way its model
    // survives a provider switch.
    const rememberedCustom = configCustomModelByProvider[cfgProviderValue];
    const initialModelValue = rememberedCustom
        ? '__custom__'
        : (modelOpts[0] ? modelOpts[0].value : '');

    initDropdown(modelEl, modelOpts, initialModelValue, onModelSelectChange);

    // API Key
    const keyField = p.api_key_field;
    const keyWrap = document.getElementById('cfg-api-key-wrap');
    const keyInput = document.getElementById('cfg-api-key');

    // Only LinkAI (an aggregation platform) gets a link to its console for
    // managing the aggregated key; other providers manage keys on their sites.
    const cfgManageKey = document.getElementById('cfg-manage-key');
    if (cfgManageKey) cfgManageKey.classList.toggle('hidden', cfgProviderValue !== 'linkai');
    if (keyField) {
        keyWrap.classList.remove('hidden');
        keyInput.classList.add('cfg-key-masked');
        const maskedVal = configApiKeys[keyField] || '';
        keyInput.value = maskedVal;
        keyInput.dataset.field = keyField;
        keyInput.dataset.masked = maskedVal ? '1' : '';
        keyInput.dataset.maskedVal = maskedVal;
        const toggleIcon = document.querySelector('#cfg-api-key-toggle i');
        if (toggleIcon) toggleIcon.className = 'fas fa-eye text-xs';

        if (!keyInput._cfgBound) {
            keyInput.addEventListener('focus', function() {
                if (this.dataset.masked === '1') {
                    this.value = '';
                    this.dataset.masked = '';
                    this.classList.remove('cfg-key-masked');
                }
            });
            keyInput.addEventListener('blur', function() {
                if (!this.value.trim() && this.dataset.maskedVal) {
                    this.value = this.dataset.maskedVal;
                    this.dataset.masked = '1';
                    this.classList.add('cfg-key-masked');
                }
            });
            keyInput.addEventListener('input', function() {
                this.dataset.masked = '';
            });
            keyInput._cfgBound = true;
        }
    } else {
        keyWrap.classList.add('hidden');
        keyInput.value = '';
        keyInput.dataset.field = '';
    }

    // API Base
    const apiBaseInput = document.getElementById('cfg-api-base');
    if (p.api_base_key) {
        document.getElementById('cfg-api-base-wrap').classList.remove('hidden');
        apiBaseInput.value = configApiBases[p.api_base_key] || p.api_base_default || '';
        // Hint the version-path tail (e.g. /v1) so users are reminded to
        // include it themselves. We don't auto-rewrite anything server-side.
        apiBaseInput.placeholder = p.api_base_placeholder || 'https://...';
    } else {
        document.getElementById('cfg-api-base-wrap').classList.add('hidden');
        apiBaseInput.value = '';
        apiBaseInput.placeholder = 'https://...';
    }

    onModelSelectChange(initialModelValue, { restoredCustom: rememberedCustom });
    syncReasoningEffortOptions();
}

function onModelSelectChange(val, opts) {
    opts = opts || {};
    cfgModelValue = val || getDropdownValue(document.getElementById('cfg-model-select'));
    const customWrap = document.getElementById('cfg-model-custom-wrap');
    const customInput = document.getElementById('cfg-model-custom');
    if (cfgModelValue === '__custom__') {
        customWrap.classList.remove('hidden');
        // When switching back to a provider we restore the remembered value;
        // otherwise this is a fresh pick of "custom" and we focus for input.
        if (opts.restoredCustom) {
            customInput.value = opts.restoredCustom;
        } else {
            customInput.focus();
        }
    } else {
        customWrap.classList.add('hidden');
        customInput.value = '';
    }
    syncReasoningEffortOptions();
}

function syncModelSelection(model) {
    const p = configProviders[cfgProviderValue];
    if (!p) return;

    const modelEl = document.getElementById('cfg-model-select');
    if (p.models && p.models.includes(model)) {
        const modelOpts = (p.models || []).map(m => ({ value: m, label: m }));
        modelOpts.push({ value: '__custom__', label: t('config_custom_option') });
        initDropdown(modelEl, modelOpts, model, onModelSelectChange);
        cfgModelValue = model;
        document.getElementById('cfg-model-custom-wrap').classList.add('hidden');
    } else {
        cfgModelValue = '__custom__';
        const modelOpts = (p.models || []).map(m => ({ value: m, label: m }));
        modelOpts.push({ value: '__custom__', label: t('config_custom_option') });
        initDropdown(modelEl, modelOpts, '__custom__', onModelSelectChange);
        document.getElementById('cfg-model-custom-wrap').classList.remove('hidden');
        document.getElementById('cfg-model-custom').value = model;
        // Seed the per-provider memory so switching away and back keeps it.
        if (model) configCustomModelByProvider[cfgProviderValue] = model;
    }
    syncReasoningEffortOptions();
}

function syncReasoningEffortOptions() {
    const wrap = document.getElementById('cfg-reasoning-effort-wrap');
    const el = document.getElementById('cfg-reasoning-effort');
    if (!wrap || !el) return;

    const provider = configProviders[cfgProviderValue] || {};
    const selectedModel = getSelectedModel();
    const reasoningByModel = provider.reasoning_by_model || {};
    const reasoning = reasoningByModel[selectedModel] || provider.reasoning || {};
    const options = reasoning.supported ? (reasoning.options || []) : [];
    const thinkingEl = document.getElementById('cfg-enable-thinking');

    if (options.length) {
        const values = options.map(opt => opt.value);
        // Prefer this model's own saved effort (per-model config) so switching
        // vendors never reinterprets a value set for a different model. Key is
        // the lowercased model name, matching the backend resolve path.
        const savedForModel = configReasoningByModel[`${cfgProviderValue}:${selectedModel.trim().toLowerCase()}`]
            || configReasoningByModel[cfgProviderValue + ':' + selectedModel];
        const saved = savedForModel || cfgReasoningEffortValue;
        // Fall back to the active model's native enum when the saved value is
        // not valid here. Resolved even while hidden so a save never writes
        // another model's enum under this model's key.
        cfgReasoningEffortValue = values.includes(saved) ? saved : (reasoning.default || options[0].value);
    }

    // Effort only shapes a thinking pass, so the field follows the toggle.
    if (!thinkingEl || !thinkingEl.checked || !options.length) {
        wrap.classList.add('hidden');
        return;
    }

    wrap.classList.remove('hidden');
    initDropdown(
        el,
        options.map(opt => ({ value: opt.value, label: opt.label || opt.value })),
        cfgReasoningEffortValue,
        (val) => { cfgReasoningEffortValue = val; }
    );
}

function getSelectedModel() {
    if (cfgModelValue === '__custom__') {
        return document.getElementById('cfg-model-custom').value.trim();
    }
    return cfgModelValue;
}

function toggleApiKeyVisibility() {
    const input = document.getElementById('cfg-api-key');
    const icon = document.querySelector('#cfg-api-key-toggle i');
    if (input.classList.contains('cfg-key-masked')) {
        input.classList.remove('cfg-key-masked');
        icon.className = 'fas fa-eye-slash text-xs';
    } else {
        input.classList.add('cfg-key-masked');
        icon.className = 'fas fa-eye text-xs';
    }
}

function showStatus(elId, msgKey, isError) {
    const el = document.getElementById(elId);
    el.textContent = t(msgKey);
    el.classList.toggle('text-red-500', !!isError);
    el.classList.toggle('text-primary-500', !isError);
    el.classList.remove('opacity-0');
    // Warning messages (errors) should stay visible, success messages auto-hide
    if (!isError) {
        setTimeout(() => el.classList.add('opacity-0'), 2500);
    }
}

function saveModelConfig() {
    const model = getSelectedModel();
    if (!model) return;

    const updates = { model: model };
    const p = configProviders[cfgProviderValue];
    updates.use_linkai = (cfgProviderValue === 'linkai');
    if (cfgProviderValue === 'linkai') {
        updates.bot_type = '';
    } else {
        updates.bot_type = cfgProviderValue;
    }
    if (p && p.api_base_key) {
        const base = document.getElementById('cfg-api-base').value.trim();
        if (base) updates[p.api_base_key] = base;
    }
    if (p && p.api_key_field) {
        const keyInput = document.getElementById('cfg-api-key');
        const rawVal = keyInput.value.trim();
        if (rawVal && keyInput.dataset.masked !== '1') {
            updates[p.api_key_field] = rawVal;
        }
    }

    const btn = document.getElementById('cfg-model-save');
    btn.disabled = true;
    fetch('/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ updates })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            configCurrentModel = model;
            if (data.applied) {
                const keyInput = document.getElementById('cfg-api-key');
                Object.entries(data.applied).forEach(([k, v]) => {
                    if (k === 'model') return;
                    if (k.includes('api_key')) {
                        const masked = v.length > 8
                            ? v.substring(0, 4) + '*'.repeat(v.length - 8) + v.substring(v.length - 4)
                            : v;
                        configApiKeys[k] = masked;
                        if (keyInput.dataset.field === k) {
                            keyInput.value = masked;
                            keyInput.dataset.masked = '1';
                            keyInput.dataset.maskedVal = masked;
                            keyInput.classList.add('cfg-key-masked');
                            const toggleIcon = document.querySelector('#cfg-api-key-toggle i');
                            if (toggleIcon) toggleIcon.className = 'fas fa-eye text-xs';
                        }
                    } else {
                        configApiBases[k] = v;
                    }
                });
            }
            showStatus('cfg-model-status', 'config_saved', false);
        } else {
            showStatus('cfg-model-status', 'config_save_error', true);
        }
    })
    .catch(() => showStatus('cfg-model-status', 'config_save_error', true))
    .finally(() => { btn.disabled = false; });
}

function saveAgentConfig() {
    const effortKey = `${cfgProviderValue}:${getSelectedModel().trim().toLowerCase()}`;
    const mergedEffortByModel = Object.assign({}, configReasoningByModel, { [effortKey]: cfgReasoningEffortValue });
    const updates = {
        agent_max_context_tokens: parseInt(document.getElementById('cfg-max-tokens').value) || 0,
        agent_max_context_turns: parseInt(document.getElementById('cfg-max-turns').value) || 30,
        agent_max_steps: parseInt(document.getElementById('cfg-max-steps').value) || 100,
        enable_thinking: document.getElementById('cfg-enable-thinking').checked,
        // Persist effort per model (merge with the existing map so other
        // models' saved efforts survive the flat config save).
        reasoning_effort_by_model: mergedEffortByModel,
        subagent_enabled: document.getElementById('cfg-subagent').checked,
        self_evolution_enabled: document.getElementById('cfg-self-evolution').checked,
    };

    const btn = document.getElementById('cfg-agent-save');
    btn.disabled = true;
    fetch('/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ updates })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            // Reflect the merged map so a later model switch shows/uses the
            // just-saved value instead of a stale in-memory one.
            configReasoningByModel = mergedEffortByModel;
            showStatus('cfg-agent-status', 'config_saved', false);
        } else {
            showStatus('cfg-agent-status', 'config_save_error', true);
        }
    })
    .catch(() => showStatus('cfg-agent-status', 'config_save_error', true))
    .finally(() => { btn.disabled = false; });
}

// Persist the instance-wide default permission mode. Sessions that never pinned
// their own follow it, so the composer chip is refreshed afterwards.
function saveGlobalPermission(mode) {
    fetch('/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ updates: { agent_permission_mode: mode } })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'success') {
            showStatus('cfg-permission-status', 'config_saved', false);
            refreshSessionSettings();
        } else {
            showStatus('cfg-permission-status', 'config_save_error', true);
        }
    })
    .catch(() => showStatus('cfg-permission-status', 'config_save_error', true));
}

function savePasswordConfig() { openAccountPassword(); }

function loadConfigView() {
    fetch('/config').then(r => r.json()).then(data => {
        if (data.status !== 'success') return;
        appConfig = data;
        initConfigView(data);
    }).catch(() => {});
    loadExternalApi();
}

// ---- Chat API card: the OpenAI-compatible /v1 endpoint ----------------------

let externalApiState = { enabled: false, api_key: '' };
let externalApiLang = 'curl';
let externalApiRequest = 0;

function _externalApiRevealed() {
    return !document.getElementById('cfg-api-token').classList.contains('cfg-key-masked');
}

function externalApiExample(lang, reveal) {
    const base = `${window.location.origin}/v1`;
    const raw = externalApiState.issued?.api_key || 'sak_YOUR_SERVICE_ACCOUNT_KEY';
    const key = reveal ? raw : 'sak_YOUR_SERVICE_ACCOUNT_KEY';
    if (lang === 'python') {
        return [
            'import requests',
            '',
            'resp = requests.post(',
            `    "${base}/chat/completions",`,
            `    headers={"Authorization": "Bearer ${key}"},`,
            '    json={"messages": [{"role": "user", "content": "Hello"}]},',
            ')',
            'print(resp.json()["choices"][0]["message"]["content"])',
        ].join('\n');
    }
    if (lang === 'node') {
        return [
            `const resp = await fetch("${base}/chat/completions", {`,
            '  method: "POST",',
            `  headers: { "Authorization": "Bearer ${key}", "Content-Type": "application/json" },`,
            '  body: JSON.stringify({ messages: [{ role: "user", content: "Hello" }] }),',
            '});',
            'const data = await resp.json();',
            'console.log(data.choices[0].message.content);',
        ].join('\n');
    }
    return [
        `curl ${base}/chat/completions \\`,
        `  -H "Authorization: Bearer ${key}" \\`,
        '  -H "Content-Type: application/json" \\',
        `  -d '{"messages": [{"role": "user", "content": "Hello"}]}'`,
    ].join('\n');
}

function renderExternalApi() {
    const state = externalApiState;
    const enabled = document.getElementById('cfg-api-enabled');
    enabled.checked = !!state.enabled; enabled.disabled = !state.can_configure;
    document.getElementById('cfg-api-detail').classList.remove('hidden');
    document.getElementById('cfg-api-base-url').value = window.location.origin + '/v1';
    document.getElementById('cfg-api-user').value = state.user_id || '';
    document.getElementById('cfg-api-token').value = state.issued?.api_key || '';
    document.getElementById('cfg-api-example').textContent = externalApiExample(externalApiLang, false);
    const keys = document.getElementById('cfg-api-keys'); keys.replaceChildren();
    (state.keys || []).forEach(key => {
        const row = document.createElement('div'); row.className = 'flex gap-3 items-center text-sm';
        const name = document.createElement('code'); name.textContent = key.masked; row.appendChild(name);
        for (const action of ['rotate', 'revoke']) {
            const button = document.createElement('button'); button.className = 'agent-btn';
            button.textContent = t('config_api_' + action);
            button.onclick = () => {
                const target = {scope: menuScope(), user: state.user_id};
                showConfirmDialog({title: button.textContent, message: t('config_api_key_change_confirm'), onConfirm: () => _postExternalApi(action, key.id, target)});
            };
            row.appendChild(button);
        }
        keys.appendChild(row);
    });
}

async function loadExternalApi() {
    const request = ++externalApiRequest;
    const scope = menuScope();
    try {
        const user = document.getElementById('cfg-api-user')?.value || '';
        const response = await fetch('/api/external_api?user_id=' + encodeURIComponent(user));
        const data = await response.json();
        if (scope !== menuScope() || request !== externalApiRequest) return;
        if (!response.ok || data.status !== 'success') throw new Error(data.message || t('config_api_failed'));
        externalApiState = data; renderExternalApi();
    } catch (error) { if (scope === menuScope() && request === externalApiRequest) document.getElementById('cfg-api-error').textContent = error.message; }
}

async function _postExternalApi(action, credentialId, target) {
    const request = ++externalApiRequest;
    target = target || {scope: menuScope(), user: document.getElementById('cfg-api-user').value};
    const scope = target.scope;
    if (scope !== menuScope() || request !== externalApiRequest) return;
    try {
        const response = await fetch('/api/external_api', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action, credential_id:credentialId, user_id:target.user})});
        const data = await response.json();
        if (scope !== menuScope() || request !== externalApiRequest) return;
        if (!response.ok || data.status !== 'success') throw new Error(data.message || t('config_api_failed'));
        externalApiState = data; renderExternalApi();
        document.getElementById('cfg-api-error').textContent = '';
    } catch (error) { if (scope === menuScope() && request === externalApiRequest) document.getElementById('cfg-api-error').textContent = error.message; }
}

function onExternalApiToggle(input) {
    // The switch keeps its state until the user confirms either way.
    const enable = input.checked;
    input.checked = !enable;
    const target = {scope: menuScope(), user: document.getElementById('cfg-api-user').value};
    showConfirmDialog({
        title: t(enable ? 'config_api_enable' : 'config_api_disable'),
        message: t(enable ? 'config_api_enable_confirm' : 'config_api_disable_confirm'),
        okText: t(enable ? 'config_api_enable_ok' : 'config_api_disable_ok'),
        tone: enable ? 'primary' : 'danger',
        onConfirm: () => _postExternalApi(enable ? 'enable' : 'disable', undefined, target),
    });
}

function createServiceApiKey() { return _postExternalApi('create'); }

function switchExternalApiExample(lang) {
    externalApiLang = lang;
    renderExternalApi();
}

function toggleExternalApiKey() {
    const input = document.getElementById('cfg-api-token');
    const masked = input.classList.toggle('cfg-key-masked');
    document.querySelector('#cfg-api-token-toggle i').className = masked ? 'fas fa-eye text-xs' : 'fas fa-eye-slash text-xs';
    renderExternalApi();
}

function copyExternalApiField(id, btn) {
    const text = id === 'cfg-api-example'
        ? externalApiExample(externalApiLang, true)
        : document.getElementById(id).value;
    copyToClipboard(text).then(() => {
        const icon = btn.querySelector('i');
        const original = icon.className;
        icon.className = original.replace(/far fa-copy/, 'fas fa-check');
        setTimeout(() => { icon.className = original; }, 1200);
    }).catch(() => {});
}

function switchConfigTab(tab) {
    if (!['basic', 'models', 'catalog'].includes(tab)) return;
    if (tab !== 'catalog' && !_modelsManageAllowed()) tab = 'catalog';
    ['basic', 'models', 'catalog'].forEach(name => {
        document.getElementById(`config-tab-${name}`)?.classList.toggle('active', name === tab);
        document.getElementById(`config-panel-${name}`)?.classList.toggle('hidden', name !== tab);
    });
    if (tab === 'models') loadModelsView();
    if (tab === 'catalog') loadMemberCatalog();
    // Re-pull /config when returning to Basic: a provider added on the Models
    // tab must show up in the basic main-model provider picker without a manual
    // page refresh. loadConfigView re-renders from the fresh provider list.
    if (tab === 'basic') loadConfigView();
    routeNoteTab('config', tab);
}


window.addEventListener('account-invalidated', () => { ++externalApiRequest; externalApiState = {}; document.getElementById('cfg-api-user').value = ''; document.getElementById('cfg-api-token').value = ''; document.getElementById('cfg-api-example').textContent = ''; });
