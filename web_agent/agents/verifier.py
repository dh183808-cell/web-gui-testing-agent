"""Agent5: deterministic oracle verification and targeted probe decisions.

Input: serialized TestContract and execution evidence; output: Verdict and,
via plan_probe, an optional detail_reobserve request. Inputs are not mutated.
No model, browser, subprocess or file side effects. Unsupported oracle kinds
raise ValueError; incomplete/error traces become INCONCLUSIVE/ERROR.
The pipeline owns probe authorization, budget, execution and evidence merging.
"""
from __future__ import annotations

import re
import operator
from typing import Any

from ..evidence_plan import get_path
from ..probe_plan import make_probe_request
from ..schema import Verdict, NUMERIC_ORACLES, finite_number, validate_oracle_binding
from ..date_semantics import compare_date


class Agent5EvidenceVerifier:
    """Evaluate contract-defined observables; do not synthesize missing evidence."""

    def verify_dynamic(self, contract, evidence):
        if evidence.get('execution_status') != 'COMPLETE':
            return Verdict(status='ERROR', message='Formal Selenium execution did not complete')
        checks = []
        for oracle in contract['oracles']:
            measurement = evidence.get('measurements', {}).get(oracle['id'], {})
            actual = measurement.get('value')
            expected = oracle['expected']
            date_comparison = dict(expected=expected, raw_actual=actual, normalized_actual=None,
                                   reason='Complete independent Formal date evidence not established')
            skipped = next((s for s in evidence.get('skipped_steps',[])
                            if s['step_id']==oracle.get('step_id')),None)
            if measurement.get('read'):
                try:
                    validate_oracle_binding(oracle, measurement)
                except ValueError as exc:
                    return Verdict(status='ERROR',message='Formal evidence binding type error: '+str(exc))
            if (actual is None or measurement.get('status', 'observed') != 'observed'
                    or measurement.get('text_complete') is False or measurement.get('truncated') is True
                    or not measurement.get('observation_ref') or not measurement.get('screenshot_ref')
                    or measurement.get('step_id', oracle.get('step_id')) != oracle.get('step_id')):
                passed = None
            elif oracle.get('target') and actual is False:
                # A zero locator match, partial inventory or a Trial observation
                # never establishes absence. Require independent Formal evidence.
                probe = measurement.get('probe',{})
                samples = probe.get('samples',[])
                reliable = (evidence.get('purpose')=='formal'
                    and evidence.get('blockage',{}).get('review',{}).get('approved') is True
                    and measurement.get('absence_confirmed') is True
                    and measurement.get('inventory_complete') is True
                    and probe.get('context_stable') is True and not probe.get('reasons')
                    and probe.get('url') == evidence.get('blockage',{}).get('measurement',{}).get('probe',{}).get('url')
                    and probe.get('title') == evidence.get('blockage',{}).get('measurement',{}).get('probe',{}).get('title')
                    and len(samples)>=3 and all(s.get('ready_state')=='complete' and s.get('found')==0 for s in samples)
                    and probe.get('elapsed_seconds',0)>=probe.get('wait_seconds',2)>0)
                passed = False if reliable else None
            elif (evidence.get('execution_scope') == 'reviewed_prefix_and_target_probe'
                    and actual is False and expected is True
                    and measurement.get('read') in {'exists','visible_exists','visible'}):
                # A redundant model locator may also miss the claimed control.
                # It cannot independently convert an incomplete inventory to FAIL.
                passed = None
            elif oracle['kind'] == 'date_equals':
                if (evidence.get('purpose') != 'formal' or not measurement.get('read')
                        or measurement.get('step_id') != oracle.get('step_id')
                        or measurement.get('visible') != 1 or measurement.get('matched') != 1):
                    passed = None
                    date_comparison = dict(reason='Independent visible Formal date measurement required',
                                           expected=expected, raw_actual=actual, normalized_actual=None)
                else:
                    passed, date_comparison = compare_date(expected, measurement,formal_context=evidence)
            elif oracle['kind'] == 'equals':
                passed = type(actual) is type(expected) and actual == expected
            elif oracle['kind'] == 'set_equals':
                passed = isinstance(actual,list) and isinstance(expected,list) and sorted(actual)==sorted(expected)
            elif oracle['kind'] in NUMERIC_ORACLES:
                passed = (finite_number(actual) and finite_number(expected)
                          and {'gte':operator.ge,'gt':operator.gt,'lte':operator.le,'lt':operator.lt}[oracle['kind']](actual,expected))
            else:
                passed = expected in actual if isinstance(actual,(str,list)) else False
            checks.append(dict(oracle_id=oracle['id'], meaning=oracle['meaning'], expected=expected,
                               actual=actual, passed=passed, observation_ref=measurement.get('observation_ref'),
                               screenshot_ref=measurement.get('screenshot_ref'),
                               **({'date_comparison':date_comparison} if oracle['kind']=='date_equals' else {}),
                               **{k:oracle[k] for k in ('source_type','source_quote','source_span') if k in oracle},
                               binding={k: measurement[k] for k in ('by','selector','read','step_id','attribute','target') if k in measurement},
                               evidence_status='not_executed' if skipped else measurement.get('status',
                                   'observed' if measurement else 'missing'),
                               **({'not_executed_reason':skipped['reason'],
                                   'blocked_by':skipped['blocked_by']} if skipped else {})))
        state = ('FAIL' if any(c['passed'] is False for c in checks) else
                 'INCONCLUSIVE' if not checks or any(c['passed'] is None for c in checks) else 'PASS')
        if evidence.get('execution_scope') == 'reviewed_prefix_and_target_probe':
            # Even if the target appears in Formal, missing dependent actions
            # cannot become a PASS. Preserve that changed state as INCONCLUSIVE.
            if state == 'PASS':
                state = 'INCONCLUSIVE'
        verdict = Verdict(status=state, message='Formal DOM evidence compared with frozen expectations', checks=checks)
        return verdict

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"^(Title:\s*)", "", text.strip(), flags=re.I)).casefold().strip(" .")

    _read = staticmethod(get_path)

    def verify(self, contract: dict[str, Any], evidence: dict[str, Any]) -> Verdict:
        state = evidence.get("execution_status")
        if state == "ERROR":
            return Verdict(status="ERROR", message=evidence.get("error", "Selenium 执行失败"),
                           suggestions=["检查浏览器驱动、页面可达性与定位器"])
        if state != "COMPLETE":
            return Verdict(status="INCONCLUSIVE", message="尚未获得完整执行轨迹或无法找到结果条目")
        checks = []
        missing = []
        for oracle in contract.get("oracles", []):
            left, right = oracle["left"], oracle["right"]
            # Right side may be either the frozen contract query or previously observed evidence.
            actual = self._read(evidence, left)
            expected = self._read(contract, right) if right.startswith("query.") else self._read(evidence, right)
            if actual is None or expected is None or (isinstance(actual, str) and not actual.strip()) or (
                    isinstance(expected, str) and not expected.strip()):
                missing.append(oracle["id"])
                checks.append({"oracle_id": oracle["id"], "name": oracle["meaning"],
                               "passed": None, "expected": expected, "actual": actual})
                continue
            if oracle["kind"] == "normalized_equals":
                passed = self._norm(str(actual)) == self._norm(str(expected))
            elif oracle["kind"] == "boolean_equals":
                passed = type(actual) is bool and type(expected) is bool and actual == expected
            elif oracle["kind"] == "equals":
                passed = str(actual).strip() == str(expected).strip()
            else:
                raise ValueError("Unsupported oracle type")
            checks.append({"oracle_id": oracle["id"], "name": oracle["meaning"],
                           "passed": passed, "expected": expected, "actual": actual})
        if missing:
            return Verdict(status="INCONCLUSIVE", message=f"缺少关键证据：{', '.join(missing)}", checks=checks)
        if any(c["passed"] is False for c in checks):
            return Verdict(status="FAIL", message="执行完成但出现测试预言不一致（真实网站需人工核实）", checks=checks)
        return Verdict(status="PASS", message="已收集的证据满足当前测试契约中的全部预言", checks=checks)

    def plan_probe(self, contract: dict[str, Any], evidence: dict[str, Any],
                   verdict: Verdict) -> dict[str, Any] | None:
        """Return an eligible missing-detail request; scheduling stays in Pipeline."""
        if verdict.status != 'INCONCLUSIVE':
            return None
        missing = missing_oracle_evidence(contract, evidence, verdict.checks)
        return make_probe_request(contract, evidence, missing=missing)


def missing_oracle_evidence(contract: dict[str, Any], evidence: dict[str, Any], checks: list[dict[str, Any]]) -> list[str]:
    missing_ids = {c.get('oracle_id') for c in checks if c.get('passed') is None}
    missing: list[str] = []
    for oracle in contract['oracles']:
        if oracle['id'] not in missing_ids:
            continue
        for path in (oracle['left'], oracle['right']):
            if path.startswith('query.'):
                continue
            value = get_path(evidence, path)
            if (value is None or isinstance(value, str) and not value.strip()) and path not in missing:
                missing.append(path)
    return missing
