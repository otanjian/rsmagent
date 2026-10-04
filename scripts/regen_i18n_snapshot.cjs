// Regenerate tests/fixtures/console_i18n_snapshot.json from the merged table,
// refusing to write unless every key the fixture already had keeps its value.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const I18N_DIR = path.join(ROOT, 'channel/web/static/js/i18n');
const FIXTURE = path.join(ROOT, 'tests/fixtures/console_i18n_snapshot.json');

const ctx = { window: {} };
vm.createContext(ctx);
for (const file of fs.readdirSync(I18N_DIR).filter(f => f.endsWith('.js')).sort()) {
    vm.runInContext(fs.readFileSync(path.join(I18N_DIR, file), 'utf8'), ctx, { filename: file });
}
const merged = {};
for (const domain of Object.keys(ctx.window.__cowI18N__)) {
    for (const lang of Object.keys(ctx.window.__cowI18N__[domain])) {
        Object.assign(merged[lang] || (merged[lang] = {}), ctx.window.__cowI18N__[domain][lang]);
    }
}

const before = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
const problems = [];
for (const lang of Object.keys(before)) {
    if (!merged[lang]) { problems.push(`language ${lang} disappeared`); continue; }
    for (const [key, value] of Object.entries(before[lang])) {
        if (!(key in merged[lang])) problems.push(`${lang}.${key} was dropped`);
        else if (merged[lang][key] !== value) problems.push(`${lang}.${key} changed value`);
    }
}
if (problems.length) {
    console.error('REFUSING TO WRITE:\n  ' + problems.join('\n  '));
    process.exit(1);
}

let added = 0;
for (const lang of Object.keys(merged)) {
    for (const [key, value] of Object.entries(merged[lang])) {
        if (!before[lang] || !(key in before[lang])) {
            added += 1;
            console.log(`+ ${lang}.${key} = ${JSON.stringify(value)}`);
        }
    }
}

// Language order follows the fixture as it stands rather than being re-sorted:
// this file is a reviewable artifact, and re-ordering three thousand lines to
// add one key turns a one-line change into an unreviewable diff.
const langOrder = [...Object.keys(before),
                  ...Object.keys(merged).filter(l => !(l in before))];
const ordered = {};
for (const lang of langOrder) {
    ordered[lang] = {};
    for (const key of Object.keys(merged[lang]).sort()) ordered[lang][key] = merged[lang][key];
}
fs.writeFileSync(FIXTURE, JSON.stringify(ordered, null, 2) + '\n', 'utf8');
console.log(`wrote ${FIXTURE} (${added} added key(s), languages: ${langOrder.join(', ')})`);
