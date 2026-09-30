(args) => {
  const vis = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width >= 30 && r.height >= 30 && s.visibility !== 'hidden' && s.display !== 'none'
      && parseFloat(s.opacity || '1') > 0.05 && r.bottom > 0 && r.right > 0
      && r.top < (window.innerHeight || 800) * 3 && r.left < (window.innerWidth || 1200) * 2;
  };
  const shown = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'; };
  for (const f of document.querySelectorAll('iframe')) {
    const src = f.getAttribute('src') || '', title = f.getAttribute('title') || '';
    const both = `${src} ${title}`;
    if (!vis(f)) continue;
    if (/hcaptcha/i.test(both)) {
      if (/frame=(checkbox|challenge)/i.test(src) || /challenge/i.test(title) && !/enclave/i.test(src))
        return 'captcha: hCaptcha';
    }
    if (/recaptcha/i.test(both)) {
      if (/bframe/i.test(src) || /challenge/i.test(title)) return 'captcha: reCAPTCHA challenge';
      if (/anchor/i.test(src) && !/size=invisible/i.test(src)) return 'captcha: reCAPTCHA checkbox';
    }
    if (/challenges\.cloudflare\.com|turnstile/i.test(both)) return 'captcha: Cloudflare Turnstile';
    if (/arkoselabs|funcaptcha/i.test(both)) return 'captcha: Arkose/FunCaptcha';
  }
  for (const el of document.querySelectorAll('.cf-turnstile, [data-turnstile], .h-captcha:not([data-size="invisible"])')) {
    if (vis(el)) return 'captcha: challenge widget';
  }
  if (/^(just a moment|attention required|verify you are human|security check)/i.test(document.title.trim()))
    return 'captcha: bot challenge page';
  const scopeEl = (args.scope && document.querySelector(args.scope)) || document.body;
  // Page text WITHOUT the job's own content (description, job cards): a security posting that
  // says "investigate unusual activity" or "log in to your SIEM" is not a checkpoint.
  let text = '';
  if (document.body) {
    if (args.exclude) {
      const clone = document.body.cloneNode(true);
      clone.querySelectorAll(args.exclude).forEach(e => e.remove());
      clone.querySelectorAll('script, style, noscript, template').forEach(e => e.remove());
      text = (clone.textContent || '').replace(/\s+/g, ' ').slice(0, 30000);
    } else {
      text = document.body.innerText.slice(0, 30000);
    }
  }
  const pw = [...document.querySelectorAll('input[type="password"]')].some(e => {
    const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden';
  });
  if (pw) return 'login_wall: password field on page';
  if (/\b(sign in|log in|login) to (apply|continue)\b|\bcreate (an |your )?account to (apply|continue)\b|\byou must (sign in|log in|be (logged|signed) in)\b|\bsign in to your account to (apply|continue)\b/i.test(text))
    return 'login_wall: sign-in required';
  for (const f of document.querySelectorAll('iframe[src], a[href]')) {
    const u = f.getAttribute('src') || f.getAttribute('href') || '';
    if (/hackerrank\.com|codility\.com|codesignal\.com|testgorilla\.com|criteriacorp\.com|hirevue\.com|pymetrics\.(com|ai)|mettl\.com|harver\.com/i.test(u)
        && scopeEl.contains(f) && (f.tagName === 'A' ? shown(f) : vis(f)))
      return 'assessment: third-party assessment';
  }
  const stext = (scopeEl.innerText || '').slice(0, 30000);
  if (/\b(start|begin|take|complete) (the |your |an |this )?(online |coding |skills? |technical )?(assessment|coding challenge|skills test)\b/i.test(stext))
    return 'assessment: assessment step';
  for (const re of (args.extra || [])) {
    if (new RegExp(re[1], 'i').test(text) || new RegExp(re[1], 'i').test(location.href)) return re[0];
  }
  return null;
}
