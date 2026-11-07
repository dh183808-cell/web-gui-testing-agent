"""Trusted local browser owner. This process is NOT a code sandbox.

Default mode accepts hash-pinned fixture programs. Explicit research mode also
interprets the checked Selenium subset; never executes arbitrary model bytecode.
"""
from __future__ import annotations

from contextlib import redirect_stdout, nullcontext
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback

from .dom_inspector import observe_session, safe_text, safe_url
from .fixture_server import runtime_fixture
from .selenium_runtime import make_driver, reviewed_script, TRUSTED_SCRIPT_IDS


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
        from .checked_selenium import execute_source, collect_measurements, normalize_bindings
        started = time.monotonic()
        result = dict(status='ERROR', measurements={})
        try:
            queries = normalize_bindings(request.get('queries', {}), step_id=request.get('step_id'))
            execute_source(request['source'], driver, base_url)
            result['measurements'] = collect_measurements(driver, base_url, queries, step_id=request.get('step_id'))
            result['status'] = 'COMPLETE'
        except Exception as exc:
            result.update(status='ERROR', error_type=type(exc).__name__,
                          error=safe_text(str(exc).splitlines()[0] if str(exc) else type(exc).__name__, 1200))
        try:
            observation = capture(max_elements=80, max_html=16000)
            name = observation['observation_id'] + '.png'
            driver.save_screenshot(str(run_dir / name))
            observation['screenshot_ref'] = name
            result.update(observation=observation, screenshot_ref=name)
            for m in result['measurements'].values():
                m['observation_ref'] = observation['observation_ref']
                m['screenshot_ref'] = name
        except Exception as exc:
            result['observation_error_type'] = type(exc).__name__
        result.update(elapsed_seconds=round(time.monotonic()-started,3), browser_session_id=driver.session_id)
        return result

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
            reply({'status': 'READY', 'browser_session_id': driver.session_id,
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
