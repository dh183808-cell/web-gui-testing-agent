"""UI lifecycle/configuration tests. Real A/B/C acceptance is recorded separately."""
import json
from pathlib import Path
import threading
import time
import uuid

import pytest

from web_agent import ui_tasks as ui


class StubLLM:
    def __init__(self, **kwargs): self.settings = kwargs
    def close(self): pass


AI = dict(provider='deepseek', model='test-model')
SECRET = 'synthetic-ui-secret'


def wait_finished(store, key):
    deadline = time.monotonic()+3
    while time.monotonic()<deadline:
        job=store.snapshot(key)
        if job['status']!='RUNNING' and 'duration_seconds' in job: return job
        threading.Event().wait(.01)
    pytest.fail('background test did not complete')


def test_store_deduplicates_clicks_and_refresh_and_uses_phase_policy(tmp_path):
    release=threading.Event(); entered=threading.Event(); calls=[]
    def runner(**kw):
        calls.append(kw)
        kw['progress_callback'](dict(agent='Agent1',action='started',elapsed_seconds=.1))
        entered.set()
        assert release.wait(3)
        return dict(run_dir=str(tmp_path),report=str(tmp_path/'report.md'),verdict=dict(status='PASS'))
    store=ui.TaskStore(tmp_path/'jobs',runner=runner,llm_factory=StubLLM)
    inputs=next(iter(ui.DEMO_PRESETS.values()));key=uuid.uuid4().hex
    try:
        assert store.start(inputs,request_id=key,ai_config=AI,api_key=SECRET)==key
        assert entered.wait(2)
        assert store.start(inputs,request_id=key)==key
        assert store.start(inputs,request_id=uuid.uuid4().hex)==key
        assert store.snapshot(key)['events'][0]['agent']=='Agent1'
    finally: release.set()
    job=wait_finished(store,key)
    assert len(calls)==1 and job['status']=='FINISHED'
    assert calls[0]['headless'] is True and calls[0]['formal_headless'] is False
    assert calls[0]['requirement']==inputs['requirement']
    assert store.start(inputs,request_id=key)==key and len(calls)==1
    reloaded=ui.TaskStore(tmp_path/'jobs',runner=lambda **kw:pytest.fail('must not restart'))
    assert reloaded.start(inputs,request_id=key)==key


def test_restart_marks_active_job_interrupted_without_running(tmp_path):
    key=uuid.uuid4().hex
    (tmp_path/(key+'.json')).write_text(json.dumps(dict(id=key,status='RUNNING',started_at=time.time(),events=[])),encoding='utf-8')
    store=ui.TaskStore(tmp_path,runner=lambda **kw:pytest.fail('must not restart'))
    assert store.snapshot(key)['status']=='INTERRUPTED' and store.active() is None


def test_task_exception_does_not_publish_sdk_message(tmp_path):
    def runner(**kw): raise RuntimeError('synthetic credential must not appear')
    store=ui.TaskStore(tmp_path,runner=runner,llm_factory=StubLLM);key=uuid.uuid4().hex
    store.start(next(iter(ui.DEMO_PRESETS.values())),request_id=key,ai_config=AI,api_key=SECRET)
    assert wait_finished(store,key)['error_type']=='RuntimeError'
    assert 'synthetic credential' not in (tmp_path/(key+'.json')).read_text(encoding='utf-8')


def test_final_cleanup_verdict_wins_over_returned_business_verdict(tmp_path):
    (tmp_path/'final_verdict.json').write_text('{"status":"ERROR"}',encoding='utf-8')
    def runner(**kw): return dict(run_dir=str(tmp_path),verdict=dict(status='PASS'))
    store=ui.TaskStore(tmp_path/'jobs',runner=runner,llm_factory=StubLLM);key=uuid.uuid4().hex
    store.start(next(iter(ui.DEMO_PRESETS.values())),request_id=key,ai_config=AI,api_key=SECRET)
    assert wait_finished(store,key)['result']['verdict']['status']=='ERROR'


