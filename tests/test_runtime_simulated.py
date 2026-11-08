"""Pure fake-WebDriver tests (NOT actual browser execution).

Verifies the Selenium runtime orchestration paths without importing selenium.
"""
import sys
import types
from pathlib import Path

import pytest

from web_agent.agents import Agent1TestDesigner, Agent2TestReviewer, Agent5EvidenceVerifier
from web_agent.dom_inspector import compact_dom
from web_agent.fixture_server import search_form
from web_agent.agents.generator import find_locators
from web_agent import selenium_runtime


class Node:
    def __init__(self, driver, kind):
        self.driver, self.kind = driver, kind
        self.text = {"title": "Title: Evaluating GUI Agents for Software Testing",
                     "list_title": "Evaluating GUI Agents for Software Testing",
                     "submit": "Search", "add_term": "Add another term +"}.get(kind, "")
        self.value = ""

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def is_selected(self):
        return self.driver.cs

    def clear(self):
        self.value = ""

    def send_keys(self, value):
        self.value = value
        self.driver.keyword = value

    def get_property(self, name):
        return self.value

    def get_attribute(self, name):
        if self.kind == "link" and name == "href":
            return "http://example.test/abs/2601.00001"
        if self.kind == "submit" and name == "type":
            return "submit"
        if self.kind == "add_term" and name == "type":
            return "button"
        if self.kind == "form" and name == "action":
            return "http://example.test/search/advanced"
        return None

    def click(self):
        if self.kind == "checkbox":
            self.driver.cs = True
        elif self.kind == "submit":
            if self.driver.noop_submit:
                return
            from urllib.parse import urlencode
            query = {"terms-0-term": self.driver.keyword, "terms-0-field": self.driver.field}
            if self.driver.cs:
                query["classification-computer_science"] = "y"
            self.driver.current_url = ("http://example.test/results?q=GUI+Agent"
                                       if self.driver.alt_results_url else
                                       "http://example.test/search/advanced?" + urlencode(query))
            self.driver.state = "results"
        elif self.kind == "link":
            self.driver.current_url = "http://example.test/abs/2601.00001"
            self.driver.state = "detail"

    def find_element(self, by, selector):
        if self.kind == "keyword" and "ancestor::form" in selector:
            return Node(self.driver, "form")
        assert self.kind == "result"
        return Node(self.driver, "list_title")

    def find_elements(self, by, selector):
        if self.kind == "form" and "button" in selector:
            return [Node(self.driver, "add_term"), Node(self.driver, "submit")]
        if self.kind == "result" and "/abs/" in selector:
            return [Node(self.driver, "link")]
        return []


class FakeDriver:
    def __init__(self):
        self.current_url = "http://example.test/search/advanced"
        self.state = "search"
        self.keyword = ""
        self.field = "all"
        self.cs = False
        self.closed = False
        self.noop_submit = False
        self.alt_results_url = False

    def get(self, url):
        self.current_url = url

    @property
    def page_source(self):
        return "<li class=arxiv-result>ok</li>" if self.state == "results" else "<html></html>"

    def find_elements(self, by, selector):
        if self.state == "search":
            if selector in ['#terms-0-term', 'input[name="terms-0-term"]', 'input[type="search"]']:
                return [Node(self, "keyword")]
            if selector in ['#terms-0-field', 'select[name="terms-0-field"]']:
                return [Node(self, "select")]
            if selector in ['#classification-computer_science', 'input[name="classification-computer_science"]']:
                return [Node(self, "checkbox")]
            if "submit" in selector:
                return [Node(self, "submit")]
        if self.state == "results" and selector == "li.arxiv-result":
            return [Node(self, "result")]
        if self.state == "detail" and selector in {"h1.title", "h1#title", "h1"}:
            return [Node(self, "title")]
        return []

    def save_screenshot(self, path):
        Path(path).write_bytes(b"simulated-png")

    def quit(self):
        self.closed = True


@pytest.fixture
def fake_selenium(monkeypatch):
    class By:
        CSS_SELECTOR = "css"
        XPATH = "xpath"

    class WebDriverWait:
        def __init__(self, driver, timeout):
            self.driver = driver

        def until(self, condition):
            result = condition(self.driver)
            if not result:
                raise AssertionError("Simulated wait condition never became true")
            return result

    class Select:
        def __init__(self, node):
            self.node = node

        def select_by_value(self, value):
            self.node.driver.field = value

    # Use importable stub hierarchy to test execute_test, not to emulate Selenium globally.
    selenium = types.ModuleType("selenium")
    webdriver = types.ModuleType("selenium.webdriver")
    common = types.ModuleType("selenium.webdriver.common")
    by = types.ModuleType("selenium.webdriver.common.by")
    support = types.ModuleType("selenium.webdriver.support")
    ui = types.ModuleType("selenium.webdriver.support.ui")
    by.By, ui.Select, ui.WebDriverWait = By, Select, WebDriverWait
    for mod in (selenium, webdriver, common, by, support, ui):
        monkeypatch.setitem(sys.modules, mod.__name__, mod)
    fake = FakeDriver()
    monkeypatch.setattr(selenium_runtime, "make_driver", lambda **kw: fake)
    return fake


