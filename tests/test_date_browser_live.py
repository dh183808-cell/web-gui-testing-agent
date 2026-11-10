"""Opt-in real Chrome/local fixtures with hand-authored code, NO LLM E2E claim."""
from contextlib import ExitStack
import hashlib
import json
import os

import pytest

from tests.test_runtime_performance_live import dense_fixture, artifact_dir
from tests.test_date_semantics import plan
from web_agent.agents.executor import Agent4TestExecutor
from web_agent.agents.verifier import Agent5EvidenceVerifier
from web_agent.selenium_runtime import create_session


pytestmark = pytest.mark.skipif(os.getenv('WEBAGENT_RUN_BROWSER_TESTS') != '1',
                               reason='Opt-in real Chrome/local date fixtures')


@pytest.mark.parametrize('raw,attributes,structure,status', [
    ('2026-11-10','type="date"','section','PASS'),
    ('11/10/2026','data-date-format="mm/dd/yyyy"','div','PASS'),
    ('11/11/2026','placeholder="DD/MM/YYYY"','fieldset','FAIL'),
    ('03/04/2026','','article','INCONCLUSIVE'),
])
def test_real_date_value_and_format_replay(raw, attributes, structure, status):
    # Synthetic one-month calendar: initial default differs; next then day click
    # are required. A known defective field writes the wrong date on day 10.
    page = '''<!doctype html><meta charset="utf-8"><title>Date fixture</title>
    <STRUCTURE><label>Calendar date</label><input id="date" ATTRS value="2026-10-01"
        onclick="document.querySelector('#calendar').hidden=false">
    <div id="calendar" hidden><h2 id="month">October 2026</h2>
    <button id="next" onclick="document.querySelector('#month').textContent='November 2026';
        document.querySelector('#day').hidden=false">Next month</button>
    <button id="day" hidden onclick="document.querySelector('#date').value='RAW';
        document.querySelector('#calendar').hidden=true">10</button></div></STRUCTURE>'''
    page = page.replace('STRUCTURE',structure).replace('ATTRS',attributes).replace('RAW',raw)
    root = artifact_dir('date_local')
    source = '''# webagent-step: S1
driver.find_element(By.ID, "date").click()
WebDriverWait(driver, 5).until(EC.visibility_of_element_located((By.ID, "next")))

# webagent-step: S2
driver.find_element(By.ID, "next").click()
WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.ID, "day"))).click()
'''
    path = root/'hand_authored_test.py'
    path.write_text(source,encoding='utf-8')
    digest = hashlib.sha256(source.encode()).hexdigest()
    bindings = dict(O1=dict(by='id',selector='date',read='attribute',attribute='value',step_id='S2'))
    results, sessions = {}, []
    try:
        with dense_fixture(page) as url, ExitStack() as stack:
            for purpose in ('trial','formal'):
                session = create_session(root,purpose=purpose,start_url=url,research_mode=True)
                sessions.append(session)
                stack.callback(session.close_session)
                result = (session.execute_program(source,step_ids=['S1','S2'],bindings=bindings)
                    if purpose == 'trial' else Agent4TestExecutor().execute_dynamic(
                        dict(script_path=str(path),script_sha256=digest,cold_trial_complete=True,
                             step_ids=['S1','S2'],bindings=bindings),session=session))
                results[purpose] = result
                assert result['execution_status'] == 'COMPLETE',result
                assert result['script_sha256'] == digest
                assert result['measurements']['O1']['value'] == raw
                for key in ('observation_ref','screenshot_ref'):
                    assert (session.run_dir/result['measurements']['O1'][key]).is_file()
                cleanup = session.close_session()
                assert cleanup['process_tree_terminated'] and cleanup['profile_removed']
            assert results['trial']['session_id'] != results['formal']['session_id']
            assert results['trial']['browser_session_id'] != results['formal']['browser_session_id']
            verdict = Agent5EvidenceVerifier().verify_dynamic(plan(),results['formal'])
            results['verdict'] = verdict.to_dict()
            assert verdict.status == status
    finally:
        results['test_type'] = 'Real Chrome + synthetic local fixture + hand-authored code; no LLM'
        results['cleanup'] = [s.close_session() for s in sessions]
        (root/'acceptance.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
