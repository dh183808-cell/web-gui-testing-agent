"""Agent2 -> Agent4 Evidence Plan interpreter.

A plan determines *when* evidence is collected. Any evidence outside the
allowlist stays out of the exchange, and missing fields remain explicit.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse
from typing import Any, Callable

ALLOWED_FIELDS = frozenset({
    'search.entered_keyword', 'search.applied_keyword', 'search.applied_field',
    'search.applied_computer_science', 'results.count', 'results.title',
    'results.arxiv_id', 'detail.title', 'detail.arxiv_id', 'detail.detail_url',
    'current_url', 'screenshot',
})


def get_path(document: dict[str, Any], dotted: str) -> Any:
    current: Any = document
    for field in dotted.split('.'):
        if not isinstance(current, dict) or field not in current:
            return None
        current = current[field]
    return current


def collect_checkpoint(
    evidence: dict[str, Any], plan: list[dict[str, Any]], checkpoint: str,
    *, capture: Callable[[str], None] | None = None, screenshot_name: str = '',
) -> dict[str, Any]:
    """Collect exactly plan-requested evidence; record absence rather than invent it.

    The evidence already contains observed Selenium values at this checkpoint.
    This function records acquisition status and takes a screenshot only if requested.
    """
    entries = [p for p in plan if p.get('checkpoint') == checkpoint]
    requested: list[str] = []
    for entry in entries:
        for name in entry.get('collect', []):
            if name not in ALLOWED_FIELDS:
                raise ValueError(f'Unsupported evidence field: {name}')
            if name not in requested:
                requested.append(name)
    observed: dict[str, Any] = {}
    missing: list[str] = []
    for name in requested:
        if name == 'screenshot':
            if capture and screenshot_name:
                before = set(evidence.get('screenshots', []))
                capture(screenshot_name)
                captured = sorted(set(evidence.get('screenshots', [])) - before)
                if captured:
                    observed[name] = captured[0]
                else:
                    missing.append(name)
            else:
                missing.append(name)
        else:
            val = get_path(evidence, name)
            if val is None or (isinstance(val, str) and not val.strip()):
                missing.append(name)
            else:
                observed[name] = val
    event = {'checkpoint': checkpoint, 'requested': requested,
             'observed': observed, 'missing': missing}
    evidence.setdefault('collection_log', []).append(event)
    return event


def normalize_id(url: str) -> str:
    path = urlparse(url).path
    m = re.search(r"/abs/([^?#]+)", path)
    return m.group(1).replace(".pdf", "") if m else ""
