"""v0.3: targeted evidence acquisition and bounded script recovery.

All subprocesses in these tests are mocked. These are not live Selenium runs.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from web_agent.agents import Agent1TestDesigner, Agent2TestReviewer, Agent5EvidenceVerifier, Agent6RecoveryController
from web_agent.evidence_plan import collect_checkpoint
from web_agent.probe import make_probe_request, merge_probe, _validate_request
from web_agent.generator import Agent3SeleniumGenerator
from web_agent.fixture_server import search_form
from web_agent.dom_inspector import compact_dom
from .test_contract import build, sample_evidence


def test_evidence_plan_collects_only_selected_fields():
    state = {'search': {'entered_keyword': 'GUI Agent', 'applied_keyword': 'GUI Agent'},
             'screenshots': [], 'collection_log': []}
    captures = []
    def capture(name):
        captures.append(name)
        state['screenshots'].append(name + '.png')
    plan = [{'checkpoint': 'form_filled', 'collect': ['search.entered_keyword', 'screenshot']},
            {'checkpoint': 'search_submitted', 'collect': ['search.applied_keyword']}]
    a = collect_checkpoint(state, plan, 'form_filled', capture=capture, screenshot_name='01_form')
    b = collect_checkpoint(state, plan, 'search_submitted', capture=capture)
    assert a['observed']['search.entered_keyword'] == 'GUI Agent'
    assert b['observed']['search.applied_keyword'] == 'GUI Agent'
    assert captures == ['01_form']
    assert len(state['collection_log']) == 2


def test_plan_reports_missing_and_rejects_unknown_fields():
    e = {'search': {}, 'screenshots': []}
    row = collect_checkpoint(e, [{'checkpoint':'x', 'collect':['search.applied_keyword']}], 'x')
    assert row['missing'] == ['search.applied_keyword']
    with pytest.raises(ValueError):
        collect_checkpoint(e, [{'checkpoint':'x', 'collect':['credentials.password']}], 'x')


def test_probe_request_is_targeted_to_missing_detail(tmp_path):
    c = build()
    e = sample_evidence()
    e['detail']['detail_url'] = 'http://127.0.0.1:8765/abs/2601.00001'
    e['detail'].pop('title')
    v = Agent5EvidenceVerifier().verify(c, e)
    assert v.status == 'INCONCLUSIVE'
    request = make_probe_request(c, e, missing=['detail.title'])
    assert request['type'] == 'detail_reobserve'
    assert request['fields'] == ['detail.title']
    assert request['expected_id'] == e['results']['arxiv_id']


def test_probe_rejects_wrong_paper_and_unsafe_url():
    c = build()
    e = sample_evidence()
    e['detail']['detail_url'] = 'http://127.0.0.1:8765/abs/2601.00001'
    e['detail'].pop('title')
    request = make_probe_request(c, e, missing=['detail.title'])
    assert request
    with pytest.raises(ValueError):
        _validate_request({**request, 'url': 'https://attacker.test/abs/2601.00001'},
                          'http://127.0.0.1:8765/search/advanced')
    assert make_probe_request(c, {**e, 'results': {'arxiv_id': '9999.00001'}}, missing=['detail.title']) is None
    assert make_probe_request(c, e, missing=['detail.title', 'results.title']) is None


def test_merge_probe_adds_missing_but_does_not_overwrite():
    c = build()
    e = sample_evidence()
    e['detail']['detail_url'] = 'http://127.0.0.1:8765/abs/2601.00001'
    e['detail'].pop('title')
    request = make_probe_request(c, e, missing=['detail.title'])
    probe = {'status': 'COMPLETE', 'fields': {'detail': {'title': e['results']['title']}},
             'steps': [{'stage': 'reobserve_detail'}], 'screenshots': ['probe_01_detail.png']}
    merge_probe(e, probe, request)
    assert Agent5EvidenceVerifier().verify(c, e).status == 'PASS'
    assert e['probes'][0]['status'] == 'COMPLETE'
    # Even a falsely claimed probe success must not erase a conflicting existing observation.
    e['detail']['title'] = 'Existing truth'
    merge_probe(e, probe, request)
    assert e['detail']['title'] == 'Existing truth'


def test_recovery_agent_restricts_repair_and_stops_on_application_fail(tmp_path):
    ctl = Agent6RecoveryController()
    c = build()
    e = sample_evidence(bad=True)
    v = Agent5EvidenceVerifier().verify(c, e)
    assert v.status == 'FAIL'
    assert ctl.plan_repair(v, e, target='fixture') is None
    e = {'execution_status': 'ERROR', 'error_stage': 'locate_keyword', 'error': 'timeout'}
    v = Agent5EvidenceVerifier().verify(c, e)
    request = ctl.plan_repair(v, e, target='fixture')
    assert request['locator_key'] == 'keyword'
    assert ctl.plan_repair(v, e, target='fixture', previous_repairs=1) is None
    assert ctl.plan_repair(v, e, target='arxiv') is None
    assert ctl.plan_repair(v, {**e, 'error_stage': 'wait_results_loaded'}, target='fixture') is None


def test_generator_repairs_only_allowlisted_locators(tmp_path):
    c = build()
    snapshot = compact_dom(search_form())
    gen = Agent3SeleniumGenerator()
    gen.generate(c, snapshot, tmp_path, base_url='http://127.0.0.1:8765/search/advanced',
                 demo_broken_locator=True)
    plan = json.loads((tmp_path / 'locator_plan.json').read_text(encoding='utf-8'))
    assert plan['keyword'] == ['#__demo_missing_keyword__']
    request = {'repair_type': 'recompute_allowlisted_locator', 'locator_key': 'keyword',
               'failure_stage': 'locate_keyword'}
    updated = gen.generate(c, snapshot, tmp_path, base_url='http://127.0.0.1:8765/search/advanced',
                           mode='template', repair_request=request, attempt=2)
    new_plan = json.loads((tmp_path / 'locator_plan_attempt2.json').read_text(encoding='utf-8'))
    assert new_plan['keyword'][0] == '#terms-0-term'
    assert 'evidence_attempt2.json' in updated.read_text(encoding='utf-8')
    assert 'evidence.json' in (tmp_path / 'generated_test.py').read_text(encoding='utf-8')
    assert c['oracles'] == build()['oracles']
    with pytest.raises(ValueError):
        gen.generate(c, snapshot, tmp_path, base_url='http://localhost',
                     repair_request={**request, 'locator_key': 'arbitrary_python'}, attempt=2)


def _mock_inspector(monkeypatch):
    import web_agent.pipeline as pipe
    monkeypatch.setattr(pipe, 'inspect_page', lambda *a, **kw: {
        **compact_dom(search_form()), 'inspector_source': 'real_selenium_dom'})


def test_pipeline_agent5_active_reobservation(tmp_path, monkeypatch):
    import web_agent.pipeline as pipe
    _mock_inspector(monkeypatch)
    calls = []
    def fake_run(args, **kwargs):
        calls.append(args)
        if args[1] == '-m':
            result_path = Path(args[args.index('--output')+1])
            result_path.write_text(json.dumps({'status': 'COMPLETE', 'fields': {
                'detail': {'title': 'Evaluating GUI Agents for Software Testing'}},
                'screenshots': ['probe_01_detail.png'], 'steps': [{'stage':'reobserve_detail'}]}), encoding='utf-8')
        else:
            data = sample_evidence()
            data['detail']['detail_url'] = 'http://127.0.0.1:8765/abs/2601.00001'
            Path(args[1]).with_name('evidence.json').write_text(json.dumps(data), encoding='utf-8')
        return SimpleNamespace(stdout='mocked', stderr='', returncode=0)
    monkeypatch.setattr(pipe.subprocess, 'run', fake_run)
    r = pipe.run_pipeline(target='fixture', demo_evidence_gap=True, output_root=tmp_path)
    assert r['verdict']['status'] == 'PASS'
    assert r['probe_count'] == 1
    assert r['repair_count'] == 0
    assert any(args[1] == '-m' for args in calls)
    d = Path(r['run_dir'])
    assert (d / 'probe_request.json').exists()
    assert 'Agent5 · 主动补证' in (d / 'report.html').read_text(encoding='utf-8')
    assert (d / 'controlled_evidence_gap.json').exists()


def test_pipeline_agent6_regenerates_after_locator_fault(tmp_path, monkeypatch):
    import web_agent.pipeline as pipe
    _mock_inspector(monkeypatch)
    def fake_run(args, **kwargs):
        file = Path(args[1])
        if file.name == 'generated_test.py':
            data = {'execution_status': 'ERROR', 'error_stage': 'locate_keyword',
                    'error': 'TimeoutException [locate_keyword]', 'screenshots': [], 'steps': []}
            path = file.with_name('evidence.json')
        else:
            data = sample_evidence()
            path = file.with_name('evidence_attempt2.json')
        path.write_text(json.dumps(data), encoding='utf-8')
        return SimpleNamespace(stdout='mock', stderr='', returncode=0)
    monkeypatch.setattr(pipe.subprocess, 'run', fake_run)
    r = pipe.run_pipeline(target='fixture', demo_broken_locator=True, output_root=tmp_path)
    assert r['verdict']['status'] == 'PASS'
    assert r['repair_count'] == 1
    assert r['probe_count'] == 0
    assert r['verdict']['auto_repair_performed'] is True
    d = Path(r['run_dir'])
    assert (d / 'repair_request.json').exists()
    assert (d / 'generated_test_attempt2.py').exists()
    assert (d / 'evidence_attempt2.json').exists()
    assert r['evidence']['attempt_history'][0]['status'] == 'ERROR'
    assert r['evidence']['attempt_history'][1]['status'] == 'COMPLETE'


def test_pipeline_does_not_retry_a_confirmed_app_bug(tmp_path, monkeypatch):
    import web_agent.pipeline as pipe
    _mock_inspector(monkeypatch)
    calls = []
    def fake_run(args, **kwargs):
        calls.append(args)
        Path(args[1]).with_name('evidence.json').write_text(json.dumps(sample_evidence(bad=True)), encoding='utf-8')
        return SimpleNamespace(stdout='mock', stderr='', returncode=0)
    monkeypatch.setattr(pipe.subprocess, 'run', fake_run)
    r = pipe.run_pipeline(target='fixture-bug', output_root=tmp_path)
    assert r['verdict']['status'] == 'FAIL'
    assert len(calls) == 1
    assert r['repair_count'] == 0


def test_fault_injection_cannot_target_real_arxiv(tmp_path):
    import web_agent.pipeline as pipe
    with pytest.raises(ValueError, match='本地'):
        pipe.run_pipeline(target='arxiv', allow_live_site=True, demo_broken_locator=True,
                          output_root=tmp_path)
