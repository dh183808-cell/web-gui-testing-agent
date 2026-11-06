"""Versioned dynamic evidence, closed oracle registry and read-only probe protocol.

References resolve by declared IDs only. No eval, dotted Python expressions or
website field allowlist. Provenance binding is an integrity check inside the
trusted local pipeline, not a signature for accepting external evidence.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re

from .dynamic_execution import ExecutionRejected, step_for
from .schema import Verdict

EVIDENCE_VERSION = '0.4.1'
READERS = {'text': 'text', 'value': 'value', 'url': 'url', 'boolean': 'checked',
           'count': 'count', 'number': 'number', 'exists': 'exists', 'visible': 'visible',
           'enabled': 'enabled', 'selected': 'checked', 'page_title': 'page_title', 'file': 'file'}
READ_ONLY = frozenset({'observe', 'wait'})
BOOLEAN_KINDS = {'boolean', 'exists', 'visible', 'enabled', 'selected'}


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def artifact_path(root, relative):
    if type(relative) is not str or not relative or Path(relative).is_absolute() or '\\' in relative:
        raise ValueError('Artifact must be a relative portable path')
    base = Path(root).resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base) or path == base:
        raise ValueError('Artifact escaped run directory')
    return path


def make_record(contract, requirement, *, value=None, status='observed', reason=None,
                attempt=1, collection_attempt=1, url, observation_id, session_id,
                source_sha256, artifacts=(), source='selenium'):
    return {'evidence_version': EVIDENCE_VERSION, 'evidence_id': requirement.evidence_id,
            'step_id': requirement.step_id, 'output': requirement.output, 'kind': requirement.kind,
            'value': value, 'status': status, 'reason': reason, 'attempt': attempt,
            'collection_attempt': collection_attempt, 'collected_at': timestamp(), 'source': source,
            'url': url, 'page_state': observation_id, 'session_id': session_id,
            'contract_digest': contract.design_digest, 'source_sha256': source_sha256,
            'artifacts': list(artifacts)}


def validate_record(contract, requirement, record, *, run_dir=None):
    """Reject malformed/mismatched records; empty text, zero and False are values."""
    keys = {'evidence_version', 'evidence_id', 'step_id', 'output', 'kind', 'value', 'status',
            'reason', 'attempt', 'collection_attempt', 'collected_at', 'source', 'url', 'page_state',
            'session_id', 'contract_digest', 'source_sha256', 'artifacts'}
    if type(record) is not dict or set(record) != keys:
        raise ValueError('Invalid evidence envelope')
    if (record['evidence_version'] != EVIDENCE_VERSION or record['contract_digest'] != contract.design_digest or
            any(record[key] != getattr(requirement, key) for key in ('evidence_id', 'step_id', 'output', 'kind'))):
        raise ValueError('Evidence does not match the frozen requirement')
    for key in ('attempt', 'collection_attempt'):
        if type(record[key]) is not int or not 1 <= record[key] <= 100:
            raise ValueError('Invalid evidence attempt')
    if record['status'] not in ('observed', 'missing', 'failed') or record['source'] not in ('selenium', 'probe', 'download'):
        raise ValueError('Invalid evidence status/source')
    for key in ('url', 'page_state', 'session_id'):
        if type(record[key]) is not str or not 1 <= len(record[key]) <= 3000:
            raise ValueError('Missing evidence provenance')
    from .dynamic_execution import origin
    if origin(record['url']) != origin(contract.design.target_url):
        raise ValueError('Evidence page is outside the reviewed origin')
    instant = datetime.fromisoformat(record['collected_at'])
    if instant.tzinfo is None:
        raise ValueError('Evidence timestamp must have timezone')
    if not re.fullmatch('[a-f0-9]{64}', record['source_sha256']):
        raise ValueError('Missing generated source digest')
    if type(record['artifacts']) is not list or len(record['artifacts']) > 5:
        raise ValueError('Invalid artifact references')
    for relative in record['artifacts']:
        path = artifact_path(run_dir or '.', relative)
        if run_dir and not path.is_file():
            raise ValueError('Evidence artifact unavailable')
    if record['status'] != 'observed':
        if type(record['reason']) is not str or not record['reason'] or record['value'] is not None:
            raise ValueError('Missing evidence needs a reason and no value')
        return
    value, kind = record['value'], requirement.kind
    if kind in BOOLEAN_KINDS and type(value) is not bool:
        raise ValueError('Boolean evidence must be boolean')
    if kind == 'count' and (type(value) is not int or value < 0):
        raise ValueError('Count evidence must be nonnegative integer')
    if kind == 'number':
        numeric(value)
    if kind in ('text', 'value', 'url', 'page_title') and (type(value) is not str or len(value) > 2000):
        raise ValueError('Text evidence must be bounded text')
    if kind == 'file':
        if type(value) is not dict or set(value) != {'path', 'size', 'sha256', 'complete'}:
            raise ValueError('Invalid file evidence')
        artifact_path(run_dir or '.', value['path'])
        if (type(value['size']) is not int or value['size'] < 0 or type(value['complete']) is not bool or
                not re.fullmatch('[a-f0-9]{64}', value['sha256']) or value['path'] not in record['artifacts']):
            raise ValueError('Invalid file metadata')


def numeric(value):
    if type(value) not in (int, float, str) or isinstance(value, bool):
        raise ValueError('Expected a number, not boolean/container')
    if len(str(value)) > 2000:
        raise ValueError('Number exceeds evidence size limit')
    if type(value) is str and not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', value.strip()):
        raise ValueError('Invalid numeric text')
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError('Invalid number') from exc
    if not number.is_finite():
        raise ValueError('Non-finite number')
    return number


def normalize(value):
    return re.sub(r'\s+', ' ', value).strip().casefold()


def file_valid(value, run_dir):
    if run_dir is None:
        raise ValueError('File validation needs trusted artifact root')
    path = artifact_path(run_dir, value['path'])
    if not path.is_file():
        raise ValueError('Downloaded file is unavailable')
    if path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError('Downloaded file exceeds validation limit')
    data = path.read_bytes()
    if len(data) != value['size'] or hashlib.sha256(data).hexdigest() != value['sha256']:
        raise ValueError('Downloaded file changed after collection')
    if path.suffix.lower() in ('.crdownload', '.part', '.tmp') or not value['complete'] or not data:
        return False
    # Conservative, deterministic basic checks, not domain-specific content QA.
    if path.suffix.lower() == '.pdf':
        return data.startswith(b'%PDF-') and b'%%EOF' in data[-2048:]
    if path.suffix.lower() == '.json':
        try:
            json.loads(data)
        except (ValueError, UnicodeError):
            return False
    return True


def _compare_ordered(relation, actual, expected):
    left, right = numeric(actual), numeric(expected)
    return {'greater_than': left > right, 'less_than': left < right,
            'greater_or_equal': left >= right, 'less_or_equal': left <= right}[relation]


def _compare_text(relation, actual, expected):
    if type(actual) is not str or type(expected) is not str:
        raise ValueError('Text comparison needs two strings')
    return normalize(actual) == normalize(expected) if relation == 'normalized_equals' else expected in actual


def _boolean_equal(actual, expected):
    if type(actual) is not bool or type(expected) is not bool:
        raise ValueError('Boolean comparison needs two booleans')
    return actual == expected


# Application-owned registry: adding a relation requires a reviewed handler,
# matching schema validation and tests. Contracts cannot supply callable code.
ORACLE_EVALUATORS = {
    'equals': lambda a, b, root: type(a) is type(b) and a == b,
    'not_equals': lambda a, b, root: not (type(a) is type(b) and a == b),
    'normalized_equals': lambda a, b, root: _compare_text('normalized_equals', a, b),
    'contains': lambda a, b, root: _compare_text('contains', a, b),
    'boolean_equals': lambda a, b, root: _boolean_equal(a, b),
    'greater_than': lambda a, b, root: _compare_ordered('greater_than', a, b),
    'less_than': lambda a, b, root: _compare_ordered('less_than', a, b),
    'greater_or_equal': lambda a, b, root: _compare_ordered('greater_or_equal', a, b),
    'less_or_equal': lambda a, b, root: _compare_ordered('less_or_equal', a, b),
    'file_valid': lambda a, b, root: file_valid(a, root) == b,
}


def compare(relation, actual, expected, run_dir):
    handler = ORACLE_EVALUATORS.get(relation)
    if handler is None:
        raise ValueError('Unsupported dynamic oracle')
    return handler(actual, expected, run_dir)


def verify_dynamic(contract, evidence, *, execution_status='COMPLETE', error=None, run_dir=None):
    contract.assert_integrity()
    requirements = {e.evidence_id: e for e in contract.design.evidence_requirements}
    data = {d.data_id: d.value for d in contract.design.test_data}
    checks = []
    def resolve(ref):
        if ref.kind == 'literal':
            return ref.value
        if ref.kind == 'test_data':
            return data[ref.ref_id]
        if ref.kind != 'evidence':
            raise ValueError('Unsupported oracle reference')
        record = evidence.get(ref.ref_id)
        validate_record(contract, requirements[ref.ref_id], record, run_dir=run_dir)
        if record['status'] != 'observed':
            raise ValueError(record['reason'])
        return record['value']
    for oracle in contract.design.oracles:
        actual = expected = None
        reason = None
        try:
            actual, expected = resolve(oracle.actual), resolve(oracle.expected)
            passed = compare(oracle.relation, actual, expected, run_dir)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            passed, reason = None, str(exc)[:250]
        checks.append({'oracle_id': oracle.oracle_id, 'step_id': oracle.after_step_id,
            'evidence_ids': list(oracle.evidence_ids), 'relation': oracle.relation,
            'passed': passed, 'actual': actual, 'expected': expected, 'reason': reason,
            'evidence_attempts': {eid: evidence[eid].get('attempt') for eid in oracle.evidence_ids
                                  if type(evidence.get(eid)) is dict}})
    # A confirmed failure is terminal even when other observations are missing.
    if any(c['passed'] is False for c in checks):
        return Verdict('FAIL', '可信证据确认至少一个 Oracle 不满足', checks)
    if execution_status == 'ERROR':
        return Verdict('ERROR', '执行错误阻止正常验证', checks)
    if execution_status != 'COMPLETE' or not checks or any(c['passed'] is None for c in checks):
        return Verdict('INCONCLUSIVE', '必要证据不足，未确认功能失败', checks)
    return Verdict('PASS', '所有必要 Oracle 均有可信证据且通过', checks)


def plan_dynamic_probe(contract, evidence, verdict, *, step_id=None):
    if verdict.status != 'INCONCLUSIVE':
        return None
    missing_oracles = {c['oracle_id'] for c in verdict.checks if c['passed'] is None}
    for requirement in contract.design.evidence_requirements:
        if step_id and requirement.step_id != step_id:
            continue
        oracle_ids = [o.oracle_id for o in contract.design.oracles
                      if o.oracle_id in missing_oracles and requirement.evidence_id in o.evidence_ids]
        if not oracle_ids or requirement.kind == 'file' or step_for(contract, requirement.step_id).action not in READ_ONLY:
            continue
        old = evidence.get(requirement.evidence_id)
        try:
            validate_record(contract, requirement, old)
            if old['status'] == 'observed':
                continue
        except (ValueError, TypeError, KeyError):
            pass
        return {'type': 'dynamic_read', 'contract_digest': contract.design_digest,
                'step_id': requirement.step_id, 'evidence_ids': [requirement.evidence_id],
                'oracle_ids': oracle_ids, 'side_effect': 'read_only',
                'reason': 'Recollect missing declared evidence at its original checkpoint'}
    return None


def validate_probe(contract, request, *, current_step_id):
    if type(request) is not dict or set(request) != {'type', 'contract_digest', 'step_id',
            'evidence_ids', 'oracle_ids', 'side_effect', 'reason'}:
        raise ExecutionRejected('Invalid probe envelope')
    if (request['type'] != 'dynamic_read' or request['contract_digest'] != contract.design_digest or
            request['side_effect'] != 'read_only' or request['step_id'] != current_step_id):
        raise ExecutionRejected('Probe is not read-only at the original checkpoint')
    step_for(contract, request['step_id'])
    if step_for(contract, request['step_id']).action not in READ_ONLY:
        raise ExecutionRejected('Probe cannot repeat business action')
    requirements = {e.evidence_id: e for e in contract.design.evidence_requirements}
    oracles = {o.oracle_id: o for o in contract.design.oracles}
    if (type(request['evidence_ids']) is not list or not request['evidence_ids'] or
            type(request['oracle_ids']) is not list or not request['oracle_ids']):
        raise ExecutionRejected('Probe must reference original evidence and oracles')
    for eid in request['evidence_ids']:
        if eid not in requirements or requirements[eid].step_id != current_step_id or requirements[eid].kind == 'file':
            raise ExecutionRejected('Probe evidence outside reviewed checkpoint')
        if not any(oid in oracles and eid in oracles[oid].evidence_ids for oid in request['oracle_ids']):
            raise ExecutionRejected('Probe oracle/evidence association invalid')
    if any(oid not in oracles or not set(oracles[oid].evidence_ids) & set(request['evidence_ids'])
           for oid in request['oracle_ids']):
        raise ExecutionRejected('Probe references unrelated oracle')


def merge_dynamic(contract, evidence, incoming, *, allowed_ids=None, run_dir=None):
    """Fill holes only; never replace reliable evidence, even with a newer value."""
    added = []
    requirements = {e.evidence_id: e for e in contract.design.evidence_requirements}
    for eid, record in incoming.items():
        if eid not in requirements or allowed_ids is not None and eid not in allowed_ids:
            raise ExecutionRejected('Unexpected evidence ID in collection result')
        validate_record(contract, requirements[eid], record, run_dir=run_dir)
        old = evidence.get(eid)
        try:
            validate_record(contract, requirements[eid], old, run_dir=run_dir)
            reliable = old['status'] == 'observed'
        except (ValueError, TypeError, KeyError, OSError):
            reliable = False
        if not reliable:
            evidence[eid] = record
            added.append(eid)
    return added
