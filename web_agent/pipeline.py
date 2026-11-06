"""Six-role WebAgent v0.3 pipeline.

A1/A2 produce a frozen contract; A3 generates allowlisted Selenium code;
A4 runs with the evidence plan; A5 evaluates and can request one targeted GUI
re-observation; A6 can request one locator-only regeneration on an execution
ERROR. No fault is reclassified as an application BUG due to a tool failure.
engine=dynamic plans through A1/A2; execute_dynamic=True additionally runs the
the persistent A3/A4 loop and A5 verification with bounded A6 recovery.
mode selects template/LLM independently.
"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import uuid

from .agents import (Agent1TestDesigner, Agent2TestReviewer,
                     Agent4TestExecutor, Agent5EvidenceVerifier, Agent6RecoveryController)
from .agents.verifier import missing_oracle_evidence as _missing_oracle_evidence
from .dom_inspector import compact_dom, inspect_page
from .fixture_server import local_fixture, search_form
from .generator import Agent3SeleniumGenerator
from .probe import merge_probe
from .report import html_report
from .schema import JSONModel, TestDesign, Verdict

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REQUIREMENT = '在 arXiv 中搜索“GUI Agent”，打开第一篇论文，确认结果列表与详情页的标题和论文 ID 一致。'
ARXIV_URL = "https://arxiv.org/search/advanced"


def _write_json(path: Path, obj: dict | list):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


def _run_script(script: Path, run_dir: Path, *, attempt: int, env: dict) -> dict:
    """Compatibility hook; execution belongs to Agent4."""
    return Agent4TestExecutor(root=ROOT, runner=subprocess.run).execute(
        script, run_dir, attempt=attempt, env=env)


def _run_probe(request: dict, run_dir: Path, base_url: str, *, env: dict,
               headless: bool, demo_delay: float, allow_live: bool) -> dict:
    """Compatibility hook; Pipeline has already authorized this probe."""
    return Agent4TestExecutor(root=ROOT, runner=subprocess.run).probe(
        request, run_dir, base_url, env=env, headless=headless,
        demo_delay=demo_delay, allow_live=allow_live)


def run_pipeline(*, requirement: str = DEFAULT_REQUIREMENT, target: str = 'fixture',
                 mode: str = 'template', headless: bool = True, demo_delay: float = 0.0,
                 allow_live_site: bool = False, only_generate: bool = False,
                 output_root: Path | None = None, llm: JSONModel | None = None,
                 provider: str | None = None, model: str | None = None,
                 max_probes: int = 1, max_repairs: int = 1,
                 enable_live_probes: bool = False, enable_live_recovery: bool = False,
                 demo_evidence_gap: bool = False, demo_broken_locator: bool = False,
                 engine: str = 'legacy', target_url: str | None = None,
                 template_design: TestDesign | dict | None = None,
                 execute_dynamic: bool = False, execution_policy=None,
                 fixture_site: str | None = None) -> dict:
    if engine not in {'legacy', 'dynamic'}:
        raise ValueError('engine must be legacy or dynamic')
    if mode not in {'template', 'llm'}:
        raise ValueError('Unsupported mode')
    if engine == 'dynamic':
        if fixture_site is not None:
            from .targets import dynamic_target
            with dynamic_target(target_url=target_url, fixture_site=fixture_site) as url:
                return run_pipeline(requirement=requirement, mode=mode, engine=engine,
                    headless=headless, allow_live_site=allow_live_site, only_generate=only_generate,
                    output_root=output_root, llm=llm, provider=provider, model=model,
                    max_probes=max_probes, max_repairs=max_repairs,
                    enable_live_probes=enable_live_probes, enable_live_recovery=enable_live_recovery,
                    demo_evidence_gap=demo_evidence_gap, demo_broken_locator=demo_broken_locator,
                    target_url=url, template_design=template_design,
                    execute_dynamic=execute_dynamic, execution_policy=execution_policy)
        if demo_evidence_gap or demo_broken_locator:
            raise ValueError('Legacy execution/recovery options are not available for the dynamic engine')
        if execute_dynamic and only_generate:
            raise ValueError('execute_dynamic and only_generate are mutually exclusive')
        if type(execute_dynamic) is not bool:
            raise ValueError('execute_dynamic must be boolean')
        from .dynamic_execution import ExecutionPolicy
        from .targets import bind_template, target_metadata
        target_config = target_metadata(target_url)
        policy = execution_policy or ExecutionPolicy(allow_live_site=allow_live_site,
            max_probes=max_probes, max_repairs=max_repairs,
            allow_live_probes=enable_live_probes, allow_live_recovery=enable_live_recovery)
        if not isinstance(policy, ExecutionPolicy):
            raise ValueError('Invalid execution_policy')
        if policy.allow_live_site and not allow_live_site:
            raise ValueError('Live execution policy requires allow_live_site=True')
        if execute_dynamic:
            policy.authorize(target_url, target_url)
        if mode == 'template':
            template_design = bind_template(template_design, requirement=requirement, target_url=target_url)
        if mode == 'llm' and llm is None:
            from .llm_adapter import StructuredLLM
            llm = StructuredLLM(provider=provider, model=model)
        result = _plan_dynamic(requirement=requirement, target_url=target_url, mode=mode,
                             output_root=output_root, llm=llm, provider=provider, model=model,
                             template_design=template_design)
        result['target_config'] = target_config
        if execute_dynamic and result['contract']:
            from .schema import parse_contract
            execution = run_dynamic_contract(parse_contract(result['contract']), Path(result['run_dir']),
                mode=mode, llm=llm, policy=policy, headless=headless)
            result.update(execution)
            result['stages'].extend([
                {'agent': 'Agent3', 'result': 'GENERATED' if execution['steps'] else 'NOT_COMPLETED',
                 'completed_steps': len(execution['steps']), 'files': 'dynamic_S_*.py / generation_S_*.json'},
                {'agent': 'Agent4', 'result': execution['execution_status'], 'file': 'dynamic_execution.json'},
                {'agent': 'Agent5', 'result': execution['verdict']['status'], 'file': 'final_verdict.json'},
                {'agent': 'Agent6', 'result': 'REPAIRED' if execution.get('repair_count') else 'NO_REPAIR',
                 'file': 'dynamic_recovery.json'}])
            _write_json(Path(result['run_dir']) / 'stages.json', result['stages'])
        from .report import dynamic_html_report
        result['report'] = str(dynamic_html_report(Path(result['run_dir']), result))
        _write_json(Path(result['run_dir']) / 'dynamic_result.json', result)
        return result
    if fixture_site is not None:
        raise ValueError('fixture_site requires engine=dynamic')
    if execute_dynamic or execution_policy is not None:
        raise ValueError('Dynamic execution options require engine=dynamic')
    if target_url is not None or template_design is not None:
        raise ValueError('target_url/template_design require engine=dynamic')
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
                    request = verifier.plan_probe(contract, evidence, verdict)
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


def run_dynamic_contract(contract, run_dir: Path, *, mode: str = 'llm', llm: JSONModel | None = None,
                         policy=None, headless=True) -> dict:
    """Frozen contract -> same-session execution, checkpoint probes and verification.

    Recovery changes only the current execution method and always uses fresh DOM.
    Confirmed oracle failure is terminal. Read probes never replay business actions.
    """
    from queue import Queue, Empty
    from threading import Thread
    import time
    import ipaddress
    from urllib.parse import urlsplit
    from .dynamic_execution import ExecutionPolicy, ExecutionRejected
    from .dynamic_schema import DynamicTestContract
    from .dynamic_verification import merge_dynamic, validate_probe

    if not isinstance(contract, DynamicTestContract):
        raise ValueError('run_dynamic_contract requires a reviewed v0.4 contract')
    contract.assert_integrity()
    policy = policy or ExecutionPolicy()
    if not isinstance(policy, ExecutionPolicy):
        raise ValueError('Invalid execution_policy')
    if mode not in ('llm', 'template'):
        raise ValueError('Unsupported grounding mode')
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(run_dir / 'test_contract.json', contract.to_dict())
    deadline = time.monotonic() + policy.total_timeout
    states = {s.step_id: 'PENDING' for s in contract.design.steps}
    results, state, evidence = [], {}, {}
    probes, recovery_history, execution_attempts = [], [], []
    observations = repairs = 0
    session = None
    current_step = None
    error = None
    terminal_failure = False
    generator, executor = Agent3SeleniumGenerator(), Agent4TestExecutor()
    verifier, recovery = Agent5EvidenceVerifier(), Agent6RecoveryController()
    host = urlsplit(contract.design.target_url).hostname
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == 'localhost'

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError('Total dynamic execution budget exceeded')
        return value

    step_deadline = deadline

    def step_remaining():
        value = min(remaining(), step_deadline - time.monotonic())
        if value <= 0:
            raise TimeoutError('Single step execution budget exceeded')
        return value

    def observe():
        nonlocal observations
        if observations >= policy.max_observations:
            raise ExecutionRejected('Maximum observations exceeded')
        observation = session.observe(timeout=step_remaining())
        observations += 1
        _write_json(run_dir / f'observation_{observations:03d}.json', observation)
        return observation

    def bounded_generate(step_id, observation, label):
        # Provider threads never receive session/driver. Late responses cannot run.
        queue = Queue()
        attempt_dir = run_dir / 'generation' / (step_id + '_' + label)
        attempt_dir.mkdir(parents=True, exist_ok=False)
        def generate():
            try:
                artifact = generator.generate_dynamic(contract, step_id, observation,
                    attempt_dir, state=dict(state), policy=policy, mode=mode, llm=llm)
                artifact['source_file'] = (attempt_dir / artifact['source_file']).relative_to(run_dir).as_posix()
                queue.put((True, artifact))
            except Exception as exc:
                queue.put((False, exc))
        Thread(target=generate, daemon=True).start()
        try:
            ok, value = queue.get(timeout=step_remaining())
        except Empty as exc:
            raise TimeoutError('Agent3 generation budget exceeded') from exc
        if not ok:
            raise value
        contract.assert_integrity()
        source_path = (run_dir / value['source_file']).resolve()
        if not source_path.is_relative_to(run_dir.resolve()) or source_path.read_text(encoding='utf-8') != value['source']:
            raise ExecutionRejected('Saved generated source was modified')
        return value

    def check_oracles(status='RUNNING'):
        return verifier.verify_dynamic(contract, evidence, execution_status=status, run_dir=run_dir)

    try:
        policy.authorize(contract.design.target_url, contract.design.target_url)
        if len(states) > policy.max_steps:
            raise ExecutionRejected('Contract exceeds maximum steps')
        if any(s.action not in policy.allowed_actions for s in contract.design.steps):
            raise ExecutionRejected('Contract includes actions disabled by execution policy', category='permission')
        session = executor.open_dynamic(contract, run_dir, policy=policy, headless=headless)
        for step in contract.design.ordered_steps():
            current_step = step.step_id
            step_deadline = min(deadline, time.monotonic() + policy.step_timeout)
            observation = observe()
            attempt = 1
            while True:
                states[step.step_id] = 'GENERATING'
                artifact = bounded_generate(step.step_id, observation, f'attempt{attempt}')
                states[step.step_id] = 'RUNNING'
                outcome = session.execute(artifact, timeout=step_remaining())
                execution_attempts.append({**outcome, 'source_file': artifact['source_file']})
                _write_json(run_dir / 'execution_attempts.json', execution_attempts)
                if outcome['step_id'] != step.step_id or outcome['status'] not in ('COMPLETE', 'ERROR'):
                    raise ExecutionRejected('Invalid executor step result')
                if outcome['status'] == 'COMPLETE':
                    if attempt > 1:
                        recovery_history[-1]['result_status'] = 'COMPLETE'
                    break
                failure = outcome.get('error') or {'step_id': step.step_id, 'category': 'environment', 'action_started': True}
                if attempt > 1:
                    recovery_history[-1]['result_status'] = 'ERROR'
                fresh = observe() if repairs < policy.max_repairs and (local or policy.allow_live_recovery) else None
                decision = recovery.plan_dynamic_repair(contract, failure, fresh,
                    previous_repairs=repairs, max_repairs=policy.max_repairs,
                    live_recovery_allowed=policy.allow_live_recovery, verdict=check_oracles())
                decision['result_status'] = 'SCHEDULED' if decision['allowed'] else 'DENIED'
                recovery_history.append(decision)
                _write_json(run_dir / 'dynamic_recovery.json', recovery_history)
                if not decision['allowed']:
                    error = failure
                    raise ExecutionRejected('Execution failed; recovery denied or exhausted')
                # Pipeline rechecks permission, strategy and immutable binding.
                if (decision['contract_digest'] != contract.design_digest or decision['step_id'] != step.step_id or
                        decision['strategy'] != 'regenerate_from_fresh_dom' or repairs >= policy.max_repairs or
                        not local and not policy.allow_live_recovery):
                    raise ExecutionRejected('Unauthorized recovery decision')
                if (failure.get('category') not in ('locator', 'interaction', 'wait') or
                        type(failure.get('action_started')) is not bool or
                        failure['action_started'] and step.action not in ('observe', 'wait')):
                    raise ExecutionRejected('Recovery would replay unsafe action')
                repairs += 1
                attempt += 1
                observation = fresh
            outcome['source_file'] = artifact['source_file']
            results.append(outcome)
            state[step.step_id] = outcome['outputs']
            merge_dynamic(contract, evidence, outcome['evidence'], run_dir=run_dir)
            states[step.step_id] = 'COMPLETE'
            _write_json(run_dir / 'dynamic_steps.json', results)
            checkpoint = check_oracles()
            if checkpoint.status == 'FAIL':
                terminal_failure = True
                break
            # Only read-only original checkpoints may be supplemented. No replay
            # of click, submit, navigation, download or historic page transitions.
            while len(probes) < policy.max_probes:
                request = verifier.plan_dynamic_probe(contract, evidence, checkpoint, step_id=step.step_id)
                if request is None:
                    break
                entry = {'request': request, 'status': 'DENIED'}
                probes.append(entry)
                if step.action not in ('observe', 'wait') or not local and not policy.allow_live_probes:
                    entry['reason'] = 'Read-only checkpoint/live permission required'
                    break
                validate_probe(contract, request, current_step_id=step.step_id)
                try:
                    probe_observation = observe()
                    probe_artifact = bounded_generate(step.step_id, probe_observation, f'probe{len(probes)}')
                    probe_result = session.probe(request, probe_artifact, timeout=step_remaining())
                    entry.update(status=probe_result['status'], result=probe_result, source_file=probe_artifact['source_file'])
                    if probe_result['status'] == 'COMPLETE':
                        entry['added'] = merge_dynamic(contract, evidence, probe_result['evidence'],
                            allowed_ids=set(request['evidence_ids']), run_dir=run_dir)
                        for key, value in probe_result.get('outputs', {}).items():
                            if state[step.step_id].get(key) is None:
                                state[step.step_id][key] = value
                except Exception as exc:
                    entry.update(status='ERROR', error_type=type(exc).__name__)
                checkpoint = check_oracles()
                if checkpoint.status == 'FAIL':
                    terminal_failure = True
                    break
                if session.closed:
                    raise ExecutionRejected('Probe session closed before execution finished')
            _write_json(run_dir / 'dynamic_probes.json', probes)
            if terminal_failure:
                break
        step_deadline = min(deadline, time.monotonic() + policy.step_timeout)
        if not terminal_failure:
            observe()
    except Exception as exc:
        error = error or {'type': type(exc).__name__, 'step_id': current_step,
            'category': getattr(exc, 'category', 'invalid_code' if isinstance(exc, ValueError) else 'environment'),
            'action_started': None, 'message': str(exc)[:350]
            if isinstance(exc, (ExecutionRejected, TimeoutError)) else 'Dynamic execution failed'}
        if current_step and states[current_step] != 'COMPLETE':
            states[current_step] = 'ERROR'
    finally:
        for sid, status in states.items():
            if status == 'PENDING':
                states[sid] = 'SKIPPED'
        if session:
            try:
                session.close()
            except Exception:
                error = error or {'type': 'CleanupError', 'category': 'environment', 'message': 'Browser cleanup failed'}
    verdict = verifier.verify_dynamic(contract, evidence, execution_status='ERROR' if error else 'COMPLETE',
                                      error=error, run_dir=run_dir).to_dict()
    result = {'execution_status': 'ERROR' if error else 'COMPLETE', 'verdict': verdict,
              'step_states': states, 'steps': results, 'outputs': state, 'evidence': evidence,
              'observations': observations, 'session': session.info if session else None,
              'session_closed': session.closed if session else True,
              'contract_digest': contract.design_digest, 'error': error, 'policy': policy.to_dict(),
              'probe_count': len(probes), 'repair_count': repairs, 'probes': probes,
              'recovery_history': recovery_history, 'stop_reason': 'oracle_failure' if terminal_failure else None}
    _write_json(run_dir / 'dynamic_execution.json', result)
    _write_json(run_dir / 'dynamic_evidence.json', evidence)
    _write_json(run_dir / 'dynamic_recovery.json', recovery_history)
    _write_json(run_dir / 'dynamic_probes.json', probes)
    _write_json(run_dir / 'final_verdict.json', verdict)
    return result


def _plan_dynamic(*, requirement: str, target_url: str, mode: str,
                  output_root: Path | None, llm: JSONModel | None,
                  provider: str | None, model: str | None,
                  template_design: TestDesign | dict | None) -> dict:
    """Stage 1 boundary: A1 -> A2, at most two reviews; never call A3-A6.

    Approval means PLANNED, not execution success. All results remain NOT_RUN
    with an INCONCLUSIVE functional verdict; the caller may explicitly opt into
    the separate stage-2 execution loop after this function returns.
    """
    from .dynamic_schema import validate_target_url
    validate_target_url(target_url)
    if mode == 'llm' and llm is None:
        from .llm_adapter import StructuredLLM
        llm = StructuredLLM(provider=provider, model=model)
    output_root = Path(output_root or ROOT / 'artifacts')
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = output_root / (datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6])
    run_dir.mkdir()
    designer, reviewer = Agent1TestDesigner(), Agent2TestReviewer()
    reviews, stages = [], []
    previous, contract, feedback = None, None, []
    try:
        for revision in (1, 2):
            design = designer.design(requirement, engine='dynamic', target_url=target_url,
                mode=mode, llm=llm, feedback=feedback, previous_design=previous,
                template_design=template_design)
            _write_json(run_dir / f'test_design_revision{revision}.json', design.to_dict())
            stages.append({'agent': 'Agent1', 'result': 'DESIGNED', 'revision': revision})
            contract, decision = reviewer.review(design, engine='dynamic', target=target_url,
                original_requirement=requirement, mode=mode, llm=llm, revision=revision)
            decision['attempt'] = revision
            reviews.append(decision)
            stages.append({'agent': 'Agent2', 'result': 'APPROVED' if contract else 'REJECTED',
                           'revision': revision})
            if contract is not None:
                break
            previous, feedback = design, decision['issues']
            # An authored template cannot revise itself. Keep the rejection
            # visible to its author rather than reviewing the same input twice.
            if mode == 'template':
                break
        _write_json(run_dir / 'test_design.json', design.to_dict())
        _write_json(run_dir / 'review_decision.json', {'approved': contract is not None, 'history': reviews})
        if contract:
            _write_json(run_dir / 'test_contract.json', contract.to_dict())
        verdict = Verdict(status='INCONCLUSIVE', message='动态规划已完成，尚未执行浏览器测试' if contract
                          else '动态设计未通过审核，尚未执行浏览器测试').to_dict()
        result = {'engine': 'dynamic', 'mode': mode, 'planning_status': 'PLANNED' if contract else 'REVIEW_REJECTED',
                  'execution_status': 'NOT_RUN', 'run_dir': str(run_dir), 'design': design.to_dict(),
                  'contract': contract.to_dict() if contract else None, 'reviews': reviews,
                  'verdict': verdict, 'stages': stages, 'probe_count': 0, 'repair_count': 0}
        _write_json(run_dir / 'final_verdict.json', verdict)
        _write_json(run_dir / 'planning_result.json', result)
        return result
    except Exception as exc:
        (run_dir / 'pipeline_error.txt').write_text(f'{type(exc).__name__}: {exc}', encoding='utf-8')
        raise
