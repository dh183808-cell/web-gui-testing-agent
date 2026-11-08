import json
from pathlib import Path
from urllib.request import urlopen

from web_agent.fixture_server import local_fixture
from web_agent.pipeline import run_pipeline
from web_agent.report import html_report
from web_agent.agents import Agent5EvidenceVerifier
from web_agent.agents import executor as executor_module
from .test_contract import sample_evidence, build


def test_local_fixture_and_bug_endpoint():
    with local_fixture(bug=True) as (url, _):
        form = urlopen(url).read().decode()
        assert 'name="bug" value="1"' in form
        query = url.split("?")[0] + "?terms-0-term=GUI+Agent&bug=1"
        page = urlopen(query).read().decode()
        assert "arxiv-result" in page
        assert "Intentionally Wrong" in page


def test_pipeline_generates_all_artifacts_without_running_browser(tmp_path):
    r = run_pipeline(target="fixture", only_generate=True, output_root=tmp_path)
    d = Path(r["run_dir"])
    for name in ["draft_cases.json", "test_contract.json", "dom_snapshot.json",
                 "locator_plan.json", "generated_test.py", "evidence.json",
                 "final_verdict.json", "report.html", "stages.json"]:
        assert (d/name).exists(), name
    assert r["verdict"]["status"] == "INCONCLUSIVE"
    assert r["inspector"]["inspector_source"] == "bundled_local_reference_dom"


def test_report_escapes_user_content(tmp_path):
    c = build()
    c["requirement"] = '<script>alert("XSS")</script>'
    v = Agent5EvidenceVerifier().verify(c, sample_evidence()).to_dict()
    p = html_report(tmp_path, c, sample_evidence(), v)
    text = p.read_text(encoding="utf-8")
    assert "&lt;script&gt;" in text
    assert '<script>alert("XSS")</script>' not in text


def test_six_stage_pipeline_with_mock_executor_pass(tmp_path, monkeypatch):
    """Contract/evidence/report integration; NOT a real Selenium browser test."""
    from types import SimpleNamespace
    import web_agent.pipeline as pipeline
    from web_agent.dom_inspector import compact_dom
    from web_agent.fixture_server import search_form
    monkeypatch.setattr(pipeline, "inspect_page", lambda *a, **kw: {
        **compact_dom(search_form()), "inspector_source": "real_selenium_dom"})

    def mock_subprocess_run(args, **kwargs):
        p = Path(args[1]).with_name("evidence.json")
        p.write_text(json.dumps(sample_evidence(), ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(stdout="MOCK: no browser executed", stderr="", returncode=0)
    monkeypatch.setattr(executor_module.subprocess, "run", mock_subprocess_run)
    r = pipeline.run_pipeline(target="fixture", output_root=tmp_path)
    assert r["verdict"]["status"] == "PASS"
    assert [s["agent"] for s in r["stages"]] == [f"Agent{i}" for i in range(1, 7)]
    assert Path(r["report"]).exists()


def test_six_stage_pipeline_with_mock_executor_detects_defect(tmp_path, monkeypatch):
    """Synthetic injected mismatch -> FAIL, without lying about arxiv.org."""
    from types import SimpleNamespace
    import web_agent.pipeline as pipeline
    from web_agent.dom_inspector import compact_dom
    from web_agent.fixture_server import search_form
    monkeypatch.setattr(pipeline, "inspect_page", lambda *a, **kw: {
        **compact_dom(search_form()), "inspector_source": "real_selenium_dom"})

    def mock_subprocess_run(args, **kwargs):
        Path(args[1]).with_name("evidence.json").write_text(
            json.dumps(sample_evidence(bad=True), ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(stdout="MOCK", stderr="", returncode=0)
    monkeypatch.setattr(executor_module.subprocess, "run", mock_subprocess_run)
    r = pipeline.run_pipeline(target="fixture-bug", output_root=tmp_path)
    assert r["verdict"]["status"] == "FAIL"
    assert Path(r["report"]).exists()
