(args) => {
  const scope = args.formIndex != null ? document.forms[args.formIndex]
    : (args.scope ? document.querySelector(args.scope) : document.body);
  if (!scope) return [];
  const prefer = args.prefer || ['id', 'name'];
  const cattr = args.containerKeyAttr || null;
  const q = s => String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const uniq = s => { try { return document.querySelectorAll(s).length === 1; } catch (e) { return false; } };
  const sel = el => {
    if (el.id && uniq(`[id="${q(el.id)}"]`)) return `[id="${q(el.id)}"]`;
    const tag = el.tagName.toLowerCase();
    if (el.getAttribute('name')) {
      let s = `${tag}[name="${q(el.getAttribute('name'))}"]`;
      if ((el.type === 'radio' || el.type === 'checkbox') && el.getAttribute('value'))
        s += `[value="${q(el.getAttribute('value'))}"]`;
      if (uniq(s)) return s;
    }
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur.tagName !== 'HTML') {
      if (cur !== el && cur.id && uniq(`[id="${q(cur.id)}"]`)) { parts.unshift(`[id="${q(cur.id)}"]`); break; }
      if (cur !== el && cattr && cur.getAttribute(cattr)) {
        const s = `[${cattr}="${q(cur.getAttribute(cattr))}"]`;
        if (uniq(s)) { parts.unshift(s); break; }
      }
      let i = 1, sib = cur;
      while ((sib = sib.previousElementSibling)) if (sib.tagName === cur.tagName) i++;
      parts.unshift(`${cur.tagName.toLowerCase()}:nth-of-type(${i})`);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  };
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const txt = el => (el ? (el.innerText || el.textContent || '') : '');
  const starred = t => /[*✱]\s*$/.test(t.trim()) || /\(?\brequired\)?\s*$/i.test(t.trim());
  const clean = t => t.replace(/[*✱]/g, '').replace(/\s+/g, ' ').trim()
    .replace(/\s*\(?\brequired\)?$/i, '').trim();
  const LABELISH = 'legend, label, [class*="label"], [class*="question-title"], [class*="title"], h3, h4, [class*="question-text"]';
  const isOptionLabel = l => l.tagName === 'LABEL' && (l.control && (l.control.type === 'radio' || l.control.type === 'checkbox'));
  const CONTROLS = 'input:not([type="hidden"]), select, textarea, [role="combobox"]';
  // Nearest ancestor (<= 6 levels) holding a label-like element that precedes `el`. Stops as soon
  // as an ancestor also contains controls that are not part of this field (`group`).
  const container = (el, group) => {
    group = group || [el];
    let cur = el.parentElement;
    for (let d = 0; cur && d < 6 && cur !== document.body; d++, cur = cur.parentElement) {
      const foreign = [...cur.querySelectorAll(CONTROLS)].some(c => !group.some(g => g === c || g.contains(c)));
      if (foreign) return null;
      for (const l of cur.querySelectorAll(LABELISH)) {
        if (l.contains(el) || isOptionLabel(l)) continue;
        if (l.querySelector('input, select, textarea')) continue;
        if (!(l.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
        if (!clean(txt(l))) continue;
        return {box: cur, label: l};
      }
    }
    return null;
  };
  const ckey = el => {
    if (!cattr) return null;
    const c = el.closest(`[${cattr}]`);
    return c ? c.getAttribute(cattr) : null;
  };
  const keyOf = el => {
    const ck = ckey(el);
    if (ck) return ck;
    for (const p of prefer) { const v = p === 'id' ? el.id : el.getAttribute('name'); if (v) return v; }
    return null;
  };
  // text of a <label>; when the label wraps the control, prefer its inner caption element
  const labelText = (l, el) => {
    if (l.contains(el)) {
      const inner = l.querySelector('[class*="label"], [class*="title"], [class*="text"]');
      if (inner && !inner.contains(el) && clean(txt(inner))) return txt(inner);
    }
    return txt(l);
  };
  const labelOf = (el, group) => {
    if (el.labels && el.labels.length) {
      const own = [...el.labels].filter(l => !(l.control && l.control !== el));
      const t = own.map(l => labelText(l, el)).join(' ');
      if (clean(t)) return {text: t, el: own[0]};
    }
    const lb = el.getAttribute('aria-labelledby');
    if (lb) {
      const els = lb.split(/\s+/).map(i => document.getElementById(i)).filter(Boolean);
      const t = els.map(txt).join(' ');
      if (clean(t)) return {text: t, el: els[0]};
    }
    const c = container(el, group);
    if (c) return {text: txt(c.label), el: c.label};
    if (el.getAttribute('aria-label')) return {text: el.getAttribute('aria-label')};
    if (el.placeholder) return {text: el.placeholder};
    return {text: el.getAttribute('name') || el.id || ''};
  };
  const reqOf = (el, lab) => {
    if (el.required || el.getAttribute('aria-required') === 'true' || starred(lab.text || '')) return true;
    const l = lab.el;
    if (!l) return false;
    if (/(^|[\s_-])required/i.test(String(l.className || ''))) return true;
    return !!l.querySelector('.required, [class*="required"]');
  };
  const trigger = el => {
    const own = el.parentElement && el.parentElement.closest('a, button, [role="button"]');
    if (own && visible(own)) return sel(own);
    const c = container(el);
    const box = c ? c.box : el.parentElement;
    if (!box) return '';
    for (const b of box.querySelectorAll('button, a, [role="button"]')) {
      if (visible(b) && /upload|attach|choose|browse|select file|add file/i.test(txt(b))) return sel(b);
    }
    return '';
  };
  // Controls associated with a form through form="..." live OUTSIDE its subtree but are still
  // submitted with it: include every such control of any form in (or around) the scope.
  const forms = scope.tagName === 'FORM' ? [scope]
    : [...scope.querySelectorAll('form'), scope.closest && scope.closest('form')].filter(Boolean);
  const external = [];
  for (const f of forms) for (const e of f.elements) {
    if (!scope.contains(e) && !external.includes(e)) external.push(e);
  }
  const all = s => [...scope.querySelectorAll(s), ...external.filter(e => e.matches(s))];
  const TRANSPORT_WORDS = new Set(['csrf', 'xsrf', 'csrfmiddlewaretoken', 'authenticity',
    'nonce', 'captcha', 'recaptcha', 'hcaptcha', 'turnstile', 'utm', 'trk', 'fingerprint']);
  const TRANSPORT_NAMES = new Set(['_token', 'token', '_method', 'gh_src', 'gh_jid',
    'session_redirect', 'redirect', 'redirect_url', 'return_to', 'timestamp', 'locale']);
  const transport = (args.transport || []).map(p => new RegExp(`^(?:${p})$`));
  const out = [];
  const used = new Set();
  // Help/description text attached to a field (aria-describedby, or hint text in its
  // container). Conditions often live here ("...in Canada", "without sponsorship").
  const descOf = f => {
    let el = null;
    try { el = document.querySelector(f.selector); } catch (e) { el = null; }
    if (!el) return '';
    const ids = (el.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
    let t = ids.map(i => document.getElementById(i)).filter(Boolean).map(txt).join(' ');
    if (!clean(t)) {
      const c = container(el);
      if (c) {
        const h = c.box.querySelector('[class*="description"], [class*="help"], [class*="hint"], [class*="subtitle"], small');
        if (h && !h.contains(el) && h !== c.label && !c.label.contains(h)) t = txt(h);
      }
    }
    return clean(t).slice(0, 500);
  };
  const push = f => { if (f.description === undefined) f.description = descOf(f); out.push(f); };

  // yes/no button pairs (Ashby "yesno")
  for (const yn of scope.querySelectorAll('[class*="yesno"]')) {
    const btns = [...yn.querySelectorAll('button')];
    if (btns.length < 2 || yn.parentElement.closest('[class*="yesno"]')) continue;
    yn.querySelectorAll('input').forEach(i => used.add(i));
    const lab = labelOf(btns[0], [yn]);
    push({key: keyOf(yn) || keyOf(yn.querySelector('input') || yn), label: clean(lab.text),
          type: 'radio', widget: 'yesno', required: reqOf(yn, lab), selector: sel(yn),
          options: btns.map(b => clean(txt(b))), option_selectors: btns.map(sel),
          current: (btns.find(b => b.getAttribute('aria-pressed') === 'true') || {innerText: ''}).innerText.trim() || null,
          visible: visible(yn), trigger: '', max_length: null});
  }

  // radio/checkbox groups
  const groups = new Map();
  for (const el of all('input[type="radio"], input[type="checkbox"]')) {
    if (used.has(el)) continue;  // aria-hidden ones too: checked ones are still submitted
    const gk = ckey(el) || el.getAttribute('name') || el.id;
    if (!gk) continue;
    if (!groups.has(gk)) groups.set(gk, []);
    groups.get(gk).push(el);
  }
  for (const [gk, els] of groups) {
    els.forEach(e => used.add(e));
    const isRadio = els[0].type === 'radio';
    const optText = e => {
      const own = e.labels && e.labels.length ? clean(txt(e.labels[0])) : '';
      return own || clean(e.getAttribute('aria-label') || '') || e.value || '';
    };
    const fs = els[0].closest('fieldset');
    let lab;
    if (!isRadio && els.length === 1 && !(fs && fs.querySelector('legend'))) {
      const c = container(els[0], els);
      const own = optText(els[0]);
      lab = c ? {text: txt(c.label), el: c.label} : {text: own};
      const single = {key: gk, label: clean(lab.text) || own, type: 'checkbox', widget: 'checkbox',
        required: reqOf(els[0], lab) || (fs && fs.getAttribute('aria-required') === 'true'),
        selector: sel(els[0]), options: own && c ? [own] : [], option_selectors: own && c ? [sel(els[0])] : [],
        current: els[0].checked ? 'true' : null, trigger: '', max_length: null,
        named: !!els[0].name,
        visible: els[0].getAttribute('aria-hidden') !== 'true' &&
                 (visible(els[0]) || [...(els[0].labels || [])].some(visible))};
      push(single);
      continue;
    }
    if (fs && fs.querySelector('legend')) lab = {text: txt(fs.querySelector('legend')), el: fs.querySelector('legend')};
    else { const c = container(els[0], els); lab = c ? {text: txt(c.label), el: c.label} : {text: gk}; }
    const req = els.some(e => e.required) || (fs && fs.getAttribute('aria-required') === 'true') || reqOf(els[0], lab);
    const checked = els.filter(e => e.checked).map(optText);
    push({key: gk, label: clean(lab.text), type: isRadio ? 'radio' : 'multiselect',
          widget: isRadio ? 'radio' : 'checkbox_group', required: !!req,
          selector: fs && fs.id ? sel(fs) : sel(els[0]), options: els.map(optText),
          option_selectors: els.map(sel), current: isRadio ? (checked[0] || null) : (checked.length ? checked : null),
          named: els.some(e => !!e.name),
          visible: els.some(e => e.getAttribute('aria-hidden') !== 'true') &&
                   (els.some(visible) || els.some(e => e.labels && e.labels[0] && visible(e.labels[0]))),
          trigger: '', max_length: null});
  }

  // everything else
  for (const el of all('input, textarea, select')) {
    if (used.has(el)) continue;
    const t = (el.getAttribute('type') || el.type || '').toLowerCase();
    if (t === 'hidden') {
      // Native hidden inputs are usually transport metadata (CSRF, tracking, CAPTCHA tokens) or
      // back a visible widget. Exempt only names made of known metadata words, or names the
      // adapter lists (exact patterns, e.g. Lever's serialized card definitions). Anything else
      // with a value may be an applicant answer the form will submit: verify it.
      const v = (el.value || '').trim();
      if (!el.name || !v) continue;
      if (transport.some(rx => rx.test(el.name))) continue;
      const words = el.name.replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase()
        .split(/[^a-z0-9]+/).filter(Boolean);
      if (words.some(w => TRANSPORT_WORDS.has(w)) || TRANSPORT_NAMES.has(el.name.toLowerCase())) continue;
      const c1 = container(el);
      const backs1 = c1 && [...c1.box.querySelectorAll('input, select, textarea, [role="combobox"]')]
        .some(o => o !== el && o.type !== 'hidden' && visible(o));
      if (backs1) continue;
      const lab = labelOf(el);
      push({key: keyOf(el), label: clean(lab.text) || el.name, required: false, selector: sel(el),
            options: [], option_selectors: [], current: v, visible: false, trigger: '',
            max_length: null, type: 'text', widget: 'hidden_value'});
      continue;
    }
    if (['submit', 'button', 'reset', 'image', 'radio', 'checkbox'].includes(t)) continue;
    // search inputs ARE form fields (typeahead questions); only a widget's own auxiliary
    // filter box or the site's header/nav search is skipped
    if (t === 'search' && el.closest('[role="listbox"], [role="menu"], header, nav')) continue;
    if (el.disabled) continue;
    const isFile = t === 'file';
    // file inputs are usually visually hidden; judge them by their wrapper / label instead
    const shown = el.getAttribute('aria-hidden') !== 'true' && (isFile
      ? (visible(el) || (el.parentElement && visible(el.parentElement))
         || [...(el.labels || [])].some(visible)) : visible(el));
    if (!shown) {
      // Hidden by CSS/aria but enabled: the browser still SUBMITS its value. We can't operate
      // it, so any value it holds must be one you approved (checked before submit), else CP3.
      const v = isFile ? (el.files && el.files.length ? el.files[0].name : '')
        : (el.tagName === 'SELECT' ? [...el.selectedOptions].filter(o => o.value !== '')
             .map(o => o.text.trim()).join(' | ') : el.value);
      // native forms only submit NAMED controls; and a hidden input backing a visible widget in
      // the same field (react-select, custom pickers) is checked through that widget
      const c0 = container(el);
      const backs = c0 && [...c0.box.querySelectorAll('input, select, textarea, [role="combobox"]')]
        .some(o => o !== el && visible(o));
      if (v && el.name && !backs) {
        const lab = labelOf(el);
        push({key: keyOf(el), label: clean(lab.text) || el.name || el.id, required: false,
              selector: sel(el), options: [], option_selectors: [], current: v,
              visible: false, trigger: '', max_length: null, type: isFile ? 'file' : 'text',
              widget: 'hidden_value'});
      }
      continue;
    }
    const lab = labelOf(el);
    const key = keyOf(el);
    const rec = {key, label: clean(lab.text), required: reqOf(el, lab), selector: sel(el),
                 options: [], option_selectors: [], current: null, visible: shown, trigger: '',
                 max_length: el.maxLength > 0 ? el.maxLength : null};
    if (isFile) {
      if (!rec.required) {
        const grp = el.closest('[role="group"][aria-required="true"]');
        if (grp) rec.required = true;
      }
      // some widgets upload, then reset the native input and show the attachment instead
      let attached = el.files && el.files.length ? el.files[0].name : null;
      if (!attached) {
        const c = container(el);
        const box = c ? c.box : el.parentElement;
        const m = box && (box.innerText || '').match(/([\w\-. ()]+\.(?:pdf|docx?|txt|rtf|odt))\b/i);
        if (m) attached = m[1].trim();
      }
      Object.assign(rec, {type: 'file', widget: 'file', trigger: trigger(el), current: attached});
    } else if (el.tagName === 'SELECT') {
      const opts = [...el.options].filter(o => o.value !== '' && !o.disabled);
      // multi-selects: EVERY selected non-placeholder option (saved extras must be visible)
      const picked = [...el.options].filter(o => o.selected && o.value !== '').map(o => o.text.trim());
      const cur = el.multiple ? (picked.length ? picked : null) : (picked[0] || null);
      Object.assign(rec, {type: el.multiple ? 'multiselect' : 'select', widget: 'select',
                          options: opts.map(o => o.text.trim()), current: cur});
    } else if (el.getAttribute('role') === 'combobox') {
      const shell = el.closest('.select-shell') || el.closest('[class*="control"]')
        || el.closest('[class*="inputContainer"]') || el.parentElement;
      const sv = shell && shell.querySelector('[class*="single-value"], [class*="singleValue"]');
      Object.assign(rec, {type: 'select', widget: 'combobox', current: sv ? txt(sv).trim() : (el.value || null)});
    } else if (el.tagName === 'TEXTAREA') {
      Object.assign(rec, {type: 'textarea', widget: 'text', current: el.value || null});
    } else {
      const map = {email: 'email', tel: 'tel', url: 'url', number: 'number', date: 'date'};
      let qt = map[t] || 'text';
      const hint = `${el.className} ${el.placeholder || ''}`;
      if (qt === 'text' && /date/i.test(hint)) qt = 'date';
      Object.assign(rec, {type: qt, widget: qt === 'date' ? 'date' : 'text', current: el.value || null,
                          hint: el.getAttribute('placeholder') || el.getAttribute('data-date-format') || ''});
    }
    push(rec);
  }
  // Custom (non-native) controls: role=combobox/listbox/radiogroup/spinbutton/textbox or
  // contenteditable, rendered with buttons/divs. We can't operate these safely, but they must
  // never be invisible: a required one, or one holding a value, forces CP3.
  const NATIVE = 'input, select, textarea';
  const customs = scope.querySelectorAll('[role="combobox"], [role="listbox"], [role="radiogroup"], ' +
    '[role="spinbutton"], [role="textbox"], [contenteditable="true"]');
  for (const el of customs) {
    if (el.matches(NATIVE) || used.has(el)) continue;
    // wrappers around native controls (react-select shells, native radio groups) are handled above
    if (el.querySelector(NATIVE)) continue;
    if (el.closest('[role="listbox"]') && el.getAttribute('role') !== 'listbox') continue;
    if (el.getAttribute('role') === 'listbox' && el.closest('[role="combobox"]')) continue;
    if (!visible(el)) continue;
    const lab = labelOf(el);
    const req = el.getAttribute('aria-required') === 'true' || reqOf(el, lab);
    const shown = clean(el.getAttribute('aria-valuetext') || txt(el) || '');
    const placeholder = /^(select|choose|pick|search|--|please select)/i.test(shown);
    push({key: keyOf(el), label: clean(lab.text), type: 'select', widget: 'custom',
          required: !!req, selector: sel(el), options: [], option_selectors: [],
          current: shown && !placeholder ? shown : null, visible: true, trigger: '',
          max_length: null});
  }

  // stable document order
  const pos = f => { try { return document.querySelector(f.selector); } catch (e) { return null; } };
  const withPos = out.map(f => [f, pos(f)]);
  withPos.sort((a, b) => (a[1] && b[1]) ? ((a[1].compareDocumentPosition(b[1]) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1) : 0);
  return withPos.map(x => x[0]);
}
