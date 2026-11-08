"""Phase-aware repairs: offline control regressions and opt-in REAL local Chrome."""
import hashlib
import json
import os
from pathlib import Path

import pytest
from selenium.common.exceptions import NoSuchElementException, TimeoutException, InvalidSelectorException

from web_agent.checked_selenium import (execute_source, collect_measurements, execution_diagnostics,
                                        step_repair_context, split_program)
from web_agent.agents.generator import Agent3SeleniumGenerator

BASE = 'https://authorized.test/'
WAIT_BAD = 'WebDriverWait(driver, .1).until(EC.presence_of_element_located((By.ID, "missing")))'
WAIT_GOOD = 'WebDriverWait(driver, .1).until(EC.presence_of_element_located((By.ID, "item")))'
PREFIX = ('submit = WebDriverWait(driver, .1).until(EC.element_to_be_clickable((By.ID, "submit")))\n'
          'submit.click()\n')
SUFFIX = '\nitems = driver.find_elements(By.ID, "item")'
CODE = PREFIX + WAIT_BAD + SUFFIX
S1 = 'driver.find_element(By.ID, "next").click()\ndriver.find_element(By.ID, "name").send_keys("synthetic")'
QUERY = {'O1': dict(by='id', selector='item', read='count', step_id='S2')}


class Node:
    tag_name, text = 'button', 'synthetic result'
    def __init__(self, driver, selector): self.driver, self.selector = driver, selector
    def get_attribute(self, name): return ''
    def is_displayed(self): return True
    def is_enabled(self): return True
    def click(self):
        if self.selector == 'broken': raise RuntimeError('synthetic click failure')
        if self.selector == 'go': pytest.fail('Result-page Go was executed as an entry-page action')
        self.driver.page = 'form' if self.selector == 'next' else 'results'
        self.driver.current_url = BASE + self.driver.page
    def send_keys(self, *values): self.driver.value = ''.join(str(v) for v in values)


class Driver:
    title = 'Synthetic phase fixture'
    def __init__(self, page='form', allow_bad_wait=False):
        self.page, self.value, self.current_url = page, '', BASE + page
        self.allow_bad_wait = allow_bad_wait
    def find_element(self, by, selector):
        nodes = self.find_elements(by, selector)
        if not nodes: raise NoSuchElementException(selector)
        return nodes[0]
    def find_elements(self, by, selector):
        if selector == 'invalid': raise InvalidSelectorException('synthetic invalid locator')
        allowed = {'start': {'next'}, 'form': {'name', 'submit', 'broken'}, 'results': {'item', 'go'}}[self.page]
        if selector in allowed or (selector == 'missing' and self.allow_bad_wait):
            return [Node(self, selector)]
        return []
    def view(self):
        return dict(url=self.current_url, state_hash=self.page + ':' + self.value,
                    observation_ref=self.page + '.json', elements=[dict(id=self.page)])


def run_checked(driver, source, queries=None, step_id='S2'):
    """Offline interpreter execution; intentionally NOT browser acceptance."""
    progress, actions, entry = {}, [], driver.view()
    result, stage = dict(status='ERROR', measurements={}), 'source'
    try:
        execute_source(source,driver,BASE,actions=actions,progress=progress)
        stage = 'evidence'
        result['measurements'] = collect_measurements(driver,BASE,queries or {},step_id=step_id)
        stage, result['status'] = 'complete', 'COMPLETE'
    except Exception as exc:
        result.update(error_type=type(exc).__name__,error=str(exc).splitlines()[0])
    result.update(actions=actions,current_url=driver.current_url,step_id=step_id,
        execution=execution_diagnostics(progress,actions,stage=stage,entry=entry,current=driver.view()))
    return result


