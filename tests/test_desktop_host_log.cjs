// The desktop shell's own log file, driven through the real compiled module.
//
// Change ``harden-windows-desktop-reliability``, tasks 1.1-1.3. The 1.1 GB
// ``run.log`` incident (docs/design/windows-desktop-verification-2026-10-13.md
// section 4.1) happened because a JSON/log formatter could raise, because the
// console wrapper wrote to a broken pipe without protection, and because the
// ``uncaughtException`` handler reported itself *through* that same pipe. Each
// of those is a test here, and every one of them is a way for "logging" to
// become the outage:
//
//   * a throwing log input (circular object, BigInt, throwing toString) must
//     still land as a line, not as an exception;
//   * a console whose stream fails synchronously, and a stream that fails
//     asynchronously, must both mute that terminal *once* and keep the file
//     channel working;
//   * an uncaughtException must be recorded exactly once and never re-enter;
//   * the file must stay bounded across continuous writes (rotation), and a
//     burst must not silently drop the records that explain the burst.
//
// Run: node --test tests/test_desktop_host_log.cjs

const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const srcDir = path.join(desktop, 'src', 'main');
const compiled = path.join(desktop, 'dist', 'main', 'host-log.js');
const compiledDataDir = path.join(desktop, 'dist', 'main', 'data-dir.js');

let host;
let dataDir;

function needsBuild() {
    if (!fs.existsSync(compiled) || !fs.existsSync(compiledDataDir)) return true;
    const newest = Math.max(
        ...fs.readdirSync(srcDir).filter((n) => n.endsWith('.ts'))
            .map((n) => fs.statSync(path.join(srcDir, n)).mtimeMs),
    );
    return newest > fs.statSync(compiled).mtimeMs;
}

before(() => {
    if (needsBuild()) {
        execFileSync(process.execPath, [
            path.join(desktop, 'node_modules', 'typescript', 'bin', 'tsc'),
            '-p', 'tsconfig.main.json',
        ], { cwd: desktop, stdio: 'inherit' });
    }
    host = require(compiled);
    dataDir = require(compiledDataDir);
});

const scratchDirs = [];

function scratch() {
    // Every directory this file creates is removed at the end: a test that
    // leaves a hundred temp trees behind is a test that gets run less often.
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cow-host-log-'));
    scratchDirs.push(dir);
    return dir;
}

after(() => {
    for (const dir of scratchDirs) {
        fs.rmSync(dir, { recursive: true, force: true });
    }
});

function read(file) {
    try {
        return fs.readFileSync(file, 'utf8');
    } catch {
        return '';
    }
}

function records(text) {
    return text.split('\n').filter(Boolean);
}

function count(text, needle) {
    return text.split(needle).length - 1;
}

function runDriver(dir, mode) {
    const driver = path.join(dir, 'driver.cjs');
    fs.writeFileSync(driver, DRIVER, 'utf8');
    return execFileSync(process.execPath, [driver, compiled, path.join(dir, 'logs'), mode], {
        encoding: 'utf8',
        timeout: 60000,
    });
}

// ---------------------------------------------------------------------------
// The shape of the file
// ---------------------------------------------------------------------------

test('the shell writes its own file, bounded by default', () => {
    assert.equal(host.HOST_LOG_NAME, 'desktop-main.log',
        'the shell must not share the backend\'s run.log: two writers cannot rotate one file');
    assert.equal(host.HOST_LOG_MAX_BYTES, 10 * 1024 * 1024);
    assert.equal(host.HOST_LOG_BACKUPS, 2);
});

test('the shell log lands in the same directory as the backend log', () => {
    const saved = process.env.COW_DATA_DIR;
    delete process.env.COW_DATA_DIR;
    try {
        // A source run keeps both files in the checkout; a packaged build keeps
        // both in ~/.cow. The shell used to disagree with the backend here, so
        // "open log folder" revealed only half the story.
        assert.equal(dataDir.resolveDataDir('/tmp/checkout', false), '/tmp/checkout');
        assert.equal(dataDir.resolveDataDir('/tmp/checkout', true), dataDir.COW_DATA_DIR);
    } finally {
        if (saved === undefined) delete process.env.COW_DATA_DIR;
        else process.env.COW_DATA_DIR = saved;
    }
});

