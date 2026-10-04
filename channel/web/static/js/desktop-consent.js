// Switching accounts keeps this authorization URL (and the native callback)
// intact. Revoke the old browser session before showing the shared login form.
(function () {
    'use strict';
    const button = document.getElementById('desktop-switch-account');
    if (!button) return; // Password-change notices share the page shell.
    const error = document.getElementById('desktop-switch-error');
    const form = document.querySelector('.consent-form');
    const decisions = Array.from(form.querySelectorAll('button'));

    button.addEventListener('click', async function () {
        if (button.disabled) return;
        button.disabled = true;
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
        button.textContent = '换个账号登录';
        decisions.forEach(control => { control.disabled = false; });
        form.removeAttribute('aria-busy');
    });
})();
