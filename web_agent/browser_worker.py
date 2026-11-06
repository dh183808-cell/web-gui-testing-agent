"""Agent4's persistent WebDriver owner. JSON lines in/out, no pickled objects.

Trusted bootstrap and static DOM scripts may use Selenium. Generated snippets
have no imports/builtins and must EXACTLY equal the closed renderer output.
Chrome CDP intercepts outgoing requests before transmission, allowing only the
reviewed origin (including redirect hops/subresources). Missing interception is
a startup failure, never a reason to run unguarded.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import hashlib
import sys
import time
from urllib.parse import urljoin

from .dom_inspector import observe_session
from .dynamic_execution import (ExecutionPolicy, ExecutionRejected, render_source,
                                resolve_inputs, source_digest, step_for, validate_operation)
from .dynamic_schema import DynamicTestContract


def make_dynamic_driver(*, headless: bool, allow_downloads=False):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    options = Options()
    if headless:
        options.add_argument('--headless=new')
    options.add_argument('--disable-background-networking')
    options.add_argument('--disable-sync')
    options.add_argument('--disable-extensions')
    options.add_argument('--no-first-run')
    options.add_argument('--window-size=1280,900')
    options.set_capability('unhandledPromptBehavior', 'dismiss and notify')
    options.add_experimental_option('prefs', {'download_restrictions': 0 if allow_downloads else 3,
        'credentials_enable_service': False, 'profile.password_manager_enabled': False,
        'profile.default_content_setting_values.notifications': 2})
    if os.getenv('CHROME_BINARY'):
        options.binary_location = os.environ['CHROME_BINARY']
    service = Service(os.environ['CHROMEDRIVER_PATH']) if os.getenv('CHROMEDRIVER_PATH') else Service()
    return webdriver.Chrome(service=service, options=options)


class ElementCapability:
    def __init__(self, element, api):
        self._element, self._api = element, api

    def clear(self):
        self._api.trace.append('WebElement.clear')
        self._api.action_started = True
        self._element.clear()

    def send_keys(self, value):
        self._api.trace.append('WebElement.send_keys(reviewed_value)')
        self._api.action_started = True
        self._element.send_keys(value)

    def click(self):
        self._api.trace.append('WebElement.click')
        self._api.action_started = True
        self._element.click()

    def is_selected(self):
        self._api.trace.append('WebElement.is_selected')
        return self._element.is_selected()


class SeleniumCapabilities:
    """Fixed browser API used by canonical generated source; no model JS/API names."""
    def __init__(self, owner, timeout):
        self.owner, self.driver, self.timeout = owner, owner.driver, timeout
        self.trace = []
        self.action_started = False
        self.read_errors = {}
        self.download_value = None
        self.deadline = time.monotonic() + timeout

    def _until(self, condition):
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.common.exceptions import TimeoutException
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutException('Browser attempt deadline exceeded')
        return WebDriverWait(self.driver, remaining, poll_frequency=0.05).until(condition)

    def navigate(self, url):
        self.owner.policy.authorize(url, self.owner.contract.design.target_url)
        self.trace.append('WebDriver.get(reviewed_url)')
        self.action_started = True
        self.driver.get(url)

    def ready(self):
        self.trace.append('wait(document.readyState)')
        self._until(lambda d: d.execute_script('return document.readyState') == 'complete')

    def wait(self, node_id, condition):
        self.trace.append(f'wait({node_id},{condition})')
        node = self.owner.refs[node_id]
        self._until(lambda d: node.is_displayed() and (condition != 'enabled' or node.is_enabled()))
        # Re-check live sensitivity and control identity. A page can mutate a
        # formerly public input into a password/file field after observation.
        actual = self.driver.execute_script(r'''
            const e = arguments[0];
            const type = (e.getAttribute('type') || '').toLowerCase();
            return {tag:e.tagName.toLowerCase(), type,
              private:!!e.closest('[data-private],[data-sensitive]') ||
                ['password','hidden','file'].includes(type) ||
                /password|passwd|secret|token|credential|credit|card.number|ssn|otp|one.time/i
                  .test([e.id,e.name,e.autocomplete,e.getAttribute('aria-label')].join(' '))};
        ''', node)
        snapshot = next(n for n in self.owner.observation['elements'] if n['node_id'] == node_id)
        if actual['private'] or (actual['tag'], actual['type']) != (snapshot['tag'], snapshot['type']):
            raise ExecutionRejected('Target changed identity or became sensitive after observation')
        if not node.is_enabled():
            raise ExecutionRejected('Target became disabled after observation')
        return ElementCapability(node, self)

    def lookup(self, node_id, condition):
        """State checks can observe hidden/absent elements without a visible wait."""
        self.trace.append(f'lookup_state({node_id})')
        if node_id is None:
            step = step_for(self.owner.contract, self.owner.current_step)
            if step.target.role != 'exact_aria_label':
                raise ExecutionRejected('Absence requires exact aria-label semantics')
            # Recheck the whole live document, not just the bounded summary.
            found = self.driver.execute_script('''
                return Array.from(document.querySelectorAll('[aria-label]'))
                    .find(e=>e.getAttribute('aria-label')===arguments[0]) || null;
            ''', step.target.description)
            if found is not None:
                from selenium.common.exceptions import StaleElementReferenceException
                raise StaleElementReferenceException('Absent target changed after observation')
            return None
        node = self.owner.refs[node_id]
        private = self.driver.execute_script("return !!arguments[0].closest('[data-private],[data-sensitive]') || ['password','hidden','file'].includes(arguments[0].type)", node)
        if private:
            raise ExecutionRejected('State target became sensitive')
        return ElementCapability(node, self)

    def capture(self, output, reader, element):
        """Read failures become explicit missing evidence, never invented values."""
        from selenium.common.exceptions import WebDriverException
        self.trace.append('collect(' + reader + ')')
        try:
            if reader == 'exists':
                return element is not None
            if reader == 'visible':
                return element is not None and element._element.is_displayed()
            if reader == 'enabled':
                return element is not None and element._element.is_enabled()
            if reader == 'file':
                return self.download_value
            if reader == 'page_title':
                return self.driver.title[:2000]
            if reader == 'url':
                return self.current_url()
            if element is None:
                raise ValueError('No target for requested reader')
            return {'text': lambda: self.read_text(element), 'value': lambda: self.read_value(element),
                    'checked': element.is_selected, 'count': lambda: self.count(element),
                    'number': lambda: self.read_text(element)}[reader]()
        except (WebDriverException, ValueError) as exc:
            self.read_errors[output] = type(exc).__name__ + ': collection unavailable'
            return None

    def download(self, node):
        if not self.owner.policy.allow_downloads or self.owner.run_dir is None:
            raise ExecutionRejected('Download not authorized', category='permission')
        href = node._element.get_attribute('href')
        self.owner.policy.authorize(href, self.owner.contract.design.target_url)
        if node._element.get_attribute('target') not in ('', None, '_self'):
            raise ExecutionRejected('Download cannot open another context')
        # A separate, application-owned directory for each attempt. Browser
        # filenames never become generated Python or user-chosen paths.
        folder = self.owner.run_dir / 'downloads' / (self.owner.current_step + '_' + str(self.owner.current_attempt))
        folder.mkdir(parents=True, exist_ok=False)
        self.driver.execute_cdp_cmd('Browser.setDownloadBehavior', {'behavior': 'allow', 'downloadPath': str(folder.resolve())})
        try:
            self.trace.append('download(reviewed_anchor)')
            self.action_started = True
            node._element.click()
            def completed(d):
                files = list(folder.iterdir())
                if len(files) == 1 and files[0].is_file() and files[0].suffix.lower() not in ('.crdownload', '.tmp', '.part'):
                    return files[0]
                return False
            path = self._until(completed)
            if not path.resolve().is_relative_to(folder.resolve()) or path.stat().st_size > 10 * 1024 * 1024:
                raise ExecutionRejected('Downloaded file outside limits')
            content = path.read_bytes()
            self.download_value = {'path': path.relative_to(self.owner.run_dir).as_posix(),
                'size': len(content), 'sha256': hashlib.sha256(content).hexdigest(), 'complete': True}
        finally:
            self.driver.execute_cdp_cmd('Browser.setDownloadBehavior', {'behavior': 'deny'})

    def page_token(self):
        self.trace.append('read(page_transition_token)')
        return self.driver.execute_script('''
            const html = document.documentElement.innerHTML;
            let hash = 2166136261;
            for (let i=0; i<html.length; i++) hash = Math.imul(hash ^ html.charCodeAt(i), 16777619);
            return location.href + '\\n' + (hash >>> 0);
        ''')

    def transition(self, before, change):
        self.trace.append(f'wait(transition:{change})')
        if change == 'dom':
            self._until(lambda d: self.page_token() != before)
        elif change == 'navigation':
            self._until(lambda d: d.current_url != before.split('\n', 1)[0])
        self.ready()

    def guard_target(self, node):
        self.trace.append('guard(navigation_target)')
        element = node._element
        if element.get_dom_attribute('download') is not None:
            raise ExecutionRejected('Downloads are disabled')
        if element.get_attribute('target') not in (None, '', '_self'):
            raise ExecutionRejected('New browsing contexts are disabled')
        href = element.get_attribute('href')
        form_action = self.driver.execute_script(
            'return arguments[0].form ? arguments[0].form.action : null', element)
        for url in (href, form_action):
            if url:
                self.owner.policy.authorize(urljoin(self.driver.current_url, url), self.owner.contract.design.target_url)

    def select_by_visible_text(self, node, value):
        from selenium.webdriver.support.ui import Select
        self.trace.append('Select.select_by_visible_text(reviewed_value)')
        self.action_started = True
        Select(node._element).select_by_visible_text(value)

    def read_text(self, node):
        self.trace.append('WebElement.text')
        # Text from a public parent must not reintroduce excluded private children.
        return self.driver.execute_script('''
            const root=arguments[0], walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
            const parts=[];
            while(walker.nextNode()) {
                const e=walker.currentNode.parentElement;
                if(!e || e.closest('[data-private],[data-sensitive],script,style,input,textarea,[hidden],[aria-hidden="true"]')) continue;
                if(!e.getClientRects().length || getComputedStyle(e).visibility==='hidden') continue;
                parts.push(walker.currentNode.textContent);
            }
            return parts.join(' ').replace(/\\s+/g,' ').trim().slice(0,2000);
        ''', node._element)

    def read_value(self, node):
        self.trace.append('WebElement.get_property(value)')
        value = node._element.get_property('value')
        if value is None:
            raise ExecutionRejected('Selected element has no value property')
        return str(value)[:2000]

    def current_url(self):
        self.trace.append('WebDriver.current_url')
        # Never return query/fragment values to the model/evidence store.
        from urllib.parse import urlsplit, urlunsplit
        url = urlsplit(self.driver.current_url)
        return urlunsplit((url.scheme, url.netloc, url.path, '', ''))

    def count(self, node):
        self.trace.append('WebElement.find_elements(direct_children)')
        return len(node._element.find_elements('xpath', './*'))


class BrowserOwner:
    def __init__(self, contract, policy, *, headless=True, driver_factory=None, guard_factory=None, run_dir=None):
        self.contract = contract
        self.policy = policy
        self.policy.authorize(contract.design.target_url, contract.design.target_url)
        self.driver = None
        self.guard = None
        self.observation, self.refs, self.state = None, {}, {}
        self.observation_count = 0
        self.blocked = []
        self.network_errors = []
        self.started = time.monotonic()
        self.run_dir = Path(run_dir).resolve() if run_dir else None
        self.attempts, self.completed_artifacts = {}, {}
        self.current_step, self.current_attempt = None, 0
        self.probe_count = 0
        self.last_errors = {}
        self.repair_count = 0
        if contract.design.assumptions or contract.design.missing_information or contract.design.unsupported_requests:
            raise ExecutionRejected('Contract has unresolved planning information')
        if any(p.status != 'provided' for p in contract.design.preconditions):
            raise ExecutionRejected('Contract has unknown preconditions')
        try:
            self.driver = (driver_factory(headless=headless) if driver_factory else
                           make_dynamic_driver(headless=headless, allow_downloads=policy.allow_downloads))
            self.driver.set_page_load_timeout(policy.step_timeout)
            self.driver.set_script_timeout(policy.step_timeout)
            self.driver.execute_cdp_cmd('Browser.setDownloadBehavior', {'behavior': 'deny'})
            from .browser_network import OriginGuard
            self.guard = (guard_factory or OriginGuard)(self.driver, policy, contract.design.target_url)
            self.blocked, self.network_errors = self.guard.blocked, self.guard.errors
        except Exception:
            self.close()
            raise

    def check(self, *, allow_blank=False):
        if time.monotonic() - self.started > self.policy.total_timeout:
            raise TimeoutError('Total browser execution budget exceeded')
        if self.blocked or self.network_errors:
            raise ExecutionRejected('Network policy violation; session stopped', category='permission')
        if len(self.driver.window_handles) != 1:
            raise ExecutionRejected('Unexpected new browser context')
        if self.driver.current_url in ('about:blank', 'data:,') and allow_blank and not self.state:
            return
        self.policy.authorize(self.driver.current_url, self.contract.design.target_url)

    def observe(self):
        self.check(allow_blank=True)
        if self.observation_count >= self.policy.max_observations:
            raise ExecutionRejected('Maximum observations exceeded')
        self.observation_count += 1
        self.observation, self.refs = observe_session(self.driver,
            observation_number=self.observation_count, max_elements=self.policy.max_elements)
        return self.observation

    def execute(self, artifact, *, timeout=None):
        self.check(allow_blank=True)
        if type(artifact) is not dict or set(artifact) != {'step_id', 'operation', 'source'}:
            raise ExecutionRejected('Invalid execution envelope')
        step = step_for(self.contract, artifact['step_id'])
        if step.step_id in self.state or not set(step.depends_on) <= self.state.keys():
            raise ExecutionRejected('Step already executed or dependencies incomplete')
        if len(self.state) >= self.policy.max_steps:
            raise ExecutionRejected('Maximum steps exceeded')
        if self.observation is None:
            raise ExecutionRejected('Observe before executing a step')
        inputs = resolve_inputs(self.contract, step, self.state)
        validate_operation(self.contract, step.step_id, self.observation, artifact['operation'], inputs, self.policy)
        canonical = render_source(self.contract, artifact['operation'])
        if type(artifact['source']) is not str or artifact['source'] != canonical:
            raise ExecutionRejected('Source differs from trusted canonical Selenium rendering')
        attempt = self.attempts.get(step.step_id, 0) + 1
        if attempt > 1 + self.policy.max_repairs:
            raise ExecutionRejected('Worker recovery budget exhausted')
        if attempt > 1:
            from urllib.parse import urlsplit
            import ipaddress
            host = urlsplit(self.contract.design.target_url).hostname
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = host == 'localhost'
            previous = self.last_errors.get(step.step_id, {})
            if (self.repair_count >= self.policy.max_repairs or not local and not self.policy.allow_live_recovery or
                    previous.get('category') not in ('locator', 'interaction', 'wait') or
                    type(previous.get('action_started')) is not bool or
                    previous['action_started'] and step.action not in ('observe', 'wait')):
                raise ExecutionRejected('Worker rejected unsafe or unauthorized recovery')
            self.repair_count += 1
        self.attempts[step.step_id] = attempt
        self.current_step, self.current_attempt = step.step_id, attempt
        budget = self.operation_budget(timeout)
        api = SeleniumCapabilities(self, budget)
        scope = {'__builtins__': {}, 'api': api, 'inputs': inputs, 'target_url': self.contract.design.target_url}
        started = time.monotonic()
        try:
            exec(compile(canonical, f'<generated:{step.step_id}>', 'exec'), scope, scope)
            self.check()
            outputs = scope['outputs']
            if set(outputs) != set(step.outputs):
                raise ExecutionRejected('Unexpected output mapping')
            self.state[step.step_id] = outputs
            self.completed_artifacts[step.step_id] = artifact
            evidence = self.collect_records(step, outputs, api, source_digest(canonical), attempt)
            return {'step_id': step.step_id, 'status': 'COMPLETE', 'outputs': outputs, 'evidence': evidence,
                    'source_sha256': source_digest(canonical), 'contract_digest': self.contract.design_digest,
                    'session_id': self.driver.session_id, 'observation_id': self.observation['observation_id'],
                    'url': api.current_url(), 'api_trace': api.trace, 'seconds': time.monotonic() - started,
                    'attempt': attempt}
        except Exception as exc:
            from selenium.common.exceptions import (StaleElementReferenceException, NoSuchElementException,
                TimeoutException, ElementClickInterceptedException, ElementNotInteractableException, WebDriverException)
            if not isinstance(exc, WebDriverException):
                raise
            self.check()  # policy/environment failures are fatal, never repairable
            category = ('locator' if isinstance(exc, (StaleElementReferenceException, NoSuchElementException)) else
                        'wait' if isinstance(exc, TimeoutException) else
                        'interaction' if isinstance(exc, (ElementClickInterceptedException, ElementNotInteractableException)) else 'environment')
            failure = {'step_id': step.step_id, 'category': category, 'type': type(exc).__name__,
                       'action_started': api.action_started, 'message': 'Browser operation failed'}
            self.last_errors[step.step_id] = failure
            return {'step_id': step.step_id, 'status': 'ERROR', 'attempt': attempt,
                'source_sha256': source_digest(canonical), 'contract_digest': self.contract.design_digest,
                'session_id': self.driver.session_id, 'observation_id': self.observation['observation_id'],
                'api_trace': api.trace, 'outputs': {}, 'evidence': {},
                'error': failure}
        finally:
            # Any action invalidates the observation, even on the same URL.
            self.observation, self.refs = None, {}

    def collect_records(self, step, outputs, api, digest, attempt, *, source='selenium', collection_attempt=1):
        from .dynamic_verification import make_record
        artifacts = []
        if self.run_dir and self.observation:
            name = f"worker_dom_{self.observation_count:03d}.json"
            (self.run_dir / name).write_text(json.dumps(self.observation, ensure_ascii=False), encoding='utf-8')
            artifacts.append(name)
        records = {}
        for req in self.contract.design.evidence_requirements:
            if req.step_id != step.step_id or req.output not in outputs:
                continue
            value = outputs[req.output]
            refs = artifacts + ([value['path']] if req.kind == 'file' and type(value) is dict else [])
            records[req.evidence_id] = make_record(self.contract, req, value=value,
                status='observed' if value is not None else 'failed' if req.output in api.read_errors else 'missing',
                reason=api.read_errors.get(req.output, 'Value unavailable') if value is None else None,
                attempt=attempt, collection_attempt=collection_attempt, url=api.current_url(),
                observation_id=self.observation['observation_id'], session_id=self.driver.session_id,
                source_sha256=digest, artifacts=refs, source='download' if req.kind == 'file' else source)
        return records

    def operation_budget(self, timeout):
        import math
        if timeout is not None and (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
            raise ExecutionRejected('Invalid command timeout')
        # Leave IPC time for returning structured Selenium timeouts. A blocked
        # command still hits the parent's harder deadline and kills the worker.
        budget = min(timeout or self.policy.step_timeout, self.policy.step_timeout) * 0.65
        self.driver.set_page_load_timeout(budget)
        self.driver.set_script_timeout(budget)
        return budget

    def probe(self, request, artifact, *, timeout=None):
        from .dynamic_verification import READERS, validate_probe
        self.check()
        from urllib.parse import urlsplit
        import ipaddress
        host = urlsplit(self.contract.design.target_url).hostname
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host == 'localhost'
        if not local and not self.policy.allow_live_probes:
            raise ExecutionRejected('Worker requires extra live probe permission', category='permission')
        if not self.state or request['step_id'] != next(reversed(self.state)):
            raise ExecutionRejected('Probe cannot replay a historical checkpoint')
        validate_probe(self.contract, request, current_step_id=next(reversed(self.state)))
        if self.probe_count >= self.policy.max_probes:
            raise ExecutionRejected('Worker probe budget exhausted')
        self.probe_count += 1
        step = step_for(self.contract, request['step_id'])
        if step.action not in ('observe', 'wait'):
            raise ExecutionRejected('Probe never repeats action steps')
        if self.observation is None:
            raise ExecutionRejected('Probe requires a new observation')
        inputs = resolve_inputs(self.contract, step, self.state)
        validate_operation(self.contract, step.step_id, self.observation, artifact['operation'], inputs, self.policy)
        if artifact['source'] != render_source(self.contract, artifact['operation']):
            raise ExecutionRejected('Invalid probe source')
        api = SeleniumCapabilities(self, self.operation_budget(timeout))
        required = [r for r in self.contract.design.evidence_requirements if r.evidence_id in request['evidence_ids']]
        canonical = render_source(self.contract, artifact['operation'])
        scope = {'__builtins__': {}, 'api': api, 'inputs': inputs, 'target_url': self.contract.design.target_url}
        try:
            exec(compile(canonical, f'<probe:{step.step_id}>', 'exec'), scope, scope)
            self.check()
        except Exception as exc:
            from selenium.common.exceptions import WebDriverException
            if not isinstance(exc, WebDriverException):
                raise
            self.observation, self.refs = None, {}
            return {'status': 'ERROR', 'evidence': {}, 'outputs': {}, 'step_id': step.step_id,
                    'error_type': type(exc).__name__, 'api_trace': api.trace}
        outputs = {r.output: scope['outputs'][r.output] for r in required}
        records = self.collect_records(step, outputs, api, source_digest(artifact['source']),
            self.attempts[step.step_id], source='probe', collection_attempt=self.probe_count + 1)
        for key, value in outputs.items():
            if self.state[step.step_id].get(key) is None and value is not None:
                self.state[step.step_id][key] = value
        self.observation, self.refs = None, {}
        return {'status': 'COMPLETE', 'evidence': records, 'outputs': dict(self.state[step.step_id]),
                'api_trace': api.trace, 'step_id': step.step_id, 'source_sha256': source_digest(canonical),
                'observation_id': request.get('observation_id', artifact['operation']['observation_id'])}

    def close(self):
        if self.driver is not None:
            try:
                self.driver.quit()
            finally:
                self.driver = None
                if self.guard:
                    self.guard.close()


def main():
    owner = None
    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                command = request['command']
                if command == 'start' and owner is None:
                    owner = BrowserOwner(DynamicTestContract.from_dict(request['contract']),
                        ExecutionPolicy(**request['policy']), headless=request['headless'], run_dir=request['run_dir'])
                    value = {'session_id': owner.driver.session_id, 'network_guard': 'cdp_fetch_before_request_same_origin'}
                elif command == 'observe' and owner:
                    value = owner.observe()
                elif command == 'execute' and owner:
                    value = owner.execute(request['artifact'], timeout=request.get('timeout'))
                elif command == 'probe' and owner:
                    value = owner.probe(request['request'], request['artifact'], timeout=request.get('timeout'))
                elif command == 'close':
                    if owner:
                        owner.close()
                    print(json.dumps({'ok': True, 'result': {'closed': True}}), flush=True)
                    return
                else:
                    raise ExecutionRejected('Invalid worker command/state')
                print(json.dumps({'ok': True, 'result': value}, ensure_ascii=False), flush=True)
            except Exception as exc:
                # Selenium exception strings may contain page/driver data. Expose
                # bounded application policy messages and the exception class only.
                message = str(exc)[:300] if isinstance(exc, (ExecutionRejected, TimeoutError)) else 'Browser worker operation failed'
                if owner and (owner.blocked or owner.network_errors):
                    message = 'Network policy violation; session stopped before further execution'
                print(json.dumps({'ok': False, 'error_type': type(exc).__name__, 'error': message,
                                  'category': getattr(exc, 'category', 'environment')}), flush=True)
                return  # Fail closed: no further commands after any failure.
    finally:
        if owner:
            owner.close()


if __name__ == '__main__':
    main()
