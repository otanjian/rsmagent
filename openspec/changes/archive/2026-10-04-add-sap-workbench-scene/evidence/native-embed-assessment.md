# 用户提出直接嵌入浏览器的核对

2026-10-04。普通 Web 工作台不能将独立 Chrome 操作系统窗口作为 DOM 元素嵌入；iframe 嵌入的是 URL 对应的网页。当前实现为专属 Chrome + CDP 画面流/input，主界面 canvas 与工具使用同一 browser target，原生 OpenCode 使用独立 iframe。专属 Chrome 使用独立 profile；当前可见窗口供测试登录/证书处理，后台运行仍然属于画面流方案。

用户截图的 `ERR_CERT_AUTHORITY_INVALID` 是该浏览器对 SAP 证书不信任。改用 iframe 不会使证书自动受信任；已有浏览器窗口的登录态或证书例外也不能通过 iframe 直接继承。此核对没有处理浏览器安全警告或更改证书信任。

对用户已授权例外的精确测试源，仅匿名 HEAD 读取 SAP 登录入口（Client 200、语言 zh）的响应头：HTTP 200，未返回 X-Frame-Options 或 Content-Security-Policy。这只表明该登录入口没有在这次响应中声明这两类限制，尚不能证明登录后页面、SSO、Cookie 或浏览器证书下的 iframe 行为。没有传输账号密码或 SAP 业务数据。

若采用原生页面嵌入，Web 端应为 SAP iframe + 右下角 OpenCode iframe，并让浏览器工具连接到实际 iframe 的 SAP 会话；直接把 canvas 换成 URL、继续操作另一个 Chrome，不能满足同一页面操作目标。桌面端可评估 Electron WebContentsView，但涉及桌面接入，不在本次视口修复范围内。当前未实施架构替换。

机制来源：[MDN iframe](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/iframe)、[X-Frame-Options](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/X-Frame-Options)、[Electron Web Embeds](https://www.electronjs.org/docs/latest/tutorial/web-embeds)。
