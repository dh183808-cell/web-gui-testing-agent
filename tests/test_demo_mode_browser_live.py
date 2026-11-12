"""Opt-in real Windows windows + local Chrome + scripted model, never a real API."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from web_agent import desktop_layout as layout
from web_agent import selenium_runtime as runtime
from web_agent.demo_console import DemoConsole
from web_agent.pipeline import run_pipeline
from tests.test_demo_mode import ScriptedDemoModel
from tests.test_runtime_performance_live import dense_fixture, artifact_dir

pytestmark=pytest.mark.skipif(os.getenv('WEBAGENT_RUN_BROWSER_TESTS')!='1' or os.name!='nt',
                             reason='Opt-in real Windows interactive Chrome required')


def top_window_at(rect):
    with layout.physical_pixels() as api:
        point=W.POINT(rect['x']+rect['width']//2,rect['y']+rect['height']//2)
        api.WindowFromPoint.argtypes=[W.POINT];api.WindowFromPoint.restype=W.HWND
        api.GetAncestor.argtypes=[W.HWND,W.UINT];api.GetAncestor.restype=W.HWND
        return int(api.GetAncestor(api.WindowFromPoint(point),2) or 0)


@contextmanager
def actual_streamlit(root,rect):
    """Run actual app with a fresh TaskStore; never alter historical task files."""
    wrapper=root/'isolated_ui.py'
    project=Path(__file__).resolve().parents[1]
    wrapper.write_text('import sys, runpy\nfrom pathlib import Path\n'
        f'sys.path.insert(0,{str(project)!r})\n'
        'from web_agent import ui_tasks\noriginal=ui_tasks.TaskStore\n'
        f'ui_tasks.TaskStore=lambda:original(Path({str(root / "ui_jobs")!r}))\n'
        f'runpy.run_path({str(project / "app_ui.py")!r},run_name="__main__")\n',encoding='utf-8')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    browser=None;server=None
    with (root/'streamlit.log').open('w',encoding='utf-8') as log:
        try:
            server=subprocess.Popen([sys.executable,'-m','streamlit','run',str(wrapper),
                '--server.address','127.0.0.1','--server.port',str(port),'--server.headless','true',
                '--browser.gatherUsageStats','false'],stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
            url=f'http://127.0.0.1:{port}'
            for _ in range(120):
                if server.poll() is not None: raise RuntimeError('Streamlit exited')
                try:
                    with urllib.request.urlopen(url+'/_stcore/health',timeout=.3): break
                except OSError: time.sleep(.25)
            else: raise RuntimeError('Streamlit startup timeout')
            browser=runtime.make_driver(headless=False,profile_dir=root/'frontend_profile',window_rect=rect)
            positioned=layout.place_chrome(browser,rect)
            assert positioned['positioned'],positioned
            browser.get(url)
            from selenium.webdriver.support.ui import WebDriverWait
            WebDriverWait(browser,20).until(lambda d:'配置本次测试' in d.find_element('tag name','body').text)
            assert 'Traceback' not in browser.find_element('tag name','body').text
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC
            requirement=WebDriverWait(browser,20).until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR,'[data-testid="stTextArea"] textarea')))
            requirement.send_keys('Local presentation text')
            WebDriverWait(browser,10).until(EC.element_to_be_clickable((By.XPATH,'//label[contains(.,"演示模式")]'))).click()
            WebDriverWait(browser,10).until(lambda d:'无需额外启动终端' in d.find_element('tag name','body').text)
            assert browser.find_element(By.CSS_SELECTOR,'[data-testid="stTextArea"] textarea').get_attribute('value')=='Local presentation text'
            browser.find_element(By.XPATH,'//label[contains(.,"常规模式")]').click()
            WebDriverWait(browser,10).until(lambda d:'无需额外启动终端' not in d.find_element('tag name','body').text)
            browser.save_screenshot(str(root/'streamlit_before.png'))
            yield browser,positioned
            browser.save_screenshot(str(root/'streamlit_after.png'))
            assert server.poll() is None
        finally:
            if browser: browser.quit()
            if server and server.poll() is None:
                server.terminate()
                try: server.wait(5)
                except subprocess.TimeoutExpired: server.kill();server.wait(5)


@pytest.mark.parametrize('mode',['normal','demo'])
def test_real_pipeline_windows_and_background_restoration(mode,monkeypatch):
    root=artifact_dir('demo_windows_'+mode)
    regions=layout.split_work_area(layout.monitors()[0]['work'])
    sessions=[];events=[];checks=[];restored=[]
    original=runtime.create_session
    results=dict(test_type='Real local Chrome/Streamlit/Windows + scripted model; no paid API',mode=mode)
    log=(root/'terminal_events.log').open('w',encoding='utf-8')
    class Tee:
        def write(self,text): log.write(text);sys.stdout.write(text)
        def flush(self): log.flush();sys.stdout.flush()
    console=DemoConsole(stream=Tee())
    def session(*args,**kwargs):
        s=original(*args,**kwargs);sessions.append(s)
        window=None
        meta=s.metadata
        handles=layout.process_windows(meta['worker_pid'])
        assert len(handles)==(0 if kwargs['headless'] else 1),handles
        if handles: window=layout.window_info(handles[0])
        if meta.get('window_layout'):
            native=layout.window_info(meta['window_layout']['hwnd']);window=native
            assert meta['window_layout']['positioned'],meta['window_layout']
            assert top_window_at(regions['right'])==native['hwnd']
        checks.append(dict(session_id=s.session_id,purpose=s.purpose,browser_headless=meta['browser_headless'],
                           profile_id=meta['profile_id'],native_window=window,options=kwargs))
        return s
    def progress(record):
        events.append(record);console(record)
        if record['action']=='session_closed' and record['reason'] in {'mandatory_cold_trial','formal_replay_finished'}:
            # Trial/Formal has gone; actual UI window remains at the right-half point.
            restored.append(top_window_at(regions['right'])==frontend['hwnd'])
    monkeypatch.setattr(runtime,'create_session',session)
    try:
        with actual_streamlit(root,regions['right']) as (ui,frontend),dense_fixture('<!doctype html><title>Demo fixture</title>'
                '<h1>Local demo</h1><label for="query">Query</label><input id="query"><p>Real Selenium input</p>') as url:
            desktop=dict(regions)
            if os.environ.get('WEBAGENT_TEST_CONSOLE_LAYOUT'):
                prepared=json.loads(os.environ['WEBAGENT_TEST_CONSOLE_LAYOUT'])
                desktop.update(terminal_hwnd=prepared['terminal_hwnd'],frontend_hwnd=frontend['hwnd'])
                results['terminal_window']=layout.window_info(prepared['terminal_hwnd'])
                assert results['terminal_window']['visible']
            result=run_pipeline(target='dynamic',mode='llm',llm=ScriptedDemoModel(),requirement='Fill demo',
                start_url=url,allow_live_site=True,allow_local_research=True,output_root=root,headless=True,
                formal_headless=False,run_mode=mode,demo_layout=desktop,progress_callback=progress)
            assert result['verdict']['status']=='PASS',result
            assert len(sessions)==3
            assert sum(e['action']=='step_started' for e in events)==3
            assert sum(e['action']=='step_finished' for e in events)==3
            assert [s.metadata['browser_headless'] for s in sessions]==([True,True,False] if mode=='normal' else [False,True,False])
            assert len({s.metadata['profile_id'] for s in sessions})==3
            assert len({s.metadata['browser_session_id'] for s in sessions})==3
            assert all(restored),restored
            if mode=='demo':
                assert checks[0]['native_window']['rect']==checks[2]['native_window']['rect']
                assert any(r['action']=='browser_action' and r.get('purpose')=='trial' for r in events)
                assert any(r['action']=='browser_action' and r.get('purpose')=='formal' for r in events)
                assert not any(r['action']=='browser_action' and r.get('phase')=='mandatory_cold_trial' for r in events)
            run=Path(result['run_dir'])
            delivery=json.loads((run/'agent3_delivery.json').read_text('utf-8'))
            evidence=json.loads((run/'formal_evidence.json').read_text('utf-8'))
            assert evidence['purpose']=='formal' and evidence['session_id']==sessions[-1].session_id
            assert delivery['script_sha256']==evidence['script_sha256']==hashlib.sha256((run/'generated_test.py').read_bytes()).hexdigest()
            assert delivery['contract_sha256']==evidence['contract_sha256']
            for m in evidence['measurements'].values():
                assert (Path(evidence['formal_run_dir'])/m['screenshot_ref']).is_file()
                assert (Path(evidence['formal_run_dir'])/m['observation_ref']).is_file()
            assert top_window_at(regions['right'])==frontend['hwnd']
            results.update(result=result,window_checks=checks,frontend=frontend,restored=restored,events=events)
    finally:
        results['cleanup']=[s.close_session() for s in sessions]
        console.close();console.thread.join(2);log.close()
        (root/'acceptance.json').write_text(json.dumps(results,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    assert all(c['process_tree_terminated'] and c['profile_removed'] and c['owned_processes_remaining']==0 for c in results['cleanup'])


def test_real_placement_failure_keeps_formal_evidence(monkeypatch):
    root=artifact_dir('demo_windows_fallback')
    # Worker imports layout in its own process. An impossible requested width triggers
    # Chrome's genuine minimum-size behaviour rather than a mocked Worker result.
    rect=dict(x=0,y=0,width=100,height=100)
    sessions=[];result={}
    try:
        with dense_fixture('<!doctype html><title>Fallback</title><input id="query">') as url:
            session=runtime.create_session(root,purpose='formal',headless=False,window_rect=rect,
                start_url=url,research_mode=True,presentation_events=True)
            sessions.append(session)
            assert session.metadata['window_layout']['warning']
            assert 'hwnd' in session.metadata['window_layout']  # genuine minimum size, not lookup failure
            assert not session.metadata['window_layout']['positioned']
            result=session.execute_checked('driver.find_element(By.ID,"query").send_keys("demo")',step_id='S1',
                queries={'O1':dict(by='id',selector='query',read='attribute',attribute='value',step_id='S1')})
            assert result['status']=='COMPLETE' and result['measurements']['O1']['value']=='demo'
            assert (session.run_dir/result['measurements']['O1']['screenshot_ref']).is_file()
    finally:
        cleanup=[s.close_session() for s in sessions]
        (root/'acceptance.json').write_text(json.dumps(dict(result=result,cleanup=cleanup,
            layout=[s.metadata.get('window_layout') for s in sessions]),indent=2),encoding='utf-8')
    assert all(c['process_tree_terminated'] and c['profile_removed'] for c in cleanup)


def test_real_launcher_console_rectangle():
    root=artifact_dir('demo_console_window')
    helper=root/'console_probe.py'
    project=Path(__file__).resolve().parents[1]
    helper.write_text('import sys,time,json\nfrom pathlib import Path\n'
        f'sys.path.insert(0,{str(project)!r})\n'
        'from web_agent.desktop_layout import prepare_console\n'
        'from web_agent.demo_console import DemoConsole\n'
        f'root=Path({str(root)!r})\n'
        'try:\n'
        ' config=prepare_console()\n'
        ' (root/"window.json").write_text(json.dumps(config),encoding="utf-8")\n'
        ' print("WebAgent | DEMO MODE · real console placement",flush=True)\n'
        ' sink=DemoConsole()\n'
        ' sink(dict(agent="Runtime",action="session_created",purpose="presentation_probe",elapsed_seconds=0))\n'
        ' sink.close()\n'
        ' for _ in range(100):\n'
        '  if (root/"stop").exists(): break\n'
        '  time.sleep(.2)\n'
        'except Exception as exc:\n'
        ' (root/"error.txt").write_text(type(exc).__name__+": "+str(exc),encoding="utf-8")\n',encoding='utf-8')
    host=Path(os.environ['WINDIR'])/'System32'/'conhost.exe'
    proc=subprocess.Popen([str(host),sys.executable,str(helper)],cwd=project)
    try:
        for _ in range(100):
            if (root/'window.json').exists() or (root/'error.txt').exists(): break
            time.sleep(.1)
        assert not (root/'error.txt').exists(),(root/'error.txt').read_text('utf-8') if (root/'error.txt').exists() else ''
        config=json.loads((root/'window.json').read_text('utf-8'))
        info=layout.window_info(config['terminal_hwnd'])
        assert info['visible']
        assert all(abs(info['rect'][k]-config['left'][k])<=16 for k in config['left']),info
        (root/'acceptance.json').write_text(json.dumps(dict(configuration=config,actual=info),indent=2),encoding='utf-8')
    finally:
        (root/'stop').touch()
        proc.wait(timeout=25)