@pytest.mark.parametrize("detail", [True, False])
def test_runtime_contract_branch_executes_without_hallucinations(fake_selenium, tmp_path, detail):
    draft = Agent1TestDesigner().design('搜索“GUI Agent”')
    draft["query"].update({"search_field": "title", "computer_science": True,
                           "open_first_result": detail})
    contract = Agent2TestReviewer().build_contract(draft, target="fixture").to_dict()
    locators = find_locators(compact_dom(search_form()))
    output = tmp_path / "evidence.json"
    evidence = selenium_runtime.execute_test(contract, locators, "http://example.test/search/advanced",
                                             output, headless=True)
    assert evidence["execution_status"] == "COMPLETE"
    assert fake_selenium.closed
    assert evidence["search"]["applied_keyword"] == "GUI Agent"
    assert evidence["search"]["applied_field"] == "title"
    assert evidence["search"]["applied_computer_science"] is True
    assert len(evidence["screenshots"]) == (3 if detail else 2)
    assert ("detail_loaded" in [x["stage"] for x in evidence["steps"]]) is detail
    assert Agent5EvidenceVerifier().verify(contract, evidence).status == "PASS"


def test_submission_is_bound_to_keyword_form(fake_selenium, tmp_path):
    # Two page-level Search buttons are present in the production-like HTML.
    # Only the button in the input's owning form may be clicked.
    html = search_form().decode("utf-8")
    assert 'action="/header-search"' in html
    assert '<button class="is-link">Search</button>' in html
    assert 'form:has(input[name="terms-0-term"]) button:not([type])' in find_locators(compact_dom(search_form()))["submit"]
    draft = Agent1TestDesigner().design('搜索“GUI Agent”')
    contract = Agent2TestReviewer().build_contract(draft, target="fixture").to_dict()
    evidence = selenium_runtime.execute_test(contract, find_locators(compact_dom(search_form())),
        "http://example.test/search/advanced", tmp_path / "evidence.json")
    assert evidence["execution_status"] == "COMPLETE"
    assert "submit_clicked" in [s["stage"] for s in evidence["steps"]]
    assert any(s["stage"] == "submit_target" and s["button"]["text"] == "Search"
               for s in evidence["steps"])


def test_click_without_navigation_is_actionable_error(fake_selenium, tmp_path):
    fake_selenium.noop_submit = True
    draft = Agent1TestDesigner().design('搜索“GUI Agent”')
    contract = Agent2TestReviewer().build_contract(draft, target="fixture").to_dict()
    evidence = selenium_runtime.execute_test(contract, find_locators(compact_dom(search_form())),
        "http://example.test/search/advanced", tmp_path / "evidence.json")
    assert evidence["execution_status"] == "ERROR"
    assert evidence["error_stage"] == "wait_search_transition"
    assert "submit_clicked" in [s["stage"] for s in evidence["steps"]]
    assert (tmp_path / "failure_diagnostics.json").exists()
    assert "wait_search_transition" in evidence["error"]


def test_results_without_query_proof_are_not_automatic_pass(fake_selenium, tmp_path):
    fake_selenium.alt_results_url = True
    draft = Agent1TestDesigner().design('搜索“GUI Agent”')
    draft["query"]["open_first_result"] = False
    contract = Agent2TestReviewer().build_contract(draft, target="fixture").to_dict()
    evidence = selenium_runtime.execute_test(contract, find_locators(compact_dom(search_form())),
        "http://example.test/search/advanced", tmp_path / "evidence.json")
    assert evidence["execution_status"] == "COMPLETE"
    assert evidence["search"]["applied_keyword"] is None
    assert Agent5EvidenceVerifier().verify(contract, evidence).status == "INCONCLUSIVE"


def test_error_report_includes_failure_phase(fake_selenium, tmp_path):
    from web_agent.report import html_report
    fake_selenium.noop_submit = True
    draft = Agent1TestDesigner().design('搜索“GUI Agent”')
    contract = Agent2TestReviewer().build_contract(draft, target="fixture").to_dict()
    evidence = selenium_runtime.execute_test(contract, find_locators(compact_dom(search_form())),
        "http://example.test/search/advanced", tmp_path / "evidence.json")
    verdict = Agent5EvidenceVerifier().verify(contract, evidence).to_dict()
    report = html_report(tmp_path, contract, evidence, verdict)
    html = report.read_text(encoding="utf-8")
    assert "执行错误诊断（Agent4）" in html
    assert "wait_search_transition" in html
    assert "failure_diagnostics.json" in html
