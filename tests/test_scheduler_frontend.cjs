// The ported "Tasks" view, driven through the page's real three layers (see
// tests/_tasks_page.cjs): upstream's fragments' scripts, console.js's helpers
// and the fork patch module that owns everything upstream has no notion of.
//
// What the patch module owes the fork, and what this file locks:
//
//   * a refusal is a terminal, readable state -- the closed consumer that
//     returns 503 in database identity mode used to leave the hardcoded
//     "Loading..." placeholder up forever;
//   * the per-action projection decides which controls exist, so the page never
//     offers a verb the five scheduler handlers would refuse (a closed
//     `scheduler.runs.list` must not even be fetched);
//   * an existing task keeps its delivery target: the edit surface shows the
//     current instance + recipient as fields of record and saving them again is
//     a no-op server-side (TaskAccessService.update_task rejects a *changed*
//     receiver/channel_type as `forged_field`);
//   * an answer that lands after a tenant switch is dropped instead of painted.
//
// The card-level verbs (`task.capabilities`) are locked next door by
// tests/test_recovered_pages_frontend.cjs, and the markup/i18n/serving side by
// tests/test_task_page_assets.py and tests/test_upstream_drift_guards.py.
const { test } = require('node:test');
const assert = require('node:assert/strict');

const { boot, flush } = require('./_tasks_page.cjs');

const TASK = {
    id: 't-nightly', name: 'nightly', enabled: true, scope: 'personal', agent_id: 'owner',
    schedule: { type: 'cron', expression: '0 2 * * *' },
    action: { type: 'send_message', content: 'run the report' },
    capabilities: { view: true, manage: true, run: true },
};

// The task list reads one URL; answer it and leave the rest to the defaults.
const withTasks = (payload, extra = {}) => boot({
    answers: { '/api/scheduler?': payload, ...extra.answers },
    features: extra.features,
    context: extra.context,
});

const emptyLine = page => page.get('tasks-empty').querySelector('p');

// ---------------------------------------------------------------------------
// 1. A refusal ends in a readable state
// ---------------------------------------------------------------------------

test('a closed scheduler consumer surfaces a reason instead of hanging on Loading', async () => {
    const page = withTasks({
        status: 'error', message: 'unavailable in database identity mode', code: 'database_unavailable',
    });
    await page.sandbox.loadTasksView();
    assert.equal(emptyLine(page).textContent, 'tasks_unavailable');
    assert.equal(page.get('tasks-empty').classList.contains('hidden'), false, 'the reason is visible');
    assert.equal(page.get('tasks-list').classList.contains('hidden'), true, 'no list is shown');
});

test('a non-closed error shows the server reason it was sent', async () => {
    const page = withTasks({ status: 'error', code: 'forbidden', message: 'not your Agent' });
    await page.sandbox.loadTasksView();
    assert.equal(emptyLine(page).textContent, 'not your Agent');
    assert.equal(page.get('tasks-list').classList.contains('hidden'), true);
});

test('a failure that is not even JSON still ends in a stated reason', async () => {
    const page = boot();
    page.sandbox.fetch = () => Promise.reject(new Error('network down'));
    await page.sandbox.loadTasksView();
    assert.equal(emptyLine(page).textContent, 'tasks_unavailable',
        'a dropped request must not be labelled as an empty schedule');
});

test('a successful list renders and clears the Loading placeholder', async () => {
    const page = withTasks({ status: 'success', tasks: [TASK] });
    await page.sandbox.loadTasksView();
    assert.equal(page.get('tasks-empty').classList.contains('hidden'), true);
    assert.equal(page.get('tasks-list').classList.contains('hidden'), false);
    const card = page.get('tasks-list').children[0];
    assert.ok(card, 'a card is appended');
    assert.match(card.innerHTML, /nightly/, 'the card renders the task name');
});

test('a genuinely empty schedule is not rendered as a refusal', async () => {
    const empty = withTasks({ status: 'success', tasks: [] });
    await empty.sandbox.loadTasksView();
    const text = emptyLine(empty).textContent;
    assert.match(text, /暂无定时任务|No scheduled tasks/, text);
    assert.notEqual(text, 'tasks_unavailable');
});

test('the whole team ledger is requested, not the active chat Agent', async () => {
    const page = withTasks({ status: 'success', tasks: [] });
    await page.sandbox.loadTasksView();
    assert.deepEqual(page.requests.map(r => r.url), ['/api/scheduler?agent_id='],
        'an empty agent_id is the aggregate view the server expects');
});

// ---------------------------------------------------------------------------
// 2. Per-action gating: no control without the projection behind it
// ---------------------------------------------------------------------------

test('a closed create action removes the add button', async () => {
    const page = withTasks({ status: 'success', tasks: [TASK] },
                           { features: { 'scheduler.create': false, 'scheduler.runs.list': true } });
    await page.sandbox.loadTasksView();
    assert.equal(page.get('task-add-btn').style.display, 'none');
});

