/* SAP scene only: no chat composer, default Agent, or global coding state. */
(function () {
    'use strict';
    const API = '/api/scenes/sap-workbench';
    // The session permission profile the platform maps to a server-defined
    // ruleset, re-enabling the SAP tool for sessions this scene creates. It is a
    // *name*: the rules live on the server so a page cannot grant itself a tool.
    const CODING_PERMISSION_PROFILE = 'sap_workbench';
    const messages = {
        zh: {
            rememberSap: '记住 SAP 登录', forgetSap: '清除已记住的 SAP 登录', sapRemembered: '已记住 SAP 登录状态；下次成功登录后会保存账号密码。',
            sap_login_unavailable: 'SAP 登录记忆暂不可用，可继续手动登录。', sap_keychain_unavailable: 'macOS 钥匙串不可用，登录信息未保存。',
            desktopRead: 'macOS 当前页面读取',
            title: 'SAP智能工作台', subtitle: 'SAP Web GUI 与高级智能体', close: '关闭', home: '返回首页',
            settings: '连接配置', back: '返回工作台', refresh: '刷新状态', pending: '运行环境待就绪',
            sap: 'SAP 页面', opencode: '高级智能体对话', browser: '浏览器执行节点', project: '项目目录',
            mcp: 'MCP 连接', start: '新建工作台会话', resume: '恢复已有会话', login: '登录 SAP',
            emptySap: 'SAP 页面将在专属浏览器就绪后显示',
            emptyCode: '高级智能体对话将在会话绑定完成后显示',
            setupHint: '先完成连接配置。当前版本尚未接通浏览器和工具执行，保存配置不会启动会话。',
            memberHint: '请联系租户管理员配置连接。', loading: '正在读取配置…', failure: '请求失败，请重试。',
            unavailable: '运行组件未就绪', not_run: '未联调', configured: '已填写', missing: '未配置',
            check: '检查已保存配置', checkHint: '这里只检查配置完整性；尚未执行远端连接或 SAP 操作。',
            saved: '配置已保存。运行功能仍需通过联调后开放。', save: '保存配置', saving: '正在保存…',
            system: 'SAP 系统标识', sapUrl: 'SAP Web GUI 完整 URL', client: 'SAP Client',
            language: 'SAP 语言', origins: '允许的 SAP / SSO 来源（每行一项）', loginMode: '登录方式',
            password: '统一 SAP 账号密码登录', manual: '在 SAP 页面登录', sso: '企业 SSO',
            accountRule: '两个 MCP 使用下方维护的账号密码；SAP 系统和 Client 沿用上方配置。',
            mcp_credentials: 'MCP 账号密码', mcpUsername: 'MCP 的 SAP 账号', mcpPassword: 'MCP 的 SAP 密码',
            mcpPasswordSaved: '密码已设置；留空保留，输入新值替换。', mcpPasswordMissing: '密码尚未设置。',
            mcpClearPassword: '清除已保存的 MCP 密码', mcpCredentialsHint: '密码加密保存在当前租户的场景配置中，不回显。更改账号、SAP 系统、地址或 Client 后需重新填写密码；当前不依赖 Web GUI 登录。',
            mcp_credentials_required: '请填写 MCP 账号、密码及 SAP 地址和 Client。',
            credential_crypto_unavailable: '服务端凭据加密不可用，密码未保存。请检查主密钥配置。',
            fixedMcp: 'MCP 地址已固定，只需选择是否启用。仅连接测试 SAP https://sap.goodsap.cn:44300 时跳过后台证书校验；其他 SAP 地址仍校验，不复用浏览器证书设置。',
            mcp_connection_fixed: 'MCP 地址和连接方式已固定，请刷新配置后重试。',
            loginHint: 'SAP 页面仍单独登录；当前 MCP 使用配置中的账号密码。',
            agent: '高级智能体配置', select: '请选择', webUrl: '高级智能体 Web 地址',
            projectHint: '当前使用本机高级智能体 执行环境，项目目录须在本机存在。请在已有 coding 配置中维护。',
            node: '浏览器执行节点引用', capacity: '并发会话上限', idle: '空闲回收秒数',
            flags: '启用意向', enabled: '可视工作台', automation: '自动页面操作', commit: '业务提交',
            flagsHint: '这些开关只保存配置意向；未实现或未验收的运行能力始终关闭。',
            addMcp: '添加 MCP', remove: '移除', id: '连接标识', transport: '连接方式',
            remote: '远程 HTTP', stdio: '受管本地配置', endpoint: 'MCP 地址', profile: '本地配置引用',
            auth: '传输认证', none: '无额外认证', sap_session: '当前 SAP 登录', service_ref: '网关服务凭据引用',
            credentialRef: '网关服务凭据引用', connectionEnabled: '启用此连接', ratio: 'SAP 页面宽度比例',
            disabled: '场景尚未启用', browser_runtime_unavailable: '浏览器运行组件尚未接通',
            opencode_adapter_unverified: '高级智能体工具适配尚未验收', session_access_unverified: '上游会话访问隔离尚未验收',
            sap_login_adapter_unavailable: 'SAP 与 MCP 同源登录尚未接通', opencode_quota_unverified: '高级智能体模型配额接入尚未验收',
            config_conflict: '配置已被更新，请刷新后重新编辑。', config_forbidden: '没有配置管理权限。',
            invalid_url: '地址必须是完整 HTTP(S) URL，且不能含账号、密码或令牌。',
            sap_parameter_conflict: 'URL 中的 SAP Client / 语言与表单不一致。', invalid_config: '配置格式不正确。',
            invalid_flags: '请先启用上一级功能。', sap_credentials_required: 'MCP 必须跟随当前 SAP 登录。',
            unsaved: '连接配置尚未保存，是否放弃修改？',
            viewStart: '打开 SAP 页面', viewStop: '关闭页面', viewStarting: '正在启动专属浏览器…',
            viewLive: '已连接：点击或输入即发送到 SAP', viewEnded: '页面连接已断开，可重新打开。',
            viewReconnecting: '连接中断，正在尝试重连…',
            browser_unavailable: '本机找不到可用的 Chrome，无法启动浏览器执行节点。',
            config_missing: '请先保存 SAP Web GUI 地址。', runtime_unavailable: '浏览器执行节点未能启动，请重试。',
            viewDisabled: '场景尚未启用，无法打开页面。',
            viewMemberOnly: '实时页面当前仅向租户管理员开放。',
        },
        en: {
            rememberSap: 'Remember SAP login', forgetSap: 'Forget SAP login', sapRemembered: 'SAP session remembered. Credentials will be saved after the next successful login.',
            sap_login_unavailable: 'SAP login memory is unavailable. You can still sign in manually.', sap_keychain_unavailable: 'macOS Keychain is unavailable; login details were not saved.',
            desktopRead: 'Read current page on macOS',
            title: 'SAP Intelligent Workbench', subtitle: 'SAP Web GUI and Advanced Agent', close: 'Close', home: 'Back to home', settings: 'Connections',
            back: 'Back to workbench', refresh: 'Refresh status', pending: 'Runtime not ready', sap: 'SAP page',
            opencode: 'Advanced Agent chat', browser: 'Browser worker', project: 'Project directory', mcp: 'MCP connections',
            start: 'New workbench session', resume: 'Resume session', login: 'Sign in to SAP',
            emptySap: 'The SAP page will appear when its browser is ready', emptyCode: 'The Advanced Agent will appear after session binding',
            setupHint: 'Configure connections first. Browser and tool execution are not connected in this build. Saving does not start a session.',
            memberHint: 'Ask your tenant administrator to configure connections.', loading: 'Loading configuration…',
            failure: 'Request failed. Please retry.', unavailable: 'Runtime unavailable', not_run: 'Not verified',
            configured: 'Configured', missing: 'Missing', check: 'Check saved configuration',
            checkHint: 'This checks configuration completeness only; no remote connection or SAP operation has been tested.',
            saved: 'Configuration saved. Runtime features still require integration verification.', save: 'Save configuration', saving: 'Saving…',
            system: 'SAP system ID', sapUrl: 'Full SAP Web GUI URL', client: 'SAP Client', language: 'SAP language',
            origins: 'Allowed SAP / SSO origins (one per line)', loginMode: 'Sign-in method', password: 'Unified SAP password sign-in',
            manual: 'Sign in on the SAP page', sso: 'Enterprise SSO',
            accountRule: 'Both MCP gateways use the account below and the SAP system and Client configured above.',
            mcp_credentials: 'MCP credentials', mcpUsername: 'MCP SAP username', mcpPassword: 'MCP SAP password',
            mcpPasswordSaved: 'Password is set. Leave blank to keep it or enter a replacement.', mcpPasswordMissing: 'No password is set.',
            mcpClearPassword: 'Clear saved MCP password', mcpCredentialsHint: 'The password is encrypted in this tenant’s scene settings and never displayed. Re-enter it after changing the account, SAP system, address or Client. Web GUI sign-in is currently independent.',
            mcp_credentials_required: 'Set the MCP username and password, SAP URL and Client.',
            credential_crypto_unavailable: 'Credential encryption is unavailable; the password was not saved. Check the server master key.',
            fixedMcp: 'MCP endpoints are fixed; only enablement is editable. Backend certificate verification is skipped only for test SAP https://sap.goodsap.cn:44300. Other SAP endpoints are verified; browser certificate settings are not reused.',
            mcp_connection_fixed: 'MCP endpoints and transport are fixed. Refresh the configuration and retry.',
            loginHint: 'Sign in to the SAP page separately. MCP currently uses its configured credentials.',
            agent: 'Advanced Agent configuration', select: 'Select', webUrl: 'Advanced Agent Web URL',
            projectHint: 'This workbench runs the Advanced Agent locally. The project must exist on this host. Manage it in the existing coding configuration.',
            node: 'Browser worker reference', capacity: 'Maximum concurrent sessions', idle: 'Idle timeout (seconds)',
            flags: 'Requested features', enabled: 'Visual workbench', automation: 'Page automation', commit: 'Business submission',
            flagsHint: 'Flags record intent only. Unimplemented or unverified runtime features remain disabled.',
            addMcp: 'Add MCP', remove: 'Remove', id: 'Connection ID', transport: 'Transport', remote: 'Remote HTTP',
            stdio: 'Managed local profile', endpoint: 'MCP URL', profile: 'Local profile reference', auth: 'Transport authentication',
            none: 'No additional authentication', sap_session: 'Current SAP sign-in', service_ref: 'Gateway credential reference',
            credentialRef: 'Gateway credential reference', connectionEnabled: 'Enable connection', ratio: 'SAP pane width',
            disabled: 'Scene is disabled', browser_runtime_unavailable: 'Browser runtime is not connected',
            opencode_adapter_unverified: 'Advanced Agent tool adapter is unverified', session_access_unverified: 'Upstream session isolation is unverified',
            sap_login_adapter_unavailable: 'Shared SAP / MCP sign-in is not connected', opencode_quota_unverified: 'Advanced Agent model quota integration is unverified',
            config_conflict: 'Configuration changed. Refresh before editing again.', config_forbidden: 'Configuration management is not permitted.',
            invalid_url: 'Use a full HTTP(S) URL without a username, password or token.', sap_parameter_conflict: 'SAP Client / language in the URL conflicts with the form.',
            invalid_config: 'Invalid configuration.', invalid_flags: 'Enable the preceding feature first.',
            sap_credentials_required: 'MCP must follow the current SAP sign-in.', unsaved: 'Discard unsaved connection changes?',
            viewStart: 'Open SAP page', viewStop: 'Close page', viewStarting: 'Starting the dedicated browser…',
            viewLive: 'Connected: clicks and typing are sent to SAP', viewEnded: 'The page connection ended. You can reopen it.',
            viewReconnecting: 'The connection dropped; reconnecting…',
            browser_unavailable: 'No usable Chrome was found, so the browser node cannot start.',
            config_missing: 'Save the SAP Web GUI URL first.', runtime_unavailable: 'The browser node did not start. Please retry.',
            viewDisabled: 'The scene is disabled, so the page cannot open.',
            viewMemberOnly: 'The live page is currently available to tenant administrators only.',
        },
        'zh-Hant': {
            rememberSap: '記住 SAP 登入', forgetSap: '清除已記住的 SAP 登入', sapRemembered: '已記住 SAP 登入狀態；下次成功登入後會儲存帳號密碼。',
            sap_login_unavailable: 'SAP 登入記憶暫不可用，可繼續手動登入。', sap_keychain_unavailable: 'macOS 鑰匙圈不可用，登入資訊未儲存。',
            desktopRead: 'macOS 目前頁面讀取',
            title: 'SAP智能工作台', subtitle: 'SAP Web GUI 與高級智能體', close: '關閉', home: '返回首頁', settings: '連線設定',
            back: '返回工作台', refresh: '重新整理狀態', pending: '執行環境尚未就緒', sap: 'SAP 頁面',
            opencode: '高級智能體對話', browser: '瀏覽器執行節點', project: '專案目錄', mcp: 'MCP 連線',
            start: '新增工作台工作階段', resume: '恢復工作階段', login: '登入 SAP',
            emptySap: '專屬瀏覽器就緒後將顯示 SAP 頁面', emptyCode: '完成工作階段綁定後將顯示高級智能體',
            setupHint: '請先完成連線設定。目前版本尚未接通瀏覽器與工具執行，儲存設定不會啟動工作階段。',
            memberHint: '請聯絡租戶管理員設定連線。', loading: '正在讀取設定…', failure: '請求失敗，請重試。',
            unavailable: '執行元件尚未就緒', not_run: '尚未驗證', configured: '已填寫', missing: '未設定',
            check: '檢查已儲存設定', checkHint: '此處僅檢查設定完整性；尚未執行遠端連線或 SAP 操作。',
            saved: '設定已儲存。執行功能仍須通過整合驗證後開放。', save: '儲存設定', saving: '正在儲存…',
            system: 'SAP 系統識別碼', sapUrl: 'SAP Web GUI 完整 URL', client: 'SAP Client', language: 'SAP 語言',
            origins: '允許的 SAP / SSO 來源（每行一項）', loginMode: '登入方式', password: '統一 SAP 帳號密碼登入',
            manual: '在 SAP 頁面登入', sso: '企業 SSO', accountRule: '兩個 MCP 使用下方維護的帳號密碼；SAP 系統和 Client 沿用上方設定。',
            mcp_credentials: 'MCP 帳號密碼', mcpUsername: 'MCP 的 SAP 帳號', mcpPassword: 'MCP 的 SAP 密碼',
            mcpPasswordSaved: '密碼已設定；留空保留，輸入新值替換。', mcpPasswordMissing: '密碼尚未設定。',
            mcpClearPassword: '清除已儲存的 MCP 密碼', mcpCredentialsHint: '密碼加密儲存在目前租戶的場景設定中，不回顯。變更帳號、SAP 系統、位址或 Client 後需重新填寫密碼；目前不依賴 Web GUI 登入。',
            mcp_credentials_required: '請填寫 MCP 帳號、密碼及 SAP 位址和 Client。',
            credential_crypto_unavailable: '伺服器憑證加密不可用，密碼未儲存。請檢查主金鑰設定。',
            fixedMcp: 'MCP 位址已固定，只需選擇是否啟用。僅連接測試 SAP https://sap.goodsap.cn:44300 時略過後端憑證驗證；其他 SAP 位址仍驗證，不共用瀏覽器憑證設定。',
            mcp_connection_fixed: 'MCP 位址與連線方式已固定，請重新整理設定後重試。',
            loginHint: 'SAP 頁面仍單獨登入；目前 MCP 使用設定中的帳號密碼。',
            agent: '高級智能體設定', select: '請選擇', webUrl: '高級智能體 Web 位址',
            projectHint: '目前使用本機高級智能體執行環境，專案目錄須在本機存在。請在既有 coding 設定中維護。',
            node: '瀏覽器執行節點參照', capacity: '同時工作階段上限', idle: '閒置回收秒數', flags: '啟用意向',
            enabled: '可視工作台', automation: '自動頁面操作', commit: '業務提交',
            flagsHint: '開關僅儲存設定意向；尚未實作或驗證的執行能力一律關閉。',
            addMcp: '新增 MCP', remove: '移除', id: '連線識別碼', transport: '連線方式', remote: '遠端 HTTP',
            stdio: '受管本機設定', endpoint: 'MCP 位址', profile: '本機設定參照', auth: '傳輸驗證',
            none: '無額外驗證', sap_session: '目前 SAP 登入', service_ref: '閘道服務憑據參照',
            credentialRef: '閘道服務憑據參照', connectionEnabled: '啟用此連線', ratio: 'SAP 頁面寬度比例',
            disabled: '場景尚未啟用', browser_runtime_unavailable: '瀏覽器執行元件尚未接通',
            opencode_adapter_unverified: '高級智能體工具適配尚未驗證', session_access_unverified: '上游工作階段存取隔離尚未驗證',
            sap_login_adapter_unavailable: 'SAP 與 MCP 共用登入尚未接通', opencode_quota_unverified: '高級智能體模型配額接入尚未驗證',
            config_conflict: '設定已更新，請重新整理後再編輯。', config_forbidden: '沒有設定管理權限。',
            invalid_url: '請使用完整 HTTP(S) URL，且不能包含帳號、密碼或權杖。',
            sap_parameter_conflict: 'URL 中的 SAP Client / 語言與表單不一致。', invalid_config: '設定格式不正確。',
            invalid_flags: '請先啟用上一級功能。', sap_credentials_required: 'MCP 必須跟隨目前 SAP 登入。',
            unsaved: '連線設定尚未儲存，是否放棄修改？',
            viewStart: '開啟 SAP 頁面', viewStop: '關閉頁面', viewStarting: '正在啟動專屬瀏覽器…',
            viewLive: '已連線：點擊或輸入即傳送到 SAP', viewEnded: '頁面連線已中斷，可重新開啟。',
            viewReconnecting: '連線中斷，正在嘗試重新連線…',
            browser_unavailable: '本機找不到可用的 Chrome，無法啟動瀏覽器執行節點。',
            config_missing: '請先儲存 SAP Web GUI 位址。', runtime_unavailable: '瀏覽器執行節點未能啟動，請重試。',
            viewDisabled: '場景尚未啟用，無法開啟頁面。',
            viewMemberOnly: '即時頁面目前僅向租戶管理員開放。',
        },
    };
    let dialog, data, generation = 0, dirty = false, previousFocus, view = null;
    let reconnects = 0, retryTimer = null;
    // Backoff for a dropped pane socket, in milliseconds; five attempts span
    // about fifteen seconds before the pane admits the connection is gone.
    const RETRIES = [800, 1600, 3200, 5000, 5000];
    let binding = null, pendingRequest = null, startingSession = false, codeFrame = null, chatOpen = false, chatFullscreen = false;
    let codeOrigin = null, codeChannel = null;
    // Identifies the current conversation mount. Every unmount and every new
    // mount takes a fresh value, so an async step of a superseded mount can
    // recognise itself as stale. Two rapid retries therefore produce one frame
    // and one readiness wait instead of stacking a second of either.
    let codeMount = 0;
    let layout = {ratio: 0.32, open: false}, layoutLoaded = false;
    let composer = null, promptRequest = null, promptTimer = null, pageCapture = null, pageFeedback = '';
    Object.assign(messages.zh, {
        accountMenu: '账号与连接', memoryOn: '登录记忆已开启', memoryOff: '登录记忆未开启',
        memorySaved: '账号密码已保存', memoryHint: '清除记忆只删除本机保存的登录信息，不会退出当前 SAP。退出请使用 SAP 页面内的“退出”。',
        readPage: '读取当前页面', readIdle: '尚未读取当前页面', readPending: '正在读取当前页面…',
        readCaptured: '{title} · 采集于 {time}', readStale: '上次采集：{snapshot}（页面已切换，请重新读取）',
        readChanged: '页面已切换，请重新读取。', readUnavailable: '页面读取尚未就绪', readFailed: '页面读取失败，请重试。', pageUntitled: 'SAP 页面',
        starterTitle: '开始与 SAP 智能助手协作', starterHint: '可以从左侧当前页面开始，也可以直接输入问题。',
        summarizePage: '总结当前页面', explainError: '解释当前报错', openTransaction: '打开事务',
        promptDraft: '输入框已有草稿，请先发送或清空后重试。', promptBusy: '助手正在处理，请稍后重试。',
        promptUnavailable: '助手尚未就绪，请稍后重试。', promptFilled: '请在输入框补充事务码后发送。',
        promptRead: '请调用 sap_page_read 读取左侧当前 SAP 页面，简要说明读取到的页面名称和内容。',
        promptSummary: '请先调用 sap_page_read 读取左侧当前 SAP 页面，再总结页面的关键信息。只依据本次读取结果。',
        promptError: '请先调用 sap_page_read 读取左侧当前 SAP 页面，解释当前可见报错并给出处理建议；如果没有可见报错，请明确说明。',
        promptTransaction: '打开事务 ', layoutFailed: '布局未能保存，重启后可能恢复默认布局。',
    });
    Object.assign(messages.en, {
        accountMenu:'Account & connections', memoryOn:'Login memory on', memoryOff:'Login memory off', memorySaved:'Credentials saved',
        memoryHint:'Forgetting removes saved login information from this device. To sign out of the current SAP session, use Exit inside SAP.',
        readPage:'Read current page', readIdle:'Current page has not been read', readPending:'Reading current page…', readCaptured:'{title} · Captured at {time}',
        readStale:'Last capture: {snapshot} (page changed; read again)', readChanged:'The page changed. Please read it again.', readUnavailable:'Page reading is not ready', readFailed:'Could not read the page. Please retry.', pageUntitled:'SAP page',
        starterTitle:'Work with your SAP assistant', starterHint:'Start with the current SAP page or type a question.',
        summarizePage:'Summarize current page', explainError:'Explain current error', openTransaction:'Open transaction',
        promptDraft:'Send or clear your existing draft first.', promptBusy:'The assistant is busy. Please try again shortly.', promptUnavailable:'The assistant is not ready yet.',
        promptFilled:'Enter the transaction code in the composer, then send.', promptRead:'Call sap_page_read to read the current SAP page and briefly describe its title and content.',
        promptSummary:'Call sap_page_read and summarize the current SAP page using only the new capture.',
        promptError:'Call sap_page_read and explain any visible error with suggested next steps. State clearly if no error is visible.',
        promptTransaction:'Open transaction ', layoutFailed:'The layout could not be saved for the next launch.',
    });
    Object.assign(messages['zh-Hant'], {
        accountMenu:'帳號與連線', memoryOn:'登入記憶已開啟', memoryOff:'登入記憶未開啟', memorySaved:'帳號密碼已儲存',
        memoryHint:'清除記憶只刪除本機儲存的登入資訊，不會登出目前 SAP。登出請使用 SAP 頁面內的「退出」。',
        readPage:'讀取目前頁面', readIdle:'尚未讀取目前頁面', readPending:'正在讀取目前頁面…', readCaptured:'{title} · 擷取於 {time}',
        readStale:'上次擷取：{snapshot}（頁面已切換，請重新讀取）', readChanged:'頁面已切換，請重新讀取。', readUnavailable:'頁面讀取尚未就緒', readFailed:'頁面讀取失敗，請重試。', pageUntitled:'SAP 頁面',
        starterTitle:'開始與 SAP 智能助手協作', starterHint:'可以從左側目前頁面開始，也可以直接輸入問題。', summarizePage:'總結目前頁面', explainError:'解釋目前錯誤', openTransaction:'開啟交易',
        promptDraft:'輸入框已有草稿，請先傳送或清空後重試。', promptBusy:'助手正在處理，請稍後重試。', promptUnavailable:'助手尚未就緒，請稍後重試。', promptFilled:'請在輸入框補充交易代碼後傳送。',
        promptRead:'請呼叫 sap_page_read 讀取左側目前 SAP 頁面，簡要說明讀取到的頁面名稱和內容。',
        promptSummary:'請先呼叫 sap_page_read 讀取左側目前 SAP 頁面，再總結頁面的關鍵資訊。只依據本次讀取結果。',
        promptError:'請先呼叫 sap_page_read 讀取左側目前 SAP 頁面，解釋目前可見錯誤並提供處理建議；如果沒有可見錯誤，請明確說明。',
        promptTransaction:'開啟交易 ', layoutFailed:'版面未能儲存，重新啟動後可能恢復預設版面。',
    });

    // Session ids already reported to the scene, so a resumed frame that keeps
    // re-announcing itself costs exactly one attach call.
    const attachSent = new Set();
    let chatDrag = null;
    Object.assign(messages.zh, {
        sapEmbedded: '已嵌入 SAP Web GUI；请在页面中登录。',
        iframe_page_control_unavailable: '原生 SAP 页面由你直接操作；当前对话尚不能控制跨域页面。',
    });
    Object.assign(messages.en, {sapEmbedded: 'SAP Web GUI is embedded. Sign in on the page.', iframe_page_control_unavailable: 'Operate the native SAP page directly. Chat cannot control this cross-origin page yet.'});
    Object.assign(messages['zh-Hant'], {sapEmbedded: '已嵌入 SAP Web GUI，請在頁面中登入。', iframe_page_control_unavailable: '原生 SAP 頁面由你直接操作；對話尚不能控制跨來源頁面。'});
    Object.assign(messages.zh, {
        opencode_runtime: '高级智能体运行环境', mcp_runtime: 'MCP 运行环境',
        opencode_runtime_missing: '本机 Bun、高级智能体源码或依赖尚未准备。',
        browser_service_unavailable: '请选择可用的本机浏览器节点。',
        mcp_check_timeout: 'MCP 连接检测超时，请检查网关服务。',
        localBrowserNode: '本机专属浏览器', unavailableBrowserNode: '当前节点不可用',
        readyToStart: '可创建工作台会话', capabilityNotes: '当前能力说明',
        navigation_limited: '画面导航：有限可用。只确认导航指令送达，不代表已完成登录或拿到业务结果。',
        mcp_business_available: 'SAP 业务数据：可用。对话按会话经服务端已登记的连接读写业务数据；SAP 凭据与连接标识不下发给模型。',
        mcp_credentials_missing: 'SAP 业务数据：未就绪。尚未保存 MCP 账号口令，服务端无法建立连接，数据调用会以 mcp_login_failed 失败。',
        page_readwrite_unavailable: '页面读写与业务提交：不可用。阅读、填写左侧 SAP 页面字段和提交业务单据均不开放。',
        desktop_page_read_conditional: '页面读取：需使用新版 macOS 桌面并打开自己的 SAP 工作台。仅读取已渲染内容；填写与业务提交不可用。',
        setupHint: '新建会话会自动结束当前账号旧会话。SAP Web GUI 直接嵌入；点击右下角 AI 对话展开高级智能体。对话可使用已配置的 MCP，原生 SAP 页面由你直接操作。',
        manualControl: '人工接管 / 暂停', autoControl: '允许对话操作', sessionStarting: '正在准备 SAP 与高级智能体 会话…',
        sessionReady: '会话已连接，当前由人工控制。', automaticReady: '已允许对话操作当前 SAP 页面。',
        sessionConnecting: '对话已就绪，正在连接 SAP 页面…',
        opencode_session_mismatch: '原会话与配置的项目不一致，已停止恢复。请核对连接配置。',
        noSessions: '尚无可恢复会话，请新建。', sap_login_required: '请先在 SAP 页面完成登录。',
        browser_not_connected: 'SAP 画面尚未连接，请稍后重试。', model_credentials_missing: '所选高级智能体模型尚未配置密钥。',
        runtime_assets_or_project_missing: '高级智能体界面资源或配置的项目目录不存在。',
        opencode_host_failed: '高级智能体场景服务启动失败，请检查场景运行日志。',
        model_configuration_unsupported: '当前模型配置暂不支持场景接入。',
        session_not_running: '会话未运行，请恢复已有会话。', control_changed: '页面已由人工接管，请重新观察。',
        check: '测试已保存连接', checkHint: '连接设置已加载。可新建或恢复会话，或在连接配置中测试连接。',
        checking: '正在测试连接，不调用模型或修改 SAP…', checked: '连接测试完成，请查看分项结果。',
        passed: '通过', failed: '失败', certificate_untrusted: '证书未受信任（后台 HTTPS 检查）',
        embed: '高级智能体原生界面', endSession: '结束工作台会话', saved: '配置已保存。',
    });
    Object.assign(messages.en, {check:'Test saved connections', checking:'Testing connections without model calls or SAP changes…', checked:'Connection checks completed.', passed:'Passed', failed:'Failed', certificate_untrusted:'Certificate not trusted by backend HTTPS', embed:'Native Advanced Agent UI', endSession:'End workbench session'});
    Object.assign(messages['zh-Hant'], {check:'測試已儲存連線', checking:'正在測試連線，不呼叫模型或修改 SAP…', checked:'連線測試完成，請查看各項結果。', passed:'通過', failed:'失敗', certificate_untrusted:'後台 HTTPS 尚未信任憑證', embed:'高級智能體原生介面', endSession:'結束工作階段'});
    Object.assign(messages.en, {manualControl: 'Take control / pause', autoControl: 'Allow chat control', sessionStarting: 'Preparing SAP and Advanced Agent…', sessionReady: 'Connected. Manual control.', automaticReady: 'Chat can now operate this SAP page.', noSessions: 'No session to resume. Create one first.'});
    Object.assign(messages['zh-Hant'], {manualControl: '人工接管 / 暫停', autoControl: '允許對話操作', sessionStarting: '正在準備 SAP 與高級智能體…', sessionReady: '工作階段已連線，目前由人工控制。', automaticReady: '已允許對話操作目前的 SAP 頁面。', noSessions: '尚無可恢復的工作階段，請先新增。'});
    // The conversation is a platform agent session the page mounts from its own
    // embed URL. There is no engine start left to narrate; what the user needs
    // is whether that session is ready, and the one safe action when it is not.
    Object.assign(messages.zh, {sessionPreparing: '正在打开智能体会话…',
        codingLoading: '正在打开对话…', codingNotReady: '智能体会话尚未就绪。',
        codingRetry: '重试', codingRetryHint: '重新打开同一智能体会话',
        codingOpenFailed: '无法打开智能体会话：{reason}', codingLinkedOk: '新会话已加入历史。',
        codingAttachFailed: '新会话未能加入历史：{reason}',
        codingDisabled: '平台智能体能力未启用。', codingUnavailable: '平台智能体服务不可用。',
        coding_upstream_unavailable: '平台智能体服务暂时不可达，请稍后重试。',
        coding_invalid_request: '智能体会话请求被拒绝，请重新打开工作台。',
        coding_not_linked: '该历史会话没有关联的平台智能体会话，无法恢复。'});
    Object.assign(messages.en, {sessionPreparing: 'Opening the agent session…',
        codingLoading: 'Opening the conversation…', codingNotReady: 'The agent session is not ready yet.',
        codingRetry: 'Retry', codingRetryHint: 'Open the same agent session again',
        codingOpenFailed: 'Could not open the agent session: {reason}', codingLinkedOk: 'The new session joined the history.',
        codingAttachFailed: 'The new session could not join the history: {reason}',
        codingDisabled: 'The platform agent capability is off.', codingUnavailable: 'The platform agent service is unavailable.',
        coding_upstream_unavailable: 'The platform agent service is temporarily unreachable. Please retry shortly.',
        coding_invalid_request: 'The agent session request was refused. Please reopen the workbench.',
        coding_not_linked: 'This history entry has no linked platform agent session, so it cannot be reopened.'});
    Object.assign(messages['zh-Hant'], {sessionPreparing: '正在開啟智能體工作階段…',
        codingLoading: '正在開啟對話…', codingNotReady: '智能體工作階段尚未就緒。',
        codingRetry: '重試', codingRetryHint: '重新開啟同一智能體工作階段',
        codingOpenFailed: '無法開啟智能體工作階段：{reason}', codingLinkedOk: '新工作階段已加入歷史。',
        codingAttachFailed: '新工作階段未能加入歷史：{reason}',
        codingDisabled: '平台智能體能力未啟用。', codingUnavailable: '平台智能體服務不可用。',
        coding_upstream_unavailable: '平台智能體服務暫時無法連線，請稍後重試。',
        coding_invalid_request: '智能體工作階段請求遭拒，請重新開啟工作台。',
        coding_not_linked: '該歷史工作階段沒有關聯的平台智能體工作階段，無法恢復。'});
    Object.assign(messages.zh, {
        browser_capacity_exhausted: '工作台并发已满，请结束闲置会话后重试。',
        local_runtime_origin_required: '当前浏览器节点仅支持本机 Web / 桌面工作台。',
        bound_session_required: '请新建或恢复工作台会话。',
        sap_origin_forbidden: 'SAP 页面尚未就绪，请检查登录或证书提示。',
        mcp_login_failed: 'MCP 登录失败，请检查配置账号和网关服务。',
        config_conflict: '连接配置已更新。请新建会话使用新配置；历史会话保持原目标。',
        platform_login_required: '平台登录已过期，请重新登录后恢复工作台。',
        session_forbidden: '当前账号已无权访问此工作台，请检查账号与租户。',
        session_closed: '工作台已结束，请恢复已有会话。',
    });
    Object.assign(messages.en, {
        opencode_runtime: 'Advanced Agent runtime', mcp_runtime: 'MCP runtime',
        opencode_runtime_missing: 'Local Bun, Advanced Agent source or dependencies are missing.',
        browser_service_unavailable: 'Select an available local browser node.',
        mcp_check_timeout: 'MCP connection check timed out. Check the gateway services.',
        localBrowserNode: 'Dedicated local browser', unavailableBrowserNode: 'Current node unavailable',
        readyToStart: 'Ready to create a session', capabilityNotes: 'Available capabilities',
        navigation_limited: 'Screen navigation: partially available. Delivery of a navigation command is confirmed, not a completed login or business result.',
        mcp_business_available: 'SAP business data: available. Chat reads and writes business data through the registered server-side connection, per session; SAP credentials and connection ids are never handed to the model.',
        mcp_credentials_missing: 'SAP business data: not ready. No MCP account password is saved, so the server cannot connect and every data call fails with mcp_login_failed.',
        page_readwrite_unavailable: 'Page read/write and business submission: not available. Reading or filling SAP page fields and submitting documents are not offered.',
        desktop_page_read_conditional: 'Page reading requires an updated macOS desktop and your active SAP workbench. Rendered content only; filling and submission remain unavailable.',
        setupHint: 'A new session ends your previous workbench sessions. SAP Web GUI is embedded directly; open AI chat at the bottom right. Chat uses configured MCP tools; operate the SAP page directly.',
        checkHint: 'Connections loaded. Create or resume a session, or test the saved connections.', saved: 'Configuration saved.',
        sap_login_required: 'Sign in on the SAP page first.', browser_not_connected: 'SAP screen is not connected yet.',
        model_credentials_missing: 'The selected Advanced Agent model has no API credential.',
        runtime_assets_or_project_missing: 'Advanced Agent Web assets or the configured project directory are missing.',
        opencode_host_failed: 'The scene Advanced Agent service could not start.', model_configuration_unsupported: 'This model configuration is not supported.',
        session_not_running: 'Resume the workbench session first.', control_changed: 'Manual control took over. Read the page again.',
        browser_capacity_exhausted: 'Workbench capacity reached. End an idle session and retry.',
        local_runtime_origin_required: 'This browser worker currently supports the local Web and desktop workbench.',
        bound_session_required: 'Create or resume a workbench session.', sap_origin_forbidden: 'Check the SAP sign-in or certificate screen.',
        mcp_login_failed: 'MCP sign-in failed. Check the configured account and gateway.',
        config_conflict: 'Connection settings changed. Create a new session; history retains its original target.',
        platform_login_required: 'Platform sign-in expired. Sign in again, then resume the workbench.',
        session_forbidden: 'This account can no longer access the workbench. Check the account and tenant.',
        session_closed: 'The workbench ended. Resume an existing session.',
        sessionConnecting: 'Chat is ready. Connecting the SAP page…',
        opencode_session_mismatch: 'The existing session does not match the configured project. Check the connection settings.',
    });
    Object.assign(messages['zh-Hant'], {
        opencode_runtime: '高級智能體執行環境', mcp_runtime: 'MCP 執行環境',
        opencode_runtime_missing: '本機 Bun、高級智能體原始碼或依賴尚未準備。',
        browser_service_unavailable: '請選擇可用的本機瀏覽器節點。',
        mcp_check_timeout: 'MCP 連線檢測逾時，請檢查閘道服務。',
        localBrowserNode: '本機專屬瀏覽器', unavailableBrowserNode: '目前節點不可用',
        readyToStart: '可建立工作階段', capabilityNotes: '目前能力說明',
        navigation_limited: '畫面導覽：有限可用。只確認導覽指令送達，不代表已完成登入或取得業務結果。',
        mcp_business_available: 'SAP 業務資料：可用。對話按工作階段經伺服器已登記的連線讀寫業務資料；SAP 憑證與連線識別不下發給模型。',
        mcp_credentials_missing: 'SAP 業務資料：未就緒。尚未儲存 MCP 帳號密碼，伺服器無法建立連線，資料呼叫會以 mcp_login_failed 失敗。',
        page_readwrite_unavailable: '頁面讀寫與業務提交：不可用。讀取、填寫左側 SAP 頁面欄位與提交業務單據均不開放。',
        desktop_page_read_conditional: '頁面讀取：需使用新版 macOS 桌面並開啟自己的 SAP 工作台。僅讀取已呈現內容；填寫與業務提交不可用。',
        setupHint: '新增工作階段會自動結束目前帳號的舊工作階段。SAP Web GUI 直接嵌入；右下角 AI 對話展開高級智能體。對話使用已設定的 MCP，SAP 頁面由你直接操作。',
        checkHint: '連線設定已載入。可新增或恢復工作階段，或測試已儲存的連線。', saved: '設定已儲存。',
        sap_login_required: '請先在 SAP 頁面登入。', browser_not_connected: 'SAP 畫面尚未連線。',
        model_credentials_missing: '選取的高級智能體模型尚未設定金鑰。',
        runtime_assets_or_project_missing: '高級智能體介面資源或設定的專案目錄不存在。',
        opencode_host_failed: '場景高級智能體服務啟動失敗。', model_configuration_unsupported: '此模型設定尚未支援。',
        session_not_running: '請先恢復工作階段。', control_changed: '目前由人工接管，請重新讀取頁面。',
        browser_capacity_exhausted: '工作階段已滿，請先結束閒置工作階段。',
        local_runtime_origin_required: '此瀏覽器節點目前支援本機 Web 與桌面工作台。',
        bound_session_required: '請新增或恢復工作階段。', sap_origin_forbidden: '請檢查 SAP 登入或憑證提示。',
        mcp_login_failed: 'MCP 登入失敗，請檢查設定帳號與閘道服務。',
        config_conflict: '連線設定已更新。請新增工作階段；歷史工作階段保留原目標。',
        platform_login_required: '平台登入已過期，請重新登入後恢復工作台。',
        session_forbidden: '目前帳號已無權存取此工作台，請檢查帳號與租戶。',
        session_closed: '工作台已結束，請恢復既有工作階段。',
        sessionConnecting: '對話已就緒，正在連線 SAP 頁面…',
        opencode_session_mismatch: '原工作階段與設定的專案不一致，已停止恢復。請核對連線設定。',
    });
    Object.assign(messages.zh, {
        opencode_auth_failed: '高级智能体认证失败，请检查服务连接配置。',
        opencode_assets_missing: '本机高级智能体界面资源尚未准备。', project_directory_missing: '配置的项目目录不存在。',
        mcp_runtime_missing: 'MCP 运行环境不存在。', mcp_credentials_required: '请先保存 MCP 账号密码。',
        credential_crypto_unavailable: '凭据加密服务不可用。', mcp_identity_tools_missing: 'MCP 缺少登录或身份核验工具。',
        mcp_identity_mismatch: 'MCP 返回的系统、Client 或用户与配置不一致。',
        mcp_not_configured: '尚未启用 MCP 连接。', mcp_login_protocol_unsupported: 'MCP 登录协议不受支持。',
    });
    Object.assign(messages.en, {
        opencode_auth_failed: 'Advanced Agent authentication failed. Check the service connection settings.',
        opencode_assets_missing: 'Local Advanced Agent UI assets are missing.', project_directory_missing: 'The configured project directory does not exist.',
        mcp_runtime_missing: 'The MCP runtime is missing.', mcp_credentials_required: 'Save the MCP username and password first.',
        credential_crypto_unavailable: 'Credential encryption is unavailable.', mcp_identity_tools_missing: 'MCP login or identity tools are missing.',
        mcp_identity_mismatch: 'The MCP system, Client or user does not match the configuration.',
        mcp_not_configured: 'No MCP connection is enabled.', mcp_login_protocol_unsupported: 'The MCP login protocol is unsupported.',
    });
    Object.assign(messages['zh-Hant'], {
        opencode_auth_failed: '高級智能體驗證失敗，請檢查服務連線設定。',
        opencode_assets_missing: '本機高級智能體介面資源尚未準備。', project_directory_missing: '設定的專案目錄不存在。',
        mcp_runtime_missing: 'MCP 執行環境不存在。', mcp_credentials_required: '請先儲存 MCP 帳號密碼。',
        credential_crypto_unavailable: '憑據加密服務不可用。', mcp_identity_tools_missing: 'MCP 缺少登入或身分驗證工具。',
        mcp_identity_mismatch: 'MCP 傳回的系統、Client 或使用者與設定不一致。',
        mcp_not_configured: '尚未啟用 MCP 連線。', mcp_login_protocol_unsupported: 'MCP 登入協定不受支援。',
    });
    Object.assign(messages.zh, {opencode: 'SAP智能助手', chatToggle: 'AI 对话', chatCollapse: '收起对话', chatFullscreen: '全屏', chatRestore: '还原', chatResize: '调整助手宽度',
        accountRule: '下方凭据用于场景连接检测及受管 MCP 桥；原生助手的 MCP 使用高级智能体项目配置。',
        loginHint: 'SAP 页面单独登录；原生助手沿用高级智能体的 MCP 配置。',
        flagsHint: '这些开关管理场景能力；原生高级智能体工具和 MCP 按自身配置及权限运行。'});
    Object.assign(messages.en, {opencode: 'SAP AI Assistant', chatToggle: 'AI chat', chatCollapse: 'Collapse chat', chatFullscreen: 'Full screen', chatRestore: 'Restore', chatResize: 'Resize assistant',
        accountRule: 'These credentials serve scene connection checks and the managed MCP bridge. Native chat uses Advanced Agent project MCP configuration.',
        loginHint: 'Sign in to SAP separately. Native chat uses Advanced Agent MCP configuration.',
        flagsHint: 'These flags control scene capabilities. Native Advanced Agent tools and MCP use their own configuration and permissions.',
        setupHint: 'A new session ends your previous workbench sessions. SAP Web GUI is embedded directly; open AI chat at the bottom right. Chat uses configured MCP tools; operate the SAP page directly.',
        sap_login_required: 'Sign in on the SAP page first.'});
    Object.assign(messages['zh-Hant'], {opencode: 'SAP智慧助手', chatToggle: 'AI 對話', chatCollapse: '收起對話', chatFullscreen: '全螢幕', chatRestore: '還原', chatResize: '調整助手寬度',
        accountRule: '下方憑據用於場景連線檢測及受管 MCP 橋；原生助手的 MCP 使用高級智能體專案設定。',
        loginHint: 'SAP 頁面單獨登入；原生助手沿用高級智能體的 MCP 設定。',
        flagsHint: '這些開關管理場景能力；原生高級智能體工具與 MCP 按自身設定及權限執行。',
        setupHint: '新增工作階段會自動結束目前帳號的舊工作階段。SAP Web GUI 直接嵌入；右下角 AI 對話展開高級智能體。對話使用已設定的 MCP，SAP 頁面由你直接操作。',
        sap_login_required: '請先在 SAP 頁面完成登入。'});
    const t = key => {
        const lang = typeof currentLang === 'string' ? currentLang : document.documentElement.lang;
        return (messages[lang] || messages.zh)[key] || messages.zh[key] || messages.zh.failure;
    };
    const el = (tag, text, className) => {
        const node = document.createElement(tag);
        if (text !== undefined) node.textContent = text;
        if (className) node.className = className;
        return node;
    };
    const button = (key, action) => {
        const node = el('button', t(key)); node.type = 'button';
        node.addEventListener('click', action); return node;
    };
    const find = name => dialog.querySelector('[data-sap="' + name + '"]');
    // The embed reports from its own origin, which is the frame's origin, not
    // this page's. Comparing against it pins the listener to the one frame the
    // scene mounted, so a message from anywhere else is ignored unread.
    function originOf(url) {
        try { return new URL(url, window.location.href).origin; } catch (error) { return null; }
    }
    async function request(path, options) {
        const response = await window.fetch(API + path, {credentials: 'same-origin', cache: 'no-store', ...options});
        const payload = await response.json();
        if (!response.ok || payload.status !== 'success') {
            const code = payload.code || (response.status === 401 ? 'unauthorized' : 'failure');
            throw new Error(code === 'unauthorized' ? 'platform_login_required' : code === 'forbidden' ? 'session_forbidden' : code);
        }
        return payload;
    }
    function noticeText(text, error) {
        const node = find('notice'); node.textContent = text; node.dataset.error = error ? 'true' : 'false';
    }
    function notice(key, error) {
        noticeText(t(key), error);
    }
    // The conversation pane is the platform's own coding iframe. The scene has no
    // engine stage left to poll: the pane reports itself ready over the platform's
    // embed protocol, and until it does the pane says what it knows and offers the
    // one safe action -- open that same coding session again.
    const READY_TIMEOUT_MS = 15000;
    let codingReadyTimer = null;
    // One status line per pane, found rather than remembered, so a remount can
    // never leave a second one behind whichever element is the live pane.
    const codeStateBox = () => find('code-pane')?.querySelectorAll('.sap-code-state')[0] || null;
    function clearCodingState() {
        if (codingReadyTimer !== null) { window.clearTimeout(codingReadyTimer); codingReadyTimer = null; }
        codeStateBox()?.remove();
    }
    function showCodingState(text, {retry = false} = {}) {
        const pane = find('code-pane');
        if (!pane) return;
        let box = codeStateBox();
        if (!box) {
            box = el('div', undefined, 'sap-code-state');
            pane.append(box);
        }
        box.setAttribute('role', retry ? 'alert' : 'status');
        box.replaceChildren(el('span', text));
        if (retry) box.append(button('codingRetry', () => retryCodingSession()));
    }
    function markCodingReady() {
        if (codingReadyTimer !== null) { window.clearTimeout(codingReadyTimer); codingReadyTimer = null; }
        codeStateBox()?.remove();
        dressCodeFrame();
    }
    // The scene owns this pane's chrome. Inside the frame the platform's compact
    // session/changes tabs and Advanced Agent's own (now empty) title strip form a
    // second header above the one this dialog already renders, so both are
    // trimmed in the frame's own document. The frame is same-origin with this
    // page, which is what lets the *shared* Web build stay untouched -- the same
    // rule the retired engine used to inject while serving its own assets. A
    // cross-origin embed cannot be dressed this way and simply keeps OpenCode's
    // defaults rather than failing.
    const CODE_FRAME_TRIM = 'header:has(#opencode-titlebar-right),' +
        '[data-slot="tabs-list"]:has([data-value="session"]):has([data-value="changes"]){display:none!important}';
    function dressCodeFrame() {
        let doc = null;
        try { doc = (codeFrame && codeFrame.contentDocument) || null; } catch (error) { doc = null; }
        if (!doc || !doc.head) return;
        const found = typeof doc.getElementById === 'function' ? doc.getElementById('sap-code-trim') : null;
        if (found) return;
        const style = doc.createElement('style');
        style.id = 'sap-code-trim';
        style.textContent = CODE_FRAME_TRIM;
        doc.head.append ? doc.head.append(style) : doc.head.appendChild(style);
    }
    function allowDiscard() { return !dirty || window.confirm(t('unsaved')); }
    function close() {
        if (!dialog?.open) return;
        if (!allowDiscard()) return;
        pauseSession(); generation++; startingSession = false; clearCodingState(); dirty = false; data = null; stopView(); unmountCode(); binding = null;
        dialog.dataset.live = 'false'; dialog.close();
        find('form').replaceChildren();
        previousFocus?.focus();
    }
    function returnHome() {
        close();
        // A rejected unsaved-settings confirmation must keep this scene open.
        if (!dialog.open && typeof window.navigateTo === 'function') window.navigateTo('chat');
    }
    function build() {
        if (dialog) return;
        const style = el('link'); style.rel = 'stylesheet';
        style.href = '/scene-assets/sap_workbench/frontend/workbench.css?v=20261006-compact'; document.head.appendChild(style);
        dialog = el('dialog'); dialog.id = 'sap-workbench-dialog';
        dialog.setAttribute('aria-labelledby', 'sap-workbench-title');
        dialog.dataset.desktop = String(Boolean(window.desktopHost || window.electronAPI));
        dialog.innerHTML = '<header><div class="sap-heading"><button type="button" data-sap="home"></button><div><p class="sap-kicker">SAP · 高级智能体</p><h1 id="sap-workbench-title"></h1></div></div><nav data-sap="toolbar"></nav></header>' +
            '<p data-sap="notice" role="status" aria-live="polite"></p>' +
            '<main data-sap="main"><section class="sap-overview"><span class="sap-badge" data-sap="badge"></span><p data-sap="hint"></p>' +
            '<div data-sap="actions" class="sap-actions"></div><div data-sap="checks" class="sap-checks"></div></section>' +
            '<div class="sap-panes" data-sap="panes"><section class="sap-pane" data-sap="sap-pane"><h2></h2><div class="sap-view-controls" data-sap="view-controls"></div><div class="sap-view" data-sap="view" hidden></div><div class="sap-empty" data-sap="empty"><span aria-hidden="true">▧</span><p></p></div></section>' +
            '<div class="sap-chat-resizer" data-sap="chat-resizer" role="separator" aria-orientation="vertical" aria-controls="sap-chat-pane" tabindex="0" hidden></div>' +
            '<section id="sap-chat-pane" class="sap-pane" data-sap="code-pane" role="region" aria-labelledby="sap-chat-title" hidden inert><header class="sap-chat-header"><h2 id="sap-chat-title"></h2><nav><button type="button" data-sap="chat-fullscreen" aria-pressed="false"></button><button type="button" data-sap="chat-close"></button></nav></header><div class="sap-page-context" data-sap="page-context"><button type="button" data-sap="read-page"></button><p data-sap="page-capture" role="status" aria-live="polite"></p><p data-sap="prompt-feedback" role="status" aria-live="polite" hidden></p></div><section class="sap-starters" data-sap="starters" hidden></section><div class="sap-empty"><span aria-hidden="true">◇</span><p></p></div></section>' +
            '<button type="button" class="sap-chat-toggle" data-sap="chat-toggle" aria-controls="sap-chat-pane" aria-expanded="false" hidden></button></div>' +
            '<details class="sap-details"><summary data-sap="details-title"></summary><ul data-sap="notes"></ul><ul data-sap="blockers"></ul></details></main>' +
            '<form data-sap="form" hidden></form>';
        document.body.appendChild(dialog);
        dialog.addEventListener('cancel', event => {
            event.preventDefault();
            if (chatFullscreen && binding) setChatFullscreen(false, true);
            else if (chatOpen && binding) setChatOpen(false, true);
            else close();
        });
        dialog.addEventListener('input', event => { if (event.target.closest('form')) dirty = true; });
        dialog.addEventListener('change', event => { if (event.target.closest('form')) dirty = true; });
        find('home').addEventListener('click', returnHome);
        find('read-page').addEventListener('click', () => sendAssistantPrompt('promptRead', true));
        find('chat-toggle').addEventListener('click', () => setChatOpen(!chatOpen, true));
        find('chat-close').addEventListener('click', () => setChatOpen(false, true));
        find('chat-fullscreen').addEventListener('click', () => setChatFullscreen(!chatFullscreen));
        const resizer = find('chat-resizer');
        resizer.addEventListener('pointerdown', event => {
            if (event.button !== 0 || !chatOpen || chatFullscreen || find('panes').getBoundingClientRect().width <= 720) return;
            event.preventDefault();
            chatDrag = {id: event.pointerId, x: event.clientX, width: find('code-pane').getBoundingClientRect().width};
            resizer.setPointerCapture(event.pointerId); dialog.dataset.chatResizing = 'true';
        });
        resizer.addEventListener('pointermove', event => {
            if (chatDrag?.id === event.pointerId) setChatWidth(chatDrag.width - (event.clientX - chatDrag.x));
        });
        for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) resizer.addEventListener(type, () => { const changed = Boolean(chatDrag); stopChatResize(); if (changed) saveLayout(); });
        resizer.addEventListener('keydown', event => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key) || !chatOpen || chatFullscreen) return;
            event.preventDefault();
            const width = find('code-pane').getBoundingClientRect().width;
            setChatWidth(event.key === 'Home' ? 320 : event.key === 'End' ? find('panes').getBoundingClientRect().width : width + (event.key === 'ArrowLeft' ? 24 : -24));
            saveLayout();
        });
        window.addEventListener('resize', () => {if (dialog.open) applyLayoutWidth();});
    }
    function showSettings() {
        if (!data?.can_manage) return;
        if (!find('form').hidden) return;
        pauseSession(); generation++; startingSession = false; stopView(); unmountCode(); binding = null;
        dialog.dataset.live = 'false';
        find('main').hidden = true; find('form').hidden = false;
        notice('checkHint');
        renderForm(); find('form').querySelector('input, select')?.focus();
    }
    function showMain() {
        if (!allowDiscard()) return;
        generation++; dirty = false; find('form').replaceChildren(); find('form').hidden = true; find('main').hidden = false; render(); notice('checkHint');
    }
    function render() {
        dialog.dataset.live = binding ? 'true' : 'false';
        dialog.querySelector('h1').textContent = t('title');
        find('home').textContent = t('home');
        find('toolbar').replaceChildren(button('refresh', load));
        if (data?.can_manage) find('toolbar').prepend(button('settings', showSettings));
        find('badge').textContent = t(data?.capabilities.visual ? 'readyToStart' : 'pending'); find('hint').textContent = t(data?.can_manage ? 'setupHint' : 'memberHint');
        const start = button('start', () => startSession(false));
        const resume = button('resume', () => startSession(true));
        start.disabled = resume.disabled = startingSession || !data?.capabilities.visual || Boolean(binding);
        find('actions').replaceChildren(start, resume);
        if (binding) {
            const actions = [button('endSession', endSession)];
            if (binding.display_mode !== 'iframe') actions.unshift(button('manualControl', () => controlSession('manual')), button('autoControl', () => controlSession('automatic')));
            find('actions').replaceChildren(...actions);
        }
        for (const [name, title, text] of [['sap-pane','sap','emptySap'], ['code-pane','opencode','emptyCode']]) {
            find(name).querySelector('h2').textContent = t(title); find(name).querySelector('.sap-empty p').textContent = t(text);
        }
        find('details-title').textContent = t('capabilityNotes');
        find('notes').replaceChildren(...(data?.capabilities.notes || []).map(key => el('li', t(key))));
        find('blockers').replaceChildren(...(data?.capabilities.blockers || []).map(key => el('li', t(key))));
        find('checks').replaceChildren(...(data?.checks || []).map(check => {
            const node = el('div'); node.append(el('strong', t(check.id)), el('span', t(check.configuration)), el('small', t(check.verification))); return node;
        }));
        renderView();
        renderChat();
        renderLoginMemory();
        renderAssistantTools();
    }
    function renderLoginMemory() {
        find('toolbar').querySelector('[data-sap-login]')?.remove();
        const state = view, session = binding;
        if (!state?.loginMemory || !session) return;
        const remember = button(state.loginMemory.enabled ? 'forgetSap' : 'rememberSap', async () => {
            remember.disabled = true;
            try {
                const result = await window.desktopHost.manageSapLogin({binding_id:session.binding_id, tenant_id:state.tenantId,
                    action:state.loginMemory.enabled ? 'forget' : 'enable'});
                if (view !== state || binding !== session) return;
                state.loginMemory = result; renderLoginMemory();
            } catch (error) {
                if (view === state) { remember.title = t(error.code || 'sap_login_unavailable'); remember.textContent = remember.title; }
            } finally { remember.disabled = false; }
        });
        const menu = el('details', undefined, 'sap-account-menu'); menu.dataset.sapLogin = 'true';
        const summary = el('summary', t('accountMenu'));
        const panel = el('div', undefined, 'sap-account-panel');
        panel.append(el('p', t('memoryHint')), remember);
        const status = el('span', t(state.loginMemory.enabled ? (state.loginMemory.credentialsSaved ? 'memorySaved' : 'memoryOn') : 'memoryOff'), 'sap-memory-status');
        summary.append(status); menu.append(summary, panel);
        if (state.loginMemory.error) remember.title = t(state.loginMemory.error);
        else if (state.loginMemory.enabled) remember.title = t('sapRemembered');
        find('toolbar').append(menu);
    }
    function renderChat() {
        const available = Boolean(binding), expanded = available && chatOpen;
        const toggle = find('chat-toggle'), pane = find('code-pane'), collapse = find('chat-close');
        toggle.hidden = !available; toggle.textContent = t('chatToggle'); toggle.title = t('chatToggle');
        toggle.setAttribute('aria-expanded', String(expanded));
        pane.hidden = !expanded; pane.inert = !expanded;
        collapse.textContent = t('chatCollapse');
        collapse.setAttribute('title', t('chatCollapse'));
        collapse.setAttribute('aria-label', t('chatCollapse'));
        dialog.dataset.chatFullscreen = String(expanded && chatFullscreen);
        const fullscreen = find('chat-fullscreen');
        fullscreen.textContent = t(chatFullscreen ? 'chatRestore' : 'chatFullscreen');
        fullscreen.setAttribute('title', fullscreen.textContent);
        fullscreen.setAttribute('aria-label', fullscreen.textContent);
        fullscreen.setAttribute('aria-pressed', String(expanded && chatFullscreen));
        find('chat-resizer').hidden = !expanded || chatFullscreen;
        applyLayoutWidth();
    }
    function updateChatResizer() {
        const resizer = find('chat-resizer'), total = find('panes').getBoundingClientRect().width;
        resizer.setAttribute('aria-label', t('chatResize'));
        resizer.setAttribute('aria-valuemin', '320');
        resizer.setAttribute('aria-valuemax', String(Math.max(320, Math.round(total - 566))));
        resizer.setAttribute('aria-valuenow', String(Math.round(find('code-pane').getBoundingClientRect().width)));
    }
    function setChatWidth(width) {
        const total = find('panes').getBoundingClientRect().width;
        if (total <= 720) return;
        const chatWidth = Math.round(Math.min(Math.max(320, total - 566), Math.max(320, width)));
        layout.ratio = Math.min(0.8, Math.max(0.1, chatWidth / total));
        dialog.style.setProperty('--sap-chat-width', chatWidth + 'px');
        updateChatResizer();
    }
    function applyLayoutWidth() {
        const total = find('panes').getBoundingClientRect().width;
        // Shrinking the window must not overwrite the user's preferred ratio.
        const width = Math.max(320, Math.min(total - 566, total * layout.ratio));
        dialog.style.setProperty('--sap-chat-width', Math.round(width) + 'px');
        updateChatResizer();
    }
    async function loadLayout() {
        if (layoutLoaded) return;
        layoutLoaded = true;
        try {
            const saved = window.desktopHost?.sapWorkbenchLayout
                ? await window.desktopHost.sapWorkbenchLayout({action:'load'})
                : JSON.parse(window.localStorage?.getItem('sap-workbench-layout') || 'null');
            if (saved && Number.isFinite(saved.ratio) && saved.ratio >= 0.1 && saved.ratio <= 0.8 && typeof saved.open === 'boolean') layout = saved;
        } catch (_) { /* Older desktops keep the default layout. */ }
        applyLayoutWidth();
    }
    function saveLayout() {
        const saved = {ratio:layout.ratio, open:layout.open};
        if (window.desktopHost?.sapWorkbenchLayout) {
            void window.desktopHost.sapWorkbenchLayout({action:'save', ...saved}).catch(() => promptFeedback('layoutFailed'));
        } else { try { window.localStorage?.setItem('sap-workbench-layout', JSON.stringify(saved)); } catch (_) { promptFeedback('layoutFailed'); } }
    }
    function stopChatResize() {
        const drag = chatDrag; chatDrag = null; dialog.dataset.chatResizing = 'false';
        const resizer = find('chat-resizer');
        if (drag && resizer.hasPointerCapture(drag.id)) resizer.releasePointerCapture(drag.id);
    }
    function setChatFullscreen(fullscreen, focus = false) {
        stopChatResize();
        chatFullscreen = Boolean(fullscreen && chatOpen && binding);
        renderChat();
        if (focus) find('chat-fullscreen').focus({preventScroll: true});
    }
    function setChatOpen(open, focus = false) {
        stopChatResize();
        chatOpen = Boolean(open && binding);
        if (focus) { layout.open = chatOpen; saveLayout(); }
        if (!chatOpen) chatFullscreen = false;
        renderChat();
        if (focus && binding) find(chatOpen ? 'chat-close' : 'chat-toggle').focus({preventScroll: true});
    }
    function sessionRequest(body) {
        return request('/sessions', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    }
    async function startSession(resume) {
        if (startingSession || binding) return;
        startingSession = true; render(); notice('sessionStarting');
        const current = ++generation;
        try {
            let body, requestId = null;
            if (resume) {
                const list = await request('/sessions');
                if (current !== generation || !dialog.open) return;
                const previous = list.sessions.find(session => session.state !== 'closed');
                if (!previous) throw new Error('noSessions');
                body = {binding_id: previous.id};
            } else {
                requestId = pendingRequest || window.crypto.randomUUID();
                pendingRequest = requestId;
                body = {request_id: requestId};
            }
            const session = await sessionRequest(body);
            if (current !== generation || !dialog.open) return;
            pendingRequest = null; binding = session; binding.requestId = requestId;
            stopView(); connectView(session); void mountCode(session, requestId);
            notice(session.display_mode === 'iframe' ? 'sapEmbedded' : 'sessionConnecting');
        } catch (error) {
            if (current === generation) notice(error.message, true);
        } finally {
            if (current === generation) {
                startingSession = false;
                if (dialog.open) render();
            }
        }
    }
    function pauseSession() {
        if (binding) void sessionRequest({binding_id: binding.binding_id, action: 'control', control: 'manual'}).catch(() => {});
    }
    async function endSession() {
        if (!binding) return;
        const current = binding, epoch = generation;
        pauseSession(); stopView(); unmountCode(); binding = null; render(); notice('checkHint');
        try { await sessionRequest({binding_id: current.binding_id, action: 'close'}); }
        catch (error) { if (epoch === generation && dialog.open && !binding) notice(error.message, true); }
    }
    async function controlSession(control) {
        if (!binding) return;
        const current = binding, epoch = generation;
        try {
            await sessionRequest({binding_id: binding.binding_id, action: 'control', control});
            if (current !== binding || epoch !== generation) return;
            notice(control === 'automatic' ? 'automaticReady' : 'sessionReady');
        } catch (error) { if (current === binding && epoch === generation) notice(error.message, true); }
    }
    function unmountCode() {
        // Bump first: an in-flight mount for the previous frame is now stale and
        // must not append its frame or arm its readiness wait when it resolves.
        codeMount++;
        stopChatResize();
        clearCodingState();
        composer = null; promptRequest = null; window.clearTimeout(promptTimer);
        if (find('starters')) find('starters').hidden = true;
        codeFrame?.remove(); codeFrame = null;
        codeOrigin = null; codeChannel = null; attachSent.clear();
        chatOpen = false;
        chatFullscreen = false;
        dialog.dataset.chatFullscreen = 'false';
        const pane = find('code-pane');
        if (pane) {pane.hidden = true; pane.inert = true;}
        find('chat-resizer').hidden = true;
        const toggle = find('chat-toggle');
        if (toggle) {toggle.hidden = true; toggle.setAttribute('aria-expanded', 'false');}
        const empty = find('code-pane')?.querySelector('.sap-empty');
        if (empty) empty.hidden = false;
    }
    function pageOrigin() {
        const location = window.location;
        return (location && location.origin) || '';
    }
    function codingFrameUrl(url, channel) {
        const params = [];
        if (!/[?&]rsm_embed=/.test(url)) params.push('rsm_embed=1');
        params.push('rsm_parent_origin=' + encodeURIComponent(pageOrigin()));
        params.push('rsm_scene=sap_workbench');
        params.push('rsm_channel=' + encodeURIComponent(channel));
        return url + (url.includes('?') ? '&' : '?') + params.join('&');
    }
    async function codingRequest(path, options) {
        const response = await window.fetch(path, {credentials: 'same-origin', cache: 'no-store', ...options});
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || payload.status !== 'success') {
            // A refusal is reported as text rather than a bare status: "coding is
            // off" and "the service is unreachable" call for different actions.
            const text = typeof payload.message === 'string' && payload.message;
            throw new Error(text || t(payload.code || 'codingUnavailable'));
        }
        return payload;
    }
    // The platform coding entry owns the upstream session: it reserves one for a
    // request id, or reopens the session a history entry already points at.
    // Neither path sends a prompt or calls a model -- opening a conversation must
    // never spend tokens.
    function openCodingSession(session, requestId) {
        if (!session.agent_id) return Promise.reject(new Error(t('codingUnavailable')));
        if (requestId) {
            return codingRequest('/api/coding/sessions', {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({agent_id: session.agent_id, request_id: requestId,
                    permission_profile: CODING_PERMISSION_PROFILE})});
        }
        if (!session.coding_session_id) return Promise.reject(new Error(t('coding_not_linked')));
        return codingRequest('/api/coding/sessions/' + encodeURIComponent(session.coding_session_id)
            + '/open?agent_id=' + encodeURIComponent(session.agent_id)).then(async opened => {
                if (typeof session.desktop_sap_page_read_enabled === 'boolean') {
                    // Reapply the current scene tool profile to historical sessions
                    // through the existing verified attach, without creating a chat.
                    await codingRequest('/api/coding/sessions/attach', {method:'POST',
                        headers:{'Content-Type':'application/json'},
                        body:JSON.stringify({agent_id:session.agent_id, source_session_id:session.coding_session_id,
                            external_session_id:opened.external_session_id, scene_binding_id:session.binding_id,
                            permission_profile:CODING_PERMISSION_PROFILE})});
                }
                return opened;
            });
    }
    function mountCode(session, requestId) {
        const wasOpen = layout.open, wasFullscreen = chatFullscreen;
        unmountCode();
        // ``screen`` is the self-managed per-session engine, kept only behind the
        // rollback switch. It authenticates with a one-use grant posted into the
        // frame, and it is the only remaining display mode that does.
        if (session.display_mode !== 'iframe') {
            mountLegacyCode(session, wasOpen, wasFullscreen);
            return;
        }
        void mountPlatformCode(session, requestId, wasOpen, wasFullscreen, ++codeMount);
    }
    function mountLegacyCode(session, wasOpen, wasFullscreen) {
        const frame = el('iframe', undefined, 'sap-opencode-frame');
        frame.title = t('opencode'); frame.name = 'sap-code-' + session.binding_id;
        frame.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms allow-downloads');
        frame.referrerPolicy = 'no-referrer';
        codeFrame = frame; codeOrigin = originOf(session.base_url || session.origin); codeChannel = session.binding_id;
        find('code-pane').append(frame);
        find('code-pane').querySelector('.sap-empty').hidden = true;
        // A one-use, short-lived grant is posted into the iframe, never placed
        // in its URL, browser history, localStorage or the OpenCode project.
        // The binding rides in the query: on the fixed shared origin it is the
        // only thing that names this session before its cookie exists, and the
        // dispatcher forwards the prefixed path straight to the session host.
        const form = el('form'); form.method = 'POST';
        form.action = (session.base_url || session.origin) + '/bootstrap?binding=' + encodeURIComponent(session.binding_id);
        form.target = frame.name;
        const input = el('input'); input.type = 'hidden'; input.name = 'token'; input.value = session.bootstrap_token;
        form.append(input); document.body.append(form); form.submit(); form.remove();
        delete session.bootstrap_token;
        setChatOpen(wasOpen);
        setChatFullscreen(wasFullscreen);
    }
    async function mountPlatformCode(session, requestId, wasOpen, wasFullscreen, mount) {
        const current = session;
        // The placeholder belongs to "no conversation yet". From here on the pane
        // either shows the frame or says why it cannot, so it goes now rather than
        // after the network round trip.
        const empty = find('code-pane').querySelector('.sap-empty');
        if (empty) empty.hidden = true;
        showCodingState(t('sessionPreparing'));
        let opened = null;
        try {
            opened = await openCodingSession(session, requestId);
        } catch (error) {
            if (mount !== codeMount || binding !== current || !dialog.open) return;
            // The reason is already the user's language: either the platform's own
            // sentence or a localized code from ``codingRequest``.
            const text = t('codingOpenFailed').replace('{reason}', error.message);
            showCodingState(text, {retry: true}); noticeText(text, true);
            return;
        }
        // A retry that landed while this open was in flight supersedes it: mount
        // only the newest attempt, or the pane would grow a second frame and a
        // second readiness wait for the same conversation.
        if (mount !== codeMount || binding !== current || !dialog.open) return;
        if (!opened.iframe_url) { showCodingState(t('codingNotReady'), {retry: true}); return; }
        if (opened.session_id) binding.coding_session_id = opened.session_id;
        if (opened.external_session_id) binding.remote_session_id = opened.external_session_id;
        // The channel is minted here, per mount, and echoed by the client inside
        // the frame. It is what makes "this browser tab's request_id now points at
        // that upstream session" a first-class, auditable fact instead of a guess.
        const channel = 'sap-ch-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
        const frame = el('iframe', undefined, 'sap-opencode-frame');
        frame.title = t('opencode'); frame.name = 'sap-code-' + session.binding_id;
        frame.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms allow-downloads');
        frame.referrerPolicy = 'no-referrer';
        frame.src = codingFrameUrl(opened.iframe_url, channel);
        frame.addEventListener('load', dressCodeFrame);
        codeFrame = frame; codeOrigin = originOf(opened.iframe_url); codeChannel = channel;
        find('code-pane').append(frame);
        find('code-pane').querySelector('.sap-empty').hidden = true;
        dressCodeFrame();
        showCodingState(t('codingLoading'));
        codingReadyTimer = window.setTimeout(() => {
            codingReadyTimer = null;
            if (mount !== codeMount) return;
            showCodingState(t('codingNotReady'), {retry: true});
        }, READY_TIMEOUT_MS);
        setChatOpen(wasOpen);
        setChatFullscreen(wasFullscreen);
    }
    function retryCodingSession() {
        if (!binding || !dialog.open) return;
        const current = binding;
        void mountCode(current, current.requestId);
    }
    // A client that already had its own session reports the id it switched to.
    // The scene records that link under the caller's own binding, so the next
    // page load resumes the conversation the user is actually looking at.
    async function attachCodingSession(externalId) {
        if (!binding || !externalId || externalId === binding.remote_session_id || attachSent.has(externalId)) return;
        const current = binding;
        attachSent.add(externalId);
        try {
            const result = await codingRequest('/api/coding/sessions/attach', {method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({agent_id: binding.agent_id, source_session_id: binding.coding_session_id,
                    ...(typeof binding.desktop_sap_page_read_enabled === 'boolean' ? {scene_binding_id: binding.binding_id} : {}),
                    external_session_id: externalId, permission_profile: CODING_PERMISSION_PROFILE})});
            if (binding !== current) return;
            if (result.session_id) binding.coding_session_id = result.session_id;
            binding.remote_session_id = result.external_session_id || externalId;
            renderAssistantTools();
            notice('codingLinkedOk');
        } catch (error) {
            if (binding !== current) return;
            attachSent.delete(externalId);
            noticeText(t('codingAttachFailed').replace('{reason}', error.message), true);
        }
    }
    window.addEventListener('message', event => {
        if (!binding || !codeFrame || event.source !== codeFrame.contentWindow) return;
        if (codeOrigin && event.origin !== codeOrigin) return;
        const message = event.data;
        if (!message || typeof message !== 'object' || message.channel !== codeChannel) return;
        if (binding.display_mode !== 'iframe') {
            // The retired engine reports the session it is serving through the
            // scene's own proxy. A different one means this pane no longer shows
            // the bound conversation, so both panes are released rather than
            // left pointing at a session nobody asked for.
            if (message.type === 'rsm.opencode.session' && message.session_id !== binding.remote_session_id) {
                pauseSession(); generation++; stopView(); unmountCode(); binding = null; render();
                notice('session_not_running', true);
            }
            return;
        }
        if (message.type === 'rsm.opencode.composer' && typeof message.session_id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(message.session_id) &&
            typeof message.empty === 'boolean' && typeof message.busy === 'boolean' && typeof message.hasDraft === 'boolean') {
            composer = message; renderAssistantTools(); return;
        }
        if (message.type === 'rsm.opencode.prompt-result' && message.request_id === promptRequest?.id) {
            window.clearTimeout(promptTimer);
            const submitted = promptRequest.submit; promptRequest = null;
            const errors = {draft:'promptDraft', busy:'promptBusy', unavailable:'promptUnavailable'};
            promptFeedback(message.status === 'accepted' ? (submitted ? '' : 'promptFilled') : (errors[message.status] || 'promptUnavailable'));
            renderAssistantTools(); return;
        }
        if (message.type === 'rsm.opencode.ready') { markCodingReady(); return; }
        if (message.type === 'rsm.opencode.session' && message.session_id) void attachCodingSession(message.session_id);
    });
    function promptFeedback(key) {
        const node = find('prompt-feedback');
        node.hidden = !key; node.textContent = key ? t(key) : '';
    }
    function renderAssistantTools() {
        const current = composer?.session_id === binding?.remote_session_id ? composer : null;
        const controls = find('page-context');
        controls.hidden = !binding || binding.display_mode !== 'iframe';
        const read = find('read-page'); read.textContent = t('readPage');
        read.disabled = !current || current.busy || Boolean(promptRequest) || !view?.readSupported;
        read.title = view?.readSupported ? t('readPage') : t('readUnavailable');
        const capture = find('page-capture');
        if (pageCapture) {
            const snapshot = t('readCaptured').replace('{title}', pageCapture.title).replace('{time}', new Date(pageCapture.time).toLocaleTimeString());
            capture.textContent = pageCapture.stale ? t('readStale').replace('{snapshot}', snapshot) : snapshot;
            if (pageFeedback) capture.textContent = t(pageFeedback) + ' · ' + capture.textContent;
        } else capture.textContent = t(pageFeedback || 'readIdle');
        const starters = find('starters');
        starters.hidden = !current?.empty;
        starters.replaceChildren(el('h3', t('starterTitle')), el('p', t('starterHint')));
        const actions = el('div', undefined, 'sap-starter-actions');
        for (const [label, prompt, submit] of [['summarizePage','promptSummary',true], ['explainError','promptError',true], ['openTransaction','promptTransaction',false]]) {
            const action = button(label, () => sendAssistantPrompt(prompt, submit));
            action.disabled = !current || current.busy || Boolean(promptRequest) || (submit && !view?.readSupported);
            actions.append(action);
        }
        starters.append(actions);
    }
    function sendAssistantPrompt(key, submit) {
        if (!composer || composer.session_id !== binding?.remote_session_id || !codeFrame || !binding || !codeOrigin || promptRequest) { promptFeedback('promptUnavailable'); return; }
        if (composer.busy) { promptFeedback('promptBusy'); return; }
        if (composer.hasDraft) { promptFeedback('promptDraft'); return; }
        const id = crypto.randomUUID();
        promptRequest = {id, submit}; promptFeedback(''); renderAssistantTools();
        codeFrame.contentWindow.postMessage({type:'rsm.opencode.prompt', channel:codeChannel, session_id:binding.remote_session_id,
            request_id:id, text:t(key), submit}, codeOrigin);
        promptTimer = window.setTimeout(() => {
            if (promptRequest?.id !== id) return;
            promptRequest = null; promptFeedback('promptUnavailable'); renderAssistantTools();
        }, 8000);
    }
    // The left pane is a live view of a dedicated Chrome. Frames come down the
    // loopback WebSocket as JPEG; clicks and keys go back up as semantic events
    // that the gateway turns into CDP input. It is started explicitly (never on
    // render) so opening the dialog costs nothing until the user asks for SAP.
    function renderView() {
        const controls = find('view-controls');
        const host = find('view');
        const empty = find('empty');
        if (!controls || !host || !empty) return;
        controls.replaceChildren();
        if (binding && !view) {
            const node = button(view ? 'viewStop' : 'viewStart', view ? stopView : startView);
            node.disabled = Boolean(view?.starting);
            controls.append(node);
        }
        host.hidden = !view;
        empty.hidden = Boolean(view);
        const fallback = 'emptySap';
        empty.querySelector('p').textContent = t(view?.message || fallback);
    }
    function startView() {
        if (view || !binding) return;
        const current = binding;
        reconnects = 0;
        view = {starting: true, message: 'viewStarting'};
        renderView();
        sessionRequest({binding_id: binding.binding_id})
            .then(session => {
                if (current !== binding || !dialog.open || !view?.starting) return;
                // A re-issued session can point at a different upstream
                // conversation. Carry the request id that reopens it and reload
                // the pane, so SAP and the conversation stay on one binding.
                session.requestId = binding.requestId;
                const relinked = session.remote_session_id !== binding.remote_session_id
                    || session.coding_session_id !== binding.coding_session_id
                    || session.origin !== binding.origin;
                binding = session;
                connectView(session);
                if (relinked) void mountCode(session, session.requestId);
            })
            .catch(error => {
                if (current !== binding || !dialog.open) return;
                view = null; renderView(); notice(error.message, true);
            });
    }
    function connectView(session) {
        view?.disposeResize?.();
        if (session.display_mode === 'iframe') {
            const frame = el('iframe', undefined, 'sap-native-frame');
            frame.title = t('sap'); frame.name = 'sap-gui-' + session.binding_id;
            frame.referrerPolicy = 'no-referrer';
            find('view').replaceChildren(frame);
            view = {starting: false, frame, message: 'sapEmbedded', tenantId:window.sessionStorage?.getItem('cow_tenant_id') || ''};
            const state = view;
            renderView();
            const open = () => {
                if (view !== state || binding !== session || !dialog.open) return;
                frame.src = session.sap_url;
                watchEmbeddedSession(session, state);
                renderLoginMemory();
            };
            if (window.desktopHost?.manageSapLogin) {
                state.loginMemory = {enabled:false};
                window.desktopHost.manageSapLogin({binding_id:session.binding_id, tenant_id:state.tenantId, action:'prepare'})
                    .then(result => { if (view === state) state.loginMemory = result; })
                    .catch(error => { state.loginMemory = {enabled:false, error:error.code || 'sap_login_unavailable'}; }).finally(open);
            } else open();
            return;
        }
        const host = find('view');
        const canvas = el('canvas', undefined, 'sap-canvas');
        canvas.tabIndex = 0;
        canvas.setAttribute('aria-label', t('sap'));
        const input = el('textarea', undefined, 'sap-input-proxy');
        input.setAttribute('aria-label', t('sap'));
        input.autocomplete = 'off'; input.spellcheck = false; input.tabIndex = -1;
        host.replaceChildren(canvas, input);
        // The token rides as the WebSocket sub-protocol: a browser cannot set an
        // Authorization header on a socket, and a query token would leak into
        // logs and history.
        const socket = new window.WebSocket('ws://127.0.0.1:' + session.port + '/screen', [session.token]);
        socket.binaryType = 'blob';
        view = {starting: false, socket, canvas, readonly: Boolean(session.readonly),
                message: 'viewLive', pending: null, drawing: false, moved: 0};
        renderView();
        canvas.focus({preventScroll:true});
        socket.addEventListener('message', event => onViewFrame(event, canvas));
        socket.addEventListener('close', () => onViewClosed(socket));
        socket.addEventListener('error', () => {
            if (view?.socket !== socket) return;
            view.message = 'runtime_unavailable';
            renderView();
        });
        bindViewInput(canvas, socket, input);
        view.disposeResize = watchViewSize(host, canvas, socket);
    }
    function watchEmbeddedSession(session, state) {
        let timer;
        let appliedNavigation = null;
        const viewId = crypto.randomUUID();
        const readProtocol = typeof session.desktop_sap_page_read_enabled === 'boolean';
        let disposed = false, supported = false, reading = null, documentVersion = 0;
        pageCapture = null; pageFeedback = ''; state.readSupported = false;
        const loaded = () => { documentVersion++; if (pageCapture) pageCapture.stale = true; renderAssistantTools(); };
        state.frame.addEventListener('load', loaded);
        const live = () => !disposed && view === state && binding === session && dialog.open;
        if (session.desktop_sap_page_read_enabled && window.desktopHost?.readSapPage) {
            window.desktopHost.getCapabilities().then(capabilities => { if (live()) { supported = capabilities.sapPageRead === true; state.readSupported = supported; renderAssistantTools(); } }).catch(() => {});
        }
        const collect = command => {
            if (!supported || command.view_id !== viewId || command.expires_at <= Date.now()) return;
            if (reading?.id === command.id) return;
            pageFeedback = 'readPending'; renderAssistantTools();
            const current = reading = {id: command.id, expires: command.expires_at, version: documentVersion, ready: false};
            window.desktopHost.readSapPage({binding_id: session.binding_id, read_id: command.id, view_id: viewId})
                .then(result => { current.result = result; })
                .catch(error => { current.error = error.code || 'extract_failed'; })
                .finally(() => { current.ready = true; });
        };
        const ping = async () => {
            if (!live()) return;
            try {
                const result = await sessionRequest({binding_id: session.binding_id, action: 'heartbeat',
                    ...(readProtocol ? {view_id: viewId, page_read_supported: supported} : {})});
                if (!live()) return;
                const readCommand = result.page_read?.command;
                if (!readCommand || result.page_read?.available !== true) {
                    if (reading && pageFeedback === 'readPending') { pageFeedback = 'readFailed'; renderAssistantTools(); }
                    reading = null;
                }
                else {
                    collect(readCommand);
                    const current = reading;
                    if (current?.ready && current.expires > Date.now()) {
                        const error = current.version !== documentVersion ? 'page_changed' : current.error;
                        await sessionRequest({binding_id: session.binding_id, action: 'read_result', read_id: current.id,
                            view_id: viewId, ...(error ? {read_error: error} : {read_result: current.result})});
                        if (!live()) return;
                        pageFeedback = error ? ({login_required:'sap_login_required', page_read_unsupported:'readUnavailable', page_read_unavailable:'readUnavailable', page_changed:'readChanged'}[error] || 'readFailed') : '';
                        if (!error && current.version === documentVersion && typeof current.result?.capturedAt === 'string' && Number.isFinite(Date.parse(current.result.capturedAt))) {
                            pageCapture = {title:String(current.result.title || t('pageUntitled')).slice(0,300), time:current.result.capturedAt, stale:false};
                        }
                        renderAssistantTools();
                        if (reading === current) reading = null;
                    }
                }
                const command = result.navigation;
                if (command && Number.isFinite(command.expires_at) && Date.now() < command.expires_at) {
                    const base = new URL(session.sap_url), target = new URL(command.url);
                    // Only the saved SAP endpoint is a navigation destination.
                    if (result.binding_id !== session.binding_id || typeof command.id !== 'string' ||
                        target.origin !== base.origin || target.pathname !== base.pathname ||
                        target.username || target.password ||
                        target.searchParams.get('~transaction') !== command.transaction) {
                        throw new Error('invalid_transaction');
                    }
                    if (appliedNavigation !== command.id) {
                        state.frame.src = target.href;
                        appliedNavigation = command.id;
                    }
                    // Acknowledge application of src, not SAP login/business success.
                    // Retry only the acknowledgement if its response was lost.
                    await sessionRequest({binding_id: session.binding_id, action: 'navigation_ack', navigation_id: command.id});
                }
            } catch (error) {
                if (view !== state || binding !== session) return;
                if (['session_closed', 'session_not_running', 'platform_login_required', 'session_forbidden', 'config_conflict', 'disabled'].includes(error.message)) {
                    stopView(); unmountCode(); binding = null; render(); notice(error.message, true);
                    return;
                }
            }
            if (live()) timer = window.setTimeout(ping, 1000);
        };
        timer = window.setTimeout(ping, 1000);
        state.disposeResize = () => {
            disposed = true; reading = null; window.clearTimeout(timer);
            state.frame.removeEventListener('load', loaded);
            if (readProtocol) void sessionRequest({binding_id: session.binding_id, action: 'heartbeat',
                view_id: viewId, page_read_supported: false, view_active: false}).catch(() => {});
        };
    }
    function watchViewSize(host, canvas, socket) {
        const state = view;
        let timer = null, observer, lastSize = '';
        const sendSize = () => {
            timer = null;
            if (view !== state || view.canvas !== canvas || socket.readyState !== 1) return;
            const rect = host.getBoundingClientRect();
            if (rect.width < 1 || rect.height < 1) return;
            // Keep the aspect ratio even for unusually large displays.
            const scale = Math.min(1, 4096 / rect.width, 4096 / rect.height);
            const width = Math.max(1, Math.round(rect.width * scale));
            const height = Math.max(1, Math.round(rect.height * scale));
            const size = width + 'x' + height;
            if (size === lastSize) return;
            socket.send(JSON.stringify({t: 'resize', width, height}));
            lastSize = size;
        };
        const schedule = () => {
            if (timer !== null) window.clearTimeout(timer);
            timer = window.setTimeout(sendSize, 120);
        };
        socket.addEventListener('open', schedule);
        if (typeof window.ResizeObserver === 'function') {
            observer = new window.ResizeObserver(schedule);
            observer.observe(host);
        } else window.addEventListener('resize', schedule);
        return () => {
            observer?.disconnect();
            window.removeEventListener?.('resize', schedule);
            if (timer !== null) window.clearTimeout(timer);
        };
    }
    // A dropped socket is usually transient (a reload, a sleeping laptop, a tab
    // switch). Retry a few times against the same node before giving the
    // browser up, so the SAP login the user just performed survives.
    function onViewClosed(socket) {
        if (view?.socket !== socket) return;
        if (reconnects >= RETRIES.length) { stopView(); notice('viewEnded'); return; }
        const wait = RETRIES[reconnects];
        reconnects += 1;
        view.message = 'viewReconnecting';
        renderView();
        notice('viewReconnecting');
        retryTimer = window.setTimeout(() => retryView(socket), wait);
    }
    function retryView(socket) {
        retryTimer = null;
        if (view?.socket !== socket || !dialog.open) return;
        const current = binding;
        sessionRequest({binding_id: binding.binding_id})
            .then(session => {
                if (current !== binding || !dialog.open || view?.socket !== socket) return;
                session.requestId = binding.requestId;
                const relinked = session.remote_session_id !== binding.remote_session_id
                    || session.coding_session_id !== binding.coding_session_id
                    || session.origin !== binding.origin;
                binding = session;
                connectView(session);
                // A re-issued session can name a new gateway or a new upstream
                // conversation. Restore both panes to the same binding, not only SAP.
                if (relinked) void mountCode(session, session.requestId);
            })
            .catch(error => {
                if (current !== binding || view?.socket !== socket) return;
                if (['session_closed', 'platform_login_required', 'unauthorized', 'session_forbidden', 'forbidden', 'config_conflict', 'disabled'].includes(error.message)) {
                    stopView(); unmountCode(); binding = null; render();
                    notice(error.message === 'unauthorized' ? 'platform_login_required' : error.message === 'forbidden' ? 'session_forbidden' : error.message, true);
                    return;
                }
                onViewClosed(socket);
            });
    }
    async function onViewFrame(event, canvas) {
        if (view?.canvas !== canvas) return;
        if (typeof event.data === 'string') {
            try {
                const message = JSON.parse(event.data);
                if (message.t === 'control' && view?.canvas === canvas && ['manual', 'automatic'].includes(message.control)) {
                    notice(message.login_required ? 'sap_login_required' : message.control === 'automatic' ? 'automaticReady' : 'sessionReady');
                }
                if (message.t === 'ready' && message.viewport) {
                    canvas.width = message.viewport.width || canvas.width || 1280;
                    canvas.height = message.viewport.height || canvas.height || 800;
                    if (view?.canvas === canvas && !view.ready) {
                        view.ready = true;
                        reconnects = 0;
                        notice('sessionReady');
                    }
                }
            } catch (error) { /* a control frame we do not understand is ignorable */ }
            return;
        }
        const state = view;
        if (!state || state.canvas !== canvas) return;
        // Keep only the newest frame while an older one is still decoding, so a
        // busy screen cannot build an unbounded backlog.
        state.pending = event.data;
        if (state.drawing) return;
        state.drawing = true;
        try {
            while (state.pending) {
                const blob = state.pending; state.pending = null;
                const bitmap = await createImageBitmap(blob);
                if (view !== state) {
                    if (typeof bitmap.close === 'function') bitmap.close();
                    state.pending = null;
                    break;
                }
                const context = canvas.getContext('2d');
                if (!canvas.width || !canvas.height) {
                    canvas.width = bitmap.width; canvas.height = bitmap.height;
                }
                context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
                if (typeof bitmap.close === 'function') bitmap.close();
            }
        } catch (error) {
            if (view === state) { state.message = 'runtime_unavailable'; renderView(); }
        } finally {
            state.drawing = false;
        }
    }
    function bindViewInput(canvas, socket, input) {
        // object-fit:contain can add letterboxing when the pane is resized.
        // Map the actual image, not the surrounding canvas element box.
        const geometry = () => {
            const rect = canvas.getBoundingClientRect();
            const scale = Math.min(rect.width / canvas.width, rect.height / canvas.height);
            const w = canvas.width * scale, h = canvas.height * scale;
            return {w, h, left: rect.left + (rect.width - w) / 2, top: rect.top + (rect.height - h) / 2};
        };
        const box = () => {const {w, h} = geometry(); return {w, h};};
        const point = event => {
            const rect = geometry();
            return {x: event.clientX - rect.left, y: event.clientY - rect.top};
        };
        const send = payload => {
            if (view?.canvas === canvas && !view.readonly && socket.readyState === 1) {
                socket.send(JSON.stringify(payload));
                // The server revokes automatic control for every deliberate
                // human input. Do not leave the previous "chat can operate"
                // notice visible after the user takes over the SAP pane.
                if (payload.t !== 'mouse' || payload.event !== 'move') notice('sessionReady');
            }
        };
        const mouse = (event, name) => {
            if (view?.readonly) return;
            const {x, y} = point(event);
            send({t: 'mouse', event: name, x, y, button: name === 'move' ? undefined : 'left', canvas: box()});
        };
        // Canvas cannot receive IME/beforeinput text. Keep a real editable
        // target focused, forwarding completed text and immediately clearing
        // it so even login input does not remain in the host document.
        let composing = false;
        const flushText = () => {
            const text = input.value;
            input.value = '';
            if (text) send({t: 'text', text});
        };
        input.addEventListener('compositionstart', () => {composing = true;});
        input.addEventListener('compositionend', () => {composing = false; flushText();});
        input.addEventListener('input', event => {
            if (!composing && !event.isComposing) flushText();
        });
        input.addEventListener('blur', () => {composing = false; input.value = '';});
        canvas.addEventListener('focus', () => input.focus({preventScroll:true}));
        canvas.addEventListener('mousedown', event => {event.preventDefault(); input.focus({preventScroll:true}); mouse(event, 'down');});
        canvas.addEventListener('mouseup', event => {event.preventDefault(); mouse(event, 'up');});
        canvas.addEventListener('mousemove', event => {
            if (view?.readonly) return;
            // Throttle moves to ~30/s; SAP does not need every pixel, and the
            // frame stream is the real back-pressure.
            const now = Date.now();
            if (view && now - view.moved < 33) return;
            if (view) view.moved = now;
            mouse(event, 'move');
        });
        canvas.addEventListener('contextmenu', event => event.preventDefault());
        canvas.addEventListener('wheel', event => {
            event.preventDefault();
            const {x, y} = point(event);
            send({t: 'mouse', event: 'wheel', x, y, deltaX: event.deltaX, deltaY: event.deltaY, canvas: box()});
        }, {passive: false});
        const keydown = event => {
            if (view?.readonly || event.code === 'F5' || composing || event.isComposing || event.keyCode === 229) return;
            const shortcut = event.ctrlKey || event.metaKey || event.altKey;
            // Paste uses the actual local clipboard event; sending Cmd/Ctrl+V
            // as well would paste the dedicated browser's unrelated clipboard.
            if ((event.ctrlKey || event.metaKey) && event.code === 'KeyV') return;
            if (!shortcut && event.key.length === 1) return;
            event.preventDefault();
            send({t: 'key', code: event.code, key: event.key,
                ctrl: event.ctrlKey, alt: event.altKey, shift: event.shiftKey, meta: event.metaKey});
        };
        const paste = event => {
            const text = event.clipboardData && event.clipboardData.getData('text');
            if (!text) return;
            event.preventDefault();
            input.value = '';
            send({t: 'text', text});
        };
        for (const target of [canvas, input]) {
            target.addEventListener('keydown', keydown);
            target.addEventListener('paste', paste);
        }
    }
    function stopView() {
        const state = view;
        state?.disposeResize?.();
        view = null;
        reconnects = 0;
        if (retryTimer !== null) { window.clearTimeout(retryTimer); retryTimer = null; }
        if (state?.socket) {
            try { state.socket.close(); } catch (error) { /* already closing */ }
        }
        if (state?.pending) state.pending = null;
        const host = find('view');
        if (host) host.replaceChildren();
        renderView();
    }
    async function load() {
        if (!allowDiscard()) return false;
        const current = ++generation; startingSession = false; clearCodingState(); dirty = false; data = null; if (!binding) stopView();
        find('form').inert = false;
        find('form').hidden = true; find('main').hidden = false;
        render(); notice('loading');
        try {
            const result = await request('/config');
            if (current !== generation || !dialog.open) return false;
            data = result; render(); notice('checkHint');
            return true;
        } catch (error) {
            if (current === generation && dialog.open) notice(error.message, true);
            return false;
        }
    }
    function field(parent, name, label, value, options) {
        const wrapper = el('label', undefined, 'sap-field'); wrapper.append(el('span', t(label)));
        const node = el(options ? 'select' : name === 'origins' ? 'textarea' : 'input'); node.name = name;
        if (options) options.forEach(([id, text]) => {
            const opt = el('option', text); opt.value = id; node.append(opt);
        });
        node.value = value == null ? '' : String(value); wrapper.append(node); parent.append(wrapper); return node;
    }
    function checkbox(parent, name, label, checked) {
        const wrapper = el('label', undefined, 'sap-checkbox'); const node = el('input');
        node.type = 'checkbox'; node.name = name; node.checked = checked;
        wrapper.append(node, el('span', t(label))); parent.append(wrapper);
    }
    function mcpRow(parent, item) {
        const row = el('fieldset', undefined, 'sap-mcp-row'); row.dataset.mcp = 'true';
        row.append(el('legend', item.id));
        field(row, 'url', 'endpoint', item.url).readOnly = true;
        checkbox(row, 'mcp_enabled', 'connectionEnabled', item.enabled !== false);
        parent.append(row);
    }
    function renderForm() {
        const form = find('form'), config = data.config; form.replaceChildren();
        const actions = el('div', undefined, 'sap-actions'); actions.append(button('back', showMain)); form.append(actions);
        const grid = el('div', undefined, 'sap-form-grid'); form.append(grid);
        field(grid, 'system_id', 'system', config.sap.system_id);
        field(grid, 'web_gui_url', 'sapUrl', config.sap.web_gui_url).type = 'url';
        field(grid, 'client', 'client', config.sap.client).maxLength = 3;
        field(grid, 'language', 'language', config.sap.language);
        field(grid, 'origins', 'origins', config.sap.allowed_origins.join('\n'));
        field(grid, 'login_mode', 'loginMode', 'manual', [['manual',t('manual')]]);
        const choices = [['', t('select')], ...data.coding_options.map(v => [v.id,v.name])];
        const agent = field(grid, 'coding_agent_id', 'agent', config.coding_agent_id, choices);
        const project = field(grid, 'project_dir', 'project', data.coding?.project_dir || ''); project.readOnly = true;
        agent.addEventListener('change', () => {project.value = data.coding_options.find(v => v.id === agent.value)?.project_dir || '';});
        field(grid, 'opencode_web', 'webUrl', data.opencode.web_url).readOnly = true;
        const nodes = [['', t('select')], ['sap-browser-worker', t('localBrowserNode')]];
        if (config.browser_service_ref && !nodes.some(([id]) => id === config.browser_service_ref)) {
            nodes.push([config.browser_service_ref, t(config.browser_service_ref === 'local' ? 'localBrowserNode' : 'unavailableBrowserNode')]);
        }
        field(grid, 'browser_service_ref', 'node', config.browser_service_ref, nodes);
        for (const [name,label] of [['max_sessions','capacity'],['idle_seconds','idle']]) {
            const input = field(grid, name, label, config[name]); input.type = 'number';
            input.min = name === 'max_sessions' ? '1' : '60'; input.max = name === 'max_sessions' ? '100' : '86400';
        }
        form.append(el('p', t('projectHint'), 'sap-help'), el('p', t('accountRule'), 'sap-account-rule'), el('p', t('loginHint'), 'sap-help'));
        form.append(el('p', t('fixedMcp'), 'sap-help'));
        const connections = el('div'); connections.dataset.sap = 'mcp-rows'; form.append(connections);
        config.mcp.connections.forEach(item => mcpRow(connections, item));
        const credentials = el('fieldset', undefined, 'sap-form-grid');
        credentials.append(el('legend', t('mcp_credentials')));
        field(credentials, 'mcp_username', 'mcpUsername', config.mcp.username || '').autocomplete = 'off';
        const passwordInput = field(credentials, 'mcp_password', 'mcpPassword', '');
        passwordInput.type = 'password'; passwordInput.autocomplete = 'new-password'; passwordInput.maxLength = 1024;
        credentials.append(el('p', t(data.mcp_password_configured ? 'mcpPasswordSaved' : 'mcpPasswordMissing'), 'sap-help'));
        checkbox(credentials, 'clear_mcp_password', 'mcpClearPassword', false);
        form.append(credentials, el('p', t('mcpCredentialsHint'), 'sap-help'));
        const flags = el('fieldset', undefined, 'sap-flags'); flags.append(el('legend', t('flags')));
        [['enabled','enabled'],['desktop_sap_page_read_enabled','desktopRead'],['automation_enabled','automation'],['commit_enabled','commit']].forEach(([name,label]) => checkbox(flags,name,label,config[name]));
        form.append(flags, el('p', t('flagsHint'), 'sap-help'));
        const footer = el('div', undefined, 'sap-actions'); const save = el('button', t('save')); save.type = 'submit'; footer.append(save);
        footer.append(button('check', async () => {
            const current = generation;
            notice('checking');
            try { const result = await request('/check', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
                if (current === generation && dialog.open) {
                    form.querySelector('[data-sap="probe-results"]')?.remove();
                    const results = el('ul'); results.dataset.sap = 'probe-results';
                    result.checks.forEach(check => {
                        const status = Number.isInteger(check.http_status) ? ' (HTTP ' + check.http_status + ')' : '';
                        const reason = check.reason ? ' · ' + t(check.reason) : '';
                        results.append(el('li', (check.id.startsWith('sap-') ? check.id : t(check.id)) + '：' + t(check.verification) + status + reason));
                    });
                    footer.before(results); notice('checked');
                }
            } catch (error) {if (current === generation && dialog.open) notice(error.message,true);}
        })); form.append(footer);
        form.onsubmit = async event => {
            event.preventDefault(); if (save.disabled) return;
            const current = generation, value = name => form.elements.namedItem(name).value;
            const next = JSON.parse(JSON.stringify(config));
            for (const key of ['system_id','web_gui_url','client','language','login_mode']) next.sap[key] = value(key);
            next.sap.allowed_origins = value('origins').split('\n').map(v=>v.trim()).filter(Boolean);
            next.coding_agent_id = value('coding_agent_id'); next.browser_service_ref = value('browser_service_ref');
            for (const key of ['max_sessions','idle_seconds']) next[key] = Number(value(key));
            for (const key of ['enabled','desktop_sap_page_read_enabled','automation_enabled','commit_enabled']) next[key] = form.elements.namedItem(key).checked;
            next.mcp.connections = config.mcp.connections.map((item, index) => ({...item,
                enabled: connections.children[index].querySelector('[name="mcp_enabled"]').checked,
            }));
            next.mcp.username = value('mcp_username');
            const body = JSON.stringify({version:data.version, config:next,
                mcp_password: passwordInput.value, clear_mcp_password: form.elements.namedItem('clear_mcp_password').checked});
            passwordInput.value = '';
            save.disabled = true; form.inert = true; notice('saving');
            try {
                const result = await request('/config', {method:'PUT',headers:{'Content-Type':'application/json'},body});
                if (current !== generation || !dialog.open) return;
                data = result; dirty = false; render(); renderForm(); notice('saved');
            } catch (error) {if (current === generation && dialog.open) notice(error.message,true);}
            finally {save.disabled = false; if (current === generation) form.inert = false;}
        };
    }
    async function open(options) {
        // The card body requests a new session; its 配置 button opens only
        // settings. An explicit plain open remains a configuration overview.
        const wantSettings = Boolean(options && options.view === 'settings');
        const wantNewSession = Boolean(options && options.view === 'new-session');
        const proceed = () => {
            build();
            if (wantNewSession && dialog.open && (binding || startingSession)) return Promise.resolve();
            if (!dialog.open) {previousFocus = document.activeElement; find('form').inert = false; dialog.showModal();}
            return Promise.all([load(), loadLayout()]).then(function ([loaded]) {
                if (!loaded) return;
                if (wantSettings && data?.can_manage) showSettings();
                else if (wantNewSession && data?.capabilities.visual) return startSession(false);
            });
        };
        if (typeof window.wsGuardUnsaved === 'function' && !window.wsGuardUnsaved(proceed)) return false;
        return proceed();
    }
    window.SapWorkbench = {open, close};
})();
