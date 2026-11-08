"""Small Selenium Python subset, interpreted without exec/eval or Python imports.

This is a capability restriction for the local research demo, NOT an OS sandbox.
Only the trusted wrappers own Selenium objects. No model object reaches getattr.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from functools import lru_cache
import operator
import re
import time
from urllib.parse import urlsplit


def origin(url):
    p = urlsplit(url)
    if p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password:
        raise ValueError('Only credential-free HTTP(S) URLs are supported')
    return p.scheme, p.hostname, p.port or (443 if p.scheme == 'https' else 80)


ATTRIBUTES = frozenset({'get', 'find_element', 'find_elements', 'click', 'clear', 'send_keys',
    'get_attribute', 'is_displayed', 'is_enabled', 'is_selected', 'text', 'title', 'current_url',
    'until', 'presence_of_element_located', 'visibility_of_element_located',
    'element_to_be_clickable', 'invisibility_of_element_located', 'text_to_be_present_in_element',
    'CSS_SELECTOR', 'XPATH', 'ID', 'NAME', 'ENTER', 'TAB', 'ESCAPE', 'SPACE', 'CONTROL', 'BACKSPACE',
    'append', 'strip', 'lower'})
ROOT_NAMES = {'driver', 'BASE_URL', 'By', 'Keys', 'EC', 'WebDriverWait', 'len'}
OBSERVED_ATTRIBUTES = frozenset({'id', 'name', 'type', 'value', 'class', 'href', 'checked',
    'textContent', 'aria-label', 'aria-checked', 'aria-expanded', 'aria-disabled',
    'data-testid', 'disabled', 'selected', 'role', 'placeholder', 'target', 'formtarget'})
LOCATORS = {'css': 'css selector', 'css selector': 'css selector', 'css_selector': 'css selector',
            'xpath': 'xpath', 'id': 'id', 'name': 'name'}
READS = {'count', 'text', 'texts', 'selected', 'selected_text', 'visible', 'enabled', 'attribute', 'exists', 'visible_exists', 'target_present'}
TEXT_LIMIT = 2_000_000


@lru_cache(maxsize=1)
def rendered_text_script():
    from pathlib import Path
    return Path(__file__).with_name('rendered_text.js').read_text(encoding='utf-8')


EVIDENCE_GUIDE = '''Trusted Runtime evidence semantics (not model Python return values):
by supports css/xpath/id/name. count returns an INTEGER of VISIBLE matched elements
(is_displayed filtered); texts returns their visible text list. Neither counts hidden
nodes. exists returns a BOOLEAN for any DOM match (including hidden nodes);
visible_exists returns a BOOLEAN for any visible match. Empty collections yield
count=0, texts=[], exists=false, visible_exists=false. selected/visible/enabled read
the native state of exactly ONE existing target; missing/ambiguous targets are UNKNOWN,
not false. text reads one visible target; text/texts use complete rendered light-DOM
text (hidden descendants excluded), not a 1000-character prefix. Incomplete/oversize
text is UNKNOWN, never a truncated PASS/FAIL. attribute reads an allowed attribute string.
selected_text reads the displayed label of the selected option of ONE native select,
e.g. value="all" with label "All fields" returns "All fields", not "all".
Missing/hidden select or missing/ambiguous selected options are UNKNOWN. Target the
select itself. selected_value is NOT supported. attribute(value) is the internal value;
text on a specific selected option is also supported for its label.
No implicit count-to-bool or string-to-number conversion. At most 200 matches.
For a frozen named required target, use {read:"target_present",target:{role:"button",
name:"source name"},step_id:"S2"}, with NO by/selector. Only this trusted full-control
inventory plus bounded waiting can support an absence claim; locator misses cannot.
Incomplete context, unsupported naming, frames or shadow roots yield UNKNOWN.'''


def normalize_bindings(bindings, *, step_id=None, expected_ids=None):
    """One locator/read contract for generation, preflight and trusted collection."""
    context = f'Step {step_id or "?"}'
    if not isinstance(bindings, dict) or len(bindings) > 120:
        raise ValueError(f'{context}: bindings must be an object with at most 120 Oracle entries')
    if expected_ids is not None and set(bindings) != set(expected_ids):
        raise ValueError(f'{context}: Oracle bindings missing={sorted(set(expected_ids)-set(bindings))}, '
                         f'unexpected={sorted(set(bindings)-set(expected_ids))}')
    result = {}
    for key, query in bindings.items():
        prefix = f'{context} Oracle {key}'
        def invalid(message):
            raise ValueError(prefix + ': ' + message)
        if not isinstance(query, dict):
            invalid('binding must be an object: by, selector, read, step_id')
        if query.get('read') == 'target_present':
            from .schema import validate_target
            if set(query) != {'read','target','step_id'} or not isinstance(query.get('step_id'),str):
                invalid('target_present requires ONLY read, target, step_id (no locator)')
            if step_id is not None and query['step_id'] != step_id:
                invalid(f'step_id must match current step {step_id}')
            validate_target(query.get('target'))
            result[key] = deepcopy(query)
            continue
        extra = set(query) - {'by', 'selector', 'read', 'step_id', 'attribute'}
        if extra:
            invalid(f'unsupported fields {sorted(extra)}; use by="xpath", selector="//input[@id=\'item\']", '
                    'read="selected", step_id="' + str(step_id or 'S1') + '"; no separate xpath field')
        missing = {'selector', 'read', 'step_id'} - set(query)
        if missing:
            invalid(f'missing fields {sorted(missing)}; required: selector, read, step_id; by defaults to css')
        if not isinstance(query['step_id'], str) or (step_id is not None and query['step_id'] != step_id):
            invalid(f'step_id must match current step {step_id}')
        by = query.get('by', 'css')
        if not isinstance(by, str) or by.lower() not in LOCATORS:
            invalid(f'unsupported by={by!r}; supported: css, xpath, id, name')
        selector = query['selector']
        if not isinstance(selector, str) or not 1 <= len(selector.strip()) <= 1000:
            invalid('selector must be a nonempty actual locator of at most 1000 characters')
        if selector.strip().upper() in {'CSS', 'XPATH', 'ID', 'NAME', 'CSS_SELECTOR'}:
            invalid('selector is a locator-type placeholder; put the type in by and the actual locator in selector')
        kind = query['read']
        if not isinstance(kind, str) or kind not in READS:
            invalid(f'unsupported read={kind!r}; supported: {", ".join(sorted(READS))}')
        if kind == 'attribute':
            if not isinstance(query.get('attribute'), str) or query['attribute'] not in OBSERVED_ATTRIBUTES:
                invalid('attribute read requires an allowed attribute name: ' + ', '.join(sorted(OBSERVED_ATTRIBUTES)))
        elif 'attribute' in query:
            invalid('attribute field is only supported with read="attribute"')
        result[key] = dict(query, by='css' if LOCATORS[by.lower()] == 'css selector' else by.lower())
    return result


def probe_target(driver, target, *, wait_seconds=2.0, poll_seconds=.2):
    """Bounded negative evidence, independent of a model-generated locator.

    Deliberately conservative: top-level light DOM, one window, supported named
    native/ARIA controls. A locator timeout or unsupported context is never false.
    """
    from .dom_inspector import displayed_atom, diagnostic_url, safe_text
    from .schema import validate_target
    validate_target(target)
    started = time.monotonic()
    samples, inventories, last = [], [], None
    entry_handle = driver.current_window_handle
    script = 'const displayed = (' + displayed_atom() + ');' + r'''
const wanted=arguments[0], norm=s=>(s||'').replace(/\s+/g,' ').trim();
const all=Array.from(document.querySelectorAll('*'));
const reasons=[];
if(all.length>30000) reasons.push('dom_limit');
if(document.querySelector('iframe,frame')) reasons.push('frames_not_inspected');
if(all.some(e=>e.shadowRoot)) reasons.push('shadow_roots_not_inspected');
function role(e) {
 const explicit=e.getAttribute('role'); if(explicit) return explicit;
 const t=e.tagName, k=(e.getAttribute('type')||'text').toLowerCase();
 if(t==='BUTTON'||(t==='INPUT'&&['button','submit','reset','image'].includes(k))) return 'button';
 if(t==='A'&&e.hasAttribute('href')) return 'link';
 if(t==='SELECT') return 'combobox';
 if(t==='TEXTAREA'||(t==='INPUT'&&['text','search','email','url','tel'].includes(k))) return 'textbox';
 if(t==='INPUT'&&['checkbox','radio'].includes(k)) return k;
 return '';
}
function name(e) {
 const ids=e.getAttribute('aria-labelledby');
 if(ids) {
   const labels=ids.trim().split(/\s+/).map(id=>document.getElementById(id));
   if(labels.some(x=>!x)) reasons.push('unresolved_aria_label');
   return norm(labels.map(x=>x?x.textContent:'').join(' '));
 }
 if(e.hasAttribute('aria-label')) return norm(e.getAttribute('aria-label'));
 if(e.labels && e.labels.length) return norm(Array.from(e.labels).map(l=>l.innerText).join(' '));
 if(e.tagName==='INPUT'&&['button','submit','reset'].includes(e.type)) return norm(e.value);
 return norm(e.innerText||e.getAttribute('alt')||e.getAttribute('title')||'');
}
const controls=[];
for(const e of all) {
 if(role(e)!==wanted.role) continue;
 const visible=displayed(e), label=name(e);
 if(visible&&!label) reasons.push('unnamed_candidate');
 if(label.length>500) reasons.push('name_limit');
 controls.push({tag:e.tagName.toLowerCase(),id:e.id,role:wanted.role,
   name:label.slice(0,500),visible,type:e.getAttribute('type')});
}
if(controls.length>500) reasons.push('inventory_limit');
const found=controls.filter(e=>e.visible&&norm(e.name)===norm(wanted.name));
return {url:location.href,title:document.title,ready_state:document.readyState,
  controls:controls.slice(0,500),found:found.length,total_controls:controls.length,
  dom_nodes:all.length,reasons:Array.from(new Set(reasons)),
  page_context:!!document.body&&!!document.querySelector('h1,h2,form,main,[role=main]')};
'''
    while True:
        row = driver.execute_script(script, target)
        handles = driver.window_handles
        if len(handles) != 1 or driver.current_window_handle != entry_handle:
            row['reasons'].append('window_context_unresolved')
        row['url'] = diagnostic_url(row['url'])
        row['title'] = safe_text(row['title'],200)
        for control in row['controls']:
            control['name'] = safe_text(control['name'],500)
        samples.append(dict(url=row['url'],ready_state=row['ready_state'],found=row['found'],
                            elapsed_seconds=round(time.monotonic()-started,3)))
        inventories.append(row['controls'])
        last = row
        if row['found'] or time.monotonic()-started >= wait_seconds:
            break
        time.sleep(poll_seconds)
    stable = (len(samples)>=3 and all(s['ready_state']=='complete' and s['url']==samples[0]['url'] for s in samples)
              and all(items==inventories[0] for items in inventories)
              and time.monotonic()-started >= wait_seconds)
    complete = not last['reasons'] and last['page_context'] and stable
    observed = bool(last['found']) or complete
    return dict(value=bool(last['found']) if observed else None,
                status='observed' if observed else 'absence_unconfirmed',
                absence_confirmed=observed and not last['found'],inventory_complete=complete,
                target=target,probe=dict(method='named_control_inventory',scope='top_level_light_dom',
                    wait_seconds=wait_seconds,elapsed_seconds=round(time.monotonic()-started,3),
                    samples=samples,context_stable=stable,current_window=entry_handle,**last))


def collect_measurements(driver, base_url, bindings, *, step_id=None, batch=False,
                         on_progress=None, completed=None):
    """Worker batches rendered text; counts never read text. No silent truncation.

    batch=False retains compatibility with callers supplying minimal driver adapters.
    Both paths use Selenium's own visibility predicate and the same evidence contract.
    """
    from .dom_inspector import safe_text, safe_href, displayed_atom, SECRET_TEXT
    result = completed if completed is not None else {}
    matched, measurements = {}, {}
    queries = normalize_bindings(bindings, step_id=step_id)
    started = time.monotonic()

    def cache_key(query):
        if query['read'] == 'target_present':
            return 'target_present', query['target']['role'], query['target']['name'], query['step_id']
        return LOCATORS[query['by']], query['selector'], query['read'], query.get('attribute'), query['step_id']

    groups = {}
    for key, query in queries.items():
        groups.setdefault(cache_key(query), []).append(key)

    for key, query in queries.items():
        def notify(operation, event, mark, **extra):
            if on_progress:
                on_progress(dict(oracle_id=key, oracle_ids=groups[cache_key(query)],
                    step_id=query['step_id'], by=query.get('by'), selector=safe_text(query.get('selector',''),1000),
                    read=query['read'], attribute=query.get('attribute'), operation=operation, event=event,
                    started_seconds=round(mark-started,4), elapsed_seconds=round(time.monotonic()-mark,4), **extra))

        def run(operation, fn):
            mark = time.monotonic()
            notify(operation,'started',mark)
            try:
                value = fn()
            except Exception as exc:
                notify(operation,'error',mark,error_type=type(exc).__name__)
                raise
            notify(operation,'complete',mark)
            return value

        def read_text(elements):
            if not elements:
                return []
            if batch:
                rows = run('rendered_text', lambda: driver.execute_script(rendered_text_script(), elements, TEXT_LIMIT))
                if len(rows) != len(elements):
                    raise ValueError('Incomplete rendered text response')
            else:
                # Legacy/native adapters still enforce completeness and the same
                # text limit. The managed Worker ALWAYS selects batch=True.
                rows = run('native_text', lambda: [
                    dict(text=n.text, status='observed', text_complete=True, text_method='native') for n in elements])
            for row in rows:
                text = row.get('text')
                if text is not None:
                    row['text_length'] = len(text)
                    if len(text) > TEXT_LIMIT:
                        row.update(text=None,status='text_limit_exceeded',text_complete=False)
                    else:
                        # Redaction can lengthen a string. Never cut off its tail
                        # using the original length after replacing a secret.
                        redacted = SECRET_TEXT.sub(r'\1=[REDACTED]',text)
                        row['text'] = redacted
                        if len(redacted) > TEXT_LIMIT:
                            row.update(text=None,status='text_limit_exceeded',text_complete=False)
                row['text_limit'] = TEXT_LIMIT
            return rows

        try:
            if query['read'] == 'target_present':
                measurement_key = cache_key(query)
                if measurement_key not in measurements:
                    measurements[measurement_key] = run('target_inventory', lambda: probe_target(driver,query['target']))
                result[key] = dict(query, **deepcopy(measurements[measurement_key]))
                notify('measurement','observed',time.monotonic(),status=result[key]['status'])
                continue
            locator = LOCATORS[query['by']], query['selector']
            # Scope is ONE collection only. Different reads/attributes/steps never
            # share values, even when their locator happens to be identical.
            measurement_key = cache_key(query)
            if measurement_key in measurements:
                result[key] = dict(query, **deepcopy(measurements[measurement_key]))
                notify('measurement','reused',time.monotonic(),reused_from=groups[measurement_key][0])
                continue
            if locator not in matched:
                nodes = run('locate', lambda: driver.find_elements(*locator))
                if len(nodes) > 200:
                    raise ValueError('More than 200 matches; narrow the locator instead of truncating a count')
                if batch and nodes:
                    states = run('visibility', lambda: driver.execute_script('const displayed = (' + displayed_atom() + ');' + r'''
return arguments[0].map(el => ({
    sensitive:/password|passwd|secret|token|api.?key|credit.?card/i.test(
        ["type","name","id","autocomplete"].map(k => el.getAttribute(k) || "").join(" ")),
    visible:displayed(el)
}));
''', nodes))
                    if len(states) != len(nodes):
                        raise ValueError('Incomplete batch visibility response')
                    if any(s['sensitive'] for s in states):
                        raise PermissionError('Sensitive controls are outside this demo')
                    wrapped = nodes  # Scalar text/state reads still use native WebDriver.
                    visible = [n for n,s in zip(nodes,states) if s['visible']]
                else:
                    wrapped = [Element(n, Browser(driver, base_url)) for n in nodes]
                    visible = run('visibility', lambda: [n for n in wrapped if n.is_displayed()])
                matched[locator] = nodes, wrapped, visible
            nodes, wrapped, visible = matched[locator]
            kind, value, status, details = query['read'], None, 'observed', {}
            if kind == 'count':
                value = len(visible)
            elif kind == 'exists':
                value = bool(nodes)
            elif kind == 'visible_exists':
                value = bool(visible)
            elif kind == 'texts':
                rows = read_text([n for n in nodes if n in visible] if batch else
                                 [n for n,w in zip(nodes,wrapped) if w in visible])
                value = [row['text'] for row in rows]
                problem = next((row['status'] for row in rows if row['status'] != 'observed'), None)
                if sum(len(t or '') for t in value) > TEXT_LIMIT:
                    problem = 'text_limit_exceeded'
                status = problem or 'observed'
                if problem:
                    value = None
                details = dict(text_complete=not problem,text_method='rendered_dom' if batch else 'native',
                               text_limit=TEXT_LIMIT,text_length=sum(row.get('text_length',0) for row in rows))
            elif len(nodes) != 1:
                status = 'missing' if not nodes else 'ambiguous'
            elif kind == 'text':
                if visible:
                    row = read_text(nodes)[0]
                    value, status = row['text'], row['status']
                    details = {k:v for k,v in row.items() if k not in {'text','status'}}
                else:
                    status = 'hidden'
            elif kind == 'selected_text':
                if nodes[0].tag_name.lower() != 'select':
                    raise ValueError('selected_text requires a native select target; use text for an option')
                if not visible:
                    status = 'hidden'
                else:
                    from selenium.webdriver.common.by import By
                    options = run('select_options',lambda: nodes[0].find_elements(By.TAG_NAME, 'option'))
                    if len(options) > 200:
                        raise ValueError('selected_text supports at most 200 options')
                    selected = run('selected_state',lambda: [n for n in options if n.is_selected()])
                    if len(selected) != 1:
                        status = 'selection_missing' if not selected else 'selection_ambiguous'
                    else:
                        # OPTION rendered layout differs from its select; native
                        # labels are small and preserve existing dropdown semantics.
                        row = read_text(selected)[0] if not batch else None
                        if batch:
                            raw = run('selected_text',lambda: selected[0].text)
                            row = dict(text=safe_text(raw,len(raw)),status='observed',text_complete=True)
                            if len(raw) > TEXT_LIMIT:
                                row.update(text=None,status='text_limit_exceeded',text_complete=False)
                        value,status = row['text'],row['status']
                        details = {k:v for k,v in row.items() if k not in {'text','status'}}
            elif kind in {'selected', 'visible', 'enabled'}:
                method = {'selected': 'is_selected', 'visible': 'is_displayed', 'enabled': 'is_enabled'}[kind]
                value = run(kind,lambda: getattr(wrapped[0], method)())
            elif kind == 'attribute':
                raw = run('attribute',lambda: wrapped[0].get_attribute(query['attribute']))
                value = (safe_href(raw) if query['attribute'] == 'href' else safe_text(raw, len(raw))) if raw is not None else None
                if raw is None:
                    status = 'attribute_missing'
                elif len(raw) > TEXT_LIMIT:
                    value,status = None,'text_limit_exceeded'
                    details = dict(text_complete=False,text_limit=TEXT_LIMIT,text_length=len(raw))
            measured = dict(value=value, status=status, matched=len(nodes), visible=len(visible),
                            visible_only=kind in {'count', 'text', 'texts', 'visible_exists'}, **details)
            measurements[measurement_key] = deepcopy(measured)
            result[key] = dict(query, **measured)
            notify('measurement','observed',time.monotonic(),status=status,
                   text_length=details.get('text_length'),text_complete=details.get('text_complete'))
        except Exception as exc:
            raise ValueError(f'Step {query["step_id"]} Oracle {key}: {type(exc).__name__}: '
                             + safe_text(str(exc).splitlines()[0], 600)) from exc
    return result


def validate_source(source):
    if not isinstance(source, str) or not source.strip() or len(source) > 24000:
        raise ValueError('Selenium Python source must contain 1..24000 characters')
    if re.search(r'^\s*#\s*webagent-step:', source, re.M):
        raise ValueError('Reserved step marker in model source')
    tree = ast.parse(source)
    if len(list(ast.walk(tree))) > 700:
        raise ValueError('Program exceeds the bounded Python subset')
    allowed = (ast.Module, ast.Expr, ast.Assign, ast.Name, ast.Load, ast.Store, ast.Constant,
               ast.Call, ast.Attribute, ast.Tuple, ast.List, ast.Subscript, ast.keyword,
               ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot,
               ast.In, ast.NotIn, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
               ast.For, ast.If, ast.ListComp, ast.comprehension)
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ValueError('Unsupported Python syntax: ' + type(node).__name__ +
                             '; use assignments, len(), comparisons, bounded for/list comprehensions and Selenium calls')
        if isinstance(node, ast.Attribute) and node.attr not in ATTRIBUTES:
            raise ValueError('Unsupported Selenium member: ' + node.attr)
        if isinstance(node, ast.keyword) and node.arg not in {'timeout', 'poll_frequency'}:
            raise ValueError('Unsupported call keyword')
        if isinstance(node, ast.Constant) and (type(node.value) not in {str, int, float, bool, type(None)}
                or isinstance(node.value, str) and len(node.value) > 2000):
            raise ValueError('Unsupported literal')
        if isinstance(node, ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr == 'until':
            if (len(node.args)!=1 or not isinstance(node.args[0],ast.Call)
                    or not isinstance(node.args[0].func,ast.Attribute)
                    or not isinstance(node.args[0].func.value,ast.Name)
                    or node.args[0].func.value.id!='EC'):
                raise ValueError('Wait.until requires one allowlisted EC predicate')
    def bind(target, names):
        if not isinstance(target, ast.Name):
            raise ValueError('Only local variable assignment/iteration is supported')
        if target.id in ROOT_NAMES or target.id.startswith('_'):
            raise ValueError('Cannot replace a runtime capability')
        names.add(target.id)

    def check(node, names):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in names:
            raise ValueError('Unknown variable: ' + node.id + '; each step has its own local variables')
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1:
                raise ValueError('Only single local variable assignment is supported')
            check(node.value, names)
            bind(node.targets[0], names)
        elif isinstance(node, ast.For):
            if node.orelse:
                raise ValueError('for/else is unsupported; iterate a bounded Selenium result list')
            check(node.iter, names)
            bind(node.target, names)
            for statement in node.body:
                check(statement, names)
        elif isinstance(node, ast.ListComp):
            if len(node.generators) != 1 or node.generators[0].is_async:
                raise ValueError('Use one synchronous bounded list comprehension')
            gen, local = node.generators[0], set(names)
            check(gen.iter, names)
            bind(gen.target, local)
            for condition in gen.ifs:
                check(condition, local)
            check(node.elt, local)
        else:
            for child in ast.iter_child_nodes(node):
                check(child, names)
    check(tree, set(ROOT_NAMES))
    return tree


class NavigationStateError(RuntimeError):
    """A returned WebDriver call did not establish the intended navigation state."""


def check_navigation_state(driver, actions, *, entry=None, current=None):
    """Technical reachability check, never a comparison against business Oracles.

    A different-document link must leave its entry URL/window. Redirects are
    allowed; this does not claim the destination content satisfies the user.
    Fragment controls and links to the current document need not navigate.
    """
    activations = [a for a in actions if 'before_windows' in a]
    observed_windows = bool(actions and entry and current and entry.get('windows'))
    if not activations and not observed_windows:
        return
    last = activations[-1] if activations else actions[-1]
    if observed_windows:
        before, windows, active = entry['windows'], current['windows'], current['current_window']
    else:
        before = last['before_windows']
        windows, active = list(driver.window_handles), driver.current_window_handle
    added = [h for h in windows if h not in before]
    if observed_windows:
        followed = {a.get('after_window') for a in activations if a.get('window_status') == 'switched'}
        added = [h for h in added if h not in followed]
    if added and (len(added) != 1 or active != added[0]):
        last['navigation_check'] = 'unselected_or_ambiguous_window'
        raise NavigationStateError('New window(s) exist but the active window is not the unique destination; '
                                   'old-page generic elements cannot confirm navigation. '
                                   'Use the supported link activation and wait for a destination-specific element.')
    for action in activations:
        if action.get('expects_navigation') and not action.get('navigated'):
            action['navigation_check'] = 'entry_page_unchanged'
            raise NavigationStateError('Link activation has not left its entry page: '
                + action.get('requested_url', '[unknown]') + '; action at line '
                + str(action.get('start_line', '?')) + '. Generic headings/containers on the old page '
                'do not confirm arrival; wait for a destination-specific technical element. '
                'Do not change frozen Oracles or repeat a successful write.')
        action['navigation_check'] = 'reached' if action.get('navigated') else 'not_required'


class Browser:
    # Inside the existing script phase deadline, never an extra per-step budget.
    WINDOW_WAIT_SECONDS = 2.0

    def __init__(self, driver, base_url, *, actions=None, on_action=None, progress=None):
        self._driver, self._origin = driver, origin(base_url)
        self._actions, self._on_action = actions, on_action
        self._progress = progress if progress is not None else {}
        self._pending_window = None

    def _window_handle(self):
        from selenium.common.exceptions import NoSuchWindowException
        try:
            return self._driver.current_window_handle
        except NoSuchWindowException:
            return None

    def _sync_windows(self, *, wait_for_target=False):
        """Follow one new same-origin window; ordinary clicks never sleep.

        Unannounced JS popups are checked on subsequent driver reads / EC polls.
        An explicit new target (or an observed blank new window) gets a bounded
        navigation wait. Never choose an arbitrary handle from multiple windows.
        """
        pending = self._pending_window
        if pending is None:
            return
        record = pending['record']
        previous = dict(record)
        deadline = time.monotonic() + self.WINDOW_WAIT_SECONDS
        waiting = wait_for_target and pending['expects_new']
        selected = None
        try:
            while True:
                handles = list(self._driver.window_handles)
                new = [h for h in handles if h not in pending['handles']]
                record.update(after_windows=handles, new_windows=new)
                if len(new) > 1:
                    record['window_status'] = 'ambiguous'
                    raise RuntimeError('Click opened multiple new windows; automatic selection is unsupported')
                if new:
                    if selected != new[0]:
                        self._driver.switch_to.window(new[0])
                        selected = new[0]
                    url = self._driver.current_url
                    if url not in ('', 'about:blank'):
                        try:
                            self._check_url(url)
                        except (PermissionError, ValueError):
                            record['window_status'] = 'rejected_origin'
                            raise PermissionError('New window is outside the authorized HTTP(S) origin')
                        record.update(window_status='switched', after_window=new[0],
                                      navigation_type='new_window')
                        self._pending_window = None
                        break
                    # window.open() may return a blank window before assigning location.
                    waiting = True
                    record['window_status'] = 'waiting_for_url'
                elif pending['handle'] not in handles:
                    record['window_status'] = 'source_closed'
                    raise RuntimeError('Source window closed without one identifiable new window')
                else:
                    record['window_status'] = 'waiting_for_window' if waiting else 'no_new_window'
                if not waiting:
                    break
                if time.monotonic() >= deadline:
                    from selenium.common.exceptions import TimeoutException
                    record['window_status'] = 'timeout'
                    raise TimeoutException('New-window navigation did not resolve to one authorized page within 2s; '
                                           'popup may be blocked or still blank')
                time.sleep(.05)
        except Exception as exc:
            self._pending_window = None
            # Do not leave later observations/evidence focused on an unauthorized page.
            if pending['handle'] in self._driver.window_handles:
                self._driver.switch_to.window(pending['handle'])
            record.update(status='error', error_type=type(exc).__name__)
            raise
        finally:
            record['after_url'] = self._url()
            record['after_window'] = self._window_handle()
            record['navigated'] = (record['before_url'] != record['after_url']
                                   or record['before_window'] != record['after_window'])
            if record.get('navigation_type') != 'new_window':
                record['navigation_type'] = 'same_window' if record['navigated'] else 'none'
            # A delayed switch updates the ORIGINAL click, not a fabricated action.
            if record['status'] != 'started' and record != previous and self._on_action:
                self._on_action(dict(record))

    def _activate(self, element, target, fn, *, action='click'):
        self._sync_windows()
        # One metadata command also handles clicking an anchor's child and base/form
        # targets. No page JS is exposed to generated Python; no input values are read.
        info = self._driver.execute_script("""
