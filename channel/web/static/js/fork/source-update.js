/* The version menu's sole fork updater. Fixed target IDs are server-issued;
 * identity changes abort requests and discard confirmations/polling state. */
(function (root) {
    'use strict';
    let epoch = 0, controller = null, timer = null, checked = null, busy = false;
    const text = (zh, en) => typeof currentLang === 'undefined' || currentLang === 'zh' ? zh : en;
    const element = id => document.getElementById(id);
    const admin = () => {
        const self = typeof _baseAccountSelf === 'function' && _baseAccountSelf();
        return !!(self && self.user && self.user.is_platform_admin && _accountAppVisible);
    };
    function render(message, enabled = true) {
        element('source-update-status').textContent = message || '';
        element('update-action-item').disabled = busy || !enabled;
        element('update-action-label').textContent = checked && !checked.up_to_date
            ? text('立即更新', 'Update now') : text('检查更新', 'Check for updates');
        element('update-dot').classList.toggle('hidden', !checked || checked.up_to_date);
    }
    function reset() {
        epoch++; checked = null; busy = false;
        if (controller) controller.abort();
        controller = null;
        if (timer) clearTimeout(timer);
        timer = null;
        element('source-update-open').classList.add('hidden');
        element('update-menu').classList.add('hidden');
        element('source-update-target').textContent = '';
        render('');
    }
    async function request(url, body, generation) {
        if (!admin() || generation !== epoch) throw new Error('identity_changed');
        controller = new AbortController();
        const response = await fetch(url, {signal: controller.signal, ...(body === undefined ? {} : {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        })});
        const data = await response.json();
        if (generation !== epoch || !admin()) throw new Error('identity_changed');
        if (!response.ok || data.status === 'error') {
            const error = new Error(data.code || data.message || 'update_failed');
            error.status = response.status;
            throw error;
        }
        return data;
    }
    root.toggleUpdateMenu = async function toggleUpdateMenu(event) {
        if (event) event.stopPropagation();
        if (!admin()) return;
        element('update-menu').classList.toggle('hidden');
        if (element('update-menu').classList.contains('hidden')) return;
        const generation = epoch;
        try {
            const metadata = await request('/api/version', undefined, generation);
            render(metadata.update_supported ? '' : metadata.unsupported_reason || text('此部署不支持源码更新', 'Source updates unavailable'), metadata.update_supported);
            if (metadata.update_supported) {
                const state = await request('/api/update/status', undefined, generation);
                if (state.state !== 'idle') await poll(generation, state);
            }
        } catch (error) { if (generation === epoch) render(error.message, false); }
    };
    root.checkForConsoleUpdate = async function checkForConsoleUpdate() {
        if (busy || !admin()) return;
        const generation = epoch;
        busy = true; checked = null; render(text('正在检查…', 'Checking…'));
        try {
            const result = await request('/api/update/check', {}, generation);
            checked = result;
            element('source-update-target').textContent = result.source + ': ' + result.current_sha.slice(0, 12) + ' → ' + result.target_sha.slice(0, 12);
            busy = false;
            render(result.up_to_date ? text('已是最新版本', 'Up to date') : text('目标已确认，更新时将暂时停服', 'Target checked; update requires downtime'));
        } catch (error) { if (generation === epoch) { busy = false; render(error.message); } }
    };
    async function poll(generation, initial) {
        if (generation !== epoch || !admin()) return;
        try {
            const status = initial || await request('/api/update/status', undefined, generation);
            const terminal = ['idle', 'completed', 'rolled_back', 'failed', 'recovery_required'].includes(status.state);
            busy = !terminal;
            const labels = {
                idle: text('尚未启动，请重新检查', 'Not started; check again'),
                queued: text('等待更新', 'Queued'), preparing: text('准备候选版本', 'Preparing release'),
                stopping: text('停止服务', 'Stopping service'), backup: text('备份实例', 'Backing up instance'),
                rehearsing: text('验证恢复', 'Verifying restore'), starting: text('启动候选版本', 'Starting release'),
                health: text('检查服务健康', 'Checking health'), promoting: text('开放服务', 'Resuming service'),
                restoring: text('正在恢复旧版本', 'Restoring previous release'),
                completed: text('更新完成，刷新页面以加载新版本', 'Updated; reload to load the new version'),
                rolled_back: text('更新失败，已恢复旧版本及数据', 'Update failed; previous release and data restored'),
                failed: text('更新失败，原服务未替换', 'Update failed before replacement'),
                recovery_required: text('需要管理员处理，未自动覆盖数据', 'Operator recovery required; data was not overwritten')
            };
            if (terminal) checked = null;
            render((labels[status.state] || status.state) + (status.code ? ' (' + status.code + ')' : ''), status.state !== 'recovery_required');
            if (terminal) return;
        } catch (error) {
            if (generation !== epoch) return;
            if ([401, 403].includes(error.status)) { reset(); return; }
            render(text('等待服务恢复，稍后继续检查进度', 'Waiting for service; progress checks will resume'));
        }
        timer = setTimeout(() => poll(generation), 1500);
    }
    root.startConsoleUpdate = function startConsoleUpdate() {
        if (busy || !admin() || !checked || checked.up_to_date) return;
        const generation = epoch, checkId = checked.check_id;
        showConfirmModal(text('确认更新', 'Confirm update'), text('更新至 ', 'Update to ') + checked.target_sha.slice(0, 12), async () => {
            if (generation !== epoch || !admin() || !checked || checked.check_id !== checkId || busy) return;
            busy = true; render(text('正在启动更新…', 'Starting update…'));
            try {
                await request('/api/update/start', {check_id: checkId}, generation);
                await poll(generation);
            } catch (error) {
                if (generation === epoch) {
                    checked = null;
                    if (!error.status) await poll(generation);
                    else { busy = false; render(error.message); }
                }
            }
        });
    };
    root.onUpdateActionClick = () => checked && !checked.up_to_date ? root.startConsoleUpdate() : root.checkForConsoleUpdate();
    root.addEventListener('account-invalidated', reset);
    root.addEventListener('pagehide', reset);
    root.addEventListener('account-ready', () => {
        reset();
        element('source-update-open').textContent = text('检查更新', 'Check for updates');
        element('source-update-open').classList.toggle('hidden', !admin());
    });
})(window);
