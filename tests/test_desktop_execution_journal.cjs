// The main process's durable execution journal, without a window.
//
// Change ``align-desktop-project-execution-with-master``, tasks 7.1 / 7.2 / 7.6.
// The journal is the only place on this machine that can answer "did I already
// run this?", so the failures that matter here are the quiet ones: a redelivery
// that runs the effect twice, a crash that is silently treated as "never ran",
// a pruned receipt that makes a spent command look fresh again. Each test below
// drives the real compiled ``JournalStore`` against a real file on disk, and the
// crash cases are simulated by constructing a *second* store over the same path
// -- which is what a restarted process actually does.
//
// Run: node --test tests/test_desktop_execution_journal.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const srcDir = path.join(desktop, 'src', 'main', 'project-execution');
const compiled = path.join(desktop, 'dist', 'main', 'project-execution', 'journal.js');

let journal;

function needsBuild() {
    if (!fs.existsSync(compiled)) return true;
    const newest = Math.max(
        ...fs.readdirSync(srcDir).filter((n) => n.endsWith('.ts'))
            .map((n) => fs.statSync(path.join(srcDir, n)).mtimeMs),
    );
    return newest > fs.statSync(compiled).mtimeMs;
}

before(() => {
    if (needsBuild()) {
        execFileSync('npm', ['run', 'build:main'], { cwd: desktop, stdio: 'inherit' });
    }
    journal = require(compiled);
});

/** A fresh user-data directory per test: the journal is per installation. */
function scratch() {
    return fs.mkdtempSync(path.join(os.tmpdir(), 'execution-journal-'));
}

function record(overrides = {}) {
    const base = {
        journal_id: 'jrn-1',
        command_id: 'cmd-1',
        params_digest: 'sha256:aaaa',
        origin: 'https://master.example',
        user_id: 'u-1',
        tenant_id: 't-1',
        device_id: 'dev-1',
        workspace_id: 'ws-1',
        run_id: 'run-1',
        tool_call_id: 'call-1',
        tool: 'write_file',
        grant_version: 3,
        started_at: 1_700_000_000_000,
    };
    const merged = { ...base, ...overrides };
    return merged;
}

function scopeOf(rec) {
    return {
        origin: rec.origin,
        user_id: rec.user_id,
        tenant_id: rec.tenant_id,
        device_id: rec.device_id,
        command_id: rec.command_id,
    };
}

function open(dir, options = {}) {
    return new journal.JournalStore({ file: path.join(dir, 'execution-journal.json'), ...options });
}

function readFileBody(dir) {
    return fs.readFileSync(path.join(dir, 'execution-journal.json'), 'utf8');
}

test('a start intent reaches disk before the effect can run', () => {
    const dir = scratch();
    const store = open(dir);
    const rec = record();
    const { entry, created } = store.begin(rec, scopeOf(rec));

    assert.equal(created, true);
    assert.equal(entry.state, 'started');
    assert.equal(entry.receipt, null);

    // A restarted process -- a second store over the same file -- must see the
    // intent even though nobody ever reported a result.
    const reopened = open(dir);
    const seen = reopened.lookup(rec.command_id, rec.params_digest, scopeOf(rec));
    assert.equal(seen.kind, 'started');
    assert.equal(seen.entry.journal_id, rec.journal_id);
    assert.equal(seen.entry.tool, 'write_file');
    assert.equal(seen.entry.grant_version, 3);
});

test('the journal file is private to the user', () => {
    const dir = scratch();
    open(dir).begin(record(), scopeOf(record()));
    const mode = fs.statSync(path.join(dir, 'execution-journal.json')).mode & 0o777;
    assert.equal(mode, 0o600, 'receipts carry run and tool-call ids; keep them owner-only');
});

test('a redelivery with the same payload returns the stored record instead of a second one', () => {
    const dir = scratch();
    const store = open(dir);
    const rec = record();
    store.begin(rec, scopeOf(rec));

    const again = store.begin(rec, scopeOf(rec));
    assert.equal(again.created, false, 'a genuine redelivery must not write a new record');
    assert.equal(again.entry.journal_id, rec.journal_id);
    assert.equal(store.list().length, 1);
});

