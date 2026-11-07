"""Offline regression for dynamic control, restricted Python and evidence semantics."""
import hashlib
import json
import os
from pathlib import Path
import uuid
from types import SimpleNamespace

import pytest

from web_agent.checked_selenium import (execute_source, validate_source, split_program,
                                        normalize_bindings, collect_measurements)
from web_agent.dom_inspector import compact_dom
from web_agent.agents.executor import Agent4TestExecutor
from web_agent.agents.verifier import Agent5EvidenceVerifier


@pytest.mark.parametrize('source', [
    'import os', 'driver.execute_script("1")', 'open("private")', 'driver.__class__',
    'driver.service.process.kill()', 'eval("1")', 'while True: pass',
    'driver=1', 'x=driver\nx.title="fake"', 'driver.get(**{})',
])
def test_generated_privilege_paths_are_rejected(source):
    with pytest.raises((ValueError,SyntaxError)):
        validate_source(source)


def test_valid_python_subset_and_program_boundaries():
    code = 'field = driver.find_element(By.CSS_SELECTOR, "input")\nfield.send_keys("synthetic", Keys.ENTER)'
    validate_source(code)
    assert split_program('# webagent-step: S1\n'+code)[0]['source']==code
    with pytest.raises(ValueError):
        split_program('# webagent-step: S1\ndriver.title\n# webagent-step: S1\ndriver.title')


def test_interpreter_cannot_navigate_outside_authorized_origin():
    driver = SimpleNamespace(get=lambda url: pytest.fail('Unauthorized navigation executed'))
    with pytest.raises(PermissionError):
        execute_source('driver.get("https://other.test/")', driver, 'https://authorized.test/')


def test_interpreter_prohibits_string_reflection_and_subscripts():
    for code in ['x="abc"\nx.title', 'x="abc"\nx[0]']:
        with pytest.raises((ValueError,PermissionError)):
            execute_source(code,SimpleNamespace(),'https://authorized.test/')


def test_observation_preserves_class_and_safe_spa_routes():
    snapshot=compact_dom('<input class="new-todo input"><a href="#/active">Active</a><a href="#token=private">bad</a>')
    assert 'class="new-todo input"' in snapshot['html']
    assert 'href="#/active"' in snapshot['html']
    assert 'private' not in snapshot['html']


def test_formal_executor_rejects_changed_source_before_browser(tmp_path):
    p=tmp_path/'program.py'
    p.write_text('driver.title',encoding='utf-8')
    delivery=dict(script_path=str(p),script_sha256=hashlib.sha256(b'original').hexdigest())
    with pytest.raises(ValueError,match='hash changed'):
        Agent4TestExecutor().execute_dynamic(delivery,session=SimpleNamespace(purpose='formal'))


def test_business_mismatch_is_fail_and_missing_evidence_is_inconclusive():
    contract={'oracles':[dict(id='O1',kind='equals',expected=['unfinished'],meaning='visible list')]}
    evidence=dict(execution_status='COMPLETE',measurements={'O1':dict(
        value=['wrong'],observation_ref='actual.json',screenshot_ref='actual.png')})
    verifier=Agent5EvidenceVerifier()
    assert verifier.verify_dynamic(contract,evidence).status=='FAIL'
    evidence['measurements']['O1']['value']=None
    assert verifier.verify_dynamic(contract,evidence).status=='INCONCLUSIVE'
    evidence['execution_status']='ERROR'
    assert verifier.verify_dynamic(contract,evidence).status=='ERROR'


def test_empty_oracles_never_pass():
    assert Agent5EvidenceVerifier().verify_dynamic({'oracles':[]},{'execution_status':'COMPLETE'}).status=='INCONCLUSIVE'


class NativeElement:
    """Offline native-element double. Live coverage below uses actual Chrome."""
    def __init__(self, text='', *, selected=False, visible=True):
        self.text, self.selected, self.visible = text, selected, visible
        self.inputs = []

    def get_attribute(self, name):
        return {'type':'checkbox', 'class':'toggle', 'data-testid':'choice'}.get(name)

    def is_selected(self):
        return self.selected

    def is_displayed(self):
        return self.visible

    def is_enabled(self):
        return True

    def send_keys(self, *values):
        self.inputs.append(values)


def test_len_comparisons_lists_and_native_states_execute():
    node = NativeElement(' Read book ', selected=True)
    other = NativeElement('Do Homeworks')
    driver = SimpleNamespace(find_elements=lambda *args: [node, other], find_element=lambda *args: node)
    execute_source('''nodes = driver.find_elements(By.CSS_SELECTOR, "input")
texts = [e.text.strip() for e in nodes if e.is_displayed() and e.is_enabled()]
states = []
for e in nodes:
    states.append(e.is_selected())
if len(texts) == 2 and states[0] and not states[1]:
    nodes[0].send_keys(texts[0], len(nodes), nodes[0].get_attribute("class"))
''', driver, 'https://authorized.test/')
    assert node.inputs == [('Read book', 2, 'toggle')]


