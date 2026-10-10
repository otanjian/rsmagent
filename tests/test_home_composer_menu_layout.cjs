// Composer menus keep their trigger placement and enough room for their lists.
// NODE_PATH="$(npm root -g)" node --test tests/test_home_composer_menu_layout.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

const repo = path.resolve(__dirname, '..');
const appearanceCss = fs.readFileSync(path.join(repo, 'channel/web/static/css/appearance.css'), 'utf8');

test('chat-home CSS keeps attachment and command menu placement', () => {
    assert.match(
        appearanceCss,
        /#chat-main\.chat-home :is\(#slash-menu, #mention-menu\)\s*\{\s*top:\s*calc\(100% \+ 8px\);\s*bottom:\s*auto;/s,
        'long command lists stay below the composer on home',
    );
    assert.match(
        appearanceCss,
        /#chat-main\.chat-home #attach-menu\s*\{\s*top:\s*auto;\s*bottom:\s*calc\(100% \+ 8px\);/s,
        'attachments open above the composer on home',
    );
    assert.doesNotMatch(
        appearanceCss,
        /#chat-main\.chat-home :is\([^)]*#workspace-selector-menu[^)]*\)\s*\{\s*top:\s*calc\(100% \+ 8px\)/s,
        'workspace menu must not be forced below the composer on home',
    );
});

