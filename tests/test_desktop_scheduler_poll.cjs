// Desktop scheduler notification poll
// (change integrate-upstream-core-capabilities, tasks 7.5 + 7.6 Desktop half;
// spec database-runtime-consumers "关闭和切换使客户端停止无效请求",
// implementation.md §8 client rules, design D2 gating).
//
// The hook is React, so the test does not mount a component tree. Instead it
// exercises the exact decision functions the hook delegates to (they are named
// exports of the same file) and then drives the hook itself through a minimal
// React shim + fake clock, so the wiring -- gating, paging, pause, teardown --
// is verified behaviourally too. The real `.ts` source is transpiled with the
// TypeScript that ships in desktop/node_modules; if it is missing the test
// fails loudly rather than silently passing a weaker check.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const read = (p) => fs.readFileSync(path.join(root, p), 'utf8');
const HOOK = 'desktop/src/renderer/src/hooks/useSchedulerNotifyPoll.ts';

function loadTypeScript() {
    const modulePath = path.join(root, 'desktop', 'node_modules', 'typescript');
    assert.ok(fs.existsSync(modulePath), 'desktop/node_modules/typescript is required for this test');
    return require(modulePath);
}

const ts = loadTypeScript();

/** Transpile one .ts file and evaluate it with a relative-import resolver. */
function loadTsFrom(absPath, deps = {}) {
    const source = fs.readFileSync(absPath, 'utf8');
    const { outputText } = ts.transpileModule(source, {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
        fileName: absPath,
    });
    const module = { exports: {} };
    const baseDir = path.dirname(absPath);
    const resolver = (id) => {
        if (deps[id]) return deps[id];
        if (id.startsWith('.')) {
            const candidateBase = path.resolve(baseDir, id);
            for (const ext of ['.ts', '.tsx', '.js']) {
                if (fs.existsSync(candidateBase + ext)) return loadTsFrom(candidateBase + ext, deps);
            }
            if (fs.existsSync(candidateBase)) return loadTsFrom(candidateBase, deps);
        }
        return require(id);
    };
    const context = vm.createContext({
        module,
        exports: module.exports,
        require: resolver,
        console,
        process,
        setTimeout,
        clearTimeout,
        Buffer,
        URL,
        __filename: absPath,
        __dirname: baseDir,
        ...(deps.__sandbox || {}),
    });
    vm.runInContext(outputText, context, { filename: absPath });
    return module.exports;
}

/** The real capability predicate, shared with the hook so `instanceof` matches. */
const features = loadTsFrom(path.join(root, 'desktop/src/renderer/src/api/features.ts'));

/** The context error the hook's `instanceof` checks recognize. */
class FakeContextError extends Error {
    constructor(kind, message = kind, code = '', status = 0) {
        super(message);
        this.name = 'ContextError';
        this.kind = kind;
        this.code = code;
        this.status = status;
    }
}

const toPlain = (value) => JSON.parse(JSON.stringify(value));

/** A finished, client-delivered run; override any field per case. */
function run(overrides = {}) {
    const runId = overrides.run_id || 'r1';
    return {
        run_id: runId,
        session_id: `sess-${runId}`,
        task_id: 'task-1',
        status: 'done',
        started_at: 100,
        channel_type: 'web',
        task_name: `task-${runId}`,
        output_preview: `preview-${runId}`,
        ...overrides,
    };
}

const openActions = () => ({ 'scheduler.runs.list': { available: true, reason: '' } });

const flush = async (rounds = 8) => {
    for (let i = 0; i < rounds; i++) await new Promise((resolve) => setImmediate(resolve));
};

/**
 * A harness around one freshly-loaded hook module: fake context, fake API with
 * a scripted page queue, fake React, and a fake clock. `pages` entries are
 * either an array of runs, a promise, or a function (which may throw or return
 * a promise) so a tick can be held in flight and released on demand.
 */
