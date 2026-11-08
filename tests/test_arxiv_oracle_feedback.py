"""Offline contracts and routing. These doubles do not prove real LLM/browser success."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from web_agent.schema import validate_dynamic_plan, validate_oracle_binding
from web_agent.checked_selenium import collect_measurements, EVIDENCE_GUIDE
from web_agent.agents.designer import Agent1TestDesigner
from web_agent.agents.reviewer import Agent2TestReviewer
from web_agent.agents.generator import Agent3SeleniumGenerator
from web_agent.agents.verifier import Agent5EvidenceVerifier


def oracle(kind='gte', expected=1, step='S1'):
    return dict(id='O1',step_id=step,kind=kind,expected=expected,meaning='requested result count')


def evidence(actual, read='count'):
    return dict(execution_status='COMPLETE',measurements={'O1':dict(value=actual,read=read,
        step_id='S1',status='observed',observation_ref='real.json',screenshot_ref='real.png')})


@pytest.mark.parametrize('kind,threshold,actual,passed', [
    ('gte',1,50,True),('gte',1,0,False),('gte',1,1,True),('gt',1,1,False),
    ('gt',1,2,True),('lte',3,2,True),('lte',3,4,False),('lt',3,3,False),
])
def test_numeric_oracle_comparisons(kind,threshold,actual,passed):
    result=Agent5EvidenceVerifier().verify_dynamic({'oracles':[oracle(kind,threshold)]},evidence(actual))
    assert result.status==('PASS' if passed else 'FAIL')
    assert result.checks[0]['actual']==actual


@pytest.mark.parametrize('bad', [True,False,'1',None,float('nan'),float('inf')])
def test_numeric_plan_never_accepts_bool_or_nonfinite(bad):
    with pytest.raises(ValueError,match='finite number'):
        validate_dynamic_plan(dict(steps=[dict(id='S1',goal='read')],oracles=[oracle(expected=bad)]))


@pytest.mark.parametrize('o,binding', [
    (oracle('equals',True),dict(read='count')),
    (oracle('gte',1),dict(read='exists')),
    (oracle('gte',1),dict(read='selected')),
    (oracle('equals',1),dict(read='visible_exists')),
])
def test_binding_types_are_rejected_without_changing_expected(o,binding):
    before=dict(o)
    with pytest.raises(ValueError,match='incompatible'):
        validate_oracle_binding(o,binding)
    assert o==before


def test_wrong_formal_binding_type_is_technical_error_not_website_failure():
    result=Agent5EvidenceVerifier().verify_dynamic({'oracles':[oracle('equals',True)]},evidence(50,'count'))
    assert result.status=='ERROR' and 'type error' in result.message


def test_exists_and_visible_exists_differ_from_scalar_state_missing():
    class Node:
        def __init__(self, visible): self.visible=visible
        def is_displayed(self): return self.visible
        def get_attribute(self,k): return ''
    driver=SimpleNamespace(find_elements=lambda by,selector: [] if selector=='missing' else [Node(False)])
    queries={key:dict(selector=selector,read=read,step_id='S1') for key,selector,read in [
        ('hidden_exists','hidden','exists'),('hidden_visible','hidden','visible_exists'),
        ('missing_exists','missing','exists'),('missing_state','missing','visible'),
        ('hidden_count','hidden','count')]}
    result=collect_measurements(driver,'https://authorized.test/',queries)
    assert [result[k]['value'] for k in queries]==[True,False,False,None,0]
    assert result['hidden_count']['visible_only'] is True
    assert result['missing_state']['status']=='missing'
    assert Agent5EvidenceVerifier().verify_dynamic({'oracles':[oracle('equals',False)]},
        evidence(False,'visible_exists')).status=='PASS'


def test_agent3_rejects_count_for_boolean_before_execution():
    class Model:
        def complete_json(self,*args):
            return dict(code='driver.title',bindings={'O1':dict(selector='.items',read='count',step_id='S1')})
    with pytest.raises(ValueError,match='visible_exists'):
        Agent3SeleniumGenerator().develop_step({'oracles':[oracle('equals',True)]},dict(id='S1'),{},llm=Model())


def test_planning_provenance_and_active_choice_prompts():
    class Model:
        def complete_json(self,system,user):
            assert 'ACTIVELY SELECT' in system and 'Do NOT assume webpage defaults' in system
            assert 'exact text/heading' in system and 'source_quote' in system
            assert 'gte' in system
            return dict(steps=[dict(id='S1',goal='Set All fields and search')],
                oracles=[dict(oracle(),source_type='user_requirement',source_quote='at least one')])
    draft=Agent1TestDesigner().design_dynamic('Set All fields, search and show at least one result',
        url='https://authorized.test/',llm=Model())
    assert draft['oracles'][0]['expected']==1
    broken=dict(draft,oracles=[dict(oracle(),source_type='user_requirement',source_quote='invented request')])
    with pytest.raises(ValueError,match='source_quote'):
        validate_dynamic_plan(broken)


def test_reviewer_rejects_invented_heading_even_if_model_approves():
    class Model:
        def complete_json(self,system,user):
            assert EVIDENCE_GUIDE in system and 'INTEGER of VISIBLE' in system
            assert 'actively selected' in system and 'ORIGINAL requirement' in system
            assert 'do NOT reject a plan because read' in system
            assert 'BOOLEAN' in system and 'string-label Oracle' in system
            return dict(approved=True,issues=[])
    draft=dict(requirement='Search and verify at least one result',
        oracles=[oracle('contains','Search results')])
    result=Agent2TestReviewer().review_dynamic(draft,llm=Model())
    assert not result['approved'] and 'no source' in result['issues'][0]


def test_implementation_reviewer_receives_exact_count_semantics():
    class Model:
        def complete_json(self,system,user):
            assert EVIDENCE_GUIDE in system and 'is_displayed filtered' in system
            assert 'issue_type' in system and 'planning' in system
            return dict(approved=True,issues=[])
    result=Agent2TestReviewer().review_implementation({'oracles':[oracle('equals',True)]},[],
        {'O1':dict(read='count')},observation={},llm=Model())
    assert result['approved'] is False and result['step_ids']==['S1']
    assert 'incompatible' in result['issues'][0]


@pytest.mark.parametrize('category,repeat', [('implementation',False),('planning',False),
                                          ('implementation',True),('planning',True)])
def test_feedback_routes_to_owner_once_and_preserves_versions(tmp_path,monkeypatch,category,repeat):
    from web_agent import selenium_runtime as runtime
    from web_agent.pipeline import run_pipeline
    created=[]
    class Session:
        def __init__(self,purpose):
            self.purpose,self.run_dir=purpose,tmp_path
        def observe(self,**kw): return {}
        def close_session(self): return dict(process_tree_terminated=True,profile_removed=True)
        def execute_checked(self,source,**kw): return dict(status='COMPLETE')
        def execute_program(self,source,**kw):
            measurements=evidence(2)['measurements']
            measurements['O1']['step_id']='S2'
            return dict(execution_status='COMPLETE',browser_session_id=str(len(created)),
                        measurements=measurements)
    def create(*args,purpose,**kw):
        s=Session(purpose);created.append(s);return s
    class Model:
        calls=[]
        def __init__(self): self.plans,self.audits,self.developer_calls=0,0,[]
        def complete_json(self,system,user):
            data=json.loads(user)
            if system.startswith('You are Agent1'):
                self.plans+=1
                if self.plans==2:
                    assert category=='planning' and 'requested comparison' in str(data['review_feedback'])
                return dict(steps=[dict(id='S1',goal='Prepare page'),dict(id='S2',goal='Count results')],
                    oracles=[dict(oracle('equals' if category=='planning' and self.plans==1 else 'gte',1,'S2'),
                                  source_type='user_requirement',source_quote='at least one result')])
            if system.startswith('You are Agent2 reviewing'):
                self.audits+=1
                return dict(approved=False if self.audits==1 or repeat else True,
                    issue_type=category,step_ids=['S2'],issues=['requested comparison / locator needs repair'])
            if system.startswith('You are Agent2'):
                return dict(approved=True,input_status='consistent',issues=[])
            self.developer_calls.append(data)
            if data['current_step']['id']=='S2' and category=='implementation' and self.audits:
                assert data['previous_code']=='driver.title'
                assert data['previous_bindings']['O1']['selector']=='.results'
                assert data['actual_error']['issue_type']=='implementation'
                assert 'locator' in str(data['actual_error']['issues'])
            return dict(code='driver.title',bindings={} if data['current_step']['id']=='S1' else {
                'O1':dict(selector='.results',read='count',step_id='S2')})
    model=Model()
    monkeypatch.setattr(runtime,'ARTIFACT_ROOT',tmp_path)
    monkeypatch.setattr(runtime,'create_session',create)
    result=run_pipeline(target='dynamic',mode='llm',requirement='Search and count at least one result',
        start_url='https://authorized.test/',allow_live_site=True,allow_local_research=True,
        output_root=tmp_path,llm=model)
    path=Path(result['run_dir'])
    assert result['verdict']['status']==('ERROR' if repeat else 'PASS')
    assert model.plans==(2 if category=='planning' else 1)
    assert model.audits==2
    revisions=json.loads((path/'review_revisions.json').read_text())
    assert revisions[category]==1 and revisions['planning' if category=='implementation' else 'implementation']==0
    if category=='planning':
        assert json.loads((path/'test_contract_r1.json').read_text())['oracles'][0]['kind']=='equals'
        assert json.loads((path/'test_contract_r2.json').read_text())['oracles'][0]['kind']=='gte'
    else:
        assert (path/'review_prefix_replay.json').exists()
        assert len([d for d in model.developer_calls if d['current_step']['id']=='S1'])==1
    if repeat:
        assert (path/'agent6_diagnosis.json').exists() and not (path/'formal_evidence.json').exists()
    else:
        delivery=json.loads((path/'agent3_delivery.json').read_text())
        assert delivery['repairs_used']==1 and max(delivery['attempts'].values())<=3


def test_todomvc_historical_formal_evidence_still_verifies():
    """Deterministic comparison of existing real evidence, not a new live run."""
    p=Path(__file__).resolve().parents[1]/'artifacts/agent3_runtime_fix_20261107_104506/dynamic_20261107_105944_565a71'
    if not p.exists():
        # Standalone synthetic equivalent for a clone without local artifacts.
        contract={'oracles':[oracle('equals',True)]}
        formal=evidence(True,'selected')
    else:
        contract=json.loads((p/'test_contract.json').read_text(encoding='utf-8'))
        formal=json.loads((p/'formal_evidence.json').read_text(encoding='utf-8'))
    assert Agent5EvidenceVerifier().verify_dynamic(contract,formal).status=='PASS'
