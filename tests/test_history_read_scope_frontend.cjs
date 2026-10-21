'use strict';
// Cross-owner history UI (change add-enterprise-interview-management-scene).
// The read scope is opt-in, the read-only detail renders untrusted text through
// textContent, and selecting an Agent never bundles the read-all grant.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const consoleSrc = fs.readFileSync(path.join(root, 'channel/web/static/js/views/sessions.js'), 'utf8');
const adminSrc = fs.readFileSync(path.join(root, 'channel/web/static/js/identity-admin.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'channel/web/chat.html'), 'utf8')
  + fs.readFileSync(path.join(root, 'channel/web/templates/fork/views/business.html'), 'utf8');
const i18n = fs.readFileSync(path.join(root, 'channel/web/static/js/i18n/core.js'), 'utf8');

test('history page exposes an opt-in cross-owner scope', () => {
  assert.match(html, /id="history-read-scope"/);
  assert.match(html, /onHistoryReadScopeChange/);
  assert.match(html, /id="history-read-target-select"/);
  assert.match(consoleSrc, /read_scope=agent_all/);
  assert.match(consoleSrc, /\/api\/sessions\/read_targets/);
});

test('a read-only row opens the viewer instead of the chat', () => {
  assert.match(consoleSrc, /if \(s\.read_only\) openReadOnlyHistory\(s\.session_id, ownerId\);/);
  assert.match(consoleSrc, /more\.hidden = !!s\.optimistic \|\| !!s\.read_only;/);
});

test('read-only messages are rendered as text, never as HTML', () => {
  assert.match(consoleSrc, /text\.textContent = typeof msg\.content === 'string' \? msg\.content : '';/);
  assert.match(consoleSrc, /history-readonly/);
});

test('i18n covers the new surface in all three languages', () => {
  const key = '"history_readonly_title"';
  assert.strictEqual(i18n.split(key).length - 1, 3);
  assert.strictEqual(i18n.split('"history_scope_agent_all"').length - 1, 3);
});

function collectGrantsSource() {
  const start = adminSrc.indexOf('function _collectResourceGrants()');
  const end = adminSrc.indexOf('function modelDefaultsHtml', start);
  assert.ok(start >= 0 && end > start, 'missing _collectResourceGrants');
  return adminSrc.slice(start, end);
}

function buildCollector(state) {
  const src = collectGrantsSource() + '\nreturn _collectResourceGrants;';
  const factory = new Function('_resourceKinds', '_resourceState', '_resourceActions', src);
  return factory(['menu', 'agent'], state, { agent: ['read', 'use', 'edit', 'enable', 'history_read_all'] });
}

test('selecting an Agent does not bundle history_read_all', () => {
  const state = {
    agent: {
      selected: new Set(['agent:advisor']),
      historyReadAll: new Set(),
      actions: ['read', 'use', 'edit', 'enable', 'history_read_all'],
    },
  };
  const grants = buildCollector(state)();
  assert.ok(grants.some(g => g.action === 'read'));
  assert.ok(!grants.some(g => g.action === 'history_read_all'));
});

test('the dedicated checkbox adds exactly the read-all grant', () => {
  const state = {
    agent: {
      selected: new Set(['agent:advisor']),
      historyReadAll: new Set(['agent:advisor']),
      actions: ['read', 'use', 'edit', 'enable', 'history_read_all'],
    },
  };
  const grants = buildCollector(state)();
  const readAll = grants.filter(g => g.action === 'history_read_all');
  assert.strictEqual(readAll.length, 1);
  assert.strictEqual(readAll[0].resource_id, 'agent:advisor');
});

test('the report entry posts once and downloads the returned markdown', () => {
  assert.match(consoleSrc, /\/api\/history\/report/);
  assert.match(consoleSrc, /function generateInterviewReport/);
  assert.match(consoleSrc, /method: 'POST'/);
  assert.match(consoleSrc, /pre\.textContent = data\.report/);
  assert.match(consoleSrc, /new Blob\(\[report\]/);
  assert.match(consoleSrc, /history_report_note/);
  assert.strictEqual(i18n.split('"history_report_generate"').length - 1, 3);
});