const el = arguments[0], a = el.closest('a[href],area[href]');
const rect = el.getBoundingClientRect();
if (el.tagName !== 'OPTION' && rect.width && rect.height &&
    (rect.top < 80 || rect.bottom > innerHeight - 80)) {
    el.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'});
}
const form = !a && el.form && (el.type === 'submit' || el.type === 'image') ? el.form : null;
const base = document.querySelector('base[target]');
return {href: a ? a.href : '', target: (a ? a.getAttribute('target') :
    form ? el.getAttribute('formtarget') || form.getAttribute('target') : '') ||
    ((a || form) && base ? base.getAttribute('target') : '') || ''};
""", element)
        if info['href']:
            self._check_url(info['href'])
        from .dom_inspector import diagnostic_url
        hint = info['target'].lower()
        before = self._driver.current_url
        def document(url):
            return urlsplit(url)._replace(fragment='').geturl()
        window = dict(handle=self._driver.current_window_handle, handles=list(self._driver.window_handles),
                      expects_new=bool(hint and hint not in {'_self', '_parent', '_top'}),
                      requested_url=diagnostic_url(info['href']) if info['href'] else None,
                      expects_navigation=bool(info['href'] and document(info['href']) != document(before)))
        def activate_and_follow():
            try:
                fn()
            except Exception as exc:
                from selenium.common.exceptions import ElementClickInterceptedException
                if isinstance(exc, ElementClickInterceptedException):
                    try:
                        detail = self._driver.execute_script("""
