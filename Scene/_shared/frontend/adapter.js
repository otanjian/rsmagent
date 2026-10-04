// rsmagent host seam. Original business scripts follow this adapter unchanged.
let activeSceneContext = null;
let currentUser = null;
let mounted = null;
let _totalRows = 0; // The original script creates this variable implicitly.
let _hostScene = null;
const hostSendMessage = window.sendMessage;
const hostNewChat = window.newChat;
const hostFetch = window.fetch.bind(window);

function t(key) {
    const lang = typeof currentLang === 'string' && currentLang.startsWith('en') ? 'en' : 'zh';
    return (I18N[lang] && I18N[lang][key]) || I18N.zh[key] || key;
}
function newChat() {
    hostNewChat();
    window.navigateTo('chat');
}
function sendMessage(prompt, visuals) {
    if (typeof prompt === 'string') {
        document.getElementById('chat-input').value = prompt;
        window.navigateTo('chat');
    }
    const result = hostSendMessage();
    if (visuals) {
        const html = _buildSapVisualsHtml(visuals);
        if (html) {
            const panel = document.createElement('div');
            panel.className = 'px-6 py-3';
            panel.innerHTML = html;
            messagesDiv.appendChild(panel);
        }
    }
    return result;
}
function fetch(input, options) {
    if (input === '/assets/scheduling_template.xlsx' || input === '/assets/scheduling_test_data.xlsx') {
        input = '/scene-assets/production_plan/assets/' + input.split('/').pop();
    }
    if (input === '/api/workbench/upload' && options && options.body && _hostScene) {
        const body = JSON.parse(options.body);
        body.scene_id = _hostScene.id;
        options = Object.assign({}, options, {body: JSON.stringify(body)});
    }
    return hostFetch(input, options);
}

function formatFileSize(bytes) {
    if (!bytes) return '0 B';
    const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), 3);
    return (bytes / Math.pow(1024, i)).toFixed(1) + ' ' + ['B', 'KB', 'MB', 'GB'][i];
}
function parseCSV(text) {
    const book = XLSX.read(text, {type: 'string'});
    return XLSX.utils.sheet_to_json(book.Sheets[book.SheetNames[0]], {defval: ''});
}
function _toWebUrl(url) {
    if (/^\/(scene-assets|preview|assets|api)\//.test(url)) return url;
    if (/^file:\/\//.test(url)) return '/api/file?path=' + encodeURIComponent(url.replace(/^file:\/\//, ''));
    if (/^(\/|[A-Za-z]:[\\/])/.test(url)) return '/api/file?path=' + encodeURIComponent(url);
    return url;
}
function switchView(view) {
    if (view !== 'config') window.navigateTo(view);
}
function switchConfigTab(tab) {
    if (tab !== 'erp') return;
    // Connection management lives on the console page (task 6.5). The scene used
    // to open a second writable form here; it now navigates instead, so there is
    // exactly one place a connection -- and its credential -- can be changed.
    openErpConnectionConsole();
}
function showStatus(id, key, error) {
    const el = document.getElementById(id);
    if (el) { el.textContent = t(key); el.classList.remove('hidden'); }
    else showToast(t(key), error ? 'error' : 'success');
}

window.SceneOriginal = {
    resetChat() {
        activeSceneContext = null;
        document.getElementById('scene-subpanel')?.classList.add('hidden');
    },
    // The scene card's own 配置 entry point (``card_action: "configure"``).
    // Only scenes that actually expose a configuration surface open it; every
    // other scene keeps the plain open path, so a card never advertises a
    // configuration it does not have.
    async configure(scene) {
        if (scene.id === 'sap_workbench') return window.SapWorkbench.open({view: 'settings'});
        return window.SceneOriginal.open(scene);
    },
    async open(scene) {
        if (scene.id === 'sap_workbench') return window.SapWorkbench.open({view: 'new-session'});
        _hostScene = scene;
        if (!mounted) {
            mounted = fetch('/scene-assets/workbenches.html').then(r => {
                if (!r.ok) throw new Error('场景工作台页面加载失败');
                return r.text();
            }).then(html => {
                const root = document.createElement('div');
                root.id = 'scene-original-root';
                root.innerHTML = html;
                document.body.appendChild(root);
                const panel = document.getElementById('scene-subpanel');
                const chat = document.getElementById('view-chat');
                if (panel && chat) chat.prepend(panel);
            }).catch(error => { mounted = null; throw error; });
        }
        await mounted;
        // Host capability projection is fetched for each open, never retained
        // across a tenant/account switch.
        const capability = await fetch('/api/scenes/capabilities').then(r => {
            if (!r.ok) throw new Error('没有场景访问权限');
            return r.json();
        });
        currentUser = capability.user;
        erpConnectionsData = [];
        erpConnectionsLoaded = false;
        if (scene.id === 'quality_traceability') return openQualityTraceScene(scene);
        if (scene.id === 'sap_data_analysis') return openSapDataAnalysisWorkbench(scene);
        if (scene.id === 'production_scheduling_airbag') return openAirbagSchedulingWorkbench(scene);
        const specialized = {
            finance_voucher: openVoucherWorkbench,
            finance_expert: openTaxationWorkbench,
            finance_chenyiwei: openCasWorkbench,
            production_plan: openSchedulingWorkbench,
            finance_report_audit: openFinancialAuditWorkbench,
        };
        if (scene.has_workbench && scene.sub_scenes && scene.sub_scenes.length) {
            return (specialized[scene.id] || openWorkbench)(scene);
        }
        return activateScene(scene);
    },
};
