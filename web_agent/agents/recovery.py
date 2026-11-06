"""Agent6: bounded locator-repair policy and final verdict serialization.

plan_repair takes a Verdict, evidence, target and retry/authorization state;
returns an allowlisted repair dict or None, without mutating inputs or running
a browser/generator. The pipeline schedules regeneration and retries.
finalize takes a Verdict and history, returns the final dict and writes
final_verdict.json in an existing run_dir. File errors propagate.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..schema import Verdict


class Agent6RecoveryController:
    """Bounded, evidence-driven locator recovery, not a second bug oracle.

    Only locator errors are eligible. Repaired scripts regenerate allowlisted
    candidate selectors from the inspected DOM. Application FAIL is terminal.
    """

    RECOVERABLE_STAGES = {
        "locate_keyword": "keyword",
        "set_search_field": "field",
        "set_cs_filter": "computer_science",
    }

    def plan_dynamic_repair(self, contract, error, observation, *, previous_repairs=0,
                            max_repairs=1, live_recovery_allowed=False, verdict=None):
        """Pure decision. Only execution method may change; no semantic repair."""
        from urllib.parse import urlsplit
        import ipaddress
        from ..dynamic_execution import origin, step_for
        contract.assert_integrity()
        decision = {'type': 'dynamic_recovery', 'allowed': False, 'strategy': 'stop',
            'contract_digest': contract.design_digest, 'step_id': error.get('step_id'),
            'category': error.get('category', 'environment'), 'reason': 'Not safely recoverable',
            'attempt': previous_repairs + 1, 'observation_id': observation.get('observation_id') if observation else None}
        if verdict and verdict.status == 'FAIL':
            decision.update(category='application_assertion', reason='Confirmed application failure is terminal')
            return decision
        if type(max_repairs) is not int or not 0 <= max_repairs <= 3 or previous_repairs >= max_repairs:
            decision['reason'] = 'Recovery budget exhausted'
            return decision
        try:
            step = step_for(contract, error['step_id'])
            hostname = urlsplit(contract.design.target_url).hostname
            try:
                local = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                local = hostname == 'localhost'
            if not local and not live_recovery_allowed:
                decision['reason'] = 'Extra live-site recovery permission required'
                return decision
            if not observation or origin(observation['url']) != origin(contract.design.target_url):
                return decision
            if error.get('category') not in ('locator', 'interaction', 'wait'):
                return decision
            if type(error.get('action_started')) is not bool:
                return decision
            if error['action_started'] and step.action not in ('observe', 'wait'):
                decision['reason'] = 'Potential side effect: action already dispatched; do not replay'
                return decision
            decision.update(allowed=True, strategy='regenerate_from_fresh_dom',
                            reason='Retry unchanged step using newly observed DOM')
        except (KeyError, ValueError, TypeError):
            pass
        return decision

    def plan_repair(self, verdict: Verdict, evidence: dict[str, Any], *, target: str,
                    previous_repairs: int = 0, max_repairs: int = 1,
                    live_recovery_allowed: bool = False) -> dict[str, Any] | None:
        if verdict.status != "ERROR" or previous_repairs >= max_repairs:
            return None
        if target == "arxiv" and not live_recovery_allowed:
            return None
        if target not in {"arxiv", "fixture", "fixture-bug"}:
            return None
        stage = evidence.get("error_stage")
        part = self.RECOVERABLE_STAGES.get(stage)
        if not part:
            return None
        diagnostics = evidence.get("diagnostics") or {}
        if (target == "arxiv" and not diagnostics.get("url")):
            return None
        return {
            "repair_type": "recompute_allowlisted_locator",
            "failure_stage": stage,
            "locator_key": part,
            "scope": "locator_only",
            "reason": evidence.get("error", "")[:400],
            "attempt": previous_repairs + 1,
            "contract_must_remain_unchanged": True,
        }

    def finalize(self, verdict: Verdict, *, run_dir: Path,
                 recovery_history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        history = recovery_history or []
        successful_repair = bool(history and history[-1].get("result_status") == "PASS")
        policy = "completed_no_repair"
        if history:
            policy = "bounded_locator_repair_completed" if successful_repair else "bounded_locator_repair_attempted"
        elif verdict.status == "ERROR":
            policy = "not_recoverable_or_recovery_disabled"
        elif verdict.status == "INCONCLUSIVE":
            policy = "insufficient_evidence_after_allowed_probes"
        output = {**verdict.to_dict(), "recovery_policy": policy,
                  "auto_repair_performed": bool(history), "recovery_history": history,
                  "run_dir": str(run_dir)}
        (run_dir / "final_verdict.json").write_text(
            json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        return output
