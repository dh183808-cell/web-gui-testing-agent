"""Agent4: isolated script/probe execution and evidence/log persistence.

The generated script calls shared selenium_runtime.execute_test; the probe
worker calls shared probe.execute_probe. This agent manages those processes,
preserves attempt filenames and returns raw evidence, never functional verdicts.
Timeouts/missing output become ERROR evidence; malformed JSON and I/O/OS errors
propagate as before. Runner injection permits browser-free unit tests.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable


def _write_json(path: Path, obj: dict[str, Any]) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


class Agent4TestExecutor:
    """Execute only scripts/requests supplied by Pipeline in its approved workflow."""

    def execute_dynamic(self, delivery, *, session):
        import hashlib
        source = Path(delivery['script_path']).read_text(encoding='utf-8')
        if hashlib.sha256(source.encode()).hexdigest() != delivery['script_sha256']:
            raise ValueError('Frozen Agent3 source hash changed before formal execution')
        if not delivery.get('cold_trial_complete') or session.purpose != 'formal':
            raise ValueError('Formal execution requires a completed cold trial and independent formal session')
        if delivery.get('blockage'):
            if delivery['blockage'].get('review',{}).get('approved') is not True:
                raise ValueError('Partial execution requires an approved Agent2 blockage review')
        evidence = session.execute_program(source, step_ids=delivery['step_ids'], bindings=delivery['bindings'])
        if delivery.get('blockage'):
            evidence.update(execution_scope=delivery['execution_scope'],blockage=delivery['blockage'],
                            skipped_steps=delivery['skipped_steps'])
        return evidence

    def __init__(self, *, root: Path | None = None,
                 runner: Callable[..., subprocess.CompletedProcess[str]] | None = None) -> None:
        self.root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
        self._runner = runner if runner is not None else subprocess.run

    def execute(self, script: Path, run_dir: Path, *, attempt: int, env: dict[str, str]) -> dict[str, Any]:
        """Run a generated script (100s); persist attempt evidence and runner log.

        env is the child environment, including Pipeline's project PYTHONPATH.
        Returned evidence carries execution_status/steps/screenshots and any
        runtime collection_log/diagnostics, with no PASS/FAIL adjudication.
        """
        evidence_name = 'evidence.json' if attempt == 1 else f'evidence_attempt{attempt}.json'
        log_name = 'runner.log' if attempt == 1 else f'runner_attempt{attempt}.log'
        try:
            result = self._runner([sys.executable, str(script)], cwd=str(self.root), env=env,
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

    def probe(self, request: dict[str, Any], run_dir: Path, base_url: str, *, env: dict[str, str],
              headless: bool, demo_delay: float, allow_live: bool) -> dict[str, Any]:
        """Run an authorized detail_reobserve request (45s), preserving request,
        result JSON and worker log. Return status/fields/steps/screenshots or
        error; the shared worker validates URL, field and live-site constraints.
        """
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
            proc = self._runner(args, cwd=str(self.root), env=env, timeout=45,
                                capture_output=True, text=True)
            (run_dir / 'probe_runner.log').write_text((proc.stdout + '\n' + proc.stderr)[-10000:], encoding='utf-8')
            if result_path.exists():
                return json.loads(result_path.read_text(encoding='utf-8'))
            result = {'status': 'ERROR', 'error': 'Probe process failed: ' + (proc.stderr or 'No probe evidence')[-700:]}
        except subprocess.TimeoutExpired:
            result = {'status': 'ERROR', 'error': 'Probe exceeded its 45-second budget'}
        _write_json(result_path, result)
        return result
