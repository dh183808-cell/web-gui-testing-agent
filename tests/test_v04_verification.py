"""Stage 3: provenance-bound oracles, read probes, method-only bounded recovery."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import time

import pytest

from web_agent.agents import Agent2TestReviewer, Agent3SeleniumGenerator, Agent4TestExecutor, Agent5EvidenceVerifier, Agent6RecoveryController
from web_agent.dynamic_schema import TestDesign as Design, requirement_id
from web_agent.dynamic_execution import ExecutionPolicy, ExecutionRejected, source_digest
from web_agent.dynamic_verification import (make_record, merge_dynamic, validate_probe, validate_record,
    artifact_path, timestamp, compare, numeric)
from web_agent.pipeline import run_dynamic_contract
from .test_v04_execution import reviewed, observation, operation, GroundingModel, REAL
from .test_v04_planning import form_design, search_design


def records(contract, values=None, *, missing=()):
    values = values or {}
    return {e.evidence_id: make_record(contract, e, value=None if e.evidence_id in missing else values.get(e.evidence_id, '机器人乙'),
        status='missing' if e.evidence_id in missing else 'observed',
        reason='Read failed' if e.evidence_id in missing else None, url=contract.design.target_url,
        observation_id='obs_1', session_id='same_session', source_sha256='a' * 64)
        for e in contract.design.evidence_requirements}


def one_oracle(kind='text', relation='equals', expected='机器人乙', action='observe', base='http://127.0.0.1:8765', path='/checks'):
    raw = '在 ' + base + path + ' 观察检查目标，确认结果为机器人乙或 true 或 2，并验证下载文件。'
    rid = requirement_id(raw, '确认结果')
    design = {'protocol_version': '0.4', 'document_type': 'test_design', 'requirement': raw,
        'target_url': base + path, 'test_goal': 'Verify the reviewed property',
        'requirements': [{'requirement_id': rid, 'text': '确认结果', 'source_quote': '确认结果', 'kind': 'assertion'}],
        'steps': [
            {'step_id': 'S_open', 'requirement_ids': [rid], 'action': 'navigate',
             'target': {'description': '检查页面', 'role': 'page', 'position': None},
             'depends_on': [], 'precondition_ids': [], 'inputs': {}, 'outputs': []},
            {'step_id': 'S_check', 'requirement_ids': [rid], 'action': action,
             'target': {'description': '检查目标', 'role': 'semantic_control', 'position': None},
             'depends_on': ['S_open'], 'precondition_ids': [], 'inputs': {}, 'outputs': ['observed']}],
        'preconditions': [], 'test_data': [], 'assumptions': [], 'missing_information': [], 'unsupported_requests': [],
        'evidence_requirements': [{'evidence_id': 'E_check', 'step_id': 'S_check', 'output': 'observed', 'description': '检查目标', 'kind': kind}],
        'oracles': [{'oracle_id': 'O_check', 'requirement_ids': [rid], 'after_step_id': 'S_check',
            'relation': relation, 'actual': {'kind': 'evidence', 'evidence_id': 'E_check'},
            'expected': {'kind': 'literal', 'value': expected}, 'evidence_ids': ['E_check']}]}
    contract, decision = Agent2TestReviewer().review_dynamic(Design.from_dict(design),
        requirement=raw, target_url=base + path, mode='template')
    assert decision['approved'], decision
    return contract


@pytest.mark.parametrize('kind,relation,actual,expected', [
    ('text', 'equals', '机器人乙', '机器人乙'), ('text', 'not_equals', '机器人甲', '机器人乙'),
    ('text', 'contains', '前缀机器人乙后缀', '机器人乙'), ('text', 'normalized_equals', '  机器人乙  ', '机器人乙'),
    ('count', 'greater_than', 3, 2), ('number', 'less_than', '1.5', 2),
    ('number', 'greater_or_equal', '2.0', 2), ('value', 'less_or_equal', '2', 2),
    ('exists', 'boolean_equals', True, True), ('visible', 'equals', True, True),
    ('enabled', 'equals', True, True), ('selected', 'equals', True, True),
    ('page_title', 'contains', '机器人乙 — 检查页面', '机器人乙'),
])
def test_dynamic_oracle_registry(kind, relation, actual, expected):
    contract = one_oracle(kind, relation, expected)
    verdict = Agent5EvidenceVerifier().verify_dynamic(contract, records(contract, {'E_check': actual}))
    assert verdict.status == 'PASS', verdict
    check = verdict.checks[0]
    assert check['oracle_id'] == 'O_check' and check['evidence_ids'] == ['E_check']
    assert check['step_id'] == 'S_check' and check['evidence_attempts'] == {'E_check': 1}


@pytest.mark.parametrize('value', [True, 'nan', 'inf', '', '__import__("os")', {}, None])
def test_numeric_comparisons_do_not_coerce_invalid_values(value):
    with pytest.raises(ValueError):
        numeric(value)


def test_cross_page_failure_dominates_missing_evidence_and_execution_error():
    contract = reviewed(form_design)
    evidence = records(contract, {'E_preview': '错误姓名'}, missing=['E_retained'])
    for status in ('COMPLETE', 'ERROR'):
        verdict = Agent5EvidenceVerifier().verify_dynamic(contract, evidence, execution_status=status)
        assert verdict.status == 'FAIL'
        assert [c['passed'] for c in verdict.checks] == [False, None]
        assert Agent5EvidenceVerifier().plan_dynamic_probe(contract, evidence, verdict) is None


def test_cross_page_reference_and_four_statuses():
    contract = reviewed()
    evidence = records(contract)
    verify = Agent5EvidenceVerifier().verify_dynamic
    assert verify(contract, evidence).status == 'PASS'
    evidence['E_detail']['value'] = 'different'
    assert verify(contract, evidence).status == 'FAIL'
    del evidence['E_detail']
    assert verify(contract, evidence).status == 'INCONCLUSIVE'
    assert verify(contract, evidence, execution_status='ERROR').status == 'ERROR'
    assert verify(contract, evidence, execution_status='NOT_RUN').status == 'INCONCLUSIVE'


@pytest.mark.parametrize('field,value', [('contract_digest', 'b' * 64), ('step_id', 'S_wrong'),
    ('evidence_id', 'E_wrong'), ('attempt', True), ('source', 'model'), ('collected_at', 'today'),
    ('session_id', ''), ('url', 'https://outside.example'), ('source_sha256', 'x'), ('artifacts', ['../escape'])])
def test_malformed_or_foreign_evidence_is_not_trusted(field, value):
    contract = reviewed()
    evidence = records(contract)
    evidence['E_detail'][field] = value
    verdict = Agent5EvidenceVerifier().verify_dynamic(contract, evidence)
    assert verdict.status == 'INCONCLUSIVE'
    assert verdict.checks[0]['passed'] is None


def test_empty_text_false_zero_are_observations_not_missing():
    for kind, actual, expected in [('exists', False, True), ('count', 0, 2), ('text', '', '机器人乙')]:
        contract = one_oracle(kind, expected=expected)
        assert Agent5EvidenceVerifier().verify_dynamic(contract, records(contract, {'E_check': actual})).status == 'FAIL'


@pytest.mark.parametrize('content,filename,passed', [(b'hello', 'report.txt', True),
    (b'', 'report.txt', False), (b'not a pdf', 'report.pdf', False),
    (b'%PDF-1.4\n%%EOF', 'report.pdf', True), (b'{"x":1}', 'report.json', True),
    (b'{broken', 'report.json', False), (b'partial', 'report.crdownload', False)])
def test_file_completion_and_basic_validity(tmp_path, content, filename, passed):
    contract = one_oracle('file', 'file_valid', True, action='download')
    path = tmp_path / filename
    path.write_bytes(content)
    value = {'path': filename, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest(), 'complete': True}
    evidence = records(contract, {'E_check': value})
    evidence['E_check']['artifacts'] = [filename]
    result = Agent5EvidenceVerifier().verify_dynamic(contract, evidence, run_dir=tmp_path)
    assert result.status == ('PASS' if passed else 'FAIL'), result
    path.write_bytes(b'tampered')
    assert Agent5EvidenceVerifier().verify_dynamic(contract, evidence, run_dir=tmp_path).status == 'INCONCLUSIVE'


def test_merge_cannot_overwrite_observed_false_or_conflicting_value():
    contract = reviewed()
    original = records(contract)
    incoming = records(contract, {'E_result': 'conflict', 'E_detail': 'conflict'})
    assert merge_dynamic(contract, original, incoming) == []
    assert original['E_result']['value'] == '机器人乙'
    del original['E_detail']
    assert merge_dynamic(contract, original, incoming) == ['E_detail']
    with pytest.raises(ExecutionRejected):
        merge_dynamic(contract, original, {'E_unknown': incoming['E_detail']})


def test_probe_is_linked_to_original_oracle_and_checkpoint():
    contract = reviewed()
    evidence = records(contract, missing=['E_detail'])
    agent = Agent5EvidenceVerifier()
    request = agent.plan_dynamic_probe(contract, evidence, agent.verify_dynamic(contract, evidence), step_id='S_title')
    assert request['oracle_ids'] == ['O_title'] and request['evidence_ids'] == ['E_detail']
    validate_probe(contract, request, current_step_id='S_title')
    for changes in ({'side_effect': 'submit'}, {'step_id': 'S_result'}, {'evidence_ids': ['E_result']},
                    {'oracle_ids': ['O_fake']}, {'contract_digest': 'changed'}):
        with pytest.raises(ExecutionRejected):
            validate_probe(contract, dict(request, **changes), current_step_id='S_title')


@pytest.mark.parametrize('category,started,step,allowed', [
    ('locator', False, 'S_query', True), ('interaction', True, 'S_query', False),
    ('wait', False, 'S_detail', True), ('wait', True, 'S_detail', False),
    ('interaction', True, 'S_search', False), ('permission', False, 'S_query', False),
    ('environment', False, 'S_query', False), ('invalid_code', False, 'S_query', False),
    ('evidence_missing', False, 'S_title', False), ('application_assertion', False, 'S_title', False)])
def test_recovery_classification_and_side_effect_policy(category, started, step, allowed):
    contract = reviewed()
    decision = Agent6RecoveryController().plan_dynamic_repair(contract,
        {'step_id': step, 'category': category, 'action_started': started}, observation())
    assert decision['allowed'] is allowed
    assert decision['contract_digest'] == contract.design_digest


def test_recovery_budget_application_fail_and_extra_live_permission():
    agent = Agent6RecoveryController()
    contract = reviewed()
    error = {'step_id': 'S_query', 'category': 'locator', 'action_started': False}
    assert not agent.plan_dynamic_repair(contract, error, observation(), previous_repairs=1)['allowed']
    from web_agent.schema import Verdict
    assert not agent.plan_dynamic_repair(contract, error, observation(), verdict=Verdict('FAIL', 'confirmed'))['allowed']
    live = reviewed(base='https://catalog.example')
    obs = dict(observation(), url=live.design.target_url)
    assert not agent.plan_dynamic_repair(live, error, obs)['allowed']
    assert agent.plan_dynamic_repair(live, error, obs, live_recovery_allowed=True)['allowed']
    assert not agent.plan_dynamic_repair(contract, error, dict(obs, url='https://outside.example'))['allowed']


class ScenarioSession:
    """Protocol fake, not a browser acceptance test."""
    def __init__(self, contract, *, fail_step=None, fail_times=0, missing=False, probe_success=True, fail_value=False):
        self.contract, self.fail_step, self.fail_times = contract, fail_step, fail_times
        self.missing, self.probe_success, self.fail_value = missing, probe_success, fail_value
        self.closed, self.info = False, {'session_id': 'same_session'}
        self.calls, self.probes, self.counter, self.attempts = [], [], 0, {}
    def observe(self, **kwargs):
        self.counter += 1
        obs = observation()
        obs['observation_id'] = 'obs_' + str(self.counter)
        # New live node IDs demonstrate regeneration does not reuse old refs.
        obs['elements'] = [dict(obs['elements'][0], node_id='node_' + str(self.counter),
            name='检查目标', tag='output', type='', text='机器人乙')]
        return obs
    def execute(self, artifact, **kwargs):
        sid = artifact['step_id']
        self.calls.append(artifact)
        attempt = self.attempts.get(sid, 0) + 1
        self.attempts[sid] = attempt
        if sid == self.fail_step and attempt <= self.fail_times:
            return {'step_id': sid, 'status': 'ERROR', 'error': {'step_id': sid, 'category': 'locator', 'action_started': False}}
        values = records(self.contract, {'E_check': 'bad' if self.fail_value else '机器人乙'}, missing=['E_check'] if self.missing else [])
        values = {eid: row for eid, row in values.items() if row['step_id'] == sid}
        for row in values.values(): row['attempt'] = attempt
        return {'step_id': sid, 'status': 'COMPLETE', 'outputs': {'observed': None if self.missing else '机器人乙'} if sid == 'S_check' else {}, 'evidence': values}
    def probe(self, request, artifact, **kwargs):
        self.probes.append((request, artifact))
        values = records(self.contract, missing=[] if self.probe_success else ['E_check'])
        for row in values.values():
            row.update(source='probe', collection_attempt=2)
        return {'step_id': 'S_check', 'status': 'COMPLETE', 'evidence': values, 'outputs': {'observed': '机器人乙' if self.probe_success else None}}
    def close(self): self.closed = True


@pytest.mark.parametrize('success', [True, False])
def test_pipeline_directed_probe_success_failure_and_nonoverwrite(monkeypatch, tmp_path, success):
    contract = one_oracle()
    session = ScenarioSession(contract, missing=True, probe_success=success)
    monkeypatch.setattr(Agent4TestExecutor, 'open_dynamic', lambda *a, **kw: session)
    result = run_dynamic_contract(contract, tmp_path, llm=GroundingModel())
    assert result['verdict']['status'] == ('PASS' if success else 'INCONCLUSIVE'), result
    assert result['probe_count'] == 1 and result['repair_count'] == 0
    assert len(session.calls) == 2 and len(session.probes) == 1 and session.closed
    assert session.probes[0][1]['operation']['observation_id'] != session.calls[-1]['operation']['observation_id']


@pytest.mark.parametrize('fail_times,budget,status,repairs', [(1, 1, 'PASS', 1), (2, 1, 'ERROR', 1), (1, 0, 'ERROR', 0)])
def test_pipeline_recovery_fresh_dom_failed_retry_and_budget(monkeypatch, tmp_path, fail_times, budget, status, repairs):
    contract = one_oracle()
    session = ScenarioSession(contract, fail_step='S_check', fail_times=fail_times)
    monkeypatch.setattr(Agent4TestExecutor, 'open_dynamic', lambda *a, **kw: session)
    result = run_dynamic_contract(contract, tmp_path, llm=GroundingModel(), policy=ExecutionPolicy(max_repairs=budget))
    assert result['verdict']['status'] == status, result
    assert result['repair_count'] == repairs and session.closed
    sources = [a for a in session.calls if a['step_id'] == 'S_check']
    if repairs:
        assert sources[0]['operation']['observation_id'] != sources[1]['operation']['observation_id']
        assert sources[0]['operation']['target_id'] != sources[1]['operation']['target_id']
        assert (tmp_path / sources[0]['source_file']).exists() and (tmp_path / sources[1]['source_file']).exists()
        assert all(h['contract_digest'] == contract.design_digest for h in result['recovery_history'])


def test_confirmed_failure_is_terminal_without_probe_or_repair(monkeypatch, tmp_path):
    contract = one_oracle()
    session = ScenarioSession(contract, fail_value=True)
    monkeypatch.setattr(Agent4TestExecutor, 'open_dynamic', lambda *a, **kw: session)
    result = run_dynamic_contract(contract, tmp_path, llm=GroundingModel())
    assert result['verdict']['status'] == 'FAIL' and result['stop_reason'] == 'oracle_failure'
    assert not session.probes and result['repair_count'] == 0


@pytest.mark.parametrize('permission', ['probe', 'recovery'])
def test_pipeline_rejects_unauthorized_live_extra_actions(monkeypatch, tmp_path, permission):
    contract = one_oracle(base='https://catalog.example')
    session = ScenarioSession(contract, missing=permission == 'probe', fail_step='S_check', fail_times=1 if permission == 'recovery' else 0)
    monkeypatch.setattr(Agent4TestExecutor, 'open_dynamic', lambda *a, **kw: session)
    result = run_dynamic_contract(contract, tmp_path, llm=GroundingModel(), policy=ExecutionPolicy(allow_live_site=True))
    assert not session.probes and result['repair_count'] == 0
    assert result['verdict']['status'] == ('INCONCLUSIVE' if permission == 'probe' else 'ERROR')


def test_legacy_confirmed_fail_with_missing_evidence_regression():
    from .test_contract import build
    contract = build()
    evidence = {'execution_status': 'COMPLETE', 'search': {'applied_keyword': 'wrong'}, 'results': {}, 'detail': {}}
    verdict = Agent5EvidenceVerifier().verify(contract, evidence)
    assert verdict.status == 'FAIL'
    assert any(c['passed'] is False for c in verdict.checks) and any(c['passed'] is None for c in verdict.checks)


@pytest.mark.parametrize('option', [{'max_probes': True}, {'max_probes': 4}, {'max_repairs': -1},
    {'allow_downloads': 1}, {'allow_live_probes': True}, {'allow_live_recovery': True}])
def test_new_policy_fields_are_strict_and_require_base_permission(option):
    with pytest.raises(ValueError):
        ExecutionPolicy(**option)


def test_every_protocol_relation_has_a_trusted_registered_evaluator():
    from web_agent.dynamic_schema import RELATIONS, EVIDENCE_KINDS, DESIGN_WIRE_GUIDE
    from web_agent.dynamic_verification import ORACLE_EVALUATORS, READERS
    assert set(RELATIONS) == set(ORACLE_EVALUATORS)
    assert set(EVIDENCE_KINDS) - {'screenshot'} == set(READERS)
    assert DESIGN_WIRE_GUIDE['evidence_requirements'][0]['kind'] == '|'.join(EVIDENCE_KINDS)


def test_download_requires_contract_action_and_separate_permission(tmp_path):
    contract = one_oracle('file', 'file_valid', True, action='download')
    obs = observation()
    obs['elements'][0].update(tag='a', type='', name='检查目标')
    model = CheckModel()
    with pytest.raises(ExecutionRejected, match='permission'):
        Agent3SeleniumGenerator().generate_dynamic(contract, 'S_check', obs, tmp_path,
            state={'S_open': {}}, policy=ExecutionPolicy(), llm=model)
    artifact = Agent3SeleniumGenerator().generate_dynamic(contract, 'S_check', obs, tmp_path,
        state={'S_open': {}}, policy=ExecutionPolicy(allow_downloads=True), llm=model)
    assert 'api.download(element)' in artifact['source']


def test_missing_probe_does_not_overwrite_reliable_false():
    contract = one_oracle('visible', expected=True)
    observed = records(contract, {'E_check': False})
    incoming = records(contract, {'E_check': True})
    assert not merge_dynamic(contract, observed, incoming)
    assert observed['E_check']['value'] is False


def test_absence_cannot_be_inferred_from_truncated_or_generic_semantics(tmp_path):
    from web_agent.dynamic_execution import validate_operation
    contract = one_oracle('exists', expected=False)
    obs = dict(observation(), elements=[], truncated=False)
    op = operation('S_check', target_id=None, reads={'observed': 'exists'})
    with pytest.raises(ExecutionRejected, match='exact_aria_label'):
        validate_operation(contract, 'S_check', obs, op, {}, ExecutionPolicy())
    raw = contract.design.to_dict()
    raw['steps'][1]['target']['role'] = 'exact_aria_label'
    contract, _ = Agent2TestReviewer().review_dynamic(Design.from_dict(raw), requirement=raw['requirement'],
        target_url=raw['target_url'], mode='template')
    with pytest.raises(ExecutionRejected, match='exhaustive'):
        validate_operation(contract, 'S_check', dict(obs, truncated=True), op, {}, ExecutionPolicy())
    artifact = Agent3SeleniumGenerator().generate_dynamic(contract, 'S_check', obs, tmp_path,
        state={'S_open': {}}, policy=ExecutionPolicy(), mode='template')
    assert 'api.lookup(None' in artifact['source']


def test_worker_probe_rejects_replaying_business_action(tmp_path):
    from web_agent.browser_worker import BrowserOwner
    from .test_v04_execution import FakeOwnerDriver, FakeGuard
    contract = reviewed()
    driver = FakeOwnerDriver()
    driver.current_url = contract.design.target_url
    owner = BrowserOwner(contract, ExecutionPolicy(), driver_factory=lambda **kw: driver, guard_factory=FakeGuard)
    owner.state = {'S_open': {}, 'S_query': {}}
    request = {'type': 'dynamic_read', 'contract_digest': contract.design_digest, 'step_id': 'S_query',
        'evidence_ids': ['E_detail'], 'oracle_ids': ['O_title'], 'side_effect': 'read_only', 'reason': 'bad'}
    try:
        with pytest.raises(ExecutionRejected, match='business action'):
            owner.probe(request, {})
    finally:
        owner.close()


def test_pipeline_rechecks_forged_agent6_permission(monkeypatch, tmp_path):
    contract = one_oracle(base='https://catalog.example')
    session = ScenarioSession(contract, fail_step='S_check', fail_times=1)
    monkeypatch.setattr(Agent4TestExecutor, 'open_dynamic', lambda *a, **kw: session)
    def forged(*a, **kw):
        return {'allowed': True, 'step_id': 'S_check', 'contract_digest': contract.design_digest,
                'strategy': 'regenerate_from_fresh_dom'}
    monkeypatch.setattr(Agent6RecoveryController, 'plan_dynamic_repair', forged)
    result = run_dynamic_contract(contract, tmp_path, llm=GroundingModel(), policy=ExecutionPolicy(allow_live_site=True))
    assert result['verdict']['status'] == 'ERROR' and result['repair_count'] == 0
    assert len(session.calls) == 2


class CheckModel:
    def __init__(self, *, value_probe=False):
        self.calls = []
        self.value_probe = value_probe
    def complete_json(self, system, user):
        payload = json.loads(user)
        self.calls.append(payload)
        obs, step = payload['observation'], payload['step']
        candidates = [e for e in obs['elements'] if e['name'] == step['target']['description']]
        if self.value_probe:
            candidates = [e for e in candidates if e['tag'] == ('p' if len(self.calls) == 1 else 'output')]
        selected = candidates[0] if candidates else None
        from web_agent.dynamic_verification import READERS
        return {'step_id': step['step_id'], 'observation_id': obs['observation_id'],
                'target_id': selected['node_id'] if selected else None,
                'wait': 'visible', 'expected_change': 'none',
                'reads': {r['output']: READERS[r['kind']] for r in payload['evidence_requirements']},
                'rationale': 'Select the current accessible target and reviewed property reader'}


@REAL
def test_real_chrome_download_file_evidence(tmp_path):
    from web_agent.dynamic_fixture import dynamic_fixture
    with dynamic_fixture() as (base, _):
        contract = one_oracle('file', 'file_valid', True, action='download', base=base, path='/download')
        result = run_dynamic_contract(contract, tmp_path, llm=CheckModel(),
            policy=ExecutionPolicy(allow_downloads=True, step_timeout=12))
    assert result['verdict']['status'] == 'PASS', result
    record = result['evidence']['E_check']
    assert record['source'] == 'download' and record['value']['complete']
    assert json.loads((tmp_path / record['value']['path']).read_text()) == {'fixture': True, 'count': 2}
    assert record['value']['path'] in record['artifacts'] and record['attempt'] == 1
    assert result['probe_count'] == 0 and result['repair_count'] == 0


@REAL
def test_real_chrome_targeted_read_probe(tmp_path):
    from web_agent.dynamic_fixture import dynamic_fixture
    with dynamic_fixture() as (base, _):
        # First read targets a paragraph with no value property. The read-only
        # probe grounds the SAME semantic requirement in the output element.
        contract = one_oracle('value', base=base)
        model = CheckModel(value_probe=True)
        result = run_dynamic_contract(contract, tmp_path, llm=model, policy=ExecutionPolicy(step_timeout=12))
    assert result['verdict']['status'] == 'PASS', result
    assert result['probe_count'] == 1 and result['repair_count'] == 0
    record = result['evidence']['E_check']
    assert record['source'] == 'probe' and record['collection_attempt'] == 2
    assert result['steps'][-1]['evidence']['E_check']['status'] == 'failed'
    probe = result['probes'][0]
    assert probe['result']['source_sha256'] == source_digest((tmp_path / probe['source_file']).read_text(encoding='utf-8'))
    assert probe['request']['oracle_ids'] == ['O_check']
    assert model.calls[0]['observation']['observation_id'] != model.calls[1]['observation']['observation_id']


class MutationModel(GroundingModel):
    def __init__(self, base):
        super().__init__()
        self.base, self.triggered = base, False
    def complete_json(self, system, user):
        from urllib.request import urlopen
        result = super().complete_json(system, user)
        if result['step_id'] == 'S_query' and not self.triggered:
            self.triggered = True
            with urlopen(self.base + '/trigger-mutation', timeout=2) as response:
                generation = int(response.read())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                with urlopen(self.base + '/mutation-status', timeout=2) as response:
                    if int(response.read()) >= generation:
                        return result
                time.sleep(0.03)
            raise AssertionError('Fixture DOM mutation was not acknowledged')
        return result


@REAL
def test_real_chrome_dom_change_recovery_new_observation(tmp_path):
    from web_agent.dynamic_fixture import dynamic_fixture
    with dynamic_fixture() as (base, _):
        contract = reviewed(base=base, path='/recover')
        model = MutationModel(base)
        result = run_dynamic_contract(contract, tmp_path, llm=model,
            policy=ExecutionPolicy(step_timeout=12, max_repairs=1))
    assert result['verdict']['status'] == 'PASS', result
    assert result['repair_count'] == 1 and result['probe_count'] == 0
    attempts = json.loads((tmp_path / 'execution_attempts.json').read_text(encoding='utf-8'))
    query = [r for r in attempts if r['step_id'] == 'S_query']
    assert [r['status'] for r in query] == ['ERROR', 'COMPLETE']
    assert query[0]['error']['category'] == 'locator' and not query[0]['error']['action_started']
    assert query[0]['observation_id'] != query[1]['observation_id']
    assert query[0]['session_id'] == query[1]['session_id']
    assert result['recovery_history'][0]['observation_id'] == query[1]['observation_id']
    assert result['recovery_history'][0]['contract_digest'] == contract.design_digest


@REAL
@pytest.mark.parametrize('kind,target,expected', [('exists', '不存在目标', False),
                                               ('visible', '隐藏目标', False),
                                               ('enabled', '禁用目标', False)])
def test_real_chrome_absence_hidden_disabled_state(tmp_path, kind, target, expected):
    from web_agent.dynamic_fixture import dynamic_fixture
    from dataclasses import replace
    with dynamic_fixture() as (base, _):
        draft = one_oracle(kind, expected=expected, base=base).design.to_dict()
        draft['steps'][1]['target']['description'] = target
        if kind == 'exists':
            draft['steps'][1]['target']['role'] = 'exact_aria_label'
        contract, decision = Agent2TestReviewer().review_dynamic(Design.from_dict(draft),
            requirement=draft['requirement'], target_url=draft['target_url'], mode='template')
        assert decision['approved']
        result = run_dynamic_contract(contract, tmp_path, llm=CheckModel(), policy=ExecutionPolicy(step_timeout=12))
    assert result['verdict']['status'] == 'PASS', result
