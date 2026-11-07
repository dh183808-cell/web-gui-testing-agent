"""Schema and typed exchange artifacts for the six-role pipeline."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import math
from typing import Any, Literal, Protocol

VERDICTS = {"PASS", "FAIL", "ERROR", "INCONCLUSIVE"}
NUMERIC_ORACLES = {'gte', 'gt', 'lte', 'lt'}
ORACLE_GUIDE = '''Oracle kinds: equals (same JSON type and value), contains (text/list
membership), set_equals (list equality ignoring order), gte/gt/lte/lt (finite numeric
comparison, never boolean coercion). "At least one result" should use kind="gte",
expected=1 with read="count", OR kind="equals", expected=true with read="visible_exists".
A numeric count is NOT a boolean. Never change the expected value to fit observed data.'''


def finite_number(value):
    return type(value) is int or type(value) is float and math.isfinite(value)


def validate_oracle_binding(oracle, binding):
    """Check data types before executing code; no change to the frozen expectation."""
    kind, expected, read = oracle['kind'], oracle['expected'], binding['read']
    prefix = f'Step {oracle["step_id"]} Oracle {oracle["id"]}'
    if kind in NUMERIC_ORACLES or kind == 'equals' and finite_number(expected):
        allowed = {'count'}
    elif kind == 'equals' and type(expected) is bool:
        allowed = {'exists', 'visible_exists', 'selected', 'visible', 'enabled'}
    elif kind == 'set_equals' or kind == 'equals' and isinstance(expected, list):
        allowed = {'texts'}
    elif isinstance(expected, str):
        allowed = {'text', 'texts', 'selected_text'} if kind == 'contains' else {'text', 'attribute', 'selected_text'}
    else:
        allowed = set()
    if read not in allowed:
        raise ValueError(f'{prefix}: read={read!r} is incompatible with {kind} '
                         f'expected type {type(expected).__name__}; use {sorted(allowed)}. '
                         'For boolean existence use exists/visible_exists, not count; '
                         'quantity comparisons require a numeric Oracle reviewed by Agent1/2.')


def validate_dynamic_plan(plan):
    """Small semantic contract; execution selectors belong to Agent3, not this plan."""
    steps, oracles = plan.get('steps'), plan.get('oracles')
    if not isinstance(steps, list) or not 1 <= len(steps) <= 12:
        raise ValueError('Dynamic plan requires 1..12 steps')
    ids = []
    for i, step in enumerate(steps, 1):
        if step.get('id') != f'S{i}' or not isinstance(step.get('goal'), str) or not step['goal'].strip():
            raise ValueError('Invalid ordered semantic step')
        ids.append(step['id'])
    if not isinstance(oracles, list) or not 1 <= len(oracles) <= 12:
        raise ValueError('Dynamic plan requires explicit oracles')
    oracle_ids = set()
    for oracle in oracles:
        if (not isinstance(oracle.get('id'), str) or oracle['id'] in oracle_ids
                or oracle.get('step_id') not in ids or oracle.get('kind') not in {'equals','contains','set_equals'} | NUMERIC_ORACLES
                or 'expected' not in oracle or not isinstance(oracle.get('meaning'), str)):
            raise ValueError('Invalid semantic oracle')
        if oracle['kind'] in NUMERIC_ORACLES and not finite_number(oracle['expected']):
            raise ValueError(f'Oracle {oracle["id"]}: numeric comparison requires a finite number, not bool')
        quote = oracle.get('source_quote')
        if quote is not None and (not isinstance(quote, str) or not quote.strip()
                                  or quote not in plan.get('requirement', '')):
            raise ValueError(f'Oracle {oracle["id"]}: source_quote must quote the original requirement exactly')
        oracle_ids.add(oracle['id'])


class JSONModel(Protocol):
    """Shared optional A1/A2/A3 dependency, also implementable by test doubles.

    Provider configuration and credentials belong to llm_adapter. This protocol
    adds no runtime validation or model instance; errors propagate to the caller.
    """

    def complete_json(self, system: str, user: str) -> dict[str, Any]: ...


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class QuerySpec:
    keyword: str
    search_field: str = "all"
    computer_science: bool = False
    open_first_result: bool = True

    def validate(self) -> None:
        if not isinstance(self.keyword, str) or not (1 <= len(self.keyword.strip()) <= 120):
            raise ValueError("keyword must contain between 1 and 120 characters")
        if self.search_field not in {"all", "title", "abstract", "author"}:
            raise ValueError("unsupported search field")
        if not isinstance(self.computer_science, bool) or not isinstance(self.open_first_result, bool):
            raise ValueError("computer_science and open_first_result must be booleans")


@dataclass
class TestContract:
    case_id: str
    requirement: str
    query: dict[str, Any]
    steps: list[dict[str, Any]]
    oracles: list[dict[str, Any]]
    evidence_plan: list[dict[str, Any]]
    mode: str
    target: str
    approved_at: str = field(default_factory=now)
    revision: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Verdict:
    status: Literal["PASS", "FAIL", "ERROR", "INCONCLUSIVE"]
    message: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
