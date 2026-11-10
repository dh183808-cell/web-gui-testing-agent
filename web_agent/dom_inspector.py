"""Read-only Selenium DOM Inspector (a *tool*, not Agent 7)."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from functools import lru_cache
from pkgutil import get_data
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from bs4 import BeautifulSoup, Comment


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
    """Keep non-secret SPA routes AND document anchors in link observations."""
    try:
        fragment = urlsplit(value).fragment
        route = '#' + fragment if (re.fullmatch(r'(?:/[A-Za-z0-9/_-]{0,100}|[A-Za-z0-9_-]{1,80})', fragment)
                                    and not SENSITIVE.search(fragment)) else ''
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
                           'href', 'target', 'formtarget', 'disabled', 'checked', 'selected', 'hidden', 'aria-disabled',
                           'data-date-format', 'data-format', 'data-date', 'aria-controls',
                           'aria-labelledby', 'aria-selected', 'aria-current', 'data-outside-month'}:
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
                region_css: str | None = None, max_html: int = 12000, focus_terms=()) -> dict:
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
    terms = list(dict.fromkeys(str(t).strip().lower()[:80] for t in focus_terms if str(t).strip()))[:24]
    relevance = {}
    def relevant(tag):
        key = id(tag)
        if key not in relevance:
            relevance[key] = any(t in tag.get_text(' ', strip=True).lower() for t in terms)
        return relevance[key]
    candidates = region.select(CONTROLS)
    if terms:
        # Late-page controls must compete on task relevance, not DOM order alone.
        candidates.sort(key=lambda tag: not relevant(tag))
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
            "target": str(tag.get("target", ""))[:160],
        })
    contexts = region.select('h1,h2,h3,main,article,[role="main"],section[id],div[id],dl')
    # Keep identifiers for content scopes as well as controls. This lets evidence
    # target a complete article rather than a list of child links.
    contexts = [t for t in contexts if t.name in {'h1','h2','h3','main','article','dl'} or
                t.get('role') == 'main' or terms and relevant(t)]
    headings = sorted((t for t in contexts if t.name in {'h1','h2','h3'}),
                      key=lambda t: not relevant(t))[:6]
    scopes = sorted((t for t in contexts if t.name not in {'h1','h2','h3'}),
                    key=lambda t: (t.name not in {'main','article','dl'}, not relevant(t)))[:8]
    content_regions = [dict(_tag_context(t, max_text), text_length=len(t.get_text(' ', strip=True)),
                           parent=_tag_context(t.parent, max_text)) for t in headings + scopes]
    text_matches = []
    if terms:
        # Distinguish prose mentions from links, even if both contain the target.
        for term in terms:
            matches = region.find_all(string=re.compile(re.escape(term), re.I), limit=2)
            for node in matches:
                tag = node.parent
                text = tag.get_text(' ', strip=True)
                start = max(0, text.lower().find(term) - max_text // 3)
                text_matches.append(dict(_tag_context(tag, max_text),
                    text=safe_text(text[start:start+max_text], max_text),
                    parent=_tag_context(tag.parent, max_text)))
                if len(text_matches) >= 24:
                    break
            if len(text_matches) >= 24:
                break
    relevant_html = str(region)
    if terms:
        fragments = [str(t)[:1200] for t in candidates if relevant(t)][:24]
        # Bounded snippets retain the exact link markup even far below the header.
        relevant_html = '\n'.join(fragments) + '\n' + relevant_html
    text_truncated = any(len(tag.get_text(' ', strip=True)) > max_text for tag in candidates[:max_elements])
    return {"title": (soup.title.get_text(" ", strip=True) if soup.title else "")[:140],
            "elements": elements, "count": len(elements), 'total_candidates': len(candidates),
            'content_regions': content_regions,
            # Keep bounded table/grid markup so calendar headings, month/year
            # choices and day attributes survive a long page's HTML prefix.
            # These are untrusted candidates; visibility must be checked live.
            'calendar_regions': [str(t)[:5000] for t in region.select('table,[role="grid"]')[:3]],
            'text_matches': text_matches,
            'locator_notes': 'href_path omits userinfo/query values and may be relative or resolved; '
                             'it is not the literal href attribute. Plain #anchors stay on this document. '
                             'Prose mentions are not automatically links.',
            'html': relevant_html[:max_html], 'untrusted_page_data': True, 'region_css': region_css,
            'truncated': input_truncated or text_truncated or len(candidates) > max_elements or len(relevant_html) > max_html}


@lru_cache(maxsize=1)
def displayed_atom():
    """Use the SAME visibility implementation as installed Selenium, including opacity."""
    return get_data('selenium.webdriver.remote', 'isDisplayed.js').decode('utf-8')


def observe_session(driver, *, region_css: str | None = None, max_elements: int = 60,
                    max_text: int = 120, max_html: int = 12000, focus_terms=()) -> dict:
    """One batch for live DOM + metadata, two window calls; no per-element round trips."""
    if not (1 <= max_elements <= 200 and 1 <= max_text <= 500 and 100 <= max_html <= 50000):
        raise ValueError('Observation limits out of range')
    terms = list(dict.fromkeys(str(t).strip().lower()[:80] for t in focus_terms if str(t).strip()))[:24]
    script = 'const focusTerms = ' + json.dumps(terms, ensure_ascii=True) + ';'
    raw = driver.execute_script(script + 'const displayed = (' + displayed_atom() + ');' + r'''
const [region, selector, limit, textLimit] = arguments;
const root = region ? document.querySelector(region) : document;
if (!root) return {missing_region:true};
const candidates = Array.from(root.querySelectorAll(selector)), live = [];
if (focusTerms.length) {
    const score = el => focusTerms.some(t => (el.textContent || "").toLowerCase().includes(t)) ? 0 : 1;
    const scores = new Map(candidates.map(el => [el, score(el)]));
    candidates.sort((a,b) => scores.get(a) - scores.get(b));
}
const sensitive = /password|passwd|token|secret|cookie|authorization|api.?key/i;
let scanned = 0;
for (const el of candidates) {
    if (scanned++ >= limit * 2 || live.length >= limit) break;
    const attr = key => el.getAttribute(key) || "";
    const identity = ["id","name","autocomplete","aria-label"].map(attr).join(" ");
    if (["hidden","password"].includes(attr("type").toLowerCase()) || sensitive.test(identity)) continue;
    const visible = displayed(el);
    live.push({tag:el.tagName.toLowerCase(), id:attr("id"), name:attr("name"),
        ...Object.fromEntries(["class","type","data-testid","role","aria-checked","aria-expanded","aria-label","target","formtarget",
            "data-date-format","data-format","placeholder","aria-controls","aria-labelledby",
            "aria-selected","aria-current"]
            .map(k => [k,attr(k).slice(0,160)])),
        href_path:String(el.href || attr("href")), visible, enabled:!el.matches(":disabled"),
        text:visible ? (el.innerText || el.textContent || "").slice(0,textLimit) : "",
        value:String(el.value || "").slice(0,textLimit), checked:el.checked === true});
}
return {html:document.documentElement.outerHTML.slice(0,2000001), live,
    url:location.href, title:document.title, ready_state:document.readyState,
    frame_context:window === window.top ? "top" : "unsupported",
    truncated:candidates.length > scanned};
''', region_css, CONTROLS, max_elements, max_text)
    if raw.get('missing_region'):
        raise ValueError('Requested observation region does not exist')
    snapshot = compact_dom(raw['html'], max_elements, max_text=max_text,
                           region_css=region_css, max_html=max_html, focus_terms=terms)
    live = []
    for row in raw['live']:
        live.append({k: (v if isinstance(v, bool) else
                        safe_href(v) if k == 'href_path' else
                        safe_text(v, max_text if k in {'text', 'value'} else 100 if k in {'id','name'} else 160))
                     for k,v in row.items()})
    snapshot['truncated'] |= raw['truncated']
    snapshot.update(url=diagnostic_url(raw['url']), title=safe_text(raw['title'], 140),
                    current_window=driver.current_window_handle, windows=list(driver.window_handles),
                    frame_context=raw['frame_context'], ready_state=raw['ready_state'], live_elements=live,
                    limitations=['top-level DOM only; checked clicks follow one new same-origin window; '
                                 'no arbitrary window/history selection, frame switching or Shadow DOM traversal'])
    snapshot['state_hash'] = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    snapshot['observation_id'] = uuid.uuid4().hex
    return snapshot