def test_progress_is_event_based_and_does_not_invent_agent6():
    job=dict(status='FINISHED',started_at=time.time(),duration_seconds=9,events=[
        dict(agent='Agent3',action='generate_step',step_id='S2',elapsed_seconds=1),
        dict(agent='LLM',action='call_started',elapsed_seconds=2),
        dict(agent='LLM',action='call_finished',status='COMPLETE',elapsed_seconds=3),
        dict(agent='Agent3',action='repair_from_actual_error',repairs=1,elapsed_seconds=4),
        dict(agent='Agent3',action='cold_trial_finished',status='COMPLETE',elapsed_seconds=8),
    ])
    view=ui.progress_view(job)
    assert view['calls']==view['calls_finished']==1 and view['repairs']==1
    assert view['stages']['Agent6']['status']=='未调用'
    assert view['stages']['Agent3']['status']=='已完成' and view['stages']['Agent3']['seconds']==7


def test_evidence_paths_cannot_escape_run(tmp_path,monkeypatch):
    monkeypatch.setattr(ui,'ROOT',tmp_path)
    run=tmp_path/'artifacts'/'run';run.mkdir(parents=True)
    formal=run/'formal';formal.mkdir();shot=formal/'shot.png';shot.write_bytes(b'image')
    outside=tmp_path/'outside.txt';outside.write_text('synthetic',encoding='utf-8')
    assert ui.artifact_path(run,'formal/shot.png')==shot
    assert ui.artifact_path(run,str(shot))==shot
    assert ui.artifact_path(run,'../../outside.txt') is None
    assert ui.artifact_path(run,str(outside)) is None


def test_presets_match_accepted_v05_inputs():
    names=['dynamic_20261108_212444_830dde','dynamic_20261108_212648_05ee75','dynamic_20261108_213003_ef5106']
    for preset,name in zip(ui.DEMO_PRESETS.values(),names):
        path=ui.ROOT/'artifacts'/name/'test_inputs.json'
        if path.exists():
            original=ui.read_json(path)
            assert {k:preset[k] for k in original}==original


def test_frontend_user_configuration_and_no_presets_or_browser_switch(tmp_path,monkeypatch):
    from streamlit.testing.v1 import AppTest
    import streamlit as st
    store=ui.TaskStore(tmp_path)
    monkeypatch.setattr(ui,'TaskStore',lambda:store)
    st.cache_resource.clear()
    try:
        app=AppTest.from_file(str(ui.ROOT/'app_ui.py'),default_timeout=10).run()
        assert not app.exception
        assert not any(x.key=='preset' for x in app.selectbox)
        assert app.text_area(key='test_basis').value==''
        assert app.text_input(key='api_key').value==''
        app.selectbox(key='ai_provider').select('siliconflow').run()
        assert app.text_input(key='base_url').value=='https://api.siliconflow.cn/v1'
        app.text_input(key='model').set_value('custom/model').run()
        app.text_input(key='url').set_value(ui.FORM_URL).run()
        app.text_input(key='api_key').set_value(SECRET).run()
        app.text_area(key='requirement').set_value('editable input').run()
        assert app.text_area(key='requirement').value=='editable input'
        assert len(app.checkbox)==1 and app.button(key='start_test').disabled
        app.checkbox[0].check().run()
        assert not app.button(key='start_test').disabled
        assert app.slider(key='formal_demo_delay').value==1.5
    finally: st.cache_resource.clear()


def test_frontend_passes_current_credentials_then_clears_password(tmp_path,monkeypatch):
    from streamlit.testing.v1 import AppTest
    import streamlit as st
    clients=[]
    class Client(StubLLM):
        def __init__(self, **kw): super().__init__(**kw);clients.append(self)
    store=ui.TaskStore(tmp_path,runner=lambda **kw:dict(run_dir=str(tmp_path),verdict=dict(status='PASS',checks=[])),
                       llm_factory=Client)
    monkeypatch.setattr(ui,'TaskStore',lambda:store);st.cache_resource.clear()
    try:
        app=AppTest.from_file(str(ui.ROOT/'app_ui.py'),default_timeout=10).run()
        app.selectbox(key='ai_provider').select('openrouter').run()
        app.text_input(key='model').set_value('chosen/model').run()
        app.text_input(key='api_key').set_value(SECRET).run()
        app.text_input(key='url').set_value(ui.FORM_URL).run()
        app.text_area(key='requirement').set_value('Check feedback').run()
        app.checkbox(key='authorized').check().run()
        app.button(key='start_test').click().run()
        assert not app.exception
        assert clients and clients[0].settings['api_key']==SECRET
        assert clients[0].settings['provider']=='openrouter' and clients[0].settings['use_environment'] is False
        assert 'api_key' not in app.session_state
        assert len(store.jobs)==1
        app.run()
        assert len(store.jobs)==1
        for p in tmp_path.glob('*.json'): assert SECRET not in p.read_text(encoding='utf-8')
    finally:st.cache_resource.clear()