@pytest.mark.parametrize('source,phase,operation,completed', [
    (WAIT_BAD, 'before_action', 'wait', 0),
    ('driver.find_element(By.ID,"broken").click()', 'during_action', 'action', 0),
    (CODE, 'after_action', 'wait', 1),
])
def test_failure_stage_is_based_on_actual_execution(source,phase,operation,completed):
    result = run_checked(Driver(),source)
    assert result['status'] == 'ERROR'
    execution = result['execution']
    assert (execution['phase'],execution['failed_operation'],execution['successful_actions']) == (phase,operation,completed)
    assert execution['failed_statement']['start_line'] == (3 if source == CODE else 1)
    if source == CODE:
        assert execution['state_changed'] and execution['url_changed']
        assert result['actions'][0]['status'] == 'complete' and result['actions'][0]['start_line'] == 2


def test_wait_patch_preserves_successful_prefix_and_unexecuted_suffix(tmp_path):
    result = run_checked(Driver(),CODE)
    context = step_repair_context(CODE,result)
    assert context['scope'] == 'wait' and context['locked_prefix'] == PREFIX
    assert context['locked_suffix'] == SUFFIX
    class Model:
        def complete_json(self,system,user):
            data = json.loads(user)
            assert data['untrusted_entry_page']['url'] == BASE+'form'
            assert data['untrusted_failure_page']['url'] == BASE+'results'
            assert data['execution_context']['phase'] == 'after_action'
            assert data['previous_code'] == CODE and 'LOCAL WAIT REPAIR' in system
            return dict(code=WAIT_GOOD,bindings={})
    fixed = Agent3SeleniumGenerator().develop_step({'oracles':[]},{'id':'S2'},{'url':BASE+'form'},
        llm=Model(),previous=dict(code=CODE,bindings={}),error=result,
        entry_observation={'url':BASE+'form'},failure_observation={'url':BASE+'results'},repair_context=context)
    assert fixed['code'] == PREFIX+WAIT_GOOD+SUFFIX
    # Works from the step entry. It MUST fail if incorrectly run from the result page.
    assert run_checked(Driver(),fixed['code'])['status'] == 'COMPLETE'
    assert run_checked(Driver('results'),fixed['code'])['status'] == 'ERROR'


@pytest.mark.parametrize('patch',[
    'driver.find_element(By.ID,"go").click()\n'+WAIT_GOOD,
    'again()\n'+WAIT_GOOD,
    'driver.title',
])
def test_wait_repair_cannot_replace_a_successful_action_or_drop_wait(patch):
    class Model:
        def complete_json(self,*args): return dict(code=patch,bindings={})
    with pytest.raises(ValueError,match='Wait-only repair'):
        Agent3SeleniumGenerator().develop_step({'oracles':[]},{'id':'S2'},{},llm=Model(),
            previous=dict(code=CODE,bindings={}),repair_context=step_repair_context(CODE,run_checked(Driver(),CODE)))


def test_statement_boundaries_handle_unicode_and_multiple_statements_on_one_line():
    code = 'label="验证";driver.find_element(By.ID,"submit").click();'+WAIT_BAD+SUFFIX
    context = step_repair_context(code,run_checked(Driver(),code))
    assert context['scope'] == 'wait' and not context['partial_statement_actions']
    assert context['locked_prefix'].endswith('.click();')
    class Model:
        def complete_json(self,*args): return dict(code=WAIT_GOOD,bindings={})
    fixed = Agent3SeleniumGenerator().develop_step({'oracles':[]},{'id':'S2'},{},llm=Model(),repair_context=context)
    assert fixed['code'] == code.replace(WAIT_BAD,WAIT_GOOD)
    assert run_checked(Driver(),fixed['code'])['status'] == 'COMPLETE'


def test_partially_executed_compound_statement_is_not_resumed_in_the_middle():
    code = 'title=driver.title\nif title:\n    driver.find_element(By.ID,"submit").click()\n    '+WAIT_BAD
    result = run_checked(Driver(),code)
    context = step_repair_context(code,result)
    assert context['partial_statement_actions'] and context['scope'] == 'tail'
    assert context['locked_prefix'] == 'title=driver.title\n'
    assert context['failed_code'].startswith('if title:')