test('an explicit COW_DATA_DIR wins in every build', () => {
    const saved = process.env.COW_DATA_DIR;
    process.env.COW_DATA_DIR = path.join(os.tmpdir(), 'cow-explicit');
    try {
        assert.equal(dataDir.resolveDataDir('/tmp/checkout', false), process.env.COW_DATA_DIR);
        assert.equal(dataDir.resolveDataDir('/tmp/checkout', true), process.env.COW_DATA_DIR);
    } finally {
        if (saved === undefined) delete process.env.COW_DATA_DIR;
        else process.env.COW_DATA_DIR = saved;
    }
});

test('a record carries its level, timestamp and text', () => {
    const dir = scratch();
    const log = new host.HostLog({ dir });
    log.write('ERROR', ['backend failed', { code: 9009 }]);
    const lines = records(read(log.file));
    assert.equal(lines.length, 1);
    assert.match(lines[0], /^\[MAIN\]\[\d{4}-\d{2}-\d{2}T[\d:.]+Z\] \[ERROR\] backend failed /);
    assert.match(lines[0], /9009/);
    log.close();
});

test('a record is on disk when the call returns (nothing is queued)', () => {
    const dir = scratch();
    const log = new host.HostLog({ dir });
    for (let i = 0; i < 100; i += 1) log.write('LOG', [`durable ${i}`]);
    // No waiting, no flush, no drain: a queue here would be exactly the
    // unbounded memory the incident was made of.
    const lines = records(read(log.file));
    assert.equal(lines.length, 100);
    assert.match(lines[99], /durable 99/);
    log.close();
});

// ---------------------------------------------------------------------------
// Task 1.1: a failure to log must never become the failure
// ---------------------------------------------------------------------------

test('hostile log inputs reach the file as text', () => {
    const dir = scratch();
    const log = new host.HostLog({ dir });
    const circular = { name: 'loop' };
    circular.self = circular;
    const hostileToString = () => undefined;
    hostileToString.toString = () => { throw new Error('no string for you'); };

    assert.doesNotThrow(() => {
        log.write('ERROR', [circular]);
        log.write('ERROR', [10n]);
        log.write('ERROR', [hostileToString]);
        log.write('ERROR', [new Error('real failure')]);
    });
    const text = read(log.file);
    assert.equal(count(text, '[unserializable object]'), 1, 'a circular object is still logged');
    assert.match(text, /10/, 'a BigInt is a value, not a failure');
    assert.match(text, /\[unprintable value\]/, 'a value that cannot even be stringified');
    assert.match(text, /real failure/);
    log.close();
});

test('an unwritable directory degrades quietly instead of throwing', () => {
    const dir = scratch();
    // A *file* where the log directory should be: every attempt fails.
    const blocker = path.join(dir, 'not-a-directory');
    fs.writeFileSync(blocker, 'x');

    const realMkdir = fs.mkdirSync;
    let attempts = 0;
    fs.mkdirSync = (...args) => { attempts += 1; return realMkdir(...args); };
    try {
        const log = new host.HostLog({ dir: path.join(blocker, 'logs') });
        for (let i = 0; i < 50; i += 1) {
            assert.doesNotThrow(() => log.write('LOG', [`line ${i}`]));
        }
        assert.equal(attempts, host.HOST_LOG_FAILURE_LIMIT,
            'retries are bounded: the log gives up instead of paying a failing syscall per line');
        assert.equal(log.disabled, true);
        assert.equal(fs.existsSync(path.join(blocker, 'logs')), false);
        log.close();
    } finally {
        fs.mkdirSync = realMkdir;
    }
});

test('a disk that keeps refusing writes stops being retried', () => {
    const dir = scratch();
    const realWrite = fs.writeSync;
    let attempts = 0;
    fs.writeSync = () => {
        attempts += 1;
        throw Object.assign(new Error('ENOSPC: no space left on device'), { code: 'ENOSPC' });
    };
    try {
        const log = new host.HostLog({ dir });
        for (let i = 0; i < 40; i += 1) {
            assert.doesNotThrow(() => log.write('ERROR', [`disk full ${i}`]));
        }
        assert.equal(attempts, host.HOST_LOG_FAILURE_LIMIT);
        assert.equal(log.disabled, true);
        log.close();
    } finally {
        fs.writeSync = realWrite;
    }
});