@pytest.mark.parametrize('verdict',['PASS','FAIL','ERROR','INCONCLUSIVE'])
def test_frontend_renders_completed_four_state_result(tmp_path,monkeypatch,verdict):
    from streamlit.testing.v1 import AppTest
    import streamlit as st
    store=ui.TaskStore(tmp_path)
    key=uuid.uuid4().hex
    store.jobs[key]=dict(id=key,inputs=next(iter(ui.DEMO_PRESETS.values())),status='FINISHED',
        started_at=time.time(),duration_seconds=3,events=[],result=dict(run_dir=str(tmp_path),
            verdict=dict(status=verdict,message='test verdict',checks=[])))
    monkeypatch.setattr(ui,'TaskStore',lambda:store)
    st.cache_resource.clear()
    try:
        app=AppTest.from_file(str(ui.ROOT/'app_ui.py'),default_timeout=10)
        app.query_params['job']=key
        app.run()
        assert not app.exception
        assert not app.text_input and not app.text_area
        assert any(verdict in item.value for item in app.markdown)
        app.button(key='new_task').click().run()
        assert not app.exception and app.query_params.get('job') is None
        assert app.button(key='start_test').disabled
    finally: st.cache_resource.clear()


def test_model_wait_clock_and_stage_intervals_are_derived_from_events():
    job=dict(status='RUNNING',started_at=100,events=[
        dict(agent='Agent1',action='started',elapsed_seconds=0),
        dict(agent='LLM',action='call_started',stage='Agent1',elapsed_seconds=2),
    ])
    early=ui.progress_view(job,now=110);later=ui.progress_view(job,now=117)
    assert early['pending_seconds']==8 and later['pending_seconds']==15
    assert later['calls']==1 and later['calls_finished']==0
    assert later['stages']['Agent1']['status']=='执行中'
    assert later['stages']['Agent2']['status']=='等待中'
    assert later['stages']['Agent1']['seconds']==17
    job['events'] += [dict(agent='LLM',action='call_finished',status='COMPLETE',elapsed_seconds=18),
        dict(agent='Agent1',action='semantic_plan_created',steps=2,elapsed_seconds=19),
        dict(agent='Agent2',action='started',elapsed_seconds=20)]
    view=ui.progress_view(job,now=125)
    assert view['pending_seconds'] is None and view['current']=='Agent2'
    assert view['stages']['Agent1']['seconds']==19 and view['stages']['Agent2']['seconds']==5


def test_formal_browser_notice_tracks_real_session_and_steps():
    job=dict(status='RUNNING',started_at=100,events=[
        dict(agent='Agent4',action='started',elapsed_seconds=1),
        dict(agent='Formal',action='started',elapsed_seconds=1),
        dict(agent='Runtime',action='session_created',purpose='formal',elapsed_seconds=3),
        dict(agent='Runtime',action='step_started',purpose='formal',step_id='S4',elapsed_seconds=4),
    ])
    view=ui.progress_view(job,now=106)
    assert view['formal_open'] and view['step']=='S4' and view['current']=='Formal'
    assert view['stages']['Agent4']['status']==view['stages']['Formal']['status']=='执行中'
    assert '正式浏览器已启动' in ui.activity_text(job['events'][2])
    job['events'].append(dict(agent='Runtime',action='session_closed',reason='formal_replay_finished',elapsed_seconds=7))
    assert not ui.progress_view(job,now=108)['formal_open']


