"""Offline presentation/control tests. Scripted models do not validate real LLM ability."""
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import threading
import time
import uuid

import pytest

from web_agent import desktop_layout as layout
from web_agent.demo_console import DemoConsole, format_event, safe_display


@pytest.mark.parametrize('work', [dict(x=0,y=0,width=1920,height=1040),
    dict(x=-2560,y=100,width=2560,height=1400),dict(x=0,y=0,width=1537,height=824)])
def test_split_physical_work_area_without_resolution_or_origin_assumptions(work):
    regions = layout.split_work_area(work)
    left, right = regions['left'], regions['right']
    assert left['width']+right['width'] == work['width']
    assert right['x'] == work['x']+left['width']
    assert left['height'] == right['height'] == work['height']
    assert abs(left['width']-right['width']) <= 1


@pytest.mark.parametrize('dpi', [96,120,144])
def test_windows_physical_pixel_contract_is_not_scaled_twice(dpi,monkeypatch):
    # Native APIs run in a per-monitor-aware thread; recorded DPI is diagnostic only.
    monkeypatch.setattr(layout,'window_info',lambda hwnd: dict(visible=True,dpi=dpi))
    config = layout.freeze_presentation('demo',dict(terminal_hwnd=11,frontend_hwnd=12,
        **layout.split_work_area(dict(x=-1920,y=0,width=1920,height=1040))))
    assert config['right'] == dict(x=-960,y=0,width=960,height=1040)
    assert config['desktop_ready']


def test_normal_never_touches_desktop_or_inherits_demo_layout(monkeypatch):
    monkeypatch.setattr(layout,'monitors',lambda: pytest.fail('Normal must not inspect desktop'))
    assert layout.freeze_presentation() == {'mode':'normal'}
    monkeypatch.setenv('WEBAGENT_DEMO_LAYOUT','invalid')
    assert layout.freeze_presentation('normal') == {'mode':'normal'}


def test_demo_without_launcher_uses_existing_terminal_and_primary_right_half(monkeypatch):
    monkeypatch.delenv('WEBAGENT_DEMO_LAYOUT',raising=False)
    monkeypatch.setattr(layout,'monitors',lambda:[dict(device='primary',
        work=dict(x=0,y=0,width=1920,height=1040))])
    monkeypatch.setattr(layout,'place_window',lambda *a:pytest.fail('Do not move the user terminal/frontend'))
    config=layout.freeze_presentation('demo')
    assert config['terminal_output']=='streamlit_stdout'
    assert config['right']==dict(x=960,y=0,width=960,height=1040)
    assert 'warning' not in config
    assert not config['desktop_ready']  # Automated whole-desktop layout is optional.


@pytest.mark.parametrize('mode',['normal','demo'])
def test_task_logs_to_launching_streamlit_stdout_without_spawning_console(mode,tmp_path,monkeypatch):
    from web_agent.ui_tasks import TaskStore
    import subprocess
    output=io.StringIO();closed=threading.Event()
    monkeypatch.setattr(sys,'stdout',output)
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**kw:pytest.fail('Do not spawn a console'))
    class Client:
        def __init__(self,**kw): pass
        def close(self): closed.set()
    def runner(**kw):
        print('LLM: call_started',flush=True)
        for record in [dict(agent='Agent1',action='semantic_plan_created',plan_steps=[dict(id='S1',goal='Fill demo')]),
            dict(agent='Agent2',action='review_finished',approved=False,issues=['Check source']),
            dict(agent='Agent3',action='code_generated',code='driver.title'),
            dict(agent='Runtime',action='browser_action',purpose='trial',operation='click'),
            dict(agent='Cold',action='finished',status='COMPLETE'),
            dict(agent='Agent4',action='started'),
            dict(agent='Agent5',action='check_finished',expected='demo',actual='demo',passed=True),
            dict(agent='Pipeline',action='run_finished',status='PASS')]:
            kw['progress_callback'](record)
        return dict(run_dir=str(tmp_path),verdict=dict(status='PASS'))
    store=TaskStore(tmp_path,runner=runner,llm_factory=Client)
    store.start(dict(url='http://127.0.0.1:8080/',requirement='demo'),request_id=uuid.uuid4().hex,
        api_key='synthetic-key',ai_config=dict(provider='deepseek',model='offline-only'),run_mode=mode,demo_layout={})
    assert closed.wait(3)
    text=output.getvalue()
    assert 'LLM: call_started' in text
    if mode=='demo':
        for expected in ('Planner','Fill demo','Review rejected','Check source','driver.title','operation=click',
                         'Cold','Executor','expected=demo','actual=demo','status=PASS'):
            assert expected in text
    else:
        assert 'Planner' not in text and 'driver.title' not in text


