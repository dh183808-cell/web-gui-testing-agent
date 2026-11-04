"""Agent 4 runtime: Selenium actions, phase-aware diagnostics and observable evidence.

The search submission is bound to the keyword input's owning HTML form. This
avoids accidentally clicking a similarly named Search control in the site header.
"""
from __future__ import annotations

import json
import os
import re
import time
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .evidence_plan import collect_checkpoint


def make_driver(*, headless: bool = True):
    """Import Selenium lazily so contract/generation tests work without a browser."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    opts = Options()
    opts.add_argument("--window-size=1440,1000")
    if headless:
        opts.add_argument("--headless=new")
    if os.name == "posix":
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
    if os.getenv("CHROME_BINARY"):
        opts.binary_location = os.environ["CHROME_BINARY"]
    opts.page_load_strategy = "normal"
    service = Service(os.environ["CHROMEDRIVER_PATH"]) if os.getenv("CHROMEDRIVER_PATH") else None
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(30)
    return driver


def normalize_id(url: str) -> str:
    path = urlparse(url).path
    m = re.search(r"/abs/([^?#]+)", path)
    return m.group(1).replace(".pdf", "") if m else ""


def _first_displayed(driver, css: list[str], *, timeout: int = 12):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    def locate(d):
        for selector in css:
            for node in d.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if node.is_displayed() and node.is_enabled():
                        return node
                except Exception:
                    continue
        return False

    return WebDriverWait(driver, timeout).until(locate)


def _control_description(node) -> dict:
    def read(call, default=""):
        try:
            value = call()
            return str(value or "")[:160]
        except Exception:
            return default
    return {
        "text": read(lambda: node.text),
        "type": read(lambda: node.get_attribute("type")),
        "aria_label": read(lambda: node.get_attribute("aria-label")),
        "value": read(lambda: node.get_attribute("value")),
        "outer_html": read(lambda: node.get_attribute("outerHTML"))[:220],
    }


def _form_submit(driver, keyword_node, candidates: list[str], *, timeout: int = 12):
    """Select a Search submit control *from the keyword field's form*.

    Forms commonly have multiple buttons, and the actual Search button can be
    <button>Search</button> (implicit submit). A page-wide button[type=submit]
    can match the unrelated header search. Never fall back to an unrelated form.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    def locate(d):
        try:
            form = keyword_node.find_element(By.XPATH, "./ancestor::form[1]")
            controls = form.find_elements(By.CSS_SELECTOR, 'button, input[type="submit"]')
        except Exception:
            # Some custom forms associate a control via form=ID rather than ancestry.
            # Only use candidates explicitly constrained to the advanced-search form.
            controls = []
            for selector in candidates:
                if "form" not in selector:
                    continue
                try:
                    controls.extend(d.find_elements(By.CSS_SELECTOR, selector))
                except Exception:
                    continue
        ranked = []
        for node in controls:
            try:
                if not node.is_displayed() or not node.is_enabled():
                    continue
                info = _control_description(node)
                label = (info["text"] or info["aria_label"] or info["value"]).strip().casefold()
                node_type = info["type"].casefold()
                if node_type in {"button", "reset"}:
                    continue
                if label == "search":
                    priority = 0
                elif label.startswith("search ") or label.endswith(" search"):
                    priority = 1
                elif node_type == "submit" and not label:
                    priority = 2
                else:
                    continue
                ranked.append((priority, node))
            except Exception:
                continue
        if not ranked:
            return False
        ranked.sort(key=lambda item: item[0])
        return ranked[0][1]

    return WebDriverWait(driver, timeout).until(locate)


def _has_no_results(driver) -> bool:
    from selenium.webdriver.common.by import By
    try:
        if driver.find_elements(By.CSS_SELECTOR, "#no-results, .no-results"):
            return True
        # Only an explicit empty-result message, not arbitrary text in help panels.
        text = driver.page_source.lower()
        return any(phrase in text for phrase in (
            "no results found", "no matching results", "no papers found"
        ))
    except Exception:
        return False


def _has_results(driver) -> bool:
    from selenium.webdriver.common.by import By
    try:
        return bool(driver.find_elements(By.CSS_SELECTOR, "li.arxiv-result")) or _has_no_results(driver)
    except Exception:
        return False


def _transition_state(driver):
    """A transition must be evidenced by query parameters or actual results DOM."""
    params = parse_qs(urlparse(driver.current_url).query, keep_blank_values=True)
    if "terms-0-term" in params:
        return "url_query"
    if _has_results(driver):
        return "results_dom"
    return False


