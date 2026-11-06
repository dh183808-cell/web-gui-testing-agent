"""Offline tests of REAL LLM workflow contracts, with a mocked JSON service.

A mocked model does NOT claim the live LLM API was called. These tests verify
role interactions, strict schema, revision behavior and generated artifacts.
"""
import ast
import json
from pathlib import Path

import pytest

from web_agent.agents import Agent1TestDesigner, Agent2TestReviewer, Agent5EvidenceVerifier
from web_agent.dom_inspector import compact_dom
from web_agent.fixture_server import search_form
from web_agent.generator import Agent3SeleniumGenerator
from web_agent.pipeline import run_pipeline


class QueueLLM:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append({"system": system, "user": user})
        return self.outputs.pop(0)


def test_real_mode_short_search_with_title_filter_and_cs():
    llm = QueueLLM({"keyword": "GUI Agent", "search_field": "title",
                    "computer_science": True, "open_first_result": False,
                    "unsupported_requests": []})
    draft = Agent1TestDesigner().design("只在标题中搜索 GUI Agent，限制计算机科学，不打开详情", mode="llm", llm=llm)
    contract = Agent2TestReviewer().build_contract(draft, target="fixture").to_dict()
    assert contract["case_id"] == "arxiv_search_only_001"
    assert [s["action"] for s in contract["steps"]] == ["navigate", "fill", "submit", "read"]
    assert {o["id"] for o in contract["oracles"]} == {"O1", "O4", "O5"}
    evidence = {"execution_status": "COMPLETE", "search": {"applied_keyword": "GUI Agent",
                "applied_field": "title", "applied_computer_science": True}, "results": {"count": 2}}
    assert Agent5EvidenceVerifier().verify(contract, evidence).status == "PASS"
    evidence["search"]["applied_computer_science"] = False
    assert Agent5EvidenceVerifier().verify(contract, evidence).status == "FAIL"
    evidence["search"].pop("applied_field")
    # v0.4 stage 3: confirmed failure takes precedence over unrelated missing evidence.
    assert Agent5EvidenceVerifier().verify(contract, evidence).status == "FAIL"


def test_llm_review_rejects_then_agent1_revises(tmp_path):
    llm = QueueLLM(
        {"keyword": "agent", "search_field": "all", "computer_science": False,
         "open_first_result": True, "unsupported_requests": []},
        {"approved": False, "issues": ["需求是标题搜索且不需要打开详情"], "coverage": []},
        {"keyword": "agent", "search_field": "title", "computer_science": False,
         "open_first_result": False, "unsupported_requests": []},
        {"approved": True, "issues": [], "coverage": ["search_field", "result_observation"]},
        {"preferred": {"keyword": "#terms-0-term", "submit": 'form button[type="submit"]',
                       "field": "#terms-0-field"}},
    )
    run = run_pipeline(requirement="标题中搜索 agent，不打开详情", mode="llm", target="fixture",
                       output_root=tmp_path, only_generate=True, llm=llm)
    d = Path(run["run_dir"])
    assert run["contract"]["query"]["open_first_result"] is False
    assert run["contract"]["revision"] == 2
    assert len(run["reviews"]) == 2
    assert len(llm.calls) == 5
    assert (d / "review_decision.json").exists()
    audit = json.loads((d / "generation_audit.json").read_text(encoding="utf-8"))
    assert audit["llm_selected_locators"]["submit"] == 'form button[type="submit"]'
    assert run["verdict"]["status"] == "INCONCLUSIVE"  # generate-only must not pretend PASS
    compile((d / "generated_test.py").read_text(encoding="utf-8"), str(d / "generated_test.py"), "exec")


def test_agent3_rejects_hallucinated_css(tmp_path):
    draft = Agent1TestDesigner().design('搜“GUI Agent”，打开详情')
    contract = Agent2TestReviewer().build_contract(draft, target="fixture").to_dict()
    llm = QueueLLM({"preferred": {"keyword": '#evil-selector', "submit": 'button[type="submit"]'}})
    with pytest.raises(ValueError, match="未经允许"):
        Agent3SeleniumGenerator().generate(contract, compact_dom(search_form()), tmp_path,
                                           base_url="http://localhost:8765", mode="llm", llm=llm)
    assert not (tmp_path / "generated_test.py").exists()


def test_agent1_rejects_unsupported_tasks():
    llm = QueueLLM({"keyword": "GUI Agent", "search_field": "all", "computer_science": False,
                    "open_first_result": True, "unsupported_requests": ["按年份筛选"]})
    with pytest.raises(ValueError, match="不支持"):
        Agent1TestDesigner().design("只搜 2025 年文章", mode="llm", llm=llm)


def test_review_needs_approval(tmp_path):
    llm = QueueLLM({"keyword": "GUI Agent", "search_field": "all",
                    "computer_science": False, "open_first_result": True, "unsupported_requests": []},
                   {"approved": False, "issues": ["too vague"], "coverage": []},
                   {"keyword": "GUI Agent", "search_field": "all",
                    "computer_science": False, "open_first_result": True, "unsupported_requests": []},
                   {"approved": False, "issues": ["still too vague"], "coverage": []})
    with pytest.raises(ValueError, match="两次审核均拒绝"):
        run_pipeline(mode="llm", target="fixture", output_root=tmp_path,
                     only_generate=True, llm=llm)
    dirs = list(tmp_path.iterdir())
    assert len(dirs) == 1
    assert (dirs[0] / "review_decision.json").exists()
    assert not (dirs[0] / "generated_test.py").exists()


def test_contract_is_not_rewritten_when_llm_reviews(tmp_path):
    draft = Agent1TestDesigner().design('搜索“GUI Agent”，打开论文详情')
    baseline = Agent2TestReviewer().build_contract(draft, target="fixture").to_dict()
    before = json.dumps(baseline["oracles"], sort_keys=True)
    llm = QueueLLM({"approved": True, "issues": [], "coverage": ["evidence"]})
    approved, review = Agent2TestReviewer().review(draft, target="fixture", mode="llm", llm=llm)
    assert review["approved"] is True
    assert json.dumps(approved.to_dict()["oracles"], sort_keys=True) == before
