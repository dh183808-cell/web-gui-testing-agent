"""Trusted local browser owner. This process is NOT a code sandbox.

Default mode accepts hash-pinned fixture programs. Explicit research mode also
interprets the checked Selenium subset; never executes arbitrary model bytecode.
"""
from __future__ import annotations

from contextlib import redirect_stdout, nullcontext, contextmanager
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback

from .dom_inspector import observe_session, safe_text, safe_url
from .fixture_server import runtime_fixture
from .selenium_runtime import make_driver, reviewed_script, TRUSTED_SCRIPT_IDS, CHECKED_PHASES, phase_domain


def checked_execution(driver, base_url, request, *, capture, save_screenshot, record_action=None, phase_event=None,
                      record_measurement=None):
    """Four measured stages; hard deadlines are enforced by the owning controller."""
    from .checked_selenium import (execute_source, collect_measurements, normalize_bindings, execution_diagnostics,
                                   check_navigation_state, NavigationStateError)
    started = time.monotonic()
    result = dict(status='ERROR', measurements={}, actions=[], oracle_log=[],
                  script_status='NOT_RUN', oracle_status='NOT_RUN',
                  timings=dict.fromkeys(CHECKED_PHASES,0.0), phase_status=dict.fromkeys(CHECKED_PHASES,'NOT_RUN'),
                  phase_limits=request.get('phase_limits',{}))
    progress, entry, current, stage = {}, None, None, 'preflight'
    active = 'pre_observe'

    def report():
        result['execution'] = execution_diagnostics(progress,result['actions'],stage=stage,entry=entry,current=current)
        return {k:v for k,v in result.items() if k != 'observation'}

    def oracle_progress(record):
        result['oracle_log'].append(record)
        if record_measurement:
            record_measurement(record)
        if phase_event:
            # Progress never starts a new phase or extends the existing deadline.
            # Send only the new record/value, not every prior long body on every
            # operation. The controller merges deltas for timeout diagnostics.
            partial = dict(oracle_progress=record)
            if record['event'] in {'observed','reused'}:
                partial['measurements'] = {record['oracle_id']:result['measurements'][record['oracle_id']]}
            phase_event('oracle_capture','progress',partial)

    @contextmanager
    def measured(name):
        nonlocal active
        active = name
        result['phase_status'][name] = 'RUNNING'
        if phase_event:
            phase_event(name,'started',report())
        mark = time.monotonic()
        try:
            yield
        except Exception:
            result['phase_status'][name] = 'ERROR'
            raise
        else:
            result['phase_status'][name] = 'COMPLETE'
        finally:
            result['timings'][name] = round(time.monotonic()-mark,3)
            if phase_event:
                phase_event(name,'finished',report())

    try:
        queries = normalize_bindings(request.get('queries', {}), step_id=request.get('step_id'))
        with measured('pre_observe'):
            entry = capture(max_elements=80,max_html=16000)
            result['before_url'] = entry['url']
            result.update(before_window=entry.get('current_window'), before_windows=entry.get('windows', []))
        stage = 'source'
        with measured('script_execute'):
            result['script_status'] = 'RUNNING'
            try:
                execute_source(request['source'],driver,base_url,actions=result['actions'],
                               on_action=record_action,progress=progress)
            except Exception:
                result['script_status'] = 'ERROR'
                raise
            result['script_status'] = 'COMPLETE'
        stage = 'evidence'
        with measured('oracle_capture'):
            result['oracle_status'] = 'RUNNING'
            try:
                result['measurements'] = collect_measurements(driver,base_url,queries,
                    step_id=request.get('step_id'),batch=True,
                    on_progress=oracle_progress,completed=result['measurements'])
            except Exception:
                result['oracle_status'] = 'ERROR'
                raise
            result['oracle_status'] = 'COMPLETE'
        stage = 'complete'
        result['status'] = 'COMPLETE'
    except Exception as exc:
        result.update(status='ERROR',error_type=type(exc).__name__,failed_phase=active,
            failure_domain=phase_domain(active),
            error=safe_text(str(exc).splitlines()[0] if str(exc) else type(exc).__name__,1200))
    # Source/Oracle progress survives a failed observation. Missing formal evidence
    # remains ERROR; it is never presented as a failed Selenium operation or a PASS.
    try:
        with measured('post_observe'):
            current = capture(max_elements=80,max_html=16000)
            active_changed = entry is not None and entry.get('current_window') != current.get('current_window')
            result.update(observation=current,after_url=current['url'],
                          after_window=current.get('current_window'), after_windows=current.get('windows', []),
                          active_window_changed=active_changed,
                          window_changed=active_changed or result.get('before_windows', []) != current.get('windows', []),
                          navigated=result.get('before_url') != current['url'] or active_changed)
            name = current['observation_id'] + '.png'
            if save_screenshot(name) is False:
                raise RuntimeError('Screenshot was not saved')
            current['screenshot_ref'] = name
            result['screenshot_ref'] = name
            for m in result['measurements'].values():
                m.update(observation_ref=current['observation_ref'],screenshot_ref=name)
                if 'calendar_evidence' in m:
                    # Bound to this capture; the controller adds owned session identity.
                    m['calendar_evidence']['provenance'] = dict(step_id=request.get('step_id'),
                        observation_ref=current['observation_ref'],screenshot_ref=name,
                        url=current['url'],window=current.get('current_window'))
    except Exception as exc:
        result['observation_error_type'] = type(exc).__name__
        if result['status'] == 'COMPLETE':
            result.update(status='ERROR',error_type=type(exc).__name__,failed_phase='post_observe',
                          failure_domain='observation',error='Post-execution observation/evidence persistence failed')
    if result['status'] == 'COMPLETE':
        try:
            # A popup can appear during evidence/observation after the script's
            # last poll. Use existing snapshots; no additional DOM scan or wait.
            check_navigation_state(driver,result['actions'],entry=entry,current=current)
        except NavigationStateError as exc:
            stage, progress['operation'] = 'source', 'navigation'
            result.update(status='ERROR',error_type=type(exc).__name__,error=str(exc),
                          failed_phase='post_observe',failure_domain='navigation')
    report()
    result.update(elapsed_seconds=round(time.monotonic()-started,3),browser_session_id=driver.session_id)
    return result