test('a redelivery with a different payload is refused, never executed as the new payload', () => {
    const dir = scratch();
    const store = open(dir);
    const rec = record();
    store.begin(rec, scopeOf(rec));

    const tampered = record({ params_digest: 'sha256:bbbb' });
    assert.throws(
        () => store.begin(tampered, scopeOf(tampered)),
        (err) => err.name === 'JournalConflict' && err.code === 'command_conflict',
    );

    // The original authorization survives the refusal untouched.
    const seen = store.lookup(rec.command_id, rec.params_digest, scopeOf(rec));
    assert.equal(seen.kind, 'started');
    assert.equal(seen.entry.params_digest, 'sha256:aaaa');

    // And a reconnect that asks with the new digest is told it conflicts.
    assert.equal(store.lookup(rec.command_id, tampered.params_digest, scopeOf(rec)).kind, 'conflict');
});

test('the identity in the scope must be the identity in the record', () => {
    const dir = scratch();
    const store = open(dir);
    const rec = record();
    assert.throws(
        () => store.begin(rec, { ...scopeOf(rec), device_id: 'dev-other' }),
        /scope disagrees with the record on 'device_id'/,
    );
    assert.equal(store.list().length, 0);
});

test('every required field of the contract is enforced before anything is written', () => {
    const dir = scratch();
    const store = open(dir);
    const broken = record({ workspace_id: '' });
    assert.throws(() => store.begin(broken, scopeOf(broken)), /missing 'workspace_id'/);
    assert.equal(fs.existsSync(path.join(dir, 'execution-journal.json')), false,
        'a refused record must not leave a half-written file');
});

test('a completion is reported from the receipt, not re-run', () => {
    const dir = scratch();
    const store = open(dir);
    const rec = record();
    store.begin(rec, scopeOf(rec));

    const frame = {
        command_id: rec.command_id,
        phase: 'succeeded',
        effects: 'partial',
        exit_code: 0,
        stdout_digest: 'sha256:cccc',
        stderr_digest: 'sha256:dddd',
        duration_ms: 42,
    };
    store.complete(rec.command_id, rec.params_digest, { outcome: 'succeeded', effects: 'partial', frame }, scopeOf(rec));

    // The redelivery after a lost result frame is answered from disk.
    const reopened = open(dir);
    const seen = reopened.lookup(rec.command_id, rec.params_digest, scopeOf(rec));
    assert.equal(seen.kind, 'completed');
    assert.equal(seen.entry.receipt.outcome, 'succeeded');
    assert.deepEqual(seen.entry.receipt.frame, frame);
    assert.ok(seen.entry.completed_at > 0);
});

test('a second receipt for one command cannot overwrite the first', () => {
    const dir = scratch();
    const store = open(dir);
    const rec = record();
    store.begin(rec, scopeOf(rec));
    const first = store.complete(rec.command_id, rec.params_digest,
        { outcome: 'failed', effects: 'none' }, scopeOf(rec));
    const second = store.complete(rec.command_id, rec.params_digest,
        { outcome: 'succeeded', effects: 'partial' }, scopeOf(rec));

    assert.equal(second.completed_at, first.completed_at);
    assert.equal(second.receipt.outcome, 'failed', 'the recorded truth is not the last thing a caller said');
});

test('a receipt cannot be recorded for a command this machine never started', () => {
    const dir = scratch();
    const store = open(dir);
    assert.throws(
        () => store.complete('cmd-never', 'sha256:aaaa', { outcome: 'succeeded', effects: 'none' }, {
            origin: 'https://master.example', user_id: 'u-1', tenant_id: 't-1',
            device_id: 'dev-1', command_id: 'cmd-never',
        }),
        /no start intent/,
    );
});

test('a crash between start and receipt becomes outcome_unknown, and is not retried', () => {
    const dir = scratch();
    const rec = record();
    open(dir).begin(rec, scopeOf(rec));

    // Restart. The intent is on disk; the receipt never was.
    const restarted = open(dir);
    assert.equal(restarted.recover(), 1);

    const seen = restarted.lookup(rec.command_id, rec.params_digest, scopeOf(rec));
    assert.equal(seen.kind, 'unknown');
    assert.equal(seen.entry.receipt.outcome, 'outcome_unknown');
    assert.equal(seen.entry.receipt.effects, 'unknown',
        'a generic script gives no exactly-once guarantee; "unknown" is the honest answer');

    // Recovery is idempotent and never turns an unknown back into a runnable start.
    const second = open(dir);
    assert.equal(second.recover(), 0);
    assert.equal(second.lookup(rec.command_id, rec.params_digest, scopeOf(rec)).kind, 'unknown');
});