function makeHarness(options = {}) {
    const timers = [];
    let seq = 0;
    const state = {
        gate: options.gate || 'ready',
        session: options.session === undefined ? { epoch: 3, tenantId: 't1' } : options.session,
        blockedReason: '',
        lastError: null,
        identityMode: '',
        probeFailed: false,
        featureActions:
            options.featureActions !== undefined
                ? options.featureActions
                : options.open === false
                  ? null
                  : openActions(),
        featureRevision: options.featureRevision || 0,
    };
    const refreshCalls = { count: 0 };
    const desktopContext = {
        subscribe: () => () => {},
        getSnapshot: () => state,
        requireFeature: async () => {},
        refreshCapabilities: async () => {
            refreshCalls.count += 1;
            return state.featureActions;
        },
    };

    const calls = [];
    const pages = options.pages ? options.pages.slice() : [];
    const api = {
        getSchedulerRunsSince: async (since, limit, offset) => {
            calls.push({ since, limit, offset });
            if (!pages.length) return [];
            const entry = pages.shift();
            const value = typeof entry === 'function' ? entry(since, limit, offset) : entry;
            return value;
        },
    };

    const notified = [];
    const notify = (sid, name, preview) => notified.push({ sid, name, preview });

    const react = {
        cleanups: [],
        useSyncExternalStore: (_subscribe, getSnapshot) => getSnapshot(),
        useEffect: (fn) => {
            const cleanup = fn();
            if (typeof cleanup === 'function') react.cleanups.push(cleanup);
        },
        lastCleanup: () => {
            const cleanup = react.cleanups.pop();
            if (cleanup) cleanup();
        },
    };

    const hook = loadTsFrom(path.join(root, HOOK), {
        react,
        '../api/client': { __esModule: true, default: api },
        '../lib/taskNotify': { __esModule: true, notifyScheduledRun: notify },
        '../api/context': { __esModule: true, default: desktopContext, ContextError: FakeContextError },
        '../api/features': features,
        __sandbox: {
            setTimeout: (fn, ms) => {
                const id = ++seq;
                timers.push({ id, fn, ms });
                return id;
            },
            clearTimeout: (id) => {
                const i = timers.findIndex((t) => t.id === id);
                if (i >= 0) timers.splice(i, 1);
            },
        },
    });

    return {
        hook,
        state,
        calls,
        timers,
        notified,
        react,
        refreshCalls,
        FakeContextError,
        runNextTimer: async () => {
            const timer = timers.shift();
            assert.ok(timer, 'the loop should have queued its next tick');
            await timer.fn();
            await flush();
        },
    };
}

// --------------------------------------------------------------------------
// Pure decisions
// --------------------------------------------------------------------------

test('a full page continues the drain and a short page ends it', () => {
    const { hook } = makeHarness();
    const limit = hook.PAGE_LIMIT;
    assert.deepEqual(toPlain(hook.nextDrainStep(limit, 0)), { nextOffset: limit, done: false });
    assert.deepEqual(toPlain(hook.nextDrainStep(limit, limit)), { nextOffset: limit * 2, done: false });
    assert.deepEqual(toPlain(hook.nextDrainStep(limit - 1, limit)), { nextOffset: limit, done: true });
    assert.deepEqual(toPlain(hook.nextDrainStep(0, 0)), { nextOffset: 0, done: true });
});

test('collectRunPages drains a full page, ends on a short one and bounds a pathological server', async () => {
    const { hook } = makeHarness();
    const full = () =>
        Array.from({ length: hook.PAGE_LIMIT }, (_, i) => run({ run_id: `p${i}`, started_at: 100 }));

    const offsets = [];
    const pages = [full(), [run({ run_id: 'tail', started_at: 100 })]];
    const collected = await hook.collectRunPages(async (offset) => {
        offsets.push(offset);
        return pages.shift();
    });
    assert.deepEqual(offsets, [0, hook.PAGE_LIMIT], 'offset advances only within one since window');
    assert.equal(collected.length, hook.PAGE_LIMIT + 1, 'a full page is not the end of the window');

    // A server that always returns a full page is bounded, not spun forever.
    const spins = [];
    const bounded = await hook.collectRunPages(async (offset) => {
        spins.push(offset);
        return full();
    });
    assert.equal(spins.length, hook.MAX_PAGES, 'MAX_PAGES bounds the drain');
    assert.equal(spins[hook.MAX_PAGES - 1], (hook.MAX_PAGES - 1) * hook.PAGE_LIMIT);
    assert.equal(bounded.length, hook.MAX_PAGES * hook.PAGE_LIMIT);
});

test('a cancelled drain discards the in-flight window instead of returning it', async () => {
    const { hook } = makeHarness();
    let cancelled = false;
    const full = () =>
        Array.from({ length: hook.PAGE_LIMIT }, (_, i) => run({ run_id: `p${i}`, started_at: 100 }));
    const result = await hook.collectRunPages(async (offset) => {
        // A tenant switch lands while the second page is in flight.
        if (offset > 0) cancelled = true;
        return full();
    }, () => cancelled);
    assert.equal(result, null, 'a stale identity’s page must not be applied');
});

