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
import uuid

from .agents import (Agent1TestDesigner, Agent2TestReviewer,
                     Agent4TestExecutor, Agent5EvidenceVerifier, Agent6RecoveryController)
from .agents.verifier import missing_oracle_evidence as _missing_oracle_evidence
from .dom_inspector import compact_dom, inspect_page
from .fixture_server import local_fixture, search_form
from .generator import Agent3SeleniumGenerator
from .probe import merge_probe
from .report import html_report
from .schema import JSONModel

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
                 start_url: str | None = None, allow_local_research: bool = False,
                 step_attempts: int = 3, task_repairs: int = 5) -> dict:
    if target == 'dynamic':
        if not allow_live_site or not allow_local_research or mode != 'llm':
            raise ValueError('Dynamic demo requires --mode llm --allow-live-site --allow-local-research')
        return _run_dynamic(requirement=requirement, url=start_url, headless=headless,
                            output_root=output_root, llm=llm, provider=provider, model=model,
                            step_attempts=step_attempts, task_repairs=task_repairs)
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


def _run_dynamic(*, requirement, url, headless, output_root, llm, provider, model,
                 step_attempts, task_repairs):
    """Dynamic branch in the existing Pipeline; six Agents retain their roles."""
    import hashlib
    import time
    from .checked_selenium import origin
    from .selenium_runtime import create_session, RuntimeLimits, ARTIFACT_ROOT
    from .agents.generator import Agent3SeleniumGenerator
    from .schema import validate_dynamic_plan
    if not url or not requirement.strip():
        raise ValueError('Explicit URL and natural language requirement are required')
    origin(url)
    limits = RuntimeLimits(execution_seconds=30, startup_seconds=60, session_seconds=1200,
                           step_attempts=step_attempts, task_repairs=task_repairs)
    root = Path(output_root or ARTIFACT_ROOT).resolve()
    if not root.is_relative_to(ARTIFACT_ROOT.resolve()):
        raise ValueError('Outputs must stay inside artifacts/')
    run_dir = root / ('dynamic_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6])
    run_dir.mkdir(parents=True)
    sessions, events, reviews = [], [], []
    repairs, calls_start = 0, 0
    attempts = {}
    stage, contract = 'Agent1', {}
    started = time.monotonic()

    def write(name, value):
        _write_json(run_dir / name, value)

    def event(agent, action, **data):
        events.append(dict(agent=agent, action=action, elapsed_seconds=round(time.monotonic()-started,3), **data))
        write('events.json', events)
        print(agent + ': ' + action, flush=True)

    def session(purpose):
        s = create_session(run_dir, purpose=purpose, start_url=url, research_mode=True,
                           limits=limits, headless=headless)
        sessions.append(s)
        return s

    def observe(s):
        view = dict(s.observe(max_elements=80, max_html=16000))
        ref = view.get('html_ref')
        if ref:
            view['html_summary'] = (s.run_dir / ref).read_text(encoding='utf-8')[:16000]
        return view

    def fail(stage_name, failure):
        diagnosis = Agent6RecoveryController().diagnose_dynamic(stage=stage_name, result=failure, repairs=repairs)
        write('agent6_diagnosis.json', diagnosis)
        event('Agent6', 'diagnosed_stop', error_type=failure.get('error_type'))
        verdict = dict(status='ERROR', message='Technical run incomplete; no business PASS/FAIL',
                       error_type=failure.get('error_type'), failed_stage=stage_name, checks=[])
        write('final_verdict.json', verdict)
        return dict(verdict=verdict, run_dir=str(run_dir), report=str(run_dir/'report.md'))

    try:
        if llm is None:
            from .llm_adapter import StructuredLLM
            llm = StructuredLLM(provider=provider or 'deepseek', model=model)
        calls_start = len(getattr(llm,'calls',[]))
        feedback = []
        for revision in range(2):
            draft = Agent1TestDesigner().design_dynamic(requirement, url=url, llm=llm, feedback=feedback)
            write('agent1_draft_' + str(revision+1) + '.json', draft)
            event('Agent1','semantic_plan_created', steps=len(draft['steps']))
            stage = 'Agent2'
            decision = Agent2TestReviewer().review_dynamic(draft, llm=llm)
            reviews.append(decision)
            write('agent2_reviews.json',reviews)
            if decision['approved'] and not draft.get('missing_inputs'):
                contract = dict(draft, revision=revision+1, requirement_id=uuid.uuid4().hex)
                validate_dynamic_plan(contract)
                break
            feedback = decision.get('issues',[]) + draft.get('missing_inputs',[])
        if not contract:
            return fail('Agent2', {'error_type':'ReviewRejected'})
        contract_hash = hashlib.sha256(json.dumps(contract,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        write('test_contract.json',contract)
        event('Agent2','contract_frozen', contract_sha256=contract_hash)
        stage = 'Agent3'
        generator = Agent3SeleniumGenerator()
        trial = session('trial')
        observation = observe(trial)
        implementation, bindings, debug = [], {}, []
        cold_error = None
        while True:
            for index in range(len(implementation), len(contract['steps'])):
                step = contract['steps'][index]
                previous, error = (cold_error or {}).get('previous_proposal'), cold_error
                cold_error = None
                while True:
                    attempts[step['id']] = attempts.get(step['id'],0)+1
                    if attempts[step['id']] > step_attempts:
                        return fail('Agent3',error or dict(error_type='StepBudgetExhausted',step_id=step['id']))
                    name = 'agent3_' + step['id'] + '_' + uuid.uuid4().hex[:8]
                    event('Agent3','generate_step',step_id=step['id'],attempt=attempts[step['id']])
                    try:
                        proposal = generator.develop_step(contract,step,observation,llm=llm,
                                                          previous=previous,error=error,proposal_path=run_dir/(name+'.json'))
                        source = proposal['code']
                        previous = proposal
                        (run_dir/(name+'.py')).write_text(source,encoding='utf-8')
                        write(name+'.json',proposal)
                        result = trial.execute_checked(source,step_id=step['id'],queries=proposal['bindings'])
                    except (ValueError,SyntaxError) as exc:
                        # develop_step saves the raw proposal before validating it. Keep
                        # rejected code AND bindings available to the next repair call.
                        proposal_path = run_dir/(name+'.json')
                        if proposal_path.exists():
                            previous = json.loads(proposal_path.read_text(encoding='utf-8'))
                        result = dict(status='REJECTED',error_type=type(exc).__name__,error=str(exc)[:1200],
                                      step_id=step['id'])
                    debug.append(dict(step_id=step['id'],attempt=attempts[step['id']],proposal_ref=name,
                                      result=result))
                    write('agent3_debug.json',debug)
                    if result['status']=='COMPLETE':
                        implementation.append(dict(id=step['id'],source=source))
                        bindings.update(proposal['bindings'])
                        observation = observe(trial)
                        event('Agent3','step_complete_observed',step_id=step['id'],
                              observation_ref=result.get('observation_ref'))
                        break
                    error = result
                    if attempts[step['id']] >= step_attempts or repairs >= task_repairs or result['status']=='TIMEOUT':
                        return fail('Agent3',result)
                    repairs += 1
                    observation = observe(trial)
                    event('Agent3','repair_from_actual_error',step_id=step['id'],repairs=repairs,
                          error_type=result.get('error_type'))
            path, digest = generator.freeze_dynamic(implementation,run_dir=run_dir)
            review = Agent2TestReviewer().review_implementation(contract,implementation,bindings,
                                                                observation=observation,llm=llm)
            write('implementation_review_'+uuid.uuid4().hex[:6]+'.json',review)
            if not review['approved']:
                return fail('Agent2Implementation',dict(error_type='ImplementationReviewRejected'))
            trial.close_session()
            cold = session('trial')
            source = path.read_text(encoding='utf-8')
            replay = cold.execute_program(source,step_ids=[s['id'] for s in contract['steps']],bindings=bindings)
            write('agent3_cold_trial_'+uuid.uuid4().hex[:6]+'.json',replay)
            event('Agent3','cold_trial_finished',status=replay['execution_status'],script_sha256=digest)
            if replay['execution_status']=='COMPLETE':
                cold.close_session()
                break
            failed = replay['steps'][-1]
            if repairs >= task_repairs or failed['status']=='TIMEOUT':
                return fail('Agent3ColdTrial',failed)
            failed_index = len(replay['steps'])-1
            repairs += 1
            failed = dict(failed, previous_proposal=dict(code=implementation[failed_index]['source'],
                          bindings={key:v for key,v in bindings.items()
                                    if v['step_id']==implementation[failed_index]['id']}))
            implementation = implementation[:failed_index]
            bindings = {key:v for key,v in bindings.items() if v['step_id'] in [s['id'] for s in implementation]}
            trial, cold_error = cold, failed
            observation = observe(trial)
            event('Agent3','cold_trial_repair',step_id=contract['steps'][failed_index]['id'],
                  repairs=repairs,error_type=failed.get('error_type'))
        delivery = dict(script_path=str(path),script_sha256=digest,contract_sha256=contract_hash,
                        cold_trial_complete=True,step_ids=[s['id'] for s in contract['steps']],
                        bindings=bindings,repairs_used=repairs,attempts=attempts)
        write('agent3_delivery.json',delivery)
        stage = 'Agent4'
        formal = session('formal')
        evidence = Agent4TestExecutor().execute_dynamic(delivery,session=formal)
        evidence.update(contract_sha256=contract_hash,formal_run_dir=str(formal.run_dir))
        write('formal_evidence.json',evidence)
        cleanup = formal.close_session()
        if evidence['execution_status']!='COMPLETE' or not cleanup['process_tree_terminated']:
            return fail('Agent4',evidence.get('steps',[{}])[-1])
        event('Agent4','same_source_replayed_independently',script_sha256=digest,
              browser_session_id=evidence['browser_session_id'])
        stage='Agent5'
        verdict = Agent5EvidenceVerifier().verify_dynamic(contract,evidence).to_dict()
        write('final_verdict.json',verdict)
        event('Agent5','business_verdict',status=verdict['status'])
        return dict(verdict=verdict,run_dir=str(run_dir),report=str(run_dir/'report.md'))
    except Exception as exc:
        # Do not serialize SDK error bodies, request headers, environment or credentials.
        write('pipeline_error.json',dict(stage=stage,error_type=type(exc).__name__,
                                         status_code=getattr(exc,'status_code',None)))
        return fail(stage,{'error_type':type(exc).__name__})
    finally:
        cleanups = [s.close_session() for s in sessions]
        if cleanups and not all(c.get('process_tree_terminated') and c.get('profile_removed') for c in cleanups):
            final_path=run_dir/'final_verdict.json'
            if final_path.exists():
                failed_final=json.loads(final_path.read_text(encoding='utf-8'))
                failed_final.update(status='ERROR',message='Owned browser cleanup was not confirmed')
                write('final_verdict.json',failed_final)
        write('cleanup.json',cleanups)
        write('llm_calls.json',getattr(llm,'calls',[])[calls_start:] if llm else [])
        final_path=run_dir/'final_verdict.json'
        final=json.loads(final_path.read_text(encoding='utf-8')) if final_path.exists() else {'status':'ERROR'}
        (run_dir/'report.md').write_text(
            '# Dynamic Web GUI demo\n\nStatus: '+final['status']+'\n\n'
            + 'Requirement: '+requirement+'\n\n'
            + 'See test_contract.json, generated_test.py, agent3_debug.json, formal_evidence.json, '
            + 'final_verdict.json and cleanup.json.\n\n'
            + 'Local capability-limited execution; not an OS sandbox.\n',encoding='utf-8')
