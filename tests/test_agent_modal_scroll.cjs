// The create-Agent modal is taller than a laptop viewport, so the dialog has to
// scroll. Layout is not exercised by any other test here, and the failure is
// silent in the markup: the fields simply end up below the fold with no way to
// reach them. These cases pin the four rules that have to hold together for the
// scroll to exist *and* leave the action button usable.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const css = fs.readFileSync(
    path.join(__dirname, '../channel/web/static/css/console.css'), 'utf8');

/** The declaration block for one rule, by exact selector. */
function rule(selector) {
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const match = new RegExp(`(?:^|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`, 'm').exec(css);
    assert.ok(match, `missing rule for ${selector}`);
    return match[1];
}

/** Does a declaration block set `property` to one of `values`? */
function declares(block, property, values) {
    const found = new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*([^;]+)`, 'm').exec(block);
    return !!found && values.includes(found[1].trim());
}

test('the Agent modal scrolls its body instead of overflowing the viewport', () => {
    // No scroll region means the overflow is painted past the card's bottom edge
    // and is unreachable: the overlay is fixed, so neither the page nor the card
    // can be scrolled to it.
    assert.ok(declares(rule('.agent-modal-body'), 'overflow-y', ['auto', 'scroll']),
        'the body must be the scroll region');
    // ...and it only scrolls because the card clamps its own height. Without the
    // clamp the body never overflows and every field stretches the dialog.
    assert.ok(declares(rule('.agent-modal-card'), 'max-height', ['calc(100vh - 48px)']),
        'the card must stay inside the viewport');
});

test('the action row and the title stay put while the fields scroll', () => {
    // The footer holds 创建智能体. Shrinking it to fit a long form would squeeze
    // the button (or clip it) exactly when the form is long enough to need the
    // scroll, which is the case this guards. The same goes for the drawers' own
    // header row (the close button lives there).
    const shared = rule('.agent-modal-title,\n.agent-modal-head,\n.agent-modal-foot');
    assert.ok(declares(shared, 'flex-shrink', ['0']),
        'title, head and footer must not shrink');
    // The footer is a sibling of the scroll region, not inside it: nesting it
    // would scroll the action out of reach again.
    const body = rule('.agent-modal-body');
    assert.ok(!/agent-modal-foot/.test(body), 'the footer is not part of the body');
});

test('the scrolling body does not trap an opened dropdown menu', () => {
    // The menu is absolutely positioned inside the body, so a scroll region is
    // where a dropdown can get clipped. Two rules keep that from happening: the
    // menu scrolls on its own, and the open handler flips it upward when the
    // viewport bottom would cut it.
    assert.ok(declares(rule('.cfg-dropdown-menu'), 'max-height', ['240px']),
        'the menu keeps its own height cap');
    assert.ok(declares(rule('.cfg-dropdown-menu'), 'overflow-y', ['auto']),
        'the menu scrolls its own options');
    const console_js = fs.readFileSync(
        path.join(__dirname, '../channel/web/static/js/console.js'), 'utf8');
    assert.match(console_js, /classList\.toggle\('drop-up'/,
        'a menu near the viewport bottom is flipped upward');
});
