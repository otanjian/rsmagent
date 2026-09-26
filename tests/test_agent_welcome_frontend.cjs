// Exercise the empty-chat onboarding in console.js (change
// improve-agent-chat-onboarding): one ordinary Agent shows its avatar, name,
// self-introduction, usage line and up-to-four questions; the questions send
// through the original composer and are disabled with a reason while the
// composer holds a draft or an attachment. Other empty-chat modes keep the six
// generic entry cards, and the copy is always plain text.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');
const start = source.indexOf('const AGENT_QUESTION_COUNT = 4;');
const end = source.indexOf('function renderWelcomeScreen()', start);
assert.ok(start >= 0 && end > start, 'the onboarding slice must be present');

function classList() {
    const set = new Set();
    return {
        add: (...c) => c.forEach(x => set.add(x)),
        remove: (...c) => c.forEach(x => set.delete(x)),
        contains: c => set.has(c),
        toggle: (c, on) => {
            if (on === undefined) set.has(c) ? set.delete(c) : set.add(c);
            else if (on) set.add(c); else set.delete(c);
        },
    };
}

function makeEl({ plainTextOnly = false } = {}) {
    const node = {
        children: [], dataset: {}, attrs: {}, _listeners: {}, _text: '',
        value: '', disabled: false, className: '',
        classList: classList(),
        addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
        appendChild(child) { this.children.push(child); child.parent = this; return child; },
        dispatch(type) { (this._listeners[type] || []).forEach(fn => fn()); },
        dispatchEvent(evt) { this.dispatch(evt && evt.type); return true; },
        querySelector: () => null,
    };
    // Real DOM: assigning textContent replaces the children, which is how
    // renderAgentQuestions() clears the host before rebuilding it.
    Object.defineProperty(node, 'textContent', {
        get() { return node._text; },
        set(v) { node._text = String(v); node.children = []; },
    });
    Object.defineProperty(node, 'innerHTML', {
        get() { return node._html || ''; },
        set(v) {
            if (plainTextOnly) throw new Error('Intro must remain plain text');
            node._html = String(v);
        },
    });
    return node;
}

const COPY = {
    home_agent_greeting: '你好，我是{name}',
    home_agent_description: '告诉我你的任务。',
    home_greeting: '通用欢迎标题',
    home_description: '通用欢迎简介',
    home_agent_usage_default: '描述目标 → 补充信息 → 确认下一步',
    home_agent_questions_label: '点击即发送',
    home_agent_questions_draft: '输入框已有内容，请先发送或清空后使用建议问题',
    home_agent_questions_default_1: '你能帮我做什么？',
    home_agent_questions_default_2: '开始之前，我需要准备什么？',
    home_agent_questions_default_3: '能说明一下你的工作步骤吗？',
    home_agent_questions_default_4: '可以给我一个使用示例吗？',
};

function setup() {
    const heading = makeEl({ plainTextOnly: true });
    const description = makeEl({ plainTextOnly: true });
    const avatar = makeEl();
    const usage = makeEl({ plainTextOnly: true });
    const onboarding = makeEl();
    const label = makeEl({ plainTextOnly: true });
    const questions = makeEl();
    const state = makeEl({ plainTextOnly: true });
    const suggestions = makeEl();
    const main = makeEl();
    const chatInput = makeEl();
    const lookup = {
        '#home-agent-avatar': avatar, '#home-agent-usage': usage,
        '#home-agent-onboarding': onboarding, '#home-agent-questions-label': label,
        '#home-agent-questions': questions, '#home-agent-questions-state': state,
        '.home-suggestions': suggestions,
    };
    const welcome = {
        querySelector: sel => sel === '.home-greeting' ? heading
            : (sel === '#welcome-subtitle' ? description : lookup[sel] || null),
    };
    const sends = [];
    const ctx = {
        activeAgentId: 'a', chatAgentCatalog: [], managedAgents: [], shared: false,
        welcome, main, chatInput, questionHost: questions, questionState: state,
        pendingAttachments: [], uploadingCount: 0, sendBtnMode: 'send',
        _identityMode: () => 'legacy',
        t: key => COPY[key] !== undefined ? COPY[key] : key,
        escapeHtml: x => String(x),
        codingPaneMounted: () => ctx.coding,
        sharedConversation: () => ctx.shared,
        findAgent: id => ctx.managedAgents.find(a => a.id === id),
        agentAvatarHTML: agent => `<img class="agent-avatar agent-avatar-56" data-id="${agent.id}">`,
        sendMessage: () => { sends.push(ctx.chatInput.value); },
        Event: class { constructor(type) { this.type = type; } },
        document: {
            getElementById: id => id === 'welcome-screen' ? ctx.welcome
                : (id === 'chat-main' ? ctx.main
                : (id === 'home-agent-questions' ? ctx.questionHost : ctx.questionState)),
            createElement: () => makeEl({ plainTextOnly: true }),
            querySelectorAll: () => ctx.questionHost.children,
        },
        fetch: () => { throw new Error('Intro must not send or start a run'); },
    };
    vm.createContext(ctx);
    vm.runInContext(source.slice(start, end), ctx);
    return { ctx, heading, description, avatar, usage, onboarding, label, questions, state, suggestions, main, sends, chatInput };
}

