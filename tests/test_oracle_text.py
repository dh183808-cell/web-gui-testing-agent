"""Oracle text completeness and progress contracts; real Chrome lives in live suite."""
from types import SimpleNamespace

import pytest

from web_agent import checked_selenium as checked
from web_agent.local_worker import checked_execution
from web_agent.agents.verifier import Agent5EvidenceVerifier


def test_tail_and_exact_long_text_use_full_observation_not_prefix():
    body = 'paragraph ' * 600 + 'TAIL_KEYWORD'
    calls, events = [], []
    driver = SimpleNamespace(find_elements=lambda *a:[object()])
    def script(source, *args):
        if source == checked.rendered_text_script():
            calls.append('text')
            return [dict(text=body,text_complete=True,status='observed',text_method='rendered_dom')]
        return [dict(visible=True,sensitive=False)]
    driver.execute_script = script
    queries = {k:dict(by='xpath',selector='//article',read='text',step_id='S1') for k in ('O1','O2','O3')}
    result = checked.collect_measurements(driver,'https://authorized.test/',queries,batch=True,on_progress=events.append)
    assert calls == ['text']
    assert all(m['value'] == body and m['text_complete'] for m in result.values())
    for m in result.values(): m.update(observation_ref='snapshot.json',screenshot_ref='snapshot.png')
    contract = {'oracles':[
        dict(id='O1',step_id='S1',kind='contains',expected='TAIL_KEYWORD',meaning='tail'),
        dict(id='O2',step_id='S1',kind='equals',expected=body,meaning='whole'),
        dict(id='O3',step_id='S1',kind='equals',expected=body[:1000],meaning='prefix alone is unequal')]}
    verdict = Agent5EvidenceVerifier().verify_dynamic(contract,dict(execution_status='COMPLETE',measurements=result))
    assert [c['passed'] for c in verdict.checks] == [True,True,False]
    assert [e['event'] for e in events if e['operation']=='rendered_text'] == ['started','complete']
    assert events[0]['oracle_ids'] == ['O1','O2','O3']
    assert events[-1]['event'] == 'reused'
    assert all(body not in str(e) for e in events)  # No page text in operation logs.


@pytest.mark.parametrize('status',['text_limit_exceeded','text_structure_limit','unsupported_shadow_text','sensitive_text'])
def test_incomplete_text_never_becomes_a_business_fail_or_pass(status):
    driver = SimpleNamespace(find_elements=lambda *a:[object()],
        execute_script=lambda script,*a:[dict(text=None,status=status,text_complete=False,text_length=999999)]
        if script == checked.rendered_text_script() else [dict(visible=True,sensitive=False)])
    values = checked.collect_measurements(driver,'https://authorized.test/',{
        'O1':dict(selector='article',read='text',step_id='S1')},batch=True)
    values['O1'].update(observation_ref='snapshot.json',screenshot_ref='snapshot.png')
    assert values['O1']['value'] is None and not values['O1']['text_complete']
    for kind in ('equals','contains'):
        verdict = Agent5EvidenceVerifier().verify_dynamic(
            {'oracles':[dict(id='O1',step_id='S1',kind=kind,expected='some text',meaning='incomplete')]},
            dict(execution_status='COMPLETE',measurements=values))
        assert verdict.status == 'INCONCLUSIVE' and verdict.checks[0]['passed'] is None


def test_native_compatibility_path_also_does_not_silently_truncate():
    class Node:
        text = 'x' * (checked.TEXT_LIMIT + 1)
        def is_displayed(self): return True
        def get_attribute(self,name): return ''
    result = checked.collect_measurements(SimpleNamespace(find_elements=lambda *a:[Node()]),
        'https://authorized.test/',{'O1':dict(selector='article',read='text',step_id='S1')})
    assert result['O1']['status'] == 'text_limit_exceeded' and result['O1']['value'] is None


def test_redaction_does_not_cut_long_text_tail_when_replacement_grows():
    text='token=x ' + 'public content ' * 200 + 'TAIL_MARKER'
    driver=SimpleNamespace(find_elements=lambda *a:[object()],
        execute_script=lambda script,*a:[dict(text=text,text_complete=True,status='observed')]
        if script==checked.rendered_text_script() else [dict(visible=True,sensitive=False)])
    result=checked.collect_measurements(driver,'https://authorized.test/',
        {'O1':dict(selector='article',read='text',step_id='S1')},batch=True)
    assert 'token=x' not in result['O1']['value']
    assert result['O1']['value'].endswith('TAIL_MARKER') and result['O1']['text_complete']