// ---------------------------------------------------------------------------
// Task 1.2: bounded file, bounded backups, no rotation race
// ---------------------------------------------------------------------------

test('rotation keeps one live file plus two backups', () => {
    const dir = scratch();
    const log = new host.HostLog({ dir, maxBytes: 600, backups: 2 });
    for (let i = 1; i <= 300; i += 1) log.write('LOG', [`line ${i} ${'x'.repeat(40)}`]);

    assert.equal(fs.existsSync(`${log.file}.1`), true, 'first backup exists');
    assert.equal(fs.existsSync(`${log.file}.2`), true, 'second backup exists');
    assert.equal(fs.existsSync(`${log.file}.3`), false, 'backups are capped, not appended forever');

    const all = [log.file, `${log.file}.1`, `${log.file}.2`];
    for (const file of all) {
        assert.ok(fs.statSync(file).size <= 600 + 120, `${file} stayed near the cap`);
    }

    // Retention is bounded on purpose: the newest records are kept, the oldest
    // rotate away, and nothing is ever written twice.
    const text = all.map(read).join('\n');
    assert.match(read(log.file), /line 300 /, 'the live file holds the newest record');
    assert.equal(text.includes('line 1 '), false, 'the oldest record rotated away');
    for (let i = 100; i <= 300; i += 1) {
        const hits = count(text, `line ${i} `);
        assert.ok(hits <= 1, `record ${i} appears ${hits} times`);
    }
    assert.ok(records(text).length >= 15, `three files still hold a useful tail, got ${records(text).length}`);
    log.close();
});

test('a burst of thousands of records is bounded and loses none of it', () => {
    const dir = scratch();
    const log = new host.HostLog({ dir, maxBytes: 400, backups: 2 });
    // The incident's shape: thousands of lines back to back. A stream would hit
    // its high-water mark here and any "skip while behind" policy would drop the
    // crash record at the end; synchronous writes keep the whole burst.
    for (let i = 1; i <= 5000; i += 1) log.write('LOG', [`burst ${i}`]);

    const text = read(log.file);
    assert.equal(count(text, 'burst 5000'), 1, 'the last record of the burst survived');
    assert.ok(fs.statSync(log.file).size <= 400 + 120, 'and the live file is still capped');
    log.close();
});

test('a single file with no backups is truncated at the cap', () => {
    const dir = scratch();
    const log = new host.HostLog({ dir, maxBytes: 300, backups: 0 });
    for (let i = 1; i <= 40; i += 1) log.write('LOG', [`only ${i}`]);
    assert.equal(fs.existsSync(`${log.file}.1`), false);
    assert.ok(fs.statSync(log.file).size <= 300 + 120);
    assert.equal(read(log.file).includes('only 1 '), false);
    log.close();
});

test('a pre-existing log keeps its history and its size is measured', () => {
    const dir = scratch();
    const first = new host.HostLog({ dir, maxBytes: 400 });
    first.write('LOG', ['from the previous run']);
    first.close();

    const second = new host.HostLog({ dir, maxBytes: 400 });
    second.write('LOG', ['from this run']);
    const text = read(second.file);
    assert.match(text, /from the previous run/, 'a restart appends, it does not clobber');
    assert.match(text, /from this run/);
    second.close();
});

// ---------------------------------------------------------------------------
// Tasks 1.1-1.2 in a real process: the reported incident
// ---------------------------------------------------------------------------

