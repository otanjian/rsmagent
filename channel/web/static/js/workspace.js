/* =====================================================================
 * Workspace panel: file preview + file manager + @ file references.
 * Loaded after console.js and reuses its globals (t, escapeHtml,
 * renderMarkdown, applyHighlighting, pendingAttachments, ...).
 * ===================================================================== */

const WS_WIDTH_KEY = 'cow_workspace_width';
const WS_DEFAULT_WIDTH = 420;
const WS_MIN_WIDTH = 280;

// Panel state
let wsPanelOpen = false;
let wsActiveTab = 'preview';
let wsCurrentFile = null;
// Preview editor state. `wsEditBaseline` is the text area's own value as loaded,
// so comparing against it tells whether anything actually changed;
// `wsEditBaseMtime` is the timestamp the server checks to detect that the agent
// rewrote the file mid-edit.
let wsEditing = false;
let wsEditBaseline = '';
let wsEditBaseMtime = null;
let wsSaving = false;
// Set once the user closes the panel by hand: from then on we stop
// auto-opening artifacts for the rest of the page session.
let wsAutoOpenSuppressed = false;
// Artifacts produced by the turn currently streaming.
let wsTurnArtifacts = [];

// File manager state
let wsCurrentDir = '';
let wsCurrentRoot = '';   // absolute path of the workspace/project root
// Which source the panel is showing: 'backend' (the server's own directory) or
// 'desktop' (a project on this machine, reached through the local source).
let wsCurrentSource = 'backend';
let wsSearchMode = false;
let wsSearchTimer = null;
// Agent whose directory the panel browses. Normally the conversation's own
// Agent (the `agent` query param below); set to the caller's private Agent only
// after that one refused the request, see wsFallBackToOwnAgent.
let wsAgentOverride = '';
// The caller's own private Agent id, memoized once known. `undefined` means
// "not asked yet" so an empty answer is retried rather than cached.
let wsOwnAgentId;
// The caller's own end-user id (``usr_...``), memoized once known. `undefined`
// means "not asked yet"; '' is a real answer — this deployment has no end-user
// identity (a legacy single-user install) — and the panel then stays on the
// Agent's own folder, exactly as it did before the per-user layout existed.
let wsOwnUserIdCache;

/**
 * Which "scope" the panel is currently showing, bumped whenever that scope
 * changes: opening the panel, switching Agent, switching session. A listing or
 * preview that started under the previous scope carries files from a folder the
 * reader has already left, so its late arrival must be dropped rather than
 * rendered — for an account switch it would otherwise paint another identity's
 * entries into this one's panel. Every async read captures the epoch it started
 * under (``wsScopeEpoch``) and compares before rendering.
 */
let wsScopeEpoch = 0;

/** Enter a new scope, invalidating everything already in flight. */
function wsNewScopeEpoch() {
    wsScopeEpoch += 1;
    return wsScopeEpoch;
}

/** Whether a read started under ``epoch`` has been overtaken by a scope change. */
function wsScopeStale(epoch) {
    return epoch !== wsScopeEpoch;
}

// Where the folders of each Agent live, relative to the workspace root the file
// API reports. That root is the shared root of the caller's tenant (or a
// project the session opened), and every Agent — shared or private — keeps its
// own things under `agents/<agent id>`: `AGENT.md`, `knowledge/`, `memory/`,
// `outputs/`, `scheduler/`, `skills/`. The panel opens on that folder, so the
// entry answers "where does this Agent keep its files?" (server side, the same
// layout is what `_tenant_agent_workspace` resolves and what an Agent's
// workspace is set to).
const WS_AGENT_DIR = 'agents';

// The protected per-user container inside an Agent's workspace — the same
// directory `common.state_dir.agent_user_root` resolves. A tenant-shared Agent
// keeps every member's platform files (uploads, delivered results, scratch
// work) under `user/<user id>/`, and the file surface refuses one member the
// other's subtree.
const WS_USER_DIR = 'user';

/** The Agent the panel is browsing for; '' before the roster is known. */
function wsScopedAgentId() {
    return wsAgentOverride
        || ((typeof activeAgentId !== 'undefined') ? activeAgentId : '');
}

/** An Agent's own folder, relative to the workspace root; '' without an Agent. */
function wsAgentDirPath(agentId) {
    const id = agentId || '';
    return id ? `${WS_AGENT_DIR}/${id}` : '';
}

/**
 * ``'private'`` or ``'tenant'`` for an Agent the console knows about, else ''.
 *
 * Read off the rosters the console already holds (the use range the chat
 * pickers work from, then the management catalogue). The value is the server's
 * own derivation, repeated — the console never infers who a shared Agent
 * belongs to — and a row neither roster has stays '' rather than being assumed
 * to be either state.
 */
function wsAgentVisibility(agentId) {
    if (!agentId) return '';
    const rosters = [];
    if (typeof chatAgentCatalog !== 'undefined' && chatAgentCatalog) rosters.push(chatAgentCatalog);
    if (typeof agentCatalog !== 'undefined' && agentCatalog) rosters.push(agentCatalog);
    for (const roster of rosters) {
        const row = roster.find(a => a && a.id === agentId);
        if (row && typeof row.visibility === 'string' && row.visibility) return row.visibility;
    }
    return '';
}

/**
 * This caller's own file folder inside a tenant-shared Agent, or ''.
 *
 * A shared Agent keeps the things every member shares (`AGENT.md`,
 * `knowledge/`, `memory/`) at its own root, and each member's own uploads and
 * delivered results under `user/<user id>`. Opening the panel on the shared
 * root would show a member a folder that is not theirs, so a shared Agent opens
 * on the caller's own subtree instead.
 *
 * Empty whenever the answer would be a guess: a private Agent has no such split
 * (its whole root is the owner's), an unknown roster row is not treated as
 * shared, and an unknown user id is not invented.
 */
function wsOwnUserDirPath(agentId) {
    const dir = wsAgentDirPath(agentId);
    if (!dir || !wsOwnUserIdCache) return '';
    if (wsAgentVisibility(agentId) !== 'tenant') return '';
    return `${dir}/${WS_USER_DIR}/${wsOwnUserIdCache}`;
}

/** The directory the panel opens on: the caller's own folder of a shared Agent,
 *  otherwise the Agent's own folder -- or the project root, when the session's
 *  files are a local project on this machine (task 9.2). A local project has no
 *  `agents/<id>/` and no per-member subtree, so the Agent arithmetic must not run
 *  against it: the user opened a directory and that directory is the root. */
function wsAgentLandingPath() {
    const localLanding = wsLocalLanding();
    if (localLanding !== null) return localLanding;
    const agentId = wsScopedAgentId();
    return wsOwnUserDirPath(agentId) || wsAgentDirPath(agentId);
}

/** The local source's landing path, or null when the backend decides. */
function wsLocalLanding() {
    if (typeof CowProjectSource === 'undefined') return null;
    try {
        return CowProjectSource.landing();
    } catch (_) {
        return null;
    }
}

/**
 * The label to show for a local project's root.
 *
 * The project's *name*, never its directory: the shell keeps the absolute root
 * to itself, and the console's selector is where the user chose it. Falls back to
 * the generic label so the crumb is never blank.
 */
function wsLocalRootLabel() {
    try {
        if (typeof _wsSelState !== 'undefined' && _wsSelState && _wsSelState.current
                && String(_wsSelState.current.path || '').indexOf('desktop:') === 0) {
            return String(_wsSelState.current.name || '') || t('ws_sel_local_dir');
        }
    } catch (_) { /* the selector is not on this page */ }
    return t('ws_sel_local_dir');
}

/**
 * The session's file *source* changed (task 9.2): a local project was opened or
 * closed.
 *
 * The panel must land on the new source's root and drop what it was showing --
 * otherwise it would keep listing the previous project's files, or the server's,
 * under the name of the one the user just opened. Only the panel's own state is
 * touched: an in-flight listing is invalidated by the scope epoch rather than
 * awaited, so the switch is immediate.
 */
function wsSourceChanged(opts) {
    const { reveal = false } = opts || {};
    wsNewScopeEpoch();
    wsCurrentSource = 'backend';
    wsCurrentRoot = '';
    wsCurrentFile = null;
    wsCurrentDir = wsAgentLandingPath();
    // The panel follows the *source*, so the watch does too (task 9.3): a
    // subscription that outlived the project it was about would keep refreshing
    // a directory the user has already replaced.
    wsWatchSource();
    // Opening a local project is a deliberate act; showing the files it will be
    // read from is the confirmation the other selection flows also give.
    if (reveal && typeof openWorkspacePanel === 'function') {
        wsAutoOpenSuppressed = false;
        openWorkspacePanel('files');
    }
    if (wsPanelOpen) refreshWorkspaceTree();
}

// =====================================================================
// Local project watching (task 9.3)
// =====================================================================

/** Unsubscribe from the host's project changes, when a watch is live. */
let wsWatchOff = null;

/**
 * The file the panel has told the user about since it last read it, or `''`.
 *
 * Only the *notification* lives here: the editor keeps its own baseline, and the
 * save path keeps its own conflict check. This is what the title's warning is
 * drawn from, so a change reported twice does not nag twice.
 */
let wsStaleFile = '';

/**
 * Follow the current project for changes, or stop following it.
 *
 * Subscribed through the same source adapter the panel reads from, so a page
 * with no desktop host (the browser) and a session with no local project both
 * end up with no subscription at all -- rather than a timer that polls nothing.
 */
function wsWatchSource() {
    wsWatchStop();
    if (typeof CowProjectSource === 'undefined' || typeof CowProjectSource.watch !== 'function') return;
    if (wsLocalLanding() === null) return;
    try {
        wsWatchOff = CowProjectSource.watch(wsOnProjectChanged);
    } catch (_) {
        wsWatchOff = null;
    }
}

function wsWatchStop() {
    const off = wsWatchOff;
    wsWatchOff = null;
    if (typeof off === 'function') {
        try { off(); } catch (_) { /* the host is already gone */ }
    }
}

/**
 * One change report from the local project.
 *
 * Two different reactions, because the two panes can lose different things:
 *
 *  - the **file list** is a view of the directory, so it is re-listed -- from
 *    the same local source, which re-verifies the authorization on the way;
 *  - the **open file** may be something the user is typing into, so it is never
 *    silently reloaded: the panel checks it and says that it changed on disk,
 *    leaving the unsaved text (and the save path's own conflict check) alone.
 */
function wsOnProjectChanged(event) {
    if (!event) return;
    if (event.ended) {
        // The authorization behind this project is gone (a revoke, a re-pick, a
        // detached device). Re-verify instead of continuing to show a listing
        // nobody can read: `_desktopLocalLost` drops the reference and has the
        // selector re-read the *live* source, so the panel and the session's
        // execution target describe the same project again (task 9.3).
        wsWatchStop();
        if (event.reason === 'stale_context' && typeof _desktopLocalLost === 'function') {
            _desktopLocalLost(event.reason);
            return;
        }
        if (typeof wsSourceChanged === 'function') wsSourceChanged();
        return;
    }
    const touched = (event.changed || []).concat(event.removed || []);
    if (wsPanelOpen && wsDirWasTouched(touched, wsCurrentDir)) refreshWorkspaceTree();
    // A file the preview is showing may be the one that changed. `wsCurrentFile`
    // is only ever a file (directories navigate instead of previewing).
    if (wsCurrentFile) wsCheckOpenFile(touched);
}

/** Whether the directory on screen (or one of its parents) was reported. */
function wsDirWasTouched(touched, dir) {
    const current = dir || '';
    let acc = current;
    for (;;) {
        if (touched.indexOf(acc) >= 0) return true;
        if (!acc) return false;
        const cut = acc.lastIndexOf('/');
        acc = cut < 0 ? '' : acc.slice(0, cut);
    }
}

/**
 * Re-check the file in the preview against the disk, without discarding edits.
 *
 * The comparison is on the version the panel recorded when it opened the file,
 * so a change that does not touch this file costs nothing; when it does, the
 * user is told once, and the save button keeps working -- a save against a stale
 * baseline comes back as `conflict`, which the panel already turns into an
 * explicit overwrite confirmation.
 */
async function wsCheckOpenFile(touched) {
    const target = wsCurrentFile;
    if (!target) return;
    const path = wsEditTargetPath(target);
    if (!path) return;
    const dir = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '';
    if (!wsDirWasTouched(touched, dir)) return;
    const epoch = wsScopeEpoch;
    let meta = null;
    try {
        meta = (await wsApi(`/api/workspace/resolve?path=${encodeURIComponent(path)}`)).file;
    } catch (e) {
        if (wsScopeStale(epoch) || wsCurrentFile !== target) return;
        wsStaleFile = path;
        _wsToast(t('ws_local_file_gone'));
        return;
    }
    if (wsScopeStale(epoch) || wsCurrentFile !== target) return;
    const known = Number(target.mtime || 0);
    const now = Number((meta && meta.mtime) || 0);
    if (now && known && now === known) return;
    if (wsStaleFile === path) return;
    wsStaleFile = path;
    _wsToast(t('ws_local_file_changed'));
    wsRenderPreviewTitle();
}

// =====================================================================
// Metadata helpers
// =====================================================================
const WS_KIND_ICONS = {
    directory: 'fa-folder',
    html: 'fa-file-code',
    markdown: 'fa-file-lines',
    image: 'fa-file-image',
    video: 'fa-file-video',
    audio: 'fa-file-audio',
    pdf: 'fa-file-pdf',
    csv: 'fa-file-csv',
    code: 'fa-file-code',
    office: 'fa-file-word',
    text: 'fa-file-lines',
    file: 'fa-file',
};

const WS_KIND_BY_EXT = {
    html: ['html', 'htm'],
    markdown: ['md', 'markdown'],
    image: ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'svg', 'ico'],
    video: ['mp4', 'webm', 'mov', 'avi', 'mkv', 'm4v'],
    audio: ['mp3', 'wav', 'ogg', 'm4a', 'flac', 'aac'],
    pdf: ['pdf'],
    csv: ['csv', 'tsv'],
    code: ['py', 'js', 'ts', 'tsx', 'jsx', 'java', 'c', 'cpp', 'h', 'go', 'rs',
           'rb', 'php', 'sh', 'sql', 'css', 'scss', 'json', 'yaml', 'yml',
           'xml', 'toml', 'ini'],
    text: ['txt', 'log'],
    office: ['doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx'],
};

const WS_EXT_KIND = (() => {
    const map = {};
    for (const [kind, exts] of Object.entries(WS_KIND_BY_EXT)) {
        exts.forEach(e => { map[e] = kind; });
    }
    return map;
})();

const WS_PREVIEWABLE = new Set(
    ['html', 'markdown', 'image', 'video', 'audio', 'pdf', 'csv', 'code', 'text']
);

// Kinds the panel offers an editor for. Mirrors EDITABLE_KINDS in
// agent/protocol/artifact.py; the server rejects anything else on save.
const WS_EDITABLE = new Set(['html', 'markdown', 'csv', 'code', 'text']);

function wsKindOf(name) {
    const ext = (name || '').split('.').pop().toLowerCase();
    return WS_EXT_KIND[ext] || 'file';
}

function wsIconClass(kind) {
    return `fas ${WS_KIND_ICONS[kind] || WS_KIND_ICONS.file} ws-icon-${kind}`;
}

function wsFormatSize(bytes) {
    if (!bytes && bytes !== 0) return '';
    const units = ['B', 'KB', 'MB', 'GB'];
    let n = bytes;
    for (const u of units) {
        if (n < 1024) return `${u === 'B' ? Math.round(n) : n.toFixed(1)}${u}`;
        n /= 1024;
    }
    return `${n.toFixed(1)}TB`;
}

// The local project source (task 9.2) reads the panel's own classifiers and the
// binding the console already tracks, rather than keeping a second copy of
// either: the icons, the preview, the editor's rules and the choice of source
// must not be able to disagree. Configured once, at load, before any request.
if (typeof CowProjectSource !== 'undefined') {
    CowProjectSource.configure({
        binding: () => (typeof _desktopContextForRequest === 'function'
            ? _desktopContextForRequest() : null),
        kindOf: (name) => wsKindOf(name),
        editable: (kind) => WS_EDITABLE.has(kind),
        previewable: (kind) => WS_PREVIEWABLE.has(kind),
    });
}

