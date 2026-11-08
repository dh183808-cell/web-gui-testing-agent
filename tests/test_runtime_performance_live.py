"""Opt-in REAL Chrome benchmarks on synthetic local pages, no external site or LLM."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import tempfile
from threading import Thread
import time
import uuid
from collections import Counter

import pytest

from web_agent.checked_selenium import collect_measurements, split_program
from web_agent.dom_inspector import observe_session, compact_dom, CONTROLS
from web_agent.selenium_runtime import make_driver, create_session, RuntimeLimits
from web_agent.agents.executor import Agent4TestExecutor
from web_agent.agents.verifier import Agent5EvidenceVerifier

pytestmark=pytest.mark.skipif(os.getenv('WEBAGENT_RUN_BROWSER_TESTS')!='1',reason='Opt-in real Chrome required')

PAGE='''<!doctype html><title>Runtime performance fixture</title><section id="task" class="completed">
<label for="query">Search term</label><input id="query" class="input" data-testid="query" value="initial">
<select id="scope"><option value="title" selected>Title</option><option value="all">All fields</option></select>
<input id="opacity" type="checkbox" style="opacity:0" checked>
<button id="hidden" style="display:none">Hidden content</button>
<fieldset disabled><input id="disabled"><legend><input id="legend"></legend></fieldset>
<a id="active" href="#/active">Active</a><input type="password" value="synthetic-password">
<input name="api_key" value="synthetic-key"><input type="hidden" value="synthetic-hidden">
<button id="load" onclick="setTimeout(()=>{const b=document.createElement('button');
b.id='continue';b.textContent='Continue';document.querySelector('#task').append(b);},100)">Load</button>
<ul>''' + ''.join(f'<li class="item"><a id="paper{i}" href="/paper/{i}">Result {i}</a></li>' for i in range(50)) + (
    '</ul>' + ''.join(f'<input id="noise{i}">' for i in range(30))+'</section>')


@contextmanager
def dense_fixture():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload=PAGE.encode()
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length',str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}/'
    finally: server.shutdown();server.server_close();thread.join(timeout=2)


def artifact_dir(prefix):
    root=Path(os.getenv('WEBAGENT_TEST_ARTIFACT_ROOT',str(Path(__file__).resolve().parents[1]/'artifacts')))
    root=root/(prefix+'_'+uuid.uuid4().hex[:8]);root.mkdir(parents=True)
    return root


def legacy_observe_workload(driver):
    """The former per-element attribute workload, retained ONLY for a fair benchmark."""
    from selenium.webdriver.common.by import By
    snapshot=compact_dom(driver.page_source,80,max_html=16000)
    for node in driver.find_elements(By.CSS_SELECTOR,CONTROLS)[:160]:
        identity=' '.join(node.get_attribute(k) or '' for k in ('id','name','aria-label'))
        from web_agent.dom_inspector import SENSITIVE
        if (node.get_attribute('type') or '').lower() in {'hidden','password'} or SENSITIVE.search(identity):
            continue
        node.tag_name
        for attr in ('id','name','class','type','data-testid','role','aria-checked','aria-expanded','aria-label','href'):
            node.get_attribute(attr)
        node.is_displayed();node.is_enabled();node.text
        node.get_property('value');node.get_property('checked')
        snapshot.setdefault('live_count',0)
        snapshot['live_count']+=1
        if snapshot['live_count']>=80: break
    driver.current_url;driver.title;driver.current_window_handle;driver.window_handles
    driver.execute_script('return window === window.top')
    driver.execute_script('return document.readyState')
    return snapshot


def test_real_chrome_batch_observation_and_count_benchmark():
    root=artifact_dir('batch_benchmark')
    driver=None
    samples={}
    try:
        with dense_fixture() as url,tempfile.TemporaryDirectory(dir=root) as profile:
            try:
                driver=make_driver(profile_dir=Path(profile),service_log=root/'chromedriver.log')
                driver.get(url)
                original=driver.execute
                calls=Counter()
                def execute(command,*args,**kwargs):
                    calls[command]+=1
                    return original(command,*args,**kwargs)
                driver.execute=execute
                def sample(name,fn):
                    calls.clear();mark=time.monotonic()
                    value=fn()
                    samples[name]=dict(seconds=round(time.monotonic()-mark,4),
                                       commands=sum(calls.values()),command_counts=dict(calls))
                    return value
                old=sample('old_observe',lambda:legacy_observe_workload(driver))
                current=sample('batch_observe',lambda:observe_session(driver,max_elements=80,max_html=16000))
                assert samples['batch_observe']['commands']==3
                assert samples['old_observe']['commands']>1000
                assert old['count']==current['count'] and old['elements']==current['elements']
                live={e['id']:e for e in current['live_elements']}
                assert not live['opacity']['visible'] and live['opacity']['checked']
                assert not live['hidden']['visible'] and live['hidden']['text']==''
                assert not live['disabled']['enabled'] and live['legend']['enabled']
                assert live['query']['value']=='initial' and live['active']['href_path'].endswith('#/active')
                assert current['truncated'] and 'synthetic-password' not in json.dumps(current)
                queries={'O1':dict(by='css',selector='.item',read='count',step_id='S1')}
                old_count=sample('old_count',lambda:collect_measurements(driver,url,queries))
                count=sample('batch_count',lambda:collect_measurements(driver,url,queries,batch=True))
                assert count==old_count and count['O1']['value']==50
                assert samples['batch_count']['commands']==2 and samples['old_count']['commands']>=251
                assert samples['batch_count']['command_counts'].get('getElementText',0)==0
                # Every required scalar/text mode still uses native Selenium semantics.
                more={
                    'O2':dict(by='id',selector='scope',read='selected_text',step_id='S1'),
                    'O3':dict(by='xpath',selector='//input[@id="opacity"]',read='selected',step_id='S1'),
                    'O4':dict(by='id',selector='scope',read='attribute',attribute='value',step_id='S1'),
                    'O5':dict(by='css',selector='.item',read='texts',step_id='S1'),
                    'O6':dict(by='css',selector='#missing',read='selected',step_id='S1')}
                assert collect_measurements(driver,url,more,batch=True)==collect_measurements(driver,url,more)
                samples['browser_version']=driver.capabilities['browserVersion']
                driver.save_screenshot(str(root/'fixture.png'))
            finally:
                if driver is not None:
                    service=driver.service
                    driver.quit()
                    assert service.process.poll() is not None
    finally:
        (root/'benchmark.json').write_text(json.dumps(samples,indent=2),encoding='utf-8')


def test_real_chrome_trial_cold_formal_timings_evidence_and_script_timeout():
    root=artifact_dir('phased_runtime')
    sessions,results=[],{}
    steps=[
        dict(id='S1',source='driver.find_element(By.ID,"query").clear()\ndriver.find_element(By.ID,"query").send_keys("phase-check")'),
        dict(id='S2',source="""driver.find_element(By.XPATH,"//select[@id='scope']/option[@value='all']").click()"""),
        dict(id='S3',source='driver.find_element(By.ID,"load").click()\nWebDriverWait(driver,2).until(EC.presence_of_element_located((By.ID,"continue")))'),
        # Deliberately retain all 50 text reads, as in the failing user's script.
        dict(id='S4',source='WebDriverWait(driver,1).until(EC.presence_of_element_located((By.CSS_SELECTOR,".item")))\nresult=[el.text for el in driver.find_elements(By.CSS_SELECTOR,".item")]')]
    source=''.join('# webagent-step: '+s['id']+'\n'+s['source']+'\n\n' for s in steps)
    bindings=dict(O1=dict(by='id',selector='query',read='attribute',attribute='value',step_id='S1'),
        O2=dict(by='name',selector='not-present',read='exists',step_id='S2'),
        O3=dict(by='id',selector='scope',read='selected_text',step_id='S2'),
        O4=dict(by='css',selector='.item',read='count',step_id='S4'))
    contract={'oracles':[dict(id='O1',step_id='S1',kind='equals',expected='phase-check',meaning='input'),
        dict(id='O2',step_id='S2',kind='equals',expected=False,meaning='missing target'),
        dict(id='O3',step_id='S2',kind='equals',expected='All fields',meaning='selected label'),
        dict(id='O4',step_id='S4',kind='gte',expected=1,meaning='visible results')]}
    try:
        with dense_fixture() as url:
            for kind,purpose in [('development','trial'),('cold','trial'),('formal','formal')]:
                session=create_session(root,purpose=purpose,start_url=url,research_mode=True)
                sessions.append(session)
                if kind=='development':
                    result_steps=[session.execute_checked(s['source'],step_id=s['id'],
                        queries={k:v for k,v in bindings.items() if v['step_id']==s['id']}) for s in steps]
                    result=dict(execution_status='COMPLETE' if all(r['status']=='COMPLETE' for r in result_steps)
                        else 'ERROR',steps=result_steps)
                elif kind=='cold':
                    result=session.execute_program(source,step_ids=[s['id'] for s in steps],bindings=bindings)
                else:
                    path=root/'generated_test.py';path.write_text(source,encoding='utf-8',newline='\n')
                    result=Agent4TestExecutor().execute_dynamic(dict(script_path=str(path),
                        script_sha256=hashlib.sha256(source.encode()).hexdigest(),cold_trial_complete=True,
                        step_ids=[s['id'] for s in steps],bindings=bindings),session=session)
                    verdict=Agent5EvidenceVerifier().verify_dynamic(contract,result).to_dict()
                    assert verdict['status']=='PASS'
                    results['verdict']=verdict
                    for m in result['measurements'].values():
                        assert (session.run_dir/m['observation_ref']).is_file()
                        assert (session.run_dir/m['screenshot_ref']).is_file()
                results[kind]=result
                assert result['execution_status']=='COMPLETE',result
                for step in result['steps']:
                    assert set(step['timings'])==set(runtime_phase for runtime_phase in
                        ('pre_observe','script_execute','oracle_capture','post_observe'))
                    assert all(v=='COMPLETE' for v in step['phase_status'].values())
                assert session.close_session()['process_tree_terminated']
            assert results['cold']['script_sha256']==results['formal']['script_sha256']
            assert len({s.metadata['browser_session_id'] for s in sessions})==3
            timeout=create_session(root,purpose='trial',start_url=url,research_mode=True,
                                   limits=RuntimeLimits(execution_seconds=.2))
            sessions.append(timeout)
            failed=timeout.execute_checked(
                'WebDriverWait(driver,2).until(EC.presence_of_element_located((By.ID,"missing")))',step_id='S1')
            results['script_timeout']=failed
            assert failed['status']=='TIMEOUT' and failed['failed_phase']=='script_execute'
            assert failed['script_status']=='TIMEOUT' and failed['failure_domain']=='script'
            assert failed['cleanup']['process_tree_terminated'] and failed['cleanup']['owned_processes_remaining']==0
    finally:
        results['cleanup']=[s.close_session() for s in sessions]
        (root/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