test('recovery leaves finished commands finished', () => {
    const dir = scratch();
    const store = open(dir);
    const done = record({ command_id: 'cmd-done', journal_id: 'jrn-done' });
    store.begin(done, scopeOf(done));
    store.complete(done.command_id, done.params_digest, { outcome: 'succeeded', effects: 'project' }, scopeOf(done));

    const pending = record({ command_id: 'cmd-pending', journal_id: 'jrn-pending' });
    store.begin(pending, scopeOf(pending));

    const restarted = open(dir);
    assert.equal(restarted.recover(), 1);
    assert.equal(restarted.lookup(done.command_id, done.params_digest, scopeOf(done)).kind, 'completed');
    assert.equal(restarted.lookup(pending.command_id, pending.params_digest, scopeOf(pending)).kind, 'unknown');
});

test('retention drops aged receipts but keeps the unknown outcomes it still owes answers for', () => {
    const dir = scratch();
    let clock = 1_000_000;
    const store = open(dir, { now: () => clock });

    const aged = record({ command_id: 'cmd-aged', journal_id: 'jrn-aged' });
    store.begin(aged, scopeOf(aged));
    clock = 1_000_001;
    store.complete(aged.command_id, aged.params_digest, { outcome: 'succeeded', effects: 'project' }, scopeOf(aged));

    // A crash left this one unresolved, and recovery stamped it as unknown at the
    // very same millisecond as the aged receipt.
    const unresolved = record({ command_id: 'cmd-unresolved', journal_id: 'jrn-unresolved' });
    store.begin(unresolved, scopeOf(unresolved));
    assert.equal(store.recover(), 1);

    clock = 1_000_002;
    const fresh = record({ command_id: 'cmd-fresh', journal_id: 'jrn-fresh' });
    store.begin(fresh, scopeOf(fresh));
    store.complete(fresh.command_id, fresh.params_digest, { outcome: 'succeeded', effects: 'project' }, scopeOf(fresh));

    // Both the aged receipt and the unknown record are older than the cut, so the
    // only thing that can save the unknown is its state -- which is the point.
    const removed = store.prune(1_000_002);
    assert.deepEqual(removed, ['jrn-aged']);
    assert.deepEqual(store.list().map((e) => e.command_id).sort(), ['cmd-fresh', 'cmd-unresolved'],
        'reclaiming an unknown would turn "assume it ran" into "not in the journal"');

    const still = open(dir);
    assert.equal(still.lookup(unresolved.command_id, unresolved.params_digest, scopeOf(unresolved)).kind, 'unknown');
    assert.equal(still.lookup(fresh.command_id, fresh.params_digest, scopeOf(fresh)).kind, 'completed');
    assert.equal(still.lookup(aged.command_id, aged.params_digest, scopeOf(aged)).kind, 'pruned');
});

test('a pruned receipt leaves a tombstone, so a spent command cannot look fresh again', () => {
    const dir = scratch();
    let clock = 5_000_000;
    const store = open(dir, { now: () => clock });
    const rec = record();
    store.begin(rec, scopeOf(rec));
    store.complete(rec.command_id, rec.params_digest, { outcome: 'succeeded', effects: 'project' }, scopeOf(rec));

    clock += 10;
    store.prune(clock);
    assert.equal(store.lookup(rec.command_id, rec.params_digest, scopeOf(rec)).kind, 'pruned',
        'not "none": the command spent its authorization, even though the receipt is gone');

    // Re-begun after pruning is a conflict, not a fresh run.
    const restarted = open(dir);
    assert.throws(
        () => restarted.begin(rec, scopeOf(rec)),
        (err) => err.name === 'JournalConflict' && err.code === 'command_conflict',
    );
});

test('tombstones are bounded too', () => {
    const dir = scratch();
    let clock = 9_000_000;
    const store = open(dir, { now: () => clock });
    for (const id of ['cmd-a', 'cmd-b']) {
        const rec = record({ command_id: id, journal_id: `jrn-${id}` });
        store.begin(rec, scopeOf(rec));
        clock += 1;
        store.complete(rec.command_id, rec.params_digest, { outcome: 'succeeded', effects: 'project' }, scopeOf(rec));
    }
    clock += 1;
    store.prune(clock);
    assert.equal(store.list().length, 0);

    clock += 1_000;
    assert.equal(store.pruneTombstones(clock), 2);
    // With the tombstones gone the key is genuinely absent -- nothing is kept forever.
    assert.equal(store.lookup('cmd-a', 'sha256:aaaa', {
        origin: 'https://master.example', user_id: 'u-1', tenant_id: 't-1',
        device_id: 'dev-1', command_id: 'cmd-a',
    }).kind, 'none');
});