def result_fixture(tmp_path,monkeypatch):
    monkeypatch.setattr(ui,'ROOT',tmp_path)
    run=tmp_path/'artifacts'/'run';formal=run/'formal'/'session';formal.mkdir(parents=True)
    trial=run/'trial';trial.mkdir();(trial/'trial.png').write_bytes(b'trial')
    # A valid small PNG, sufficient for Streamlit's real image renderer.
    import base64
    png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
    (formal/'pass.png').write_bytes(png);(formal/'fail.png').write_bytes(png)
    contract=dict(oracles=[dict(id='O1',step_id='S1',meaning='input value',expected='WebAgent Test',source_type='user_requirement',source_quote='input WebAgent Test'),
        dict(id='O2',step_id='S2',meaning='feedback',expected='Accepted!',source_type='test_basis',source_quote='should display Accepted!'),
        dict(id='O3',step_id='S3',meaning='dependent check',expected='',source_type='test_basis',source_quote='after missing action')])
    evidence=dict(purpose='formal',formal_run_dir=str(formal),measurements={
        'O1':dict(step_id='S1',value='WebAgent Test',status='observed',screenshot_ref='pass.png'),
        'O2':dict(step_id='S2',value='Received!',status='observed',screenshot_ref='fail.png')},
        steps=[dict(step_id='S1',status='COMPLETE',screenshot_ref='pass.png'),dict(step_id='S2',status='COMPLETE',screenshot_ref='fail.png')])
    verdict=dict(status='FAIL',checks=[dict(oracle_id='O1',actual='ignore cached value',passed=True),
        dict(oracle_id='O2',actual='ignore cached value',passed=False),
        dict(oracle_id='O3',passed=None,evidence_status='not_executed',blocked_by='O2')],
        skipped_steps=[dict(step_id='S3',blocked_by='O2',status='NOT_EXECUTED',reason='dependent')])
    for name,data in [('test_contract.json',contract),('formal_evidence.json',evidence)]:
        (run/name).write_text(json.dumps(data),encoding='utf-8')
    job=dict(id=uuid.uuid4().hex,status='FINISHED',started_at=time.time(),duration_seconds=4,events=[],
        inputs=next(iter(ui.DEMO_PRESETS.values())),result=dict(run_dir=str(run),verdict=verdict))
    return job,run,evidence


def test_results_prioritize_failures_and_use_formal_values_not_trial_or_cached_verdict(tmp_path,monkeypatch):
    job,run,evidence=result_fixture(tmp_path,monkeypatch)
    view=ui.result_view(job)
    assert view['focus']['oracle_id']=='O2' and view['focus']['actual']=='Received!'
    assert view['focus']['source_quote']=='should display Accepted!'
    assert view['focus']['screenshot']==run/'formal'/'session'/'fail.png'
    assert view['rows'][2]['status']=='NOT_EXECUTED' and not view['rows'][2]['actual_available']
    evidence['measurements']['O2']['screenshot_ref']='../../trial/trial.png'
    (run/'formal_evidence.json').write_text(json.dumps(evidence),encoding='utf-8')
    assert ui.result_view(job)['focus']['screenshot'] is None
    evidence['measurements']['O2']['screenshot_ref']='fail.png'
    (run/'formal_evidence.json').write_text(json.dumps(evidence),encoding='utf-8')
    job['result']['verdict']['checks'][1]['passed']=True
    assert ui.result_view(job)['focus']['oracle_id']=='O2'  # Final successful value, not presence check.
    evidence['purpose']='trial'
    (run/'formal_evidence.json').write_text(json.dumps(evidence),encoding='utf-8')
    view=ui.result_view(job)
    assert not view['formal_valid'] and all(r['actual'] is None and r['screenshot'] is None for r in view['rows'])


def test_history_listing_and_switching_is_read_only(tmp_path,monkeypatch):
    from streamlit.testing.v1 import AppTest
    import streamlit as st
    app_path=ui.ROOT/'app_ui.py'
    job,run,evidence=result_fixture(tmp_path,monkeypatch)
    store=ui.TaskStore(tmp_path/'jobs',runner=lambda **kw:pytest.fail('history must not execute'))
    other=dict(job,id=uuid.uuid4().hex,started_at=job['started_at']-10)
    store.jobs={job['id']:job,other['id']:other}
    for value in store.jobs.values():store._save(value)
    before={p.name:p.read_bytes() for p in store.root.glob('*.json')}
    assert [x['id'] for x in store.recent()]==[job['id'],other['id']]
    monkeypatch.setattr(ui,'TaskStore',lambda:store);st.cache_resource.clear()
    try:
        app=AppTest.from_file(str(app_path),default_timeout=10)
        app.query_params['job']=job['id'];app.run()
        assert not app.exception
        assert app.selectbox(key='oracle_'+job['id']).value=='O2'
        assert not app.text_area and not app.text_input
        app.selectbox(key='oracle_'+job['id']).select('O1').run()
        app.radio(key='evidence_mode_'+job['id']).set_value('按执行步骤').run()
        assert app.selectbox(key='step_'+job['id']).value=='S1'
        app.selectbox(key='history_job').select(other['id']).run()
        assert not app.exception and app.query_params['job']==other['id']
        assert app.selectbox(key='oracle_'+other['id']).value=='O2'
        assert all(p.read_bytes()==before[p.name] for p in store.root.glob('*.json'))
    finally:st.cache_resource.clear()


