"""Agent2: contract, oracle and evidence-plan construction plus review.

Input: Agent1 draft dict and target/mode; output: TestContract and a decision
dict (approved/method/issues/coverage, plus a template warning when applicable).
Only llm review calls the shared model. No browser/file writes or draft edits.
Invalid input/model data raises ValueError/KeyError; model errors propagate.
The pipeline owns approval retries and the contract revision number.
"""
from __future__ import annotations

import json
from typing import Any

from ..schema import JSONModel, QuerySpec, TestContract


class Agent2TestReviewer:
    """Construct a deterministic contract, then independently review intent + observability."""

    def approve(self, draft: dict[str, Any], *, target: str) -> TestContract:
        # Kept for v0.1 backwards compatibility.
        return self.build_contract(draft, target=target)

    def build_contract(self, draft: dict[str, Any], *, target: str) -> TestContract:
        query = draft["query"]
        spec = QuerySpec(**query)
        spec.validate()
        if target not in {"arxiv", "fixture", "fixture-bug"}:
            raise ValueError("Unrecognized target")
        steps = [
            {"id": "S1", "action": "navigate", "target": "advanced_search"},
            {"id": "S2", "action": "fill", "target": "search.keyword", "value": spec.keyword},
            {"id": "S3", "action": "submit", "target": "search.form"},
            {"id": "S4", "action": "read", "target": "results.first_paper"},
        ]
        oracles = [
            {"id": "O1", "kind": "equals", "left": "search.applied_keyword", "right": "query.keyword",
             "meaning": "搜索表单关键词已被提交"},
        ]
        evidence = [
            {"checkpoint": "form_filled", "collect": ["search.entered_keyword", "screenshot"]},
            {"checkpoint": "search_submitted", "collect": ["search.applied_keyword"]},
        ]
        if spec.search_field != "all":
            oracles.append({"id": "O4", "kind": "equals", "left": "search.applied_field",
                            "right": "query.search_field", "meaning": "指定搜索字段已应用"})
            evidence[1]["collect"].append("search.applied_field")
        if spec.computer_science:
            oracles.append({"id": "O5", "kind": "boolean_equals", "left": "search.applied_computer_science",
                            "right": "query.computer_science", "meaning": "计算机科学分类约束已应用"})
            evidence[1]["collect"].append("search.applied_computer_science")
        if spec.open_first_result:
            steps.extend([
                {"id": "S5", "action": "click", "target": "results.first_paper.abs_link"},
                {"id": "S6", "action": "read", "target": "paper.detail"},
            ])
            oracles.extend([
                {"id": "O2", "kind": "equals", "left": "results.arxiv_id", "right": "detail.arxiv_id",
                 "meaning": "列表与详情页论文 ID 一致"},
                {"id": "O3", "kind": "normalized_equals", "left": "results.title", "right": "detail.title",
                 "meaning": "列表与详情页论文标题一致"},
            ])
            evidence.extend([
                {"checkpoint": "results_loaded", "collect": ["results.count", "results.title", "results.arxiv_id", "screenshot"]},
                {"checkpoint": "detail_loaded", "collect": ["detail.title", "detail.arxiv_id", "current_url", "screenshot"]},
            ])
        else:
            evidence.append({"checkpoint": "results_loaded", "collect": ["results.count", "screenshot"]})
        return TestContract(case_id="arxiv_search_detail_001" if spec.open_first_result else "arxiv_search_only_001",
                            requirement=draft["requirement"], query=query, mode=draft["interpretation_mode"],
                            target=target, steps=steps, oracles=oracles, evidence_plan=evidence)

    def review(self, draft: dict[str, Any], *, target: str, mode: str = "template", llm: JSONModel | None = None
               ) -> tuple[TestContract, dict[str, Any]]:
        contract = self.build_contract(draft, target=target)
        if mode == "template":
            return contract, {"approved": True, "method": "deterministic_rules",
                              "issues": [], "coverage": ["supported_query_fields", "observable_search_conditions"],
                              "warning": "仅进行确定性审查，未进行 LLM 独立语义审核"}
        if mode != "llm":
            raise ValueError("Unsupported reviewer mode")
        if llm is None:
            from ..llm_adapter import StructuredLLM
            llm = StructuredLLM()
        system = (
            "You are Agent2, an INDEPENDENT test reviewer. Review the user's original Chinese/English requirement "
            "against the proposed query and executable contract. Only supported: one arXiv advanced search, "
            "keyword, one field (all/title/abstract/author), computer_science checkbox, optionally open "
            "the first result and compare its arXiv ID/title with the details. "
            "Reject if keyword, field, category or detail intent is mismatched, if any demanded behavior is missing, "
            "or if oracle evidence is insufficient. If it is unsupported, reject with reason; do not hallucinate support. "
            "Respond with a JSON object: approved (boolean), issues (array of concise strings), "
            "coverage (array of short labels). Do not modify the test contract or expected results."
        )
        data = llm.complete_json(system, json.dumps({"original_requirement": draft["requirement"],
                                                     "candidate_contract": contract.to_dict()}, ensure_ascii=False))
        if type(data.get("approved")) is not bool:
            raise ValueError("Agent2 review 必须输出布尔 approved")
        issues, coverage = data.get("issues", []), data.get("coverage", [])
        if (not isinstance(issues, list) or not all(isinstance(x, str) for x in issues)
                or not isinstance(coverage, list) or not all(isinstance(x, str) for x in coverage)):
            raise ValueError("Agent2 review 的 issues / coverage 必须是字符串数组")
        if not data["approved"] and not issues:
            issues = ["Agent2 拒绝用例，但未提供可恢复的审查意见"]
        return contract, {"approved": data["approved"], "method": "llm_independent_review",
                          "issues": issues, "coverage": coverage}
