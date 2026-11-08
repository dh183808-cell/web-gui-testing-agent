"""Window-following regressions; live acceptance is opt-in and uses local Chrome."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
import time
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
import uuid

import pytest
from selenium.common.exceptions import NoSuchElementException, TimeoutException

from web_agent import checked_selenium as checked
from web_agent.checked_selenium import NavigationStateError
from web_agent.local_worker import checked_execution

BASE = 'https://authorized.test/'
CLICK = 'driver.find_element(By.ID,"open").click()'
QUERY = {'O1': dict(by='id', selector='destination', read='text', step_id='S1')}


class Clock:
    now = 0.
    def monotonic(self): return self.now
    def sleep(self, seconds): self.now += seconds


class Driver:
    """Explicit window-capable double; no claim of real browser acceptance."""
    session_id = 'offline'
    def __init__(self, mode, clock):
        self.mode, self.clock, self.current_window_handle = mode, clock, 'source'
        self.pages = {'source': BASE}
        self.clicked_at = None
        self.calls = []
        self.switch_to = SimpleNamespace(window=self.switch)
    def switch(self, handle):
        assert handle in self.pages
        self.calls.append(('switch', handle))
        self.current_window_handle = handle
    @property
    def window_handles(self):
        self.calls.append(('handles',))
        if self.clicked_at is not None:
            elapsed = self.clock.now-self.clicked_at
            if self.mode == 'delayed_same' and elapsed >= .15:
                self.pages['source'] = BASE+'detail'
            if self.mode in {'new', 'same_url', 'cross', 'multiple', 'blank', 'stays_blank', 'closed'} or self.mode in {'delayed', 'js_delayed'} and elapsed >= .15:
                self.pages['destination'] = ('https://other.test/' if self.mode == 'cross' else
                    'about:blank' if self.mode == 'stays_blank' or self.mode == 'blank' and elapsed < .15 else
                    BASE if self.mode == 'same_url' else BASE+'detail')
                if self.mode == 'multiple': self.pages['another'] = BASE+'other'
                if self.mode == 'closed': self.pages.pop('source',None)
            if self.mode == 'source_closed': self.pages.pop('source',None)
        return list(self.pages)
    @property
    def current_url(self): return self.pages[self.current_window_handle]
    @property
    def title(self): return self.current_url
    def execute_script(self, script, *args):
        assert "getAttribute('target')" in script
        return dict(href=BASE+'detail' if self.mode != 'js_delayed' else '',
                    target='_blank' if self.mode in {'new','delayed','blank','blocked'} else '')
    def find_element(self, by, selector):
        nodes = self.find_elements(by, selector)
        if not nodes: raise NoSuchElementException(selector)
        return nodes[0]
    def find_elements(self, by, selector):
        if selector == 'open' or selector == 'destination' and self.current_url == BASE+'detail':
            return [Node(self)]
        return []
    def view(self):
        return dict(url=self.current_url, current_window=self.current_window_handle,
            windows=self.window_handles, observation_ref='snapshot.json', observation_id='snapshot',
            state_hash=self.current_window_handle)


class Node:
    text = 'Destination page'
    def __init__(self, driver): self.driver = driver
    def get_attribute(self, name): return ''
    def is_displayed(self): return True
    def click(self):
        self.driver.clicked_at = self.driver.clock.now
        if self.driver.mode == 'same': self.driver.pages['source'] = BASE+'detail'
    def send_keys(self,*values):
        from selenium.webdriver.common.keys import Keys
        if Keys.ENTER in values: self.click()


@pytest.mark.parametrize('mode,navigation', [('same','same_window'),('new','new_window'),
                                           ('delayed','new_window'),('blank','new_window')])
def test_follow_window_before_operations_observation_and_oracles(monkeypatch, mode, navigation):
    clock = Clock()
    monkeypatch.setattr(checked, 'time', clock)
    driver, actions = Driver(mode, clock), []
    checked.execute_source(CLICK+'\ntext=driver.find_element(By.ID,"destination").text',
                           driver, BASE, actions=actions)
    values = checked.collect_measurements(driver, BASE, QUERY)
    assert values['O1']['value'] == 'Destination page'
    assert actions[0]['status'] == 'complete' and actions[0]['navigated']
    assert actions[0]['navigation_type'] == navigation
    assert actions[0]['after_window'] == driver.current_window_handle
    assert (clock.now > 0) == (mode in {'delayed','blank'})


@pytest.mark.parametrize('mode',['same','new','delayed','blank'])
def test_enter_activation_uses_same_navigation_and_evidence_path(monkeypatch,mode):
    clock = Clock()
    monkeypatch.setattr(checked,'time',clock)
    driver,actions = Driver(mode,clock),[]
    checked.execute_source('driver.find_element(By.ID,"open").send_keys(Keys.ENTER)\n'
        'WebDriverWait(driver,.1).until(EC.presence_of_element_located((By.ID,"destination")))',
        driver,BASE,actions=actions)
    assert checked.collect_measurements(driver,BASE,QUERY)['O1']['value'] == 'Destination page'
    assert actions[0]['action'] == 'send_keys'
    assert actions[0]['navigation_check'] == 'reached'
    assert actions[0]['after_window'] == ('source' if mode=='same' else 'destination')


def test_generic_old_heading_cannot_confirm_unchanged_link_navigation(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(checked,'time',clock)
    driver,actions,progress = Driver('unchanged',clock),[],{}
    # "open" exists on BOTH pages, just as a generic h1/container can.
    with pytest.raises(NavigationStateError,match='destination-specific'):
        checked.execute_source(CLICK+'\nWebDriverWait(driver,.1).until('
            'EC.presence_of_element_located((By.ID,"open")))',driver,BASE,actions=actions,progress=progress)
    assert actions[0]['status'] == 'complete'
    assert actions[0]['navigation_check'] == 'entry_page_unchanged'
    assert progress['operation'] == 'navigation' and not progress['source_complete']


def test_unselected_new_window_cannot_be_reported_as_navigation_complete():
    driver = SimpleNamespace(window_handles=['source','new'],current_window_handle='source')
    actions = [dict(before_windows=['source'],action='send_keys',status='complete')]
    with pytest.raises(NavigationStateError,match='active window'):
        checked.check_navigation_state(driver,actions)
    assert actions[0]['navigation_check'] == 'unselected_or_ambiguous_window'


def test_native_click_failure_records_actual_hit_target_without_js_click():
    from selenium.common.exceptions import ElementClickInterceptedException
    driver, actions, scripts = Driver('same', Clock()), [], []
    node = Node(driver)
    def blocked():
        raise ElementClickInterceptedException('synthetic obstruction')
    node.click = blocked
    driver.find_element = lambda *args: node
    metadata = driver.execute_script
    def execute(script, *args):
        scripts.append(script)
        if 'elementFromPoint' in script:
            return dict(target={'tag':'A'}, blocker={'tag':'DIV','id':'overlay'},target_hit=False)
        return metadata(script, *args)
    driver.execute_script = execute
    with pytest.raises(ElementClickInterceptedException):
        checked.execute_source(CLICK, driver, BASE, actions=actions)
    assert actions[0]['status'] == 'error'
    assert actions[0]['click_diagnostic']['blocker']['id'] == 'overlay'
    assert 'scrollIntoView' in scripts[0]
    assert not any('.click(' in source for source in scripts)


def test_late_unselected_window_in_post_observation_enters_repair_not_complete(monkeypatch):
    driver = SimpleNamespace(session_id='offline')
    def execute(*args,actions,**kwargs):
        actions.append(dict(action='send_keys',status='complete'))
    monkeypatch.setattr(checked,'execute_source',execute)
    count = []
    def capture(**kwargs):
        count.append(1)
        return dict(url=BASE,current_window='source',windows=['source'] if len(count)==1 else ['source','late'],
                    observation_ref='snapshot.json',observation_id='snapshot',state_hash=str(len(count)))
    result = checked_execution(driver,BASE,dict(source='driver.title'),capture=capture,save_screenshot=lambda n:True)
    assert result['status'] == 'ERROR' and result['error_type'] == 'NavigationStateError'
    assert result['failure_domain'] == 'navigation'  # Pipeline permits normal bounded repair.
    assert result['execution']['failed_operation'] == 'navigation'
    assert result['script_status'] == 'COMPLETE' and result['screenshot_ref'] == 'snapshot.png'


def test_sequential_resolved_new_windows_are_not_a_simultaneous_ambiguity():
    actions = [dict(before_windows=['source'],window_status='switched',after_window='one'),
               dict(before_windows=['source','one'],window_status='switched',after_window='two')]
    checked.check_navigation_state(None,actions,
        entry=dict(windows=['source']),current=dict(windows=['source','one','two'],current_window='two'))


def test_agent3_receives_navigation_error_and_repairs_only_unfinished_wait():
    from web_agent.agents.generator import Agent3SeleniumGenerator
    class RealClock:
        @property
        def now(self): return time.monotonic()
    driver = Driver('delayed_same',RealClock())
    code = CLICK+'\nWebDriverWait(driver,1).until(EC.presence_of_element_located((By.ID,"open")))'
    result = checked_execution(driver,BASE,dict(source=code,step_id='S1'),
        capture=lambda **kw:driver.view(),save_screenshot=lambda n:True)
    assert result['status'] == 'ERROR' and result['error_type'] == 'NavigationStateError'
    assert result['execution']['phase'] == 'after_action'
    repair = checked.step_repair_context(code,result)
    assert repair['locked_prefix'] == CLICK+'\n'
    class Model:
        def complete_json(self,system,user):
            payload = json.loads(user)
            assert payload['actual_error']['error_type'] == 'NavigationStateError'
            assert payload['previous_code'] == code
            assert 'destination-specific' in system and 'NavigationStateError' in system
            return dict(code='WebDriverWait(driver,1).until('
                'EC.presence_of_element_located((By.ID,"destination")))',bindings={})
    proposal = Agent3SeleniumGenerator().develop_step({'oracles':[]},{'id':'S1'},driver.view(),
        llm=Model(),previous=dict(code=code,bindings={}),error=result,repair_context=repair)
    assert proposal['code'].count('.click()') == 1
    fresh = Driver('delayed_same',RealClock())
    replay = checked_execution(fresh,BASE,dict(source=proposal['code'],step_id='S1'),
        capture=lambda **kw:fresh.view(),save_screenshot=lambda n:True)
    assert replay['status'] == 'COMPLETE' and replay['after_url'] == BASE+'detail'


def test_ordinary_click_has_no_sleep_and_delayed_js_is_detected_on_read(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(checked, 'time', clock)
    driver, actions, events = Driver('js_delayed', clock), [], []
    browser = checked.Browser(driver, BASE, actions=actions, on_action=events.append)
    browser.find_element('id','open').click()
    assert clock.now == 0 and driver.current_window_handle == 'source'
    assert actions[0]['window_status'] == 'no_new_window'
    clock.now = .2  # Next explicit-wait poll, without an unconditional click sleep.
    assert browser.find_element('id','destination').text == 'Destination page'
    assert len(actions) == 1 and actions[0]['window_status'] == 'switched'
    assert len(events) == 3 and events[-1]['after_window'] == 'destination'


@pytest.mark.parametrize('mode,error,status', [
    ('cross',PermissionError,'rejected_origin'), ('multiple',RuntimeError,'ambiguous'),
    ('blocked',TimeoutException,'timeout'), ('stays_blank',TimeoutException,'timeout'),
    ('source_closed',RuntimeError,'source_closed')])
def test_unsafe_or_unresolved_windows_are_errors_not_old_page_complete(monkeypatch,mode,error,status):
    clock = Clock()
    monkeypatch.setattr(checked, 'time', clock)
    driver, actions = Driver(mode, clock), []
    with pytest.raises(error):
        checked.execute_source(CLICK, driver, BASE, actions=actions)
    assert actions[0]['status'] == 'error' and actions[0]['window_status'] == status
    if mode != 'source_closed':
        assert driver.current_window_handle == 'source'
    assert (clock.now >= checked.Browser.WINDOW_WAIT_SECONDS) == (mode in {'blocked','stays_blank'})


@pytest.mark.parametrize('mode',['same_url','closed'])
def test_window_identity_counts_as_navigation_even_if_url_is_unchanged(monkeypatch,mode):
    clock = Clock()
    monkeypatch.setattr(checked,'time',clock)
    driver,actions = Driver(mode,clock),[]
    checked.execute_source(CLICK,driver,BASE,actions=actions)
    assert driver.current_window_handle == 'destination'
    assert actions[0]['navigation_type'] == 'new_window' and actions[0]['navigated']
    assert actions[0]['window_status'] == 'switched'
    if mode == 'same_url':
        assert actions[0]['before_url'] == actions[0]['after_url']


def test_dom_summary_preserves_link_window_target_without_extra_driver_calls():
    from web_agent.dom_inspector import compact_dom
    view = compact_dom('<a href="/detail" target="_blank">Destination</a>')
    assert view['elements'][0]['target'] == '_blank'
    assert 'target="_blank"' in view['html']


def test_worker_logs_switch_and_captures_evidence_from_new_window(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(checked,'time',clock)
    driver = Driver('new',clock)
    monkeypatch.setattr(checked,'collect_measurements',
        lambda d,b,q,**kw: checked_collect(d,b,q))
    result = checked_execution(driver,BASE,dict(source=CLICK,queries=QUERY,step_id='S1'),
        capture=lambda **kw:driver.view(),save_screenshot=lambda name:True)
    assert result['status'] == 'COMPLETE'
    assert result['before_window'] == 'source' and result['after_window'] == 'destination'
    assert result['window_changed'] and result['active_window_changed']
    assert result['execution']['current_state']['current_window'] == 'destination'
    assert result['measurements']['O1']['value'] == 'Destination page'
    assert result['measurements']['O1']['screenshot_ref'] == 'snapshot.png'


checked_collect = checked.collect_measurements


PAGE = """<!doctype html><title>Window navigation fixture</title>
<button id="ordinary" onclick="this.textContent='Clicked'">Click</button>
<a id="same" href="/detail">Same tab</a>
<a id="new" target="_blank" href="/detail"><span id="child">New tab</span></a>
<a id="delayed" target="_blank" href="/detail"
 onclick="event.preventDefault(); setTimeout(()=>window.open('/detail','_blank'),180)">Delayed</a>
