"""Small Selenium Python subset, interpreted without exec/eval or Python imports.

This is a capability restriction for the local research demo, NOT an OS sandbox.
Only the trusted wrappers own Selenium objects. No model object reaches getattr.
"""
from __future__ import annotations

import ast
import operator
import re
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
    'data-testid', 'disabled', 'selected', 'role', 'placeholder'})
LOCATORS = {'css': 'css selector', 'css selector': 'css selector', 'css_selector': 'css selector',
            'xpath': 'xpath', 'id': 'id', 'name': 'name'}
READS = {'count', 'text', 'texts', 'selected', 'selected_text', 'visible', 'enabled', 'attribute', 'exists', 'visible_exists'}
EVIDENCE_GUIDE = '''Trusted Runtime evidence semantics (not model Python return values):
by supports css/xpath/id/name. count returns an INTEGER of VISIBLE matched elements
(is_displayed filtered); texts returns their visible text list. Neither counts hidden
nodes. exists returns a BOOLEAN for any DOM match (including hidden nodes);
visible_exists returns a BOOLEAN for any visible match. Empty collections yield
count=0, texts=[], exists=false, visible_exists=false. selected/visible/enabled read
the native state of exactly ONE existing target; missing/ambiguous targets are UNKNOWN,
not false. text reads one visible target; attribute reads an allowed attribute string.
selected_text reads the displayed label of the selected option of ONE native select,
e.g. value="all" with label "All fields" returns "All fields", not "all".
Missing/hidden select or missing/ambiguous selected options are UNKNOWN. Target the
select itself. selected_value is NOT supported. attribute(value) is the internal value;
text on a specific selected option is also supported for its label.
No implicit count-to-bool or string-to-number conversion. At most 200 matches.'''


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


def collect_measurements(driver, base_url, bindings, *, step_id=None, batch=False):
    """Native scalar semantics; Worker batches safety/visibility, never text for counts.

    batch=False retains compatibility with callers supplying minimal driver adapters.
    Both paths use Selenium's own visibility predicate and the same evidence contract.
    """
    from .dom_inspector import safe_text, safe_href, displayed_atom
    result, matched = {}, {}
    for key, query in normalize_bindings(bindings, step_id=step_id).items():
        try:
            locator = LOCATORS[query['by']], query['selector']
            if locator not in matched:
                nodes = driver.find_elements(*locator)
                if len(nodes) > 200:
                    raise ValueError('More than 200 matches; narrow the locator instead of truncating a count')
                if batch and nodes:
                    states = driver.execute_script('const displayed = (' + displayed_atom() + ');' + r'''
return arguments[0].map(el => ({
    sensitive:/password|passwd|secret|token|api.?key|credit.?card/i.test(
        ["type","name","id","autocomplete"].map(k => el.getAttribute(k) || "").join(" ")),
    visible:displayed(el)
}));
''', nodes)
                    if len(states) != len(nodes):
                        raise ValueError('Incomplete batch visibility response')
                    if any(s['sensitive'] for s in states):
                        raise PermissionError('Sensitive controls are outside this demo')
                    wrapped = nodes  # Scalar text/state reads still use native WebDriver.
                    visible = [n for n,s in zip(nodes,states) if s['visible']]
                else:
                    wrapped = [Element(n, Browser(driver, base_url)) for n in nodes]
                    visible = [n for n in wrapped if n.is_displayed()]
                matched[locator] = nodes, wrapped, visible
            nodes, wrapped, visible = matched[locator]
            kind, value, status = query['read'], None, 'observed'
            if kind == 'count':
                value = len(visible)
            elif kind == 'exists':
                value = bool(nodes)
            elif kind == 'visible_exists':
                value = bool(visible)
            elif kind == 'texts':
                value = [safe_text(n.text, 500) for n in visible]
            elif len(nodes) != 1:
                status = 'missing' if not nodes else 'ambiguous'
            elif kind == 'text':
                value = safe_text(wrapped[0].text, 1000) if visible else None
                status = 'observed' if visible else 'hidden'
            elif kind == 'selected_text':
                if nodes[0].tag_name.lower() != 'select':
                    raise ValueError('selected_text requires a native select target; use text for an option')
                if not visible:
                    status = 'hidden'
                else:
                    from selenium.webdriver.common.by import By
                    options = nodes[0].find_elements(By.TAG_NAME, 'option')
                    if len(options) > 200:
                        raise ValueError('selected_text supports at most 200 options')
                    selected = [n for n in options if n.is_selected()]
                    if len(selected) != 1:
                        status = 'selection_missing' if not selected else 'selection_ambiguous'
                    else:
                        value = safe_text(Element(selected[0], Browser(driver, base_url)).text, 1000)
            elif kind in {'selected', 'visible', 'enabled'}:
                method = {'selected': 'is_selected', 'visible': 'is_displayed', 'enabled': 'is_enabled'}[kind]
                value = getattr(wrapped[0], method)()
            elif kind == 'attribute':
                raw = wrapped[0].get_attribute(query['attribute'])
                value = (safe_href(raw) if query['attribute'] == 'href' else safe_text(raw, 1000)) if raw is not None else None
                if raw is None:
                    status = 'attribute_missing'
            result[key] = dict(query, value=value, status=status, matched=len(nodes), visible=len(visible),
                               visible_only=kind in {'count', 'text', 'texts', 'visible_exists'})
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


