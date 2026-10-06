/* scenes/index.js — 场景中心（场景应用）主前端逻辑。
 *
 * 独立于 console.js（仅由 ``chat.html`` 以 defer 引入），复用 console.js 暴露的
 * 全局函数（t / navigateTo / switchSession / generateSessionId / addBotMessage）
 * 与全局词法绑定（I18N / currentLang / sessionId）。本模块负责：
 *
 *   loadScenesView()、renderScenesView()  场景中心：分类页签 + 场景卡片 + 空态
 *   ensureScenePickerData()、showScenePicker()   ``/场景``（或 ``/scenes``）场景选择器
 *   activateScene(scene)、openSceneById(sceneId)  场景激活与工作台分发
 *
 * 工作台渲染器（scenes/workbenches/*.js）通过 ScenesRegistry（registry.js）
 * 注册；未注册专用渲染器的场景走通用激活流程（进入对话上下文）。
 * 其余未开放目标（backup/open_api 等）仍沿用 console.js 的占位分支。
 */
(function () {
    'use strict';

    // ---- 确保 registry 与工作台渲染器已加载（``chat.html`` 仅引入本文件） --
    const SCENES_BASE = 'assets/js/scenes';
    let _registryReady = Promise.resolve();
    function ensureRegistry() {
        if (window.ScenesRegistry) return Promise.resolve(window.ScenesRegistry);
        _registryReady = _registryReady.then(function () {
            return new Promise(function (resolve) {
                const s = document.createElement('script');
                s.src = SCENES_BASE + '/registry.js';
                s.onload = function () { resolve(window.ScenesRegistry); };
                s.onerror = function () { resolve(null); };
                document.head.appendChild(s);
            });
        });
        return _registryReady;
    }
    let _workbenchesReady = null;
    const WORKBENCH_SCRIPT_TIMEOUT_MS = 45000;
    function ensureWorkbenches() {
        // Already loaded by a prior visit (or a recovered page): skip re-fetch.
        if (window.SceneOriginal && window.SapWorkbench) return Promise.resolve();
        if (_workbenchesReady) return _workbenchesReady;
        if (!document.querySelector('link[href="/scene-assets/_shared/frontend/host.css"]')) {
            const style = document.createElement('link');
            style.rel = 'stylesheet';
            style.href = '/scene-assets/_shared/frontend/host.css';
            document.head.appendChild(style);
        }
        function script(url) {
            return new Promise(function (resolve, reject) {
                const s = document.createElement('script');
                s.src = url;
                let settled = false;
                const timer = setTimeout(function () {
                    if (settled) return;
                    settled = true;
                    s.remove();
                    reject(new Error('场景脚本加载超时'));
                }, WORKBENCH_SCRIPT_TIMEOUT_MS);
                s.onload = function () {
                    if (settled) return;
                    settled = true;
                    clearTimeout(timer);
                    resolve();
                };
                s.onerror = function () {
                    if (settled) return;
                    settled = true;
                    clearTimeout(timer);
                    s.remove();
                    reject(new Error('场景脚本加载失败'));
                };
                document.head.appendChild(s);
            });
        }
        // Parallel fetch: sequential xlsx→runtime made first open feel dead under load.
        _workbenchesReady = Promise.all([
            script('/scene-assets/_shared/frontend/xlsx.full.min.js'),
            script('/scene-assets/runtime.js'),
        ]).then(function () { /* ready */ })
            .catch(function (err) { _workbenchesReady = null; throw err; });
        return _workbenchesReady;
    }

    // ---- 从 console.js 复用（可缺失，逐项降级） --------------------------
    function liveLang() {
        if (typeof currentLang === 'string' && currentLang) return currentLang;
        if (window.__cowLang__) return window.__cowLang__;
        return 'zh';
    }
    function t(key) {
        const lang = liveLang();
        const i18n = window.I18N || {};
        if (i18n[lang] && i18n[lang][key] != null) return i18n[lang][key];
        if (i18n.en && i18n.en[key] != null) return i18n.en[key];
        return key;
    }
    function escapeHtml(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    function gotoChat() {
        if (typeof navigateTo === 'function') navigateTo('chat');
    }
    function newSessionId() {
        if (typeof generateSessionId === 'function') return generateSessionId();
        return 'session_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
    }

    // ---- 场景缓存状态 ----------------------------------------------------
    let _catalogCache = null;        // { categories, scenes }
    let _catalogLoading = null;      // in-flight promise
    let _activeCategory = null;      // 当前分类 id
    let _picker = null;              // 场景选择器 DOM/状态

    // ---- 请求后端 `/api/scenes` -------------------------------------------
    function fetchScenes() {
        return fetch('/api/scenes', { credentials: 'same-origin', cache: 'no-store' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data || data.status !== 'success') throw new Error(data && data.message || 'bad scenes response');
                return data;
            });
    }

    function ensureSceneData(force) {
        if (_catalogCache && !force) return Promise.resolve(_catalogCache);
        if (_catalogLoading) return _catalogLoading;
        _catalogLoading = fetchScenes().then(function (data) {
            _catalogCache = { categories: data.categories || [], scenes: data.scenes || [] };
            if (typeof data.categories === 'function') _catalogCache.categories = [];
            return _catalogCache;
        }).finally(function () {
            _catalogLoading = null;
        });
        return _catalogLoading;
    }

    function categoryById(id) {
        const cat = (_catalogCache.categories || []).find(function (c) { return c.id === id; });
        return cat ? { id: cat.id, name: cat.name, icon: cat.icon, color: cat.color } : null;
    }

    function scenesForCategory(categoryId) {
        return (_catalogCache.scenes || []).filter(function (s) {
            return categoryId === 'all' || s.category === categoryId;
        });
    }

    function findScene(sceneId) {
        const scenes = _catalogCache.scenes || [];
        for (let i = 0; i < scenes.length; i++) {
            const s = scenes[i];
            if (s.id === sceneId) return s;
            const subs = s.sub_scenes || [];
            for (let j = 0; j < subs.length; j++) {
                if (subs[j].id === sceneId) {
                    // 合并父场景元数据，与 scenes/service.find_scene 行为一致。
                    return {
                        id: subs[j].id,
                        name: subs[j].name,
                        parent_id: s.id,
                        parent_name: s.name,
                        category: subs[j].category || s.category,
                        icon: subs[j].icon || s.icon,
                        color: subs[j].color || s.color,
                        description: subs[j].description || s.description,
                        system_prompt: subs[j].system_prompt || s.system_prompt,
                        skill_name: subs[j].skill_name || s.skill_name,
                        has_workbench: subs[j].has_workbench != null ? subs[j].has_workbench : s.has_workbench,
                        workbench_title: subs[j].workbench_title || s.workbench_title,
                        import_config: subs[j].import_config || s.import_config,
                        erp_config: subs[j].erp_config || s.erp_config,
                        required_permission: subs[j].required_permission || s.required_permission,
                        indicators: subs[j].indicators || s.indicators,
                        required_fields: subs[j].required_fields || s.required_fields,
                    };
                }
            }
        }
        return null;
    }

    // ---- 场景中心渲染 ----------------------------------------------------
    function getEl(id) { return document.getElementById(id); }

    // 控制空态盒整体显隐。``visible`` 为真时显示空态盒；``showGuide`` 决定
    // 是否显示「开始对话」引导；``loading`` 为真时标题显示「加载中」，否则
    // 显示「无可用场景」。空态盒仅当「当前分类无可展示场景或加载失败」出现。
    function setEmptyState(visible, showGuide, loading) {
        const empty = getEl('scenes-empty');
        if (!empty) return;
        if (visible) {
            empty.classList.remove('hidden');
            const title = getEl('scenes-empty-title');
            if (title) {
                const key = loading ? 'scenes_loading' :
                    (!_catalogCache || !_catalogCache.scenes.length ? 'scenes_empty' : 'scenes_no_category');
                title.textContent = t(key);
                title.setAttribute('data-i18n', key);
            }
            const guide = getEl('scenes-empty-guide');
            if (guide) guide.classList.toggle('hidden', !showGuide);
        } else {
            empty.classList.add('hidden');
        }
    }

    function renderTabs() {
        const tabsEl = getEl('scenes-tabs');
        if (!tabsEl) return;
        if (!_activeCategory) _activeCategory = 'all';
        tabsEl.replaceChildren();
        tabsEl.classList.remove('hidden');

        if (!_catalogCache.categories.length) {
            tabsEl.classList.add('hidden');
            return;
        }

        const allTab = document.createElement('button');
        allTab.textContent = t('scenes_all');
        allTab.className = 'scene-tab px-3 py-1.5 rounded-full text-sm font-medium transition-colors ' +
            (_activeCategory === 'all'
                ? 'bg-primary-500 text-white'
                : 'bg-slate-100 dark:bg-white/10 text-slate-600 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-white/15');
        allTab.addEventListener('click', function () { selectCategory('all'); });
        tabsEl.appendChild(allTab);

        (_catalogCache.categories || []).forEach(function (cat) {
            const btn = document.createElement('button');
            btn.textContent = cat.name + ' ' + scenesForCategory(cat.id).length;
            btn.className = 'scene-tab px-3 py-1.5 rounded-full text-sm font-medium transition-colors ' +
                (_activeCategory === cat.id
                    ? 'bg-primary-500 text-white'
                    : 'bg-slate-100 dark:bg-white/10 text-slate-600 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-white/15');
            btn.addEventListener('click', function () { selectCategory(cat.id); });
            tabsEl.appendChild(btn);
        });
    }

    function renderCards() {
        const grid = getEl('scenes-grid');
        if (!grid) return;
        grid.replaceChildren();
        grid.classList.remove('hidden');

        const scenes = scenesForCategory(_activeCategory);
        if (!scenes.length) {
            grid.classList.add('hidden');
            setEmptyState(true, false, false);
            return;
        }
        // 有场景：显示卡片网格，隐藏空态盒。
        setEmptyState(false);

        scenes.forEach(function (scene) {
            const cat = categoryById(scene.category) || {};
            const accent = scene.color || cat.color || '#64748b';
            // A scene that offers its own configuration gets a real 配置 control
            // in place of the category tag. That control has to be a nested
            // button, and a <button> may not contain another one, so such a card
            // is a div carrying button semantics instead.
            const configurable = scene.card_action === 'configure';
            const card = document.createElement(configurable ? 'div' : 'button');
            if (!configurable) card.type = 'button';
            card.className = 'scene-card text-left flex flex-col rounded-2xl border border-slate-200 dark:border-white/10 ' +
                'bg-white dark:bg-[#1c1c1c] p-5 hover:shadow-lg hover:border-slate-300 dark:hover:border-white/20 ' +
                'transition-all duration-200 cursor-pointer';
            if (configurable) { card.setAttribute('role', 'button'); card.tabIndex = 0; }
            card.dataset.sceneId = scene.id;
            card.innerHTML =
                '<div class="flex items-start justify-between mb-3">' +
                    '<div class="w-11 h-11 rounded-xl flex items-center justify-center" style="background:' +
                        accent + '1a;">' +
                        '<i class="fas ' + (scene.icon || cat.icon || 'fa-cube') + ' text-lg" style="color:' +
                        accent + ';"></i></div>' +
                    (scene.has_workbench
                        ? '<span class="scene-badge text-[11px] px-2 py-0.5 rounded-full bg-indigo-50 dark:bg-indigo-900/30 ' +
                          'text-indigo-500 dark:text-indigo-300">' + t('scenes_workbench') + '</span>' : '') +
                '</div>' +
                '<div class="font-semibold text-slate-800 dark:text-slate-100 mb-1">' + escapeHtml(scene.name) + '</div>' +
                '<p class="text-sm text-slate-500 dark:text-slate-400 ' + (scene.id === 'sap_workbench' ? '' : 'line-clamp-2 ') + 'flex-1">' +
                    escapeHtml(scene.description || '') + '</p>' +
                '<div class="mt-3 flex items-center justify-between">' +
                    (configurable
                        ? '<button type="button" class="scene-configure text-xs px-3 py-1 rounded-full border font-medium ' +
                          'border-slate-300 dark:border-white/20 text-slate-600 dark:text-slate-300 ' +
                          'hover:border-primary-400 hover:text-primary-500 transition-colors">' +
                          escapeHtml(t('scenes_configure')) + '</button>'
                        : '<span class="text-[11px] px-2 py-0.5 rounded-full" style="color:' +
                          (cat.color || '#64748b') + ';background:' + (cat.color || '#64748b') + '1a;">' +
                          escapeHtml(cat.name || '') + '</span>') +
                    '<span class="text-xs text-slate-400 dark:text-slate-500 flex items-center gap-1">' +
                        '<i class="fas fa-arrow-right text-[10px]"></i>' + t('scenes_go_chat') + '</span>' +
                '</div>';
            // One delegated listener: the 配置 control lives in markup set via
            // innerHTML, so the routing is done on the event target rather than
            // on a node reference that only a real parser would produce.
            card.addEventListener('click', function (event) {
                if (configurable && event.target && typeof event.target.closest === 'function'
                    && event.target.closest('.scene-configure')) {
                    onSceneConfigureClick(scene);
                    return;
                }
                onSceneCardClick(scene);
            });
            if (configurable) {
                card.addEventListener('keydown', function (event) {
                    // The nested control handles its own keys; only the card
                    // itself activates here.
                    if (event.target !== card) return;
                    if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        onSceneCardClick(scene);
                    }
                });
            }
            grid.appendChild(card);
        });
    }

    function selectCategory(categoryId) {
        _activeCategory = categoryId;
        renderTabs();
        renderCards();
    }

    function loadScenesView() {
        const empty = getEl('scenes-empty');
        const tabs = getEl('scenes-tabs');
        const grid = getEl('scenes-grid');
        if (tabs) { tabs.replaceChildren(); tabs.classList.add('hidden'); }
        if (grid) { grid.replaceChildren(); grid.classList.add('hidden'); }
        // 初始先显示「加载中」空态；数据到达后由 renderCards 决定空态/网格显隐。
        if (empty) { empty.classList.remove('hidden'); const l = getEl('scenes-empty-guide'); if (l) l.classList.add('hidden'); }

        return ensureSceneData(true).then(function () {
            if (!_catalogCache.scenes.length) {
                // 整库无场景：显示空态与引导。
                setEmptyState(true, true, false);
                return;
            }
            renderTabs();
            renderCards();
            // Prefetch workbench runtime while the user browses cards so the
            // first click does not sit silently on a multi-megabyte download.
            ensureWorkbenches().catch(function () { /* retry on click */ });
        }).catch(function () {
            // 加载失败：显示空态与引导。
            setEmptyState(true, true, false);
        });
    }

    // ---- 场景卡片点击 ----------------------------------------------------
    function onSceneCardClick(scene) {
        openSceneById(scene.id);
    }

    // 卡片上的「配置」入口：与普通打开共用同一条准备路径，只把最后一步分发
    // 换成配置面，避免出现两个入口行为不一致。
    function onSceneConfigureClick(scene) {
        return openSceneById(scene.id, 'configure');
    }

    // ---- 激活场景 / 工作台分发 -------------------------------------------
    // ``action`` 为 ``'configure'`` 时走卡片的「配置」入口：准备步骤与普通打开
    // 完全一致（否则首次点击时运行时还没加载，会误退化成普通打开），只有最后
    // 一步分发不同——声明了配置面的场景打开配置，其余场景照常打开。
    function openSceneById(sceneId, action) {
        // Immediate feedback: large workbench scripts can take seconds; without
        // this the card click looks dead.
        showNotice(t('scenes_opening'));
        // 允许在进入场景中心前直接被调用（工作台/选择器/直链）：先确保目录数据。
        return ensureSceneData(false).then(function () {
            return ensureRegistry();
        }).then(function () {
            const scene = findScene(sceneId);
            if (!scene) return;
            const registry = window.ScenesRegistry;
            const wbType = registry && registry.resolveWorkbenchType
                ? registry.resolveWorkbenchType(scene) : 'base';
            return ensureWorkbenches().then(function () {
                const original = window.SceneOriginal;
                if (original && action === 'configure' && typeof original.configure === 'function') {
                    return original.configure(scene);
                }
                if (original) return original.open(scene);
                // A missing SAP runtime must never activate a normal Agent.
                if (scene.id === 'sap_workbench') throw new Error(t('scenes_activate_failed'));
                if (scene.has_workbench && registry && registry.hasRenderer && registry.hasRenderer(wbType)) {
                    return registry.render(wbType, scene);
                }
                activateScene(scene);
            });
        }).catch(function (error) { showNotice(error.message || t('scenes_activate_failed')); });
    }

    function activateScene(scene) {
        if (!scene || !scene.id) return;
        const sid = newSessionId();
        fetch('/api/scenes/activate', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scene_id: scene.id, session_id: sid }),
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data || data.status !== 'success') {
                    showNotice(t('scenes_activate_failed'));
                    return;
                }
                // 创建新会话并注入场景 greeting。
                if (typeof switchSession === 'function') switchSession(sid);
                else if (typeof sessionId !== 'undefined') { /* fallback below */ }
                if (typeof addBotMessage === 'function') {
                    const greeting = t('scenes_greeting').replace('{name}', scene.name || scene.id);
                    addBotMessage(greeting);
                }
                gotoChat();
            })
            .catch(function () {
                showNotice(t('scenes_activate_failed'));
            });
    }

    // 轻量提示（下拉即走）。自建 div 避免依赖 console.js 的 toast。
    function showNotice(msg) {
        let el = document.getElementById('scenes-notice');
        if (!el) {
            el = document.createElement('div');
            el.id = 'scenes-notice';
            el.className = 'fixed top-4 left-1/2 -translate-x-1/2 z-[1000] px-4 py-2 rounded-lg ' +
                'bg-white dark:bg-[#2a2a2a] text-slate-700 dark:text-slate-200 ' +
                'border border-slate-200 dark:border-white/15 shadow-lg text-sm';
            document.body.appendChild(el);
        }
        el.textContent = msg;
        el.style.opacity = '1';
        clearTimeout(el._hide);
        el._hide = setTimeout(function () { el.style.opacity = '0'; }, 2600);
    }
    window.showScenesNotice = showNotice;

    // ---- `/场景` 场景选择器 ----------------------------------------------
    function ensureScenePickerData() {
        return ensureSceneData(false);
    }

    function buildPickerModal() {
        const modal = document.createElement('div');
        modal.id = 'scenes-picker';
        modal.className = 'fixed inset-0 z-[999] hidden items-center justify-center bg-black/40 p-4';
        modal.innerHTML =
            '<div class="scenes-picker-panel w-full max-w-lg rounded-2xl bg-white dark:bg-[#1c1c1c] ' +
                'border border-slate-200 dark:border-white/10 shadow-2xl overflow-hidden">' +
                '<div class="flex items-center justify-between px-5 py-3 border-b border-slate-200 dark:border-white/10">' +
                    '<span class="font-semibold text-slate-800 dark:text-slate-100">' + t('scenes_picker_title') + '</span>' +
                    '<button type="button" class="scenes-picker-close text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 text-xl leading-none">&times;</button>' +
                '</div>' +
                '<div class="px-5 py-3"><input type="text" class="scenes-picker-input w-full px-3 py-2 rounded-lg text-sm ' +
                    'bg-slate-100 dark:bg-white/10 text-slate-800 dark:text-slate-100 border border-slate-200 dark:border-white/10 ' +
                    'focus:outline-none focus:ring-2 focus:ring-primary-500/40" placeholder="' + t('scenes_picker_placeholder') + '"></div>' +
                '<div class="scenes-picker-list max-h-72 overflow-y-auto px-2 pb-3"></div>' +
            '</div>';
        document.body.appendChild(modal);
        return modal;
    }

    function showScenePicker() {
        // 允许在进入场景中心前直接通过 `/场景` 触发：先确保目录数据已加载。
        ensureSceneData(false).then(function () {
            _openPickerModal();
        }).catch(function () {
            showNotice(t('scenes_activate_failed'));
        });
    }

    function _openPickerModal() {
        const modal = getEl('scenes-picker') || buildPickerModal();
        const input = modal.querySelector('.scenes-picker-input');
        const list = modal.querySelector('.scenes-picker-list');
        const closeBtn = modal.querySelector('.scenes-picker-close');

        modal.classList.remove('hidden');
        modal.classList.add('flex');

        function renderFiltered(query) {
            const q = (query || '').trim().toLowerCase();
            const scenes = (_catalogCache.scenes || []).filter(function (s) {
                if (!q) return true;
                return (s.name || '').toLowerCase().indexOf(q) >= 0
                    || (s.description || '').toLowerCase().indexOf(q) >= 0;
            });
            list.replaceChildren();
            if (!scenes.length) {
                list.innerHTML = '<div class="text-center text-sm text-slate-400 py-6">' + t('scenes_picker_empty') + '</div>';
                return;
            }
            scenes.forEach(function (scene) {
                const item = document.createElement('button');
                item.type = 'button';
                item.className = 'scene-picker-item w-full text-left flex items-center gap-3 px-3 py-2 rounded-lg ' +
                    'hover:bg-slate-100 dark:hover:bg-white/10 transition-colors';
                item.innerHTML =
                    '<i class="fas ' + (scene.icon || 'fa-cube') + ' w-6 text-center" style="color:' + (scene.color || '#0ea5e9') + ';"></i>' +
                    '<div class="min-w-0"><div class="text-sm font-medium text-slate-800 dark:text-slate-100 truncate">' +
                        escapeHtml(scene.name) + '</div>' +
                    (scene.description ? '<div class="text-xs text-slate-500 dark:text-slate-400 truncate">' + escapeHtml(scene.description) + '</div>' : '') +
                    '</div>';
                item.addEventListener('click', function () {
                    closePicker();
                    openSceneById(scene.id);
                });
                list.appendChild(item);
            });
        }

        function closePicker() {
            modal.classList.add('hidden');
            modal.classList.remove('flex');
            if (closeBtn) closeBtn.onclick = null;
            input.onkeydown = null;
            document.removeEventListener('keydown', onKey);
        }
        function onKey(e) {
            if (e.key === 'Escape') { e.preventDefault(); closePicker(); }
            if (e.key === 'Enter') {
                e.preventDefault();
                const first = list.querySelector('.scene-picker-item');
                if (first) first.click();
            }
        }

        if (closeBtn) closeBtn.onclick = closePicker;
        input.value = '';
        input.onkeydown = onKey;
        input.oninput = function () { renderFiltered(input.value); };
        document.addEventListener('keydown', onKey);
        renderFiltered('');
        setTimeout(function () { input.focus(); }, 10);
        modal.querySelector('.scenes-picker-panel').addEventListener('click', function (e) { e.stopPropagation(); });
        modal.addEventListener('click', function () { closePicker(); });
    }

    // ---- 对外暴露 --------------------------------------------------------
    window.loadScenesView = loadScenesView;
    window.selectCategory = selectCategory;
    window.ensureScenePickerData = ensureScenePickerData;
    window.showScenePicker = showScenePicker;
    window.activateScene = activateScene;
    window.openSceneById = openSceneById;
    window.continueSceneSapAnalysis = function (question) {
        return ensureWorkbenches().then(function () {
            return window.SceneOriginal.sendSapFollowUpAnalysis(question);
        }).catch(function (error) { showNotice(error.message); });
    };
})();
