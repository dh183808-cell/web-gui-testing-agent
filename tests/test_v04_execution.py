"""Closed generation and worker safety tests; real Chrome cases require opt-in.

Run real local acceptance with WEBAGENT_REAL_CHROME=1. Default tests never
create Chrome. Model grounding is mocked even in real browser acceptance.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import time

import pytest

from web_agent.agents import Agent2TestReviewer, Agent3SeleniumGenerator
from web_agent.browser_worker import BrowserOwner
from web_agent.dynamic_execution import (ExecutionPolicy, ExecutionRejected, render_source,
                                         resolve_inputs, source_digest, validate_operation)
from web_agent.dynamic_schema import TestDesign as Design, requirement_id
from web_agent.pipeline import run_dynamic_contract
from .test_v04_planning import search_design, form_design, approval
from .test_v02_agent_upgrade import QueueLLM


def reviewed(factory=search_design, base='http://127.0.0.1:8765', path=None):
    raw = factory()
    old_url = raw['target_url']
    url = base + (path or ('/search' if factory is search_design else '/apply'))
    old_ids = [r['requirement_id'] for r in raw['requirements']]
    raw['requirement'] = raw['requirement'].replace(old_url, url)
    raw['target_url'] = url
    for row in raw['requirements']:
        row['requirement_id'] = requirement_id(raw['requirement'], row['source_quote'])
    new_ids = [r['requirement_id'] for r in raw['requirements']]
    for key in ('steps', 'oracles'):
        for row in raw[key]:
            row['requirement_ids'] = [new_ids[old_ids.index(r)] for r in row['requirement_ids']]
    names = {'S_open': '目录检索' if factory is search_design else '报名',
             'S_search': '执行检索', 'S_result': '条目标题', 'S_detail': '条目链接',
             'S_title': '详情标题', 'S_retained': '姓名输入框'}
    for row in raw['steps']:
        if row['step_id'] in names:
            row['target']['description'] = names[row['step_id']]
    contract, decision = Agent2TestReviewer().review_dynamic(Design.from_dict(raw),
        requirement=raw['requirement'], target_url=url, mode='template')
    assert decision['approved'], decision
    return contract


def observation():
    return {'observation_id': 'obs_1_example', 'elements': [
        {'node_id': 'n_1', 'tag': 'input', 'type': 'search', 'name': '搜索框',
         'text': '', 'visible': True, 'enabled': True, 'sensitive': False,
         'parent_id': None, 'form_id': None, 'options': []}], 'url': 'http://127.0.0.1:8765/search'}


def operation(step='S_query', **changes):
    result = {'step_id': step, 'observation_id': 'obs_1_example', 'target_id': 'n_1',
        'wait': 'enabled', 'reads': {}, 'expected_change': 'none', 'rationale': 'Visible accessible search input'}
    result.update(changes)
    return result


def test_generated_source_is_actual_action_specific_python(tmp_path):
    contract = reviewed()
    artifact = Agent3SeleniumGenerator().generate_dynamic(contract, 'S_query', observation(), tmp_path,
        state={}, policy=ExecutionPolicy(), llm=QueueLLM(operation()))
    assert 'element.clear()' in artifact['source']
    assert "element.send_keys(inputs['value'])" in artifact['source']
    assert artifact['audit']['source_sha256'] == source_digest(artifact['source'])
    assert artifact['audit']['contract_digest'] == contract.design_digest
    assert (tmp_path / 'dynamic_S_query.py').read_text(encoding='utf-8') == artifact['source']


@pytest.mark.parametrize('payload', [
    "import os; os.system('whoami')", {'code': 'open("x","w")'},
    operation(target_id='n_missing'), operation(wait='eval'), operation(expected_change='navigation'),
    operation(python='element.click()'), operation(step='S_detail'),
    operation(observation_id='old'), operation(reads={'secret': '__dict__'}),
])
def test_unsafe_or_semantically_unbound_model_output_rejected(payload, tmp_path):
    with pytest.raises((ExecutionRejected, ValueError)):
        Agent3SeleniumGenerator().generate_dynamic(reviewed(), 'S_query', observation(), tmp_path,
            state={}, policy=ExecutionPolicy(), llm=QueueLLM(payload))
    assert not list(tmp_path.glob('dynamic_*.py'))


def test_operation_cannot_change_reviewed_input_value(tmp_path):
    raw = operation(value='invented')
    with pytest.raises(ExecutionRejected):
        Agent3SeleniumGenerator().generate_dynamic(reviewed(), 'S_query', observation(), tmp_path,
            state={}, policy=ExecutionPolicy(), llm=QueueLLM(raw))


@pytest.mark.parametrize('url', ['https://example.com', 'file:///C:/secret', 'javascript:alert(1)',
                                'http://127.0.0.1:9999', 'http://user:pass@127.0.0.1:8765'])
def test_origin_policy_rejects_unreviewed_destinations(url):
    with pytest.raises((ExecutionRejected, ValueError)):
        ExecutionPolicy().authorize(url, 'http://127.0.0.1:8765/search')


def test_live_execution_requires_explicit_authorization():
    with pytest.raises(ExecutionRejected, match='allow_live_site'):
        ExecutionPolicy().authorize('https://catalog.example/search', 'https://catalog.example/search')


@pytest.mark.parametrize('option', [{'max_steps': True}, {'max_observations': 0}, {'step_timeout': float('nan')},
                                   {'total_timeout': 10000}, {'max_elements': 10000}])
def test_budgets_are_strictly_bounded(option):
    with pytest.raises(ValueError):
        ExecutionPolicy(**option)


class FakeOwnerDriver:
    session_id = 'one-fake-session'
    current_url = 'about:blank'
    window_handles = ['one']
    closed = False
    def set_page_load_timeout(self, seconds): pass
    def set_script_timeout(self, seconds): pass
    def execute_cdp_cmd(self, *args): pass
    def quit(self): self.closed = True
    @property
    def network(self): return self
    def add_request_handler(self, event, callback):
        self.callback = callback
        return 1


class FakeGuard:
    def __init__(self, driver, policy, target_url):
        self.blocked, self.errors = [], []
    def close(self): pass


@pytest.mark.parametrize('source', ["import os\n", "outputs = {}; api.__dict__\n", "open('stolen')\n",
                                   "while True: pass\n", "__import__('subprocess')\n"])
def test_worker_rejects_arbitrary_python_even_if_ast_is_valid(source):
    driver = FakeOwnerDriver()
    owner = BrowserOwner(reviewed(), ExecutionPolicy(), driver_factory=lambda **kw: driver, guard_factory=FakeGuard)
    owner.observation = observation()
    owner.state = {'S_open': {}}
    driver.current_url = 'http://127.0.0.1:8765/search'
    try:
        with pytest.raises(ExecutionRejected, match='canonical'):
            owner.execute({'step_id': 'S_query', 'operation': operation(), 'source': source})
    finally:
        owner.close()
    assert driver.closed


def test_worker_wont_start_without_network_interception():
    driver = FakeOwnerDriver()
    def fail(*args): raise RuntimeError('unsupported interception')
    with pytest.raises(RuntimeError):
        BrowserOwner(reviewed(), ExecutionPolicy(), driver_factory=lambda **kw: driver, guard_factory=fail)
    assert driver.closed


def test_worker_intercepts_off_origin_before_continuing():
    from web_agent.browser_network import OriginGuard
    guard = OriginGuard.__new__(OriginGuard)
    guard.policy, guard.target_url = ExecutionPolicy(), 'http://127.0.0.1:8765'
    guard.blocked = []
    calls = []
    guard._send = lambda *args, **kwargs: calls.append(args)
    guard._event({'method': 'Fetch.requestPaused', 'sessionId': 'session', 'params': {
        'requestId': 'req', 'request': {'url': 'http://127.0.0.1:9999/exfiltrate'}}})
    assert calls[0][0] == 'Fetch.failRequest'
    assert guard.blocked


class GroundingModel:
    """Generic mocked A3 selecting exact accessible semantic names in live DOM."""
    def __init__(self): self.calls = []
    def complete_json(self, system, user):
        payload = json.loads(user)
        self.calls.append(payload)
        step, obs = payload['step'], payload['observation']
        name = step['target']['description']
        candidates = [n for n in obs['elements'] if n['name'] == name]
        position = step['target']['position']
        if position:
            candidates = candidates[position-1:position]
        assert len(candidates) == 1, (name, candidates, obs)
        node = candidates[0]
        readers = {'text': 'text', 'value': 'value', 'boolean': 'checked', 'url': 'url', 'count': 'count'}
        expected = {e['output']: readers[e['kind']] for e in payload['evidence_requirements']}
        change = 'none'
        if step['action'] in ('click', 'submit'):
            change = 'navigation' if node['tag'] == 'a' or step['action'] == 'submit' else 'dom'
        return {'step_id': step['step_id'], 'observation_id': obs['observation_id'],
                'target_id': node['node_id'], 'wait': 'enabled' if step['action'] not in ('observe', 'wait') else 'visible',
                'reads': {key: expected.get(key, 'text') for key in step['outputs']},
                'expected_change': change, 'rationale': 'Exact accessible name and requested ordinal in live observation'}


REAL = pytest.mark.skipif(os.getenv('WEBAGENT_REAL_CHROME') != '1', reason='Real Chrome acceptance is opt-in')


@REAL
@pytest.mark.parametrize('factory', [search_design, form_design])
def test_real_chrome_two_dynamic_workflows(factory, tmp_path):
    from web_agent.dynamic_fixture import dynamic_fixture
    from web_agent.agents import Agent4TestExecutor
    with dynamic_fixture() as (base, _):
        contract = reviewed(factory, base)
        model = GroundingModel()
        result = run_dynamic_contract(contract, tmp_path, llm=model,
            policy=ExecutionPolicy(step_timeout=10, total_timeout=90))
    assert result['execution_status'] == 'COMPLETE', result
    assert result['verdict']['status'] == 'PASS'
    assert result['session_closed'] is True
    assert len({r['session_id'] for r in result['steps']}) == 1
    assert result['observations'] == len(contract.design.steps) + 1
    for outcome in result['steps']:
        source = (tmp_path / outcome['source_file']).read_text(encoding='utf-8')
        assert source_digest(source) == outcome['source_sha256']
        assert outcome['api_trace']
    if factory is search_design:
        assert result['evidence']['E_result']['value'] == '机器人乙'
        assert result['evidence']['E_detail']['value'] == '机器人乙'
        assert '/detail/two' in result['steps'][-1]['url']
    else:
        assert result['evidence']['E_preview']['value'] == '张三'
        assert result['evidence']['E_retained']['value'] == '张三'
        assert any('Select.select_by_visible_text' in call for r in result['steps'] for call in r['api_trace'])
    # Parent worker is closed and generated files/evidence remain reviewable.
    assert (tmp_path / 'dynamic_execution.json').exists()
    observations = [json.loads(p.read_text(encoding='utf-8')) for p in tmp_path.glob('observation_*.json')]
    assert len({o['observation_id'] for o in observations}) == len(observations)
    assert 'never-export' not in json.dumps(observations)
    if factory is search_design:
        assert any(n['name'] == '会话状态' and n['text'] == '同一会话'
                   for n in observations[-1]['elements'])
    else:
        assert any(n['name'] == '预览城市' and n['text'] == '杭州'
                   for obs in observations for n in obs['elements'])


@pytest.mark.parametrize('case', ['no_observation', 'dependencies', 'duplicate', 'stale'])
def test_worker_requires_fresh_observation_and_completed_dependencies(case):
    driver = FakeOwnerDriver()
    driver.current_url = 'http://127.0.0.1:8765/search'
    owner = BrowserOwner(reviewed(), ExecutionPolicy(), driver_factory=lambda **kw: driver, guard_factory=FakeGuard)
    artifact = {'step_id': 'S_query', 'operation': operation(),
                'source': render_source(owner.contract, operation())}
    owner.observation = observation() if case != 'no_observation' else None
    owner.state = {} if case == 'dependencies' else {'S_open': {}}
    if case == 'duplicate':
        owner.state['S_query'] = {}
    if case == 'stale':
        artifact['operation'] = operation(observation_id='stale')
    try:
        with pytest.raises(ExecutionRejected):
            owner.execute(artifact)
    finally:
        owner.close()
    assert driver.closed


def test_first_of_equivalent_nodes_cannot_satisfy_second_requirement():
    obs = observation()
    first = dict(obs['elements'][0], tag='h2', type='', name='条目标题')
    second = dict(first, node_id='n_2')
    obs['elements'] = [first, second]
    op = operation('S_result', reads={'title': 'text'}, wait='visible')
    with pytest.raises(ExecutionRejected, match='ordinal'):
        validate_operation(reviewed(), 'S_result', obs, op, {}, ExecutionPolicy())
    validate_operation(reviewed(), 'S_result', obs, dict(op, target_id='n_2'), {}, ExecutionPolicy())


class RecordingSession:
    def __init__(self):
        self.closed = False
        self.info = {'session_id': 'fake'}
        self.executed = []
        self.observed = 0
    def observe(self, **kwargs):
        self.observed += 1
        return observation()
    def execute(self, artifact, **kwargs):
        self.executed.append(artifact)
        return {'step_id': artifact['step_id'], 'status': 'COMPLETE', 'outputs': {}, 'evidence': {}}
    def close(self): self.closed = True


def test_pipeline_times_out_generation_closes_session_and_does_not_execute_late_result(monkeypatch, tmp_path):
    from threading import Event
    from web_agent.agents import Agent4TestExecutor
    released, finished = Event(), Event()
    session = RecordingSession()
    monkeypatch.setattr(Agent4TestExecutor, 'open_dynamic', lambda *a, **kw: session)
    def slow(*a, **kw):
        released.wait(2)
        finished.set()
        raise ValueError('late response')
    monkeypatch.setattr(Agent3SeleniumGenerator, 'generate_dynamic', slow)
    try:
        result = run_dynamic_contract(reviewed(), tmp_path, policy=ExecutionPolicy(step_timeout=0.08))
        assert result['execution_status'] == 'ERROR'
        assert result['error']['type'] == 'TimeoutError'
        assert session.closed and not session.executed
        assert result['step_states']['S_open'] == 'ERROR'
        assert result['step_states']['S_query'] == 'SKIPPED'
    finally:
        released.set()
        assert finished.wait(2)
    assert not session.executed


@pytest.mark.parametrize('case', ['steps', 'observations', 'source_tamper', 'total_timeout'])
def test_pipeline_budget_and_tamper_failure_stop_and_cleanup(case, monkeypatch, tmp_path):
    from web_agent.agents import Agent4TestExecutor
    session = RecordingSession()
    opened = []
    def open_session(*a, **kw):
        opened.append(True)
        if case == 'total_timeout':
            time.sleep(0.03)
        return session
    monkeypatch.setattr(Agent4TestExecutor, 'open_dynamic', open_session)
    actual = Agent3SeleniumGenerator.generate_dynamic
    def generate(self, *a, **kw):
        artifact = actual(self, *a, **kw)
        if case == 'source_tamper':
            (Path(a[3]) / artifact['source_file']).write_text('import os\n', encoding='utf-8')
        return artifact
    monkeypatch.setattr(Agent3SeleniumGenerator, 'generate_dynamic', generate)
    policy = ExecutionPolicy(**{'steps': {'max_steps': 1}, 'observations': {'max_observations': 1},
        'source_tamper': {}, 'total_timeout': {'total_timeout': 0.01}}[case])
    result = run_dynamic_contract(reviewed(), tmp_path, policy=policy, mode='template')
    assert result['execution_status'] == 'ERROR' and result['verdict']['status'] == 'ERROR'
    assert result['session_closed']
    assert len(session.executed) == (1 if case == 'observations' else 0)
    assert (not opened if case == 'steps' else session.closed)


def test_browser_transport_timeout_terminates_worker_tree(monkeypatch, tmp_path):
    from io import StringIO
    from queue import Queue
    from web_agent.browser_session import BrowserSession
    import web_agent.browser_session as transport
    class Process:
        pid = 12345678
        stdin, stdout = StringIO(), StringIO()
        stopped = False
        def poll(self): return 0 if self.stopped else None
        def wait(self, **kw): self.stopped = True
    session = BrowserSession.__new__(BrowserSession)
    session.process, session.closed, session.messages = Process(), False, Queue()
    session.deadline = time.monotonic() + 3
    session.log = (tmp_path / 'transport.log').open('w')
    calls = []
    monkeypatch.setattr(transport.subprocess, 'run', lambda args, **kw: calls.append(args))
    monkeypatch.setattr(transport.os, 'killpg', lambda *args: calls.append(args), raising=False)
    with pytest.raises(TimeoutError, match='terminated'):
        session._rpc({'command': 'observe'}, 0.01)
    assert session.closed and session.process.stopped
    assert calls and str(session.process.pid) in str(calls)


@pytest.mark.parametrize('change', [{'tag': 'input', 'type': 'password', 'private': True},
                                  {'tag': 'input', 'type': 'file', 'private': True},
                                  {'tag': 'input', 'type': 'text', 'private': False}])
def test_live_control_mutation_is_rejected(change):
    from web_agent.browser_worker import SeleniumCapabilities
    class Node:
        def is_displayed(self): return True
        def is_enabled(self): return True
    class Driver:
        def execute_script(self, *a): return change
    class Owner:
        driver, refs, observation = Driver(), {'n_1': Node()}, observation()
    with pytest.raises(ExecutionRejected, match='changed identity'):
        SeleniumCapabilities(Owner(), 1).wait('n_1', 'enabled')


def test_pipeline_explicit_execution_receives_frozen_contract_and_records_a3_a4(monkeypatch, tmp_path):
    import web_agent.pipeline as pipe
    contract = reviewed()
    raw = contract.design.to_dict()
    calls = []
    def execute(frozen, run_dir, **kwargs):
        frozen.assert_integrity()
        calls.append((frozen.design_digest, kwargs))
        return {'execution_status': 'COMPLETE', 'steps': [{'step_id': 'S_open'}],
                'verdict': {'status': 'INCONCLUSIVE'}, 'session_closed': True}
    def forbidden(*a, **kw): raise AssertionError('Unexpected legacy execution or Agent5/6')
    monkeypatch.setattr(pipe, 'run_dynamic_contract', execute)
    for name in ('local_fixture', 'inspect_page', '_run_script', '_run_probe', 'html_report'):
        monkeypatch.setattr(pipe, name, forbidden)
    for cls in (pipe.Agent5EvidenceVerifier, pipe.Agent6RecoveryController):
        monkeypatch.setattr(cls, '__init__', forbidden)
    result = pipe.run_pipeline(engine='dynamic', execute_dynamic=True, mode='llm',
        requirement=raw['requirement'], target_url=raw['target_url'],
        llm=QueueLLM(raw, approval(raw)), output_root=tmp_path)
    assert calls[0][0] == contract.design_digest
    assert result['execution_status'] == 'COMPLETE' and result['verdict']['status'] == 'INCONCLUSIVE'
    assert [s['agent'] for s in result['stages']] == ['Agent1', 'Agent2', 'Agent3', 'Agent4', 'Agent5', 'Agent6']
    assert (Path(result['run_dir']) / 'dynamic_result.json').exists()


def test_previous_outputs_are_resolved_without_model_rewriting_inputs(tmp_path):
    from .test_v04_planning import review
    raw = reviewed().design.to_dict()
    raw['steps'][0]['outputs'] = ['seed']
    raw['steps'][1]['inputs']['value'] = {'kind': 'step_output', 'step_id': 'S_open', 'output': 'seed'}
    contract, decision = review(raw, mode='template')
    assert decision['approved']
    with pytest.raises(ExecutionRejected, match='previous output'):
        resolve_inputs(contract, contract.design.steps[1], {})
    model = QueueLLM(operation())
    result = Agent3SeleniumGenerator().generate_dynamic(contract, 'S_query', observation(), tmp_path,
        state={'S_open': {'seed': '机器人'}}, policy=ExecutionPolicy(), llm=model)
    assert json.loads(model.calls[0]['user'])['referenced_outputs'] == {'value': '机器人'}
    assert "inputs['value']" in result['source'] and '机器人' not in result['source']


@REAL
@pytest.mark.parametrize('path', ['/redirect-out', '/image-out', '/worker-out'])
def test_real_chrome_cross_origin_and_child_worker_blocked_before_transmission(path, tmp_path):
    from web_agent.dynamic_fixture import dynamic_fixture
    with dynamic_fixture() as (outside, sink), dynamic_fixture() as (base, server):
        server.external_origin = outside
        result = run_dynamic_contract(reviewed(base=base, path=path), tmp_path,
            llm=GroundingModel(), policy=ExecutionPolicy(step_timeout=8, total_timeout=40))
        assert result['execution_status'] == 'ERROR'
        assert result['session_closed']
        assert sink.blocked_hits == 0
        assert result['verdict']['status'] == 'ERROR'
        assert 'Network policy violation' in result['error']['message'], result
