"""Stage 4 integration contracts: target scope, UI/CLI result shape and reports."""
import json
from pathlib import Path

import pytest

from web_agent.pipeline import run_pipeline
from web_agent.targets import dynamic_target, target_metadata, bind_template
from web_agent.dynamic_execution import ExecutionPolicy
from .test_v04_planning import search_design, form_design, approval, QueueLLM
from .test_v04_execution import reviewed, GroundingModel, REAL
from .test_v04_verification import one_oracle, CheckModel
from web_agent.dynamic_schema import requirement_id


def acceptance_design(scenario, base):
    """Mock Agent1 response, authored before acceptance; no executable business code."""
    if scenario == 'download':
        return one_oracle('file', 'file_valid', True, action='download', base=base, path='/download').design.to_dict()
    raw = reviewed(form_design if scenario == 'form' else search_design, base).design.to_dict()
    old_ids = [r['requirement_id'] for r in raw['requirements']]
    if scenario == 'form':
        raw = json.loads(json.dumps(raw, ensure_ascii=False).replace('张三', '李四').replace('杭州', '北京'))
    else:
        raw['requirement'] += ' 分类选择“机器人”，确认已应用分类为“机器人”。'
    for row in raw['requirements']:
        row['requirement_id'] = requirement_id(raw['requirement'], row['source_quote'])
    new_ids = [r['requirement_id'] for r in raw['requirements']]
    for rows in (raw['steps'], raw['oracles']):
        for row in rows:
            row['requirement_ids'] = [new_ids[old_ids.index(rid)] for rid in row['requirement_ids']]
    if scenario == 'catalog':
        action_quote, assertion_quote = '分类选择“机器人”', '确认已应用分类为“机器人”'
        arid, vrid = [requirement_id(raw['requirement'], q) for q in (action_quote, assertion_quote)]
        raw['requirements'].extend([{'requirement_id': arid, 'text': action_quote, 'source_quote': action_quote, 'kind': 'action'},
                                    {'requirement_id': vrid, 'text': assertion_quote, 'source_quote': assertion_quote, 'kind': 'assertion'}])
        raw['test_data'].append({'data_id': 'D_category', 'value': '机器人', 'source_quote': action_quote})
        raw['steps'].insert(2, {'step_id': 'S_filter', 'requirement_ids': [arid], 'action': 'select',
            'target': {'description': '分类筛选', 'role': 'semantic_control', 'position': None},
            'depends_on': ['S_query'], 'precondition_ids': [],
            'inputs': {'value': {'kind': 'test_data', 'data_id': 'D_category'}}, 'outputs': []})
        next(s for s in raw['steps'] if s['step_id'] == 'S_search')['depends_on'] = ['S_filter']
        raw['steps'].insert(4, {'step_id': 'S_filter_state', 'requirement_ids': [vrid], 'action': 'observe',
            'target': {'description': '已应用分类', 'role': 'semantic_control', 'position': None},
            'depends_on': ['S_search'], 'precondition_ids': [], 'inputs': {}, 'outputs': ['category']})
        next(s for s in raw['steps'] if s['step_id'] == 'S_result')['depends_on'] = ['S_filter_state']
        raw['evidence_requirements'].append({'evidence_id': 'E_category', 'step_id': 'S_filter_state',
            'output': 'category', 'description': '筛选后的已应用分类', 'kind': 'text'})
        raw['oracles'].append({'oracle_id': 'O_category', 'requirement_ids': [vrid], 'after_step_id': 'S_filter_state',
            'relation': 'equals', 'actual': {'kind': 'evidence', 'evidence_id': 'E_category'},
            'expected': {'kind': 'test_data', 'data_id': 'D_category'}, 'evidence_ids': ['E_category']})
    return raw


class FullPipelineModel:
    """Mock A1/A2 + live-DOM A3. No real provider inference is claimed."""
    def __init__(self, raw, *, download=False):
        self.raw, self.calls = raw, []
        self.grounder = CheckModel() if download else GroundingModel()
    def complete_json(self, system, user):
        payload = json.loads(user)
        self.calls.append(payload)
        if 'wire_guide' in payload:
            assert payload['original_requirement'] == self.raw['requirement']
            return self.raw
        if 'candidate_design' in payload:
            return approval(payload['candidate_design'])
        return self.grounder.complete_json(system, user)


