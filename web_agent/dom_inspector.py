"""Read-only Selenium DOM Inspector (a *tool*, not Agent 7)."""
from __future__ import annotations

import os
from bs4 import BeautifulSoup
from .selenium_runtime import make_driver


def compact_dom(html: str, max_elements: int = 120) -> dict:
    """Reduce DOM to relevant public elements; do not send full page/credentials to LLM."""
    soup = BeautifulSoup(html, "html.parser")
    elements = []
    for tag in soup.select("input, select, button, textarea, a, form")[:max_elements]:
        if tag.name == "input" and tag.get("type", "").lower() in {"hidden", "password"}:
            continue
        elements.append({
            "tag": tag.name,
            "id": str(tag.get("id", ""))[:100],
            "name": str(tag.get("name", ""))[:100],
            "type": str(tag.get("type", ""))[:40],
            "aria_label": str(tag.get("aria-label", ""))[:120],
            "text": tag.get_text(" ", strip=True)[:120],
            "href_path": str(tag.get("href", "")).split("?")[0][:150],
        })
    return {"title": (soup.title.get_text(" ", strip=True) if soup.title else "")[:140],
            "elements": elements, "count": len(elements)}


def inspect_page(url: str, *, headless: bool = True) -> dict:
    driver = make_driver(headless=headless)
    try:
        driver.get(url)
        snapshot = compact_dom(driver.page_source)
        snapshot["url"] = driver.current_url
        return snapshot
    finally:
        driver.quit()