def test_collection_failure_repairs_only_bindings_and_keeps_all_source():
    source = 'driver.find_element(By.ID,"submit").click()'
    result = run_checked(Driver(),source,{'O1':dict(selector='invalid',read='count',step_id='S2')})
    context = step_repair_context(source,result)
    assert context['scope'] == 'bindings' and context['phase'] == 'after_action'
    class Model:
        def complete_json(self,system,user):
            assert 'ONLY evidence bindings' in system
            assert json.loads(user)['previous_code'] == source
            return dict(bindings=QUERY)
    contract = {'oracles':[dict(id='O1',step_id='S2',kind='gte',expected=1)]}
    fixed = Agent3SeleniumGenerator().develop_step(contract,{'id':'S2'},{},llm=Model(),
        previous=dict(code=source,bindings={}),error=result,repair_context=context)
    assert fixed['code'] == source and fixed['bindings'] == QUERY


def run_pipeline_regression(tmp_path,monkeypatch,*,cold_failure=False,reject_patch=False,step_attempts=3,task_repairs=5):
    from web_agent import selenium_runtime as runtime
    from web_agent.pipeline import run_pipeline
    created, executed = [], []
    class Session:
        def __init__(self,purpose):
            self.purpose, self.run_dir = purpose, tmp_path
            self.session_id = str(len(created))
            self.metadata = dict(browser_session_id=self.session_id)
            self.driver = Driver('start',allow_bad_wait=cold_failure and not created)
        def observe(self,**kw): return self.driver.view()
        def close_session(self): return dict(process_tree_terminated=True,profile_removed=True)
        def execute_checked(self,source,*,step_id,queries):
            executed.append((self.session_id,step_id,self.driver.page,source))
            result = run_checked(self.driver,source,queries,step_id)
            for measurement in result['measurements'].values():
                measurement.update(observation_ref='offline.json',screenshot_ref='offline.png')
            # The real worker persists this reference; the offline double does not.
            result['execution']['entry_state'].pop('observation_ref')
            return result
        def execute_program(self,source,*,step_ids,bindings):
            results = []
            for step in split_program(source):
                results.append(self.execute_checked(step['source'],step_id=step['id'],
                    queries={k:v for k,v in bindings.items() if v['step_id']==step['id']}))
                if results[-1]['status'] != 'COMPLETE': break
            return dict(execution_status='COMPLETE' if all(r['status']=='COMPLETE' for r in results) else 'ERROR',
                steps=results,browser_session_id=self.session_id,
                measurements={k:v for r in results for k,v in r['measurements'].items()})
    def create(*args,purpose,**kw):
        s=Session(purpose);created.append(s);return s
    class Model:
        calls = []
        def __init__(self): self.generated=[];self.repairs=0
        def complete_json(self,system,user):
            data=json.loads(user)
            if system.startswith('You are Agent1'):
                return dict(steps=[dict(id='S1',goal='Open form and enter synthetic input'),
                    dict(id='S2',goal='Submit and wait for result')],oracles=[dict(id='O1',step_id='S2',
                    kind='gte',expected=1,meaning='at least one result')])
            if system.startswith('You are Agent2'): return dict(approved=True,issues=[])
            sid=data['current_step']['id'];self.generated.append(sid)
            if sid=='S1': return dict(code=S1,bindings={})
            if not data['execution_context']: return dict(code=CODE,bindings=QUERY)
            self.repairs+=1
            assert data['execution_context']['scope']=='wait'
            assert data['execution_context']['phase']=='after_action'
            assert data['untrusted_page']['url']==data['untrusted_entry_page']['url']==BASE+'form'
            assert data['untrusted_failure_page']['url']==BASE+'results'
            assert data['execution_context']['locked_prefix']==PREFIX
            assert data['execution_context']['current_at_step_entry']
            assert data['execution_context']['entry_validation']=='saved_prefix_replay'
            return dict(code=('driver.find_element(By.ID,"go").click()\n'+WAIT_GOOD
                              if reject_patch and (reject_patch != 'once' or self.repairs == 1) else WAIT_GOOD),bindings=QUERY)
    model=Model()
    monkeypatch.setattr(runtime,'ARTIFACT_ROOT',tmp_path)
    monkeypatch.setattr(runtime,'create_session',create)
    result=run_pipeline(target='dynamic',mode='llm',requirement='Submit synthetic input and check at least one result',
        start_url=BASE,allow_live_site=True,allow_local_research=True,output_root=tmp_path,llm=model,
        step_attempts=step_attempts,task_repairs=task_repairs)
    return result,model,created,executed


