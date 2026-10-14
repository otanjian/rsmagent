// A missing core dependency names the module, and a missing optional tool does
// not block anything.
//
// Change ``harden-windows-desktop-reliability``, task 2.2: "复用构建检查与启动
// stderr，核心缺依赖停止就绪，可选工具首次使用前给出模块/运行时缺项（含
// Playwright）；验证聊天正常，不建立全量扫描或依赖注册表".
//
// Two halves, and both need a test rather than a promise:
//
//  * the *core* half -- the backend's own stderr says ``ModuleNotFoundError: No
//    module named 'x'``, and that has to become the unrecoverable
//    ``dependency_missing`` state rather than "the backend crashed";
//  * the *optional* half -- Playwright is checked when the browser tool is first
//    used, so a desktop without it still chats, and no full dependency scan runs
//    at startup (the task forbids a registry).
//
// Run: node --test tests/test_desktop_dependency_error.cjs

const { test, before } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const root = path.join(__dirname, '..');
const desktop = path.join(root, 'desktop');
const compiled = path.join(desktop, 'dist', 'main', 'dependency-error.js');
const managerSource = fs.readFileSync(
    path.join(desktop, 'src', 'main', 'python-manager.ts'), 'utf8');
const useBackendSource = fs.readFileSync(
    path.join(desktop, 'src', 'renderer', 'src', 'hooks', 'useBackend.ts'), 'utf8');
const browserTool = fs.readFileSync(
    path.join(root, 'agent', 'tools', 'browser', 'browser_tool.py'), 'utf8');
const probeSource = fs.readFileSync(
    path.join(desktop, 'src', 'main', 'python-probe.ts'), 'utf8');

let mod;

before(() => {
    const src = path.join(desktop, 'src', 'main', 'dependency-error.ts');
    if (!fs.existsSync(compiled) || fs.statSync(compiled).mtimeMs < fs.statSync(src).mtimeMs) {
        execFileSync(process.execPath,
            [require.resolve('typescript/bin/tsc', { paths: [desktop] }),
                '-p', 'tsconfig.main.json'],
            { cwd: desktop, stdio: 'pipe' });
    }
    delete require.cache[require.resolve(compiled)];
    mod = require(compiled);
});

test('the module Python named is what the shell reports', () => {
    assert.equal(mod.missingModuleFrom([
        '[INFO] starting',
        "ModuleNotFoundError: No module named 'xlsxwriter'",
        'Traceback (most recent call last):',
    ]), 'xlsxwriter');
    assert.equal(mod.missingModuleFrom(["ImportError: cannot import name 'tenacity'"]), 'tenacity');
});

test('the first named module wins, because the rest is a cascade', () => {
    assert.equal(mod.missingModuleFrom([
        "ModuleNotFoundError: No module named 'first_one'",
        "ModuleNotFoundError: No module named 'second_one'",
    ]), 'first_one');
});

test('ordinary crash output names nothing', () => {
    assert.equal(mod.missingModuleFrom(['OSError: [WinError 10048] the port is in use']), null);
    assert.equal(mod.missingModuleFrom(['ModuleNotFoundError: No module named']), null,
        'a line without a quoted module must not invent one');
    assert.equal(mod.missingModuleFrom([]), null);
});

test('the manager delegates, and the state is unrecoverable', () => {
    assert.match(managerSource, /import \{ missingModuleFrom \} from '\.\/dependency-error'/,
        'the classification is shared, not re-derived');
    assert.match(managerSource, /private missingModule\(\): string \| null \{\s*\n\s*return missingModuleFrom\(this\.recentLogs\)/,
        'and the manager must use it');
    assert.match(useBackendSource, /dependency_missing/);
    // `new Set<BackendErrorCode>([...])`, not a bare array literal.
    const unrecoverable = /UNRECOVERABLE_CODES[^=]*=\s*(?:new Set<[^>]*>\()?\[([\s\S]*?)\]/
        .exec(useBackendSource);
    assert.ok(unrecoverable, 'UNRECOVERABLE_CODES was not found');
    assert.ok(/dependency_missing/.test(unrecoverable[1]),
        'a missing core dependency must not offer a "retry" that cannot help');
});

test('the browser dependency check happens at first use, not at startup', () => {
    const execute = browserTool.indexOf('def execute(self, args');
    const check = browserTool.indexOf('_check_engine_ready()');
    assert.ok(execute > 0 && check > execute,
        'the playwright check belongs to the tool call, so chat is unaffected by it');
    const moduleScope = browserTool.slice(0, browserTool.indexOf('class '));
    assert.ok(!/^\s*import playwright/m.test(moduleScope),
        'importing the module must not require playwright');
});

test('nothing scans the whole environment at startup (no registry, no pip list)', () => {
    // Task 2.2's "不建立全量扫描或依赖注册表": the only dependency knowledge the
    // startup path may use is what Python itself printed.
    for (const [name, source] of [['python-manager.ts', managerSource],
        ['python-probe.ts', probeSource]]) {
        assert.ok(!/pip list|importlib\.metadata|pkg_resources|subprocess[^\n]*-m pip/i.test(source),
            `${name} must not scan the environment`);
    }
});
