"""v0.4 semantic planning protocol; no browser, locator or model dependency.

All nested values are frozen records/tuples/scalars. from_dict is the strict
JSON boundary; unknown keys, coercions, dangling references and cycles fail.
IDs are semantic names (S_search, O_title, E_title); requirement IDs are derived
from the original requirement and its exact source span, stable on revision.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any
from urllib.parse import urlsplit

PROTOCOL_VERSION = '0.4'
ACTIONS = ('navigate', 'fill', 'click', 'select', 'check', 'submit', 'observe', 'wait', 'download')
RELATIONS = ('equals', 'not_equals', 'contains', 'greater_than', 'less_than',
             'greater_or_equal', 'less_or_equal', 'normalized_equals', 'boolean_equals', 'file_valid')
EVIDENCE_KINDS = ('text', 'url', 'value', 'boolean', 'count', 'screenshot',
                  'number', 'exists', 'visible', 'enabled', 'selected', 'page_title', 'file')


def _object(value: Any, keys: str, where: str) -> dict:
    if type(value) is not dict or set(value) != set(keys.split()):
        raise ValueError(f'{where}: expected exactly these fields: {keys}')
    return value


def _text(value: Any, where: str, *, empty: bool = False) -> str:
    if type(value) is not str or len(value) > 20000 or (not empty and not value.strip()):
        raise ValueError(f'{where}: expected nonempty text')
    return value


def _choice(value: Any, options: tuple[str, ...], where: str) -> str:
    if type(value) is not str or value not in options:
        raise ValueError(f'{where}: expected one of {options}')
    return value


def _items(value: Any, where: str) -> list:
    if type(value) is not list or len(value) > 100:
        raise ValueError(f'{where}: expected array with at most 100 entries')
    return value


def _strings(value: Any, where: str) -> tuple[str, ...]:
    result = tuple(_text(v, where) for v in _items(value, where))
    if len(set(result)) != len(result):
        raise ValueError(f'{where}: duplicate values')
    return result


def _id(value: Any, prefix: str) -> str:
    if type(value) is not str or not re.fullmatch(prefix + r'_[A-Za-z0-9_]{1,64}', value):
        raise ValueError(f'Invalid {prefix} ID: {value!r}')
    return value


def _scalar(value: Any) -> str | int | float | bool:
    if type(value) not in (str, int, float, bool) or (type(value) is float and not math.isfinite(value)):
        raise ValueError('Data values must be finite JSON scalars (no null/containers)')
    if type(value) is str:
        _text(value, 'value')
    return value


def validate_target_url(value: Any) -> str:
    value = _text(value, 'target_url')
    try:
        url = urlsplit(value)
        url.port  # Validate the optional port without network access.
    except ValueError as exc:
        raise ValueError('Invalid target_url') from exc
    if (url.scheme not in ('https', 'http') or not url.hostname or url.username is not None
            or url.password is not None or any(c.isspace() for c in value)):
        raise ValueError('target_url must be an HTTP(S) URL without credentials')
    return value


def requirement_id(requirement: str, source_quote: str) -> str:
    """Application-owned identity for an exact requirement span across revisions."""
    return 'R_' + hashlib.sha256((requirement + '\0' + source_quote).encode()).hexdigest()[:16]


def _quote(value: Any, raw: str, *, unknown: bool = False) -> str:
    quote = _text(value, 'source_quote', empty=unknown)
    if quote and quote not in raw:
        raise ValueError('source_quote must be an exact span of the original requirement')
    return quote


@dataclass(frozen=True)
class Requirement:
    requirement_id: str
    text: str
    source_quote: str
    kind: str

    def to_dict(self) -> dict:
        return dict(vars(self))


@dataclass(frozen=True)
class SemanticTarget:
    description: str
    role: str
    position: int | None

    def to_dict(self) -> dict:
        return dict(vars(self))


@dataclass(frozen=True)
class ValueReference:
    """kind: literal, test_data, step_output or evidence; never executable text."""
    kind: str
    value: str | int | float | bool = ''
    ref_id: str = ''
    output: str = ''

    @classmethod
    def from_dict(cls, value: Any) -> ValueReference:
        if type(value) is not dict:
            raise ValueError('Reference must be an object')
        kind = _choice(value.get('kind'), ('literal', 'test_data', 'step_output', 'evidence'), 'reference.kind')
        if kind == 'literal':
            _object(value, 'kind value', 'literal')
            return cls(kind, value=_scalar(value['value']))
        if kind == 'step_output':
            _object(value, 'kind step_id output', 'step_output')
            return cls(kind, ref_id=_id(value['step_id'], 'S'), output=_text(value['output'], 'output'))
        field, prefix = ('data_id', 'D') if kind == 'test_data' else ('evidence_id', 'E')
        _object(value, f'kind {field}', kind)
        return cls(kind, ref_id=_id(value[field], prefix))

    def to_dict(self) -> dict:
        if self.kind == 'literal':
            return {'kind': self.kind, 'value': self.value}
        if self.kind == 'step_output':
            return {'kind': self.kind, 'step_id': self.ref_id, 'output': self.output}
        return {'kind': self.kind, 'data_id' if self.kind == 'test_data' else 'evidence_id': self.ref_id}


@dataclass(frozen=True)
class Precondition:
    precondition_id: str
    description: str
    status: str
    source_quote: str

    def to_dict(self) -> dict:
        return dict(vars(self))


@dataclass(frozen=True)
class TestDatum:
    data_id: str
    value: str | int | float | bool
    source_quote: str

    def to_dict(self) -> dict:
        return dict(vars(self))


@dataclass(frozen=True)
class SemanticStep:
    step_id: str
    requirement_ids: tuple[str, ...]
    action: str
    target: SemanticTarget
    depends_on: tuple[str, ...]
    precondition_ids: tuple[str, ...]
    inputs: tuple[tuple[str, ValueReference], ...]
    outputs: tuple[str, ...]

    def to_dict(self) -> dict:
        return {'step_id': self.step_id, 'requirement_ids': list(self.requirement_ids),
                'action': self.action, 'target': self.target.to_dict(),
                'depends_on': list(self.depends_on), 'precondition_ids': list(self.precondition_ids),
                'inputs': {k: v.to_dict() for k, v in self.inputs}, 'outputs': list(self.outputs)}


@dataclass(frozen=True)
class EvidenceRequirement:
    evidence_id: str
    step_id: str
    output: str
    description: str
    kind: str

    def to_dict(self) -> dict:
        return dict(vars(self))


@dataclass(frozen=True)
class SemanticOracle:
    oracle_id: str
    requirement_ids: tuple[str, ...]
    after_step_id: str
    relation: str
    actual: ValueReference
    expected: ValueReference
    evidence_ids: tuple[str, ...]

    def to_dict(self) -> dict:
        return {'oracle_id': self.oracle_id, 'requirement_ids': list(self.requirement_ids),
                'after_step_id': self.after_step_id, 'relation': self.relation,
                'actual': self.actual.to_dict(), 'expected': self.expected.to_dict(),
                'evidence_ids': list(self.evidence_ids)}


DESIGN_FIELDS = ('protocol_version document_type requirement target_url test_goal requirements '
                 'steps preconditions test_data oracles evidence_requirements assumptions '
                 'missing_information unsupported_requests')


@dataclass(frozen=True)
class TestDesign:
    requirement: str
    target_url: str
    test_goal: str
    requirements: tuple[Requirement, ...]
    steps: tuple[SemanticStep, ...]
    preconditions: tuple[Precondition, ...]
    test_data: tuple[TestDatum, ...]
    oracles: tuple[SemanticOracle, ...]
    evidence_requirements: tuple[EvidenceRequirement, ...]
    assumptions: tuple[str, ...]
    missing_information: tuple[str, ...]
    unsupported_requests: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: Any) -> TestDesign:
        d = _object(value, DESIGN_FIELDS, 'TestDesign')
        if d['protocol_version'] != PROTOCOL_VERSION or d['document_type'] != 'test_design':
            raise ValueError('Expected v0.4 test_design')
        raw = _text(d['requirement'], 'requirement')
        requirements = []
        for row in _items(d['requirements'], 'requirements'):
            _object(row, 'requirement_id text source_quote kind', 'requirement')
            quote = _quote(row['source_quote'], raw)
            rid = _id(row['requirement_id'], 'R')
            if rid != requirement_id(raw, quote):
                raise ValueError('requirement_id must match requirement_id(original, source_quote)')
            requirements.append(Requirement(rid, _text(row['text'], 'requirement.text'), quote,
                _choice(row['kind'], ('action', 'assertion'), 'requirement.kind')))
        steps = []
        for row in _items(d['steps'], 'steps'):
            _object(row, 'step_id requirement_ids action target depends_on precondition_ids inputs outputs', 'step')
            t = _object(row['target'], 'description role position', 'target')
            position = t['position']
            if position is not None and (type(position) is not int or position < 1):
                raise ValueError('target.position must be null or a positive one-based integer')
            inputs = row['inputs']
            if type(inputs) is not dict or len(inputs) > 100:
                raise ValueError('step.inputs must be a name-to-reference object')
            steps.append(SemanticStep(_id(row['step_id'], 'S'), _strings(row['requirement_ids'], 'requirement_ids'),
                _choice(row['action'], ACTIONS, 'action'),
                SemanticTarget(_text(t['description'], 'target.description'), _text(t['role'], 'target.role'), position),
                _strings(row['depends_on'], 'depends_on'), _strings(row['precondition_ids'], 'precondition_ids'),
                tuple((_text(k, 'input name'), ValueReference.from_dict(v)) for k, v in inputs.items()),
                _strings(row['outputs'], 'outputs')))
        preconditions = []
        for row in _items(d['preconditions'], 'preconditions'):
            _object(row, 'precondition_id description status source_quote', 'precondition')
            status = _choice(row['status'], ('provided', 'unknown'), 'precondition.status')
            quote = _quote(row['source_quote'], raw, unknown=status == 'unknown')
            if status == 'unknown' and quote:
                raise ValueError('unknown precondition must have empty source_quote')
            preconditions.append(Precondition(_id(row['precondition_id'], 'P'),
                _text(row['description'], 'precondition.description'), status, quote))
        data = []
        for row in _items(d['test_data'], 'test_data'):
            _object(row, 'data_id value source_quote', 'test_data')
            quote, literal = _quote(row['source_quote'], raw), _scalar(row['value'])
            if type(literal) is str and literal not in quote:
                raise ValueError('String test data must occur in its source_quote')
            if type(literal) is not str and json.dumps(literal, allow_nan=False) not in quote:
                raise ValueError('Numeric/boolean test data must occur verbatim in source_quote')
            data.append(TestDatum(_id(row['data_id'], 'D'), literal, quote))
        evidence = []
        for row in _items(d['evidence_requirements'], 'evidence_requirements'):
            _object(row, 'evidence_id step_id output description kind', 'evidence')
            evidence.append(EvidenceRequirement(_id(row['evidence_id'], 'E'), _id(row['step_id'], 'S'),
                _text(row['output'], 'evidence.output'), _text(row['description'], 'evidence.description'),
                _choice(row['kind'], EVIDENCE_KINDS, 'evidence.kind')))
        oracles = []
        for row in _items(d['oracles'], 'oracles'):
            _object(row, 'oracle_id requirement_ids after_step_id relation actual expected evidence_ids', 'oracle')
            oracles.append(SemanticOracle(_id(row['oracle_id'], 'O'), _strings(row['requirement_ids'], 'requirement_ids'),
                _id(row['after_step_id'], 'S'), _choice(row['relation'], RELATIONS, 'oracle.relation'),
                ValueReference.from_dict(row['actual']), ValueReference.from_dict(row['expected']),
                _strings(row['evidence_ids'], 'evidence_ids')))
        result = cls(raw, validate_target_url(d['target_url']), _text(d['test_goal'], 'test_goal'),
            tuple(requirements), tuple(steps), tuple(preconditions), tuple(data), tuple(oracles), tuple(evidence),
            _strings(d['assumptions'], 'assumptions'), _strings(d['missing_information'], 'missing_information'),
            _strings(d['unsupported_requests'], 'unsupported_requests'))
        result._validate_links()
        return result

    def to_dict(self) -> dict[str, Any]:
        result = {'protocol_version': PROTOCOL_VERSION, 'document_type': 'test_design',
                  'requirement': self.requirement, 'target_url': self.target_url, 'test_goal': self.test_goal}
        for key in ('requirements', 'steps', 'preconditions', 'test_data', 'oracles', 'evidence_requirements'):
            result[key] = [row.to_dict() for row in getattr(self, key)]
        for key in ('assumptions', 'missing_information', 'unsupported_requests'):
            result[key] = list(getattr(self, key))
        return result

    def _validate_links(self) -> None:
        def indexed(rows, key):
            result = {getattr(row, key): row for row in rows}
            if len(result) != len(rows):
                raise ValueError(f'Duplicate {key}')
            return result

        requirements = indexed(self.requirements, 'requirement_id')
        steps = indexed(self.steps, 'step_id')
        preconditions = indexed(self.preconditions, 'precondition_id')
        data = indexed(self.test_data, 'data_id')
        evidence = indexed(self.evidence_requirements, 'evidence_id')
        indexed(self.oracles, 'oracle_id')
        if not requirements or not steps:
            raise ValueError('Dynamic design needs requirements and semantic steps')
        ancestors: dict[str, set[str]] = {}

        def visit(sid, visiting):
            if sid not in steps:
                raise ValueError(f'Unknown step reference: {sid}')
            if sid in visiting:
                raise ValueError('Step dependency cycle')
            if sid not in ancestors:
                parents = set(steps[sid].depends_on)
                for parent in steps[sid].depends_on:
                    parents.update(visit(parent, visiting | {sid}))
                ancestors[sid] = parents
            return ancestors[sid]

        for sid in steps:
            visit(sid, set())

        def reqs(ids):
            if not ids or not set(ids) <= requirements.keys():
                raise ValueError('Unknown or empty requirement_ids')

        def reference(ref, available, *, oracle=False):
            if ref.kind == 'test_data' and ref.ref_id not in data:
                raise ValueError('Unknown test_data reference')
            if ref.kind == 'step_output':
                if ref.ref_id not in available or ref.output not in steps[ref.ref_id].outputs:
                    raise ValueError('step_output requires a declared output of a dependency')
            if ref.kind == 'evidence':
                if ref.ref_id not in evidence or evidence[ref.ref_id].step_id not in available:
                    raise ValueError('Evidence reference not available at this step')
                if not oracle:
                    raise ValueError('Step inputs must reference data or step_output, not evidence')

        for step in self.steps:
            reqs(step.requirement_ids)
            if not set(step.precondition_ids) <= preconditions.keys():
                raise ValueError('Unknown precondition reference')
            for _, ref in step.inputs:
                reference(ref, ancestors[step.step_id])
                if ref.kind == 'literal':
                    raise ValueError('Step input data must use sourced test_data or step_output')
        for item in self.evidence_requirements:
            if item.step_id not in steps or item.output not in steps[item.step_id].outputs:
                raise ValueError('Evidence must reference a declared step output')
        for oracle in self.oracles:
            reqs(oracle.requirement_ids)
            if oracle.after_step_id not in steps:
                raise ValueError('Unknown oracle step')
            available = ancestors[oracle.after_step_id] | {oracle.after_step_id}
            reference(oracle.actual, available, oracle=True)
            reference(oracle.expected, available, oracle=True)
            if oracle.actual.kind != 'evidence' or oracle.expected.kind == 'step_output':
                raise ValueError('Oracle actual must be evidence; expected must be literal/data/evidence')
            used = {r.ref_id for r in (oracle.actual, oracle.expected) if r.kind == 'evidence'}
            if set(oracle.evidence_ids) != used:
                raise ValueError('Oracle evidence_ids must exactly match its operands')
            kinds = {evidence[eid].kind for eid in used}
            if 'screenshot' in kinds:
                raise ValueError('Screenshots are supporting artifacts, not scalar oracle operands')
            if oracle.relation == 'file_valid':
                if kinds != {'file'} or oracle.expected.kind != 'literal' or type(oracle.expected.value) is not bool:
                    raise ValueError('file_valid requires file evidence and a boolean expectation')
            elif 'file' in kinds:
                raise ValueError('File evidence requires file_valid')
            if oracle.relation in ('greater_than', 'less_than', 'greater_or_equal', 'less_or_equal'):
                if not kinds <= {'count', 'value', 'number'}:
                    raise ValueError('Ordered comparisons require numeric observable evidence')
                if oracle.expected.kind == 'literal' and type(oracle.expected.value) not in (int, float):
                    raise ValueError('Ordered comparisons require a numeric expected value')
            if oracle.expected.kind == 'literal' and type(oracle.expected.value) is str:
                if oracle.expected.value not in self.requirement:
                    raise ValueError('Literal expected text must come from the original requirement')

    def ordered_steps(self) -> tuple[SemanticStep, ...]:
        """Stable topological order for stage 2; wire list order is not execution order."""
        pending = list(self.steps)
        result, done = [], set()
        while pending:
            ready = next((s for s in pending if set(s.depends_on) <= done), None)
            if ready is None:
                raise ValueError('Invalid dependency graph')
            result.append(ready)
            done.add(ready.step_id)
            pending.remove(ready)
        return tuple(result)

    def validate_revision(self, previous: TestDesign) -> None:
        """Unchanged entities retain IDs; repairs may change their semantics."""
        if (self.requirement, self.target_url) != (previous.requirement, previous.target_url):
            raise ValueError('Revision cannot change original requirement or target website')
        for key, id_key in (('steps', 'step_id'), ('oracles', 'oracle_id'),
                            ('evidence_requirements', 'evidence_id'), ('test_data', 'data_id')):
            old = {}
            for row in getattr(previous, key):
                item = row.to_dict()
                old_id = item.pop(id_key)
                old[json.dumps(item, sort_keys=True, ensure_ascii=False)] = old_id
            for row in getattr(self, key):
                item = row.to_dict()
                new_id = item.pop(id_key)
                signature = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if signature in old and old[signature] != new_id:
                    raise ValueError(f'Unchanged entity must retain {id_key}')


def design_digest(design: TestDesign) -> str:
    payload = json.dumps(design.to_dict(), sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class DynamicTestContract:
    """Reviewed, immutable snapshot. Serialized consumers must verify the digest.

    Digest detects accidental changes, not a malicious party able to re-sign it.
    The only producer in the workflow is Agent2 after both review gates pass.
    """
    design: TestDesign
    revision: int
    approved_at: str
    design_digest: str

    @classmethod
    def freeze(cls, design: TestDesign, revision: int = 1) -> DynamicTestContract:
        snapshot = TestDesign.from_dict(design.to_dict())
        if type(revision) is not int or not 1 <= revision <= 2:
            raise ValueError('revision must be 1 or 2')
        return cls(snapshot, revision, datetime.now(timezone.utc).isoformat(timespec='seconds'), design_digest(snapshot))

    def assert_integrity(self) -> None:
        validated = TestDesign.from_dict(self.design.to_dict())
        if self.design_digest != design_digest(validated):
            raise ValueError('Frozen semantic contract digest mismatch')

    def to_dict(self) -> dict[str, Any]:
        self.assert_integrity()
        return {'protocol_version': PROTOCOL_VERSION, 'document_type': 'test_contract',
                'design': self.design.to_dict(), 'revision': self.revision,
                'approved_at': self.approved_at, 'design_digest': self.design_digest}

    @classmethod
    def from_dict(cls, value: Any) -> DynamicTestContract:
        d = _object(value, 'protocol_version document_type design revision approved_at design_digest', 'contract')
        if d['protocol_version'] != PROTOCOL_VERSION or d['document_type'] != 'test_contract':
            raise ValueError('Expected v0.4 test_contract')
        if type(d['revision']) is not int or not 1 <= d['revision'] <= 2:
            raise ValueError('Invalid contract revision')
        stamp = _text(d['approved_at'], 'approved_at')
        try:
            if datetime.fromisoformat(stamp).tzinfo is None:
                raise ValueError('Timestamp must include timezone')
        except ValueError as exc:
            raise ValueError('Invalid approved_at') from exc
        contract = cls(TestDesign.from_dict(d['design']), d['revision'], stamp,
                       _text(d['design_digest'], 'design_digest'))
        contract.assert_integrity()
        return contract


# Compact wire shape supplied to the model alongside the original requirement.
# Example IDs are placeholders; Agent1 assigns R_* IDs from exact source spans.
DESIGN_WIRE_GUIDE = {
    'protocol_version': '0.4', 'document_type': 'test_design',
    'requirement': 'exact original requirement', 'target_url': 'exact target URL', 'test_goal': 'goal',
    'requirements': [{'requirement_id': 'R_placeholder', 'text': 'atomic action or assertion',
                      'source_quote': 'exact source span', 'kind': 'action|assertion'}],
    'steps': [{'step_id': 'S_semantic_name', 'requirement_ids': ['R_placeholder'], 'action': '|'.join(ACTIONS),
               'target': {'description': 'semantic target, never CSS/XPath', 'role': 'semantic role', 'position': None},
               'depends_on': [], 'precondition_ids': [], 'inputs': {}, 'outputs': ['output_name']}],
    'preconditions': [{'precondition_id': 'P_session', 'description': 'needed state',
                       'status': 'provided|unknown', 'source_quote': 'exact span, empty for unknown'}],
    'test_data': [{'data_id': 'D_name', 'value': 'user-supplied scalar', 'source_quote': 'exact span containing value'}],
    'oracles': [{'oracle_id': 'O_semantic_name', 'requirement_ids': ['R_placeholder'], 'after_step_id': 'S_semantic_name',
                 'relation': '|'.join(RELATIONS), 'actual': {'kind': 'evidence', 'evidence_id': 'E_name'},
                 'expected': {'kind': 'literal', 'value': 'user-requested expectation'}, 'evidence_ids': ['E_name']}],
    'evidence_requirements': [{'evidence_id': 'E_name', 'step_id': 'S_semantic_name', 'output': 'output_name',
                               'description': 'observable supporting oracle', 'kind': '|'.join(EVIDENCE_KINDS)}],
    'assumptions': [], 'missing_information': [], 'unsupported_requests': [],
}