@pytest.mark.parametrize('cold_failure',[False,True])
def test_pipeline_restores_entry_preserves_code_and_independently_replays(tmp_path,monkeypatch,cold_failure):
    result,model,created,executed=run_pipeline_regression(tmp_path,monkeypatch,cold_failure=cold_failure)
    assert result['verdict']['status']=='PASS',result
    assert model.generated==['S1','S2','S2'] and model.repairs==1
    assert all(page=='form' for _,sid,page,_ in executed if sid=='S2')
    s2=[source for _,sid,_,source in executed if sid=='S2']
    assert all(source.startswith(PREFIX) for source in s2) and s2[-1]==(PREFIX+WAIT_GOOD+SUFFIX)
    path=Path(result['run_dir'])
    delivery=json.loads((path/'agent3_delivery.json').read_text())
    assert delivery['attempts']=={'S1':1,'S2':2} and delivery['repairs_used']==1
    assert delivery['cold_trial_complete'] and created[-1].purpose=='formal'
    source=(path/'generated_test.py').read_text()
    assert delivery['script_sha256']==hashlib.sha256(source.encode()).hexdigest()
    events=json.loads((path/'events.json').read_text())
    assert any(e['action']=='step_entry_restored' for e in events)
    assert (path/'formal_evidence.json').exists()


def test_bad_regeneration_is_rejected_then_corrected_without_losing_locked_code(tmp_path,monkeypatch):
    result,model,created,executed=run_pipeline_regression(tmp_path,monkeypatch,reject_patch='once')
    assert result['verdict']['status']=='PASS' and model.generated==['S1','S2','S2','S2']
    assert len(created)==4  # development, restored entry, cold trial, independent formal
    assert all('go' not in source for _,_,_,source in executed)
    debug=json.loads((Path(result['run_dir'])/'agent3_debug.json').read_text())
    assert [d['result']['status'] for d in debug if d['step_id']=='S2']==['ERROR','REJECTED','COMPLETE']
    delivery=json.loads((Path(result['run_dir'])/'agent3_delivery.json').read_text())
    assert delivery['attempts']=={'S1':1,'S2':3} and delivery['repairs_used']==2


@pytest.mark.parametrize('attempts,repairs,expected_calls',[(3,5,3),(3,1,2),(1,5,1)])
def test_bad_result_page_repair_stops_with_existing_budgets(tmp_path,monkeypatch,attempts,repairs,expected_calls):
    result,model,created,executed=run_pipeline_regression(tmp_path,monkeypatch,reject_patch=True,
        step_attempts=attempts,task_repairs=repairs)
    assert result['verdict']['status']=='ERROR'
    assert model.generated.count('S2')==expected_calls
    assert all(s.purpose=='trial' for s in created)
    assert len([s for _,sid,_,s in executed if sid=='S2'])==1
    debug=json.loads((Path(result['run_dir'])/'agent3_debug.json').read_text())
    assert [d['result']['status'] for d in debug if d['step_id']=='S2']==['ERROR']+['REJECTED']*(expected_calls-1)