def test_running_frontend_collapses_inputs_and_shows_real_wait(tmp_path,monkeypatch):
    from streamlit.testing.v1 import AppTest
    import streamlit as st
    store=ui.TaskStore(tmp_path)
    key=uuid.uuid4().hex
    store.jobs[key]=dict(id=key,status='RUNNING',started_at=time.time()-8,
        inputs=next(iter(ui.DEMO_PRESETS.values())),events=[dict(agent='Agent1',action='started',elapsed_seconds=0),
        dict(agent='LLM',action='call_started',stage='Agent1',provider='deepseek',elapsed_seconds=1)])
    monkeypatch.setattr(ui,'TaskStore',lambda:store);st.cache_resource.clear()
    try:
        app=AppTest.from_file(str(ui.ROOT/'app_ui.py'),default_timeout=10)
        app.query_params['job']=key;app.run()
        assert not app.exception and not app.text_area and not app.text_input
        assert app.button(key='new_task').disabled
        assert any('已等待' in item.value for item in app.info)
        assert any('Agent1' in item.value and '正在执行' in item.value for item in app.subheader)
        assert any(item.label=='本次测试配置 · 只读' and not item.proto.expanded for item in app.expander)
        cards=[m.value for m in app.markdown if '<div class="stage' in m.value]
        assert len(cards)==6 and sum('正式浏览器测试与取证' in x for x in cards)==1
        assert not any('Agent6' in x for x in cards)
    finally:st.cache_resource.clear()


def test_history_remains_read_only_while_another_task_runs(tmp_path,monkeypatch):
    from streamlit.testing.v1 import AppTest
    import streamlit as st
    store=ui.TaskStore(tmp_path,runner=lambda **kw:pytest.fail('viewing must not execute'))
    active=uuid.uuid4().hex;history=uuid.uuid4().hex
    store.jobs[active]=dict(id=active,status='RUNNING',started_at=time.time(),events=[],inputs=next(iter(ui.DEMO_PRESETS.values())))
    store.jobs[history]=dict(id=history,status='FINISHED',started_at=time.time()-10,duration_seconds=3,
        events=[],inputs=next(iter(ui.DEMO_PRESETS.values())),result=dict(run_dir=str(tmp_path),verdict=dict(status='PASS',checks=[])))
    monkeypatch.setattr(ui,'TaskStore',lambda:store);st.cache_resource.clear()
    try:
        app=AppTest.from_file(str(ui.ROOT/'app_ui.py'),default_timeout=10)
        app.query_params['job']=history;app.run()
        assert not app.exception and app.button(key='new_task').disabled
        assert not app.text_input and not app.text_area and store.active()==active
        app.button(key='return_active').click().run()
        assert not app.exception and app.query_params['job']==active
        assert store.active()==active and len(store.jobs)==2
    finally:st.cache_resource.clear()


def rejected_plan_job(run_dir):
    """Offline example matching two normal reviews followed by a planning stop."""
    return dict(id=uuid.uuid4().hex,status='FINISHED',started_at=100,duration_seconds=8,
        inputs=next(iter(ui.DEMO_PRESETS.values())),events=[
            dict(agent='Agent2',action='started',elapsed_seconds=1),
            dict(agent='Agent2',action='review_finished',approved=False,elapsed_seconds=2),
            dict(agent='Agent1',action='started',elapsed_seconds=3),
            dict(agent='Agent1',action='semantic_plan_created',elapsed_seconds=4),
            dict(agent='Agent2',action='started',elapsed_seconds=5),
            dict(agent='Agent2',action='review_finished',approved=False,elapsed_seconds=6),
            dict(agent='Agent2',action='input_not_ready',elapsed_seconds=7),
            dict(agent='Pipeline',action='run_finished',status='INCONCLUSIVE',elapsed_seconds=8)],
        result=dict(run_dir=str(run_dir),verdict=dict(status='INCONCLUSIVE',checks=[],
            stopped_stage='Agent2',reason_type='contract_unapproved',execution_started=False)))


