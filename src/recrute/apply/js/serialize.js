() => {
  const live = [...document.querySelectorAll('input, textarea, select')];
  const clone = document.documentElement.cloneNode(true);
  const copies = [...clone.querySelectorAll('input, textarea, select')];
  live.forEach((el, i) => {
    const c = copies[i];
    if (!c) return;
    if (el.tagName === 'TEXTAREA') c.textContent = el.value;
    else if (el.tagName === 'SELECT') [...el.options].forEach((o, j) => {
      if (o.selected) c.options[j].setAttribute('selected', ''); else c.options[j].removeAttribute('selected');
    });
    else if (el.type === 'checkbox' || el.type === 'radio') { if (el.checked) c.setAttribute('checked', ''); else c.removeAttribute('checked'); }
    else if (el.type === 'file') c.setAttribute('data-files', [...(el.files || [])].map(f => f.name).join(', '));
    else if (el.type !== 'password') c.setAttribute('value', el.value);
  });
  // Secrets never reach a receipt: password / one-time-code fields lose any value, including
  // one already present as an attribute in the page's markup.
  clone.querySelectorAll('input[type="password" i], input[autocomplete~="current-password"], ' +
                         'input[autocomplete~="new-password"], input[autocomplete~="one-time-code"], ' +
                         'input[name*="password" i], input[id*="password" i], input[name*="passcode" i], ' +
                         'input[name*="otp" i], input[id*="otp" i], input[name*="verification" i]')
    .forEach(c => { c.removeAttribute('value'); c.value = ''; });
  clone.querySelectorAll('script').forEach(s => s.remove());
  return '<!DOCTYPE html>\n' + clone.outerHTML;
}
