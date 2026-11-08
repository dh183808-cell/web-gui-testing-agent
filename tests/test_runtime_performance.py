"""Runtime batching, evidence compatibility, and hard per-phase controller deadlines."""
import copy
import io
import json
import queue
import threading
from types import SimpleNamespace

import pytest

from web_agent import selenium_runtime as runtime
from web_agent.checked_selenium import collect_measurements, rendered_text_script
from web_agent.dom_inspector import observe_session
from web_agent.local_worker import checked_execution


def test_observation_is_three_commands_and_retains_context_and_redaction():
    class Driver:
        calls=[]
        @property
        def current_window_handle(self): self.calls.append('window');return 'w'
        @property
        def window_handles(self): self.calls.append('windows');return ['w']
        def execute_script(self,script,*args):
            self.calls.append('batch')
            assert args[2:]==(80,120)
            return dict(html='''<section class="completed"><label for="item">A task</label>
                <input id="item" class="toggle" data-testid="choice"><a href="#/active">Active</a></section>''',
                live=[dict(tag='input',id='item',name='',visible=False,enabled=True,checked=True,
                           value='token=synthetic',text='A task')],
                url='https://authorized.test/#/active',title='Fixture',frame_context='top',
                ready_state='complete',truncated=False)
    driver=Driver()
    view=observe_session(driver,max_elements=80,max_html=16000)
    assert driver.calls==['batch','window','windows']
    item=view['elements'][0]
    assert item['class']=='toggle' and item['data-testid']=='choice'
    assert item['parent']['class']=='completed' and item['label']=='A task'
    assert view['url'].endswith('#/active') and '#/active' in view['html']
    assert view['live_elements'][0]['checked'] and not view['live_elements'][0]['visible']
    assert 'synthetic' not in json.dumps(view) and view['untrusted_page_data']


def test_count_batch_does_not_read_text_or_per_element_properties_and_reuses_locator():
    class Node:
        def __getattr__(self,name): pytest.fail('Unneeded per-element read: '+name)
    calls=[]
    nodes=[Node() for _ in range(50)]
    driver=SimpleNamespace(
        find_elements=lambda *args: calls.append(('find',args)) or nodes,
        execute_script=lambda script,items: calls.append(('batch',len(items))) or [
            dict(visible=i%2==0,sensitive=False) for i in range(len(items))])
    queries={k:dict(by='css',selector='.item',read=read,step_id='S1')
             for k,read in [('count','count'),('exists','exists'),('visible','visible_exists')]}
    values=collect_measurements(driver,'https://authorized.test/',queries,batch=True)
    assert [values[k]['value'] for k in queries]==[25,True,True]
    assert calls==[('find',('css selector','.item')),('batch',50)]
    assert values['count']['matched']==50 and values['count']['visible']==25


@pytest.mark.parametrize('batch',[False,True])
def test_duplicate_body_measurements_are_read_once_with_independent_oracles(batch):
    reads = []
    text = 'First fact; Second fact. ' + 'synthetic large body ' * 5000
    class Body:
        def get_attribute(self,name): return ''
        def is_displayed(self): return True
        @property
        def text(self):
            reads.append('text')
            if len(reads)>1: pytest.fail('Second large body text read would exhaust the Oracle deadline')
            return text
    def script(source,*args):
        if source == rendered_text_script():
            reads.append('batch_text')
            return [dict(text=text,status='observed',text_complete=True,text_method='rendered_dom')]
        return [dict(visible=True,sensitive=False)]
    driver = SimpleNamespace(find_elements=lambda *a:[Body()],
        execute_script=script)
    bindings = {key:dict(by='css',selector='article',read='text',step_id='S1') for key in ('O1','O2')}
    result = collect_measurements(driver,'https://authorized.test/',bindings,batch=batch)
    assert reads == (['batch_text'] if batch else ['text'])
    assert result['O1']['value'] == result['O2']['value']
    assert result['O1']['value'] == text  # Complete evidence, not an arbitrary 1000-char prefix.
    assert result['O1'] is not result['O2']
    result['O1']['oracle_note'] = 'independent'
    assert 'oracle_note' not in result['O2']
    for item in result.values(): item.update(observation_ref='snapshot.json',screenshot_ref='snapshot.png')
    from web_agent.agents.verifier import Agent5EvidenceVerifier
    contract = {'oracles':[dict(id=key,step_id='S1',kind='contains',expected=expected,meaning=expected)
        for key,expected in [('O1','First fact'),('O2','absent fact')]]}
    verdict = Agent5EvidenceVerifier().verify_dynamic(contract,dict(execution_status='COMPLETE',measurements=result))
    assert verdict.status == 'FAIL'
    assert [c['passed'] for c in verdict.checks] == [True,False]  # Independent conditions, same actual sample.


