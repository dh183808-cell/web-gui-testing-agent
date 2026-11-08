"""Offline input/provenance contracts and routing, not real-model/browser acceptance."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from web_agent.agents.designer import Agent1TestDesigner
from web_agent.agents.reviewer import Agent2TestReviewer
from web_agent.pipeline import run_pipeline
from web_agent.schema import validate_dynamic_plan


def proposal(expected, quote, source_type='test_basis', kind='equals'):
    return dict(steps=[dict(id='S1', goal='执行用户要求的操作并观察结果')],
        oracles=[dict(id='O1', step_id='S1', meaning='用户指定行为的预期结果',
                      kind=kind, expected=expected, source_type=source_type, source_quote=quote)],
        missing_inputs=[], preconditions=[])


class PlanningModel:
    """Explicit response fixtures; does not purport to infer semantics like a real LLM."""
    calls = []

    def __init__(self, draft, *, input_status='consistent', issues=()):
        self.draft, self.input_status, self.issues = draft, input_status, list(issues)
        self.design_inputs, self.review_inputs = [], []

    def complete_json(self, system, user):
        data = json.loads(user)
        if system.startswith('You are Agent1'):
            assert 'test_basis' in system and 'source_type' in system
            assert 'never silently rewrite' in system
            assert 'observe it BEFORE any mutation' in system
            self.design_inputs.append(data)
            return deepcopy(self.draft)
        if system.startswith('You are Agent2 reviewing'):
            return dict(approved=True, issues=[])
        if system.startswith('You are Agent2'):
            assert 'deliberate negative test is not itself a conflict' in system
            assert 'first force the expected' in system
            self.review_inputs.append(data)
            return dict(approved=True, input_status=self.input_status, issues=self.issues, coverage=['O1'])
        pytest.fail('Input review must finish before invoking Agent3')


def run_plan(tmp_path, monkeypatch, model, requirement, basis=''):
    from web_agent import selenium_runtime as runtime
    monkeypatch.setattr(runtime, 'ARTIFACT_ROOT', tmp_path)
    monkeypatch.setattr(runtime, 'create_session',
                        lambda *a, **kw: pytest.fail('Planning-only/conflicting input started Chrome'))
    result = run_pipeline(target='dynamic', mode='llm', start_url='https://arxiv.org/search/advanced',
        requirement=requirement, test_basis=basis, only_generate=True, allow_live_site=True,
        allow_local_research=True, output_root=tmp_path, llm=model)
    return result, Path(result['run_dir'])


@pytest.mark.parametrize('requirement,basis,draft,scope', [
    ('清空关键词并提交，检查空查询提示。',
     '搜索说明\n1. 空关键词提交时显示“请输入关键词”。',
     proposal('请输入关键词', '空关键词提交时显示“请输入关键词”。'), 'test_basis'),
    ('搜索“图神经网络”，验证至少有一条结果。', '',
     proposal(1, '至少有一条结果', 'user_requirement', 'gte'), 'user_requirement'),
    ('输入121个字符，验证按说明拒绝请求。',
     '搜索说明\n输入超过120个字符时拒绝请求，显示“关键词过长”。',
     proposal('关键词过长', '输入超过120个字符时拒绝请求，显示“关键词过长”。'), 'test_basis'),
    ('进入搜索页面，验证初始默认字段。',
     '搜索说明\n默认搜索字段为“All fields”。',
     proposal('All fields', '默认搜索字段为“All fields”。'), 'test_basis'),
])
def test_clear_inputs_freeze_sourced_contract_without_browser(tmp_path, monkeypatch,
                                                             requirement, basis, draft, scope):
    model = PlanningModel(draft)
    result, path = run_plan(tmp_path, monkeypatch, model, requirement, basis)
    assert result['planning_only'] and result['verdict']['status'] == 'INCONCLUSIVE'
    assert not result['verdict']['execution_started']
    assert result['verdict']['reason_type'] == 'planning_only'  # Planning is never a business PASS.
    contract = json.loads((path/'test_contract.json').read_text('utf-8'))
    validate_dynamic_plan(contract)
    assert contract['requirement'] == requirement and contract['test_basis'] == basis
    assert contract['validation_scope'] == scope and contract['provenance_version'] == 1
    oracle = contract['oracles'][0]
    source = basis if oracle['source_type'] == 'test_basis' else requirement
    span = oracle['source_span']
    assert source[span['start']:span['end']] == oracle['source_quote']
    assert span['line'] == (2 if basis else 1)
    assert model.design_inputs[0]['test_basis'] == basis
    assert model.design_inputs[0]['requirement'] == requirement
    assert len(model.design_inputs) == len(model.review_inputs) == 1
    assert not (path/'agent6_diagnosis.json').exists()
    assert not (path/'agent3_delivery.json').exists()
    assert json.loads((path/'test_inputs.json').read_text('utf-8')) == dict(requirement=requirement, test_basis=basis)
    if not basis:
        assert '不代表说明书符合性' in (path/'report.md').read_text('utf-8')


@pytest.mark.parametrize('requirement,basis', [
    ('提交空关键词，验证返回全部结果且不显示提示。',
     '空关键词禁止提交，必须提示“请输入关键词”。'),
    ('新增两个待办：A、B；完成A后筛选未完成，验证仅有A。',
     '筛选未完成时，仅显示尚未完成的待办事项。'),
])
def test_input_conflict_stops_once_without_rewriting_or_selenium(tmp_path, monkeypatch, requirement, basis):
    issue = f'测试依据“{basis}”与用户验收条件“{requirement}”冲突，需澄清。'
    model = PlanningModel(proposal(False, requirement, 'user_requirement'),
                          input_status='conflict', issues=[issue])
    result, path = run_plan(tmp_path, monkeypatch, model, requirement, basis)
    assert result['verdict']['status'] == 'INCONCLUSIVE'
    assert result['verdict']['reason_type'] == 'conflict'
    assert result['verdict']['issues'] == [issue]
    assert len(model.design_inputs) == 1  # No self-rewrite to erase a genuine conflict.
    assert not (path/'test_contract.json').exists()
    assert not (path/'agent6_diagnosis.json').exists()
    assert not json.loads((path/'cleanup.json').read_text('utf-8'))


def test_missing_acceptance_conditions_are_inconclusive_even_if_reviewer_approves(tmp_path, monkeypatch):
    model = PlanningModel(dict(steps=[dict(id='S1', goal='搜索')],
                               oracles=[], missing_inputs=['“正常”没有可判断的验收条件']))
    result, path = run_plan(tmp_path, monkeypatch, model, '搜索图神经网络，确认功能正常。')
    assert result['verdict']['status'] == 'INCONCLUSIVE'
    assert result['verdict']['reason_type'] == 'unclear'
    assert len(model.design_inputs) == 1 and not (path/'test_contract.json').exists()


def test_new_review_cannot_default_missing_input_assessment_to_consistent(tmp_path, monkeypatch):
    class Model(PlanningModel):
        def complete_json(self, system, user):
            result = super().complete_json(system, user)
            if system.startswith('You are Agent2'):
                result.pop('input_status', None)
            return result
    model = Model(proposal(True, '验证按钮可用', 'user_requirement'))
    result, path = run_plan(tmp_path, monkeypatch, model, '打开页面，验证按钮可用')
    assert result['verdict']['status'] == 'INCONCLUSIVE'
    assert result['verdict']['reason_type'] == 'unclear'
    assert 'input_status' in result['verdict']['issues'][0]
    assert not (path/'test_contract.json').exists()


@pytest.mark.parametrize('draft', [
    proposal('成功', '操作完成', 'test_basis'),  # no manual supplied
    proposal('成功', '不存在的用户原文', 'user_requirement'),
    proposal('成功', '提交表单', 'user_requirement'),  # quote exists but not the literal expected result
    dict(steps=[dict(id='S1', goal='提交表单')], oracles=[dict(id='O1', step_id='S1',
         kind='equals', expected=True, meaning='凭空的期望')]),  # no citation at all
    proposal(True, '提交表单', ['user_requirement']),  # invalid source type is feedback, not TypeError
])
def test_citation_errors_cannot_be_approved_by_model_or_enter_agent3(tmp_path, monkeypatch, draft):
    model = PlanningModel(draft)
    result, path = run_plan(tmp_path, monkeypatch, model, '提交表单，验证出现成功。')
    assert result['verdict']['status'] == 'INCONCLUSIVE'
    assert result['verdict']['reason_type'] == 'contract_unapproved'
    assert len(model.design_inputs) == 2  # Existing one planning revision, never unbounded.
    reviews = json.loads((path/'agent2_reviews.json').read_text('utf-8'))
    assert all(not r['approved'] and r['source_issues'] for r in reviews)
    assert not (path/'test_contract.json').exists()


def test_trusted_input_and_positions_override_model_rewrites_and_forged_span():
    basis = '第一行说明\n结果显示“完成”。'
    draft = proposal('完成', '结果显示“完成”。')
    draft.update(requirement='被模型改写的需求', test_basis='伪造的说明书', provenance_version=0)
    draft['oracles'][0]['source_span'] = dict(start=999, end=1000, line=999)
    plan = Agent1TestDesigner().design_dynamic('提交任务并检查结果', url='https://authorized.test/',
        test_basis=basis, llm=PlanningModel(draft))
    validate_dynamic_plan(plan)
    assert plan['requirement'] == '提交任务并检查结果' and plan['test_basis'] == basis
    assert plan['provenance_version'] == 1
    assert plan['oracles'][0]['source_span'] == dict(start=6, end=len(basis), line=2)
    broken = deepcopy(plan)
    broken['oracles'][0]['source_span']['start'] = 0
    with pytest.raises(ValueError, match='source_span'):
        validate_dynamic_plan(broken)


def test_mixed_sources_do_not_relabel_user_condition_as_manual_compliance():
    basis, requirement = '提交后显示“完成”。', '提交任务，验证按钮仍可用。'
    draft = proposal('完成', basis)
    draft['oracles'].append(dict(id='O2', step_id='S1', kind='equals', expected=True,
        meaning='用户另提按钮验收条件', source_type='user_requirement', source_quote='验证按钮仍可用'))
    model = PlanningModel(draft)
    plan = Agent1TestDesigner().design_dynamic(requirement, test_basis=basis,
                                               url='https://authorized.test/', llm=model)
    assert Agent2TestReviewer().review_dynamic(plan, llm=model)['approved']
    assert plan['validation_scope'] == 'mixed'
    assert [o['source_type'] for o in plan['oracles']] == ['test_basis', 'user_requirement']


@pytest.mark.parametrize('with_basis', [False, True])
def test_cli_keeps_original_dynamic_arxiv_flags_and_passes_optional_basis(monkeypatch, capsys, with_basis):
    import run_demo
    import sys
    seen = []
    def run(**options):
        seen.append(options)
        return dict(verdict=dict(status='INCONCLUSIVE'), planning_only=True)
    monkeypatch.setattr(run_demo, 'run_pipeline', run)
    args = ['run_demo.py','--target','dynamic','--mode','llm','--provider','deepseek',
            '--url','https://arxiv.org/search/advanced','--allow-live-site',
            '--allow-local-research','--requirement','搜索并验证至少一条结果','--only-generate']
    if with_basis:
        args += ['--test-basis','说明：搜索显示匹配结果。']
    monkeypatch.setattr(sys, 'argv', args)
    assert run_demo.main() == 0
    assert seen[0]['test_basis'] == ('说明：搜索显示匹配结果。' if with_basis else None)
    assert seen[0]['start_url'] == 'https://arxiv.org/search/advanced'
    assert seen[0]['headless'] and seen[0]['only_generate']
    assert json.loads(capsys.readouterr().out)['verdict']['status'] == 'INCONCLUSIVE'


@pytest.mark.parametrize('actual,status', [(2, 'PASS'), (0, 'FAIL')])
def test_sourced_contract_preserves_existing_cold_trial_and_formal_pipeline(tmp_path, monkeypatch, actual, status):
    from web_agent import selenium_runtime as runtime
    sessions, sources = [], []
    class Session:
        def __init__(self, purpose):
            self.purpose, self.run_dir = purpose, tmp_path
            self.session_id = str(len(sessions))
        def observe(self, **options): return {}
        def close_session(self): return dict(process_tree_terminated=True, profile_removed=True)
        def execute_checked(self, source, **options): return dict(status='COMPLETE')
        def execute_program(self, source, **options):
            sources.append(source)
            return dict(execution_status='COMPLETE', browser_session_id=self.session_id,
                measurements={'O1':dict(options['bindings']['O1'], value=actual, status='observed',
                                        observation_ref='mock.json', screenshot_ref='mock.png')})
    def create(*args, purpose, **options):
        session = Session(purpose)
        sessions.append(session)
        return session
    class Model(PlanningModel):
        def complete_json(self, system, user):
            if system.startswith('You are Agent3'):
                return dict(code='driver.title', bindings={'O1':dict(
                    selector='.result', read='count', step_id='S1')})
            return super().complete_json(system, user)
    monkeypatch.setattr(runtime, 'ARTIFACT_ROOT', tmp_path)
    monkeypatch.setattr(runtime, 'create_session', create)
    basis = '搜索结果至少一条。'
    model = Model(proposal(1, basis, kind='gte'))
    result = run_pipeline(target='dynamic', mode='llm', start_url='https://arxiv.org/search/advanced',
        requirement='搜索GUI Agent并检查结果数量', test_basis=basis, allow_live_site=True,
        allow_local_research=True, output_root=tmp_path, llm=model)
    assert result['verdict']['status'] == status  # Mock control-flow test only.
    assert result['verdict']['validation_scope'] == 'test_basis'
    assert [s.purpose for s in sessions] == ['trial', 'trial', 'formal']
    assert len(sources) == 2 and sources[0] == sources[1]
    contract = json.loads((Path(result['run_dir'])/'test_contract.json').read_text('utf-8'))
    assert contract['oracles'][0]['source_type'] == 'test_basis'
    assert contract['test_basis'] == basis