class Browser:
    def __init__(self, driver, base_url, *, actions=None, on_action=None, progress=None):
        self._driver, self._origin = driver, origin(base_url)
        self._actions, self._on_action = actions, on_action
        self._progress = progress if progress is not None else {}

    def _url(self):
        from .dom_inspector import diagnostic_url
        try:
            return diagnostic_url(self._driver.current_url)
        except Exception:
            return '[unavailable]'

    def _action(self, action, target, fn):
        self._progress['operation'] = 'action'
        if self._actions is None:
            return fn()
        from .dom_inspector import safe_text
        record = dict(action=action,target=safe_text(target,300),before_url=self._url(),status='started')
        record.update(self._progress.get('top_level_statement', {}))
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
            record.update(status='error',error_type=type(exc).__name__)
            raise
        finally:
            record['after_url'] = self._url()
            record['navigated'] = record['before_url'] != record['after_url']
            if self._on_action:
                self._on_action(dict(record))

    def _check_url(self, url):
        if origin(url) != self._origin:
            raise PermissionError('Navigation outside the authorized origin')

    def get(self, url):
        self._check_url(url)
        from .dom_inspector import diagnostic_url
        self._action('get',diagnostic_url(url),lambda: self._driver.get(url))
        self._check_url(self._driver.current_url)

    def find_element(self, by, value):
        return Element(self._driver.find_element(by, value), self, target=f'{by}: {value}')

    def find_elements(self, by, value):
        return [Element(e, self, target=f'{by}: {value}') for e in self._driver.find_elements(by, value)[:200]]

    @property
    def title(self):
        return self._driver.title

    @property
    def current_url(self):
        return self._driver.current_url


class Element:
    def __init__(self, element, browser, *, target='element'):
        self._element, self._browser = element, browser
        self._target = target
        identity = ' '.join(element.get_attribute(k) or '' for k in ('type', 'name', 'id', 'autocomplete'))
        if re.search(r'password|passwd|secret|token|api.?key|credit.?card', identity, re.I):
            raise PermissionError('Sensitive controls are outside this demo')

    def click(self):
        href = self._element.get_attribute('href')
        if href:
            self._browser._check_url(href)
        self._browser._action('click',self._target,self._element.click)
        self._browser._check_url(self._browser.current_url)

    def clear(self):
        self._browser._action('clear',self._target,self._element.clear)

    def send_keys(self, *values):
        if not values or sum(len(str(v)) for v in values) > 2000:
            raise ValueError('Input exceeds the research demo limit')
        # Log the action and locator, never input text/credentials.
        self._browser._action('send_keys',self._target,lambda: self._element.send_keys(*values))

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
        return {k: view[k] for k in ('url', 'state_hash', 'observation_ref') if k in (view or {})}
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
