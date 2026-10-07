/* Authorized terminal notifications. One consumer, independent of the active
 * Agent/session. IndexedDB read/write transactions arbitrate between windows;
 * localStorage read-then-write is not an atomic claim. */
(function (root) {
    'use strict';
    function database(factory) {
        let opening;
        const open = () => opening || (opening = new Promise((resolve, reject) => {
            const request = factory.open('cow-scheduler-notifications-v1', 1);
            request.onupgradeneeded = () => request.result.createObjectStore('state');
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => { opening = null; reject(request.error); };
        }));
        async function transaction(action) {
            const db = await open();
            return new Promise((resolve, reject) => {
                const tx = db.transaction('state', 'readwrite'), records = tx.objectStore('state');
                let result;
                tx.oncomplete = () => resolve(result);
                tx.onerror = tx.onabort = () => reject(tx.error || new Error('Notification storage unavailable'));
                action(records, value => { result = value; });
            });
        }
        return {
            read: key => transaction((s, done) => { s.get(key).onsuccess = e => done(e.target.result); }),
            advance: (key, cursor) => transaction((s, done) => {
                s.get(key).onsuccess = e => { s.put(Math.max(e.target.result || 0, cursor), key); done(cursor); };
            }),
            manualOrigin: (key, task, owner) => transaction((s, done) => {
                s.put({owner, expires: Date.now() + 30000}, key + ':manual:' + task); done(true);
            }),
            claim: (key, run, owner) => transaction((s, done) => {
                const id = key + ':run:' + run.run_id;
                s.get(id).onsuccess = event => {
                    if (event.target.result) { done(false); return; }
                    const claim = () => { s.put(Date.now(), id); done(true); };
                    if (run.trigger === 'manual' && owner) {
                        s.get(key + ':manual:' + run.agent_id + ':' + run.task_id).onsuccess = e => {
                            const origin = e.target.result;
                            if (origin && origin.owner !== owner && origin.expires > Date.now()) {
                                done(null); // retry without advancing this window's cursor
                            } else claim();
                        };
                    } else claim();
                };
            }),
        };
    }
    function create(options) {
        let generation = 0, timer = null, controller = null, current = null;
        const storage = options.storage;
        function stop() {
            generation++; if (timer !== null) options.clearTimer(timer);
            timer = null; if (controller) controller.abort(); controller = null; current = null;
            if (options.close) options.close();
        }
        async function start(identity) {
            stop(); if (!identity) return;
            current = identity; const gen = generation;
            const key = JSON.stringify([identity.server, identity.user, identity.tenant]);
            const valid = () => gen === generation && options.current(identity);
            async function tick() {
                if (!valid()) { if (gen === generation) stop(); return; }
                controller = new AbortController();
                try {
                    if (options.beforePoll) await options.beforePoll(key);
                    let cursor = await storage.read(key);
                    let more;
                    do {
                        if (!valid()) return;
                        const page = await options.fetchPage(cursor, controller.signal);
                        if (!valid()) return;
                        if (!page || page.status !== 'success' || !Array.isArray(page.runs)
                            || !Number.isSafeInteger(page.cursor) || page.cursor < (cursor || 0)) {
                            throw new Error('Invalid terminal cursor response');
                        }
                        for (const run of page.runs) {
                            if (!valid()) return;
                            if (run.status !== 'done' || run.channel_type !== 'web' || !run.session_id) continue;
                            if (options.eligible && !options.eligible(run)) continue;
                            const claimed = await storage.claim(key, run, options.owner);
                            if (claimed === null) throw new Error('Waiting for manual run origin');
                            if (claimed && valid()) await options.notify(run, identity);
                        }
                        if (!valid()) return;
                        if (page.has_more && page.cursor === cursor) throw new Error('Cursor did not advance');
                        await storage.advance(key, page.cursor);
                        cursor = page.cursor; more = page.has_more === true;
                    } while (more && valid());
                } catch (error) {
                    if (error.status === 401 || error.status === 403) { if (gen === generation) stop(); return; }
                    // Transient failures preserve the last committed cursor.
                } finally {
                    if (valid()) timer = options.setTimer(tick, options.interval || 10000);
                }
            }
            await tick();
        }
        return {start, stop, identity: () => current};
    }
    const api = {create, database};
    if (typeof module !== 'undefined' && module.exports) { module.exports = api; return; }
    root.SchedulerNotificationInternals = api;
    const notifications = new Set();
    const manualTasks = new Map();
    const owner = crypto.randomUUID();
    const storage = database(indexedDB);
    function identity() {
        if (!_accountAppVisible || !_accountState.authenticated || !_featureAvailable('scheduler.runs.list')) return null;
        const self = _baseAccountSelf();
        const user = self && self.user && self.user.id, tenant = sessionStorage.getItem('cow_tenant_id');
        return user && tenant ? {server: location.origin, user, tenant, epoch: _authEpoch} : null;
    }
    function current(who) {
        const now = identity();
        return !!now && now.server === who.server && now.user === who.user
            && now.tenant === who.tenant && now.epoch === who.epoch;
    }
    async function click(run, who) {
        if (!current(who)) return;
        // Run preview permission does not grant the delivered conversation.
        const url = '/api/history?session_id=' + encodeURIComponent(run.session_id)
            + '&agent_id=' + encodeURIComponent(run.agent_id) + '&page=1&page_size=1';
        const response = await fetch(url);
        const data = await response.json();
        if (!current(who)) return;
        if (!response.ok || data.status !== 'success') { _wsToast(t('nav_denied')); return; }
        window.focus();
        navigateTo('chat', () => { if (current(who)) switchSession(run.session_id, run.agent_id); });
    }
    const observer = create({
        storage, current, owner,
        beforePoll: async key => {
            for (const [task, expires] of manualTasks) {
                if (expires < Date.now()) manualTasks.delete(task);
                else await storage.manualOrigin(key, task, owner);
            }
        },
        setTimer: (fn, delay) => setTimeout(fn, delay), clearTimer: id => clearTimeout(id),
        close: () => { notifications.forEach(n => n.close()); notifications.clear(); },
        fetchPage: async (cursor, signal) => {
            const response = await fetch('/api/scheduler/runs?mode=notifications&agent_id=&limit=100'
                + (cursor === undefined ? '' : '&cursor=' + cursor), {signal});
            if (!response.ok) { const error = new Error('Notification query failed'); error.status = response.status; throw error; }
            return response.json();
        },
        notify: async (run, who) => {
            if (!current(who)) return;
            if (run.trigger === 'manual') manualTasks.delete(run.agent_id + ':' + run.task_id);
            if (!taskNotifyEnabled) return;
            if (currentView === 'chat' && sessionId === run.session_id && activeAgentId === run.agent_id && document.hasFocus()) return;
            const title = run.task_name || t('notify_task_done');
            const body = firstLineSnippet(run.output_preview || '') || t('notify_task_done');
            playNotifyBeep();
            if (document.hidden) { unreadCount++; document.title = `(${unreadCount}) ${baseDocTitle}`; }
            if (typeof Notification === 'undefined' || Notification.permission !== 'granted') { refreshNotifyBlockedHint(); return; }
            const notification = new Notification(title, {body, tag: run.run_id});
            notifications.add(notification);
            notification.onclose = () => notifications.delete(notification);
            notification.onclick = () => { notification.close(); click(run, who).catch(() => {}); };
        },
    });
    observer.registerManualRun = async task => {
        const who = identity(); if (!who) return;
        const key = JSON.stringify([who.server, who.user, who.tenant]);
        const id = (task.agent_id || activeAgentId) + ':' + task.id;
        manualTasks.set(id, Date.now() + 24 * 60 * 60 * 1000);
        await storage.manualOrigin(key, id, owner);
    };
    root.SchedulerNotifications = observer;
    root.addEventListener('account-ready', () => observer.start(identity()));
    root.addEventListener('account-invalidated', () => { manualTasks.clear(); observer.stop(); });
    root.addEventListener('pagehide', () => observer.stop());
})(typeof window === 'undefined' ? globalThis : window);