@REAL
@pytest.mark.parametrize('scenario,variant', [('catalog', None), ('catalog', 'layout'), ('form', 'layout'), ('download', None)])
def test_real_chrome_full_six_agent_pipeline_across_scenarios(tmp_path, scenario, variant):
    from web_agent.dynamic_fixture import dynamic_fixture
    import hashlib
    with dynamic_fixture(variant=variant) as (base, _):
        raw = acceptance_design(scenario, base)
        model = FullPipelineModel(raw, download=scenario == 'download')
        from urllib.request import urlopen
        with urlopen(raw['target_url'], timeout=3) as response:
            prepared_digest = hashlib.sha256(response.read()).hexdigest()
        # The held-out layout is fixed before planning/generation, not repaired
        # with page-specific selector changes after a failure.
        (tmp_path / 'prepared_variant.json').write_text(json.dumps({'scenario': scenario, 'variant': variant,
            'requirement': raw['requirement'], 'prepared_page_sha256': prepared_digest,
            'prompt_sha256': hashlib.sha256(Path('web_agent/agents/generator.py').read_bytes()).hexdigest()}, ensure_ascii=False), encoding='utf-8')
        result = run_pipeline(engine='dynamic', mode='llm', execute_dynamic=True,
            requirement=raw['requirement'], target_url=raw['target_url'], llm=model, output_root=tmp_path,
            execution_policy=ExecutionPolicy(allow_downloads=scenario == 'download', step_timeout=12))
    assert result['verdict']['status'] == 'PASS', result
    assert [s['agent'] for s in result['stages']] == ['Agent1', 'Agent2', 'Agent3', 'Agent4', 'Agent5', 'Agent6']
    assert 'wire_guide' in model.calls[0] and 'candidate_design' in model.calls[1]
    assert result['session_closed'] and len({s['session_id'] for s in result['steps']}) == 1
    root = Path(result['run_dir'])
    assert Path(result['report']).exists()
    for step in result['steps']:
        assert hashlib.sha256((root / step['source_file']).read_text(encoding='utf-8').encode()).hexdigest() == step['source_sha256']
    if scenario == 'catalog':
        assert result['evidence']['E_category']['value'] == '机器人'
        assert result['evidence']['E_detail']['value'] == '机器人乙'
    if scenario == 'form':
        assert result['evidence']['E_preview']['value'] == '李四'
        assert result['evidence']['E_retained']['value'] == '李四'
    (tmp_path / 'acceptance_result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


@REAL
def test_real_chrome_missing_control_is_error_never_fixture_pass(tmp_path):
    from web_agent.dynamic_fixture import dynamic_fixture
    with dynamic_fixture(variant='missing') as (base, _):
        raw = acceptance_design('catalog', base)
        result = run_pipeline(engine='dynamic', mode='llm', execute_dynamic=True,
            requirement=raw['requirement'], target_url=raw['target_url'], llm=FullPipelineModel(raw), output_root=tmp_path)
    assert result['verdict']['status'] == 'ERROR' and result['session_closed']
    assert result['step_states']['S_query'] == 'ERROR'


@REAL
def test_real_chrome_builtin_template_owned_fixture_no_model(tmp_path):
    raw = json.loads(Path('examples/download_design.json').read_text(encoding='utf-8'))
    result = run_pipeline(engine='dynamic', mode='template', execute_dynamic=True, fixture_site='download',
        template_design=raw, requirement=raw['requirement'], output_root=tmp_path,
        execution_policy=ExecutionPolicy(allow_downloads=True, step_timeout=12))
    assert result['verdict']['status'] == 'PASS' and result['session_closed'], result
    assert result['reviews'][0]['semantic_review_performed'] is False
    assert Path(result['report']).is_file()


def test_dynamic_target_catalog_is_loopback_and_exact_origin():
    with dynamic_target(fixture_site='catalog') as url:
        info = target_metadata(url)
        assert info['scope'] == 'loopback'
        assert info['allowed_origins'] == [info['allowed_origins'][0]]
        assert url.endswith('/search')


def test_target_configuration_rejects_mixed_or_implicit_external_scope():
    with pytest.raises(ValueError):
        with dynamic_target(fixture_site='catalog', target_url='http://127.0.0.1:1/search'):
            pass
    with pytest.raises(ValueError):
        ExecutionPolicy(allow_live_probes=True)
    with pytest.raises(ValueError):
        run_pipeline(engine='legacy', fixture_site='catalog')


def test_dynamic_planning_result_has_target_config_and_html_report(tmp_path):
    raw = search_design()
    result = run_pipeline(engine='dynamic', mode='llm', requirement=raw['requirement'],
        target_url=raw['target_url'], llm=QueueLLM(raw, approval(raw)), output_root=tmp_path)
    report = Path(result['report'])
    assert result['planning_status'] == 'PLANNED'
    assert result['execution_status'] == 'NOT_RUN'
    assert result['target_config']['navigation_policy'] == 'exact_same_origin'
    assert report.exists() and 'v0.4' in report.read_text(encoding='utf-8')
    assert json.loads((Path(result['run_dir']) / 'dynamic_result.json').read_text(encoding='utf-8'))['target_config']


def test_dynamic_template_binding_only_accepts_exact_requirement_and_url_placeholder():
    raw = form_design()
    template = raw.copy()
    template['target_url'] = '$TARGET_URL'
    bound = bind_template(template, requirement=raw['requirement'], target_url='http://127.0.0.1:1/apply')
    assert bound['target_url'] == 'http://127.0.0.1:1/apply'
    with pytest.raises(ValueError):
        bind_template(template, requirement='different requirement', target_url='http://127.0.0.1:1/apply')


def test_streamlit_legacy_and_dynamic_planning_are_renderable(tmp_path):
    from streamlit.testing.v1 import AppTest
    # Real Streamlit execution and widget events; template planning has no LLM
    # or browser and verifies the UI's actual backend argument wiring.
    at = AppTest.from_file('app_ui.py', default_timeout=20).run()
    assert not at.exception
    assert at.radio[0].value.startswith('v0.3')
    at.radio[0].set_value('v0.4 动态流程').run()
    at.selectbox[0].set_value('指定本地起始 URL').run()
    contract = reviewed()
    raw = contract.design.to_dict()
    at.text_area[0].set_value(raw['requirement'])
    at.text_area[1].set_value(json.dumps(raw, ensure_ascii=False))
    at.text_input[0].set_value(raw['target_url'])
    next(x for x in at.checkbox if x.label == '只生成脚本，不运行浏览器').check()
    at.button[0].click().run()
    assert not at.exception
    result = at.session_state['result']
    assert result['engine'] == 'dynamic' and result['execution_status'] == 'NOT_RUN'
    assert result['verdict']['status'] == 'INCONCLUSIVE'
    assert Path(result['report']).is_file()


def test_streamlit_rejected_result_has_no_missing_legacy_fields(tmp_path):
    from streamlit.testing.v1 import AppTest
    raw = form_design()
    rejection = approval(raw, False, ['需求尚未覆盖'])
    result = run_pipeline(engine='dynamic', mode='llm', requirement=raw['requirement'],
        target_url=raw['target_url'], llm=QueueLLM(raw, rejection, raw, rejection), output_root=tmp_path)
    at = AppTest.from_file('app_ui.py', default_timeout=20)
    at.session_state['result'] = result
    at.run()
    assert not at.exception
    assert result['contract'] is None and Path(result['report']).is_file()


def test_cli_dynamic_template_planning_outputs_dynamic_result(tmp_path):
    import subprocess, sys, os
    raw = reviewed().design.to_dict()
    template = tmp_path / 'design.json'
    template.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
    completed = subprocess.run([sys.executable, '-B', 'run_demo.py', '--engine', 'dynamic',
        '--mode', 'template', '--design-file', str(template), '--requirement', raw['requirement'],
        '--target-url', raw['target_url']], capture_output=True, encoding='utf-8',
        env=dict(os.environ, PYTHONIOENCODING='utf-8'))
    assert completed.returncode == 0, completed.stderr
    output = json.loads(completed.stdout)
    assert output['workflow'] == 'dynamic' and output['status'] == 'INCONCLUSIVE'
    assert Path(output['report']).is_file()


def test_dynamic_external_permission_checked_before_model_or_browser(tmp_path):
    raw = search_design()
    model = QueueLLM()
    with pytest.raises(ValueError, match='allow_live_site'):
        run_pipeline(engine='dynamic', mode='llm', execute_dynamic=True, requirement=raw['requirement'],
            target_url=raw['target_url'], llm=model, output_root=tmp_path)
    assert not model.calls


def test_disabled_actions_are_rejected_before_opening_browser(monkeypatch, tmp_path):
    from web_agent.agents import Agent4TestExecutor
    from web_agent.pipeline import run_dynamic_contract
    def forbidden(*a, **kw):
        raise AssertionError('Browser must not be created for disallowed actions')
    monkeypatch.setattr(Agent4TestExecutor, 'open_dynamic', forbidden)
    result = run_dynamic_contract(reviewed(), tmp_path, policy=ExecutionPolicy(allowed_actions=('observe',)))
    assert result['verdict']['status'] == 'ERROR' and result['error']['category'] == 'permission'
    assert result['session'] is None


def test_builtin_template_and_owned_fixture_planning_without_model(tmp_path):
    raw = json.loads(Path('examples/download_design.json').read_text(encoding='utf-8'))
    result = run_pipeline(engine='dynamic', mode='template', fixture_site='download',
        template_design=raw, requirement=raw['requirement'], output_root=tmp_path)
    assert result['contract']['design']['target_url'].startswith('http://127.0.0.1:')
    assert result['verdict']['status'] == 'INCONCLUSIVE' and result['execution_status'] == 'NOT_RUN'


def test_dynamic_report_escapes_code_and_survives_review_rejection(tmp_path):
    from web_agent.report import dynamic_html_report
    run_dir = tmp_path
    source = run_dir / 'generation/S_test_attempt1/dynamic_S_test.py'
    source.parent.mkdir(parents=True)
    source.write_text('# <script>unsafe()</script>', encoding='utf-8')
    result = {'contract': None, 'design': {'requirement': '<img src=x onerror=alert(1)>', 'target_url': 'http://127.0.0.1/'},
              'verdict': {'status': 'INCONCLUSIVE', 'message': 'Rejected', 'checks': []}, 'reviews': [{'approved': False}]}
    report = dynamic_html_report(run_dir, result).read_text(encoding='utf-8')
    assert '<script>unsafe()' not in report and '&lt;script&gt;' in report
    assert '<img src=x' not in report and 'dynamic_S_test.py' in report


def test_streamlit_completed_dynamic_result_displays_sources_dom_and_evidence(tmp_path):
    from streamlit.testing.v1 import AppTest
    from web_agent.report import dynamic_html_report
    contract = reviewed()
    source = tmp_path / 'generation/S_query_attempt1/dynamic_S_query.py'
    source.parent.mkdir(parents=True)
    source.write_text('outputs = {}\n', encoding='utf-8')
    (source.parent / 'generation_S_query.json').write_text('{"step_id":"S_query"}', encoding='utf-8')
    (tmp_path / 'observation_001.json').write_text('{"observation_id":"obs_1"}', encoding='utf-8')
    result = {'engine': 'dynamic', 'run_dir': str(tmp_path), 'contract': contract.to_dict(),
        'design': contract.design.to_dict(), 'evidence': {}, 'reviews': [], 'stages': [],
        'verdict': {'status': 'PASS', 'message': 'Mock rendering result', 'checks': []}}
    result['report'] = str(dynamic_html_report(tmp_path, result))
    at = AppTest.from_file('app_ui.py', default_timeout=20)
    at.session_state['result'] = result
    at.run()
    assert not at.exception
    assert any('dynamic_S_query.py' in x.label for x in at.expander)
    assert any(x.label == 'observation_001.json' for x in at.expander)
