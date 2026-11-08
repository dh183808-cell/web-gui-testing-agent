"""Targeted offline regressions; actual Chrome coverage lives in test_dynamic_demo."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from web_agent.checked_selenium import collect_measurements, normalize_bindings, execute_source, EVIDENCE_GUIDE
from web_agent.dom_inspector import diagnostic_url, safe_href
from web_agent.agents.designer import Agent1TestDesigner
from web_agent.agents.reviewer import Agent2TestReviewer
from web_agent.agents.generator import Agent3SeleniumGenerator
from web_agent.agents.verifier import Agent5EvidenceVerifier


class Option:
    text = 'All fields'
    def __init__(self, selected=True): self.selected = selected
    def is_selected(self): return self.selected
    def get_attribute(self, name): return ''


class Select:
    tag_name = 'select'
    def __init__(self, options=None, visible=True):
        self.options = [Option()] if options is None else options
        self.visible = visible
    def find_elements(self, by, selector):
        assert (by, selector) == ('tag name', 'option')
        return self.options
    def is_displayed(self): return self.visible
    def get_attribute(self, name): return 'all' if name == 'value' else ''


def test_selected_text_is_display_label_not_internal_value():
    driver = SimpleNamespace(find_elements=lambda *args: [Select()])
    queries = {'O1':dict(by='id',selector='field',read='selected_text',step_id='S1'),
               'value':dict(selector='#field',read='attribute',attribute='value',step_id='S1')}
    values = collect_measurements(driver,'https://authorized.test/',queries)
    assert values['O1']['value'] == 'All fields'
    assert values['value']['value'] == 'all'
    values['O1'].update(observation_ref='actual.json',screenshot_ref='actual.png')
    verdict = Agent5EvidenceVerifier().verify_dynamic({'oracles':[dict(id='O1',step_id='S1',
        kind='equals',expected='All fields',meaning='requested dropdown label')]},
        dict(execution_status='COMPLETE',measurements=values))
    assert verdict.status == 'PASS' and verdict.checks[0]['binding']['read'] == 'selected_text'


@pytest.mark.parametrize('nodes,status', [([], 'missing'), ([Select(),Select()], 'ambiguous'),
    ([Select(visible=False)],'hidden'), ([Select(options=[])],'selection_missing'),
    ([Select(options=[Option(),Option()])],'selection_ambiguous')])
def test_selected_text_unknown_is_never_false(nodes,status):
    result = collect_measurements(SimpleNamespace(find_elements=lambda *a:nodes),
        'https://authorized.test/',{'O1':dict(selector='select',read='selected_text',step_id='S1')})['O1']
    assert result['value'] is None and result['status'] == status


def test_selected_text_requires_native_select_and_rejects_old_unsupported_read():
    node = Select(); node.tag_name = 'option'
    with pytest.raises(ValueError,match='Step S1 Oracle O1.*native select'):
        collect_measurements(SimpleNamespace(find_elements=lambda *a:[node]),'https://authorized.test/',
            {'O1':dict(selector='option',read='selected_text',step_id='S1')})
    with pytest.raises(ValueError,match='selected_text'):
        normalize_bindings({'O1':dict(selector='select',read='selected_value',step_id='S1')})


def test_semantic_plans_do_not_freeze_unsupported_execution_hints():
    class Model:
        def complete_json(self,system,user):
            return dict(steps=[dict(id='S1',goal='Choose All fields')],oracles=[dict(
                id='O1',step_id='S1',kind='equals',expected='All fields',meaning='label',read='selected_value')])
    contract = Agent1TestDesigner().design_dynamic('Choose All fields',url='https://authorized.test/',llm=Model())
    assert contract['oracles'][0]['expected'] == 'All fields' and 'read' not in contract['oracles'][0]


def test_reviewer_receives_actual_dropdown_capability_and_checkpoint():
    class Model:
        def complete_json(self,system,user):
            assert EVIDENCE_GUIDE in system and 'selected_value is NOT supported' in system
            assert 'repair_scope="bindings"' in system and 'read="selected_text"' in system
            assert json.loads(user)['untrusted_step_checkpoints']['S1']['url'] == 'checkpoint'
            return dict(approved=True,issues=[])
    result = Agent2TestReviewer().review_implementation({'oracles':[dict(id='O1',step_id='S1',
        kind='equals',expected='All fields')]},[],{'O1':dict(read='selected_text')},
        observation={},checkpoints={'S1':dict(url='checkpoint')},llm=Model())
    assert result['approved']


@pytest.mark.parametrize('repeat',[False,True])
def test_binding_only_review_preserves_successful_suffix_and_browser(tmp_path,monkeypatch,repeat):
    from web_agent import selenium_runtime as runtime
    from web_agent.pipeline import run_pipeline
    created, executed = [], []
    code = {f'S{i}':f'checkpoint{i} = driver.title' for i in range(1,4)}
    class Session:
        def __init__(self,purpose):
            self.purpose,self.run_dir,self.at = purpose,tmp_path,0
            self.session_id = str(len(created))
            self.metadata = {'browser_session_id':self.session_id}
        def observe(self,**kw): return dict(url=f'checkpoint{self.at}')
        def close_session(self): return dict(process_tree_terminated=True,profile_removed=True)
        def execute_checked(self,source,**kw):
            self.at += 1; executed.append((self.session_id,kw['step_id'],source))
            return dict(status='COMPLETE')
        def execute_program(self,source,**kw):
            executed.append((self.session_id,'full_replay',source))
            assert kw['bindings']['O1']['read'] == 'selected_text'
            return dict(execution_status='COMPLETE',browser_session_id=self.session_id,
                measurements={'O1':dict(kw['bindings']['O1'],value='All fields',status='observed',
                    observation_ref='real.json',screenshot_ref='real.png')})
    def create(*args,purpose,**kw):
        s=Session(purpose);created.append(s);return s
    class Model:
        calls=[]
        def __init__(self): self.generated,self.repairs,self.audits=[],0,0
        def complete_json(self,system,user):
            data=json.loads(user)
            if system.startswith('You are Agent1'):
                return dict(steps=[dict(id=f'S{i}',goal='Choose All fields' if i==1 else 'Continue')
                    for i in range(1,4)],oracles=[dict(id='O1',step_id='S1',kind='equals',
                        expected='All fields',meaning='requested label')])
            if system.startswith('You are Agent2 reviewing'):
                self.audits+=1
                return dict(approved=self.audits>1 and not repeat,issues=['O1 reads internal value, use selected_text'],
                    issue_type='implementation',repair_scope='bindings',step_ids=['S1'])
            if system.startswith('You are Agent2'):
                return dict(approved=True,issues=[])
            if system.startswith('You are Agent3 repairing'):
                self.repairs+=1
                assert len(created)==1 and len(executed)==3
                assert data['untrusted_page']['url']=='checkpoint1'
                assert data['previous_code']==code['S1']
                assert data['previous_bindings']['O1']['read']=='attribute'
                assert data['contract']['oracles'][0]['expected']=='All fields'
                return dict(bindings={'O1':dict(selector='select',read='selected_text',step_id='S1')})
            sid=data['current_step']['id'];self.generated.append(sid)
            return dict(code=code[sid],bindings={} if sid!='S1' else {'O1':dict(
                selector='select',read='attribute',attribute='value',step_id='S1')})
    model=Model()
    monkeypatch.setattr(runtime,'ARTIFACT_ROOT',tmp_path)
    monkeypatch.setattr(runtime,'create_session',create)
    result=run_pipeline(target='dynamic',mode='llm',requirement='Choose All fields and continue',
        start_url='https://authorized.test/',allow_live_site=True,allow_local_research=True,
        output_root=tmp_path,llm=model)
    path=Path(result['run_dir'])
    assert result['verdict']['status']==('ERROR' if repeat else 'PASS')
    assert model.generated==['S1','S2','S3'] and model.repairs==1 and model.audits==2
    assert len(created)==(1 if repeat else 3)
    assert not (path/'review_prefix_replay.json').exists()
    source=''.join('# webagent-step: '+sid+'\n'+text+'\n\n' for sid,text in code.items())
    assert (path/'generated_test.py').read_text()==source
    assert json.loads((path/'test_contract.json').read_text())['oracles'][0]['expected']=='All fields'
    if not repeat:
        delivery=json.loads((path/'agent3_delivery.json').read_text())
        assert delivery['script_sha256']==hashlib.sha256(source.encode()).hexdigest()
        assert delivery['attempts']=={'S1':2,'S2':1,'S3':1} and delivery['repairs_used']==1
        assert all(e[2]==source for e in executed if e[1]=='full_replay')


def test_binding_only_model_cannot_return_replacement_code():
    class Model:
        def complete_json(self,*args): return dict(code='driver.title',bindings={})
    with pytest.raises(ValueError,match='never code or expectations'):
        Agent3SeleniumGenerator().repair_bindings({'oracles':[]},{'id':'S1'},{},llm=Model(),
            previous={'code':'driver.title','bindings':{}},error={})


def test_review_payload_is_bounded_and_excludes_unneeded_checkpoints():
    class Model:
        def complete_json(self,system,user):
            data=json.loads(user)
            assert len(user)<40000
            assert set(data['untrusted_step_checkpoints'])=={'S1'}
            page=data['untrusted_step_checkpoints']['S1']
            assert page['truncated'] and 'live_elements' not in page
            assert page['elements'][0]['id']=='field' and page['elements'][0]['parent']['class']=='form'
            return dict(approved=True,issues=[])
    snapshot=dict(html_summary='x'*16000,live_elements=['duplicate']*1000,elements=[dict(
        id='field',tag='select',text='All fields'*100,parent=dict(tag='form',**{'class':'form','text':'long'*500}))]*80)
    before=json.dumps(snapshot)
    Agent2TestReviewer().review_implementation({'oracles':[dict(id='O1',step_id='S1',kind='equals',expected='All fields')]},
        [],{'O1':dict(read='selected_text')},observation=snapshot,
        checkpoints={'S1':snapshot,'S2':snapshot},llm=Model())
    assert json.dumps(snapshot)==before


def test_diagnostic_url_preserves_search_state_but_redacts_secrets():
    url='https://u:p@authorized.test/search/?terms-0-term=machine+learning&terms-0-field=all&token=synthetic-secret&custom=private'
    safe=diagnostic_url(url)
    assert 'terms-0-term=machine+learning' in safe and 'terms-0-field=all' in safe
    assert all(s not in safe for s in ['u:p@','synthetic-secret','private'])
    assert diagnostic_url(url)!=diagnostic_url(url.replace('machine+learning','quantum'))
    assert diagnostic_url(url)!=diagnostic_url(url.replace('private','other'))
    assert '?' not in safe_href(url)  # link summaries stay minimal
    assert diagnostic_url('https://authorized.test/#/active').endswith('#/active')


def test_actual_actions_include_navigation_and_failure_without_input_values():
    class Node(Select):
        def send_keys(self,*args): pass
        def click(self): driver.current_url='https://authorized.test/search/?query=quantum'
        def clear(self): raise RuntimeError('synthetic control failure')
    driver=SimpleNamespace(current_url='https://authorized.test/',find_element=lambda *a:Node(),
        current_window_handle='source',window_handles=['source'],
        execute_script=lambda *a:dict(href='',target=''))
    actions,events=[],[]
    with pytest.raises(RuntimeError):
        execute_source('x=driver.find_element(By.ID,"choice")\nx.send_keys("synthetic-input")\nx.click()\nx.clear()',
            driver,'https://authorized.test/',actions=actions,on_action=events.append)
    assert [a['action'] for a in actions]==['send_keys','click','clear']
    assert actions[1]['navigated'] and 'query=quantum' in actions[1]['after_url']
    assert actions[2]['status']=='error' and actions[2]['error_type']=='RuntimeError'
    assert len(events)==6 and 'synthetic-input' not in str(events)