/**
 * Add the panel's own scope to a workspace API path.
 *
 * `sessionId` and `activeAgentId` are globals from console.js on the same page.
 * Scoping reads to the session keeps the file panel / @ picker / preview
 * following the session's opened project directory; and without an opened
 * project the root falls back to the shared root of the caller's tenant, where
 * every Agent has a folder of its own, so the panel must say which Agent is
 * active — else it always shows the default Agent's directory.
 */
function wsScopedPath(path) {
    try {
        if (path.startsWith('/api/workspace/')) {
            const sid = (typeof sessionId !== 'undefined') ? sessionId : '';
            if (sid) path += (path.includes('?') ? '&' : '?') + 'session=' + encodeURIComponent(sid);
            const aid = wsScopedAgentId();
            if (aid) path += (path.includes('?') ? '&' : '?') + 'agent=' + encodeURIComponent(aid);
        }
    } catch (e) { /* globals not available yet */ }
    return path;
}

/** The error for a response the panel cannot use, carrying the server's code. */
function wsResponseError(res, data) {
    const err = new Error(data.message || `Request failed (HTTP ${res.status})`);
    err.status = res.status;
    err.code = data.code;
    err.data = data;
    return err;
}

async function wsApi(path) {
    // The session's *source* decides where the panel reads from (task 9.2). A
    // session bound to a project on this machine reads it through the local
    // source -- the backend has no such path, and its answer would either 404 or
    // describe a different file with the same name. `null` means "no local
    // project here", and the original request is made untouched, so a backend
    // source keeps its existing behaviour.
    if (typeof CowProjectSource !== 'undefined') {
        const local = await CowProjectSource.handle(path);
        if (local) {
            if (local.status === 'success') return local;
            const err = new Error(local.message || 'request failed');
            err.code = local.code;
            err.local = true;
            // A local read that comes back `stale_context` is the host saying the
            // authorization behind this project is gone (task 9.3). Drop the
            // reference and re-derive the source rather than leaving a chip and a
            // panel pointed at a directory nothing can read; the refusal still
            // reaches the caller, so the panel can say what happened.
            if (local.code === 'stale_context' && typeof _desktopLocalLost === 'function') {
                _desktopLocalLost(local.code);
            }
            throw err;
        }
    }
    const res = await fetch(wsScopedPath(path));
    let data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok || data.status !== 'success') throw wsResponseError(res, data);
    return data;
}

/** A workspace write: same scope, same refusal decoding, JSON body. */
async function wsApiPost(path, body) {
    const res = await fetch(wsScopedPath(path), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok || data.status !== 'success') throw wsResponseError(res, data);
    return data;
}

/**
 * Turn a workspace request failure into a readable, localized message.
 * The backend reports a stable `code` (e.g. `forbidden`, `database_unavailable`);
 * fall back to the raw status/message when it is absent.
 */
function wsErrorMessage(e) {
    const status = e && e.status;
    const code = e && e.code;
    const msg = String((e && e.message) || '');
    if (code === 'forbidden' || status === 403 || /forbidden/i.test(msg)) {
        return t('ws_forbidden');
    }
    if (code === 'database_unavailable' || status === 503
            || /unavailable in database identity mode/i.test(msg)) {
        return t('ws_unavailable');
    }
    // A refusal from the *local* source (task 9.2). Each one is a different
    // situation with a different fix, so they are told apart rather than
    // collapsed into "the file could not be read".
    if (code === 'source_read_only') return t('ws_local_read_only');
    if (code === 'device_offline' || code === 'device_error') return t('ws_local_offline');
    if (code === 'stale_context' || code === 'grant_revoked') return t('ws_local_stale');
    if (e && e.local) return t('ws_local_unavailable');
    return msg || t('ws_preview_failed');
}

// =====================================================================
// Panel open / close / resize
// =====================================================================
function openWorkspacePanel(tab) {
    const panel = document.getElementById('workspace-panel');
    if (!panel) return;
    panel.classList.remove('hidden');
    wsPanelOpen = true;
    const width = parseInt(localStorage.getItem(WS_WIDTH_KEY), 10);
    if (width >= WS_MIN_WIDTH) panel.style.width = `${width}px`;
    if (tab) switchWorkspaceTab(tab);
}

/**
 * @param {boolean} byUser - true when triggered by the close button, which
 *   also disables auto-open for the rest of the session.
 */
function closeWorkspacePanel(byUser) {
    const panel = document.getElementById('workspace-panel');
    if (!panel) return;
    panel.classList.add('hidden');
    wsPanelOpen = false;
    // Anything still in flight belongs to the visit that just ended; reopening
    // the panel starts a fresh scope and must not be painted by that answer.
    wsNewScopeEpoch();
    if (byUser) wsAutoOpenSuppressed = true;
}

function toggleWorkspacePanel() {
    if (wsPanelOpen) {
        closeWorkspacePanel(true);
        return;
    }
    wsAutoOpenSuppressed = false;
    openWorkspacePanel();
    showActiveAgentWorkspace();
}

/**
 * Land the file list on the folder the panel opens on.
 *
 * Opening the panel asks for that Agent's files, so whatever the previous visit
 * left behind is dropped first: the file previewed then (which used to make the
 * panel reopen on the preview tab), the directory drilled into then, and any
 * fallback Agent chosen then. The rows on screen belong to that old folder, so
 * the list is emptied here — that is what makes the tab switch below re-list
 * instead of showing stale entries as if they were this Agent's own.
 *
 * A tenant-shared Agent opens on the caller's own `user/<user id>` folder, so
 * the caller's user id has to be known before the path can be derived; only
 * then is the landing path computed and the tab switched. Another scope change
 * (a different Agent or session) landing meanwhile takes the panel over, and
 * this visit renders nothing (see `wsScopeEpoch`).
 */
function showActiveAgentWorkspace() {
    // A read in flight for the previous visit's Agent (or the previous session)
    // must not land in this one.
    const epoch = wsNewScopeEpoch();
    // The fallback Agent of a previous visit is dropped *before* the landing
    // path is derived, so the folder shown is the active Agent's own.
    wsAgentOverride = '';
    // A visit starts on the files, not where the last one ended up.
    wsResetUploadAndTrashState();
    const list = document.getElementById('ws-file-list');
    if (list && list.childElementCount) list.innerHTML = '';
    wsOwnUserId().then(() => {
        if (wsScopeStale(epoch)) return;
        wsCurrentDir = wsAgentLandingPath();
        switchWorkspaceTab('files');
    });
}

function switchWorkspaceTab(tab) {
    wsActiveTab = tab;
    document.querySelectorAll('.workspace-tab').forEach(el => {
        el.classList.toggle('active', el.dataset.wsTab === tab);
    });
    document.querySelectorAll('.workspace-body').forEach(el => {
        el.classList.toggle('active', el.id === `ws-body-${tab}`);
    });
    wsUpdateHeaderActions();
    if (tab === 'files' && !document.getElementById('ws-file-list').childElementCount) {
        loadWorkspaceDir(wsCurrentDir);
    }
}

function wsUpdateHeaderActions() {
    const onFile = wsActiveTab === 'preview' && !!wsCurrentFile;
    // While editing, the viewer actions would act on the saved file rather than
    // on what is in the text area, which reads as a bug. Hide them instead.
    ['ws-btn-external', 'ws-btn-download', 'ws-btn-copy'].forEach(id => {
        document.getElementById(id)?.classList.toggle('hidden', !onFile || wsEditing);
    });
    // The same three buttons mean something *else* for a file on this machine
    // (task 9.4): there is no server URL to open or download, so they become
    // "open with a system application", "save a copy as" and "copy the full
    // local path". The labels say which, because a button whose behaviour
    // changes silently is a button the user cannot trust.
    const local = onFile && wsIsLocalFile(wsCurrentFile);
    wsRetitle('ws-btn-external', local ? 'ws_open_system' : 'ws_open_external');
    wsRetitle('ws-btn-download', local ? 'ws_save_as' : 'ws_download');
    wsRetitle('ws-btn-copy', local ? 'ws_local_copy_path' : 'ws_copy_path');
    document.getElementById('ws-btn-reveal')
        ?.classList.toggle('hidden', !local || wsEditing);
    document.getElementById('ws-btn-edit')
        ?.classList.toggle('hidden', !onFile || wsEditing || !wsIsEditable(wsCurrentFile));
    ['ws-btn-save', 'ws-btn-edit-cancel'].forEach(id => {
        document.getElementById(id)?.classList.toggle('hidden', !onFile || !wsEditing);
    });
}

/**
 * Point a header button's tooltip at a key, for the language in force *now*.
 *
 * `data-i18n-title` is what a later language switch reads, so both are set:
 * setting only `title` would look right until the user changed language, and
 * setting only the attribute would leave the old text until they did.
 */
function wsRetitle(id, key) {
    const el = document.getElementById(id);
    if (!el || el.dataset.i18nTitle === key) return;
    el.dataset.i18nTitle = key;
    el.title = t(key);
}

function initWorkspaceResizer() {
    const resizer = document.getElementById('ws-resizer');
    const panel = document.getElementById('workspace-panel');
    if (!resizer || !panel) return;

    let startX = 0;
    let startWidth = 0;

    function onMove(e) {
        const delta = startX - e.clientX;
        const next = Math.max(WS_MIN_WIDTH, Math.min(window.innerWidth * 0.7, startWidth + delta));
        panel.style.width = `${next}px`;
    }

    function onUp() {
        resizer.classList.remove('dragging');
        document.body.style.userSelect = '';
        // The preview iframe swallows mousemove while dragging over it.
        document.getElementById('ws-preview-content')?.style.removeProperty('pointer-events');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        localStorage.setItem(WS_WIDTH_KEY, String(parseInt(panel.style.width, 10) || WS_DEFAULT_WIDTH));
    }

    resizer.addEventListener('mousedown', (e) => {
        e.preventDefault();
        startX = e.clientX;
        startWidth = panel.offsetWidth;
        resizer.classList.add('dragging');
        document.body.style.userSelect = 'none';
        document.getElementById('ws-preview-content')?.style.setProperty('pointer-events', 'none');
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
    });
}

// =====================================================================
// Preview
// =====================================================================
function wsSetPreviewEmpty(message, icon) {
    const body = document.getElementById('ws-preview-content');
    const title = document.getElementById('ws-preview-title');
    if (!body) return;
    title?.classList.add('hidden');
    body.innerHTML = `<div class="workspace-empty">
        <i class="fas ${icon || 'fa-eye'}"></i>
        <span>${escapeHtml(message)}</span>
    </div>`;
}

/**
 * Open a file in the preview tab.
 * @param {object|string} target - file metadata, or a path to resolve first.
 */
async function openInPreview(target) {
    // Opening another file replaces the editor, so settle unsaved edits first.
    if (!wsGuardUnsaved(() => openInPreview(target))) return;

    let meta = target;
    if (typeof target === 'string') {
        const epoch = wsScopeEpoch;
        try {
            meta = (await wsApi(`/api/workspace/resolve?path=${encodeURIComponent(target)}`)).file;
        } catch (e) {
            if (wsScopeStale(epoch)) return;
            openWorkspacePanel('preview');
            wsSetPreviewEmpty(wsErrorMessage(e), 'fa-triangle-exclamation');
            return;
        }
        // The reader moved on (another Agent/session/account) while the path was
        // being resolved: opening it now would preview a file of the old scope.
        if (wsScopeStale(epoch)) return;
    }
    if (!meta) return;
    // Directories have nothing to render; browse into them instead.
    if (meta.is_dir) {
        openWorkspacePanel('files');
        switchWorkspaceTab('files');
        loadWorkspaceDir(meta.path || '');
        return;
    }

    wsCurrentFile = meta;
    wsEditing = false;
    wsStaleFile = '';
    openWorkspacePanel('preview');
    switchWorkspaceTab('preview');
    wsRenderPreviewTitle();
    wsUpdateHeaderActions();
    await wsRenderPreview(meta);
}

/** Show the current file's path, marked with a dot while edits are unsaved. */
function wsRenderPreviewTitle() {
    const title = document.getElementById('ws-preview-title');
    if (!title) return;
    if (!wsCurrentFile) {
        title.classList.add('hidden');
        return;
    }
    const path = wsEditTargetPath(wsCurrentFile);
    const name = wsCurrentFile.path || wsCurrentFile.file_name || wsCurrentFile.name || '';
    const stale = !!wsStaleFile && wsStaleFile === path;
    title.textContent = wsEditorDirty() ? `${name} •` : name;
    // The disk changed under this file (task 9.3). A hover note rather than a
    // second marker: the unsaved dot already means "there is something to
    // resolve here", and the save path asks the real question on save.
    title.title = stale ? t('ws_local_file_changed') : '';
    title.classList.remove('hidden');
}