@pytest.mark.skipif(os.getenv('WEBAGENT_RUN_BROWSER_TESTS') != '1',reason='Opt-in real local Chrome required')
def test_real_chrome_navigation_and_spa_wait_repairs(monkeypatch):
    """REAL Chrome + scripted model responses, NOT real-LLM acceptance. No external site."""
    from web_agent.fixture_server import runtime_fixture
    from web_agent.pipeline import run_pipeline
    nav = 'driver.find_element(By.ID,"next").click()\n'
    load = ('driver.find_element(By.ID,"name").send_keys("phase-check")\n'
            'driver.find_element(By.ID,"load").click()\n')
    suffix = '\ndriver.find_element(By.ID,"continue").click()'
    def wait(selector):
        return 'WebDriverWait(driver, .3).until(EC.presence_of_element_located((By.ID,"'+selector+'")))'
    queries = dict(O1=dict(by='id',selector='status',read='text',step_id='S3'),
                   O2=dict(by='id',selector='continue',read='count',step_id='S3'))
    class Model:
        calls=[]
        def complete_json(self,system,user):
            data=json.loads(user)
            if system.startswith('You are Agent1'):
                return dict(steps=[dict(id='S1',goal='Open the form'),
                    dict(id='S2',goal='Enter phase-check, load the dynamic button and continue'),
                    dict(id='S3',goal='Read the resulting state')],oracles=[
                    dict(id='O1',step_id='S3',meaning='continued state',kind='equals',expected='Continued'),
                    dict(id='O2',step_id='S3',meaning='one visible continue button',kind='gte',expected=1)])
            if system.startswith('You are Agent2'): return dict(approved=True,issues=[])
            sid=data['current_step']['id']
            if data['execution_context']:
                context=data['execution_context']
                assert context['phase']=='after_action' and context['scope']=='wait'
                assert context['state_changed'] and context['current_at_step_entry']
                assert context['url_changed']==(sid=='S1')
                return dict(code=wait('name' if sid=='S1' else 'continue'),bindings={})
            return dict(code=nav+wait('missing-form') if sid=='S1' else
                load+wait('missing-continue')+suffix if sid=='S2' else 'driver.title',
                bindings=queries if sid=='S3' else {})
    root=Path(os.getenv('WEBAGENT_TEST_ARTIFACT_ROOT',str(Path(__file__).resolve().parents[1]/'artifacts')))
    with runtime_fixture() as (url,state):
        result=run_pipeline(target='dynamic',mode='llm',start_url=url,
            requirement='Open the form, enter phase-check, load and click Continue; verify Continued and a visible button.',
            allow_live_site=True,allow_local_research=True,output_root=root,llm=Model())
        assert not state['submissions']  # no server writes or backend reset assumptions
    path=Path(result['run_dir'])
    assert result['verdict']['status']=='PASS',path
    debug=json.loads((path/'agent3_debug.json').read_text())
    failures=[d['result'] for d in debug if d['result']['status']=='ERROR']
    assert len(failures)==2 and all(r['execution']['phase']=='after_action' for r in failures)
    assert [r['execution']['url_changed'] for r in failures]==[True,False]
    assert all(r['execution']['state_changed'] for r in failures)
    source=(path/'generated_test.py').read_text()
    assert nav+wait('name') in source and load+wait('continue')+suffix in source
    delivery=json.loads((path/'agent3_delivery.json').read_text())
    evidence=json.loads((path/'formal_evidence.json').read_text())
    assert delivery['cold_trial_complete'] and delivery['attempts']=={'S1':2,'S2':2,'S3':1}
    assert delivery['repairs_used']==2 and delivery['script_sha256']==evidence['script_sha256']
    assert evidence['measurements']['O1']['value']=='Continued'
    assert evidence['measurements']['O2']['value']==1
    cleanups=json.loads((path/'cleanup.json').read_text())
    assert all(c['process_tree_terminated'] and c['profile_removed'] for c in cleanups)
    events=json.loads((path/'events.json').read_text())
    session_ids=[e['browser_session_id'] for e in events if e['action']=='session_created']
    assert len(session_ids)==len(set(session_ids))==5
