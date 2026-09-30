(scope) => {
  const root = (scope && document.querySelector(scope)) || document;
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const errs = [...root.querySelectorAll('[aria-invalid="true"], .error-message, .helper-text--error, [class*="error"][role="alert"], [class*="inline-feedback--error"], [class*="_error_"]')]
    .filter(vis).map(e => (e.innerText || e.getAttribute('aria-label') || e.id || 'invalid').trim()).filter(Boolean);
  return errs.slice(0, 10);
}