def _failure_diagnostics(driver) -> dict:
    """Collect bounded, field-specific diagnostics, not a full personal DOM dump."""
    from selenium.webdriver.common.by import By
    out = {}
    try:
        out["url"] = driver.current_url
        out["title"] = str(driver.title)[:180]
    except Exception:
        pass
    try:
        out["headings"] = [x.text[:120] for x in driver.find_elements(By.CSS_SELECTOR, "h1, h2")[:6]]
    except Exception:
        pass
    try:
        out["result_count"] = len(driver.find_elements(By.CSS_SELECTOR, "li.arxiv-result"))
    except Exception:
        pass
    try:
        keyword_nodes = driver.find_elements(By.CSS_SELECTOR, '#terms-0-term, input[name="terms-0-term"]')
        if keyword_nodes:
            keyword = keyword_nodes[0]
            out["keyword_input_value"] = str(keyword.get_property("value") or "")[:150]
            form = keyword.find_element(By.XPATH, "./ancestor::form[1]")
            out["form_action"] = str(form.get_attribute("action") or "")[:230]
            out["form_buttons"] = [_control_description(e) for e in form.find_elements(
                By.CSS_SELECTOR, 'button, input[type="submit"]')[:8]]
    except Exception:
        pass
    return out


def _safe_screenshot(driver, path: Path) -> None:
    try:
        driver.save_screenshot(str(path))
    except Exception:
        pass