test('an open create action leaves the add button alone', async () => {
    const page = withTasks({ status: 'success', tasks: [TASK] },
                           { features: { 'scheduler.create': true, 'scheduler.runs.list': true } });
    await page.sandbox.loadTasksView();
    assert.equal(page.get('task-add-btn').style.display, '');
});

test('a closed records action removes the tab and issues no request', async () => {
    const page = withTasks({ status: 'success', tasks: [TASK] },
                           { features: { 'scheduler.create': true, 'scheduler.runs.list': false } });
    await page.sandbox.loadTasksView();
    assert.equal(page.get('tasks-tab-records').style.display, 'none');
    // Reaching the pane directly (a deep link, a stale tab) must not fetch what
    // the projection closed, and must not read as "nothing ran yet".
    await page.sandbox.loadRunsView();
    assert.deepEqual(page.requests.map(r => r.url), ['/api/scheduler?agent_id='],
        'the records request must not be sent');
    assert.equal(page.get('runs-empty').classList.contains('hidden'), false);
    assert.equal(page.get('runs-empty').querySelectorAll('p')[0].textContent,
                 'scheduler_records_unavailable');
});

test('a closed delete action detaches the delete control from a record', async () => {
    const page = boot({ features: { 'scheduler.runs.delete': false, 'scheduler.runs.list': true } });
    const card = page.sandbox.renderRunCard({ run_id: 'r1', task_name: 'nightly', status: 'success' });
    assert.match(card.innerHTML, /run-delete-btn/, 'upstream draws the control');
    assert.equal(card.querySelector('.run-delete-btn').removed, true,
        'the fork takes it back out when the server owns the verb');
});

test('the delete control stays when the projection grants it', async () => {
    const page = boot({ features: { 'scheduler.runs.delete': true, 'scheduler.runs.list': true } });
    const card = page.sandbox.renderRunCard({ run_id: 'r1', task_name: 'nightly', status: 'success' });
    assert.match(card.innerHTML, /run-delete-btn/);
    assert.notEqual(card.querySelector('.run-delete-btn').removed, true);
});

// ---------------------------------------------------------------------------
// 3. The execution records pane keeps refusal and emptiness apart
// ---------------------------------------------------------------------------

test('a refused records read states the reason rather than an empty history', async () => {
    const page = boot({
        features: { 'scheduler.runs.list': true },
        answers: { '/api/scheduler/runs': { status: 'error', message: 'unavailable in database identity mode' } },
    });
    await page.sandbox.loadRunsView();
    const [message, guide] = page.get('runs-empty').querySelectorAll('p');
    assert.equal(message.textContent, 'scheduler_records_unavailable');
    assert.equal(guide.classList.contains('hidden'), true,
        'the "records show up here once a task runs" guide is only true for an empty history');
    assert.equal(page.get('runs-loading').classList.contains('hidden'), true, 'the spinner is stopped');
});

test('a genuinely empty history keeps its own copy', async () => {
    const page = boot({
        features: { 'scheduler.runs.list': true },
        answers: { '/api/scheduler/runs': { status: 'success', runs: [] } },
    });
    await page.sandbox.loadRunsView();
    const [message, guide] = page.get('runs-empty').querySelectorAll('p');
    assert.equal(message.textContent, 'records_empty');
    assert.equal(guide.classList.contains('hidden'), false);
});

test('records render, and a later page of them is appended', async () => {
    const page = boot({
        features: { 'scheduler.runs.list': true },
        answers: {
            '/api/scheduler/runs': {
                status: 'success',
                runs: [{ run_id: 'r1', task_name: 'nightly', status: 'success', started_at: 1, ended_at: 2 }],
            },
        },
    });
    await page.sandbox.loadRunsView();
    const list = page.get('runs-list');
    assert.equal(list.classList.contains('hidden'), false);
    assert.equal(list.children.length, 1);
    assert.match(list.children[0].innerHTML, /nightly/);
});

// ---------------------------------------------------------------------------
// 4. An existing task keeps its delivery target
// ---------------------------------------------------------------------------

// An IM task with a settled target, plus the directory that would let someone
// move it somewhere else.
const IM_TASK = {
    ...TASK,
    id: 't-im', name: 'push to support',
    action: {
        type: 'send_message', content: 'daily digest',
        channel_type: 'wecom', instance_id: 'inst-a', receiver: 'zhang',
        receiver_name: 'Zhang', notify_session_id: 'sess-1', is_group: false,
    },
};
const DIRECTORY = {
    '/api/scheduler/instances': {
        status: 'success',
        instances: [{ instance_id: 'inst-a', name: 'Support', channel_type: 'wecom' },
                    { instance_id: 'inst-b', name: 'Ops', channel_type: 'wecom' }],
    },
    '/api/scheduler/recipients': {
        status: 'success',
        recipients: [{ instance_id: 'inst-a', channel_type: 'wecom', receiver: 'zhang', name: 'Zhang' },
                     { instance_id: 'inst-b', channel_type: 'wecom', receiver: 'li', name: 'Li' }],
    },
};