async function wsRenderPreview(meta) {
    const body = document.getElementById('ws-preview-content');
    if (!body) return;
    const kind = meta.kind || wsKindOf(meta.file_name || meta.name || meta.path);
    const name = meta.file_name || meta.name || (meta.path || '').split('/').pop();
    const previewUrl = meta.preview_url;
    const rawUrl = meta.raw_url || previewUrl;
    const local = wsIsLocalFile(meta);

    if (kind === 'html') {
        body.innerHTML = '';
        const frame = document.createElement('iframe');
        if (local) {
            // No allow-same-origin: the generated page runs in an opaque origin
            // and cannot reach the console's storage or auth cookie. No
            // allow-popups either: a preview is not a place to launch things
            // from (task 9.5).
            frame.setAttribute('sandbox', 'allow-scripts allow-forms allow-modals');
            // The document is handed over as `srcdoc` in the same opaque origin
            // the server's HTML preview already runs in -- but only when the
            // host cannot serve it properly. A local file has no
            // server URL to point a frame at, and one must not be invented, or
            // the preview would show the *server's* file of the same name.
            const epoch = wsScopeEpoch;
            const ticket = await wsLocalPreviewTicket(meta);
            if (wsScopeStale(epoch)) return;
            if (ticket && ticket.ok) {
                // The host's own answer: the response carries the sandbox and a
                // `default-src 'none'` policy, so this frame cannot fetch, post,
                // frame or object -- and the URL stops working when it expires.
                frame.src = ticket.url;
            } else if (ticket && !wsLocalPreviewUnsupported(ticket)) {
                wsSetPreviewEmpty(wsLocalPreviewMessage(ticket), 'fa-triangle-exclamation');
                return;
            } else {
                let page = null;
                try {
                    page = await wsLocalPreviewText(meta);
                } catch (e) {
                    if (wsScopeStale(epoch)) return;
                    wsSetPreviewEmpty(wsErrorMessage(e), 'fa-triangle-exclamation');
                    return;
                }
                if (wsScopeStale(epoch)) return;
                frame.srcdoc = page.text;
            }
        } else {
            // The server's own preview: unchanged sandbox flags, and unchanged
            // behaviour -- the response it loads already carries its own policy.
            frame.setAttribute('sandbox', 'allow-scripts allow-popups allow-forms allow-modals');
            frame.src = previewUrl;
        }
        body.appendChild(frame);
        return;
    }

    if (local && (kind === 'image' || kind === 'video' || kind === 'audio' || kind === 'pdf')) {
        // These need the file's *bytes*, which the panel's local read (a bounded
        // text page) does not carry. The host reads them and answers with a
        // short-lived protected URL for one file (task 9.5): the panel embeds
        // it, and never receives the bytes or a path.
        const epoch = wsScopeEpoch;
        const ticket = await wsLocalPreviewTicket(meta);
        if (wsScopeStale(epoch)) return;
        if (ticket && ticket.ok) {
            if (kind === 'image') {
                wsRenderLocalMedia(body, 'img', ticket, name, epoch, meta);
            } else if (kind === 'video') {
                wsRenderLocalMedia(body, 'video', ticket, name, epoch, meta);
            } else if (kind === 'audio') {
                wsRenderLocalMedia(body, 'audio', ticket, name, epoch, meta);
            } else {
                // A PDF is served by the same scheme, but this shell has no
                // PDF viewer, so the panel says so instead of drawing a blank
                // frame -- and offers the system application, which can.
                wsSetLocalPreviewFallback(body, name);
            }
            return;
        }
        if (ticket && !wsLocalPreviewUnsupported(ticket)) {
            const refusal = ticket;
            if (refusal.code === 'limit_exceeded' || refusal.code === 'unsupported_type') {
                wsSetLocalPreviewFallback(body, name, wsLocalPreviewMessage(refusal));
                return;
            }
            wsSetPreviewEmpty(wsLocalPreviewMessage(refusal), 'fa-triangle-exclamation');
            return;
        }
        // No host, or a host that cannot preview: saying so is the honest
        // answer; naming a server URL here would preview a different file with
        // the same name.
        wsSetLocalPreviewFallback(body, name);
        return;
    }

    if (kind === 'image') {
        body.innerHTML = `<div class="ws-pad"><img class="ws-media" src="${escapeHtml(rawUrl)}" alt="${escapeHtml(name)}"></div>`;
        return;
    }

    if (kind === 'video') {
        body.innerHTML = `<div class="ws-pad"><video class="ws-media" controls preload="metadata" src="${escapeHtml(rawUrl)}"></video></div>`;
        return;
    }

    if (kind === 'audio') {
        body.innerHTML = `<div class="ws-pad"><audio class="ws-media" controls src="${escapeHtml(rawUrl)}"></audio></div>`;
        return;
    }

    if (kind === 'pdf') {
        body.innerHTML = '';
        const frame = document.createElement('iframe');
        frame.src = previewUrl;
        body.appendChild(frame);
        return;
    }

    if (kind === 'markdown' || kind === 'code' || kind === 'text' || kind === 'csv') {
        body.innerHTML = `<div class="workspace-empty"><i class="fas fa-spinner fa-spin"></i></div>`;
        const epoch = wsScopeEpoch;
        let partial = false;
        try {
            let text;
            if (local) {
                // Paged on purpose: a preview pulls at most one bounded page, and
                // a partial page is labelled as one rather than passed off as the
                // whole file.
                const page = await wsLocalPreviewText(meta);
                text = page.text;
                partial = page.truncated;
            } else {
                const res = await fetch(previewUrl);
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                text = await res.text();
            }
            // The scope changed while the body was loading: this text belongs to
            // the file the reader has already navigated away from.
            if (wsScopeStale(epoch)) return;
            if (kind === 'markdown') {
                body.innerHTML = `<div class="ws-pad msg-content">${renderMarkdown(text)}</div>`;
            } else if (kind === 'csv') {
                body.innerHTML = `<pre>${escapeHtml(text)}</pre>`;
            } else {
                const lang = (name.split('.').pop() || '').toLowerCase();
                body.innerHTML = `<pre><code class="language-${escapeHtml(lang)}">${escapeHtml(text)}</code></pre>`;
            }
            applyHighlighting(body);
            if (partial) {
                body.insertAdjacentHTML('beforeend', `<div class="workspace-empty" style="height:auto;padding:12px;">
                    <span>${escapeHtml(t('ws_preview_partial'))}</span></div>`);
            }
        } catch (e) {
            if (wsScopeStale(epoch)) return;
            wsSetPreviewEmpty(wsErrorMessage(e), 'fa-triangle-exclamation');
        }
        return;
    }

    // Unsupported type: offer a download instead of a broken viewer.
    const download = local
        // A local file has nothing to download *from* the browser: the bytes are
        // on this machine already, and a link with no href would be a broken
        // button pretending to be an action.
        ? ''
        : `<a href="${escapeHtml(rawUrl)}" download="${escapeHtml(name)}"
               class="file-card-btn" style="width:auto;padding:4px 12px;border:1px solid currentColor;">
               <i class="fas fa-download"></i>&nbsp;${escapeHtml(t('ws_download'))}
           </a>`;
    body.innerHTML = `<div class="workspace-empty">
        <i class="${wsIconClass(kind)}"></i>
        <span>${escapeHtml(name)}</span>
        <span>${escapeHtml(t(local ? 'ws_local_preview_binary' : 'ws_no_inline_preview'))}</span>
        ${download}
    </div>`;
}

/**
 * Whether a file the panel is showing comes from the machine it is running on.
 *
 * Local entries are marked by the source adapter (`source: 'desktop'`,
 * `local: true`) and deliberately carry no `raw_url` / `preview_url`: those are
 * server URLs, and asking the server for a path that exists on the client would
 * answer about a different file with the same name.
 */
function wsIsLocalFile(meta) {
    return !!meta && (meta.local === true || meta.source === 'desktop');
}

/** One bounded page of a local file's text, through the local source. */
async function wsLocalPreviewText(meta) {
    const data = await wsApi(`/api/workspace/read?path=${encodeURIComponent(wsEditTargetPath(meta))}`);
    return { text: String(data.content || ''), truncated: !!data.truncated };
}

/**
 * The protected preview of one local file, or `null` when there is no host
 * (task 9.5).
 *
 * `null` is not a refusal: it means "this environment cannot preview local
 * content at all", which is the one case where the panel falls back to what it
 * did before. Every other answer is the host's own -- `{ok:true, url, ...}` or
 * `{ok:false, code, message}` -- and is reported, because a file that is gone
 * and a project whose grant was revoked are different situations that would
 * otherwise be shown as a blank frame.
 */
async function wsLocalPreviewTicket(meta) {
    if (typeof CowProjectSource === 'undefined' || typeof CowProjectSource.preview !== 'function') return null;
    if (wsLocalCardIsGone(meta)) return { ok: false, code: 'not_found', message: '' };
    return CowProjectSource.preview(wsEditTargetPath(meta), wsLocalCardScope(meta));
}

/** True when a preview refusal means "this host cannot preview at all". */
function wsLocalPreviewUnsupported(refusal) {
    const code = (refusal && refusal.code) || '';
    return code === 'feature_unavailable' || code === 'no_host';
}

/** Why a local file could not be previewed, in the user's language. */
function wsLocalPreviewMessage(refusal) {
    const code = (refusal && refusal.code) || '';
    if (code === 'not_found') return t('ws_local_file_gone');
    if (code === 'wrong_project') return t('ws_local_other_project');
    if (code === 'stale_context' || code === 'grant_revoked') return t('ws_local_stale');
    if (code === 'device_offline' || code === 'device_error') return t('ws_local_offline');
    if (code === 'limit_exceeded') return t('ws_local_preview_too_large');
    if (code === 'unsupported_type') return t('ws_local_preview_unsupported');
    if (code === 'feature_unavailable' || code === 'no_host') return t('ws_local_action_unavailable');
    return t('ws_local_preview_failed');
}

/**
 * One local media element, pointed at a protected preview URL (task 9.5).
 *
 * The URL is short-lived by design, so an image that is still on screen when its
 * ticket expires is re-minted **once** rather than left as a broken picture: the
 * panel asks the host again, and reports the refusal if the second answer is no.
 * A `<video>` or `<audio>` is deliberately not re-minted: the user is watching
 * it, and swapping the source under them would be a stranger failure than an
 * error they can refresh away.
 */
function wsRenderLocalMedia(body, tag, ticket, name, epoch, meta) {
    body.innerHTML = '';
    const pad = document.createElement('div');
    pad.className = 'ws-pad';
    const media = document.createElement(tag);
    media.className = 'ws-media';
    media.src = ticket.url;
    if (tag === 'img') {
        media.alt = name;
    } else {
        media.controls = true;
        media.setAttribute('preload', 'metadata');
    }
    let retried = false;
    media.addEventListener('error', async () => {
        if (retried || tag !== 'img' || wsScopeStale(epoch)) return;
        retried = true;
        const again = await wsLocalPreviewTicket(meta);
        if (wsScopeStale(epoch)) return;
        if (again && again.ok) media.src = again.url;
        else if (again) wsSetPreviewEmpty(wsLocalPreviewMessage(again), 'fa-triangle-exclamation');
    });
    pad.appendChild(media);
    body.appendChild(pad);
}

/**
 * The card shown when a local file has no inline preview here.
 *
 * It carries the one action that does work for a file that is on this machine:
 * the system's own application (task 9.4). `message` says *why* the preview did
 * not happen when the host gave a reason -- a kind this surface cannot render, or
 * a file too large to pull into the main process -- and falls back to the
 * general sentence otherwise.
 */
function wsSetLocalPreviewFallback(body, name, message) {
    body.innerHTML = `<div class="workspace-empty">
        <i class="${wsIconClass(wsKindOf(name))}"></i>
        <span>${escapeHtml(name)}</span>
        <span>${escapeHtml(message || t('ws_local_preview_binary'))}</span>
        <button class="file-card-btn" style="width:auto;padding:4px 12px;border:1px solid currentColor;"
                onclick="openPreviewExternally()">${escapeHtml(t('ws_open_system'))}</button>
    </div>`;
}

function openPreviewExternally() {
    if (!wsCurrentFile) return;
    if (wsIsLocalFile(wsCurrentFile)) {
        // There is no server URL for a local file, and opening `undefined` would
        // navigate to the string "undefined". For a file that *is* on this
        // machine the honest answer is the system's own application (task 9.4):
        // the same button, the action that actually means "open it".
        wsLocalAction(wsCurrentFile, 'open').then((outcome) => {
            if (outcome.ok) _wsToast(t('ws_local_opened'));
            else _wsToast(wsLocalActionMessage(outcome));
        });
        return;
    }
    window.open(wsCurrentFile.preview_url || wsCurrentFile.raw_url, '_blank', 'noopener');
}

/**
 * One system action on one local file (task 9.4).
 *
 * Every call re-verifies on the host side -- the live grant, the file itself,
 * the real path inside the project -- so a file deleted or a project closed
 * since the panel read it is refused with the reason it actually failed,
 * rather than opening something stale or silently doing nothing.
 *
 * @param {object} meta - the entry the panel is showing or a card carries.
 * @param {string} action - open | reveal | copyPath | saveAs
 * @returns {Promise<{ok: boolean, code?: string, message?: string}>}
 */
async function wsLocalAction(meta, action, options) {
    if (typeof CowProjectSource === 'undefined' || typeof CowProjectSource.act !== 'function') {
        return { ok: false, code: 'feature_unavailable', message: '' };
    }
    // A card the server rebuilt from history already knows whether the file is
    // still there (task 9.6). Asking the host anyway would report "not found"
    // from a *different* project if one with a same-named file is open, which is
    // a more confusing way to say the same thing.
    if (wsLocalCardIsGone(meta)) return { ok: false, code: 'not_found', message: '' };
    const path = wsEditTargetPath(meta);
    if (!path) return { ok: false, code: 'invalid_request', message: '' };
    try {
        const reply = await CowProjectSource.act(action, path,
            Object.assign({}, options || {}, wsLocalCardScope(meta)));
        if (reply && reply.ok === false) return reply;
        return Object.assign({ ok: true }, reply || {});
    } catch (e) {
        return { ok: false, code: (e && e.code) || 'device_error', message: String((e && e.message) || '') };
    }
}

/** True when a replayed card already knows the file is no longer there. */
function wsLocalCardIsGone(meta) {
    return !!meta && meta.local === true && meta.resolution === 'missing';
}

/**
 * The project a card belongs to, when it is a project other than any local one.
 *
 * Only a replayed card carries `workspace_id`; a live one has none and keeps the
 * binding's own project, which is the project it was just produced in.
 */
function wsLocalCardScope(meta) {
    return (meta && meta.workspace_id) ? { expectedWorkspaceId: String(meta.workspace_id) } : {};
}

/**
 * Why a system action on a local file did not happen, in the user's language.
 *
 * Each refusal is a different situation with a different fix, so they are told
 * apart: a file that is gone, a project whose authorization was revoked, a
 * machine with nothing that opens this kind of file, and a build that cannot
 * act on local files at all are four different sentences. The host's own words
 * are appended when they add something the sentence does not have (the OS
 * explaining *which* application is missing, for instance).
 */
function wsLocalActionMessage(refusal) {
    const code = (refusal && refusal.code) || '';
    const detail = String((refusal && refusal.message) || '').trim();
    const suffix = detail && detail !== code ? ` (${detail})` : '';
    if (code === 'not_found') return t('ws_local_file_gone');
    if (code === 'wrong_project') return t('ws_local_other_project');
    if (code === 'changed') return `${t('ws_local_file_changed')}${suffix}`;
    if (code === 'stale_context' || code === 'grant_revoked') return t('ws_local_stale');
    if (code === 'device_offline' || code === 'device_error') return t('ws_local_offline');
    if (code === 'no_application') return `${t('ws_local_no_application')}${suffix}`;
    if (code === 'feature_unavailable' || code === 'no_host') return t('ws_local_action_unavailable');
    if (code === 'permission_denied') return `${t('ws_forbidden')}${suffix}`;
    return `${t('ws_local_action_failed')}${suffix}`;
}

/** A "the file changed under you" question the user can answer. Browser modal. */
function wsLocalConfirm(message) {
    try {
        return window.confirm(message);
    } catch (_) {
        // No dialog available (an exotic frame): refuse rather than copy a
        // version the user has not agreed to.
        return false;
    }
}

/** One save-a-copy attempt. Returns `''` on success, else the refusal code. */
async function wsSaveLocalCopyOnce(meta, expectedMtime, acceptCurrent) {
    const outcome = await wsLocalAction(meta, 'saveAs', {
        expectedMtime: expectedMtime,
        acceptCurrent: acceptCurrent,
    });
    return outcome.ok ? '' : (outcome.code || 'device_error');
}

/**
 * Write a copy of a local file where the user chooses (task 9.4).
 *
 * The version the panel read travels with the request. When the file changed
 * on disk meanwhile the host refuses, and the user is asked -- copying a file
 * they have not seen is the one thing "save a copy" must not do silently. A
 * dismissed save dialog is not an error and gets no message: the user knows.
 */
async function wsSaveLocalCopy(meta) {
    const expected = Number(meta.mtime || 0);
    let code = await wsSaveLocalCopyOnce(meta, expected, false);
    if (code === 'changed') {
        if (!wsLocalConfirm(t('ws_local_save_as_changed'))) return;
        code = await wsSaveLocalCopyOnce(meta, expected, true);
    }
    if (code === '') {
        _wsToast(t('ws_local_saved_as'));
        return;
    }
    if (code === 'cancelled') return;
    _wsToast(wsLocalActionMessage({ code }));
}

function downloadPreviewFile() {
    if (!wsCurrentFile) return;
    if (wsIsLocalFile(wsCurrentFile)) {
        // The bytes are already on this machine: there is nothing to download
        // from the browser. What the button means for a local file is "put a
        // copy somewhere else", which is exactly what the host's save-as does.
        wsSaveLocalCopy(wsCurrentFile);
        return;
    }
    const a = document.createElement('a');
    a.href = wsCurrentFile.raw_url || wsCurrentFile.preview_url;
    a.download = wsCurrentFile.file_name || wsCurrentFile.name || '';
    document.body.appendChild(a);
    a.click();
    a.remove();
}

/** Show a local file in the system's file manager (task 9.4). */
function revealPreviewFile() {
    if (!wsCurrentFile) return;
    if (!wsIsLocalFile(wsCurrentFile)) return;
    wsLocalAction(wsCurrentFile, 'reveal').then((outcome) => {
        if (outcome.ok) _wsToast(t('ws_local_revealed'));
        else _wsToast(wsLocalActionMessage(outcome));
    });
}

function copyPreviewPath() {
    if (!wsCurrentFile) return;
    if (wsIsLocalFile(wsCurrentFile)) {
        // The *full local path*, copied on this machine by the host: a local
        // file's panel path is project-relative, and the user asking to copy a
        // path wants the one their terminal and their editor understand. The
        // path never travels through the page (task 9.4).
        wsLocalAction(wsCurrentFile, 'copyPath').then((outcome) => {
            if (outcome.ok) _wsToast(t('ws_local_copied'));
            else _wsToast(wsLocalActionMessage(outcome));
        });
        return;
    }
    const path = wsCurrentFile.abs_path || wsCurrentFile.path || '';
    copyToClipboard(path).then(() => {
        const btn = document.getElementById('ws-btn-copy');
        const icon = btn && btn.querySelector('i');
        if (icon) {
            icon.className = 'fas fa-check';
            setTimeout(() => { icon.className = 'fas fa-link'; }, 1500);
        }
    });
}

