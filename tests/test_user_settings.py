"""User credentials, trusted presentation pacing and frozen-plan dependencies."""
import copy
import hashlib
import json
from pathlib import Path
import threading
import uuid
from types import SimpleNamespace

import pytest

from web_agent import llm_adapter as adapter
from web_agent import selenium_runtime as runtime
from web_agent import ui_tasks as ui
from web_agent.schema import dependency_issues, validate_dynamic_plan
from web_agent.agents.reviewer import Agent2TestReviewer


def test_ui_requires_explicit_secret_even_when_developer_environment_exists(tmp_path, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'synthetic-developer-key')
    store = ui.TaskStore(tmp_path, runner=lambda **kw: pytest.fail('must not execute'))
    with pytest.raises(ValueError, match='不会使用后台'):
        store.start(next(iter(ui.DEMO_PRESETS.values())), request_id=uuid.uuid4().hex,
                    ai_config=dict(provider='deepseek', model='chosen'))
    assert not store.jobs and not list(tmp_path.glob('*.json'))


@pytest.mark.parametrize('provider', adapter.PROVIDERS)
def test_task_uses_explicit_model_and_credentials_without_persisting_them(tmp_path, provider):
    done = threading.Event()
    received = []
    secret = 'synthetic-user-key-never-save'
    class Client:
        def __init__(self, **kw): received.append(kw)
        def close(self): done.set()
    def runner(**kw):
        assert isinstance(kw['llm'], Client)
        assert kw['provider'] == provider and kw['model'] == 'chosen/model'
        assert kw['headless'] is True and kw['formal_headless'] is False
        assert kw['formal_demo_delay'] == 2
        return dict(run_dir=str(tmp_path), verdict=dict(status='PASS'))
    store = ui.TaskStore(tmp_path, runner=runner, llm_factory=Client)
    key = store.start(dict(next(iter(ui.DEMO_PRESETS.values())), api_key=secret), request_id=uuid.uuid4().hex,
                      ai_config=dict(provider=provider, model='chosen/model'), api_key=secret, formal_demo_delay=2)
    assert done.wait(3)
    # Close occurs before final disk save; synchronize with the same store lock.
    with store.lock:
        job = store.snapshot(key)
        assert secret not in json.dumps(job)
    assert received[0]['api_key'] == secret and received[0]['use_environment'] is False
    for path in tmp_path.glob('*'):
        if path.is_file(): assert secret not in path.read_text(encoding='utf-8')


def test_public_defaults_read_only_public_dotenv_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, 'ROOT', tmp_path)
    (tmp_path/'.env').write_text('DEEPSEEK_API_KEY=synthetic-hidden\nDEEPSEEK_MODEL=chosen\n'
        'DEEPSEEK_BASE_URL=https://example.test/v1\n', encoding='utf-8')
    assert ui.provider_defaults('deepseek') == dict(model='chosen', base_url='https://example.test/v1')
    assert 'synthetic-hidden' not in json.dumps(ui.provider_defaults('deepseek'))


@pytest.mark.parametrize('delay', [-1, 3.1, float('nan'), float('inf'), True, '1'])
def test_presentation_delay_rejects_invalid_values(delay):
    with pytest.raises(ValueError): runtime.validate_presentation_delay(delay)


def test_merged_formal_activity_and_pause_events_have_nonconflicting_time_fields():
    assert ui.activity_text(dict(agent='Formal', action='started')) is None
    assert '正式浏览器测试与取证' in ui.activity_text(dict(agent='Agent4', action='started'))
    session = runtime.LocalSession.__new__(runtime.LocalSession)
    session.purpose='formal';session.formal_demo_delay=1.5;session.session_id='isolated'
    session.metadata=dict(browser_session_id='browser')
    session._pause_cancel=SimpleNamespace(wait=lambda delay: False)
    session._write=lambda *a: None
    events=[]
    def callback(action, **data):
        # The same reserved elapsed_seconds field used by Pipeline.event.
        events.append(dict(action=action, elapsed_seconds=10, **data))
    session.progress_callback=callback
    session.execute_checked=lambda *a, **kw: dict(status='COMPLETE')
    session.execute_program('# webagent-step: S1\ndriver.title\n', step_ids=['S1'], bindings={})
    assert [e['action'] for e in events]==['presentation_pause_started','presentation_pause_finished']
    assert 'duration_seconds' in events[-1]