const questionTexts = questions => questions.children.map(b => b.textContent);

test('ordinary Agent shows avatar, introduction, usage and configured questions', () => {
    const h = setup();
    h.ctx.chatAgentCatalog = [{
        id: 'a', name: 'BUG $& <管家>', greeting: '  我是 BUG 管家。\n<img src=x onerror=alert(1)>  ',
        description: '简介', usage_hint: '  描述异常 → 补齐复现 → 记录缺陷  ',
        suggested_questions: ['带我登记 BUG', '需要哪些信息？'],
    }];
    h.ctx.paintWelcomeAgentIntro();

    assert.equal(h.heading.textContent, '你好，我是BUG $& <管家>');
    assert.equal(h.description.textContent, '我是 BUG 管家。\n<img src=x onerror=alert(1)>');
    assert.equal(h.usage.textContent, '描述异常 → 补齐复现 → 记录缺陷');
    assert.deepEqual(questionTexts(h.questions), ['带我登记 BUG', '需要哪些信息？']);
    assert.ok(h.avatar.innerHTML.includes('agent-avatar-56'), 'the avatar is rendered');
    assert.ok(!h.onboarding.classList.contains('hidden'), 'the onboarding block is shown');
    assert.ok(h.suggestions.classList.contains('hidden'), 'the six generic cards are hidden');
    assert.ok(h.main.classList.contains('agent-onboarding'), 'the home switches to the natural flow');
    assert.equal(h.questions.children[0].innerHTML, '', 'questions are text, never markup');
});

test('missing copy falls back to neutral usage and four default questions', () => {
    const h = setup();
    h.ctx.chatAgentCatalog = [{ id: 'a', name: '新助手' }];
    h.ctx.paintWelcomeAgentIntro();

    assert.equal(h.heading.textContent, '你好，我是新助手');
    assert.equal(h.description.textContent, '告诉我你的任务。');
    assert.equal(h.usage.textContent, COPY.home_agent_usage_default);
    assert.deepEqual(questionTexts(h.questions), [
        COPY.home_agent_questions_default_1, COPY.home_agent_questions_default_2,
        COPY.home_agent_questions_default_3, COPY.home_agent_questions_default_4,
    ]);
    assert.equal(h.questions.children.length, 4);
});

test('switching the empty chat uses the new owner', () => {
    const h = setup();
    h.ctx.chatAgentCatalog = [
        { id: 'a', name: '分析参谋', greeting: '欢迎分析经营数据', suggested_questions: ['看营收'] },
        { id: 'b', name: '资料助手', greeting: ' ', description: ' 整理资料。 ', usage_hint: ' ' },
    ];
    h.ctx.paintWelcomeAgentIntro();
    assert.deepEqual(questionTexts(h.questions), ['看营收']);
    assert.equal(h.usage.textContent, COPY.home_agent_usage_default);

    h.ctx.activeAgentId = 'b';
    h.ctx.paintWelcomeAgentIntro();
    assert.equal(h.heading.textContent, '你好，我是资料助手');
    assert.equal(h.description.textContent, '整理资料。');
    assert.deepEqual(questionTexts(h.questions), [
        COPY.home_agent_questions_default_1, COPY.home_agent_questions_default_2,
        COPY.home_agent_questions_default_3, COPY.home_agent_questions_default_4,
    ]);
});

test('other modes keep the generic home and no onboarding', () => {
    const h = setup();
    h.ctx.chatAgentCatalog = [{ id: 'a', name: '代码助手', agent_type: 'coding' }];
    h.ctx.paintWelcomeAgentIntro();
    assert.equal(h.heading.textContent, COPY.home_greeting);
    assert.equal(h.description.textContent, COPY.home_description);
    assert.ok(h.onboarding.classList.contains('hidden'));
    assert.ok(!h.suggestions.classList.contains('hidden'), 'coding keeps the generic cards');
    assert.ok(!h.main.classList.contains('agent-onboarding'));

    // A group chat that has invited others is not the ordinary single-Agent home.
    h.ctx.chatAgentCatalog = [{ id: 'a', name: '助手', suggested_questions: ['问题'] }];
    h.ctx.shared = true;
    h.ctx.paintWelcomeAgentIntro();
    assert.ok(h.onboarding.classList.contains('hidden'));
    assert.ok(!h.suggestions.classList.contains('hidden'));
});

