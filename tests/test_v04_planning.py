"""v0.4 stage 1: semantic planning only, mocked models and no browser visits."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

from web_agent.agents import Agent1TestDesigner, Agent2TestReviewer
from web_agent.dynamic_schema import requirement_id, TestDesign as Design
from web_agent.schema import DynamicTestContract, TestContract as LegacyContract, parse_contract
from web_agent.pipeline import run_pipeline
from .test_contract import build
from .test_v02_agent_upgrade import QueueLLM


def search_design():
    raw = '在 https://catalog.example/search 搜索“机器人”，打开第二项结果，确认详情标题与该结果标题一致。'
    quotes = ['搜索“机器人”', '打开第二项结果', '确认详情标题与该结果标题一致']
    rids = [requirement_id(raw, q) for q in quotes]
    def step(sid, action, description, reqs, deps=(), inputs=None, outputs=(), position=None):
        return {'step_id': sid, 'requirement_ids': reqs, 'action': action,
                'target': {'description': description, 'role': 'semantic_control', 'position': position},
                'depends_on': list(deps), 'precondition_ids': [], 'inputs': inputs or {}, 'outputs': list(outputs)}
    steps = [
        step('S_open', 'navigate', '目录搜索页面', [rids[0]]),
        step('S_query', 'fill', '搜索框', [rids[0]], ['S_open'],
             {'value': {'kind': 'test_data', 'data_id': 'D_keyword'}}),
        step('S_search', 'submit', '搜索表单', [rids[0]], ['S_query']),
        step('S_result', 'observe', '第二项结果标题', [rids[1]], ['S_search'], outputs=['title'], position=2),
        step('S_detail', 'click', '第二项结果链接', [rids[1]], ['S_result'], position=2),
        step('S_title', 'observe', '详情页标题', [rids[2]], ['S_detail'], outputs=['title']),
    ]
    return {
        'protocol_version': '0.4', 'document_type': 'test_design', 'requirement': raw,
        'target_url': 'https://catalog.example/search', 'test_goal': '第二项结果与详情标题一致',
        'requirements': [{'requirement_id': rid, 'text': quote, 'source_quote': quote,
                          'kind': 'assertion' if i == 2 else 'action'}
                         for i, (rid, quote) in enumerate(zip(rids, quotes))],
        'steps': steps, 'preconditions': [],
        'test_data': [{'data_id': 'D_keyword', 'value': '机器人', 'source_quote': '搜索“机器人”'}],
        'oracles': [{'oracle_id': 'O_title', 'requirement_ids': [rids[2]], 'after_step_id': 'S_title',
                     'relation': 'equals', 'actual': {'kind': 'evidence', 'evidence_id': 'E_detail'},
                     'expected': {'kind': 'evidence', 'evidence_id': 'E_result'},
                     'evidence_ids': ['E_detail', 'E_result']}],
        'evidence_requirements': [
            {'evidence_id': 'E_result', 'step_id': 'S_result', 'output': 'title',
             'description': '第二项结果的标题', 'kind': 'text'},
            {'evidence_id': 'E_detail', 'step_id': 'S_title', 'output': 'title',
             'description': '打开后的详情标题', 'kind': 'text'},
        ], 'assumptions': [], 'missing_information': [], 'unsupported_requests': [],
    }


def form_design():
    raw = ('在 https://enroll.example/apply 姓名填“张三”，城市选择“杭州”，'
           '点击下一步，确认预览姓名为“张三”，返回上一步，确认姓名仍为“张三”。')
    quotes = ['姓名填“张三”', '城市选择“杭州”', '点击下一步', '确认预览姓名为“张三”',
              '返回上一步', '确认姓名仍为“张三”']
    rids = [requirement_id(raw, q) for q in quotes]
    specs = [
        ('S_open', 'navigate', '报名页面', 0, None, []),
        ('S_name', 'fill', '姓名输入框', 0, 'D_name', []),
        ('S_city', 'select', '城市选择框', 1, 'D_city', []),
        ('S_next', 'click', '下一步按钮', 2, None, []),
        ('S_preview', 'observe', '预览页姓名', 3, None, ['name']),
        ('S_back', 'click', '上一步按钮', 4, None, []),
        ('S_retained', 'observe', '表单姓名输入值', 5, None, ['name']),
    ]
    steps = []
    for sid, action, desc, index, datum, outputs in specs:
        steps.append({'step_id': sid, 'requirement_ids': [rids[index]], 'action': action,
            'target': {'description': desc, 'role': 'semantic_control', 'position': None},
            'depends_on': [steps[-1]['step_id']] if steps else [], 'precondition_ids': [],
            'inputs': {'value': {'kind': 'test_data', 'data_id': datum}} if datum else {}, 'outputs': outputs})
    oracles, evidence = [], []
    for sid, rid, suffix in [('S_preview', rids[3], 'preview'), ('S_retained', rids[5], 'retained')]:
        evidence.append({'evidence_id': 'E_' + suffix, 'step_id': sid, 'output': 'name',
                         'description': '该步骤可见姓名', 'kind': 'value'})
        oracles.append({'oracle_id': 'O_' + suffix, 'requirement_ids': [rid], 'after_step_id': sid,
            'relation': 'equals', 'actual': {'kind': 'evidence', 'evidence_id': 'E_' + suffix},
            'expected': {'kind': 'test_data', 'data_id': 'D_name'}, 'evidence_ids': ['E_' + suffix]})
    return {'protocol_version': '0.4', 'document_type': 'test_design', 'requirement': raw,
        'target_url': 'https://enroll.example/apply', 'test_goal': '验证预览与返回后的表单数据保留',
        'requirements': [{'requirement_id': rid, 'text': q, 'source_quote': q,
                          'kind': 'assertion' if i in (3, 5) else 'action'}
                         for i, (rid, q) in enumerate(zip(rids, quotes))],
        'steps': steps, 'preconditions': [], 'test_data': [
            {'data_id': 'D_name', 'value': '张三', 'source_quote': quotes[0]},
            {'data_id': 'D_city', 'value': '杭州', 'source_quote': quotes[1]}],
        'oracles': oracles, 'evidence_requirements': evidence,
        'assumptions': [], 'missing_information': [], 'unsupported_requests': []}


def approval(design, approved=True, issues=None):
    return {'approved': approved, 'issues': issues or [],
            'coverage': [r['requirement_id'] for r in design['requirements']]}


def plan(raw, model=None, previous=None):
    return Agent1TestDesigner().design(raw['requirement'], engine='dynamic', mode='llm',
        target_url=raw['target_url'], llm=model or QueueLLM(raw), previous_design=previous)


def review(raw, model=None, mode='llm'):
    design = Design.from_dict(raw) if type(raw) is dict else raw
    return Agent2TestReviewer().review(design, engine='dynamic', target=design.target_url,
        original_requirement=design.requirement, mode=mode,
        llm=model or QueueLLM(approval(design.to_dict())))


@pytest.mark.parametrize('factory', [search_design, form_design])
def test_two_websites_produce_real_semantic_steps(factory):
    raw = factory()
    model = QueueLLM(raw)
    design = plan(raw, model)
    assert isinstance(design, Design)
    assert len(design.steps) >= 6
    assert design.target_url == raw['target_url']
    assert not hasattr(design, 'query')
    assert not any('arxiv' in s.target.description for s in design.steps)
    payload = json.loads(model.calls[0]['user'])
    assert payload['original_requirement'] == raw['requirement']
    assert payload['wire_guide']['protocol_version'] == '0.4'
    contract, decision = review(design)
    assert decision['approved']
    assert contract.design == design


def test_agent1_application_owned_requirement_ids_and_revision_stability():
    raw = search_design()
    old_ids = [r['requirement_id'] for r in raw['requirements']]
    for i, row in enumerate(raw['requirements']):
        row['requirement_id'] = f'R_local{i}'
    for key in ('steps', 'oracles'):
        for row in raw[key]:
            row['requirement_ids'] = [f'R_local{old_ids.index(rid)}' for rid in row['requirement_ids']]
    design = plan(raw)
    assert [r.requirement_id for r in design.requirements] == old_ids
    revised = plan(raw, previous=design)
    assert revised.steps == design.steps
    bad = revised.to_dict()
    bad['oracles'][0]['oracle_id'] = 'O_renamed'
    with pytest.raises(ValueError, match='retain oracle_id'):
        plan(bad, previous=design)


def test_reviewer_catches_first_instead_of_second_even_if_mock_approves():
    bad = search_design()
    for step in bad['steps']:
        if step['target']['position']:
            step['target']['position'] = 1
    contract, decision = review(bad)
    assert contract is None
    assert any('Ordinal mismatch' in issue for issue in decision['issues'])


def test_reviewer_catches_a_missing_verification_with_other_oracle_remaining():
    bad = form_design()
    bad['oracles'].pop()
    contract, decision = review(bad)
    assert contract is None
    assert any('Missing verification oracle' in issue for issue in decision['issues'])


def test_independent_semantic_review_receives_original_and_catches_omitted_requirement():
    bad = form_design()
    omitted = bad['requirements'].pop()['requirement_id']
    bad['steps'] = [s for s in bad['steps'] if omitted not in s['requirement_ids']]
    bad['oracles'] = [o for o in bad['oracles'] if omitted not in o['requirement_ids']]
    bad['evidence_requirements'].pop()
    model = QueueLLM(approval(bad, False, ['遗漏返回后检查姓名保留的要求']))
    contract, decision = review(bad, model)
    assert contract is None
    assert '遗漏' in decision['issues'][-1]
    payload = json.loads(model.calls[0]['user'])
    assert payload['original_requirement'] == bad['requirement']
    assert '确认姓名仍为“张三”' in payload['original_requirement']


def test_missing_precondition_is_unknown_and_never_invented():
    raw = form_design()
    raw['preconditions'] = [{'precondition_id': 'P_access', 'description': '是否需要登录尚未确认',
                             'status': 'unknown', 'source_quote': ''}]
    raw['steps'][0]['precondition_ids'] = ['P_access']
    raw['missing_information'] = ['未说明登录前置条件']
    design = plan(raw)
    assert design.preconditions[0].status == 'unknown'
    contract, decision = review(design)
    assert contract is None
    assert any('Unconfirmed precondition' in issue for issue in decision['issues'])
    bad = deepcopy(raw)
    bad['preconditions'][0].update(status='provided', source_quote='用户已经登录')
    with pytest.raises(ValueError, match='source_quote'):
        plan(bad)


@pytest.mark.parametrize('field', ['assumptions', 'missing_information', 'unsupported_requests'])
def test_unresolved_information_cannot_be_overridden_by_llm(field):
    raw = search_design()
    raw[field] = ['Unconfirmed input']
    assert review(raw)[0] is None


@pytest.mark.parametrize('corruption', [
    'extra', 'version', 'cycle', 'dangling', 'bool_position', 'bad_action', 'selector',
    'wrong_site', 'wrong_requirement', 'evidence_output', 'evidence_operand', 'future_data',
    'invent_data', 'literal_input', 'duplicate_id', 'unknown_precondition', 'missing_field',
])
def test_invalid_model_protocol_is_rejected(corruption):
    raw = search_design()
    candidate = deepcopy(raw)
    if corruption == 'extra': candidate['executor_code'] = 'print(1)'
    elif corruption == 'version': candidate['protocol_version'] = '0.5'
    elif corruption == 'cycle': candidate['steps'][0]['depends_on'] = ['S_title']
    elif corruption == 'dangling': candidate['steps'][1]['depends_on'] = ['S_unknown']
    elif corruption == 'bool_position': candidate['steps'][3]['target']['position'] = True
    elif corruption == 'bad_action': candidate['steps'][1]['action'] = 'execute_python'
    elif corruption == 'selector': candidate['steps'][1]['target']['css'] = '#query'
    elif corruption == 'wrong_site': candidate['target_url'] = 'https://other.example'
    elif corruption == 'wrong_requirement': candidate['requirement'] += ' changed'
    elif corruption == 'evidence_output': candidate['evidence_requirements'][0]['output'] = 'unknown'
    elif corruption == 'evidence_operand': candidate['oracles'][0]['evidence_ids'] = ['E_detail']
    elif corruption == 'future_data': candidate['steps'][1]['inputs']['value'] = {
        'kind': 'step_output', 'step_id': 'S_title', 'output': 'title'}
    elif corruption == 'invent_data': candidate['test_data'][0]['value'] = 'invented secret'
    elif corruption == 'literal_input': candidate['steps'][1]['inputs']['value'] = {'kind': 'literal', 'value': '机器人'}
    elif corruption == 'duplicate_id': candidate['steps'][1]['step_id'] = 'S_open'
    elif corruption == 'unknown_precondition': candidate['steps'][1]['precondition_ids'] = ['P_fake']
    elif corruption == 'missing_field': candidate.pop('assumptions')
    with pytest.raises(ValueError):
        plan(raw, QueueLLM(candidate))


@pytest.mark.parametrize('invalid', [None, [], 'not JSON', {'requirements': []}])
def test_nonobject_or_incomplete_llm_output_rejected(invalid):
    with pytest.raises(ValueError):
        plan(search_design(), QueueLLM(invalid))


@pytest.mark.parametrize('bad', [
    {'approved': 'true', 'issues': [], 'coverage': []},
    {'approved': True, 'issues': [], 'coverage': [], 'contract': {}},
    {'approved': True, 'issues': [], 'coverage': ['R_unknown']},
    [],
])
def test_invalid_reviewer_json_rejected(bad):
    with pytest.raises(ValueError, match='review JSON'):
        review(search_design(), QueueLLM(bad))


def test_step_output_and_oracle_evidence_links():
    raw = search_design()
    raw['steps'][4]['inputs']['selected_title'] = {'kind': 'step_output', 'step_id': 'S_result', 'output': 'title'}
    design = plan(raw)
    contract, _ = review(design)
    data = contract.to_dict()
    parsed = parse_contract(data)
    assert parsed.design.oracles[0].evidence_ids == ('E_detail', 'E_result')
    assert parsed.design.steps[4].inputs[0][1].ref_id == 'S_result'
    assert parsed.to_dict() == data


def test_contract_is_deeply_frozen_and_serialized_tampering_detected():
    contract, _ = review(search_design())
    with pytest.raises(FrozenInstanceError):
        contract.design.steps[0].target.description = 'different website'
    wire = contract.to_dict()
    wire['design']['steps'][3]['target']['position'] = 1
    with pytest.raises(ValueError, match='digest'):
        parse_contract(wire)
    assert contract.design.steps[3].target.position == 2


def test_legacy_and_dynamic_contracts_coexist():
    legacy = build()
    parsed = parse_contract(legacy)
    assert isinstance(parsed, LegacyContract)
    assert parsed.to_dict() == legacy
    assert isinstance(review(search_design())[0], DynamicTestContract)
    with pytest.raises(ValueError, match='protocol_version'):
        parse_contract({'protocol_version': '9'})
    draft = Agent1TestDesigner().design('搜索“GUI Agent”')
    assert draft['query']['keyword'] == 'GUI Agent'


def forbid_legacy_execution(monkeypatch):
    import web_agent.pipeline as pipe
    def forbidden(*args, **kwargs):
        raise AssertionError('Dynamic stage 1 must not use the legacy browser workflow')
    for name in ('local_fixture', 'inspect_page', '_run_script', '_run_probe', 'html_report'):
        monkeypatch.setattr(pipe, name, forbidden)
    for cls in (pipe.Agent3SeleniumGenerator, pipe.Agent4TestExecutor,
                pipe.Agent5EvidenceVerifier, pipe.Agent6RecoveryController):
        monkeypatch.setattr(cls, '__init__', forbidden)


@pytest.mark.parametrize('factory', [search_design, form_design])
def test_dynamic_pipeline_only_plans_and_never_calls_legacy_agents(factory, tmp_path, monkeypatch):
    forbid_legacy_execution(monkeypatch)
    raw = factory()
    result = run_pipeline(engine='dynamic', requirement=raw['requirement'], target_url=raw['target_url'],
        mode='llm', llm=QueueLLM(raw, approval(raw)), output_root=tmp_path)
    assert result['planning_status'] == 'PLANNED'
    assert result['execution_status'] == 'NOT_RUN'
    assert result['verdict']['status'] == 'INCONCLUSIVE'
    assert result['contract']['protocol_version'] == '0.4'
    folder = Path(result['run_dir'])
    assert parse_contract(json.loads((folder / 'test_contract.json').read_text(encoding='utf-8')))
    assert not list(folder.glob('generated_test*.py'))


def test_review_rejection_revision_and_stable_ids(tmp_path):
    good = search_design()
    bad = deepcopy(good)
    for row in bad['steps']:
        if row['target']['position']:
            row['target']['position'] = 1
    model = QueueLLM(bad, approval(bad, False, ['应打开第二项']), good, approval(good))
    result = run_pipeline(engine='dynamic', requirement=good['requirement'], target_url=good['target_url'],
                          mode='llm', llm=model, output_root=tmp_path)
    assert result['contract']['revision'] == 2
    assert len(result['reviews']) == 2
    assert len(model.calls) == 4
    assert '应打开第二项' in json.loads(model.calls[2]['user'])['review_feedback']
    assert json.loads(model.calls[2]['user'])['previous_design']['steps'][3]['target']['position'] == 1


def test_review_budget_stops_after_two_rejections(tmp_path):
    raw = form_design()
    rejection = approval(raw, False, ['需要重新核对覆盖'])
    model = QueueLLM(raw, rejection, raw, rejection)
    result = run_pipeline(engine='dynamic', requirement=raw['requirement'], target_url=raw['target_url'],
        mode='llm', llm=model, output_root=tmp_path)
    assert result['planning_status'] == 'REVIEW_REJECTED'
    assert result['contract'] is None
    assert result['execution_status'] == 'NOT_RUN'
    assert len(model.calls) == 4
    assert not (Path(result['run_dir']) / 'test_contract.json').exists()


def test_engine_and_planning_mode_are_independent_and_no_template_fallback(tmp_path):
    raw = form_design()
    with pytest.raises(ValueError, match='template_design'):
        Agent1TestDesigner().design(raw['requirement'], engine='dynamic', mode='template',
                                    target_url=raw['target_url'])
    result = run_pipeline(engine='dynamic', mode='template', requirement=raw['requirement'],
        target_url=raw['target_url'], template_design=raw, output_root=tmp_path)
    assert result['planning_status'] == 'PLANNED'
    assert result['mode'] == 'template'
    assert result['reviews'][0]['semantic_review_performed'] is False
    with pytest.raises(ValueError, match='require engine=dynamic'):
        run_pipeline(target_url=raw['target_url'])
    with pytest.raises(ValueError, match='engine must'):
        run_pipeline(engine='mystery')


def test_topological_order_does_not_depend_on_wire_list_order():
    raw = search_design()
    raw['steps'].reverse()
    design = Design.from_dict(raw)
    order = [s.step_id for s in design.ordered_steps()]
    assert order == ['S_open', 'S_query', 'S_search', 'S_result', 'S_detail', 'S_title']


@pytest.mark.parametrize('change', ['screenshot', 'ordered_text', 'numeric_data', 'same_output'])
def test_invalid_or_vacuous_oracle_is_not_approved(change):
    raw = search_design()
    if change == 'screenshot':
        raw['evidence_requirements'][0]['kind'] = 'screenshot'
    elif change == 'ordered_text':
        raw['oracles'][0]['relation'] = 'greater_than'
    elif change == 'numeric_data':
        raw['test_data'][0]['value'] = 999
    else:
        raw['evidence_requirements'][1]['step_id'] = 'S_result'
    if change == 'same_output':
        contract, decision = review(raw)
        assert contract is None
        assert any('same observable' in s for s in decision['issues'])
    else:
        with pytest.raises(ValueError):
            plan(raw)


def test_reviewer_refuses_incomplete_llm_coverage():
    raw = search_design()
    model = QueueLLM({'approved': True, 'issues': [], 'coverage': []})
    contract, decision = review(raw, model)
    assert contract is None
    assert any('cover all' in issue for issue in decision['issues'])
