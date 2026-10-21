// The conversation surfaces must not offer a self-service way to raise the
// session permission mode: execution is governed by the caller's role grants.
// The refused-tool hint stays (it explains why) but carries no action button.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { loadDictionaries } = require('./support/i18n_namespaces.cjs');
const { readWebLayer } = require('./_web_layer.cjs');
const root = path.join(__dirname, '..');
const read = (p) => fs.readFileSync(path.join(root, p), 'utf8');

test('web console has no session permission selector', () => {
    const markup = require('./support/console_sources.cjs').markup();
    const consoleJs = require('./support/console_sources.cjs').modules().filter(item => !item.name.includes('i18n')).map(item => item.source).join('\n');
    assert.doesNotMatch(markup, /permission-selector-btn/);
    assert.doesNotMatch(markup, /permission-selector-menu/);
    assert.doesNotMatch(consoleJs, /togglePermissionSelector/);
    assert.doesNotMatch(consoleJs, /renderPermissionMenu/);
});

test('web refused-tool hint keeps its explanation but drops the action button', () => {
    const consoleJs = require('./support/console_sources.cjs').modules().filter(item => !item.name.includes('i18n')).map(item => item.source).join('\n');
    assert.match(consoleJs, /perm-denied-hint/);
    assert.match(consoleJs, /perm_denied_hint/);
    assert.match(consoleJs, /perm_denied_role_hint/);
    assert.match(consoleJs, /perm_denied_isolation_hint/);
    assert.match(consoleJs, /kind === 'isolation'/);
    assert.doesNotMatch(consoleJs, /perm-denied-btn/);
    assert.doesNotMatch(consoleJs, /perm_denied_action/);
});

test('every language explains an isolation refusal', () => {
    const dicts = loadDictionaries();
    for (const lang of ['zh', 'zh-Hant', 'en']) {
        assert.ok(Object.prototype.hasOwnProperty.call(dicts[lang] || {}, 'perm_denied_isolation_hint'),
            `perm_denied_isolation_hint is missing from the ${lang} dictionary`);
    }
});

test('desktop has no permission selector or adjust button', () => {
    const chatInput = read('desktop/src/renderer/src/components/ChatInput.tsx');
    const messageSteps = read('desktop/src/renderer/src/components/MessageSteps.tsx');
    const store = read('desktop/src/renderer/src/store/sessionSettingsStore.ts');
    assert.doesNotMatch(chatInput, /PermissionSelector/);
    assert.doesNotMatch(messageSteps, /perm_denied_action/);
    assert.match(messageSteps, /perm_denied_role_hint/);
    assert.match(messageSteps, /perm_denied_isolation_hint/);
    assert.match(messageSteps, /kind === 'isolation'/);
    assert.doesNotMatch(store, /'permission'/);
    assert.equal(
        fs.existsSync(path.join(root, 'desktop/src/renderer/src/components/PermissionSelector.tsx')),
        false,
    );
    const i18n = read('desktop/src/renderer/src/i18n.ts');
    assert.match(i18n, /perm_denied_isolation_hint/);
});

test('the global default permission reads as owned by roles in database mode', () => {
    const handlers = readWebLayer();
    assert.match(handlers, /permission_mode_editable/);
    assert.match(handlers, /permission_mode_source/);

    // Web console: the dropdown is rendered read-only from that flag.
    const consoleJs = require('./support/console_sources.cjs').modules().filter(item => !item.name.includes('i18n')).map(item => item.source).join('\n');
    assert.match(consoleJs, /permission_mode_editable/);
    assert.match(consoleJs, /cfg-dropdown-readonly/);
    // The dictionary moved to a per-domain namespace file (task 8.5); the
    // localized read-only explanation must still ship in every language.
    const dicts = loadDictionaries();
    for (const lang of ['zh', 'zh-Hant', 'en']) {
        assert.ok(Object.prototype.hasOwnProperty.call(dicts[lang] || {}, 'config_permission_role_desc'),
            `config_permission_role_desc is missing from the ${lang} dictionary`);
    }
    assert.match(require('./support/console_sources.cjs').markup(), /cfg-permission-role-desc/);

    // Desktop settings page: same read-only treatment.
    const basic = read('desktop/src/renderer/src/pages/settings/BasicSettings.tsx');
    assert.match(basic, /permission_mode_editable/);
    assert.match(basic, /config_permission_role_desc/);
});