const el=arguments[0], r=el.getBoundingClientRect();
const x=Math.max(0,Math.min(innerWidth-1,(r.left+r.right)/2));
const y=Math.max(0,Math.min(innerHeight-1,(r.top+r.bottom)/2));
const hit=document.elementFromPoint(x,y);
const brief=n=>n?{tag:n.tagName,id:n.id,classes:String(n.className).slice(0,200)}:null;
return {target:brief(el),blocker:brief(hit),point:{x,y},
        rect:{top:r.top,bottom:r.bottom,left:r.left,right:r.right},
        viewport:{width:innerWidth,height:innerHeight},target_hit:hit===el||el.contains(hit)};
""", element)
                        self._pending_window['record']['click_diagnostic'] = detail
                    except Exception:
                        pass  # Diagnostics must never replace the original Selenium error.
                raise
            self._sync_windows(wait_for_target=True)
            self._check_url(self._driver.current_url)
        self._action(action, target, activate_and_follow, window=window)

    def _url(self):
        from .dom_inspector import diagnostic_url
        try:
            return diagnostic_url(self._driver.current_url)
        except Exception:
            return '[unavailable]'

    def _action(self, action, target, fn, *, window=None):
        # Clear/input actions can follow a click whose JS opened a window later.
        if window is None:
            self._sync_windows()
        self._progress['operation'] = 'action'
        from .dom_inspector import safe_text
        record = dict(action=action,target=safe_text(target,300),before_url=self._url(),status='started')
        record.update(self._progress.get('top_level_statement', {}))
        if window is not None:
            record.update(before_window=window['handle'], before_windows=window['handles'],
                          window_status='pending', expects_new_window=window['expects_new'],
                          requested_url=window['requested_url'], expects_navigation=window['expects_navigation'])
            self._pending_window = dict(window, record=record)
        if self._actions is not None:
            if len(self._actions) >= 200:
                raise ValueError('Action log limit exceeded (200)')
            self._actions.append(record)
        if self._on_action:
            self._on_action(dict(record))
        try:
            result = fn()
            record['status'] = 'complete'
            return result
        except Exception as exc:
            if window is not None:
                self._pending_window = None
            record.update(status='error',error_type=type(exc).__name__)
            raise
        finally:
            if window is not None:
                record.update(after_window=self._window_handle(), after_windows=list(self._driver.window_handles))
            record['after_url'] = self._url()
            record['navigated'] = (record['before_url'] != record['after_url'] or
                                  record.get('before_window') != record.get('after_window'))
            if self._on_action:
                self._on_action(dict(record))

    def _check_url(self, url):
        if origin(url) != self._origin:
            raise PermissionError('Navigation outside the authorized origin')

    def get(self, url):
        self._check_url(url)
        self._sync_windows()
        self._pending_window = None
        from .dom_inspector import diagnostic_url
        self._action('get',diagnostic_url(url),lambda: self._driver.get(url))
        self._check_url(self._driver.current_url)

    def find_element(self, by, value):
        self._sync_windows()
        return Element(self._driver.find_element(by, value), self, target=f'{by}: {value}')

    def find_elements(self, by, value):
        self._sync_windows()
        return [Element(e, self, target=f'{by}: {value}') for e in self._driver.find_elements(by, value)[:200]]

    @property
    def title(self):
        self._sync_windows()
        return self._driver.title

    @property
    def current_url(self):
        self._sync_windows()
        return self._driver.current_url


class Element:
    def __init__(self, element, browser, *, target='element'):
        self._element, self._browser = element, browser
        self._target = target
        identity = ' '.join(element.get_attribute(k) or '' for k in ('type', 'name', 'id', 'autocomplete'))
        if re.search(r'password|passwd|secret|token|api.?key|credit.?card', identity, re.I):
            raise PermissionError('Sensitive controls are outside this demo')

    def click(self):
        self._browser._activate(self._element, self._target, self._element.click)

    def clear(self):
        self._browser._action('clear',self._target,self._element.clear)

    def send_keys(self, *values):
        if not values or sum(len(str(v)) for v in values) > 2000:
            raise ValueError('Input exceeds the research demo limit')
        # Enter may activate a link OR submit a form. Reuse trusted activation,
        # without logging input values or imposing a wait on ordinary text input.
        from selenium.webdriver.common.keys import Keys
        send = lambda: self._element.send_keys(*values)
        if any(Keys.ENTER in str(value) for value in values):
            self._browser._activate(self._element, self._target, send, action='send_keys')
        else:
            self._browser._action('send_keys',self._target,send)

    def get_attribute(self, name):
        if name not in OBSERVED_ATTRIBUTES:
            raise PermissionError('Attribute is not an allowed observation')
        return self._element.get_attribute(name)

    @property
    def text(self):
        return self._element.text[:2000]

    def is_displayed(self):
        return self._element.is_displayed()

    def is_enabled(self):
        return self._element.is_enabled()

    def is_selected(self):
        return self._element.is_selected()

    def find_element(self, by, value):
        return Element(self._element.find_element(by, value), self._browser,target=f'{self._target} > {by}: {value}')

    def find_elements(self, by, value):
        return [Element(e, self._browser,target=f'{self._target} > {by}: {value}')
                for e in self._element.find_elements(by, value)[:200]]


def execute_source(source, driver, base_url, *, actions=None, on_action=None, progress=None):
    """Interpret a finite Selenium subset; no model Python bytecode execution."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    tree = validate_source(source)
    progress = progress if progress is not None else {}
    progress.update(operation='code', completed_end_line=0, source_complete=False)
    browser = Browser(driver, base_url,actions=actions if actions is not None else [],
                      on_action=on_action,progress=progress)

    def wait(target, timeout=10, poll_frequency=.2):
        if target is not browser or not isinstance(timeout, (int, float)) or not .1 <= timeout <= 20:
            raise ValueError('Wait must target the session driver, with timeout 0.1..20')
        return WebDriverWait(browser, timeout, poll_frequency=max(.1, min(1, poll_frequency)))

    def bounded_len(obj):
        if type(obj) not in (list, tuple, str):
            raise ValueError('len() accepts only a bounded list, tuple or string')
        return len(obj)

    env = dict(driver=browser, BASE_URL=base_url, By=By, Keys=Keys, EC=EC, WebDriverWait=wait, len=bounded_len)
    budget = 5000

    def tick():
        nonlocal budget
        budget -= 1
        if budget < 0:
            raise ValueError('Bounded Python operation limit exceeded (5000); simplify iteration')

    def items(obj):
        if type(obj) not in (list, tuple) or len(obj) > 200:
            raise ValueError('Iteration requires a list/tuple with at most 200 items')
        return list(obj)  # snapshot: appending to the source cannot extend iteration

    def append(obj, item):
        if len(obj) >= 200:
            raise ValueError('List exceeds 200 items')
        obj.append(item)

    comparisons = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
                   ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
                   ast.Is: operator.is_, ast.IsNot: operator.is_not,
                   ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}

    def value(node):
        tick()
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return env[node.id]
        if isinstance(node, (ast.Tuple, ast.List)):
            if len(node.elts) > 200:
                raise ValueError('List/tuple exceeds 200 items')
            return tuple(value(x) for x in node.elts) if isinstance(node, ast.Tuple) else [value(x) for x in node.elts]
        if isinstance(node, ast.ListComp):
            gen = node.generators[0]
            sequence = items(value(gen.iter))
            # Comprehension variables have Python's local scope.
            name, missing = gen.target.id, object()
            old = env.get(name, missing)
            result = []
            try:
                for item in sequence:
                    env[name] = item
                    if all(value(condition) for condition in gen.ifs):
                        result.append(value(node.elt))
            finally:
                if old is missing:
                    env.pop(name, None)
                else:
                    env[name] = old
            return result
        if isinstance(node, ast.Compare):
            left = value(node.left)
            for op, right_node in zip(node.ops, node.comparators):
                right = value(right_node)
                if not comparisons[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.BoolOp):
            result = None
            for child in node.values:
                result = value(child)
                if isinstance(node.op, ast.And) and not result or isinstance(node.op, ast.Or) and result:
                    break
            return result
        if isinstance(node, ast.UnaryOp):
            return not value(node.operand)
        if isinstance(node, ast.Attribute):
            obj = value(node.value)
            if type(obj) is list and node.attr == 'append':
                return lambda item: append(obj, item)
            if type(obj) is str and node.attr in {'strip', 'lower'}:
                return getattr(obj, node.attr)
            # Strings/lists/functions cannot expose their Python members.
            if not isinstance(obj, (Browser, Element, WebDriverWait)) and obj not in (By, Keys, EC):
                raise PermissionError('Attribute access on non-Selenium object')
            return getattr(obj, node.attr)
        if isinstance(node, ast.Subscript):
            obj, index = value(node.value), value(node.slice)
            if type(obj) not in (list, tuple) or type(index) is not int or not 0 <= index < 200:
                raise ValueError('Only bounded Selenium result indexing is allowed')
            return obj[index]
        if isinstance(node, ast.Call):
            fn = value(node.func)
            if not callable(fn):
                raise ValueError('Selenium target is not callable')
            args = [value(x) for x in node.args]
            kwargs = {k.arg: value(k.value) for k in node.keywords}
            progress['operation'] = 'wait' if isinstance(node.func,ast.Attribute) and node.func.attr=='until' else 'code'
            return fn(*args, **kwargs)
        raise ValueError('Unsupported expression')

    def statement_run(statement):
        tick()
        progress['statement'] = dict(start_line=statement.lineno,end_line=statement.end_lineno)
        progress['operation'] = 'code'
        if isinstance(statement, ast.Assign):
            env[statement.targets[0].id] = value(statement.value)
        elif isinstance(statement, ast.For):
            for item in items(value(statement.iter)):
                env[statement.target.id] = item
                for child in statement.body:
                    statement_run(child)
        elif isinstance(statement, ast.If):
            for child in statement.body if value(statement.test) else statement.orelse:
                statement_run(child)
        elif isinstance(statement, ast.Expr):
            value(statement.value)
        else:
            raise ValueError('Unsupported statement')

    for statement in tree.body:
        progress['top_level_statement'] = dict(start_line=statement.lineno,end_line=statement.end_lineno,
            start_column=statement.col_offset,end_column=statement.end_col_offset)
        statement_run(statement)
        progress['completed_end_line'] = statement.end_lineno
    # Reconcile before Oracle collection and post-observation use this same driver.
    browser._sync_windows()
    progress['operation'] = 'navigation'
    check_navigation_state(driver, browser._actions)
    progress.update(source_complete=True,operation='complete')


