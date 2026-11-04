"""Six-role WebAgent v0.3 pipeline.

A1/A2 produce a frozen contract; A3 generates allowlisted Selenium code;
A4 runs with the evidence plan; A5 evaluates and can request one targeted GUI
re-observation; A6 can request one locator-only regeneration on an execution
ERROR. No fault is reclassified as an application BUG due to a tool failure.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from .agents import (Agent1TestDesigner, Agent2TestReviewer,
                     Agent5EvidenceVerifier, Agent6RecoveryController)
from .dom_inspector import compact_dom, inspect_page
from .fixture_server import local_fixture, search_form
from .generator import Agent3SeleniumGenerator
from .probe import make_probe_request, merge_probe
from .report import html_report

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REQUIREMENT = '在 arXiv 中搜索“GUI Agent”，打开第一篇论文，确认结果列表与详情页的标题和论文 ID 一致。'
ARXIV_URL = "https://arxiv.org/search/advanced"


def _write_json(path: Path, obj: dict | list):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


def _run_script(script: Path, run_dir: Path, *, attempt: int, env: dict) -> dict:
    evidence_name = 'evidence.json' if attempt == 1 else f'evidence_attempt{attempt}.json'
    log_name = 'runner.log' if attempt == 1 else f'runner_attempt{attempt}.log'
    try:
        result = subprocess.run([sys.executable, str(script)], cwd=str(ROOT), env=env,
                                timeout=100, capture_output=True, text=True)
        (run_dir / log_name).write_text((result.stdout + '\n' + result.stderr)[-16000:], encoding='utf-8')
        p = run_dir / evidence_name
        if p.exists():
            evidence = json.loads(p.read_text(encoding='utf-8'))
            if result.returncode != 0 and evidence.get('execution_status') == 'COMPLETE':
                evidence['execution_status'] = 'ERROR'
                evidence['error_stage'] = 'subprocess_exit'
                evidence['error'] = f'Script exited with code {result.returncode} despite COMPLETE evidence'
                _write_json(p, evidence)
        else:
            evidence = {'execution_status': 'ERROR',
                        'error': 'Executor process failed: ' + (result.stderr or 'No evidence produced')[-1300:],
                        'error_stage': 'subprocess_execution', 'steps': [], 'screenshots': []}
            _write_json(p, evidence)
    except subprocess.TimeoutExpired:
        evidence = {'execution_status': 'ERROR', 'error': '执行超过 100 秒，子进程已终止',
                    'error_stage': 'subprocess_timeout', 'steps': [], 'screenshots': []}
        _write_json(run_dir / evidence_name, evidence)
    return evidence


def _missing_oracle_evidence(contract: dict, evidence: dict, checks: list[dict]) -> list[str]:
    missing_ids = {c.get('oracle_id') for c in checks if c.get('passed') is None}
    missing: list[str] = []
    for oracle in contract['oracles']:
        if oracle['id'] not in missing_ids:
            continue
        for path in (oracle['left'], oracle['right']):
            if path.startswith('query.'):
                continue
            from .evidence_plan import get_path
            value = get_path(evidence, path)
            if (value is None or isinstance(value, str) and not value.strip()) and path not in missing:
                missing.append(path)
    return missing


def _run_probe(request: dict, run_dir: Path, base_url: str, *, env: dict,
               headless: bool, demo_delay: float, allow_live: bool) -> dict:
    request_path = run_dir / 'probe_request.json'
    result_path = run_dir / 'probe_result.json'
    _write_json(request_path, request)
    args = [sys.executable, '-m', 'web_agent.probe_runner', '--request', str(request_path),
            '--output', str(result_path), '--base-url', base_url, '--demo-delay', str(demo_delay)]
    if not headless:
        args.append('--show-browser')
    if allow_live:
        args.append('--allow-live')
    try:
        proc = subprocess.run(args, cwd=str(ROOT), env=env, timeout=45,
                              capture_output=True, text=True)
        (run_dir / 'probe_runner.log').write_text((proc.stdout + '\n' + proc.stderr)[-10000:], encoding='utf-8')
        if result_path.exists():
            return json.loads(result_path.read_text(encoding='utf-8'))
        result = {'status': 'ERROR', 'error': 'Probe process failed: ' + (proc.stderr or 'No probe evidence')[-700:]}
    except subprocess.TimeoutExpired:
        result = {'status': 'ERROR', 'error': 'Probe exceeded its 45-second budget'}
    _write_json(result_path, result)
    return result


def run_pipeline(*, requirement: str = DEFAULT_REQUIREMENT, target: str = 'fixture',
                 mode: str = 'template', headless: bool = True, demo_delay: float = 0.0,
                 allow_live_site: bool = False, only_generate: bool = False,
                 output_root: Path | None = None, llm=None,
                 provider: str | None = None, model: str | None = None,
                 max_probes: int = 1, max_repairs: int = 1,
                 enable_live_probes: bool = False, enable_live_recovery: bool = False,
                 demo_evidence_gap: bool = False, demo_broken_locator: bool = False) -> dict:
    if target == 'arxiv' and not allow_live_site:
        raise ValueError('真实 arXiv 运行需要确认网站许可与访问规则：--allow-live-site')
    if target not in {'arxiv', 'fixture', 'fixture-bug'}:
        raise ValueError('Unsupported target')
    if mode not in {'template', 'llm'}:
        raise ValueError('Unsupported mode')
    if max_probes not in (0, 1) or max_repairs not in (0, 1):
        raise ValueError('v0.3 allows at most one probe and one repair')
    if (demo_evidence_gap or demo_broken_locator) and target not in {'fixture', 'fixture-bug'}:
        raise ValueError('故障注入只允许在本地合成测试站运行')
    if demo_evidence_gap and demo_broken_locator:
        raise ValueError('Please test one controlled fault injection per run')
    if mode == 'llm' and llm is None:
        from .llm_adapter import StructuredLLM
        llm = StructuredLLM(provider=provider, model=model)

    output_root = Path(output_root or ROOT / 'artifacts')
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = output_root / (datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6])
    run_dir.mkdir()
    stages: list[dict] = []
    try:
        designer, reviewer = Agent1TestDesigner(), Agent2TestReviewer()
        draft = designer.design(requirement, mode=mode, llm=llm)
        _write_json(run_dir / 'initial_draft_cases.json', draft)
        reviews = []
        for attempt in range(2):
            contract_obj, review = reviewer.review(draft, target=target, mode=mode, llm=llm)
            review['attempt'] = attempt + 1
            reviews.append(review)
            if review['approved']:
                break
            if attempt == 0:
                draft = designer.design(requirement, mode=mode, llm=llm, feedback=review['issues'])
        else:
            _write_json(run_dir / 'review_decision.json', {'approved': False, 'history': reviews})
            raise ValueError('Agent2 两次审核均拒绝用例，已终止')
        contract_obj.revision = len(reviews)
        contract = contract_obj.to_dict()
        _write_json(run_dir / 'draft_cases.json', draft)
        _write_json(run_dir / 'review_decision.json', {'approved': True, 'history': reviews})
        _write_json(run_dir / 'test_contract.json', contract)
        stages.append({'agent': 'Agent1', 'result': 'DESIGNED', 'file': 'draft_cases.json'})
        stages.append({'agent': 'Agent2', 'result': f'APPROVED R{len(reviews)}', 'file': 'review_decision.json'})

        context = local_fixture(bug=(target == 'fixture-bug')) if target != 'arxiv' else nullcontext((ARXIV_URL, None))
        with context as (base_url, _):
            try:
                if only_generate:
                    raise RuntimeError('Generate-only mode uses synthetic reference DOM')
                snapshot = inspect_page(base_url, headless=True)
                snapshot['inspector_source'] = 'real_selenium_dom'
            except Exception as err:
                if target == 'arxiv' and not only_generate:
                    raise RuntimeError('真实 arXiv DOM 无法读取；已停止盲目执行') from err
                snapshot = compact_dom(search_form())
                snapshot['inspector_source'] = 'bundled_local_reference_dom'
                snapshot['inspection_warning'] = f'DOM unavailable: {type(err).__name__}: {str(err)[:180]}'
            _write_json(run_dir / 'dom_snapshot.json', snapshot)
            generator = Agent3SeleniumGenerator()
            script = generator.generate(contract, snapshot, run_dir, base_url=base_url,
                                        headless=headless, demo_delay=demo_delay, mode=mode, llm=llm,
                                        demo_broken_locator=demo_broken_locator)
            stages.append({'agent': 'Agent3', 'result': 'GENERATED', 'file': 'generated_test.py',
                           'dom_source': snapshot['inspector_source']})
            if only_generate:
                evidence = {'execution_status': 'NOT_RUN', 'steps': [], 'screenshots': [],
                            'collection_log': [], 'error': '只生成脚本；浏览器没有执行'}
                _write_json(run_dir / 'evidence.json', evidence)
                verdict = Agent5EvidenceVerifier().verify(contract, evidence)
                recovery_history: list[dict] = []
            else:
                env = os.environ.copy()
                env['PYTHONPATH'] = str(ROOT) + os.pathsep + env.get('PYTHONPATH', '')
                evidence = _run_script(script, run_dir, attempt=1, env=env)
                verifier = Agent5EvidenceVerifier()
                verdict = verifier.verify(contract, evidence)
                recovery_history = []
                attempt_history = [{'attempt': 1, 'status': evidence.get('execution_status'),
                                    'error_stage': evidence.get('error_stage'), 'script': script.name}]

                # Agent6 -> Agent3 -> Agent4: one bounded locator-only recovery.
                controller = Agent6RecoveryController()
                repair = controller.plan_repair(verdict, evidence, target=target,
                     previous_repairs=0, max_repairs=max_repairs,
                     live_recovery_allowed=enable_live_recovery and allow_live_site)
                if repair:
                    _write_json(run_dir / 'repair_request.json', repair)
                    new_script = generator.generate(contract, snapshot, run_dir, base_url=base_url,
                        headless=headless, demo_delay=demo_delay, mode='template', llm=None,
                        repair_request=repair, attempt=2)
                    new_evidence = _run_script(new_script, run_dir, attempt=2, env=env)
                    attempt_history.append({'attempt': 2, 'status': new_evidence.get('execution_status'),
                                            'error_stage': new_evidence.get('error_stage'), 'script': new_script.name})
                    verdict = verifier.verify(contract, new_evidence)
                    recovery_history.append({'request': repair, 'script': new_script.name,
                                             'result_status': verdict.status,
                                             'original_error': evidence.get('error')})
                    # Keep failed first-run screenshots and diagnostic history.
                    for name in evidence.get('screenshots', []):
                        if name not in new_evidence.setdefault('screenshots', []):
                            new_evidence['screenshots'].append(name)
                    new_evidence['initial_error'] = evidence.get('error')
                    evidence = new_evidence
                    stages[2]['result'] = 'GENERATED + PATCHED'
                evidence['attempt_history'] = attempt_history

                # An explicit local-only fault-injection to demonstrate an actual
                # second Selenium session with targeted evidence acquisition.
                if demo_evidence_gap:
                    if not contract['query'].get('open_first_result', False) or evidence.get('execution_status') != 'COMPLETE':
                        raise ValueError('Evidence gap demo needs a completed paper detail run')
                    saved = evidence.get('detail', {}).pop('title', None)
                    # Keep the simulated acquisition gap consistent across all
                    # demo artifacts; do not leave the removed value in trace logs.
                    for event in evidence.get('collection_log', []):
                        if event.get('checkpoint') == 'detail_loaded':
                            event.get('observed', {}).pop('detail.title', None)
                            if 'detail.title' not in event.setdefault('missing', []):
                                event['missing'].append('detail.title')
                    for step in evidence.get('steps', []):
                        if step.get('stage') == 'detail_loaded':
                            step.pop('title', None)
                    _write_json(run_dir / 'controlled_evidence_gap.json',
                                {'injection': 'simulate an omitted detail.title evidence field',
                                 'original_value_present': bool(saved), 'synthetic': True,
                                 'not_an_application_bug': True})
                    _write_json(run_dir / 'evidence.json', evidence)
                    verdict = verifier.verify(contract, evidence)
                if verdict.status == 'INCONCLUSIVE' and max_probes:
                    missing = _missing_oracle_evidence(contract, evidence, verdict.checks)
                    request = make_probe_request(contract, evidence, missing=missing)
                    if request and (target != 'arxiv' or enable_live_probes and allow_live_site):
                        probe = _run_probe(request, run_dir, base_url, env=env,
                                           headless=headless, demo_delay=demo_delay,
                                           allow_live=target == 'arxiv' and allow_live_site)
                        evidence = merge_probe(evidence, probe, request)
                        _write_json(run_dir / 'evidence.json', evidence)
                        verdict = verifier.verify(contract, evidence)
                _write_json(run_dir / 'recovery_history.json', recovery_history)
                _write_json(run_dir / 'evidence.json', evidence)
            stages.append({'agent': 'Agent4', 'result': evidence.get('execution_status'), 'file': 'evidence.json'})
            stages.append({'agent': 'Agent5', 'result': verdict.status,
                           'file': 'final_verdict.json', 'probes': len(evidence.get('probes', []))})

        final = Agent6RecoveryController().finalize(verdict, run_dir=run_dir,
                                                    recovery_history=recovery_history)
        stages.append({'agent': 'Agent6', 'result': 'FINALIZED', 'file': 'report.html',
                       'repairs': len(recovery_history)})
        report_path = html_report(run_dir, contract, evidence, final, inspection=snapshot)
        _write_json(run_dir / 'stages.json', stages)
        return {'run_dir': str(run_dir), 'report': str(report_path), 'verdict': final,
                'stages': stages, 'inspector': snapshot, 'contract': contract,
                'evidence': evidence, 'reviews': reviews,
                'probe_count': len(evidence.get('probes', [])),
                'repair_count': len(recovery_history)}
    except Exception as exc:
        (run_dir / 'pipeline_error.txt').write_text(f'{type(exc).__name__}: {exc}', encoding='utf-8')
        raise
