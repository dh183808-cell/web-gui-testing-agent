"""Opt-in local Chrome + hand-authored frozen navigation. No LLM/external site."""
from contextlib import ExitStack
import hashlib
import json
import os

import pytest
from bs4 import BeautifulSoup

from tests.date_navigation_support import local_page, program, title
from tests.test_date_semantics import plan
from tests.test_runtime_performance_live import artifact_dir, dense_fixture
from web_agent.agents.executor import Agent4TestExecutor
from web_agent.agents.verifier import Agent5EvidenceVerifier
from web_agent.selenium_runtime import create_session


pytestmark = pytest.mark.skipif(os.getenv('WEBAGENT_RUN_BROWSER_TESTS') != '1',
    reason='Opt-in real local Chrome cross-year navigation')


@pytest.mark.parametrize('start,stuck,expected_clicks', [
    ((2025,12), False, ['next']*11),
    ((2027,1), False, ['prev']*2),
    ((2025,12), True, ['next']),
])
def test_real_chrome_cross_year_and_stuck_navigation(start, stuck, expected_clicks):
    root = artifact_dir('date_navigation_local')
    source = program(timeout=1.5)
    path = root / 'hand_authored_test.py'
    path.write_text(source, encoding='utf-8')
    digest = hashlib.sha256(source.encode()).hexdigest()
    contract = plan()
    contract['steps'] = [dict(id='S1', goal='Open the calendar'),
        dict(id='S2', goal='Navigate to November 2026', depends_on=['S1']),
        dict(id='S3', goal='Confirm month and select day 10', depends_on=['S2']),
        dict(id='S4', goal='Measure actual selected date', depends_on=['S3'])]
    contract['oracles'][0]['step_id'] = 'S4'
    bindings = {'O1':dict(by='id', selector='date', read='attribute', attribute='value', step_id='S4')}
    sessions = []
    records = dict(start=list(start), stuck=stuck, expected_clicks=expected_clicks,
        test_type='Real local Chrome + hand-authored checked script; no LLM',
        script_sha256=digest, contract=contract)
    try:
        with dense_fixture(local_page(start, stuck=stuck)) as url, ExitStack() as stack:
            # Stuck navigation stops at cold trial; do not deliver broken code to Agent4.
            for purpose in (('trial',) if stuck else ('trial','formal')):
                session = create_session(root, purpose=purpose, start_url=url, research_mode=True)
                sessions.append(session)
                stack.callback(session.close_session)
                entry = session.observe(screenshot=True)
                entry_html = (session.run_dir / entry['observation_ref']).with_suffix('.html').read_text('utf-8')
                assert BeautifulSoup(entry_html, 'html.parser').select_one('#month').get_text(strip=True) == title(*start)
                delivery = dict(script_path=str(path), script_sha256=digest,
                    cold_trial_complete=records.get('trial',{}).get('execution_status') == 'COMPLETE',
                    step_ids=['S1','S2','S3','S4'], bindings=bindings)
                result = (session.execute_program(source, step_ids=delivery['step_ids'], bindings=bindings)
                    if purpose == 'trial' else Agent4TestExecutor().execute_dynamic(delivery, session=session))
                records[purpose] = result
                assert result['script_sha256'] == digest
                actions = [a for step in result['steps'] for a in step['actions'] if a['status']=='complete']
                navigation = [a['target'].rsplit(': ',1)[-1] for a in actions
                              if a['target'] in {'id: next','id: prev'}]
                assert navigation == expected_clicks
                if stuck:
                    assert result['execution_status'] == 'ERROR'
                    assert result['steps'][-1]['step_id'] == 'S2'
                    assert result['steps'][-1]['error_type'] == 'TimeoutException'
                    assert result['measurements'] == {}
                    assert not any(a['target']=='id: day10' for a in actions)
                    records['technical_verdict'] = Agent5EvidenceVerifier().verify_dynamic(contract,result).to_dict()
                    assert records['technical_verdict']['status'] == 'ERROR'
                    records['formal_not_started_reason'] = 'Cold navigation failed; no Agent4 delivery'
                else:
                    assert result['execution_status'] == 'COMPLETE', result
                    m = result['measurements']['O1']
                    assert m['value'] == '11/10/2026'
                    calendar = m['calendar_evidence']
                    assert calendar['heading']['text'] == 'November 2026'
                    assert calendar['selected'][0]['text'] == '10'
                    assert calendar['provenance']['purpose'] == purpose
                    for key in ('observation_ref','screenshot_ref'):
                        assert calendar['provenance'][key] == m[key]
                        assert (session.run_dir / m[key]).is_file()
                    verdict = Agent5EvidenceVerifier().verify_dynamic(contract, result)
                    records[purpose+'_verdict'] = verdict.to_dict()
                    assert verdict.status == ('PASS' if purpose=='formal' else 'INCONCLUSIVE')
                assert session.close_session()['profile_removed']
            if not stuck:
                assert records['trial']['session_id'] != records['formal']['session_id']
                assert records['trial']['browser_session_id'] != records['formal']['browser_session_id']
    finally:
        records['cleanup'] = [s.close_session() for s in sessions]
        (root / 'acceptance.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
    assert all(c['owned_processes_remaining']==0 and c['profile_removed'] for c in records['cleanup'])
