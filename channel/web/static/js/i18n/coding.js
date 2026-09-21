/* =====================================================================
 * Console i18n namespace: coding sessions (coding agents)
 *
 * Change add-opencode-coding-agents, task group 4. Registered on
 * window.__cowI18N__ the same way every other namespace is, so console.js
 * merges it into the single lookup table.
 * ===================================================================== */
(function () {
    'use strict';

    if (typeof window === 'undefined') return;

    window.__cowI18N__ = window.__cowI18N__ || {};

    window.__cowI18N__['coding'] = {
        zh: {
            coding_loading: '正在打开编码会话…',
            coding_unavailable: '编码服务暂时不可用，稍后会自动重试。',
            coding_open_failed: '打开编码会话失败：{reason}',
            coding_not_ready: '编码会话启动较慢，仍在等待服务响应。',
            coding_retry: '重试',
            coding_retry_hint: '重试只会重新打开当前会话，不会新建会话。',
            coding_disabled: '编码功能当前未启用，请联系管理员。',
            coding_linked: '已在 Opencode 中打开新的会话，正在登记…',
            coding_linked_ok: '新会话已加入历史记录。',
            coding_attach_failed: '登记 Opencode 中新建的会话失败：{reason}',
            coding_project: '项目目录',
            coding_service: '编码服务',
            coding_type_coding: '编码（Opencode）',
            coding_type_normal: '普通',
            coding_session_hint: '此对话由 Opencode 提供，支持代码编辑、差异与终端。',
        },
        en: {
            coding_loading: 'Opening the coding session…',
            coding_unavailable: 'The coding service is unavailable; retrying shortly.',
            coding_open_failed: 'Could not open the coding session: {reason}',
            coding_not_ready: 'The coding service is taking a while to respond.',
            coding_retry: 'Retry',
            coding_retry_hint: 'Retrying reopens this same session; it never starts a new one.',
            coding_disabled: 'Coding is not enabled. Ask an administrator.',
            coding_linked: 'A new Opencode session was opened; registering it…',
            coding_linked_ok: 'The new session was added to your history.',
            coding_attach_failed: 'Could not register the Opencode session: {reason}',
            coding_project: 'Project directory',
            coding_service: 'Coding service',
            coding_type_coding: 'Coding (Opencode)',
            coding_type_normal: 'Normal',
            coding_session_hint: 'This conversation runs in Opencode: code editing, diffs and a terminal.',
        },
        'zh-Hant': {
            coding_loading: '正在開啟編碼工作階段…',
            coding_unavailable: '編碼服務暫時無法使用，稍後會自動重試。',
            coding_open_failed: '開啟編碼工作階段失敗：{reason}',
            coding_not_ready: '編碼工作階段啟動較慢，仍在等待服務回應。',
            coding_retry: '重試',
            coding_retry_hint: '重試只會重新開啟目前的工作階段，不會新建工作階段。',
            coding_disabled: '編碼功能目前未啟用，請聯絡管理員。',
            coding_linked: '已在 Opencode 中開啟新的工作階段，正在登記…',
            coding_linked_ok: '新工作階段已加入歷史記錄。',
            coding_attach_failed: '登記 Opencode 中新建的工作階段失敗：{reason}',
            coding_project: '專案目錄',
            coding_service: '編碼服務',
            coding_type_coding: '編碼（Opencode）',
            coding_type_normal: '普通',
            coding_session_hint: '此對話由 Opencode 提供，支援程式碼編輯、差異與終端。',
        },
    };
}());