def test_measurement_cache_is_not_shared_across_steps_reads_attributes_or_collections():
    calls = []
    class Node:
        def get_attribute(self,name): calls.append(name);return name
        def is_displayed(self): return True
        @property
        def text(self): calls.append('text');return 'text sample'
    driver = SimpleNamespace(find_elements=lambda *a:[Node()],
        execute_script=lambda source,*a: (calls.append('text') or [
            dict(text='text sample',status='observed',text_complete=True,text_method='rendered_dom')])
            if source == rendered_text_script() else [dict(visible=True,sensitive=False)])
    queries = {
        'text1':dict(selector='article',read='text',step_id='S1'),
        'text2':dict(selector='article',read='text',step_id='S2'),
        'texts':dict(selector='article',read='texts',step_id='S1'),
        'class':dict(selector='article',read='attribute',attribute='class',step_id='S1'),
        'id':dict(selector='article',read='attribute',attribute='id',step_id='S1')}
    collect_measurements(driver,'https://authorized.test/',queries,batch=True)
    assert calls == ['text','text','text','class','id']
    collect_measurements(driver,'https://authorized.test/',{'again':queries['text1']},batch=True)
    assert calls[-1] == 'text' and calls.count('text') == 4


def test_duplicate_text_lists_do_not_share_mutable_evidence_values():
    driver = SimpleNamespace(find_elements=lambda *a:[SimpleNamespace(text='item')],
        execute_script=lambda source,*a: [dict(text='item',status='observed',text_complete=True)]
            if source == rendered_text_script() else [dict(visible=True,sensitive=False)])
    queries = {k:dict(selector='li',read='texts',step_id='S1') for k in ('O1','O2')}
    result = collect_measurements(driver,'https://authorized.test/',queries,batch=True)
    result['O1']['value'].append('changed')
    assert result['O2']['value'] == ['item']


def test_batch_missing_scalar_and_sensitive_targets_keep_existing_semantics():
    driver=SimpleNamespace(find_elements=lambda *a:[])
    values=collect_measurements(driver,'https://authorized.test/',{
        k:dict(selector='.absent',read=k,step_id='S1') for k in ('count','selected','visible_exists')},batch=True)
    assert values['count']['value']==0 and values['visible_exists']['value'] is False
    assert values['selected']['value'] is None and values['selected']['status']=='missing'
    driver.find_elements=lambda *a:[object()]
    driver.execute_script=lambda *a:[dict(visible=True,sensitive=True)]
    with pytest.raises(ValueError,match='Sensitive controls'):
        collect_measurements(driver,'https://authorized.test/',{
            'O1':dict(selector='#protected',read='count',step_id='S1')},batch=True)


class Clock:
    def __init__(self): self.now=0.
    def monotonic(self): return self.now


class Responses:
    """Deterministic elapsed time, no real sleeping or mock isolation claims."""
    def __init__(self,clock,messages): self.clock,self.messages=clock,list(messages)
    def get(self,timeout):
        delay,message=self.messages.pop(0) if self.messages else (timeout+1,None)
        if delay>timeout:
            self.clock.now+=timeout
            raise queue.Empty()
        self.clock.now+=delay
        return copy.deepcopy(message)


