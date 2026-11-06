"""Closed operation language, policy and canonical Python renderer for v0.4.

Model output is data, never Python. The worker independently validates the
contract/step/observation binding and requires exact canonical source equality.
This is a capability boundary, not an attempt to sandbox arbitrary Python.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import ipaddress
import json
import math
from urllib.parse import urlsplit

from .dynamic_schema import DynamicTestContract, SemanticStep, validate_target_url


class ExecutionRejected(ValueError):
    def __init__(self, message, *, category='invalid_code'):
        super().__init__(message)
        self.category = category


def origin(url: str) -> str:
    validate_target_url(url)
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    return f'{parts.scheme}://{parts.hostname.lower()}:{port}'


@dataclass(frozen=True)
class ExecutionPolicy:
    max_steps: int = 40
    max_observations: int = 85
    step_timeout: float = 8.0
    total_timeout: float = 120.0
    startup_timeout: float = 25.0
    max_elements: int = 100
    allow_live_site: bool = False
    max_probes: int = 1
    max_repairs: int = 1
    allow_live_probes: bool = False
    allow_live_recovery: bool = False
    allow_downloads: bool = False
    allowed_actions: tuple[str, ...] = ('navigate', 'fill', 'click', 'select', 'check', 'submit', 'observe', 'wait', 'download')

    def __post_init__(self):
        for name, maximum in (('max_steps', 100), ('max_observations', 250), ('max_elements', 150)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f'Invalid {name}')
        for name, maximum in (('step_timeout', 30), ('total_timeout', 600), ('startup_timeout', 60)):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
                raise ValueError(f'Invalid {name}')
        if type(self.allow_live_site) is not bool:
            raise ValueError('allow_live_site must be boolean')
        for name in ('max_probes', 'max_repairs'):
            if type(getattr(self, name)) is not int or not 0 <= getattr(self, name) <= 3:
                raise ValueError(f'Invalid {name}')
        for name in ('allow_live_probes', 'allow_live_recovery', 'allow_downloads'):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f'Invalid {name}')
        if (self.allow_live_probes or self.allow_live_recovery) and not self.allow_live_site:
            raise ValueError('Extra live permissions require allow_live_site')
        from .dynamic_schema import ACTIONS
        if (not isinstance(self.allowed_actions, (tuple, list)) or not self.allowed_actions or
                any(type(a) is not str or a not in ACTIONS for a in self.allowed_actions) or
                len(set(self.allowed_actions)) != len(self.allowed_actions)):
            raise ValueError('Invalid allowed_actions')
        object.__setattr__(self, 'allowed_actions', tuple(self.allowed_actions))

    def to_dict(self) -> dict:
        return asdict(self)

    def authorize(self, url: str, target_url: str) -> None:
        if origin(url) != origin(target_url):
            raise ExecutionRejected('Navigation/network request outside the reviewed origin', category='permission')
        hostname = urlsplit(url).hostname
        try:
            local = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            local = hostname.lower() == 'localhost'
        if not local and not self.allow_live_site:
            raise ExecutionRejected('Live dynamic execution needs explicit allow_live_site', category='permission')


def step_for(contract: DynamicTestContract, step_id: str) -> SemanticStep:
    contract.assert_integrity()
    found = [s for s in contract.design.steps if s.step_id == step_id]
    if len(found) != 1:
        raise ExecutionRejected('Unknown step_id')
    return found[0]


def resolve_inputs(contract: DynamicTestContract, step: SemanticStep, state: dict) -> dict:
    data = {d.data_id: d.value for d in contract.design.test_data}
    result = {}
    for key, ref in step.inputs:
        if ref.kind == 'test_data':
            result[key] = data[ref.ref_id]
        elif ref.kind == 'step_output':
            if ref.ref_id not in state or ref.output not in state[ref.ref_id]:
                raise ExecutionRejected('Required previous output is not available')
            result[key] = state[ref.ref_id][ref.output]
        else:
            raise ExecutionRejected('Unsupported dynamic input reference')
        if type(result[key]) not in (str, int, float, bool) or len(str(result[key])) > 2000:
            raise ExecutionRejected('Input must be a bounded scalar')
    return result


def validate_operation(contract: DynamicTestContract, step_id: str, observation: dict,
                       operation: dict, inputs: dict, policy: ExecutionPolicy) -> None:
    step = step_for(contract, step_id)
    if step.action not in policy.allowed_actions:
        raise ExecutionRejected('Reviewed action disabled by execution policy', category='permission')
    if type(operation) is not dict or set(operation) != {
        'step_id', 'observation_id', 'target_id', 'wait', 'reads', 'expected_change', 'rationale'}:
        raise ExecutionRejected('Operation must contain exactly the allowlisted fields; no Python/selectors')
    if operation['step_id'] != step_id or operation['observation_id'] != observation['observation_id']:
        raise ExecutionRejected('Stale observation or wrong step binding')
    if type(operation['rationale']) is not str or not 1 <= len(operation['rationale']) <= 600:
        raise ExecutionRejected('A bounded locator selection rationale is required')
    if operation['wait'] not in ('visible', 'enabled', 'ready'):
        raise ExecutionRejected('Unsupported wait condition')
    if operation['expected_change'] not in ('none', 'dom', 'navigation'):
        raise ExecutionRejected('Unsupported page transition')
    if type(operation['reads']) is not dict or set(operation['reads']) != set(step.outputs):
        raise ExecutionRejected('Every declared step output must have exactly one read')
    from .dynamic_verification import READERS
    if any(type(v) is not str or v not in set(READERS.values())
           for v in operation['reads'].values()):
        raise ExecutionRejected('Unsupported output reader')
    evidence_kinds = READERS
    for item in contract.design.evidence_requirements:
        if item.step_id == step_id and operation['reads'].get(item.output) != evidence_kinds.get(item.kind):
            raise ExecutionRejected('Output reader does not match reviewed evidence type')
    if step.action not in ('navigate', 'click', 'submit') and operation['expected_change'] != 'none':
        raise ExecutionRejected('Unexpected transition for this reviewed action')
    if step.action == 'navigate':
        if operation['target_id'] is not None or operation['wait'] != 'ready':
            raise ExecutionRejected('Navigation must use the reviewed URL, without a locator')
        policy.authorize(inputs.get('url', contract.design.target_url), contract.design.target_url)
        if set(inputs) - {'url'}:
            raise ExecutionRejected('Unsupported navigation inputs')
        if any(v not in ('url',) for v in operation['reads'].values()):
            raise ExecutionRejected('Navigation may only produce a URL output')
        return
    nodes = {n['node_id']: n for n in observation['elements']}
    if step.action == 'observe' and operation['target_id'] is None and operation['reads'] and set(operation['reads'].values()) <= {'exists', 'visible'}:
        if step.target.role != 'exact_aria_label' or step.target.position is not None:
            raise ExecutionRejected('Absent target requires an explicit exact_aria_label semantic target')
        if observation.get('truncated', True) or any(n.get('name') == step.target.description for n in nodes.values()):
            raise ExecutionRejected('Absence requires an exhaustive observation and exact semantic name')
        if inputs:
            raise ExecutionRejected('Observation may not consume action inputs')
        return
    node = nodes.get(operation['target_id']) if type(operation['target_id']) is str else None
    if node is None or node.get('sensitive'):
        raise ExecutionRejected('Target must be a non-sensitive node in the live observation')
    if not node['visible'] and not (step.action == 'observe' and set(operation['reads'].values()) <= {'exists', 'visible', 'enabled', 'checked'}):
        raise ExecutionRejected('Target is not visible')
    if step.target.position is not None and node.get('name'):
        peers = [n for n in observation['elements'] if n['tag'] == node['tag']
                 and n.get('name') == node['name'] and n['visible']]
        if len(peers) > 1 and (step.target.position > len(peers)
                               or peers[step.target.position - 1]['node_id'] != node['node_id']):
            raise ExecutionRejected('Selected live node violates the reviewed ordinal')
    if step.action in ('fill', 'click', 'select', 'check', 'submit') and not node['enabled']:
        raise ExecutionRejected('Target is disabled')
    if step.action == 'fill' and (node['tag'] not in ('input', 'textarea') or
                                  node['type'] in ('password', 'file', 'hidden', 'checkbox', 'radio', 'submit')):
        raise ExecutionRejected('Fill requires a non-sensitive text field')
    if step.action == 'select' and node['tag'] != 'select':
        raise ExecutionRejected('Select requires a native select element')
    if step.action == 'check' and (node['tag'] != 'input' or node['type'] not in ('checkbox', 'radio')):
        raise ExecutionRejected('Check requires a checkbox/radio')
    if step.action == 'submit' and not (node['tag'] == 'button' or
            node['tag'] == 'input' and node['type'] == 'submit'):
        raise ExecutionRejected('Submit requires a visible submit control')
    if step.action == 'download':
        if not policy.allow_downloads or node['tag'] != 'a' or not operation['reads'] or set(operation['reads'].values()) != {'file'}:
            raise ExecutionRejected('Downloads require explicit permission, an anchor and reviewed file output', category='permission')
    elif 'file' in operation['reads'].values():
        raise ExecutionRejected('File collection requires a reviewed download action')
    if step.action in ('fill', 'select', 'check'):
        if set(inputs) != {'value'}:
            raise ExecutionRejected('Control operation requires exactly the reviewed value input')
        if step.action == 'check' and type(inputs['value']) is not bool:
            raise ExecutionRejected('Checkbox value must be boolean')
        if step.action != 'check' and type(inputs['value']) is not str:
            raise ExecutionRejected('Text/select value must be a string')
    elif inputs:
        raise ExecutionRejected('Inputs not consumed by this operation are unsupported')


def render_source(contract: DynamicTestContract, operation: dict) -> str:
    """Only trusted literals and fixed Selenium capability calls reach Python.

    A canonical exact-source check is performed before compilation in the worker.
    No model expressions, attribute names, selectors or executable waits appear.
    """
    step = step_for(contract, operation['step_id'])
    lines = [f'# WebAgent v0.4 step {step.step_id}; contract {contract.design_digest}', 'outputs = {}']
    if step.action == 'navigate':
        lines += ["api.navigate(inputs.get('url', target_url))", "api.ready()"]
    else:
        state_read = step.action == 'observe' and set(operation['reads'].values()) <= {'exists', 'visible', 'enabled', 'checked'}
        method = 'lookup' if state_read else 'wait'
        lines += [f"element = api.{method}({operation['target_id']!r}, {operation['wait']!r})"]
        if step.action in ('click', 'submit'):
            lines += ['before = api.page_token()', 'api.guard_target(element)', 'element.click()',
                      f"api.transition(before, {operation['expected_change']!r})"]
        elif step.action == 'fill':
            lines += ['element.clear()', "element.send_keys(inputs['value'])"]
        elif step.action == 'select':
            lines += ["api.select_by_visible_text(element, inputs['value'])"]
        elif step.action == 'check':
            lines += ["if element.is_selected() != inputs['value']:", '    element.click()']
        elif step.action == 'download':
            lines += ['api.download(element)']
        elif step.action not in ('observe', 'wait'):
            raise ExecutionRejected('Unsupported action')
    for output, reader in operation['reads'].items():
        element = 'None' if step.action == 'navigate' else 'element'
        lines.append(f'outputs[{output!r}] = api.capture({output!r}, {reader!r}, {element})')
    return '\n'.join(lines) + '\n'


def source_digest(source: str) -> str:
    return hashlib.sha256(source.encode('utf-8')).hexdigest()