@pytest.mark.parametrize('code', [
    'len=1', 'for driver in []:\n    driver.title', '[x for x in [] for y in []]',
    'items=[1]\nitems.__iter__()', 'items=[1]\nitems.append(open("secret"))',
    'for x in []:\n    x=driver.__class__', 'data=[x for x in [1]]\nx',
])
def test_extended_subset_keeps_privilege_and_scope_guards(code):
    with pytest.raises((ValueError, SyntaxError)):
        validate_source(code)


def test_iteration_and_list_growth_remain_bounded():
    source = 'items=[1,2]\nfor a in items:\n    for b in items:\n        for c in items:\n            for d in items:\n                for e in items:\n                    for f in items:\n                        for g in items:\n                            for h in items:\n                                for i in items:\n                                    len(items)'
    with pytest.raises(ValueError, match='operation limit'):
        execute_source(source.replace('items=[1,2]', 'items=[1,2,3]'), SimpleNamespace(), 'https://authorized.test/')
    with pytest.raises(ValueError, match='200 items'):
        execute_source('items=[1,2]\nfor a in items:\n    for b in items:\n        for c in items:\n            for d in items:\n                for e in items:\n                    for f in items:\n                        for g in items:\n                            for h in items:\n                                items.append(1)', SimpleNamespace(), 'https://authorized.test/')


@pytest.mark.parametrize('by,native', [('css','css selector'), ('xpath','xpath'), ('id','id'), ('name','name')])
def test_evidence_uses_same_locator_types_as_operations(by, native):
    calls = []
    def find(by, selector):
        calls.append((by, selector))
        return [NativeElement(selected=True)]
    binding = {'O3':dict(by=by, selector='actual-locator', read='selected', step_id='S6')}
    result = collect_measurements(SimpleNamespace(find_elements=find), 'https://authorized.test/', binding)
    assert calls == [(native, 'actual-locator')]
    assert result['O3']['value'] is True and result['O3']['status'] == 'observed'


def test_evidence_distinguishes_unselected_hidden_missing_and_ambiguous():
    nodes = {'unchecked':[NativeElement()], 'hidden':[NativeElement(visible=False,selected=True)],
             'missing':[], 'ambiguous':[NativeElement(), NativeElement()]}
    driver = SimpleNamespace(find_elements=lambda by, selector: nodes[selector])
    queries = {key:dict(selector=key, read='selected', step_id='S6') for key in nodes}
    result = collect_measurements(driver, 'https://authorized.test/', queries)
    assert result['unchecked']['value'] is False and result['unchecked']['matched']==1
    assert result['hidden']['value'] is True and result['hidden']['visible']==0
    assert result['missing']['value'] is None and result['missing']['status']=='missing'
    assert result['ambiguous']['value'] is None and result['ambiguous']['status']=='ambiguous'
    result = collect_measurements(driver, 'https://authorized.test/', {
        'O1':dict(selector='missing',read='visible',step_id='S6'),
        'O2':dict(selector='hidden',read='visible',step_id='S6')})
    assert result['O1']['value'] is None and result['O2']['value'] is False


def test_legacy_css_counts_texts_and_attribute_reads():
    driver = SimpleNamespace(find_elements=lambda *args: [NativeElement('label')])
    queries = {kind:dict(selector='.toggle',read=kind,step_id='S1') for kind in ('count','texts','text','enabled')}
    queries['attr'] = dict(selector='.toggle',read='attribute',attribute='data-testid',step_id='S1')
    result = collect_measurements(driver, 'https://authorized.test/', queries)
    assert [result[k]['value'] for k in queries] == [1,['label'],'label',True,'choice']
    assert all(m['by']=='css' for m in result.values())
    with pytest.raises(ValueError, match='More than 200'):
        collect_measurements(SimpleNamespace(find_elements=lambda *a: [NativeElement()]*201),
                             'https://authorized.test/', {'O1':queries['count']})


@pytest.mark.parametrize('patch,detail', [
    ({'xpath':'//input'}, 'by="xpath"'), ({'selector':'CSS'}, 'actual locator'),
    ({'read':'is_selected'}, 'selected'), ({'by':'tag'}, 'css, xpath, id, name'),
    ({'read':'attribute','attribute':'innerHTML'}, 'allowed attribute'),
    ({'step_id':'S9'}, 'current step S6'),
])
def test_binding_errors_identify_step_oracle_field_and_correction(patch, detail):
    binding = dict(selector='#item',read='selected',step_id='S6', **{})
    binding.update(patch)
    with pytest.raises(ValueError) as error:
        normalize_bindings({'O3':binding},step_id='S6')
    assert 'Step S6 Oracle O3' in str(error.value) and detail in str(error.value)