test('two runs in the same second are ordered by run_id and notify once each over two windows', () => {
    const { hook } = makeHarness();
    const seen = new Set();
    const sameSecond = [run({ run_id: 'b', started_at: 100 }), run({ run_id: 'a', started_at: 100 })];

    const first = hook.planNotifyBatch(sameSecond, 0, seen);
    assert.deepEqual(toPlain(first.runs.map((r) => r.run_id)), ['a', 'b'], 'oldest-first with a run_id tiebreak');
    assert.equal(first.since, 100);

    // The next tick reads the same window again (`started_at >= since`).
    const second = hook.planNotifyBatch(sameSecond, first.since, seen);
    assert.deepEqual(toPlain(second.runs), [], 'a re-read window must not re-fire');
    assert.equal(second.since, 100, 'the cursor never moves backwards');
});

test('notifications are oldest-first and the since cursor is monotonic', () => {
    const { hook } = makeHarness();
    const seen = new Set();
    const batch = hook.planNotifyBatch(
        [run({ run_id: 'c', started_at: 300 }), run({ run_id: 'a', started_at: 100 }), run({ run_id: 'b', started_at: 200 })],
        0,
        seen,
    );
    assert.deepEqual(toPlain(batch.runs.map((r) => r.run_id)), ['a', 'b', 'c']);
    assert.equal(batch.since, 300);
    // A cursor ahead of every run is left alone (never rewound).
    assert.equal(hook.advanceSince(500, batch.runs), 500);
    assert.equal(hook.planNotifyBatch([], 500, new Set()).since, 500);
});

test('only client-delivered finished runs notify -- a timer tick and a manual run both do', () => {
    const { hook } = makeHarness();
    assert.equal(hook.isNotifiableRun({ channel_type: 'web', status: 'done' }), true);
    assert.equal(hook.isNotifiableRun({ channel_type: '', status: 'done' }), true);
    assert.equal(hook.isNotifiableRun({ channel_type: 'feishu', status: 'done' }), false);
    assert.equal(hook.isNotifiableRun({ channel_type: 'wechat', status: 'done' }), false);
    assert.equal(hook.isNotifiableRun({ channel_type: 'web', status: 'running' }), false);
    assert.equal(hook.isNotifiableRun({ channel_type: 'web', status: 'error' }), false);

    const seen = new Set();
    const batch = hook.planNotifyBatch(
        [
            run({ run_id: 'timer', started_at: 10, trigger: 'scheduled' }),
            run({ run_id: 'manual', started_at: 11, trigger: 'manual' }),
            run({ run_id: 'feishu', started_at: 12, channel_type: 'feishu' }),
            run({ run_id: 'open', started_at: 13, status: 'running' }),
        ],
        0,
        seen,
    );
    assert.deepEqual(toPlain(batch.runs.map((r) => r.run_id)), ['timer', 'manual']);
    assert.equal(seen.has('feishu'), false, 'a skipped run is not remembered as notified');
    assert.equal(seen.has('open'), false);
});

test('the seen set is bounded and evicts the oldest ids', () => {
    const { hook } = makeHarness();
    const seen = new Set();
    for (let i = 0; i < hook.SEEN_RUN_LIMIT + 3; i++) hook.rememberNotifiedRun(seen, `r${i}`);
    assert.equal(seen.size, hook.SEEN_RUN_LIMIT, 'memory stays bounded');
    assert.equal(seen.has('r0'), false, 'the oldest id is evicted first');
    assert.equal(seen.has(`r${hook.SEEN_RUN_LIMIT + 2}`), true);
    hook.rememberNotifiedRun(seen, '');
    assert.equal(seen.size, hook.SEEN_RUN_LIMIT, 'an empty id is never remembered');
});

test('a refusal pauses; only a network error retries', () => {
    const h = makeHarness();
    assert.equal(h.hook.pollErrorDecision(new h.FakeContextError('network')), 'retry');
    assert.equal(h.hook.pollErrorDecision(new h.FakeContextError('forbidden')), 'pause');
    assert.equal(h.hook.pollErrorDecision(new h.FakeContextError('unavailable')), 'pause');
    assert.equal(h.hook.pollErrorDecision(new h.FakeContextError('stale_context')), 'pause');
    assert.equal(
        h.hook.pollErrorDecision(new features.FeatureUnavailableError('scheduler.runs.list', 'not_accepted')),
        'pause',
    );
    assert.equal(h.hook.pollErrorDecision(new Error('boom')), 'pause');
});