// =====================================================================
// Preview editor
// =====================================================================
function wsIsEditable(meta) {
    if (!meta || meta.is_dir) return false;
    return WS_EDITABLE.has(meta.kind || wsKindOf(meta.file_name || meta.name || meta.path));
}

/**
 * Path to send to the read/write API. The absolute path is unambiguous, which
 * matters for files the panel reaches outside the session's workspace root
 * (memory / knowledge assets while a project is open).
 */
function wsEditTargetPath(meta) {
    return meta.abs_path || meta.path || meta.rel_path || '';
}

function wsEditorTextarea() {
    return document.getElementById('ws-editor');
}

function wsEditorDirty() {
    const ta = wsEditorTextarea();
    return wsEditing && !!ta && ta.value !== wsEditBaseline;
}

/** Forget the editor's state, leaving what is on screen to the caller. */
function wsDiscardEditState() {
    wsEditing = false;
    wsEditBaseline = '';
    wsEditBaseMtime = null;
}

/**
 * Gate an action that would throw away the editor's contents.
 *
 * @param {function} next - run once the user agrees to discard the edits, and
 *   responsible for whatever replaces the editor. It runs with edit mode
 *   already off, so it must not be a function that bails out when not editing.
 * @returns {boolean} true when there is nothing to lose and the caller may
 *   proceed immediately; false once the confirmation has been put on screen.
 */
function wsGuardUnsaved(next) {
    if (!wsEditorDirty()) return true;
    showConfirmDialog({
        title: t('ws_edit_discard_title'),
        message: t('ws_edit_discard_msg'),
        okText: t('ws_edit_discard_ok'),
        onConfirm: () => {
            wsDiscardEditState();
            next();
        },
    });
    return false;
}

/**
 * Why the server refused to make a file editable. Truncation is reported first:
 * a partial read can also split a multi-byte character and so come back lossy,
 * but the size is the reason the user needs to hear.
 */
function wsUneditableReason(data) {
    if (data.truncated) return 'ws_edit_too_large';
    if (data.lossy) return 'ws_edit_encoding';
    return 'ws_edit_unsupported';
}

/** Load the file's current text into an editable text area. */
async function startPreviewEdit() {
    if (wsEditing || !wsIsEditable(wsCurrentFile)) return;
    const target = wsCurrentFile;
    const body = document.getElementById('ws-preview-content');
    if (!body) return;
    body.innerHTML = `<div class="workspace-empty"><i class="fas fa-spinner fa-spin"></i></div>`;

    let data;
    try {
        data = await wsApi(`/api/workspace/read?path=${encodeURIComponent(wsEditTargetPath(target))}`);
    } catch (e) {
        _wsToast(`${t('ws_edit_load_failed')}: ${wsErrorMessage(e)}`);
        await wsRenderPreview(target);
        return;
    }
    // The user may have navigated away while the request was in flight.
    if (wsCurrentFile !== target) return;
    if (!data.editable) {
        _wsToast(t(wsUneditableReason(data)));
        await wsRenderPreview(target);
        return;
    }

    wsEditing = true;
    wsEditBaseMtime = data.mtime;
    // Read the baseline back out of the text area rather than using the response
    // text: a text area normalizes CRLF to LF in its value, so a CRLF file would
    // compare as modified from the moment it loaded.
    wsEditBaseline = wsMountEditor(body, data.content).value;
    wsRenderPreviewTitle();
    wsUpdateHeaderActions();
}

/** @returns {HTMLTextAreaElement} the text area now holding the file. */
function wsMountEditor(body, content) {
    body.innerHTML = '';
    const ta = document.createElement('textarea');
    ta.id = 'ws-editor';
    ta.className = 'ws-editor';
    ta.spellcheck = false;
    ta.value = content;
    body.appendChild(ta);

    ta.addEventListener('input', wsRenderPreviewTitle);
    ta.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
            // Save in place, the way an editor does. The Save button instead
            // returns to the rendered preview.
            e.preventDefault();
            savePreviewEdit({ keepEditing: true });
        } else if (e.key === 'Escape') {
            e.preventDefault();
            cancelPreviewEdit();
        } else if (e.key === 'Tab') {
            // Otherwise Tab leaves the text area, which is never what indenting
            // a line of code is meant to do.
            e.preventDefault();
            wsInsertAtCursor(ta, '    ');
        }
    });
    ta.focus();
    return ta;
}

function wsInsertAtCursor(ta, text) {
    const { selectionStart: start, selectionEnd: end } = ta;
    ta.value = ta.value.slice(0, start) + text + ta.value.slice(end);
    ta.selectionStart = ta.selectionEnd = start + text.length;
    wsRenderPreviewTitle();
}

/**
 * Write the text area back to disk.
 *
 * @param {object} [opts]
 * @param {boolean} [opts.keepEditing] - stay in the editor after saving.
 * @param {boolean} [opts.force] - save even though the file changed on disk.
 */