@pytest.mark.parametrize('purpose,delay,expected_pauses', [('formal', 1.5, 2), ('formal', 0, 0), ('trial', 1.5, 0)])
def test_formal_pause_is_after_evidence_outside_phase_timing_including_final_page(purpose, delay, expected_pauses):
    sequence = []
    session = runtime.LocalSession.__new__(runtime.LocalSession)
    session.purpose = purpose
    session.formal_demo_delay = delay
    session.session_id = 'isolated'
    session.metadata = dict(browser_session_id='browser')
    session._pause_cancel = SimpleNamespace(wait=lambda value: sequence.append(('wait', value)) or False)
    session._write = lambda name, data: None
    session.progress_callback = lambda action, **kw: sequence.append((action, kw['step_id']))
    def execute(source, *, step_id, queries):
        sequence.append(('evidence', step_id))
        return dict(status='COMPLETE', timings=dict(script_execute=.01), measurements={
            step_id:dict(value='actual', screenshot_ref=step_id+'.png')})
    session.execute_checked = execute
    source = '# webagent-step: S1\ndriver.title\n# webagent-step: S2\ndriver.title\n'
    result = session.execute_program(source, step_ids=['S1', 'S2'], bindings={})
    assert result['script_sha256'] == hashlib.sha256(source.encode()).hexdigest()
    assert result['timings']['script_execute'] == .02
    assert set(result['measurements']) == {'S1', 'S2'}
    assert sum(s[0] == 'wait' for s in sequence) == expected_pauses
    if expected_pauses:
        assert [s[0] for s in sequence] == ['evidence', 'presentation_pause_started', 'wait',
            'presentation_pause_finished'] * 2
        assert all(s['presentation']['requested_seconds'] == delay for s in result['steps'])


def dependency_plan():
    return dict(requirement='Check OK and feedback', steps=[dict(id='S1', goal='Fill input'),
        dict(id='S2', goal='Check OK'), dict(id='S3', goal='Click OK', depends_on=['S1','S2']),
        dict(id='S4', goal='Read submitted feedback')], oracles=[dict(id='O1', step_id='S2',
            kind='equals', expected=True, meaning='OK exists', target=dict(role='button', name='OK'),
            source_quote='Check OK and feedback')])


def test_dependency_missing_outcome_link_is_rejected_before_frozen_execution():
    plan = dependency_plan()
    plan['provenance_version'] = 1
    with pytest.raises(ValueError, match='S4.*depends_on'):
        validate_dynamic_plan(plan, check_sources=False)
    plan['steps'][-1]['depends_on'] = ['S3']
    validate_dynamic_plan(plan, check_sources=False)
    assert not dependency_issues(plan)


def test_agent2_cannot_approve_incomplete_target_dependency_chain():
    model = SimpleNamespace(complete_json=lambda *a: dict(approved=True, issues=[], input_status='consistent'))
    plan = dependency_plan()
    before = copy.deepcopy(plan)
    decision = Agent2TestReviewer().review_dynamic(plan, llm=model)
    assert not decision['approved'] and 'S4' in decision['dependency_issues'][0]
    assert plan == before  # Never silently link independent work or change expectations.


@pytest.mark.parametrize('corrected', [True, False])
def test_structural_dependency_uses_existing_one_planning_revision_not_codegen(tmp_path, monkeypatch, corrected):
    from web_agent.pipeline import run_pipeline
    from copy import deepcopy
    monkeypatch.setattr(runtime, 'ARTIFACT_ROOT', tmp_path)
    monkeypatch.setattr(runtime, 'create_session', lambda *a, **kw: pytest.fail('must resolve planning first'))
    class Model:
        calls = []
        designs = 0
        def complete_json(self, system, user):
            data = json.loads(user)
            if system.startswith('You are Agent1'):
                self.designs += 1
                plan = dependency_plan()
                plan['oracles'][0]['source_type'] = 'user_requirement'
                if self.designs > 1:
                    assert data['review_feedback']
                    if corrected: plan['steps'][-1]['depends_on'] = ['S3']
                return deepcopy(plan)
            if system.startswith('You are Agent2'):
                return dict(approved=True, issues=[], input_status='consistent')
            pytest.fail('dependency issue must not reach Agent3')
    model = Model()
    result = run_pipeline(target='dynamic', mode='llm', start_url='https://authorized.test/',
        requirement='Check OK and feedback', allow_live_site=True, allow_local_research=True,
        only_generate=True, llm=model, output_root=tmp_path)
    assert model.designs == 2 and result['verdict']['status'] == 'INCONCLUSIVE'
    path = Path(result['run_dir'])
    assert (path/'test_contract.json').exists() == corrected
    assert not (path/'agent3_debug.json').exists()
    assert not (path/'agent6_diagnosis.json').exists()
    if not corrected: assert '步骤依赖不完整' in result['verdict']['issues'][0]
