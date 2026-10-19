// The packaged app must be able to find the local-file helpers.
//
// This guards the defect that made "read local files" work in a dev tree and not
// from an install: the app resolves them at `<resources>/fs-guard[.exe]`
// (fs-guard-binary.ts), `extraResources` copies `desktop/resources/` to that
// directory, and *nothing* put the cargo artifacts there -- the release workflow
// compiled one helper on macOS only, and copied neither.
//
// Two things have to hold together, and either one alone is useless: the packaging
// configuration must ship the files, and the build must actually produce them
// there. Asserted separately below, because a test that only checked the config
// would pass while every release shipped without a helper.
//
// Run: node --test tests/test_desktop_packaged_helpers.cjs

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const pkg = JSON.parse(fs.readFileSync(path.join(desktop, 'package.json'), 'utf8'));

const HELPER_NAMES = ['fs-guard', 'fs-guard.exe', 'project-fs-guard', 'project-fs-guard.exe'];

test('the packaging config ships the helpers out of resources/', () => {
    const entries = pkg.build.extraResources || [];

    // A per-file entry, named one at a time. That form exists because the
    // directory-wide ``filter`` allow-list silently drops anything it does not
    // match -- no warning, no error -- which is how both helpers were missing from
    // every package while still resolving fine in a dev tree.
    const named = new Set(entries.map((e) => e && e.to));
    for (const name of HELPER_NAMES) {
        assert.ok(named.has(name),
            `extraResources must name ${name} explicitly, not rely on a filter`);
    }

    // And the directory copy must not exclude them either, as a second line of
    // defence if the per-file entries were ever dropped.
    const entry = entries.find((e) => e && e.from === 'resources');
    assert.ok(entry, 'extraResources must still copy desktop/resources');
    const admitsEverything = (entry.filter || []).includes('**/*');
    for (const name of HELPER_NAMES) {
        assert.ok(admitsEverything || (entry.filter || []).includes(name),
            `the resources filter must not exclude ${name}`);
    }
});

test('the release workflow builds *and collects* both helpers', () => {
    const workflow = fs.readFileSync(
        path.join(root, '.github', 'workflows', 'release.yml'), 'utf8');
    // Building only the read helper leaves every write refused.
    assert.match(workflow, /--features write-ops/,
        'the write helper needs the write-ops feature');
    // Collected, not just built: the artifact under target/release is invisible to
    // the packaged app.
    assert.match(workflow, /cp [^\n]*desktop\/resources\//,
        'the workflow must copy the built helpers into desktop/resources');
    // The Windows job used to have no helper step at all.
    assert.match(workflow, /fs-guard\.exe/,
        'the workflow must name the Windows artifact');
});

test('the helpers are gitignored, not committed', () => {
    const ignore = fs.readFileSync(path.join(root, '.gitignore'), 'utf8');
    for (const name of ['fs-guard', 'fs-guard.exe', 'project-fs-guard', 'project-fs-guard.exe']) {
        assert.ok(ignore.includes(`desktop/resources/${name}`),
            `desktop/resources/${name} must be ignored: platform-specific build output`);
    }
});
