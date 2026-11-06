"""Schema and typed exchange artifacts for the six-role pipeline."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

# Semantic contracts remain v0.4; provenance-bound dynamic evidence has its own
# v0.4.1 envelope (dynamic_verification). Legacy QuerySpec/TestContract stay unchanged.
from .dynamic_schema import DynamicTestContract, TestDesign

VERDICTS = {"PASS", "FAIL", "ERROR", "INCONCLUSIVE"}


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


def parse_contract(document: dict[str, Any]) -> TestContract | DynamicTestContract:
    """Parse a persisted contract; unversioned documents are the v0.3 wire format.

    Callers must dispatch on the returned type. A dynamic contract must never
    be passed into the fixed v0.3 generator/runtime as if it were a QuerySpec.
    """
    if type(document) is not dict:
        raise ValueError('Contract must be a JSON object')
    version = document.get('protocol_version', '0.3')
    if version == '0.4':
        return DynamicTestContract.from_dict(document)
    if version != '0.3':
        raise ValueError('Unsupported contract protocol_version')
    data = {k: v for k, v in document.items() if k != 'protocol_version'}
    try:
        contract = TestContract(**data)
        QuerySpec(**contract.query).validate()
    except (TypeError, KeyError) as exc:
        raise ValueError('Invalid legacy contract') from exc
    return contract