def execution_diagnostics(progress, actions, *, stage, entry=None, current=None):
    """Technical progress only: a returned click is not proof of a business result."""
    completed = [a for a in actions if a['status'] == 'complete']
    failed_statement = progress.get('top_level_statement', {})
    partial = any((a.get('start_line'),a.get('start_column')) ==
                  (failed_statement.get('start_line'),failed_statement.get('start_column')) for a in completed)
    operation = 'evidence' if stage == 'evidence' else progress.get('operation', 'preflight')
    phase = ('complete' if stage == 'complete' else
             'during_action' if operation == 'action' or any(a['status'] != 'complete' for a in actions) else
             'after_action' if completed or (stage == 'evidence' and progress.get('source_complete')) else 'before_action')
    def state(view):
        return {k: view[k] for k in ('url', 'state_hash', 'observation_ref', 'current_window', 'windows')
                if k in (view or {})}
    before, after = state(entry), state(current)
    return dict(phase=phase, failed_operation=operation, source_complete=progress.get('source_complete', False),
                completed_end_line=progress.get('completed_end_line', 0),
                failed_statement=failed_statement if stage != 'complete' else {},
                failed_inner_statement=progress.get('statement', {}) if stage != 'complete' else {},
                successful_actions=len(completed), partial_statement_actions=partial,
                entry_state=before, current_state=after,
                state_changed=(before['state_hash'] != after['state_hash']
                               if before.get('state_hash') and after.get('state_hash') else None),
                url_changed=(before['url'] != after['url'] if 'url' in before and 'url' in after else None))


