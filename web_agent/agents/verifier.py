"""Agent5: deterministic oracle verification and targeted probe decisions.

Input: serialized TestContract and execution evidence; output: Verdict and,
via plan_probe, an optional detail_reobserve request. Inputs are not mutated.
No model, browser, subprocess or file side effects. Unsupported oracle kinds
raise ValueError; incomplete/error traces become INCONCLUSIVE/ERROR. Confirmed
oracle failures take precedence over unrelated missing evidence.
The pipeline owns probe authorization, budget, execution and evidence merging.
"""
from __future__ import annotations

import re
from typing import Any

from ..evidence_plan import get_path
from ..probe_plan import make_probe_request
from ..schema import Verdict


class Agent5EvidenceVerifier:
    """Evaluate contract-defined observables; do not synthesize missing evidence."""

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"^(Title:\s*)", "", text.strip(), flags=re.I)).casefold().strip(" .")

    _read = staticmethod(get_path)

    def verify(self, contract: dict[str, Any], evidence: dict[str, Any]) -> Verdict:
        from ..dynamic_schema import DynamicTestContract
        if isinstance(contract, DynamicTestContract):
            return self.verify_dynamic(contract, evidence.get('evidence', evidence),
                execution_status=evidence.get('execution_status', 'COMPLETE'))
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
        if any(c["passed"] is False for c in checks):
            return Verdict(status="FAIL", message="执行完成但出现测试预言不一致（真实网站需人工核实）", checks=checks)
        if missing:
            return Verdict(status="INCONCLUSIVE", message=f"缺少关键证据：{', '.join(missing)}", checks=checks)
        return Verdict(status="PASS", message="已收集的证据满足当前测试契约中的全部预言", checks=checks)

    def verify_dynamic(self, contract, evidence, *, execution_status='COMPLETE', error=None, run_dir=None):
        from ..dynamic_verification import verify_dynamic
        return verify_dynamic(contract, evidence, execution_status=execution_status, error=error, run_dir=run_dir)

    def plan_dynamic_probe(self, contract, evidence, verdict, *, step_id=None):
        from ..dynamic_verification import plan_dynamic_probe
        return plan_dynamic_probe(contract, evidence, verdict, step_id=step_id)

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