@pytest.mark.parametrize('mode,expected', [('normal',[True,True,False]),('demo',[False,True,False])])
def test_trial_cold_formal_policy_and_frozen_rectangle(mode,expected):
    original = dict(right=dict(x=960,y=0,width=960,height=1040))
    config = layout.freeze_presentation(mode,original)
    original['right']['x']=0
    settings = [layout.session_display(config,purpose,reason,True,False) for purpose,reason in
                [('trial','incremental_development'),('trial','mandatory_cold_trial'),('formal','independent_formal_replay')]]
    assert [s['headless'] for s in settings] == expected
    if mode == 'demo':
        assert settings[0]['window_rect'] == settings[2]['window_rect'] == dict(x=960,y=0,width=960,height=1040)
        assert 'window_rect' not in settings[1]
        settings[0]['window_rect']['x']=0
        assert config['right']['x']==960


def test_missing_launcher_warns_and_layout_failure_is_nonfatal(monkeypatch):
    monkeypatch.setattr(layout,'monitors',lambda: (_ for _ in ()).throw(OSError('no desktop')))
    config=layout.freeze_presentation('demo',{})
    assert config['warning'] and not config['desktop_ready']
    monkeypatch.setattr(layout,'chrome_window',lambda driver: (_ for _ in ()).throw(OSError('no hwnd')))
    class Driver:
        def set_window_rect(self,**kw): raise RuntimeError('unavailable')
    result=layout.place_chrome(Driver(),dict(x=1,y=2,width=960,height=1000))
    assert not result['positioned'] and result['warning'] and result['fallback_error_type']=='RuntimeError'


def test_console_redacts_secrets_terminal_controls_and_does_not_print_hidden_reasoning():
    secret='synthetic-user-credential'
    text=format_event(dict(agent='Agent3',action='code_generated',elapsed_seconds=12,code=
        f'field.send_keys("{secret}")\npassword="hidden"\nprint("sk-123456abcdef")\n\x1b[2J\x9b2J\u202eevil',
        reasoning_content='NEVER SHOW HIDDEN THOUGHT',request={'Authorization':'Bearer dangerous'}),secrets=(secret,))
    for forbidden in (secret,'hidden','sk-123456abcdef','\x1b','\x9b','\u202e','NEVER SHOW','dangerous'):
        assert forbidden not in text
    assert '[REDACTED]' in text and 'Actual Selenium code' in text
    assert len(safe_display('x'*40000))<6100


def test_console_fifo_and_slow_output_never_block_producer():
    gate=threading.Event()
    class Slow(io.StringIO):
        def write(self,value): gate.wait(2); return super().write(value)
    stream=Slow(); console=DemoConsole(stream=stream,capacity=4)
    started=time.monotonic()
    for i in range(100): console(dict(agent='Agent3',action='generate_step',step_id=f'S{i}'))
    assert time.monotonic()-started<.5
    assert console.dropped>0
    gate.set(); console.close(); console.thread.join(2)
    output=stream.getvalue()
    positions=[output.index(f'step_id=S{i}') for i in range(4)]
    assert positions==sorted(positions)


def test_public_planning_review_code_and_formal_events_are_rendered():
    plan=format_event(dict(agent='Agent1',action='semantic_plan_created',plan_steps=[dict(id='S1',goal='Fill field')],
        plan_oracles=[dict(id='O1',kind='equals',expected='done',source_type='user_requirement')]))
    assert 'Fill field' in plan and 'equals' in plan and 'user_requirement' in plan
    reject=format_event(dict(agent='Agent2',action='review_finished',approved=False,issues=['Missing source']))
    assert 'rejected' in reject and 'Missing source' in reject and 'error_type' not in reject
    assert not format_event(dict(agent='Runtime',action='browser_action',phase='mandatory_cold_trial'))
    check=format_event(dict(agent='Agent5',action='check_finished',expected='done',actual='done',passed=True))
    assert 'expected=done' in check and 'actual=done' in check and 'passed=true' in check


