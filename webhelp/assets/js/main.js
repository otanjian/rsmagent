/**
 * 容大AI 产品介绍站点交互：
 * 主题切换、移动端导航、代码标签页、复制、滚动显现。
 */
(function () {
  'use strict';

  var root = document.documentElement;
  var THEME_KEY = 'webhelp-theme';

  /* ===== 主题切换 ===== */
  // 站点默认深色（与上游一致），仅在显式选择后才跟随 data-theme
  function currentTheme() {
    return root.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  }

  var themeToggle = document.getElementById('themeToggle');
  if (themeToggle) {
    themeToggle.addEventListener('click', function () {
      var next = currentTheme() === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch (err) { /* 忽略 */ }
    });
  }

  /* ===== 移动端导航 ===== */
  var navToggle = document.getElementById('navToggle');
  var navLinks = document.getElementById('navLinks');

  function closeNav() {
    if (!navLinks) return;
    navLinks.classList.remove('is-open');
    if (navToggle) navToggle.setAttribute('aria-expanded', 'false');
  }

  if (navToggle && navLinks) {
    navToggle.addEventListener('click', function () {
      var open = navLinks.classList.toggle('is-open');
      navToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });

    navLinks.addEventListener('click', function (event) {
      if (event.target.closest('a')) closeNav();
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') closeNav();
    });

    window.addEventListener('resize', function () {
      if (window.innerWidth > 860) closeNav();
    });
  }

  /* ===== 代码标签页 ===== */
  document.querySelectorAll('.code-tabs').forEach(function (group) {
    var block = group.closest('.code-block');
    if (!block) return;

    group.addEventListener('click', function (event) {
      var tab = event.target.closest('.code-tab');
      if (!tab) return;

      var target = tab.getAttribute('data-code-tab');

      group.querySelectorAll('.code-tab').forEach(function (item) {
        item.classList.toggle('is-active', item === tab);
      });

      block.querySelectorAll('.code-content').forEach(function (panel) {
        panel.classList.toggle('is-active', panel.getAttribute('data-code-panel') === target);
      });
    });
  });

  /* ===== 复制代码 ===== */
  document.querySelectorAll('.copy-btn').forEach(function (button) {
    button.addEventListener('click', function () {
      var block = button.closest('.code-block');
      if (!block) return;

      var panel = block.querySelector('.code-content.is-active') || block.querySelector('.code-content');
      if (!panel) return;

      var text = panel.innerText.replace(/^\$\s?/gm, '').replace(/^>\s?/gm, '');
      var done = button.getAttribute('data-copy-done') || 'Copied!';
      var label = button.getAttribute('data-copy-label') || button.textContent;

      function flash(ok) {
        button.textContent = ok ? done : 'Error';
        button.classList.toggle('is-copied', ok);
        window.setTimeout(function () {
          button.textContent = label;
          button.classList.remove('is-copied');
        }, 1800);
      }

      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { flash(true); }, function () { flash(false); });
        return;
      }

      var area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      try {
        flash(document.execCommand('copy'));
      } catch (err) {
        flash(false);
      }
      document.body.removeChild(area);
    });
  });

  /* ===== 应用场景：搜索 + 岗位筛选 ===== */
  var roleFilters = document.getElementById('scenarioRoleFilters');
  var scenarioSearch = document.getElementById('scenarioSearch');
  if (roleFilters || scenarioSearch) {
    var activeRole = '';

    function refreshChipCounts(query) {
      if (!roleFilters) return;
      var cards = Array.prototype.slice.call(document.querySelectorAll('.scenario-card'));
      var matched = cards.filter(function (card) {
        var haystack = card.getAttribute('data-scenario-search') || '';
        return !query || haystack.indexOf(query) !== -1;
      });
      roleFilters.querySelectorAll('.scenario-chip').forEach(function (chip) {
        var role = chip.getAttribute('data-role') || '';
        var count = matched.filter(function (card) {
          if (!role) return true;
          var roles = (card.getAttribute('data-scenario-roles') || '').split(/\s+/).filter(Boolean);
          return roles.indexOf(role) !== -1;
        }).length;
        var badge = chip.querySelector('.scenario-chip-count');
        if (badge) badge.textContent = String(count);
      });
    }

    function applyScenarioFilters() {
      var query = ((scenarioSearch && scenarioSearch.value) || '').trim().toLowerCase();
      if (roleFilters) {
        roleFilters.querySelectorAll('.scenario-chip').forEach(function (chip) {
          var on = (chip.getAttribute('data-role') || '') === activeRole;
          chip.classList.toggle('is-active', on);
          chip.setAttribute('aria-pressed', on ? 'true' : 'false');
        });
      }

      document.querySelectorAll('.scenario-card').forEach(function (card) {
        var roles = (card.getAttribute('data-scenario-roles') || '').split(/\s+/).filter(Boolean);
        var haystack = card.getAttribute('data-scenario-search') || '';
        var roleOk = !activeRole || roles.indexOf(activeRole) !== -1;
        var searchOk = !query || haystack.indexOf(query) !== -1;
        var show = roleOk && searchOk;
        card.hidden = !show;
        // .feature-card { display:block } 会盖掉 [hidden]；内联 display 不依赖 CSS 缓存版本
        card.style.display = show ? '' : 'none';
      });
      refreshChipCounts(query);
    }

    if (roleFilters) {
      roleFilters.addEventListener('click', function (event) {
        var chip = event.target.closest('.scenario-chip');
        if (!chip || !roleFilters.contains(chip)) return;
        activeRole = chip.getAttribute('data-role') || '';
        applyScenarioFilters();
      });
    }
    if (scenarioSearch) {
      scenarioSearch.addEventListener('input', applyScenarioFilters);
    }
  }

  /* ===== 应用场景：一键体验 → 打开对应智能体对话 ===== */
  // 跳转到控制台，由控制台按 open_agent / message 打开智能体并发送初始消息。
  var scenarioData = document.querySelector('script[data-scenario-data]');
  if (scenarioData) {
    var scenarioPayload = null;
    try {
      scenarioPayload = JSON.parse(scenarioData.textContent || '{}');
    } catch (err) {
      scenarioPayload = null;
    }

    if (scenarioPayload && scenarioPayload.agents) {
      var scenarioAgents = scenarioPayload.agents || {};
      var scenarioMessage = scenarioPayload.message || '';
      var scenarioOpenPath = scenarioPayload.open_path || '/';

      document.querySelectorAll('[data-scenario-try]').forEach(function (button) {
        button.addEventListener('click', function () {
          var slug = button.getAttribute('data-scenario-try');
          var agentKey = scenarioAgents[slug];
          if (!agentKey) return;

          var target = new URL(scenarioOpenPath, window.location.origin);
          target.searchParams.set('open_agent', agentKey);
          if (scenarioMessage) {
            target.searchParams.set('message', scenarioMessage);
          }
          window.location.href = target.pathname + target.search;
        });
      });
    }
  }

  /* ===== 场景详情：复制推荐任务提示词 ===== */
  // 提示词正文已渲染在卡片里，复制时直接取渲染后的文本，避免再往属性里塞一遍。
  document.querySelectorAll('[data-sdoc-copy]').forEach(function (button) {
    button.addEventListener('click', function () {
      var card = button.closest('.sdoc-task');
      var body = card ? card.querySelector('.sdoc-task-body') : null;
      if (!body) return;

      var text = body.innerText.trim();
      if (!text) return;

      var done = button.getAttribute('data-copy-done') || 'Copied!';
      var label = button.getAttribute('data-copy-label') || button.textContent;

      function flash(ok) {
        button.textContent = ok ? done : 'Error';
        button.classList.toggle('is-copied', ok);
        window.setTimeout(function () {
          button.textContent = label;
          button.classList.remove('is-copied');
        }, 1800);
      }

      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { flash(true); }, function () { flash(false); });
        return;
      }

      var area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      try {
        flash(document.execCommand('copy'));
      } catch (err) {
        flash(false);
      }
      document.body.removeChild(area);
    });
  });

  /* ===== 滚动显现 ===== */
  var revealItems = document.querySelectorAll('.reveal');
  if (revealItems.length) {
    if ('IntersectionObserver' in window) {
      var observer = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add('is-visible');
            observer.unobserve(entry.target);
          }
        });
      }, { rootMargin: '0px 0px -8% 0px', threshold: 0.06 });

      revealItems.forEach(function (item) {
        observer.observe(item);
      });
    } else {
      revealItems.forEach(function (item) {
        item.classList.add('is-visible');
      });
    }
  }
})();
