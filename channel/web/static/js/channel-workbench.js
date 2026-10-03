// Shared channel presentation module (change upgrade-personal-channel-workbench,
// task 3.1).
//
// The tenant console and the member workbench show the *same* objects — a
// channel card, a type icon, the credential fields a type declares, and the
// 扫码 / 手工 mode strip — from two different request contexts. This file owns
// that presentation once, so the two surfaces cannot drift into two credential
// field definitions or two scan fixes.
//
// Three rules this file exists to keep:
//
// 1. **No ambient state.** Nothing here reads ``channelScope()``,
//    ``tenantChannelDraft``, ``agentCatalog``, ``tenantChannelTypes`` or
//    ``tenantChannelInstances``. Every input arrives as an argument, so the
//    personal controller cannot accidentally render the public draft and the
//    public controller cannot accidentally render the member's list.
// 2. **No fixed DOM ids.** Every attribute the markup carries is namespaced by
//    a ``prefix`` the caller chooses (``tenant-channel`` / ``personal-channel``).
//    A card rendered for one surface therefore cannot be found — or overwritten
//    — by the other surface's collectors.
// 3. **No requests, no drafts.** The module builds markup and reads markup. It
//    never fetches, never persists, and never decides which verb is allowed: the
//    server's ``actions`` projection stays the only source of that.
//
// ``escape`` and ``t`` are injected by the caller (falling back to the page
// globals) so the builders are pure enough to be exercised directly.
(function () {
    'use strict';

    const DEFAULT_ICON = 'fa-tower-broadcast';
    const DEFAULT_COLOR = 'primary';

    //: Attribute names the credential inputs carry, per surface. Kept as a pair
    //: so a collector and its builder cannot disagree about which attribute they
    //: mean.
    function fieldAttr(prefix) { return 'data-' + prefix + '-field'; }
    function requiredAttr(prefix) { return 'data-' + prefix + '-required'; }
    function heldAttr(prefix) { return 'data-' + prefix + '-held'; }

    function _escape(opts) {
        if (opts && typeof opts.escape === 'function') return opts.escape;
        if (typeof escapeHtml === 'function') {
            return value => escapeHtml(String(value === undefined || value === null ? '' : value));
        }
        return value => String(value === undefined || value === null ? '' : value);
    }

    function _t(opts) {
        if (opts && typeof opts.t === 'function') return opts.t;
        if (typeof t === 'function') return t;
        return key => String(key === undefined || key === null ? '' : key);
    }

    function _lang(opts) {
        if (opts && opts.lang) return opts.lang;
        if (typeof currentLang === 'string' && currentLang) return currentLang;
        return 'zh';
    }

    //: Copy keys the shared builders need. They are page copy, so a caller whose
    //: surface words things differently passes its own; the defaults stay the
    //: tenant console's wording so a caller that says nothing renders exactly
    //: what it always did.
    const HELD_HINT_KEY = 'tenant_channel_held_hint';
    const SECRET_NOTE_KEY = 'tenant_channel_secret_note';

    function _heldHintKey(o) {
        return (o && o.heldHintKey) || HELD_HINT_KEY;
    }

    function _secretNoteKey(o) {
        return (o && o.secretNoteKey) || SECRET_NOTE_KEY;
    }

    // ---- pure descriptors --------------------------------------------------

    // The server's own declaration for a type, or null. The console must never
    // keep a second copy of a type's field list: it would drift from what the
    // write path validates.
    function typeSpec(types, channelType) {
        const list = Array.isArray(types) ? types : [];
        return list.find(item => item && item.channel_type === channelType) || null;
    }

    function typeLabel(spec, channelType, lang) {
        if (!spec || !spec.label) return String(channelType || '');
        return String(spec.label[lang] || spec.label.en || spec.label.zh
            || channelType || '');
    }

    function fieldLabel(field, lang) {
        if (!field || !field.label) return String((field && field.key) || '');
        return String(field.label[lang] || field.label.en || field.label.zh
            || field.key || '');
    }

    // Icon / colour come from the same declaration the platform page uses, with
    // a neutral fallback for a type this deployment does not describe.
    function appearance(spec) {
        return {
            icon: (spec && spec.icon) || DEFAULT_ICON,
            color: (spec && spec.color) || DEFAULT_COLOR,
        };
    }

    // The type list a form may offer is exactly what the server sent, so an
    // unsupported type can never be submitted.
    function typeOptions(types, lang) {
        const list = Array.isArray(types) ? types : [];
        return list.map(spec => ({
            value: spec.channel_type,
            label: typeLabel(spec, spec.channel_type, lang),
        }));
    }

    // Agent candidates for a *public* instance. The personal workbench does NOT
    // use this: its candidates come from the server's own ``agent_options``,
    // already narrowed to the member's private agents.
    function agentOptions(catalog, selected, opts) {
        const t = _t(opts);
        const noneKey = (opts && opts.noneKey) || 'tenant_channel_agent_none';
        const list = Array.isArray(catalog) ? catalog : [];
        return [{ value: '', label: t(noneKey) }].concat(list.map(agent => ({
            value: agent.id,
            label: agent.name ? agent.name + ' (' + agent.id + ')' : agent.id,
        })));
    }

    // A type supports scanning only when both tables know it: the start function
    // to run and the copy to show. Requiring both means the two cannot drift
    // into a half-registered type — which is how a WeCom card came to be
    // rendered with Feishu's wording.
    function supportsScan(scanTypes, channelType) {
        const table = scanTypes || {};
        return !!table[channelType || ''];
    }

    function hasScanCopy(scanCopyTable, channelType) {
        const table = scanCopyTable || {};
        return !!table[channelType || ''];
    }

    // A type is offered a scan entry only when it can both start and describe
    // one. Callers pass both tables so the check is one place, not two.
    function scanReady(scanTypes, scanCopyTable, channelType) {
        return supportsScan(scanTypes, channelType)
            && hasScanCopy(scanCopyTable, channelType);
    }

    function scanCopy(scanCopyTable, channelType) {
        const table = scanCopyTable || {};
        return table[channelType || ''] || null;
    }

    function scanStartFor(scanTypes, channelType) {
        const table = scanTypes || {};
        return table[channelType || ''] || '';
    }

    // The name a scan-created channel gets without asking the operator for one.
    // The app id is the only part of a scan result a person can tell apart in a
    // list, so the name is the type plus its last four characters.
    function autoName(spec, channelType, credentials, lang) {
        const label = typeLabel(spec, channelType, lang);
        const fields = (spec && spec.credential_fields) || [];
        const firstKey = fields.length ? fields[0].key : '';
        const source = String((credentials || {})[firstKey] || '');
        const tail = source.slice(-4);
        return tail ? label + ' · ' + tail : label;
    }

    // Required credential fields the operator has left empty, for a create. An
    // edit is deliberately exempt: a blank secret there means "keep the stored
    // value", which the server honours by merging over the existing bundle.
    function missingRequiredFields(fields, collected) {
        const provided = collected || {};
        return (Array.isArray(fields) ? fields : []).filter(
            field => field && field.required && !String(provided[field.key] || '').trim());
    }

    // Read the credential inputs a form rendered. Scoped to ``root`` and to the
    // caller's own prefix, so the personal form can never collect the public
    // form's values (or the other way round).
    function collectFields(root, prefix) {
        const out = {};
        if (!root || !root.querySelectorAll) return out;
        const attr = fieldAttr(prefix);
        root.querySelectorAll('[' + attr + ']').forEach(el => {
            const key = el.getAttribute(attr);
            const value = (el.value || '').trim();
            if (value) out[key] = value;
        });
        return out;
    }

    // Build the create/update body. Only the fields the caller actually supplied
    // are sent, so editing a display name never blanks a stored secret.
    function payload(form) {
        const spec = form || {};
        const out = {};
        if (spec.display_name !== undefined) out.display_name = spec.display_name;
        if (spec.agent_id !== undefined) out.agent_id = spec.agent_id;
        if (spec.credentials !== undefined) out.credentials = spec.credentials;
        if (spec.expected_version !== undefined) out.expected_version = spec.expected_version;
        out.recent_password = spec.recent_password || '';
        // Only sent when a scan minted one: the server treats it as standing in
        // for the password, so an empty value must stay absent rather than sent.
        if (spec.scan_ticket) out.scan_ticket = spec.scan_ticket;
        return out;
    }

    // ---- markup builders ---------------------------------------------------

    // The card shell both surfaces use, so the two lists are recognisably the
    // same interface. Pure string: no lookup, no global.
    function cardShell(opts) {
        const o = opts || {};
        const escape = _escape(o);
        const iid = o.iid || '';
        const icon = o.icon || DEFAULT_ICON;
        const color = o.color || DEFAULT_COLOR;
        const statusDot = o.statusDot || 'bg-primary-400';
        return `
            <div class="flex items-center gap-4${o.headerMb ? ' mb-5' : ''}">
                <div class="w-10 h-10 rounded-xl bg-${color}-50 dark:bg-${color}-900/20 flex items-center justify-center flex-shrink-0">
                    <i class="fas ${icon} text-${color}-500 text-base"></i>
                </div>
                <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2">
                        <span class="font-semibold text-slate-800 dark:text-slate-100">${escape(o.label)}</span>
                        <span class="w-2 h-2 rounded-full ${statusDot}"></span>
                        ${o.statusText || ''}
                    </div>
                    <p class="text-xs text-slate-500 dark:text-slate-400 mt-0.5 font-mono">${escape(o.subtitle || iid)}</p>
                </div>
                ${o.actionsHtml || ''}
            </div>
            ${o.bodyHtml || ''}`;
    }

    // One credential input, from the server's field declaration.
    function fieldInput(opts) {
        const o = opts || {};
        const escape = _escape(o);
        const t = _t(o);
        const lang = _lang(o);
        const field = o.field || {};
        const prefix = o.prefix || 'tenant-channel';
        const val = o.value === undefined || o.value === null ? '' : String(o.value);
        // Required-ness is the server's declaration; without it the console
        // would keep a second copy of the minimum set and drift from what the
        // server enforces on save.
        const required = field.required ? ' ' + requiredAttr(prefix) + '="1"' : '';
        const attr = fieldAttr(prefix);
        const key = escape(field.key);
        const label = escape(fieldLabel(field, lang));
        if (field.secret) {
            // Never pre-filled: the server has no plaintext to send back, and an
            // edit must not render a stored secret. A scan is the one case where
            // the console does hold the plaintext, and the operator cannot
            // otherwise tell that it arrived — so say so, without putting the
            // value in the DOM.
            const held = val
                ? `<p class="mt-1 text-xs text-emerald-600 dark:text-emerald-400"
                   ${heldAttr(prefix)}="${key}">${escape(t(_heldHintKey(o)))}</p>`
                : '';
            return `<input type="password" autocomplete="new-password"${required}
                       ${attr}="${key}"
                       value="" placeholder="${label}"
                       class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-white/10
                              bg-white dark:bg-[#141414] text-sm text-slate-700 dark:text-slate-200">${held}`;
        }
        return `<input type="${field.type === 'number' ? 'number' : 'text'}"${field.type === 'number' ? ' min="1" max="65535" step="1"' : ''}${required}
                   ${attr}="${key}"
                   value="${escape(val)}" placeholder="${label}"
                   class="w-full px-3 py-2 rounded-lg border border-slate-200 dark:border-white/10
                          bg-white dark:bg-[#141414] text-sm text-slate-700 dark:text-slate-200">`;
    }

    // Every declared field as a labelled block, so a form renders exactly the
    // keys the create path accepts.
    function fieldsHtml(opts) {
        const o = opts || {};
        const escape = _escape(o);
        const t = _t(o);
        const lang = _lang(o);
        const values = o.values || {};
        return (Array.isArray(o.fields) ? o.fields : []).map(field => `
                        <div>
                            <label class="block text-xs text-slate-500 dark:text-slate-400 mb-1">
                                ${escape(fieldLabel(field, lang))}${field.required ? ' <span class="text-red-500">*</span>' : ''}</label>
                            ${fieldInput(Object.assign({}, o, { field: field,
                                value: values[field.key] }))}
                        </div>`).join('');
    }

    function webhookHint(opts) {
        const o = opts || {};
        const webhook = o.spec && o.spec.webhook;
        if (!webhook) return '';
        const escape = _escape(o);
        const t = _t(o);
        const path = o.iid ? webhook.path + '/' + encodeURIComponent(o.iid) : '';
        return `<div class="text-xs text-slate-500 dark:text-slate-400 space-y-1 break-all">
            <p>${escape(t('tenant_channel_webhook_hint'))}</p>
            ${path ? `<p>${escape(t('tenant_channel_webhook_path'))}: <code>${escape(path)}</code></p>`
                : `<p>${escape(t('tenant_channel_webhook_after_save'))}</p>`}
        </div>`;
    }

    const ACTIVE_TAB_CLASSES = 'bg-white dark:bg-slate-700 text-slate-800 dark:text-slate-100 shadow-sm';
    const INACTIVE_TAB_CLASSES = 'text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200';

    function _tabButton(opts, iid, mode, labelKey, active) {
        const escape = _escape(opts);
        const t = _t(opts);
        const prefix = opts.prefix || 'tenant-channel';
        // Switching is an inline call only when the caller supplied the global
        // function to call. The personal workbench instead binds the same
        // attribute by delegation, so no surface has to publish a global.
        const onclick = typeof opts.switchCall === 'function'
            ? ` onclick="${escape(opts.switchCall(iid, mode))}"` : '';
        return `
            <button type="button" data-${prefix}-mode="${mode}"${onclick}
                class="flex-1 px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${active ? ACTIVE_TAB_CLASSES : INACTIVE_TAB_CLASSES}">
                ${escape(t(labelKey))}
            </button>`;
    }

    // The 扫码 / 手工 strip. A scan-capable type gets both tabs; anything else
    // gets the manual tab alone, so the strip never promises a scan it cannot
    // start.
    function modeTabs(opts) {
        const o = opts || {};
        const prefix = o.prefix || 'tenant-channel';
        const manualKey = (o.copy && o.copy.manualTab) || 'feishu_mode_manual';
        if (!o.supportsScan || !o.copy) {
            return `
        <div class="flex items-center justify-center gap-1 mb-5 bg-slate-100 dark:bg-white/5 rounded-lg p-1">
            ${_tabButton(o, o.iid, 'manual', manualKey, true)}
        </div>`;
        }
        const mode = o.mode === 'scan' ? 'scan' : 'manual';
        return `
        <div class="flex items-center justify-center gap-1 mb-5 bg-slate-100 dark:bg-white/5 rounded-lg p-1">
            ${_tabButton(o, o.iid, 'scan', o.copy.tab, mode === 'scan')}
            ${_tabButton(o, o.iid, 'manual', manualKey, mode === 'manual')}
        </div>`;
    }

    // The scan pane. It never persists anything: a scan only fills the caller's
    // draft, which is why the start call is supplied rather than known here.
    function scanPane(opts) {
        const o = opts || {};
        if (!o.supportsScan || !o.copy) return '';
        const escape = _escape(o);
        const t = _t(o);
        const prefix = o.prefix || 'tenant-channel';
        const iid = o.iid || 'new';
        const statusId = o.statusId || (prefix + '-scan-status-' + iid);
        const startFn = o.startCall || '';
        return `
        <div id="${escape(prefix)}-pane-scan-${escape(iid)}" class="${o.mode === 'scan' ? '' : 'hidden'}">
            <div class="flex flex-col items-center py-4">
                <p class="text-sm text-slate-600 dark:text-slate-300 mb-3 text-center">${escape(t(o.copy.desc))}</p>
                <button type="button" onclick="${escape(startFn)}('${escape(statusId)}', '${escape(iid)}')"
                    class="mt-2 px-6 py-2.5 rounded-lg bg-emerald-500 hover:bg-emerald-600 text-white text-sm font-medium
                           cursor-pointer transition-colors duration-150">
                    <i class="fas fa-qrcode mr-2"></i>${escape(t(o.copy.btn))}
                </button>
                <div id="${escape(statusId)}" class="mt-4 w-full"></div>
            </div>
        </div>`;
    }

    // The manual pane, holding the declared credential fields.
    function manualPane(opts) {
        const o = opts || {};
        const escape = _escape(o);
        const t = _t(o);
        const prefix = o.prefix || 'tenant-channel';
        const iid = o.iid || 'new';
        return `
        <div id="${escape(prefix)}-pane-manual-${escape(iid)}" class="${o.mode === 'manual' ? '' : 'hidden'}">
            <div class="space-y-4">
                <div id="${escape(prefix)}-fields" class="space-y-4">
                    ${fieldsHtml(o)}
                </div>
                <p class="text-xs text-slate-400 dark:text-slate-500">${escape(t(_secretNoteKey(o)))}</p>
            </div>
        </div>`;
    }

    // Show / hide the two panes of an already-rendered form. Both stay in the
    // DOM so switching tabs cannot wipe credentials the operator already typed.
    function applyMode(root, prefix, iid, mode) {
        const p = prefix || 'tenant-channel';
        const scope = root || (typeof document !== 'undefined' ? document : null);
        if (!scope || !scope.getElementById) return;
        const scan = scope.getElementById(p + '-pane-scan-' + iid);
        const manual = scope.getElementById(p + '-pane-manual-' + iid);
        if (scan) scan.classList.toggle('hidden', mode !== 'scan');
        if (manual) manual.classList.toggle('hidden', mode !== 'manual');
    }

    // Bind the mode strip of one rendered form to a callback. Explicit container
    // plus callback: the module never reaches for a page-global function, so two
    // forms on one page cannot steal each other's clicks.
    function bindModeSwitch(container, prefix, onSwitch) {
        if (!container || !container.addEventListener) return;
        const attr = 'data-' + (prefix || 'tenant-channel') + '-mode';
        container.addEventListener('click', event => {
            const target = event.target && event.target.closest
                ? event.target.closest('[' + attr + ']') : null;
            if (!target || !container.contains(target)) return;
            if (typeof onSwitch === 'function') {
                onSwitch(target.getAttribute(attr));
            }
        });
    }

    // The credential step as *field descriptors* rather than markup.
    //
    // The tenant page renders a type's declared fields as inline inputs via
    // ``fieldsHtml``; the member workbench renders them as a modal step. Both
    // derive from the same declaration through one of these two functions, so
    // neither surface can require a key the create path does not accept.
    //
    // ``opts.keep`` marks an edit: a blank secret then means "keep the stored
    // value", which the server honours by merging over the existing bundle, so
    // nothing is required and ``opts.keepHelpKey`` explains it.
    function credentialDescriptors(fields, opts) {
        const o = opts || {};
        const lang = _lang(o);
        const keep = o.keep === true;
        const helpKey = o.keepHelpKey;
        return (Array.isArray(fields) ? fields : []).map(field => {
            const descriptor = {
                name: field.key,
                label: fieldLabel(field, lang),
                rawLabel: true,
                type: field.secret ? 'password' : 'text',
                required: !keep && field.required === true,
            };
            if (keep && helpKey) descriptor.help = helpKey;
            return descriptor;
        });
    }

    const api = {
        DEFAULT_ICON: DEFAULT_ICON,
        DEFAULT_COLOR: DEFAULT_COLOR,
        HELD_HINT_KEY: HELD_HINT_KEY,
        SECRET_NOTE_KEY: SECRET_NOTE_KEY,
        fieldAttr: fieldAttr,
        requiredAttr: requiredAttr,
        heldAttr: heldAttr,
        typeSpec: typeSpec,
        typeLabel: typeLabel,
        fieldLabel: fieldLabel,
        appearance: appearance,
        typeOptions: typeOptions,
        agentOptions: agentOptions,
        supportsScan: supportsScan,
        hasScanCopy: hasScanCopy,
        scanReady: scanReady,
        scanCopy: scanCopy,
        scanStartFor: scanStartFor,
        autoName: autoName,
        missingRequiredFields: missingRequiredFields,
        collectFields: collectFields,
        payload: payload,
        credentialDescriptors: credentialDescriptors,
        cardShell: cardShell,
        fieldInput: fieldInput,
        fieldsHtml: fieldsHtml,
        webhookHint: webhookHint,
        modeTabs: modeTabs,
        scanPane: scanPane,
        manualPane: manualPane,
        applyMode: applyMode,
        bindModeSwitch: bindModeSwitch,
    };

    if (typeof window !== 'undefined') window.ChannelWorkbench = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
