"""Read-only Selenium DOM Inspector (a *tool*, not Agent 7)."""
from __future__ import annotations

import os
from bs4 import BeautifulSoup
from .selenium_runtime import make_driver


def compact_dom(html: str, max_elements: int = 120) -> dict:
    """Reduce DOM to relevant public elements; do not send full page/credentials to LLM."""
    soup = BeautifulSoup(html, "html.parser")
    elements = []
    for tag in soup.select("input, select, button, textarea, a, form")[:max_elements]:
        if tag.name == "input" and tag.get("type", "").lower() in {"hidden", "password"}:
            continue
        elements.append({
            "tag": tag.name,
            "id": str(tag.get("id", ""))[:100],
            "name": str(tag.get("name", ""))[:100],
            "type": str(tag.get("type", ""))[:40],
            "aria_label": str(tag.get("aria-label", ""))[:120],
            "text": tag.get_text(" ", strip=True)[:120],
            "href_path": str(tag.get("href", "")).split("?")[0][:150],
        })
    return {"title": (soup.title.get_text(" ", strip=True) if soup.title else "")[:140],
            "elements": elements, "count": len(elements)}


def inspect_page(url: str, *, headless: bool = True) -> dict:
    driver = make_driver(headless=headless)
    try:
        driver.get(url)
        snapshot = compact_dom(driver.page_source)
        snapshot["url"] = driver.current_url
        return snapshot
    finally:
        driver.quit()


# Static application-owned JS, never supplied or extended by a model/page.
# WebElement references remain in Agent4's worker. Only bounded metadata is sent
# to Agent3; no page_source, cookies, storage, input values or hidden fields.
LIVE_DOM_SCRIPT = r'''
const limit = arguments[0];
const candidates = Array.from(document.querySelectorAll(
  'input,textarea,select,button,a,form,h1,h2,h3,p,li,label,output,[role],[data-testid]'));
const secret = /password|passwd|secret|token|credential|credit|card.number|ssn|otp|one.time/i;
const visible = e => !!(e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
const safeText = e => {
  const clone = e.cloneNode(true);
  clone.querySelectorAll('input,textarea,script,style,[hidden],[aria-hidden="true"],[data-private],[data-sensitive]')
    .forEach(x=>x.remove());
  return (clone.textContent || '').replace(/\s+/g,' ').trim().slice(0,160);
};
const selected = [];
let truncated = false;
for (const e of candidates) {
  const type = (e.getAttribute('type') || '').toLowerCase();
  const sensitive = secret.test([e.id,e.name,e.autocomplete,e.getAttribute('aria-label')].join(' ')) ||
    ['hidden','password','file'].includes(type) || !!e.closest('[data-private],[data-sensitive]');
  if (sensitive) continue;
  if (selected.length >= limit) { truncated = true; break; }
  const labels = e.labels ? Array.from(e.labels).filter(x=>!x.closest('[data-private],[data-sensitive]'))
    .map(x=>safeText(x)).join(' ') : '';
  const labelled = (e.getAttribute('aria-labelledby') || '').split(/\s+/).map(id=>document.getElementById(id))
    .filter(x=>x && !x.closest('[data-private],[data-sensitive]')).map(x=>safeText(x)).join(' ');
  selected.push({element:e, tag:e.tagName.toLowerCase(), type,
    name:(e.getAttribute('aria-label')||labelled||labels||e.getAttribute('placeholder')||(visible(e)?safeText(e):'')).slice(0,160),
    text: !visible(e) || ['input','textarea','select'].includes(e.tagName.toLowerCase()) ? '' : safeText(e),
    role:(e.getAttribute('role')||'').slice(0,40), visible:visible(e),
    enabled:!e.disabled && e.getAttribute('aria-disabled')!=='true', sensitive:false,
    dom_id:(e.id||'').slice(0,100), parent:e.parentElement, form:e.form || e.closest('form'),
    options:e.tagName==='SELECT' ? Array.from(e.options).slice(0,30).map(x=>x.textContent.trim().slice(0,100)) : []});
}
return {title:document.title.slice(0,140), elements:selected,
  truncated, ready:document.readyState};
'''


def observe_session(driver, *, observation_number: int, max_elements: int = 100) -> tuple[dict, dict]:
    """Read the existing session, returning public metadata and worker-only refs.

    Names/text can contain public personal information; bounded email/long-number
    redaction reduces exposure but is not a universal PII detector. Sensitive
    controls and data-private/data-sensitive subtrees are excluded entirely.
    """
    import hashlib
    import json
    import re
    from urllib.parse import urlsplit, urlunsplit

    def redact(text):
        text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[redacted-email]', str(text))
        return re.sub(r'\b\d{8,}\b', '[redacted-number]', text)[:160]

    result = driver.execute_script(LIVE_DOM_SCRIPT, max_elements)
    rows = result['elements']
    handles = {row['element'].id: f'n_{i}' for i, row in enumerate(rows)}
    refs, elements = {}, []
    for row in rows:
        element = row.pop('element')
        node_id = handles[element.id]
        refs[node_id] = element
        row['node_id'] = node_id
        for relation in ('parent', 'form'):
            value = row[relation]
            row[relation + '_id'] = handles.get(value.id) if value else None
            del row[relation]
        for field in ('text', 'name', 'dom_id', 'role'):
            row[field] = redact(row[field])
        row['options'] = [redact(option) for option in row['options']]
        elements.append(row)
    url = urlsplit(driver.current_url)
    public_url = urlunsplit((url.scheme, url.netloc, url.path, '', ''))
    public = {'url': public_url, 'title': redact(result['title']), 'elements': elements,
              'ready': result['ready'], 'truncated': result['truncated'],
              'untrusted_page_data': True, 'observation_number': observation_number}
    digest = hashlib.sha256(json.dumps(public, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    public['observation_id'] = f'obs_{observation_number}_{digest}'
    return public, refs
