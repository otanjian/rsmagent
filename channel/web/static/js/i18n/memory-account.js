// Account memory UI additions; loaded before console.js snapshots namespaces.
(function () {
    const registry = window.__cowI18N__ = window.__cowI18N__ || {};
    registry['memory-account'] = {
        zh: {
            memory_account_type_daily: '每日/主动记忆',
            memory_account_type_evolution: '进化记录',
            memory_account_type_dream: '梦境日记',
            memory_account_no_files: '暂无记忆文件',
            memory_account_no_evolution: '暂无进化记录',
            memory_account_counts: '长期：{global}；每日/主动：{daily}；进化：{evolution}；梦境：{dream}',

            memory_account_empty_hint: '当前账号尚无此类记忆。可在对话中明确要求记住偏好；自动记录在生成后显示。',
            memory_clear_all: '清空我的记忆', memory_refresh: '刷新', memory_retry_index: '重试索引',
            memory_type_long_term: '长期记忆', memory_load_failed: '记忆加载失败',
            memory_retry_hint: '请点击刷新重试；登录失效时请重新登录。',
            memory_account_changed: '账号或租户已切换，请重新打开记忆。',
            memory_entry_missing: '该记忆已不存在，请刷新列表。',
            memory_clear_all_message: '将删除当前租户下本人全部 {count} 个记忆文件，包括长期、每日、主动记忆、进化记录和梦境日记。聊天历史、个人人设和共享记忆保留。',
        },
        'zh-Hant': {
            memory_account_type_daily: '每日/主動記憶',
            memory_account_type_evolution: '進化記錄',
            memory_account_type_dream: '夢境日記',
            memory_account_no_files: '暫無記憶檔案',
            memory_account_no_evolution: '暫無進化記錄',
            memory_account_counts: '長期：{global}；每日/主動：{daily}；進化：{evolution}；夢境：{dream}',

            memory_account_empty_hint: '目前帳號尚無此類記憶。可在對話中明確要求記住偏好；自動記錄在產生後顯示。',
            memory_clear_all: '清空我的記憶', memory_refresh: '重新整理', memory_retry_index: '重試索引',
            memory_type_long_term: '長期記憶', memory_load_failed: '記憶載入失敗',
            memory_retry_hint: '請重新整理再試；登入失效時請重新登入。',
            memory_account_changed: '帳號或租戶已切換，請重新開啟記憶。',
            memory_entry_missing: '該記憶已不存在，請重新整理清單。',
            memory_clear_all_message: '將刪除目前租戶下本人全部 {count} 個記憶檔案，包括長期、每日、主動記憶、進化記錄和夢境日記。聊天歷史、個人人設和共用記憶保留。',
        },
        en: {
            memory_account_type_daily: 'Daily / explicit',
            memory_account_type_evolution: 'Evolution',
            memory_account_type_dream: 'Dream',
            memory_account_no_files: 'No memory files',
            memory_account_no_evolution: 'No evolution records yet',
            memory_account_counts: 'Long-term: {global}; daily/explicit: {daily}; evolution: {evolution}; dreams: {dream}',

            memory_account_empty_hint: 'Your account has no memories in this category yet. Ask an Agent to remember a preference; automatic records appear after they are generated.',
            memory_clear_all: 'Clear my memory', memory_refresh: 'Refresh', memory_retry_index: 'Retry indexing',
            memory_type_long_term: 'Long-term memory', memory_load_failed: 'Memory could not be loaded',
            memory_retry_hint: 'Refresh to retry, or sign in again if your session expired.',
            memory_account_changed: 'Your account or tenant changed. Reopen memory management.',
            memory_entry_missing: 'This memory no longer exists. Refresh the list.',
            memory_clear_all_message: 'Delete all {count} of your memory files in this tenant, including long-term, daily and explicit memories, evolution records and dream diaries. Chat history, your profile and shared memory are kept.',
        },
    };
})();
