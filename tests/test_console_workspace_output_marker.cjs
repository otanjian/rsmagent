// A personal coding turn carries a platform note naming the folder its
// generated files belong in (see `output_line` in agent/coding/files.py). That
// line is appended to the prompt, so history persists it -- and the reader never
// typed it. It must be recognised and dropped when the message is rendered from
// history, exactly like the attachment markers the same mechanism already
// recovers chips from, while an unknown bracketed line stays the reader's own
// text.
//
// Run against the shipped console (the artifact chat.html loads), sliced to the
// two functions under test so no DOM wire-up runs.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(
    path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');
const start = source.indexOf('// Attachment markers the backend appends');
const end = source.indexOf('function renderToolCallsHtml(');
assert.ok(start >= 0 && end > start, 'Missing the attachment-marker section in console.js');
const section = source.slice(start, end);

function element() {
    const el = {
        className: '', innerHTML: '', dataset: {},
        querySelector: () => element(),
        querySelectorAll: () => [],
    };
    return el;
}

function makeCtx() {
    const ctx = {
        document: { createElement: element },
        escapeHtml: value => String(value ?? ''),
        _toWebUrl: value => `/api/file?path=${value}`,
        t: key => key,
        formatTime: () => '00:00',
        renderMarkdown: text => `<p>${text}</p>`,
        highlightMentions() {},
    };
    vm.createContext(ctx);
    vm.runInContext(section, ctx);
    return ctx;
}

/** The rendered bubble's own text, then the text it kept for editing. */
function render(ctx, content) {
    const el = ctx.createUserMessageEl(content, 1, null);
    return { bubble: el.innerHTML, raw: el.dataset.rawContent, html: el.innerHTML };
}

test('the output-location note never reaches the reader own words', () => {
    const ctx = makeCtx();

    const { raw, html } = render(ctx, '生成一份报价单\n[个人目录输出位置: outputs/sess-1/req-9]');

    assert.equal(raw, '生成一份报价单');
    assert.ok(!html.includes('outputs/sess-1/req-9'), html);
});

test('the note is dropped whether it is simplified or traditional', () => {
    const ctx = makeCtx();

    assert.equal(render(ctx, 'r\n[個人目錄輸出位置: outputs/sess-1]').raw, 'r');
    assert.equal(render(ctx, 'r\n[Personal directory output location: outputs/x]').raw, 'r');
});

test('a note above the attachment markers still leaves the chips', () => {
    const ctx = makeCtx();

    // The order the backend writes them in: the turn's note, then the files.
    const { raw, html } = render(ctx,
        '看看这个\n[个人目录输出位置: outputs/sess-1/req-9]\n[工作空间文件: uploads/sess-1/a.txt]');

    assert.equal(raw, '看看这个');
    assert.match(html, /a\.txt/);
    assert.ok(!html.includes('outputs/sess-1/req-9'), html);
});

test('a message with no marker at all is rendered untouched', () => {
    const ctx = makeCtx();

    const { raw, html } = render(ctx, 'just a question');

    assert.equal(raw, 'just a question');
    assert.match(html, /just a question/);
});

test('an unrecognised bracketed line stays the reader own text', () => {
    const ctx = makeCtx();

    const { raw } = render(ctx, 'note\n[提醒: 明天再看]');

    assert.equal(raw, 'note\n[提醒: 明天再看]');
});

test('a note-shaped line the reader typed mid-message is not dropped', () => {
    const ctx = makeCtx();

    // Only *trailing* platform lines are stripped, so a quoted example inside
    // the message body survives.
    const { raw } = render(ctx,
        '[个人目录输出位置: outputs/x]\n这部分是我自己写的');

    assert.equal(raw, '[个人目录输出位置: outputs/x]\n这部分是我自己写的');
});