// --------------------------------------------------------------------------
// The hook itself: gating, paging, pause/retry, teardown
// --------------------------------------------------------------------------

test('a closed or unresolved scheduler.runs.list issues no request and reports not-available', async () => {
    for (const featureActions of [null, {}, { 'scheduler.runs.list': { available: false, reason: 'not_accepted' } }]) {
        const h = makeHarness({ featureActions });
        h.hook.useSchedulerNotifyPoll(true);
        await flush();
        assert.equal(h.calls.length, 0, 'a closed action must not issue any request');
        assert.deepEqual(toPlain(h.hook.getSchedulerNotifyState()), { available: false, paused: false, reason: '' });
    }
});

test('the poll does not start until the shell is ready', async () => {
    const h = makeHarness();
    h.hook.useSchedulerNotifyPoll(false);
    await flush();
    assert.equal(h.calls.length, 0);
});

test('only one loop runs process-wide even if the hook is mounted twice', async () => {
    const page = () => [run({ run_id: 'a', started_at: 100 })];
    const h = makeHarness({ pages: [page(), page()] });
    h.hook.useSchedulerNotifyPoll(true);
    h.hook.useSchedulerNotifyPoll(true); // a second mount must not start a second loop
    await flush();
    assert.equal(h.calls.length, 1, 'the double mount issued one request, not two');
    assert.equal(h.notified.length, 1, 'a run must not be announced twice');
    assert.equal(h.timers.length, 1, 'one timer, not two');
});

test('the first tick starts at "now": pre-startup runs are not replayed', async () => {
    const before = Math.floor(Date.now() / 1000);
    const h = makeHarness({ pages: [[]] });
    h.hook.useSchedulerNotifyPoll(true);
    await flush();
    assert.equal(h.calls.length, 1);
    assert.ok(h.calls[0].since >= before, 'the since cursor starts at the client startup time');
});

test('an open action polls once and does not re-fire a same-second window on the next tick', async () => {
    const page = () => [run({ run_id: 'b', started_at: 100 }), run({ run_id: 'a', started_at: 100 })];
    const h = makeHarness({ pages: [page(), page()] });
    h.hook.useSchedulerNotifyPoll(true);
    await flush();

    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].offset, 0);
    assert.deepEqual(toPlain(h.notified.map((n) => n.sid)), ['sess-a', 'sess-b']);

    await h.runNextTimer();
    assert.equal(h.calls.length, 2);
    assert.ok(h.calls[1].since >= h.calls[0].since, 'the cursor never moves backwards');
    assert.equal(h.calls[1].offset, 0);
    assert.equal(h.notified.length, 2, 'the same run must not notify twice');
});

test('a full page is drained within one tick before the cursor advances', async () => {
    const full = Array.from({ length: 20 }, (_, i) => run({ run_id: `p0-${i}`, started_at: 100 }));
    const h = makeHarness({ pages: [full, [run({ run_id: 'p1-0', started_at: 100 })]] });
    h.hook.useSchedulerNotifyPoll(true);
    await flush();
    assert.deepEqual(toPlain(h.calls.map((c) => c.offset)), [0, 20]);
    assert.equal(h.notified.length, 21, 'every same-second run is collected before since advances');
});

test('a 403 pauses with no further request and no normal-interval reschedule', async () => {
    const h = makeHarness({
        pages: [
            () => {
                throw new h.FakeContextError('forbidden');
            },
        ],
    });
    h.hook.useSchedulerNotifyPoll(true);
    await flush();

    assert.equal(h.calls.length, 1, 'the refusal is not retried');
    assert.equal(h.calls[0].offset, 0, 'the request range is not widened');
    assert.equal(h.timers.length, 0, 'no normal-interval timer is armed');
    assert.equal(h.refreshCalls.count, 0, 'a refusal issues no further request');
    const state = h.hook.getSchedulerNotifyState();
    assert.equal(state.available, true);
    assert.equal(state.paused, true, 'the permission state is shown, never "no records"');
    assert.equal(state.reason, 'forbidden');
});

