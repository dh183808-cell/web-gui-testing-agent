"""Offline control tests; only the opt-in live test proves Chrome execution."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import pytest

from web_agent import selenium_runtime as runtime
from web_agent.dom_inspector import compact_dom, observe_session
from web_agent.fixture_server import runtime_fixture


def bare_session(tmp_path):
    session = object.__new__(runtime.LocalSession)
    session.session_id, session.purpose, session.run_dir = 'test', 'trial', tmp_path
    session.cleanup, session._last_url, session._last_observation_ref = {}, 'http://fixture/', None
    session.limits = runtime.RuntimeLimits()
    session._request = lambda *a: pytest.fail('Unreviewed code reached Worker')
    return session


@pytest.mark.parametrize('source', [
    'import os', "open('/private')", "exec('pass')", "eval('1')", 'print("harmless")',
    'driver.get("http://example.test")', 'while True: pass', '', 'driver.find_element(By.ID, "name")'])
def test_all_arbitrary_python_is_disabled_locally(tmp_path, source):
    result = bare_session(tmp_path).execute_checked(source)
    assert result['status'] == 'REJECTED' and result['error_type'] == 'UntrustedCodeDisabled'
    assert not list(tmp_path.glob('*.py'))


def test_script_id_cannot_select_arbitrary_file(tmp_path):
    for script_id in ['../unreviewed.py', 'import os', 'generated.py']:
        result = bare_session(tmp_path).execute_trusted(script_id)
        assert result['status'] == 'REJECTED'


def test_changed_trusted_source_fails_before_worker(tmp_path, monkeypatch):
    session = bare_session(tmp_path)
    monkeypatch.setattr(runtime, 'REVIEWED_SCRIPT_SHA256', '0' * 64)
    cleaned = []
    session.close_session = lambda **kw: cleaned.append(kw)
    result = session.execute_trusted('smoke')
    assert result['status'] == 'ERROR' and result['error_type'] == 'PermissionError'
    assert cleaned and not list(tmp_path.glob('reviewed_script.py'))


def test_worker_env_does_not_inherit_synthetic_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('WEBAGENT_SYNTHETIC_CREDENTIAL', 'synthetic-only')
    monkeypatch.setenv('OPENAI_SYNTHETIC_TEST_KEY', 'synthetic-only')
    env = runtime._worker_environment(tmp_path)
    assert 'WEBAGENT_SYNTHETIC_CREDENTIAL' not in env
    assert 'OPENAI_SYNTHETIC_TEST_KEY' not in env
    assert env['TEMP'] == str(tmp_path)


def test_controller_timeout_closes_owned_worker(tmp_path):
    session = bare_session(tmp_path)
    def request(*args):
        raise TimeoutError('synthetic control deadline')
    session._request = request
    closed = []
    def close(**options):
        closed.append(options)
        session.cleanup.update(process_tree_terminated=True, owned_processes_remaining=0)
    session.close_session = close
    result = session.execute_trusted('deadline')
    assert result['status'] == 'TIMEOUT' and result['error_type'] == 'TimeoutError'
    assert closed == [{'graceful': False}]
    assert result['cleanup']['process_tree_terminated']


@pytest.mark.parametrize('options', [{'execution_seconds': 0}, {'execution_seconds': float('nan')},
                                    {'session_seconds': float('inf')}, {'step_attempts': 4},
                                    {'step_attempts': 1.5}, {'task_repairs': 6}])
def test_invalid_limits_rejected(options):
    with pytest.raises(ValueError):
        runtime.RuntimeLimits(**options)


def test_output_directory_cannot_escape_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'ARTIFACT_ROOT', tmp_path / 'artifacts')
    with pytest.raises(ValueError, match='inside artifacts'):
        runtime.create_session(tmp_path / 'outside')


def test_reviewed_asset_hash_matches_repository_source():
    path, digest = runtime.reviewed_script()
    assert path.is_file() and len(digest) == 64


def test_reviewed_hash_survives_windows_checkout_line_endings(tmp_path, monkeypatch):
    original, digest = runtime.reviewed_script()
    copied = tmp_path / 'tests' / 'fixtures' / 'runtime_smoke.py'
    copied.parent.mkdir(parents=True)
    copied.write_bytes(original.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))
    monkeypatch.setattr(runtime, 'ROOT', tmp_path)
    assert runtime.reviewed_script()[1] == digest


def test_missing_driver_does_not_trigger_download_or_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'ARTIFACT_ROOT', tmp_path)
    monkeypatch.delenv('CHROMEDRIVER_PATH', raising=False)
    monkeypatch.setattr(runtime.shutil, 'which', lambda name: None)
    monkeypatch.setattr(runtime.subprocess, 'Popen', lambda *a, **k: pytest.fail('Unexpected child or download'))
    with pytest.raises(runtime.LocalRuntimeUnavailable, match='automatic downloads are disabled'):
        runtime.create_session(tmp_path / 'no-driver')


def test_fixture_reset_is_backend_reset_not_just_browser_storage():
    from urllib.parse import urlsplit
    def submit(url):
        p = urlsplit(url)
        conn = http.client.HTTPConnection(p.hostname, p.port)
        conn.request('POST', '/submit', body='synthetic')
        response = conn.getresponse()
        value = json.loads(response.read())
        conn.close()
        return value
    with runtime_fixture(seed='same-seed') as (url, first):
        assert submit(url)['count'] == 1
        assert submit(url)['count'] == 2
    with runtime_fixture(seed='same-seed') as (_, second):
        assert second['seed'] == first['seed']
        assert second['submissions'] == []


def test_dom_relevance_redaction_and_truncation():
    html = '''<title>safe</title><script>password=bad</script><!-- token=bad -->
    <input type=password value="do-not-save"><input type=hidden value="hidden-data">
    <input name=api_key value="credential"><textarea>private free text</textarea>
    ''' + '<button>noise</button>' * 20 + '''
    <section id=task><label for=target>Target</label><input id=target value="private">
    <button>Very long button text</button><a href="https://u:p@example.test/p?token=credential#secret">Go</a></section>'''
    result = compact_dom(html, 2, region_css='#task', max_text=5)
    assert result['elements'][0]['id'] == 'target'
    assert result['elements'][0]['label'] == 'Targe'
    assert result['elements'][0]['parent']['id'] == 'task'
    assert result['truncated'] and result['untrusted_page_data']
    full = json.dumps(compact_dom(html))
    for secret in ['do-not-save', 'hidden-data', 'credential', 'private free text', 'u:p@']:
        assert secret not in full


def test_observe_existing_session_detects_same_url_change_without_quit():
    class Driver:
        page_source = '<title>Fixture</title><p>Before</p>'
        current_url = 'http://127.0.0.1/form'
        title, current_window_handle, window_handles = 'Fixture', 'window-1', ['window-1']
        def find_elements(self, *args):
            return []
        def execute_script(self, source):
            return True if 'window.top' in source else 'complete'
        def quit(self):
            pytest.fail('Inspector closed an existing session')
    driver = Driver()
    before = observe_session(driver)
    driver.page_source = '<title>Fixture</title><p>After AJAX</p>'
    after = observe_session(driver)
    assert before['url'] == after['url']
    assert before['state_hash'] != after['state_hash']
    assert after['frame_context'] == 'top'


def test_driver_configuration_keeps_chrome_sandbox(monkeypatch, tmp_path):
    from selenium import webdriver
    captured = {}
    class Driver:
        def set_page_load_timeout(self, value):
            pass
    def create(**kwargs):
        captured.update(kwargs)
        return Driver()
    monkeypatch.setattr(webdriver, 'Chrome', create)
    runtime.make_driver(profile_dir=tmp_path / 'profile', proxy_url='http://127.0.0.1:8766')
    args = captured['options'].arguments
    assert '--no-sandbox' not in args
    assert '--proxy-bypass-list=<-loopback>' in args
    assert f'--user-data-dir={tmp_path / "profile"}' in args