def test_runtime_rejects_bad_binding_before_actions(tmp_path):
    from tests.test_phase1_infrastructure import bare_session
    session = bare_session(tmp_path)
    session.research_mode = True
    result = session.execute_checked('driver.title',step_id='S6',queries={
        'O3':dict(selector='XPATH',xpath='//input',read='selected',step_id='S6')})
    assert result['status']=='REJECTED'
    assert 'Step S6 Oracle O3' in result['error'] and 'by="xpath"' in result['error']


def test_dom_structured_and_html_context_are_consistent():
    view = compact_dom('''<ul><li class="completed" data-testid="item"><div class="view">
      <input class="toggle" data-testid="toggle" type="checkbox" checked>
      <label data-testid="title">Read book</label></div></li></ul>
      <a class="selected" href="#/active">Active</a>''')
    checkbox, link = view['elements']
    assert checkbox['class']=='toggle' and checkbox['data-testid']=='toggle'
    assert checkbox['parent']['class']=='completed' and checkbox['parent']['text']=='Read book'
    assert checkbox['checked'] and checkbox['label']=='Read book'
    assert checkbox['container']['class']=='view'
    assert link['href_path']=='#/active' and 'href="#/active"' in view['html']


def test_verifier_keeps_binding_identity_and_unknown_state():
    contract = {'oracles':[dict(id='O3',step_id='S6',kind='equals',expected=False,meaning='Do Homeworks unchecked')]}
    measurement = dict(value=False,status='observed',matched=1,by='xpath',selector='//li[2]/input',
                       read='selected',step_id='S6',observation_ref='actual.json',screenshot_ref='actual.png')
    evidence = dict(execution_status='COMPLETE',measurements={'O3':measurement})
    verdict = Agent5EvidenceVerifier().verify_dynamic(contract,evidence)
    assert verdict.status=='PASS' and verdict.checks[0]['binding']['selector']=='//li[2]/input'
    measurement.update(value=None,status='missing',matched=0)
    assert Agent5EvidenceVerifier().verify_dynamic(contract,evidence).status=='INCONCLUSIVE'
    measurement.update(value=False,status='observed',step_id='S5')
    assert Agent5EvidenceVerifier().verify_dynamic(contract,evidence).status=='INCONCLUSIVE'


def test_pipeline_repair_receives_rejected_code_and_binding(tmp_path, monkeypatch):
    """Offline control regression, not proof of real autonomous LLM repair."""
    from web_agent import selenium_runtime as runtime
    from web_agent.pipeline import run_pipeline
    model_calls = []
    class FakeLLM:
        calls = []
        def complete_json(self, system, user):
            data = json.loads(user)
            if 'Agent1' in system:
                return dict(steps=[dict(id='S1',goal='Read the choice')],
                    oracles=[dict(id='O1',step_id='S1',meaning='choice state',kind='equals',expected=False)])
            if 'Agent2' in system:
                return dict(approved=True,issues=[])
            model_calls.append(data)
            if len(model_calls)==1:
                return dict(code='driver.title',bindings={'O1':dict(selector='XPATH',xpath='//input',read='selected',step_id='S1')})
            assert data['previous_code']=='driver.title'
            assert data['previous_bindings']['O1']['xpath']=='//input'
            assert 'Step S1 Oracle O1' in data['actual_error']['error']
            assert 'by="xpath"' in data['actual_error']['error']
            return dict(code='driver.title',bindings={'O1':dict(by='xpath',selector='//input',read='selected',step_id='S1')})
    class FakeSession:
        def __init__(self, purpose):
            self.purpose = purpose
            self.run_dir = tmp_path
        def observe(self, **kw):
            return {}
        def execute_checked(self, source, **kw):
            return dict(status='COMPLETE')
        def execute_program(self, source, **kw):
            return dict(execution_status='COMPLETE',browser_session_id=self.purpose,
                measurements={'O1':dict(value=False,step_id='S1',status='observed',
                                        observation_ref='test.json',screenshot_ref='test.png')})
        def close_session(self):
            return dict(process_tree_terminated=True,profile_removed=True)
    monkeypatch.setattr(runtime,'ARTIFACT_ROOT',tmp_path)
    monkeypatch.setattr(runtime,'create_session',lambda *a,purpose,**kw: FakeSession(purpose))
    result = run_pipeline(target='dynamic',mode='llm',requirement='Read the choice',start_url='https://authorized.test/',
                          allow_live_site=True,allow_local_research=True,output_root=tmp_path,llm=FakeLLM())
    assert result['verdict']['status']=='PASS' and len(model_calls)==2
    debug = json.loads((Path(result['run_dir'])/'agent3_debug.json').read_text())
    assert [entry['result']['status'] for entry in debug] == ['REJECTED','COMPLETE']


