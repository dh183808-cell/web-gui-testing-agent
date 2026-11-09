"""Negative evidence control regressions; real acceptance is saved separately."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from web_agent import checked_selenium as checked
from web_agent.agents.verifier import Agent5EvidenceVerifier
from web_agent.schema import validate_oracle_binding


TARGET = dict(role='button',name='Clear')
BINDING = dict(read='target_present',target=TARGET,step_id='S2')
ORACLE = dict(id='O1',step_id='S2',kind='equals',expected=True,meaning='required control',
              target=TARGET,source_type='test_basis',source_quote='Visible Clear button')


def test_target_binding_cannot_substitute_a_missed_locator():
    assert checked.normalize_bindings({'O1':BINDING})['O1']==BINDING
    validate_oracle_binding(ORACLE,BINDING)
    with pytest.raises(ValueError,match='does not establish absence'):
        validate_oracle_binding(ORACLE,dict(read='visible_exists',selector='#wrong'))
    with pytest.raises(ValueError,match='SAME frozen target'):
        validate_oracle_binding(ORACLE,dict(BINDING,target=dict(role='button',name='Other')))
    with pytest.raises(ValueError,match='no locator'):
        checked.normalize_bindings({'O1':dict(BINDING,selector='#wrong')})


def fake_probe(monkeypatch,*,reasons=(),handles=('w',),delayed=False,loading=False):
    clock=[0.0]
    monkeypatch.setattr(checked.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(checked.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    def script(*args):
        return dict(url='https://authorized.test/form',title='Form',
            ready_state='loading' if loading else 'complete',
            found=1 if delayed and clock[0]>=.4 else 0,controls=[],total_controls=0,
            dom_nodes=10,reasons=list(reasons),page_context=True)
    return SimpleNamespace(current_window_handle='w',window_handles=list(handles),execute_script=script)


def test_probe_waits_and_reuses_identical_oracle_measurements(monkeypatch):
    driver=fake_probe(monkeypatch)
    result=checked.collect_measurements(driver,'https://authorized.test/',{'O1':BINDING,'O2':BINDING})
    assert result['O1']['value'] is False and result['O1']['absence_confirmed']
    assert result['O1']['probe']['elapsed_seconds']>=2
    assert len(result['O1']['probe']['samples'])>=3
    assert result['O1']==result['O2'] and result['O1'] is not result['O2']


@pytest.mark.parametrize('options',[
    dict(reasons=['frames_not_inspected']),dict(reasons=['shadow_roots_not_inspected']),
    dict(reasons=['unnamed_candidate']),dict(handles=['w','new']),dict(loading=True),
])
def test_partial_or_wrong_window_context_is_unknown(monkeypatch,options):
    result=checked.probe_target(fake_probe(monkeypatch,**options),TARGET)
    assert result['value'] is None and result['status']=='absence_unconfirmed'
    assert not result['absence_confirmed']


def test_loading_delay_is_not_misclassified_as_absence(monkeypatch):
    result=checked.probe_target(fake_probe(monkeypatch,delayed=True),TARGET)
    assert result['value'] is True and result['probe']['elapsed_seconds']<2
    assert not result['absence_confirmed']


def formal_measurement():
    return dict(BINDING,value=False,status='observed',absence_confirmed=True,inventory_complete=True,
        probe=dict(method='named_control_inventory',url='https://authorized.test/form',
                   context_stable=True,reasons=[],wait_seconds=2,elapsed_seconds=2.1,
                   samples=[dict(ready_state='complete',found=0)]*3),
        observation_ref='formal.json',screenshot_ref='formal.png')


@pytest.mark.parametrize('mutation,expected',[
    ('none','FAIL'),('trial','INCONCLUSIVE'),('partial','INCONCLUSIVE'),
    ('unreviewed','INCONCLUSIVE'),('changed_page','INCONCLUSIVE'),
    ('present_in_formal','INCONCLUSIVE'),('technical','ERROR'),
])
def test_only_reviewed_independent_complete_negative_evidence_can_fail(mutation,expected):
    m=formal_measurement()
    evidence=dict(execution_status='COMPLETE',purpose='formal',
        execution_scope='reviewed_prefix_and_target_probe',measurements={'O1':m},
        blockage=dict(review=dict(approved=True),measurement=copy.deepcopy(m)))
    if mutation=='trial': evidence['purpose']='trial'
    if mutation=='partial': m['inventory_complete']=False
    if mutation=='unreviewed': evidence['blockage']['review']['approved']=False
    if mutation=='changed_page': m['probe']['url']='https://authorized.test/wrong'
    if mutation=='present_in_formal': m['value']=True
    if mutation=='technical': evidence['execution_status']='ERROR'
    assert Agent5EvidenceVerifier().verify_dynamic({'oracles':[ORACLE]},evidence).status==expected


def test_browser_failure_remains_a_technical_error():
    from web_agent.local_worker import checked_execution
    driver=SimpleNamespace(title='Form',session_id='offline',current_window_handle='w',window_handles=['w'],
        execute_script=lambda *a:(_ for _ in ()).throw(RuntimeError('synthetic browser failure')))
    result=checked_execution(driver,'https://authorized.test/',dict(source='driver.title',step_id='S2',
        queries={'O1':BINDING}),capture=lambda **kw:dict(url='https://authorized.test/',
        observation_id='snapshot',observation_ref='snapshot.json'),save_screenshot=lambda name:True)
    assert result['status']=='ERROR' and result['failed_phase']=='oracle_capture'
    assert not result['measurements']


def test_redundant_missed_locator_cannot_overrule_incomplete_negative_inventory():
    m=formal_measurement()
    m.update(value=None,status='absence_unconfirmed',inventory_complete=False)
    evidence=dict(execution_status='COMPLETE',purpose='formal',
        execution_scope='reviewed_prefix_and_target_probe',
        blockage=dict(review=dict(approved=True),measurement=m),
        measurements={'O1':m,'O2':dict(read='visible_exists',step_id='S2',value=False,status='observed',
            observation_ref='formal.json',screenshot_ref='formal.png')})
    contract=dict(oracles=[ORACLE,dict(id='O2',step_id='S2',kind='equals',expected=True,meaning='visible target')])
    assert Agent5EvidenceVerifier().verify_dynamic(contract,evidence).status=='INCONCLUSIVE'


def test_presence_field_on_value_oracle_reports_exact_problem(tmp_path):
    from web_agent.agents.designer import Agent1TestDesigner
    class Model:
        def complete_json(self,*args):
            return dict(steps=[dict(id='S1',goal='Read input')],oracles=[dict(ORACLE,
                id='O4',step_id='S1',expected='',source_quote='cleared')])
    draft=tmp_path/'draft.json'
    with pytest.raises(ValueError,match='Oracle O4: target is ONLY.*OMIT'):
        Agent1TestDesigner().design_dynamic('cleared',url='https://authorized.test/',llm=Model(),proposal_path=draft)
    assert draft.exists() and draft.with_suffix('.validation.json').exists()


@pytest.mark.parametrize('headless,formal_headless,expected_modes', [
    (True,None,[True,True,True]), (False,None,[False,False,False]),
    (True,False,[True,True,False]),
])
def test_reviewed_prefix_is_cold_replayed_and_formally_verified_without_dependent_codegen(
        tmp_path,monkeypatch,headless,formal_headless,expected_modes):
    from web_agent import selenium_runtime as runtime
    from web_agent.pipeline import run_pipeline
    created=[]
    modes=[]
    events=[]
    generated=[]
    basis='Visible Clear button; clicking Clear empties input.'
    class Model:
        calls=[]
        def complete_json(self,system,user):
            data=json.loads(user)
            if system.startswith('You are Agent1'):
                return dict(steps=[dict(id='S1',goal='Fill input'),dict(id='S2',goal='Check Clear'),
                                  dict(id='S3',goal='Click Clear and check input',depends_on=['S2'])],
                    oracles=[ORACLE,dict(id='O2',step_id='S3',kind='equals',expected='',meaning='empty input',
                        source_type='test_basis',source_quote='clicking Clear empties input.')])
            if system.startswith('You are Agent2'):
                return dict(approved=True,input_status='consistent',issues=[])
            sid=data['current_step']['id'];generated.append(sid)
            return dict(code='driver.title',bindings={'O1':BINDING} if sid=='S2' else {})
    class Session:
        def __init__(self,purpose): self.purpose=purpose;self.run_dir=tmp_path
        def observe(self,**kw): return {}
        def close_session(self): return dict(process_tree_terminated=True,profile_removed=True)
        def execute_checked(self,source,**kw):
            return dict(status='COMPLETE',measurements={'O1':formal_measurement()} if kw['step_id']=='S2' else {})
        def execute_program(self,source,**kw):
            assert kw['step_ids']==['S1','S2'] and '# webagent-step: S3' not in source
            return dict(execution_status='COMPLETE',purpose=self.purpose,browser_session_id=str(len(created)),
                        script_sha256=hashlib.sha256(source.encode()).hexdigest(),measurements={'O1':formal_measurement()})
    def create(*args,purpose,**kw):
        modes.append(kw['headless'])
        s=Session(purpose);created.append(s);return s
    monkeypatch.setattr(runtime,'ARTIFACT_ROOT',tmp_path)
    monkeypatch.setattr(runtime,'create_session',create)
    result=run_pipeline(target='dynamic',mode='llm',requirement='Fill input and check Clear; if present click it.',
        test_basis=basis,start_url='https://authorized.test/form',allow_live_site=True,
        allow_local_research=True,output_root=tmp_path,llm=Model(),headless=headless,
        formal_headless=formal_headless,progress_callback=events.append)
    assert result['verdict']['status']=='FAIL'
    assert generated==['S1','S2'] and [s.purpose for s in created]==['trial','trial','formal']
    assert modes == expected_modes
    assert events[0]['action']=='run_started' and events[-1]['action']=='run_finished'
    assert sum(e['action']=='call_started' for e in events)==sum(e['action']=='call_finished' for e in events)
    assert result['verdict']['skipped_steps'][0]['step_id']=='S3'
    from pathlib import Path
    delivery=json.loads((Path(result['run_dir'])/'agent3_delivery.json').read_text(encoding='utf-8'))
    assert delivery['cold_trial_complete'] and delivery['repairs_used']==0
