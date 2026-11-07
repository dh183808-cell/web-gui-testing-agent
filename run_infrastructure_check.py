"""Real LOCAL Selenium + Chrome acceptance using reviewed fixture programs only."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import uuid

from web_agent.selenium_runtime import (ARTIFACT_ROOT, RuntimeLimits, create_session,
                                         LocalRuntimeUnavailable, REVIEWED_SCRIPT_SHA256)


def run_acceptance(run_dir: Path, *, driver_path=None, chrome_binary=None, headless=True) -> dict:
    run_dir = run_dir.resolve()
    if not run_dir.is_relative_to(ARTIFACT_ROOT.resolve()):
        raise ValueError('Acceptance outputs must stay inside artifacts/')
    run_dir.mkdir(parents=True, exist_ok=False)
    result = dict(status='FAILED', run_dir=str(run_dir), real_chrome_executed=False,
                  script_origin='human-authored, reviewed fixture; NOT Agent3 output',
                  script_sha256=REVIEWED_SCRIPT_SHA256, execution_mode='local_trusted',
                  security_sandbox=False, checks={}, runs={},
                  unsupported=['arbitrary or LLM-generated Python', 'host file/permission isolation',
                               'enforced network/CPU/memory isolation', 'real LLM end-to-end'])
    checks, runs, sessions = result['checks'], result['runs'], []
    options = dict(driver_path=driver_path, chrome_binary=chrome_binary, headless=headless)

    def new(purpose='trial', limits=None):
        session = create_session(run_dir, purpose=purpose, limits=limits or RuntimeLimits(), **options)
        sessions.append(session)
        result['real_chrome_executed'] = True
        result['browser_version'] = session.metadata['browser_version']
        return session

    def cleaned(session):
        c = session.close_session()
        return c['process_tree_terminated'] and c['owned_processes_remaining'] == 0 and c['profile_removed']

    try:
        continuous = new()
        first = continuous.observe(screenshot=True)
        nav = continuous.execute_trusted('navigate')
        before = continuous.observe(region_css='#task')
        update = continuous.execute_trusted('update')
        after = continuous.observe(region_css='#task', screenshot=True)
        runs['continuous'] = dict(navigate=nav, update=update, first=first, before=before, after=after)
        checks['A_actual_form_submission'] = (update.get('details', {}).get('receipt') == '1'
                                              and update.get('submissions') == ['Synthetic QA'])
        checks['B_navigation_observed'] = first['url'] != before['url'] and any(e['id'] == 'name' for e in before['elements'])
        checks['B_same_url_ajax_observed'] = (before['url'] == after['url'] and before['state_hash'] != after['state_hash']
            and any(e['id'] == 'continue' and e['visible'] for e in after['live_elements'])
            and bool(update.get('details', {}).get('ajax_observation_ref')))
        checks['C_same_webdriver_continuous'] = (nav['status'] == update['status'] == 'COMPLETE' and
            nav.get('browser_session_id') == update.get('browser_session_id') == continuous.metadata['browser_session_id'])
        checks['C_popup_observed'] = update.get('details', {}).get('dialog_visible') is True
        checks['arbitrary_python_rejected'] = all(continuous.execute_checked(source)['status'] == 'REJECTED'
            for source in ["import os", "open('sentinel')", "print('even harmless arbitrary source is disabled')"])
        checks['unknown_script_rejected'] = continuous.execute_trusted('../../unreviewed.py')['status'] == 'REJECTED'
        checks['C_cleanup'] = cleaned(continuous)

        trial = new()
        trial_result = trial.execute_trusted('smoke')
        runs['trial'] = trial_result
        checks['D_trial_complete'] = trial_result['status'] == 'COMPLETE'
        checks['D_trial_cleanup'] = cleaned(trial)
        formal = new('formal')
        formal_result = formal.execute_trusted('smoke')
        runs['formal'] = formal_result
        checks['D_formal_complete'] = formal_result['status'] == 'COMPLETE'
        checks['D_identical_source'] = trial_result['script_sha256'] == formal_result['script_sha256'] == REVIEWED_SCRIPT_SHA256
        checks['D_independent_browser_profile_and_backend'] = all(trial.metadata[k] != formal.metadata[k]
            for k in ('browser_session_id', 'profile_id', 'data_id'))
        checks['D_equivalent_initial_data'] = (trial.seed == formal.seed and
            trial.metadata['initial_submissions'] == formal.metadata['initial_submissions'] == 0 and
            trial_result.get('submissions') == formal_result.get('submissions') == ['Synthetic QA'])
        checks['D_no_cookie_or_storage_leak'] = all(r.get('details', {}).get('initial_storage_clean') is True
                                                   for r in (trial_result, formal_result))
        checks['D_independent_artifacts'] = trial.run_dir != formal.run_dir and trial.run_dir.parent.name == 'trial' and formal.run_dir.parent.name == 'formal'
        checks['D_formal_cleanup'] = cleaned(formal)
        reset = formal.reset_environment()
        sessions.append(reset)
        runs['reset'] = reset.execute_trusted('smoke')
        checks['reset_browser_and_backend'] = reset.metadata['initial_submissions'] == 0 and runs['reset']['status'] == 'COMPLETE'
        checks['reset_cleanup'] = cleaned(reset)

        for script, expected in [('locator_failure', 'NoSuchElementException'), ('wait_timeout', 'TimeoutException'),
                                 ('browser_exception', 'InvalidSessionIdException')]:
            session = new()
            outcome = session.execute_trusted(script)
            runs[script] = outcome
            checks['E_' + script + '_returned'] = outcome['status'] == 'ERROR' and outcome.get('error_type') == expected
            checks['E_' + script + '_cleanup'] = cleaned(session)

        timeout_session = new(limits=replace(RuntimeLimits(), execution_seconds=.7))
        runs['deadline'] = timeout_session.execute_trusted('deadline')
        checks['E_deadline_enforced'] = runs['deadline']['status'] == 'TIMEOUT'
        checks['E_deadline_tree_cleanup'] = cleaned(timeout_session)
        # Confirm the operation actually started before the controller terminated it.
        checks['E_deadline_started'] = 'probe started' in (timeout_session.run_dir / 'stdout.log').read_text(encoding='utf-8')
        lifespan = new(limits=replace(RuntimeLimits(), execution_seconds=10, session_seconds=1))
        runs['session_deadline'] = lifespan.execute_trusted('deadline')
        checks['E_session_deadline_enforced'] = runs['session_deadline']['status'] == 'TIMEOUT'
        checks['E_session_deadline_cleanup'] = cleaned(lifespan)
        result['status'] = 'PASSED' if all(checks.values()) else 'FAILED'
    except Exception as exc:
        result.update(status='ENVIRONMENT_UNAVAILABLE' if isinstance(exc, LocalRuntimeUnavailable) else 'FAILED',
                      error_type=type(exc).__name__, error=str(exc)[:300])
    finally:
        result['cleanup'] = [s.close_session() for s in sessions]
        if not all(c['process_tree_terminated'] and c['profile_removed'] for c in result['cleanup']):
            result['status'] = 'FAILED'
        (run_dir / 'acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='New directory inside artifacts/')
    parser.add_argument('--driver-path', type=Path)
    parser.add_argument('--chrome-binary', type=Path)
    parser.add_argument('--show-browser', action='store_true')
    args = parser.parse_args()
    output = args.output or ARTIFACT_ROOT / ('phase1_local_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6])
    result = run_acceptance(output, driver_path=args.driver_path, chrome_binary=args.chrome_binary,
                            headless=not args.show_browser)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'PASSED' else 2 if result['status'] == 'ENVIRONMENT_UNAVAILABLE' else 1


if __name__ == '__main__':
    raise SystemExit(main())
