"""Opt-in local Chrome DOM collection and frozen replay; no model or external site."""
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


pytestmark=pytest.mark.skipif(os.getenv('WEBAGENT_RUN_BROWSER_TESTS')!='1',reason='Opt-in local Chrome calendar evidence')


def calendar_page(case):
    """Synthetic ARIA calendar or datepicker-shaped state; no real library claim."""
    component=case.startswith('component')
    day=11 if case=='wrong-date' else 10
    raw='11/12/2026' if case=='conflict' else f'11/{day:02}/2026'
    controls='' if component or case=='unlinked' else 'aria-controls="calendar"'
    role='' if component else 'role="grid" aria-labelledby="month"'
    cell='class="day" data-date="1794268800000"' if component else 'role="gridcell" aria-selected="false"'
    body='''<!doctype html><meta charset="utf-8"><title>Local calendar evidence fixture</title>
<label for="date">Calendar date</label><input id="date" CONTROLS value="10/01/2026"
 onclick="document.querySelector('#calendar').hidden=false">
<div id="calendar" ROOT_CLASS hidden><div class="datepicker-days">
<h2 id="month">October 2026</h2><table GRID_ROLE><thead><tr><th class="datepicker-switch">October 2026</th></tr></thead>
<tbody><tr><td id="today" role="gridcell" aria-current="date">9</td>
<td id="day" CELL tabindex="0" onclick="choose()">DAY</td>
<td id="other" role="gridcell" aria-selected="false">12</td></tr></tbody></table>
<button id="next" onclick="document.querySelector('#month').textContent='November 2026';
document.querySelector('.datepicker-switch').textContent='November 2026'">Next month</button>
</div></div>EXTRA
<script>
const input=document.querySelector('#date'), picker=document.querySelector('#calendar'), day=document.querySelector('#day');
const instance={element:[input],inputField:[input],picker:[picker],dates:[]};
COMPONENT
function choose() {
  input.value='RAW';
  SELECTION
  CLOSING
}
</script>'''
    selection="day.setAttribute('aria-selected','true');"
    if component:
        selection="day.className='active day'; instance.dates=[new Date(1794268800000)];"
    if case in {'month-only','today-only'}:
        selection="day.className='today focused'; day.setAttribute('aria-current','date');"
    if case=='multiple-selected': selection+="document.querySelector('#other').setAttribute('aria-selected','true');"
    if case=='component-active-only': selection="day.className='active day';"
    extra=''
    if case=='multiple-calendars': extra='<table role="grid"><tr><td role="gridcell" aria-selected="true">10</td></tr></table>'
    if case=='multiple-inputs': extra='<input aria-controls="calendar">'
    if case=='duplicate-heading': extra='<h2 id="month">October 2026</h2>'
    return (body.replace('CONTROLS',controls).replace('ROOT_CLASS','class="datepicker"' if component else '')
        .replace('GRID_ROLE',role).replace('CELL',cell).replace('DAY',str(day)).replace('EXTRA',extra)
        .replace('COMPONENT',"window.jQuery={data:(node,key)=>node===input&&key==='datepicker'?instance:undefined};" if component else '')
        .replace('RAW',raw).replace('SELECTION',selection)
        .replace('CLOSING',"picker.hidden=true;" if case in {'closed','closed-reopen'} else ''))


@pytest.mark.parametrize('case,status',[
    ('aria','PASS'),('wrong-date','FAIL'),('unlinked','INCONCLUSIVE'),
    ('month-only','INCONCLUSIVE'),('multiple-selected','INCONCLUSIVE'),
    ('multiple-calendars','INCONCLUSIVE'),('multiple-inputs','INCONCLUSIVE'),
    ('today-only','INCONCLUSIVE'),('conflict','INCONCLUSIVE'),
    ('component','PASS'),('component-active-only','INCONCLUSIVE'),
    ('closed','INCONCLUSIVE'),('closed-reopen','PASS'),('duplicate-heading','INCONCLUSIVE')])
def test_real_calendar_collection_and_independent_replay(case,status):
    root=artifact_dir('calendar_local')
    source='''# webagent-step: S1
driver.find_element(By.ID, "date").click()
WebDriverWait(driver, 5).until(EC.visibility_of_element_located((By.ID, "calendar")))

# webagent-step: S2
driver.find_element(By.ID, "next").click()
driver.find_element(By.ID, "day").click()
'''
    if case=='closed-reopen':
        source+='''driver.find_element(By.ID, "date").click()
WebDriverWait(driver, 5).until(EC.visibility_of_element_located((By.ID, "calendar")))
'''
    path=root/'hand_authored_test.py'; path.write_text(source,encoding='utf-8')
    digest=hashlib.sha256(source.encode()).hexdigest()
    bindings={'O1':dict(by='id',selector='date',read='attribute',attribute='value',step_id='S2')}
    sessions,results=[],dict(case=case,test_type='Real local Chrome + hand-authored frozen code; no LLM')
    try:
        with dense_fixture(calendar_page(case)) as url, ExitStack() as stack:
            for purpose in ('trial','formal'):
                session=create_session(root,purpose=purpose,start_url=url,research_mode=True)
                sessions.append(session);stack.callback(session.close_session)
                result=(session.execute_program(source,step_ids=['S1','S2'],bindings=bindings) if purpose=='trial'
                    else Agent4TestExecutor().execute_dynamic(dict(script_path=str(path),script_sha256=digest,
                        cold_trial_complete=results['trial']['execution_status']=='COMPLETE',
                        step_ids=['S1','S2'],bindings=bindings),session=session))
                results[purpose]=result
                assert result['execution_status']=='COMPLETE',result
                assert result['script_sha256']==digest
                m=result['measurements']['O1']; c=m['calendar_evidence']
                assert c['input_value']==m['value']
                assert c['provenance']['purpose']==purpose
                assert c['provenance']['session_id']==session.session_id
                assert c['provenance']['browser_session_id']==result['browser_session_id']
                for key in ('observation_ref','screenshot_ref'):
                    assert c['provenance'][key]==m[key]
                    assert (session.run_dir/m[key]).is_file()
                assert session.close_session()['profile_removed']
            assert results['trial']['session_id']!=results['formal']['session_id']
            assert results['trial']['browser_session_id']!=results['formal']['browser_session_id']
            verdict=Agent5EvidenceVerifier().verify_dynamic(plan(),results['formal'])
            results['verdict']=verdict.to_dict()
            assert verdict.status==status,verdict.to_dict()
            if status=='PASS':
                assert verdict.checks[0]['date_comparison']['normalized_actual']=='2026-11-10'
            # Trial has valid-looking DOM too; it still cannot decide business PASS.
            assert Agent5EvidenceVerifier().verify_dynamic(plan(),results['trial']).status=='INCONCLUSIVE'
    finally:
        results['cleanup']=[s.close_session() for s in sessions]
        (root/'acceptance.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
