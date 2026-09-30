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
  clone.querySelectorAll('script').forEach(s => s.remove());
  return '<!DOCTYPE html>\n' + clone.outerHTML;
}