test('opening an existing task shows its target read-only, not as a picker', async () => {
    const page = withTasks({ status: 'success', tasks: [IM_TASK] }, { answers: DIRECTORY });
    await page.sandbox.openTaskEditModal(IM_TASK);
    await flush();

    const instance = page.get('task-edit-instance');
    const recipient = page.get('task-edit-recipient');
    assert.equal(instance._ddReadOnly, true, 'the channel instance cannot be switched');
    assert.equal(instance._ddValue, 'inst-a', 'it shows the instance the task delivers to');
    assert.equal(recipient._ddReadOnly, true, 'the recipient cannot be switched');
    assert.equal(recipient._ddValue, 'inst-a:zhang', 'it shows the recipient the task delivers to');
    for (const el of [instance, recipient]) {
        assert.equal(el.classList.contains('cfg-dropdown-disabled'), true,
            'a read-only control must not look openable');
    }
    assert.equal(page.get('task-instance-tip').dataset['attr_data-tip-key'], 'task_target_locked',
        'the tip explains why the target cannot move');
});

test('saving an existing task reposts its target unchanged', async () => {
    const page = withTasks({ status: 'success', tasks: [IM_TASK] }, {
        answers: { ...DIRECTORY, '/api/scheduler/update': { status: 'success' } },
    });
    await page.sandbox.openTaskEditModal(IM_TASK);
    await flush();
    page.get('task-edit-name').value = 'push to support (renamed)';
    await page.sandbox.saveTaskEdit();

    const update = page.requests.find(r => r.url === '/api/scheduler/update');
    assert.ok(update, 'the edit is posted');
    assert.equal(update.body.action.receiver, 'zhang',
        'a changed receiver is refused server-side as forged_field');
    assert.equal(update.body.action.channel_type, 'wecom');
    assert.equal(update.body.action.instance_id, 'inst-a');
    assert.equal(update.body.action.receiver_name, 'Zhang', 'the stored metadata is preserved');
    assert.equal(update.body.action.notify_session_id, 'sess-1');
    assert.equal(update.body.action.content, 'daily digest');
});

test('a target that is no longer offered still shows the task\'s own value', async () => {
    // The instance directory moved on (the binding was removed); the task still
    // delivers there, so the field must show that and not the first option.
    const page = withTasks({ status: 'success', tasks: [IM_TASK] }, {
        answers: {
            '/api/scheduler/instances': {
                status: 'success',
                instances: [{ instance_id: 'inst-b', name: 'Ops', channel_type: 'wecom' }],
            },
            '/api/scheduler/recipients': { status: 'success', recipients: [] },
        },
    });
    await page.sandbox.openTaskEditModal(IM_TASK);
    await flush();
    assert.equal(page.get('task-edit-instance')._ddValue, 'inst-a',
        'the stored instance is shown even though the directory no longer lists it');
    assert.equal(page.get('task-edit-recipient')._ddValue, 'inst-a:zhang');
});

test('creating a task can still choose a target', async () => {
    const page = withTasks({ status: 'success', tasks: [IM_TASK] }, {
        answers: { ...DIRECTORY, '/api/scheduler/create': { status: 'success' } },
    });
    // Edit first, then create: the create dialog reuses the same controls, so a
    // lock that outlived the edit would leave the new task unpickable.
    await page.sandbox.openTaskEditModal(IM_TASK);
    await flush();
    await page.sandbox.openTaskCreateModal();
    await flush();

    const instance = page.get('task-edit-instance');
    assert.equal(instance._ddReadOnly, false, 'the instance picker is interactive again');
    assert.equal(instance.classList.contains('cfg-dropdown-disabled'), false);
    assert.equal(instance._ddValue, '',
        'create mode pre-picks nothing: the target is an explicit choice');
    assert.equal(instance.querySelector('.cfg-dropdown-text').textContent,
                 'task_instance_placeholder', 'the picker is showing its placeholder');
    assert.equal(page.get('task-instance-tip').dataset['attr_data-tip-key'], 'task_instance_tip');
});

// ---------------------------------------------------------------------------
// 5. An answer that lands after the identity moved is dropped
// ---------------------------------------------------------------------------

test('a task list that resolves after a tenant switch does not paint', async () => {
    const page = withTasks({ status: 'success', tasks: [TASK] });
    const inFlight = page.sandbox.loadTasksView();
    page.moveIdentity();
    await inFlight;
    await flush();
    assert.equal(page.get('tasks-list').children.length, 0,
        'the previous tenant\'s rows must not appear under the new one');
    assert.equal(page.get('tasks-list').classList.contains('hidden'), true);
});

test('a records page that resolves after a tenant switch does not paint', async () => {
    const page = boot({
        features: { 'scheduler.runs.list': true },
        answers: {
            '/api/scheduler/runs': {
                status: 'success',
                runs: [{ run_id: 'r1', task_name: 'nightly', status: 'success' }],
            },
        },
    });
    const inFlight = page.sandbox.loadRunsView();
    page.moveIdentity();
    await inFlight;
    await flush();
    assert.equal(page.get('runs-list').children.length, 0);
});