def validate_wait_repair(source):
    """A wait patch cannot sneak in a second click, navigation or callable alias."""
    tree = ast.parse(source)
    reads = {'until', 'find_element', 'find_elements', 'get_attribute', 'is_displayed',
             'is_enabled', 'is_selected', 'strip', 'lower', 'presence_of_element_located',
             'visibility_of_element_located', 'element_to_be_clickable',
             'invisibility_of_element_located', 'text_to_be_present_in_element'}
    if not tree.body or any(not isinstance(n, (ast.Expr, ast.Assign)) for n in tree.body):
        raise ValueError('Wait-only repair requires simple assignments/expressions with an explicit EC wait')
    waited = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in {'WebDriverWait', 'len'}:
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr not in reads:
                raise ValueError('Wait-only repair cannot add click/input/navigation or callable aliases; '
                                 'return only the failed technical wait, successful actions are locked')
            waited |= node.func.attr == 'until'
    if not waited:
        raise ValueError('Wait-only repair must retain an explicit technical EC wait')


def step_repair_context(source, result):
    """Lock completed top-level statements; never split a partly executed if/for."""
    execution = result.get('execution')
    if not execution:
        return None  # Older/unknown records cannot prove a safe local patch boundary.
    context = dict(execution, original_code=source)
    if execution.get('source_complete') and execution.get('failed_operation') == 'evidence':
        return dict(context, scope='bindings', locked_prefix=source)
    lines = source.splitlines(keepends=True)
    boundary = execution.get('failed_statement', {})
    start, end = boundary.get('start_line', 0), boundary.get('end_line', 0)
    if not 1 <= start <= end <= len(lines):
        return None
    def offset(line, column):
        # AST columns count UTF-8 bytes, not Unicode characters.
        return sum(len(s) for s in lines[:line-1]) + len(lines[line-1].encode('utf-8')[:column].decode('utf-8'))
    first = offset(start,boundary.get('start_column',0))
    last = offset(end,boundary.get('end_column',len(lines[end-1].rstrip().encode('utf-8'))))
    failed = source[first:last]
    if execution.get('failed_operation') == 'wait' and not execution.get('partial_statement_actions'):
        try:
            validate_wait_repair(failed)
        except (ValueError, SyntaxError):
            pass
        else:
            return dict(context, scope='wait', locked_prefix=source[:first],
                        failed_code=failed, locked_suffix=source[last:])
    return dict(context, scope='tail', locked_prefix=source[:first],
                failed_code=source[first:], locked_suffix='')


def split_program(source):
    parts = re.split(r'^# webagent-step: (S[1-9][0-9]*)\s*$', source, flags=re.M)
    if parts[0].strip() or len(parts) < 3:
        raise ValueError('Missing frozen step boundaries')
    steps = [{'id': parts[i], 'source': parts[i+1].strip()} for i in range(1, len(parts), 2)]
    if len({s['id'] for s in steps}) != len(steps):
        raise ValueError('Duplicate program step')
    for step in steps:
        validate_source(step['source'])
    return steps