test('workspace and agent menus fit their lists above their triggers', async (t) => {
    let chromium;
    try {
        ({ chromium } = require('playwright'));
    } catch {
        t.skip('playwright not installed (set NODE_PATH to a global install)');
        return;
    }

    const staticRoot = path.join(repo, 'channel/web/static');
    const fixtureHtml = `<!DOCTYPE html>
<html lang="zh" data-web-palette="business">
<head>
<meta charset="utf-8">
<link rel="stylesheet" href="/assets/css/console.css">
<link rel="stylesheet" href="/assets/css/appearance.css">
<style>
  * { box-sizing: border-box; }
  .composer-menu { animation: none; }
  html, body { margin: 0; height: 100%; }
  #chat-main.chat-home {
    display: flex; flex-direction: column; height: 100vh;
    --home-header-h: 64px; --home-hero-h: 402px;
    --home-hero-offset: max(24px, calc(50dvh - (var(--home-header-h) + var(--home-hero-h)) / 2));
    padding-top: var(--home-hero-offset);
    overflow-x: hidden; overflow-y: auto;
  }
  .home-intro { order: 1; height: 120px; flex: 0 0 auto; }
  #chat-input-area { order: 2; flex: 0 0 auto; width: min(760px, calc(100% - 80px)); margin: 0 auto; }
  #composer-card { position: relative; min-height: 140px; padding: 16px; border: 1px solid #ddd; }
  .home-suggestions { order: 3; height: 400px; flex: 0 0 auto; }
  #chat-main:not(.chat-home) { display: flex; flex-direction: column; height: 100vh; justify-content: end; padding-bottom: 24px; }
  #chat-main:not(.chat-home) > :is(.home-intro, .home-suggestions) { display: none; }
  .hidden { display: none !important; }
</style>
</head>
<body>
<div id="chat-main" class="chat-main chat-home">
  <div class="home-intro">hero</div>
  <div id="chat-input-area">
    <div id="composer-card" class="composer-card">
      <div class="composer-identity-row" style="min-height:92px">input</div>
      <div class="composer-toolbar">
        <div class="composer-group">
          <button id="workspace-selector-btn" type="button" class="composer-chip">默认空间</button>
        </div>
        <div class="composer-group composer-group-end">
          <div id="composer-identity" class="composer-identity">
            <button id="composer-agent-btn" type="button" class="composer-agent-btn"><span class="agent-avatar agent-avatar-24">助</span></button>
            <div id="composer-agent-menu" class="composer-menu composer-menu-up hidden">
              <div class="composer-menu-title">切换当前智能体</div>
              ${Array.from({ length: 12 }, (_, i) => `<button type="button" class="composer-menu-item agent-row${i === 0 ? ' current' : ''}"><span class="agent-avatar agent-avatar-24">${i + 1}</span><span>智能体 ${i + 1}</span></button>`).join('')}
            </div>
          </div>
        </div>
      </div>
      <div id="workspace-selector-menu" class="workspace-selector-menu">
        <div class="ws-sel-section-title">选择工作空间</div>
        <button type="button" class="ws-sel-item active"><span>默认空间</span><i class="ws-sel-check"></i></button>
        <button type="button" class="ws-sel-item"><span>项目甲</span></button>
        <button type="button" class="ws-sel-item"><span>新建项目</span></button>
      </div>
    </div>
  </div>
  <div class="home-suggestions">cards</div>
</div>
</body>
</html>`;

    const mime = {
        '.css': 'text/css; charset=utf-8',
        '.js': 'text/javascript; charset=utf-8',
        '.svg': 'image/svg+xml',
        '.woff2': 'font/woff2',
        '.woff': 'font/woff',
        '.ttf': 'font/ttf',
    };
    const server = http.createServer((req, res) => {
        const pathname = new URL(req.url, 'http://fixture').pathname;
        if (pathname === '/' || pathname === '/fixture') {
            res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
            res.end(fixtureHtml);
            return;
        }
        if (pathname.startsWith('/assets/')) {
            const file = path.resolve(staticRoot, '.' + pathname.slice('/assets'.length));
            if (!file.startsWith(staticRoot + path.sep) || !fs.existsSync(file)) {
                res.writeHead(404); res.end('missing'); return;
            }
            res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream' });
            fs.createReadStream(file).pipe(res);
            return;
        }
        res.writeHead(404); res.end('missing');
    });

    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const origin = 'http://127.0.0.1:' + server.address().port;
    let browser;
    try {
        try {
            browser = await chromium.launch({ channel: 'chrome', headless: true });
        } catch {
            browser = await chromium.launch({ headless: true });
        }
        for (const viewport of [{ width: 1440, height: 900 }, { width: 1280, height: 720 }, { width: 375, height: 812 }]) {
            const page = await browser.newPage({ viewport });
            await page.goto(origin + '/fixture', { waitUntil: 'networkidle' });
            const readGeometry = () => page.evaluate(() => {
                const menu = document.getElementById('workspace-selector-menu');
                const card = document.getElementById('composer-card');
                const menuBox = menu.getBoundingClientRect();
                const cardBox = card.getBoundingClientRect();
                return {
                    menuTop: menuBox.top,
                    menuBottom: menuBox.bottom,
                    menuLeft: menuBox.left,
                    menuRight: menuBox.right,
                    cardTop: cardBox.top,
                    buttonTop: document.getElementById('workspace-selector-btn').getBoundingClientRect().top,
                    lastItemBottom: menu.lastElementChild.getBoundingClientRect().bottom,
                    viewportHeight: innerHeight,
                    viewportWidth: innerWidth,
                };
            });
            for (const inputHeight of [92, 220]) {
                await page.locator('.composer-identity-row').evaluate((el, height) => {
                    el.style.minHeight = height + 'px';
                }, inputHeight);
                const geometry = await readGeometry();
                const label = `${viewport.width}x${viewport.height}, input ${inputHeight}px`;
                assert.ok(geometry.menuTop >= 0, `${label}: not clipped above`);
                assert.ok(geometry.menuBottom <= geometry.viewportHeight + 1, `${label}: not clipped below`);
                assert.ok(geometry.menuLeft >= 0 && geometry.menuRight <= geometry.viewportWidth + 1,
                    `${label}: not clipped horizontally`);
                assert.ok(geometry.menuBottom > geometry.cardTop + 16,
                    `${label}: menu is beside the input, not above the entire card`);
                const gap = geometry.buttonTop - geometry.menuBottom;
                assert.ok(gap >= -1 && gap <= 24, `${label}: menu hugs its toolbar button (gap ${gap}px)`);
                assert.ok(geometry.lastItemBottom <= geometry.menuBottom,
                    `${label}: new-project action is fully visible`);
            }
            await page.locator('#workspace-selector-menu').evaluate(el => el.classList.add('hidden'));
            await page.locator('#composer-agent-menu').evaluate(el => el.classList.remove('hidden'));
            for (const home of [true, false]) {
                await page.locator('#chat-main').evaluate((el, home) => el.classList.toggle('chat-home', home), home);
                for (const inputHeight of [92, 220]) {
                    await page.locator('.composer-identity-row').evaluate((el, height) => el.style.minHeight = height + 'px', inputHeight);
                    for (const count of [3, 12]) {
                        const geometry = await page.locator('#composer-agent-menu').evaluate((menu, count) => {
                            menu.querySelectorAll('.agent-row').forEach((el, i) => el.classList.toggle('hidden', i >= count));
                            menu.scrollTop = 0;
                            const box = menu.getBoundingClientRect();
                            const button = document.getElementById('composer-agent-btn').getBoundingClientRect();
                            const last = menu.querySelectorAll('.agent-row')[count - 1];
                            const scrollable = menu.scrollHeight > menu.clientHeight;
                            menu.scrollTop = menu.scrollHeight;
                            return { top: box.top, bottom: box.bottom, left: box.left, right: box.right,
                                height: box.height, gap: button.top - box.bottom, rightGap: button.right - box.right,
                                scrollable, lastBottom: last.getBoundingClientRect().bottom };
                        }, count);
                        const label = `${viewport.width}x${viewport.height}, ${home ? 'home' : 'session'}, input ${inputHeight}px, ${count} agents`;
                        assert.ok(geometry.top >= 0 && geometry.bottom <= viewport.height + 1, `${label}: menu fits vertically (${geometry.top}..${geometry.bottom})`);
                        assert.ok(geometry.left >= 0 && geometry.right <= viewport.width + 1, `${label}: menu fits horizontally`);
                        assert.equal(geometry.scrollable, count === 12, `${label}: only long lists scroll`);
                        assert.ok(geometry.lastBottom <= geometry.bottom, `${label}: last agent reachable by scrolling`);
                        if (count === 12) assert.equal(geometry.height, 380, `${label}: long lists use the shared height cap`);
                        if (viewport.width > 640) {
                            assert.ok(Math.abs(geometry.gap - 6) < 1, `${label}: 6px above the avatar (gap ${geometry.gap}px)`);
                            assert.ok(Math.abs(geometry.rightGap) < 1, `${label}: aligned with the avatar's right edge`);
                        } else {
                            assert.ok(geometry.gap >= -1 && geometry.gap <= 24, `${label}: stays near the toolbar (gap ${geometry.gap}px)`);
                        }
                    }
                }
            }
            await page.close();
        }
    } finally {
        if (browser) await browser.close();
        await new Promise(resolve => server.close(resolve));
    }
});