test('a service fault pauses instead of retrying', async () => {
    const h = makeHarness({
        pages: [
            () => {
                throw new h.FakeContextError('unavailable');
            },
        ],
    });
    h.hook.useSchedulerNotifyPoll(true);
    await flush();
    assert.equal(h.timers.length, 0);
    assert.equal(h.hook.getSchedulerNotifyState().paused, true);
    assert.equal(h.hook.getSchedulerNotifyState().reason, 'unavailable');
});

test('a transient network error reschedules on the normal interval', async () => {
    const h = makeHarness({
        pages: [
            () => {
                throw new h.FakeContextError('network');
            },
        ],
    });
    h.hook.useSchedulerNotifyPoll(true);
    await flush();
    assert.equal(h.timers.length, 1, 'a network blip is retried');
    assert.equal(h.hook.getSchedulerNotifyState().paused, false);
});

test('an identity change during an in-flight tick discards it and restarts clean', async () => {
    let releaseOld;
    const oldPage = new Promise((resolve) => {
        releaseOld = resolve;
    });
    const h = makeHarness({ pages: [() => oldPage, [run({ run_id: 'new-run', started_at: 200 })]] });
    h.hook.useSchedulerNotifyPoll(true);
    await flush();
    assert.equal(h.calls.length, 1, 'the first identity’s request is in flight');
    assert.deepEqual(h.notified, []);

    // The member switches tenant (new epoch + revision) while the request is in
    // flight: React tears the effect down before re-running it.
    h.state.session = { epoch: 4, tenantId: 't2' };
    h.state.featureRevision += 1;
    h.react.lastCleanup();
    assert.equal(h.timers.length, 0, 'teardown clears the timer');
    h.hook.useSchedulerNotifyPoll(true);
    await flush();
    assert.deepEqual(toPlain(h.notified.map((n) => n.sid)), ['sess-new-run']);

    // The previous identity's answer now lands. It must be discarded, not
    // notified, even though this run was never seen before.
    releaseOld([run({ run_id: 'old-run', started_at: 50 })]);
    await flush();
    assert.deepEqual(toPlain(h.notified.map((n) => n.sid)), ['sess-new-run']);
});

test('teardown reports the poll as no longer available', async () => {
    const h = makeHarness({ pages: [[run({ run_id: 'r', started_at: 100 })]] });
    h.hook.useSchedulerNotifyPoll(true);
    await flush();
    assert.equal(h.hook.getSchedulerNotifyState().available, true);
    h.react.lastCleanup();
    assert.deepEqual(toPlain(h.hook.getSchedulerNotifyState()), { available: false, paused: false, reason: '' });
});

// --------------------------------------------------------------------------
// Wiring (static): the hook delegates to the decisions and reaches the one
// runs reader through the capability gate only.
// --------------------------------------------------------------------------

test('the hook wires the pure decisions and tears down on identity change', () => {
    const source = read(HOOK);
    assert.match(source, /collectRunPages\(/);
    assert.match(source, /planNotifyBatch\(runs, since, notifiedRunIds\)/);
    assert.match(source, /pollErrorDecision\(err\) === 'retry'/);
    assert.match(source, /\}, \[allowed, epoch, tenantId, revision\]\)/);
    assert.match(source, /let since = Math\.floor\(Date\.now\(\) \/ 1000\)/, 'history is not replayed');
    assert.match(source, /cancelled = true[\s\S]{0,80}loopActive = false/);
    assert.match(source, /notifiedRunIds\.clear\(\)/);

    // The pause branch stops the loop: it neither arms a timer nor issues a
    // request (the runs reader is reached only through `collectRunPages`).
    const pauseBody = source.slice(source.indexOf('const pause ='), source.indexOf('const collect ='));
    assert.doesNotMatch(pauseBody, /schedule\(\)/);
    assert.doesNotMatch(pauseBody, /getSchedulerRunsSince/);
    assert.doesNotMatch(pauseBody, /refreshCapabilities/);
});

test('usePushPoll stays silent for scheduler pushes so this poll is the only source', () => {
    const push = read('desktop/src/renderer/src/hooks/usePushPoll.ts');
    assert.match(push, /startsWith\('scheduler_'\)/);
    assert.match(push, /added && !isScheduler/);
});

test('the runs reader keeps one since window, one gate and never an empty-list fallback', () => {
    const client = read('desktop/src/renderer/src/api/client.ts');
    assert.match(client, /getSchedulerRunsSince\(since: number, limit = 20, offset = 0\)/);
    assert.match(client, /requireFeature\('scheduler\.runs\.list'\)/);
    assert.doesNotMatch(client, /catch\(\(\) => \[\]\)/);
});