test('an unreadable journal fails closed and keeps the bytes for an operator', () => {
    const dir = scratch();
    const file = path.join(dir, 'execution-journal.json');
    fs.writeFileSync(file, '{ this is not json');

    const store = open(dir);
    assert.equal(store.degraded, true);
    assert.match(store.degradationReason, /unreadable/);
    assert.equal(fs.existsSync(file), false);
    assert.equal(fs.readFileSync(`${file}.corrupt`, 'utf8'), '{ this is not json',
        'the unreadable bytes are quarantined, not deleted');

    // With no readable journal this machine cannot rule out a doubled effect.
    const rec = record();
    assert.throws(() => store.begin(rec, scopeOf(rec)), (err) => err.name === 'JournalDegraded');
    assert.equal(fs.existsSync(file), false);
});

test('no temporary file survives a write', () => {
    const dir = scratch();
    const store = open(dir);
    const rec = record();
    store.begin(rec, scopeOf(rec));
    store.complete(rec.command_id, rec.params_digest, { outcome: 'cancelled', effects: 'none' }, scopeOf(rec));
    assert.deepEqual(fs.readdirSync(dir), ['execution-journal.json']);
    assert.match(readFileBody(dir), /"version":1/);
});

test('the record cap is a backstop, and it only ever reclaims finished receipts', () => {
    const dir = scratch();
    let clock = 1_000_000;
    const store = open(dir, { now: () => clock });
    for (const id of ['cmd_1', 'cmd_2', 'cmd_3', 'cmd_4']) {
        const rec = record({ command_id: id, journal_id: `jrn-${id}` });
        store.begin(rec, scopeOf(rec));
        clock += 1;
        store.complete(rec.command_id, rec.params_digest,
            { outcome: 'succeeded', effects: 'project' }, scopeOf(rec));
    }
    const live = record({ command_id: 'cmd_live', journal_id: 'jrn-live' });
    store.begin(live, scopeOf(live));

    // Nothing is old enough to age out -- prune(0) keeps every finished record --
    // so the cap is the only thing acting here.
    const removed = store.prune(0, { keepAtMost: 2 });
    assert.deepEqual(removed, ['jrn-cmd_1', 'jrn-cmd_2', 'jrn-cmd_3'],
        'oldest first, and only as many as the cap needs');
    assert.deepEqual(store.list().map((e) => e.command_id).sort(), ['cmd_4', 'cmd_live'],
        'an unfinished command is never reclaimed to satisfy a quota');

    // A receipt the cap reclaimed must leave the same tombstone the age window
    // does, or a capped-out command would look brand new and run again.
    const evicted = record({ command_id: 'cmd_1', journal_id: 'jrn-cmd_1' });
    assert.equal(store.lookup('cmd_1', evicted.params_digest, scopeOf(evicted)).kind, 'pruned');
    assert.throws(
        () => store.begin(evicted, scopeOf(evicted)),
        (err) => err.name === 'JournalConflict' && err.code === 'command_conflict',
    );
});

test('a cap larger than the journal reclaims nothing', () => {
    const dir = scratch();
    const store = open(dir);
    const rec = record();
    store.begin(rec, scopeOf(rec));
    store.complete(rec.command_id, rec.params_digest, { outcome: 'succeeded', effects: 'project' }, scopeOf(rec));
    assert.deepEqual(store.prune(0, { keepAtMost: 100 }), []);
    assert.equal(store.list().length, 1);
});

test('the journal path and retention window come from one place', () => {
    assert.equal(journal.journalPath('/tmp/uda'), path.join('/tmp/uda', 'execution-journal.json'));
    assert.equal(journal.journalRetentionMs(7), 7 * 24 * 60 * 60 * 1000);
    assert.equal(journal.journalRetentionMs(0), 24 * 60 * 60 * 1000, 'never a zero-width window');
    assert.equal(journal.journalRetentionMs(2.7), 2 * 24 * 60 * 60 * 1000);
});

test('the dedup key is the contract\'s own field list', () => {
    const key = journal.dedupKey({
        origin: 'o', user_id: 'u', tenant_id: 't', device_id: 'd', command_id: 'c',
    });
    assert.equal(key, 'o\u0000u\u0000t\u0000d\u0000c');
    // A different account on the same device is a different command.
    assert.notEqual(key, journal.dedupKey({
        origin: 'o', user_id: 'u2', tenant_id: 't', device_id: 'd', command_id: 'c',
    }));
});