def test_controller_timeout_identifies_actual_read_and_keeps_completed_title():
    from web_agent.selenium_runtime import RuntimePhaseTimeout
    active = dict(oracle_id='O4',oracle_ids=['O4','O5'],step_id='S4',by='xpath',
        selector='//article',read='text',operation='rendered_text',event='started',started_seconds=.7)
    partial = dict(oracle_log=[active],measurements={'O3':dict(value='Title',status='observed')},
                   script_status='COMPLETE')
    exc = RuntimePhaseTimeout('oracle_capture',partial,5.)
    assert exc.partial['oracle_timeout']['oracle_ids'] == ['O4','O5']
    assert exc.partial['oracle_timeout']['elapsed_seconds'] == 4.3
    assert exc.partial['measurements']['O3']['value'] == 'Title'
    assert partial['oracle_log'][-1]['event'] == 'started'  # Input record unchanged.


def test_worker_logs_completed_title_before_body_read_error():
    class Node:
        def __init__(self,text): self.text = text
    driver = SimpleNamespace(title='Title',session_id='offline',
        find_elements=lambda by,sel:[Node('Title')] if sel=='h1' else [Node('body')])
    def script(source, nodes, *args):
        if source != checked.rendered_text_script(): return [dict(visible=True,sensitive=False)]
        if nodes[0].text == 'body': raise RuntimeError('synthetic blocked DOM')
        return [dict(text='Title',text_complete=True,status='observed')]
    driver.execute_script = script
    events = []
    result = checked_execution(driver,'https://authorized.test/',
        dict(source='driver.title',step_id='S1',queries={
            k:dict(selector=selector,read='text',step_id='S1') for k,selector in [('O1','h1'),('O2','article')]}),
        capture=lambda **kw:dict(url='https://authorized.test/',observation_id='snapshot',observation_ref='snapshot.json'),
        save_screenshot=lambda n:True,record_measurement=events.append)
    assert result['status']=='ERROR' and result['failed_phase']=='oracle_capture'
    assert result['measurements']['O1']['value'] == 'Title'
    assert events[-1]['operation']=='rendered_text' and events[-1]['event']=='error'
    assert events[-1]['oracle_id']=='O2' and events[-1]['selector']=='article'


def test_oracle_progress_is_incremental_and_never_renews_deadline(tmp_path,monkeypatch):
    import copy
    from tests.test_runtime_performance import controller
    session,clock,closed=controller(tmp_path,monkeypatch,slow_phase='oracle_capture')
    messages=session._responses.messages
    index=next(i for i,(delay,msg) in enumerate(messages)
        if msg.get('_runtime_phase')=='oracle_capture' and msg['event']=='finished')
    # First result completed; a later read starts after 0.1 sec. Repeated
    # progress cannot extend the phase's 0.5 sec hard deadline.
    events=[
        dict(oracle_id='O1',oracle_ids=['O1'],selector='h1',read='text',
             operation='measurement',event='observed',started_seconds=.1),
        dict(oracle_id='O2',oracle_ids=['O2','O3'],selector='article',read='text',
             operation='rendered_text',event='started',started_seconds=.2)]
    progress=[(.1,dict(_runtime_phase='oracle_capture',event='progress',
        partial=dict(oracle_progress=e,**(
            dict(measurements={'O1':dict(value='Title',status='observed')}) if i==0 else {}))))
        for i,e in enumerate(events)]
    messages[index:index]=copy.deepcopy(progress)
    result=session.execute_checked('driver.title',step_id='S1')
    assert result['status']=='TIMEOUT' and result['failed_phase']=='oracle_capture'
    assert result['oracle_timeout']['selector']=='article'
    assert result['oracle_timeout']['oracle_ids']==['O2','O3']
    assert result['measurements']['O1']['value']=='Title'
    assert result['timings']['oracle_capture']==pytest.approx(.5)
    assert result['cleanup']['process_tree_terminated'] and closed
