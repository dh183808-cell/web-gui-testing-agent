"""Pure, bounded detail-probe planning shared by verification and probe validation."""
from __future__ import annotations

from typing import Any

from .evidence_plan import normalize_id

RECOVERABLE_EVIDENCE = frozenset({'detail.title', 'detail.arxiv_id'})


def make_probe_request(contract: dict[str, Any], evidence: dict[str, Any], *, missing: list[str]) -> dict[str, Any] | None:
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