class ScriptedDemoModel:
    """Predetermined local fixture plan/code; no API requests or hidden reasoning."""
    calls=[]
    def complete_json(self,system,user):
        if system.startswith('You are Agent1'):
            return dict(steps=[dict(id='S1',goal='Fill the field with demo')],oracles=[dict(
                id='O1',step_id='S1',meaning='field text',kind='equals',expected='demo',
                source_type='user_requirement',source_quote='demo')])
        if system.startswith('You are Agent2'):
            return dict(approved=True,input_status='consistent',issues=[])
        return dict(code='driver.find_element(By.ID, "query").send_keys("demo")',
                    bindings={'O1':dict(by='id',selector='query',read='attribute',attribute='value',step_id='S1')})


@pytest.mark.parametrize('mode',['normal','demo'])
def test_pipeline_same_contract_code_verdict_and_three_sessions(mode,tmp_path,monkeypatch):
    from web_agent import selenium_runtime as runtime
    from web_agent.pipeline import run_pipeline
    sessions=[]; records=[]
    class Session:
        def __init__(self,purpose,options):
            self.purpose=purpose; self.options=options; self.run_dir=tmp_path
            self.session_id=uuid.uuid4().hex; self.metadata=dict(browser_session_id=self.session_id)
            sessions.append(self)
        def observe(self,**kw): return {}
        def execute_checked(self,*a,**kw): return dict(status='COMPLETE')
        def execute_program(self,*a,**kw):
            return dict(execution_status='COMPLETE',browser_session_id=self.session_id,measurements={'O1':dict(
                value='demo',status='observed',step_id='S1',observation_ref='dom.json',screenshot_ref='screen.png')})
        def close_session(self): return dict(process_tree_terminated=True,profile_removed=True)
    monkeypatch.setattr(runtime,'ARTIFACT_ROOT',tmp_path)
    monkeypatch.setattr(runtime,'create_session',lambda *a,purpose,**kw:Session(purpose,kw))
    result=run_pipeline(target='dynamic',mode='llm',llm=ScriptedDemoModel(),requirement='Fill demo',
        start_url='http://127.0.0.1:8080/',allow_live_site=True,allow_local_research=True,output_root=tmp_path,
        headless=True,formal_headless=False,run_mode=mode,demo_layout={},progress_callback=records.append)
    assert result['verdict']['status']=='PASS'
    assert [s.options['headless'] for s in sessions]==([True,True,False] if mode=='normal' else [False,True,False])
    assert len({s.session_id for s in sessions})==3
    run=Path(result['run_dir']); delivery=json.loads((run/'agent3_delivery.json').read_text())
    evidence=json.loads((run/'formal_evidence.json').read_text())
    assert evidence['contract_sha256']==delivery['contract_sha256']
    assert evidence['browser_session_id']==sessions[-1].session_id
    assert json.loads((run/'presentation.json').read_text(encoding='utf-8'))['mode']==mode
    actions=[r['action'] for r in records]
    assert ('code_generated' in actions)==(mode=='demo')
    if mode=='demo':
        assert actions.index('code_generated')<actions.index('step_executed')
        assert actions.index('check_finished')<actions.index('business_verdict')


def test_task_mode_is_frozen_and_history_never_reexecutes(tmp_path,monkeypatch):
    from web_agent.ui_tasks import TaskStore
    entered=threading.Event();release=threading.Event();received=[]
    class Client:
        def __init__(self,**kw): pass
        def close(self): pass
    def runner(**kw):
        received.append(kw);entered.set();release.wait(3)
        return dict(run_dir=str(tmp_path),verdict=dict(status='PASS'))
    store=TaskStore(tmp_path,runner=runner,llm_factory=Client)
    original=dict(right=dict(x=960,y=0,width=960,height=1040))
    request=uuid.uuid4().hex
    inputs=dict(url='http://127.0.0.1:8080/',requirement='demo',test_basis='')
    try:
        key=store.start(inputs,request_id=request,api_key='synthetic',run_mode='demo',demo_layout=original,
                        ai_config=dict(provider='deepseek',model='offline-only'))
        assert entered.wait(2)
        original['right']['x']=0
        assert store.start(inputs,request_id=request,run_mode='normal')==key
        snapshot=store.snapshot(key);snapshot['presentation']['mode']='normal'
        assert store.snapshot(key)['presentation']['mode']=='demo'
        assert received[0]['run_mode']=='demo' and received[0]['demo_layout']['right']['x']==960
        store.recent();store.snapshot(key)
        assert len(received)==1
    finally: release.set()