def test_unapproved_plan_result_and_history_never_claim_browser_or_formal_evidence(tmp_path,monkeypatch):
    from copy import deepcopy
    from streamlit.testing.v1 import AppTest
    import streamlit as st
    app_path=ui.ROOT/'app_ui.py'
    monkeypatch.setattr(ui,'ROOT',tmp_path)
    run=tmp_path/'artifacts'/'unapproved';run.mkdir(parents=True)
    job=rejected_plan_job(run)
    (run/'final_verdict.json').write_text(json.dumps(job['result']['verdict']),encoding='utf-8')
    store=ui.TaskStore(tmp_path/'jobs',runner=lambda **kw:pytest.fail('history must not execute'))
    other=dict(deepcopy(job),id=uuid.uuid4().hex,started_at=90)
    store.jobs={job['id']:job,other['id']:other}
    for value in store.jobs.values():store._save(value)
    before={p:p.read_bytes() for p in tmp_path.rglob('*.json')}
    original=deepcopy(job)
    view=ui.result_view(job)
    assert view['execution_summary']=='测试规划未通过，尚未执行浏览器。'
    assert not view['formal_valid'] and not view['rows'] and not view['steps']
    assert ui.progress_view(job)['stages']['Agent2']['status']=='审核未通过'
    assert job==original
    monkeypatch.setattr(ui,'TaskStore',lambda:store);st.cache_resource.clear()
    try:
        app=AppTest.from_file(str(app_path),default_timeout=10)
        app.query_params['job']=job['id'];app.run()
        for _ in range(2):
            assert not app.exception
            assert any('测试规划未通过，尚未执行浏览器。' in e.value for e in app.info)
            assert not any('独立正式证据' in e.value or '真实模型与正式证据' in e.value for e in app.caption)
            card=next(m.value for m in app.markdown if '<div class="stage' in m.value and 'Agent2' in m.value)
            assert '审核未通过' in card and '异常' not in card
            assert any('INCONCLUSIVE' in m.value for m in app.markdown)
            assert not app.text_input and not app.text_area and not app.image
            app.run()  # A refresh is read-only too.
            app.selectbox(key='history_job').select(other['id']).run()
        assert all(p.read_bytes()==data for p,data in before.items())
        assert store.active() is None
    finally:st.cache_resource.clear()


@pytest.mark.parametrize('action',['review_finished','blockage_reviewed'])
def test_normal_review_rejection_can_later_complete_without_stale_error(action):
    job=dict(status='RUNNING',started_at=100,events=[
        dict(agent='Agent2',action='started',elapsed_seconds=0),
        dict(agent='Agent2',action=action,approved=False,elapsed_seconds=1)])
    assert ui.progress_view(job,now=102)['stages']['Agent2']['status']=='审核未通过'
    job['events'] += [dict(agent='Agent1',action='started',elapsed_seconds=2),
        dict(agent='Agent1',action='semantic_plan_created',elapsed_seconds=3),
        dict(agent='Agent2',action='started',elapsed_seconds=4),
        dict(agent='Agent2',action=action,approved=True,elapsed_seconds=5)]
    job.update(status='FINISHED',duration_seconds=6)
    view=ui.progress_view(job)
    assert view['stages']['Agent2']['status']=='已完成'
    assert view['stages']['Agent2']['seconds']==2
    assert view['calls_failed']==0


@pytest.mark.parametrize('failure', ['llm','diagnosed','both'])
def test_agent2_technical_errors_remain_errors_after_normal_review_history(failure):
    job=dict(status='FINISHED',started_at=100,duration_seconds=7,events=[
        dict(agent='Agent2',action='started',elapsed_seconds=0),
        dict(agent='Agent2',action='review_finished',approved=False,elapsed_seconds=1),
        dict(agent='Agent2',action='started',elapsed_seconds=2)])
    if failure in {'llm','both'}:
        job['events'] += [dict(agent='LLM',action='call_started',stage='Agent2',elapsed_seconds=3),
            dict(agent='LLM',action='call_finished',status='ERROR',elapsed_seconds=4)]
    if failure in {'diagnosed','both'}:
        job['events'].append(dict(agent='Agent6',action='diagnosed_stop',stage='Agent2Implementation',elapsed_seconds=5))
    view=ui.progress_view(job)
    assert view['stages']['Agent2']['status']=='异常'
    assert view['calls_failed']==int(failure in {'llm','both'})


