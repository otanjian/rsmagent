const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');
const start = source.indexOf('function paintWelcomeAgentIntro()');
const end = source.indexOf('function renderWelcomeScreen()', start);
assert.ok(start >= 0 && end > start);

function setup() {
    const heading = { textContent: '' }, description = { textContent: '' };
    for (const el of [heading, description]) {
        Object.defineProperty(el, 'innerHTML', { set() { throw new Error('Intro must remain plain text'); } });
    }
    const welcome = { querySelector: selector => selector === '.home-greeting' ? heading : description };
    const ctx = {
        activeAgentId: 'a', chatAgentCatalog: [], managedAgents: [], welcome, coding: false,
        t: key => ({ home_agent_greeting: '你好，我是{name}', home_agent_description: '告诉我你的任务。',
            home_greeting: '通用欢迎标题', home_description: '通用欢迎简介' })[key],
        codingPaneMounted: () => ctx.coding,
        findAgent: id => ctx.managedAgents.find(a => a.id === id),
        document: { getElementById: () => ctx.welcome },
        fetch: () => { throw new Error('Intro must not send or start a run'); },
    };
    vm.createContext(ctx);
    vm.runInContext(source.slice(start, end), ctx);
    return { ctx, heading, description };
}

test('intro uses the fresh chat roster and displays configured content as plain text', () => {
    const { ctx, heading, description } = setup();
    ctx.managedAgents = [{ id: 'a', name: '旧名称', greeting: '旧问候语' }];
    ctx.chatAgentCatalog = [{ id: 'a', name: 'BUG $& <管家>', greeting: '  我是 BUG 管家。\n<img src=x onerror=alert(1)>  ', description: '简介' }];
    ctx.paintWelcomeAgentIntro();
    assert.equal(heading.textContent, '你好，我是BUG $& <管家>');
    assert.equal(description.textContent, '我是 BUG 管家。\n<img src=x onerror=alert(1)>');
});

test('switching the empty chat uses the new owner, with description and neutral fallbacks', () => {
    const { ctx, heading, description } = setup();
    ctx.chatAgentCatalog = [
        { id: 'a', name: '分析参谋', greeting: '欢迎分析经营数据' },
        { id: 'b', name: '资料助手', greeting: ' ', description: ' 整理资料。 ' },
        { id: 'c', name: '新助手' },
    ];
    ctx.paintWelcomeAgentIntro();
    assert.equal(description.textContent, '欢迎分析经营数据');
    ctx.activeAgentId = 'b';
    ctx.paintWelcomeAgentIntro();
    assert.equal(heading.textContent, '你好，我是资料助手');
    assert.equal(description.textContent, '整理资料。');
    ctx.activeAgentId = 'c';
    ctx.paintWelcomeAgentIntro();
    assert.equal(description.textContent, '告诉我你的任务。');
    ctx.chatAgentCatalog = [];
    ctx.paintWelcomeAgentIntro();
    assert.equal(heading.textContent, '通用欢迎标题');
    assert.equal(description.textContent, '通用欢迎简介');
});

test('history and the coding pane do not receive an extra introduction', () => {
    const { ctx, heading, description } = setup();
    ctx.chatAgentCatalog = [{ id: 'a', name: '助手', greeting: '你好' }];
    const welcome = ctx.welcome;
    ctx.welcome = null;
    ctx.paintWelcomeAgentIntro();
    assert.equal(description.textContent, '');
    ctx.welcome = welcome;
    ctx.coding = true;
    ctx.paintWelcomeAgentIntro();
    assert.equal(heading.textContent, '');
});

test('locale repaint translates the heading without changing authored greeting content', () => {
    const { ctx, heading, description } = setup();
    ctx.chatAgentCatalog = [{ id: 'a', name: 'BUG 管家', greeting: '由管理员编写的问候语' }];
    ctx.paintWelcomeAgentIntro();
    ctx.t = key => key === 'home_agent_greeting' ? "Hi, I'm {name}" : key;
    ctx.paintWelcomeAgentIntro();
    assert.equal(heading.textContent, "Hi, I'm BUG 管家");
    assert.equal(description.textContent, '由管理员编写的问候语');
});