<button id="blank" onclick="const w=window.open('about:blank','_blank');
 setTimeout(()=>w.location='/detail',180)">Blank then navigate</button>
<button id="js_delayed"
 onclick="setTimeout(()=>window.open('/detail','_blank'),180)">Delayed JS</button>"""
DETAIL = """<!doctype html><title>Destination</title><h1 id="destination">Destination page</h1>
<button id="confirm" onclick="document.querySelector('#result').textContent='Confirmed'">Confirm</button>
<p id="result">Initial</p>"""
LARGE = '<!doctype html><title>Large body</title><article id="body"><p>First fact. Second fact.</p>' + (
    '<p>Long synthetic paragraph for duplicate text evidence. ' + 'content ' * 30 + '</p>') * 400 + '</article>'


@contextmanager
def window_fixture():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            content = (LARGE if self.path.startswith('/large') else
                       DETAIL if self.path.startswith('/detail') else PAGE).encode()
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length',str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        def log_message(self,*args): pass
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}/'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.skipif(os.getenv('WEBAGENT_RUN_BROWSER_TESTS')!='1',reason='Opt-in real Chrome required')
def test_real_chrome_trial_cold_and_formal_follow_same_frozen_code():
    from web_agent.selenium_runtime import create_session
    from web_agent.agents.executor import Agent4TestExecutor
    from web_agent.agents.verifier import Agent5EvidenceVerifier
    root = Path(os.getenv('WEBAGENT_TEST_ARTIFACT_ROOT',
        str(Path(__file__).resolve().parents[1]/'artifacts')))/('window_navigation_'+uuid.uuid4().hex[:8])
    root.mkdir(parents=True)
    steps, bindings, oracles = [], {}, []
    modes = ('same','new','delayed','blank','js_delayed','child','enter_same','enter_new','enter_delayed')
    for i,mode in enumerate(modes,1):
        sid,oid = f'S{i}',f'O{i}'
        control = mode.removeprefix('enter_')
        activation = 'send_keys(Keys.ENTER)' if mode.startswith('enter_') else 'click()'
        source = ('driver.get(BASE_URL)\ndriver.find_element(By.ID,"ordinary").click()\n'
            f'driver.find_element(By.ID,"{control}").{activation}\n'
            'WebDriverWait(driver,2).until(EC.element_to_be_clickable((By.ID,"confirm")))\n'
            'driver.find_element(By.ID,"confirm").click()')
        steps.append(dict(id=sid,source=source))
        bindings[oid] = dict(by='id',selector='result',read='text',step_id=sid)
        oracles.append(dict(id=oid,step_id=sid,kind='equals',expected='Confirmed',meaning='Destination control worked'))
    source = ''.join('# webagent-step: '+s['id']+'\n'+s['source']+'\n\n' for s in steps)
    script = root/'generated_test.py'
    script.write_text(source,encoding='utf-8',newline='\n')
    digest = hashlib.sha256(source.encode()).hexdigest()
    sessions,results = [], {}
    try:
        with window_fixture() as url:
            for kind,purpose in [('development','trial'),('cold','trial'),('formal','formal')]:
                session = create_session(root,purpose=purpose,start_url=url,research_mode=True)
                sessions.append(session)
                if kind == 'development':
                    records = [session.execute_checked(s['source'],step_id=s['id'],
                        queries={k:v for k,v in bindings.items() if v['step_id']==s['id']}) for s in steps]
                    result = dict(steps=records,execution_status='COMPLETE' if all(
                        r['status']=='COMPLETE' for r in records) else 'ERROR')
                elif kind == 'cold':
                    result = session.execute_program(source,step_ids=[s['id'] for s in steps],bindings=bindings)
                else:
                    result = Agent4TestExecutor().execute_dynamic(dict(script_path=str(script),script_sha256=digest,
                        cold_trial_complete=results['cold']['execution_status']=='COMPLETE',
                        step_ids=[s['id'] for s in steps],bindings=bindings),session=session)
                results[kind] = result
                assert result['execution_status'] == 'COMPLETE', result
                for mode,record in zip(modes,result['steps']):
                    assert record['measurements'] and all(v['value']=='Confirmed' for v in record['measurements'].values())
                    activation = record['actions'][2]  # get, ordinary click, link activation, confirm
                    same = mode in {'same','enter_same'}
                    assert activation['navigation_type'] == ('same_window' if same else 'new_window')
                    assert activation['window_status'] == ('no_new_window' if same else 'switched')
                    assert record['active_window_changed'] == (not same)
                    snapshot = json.loads((session.run_dir/record['observation_ref']).read_text())
                    assert snapshot['current_window'] == record['after_window']
                    assert '/detail' in snapshot['url'] and 'id="result"' in (session.run_dir/snapshot['html_ref']).read_text()
                    for m in record['measurements'].values():
                        assert (session.run_dir/m['screenshot_ref']).is_file()
                assert all(v=='COMPLETE' for r in result['steps'] for v in r['phase_status'].values())
                assert session.close_session()['process_tree_terminated']
            assert results['cold']['script_sha256'] == results['formal']['script_sha256'] == digest
            verdict = Agent5EvidenceVerifier().verify_dynamic({'oracles':oracles},results['formal']).to_dict()
            results['verdict'] = verdict
            assert verdict['status'] == 'PASS'
            assert len({s.metadata['browser_session_id'] for s in sessions}) == 3
            assert len({s.metadata['profile_id'] for s in sessions}) == 3
    finally:
        results['cleanup'] = [s.close_session() for s in sessions]
        (root/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    assert all(c['process_tree_terminated'] and c['profile_removed'] and
               c['owned_processes_remaining']==0 for c in results['cleanup'])


@pytest.mark.skipif(os.getenv('WEBAGENT_RUN_BROWSER_TESTS')!='1',reason='Opt-in real Chrome required')
def test_real_chrome_large_body_is_read_once_for_two_independent_oracles():
    from web_agent.selenium_runtime import create_session
    from web_agent.agents.executor import Agent4TestExecutor
    from web_agent.agents.verifier import Agent5EvidenceVerifier
    root = Path(os.getenv('WEBAGENT_TEST_ARTIFACT_ROOT',
        str(Path(__file__).resolve().parents[1]/'artifacts')))/('body_dedup_'+uuid.uuid4().hex[:8])
    root.mkdir(parents=True)
    source = '# webagent-step: S1\ndriver.title\n'
    script = root/'generated_test.py'
    script.write_text(source,encoding='utf-8',newline='\n')
    digest = hashlib.sha256(source.encode()).hexdigest()
    bindings = {k:dict(by='css',selector='#body',read='text',step_id='S1') for k in ('O1','O2')}
    contract = {'oracles':[dict(id=k,step_id='S1',kind='contains',expected=fact,meaning=fact)
        for k,fact in [('O1','First fact'),('O2','Second fact')]]}
    sessions,results = [],{}
    try:
        with window_fixture() as url:
            for purpose in ('trial','formal'):
                session = create_session(root,purpose=purpose,start_url=url+'large',research_mode=True)
                sessions.append(session)
                result = (session.execute_program(source,step_ids=['S1'],bindings=bindings) if purpose=='trial' else
                    Agent4TestExecutor().execute_dynamic(dict(script_path=str(script),script_sha256=digest,
                        cold_trial_complete=results['trial']['execution_status']=='COMPLETE',
                        step_ids=['S1'],bindings=bindings),session=session))
                results[purpose] = result
                assert result['execution_status'] == 'COMPLETE',result
                values = result['measurements']
                assert values['O1'] is not values['O2']
                assert values['O1']['value'] == values['O2']['value']
                assert result['steps'][0]['timings']['oracle_capture'] < 5
                assert session.close_session()['process_tree_terminated']
                log = (session.run_dir/'chromedriver.log').read_text(encoding='utf-8')
                reads = log.count('COMMAND GetElementText')
                results[purpose]['body_text_commands'] = reads
                assert reads == 0  # Worker uses batched rendered DOM text; no native recursion.
                captures = [r for r in result['steps'][0]['oracle_log']
                            if r['operation']=='rendered_text' and r['event']=='complete']
                assert len(captures) == 1 and captures[0]['oracle_ids'] == ['O1','O2']
                for measurement in values.values():
                    assert (session.run_dir/measurement['observation_ref']).is_file()
                    assert (session.run_dir/measurement['screenshot_ref']).is_file()
            verdict = Agent5EvidenceVerifier().verify_dynamic(contract,results['formal']).to_dict()
            results['verdict'] = verdict
            assert verdict['status'] == 'PASS' and len(verdict['checks']) == 2
    finally:
        results['cleanup'] = [s.close_session() for s in sessions]
        (root/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