test('history and the coding pane do not receive an extra introduction', () => {
    const h = setup();
    h.ctx.chatAgentCatalog = [{ id: 'a', name: '助手', greeting: '你好' }];
    h.ctx.welcome = null;
    h.ctx.paintWelcomeAgentIntro();
    assert.equal(h.description.textContent, '');
    h.ctx.welcome = h.welcome;
    h.ctx.coding = true;
    h.ctx.paintWelcomeAgentIntro();
    assert.equal(h.heading.textContent, '');
});

test('locale repaint translates the heading without changing authored content', () => {
    const h = setup();
    h.ctx.chatAgentCatalog = [{ id: 'a', name: 'BUG 管家', greeting: '由管理员编写的问候语' }];
    h.ctx.paintWelcomeAgentIntro();
    h.ctx.t = key => key === 'home_agent_greeting' ? "Hi, I'm {name}" : key;
    h.ctx.paintWelcomeAgentIntro();
    assert.equal(h.heading.textContent, "Hi, I'm BUG 管家");
    assert.equal(h.description.textContent, '由管理员编写的问候语');
});

test('clicking a question fills the original composer and sends exactly once', () => {
    const h = setup();
    h.ctx.chatAgentCatalog = [{ id: 'a', name: '助手', suggested_questions: ['带我梳理需求'] }];
    h.ctx.paintWelcomeAgentIntro();
    h.questions.children[0].dispatch('click');
    assert.deepEqual(h.sends, ['带我梳理需求']);
    assert.equal(h.chatInput.value, '带我梳理需求');
});

test('a draft or attachment disables the questions with a reason', () => {
    const h = setup();
    h.ctx.chatAgentCatalog = [{ id: 'a', name: '助手', suggested_questions: ['带我梳理需求'] }];
    h.ctx.paintWelcomeAgentIntro();
    h.chatInput.value = '已有草稿';
    h.ctx.syncAgentQuestionState();
    assert.equal(h.questions.children[0].disabled, true);
    assert.equal(h.state.textContent, COPY.home_agent_questions_draft);
    assert.ok(!h.state.classList.contains('hidden'));
    h.questions.children[0].dispatch('click');
    assert.deepEqual(h.sends, [], 'a disabled question must not send');

    // Clearing the box restores the entry.
    h.chatInput.value = '';
    h.ctx.syncAgentQuestionState();
    assert.equal(h.questions.children[0].disabled, false);
    assert.ok(h.state.classList.contains('hidden'));

    // An attachment (even one still uploading) disables it again.
    h.ctx.pendingAttachments = [{ _uploading: true }];
    h.ctx.syncAgentQuestionState();
    assert.equal(h.questions.children[0].disabled, true);
    h.ctx.pendingAttachments = [];
    h.ctx.uploadingCount = 1;
    h.ctx.syncAgentQuestionState();
    assert.equal(h.questions.children[0].disabled, true);
});

test('a live turn disables the questions', () => {
    const h = setup();
    h.ctx.chatAgentCatalog = [{ id: 'a', name: '助手', suggested_questions: ['带我梳理需求'] }];
    h.ctx.paintWelcomeAgentIntro();
    h.ctx.sendBtnMode = 'cancel';
    h.ctx.syncAgentQuestionState();
    assert.equal(h.questions.children[0].disabled, true);
});

test('the onboarding hero drops the brand eyebrow and the hero description', () => {
    const css = fs.readFileSync(path.join(__dirname, '../channel/web/static/css/appearance.css'), 'utf8');
    // The hero already reads "你好，我是<智能体>", so the brand eyebrow above it
    // and the brand tagline between the introduction and the usage line are
    // duplicated chrome. Both stay in every other empty-chat mode.
    assert.match(css, /#chat-main\.chat-home\.agent-onboarding \.home-intro \.home-eyebrow \{ display: none; \}/,
        'the brand eyebrow still sits above the Agent greeting');
    assert.match(css, /#chat-main\.chat-home\.agent-onboarding #welcome-screen \[data-brand-desc\] \{ display: none; \}/,
        'the brand tagline still splits the introduction from the usage line');
});