@pytest.mark.skipif(os.getenv('WEBAGENT_RUN_BROWSER_TESTS') != '1',reason='Opt-in real Chrome required')
def test_real_chrome_xpath_states_len_and_independent_replay():
    """Fixed reviewed test on the existing fixture, not LLM-generated acceptance."""
    from web_agent.fixture_server import local_fixture
    from web_agent.selenium_runtime import create_session
    root = Path(os.getenv('WEBAGENT_TEST_ARTIFACT_ROOT', str(Path(__file__).resolve().parents[1]/'artifacts')))
    root = root/('xpath_live_'+uuid.uuid4().hex[:8])
    root.mkdir(parents=True)
    code = '''nodes = driver.find_elements(By.NAME, "classification-computer_science")
if len(nodes) == 1 and not nodes[0].is_selected():
    nodes[0].click()
names = [e.get_attribute("id") for e in nodes if e.is_enabled()]
driver.find_element(By.ID, "terms-0-term").send_keys(len(names))
driver.find_element(By.XPATH, "//select[@id='terms-0-field']/option[normalize-space(.)='Title']").click()
driver.find_element(By.XPATH, "//select[@id='terms-0-field']/option[normalize-space(.)='All fields']").click()'''
    queries = {
        'O1':dict(by='xpath',selector="//label[contains(.,'Computer Science')]/input",read='selected',step_id='S1'),
        'O2':dict(by='id',selector='terms-0-term',read='attribute',attribute='value',step_id='S1'),
        'O3':dict(by='name',selector='classification-computer_science',read='count',step_id='S1'),
        'O4':dict(selector='#terms-0-field',read='enabled',step_id='S1'),
        'O5':dict(by='xpath',selector='//input[@id="missing"]',read='selected',step_id='S1'),
        'O6':dict(selector='#terms-0-field',read='visible_exists',step_id='S1'),
        'O7':dict(selector='#missing',read='exists',step_id='S1'),
        'O8':dict(selector='#terms-0-field option:checked',read='text',step_id='S1'),
        'O9':dict(by='id',selector='terms-0-field',read='selected_text',step_id='S1'),
        'O10':dict(selector='#terms-0-field',read='attribute',attribute='value',step_id='S1'),
    }
    sessions, results, cleanup = [], [], []
    try:
        with local_fixture() as (url, _):
            for purpose in ('trial','formal'):
                session = create_session(root,purpose=purpose,start_url=url,research_mode=True)
                sessions.append(session)
                initial = session.execute_checked('driver.title',step_id='S1',queries={'O1':queries['O1']})
                assert initial['measurements']['O1']['value'] is False
                result = session.execute_checked(code,step_id='S1',queries=queries)
                results.append(result)
                assert result['status']=='COMPLETE', result
                assert [result['measurements'][k]['value'] for k in list(queries)[:5]] == [True,'1',1,True,None]
                assert result['measurements']['O5']['status']=='missing'
                assert [result['measurements'][k]['value'] for k in ('O6','O7','O8','O9','O10')]==[True,False,'All fields','All fields','all']
                assert [a['action'] for a in result['actions']]==['click','send_keys','click','click']
                assert (session.run_dir/'browser_actions.jsonl').exists()
                contract={'oracles':[dict(id='O3',step_id='S1',kind='gte',expected=1,meaning='at least one'),
                    dict(id='O6',step_id='S1',kind='equals',expected=True,meaning='visible select exists'),
                    dict(id='O7',step_id='S1',kind='equals',expected=False,meaning='missing node'),
                    dict(id='O8',step_id='S1',kind='equals',expected='All fields',meaning='requested selection'),
                    dict(id='O9',step_id='S1',kind='equals',expected='All fields',meaning='selected display label')]}
                assert Agent5EvidenceVerifier().verify_dynamic(contract,dict(execution_status='COMPLETE',
                    measurements=result['measurements'])).status=='PASS'
                cleanup.append(session.close_session())
        assert sessions[0].metadata['browser_session_id'] != sessions[1].metadata['browser_session_id']
        assert sessions[0].run_dir != sessions[1].run_dir
        assert all(c['process_tree_terminated'] and c['profile_removed'] for c in cleanup)
    finally:
        cleanup = [s.close_session() for s in sessions]
        (root/'results.json').write_text(json.dumps(dict(results=results,cleanup=cleanup),indent=2),encoding='utf-8')