@pytest.mark.parametrize('verdict,events,message', [
    (dict(execution_started=False,reason_type='conflict'),[], '测试输入存在冲突，尚未执行浏览器。'),
    (dict(execution_started=False,reason_type='unclear'),[], '测试预期尚不明确，尚未执行浏览器。'),
    (dict(execution_started=False,reason_type='planning_only'),[], '仅完成测试规划，尚未执行浏览器。'),
    (dict(execution_started=True,reason_type='contract_unapproved'),[], '已进入浏览器执行，尚未形成可用的正式证据。'),
    ({},[dict(agent='Runtime',action='session_created',purpose='trial')], '已进入浏览器执行，尚未形成可用的正式证据。'),
    ({},[dict(agent='Agent4',action='started')], '已进入正式执行，正式证据尚不完整。'),
    ({},[dict(agent='Runtime',action='session_created',purpose='formal')], '已进入正式执行，正式证据尚不完整。'),
    ({},[], '未发现浏览器执行或正式取证记录。'),
])
def test_other_early_stops_describe_actual_execution_without_reclassifying_verdict(tmp_path,monkeypatch,verdict,events,message):
    from copy import deepcopy
    monkeypatch.setattr(ui,'ROOT',tmp_path)
    job=rejected_plan_job(tmp_path/'artifacts'/'missing')
    job['events']=events
    job['result']['verdict']=dict(status='INCONCLUSIVE',checks=[],**verdict)
    original=deepcopy(job)
    view=ui.result_view(job)
    assert view['execution_summary']==message
    assert not view['formal_valid'] and not view['rows']
    assert job==original


def test_formal_file_without_measurements_or_screenshots_is_not_claimed_as_collected_evidence(tmp_path,monkeypatch):
    job,run,evidence=result_fixture(tmp_path,monkeypatch)
    evidence.update(measurements={},steps=[])
    (run/'formal_evidence.json').write_text(json.dumps(evidence),encoding='utf-8')
    job['result']['verdict']=dict(status='INCONCLUSIVE',checks=[])
    view=ui.result_view(job)
    assert view['formal_valid']
    assert view['execution_summary']=='已进入正式执行，正式证据尚不完整。'
    assert view['verdict']['status']=='INCONCLUSIVE'
    assert all(not r['actual_available'] and r['screenshot'] is None for r in view['rows'])


@pytest.mark.parametrize('status', ['PASS','FAIL'])
def test_formal_result_keeps_comparison_screenshot_code_and_report(tmp_path,monkeypatch,status):
    from copy import deepcopy
    from streamlit.testing.v1 import AppTest
    import streamlit as st
    app_path=ui.ROOT/'app_ui.py'
    job,run,evidence=result_fixture(tmp_path,monkeypatch)
    contract=ui.read_json(run/'test_contract.json')
    contract['oracles']=contract['oracles'][:2]
    contract['oracles'][1]['expected']='Received!' if status=='PASS' else 'Accepted!'
    (run/'test_contract.json').write_text(json.dumps(contract),encoding='utf-8')
    verdict=job['result']['verdict']
    verdict.update(status=status,checks=verdict['checks'][:2],skipped_steps=[])
    verdict['checks'][1]['passed']=status=='PASS'
    (run/'generated_test.py').write_text('# frozen test code\n',encoding='utf-8')
    (run/'report.md').write_text('saved report',encoding='utf-8')
    original=deepcopy(job)
    view=ui.result_view(job)
    assert view['formal_valid'] and view['execution_summary']=='已采集独立正式证据。'
    assert view['focus']['actual']=='Received!' and view['focus']['expected']==contract['oracles'][1]['expected']
    assert view['focus']['status']==status and view['focus']['screenshot']==run/'formal'/'session'/'fail.png'
    store=ui.TaskStore(tmp_path/'jobs',runner=lambda **kw:pytest.fail('saved result must not execute'))
    store.jobs[job['id']]=job
    monkeypatch.setattr(ui,'TaskStore',lambda:store);st.cache_resource.clear()
    try:
        app=AppTest.from_file(str(app_path),default_timeout=10)
        app.query_params['job']=job['id'];app.run()
        assert not app.exception
        assert any('独立正式证据' in e.value for e in app.caption)
        assert app.selectbox(key='oracle_'+job['id']).value==view['focus']['oracle_id']
        assert len(app.image)==1
        assert any('frozen test code' in e.value for e in app.code)
        assert any('saved report' in e.value for e in app.text)
        downloads=[e.proto.label for e in app.get('download_button')]
        assert all(label in downloads for label in ('下载当前正式截图','下载生成代码','下载报告'))
        assert job==original
    finally:st.cache_resource.clear()
