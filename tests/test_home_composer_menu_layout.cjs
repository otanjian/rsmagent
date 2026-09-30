// Homepage composer menus: toolbar chip popovers must open above the composer
// so they are not clipped by #chat-main's overflow edge (hero sits mid/low).
// NODE_PATH="$(npm root -g)" node --test tests/test_home_composer_menu_layout.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

const repo = path.resolve(__dirname, '..');
const appearanceCss = fs.readFileSync(path.join(repo, 'channel/web/static/css/appearance.css'), 'utf8');

test('chat-home CSS opens toolbar chip menus upward and command menus downward', () => {
    assert.match(
        appearanceCss,
        /#chat-main\.chat-home :is\(#slash-menu, #mention-menu, #model-selector-menu\)\s*\{\s*top:\s*calc\(100% \+ 8px\);\s*bottom:\s*auto;/s,
        'long command/model lists stay below the composer on home',
    );
    assert.match(
        appearanceCss,
        /#chat-main\.chat-home :is\(#attach-menu, #workspace-selector-menu, #composer-agent-menu\)\s*\{\s*top:\s*auto;\s*bottom:\s*calc\(100% \+ 8px\);/s,
        'toolbar chip menus open above the composer on home',
    );
    assert.doesNotMatch(
        appearanceCss,
        /#chat-main\.chat-home :is\([^)]*#workspace-selector-menu[^)]*\)\s*\{\s*top:\s*calc\(100% \+ 8px\)/s,
        'workspace menu must not be forced below the composer on home',
    );
});

test('workspace menu stays inside the viewport above the composer on chat-home', async (t) => {
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
</style>
</head>
<body>
<div id="chat-main" class="chat-main chat-home">
  <div class="home-intro">hero</div>
  <div id="chat-input-area">
    <div id="composer-card">
      <div style="height:92px">input</div>
      <div id="workspace-selector-menu" class="workspace-selector-menu">
        <div class="ws-sel-section-title">选择工作空间</div>
        <button type="button" class="ws-sel-item active"><span>默认空间</span><i class="ws-sel-check"></i></button>
        <button type="button" class="ws-sel-item"><span>项目甲</span></button>
        <button type="button" class="ws-sel-item"><span>项目乙</span></button>
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
            const geometry = await page.evaluate(() => {
                const menu = document.getElementById('workspace-selector-menu');
                const card = document.getElementById('composer-card');
                const menuBox = menu.getBoundingClientRect();
                const cardBox = card.getBoundingClientRect();
                return {
                    menuTop: menuBox.top,
                    menuBottom: menuBox.bottom,
                    cardTop: cardBox.top,
                    viewportHeight: innerHeight,
                };
            });
            assert.ok(geometry.menuTop >= 0, `${viewport.width}x${viewport.height}: not clipped above`);
            assert.ok(
                geometry.menuBottom <= geometry.viewportHeight + 1,
                `${viewport.width}x${viewport.height}: not clipped below`,
            );
            assert.ok(
                geometry.menuBottom <= geometry.cardTop + 1,
                `${viewport.width}x${viewport.height}: menu opens above composer`,
            );
            await page.close();
        }
    } finally {
        if (browser) await browser.close();
        await new Promise(resolve => server.close(resolve));
    }
});
