/* New chat and multi-agent team chat.
   Split out of console.js. These are classic scripts sharing one global
   scope; see channel/web/README.md before changing the load order. */

/* The session-panel "新对话" button. With a single Agent there is nobody to
   choose between, so it just starts a chat. With several, it opens a menu: pick
   an Agent for a solo chat, or open the team picker for a group chat. */
function onNewChatButton(event, surfaceName = 'panel') {
    const surface = _newChatSurface(surfaceName);
    const onCaret = event && event.target && event.target.closest
        && event.target.closest('#' + surface.caret);
    if (!onCaret) { surface.start(); return; }
    event.stopPropagation();
    openNewChatMenu(surface.menu, surfaceName);
}

function startSoloChat(agentId) {
    closeNewChatMenus();
    if (!agentId) { newChat(true); return; }
    activeAgentId = agentId;
    writeScopedPreference('cow_active_agent', activeAgentId);
    newChat(true);
    if (typeof resetWorkspaceToAgentRoot === 'function') resetWorkspaceToAgentRoot();
    renderComposerIdentity();
}

// The first checked Agent owns the conversation; the rest are invited as guests.
let _teamChatPicks = [];

function openTeamChatModal() {
    // Read before the pickers close: hiding the menu row that was clicked blurs
    // it, and focus has to come back to the launch control the user actually
    // used rather than to a row that no longer exists.
    const active = document.activeElement;
    const fromMenu = _newChatSurfaceOfMenu(active);
    const trigger = fromMenu ? document.getElementById(_newChatSurface(fromMenu).control) : active;
    closeNewChatMenus();
    // A picker opened from the phone drawer is a sheet over the whole viewport:
    // leaving the drawer up behind it would stack two layers the user has to
    // dismiss and hide the control that focus returns to.
    const sidebar = document.getElementById('sidebar');
    if (window.innerWidth < 1024 && trigger && sidebar
            && typeof sidebar.contains === 'function' && sidebar.contains(trigger)) {
        closeSidebar();
    }
    // A coding conversation is *not* replaced here: this modal only prepares an
    // ordinary roster over a session id nobody has opened yet, and the coding
    // pane is left at commit time (task 3.2). Keeping the entry available also
    // means the user never has to leave the coding chat to look at the picker.
    const start = initialTeamChatSelection();
    _teamChatDraft = {
        epoch: ((_teamChatDraft && _teamChatDraft.epoch) || 0) + 1,
        selected: start.ids,
        owner: start.owner,
        query: '',
        phase: 'editing',
        error: '',
    };
    const search = document.getElementById('team-chat-search');
    if (search) search.value = '';
    renderTeamChatDraft();
    document.getElementById('team-chat-modal')?.classList.remove('hidden');
    _teamChatTrigger = (typeof trigger === 'object') ? trigger : null;
    document.addEventListener('keydown', _teamChatEscape);
    if (search) search.focus();
}

function closeTeamChatModal({ restoreFocus = true } = {}) {
    // Dropping the draft is what invalidates an in-flight save: its continuation
    // checks that it still owns `_teamChatDraft`, so closing during the write
    // cancels the commit without touching the conversation on screen.
    _teamChatDraft = null;
    document.getElementById('team-chat-modal')?.classList.add('hidden');
    document.removeEventListener('keydown', _teamChatEscape);
    if (restoreFocus) _restoreTeamChatFocus();
    else _teamChatTrigger = null;
}

/** From the group-chat picker, jump to creating a new Agent. */
function openAgentCreateFromModal() {
    // Focus belongs to the create form this hands off to, not to the launch
    // button the sheet came from.
    closeTeamChatModal({ restoreFocus: false });
    navigateTo('agents');
    if (typeof openAgentCreateForm === 'function') openAgentCreateForm();
}

function toggleTeamChatPick(agentId) {
    const draft = _teamChatDraft;
    if (!draft || draft.phase !== 'editing') return;
    const i = draft.selected.indexOf(agentId);
    if (i === -1) {
        draft.selected.push(agentId);
        // Selecting is the conscious act that may also answer "who responds by
        // default"; nobody is picked for the user otherwise.
        if (!draft.owner) draft.owner = agentId;
    } else {
        draft.selected.splice(i, 1);
        // The default responder is never re-assigned silently: dropping it
        // leaves the field empty until the user says who takes its place
        // (design D3).
        if (draft.owner === agentId) draft.owner = '';
    }
    draft.error = '';
    renderTeamChatDraft();
}

function renderTeamChatList() {
    const list = document.getElementById('team-chat-list');
    if (!list) return;
    list.innerHTML = enabledAgents().map(agent => {
        const rank = _teamChatPicks.indexOf(agent.id);
        const on = rank !== -1;
        const owner = rank === 0;
        return `<button type="button" class="team-chat-row${on ? ' on' : ''}" onclick="toggleTeamChatPick('${escapeHtml(agent.id)}')">
            ${agentAvatarHTML(agent, 28)}
            <span class="team-chat-name">${escapeHtml(agent.name)}</span>
            ${owner ? `<span class="team-chat-owner">${escapeHtml(t('new_team_chat_owner'))}</span>` : ''}
            <span class="team-chat-check"><i class="fas ${on ? 'fa-circle-check' : 'fa-circle'}"></i></span>
        </button>`;
    }).join('');
}

function startTeamChat() {
    const draft = _teamChatDraft;
    if (!draft || draft.phase !== 'editing') return;   // one attempt at a time
    const problem = teamChatRosterProblem(draft);
    if (problem) {
        draft.error = t(problem);
        renderTeamChatDraft();
        return;
    }
    // The roster is saved before anything moves, so the ordinary leave-page
    // protection is asked up front -- cancelling it changes nothing at all
    // (task 3.4). The same guard is asked again at commit time.
    if (typeof wsGuardUnsaved === 'function' && !wsGuardUnsaved(() => startTeamChat())) return;
    prepareTeamChatSession(draft);
}

function newChat(optimistic = true, inherit = true) {
    const seam = (typeof codingSeam === 'function') ? codingSeam() : null;
    // A new conversation for a coding Agent is a new Opencode session; the
    // ordinary pane is not prepared for it at all.
    if (seam && seam.isCodingAgent(activeAgentId)) {
        if (typeof wsGuardUnsaved === 'function'
            && !wsGuardUnsaved(() => newChat(optimistic, inherit))) return;
        return seam.open(activeAgentId, '');
    }
    // A fresh session resets the preview panel, discarding an open editor.
    if (typeof wsGuardUnsaved === 'function'
        && !wsGuardUnsaved(() => newChat(optimistic, inherit))) return;
    commitPreparedSession(generateSessionId(), { optimistic, inherit });
    if (typeof resetTimeline === 'function') resetTimeline();
}