def controller(tmp_path,monkeypatch,*,slow_phase=None,session_seconds=100):
    clock=Clock()
    monkeypatch.setattr(runtime,'time',SimpleNamespace(monotonic=clock.monotonic))
    session=object.__new__(runtime.LocalSession)
    session.session_id,session.purpose,session.run_dir='offline','formal',tmp_path
    session.research_mode=True
    session.limits=runtime.RuntimeLimits(execution_seconds=.5,observation_seconds=.5,oracle_seconds=.5)
    session._closed=False
    session._lock=threading.RLock()
    session._expires=session_seconds
    session._proc=SimpleNamespace(stdin=io.StringIO())
    session._last_url,session._last_observation_ref='https://authorized.test/','prior.json'
    session.cleanup={}
    closed=[]
    def close(**kw):
        closed.append(kw)
        session.cleanup.update(process_tree_terminated=True,owned_processes_remaining=0)
    session.close_session=close
    partial=dict(script_status='NOT_RUN',oracle_status='NOT_RUN',measurements={},
        timings=dict.fromkeys(runtime.CHECKED_PHASES,0.),phase_status=dict.fromkeys(runtime.CHECKED_PHASES,'NOT_RUN'))
    messages=[]
    for phase in runtime.CHECKED_PHASES:
        partial['phase_status'][phase]='RUNNING'
        messages.append((0,dict(_runtime_phase=phase,event='started',partial=copy.deepcopy(partial))))
        if phase=='script_execute': partial['script_status']='COMPLETE'
        if phase=='oracle_capture':
            partial['oracle_status']='COMPLETE'
            partial['measurements']={'O1':dict(value=50,status='observed')}
        partial['timings'][phase]=.6 if phase==slow_phase else .4
        partial['phase_status'][phase]='COMPLETE'
        messages.append((partial['timings'][phase],dict(_runtime_phase=phase,event='finished',partial=copy.deepcopy(partial))))
    messages.append((0,dict(partial,status='COMPLETE')))
    session._responses=Responses(clock,messages)
    return session,clock,closed


def test_four_phase_budgets_do_not_share_the_script_deadline(tmp_path,monkeypatch):
    session,clock,closed=controller(tmp_path,monkeypatch)
    result=session.execute_checked('driver.title',step_id='S1')
    assert result['status']=='COMPLETE' and clock.now==pytest.approx(1.6)
    assert not closed and result['script_status']=='COMPLETE'
    assert result['timings']==dict.fromkeys(runtime.CHECKED_PHASES,.4)


@pytest.mark.parametrize('phase',runtime.CHECKED_PHASES)
def test_timeout_classifies_phase_preserves_progress_and_kills_tree(tmp_path,monkeypatch,phase):
    session,clock,closed=controller(tmp_path,monkeypatch,slow_phase=phase)
    result=session.execute_checked('driver.title',step_id='S1')
    assert result['status']=='TIMEOUT' and result['error_type']=='RuntimePhaseTimeout'
    assert result['failed_phase']==phase and result['timeout_scope']=='phase'
    assert result['failure_domain']==runtime.phase_domain(phase)
    assert result['timings'][phase]==pytest.approx(.5) and result['phase_status'][phase]=='TIMEOUT'
    assert clock.now==pytest.approx(runtime.CHECKED_PHASES.index(phase)*.4+.5)
    assert closed==[dict(graceful=False)] and result['cleanup']['process_tree_terminated']
    if phase in {'oracle_capture','post_observe'}:
        assert result['script_status']=='COMPLETE'
    if phase=='post_observe':
        assert result['oracle_status']=='COMPLETE' and result['measurements']['O1']['value']==50
        assert 'screenshot_ref' not in result['measurements']['O1']  # not fabricated


def test_session_deadline_still_caps_all_phases(tmp_path,monkeypatch):
    session,clock,_=controller(tmp_path,monkeypatch,session_seconds=.6)
    result=session.execute_checked('driver.title',step_id='S1')
    assert result['timeout_scope']=='session' and clock.now==pytest.approx(.6)
    assert result['failed_phase']=='script_execute' and result['script_status']=='TIMEOUT'


def test_worker_post_observation_failure_does_not_become_script_failure():
    driver=SimpleNamespace(title='Fixture',session_id='test')
    captured=[]
    def capture(**kw):
        captured.append(kw)
        if len(captured)==2: raise RuntimeError('synthetic post-observation error')
        return dict(url='https://authorized.test/',state_hash='entry',observation_ref='entry.json')
    events=[]
    result=checked_execution(driver,'https://authorized.test/',dict(source='driver.title',step_id='S1'),
        capture=capture,save_screenshot=lambda name:pytest.fail('No snapshot available'),
        phase_event=lambda name,event,partial:events.append((name,event,copy.deepcopy(partial))))
    assert result['status']=='ERROR' and result['failure_domain']=='observation'
    assert result['failed_phase']=='post_observe'
    assert result['script_status']==result['oracle_status']=='COMPLETE'
    assert result['execution']['source_complete'] and result['execution']['phase']=='complete'
    assert set(result['timings'])==set(runtime.CHECKED_PHASES)
    assert [name for name,event,_ in events if event=='started']==list(runtime.CHECKED_PHASES)


