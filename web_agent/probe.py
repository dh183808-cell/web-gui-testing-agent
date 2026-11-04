"""Agent5's targeted, bounded GUI re-observation.

This is not a second full search. It navigates only to the recorded paper
abstract page, never overwrites previously observed nonempty values, and
never interprets untrusted page content as instructions.
"""
from __future__ import annotations

import json
import re
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from .selenium_runtime import make_driver, normalize_id, _first_displayed

RECOVERABLE_EVIDENCE = frozenset({'detail.title', 'detail.arxiv_id'})


def make_probe_request(contract: dict, evidence: dict, *, missing: list[str]) -> dict | None:
    """Ask for a specific detail-field read only if the app run completed."""
    if evidence.get('execution_status') != 'COMPLETE':
        return None
    wanted = sorted(set(missing) & RECOVERABLE_EVIDENCE)
    if not wanted or any(f not in RECOVERABLE_EVIDENCE for f in missing):
        return None
    url = evidence.get('detail', {}).get('detail_url') or evidence.get('results', {}).get('paper_url')
    if not isinstance(url, str) or not normalize_id(url):
        return None
    expected_id = evidence.get('results', {}).get('arxiv_id')
    if not expected_id or normalize_id(url) != expected_id:
        return None
    return {
        'type': 'detail_reobserve', 'url': url, 'fields': wanted,
        'expected_id': expected_id, 'target': contract['target'],
        'reason': 'Missing required detail observation for test oracle',
    }


def _validate_request(request: dict, base_url: str, *, allow_live: bool = False) -> None:
    if request.get('type') != 'detail_reobserve':
        raise ValueError('Only detail_reobserve is allowed')
    if not set(request.get('fields', [])) <= RECOVERABLE_EVIDENCE or not request.get('fields'):
        raise ValueError('Unsupported probe fields')
    url, base = urlparse(request['url']), urlparse(base_url)
    if url.scheme not in {'http', 'https'} or url.netloc != base.netloc or url.scheme != base.scheme:
        raise ValueError('Probe URL must have the same origin as the tested site')
    if not re.fullmatch(r'/abs/[A-Za-z0-9.]+', url.path):
        raise ValueError('Probe only supports an arXiv abstract detail page')
    target = request.get('target')
    if target == 'arxiv':
        if not allow_live or url.scheme != 'https' or url.hostname != 'arxiv.org':
            raise ValueError('Live probe needs explicit authorization and arxiv.org origin')
    elif target in {'fixture', 'fixture-bug'}:
        if url.hostname not in {'127.0.0.1', 'localhost'}:
            raise ValueError('Fixture probe must stay on loopback')
    else:
        raise ValueError('Unsupported target')
    if normalize_id(request['url']) != request['expected_id']:
        raise ValueError('Probe target ID differs from original result')


def execute_probe(request: dict, base_url: str, output: Path, *, headless=True,
                  demo_delay: float = 0, allow_live=False) -> dict:
    from selenium.webdriver.support.ui import WebDriverWait
    _validate_request(request, base_url, allow_live=allow_live)
    output = Path(output)
    result: dict[str, Any] = {'status': 'RUNNING', 'fields': {}, 'steps': [], 'screenshots': [], 'error': None}
    driver = None
    try:
        driver = make_driver(headless=headless)
        driver.get(request['url'])
        if urlparse(driver.current_url).netloc != urlparse(base_url).netloc:
            raise ValueError('Probe unexpectedly navigated outside the approved origin')
        WebDriverWait(driver, 15).until(lambda d: normalize_id(d.current_url) == request['expected_id'])
        vals: dict[str, str] = {}
        if 'detail.title' in request['fields']:
            title = _first_displayed(driver, ['h1.title', 'h1#title', 'h1'], timeout=10).text
            vals['title'] = re.sub(r'^\s*Title\s*:\s*', '', title, flags=re.I).strip()
        if 'detail.arxiv_id' in request['fields']:
            vals['arxiv_id'] = normalize_id(driver.current_url)
        result['fields'] = {'detail': vals}
        result['steps'].append({'stage': 'reobserve_detail', 'url': driver.current_url,
                                'observed': sorted(vals)})
        if not headless and demo_delay:
            time.sleep(min(2, max(0, float(demo_delay))))
        shot = output.with_name('probe_01_detail.png')
        try:
            if driver.save_screenshot(str(shot)) and shot.exists():
                result['screenshots'].append(shot.name)
        except Exception:
            pass
        result['status'] = 'COMPLETE' if all(vals.get(f.split('.')[1]) for f in request['fields']) else 'INCONCLUSIVE'
    except Exception as exc:
        result['status'] = 'ERROR'
        result['error'] = f'{type(exc).__name__}: {str(exc)[:200]}'
        result['traceback_tail'] = traceback.format_exc()[-1600:]
    finally:
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        if driver:
            driver.quit()
    return result


def merge_probe(evidence: dict, probe: dict, request: dict) -> dict:
    """Fill only absent keys, never replace a conflicting observed value."""
    if probe.get('status') != 'COMPLETE':
        evidence.setdefault('probes', []).append({'request': request, 'status': probe.get('status'),
                                                  'error': probe.get('error')})
        return evidence
    added: dict[str, Any] = {}
    for field in request['fields']:
        group, key = field.split('.')
        observed = probe.get('fields', {}).get(group, {}).get(key)
        old = evidence.get(group, {}).get(key)
        if observed is not None and str(observed).strip() and not old:
            evidence.setdefault(group, {})[key] = observed
            added[field] = observed
    evidence.setdefault('probes', []).append({'request': request, 'status': 'COMPLETE', 'added': added})
    evidence.setdefault('screenshots', []).extend(x for x in probe.get('screenshots', [])
                                                  if x not in evidence.get('screenshots', []))
    evidence.setdefault('steps', []).extend(probe.get('steps', []))
    evidence.setdefault('collection_log', []).append({
        'checkpoint': 'active_reobserve', 'requested': request['fields'],
        'observed': added, 'missing': sorted(set(request['fields']) - set(added)),
    })
    return evidence
