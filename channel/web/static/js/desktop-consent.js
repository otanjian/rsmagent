// Switching accounts keeps this authorization URL (and the native callback)
// intact. Revoke the old browser session before showing the shared login form.
(function () {
    'use strict';
    const button = document.getElementById('desktop-switch-account');
    if (!button) return; // Password-change notices share the page shell.
    const error = document.getElementById('desktop-switch-error');
    const form = document.querySelector('.consent-form');
    const decisions = Array.from(form.querySelectorAll('button'));
    const editServer = document.getElementById('desktop-edit-server');
    const serverForm = document.getElementById('desktop-server-form');
    if (editServer && serverForm) {
        const input = document.getElementById('desktop-server-origin');
        const serverError = document.getElementById('desktop-server-error');
        const cancel = document.getElementById('desktop-cancel-server');
        const original = input.value;
        const closeEditor = function () {
            serverForm.hidden = true;
            editServer.setAttribute('aria-expanded', 'false');
            serverError.hidden = true;
            input.value = original;
            button.disabled = false;
            decisions.forEach(control => { control.disabled = false; });
            editServer.focus();
        };
        editServer.addEventListener('click', function () {
            if (!serverForm.hidden) { closeEditor(); return; }
            serverForm.hidden = false;
            editServer.setAttribute('aria-expanded', 'true');
            button.disabled = true;
            decisions.forEach(control => { control.disabled = true; });
            input.focus();
            input.select();
        });
        cancel.addEventListener('click', closeEditor);
        input.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') { event.preventDefault(); closeEditor(); }
        });
        serverForm.addEventListener('submit', function (event) {
            let target;
            try { target = new URL(input.value.trim()); } catch (_) { /* invalid below */ }
            if (target && target.origin === new URL(original).origin
                && target.pathname === '/' && !target.username && !target.password
                && !target.search && !target.hash) {
                event.preventDefault();
                closeEditor();
                return;
            }
            if (!target || target.protocol !== 'https:' || target.username || target.password
                || target.pathname !== '/' || target.search || target.hash || target.port === '0'
                || /[\s\\]/.test(input.value.trim())) {
                event.preventDefault();
                serverError.textContent = '请输入 HTTPS 服务器地址，不包含路径、账号或查询参数。';
                serverError.hidden = false;
                input.focus();
                return;
            }
            input.value = target.origin;
            serverError.hidden = true;
            editServer.disabled = true;
            cancel.disabled = true;
            serverForm.querySelector('button[type="submit"]').disabled = true;
            serverForm.setAttribute('aria-busy', 'true');
        });
    }

    button.addEventListener('click', async function () {
        if (button.disabled) return;
        button.disabled = true;
        if (editServer) editServer.disabled = true;
        button.textContent = '正在切换…';
        error.hidden = true;
        decisions.forEach(control => { control.disabled = true; });
        form.setAttribute('aria-busy', 'true');
        try {
            const response = await fetch('/auth/logout', {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Accept': 'application/json' }
            });
            const result = await response.json().catch(() => null);
            if (response.status === 401 || (response.ok && result && result.status === 'success')) {
                // Reload the exact same URL: the old Cookie can no longer
                // authorize, so GET shows login without cancelling the request.
                location.reload();
                return;
            }
        } catch (_) {
            // Keep the current page available for retry if logout is unconfirmed.
        }
        error.textContent = '切换账号未完成，请重试。';
        error.hidden = false;
        button.disabled = false;
        if (editServer) editServer.disabled = false;
        button.textContent = '换个账号登录';
        decisions.forEach(control => { control.disabled = false; });
        form.removeAttribute('aria-busy');
    });
})();