def test_worker_formal_evidence_requires_screenshot_and_records_all_timings(monkeypatch):
    driver=SimpleNamespace(title='Fixture',session_id='test')
    counter=[]
    def capture(**kw):
        counter.append(1)
        return dict(url='https://authorized.test/',state_hash='stable',
                    observation_id='snapshot',observation_ref='snapshot.json')
    import web_agent.checked_selenium as checked
    monkeypatch.setattr(checked,'collect_measurements',lambda *a,**kw:{'O1':dict(value=1,status='observed')})
    result=checked_execution(driver,'https://authorized.test/',dict(source='driver.title'),capture=capture,
        save_screenshot=lambda name:True)
    assert result['status']=='COMPLETE' and all(v=='COMPLETE' for v in result['phase_status'].values())
    assert result['measurements']['O1']['observation_ref']=='snapshot.json'
    assert result['measurements']['O1']['screenshot_ref']=='snapshot.png'
    assert len(counter)==2 and all(v>=0 for v in result['timings'].values())
    failed=checked_execution(driver,'https://authorized.test/',dict(source='driver.title'),capture=capture,
        save_screenshot=lambda name:False)
    assert failed['status']=='ERROR' and failed['script_status']=='COMPLETE'
    assert failed['failed_phase']=='post_observe' and 'screenshot_ref' not in failed['measurements']['O1']


@pytest.mark.parametrize('options',[{'observation_seconds':0},{'observation_seconds':31},{'oracle_seconds':float('nan')}])
def test_stage_limits_are_validated(options):
    with pytest.raises(ValueError): runtime.RuntimeLimits(**options)


@pytest.mark.parametrize('cold_failure',[False,True])
def test_pipeline_does_not_regenerate_successful_code_after_observation_error(tmp_path,monkeypatch,cold_failure):
    from web_agent.pipeline import run_pipeline
    generated=[]
    class Model:
        calls=[]
        def complete_json(self,system,user):
            if system.startswith('You are Agent1'):
                return dict(steps=[dict(id='S1',goal='Read state')],oracles=[dict(id='O1',
                    step_id='S1',kind='gte',expected=1,meaning='at least one',
                    source_type='user_requirement',source_quote='at least one item')])
            if system.startswith('You are Agent2'): return dict(approved=True,input_status='consistent',issues=[])
            generated.append(1)
            return dict(code='driver.title',bindings={'O1':dict(selector='.item',read='count',step_id='S1')})
    failure=dict(status='ERROR',error_type='RuntimeError',failed_phase='post_observe',
                 failure_domain='observation',script_status='COMPLETE',step_id='S1')
    class Session:
        def __init__(self,purpose): self.purpose,self.run_dir=purpose,tmp_path
        def observe(self,**kw): return {}
        def execute_checked(self,*args,**kw): return dict(status='COMPLETE') if cold_failure else failure
        def execute_program(self,*args,**kw): return dict(execution_status='ERROR',steps=[failure])
        def close_session(self): return dict(process_tree_terminated=True,profile_removed=True)
    monkeypatch.setattr(runtime,'ARTIFACT_ROOT',tmp_path)
    monkeypatch.setattr(runtime,'create_session',lambda *a,purpose,**kw:Session(purpose))
    result=run_pipeline(target='dynamic',mode='llm',start_url='https://authorized.test/',requirement='Read state and verify at least one item',
        allow_local_research=True,allow_live_site=True,output_root=tmp_path,llm=Model())
    assert result['verdict']['status']=='ERROR' and len(generated)==1
    assert result['verdict']['failed_phase']=='post_observe' and result['verdict']['script_status']=='COMPLETE'
    assert not (tmp_path/'formal_evidence.json').exists()