def main():
    # The controller assigns this waiting process to its Job before sending init.
    config = json.loads(sys.stdin.readline())
    run_dir = Path(config['run_dir']).resolve()
    profile = run_dir / 'profile'
    profile.mkdir()
    driver = None
    output = sys.stdout

    def reply(value):
        output.write(json.dumps(value, ensure_ascii=True) + '\n')
        output.flush()

    def capture(**options):
        observation = observe_session(driver, **options)
        ref = 'observation_' + observation['observation_id']
        (run_dir / (ref + '.html')).write_text(observation.pop('html'), encoding='utf-8')
        observation.update(html_ref=ref + '.html', observation_ref=ref + '.json')
        (run_dir / (ref + '.json')).write_text(json.dumps(observation, indent=2), encoding='utf-8')
        return observation

    def checked(request):
        def record_action(action):
            with (run_dir/'browser_actions.jsonl').open('a',encoding='utf-8') as trace:
                trace.write(json.dumps(dict(step_id=request.get('step_id'),
                    browser_session_id=driver.session_id,**action),ensure_ascii=True)+'\n')
        def record_measurement(record):
            with (run_dir/'oracle_capture.jsonl').open('a',encoding='utf-8') as trace:
                trace.write(json.dumps(dict(browser_session_id=driver.session_id,**record),ensure_ascii=True)+'\n')
        return checked_execution(driver,base_url,request,capture=capture,
            save_screenshot=lambda name: driver.save_screenshot(str(run_dir/name)),record_action=record_action,
            record_measurement=record_measurement,
            phase_event=lambda name,event,partial: reply(dict(_runtime_phase=name,event=event,partial=partial)))

    try:
        environment = (nullcontext((config['start_url'], {'submissions': []})) if config.get('start_url')
                       else runtime_fixture(seed=config['seed']))
        with environment as (base_url, state), \
                (run_dir / 'stdout.log').open('w', encoding='utf-8', buffering=1) as log, redirect_stdout(log):
            driver = make_driver(profile_dir=profile, driver_path=config.get('driver_path'),
                                 chrome_binary=config.get('chrome_binary'),
                                 service_log=run_dir / 'chromedriver.log', headless=config['headless'],
                                 page_load_strategy='eager' if config.get('research_mode') else 'normal')
            driver.get(base_url)
            # Trusted control-side diagnostic; do not expose full command-line paths.
            try:
                chrome_args = driver.execute_cdp_cmd('Browser.getBrowserCommandLine', {})['arguments']
                browser_headless = any(arg.startswith('--headless') for arg in chrome_args)
            except Exception:
                browser_headless = None  # Diagnostic availability must not break a session.
            reply({'status': 'READY', 'browser_session_id': driver.session_id,
                   'browser_headless': browser_headless,
                   'browser_version': driver.capabilities.get('browserVersion'),
                   'profile_id': str(profile), 'data_id': base_url,
                   'initial_submissions': len(state['submissions']), 'base_url': base_url})
            for line in sys.stdin:
                request = json.loads(line)
                op = request.get('op')
                if op == 'close':
                    driver.quit()
                    driver = None
                    reply({'status': 'CLOSED', 'driver_quit': True})
                    break
                if op == 'observe':
                    try:
                        observation = capture(**request.get('options', {}))
                        if request.get('screenshot'):
                            # Screenshots are opt-in; acceptance uses synthetic data only.
                            name = observation['observation_id'] + '.png'
                            driver.save_screenshot(str(run_dir / name))
                            observation['screenshot_ref'] = name
                            (run_dir / observation['observation_ref']).write_text(
                                json.dumps(observation, indent=2), encoding='utf-8')
                        reply({'status': 'COMPLETE', 'observation': observation})
                    except Exception as exc:
                        reply({'status': 'ERROR', 'error_type': type(exc).__name__,
                               'error': safe_text(str(exc).splitlines()[0], 300)})
                    continue
                if op == 'checked' and config.get('research_mode'):
                    reply(checked(request))
                    continue
                if op != 'execute' or request.get('script_id') not in TRUSTED_SCRIPT_IDS:
                    reply({'status': 'REJECTED', 'error_type': 'UntrustedCodeDisabled'})
                    continue
                started = time.monotonic()
                result = {'status': 'ERROR'}
                try:
                    path, digest = reviewed_script()
                    source = path.read_bytes().replace(b'\r\n', b'\n')
                    if hashlib.sha256(source).hexdigest() != digest:
                        raise PermissionError('Reviewed script changed before loading')
                    namespace = {'__name__': 'reviewed_fixture', '__file__': str(path)}
                    # Only this fixed, hash-checked repository asset is executable.
                    # Raw source from control requests is never compiled or executed.
                    exec(compile(source, str(path), 'exec'), namespace)
                    from types import SimpleNamespace
                    context = SimpleNamespace(driver=driver, base_url=base_url, state=state,
                                              capture=capture, run_dir=run_dir)
                    result.update(status='COMPLETE', details=namespace[request['script_id']](context))
                except Exception as exc:
                    result.update(status='ERROR', error_type=type(exc).__name__,
                                  error=safe_text(str(exc).splitlines()[0] if str(exc) else type(exc).__name__, 300))
                    # No locals/environment/raw Selenium response in diagnostics.
                    with (run_dir / 'stderr.log').open('a', encoding='utf-8') as errors:
                        errors.write(type(exc).__name__ + ': ' + result['error'] + '\n')
                        errors.writelines(f'{Path(f.filename).name}:{f.lineno} in {f.name}\n'
                                          for f in traceback.extract_tb(exc.__traceback__))
                try:
                    result['observation'] = capture()
                    result['current_url'] = safe_url(driver.current_url)
                except Exception as exc:
                    result['observation_error_type'] = type(exc).__name__
                result.update(elapsed_seconds=round(time.monotonic() - started, 3),
                              submissions=list(state['submissions']), browser_session_id=driver.session_id)
                reply(result)
    except Exception as exc:
        reply({'status': 'ERROR', 'error_type': type(exc).__name__,
               'error': safe_text(str(exc).splitlines()[0] if str(exc) else type(exc).__name__, 300)})
    finally:
        if driver is not None:
            driver.quit()


if __name__ == '__main__':
    main()
