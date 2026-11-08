from pathlib import Path
import json
import pytest

from web_agent.agents import (Agent1TestDesigner, Agent2TestReviewer,
                              Agent5EvidenceVerifier, Agent6RecoveryController)
from web_agent.dom_inspector import compact_dom
from web_agent.fixture_server import search_form
from web_agent.agents.generator import Agent3SeleniumGenerator, find_locators
from web_agent.pipeline import DEFAULT_REQUIREMENT


def build():
    d = Agent1TestDesigner().design(DEFAULT_REQUIREMENT)
    return Agent2TestReviewer().build_contract(d, target="fixture").to_dict()


def sample_evidence(bad=False):
    return {"execution_status": "COMPLETE", "steps": [{"stage": "search_submitted"}],
            "search": {"applied_keyword": "GUI Agent"},
            "results": {"arxiv_id": "2601.00001", "title": "Evaluating GUI Agents for Software Testing"},
            "detail": {"arxiv_id": "2601.00001", "title": (
                "Completely Different Title" if bad else "Evaluating GUI Agents for Software Testing")},
            "screenshots": []}


def test_template_mode_explicit():
    d = Agent1TestDesigner().design(DEFAULT_REQUIREMENT)
    assert d["query"]["keyword"] == "GUI Agent"
    assert d["interpretation_mode"] == "template"
    assert "非真实 LLM" in d["warning"]


def test_llm_mode_requires_key(monkeypatch):
    from web_agent import llm_adapter
    monkeypatch.setattr(llm_adapter, "_load_project_dotenv", lambda: None)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        Agent1TestDesigner().design("搜索论文", mode="llm")


def test_reviewed_oracles():
    c = build()
    assert len(c["oracles"]) == 3
    assert len(c["evidence_plan"]) == 4
    assert c["query"]["keyword"] == "GUI Agent"


def test_dom_and_locator_inspection():
    snap = compact_dom(search_form())
    assert snap["count"] >= 4
    loc = find_locators(snap)
    assert loc["keyword"][0] == "#terms-0-term"
    assert loc["submit"]


def test_code_generation_is_importable(tmp_path):
    path = Agent3SeleniumGenerator().generate(
        build(), compact_dom(search_form()), tmp_path,
        base_url="http://127.0.0.1:8765/search/advanced")
    code = path.read_text(encoding="utf-8")
    compile(code, str(path), "exec")
    assert "selenium_runtime" in code
    assert (tmp_path/"locator_plan.json").exists()


def test_cross_page_pass():
    v = Agent5EvidenceVerifier().verify(build(), sample_evidence())
    assert v.status == "PASS"
    assert all(c["passed"] for c in v.checks)


def test_injected_wrong_title_fail(tmp_path):
    v = Agent5EvidenceVerifier().verify(build(), sample_evidence(bad=True))
    assert v.status == "FAIL"
    assert not v.checks[-1]["passed"]
    result = Agent6RecoveryController().finalize(v, run_dir=tmp_path)
    assert result["auto_repair_performed"] is False


def test_failure_does_not_rewrite_oracle():
    c = build()
    before = json.dumps(c, sort_keys=True)
    Agent5EvidenceVerifier().verify(c, sample_evidence(bad=True))
    assert json.dumps(c, sort_keys=True) == before


def test_error_and_inconclusive():
    verifier = Agent5EvidenceVerifier()
    assert verifier.verify(build(), {"execution_status": "ERROR", "error": "driver missing"}).status == "ERROR"
    assert verifier.verify(build(), {"execution_status": "NOT_RUN"}).status == "INCONCLUSIVE"
    assert verifier.verify(build(), {"execution_status": "COMPLETE", "results": {}, "detail": {}}).status == "INCONCLUSIVE"
