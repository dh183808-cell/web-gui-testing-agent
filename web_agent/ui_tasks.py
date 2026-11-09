"""UI task lifetime and presentation data; execution stays in the existing Pipeline."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import threading
import time
import uuid

from .pipeline import ROOT, run_pipeline


FORM_URL = 'https://www.selenium.dev/selenium/web/web-form.html'
DEMO_PRESETS = {
    'A · 提交反馈（预期 PASS）': dict(url=FORM_URL,
        test_basis='在 Web form 页面填写 Text input 并点击 Submit 后，结果页面应显示 Received!。',
        requirement='在 Text input 输入 WebAgent Test，点击 Submit，验证提交后的页面反馈符合测试依据。'),
    'B · 错误反馈规格（预期 FAIL）': dict(url=FORM_URL,
        test_basis='在 Web form 页面填写 Text input 并点击 Submit 后，结果页面应显示 Accepted!。',
        requirement='在 Text input 输入 WebAgent Test，点击 Submit，验证提交后的页面反馈符合测试依据。'),
    'C · 缺失 Reset（预期 FAIL）': dict(url=FORM_URL,
        test_basis='Web form 页面应提供一个可见的 Reset 按钮，用户填写 Text input 后，点击 Reset 应清空输入内容。',
        requirement='在 Text input 中输入 WebAgent Test，检查页面是否提供 Reset 按钮；如果存在则点击，并验证输入框被清空。如果按钮不存在，应依据测试说明判断该功能是否符合预期。'),
}
STAGES = ('Agent1', 'Agent2', 'Agent3', 'Cold', 'Agent4', 'Formal', 'Agent5', 'Agent6')
STAGE_LABELS = dict(Agent1='Agent1 · 测试设计', Agent2='Agent2 · 契约审核',
    Agent3='Agent3 · 编码调试', Cold='Cold Trial · 冷启动', Agent4='Agent4 · 独立执行',
    Formal='Formal · 正式取证', Agent5='Agent5 · 证据判定', Agent6='Agent6 · 异常诊断')


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def artifact_path(run_dir, reference):
    """Only show/download files within this run, also resolving symlinks."""
    root = Path(run_dir).resolve()
    if not root.is_relative_to((ROOT / 'artifacts').resolve()):
        return None
    path = (root / reference).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


def activity_text(event):
    """Translate facts only; unrecognised events remain available in raw logs."""
    agent, action = event['agent'], event['action']
    sid = event.get('step_id', '')
    if action == 'call_started': return '正在等待 DeepSeek 返回' if event.get('provider') == 'deepseek' else '正在等待模型返回'
    if action == 'call_finished': return '模型请求异常：' + event.get('error_type', '未知异常') if event.get('status') == 'ERROR' else '模型已返回，正在检查输出'
    if action == 'generate_step': return f"Agent3 正在为 {sid} 生成代码（第 {event.get('attempt', 1)} 次尝试）"
    if action == 'step_started': return f"开始执行 {sid} · " + ('正式浏览器' if event.get('purpose') == 'formal' else '后台浏览器')
    if action == 'step_finished': return f"{sid} 技术执行" + ('完成' if event.get('status') == 'COMPLETE' else '异常：' + event.get('status', '未知'))
    if action == 'step_complete_observed': return f'{sid} 已完成操作并重新观察页面'
    if action == 'repair_from_actual_error': return f"{sid} 根据实际错误修复代码 · {event.get('error_type', '')}"
    if action == 'step_entry_restored': return f'{sid} 已重放前置步骤，恢复入口状态'
    if action == 'cold_trial_repair': return f'{sid} 冷启动未通过，进入有限修复'
    if action == 'session_starting': return '正在启动独立的正式 Chrome' if event.get('purpose') == 'formal' else '正在启动后台 Chrome 会话'
    if action == 'session_created': return '正式浏览器已启动，正在独立重放冻结代码' if event.get('purpose') == 'formal' else '后台浏览器已就绪'
    if action == 'session_closed': return '浏览器会话已关闭并返回清理记录'
    if action == 'started': return STAGE_LABELS.get(agent, agent) + '开始'
    if action == 'semantic_plan_created': return f"测试规划已生成 · {event.get('steps', 0)} 个步骤"
    if action == 'contract_frozen': return '测试契约与 Oracle 已审核冻结'
    if action in {'review_finished', 'blockage_reviewed'}: return 'Agent2 审核通过' if event.get('approved') else 'Agent2 审核未通过，返回具体意见'
    if action == 'review_feedback': return 'Agent2 返回审核意见，按现有预算修订'
    if action == 'evidence_bindings_repaired': return f'{sid} 取证绑定已修复，冻结代码未改变'
    if action == 'cold_trial_finished': return '完整冷启动' + ('通过' if event.get('status') == 'COMPLETE' else '未通过')
    if action == 'same_source_replayed_independently': return 'Formal 已完成同版本代码独立重放'
    if action == 'business_verdict': return 'Agent5 已给出正式结论：' + event['status']
    if action == 'diagnosed_stop': return 'Agent6 已诊断并停止：' + (event.get('error_type') or '技术执行异常')
    if action == 'input_not_ready': return '测试依据或预期尚不明确，停止后续执行'
    if action == 'invalid_plan': return '测试规划未通过校验，返回有限修订'
    if action == 'run_started': return '任务已启动，开始理解测试输入'
    if action == 'run_finished': return '任务已结束 · ' + event['status']
    return None


def progress_view(job, *, now=None):
    """Project existing events into UI states; no execution decisions live here.

    Stage times sum the intervals assigned to that phase by actual events.
    Agent4 and Formal deliberately describe the same formal execution interval.
    """
    elapsed = job.get('duration_seconds')
    if elapsed is None: elapsed = max(0, (time.time() if now is None else now)-job['started_at'])
    states = {name: dict(status='等待中', seconds=0.0, called=False) for name in STAGES}
    current, step, calls, completed, errors, repairs = '尚未开始', '—', 0, 0, 0, 0
    active, last, pending, formal_session, activities = [], 0.0, None, False, []

    def tick(t):
        nonlocal last
        for name in active: states[name]['seconds'] += max(0, t-last)
        last = t

    def focus(name):
        nonlocal active, current, step
        if name != current: step = '—'
        for old in active:
            if states[old]['status'] == '执行中': states[old]['status'] = '等待中'
        current = name
        active = ['Agent4', 'Formal'] if name in {'Agent4', 'Formal'} else [name]
        for item in active: states[item].update(status='执行中', called=True)

    def finish(name, ok=True):
        nonlocal active
        states[name].update(status='已完成' if ok else '异常', called=True)
        active = [item for item in active if item != name]

    for event in job.get('events', []):
        agent, action = event['agent'], event['action']
        t = event.get('elapsed_seconds', 0)
        tick(t)
        repairs = max(repairs, event.get('repairs', 0))
        if action == 'call_started':
            calls += 1
            stage = event.get('stage', '')
            name = next((name for name in STAGES if stage.startswith(name)), None)
            if name and name != current: focus(name)
            pending = t
        elif action == 'call_finished':
            completed += 1
            errors += int(event.get('status') == 'ERROR')
            pending = None
            if event.get('status') == 'ERROR':
                for name in active: states[name]['status'] = '异常'
        elif action == 'started' and agent in states:
            focus(agent)
        elif agent == 'Agent3' and action in {'generate_step', 'repair_from_actual_error', 'cold_trial_repair',
                                            'evidence_bindings_repaired', 'step_complete_observed', 'step_entry_restored'}:
            focus('Agent3')
        elif agent == 'Runtime' and action in {'session_starting', 'step_started'}:
            phase = event.get('phase', event.get('reason'))
            name = 'Formal' if event.get('purpose') == 'formal' else 'Cold' if phase == 'mandatory_cold_trial' else 'Agent3'
            if name != current: focus(name)
        if event.get('step_id'): step = event['step_id']
        if action == 'session_created' and event.get('purpose') == 'formal': formal_session = True
        if action == 'session_closed' and formal_session and event.get('reason') == 'formal_replay_finished': formal_session = False
        if action in {'semantic_plan_created', 'contract_frozen', 'review_finished', 'blockage_reviewed', 'same_source_replayed_independently', 'business_verdict', 'diagnosed_stop'} and agent in states:
            finish(agent, event.get('approved') is not False)
        if action == 'cold_trial_finished':
            finish('Agent3', event.get('status') == 'COMPLETE')
        if agent in {'Cold', 'Formal'} and action == 'finished': finish(agent, event.get('status') == 'COMPLETE')
        if action == 'step_finished' and event.get('status') != 'COMPLETE':
            for name in active: states[name]['status'] = '异常'
        if action == 'diagnosed_stop':
            failed = event.get('stage', '')
            if failed.startswith('Agent4'):
                finish('Agent4', False)
                if states['Formal']['called']: finish('Formal', False)
            elif failed.startswith('Agent3Cold') and states['Cold']['called']: finish('Cold', False)
            elif failed.startswith('Agent3'): finish('Agent3', False)
            elif failed.startswith('Agent2'): finish('Agent2', False)
            elif failed.startswith('Agent1'): finish('Agent1', False)
        description = activity_text(event)
        if description: activities.append(dict(seconds=t, text=description))
    tick(max(last, elapsed))
    if job['status'] != 'RUNNING':
        for state in states.values():
            if not state['called']: state['status'] = '未调用'
            elif state['status'] in {'执行中', '等待中'}: state['status'] = '已停止'
        current, pending, formal_session = '结束', None, False
    for state in states.values(): state['seconds'] = round(state['seconds'], 1) if state['called'] else None
    return dict(stages=states, current=current, step=step, elapsed=elapsed, calls=calls,
        calls_finished=completed, calls_failed=errors, repairs=repairs,
        pending_seconds=round(max(0, elapsed-pending), 1) if pending is not None else None,
        formal_open=formal_session, activities=activities,
        current_seconds=states.get(current, {}).get('seconds'))


def result_view(job):
    """Join frozen Oracles and Agent5 decisions with ONLY Formal actual values."""
    result = job.get('result') or {}
    run = Path(result.get('run_dir') or job.get('run_dir') or ROOT/'artifacts'/'missing')
    def load(name):
        path = artifact_path(run, name)
        return read_json(path, {}) if path else {}
    verdict = result.get('verdict') or dict(status='ERROR', checks=[], message=job.get('error_type', '任务中断'))
    contract, formal = load('test_contract.json'), load('formal_evidence.json')
    formal_dir = Path(formal.get('formal_run_dir') or run/'formal'/'missing').resolve()
    formal_valid = formal.get('purpose') == 'formal' and formal_dir.is_relative_to((run/'formal').resolve())
    def formal_path(ref):
        path = artifact_path(run, str(formal_dir/ref)) if ref and formal_valid else None
        return path if path and path.is_relative_to(formal_dir) else None
    measures = formal.get('measurements', {}) if formal_valid else {}
    decisions = {item['oracle_id']:item for item in verdict.get('checks', [])}
    oracles = contract.get('oracles') or [dict(item, id=item['oracle_id']) for item in verdict.get('checks', [])]
    rows = []
    for oracle in oracles:
        oid = oracle['id']; decision = decisions.get(oid, {}); measure = measures.get(oid, {})
        skipped = decision.get('evidence_status') == 'not_executed'
        status = 'NOT_EXECUTED' if skipped else 'PASS' if decision.get('passed') is True else 'FAIL' if decision.get('passed') is False else 'INCONCLUSIVE'
        ref = measure.get('screenshot_ref')
        screenshot = formal_path(ref) if not skipped else None
        rows.append(dict(oracle_id=oid, step_id=oracle.get('step_id', measure.get('step_id', '')),
            meaning=oracle.get('meaning', decision.get('meaning', '')), expected=oracle.get('expected'),
            actual=measure.get('value') if not skipped else None, actual_available=bool(measure) and not skipped,
            source_type=oracle.get('source_type', decision.get('source_type', '')),
            source_quote=oracle.get('source_quote', decision.get('source_quote', '')),
            status=status, screenshot=screenshot, observation_ref=measure.get('observation_ref'),
            evidence_status=measure.get('status'),
            reason=decision.get('not_executed_reason', ''), blocked_by=decision.get('blocked_by')))
    failures = [r for r in rows if r['status'] == 'FAIL']
    successes = [r for r in rows if r['status'] == 'PASS' and r['actual_available']]
    # Prefer the final value check over prerequisite presence checks for a PASS.
    focus = failures[0] if failures else successes[-1] if successes else rows[0] if rows else None
    steps = []
    if formal_valid:
        for step in formal.get('steps', []):
            ref = step.get('screenshot_ref')
            path = formal_path(ref)
            steps.append(dict(step_id=step['step_id'], status=step['status'], screenshot=path,
                              current_url=step.get('current_url', '')))
    return dict(run_dir=run, verdict=verdict, rows=rows, focus=focus, steps=steps,
                skipped=verdict.get('skipped_steps', []), formal_valid=formal_valid)


class TaskStore:
    """One cache_resource store per server; disk records never auto-resume runs."""
    def __init__(self, root=None, *, runner=run_pipeline):
        self.root = Path(root or ROOT / 'artifacts' / 'ui_jobs')
        self.root.mkdir(parents=True, exist_ok=True)
        self.runner = runner
        self.lock = threading.RLock()
        self.jobs = {}
        for path in self.root.glob('*.json'):
            job = read_json(path)
            if not isinstance(job, dict) or 'id' not in job: continue
            if job.get('status') == 'RUNNING':
                job.update(status='INTERRUPTED', error_type='ServerRestarted',
                           duration_seconds=max(0, time.time()-job['started_at']))
                self._save(job)
            self.jobs[job['id']] = job

    def _save(self, job):
        path = self.root / (job['id'] + '.json')
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)

    def active(self):
        with self.lock:
            return next((key for key, value in self.jobs.items() if value['status'] == 'RUNNING'), None)

    def snapshot(self, job_id):
        with self.lock: return deepcopy(self.jobs.get(job_id))

    def recent(self, limit=20):
        """Listing/viewing history never starts work or rewrites completed jobs."""
        with self.lock:
            recent = sorted(self.jobs.values(), key=lambda job: job['started_at'], reverse=True)[:limit]
            return [dict(id=job['id'], started_at=job['started_at'], status=job['status'],
                verdict=(job.get('result') or {}).get('verdict', {}).get('status', job['status']),
                label=datetime.fromtimestamp(job['started_at'], timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
                      + ' · ' + (job.get('result') or {}).get('verdict', {}).get('status', job['status'])
                      + ' · ' + job['inputs']['requirement'][:18] + ' · ' + job['id'][:8]) for job in recent]

    def start(self, inputs, *, request_id):
        from .checked_selenium import origin
        uuid.UUID(hex=request_id)
        origin(inputs['url'])
        if not inputs['requirement'].strip(): raise ValueError('请填写 Test Requirement')
        inputs = {key: inputs.get(key, '') for key in ('url', 'test_basis', 'requirement')}
        with self.lock:
            if request_id in self.jobs: return request_id
            active = self.active()
            if active: return active
            job = dict(id=request_id, inputs=inputs, status='RUNNING', started_at=time.time(),
                       started_utc=datetime.now(timezone.utc).isoformat(), events=[], result=None)
            self.jobs[request_id] = job
            self._save(job)
            thread = threading.Thread(target=self._run, args=(request_id,), daemon=True,
                                      name='webagent-ui-' + request_id[:8])
            try:
                thread.start()
            except Exception:
                job.update(status='ERROR', error_type='TaskStartFailed', duration_seconds=0)
                self._save(job)
                raise
            return request_id

    def _run(self, job_id):
        def progress(record):
            with self.lock:
                job['events'].append(record)
                if record['action'] == 'run_started': job['run_dir'] = record['run_dir']
                self._save(job)
        job = self.jobs[job_id]
        try:
            result = self.runner(target='dynamic', mode='llm', provider='deepseek',
                start_url=job['inputs']['url'], requirement=job['inputs']['requirement'],
                test_basis=job['inputs']['test_basis'], allow_live_site=True, allow_local_research=True,
                headless=True, formal_headless=False, progress_callback=progress)
            # Cleanup may update the verdict in finally; disk is authoritative.
            verdict = read_json(Path(result['run_dir'])/'final_verdict.json', result['verdict'])
            with self.lock:
                job.update(result=dict(result, verdict=verdict), run_dir=result['run_dir'], status='FINISHED')
        except Exception as exc:
            # SDK exceptions can include request bodies; retain only their class.
            with self.lock: job.update(status='ERROR', error_type=type(exc).__name__)
        finally:
            with self.lock:
                job['duration_seconds'] = round(time.time()-job['started_at'], 3)
                self._save(job)
