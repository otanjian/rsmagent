/* Authentication owns entry; no shared-password startup fallback. */
applyTheme();
applyI18n();
_initAccountMenuChrome();
document.getElementById('account-password-form')?.addEventListener('submit', submitAccountPassword);
refreshAccountIdentity();
if (typeof codingSeam === 'function') codingSeam().wire();
requestAnimationFrame(() => document.body.classList.add('transition-colors', 'duration-200'));
