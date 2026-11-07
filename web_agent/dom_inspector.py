"""Read-only Selenium DOM Inspector (a *tool*, not Agent 7)."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from bs4 import BeautifulSoup, Comment
from .selenium_runtime import make_driver


CONTROLS = 'input, select, button, textarea, a, form, [role="button"], [role="dialog"]'
SENSITIVE = re.compile(r'password|passwd|token|secret|cookie|authorization|api.?key', re.I)
SECRET_TEXT = re.compile(r'(?i)\b(password|token|secret|api[_-]?key|authorization)\s*[:=]\s*\S+')


def safe_text(value, limit: int = 160) -> str:
    return SECRET_TEXT.sub(r'\1=[REDACTED]', str(value or ''))[:limit]


def safe_url(value: str) -> str:
    """Exclude credentials, query values and fragments from diagnostic URLs."""
    try:
        p = urlsplit(value)
        host = p.hostname or ''
        if ':' in host:
            host = f'[{host}]'
        netloc = host + (f':{p.port}' if p.port else '')
        return urlunsplit((p.scheme, netloc, p.path, '', ''))[:500]
    except ValueError:
        return '[invalid URL]'


def safe_href(value: str) -> str:
    """Keep plain SPA paths consistently in HTML and structured observations."""
    try:
        fragment = urlsplit(value).fragment
        route = '#' + fragment if re.fullmatch(r'/[A-Za-z0-9/_-]{0,100}', fragment) else ''
        return safe_url(value) + route
    except ValueError:
        return '[invalid URL]'


def diagnostic_url(value: str) -> str:
    """Keep public search/paging state; redact other values and credentials.

    Link summaries still use safe_href. Unknown query state is distinguished by a
    digest without revealing its value. This is diagnostic data, not a navigation URL.
    """
    try:
        p = urlsplit(value)
        if len(p.query) > 8000:
            return safe_href(value) + '?query=[too long]'
        public = re.compile(r'(?:.*[-_])?(?:q|query|search|searchtype|page|start|size|order|sort|term|field|operator)$', re.I)
        pairs, redacted = [], False
        for key, text in parse_qsl(p.query, keep_blank_values=True, max_num_fields=100):
            if public.fullmatch(key) and not SENSITIVE.search(key) and not SENSITIVE.search(text):
                pairs.append((key[:80], safe_text(text, 160)))
            else:
                pairs.append((key[:80] if not SENSITIVE.search(key) else 'redacted', '[REDACTED]'))
                redacted = True
        if redacted:
            pairs.append(('query_digest', hashlib.sha256(p.query.encode()).hexdigest()[:12]))
        route = '#' + p.fragment if re.fullmatch(r'/[A-Za-z0-9/_-]{0,100}', p.fragment) else ''
        return safe_url(value) + ('?' + urlencode(pairs) if pairs else '') + route
    except ValueError:
        return '[invalid URL]'


def _tag_context(tag, max_text):
    if tag is None:
        return None
    return {'tag': tag.name, **{k: safe_text(tag.get(k, ''), 160) for k in
            ('id', 'class', 'role', 'data-testid', 'aria-checked', 'aria-expanded', 'aria-disabled')},
            **{k: tag.has_attr(k) for k in ('checked', 'selected', 'disabled', 'hidden')},
            'text': safe_text(tag.get_text(' ', strip=True), max_text)}


def _soup(html):
    soup = BeautifulSoup(html, 'html.parser')
    for node in list(soup.find_all(string=lambda t: isinstance(t, Comment))):
        node.extract()
    for tag in list(soup.find_all(True)):
        if tag.parent is None:
            continue
        identity = ' '.join(str(tag.get(k, '')) for k in ('id', 'name', 'autocomplete', 'aria-label'))
        if tag.name in {'script', 'style', 'template'} or (
                tag.name in {'input', 'textarea'} and (
                    str(tag.get('type', '')).lower() in {'hidden', 'password'} or SENSITIVE.search(identity))):
            tag.decompose()
            continue
        for key in list(tag.attrs):
            if key not in {'id', 'class', 'name', 'type', 'role', 'aria-label', 'aria-checked',
                           'aria-expanded', 'data-testid', 'placeholder', 'for',
                           'href', 'disabled', 'checked', 'selected', 'hidden', 'aria-disabled'}:
                del tag.attrs[key]
            elif key == 'href':
                tag.attrs[key] = safe_href(str(tag.attrs[key]))
            elif key == 'class':
                tag.attrs[key] = ' '.join(tag.attrs[key]) if isinstance(tag.attrs[key], list) else safe_text(tag.attrs[key])
            else:
                tag.attrs[key] = safe_text(tag.attrs[key])
        if tag.name == 'textarea':
            tag.clear()
    for node in list(soup.find_all(string=True)):
        node.replace_with(safe_text(node, 2000))
    return soup


def compact_dom(html: str, max_elements: int = 120, *, max_text: int = 120,
                region_css: str | None = None, max_html: int = 12000) -> dict:
    """Local, bounded reduction. Raw HTML is never the default model payload."""
    if not (1 <= max_elements <= 200 and 1 <= max_text <= 500 and 100 <= max_html <= 50000):
        raise ValueError('Observation limits out of range')
    if isinstance(html, bytes):
        html = html.decode('utf-8', errors='replace')
    input_truncated = len(html) > 2_000_000
    soup = _soup(html[:2_000_000])
    region = soup.select_one(region_css) if region_css else soup
    if region is None:
        raise ValueError('Requested observation region does not exist')
    candidates = region.select(CONTROLS)
    elements = []
    for tag in candidates[:max_elements]:
        parent = tag.find_parent(['form', 'section', 'article', 'dialog', 'li'])
        label = soup.find('label', attrs={'for': tag.get('id')}) if tag.get('id') else None
        if label is None:
            label = tag.find_parent('label')
        if label is None and tag.name in {'input', 'select', 'textarea'}:
            # Unnamed controls commonly share a small wrapper with their label.
            labels = tag.parent.find_all('label', recursive=False) if tag.parent else []
            label = labels[0] if len(labels) == 1 else None
        elements.append({
            **_tag_context(tag, max_text),
            "tag": tag.name,
            "id": str(tag.get("id", ""))[:100],
            "name": str(tag.get("name", ""))[:100],
            "type": str(tag.get("type", ""))[:40],
            "aria_label": str(tag.get("aria-label", ""))[:120],
            "text": safe_text(tag.get_text(" ", strip=True), max_text),
            "label": safe_text(label.get_text(' ', strip=True) if label else '', max_text),
            "parent": _tag_context(parent, max_text),
            "container": _tag_context(tag.parent, max_text) if tag.parent and tag.parent.name != '[document]' else None,
            "href_path": safe_href(str(tag.get("href", ""))),
        })
    relevant_html = str(region)
    text_truncated = any(len(tag.get_text(' ', strip=True)) > max_text for tag in candidates[:max_elements])
    return {"title": (soup.title.get_text(" ", strip=True) if soup.title else "")[:140],
            "elements": elements, "count": len(elements), 'total_candidates': len(candidates),
            'html': relevant_html[:max_html], 'untrusted_page_data': True, 'region_css': region_css,
            'truncated': input_truncated or text_truncated or len(candidates) > max_elements or len(relevant_html) > max_html}


def observe_session(driver, *, region_css: str | None = None, max_elements: int = 60,
                    max_text: int = 120, max_html: int = 12000) -> dict:
    """The caller owns the driver. This never creates/closes it or changes frames."""
    from selenium.webdriver.common.by import By

    snapshot = compact_dom(driver.page_source, max_elements, max_text=max_text,
                           region_css=region_css, max_html=max_html)
    root = driver.find_element(By.CSS_SELECTOR, region_css) if region_css else driver
    live = []
    for node in root.find_elements(By.CSS_SELECTOR, CONTROLS)[:max_elements * 2]:
        try:
            identity = ' '.join(node.get_attribute(k) or '' for k in ('id', 'name', 'aria-label'))
            if (node.get_attribute('type') or '').lower() in {'hidden', 'password'} or SENSITIVE.search(identity):
                continue
            live.append({'tag': node.tag_name, 'id': safe_text(node.get_attribute('id'), 100),
                         'name': safe_text(node.get_attribute('name'), 100),
                         **{k: safe_text(node.get_attribute(k), 160) for k in
                            ('class', 'type', 'data-testid', 'role', 'aria-checked', 'aria-expanded', 'aria-label')},
                         'href_path': safe_href(node.get_attribute('href') or ''),
                         'visible': node.is_displayed(), 'enabled': node.is_enabled(),
                         'text': safe_text(node.text, max_text),
                         'value': safe_text(node.get_property('value'), max_text),
                         'checked': node.get_property('checked') is True})
        except Exception:
            snapshot['truncated'] = True
        if len(live) >= max_elements:
            break
    snapshot.update(url=diagnostic_url(driver.current_url), title=safe_text(driver.title, 140),
                    current_window=driver.current_window_handle, windows=list(driver.window_handles),
                    frame_context='top' if driver.execute_script('return window === window.top') else 'unsupported',
                    ready_state=driver.execute_script('return document.readyState'), live_elements=live,
                    limitations=['top-level DOM only; no frame/window switching or Shadow DOM traversal'])
    snapshot['state_hash'] = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    snapshot['observation_id'] = uuid.uuid4().hex
    return snapshot


def inspect_page(url: str, *, headless: bool = True) -> dict:
    driver = make_driver(headless=headless)
    try:
        driver.get(url)
        return observe_session(driver)
    finally:
        driver.quit()