async function savePreviewEdit(opts) {
    const { keepEditing = false, force = false } = opts || {};
    const ta = wsEditorTextarea();
    if (!wsEditing || !wsCurrentFile || !ta) return;
    // Ctrl+S bypasses the button's disabled state, and a second save sent
    // before the first reply carries a stale mtime - which would come back as a
    // conflict against our own write.
    if (wsSaving) return;
    // Writing an untouched file would bump its mtime for nothing.
    if (!force && !wsEditorDirty()) {
        if (!keepEditing) await wsExitEdit();
        return;
    }

    const target = wsCurrentFile;
    const content = ta.value;
    const btn = document.getElementById('ws-btn-save');
    wsSaving = true;
    btn?.classList.add('ws-btn-busy');
    try {
        const payload = {
            path: wsEditTargetPath(target),
            content: content,
            session: (typeof sessionId !== 'undefined') ? sessionId : '',
            expected_mtime: force ? null : wsEditBaseMtime,
        };
        // The session's source decides where the save lands (task 9.2). A local
        // project saves through the local source -- authorized, serialised and
        // journaled exactly like the model's own write -- while a backend project
        // keeps the original endpoint. `null` means "no local project here".
        const local = (typeof CowProjectSource !== 'undefined')
            ? await CowProjectSource.write(payload)
            : null;
        let data;
        if (local) {
            data = local;
        } else {
            const res = await fetch('/api/workspace/write', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            data = await res.json();
        }
        if (data.code === 'conflict') {
            showConfirmDialog({
                title: t('ws_edit_conflict_title'),
                message: t('ws_edit_conflict_msg'),
                okText: t('ws_edit_overwrite'),
                onConfirm: () => savePreviewEdit({ keepEditing: keepEditing, force: true }),
            });
            return;
        }
        if (data.status !== 'success') throw new Error(data.message || 'save failed');

        wsEditBaseline = content;
        wsEditBaseMtime = data.mtime;
        target.size = data.size;
        target.mtime = data.mtime;
        // The version on disk is this save's own result, so the "changed under
        // you" note is resolved by definition (task 9.3).
        if (wsStaleFile === wsEditTargetPath(target)) wsStaleFile = '';
        _wsToast(t('ws_edit_saved'));
        if (keepEditing) {
            wsRenderPreviewTitle();
        } else {
            await wsExitEdit();
        }
    } catch (e) {
        _wsToast(`${t('ws_edit_save_failed')}: ${wsErrorMessage(e)}`);
    } finally {
        wsSaving = false;
        btn?.classList.remove('ws-btn-busy');
    }
}

function cancelPreviewEdit() {
    if (!wsEditing) return;
    // Retry through wsExitEdit rather than through this function, which bails
    // out on the very flag the guard clears before retrying.
    if (!wsGuardUnsaved(wsExitEdit)) return;
    wsExitEdit();
}

/** Leave edit mode and show the rendered preview again. */
async function wsExitEdit() {
    wsDiscardEditState();
    wsRenderPreviewTitle();
    wsUpdateHeaderActions();
    if (wsCurrentFile) await wsRenderPreview(wsCurrentFile);
}

// =====================================================================
// Artifact cards in messages
// =====================================================================

/** Build the HTML for a file card. `meta` needs file_name / kind / raw_url. */
function renderFileCard(meta) {
    const name = meta.file_name || meta.name || '';
    const kind = meta.kind || wsKindOf(name);
    const relPath = meta.rel_path || meta.path || '';
    // Skip the path when it adds nothing beyond the file name already shown.
    const sub = [relPath === name ? '' : relPath, wsFormatSize(meta.size)]
        .filter(Boolean).join(' · ');
    const payload = escapeHtml(JSON.stringify({
        file_name: name,
        rel_path: meta.rel_path || meta.path || '',
        abs_path: meta.abs_path || '',
        kind: kind,
        size: meta.size || 0,
        mtime: meta.mtime || 0,
        raw_url: meta.raw_url || '',
        preview_url: meta.preview_url || '',
        local: wsIsLocalFile(meta),
        previewable: meta.previewable !== false && WS_PREVIEWABLE.has(kind),
        // Which project produced this file (task 9.6). A replayed card keeps the
        // project it came from, and the panel refuses to act on it while a
        // *different* project is open -- the relative path would otherwise be
        // resolved against the project open now, which may hold a same-named
        // file that is not the one the run produced.
        workspace_id: meta.workspace_id || '',
        resolution: meta.resolution || 'ok',
    }));
    const canPreview = meta.previewable !== false && WS_PREVIEWABLE.has(kind);
    // A local card's "download" would fetch a server URL for a file that is on
    // this machine -- there is no such URL. The card carries what actually
    // works for a local file instead (task 9.4): open it with the system's
    // application, show it in the file manager, copy its real path, or write a
    // copy elsewhere.
    const actions = wsIsLocalFile(meta)
        ? `<div class="file-card-btn" data-action="local-open" title="${escapeHtml(t('ws_open_system'))}"><i class="fas fa-up-right-from-square"></i></div>
            <div class="file-card-btn" data-action="local-reveal" title="${escapeHtml(t('ws_reveal_file'))}"><i class="fas fa-folder-open"></i></div>
            <div class="file-card-btn" data-action="local-copy-path" title="${escapeHtml(t('ws_local_copy_path'))}"><i class="fas fa-link"></i></div>
            <div class="file-card-btn" data-action="local-save-as" title="${escapeHtml(t('ws_save_as'))}"><i class="fas fa-download"></i></div>`
        : `<div class="file-card-btn" data-action="download" title="${escapeHtml(t('ws_download'))}"><i class="fas fa-download"></i></div>`;
    return `<div class="file-card" data-file='${payload}'>
        <i class="file-card-icon ${wsIconClass(kind)}"></i>
        <div class="file-card-info">
            <div class="file-card-name">${escapeHtml(name)}</div>
            ${sub ? `<div class="file-card-sub">${escapeHtml(sub)}</div>` : ''}
        </div>
        <div class="file-card-actions">
            ${canPreview ? `<div class="file-card-btn" data-action="preview" title="${escapeHtml(t('ws_preview'))}"><i class="fas fa-eye"></i></div>` : ''}
            ${actions}
        </div>
    </div>`;
}

/** Append an artifact card to a live bot bubble and remember it for auto-open. */
function appendArtifactCard(container, item) {
    if (!container) return;
    const existing = container.querySelector(`[data-artifact-path="${CSS.escape(item.abs_path || '')}"]`);
    if (existing) return;
    const wrap = document.createElement('div');
    wrap.className = 'file-card-list';
    wrap.dataset.artifactPath = item.abs_path || '';
    wrap.innerHTML = renderFileCard(item);
    container.appendChild(wrap);
    wsTurnArtifacts.push(item);
}

function resetTurnArtifacts() {
    wsTurnArtifacts = [];
}

/**
 * Auto-open policy: only when the turn produced exactly one previewable
 * artifact, only while the user hasn't dismissed the panel by hand, and never
 * over an open editor - the file cards stay in the message either way.
 */
function maybeAutoOpenArtifact() {
    const items = wsTurnArtifacts.filter(a => a.previewable);
    wsTurnArtifacts = [];
    if (wsAutoOpenSuppressed || wsEditing || items.length !== 1) return;
    openInPreview(items[0]);
}

/**
 * Render the artifact cards of a history message. The list is built by the
 * backend from the persisted write/edit steps, since only it knows the
 * workspace root and which files still exist.
 */
function renderArtifactCards(artifacts) {
    if (!Array.isArray(artifacts) || !artifacts.length) return '';
    return artifacts.map(item =>
        `<div class="file-card-list" data-artifact-path="${escapeHtml(item.abs_path || '')}">
            ${renderFileCard(item)}
        </div>`
    ).join('');
}

async function wsResolveMeta(meta) {
    if (meta.preview_url && meta.raw_url) return meta;
    const path = meta.abs_path || meta.rel_path || meta.path;
    if (!path) return null;
    return (await wsApi(`/api/workspace/resolve?path=${encodeURIComponent(path)}`)).file;
}

function wsTriggerDownload(url, name) {
    const a = document.createElement('a');
    a.href = url;
    a.download = name || '';
    document.body.appendChild(a);
    a.click();
    a.remove();
}

// Delegate clicks on file cards, inline path chips and workspace links.
// Delegation (rather than per-element listeners) is what keeps this working
// after a streamed message re-renders its innerHTML.
document.addEventListener('click', async (e) => {
    // A closer handler already claimed this click (e.g. the knowledge viewer
    // navigating between its own documents).
    if (e.defaultPrevented) return;

    // Workspace-relative link inside rendered markdown.
    const wsLink = e.target.closest('a[data-ws-path]');
    if (wsLink) {
        e.preventDefault();
        openWorkspaceLink(wsLink.dataset.wsPath);
        return;
    }

    const chip = e.target.closest('.file-chip');
    if (chip && chip.dataset.path) {
        e.preventDefault();
        openInPreview(chip.dataset.path);
        return;
    }

    // Workspace reference chip inside a user message bubble.
    const ref = e.target.closest('[data-ws-open]');
    if (ref) {
        e.preventDefault();
        openInPreview(ref.dataset.wsOpen);
        return;
    }

    const card = e.target.closest('.file-card');
    if (!card) return;
    e.preventDefault();
    let meta;
    try { meta = JSON.parse(card.dataset.file); } catch (_) { return; }

    const action = e.target.closest('[data-action]')?.dataset.action;
    if (action === 'download') {
        if (meta.raw_url) {
            wsTriggerDownload(meta.raw_url, meta.file_name);
            return;
        }
        try {
            const full = await wsResolveMeta(meta);
            if (full) wsTriggerDownload(full.raw_url, full.name);
        } catch (_) {}
        return;
    }
    // The card of a file on this machine (task 9.4). Handled before the preview
    // branch so a card action never opens the panel instead of acting.
    if (action && action.indexOf('local-') === 0) {
        if (action === 'local-save-as') {
            await wsSaveLocalCopy(meta);
            return;
        }
        const kind = { 'local-open': 'open', 'local-reveal': 'reveal', 'local-copy-path': 'copyPath' }[action];
        if (!kind) return;
        const outcome = await wsLocalAction(meta, kind);
        if (!outcome.ok) {
            _wsToast(wsLocalActionMessage(outcome));
            return;
        }
        if (kind === 'open') _wsToast(t('ws_local_opened'));
        else if (kind === 'reveal') _wsToast(t('ws_local_revealed'));
        else _wsToast(t('ws_local_copied'));
        return;
    }
    openInPreview(meta.preview_url ? meta : (meta.abs_path || meta.rel_path));
});

// =====================================================================
// Inline path chips: turn local paths mentioned in text into clickable chips
// =====================================================================
const WS_PATH_EXTS = Object.values(WS_KIND_BY_EXT).flat().join('|');
// Absolute (/Users/..., C:\...), home-relative (~/cow/...) or workspace-relative
// (websites/report.html) paths, always anchored on a known file extension and
// containing at least one separator, which keeps prose like "see report.html"
// from turning into chips. Excluding `:` and `/` before the match is what stops
// the tail of an http(s) URL from being picked up.
const WS_PATH_RE = new RegExp(
    '(^|[^\\w/\\\\.~:-])((?:~\\/|\\/|[A-Za-z]:\\\\)?(?:[\\w.\\-\\u4e00-\\u9fa5]+[\\/\\\\])+[\\w.\\-\\u4e00-\\u9fa5]+\\.(?:'
    + WS_PATH_EXTS + '))(?=$|[^\\w.\\-\\u4e00-\\u9fa5]|$)',
    'gi'
);

function _buildFileChip(path) {
    const name = path.split(/[\\/]/).pop();
    const kind = wsKindOf(name);
    return `<span class="file-chip" data-path="${escapeHtml(path)}" title="${escapeHtml(path)}">` +
        `<i class="${wsIconClass(kind)}"></i>${escapeHtml(name)}</span>`;
}

/**
 * Rewrite bare file paths in already-rendered markdown into chips.
 * Only touches text nodes outside <pre>/<code>/<a> so code samples and
 * existing links stay untouched.
 */
function injectFileChips(html) {
    if (!html || !html.includes('.')) return html;

    // Split on tags; track whether we're inside a region we must not rewrite.
    let depthSkip = 0;
    return html.split(/(<[^>]+>)/).map((chunk) => {
        if (chunk.startsWith('<')) {
            const tag = chunk.match(/^<\/?\s*([a-zA-Z0-9]+)/);
            const name = tag ? tag[1].toLowerCase() : '';
            if (['pre', 'code', 'a', 'img', 'video', 'audio'].includes(name)) {
                if (chunk.startsWith('</')) depthSkip = Math.max(0, depthSkip - 1);
                else if (!chunk.endsWith('/>')) depthSkip += 1;
            }
            return chunk;
        }
        if (depthSkip > 0 || !chunk.trim()) return chunk;
        return chunk.replace(WS_PATH_RE, (match, lead, path) =>
            path.includes('://') ? match : lead + _buildFileChip(path)
        );
    }).join('');
}

// =====================================================================
// Workspace links inside rendered markdown
// =====================================================================
/**
 * Decide whether an href points at a workspace file rather than the web.
 *
 * Agent replies cite their own files with a workspace-relative markdown link
 * (`[title](knowledge/x.md)`). The browser would resolve those against the
 * console URL and open a 404 in a new tab, so they need routing to the
 * preview panel instead.
 *
 * @returns {string|null} the cleaned workspace path, or null if not one.
 */
function wsWorkspaceHref(href) {
    if (!href) return null;
    // A scheme (http, mailto, file, data), a protocol-relative host, an
    // in-page anchor or a site-absolute path is never a workspace file.
    if (/^[a-zA-Z][\w+.-]*:/.test(href)) return null;
    if (href.startsWith('//') || href.startsWith('#') || href.startsWith('/')) return null;

    let path = href.split('#')[0].split('?')[0].trim();
    // markdown-it percent-encodes non-ASCII hrefs; the API wants them raw.
    try { path = decodeURI(path); } catch (_) {}
    if (!path) return null;
    // Require a known extension so prose links stay untouched.
    return WS_EXT_KIND[(path.split('.').pop() || '').toLowerCase()] ? path : null;
}

/**
 * Open a workspace file referenced by a link in a rendered message.
 * Agent links are occasionally relative to the citing document rather than to
 * the workspace root, so fall back to a filename search before giving up.
 */
async function openWorkspaceLink(path) {
    // The panel lives in the chat view, so a link clicked from elsewhere (the
    // knowledge reader, a memory file) would otherwise open out of sight.
    if (typeof navigateTo === 'function' && currentView !== 'chat') navigateTo('chat');

    try {
        const data = await wsApi(`/api/workspace/resolve?path=${encodeURIComponent(path)}`);
        openInPreview(data.file);
        return;
    } catch (_) { /* fall through to the name search */ }

    const name = path.split('/').pop();
    try {
        const data = await wsApi(`/api/workspace/search?q=${encodeURIComponent(name)}&limit=10`);
        const hit = (data.results || []).find(r => !r.is_dir && r.name === name);
        if (hit) {
            openInPreview(hit);
            return;
        }
    } catch (_) {}

    openWorkspacePanel('preview');
    switchWorkspaceTab('preview');
    wsSetPreviewEmpty(`${t('ws_link_not_found')}: ${path}`, 'fa-triangle-exclamation');
}

// =====================================================================
// File manager tab
// =====================================================================
function refreshWorkspaceTree() {
    if (wsTrashMode) {
        loadWorkspaceTrash();
        return;
    }
    const input = document.getElementById('ws-search-input');
    if (input) input.value = '';
    wsSearchMode = false;
    loadWorkspaceDir(wsCurrentDir);
}

/** Switching the active Agent moves the file panel to that Agent's own folder.
 *  Land there again, but only if the panel is already open — never pop it open
 *  on a switch. */
function resetWorkspaceToAgentRoot() {
    // A listing in flight for the old Agent belongs to the old scope.
    const epoch = wsNewScopeEpoch();
    // The new Agent may well be one the caller can browse, so the previous
    // Agent's fallback must not outlive the switch — and the landing path is the
    // new Agent's folder, never the old fallback's.
    wsAgentOverride = '';
    // Another Agent has another bin, and the last drop's report described a
    // folder of the previous Agent.
    wsResetUploadAndTrashState();
    // A tenant-shared Agent lands on the caller's own `user/<user id>` folder,
    // so the path cannot be derived before that id is known. A switch that
    // happens meanwhile supersedes this one (see `wsScopeEpoch`).
    wsOwnUserId().then(() => {
        if (wsScopeStale(epoch)) return;
        wsCurrentDir = wsAgentLandingPath();
        if (wsPanelOpen) refreshWorkspaceTree();
    });
}

// =====================================================================
// Access fallback: the caller's own private Agent
// =====================================================================
/**
 * Whether a workspace failure means "this Agent's directory is not yours".
 *
 * `_workspace_request_scope` refuses with 403/404 an Agent bound to another
 * tenant, one privately owned by somebody else, and a session the caller does
 * not own, so both statuses are the permission case the panel falls back from.
 * Once the fallback Agent is in use this is false: a second refusal would
 * otherwise loop the reload.
 */
function wsShouldFallBackToOwnAgent(e) {
    return !wsAgentOverride && wsIsRefusal(e);
}

/** True for the refusals `_workspace_request_scope` answers with (403/404). */
function wsIsRefusal(e) {
    const status = e && e.status;
    return status === 403 || status === 404;
}

// =====================================================================
// Identity: whose files the panel is showing
// =====================================================================
/**
 * The caller's own end-user id, or '' when this deployment has none.
 *
 * `/auth/me` is the projection the console's own account menu reads, so this is
 * the same id the file surface keys `user/<user id>` by. Asked once per page:
 * the answer cannot change without a login, which reloads the page, and an
 * empty answer is a fact about the deployment rather than a failure to retry.
 */
async function wsOwnUserId() {
    if (wsOwnUserIdCache !== undefined) return wsOwnUserIdCache;
    try {
        const res = await fetch('/auth/me', { credentials: 'same-origin', cache: 'no-store' });
        const data = await res.json();
        const user = (data && data.status === 'success' && data.user) || {};
        wsOwnUserIdCache = user.id ? String(user.id) : '';
    } catch (_) {
        wsOwnUserIdCache = '';
    }
    return wsOwnUserIdCache;
}

/**
 * Ask the server to create this caller's own folder inside the addressed Agent.
 *
 * That folder is created by a write, so a member who has never filed anything
 * would land on a path that does not exist yet. The user id is the server's own
 * reading of the verified identity — the body names only the Agent — so this
 * can never create, or even address, another member's subtree. `session` and
 * `agent` travel the same way they do for every other workspace call, so the
 * server applies the same tenant-binding, private-owner and owned-session
 * checks it applies to the panel's reads.
 */
async function wsEnsureUserDir() {
    const body = {};
    if (typeof sessionId !== 'undefined' && sessionId) body.session = sessionId;
    const agentId = wsScopedAgentId();
    if (agentId) body.agent = agentId;
    const res = await fetch('/api/workspace/user-dir', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    const data = await res.json();
    if (data.status !== 'success') throw new Error(data.message || 'ensure failed');
    return true;
}

/**
 * The caller's own private Agent id, or '' when they own none.
 *
 * `/api/agents?view=personal` already filters server-side to the Agents this
 * member owns, so no ownership decision is made here. The member's default
 * Agent is preferred when it is one of them — that is the Agent the console
 * sends them to — then whichever private Agent they own.
 */
async function wsOwnPrivateAgentId() {
    if (wsOwnAgentId !== undefined) return wsOwnAgentId;
    try {
        const res = await fetch('/api/agents?view=personal', { cache: 'no-store' });
        const data = await res.json();
        const agents = (data && data.status === 'success' && Array.isArray(data.agents))
            ? data.agents : [];
        const ids = agents.map(a => a.id).filter(Boolean);
        const preferred = data && data.default_agent_id;
        const resolved = (preferred && ids.includes(preferred)) ? preferred : (ids[0] || '');
        // Only a positive answer is remembered: a member with no private Agent
        // yet must keep working after they create one.
        if (resolved) wsOwnAgentId = resolved;
        return resolved;
    } catch (_) {
        return '';
    }
}

/**
 * Point the panel at the caller's own private Agent's folder, and say so — the
 * folder about to be listed belongs to a different Agent than the one asked
 * for, which is otherwise indistinguishable from a wrong folder.
 *
 * @returns {Promise<boolean>} whether a fallback Agent is now in effect.
 */
async function wsFallBackToOwnAgent() {
    const agentId = await wsOwnPrivateAgentId();
    if (!agentId) return false;
    wsAgentOverride = agentId;
    if (typeof _wsToast === 'function') _wsToast(t('ws_fallback_own_agent'));
    return true;
}

/** One tree request for a workspace-relative directory. */
function wsTreeRequest(relPath) {
    return wsApi(`/api/workspace/tree?path=${encodeURIComponent(relPath || '')}`);
}

/**
 * Load a directory into the file list. `relPath` is relative to the workspace
 * root; empty means the root itself.
 *
 * Two ways a listing can be missing are survived:
 *
 *  - the Agent's own folder is refused (403/404: another tenant's Agent,
 *    somebody else's private Agent, a session the caller does not own). The
 *    panel then shows the caller's own private Agent's folder instead of a dead
 *    end, and the retry asks for the *new* Agent's folder — it is a different
 *    directory, so the path is re-derived rather than reused.
 *  - the Agent's own folder is simply not there. A root that keeps its Agents
 *    elsewhere (a project the session opened, a legacy single-Agent install)
 *    has no `agents/<id>/`, and landing on the root itself is then the honest
 *    answer. That fallback is only offered for the landing path — a directory
 *    the user navigated into is reported as the error it is.
 */
async function loadWorkspaceDir(relPath) {
    const list = document.getElementById('ws-file-list');
    if (!list) return;
    // A directory listing is never the bin. Leaving the bin by any route — the
    // back button, a drop's own refresh, the breadcrumb — puts the toolbar back
    // to the directory's controls, so the two modes cannot be half-applied.
    if (wsTrashMode) {
        wsTrashMode = false;
        wsUpdateToolbarState();
    }
    list.innerHTML = `<div class="workspace-empty"><i class="fas fa-spinner fa-spin"></i></div>`;
    const epoch = wsScopeEpoch;
    const landing = !!relPath && relPath === wsAgentLandingPath();
    // A tenant-shared Agent opens on the caller's own `user/<user id>` folder,
    // which only a write creates. When that landing is missing, the Agent's own
    // folder is the honest next answer and the workspace root the last one — the
    // same degradation a private Agent already has, one level deeper.
    const ownUserLanding = landing && !!wsOwnUserDirPath(wsScopedAgentId());
    const candidates = landing
        ? (ownUserLanding ? [relPath, wsAgentDirPath(wsScopedAgentId()), ''] : [relPath, ''])
        : [relPath || ''];
    let data = null;
    let failure = null;
    // One landing asks about the caller's private Agent at most once: a second
    // candidate runs into the same refusal, so probing again would only ask the
    // same question and repeat the answer. Creating the caller's own folder is
    // asked for at most once for the same reason.
    let fallbackTried = false;
    let ensureTried = false;
    for (const candidate of candidates) {
        try {
            data = await wsTreeRequest(candidate);
            break;
        } catch (e) {
            failure = e;
            // The caller's own folder is absent rather than refused: have the
            // server create it (empty, idempotent) and ask once more before
            // treating the landing as missing. Only the landing itself is
            // retried — the candidates after it are a different answer, not a
            // second attempt at the same question.
            if (candidate === relPath && ownUserLanding && !ensureTried && !wsIsRefusal(e)) {
                ensureTried = true;
                let created = false;
                try {
                    created = await wsEnsureUserDir();
                } catch (_) {
                    created = false;
                }
                if (created) {
                    try {
                        data = await wsTreeRequest(candidate);
                        break;
                    } catch (e2) {
                        failure = e2;
                    }
                }
            }
            if (fallbackTried || !wsShouldFallBackToOwnAgent(e)) continue;
            fallbackTried = true;
            if (!(await wsFallBackToOwnAgent())) continue;
            try {
                data = await wsTreeRequest(landing ? wsAgentLandingPath() : candidate);
                break;
            } catch (e2) {
                failure = e2;
            }
        }
    }
    // The Agent, session or account changed while this was in flight: the answer
    // describes a folder the reader has left, so it is dropped, not rendered.
    if (wsScopeStale(epoch)) return;
    if (!data) {
        // A landing on the Agent's own folder that stays refused is a permission
        // problem, and says so in the reader's language: the raw 404 behind it
        // ("agent not found") is about the API's roster, not about the folder
        // the reader asked for.
        const message = (landing && wsIsRefusal(failure))
            ? t('ws_agent_forbidden')
            : wsErrorMessage(failure);
        list.innerHTML = `<div class="workspace-empty">
            <i class="fas fa-triangle-exclamation"></i><span>${escapeHtml(message)}</span></div>`;
        return;
    }
    // The source the panel is currently showing (task 9.2). A local project has
    // no absolute root to name: its location is the user's own business and the
    // shell never hands it over, so the crumb shows the label they chose instead.
    wsCurrentSource = data.source === 'desktop' ? 'desktop' : 'backend';
    wsCurrentDir = data.path || '';
    wsCurrentRoot = wsCurrentSource === 'desktop' ? '' : (data.root || wsCurrentRoot);
    wsSearchMode = false;
    // Browsing leaves search mode; drop the stale query from the box.
    const searchBox = document.getElementById('ws-search-input');
    if (searchBox && searchBox.value) searchBox.value = '';
    renderWorkspaceBreadcrumb(wsCurrentDir);
    renderWorkspaceEntries(data.entries, data.truncated);
}

function renderWorkspaceBreadcrumb(relPath) {
    const bar = document.getElementById('ws-breadcrumb');
    if (!bar) return;
    const parts = (relPath || '').split('/').filter(Boolean);
    // At the root, show the root's absolute path beside the house so the user
    // knows which directory the panel is anchored to. When navigated inside,
    // the deeper crumbs already convey location, so the house stays icon-only.
    const atRoot = parts.length === 0;
    const rootText = wsCurrentRoot || (wsCurrentSource === 'desktop' ? wsLocalRootLabel() : '');
    const rootLabel = atRoot && rootText
        ? ` <span class="crumb-root">${escapeHtml(rootText)}</span>`
        : '';
    // A local project is where the "does anything leave this machine?" question
    // is actually asked, so the answer belongs on this crumb (task 9.7): files
    // stay here, tool outputs/excerpts/errors/metadata go to the server, and a
    // whole file goes up only on an explicit upload. The label alone would leave
    // the user to guess.
    const rootTooltip = wsCurrentSource === 'desktop'
        ? `${rootText}${rootText ? ' — ' : ''}${t('ws_local_data_flow')}`
        : rootText;
    const crumbs = [`<span class="crumb" data-ws-dir="" data-tooltip="${escapeHtml(rootTooltip)}"><i class="fas fa-house"></i>${rootLabel}</span>`];
    let acc = '';
    parts.forEach((p) => {
        acc = acc ? `${acc}/${p}` : p;
        crumbs.push('<span class="sep">/</span>');
        crumbs.push(`<span class="crumb" data-ws-dir="${escapeHtml(acc)}">${escapeHtml(p)}</span>`);
    });
    bar.innerHTML = crumbs.join('');
}

function renderWorkspaceEntries(entries, truncated) {
    const list = document.getElementById('ws-file-list');
    if (!list) return;
    if (!entries || entries.length === 0) {
        list.innerHTML = `<div class="workspace-empty"><i class="fas fa-folder-open"></i>
            <span>${escapeHtml(t('ws_empty_dir'))}</span></div>`;
        return;
    }
    const rows = entries.map(entry => {
        const meta = entry.is_dir ? '' : wsFormatSize(entry.size);
        const lock = wsRowLockHTML(entry);
        return `<div class="ws-file-row" ${wsRowAttrs(entry)}>
            <i class="${wsIconClass(entry.kind)}"></i>
            <span class="ws-file-name">${escapeHtml(entry.name)}</span>
            ${lock}
            <span class="ws-file-meta">${escapeHtml(meta)}</span>
            ${wsRowDeleteHTML(entry)}
        </div>`;
    });
    if (truncated) {
        rows.push(`<div class="workspace-empty" style="height:auto;padding:12px;">
            <span>${escapeHtml(t('ws_truncated'))}</span></div>`);
    }
    list.innerHTML = rows.join('');
}

/**
 * Row attributes for a tree/search entry. Everything is draggable into the
 * composer; `data-ws-dir` additionally makes a click navigate rather than
 * preview, since directories have nothing to render.
 *
 * `data-ws-locked` is the server's own verdict on whether this entry may be
 * deleted (``_annotate_deletable``), not a rule re-derived here: a locked row
 * stays readable and previewable, and is simply left out of a deletion.
 */
function wsRowAttrs(entry) {
    const payload = escapeHtml(JSON.stringify(entry));
    const nav = entry.is_dir ? ` data-ws-dir="${escapeHtml(entry.path)}"` : '';
    const locked = entry.deletable === false
        ? ` data-ws-locked="1" data-ws-lock-reason="${escapeHtml(entry.undeletable_reason || '')}"`
        : '';
    return `data-ws-file='${payload}' draggable="true"${nav}`
        + ` data-ws-rel="${escapeHtml(entry.path || '')}"`
        + ` data-ws-size="${entry.is_dir ? 0 : (entry.size || 0)}"`
        + ` data-ws-is-dir="${entry.is_dir ? '1' : '0'}"${locked}`;
}

/** The lock a row the caller may not delete carries, or ''. */
function wsRowLockHTML(entry) {
    if (entry.deletable !== false) return '';
    const reason = wsFailureText(entry.undeletable_reason);
    return `<span class="ws-row-lock" title="${escapeHtml(reason)}">
        <i class="fas fa-lock"></i></span>`;
}

/**
 * The row's own delete control, or '' when the server said it may not be.
 *
 * Per row rather than a tick box plus a toolbar button: a locked row has no
 * action to offer at all, so the reason for its refusal is its lock's title
 * instead of a button that would decline after the attempt, and the row itself
 * says what can be done to it.
 */
function wsRowDeleteHTML(entry) {
    if (entry.deletable === false) return '';
    return `<button type="button" class="ws-row-act ws-row-act-danger"
        data-ws-act="delete" title="${escapeHtml(t('ws_delete'))}">
        <i class="fas fa-trash-can"></i></button>`;
}

function renderWorkspaceSearchResults(results) {
    const list = document.getElementById('ws-file-list');
    if (!list) return;
    if (!results.length) {
        list.innerHTML = `<div class="workspace-empty"><i class="fas fa-magnifying-glass"></i>
            <span>${escapeHtml(t('ws_no_results'))}</span></div>`;
        return;
    }
    list.innerHTML = results.map(entry => `
        <div class="ws-file-row" ${wsRowAttrs(entry)}>
            <i class="${wsIconClass(entry.kind)}"></i>
            <span class="ws-file-name">${escapeHtml(entry.name)}</span>
            ${wsRowLockHTML(entry)}
            <span class="ws-file-path">${escapeHtml(entry.path)}</span>
            ${wsRowDeleteHTML(entry)}
        </div>`).join('');
}

async function runWorkspaceSearch(query) {
    if (!query.trim()) {
        loadWorkspaceDir(wsCurrentDir);
        return;
    }
    const epoch = wsScopeEpoch;
    try {
        const data = await wsApi(`/api/workspace/search?q=${encodeURIComponent(query)}&limit=60`);
        // A hit list from the previous Agent/session/account must not appear in
        // this one (it would name files of a folder the reader has left).
        if (wsScopeStale(epoch)) return;
        wsSearchMode = true;
        renderWorkspaceSearchResults(data.results || []);
    } catch (e) {
        if (wsScopeStale(epoch)) return;
        const list = document.getElementById('ws-file-list');
        if (list) {
            list.innerHTML = `<div class="workspace-empty">
                <i class="fas fa-triangle-exclamation"></i><span>${escapeHtml(wsErrorMessage(e))}</span></div>`;
        }
    }
}

function initWorkspaceFilesTab() {
    const list = document.getElementById('ws-file-list');
    const bar = document.getElementById('ws-breadcrumb');
    const input = document.getElementById('ws-search-input');

    bar?.addEventListener('click', (e) => {
        const crumb = e.target.closest('[data-ws-dir]');
        if (crumb) loadWorkspaceDir(crumb.dataset.wsDir);
    });

    list?.addEventListener('click', (e) => {
        const row = e.target.closest('.ws-file-row');
        if (!row) return;
        // A row's own controls are checked before the row's click, so pressing
        // delete on a file does not also open that file in the preview.
        const act = e.target.closest('.ws-row-act');
        if (act) {
            if (act.dataset.wsAct === 'delete') askWorkspaceDelete(row);
            else if (act.dataset.wsAct === 'restore') restoreTrashRow(row);
            else if (act.dataset.wsAct === 'purge') askWorkspacePurge(row);
            return;
        }
        if (e.target.closest('.ws-row-lock')) return;
        // A bin row addresses a batch entry, not a file on disk: it has nothing
        // to navigate into and nothing to preview.
        if (wsTrashMode) return;
        if (row.dataset.wsDir !== undefined) {
            loadWorkspaceDir(row.dataset.wsDir);
            return;
        }
        list.querySelectorAll('.ws-file-row.active').forEach(el => el.classList.remove('active'));
        row.classList.add('active');
        try { openInPreview(JSON.parse(row.dataset.wsFile)); } catch (_) {}
    });

    list?.addEventListener('dragstart', (e) => {
        const row = e.target.closest('.ws-file-row[data-ws-file]');
        if (!row) return;
        e.dataTransfer.effectAllowed = 'copy';
        e.dataTransfer.setData('application/x-cow-workspace-file', row.dataset.wsFile);
    });

    input?.addEventListener('input', () => {
        clearTimeout(wsSearchTimer);
        const q = input.value;
        wsSearchTimer = setTimeout(() => runWorkspaceSearch(q), 200);
    });
}

// =====================================================================
// Drag a workspace file into the conversation
// =====================================================================
function addWorkspaceRefAttachment(entry) {
    const relPath = entry.path || entry.rel_path || '';
    if (!relPath) return;
    if (pendingAttachments.some(a => a.file_type === 'workspace_ref' && a.file_path === relPath)) return;
    pendingAttachments.push({
        file_path: relPath,
        file_name: entry.name || entry.file_name || relPath.split('/').pop(),
        // Referenced in place; the backend must not treat it as an upload.
        file_type: 'workspace_ref',
        is_dir: !!entry.is_dir,
    });
    renderAttachmentPreview();
}

function initWorkspaceDropTarget() {
    const target = document.getElementById('chat-main');
    if (!target) return;

    const isWorkspaceDrag = (e) =>
        Array.from(e.dataTransfer?.types || []).includes('application/x-cow-workspace-file');

    target.addEventListener('dragover', (e) => {
        if (!isWorkspaceDrag(e)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = 'copy';
        target.classList.add('ws-drop-active');
    });

    // relatedTarget is the node being entered; moving between descendants of the
    // target still fires dragleave, so only clear when the pointer truly left.
    target.addEventListener('dragleave', (e) => {
        if (!target.contains(e.relatedTarget)) target.classList.remove('ws-drop-active');
    });

    // No stopPropagation: the outer #view-chat drop handler owns resetting the
    // upload overlay state, and it ignores drops that carry no files.
    target.addEventListener('drop', (e) => {
        if (!isWorkspaceDrag(e)) return;
        e.preventDefault();
        target.classList.remove('ws-drop-active');
        try {
            addWorkspaceRefAttachment(JSON.parse(e.dataTransfer.getData('application/x-cow-workspace-file')));
        } catch (_) {}
    });
}

// =====================================================================
// @ file references in the chat input
// =====================================================================
let mentionActive = false;
let mentionStart = -1;
let mentionItems = [];
let mentionIndex = 0;
let mentionTimer = null;

function hideMentionMenu() {
    mentionActive = false;
    mentionStart = -1;
    mentionItems = [];
    document.getElementById('mention-menu')?.classList.add('hidden');
}

function renderMentionMenu() {
    const menu = document.getElementById('mention-menu');
    if (!menu) return;
    if (!mentionItems.length) {
        menu.innerHTML = `<div class="mention-empty">${escapeHtml(t('ws_no_results'))}</div>`;
        menu.classList.remove('hidden');
        return;
    }
    menu.innerHTML = mentionItems.map((item, i) => {
        if (item.kind === 'agent') {
            const face = typeof agentAvatarHTML === 'function'
                ? agentAvatarHTML(item, 20)
                : `<i class="fas fa-user"></i>`;
            return `<div class="mention-item ${i === mentionIndex ? 'active' : ''}" data-idx="${i}">
                ${face}
                <span class="m-name">${escapeHtml(item.name)}</span>
                <span class="m-path">${escapeHtml(item.id)}</span>
            </div>`;
        }
        return `<div class="mention-item ${i === mentionIndex ? 'active' : ''}" data-idx="${i}">
            <i class="${wsIconClass(item.kind)}"></i>
            <span class="m-name">${escapeHtml(item.name)}</span>
            <span class="m-path">${escapeHtml(item.path)}</span>
        </div>`;
    }).join('');
    menu.classList.remove('hidden');
}

function matchingAgentMentions(query) {
    // @ addresses one Agent in particular, which only means something once a
    // conversation has more than its owner. A solo chat keeps @ as the file
    // picker it always was.
    if (typeof sharedConversation !== 'function' || !sharedConversation()) return [];
    const q = String(query || '').toLowerCase();
    // The owner is offered alongside its teammates: in a group chat it is one
    // voice among several, and @ is how the user picks it back out after a
    // teammate has been speaking. sessionRoster() already lists it first.
    const roster = typeof sessionRoster === 'function' ? sessionRoster() : [];
    return roster
        .filter(agent => !q || agent.id.toLowerCase().includes(q) || String(agent.name).toLowerCase().includes(q))
        .slice(0, 6)
        .map(agent => ({ kind: 'agent', id: agent.id, name: agent.name, avatar: agent.avatar || '' }));
}

async function updateMentionQuery(query) {
    const agents = matchingAgentMentions(query);
    try {
        const data = await wsApi(`/api/workspace/search?q=${encodeURIComponent(query)}&limit=12`);
        if (!mentionActive) return;
        mentionItems = agents.concat(data.results || []);
        mentionIndex = 0;
        renderMentionMenu();
    } catch (_) {
        if (!mentionActive) return;
        mentionItems = agents;
        mentionIndex = 0;
        if (agents.length) renderMentionMenu();
        else hideMentionMenu();
    }
}

function acceptMention(idx) {
    const item = mentionItems[idx];
    const input = document.getElementById('chat-input');
    if (!item || !input) return;
    const before = input.value.slice(0, mentionStart);
    const after = input.value.slice(input.selectionStart);
    if (item.kind === 'agent') {
        // Write the name, not the id: the mention is addressed to a colleague
        // and should read like one. The server resolves either form.
        const inserted = `@${item.name || item.id} `;
        input.value = before + inserted + after;
        input.selectionStart = input.selectionEnd = before.length + inserted.length;
        if (typeof addTeamMember === 'function') addTeamMember(item.id);
    } else {
        addWorkspaceRefAttachment(item);
        // Drop the "@query" fragment: the file travels as an attachment, not as text.
        input.value = before + after;
        input.selectionStart = input.selectionEnd = before.length;
    }
    hideMentionMenu();
    input.focus();
    input.dispatchEvent(new Event('input'));
}

function initMention() {
    const input = document.getElementById('chat-input');
    const menu = document.getElementById('mention-menu');
    if (!input || !menu) return;

    input.addEventListener('input', () => {
        const pos = input.selectionStart;
        const before = input.value.slice(0, pos);
        // Trigger on "@" at the start of the input or after whitespace.
        const match = before.match(/(?:^|\s)@([^\s@]*)$/);
        if (!match) {
            if (mentionActive) hideMentionMenu();
            return;
        }
        mentionActive = true;
        mentionStart = pos - match[1].length - 1;
        clearTimeout(mentionTimer);
        const q = match[1];
        mentionTimer = setTimeout(() => updateMentionQuery(q), 150);
    });

    // Capture phase so Enter/arrows are consumed before the send handler.
    input.addEventListener('keydown', (e) => {
        if (!mentionActive || !mentionItems.length) return;
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            e.stopImmediatePropagation();
            mentionIndex = (mentionIndex + 1) % mentionItems.length;
            renderMentionMenu();
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            e.stopImmediatePropagation();
            mentionIndex = (mentionIndex - 1 + mentionItems.length) % mentionItems.length;
            renderMentionMenu();
        } else if (e.key === 'Enter' || e.key === 'Tab') {
            e.preventDefault();
            e.stopImmediatePropagation();
            acceptMention(mentionIndex);
        } else if (e.key === 'Escape') {
            e.preventDefault();
            e.stopImmediatePropagation();
            hideMentionMenu();
        }
    }, true);

    menu.addEventListener('mousedown', (e) => {
        const item = e.target.closest('.mention-item');
        if (!item) return;
        e.preventDefault();
        acceptMention(parseInt(item.dataset.idx, 10));
    });

    document.addEventListener('click', (e) => {
        if (mentionActive && !menu.contains(e.target) && e.target !== input) hideMentionMenu();
    });
}

/** Re-render the JS-generated parts of the panel after a language switch. */
function relocalizeWorkspacePanel() {
    if (!wsCurrentFile) wsSetPreviewEmpty(t('ws_preview_empty'));
    // The toolbar's own labels are markup (`data-i18n-title`); the row actions
    // carry `t()` text written at render time, so they are re-rendered below.
    if (wsUpload) wsRenderUpload();
    if (wsTrashMode) {
        renderWorkspaceTrashHeader(wsTrashBytes);
        renderTrashEntries(wsTrashEntries);
        return;
    }
    if (wsActiveTab === 'files' && !wsSearchMode
        && document.getElementById('ws-file-list')?.childElementCount) {
        loadWorkspaceDir(wsCurrentDir);
    }
}

// Reset the panel when the active session changes. The file panel is scoped to
// a session's Agent, so stale state from the previous session must be dropped
// and, if open, reloaded against the new session's Agent.
function wsOnSessionSwitch() {
    // Nothing in flight for the previous session may render here (see
    // wsScopeEpoch): the panel is scoped to a session's Agent.
    const epoch = wsNewScopeEpoch();
    // The next session may address an Agent the caller *can* browse, so the
    // previous one's fallback must not carry over — and the landing path below
    // is the new session's Agent's own folder, not the old fallback's.
    wsAgentOverride = '';
    wsCurrentRoot = '';
    wsSearchMode = false;
    wsCurrentFile = null;
    wsTurnArtifacts = [];
    // The bin belongs to an Agent too: another Agent's bin is a different bin,
    // and the last drop's report describes a folder that is not this session's.
    wsResetUploadAndTrashState();
    wsDiscardEditState();
    wsUpdateHeaderActions();
    // A shared Agent lands on the caller's own `user/<user id>` folder, so the
    // reload waits for that id; a session switch meanwhile supersedes this one.
    wsOwnUserId().then(() => {
        if (wsScopeStale(epoch)) return;
        wsCurrentDir = wsAgentLandingPath();
        if (!wsPanelOpen) return;
        if (wsActiveTab === 'files') {
            loadWorkspaceDir(wsCurrentDir);
        } else {
            wsSetPreviewEmpty(t('ws_preview_empty'));
        }
    });
}

// =====================================================================
// Drop to upload into the folder the panel is showing
// =====================================================================
//
// A drop on the panel writes into the directory currently listed, folders
// included: a dragged tree is recreated by sending each file's own path below
// that directory. **One file per request**, three at a time — a 200MB file and
// a 5000-file drop are then the same code path, a failure is retryable on its
// own, the server's memory stays flat (it never spools a whole drop to disk
// before writing the first file), and the progress readout is exact: finished
// bytes plus the in-flight requests' own `upload.onprogress`.
//
// The ceilings below are the *experience* layer — they say "this is not going
// to work" before the bytes move. The server checks its own numbers again and
// its answer is the one that counts; a client-side guard is not a guarantee.

/** Files one drop may carry (server-side ceiling is the same number). */
const WS_UPLOAD_MAX_FILES = 5000;
/** Bytes one drop may carry. */
const WS_UPLOAD_MAX_TOTAL_BYTES = 5 * 1024 * 1024 * 1024;
/** Bytes one file may carry; the server refuses a larger one outright. */
const WS_UPLOAD_MAX_FILE_BYTES = 200 * 1024 * 1024;
/** Requests in flight at once. Peak bytes in flight ≈ this × the largest file
 *  the panel accepts; the panel stays usable because the browser, not the
 *  page, buffers the request bodies. */
const WS_UPLOAD_CONCURRENCY = 3;
/** Directory entries read in parallel while walking a dropped tree. */
const WS_UPLOAD_WALK_CONCURRENCY = 12;
/** Depth guard rail for a dropped tree. The entries API hands back a tree, so a
 *  cycle cannot occur; a pathological drop should still not spin forever. */
const WS_UPLOAD_MAX_DEPTH = 32;

//: The drop in progress, or the report of the last one.
let wsUpload = null;
//: Identifies the current drop. A second drop (or a scope change) makes the
//: first one's late answers stale, and they must not paint into the new report.
let wsUploadSeq = 0;

/**
 * The directory the caller may write into for the Agent being browsed, or ''.
 *
 * A tenant-shared Agent keeps each member's files under `user/<user id>`; a
 * private one has no such split (its whole workspace is the owner's). Both
 * answers are the ones the panel already derives for its landing path, so the
 * drop affordance asks the same question rather than a second one. This is a
 * UI hint only: the server decides, and it refuses regardless of what the
 * panel believes.
 */
function wsWritableRootPath() {
    const agentId = wsScopedAgentId();
    if (!agentId) return '';
    if (wsAgentVisibility(agentId) === 'tenant') return wsOwnUserDirPath(agentId);
    return wsAgentDirPath(agentId);
}

/** Whether the folder currently listed is one the caller may write into. */
function wsCanWriteHere() {
    const root = wsWritableRootPath();
    if (!root) return false;
    const dir = wsCurrentDir || '';
    return dir === root || dir.indexOf(root + '/') === 0;
}

/** Show where the drop will land, before it lands. */
function wsShowDropHint() {
    const hint = document.getElementById('ws-drop-hint');
    const text = document.getElementById('ws-drop-hint-text');
    if (!hint || !text) return;
    const writable = wsCanWriteHere();
    hint.classList.toggle('ws-drop-refused', !writable);
    text.textContent = writable
        ? t('ws_upload_drop_here').replace('{dir}', wsCurrentDir || '/')
        : t('ws_upload_out_of_scope');
    hint.classList.remove('hidden');
}

function wsHideDropHint() {
    const hint = document.getElementById('ws-drop-hint');
    if (hint) hint.classList.add('hidden');
}

/** Run `worker` over `items`, at most `limit` at a time, and wait for all. */
async function wsEachLimited(items, limit, worker) {
    let next = 0;
    const run = async () => {
        while (next < items.length) {
            const index = next;
            next += 1;
            await worker(items[index], index);
        }
    };
    const width = Math.max(1, Math.min(limit, items.length));
    const runners = [];
    for (let i = 0; i < width; i += 1) runners.push(run());
    await Promise.all(runners);
}

function wsEntryFile(entry) {
    return new Promise((resolve, reject) => entry.file(resolve, reject));
}

/**
 * Every child of one dropped directory.
 *
 * `readEntries()` answers in chunks and a large folder needs more than one
 * call; an empty array is how the API says "that was the last chunk", not
 * "empty folder".
 */
function wsReadDirectory(dirEntry) {
    return new Promise((resolve, reject) => {
        const reader = dirEntry.createReader();
        const found = [];
        const step = () => {
            reader.readEntries((batch) => {
                if (!batch.length) { resolve(found); return; }
                for (const child of batch) found.push(child);
                step();
            }, reject);
        };
        step();
    });
}

/**
 * Collect one dropped entry into `out` as `{file, rel}`.
 *
 * `rel` is the file's path below the drop, which is what carries a dragged
 * folder's shape: the server joins it onto the destination directory instead of
 * flattening the tree.
 */
async function wsWalkEntry(entry, prefix, out, depth, problems) {
    if (entry.isFile) {
        try {
            const file = await wsEntryFile(entry);
            out.push({ file, rel: prefix ? `${prefix}/${file.name}` : file.name });
        } catch (e) {
            problems.push({ name: prefix || entry.name, code: 'unreadable' });
        }
        return;
    }
    if (!entry.isDirectory) return;
    const here = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (depth >= WS_UPLOAD_MAX_DEPTH) {
        problems.push({ name: here, code: 'too_deep' });
        return;
    }
    let children;
    try {
        children = await wsReadDirectory(entry);
    } catch (e) {
        problems.push({ name: here, code: 'unreadable' });
        return;
    }
    await wsEachLimited(children, WS_UPLOAD_WALK_CONCURRENCY,
        (child) => wsWalkEntry(child, here, out, depth + 1, problems));
}

/**
 * Everything a drop carries, as `{files, problems, unsupported}`.
 *
 * The entries are all converted to handles *synchronously*, before the first
 * `await`: the drag's data store is only readable during the event, and a
 * single `await` before `webkitGetAsEntry()` is what turns a folder into an
 * empty `FileList`.
 */
async function wsCollectDroppedFiles(dataTransfer) {
    const entries = [];
    for (const item of Array.from(dataTransfer.items || [])) {
        if (item.kind !== 'file') continue;
        const entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
        if (!entry) {
            // Without the directory API a dropped folder is indistinguishable
            // from a file, so there is no honest way to walk it. Uploading the
            // top level only would silently lose everything inside.
            return { files: [], problems: [], unsupported: true };
        }
        entries.push(entry);
    }
    const files = [];
    const problems = [];
    await wsEachLimited(entries, WS_UPLOAD_WALK_CONCURRENCY,
        (entry) => wsWalkEntry(entry, '', files, 0, problems));
    return { files, problems, unsupported: false };
}

/** Split a walked drop into what will be sent and what cannot be. */
function wsPartitionDrop(files) {
    const accepted = [];
    const skipped = [];
    let totalBytes = 0;
    for (const item of files) {
        const size = item.file.size || 0;
        if (size > WS_UPLOAD_MAX_FILE_BYTES) {
            skipped.push({ name: item.rel, code: 'too_large', size });
        } else if (accepted.length >= WS_UPLOAD_MAX_FILES) {
            skipped.push({ name: item.rel, code: 'too_many_files', size });
        } else if (totalBytes + size > WS_UPLOAD_MAX_TOTAL_BYTES) {
            skipped.push({ name: item.rel, code: 'too_much_total', size });
        } else {
            accepted.push(item);
            totalBytes += size;
        }
    }
    return { accepted, skipped, totalBytes };
}

/**
 * Send one file. Resolves with `null` on success, or a `{name, code}` failure.
 *
 * `XMLHttpRequest` rather than `fetch` for one reason: `upload.onprogress` is
 * the only way to report bytes actually on the wire, and a
 * 5000-file drop reported by "files finished" alone looks frozen for minutes.
 */
function wsSendOne(upload, index, item) {
    return new Promise((resolve) => {
        const form = new FormData();
        form.append('dir', upload.dir);
        form.append('relative_path', item.rel);
        form.append('file', item.file, item.file.name);

        const xhr = new XMLHttpRequest();
        xhr.open('POST', wsScopedPath('/api/workspace/upload'));
        xhr.upload.addEventListener('progress', (e) => {
            if (!e.lengthComputable || wsUpload !== upload) return;
            // `e.total` is the multipart body, which is the file plus framing;
            // scaling by the ratio reports the file's own progress instead of
            // the envelope's (which never quite reaches its total for small
            // files).
            const size = item.file.size || 0;
            const ratio = e.total ? size / e.total : 1;
            upload.transferred[index] = Math.min(size, Math.round(e.loaded * ratio));
            wsRenderUpload();
        });
        xhr.addEventListener('load', () => {
            let data = {};
            try { data = JSON.parse(xhr.responseText || '{}'); } catch (e) { data = {}; }
            if (xhr.status >= 200 && xhr.status < 300 && data.status === 'success') {
                if (wsUpload === upload) {
                    upload.transferred[index] = item.file.size || 0;
                    upload.doneCount += 1;
                    if (data.renamed) upload.renamed.push(data.path);
                }
                resolve(null);
                return;
            }
            resolve(wsUploadFailure(item, data.code, xhr.status, data.message));
        });
        xhr.addEventListener('error', () =>
            resolve(wsUploadFailure(item, 'network', 0, '')));
        xhr.addEventListener('abort', () =>
            resolve(wsUploadFailure(item, 'network', 0, '')));
        xhr.send(form);
    });
}

/** One failed file, in the shape the report and the retry both read. */
function wsUploadFailure(item, code, status, message) {
    let reason = code || '';
    // A 413 comes from the proxy, not the app: it never reaches the handler, so
    // there is no `code` to read and the distinction has to be made here.
    if (!reason && status === 413) reason = 'too_large';
    if (!reason && (status === 401 || status === 403)) reason = 'forbidden';
    return { name: item.rel, code: reason || 'failed', status: status || 0,
             message: message || '', item };
}

/** A failed file's reason, in the reader's language. */
function wsUploadFailureText(failure) {
    const key = WS_FAILURE_KEYS[failure.code];
    if (key) return t(key);
    return failure.message || failure.code || '';
}

/**
 * Send every accepted file, and keep the report in step.
 *
 * `transferred` is keyed by index and only ever grows, so the byte readout is
 * monotonic even when a request fails part-way: bytes on the wire is what it
 * counts, and a failure does not unsend them.
 */
async function wsSendAll(upload, items) {
    const failures = [];
    await wsEachLimited(items.map((item, index) => ({ item, index })),
        WS_UPLOAD_CONCURRENCY, async ({ item, index }) => {
            if (wsUpload !== upload) return;
            const failure = await wsSendOne(upload, index, item);
            if (failure) failures.push(failure);
            if (wsUpload === upload) wsRenderUpload();
        });
    return failures;
}

function wsUploadProgressBytes(upload) {
    let sum = 0;
    for (const key of Object.keys(upload.transferred)) sum += upload.transferred[key];
    return sum;
}

/** Paint the report. Called on every progress event, so it stays cheap. */
function wsRenderUpload() {
    const box = document.getElementById('ws-upload-progress');
    if (!box || !wsUpload) return;
    const upload = wsUpload;
    const bytes = Math.min(wsUploadProgressBytes(upload), upload.totalBytes);
    const percent = upload.totalBytes ? (bytes / upload.totalBytes) * 100 : 0;
    const failed = upload.failures.length + upload.skipped.length;
    // A drop that lost something never reads 100%: the bar would contradict the
    // list right below it.
    const shown = failed ? Math.min(percent, 99) : percent;

    const title = document.getElementById('ws-upload-title');
    const count = document.getElementById('ws-upload-count');
    const detail = document.getElementById('ws-upload-detail');
    const fill = document.getElementById('ws-upload-fill');
    if (title) title.textContent = t(upload.phase === 'walking'
        ? 'ws_upload_walking' : (failed ? 'ws_upload_partial' : 'ws_upload_sending'));
    if (count) {
        count.textContent = upload.phase === 'walking'
            ? t('ws_upload_found').replace('{count}', String(upload.found))
            : t('ws_upload_bytes')
                .replace('{done}', wsFormatSize(bytes))
                .replace('{total}', wsFormatSize(upload.totalBytes))
                .replace('{done_count}', String(upload.doneCount))
                .replace('{count}', String(upload.queued))
                .replace('{percent}', String(Math.floor(percent)));
    }
    if (fill) fill.style.width = `${shown}%`;
    box.classList.toggle('ws-upload-partial', !!failed);
    if (detail) {
        const lines = upload.skipped.concat(upload.failures)
            .slice(0, 20)
            .map(f => `${f.name} · ${wsUploadFailureText(f)}`);
        const more = upload.skipped.length + upload.failures.length - lines.length;
        if (more > 0) lines.push(t('ws_upload_more').replace('{count}', String(more)));
        if (upload.renamed.length) {
            const shown = upload.renamed.slice(0, 3).join(' · ');
            lines.unshift(t('ws_upload_renamed')
                .replace('{count}', String(upload.renamed.length))
                .replace('{paths}', shown));
        }
        detail.textContent = lines.join('\n');
        detail.classList.toggle('hidden', lines.length === 0);
    }
    const retry = document.getElementById('ws-upload-retry');
    const dismiss = document.getElementById('ws-upload-dismiss');
    if (retry) {
        const retryable = upload.failures.filter(f => f.item).length;
        retry.classList.toggle('hidden', retryable === 0);
        retry.textContent = t('ws_upload_retry').replace('{count}', String(retryable));
    }
    if (dismiss) {
        dismiss.classList.toggle('hidden', upload.phase !== 'done');
        dismiss.textContent = t('ws_upload_dismiss');
    }
    box.classList.remove('hidden');
}

/** The whole flow for one drop. */
async function wsHandleDrop(dataTransfer) {
    if (!wsCanWriteHere()) {
        _wsToast(t('ws_upload_out_of_scope'));
        return;
    }
    const seq = wsUploadSeq + 1;
    wsUploadSeq = seq;
    const upload = wsUpload = {
        seq,
        dir: wsCurrentDir || '',
        phase: 'walking',
        found: 0,
        queued: 0,
        totalBytes: 0,
        doneCount: 0,
        transferred: {},
        renamed: [],
        skipped: [],
        failures: [],
    };
    wsRenderUpload();

    const collected = await wsCollectDroppedFiles(dataTransfer);
    if (wsUpload !== upload) return;
    if (collected.unsupported) {
        wsUpload = null;
        document.getElementById('ws-upload-progress')?.classList.add('hidden');
        _wsToast(t('ws_upload_no_dir_api'));
        return;
    }
    upload.found = collected.files.length;
    const { accepted, skipped, totalBytes } = wsPartitionDrop(collected.files);
    upload.queued = accepted.length;
    upload.totalBytes = totalBytes;
    upload.skipped = collected.problems.concat(skipped);
    upload.phase = 'sending';
    wsRenderUpload();

    if (accepted.length) {
        upload.failures = await wsSendAll(upload, accepted);
    }
    if (wsUpload !== upload) return;
    upload.phase = 'done';
    upload.failures = upload.failures.filter(f => f.item);
    wsRenderUpload();
    // Refresh whatever the panel is showing so the files that did land are on
    // screen; the report above the list explains the ones that did not.
    if (wsTrashMode) loadWorkspaceTrash();
    else loadWorkspaceDir(wsCurrentDir);
}

/** Re-send only the files that failed. Nothing already on disk is re-sent. */
async function retryFailedWorkspaceUploads() {
    const upload = wsUpload;
    if (!upload || upload.phase !== 'done') return;
    const retryable = upload.failures.filter(f => f.item);
    if (!retryable.length) return;
    const items = retryable.map(f => f.item);
    upload.phase = 'sending';
    upload.failures = [];
    // The retry has its own byte budget: the first attempt's bytes were spent
    // on files that are not there, so counting them again would overstate it.
    upload.totalBytes = items.reduce((n, item) => n + (item.file.size || 0), 0);
    upload.queued = items.length;
    upload.doneCount = 0;
    upload.transferred = {};
    wsRenderUpload();
    upload.failures = await wsSendAll(upload, items);
    if (wsUpload !== upload) return;
    upload.phase = 'done';
    upload.failures = upload.failures.filter(f => f.item);
    wsRenderUpload();
    if (wsTrashMode) loadWorkspaceTrash();
    else loadWorkspaceDir(wsCurrentDir);
}

/** Put the report away. The drop it described is over. */
function dismissWorkspaceUpload() {
    wsUpload = null;
    document.getElementById('ws-upload-progress')?.classList.add('hidden');
}

function initWorkspaceUploadDrop() {
    const target = document.getElementById('ws-body-files');
    if (!target) return;
    let depth = 0;
    const hasFiles = (e) => Array.from((e.dataTransfer && e.dataTransfer.types) || [])
        .includes('Files');

    // The chat view above has its own file drop (attachments) and its own
    // "drop files here" overlay. A drop that belongs to the panel is stopped
    // here, all three events, or the user would watch the attachment overlay
    // promise one thing while the panel did another.
    target.addEventListener('dragenter', (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        e.stopPropagation();
        depth += 1;
        wsShowDropHint();
    });
    target.addEventListener('dragover', (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        e.stopPropagation();
        e.dataTransfer.dropEffect = wsCanWriteHere() ? 'copy' : 'none';
    });
    target.addEventListener('dragleave', () => {
        if (depth === 0) return;
        depth -= 1;
        if (depth === 0) wsHideDropHint();
    });
    target.addEventListener('drop', (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        e.stopPropagation();
        depth = 0;
        wsHideDropHint();
        wsHandleDrop(e.dataTransfer);
    });
}

// =====================================================================
// Files tab: per-row deletion and the recycle bin
// =====================================================================

//: Every refusal code the panel can be handed, and the label it shows for it.
//: One table, because the same code arrives from three directions (a row's
//: `undeletable_reason`, a delete response's `failed[]`, a restore's
//: `failed[]`) and they must not disagree on the wording.
const WS_FAILURE_KEYS = {
    outside_own_directory: 'ws_lock_outside',
    agent_internal: 'ws_lock_agent_internal',
    user_container: 'ws_lock_user_container',
    trash_not_targetable: 'ws_lock_trash',
    own_directory_root: 'ws_lock_own_root',
    not_agent_workspace: 'ws_lock_not_agent_workspace',
    unsafe_user_directory: 'ws_lock_outside',
    no_user: 'ws_lock_no_user',
    unsafe_path: 'ws_lock_outside',
    not_found: 'ws_delete_missing',
    not_movable: 'ws_delete_missing',
    not_removable: 'ws_delete_missing',
    batch_not_found: 'ws_trash_batch_gone',
    forbidden: 'ws_forbidden',
    too_large: 'ws_upload_err_too_large',
    too_many_files: 'ws_upload_err_too_many_files',
    too_much_total: 'ws_upload_err_too_much_total',
    too_deep: 'ws_upload_err_too_deep',
    unreadable: 'ws_upload_err_unreadable',
    network: 'ws_upload_err_network',
    incomplete_upload: 'ws_upload_err_incomplete',
    failed: 'ws_delete_failed',
};

/** A refusal code in the reader's language; the raw code if it is unknown. */
function wsFailureText(code) {
    const key = WS_FAILURE_KEYS[code];
    return key ? t(key) : (code || '');
}

//: Whether the panel is showing the recycle bin instead of a directory.
let wsTrashMode = false;
//: The bin as last read, so a restore can say what it acted on.
let wsTrashEntries = [];
//: The bin's total size, as the server reported it.
let wsTrashBytes = 0;
//: The bin's retention window, in days, as the server reported it.
let wsTrashRetention = 0;

function wsRowRel(row) {
    return (row && row.dataset && row.dataset.wsRel) || '';
}

/**
 * Ask before moving one row to the bin, and say what that means.
 *
 * A single row's confirmation still names the counts and the volume, because
 * the message is the same one a batch used to raise (and a *directory* row is
 * one click that carries everything under it).
 */
function askWorkspaceDelete(row) {
    if (!row || row.dataset.wsLocked === '1') return;
    const rel = wsRowRel(row);
    if (!rel) return;
    const size = parseInt(row.dataset.wsSize, 10) || 0;
    const isDir = row.dataset.wsIsDir === '1';
    const message = [
        t('ws_delete_confirm_msg')
            .replace('{count}', '1')
            .replace('{size}', wsFormatSize(size)),
        isDir ? t('ws_delete_confirm_dirs').replace('{count}', '1') : '',
        t('ws_delete_confirm_trash'),
    ].filter(Boolean).join(' ');
    showConfirmDialog({
        title: t('ws_delete_confirm_title'),
        message,
        okText: t('ws_delete_go'),
        onConfirm: () => wsRunDelete([rel]),
    });
}

async function wsRunDelete(targets) {
    try {
        const data = await wsApiPost('/api/workspace/delete', { targets });
        const deleted = data.deleted || [];
        const failed = data.failed || [];
        if (deleted.length) {
            _wsToast(t('ws_delete_done').replace('{count}', String(deleted.length)));
        }
        if (failed.length) _wsToast(wsFailureSummary(failed));
        loadWorkspaceDir(wsCurrentDir);
    } catch (e) {
        _wsToast(wsErrorMessage(e));
    }
}

/** Which controls the files tab is showing: a directory's, or the bin's. */
function wsUpdateToolbarState() {
    const show = (id, visible) => {
        const el = document.getElementById(id);
        if (el) el.classList.toggle('hidden', !visible);
    };
    // Only controls that act on the panel as a whole live here; anything that
    // acts on one row is on that row.
    show('ws-btn-trash', !wsTrashMode);
    show('ws-btn-refresh', !wsTrashMode);
    show('ws-btn-trash-back', wsTrashMode);
    show('ws-btn-purge-all', wsTrashMode);
    const search = document.querySelector('.workspace-search');
    if (search) search.classList.toggle('hidden', wsTrashMode);
}

/**
 * Forget what belonged to the folder the panel was showing.
 *
 * The bin is per Agent, the drop report describes one directory, and a listing
 * is about to be replaced. A drop still in flight carries the scope it started
 * under, so the sequence is bumped to retire it: its late answers are dropped
 * instead of painted into the folder now on screen.
 */
function wsResetUploadAndTrashState() {
    wsTrashMode = false;
    wsTrashEntries = [];
    wsTrashBytes = 0;
    wsTrashRetention = 0;
    wsUploadSeq += 1;
    wsUpload = null;
    document.getElementById('ws-upload-progress')?.classList.add('hidden');
    document.getElementById('ws-drop-hint')?.classList.add('hidden');
    wsUpdateToolbarState();
}

/** A date the panel shows as-is: the bin's own clock, not a relative one. */
function wsShortWhen(seconds) {
    if (!seconds) return '';
    const when = new Date(seconds * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}`
        + ` ${pad(when.getHours())}:${pad(when.getMinutes())}`;
}

/**
 * One line for a set of refusals: the first few reasons, then a count.
 *
 * A per-item table would be the honest shape, but the panel's report area is
 * the file list itself and the items are still there — what a reader needs from
 * a toast is *why*, and the reasons are grouped by code, so three lines cover
 * the cases that actually happen.
 */
function wsFailureSummary(failed) {
    const byCode = new Map();
    for (const item of failed) {
        const code = item.code || 'failed';
        byCode.set(code, (byCode.get(code) || 0) + 1);
    }
    const lines = [];
    for (const [code, count] of byCode) {
        lines.push(`${wsFailureText(code)} ×${count}`);
        if (lines.length >= 3) break;
    }
    return t('ws_delete_failed').replace('{detail}', lines.join(' · '));
}

async function openWorkspaceTrash() {
    wsTrashMode = true;
    wsUpdateToolbarState();
    await loadWorkspaceTrash();
}

function closeWorkspaceTrash() {
    wsTrashMode = false;
    wsTrashEntries = [];
    wsUpdateToolbarState();
    loadWorkspaceDir(wsCurrentDir);
}

async function loadWorkspaceTrash() {
    const list = document.getElementById('ws-file-list');
    if (!list) return;
    const epoch = wsScopeEpoch;
    list.innerHTML = `<div class="workspace-empty"><i class="fas fa-spinner fa-spin"></i></div>`;
    try {
        const data = await wsApi('/api/workspace/trash');
        if (wsScopeStale(epoch)) return;
        wsTrashEntries = data.entries || [];
        wsTrashBytes = data.total_size || 0;
        wsTrashRetention = data.retention_days || 0;
        renderWorkspaceTrashHeader(wsTrashBytes);
        renderTrashEntries(wsTrashEntries);
    } catch (e) {
        if (wsScopeStale(epoch)) return;
        list.innerHTML = `<div class="workspace-empty">
            <i class="fas fa-triangle-exclamation"></i><span>${escapeHtml(wsErrorMessage(e))}</span></div>`;
    }
}

/** The bin's own breadcrumb: what it is, how much, and how long it keeps it. */
function renderWorkspaceTrashHeader(totalSize) {
    const bar = document.getElementById('ws-breadcrumb');
    if (!bar) return;
    bar.innerHTML = `<span class="crumb"><i class="fas fa-recycle"></i>
        <span class="crumb-root">${escapeHtml(t('ws_trash_title'))} ·
        ${escapeHtml(String(wsTrashEntries.length))} · ${escapeHtml(wsFormatSize(totalSize))} ·
        ${escapeHtml(t('ws_trash_keep').replace('{days}', String(wsTrashRetention)))}</span></span>`;
}

function renderTrashEntries(entries) {
    const list = document.getElementById('ws-file-list');
    if (!list) return;
    if (!entries.length) {
        list.innerHTML = `<div class="workspace-empty"><i class="fas fa-recycle"></i>
            <span>${escapeHtml(t('ws_trash_empty_state'))}</span></div>`;
        return;
    }
    list.innerHTML = entries.map(entry => `
        <div class="ws-file-row ws-bin-row" data-ws-rel="${escapeHtml(entry.rel || '')}"
             data-ws-size="${entry.size || 0}" data-ws-is-dir="${entry.kind === 'directory' ? '1' : '0'}"
             data-ws-bin-batch="${escapeHtml(entry.batch_id || '')}" data-ws-bin-index="${entry.index}"
             title="${escapeHtml(t('ws_trash_deleted_at').replace('{when}', wsShortWhen(entry.deleted_at)))}">
            <i class="${wsIconClass(entry.kind)}"></i>
            <span class="ws-file-name">${escapeHtml(entry.rel || '')}</span>
            <span class="ws-file-meta">${escapeHtml(wsFormatSize(entry.size))}</span>
            <button type="button" class="ws-row-act" data-ws-act="restore"
                    title="${escapeHtml(t('ws_trash_restore'))}">
                <i class="fas fa-rotate-left"></i></button>
            <button type="button" class="ws-row-act ws-row-act-danger" data-ws-act="purge"
                    title="${escapeHtml(t('ws_trash_purge'))}">
                <i class="fas fa-fire"></i></button>
        </div>`).join('');
}

/** A bin row's batch and index, which is how a bin entry is addressed. */
function wsBinAddress(row) {
    const batch = (row && row.dataset && row.dataset.wsBinBatch) || '';
    const index = parseInt((row && row.dataset && row.dataset.wsBinIndex) || '', 10);
    if (!batch || Number.isNaN(index)) return null;
    return { batch, index };
}

/** Restore one bin row to the place it came from. */
async function restoreTrashRow(row) {
    const address = wsBinAddress(row);
    if (!address) return;
    const restored = [];
    const failed = [];
    try {
        const data = await wsApiPost('/api/workspace/trash/restore',
            { batch_id: address.batch, indices: [address.index] });
        for (const item of data.restored || []) restored.push(item);
        for (const item of data.failed || []) failed.push(item);
    } catch (e) {
        failed.push({ code: e.code || 'failed' });
    }
    _wsToast(wsRestoreSummary(restored, failed));
    await loadWorkspaceTrash();
}

/**
 * What a restore actually did.
 *
 * A destination that was taken again is restored as `name (1).ext`, so the
 * summary names the real path rather than the one the entry asked for — that
 * path is the only way the user finds the file.
 */
function wsRestoreSummary(restored, failed) {
    const parts = [];
    if (restored.length) {
        parts.push(t('ws_trash_restored').replace('{count}', String(restored.length)));
    }
    const renamed = restored.filter(item => item.renamed);
    if (renamed.length) {
        const shown = renamed.slice(0, 3).map(item => item.path).join(' · ');
        parts.push(t('ws_trash_restored_renamed')
            .replace('{count}', String(renamed.length)).replace('{paths}', shown));
    }
    if (failed.length) {
        parts.push(wsFailureSummary(failed.map(item => ({ code: item.code }))));
    }
    return parts.join(' · ') || t('ws_trash_restored').replace('{count}', '0');
}

/** Permanently destroy one bin row, after saying it cannot be undone. */
function askWorkspacePurge(row) {
    const address = wsBinAddress(row);
    if (!address) return;
    showConfirmDialog({
        title: t('ws_trash_purge_confirm_title'),
        message: t('ws_trash_purge_confirm_msg').replace('{count}', '1'),
        okText: t('ws_trash_purge'),
        onConfirm: () => wsRunPurge([[address.batch, [address.index]]]),
    });
}

function emptyWorkspaceTrash() {
    if (!wsTrashEntries.length) return;
    showConfirmDialog({
        title: t('ws_trash_empty_confirm_title'),
        message: t('ws_trash_empty_confirm_msg')
            .replace('{count}', String(wsTrashEntries.length)),
        okText: t('ws_trash_purge'),
        onConfirm: () => wsRunPurge([]),
    });
}

/** Purge the given batches, or the whole bin when `groups` is empty. */
async function wsRunPurge(groups) {
    const purged = [];
    const failed = [];
    if (!groups.length) {
        try {
            const data = await wsApiPost('/api/workspace/trash/purge', {});
            purged.push(...(data.purged || []));
            failed.push(...(data.failed || []));
        } catch (e) {
            failed.push({ code: e.code || 'failed' });
        }
    } else {
        for (const [batchId, indices] of groups) {
            try {
                const data = await wsApiPost('/api/workspace/trash/purge',
                    { batch_id: batchId, indices });
                purged.push(...(data.purged || []));
                failed.push(...(data.failed || []));
            } catch (e) {
                failed.push({ code: e.code || 'failed' });
            }
        }
    }
    if (failed.length) _wsToast(wsFailureSummary(failed));
    else if (purged.length) {
        _wsToast(t('ws_trash_purged').replace('{count}', String(purged.length)));
    }
    await loadWorkspaceTrash();
}

// =====================================================================
// Init
// =====================================================================
function initWorkspacePanel() {
    initWorkspaceResizer();
    initWorkspaceFilesTab();
    initWorkspaceDropTarget();
    initWorkspaceUploadDrop();
    initMention();
    wsUpdateToolbarState();
    wsSetPreviewEmpty(t('ws_preview_empty'));

    // Reloading or closing the tab would drop an open editor's changes silently.
    window.addEventListener('beforeunload', (e) => {
        if (!wsEditorDirty()) return;
        e.preventDefault();
        e.returnValue = '';
    });

    // The panel belongs to the chat view only; follow view switches.
    const toggle = document.getElementById('workspace-toggle-btn');
    const chatView = document.getElementById('view-chat');
    if (toggle && chatView) {
        const sync = () => toggle.classList.toggle('hidden', !chatView.classList.contains('active'));
        sync();
        new MutationObserver(sync).observe(chatView, { attributes: true, attributeFilter: ['class'] });
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initWorkspacePanel);
} else {
    initWorkspacePanel();
}
