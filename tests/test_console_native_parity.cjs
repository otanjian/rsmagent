// Actual Electron WebContentsView + production IPC/preload. HTTP identities are
// controlled fixture responses; this is not installed-package/device pairing acceptance.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync, spawnSync } = require('node:child_process');
exports.run = async function (origin, output) {
    const repo = path.resolve(__dirname, '..');
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'cow-native-parity-'));
    const paths = execFileSync(process.env.PYTHON_BIN || path.join(repo, '.venv/bin/python'),
        ['-c', 'import json; from channel.web.route_registry import shell_entry_paths; print(json.dumps(shell_entry_paths()))'],
        { cwd: repo, encoding: 'utf8' }).trim();
    const { _electron } = require('playwright');
    let app;
    const report = { platform: process.platform, electron: '', scenarios: [], pageErrors: [],
        scope: 'production Electron container/preload/IPC; controlled HTTP identities; no installed-package acceptance' };
    async function scenario(name, fn) {
        try { await fn(); report.scenarios.push({ name, passed: true }); }
        catch (error) { report.scenarios.push({ name, passed: false, message: error.message }); throw error; }
    }
    try {
        const env = { ...process.env, COW_NATIVE_ORIGIN: origin, COW_NATIVE_PROFILE: profile,
            COW_NATIVE_PATHS: paths };
        delete env.ELECTRON_RUN_AS_NODE;
        app = await _electron.launch({ executablePath: require('../desktop/node_modules/electron'),
            args: [path.join(__dirname, 'fixtures/console-native-parity.cjs')], env });
        report.electron = await app.evaluate(() => process.versions.electron);
        const context = app.context();
        await context.addInitScript(() => sessionStorage.setItem('cow_tenant_id', 'fixture-tenant'));
        const page = context.pages()[0] || await context.waitForEvent('page');
        page.setDefaultTimeout(15000);
        page.on('pageerror', error => report.pageErrors.push(error.message));
        await page.reload();
        await page.locator('#app').waitFor({ state: 'visible' });
        await scenario('native: registered deep route, refresh, back and bridge generation', async () => {
            await page.waitForFunction(() => currentView === 'knowledge' && window.CowDesktopHost?.isDesktop());
            assert.equal(new URL(page.url()).pathname, '/knowledge/graph');
            assert.ok((await page.evaluate(() => desktopHost.getCapabilities())).bridge);
            const first = await app.evaluate(() => global.parityContainer.generation);
            await page.locator('#knowledge-tab-sources').click();
            await page.waitForURL('**/knowledge/sources');
            await page.reload();
            await page.locator('#knowledge-panel-sources').waitFor({ state: 'visible' });
            assert.ok((await page.evaluate(() => desktopHost.getCapabilities())).bridge);
            assert.ok(await app.evaluate(() => global.parityContainer.generation) > first);
            await page.evaluate(() => navigateTo('chat'));
            await page.waitForURL(origin + '/');
            await page.goBack();
            await page.waitForURL('**/knowledge/sources');
            await page.locator('#knowledge-panel-sources').waitFor({ state: 'visible' });
            assert.equal(await page.evaluate(() => typeof require), 'undefined');
        });
        await scenario('native: Web logout precedes host sign-out and new document rebinds', async () => {
            const logout = page.waitForResponse(r => new URL(r.url()).pathname === '/auth/logout');
            const result = await page.evaluate(() => CowDesktopAccount.logout());
            assert.equal((await logout).status(), 200);
            assert.equal(result.ok, true);
            assert.equal(await app.evaluate(() => global.paritySignOuts), 1);
            await page.reload();
            await page.locator('#app').waitFor({ state: 'visible' });
            assert.ok((await page.evaluate(() => desktopHost.getCapabilities())).bridge);
            assert.equal(new URL(page.url()).pathname, '/knowledge/sources');
        });
        assert.deepEqual(report.pageErrors, []);
        await page.screenshot({ path: path.join(output, 'native-container.png') });
    } finally {
        fs.writeFileSync(path.join(output, 'native-results.json'), JSON.stringify(report, null, 2));
        if (app) await app.close();
        fs.rmSync(profile, { recursive: true, force: true });
        console.log('Native parity artifact: ' + path.join(output, 'native-results.json'));
    }
};
if (require.main === module) {
    const result = spawnSync(process.execPath, [require.resolve('./test_knowledge_console_browser.cjs')],
        { stdio: 'inherit', env: { ...process.env, COW_NATIVE_PARITY: '1' } });
    if (result.error) throw result.error;
    process.exitCode = result.status === null ? 1 : result.status;
}