const DRIVER = `// generated by tests/test_desktop_host_log.cjs
const { initHostLog } = require(process.argv[2]);
const dir = process.argv[3];
const mode = process.argv[4];
const log = initHostLog(dir);
const epipe = () => Object.assign(new Error('EPIPE: broken pipe, write'), { code: 'EPIPE' });

if (mode === 'sync-throw') {
  // A console call that raises synchronously (the stack the incident reported).
  console.log = () => { throw epipe(); };
  console.error = () => { throw epipe(); };
  log.captureConsole();
  for (let i = 0; i < 50; i += 1) { console.log('sync ' + i); }
  for (let i = 0; i < 50; i += 1) { console.error('sync-err ' + i); }
} else {
  log.captureConsole();
  log.captureCrashes();
  // The asynchronous form: EPIPE arrives as an 'error' event, and with no
  // listener that event *is* the uncaughtException that starts the storm.
  process.stdout.emit('error', epipe());
  process.stderr.emit('error', epipe());
  if (mode === 'storm') {
    for (let i = 0; i < 2000; i += 1) console.log('storm ' + i);
    for (let i = 0; i < 500; i += 1) console.error('storm-err ' + i);
    process.emit('uncaughtException', epipe());
    process.emit('uncaughtException', epipe());
  }
}

log.close();
`;

test('a broken stdout is muted once and does not become an exception', () => {
    const dir = scratch();
    runDriver(dir, 'async-error');
    const text = read(path.join(dir, 'logs', 'desktop-main.log'));

    assert.equal(count(text, 'stdout output disabled'), 1,
        'the terminal is disabled once, not once per write');
    assert.equal(count(text, 'stderr output disabled'), 1);
    assert.equal(text.includes('[crash]'), false,
        'an EPIPE delivered as an event is handled, not escalated');
});

test('a console that throws synchronously mutes its terminal, not the app', () => {
    const dir = scratch();
    runDriver(dir, 'sync-throw');
    const text = read(path.join(dir, 'logs', 'desktop-main.log'));

    assert.equal(count(text, 'stdout output disabled'), 1);
    assert.equal(count(text, 'stderr output disabled'), 1);
    assert.equal(text.includes('[crash]'), false);
    // 50 + 50 records still landed, plus the two "disabled" records -- one line
    // each, so the count is exact.
    assert.equal(records(text).length, 102);
});

test('the reported log storm is bounded and the crash handler never recurses', () => {
    const dir = scratch();
    runDriver(dir, 'storm');
    const file = path.join(dir, 'logs', 'desktop-main.log');
    const text = read(file);

    // Old behaviour: this file grew at 3.6 MB/s to 1.1 GB and never stopped.
    assert.ok(fs.statSync(file).size < 400 * 1024, `log must stay small, got ${fs.statSync(file).size}`);
    assert.equal(count(text, '[crash] uncaughtException'), 2,
        'each uncaughtException is recorded exactly once');
    assert.equal(count(text, 'stdout output disabled'), 1, 'muting does not repeat per log line');
    assert.equal(count(text, 'storm 1999'), 1, 'the burst is not truncated away');
    assert.equal(count(text, 'storm-err 499'), 1);
});

test('rotation never touches the backend log or other files in the data directory', () => {
    const dir = scratch();
    // The backend's own log and the app's data live in the same directory, and
    // the shell must only ever rotate its own file.
    fs.writeFileSync(path.join(dir, 'run.log'), 'backend history\n');
    fs.writeFileSync(path.join(dir, 'identity.db'), 'sensitive\n');

    const log = new host.HostLog({ dir, maxBytes: 200, backups: 2 });
    for (let i = 1; i <= 200; i += 1) log.write('LOG', [`rotate ${i}`]);

    assert.equal(read(path.join(dir, 'run.log')), 'backend history\n',
        'the backend log is left alone');
    assert.equal(read(path.join(dir, 'identity.db')), 'sensitive\n',
        'no business file is truncated or deleted');
    assert.equal(fs.existsSync(path.join(dir, 'run.log.1')), false,
        'the shell does not rotate a file it does not own');
    const mine = fs.readdirSync(dir).filter((n) => n.startsWith('desktop-main.log'));
    assert.ok(mine.length > 1, 'its own generations are what got rotated');
    assert.deepEqual(
        fs.readdirSync(dir).sort(),
        ['desktop-main.log', 'desktop-main.log.1', 'desktop-main.log.2', 'identity.db', 'run.log'].sort(),
        'the data directory keeps exactly the files that were there plus the shell log',
    );
    log.close();
});
