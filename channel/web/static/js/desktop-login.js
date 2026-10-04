// The Web login view, completing on the current Desktop authorization URL.
// The browser owns only its Cookie session; consent and PKCE still happen next.
(function () {
    'use strict';
    const form = document.getElementById('login-form');
    const username = document.getElementById('login-username');
    const password = document.getElementById('login-password');
    const error = document.getElementById('login-error');
    const button = document.getElementById('login-btn');
    const toggle = document.getElementById('login-toggle-pwd');

    document.getElementById('auth-check-panel').classList.add('hidden');
    document.getElementById('login-subtitle').textContent = '请输入登录信息以访问控制台';
    form.classList.remove('hidden');
    username.required = true;
    password.required = true;
    username.setAttribute('aria-label', '账号');
    password.setAttribute('aria-label', '密码');
    error.setAttribute('role', 'alert');
    toggle.setAttribute('aria-label', '显示密码');

    window.toggleLoginPassword = function () {
        const show = password.type === 'password';
        password.type = show ? 'text' : 'password';
        toggle.querySelector('i').classList.replace(
            show ? 'fa-eye' : 'fa-eye-slash', show ? 'fa-eye-slash' : 'fa-eye');
        toggle.setAttribute('aria-label', show ? '隐藏密码' : '显示密码');
    };

    form.onsubmit = async function (event) {
        event.preventDefault();
        if (button.disabled) return;
        error.classList.add('hidden');
        button.disabled = true;
        form.setAttribute('aria-busy', 'true');
        button.textContent = '正在登录…';
        let message = '登录未完成，请重试';
        try {
            const response = await fetch('/auth/login', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
                body: JSON.stringify({ username: username.value, password: password.value })
            });
            const result = await response.json();
            if (response.ok && result && result.status === 'success') {
                password.value = '';
                // Preserve state, challenge and callback exactly; a login alone
                // never approves the Desktop request or navigates to its callback.
                location.reload();
                return;
            }
            message = response.status === 401 ? '登录信息有误，请重试'
                : (result && result.message) || message;
        } catch (_) {
            message = '暂时无法登录，请检查网络连接后重试。';
        }
        error.textContent = message;
        error.classList.remove('hidden');
        button.disabled = false;
        button.textContent = '登录';
        form.removeAttribute('aria-busy');
        password.value = '';
        password.focus();
    };
    username.focus();
})();
