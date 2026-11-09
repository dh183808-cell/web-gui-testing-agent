"""UI lifecycle/configuration tests. Real A/B/C acceptance is recorded separately."""
import json
from pathlib import Path
import threading
import time
import uuid

import pytest

from web_agent import ui_tasks as ui


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
        calls.append(kw);entered.set()
        kw['progress_callback'](dict(agent='Agent1',action='started',elapsed_seconds=.1))
        assert release.wait(3)
        return dict(run_dir=str(tmp_path),report=str(tmp_path/'report.md'),verdict=dict(status='PASS'))
    store=ui.TaskStore(tmp_path/'jobs',runner=runner)
    inputs=next(iter(ui.DEMO_PRESETS.values()));key=uuid.uuid4().hex
    try:
        assert store.start(inputs,request_id=key)==key
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
    store=ui.TaskStore(tmp_path,runner=runner);key=uuid.uuid4().hex
    store.start(next(iter(ui.DEMO_PRESETS.values())),request_id=key)
    assert wait_finished(store,key)['error_type']=='RuntimeError'
    assert 'synthetic credential' not in (tmp_path/(key+'.json')).read_text(encoding='utf-8')


def test_final_cleanup_verdict_wins_over_returned_business_verdict(tmp_path):
    (tmp_path/'final_verdict.json').write_text('{"status":"ERROR"}',encoding='utf-8')
    def runner(**kw): return dict(run_dir=str(tmp_path),verdict=dict(status='PASS'))
    store=ui.TaskStore(tmp_path/'jobs',runner=runner);key=uuid.uuid4().hex
    store.start(next(iter(ui.DEMO_PRESETS.values())),request_id=key)
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


def test_frontend_presets_are_editable_and_no_browser_switch(tmp_path,monkeypatch):
    from streamlit.testing.v1 import AppTest
    import streamlit as st
    store=ui.TaskStore(tmp_path)
    monkeypatch.setattr(ui,'TaskStore',lambda:store)
    st.cache_resource.clear()
    try:
        app=AppTest.from_file(str(ui.ROOT/'app_ui.py'),default_timeout=10).run()
        assert not app.exception
        app.selectbox(key='preset').select(list(ui.DEMO_PRESETS)[1]).run()
        assert app.text_area(key='test_basis').value==list(ui.DEMO_PRESETS.values())[1]['test_basis']
        app.text_area(key='requirement').set_value('editable input').run()
        assert app.text_area(key='requirement').value=='editable input'
        assert len(app.checkbox)==1 and app.button(key='start_test').disabled
        app.checkbox[0].check().run()
        assert not app.button(key='start_test').disabled
    finally: st.cache_resource.clear()


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