def execute_test(contract: dict, locators: dict, base_url: str, output: Path,
                 *, headless: bool = True, demo_delay: float = 0.0) -> dict:
    """One low-frequency public search task, without login, downloads or crawling."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select, WebDriverWait

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = {
        "execution_status": "RUNNING", "target": contract["target"],
        "search": {}, "results": {}, "detail": {}, "steps": [],
        "screenshots": [], "error": None, "error_stage": None,
        "collection_log": [], "evidence_plan_version": 1,
    }
    driver = None
    phase = "start"
    demo_delay = min(3.0, max(0.0, float(demo_delay)))
    plan = contract.get("evidence_plan", [])
    shot_prefix = "attempt2_" if output.stem.endswith("attempt2") else ""

    def demo_pause(element=None):
        if demo_delay <= 0 or headless or driver is None:
            return
        if element is not None:
            try:
                driver.execute_script("arguments[0].style.outline='3px solid #ea8b4b'", element)
            except Exception:
                pass
        time.sleep(demo_delay)

    def record(stage: str, **values) -> None:
        evidence["steps"].append({"stage": stage, **values})

    def capture(name: str) -> None:
        if driver is None:
            return
        path = output.parent / (shot_prefix + name + ".png")
        _safe_screenshot(driver, path)
        if path.exists():
            evidence["screenshots"].append(path.name)

    try:
        phase = "browser_launch"
        driver = make_driver(headless=headless)
        phase = "navigate"
        driver.get(base_url)
        record("navigate", url=driver.current_url)
        demo_pause()
        phase = "locate_keyword"
        kw_node = _first_displayed(driver, locators["keyword"])
        phase = "fill_keyword"
        kw_node.clear()
        kw_node.send_keys(contract["query"]["keyword"])
        entered_keyword = kw_node.get_property("value")
        record("fill", keyword=entered_keyword)
        demo_pause(kw_node)
        if contract["query"]["search_field"] != "all":
            phase = "set_search_field"
            field = _first_displayed(driver, locators["field"], timeout=4)
            Select(field).select_by_value(contract["query"]["search_field"])
        if contract["query"].get("computer_science"):
            phase = "set_cs_filter"
            cb = _first_displayed(driver, locators["computer_science"], timeout=4)
            if not cb.is_selected():
                cb.click()
        evidence["search"]["entered_keyword"] = entered_keyword
        evidence["current_url"] = driver.current_url
        collect_checkpoint(evidence, plan, "form_filled", capture=capture,
                           screenshot_name="01_search_form")
        phase = "locate_search_submit"
        button = _form_submit(driver, kw_node, locators.get("submit", []), timeout=12)
        button_info = _control_description(button)
        record("submit_target", button=button_info)
        demo_pause(button)
        phase = "click_search_submit"
        button.click()
        record("submit_clicked", url=driver.current_url)
        phase = "wait_search_transition"
        transition = WebDriverWait(driver, 22).until(_transition_state)
        record("search_transition", signal=transition, url=driver.current_url)
        params = parse_qs(urlparse(driver.current_url).query, keep_blank_values=True)
        # Unknown is distinct from an observed negative: it must never satisfy an oracle.
        applied = params.get("terms-0-term", [None])[0]
        applied_field = params.get("terms-0-field", [None])[0]
        applied_cs = (
            params.get("classification-computer_science", [""])[0].lower() in {"y", "on", "true", "1"}
            if "classification-computer_science" in params else None
        )
        evidence["search"] = {
            "entered_keyword": entered_keyword, "applied_keyword": applied,
            "applied_field": applied_field, "applied_computer_science": applied_cs,
            "results_url": driver.current_url, "submission_signal": transition,
        }
        record("search_submitted", url=driver.current_url, applied_keyword=applied)
        evidence["current_url"] = driver.current_url
        collect_checkpoint(evidence, plan, "search_submitted")
        phase = "wait_results_loaded"
        WebDriverWait(driver, 22).until(_has_results)
        demo_pause()
        items = driver.find_elements(By.CSS_SELECTOR, "li.arxiv-result")
        evidence["results"]["count"] = len(items)
        if not contract["query"].get("open_first_result", True):
            evidence["current_url"] = driver.current_url
            collect_checkpoint(evidence, plan, "results_loaded", capture=capture,
                               screenshot_name="02_search_results")
            record("results_loaded", count=len(items), mode="search_only")
            evidence["execution_status"] = "COMPLETE"
            return evidence
        if not items:
            evidence["current_url"] = driver.current_url
            collect_checkpoint(evidence, plan, "results_loaded", capture=capture,
                               screenshot_name="02_search_results")
            evidence["execution_status"] = "INCONCLUSIVE"
            record("results_loaded", note="no result; cannot verify paper details")
            return evidence
        phase = "read_first_result"
        first = items[0]
        try:
            list_title = first.find_element(By.CSS_SELECTOR, "p.title").text
        except Exception:
            list_title = ""
        links = first.find_elements(By.CSS_SELECTOR, 'a[href*="/abs/"]')
        if not links:
            raise RuntimeError("result item lacks an abstract link")
        link = links[0]
        paper_url = link.get_attribute("href") or ""
        evidence["results"].update({
            "title": list_title, "arxiv_id": normalize_id(paper_url), "paper_url": paper_url
        })
        record("results_loaded", **evidence["results"])
        evidence["current_url"] = driver.current_url
        collect_checkpoint(evidence, plan, "results_loaded", capture=capture,
                           screenshot_name="02_search_results")
        phase = "click_paper_link"
        demo_pause(link)
        link.click()
        phase = "wait_paper_detail"
        WebDriverWait(driver, 18).until(lambda d: "/abs/" in urlparse(d.current_url).path)
        phase = "read_paper_detail"
        detail_title = _first_displayed(driver, ["h1.title", "h1#title", "h1"], timeout=12).text
        detail_title = re.sub(r"^\s*Title\s*:\s*", "", detail_title, flags=re.I)
        evidence["detail"] = {
            "title": detail_title, "arxiv_id": normalize_id(driver.current_url),
            "detail_url": driver.current_url,
        }
        evidence["current_url"] = driver.current_url
        collect_checkpoint(evidence, plan, "detail_loaded", capture=capture,
                           screenshot_name="03_paper_detail")
        demo_pause()
        record("detail_loaded", **evidence["detail"])
        evidence["execution_status"] = "COMPLETE"
        return evidence
    except Exception as exc:
        evidence["execution_status"] = "ERROR"
        evidence["error_stage"] = phase
        error_name = type(exc).__name__
        labels = {
            "locate_search_submit": "未在关键词输入框所属表单内找到可点击的 Search 提交按钮",
            "wait_search_transition": "点击 Search 后，未观察到有效搜索 URL 或结果 DOM",
            "wait_results_loaded": "已提交搜索，但未出现可识别的结果列表或空结果提示",
            "wait_paper_detail": "点击论文后未进入摘要详情页面",
        }
        detail = labels.get(phase, "执行当前浏览器步骤失败")
        evidence["error"] = f"{error_name} [{phase}]: {detail}; {str(exc)[:230]}"
        evidence["traceback_tail"] = traceback.format_exc()[-4500:]
        record("exception", phase=phase, error=evidence["error"])
        if driver is not None:
            evidence["diagnostics"] = _failure_diagnostics(driver)
            (output.parent / "failure_diagnostics.json").write_text(
                json.dumps(evidence["diagnostics"], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            capture("99_execution_error")
        return evidence
    finally:
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        if driver is not None:
            driver.quit()
